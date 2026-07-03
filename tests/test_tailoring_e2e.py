"""End-to-end tailoring: a cached crux + the master resume in, a PDF + honest diff out.

This is the whole tailoring path as the bot will drive it — crux from the store,
analysis derived without any network call, one faked LLM planning call, then a
real tectonic compile of the fixture resume. Skips when tectonic is missing.
"""

import json
import shutil
from pathlib import Path

import pytest

from cvflow.analysis import resolve_tailoring_analysis
from cvflow.resume import ResumeTailor, TailoringError, TailoringPlan, parse_master
from cvflow.storage import ApplicationStore

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "resume"


class _FakeProvider:
    def __init__(self, payload: str) -> None:
        self.payload = payload

    def generate(self, prompt: str) -> str:
        return self.payload


def _seeded_store() -> ApplicationStore:
    store = ApplicationStore(":memory:")
    store.add("acme:1", "Acme Web Services", "Backend Developer", "https://example.test/jd/1")
    store.save_crux(
        "acme:1",
        json.dumps(
            {
                "must_have_skills": ["Java", "Spring Boot"],
                "tech_stack": ["Java", "Spring Boot", "Postgres"],
                "seniority_signal": "junior",
                "applicant_instructions": [],
            }
        ),
    )
    return store


@pytest.mark.skipif(shutil.which("tectonic") is None, reason="tectonic not installed")
def test_crux_to_compiled_pdf_with_honest_diff(tmp_path: Path) -> None:
    analysis = resolve_tailoring_analysis(
        "acme:1", store=_seeded_store(), analyzer=None, use_jd_analysis=False
    )
    assert "Java" in analysis.required_skills  # crux drove the analysis, no network

    payload = json.dumps(
        {
            "section_order": ["skills", "experience", "projects"],
            "selected_project_ids": ["taskboard"],
            "diff_narration": "Led with skills; taskboard matches the Spring Boot ask.",
        }
    )
    master = parse_master(FIXTURE_ROOT)
    tailor = ResumeTailor(_FakeProvider(payload), master)  # rephrase defaults off
    plan = tailor.plan(analysis)

    pdf = tailor.compile_tailored(plan, tmp_path, stem="tailored-acme-1")
    assert pdf.exists() and pdf.stat().st_size > 0

    diff = tailor.diff(plan)
    assert "skills" in diff.lower() and "taskboard" in diff
    assert "No bullets reworded" in diff  # honest: reorder/select only, nothing reworded


def test_numbers_guard_holds_across_the_pipeline(tmp_path: Path) -> None:
    # Same pipeline, but the plan smuggles in a reworded bullet with an invented figure:
    # the run must die before anything compiles.
    analysis = resolve_tailoring_analysis(
        "acme:1", store=_seeded_store(), analyzer=None, use_jd_analysis=False
    )
    master = parse_master(FIXTURE_ROOT)
    tailor = ResumeTailor(_FakeProvider("{}"), master)
    plan = TailoringPlan(
        section_order=list(master.section_order),
        selected_project_ids=["taskboard"],
        diff_narration="",
        rephrased={
            "Built python flask APIs for internal tooling": (
                "Built flask APIs serving 2000000 requests a day"
            )
        },
    )
    with pytest.raises(TailoringError, match="adds numbers"):
        tailor.compile_tailored(plan, tmp_path)
    assert analysis.seniority == "junior"  # the analysis side stays usable regardless
