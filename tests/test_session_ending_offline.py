"""
Offline tests for CallSession's inbound ending (fake socket, fake Plivo, no audio, no LLM).
Run: python tests/test_session_ending_offline.py
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.booking import flow
from app.config import settings
from app.voice import session as sess


class FakeAdapter:
    name = "plivo"

    def __init__(self, fail_hangup=False, fail_transfer=False):
        self.hangups, self.transfers, self.fail_hangup, self.fail_transfer = [], [], fail_hangup, fail_transfer

    async def hangup_call(self, call_id):
        if self.fail_hangup:
            raise RuntimeError("boom")
        self.hangups.append(call_id)

    async def transfer_call(self, call_id, url):
        if self.fail_transfer:
            raise RuntimeError("boom")
        self.transfers.append(url)

    def clear_message(self, sid):
        return {"clear": sid}


class FakeWS:
    closed = False

    async def close(self, code=1000):
        self.closed = True

    async def send_text(self, _):
        pass


class FakeGraph:
    def __init__(self):
        self.updates = []

    def update_state(self, config, values, as_node=None):
        self.updates.append((values, as_node))

    def get_state(self, config):
        return SimpleNamespace(values={"phase": flow.PHASE_SLOT})


def make(**kw):
    s = sess.CallSession(FakeWS(), FakeGraph(), None, FakeAdapter(**kw))
    s.call_id, s.phone, s.stream_id = "c1", "+911", "s1"
    spoken = []

    async def speak(text, generation=None):
        spoken.append(text)
        return "m1"

    async def wait_played(name, timeout=20):
        return True

    s._speak, s._wait_played = speak, wait_played
    s._spoken = spoken
    return s


def patch_db():
    sess.save_snapshot = lambda *a, **k: True
    sess.mark_transfer_not_started = lambda *a, **k: True
    sess.finish_receptionist_attempt = lambda *a, **k: True


async def t_end_hangs_up():
    s = make()
    await s._end_call("m1", "Thank you. Take care.")
    assert s.adapter.hangups == ["c1"], s.adapter.hangups


async def t_end_hangs_up_even_without_marker():
    s = make()
    real = asyncio.sleep
    sess.asyncio.sleep = lambda _s: real(0)
    try:
        await s._end_call(None, "x")
    finally:
        sess.asyncio.sleep = real
    assert s.adapter.hangups == ["c1"]


async def t_hangup_failure_closes_stream():
    s = make(fail_hangup=True)
    await s._end_call("m1", "x")
    assert s.ws.closed


async def t_final_line_not_interrupted():
    s = make()
    s._ending, s.speaking, s._generation = True, True, 5
    await s._barge_in()
    assert s._generation == 5          # unchanged: nothing was cut


async def t_silence_sends_one_turn():
    s = make()
    settings.INBOUND_SILENCE_REASK_S = 0.5
    s._last_inbound_state = {"phase": flow.PHASE_OFFER}
    s._arm_silence_timer(s._generation, "m1")
    await asyncio.wait_for(s._silence_task, 5)
    assert s.turns.get_nowait() == sess._TURN_SILENCE


async def t_speaking_caller_not_cut_off():
    s = make()
    settings.INBOUND_SILENCE_REASK_S = 0.5
    s._last_inbound_state = {"phase": flow.PHASE_SLOT}
    s.vad.in_speech = True
    s._arm_silence_timer(s._generation, "m1")
    await asyncio.sleep(1.5)
    assert s.turns.empty()             # still waiting while the caller talks
    s.vad.in_speech = False
    await asyncio.wait_for(s._silence_task, 5)
    assert s.turns.get_nowait() == sess._TURN_SILENCE


async def t_no_timer_during_intake():
    s = make()
    s._last_inbound_state = {"phase": None}
    s._arm_silence_timer(s._generation, "m1")
    assert s._silence_task is None


async def t_transfer_ok_closes_stream():
    s = make()
    await s._connect_receptionist("m1")
    assert s.adapter.transfers and "/receptionist?lang=" in s.adapter.transfers[0] and s.ws.closed


async def t_transfer_failure_resumes_booking():
    s = make(fail_transfer=True)
    s._last_inbound_state = {"phase": flow.PHASE_TRANSFERRING}
    await s._connect_receptionist("m1")
    assert s._spoken and "not available" in s._spoken[0], s._spoken
    assert s.graph.updates and s.graph.updates[0][0]["phase"] == flow.PHASE_SLOT
    assert not s._ending and not s._transferred
    s._cancel_silence_timer()


async def t_resume_start_asks_for_slot():
    s = make()
    await s._start_resume_call({"lang": "hi"})
    assert s.lang == "hi" and s.turns.get_nowait() == sess._TURN_RESUME


def main():
    patch_db()
    from app.agent.shared.loader import validate_data_files, set_reply_language
    validate_data_files()
    set_reply_language("en")
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("t_"):
            try:
                asyncio.run(fn())
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback; traceback.print_exc()
                print("FAIL", name, repr(e))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
