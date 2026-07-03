"""End-to-end bot flow with the network mocked out.

Real store, real run bodies, real renderers, real ResumeTailor + tectonic —
only the job scrape and the LLM replies are faked. This is the W-milestone
check: digest with buttons, tailor to a PDF + diff, skip, backlog filters,
expiry, heartbeat.
"""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cvflow.app.buttons import on_button
from cvflow.app.commands import Services, on_text
from cvflow.resume import ResumeTailor, parse_master
from cvflow.runs import run_discover, run_heartbeat, run_tailor
from cvflow.statemachine import Status
from cvflow.storage import ApplicationStore

FIXTURE_RESUME = Path(__file__).parent / "fixtures" / "resume"
AUTH_ID = 4242
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


class _Posting:
    def __init__(self, job_id: str, title: str) -> None:
        self.job_id = job_id
        self.title = title
        self.company = "Acme"
        self.location = "Pune"
        self.url = f"https://example.test/{job_id}"
        self.date_posted = "2026-07-01"


class _BJ:
    def __init__(self, job_id: str, title: str, cohort: str) -> None:
        self.posting = _Posting(job_id, title)
        self.benchmark = 80 if cohort == "M" else 60
        self.fit_score = 8
        self.fit_reason = "Stack matches"
        self.concerns: list[str] = []
        self.cohort = cohort
        self.ctc_lpa = 12.0 if cohort == "M" else None


class _FakeDiscovery:
    """Mimics the real pipeline's persistence side effects, no network."""

    def __init__(self, store: ApplicationStore) -> None:
        self._store = store

    def discover(self, progress: Any = None) -> dict[str, Any]:
        jobs = [_BJ("m:1", "Backend Dev", "M"), _BJ("n:1", "Platform Eng", "N")]
        crux = json.dumps(
            {
                "must_have_skills": ["Java", "Spring Boot"],
                "tech_stack": ["Java", "Spring Boot", "Postgres"],
                "seniority_signal": "junior",
                "applicant_instructions": [],
            }
        )
        for bj in jobs:
            p = bj.posting
            self._store.add(
                p.job_id, p.company, p.title, p.url,
                date_posted=p.date_posted, date_posted_parsed=p.date_posted,
            )
            self._store.set_discovery_meta(
                p.job_id, benchmark=bj.benchmark, fit_score=bj.fit_score,
                fit_reason=bj.fit_reason, concerns=bj.concerns, cohort=bj.cohort,
                ctc_lpa=bj.ctc_lpa,
            )
            self._store.save_crux(p.job_id, crux)
        return {
            "M": [jobs[0]], "N": [jobs[1]],
            "_dropped": {"low pay": 2}, "_drop_records": [],
            "_notices": ["Naukri unreachable today"],
        }


class _PlanProvider:
    def generate(self, prompt: str) -> str:
        return json.dumps(
            {
                "section_order": ["skills", "experience", "projects"],
                "selected_project_ids": ["taskboard"],
                "diff_narration": "Led with skills.",
            }
        )


def _update(text: str = "") -> SimpleNamespace:
    message = SimpleNamespace(text=text, reply_text=AsyncMock(), reply_document=AsyncMock())
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=AUTH_ID),
        effective_message=message,
        message=message,
        callback_query=None,
    )


def _button_update(data: str) -> SimpleNamespace:
    message = SimpleNamespace(reply_text=AsyncMock(), reply_document=AsyncMock())
    query = SimpleNamespace(data=data, answer=AsyncMock(), message=message)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=AUTH_ID),
        effective_message=message,
        callback_query=query,
    )


def _replies(update: SimpleNamespace) -> str:
    return "\n".join(
        str(c.args[0]) for c in update.effective_message.reply_text.await_args_list
    )


@pytest.mark.skipif(shutil.which("tectonic") is None, reason="tectonic not installed")
async def test_full_flow_discover_tailor_skip_jobs_expiry_heartbeat(tmp_path: Path) -> None:
    store = ApplicationStore(":memory:")
    chat: list[str] = []  # what the notifier delivers (digest, drop report)

    # a stale job from an earlier run: the discover sweep must expire it
    store.add("stale:1", "OldCo", "Old Role", "https://example.test/stale")
    store._conn.execute(
        "UPDATE applications SET discovered_at = '2026-05-01T00:00:00+00:00'"
    )
    store._conn.commit()

    tailor_engine = ResumeTailor(_PlanProvider(), parse_master(FIXTURE_RESUME))
    services = Services(
        store=store,
        notify=chat.append,
        discover=lambda: run_discover(
            store=store, discovery=_FakeDiscovery(store), notify=chat.append,
            retention_days=21, now=lambda: NOW, report_drops=True,
        ),
        tailor=lambda job_id: run_tailor(
            job_id, store=store, tailor=tailor_engine, output_dir=str(tmp_path),
            use_jd_analysis=False, analyzer=None,
        ),
        authorized_user_id=AUTH_ID,
    )
    context = SimpleNamespace(bot_data={"services": services})

    # 1. /discover — digest lands in chat, quick-action buttons follow
    update = _update("/discover")
    await on_text(update, context)
    digest = chat[0]
    assert "1. Backend Dev @ Acme" in digest and "2. Platform Eng @ Acme" in digest
    assert "posted 2d ago" in digest  # posting date rendered
    assert "Naukri unreachable today" in digest  # source notice surfaced
    assert "2 low pay" in chat[1]  # drop report as its own message
    markup = update.effective_message.reply_text.await_args_list[-1].kwargs["reply_markup"]
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "tailor:m:1" in callbacks and "skip:n:1" in callbacks
    assert store.get("stale:1").status is Status.EXPIRED  # type: ignore[union-attr]

    # 2. [Tailor] button on job 1 — compiles a real PDF, posts the honest diff
    tap = _button_update("tailor:m:1")
    await on_button(tap, context)
    assert store.get("m:1").status is Status.TAILORED  # type: ignore[union-attr]
    text = _replies(tap)
    assert "✅ Tailored — Backend Dev @ Acme" in text
    assert "No bullets reworded" in text  # rephrase off -> verbatim promise
    sent_doc = tap.effective_message.reply_document.await_args.kwargs
    assert sent_doc["filename"].endswith(".pdf")
    assert (tmp_path / sent_doc["filename"]).stat().st_size > 0

    # 3. /skip 2 — the other job leaves the open list
    update = _update("skip 2")
    await on_text(update, context)
    assert store.get("n:1").status is Status.SKIPPED  # type: ignore[union-attr]

    # 4. /jobs filters — open is empty now; all shows the tailored one marked
    update = _update("/jobs")
    await on_text(update, context)
    assert "/discover" in _replies(update)  # friendly empty backlog
    update = _update("/jobs all")
    await on_text(update, context)
    listing = _replies(update)
    assert "Backend Dev @ Acme" in listing and "tailored" in listing.lower()

    # 5. /status + heartbeat — expiry and the rest visible in the counts
    update = _update("/status")
    await on_text(update, context)
    counts = _replies(update)
    assert "tailored=1" in counts and "skipped=1" in counts and "expired=1" in counts
    beat = run_heartbeat(store)
    assert "alive" in beat and "tailored=1" in beat
