"""JD distillation -> structured crux. Mocked provider; no network."""

import json

import pytest

from cvflow.discovery import JobPosting
from cvflow.discovery.distill import Crux, Distiller


def _posting(jid="indeed:1", desc="Build CI/CD pipelines. 2+ years."):
    return JobPosting(job_id=jid, title="DevOps Engineer", company="Acme",
                      location="Remote", description=desc, url=f"https://x/{jid}",
                      site="indeed", date_posted="2026-06-05")


_REPLY = json.dumps({
    "job_id": "indeed:1", "role_family": "devops", "seniority_signal": "junior",
    "min_years_required": 2, "max_years_required": 4, "work_mode": "remote",
    "location_text": "Remote (IN)", "country": "india", "stated_salary": None,
    "tech_stack": ["docker", "k8s"], "must_have_skills": ["k8s", "terraform"],
    "night_shift_only": False,
    "app_maintenance_focus": False, "company_type": "product", "red_flags": [],
    "applicant_instructions": None, "one_line": "Build CI/CD for a product team",
})


class _Prov:
    def __init__(self, reply=_REPLY):
        self.reply = reply
        self.calls = []

    def generate_structured(self, prompt, *, schema, seed, max_output_tokens):
        self.calls.append({"prompt": prompt, "schema": schema, "seed": seed})
        return self.reply


def test_distiller_parses_crux_and_passes_jd_text():
    prov = _Prov()
    crux = Distiller(prov, seed=7).distill(_posting())
    assert isinstance(crux, Crux)
    assert crux.role_family == "devops"
    assert crux.min_years_required == 2
    assert crux.stated_salary is None
    assert crux.tech_stack == ["docker", "k8s"]
    assert crux.must_have_skills == ["k8s", "terraform"]
    assert "Build CI/CD pipelines" in prov.calls[0]["prompt"]  # JD text fed in
    assert prov.calls[0]["schema"] is Crux
    assert prov.calls[0]["seed"] == 7


def test_distiller_truncates_long_jd():
    prov = _Prov()
    long_desc = "x" * 50_000
    Distiller(prov, seed=1, max_jd_chars=1000).distill(_posting(desc=long_desc))
    assert len(prov.calls[0]["prompt"]) < 5000  # JD truncated


def test_distiller_raises_on_unparseable_reply():
    from pydantic import ValidationError

    prov = _Prov(reply="not json")
    with pytest.raises(ValidationError):
        Distiller(prov, seed=1).distill(_posting())
