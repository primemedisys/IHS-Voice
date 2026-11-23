"""
app/models/inbound_inquiry.py
─────────────────────────────
One row per inbound call.

Written as the conversation goes (not only at the end), so an unexpected
disconnect, a crash or a receptionist transfer still leaves everything the
caller already told us. `call_id` is unique: one call = one row, updated
in place, never duplicated.
"""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class InboundInquiry(Base):
    __tablename__ = "inbound_inquiries"

    id: Mapped[int] = mapped_column(primary_key=True)

    # ── who / which call ──────────────────────────────────────────
    call_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    phone: Mapped[str] = mapped_column(String(20), index=True)          # caller number, E.164
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), nullable=True)

    patient_type: Mapped[str | None] = mapped_column(String(20), nullable=True)     # existing / new
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    date_of_birth: Mapped[str | None] = mapped_column(String(20), nullable=True)    # YYYY-MM-DD as confirmed
    identity_status: Mapped[str | None] = mapped_column(String(20), nullable=True)  # pending/verified/failed/refused/na

    # ── what is wrong ─────────────────────────────────────────────
    condition: Mapped[str | None] = mapped_column(Text, nullable=True)   # injury / reason for calling
    symptoms: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_when: Mapped[str | None] = mapped_column(String(100), nullable=True)
    severity: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prior_treatment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── what they want ────────────────────────────────────────────
    wants_appointment: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    wants_receptionist: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # ── when they would like to come (a request, never a confirmed booking) ──
    preferred_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    preferred_time: Mapped[str | None] = mapped_column(String(30), nullable=True)   # 10:30, or morning / afternoon / evening / flexible
    preferred_slot_text: Mapped[str | None] = mapped_column(Text, nullable=True)   # the caller's own words

    # ── how it ended ──────────────────────────────────────────────
    transfer_status: Mapped[str] = mapped_column(String(20), default="none")   # none/requested/done/failed
    outcome: Mapped[str] = mapped_column(String(20), default="open")           # open/completed/transferred/abandoned
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── recording (metadata only, no S3 in Stage 1) ───────────────
    recording_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recording_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── timing ────────────────────────────────────────────────────
    call_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    call_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )