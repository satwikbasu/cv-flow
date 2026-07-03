"""Tests for cvflow.digest_summary."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cvflow.digest_summary import compress_drops, summarize_drops, write_discover_log
from cvflow.discovery import DROP_BUCKET_ORDER, DropRecord
from cvflow.llm import LLMError, RpmExceeded

# ---------------------------------------------------------------------------
# compress_drops
# ---------------------------------------------------------------------------


def _rec(bucket: str, label: str = "Job @ Co", detail: str = "reason") -> DropRecord:
    return DropRecord(bucket, label, detail)


def test_compress_drops_empty_returns_empty_string() -> None:
    assert compress_drops([], samples_per_bucket=3) == ""


def test_compress_drops_order_matches_drop_bucket_order() -> None:
    # Supply buckets in reverse canonical order.
    records = [
        _rec("low pay", "Low Pay Job @ Acme"),
        _rec("too senior", "Senior Job @ Corp"),
        _rec("abroad", "Abroad Job @ Intl"),
    ]
    output = compress_drops(records, samples_per_bucket=3)
    lines = output.splitlines()
    buckets_in_output = [line.split(":")[0].lstrip("- ") for line in lines]
    # All three should appear in DROP_BUCKET_ORDER order.
    positions = [DROP_BUCKET_ORDER.index(b) for b in buckets_in_output]
    assert positions == sorted(positions)


def test_compress_drops_counts_correct() -> None:
    records = [
        _rec("low pay", "Job A @ X"),
        _rec("low pay", "Job B @ Y"),
        _rec("abroad", "Job C @ Z"),
    ]
    output = compress_drops(records, samples_per_bucket=5)
    # "low pay" should show count 2, "abroad" count 1.
    assert "low pay: 2" in output
    assert "abroad: 1" in output


def test_compress_drops_examples_capped_at_samples_per_bucket() -> None:
    records = [_rec("wrong stack", f"Job {i} @ Co") for i in range(10)]
    output = compress_drops(records, samples_per_bucket=3)
    # Extract the line for "wrong stack".
    line = next(ln for ln in output.splitlines() if "wrong stack" in ln)
    # Should have exactly 3 quoted examples.
    assert line.count('"') == 6  # 3 pairs of quotes


def test_compress_drops_capped_bucket_handled_normally() -> None:
    # The "capped" bucket uses label "(summary)" — ensure it appears and is treated like any bucket.
    records = [
        DropRecord("capped", "(summary)", "too many results; capped"),
        DropRecord("capped", "(summary)", "too many results; capped"),
    ]
    output = compress_drops(records, samples_per_bucket=2)
    assert "capped: 2" in output
    assert '(summary)' in output


def test_compress_drops_unknown_bucket_comes_last() -> None:
    records = [
        _rec("too senior", "Senior Job @ Corp"),
        _rec("custom-unknown-bucket", "Odd Job @ Weird"),
    ]
    output = compress_drops(records, samples_per_bucket=2)
    lines = output.splitlines()
    bucket_names = [ln.split(":")[0].lstrip("- ") for ln in lines]
    assert bucket_names.index("too senior") < bucket_names.index("custom-unknown-bucket")


# ---------------------------------------------------------------------------
# write_discover_log
# ---------------------------------------------------------------------------


def _fixed_now() -> datetime:
    return datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


def test_write_discover_log_creates_file(tmp_path: Path) -> None:
    log_dir = str(tmp_path / "logs")
    path = write_discover_log("digest here", ["chunk1", "chunk2"], log_dir, now=_fixed_now)
    assert path.exists()
    assert path.suffix == ".md"


def test_write_discover_log_returned_path_exists(tmp_path: Path) -> None:
    path = write_discover_log("digest", [], str(tmp_path), now=_fixed_now)
    assert path.exists()


def test_write_discover_log_content_contains_digest_and_chunks(tmp_path: Path) -> None:
    digest = "## Digest\nFound 5 jobs."
    chunks = ["Drop chunk A", "Drop chunk B"]
    path = write_discover_log(digest, chunks, str(tmp_path), now=_fixed_now)
    content = path.read_text(encoding="utf-8")
    assert digest in content
    for chunk in chunks:
        assert chunk in content


def test_write_discover_log_empty_chunks_has_only_digest(tmp_path: Path) -> None:
    digest = "Only the digest."
    path = write_discover_log(digest, [], str(tmp_path), now=_fixed_now)
    content = path.read_text(encoding="utf-8")
    assert content == digest


def test_write_discover_log_creates_missing_dir(tmp_path: Path) -> None:
    log_dir = str(tmp_path / "deep" / "nested" / "dir")
    path = write_discover_log("d", [], log_dir, now=_fixed_now)
    assert path.exists()


def test_write_discover_log_filename_has_no_colons(tmp_path: Path) -> None:
    path = write_discover_log("d", [], str(tmp_path), now=_fixed_now)
    assert ":" not in path.name


# ---------------------------------------------------------------------------
# summarize_drops
# ---------------------------------------------------------------------------


class _FakeProvider:
    """Provider that records call count and returns a fixed response."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.call_count = 0

    def generate(self, prompt: str, **kwargs: object) -> str:
        self.call_count += 1
        return self._response


class _RaisingProvider:
    """Provider that raises a given exception."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.call_count = 0

    def generate(self, prompt: str, **kwargs: object) -> str:
        self.call_count += 1
        raise self._exc


def test_summarize_drops_empty_records_returns_none_no_call() -> None:
    provider = _FakeProvider("should not be called")
    result = summarize_drops([], provider=provider, max_chars=200, samples_per_bucket=3)
    assert result is None
    assert provider.call_count == 0


def test_summarize_drops_success_truncated_to_max_chars() -> None:
    records = [_rec("low pay", "Job A @ Acme")]
    long_response = "x" * 1000
    provider = _FakeProvider(long_response)
    result = summarize_drops(records, provider=provider, max_chars=100, samples_per_bucket=3)
    assert result is not None
    assert len(result) <= 100


def test_summarize_drops_success_returns_non_none() -> None:
    records = [_rec("too senior", "Snr Dev @ Corp"), _rec("abroad", "Dev @ UK")]
    provider = _FakeProvider("A few jobs were filtered due to seniority and location.")
    result = summarize_drops(records, provider=provider, max_chars=700, samples_per_bucket=3)
    assert result is not None
    assert isinstance(result, str)


def test_summarize_drops_rpm_exceeded_returns_none() -> None:
    records = [_rec("low pay", "Job @ Co")]
    provider = _RaisingProvider(RpmExceeded("rate limit"))
    result = summarize_drops(records, provider=provider, max_chars=700, samples_per_bucket=3)
    assert result is None


def test_summarize_drops_llm_error_returns_none() -> None:
    records = [_rec("wrong stack", "Job @ Co")]
    provider = _RaisingProvider(LLMError("some error"))
    result = summarize_drops(records, provider=provider, max_chars=700, samples_per_bucket=3)
    assert result is None


def test_summarize_drops_strips_whitespace_and_truncates() -> None:
    records = [_rec("abroad", "Dev @ US")]
    padded = "  " + "a" * 500 + "  "
    provider = _FakeProvider(padded)
    result = summarize_drops(records, provider=provider, max_chars=200, samples_per_bucket=2)
    assert result is not None
    assert len(result) <= 200
    assert not result.startswith(" ")
