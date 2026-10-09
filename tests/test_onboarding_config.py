from pathlib import Path
from typing import Any

import pytest
from tests.test_config import _valid, _write

from cvflow.config import ConfigError, FrontierConfig, load_config


def _load(tmp_path: Path, onboarding: Any = None) -> Any:
    data = _valid()
    if onboarding is not None:
        data["onboarding"] = onboarding
    return load_config(_write(tmp_path, data)).onboarding


def test_absent_block_defaults(tmp_path: Path) -> None:
    ob = _load(tmp_path)
    assert ob.mode == "auto"
    assert ob.frontier is None
    assert ob.max_compile_attempts == 3
    assert ob.staging_dir == "data/onboarding"


def test_blank_frontier_key_is_none(tmp_path: Path) -> None:
    ob = _load(tmp_path, {"frontier": {"provider": "", "api_key": "  ", "base_url": "",
                                       "model": ""}})
    assert ob.frontier is None


def test_filled_frontier(tmp_path: Path) -> None:
    ob = _load(tmp_path, {"mode": "manual", "frontier": {
        "provider": "openrouter", "api_key": "k", "base_url": "https://x/v1", "model": "m"}})
    assert ob.mode == "manual"
    assert ob.frontier == FrontierConfig("openrouter", "k", "https://x/v1", "m")


def test_filled_key_requires_other_fields(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _load(tmp_path, {"frontier": {"api_key": "k"}})


def test_bad_mode(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _load(tmp_path, {"mode": "bogus"})
