"""Tests for the pure run bodies. All deps are injected fakes; no bot, no network."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cvflow.runs import run_discover, run_heartbeat, run_tailor
from cvflow.statemachine import IllegalTransition, Status
from cvflow.storage import ApplicationStore

NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


class _Posting:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.title = "Backend Dev"
        self.company = "Acme"
        self.location = ""
        self.url = f"https://example.test/{job_id}"
        self.date_posted = ""


class _BJ:
    def __init__(self, job_id: str, cohort: str) -> None:
        self.posting = _Posting(job_id)
        self.benchmark = 80
        self.fit_score = 8
        self.fit_reason = "Stack matches"
        self.concerns: list[str] = []
        self.cohort = cohort
        self.ctc_lpa = 12.0 if cohort == "M" else None


class _FakeDiscovery:
    def __init__(self, calls: list[str], result: dict[str, Any] | None = None) -> None:
        self._calls = calls
        self._result = result

    def discover(self, progress: Any = None) -> dict[str, Any]:
        self._calls.append("discover")
        if self._result is not None:
            return self._result
        return {"M": [_BJ("m:1", "M")], "N": [_BJ("n:1", "N")], "_drop_records": []}


class _TrackingStore(ApplicationStore):
    """Real in-memory store that also records the call order of the interesting methods."""

    def __init__(self, calls: list[str]) -> None:
        super().__init__(":memory:")
        self._calls = calls

    def expire_stale(self, now: datetime, retention_days: int) -> int:
        self._calls.append("expire")
        return super().expire_stale(now, retention_days)


def _seed(store: ApplicationStore, job_id: str = "m:1") -> None:
    store.add(job_id, "Acme", "Backend Dev", f"https://example.test/{job_id}")


# --- run_discover ---


def test_discover_sweeps_retention_before_discovering() -> None:
    calls: list[str] = []
    store = _TrackingStore(calls)
    run_discover(
        store=store,
        discovery=_FakeDiscovery(calls),
        notify=lambda _m: None,
        retention_days=21,
        now=lambda: NOW,
    )
    assert calls.index("expire") < calls.index("discover")


def test_discover_sets_slots_saves_digest_and_notifies() -> None:
    calls: list[str] = []
    store = _TrackingStore(calls)
    sent: list[str] = []
    outcome = run_discover(
        store=store,
        discovery=_FakeDiscovery(calls),
        notify=sent.append,
        retention_days=21,
        now=lambda: NOW,
    )
    assert outcome.ordered_job_ids == ["m:1", "n:1"]
    assert store.digest_slots() == ["m:1", "n:1"]
    saved = store.get_last_digest()
    assert saved is not None and "cv-flow" in saved[0]
    assert sent and sent[0] == outcome.digest


def test_discover_reports_expired_count() -> None:
    calls: list[str] = []
    store = _TrackingStore(calls)
    store.add("old:1", "Acme", "Old Role", "https://example.test/old")
    store._conn.execute(  # age the row well past retention
        "UPDATE applications SET discovered_at = '2026-05-01T00:00:00+00:00'"
    )
    store._conn.commit()
    outcome = run_discover(
        store=store,
        discovery=_FakeDiscovery(calls),
        notify=lambda _m: None,
        retention_days=21,
        now=lambda: NOW,
    )
    assert outcome.expired == 1
    assert store.get("old:1").status is Status.EXPIRED  # type: ignore[union-attr]


def test_discover_writes_log_and_sends_drop_footer(tmp_path: Path) -> None:
    calls: list[str] = []
    store = _TrackingStore(calls)
    sent: list[str] = []
    result = {
        "M": [_BJ("m:1", "M")],
        "N": [],
        "_dropped": {"low pay": 3},
        "_drop_records": [],
    }
    run_discover(
        store=store,
        discovery=_FakeDiscovery(calls, result),
        notify=sent.append,
        retention_days=21,
        now=lambda: NOW,
        report_drops=True,
        log_dir=str(tmp_path),
    )
    assert len(sent) == 2  # digest, then the drop message
    assert "3 low pay" in sent[1]  # deterministic footer (no summary provider given)
    assert list(tmp_path.glob("*.md"))  # discover log written


def test_discover_drop_message_prefers_llm_summary() -> None:
    calls: list[str] = []
    sent: list[str] = []

    class _Summarizer:
        def generate(self, prompt: str, **kwargs: object) -> str:
            return "A few low-pay roles were filtered."

    result = {
        "M": [_BJ("m:1", "M")],
        "N": [],
        "_dropped": {"low pay": 3},
        "_drop_records": [("low pay", "Dev @ Cheap", "3 LPA")],
    }
    from cvflow.discovery import DropRecord

    result["_drop_records"] = [DropRecord("low pay", "Dev @ Cheap", "3 LPA")]
    run_discover(
        store=_TrackingStore(calls),
        discovery=_FakeDiscovery(calls, result),
        notify=sent.append,
        retention_days=21,
        now=lambda: NOW,
        report_drops=True,
        summary_provider=_Summarizer(),
    )
    assert "low-pay roles were filtered" in sent[1]


# --- run_tailor ---


class _FakeTailor:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def plan(self, analysis: Any, *, feedback: str | None = None) -> str:
        self._calls.append("plan")
        return "the-plan"

    def compile_tailored(self, plan: Any, outdir: Any, *, stem: str) -> Path:
        self._calls.append("compile")
        pdf = Path(outdir) / f"{stem}.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF")
        return pdf

    def diff(self, plan: Any) -> str:
        return "No bullets reworded — every line is verbatim from your master resume."


def _crux_store(calls: list[str]) -> _TrackingStore:
    store = _TrackingStore(calls)
    _seed(store, "m:1")
    store.save_crux(
        "m:1",
        '{"must_have_skills": ["python"], "tech_stack": ["python"],'
        ' "seniority_signal": "junior", "applicant_instructions": []}',
    )
    return store


def test_tailor_compiles_then_flips_status_and_returns_result(tmp_path: Path) -> None:
    calls: list[str] = []
    store = _crux_store(calls)

    class _StatusTracking(_FakeTailor):
        def compile_tailored(self, plan: Any, outdir: Any, *, stem: str) -> Path:
            # status must still be discovered while compiling — it flips only on success
            assert store.get("m:1").status is Status.DISCOVERED  # type: ignore[union-attr]
            return super().compile_tailored(plan, outdir, stem=stem)

    result = run_tailor(
        "m:1",
        store=store,
        tailor=_StatusTracking(calls),
        output_dir=str(tmp_path),
        use_jd_analysis=False,
        analyzer=None,
    )
    assert store.get("m:1").status is Status.TAILORED  # type: ignore[union-attr]
    assert result["role"] == "Backend Dev" and result["company"] == "Acme"
    assert result["pdf_path"].endswith(".pdf") and "m-1" in result["pdf_path"]
    assert "verbatim" in result["diff"]


def test_tailor_failure_leaves_status_untouched(tmp_path: Path) -> None:
    calls: list[str] = []
    store = _crux_store(calls)

    class _Boom(_FakeTailor):
        def compile_tailored(self, plan: Any, outdir: Any, *, stem: str) -> Path:
            raise RuntimeError("compile exploded")

    with pytest.raises(RuntimeError):
        run_tailor(
            "m:1",
            store=store,
            tailor=_Boom(calls),
            output_dir=str(tmp_path),
            use_jd_analysis=False,
            analyzer=None,
        )
    assert store.get("m:1").status is Status.DISCOVERED  # type: ignore[union-attr]


def test_tailor_refuses_skipped_job_before_compiling(tmp_path: Path) -> None:
    calls: list[str] = []
    store = _crux_store(calls)
    store.set_status("m:1", Status.SKIPPED)
    with pytest.raises(IllegalTransition):
        run_tailor(
            "m:1",
            store=store,
            tailor=_FakeTailor(calls),
            output_dir=str(tmp_path),
            use_jd_analysis=False,
            analyzer=None,
        )
    assert "compile" not in calls  # refused up front, not after the expensive work


def test_tailor_unknown_job_raises(tmp_path: Path) -> None:
    calls: list[str] = []
    from cvflow.storage import UnknownJob

    with pytest.raises(UnknownJob):
        run_tailor(
            "ghost:1",
            store=_TrackingStore(calls),
            tailor=_FakeTailor(calls),
            output_dir=str(tmp_path),
            use_jd_analysis=False,
            analyzer=None,
        )


# --- run_heartbeat ---


def test_heartbeat_counts_every_status() -> None:
    calls: list[str] = []
    store = _TrackingStore(calls)
    _seed(store, "a")
    _seed(store, "b")
    store.set_status("b", Status.SKIPPED)
    line = run_heartbeat(store)
    assert "discovered=1" in line and "skipped=1" in line
    assert "tailored=0" in line and "expired=0" in line
