"""
app/routes/v1/outbound/plivo.py
─────────────────────────────────────
Webhooks for OUTBOUND reminder calls (Plivo calls these, we never expose them to patients).

  POST /voice/plivo/outbound/answer?appt=ID     patient picked up: open the audio stream in reminder mode
  POST /voice/plivo/outbound/hangup?appt=ID     the call ended: save the result or schedule a retry
  POST /voice/plivo/outbound/recording?appt=ID  the recording is ready: save its URL
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request, Response

from app.routes.v1.inbound.plivo import _base, _verified_form, _xml, adapter
from app.services.outbound import appointments as appointment_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice/plivo/outbound", tags=["outbound"])


@router.post("/answer")
async def outbound_answer(request: Request, appt: int) -> Response:
    """The patient answered. Reply with the same <Stream> XML inbound calls get, plus 'reminder' details."""
    try:
        form = await _verified_form(request)
        call_uuid = form.get("CallUUID", "")
        brief = await asyncio.to_thread(appointment_service.get_brief, appt)
        if not brief:
            logger.error("❌ [Outbound] Appointment %s not found, hanging up", appt)
            return _xml(adapter.hangup_xml())

        await asyncio.to_thread(appointment_service.set_call_uuid, appt, call_uuid)
        # ws_url = _base().replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/voice/plivo/stream"
        ws_url = (_base().replace("https://", "wss://", 1).replace("http://", "ws://", 1) + f"/voice/plivo/stream?mode=reminder&apptId={appt}")
        status_url = f"{_base()}/voice/plivo/stream-status"
        logger.info("📞 [Outbound] Patient answered (appointment %s, call %s)", appt, call_uuid)
        return _xml(adapter.stream_xml(
            ws_url, status_url, brief["phone"], extra={"mode": "reminder", "apptId": appt}))
    except HTTPException:
        raise
    except Exception:
        logger.exception("❌ [Outbound] Answer handler failed")
        return _xml(adapter.hangup_xml())


@router.post("/hangup")
async def outbound_hangup(request: Request, appt: int) -> dict:
    """Plivo reports the end of the call. A decision saved during the call is never overwritten."""
    form = await _verified_form(request)
    status = (form.get("CallStatus") or "").lower()
    try:
        duration = int(float(form.get("Duration") or form.get("BillDuration") or 0))
    except ValueError:
        duration = 0
    answered = status == "completed" and duration > 0
    logger.info("📴 [Outbound] Appointment %s ended: status=%s cause=%s duration=%ss answered=%s",
                appt, status or "-", form.get("HangupCause", "-"), duration, answered)
    await asyncio.to_thread(appointment_service.finish_attempt, appt, answered)
    return {"status": "success"}


@router.post("/recording")
async def outbound_recording(request: Request, appt: int) -> dict:
    """Plivo finished the recording and tells us where it is."""
    form = await _verified_form(request)
    url = next((form[key] for key in
                ("RecordUrl", "record_url", "RecordingUrl", "recording_url", "recording_url_mp3")
                if form.get(key)), "")
    duration = form.get("RecordingDuration") or form.get("recording_duration") or "?"
    logger.info("🎙️ [Outbound] Recording ready for appointment %s (%ss)", appt, duration)
    if url:
        await asyncio.to_thread(appointment_service.set_recording, appt, url)
    else:
        logger.warning("🎙️ [Outbound] No recording URL in the callback, fields were: %s", sorted(form))
    return {"status": "success"}