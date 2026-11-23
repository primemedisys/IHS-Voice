"""
app/agent/intake/tools.py
──────────────────
Actions the LLM can take. The docstring tells the LLM when to use each tool.
`config` is injected automatically and is NOT shown to the LLM
(it carries the call_id and the caller's phone).

One tool:
    request_callback -> SAVE what the caller said so far (safe to call many times).
The end of the call (booking offer, transfer, slot, goodbye) is NOT an LLM job:
see app/agent/booking/nodes.py.
"""

import logging
from typing import Literal

from langchain_core.runnables import RunnableConfig # type: ignore
from langchain_core.tools import tool # type: ignore
from sqlalchemy import select

from app.database import get_session
from app.models.callback import Callback
from app.services.inbound.inquiries import get_inquiry, upsert_inquiry

logger = logging.getLogger(__name__)

# Everything the clinic needs before the conversation may end.
INTAKE_FIELDS = ("condition", "started_when", "severity", "prior_treatment")


def _missing_intake(call_id: str) -> list[str]:
    """Which intake details are still unknown (read back from the saved row)."""
    inquiry = get_inquiry(call_id)
    if inquiry is None:
        return list(INTAKE_FIELDS)
    return [
        name
        for name in INTAKE_FIELDS
        if not str(getattr(inquiry, name, "") or "").strip()
    ]


def _callback_exists(call_id: str) -> bool:
    """One callback row per call, no matter how often the tools run."""
    try:
        with get_session() as db:
            row = db.execute(
                select(Callback.id).where(Callback.call_id == call_id).limit(1)
            ).first()
            return row is not None
    except Exception:
        logger.exception("❌ [Tools] Could not check the callback row for %s", call_id)
        return False

def _ensure_callback(call_id: str, phone: str, urgency: str = "routine") -> bool:
    """Create or refresh the callback for this inquiry."""
    from app.voice.call_wrapup import ensure_pending_callback

    return ensure_pending_callback(call_id, urgency)

@tool
def request_callback(
    condition: str,
    urgency: Literal["routine", "urgent"],
    config: RunnableConfig,
    started_when: str = "",
    severity: str = "",
    prior_treatment: str = "",
    wants_appointment: bool | None = None,
) -> str:
    """Save what the caller has told you about their problem. Call it again whenever
    the caller adds a new detail: every call updates the same record for this call,
    so nothing is lost if the line drops. This tool does NOT end the call.

    - condition: the problem in the caller's own words.
    - started_when: when it started, as the caller said it.
    - severity: how bad it is, as the caller said it.
    - prior_treatment: medicine, plaster, scan or another doctor already tried.
    - urgency: "urgent" only if the problem is severe or getting worse.
    - wants_appointment: true if the caller wants an appointment or a clinic visit.
    """
    cfg = config["configurable"]
    call_id = cfg["thread_id"]
    phone = cfg.get("caller_phone", "unknown")

    saved = upsert_inquiry(
        call_id,
        phone,
        condition=(condition or "").strip() or None,
        started_when=(started_when or "").strip() or None,
        severity=(severity or "").strip() or None,
        prior_treatment=(prior_treatment or "").strip() or None,
        wants_appointment=wants_appointment,
    )
    if not saved:
        return "The inquiry could NOT be saved."
    
    if not _ensure_callback(call_id, phone, urgency):
        return "The inquiry was saved, but the callback could NOT be saved."

    missing = _missing_intake(call_id)
    if missing:
        return "Saved. Intake still missing: " + ", ".join(missing) + "."
    return "Saved. Intake complete."

TOOLS = [request_callback]