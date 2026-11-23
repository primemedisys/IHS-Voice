"""Helpers for inbound call completion and receptionist transfer."""

import logging
from sqlalchemy import select

from app.database import get_session
from app.models.callback import Callback
from app.models.inbound_inquiry import InboundInquiry
from app.services.inbound.inquiries import upsert_inquiry

logger = logging.getLogger(__name__)

# ==================================================
# CALL SNAPSHOT
# ==================================================

def save_snapshot(
    call_id: str,
    phone: str,
    state: dict,
    current_text: str = "",
    caller_turns: list[str] | None = None,
    **fields,
) -> bool:
    """Save known information. The summary contains caller statements."""
    if not call_id:
        return False

    if caller_turns is not None:
        lines = list(caller_turns)
    else:
        lines = []
        for message in state.get("messages", []):
            if getattr(message, "type", "") == "human" and isinstance(message.content, str):
                lines.append(message.content)
        if current_text and (not lines or lines[-1] != current_text):
            lines.append(current_text)

    caller_type = state.get("caller_type")
    verified = bool(state.get("is_verified"))

    info = {
        "patient_type": caller_type,
        "patient_id": state.get("patient_id") if verified else None,
        "name": state.get("given_name") if caller_type == "new" else (
            state.get("patient_name") if verified else None
        ),
        "date_of_birth": state.get("given_dob") if caller_type == "new" else None,
        "identity_status": state.get("identity_status"),
        "condition": state.get("condition"),
        "symptoms": state.get("symptoms"),
        "started_when": state.get("started_when"),
        "severity": state.get("severity"),
        "prior_treatment": state.get("prior_treatment"),
        "wants_appointment": state.get("wants_appointment"),
        "wants_receptionist": state.get("wants_receptionist"),
    }

    if lines:
        info["summary"] = "Caller statements:\n" + "\n".join(lines)

    info.update(fields)
    return upsert_inquiry(call_id, phone, **info)


# ==================================================
# TRANSFER TO THE RECEPTIONIST
# ==================================================

def mark_transfer_not_started(call_id: str) -> bool:
    """Keep the request for follow-up if dialing did not start."""
    return upsert_inquiry(
        call_id,
        transfer_status="not_started",
        outcome="needs_followup",
    )


def finish_receptionist_attempt(call_id: str, answered: bool) -> bool:
    """Save the dial result and ensure an unanswered call has a callback."""
    if not call_id:
        return False

    try:
        with get_session() as db:
            inquiry = db.scalar(
                select(InboundInquiry)
                .where(InboundInquiry.call_id == call_id)
                .with_for_update()
            )

            if not inquiry:
                logger.error("No inquiry available for receptionist result: %s", call_id)
                return False

            # Repeated provider callbacks must not create duplicate callbacks.
            if inquiry.transfer_status in ("connected", "failed"):
                return True

            if answered:
                inquiry.transfer_status = "connected"
                inquiry.outcome = "transferred"
                # The assistant has the caller: the team does not need to call back.
                for row in db.scalars(
                    select(Callback).where(
                        Callback.call_id == call_id,
                        Callback.status == "pending",
                    )
                ):
                    row.status = "done"
            else:
                inquiry.transfer_status = "failed"
                inquiry.outcome = "needs_followup"

                pending = db.scalar(
                    select(Callback).where(
                        Callback.call_id == call_id,
                        Callback.status == "pending",
                    ).limit(1)
                )

                if not pending:
                    db.add(Callback(
                        call_id=call_id,
                        phone=inquiry.phone,
                        name=inquiry.name,
                        date_of_birth=inquiry.date_of_birth,
                        issue="Receptionist unavailable; follow up on the inbound inquiry.",
                        urgency="routine",
                        status="pending",
                        reason="receptionist_unavailable",
                    ))

        return True
    except Exception:
        logger.exception("Could not save receptionist result for call %s", call_id)
        return False
    
# ==================================================
# CALLBACK RECORD
# ==================================================

def ensure_pending_callback(call_id: str, urgency: str = "routine") -> bool:
    """Create or refresh the pending callback from the saved inquiry."""
    if not call_id:
        return False

    try:
        with get_session() as db:
            inquiry = db.scalar(
                select(InboundInquiry)
                .where(InboundInquiry.call_id == call_id)
                .with_for_update()
            )
            if not inquiry:
                logger.error("No inquiry available for callback: %s", call_id)
                return False

            callback = db.scalar(
                select(Callback).where(
                    Callback.call_id == call_id,
                    Callback.status == "pending",
                ).limit(1)
            )

            issue = (
                inquiry.condition
                or inquiry.symptoms
                or "Inbound call: follow up on the caller's inquiry."
            )

            if callback:
                callback.phone = inquiry.phone
                if inquiry.name:
                    callback.name = inquiry.name
                if inquiry.date_of_birth:
                    callback.date_of_birth = inquiry.date_of_birth
                callback.issue = issue
                if urgency == "urgent" and callback.urgency == "routine":
                    callback.urgency = "urgent"
            else:
                db.add(Callback(
                    call_id=call_id,
                    phone=inquiry.phone,
                    name=inquiry.name,
                    date_of_birth=inquiry.date_of_birth,
                    issue=issue,
                    urgency=urgency,
                    status="pending",
                    reason="inbound_callback",
                ))

        return True
    except Exception:
        logger.exception("Could not save callback for call %s", call_id)
        return False