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


def test_pick_season_for_levels_skips_empty_fall_oct26():
    """
    Live failure mode: on ~Oct 26, Summer III and Fall both in-range; Fall has the
    later startDate but 0 Beer A/B divisions → must fall back to Summer III.
    """
    from lean_api import pick_season_for_levels

    seasons = [
        {
            "uid": "summer-uid",
            "name": "Summer III- 2026",
            "startDate": "2026-07-01T00:00:00",
            "endDate": "2026-12-15T00:00:00",
        },
        {
            "uid": "fall-uid",
            "name": "Fall 2026",
            # Starts ~Oct 25 16:00 UTC → later start than Summer; both in range on Oct 26.
            "startDate": "2026-10-25T16:00:00",
            "endDate": "2027-01-15T00:00:00",
        },
    ]
    now = datetime(2026, 10, 26, 18, 0, 0, tzinfo=timezone.utc)
    levels = ["Beer A", "Beer B"]

    def fake_list(_api: str, uid: str):
        if uid == "fall-uid":
            return [
                {"divisionUid": "f1", "divisionName": "Thursday Open", "leagueName": "Fall Open"},
            ]
        return [
            {
                "divisionUid": "s1",
                "divisionName": "Sunday Beer (A)- Court E",
                "leagueName": "Summer III Beer A",
            },
            {
                "divisionUid": "s2",
                "divisionName": "Friday Beer (B)- Court H",
                "leagueName": "Summer III Beer B",
            },
        ]

    logs: list[str] = []
    season, matched, _reason, status = pick_season_for_levels(
        "https://api.example",
        seasons,
        levels,
        now=now,
        list_divisions=fake_list,
        log_fn=logs.append,
    )
    assert season["uid"] == "summer-uid"
    assert len(matched) == 2
    assert status["picked"]["uid"] == "summer-uid"
    assert any(s["reason"] == "not posted yet" for s in status["skipped"])
    assert status["skipped"][0]["name"] == "Fall 2026"


def test_pick_season_for_levels_override_skips_fallback():
    from lean_api import pick_season_for_levels

    seasons = [
        {
            "uid": "summer-uid",
            "name": "Summer III- 2026",
            "startDate": "2026-07-01T00:00:00",
            "endDate": "2026-12-15T00:00:00",
        },
        {
            "uid": "fall-uid",
            "name": "Fall 2026",
            "startDate": "2026-10-25T16:00:00",
            "endDate": "2027-01-15T00:00:00",
        },
    ]

    def fake_list(_api: str, uid: str):
        if uid == "fall-uid":
            return [{"divisionUid": "f1", "divisionName": "Open", "leagueName": "Open"}]
        return [
            {"divisionUid": "s1", "divisionName": "Beer A", "leagueName": "Beer A"},
        ]

    with pytest.raises(LeanApiError, match='season="Fall 2026"'):
        pick_season_for_levels(
            "https://api.example",
            seasons,
            ["Beer A"],
            season="Fall 2026",
            list_divisions=fake_list,
        )


def test_pick_season_skips_empty_standings_as_not_posted():
    """Divisions matching levels but with no teams → not posted yet; fall back."""
    from lean_api import pick_season_for_levels

    seasons = [
        {
            "uid": "new-uid",
            "name": "Fall 2026",
            "startDate": "2026-10-25T16:00:00",
            "endDate": "2027-01-15T00:00:00",
        },
        {
            "uid": "summer-uid",
            "name": "Summer III- 2026",
            "startDate": "2026-07-01T00:00:00",
            "endDate": "2026-12-15T00:00:00",
        },
    ]
    now = datetime(2026, 10, 26, 18, 0, 0, tzinfo=timezone.utc)

    def fake_list(_api: str, uid: str):
        return [
            {"divisionUid": f"{uid}-d1", "divisionName": "Sunday Beer (A)", "leagueName": "Beer A"},
        ]

    def fake_standings(_api: str, uid: str):
        if uid.startswith("new-uid"):
            return []  # no teams yet
        return [{"teamName": "Him-Roids", "captainName": "R. Baas"}]

    season, _matched, _reason, status = pick_season_for_levels(
        "https://api.example",
        seasons,
        ["Beer A"],
        now=now,
        list_divisions=fake_list,
        probe_standings=fake_standings,
    )
    assert season["uid"] == "summer-uid"
    assert status["skipped"][0]["name"] == "Fall 2026"
    assert status["skipped"][0]["reason"] == "not posted yet"
    assert status["picked"]["name"] == "Summer III- 2026"


def test_season_probe_skips_lean_api_error_and_continues():
    """Persistent 500 on first standings probe must not abort; try next division/season."""
    from lean_api import LeanApiError, pick_season_for_levels

    seasons = [
        {
            "uid": "bad-uid",
            "name": "Broken Season",
            "startDate": "2026-10-01T00:00:00",
            "endDate": "2026-12-01T00:00:00",
        },
        {
            "uid": "good-uid",
            "name": "Summer III- 2026",
            "startDate": "2026-07-01T00:00:00",
            "endDate": "2026-12-15T00:00:00",
        },
    ]
    now = datetime(2026, 10, 26, 18, 0, 0, tzinfo=timezone.utc)

    def fake_list(_api: str, uid: str):
        return [
            {"divisionUid": f"{uid}-d1", "divisionName": "Beer A", "leagueName": "Beer A"},
            {"divisionUid": f"{uid}-d2", "divisionName": "Beer A2", "leagueName": "Beer A"},
        ]

    def fake_standings(_api: str, uid: str):
        if uid == "bad-uid-d1":
            raise LeanApiError("HTTP 500 for standings")
        if uid.startswith("bad-uid"):
            return []  # empty → not posted
        return [{"teamName": "Him-Roids", "captainName": "R. Baas"}]

    season, matched, _reason, status = pick_season_for_levels(
        "https://api.example",
        seasons,
        ["Beer A"],
        now=now,
        list_divisions=fake_list,
        probe_standings=fake_standings,
    )
    assert season["uid"] == "good-uid"
    assert status["picked"]["uid"] == "good-uid"
    assert any(s["name"] == "Broken Season" for s in status["skipped"])
