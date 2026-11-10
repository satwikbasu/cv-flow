"""Tests for the OpenAI-compatible chat client. No live key, no network."""

import json

import pytest

from cvflow.llm import ChatClient, LLMError, LLMHTTPError


def _fake_post(captured):
    def post(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json.loads(body)
        return json.dumps({"choices": [{"message": {"content": "ranked!"}}]})

    return post


def test_generate_posts_chat_completions_and_returns_content():
    captured = {}
    client = ChatClient(
        base_url="https://api.example.com/v1",
        api_key="secret",
        model="some-model",
        max_requests_per_minute=40,
        post_fn=_fake_post(captured),
    )
    out = client.generate("rank these")
    assert out == "ranked!"
    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["body"]["model"] == "some-model"
    assert captured["body"]["messages"] == [{"role": "user", "content": "rank these"}]


def test_generate_structured_strictifies_schema_for_strict_hosts():
    from pydantic import BaseModel

    class Inner(BaseModel):
        amount: int | None = None

    class Outer(BaseModel):
        name: str
        inner: Inner

    captured = {}
    client = ChatClient(
        base_url="https://api.groq.com/openai/v1",
        api_key="k",
        model="m",
        max_requests_per_minute=40,
        post_fn=_fake_post(captured),
    )
    client.generate_structured("go", schema=Outer, max_output_tokens=64)
    sent = captured["body"]["response_format"]["json_schema"]["schema"]
    # Groq's strict mode needs every object to forbid extras and require all its properties.
    assert sent["additionalProperties"] is False
    assert set(sent["required"]) == set(sent["properties"])
    inner = sent["$defs"]["Inner"]
    assert inner["additionalProperties"] is False
    assert set(inner["required"]) == set(inner["properties"])


def test_generate_structured_uses_best_effort_for_free_form_maps():
    from pydantic import BaseModel

    class Files(BaseModel):
        name: str
        files: dict[str, str]  # open-ended keys: strict mode cannot express this

    captured = {}
    client = ChatClient(
        base_url="https://api.groq.com/openai/v1",
        api_key="k",
        model="m",
        max_requests_per_minute=40,
        post_fn=_fake_post(captured),
    )
    client.generate_structured("go", schema=Files, max_output_tokens=64)
    spec = captured["body"]["response_format"]["json_schema"]
    assert spec["strict"] is False
    # the map keeps its open value schema so the model can still fill it
    assert spec["schema"]["properties"]["files"]["additionalProperties"] == {"type": "string"}


def _ok_post(calls):
    def post(url, headers, body):
        calls.append(json.loads(body))
        return json.dumps({"choices": [{"message": {"content": "ok"}}]})

    return post


def test_rpm_budget_sleeps_instead_of_raising_on_frozen_clock():
    calls, sleeps = [], []
    client = ChatClient(
        base_url="b", api_key="k", model="m", max_requests_per_minute=1,
        post_fn=_ok_post(calls), now=lambda: 1000.0, sleep=sleeps.append,
    )
    assert client.generate("a") == "ok"
    assert sleeps == []
    assert client.generate("b") == "ok"  # no RpmExceeded
    assert sleeps == [60.0]
    assert len(calls) == 2


def test_extra_body_merged_without_clobbering_core_fields():
    calls = []
    client = ChatClient(
        base_url="b", api_key="k", model="m", max_requests_per_minute=5,
        post_fn=_ok_post(calls),
        extra_body={"reasoning_effort": "none", "model": "evil", "messages": []},
    )
    client.generate("hi")
    assert calls[0]["reasoning_effort"] == "none"
    assert calls[0]["model"] == "m"
    assert calls[0]["messages"] == [{"role": "user", "content": "hi"}]


def _flaky(statuses, calls):
    it = iter(statuses)

    def post(url, headers, body):
        calls.append(1)
        st = next(it)
        if st is not None:
            raise LLMHTTPError(f"HTTP {st}", st)
        return json.dumps({"choices": [{"message": {"content": "ok"}}]})

    return post


def test_retries_429_then_succeeds():
    calls, sleeps = [], []
    client = ChatClient(
        base_url="b", api_key="k", model="m", max_requests_per_minute=10,
        post_fn=_flaky([429, None], calls), sleep=sleeps.append,
    )
    assert client.generate("x") == "ok"
    assert len(calls) == 2
    assert sleeps == [1.0]


def test_retry_exhausted_raises_llmerror():
    calls, sleeps = [], []
    client = ChatClient(
        base_url="b", api_key="k", model="m", max_requests_per_minute=10,
        post_fn=_flaky([429, 429, 429], calls), sleep=sleeps.append, max_retries=3,
    )
    with pytest.raises(LLMError):
        client.generate("x")
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


def test_non_retryable_status_raises_immediately():
    calls, sleeps = [], []
    client = ChatClient(
        base_url="b", api_key="k", model="m", max_requests_per_minute=10,
        post_fn=_flaky([401], calls), sleep=sleeps.append,
    )
    with pytest.raises(LLMHTTPError):
        client.generate("x")
    assert len(calls) == 1 and sleeps == []


def test_malformed_reply_raises_llmerror():
    client = ChatClient(
        base_url="b", api_key="k", model="m",
        max_requests_per_minute=40, post_fn=lambda u, h, b: "{not json}",
    )
    with pytest.raises(LLMError):
        client.generate("x")


def _rate_limited_then_ok(retry_after: float | None, calls: list[int]):
    def post(url, headers, body):
        calls.append(1)
        if len(calls) == 1:
            raise LLMHTTPError("HTTP 429", 429, retry_after=retry_after)
        return json.dumps({"choices": [{"message": {"content": "ok"}}]})

    return post


def test_429_waits_for_the_hosts_retry_after():
    # Groq's token budget resets per minute; a fixed 1s/2s backoff gives up long before that.
    calls: list[int] = []
    sleeps: list[float] = []
    client = ChatClient(
        base_url="https://api.example.com/v1", api_key="k", model="m",
        max_requests_per_minute=40, post_fn=_rate_limited_then_ok(7.5, calls),
        sleep=sleeps.append,
    )
    assert client.generate("hi") == "ok"
    assert sleeps == [7.5]


def test_retry_after_is_capped_so_a_bad_header_cannot_hang_a_run():
    calls: list[int] = []
    sleeps: list[float] = []
    client = ChatClient(
        base_url="https://api.example.com/v1", api_key="k", model="m",
        max_requests_per_minute=40, post_fn=_rate_limited_then_ok(5000.0, calls),
        sleep=sleeps.append,
    )
    client.generate("hi")
    assert sleeps == [60.0]


def test_parse_retry_after_handles_seconds_and_junk():
    from cvflow.llm import _parse_retry_after

    assert _parse_retry_after("2") == 2.0
    assert _parse_retry_after("1.5") == 1.5
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None


def test_http_errors_keep_the_hosts_reason_and_retry_after(monkeypatch):
    # Seen live: a distill failed with a bare "HTTP 400: Bad Request" and the cause was lost.
    import io
    import urllib.error
    import urllib.request
    from email.message import Message

    from cvflow.llm import _urllib_post

    headers = Message()
    headers["retry-after"] = "6"
    body = b'{"error":{"message":"json_validate_failed: output did not match the schema"}}'

    def fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", headers, io.BytesIO(body))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(LLMHTTPError) as excinfo:
        _urllib_post("https://api.example.com/v1/chat/completions", {}, "{}")
    assert "json_validate_failed" in str(excinfo.value)
    assert excinfo.value.status == 400 and excinfo.value.retry_after == 6.0
