"""Naukri jobapi adapter. Mocked HTTP; no network."""

import base64

import pytest

from cvflow.discovery import normalize_rows
from cvflow.discovery.naukri import (
    NaukriUnreachable,
    make_nkparam,
    parse_salary,
    search_naukri,
)


def test_make_nkparam_is_fresh_base64():
    a = make_nkparam()
    b = make_nkparam()
    assert base64.b64decode(a)  # valid base64
    assert a != b  # one-time token: changes each call (timestamp in plaintext)


def test_parse_salary():
    assert parse_salary("3-6 Lacs PA") == (300000.0, 600000.0, "INR")
    assert parse_salary("3.75-6.5 Lacs PA") == (375000.0, 650000.0, "INR")
    assert parse_salary("Not disclosed") == (None, None, None)
    assert parse_salary(None) == (None, None, None)
    assert parse_salary("competitive") == (None, None, None)


def _page(*jobs):
    return 200, {"jobDetails": list(jobs)}


def _job(jid, title="DevOps Engineer", sal="3-6 Lacs PA", exp="1-3 Yrs"):
    return {
        "jobId": jid, "title": title, "companyName": "Acme",
        "placeholders": [
            {"type": "location", "label": "Bengaluru"},
            {"type": "experience", "label": exp},
            {"type": "salary", "label": sal},
        ],
        "tagsAndSkills": "docker,kubernetes,aws",
        "jobDescription": "Build CI/CD pipelines.",
        "jdURL": f"/job-listings-{jid}",
        "footerPlaceholderLabel": "2 Days Ago",
    }


def test_search_naukri_maps_rows_and_normalizes():
    calls = {"n": 0}

    def get_fn(url, *, params, headers):
        calls["n"] += 1
        assert headers["nkparam"]  # token attached
        assert headers["appid"] == "109"
        if params["pageNo"] == 1:
            return _page(_job("111"), _job("222", sal="Not disclosed"))
        return (200, {"jobDetails": []})

    rows = search_naukri(search_term="devops engineer", location="Remote",
                         results_wanted=20, hours_old=72,
                         get_fn=get_fn, nkparam_fn=lambda: "tok")
    assert len(rows) == 2
    assert rows[0]["site"] == "naukri" and rows[0]["id"] == "111"
    assert rows[0]["min_amount"] == 300000.0 and rows[0]["currency"] == "INR"
    assert rows[0]["experience_range"] == "1-3 Yrs"
    assert rows[1]["min_amount"] is None  # 'Not disclosed'
    assert "docker" in rows[0]["description"]
    # rows must flow through normalize_rows cleanly
    postings = normalize_rows(rows)
    assert postings[0].job_id == "naukri:111"
    assert postings[0].url == "https://www.naukri.com/job-listings-111"


def test_search_naukri_paginates_until_results_wanted():
    def get_fn(url, *, params, headers):
        return _page(*[_job(f"{params['pageNo']}-{i}") for i in range(20)])

    rows = search_naukri(search_term="x", location="", results_wanted=35, hours_old=72,
                         get_fn=get_fn, nkparam_fn=lambda: "tok", max_pages=5)
    assert len(rows) == 35  # capped to results_wanted across pages


def test_search_naukri_raises_unreachable_after_403_retry():
    calls = {"n": 0}

    def get_fn(url, *, params, headers):
        calls["n"] += 1
        return 403, {}

    with pytest.raises(NaukriUnreachable):
        search_naukri(search_term="x", location="", results_wanted=20, hours_old=72,
                      get_fn=get_fn, nkparam_fn=lambda: "tok")
    assert calls["n"] == 2  # one retry with a fresh token, then declare it unreachable


def test_search_naukri_400_is_clean_end_of_pages():
    calls = {"n": 0}

    def get_fn(url, *, params, headers):
        calls["n"] += 1
        if params["pageNo"] == 1:
            return _page(_job("1"))
        return 400, {}  # page 2 -> no more pages, not a block

    rows = search_naukri(search_term="x", location="Remote", results_wanted=40, hours_old=72,
                         get_fn=get_fn, nkparam_fn=lambda: "tok")
    assert len(rows) == 1            # page 1 kept
    assert calls["n"] == 2           # page 2 tried once, no 403-style retry


def test_search_naukri_block_after_first_page_keeps_rows():
    # Page 1 reaches Naukri; a later-page block is treated as end-of-pages, not unreachable.
    def get_fn(url, *, params, headers):
        if params["pageNo"] == 1:
            return _page(_job("1"))
        return 403, {}

    rows = search_naukri(search_term="x", location="", results_wanted=40, hours_old=72,
                         get_fn=get_fn, nkparam_fn=lambda: "tok")
    assert len(rows) == 1  # reached the API, so no NaukriUnreachable
