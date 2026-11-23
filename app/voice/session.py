"""
app/voice/session.py
────────────────────
One CallSession = one live phone call over the provider's WebSocket (Plivo or Twilio).

  caller audio -> VAD (finds sentence end) -> speech-to-text -> LangGraph -> text-to-speech -> audio back

Rules:
  - A failure in one turn never kills the call; the caller hears an approved fallback line.
  - Transcripts are logged only when LOG_TRANSCRIPTS=true (testing).
  - The call starts in DEFAULT_LANGUAGE (Odia) and follows the caller's language.
  - action == "transfer" moves the call to the doctor (emergency).
  - action == "receptionist" connects the doctor's assistant; if nobody answers the call
    comes back as a "resume" stream and the bot asks for the appointment day and time.
  - action == "end" speaks the final line (never interrupted) and always hangs up.
  - After a booking question the bot waits for the caller; only if the caller stays silent
    (and is not speaking) does it re-ask once, then close by default.
  - A short acknowledgement plays only after a long caller sentence when the reply is slow.
  - All provider-specific message formats live in the adapter, not here.
"""

import asyncio
import json
import logging
import time

from fastapi import WebSocket

from app.agent.booking import flow
from app.agent.booking.nodes import SILENCE, resume_after_failed_transfer
from app.agent.shared.loader import pick_reply, set_reply_language
from app.config import settings
from app.services.inbound import call_logs as call_service
from app.services.inbound import summary as summary_service
from app.speech.audio_utils import VoiceActivityDetector, ulaw_to_pcm, utterance_to_wav
from app.speech.factory import synthesize_ulaw, transcribe
from app.speech.text_utils import requested_language, split_sentences
from app.telephony.base import StreamEvent, TelephonyAdapter
from app.voice.filler import filler_kind, pick_filler
from app.voice.call_wrapup import (
    finish_receptionist_attempt,
    mark_transfer_not_started,
    save_snapshot,
)

logger = logging.getLogger("voice.session")

PLAY_CHUNK_BYTES = 3200  # 400 ms of 8 kHz mu-law audio per message
PCM_BYTES_PER_SECOND = 16000  # 8 kHz, 16-bit mono PCM
SPEECH_CHARS_PER_SECOND = 14.0  # rough speaking rate, used to time the hangup after a goodbye
PLAYBACK_WAIT_TIMEOUT_S = 20  # max wait for the provider's 'audio finished' marker
SUPPORTED_LANGS = ("en", "hi", "or")

# Queue items that are not caller audio.
_TURN_RESUME = "__resume__"      # the call came back after the assistant did not answer
_TURN_SILENCE = "__silence__"    # the caller stayed silent after a booking question

# The Answer webhook knows the caller's number; keep it briefly in case the stream start lacks it.
_CALLERS: dict[str, str] = {}


def register_caller(call_id: str, phone: str) -> None:
    """Remember a caller's number from the Answer webhook."""
    _CALLERS[call_id] = phone


def _public_url(path: str) -> str:
    """Full public URL for a provider callback, e.g. _public_url("/voice/plivo/transfer")."""
    return f"{settings.PUBLIC_BASE_URL.rstrip('/')}{path}"


class CallSession:
    """Handles everything that happens on one call's audio stream."""

    def __init__(self, ws: WebSocket, graph, reminder_graph, adapter: TelephonyAdapter) -> None:
        """One live call: keeps the socket, the graph, the telephony adapter and all per-call state
        (language, filler, silence timer, transfer and ending flags)."""
        self.ws = ws
        self.graph = graph
        self.reminder_graph = reminder_graph
        self.adapter = adapter
        self.mode = "inbound"
        self.appt_name = ""
        self.appt_id: int | None = None
        self.call_id: str | None = None
        self.stream_id: str | None = None
        self.phone = "unknown"

        self.vad = VoiceActivityDetector(
            rms_threshold=settings.VAD_RMS_THRESHOLD,
            end_silence_ms=settings.VAD_END_SILENCE_MS,
            min_speech_ms=settings.VAD_MIN_SPEECH_MS,
            max_utterance_ms=settings.VAD_MAX_UTTERANCE_S * 1000,
        )
        self.turns: asyncio.Queue = asyncio.Queue()  # Queued caller turns; None means greeting
        self.speaking = False
        self._played: dict[str, asyncio.Event] = {}  # Marker name -> playback completed
        self._marker_no = 0
        self._generation = 0                         # Incremented on interruption to cancel pending speech
        self._transferred = False
        self._last_inbound_state = {}
        self._initial_inbound_state: dict | None = None  # set by _greet, used on the first agent turn
        self._last_inbound_reply = ""
        self._inbound_caller_turns: list[str] = []
        self._silence_task: asyncio.Task | None = None
        self._resumed = False                        # the call came back after the assistant did not answer
        self._ending = False                         # final line is playing: no barge-in, no new turns

        self._t_start = 0.0
        self._audio_started = False
        self._first_audio_at: float | None = None
        self._filler_played = False
        self._fillers_used = 0
        self._turns_since_filler = 99
        self._filler_task: asyncio.Task | None = None
        self._filler_used: set[str] = set()      # lines already said on this call
        self._turn_text: str | None = None       # what the caller just said (known after speech-to-text)

        self.lang = settings.DEFAULT_LANGUAGE        # Current call language: or, hi, or en
        self._lang_set = False
        self._pending_lang: str | None = None

    # ==================================================
    # MAIN LOOP
    # ==================================================

    async def run(self) -> None:
        """Read events from the provider until the call ends, then save the summary."""
        worker = asyncio.create_task(self._turn_worker())
        try:
            async for raw in self.ws.iter_text():
                event = self.adapter.parse_stream_event(json.loads(raw))
                if event:
                    await self._on_event(event)
        except Exception:
            if self._transferred or self._ending:
                logger.info("📴 [Voice] Stream closed by the bot after the transfer/hangup")
            else:
                logger.exception("❌ [Voice] Stream loop crashed")
        finally:
            worker.cancel()
            logger.info("👋 [Voice] Stream closed")
            await self._finish()

    async def _on_event(self, event: StreamEvent) -> None:
        """Route one neutral event (media is the most frequent, so it is first)."""
        if event.kind == "media":
            await self._on_media(event)
        elif event.kind == "start":
            await self._on_start(event)
        elif event.kind == "mark":
            self._on_played(event.name)
        elif event.kind == "dtmf":
            logger.info("🔢 [Voice] A key was pressed")
        elif event.kind == "stop":
            logger.info("🛑 [Voice] Provider ended the stream")

    async def _on_start(self, event: StreamEvent) -> None:
        """Stream began: check the audio format, work out the mode, log the call, queue the greeting."""
        self.call_id = event.call_id
        self.stream_id = event.stream_id
        self._t_start = time.perf_counter()

        if not event.format_ok:
            logger.error("❌ [Voice] Unexpected audio format from the provider")
            await self.ws.close(code=1003)
            return

        fallback = _CALLERS.pop(self.call_id or "", "")
        self.phone = event.caller_phone or fallback or ""

        # Outbound reminder calls carry mode=reminder and the appointment id in the stream headers.
        extra = {**(event.extra or {}), **(getattr(self, "query_params", None) or {})}
        logger.info("🔖 [Voice] Stream headers: %s", extra)

        if extra.get("mode") == "reminder":
            await self._start_reminder_call(extra)
        elif extra.get("mode") == "resume":
            await self._start_resume_call(extra)
        else:
            await self._start_inbound_call()

    async def _on_media(self, event: StreamEvent) -> None:
        """Caller audio: run the VAD, handle interruption, queue finished sentences."""
        if self._transferred or not event.audio:
            return
        utterance = self.vad.feed(ulaw_to_pcm(event.audio))

        if (
            self.speaking
            and not self._ending
            and self.vad.in_speech
            and self.vad.speech_ms >= settings.BARGE_IN_MS
        ):
            await self._barge_in()
        if utterance:
            self.turns.put_nowait(utterance)

    def _on_played(self, name: str) -> None:
        """The provider finished playing audio up to a marker."""
        event = self._played.pop(name, None)
        if event:
            event.set()
        if not self._played:
            self.speaking = False

    async def _barge_in(self) -> None:
        """Caller talked over the bot: stop the audio now and drop any reply still being prepared."""
        if self._ending:
            return          # the final line is never cut off
        logger.info(
            "✋ [Voice] Caller interrupted (speech %s ms), clearing audio", self.vad.speech_ms
        )
        self._generation += 1
        self.speaking = False

        if self.mode == "inbound":
            self._last_inbound_reply = ""

        await self._send(self.adapter.clear_message(self.stream_id))
        for event in self._played.values():
            event.set()
        self._played.clear()

    # ==================================================
    # LANGUAGE
    # ==================================================

    def _update_language(self, text: str, provider_lang: str | None) -> None:
        """Follow the caller's language: the first clear sentence sets it, a switch needs two in a row."""
        # The call keeps its language (Odia by default) until the caller ASKS for another one
        # ("speak Hindi", "can you talk in English"). Speech-to-text often labels a short or mixed
        # sentence as another language, so the detected language is never used to switch.
        requested = requested_language(text)
        if requested and requested != self.lang:
            self.lang, self._lang_set, self._pending_lang = requested, True, None
            logger.info("🌐 [Voice] Caller asked for %s, switching", self.lang)

    # ==================================================
    # TURN HANDLING
    # ==================================================

    async def _turn_worker(self) -> None:
        """Answer queued sentences one at a time; an error in one turn never stops the next."""
        while True:
            item = await self.turns.get()
            if self._transferred or self._ending:
                continue
            try:
                if item is None:
                    await self._greet()
                elif item == _TURN_RESUME:
                    await self._resume_booking()
                elif item == _TURN_SILENCE:
                    await self._handle_turn(b"", silence=True)
                else:
                    await self._handle_turn(item)
            except Exception:
                logger.exception("❌ [Voice] Turn failed")

    async def _greet(self) -> None:
        """Say the opening line (reminder or inbound greeting) and log how fast audio started."""
        self._audio_started = False
        self._first_audio_at = None
        set_reply_language(self.lang)

        if self.mode == "reminder":
            greeting = self._greet_reminder()
        else:
            greeting = await self._greet_inbound()

        if settings.LOG_TRANSCRIPTS:
            logger.info("🤖 [Bot] %s", greeting)

        if self.mode == "inbound":
            self._last_inbound_reply = greeting

        await self._speak(greeting)

        if self._first_audio_at:
            logger.info(
                "⏱️ [Voice] greeting: first audio %.1fs after the stream started",
                self._first_audio_at - self._t_start,
            )

    async def _handle_turn(self, pcm_8k: bytes, silence: bool = False) -> None:
        """One caller turn: speech to text, LangGraph, text to speech, with timing logs and safe fallbacks."""
        t0 = time.perf_counter()
        turn_gen = self._generation
        self._audio_started = False
        self._first_audio_at = None
        self._filler_played = False
        self._turn_text = None
        self._cancel_silence_timer()

        speech_s = len(pcm_8k) / PCM_BYTES_PER_SECOND
        self._turns_since_filler += 1
        use_filler = (
            not silence
            and settings.FILLER_ENABLED
            and speech_s >= settings.FILLER_MIN_SPEECH_S
            and self._fillers_used < settings.FILLER_MAX_PER_CALL
            and self._turns_since_filler >= 2
        )

        self._filler_task = (
            asyncio.create_task(self._filler_after_delay(turn_gen)) if use_filler else None
        )
        try:
            # 1) speech to text (a silence turn has no audio: the flow just gets the marker)
            if silence:
                text, t_stt = SILENCE, time.perf_counter()
                logger.info("🤫 [Voice] Caller silent after the question, re-asking or closing")
            else:
                transcript = await self._transcribe(pcm_8k)
                text = transcript.text if transcript else ""
                t_stt = time.perf_counter()
                if not text:
                    marker = await self._speak(pick_reply("not_understood", self.lang), turn_gen)
                    self._arm_silence_timer(turn_gen, marker)    # never leave dead air
                    return

                self._turn_text = text
                self._note_caller_text(text, transcript.language)
                if self.mode == "inbound":
                    self._inbound_caller_turns.append(text)

            # 2) the agent
            result = await self._run_agent(text, turn_gen)
            if result is None:
                return
            t_agent = time.perf_counter()

            if self.mode == "inbound" and result.get("action") in ("end", "receptionist"):
                self._ending = True        # this line must be heard in full

            # 3) text to speech
            reply, marker = await self._say_reply(result, turn_gen)
            self._log_turn_timing(t0, t_stt, t_agent)

            # 4) whatever the agent asked for next (transfer, connect the assistant, hang up)
            await self._apply_action(result, reply, marker, turn_gen)
        finally:
            await self._cancel_filler()

    async def _transcribe(self, pcm_8k: bytes):
        """Speech to text. Returns the transcript, or None when the speech service fails."""
        try:
            return await transcribe(utterance_to_wav(pcm_8k))
        except Exception:
            logger.exception("❌ [Voice] Speech-to-text failed")
            return None

    def _note_caller_text(self, text: str, provider_lang: str | None) -> None:
        """Update the call language from what the caller said and log it."""
        self._update_language(text, provider_lang)
        set_reply_language(self.lang)               # the agent's prompt and fixed lines follow this

        if settings.LOG_TRANSCRIPTS:
            logger.info("🗣️ [Caller] %s", text)
        else:
            logger.info("🗣️ [Voice] Caller spoke (%s characters)", len(text))

    async def _run_agent(self, text: str, turn_gen: int) -> dict | None:
        """Run the LangGraph for this turn. On failure the caller hears the error line and None is returned."""
        config = {
            "configurable": {
                "thread_id": self.call_id,
                "caller_phone": self.phone,
            }
        }

        if self.mode == "reminder" and self.appt_id:
            config["configurable"]["appt_id"] = self.appt_id      # the reminder graph needs the appointment id
        try:
            graph_input = {
                "messages": [("user", text)],
                "caller_phone": self.phone,
                "action": None,
            }

            initial_state = self._initial_inbound_state
            if self.mode == "inbound" and initial_state is not None:
                graph_input.update(initial_state)

            result = await asyncio.to_thread(
                self.graph.invoke,
                graph_input,
                config,
            )

            if self.mode == "inbound":
                self._initial_inbound_state = None
                self._last_inbound_state = result

                await asyncio.to_thread(
                    save_snapshot,
                    self.call_id,
                    self.phone,
                    result,
                    caller_turns=list(self._inbound_caller_turns),
                )
            return result

        except Exception:
            logger.exception("❌ [Voice] Agent failed")
            await self._speak(pick_reply("error", self.lang), turn_gen)
            return None

    async def _say_reply(self, result: dict, turn_gen: int) -> tuple[str, str | None]:
        """Speak the agent's answer. Returns (reply text, playback marker)."""
        reply = (result["messages"][-1].content or "").strip() or pick_reply("error", self.lang)
        if settings.LOG_TRANSCRIPTS:
            logger.info("🤖 [Bot] %s", reply)
        marker = await self._speak(reply, turn_gen)

        if self.mode == "inbound" and marker and turn_gen == self._generation:
            self._last_inbound_reply = reply
        return reply, marker

    def _log_turn_timing(self, t0: float, t_stt: float, t_agent: float) -> None:
        """Log how long speech-to-text and the agent took, and when the reply audio started."""
        first = f"{self._first_audio_at - t0:.1f}s" if self._first_audio_at else "none"
        logger.info(
            "⏱️ [Voice] stt %.1fs | agent %.1fs | first reply audio %s after speech ended "
            "(+%.1fs end-of-speech wait) | filler %s",
            t_stt - t0,
            t_agent - t_stt,
            first,
            settings.VAD_END_SILENCE_MS / 1000,
            "played" if self._filler_played else "not used",
        )

    async def _apply_action(
        self, result: dict, reply: str, marker: str | None, turn_gen: int
    ) -> None:
        """Do what the agent asked for after speaking: transfer, connect the assistant, hang up,
        or wait for the caller (with a speech-aware silence timer)."""
        action = result.get("action")
        if action == "transfer":
            await self._transfer(marker)
        # elif action == "hangup" and self.mode == "reminder":
        #     logger.info("👋 [Voice] Reminder saved, hanging up")
        #     # Let the goodbye finish playing on the phone (≈14 characters a second) before dropping the line.
        #     await asyncio.sleep(min(6.0, max(2.0, len(reply) / SPEECH_CHARS_PER_SECOND)))
        #     await self.ws.close()
        elif action == "hangup" and self.mode == "reminder":
            logger.info("👋 [Voice] Reminder saved, hanging up")
            # Wait for Plivo to confirm the goodbye finished playing; use the timer only if it never confirms.
            played = await self._wait_played(marker) if marker else False
            if not played:
                await asyncio.sleep(min(6.0, max(2.0, len(reply) / SPEECH_CHARS_PER_SECOND)))
            await asyncio.sleep(1.0)  # short tail so the last word is not clipped
            await self.ws.close()
        elif self.mode == "inbound" and action == "receptionist":
            await self._connect_receptionist(marker)
        elif self.mode == "inbound" and action == "end":
            await self._end_call(marker, reply)
        elif self.mode == "inbound":
            self._arm_silence_timer(turn_gen, marker)

    async def _filler_after_delay(self, generation: int) -> None:
        """If no reply audio started after FILLER_AFTER_MS, play a short cached acknowledgement in the caller's language."""
        try:
            await asyncio.sleep(settings.FILLER_AFTER_MS / 1000)
            if self._audio_started or generation != self._generation or self._transferred:
                return
            phase = (self._last_inbound_state or {}).get("phase") if self.mode == "inbound" else None
            kind = filler_kind(self._turn_text, phase)
            line = pick_filler(kind, self.lang, self._filler_used) if kind else None
            if not line:
                return                                  # emergency/urgent, or every line already used
            ulaw = await synthesize_ulaw(line)          # cached, so it starts instantly
            if self._audio_started or generation != self._generation:
                return
            await self._send_audio(ulaw)
            self.speaking = True
            self._filler_played = True
            self._fillers_used += 1
            self._turns_since_filler = 0
            await self._send_marker("filler")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("⚠️ [Voice] Filler clip failed")

    async def _cancel_filler(self) -> None:
        """Stop the short acknowledgement clip if it has not played yet."""
        task, self._filler_task = self._filler_task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _transfer(self, marker: str | None) -> None:
        """Emergency: let the transfer line finish, then move the call to the doctor's number."""
        self._transferred = True
        if marker:
            await self._wait_played(marker)
        url = _public_url(f"/voice/{self.adapter.name}/transfer")

        try:
            await self.adapter.transfer_call(self.call_id, url)
        except Exception:
            logger.exception("❌ [Voice] Could not transfer the call")
            self._transferred = False
            await self._speak(pick_reply("transfer_failed", self.lang))
            return

        await self._close_stream()

    # ==================================================
    # INBOUND CALLS
    # ==================================================

    async def _start_inbound_call(self) -> None:
        """Inbound call: log it, save an empty snapshot and queue the greeting."""
        if not self.phone:
            self.phone = "unknown"
            logger.warning("⚠️ [Voice] Caller number missing, treating the caller as new")

        await asyncio.to_thread(call_service.start_call, self.call_id, self.phone)
        await asyncio.to_thread(save_snapshot, self.call_id, self.phone, {})
        self.turns.put_nowait(None)
        logger.info("✅ [Voice] Stream started for call %s", self.call_id)

    async def _greet_inbound(self) -> str:
        """Inbound greeting: by name when the phone matches a patient, generic otherwise."""
        # If lookup fails, use the generic greeting. If a later lookup finds
        # an existing patient, verification starts by asking for their name.
        self._initial_inbound_state = {"identity_stage": "verify_name"}

        try:
            patient = await asyncio.to_thread(self._find_caller_by_phone)
        except Exception:
            logger.exception("❌ [Voice] Could not look up caller for greeting")
            patient = None

        if patient:
            patient_id, name = patient
            greeting = pick_reply("greeting_existing", self.lang).format(name=name)
            self._initial_inbound_state = {
                "caller_type": "existing",
                "patient_id": patient_id,
                "is_verified": False,
                "verify_attempts": 0,
                "urgency": "routine",
                "identity_stage": "confirm_name",
            }
        else:
            greeting = pick_reply("greeting", self.lang)
        return greeting

    def _find_caller_by_phone(self) -> tuple[int, str] | None:
        """Look the caller up in the patient table. Runs in a worker thread."""
        from app.database import get_session
        from app.services.shared import patients as patient_service

        if not self.phone or self.phone == "unknown":
            return None

        phone = patient_service.normalize_phone(self.phone)
        with get_session() as db:
            patient = patient_service.find_by_phone(db, phone)
            if patient and patient.full_name:
                return patient.id, patient.full_name
        return None

    def _current_inbound_state(self) -> dict:
        """Latest agent state for this inbound call (falls back to the state set at the greeting)."""
        return self._last_inbound_state or self._initial_inbound_state or {}

    # ==================================================
    # END OF THE INBOUND CALL
    # ==================================================

    def _booking_phase_active(self) -> bool:
        return (self._last_inbound_state or {}).get("phase") in flow.BOOKING_PHASES

    def _cancel_silence_timer(self) -> None:
        """Stop the silence watch (never cancels the task that is running right now)."""
        task = self._silence_task
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
        self._silence_task = None

    def _arm_silence_timer(self, generation: int, marker: str | None) -> None:
        """After a booking question: if the caller stays silent, send one silence turn."""
        self._cancel_silence_timer()
        if self.mode != "inbound" or self._ending or not self._booking_phase_active():
            return
        self._silence_task = asyncio.create_task(self._silence_after_playback(generation, marker))

    async def _silence_after_playback(self, generation: int, marker: str | None) -> None:
        """Wait for the question to finish playing, then for the caller to be quiet for
        INBOUND_SILENCE_REASK_S. Any speech restarts the wait, so a caller who is talking
        or thinking aloud is never cut off."""
        try:
            if marker:
                await self._wait_played(marker)
            window = settings.INBOUND_SILENCE_REASK_S
            quiet, elapsed = 0.0, 0.0
            while quiet < window and elapsed < window * 4:
                await asyncio.sleep(0.25)
                elapsed += 0.25
                if generation != self._generation or self._transferred or self._ending:
                    return
                if not self.turns.empty():
                    return                    # a caller turn is already waiting: it will answer
                quiet = 0.0 if self.vad.in_speech else quiet + 0.25
            if generation == self._generation and not self._ending and self.turns.empty():
                self.turns.put_nowait(_TURN_SILENCE)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("❌ [Voice] Silence timer failed")

    async def _end_call(self, marker: str | None, reply: str) -> None:
        """The final line was spoken: save, let it finish playing, always hang up."""
        try:
            await asyncio.to_thread(
                save_snapshot,
                self.call_id,
                self.phone,
                self._current_inbound_state(),
                caller_turns=list(self._inbound_caller_turns),
                outcome="needs_followup",
            )
        except Exception:
            logger.exception("❌ [Voice] Could not save the closing snapshot")

        try:
            played = await self._wait_played(marker) if marker else False
            if not played:
                # No confirmation from the provider: wait roughly as long as the line takes to say.
                await asyncio.sleep(min(6.0, max(2.0, len(reply or "") / SPEECH_CHARS_PER_SECOND)))
        finally:
            await self._hangup()

    async def _hangup(self) -> None:
        """End the call. If the provider call fails, closing the stream still ends it."""
        self._transferred = True
        if self.adapter.name == "plivo" and self.call_id:
            try:
                await self.adapter.hangup_call(self.call_id)
                logger.info("👋 [Voice] Call ended by the bot")
                return
            except Exception:
                logger.exception("❌ [Voice] Plivo hangup failed, closing the stream instead")
        await self._close_stream()

    async def _connect_receptionist(self, marker: str | None) -> None:
        """The caller said yes: hand the call to the doctor's assistant. If that cannot start,
        the bot stays on the line and asks for the appointment day and time."""
        state = self._current_inbound_state()
        saved = await asyncio.to_thread(
            save_snapshot,
            self.call_id,
            self.phone,
            state,
            caller_turns=list(self._inbound_caller_turns),
            wants_receptionist=True,
            transfer_status="requested",
        )
        if not saved:
            logger.error("❌ [Voice] Could not save the transfer request for %s", self.call_id)

        played = await self._wait_played(marker) if marker else False
        if not played or self.adapter.name != "plivo":
            logger.info("Receptionist transfer not started (playback not confirmed)")
            await asyncio.to_thread(mark_transfer_not_started, self.call_id)
            self._ending = False
            await self._resume_booking()
            return

        self._transferred = True
        lang = self.lang if self.lang in SUPPORTED_LANGS else "en"
        url = _public_url(f"/voice/plivo/receptionist?lang={lang}")
        try:
            logger.info("☎️ [Voice] Receptionist transfer URL: %s", url)
            await self.adapter.transfer_call(self.call_id, url)
        except Exception:
            logger.exception("Could not initiate receptionist transfer")
            self._transferred = False
            await asyncio.to_thread(finish_receptionist_attempt, self.call_id, False)
            self._ending = False
            await self._resume_booking()
            return

        # Plivo runs the transfer XML only after this stream's socket closes.
        await self._close_stream()

    async def _resume_booking(self) -> None:
        """The assistant did not answer (or never rang): say so and ask for the day and time."""
        info = resume_after_failed_transfer()
        set_reply_language(self.lang)
        line = info.pop("line")
        config = {"configurable": {"thread_id": self.call_id}}
        try:
            from langchain_core.messages import AIMessage  # type: ignore

            await asyncio.to_thread(
                self.graph.update_state,
                config,
                {**info, "messages": [AIMessage(line)]},
                "booking_flow",
            )
            values = (await asyncio.to_thread(self.graph.get_state, config)).values
            self._last_inbound_state = dict(values)
        except Exception:
            logger.exception("❌ [Voice] Could not move the call back to the booking step")
            self._last_inbound_state = {**(self._last_inbound_state or {}), **info}

        if settings.LOG_TRANSCRIPTS:
            logger.info("🤖 [Bot] %s", line)
        self._last_inbound_reply = line
        marker = await self._speak(line)
        self._arm_silence_timer(self._generation, marker)

    async def _start_resume_call(self, extra: dict) -> None:
        """A new stream for a call we already know: the assistant did not pick up.
        Load the saved state and ask for the appointment day and time (no greeting)."""
        lang = (extra.get("lang") or "").lower()
        self.lang = lang if lang in SUPPORTED_LANGS else self.lang
        self._lang_set = True
        try:
            snap = await asyncio.to_thread(
                self.graph.get_state, {"configurable": {"thread_id": self.call_id}}
            )
            values = dict(snap.values or {})
        except Exception:
            logger.exception("❌ [Voice] Could not load the saved call state")
            values = {}
        self._last_inbound_state = values
        self._resumed = True
        self._inbound_caller_turns = [
            m.content for m in values.get("messages", [])
            if getattr(m, "type", "") == "human" and isinstance(m.content, str) and m.content != SILENCE
        ]
        self.turns.put_nowait(_TURN_RESUME)
        logger.info("🔁 [Voice] Resumed call %s after the assistant did not answer", self.call_id)

    # ==================================================
    # OUTBOUND CALLS
    # ==================================================

    async def _start_reminder_call(self, extra: dict) -> None:
        """Reminder call: load the appointment, switch to the reminder graph and queue the greeting."""
        from app.services.outbound import appointments as appointment_service   # lazy import: inbound never needs it
        self.mode = "reminder"
        try:
            self.appt_id = int(extra.get("apptId") or 0)
        except ValueError:
            self.appt_id = None
        brief = await asyncio.to_thread(appointment_service.get_brief, self.appt_id)
        if not brief or self.reminder_graph is None:
            logger.error("❌ [Voice] Reminder call without appointment/graph, hanging up")
            await self.ws.close()
            return
        self.appt_name = brief["name"]
        self.phone = brief["phone"]
        self.graph = self.reminder_graph
        if brief.get("language") in SUPPORTED_LANGS:
            self.lang = brief["language"]
            self._lang_set = True          # appointment language is the baseline; a switch needs two turns
        logger.info("📞 [Voice] Reminder call for %s (appointment %s)", self.appt_name, self.appt_id)
        self.turns.put_nowait(None)                      # greet first, don't wait on the network
        asyncio.create_task(self._start_recording())     # Plivo API call, off the greeting path
        asyncio.create_task(asyncio.to_thread(call_service.start_call, self.call_id, self.phone))
        logger.info("✅ [Voice] Stream started for call %s", self.call_id)
        return

    def _greet_reminder(self) -> str:
        """Reminder greeting in the call language, with the patient's name."""
        from app.agent.reminders.graph import reminder_greeting
        return reminder_greeting(self.lang, self.appt_name)

    async def _start_recording(self) -> None:
        """Ask Plivo to record this call. A failure here never stops the call."""
        if not self.call_id or not self.appt_id:
            return
        try:
            callback = _public_url(f"/voice/plivo/outbound/recording?appt={self.appt_id}")
            await self.adapter.start_recording(self.call_id, callback)
            logger.info("🎙️ [Voice] Recording started (appointment %s)", self.appt_id)
        except Exception:
            logger.warning("⚠️ [Voice] Could not start the recording", exc_info=True)

    # ==================================================
    # SPEAKING
    # ==================================================

    async def _speak(self, text: str, generation: int | None = None) -> str | None:
        """Make speech for `text` (pieces in parallel, played in order). Returns a marker name, or None if nothing played.
        If the caller interrupted since `generation`, nothing is played."""
        generation = self._generation if generation is None else generation
        sent = False
        tasks = [asyncio.create_task(synthesize_ulaw(piece)) for piece in split_sentences(text)]
        try:
            for task in tasks:
                if generation != self._generation:
                    break
                try:
                    ulaw = await task
                except Exception:
                    logger.exception("❌ [Voice] Text-to-speech failed")
                    break
                if generation != self._generation:
                    break
                if not self._audio_started:
                    self._audio_started = True
                    self._first_audio_at = time.perf_counter()
                await self._send_audio(ulaw)
                self.speaking = True
                sent = True
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if not sent:
            return None
        return await self._send_marker("reply")

    async def _send_audio(self, ulaw: bytes) -> None:
        """Send audio to the caller in 400 ms chunks."""
        for i in range(0, len(ulaw), PLAY_CHUNK_BYTES):
            await self._send(self.adapter.audio_message(self.stream_id, ulaw[i:i + PLAY_CHUNK_BYTES]))

    async def _wait_played(self, name: str, timeout: float = PLAYBACK_WAIT_TIMEOUT_S) -> bool:
        """Wait for Plivo's playback checkpoint."""
        event = self._played.get(name)

        if event:
            try:
                await asyncio.wait_for(event.wait(), timeout)
            except asyncio.TimeoutError:
                logger.warning("⚠️ [Voice] Playback confirmation timed out")
                return False

        return True

    async def _send(self, payload: dict) -> None:
        """Send one JSON message to the provider."""
        await self.ws.send_text(json.dumps(payload))

    async def _close_stream(self) -> None:
        """Close the provider's WebSocket (it may already be closed)."""
        try:
            await self.ws.close()
        except Exception:
            logger.debug("Stream already closed", exc_info=True)

    async def _send_marker(self, prefix: str) -> str:
        """Ask the provider to tell us when the audio sent so far has finished playing."""
        self._marker_no += 1
        name = f"{prefix}-{self._marker_no}"
        self._played[name] = asyncio.Event()
        await self._send(self.adapter.mark_message(self.stream_id, name))
        return name

    # ==================================================
    # CALL END
    # ==================================================

    async def _finish(self) -> None:
        """Call ended: write the summary and alert the doctor if the call needs attention."""
        if not self.call_id:
            return
        if self.mode == "reminder":
            logger.info("📴 [Voice] Reminder call ended (appointment %s)", self.appt_id)
            return
        try:
            await asyncio.to_thread(summary_service.finish_call, self.graph, self.call_id, self._resumed)
        except Exception:
            logger.exception("❌ [Voice] Could not save the call summary")
