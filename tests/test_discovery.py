"""Tests for discovery: row normalization and dedup. Fully mocked — no network, no LLM."""

from cvflow.discovery import normalize_rows


def _row(id_: str, site: str = "linkedin", **over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": id_,
        "site": site,
        "title": f"Role {id_}",
        "company": f"Co {id_}",
        "location": "Remote",
        "description": f"desc {id_}",
        "job_url": f"https://x/{id_}",
        "date_posted": "2026-06-02",
    }
    base.update(over)
    return base


def test_normalize_builds_stable_job_id() -> None:
    postings = normalize_rows([_row("1"), _row("2", site="indeed")])
    ids = [p.job_id for p in postings]
    assert ids == ["linkedin:1", "indeed:2"]
    assert postings[0].title == "Role 1"
    assert postings[0].url == "https://x/1"


def test_normalize_dedups_within_batch_first_wins() -> None:
    postings = normalize_rows([_row("1", title="A"), _row("1", title="B")])
    assert len(postings) == 1
    assert postings[0].title == "A"


def test_normalize_falls_back_to_url_hash_when_no_id() -> None:
    postings = normalize_rows([_row("", id="")])
    assert len(postings) == 1
    assert postings[0].job_id.startswith("linkedin:")  # url-hash suffix, still stable


def test_normalize_cleans_nan_and_carries_salary() -> None:
    rows = [{
        "id": "1", "site": "indeed", "title": "Backend", "company": float("nan"),
        "location": "Remote", "description": "d", "job_url": "https://x/1",
        "date_posted": "2026-06-02", "min_amount": 800000.0, "max_amount": 1200000.0,
        "currency": "INR",
    }]
    p = normalize_rows(rows)[0]
    assert p.company == ""
    assert p.min_amount == 800000.0
    assert p.max_amount == 1200000.0
    assert p.currency == "INR"


def test_normalize_missing_salary_is_none() -> None:
    p = normalize_rows([_row("1")])[0]
    assert p.min_amount is None
    assert p.currency is None


def test_normalize_carries_experience_range() -> None:
    p = normalize_rows([_row("1", experience_range="2-4 Yrs")])[0]
    assert p.experience_range == "2-4 Yrs"


def test_normalize_experience_range_absent_is_none() -> None:
    assert normalize_rows([_row("1")])[0].experience_range is None


def test_normalize_carries_job_type() -> None:
    assert normalize_rows([_row("1", job_type="internship")])[0].job_type == "internship"
    assert normalize_rows([_row("1")])[0].job_type is None
