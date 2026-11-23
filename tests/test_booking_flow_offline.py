"""
Offline tests for the end-of-call flow (no LLM, no database, no phone).
Run:  python -m pytest tests/test_booking_flow_offline.py   (or: python tests/test_booking_flow_offline.py)
"""

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import HumanMessage  # type: ignore

from app.agent.booking import nodes as bn
from app.agent.booking import extractors as ex
from app.agent.booking import flow
from app.agent.shared.loader import set_reply_language, validate_data_files

# Monday 23 Nov 2026, 11:00 IST
NOW = datetime(2026, 11, 23, 11, 0, tzinfo=flow.IST)
SAVED: list = []


def setup_module(_=None):
    validate_data_files()
    set_reply_language("en")
    ex._llm_choice = lambda text: "unclear"        # no LLM in tests
    ex._llm_slot = lambda text, today: None
    bn.answer_question = lambda state: "The clinic is at Bhubaneswar."
    bn._save_slot = lambda state, config, slot, text: SAVED.append((slot, text)) or True
    flow.now_ist = lambda: NOW
    # Tests must not depend on the hours currently set in data/clinic_info.json.
    flow.load_hours = lambda: flow.Hours(
        open_days=(0, 1, 2, 3, 4, 5), open_time=flow.time(10, 0), close_time=flow.time(18, 0)
    )


def turn(phase, text, **extra):
    state = {"phase": phase, "messages": [HumanMessage(text)], **extra}
    return bn.booking_flow(state, {"configurable": {"thread_id": "t", "caller_phone": "1"}})


def say(out):
    return out["messages"][0].content


# ---------- hours / validation ----------
def test_hours():
    assert flow.is_open_now(NOW)
    assert not flow.is_open_now(datetime(2026, 11, 22, 11, 0, tzinfo=flow.IST))     # Sunday
    assert not flow.is_open_now(datetime(2026, 11, 23, 18, 0, tzinfo=flow.IST))     # closing time
    assert flow.validate_slot(flow.Slot(day=date(2026, 11, 29)), NOW) == "closed_day"
    assert flow.validate_slot(flow.Slot(day=date(2026, 11, 24), hhmm="19:00"), NOW) == "outside_hours"
    assert flow.validate_slot(flow.Slot(day=date(2026, 11, 22)), NOW) == "past"
    assert flow.validate_slot(flow.Slot(day=date(2026, 11, 23), hhmm="10:30"), NOW) == "past"
    assert flow.validate_slot(flow.Slot(day=date(2026, 11, 24), hhmm="17:00"), NOW) is None


# ---------- choices ----------
def test_choices():
    c = ex.classify_choice
    assert c("yes please") == "transfer"
    assert c("haan connect karo") == "transfer"
    assert c("हाँ") == "transfer"
    assert c("ହଁ") == "transfer"
    assert c("call me later") == "callback"
    assert c("no I don't want to book") == "no_booking"
    assert c("नहीं") == "no_booking"
    assert c("what is your address?") == "question"
    assert c("hmm") == "unclear"
    assert c("") == "unclear"


def test_is_no():
    assert ex.is_no("no thanks")
    assert ex.is_no("नहीं बस")
    assert ex.is_no("that's all")
    assert not ex.is_no("no but what are your timings?")
    assert not ex.is_no("what is the fee")


# ---------- slot reading ----------
def test_slots():
    r = ex.extract_slot("tomorrow at 5 pm", NOW, use_llm=False)
    assert r.slot.day == date(2026, 11, 24) and r.slot.hhmm == "17:00"
    r = ex.extract_slot("tomorrow evening", NOW, use_llm=False)
    assert r.slot.day == date(2026, 11, 24) and r.slot.bucket == "evening" and r.slot.hhmm is None
    r = ex.extract_slot("kal 4 baje", NOW, use_llm=False)
    assert r.slot.day == date(2026, 11, 24) and r.slot.hhmm == "16:00"
    r = ex.extract_slot("शनिवार सुबह", NOW, use_llm=False)
    assert r.slot.day == date(2026, 11, 28) and r.slot.bucket == "morning"
    r = ex.extract_slot("any time is fine", NOW, use_llm=False)
    assert r.flexible
    r = ex.extract_slot("is tomorrow available?", NOW, use_llm=False)
    assert r.slot.day == date(2026, 11, 24) and r.asks_availability
    r = ex.extract_slot("sunday at 11", NOW, use_llm=False)
    assert r.slot.day == date(2026, 11, 29)
    r = ex.extract_slot("କାଲି 7 ବେଳକୁ", NOW, use_llm=False)
    assert r.slot.hhmm == "19:00"
    r = ex.extract_slot("କାଲି ସନ୍ଧ୍ୟା 5", NOW, use_llm=False)
    assert r.slot.hhmm == "17:00"
    r = ex.extract_slot("tomorrow 11 ବେଳେ", NOW, use_llm=False)
    assert r.slot.hhmm == "11:00"


# ---------- the whole ending ----------
def test_transfer_when_open():
    out = turn(flow.PHASE_OFFER, "yes connect me")
    assert out["action"] == "receptionist" and out["phase"] == flow.PHASE_TRANSFERRING


def test_transfer_when_closed():
    flow.now_ist = lambda: datetime(2026, 11, 22, 11, 0, tzinfo=flow.IST)     # Sunday
    try:
        out = turn(flow.PHASE_OFFER, "yes")
        assert out["phase"] == flow.PHASE_SLOT and out.get("action") is None
        assert "closed" in say(out)
    finally:
        flow.now_ist = lambda: NOW


def test_no_booking_then_goodbye():
    out = turn(flow.PHASE_OFFER, "no thanks")
    assert out["phase"] == flow.PHASE_ANYTHING_ELSE and "anything else" in say(out)
    out = turn(flow.PHASE_ANYTHING_ELSE, "no")
    assert out["action"] == "end" and out["phase"] == flow.PHASE_DONE and "Take care" in say(out)


def test_question_then_reask():
    out = turn(flow.PHASE_OFFER, "where is the clinic?")
    assert "Bhubaneswar" in say(out) and "book an appointment" in say(out)
    assert out.get("action") is None and "phase" not in out


def test_slot_happy_path():
    SAVED.clear()
    out = turn(flow.PHASE_SLOT, "tomorrow at 5 pm")
    assert out["phase"] == flow.PHASE_ANYTHING_ELSE and "5 PM" in say(out) and "Tuesday, 24 November" in say(out)
    assert SAVED and SAVED[0][1].startswith("Tuesday")


def test_slot_day_then_time():
    out = turn(flow.PHASE_SLOT, "tomorrow")
    assert "what time" in say(out).lower() and out["slot_date"] == "2026-11-24"
    out2 = turn(flow.PHASE_SLOT, "evening", slot_date=out["slot_date"], slot_attempts=out["slot_attempts"])
    assert out2["phase"] == flow.PHASE_ANYTHING_ELSE


def test_slot_sunday_then_valid():
    out = turn(flow.PHASE_SLOT, "this sunday")
    assert "closed on Sundays" in say(out) and out["slot_attempts"] == 1 and out["slot_date"] is None
    out2 = turn(flow.PHASE_SLOT, "tuesday at 11 am", slot_attempts=1)
    assert out2["phase"] == flow.PHASE_ANYTHING_ELSE


def test_slot_outside_hours():
    out = turn(flow.PHASE_SLOT, "tomorrow at 8 pm")
    assert "10 AM to 6 PM" in say(out) and out["slot_date"] == "2026-11-24" and out["slot_time"] is None


def test_slot_gives_up_after_two():
    SAVED.clear()
    out = turn(flow.PHASE_SLOT, "umm", slot_attempts=1)
    assert out["phase"] == flow.PHASE_ANYTHING_ELSE and SAVED[-1][1] == "flexible"


def test_availability_question_not_confirmed():
    out = turn(flow.PHASE_SLOT, "is it available?")
    assert "cannot check availability" in say(out) and out["slot_attempts"] == 1


def test_silence():
    out = turn(flow.PHASE_OFFER, bn.SILENCE)
    assert out["silences"] == 1 and out.get("action") is None
    out = turn(flow.PHASE_OFFER, bn.SILENCE, silences=1)
    assert out["action"] == "end"
    out = turn(flow.PHASE_ANYTHING_ELSE, bn.SILENCE, silences=1)
    assert out["action"] == "end" and "Take care" in say(out)


def test_anything_else_limit():
    out = turn(flow.PHASE_ANYTHING_ELSE, "what are the fees?", anything_rounds=2)
    assert out["action"] == "end" and "Take care" in say(out)


def test_hindi_odia_lines():
    for lang in ("hi", "or"):
        set_reply_language(lang)
        out = turn(flow.PHASE_SLOT, "kal 4 baje")
        assert out["phase"] == flow.PHASE_ANYTHING_ELSE and "{" not in say(out)
    set_reply_language("en")


def test_clinic_question_in_slot_phase():
    out = turn(flow.PHASE_SLOT, "Clinic କେତେଟାରୁ କେତେଟା ଖୋଲିବ?")
    assert "Bhubaneswar" in say(out) and "Which day" in say(out)
    assert out.get("slot_attempts") is None and "phase" not in out


def test_time_added_after_save_updates_request():
    SAVED.clear()
    out = turn(flow.PHASE_ANYTHING_ELSE, "ସନ୍ଧ୍ୟା ବେଳକୁ 5ଟା", slot_date="2026-11-24")
    assert out["phase"] == flow.PHASE_ANYTHING_ELSE and "Tuesday, 24 November" in say(out)
    assert SAVED and SAVED[-1][0].hhmm == "17:00", SAVED


def test_calendar_dates():
    now = datetime(2026, 11, 22, 14, 31, tzinfo=flow.IST)
    r = ex.extract_slot("ମତେ ଏଇ November 25 ତାରିଖ ସନ୍ଧ୍ୟାବେଳେ 6 ଟା କୁ booking", now, use_llm=False)
    assert r.slot.day == date(2026, 11, 25) and r.slot.hhmm == "18:00", r.slot
    r = ex.extract_slot("25 November at 5 pm", now, use_llm=False)
    assert r.slot.day == date(2026, 11, 25) and r.slot.hhmm == "17:00", r.slot
    r = ex.extract_slot("25th evening", now, use_llm=False)
    assert r.slot.day == date(2026, 11, 25) and r.slot.bucket == "evening", r.slot
    r = ex.extract_slot("at 5 pm", now, use_llm=False)
    assert r.slot.day is None and r.slot.hhmm == "17:00"


def test_suffixes_spoken_numbers_next_next():
    now = datetime(2026, 11, 22, 15, 41, tzinfo=flow.IST)          # Sunday
    r = ex.extract_slot("ମୁଁ ଚାହୁଁଛି ଆମର next next ଶନିବାରେ ସନ୍ଧ୍ୟା ସାତଟା ବେଳକୁ", now, use_llm=False)
    assert r.slot.day == date(2026, 12, 5) and r.slot.hhmm == "19:00", r.slot
    r = ex.extract_slot("next ଶନିବାର ସନ୍ଧ୍ୟା ସାତଟା ବେଳକୁ", now, use_llm=False)
    assert r.slot.day == date(2026, 11, 28) and r.slot.hhmm == "19:00", r.slot
    r = ex.extract_slot("अगले शनिवार शाम सात बजे", now, use_llm=False)
    assert r.slot.day == date(2026, 11, 28) and r.slot.hhmm == "19:00", r.slot
    r = ex.extract_slot("tomorrow at five pm", now, use_llm=False)
    assert r.slot.day == date(2026, 11, 23) and r.slot.hhmm == "17:00", r.slot
    r = ex.extract_slot("କାଲି ସକାଳେ", now, use_llm=False)
    assert r.slot.day == date(2026, 11, 23) and r.slot.bucket == "morning", r.slot


def test_llm_fills_the_missing_part():
    now = datetime(2026, 11, 22, 15, 41, tzinfo=flow.IST)
    ex._llm_slot = lambda text, today: ex.SlotRead(day="2026-11-28", time_24h=None, part_of_day=None)
    try:
        r = ex.extract_slot("evening please", now)          # rules: bucket only, LLM adds the day
        assert r.slot.day == date(2026, 11, 28) and r.slot.bucket == "evening", r.slot
    finally:
        ex._llm_slot = lambda text, today: None


def test_odia_marathi_no_words():
    assert ex.is_no("ନାଇଁ ନାଇଁ ବସ ସେତିକି।")
    assert ex.is_no("नाही नाही नाही")
    assert ex.classify_choice("ନାଇଁ") == "no_booking"


if __name__ == "__main__":
    setup_module()
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback; traceback.print_exc()
                print("FAIL", name, e)
    sys.exit(1 if failed else 0)
