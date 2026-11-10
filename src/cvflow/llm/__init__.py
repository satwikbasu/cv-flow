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

    def __init__(self, message: str, status: int, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after  # seconds the host asked us to wait, if it said


_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_WAIT = 60.0  # never sleep longer than this on one retry, whatever the host says


def _parse_retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header; the HTTP-date form is rare enough to ignore."""
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _strict_json_schema(schema: Any) -> Any:
    """Make a JSON schema satisfy strict structured-output hosts (Groq rejects anything else).

    Every object must forbid extra properties and list all of them as required; pydantic emits
    neither by default. Walks nested objects, ``$defs``, arrays and unions in place.
    """
    if isinstance(schema, dict):
        if schema.get("type") == "object" and "properties" in schema:
            schema["additionalProperties"] = False
            schema["required"] = list(schema["properties"])
        for value in schema.values():
            _strict_json_schema(value)
    elif isinstance(schema, list):
        for item in schema:
            _strict_json_schema(item)
    return schema


def _has_open_object(schema: Any) -> bool:
    """True if any object takes arbitrary keys (a ``dict[str, X]`` field).

    Strict structured output cannot express those, so such schemas go out best-effort.
    """
    if isinstance(schema, dict):
        if schema.get("type") == "object" and (
            "properties" not in schema or schema.get("additionalProperties") not in (None, False)
        ):
            return True
        return any(_has_open_object(v) for v in schema.values())
    if isinstance(schema, list):
        return any(_has_open_object(item) for item in schema)
    return False


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
        # Keep the start of the host's error body: it names the real cause (a schema mismatch,
        # a bad parameter, which limit was hit), which "Bad Request" alone does not.
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()[:300]
        except Exception:  # noqa: BLE001 (best effort; the status alone still gets raised)
            detail = ""
        raise LLMHTTPError(
            f"POST {url} -> HTTP {exc.code}: {exc.reason}" + (f": {detail}" if detail else ""),
            exc.code,
            retry_after=_parse_retry_after(exc.headers.get("retry-after") if exc.headers else None),
        ) from exc
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
                # Prefer the host's own Retry-After (Groq sends one on every 429); a per-minute
                # token budget outlasts a short fixed backoff.
                wait = exc.retry_after if exc.retry_after is not None else 2 ** (attempt - 1)
                self._sleep(min(float(wait), _MAX_RETRY_WAIT))
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

        When ``schema`` is a pydantic model, its JSON schema is sent as a ``json_schema``
        response_format: strict when every object is closed, best-effort when it has
        open-ended maps that strict mode cannot express. Otherwise a plain ``json_object`` is
        requested. The caller still validates with pydantic. Returns the raw JSON string.
        """
        response_format: dict[str, Any] = {"type": "json_object"}
        if schema is not None and hasattr(schema, "model_json_schema"):
            raw_schema = schema.model_json_schema()
            strict = not _has_open_object(raw_schema)
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": strict,
                    "schema": _strict_json_schema(raw_schema) if strict else raw_schema,
                },
            }
        # No top_p: temperature=0 is already greedy, and Mistral rejects top_p<1 then.
        return self.generate(
            prompt, temperature=0, seed=seed,
            max_tokens=max_output_tokens, response_format=response_format,
        )
