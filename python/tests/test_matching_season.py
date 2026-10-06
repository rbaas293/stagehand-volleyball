"""Unit tests for captain/team matching and season selection (PR #4 Majors)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lean_api import LeanApiError, pick_current_season
from main import (
    ResolveError,
    StandingsRow,
    captain_matches,
    normalize_captain,
    resolve_teams_from_standings,
)


def test_normalize_captain_strips_punct_and_case():
    assert normalize_captain("R. Baas") == "r baas"
    assert normalize_captain("R Baas") == "r baas"
    assert normalize_captain("  H. Robinson ") == "h robinson"


def test_captain_matches_exact_tokens_only():
    assert captain_matches("R. Baas", "R Baas")
    assert captain_matches("H. Robinson", "H Robinson")
    assert not captain_matches("H. Robinson", "Robinson")  # no substring
    assert not captain_matches("R. Baas", "Ryan Baas")
    assert not captain_matches("Ryan Baas", "R. Baas")
    assert not captain_matches(None, "R. Baas")
    assert not captain_matches("R. Baas", "")


def test_team_name_exact_only_no_substring():
    rows = [
        StandingsRow(teamName="Win or Lose We Booze", captainName="H. Robinson"),
        StandingsRow(teamName="Booze Cruise", captainName="Someone"),
    ]
    matched, res, _ = resolve_teams_from_standings(
        rows, use_captain=False, captain_names=[], config_team_name="Win or Lose We Booze"
    )
    assert res == "teamName"
    assert len(matched) == 1
    assert matched[0].team_name == "Win or Lose We Booze"

    with pytest.raises(ResolveError, match="exact"):
        resolve_teams_from_standings(
            rows, use_captain=False, captain_names=[], config_team_name="Booze"
        )


def test_pick_current_season_in_range_deterministic():
    seasons = [
        {
            "uid": "a",
            "name": "Old",
            "startDate": "2025-01-01T00:00:00",
            "endDate": "2025-06-01T00:00:00",
        },
        {
            "uid": "b",
            "name": "Current",
            "startDate": "2026-07-01T00:00:00",
            "endDate": "2026-12-01T00:00:00",
        },
        {
            "uid": "c",
            "name": "OverlapLater",
            "startDate": "2026-08-01T00:00:00",
            "endDate": "2026-11-01T00:00:00",
        },
    ]
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    picked = pick_current_season(seasons, now=now)
    # Both b and c in range; latest startDate wins → c
    assert picked["uid"] == "c"


def test_pick_current_season_override_exact_name():
    seasons = [
        {"uid": "1", "name": "Summer I- 2026", "startDate": "2026-01-01T00:00:00", "endDate": "2026-03-01T00:00:00"},
        {"uid": "2", "name": "Summer III- 2026", "startDate": "2026-07-01T00:00:00", "endDate": "2026-12-01T00:00:00"},
    ]
    picked = pick_current_season(seasons, season="Summer III- 2026")
    assert picked["uid"] == "2"


def test_pick_current_season_override_uid():
    seasons = [
        {"uid": "abc", "name": "A", "startDate": "2026-01-01T00:00:00", "endDate": "2026-02-01T00:00:00"},
        {"uid": "def", "name": "B", "startDate": "2026-03-01T00:00:00", "endDate": "2026-04-01T00:00:00"},
    ]
    assert pick_current_season(seasons, season="def")["name"] == "B"


def test_pick_current_season_override_missing_raises():
    seasons = [{"uid": "1", "name": "Only", "startDate": "2026-01-01T00:00:00", "endDate": "2026-02-01T00:00:00"}]
    with pytest.raises(LeanApiError, match="No season matched"):
        pick_current_season(seasons, season="Nope")


def test_pick_current_season_fallback_latest_start():
    seasons = [
        {"uid": "a", "name": "Past", "startDate": "2024-01-01T00:00:00", "endDate": "2024-06-01T00:00:00"},
        {"uid": "b", "name": "Future", "startDate": "2027-01-01T00:00:00", "endDate": "2027-06-01T00:00:00"},
    ]
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Neither in range → latest startDate
    assert pick_current_season(seasons, now=now)["uid"] == "b"
