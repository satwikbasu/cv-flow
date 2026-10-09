import shutil
from pathlib import Path

import pytest
from tests.test_onboarding_applier import _bundle, _repo

from cvflow.knowledge import KnowledgeBase
from cvflow.onboarding import OnboardingError
from cvflow.onboarding.backends import GenerationBackend
from cvflow.onboarding.bundle import CandidateBundle
from cvflow.onboarding.compile_repair import CompileOutcome, compile_with_repair
from cvflow.runs import run_onboard

OK = CompileOutcome(True, 1, "", False)


class FakeBackend:
    def __init__(self, bundle: CandidateBundle | None = None) -> None:
        self.bundle = bundle

    def generate_bundle(self, prompt: str) -> CandidateBundle:
        if self.bundle is None:
            raise OnboardingError("model said no")
        return self.bundle

    def repair(self, prompt: str) -> dict[str, str]:
        return {}


def _resume(tmp_path: Path) -> Path:
    f = tmp_path / "cv.txt"
    f.write_text("Test Person\nDevOps engineer")
    return f


def test_run_onboard_happy_path(tmp_path: Path) -> None:
    repo = _repo(_mk(tmp_path), config=True)
    msgs: list[str] = []
    out = run_onboard(
        [_resume(tmp_path)], backend=FakeBackend(_bundle()), repo_root=repo, user_facts={},
        notify=msgs.append, compile_repair=lambda *a, **k: OK,
    )
    assert out.ok and not out.flagged and out.report
    assert out.pdf_path is None or out.pdf_path.name == "onboarding.pdf"
    KnowledgeBase.load(repo / "profile", repo / "profile" / "form_fields.json")
    assert msgs[0] == "reading resume..."


def _mk(tmp_path: Path) -> Path:
    d = tmp_path / "repo"
    d.mkdir()
    return d


def test_run_onboard_generation_failure_applies_nothing(tmp_path: Path) -> None:
    repo = _repo(_mk(tmp_path))
    out = run_onboard([_resume(tmp_path)], backend=FakeBackend(), repo_root=repo, user_facts={})
    assert not out.ok and out.flagged and "model said no" in out.report[0]
    assert not (repo / "profile").exists()


def test_run_onboard_parse_failure(tmp_path: Path) -> None:
    repo = _repo(_mk(tmp_path))
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    for f in (empty, tmp_path / "missing.txt"):
        out = run_onboard([f], backend=FakeBackend(_bundle()), repo_root=repo, user_facts={})
        assert not out.ok and out.flagged
        assert "Could not read your resume" in out.report[0]
    assert not (repo / "profile").exists()


def test_run_onboard_refuses_clobber(tmp_path: Path) -> None:
    repo = _repo(_mk(tmp_path), config=True)
    kw = {"backend": FakeBackend(_bundle()), "repo_root": repo, "user_facts": {}}
    run_onboard([_resume(tmp_path)], compile_repair=lambda *a, **k: OK, **kw)  # type: ignore[arg-type]
    out = run_onboard([_resume(tmp_path)], compile_repair=lambda *a, **k: OK, **kw)  # type: ignore[arg-type]
    assert not out.ok and not out.flagged and "Could not apply" in out.report[0]


@pytest.mark.skipif(shutil.which("tectonic") is None, reason="tectonic not installed")
def test_run_onboard_real_compile(tmp_path: Path) -> None:
    repo = _repo(_mk(tmp_path), config=True)
    backend: GenerationBackend = FakeBackend(_bundle())
    out = run_onboard(
        [_resume(tmp_path)], backend=backend, repo_root=repo, user_facts={},
        compile_repair=compile_with_repair,
    )
    assert out.ok, out.report
    assert out.pdf_path == repo / "data" / "resumes" / "onboarding.pdf"
    assert out.pdf_path.read_bytes().startswith(b"%PDF")
