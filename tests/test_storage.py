import json
from datetime import UTC, datetime, timedelta

import pytest

from cvflow.analysis import JDAnalysis
from cvflow.statemachine import IllegalTransition, Status
from cvflow.storage import (
    ApplicationStore,
    DuplicateJob,
    FormFields,
    MissingField,
    UnknownJob,
)


def _store() -> ApplicationStore:
    return ApplicationStore(":memory:")


def test_add_and_get() -> None:
    s = _store()
    app = s.add(
        "li:1", "Acme", "Backend Engineer", "http://x",
        date_posted="2 days ago", date_posted_parsed="2026-06-22",
    )
    assert app.status is Status.DISCOVERED
    got = s.get("li:1")
    assert got is not None
    assert got.company == "Acme"
    assert got.date_posted == "2 days ago"
    assert got.date_posted_parsed == "2026-06-22"
    assert got.discovered_at


def test_add_duplicate_raises() -> None:
    s = _store()
    s.add("li:1", "A", "R", "u")
    with pytest.raises(DuplicateJob):
        s.add("li:1", "A", "R", "u")


def test_get_missing_returns_none() -> None:
    assert _store().get("nope") is None


def test_set_status_skip() -> None:
    s = _store()
    s.add("li:1", "A", "R", "u")
    s.set_status("li:1", Status.SKIPPED)
    got = s.get("li:1")
    assert got is not None and got.status is Status.SKIPPED


def test_set_status_unknown_raises() -> None:
    with pytest.raises(UnknownJob):
        _store().set_status("nope", Status.SKIPPED)


def test_set_tailored() -> None:
    s = _store()
    s.add("li:1", "A", "R", "u")
    s.set_tailored("li:1", "/path/r.pdf")
    got = s.get("li:1")
    assert got is not None
    assert got.status is Status.TAILORED
    assert got.tailored_pdf_path == "/path/r.pdf"
    assert got.tailored_at


def test_re_tailor_updates_pdf() -> None:
    s = _store()
    s.add("li:1", "A", "R", "u")
    s.set_tailored("li:1", "/a.pdf")
    s.set_tailored("li:1", "/b.pdf")
    got = s.get("li:1")
    assert got is not None and got.tailored_pdf_path == "/b.pdf"


def test_cannot_tailor_skipped() -> None:
    s = _store()
    s.add("li:1", "A", "R", "u")
    s.set_status("li:1", Status.SKIPPED)
    with pytest.raises(IllegalTransition):
        s.set_tailored("li:1", "/p.pdf")


def test_cannot_skip_a_tailored_job() -> None:
    s = _store()
    s.add("li:1", "A", "R", "u")
    s.set_tailored("li:1", "/p.pdf")
    with pytest.raises(IllegalTransition):
        s.set_status("li:1", Status.SKIPPED)


def test_expire_stale_expires_old_untailored() -> None:
    s = _store()
    s.add("old", "A", "R", "u")
    future = datetime.now(UTC) + timedelta(days=30)
    assert s.expire_stale(future, retention_days=21) == 1
    got = s.get("old")
    assert got is not None and got.status is Status.EXPIRED


def test_expire_stale_keeps_recent_and_tailored() -> None:
    s = _store()
    s.add("recent", "A", "R", "u")
    s.add("done", "A", "R", "u")
    s.set_tailored("done", "/p.pdf")
    future = datetime.now(UTC) + timedelta(days=30)
    assert s.expire_stale(future, retention_days=21) == 1
    done = s.get("done")
    recent = s.get("recent")
    assert done is not None and done.status is Status.TAILORED
    assert recent is not None and recent.status is Status.EXPIRED


def test_expire_stale_no_op_when_fresh() -> None:
    s = _store()
    s.add("j", "A", "R", "u")
    assert s.expire_stale(datetime.now(UTC), retention_days=21) == 0
    got = s.get("j")
    assert got is not None and got.status is Status.DISCOVERED


def test_discovery_meta_roundtrip() -> None:
    s = _store()
    s.add("j", "A", "R", "u")
    s.set_discovery_meta(
        "j", benchmark=88, fit_score=90, fit_reason="strong",
        concerns=["PAY_UNKNOWN"], cohort="N", ctc_lpa=None,
    )
    got = s.get("j")
    assert got is not None
    assert got.benchmark == 88
    assert got.fit_score == 90
    assert got.cohort == "N"
    assert got.fit_reason == "strong"


def test_jd_text_roundtrip() -> None:
    s = _store()
    s.add("j", "A", "R", "u")
    s.set_jd_text("j", "the full jd")
    assert s.get_jd_text("j") == "the full jd"


def test_list_by_status_orders_by_discovered_at() -> None:
    s = _store()
    s.add("a", "A", "R", "u")
    s.add("b", "B", "R", "u")
    assert [r.job_id for r in s.list_by_status(Status.DISCOVERED)] == ["a", "b"]


def test_digest_slots_roundtrip() -> None:
    s = _store()
    s.set_digest_slots(["a", "b", "c"])
    assert s.get_digest_slot(1) == "a"
    assert s.get_digest_slot(3) == "c"
    assert s.get_digest_slot(9) is None
    assert s.digest_slots() == ["a", "b", "c"]


def test_last_digest_roundtrip() -> None:
    s = _store()
    assert s.get_last_digest() is None
    s.set_last_digest("hello digest")
    row = s.get_last_digest()
    assert row is not None and row[0] == "hello digest"


def test_analysis_roundtrip() -> None:
    s = _store()
    s.add("j", "A", "R", "u")
    a = JDAnalysis(
        required_skills=["go"], preferred_quals=[], seniority="mid",
        tone="", applicant_instructions=[],
    )
    s.save_analysis("j", a)
    assert s.get_analysis("j") == a


def test_crux_version_gating() -> None:
    s = _store()
    s.save_crux("j", '{"x": 1}', version="4")
    assert s.get_crux("j") == '{"x": 1}'
    assert s.get_crux("j", version="4") == '{"x": 1}'
    assert s.get_crux("j", version="5") is None


def test_form_fields(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "ff.json"
    p.write_text(json.dumps({"_comment": "x", "full_name": "Jane", "phone": ""}))
    ff = FormFields.load(p)
    assert ff.populated == {"full_name": "Jane"}
    assert ff.missing() == ["phone"]
    assert ff.is_filled("full_name")
    assert not ff.is_filled("phone")
    assert ff.require("full_name") == "Jane"
    with pytest.raises(MissingField):
        ff.require("phone")


def test_store_is_usable_from_another_thread(tmp_path) -> None:
    # The bot shares one store between the event-loop thread (handlers) and the
    # executor threads that run discovery/tailoring; sqlite must allow that.
    import threading

    s = ApplicationStore(tmp_path / "db.sqlite")
    s.add("j", "Acme", "Dev", "https://example.test/j")
    errors: list[BaseException] = []

    def _use() -> None:
        try:
            assert s.get("j") is not None
            s.set_digest_slots(["j"])
        except BaseException as exc:  # noqa: BLE001 — capture for the main thread
            errors.append(exc)

    t = threading.Thread(target=_use)
    t.start()
    t.join()
    assert errors == []
