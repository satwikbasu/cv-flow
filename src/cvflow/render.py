"""Pure text rendering for the digest and the /jobs backlog.

Everything here is deterministic string building — no Telegram imports, no
network — so the bot layer stays a thin shell and these stay unit-testable.
Inline buttons are constructed in ``app/``; this module only decides the text
and the ordinal → job_id slot order that the buttons and ``/tailor N`` share.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from cvflow.dates import parse_posting_date, render_age
from cvflow.discovery import DROP_BUCKET_ORDER, DropRecord
from cvflow.statemachine import Status
from cvflow.storage import Application

__all__ = ["render_digest", "render_jobs", "format_drop_report", "filtered_footer"]

_MAX_MSG = 4000  # Telegram's hard limit is 4096; leave headroom.


def _digest_age(bj: Any, now: datetime) -> str:
    posted = parse_posting_date(bj.posting.date_posted, now.date())
    # A digest entry was discovered just now, so the fallback reads "found today".
    return render_age(posted, now.isoformat(), now)


def _digest_block(bj: Any, i: int, now: datetime) -> str:
    p = bj.posting
    company = p.company or "Unknown company"
    bits = [f"bench {bj.benchmark}", f"fit {bj.fit_score}"]
    if bj.ctc_lpa:
        bits.append(f"{round(bj.ctc_lpa)} LPA")
    if p.location:
        bits.append(p.location)
    age = _digest_age(bj, now)
    if age:
        bits.append(age)
    concerns = f"  ⚠️ {'; '.join(bj.concerns)}\n" if bj.concerns else ""
    return (
        f"{i}. {p.title} @ {company}  ({' · '.join(bits)})\n"
        f"  {p.url}\n  {bj.fit_reason}\n{concerns}"
    )


def render_digest(result: dict[str, Any], *, now: datetime) -> str:
    """Render the two-cohort digest (M = stated pay, N = no pay), continuously numbered.

    Drop counts stay OUT of the digest — they travel as a separate message so they are
    never shown twice. Source notices (e.g. a blocked Naukri) are always appended.
    """
    m, n = result.get("M", []), result.get("N", [])
    notices = [f"⚠️ {line}" for line in result.get("_notices", [])]
    if not m and not n:
        return "\n".join(["No new jobs today.", *notices])
    parts = ["🗞️ cv-flow — new jobs today:\n"]
    idx = 1
    parts.append("💰 With stated pay (ranked by value)\n")
    if m:
        for bj in m:
            parts.append(_digest_block(bj, idx, now))
            idx += 1
    else:
        parts.append("No stated-pay jobs today.\n")
    parts.append("📋 Pay not stated (ranked by fit)\n")
    if n:
        for bj in n:
            parts.append(_digest_block(bj, idx, now))
            idx += 1
    else:
        parts.append("No pay-not-stated jobs today.\n")
    parts.extend(notices)
    parts.append("Reply: /tailor N · /skip N — or tap the buttons under each job.")
    return "\n".join(parts)


_JOBS_STATUSES: dict[str, tuple[Status, ...]] = {
    "open": (Status.DISCOVERED,),
    "tailored": (Status.TAILORED,),
    "all": (Status.DISCOVERED, Status.TAILORED),
}


def _jobs_age(app: Application, now: datetime) -> str:
    posted = None
    if app.date_posted_parsed:
        try:
            posted = datetime.fromisoformat(app.date_posted_parsed).date()
        except ValueError:
            posted = None
    return render_age(posted, app.discovered_at, now)


def render_jobs(
    apps: list[Application], which: str, *, now: datetime
) -> tuple[str, list[tuple[int, str]]]:
    """Render the persistent backlog, benchmark-sorted and renumbered.

    Returns the text plus the (ordinal, job_id) slot order so the caller can rewrite
    the digest-slot map — a following ``/tailor N`` must resolve against THIS list.
    """
    statuses = _JOBS_STATUSES.get(which, _JOBS_STATUSES["open"])
    picked = [a for a in apps if a.status in statuses]
    picked.sort(key=lambda a: a.benchmark if a.benchmark is not None else -1, reverse=True)
    if not picked:
        return ("Nothing here yet — run /discover to go job hunting.", [])
    lines: list[str] = []
    slots: list[tuple[int, str]] = []
    for i, app in enumerate(picked, start=1):
        slots.append((i, app.job_id))
        bits = []
        if app.benchmark is not None:
            bits.append(f"bench {app.benchmark}")
        if app.fit_score is not None:
            bits.append(f"fit {app.fit_score}")
        age = _jobs_age(app, now)
        if age:
            bits.append(age)
        marker = "  ✂️ tailored" if app.status is Status.TAILORED else ""
        lines.append(f"{i}. {app.role} @ {app.company}  ({' · '.join(bits)}){marker}")
        lines.append(f"  {app.jd_url}")
    lines.append("")
    lines.append("Reply: /tailor N · /skip N — or tap the buttons.")
    return ("\n".join(lines), slots)


def filtered_footer(dropped: dict[str, int] | None) -> str:
    """One line summarising what the gates removed today, so the digest isn't a black box."""
    if not dropped:
        return ""
    items = [f"{dropped[k]} {k}" for k in DROP_BUCKET_ORDER if dropped.get(k)]
    return "🔍 Filtered today: " + " · ".join(items) if items else ""


def format_drop_report(records: list[DropRecord]) -> list[str]:
    """Group drop records by bucket (in DROP_BUCKET_ORDER) and chunk into <=4000-char
    messages. A bucket that overflows one message repeats its header (cont.) so each
    chunk keeps context. Empty input -> []."""
    if not records:
        return []
    grouped: dict[str, list[DropRecord]] = {}
    for r in records:
        grouped.setdefault(r.bucket, []).append(r)

    messages: list[str] = []
    current: list[str] = []
    length = 0

    def flush() -> None:
        nonlocal current, length
        if current:
            messages.append("\n".join(current))
            current = []
            length = 0

    def add(line: str) -> None:
        nonlocal length
        if length + len(line) + 1 > _MAX_MSG:
            flush()
        current.append(line)
        length += len(line) + 1

    unknown = [b for b in grouped if b not in set(DROP_BUCKET_ORDER)]
    for bucket in [*DROP_BUCKET_ORDER, *unknown]:
        recs = grouped.get(bucket)
        if not recs:
            continue
        header = f"🚫 Dropped {len(recs)} — {bucket}"
        add(header)
        for r in recs:
            line = f"• {r.label} — {r.detail}"
            if length + len(line) + 1 > _MAX_MSG:
                flush()
                add(f"{header} (cont.)")
            add(line)
    flush()
    return messages
