"""
app/agent/shared/loader.py

Loads the doctor-approved JSON files from /data once and keeps them in memory.
To change what the bot says, edit the JSON and restart. No code changes.
A missing or broken file raises a clear error at startup.
"""

import contextvars
import json
import random
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from app.config import settings

# ==================================================
# SETTINGS AND REQUIRED KEYS
# ==================================================

DATA_DIR = Path(__file__).resolve().parents[3] / "data"

# The caller's language for the current turn.
_REPLY_LANGUAGE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "reply_language", default=""
)

# Replies used by the code. Missing keys should be caught at startup.
REQUIRED_REPLIES = (
    "greeting",
    "ask_identity",
    "ask_name",
    "ask_dob",
    "verified",
    "verify_retry",
    "verify_failed",
    "not_understood",
    "medical_advice_decline",
    "emergency_transfer",
    "transfer_failed",
    "error",
    "filler_empathy",
    "filler_details",
    "filler_question",
    "greeting_existing",
    "verify_ask_name",
    "verify_ask_dob",
    "identity_refused",
    "identity_failed_offer",
    "someone_else_answering",
    "new_ask_name",
    "new_ask_dob",
    "receptionist_connecting",
    "offer_booking",
    "offer_unclear",
    "reask_offer",
    "no_booking",
    "clinic_closed_now",
    "assistant_unavailable",
    "ask_slot",
    "ask_slot_again",
    "ask_slot_day",
    "ask_slot_time",
    "slot_closed_day",
    "slot_outside_hours",
    "slot_past",
    "slot_availability",
    "slot_saved",
    "slot_saved_flexible",
    "close_team_will_call",
    "ask_anything_else",
    "goodbye_final",
)


# ==================================================
# REPLY LANGUAGE
# ==================================================

def set_reply_language(lang: str) -> None:
    """Remember the caller's language for this turn ('en', 'hi' or 'or')."""
    _REPLY_LANGUAGE.set(lang)


def current_language() -> str:
    """The caller's language for this turn; falls back to DEFAULT_LANGUAGE."""
    return _REPLY_LANGUAGE.get() or settings.DEFAULT_LANGUAGE


def iter_variants(value) -> Iterator[str]:
    """Every text variant inside a fixed reply."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        yield from (v for v in value if isinstance(v, str))
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_variants(item)


# ==================================================
# READING THE DATA FILES
# ==================================================

def _read(name: str) -> dict:
    """Read one JSON file from /data."""
    path = DATA_DIR / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise RuntimeError(f"Data file missing: {path}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Data file '{name}' is not valid JSON: {e}") from e


@lru_cache
def load_clinic() -> dict:
    """Clinic info: address, timings, fees, procedures, FAQs."""
    return _read("clinic_info.json")


@lru_cache
def load_doctor_answers() -> dict:
    """What the doctor has approved the bot to say about injuries, procedures and common questions."""
    return _read("doctor_answers.json")


@lru_cache
def load_rules() -> dict:
    """Emergency and urgent phrases approved by the doctor."""
    return _read("emergency_rules.json")


@lru_cache
def load_replies() -> dict:
    """Fixed lines the bot says."""
    return _read("fixed_replies.json")


# ==================================================
# FIXED REPLIES
# ==================================================

def pick_reply(key: str, lang: str | None = None) -> str:
    """Return a fixed reply in the caller's language, or English as fallback."""
    value = load_replies()[key]
    if isinstance(value, dict):
        value = value.get(lang or current_language()) or value.get("en") or next(iter(value.values()))
    return random.choice(value) if isinstance(value, list) else value


# ==================================================
# STARTUP CHECKS
# ==================================================

def _check_doctor_answers(data: dict) -> None:
    """Fail at startup (not mid-call) if the doctor's file is edited into a broken shape."""
    problems = []
    for item in data.get("injury_guidance", []):
        if not (isinstance(item, dict) and item.get("topic") and item.get("answer")):
            problems.append(f"injury_guidance entry needs topic and answer: {item!r}")
    for item in data.get("faqs", []):
        if not (isinstance(item, dict) and item.get("q") and item.get("a")):
            problems.append(f"faqs entry needs q and a: {item!r}")
    for item in data.get("procedures", []):
        if not (isinstance(item, dict) and item.get("name")):
            problems.append(f"procedures entry needs a name: {item!r}")
    if not data.get("fallback"):
        problems.append("fallback is missing")
    if problems:
        raise RuntimeError("doctor_answers.json problems: " + "; ".join(problems))


def validate_data_files() -> None:
    """Load the data files and check the required reply keys exist."""
    load_clinic()
    load_rules()
    _check_doctor_answers(load_doctor_answers())
    replies = load_replies()
    missing = [key for key in REQUIRED_REPLIES if not replies.get(key)]
    if missing:
        raise RuntimeError(f"fixed_replies.json is missing or has empty keys: {missing}")