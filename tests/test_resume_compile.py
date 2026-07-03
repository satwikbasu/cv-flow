"""Tests for building the full tailored .tex and compiling it with tectonic.

They run against the fake-persona fixture resume in tests/fixtures/resume; the
compile tests skip when tectonic is not installed.
"""

import shutil
from pathlib import Path

import pytest

from cvflow.resume import (
    CompileError,
    ResumeTailor,
    TailoringError,
    TailoringPlan,
    parse_master,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "resume"

needs_tectonic = pytest.mark.skipif(
    shutil.which("tectonic") is None, reason="tectonic not installed"
)


class _Stub:
    def generate(self, prompt: str) -> str:
        return "{}"


def test_tailored_document_reorders_and_selects_projects() -> None:
    master = parse_master(FIXTURE_ROOT)
    tailor = ResumeTailor(_Stub(), master)
    order = list(reversed(master.section_order))
    plan = TailoringPlan(
        section_order=order, selected_project_ids=["taskboard"], diff_narration=""
    )
    doc = tailor.tailored_document(plan)
    assert "\\begin{document}" in doc and "\\end{document}" in doc
    # the selected project's content is inlined; the unselected one is absent
    assert "CRUD task tracker" in doc  # taskboard bullet
    assert "home-lab services" not in doc  # netmon-dashboard bullet
    # untouched sections stay as \input references, in the plan's order
    assert doc.index("\\input{sections/skills.tex}") < doc.index("Acme Web Services")


def test_tailored_document_always_keeps_the_heading() -> None:
    master = parse_master(FIXTURE_ROOT)
    tailor = ResumeTailor(_Stub(), master)
    # Even a plan that keeps only one section must retain the name/contact heading —
    # it lives between \begin{document} and the first section input.
    plan = TailoringPlan(section_order=["skills"], selected_project_ids=[], diff_narration="")
    doc = tailor.tailored_document(plan)
    assert "Jordan Lee" in doc
    assert doc.index("Jordan Lee") < doc.index("\\input{sections/skills.tex}")


def test_tailored_document_applies_rewrites_to_experience_and_projects() -> None:
    master = parse_master(FIXTURE_ROOT)
    tailor = ResumeTailor(_Stub(), master)
    plan = TailoringPlan(
        section_order=list(master.section_order),
        selected_project_ids=["taskboard"],
        diff_narration="",
        rephrased={
            "Built python flask APIs for internal tooling": "Built flask REST APIs",
            "Built a CRUD task tracker with role-based lists": "Shipped a CRUD tracker",
        },
    )
    doc = tailor.tailored_document(plan)
    assert "Built flask REST APIs" in doc
    assert "Shipped a CRUD tracker" in doc
    assert "internal tooling" not in doc


@needs_tectonic
def test_compile_master_produces_pdf(tmp_path: Path) -> None:
    master = parse_master(FIXTURE_ROOT)
    tailor = ResumeTailor(_Stub(), master)
    pdf = tailor.compile_master(tmp_path)
    assert pdf.exists() and pdf.stat().st_size > 0


@needs_tectonic
def test_compile_tailored_produces_pdf(tmp_path: Path) -> None:
    master = parse_master(FIXTURE_ROOT)
    tailor = ResumeTailor(_Stub(), master)
    plan = TailoringPlan(
        section_order=list(master.section_order),
        selected_project_ids=["taskboard"],
        diff_narration="",
    )
    pdf = tailor.compile_tailored(plan, tmp_path)
    assert pdf.exists() and pdf.suffix == ".pdf"
    # the temporary tailored .tex must not linger in the resume root
    assert not list(FIXTURE_ROOT.glob("_tailored*.tex"))


@needs_tectonic
def test_compile_tailored_names_pdf_by_stem(tmp_path: Path) -> None:
    master = parse_master(FIXTURE_ROOT)
    tailor = ResumeTailor(_Stub(), master)
    plan = TailoringPlan(
        section_order=list(master.section_order),
        selected_project_ids=["taskboard"],
        diff_narration="",
    )
    pdf = tailor.compile_tailored(plan, tmp_path, stem="tailored-acme-backend")
    assert pdf.name == "tailored-acme-backend.pdf" and pdf.exists()


def test_compile_tailored_runs_the_numbers_guard_first(tmp_path: Path) -> None:
    # A plan whose rewrites would sneak in a new number is refused before any compile.
    master = parse_master(FIXTURE_ROOT)
    tailor = ResumeTailor(_Stub(), master)
    plan = TailoringPlan(
        section_order=list(master.section_order),
        selected_project_ids=["taskboard"],
        diff_narration="",
        rephrased={
            "Built a CRUD task tracker with role-based lists": (
                "Built a CRUD task tracker used by 9999 teams"
            )
        },
    )
    with pytest.raises(TailoringError):
        tailor.compile_tailored(plan, tmp_path)


def test_compile_error_is_exported() -> None:
    assert issubclass(CompileError, Exception)
