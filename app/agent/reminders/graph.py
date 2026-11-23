"""
app/agent/reminders/graph.py
───────────────────────────
The agent for OUTBOUND reminder calls. A separate, smaller graph, so the inbound flow is untouched:

START -> urgency_check -+-> escalate -> END                 (emergency words: transfer to the doctor)
                        +-> assistant <-> tools -> END      (the reminder conversation)

Each patient turn runs the graph once. The appointment id arrives in config["configurable"]["appt_id"].
When the decision is saved and the assistant has said goodbye, it sets action="hangup".
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage, trim_messages  # type: ignore
from langchain_core.runnables import RunnableConfig  # type: ignore
from langchain_core.tools import tool  # type: ignore
from langgraph.graph import END, START, StateGraph  # type: ignore
from langgraph.prebuilt import ToolNode, tools_condition  # type: ignore

from app.agent.llm import get_llm
from app.agent.shared.loader import current_language, pick_reply
from app.agent.intake.nodes import urgency_check
from app.agent.intake.prompts import LANGUAGE_NAMES
from app.agent.state import CallState
from app.agent.intake.tools import request_callback
from app.services.outbound import appointments as appointment_service
from app.services.inbound import call_logs as call_service
from app.services.shared.notifications import notify_doctor

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# ==================================================
# OPENING LINE (spoken by the phone layer, before the agent runs)
# ==================================================
# Please have someone who speaks Odia and Hindi read these aloud once and fix the wording.

_GREETING = {
    "en": "Hello, this is the AI assistant of Dr. Pushpak Samal's clinic. Am I speaking with {name}? This call may be recorded.",
    "hi": "नमस्ते, मैं डॉक्टर पुष्पक सामल के क्लिनिक की AI सहायक बोल रही हूँ। क्या मैं {name} जी से बात कर रही हूँ? यह कॉल रिकॉर्ड हो सकती है।",
    "or": "ନମସ୍କାର, ମୁଁ ଡାକ୍ତର ପୁଷ୍ପକ ସାମଲଙ୍କ କ୍ଲିନିକର AI ସହାୟକ। ମୁଁ {name}ଙ୍କ ସହ କଥା ହେଉଛି କି? ଏହି କଲ୍ ରେକର୍ଡ ହୋଇପାରେ।",
}


def reminder_greeting(lang: str, name: str) -> str:
    """The first thing the patient hears. It only names the clinic and asks who is on the line."""
    return _GREETING.get(lang, _GREETING["en"]).format(name=name)


# ==================================================
# TOOLS
# ==================================================


@tool
def record_reminder_outcome(
    outcome: Literal["confirmed", "reschedule", "cancel", "wrong_person"],
    config: RunnableConfig,
    feeling: str = "",
    note: str = "",
) -> str:
    """Save the result of the reminder call. Call it once the conversation has reached a result:
    - confirmed: the patient will come to the appointment;
    - reschedule: the patient wants a different time (put their preferred time in `note`);
    - cancel: the patient will not come;
    - wrong_person: the person on the phone is not the patient, or the patient cannot talk now.
    `feeling` is a short note of how the patient says they feel (pain, movement), in English."""
    cfg = config["configurable"]
    appointment_id = cfg.get("appt_id")
    saved = appointment_service.record_outcome(appointment_id, outcome, feeling, note)

    if saved and outcome in ("reschedule", "cancel"):
        brief = appointment_service.get_brief(appointment_id)
        call_service.create_callback(
            call_id=cfg["thread_id"],
            phone=cfg.get("caller_phone", "unknown"),
            name=brief["name"] if brief else None,
            issue=f"Patient wants to {outcome} the appointment. {note}".strip(),
            urgency="routine",
        )
    return "Outcome saved." if saved else "The outcome could NOT be saved."


REMINDER_TOOLS = [record_reminder_outcome, request_callback]

# ==================================================
# PROMPT
# ==================================================


def _describe_time(when: datetime) -> str:
    """'tomorrow at 10:30 AM' / 'today at 4:00 PM' / 'on 20 November at 10:30 AM' (in Indian time)."""
    local = when.astimezone(IST)
    today = datetime.now(IST).date()
    if local.date() == today:
        day = "today"
    elif local.date() == today + timedelta(days=1):
        day = "tomorrow"
    else:
        day = f"on {local.day} {local.strftime('%B')}"
    return f"{day} at {local.strftime('%I:%M %p').lstrip('0')}"


def build_reminder_prompt(appointment: dict) -> str:
    """Instructions for this call. The opening line (who is speaking, 'am I speaking with ...?') was already said."""
    language = LANGUAGE_NAMES.get(current_language(), "English")
    name = appointment["name"]
    when = _describe_time(appointment["appointment_at"])
    return f"""You are the AI phone assistant of Dr. Pushpak Samal's orthopedic clinic in Bhubaneswar.
You are making a REMINDER CALL to {name} about an appointment {when}.

HOW TO SPEAK (your words are spoken aloud on a phone call):
- Warm, calm and natural. Reply in 1-2 short sentences and ask only one question at a time.
- Plain, simple words. No lists, markdown, emojis or medical jargon.
- Speak {language}, in that language's own script, unless the patient clearly switches language.
- Always use the respectful "you" (ଆପଣ in Odia, आप in Hindi).
- You are an AI assistant. Say so honestly if asked.
- Never introduce yourself or the clinic again: the opening line already did. Do not repeat "I am the AI assistant".

CALL PLAN (you already asked "Am I speaking with {name}?"):
1. If the person is NOT {name}, or {name} cannot talk now: do not mention the appointment or anything about health.
   Ask when a good time to call back would be, call record_reminder_outcome with outcome='wrong_person', and say goodbye.
2. If it is {name}: remind them of the appointment {when} at Dr. Pushpak Samal's clinic and ask whether they can come.
3. Then ask, kindly, how they are feeling, and whether the pain or movement is better, the same or worse.
   Listen and acknowledge briefly in your own words.
4. Call record_reminder_outcome: confirmed (will come), reschedule (wants another time) or cancel (will not come).
   Put what they said about how they feel into `feeling`.
5. After it is saved, say a short warm goodbye. Do not start any new topic after the goodbye.

STRICT RULES:
- Never diagnose, suggest medicines, exercises or treatment, and never say whether surgery is needed.
  If asked, say the doctor will guide them at the visit, then call request_callback with their question.
- If they describe severe pain, a fall, numbness, or that things are getting much worse: reassure them and call
  request_callback with urgency='urgent'. Then continue the plan.
- You cannot book or change appointments. For a new time, note their preference and say the clinic team will call to confirm.
- Use only the appointment details above. Never invent timings, fees or doctors.
- Never reveal any other patient's information.
"""


# ==================================================
# NODES
# ==================================================


def _last_user_text(state: CallState) -> str:
    for message in reversed(state["messages"]):
        if isinstance(message, HumanMessage):
            return message.content
    return ""


def _outcome_saved_this_turn(messages) -> bool:
    """True if record_reminder_outcome ran since the patient's latest message."""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return False
        if isinstance(message, ToolMessage) and message.name == "record_reminder_outcome":
            return "could not" not in str(message.content).lower()
    return False


def _said_goodbye(text: str) -> bool:
    """True if the reply is a real sign-off (a farewell and no new question)."""
    if not text or "?" in text:
        return False
    lowered = text.lower()
    return any(word in lowered for word in _GOODBYES)


_GOODBYES = (
    "goodbye", "good bye", "bye", "take care", "see you", "have a good", "have a nice",
    "thank you for your time", "thanks for your time",
    "ଧନ୍ୟବାଦ", "ନମସ୍କାର", "शुभ", "धन्यवाद", "नमस्ते", "फिर मिलें",
)


def reminder_escalate(state: CallState, config: RunnableConfig):
    """Emergency words during a reminder call: save a callback, alert the doctor, transfer the call."""
    cfg = config["configurable"]
    phone = state.get("caller_phone", "unknown")
    issue = _last_user_text(state)
    brief = appointment_service.get_brief(cfg.get("appt_id"))
    name = brief["name"] if brief else None

    call_service.create_callback(call_id=cfg["thread_id"], phone=phone, name=name, issue=issue, urgency="emergency")
    notify_doctor(f"{name or 'A patient'} ({phone}) said during a reminder call: {issue}", level="emergency")
    return {
        "messages": [AIMessage(pick_reply("emergency_transfer"))],
        "action": "transfer",
        "urgency": "emergency",
    }


def reminder_assistant(state: CallState, config: RunnableConfig):
    """The reminder conversation. Once the decision is saved and the goodbye is said, ask the phone layer to hang up."""
    appointment = appointment_service.get_brief(config["configurable"].get("appt_id"))
    if not appointment:
        logger.error("❌ [Reminder] Appointment not found for this call")
        return {"messages": [AIMessage(pick_reply("error"))], "action": "hangup"}

    try:
        llm = get_llm().bind_tools(REMINDER_TOOLS)
        recent = trim_messages(state["messages"], strategy="last", token_counter=len, max_tokens=30, start_on="human")
        response = llm.invoke([SystemMessage(build_reminder_prompt(appointment))] + recent)
    except Exception:
        logger.exception("❌ [Reminder] LLM call failed")
        return {"messages": [AIMessage(pick_reply("error"))]}

    update = {"messages": [response]}
    if (not response.tool_calls
            and _outcome_saved_this_turn(state["messages"])
            and _said_goodbye(str(response.content or ""))):
        update["action"] = "hangup"
    return update


def route_after_urgency(state: CallState) -> str:
    return "escalate" if state.get("turn_level") == "emergency" else "assistant"


def build_reminder_graph(checkpointer):
    """Wire the nodes together and compile with the same checkpointer as the inbound graph."""
    builder = StateGraph(CallState)
    builder.add_node("urgency_check", urgency_check)
    builder.add_node("escalate", reminder_escalate)
    builder.add_node("assistant", reminder_assistant)
    builder.add_node("tools", ToolNode(REMINDER_TOOLS))

    builder.add_edge(START, "urgency_check")
    builder.add_conditional_edges("urgency_check", route_after_urgency, ["escalate", "assistant"])
    builder.add_edge("escalate", END)
    builder.add_conditional_edges("assistant", tools_condition)   # tool call -> tools, else END
    builder.add_edge("tools", "assistant")                        # after saving, speak the goodbye

    return builder.compile(checkpointer=checkpointer)