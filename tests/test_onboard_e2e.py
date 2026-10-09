"""/onboard chat flow driven through the real handlers with a fake runner (no LLM, no compile)."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from cvflow.app.buttons import on_button
from cvflow.app.commands import _HINT, Services, on_text
from cvflow.app.onboard import on_document
from cvflow.runs import OnboardOutcome
from cvflow.storage import ApplicationStore

AUTH_ID = 4242


class _FakeRunner:
    def __init__(self, tmp: Path, pdf: Path, *, profile_exists: bool = False) -> None:
        self.manual = False
        self.profile_dir = str(tmp / "profile")
        self.staging_dir = str(tmp / "staging")
        if profile_exists:
            Path(self.profile_dir).mkdir()
            (Path(self.profile_dir) / "about.md").write_text("x")
        self.pdf = pdf
        self.calls: list[tuple[list[Path], dict[str, str], bool]] = []

    def run_auto(self, files: list[Path], facts: dict[str, str], force: bool) -> OnboardOutcome:
        self.calls.append((files, facts, force))
        return OnboardOutcome(["ok"], self.pdf, False, True)

    def run_manual(self, *a: Any) -> OnboardOutcome:  # pragma: no cover
        raise AssertionError

    def build_prompt(self, files: list[Path], facts: dict[str, str]) -> str:  # pragma: no cover
        return "prompt"


def _services(runner: _FakeRunner) -> Services:
    return Services(
        store=ApplicationStore(":memory:"),
        notify=lambda _m: None,
        discover=lambda: None,  # type: ignore[arg-type, return-value]
        tailor=lambda _j: {},
        authorized_user_id=AUTH_ID,
        onboard=runner,  # type: ignore[arg-type]
    )


def _msg(text: str | None = None, document: Any = None) -> SimpleNamespace:
    message = SimpleNamespace(
        text=text,
        reply_text=AsyncMock(),
        reply_document=AsyncMock(),
        effective_attachment=document,
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=AUTH_ID),
        effective_message=message,
        callback_query=None,
    )


def _doc(name: str, content: bytes = b"resume text") -> SimpleNamespace:
    async def download_to_drive(dest: Path) -> None:
        Path(dest).write_bytes(content)

    tg_file = SimpleNamespace(download_to_drive=download_to_drive)
    return SimpleNamespace(
        file_name=name, file_size=len(content), get_file=AsyncMock(return_value=tg_file)
    )


def _replies(update: SimpleNamespace) -> str:
    return "\n".join(str(c.args[0]) for c in update.effective_message.reply_text.await_args_list)


async def _tap(data: str, ctx: Any) -> None:
    message = SimpleNamespace(reply_text=AsyncMock(), reply_document=AsyncMock())
    query = SimpleNamespace(data=data, answer=AsyncMock(), message=message)
    user = SimpleNamespace(id=AUTH_ID)
    await on_button(
        SimpleNamespace(effective_user=user, effective_message=message, callback_query=query), ctx
    )


async def test_happy_path(tmp_path: Path) -> None:
    pdf = tmp_path / "onboarding.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    runner = _FakeRunner(tmp_path, pdf)
    ctx = SimpleNamespace(bot_data={"services": _services(runner)})

    await on_text(_msg("/onboard"), ctx)
    assert ctx.bot_data["onboard"]["phase"] == "awaiting_files"
    up = _msg(document=_doc("cv.pdf"))
    await on_document(up, ctx)
    assert "Got cv.pdf" in _replies(up)
    await on_text(_msg("done"), ctx)
    for answer in ["5", "12", "skip", "Pune"]:
        await on_text(_msg(answer), ctx)
    assert "onboard" not in ctx.bot_data
    files, facts, force = runner.calls[0]
    assert [f.name for f in files] == ["cv.pdf"] and files[0].read_bytes() == b"resume text"
    assert facts == {"yoe_have": "5", "min_ctc_lpa": "12", "locations": "Pune"}
    assert force is False


async def test_happy_path_replies_and_pdf(tmp_path: Path) -> None:
    pdf = tmp_path / "onboarding.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    runner = _FakeRunner(tmp_path, pdf)
    ctx = SimpleNamespace(bot_data={"services": _services(runner)})
    await on_text(_msg("/onboard"), ctx)
    await on_document(_msg(document=_doc("cv.txt")), ctx)
    await on_text(_msg("done"), ctx)
    last = None
    for answer in ["1", "2", "3", "skip"]:
        last = _msg(answer)
        await on_text(last, ctx)
    assert last is not None
    assert "ok" in _replies(last)
    last.effective_message.reply_document.assert_awaited_once()
    assert last.effective_message.reply_document.await_args.kwargs["filename"] == "onboarding.pdf"


async def test_existing_profile_asks_replace_and_cancel_clears(tmp_path: Path) -> None:
    runner = _FakeRunner(tmp_path, tmp_path / "x.pdf", profile_exists=True)
    ctx = SimpleNamespace(bot_data={"services": _services(runner)})
    up = _msg("/onboard")
    await on_text(up, ctx)
    markup = up.effective_message.reply_text.await_args.kwargs["reply_markup"]
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert data == ["onboard:replace", "onboard:cancel"]
    assert ctx.bot_data["onboard"]["phase"] == "confirm_replace"

    await _tap("onboard:cancel", ctx)
    assert "onboard" not in ctx.bot_data


async def test_replace_starts_flow_with_force(tmp_path: Path) -> None:
    runner = _FakeRunner(tmp_path, tmp_path / "x.pdf", profile_exists=True)
    ctx = SimpleNamespace(bot_data={"services": _services(runner)})
    await on_text(_msg("/onboard"), ctx)
    await _tap("onboard:replace", ctx)
    assert ctx.bot_data["onboard"]["phase"] == "awaiting_files"
    assert ctx.bot_data["onboard"]["force"] is True


async def test_non_text_without_flow_gets_hint(tmp_path: Path) -> None:
    ctx = SimpleNamespace(bot_data={"services": _services(_FakeRunner(tmp_path, tmp_path / "x"))})
    up = _msg(None)
    await on_text(up, ctx)
    up.effective_message.reply_text.assert_awaited_once_with(_HINT)


async def test_wrong_document_type_rejected(tmp_path: Path) -> None:
    ctx = SimpleNamespace(bot_data={"services": _services(_FakeRunner(tmp_path, tmp_path / "x"))})
    await on_text(_msg("/onboard"), ctx)
    up = _msg(document=_doc("cv.exe"))
    await on_document(up, ctx)
    assert "PDF, DOCX or TXT" in _replies(up)
    assert ctx.bot_data["onboard"]["files"] == []
