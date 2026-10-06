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

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from lean_http import (
    CircuitOpenError,
    HttpSettings,
    LeanApiError,
    configure_http,
    get_client,
    http_get_json,
)

# Match result codes from ninScripts MatchResult enum.
_RESULT_HOME_PLAY = 10
_RESULT_HOME_FORFEIT = 11
_RESULT_AWAY_PLAY = 20
_RESULT_AWAY_FORFEIT = 21
_RESULT_TIE = 1
_RESULT_CANCELLED = 2

_ET = ZoneInfo("America/New_York")

_API_BY_SITE_HOST = {
    "flannagans.league.ninja": "https://flan1-lms-pub-api.league.ninja",
}


# Re-export for callers / tests.
__all__ = [
    "CircuitOpenError",
    "HttpSettings",
    "LeanApiError",
    "configure_http",
    "get_client",
    "http_get_json",
]



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
    raise LeanApiError(
        "Cannot infer apiBaseUrl. Set apiBaseUrl (e.g. https://flan1-lms-pub-api.league.ninja) "
        "or use a known siteUrl/leagueUrl host (flannagans.league.ninja)."
    )


def division_url(site_url: str, div_uid: str) -> str:
    base = (site_url or "https://flannagans.league.ninja").rstrip("/")
    parsed = urlparse(base)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return f"{origin}/leagues/division/{div_uid}"


def list_seasons(api_base: str) -> list[dict[str, Any]]:
    data = http_get_json(f"{api_base}/nav/seasons/")
    if not isinstance(data, list):
        raise LeanApiError("Unexpected /nav/seasons/ shape")
    return data


def _parse_api_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "")).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _season_bounds(s: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    start = _parse_api_dt(s.get("startDate") if isinstance(s.get("startDate"), str) else None)
    end = _parse_api_dt(s.get("endDate") if isinstance(s.get("endDate"), str) else None)
    if start is None and isinstance(s.get("startDate"), datetime):
        start = s["startDate"]
    if end is None and isinstance(s.get("endDate"), datetime):
        end = s["endDate"]
    return start, end


def _match_season_override(
    seasons: list[dict[str, Any]], override: str
) -> dict[str, Any]:
    needle = override.casefold()
    hits = [
        s
        for s in seasons
        if needle == str(s.get("uid") or "").casefold()
        or needle == str(s.get("name") or "").casefold()
        or needle in str(s.get("name") or "").casefold()
    ]
    if not hits:
        names = ", ".join(str(s.get("name") or s.get("uid")) for s in seasons)
        raise LeanApiError(
            f'No season matched config season="{override}". Seasons: {names}.'
        )

    def rank(s: dict[str, Any]) -> tuple:
        name = str(s.get("name") or "")
        uid = str(s.get("uid") or "")
        exact_name = 0 if name.casefold() == needle else 1
        exact_uid = 0 if uid.casefold() == needle else 1
        return (exact_name, exact_uid, -(len(name)), str(s.get("startDate") or ""), uid)

    hits.sort(key=rank)
    return hits[0]


def _start_sort_key(s: dict[str, Any]) -> tuple:
    # Latest start first; stable uid tie-break.
    return (str(s.get("startDate") or ""), str(s.get("uid") or ""))


def ordered_season_candidates(
    seasons: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    season: str | None = None,
) -> list[dict[str, Any]]:
    """
    Seasons to try, best-first (no division lookup yet).

    1. Config `season` override → that one season only.
    2. Else in-range seasons (startDate ≤ now ≤ endDate), latest startDate first.
    3. Then remaining seasons by latest startDate (fallback when none in range,
       or when in-range seasons have no matching levels).
    """
    if not seasons:
        raise LeanApiError("No seasons returned by /nav/seasons/")

    override = (season or "").strip()
    if override:
        return [_match_season_override(seasons, override)]

    now = now or datetime.now(timezone.utc)
    in_range: list[dict[str, Any]] = []
    for s in seasons:
        start, end = _season_bounds(s)
        if start and end and start <= now <= end:
            in_range.append(s)

    in_range.sort(key=_start_sort_key, reverse=True)
    rest = [s for s in seasons if s not in in_range]
    rest.sort(key=_start_sort_key, reverse=True)
    # Prefer in-range, then other seasons (most recent first).
    return in_range + rest


def pick_current_season(
    seasons: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    season: str | None = None,
) -> dict[str, Any]:
    """
    Deterministic date-only season pick (no levels awareness).

    Prefer the first candidate from ordered_season_candidates. When levels are
    configured, prefer pick_season_for_levels so seasons with matching divisions
    win over an empty newer overlap (e.g. Fall vs Summer III).
    """
    return ordered_season_candidates(seasons, now=now, season=season)[0]


NOT_POSTED_YET = "not posted yet"


def _season_has_posted_data(
    matched: list[dict[str, Any]],
    *,
    api_base: str,
    probe_standings: Callable[[str, str], list[dict[str, Any]]] | None,
    probe_limit: int = 8,
) -> tuple[bool, str]:
    """
    True when the season looks posted for our levels.

    - 0 matching divisions → not posted yet
    - If probe_standings is provided: need at least one probed division with teams
      (empty standings across probes → not posted yet)
    - If no probe: matching divisions alone count as posted
    """
    if not matched:
        return False, NOT_POSTED_YET
    if probe_standings is None:
        return True, "has matching divisions"
    empty = 0
    probed = 0
    for d in matched[: max(1, probe_limit)]:
        uid = str(d.get("divisionUid") or "")
        if not uid:
            continue
        probed += 1
        rows = probe_standings(api_base, uid)
        if rows:
            return True, "has teams in standings"
        empty += 1
    if probed == 0:
        return True, "has matching divisions"
    if empty == probed:
        return False, NOT_POSTED_YET
    return True, "has matching divisions"


def pick_season_for_levels(
    api_base: str,
    seasons: list[dict[str, Any]],
    levels: list[str],
    *,
    now: datetime | None = None,
    season: str | None = None,
    list_divisions: Any = None,
    probe_standings: Callable[[str, str], list[dict[str, Any]]] | None = None,
    probe_limit: int = 8,
    log_fn: Any = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], str, dict[str, Any]]:
    """
    Pick a season that has posted data for `levels`.

    Skips seasons with no matching divisions, or (when probed) matching divisions
    with no teams yet — logged as reason "not posted yet". Falls back to the next
    candidate that has data. Returns (season, matched_divisions, reason, season_status).

    A config `season` override is tried alone (no auto-fallback to another season).
    """
    list_divs = list_season_divisions if list_divisions is None else list_divisions
    _log = log_fn or (lambda _msg: None)
    override = (season or "").strip()
    candidates = ordered_season_candidates(seasons, now=now, season=season)
    skipped: list[dict[str, str]] = []

    for cand in candidates:
        uid = str(cand.get("uid") or "")
        name = str(cand.get("name") or uid)
        all_divs = list_divs(api_base, uid)
        matched = filter_divisions_by_levels(all_divs, levels)
        ok, detail = _season_has_posted_data(
            matched,
            api_base=api_base,
            probe_standings=probe_standings,
            probe_limit=probe_limit,
        )
        if not ok:
            reason = NOT_POSTED_YET
            if not matched:
                _log(
                    f"Season {name!r}: no divisions matching levels={levels!r} "
                    f"— {NOT_POSTED_YET}; trying next"
                )
            else:
                _log(
                    f"Season {name!r}: {len(matched)} level match(es) but no teams yet "
                    f"— {NOT_POSTED_YET}; trying next"
                )
            skipped.append({"name": name, "uid": uid, "reason": reason})
            if override:
                break
            continue

        reason = (
            f'config season override "{override}" → {name!r} ({uid})'
            if override
            else (
                f"using {name!r} ({uid}): {detail}; "
                f"{len(matched)} division(s) matching levels={levels!r}"
            )
        )
        if skipped:
            skipped_names = ", ".join(s["name"] for s in skipped)
            reason += f"; skipped not-posted: [{skipped_names}]"
        _log(f"Season pick: {reason}")
        season_status = {
            "picked": {"name": name, "uid": uid},
            "skipped": skipped,
        }
        return cand, matched, reason, season_status

    names = ", ".join(s["name"] for s in skipped) or "(none)"
    hint = (
        f' Set config season to a known good season name/uid (e.g. season: "Summer III- 2026"), '
        f"or adjust levels={levels!r}."
    )
    season_status = {"picked": None, "skipped": skipped}
    if override:
        raise LeanApiError(
            f'Config season="{override}" is {NOT_POSTED_YET} for levels={levels!r}. '
            f"Skipped: {names}.{hint}"
        )
    raise LeanApiError(
        f"No season has posted data for levels={levels!r}. "
        f"Skipped as {NOT_POSTED_YET}: {names}.{hint}"
    )


def list_season_divisions(api_base: str, season_uid: str) -> list[dict[str, Any]]:
    # SPA uses axios get("nav/season/"+uid) — leading slash 404s on this host.
    data = http_get_json(f"{api_base}/nav/season/{season_uid}")
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
    data = http_get_json(f"{api_base}/divisions/{div_uid}/standings/")
    if not isinstance(data, list):
        raise LeanApiError(f"Unexpected standings shape for {div_uid}")
    return data


def get_schedule_v2(api_base: str, div_uid: str) -> list[dict[str, Any]]:
    data = http_get_json(f"{api_base}/divisions/{div_uid}/schedule/v2/")
    if not isinstance(data, list):
        raise LeanApiError(f"Unexpected schedule shape for {div_uid}")
    return data


def get_division(api_base: str, div_uid: str) -> dict[str, Any]:
    data = http_get_json(f"{api_base}/divisions/{div_uid}")
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
    date_str = local.strftime("%a, %b ") + str(local.day)
    hour = local.strftime("%I").lstrip("0") or "0"
    minute = local.strftime("%M")
    ampm = local.strftime("%p").lower()
    time_str = f"{hour}:{minute} {ampm}"
    return date_str, time_str


def _week_label(match: dict[str, Any]) -> str:
    inc_name = (match.get("incrementName") or "").strip()
    inc_num = match.get("incrementNumber")
    is_tourn = bool(match.get("isTournament"))
    date_part = ""
    if match.get("incrementDate"):
        try:
            dt = datetime.fromisoformat(str(match["incrementDate"]).replace("Z", "")).replace(
                tzinfo=timezone.utc
            )
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
