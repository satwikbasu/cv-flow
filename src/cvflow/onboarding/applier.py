"""Validate an onboarding bundle in a staging tree, then swap it into the live repo."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from cvflow.config import ConfigError, load_config
from cvflow.discovery.skills import load_skill_profile
from cvflow.knowledge import KnowledgeBase
from cvflow.onboarding import OnboardingError
from cvflow.onboarding.bundle import CandidateBundle
from cvflow.resume import parse_master

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class AppliedResult:
    profile_dir: Path
    resume_dir: Path
    backup_dir: Path | None
    config_written: bool
    staged_config: Path | None


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _check_names(kind: str, names: list[str]) -> None:
    bad = [n for n in names if not _NAME_RE.match(n)]
    if bad:
        raise OnboardingError(f"unsafe {kind} name(s) (use letters, digits, - and _): {bad}")


def _stage_profile(bundle: CandidateBundle, root: Path) -> None:
    for key, text in bundle.profile_docs.items():
        _write(root / f"{key}.md", text)
    for slug, text in bundle.project_docs.items():
        _write(root / "projects" / f"{slug}.md", text)
    _write(root / "form_fields.json", json.dumps(bundle.form_fields, indent=2))
    _write(
        root / "candidate_skills.yaml",
        yaml.safe_dump(
            {
                "skills": bundle.candidate_skills.skills,
                "synonyms": bundle.candidate_skills.synonyms,
            }
        ),
    )


def _stage_resume(bundle: CandidateBundle, repo_root: Path, root: Path) -> None:
    res = bundle.resume
    names = list(res.sections)
    _check_names("section", names)
    _check_names("project", list(res.project_blocks))
    if res.project_blocks and "projects" not in res.sections:
        raise OnboardingError("project_blocks given but resume.sections has no 'projects' key")
    template_path = repo_root / "resume.template" / "master.tex"
    if not template_path.exists():
        raise OnboardingError(f"resume template not found: {template_path}")
    template = template_path.read_text()
    for marker in ("%__HEADING__", "%__SECTIONS__"):
        if marker not in template:
            raise OnboardingError(f"resume template is missing {marker}")
    inputs = "\n".join(f"\\input{{sections/{n}.tex}}" for n in names)
    master = template.replace("%__HEADING__", res.heading_tex).replace("%__SECTIONS__", inputs)
    _write(root / "master.tex", master)
    for name, body in res.sections.items():
        if name != "projects":
            _write(root / "sections" / f"{name}.tex", body)
    if "projects" in res.sections:
        lines = ["\\section{Projects}", "\\resumeSubHeadingListStart"]
        lines += [f"  \\input{{sections/projects/{s}.tex}}" for s in res.project_blocks]
        lines.append("\\resumeSubHeadingListEnd")
        _write(root / "sections" / "projects.tex", "\n".join(lines) + "\n")
        for slug, block in res.project_blocks.items():
            _write(root / "sections" / "projects" / f"{slug}.tex", block)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise OnboardingError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise OnboardingError(f"{path} root must be a mapping")
    return data


def _validate(bundle: CandidateBundle, profile: Path, resume: Path) -> None:
    try:
        KnowledgeBase.load(profile, profile / "form_fields.json")
        load_skill_profile(profile / "candidate_skills.yaml")
    except Exception as exc:
        raise OnboardingError(f"staged profile failed validation: {exc}") from exc
    try:
        m = parse_master(resume)
    except Exception as exc:
        raise OnboardingError(f"staged resume failed to parse: {exc}") from exc
    if len(m.section_order) < 1:
        raise OnboardingError("staged resume has no sections")
    got = {p.project_id for p in m.projects}
    if got != set(bundle.project_docs):
        raise OnboardingError(
            f"resume projects {sorted(got)} != project_docs {sorted(bundle.project_docs)}"
        )


def apply_bundle(
    bundle: CandidateBundle,
    repo_root: str | Path,
    *,
    config_path: str | Path = "config.yaml",
    force: bool = False,
) -> AppliedResult:
    repo_root = Path(repo_root)
    config_path = repo_root / config_path  # absolute config_path wins
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    onboarding = repo_root / "data" / "onboarding"
    staging = onboarding / f"staging-{stamp}"
    s_profile, s_resume, s_config = staging / "profile", staging / "resume", staging / "config.yaml"
    try:
        _stage_profile(bundle, s_profile)
        _stage_resume(bundle, repo_root, s_resume)
        _validate(bundle, s_profile, s_resume)
        base = config_path if config_path.exists() else repo_root / "config.example.yaml"
        if not base.exists():
            raise OnboardingError(f"no config to validate against: {base}")
        merged = _read_yaml(base)
        merged["discovery"] = bundle.discovery
        merged["preferences"] = bundle.preferences
        _write(s_config, yaml.safe_dump(merged, sort_keys=False))
        try:
            load_config(s_config)
        except ConfigError as exc:
            raise OnboardingError(f"merged config failed validation: {exc}") from exc

        live_profile, live_resume = repo_root / "profile", repo_root / "resume"
        if live_profile.exists() and any(live_profile.iterdir()) and not force:
            raise OnboardingError(
                f"{live_profile} is not empty; refusing to overwrite (use force=True)"
            )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    backup: Path | None = None
    if live_profile.exists() or live_resume.exists():
        backup = onboarding / f"backup-{stamp}"
        backup.mkdir(parents=True)
        for live in (live_profile, live_resume):
            if live.exists():
                shutil.move(str(live), str(backup / live.name))
    shutil.move(str(s_profile), str(live_profile))
    shutil.move(str(s_resume), str(live_resume))

    if config_path.exists():
        live_cfg = _read_yaml(config_path)
        live_cfg["discovery"] = bundle.discovery
        live_cfg["preferences"] = bundle.preferences
        config_path.write_text(yaml.safe_dump(live_cfg, sort_keys=False))
        shutil.rmtree(staging, ignore_errors=True)
        return AppliedResult(live_profile, live_resume, backup, True, None)
    return AppliedResult(live_profile, live_resume, backup, False, s_config)
