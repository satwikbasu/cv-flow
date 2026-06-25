"""Tests for discovery: normalization, dedup, two-stage distill+benchmark, date persistence and
the blocked-source notice. Fully mocked — no live JobSpy/network and no live LLM."""

import json
import re
from datetime import date

from cvflow.discovery import DiscoveryService, normalize_rows
from cvflow.discovery.naukri import NaukriUnreachable
from cvflow.statemachine import Status
from cvflow.storage import ApplicationStore


def _row(id_: str, site: str = "linkedin", **over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": id_,
        "site": site,
        "title": f"Role {id_}",
        "company": f"Co {id_}",
        "location": "Remote",
        "description": f"desc {id_}",
        "job_url": f"https://x/{id_}",
        "date_posted": "2026-06-02",
    }
    base.update(over)
    return base


# --- normalization ---


def test_normalize_builds_stable_job_id() -> None:
    postings = normalize_rows([_row("1"), _row("2", site="indeed")])
    ids = [p.job_id for p in postings]
    assert ids == ["linkedin:1", "indeed:2"]
    assert postings[0].title == "Role 1"
    assert postings[0].url == "https://x/1"


def test_normalize_dedups_within_batch_first_wins() -> None:
    postings = normalize_rows([_row("1", title="A"), _row("1", title="B")])
    assert len(postings) == 1
    assert postings[0].title == "A"


def test_normalize_falls_back_to_url_hash_when_no_id() -> None:
    postings = normalize_rows([_row("", id="")])
    assert len(postings) == 1
    assert postings[0].job_id.startswith("linkedin:")  # url-hash suffix, still stable


def test_normalize_cleans_nan_and_carries_salary() -> None:
    rows = [{
        "id": "1", "site": "indeed", "title": "Backend", "company": float("nan"),
        "location": "Remote", "description": "d", "job_url": "https://x/1",
        "date_posted": "2026-06-02", "min_amount": 800000.0, "max_amount": 1200000.0,
        "currency": "INR",
    }]
    p = normalize_rows(rows)[0]
    assert p.company == ""
    assert p.min_amount == 800000.0
    assert p.max_amount == 1200000.0
    assert p.currency == "INR"


def test_normalize_missing_salary_is_none() -> None:
    p = normalize_rows([_row("1")])[0]
    assert p.min_amount is None
    assert p.currency is None


def test_normalize_carries_experience_range() -> None:
    p = normalize_rows([_row("1", experience_range="2-4 Yrs")])[0]
    assert p.experience_range == "2-4 Yrs"


def test_normalize_experience_range_absent_is_none() -> None:
    assert normalize_rows([_row("1")])[0].experience_range is None


def test_normalize_carries_job_type() -> None:
    assert normalize_rows([_row("1", job_type="internship")])[0].job_type == "internship"
    assert normalize_rows([_row("1")])[0].job_type is None


def test_parse_min_years() -> None:
    from cvflow.discovery import _parse_min_years
    assert _parse_min_years("2-4 Yrs") == 2
    assert _parse_min_years("5+ years") == 5
    assert _parse_min_years("0-1 Yrs") == 0
    assert _parse_min_years("Fresher") == 0
    assert _parse_min_years("") is None
    assert _parse_min_years(None) is None
    assert _parse_min_years("competitive") is None  # unparseable -> keep (never fabricate)


# --- two-stage discovery (distill -> benchmark) ---


class _StubMistral:
    """Stub Mistral provider: distills (generate_structured) AND fit-scores (generate)."""

    def __init__(self) -> None:
        self.calls = 0

    def generate_structured(self, prompt, *, schema, seed, max_output_tokens):
        self.calls += 1
        m = re.search(r"job_id: (\S+)", prompt)
        jid = m.group(1) if m else "unknown"
        return json.dumps({
            "job_id": jid, "role_family": "devops", "seniority_signal": "junior",
            "min_years_required": 1, "max_years_required": 2, "work_mode": "remote",
            "location_text": "Remote", "country": "india", "stated_salary": None,
            "tech_stack": ["k8s"], "night_shift_only": False, "app_maintenance_focus": False,
            "company_type": "product", "red_flags": [], "applicant_instructions": None,
            "one_line": "infra"})

    def generate(self, prompt, **kw):
        ids = sorted(set(re.findall(r'"job_id": "([^"]+)"', prompt)))
        return json.dumps({"results": [{"job_id": i, "fit_score": 80, "fit_reason": "ok",
                                        "concern_codes": []} for i in ids]})


def _two_stage_service(store, search_fn, **over):
    kwargs = dict(
        store=store, search_fn=search_fn,
        search_terms=["go developer"], locations=["Remote", "India"],
        sites=["linkedin", "indeed"], results_wanted_per_site=10, hours_old=72,
        throttle_seconds=1.0, sleep=lambda s: None,
        exclude_title_keywords=["senior", "lead"], min_ctc_lpa=7,
        distiller=_StubMistral(), fingerprint="FP",
        prefer_roles={"devops": 1.0}, exclude_when=[], fit_weight=0.70,
        comp_weight=0.30, top_ctc_lpa=40, max_distill_per_cohort=60, top_n_per_cohort=5,
    )
    kwargs.update(over)
    return DiscoveryService(**kwargs)


def _all_ids(result):
    return {bj.posting.job_id for bj in result["M"] + result["N"]}


def test_discover_cross_day_dedup_excludes_known_jobs() -> None:
    store = ApplicationStore(":memory:")
    store.add("linkedin:1", "Co 1", "Role 1", "https://x/1")  # already seen previously
    svc = _two_stage_service(store, lambda **k: [_row("1"), _row("2")])
    result = svc.discover()
    assert _all_ids(result) == {"linkedin:2"}  # the known job was filtered out


def test_discover_persists_presented_jobs_as_discovered() -> None:
    store = ApplicationStore(":memory:")
    svc = _two_stage_service(store, lambda **k: [_row("1"), _row("2")])
    svc.discover()
    discovered = store.list_by_status(Status.DISCOVERED)
    assert {a.job_id for a in discovered} == {"linkedin:1", "linkedin:2"}


def test_discover_throttles_once_per_scrape_pair() -> None:
    store = ApplicationStore(":memory:")
    calls = {"n": 0}

    sleeps: list[float] = []

    def search_fn(**kwargs):
        calls["n"] += 1
        return [_row("1")]

    svc = _two_stage_service(store, search_fn, throttle_seconds=1.0, sleep=sleeps.append)
    svc.discover()
    assert calls["n"] == 2  # 1 term x 2 locations
    assert sleeps == [1.0, 1.0]


def test_discover_survives_a_failing_search_batch() -> None:
    """One (term,location) batch raising must not abort the whole run."""
    store = ApplicationStore(":memory:")
    calls = {"n": 0}

    def flaky_search(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("LinkedInException: Invalid country string: 'kosovo'")
        return [_row("2", site="indeed")]

    svc = _two_stage_service(store, flaky_search, throttle_seconds=0.0, sleep=lambda s: None)
    result = svc.discover()
    assert _all_ids(result) == {"indeed:2"}  # good batch survived


def test_prefilter_drops_excluded_titles_and_below_floor_salary() -> None:
    store = ApplicationStore(":memory:")
    rows = [
        _row("1", title="Senior DevOps Engineer"),
        _row("2", title="DevOps Engineer", min_amount=400000.0,
             max_amount=500000.0, currency="INR"),
        _row("3", title="DevOps Engineer", min_amount=800000.0,
             max_amount=1200000.0, currency="INR"),
        _row("4", title="Platform Engineer"),
    ]
    svc = _two_stage_service(store, lambda **k: rows, throttle_seconds=0.0,
                             sleep=lambda s: None, locations=["Remote"])
    result = svc.discover()
    assert _all_ids(result) == {"linkedin:3", "linkedin:4"}


def test_discovery_passes_country_and_fetch_description_to_search_fn() -> None:
    store = ApplicationStore(":memory:")
    captured = {}

    def search_fn(**kwargs):
        captured.update(kwargs)
        return []

    svc = DiscoveryService(
        store=store, search_fn=search_fn,
        search_terms=["x"], locations=["Remote"], sites=["indeed"],
        results_wanted_per_site=10, hours_old=72,
        throttle_seconds=0.0, sleep=lambda s: None,
        country_indeed="india", linkedin_fetch_description=True,
    )
    svc.discover()
    assert captured.get("country_indeed") == "india"
    assert captured.get("linkedin_fetch_description") is True
    assert "job_type" not in captured  # not sent (Indeed hours_old conflict)


def test_discover_two_stage_partitions_and_benchmarks():
    store = ApplicationStore(":memory:")
    rows = [
        _row("1", title="DevOps Engineer", min_amount=2_000_000, max_amount=2_300_000,
             currency="INR", description="CI/CD pipelines, 2 years"),       # M
        _row("2", title="Platform Engineer", description="K8s platform, fresher"),  # N
    ]

    def _distill(prompt, *, schema, seed, max_output_tokens):
        jid = "linkedin:1" if "linkedin:1" in prompt else "linkedin:2"
        return json.dumps({
            "job_id": jid, "role_family": "devops", "seniority_signal": "junior",
            "min_years_required": 2, "max_years_required": 3, "work_mode": "remote",
            "location_text": "Remote", "country": "india", "stated_salary": None,
            "tech_stack": ["k8s"], "night_shift_only": False, "app_maintenance_focus": False,
            "company_type": "product", "red_flags": [], "applicant_instructions": None,
            "one_line": "infra"})

    class _Stub:
        def generate_structured(self, prompt, *, schema, seed, max_output_tokens):
            return _distill(prompt, schema=schema, seed=seed, max_output_tokens=max_output_tokens)

        def generate(self, prompt, **kw):
            ids = [x for x in ("linkedin:1", "linkedin:2") if x in prompt]
            return json.dumps({"results": [{"job_id": i, "fit_score": 80, "fit_reason": "ok",
                                            "concern_codes": []} for i in ids]})

    svc = DiscoveryService(
        store=store, search_fn=lambda **k: rows,
        search_terms=["x"], locations=["Remote"], sites=["linkedin"],
        results_wanted_per_site=10, hours_old=72,
        throttle_seconds=0.0, sleep=lambda s: None,
        exclude_title_keywords=["senior"], min_ctc_lpa=7,
        distiller=_Stub(), fingerprint="FP", prefer_roles={"devops": 1.0},
        exclude_when=[], fit_weight=0.70, comp_weight=0.30, top_ctc_lpa=40,
        max_distill_per_cohort=60, top_n_per_cohort=5,
    )
    result = svc.discover()
    m_ids = [j.posting.job_id for j in result["M"]]
    n_ids = [j.posting.job_id for j in result["N"]]
    assert m_ids == ["linkedin:1"]      # has INR salary
    assert n_ids == ["linkedin:2"]      # no salary
    assert result["M"][0].cohort == "M" and result["N"][0].cohort == "N"
    assert store.exists("linkedin:1") and store.exists("linkedin:2")  # persisted as discovered
    assert store.get_crux("linkedin:1") is not None                   # crux cached


def test_discover_drops_jobs_via_exclude_when():
    store = ApplicationStore(":memory:")
    rows = [_row("1", title="DevOps Engineer", description="night shift only role")]

    class _Stub:
        def generate_structured(self, prompt, *, schema, seed, max_output_tokens):
            return json.dumps({
                "job_id": "linkedin:1", "role_family": "devops", "seniority_signal": "junior",
                "min_years_required": 1, "max_years_required": 2, "work_mode": "onsite",
                "location_text": "X", "country": "india", "stated_salary": None,
                "tech_stack": [], "night_shift_only": True, "app_maintenance_focus": False,
                "company_type": "product", "red_flags": [], "applicant_instructions": None,
                "one_line": "x"})

        def generate(self, prompt, **kw):
            return "[]"

    svc = DiscoveryService(
        store=store, search_fn=lambda **k: rows,
        search_terms=["x"], locations=["Remote"], sites=["linkedin"],
        results_wanted_per_site=10, hours_old=72, throttle_seconds=0.0,
        sleep=lambda s: None, distiller=_Stub(), fingerprint="FP",
        prefer_roles={}, exclude_when=[{"field": "night_shift_only", "equals": True}],
        fit_weight=0.70, comp_weight=0.30, top_ctc_lpa=40, max_distill_per_cohort=60,
        top_n_per_cohort=5,
    )
    result = svc.discover()
    assert result["M"] == [] and result["N"] == []  # dropped by exclude_when


def test_prefilter_drops_naukri_experience_over_ceiling():
    store = ApplicationStore(":memory:")
    rows = [
        _row("1", site="naukri", title="DevOps Engineer", experience_range="6-9 Yrs"),
        _row("2", site="naukri", title="DevOps Engineer", experience_range="0-2 Yrs"),
        _row("3", site="naukri", title="DevOps Engineer", experience_range="competitive"),
    ]
    svc = _two_stage_service(store, lambda **k: rows, throttle_seconds=0.0,
                             sleep=lambda s: None, locations=["Remote"], yoe_ceiling=3,
                             naukri_search_fn=lambda **k: [])
    result = svc.discover()
    # 6-9 dropped (min 6 > 3); 0-2 kept; unparseable kept (never drop on absent fact)
    assert _all_ids(result) == {"naukri:2", "naukri:3"}


def test_prefilter_no_ceiling_keeps_high_experience():
    store = ApplicationStore(":memory:")
    rows = [_row("1", site="naukri", title="DevOps Engineer", experience_range="8-10 Yrs")]
    svc = _two_stage_service(store, lambda **k: rows, throttle_seconds=0.0,
                             sleep=lambda s: None, locations=["Remote"],
                             naukri_search_fn=lambda **k: [])  # no yoe_ceiling
    result = svc.discover()
    assert _all_ids(result) == {"naukri:1"}


def test_discover_logs_stage_timings(caplog):
    import logging
    store = ApplicationStore(":memory:")
    rows = [_row("1", title="DevOps Engineer", min_amount=2_000_000,
                 max_amount=2_300_000, currency="INR")]
    svc = _two_stage_service(store, lambda **k: rows, throttle_seconds=0.0,
                             sleep=lambda s: None, locations=["Remote"])
    with caplog.at_level(logging.INFO, logger="cvflow.discovery"):
        svc.discover()
    msgs = " ".join(r.message for r in caplog.records)
    assert "stage scrape" in msgs
    assert "stage prefilter" in msgs
    assert "cohort M" in msgs
    assert "discover total" in msgs


def test_prefilter_drops_internship_by_job_type():
    store = ApplicationStore(":memory:")
    rows = [
        _row("1", title="DevOps Engineer", job_type="internship"),
        _row("2", title="DevOps Engineer", job_type="fulltime"),
        _row("3", title="DevOps Engineer"),  # no job_type -> kept (never drop on absent)
    ]
    svc = _two_stage_service(store, lambda **k: rows, throttle_seconds=0.0,
                             sleep=lambda s: None, locations=["Remote"])
    result = svc.discover()
    assert _all_ids(result) == {"linkedin:2", "linkedin:3"}


def test_discover_partitions_M_from_crux_salary_not_structured():
    # JobSpy structured salary is empty, but the JD (crux) states INR pay -> must land in M.
    store = ApplicationStore(":memory:")
    rows = [
        _row("1", title="DevOps Engineer", description="pays 18 LPA"),     # crux: 18 LPA/yr -> M
        _row("2", title="DevOps Engineer", description="₹50k/month"),      # 6 LPA -> below floor
        _row("3", title="DevOps Engineer", description="no pay listed"),   # no salary -> N
    ]

    def _crux_json(jid, salary):
        return json.dumps({
            "job_id": jid, "role_family": "devops", "seniority_signal": "junior",
            "min_years_required": 1, "max_years_required": 2, "work_mode": "remote",
            "location_text": "Remote", "country": "india", "stated_salary": salary,
            "tech_stack": ["k8s"], "night_shift_only": False, "app_maintenance_focus": False,
            "company_type": "product", "red_flags": [], "applicant_instructions": None,
            "one_line": "infra"})

    class _Stub:
        def generate_structured(self, prompt, *, schema, seed, max_output_tokens):
            jid = re.search(r"job_id: (\S+)", prompt).group(1)
            sal = {
                "linkedin:1": {"min_amount": 1500000, "max_amount": 1800000, "currency": "INR",
                               "period": "year"},
                "linkedin:2": {"min_amount": 50000, "max_amount": 50000, "currency": "INR",
                               "period": "month"},
                "linkedin:3": None,
            }[jid]
            return _crux_json(jid, sal)

        def generate(self, prompt, **kw):
            ids = sorted(set(re.findall(r'"job_id": "([^"]+)"', prompt)))
            return json.dumps({"results": [{"job_id": i, "fit_score": 80, "fit_reason": "ok",
                                            "concern_codes": []} for i in ids]})

    svc = _two_stage_service(store, lambda **k: rows, throttle_seconds=0.0,
                             sleep=lambda s: None, locations=["Remote"], min_ctc_lpa=7,
                             distiller=_Stub())
    result = svc.discover()
    assert [j.posting.job_id for j in result["M"]] == ["linkedin:1"]   # 18 LPA -> M
    assert [j.posting.job_id for j in result["N"]] == ["linkedin:3"]   # no pay -> N
    assert result["M"][0].ctc_lpa == 18.0                              # crux salary used for comp
    # linkedin:2 (₹50k/mo = 6 LPA) dropped by the salary floor
    assert "linkedin:2" not in {j.posting.job_id for j in result["M"] + result["N"]}


def test_reconsider_discovered_reshows_discovered_but_not_acted_on():
    store = ApplicationStore(":memory:")
    store.add("linkedin:1", "Co", "Role", "https://x/1")  # stays 'discovered'
    store.add("linkedin:2", "Co", "Role", "https://x/2")
    store.set_status("linkedin:2", Status.SKIPPED)  # advanced past discovered -> still excluded

    rows = [_row("1"), _row("2"), _row("3")]
    svc = _two_stage_service(store, lambda **k: rows, throttle_seconds=0.0,
                             sleep=lambda s: None, locations=["Remote"],
                             reconsider_discovered=True)
    result = svc.discover()
    ids = _all_ids(result)
    assert "linkedin:1" in ids   # re-shown (was only 'discovered')
    assert "linkedin:2" not in ids  # acted on -> still deduped
    assert "linkedin:3" in ids   # brand new


def test_default_dedup_excludes_all_seen():
    store = ApplicationStore(":memory:")
    store.add("linkedin:1", "Co", "Role", "https://x/1")  # 'discovered'
    svc = _two_stage_service(store, lambda **k: [_row("1"), _row("2")], throttle_seconds=0.0,
                             sleep=lambda s: None, locations=["Remote"])  # default: not reconsider
    result = svc.discover()
    assert _all_ids(result) == {"linkedin:2"}


def _must_have_distiller(by_job):
    """Distiller stub emitting per-job must_have_skills (everything else neutral)."""
    class _Stub:
        def generate_structured(self, prompt, *, schema, seed, max_output_tokens):
            jid = re.search(r"job_id: (\S+)", prompt).group(1)
            return json.dumps({
                "job_id": jid, "role_family": "devops", "seniority_signal": "junior",
                "min_years_required": 1, "max_years_required": 2, "work_mode": "remote",
                "location_text": "Remote", "country": "india", "stated_salary": None,
                "tech_stack": ["k8s"], "must_have_skills": by_job[jid],
                "night_shift_only": False, "app_maintenance_focus": False,
                "company_type": "product", "red_flags": [], "applicant_instructions": None,
                "one_line": "infra"})

        def generate(self, prompt, **kw):
            ids = sorted(set(re.findall(r'"job_id": "([^"]+)"', prompt)))
            return json.dumps({"results": [{"job_id": i, "fit_score": 80, "fit_reason": "ok",
                                            "concern_codes": []} for i in ids]})
    return _Stub()


def test_must_have_skill_gate_drops_majority_missing():
    store = ApplicationStore(":memory:")
    rows = [_row("1"), _row("2")]
    distiller = _must_have_distiller({
        "linkedin:1": ["python", "go", "docker"],      # all covered -> kept
        "linkedin:2": ["llm", "openai", "kubernetes"],  # 3/3 missing -> dropped
    })
    svc = _two_stage_service(
        store, lambda **k: rows, throttle_seconds=0.0, sleep=lambda s: None,
        locations=["Remote"], distiller=distiller,
        candidate_skills=frozenset({"python", "go", "docker"}), skill_synonyms={},
    )
    assert _all_ids(svc.discover()) == {"linkedin:1"}


def test_must_have_gate_keeps_minority_missing_and_uses_synonyms():
    store = ApplicationStore(":memory:")
    rows = [_row("1")]
    # golang->go (have), 1 of 5 missing (terraform) -> 20% -> kept
    distiller = _must_have_distiller(
        {"linkedin:1": ["golang", "python", "docker", "kafka", "terraform"]}
    )
    svc = _two_stage_service(
        store, lambda **k: rows, throttle_seconds=0.0, sleep=lambda s: None,
        locations=["Remote"],
        candidate_skills=frozenset({"go", "python", "docker", "kafka"}),
        skill_synonyms={"golang": "go"}, distiller=distiller,
    )
    assert _all_ids(svc.discover()) == {"linkedin:1"}


def test_must_have_gate_disabled_without_candidate_skills():
    store = ApplicationStore(":memory:")
    distiller = _must_have_distiller({"linkedin:1": ["llm", "openai", "rust"]})
    svc = _two_stage_service(  # no candidate_skills -> gate is a no-op
        store, lambda **k: [_row("1")], throttle_seconds=0.0, sleep=lambda s: None,
        locations=["Remote"], distiller=distiller,
    )
    assert _all_ids(svc.discover()) == {"linkedin:1"}


def test_discover_reports_drop_tally_by_bucket():
    store = ApplicationStore(":memory:")
    store.add("linkedin:9", "Co", "Seen", "https://x/9")  # already-seen -> dedup bucket

    class _Stub:
        def generate_structured(self, prompt, *, schema, seed, max_output_tokens):
            jid = re.search(r"job_id: (\S+)", prompt).group(1)
            sen = {"linkedin:2": "senior"}.get(jid, "junior")
            must = {"linkedin:3": ["rust", "scala", "elixir"]}.get(jid, [])
            return json.dumps({
                "job_id": jid, "role_family": "devops", "seniority_signal": sen,
                "min_years_required": 1, "max_years_required": 2, "work_mode": "remote",
                "location_text": "Remote", "country": "india", "stated_salary": None,
                "tech_stack": ["k8s"], "must_have_skills": must, "night_shift_only": False,
                "app_maintenance_focus": False, "company_type": "product", "red_flags": [],
                "applicant_instructions": None, "one_line": "x"})

        def generate(self, prompt, **kw):
            ids = sorted(set(re.findall(r'"job_id": "([^"]+)"', prompt)))
            return json.dumps({"results": [{"job_id": i, "fit_score": 80, "fit_reason": "ok",
                                            "concern_codes": []} for i in ids]})

    rows = [_row("1"), _row("2"), _row("3"), _row("9")]
    svc = _two_stage_service(
        store, lambda **k: rows, throttle_seconds=0.0, sleep=lambda s: None,
        locations=["Remote"], distiller=_Stub(),
        candidate_skills=frozenset({"python", "go", "docker"}), skill_synonyms={},
        exclude_when=[{"field": "seniority_signal", "contains_any": ["senior", "lead"]}],
    )
    result = svc.discover()
    assert _all_ids(result) == {"linkedin:1"}
    assert result["_dropped"] == {"too senior": 1, "wrong stack": 1, "already seen": 1}


def test_naukri_fetched_via_adapter_and_excluded_from_jobspy():
    store = ApplicationStore(":memory:")
    captured = {}

    def search_fn(**kwargs):
        captured["site_name"] = kwargs["site_name"]
        return [_row("j1", site="linkedin")]

    def naukri_fn(**kwargs):
        captured["naukri_called"] = True
        return [_row("n1", site="naukri")]

    svc = _two_stage_service(
        store, search_fn, throttle_seconds=0.0, sleep=lambda s: None,
        locations=["Remote"], sites=["linkedin", "naukri"], naukri_search_fn=naukri_fn,
    )
    result = svc.discover()
    ids = _all_ids(result)
    assert "naukri:n1" in ids and "linkedin:j1" in ids   # both merged + ranked
    assert captured["naukri_called"] is True
    assert "naukri" not in captured["site_name"]          # JobSpy never asked for naukri


def test_discover_emits_progress_messages_in_order():
    store = ApplicationStore(":memory:")
    rows = [_row("1"), _row("2", site="indeed")]
    svc = _two_stage_service(store, lambda **k: rows)
    msgs: list[str] = []
    svc.discover(progress=msgs.append)
    joined = "\n".join(msgs)
    assert any(m.startswith("📡 Scraped") for m in msgs)
    assert any("candidates after prefilter" in m for m in msgs)
    assert any(m.startswith("🧪 Distilled") for m in msgs)
    assert any(m.startswith("📊 Cohorts") for m in msgs)
    assert joined.index("📡") < joined.index("🧹") < joined.index("🧪") < joined.index("📊")


def test_discover_collects_drop_records_with_bucket_and_detail():
    store = ApplicationStore(":memory:")
    rows = [_row("1"), _row("2", title="Senior Engineer")]  # row 2 dropped at prefilter
    svc = _two_stage_service(store, lambda **k: rows)
    result = svc.discover()
    records = result["_drop_records"]
    rec = next(r for r in records if r.bucket == "other filter")
    assert "@" in rec.label and rec.detail
    from collections import Counter
    by_bucket = Counter(r.bucket for r in records if r.bucket != "capped")
    for bucket, n in by_bucket.items():
        assert result["_dropped"][bucket] == n


def test_persist_stage_writes_meta_and_jd_text():
    """After discover(), the persisted Application carries fit/cohort/fit_reason from the
    BenchmarkedJob and get_jd_text() returns the posting's description."""
    store = ApplicationStore(":memory:")
    rows = [_row("42", description="the scraped JD description")]
    svc = _two_stage_service(store, lambda **k: rows, throttle_seconds=0.0,
                             sleep=lambda s: None, locations=["Remote"])
    svc.discover(progress=lambda _m: None)
    job_id = "linkedin:42"
    app = store.get(job_id)
    assert app is not None, "job should have been persisted"
    assert app.fit_score == 80
    assert app.fit_reason == "ok"
    assert app.cohort == "N"  # no stated salary -> N
    assert store.get_jd_text(job_id) == "the scraped JD description"


# --- posting-date persistence (W2 change) ---


def test_discover_persists_posting_date_raw_and_parsed():
    store = ApplicationStore(":memory:")
    rows = [
        _row("1", date_posted="2026-06-02"),   # ISO -> stored verbatim + parsed
        _row("2", date_posted="5 days ago"),    # relative -> parsed against today
    ]
    svc = _two_stage_service(store, lambda **k: rows, throttle_seconds=0.0,
                             sleep=lambda s: None, locations=["Remote"],
                             today=lambda: date(2026, 6, 25))
    svc.discover()
    a = store.get("linkedin:1")
    assert a.date_posted == "2026-06-02"
    assert a.date_posted_parsed == "2026-06-02"
    b = store.get("linkedin:2")
    assert b.date_posted == "5 days ago"
    assert b.date_posted_parsed == "2026-06-20"  # 25th minus 5 days


def test_discover_unparseable_posting_date_persists_raw_with_null_parsed():
    store = ApplicationStore(":memory:")
    rows = [_row("1", date_posted="whenever")]
    svc = _two_stage_service(store, lambda **k: rows, throttle_seconds=0.0,
                             sleep=lambda s: None, locations=["Remote"],
                             today=lambda: date(2026, 6, 25))
    svc.discover()
    a = store.get("linkedin:1")
    assert a.date_posted == "whenever"
    assert a.date_posted_parsed is None  # never guessed


# --- blocked-source notice (W2 change) ---


def test_discover_surfaces_naukri_unreachable_notice():
    store = ApplicationStore(":memory:")

    def naukri_fn(**kwargs):
        raise NaukriUnreachable("datacenter IP blocked")

    svc = _two_stage_service(
        store, lambda **k: [_row("j1", site="linkedin")], throttle_seconds=0.0,
        sleep=lambda s: None, locations=["Remote"], sites=["linkedin", "naukri"],
        naukri_search_fn=naukri_fn,
    )
    result = svc.discover()
    assert result["_notices"] == ["Naukri unreachable today"]
    assert "linkedin:j1" in _all_ids(result)  # the reachable source still ranks


def test_discover_no_notice_when_naukri_reachable():
    store = ApplicationStore(":memory:")
    svc = _two_stage_service(
        store, lambda **k: [_row("j1", site="linkedin")], throttle_seconds=0.0,
        sleep=lambda s: None, locations=["Remote"], sites=["linkedin", "naukri"],
        naukri_search_fn=lambda **k: [_row("n1", site="naukri")],
    )
    assert svc.discover()["_notices"] == []


def test_discover_no_notice_when_naukri_reachable_but_empty():
    # Reaching Naukri and getting zero jobs is not "unreachable" — no notice.
    store = ApplicationStore(":memory:")
    svc = _two_stage_service(
        store, lambda **k: [_row("j1", site="linkedin")], throttle_seconds=0.0,
        sleep=lambda s: None, locations=["Remote"], sites=["linkedin", "naukri"],
        naukri_search_fn=lambda **k: [],
    )
    assert svc.discover()["_notices"] == []


def test_discover_no_notice_when_naukri_disabled():
    store = ApplicationStore(":memory:")
    svc = _two_stage_service(store, lambda **k: [_row("1")], throttle_seconds=0.0,
                             sleep=lambda s: None, locations=["Remote"], sites=["linkedin"])
    assert svc.discover()["_notices"] == []
