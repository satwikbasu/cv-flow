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
