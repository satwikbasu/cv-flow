"""Headless entry points: ``python -m cvflow.cli discover|tailor <job_id>|heartbeat``.

Reuses the exact run bodies the bot uses, so a --no-services install (or a CI
smoke run) exercises the same code path — just with stdout instead of chat.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from cvflow.runs import run_heartbeat, run_onboard

_USAGE = "usage: python -m cvflow.cli <discover|tailor <job_id>|heartbeat|onboard <file...>>"


def _pop_config(args: list[str]) -> tuple[str, list[str]]:
    """Split a global ``--config PATH`` out of ``args`` (env ``CVFLOW_CONFIG``, then default)."""
    path = os.environ.get("CVFLOW_CONFIG", "config.yaml")
    rest: list[str] = []
    it = iter(args)
    for a in it:
        if a == "--config":
            path = next(it, "")
            if not path:
                raise SystemExit("--config needs a path")
        elif a.startswith("--config="):
            path = a.split("=", 1)[1]
        else:
            rest.append(a)
    return path, rest


def _onboard(
    args: list[str],
    *,
    backend: Any,
    run_onboard_fn: Callable[..., Any],
    echo: Callable[[str], None],
) -> None:
    ap = argparse.ArgumentParser(prog="cvflow onboard")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--manual", action="store_true")
    ap.add_argument("--prompt-out")
    ap.add_argument("--reply-in")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--facts")
    ap.add_argument("--config", default="config.yaml")
    ns = ap.parse_args(args)
    facts: dict[str, str] = {}
    if ns.facts:
        loaded = yaml.safe_load(Path(ns.facts).read_text())  # JSON is valid YAML
        if not isinstance(loaded, dict):
            raise SystemExit("--facts must be a YAML/JSON mapping")
        facts = {str(k): str(v) for k, v in loaded.items()}
    from cvflow.onboarding.backends import auto_chain, manual_chain

    files: list[str | Path] = list(ns.files)
    if ns.manual and not ns.reply_in:
        from cvflow.onboarding.parse import ResumeParseError, parse_resumes
        from cvflow.onboarding.prompt import build_prompt

        try:
            prompt = build_prompt(parse_resumes(files).text, facts)
        except ResumeParseError as exc:
            raise SystemExit(f"Could not read your resume: {exc}") from exc
        if ns.prompt_out:
            Path(ns.prompt_out).write_text(prompt)
        else:
            echo(prompt)
        echo("Paste the model's JSON reply into a file and re-run with --reply-in that_file")
        return
    cfg = None
    if backend is None:  # pragma: no cover — real config path; tests inject backend
        from cvflow.app.main import _chat_client
        from cvflow.config import load_config

        cfg = load_config(ns.config)
        if ns.manual:
            backend = manual_chain(lambda _p: None, lambda: Path(ns.reply_in).read_text())
        else:
            fr = cfg.onboarding.frontier
            clients: Any = (
                _chat_client(cfg.llm.tailoring),
                _chat_client(cfg.llm.distillation),
                _chat_client(_frontier_provider(fr)) if fr is not None else None,
            )
            backend = auto_chain(cfg.onboarding, *clients)
    elif ns.manual:
        backend = manual_chain(lambda _p: None, lambda: Path(ns.reply_in).read_text())
    outcome = run_onboard_fn(
        files,
        backend=backend,
        repo_root=".",
        user_facts=facts,
        config_path=ns.config,
        force=ns.force,
        attempts=cfg.onboarding.max_compile_attempts if cfg else 3,
        notify=echo,
    )
    for chunk in outcome.report:
        echo(chunk)
    if outcome.pdf_path:
        echo(f"PDF: {outcome.pdf_path}")
    if outcome.flagged:
        raise SystemExit(2)


def _frontier_provider(fr: Any) -> Any:  # pragma: no cover — real config path
    from cvflow.config import ProviderConfig

    # Frontier config carries no rate limit; 30 RPM is a conservative client-side cap.
    return ProviderConfig(fr.provider, fr.api_key, fr.base_url, fr.model, 30)


def main(
    argv: list[str] | None = None,
    *,
    services: object | None = None,
    echo: Callable[[str], None] = print,
    onboard_backend: Any = None,
    run_onboard_fn: Callable[..., Any] = run_onboard,
) -> None:
    args = list(argv) if argv is not None else sys.argv[1:]
    if not args:
        raise SystemExit(_USAGE)
    config_path, args = _pop_config(args)
    if not args:
        raise SystemExit(_USAGE)
    if args[0] == "onboard":  # needs no profile/, so it must bypass build_services
        _onboard(
            args[1:] + ["--config", config_path],
            backend=onboard_backend,
            run_onboard_fn=run_onboard_fn,
            echo=echo,
        )
        return
    if services is None:  # pragma: no cover — real config path; tests inject services
        from cvflow.app.main import build_services
        from cvflow.config import load_config

        services = build_services(load_config(config_path), config_path=config_path)
    from cvflow.app.commands import Services

    assert isinstance(services, Services)  # noqa: S101 — narrow the injected type

    command = args[0]
    if command == "discover":
        outcome = services.discover()
        echo(outcome.digest)
        if outcome.expired:
            echo(f"(expired {outcome.expired} stale job(s) before the run)")
    elif command == "tailor":
        if len(args) < 2:
            raise SystemExit("usage: python -m cvflow.cli tailor <job_id>")
        result = services.tailor(args[1])
        echo(f"Tailored {result['role']} @ {result['company']}")
        echo(result["diff"])
        echo(f"PDF: {result['pdf_path']}")
    elif command == "heartbeat":
        echo(run_heartbeat(services.store))
    else:
        raise SystemExit(_USAGE)


if __name__ == "__main__":
    main()
