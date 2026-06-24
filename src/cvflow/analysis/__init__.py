"""Job-description analysis: fetch the full JD text and extract structured fields with an LLM.

Fetching sits behind an injectable ``fetch_fn`` (stdlib urllib by default) so tests need no
network. Extraction uses any provider exposing ``generate(prompt) -> str``. Fields absent from a
posting come back empty rather than invented, and a failed fetch or an unparseable reply raises
rather than letting bad data flow downstream. Applicant-specific instructions (for example
"include the word pineapple") are kept verbatim so the resume step can honor them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.request import Request, urlopen

__all__ = [
    "JDAnalysis",
    "JDAnalyzer",
    "JDFetchError",
    "JDAnalysisError",
    "fetch_jd",
    "resolve_tailoring_analysis",
]

logger = logging.getLogger("cvflow.analysis")


class JDFetchError(Exception):
    """Raised when a JD page cannot be fetched or is empty."""


class JDAnalysisError(Exception):
    """Raised when a JD or crux cannot be obtained or parsed."""


@dataclass(frozen=True)
class JDAnalysis:
    required_skills: list[str]
    preferred_quals: list[str]
    seniority: str
    tone: str
    applicant_instructions: list[str]

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> JDAnalysis:
        d = json.loads(raw)
        return cls(
            required_skills=list(d.get("required_skills", [])),
            preferred_quals=list(d.get("preferred_quals", [])),
            seniority=str(d.get("seniority", "")),
            tone=str(d.get("tone", "")),
            applicant_instructions=list(d.get("applicant_instructions", [])),
        )


# --- JD fetching (stdlib HTML -> text) ---

_DROP_TAGS = {"script", "style", "head"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _DROP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._parts.append(data.strip())

    def text(self) -> str:
        return "\n".join(self._parts)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()


def _urllib_fetch(url: str) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 cvflow"})  # noqa: S310 (http(s) only)
    with urlopen(req, timeout=30) as resp:  # noqa: S310 (http(s) only by config)
        if resp.status != 200:
            raise JDFetchError(f"GET {url} -> HTTP {resp.status}")
        body: bytes = resp.read()
        return body.decode("utf-8", errors="replace")


def fetch_jd(url: str, *, fetch_fn: Callable[[str], str] = _urllib_fetch) -> str:
    """Fetch ``url`` and return plain-text JD. Raises :class:`JDFetchError` if empty."""
    text = _html_to_text(fetch_fn(url))
    if not text.strip():
        raise JDFetchError(f"empty JD text from {url}")
    return text


# --- LLM extraction ---


class _Provider(Protocol):
    def generate(self, prompt: str) -> str: ...


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t.rsplit("```", 1)[0]
    return t.strip()


_PROMPT = (
    "You analyze a job description and extract structured fields.\n"
    "Return ONLY a JSON object with keys: required_skills (array of strings), "
    "preferred_quals (array), seniority (string), tone (string), "
    "applicant_instructions (array of any explicit instructions the posting gives "
    'applicants, e.g. "include the word pineapple", a required subject line, or a '
    "portfolio link to add).\n"
    "Extract only what the text states; use empty arrays/strings for anything absent. "
    "Do not invent.\n\n"
    "## Job description\n{jd}\n"
)


class JDAnalyzer:
    """LLM-extracts a :class:`JDAnalysis` from JD text via a generate(prompt)->str provider."""

    def __init__(self, provider: _Provider) -> None:
        self._provider = provider

    def analyze(self, jd_text: str) -> JDAnalysis:
        raw = self._provider.generate(_PROMPT.format(jd=jd_text))
        try:
            return JDAnalysis.from_json(_strip_code_fence(raw))
        except (ValueError, json.JSONDecodeError) as exc:
            raise JDAnalysisError(f"could not parse analysis reply: {exc}") from exc


# --- tailoring analysis resolution ---


def _crux_to_analysis(job_id: str, store: Any) -> JDAnalysis:
    """Build a tailoring JDAnalysis from the cached crux JSON (no network, no LLM)."""
    crux_json = store.get_crux(job_id)
    if not crux_json:
        raise JDAnalysisError(f"no crux or JD analysis for {job_id}; run discovery first")
    c = json.loads(crux_json)
    must = list(c.get("must_have_skills") or [])
    tech = list(c.get("tech_stack") or [])
    required = list(dict.fromkeys(must + tech))
    must_set = set(must)
    preferred = [t for t in tech if t not in must_set]
    instr = c.get("applicant_instructions")
    if isinstance(instr, list):
        instructions = instr
    elif instr:
        instructions = [instr]
    else:
        instructions = []
    return JDAnalysis(
        required_skills=required,
        preferred_quals=preferred,
        seniority=str(c.get("seniority_signal") or ""),
        tone="",
        applicant_instructions=instructions,
    )


def resolve_tailoring_analysis(
    job_id: str,
    *,
    store: Any,
    analyzer: Any,
    use_jd_analysis: bool,
    notify: Callable[[str], None] | None = None,
    fetch_fn: Callable[[str], str] = fetch_jd,
) -> JDAnalysis:
    """Return the JDAnalysis that drives tailoring.

    With ``use_jd_analysis=False`` (the default) it derives the analysis from the cached crux —
    no network, no LLM call, and it works on every ranked job. With ``True`` it re-analyzes the
    stored JD text (fetching the URL only if no text is stored); on any failure there it logs,
    notifies, and falls back to the crux rather than failing silently. Raises only if neither a
    JD nor a crux exists.
    """
    if use_jd_analysis:
        try:
            text = store.get_jd_text(job_id)
            if not text:
                app = store.get(job_id)
                if app is None:
                    raise JDAnalysisError(f"no such job: {job_id}")
                text = fetch_fn(app.jd_url)
            analysis: JDAnalysis = analyzer.analyze(text)
            store.save_analysis(job_id, analysis)
            return analysis
        except Exception as exc:  # noqa: BLE001 — fall back to the crux, never silent
            logger.warning("JD analysis failed for %s: %s; using crux", job_id, exc)
            if notify is not None:
                notify(f"JD analysis failed for {job_id}; falling back to the cached crux.")
    return _crux_to_analysis(job_id, store)
