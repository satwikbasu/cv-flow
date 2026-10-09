from pathlib import Path

from cvflow.knowledge import KnowledgeBase
from cvflow.onboarding.applier import AppliedResult
from cvflow.onboarding.bundle import FORM_FIELD_KEYS, PROFILE_DOC_KEYS, CandidateBundle
from cvflow.onboarding.compile_repair import CompileOutcome
from cvflow.onboarding.report import render_report
from cvflow.storage import FormFields


def _bundle(notes: list[str]) -> CandidateBundle:
    docs = {k: f"# {k}" for k in PROFILE_DOC_KEYS}
    docs["essay_answers"] = "# Why us\n<!-- fill me -->"
    return CandidateBundle.model_validate({
        "profile_docs": docs,
        "project_docs": {"alpha": "# a"},
        "form_fields": {k: "" for k in FORM_FIELD_KEYS},
        "candidate_skills": {"skills": ["Go"], "synonyms": {}},
        "discovery": {},
        "preferences": {},
        "resume": {"heading_tex": "x", "sections": {"projects": ""},
                   "project_blocks": {"alpha": "b"}},
        "review_notes": notes,
    })


def _render(notes: list[str], outcome: CompileOutcome) -> list[str]:
    kb = KnowledgeBase(documents={}, form_fields=FormFields({"email": "", "phone": "1"}))
    applied = AppliedResult(Path("p"), Path("r"), None, False, None)
    return render_report(_bundle(notes), applied, outcome, kb)


OK = CompileOutcome(True, 1, "", False)


def test_empty_fields_and_placeholder_listed() -> None:
    text = "\n".join(_render([], OK))
    assert "- email" in text and "- phone" not in text
    assert "placeholders" in text and "Projects wired into the resume: 1" in text


def test_flagged_warning() -> None:
    text = "\n".join(_render([], CompileOutcome(False, 3, "! bad", True)))
    assert "WARNING" in text and "! bad" in text


def test_many_notes_chunked() -> None:
    chunks = _render([f"note {i} " + "x" * 200 for i in range(100)], OK)
    assert len(chunks) > 1 and all(len(c) <= 4000 for c in chunks)
