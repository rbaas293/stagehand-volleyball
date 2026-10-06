"""
Smoke tests for packaging and config loading (no live network / browser).

These check that:
  - the main module (and helpers) import cleanly after install,
  - config.example.yaml at the repo root parses and validates (config.yaml is
    gitignored and not present in CI),
  - AppConfig accepts captainName as a string or list, and rejects empty ones.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

# Repo root = parent of python/ (this file lives in python/tests/).
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config.example.yaml"


def test_main_module_imports() -> None:
    """Console-script target `main:main` must be importable after install."""
    import lean_api
    import main
    import token_usage

    assert callable(main.main)
    assert callable(main.load_config)
    assert hasattr(main, "AppConfig")
    assert hasattr(lean_api, "_http_get_json")
    assert hasattr(token_usage, "USAGE")


def test_load_config_from_example_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Shared config.example.yaml must validate into AppConfig.

    Production load_config() only reads config.yaml (gitignored). In CI we
    point CONFIG_PATH at the tracked example so packaging still verifies the
    real loader path without committing personal config.
    """
    import main

    assert EXAMPLE_CONFIG.is_file(), f"expected example config at {EXAMPLE_CONFIG}"
    monkeypatch.setattr(main, "CONFIG_PATH", EXAMPLE_CONFIG)

    cfg = main.load_config()
    assert isinstance(cfg.captain_name, list)
    assert any(name.strip() for name in cfg.captain_name) or cfg.team_name.strip()
    assert cfg.mode in ("lean", "llm")
    # Example should have levels and/or a leagueUrl so the target validator passes.
    assert cfg.levels or cfg.league_url.startswith("http")


def test_app_config_requires_captain_or_team() -> None:
    """Empty captainName + teamName must fail validation."""
    import main

    with pytest.raises(ValidationError):
        main.AppConfig.model_validate(
            {
                "captainName": [],
                "teamName": "",
                "day": "Sunday",
                "league": "Test League",
                "leagueUrl": "https://example.com/division/1",
            }
        )

    with pytest.raises(ValidationError):
        main.AppConfig.model_validate(
            {
                "captainName": "",
                "teamName": "",
                "day": "Sunday",
                "league": "Test League",
                "leagueUrl": "https://example.com/division/1",
            }
        )


def test_app_config_accepts_camel_case_aliases() -> None:
    """YAML uses camelCase; string or list captainName coerce to list[str]."""
    import main

    as_string = main.AppConfig.model_validate(
        {
            "captainName": "H. Robinson",
            "teamName": "",
            "day": "Sunday",
            "league": "Test League",
            "leagueUrl": "https://example.com/division/1",
            "schedulePathSuffix": "/schedule",
        }
    )
    assert as_string.captain_name == ["H. Robinson"]
    assert as_string.league_url.endswith("/1")
    assert as_string.schedule_path_suffix == "/schedule"

    as_list = main.AppConfig.model_validate(
        {
            "captainName": ["H. Robinson", "R. Baas"],
            "teamName": "",
            "day": "Sunday",
            "league": "Test League",
            "leagueUrl": "https://example.com/division/1",
        }
    )
    assert as_list.captain_name == ["H. Robinson", "R. Baas"]


def test_config_example_yaml_is_valid_yaml() -> None:
    """Sanity: tracked config.example.yaml parses as a mapping."""
    raw = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    assert isinstance(data, dict)
    assert "captainName" in data or "teamName" in data
