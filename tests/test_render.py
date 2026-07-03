"""Tests for the pure digest/backlog renderers (no bot imports here)."""

from datetime import UTC, datetime

from cvflow.discovery import DropRecord, JobPosting
from cvflow.discovery.benchmark import BenchmarkedJob
from cvflow.render import format_drop_report, render_digest, render_jobs
from cvflow.statemachine import Status
from cvflow.storage import Application

NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


def _posting(job_id: str = "in:1", title: str = "Backend Dev", date_posted: str = "") -> JobPosting:
    return JobPosting(
        job_id=job_id,
        title=title,
        company="Acme",
        location="Pune",
        description="",
        url=f"https://example.test/{job_id}",
        site="indeed",
        date_posted=date_posted,
    )


def _bj(
    job_id: str = "in:1",
    cohort: str = "M",
    benchmark: int = 80,
    ctc_lpa: float | None = 12.0,
    concerns: list[str] | None = None,
    date_posted: str = "",
) -> BenchmarkedJob:
    return BenchmarkedJob(
        posting=_posting(job_id, date_posted=date_posted),
        benchmark=benchmark,
        fit_score=8,
        fit_reason="Stack matches",
        concerns=concerns or [],
        cohort=cohort,
        ctc_lpa=ctc_lpa,
    )


# --- render_digest ---


def test_digest_numbers_continuously_across_cohorts() -> None:
    result = {
        "M": [_bj("m:1"), _bj("m:2")],
        "N": [_bj("n:1", cohort="N", ctc_lpa=None)],
    }
    text = render_digest(result, now=NOW)
    assert "1. " in text and "2. " in text and "3. " in text
    assert text.index("💰") < text.index("📋")
    assert "https://example.test/m:1" in text
    assert "Stack matches" in text


def test_digest_empty_says_no_new_jobs() -> None:
    assert "No new jobs today." in render_digest({"M": [], "N": []}, now=NOW)


def test_digest_empty_still_shows_notices() -> None:
    text = render_digest(
        {"M": [], "N": [], "_notices": ["Naukri unreachable today"]}, now=NOW
    )
    assert "No new jobs today." in text
    assert "Naukri unreachable today" in text


def test_digest_shows_posted_age_when_known_and_found_today_when_not() -> None:
    result = {
        "M": [_bj("m:1", date_posted="2026-06-30")],  # 3 days before NOW
        "N": [_bj("n:2", cohort="N", ctc_lpa=None)],  # no posting date
    }
    text = render_digest(result, now=NOW)
    assert "posted 3d ago" in text
    assert "found today" in text


def test_digest_shows_pay_and_concerns() -> None:
    result = {"M": [_bj("m:1", ctc_lpa=12.4, concerns=["contract role"])], "N": []}
    text = render_digest(result, now=NOW)
    assert "12 LPA" in text
    assert "⚠️ contract role" in text


def test_digest_has_tailor_skip_affordance_and_no_drop_info() -> None:
    result = {"M": [_bj("m:1")], "N": [], "_dropped": {"low pay": 4}}
    text = render_digest(result, now=NOW)
    assert "/tailor" in text and "/skip" in text
    assert "low pay" not in text  # drop info travels separately, never in the digest


def test_digest_shows_notice_alongside_jobs() -> None:
    result = {"M": [_bj("m:1")], "N": [], "_notices": ["Naukri unreachable today"]}
    text = render_digest(result, now=NOW)
    assert "Naukri unreachable today" in text


# --- render_jobs ---


def _app(
    job_id: str,
    status: Status = Status.DISCOVERED,
    benchmark: int | None = 70,
    discovered_at: str = "2026-07-01T09:00:00+00:00",
    date_posted_parsed: str | None = None,
) -> Application:
    return Application(
        job_id=job_id,
        company="Acme",
        role="Backend Dev",
        jd_url=f"https://example.test/{job_id}",
        status=status,
        discovered_at=discovered_at,
        date_posted_parsed=date_posted_parsed,
        benchmark=benchmark,
        fit_score=7,
    )


def test_jobs_open_lists_discovered_sorted_by_benchmark() -> None:
    apps = [
        _app("a", benchmark=50),
        _app("b", benchmark=90),
        _app("c", status=Status.TAILORED, benchmark=99),
    ]
    text, slots = render_jobs(apps, "open", now=NOW)
    assert slots == [(1, "b"), (2, "a")]  # benchmark desc, tailored excluded
    assert text.index("1. ") < text.index("2. ")
    assert "found 2d ago" in text  # discovered 2026-07-01 vs NOW


def test_jobs_tailored_filter_shows_history_only() -> None:
    apps = [_app("a"), _app("b", status=Status.TAILORED)]
    text, slots = render_jobs(apps, "tailored", now=NOW)
    assert slots == [(1, "b")]


def test_jobs_all_shows_both_and_marks_tailored() -> None:
    apps = [_app("a", benchmark=90), _app("b", status=Status.TAILORED, benchmark=50)]
    text, slots = render_jobs(apps, "all", now=NOW)
    assert slots == [(1, "a"), (2, "b")]
    assert "tailored" in text.lower()


def test_jobs_uses_posting_date_when_parsed() -> None:
    apps = [_app("a", date_posted_parsed="2026-06-28")]
    text, _ = render_jobs(apps, "open", now=NOW)
    assert "posted 5d ago" in text


def test_jobs_empty_is_friendly() -> None:
    text, slots = render_jobs([], "open", now=NOW)
    assert slots == []
    assert "/discover" in text  # points the user at the next step


def test_jobs_none_benchmark_sorts_last() -> None:
    apps = [_app("a", benchmark=None), _app("b", benchmark=10)]
    _, slots = render_jobs(apps, "open", now=NOW)
    assert slots == [(1, "b"), (2, "a")]


# --- format_drop_report ---


def test_drop_report_groups_by_bucket_in_order() -> None:
    records = [
        DropRecord("too senior", "Snr @ Corp", "needs 10y"),
        DropRecord("low pay", "Dev @ Cheap", "3 LPA"),
        DropRecord("low pay", "Dev @ Cheaper", "2 LPA"),
    ]
    chunks = format_drop_report(records)
    assert len(chunks) == 1
    assert chunks[0].index("too senior") < chunks[0].index("low pay")  # canonical order
    assert "Dropped 2 — low pay" in chunks[0]


def test_drop_report_chunks_under_telegram_limit() -> None:
    records = [DropRecord("low pay", f"Dev {i} @ Co", "x" * 90) for i in range(200)]
    chunks = format_drop_report(records)
    assert len(chunks) > 1
    assert all(len(c) <= 4000 for c in chunks)
    assert "(cont.)" in chunks[1]


def test_drop_report_empty_returns_no_chunks() -> None:
    assert format_drop_report([]) == []
