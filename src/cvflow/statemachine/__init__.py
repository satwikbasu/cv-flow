"""The application status state machine for the discover -> tailor lifecycle.

A job starts ``discovered`` and moves to exactly one terminal state: ``tailored`` (the
user generated a tailored resume for it, kept as history), ``skipped`` (the user passed),
or ``expired`` (it sat untailored past the retention window). The storage layer routes
every status change through :func:`transition`, so an illegal move can never be persisted.
"""

from __future__ import annotations

from enum import StrEnum


class Status(StrEnum):
    DISCOVERED = "discovered"
    TAILORED = "tailored"
    SKIPPED = "skipped"
    EXPIRED = "expired"


class IllegalTransition(Exception):
    """Raised on a status change the transition table forbids."""


# A discovered job goes to exactly one terminal state; the rest are end states.
_LEGAL: dict[Status, frozenset[Status]] = {
    Status.DISCOVERED: frozenset({Status.TAILORED, Status.SKIPPED, Status.EXPIRED}),
    Status.TAILORED: frozenset(),
    Status.SKIPPED: frozenset(),
    Status.EXPIRED: frozenset(),
}


def transition(current: Status, target: Status) -> Status:
    """Validate and return ``target``; raise :class:`IllegalTransition` if the move is illegal."""
    if target not in _LEGAL[current]:
        raise IllegalTransition(f"illegal transition: {current.value} -> {target.value}")
    return target
