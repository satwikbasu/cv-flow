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
    excerpt = ""
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
                return CompileOutcome(True, n, "", False)
            excerpt = _excerpt(out, root, res.stderr)
            if n == attempts:
                break
            _write_repairs(root, backend.repair(build_fix_prompt(_tex_files(root), excerpt)))
            try:
                parse_master(root)
            except Exception as exc:
                raise OnboardingError(f"repair broke the resume structure: {exc}") from exc
    (root / "ONBOARDING_FLAGGED.txt").write_text(excerpt + "\n")
    return CompileOutcome(False, attempts, excerpt, True)
