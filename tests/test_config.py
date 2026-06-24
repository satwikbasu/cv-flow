from pathlib import Path
from typing import Any

import pytest
import yaml

from cvflow.config import ConfigError, load_config


def _valid() -> dict[str, Any]:
    return {
        "telegram": {"bot_token": "t", "authorized_user_id": 123},
        "schedule": {
            "daily_discovery_time": "08:00",
            "timezone": "Asia/Kolkata",
            "heartbeat_interval_minutes": 720,
        },
        "discovery": {
            "search_terms": ["devops"],
            "locations": ["Remote"],
            "sites": ["linkedin"],
            "results_wanted_per_site": 25,
            "hours_old": 48,
            "country_indeed": "india",
            "linkedin_fetch_description": True,
            "max_distill_per_cohort": 200,
            "top_n_per_cohort": 5,
            "reconsider_discovered": False,
        },
        "preferences": {
            "yoe_have": 2,
            "min_ctc_lpa": 7,
            "exclude_title_keywords": ["senior"],
            "yoe_buffer": 1,
            "top_ctc_lpa": 20,
            "fit_weight": 0.7,
            "comp_weight": 0.3,
            "prefer_roles": {"devops": 0.9},
            "exclude_when": [{"field": "country", "equals": "other"}],
        },
        "llm": {
            "distillation": {
                "provider": "mistral",
                "api_key": "k",
                "base_url": "https://api.mistral.ai/v1",
                "model": "mistral-small-2506",
                "max_requests_per_minute": 60,
            },
            "tailoring": {
                "provider": "cerebras",
                "api_key": "k",
                "base_url": "https://api.cerebras.ai/v1",
                "model": "gpt-oss-120b",
                "max_requests_per_minute": 5,
            },
        },
        "resume": {
            "master_tex_path": "resume/master.tex",
            "output_dir": "data/resumes",
            "latex_compiler": "tectonic",
        },
        "storage": {"db_path": "data/cvflow.db", "form_fields_path": "profile/form_fields.json"},
        "profile": {"knowledge_base_dir": "profile"},
    }


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def test_loads_valid_config(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _valid()))
    assert cfg.telegram.authorized_user_id == 123
    assert cfg.llm.distillation.model == "mistral-small-2506"
    assert cfg.llm.tailoring.provider == "cerebras"


def test_public_defaults(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _valid()))
    assert cfg.discovery.retention_days == 21
    assert cfg.resume.rephrase is False
    assert cfg.resume.max_projects == 2
    assert cfg.resume.use_jd_analysis is False
    assert cfg.discovery.drop_summary_provider == "distillation"


def test_missing_section_raises(tmp_path: Path) -> None:
    data = _valid()
    del data["llm"]
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, data))


def test_fit_comp_must_sum_to_one(tmp_path: Path) -> None:
    data = _valid()
    data["preferences"]["fit_weight"] = 0.5
    data["preferences"]["comp_weight"] = 0.3
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, data))


def test_bad_time_format_raises(tmp_path: Path) -> None:
    data = _valid()
    data["schedule"]["daily_discovery_time"] = "8am"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, data))


def test_disabled_sections_parsed(tmp_path: Path) -> None:
    data = _valid()
    data["resume"]["sections"] = {"certifications": False, "skills": True}
    cfg = load_config(_write(tmp_path, data))
    assert "certifications" in cfg.resume.disabled_sections
    assert "skills" not in cfg.resume.disabled_sections


def test_no_automation_auth_security(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _valid()))
    assert not hasattr(cfg, "automation")
    assert not hasattr(cfg, "auth")
    assert not hasattr(cfg, "security")


def test_missing_file_raises() -> None:
    with pytest.raises(ConfigError):
        load_config("/no/such/config.yaml")
