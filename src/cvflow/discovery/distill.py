"""Distil one JD into a structured, enum-heavy Crux.

Engine: any OpenAI-compatible provider with ``generate_structured`` (default: Mistral
``mistral-small`` — fast, high throughput, strict JSON). The distiller is told never to infer
salary or years of experience — absent facts become null/"unknown".
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from cvflow.discovery import JobPosting

logger = logging.getLogger("cvflow.discovery.distill")

__all__ = ["Crux", "Salary", "Distiller", "distill_all", "DISTILL_VERSION"]

# Bump whenever the distill prompt or Crux schema changes — invalidates cached cruxes so the new
# extraction takes effect without a manual cache wipe.
DISTILL_VERSION = "4"


class Salary(BaseModel):
    min_amount: float | None
    max_amount: float | None
    currency: str
    period: Literal["year", "month", "hour", "unknown"]


ROLE_FAMILIES: tuple[str, ...] = (
    "devops", "sre", "platform", "infra", "backend", "fullstack",
    "frontend", "network", "sysadmin", "data", "security", "other",
)


class Crux(BaseModel):
    job_id: str
    role_family: Literal["devops", "sre", "platform", "infra", "backend", "fullstack",
                         "frontend", "network", "sysadmin", "data", "security", "other"]
    seniority_signal: Literal["fresher", "junior", "mid", "senior", "lead", "unknown"]
    min_years_required: int | None
    max_years_required: int | None
    work_mode: Literal["remote", "hybrid", "onsite", "unknown"]
    location_text: str
    country: Literal["india", "other", "global-remote"]
    stated_salary: Salary | None
    tech_stack: list[str]
    must_have_skills: list[str] = []  # JD-stated MANDATORY skills/quals (deal-breakers)
    night_shift_only: bool
    app_maintenance_focus: bool
    company_type: Literal["product", "service", "staffing", "unknown"]
    red_flags: list[Literal["unpaid", "commission_only", "vague", "scam"]]
    applicant_instructions: str | None
    one_line: str


_PREAMBLE = (
    "You extract structured facts from ONE job description into the provided schema.\n"
    "Rules:\n"
    "- Use ONLY facts present in the text. If a fact is not stated, output null "
    "(or \"unknown\" for enums).\n"
    "- NEVER infer or estimate salary or years of experience.\n"
    "- role_family: classify from the DESCRIPTION, not the title. Be STRICT — only use a "
    "specific family when the role genuinely IS that. Map: customer/technical SUPPORT or "
    "helpdesk -> other; manual QA / test / SDET -> other; ERP/CRM functional or developer "
    "(Odoo, SAP, Salesforce, Dynamics, ServiceNow) -> other; pure data-entry/ops -> other. "
    "devops/sre/platform/infra/sysadmin/backend ONLY for genuine engineering roles in those "
    "areas.\n"
    "- seniority_signal: classify from the DESCRIPTION, not just the title.\n"
    "- night_shift_only: true ONLY if night/rotational-on-call with no day option.\n"
    "- app_maintenance_focus: true ONLY if primarily long-term maintenance of a large "
    "existing application codebase.\n"
    "- country: 'india' if India-based or India/Asia-eligible remote; 'other' for a specific "
    "non-India country (US/Brazil/EU/etc., onsite OR that-country remote) or a JD written in a "
    "non-English language or naming a foreign city; 'global-remote' ONLY if it explicitly "
    "hires worldwide / from any country.\n"
    "- company_type: product vs service/consultancy/staffing vs unknown.\n"
    "- tech_stack: up to 8 concrete tools, lowercased, normalized (kubernetes->k8s).\n"
    "- must_have_skills: at most 5 CONCRETE NAMED TECHNOLOGIES (languages, frameworks, "
    "databases, tools, platforms) the JD explicitly marks mandatory with words like 'must "
    "have' / 'required' / 'mandatory' / 'minimum qualification'. A deterministic gate matches "
    "these against the candidate's skill list, so they MUST be specific tool names. NEVER "
    "output generic phrases ('backend development', 'system design', 'api design', 'data "
    "structures', 'software engineering', 'problem solving') — those are not skills to match; "
    "omit them. If the JD merely lists technologies or 'responsibilities', or states no "
    "mandatory named tool, return []. Do NOT dump the whole stack. Lowercased, normalized.\n"
    "- one_line: <=140 char neutral summary.\n"
    "- applicant_instructions: copy any explicit applicant directive verbatim, else null.\n"
    "- job_id MUST equal the provided job_id exactly.\n"
)


class _Provider(Protocol):
    def generate_structured(
        self, prompt: str, *, schema: Any, seed: int, max_output_tokens: int
    ) -> str: ...


class Distiller:
    def __init__(self, provider: _Provider, *, seed: int = 0, max_jd_chars: int = 12_000) -> None:
        self._provider = provider
        self._seed = seed
        self._max_jd_chars = max_jd_chars

    def _build_prompt(self, p: JobPosting) -> str:
        return (
            f"{_PREAMBLE}\n"
            f"job_id: {p.job_id}\n"
            f"title: {p.title}\ncompany: {p.company}\nlocation: {p.location}\n"
            f"experience_range_hint: {p.experience_range or ''}\n"
            f"--- JOB DESCRIPTION ---\n{p.description[: self._max_jd_chars]}\n"
        )

    def distill(self, posting: JobPosting) -> Crux:
        raw = self._provider.generate_structured(
            self._build_prompt(posting), schema=Crux, seed=self._seed, max_output_tokens=1024
        )
        crux = Crux.model_validate_json(raw)
        # The model occasionally echoes a wrong job_id; pin it to the real one.
        return crux.model_copy(update={"job_id": posting.job_id})


def distill_all(
    postings: list[JobPosting],
    store: Any,
    distiller: Distiller,
    on_failure: Callable[[JobPosting, Exception], None] | None = None,
) -> list[Crux]:
    """Cache-aware distillation. One bad JD never aborts the run; it is logged and reported
    to ``on_failure`` so the caller can surface it."""
    out: list[Crux] = []
    total = len(postings)
    for i, p in enumerate(postings, start=1):
        # Version-gated cache: a crux from an older prompt/schema version is ignored so the
        # current extraction takes effect without a manual cache wipe.
        cached = store.get_crux(p.job_id, version=DISTILL_VERSION)
        if cached is not None:
            try:
                out.append(Crux.model_validate_json(cached))
                logger.info("distill %d/%d %s (cached)", i, total, p.job_id)
                continue
            except Exception as exc:  # noqa: BLE001 — corrupt cache -> re-distill
                logger.warning("stale crux %s (%s); re-distilling", p.job_id, exc)
        t = time.monotonic()
        try:
            crux = distiller.distill(p)
        except Exception as exc:  # noqa: BLE001 — isolate a bad/garbled/over-budget JD
            logger.warning("distill %d/%d %s FAILED: %s", i, total, p.job_id, exc)
            if on_failure is not None:
                on_failure(p, exc)
            continue
        store.save_crux(p.job_id, crux.model_dump_json(), version=DISTILL_VERSION)
        logger.info("distill %d/%d %s ok (%.1fs)", i, total, p.job_id, time.monotonic() - t)
        out.append(crux)
    return out
