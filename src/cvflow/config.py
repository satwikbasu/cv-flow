"""Typed loading and validation of ``config.yaml``.

The config holds the user's secrets and tuning (it is gitignored). This module turns it into a
frozen, typed :class:`Config` and fails loudly — never silently — on any missing key, wrong type,
or malformed value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ConfigError(Exception):
    """Raised when config.yaml is missing, malformed, or fails validation."""


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    authorized_user_id: int


@dataclass(frozen=True)
class ScheduleConfig:
    daily_discovery_time: str
    timezone: str
    heartbeat_interval_minutes: int


@dataclass(frozen=True)
class DiscoveryConfig:
    search_terms: list[str]
    locations: list[str]
    sites: list[str]
    results_wanted_per_site: int
    hours_old: int
    country_indeed: str
    linkedin_fetch_description: bool
    max_distill_per_cohort: int
    top_n_per_cohort: int
    reconsider_discovered: bool
    retention_days: int = 21
    log_dir: str = "logs/discover"
    summarize_drops: bool = True
    drop_summary_provider: str = "distillation"
    drop_summary_max_chars: int = 700
    drop_summary_samples_per_bucket: int = 3


@dataclass(frozen=True)
class PreferencesConfig:
    yoe_have: int
    min_ctc_lpa: int
    exclude_title_keywords: list[str]
    yoe_buffer: int
    top_ctc_lpa: int
    fit_weight: float
    comp_weight: float
    prefer_roles: dict[str, float]
    exclude_when: list[dict[str, Any]]


@dataclass(frozen=True)
class ProviderConfig:
    """One OpenAI-compatible chat provider (distillation or tailoring)."""

    provider: str
    api_key: str
    base_url: str
    model: str
    max_requests_per_minute: int
    # Arbitrary extra JSON merged into every chat request (e.g. reasoning_effort).
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMConfig:
    distillation: ProviderConfig
    tailoring: ProviderConfig


@dataclass(frozen=True)
class ResumeConfig:
    master_tex_path: str
    output_dir: str
    latex_compiler: str
    use_jd_analysis: bool = False
    max_projects: int = 2
    rephrase: bool = False
    disabled_sections: frozenset[str] = frozenset()


@dataclass(frozen=True)
class StorageConfig:
    db_path: str
    form_fields_path: str


@dataclass(frozen=True)
class ProfileConfig:
    knowledge_base_dir: str


@dataclass(frozen=True)
class Config:
    telegram: TelegramConfig
    schedule: ScheduleConfig
    discovery: DiscoveryConfig
    preferences: PreferencesConfig
    llm: LLMConfig
    resume: ResumeConfig
    storage: StorageConfig
    profile: ProfileConfig


def _section(data: dict[str, Any], key: str, path: str) -> dict[str, Any]:
    if key not in data:
        raise ConfigError(f"missing required section: {path}{key}")
    value = data[key]
    if not isinstance(value, dict):
        raise ConfigError(f"section {path}{key} must be a mapping, got {type(value).__name__}")
    return value


def _get(data: dict[str, Any], key: str, typ: type, path: str) -> Any:
    if key not in data:
        raise ConfigError(f"missing required key: {path}{key}")
    value = data[key]
    # bool is a subclass of int; guard against silent coercion both ways.
    if typ is int and isinstance(value, bool):
        raise ConfigError(f"key {path}{key} must be int, got bool")
    if typ is not bool and isinstance(value, bool):
        raise ConfigError(f"key {path}{key} must be {typ.__name__}, got bool")
    if not isinstance(value, typ):
        raise ConfigError(f"key {path}{key} must be {typ.__name__}, got {type(value).__name__}")
    return value


def _get_str(data: dict[str, Any], key: str, path: str) -> str:
    return str(_get(data, key, str, path))


def _get_int(data: dict[str, Any], key: str, path: str) -> int:
    return int(_get(data, key, int, path))


def _get_bool(data: dict[str, Any], key: str, path: str) -> bool:
    return bool(_get(data, key, bool, path))


def _get_str_list(data: dict[str, Any], key: str, path: str) -> list[str]:
    value = _get(data, key, list, path)
    if not all(isinstance(item, str) for item in value):
        raise ConfigError(f"key {path}{key} must be a list of strings")
    return list(value)


def _opt_str(data: dict[str, Any], key: str, path: str, default: str) -> str:
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, str):
        raise ConfigError(f"key {path}{key} must be str, got {type(value).__name__}")
    return str(value)


def _opt_int(data: dict[str, Any], key: str, path: str, default: int) -> int:
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"key {path}{key} must be int, got {type(value).__name__}")
    return int(value)


def _opt_bool(data: dict[str, Any], key: str, path: str, default: bool) -> bool:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, bool):
        raise ConfigError(f"key {path}{key} must be bool, got {type(value).__name__}")
    return bool(value)


def _get_float(data: dict[str, Any], key: str, path: str) -> float:
    if key not in data:
        raise ConfigError(f"missing required key: {path}{key}")
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"key {path}{key} must be a number")
    return float(value)


def _get_role_map(data: dict[str, Any], key: str, path: str) -> dict[str, float]:
    value = _get(data, key, dict, path)
    out: dict[str, float] = {}
    for k, v in value.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ConfigError(f"key {path}{key}.{k} must be a number")
        out[str(k)] = float(v)
    return out


def _get_rule_list(data: dict[str, Any], key: str, path: str) -> list[dict[str, Any]]:
    value = _get(data, key, list, path)
    rules: list[dict[str, Any]] = []
    for i, item in enumerate(value):
        if not isinstance(item, dict) or "field" not in item:
            raise ConfigError(f"{path}{key}[{i}] must be a mapping with a 'field' key")
        rules.append(dict(item))
    return rules


def _provider_config(section: dict[str, Any], path: str) -> ProviderConfig:
    extra_body = section.get("extra_body", {})
    if not isinstance(extra_body, dict):
        raise ConfigError(f"key {path}extra_body must be a mapping")
    return ProviderConfig(
        provider=_get_str(section, "provider", path),
        api_key=_get_str(section, "api_key", path).strip(),
        base_url=_get_str(section, "base_url", path),
        model=_get_str(section, "model", path),
        max_requests_per_minute=_get_int(section, "max_requests_per_minute", path),
        extra_body=dict(extra_body),
    )


def load_config(path: str | Path) -> Config:
    """Load, validate, and return the typed config at ``path``.

    Raises :class:`ConfigError` on a missing file, malformed YAML, or any missing, mistyped, or
    invalid field.
    """
    path = Path(path)
    try:
        raw = path.read_text()
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")

    tg = _section(data, "telegram", "")
    sch = _section(data, "schedule", "")
    disc = _section(data, "discovery", "")
    pref = _section(data, "preferences", "")
    llm = _section(data, "llm", "")
    distill = _section(llm, "distillation", "llm.")
    tail = _section(llm, "tailoring", "llm.")
    res = _section(data, "resume", "")
    store = _section(data, "storage", "")
    prof = _section(data, "profile", "")

    daily_time = _get_str(sch, "daily_discovery_time", "schedule.")
    if not _TIME_RE.match(daily_time):
        raise ConfigError(f"schedule.daily_discovery_time must be HH:MM, got {daily_time!r}")

    fit_w = _get_float(pref, "fit_weight", "preferences.")
    comp_w = _get_float(pref, "comp_weight", "preferences.")
    if abs(fit_w + comp_w - 1.0) > 1e-6:
        raise ConfigError(
            f"preferences.fit_weight + comp_weight must sum to 1.0, got {fit_w + comp_w}"
        )

    return Config(
        telegram=TelegramConfig(
            bot_token=_get_str(tg, "bot_token", "telegram.").strip(),
            authorized_user_id=_get_int(tg, "authorized_user_id", "telegram."),
        ),
        schedule=ScheduleConfig(
            daily_discovery_time=daily_time,
            timezone=_get_str(sch, "timezone", "schedule."),
            heartbeat_interval_minutes=_get_int(sch, "heartbeat_interval_minutes", "schedule."),
        ),
        discovery=DiscoveryConfig(
            search_terms=_get_str_list(disc, "search_terms", "discovery."),
            locations=_get_str_list(disc, "locations", "discovery."),
            sites=_get_str_list(disc, "sites", "discovery."),
            results_wanted_per_site=_get_int(disc, "results_wanted_per_site", "discovery."),
            hours_old=_get_int(disc, "hours_old", "discovery."),
            country_indeed=_get_str(disc, "country_indeed", "discovery."),
            linkedin_fetch_description=_get_bool(
                disc, "linkedin_fetch_description", "discovery."
            ),
            max_distill_per_cohort=_get_int(disc, "max_distill_per_cohort", "discovery."),
            top_n_per_cohort=_get_int(disc, "top_n_per_cohort", "discovery."),
            reconsider_discovered=_get_bool(disc, "reconsider_discovered", "discovery."),
            retention_days=_opt_int(disc, "retention_days", "discovery.", 21),
            log_dir=_opt_str(disc, "log_dir", "discovery.", "logs/discover"),
            summarize_drops=_opt_bool(disc, "summarize_drops", "discovery.", True),
            drop_summary_provider=_opt_str(
                disc, "drop_summary_provider", "discovery.", "distillation"
            ),
            drop_summary_max_chars=_opt_int(disc, "drop_summary_max_chars", "discovery.", 700),
            drop_summary_samples_per_bucket=_opt_int(
                disc, "drop_summary_samples_per_bucket", "discovery.", 3
            ),
        ),
        preferences=PreferencesConfig(
            yoe_have=_get_int(pref, "yoe_have", "preferences."),
            min_ctc_lpa=_get_int(pref, "min_ctc_lpa", "preferences."),
            exclude_title_keywords=_get_str_list(
                pref, "exclude_title_keywords", "preferences."
            ),
            yoe_buffer=_get_int(pref, "yoe_buffer", "preferences."),
            top_ctc_lpa=_get_int(pref, "top_ctc_lpa", "preferences."),
            fit_weight=fit_w,
            comp_weight=comp_w,
            prefer_roles=_get_role_map(pref, "prefer_roles", "preferences."),
            exclude_when=_get_rule_list(pref, "exclude_when", "preferences."),
        ),
        llm=LLMConfig(
            distillation=_provider_config(distill, "llm.distillation."),
            tailoring=_provider_config(tail, "llm.tailoring."),
        ),
        resume=ResumeConfig(
            master_tex_path=_get_str(res, "master_tex_path", "resume."),
            output_dir=_get_str(res, "output_dir", "resume."),
            latex_compiler=_get_str(res, "latex_compiler", "resume."),
            use_jd_analysis=_opt_bool(res, "use_jd_analysis", "resume.", False),
            max_projects=_opt_int(res, "max_projects", "resume.", 2),
            rephrase=_opt_bool(res, "rephrase", "resume.", False),
            disabled_sections=frozenset(
                name for name, shown in (res.get("sections") or {}).items() if shown is False
            ),
        ),
        storage=StorageConfig(
            db_path=_get_str(store, "db_path", "storage."),
            form_fields_path=_get_str(store, "form_fields_path", "storage."),
        ),
        profile=ProfileConfig(
            knowledge_base_dir=_get_str(prof, "knowledge_base_dir", "profile."),
        ),
    )
