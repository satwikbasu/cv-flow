"""Compile the generated résumé with tectonic, asking a backend to repair LaTeX errors."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cvflow.onboarding import OnboardingError
from cvflow.onboarding.backends import GenerationBackend
from cvflow.onboarding.prompt import build_fix_prompt
from cvflow.resume import parse_master


@dataclass(frozen=True)
class CompileOutcome:
    ok: bool
    attempts: int
    log_excerpt: str
    flagged: bool
    lost: str = ""


def _excerpt(out: Path, root: Path, stderr: str) -> str:
    for log in (out / "master.log", root / "master.log"):
        if log.exists():
            lines = log.read_text(errors="replace").splitlines()
            keep: list[str] = []
            for i, line in enumerate(lines):
                if line.startswith("!"):
                    keep.append(line)
                    keep += [x for x in lines[i + 1 : i + 4] if x.startswith("l.")]
            if keep:
                return "\n".join(keep)
    return (stderr or "")[-2000:]


def _tex_files(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): p.read_text()
        for p in sorted(root.rglob("*.tex"))
    }


def _counts(root: Path) -> dict[str, int]:
    """A coarse structural fingerprint of the resume - how many bullets, projects, and
    sections it holds - so we can tell when an automatic fix quietly removed content."""
    blob = "\n".join(_tex_files(root).values())
    return {
        "bullets": blob.count("\\resumeItem{"),
        "projects": blob.count("\\resumeProjectHeading"),
        "sections": blob.count("\\section{"),
    }


def _loss_note(before: dict[str, int], after: dict[str, int]) -> str:
    """Name anything the repair shrank, or return '' when nothing was lost."""
    drops = [f"{k} {before[k]}->{after[k]}" for k in before if after[k] < before[k]]
    return "the automatic LaTeX fix removed content (" + ", ".join(drops) + ")" if drops else ""


def _write_repairs(root: Path, files: dict[str, str]) -> None:
    base = root.resolve()
    for name, body in files.items():
        target = (base / name).resolve()
        if not target.is_relative_to(base):
            raise OnboardingError(f"repair tried to write outside the resume tree: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)


def compile_with_repair(
    resume_root: str | Path,
    backend: GenerationBackend,
    *,
    attempts: int = 3,
    runner: Callable[..., Any] = subprocess.run,
) -> CompileOutcome:
    root = Path(resume_root)
    baseline = _counts(root)
    excerpt = ""
    repaired = False
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        for n in range(1, attempts + 1):
            res = runner(
                ["tectonic", "-X", "compile", "--untrusted", "--keep-logs",
                 "-o", str(out), str(root / "master.tex")],
                capture_output=True,
                text=True,
            )
            if res.returncode == 0 and (out / "master.pdf").exists():
                lost = _loss_note(baseline, _counts(root)) if repaired else ""
                return CompileOutcome(True, n, "", bool(lost), lost)
            excerpt = _excerpt(out, root, res.stderr)
            if n == attempts:
                break
            _write_repairs(root, backend.repair(build_fix_prompt(_tex_files(root), excerpt)))
            repaired = True
            try:
                parse_master(root)
            except Exception as exc:
                raise OnboardingError(f"repair broke the resume structure: {exc}") from exc
    (root / "ONBOARDING_FLAGGED.txt").write_text(excerpt + "\n")
    return CompileOutcome(False, attempts, excerpt, True)
