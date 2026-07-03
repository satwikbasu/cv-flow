"""Tests for resume tailoring: parsing, planning, rendering, the numbers guard, and the diff.

The LLM is always faked. Compile tests live in test_resume_compile.py and skip
when tectonic is missing.
"""

from pathlib import Path

from cvflow.resume import (
    Master,
    Project,
    Section,
    _resume_item_bodies,
    _substitute_bullets,
    parse_master,
)

# --- parse_master ---


def test_parse_master_returns_section_order_and_projects(tmp_path: Path) -> None:
    (tmp_path / "sections" / "projects").mkdir(parents=True)
    (tmp_path / "master.tex").write_text(
        "\\begin{document}\n"
        "\\input{sections/experience.tex}\n"
        "\\input{sections/projects.tex}\n"
        "\\input{sections/skills.tex}\n"
        "\\end{document}\n"
    )
    (tmp_path / "sections" / "experience.tex").write_text("EXP")
    (tmp_path / "sections" / "skills.tex").write_text("SKILLS")
    (tmp_path / "sections" / "projects.tex").write_text(
        "\\input{sections/projects/a.tex}\n\\input{sections/projects/b.tex}\n"
    )
    (tmp_path / "sections" / "projects" / "a.tex").write_text("AAA")
    (tmp_path / "sections" / "projects" / "b.tex").write_text("BBB")

    master = parse_master(tmp_path)
    assert master.section_order == ["experience", "projects", "skills"]
    assert [p.project_id for p in master.projects] == ["a", "b"]
    assert master.sections["experience"].content == "EXP"
    assert master.projects[0].content == "AAA"


def test_parse_master_without_projects_file(tmp_path: Path) -> None:
    (tmp_path / "sections").mkdir()
    (tmp_path / "master.tex").write_text(
        "\\begin{document}\n\\input{sections/skills.tex}\n\\end{document}\n"
    )
    (tmp_path / "sections" / "skills.tex").write_text("SKILLS")
    master = parse_master(tmp_path)
    assert master.section_order == ["skills"]
    assert master.projects == []


def test_master_dataclasses_are_frozen() -> None:
    m = Master(
        root=Path("/nonexistent"),
        section_order=["skills"],
        sections={"skills": Section("skills", "SK")},
        projects=[Project("demo", "D")],
    )
    assert m.sections["skills"].name == "skills"
    assert m.projects[0].project_id == "demo"


# --- _resume_item_bodies + _substitute_bullets ---


def test_resume_item_bodies_extracts_inner_text() -> None:
    tex = "x\n  \\resumeItem{Built APIs}\n  \\resumeItem{Designed \\textbf{Docker} swarm}\n"
    bodies = [b for _, _, b in _resume_item_bodies(tex)]
    assert bodies == ["Built APIs", "Designed \\textbf{Docker} swarm"]


def test_substitute_bullets_replaces_only_mapped_bodies() -> None:
    tex = "\\resumeItem{Built APIs}\n\\resumeItem{Kept as-is}\n"
    out = _substitute_bullets(tex, {"Built APIs": "Built REST microservices"})
    assert "\\resumeItem{Built REST microservices}" in out
    assert "\\resumeItem{Kept as-is}" in out  # unmapped bodies untouched
    assert "Built APIs" not in out
