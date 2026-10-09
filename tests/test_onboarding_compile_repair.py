import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cvflow.onboarding.compile_repair import compile_with_repair

REPO = Path(__file__).resolve().parent.parent


class FakeBackend:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.prompts: list[str] = []

    def repair(self, prompt: str) -> dict[str, str]:
        self.prompts.append(prompt)
        return self.files


def _tree(root: Path) -> Path:
    (root / "sections").mkdir(parents=True)
    (root / "master.tex").write_text("\\input{sections/skills.tex}\n")
    (root / "sections" / "skills.tex").write_text("\\section{Skills}\nPython\n")
    return root


def _runner(results: list[bool]) -> Any:
    def run(cmd: list[str], **kw: Any) -> Any:
        ok = results.pop(0)
        if ok:
            (Path(cmd[cmd.index("-o") + 1]) / "master.pdf").write_bytes(b"%PDF")
        return SimpleNamespace(returncode=0 if ok else 1, stderr="! Undefined control sequence.")

    return run


def test_fail_fail_pass(tmp_path: Path) -> None:
    root = _tree(tmp_path / "resume")
    be = FakeBackend({"sections/skills.tex": "\\section{Skills}\nGo\n"})
    out = compile_with_repair(root, be, runner=_runner([False, False, True]))
    assert out.ok and out.attempts == 3 and not out.flagged
    assert "Go" in (root / "sections" / "skills.tex").read_text()


def test_always_fails_flags(tmp_path: Path) -> None:
    root = _tree(tmp_path / "resume")
    be = FakeBackend({})
    out = compile_with_repair(root, be, runner=_runner([False] * 3))
    assert out.flagged and not out.ok
    assert "Undefined control sequence" in (root / "ONBOARDING_FLAGGED.txt").read_text()
    assert len(be.prompts) == 2


def test_traversal_rejected(tmp_path: Path) -> None:
    root = _tree(tmp_path / "resume")
    from cvflow.onboarding import OnboardingError

    with pytest.raises(OnboardingError):
        compile_with_repair(root, FakeBackend({"../evil.tex": "x"}), runner=_runner([False, True]))
    assert not (tmp_path / "evil.tex").exists()


@pytest.mark.skipif(shutil.which("tectonic") is None, reason="tectonic not installed")
def test_real_tectonic_repair(tmp_path: Path) -> None:
    root = tmp_path / "resume"
    (root / "sections").mkdir(parents=True)
    tpl = (REPO / "resume.template" / "master.tex").read_text()
    master = tpl.replace("%__HEADING__", "Test").replace(
        "%__SECTIONS__", "\\input{sections/skills.tex}"
    )
    (root / "master.tex").write_text(master)
    good = "\\section{Skills}\nPython\n"
    (root / "sections" / "skills.tex").write_text("\\section{Skills}\n\\brokenmacro{x}\n")
    out = compile_with_repair(
        root, FakeBackend({"sections/skills.tex": good}), runner=subprocess.run
    )
    assert out.ok
