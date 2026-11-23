"""
app/agent/state.py

Memory for one inbound call, saved by LangGraph under the call ID.
"""

from langgraph.graph import MessagesState  # type: ignore


class CallState(MessagesState):
    caller_phone: str
    caller_type: str                     # existing / new
    patient_id: int | None
    is_verified: bool
    verify_attempts: int
    given_name: str | None
    given_dob: str | None
    urgency: str
    turn_level: str
    action: str | None

    # Identity and inquiry information
    identity_status: str | None
    identity_stage: str | None           # current name/DOB question
    name_confirmed: bool | None
    patient_name: str | None
    condition: str | None
    symptoms: str | None
    started_when: str | None
    severity: str | None
    prior_treatment: str | None
    wants_appointment: bool | None
    wants_receptionist: bool | None
    transfer_target: str | None

    # End-of-call booking flow (see app/agent/booking/flow.py)
    phase: str | None                    # intake / offer / transferring / slot / anything_else / done
    intake_turns: int
    offer_attempts: int
    slot_attempts: int
    anything_rounds: int
    silences: int
    slot_date: str | None                # YYYY-MM-DD the caller asked for
    slot_time: str | None                # HH:MM
    slot_bucket: str | None              # morning / afternoon / evening