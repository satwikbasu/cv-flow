"""Discovery: multi-board search -> cross-day dedup -> LLM ranking -> top-N.

Scraping sits behind an injectable ``search_fn`` (default: JobSpy). That seam lets a different
source (say a hosted jobs API) slot in without touching dedup, ranking, or storage. JobSpy is the
only zero-cost self-hosted default.

Ranking only ever reorders real postings; it never invents jobs — any job_id the ranker returns
that was not a candidate is dropped.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, NamedTuple

from cvflow.dates import parse_posting_date
from cvflow.statemachine import Status

__all__ = [
    "JobPosting",
    "BenchmarkedJob",
    "DiscoveryService",
    "normalize_rows",
    "DropRecord",
]

logger = logging.getLogger("cvflow.discovery")

# A job blocked by Naukri's datacenter-IP filter looks identical to "no results" unless we say so.
NAUKRI_UNREACHABLE_NOTICE = "Naukri unreachable today"

# exclude_when crux field -> human bucket for the digest "filtered" footer.
_EXCLUDE_BUCKET = {
    "seniority_signal": "too senior",
    "min_years_required": "over-experience",
    "country": "abroad",
    "night_shift_only": "night shift",
    "app_maintenance_focus": "maintenance",
    "red_flags": "red flag",
}
# Stable display order for the footer; only non-zero buckets are shown.
DROP_BUCKET_ORDER = [
    "too senior", "over-experience", "abroad", "wrong stack", "low pay",
    "night shift", "maintenance", "red flag", "other filter", "already seen", "capped",
    "distill failed",
]


def _bump(drops: dict[str, int], bucket: str, n: int = 1) -> None:
    drops[bucket] = drops.get(bucket, 0) + n


class DropRecord(NamedTuple):
    """One dropped job: ``bucket`` (a DROP_BUCKET_ORDER key), ``label`` ("<title> @ <company>"),
    ``detail`` (the human reason, matching the log line)."""

    bucket: str
    label: str
    detail: str


def _label(p: JobPosting) -> str:
    return f"{p.title or 'Untitled'} @ {p.company or 'Unknown company'}"


def _drop(
    drops: dict[str, int], records: list[DropRecord], bucket: str, p: JobPosting, detail: str
) -> None:
    _bump(drops, bucket)
    records.append(DropRecord(bucket, _label(p), detail))


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


SearchFn = Callable[..., list[dict[str, Any]]]

# Imported here (not at module top) because these modules import JobPosting from this package —
# JobPosting must be defined first to avoid a circular import.
from cvflow.discovery.benchmark import (  # noqa: E402
    BenchmarkedJob,
    benchmark_cohort,
    effective_lpa,
    fit_scores,
)
from cvflow.discovery.distill import Crux, Distiller, distill_all  # noqa: E402
from cvflow.discovery.naukri import search_naukri  # noqa: E402
from cvflow.discovery.rules import crux_excluded  # noqa: E402
from cvflow.discovery.skills import coverage_drop  # noqa: E402


def _jobspy_search(
    *,
    site_name: list[str],
    search_term: str,
    location: str,
    results_wanted: int,
    hours_old: int,
    country_indeed: str = "usa",
    linkedin_fetch_description: bool = False,
) -> list[dict[str, Any]]:
    from jobspy import scrape_jobs

    df = scrape_jobs(
        site_name=site_name,
        search_term=search_term,
        location=location,
        results_wanted=results_wanted,
        hours_old=hours_old,
        country_indeed=country_indeed,
        linkedin_fetch_description=linkedin_fetch_description,
        enforce_annual_salary=True,
    )
    if df is None or df.empty:
        return []
    return list(df.to_dict("records"))


class DiscoveryService:
    """Orchestrates search -> cross-day dedup -> ranking -> persist -> top-N."""

    def __init__(
        self,
        store: Any,
        search_fn: SearchFn = _jobspy_search,
        naukri_search_fn: SearchFn = search_naukri,
        *,
        search_terms: list[str],
        locations: list[str],
        sites: list[str],
        results_wanted_per_site: int,
        hours_old: int,
        throttle_seconds: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], date] = date.today,
        exclude_title_keywords: list[str] | None = None,
        min_ctc_lpa: int = 0,
        country_indeed: str = "usa",
        linkedin_fetch_description: bool = False,
        max_rank_candidates: int = 40,
        distiller: Any = None,
        fingerprint: str = "",
        prefer_roles: dict[str, float] | None = None,
        exclude_when: list[dict[str, Any]] | None = None,
        fit_weight: float = 0.70,
        comp_weight: float = 0.30,
        top_ctc_lpa: int = 40,
        max_distill_per_cohort: int = 60,
        top_n_per_cohort: int = 5,
        distill_seed: int = 73,
        yoe_ceiling: int | None = None,
        reconsider_discovered: bool = False,
        candidate_skills: frozenset[str] = frozenset(),
        skill_synonyms: dict[str, str] | None = None,
        max_missing_skill_ratio: float = 0.5,
    ) -> None:
        self._store = store
        self._search_fn = search_fn
        self._today = today
        self._yoe_ceiling = yoe_ceiling
        self._reconsider_discovered = reconsider_discovered
        self._search_terms = search_terms
        self._locations = locations
        self._sites = sites
        # Naukri's API needs our own nkparam-signed client (JobSpy's naukri 406s); split it out of
        # the JobSpy site list and fetch it via the dedicated adapter.
        self._naukri_search_fn = naukri_search_fn
        self._naukri_enabled = "naukri" in sites
        self._jobspy_sites = [s for s in sites if s != "naukri"]
        self._results_wanted_per_site = results_wanted_per_site
        self._hours_old = hours_old
        self._throttle_seconds = throttle_seconds
        self._sleep = sleep
        self._exclude_title_keywords = [k.lower() for k in (exclude_title_keywords or [])]
        self._min_ctc_lpa = min_ctc_lpa
        self._country_indeed = country_indeed
        self._linkedin_fetch_description = linkedin_fetch_description
        self._max_rank_candidates = max_rank_candidates
        self._distiller_provider = distiller
        self._fingerprint = fingerprint
        self._prefer_roles = prefer_roles or {}
        self._exclude_when = exclude_when or []
        self._fit_weight = fit_weight
        self._comp_weight = comp_weight
        self._top_ctc_lpa = top_ctc_lpa
        self._max_distill_per_cohort = max_distill_per_cohort
        self._top_n_per_cohort = top_n_per_cohort
        self._distill_seed = distill_seed
        self._candidate_skills = candidate_skills
        self._skill_synonyms = skill_synonyms or {}
        self._max_missing_skill_ratio = max_missing_skill_ratio
        # Per-source health, recomputed each run (see _gather_rows / _source_notices).
        self._naukri_attempted = False
        self._naukri_reached = False

    def _gather_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for term in self._search_terms:
            for location in self._locations:
                if self._jobspy_sites:
                    try:
                        rows.extend(
                            self._search_fn(
                                site_name=self._jobspy_sites,
                                search_term=term,
                                location=location,
                                results_wanted=self._results_wanted_per_site,
                                hours_old=self._hours_old,
                                country_indeed=self._country_indeed,
                                linkedin_fetch_description=self._linkedin_fetch_description,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        # Board scraping is brittle (anti-bot 403s, endpoint drift, a single
                        # unparseable posting). One failing batch must never abort the whole run
                        # or silence the digest — log and carry on.
                        logger.warning(
                            "discovery search failed for term=%r location=%r: %s",
                            term, location, exc,
                        )
                if self._naukri_enabled:
                    self._naukri_attempted = True
                    try:
                        rows.extend(
                            self._naukri_search_fn(
                                search_term=term,
                                location=location,
                                results_wanted=self._results_wanted_per_site,
                                hours_old=self._hours_old,
                            )
                        )
                        # Returning at all (even an empty list) means we reached Naukri's API.
                        self._naukri_reached = True
                    except Exception as exc:  # noqa: BLE001 — Naukri must never abort the run
                        logger.warning(
                            "naukri search failed for term=%r location=%r: %s",
                            term, location, exc,
                        )
                self._sleep(self._throttle_seconds)
        return rows

    def _source_notices(self) -> list[str]:
        """Per-source health lines for the digest. Naukri attempted on every query but never
        reached means its datacenter-IP block is active — say so instead of degrading silently."""
        notices: list[str] = []
        if self._naukri_enabled and self._naukri_attempted and not self._naukri_reached:
            notices.append(NAUKRI_UNREACHABLE_NOTICE)
        return notices

    def _prefilter(
        self, postings: list[JobPosting], drops: dict[str, int], records: list[DropRecord]
    ) -> list[JobPosting]:
        kept: list[JobPosting] = []
        for p in postings:
            title = p.title.lower()
            if any(k in title for k in self._exclude_title_keywords):
                logger.info("prefilter drop (title) %s: %s", p.job_id, p.title)
                _drop(drops, records, "other filter", p, "excluded title keyword")
                continue
            # Internships are a hard deal-breaker (full-time only). JobSpy's structured job_type is
            # reliable when present; an absent job_type is never dropped.
            if p.job_type and "intern" in p.job_type.lower():
                logger.info("prefilter drop (internship) %s: %s", p.job_id, p.job_type)
                _drop(drops, records, "other filter", p, "internship")
                continue
            cap = p.max_amount if p.max_amount is not None else p.min_amount
            # only filter on salary when stated AND in INR (else keep + let the ranker flag it)
            if cap is not None and (p.currency or "INR").upper() == "INR":
                if cap / 100_000 < self._min_ctc_lpa:
                    logger.info("prefilter drop (salary) %s: %s", p.job_id, cap)
                    _drop(drops, records, "low pay",
                          p, f"{cap / 100_000:.1f} LPA < {self._min_ctc_lpa}")
                    continue
            # Naukri structured YOE gate (when experience_range is stated): drop only on a parsed
            # minimum above the ceiling — an unparseable/absent range is kept.
            if self._yoe_ceiling is not None and p.experience_range:
                min_years = _parse_min_years(p.experience_range)
                if min_years is not None and min_years > self._yoe_ceiling:
                    logger.info(
                        "prefilter drop (experience) %s: %s", p.job_id, p.experience_range
                    )
                    _drop(drops, records, "over-experience",
                          p, f"{min_years}y > ceiling {self._yoe_ceiling}")
                    continue
            kept.append(p)
        return kept

    def _rank_cohort(
        self, cohort: str, cruxes: list[Crux], by_id: dict[str, JobPosting]
    ) -> list[BenchmarkedJob]:
        """Fit-score + benchmark one already-distilled, already-partitioned cohort."""
        if not cruxes:
            return []
        jobs = {c.job_id: by_id[c.job_id] for c in cruxes}
        t = time.monotonic()
        # Fit runs on the distillation provider (Mistral) — it enforces the json_schema array
        # shape that some hosts ignore (they emit a single object instead).
        fits = fit_scores(
            cruxes, fingerprint=self._fingerprint,
            prefer_roles=self._prefer_roles, provider=self._distiller_provider,
        )
        logger.info(
            "cohort %s: fit-scored %d cruxes in %.1fs (1 call)",
            cohort, len(cruxes), time.monotonic() - t,
        )
        ranked = benchmark_cohort(
            jobs, fits, cohort=cohort, fit_weight=self._fit_weight,
            comp_weight=self._comp_weight, min_lpa=self._min_ctc_lpa,
            top_lpa=self._top_ctc_lpa,
            cruxes={c.job_id: c for c in cruxes},
        )
        return ranked[: self._top_n_per_cohort]

    def _already_seen(self, job_id: str) -> bool:
        """Cross-day dedup. Normally any job in the store is 'seen'. With
        ``reconsider_discovered``, a job that's only ever been DISCOVERED (never acted on) is
        re-rankable — only jobs advanced past discovery stay excluded."""
        if not self._store.exists(job_id):
            return False
        if self._reconsider_discovered:
            app = self._store.get(job_id)
            return app is not None and app.status != Status.DISCOVERED
        return True

    def discover(
        self, progress: Callable[[str], None] = lambda _msg: None
    ) -> dict[str, Any]:
        t0 = time.monotonic()
        self._naukri_attempted = False
        self._naukri_reached = False
        today = self._today()
        drops: dict[str, int] = {}
        records: list[DropRecord] = []
        rows = self._gather_rows()
        scrape_secs = time.monotonic() - t0
        logger.info("stage scrape: %d raw rows in %.1fs", len(rows), scrape_secs)
        progress(f"📡 Scraped {len(rows)} raw postings in {scrape_secs:.0f}s")
        normalized = normalize_rows(rows)
        postings = self._prefilter(normalized, drops, records)
        candidates: list[JobPosting] = []
        for p in postings:
            if self._already_seen(p.job_id):
                _drop(drops, records, "already seen", p, "already shown before")
            else:
                candidates.append(p)
        logger.info(
            "stage prefilter+dedup: %d candidates (from %d unique of %d raw)",
            len(candidates), len(normalized), len(rows),
        )
        # Report the funnel honestly: candidates AFTER the title/salary/internship prefilter and
        # cross-day dedup, out of the unique normalized postings (the big 'raw -> unique' and
        # 'unique -> candidates' cuts both happen here, so show the unique baseline).
        progress(
            f"🧹 {len(candidates)} candidates after prefilter + dedup "
            f"(from {len(normalized)} unique postings)"
        )
        # Distill ONCE over the candidate pool, then partition — salary is usually only in the JD
        # text (JobSpy structured fields are empty), so the M/N split must use the crux-extracted
        # salary, not the pre-distill structured field. Keep gather order (not a date sort:
        # date_posted strings are inconsistent across sources — ISO vs '2 Days Ago' — so sorting
        # them is meaningless). Gather order already front-loads the higher-priority search terms.
        capped = candidates[: self._max_distill_per_cohort]
        if len(candidates) > self._max_distill_per_cohort:
            overflow = len(candidates) - self._max_distill_per_cohort
            _bump(drops, "capped", overflow)
            records.append(
                DropRecord("capped", "(summary)",
                           f"{overflow} jobs beyond the distill cap (not distilled this run)")
            )
            logger.info(
                "distill cap: %d of %d candidates (raise max_distill_per_cohort for more)",
                self._max_distill_per_cohort, len(candidates),
            )
            progress(f"✂️ Distill cap: {self._max_distill_per_cohort} of "
                     f"{len(candidates)} candidates ({overflow} deferred)")
        by_id = {p.job_id: p for p in capped}
        distiller = Distiller(self._distiller_provider, seed=self._distill_seed)
        t = time.monotonic()
        cruxes = distill_all(
            capped, self._store, distiller,
            on_failure=lambda p, exc: _drop(
                drops, records, "distill failed", p, f"{type(exc).__name__}: {exc}"
            ),
        )
        distill_secs = time.monotonic() - t
        logger.info(
            "stage distill: %d/%d jobs in %.1fs", len(cruxes), len(capped), distill_secs
        )
        progress(f"🧪 Distilled {len(cruxes)}/{len(capped)} JDs in {distill_secs:.0f}s")
        m_cruxes: list[Crux] = []
        n_cruxes: list[Crux] = []
        for c in cruxes:
            p = by_id[c.job_id]
            excluded, reason = crux_excluded(c.model_dump(), self._exclude_when)
            if excluded:
                field = reason.split()[0] if reason else ""
                _drop(drops, records, _EXCLUDE_BUCKET.get(field, "other filter"),
                      p, reason or "excluded by rule")
                logger.info("exclude_when drop %s: %s", c.job_id, reason)
                continue
            # Deterministic must-have skill gate: drop jobs whose stated mandatory skills are
            # mostly ones the candidate lacks (a hard requirement — enforced in code, never
            # delegated to the fit LLM, which over-credits incidental overlap).
            if self._candidate_skills:
                drop, missing = coverage_drop(
                    c.must_have_skills, self._candidate_skills, self._skill_synonyms,
                    max_missing_ratio=self._max_missing_skill_ratio,
                )
                if drop:
                    _drop(drops, records, "wrong stack", p, f"missing {', '.join(missing)}")
                    logger.info("must-have drop %s: missing %s", c.job_id, missing)
                    continue
            lpa = effective_lpa(by_id[c.job_id], c)
            if lpa is not None and lpa < self._min_ctc_lpa:
                _drop(drops, records, "low pay", p, f"{lpa:.1f} LPA < {self._min_ctc_lpa}")
                logger.info("salary-floor drop %s: %.1f LPA < %d", c.job_id, lpa, self._min_ctc_lpa)
                continue
            (m_cruxes if lpa is not None else n_cruxes).append(c)
        logger.info(
            "cohorts after gate+partition: M (stated INR pay)=%d, N (no stated pay)=%d",
            len(m_cruxes), len(n_cruxes),
        )
        progress(f"📊 Cohorts — 💰{len(m_cruxes)} stated-pay · 📋{len(n_cruxes)} no-pay")
        result: dict[str, Any] = {
            "M": self._rank_cohort("M", m_cruxes, by_id),
            "N": self._rank_cohort("N", n_cruxes, by_id),
        }
        for cohort in (result["M"], result["N"]):
            for bj in cohort:
                p = bj.posting
                if not self._store.exists(p.job_id):
                    parsed = parse_posting_date(p.date_posted, today)
                    self._store.add(
                        p.job_id, p.company, p.title, p.url,
                        date_posted=p.date_posted,
                        date_posted_parsed=parsed.isoformat() if parsed else None,
                    )
                # Persist ranking signals (upsert every run — scores can change) and the raw JD
                # text once, so tailoring works later without re-fetching a walled URL.
                self._store.set_discovery_meta(
                    p.job_id, benchmark=bj.benchmark, fit_score=bj.fit_score,
                    fit_reason=bj.fit_reason, concerns=bj.concerns, cohort=bj.cohort,
                    ctc_lpa=bj.ctc_lpa,
                )
                if p.description and self._store.get_jd_text(p.job_id) is None:
                    self._store.set_jd_text(p.job_id, p.description)
        logger.info(
            "discover total: %.1fs — presenting M=%d, N=%d (filtered: %s)",
            time.monotonic() - t0, len(result["M"]), len(result["N"]), drops,
        )
        result["_dropped"] = drops  # footer-only metadata
        result["_drop_records"] = records  # grouped drop report
        result["_notices"] = self._source_notices()  # blocked-source lines for the digest
        return result
