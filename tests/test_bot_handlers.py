"""Handler tests: fake update/context objects, a real in-memory store, fake run bodies."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from cvflow.app.buttons import on_button
from cvflow.app.commands import Services, on_text
from cvflow.runs import DiscoverOutcome
from cvflow.statemachine import Status
from cvflow.storage import ApplicationStore

AUTH_ID = 4242


def _services(
    *,
    discover: Any = None,
    tailor: Any = None,
    store: ApplicationStore | None = None,
) -> Services:
    return Services(
        store=store or ApplicationStore(":memory:"),
        notify=lambda _m: None,
        discover=discover or (lambda: DiscoverOutcome("digest", [], 0)),
        tailor=tailor or (lambda job_id: {"job_id": job_id, "role": "R", "company": "C",
                                          "pdf_path": "unused.pdf", "diff": "no changes"}),
        authorized_user_id=AUTH_ID,
    )


def _update(text: str = "", user_id: int = AUTH_ID) -> SimpleNamespace:
    message = SimpleNamespace(
        text=text,
        reply_text=AsyncMock(),
        reply_document=AsyncMock(),
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        message=message,
        callback_query=None,
    )


def _context(services: Services) -> SimpleNamespace:
    return SimpleNamespace(bot_data={"services": services})


def _seed(store: ApplicationStore, job_id: str = "j1") -> None:
    store.add(job_id, "Acme", "Backend Dev", f"https://example.test/{job_id}")


def _replies(update: SimpleNamespace) -> str:
    return "\n".join(
        str(call.args[0]) for call in update.effective_message.reply_text.await_args_list
    )


# --- auth ---


async def test_unauthorized_user_is_silently_ignored() -> None:
    services = _services()
    update = _update("/discover", user_id=999)
    await on_text(update, _context(services))
    update.effective_message.reply_text.assert_not_awaited()


# --- unknown input ---


async def test_unrecognized_text_gets_a_hint_not_a_guess() -> None:
    update = _update("what jobs are there?")
    await on_text(update, _context(_services()))
    assert "/help" in _replies(update)


# --- discover ---


async def test_discover_acks_runs_and_offers_buttons() -> None:
    calls: list[str] = []
    store = ApplicationStore(":memory:")
    _seed(store, "j1")

    def _fake_discover() -> DiscoverOutcome:
        calls.append("ran")
        return DiscoverOutcome("digest text", ["j1"], 0)

    services = _services(discover=_fake_discover, store=store)
    update = _update("/discover")
    await on_text(update, _context(services))
    assert calls == ["ran"]
    text = _replies(update)
    assert "Discovery started" in text
    # a keyboard message follows so the digest (sent via the notifier) gets buttons
    kwargs = update.effective_message.reply_text.await_args_list[-1].kwargs
    assert kwargs.get("reply_markup") is not None


async def test_discover_while_running_says_so_and_does_not_double_run() -> None:
    calls: list[str] = []
    services = _services(discover=lambda: calls.append("ran"))  # type: ignore[arg-type]
    await services.discover_lock.acquire()
    try:
        update = _update("/discover")
        await on_text(update, _context(services))
    finally:
        services.discover_lock.release()
    assert calls == []
    assert "already" in _replies(update).lower()


async def test_discover_failure_reaches_the_user() -> None:
    def _boom() -> DiscoverOutcome:
        raise RuntimeError("scrape exploded")

    update = _update("/discover")
    await on_text(update, _context(_services(discover=_boom)))
    assert "scrape exploded" in _replies(update)


# --- tailor ---


async def test_tailor_by_ordinal_posts_diff_and_pdf(tmp_path: Path) -> None:
    store = ApplicationStore(":memory:")
    _seed(store, "j1")
    store.set_digest_slots(["j1"])
    pdf = tmp_path / "out.pdf"
    pdf.write_bytes(b"%PDF")

    def _fake_tailor(job_id: str) -> dict[str, str]:
        return {"job_id": job_id, "role": "Backend Dev", "company": "Acme",
                "pdf_path": str(pdf), "diff": "No bullets reworded."}

    services = _services(store=store, tailor=_fake_tailor)
    update = _update("/tailor 1")
    await on_text(update, _context(services))
    text = _replies(update)
    assert "Backend Dev @ Acme" in text
    assert "No bullets reworded." in text
    update.effective_message.reply_document.assert_awaited_once()


async def test_tailor_bad_ordinal_hints() -> None:
    store = ApplicationStore(":memory:")
    _seed(store, "j1")
    store.set_digest_slots(["j1"])
    calls: list[str] = []
    services = _services(store=store, tailor=lambda j: calls.append(j))  # type: ignore[arg-type]
    update = _update("/tailor 7")
    await on_text(update, _context(services))
    assert calls == []
    assert "7" in _replies(update)


async def test_tailor_one_at_a_time() -> None:
    store = ApplicationStore(":memory:")
    _seed(store, "j1")
    store.set_digest_slots(["j1"])
    update = _update("tailor 1-3")
    await on_text(update, _context(_services(store=store)))
    assert "one job at a time" in _replies(update).lower()


async def test_tailor_failure_reaches_the_user() -> None:
    store = ApplicationStore(":memory:")
    _seed(store, "j1")
    store.set_digest_slots(["j1"])

    def _boom(job_id: str) -> dict[str, str]:
        raise RuntimeError("compile died")

    update = _update("t 1")
    await on_text(update, _context(_services(store=store, tailor=_boom)))
    assert "compile died" in _replies(update)
    update.effective_message.reply_document.assert_not_awaited()


# --- skip ---


async def test_skip_flips_status_and_acks() -> None:
    store = ApplicationStore(":memory:")
    _seed(store, "j1")
    _seed(store, "j2")
    store.set_digest_slots(["j1", "j2"])
    update = _update("skip 2")
    await on_text(update, _context(_services(store=store)))
    assert store.get("j2").status is Status.SKIPPED  # type: ignore[union-attr]
    assert store.get("j1").status is Status.DISCOVERED  # type: ignore[union-attr]
    assert "Backend Dev @ Acme" in _replies(update)


# --- jobs / status / help ---


async def test_jobs_lists_backlog_and_resets_slots() -> None:
    store = ApplicationStore(":memory:")
    _seed(store, "a")
    _seed(store, "b")
    store.set_discovery_meta("a", benchmark=50, fit_score=5, fit_reason="", concerns=[],
                             cohort="M", ctc_lpa=None)
    store.set_discovery_meta("b", benchmark=90, fit_score=9, fit_reason="", concerns=[],
                             cohort="M", ctc_lpa=None)
    store.set_digest_slots(["stale"])
    update = _update("/jobs")
    await on_text(update, _context(_services(store=store)))
    assert store.digest_slots() == ["b", "a"]  # benchmark order, slots rewritten
    assert "1. " in _replies(update)


async def test_status_counts_and_help_lists_commands() -> None:
    store = ApplicationStore(":memory:")
    _seed(store, "j1")
    update = _update("/status")
    await on_text(update, _context(_services(store=store)))
    assert "discovered=1" in _replies(update)

    update2 = _update("/help")
    await on_text(update2, _context(_services()))
    assert "/discover" in _replies(update2) and "/tailor" in _replies(update2)


# --- buttons ---


def _button_update(data: str, user_id: int = AUTH_ID) -> SimpleNamespace:
    message = SimpleNamespace(
        reply_text=AsyncMock(),
        reply_document=AsyncMock(),
    )
    query = SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        message=message,
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        callback_query=query,
    )


async def test_skip_button_flips_status() -> None:
    store = ApplicationStore(":memory:")
    _seed(store, "j1")
    update = _button_update("skip:j1")
    await on_button(update, _context(_services(store=store)))
    assert store.get("j1").status is Status.SKIPPED  # type: ignore[union-attr]
    update.callback_query.answer.assert_awaited()


async def test_tailor_button_runs_tailor(tmp_path: Path) -> None:
    store = ApplicationStore(":memory:")
    _seed(store, "j1")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF")
    done: list[str] = []

    def _fake_tailor(job_id: str) -> dict[str, str]:
        done.append(job_id)
        return {"job_id": job_id, "role": "R", "company": "C",
                "pdf_path": str(pdf), "diff": "d"}

    update = _button_update("tailor:j1")
    await on_button(update, _context(_services(store=store, tailor=_fake_tailor)))
    assert done == ["j1"]
    update.effective_message.reply_document.assert_awaited_once()


async def test_button_from_stranger_is_ignored() -> None:
    store = ApplicationStore(":memory:")
    _seed(store, "j1")
    update = _button_update("skip:j1", user_id=999)
    await on_button(update, _context(_services(store=store)))
    assert store.get("j1").status is Status.DISCOVERED  # type: ignore[union-attr]
