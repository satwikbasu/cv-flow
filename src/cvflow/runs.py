"""Pure run bodies for the bot's heavy work. Every dependency is injected.

These functions block for minutes (scraping, LLM calls, tectonic), so the bot
runs them in an executor thread; they must never import telegram. ``notify``
is a plain callable so progress and results reach the chat from any thread.
Single-run locking lives in the bot layer (one asyncio.Lock per verb), not here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from cvflow.analysis import resolve_tailoring_analysis
from cvflow.digest_summary import summarize_drops, write_discover_log
from cvflow.knowledge import KnowledgeBase
from cvflow.llm import LLMError
from cvflow.onboarding import OnboardingError
from cvflow.onboarding.applier import apply_bundle
from cvflow.onboarding.backends import GenerationBackend
from cvflow.onboarding.compile_repair import CompileOutcome, compile_with_repair
from cvflow.onboarding.parse import ResumeParseError, parse_resumes
from cvflow.onboarding.prompt import build_prompt
from cvflow.onboarding.report import render_report
from cvflow.render import filtered_footer, format_drop_report, render_digest
from cvflow.resume import CompileError, ResumeTailor, parse_master
from cvflow.statemachine import IllegalTransition, Status
from cvflow.storage import ApplicationStore, UnknownJob

__all__ = [
    "DiscoverOutcome",
    "OnboardOutcome",
    "run_discover",
    "run_tailor",
    "run_heartbeat",
    "run_onboard",
]


@dataclass(frozen=True)
class DiscoverOutcome:
    digest: str
    ordered_job_ids: list[str]
    expired: int


def run_discover(
    *,
    store: ApplicationStore,
    discovery: Any,
    notify: Callable[[str], None],
    retention_days: int,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    progress: Callable[[str], None] | None = None,
    report_drops: bool = False,
    summary_provider: Any = None,
    log_dir: str | None = None,
    summary_max_chars: int = 700,
    summary_samples: int = 3,
) -> DiscoverOutcome:
    """One full discovery run: sweep retention FIRST, discover, persist the slot
    order + digest, notify, then (optionally) send the drop report message.

    The retention sweep leads so expired jobs never occupy digest slots, and it
    keys off ``discovered_at`` — the fuzzy posting date plays no part in expiry.
    """
    expired = store.expire_stale(now(), retention_days)
    result = discovery.discover(progress=progress or (lambda _m: None))
    ordered = [bj.posting.job_id for bj in result.get("M", []) + result.get("N", [])]
    store.set_digest_slots(ordered)
    digest = render_digest(result, now=now())
    store.set_last_digest(digest)
    log_path = None
    if log_dir:
        chunks = format_drop_report(result.get("_drop_records", []))
        log_path = write_discover_log(digest, chunks, log_dir)
    notify(digest)
    if report_drops:
        summary = (
            summarize_drops(
                result.get("_drop_records", []),
                provider=summary_provider,
                max_chars=summary_max_chars,
                samples_per_bucket=summary_samples,
            )
            if summary_provider
            else None
        )
        msg = summary or filtered_footer(result.get("_dropped")) or "🚫 No jobs dropped."
        if log_path is not None:
            msg += f"\n📄 Full breakdown: {log_path}"
        notify(msg)
    return DiscoverOutcome(digest=digest, ordered_job_ids=ordered, expired=expired)


def run_tailor(
    job_id: str,
    *,
    store: ApplicationStore,
    tailor: Any,
    output_dir: str,
    use_jd_analysis: bool,
    analyzer: Any,
    notify: Callable[[str], None] = lambda _m: None,
    feedback: str | None = None,
) -> dict[str, str]:
    """Tailor one job to a compiled PDF and flip it to ``tailored``.

    The status check runs before any LLM or tectonic work, and the flip happens
    only AFTER a successful compile — a failed run never half-moves a job.
    """
    app = store.get(job_id)
    if app is None:
        raise UnknownJob(job_id)
    if app.status not in (Status.DISCOVERED, Status.TAILORED):
        raise IllegalTransition(f"cannot tailor a {app.status.value} job")
    analysis = resolve_tailoring_analysis(
        job_id, store=store, analyzer=analyzer, use_jd_analysis=use_jd_analysis, notify=notify
    )
    plan = tailor.plan(analysis, feedback=feedback)
    # Per-job filename so tailored resumes don't overwrite each other on disk.
    stem = "tailored-" + re.sub(r"[^A-Za-z0-9._-]", "-", job_id)
    pdf = tailor.compile_tailored(plan, output_dir, stem=stem)
    store.set_tailored(job_id, str(pdf))
    return {
        "job_id": job_id,
        "company": app.company,
        "role": app.role,
        "pdf_path": str(pdf),
        "diff": tailor.diff(plan),
    }


def run_heartbeat(store: ApplicationStore) -> str:
    """One-line liveness message with per-status counts."""
    counts = ", ".join(f"{s.value}={len(store.list_by_status(s))}" for s in Status)
    return f"💓 cv-flow alive — {counts}"


@dataclass(frozen=True)
class OnboardOutcome:
    report: list[str]
    pdf_path: Path | None
    flagged: bool
    ok: bool


def run_onboard(
    files: list[str | Path],
    *,
    backend: GenerationBackend,
    repo_root: str | Path,
    user_facts: dict[str, str],
    config_path: str | Path = "config.yaml",
    force: bool = False,
    attempts: int = 3,
    notify: Callable[[str], None] = lambda _m: None,
    compile_repair: Callable[..., CompileOutcome] = compile_with_repair,
) -> OnboardOutcome:
    """Résumé files -> generated profile + master résumé on disk -> compiled PDF.

    Nothing is applied unless parsing and generation both succeed, and
    ``apply_bundle`` leaves the live tree untouched when it fails.
    """
    notify("reading resume...")
    try:
        parsed = parse_resumes(files)
    except (ResumeParseError, OSError) as exc:
        return OnboardOutcome([f"Could not read your resume: {exc}"], None, True, False)
    prompt = build_prompt(parsed.text, user_facts)
    notify("generating candidate profile...")
    try:
        bundle = backend.generate_bundle(prompt)
    except (OnboardingError, LLMError) as exc:
        return OnboardOutcome(
            [f"Profile generation failed, nothing was written: {exc}"], None, True, False
        )
    notify("applying...")
    try:
        applied = apply_bundle(bundle, repo_root, config_path=config_path, force=force)
    except OnboardingError as exc:
        return OnboardOutcome([f"Could not apply the profile: {exc}"], None, False, False)
    notify("compiling resume...")
    outcome = compile_repair(applied.resume_dir, backend, attempts=attempts)
    kb = KnowledgeBase.load(applied.profile_dir, applied.profile_dir / "form_fields.json")
    flagged = outcome.flagged
    pdf_path: Path | None = None
    extra: list[str] = []
    if outcome.ok:
        out_dir = Path(repo_root) / "data" / "resumes"
        try:
            tailor = ResumeTailor(cast(Any, None), parse_master(applied.resume_dir))
            pdf_path = out_dir / "onboarding.pdf"
            tailor.compile_master(out_dir).replace(pdf_path)
        except CompileError as exc:
            pdf_path, flagged = None, True
            extra.append(f"Resume PDF could not be produced: {exc}")
    report = render_report(bundle, applied, outcome, kb) + extra
    if applied.staged_config is not None:
        report.append(
            f"Config blocks were staged at {applied.staged_config} (no config.yaml exists yet)."
        )
    return OnboardOutcome(report, pdf_path, flagged, ok=not flagged)
