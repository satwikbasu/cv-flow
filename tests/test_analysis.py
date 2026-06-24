import json

import pytest

from cvflow.analysis import (
    JDAnalysis,
    JDAnalysisError,
    JDAnalyzer,
    JDFetchError,
    fetch_jd,
    resolve_tailoring_analysis,
)


def test_jdanalysis_json_roundtrip() -> None:
    a = JDAnalysis(
        required_skills=["go", "docker"],
        preferred_quals=["kubernetes"],
        seniority="mid",
        tone="casual",
        applicant_instructions=["mention pineapple"],
    )
    assert JDAnalysis.from_json(a.to_json()) == a


def test_fetch_jd_strips_scripts_and_styles() -> None:
    html = (
        "<html><head><style>x{color:red}</style></head>"
        "<body><p>Hello</p><script>bad()</script><p>World</p></body></html>"
    )
    text = fetch_jd("http://example.test", fetch_fn=lambda _url: html)
    assert "Hello" in text
    assert "World" in text
    assert "bad()" not in text
    assert "color:red" not in text


def test_fetch_jd_empty_raises() -> None:
    with pytest.raises(JDFetchError):
        fetch_jd("http://example.test", fetch_fn=lambda _url: "<html><body></body></html>")


class _FakeProvider:
    def __init__(self, reply: str) -> None:
        self._reply = reply

    def generate(self, prompt: str) -> str:
        return self._reply


def test_analyzer_parses_json_reply() -> None:
    reply = json.dumps(
        {
            "required_skills": ["go"],
            "preferred_quals": [],
            "seniority": "mid",
            "tone": "",
            "applicant_instructions": [],
        }
    )
    analysis = JDAnalyzer(_FakeProvider(reply)).analyze("some jd text")
    assert analysis.required_skills == ["go"]
    assert analysis.seniority == "mid"


def test_analyzer_bad_json_raises() -> None:
    with pytest.raises(JDAnalysisError):
        JDAnalyzer(_FakeProvider("not json at all")).analyze("jd")


class _FakeStore:
    def __init__(self, crux: str | None) -> None:
        self._crux = crux

    def get_crux(self, job_id: str) -> str | None:
        return self._crux


def test_resolve_uses_crux_by_default() -> None:
    crux = json.dumps(
        {
            "must_have_skills": ["go"],
            "tech_stack": ["go", "docker"],
            "seniority_signal": "mid",
            "applicant_instructions": [],
        }
    )
    analysis = resolve_tailoring_analysis(
        "j1", store=_FakeStore(crux), analyzer=None, use_jd_analysis=False
    )
    assert "go" in analysis.required_skills
    assert "docker" in analysis.required_skills
    assert analysis.seniority == "mid"


def test_resolve_raises_without_crux() -> None:
    with pytest.raises(JDAnalysisError):
        resolve_tailoring_analysis(
            "j1", store=_FakeStore(None), analyzer=None, use_jd_analysis=False
        )
