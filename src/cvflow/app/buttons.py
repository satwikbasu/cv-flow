"""Inline-button callbacks: ``tailor:<job_id>`` and ``skip:<job_id>``.

Buttons carry the job_id (not the ordinal), so they keep working after the
slot map is renumbered by a later digest or /jobs listing.
"""

from __future__ import annotations

from typing import Any

from cvflow.app.commands import _authorized, _do_tailor, _label, _services
from cvflow.statemachine import IllegalTransition, Status

__all__ = ["on_button"]


async def on_button(update: Any, context: Any) -> None:
    services = _services(context)
    query = update.callback_query
    if query is None or not _authorized(update, services):
        return
    await query.answer()
    data = query.data or ""
    if data.startswith("onboard:"):
        if update.effective_message is not None:
            from cvflow.app.onboard import onboard_button

            await onboard_button(data.partition(":")[2], update.effective_message, context)
        return
    verb, _, job_id = data.partition(":")
    if not job_id:
        return
    message = update.effective_message
    if message is None:
        return
    if verb == "tailor":
        await _do_tailor(job_id, message, services)
    elif verb == "skip":
        label = _label(services, job_id)
        try:
            services.store.set_status(job_id, Status.SKIPPED)
        except IllegalTransition:
            await message.reply_text(f"⚠️ {label} can't be skipped any more.")
        else:
            await message.reply_text(f"🙈 Skipped {label}")
