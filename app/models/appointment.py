"""
app/models/appointment.py
─────────────────────────
One row per appointment that should get a reminder call.

Status flow:
  pending -> calling -> confirmed | reschedule | cancelled   (the patient answered and decided)
                     -> unconfirmed                          (answered, but no decision was saved)
                     -> pending (retry later) -> no_answer   (nobody answered, attempts used up)
  pending -> expired                                          (too close to the appointment to remind)
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (UniqueConstraint("phone", "appointment_at", name="uq_appointment_phone_time"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(20), index=True)                  # E.164
    appointment_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    language: Mapped[str | None] = mapped_column(String(5), nullable=True)      # en / hi / or (optional)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    call_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    feeling_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    recording_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())