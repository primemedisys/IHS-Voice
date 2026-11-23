"""
app/agent/identity/nodes.py

Inbound identity steps. These nodes return state changes for LangGraph.
"""

import logging

from langchain_core.messages import AIMessage, HumanMessage # type: ignore

from app.agent.identity.lookup import extract_identity_turn
from app.agent.shared.loader import pick_reply
from app.agent.intake.nodes import MAX_VERIFY_ATTEMPTS
from app.agent.state import CallState
from app.database import get_session
from app.models.patient import Patient
from app.services.shared import patients as patient_service

logger = logging.getLogger(__name__)


# ==================================================
# HELPERS
# ==================================================

def _last_user_text(state: CallState) -> str:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""


# ==================================================
# NEW CALLER
# ==================================================

def collect_new_identity(state: CallState) -> dict:
    """Collect a new caller's name and DOB, one question at a time."""
    name = state.get("given_name")
    dob = state.get("given_dob")
    stage = "new_name" if not name else "new_dob"

    found = extract_identity_turn(_last_user_text(state), stage)
    if found is None:
        return {"messages": [AIMessage(pick_reply("not_understood"))]}

    if found.refused:
        return {
            "identity_status": "refused",
            "identity_stage": "declined",
            "messages": [AIMessage(pick_reply("identity_refused"))],
        }

    name = found.full_name or name
    dob = found.date_of_birth or dob

    if not name:
        return {
            "identity_stage": "new_name",
            "given_dob": dob,
            "messages": [AIMessage(pick_reply("new_ask_name"))],
        }

    if not dob:
        return {
            "identity_stage": "new_dob",
            "given_name": name,
            "messages": [AIMessage(pick_reply("new_ask_dob"))],
        }

    # The graph will continue to the assistant to ask about the concern.
    return {
        "identity_stage": "complete",
        "identity_status": "not_required",
        "patient_name": name,
        "given_name": name,
        "given_dob": dob,
    }


# ==================================================
# EXISTING PATIENT
# ==================================================

def verify_existing_identity(state: CallState) -> dict:
    """Confirm the caller, then verify a separately supplied name and DOB."""
    stage = state.get("identity_stage") or "confirm_name"
    if stage not in ("confirm_name", "verify_name", "verify_dob"):
        stage = "confirm_name"

    found = extract_identity_turn(_last_user_text(state), stage)
    if found is None:
        return {"messages": [AIMessage(pick_reply("not_understood"))]}

    if found.refused:
        return {
            "identity_status": "refused",
            "messages": [AIMessage(pick_reply("identity_refused"))],
        }

    if stage == "confirm_name":
        if found.third_party or found.confirmation == "no":
            return {
                "identity_status": "failed",
                "name_confirmed": False,
                "messages": [AIMessage(pick_reply("someone_else_answering"))],
            }

        if found.confirmation != "yes":
            return {"messages": [AIMessage(pick_reply("not_understood"))]}

        return {
            "name_confirmed": True,
            "identity_status": "pending",
            "identity_stage": "verify_name",
            "messages": [AIMessage(pick_reply("verify_ask_name"))],
        }

    name = found.full_name or state.get("given_name")
    dob = found.date_of_birth or state.get("given_dob")

    if not name:
        return {
            "identity_stage": "verify_name",
            "given_dob": dob,
            "messages": [AIMessage(pick_reply("verify_ask_name"))],
        }

    if not dob:
        return {
            "identity_stage": "verify_dob",
            "given_name": name,
            "messages": [AIMessage(pick_reply("verify_ask_dob"))],
        }

    try:
        with get_session() as db:
            patient = db.get(Patient, state.get("patient_id"))
            matched = patient is not None and patient_service.verify(patient, name, dob)
            registered_name = patient.full_name if patient else ""
    except Exception:
        logger.exception("Could not verify patient identity")
        return {"messages": [AIMessage(pick_reply("error"))]}

    if matched:
        return {
            "is_verified": True,
            "identity_status": "verified",
            "identity_stage": "complete",
            "patient_name": registered_name,
            "given_name": None,
            "given_dob": None,
            "messages": [
                AIMessage(
                    pick_reply("verified").format(
                        first_name=registered_name.split()[0]
                    )
                )
            ],
        }

    attempts = state.get("verify_attempts", 0) + 1
    failed = attempts >= MAX_VERIFY_ATTEMPTS

    return {
        "verify_attempts": attempts,
        "identity_status": "failed" if failed else "pending",
        "identity_stage": "verify_name",
        "given_name": None,
        "given_dob": None,
        "messages": [
            AIMessage(
                pick_reply("identity_failed_offer" if failed else "verify_retry")
            )
        ],
    }