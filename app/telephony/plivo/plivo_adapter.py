"""
app/telephony/plivo_adapter.py
──────────────────────────────
Plivo implementation: builds the XML Plivo understands, translates Plivo's WebSocket
messages, transfers live calls through Plivo's REST API, and checks webhook signatures.
"""

import asyncio
import base64
import logging
from urllib.parse import unquote
from xml.sax.saxutils import escape, quoteattr

import plivo

from app.config import settings
from app.telephony.base import (
    STREAM_CONTENT_TYPE, 
    STREAM_ENCODING, 
    STREAM_RATE,
    StreamEvent, 
    TelephonyAdapter, 
    is_expected_format
)

logger = logging.getLogger("voice.plivo")


# ==================================================
# XML HELPERS
# ==================================================

def _wrap(*parts: str) -> str:
    """Put XML elements inside <Response>."""
    return '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(parts) + "</Response>"


def _parse_extra_headers(raw: str) -> dict[str, str]:
    """Turn 'key1=value1;key2=value2' into a dict (values are URL-decoded)."""
    result: dict[str, str] = {}
    for pair in (raw or "").split(";"):
        key, sep, value = pair.partition("=")
        if sep:
            result[key.strip()] = unquote(value.strip())
    return result


class PlivoAdapter(TelephonyAdapter):
    """Plivo Voice API: Answer URL XML, <Stream>, <Dial> and call transfer."""

    name = "plivo"

    def __init__(self) -> None:
        self._client: plivo.RestClient | None = None

    def _rest(self) -> plivo.RestClient:
        """Create the Plivo REST client on first use (so the app starts even without keys)."""
        if self._client is None:
            if not (settings.PLIVO_AUTH_ID and settings.PLIVO_AUTH_TOKEN):
                raise RuntimeError("PLIVO_AUTH_ID / PLIVO_AUTH_TOKEN are not set")
            self._client = plivo.RestClient(settings.PLIVO_AUTH_ID, settings.PLIVO_AUTH_TOKEN)
        return self._client

    # ==================================================
    # CALL INSTRUCTIONS (XML)
    # ==================================================

    def stream_xml(self, ws_url: str, status_url: str, caller_phone: str, extra: dict | None = None) -> str:
        """Open a two-way mu-law stream and pass the caller's number along as a custom header."""

        # Plivo only accepts [A-Za-z0-9] in extraHeaders: no '+', no '%', no quoting.
        headers = {"callerPhone": (caller_phone or "").lstrip("+"), **(extra or {})}
        header_str = ";".join(f"{k}={v}" for k, v in headers.items())
        attrs = (
            'bidirectional="true" keepCallAlive="true" '
            f"contentType={quoteattr(STREAM_CONTENT_TYPE)} "
            f'statusCallbackUrl={quoteattr(status_url)} statusCallbackMethod="POST" '
            f"extraHeaders={quoteattr(header_str)}"
        )
        return _wrap(f"<Stream {attrs}>{escape(ws_url)}</Stream>"'<Wait length="30"/>')

    def dial_xml(self, number: str, action_url: str, timeout: int) -> str:
        """Ring `number`. The clinic's Plivo number is shown as caller ID."""
        # caller_id = f" callerId={quoteattr(settings.PLIVO_NUMBER)}" if settings.PLIVO_NUMBER else ""
        caller_id = (f" callerId={quoteattr(settings.PLIVO_NUMBER.lstrip('+'))}"
        if settings.PLIVO_NUMBER else "")
        return _wrap(
            f'<Dial timeout="{int(timeout)}"{caller_id} action={quoteattr(action_url)} method="POST">'
            f"<Number>{escape(number)}</Number></Dial>"
        )

    def speak_hangup_xml(self, text: str) -> str:
        """Speak `text` with Plivo's built-in voice, then hang up."""
        voice = f" voice={quoteattr(settings.PLIVO_SPEAK_VOICE)}" if settings.PLIVO_SPEAK_VOICE else ""
        lang = f" language={quoteattr(settings.PLIVO_SPEAK_LANGUAGE)}" if settings.PLIVO_SPEAK_LANGUAGE else ""
        return _wrap(f"<Speak{voice}{lang}>{escape(text)}</Speak><Hangup/>")

    def speak_pause_speak_hangup_xml(self, first: str, pause: int, second: str) -> str:
        """Speak, wait, speak, then hang up (used after a failed transfer)."""
        voice = f" voice={quoteattr(settings.PLIVO_SPEAK_VOICE)}" if settings.PLIVO_SPEAK_VOICE else ""
        lang = f" language={quoteattr(settings.PLIVO_SPEAK_LANGUAGE)}" if settings.PLIVO_SPEAK_LANGUAGE else ""
        return _wrap(
            f"<Speak{voice}{lang}>{escape(first)}</Speak>"
            f'<Wait length="{int(pause)}"/>'
            f"<Speak{voice}{lang}>{escape(second)}</Speak>"
            "<Hangup/>"
        )
    
    def hangup_xml(self) -> str:
        """End the call."""
        return _wrap("<Hangup/>")

    # ==================================================
    # CALL CONTROL (REST)
    # ==================================================

    async def transfer_call(self, call_id: str, url: str) -> None:
        """Tell Plivo to fetch new XML for the caller's leg (the SDK is blocking, so run it in a thread)."""
        client = self._rest()
        await asyncio.to_thread(
            client.calls.transfer,
            call_uuid=call_id, legs="aleg", aleg_url=url, aleg_method="POST",
        )
        logger.info("✅ [Plivo] Call %s transferred", call_id)

    async def hangup_call(self, call_id: str) -> None:
        """Explicitly end a live Plivo call."""
        client = self._rest()
        await asyncio.to_thread(client.calls.delete, call_uuid=call_id)
        logger.info("Inbound call hangup requested: %s", call_id)
        
    # ==================================================
    # SECURITY
    # ==================================================

    def validate_signature(self, method: str, url: str, nonce: str,
                           signature: str, params: dict | None = None) -> bool:
        """Check Plivo's V3 signature. Any error counts as invalid."""
        try:
            token = settings.PLIVO_AUTH_TOKEN
            if method.upper() == "GET":
                return bool(plivo.utils.validate_v3_signature(method, url, nonce, token, signature))
            return bool(plivo.utils.validate_v3_signature(method, url, nonce, token, signature, params or {}))
        except Exception:
            logger.warning("⚠️ [Plivo] Signature check raised an error")
            return False

    # ==================================================
    # AUDIO STREAM MESSAGES
    # ==================================================

    def parse_stream_event(self, msg: dict) -> StreamEvent | None:
        """Translate a Plivo WebSocket message (start, media, playedStream, dtmf)."""
        event = msg.get("event")
        if event == "start":
            start = msg.get("start", {})
            # Plivo sends the custom headers at top level, the start payload also carries them.
            raw_headers = msg.get("extra_headers") or start.get("extraHeaders") or ""
            headers = _parse_extra_headers(raw_headers)
            phone = headers.get("callerPhone", "")
            if phone.isdigit() and len(phone) >= 10:
                phone = "+" + phone          # Plivo strips the '+', restore it
            return StreamEvent(
                "start",
                call_id=start.get("callId", ""),
                stream_id=start.get("streamId", ""),
                # caller_phone=headers.get("callerPhone", ""),
                caller_phone=phone,
                format_ok=is_expected_format(start.get("mediaFormat", {})),
                extra=headers,                       # ← the only new line that matters
            )
        if event == "media":
            media = msg.get("media", {})
            if media.get("track", "inbound") != "inbound":
                return None
            return StreamEvent("media", audio=base64.b64decode(media.get("payload", "")))
        if event == "playedStream":
            return StreamEvent("mark", name=msg.get("name", ""))
        if event == "dtmf":
            return StreamEvent("dtmf")
        return None

    def audio_message(self, stream_id: str, ulaw: bytes) -> dict:
        """Plivo playAudio message."""
        return {"event": "playAudio", "media": {
            "contentType": STREAM_ENCODING, "sampleRate": STREAM_RATE,
            "payload": base64.b64encode(ulaw).decode()}}

    def mark_message(self, stream_id: str, name: str) -> dict:
        """Plivo checkpoint message (answered by a playedStream event)."""
        return {"event": "checkpoint", "streamId": stream_id, "name": name}

    def clear_message(self, stream_id: str) -> dict:
        """Plivo clearAudio message."""
        return {"event": "clearAudio", "streamId": stream_id}
    
    # ==================================================
    # OUTBOUND CALLS AND RECORDING
    # ==================================================

    async def make_call(self, to: str, answer_url: str, hangup_url: str) -> str:
        client = self._rest()
        response = await asyncio.to_thread(
            client.calls.create,
            from_=settings.PLIVO_NUMBER.lstrip("+"), to_=to.lstrip("+"),
            answer_url=answer_url, answer_method="POST",
            hangup_url=hangup_url, hangup_method="POST",
        )
        return getattr(response, "request_uuid", "") or ""

    async def start_recording(self, call_id: str, callback_url: str) -> None:
        client = self._rest()
        await asyncio.to_thread(
            client.calls.record, call_uuid=call_id,
            time_limit=900, file_format="mp3",
            callback_url=callback_url, callback_method="POST",
        )