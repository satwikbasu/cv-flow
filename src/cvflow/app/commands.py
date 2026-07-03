"""The forgiving text parser for chat input.

Accepts ``/tailor 3``, ``tailor 3``, ``t 3``, a bare ``3`` right after a list,
``skip 2 4``, ``skip 2-4``, and the plain commands. Anything it can't read with
certainty returns ``None`` so the handler replies with a hint — it never
guesses and never stays silent. Ordinal→job resolution stays in the handlers;
the parser only needs the current slot list to know whether a bare number can
mean anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["Action", "parse_action"]

_VERBS = {
    "tailor": "tailor",
    "t": "tailor",
    "skip": "skip",
    "s": "skip",
}

_PLAIN = {
    "discover": "discover",
    "jobs": "jobs",
    "status": "status",
    "help": "help",
    "start": "help",
}

_JOBS_FILTERS = {"open", "tailored", "all"}

_RANGE_RE = re.compile(r"^(\d+)-(\d+)$")


@dataclass(frozen=True)
class Action:
    kind: str  # tailor | skip | discover | jobs | status | help
    ordinals: list[int] = field(default_factory=list)
    jobs_filter: str = "open"


def _parse_ordinals(tokens: list[str]) -> list[int] | None:
    """Numbers and a-b ranges only; anything else -> None (the caller hints, never guesses)."""
    out: list[int] = []
    for tok in tokens:
        m = _RANGE_RE.match(tok)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo < 1 or hi < lo:
                return None
            out.extend(range(lo, hi + 1))
        elif tok.isdigit():
            n = int(tok)
            if n < 1:
                return None
            out.append(n)
        else:
            return None
    return out or None


def parse_action(text: str, slots: list[str]) -> Action | None:
    """Parse one chat message into an Action, or None when it isn't understood."""
    tokens = text.strip().split()
    if not tokens:
        return None
    head = tokens[0].lower().lstrip("/").split("@")[0]

    if head in _PLAIN:
        kind = _PLAIN[head]
        if kind == "jobs":
            if len(tokens) == 1:
                return Action("jobs")
            if len(tokens) == 2 and tokens[1].lower() in _JOBS_FILTERS:
                return Action("jobs", jobs_filter=tokens[1].lower())
            return None
        if len(tokens) > 1:
            return None
        return Action(kind)

    if head in _VERBS:
        ordinals = _parse_ordinals(tokens[1:])
        if ordinals is None:
            return None
        return Action(_VERBS[head], ordinals=ordinals)

    # A bare number right after a numbered list means "tailor that one".
    if slots:
        ordinals = _parse_ordinals(tokens)
        if ordinals is not None and len(ordinals) == 1:
            return Action("tailor", ordinals=ordinals)
    return None
