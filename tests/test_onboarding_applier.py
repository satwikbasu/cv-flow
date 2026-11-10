import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from cvflow.knowledge import KnowledgeBase
from cvflow.onboarding import OnboardingError
from cvflow.onboarding.applier import apply_bundle
from cvflow.onboarding.bundle import FORM_FIELD_KEYS, PROFILE_DOC_KEYS, CandidateBundle
from cvflow.resume import ResumeTailor, parse_master

REPO = Path(__file__).resolve().parent.parent


def _bundle() -> CandidateBundle:
    ex = yaml.safe_load((REPO / "config.example.yaml").read_text())
    discovery = {**ex["discovery"], "search_terms": ["devops engineer"]}
    preferences = {**ex["preferences"], "prefer_roles": {"devops": 0.9}}
    block = (
        "\\resumeProjectHeading{\\textbf{%s} $|$ \\emph{Go}}{}\n"
        "\\resumeItemListStart\n\\resumeItem{Built it}\n\\resumeItemListEnd\n"
    )
    return CandidateBundle.model_validate({
        "profile_docs": {k: f"# {k}" for k in PROFILE_DOC_KEYS},
        "project_docs": {"alpha": "# alpha", "beta": "# beta"},
        "form_fields": {k: "" for k in FORM_FIELD_KEYS},
        "candidate_skills": {"skills": ["Kubernetes"], "synonyms": {"k8s": "kubernetes"}},
        "discovery": discovery,
        "preferences": preferences,
        "resume": {
            "heading_tex": "\\begin{center}\\textbf{Test Person}\\end{center}",
            "sections": {
                "projects": "",
                "skills": "\\section{Skills}\nPython, Go\n",
            },
            "project_blocks": {"alpha": block % "Alpha", "beta": block % "Beta"},
        },
        "review_notes": [],
    })


def _repo(tmp_path: Path, *, config: bool = False) -> Path:
    (tmp_path / "resume.template").mkdir()
    shutil.copy(REPO / "resume.template" / "master.tex", tmp_path / "resume.template")
    example = yaml.safe_load((REPO / "config.example.yaml").read_text())
    (tmp_path / "config.example.yaml").write_text(yaml.safe_dump(example))
    if config:
        example["telegram"]["bot_token"] = "SECRET-TOKEN"  # noqa: S105
        example["discovery"] = {**example["discovery"], "search_terms": ["old"]}
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(example))
    return tmp_path


def test_apply_fresh_no_config(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    res = apply_bundle(_bundle(), repo)
    kb = KnowledgeBase.load(repo / "profile")
    assert set(kb.project_docs()) == {"projects/alpha", "projects/beta"}
    assert [p.project_id for p in parse_master(repo / "resume").projects] == ["alpha", "beta"]
    assert res.backup_dir is None and not res.config_written
    assert res.staged_config is not None and res.staged_config.exists()
    assert not (repo / "config.yaml").exists()


def test_apply_updates_config_preserving_secrets(tmp_path: Path) -> None:
    repo = _repo(tmp_path, config=True)
    res = apply_bundle(_bundle(), repo)
    assert res.config_written and res.staged_config is None
    cfg: dict[str, Any] = yaml.safe_load((repo / "config.yaml").read_text())
    assert cfg["telegram"]["bot_token"] == "SECRET-TOKEN"  # noqa: S105
    assert cfg["discovery"] == _bundle().discovery
    assert cfg["preferences"] == _bundle().preferences
    assert cfg["discovery"]["search_terms"] == ["devops engineer"]


def test_partial_generated_sections_keep_template_defaults(tmp_path: Path) -> None:
    # Models often return only the keys they personalised; the rest must not vanish.
    repo = _repo(tmp_path, config=True)
    b = _bundle()
    b.discovery.clear()
    b.discovery["search_terms"] = ["platform engineer"]
    b.preferences.clear()
    b.preferences["prefer_roles"] = {"platform": 0.8}
    apply_bundle(b, repo)
    cfg: dict[str, Any] = yaml.safe_load((repo / "config.yaml").read_text())
    example = yaml.safe_load((REPO / "config.example.yaml").read_text())
    assert cfg["discovery"]["search_terms"] == ["platform engineer"]
    assert cfg["discovery"]["sites"] == example["discovery"]["sites"]
    assert cfg["preferences"]["prefer_roles"] == {"platform": 0.8}
    assert cfg["preferences"]["exclude_when"] == example["preferences"]["exclude_when"]


def test_generated_null_keeps_the_existing_value(tmp_path: Path) -> None:
    # The model is told never to invent a salary, so it may answer null; keep the default.
    repo = _repo(tmp_path, config=True)
    b = _bundle()
    b.preferences["min_ctc_lpa"] = None
    apply_bundle(b, repo)
    cfg: dict[str, Any] = yaml.safe_load((repo / "config.yaml").read_text())
    example = yaml.safe_load((REPO / "config.example.yaml").read_text())
    assert cfg["preferences"]["min_ctc_lpa"] == example["preferences"]["min_ctc_lpa"]


def test_wrong_shaped_or_unknown_generated_keys_are_ignored(tmp_path: Path) -> None:
    # Seen live: locations came back as a bare string. Keep the default and say so.
    repo = _repo(tmp_path, config=True)
    b = _bundle()
    b.discovery["locations"] = "Bengaluru, India"
    b.discovery["invented_knob"] = 3
    b.preferences["min_ctc_lpa"] = "seven"
    res = apply_bundle(b, repo)
    cfg: dict[str, Any] = yaml.safe_load((repo / "config.yaml").read_text())
    example = yaml.safe_load((REPO / "config.example.yaml").read_text())
    assert cfg["discovery"]["locations"] == example["discovery"]["locations"]
    assert "invented_knob" not in cfg["discovery"]
    assert cfg["preferences"]["min_ctc_lpa"] == example["preferences"]["min_ctc_lpa"]
    assert cfg["discovery"]["search_terms"] == ["devops engineer"]  # good keys still land
    assert set(res.ignored) == {
        "discovery.locations", "discovery.invented_knob", "preferences.min_ctc_lpa"
    }


def test_refuses_to_clobber_then_force_backs_up(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "profile").mkdir()
    (repo / "profile" / "keep.md").write_text("mine")
    with pytest.raises(OnboardingError):
        apply_bundle(_bundle(), repo)
    assert (repo / "profile" / "keep.md").read_text() == "mine"
    assert not (repo / "resume").exists()
    res = apply_bundle(_bundle(), repo, force=True)
    assert res.backup_dir is not None
    assert (res.backup_dir / "profile" / "keep.md").read_text() == "mine"
    assert not (repo / "profile" / "keep.md").exists()


def test_invalid_leaves_live_tree_untouched(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    # A base config that cannot validate (bad schedule time) must stop the apply cold.
    example = yaml.safe_load((repo / "config.example.yaml").read_text())
    example["schedule"]["daily_discovery_time"] = "25:99"
    (repo / "config.example.yaml").write_text(yaml.safe_dump(example))
    with pytest.raises(OnboardingError):
        apply_bundle(_bundle(), repo)
    assert not (repo / "profile").exists() and not (repo / "resume").exists()


def test_weights_that_do_not_sum_to_one_keep_the_defaults(tmp_path: Path) -> None:
    # Seen live: the model changed one weight only, so the pair summed to 0.5.
    repo = _repo(tmp_path, config=True)
    b = _bundle()
    b.preferences["fit_weight"] = 0.2
    res = apply_bundle(b, repo)
    cfg: dict[str, Any] = yaml.safe_load((repo / "config.yaml").read_text())
    example = yaml.safe_load((REPO / "config.example.yaml").read_text())
    assert cfg["preferences"]["fit_weight"] == example["preferences"]["fit_weight"]
    assert cfg["preferences"]["comp_weight"] == example["preferences"]["comp_weight"]
    assert "preferences.fit_weight" in res.ignored


def test_unsafe_slug_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    d = _bundle().model_dump()
    d["resume"]["sections"] = {"../evil": "x", "projects": ""}
    with pytest.raises(OnboardingError):
        apply_bundle(CandidateBundle.model_validate(d), repo)


@pytest.mark.skipif(shutil.which("tectonic") is None, reason="tectonic not installed")
def test_assembled_master_compiles(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    apply_bundle(_bundle(), repo)
    tailor = ResumeTailor(None, parse_master(repo / "resume"))  # type: ignore[arg-type]
    assert tailor.compile_master(tmp_path / "out").exists()


def test_projects_section_added_when_model_forgets_it(tmp_path: Path) -> None:
    # Models sometimes send project blocks but drop the empty "projects" key.
    data = _bundle().model_dump()
    data["resume"]["sections"] = {
        "experience": "\\section{Experience}\n",
        "skills": "\\section{Skills}\nPython, Go\n",
    }
    repo = _repo(tmp_path)
    apply_bundle(CandidateBundle.model_validate(data), repo)
    master = parse_master(repo / "resume")
    assert master.section_order == ["experience", "projects", "skills"]
    assert [p.project_id for p in master.projects] == ["alpha", "beta"]
