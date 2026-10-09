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
    b = _bundle()
    b.preferences["fit_weight"] = 0.9  # config validation: weights must sum to 1.0
    b.preferences["comp_weight"] = 0.9
    with pytest.raises(OnboardingError):
        apply_bundle(b, repo)
    assert not (repo / "profile").exists() and not (repo / "resume").exists()


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
