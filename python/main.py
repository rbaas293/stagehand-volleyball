#!/usr/bin/env python3
"""
Stagehand v4 (Python) scraper: volleyball game times for a team (or captain) on league.ninja.

This mirrors the TypeScript scraper in ../index.ts:
  1. Load config (captainName / teamName / day / league / leagueUrl).
  2. Launch a local Chrome browser (or Browserbase cloud browser).
  3. Open standings → extract rows with pydantic → resolve team(s) by captain or team name.
  4. Open schedule → act() to click each week tab → extract() that week's games.
  5. Write games.json next to this script (and print JSON to stdout).

Run (from this folder):
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  export OPENAI_API_KEY=...   # or put it in ../.env / .env
  python main.py

Env:
  OPENAI_API_KEY       required for local-Chrome mode
  BROWSERBASE_API_KEY  optional: run in a Browserbase cloud browser instead
  STAGEHAND_MODEL      optional, default "openai/gpt-5.6-luna"
  HEADLESS=false       optional: show the local Chrome window

Config resolution (same as TypeScript):
  - If captainName is set (non-empty), discover team(s) on the standings page whose
    captain matches (case-insensitive, partial OK: "Robinson" matches "H. Robinson"),
    then scrape all games for those team name(s).
  - If captainName is empty/absent, use teamName as an explicit team override.

league.ninja layout (as of Oct 2026):
  <division URL>           -> Standings tab (league/division names + W-L + captains)
  <division URL>/schedule  -> Schedule tab with one sub-tab per week
"""

from __future__ import annotations

# ---- Standard library -------------------------------------------------------
import asyncio          # Stagehand's Python API is async; we drive it with asyncio.run()
import json             # Parse config.json and serialize games.json
import os               # Read env vars (OPENAI_API_KEY, HEADLESS, …)
import re               # Match week-tab labels (Week / TOURNAMENT / …)
import sys              # Exit codes + stderr logging
from datetime import datetime, timezone  # scrapedAt timestamp (UTC ISO-8601)
from pathlib import Path                 # Config / output paths without string concat
from typing import Any, Literal          # Typing for status enum + loose JSON bits

# ---- Third-party ------------------------------------------------------------
from pydantic import BaseModel, Field, ValidationError, model_validator
from stagehand import Stagehand, browserbase, local_browser

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# Directory that contains this script (python/).
ROOT = Path(__file__).resolve().parent

# Prefer the shared repo-root config so one config.json drives both TS and Python.
# Fall back to python/config.json if someone wants a Python-only override.
PARENT_CONFIG = ROOT.parent / "config.json"
LOCAL_CONFIG = ROOT / "config.json"
CONFIG_PATH = PARENT_CONFIG if PARENT_CONFIG.is_file() else LOCAL_CONFIG

# Output lands next to this script so TS games.json and Python games.json don't clash.
OUTPUT_PATH = ROOT / "games.json"


# ---------------------------------------------------------------------------
# Errors (matched to the TypeScript ConfigError / MissingKeyError / ResolveError)
# ---------------------------------------------------------------------------
class ConfigError(Exception):
    """Raised when config.json is missing, unreadable, or fails validation."""


class MissingKeyError(Exception):
    """Raised when neither OPENAI_API_KEY nor BROWSERBASE_API_KEY is available."""


class ResolveError(Exception):
    """Raised when captainName / teamName matches no standings row."""


# ---------------------------------------------------------------------------
# Config model (mirrors ConfigSchema in index.ts)
# ---------------------------------------------------------------------------
class AppConfig(BaseModel):
    """User-editable knobs loaded from config.json."""

    # Captain to search for on the standings page (preferred discovery path).
    captain_name: str = Field(default="", alias="captainName")
    # Explicit team name; used only when captainName is empty/absent.
    team_name: str = Field(default="", alias="teamName")
    # Game-day label stored in the output for convenience (e.g. "Sunday").
    day: str = Field(min_length=1)
    # Human-readable league / division path for your own notes.
    league: str = Field(min_length=1)
    # Division standings URL; the script appends schedulePathSuffix for the schedule page.
    league_url: str = Field(alias="leagueUrl")
    # Path appended to leagueUrl to reach the schedule tab (default "/schedule").
    schedule_path_suffix: str = Field(default="/schedule", alias="schedulePathSuffix")

    # Allow reading camelCase JSON keys while exposing snake_case attributes in Python.
    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def require_captain_or_team(self) -> "AppConfig":
        """Same rule as the TS superRefine: at least one of captain/team must be set."""
        has_captain = bool(self.captain_name.strip())
        has_team = bool(self.team_name.strip())
        if not has_captain and not has_team:
            raise ValueError(
                "Set captainName (to discover team(s) by captain) and/or teamName "
                "(explicit team when captainName is empty)."
            )
        return self


def load_dotenv_files() -> None:
    """
    Optionally load ../.env then python/.env into os.environ (without overriding
    vars already set in the shell). Keeps secrets out of the repo; .env is gitignored.
    """
    # Try python-dotenv if installed; otherwise do a tiny manual parser so the
    # scraper still works with only `pip install stagehand`.
    candidates = [ROOT.parent / ".env", ROOT / ".env"]
    try:
        from dotenv import load_dotenv  # type: ignore

        for path in candidates:
            if path.is_file():
                # override=False: shell exports win over .env values.
                load_dotenv(path, override=False)
        return
    except ImportError:
        pass

    # Manual fallback: KEY=VALUE lines, ignore comments / blanks.
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Never clobber an env var the user already exported.
            if key and key not in os.environ:
                os.environ[key] = value


def load_config() -> AppConfig:
    """Read and validate config.json (parent preferred, then python/config.json)."""
    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError as err:
        raise ConfigError(
            f"Missing or unreadable config.json at {CONFIG_PATH} ({err}). "
            "Copy config.example.json to config.json at the repo root and edit "
            "captainName and/or teamName, day, league, and leagueUrl."
        ) from err

    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ConfigError(f"config.json is not valid JSON: {err}") from err

    try:
        return AppConfig.model_validate(parsed)
    except ValidationError as err:
        # Flatten pydantic errors into one readable line (like the TS zod issues join).
        details = "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', ())) or '(root)'}: {e.get('msg')}"
            for e in err.errors()
        )
        raise ConfigError(
            f"config.json is invalid: {details}. Expected day, league, leagueUrl, "
            "plus captainName and/or teamName (and optional schedulePathSuffix)."
        ) from err


# ---------------------------------------------------------------------------
# Extract schemas (pydantic = Zod equivalent for Stagehand extract())
# ---------------------------------------------------------------------------
class Game(BaseModel):
    """One match card involving a matched team."""

    date: str = Field(description="Game date as shown, e.g. 'Sun, Oct 11'")
    time: str = Field(description="Start time as shown, e.g. '5:00 pm'")
    week: str | None = Field(
        default=None,
        description="Week / round label, e.g. 'Week 6 - Oct 11' or 'Tournament'",
    )
    team: str | None = Field(
        default=None,
        description="Which of our matched teams is in this match",
    )
    opponent: str = Field(
        description="The other team in the match (not one of our matched teams)"
    )
    location: str = Field(
        description="Venue and court, e.g. 'The Fieldhouse - Court B'"
    )
    status: Literal["scheduled", "completed", "cancelled"] = Field(
        description=(
            "completed if a winner/score is shown, cancelled if marked "
            "cancelled/postponed, else scheduled"
        )
    )
    result: str | None = Field(
        default=None,
        description="Result if shown, e.g. 'Winner - <team>' or a score; omit if not shown",
    )


class Games(BaseModel):
    """Wrapper required by Stagehand: arrays must live on an object field."""

    games: list[Game] = Field(
        description="Only matches where one of our matched teams is playing"
    )


class StandingsRow(BaseModel):
    """One row from the division standings table."""

    team_name: str = Field(
        alias="teamName",
        description="Full team name as shown in the standings table",
    )
    captain_name: str | None = Field(
        default=None,
        alias="captainName",
        description="Captain name shown for that team, e.g. 'H. Robinson'",
    )
    record: str | None = Field(
        default=None,
        description="W-L record, e.g. '2-3'",
    )
    standing: str | None = Field(
        default=None,
        description="Rank in the standings table, e.g. '4'",
    )

    model_config = {"populate_by_name": True}


class Standings(BaseModel):
    """Full standings extract: league + division + every row."""

    league_name: str | None = Field(
        default=None,
        alias="leagueName",
        description="League / season name, e.g. 'Summer III- 2026'",
    )
    division_name: str | None = Field(
        default=None,
        alias="divisionName",
        description=(
            "Division name, e.g. 'Sunday Coed Sixes- Beer A- EVENING (5:00-7:00PM)'"
        ),
    )
    rows: list[StandingsRow] = Field(
        description="Every team row in the standings table, including team name and captain"
    )

    model_config = {"populate_by_name": True}


def games_schema_for(team_names: list[str]) -> type[BaseModel]:
    """
    Build a Games-like pydantic model whose Field description names the matched
    teams (helps the LLM filter schedule cards the same way the TS zod schema does).
    """
    # Quote each team so the description reads: "Win or Lose We Booze" or "Other"
    listed = " or ".join(f'"{t}"' for t in team_names)

    class GamesForTeams(BaseModel):
        games: list[Game] = Field(
            description=f"Only matches where {listed} is one of the two teams"
        )

    # Give the dynamic class a stable name for logging / debugging.
    GamesForTeams.__name__ = "GamesForTeams"
    return GamesForTeams


# ---------------------------------------------------------------------------
# Team resolution helpers
# ---------------------------------------------------------------------------
def captain_matches(row_captain: str | None, wanted: str) -> bool:
    """
    Case-insensitive, whitespace-normalized partial match.
    "Robinson" matches "H. Robinson" and vice versa (same as index.ts).
    """
    if not row_captain or not wanted:
        return False
    a = re.sub(r"\s+", " ", row_captain.lower()).strip()
    b = re.sub(r"\s+", " ", wanted.lower()).strip()
    return a in b or b in a


def resolve_teams_from_standings(
    rows: list[StandingsRow],
    *,
    use_captain: bool,
    captain_name: str,
    config_team_name: str,
) -> tuple[list[StandingsRow], Literal["captain", "teamName"]]:
    """
    Pick which standings rows we scrape games for.

    Returns (matched_rows, resolution_mode) where resolution_mode is
    "captain" or "teamName".
    """
    if use_captain:
        # Prefer captain discovery whenever captainName is non-empty.
        matched = [r for r in rows if captain_matches(r.captain_name, captain_name)]
        if not matched:
            captains = ", ".join(r.captain_name for r in rows if r.captain_name) or "(none extracted)"
            raise ResolveError(
                f'No standings row matched captainName="{captain_name}". '
                f"Captains seen: {captains}."
            )
        return matched, "captain"

    # Explicit teamName path (captainName empty / omitted).
    wanted = config_team_name.lower()
    matched = [r for r in rows if r.team_name.lower() == wanted]
    if not matched:
        # Fall back to case-insensitive substring if exact match fails.
        partial = [r for r in rows if wanted in r.team_name.lower()]
        if not partial:
            names = ", ".join(r.team_name for r in rows) or "(none extracted)"
            raise ResolveError(
                f'No standings row matched teamName="{config_team_name}". '
                f"Teams seen: {names}."
            )
        return partial, "teamName"
    return matched, "teamName"


# ---------------------------------------------------------------------------
# Logging / browser setup
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    """Print progress to stderr so stdout stays clean JSON (same as TS)."""
    print(f"[stagehand-volleyball] {msg}", file=sys.stderr)


def model_kwargs() -> dict[str, Any]:
    """
    Build Stagehand.create() kwargs for the LLM.

    Stagehand never reads env vars itself — we pass the key explicitly.
    Local Chrome needs OPENAI_API_KEY; Browserbase can use its Model Gateway
    when OPENAI_API_KEY is absent.
    """
    model_name = os.environ.get("STAGEHAND_MODEL", "openai/gpt-5.6-luna")
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        # model= is the model id string; model_api_key authenticates the provider.
        return {"model": model_name, "model_api_key": api_key}
    if os.environ.get("BROWSERBASE_API_KEY"):
        # Omit model — Browserbase Model Gateway picks one.
        return {}
    raise MissingKeyError(
        "OPENAI_API_KEY is not set. Export it, or put it in .env "
        "(repo root or python/). Alternatively set BROWSERBASE_API_KEY to use a "
        "Browserbase cloud browser + Model Gateway."
    )


async def launch_browser():
    """
    Launch either a Browserbase cloud browser or local Chrome.

    HEADLESS defaults to true; set HEADLESS=false to watch the window.
    """
    bb_key = os.environ.get("BROWSERBASE_API_KEY")
    if bb_key:
        log("Using Browserbase cloud browser")
        return await browserbase.launch(api_key=bb_key)

    # HEADLESS=false means show the Chrome UI; anything else (incl. unset) is headless.
    headless = os.environ.get("HEADLESS", "true").lower() != "false"
    log(f"Using local Chrome (headless={headless})")
    return await local_browser.launch(headless=headless)


# ---------------------------------------------------------------------------
# Schedule helpers (week tabs)
# ---------------------------------------------------------------------------
# Labels we care about on the schedule tablist (ignore unrelated UI tabs).
_WEEK_TAB_RE = re.compile(r"LEAGUE ROUND|TOURNAMENT|PLAYOFF|Week", re.IGNORECASE)


async def list_week_tab_labels(page) -> list[str]:
    """
    Read every role=tab under role=tablist and keep week / tournament labels.

    Python Stagehand's page.evaluate() takes a JS *expression string* (no
    extra args), so the selector logic lives entirely inside the expression.
    """
    # evaluate returns JSON-serializable values; we ask for a string array.
    raw = await page.evaluate(
        """(() => {
          return Array.from(document.querySelectorAll('[role=tablist] [role=tab]'))
            .map((el) => el.innerText.replace(/\\s*\\n\\s*/g, ' ').trim())
            .filter((t) => /LEAGUE ROUND|TOURNAMENT|PLAYOFF|Week/i.test(t));
        })()"""
    )
    # Defensive: coerce whatever came back into a clean list[str].
    if not isinstance(raw, list):
        return []
    return [str(t) for t in raw if isinstance(t, str) and _WEEK_TAB_RE.search(t)]


async def click_week_tab(stagehand: Stagehand, label: str) -> None:
    """
    Select a schedule week tab via stagehand.act() (natural-language click).

    Using act() (instead of a brittle CSS click) matches the Stagehand
    act / extract / observe flow and survives minor DOM churn on league.ninja.
    """
    await stagehand.act(
        f'Click the schedule week tab labeled exactly "{label}". '
        "It is one of the tabs in the week/round tab list on the schedule page."
    )


async def page_mentions_any_team(page, team_names: list[str]) -> bool:
    """
    Cheap pre-check: skip the LLM extract when no matched team name appears
    in the page text (e.g. tournament bracket not posted yet).
    """
    # Embed names as a JSON array literal inside the JS expression.
    names_json = json.dumps([n.lower() for n in team_names])
    result = await page.evaluate(
        f"""(() => {{
          const names = {names_json};
          const body = document.body.innerText.toLowerCase();
          return names.some((n) => body.includes(n));
        }})()"""
    )
    return bool(result)


# ---------------------------------------------------------------------------
# Main scrape flow
# ---------------------------------------------------------------------------
async def scrape() -> dict[str, Any]:
    """
    End-to-end scrape. Returns the output dict that we also write to games.json.
    """
    # Load .env before reading keys / HEADLESS so local runs Just Work.
    load_dotenv_files()
    config = load_config()

    # Normalize the discovery knobs once (strip whitespace like the TS version).
    captain_name = config.captain_name.strip()
    config_team_name = config.team_name.strip()
    use_captain = len(captain_name) > 0

    # Strip trailing slashes so suffix join is predictable.
    league_url = config.league_url.rstrip("/")
    suffix = config.schedule_path_suffix
    if not suffix.startswith("/"):
        suffix = f"/{suffix}"
    schedule_url = f"{league_url}{suffix}"

    # Announce what we're about to scrape (mirrors TS logs).
    if use_captain:
        ignored = (
            f' (config teamName="{config_team_name}" ignored while captainName is set)'
            if config_team_name
            else ""
        )
        log(f'Config: captain="{captain_name}"{ignored} day="{config.day}" league="{config.league}"')
    else:
        log(f'Config: team="{config_team_name}" day="{config.day}" league="{config.league}"')
    log(f"Standings URL: {league_url}")
    log(f"Schedule URL:  {schedule_url}")
    log(f"Config path:   {CONFIG_PATH}")

    # Fail fast on missing API key *before* launching Chrome.
    create_kwargs = model_kwargs()

    browser = await launch_browser()
    try:
        # Create the Stagehand client bound to this browser + model.
        stagehand = await Stagehand.create(browser=browser, **create_kwargs)
        try:
            # Use the first (default) page in the browser context.
            pages = await browser.context.pages()
            page = pages[0]

            # ---------------------------------------------------------------
            # 1) Standings: extract every row, then resolve target team(s).
            # ---------------------------------------------------------------
            log(f"Opening standings: {league_url}")
            await page.goto(league_url, wait_until="networkidle", timeout=60_000)
            # Brief settle so client-rendered standings finish painting.
            await page.wait_for_timeout(1_500)

            standings_result = await stagehand.extract(
                (
                    "From this league standings page, get the league/season name, "
                    "the division name, and every row in the standings table. "
                    "For each row extract the full team name, the captain name "
                    "(often shown next to or under the team), the W-L record, "
                    "and the rank. Include every team, not just one."
                ),
                Standings,
            )
            standings = standings_result.data

            matched_teams, resolution = resolve_teams_from_standings(
                standings.rows or [],
                use_captain=use_captain,
                captain_name=captain_name,
                config_team_name=config_team_name,
            )
            team_names = [t.team_name for t in matched_teams]
            log(
                "Resolved via "
                + resolution
                + ": "
                + ", ".join(
                    f'"{t.team_name}" (captain={t.captain_name or "?"})'
                    for t in matched_teams
                )
            )

            # ---------------------------------------------------------------
            # 2) Schedule: act() through each week tab, extract() games.
            # ---------------------------------------------------------------
            log(f"Opening schedule: {schedule_url}")
            await page.goto(schedule_url, wait_until="networkidle", timeout=60_000)
            await page.wait_for_timeout(1_500)

            week_labels = await list_week_tab_labels(page)
            log(f"Found {len(week_labels)} week tabs: {' | '.join(week_labels)}")

            games: list[Game] = []
            rounds_without_team_games: list[str] = []
            extract_schema = games_schema_for(team_names)
            team_list_for_prompt = " or ".join(f'"{t}"' for t in team_names)
            team_names_csv = ", ".join(team_names)

            for label in week_labels:
                # Natural-language click via act() (required Python flow).
                await click_week_tab(stagehand, label)
                await page.wait_for_timeout(1_500)

                # Skip LLM when none of our teams appear in this week's DOM.
                if not await page_mentions_any_team(page, team_names):
                    log(f"  {label}: no matched-team games listed")
                    rounds_without_team_games.append(label)
                    continue

                extract_result = await stagehand.extract(
                    (
                        f'This page shows the games for the selected week tab "{label}". '
                        f"List every match on the selected week where {team_list_for_prompt} "
                        "is one of the two teams. Each match card shows date+time, "
                        "location/court, the two team names (each followed by a captain name), "
                        'and sometimes "Winner - <team>". For each match set "team" to whichever '
                        f"of [{team_names_csv}] is playing, and \"opponent\" to the other side. "
                        f'Ignore captain names on the schedule cards. Set week to "{label}".'
                    ),
                    extract_schema,
                )

                week_games: list[Game] = list(extract_result.data.games)
                log(f"  {label}: {len(week_games)} game(s)")

                # Fill week / team defaults the same way the TS scraper does.
                for g in week_games:
                    if not g.week:
                        g.week = label
                    if not g.team and len(team_names) == 1:
                        g.team = team_names[0]
                    games.append(g)

            # ---------------------------------------------------------------
            # 3) Build output payload (shape matches the TypeScript scraper).
            # ---------------------------------------------------------------
            output: dict[str, Any] = {
                "captainSearched": captain_name if use_captain else None,
                "resolution": resolution,
                "matchedTeams": [
                    {
                        "teamName": t.team_name,
                        "captainName": t.captain_name,
                        "record": t.record,
                        "standing": t.standing,
                    }
                    for t in matched_teams
                ],
                # Back-compat: single string when one team matched, else list.
                "team": team_names[0] if len(team_names) == 1 else team_names,
                "day": config.day,
                "league": config.league,
                "url": league_url,
                "scrapedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "leagueName": standings.league_name,
                "divisionName": standings.division_name,
                "games": [g.model_dump(by_alias=False) for g in games],
                "roundsWithoutTeamGames": rounds_without_team_games,
            }
            # Only emit single-team record/standing when exactly one team matched.
            if len(matched_teams) == 1:
                output["teamRecord"] = matched_teams[0].record
                output["teamStanding"] = matched_teams[0].standing

            return output
        finally:
            # Always tear down the Stagehand session (LLM / CDP bridge).
            await stagehand.close()
    finally:
        # Always close the browser so Chrome/Browserbase sessions don't leak.
        await browser.close()


async def async_main() -> None:
    """Run scrape(), print JSON to stdout, write python/games.json."""
    output = await scrape()
    json_text = json.dumps(output, indent=2)
    # stdout: machine-readable result (can pipe to jq, etc.)
    print(json_text)
    OUTPUT_PATH.write_text(json_text + "\n", encoding="utf-8")
    n_games = len(output.get("games") or [])
    n_teams = len(output.get("matchedTeams") or [])
    log(f"Wrote {n_games} games for {n_teams} team(s) to {OUTPUT_PATH}")


def main() -> None:
    """CLI entrypoint: translate known errors into clean stderr + exit 1."""
    try:
        asyncio.run(async_main())
    except (ConfigError, MissingKeyError, ResolveError) as err:
        # Friendly one-liners for config / key / resolve failures.
        print(err, file=sys.stderr)
        sys.exit(1)
    except Exception as err:  # noqa: BLE001 — surface unexpected failures fully
        print(err, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
