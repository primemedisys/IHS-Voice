"""
app/agent/booking/nodes.py
──────────────────────────
The end of the inbound call, decided by code (not by the LLM).

Graph nodes:
    offer_booking   intake just finished -> fixed line: "book an appointment? connect you now?"
    booking_flow    handles the caller's answer in the phases offer / slot / anything_else

Output contract (what the phone layer reads):
    messages[-1]   what the bot says
    action         None | "receptionist" (connect to the assistant) | "end" (say it, then hang up)
    phase          where the call is now (see flow.py)

The LLM is used only to answer a clinic question; every other sentence is a fixed reply.
A node never crashes the call: on any failure the caller hears an approved line.
"""

import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, trim_messages  # type: ignore

from app.agent.booking import flow
from app.agent.booking.extractors import classify_choice, extract_slot, is_no
from app.agent.shared.loader import current_language, pick_reply
from app.agent.intake.prompts import build_system_prompt
from app.agent.state import CallState

logger = logging.getLogger(__name__)

# The phone layer sends this as the "caller message" when the caller stayed silent.
SILENCE = "[[silence]]"


# ==================================================
# SMALL HELPERS
# ==================================================

def _last_user_text(state: CallState) -> str:
    for message in reversed(state["messages"]):
        if isinstance(message, HumanMessage):
            return message.content or ""
    return ""


def _out(text: str, **updates) -> dict:
    return {"messages": [AIMessage(text)], **updates}


def _slot_from_state(state: CallState) -> flow.Slot:
    day = state.get("slot_date")
    from datetime import date

    return flow.Slot(
        day=date.fromisoformat(day) if day else None,
        hhmm=state.get("slot_time"),
        bucket=state.get("slot_bucket"),
    )


def _slot_updates(slot: flow.Slot) -> dict:
    return {
        "slot_date": slot.day.isoformat() if slot.day else None,
        "slot_time": slot.hhmm,
        "slot_bucket": slot.bucket,
    }


def _save_slot(state: CallState, config, slot: flow.Slot, text: str) -> bool:
    """Save the request; a save failure is logged and the call still continues."""
    from app.services.inbound.booking_request import save_preferred_slot

    cfg = (config or {}).get("configurable") or {}
    call_id = cfg.get("thread_id", "")
    phone = cfg.get("caller_phone") or state.get("caller_phone", "")
    try:
        return save_preferred_slot(call_id, phone, slot, text)
    except Exception:
        logger.exception("❌ [Booking] save_preferred_slot failed for %s", call_id)
        return False


def answer_question(state: CallState) -> str:
    """Answer one clinic question from the approved information, in 1-2 short sentences."""
    from app.agent.llm import get_llm

    try:
        recent = trim_messages(
            state["messages"], strategy="last", token_counter=len, max_tokens=12, start_on="human"
        )
        system = build_system_prompt(state) + (
            "\n\nRIGHT NOW: answer only the caller's latest question in one or two short sentences, "
            "using the approved clinic information. Do not ask any question. Do not offer an "
            "appointment or a transfer. If the answer is not in the approved information, say the "
            "clinic team will help with that when they call."
        )
        reply = get_llm().invoke([SystemMessage(system)] + recent)
        text = (getattr(reply, "content", "") or "").strip()
        return text or pick_reply("not_understood")
    except Exception:
        logger.exception("❌ [Booking] clinic answer failed")
        return pick_reply("not_understood")


# ==================================================
# NODE: intake finished -> make the booking offer
# ==================================================

def offer_booking(state: CallState):
    return _out(
        pick_reply("offer_booking"),
        phase=flow.PHASE_OFFER,
        action=None,
        offer_attempts=0,
        silences=0,
    )


# ==================================================
# NODE: the caller's answer in each booking phase
# ==================================================

def booking_flow(state: CallState, config=None):
    phase = state.get("phase")
    text = _last_user_text(state).strip()
    try:
        if phase == flow.PHASE_OFFER:
            return _on_offer(state, config, text)
        if phase == flow.PHASE_SLOT:
            return _on_slot(state, config, text)
        if phase == flow.PHASE_ANYTHING_ELSE:
            return _on_anything_else(state, config, text)
    except Exception:
        logger.exception("❌ [Booking] flow failed in phase %s", phase)
        return _out(pick_reply("close_team_will_call"), phase=flow.PHASE_DONE, action="end")
    return _out(pick_reply("goodbye_final"), phase=flow.PHASE_DONE, action="end")


def _close_flexible(state: CallState, config, text_for_team: str = "flexible") -> dict:
    """Default close: save "flexible" and end with one merged line."""
    _save_slot(state, config, flow.Slot(flexible=True), text_for_team)
    return _out(pick_reply("close_team_will_call"), phase=flow.PHASE_DONE, action="end", silences=0)


def _on_offer(state: CallState, config, text: str) -> dict:
    """The caller answers the booking offer: yes goes to the transfer, call-me-later goes to the slot
    question, no booking goes to "anything else?", a clinic question is answered and the offer re-asked."""
    silences = state.get("silences", 0)
    if text == SILENCE:
        if silences + 1 >= flow.MAX_SILENCES:
            return _close_flexible(state, config)
        return _out(pick_reply("offer_unclear"), silences=silences + 1)

    choice = classify_choice(text)

    if choice == "transfer":
        if flow.is_open_now():
            return _out(
                pick_reply("receptionist_connecting"),
                phase=flow.PHASE_TRANSFERRING, action="receptionist", silences=0,
            )
        return _out(
            pick_reply("clinic_closed_now"),
            phase=flow.PHASE_SLOT, slot_attempts=0, silences=0,
        )

    if choice == "callback":
        return _out(pick_reply("ask_slot"), phase=flow.PHASE_SLOT, slot_attempts=0, silences=0)

    if choice == "no_booking":
        return _out(
            pick_reply("no_booking"), phase=flow.PHASE_ANYTHING_ELSE, anything_rounds=0, silences=0
        )

    if choice == "question":
        answer = answer_question(state)
        return _out(f"{answer} {pick_reply('reask_offer')}", silences=0)

    attempts = state.get("offer_attempts", 0) + 1
    if attempts >= flow.MAX_OFFER_ATTEMPTS:
        # Still unclear: the safest default is the callback path.
        return _out(pick_reply("ask_slot"), phase=flow.PHASE_SLOT, slot_attempts=0, silences=0)
    return _out(pick_reply("offer_unclear"), offer_attempts=attempts, silences=0)


def _on_slot(state: CallState, config, text: str) -> dict:
    """The caller gives (or fails to give) the day and time they would like. Validates against the clinic
    hours, never confirms availability, and after two failed tries saves a flexible request."""
    lang = current_language()
    attempts = state.get("slot_attempts", 0)
    silences = state.get("silences", 0)

    if text == SILENCE:
        if silences + 1 >= flow.MAX_SILENCES:
            return _finalize_slot(state, config, _slot_from_state(state), lang)
        return _out(pick_reply("ask_slot_again"), silences=silences + 1)

    result = extract_slot(text)

    if result.clinic_question and not (result.slot.has_day or result.slot.has_time):
        # A question about the clinic is answered; it does not use up a day/time attempt.
        return _out(f"{answer_question(state)} {pick_reply('ask_slot')}", silences=0)

    if result.refused or (result.flexible and not result.slot.has_day):
        return _finalize_slot(state, config, flow.Slot(flexible=True), lang)

    # Merge with what an earlier answer already gave (the day first, then the time).
    previous = _slot_from_state(state)
    slot = flow.Slot(
        day=result.slot.day or previous.day,
        hhmm=result.slot.hhmm or previous.hhmm,
        bucket=result.slot.bucket or previous.bucket,
        flexible=result.slot.flexible,
    )
    if slot.hhmm:
        slot.bucket = None

    problem = flow.validate_slot(slot)
    if problem:
        attempts += 1
        if attempts >= flow.MAX_SLOT_ATTEMPTS:
            return _finalize_slot(state, config, flow.Slot(flexible=True), lang)
        # Forget only the part that was wrong, keep the rest.
        if problem == "outside_hours":
            slot.hhmm, slot.bucket = None, None
        else:
            slot.day = None
        key = {"closed_day": "slot_closed_day", "outside_hours": "slot_outside_hours", "past": "slot_past"}[problem]
        return _out(pick_reply(key), slot_attempts=attempts, silences=0, **_slot_updates(slot))

    if slot.has_day and slot.has_time:
        return _finalize_slot(state, config, slot, lang)

    if not slot.has_day and not slot.has_time:
        attempts += 1
        if attempts >= flow.MAX_SLOT_ATTEMPTS:
            return _finalize_slot(state, config, flow.Slot(flexible=True), lang)
        key = "slot_availability" if result.asks_availability else "ask_slot_again"
        return _out(pick_reply(key), slot_attempts=attempts, silences=0)

    # Only one of day / time so far: ask for the other one.
    attempts += 1
    if attempts >= flow.MAX_SLOT_ATTEMPTS:
        return _finalize_slot(state, config, slot, lang)
    key = "ask_slot_time" if slot.has_day else "ask_slot_day"
    return _out(pick_reply(key), slot_attempts=attempts, silences=0, **_slot_updates(slot))


def _finalize_slot(state: CallState, config, slot: flow.Slot, lang: str) -> dict:
    """Save the request and move on to 'anything else'."""
    phrase = flow.describe_slot(slot, lang)
    flexible = not phrase
    saved = _save_slot(state, config, slot, "flexible" if flexible else phrase)
    if not saved:
        logger.error("❌ [Booking] slot request could not be saved")
    reply = (
        pick_reply("slot_saved_flexible")
        if flexible
        else pick_reply("slot_saved").format(slot=phrase)
    )
    return _out(
        reply,
        phase=flow.PHASE_ANYTHING_ELSE, anything_rounds=0, silences=0,
        slot_attempts=0, **_slot_updates(slot),
    )


def _on_anything_else(state: CallState, config, text: str) -> dict:
    """The caller answers "anything else?": answer a clinic question, update the saved slot if they
    add one, or say the final goodbye and end the call."""
    silences = state.get("silences", 0)
    rounds = state.get("anything_rounds", 0)

    if text == SILENCE:
        if silences + 1 >= flow.MAX_SILENCES:
            return _out(pick_reply("goodbye_final"), phase=flow.PHASE_DONE, action="end")
        return _out(pick_reply("ask_anything_else"), silences=silences + 1)

    if is_no(text):
        return _out(pick_reply("goodbye_final"), phase=flow.PHASE_DONE, action="end")

    # The caller adds or corrects the day/time ("6 in the evening"): update the saved request.
    said = extract_slot(text, use_llm=False)
    if said.slot.has_day or said.slot.has_time:
        out = _on_slot({**state, "slot_attempts": 0}, config, text)
        out.setdefault("phase", flow.PHASE_SLOT)
        return out

    answer = answer_question(state)
    rounds += 1
    if rounds >= flow.MAX_ANYTHING_ROUNDS:
        return _out(f"{answer} {pick_reply('goodbye_final')}", phase=flow.PHASE_DONE, action="end")
    return _out(
        f"{answer} {pick_reply('ask_anything_else')}", anything_rounds=rounds, silences=0
    )


# ==================================================
# RESUME: the assistant did not pick up, the bot is back on the line
# ==================================================

def resume_after_failed_transfer() -> dict:
    """State changes + line to speak when the transfer failed and the bot rejoins the call."""
    return {
        "phase": flow.PHASE_SLOT,
        "action": None,
        "slot_attempts": 0,
        "silences": 0,
        "line": pick_reply("assistant_unavailable"),
    }
