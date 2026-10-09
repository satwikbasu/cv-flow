import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError

from cvflow.discovery.distill import ROLE_FAMILIES
from cvflow.onboarding.bundle import FORM_FIELD_KEYS, PROFILE_DOC_KEYS, CandidateBundle


def _valid() -> dict[str, Any]:
    return {
        "profile_docs": {k: "x" for k in PROFILE_DOC_KEYS},
        "project_docs": {"my-proj": "# My proj"},
        "form_fields": {k: "" for k in FORM_FIELD_KEYS},
        "candidate_skills": {"skills": ["Kubernetes"], "synonyms": {"k8s": "kubernetes"}},
        "discovery": {"search_terms": ["devops"]},
        "preferences": {"prefer_roles": {"devops": 0.9}},
        "resume": {"heading_tex": "h", "sections": {"skills": "s"},
                   "project_blocks": {"my-proj": "p"}},
        "review_notes": ["phone blank"],
    }


def _bad(mutate: Any) -> None:
    d = copy.deepcopy(_valid())
    mutate(d)
    with pytest.raises(ValidationError):
        CandidateBundle.model_validate(d)


def test_round_trip() -> None:
    d = _valid()
    assert CandidateBundle.model_validate(d).model_dump() == d


def test_missing_profile_doc() -> None:
    _bad(lambda d: d["profile_docs"].pop("skills"))


def test_extra_profile_doc() -> None:
    _bad(lambda d: d["profile_docs"].update(extra="x"))


def test_form_fields_19() -> None:
    _bad(lambda d: d["form_fields"].pop("email"))


def test_form_fields_21() -> None:
    _bad(lambda d: d["form_fields"].update(extra=""))


def test_empty_slug() -> None:
    def mutate(d: dict[str, Any]) -> None:
        d["project_docs"][" "] = "x"
        d["resume"]["project_blocks"][" "] = "x"

    _bad(mutate)


def test_unknown_synonym_target() -> None:
    _bad(lambda d: d["candidate_skills"]["synonyms"].update(tf="terraform"))


def test_bad_prefer_role() -> None:
    _bad(lambda d: d["preferences"]["prefer_roles"].update(wizard=0.5))


def test_project_slug_mismatch() -> None:
    _bad(lambda d: d["resume"]["project_blocks"].update(other="p"))


def test_taxonomy_size() -> None:
    assert len(ROLE_FAMILIES) == 12


def test_schema_small() -> None:
    assert len(json.dumps(CandidateBundle.model_json_schema())) < 8192
