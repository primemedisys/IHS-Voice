"""
app/routes/v1/staff.py
──────────────────────
Staff dashboard endpoints. Every request needs the header  X-API-Key: <ADMIN_API_KEY>.

  GET  /staff/overview                     counts for the summary cards
  GET  /staff/callbacks                    the follow-up list (?status=pending|approved|declined)
  POST /staff/callbacks/{id}/status        approve / decline / reopen one request
  GET  /staff/inquiries                    recent inbound calls with what the caller said

Approve / decline only change callbacks.status. Nothing is ever deleted.
"""

import asyncio
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import case, func, select

from app.database import get_session
from app.models.appointment import Appointment
from app.models.callback import Callback
from app.models.inbound_inquiry import InboundInquiry
from app.routes.v1.appointments import _require_key
from app.services.outbound.appointments import IST

router = APIRouter(prefix="/staff", tags=["staff"], dependencies=[Depends(_require_key)])


class StatusUpdate(BaseModel):
    status: Literal["pending", "approved", "declined"]


def _ist(value: datetime | None) -> str | None:
    return value.astimezone(IST).isoformat() if value else None


def _callback_row(callback: Callback, inquiry: InboundInquiry | None) -> dict:
    return {
        "id": callback.id,
        "call_id": callback.call_id,
        "name": callback.name or (inquiry.name if inquiry else None),
        "phone": callback.phone,
        "date_of_birth": callback.date_of_birth,
        "issue": callback.issue,
        "urgency": callback.urgency,
        "reason": callback.reason,
        "preferred_slot": callback.preferred_slot,
        "status": callback.status,
        "created_at": _ist(callback.created_at),
        "patient_type": inquiry.patient_type if inquiry else None,
        "symptoms": inquiry.symptoms if inquiry else None,
        "started_when": inquiry.started_when if inquiry else None,
        "severity": inquiry.severity if inquiry else None,
        "prior_treatment": inquiry.prior_treatment if inquiry else None,
        "summary": inquiry.summary if inquiry else None,
        "recording_url": inquiry.recording_url if inquiry else None,
    }


def _overview() -> dict:
    start_of_today = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    with get_session() as db:
        callback_counts = dict(db.execute(select(Callback.status, func.count()).group_by(Callback.status)).all())
        urgent_pending = db.scalar(select(func.count()).select_from(Callback).where(
            Callback.status == "pending", Callback.urgency.in_(("urgent", "emergency")))) or 0
        calls_today = db.scalar(select(func.count()).select_from(InboundInquiry).where(
            InboundInquiry.call_started_at >= start_of_today.astimezone(timezone.utc))) or 0
        reminder_counts = dict(db.execute(select(Appointment.status, func.count()).group_by(Appointment.status)).all())
    return {
        "callbacks": callback_counts,
        "urgent_pending": urgent_pending,
        "inbound_calls_today": calls_today,
        "reminders": reminder_counts,
    }


def _list_callbacks(status: str | None, limit: int) -> list[dict]:
    priority = case((Callback.urgency == "emergency", 0), (Callback.urgency == "urgent", 1), else_=2)
    query = (
        select(Callback, InboundInquiry)
        .outerjoin(InboundInquiry, InboundInquiry.call_id == Callback.call_id)
        .order_by(priority, Callback.created_at.desc())
        .limit(limit)
    )
    if status:
        query = query.where(Callback.status == status)
    with get_session() as db:
        return [_callback_row(callback, inquiry) for callback, inquiry in db.execute(query).all()]


def _set_callback_status(callback_id: int, status: str) -> dict | None:
    with get_session() as db:
        callback = db.get(Callback, callback_id)
        if not callback:
            return None
        callback.status = status
        inquiry = db.scalar(select(InboundInquiry).where(InboundInquiry.call_id == callback.call_id))
        return _callback_row(callback, inquiry)


def _list_inquiries(limit: int) -> list[dict]:
    with get_session() as db:
        rows = db.scalars(select(InboundInquiry).order_by(InboundInquiry.call_started_at.desc()).limit(limit))
        return [
            {
                "id": r.id,
                "call_id": r.call_id,
                "phone": r.phone,
                "name": r.name,
                "patient_type": r.patient_type,
                "identity_status": r.identity_status,
                "condition": r.condition,
                "symptoms": r.symptoms,
                "severity": r.severity,
                "wants_appointment": r.wants_appointment,
                "wants_receptionist": r.wants_receptionist,
                "preferred_date": r.preferred_date.isoformat() if r.preferred_date else None,
                "preferred_time": r.preferred_time,
                "preferred_slot_text": r.preferred_slot_text,
                "transfer_status": r.transfer_status,
                "outcome": r.outcome,
                "summary": r.summary,
                "recording_url": r.recording_url,
                "call_started_at": _ist(r.call_started_at),
            }
            for r in rows
        ]


@router.get("/overview")
async def overview() -> dict:
    return await asyncio.to_thread(_overview)


@router.get("/callbacks")
async def list_callbacks(status: str | None = None, limit: int = 200) -> list[dict]:
    return await asyncio.to_thread(_list_callbacks, status, min(max(limit, 1), 500))


@router.post("/callbacks/{callback_id}/status")
async def update_callback_status(callback_id: int, body: StatusUpdate) -> dict:
    row = await asyncio.to_thread(_set_callback_status, callback_id, body.status)
    if not row:
        raise HTTPException(status_code=404, detail="Callback request not found")
    return row


@router.get("/inquiries")
async def list_inquiries(limit: int = 100) -> list[dict]:
    return await asyncio.to_thread(_list_inquiries, min(max(limit, 1), 500))
