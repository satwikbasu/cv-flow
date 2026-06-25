"""Tests for the OpenAI-compatible chat client. No live key, no network."""

import json

import pytest

from cvflow.llm import ChatClient, LLMError, RpmExceeded


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


def test_rpm_budget_raises_before_calling_out():
    calls = {"n": 0}

    def post(url, headers, body):
        calls["n"] += 1
        return json.dumps({"choices": [{"message": {"content": "ok"}}]})

    clock = {"t": 1000.0}
    client = ChatClient(
        base_url="b", api_key="k", model="m",
        max_requests_per_minute=2, post_fn=post, now=lambda: clock["t"],
    )
    client.generate("a")
    client.generate("b")
    with pytest.raises(RpmExceeded):
        client.generate("c")
    assert calls["n"] == 2  # never called out on the over-budget request
    clock["t"] += 61  # the minute window rolls over
    assert client.generate("d") == "ok"


def test_malformed_reply_raises_llmerror():
    client = ChatClient(
        base_url="b", api_key="k", model="m",
        max_requests_per_minute=40, post_fn=lambda u, h, b: "{not json}",
    )
    with pytest.raises(LLMError):
        client.generate("x")
