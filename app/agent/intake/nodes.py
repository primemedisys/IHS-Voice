"""
app/agent/intake/nodes.py
──────────────────
The steps of the graph. Each node receives the state and returns the changes to make.
Order on every caller message:
    lookup_patient -> urgency_check -> (escalate | identity | assistant)

Error rule: a node never crashes the call. On failure it logs and returns a safe reply.
Fixed lines come from pick_reply(), which rotates between approved variants.
"""

import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, trim_messages # type: ignore
from langchain_core.runnables import RunnableConfig # type: ignore

from app.agent.llm import get_llm
from app.agent.shared.loader import pick_reply
from app.agent.intake.prompts import build_system_prompt
from app.agent.shared.rules import classify
from app.agent.state import CallState
from app.agent.intake.tools import TOOLS
from app.database import get_session
from app.models.patient import Patient
from app.services.inbound import call_logs as call_service
from app.services.shared import patients as patient_service
from app.services.shared.notifications import notify_doctor

logger = logging.getLogger(__name__)

MAX_VERIFY_ATTEMPTS = 2
RANK = { "routine": 0, "urgent": 1, "emergency": 2 }

# ==================================================
# HELPERS
# ==================================================

def _last_user_text(state: CallState) -> str:
    """Return the caller's latest message text."""
    for m in reversed(state["messages"]):
        if isinstance(m, HumanMessage):
            return m.content
    return ""

# ==================================================
# NODES
# ==================================================

def lookup_patient(state: CallState):
    """Find out who is calling (once per call). If the DB fails, treat them as NEW so nothing private is revealed."""
    if state.get("caller_type"):
        return {"caller_type": state["caller_type"]}

    phone = patient_service.normalize_phone(state.get("caller_phone", ""))
    patient_id = None
    try:
        with get_session() as db:
            patient = patient_service.find_by_phone(db, phone)
            patient_id = patient.id if patient else None
    except Exception:
        logger.exception("❌ [Node:lookup] DB lookup failed, treating caller as new")

    return {
        "caller_type": "existing" if patient_id else "new",
        "patient_id": patient_id,
        "is_verified": False,
        "verify_attempts": 0,
        "urgency": "routine",
    }

def urgency_check(state: CallState):
    """Check EVERY message for emergencies before the LLM sees it. If the check itself fails, assume 'urgent' to be safe."""
    try:
        level = classify(_last_user_text(state))
    except Exception:
        logger.exception("❌ [Node:urgency] Rule check failed, assuming urgent")
        level = "urgent"
    highest = max(state.get("urgency", "routine"), level, key=RANK.get)
    return {"turn_level": level, "urgency": highest}

def escalate(state: CallState, config: RunnableConfig):
    """Emergency: save a callback, alert the doctor, and ask the phone layer to transfer. Always transfers, even if saving fails."""
    call_id = config["configurable"]["thread_id"]
    phone = state.get("caller_phone", "unknown")
    issue = _last_user_text(state)

    name = None
    
    try:
        if state.get("patient_id"):
            with get_session() as db:
                patient = db.get(Patient, state["patient_id"])
                name = patient.full_name if patient else None
    except Exception:
        logger.exception("❌ [Node:escalate] Could not load patient name")

    call_service.create_callback(call_id=call_id, phone=phone, name=name, issue=issue, urgency="emergency")
    notify_doctor(f"{name or 'Unknown caller'} ({phone}) said: {issue}", level="emergency")

    return {
        "messages": [AIMessage(pick_reply("emergency_transfer"))],
        "action": "transfer",    # the phone layer reads this and transfers the call
        "urgency": "emergency",
    }

def assistant(state: CallState, config: RunnableConfig):
    """Normal conversation: the LLM answers or calls a tool. On LLM failure the caller hears an approved error line."""
    try:
        llm = get_llm().bind_tools(TOOLS)
        # Keep the last 30 messages so long calls stay fast and cheap
        recent = trim_messages(state["messages"], strategy="last", token_counter=len, max_tokens=30, start_on="human")
        response = llm.invoke([SystemMessage(build_system_prompt(state))] + recent)
        return {"messages": [response], "intake_turns": state.get("intake_turns", 0) + 1}
    except Exception:
        logger.exception("❌ [Node:assistant] LLM call failed")
        return {"messages": [AIMessage(pick_reply("error"))]}

