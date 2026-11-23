"""
app/services/outbound/appointments.py
───────────────────────────────────
Everything about reminder appointments: importing the Excel sheet, choosing who to call,
and saving what happened. Like the other services, these are plain blocking functions;
async code calls them with asyncio.to_thread().
"""

import io
import logging
from datetime import date, datetime, time, timedelta, timezone

from openpyxl import load_workbook
from sqlalchemy import func, or_, select

from app.config import settings
from app.database import get_session
from app.models.appointment import Appointment
from app.services.shared import patients as patient_service

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))  # India has no daylight saving time

# Accepted column headings in the Excel sheet (case-insensitive).
_COLUMNS = {
    "name": {"name", "patient name", "patient", "full name"},
    "phone": {"phone", "mobile", "phone number", "mobile number", "contact"},
    "date": {"date", "appointment date"},
    "time": {"time", "appointment time"},
    "language": {"language", "lang"},            # optional: en / hi / or
    "consent": {"consent", "reminder consent"},  # optional: "no" skips the row
}
_REQUIRED = ("name", "phone", "date", "time")
_DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d %b %Y", "%d %B %Y")
_TIME_FORMATS = ("%I:%M %p", "%I:%M%p", "%H:%M", "%H:%M:%S", "%I %p")
_LANGUAGES = {"en": "en", "english": "en", "hi": "hi", "hindi": "hi", "or": "or", "odia": "or", "oriya": "or"}
_NO = {"no", "n", "false", "0", "declined"}

_STATUS_BY_OUTCOME = {"confirmed": "confirmed", "reschedule": "reschedule", "cancel": "cancelled"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ==================================================
# EXCEL IMPORT
# ==================================================


def _parse_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_time(value) -> time | None:
    if isinstance(value, datetime):
        return value.time()
    if isinstance(value, time):
        return value
    text = str(value or "").strip().upper()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _clean_phone(value) -> str | None:
    """'98765 43210', 9876543210 (a number cell) or '+91...' -> '+919876543210'. None if it is not usable."""
    if value is None:
        return None
    if isinstance(value, float):
        value = int(value)
    raw = str(value).strip()
    if not raw:
        return None
    phone = patient_service.normalize_phone(raw)
    return phone if phone.startswith("+") and phone[1:].isdigit() and len(phone) >= 11 else None


def import_excel(content: bytes) -> dict:
    """Read an .xlsx sheet and save its rows as pending appointments.
    Re-importing the same sheet is safe: rows that already exist are skipped.
    Raises ValueError if the sheet cannot be read or a required column is missing."""
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as e:
        raise ValueError("Could not read the file. Upload an .xlsx Excel file.") from e

    rows = workbook.active.iter_rows(values_only=True)
    header = next(rows, None)
    if not header:
        raise ValueError("The sheet is empty.")

    columns: dict[str, int] = {}
    for index, heading in enumerate(header):
        key = str(heading or "").strip().lower()
        for field, names in _COLUMNS.items():
            if key in names:
                columns[field] = index
    missing = [field for field in _REQUIRED if field not in columns]
    if missing:
        raise ValueError(f"Missing column(s): {', '.join(missing)}. Expected: name, phone, date, time.")

    def cell(row, field):
        index = columns.get(field)
        return row[index] if index is not None and index < len(row) else None

    imported = 0
    skipped: list[dict] = []
    now = _now()

    with get_session() as db:
        for number, row in enumerate(rows, start=2):
            if not any(value not in (None, "") for value in row):
                continue  # blank line

            name = str(cell(row, "name") or "").strip()
            phone = _clean_phone(cell(row, "phone"))
            day = _parse_date(cell(row, "date"))
            clock = _parse_time(cell(row, "time"))

            reason = None
            if not name:
                reason = "missing name"
            elif not phone:
                reason = "invalid phone number"
            elif not day or not clock:
                reason = "invalid date or time"
            elif str(cell(row, "consent") or "").strip().lower() in _NO:
                reason = "no consent for reminder calls"
            if reason:
                skipped.append({"row": number, "reason": reason})
                continue

            when = datetime.combine(day, clock, tzinfo=IST)
            if when <= now:
                skipped.append({"row": number, "reason": "appointment is in the past"})
                continue

            exists = db.scalar(select(Appointment.id).where(
                Appointment.phone == phone, Appointment.appointment_at == when))
            if exists:
                skipped.append({"row": number, "reason": "already imported"})
                continue

            language = _LANGUAGES.get(str(cell(row, "language") or "").strip().lower())
            db.add(Appointment(patient_name=name, phone=phone, appointment_at=when, language=language))
            imported += 1

    logger.info("✅ [Appointments] Imported %s rows, skipped %s", imported, len(skipped))
    return {"imported": imported, "skipped": skipped}


# ==================================================
# CHOOSING WHO TO CALL
# ==================================================


def _retry_or_give_up(appointment: Appointment, now: datetime) -> None:
    """After an attempt that did not reach the patient: try again later, or stop."""
    if appointment.attempts >= settings.OUTBOUND_MAX_ATTEMPTS:
        appointment.status = "no_answer"
    else:
        appointment.status = "pending"
        appointment.next_attempt_at = now + timedelta(minutes=settings.OUTBOUND_RETRY_MINUTES)


def housekeeping() -> None:
    """Run before every scheduler pass: expire reminders that are too late, and unstick calls that never reported back."""
    now = _now()
    with get_session() as db:
        too_late = now + timedelta(hours=settings.REMINDER_MIN_LEAD_HOURS)
        for appointment in db.scalars(select(Appointment).where(
                Appointment.status == "pending", Appointment.appointment_at <= too_late)):
            appointment.status = "expired"

        stale = now - timedelta(minutes=20)
        for appointment in db.scalars(select(Appointment).where(
                Appointment.status == "calling", Appointment.last_attempt_at < stale)):
            logger.warning("⚠️ [Appointments] Call for %s never reported back", appointment.id)
            _retry_or_give_up(appointment, now)


def claim_due(limit: int) -> list[dict]:
    """Pick appointments that need a reminder now and mark them 'calling' so nobody calls them twice.
    Respects the limit on calls in progress. Returns [{id, name, phone}, ...]."""
    now = _now()
    with get_session() as db:
        in_progress = db.scalar(select(func.count()).select_from(Appointment)
                                .where(Appointment.status == "calling")) or 0
        slots = min(limit, settings.OUTBOUND_MAX_CONCURRENT - in_progress)
        if slots <= 0:
            return []

        rows = db.scalars(
            select(Appointment)
            .where(
                Appointment.status == "pending",
                Appointment.attempts < settings.OUTBOUND_MAX_ATTEMPTS,
                Appointment.appointment_at > now + timedelta(hours=settings.REMINDER_MIN_LEAD_HOURS),
                Appointment.appointment_at <= now + timedelta(hours=settings.REMINDER_HOURS_BEFORE),
                or_(Appointment.next_attempt_at.is_(None), Appointment.next_attempt_at <= now),
            )
            .order_by(Appointment.appointment_at)
            .limit(slots)
            .with_for_update(skip_locked=True)
        ).all()

        claimed = []
        for appointment in rows:
            appointment.status = "calling"
            appointment.attempts += 1
            appointment.last_attempt_at = now
            claimed.append({"id": appointment.id, "name": appointment.patient_name, "phone": appointment.phone})
        return claimed


def claim_one(appointment_id: int) -> dict | None:
    """Mark one appointment as 'calling' right now (used by the test endpoint). None if it is missing or already in a call."""
    with get_session() as db:
        appointment = db.get(Appointment, appointment_id)
        if not appointment or appointment.status == "calling":
            return None
        appointment.status = "calling"
        appointment.attempts += 1
        appointment.last_attempt_at = _now()
        return {"id": appointment.id, "name": appointment.patient_name, "phone": appointment.phone}


# ==================================================
# WHAT HAPPENED ON THE CALL
# ==================================================


def get_brief(appointment_id: int | None) -> dict | None:
    """The few fields the call needs: name, phone, appointment time, language."""
    if not appointment_id:
        return None
    with get_session() as db:
        appointment = db.get(Appointment, appointment_id)
        if not appointment:
            return None
        return {
            "id": appointment.id,
            "name": appointment.patient_name,
            "phone": appointment.phone,
            "appointment_at": appointment.appointment_at,
            "language": appointment.language,
        }


def set_call_uuid(appointment_id: int, call_uuid: str) -> None:
    if not call_uuid:
        return
    with get_session() as db:
        appointment = db.get(Appointment, appointment_id)
        if appointment:
            appointment.call_uuid = call_uuid


def finish_attempt(appointment_id: int, answered: bool) -> None:
    """Plivo says the call ended. If the agent already saved a decision, leave it alone."""
    with get_session() as db:
        appointment = db.get(Appointment, appointment_id)
        if not appointment or appointment.status != "calling":
            return
        if answered:
            appointment.status = "unconfirmed"
        else:
            _retry_or_give_up(appointment, _now())


def record_outcome(appointment_id: int | None, outcome: str, feeling: str = "", note: str = "") -> bool:
    """Save what the patient decided. 'wrong_person' schedules another try. Returns False if it could not be saved."""
    if not appointment_id:
        return False
    try:
        with get_session() as db:
            appointment = db.get(Appointment, appointment_id)
            if not appointment:
                return False
            if outcome == "wrong_person":
                appointment.status = "pending"
                appointment.next_attempt_at = _now() + timedelta(minutes=settings.OUTBOUND_RETRY_MINUTES)
            else:
                appointment.status = _STATUS_BY_OUTCOME[outcome]
            details = " | ".join(part for part in (
                f"Feeling: {feeling.strip()}" if feeling.strip() else "",
                f"Note: {note.strip()}" if note.strip() else "") if part)
            if details:
                appointment.feeling_note = details
        return True
    except Exception:
        logger.exception("❌ [Appointments] Could not save the outcome for %s", appointment_id)
        return False


def set_summary(appointment_id: int, summary: str) -> None:
    with get_session() as db:
        appointment = db.get(Appointment, appointment_id)
        if appointment:
            appointment.summary = summary


def set_recording(appointment_id: int, url: str) -> None:
    with get_session() as db:
        appointment = db.get(Appointment, appointment_id)
        if appointment:
            appointment.recording_url = url


def list_appointments(status: str | None = None, limit: int = 100) -> list[dict]:
    """Newest appointments first, for the staff view."""
    with get_session() as db:
        query = select(Appointment).order_by(Appointment.appointment_at.desc()).limit(limit)
        if status:
            query = query.where(Appointment.status == status)
        return [
            {
                "id": a.id,
                "name": a.patient_name,
                "phone": a.phone,
                "appointment_at": a.appointment_at.astimezone(IST).isoformat(),
                "status": a.status,
                "attempts": a.attempts,
                "feeling_note": a.feeling_note,
                "summary": a.summary,
                "has_recording": bool(a.recording_url),
            }
            for a in db.scalars(query)
        ]