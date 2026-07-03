"""Wiring tests: the PTB application, the composition root, and the headless CLI."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from telegram.ext import CallbackQueryHandler, MessageHandler

from cvflow import cli
from cvflow.app.bot import build_application
from cvflow.app.commands import Services
from cvflow.app.main import build_services
from cvflow.app.notify import TelegramNotifier
from cvflow.config import load_config
from cvflow.runs import DiscoverOutcome
from cvflow.storage import ApplicationStore

FIXTURE_RESUME = Path(__file__).parent / "fixtures" / "resume"


def _fake_services(**overrides: Any) -> Services:
    base: dict[str, Any] = {
        "store": ApplicationStore(":memory:"),
        "notify": lambda _m: None,
        "discover": lambda: DiscoverOutcome("digest text", ["j1"], 0),
        "tailor": lambda job_id: {"job_id": job_id, "role": "R", "company": "C",
                                  "pdf_path": "x.pdf", "diff": "the diff"},
        "authorized_user_id": 123,
    }
    base.update(overrides)
    return Services(**base)


# --- bot application ---


def test_build_application_registers_handlers_and_services() -> None:
    services = _fake_services()
    app = build_application("123:ABC", services)
    assert app.bot_data["services"] is services
    kinds = [type(h) for h in app.handlers[0]]
    assert MessageHandler in kinds
    assert CallbackQueryHandler in kinds


# --- composition root ---


def _config_file(tmp_path: Path) -> Path:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "form_fields.json").write_text(json.dumps({"full_name": "Jordan Lee"}))
    data = {
        "telegram": {"bot_token": "123:ABC", "authorized_user_id": 123},
        "schedule": {
            "daily_discovery_time": "08:00",
            "timezone": "Asia/Kolkata",
            "heartbeat_interval_minutes": 720,
        },
        "discovery": {
            "search_terms": ["devops"],
            "locations": ["Remote"],
            "sites": ["linkedin"],
            "results_wanted_per_site": 5,
            "hours_old": 48,
            "country_indeed": "india",
            "linkedin_fetch_description": False,
            "max_distill_per_cohort": 10,
            "top_n_per_cohort": 5,
            "reconsider_discovered": False,
            "log_dir": str(tmp_path / "logs"),
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
            "exclude_when": [],
        },
        "llm": {
            "distillation": {
                "provider": "mistral",
                "api_key": "k",
                "base_url": "https://api.mistral.ai/v1",
                "model": "m",
                "max_requests_per_minute": 60,
            },
            "tailoring": {
                "provider": "cerebras",
                "api_key": "k",
                "base_url": "https://api.cerebras.ai/v1",
                "model": "c",
                "max_requests_per_minute": 5,
            },
        },
        "resume": {
            "master_tex_path": str(FIXTURE_RESUME / "master.tex"),
            "output_dir": str(tmp_path / "resumes"),
            "latex_compiler": "tectonic",
        },
        "storage": {
            "db_path": str(tmp_path / "db.sqlite"),
            "form_fields_path": str(profile / "form_fields.json"),
        },
        "profile": {"knowledge_base_dir": str(profile)},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def test_build_services_composes_without_touching_the_network(tmp_path: Path) -> None:
    services = build_services(load_config(_config_file(tmp_path)))
    assert isinstance(services.store, ApplicationStore)
    assert isinstance(services.notify, TelegramNotifier)
    assert callable(services.discover) and callable(services.tailor)
    assert services.authorized_user_id == 123


# --- cli ---


def test_cli_heartbeat_prints_counts() -> None:
    services = _fake_services()
    services.store.add("j", "Acme", "Dev", "https://example.test/j")
    out: list[str] = []
    cli.main(["heartbeat"], services=services, echo=out.append)
    assert out and "discovered=1" in out[0]


def test_cli_discover_echoes_the_digest() -> None:
    out: list[str] = []
    cli.main(["discover"], services=_fake_services(), echo=out.append)
    assert any("digest text" in line for line in out)


def test_cli_tailor_echoes_diff_and_pdf_path() -> None:
    out: list[str] = []
    cli.main(["tailor", "j1"], services=_fake_services(), echo=out.append)
    joined = "\n".join(out)
    assert "the diff" in joined and "x.pdf" in joined


def test_cli_unknown_command_exits() -> None:
    with pytest.raises(SystemExit):
        cli.main(["frobnicate"], services=_fake_services(), echo=lambda _m: None)
