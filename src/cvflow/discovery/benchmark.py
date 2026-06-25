"""Hybrid benchmark and ranking.

The LLM scores only ``fit``; code owns ``comp`` and the blend. India-focused: no FX and no
location score (a single-currency, single-market build).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from cvflow.discovery import JobPosting

logger = logging.getLogger("cvflow.discovery.benchmark")

__all__ = [
    "FitResult",
    "BenchmarkedJob",
    "comp_score",
    "benchmark_cohort",
    "fit_scores",
    "build_fingerprint",
    "crux_salary_lpa",
    "effective_lpa",
    "FIT_SEED",
]

FIT_SEED = 1409


@dataclass(frozen=True)
class FitResult:
    fit_score: int
    fit_reason: str
    concerns: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BenchmarkedJob:
    posting: JobPosting
    benchmark: int
    fit_score: int
    fit_reason: str
    concerns: list[str]
    cohort: str            # "M" | "N"
    ctc_lpa: float | None = None

    @property
    def job_id(self) -> str:
        return self.posting.job_id


def comp_score(ctc_lpa: float, min_lpa: int, top_lpa: int) -> float:
    """0 at the floor, 1 at the top anchor, clamped. INR only (no FX)."""
    span = max(top_lpa - min_lpa, 1)
    return max(0.0, min(1.0, (ctc_lpa - min_lpa) / span))


def _ctc_lpa(posting: JobPosting) -> float | None:
    amount = posting.max_amount if posting.max_amount is not None else posting.min_amount
    if amount is None or (posting.currency or "INR").upper() != "INR":
        return None
    return amount / 100_000


_INR_TOKENS = {"INR", "RS", "RS.", "₹", "RUPEES", "INR."}


def crux_salary_lpa(crux: Any) -> float | None:
    """Annual INR LPA from a crux's extracted ``stated_salary`` (the JD often states pay that
    JobSpy's structured fields miss). Handles ₹/INR and monthly->annual; non-INR or hourly/unknown
    -> None (no FX, never fabricate)."""
    s = getattr(crux, "stated_salary", None) if crux is not None else None
    if s is None:
        return None
    cur = (s.currency or "").upper().strip()
    if cur not in _INR_TOKENS and "₹" not in (s.currency or ""):
        return None
    amount = s.max_amount if s.max_amount is not None else s.min_amount
    if amount is None:
        return None
    annual = float(amount) * 12 if s.period == "month" else float(amount)
    if s.period not in ("year", "month", "unknown"):
        return None  # hourly etc. — don't guess
    return annual / 100_000


def effective_lpa(posting: JobPosting, crux: Any) -> float | None:
    """Best INR annual LPA for a job: prefer the crux-extracted salary, else JobSpy's structured
    field. None when no INR salary is stated anywhere."""
    return crux_salary_lpa(crux) if crux_salary_lpa(crux) is not None else _ctc_lpa(posting)


def benchmark_cohort(
    jobs: dict[str, JobPosting],
    fits: dict[str, FitResult],
    *,
    cohort: str,
    fit_weight: float,
    comp_weight: float,
    min_lpa: int,
    top_lpa: int,
    cruxes: dict[str, Any] | None = None,
) -> list[BenchmarkedJob]:
    """Score and sort one cohort. M blends fit+comp; N is fit only.

    Adds code-side deterministic concerns the LLM never emits: PAY_UNKNOWN when there's no stated
    INR salary, YOE_UNKNOWN when the crux didn't state a minimum experience (never fabricate).
    """
    cruxes = cruxes or {}
    out: list[BenchmarkedJob] = []
    for job_id, posting in jobs.items():
        fit = fits.get(job_id, FitResult(0, "(no fit score)", ["RANKING_DEGRADED"]))
        lpa = effective_lpa(posting, cruxes.get(job_id))
        if cohort == "M" and lpa is not None:
            c = comp_score(lpa, min_lpa, top_lpa)
            score = round(100 * (fit_weight * fit.fit_score / 100 + comp_weight * c))
        else:
            score = round(100 * (fit.fit_score / 100))
        concerns = list(fit.concerns)
        crux = cruxes.get(job_id)
        for code, applies in (
            ("PAY_UNKNOWN", lpa is None),
            ("YOE_UNKNOWN", crux is not None and crux.min_years_required is None),
        ):
            if applies and code not in concerns:
                concerns.append(code)
        out.append(
            BenchmarkedJob(
                posting=posting, benchmark=score, fit_score=fit.fit_score,
                fit_reason=fit.fit_reason, concerns=concerns, cohort=cohort,
                ctc_lpa=lpa,
            )
        )
    out.sort(key=lambda j: j.benchmark, reverse=True)
    return out


class _Provider(Protocol):
    def generate(self, prompt: str, **kwargs: Any) -> str: ...


def build_fingerprint(*, prefs_text: str, prefer_roles: dict[str, float]) -> str:
    roles = ", ".join(f"{k}={v}" for k, v in sorted(prefer_roles.items()))
    return f"{prefs_text}\nPreferred role families (weight): {roles}\n"


_FIT_PREAMBLE = (
    "You score job FIT (0-100) for a candidate from compact job 'cruxes'.\n"
    "ROLE FIT IS THE PRIMARY DRIVER; stack overlap is secondary and only matters once the "
    "role fits. Use the given role_family weight:\n"
    "  85-100: weight>=0.8 AND real overlap with the candidate's core tools AND seniority "
    "fresher/junior/mid.\n"
    "  65-84 : weight>=0.8 with weak overlap, OR weight 0.5-0.8 with strong overlap.\n"
    "  40-64 : weight 0.3-0.5 (adjacent role) with some overlap.\n"
    "  0-39  : weight<0.3 OR an unrelated role (e.g. support/helpdesk, QA/test, ERP/CRM, "
    "pure frontend, data-entry) EVEN IF a few tools coincide.\n"
    "A shared tool or two (e.g. Linux, Docker, Python) does NOT lift an unrelated/low-weight "
    "role above 39 — do not reward incidental overlap.\n"
    "Modifiers (after the band): -10 service/staffing company; +5 modern infra stack "
    "(docker/k8s/ci-cd/cloud). Clamp 0-100.\n"
    "must_have_skills is given only as context for judging overlap. A deterministic code "
    "gate has ALREADY dropped jobs the candidate is unqualified for, so do NOT re-judge skill "
    "coverage, do NOT cap on it, and do NOT emit MISSING_MUST_HAVE.\n"
    'Return a JSON object {"results": [ ... ]} with ONE entry per given job: '
    '{"job_id","fit_score","fit_reason"(<=120 chars),'
    '"concern_codes"(subset of STACK_MISMATCH,SERVICE_COMPANY,SENIORITY_BORDERLINE,'
    "ROLE_ADJACENT)}. Score EVERY job_id given; use only the given job_ids.\n"
)

# Strict JSON-schema for the fit batch — some hosts ignore schemas and emit a single object, so
# fit runs on the distillation provider (Mistral), which enforces this exactly.
_FIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "fit_score": {"type": "integer"},
                    "fit_reason": {"type": "string"},
                    "concern_codes": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["job_id", "fit_score", "fit_reason", "concern_codes"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def _fit_view(crux: Any) -> dict[str, Any]:
    return {
        "job_id": crux.job_id, "role_family": crux.role_family,
        "seniority_signal": crux.seniority_signal, "tech_stack": crux.tech_stack,
        "must_have_skills": getattr(crux, "must_have_skills", []),
        "work_mode": crux.work_mode, "country": crux.country,
        "company_type": crux.company_type, "one_line": crux.one_line,
    }


def _salvage_objects(raw: str) -> list[Any]:
    """Recover top-level JSON objects from a malformed array (missing commas, truncated tail).
    Scans brace depth while respecting string literals; each balanced {...} that parses is kept,
    the rest dropped. One garbled entry never zeroes the cohort."""
    out: list[Any] = []
    depth = 0
    start: int | None = None
    in_str = False
    esc = False
    for i, ch in enumerate(raw):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        out.append(json.loads(raw[start : i + 1]))
                    except ValueError:
                        pass
                    start = None
    return out


def _unwrap_array(raw: str) -> str:
    """json_object mode may return {"jobs":[...]} or {"results":[...]}; find the array."""
    raw = raw.strip()
    if raw.startswith("["):
        return raw
    obj = json.loads(raw)
    if isinstance(obj, list):
        return raw
    for v in obj.values():
        if isinstance(v, list):
            return json.dumps(v)
    return "[]"


def fit_scores(
    cruxes: list[Any],
    *,
    fingerprint: str,
    prefer_roles: dict[str, float],
    provider: _Provider,
) -> dict[str, FitResult]:
    """One ranking call over all cruxes. On failure, every job degrades to fit 0 plus a flag."""
    if not cruxes:
        return {}
    ids = {c.job_id for c in cruxes}
    jobs_json = json.dumps(
        [_fit_view(c) for c in sorted(cruxes, key=lambda c: c.job_id)], indent=2
    )
    roles = ", ".join(f"{k}={v}" for k, v in sorted(prefer_roles.items()))
    prompt = (
        f"{_FIT_PREAMBLE}\nPreferred role families (weight): {roles}\n\n"
        f"## Candidate\n{fingerprint}\n\n## Jobs\n{jobs_json}\n"
    )
    try:
        # json_schema (not top_p / json_object): Mistral enforces the exact array shape;
        # top_p is omitted because temperature=0 is already greedy (Mistral rejects top_p<1).
        raw = provider.generate(
            prompt, temperature=0, seed=FIT_SEED,
            max_tokens=min(8192, 130 * len(cruxes) + 500),
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "FitBatch", "strict": True, "schema": _FIT_SCHEMA},
            },
        )
    except Exception as exc:  # noqa: BLE001 — degrade, never lose the cohort
        logger.warning("fit scoring call failed (%s); degrading cohort to fit 0", exc)
        return {jid: FitResult(0, "(ranking unavailable)", ["RANKING_DEGRADED"]) for jid in ids}
    # response_format=json_object may wrap the array in an object; accept either, and salvage
    # individual objects if the model emits slightly-malformed JSON (missing commas, a truncated
    # tail) so one bad token doesn't zero the whole cohort.
    try:
        parsed: list[Any] = json.loads(_unwrap_array(raw))
        if not isinstance(parsed, list):
            parsed = _salvage_objects(raw)
    except Exception:  # noqa: BLE001
        parsed = _salvage_objects(raw)
    if not parsed:
        logger.warning(
            "fit scoring unparseable (raw[:200]=%r); degrading cohort to fit 0", raw[:200]
        )
        return {jid: FitResult(0, "(ranking unavailable)", ["RANKING_DEGRADED"]) for jid in ids}
    out: dict[str, FitResult] = {}
    for entry in parsed if isinstance(parsed, list) else []:
        if not isinstance(entry, dict):  # the ranker sometimes returns bare strings — skip
            continue
        jid = str(entry.get("job_id"))
        if jid not in ids:
            continue
        out[jid] = FitResult(
            fit_score=int(entry.get("fit_score", 0) or 0),
            fit_reason=str(entry.get("fit_reason", "")),
            concerns=[str(c) for c in entry.get("concern_codes", [])],
        )
    for jid in ids:  # any job the model omitted still gets a row
        out.setdefault(jid, FitResult(0, "(omitted by ranker)", ["RANKING_DEGRADED"]))
    return out
