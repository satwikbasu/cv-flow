"""Deterministic install self-test: ``python -m cvflow.checkup [--config PATH] [--offline]``.

Prints one ✓/✗ line per check and returns 1 if any fail. Empty form fields are reported but are
expected (the user fills them), so they never fail the run.
"""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from cvflow.config import Config, ConfigError, FrontierConfig, ProviderConfig, load_config
from cvflow.discovery.skills import load_skill_profile
from cvflow.knowledge import KnowledgeBase
from cvflow.resume import CompileError, ResumeTailor, parse_master

__all__ = ["run_checkup", "main"]


def _urllib_probe(base_url: str, api_key: str) -> bool:
    from urllib.request import Request, urlopen

    req = Request(  # noqa: S310 (https by config)
        base_url.rstrip("/") + "/models",
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) cvflow/1.0",
        },
    )
    try:
        with urlopen(req, timeout=10) as resp:  # noqa: S310
            return bool(resp.status == 200)
    except Exception:  # noqa: BLE001 — any failure means unreachable; the caller reports it
        return False


def run_checkup(
    repo_root: str | Path = ".",
    config_path: str | Path = "config.yaml",
    *,
    offline: bool = False,
    echo: Callable[[str], None] = print,
    probe: Callable[[str, str], bool] = _urllib_probe,
) -> int:
    root = Path(repo_root)
    failures = 0

    def ok(msg: str) -> None:
        echo(f"✓ {msg}")

    def bad(msg: str) -> None:
        nonlocal failures
        failures += 1
        echo(f"✗ {msg}")

    def at(p: str | Path) -> Path:
        return root / p  # an absolute p wins

    try:
        cfg: Config = load_config(at(config_path))
    except ConfigError as exc:
        bad(f"Config loads: {exc}")
        return 1
    ok(f"Config loads ({at(config_path)})")

    profile = at(cfg.profile.knowledge_base_dir)
    try:
        kb = KnowledgeBase.load(profile, at(cfg.storage.form_fields_path))
        ok(f"Knowledge base loads ({len(kb.documents)} documents)")
        missing = kb.missing_form_fields()
        if missing:
            echo(f"  INFO {len(missing)} empty form fields to fill in: {', '.join(missing)}")
    except Exception as exc:  # noqa: BLE001 — any load failure is a reported check failure
        bad(f"Knowledge base loads: {exc}")

    skills_path = profile / "candidate_skills.yaml"
    try:
        if not skills_path.exists():
            bad(f"Skills profile: {skills_path} missing — run /onboard to create it")
        else:
            skills, _syn = load_skill_profile(skills_path)
            if skills:
                ok(f"Skills profile loads ({len(skills)} skills)")
            else:
                bad(f"Skills profile: {skills_path} is empty — run /onboard")
    except Exception as exc:  # noqa: BLE001
        bad(f"Skills profile: {exc}")

    master = None
    try:
        master = parse_master(at(cfg.resume.master_tex_path).parent)
        wired = {p.project_id for p in master.projects}
        stems = {p.stem for p in (profile / "projects").glob("*.md")}
        if not master.section_order:
            bad("Résumé wiring: master.tex includes no sections")
        elif wired != stems:
            bad(
                "Résumé wiring: projects mismatch — "
                f"only in résumé: {sorted(wired - stems)}, only in profile: {sorted(stems - wired)}"
            )
        else:
            ok(f"Résumé wired ({len(master.section_order)} sections, {len(wired)} projects)")
    except Exception as exc:  # noqa: BLE001
        bad(f"Résumé wiring: {exc}")

    if master is None:
        echo("  INFO résumé compile skipped (résumé did not parse)")
    elif shutil.which("tectonic") is None:
        echo("  INFO résumé compile skipped (tectonic not on PATH)")
    else:
        try:
            with tempfile.TemporaryDirectory() as out:
                ResumeTailor(None, master).compile_master(out)  # type: ignore[arg-type]
            ok("Résumé compiles")
        except CompileError as exc:
            bad(f"Résumé compiles: {exc}")

    if offline:
        echo("  INFO provider reachability skipped (--offline)")
    else:
        providers: list[tuple[str, ProviderConfig | FrontierConfig]] = [
            ("llm.distillation", cfg.llm.distillation),
            ("llm.tailoring", cfg.llm.tailoring),
        ]
        if cfg.onboarding.frontier is not None:
            providers.append(("onboarding.frontier", cfg.onboarding.frontier))
        for name, p in providers:
            base_url = p.base_url
            if probe(base_url, p.api_key):
                ok(f"Provider {name} reachable ({base_url})")
            else:
                bad(f"Provider {name} unreachable or rejected the key ({base_url})")

    echo("All checks passed." if not failures else f"{failures} check(s) failed.")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cvflow.checkup")
    ap.add_argument("--config", default=os.environ.get("CVFLOW_CONFIG", "config.yaml"))
    ap.add_argument("--offline", action="store_true")
    ns = ap.parse_args(argv)
    return run_checkup(".", ns.config, offline=ns.offline)


if __name__ == "__main__":
    raise SystemExit(main())
