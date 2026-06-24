from datetime import date, datetime

from cvflow.dates import parse_posting_date, render_age

TODAY = date(2026, 6, 24)
NOW = datetime(2026, 6, 24, 12, 0, 0)


def test_parse_iso_date() -> None:
    assert parse_posting_date("2026-06-20", TODAY) == date(2026, 6, 20)


def test_parse_iso_datetime() -> None:
    assert parse_posting_date("2026-06-20T08:30:00+00:00", TODAY) == date(2026, 6, 20)


def test_parse_relative_days() -> None:
    assert parse_posting_date("2 days ago", TODAY) == date(2026, 6, 22)
    assert parse_posting_date("Posted 5 Days Ago", TODAY) == date(2026, 6, 19)
    assert parse_posting_date("30+ days ago", TODAY) == date(2026, 5, 25)


def test_parse_today_and_yesterday() -> None:
    assert parse_posting_date("Just posted", TODAY) == TODAY
    assert parse_posting_date("today", TODAY) == TODAY
    assert parse_posting_date("a few hours ago", TODAY) == TODAY
    assert parse_posting_date("yesterday", TODAY) == date(2026, 6, 23)


def test_parse_weeks() -> None:
    assert parse_posting_date("2 weeks ago", TODAY) == date(2026, 6, 10)


def test_parse_unknown_returns_none() -> None:
    assert parse_posting_date("", TODAY) is None
    assert parse_posting_date("nan", TODAY) is None
    assert parse_posting_date("sometime recently", TODAY) is None


def test_render_age_uses_posting_date() -> None:
    assert render_age(date(2026, 6, 21), "2026-06-22T00:00:00+00:00", NOW) == "posted 3d ago"
    assert render_age(date(2026, 6, 24), "x", NOW) == "posted today"
    assert render_age(date(2026, 6, 23), "x", NOW) == "posted 1d ago"


def test_render_age_falls_back_to_discovered_at() -> None:
    assert render_age(None, "2026-06-20T00:00:00+00:00", NOW) == "found 4d ago"
    assert render_age(None, "2026-06-24T06:00:00+00:00", NOW) == "found today"
