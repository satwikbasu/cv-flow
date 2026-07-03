"""digest_summary — log writer, drop compressor, and LLM summarizer for discover output."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cvflow.discovery import DROP_BUCKET_ORDER, DropRecord
from cvflow.llm import LLMError

logger = logging.getLogger("cvflow.digest_summary")


def write_discover_log(
    digest_text: str,
    drop_chunks: list[str],
    log_dir: str,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Path:
    """Write the full digest + drop report to <log_dir>/<UTC-ISO-timestamp>.md.

    Creates log_dir if missing. Filename uses isoformat() with ':' → '-'.
    Body = digest_text, then a blank line, then each drop_chunk separated by blank lines.
    Returns the written path.
    """
    ts = now().isoformat().replace(":", "-")
    out_dir = Path(log_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{ts}.md"

    parts = [digest_text]
    if drop_chunks:
        parts.extend(drop_chunks)

    path.write_text("\n\n".join(parts), encoding="utf-8")
    return path


def compress_drops(records: list[DropRecord], samples_per_bucket: int) -> str:
    """Return a constant-size compact block suitable for an LLM prompt.

    Groups records by bucket and emits one line per non-empty bucket in DROP_BUCKET_ORDER:
        '- <bucket>: <count> — e.g. "<label1>", "<label2>", ...'
    Buckets not in DROP_BUCKET_ORDER come last in first-seen order.
    Returns '' when records is empty.
    """
    if not records:
        return ""

    # Gather counts and ordered samples per bucket.
    order: list[str] = []
    counts: dict[str, int] = {}
    samples: dict[str, list[str]] = {}

    for rec in records:
        b = rec.bucket
        if b not in counts:
            order.append(b)
            counts[b] = 0
            samples[b] = []
        counts[b] += 1
        if len(samples[b]) < samples_per_bucket:
            samples[b].append(rec.label)

    # Sort buckets: canonical order first, then first-seen for unknowns.
    known = [b for b in DROP_BUCKET_ORDER if b in counts]
    unknown = [b for b in order if b not in set(DROP_BUCKET_ORDER)]
    lines: list[str] = []
    for b in known + unknown:
        eg = ", ".join(f'"{s}"' for s in samples[b])
        suffix = f" — e.g. {eg}" if eg else ""
        lines.append(f"- {b}: {counts[b]}{suffix}")

    return "\n".join(lines)


def summarize_drops(
    records: list[DropRecord],
    *,
    provider: Any,
    max_chars: int,
    samples_per_bucket: int,
) -> str | None:
    """Call provider.generate() once over a compressed drop block.

    Returns a friendly <=max_chars paragraph or None when:
    - records is empty, OR
    - the provider raises LLMError (including RpmExceeded).
    Never raises.
    """
    if not records:
        return None

    block = compress_drops(records, samples_per_bucket)
    max_tokens = max_chars // 3 + 80

    prompt = (
        "You are writing a brief, warm summary for a job-seeker about which jobs were filtered "
        "out during today's discovery run and why.\n\n"
        "Here is a compact breakdown of the dropped jobs by filter category:\n"
        f"{block}\n\n"
        f"Write a single paragraph of at most {max_chars} characters summarising the above in "
        "plain, friendly language. Group jobs by reason. Name the biggest buckets and include a "
        "couple of example job titles. If there is no 'low pay' bucket you may note that nothing "
        "was dropped on salary grounds. Do NOT invent jobs or counts — use only what is shown "
        "above. No markdown, no lists, just prose."
    )

    try:
        raw: str = provider.generate(prompt, temperature=0.3, max_tokens=max_tokens)
    except LLMError as exc:
        # The caller falls back to the deterministic footer — but log WHY so a persistently
        # failing summary provider is diagnosable.
        logger.warning("drop summary failed (%s); falling back to footer", exc)
        return None

    summary = raw.strip()[:max_chars]
    if not summary:
        logger.warning("drop summary provider returned empty text; falling back to footer")
        return None
    return summary
