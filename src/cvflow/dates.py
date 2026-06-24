"""Posting-date parsing and relative-age rendering.

Job boards report when a posting went up in wildly different shapes: a clean ISO date from
some, "2 Days Ago" or "30+ days ago" from others, "Just posted" or nothing at all. We parse
what we can into a real date and return ``None`` rather than guess when we can't. The digest
then shows "posted 3d ago" when the posting date is known, falling back to "found 3d ago"
(based on when we first saw it) when it isn't — honest about which date we actually have.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

__all__ = ["parse_posting_date", "render_age"]

# Each matches a leading count in a relative phrase like "5 days ago" / "30+ days ago".
_DAYS = re.compile(r"(\d+)\s*\+?\s*day", re.IGNORECASE)
_HOURS = re.compile(r"(\d+)\s*\+?\s*hour", re.IGNORECASE)
_WEEKS = re.compile(r"(\d+)\s*\+?\s*week", re.IGNORECASE)
_MONTHS = re.compile(r"(\d+)\s*\+?\s*month", re.IGNORECASE)

# Phrases that mean "effectively today".
_TODAY_WORDS = ("just post", "just now", "today", "few hour", "minute", "moment")


def parse_posting_date(raw: str, today: date) -> date | None:
    """Best-effort parse of a board's ``date_posted`` string into a date, or ``None``.

    Never guesses: an empty or unrecognized value returns ``None`` so the caller can fall
    back to the discovery date instead of inventing a posting date.
    """
    if not raw:
        return None
    text = raw.strip().lower()
    if not text or text == "nan":
        return None

    if any(word in text for word in _TODAY_WORDS):
        return today
    if "yesterday" in text:
        return today - timedelta(days=1)
    # "an hour ago" / "a minute ago" -> today; "a day ago" / "a week ago" handled below.
    if re.search(r"\ban?\s+(hour|minute|second|moment)", text):
        return today

    if _HOURS.search(text):
        return today
    if (m := _DAYS.search(text)) is not None:
        return today - timedelta(days=int(m.group(1)))
    if (m := _WEEKS.search(text)) is not None:
        return today - timedelta(weeks=int(m.group(1)))
    if (m := _MONTHS.search(text)) is not None:
        return today - timedelta(days=30 * int(m.group(1)))
    if re.search(r"\ba\s+day", text):
        return today - timedelta(days=1)
    if re.search(r"\ba\s+week", text):
        return today - timedelta(weeks=1)

    # ISO date or datetime ("2026-06-20", "2026-06-20T08:30:00+00:00").
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(raw.strip()).date()
    except ValueError:
        return None


def _ago(days: int) -> str:
    if days <= 0:
        return "today"
    if days == 1:
        return "1d ago"
    return f"{days}d ago"


def render_age(posted: date | None, discovered_at: str, now: datetime) -> str:
    """Human age for a posting. "posted Nd ago" when the posting date is known, else
    "found Nd ago" from ``discovered_at`` (an ISO timestamp). Empty only if both are unusable."""
    if posted is not None:
        return "posted " + _ago((now.date() - posted).days)
    try:
        seen = datetime.fromisoformat(discovered_at)
    except ValueError:
        return ""
    return "found " + _ago((now.date() - seen.date()).days)
