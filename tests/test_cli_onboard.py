from pathlib import Path
from typing import Any

import pytest

from cvflow.cli import main
from cvflow.runs import OnboardOutcome


def _run(outcome: OnboardOutcome, calls: list[Any]) -> Any:
    def fn(files: list[Any], **kw: Any) -> OnboardOutcome:
        calls.append((files, kw))
        return outcome

    return fn


def test_onboard_prints_report(tmp_path: Path) -> None:
    calls: list[Any] = []
    out: list[str] = []
    main(
        ["onboard", "resume.pdf"], echo=out.append, onboard_backend="B",
        run_onboard_fn=_run(OnboardOutcome(["done"], Path("x.pdf"), False, True), calls),
    )
    assert calls[0][0] == ["resume.pdf"] and calls[0][1]["backend"] == "B"
    assert "done" in out and "PDF: x.pdf" in out


def test_onboard_flagged_exits_2() -> None:
    with pytest.raises(SystemExit) as e:
        main(
            ["onboard", "r.pdf"], echo=lambda _m: None, onboard_backend="B",
            run_onboard_fn=_run(OnboardOutcome(["bad"], None, True, False), []),
        )
    assert e.value.code == 2


def test_manual_without_reply_writes_prompt(tmp_path: Path) -> None:
    cv = tmp_path / "cv.txt"
    cv.write_text("Test Person resume")
    prompt = tmp_path / "p.txt"
    calls: list[Any] = []
    out: list[str] = []
    main(
        ["onboard", str(cv), "--manual", "--prompt-out", str(prompt)], echo=out.append,
        run_onboard_fn=_run(OnboardOutcome([], None, False, True), calls),
    )
    assert "Test Person resume" in prompt.read_text()
    assert not calls and "--reply-in" in out[-1]


def test_manual_with_reply_builds_manual_backend(tmp_path: Path) -> None:
    reply = tmp_path / "reply.json"
    reply.write_text("{}")
    calls: list[Any] = []
    main(
        ["onboard", "r.pdf", "--manual", "--reply-in", str(reply), "--force"],
        echo=lambda _m: None, onboard_backend=object(),
        run_onboard_fn=_run(OnboardOutcome([], None, False, True), calls),
    )
    kw = calls[0][1]
    assert type(kw["backend"]).__name__ == "BackendChain" and kw["force"] is True


def _spy_config(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    import cvflow.app.main as app_main
    import cvflow.config as config_mod
    from cvflow.app.commands import Services
    from cvflow.storage import ApplicationStore

    seen: list[Any] = []

    def fake_load(path: Any) -> str:
        seen.append(("load", str(path)))
        return "CFG"

    def fake_build(cfg: Any, config_path: str = "config.yaml") -> Services:
        seen.append(("build", config_path))
        return Services(
            store=ApplicationStore(":memory:"),
            notify=lambda _m: None,
            discover=lambda: None,  # type: ignore[arg-type, return-value]
            tailor=lambda _j: {},
            authorized_user_id=1,
        )

    monkeypatch.setattr(config_mod, "load_config", fake_load)
    monkeypatch.setattr(app_main, "build_services", fake_build)
    return seen


def test_cli_config_flag_selects_path(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _spy_config(monkeypatch)
    main(["--config", "alt/x.yaml", "heartbeat"], echo=lambda _m: None)
    assert seen == [("load", "alt/x.yaml"), ("build", "alt/x.yaml")]


def test_cli_config_env_then_default(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _spy_config(monkeypatch)
    monkeypatch.setenv("CVFLOW_CONFIG", "alt/env.yaml")
    main(["heartbeat"], echo=lambda _m: None)
    monkeypatch.delenv("CVFLOW_CONFIG")
    main(["heartbeat"], echo=lambda _m: None)
    assert [s[1] for s in seen if s[0] == "load"] == ["alt/env.yaml", "config.yaml"]
