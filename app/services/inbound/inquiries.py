"""
app/services/inbound/inquiries.py
───────────────────────────────
Crash-safe persistence for inbound calls.

One row per call, keyed on `call_id`:
    upsert_inquiry(call_id, phone, name="Rahul")        -> INSERT the row
    upsert_inquiry(call_id, symptoms="knee pain")       -> UPDATE the same row

Why update-first instead of one INSERT ... ON CONFLICT: Postgres checks NOT NULL
on the proposed insert row *before* resolving the conflict, so an update-only
call (no phone) would fail. Updating first avoids that entirely.

A DB failure here is logged and swallowed: a broken write must never drop a
live phone call. Partial data is expected and fine.
"""

import logging
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import get_session
from app.models.inbound_inquiry import InboundInquiry

logger = logging.getLogger(__name__)

# Only these fields may be written. Anything else a caller passes is ignored,
# so a typo can never touch `id`, `created_at`, `call_id`, ...
UPDATABLE: frozenset[str] = frozenset({
    "phone", "patient_id", "patient_type", "name", "date_of_birth", "identity_status",
    "condition", "symptoms", "started_when", "severity", "prior_treatment",
    "wants_appointment", "wants_receptionist",
    "preferred_date", "preferred_time", "preferred_slot_text",
    "transfer_status", "outcome", "summary",
    "recording_id", "recording_url", "call_ended_at",
})


def upsert_inquiry(call_id: str, phone: str = "", **fields: Any) -> bool:
    """Create or update the inquiry row for this call. Returns True if saved.

    `None` values are ignored (so a blank turn never erases what we already have);
    `False` is stored (a "no" answer matters just as much as a "yes").
    """
    if not call_id:
        logger.error("❌ [Inquiry] upsert called without a call_id")
        return False

    payload = {key: value for key, value in fields.items()
               if key in UPDATABLE and value is not None}
    if phone:
        payload["phone"] = phone

    try:
        with get_session() as db:
            # 1) Try to update the row that should already exist.
            changed = db.execute(
                update(InboundInquiry)
                .where(InboundInquiry.call_id == call_id)
                .values(**payload, updated_at=func.now())
            ).rowcount

            # 2) First write of this call -> insert it (phone is required here).
            if not changed:
                if not payload.get("phone"):
                    logger.error("❌ [Inquiry] Cannot create inquiry without a phone: %s", call_id)
                    return False
                stmt = pg_insert(InboundInquiry).values(call_id=call_id, **payload)
                updates = {key: getattr(stmt.excluded, key) for key in payload}
                updates["updated_at"] = func.now()
                db.execute(stmt.on_conflict_do_update(index_elements=["call_id"], set_=updates))
        return True
    except Exception:
        logger.exception("❌ [Inquiry] Could not save inquiry for call %s", call_id)
        return False


def get_inquiry(call_id: str) -> InboundInquiry | None:
    """Read the row back (used by tests and by the end-of-call summary)."""
    try:
        with get_session() as db:
            return db.scalar(select(InboundInquiry).where(InboundInquiry.call_id == call_id))
    except Exception:
        logger.exception("❌ [Inquiry] Could not read inquiry for call %s", call_id)
        return None