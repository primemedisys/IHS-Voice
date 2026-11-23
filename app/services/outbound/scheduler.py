"""
app/services/outbound/scheduler.py
──────────────────────────────────
The "cron job": every few minutes it looks for appointments that are about 24 hours away
and calls those patients, only during calling hours. Started once from main.py's lifespan.

Safe to leave running: OUTBOUND_ENABLED=false makes every pass do nothing.
"""

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler # type: ignore

from app.config import settings
from app.services.outbound import appointments as appointment_service
from app.services.outbound import place_call as outbound_service
from app.services.outbound.appointments import IST

logger = logging.getLogger("voice.reminders")

_scheduler: AsyncIOScheduler | None = None


def _in_calling_hours() -> bool:
    return settings.OUTBOUND_START_HOUR <= datetime.now(IST).hour < settings.OUTBOUND_END_HOUR


async def run_due_reminders() -> None:
    """One pass: tidy up, then place the calls that are due."""
    if not settings.OUTBOUND_ENABLED:
        return
    try:
        await asyncio.to_thread(appointment_service.housekeeping)
        if not _in_calling_hours():
            return
        due = await asyncio.to_thread(appointment_service.claim_due, settings.OUTBOUND_MAX_PER_RUN)
        for appointment in due:
            await outbound_service.place_call(appointment)
            await asyncio.sleep(2)  # never fire calls in the same instant
    except Exception:
        logger.exception("❌ [Reminders] Scheduler pass failed")  # the next pass will try again


def start() -> None:
    """Start the scheduler (call from inside the running event loop)."""
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler(timezone=IST)
    _scheduler.add_job(
        run_due_reminders, "interval", minutes=settings.REMINDER_POLL_MINUTES,
        id="reminders", max_instances=1, coalesce=True,
    )
    _scheduler.start()
    state = "ON" if settings.OUTBOUND_ENABLED else "OFF (OUTBOUND_ENABLED=false)"
    logger.info("⏰ [Reminders] Scheduler started, calls are %s", state)


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None