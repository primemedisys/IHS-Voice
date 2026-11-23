"""Offline checks for doctor_answers.json. Run: python tests/test_doctor_answers_offline.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.intake.prompts import build_system_prompt  # noqa: E402
from app.agent.shared import loader  # noqa: E402


def test_file_valid():
    loader.validate_data_files()
    data = loader.load_doctor_answers()
    assert data["faqs"] and data["procedures"] and data["injury_guidance"] and data["fallback"]


def test_moved_out_of_clinic_info():
    clinic = loader.load_clinic()
    assert "faqs" not in clinic and "procedures" not in clinic
    assert clinic["_hours"] and clinic["address"]          # booking hours and address stay


def test_prompt_contains_doctor_answers():
    prompt = build_system_prompt({})
    data = loader.load_doctor_answers()
    assert data["injury_guidance"][0]["answer"] in prompt
    assert data["fallback"] in prompt
    assert "_note" not in prompt.split("DOCTOR-APPROVED ANSWERS")[1]


def test_broken_file_fails_at_startup():
    for bad in ({"fallback": "x", "faqs": [{"q": "only a question"}]}, {"faqs": []}):
        try:
            loader._check_doctor_answers(bad)
        except RuntimeError:
            continue
        raise AssertionError("a broken doctor_answers.json was accepted")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
