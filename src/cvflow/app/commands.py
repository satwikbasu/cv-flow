"""Chat commands: the forgiving text parser and the handlers it feeds.

The parser accepts ``/tailor 3``, ``tailor 3``, ``t 3``, a bare ``3`` right
after a list, ``skip 2 4``, ``skip 2-4``, and the plain commands. Anything it
can't read with certainty returns ``None`` so the handler replies with a hint —
it never guesses and never stays silent.

The handlers stay thin: auth check, parse, resolve ordinals against the stored
slot map, then hand the blocking work (injected ``Services.discover`` /
``Services.tailor`` callables) to an executor thread. One asyncio.Lock per verb
keeps a manual command and the scheduler from double-running.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from cvflow.render import render_jobs
from cvflow.statemachine import IllegalTransition, Status
from cvflow.storage import ApplicationStore

if TYPE_CHECKING:
    from cvflow.runs import DiscoverOutcome

__all__ = ["Action", "parse_action", "Services", "on_text", "build_job_keyboard"]

_VERBS = {
    "tailor": "tailor",
    "t": "tailor",
    "skip": "skip",
    "s": "skip",
}

_PLAIN = {
    "discover": "discover",
    "jobs": "jobs",
    "status": "status",
    "help": "help",
    "start": "help",
}

_JOBS_FILTERS = {"open", "tailored", "all"}

_RANGE_RE = re.compile(r"^(\d+)-(\d+)$")


@dataclass(frozen=True)
class Action:
    kind: str  # tailor | skip | discover | jobs | status | help
    ordinals: list[int] = field(default_factory=list)
    jobs_filter: str = "open"


def _parse_ordinals(tokens: list[str]) -> list[int] | None:
    """Numbers and a-b ranges only; anything else -> None (the caller hints, never guesses)."""
    out: list[int] = []
    for tok in tokens:
        m = _RANGE_RE.match(tok)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo < 1 or hi < lo:
                return None
            out.extend(range(lo, hi + 1))
        elif tok.isdigit():
            n = int(tok)
            if n < 1:
                return None
            out.append(n)
        else:
            return None
    return out or None


def parse_action(text: str, slots: list[str]) -> Action | None:
    """Parse one chat message into an Action, or None when it isn't understood."""
    tokens = text.strip().split()
    if not tokens:
        return None
    head = tokens[0].lower().lstrip("/").split("@")[0]

    if head in _PLAIN:
        kind = _PLAIN[head]
        if kind == "jobs":
            if len(tokens) == 1:
                return Action("jobs")
            if len(tokens) == 2 and tokens[1].lower() in _JOBS_FILTERS:
                return Action("jobs", jobs_filter=tokens[1].lower())
            return None
        if len(tokens) > 1:
            return None
        return Action(kind)

    if head in _VERBS:
        ordinals = _parse_ordinals(tokens[1:])
        if ordinals is None:
            return None
        return Action(_VERBS[head], ordinals=ordinals)

    # A bare number right after a numbered list means "tailor that one".
    if slots:
        ordinals = _parse_ordinals(tokens)
        if ordinals is not None and len(ordinals) == 1:
            return Action("tailor", ordinals=ordinals)
    return None


@dataclass
class Services:
    """Everything the handlers need, injected so tests never build real pipelines.

    ``discover`` and ``tailor`` are the BLOCKING run bodies (wrappers around
    ``cvflow.runs``); handlers run them via ``run_in_executor``.
    """

    store: ApplicationStore
    notify: Callable[[str], None]
    discover: Callable[[], DiscoverOutcome]
    tailor: Callable[[str], dict[str, str]]
    authorized_user_id: int
    discover_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tailor_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_HELP = (
    "cv-flow finds jobs and tailors your resume — it never applies for you.\n\n"
    "/discover — hunt for jobs now (also runs daily)\n"
    "/tailor N — tailor the resume for job N from the last list\n"
    "/skip N — hide job N (also: skip 2 4, skip 2-4)\n"
    "/jobs [open|tailored|all] — the backlog, freshly numbered\n"
    "/status — counts per state\n"
    "/help — this message\n\n"
    "Plain text works too: “tailor 3”, “t 3”, or just “3” after a list."
)

_HINT = "🤔 I didn't catch that. Try /tailor 3, /skip 2, /jobs — or /help for everything."


def build_job_keyboard(slots: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """One row per listed job: [✂️ Tailor N] [🙈 Skip N] → callback tailor:/skip:<job_id>."""
    rows = [
        [
            InlineKeyboardButton(f"✂️ Tailor {i}", callback_data=f"tailor:{job_id}"),
            InlineKeyboardButton(f"🙈 Skip {i}", callback_data=f"skip:{job_id}"),
        ]
        for i, job_id in slots
    ]
    return InlineKeyboardMarkup(rows)


def _services(context: Any) -> Services:
    services: Services = context.bot_data["services"]
    return services


def _authorized(update: Any, services: Services) -> bool:
    user = update.effective_user
    return user is not None and user.id == services.authorized_user_id


def _label(services: Services, job_id: str) -> str:
    app = services.store.get(job_id)
    return f"{app.role} @ {app.company}" if app is not None else job_id


async def _do_discover(message: Any, services: Services) -> None:
    if services.discover_lock.locked():
        await message.reply_text(
            "⏳ A discovery run is already in progress — the digest is on its way."
        )
        return
    async with services.discover_lock:
        await message.reply_text("🔎 Discovery started — I'll post the digest here soon.")
        loop = asyncio.get_running_loop()
        try:
            outcome = await loop.run_in_executor(None, services.discover)
        except Exception as exc:  # noqa: BLE001 — a failed run must reach the user
            await message.reply_text(f"⚠️ Discovery failed: {exc}")
            return
    if outcome.ordered_job_ids:
        slots = list(enumerate(outcome.ordered_job_ids, start=1))
        await message.reply_text(
            "Quick actions for the digest above:", reply_markup=build_job_keyboard(slots)
        )


async def _do_tailor(job_id: str, message: Any, services: Services) -> None:
    label = _label(services, job_id)
    if services.tailor_lock.locked():
        await message.reply_text("⏳ A tailoring run is already in progress — try again shortly.")
        return
    async with services.tailor_lock:
        await message.reply_text(f"✂️ Tailoring {label} — the resume + diff will land here.")
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, services.tailor, job_id)
        except Exception as exc:  # noqa: BLE001 — a failed run must reach the user
            await message.reply_text(f"⚠️ Tailoring failed for {label}: {exc}")
            return
        headline = f"✅ Tailored — {result['role']} @ {result['company']}"
        await message.reply_text(f"{headline}\n\n{result['diff']}")
        pdf = Path(result["pdf_path"])
        with pdf.open("rb") as fh:
            await message.reply_document(document=fh, filename=pdf.name, caption=headline)


async def _do_skip(ordinals: list[int], message: Any, services: Services) -> None:
    lines: list[str] = []
    for n in ordinals:
        job_id = services.store.get_digest_slot(n)
        if job_id is None:
            lines.append(f"⚠️ No job {n} on the last list — run /jobs to renumber.")
            continue
        label = _label(services, job_id)
        try:
            services.store.set_status(job_id, Status.SKIPPED)
        except IllegalTransition:
            lines.append(f"⚠️ {n}. {label} can't be skipped any more.")
        else:
            lines.append(f"🙈 Skipped {n}. {label}")
    await message.reply_text("\n".join(lines))


async def _do_jobs(which: str, message: Any, services: Services) -> None:
    apps = services.store.list_by_status(Status.DISCOVERED) + services.store.list_by_status(
        Status.TAILORED
    )
    text, slots = render_jobs(apps, which, now=datetime.now(UTC))
    if slots:
        services.store.set_digest_slots([job_id for _, job_id in slots])
        await message.reply_text(text, reply_markup=build_job_keyboard(slots))
    else:
        await message.reply_text(text)


async def _do_status(message: Any, services: Services) -> None:
    counts = " · ".join(
        f"{s.value}={len(services.store.list_by_status(s))}" for s in Status
    )
    await message.reply_text(f"📊 {counts}")


async def on_text(update: Any, context: Any) -> None:
    """Single entry point for every text message and slash command."""
    services = _services(context)
    if not _authorized(update, services):
        return
    message = update.effective_message
    if message is None or not message.text:
        return
    action = parse_action(message.text, services.store.digest_slots())
    if action is None:
        await message.reply_text(_HINT)
        return
    if action.kind == "help":
        await message.reply_text(_HELP)
    elif action.kind == "status":
        await _do_status(message, services)
    elif action.kind == "jobs":
        await _do_jobs(action.jobs_filter, message, services)
    elif action.kind == "discover":
        await _do_discover(message, services)
    elif action.kind == "skip":
        await _do_skip(action.ordinals, message, services)
    elif action.kind == "tailor":
        if len(action.ordinals) != 1:
            await message.reply_text("⚠️ One job at a time, e.g. /tailor 4")
            return
        n = action.ordinals[0]
        job_id = services.store.get_digest_slot(n)
        if job_id is None:
            await message.reply_text(f"⚠️ No job {n} on the last list — run /jobs to renumber.")
            return
        await _do_tailor(job_id, message, services)
