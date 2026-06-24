import pytest

from cvflow.statemachine import IllegalTransition, Status, transition


def test_discovered_can_become_tailored_skipped_or_expired() -> None:
    assert transition(Status.DISCOVERED, Status.TAILORED) is Status.TAILORED
    assert transition(Status.DISCOVERED, Status.SKIPPED) is Status.SKIPPED
    assert transition(Status.DISCOVERED, Status.EXPIRED) is Status.EXPIRED


def test_terminal_states_allow_no_transition() -> None:
    for terminal in (Status.TAILORED, Status.SKIPPED, Status.EXPIRED):
        with pytest.raises(IllegalTransition):
            transition(terminal, Status.SKIPPED)


def test_self_transition_is_illegal() -> None:
    with pytest.raises(IllegalTransition):
        transition(Status.DISCOVERED, Status.DISCOVERED)


def test_status_string_values() -> None:
    assert Status.DISCOVERED == "discovered"
    assert Status.TAILORED == "tailored"
    assert Status.SKIPPED == "skipped"
    assert Status.EXPIRED == "expired"
