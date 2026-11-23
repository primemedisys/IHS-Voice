"""
app/services/inbound/booking_request.py
───────────────────────────────
Saves the day/time the caller WANTS for an appointment, as a request.

It never touches the `appointments` table: that table needs a confirmed date and time
and drives reminders. A request only goes on the inquiry row and the pending callback,
so the clinic team can read it and call the caller to confirm.
"""

import logging

from sqlalchemy import select

from app.agent.booking.flow import Slot
from app.database import get_session
from app.models.callback import Callback
from app.services.inbound.inquiries import upsert_inquiry
from app.voice.call_wrapup import ensure_pending_callback

logger = logging.getLogger(__name__)


def save_preferred_slot(call_id: str, phone: str, slot: Slot, text: str) -> bool:
    """Store the requested slot. `text` is the readable phrase for the team
    (or "flexible" when the caller gave no usable day/time). Returns True if saved."""
    if not call_id:
        return False

    saved = upsert_inquiry(
        call_id,
        phone,
        wants_appointment=True,
        preferred_date=slot.day,
        preferred_time=slot.hhmm or slot.bucket,
        preferred_slot_text=text,
    )
    if not saved:
        return False

    if not ensure_pending_callback(call_id):
        return False

    try:
        with get_session() as db:
            callback = db.scalar(
                select(Callback)
                .where(Callback.call_id == call_id, Callback.status == "pending")
                .order_by(Callback.id.desc())
                .limit(1)
            )
            if callback is None:
                return False
            callback.preferred_slot = text
            callback.reason = "appointment_request"
        return True
    except Exception:
        logger.exception("❌ [Booking] Could not attach the slot to the callback: %s", call_id)
        return False
