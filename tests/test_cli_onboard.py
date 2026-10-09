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
