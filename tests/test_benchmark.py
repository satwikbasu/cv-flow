"""Hybrid benchmark math. Pure functions; no network."""

from cvflow.discovery import JobPosting
from cvflow.discovery.benchmark import FitResult, benchmark_cohort, comp_score


def test_comp_score_anchors():
    assert comp_score(7.0, 7, 40) == 0.0
    assert comp_score(40.0, 7, 40) == 1.0
    assert abs(comp_score(23.0, 7, 40) - 0.4848) < 0.01
    assert comp_score(3.0, 7, 40) == 0.0  # below floor clamps


def _job(jid, max_amount=None, currency=None):
    return JobPosting(job_id=jid, title="T", company="C", location="L", description="d",
                      url=f"https://{jid}", site="indeed", date_posted="2026-06-05",
                      max_amount=max_amount, currency=currency)


def test_benchmark_cohort_M_blends_fit_and_comp_sorted():
    jobs = {"a": _job("a", 2_300_000, "INR"), "b": _job("b", 700_000, "INR")}
    fits = {"a": FitResult(70, "ok", []), "b": FitResult(85, "great", [])}
    out = benchmark_cohort(jobs, fits, cohort="M", fit_weight=0.70, comp_weight=0.30,
                           min_lpa=7, top_lpa=40)
    # a: 0.7*0.70 + 0.3*0.4848 = 0.4900+0.1454=0.6354 -> 64 ; b: 0.7*0.85+0.3*0 = 60
    assert [j.job_id for j in out] == ["a", "b"]
    assert out[0].benchmark == 64 and out[1].benchmark == 60
    assert out[0].cohort == "M"


def test_benchmark_cohort_N_is_fit_only():
    jobs = {"a": _job("a"), "b": _job("b")}
    fits = {"a": FitResult(80, "x", ["PAY_UNKNOWN"]), "b": FitResult(45, "y", [])}
    out = benchmark_cohort(jobs, fits, cohort="N", fit_weight=0.70, comp_weight=0.30,
                           min_lpa=7, top_lpa=40)
    assert [j.job_id for j in out] == ["a", "b"]
    assert out[0].benchmark == 80 and out[1].benchmark == 45
    assert "PAY_UNKNOWN" in out[0].concerns


def test_build_fingerprint_includes_prefs_and_roles():
    from cvflow.discovery.benchmark import build_fingerprint
    fp = build_fingerprint(prefs_text="HARD: yoe<=1", prefer_roles={"devops": 1.0, "frontend": 0.3})
    assert "yoe<=1" in fp
    assert "devops" in fp and "1.0" in fp


def test_fit_scores_parses_and_renders_prefer_roles():
    import json

    from cvflow.discovery.benchmark import fit_scores
    from cvflow.discovery.distill import Crux

    captured = {}

    class _Prov:
        def generate(self, prompt, **kw):
            captured["prompt"] = prompt
            captured["kw"] = kw
            return json.dumps([
                {"job_id": "indeed:1", "fit_score": 88, "fit_reason": "infra match",
                 "concern_codes": ["SERVICE_COMPANY"]},
                {"job_id": "ghost", "fit_score": 50, "fit_reason": "x", "concern_codes": []},
            ])

    crux = Crux(job_id="indeed:1", role_family="devops", seniority_signal="junior",
                min_years_required=2, max_years_required=4, work_mode="remote",
                location_text="Remote", country="india", stated_salary=None,
                tech_stack=["docker"], night_shift_only=False, app_maintenance_focus=False,
                company_type="service", red_flags=[], applicant_instructions=None,
                one_line="x")
    out = fit_scores([crux], fingerprint="FP", prefer_roles={"devops": 1.0}, provider=_Prov())
    assert out["indeed:1"].fit_score == 88
    assert out["indeed:1"].concerns == ["SERVICE_COMPANY"]
    assert "ghost" not in out                       # unknown job_id dropped
    assert "devops" in captured["prompt"]            # prefer_roles rendered
    # fit uses strict json_schema structured output (some hosts ignore schemas), not json_object
    assert captured["kw"]["response_format"]["type"] == "json_schema"
    assert captured["kw"]["seed"] is not None


def test_fit_scores_degrades_on_provider_error():
    from cvflow.discovery.benchmark import fit_scores
    from cvflow.discovery.distill import Crux

    class _Boom:
        def generate(self, prompt, **kw):
            raise RuntimeError("provider down")

    crux = Crux(job_id="indeed:1", role_family="devops", seniority_signal="junior",
                min_years_required=1, max_years_required=2, work_mode="remote",
                location_text="Remote", country="india", stated_salary=None, tech_stack=[],
                night_shift_only=False, app_maintenance_focus=False, company_type="product",
                red_flags=[], applicant_instructions=None, one_line="x")
    out = fit_scores([crux], fingerprint="FP", prefer_roles={}, provider=_Boom())
    assert out["indeed:1"].fit_score == 0
    assert "RANKING_DEGRADED" in out["indeed:1"].concerns


def test_n_cohort_job_gets_pay_unknown_concern():
    jobs = {"a": _job("a")}  # no salary
    fits = {"a": FitResult(80, "x", [])}
    out = benchmark_cohort(jobs, fits, cohort="N", fit_weight=0.70, comp_weight=0.30,
                           min_lpa=7, top_lpa=40)
    assert "PAY_UNKNOWN" in out[0].concerns


def test_m_cohort_job_with_salary_has_no_pay_unknown():
    jobs = {"a": _job("a", 2_300_000, "INR")}
    fits = {"a": FitResult(70, "ok", [])}
    out = benchmark_cohort(jobs, fits, cohort="M", fit_weight=0.70, comp_weight=0.30,
                           min_lpa=7, top_lpa=40)
    assert "PAY_UNKNOWN" not in out[0].concerns


def test_yoe_unknown_concern_when_crux_min_years_is_null():
    from cvflow.discovery.distill import Crux
    jobs = {"a": _job("a")}
    fits = {"a": FitResult(80, "x", [])}
    crux = Crux(job_id="a", role_family="devops", seniority_signal="junior",
                min_years_required=None, max_years_required=None, work_mode="remote",
                location_text="Remote", country="india", stated_salary=None, tech_stack=[],
                night_shift_only=False, app_maintenance_focus=False, company_type="product",
                red_flags=[], applicant_instructions=None, one_line="x")
    out = benchmark_cohort(jobs, fits, cohort="N", fit_weight=0.70, comp_weight=0.30,
                           min_lpa=7, top_lpa=40, cruxes={"a": crux})
    assert "YOE_UNKNOWN" in out[0].concerns


def test_no_yoe_unknown_when_min_years_present():
    from cvflow.discovery.distill import Crux
    jobs = {"a": _job("a", 2_300_000, "INR")}
    fits = {"a": FitResult(70, "ok", [])}
    crux = Crux(job_id="a", role_family="devops", seniority_signal="junior",
                min_years_required=1, max_years_required=2, work_mode="remote",
                location_text="Remote", country="india", stated_salary=None, tech_stack=[],
                night_shift_only=False, app_maintenance_focus=False, company_type="product",
                red_flags=[], applicant_instructions=None, one_line="x")
    out = benchmark_cohort(jobs, fits, cohort="M", fit_weight=0.70, comp_weight=0.30,
                           min_lpa=7, top_lpa=40, cruxes={"a": crux})
    assert "YOE_UNKNOWN" not in out[0].concerns


def test_fit_scores_tolerates_non_dict_entries():
    import json

    from cvflow.discovery.benchmark import fit_scores
    from cvflow.discovery.distill import Crux

    class _Prov:
        def generate(self, prompt, **kw):
            return json.dumps(["indeed:1", "garbage"])  # array of strings, not objects

    crux = Crux(job_id="indeed:1", role_family="devops", seniority_signal="junior",
                min_years_required=1, max_years_required=2, work_mode="remote",
                location_text="Remote", country="india", stated_salary=None, tech_stack=[],
                night_shift_only=False, app_maintenance_focus=False, company_type="product",
                red_flags=[], applicant_instructions=None, one_line="x")
    out = fit_scores([crux], fingerprint="FP", prefer_roles={}, provider=_Prov())
    assert out["indeed:1"].fit_score == 0
    assert "RANKING_DEGRADED" in out["indeed:1"].concerns


def _crux_for(jid):
    from cvflow.discovery.distill import Crux
    return Crux(job_id=jid, role_family="devops", seniority_signal="junior",
               min_years_required=1, max_years_required=2, work_mode="remote",
               location_text="Remote", country="india", stated_salary=None, tech_stack=[],
               night_shift_only=False, app_maintenance_focus=False, company_type="product",
               red_flags=[], applicant_instructions=None, one_line="x")


def test_fit_scores_salvages_malformed_json_missing_comma():
    from cvflow.discovery.benchmark import fit_scores
    bad = ('[{"job_id":"a","fit_score":80,"fit_reason":"ok","concern_codes":[]} '
           '{"job_id":"b","fit_score":60,"fit_reason":"y","concern_codes":["SERVICE_COMPANY"]}]')

    class _Prov:
        def generate(self, prompt, **kw):
            return bad

    out = fit_scores([_crux_for("a"), _crux_for("b")], fingerprint="FP",
                     prefer_roles={}, provider=_Prov())
    assert out["a"].fit_score == 80
    assert out["b"].fit_score == 60
    assert out["b"].concerns == ["SERVICE_COMPANY"]
    assert "RANKING_DEGRADED" not in out["a"].concerns


def test_fit_scores_degrades_when_unsalvageable():
    from cvflow.discovery.benchmark import fit_scores

    class _Prov:
        def generate(self, prompt, **kw):
            return "total garbage no json here"

    out = fit_scores([_crux_for("a")], fingerprint="FP", prefer_roles={}, provider=_Prov())
    assert out["a"].fit_score == 0
    assert "RANKING_DEGRADED" in out["a"].concerns


def test_fit_view_includes_must_have_skills_in_prompt():
    import json

    from cvflow.discovery.benchmark import _FIT_PREAMBLE, fit_scores
    from cvflow.discovery.distill import Crux
    captured = {}

    class _Prov:
        def generate(self, prompt, **kw):
            captured["prompt"] = prompt
            return json.dumps({"results": [{"job_id": "indeed:1", "fit_score": 55,
                                            "fit_reason": "adjacent stack",
                                            "concern_codes": ["STACK_MISMATCH"]}]})

    crux = Crux(job_id="indeed:1", role_family="devops", seniority_signal="junior",
                min_years_required=1, max_years_required=2, work_mode="remote",
                location_text="Remote", country="india", stated_salary=None,
                tech_stack=["docker"], must_have_skills=["kubernetes", "terraform"],
                night_shift_only=False, app_maintenance_focus=False, company_type="product",
                red_flags=[], applicant_instructions=None, one_line="x")
    out = fit_scores([crux], fingerprint="FP", prefer_roles={}, provider=_Prov())
    # must_have_skills is still passed as context, but the prompt no longer asks the LLM to gate
    # on it (the deterministic code gate owns that) and code passes concerns through.
    assert "must_have_skills" in captured["prompt"]
    assert "terraform" in captured["prompt"]
    assert "HARD GATE" not in _FIT_PREAMBLE  # the cap-at-40 directive is gone
    assert out["indeed:1"].concerns == ["STACK_MISMATCH"]
