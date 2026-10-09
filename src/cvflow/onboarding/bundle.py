"""The validated shape the onboarding generator must produce and the applier consumes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_validator

from cvflow.discovery.distill import ROLE_FAMILIES
from cvflow.discovery.skills import normalize

PROFILE_DOC_KEYS = frozenset(
    {"skills", "experience", "education", "personality", "preferences", "projects",
     "essay_answers"}
)
FORM_FIELD_KEYS = frozenset(
    {"full_name", "email", "phone", "address_line1", "address_city", "address_state",
     "address_postal_code", "address_country", "date_of_birth", "work_authorization",
     "requires_sponsorship", "salary_expectation", "linkedin_url", "github_url",
     "portfolio_url", "willing_to_relocate", "notice_period", "current_company",
     "current_title", "highest_degree"}
)


def _exact_keys(name: str, got: dict[str, Any], want: frozenset[str]) -> None:
    missing, extra = want - got.keys(), got.keys() - want
    if missing or extra:
        raise ValueError(f"{name} keys mismatch: missing={sorted(missing)} extra={sorted(extra)}")


class CandidateSkills(BaseModel):
    skills: list[str]
    synonyms: dict[str, str]

    @model_validator(mode="after")
    def _targets_known(self) -> CandidateSkills:
        known = {normalize(s) for s in self.skills}
        bad = [t for t in self.synonyms.values() if normalize(t) not in known]
        if bad:
            raise ValueError(f"synonym targets not in skills: {bad}")
        return self


class ResumeBundle(BaseModel):
    heading_tex: str
    sections: dict[str, str]
    project_blocks: dict[str, str]


class CandidateBundle(BaseModel):
    profile_docs: dict[str, str]
    project_docs: dict[str, str]
    form_fields: dict[str, str]
    candidate_skills: CandidateSkills
    discovery: dict[str, Any]
    preferences: dict[str, Any]
    resume: ResumeBundle
    review_notes: list[str]

    @model_validator(mode="after")
    def _check(self) -> CandidateBundle:
        _exact_keys("profile_docs", self.profile_docs, PROFILE_DOC_KEYS)
        _exact_keys("form_fields", self.form_fields, FORM_FIELD_KEYS)
        if any(not slug.strip() for slug in self.project_docs):
            raise ValueError("project_docs has an empty slug")
        roles = self.preferences.get("prefer_roles")
        if roles is not None:
            bad = [r for r in roles if r not in ROLE_FAMILIES]
            if bad:
                raise ValueError(f"prefer_roles not in role taxonomy: {bad}")
        if self.resume.project_blocks.keys() != self.project_docs.keys():
            raise ValueError("resume.project_blocks slugs must match project_docs slugs")
        return self
