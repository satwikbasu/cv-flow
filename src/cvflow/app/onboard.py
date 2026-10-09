"""The ``/onboard`` chat flow: collect résumé files, ask for facts, run onboarding.

State is one plain dict in ``bot_data["onboard"]`` (single authorized user, so
no ConversationHandler). The blocking work lives in ``Services.onboard``
(an ``OnboardRunner``); this module only talks to Telegram objects passed in.
Facts the résumé can't supply are asked one at a time and never guessed: a
``skip`` leaves the fact out.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from cvflow.app.commands import Services, _authorized, _services

__all__ = ["on_onboard", "on_document", "onboard_text", "onboard_button"]

_KEY = "onboard"
_ALLOWED = {".pdf", ".docx", ".txt"}
_MAX_BYTES = 5 * 1024 * 1024
_FACTS = [
    ("yoe_have", "How many years of experience do you have?"),
    ("min_ctc_lpa", "What is your minimum acceptable salary (LPA)?"),
    ("top_ctc_lpa", "What is your top / target salary (LPA)?"),
    ("locations", "Which locations do you prefer (comma-separated)?"),
]
_ASK_FILES = (
    "📄 Send your résumé as a PDF, DOCX or TXT document (max 5 MB). "
    "Send several if you like, then type done. (cancel to abort)"
)


def _state(context: Any) -> dict[str, Any] | None:
    state: dict[str, Any] | None = context.bot_data.get(_KEY)
    return state


def _runner(services: Services) -> Any:
    if services.onboard is None:
        raise RuntimeError("onboarding runner not configured")
    return services.onboard


def _begin(context: Any, services: Services, *, force: bool) -> None:
    runner = _runner(services)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    context.bot_data[_KEY] = {
        "phase": "awaiting_files",
        "files": [],
        "facts": {},
        "pending": [k for k, _ in _FACTS],
        "manual": runner.manual,
        "force": force,
        "upload_dir": str(Path(runner.staging_dir) / f"uploads-{stamp}"),
    }


async def on_onboard(update: Any, context: Any) -> None:
    services = _services(context)
    if not _authorized(update, services):
        return
    message = update.effective_message
    if services.onboard is None:
        await message.reply_text("⚠️ Onboarding isn't available (no config loaded).")
        return
    if services.onboard_lock.locked():
        await message.reply_text("⏳ An onboarding run is already in progress.")
        return
    profile = Path(services.onboard.profile_dir)
    if profile.is_dir() and any(profile.iterdir()):
        context.bot_data[_KEY] = {"phase": "confirm_replace"}
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Replace", callback_data="onboard:replace"),
                    InlineKeyboardButton("Cancel", callback_data="onboard:cancel"),
                ]
            ]
        )
        await message.reply_text(
            "A profile already exists. Onboarding again will replace it.", reply_markup=keyboard
        )
        return
    _begin(context, services, force=False)
    await message.reply_text(_ASK_FILES)


async def onboard_button(verb: str, message: Any, context: Any) -> None:
    services = _services(context)
    if verb == "cancel":
        context.bot_data.pop(_KEY, None)
        await message.reply_text("Onboarding cancelled.")
    elif verb == "replace":
        state = _state(context)
        if services.onboard is None or state is None or state.get("phase") != "confirm_replace":
            await message.reply_text("⚠️ Nothing to replace — send /onboard again.")
            return
        _begin(context, services, force=True)
        await message.reply_text(_ASK_FILES)


async def on_document(update: Any, context: Any) -> None:
    services = _services(context)
    if not _authorized(update, services):
        return
    message = update.effective_message
    state = _state(context)
    phase = state["phase"] if state else None
    if phase not in ("awaiting_files", "awaiting_reply_doc"):
        await message.reply_text("📎 I only take documents during /onboard — send /onboard first.")
        return
    doc = message.effective_attachment
    name = Path(getattr(doc, "file_name", None) or "upload").name
    if Path(name).suffix.lower() not in _ALLOWED:
        await message.reply_text("⚠️ Only PDF, DOCX or TXT files are accepted.")
        return
    size = getattr(doc, "file_size", None)
    if size is not None and size > _MAX_BYTES:
        await message.reply_text("⚠️ That file is over 5 MB — send a smaller one.")
        return
    if state is None:  # unreachable: phase check above implies state
        return
    dest = Path(state["upload_dir"]) / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    tg_file = await doc.get_file()
    await tg_file.download_to_drive(dest)
    if phase == "awaiting_reply_doc":
        await _run(message, services, context, reply_text=dest.read_text(errors="replace"))
        return
    state["files"].append(str(dest))
    await message.reply_text(f"✅ Got {name}. Send more, or type done.")


async def _ask_next(message: Any, services: Services, context: Any, state: dict[str, Any]) -> None:
    if state["pending"]:
        question = dict(_FACTS)[state["pending"][0]]
        await message.reply_text(f"{question} (or type skip)")
        return
    runner = _runner(services)
    if not state["manual"]:
        await _run(message, services, context)
        return
    loop = asyncio.get_running_loop()
    try:
        prompt = await loop.run_in_executor(
            None, runner.build_prompt, [Path(f) for f in state["files"]], state["facts"]
        )
    except Exception as exc:  # noqa: BLE001 — must reach the user
        context.bot_data.pop(_KEY, None)
        await message.reply_text(f"⚠️ Could not build the prompt: {exc}")
        return
    path = Path(state["upload_dir"]) / "prompt.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prompt)
    state["phase"] = "awaiting_reply_doc"
    with path.open("rb") as fh:
        await message.reply_document(document=fh, filename=path.name)
    await message.reply_text(
        "Give that prompt to your model, then send its JSON reply back as a .txt document."
    )


async def onboard_text(message: Any, context: Any) -> None:
    """Free text while a flow is active (or the /onboard command itself)."""
    services = _services(context)
    state = _state(context)
    if state is None:
        return
    text = message.text.strip()
    phase = state["phase"]
    if text.lower() == "cancel":
        context.bot_data.pop(_KEY, None)
        await message.reply_text("Onboarding cancelled.")
    elif phase == "awaiting_files":
        if text.lower() != "done":
            await message.reply_text("Send your résumé as a document, then type done.")
        elif not state["files"]:
            await message.reply_text("⚠️ No résumé received yet — send a document first.")
        else:
            state["phase"] = "awaiting_facts"
            await _ask_next(message, services, context, state)
    elif phase == "awaiting_facts":
        key = state["pending"].pop(0)
        if text.lower() != "skip":
            state["facts"][key] = text
        await _ask_next(message, services, context, state)
    elif phase == "awaiting_reply_doc":
        await _run(message, services, context, reply_text=text)
    else:
        await message.reply_text("Tap Replace or Cancel above, or send /onboard again.")


async def _run(
    message: Any, services: Services, context: Any, *, reply_text: str | None = None
) -> None:
    runner = services.onboard
    state = context.bot_data.pop(_KEY, None)
    if runner is None or state is None:
        return
    if services.onboard_lock.locked():
        await message.reply_text("⏳ An onboarding run is already in progress.")
        return
    files = [Path(f) for f in state["files"]]
    async with services.onboard_lock:
        await message.reply_text("🧩 Onboarding started — this can take a few minutes.")
        loop = asyncio.get_running_loop()
        try:
            if reply_text is None:
                outcome = await loop.run_in_executor(
                    None, runner.run_auto, files, state["facts"], state["force"]
                )
            else:
                outcome = await loop.run_in_executor(
                    None, runner.run_manual, files, state["facts"], state["force"], reply_text
                )
        except Exception as exc:  # noqa: BLE001 — a failed run must reach the user
            await message.reply_text(f"⚠️ Onboarding failed: {exc}")
            return
    for chunk in outcome.report:
        await message.reply_text(chunk)
    if outcome.pdf_path is not None:
        with outcome.pdf_path.open("rb") as fh:
            await message.reply_document(document=fh, filename=outcome.pdf_path.name)
