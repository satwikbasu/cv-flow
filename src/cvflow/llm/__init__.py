"""A small OpenAI-compatible chat client for the distillation, ranking and tailoring calls.

One client talks to any OpenAI-style ``/chat/completions`` endpoint over the standard library
(no extra HTTP dependency). It keeps every call inside the provider's free tier by tracking a
per-minute request budget (sleeping until the window rolls rather than overrunning the quota)
and retries transient 429/5xx responses with backoff.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any


class LLMError(Exception):
    """Base class for chat-client errors."""


class RpmExceeded(LLMError):
    """Legacy: no longer raised by normal pacing (the client sleeps instead)."""


class LLMHTTPError(LLMError):
    """A non-200 HTTP response; ``status`` lets callers tell transient from permanent."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def _now_seconds() -> float:
    return time.monotonic()


def _urllib_post(url: str, headers: dict[str, str], body: str) -> str:
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    # Some OpenAI-compatible hosts (e.g. Cerebras behind Cloudflare) reject the default
    # urllib User-Agent with a 403/1010; send a browser-like one.
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) cvflow/1.0", **headers}
    req = Request(url, data=body.encode(), headers=headers, method="POST")  # noqa: S310
    # Free-tier latency can swing past two minutes for a whole-cohort ranking call, so give it
    # room. There is no agent deadline on the scheduled path, and the RPM budget still caps volume.
    try:
        with urlopen(req, timeout=300) as resp:  # noqa: S310 (https by config)
            if resp.status != 200:
                raise LLMHTTPError(f"POST {url} -> HTTP {resp.status}", resp.status)
            raw: bytes = resp.read()
            return raw.decode("utf-8", errors="replace")
    except HTTPError as exc:
        # urlopen raises on 4xx/5xx (e.g. a missing/invalid key -> 401) before the status check
        # above. Wrap it as an LLMHTTPError so callers' ``except LLMError`` can degrade gracefully
        # and the client can retry transient statuses.
        raise LLMHTTPError(f"POST {url} -> HTTP {exc.code}: {exc.reason}", exc.code) from exc
    except URLError as exc:
        raise LLMError(f"POST {url} failed: {exc.reason}") from exc


class ChatClient:
    """OpenAI-compatible chat client; ``generate(prompt) -> str``.

    Standard-library transport (no new dependency). The per-minute budget guard keeps the
    free tier intact by sleeping until the window rolls when the budget is spent.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        max_requests_per_minute: int,
        seed_field: str = "seed",
        post_fn: Callable[[str, dict[str, str], str], str] = _urllib_post,
        now: Callable[[], float] = _now_seconds,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._max_rpm = max_requests_per_minute
        # Most OpenAI-compatible hosts call it "seed"; Mistral calls it "random_seed".
        self._seed_field = seed_field
        self._post = post_fn
        self._now = now
        self._sleep = sleep
        self._max_retries = max_retries
        self._extra_body = dict(extra_body or {})
        self._window_start = now()
        self._count = 0

    def _spend_one(self) -> None:
        t = self._now()
        if t - self._window_start >= 60.0:
            self._window_start = t
            self._count = 0
        if self._count >= self._max_rpm:
            self._sleep(max(0.0, 60.0 - (t - self._window_start)))
            # Advance deterministically (a frozen clock never rolls the window by itself).
            self._window_start = max(self._now(), self._window_start + 60.0)
            self._count = 0
        self._count += 1

    def generate(
        self,
        prompt: str,
        *,
        temperature: float | None = None,
        seed: int | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        json_object: bool = False,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            **self._extra_body,
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if seed is not None:
            payload[self._seed_field] = seed
        if top_p is not None:
            payload["top_p"] = top_p
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format
        elif json_object:
            payload["response_format"] = {"type": "json_object"}
        body = json.dumps(payload)
        for attempt in range(1, self._max_retries + 1):
            self._spend_one()
            try:
                raw = self._post(self._url, headers, body)
                break
            except LLMHTTPError as exc:
                if exc.status not in _RETRY_STATUSES or attempt == self._max_retries:
                    raise
                self._sleep(float(2 ** (attempt - 1)))
        try:
            data = json.loads(raw)
            return str(data["choices"][0]["message"]["content"])
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"could not parse chat reply: {exc}") from exc

    def generate_structured(
        self,
        prompt: str,
        *,
        schema: Any = None,
        seed: int = 0,
        max_output_tokens: int = 512,
    ) -> str:
        """Structured-JSON generation over the OpenAI-compatible chat API.

        When ``schema`` is a pydantic model, its JSON schema is sent as a strict ``json_schema``
        response_format (Mistral/OpenAI/Cerebras enforce the exact shape); otherwise a plain
        ``json_object`` is requested. The caller still validates with pydantic. Returns the raw
        JSON string.
        """
        response_format: dict[str, Any] = {"type": "json_object"}
        if schema is not None and hasattr(schema, "model_json_schema"):
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": schema.model_json_schema(),
                },
            }
        # No top_p: temperature=0 is already greedy, and Mistral rejects top_p<1 then.
        return self.generate(
            prompt, temperature=0, seed=seed,
            max_tokens=max_output_tokens, response_format=response_format,
        )
