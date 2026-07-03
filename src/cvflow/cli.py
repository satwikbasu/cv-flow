"""Headless entry points: ``python -m cvflow.cli discover|tailor <job_id>|heartbeat``.

Reuses the exact run bodies the bot uses, so a --no-services install (or a CI
smoke run) exercises the same code path — just with stdout instead of chat.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from cvflow.runs import run_heartbeat

_USAGE = "usage: python -m cvflow.cli <discover|tailor <job_id>|heartbeat>"


def main(
    argv: list[str] | None = None,
    *,
    services: object | None = None,
    echo: Callable[[str], None] = print,
) -> None:
    args = list(argv) if argv is not None else sys.argv[1:]
    if not args:
        raise SystemExit(_USAGE)
    if services is None:  # pragma: no cover — real config path; tests inject services
        from cvflow.app.main import build_services
        from cvflow.config import load_config

        services = build_services(load_config("config.yaml"))
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
