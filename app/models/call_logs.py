from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CallLog(Base):
    __tablename__ = "call_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    caller_phone: Mapped[str] = mapped_column(String(20))
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), nullable=True)
    caller_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # existing / new
    urgency: Mapped[str | None] = mapped_column(String(20), nullable=True)      # routine / urgent / emergency

    # ── new in Stage 1 ────────────────────────────────────────────
    # direction: Mapped[str | None] = mapped_column(String(10), nullable=True)    # inbound / outbound
    direction: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    appointment_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    recording_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recording_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    ended_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)  # goodbye/hangup/transfer/error

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)