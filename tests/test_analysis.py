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


# --- the optional JD re-analysis path (use_jd_analysis=True) ---


class _RicherStore:
    def __init__(self, *, crux: dict | None = None, jd_text: str | None = None) -> None:
        self._crux = crux
        self._jd_text = jd_text
        self.saved: JDAnalysis | None = None

    def get_crux(self, job_id: str) -> str | None:
        return json.dumps(self._crux) if self._crux else None

    def get_jd_text(self, job_id: str) -> str | None:
        return self._jd_text

    def get(self, job_id: str) -> None:
        return None

    def save_analysis(self, job_id: str, analysis: JDAnalysis) -> None:
        self.saved = analysis


class _FakeAnalyzer:
    def __init__(self, result: JDAnalysis) -> None:
        self._result = result
        self.calls = 0

    def analyze(self, text: str) -> JDAnalysis:
        self.calls += 1
        return self._result


class _RaisingAnalyzer:
    def analyze(self, text: str) -> JDAnalysis:
        raise RuntimeError("LLM down")


def test_crux_with_string_instruction_and_preferred_derivation() -> None:
    store = _RicherStore(
        crux={
            "must_have_skills": ["go", "python"],
            "tech_stack": ["go", "python", "postgresql", "redis"],
            "seniority_signal": "junior",
            "applicant_instructions": "mention OSS",
        }
    )
    ana = resolve_tailoring_analysis(
        "x:1", store=store, analyzer=None, use_jd_analysis=False
    )
    assert ana.required_skills == ["go", "python", "postgresql", "redis"]
    assert ana.preferred_quals == ["postgresql", "redis"]  # tech stack beyond the must-haves
    assert ana.seniority == "junior"
    assert ana.applicant_instructions == ["mention OSS"]


def test_analysis_path_uses_stored_text_without_fetch() -> None:
    expected = JDAnalysis(
        required_skills=["go"],
        preferred_quals=[],
        seniority="mid",
        tone="",
        applicant_instructions=[],
    )
    analyzer = _FakeAnalyzer(expected)
    store = _RicherStore(jd_text="full JD text", crux={"tech_stack": ["x"]})

    def _no_fetch(url: str) -> str:
        raise AssertionError("should not fetch")

    ana = resolve_tailoring_analysis(
        "x:1", store=store, analyzer=analyzer, use_jd_analysis=True, fetch_fn=_no_fetch
    )
    assert analyzer.calls == 1
    assert ana is expected
    assert store.saved is expected


def test_analysis_path_falls_back_to_crux_and_notifies_on_failure() -> None:
    store = _RicherStore(
        jd_text="text", crux={"tech_stack": ["go"], "seniority_signal": "junior"}
    )
    notices: list[str] = []
    ana = resolve_tailoring_analysis(
        "x:1",
        store=store,
        analyzer=_RaisingAnalyzer(),
        use_jd_analysis=True,
        notify=notices.append,
        fetch_fn=lambda url: "text",
    )
    assert ana.required_skills == ["go"]  # crux fallback
    assert notices and "falling back" in notices[0].lower()
