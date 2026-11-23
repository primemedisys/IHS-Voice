"""
app/routes/v1/appointments.py
─────────────────────────────
Staff/admin endpoints. Every request needs the header  X-API-Key: <ADMIN_API_KEY>.

  POST /appointments/import          upload the Excel sheet (.xlsx)
  GET  /appointments                 list appointments (?status=pending|confirmed|...)
  POST /appointments/{id}/call-now   place the reminder call right now (for testing)
"""

import asyncio
import hmac

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile

from app.config import settings
from app.services.outbound import appointments as appointment_service
from app.services.outbound import place_call as outbound_service

router = APIRouter(prefix="/appointments", tags=["appointments"])


def _require_key(x_api_key: str = Header(default="")) -> None:
    """Reject requests without the right key. If no key is configured the endpoints stay switched off."""
    if not settings.ADMIN_API_KEY:
        raise HTTPException(status_code=503, detail="Admin API is off (ADMIN_API_KEY is not set)")
    if not hmac.compare_digest(x_api_key.encode(), settings.ADMIN_API_KEY.encode()):
        raise HTTPException(status_code=401, detail="Invalid API key")


@router.post("/import", dependencies=[Depends(_require_key)])
async def import_sheet(file: UploadFile = File(...)) -> dict:
    """Upload the Excel sheet. Columns: name, phone, date, time (optional: language, consent)."""
    content = await file.read()
    try:
        return await asyncio.to_thread(appointment_service.import_excel, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("", dependencies=[Depends(_require_key)])
async def list_appointments(status: str | None = None) -> list[dict]:
    return await asyncio.to_thread(appointment_service.list_appointments, status)


@router.post("/{appointment_id}/call-now", dependencies=[Depends(_require_key)])
async def call_now(appointment_id: int) -> dict:
    """Place the reminder call immediately, ignoring the 24-hour window and calling hours. Use it for tests."""
    appointment = await asyncio.to_thread(appointment_service.claim_one, appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found, or a call is already in progress")
    ok = await outbound_service.place_call(appointment)
    return {"appointment": appointment_id, "call_placed": ok}