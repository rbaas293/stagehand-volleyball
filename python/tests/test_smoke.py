"""
Smoke tests for the Python scraper packaging surface.

These do not launch a browser or call Stagehand's LLM APIs. They only check that:
  - the main module imports cleanly (packaging / install wiring),
  - config.json at the repo root loads and validates,
  - AppConfig rejects empty captain+team the same way as production.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

# Repo root = parent of python/ (this file lives in python/tests/).
REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON_DIR = Path(__file__).resolve().parents[1]


def test_main_module_imports() -> None:
    """Console-script target `main:main` must be importable after install."""
    import main

    assert callable(main.main)
    assert callable(main.load_config)
    assert hasattr(main, "AppConfig")


def test_load_config_from_repo_root() -> None:
    """Shared ../config.json (preferred path) must validate into AppConfig."""
    import main

    config_path = REPO_ROOT / "config.json"
    assert config_path.is_file(), f"expected shared config at {config_path}"

    # load_config() resolves PARENT_CONFIG when that file exists.
    cfg = main.load_config()
    assert cfg.day
    assert cfg.league
    assert cfg.league_url.startswith("http")
    # At least one of captain/team is required by the model validator.
    assert cfg.captain_name.strip() or cfg.team_name.strip()


def test_app_config_requires_captain_or_team() -> None:
    """Empty captainName + teamName must fail validation (mirrors TS superRefine)."""
    import main

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
    """JSON uses camelCase; pydantic aliases must still populate snake_case attrs."""
    import main

    cfg = main.AppConfig.model_validate(
        {
            "captainName": "H. Robinson",
            "teamName": "",
            "day": "Sunday",
            "league": "Test League",
            "leagueUrl": "https://example.com/division/1",
            "schedulePathSuffix": "/schedule",
        }
    )
    assert cfg.captain_name == "H. Robinson"
    assert cfg.league_url.endswith("/1")
    assert cfg.schedule_path_suffix == "/schedule"


def test_config_json_is_valid_json() -> None:
    """Sanity: repo-root config.json parses (same file CI artifacts assume)."""
    raw = (REPO_ROOT / "config.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert isinstance(data, dict)
    assert "leagueUrl" in data
