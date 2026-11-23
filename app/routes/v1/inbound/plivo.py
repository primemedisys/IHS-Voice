"""
app/routes/v1/inbound/plivo.py
────────────────────────────
Plivo-facing endpoints for INBOUND calls (the WebSocket is shared with outbound reminder calls).

  POST /voice/plivo/answer               Answer URL: returns the <Stream> XML
  WS   /voice/plivo/stream               the live audio WebSocket (one CallSession per call)
  POST /voice/plivo/stream-status        stream started / stopped / failed (log only)
  POST /voice/plivo/receptionist         XML that rings the receptionist when the caller asks for the team
  POST /voice/plivo/receptionist-status  what happened when ringing: connected (bot stays off the line),
                                         or no answer (the bot comes back and asks for the day and time)
  POST /voice/plivo/transfer             XML that rings the doctor (used after an emergency)
  POST /voice/plivo/transfer-status      what happened when ringing: connected, try backup, or give up
"""

import asyncio
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket

from app.agent.shared.loader import pick_reply
from app.config import settings
from app.services.shared import patients as patient_service
from app.services.shared.notifications import notify_doctor
from app.telephony.plivo.plivo_adapter import PlivoAdapter
from app.voice.call_wrapup import finish_receptionist_attempt
from app.voice.session import SUPPORTED_LANGS, CallSession, register_caller

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice/plivo", tags=["voice"])
adapter = PlivoAdapter()


# ==================================================
# HELPERS
# ==================================================


def _base() -> str:
    """Public base URL without a trailing slash."""
    return settings.PUBLIC_BASE_URL.rstrip("/")


def _xml(body: str) -> Response:
    """Wrap XML text in an HTTP response Plivo accepts."""
    return Response(content=body, media_type="text/xml")


def _e164(raw: str) -> str:
    """Plivo sends numbers like 919999900001 (no plus). Convert to +919999900001."""
    raw = (raw or "").strip()
    if raw and not raw.startswith("+"):
        raw = "+" + raw
    return patient_service.normalize_phone(raw)


def _public_url(request: Request) -> str:
    """The URL exactly as Plivo called it (needed to verify the signature behind ngrok/proxies)."""
    url = _base() + request.url.path
    return f"{url}?{request.url.query}" if request.url.query else url


async def _verified_form(request: Request) -> dict:
    """Read Plivo's form data and, if enabled, reject requests with a bad signature (403)."""
    form = dict(await request.form())
    if settings.PLIVO_VALIDATE_SIGNATURE:
        ok = adapter.validate_signature(
            "POST",
            _public_url(request),
            request.headers.get("x-plivo-signature-v3-nonce", ""),
            request.headers.get("x-plivo-signature-v3", ""),
            form,
        )
        if not ok:
            logger.warning("❌ [Voice] Rejected a request with an invalid Plivo signature")
            raise HTTPException(status_code=403, detail="❌ Invalid Plivo signature")
    return form


def _ws_signature_ok(ws: WebSocket) -> bool:
    """Check the WebSocket upgrade signature (we try both https:// and wss:// forms of the URL)."""
    signature = ws.headers.get("x-plivo-signature-v3", "")
    nonce = ws.headers.get("x-plivo-signature-v3-nonce", "")
    host = ws.headers.get("host", "")
    return any(
        adapter.validate_signature("GET", f"{scheme}://{host}{ws.url.path}", nonce, signature)
        for scheme in ("https", "wss")
    )


def _seconds(value) -> int | None:
    """Plivo duration fields as whole seconds, or None when missing or not a number."""
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _dial_response(number: str, attempt: int) -> Response:
    """XML that rings `number` and reports back to /transfer-status."""
    action = f"{_base()}/voice/plivo/transfer-status?attempt={attempt}"
    return _xml(adapter.dial_xml(number, action, settings.TRANSFER_RING_SECONDS))


def _failed_response() -> Response:
    """Tell the caller the doctor is busy (callback is already saved) and hang up."""
    return _xml(adapter.speak_hangup_xml(pick_reply("transfer_failed")))


def _resume_response(form: dict, lang: str) -> Response:
    """The assistant did not pick up: bring the bot back onto the call.

    Plivo opens a new audio stream tagged mode=resume. The session loads the saved call state and
    says "the assistant is not available, which day and time?" (see CallSession._start_resume_call).
    If the stream cannot open, keepCallAlive plus the trailing <Wait> let the call end normally.
    """
    language = lang if lang in SUPPORTED_LANGS else "en"
    # The resume tags travel in the stream URL: Plivo's extraHeaders did not reach the session
    # in testing, while URL query parameters do (that is how reminder calls are tagged too).
    ws_url = (
        _base().replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        + "/voice/plivo/stream?"
        + urlencode({"mode": "resume", "lang": language})
    )
    status_url = f"{_base()}/voice/plivo/stream-status"
    caller = _e164(str(form.get("From", "") or ""))
    call_uuid = str(form.get("CallUUID") or form.get("DialALegUUID") or "")
    if call_uuid and caller:
        register_caller(call_uuid, caller)      # the Answer webhook does not run again for a resume
    logger.info("🔁 [Voice] Receptionist did not answer, resuming the bot on the call")
    return _xml(adapter.stream_xml(ws_url, status_url, caller, extra={"mode": "resume", "lang": language}))


# ==================================================
# ENDPOINTS
# ==================================================


@router.post("/answer")
async def answer(request: Request) -> Response:
    """Plivo calls this when someone dials the clinic number. We reply with XML that opens the audio stream."""
    try:
        form = await _verified_form(request)
        call_uuid = form.get("CallUUID", "")
        caller = _e164(form.get("From", ""))
        register_caller(call_uuid, caller)

        ws_url = _base().replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/voice/plivo/stream"
        status_url = f"{_base()}/voice/plivo/stream-status"
        logger.info("📞 [Voice] Incoming call %s", call_uuid)
        return _xml(adapter.stream_xml(ws_url, status_url, caller))
    except HTTPException:
        raise
    except Exception:
        logger.exception("❌ [Voice] Answer handler failed")
        return _xml(adapter.speak_hangup_xml(pick_reply("error")))


@router.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    """Plivo connects here and exchanges live audio. The whole call is handled by one CallSession."""
    if settings.PLIVO_VALIDATE_SIGNATURE and not _ws_signature_ok(ws):
        logger.warning("❌ [Voice] WebSocket rejected: invalid signature")
        await ws.close(code=1008)
        return
    await ws.accept()
    session = CallSession(ws, ws.app.state.graph, getattr(ws.app.state, "reminder_graph", None), adapter)
    session.query_params = dict(ws.query_params)   # reminder tags arrive in the URL
    await session.run()


@router.post("/stream-status")
async def stream_status(request: Request) -> dict:
    """Plivo reports stream events (started, stopped, failed). We only log them."""
    form = await _verified_form(request)
    logger.info("📡 [Voice] Stream %s (%s)", form.get("Event"), form.get("StatusReason", "-"))
    return {"status": "success", "message": "✅ Received"}


@router.post("/receptionist")
async def receptionist(request: Request, lang: str = "en") -> Response:
    """Plivo fetches separate dial instructions for the receptionist."""
    call_id = ""
    form: dict = {}

    try:
        form = await _verified_form(request)
        call_id = form.get("CallUUID", "")

        if not settings.RECEPTIONIST_PHONE:
            await asyncio.to_thread(finish_receptionist_attempt, call_id, False)
            return _resume_response(form, lang)

        language = lang if lang in SUPPORTED_LANGS else "en"
        action = f"{_base()}/voice/plivo/receptionist-status?lang={language}"

        return _xml(
            adapter.dial_xml(
                settings.RECEPTIONIST_PHONE,
                action,
                settings.TRANSFER_RING_SECONDS,
            )
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Receptionist dial instructions failed")
        await asyncio.to_thread(finish_receptionist_attempt, call_id, False)
        return _resume_response(form, lang)


@router.post("/receptionist-status")
async def receptionist_status(request: Request, lang: str = "en") -> Response:
    """Handle the final Dial result separately from emergency transfer."""
    form = await _verified_form(request)

    call_id = form.get("DialALegUUID") or form.get("CallUUID") or ""
    status = str(form.get("DialStatus") or "").strip().lower()
    answered = status == "completed" and bool(form.get("DialBLegUUID"))

    # Someone picked up and dropped straight away (declined, wrong key, voicemail hang-up) is not a
    # conversation: treat it as unanswered when Plivo tells us how long the assistant leg lasted.
    talk_s = _seconds(form.get("DialBLegDuration") or form.get("DialBLegBillDuration"))
    if answered and talk_s is not None and talk_s < settings.RECEPTIONIST_MIN_TALK_S:
        logger.info("☎️ [Voice] Assistant leg lasted only %ss, treating as not answered", talk_s)
        answered = False

    logger.info(
        "Receptionist dial status=%s answered=%s talk_s=%s call=%s aleg=%s cause=%s",
        status or "unknown",
        answered,
        talk_s,
        form.get("CallUUID"),
        form.get("DialALegUUID"),
        form.get("DialHangupCause", "-"),
    )

    if answered:
        # The assistant has the caller now; the bot stays off the line.
        await asyncio.to_thread(finish_receptionist_attempt, call_id, True)
        return _xml(adapter.hangup_xml())

    await asyncio.to_thread(finish_receptionist_attempt, call_id, False)
    return _resume_response(form, lang)


@router.post("/transfer")
async def transfer(request: Request, attempt: int = 1) -> Response:
    """After an emergency the call is moved here. Attempt 1 rings the doctor, attempt 2 rings the backup."""
    try:
        await _verified_form(request)
        number = settings.ESCALATION_PHONE if attempt == 1 else settings.BACKUP_PHONE
        if not number:
            logger.error("❌ [Voice] No phone number configured for transfer attempt %s", attempt)
            return _failed_response()
        return _dial_response(number, attempt)
    except HTTPException:
        raise
    except Exception:
        logger.exception("❌ [Voice] Transfer handler failed")
        return _failed_response()


@router.post("/transfer-status")
async def transfer_status(request: Request, attempt: int = 1) -> Response:
    """Plivo reports how ringing went. Answered: finish. No answer: try the backup, then the 'doctor is busy' line."""
    try:
        form = await _verified_form(request)
        answered = bool(form.get("DialBLegUUID"))
        logger.info("📞 [Voice] Transfer attempt %s: status=%s answered=%s",
                    attempt, form.get("DialStatus"), answered)

        if answered:
            return _xml(adapter.hangup_xml())
        if attempt == 1 and settings.BACKUP_PHONE:
            return _dial_response(settings.BACKUP_PHONE, 2)

        notify_doctor(f"Could not reach you for an emergency call from {form.get('From', 'unknown')}. "
                      "A callback is saved, please call back now.", level="emergency")
        return _failed_response()
    except HTTPException:
        raise
    except Exception:
        logger.exception("❌ [Voice] Transfer status handler failed")
        return _failed_response()