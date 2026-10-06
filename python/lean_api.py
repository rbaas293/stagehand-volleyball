"""
Lean (no-LLM) client for league.ninja public LMS API.

Discovered from the Flannagan's SPA (leagueStore): base URL like
https://flan1-lms-pub-api.league.ninja with endpoints:
  GET /nav/seasons/
  GET nav/season/{seasonUid}
  GET /divisions/{divUid}
  GET /divisions/{divUid}/standings/
  GET /divisions/{divUid}/schedule/v2/
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

# Match result codes from ninScripts MatchResult enum.
_RESULT_HOME_PLAY = 10
_RESULT_HOME_FORFEIT = 11
_RESULT_AWAY_PLAY = 20
_RESULT_AWAY_FORFEIT = 21
_RESULT_TIE = 1
_RESULT_CANCELLED = 2

_ET = ZoneInfo("America/New_York")

# Known club hostname → pub API host (SPA ninjaOrg.apiPub).
_API_BY_SITE_HOST = {
    "flannagans.league.ninja": "https://flan1-lms-pub-api.league.ninja",
}


class LeanApiError(Exception):
    """HTTP / shape errors from the pub API."""


def infer_api_base(*, api_base_url: str | None, site_url: str | None, league_url: str | None) -> str:
    """Resolve the pub-api base URL from config overrides or known club hosts."""
    if api_base_url and api_base_url.strip():
        return api_base_url.strip().rstrip("/")
    for candidate in (site_url, league_url):
        if not candidate:
            continue
        host = urlparse(candidate.strip()).netloc.lower()
        if host in _API_BY_SITE_HOST:
            return _API_BY_SITE_HOST[host]
        # Generic guess: <sub>.league.ninja → often club-specific; require explicit apiBaseUrl.
    raise LeanApiError(
        "Cannot infer apiBaseUrl. Set apiBaseUrl (e.g. https://flan1-lms-pub-api.league.ninja) "
        "or use a known siteUrl/leagueUrl host (flannagans.league.ninja)."
    )


def division_url(site_url: str, div_uid: str) -> str:
    base = (site_url or "https://flannagans.league.ninja").rstrip("/")
    # siteUrl may already be a full division URL — use origin only.
    parsed = urlparse(base)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return f"{origin}/leagues/division/{div_uid}"


def _http_get_json(url: str, *, timeout: float = 30.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "stagehand-volleyball/lean (+https://github.com/rbaas293/stagehand-volleyball)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        raise LeanApiError(f"HTTP {err.code} for {url}") from err
    except urllib.error.URLError as err:
        raise LeanApiError(f"Request failed for {url}: {err}") from err
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as err:
        raise LeanApiError(f"Non-JSON response from {url}") from err
    # Pub API wraps payloads as {StatusCode, Data, ErrorMessage}.
    if isinstance(payload, dict) and "Data" in payload and "StatusCode" in payload:
        if payload.get("StatusCode") not in (200, None) and payload.get("Data") is None:
            raise LeanApiError(
                f"API error for {url}: {payload.get('ErrorMessage') or payload.get('StatusCode')}"
            )
        return payload["Data"]
    return payload


def list_seasons(api_base: str) -> list[dict[str, Any]]:
    data = _http_get_json(f"{api_base}/nav/seasons/")
    if not isinstance(data, list):
        raise LeanApiError("Unexpected /nav/seasons/ shape")
    return data


def pick_current_season(seasons: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    """Prefer the season whose [start, end] contains now (UTC); else latest startDate."""
    now = now or datetime.now(timezone.utc)

    def parse_dt(raw: str | None) -> datetime | None:
        if not raw:
            return None
        try:
            # API timestamps are naive UTC.
            return datetime.fromisoformat(raw.replace("Z", "")).replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    in_range: list[dict[str, Any]] = []
    for s in seasons:
        start = parse_dt(s.get("startDate"))
        end = parse_dt(s.get("endDate"))
        if start and end and start <= now <= end:
            in_range.append(s)
    if in_range:
        # If several overlap, pick the one with the latest start.
        in_range.sort(key=lambda s: s.get("startDate") or "", reverse=True)
        return in_range[0]
    if not seasons:
        raise LeanApiError("No seasons returned by /nav/seasons/")
    return max(seasons, key=lambda s: s.get("startDate") or "")


def list_season_divisions(api_base: str, season_uid: str) -> list[dict[str, Any]]:
    # SPA uses axios get("nav/season/"+uid) — leading slash 404s on this host.
    data = _http_get_json(f"{api_base}/nav/season/{season_uid}")
    if not isinstance(data, list):
        raise LeanApiError("Unexpected nav/season shape")
    return data


def filter_divisions_by_levels(
    divisions: list[dict[str, Any]], levels: list[str]
) -> list[dict[str, Any]]:
    """Keep divisions whose leagueName/divisionName contain any level substring (case-insensitive)."""
    needles = [lv.strip().lower() for lv in levels if lv and str(lv).strip()]
    if not needles:
        return list(divisions)
    out: list[dict[str, Any]] = []
    for d in divisions:
        blob = " ".join(
            [
                str(d.get("leagueName") or ""),
                str(d.get("divisionName") or ""),
                str(d.get("competitionLevel") or ""),
            ]
        ).lower()
        if any(n in blob for n in needles):
            out.append(d)
    return out


def get_standings(api_base: str, div_uid: str) -> list[dict[str, Any]]:
    data = _http_get_json(f"{api_base}/divisions/{div_uid}/standings/")
    if not isinstance(data, list):
        raise LeanApiError(f"Unexpected standings shape for {div_uid}")
    return data


def get_schedule_v2(api_base: str, div_uid: str) -> list[dict[str, Any]]:
    data = _http_get_json(f"{api_base}/divisions/{div_uid}/schedule/v2/")
    if not isinstance(data, list):
        raise LeanApiError(f"Unexpected schedule shape for {div_uid}")
    return data


def get_division(api_base: str, div_uid: str) -> dict[str, Any]:
    data = _http_get_json(f"{api_base}/divisions/{div_uid}")
    if not isinstance(data, dict):
        raise LeanApiError(f"Unexpected division shape for {div_uid}")
    return data


def standing_row_from_api(row: dict[str, Any]) -> dict[str, Any]:
    """Map API standings row → scraper StandingsRow-like dict (camelCase)."""
    wins = row.get("matchWins")
    losses = row.get("matchLosses")
    ties = row.get("matchTies") or 0
    if wins is not None and losses is not None:
        record = f"{wins}-{losses}" if not ties else f"{wins}-{ties}-{losses}"
    else:
        record = None
    ranking = row.get("ranking")
    return {
        "teamName": row.get("teamName") or "",
        "captainName": row.get("captainName"),
        "record": record,
        "standing": str(ranking) if ranking is not None else None,
        "teamUid": row.get("teamUid"),
    }


def _format_match_start(raw: str | None) -> tuple[str, str]:
    """Return (date_str, time_str) in America/New_York, e.g. ('Sun, Oct 11', '5:00 pm')."""
    if not raw:
        return ("", "")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "")).replace(tzinfo=timezone.utc)
    except ValueError:
        return (raw, "")
    local = dt.astimezone(_ET)
    date_str = local.strftime("%a, %b ") + str(local.day)  # avoid zero-padded day
    hour = local.strftime("%I").lstrip("0") or "0"
    minute = local.strftime("%M")
    ampm = local.strftime("%p").lower()
    time_str = f"{hour}:{minute} {ampm}"
    return date_str, time_str


def _week_label(match: dict[str, Any]) -> str:
    inc_name = (match.get("incrementName") or "").strip()
    inc_num = match.get("incrementNumber")
    inc_date = match.get("incrementDate")
    is_tourn = bool(match.get("isTournament"))
    date_part = ""
    if inc_date:
        try:
            dt = datetime.fromisoformat(inc_date.replace("Z", "")).replace(tzinfo=timezone.utc)
            local = dt.astimezone(_ET)
            date_part = local.strftime("%b ") + str(local.day)
        except ValueError:
            date_part = ""
    if is_tourn:
        return f"TOURNAMENT {date_part}".strip()
    if inc_name.lower() == "week" and inc_num is not None:
        return f"Week {inc_num} - {date_part}".strip(" -")
    if inc_name and inc_num is not None:
        return f"{inc_name} {inc_num} - {date_part}".strip(" -")
    return inc_name or (date_part or "Round")


def _location_str(match: dict[str, Any]) -> str:
    loc = match.get("location") or {}
    generated = loc.get("locationGeneratedName")
    if generated:
        return str(generated)
    facility = loc.get("facilityName") or ""
    court = loc.get("locationName") or ""
    if facility and court:
        return f"{facility} - Court {court}"
    return facility or court or ""


def _status_and_result(match: dict[str, Any]) -> tuple[str, str | None]:
    result = match.get("result")
    home = (match.get("homeTeam") or {}).get("name") or ""
    away = (match.get("awayTeam") or {}).get("name") or ""
    if result is None or result == 0:
        return "scheduled", None
    if result == _RESULT_CANCELLED:
        return "cancelled", "Game Cancelled"
    if result == _RESULT_TIE:
        return "completed", "TIE"
    if result == _RESULT_HOME_PLAY:
        return "completed", f"Winner - {home}"
    if result == _RESULT_HOME_FORFEIT:
        return "completed", f"Winner By Forfeit - {home}"
    if result == _RESULT_AWAY_PLAY:
        return "completed", f"Winner - {away}"
    if result == _RESULT_AWAY_FORFEIT:
        return "completed", f"Winner By Forfeit - {away}"
    return "completed", f"result={result}"


def games_for_teams(
    matches: list[dict[str, Any]], team_names: list[str]
) -> list[dict[str, Any]]:
    """Filter schedule matches to ones involving any of team_names; map to Game dicts."""
    wanted = {n.lower() for n in team_names}
    games: list[dict[str, Any]] = []
    for m in matches:
        if m.get("matchBye"):
            continue
        home = (m.get("homeTeam") or {}).get("name") or ""
        away = (m.get("awayTeam") or {}).get("name") or ""
        home_l, away_l = home.lower(), away.lower()
        if home_l in wanted:
            team, opponent = home, away
        elif away_l in wanted:
            team, opponent = away, home
        else:
            continue
        date_str, time_str = _format_match_start(m.get("matchStart"))
        status, result = _status_and_result(m)
        games.append(
            {
                "date": date_str,
                "time": time_str,
                "week": _week_label(m),
                "team": team,
                "opponent": opponent,
                "location": _location_str(m),
                "status": status,
                "result": result,
            }
        )
    return games


def levels_match_blob(levels: list[str], *parts: str) -> bool:
    needles = [lv.strip().lower() for lv in levels if lv and str(lv).strip()]
    if not needles:
        return True
    blob = " ".join(parts).lower()
    return any(n in blob for n in needles)
