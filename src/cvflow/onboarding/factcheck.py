"""Catch numbers the generated resume contains that the source material does not.

Onboarding writes the candidate's resume from the file they uploaded and the answers
they gave, and the prompt tells the model never to invent facts. An instruction is not
a guarantee, so this is the backstop: every number in the generated resume should appear
in the uploaded resume or in one of the user's answers; anything left over is surfaced
for the person to confirm before they use the resume. Numbers (metrics, dates, durations,
pay) are the concrete fabrication risk and the part we can check without guessing. Wording
and titles are left to the person's own read of the review report, the same way the tailor
leans on the before/after diff rather than policing synonyms.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from cvflow.onboarding.bundle import ResumeBundle

_NUM_RE = re.compile(r"\d[\d,]*")


def _numbers(text: str) -> set[str]:
    """Digit-runs with commas stripped, so '10,000' and '10000' compare equal."""
    return {m.replace(",", "") for m in _NUM_RE.findall(text)}


def unverified_numbers(resume: ResumeBundle, sources: Iterable[str]) -> list[str]:
    """Numbers in the generated resume that appear in none of ``sources``.

    ``sources`` is the uploaded resume text plus the user's own answers. An empty
    result means every number traces back to something the person actually gave us.
    """
    generated = _numbers(resume.heading_tex)
    for block in (*resume.sections.values(), *resume.project_blocks.values()):
        generated |= _numbers(block)
    allowed: set[str] = set()
    for src in sources:
        allowed |= _numbers(src)
    return sorted(generated - allowed)
