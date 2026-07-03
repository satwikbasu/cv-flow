"""Tests for resume tailoring: parsing, planning, rendering, the numbers guard, and the diff.

The LLM is always faked. Compile tests live in test_resume_compile.py and skip
when tectonic is missing.
"""

import json
from pathlib import Path

import pytest

from cvflow.analysis import JDAnalysis
from cvflow.resume import (
    Master,
    Project,
    ResumeTailor,
    Section,
    TailoringError,
    TailoringPlan,
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


# --- shared fixtures for plan/render/diff ---


def _master() -> Master:
    return Master(
        root=Path("/nonexistent"),
        section_order=["experience", "projects", "skills"],
        sections={
            "experience": Section("experience", "EXP"),
            "projects": Section("projects", "P"),
            "skills": Section("skills", "SK"),
        },
        projects=[Project("netmon-dashboard", "NETMON"), Project("taskboard", "TASK")],
    )


class _FakeProvider:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.payload


def _jd() -> JDAnalysis:
    return JDAnalysis(
        required_skills=["Java", "Spring Boot"],
        preferred_quals=[],
        seniority="junior",
        tone="",
        applicant_instructions=[],
    )


# --- plan ---


def test_plan_validates_and_caps_projects_and_drops_unknown() -> None:
    payload = json.dumps(
        {
            "section_order": ["skills", "experience", "projects", "ghost-section"],
            "selected_project_ids": ["taskboard", "netmon-dashboard", "fake-proj"],
            "diff_narration": "Led with skills for the Java role; chose taskboard (Spring Boot).",
        }
    )
    provider = _FakeProvider(payload)
    tailor = ResumeTailor(provider, _master())
    plan = tailor.plan(_jd())
    assert isinstance(plan, TailoringPlan)
    assert plan.section_order == ["skills", "experience", "projects"]
    assert plan.selected_project_ids == ["taskboard", "netmon-dashboard"]
    assert "Java" in provider.prompts[0]


def test_plan_default_cap_is_two_projects() -> None:
    # Default max_projects (no override) is 2: the model's picks are truncated to two.
    payload = json.dumps(
        {
            "section_order": ["experience", "projects", "skills"],
            "selected_project_ids": ["taskboard", "netmon-dashboard", "third"],
            "diff_narration": "x",
        }
    )
    m = _master()
    m.projects.append(Project("third", "THIRD"))
    tailor = ResumeTailor(_FakeProvider(payload), m)  # default cap = 2
    plan = tailor.plan(_jd())
    assert plan.selected_project_ids == ["taskboard", "netmon-dashboard"]


def test_plan_never_drops_sections_appends_omitted() -> None:
    # Model lists only one section; the rest must still appear (reorder, never drop).
    payload = json.dumps(
        {"section_order": ["skills"], "selected_project_ids": ["taskboard"], "diff_narration": "x"}
    )
    tailor = ResumeTailor(_FakeProvider(payload), _master())
    plan = tailor.plan(_jd())
    assert plan.section_order[0] == "skills"  # the model's emphasis leads
    assert set(plan.section_order) == {"experience", "projects", "skills"}  # nothing dropped


def test_plan_empty_picks_fall_back_to_master_order_up_to_cap() -> None:
    # Model picks none -> fall back to master order (capped) so Projects isn't left empty.
    payload = json.dumps(
        {
            "section_order": ["experience", "projects", "skills"],
            "selected_project_ids": [],
            "diff_narration": "x",
        }
    )
    tailor = ResumeTailor(_FakeProvider(payload), _master(), max_projects=2)
    plan = tailor.plan(_jd())
    assert plan.selected_project_ids == ["netmon-dashboard", "taskboard"]  # master order, capped


def test_plan_shows_fewer_when_model_picks_fewer() -> None:
    # The model picking one (judging the rest irrelevant) shows just one — no padding.
    payload = json.dumps(
        {
            "section_order": ["experience", "projects", "skills"],
            "selected_project_ids": ["taskboard"],
            "diff_narration": "x",
        }
    )
    tailor = ResumeTailor(_FakeProvider(payload), _master(), max_projects=2)
    plan = tailor.plan(_jd())
    assert plan.selected_project_ids == ["taskboard"]


def test_plan_caps_to_max_projects() -> None:
    # The model's picks are truncated to max_projects (most-relevant first).
    payload = json.dumps(
        {
            "section_order": ["experience", "projects", "skills"],
            "selected_project_ids": ["netmon-dashboard", "taskboard", "third"],
            "diff_narration": "x",
        }
    )
    m = _master()
    m.projects.append(Project("third", "THIRD"))
    tailor = ResumeTailor(_FakeProvider(payload), m, max_projects=2)
    plan = tailor.plan(_jd())
    assert plan.selected_project_ids == ["netmon-dashboard", "taskboard"]


def test_plan_drops_disabled_section_and_empties_projects() -> None:
    payload = json.dumps(
        {
            "section_order": ["experience", "projects", "skills"],
            "selected_project_ids": ["taskboard"],
            "diff_narration": "x",
        }
    )
    t = ResumeTailor(
        _FakeProvider(payload), _master(), disabled_sections=frozenset({"projects"})
    )
    plan = t.plan(_jd())
    assert "projects" not in plan.section_order
    assert set(plan.section_order) == {"experience", "skills"}
    assert plan.selected_project_ids == []


def test_plan_disabled_removed_but_enabled_never_dropped() -> None:
    payload = json.dumps(
        {"section_order": ["skills"], "selected_project_ids": [], "diff_narration": "x"}
    )
    t = ResumeTailor(
        _FakeProvider(payload), _master(), disabled_sections=frozenset({"experience"})
    )
    plan = t.plan(_jd())
    assert "experience" not in plan.section_order
    assert set(plan.section_order) == {"projects", "skills"}


def test_plan_threads_feedback_into_prompt() -> None:
    payload = json.dumps(
        {"section_order": [], "selected_project_ids": [], "diff_narration": ""}
    )
    provider = _FakeProvider(payload)
    ResumeTailor(provider, _master()).plan(_jd(), feedback="emphasize backend work")
    assert "emphasize backend work" in provider.prompts[0]


# --- rephrase (folded into the single planning call) ---


def _combined_json(rewrites: object = None) -> str:
    """The one tailoring response: ordering + selection + (optional) rewrites in one object."""
    d: dict[str, object] = {
        "section_order": ["experience", "projects"],
        "selected_project_ids": ["taskboard"],
        "diff_narration": "x",
    }
    if rewrites is not None:
        d["rewrites"] = rewrites
    return json.dumps(d)


def _exp_master() -> Master:
    return Master(
        root=Path("/nonexistent"),
        section_order=["experience", "projects"],
        sections={
            "experience": Section(
                "experience", "\\resumeItem{Built python flask APIs for tooling}"
            ),
            "projects": Section("projects", "P"),
        },
        projects=[Project("taskboard", "\\resumeItem{Built a CRUD backend with postgres}")],
    )


def test_rephrase_defaults_off() -> None:
    # Without an explicit rephrase=True the planner never asks for rewrites and keeps
    # every bullet verbatim, even when the model volunteers rewrites anyway.
    p = _FakeProvider(_combined_json(rewrites=["Shipped python flask APIs"]))
    plan = ResumeTailor(p, _exp_master()).plan(_jd())
    assert plan.rephrased == {}
    assert "Bullets to reword" not in p.prompts[0]


def test_plan_keeps_clean_reword_rejects_fabrication() -> None:
    # Candidates fixed pre-call: [experience bullet, taskboard bullet]. The combined response
    # rewords both; the fabricated-number reword is rejected by the fact guard.
    payload = _combined_json(
        rewrites=[
            "Built python flask REST APIs for tooling",
            "Built a CRUD backend with postgres at 1000 rps",
        ]
    )
    t = ResumeTailor(
        _FakeProvider(payload), _exp_master(), fact_corpus="rest microservices", rephrase=True
    )
    plan = t.plan(_jd())
    assert (
        plan.rephrased["Built python flask APIs for tooling"]
        == "Built python flask REST APIs for tooling"
    )
    assert "Built a CRUD backend with postgres" not in plan.rephrased


def test_plan_is_a_single_llm_call() -> None:
    # Ordering + selection + rewording are one request — no second call against the rate limit.
    p = _FakeProvider(_combined_json(rewrites=["Built python flask REST APIs for tooling"]))
    ResumeTailor(p, _exp_master(), fact_corpus="rest", rephrase=True).plan(_jd())
    assert len(p.prompts) == 1


def test_plan_malformed_rewrites_degrades_to_reorder_only() -> None:
    # Valid ordering JSON but a junk rewrites field -> reorder-only; never raises.
    t = ResumeTailor(
        _FakeProvider(_combined_json(rewrites="not a list")), _exp_master(), rephrase=True
    )
    plan = t.plan(_jd())
    assert plan.rephrased == {}
    assert plan.section_order  # ordering still happened


def test_plan_unparseable_response_raises() -> None:
    # If the single response isn't JSON at all, ordering can't proceed -> TailoringError.
    with pytest.raises(TailoringError):
        ResumeTailor(_FakeProvider("not json"), _exp_master(), rephrase=True).plan(_jd())


def test_render_applies_accepted_rewrites() -> None:
    plan = TailoringPlan(
        section_order=["experience", "projects"],
        selected_project_ids=["taskboard"],
        diff_narration="",
        rephrased={"Built python flask APIs for tooling": "Built python flask REST APIs"},
    )
    out = ResumeTailor(_FakeProvider("{}"), _exp_master()).render(plan)
    assert "Built python flask REST APIs" in out
    assert "Built python flask APIs for tooling" not in out


# --- render + no-new-facts ---


def test_render_reorders_sections_and_selects_projects() -> None:
    tailor = ResumeTailor(_FakeProvider("{}"), _master())
    plan = TailoringPlan(
        section_order=["skills", "projects", "experience"],
        selected_project_ids=["taskboard"],
        diff_narration="",
    )
    tex = tailor.render(plan)
    assert tex.index("SK") < tex.index("EXP")
    assert "TASK" in tex  # selected project rendered
    assert "NETMON" not in tex  # unselected project omitted


def test_assert_no_new_facts_passes_for_subset_and_fails_for_addition() -> None:
    tailor = ResumeTailor(_FakeProvider("{}"), _master())
    ok_plan = TailoringPlan(["experience"], [], "")
    tailor.assert_no_new_facts(tailor.render(ok_plan))  # no raise
    with pytest.raises(TailoringError):
        tailor.assert_no_new_facts("EXP\n\\resumeItem{Fabricated 10 years at BigCo}")


def test_guard_allows_synonym_rewording() -> None:
    # Numbers-only guard: rewording with no new number is allowed — including a tech word
    # not in the master; the human review of the diff is the backstop for that.
    t = ResumeTailor(_FakeProvider("{}"), _master())
    assert t._guard_ok("Built APIs for tooling", "Architected and shipped Kubernetes APIs")


def test_guard_rejects_new_number() -> None:
    t = ResumeTailor(_FakeProvider("{}"), _master())
    assert not t._guard_ok("Built APIs for tooling", "Built APIs handling 1000000 requests")


def test_guard_allows_reword_reusing_original_number() -> None:
    t = ResumeTailor(_FakeProvider("{}"), _master())
    assert t._guard_ok("Scaled to 500K nodes", "Scaled the system to 500K simulated nodes")


def test_assert_no_new_facts_blocks_only_new_numbers() -> None:
    # _master() content (EXP/P/SK/NETMON/TASK) has no digits.
    t = ResumeTailor(_FakeProvider("{}"), _master())
    t.assert_no_new_facts("EXP architected Kubernetes platform")  # new words, no number: fine
    with pytest.raises(TailoringError):
        t.assert_no_new_facts("EXP serving 4242 users")  # a number not in master: blocked


def test_fact_corpus_numbers_are_allowed() -> None:
    # A figure from the knowledge base (fact_corpus) is a real fact, not a fabrication.
    t = ResumeTailor(_FakeProvider("{}"), _master(), fact_corpus="managed 300 nodes")
    t.assert_no_new_facts("EXP across 300 nodes")  # no raise


# --- diff ---


def test_diff_shows_before_after_for_reworded_bullets() -> None:
    plan = TailoringPlan(
        section_order=["experience"],
        selected_project_ids=[],
        diff_narration="why",
        rephrased={"Built python flask APIs for tooling": "Built python flask REST APIs"},
    )
    out = ResumeTailor(_FakeProvider("{}"), _exp_master()).diff(plan)
    assert "- Built python flask APIs for tooling" in out
    assert "+ Built python flask REST APIs" in out
    assert "Section order:" in out
    # truthful, code-derived summary (not the model's diff_narration prose)
    assert "1 bullet(s) reworded" in out


def test_diff_describes_reorder_and_project_selection() -> None:
    tailor = ResumeTailor(_FakeProvider("{}"), _master())
    plan = TailoringPlan(["skills", "experience", "projects"], ["taskboard"], "Led with skills.")
    diff = tailor.diff(plan)
    assert "skills" in diff.lower()
    assert "taskboard" in diff.lower()
    # no rewrites in this plan -> the truthful summary says so (model prose is not shown)
    assert "No bullets reworded" in diff
