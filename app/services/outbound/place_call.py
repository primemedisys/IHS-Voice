"""
app/services/outbound/place_call.py
────────────────────────────────
Places one reminder call through Plivo. Plivo rings the patient; when they answer,
Plivo asks our /voice/plivo/outbound/answer URL what to do (open the audio stream).
"""

import asyncio
import logging

from app.config import settings
from app.services.outbound import appointments as appointment_service
from app.telephony.plivo.plivo_adapter import PlivoAdapter

logger = logging.getLogger("voice.outbound")

adapter = PlivoAdapter()


async def place_call(appointment: dict) -> bool:
    """Start the call for an appointment that was already marked 'calling'. Returns False if Plivo refused it."""
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    appointment_id = appointment["id"]
    try:
        request_uuid = await adapter.make_call(
            to=appointment["phone"],
            answer_url=f"{base}/voice/plivo/outbound/answer?appt={appointment_id}",
            hangup_url=f"{base}/voice/plivo/outbound/hangup?appt={appointment_id}",
        )
        await asyncio.to_thread(appointment_service.set_call_uuid, appointment_id, request_uuid)
        logger.info("📤 [Outbound] Calling appointment %s", appointment_id)
        return True
    except Exception:
        logger.exception("❌ [Outbound] Could not place the call for appointment %s", appointment_id)
        await asyncio.to_thread(appointment_service.finish_attempt, appointment_id, False)
        return False