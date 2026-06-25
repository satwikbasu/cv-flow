"""Discovery: multi-board search -> cross-day dedup -> LLM ranking -> top-N.

Scraping sits behind an injectable ``search_fn`` (default: JobSpy). That seam lets a different
source (say a hosted jobs API) slot in without touching dedup, ranking, or storage. JobSpy is the
only zero-cost self-hosted default.

Ranking only ever reorders real postings; it never invents jobs — any job_id the ranker returns
that was not a candidate is dropped.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, NamedTuple

__all__ = [
    "JobPosting",
    "normalize_rows",
    "DropRecord",
]


class DropRecord(NamedTuple):
    """One dropped job: ``bucket`` (a DROP_BUCKET_ORDER key), ``label`` ("<title> @ <company>"),
    ``detail`` (the human reason, matching the log line)."""

    bucket: str
    label: str
    detail: str


@dataclass(frozen=True)
class JobPosting:
    job_id: str
    title: str
    company: str
    location: str
    description: str
    url: str
    site: str
    date_posted: str
    min_amount: float | None = None
    max_amount: float | None = None
    currency: str | None = None
    experience_range: str | None = None
    job_type: str | None = None


def _stable_job_id(site: str, raw_id: str, url: str) -> str:
    if raw_id:
        return f"{site}:{raw_id}"
    digest = hashlib.sha1(url.encode(), usedforsecurity=False).hexdigest()[:12]
    return f"{site}:{digest}"


def _clean(value: Any) -> str:
    """Stringify a JobSpy cell, mapping NaN/None/'nan' to ''."""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _num(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN check


_YEARS_RE = re.compile(r"(\d+)")


def _parse_min_years(text: str | None) -> int | None:
    """Parse the minimum years from a Naukri ``experience_range`` (e.g. '2-4 Yrs').

    Returns 0 for fresher/entry-level, the leading integer for a range/'5+', and None when no
    number is stated (never fabricate).
    """
    if not text:
        return None
    t = text.strip().lower()
    if "fresher" in t or "entry" in t:
        return 0
    m = _YEARS_RE.search(t)
    return int(m.group(1)) if m else None


def normalize_rows(rows: list[dict[str, Any]]) -> list[JobPosting]:
    """Convert raw search rows to deduplicated :class:`JobPosting` objects."""
    out: list[JobPosting] = []
    seen: set[str] = set()
    for row in rows:
        url = str(row.get("job_url") or row.get("url") or "")
        site = str(row.get("site") or "")
        if not url and not row.get("id"):
            continue  # unusable row (no stable handle)
        job_id = _stable_job_id(site, str(row.get("id") or ""), url)
        if job_id in seen:
            continue  # within-batch dedup, first wins
        seen.add(job_id)
        out.append(
            JobPosting(
                job_id=job_id,
                title=_clean(row.get("title")),
                company=_clean(row.get("company")),
                location=_clean(row.get("location")),
                description=_clean(row.get("description")),
                url=url,
                site=site,
                date_posted=_clean(row.get("date_posted")),
                min_amount=_num(row.get("min_amount")),
                max_amount=_num(row.get("max_amount")),
                currency=_clean(row.get("currency")) or None,
                experience_range=_clean(row.get("experience_range")) or None,
                job_type=_clean(row.get("job_type")) or None,
            )
        )
    return out
