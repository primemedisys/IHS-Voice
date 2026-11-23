"""
app/voice/filler.py
───────────────────
Chooses the short "one moment" line played while a slow reply is prepared.

The line depends on the situation, so it always sounds respectful and fitting:
    empathy   the caller described pain or an injury
    question  the caller is asking about the clinic (or we are in the closing questions)
    details   everything else (names, dates, days and times)

No filler at all on an emergency or urgent call: that reply must start straight away.
A line is never repeated within one call.
"""

import random

from app.agent.shared.loader import load_replies
from app.agent.shared.rules import classify

_PAIN_WORDS = (
    "pain", "hurt", "injur", "fracture", "broken", "swell", "sprain", "accident", "ache",
    "dard", "दर्द", "चोट", "सूजन", "फ्रैक्चर", "ଯନ୍ତ୍ରଣା", "ବିନ୍ଧ", "ଆଘାତ", "ଫୁଲି", "ଭାଙ୍ଗି",
)
_QUESTION_WORDS = (
    "fee", "cost", "price", "charge", "address", "where", "timing", "open", "insurance", "doctor",
    "kitna", "paisa", "पैसे", "कितना", "फीस", "पता", "ଟଙ୍କା", "କେତେ", "ଫି", "ଠିକଣା", "ସମୟ",
)
_QUESTION_PHASES = ("offer", "anything_else")


def filler_kind(text: str | None, phase: str | None) -> str | None:
    """Return 'empathy', 'question', 'details', or None when no filler should be played."""
    text = (text or "").lower()
    if text and classify(text) != "routine":
        return None
    if any(word in text for word in _PAIN_WORDS):
        return "empathy"
    if phase in _QUESTION_PHASES or any(word in text for word in _QUESTION_WORDS):
        return "question"
    return "details"


def pick_filler(kind: str, lang: str, used: set[str]) -> str | None:
    """A line of this kind in the caller's language that this call has not used yet."""
    value = load_replies().get(f"filler_{kind}", {})
    lines = value.get(lang) or value.get("en") or []
    fresh = [line for line in lines if line not in used]
    if not fresh:
        return None
    line = random.choice(fresh)
    used.add(line)
    return line
