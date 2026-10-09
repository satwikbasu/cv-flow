import shutil
from pathlib import Path

import yaml

from cvflow.checkup import main, run_checkup
from cvflow.onboarding.applier import apply_bundle
from test_onboarding_applier import _bundle, _repo


def _good_tree(tmp_path: Path, *, frontier: bool = False) -> tuple[Path, Path]:
    repo = _repo(tmp_path)
    apply_bundle(_bundle(), repo)
    cfg = yaml.safe_load((repo / "config.example.yaml").read_text())
    cfg["resume"]["master_tex_path"] = str(repo / "resume" / "master.tex")
    cfg["storage"]["form_fields_path"] = str(repo / "profile" / "form_fields.json")
    cfg["storage"]["db_path"] = str(repo / "data" / "cvflow.db")
    cfg["profile"]["knowledge_base_dir"] = str(repo / "profile")
    if frontier:
        cfg["onboarding"] = {
            "frontier": {
                "provider": "x", "api_key": "k", "base_url": "https://f.example/v1", "model": "m"
            }
        }
    path = repo / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return repo, path


def _run(repo: Path, path: Path, **kw: object) -> tuple[int, str]:
    out: list[str] = []
    code = run_checkup(repo, path, echo=out.append, **kw)  # type: ignore[arg-type]
    return code, "\n".join(out)


def test_good_tree_passes(tmp_path: Path) -> None:
    repo, path = _good_tree(tmp_path, frontier=True)
    probed: list[str] = []

    def probe(base_url: str, api_key: str) -> bool:
        probed.append(base_url)
        return True

    code, text = _run(repo, path, probe=probe)
    assert code == 0, text
    assert "✗" not in text
    for needle in ("Config loads", "Knowledge base", "Skills profile", "wired"):
        assert f"✓ {needle}" in text or needle in text
    assert len(probed) == 3
    assert text.count("✓") >= 6 - (0 if shutil.which("tectonic") else 1)
    # form_fields are all empty in the fixture: reported but not a failure.
    assert "empty form fields" in text


def test_offline_skips_providers(tmp_path: Path) -> None:
    repo, path = _good_tree(tmp_path)

    def probe(base_url: str, api_key: str) -> bool:  # pragma: no cover
        raise AssertionError("must not probe offline")

    code, text = _run(repo, path, offline=True, probe=probe)
    assert code == 0, text
    assert "offline" in text


def test_probe_failure_fails(tmp_path: Path) -> None:
    repo, path = _good_tree(tmp_path)
    code, text = _run(repo, path, probe=lambda _u, _k: False)
    assert code == 1
    assert "✗ Provider llm.distillation" in text


def test_project_mismatch_fails(tmp_path: Path) -> None:
    repo, path = _good_tree(tmp_path)
    (repo / "profile" / "projects" / "gamma.md").write_text("# gamma")
    code, text = _run(repo, path, offline=True)
    assert code == 1
    assert "✗" in text and "gamma" in text


def test_bad_config_fails_and_skips_dependents(tmp_path: Path) -> None:
    bad = tmp_path / "config.yaml"
    bad.write_text("telegram: 3\n")
    code, text = _run(tmp_path, bad, offline=True)
    assert code == 1
    assert "✗ Config loads" in text and "Knowledge base" not in text


def test_missing_skills_file_fails_with_hint(tmp_path: Path) -> None:
    repo, path = _good_tree(tmp_path)
    (repo / "profile" / "candidate_skills.yaml").unlink()
    code, text = _run(repo, path, offline=True)
    assert code == 1
    assert "/onboard" in text


def test_main_honors_env_config(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    repo, path = _good_tree(tmp_path)
    monkeypatch.setenv("CVFLOW_CONFIG", str(path))
    monkeypatch.chdir(repo)
    assert main(["--offline"]) == 0
    assert "✓ Config loads" in capsys.readouterr().out
