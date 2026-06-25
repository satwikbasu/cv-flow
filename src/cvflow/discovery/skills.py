"""Deterministic candidate-skill coverage gate (a hard must-have requirement).

The fit LLM tends to be charitable about ``must_have_skills``, counting incidental overlap as a
match. A hard requirement is better enforced in code than in a prompt (the same principle as the
rest of the discovery gates). This module compares a job's distilled ``must_have_skills`` against
the candidate's real skill set and drops jobs where the candidate lacks the majority of them.

The skill set and synonym map live in ``profile/candidate_skills.yaml`` so the user maintains them
by hand.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

__all__ = ["normalize", "load_skill_profile", "uncovered_skills", "coverage_drop"]

_WS = re.compile(r"\s+")


def normalize(skill: str) -> str:
    """Lowercase, strip, and collapse internal whitespace for stable matching."""
    return _WS.sub(" ", skill.strip().lower())


def load_skill_profile(path: str | Path) -> tuple[frozenset[str], dict[str, str]]:
    """Load ``{skills: [...], synonyms: {alias: canonical}}`` from YAML.

    A missing or empty file disables the gate (returns an empty profile) rather than erroring —
    discovery must never abort on a missing optional input.
    """
    p = Path(path)
    if not p.exists():
        return frozenset(), {}
    data = yaml.safe_load(p.read_text()) or {}
    skills = frozenset(normalize(s) for s in data.get("skills", []))
    synonyms = {normalize(k): normalize(v) for k, v in (data.get("synonyms") or {}).items()}
    return skills, synonyms


def uncovered_skills(
    must_have: list[str], have: frozenset[str], synonyms: dict[str, str]
) -> list[str]:
    """Normalized must-have skills the candidate lacks (after synonym resolution)."""
    missing: list[str] = []
    for raw in must_have:
        s = normalize(raw)
        canon = synonyms.get(s, s)
        if s not in have and canon not in have:
            missing.append(s)
    return missing


def coverage_drop(
    must_have: list[str],
    have: frozenset[str],
    synonyms: dict[str, str],
    *,
    max_missing_ratio: float = 0.5,
) -> tuple[bool, list[str]]:
    """Decide whether a job is dropped for missing too many must-have skills.

    Drops when the candidate lacks strictly more than ``max_missing_ratio`` of the job's
    must-have skills (by default, missing the majority). An empty must-have list never drops —
    the JD stated no mandatory skills, so there is nothing to gate on.
    """
    if not must_have:
        return False, []
    missing = uncovered_skills(must_have, have, synonyms)
    return (len(missing) / len(must_have) > max_missing_ratio), missing
