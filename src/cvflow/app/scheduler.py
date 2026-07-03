"""In-process scheduling: the daily discovery run and the heartbeat.

APScheduler's AsyncIOScheduler shares the bot's event loop, so the job bodies
are coroutines that push the blocking work into the executor — exactly like
the command handlers. The daily job quietly steps aside when a manual
/discover already holds the lock; two digests for one morning helps no one.
"""

from __future__ import annotations

import asyncio
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from cvflow.app.commands import Services, build_job_keyboard
from cvflow.config import ScheduleConfig
from cvflow.runs import run_heartbeat

__all__ = ["build_scheduler", "scheduled_discover", "scheduled_heartbeat"]


async def scheduled_discover(services: Services, bot: Any, chat_id: int) -> None:
    if services.discover_lock.locked():
        return  # a manual run is already producing today's digest
    async with services.discover_lock:
        loop = asyncio.get_running_loop()
        try:
            outcome = await loop.run_in_executor(None, services.discover)
        except Exception as exc:  # noqa: BLE001 — a failed daily run must reach the user
            await loop.run_in_executor(
                None, services.notify, f"⚠️ Daily discovery failed: {exc}"
            )
            return
    if outcome.ordered_job_ids:
        slots = list(enumerate(outcome.ordered_job_ids, start=1))
        await bot.send_message(
            chat_id,
            "Quick actions for the digest above:",
            reply_markup=build_job_keyboard(slots),
        )


async def scheduled_heartbeat(services: Services, bot: Any, chat_id: int) -> None:
    await bot.send_message(chat_id, run_heartbeat(services.store))


def build_scheduler(
    services: Services,
    bot: Any,
    *,
    chat_id: int,
    schedule: ScheduleConfig,
) -> AsyncIOScheduler:
    """Wire the cron jobs from config. The caller starts it once the loop runs."""
    sched = AsyncIOScheduler(timezone=schedule.timezone)
    hour, minute = schedule.daily_discovery_time.split(":")
    sched.add_job(
        scheduled_discover,
        CronTrigger(hour=int(hour), minute=int(minute), timezone=schedule.timezone),
        args=[services, bot, chat_id],
        name="daily-discover",
    )
    sched.add_job(
        scheduled_heartbeat,
        "interval",
        minutes=schedule.heartbeat_interval_minutes,
        args=[services, bot, chat_id],
        name="heartbeat",
    )
    return sched
