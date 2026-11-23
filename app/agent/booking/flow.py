"""
app/agent/booking/flow.py
─────────────────
Pure-Python rules for the end of an inbound call. No LLM, no database, no network.

What lives here:
    - clinic hours (from data/clinic_info.json -> "_hours")
    - "is the clinic open right now?"
    - slot validation (closed day, outside hours, date in the past)
    - turning a slot into a short spoken phrase (en / hi / or)

Call phases (stored in CallState["phase"]):
    intake        LLM collects the problem details
    offer         bot asked: book an appointment / connect to the assistant?
    transferring  the call was handed to the doctor's assistant
    slot          bot asks for the day and time of the appointment
    anything_else bot asked: is there anything else I can help with?
    done          goodbye was said, the call is ending
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from app.agent.shared.loader import load_clinic

# India Standard Time has no daylight saving, so a fixed offset is exact.
IST = timezone(timedelta(hours=5, minutes=30))

PHASE_INTAKE = "intake"
PHASE_OFFER = "offer"
PHASE_TRANSFERRING = "transferring"
PHASE_SLOT = "slot"
PHASE_ANYTHING_ELSE = "anything_else"
PHASE_DONE = "done"

BOOKING_PHASES = (PHASE_OFFER, PHASE_SLOT, PHASE_ANYTHING_ELSE)

MAX_OFFER_ATTEMPTS = 2     # unclear answers to the booking offer
MAX_SLOT_ATTEMPTS = 2      # re-asks for day/time before saving "flexible"
MAX_ANYTHING_ROUNDS = 3    # clinic questions answered after the booking is done
MAX_SILENCES = 2           # first silence = re-ask, second = default close
MAX_INTAKE_TURNS = 8       # safety net if the caller never gives every detail

_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Time-of-day words -> (start hour, end hour) inside clinic hours.
BUCKETS = {
    "morning": (10, 12),
    "afternoon": (12, 16),
    "evening": (16, 18),
}


def now_ist() -> datetime:
    return datetime.now(IST)


# ==================================================
# CLINIC HOURS
# ==================================================

@dataclass(frozen=True)
class Hours:
    open_days: tuple[int, ...]      # Monday = 0 ... Sunday = 6
    open_time: time
    close_time: time


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def load_hours() -> Hours:
    """Clinic hours from clinic_info.json. Defaults to Mon-Sat 10:00-18:00."""
    raw = load_clinic().get("_hours") or {}
    days = raw.get("open_days") or list(_DAY_KEYS[:6])
    return Hours(
        open_days=tuple(_DAY_KEYS.index(d) for d in days if d in _DAY_KEYS),
        open_time=_parse_hhmm(raw.get("open_time", "10:00")),
        close_time=_parse_hhmm(raw.get("close_time", "18:00")),
    )


def is_open_now(now: datetime | None = None) -> bool:
    """True when the doctor's assistant could be at the desk right now."""
    now = (now or now_ist()).astimezone(IST)
    hours = load_hours()
    return now.weekday() in hours.open_days and hours.open_time <= now.time() < hours.close_time


# ==================================================
# SLOTS
# ==================================================

@dataclass
class Slot:
    day: date | None = None          # the calendar day the caller asked for
    hhmm: str | None = None          # "17:00" when an exact time was said
    bucket: str | None = None        # "morning" / "afternoon" / "evening"
    flexible: bool = False           # "any time works"

    @property
    def has_day(self) -> bool:
        return self.day is not None

    @property
    def has_time(self) -> bool:
        return self.hhmm is not None or self.bucket is not None


def validate_slot(slot: Slot, now: datetime | None = None) -> str | None:
    """Return None when the slot is fine, else the reason it is not:
    "closed_day", "outside_hours" or "past"."""
    now = (now or now_ist()).astimezone(IST)
    hours = load_hours()

    if slot.day is not None:
        if slot.day < now.date():
            return "past"
        if slot.day.weekday() not in hours.open_days:
            return "closed_day"

    if slot.hhmm is not None:
        asked = _parse_hhmm(slot.hhmm)
        if not (hours.open_time <= asked < hours.close_time):
            return "outside_hours"
        if slot.day == now.date() and asked <= now.time():
            return "past"

    return None


# ==================================================
# SPOKEN SLOT TEXT
# ==================================================

_WEEKDAYS = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "hi": ["सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"],
    "or": ["ସୋମବାର", "ମଙ୍ଗଳବାର", "ବୁଧବାର", "ଗୁରୁବାର", "ଶୁକ୍ରବାର", "ଶନିବାର", "ରବିବାର"],
}
_MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"],
    "hi": ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई",
           "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"],
    "or": ["ଜାନୁଆରୀ", "ଫେବୃଆରୀ", "ମାର୍ଚ୍ଚ", "ଅପ୍ରେଲ", "ମଇ", "ଜୁନ", "ଜୁଲାଇ",
           "ଅଗଷ୍ଟ", "ସେପ୍ଟେମ୍ବର", "ଅକ୍ଟୋବର", "ନଭେମ୍ବର", "ଡିସେମ୍ବର"],
}
_BUCKET_WORDS = {
    "en": {"morning": "in the morning", "afternoon": "in the afternoon", "evening": "in the evening"},
    "hi": {"morning": "सुबह", "afternoon": "दोपहर में", "evening": "शाम को"},
    "or": {"morning": "ସକାଳେ", "afternoon": "ଅପରାହ୍ନରେ", "evening": "ସନ୍ଧ୍ୟାରେ"},
}


def _clock_text(hhmm: str) -> str:
    hour, minute = (int(p) for p in hhmm.split(":"))
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12} {suffix}" if minute == 0 else f"{hour12}:{minute:02d} {suffix}"


def describe_slot(slot: Slot, lang: str = "en") -> str:
    """A short phrase for the confirmation line, e.g. "Monday, 23 November, 5 PM"."""
    lang = lang if lang in _WEEKDAYS else "en"
    parts: list[str] = []
    if slot.day is not None:
        parts.append(
            f"{_WEEKDAYS[lang][slot.day.weekday()]}, {slot.day.day} {_MONTHS[lang][slot.day.month - 1]}"
        )
    if slot.hhmm is not None:
        parts.append(_clock_text(slot.hhmm))
    elif slot.bucket in BUCKETS:
        parts.append(_BUCKET_WORDS[lang][slot.bucket])
    return ", ".join(parts)
