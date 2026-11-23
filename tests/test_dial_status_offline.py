"""
Offline tests for /receptionist and /receptionist-status (no Plivo, no database).
Run: python tests/test_dial_status_offline.py
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routes.v1.inbound import plivo as vp

settings.PLIVO_VALIDATE_SIGNATURE = False
settings.PUBLIC_BASE_URL = "https://example.test"
CALLS = []
vp.finish_receptionist_attempt = lambda call_id, answered: CALLS.append((call_id, answered)) or True
REGISTERED = []
vp.register_caller = lambda call_id, phone: REGISTERED.append((call_id, phone))

app = FastAPI()
app.include_router(vp.router)
client = TestClient(app)


def post(path, **form):
    r = client.post(path, data=form)
    assert r.status_code == 200, r.text
    return r.text, ET.fromstring(r.text)


def t_answered_bot_stays_off():
    CALLS.clear()
    text, root = post("/voice/plivo/receptionist-status?lang=en", DialStatus="completed",
                      DialBLegUUID="b1", DialALegUUID="a1", DialBLegDuration="60", From="919999900001")
    assert root.find("Hangup") is not None and root.find("Stream") is None
    assert CALLS == [("a1", True)]


def t_no_answer_resumes_bot():
    CALLS.clear()
    for status in ("no-answer", "busy", "failed", "timeout"):
        text, root = post("/voice/plivo/receptionist-status?lang=hi", DialStatus=status,
                          DialALegUUID="a1", CallUUID="a1", From="919999900001")
        stream = root.find("Stream")
        assert stream is not None, text
        assert "mode=resume" in stream.get("extraHeaders") and "lang=hi" in stream.get("extraHeaders")
        assert "callerPhone=919999900001" in stream.get("extraHeaders")
        assert stream.text == "wss://example.test/voice/plivo/stream?mode=resume&lang=hi", stream.text
    assert all(c == ("a1", False) for c in CALLS) and len(CALLS) == 4
    assert REGISTERED and REGISTERED[0] == ("a1", "+919999900001"), REGISTERED


def t_answered_then_dropped_fast_resumes():
    CALLS.clear()
    text, root = post("/voice/plivo/receptionist-status?lang=en", DialStatus="completed",
                      DialBLegUUID="b1", DialALegUUID="a1", DialBLegDuration="2", From="919999900001")
    assert root.find("Stream") is not None and CALLS == [("a1", False)]


def t_answered_without_duration_counts_as_answered():
    CALLS.clear()
    text, root = post("/voice/plivo/receptionist-status", DialStatus="completed",
                      DialBLegUUID="b1", DialALegUUID="a1")
    assert root.find("Hangup") is not None and CALLS == [("a1", True)]


def t_receptionist_dial_xml():
    settings.RECEPTIONIST_PHONE = "+911234567890"
    text, root = post("/voice/plivo/receptionist?lang=or", CallUUID="a1")
    dial = root.find("Dial")
    assert dial.get("timeout") == str(settings.TRANSFER_RING_SECONDS)
    assert "receptionist-status?lang=or" in dial.get("action")


def t_no_receptionist_number_resumes():
    settings.RECEPTIONIST_PHONE = ""
    text, root = post("/voice/plivo/receptionist?lang=en", CallUUID="a1", From="919999900001")
    assert root.find("Stream") is not None


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("t_"):
            try:
                fn(); print("PASS", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback; traceback.print_exc(); print("FAIL", name, repr(e))
    sys.exit(1 if failed else 0)
