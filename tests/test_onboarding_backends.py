import json
from typing import Any

import pytest

from cvflow.llm import LLMError
from cvflow.onboarding import OnboardingError
from cvflow.onboarding.backends import (
    BackendChain,
    ManualBackend,
    SingleShotBackend,
    TwoStageBackend,
    auto_chain,
    manual_chain,
)
from cvflow.onboarding.bundle import FORM_FIELD_KEYS, PROFILE_DOC_KEYS, CandidateBundle

BLOCK = "\\resumeProjectHeading{\\textbf{A} $|$ \\emph{Go}}{}\n"


def _data() -> dict[str, Any]:
    return {
        "profile_docs": {k: f"# {k}" for k in PROFILE_DOC_KEYS},
        "project_docs": {"alpha": "# alpha"},
        "form_fields": {k: "" for k in FORM_FIELD_KEYS},
        "candidate_skills": {"skills": ["Kubernetes"], "synonyms": {"k8s": "kubernetes"}},
        "discovery": {"search_terms": ["devops"]},
        "preferences": {"prefer_roles": {"devops": 0.9}},
        "resume": {
            "heading_tex": "x",
            "sections": {"projects": "", "skills": "s"},
            "project_blocks": {"alpha": BLOCK},
        },
        "review_notes": ["n1"],
    }


class FakeClient:
    def __init__(self, replies: list[str] | Exception) -> None:
        self.replies = replies
        self.prompts: list[str] = []

    def _next(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if isinstance(self.replies, Exception):
            raise self.replies
        return self.replies.pop(0)

    def generate_structured(self, prompt: str, **kw: Any) -> str:
        return self._next(prompt)

    def generate(self, prompt: str, **kw: Any) -> str:
        return self._next(prompt)


def test_single_shot_valid() -> None:
    b = SingleShotBackend(FakeClient([json.dumps(_data())]))
    assert isinstance(b.generate_bundle("p"), CandidateBundle)


def test_single_shot_invalid_json_and_llm_error() -> None:
    with pytest.raises(OnboardingError):
        SingleShotBackend(FakeClient(["not json"])).generate_bundle("p")
    with pytest.raises(OnboardingError):
        SingleShotBackend(FakeClient(LLMError("boom"))).generate_bundle("p")


def test_repair_rejects_non_str_values() -> None:
    assert SingleShotBackend(FakeClient(['{"a.tex": "x"}'])).repair("p") == {"a.tex": "x"}
    with pytest.raises(OnboardingError):
        SingleShotBackend(FakeClient(['{"a.tex": 1}'])).repair("p")


def test_two_stage_feeds_stage1_into_stage2() -> None:
    d = _data()
    s1 = {k: d[k] for k in ("profile_docs", "project_docs", "form_fields", "resume")}
    s1["review_notes"] = ["n1"]
    s2 = {k: d[k] for k in ("candidate_skills", "discovery", "preferences")}
    s2["review_notes"] = ["n2"]
    c1, c2 = FakeClient([json.dumps(s1)]), FakeClient([json.dumps(s2)])
    bundle = TwoStageBackend(c1, c2).generate_bundle("stage1 prompt")
    assert "# skills" in c2.prompts[0]  # stage-1 output embedded
    assert bundle.review_notes == ["n1", "n2"]
    assert bundle.candidate_skills.skills == ["Kubernetes"]


def test_manual_fenced_and_bare() -> None:
    raw = json.dumps(_data())
    seen: list[str] = []
    for reply in (f"here:\n```json\n{raw}\n```\nthanks", raw):
        b = ManualBackend(seen.append, lambda r=reply: r)  # type: ignore[misc]
        assert isinstance(b.generate_bundle("P"), CandidateBundle)
    assert seen == ["P", "P"]
    assert manual_chain(seen.append, lambda: '{"a": "b"}').repair("p") == {"a": "b"}


def test_chain_falls_through_and_lists_all_failures() -> None:
    good = SingleShotBackend(FakeClient([json.dumps(_data())]))
    bad = SingleShotBackend(FakeClient(LLMError("down")))
    assert isinstance(BackendChain([bad, good]).generate_bundle("p"), CandidateBundle)
    with pytest.raises(OnboardingError) as ei:
        BackendChain([bad, SingleShotBackend(FakeClient(["nope"]))]).generate_bundle("p")
    assert ei.value.args[0].count("SingleShotBackend") == 2


def test_auto_chain_order() -> None:
    c = FakeClient([])
    assert len(auto_chain(None, c, c)._backends) == 1
    assert len(auto_chain(None, c, c, c)._backends) == 2
