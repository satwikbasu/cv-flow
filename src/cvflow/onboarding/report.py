"""Human-facing onboarding review report, chunked for Telegram."""

from __future__ import annotations

import re

from cvflow.knowledge import KnowledgeBase
from cvflow.onboarding.applier import AppliedResult
from cvflow.onboarding.bundle import CandidateBundle
from cvflow.onboarding.compile_repair import CompileOutcome

LIMIT = 4000
_PLACEHOLDER_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _chunk(lines: list[str]) -> list[str]:
    chunks: list[str] = []
    cur = ""
    for line in lines:
        for i in range(0, max(len(line), 1), LIMIT):
            cur = _push(chunks, cur, line[i : i + LIMIT])
    if cur:
        chunks.append(cur)
    return chunks


def _push(chunks: list[str], cur: str, line: str) -> str:
    if cur and len(cur) + 1 + len(line) > LIMIT:
        chunks.append(cur)
        cur = ""
    return f"{cur}\n{line}" if cur else line


def render_report(
    bundle: CandidateBundle,
    applied: AppliedResult,
    compile_outcome: CompileOutcome,
    kb: KnowledgeBase,
) -> list[str]:
    lines = ["Onboarding complete - please review.", f"Profile written to {applied.profile_dir}"]
    missing = kb.missing_form_fields()
    if missing:
        lines.append("Form fields needing your input (left blank, not guessed):")
        lines += [f"- {k}" for k in missing]
    essay = bundle.profile_docs.get("essay_answers", "")
    if _PLACEHOLDER_RE.search(essay) or not essay.strip():
        lines.append("Essay answers still contain placeholders or are empty - fill them in.")
    if bundle.review_notes:
        lines.append("Review notes:")
        lines += [f"- {n}" for n in bundle.review_notes]
    lines.append(f"Projects wired into the resume: {len(bundle.project_docs)}")
    if compile_outcome.ok:
        lines.append(f"Resume compiled successfully (attempt {compile_outcome.attempts}).")
    else:
        lines.append(
            "WARNING: the resume did not compile after "
            f"{compile_outcome.attempts} attempts and needs manual fixing. Your profile was "
            "kept. See ONBOARDING_FLAGGED.txt in the resume folder."
        )
        if compile_outcome.log_excerpt:
            lines.append(compile_outcome.log_excerpt)
    lines.append("Next steps: fill the blank fields above, fix any flagged items, then confirm.")
    return _chunk(lines)
