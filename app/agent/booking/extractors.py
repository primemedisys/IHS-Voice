"""
app/agent/booking/extractors.py
───────────────────────
Turn the caller's words into a small decision. Keywords first (instant, free);
the LLM is used only when the keywords cannot tell.

    classify_choice(text)  -> answer to "book an appointment / connect you now?"
    is_no(text)            -> "no, nothing else"
    extract_slot(text)     -> day + time the caller wants (never confirms availability)

Every function is safe: on any failure it returns the "unclear" answer and the
flow asks again; nothing here raises into the call.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from pydantic import BaseModel, Field

from app.agent.booking import flow
from app.agent.booking.flow import BUCKETS, IST, Slot

logger = logging.getLogger(__name__)

# ==================================================
# TEXT HELPERS
# ==================================================

_PUNCT = re.compile(r"[?:!\"#$%&()*+,./;<=>@\[\\\]^_`{|}~\-।॥]")


def _norm(text: str) -> str:
    """Lower-case and drop punctuation. Not `\\w`: it would strip Hindi/Odia vowel signs."""
    return re.sub(r"\s+", " ", _PUNCT.sub(" ", (text or "").lower())).strip()


def _words(text: str) -> set[str]:
    return set(_norm(text).split())


def _has_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    low = _norm(text)
    padded = f" {low} "
    return any(f" {p} " in padded for p in phrases)


def _hit(text: str, names: tuple[str, ...]) -> bool:
    """True if one of `names` is in the text. Hindi/Odia words also match with a grammar ending
    (ଶନିବାରେ, ଶନିବାରକୁ, सोमवार को...), which exact word matching would miss."""
    padded = f" {_norm(text)} "
    tokens = padded.split()
    for name in names:
        if " " in name:
            if f" {name} " in padded:
                return True
            continue
        for token in tokens:
            if token == name:
                return True
            if len(name) >= 4 and not name.isascii() and token.startswith(name) and len(token) - len(name) <= 6:
                return True
    return False


_NEXT_WORDS = ("next", "अगले", "अगला", "ଆସନ୍ତା", "ଆସନ୍ତାକାଲି")

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पाँच": 5, "पांच": 5, "छह": 6, "छः": 6, "सात": 7,
    "आठ": 8, "नौ": 9, "दस": 10, "ग्यारह": 11, "बारह": 12,
    "ଏକ": 1, "ଦୁଇ": 2, "ତିନି": 3, "ଚାରି": 4, "ପାଞ୍ଚ": 5, "ଛଅ": 6, "ସାତ": 7, "ଆଠ": 8,
    "ନଅ": 9, "ଦଶ": 10, "ଏଗାର": 11, "ବାର": 12,
}
_CLOCK_SUFFIXES = ("ଟାରେ", "ଟାକୁ", "ଟା")
_CLOCK_FOLLOWERS = {"am", "pm", "baje", "बजे", "ବଜେ", "oclock", "o'clock"}


def _numbers_to_digits(text: str) -> str:
    """Spoken hours as digits: "ସାତଟା" -> "7ଟା", "सात बजे" -> "7 बजे", "seven pm" -> "7 pm".
    A number word is only converted when it is clearly a clock hour."""
    tokens = (text or "").split()
    out = []
    for i, token in enumerate(tokens):
        bare = token.strip(".,!?।").lower()
        nxt = tokens[i + 1].strip(".,!?।").lower() if i + 1 < len(tokens) else ""
        prev = tokens[i - 1].strip(".,!?।").lower() if i > 0 else ""
        converted = None
        for suffix in _CLOCK_SUFFIXES:
            if bare.endswith(suffix) and bare[: -len(suffix)] in _NUMBER_WORDS:
                converted = f"{_NUMBER_WORDS[bare[: -len(suffix)]]}{suffix}"
                break
        if converted is None and bare in _NUMBER_WORDS and (nxt in _CLOCK_FOLLOWERS or (bare.isascii() and prev == "at")):
            converted = str(_NUMBER_WORDS[bare])
        out.append(converted or token)
    return " ".join(out)


# ==================================================
# YES / NO / CHOICE
# ==================================================

YES_WORDS = {
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "please", "haan", "han", "ha", "ji",
    "हाँ", "हां", "हा", "जी", "ठीक", "ज़रूर", "जरूर", "कर", "ହଁ", "ହୋ", "ଠିକ୍", "ଜି", "ହଉ", "ହୁଁ", "ଆଜ୍ଞା",
}
NO_WORDS = {
    "no", "nope", "nah", "nahi", "nahin", "na", "नहीं", "नही", "ना", "नहि", "नाही",
    "ନା", "ନାହିଁ", "ନାଇଁ", "ନାହି", "ନହିଁ", "ନୁହେଁ", "ନାଁ",
}
CALLBACK_PHRASES = (
    "call me later", "call me back", "call back", "callback", "later", "tell them to call",
    "team call", "they call", "let them call", "have them call", "you call me",
    "बाद में", "बाद मे", "कॉल कर", "फोन कर", "वापस कॉल", "ଟିମ୍ କଲ୍", "ପରେ", "ଫୋନ୍ କରନ୍ତୁ",
)
TRANSFER_PHRASES = (
    "connect me", "connect", "transfer", "talk now", "speak now", "right now", "now",
    "जोड़", "जोड़िए", "अभी बात", "अभी", "ଯୋଡ଼", "ଏବେ",
)
NO_BOOKING_PHRASES = (
    "no booking", "not now", "dont want", "don't want", "do not want", "no need",
    "not interested", "just information", "just enquiry", "just inquiry", "only information",
    "नहीं चाहिए", "ज़रूरत नहीं", "जरूरत नहीं", "नहीं करना", "ଦରକାର ନାହିଁ", "ଚାହୁଁନାହିଁ",
)
QUESTION_WORDS = {
    "address", "location", "where", "timing", "timings", "time", "hours", "fee", "fees",
    "cost", "price", "charge", "charges", "open", "close", "closed", "parking", "doctor",
    "surgery", "insurance", "how", "what", "when", "which", "who", "why",
    "पता", "कहाँ", "कहां", "फीस", "समय", "टाइम", "खुला", "बंद", "कितना", "क्या", "कब", "कौन",
    "खुलता", "खुलेगा", "खुलती", "कितने", "कितने बजे",
    "ଠିକଣା", "କେଉଁଠି", "ଫି", "ସମୟ", "କେତେ", "କେତେଟା", "କେତେଟାରୁ", "କଣ", "କେବେ", "ଖୋଲିବ", "ଖୋଲେ", "ଖୋଲା", "ବନ୍ଦ",
}


def classify_choice(text: str) -> str:
    """Answer to the booking offer.

    Returns one of: "transfer" (connect me now), "callback" (team calls me / I'll give a
    time), "no_booking", "question" (asks about the clinic), "unclear".
    """
    try:
        low = _norm(text)
        if not low:
            return "unclear"
        words = set(low.split())

        if _has_phrase(low, CALLBACK_PHRASES):
            return "callback"
        if _has_phrase(low, NO_BOOKING_PHRASES):
            return "no_booking"
        # A question about the clinic, unless it also contains a clear yes/no.
        if ("?" in (text or "") or words & QUESTION_WORDS) and not (words & YES_WORDS) and not (words & NO_WORDS):
            return "question"
        if words & NO_WORDS:
            return "no_booking"
        if _has_phrase(low, TRANSFER_PHRASES) or words & YES_WORDS:
            return "transfer"
        return _llm_choice(text)
    except Exception:
        logger.exception("❌ [Extract] classify_choice failed")
        return "unclear"


ANYTHING_ELSE_NO = (
    "no", "nope", "nothing", "nothing else", "that is all", "that's all", "thats all", "all good",
    "no thanks", "no thank you", "im good", "i'm good", "that will be all", "bas", "bas itna",
    "nahi", "nahin", "नहीं", "नही", "नाही", "बस", "बस इतना", "कुछ नहीं",
    "ବସ୍", "ବସ", "ସେତିକି", "ନାହିଁ", "ନାଇଁ", "ନା", "ନୁହେଁ", "କିଛି ନାହିଁ", "ଆଉ କିଛି ନାହିଁ",
)


def is_no(text: str) -> bool:
    """True when the caller says there is nothing else they need."""
    low = _norm(text)
    if not low:
        return False
    if low in ANYTHING_ELSE_NO or _has_phrase(low, ANYTHING_ELSE_NO):
        # "no, but what are your timings?" is still a question.
        return not (set(low.split()) & QUESTION_WORDS) and "?" not in (text or "")
    return False


# ==================================================
# SLOT: DAY + TIME
# ==================================================

_WEEKDAY_NAMES = {
    0: ("monday", "mon", "सोमवार", "ସୋମବାର", "somvar"),
    1: ("tuesday", "tue", "tues", "मंगलवार", "ମଙ୍ଗଳବାର", "mangalvar"),
    2: ("wednesday", "wed", "बुधवार", "ବୁଧବାର", "budhvar"),
    3: ("thursday", "thu", "thurs", "गुरुवार", "बृहस्पतिवार", "ଗୁରୁବାର", "guruvar"),
    4: ("friday", "fri", "शुक्रवार", "ଶୁକ୍ରବାର", "shukravar"),
    5: ("saturday", "sat", "शनिवार", "ଶନିବାର", "shanivar"),
    6: ("sunday", "sun", "रविवार", "ରବିବାର", "ravivar", "itwar"),
}
_TODAY = ("today", "aaj", "आज", "ଆଜି", "tonight")
_TOMORROW = ("tomorrow", "tmrw", "tomorow", "kal", "कल", "ଆସନ୍ତାକାଲି", "କାଲି")
_DAY_AFTER = ("day after tomorrow", "parso", "परसों", "परसो", "ପରଶୁ")
_FLEXIBLE = (
    "any time", "anytime", "any day", "whenever", "whichever", "no preference", "you decide",
    "kabhi bhi", "कभी भी", "कोई भी", "जब भी", "ଯେବେ ବି", "ଯେକୌଣସି",
)
_REFUSE = (
    "dont know", "don't know", "not sure", "i will call", "i'll call", "call you later",
    "पता नहीं", "मैं कॉल करूँगा", "मैं फोन करूँगा", "ଜାଣେନାହିଁ",
)
_AVAILABILITY = ("available", "availability", "free", "slot", "उपलब्ध", "खाली", "ଉପଲବ୍ଧ", "ଖାଲି")
_BUCKET_WORDS = {
    "morning": ("morning", "subah", "सुबह", "सवेरे", "ସକାଳ"),
    "afternoon": ("afternoon", "noon", "dopahar", "दोपहर", "ଅପରାହ୍ନ", "ମଧ୍ୟାହ୍ନ"),
    "evening": ("evening", "shaam", "sham", "शाम", "ସନ୍ଧ୍ୟା"),
    "night": ("night", "raat", "रात", "ରାତି"),
}
_CLOCK_RE = re.compile(
    r"(?<![\d:.])(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|baje|बजे|ବଜେ|ଟାରେ|ଟାକୁ|ଟା|ବେଳକୁ|ବେଳେ|ବେଳ)?(?!\d)",
    re.IGNORECASE,
)
_DAY_NUMBER_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\b")


class SlotRead(BaseModel):
    """What the caller said about the appointment day and time."""

    day: str | None = Field(None, description="The calendar date as YYYY-MM-DD, or null if no day was said.")
    time_24h: str | None = Field(None, description="Exact time as HH:MM in 24-hour clock, or null.")
    part_of_day: str | None = Field(None, description="morning, afternoon, evening or night, or null.")
    flexible: bool = Field(False, description="True if the caller said any day or any time is fine.")
    asks_availability: bool = Field(False, description="True if the caller is asking whether a slot is free.")
    asks_clinic_question: bool = Field(
        False, description="True if the caller asks about the clinic itself (hours, address, fees) and gave no day or time.",
    )
    refused: bool = Field(False, description="True if the caller will not or cannot give a day or time.")


_MONTH_WORDS = {
    1: ("january", "jan", "जनवरी", "ଜାନୁଆରୀ"),
    2: ("february", "feb", "फरवरी", "फ़रवरी", "ଫେବୃଆରୀ"),
    3: ("march", "mar", "मार्च", "ମାର୍ଚ୍ଚ"),
    4: ("april", "apr", "अप्रैल", "ଅପ୍ରେଲ"),
    5: ("may", "मई", "ମଇ"),
    6: ("june", "jun", "जून", "ଜୁନ"),
    7: ("july", "jul", "जुलाई", "ଜୁଲାଇ"),
    8: ("august", "aug", "अगस्त", "ଅଗଷ୍ଟ"),
    9: ("september", "sep", "sept", "सितंबर", "सितम्बर", "ସେପ୍ଟେମ୍ବର"),
    10: ("october", "oct", "अक्टूबर", "ଅକ୍ଟୋବର"),
    11: ("november", "nov", "नवंबर", "नवम्बर", "ନଭେମ୍ବର"),
    12: ("december", "dec", "दिसंबर", "दिसम्बर", "ଡିସେମ୍ବର"),
}
_DAY_WORDS = ("तारीख", "तारीख़", "ତାରିଖ", "date", "th", "st", "nd", "rd")


def _date_from_numbers(text: str, today: date) -> date | None:
    """A calendar date said as "November 25", "25 November", "25th" or "25 तारीख".
    Numbers that belong to a clock time (5 pm, 6ଟା, at 5) are ignored."""
    low = (text or "").lower()
    month = None
    for number, names in _MONTH_WORDS.items():
        if _hit(text, names):
            month = number
            break

    day = None
    for match in re.finditer(r"(?<![\d:.])(\d{1,2})(?:st|nd|rd|th)?(?![\d:])", low):
        value = int(match.group(1))
        after = low[match.end():match.end() + 8].lstrip()
        before = low[max(0, match.start() - 8):match.start()].rstrip()
        if re.match(r"(am|pm|a\.m|p\.m|baje|बजे|ବଜେ|ଟା)", after) or re.search(r"(at|around|by|about)$", before):
            continue
        if 1 <= value <= 31 and (month or after.startswith(_DAY_WORDS) or re.match(r"(st|nd|rd|th)", low[match.end() - 2:match.end()])):
            day = value
            break
    if day is None:
        return None

    year = today.year
    month = month or today.month
    try:
        found = date(year, month, day)
        if found < today:
            found = date(year + 1, month, day) if month != today.month or day < today.day else found
        return found
    except ValueError:
        return None


def _day_from_words(text: str, today: date) -> date | None:
    """Read the day from words (today, tomorrow, weekday names, "next next", "25th") as a date."""
    explicit = _date_from_numbers(text, today)
    if explicit:
        return explicit
    if _hit(text, _DAY_AFTER):
        return today + timedelta(days=2)
    if _hit(text, _TOMORROW):
        return today + timedelta(days=1)
    if _hit(text, _TODAY):
        return today
    for weekday, names in _WEEKDAY_NAMES.items():
        if _hit(text, names):
            ahead = (weekday - today.weekday()) % 7
            # Saying "Monday" on a Monday means next week unless "today" was said.
            found = today + timedelta(days=ahead or 7)
            # "next next Saturday" / "after next": the week after the coming one.
            if sum(1 for word in _norm(text).split() if word in _NEXT_WORDS) >= 2:
                found += timedelta(days=7)
            return found
    return None


def _time_from_words(text: str) -> tuple[str | None, str | None]:
    """Return (hh:mm or None, bucket or None). Hours 1-9 without am/pm mean the afternoon
    because the clinic is only open 10:00-18:00."""
    low = _norm(text)
    bucket = None
    for name, words in _BUCKET_WORDS.items():
        if _hit(text, words):
            bucket = name
            break

    # Only read digits as a clock time when they are clearly a time: a marker (pm / baje),
    # a colon, or a bare number together with a "at / around / by" style word.
    for match in _CLOCK_RE.finditer(text.lower()):
        hour, minute, marker = int(match.group(1)), int(match.group(2) or 0), (match.group(3) or "")
        if hour > 23 or minute > 59:
            continue
        explicit = bool(marker) or match.group(2) is not None
        before = text.lower()[:match.start()].rstrip()
        if bucket and before.endswith(tuple(w for ws in _BUCKET_WORDS.values() for w in ws)):
            explicit = True
        if not explicit and not re.search(r"\b(at|around|by|about|को|पर|ବେଳେ)\b", low):
            continue
        marker = marker.lower().replace(".", "")
        if marker == "pm" and hour < 12:
            hour += 12
        elif marker == "am" and hour == 12:
            hour = 0
        elif marker in ("", "baje", "बजे", "ବଜେ", "ଟା", "ଟାରେ", "ଟାକୁ", "ବେଳକୁ", "ବେଳେ", "ବେଳ"):
            if bucket in ("afternoon", "evening", "night") and hour < 12:
                hour += 12
            elif bucket is None and 1 <= hour <= 9:
                hour += 12
        return f"{hour:02d}:{minute:02d}", (bucket if bucket in BUCKETS else None)

    return None, (bucket if bucket in BUCKETS else ("late" if bucket == "night" else None))


def _rule_slot(text: str, today: date) -> tuple[Slot, bool, bool, bool]:
    """Keyword read. Returns (slot, flexible, asks_availability, refused)."""
    text = _numbers_to_digits(text)
    low = _norm(text)
    slot = Slot(day=_day_from_words(text, today))
    slot.hhmm, bucket = _time_from_words(text)
    if bucket == "late":            # "night" is outside clinic hours
        slot.hhmm = "21:00"
        bucket = None
    slot.bucket = bucket
    flexible = _has_phrase(low, _FLEXIBLE)
    refused = _has_phrase(low, _REFUSE)
    asks = _has_phrase(low, _AVAILABILITY)
    return slot, flexible, asks, refused


def _llm_slot(text: str, today: date) -> SlotRead | None:
    """Ask the LLM to read the day and time when the keyword rules found nothing or only half."""
    from langchain_core.messages import HumanMessage, SystemMessage  # type: ignore

    from app.agent.llm import get_llm

    system = (
        "You read what a caller said about WHEN they want a clinic appointment. "
        f"Today is {today.isoformat()} ({today.strftime('%A')}) in India. "
        "Resolve relative days (tomorrow, day after, next Monday) to a calendar date. "
        "The caller may speak English, Hindi or Odia. Never invent a day or time that was not said. "
        "If a field was not said, leave it null."
    )
    try:
        result = get_llm().with_structured_output(SlotRead).invoke(
            [SystemMessage(system), HumanMessage(text)]
        )
        return result if isinstance(result, SlotRead) else None
    except Exception:
        logger.exception("❌ [Extract] slot LLM failed")
        return None


@dataclass
class SlotResult:
    slot: Slot
    flexible: bool = False
    asks_availability: bool = False
    refused: bool = False
    clinic_question: bool = False       # asked about the clinic (hours, address...) and gave no day/time


def extract_slot(text: str, now: datetime | None = None, use_llm: bool = True) -> SlotResult:
    """Read the day/time the caller wants. Keywords first; the LLM only when they found nothing."""
    today = (now or flow.now_ist()).astimezone(IST).date()
    try:
        slot, flexible, asks, refused = _rule_slot(text, today)
        if (slot.has_day != slot.has_time) and not (flexible or refused or asks) and use_llm:
            # Only the day or only the time was understood: the LLM fills the missing part so the
            # caller is not asked again for something they already said.
            read = _llm_slot(text, today)
            if read:
                if not slot.has_day and read.day:
                    try:
                        slot.day = date.fromisoformat(read.day)
                    except ValueError:
                        pass
                if not slot.has_time:
                    if read.time_24h and re.fullmatch(r"\d{2}:\d{2}", read.time_24h):
                        slot.hhmm = read.time_24h
                    elif read.part_of_day in BUCKETS:
                        slot.bucket = read.part_of_day
        if slot.has_day or slot.has_time or flexible or refused:
            slot.flexible = flexible
            return SlotResult(slot=slot, flexible=flexible, asks_availability=asks, refused=refused)

        if not asks and (_words(text) & QUESTION_WORDS or "?" in (text or "")):
            return SlotResult(slot=Slot(), clinic_question=True)

        if use_llm and (text or "").strip():
            read = _llm_slot(text, today)
            if read is not None:
                day = None
                if read.day:
                    try:
                        day = date.fromisoformat(read.day)
                    except ValueError:
                        day = None
                bucket = read.part_of_day if read.part_of_day in BUCKETS else None
                hhmm = read.time_24h if read.time_24h and re.fullmatch(r"\d{2}:\d{2}", read.time_24h) else None
                if read.part_of_day == "night" and hhmm is None:
                    hhmm = "21:00"
                slot = Slot(day=day, hhmm=hhmm, bucket=bucket, flexible=read.flexible)
                return SlotResult(
                    slot=slot,
                    flexible=read.flexible,
                    asks_availability=asks or read.asks_availability,
                    refused=read.refused,
                    clinic_question=read.asks_clinic_question and not (day or hhmm or bucket),
                )
        return SlotResult(slot=Slot(), asks_availability=asks)
    except Exception:
        logger.exception("❌ [Extract] extract_slot failed")
        return SlotResult(slot=Slot())


# ==================================================
# LLM FALLBACK FOR THE BOOKING OFFER
# ==================================================

class ChoiceRead(BaseModel):
    choice: str = Field(
        description="transfer = wants to talk to the assistant now; callback = wants the team to call "
        "later; no_booking = does not want to book; question = asks about the clinic; unclear.",
    )


def _llm_choice(text: str) -> str:
    """Ask the LLM which offer answer the caller meant, when the keyword rules could not tell."""
    from langchain_core.messages import HumanMessage, SystemMessage  # type: ignore

    from app.agent.llm import get_llm

    system = (
        "A clinic phone bot asked the caller: 'Would you like to book an appointment? I can connect "
        "you to the doctor's assistant now.' Classify the caller's answer (English, Hindi or Odia). "
        "Answer exactly one of: transfer, callback, no_booking, question, unclear."
    )
    try:
        result = get_llm().with_structured_output(ChoiceRead).invoke(
            [SystemMessage(system), HumanMessage(text)]
        )
        choice = (getattr(result, "choice", "") or "").strip().lower()
        return choice if choice in ("transfer", "callback", "no_booking", "question") else "unclear"
    except Exception:
        logger.exception("❌ [Extract] choice LLM failed")
        return "unclear"
