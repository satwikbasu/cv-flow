"""Tests for the forgiving text parser. It never guesses: garbage returns None."""

from cvflow.app.commands import Action, parse_action

SLOTS = ["j1", "j2", "j3", "j4", "j5"]


def _kind(text: str, slots: list[str] | None = None) -> Action | None:
    return parse_action(text, SLOTS if slots is None else slots)


# --- tailor ---


def test_slash_tailor_with_number() -> None:
    a = _kind("/tailor 3")
    assert a is not None and a.kind == "tailor" and a.ordinals == [3]


def test_bare_tailor_and_single_letter() -> None:
    assert _kind("tailor 3").ordinals == [3]  # type: ignore[union-attr]
    assert _kind("t 3").ordinals == [3]  # type: ignore[union-attr]
    assert _kind("T 3").ordinals == [3]  # type: ignore[union-attr]


def test_bare_number_right_after_a_list_means_tailor() -> None:
    a = _kind("3")
    assert a is not None and a.kind == "tailor" and a.ordinals == [3]


def test_bare_number_without_a_list_is_not_guessed() -> None:
    assert _kind("3", slots=[]) is None


def test_command_with_bot_suffix() -> None:
    a = _kind("/tailor@cvflow_bot 2")
    assert a is not None and a.kind == "tailor" and a.ordinals == [2]


# --- skip ---


def test_skip_multiple_numbers() -> None:
    a = _kind("skip 2 4")
    assert a is not None and a.kind == "skip" and a.ordinals == [2, 4]


def test_skip_range_expands() -> None:
    a = _kind("skip 2-4")
    assert a is not None and a.ordinals == [2, 3, 4]


def test_skip_single_letter_and_slash() -> None:
    assert _kind("/skip 1").kind == "skip"  # type: ignore[union-attr]
    assert _kind("s 1").kind == "skip"  # type: ignore[union-attr]


# --- other commands ---


def test_jobs_default_and_filters() -> None:
    a = _kind("/jobs")
    assert a is not None and a.kind == "jobs" and a.jobs_filter == "open"
    assert _kind("jobs tailored").jobs_filter == "tailored"  # type: ignore[union-attr]
    assert _kind("/jobs all").jobs_filter == "all"  # type: ignore[union-attr]


def test_simple_commands() -> None:
    for text, kind in [
        ("/discover", "discover"),
        ("discover", "discover"),
        ("/status", "status"),
        ("/help", "help"),
        ("/start", "help"),
    ]:
        a = _kind(text)
        assert a is not None and a.kind == kind, text


# --- never guess ---


def test_garbage_returns_none() -> None:
    for text in ["hello there", "tailor three", "skip 2x", "tailor", "skip", "/jobs weird", ""]:
        assert _kind(text) is None, text


def test_zero_and_negative_ordinals_rejected() -> None:
    assert _kind("tailor 0") is None
    assert _kind("skip 0-2") is None
