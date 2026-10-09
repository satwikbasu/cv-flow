"""Tests for the in-process scheduler wiring and its job bodies."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from cvflow.app.commands import Services
from cvflow.app.scheduler import build_scheduler, scheduled_discover, scheduled_heartbeat
from cvflow.config import ScheduleConfig
from cvflow.runs import DiscoverOutcome
from cvflow.storage import ApplicationStore

CHAT = 4242


def _services(discover: Any = None) -> Services:
    return Services(
        store=ApplicationStore(":memory:"),
        notify=lambda _m: None,
        discover=discover or (lambda: DiscoverOutcome("digest", [], 0)),
        tailor=lambda job_id: {},  # unused here
        authorized_user_id=CHAT,
    )


def _schedule() -> ScheduleConfig:
    return ScheduleConfig(
        daily_discovery_time="08:30",
        timezone="Asia/Kolkata",
        heartbeat_interval_minutes=60,
    )


def test_scheduler_registers_daily_discover_and_heartbeat() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    sched = build_scheduler(_services(), bot, chat_id=CHAT, schedule=_schedule())
    jobs = {j.name: j for j in sched.get_jobs()}
    assert set(jobs) == {"daily-discover", "heartbeat"}
    daily = str(jobs["daily-discover"].trigger)
    assert "hour='8'" in daily and "minute='30'" in daily
    assert "0:00:00" not in daily  # cron, not interval
    assert "1:00:00" in str(jobs["heartbeat"].trigger)  # every 60 minutes


def test_daily_discover_tolerates_misfire() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    sched = build_scheduler(_services(), bot, chat_id=CHAT, schedule=_schedule())
    daily = {j.name: j for j in sched.get_jobs()}["daily-discover"]
    assert daily.misfire_grace_time == 3600
    assert daily.coalesce is True


async def test_scheduled_discover_runs_and_offers_buttons() -> None:
    ran: list[str] = []

    def _fake() -> DiscoverOutcome:
        ran.append("x")
        return DiscoverOutcome("digest", ["j1"], 0)

    bot = SimpleNamespace(send_message=AsyncMock())
    await scheduled_discover(_services(_fake), bot, CHAT)
    assert ran == ["x"]
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs.get("reply_markup") is not None


async def test_scheduled_discover_skips_when_a_run_is_already_going() -> None:
    ran: list[str] = []
    services = _services(lambda: ran.append("x"))  # type: ignore[arg-type]
    bot = SimpleNamespace(send_message=AsyncMock())
    await services.discover_lock.acquire()
    try:
        await scheduled_discover(services, bot, CHAT)
    finally:
        services.discover_lock.release()
    assert ran == []
    bot.send_message.assert_not_awaited()


async def test_scheduled_discover_failure_notifies() -> None:
    sent: list[str] = []

    def _boom() -> DiscoverOutcome:
        raise RuntimeError("scrape died")

    services = _services(_boom)
    services.notify = sent.append
    bot = SimpleNamespace(send_message=AsyncMock())
    await scheduled_discover(services, bot, CHAT)
    assert sent and "scrape died" in sent[0]


async def test_scheduled_heartbeat_sends_counts() -> None:
    services = _services()
    services.store.add("j", "Acme", "Dev", "https://example.test/j")
    bot = SimpleNamespace(send_message=AsyncMock())
    await scheduled_heartbeat(services, bot, CHAT)
    text = bot.send_message.await_args.args[1]
    assert "discovered=1" in text and "alive" in text
