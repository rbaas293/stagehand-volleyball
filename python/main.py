#!/usr/bin/env python3
"""
Stagehand / lean scraper: volleyball game times for a team (or captain) on league.ninja.

Modes (config.mode / SCRAPE_MODE):
  lean — public LMS HTTP API (no browser / no LLM)
  llm  — Stagehand v4 browser extract with xAI Grok BYO callback

Flow (llm):
  1. Load config.yaml (captainName / teamName / levels / leagueUrl / …).
  2. Launch local Chrome (or Browserbase).
  3. Standings → extract → resolve team(s) by captain or team name.
  4. Schedule → click each week tab → extract that week's games.
  5. Write games.json next to this script (and print JSON to stdout).

Run (from this folder):
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  python main.py                 # lean by default
  SCRAPE_MODE=llm python main.py # needs XAI_API_KEY (or ../.env / .env)

Env:
  XAI_API_KEY          required for llm mode (Grok via BYO LLM callback)
  BROWSERBASE_API_KEY  optional: run in a Browserbase cloud browser instead
  STAGEHAND_MODEL      optional, overrides config.yaml model (default "grok-4-fast-reasoning")
  SCRAPE_MODE          optional, overrides config.mode (lean|llm)
  HEADLESS=false       optional: show the local Chrome window (llm mode)

Config resolution:
  - If captainName is set (non-empty), discover team(s) on the standings page whose
    captain matches (exact after normalizing punctuation/case: "R. Baas" ≡ "R Baas"),
    then scrape all games for those team name(s).
  - If captainName is empty/absent, use teamName as an explicit team override.

league.ninja layout (as of Oct 2026):
  <division URL>           -> Standings tab (league/division names + W-L + captains)
  <division URL>/schedule  -> Schedule tab with one sub-tab per week
"""

from __future__ import annotations

# ---- Standard library -------------------------------------------------------
import asyncio  # Stagehand's Python API is async; we drive it with asyncio.run()
import json  # Serialize games.json (and the team-name list passed to the page)
import os  # Read env vars (XAI_API_KEY, HEADLESS, …)
import re  # Match week-tab labels (Week / TOURNAMENT / …)
import sys
import tempfile
import stat  # lstat / symlink checks for atomic output writes
from datetime import datetime, timezone  # scrapedAt timestamp (UTC ISO-8601)
from pathlib import Path  # Config / output paths without string concat
from typing import Any, Literal  # Typing for status enum + loose YAML/JSON bits
from urllib.parse import urlparse  # Validate leagueUrl is a real http(s) URL

# ---- Third-party ------------------------------------------------------------
import yaml  # PyYAML: parse config.yaml (safe_load only — plain data, never arbitrary objects)
from openai import AsyncOpenAI
import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from stagehand import LLMStructuredGenerateResult, Stagehand, browserbase, local_browser

# ---- Local helpers (lean HTTP API + LLM token accounting) -------------------
from lean_api import (
    LeanApiError,
    division_url as lean_division_url,
    games_for_teams,
    get_schedule_v2,
    get_standings,
    infer_api_base,
    list_seasons,
    pick_season_for_levels,
    standing_row_from_api,
)
from lean_http import CircuitTrippedError, HttpSettings, configure_http, get_client
from token_usage import USAGE, reset_usage

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# Directory that contains this module. Source checkout → python/; wheel/shiv →
# site-packages (or the zipapp), so we must NOT resolve the user's config.yaml
# relative to this path alone (Major M1).
ROOT = Path(__file__).resolve().parent

# True when we are sitting in the repo's python/ tree (editable / `python main.py`),
# not inside an installed wheel or shiv .pyz.
_RUNNING_FROM_SOURCE = (ROOT / "pyproject.toml").is_file() or (
    ROOT / "requirements.txt"
).is_file()

# Env var override for config path (after --config, before cwd).
CONFIG_ENV_VAR = "STAGEHAND_VOLLEYBALL_CONFIG"

# Repo-root config when running from a source checkout (python/../config.yaml).
PARENT_CONFIG = ROOT.parent / "config.yaml"
# Optional python/config.yaml override (source checkout only).
LOCAL_CONFIG = ROOT / "config.yaml"

# Snapshot used for logging until resolve_config_path() runs; load_config()
# always re-resolves.
CONFIG_PATH = PARENT_CONFIG if _RUNNING_FROM_SOURCE else (Path.cwd() / "config.yaml")

# Source checkout: write beside this script (python/games.json). Packaged/shiv:
# write ./games.json in the caller's cwd (site-packages is often read-only).
OUTPUT_PATH = (ROOT / "games.json") if _RUNNING_FROM_SOURCE else (Path.cwd() / "games.json")


def _unique_paths(paths: list[Path]) -> list[Path]:
    """Preserve order while dropping duplicate resolved paths."""
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        key = path.resolve() if path.exists() else path.absolute()
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def resolve_config_path(cli_path: str | Path | None = None) -> Path:
    """
    Locate config.yaml for both source checkouts and installed wheel/shiv runs.

    Precedence:
      1. --config PATH (CLI) — must exist; missing path is a hard error
      2. $STAGEHAND_VOLLEYBALL_CONFIG — must exist; missing path is a hard error
      3. ./config.yaml in the process cwd
      4. Repo-root ../config.yaml / python/config.yaml — only when running from
         a source checkout (pyproject.toml or requirements.txt beside this file)

    Explicit --config / env paths never fall through to cwd. Raises ConfigError
    listing every path tried when no automatic candidate exists.
    """
    # Explicit CLI path: require the file; do not silently use cwd instead.
    if cli_path is not None:
        path = Path(cli_path).expanduser()
        if not path.is_file():
            raise ConfigError(
                f"Config file from --config not found: {path}"
            )
        return path

    # Explicit env path: same hard-fail rule.
    env_path = (os.environ.get(CONFIG_ENV_VAR) or "").strip()
    if env_path:
        path = Path(env_path).expanduser()
        if not path.is_file():
            raise ConfigError(
                f"Config file from ${CONFIG_ENV_VAR} not found: {path}"
            )
        return path

    tried: list[Path] = []
    candidates: list[Path] = [Path.cwd() / "config.yaml"]

    if _RUNNING_FROM_SOURCE:
        # Repo root first (shared), then optional python/ override.
        candidates.append(PARENT_CONFIG)
        candidates.append(LOCAL_CONFIG)

    for path in _unique_paths(candidates):
        tried.append(path)
        if path.is_file():
            return path

    tried_lines = "\n".join(f"  - {p}" for p in tried) or "  (none)"
    raise ConfigError(
        "Could not find config.yaml. Tried:\n"
        f"{tried_lines}\n"
        "Create one with: cp config.example.yaml config.yaml\n"
        "Or pass --config PATH / set "
        f"{CONFIG_ENV_VAR}=PATH. "
        "Edit captainName and/or teamName, levels / siteUrl (or leagueUrl), "
        "and related fields. The scraper does not fall back to "
        "config.example.yaml."
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ConfigError(Exception):
    """Raised when config.yaml is missing, unreadable, not valid YAML, or fails validation."""


class MissingKeyError(Exception):
    """Raised when neither XAI_API_KEY nor BROWSERBASE_API_KEY is available."""


class ResolveError(Exception):
    """Raised when captainName / teamName matches no standings row."""


class WeekTabError(Exception):
    """
    Raised when a schedule week tab can't be selected (or the wrong tab ends up
    selected) even after a retry. We stop instead of extracting, because
    extract() would read whatever week is still on screen and save those games
    under the wrong label (or duplicate the previous week) with no error.
    """



def utc_now_iso() -> str:
    """UTC timestamp with millisecond precision, always ending in Z (no +00:00)."""
    now = datetime.now(timezone.utc)
    # Normalize to exactly 3 fractional digits so lean/llm runs compare cleanly.
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def omit_nulls(value: Any) -> Any:
    """
    Drop keys whose value is None (JSON null). Recurses into dicts/lists.
    Keeps empty strings, empty lists, False, and 0.
    """
    if isinstance(value, dict):
        return {k: omit_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [omit_nulls(v) for v in value]
    return value

def describe_error(err: BaseException) -> str:
    """
    Turn any exception into a non-empty, human-readable message.

    Some exceptions (e.g. a bare TimeoutError() or CancelledError()) have an
    empty str(), which would print a blank line and leave the user guessing.
    Fall back to repr() and always lead with the exception type name.
    """
    text = str(err).strip()
    if not text:
        # repr() is at least "TimeoutError()"; the type name is the last resort.
        text = repr(err) or type(err).__name__
    return text


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------

class HttpConfig(BaseModel):
    """Lean-mode HTTP policy (config.yaml → http:)."""

    connect_timeout_s: float = Field(default=5.0, alias="connectTimeoutS")
    read_timeout_s: float = Field(default=20.0, alias="readTimeoutS")
    max_retries: int = Field(default=4, alias="maxRetries")
    max_retries_total: int = Field(default=80, alias="maxRetriesTotal")
    backoff_base_s: float = Field(default=0.4, alias="backoffBaseS")
    backoff_max_s: float = Field(default=10.0, alias="backoffMaxS")
    jitter_s: float = Field(default=0.3, alias="jitterS")
    circuit_failure_threshold: int = Field(default=5, alias="circuitFailureThreshold")
    circuit_cooldown_s: float = Field(default=20.0, alias="circuitCooldownS")
    concurrency: int = Field(default=4, ge=1, le=32)
    user_agent: str = Field(
        default="stagehand-volleyball/lean (+https://github.com/rbaas293/stagehand-volleyball)",
        alias="userAgent",
    )

    model_config = {"populate_by_name": True}

    def to_settings(self) -> HttpSettings:
        return HttpSettings(
            connect_timeout_s=self.connect_timeout_s,
            read_timeout_s=self.read_timeout_s,
            max_retries=self.max_retries,
            max_retries_total=self.max_retries_total,
            backoff_base_s=self.backoff_base_s,
            backoff_max_s=self.backoff_max_s,
            jitter_s=self.jitter_s,
            circuit_failure_threshold=self.circuit_failure_threshold,
            circuit_cooldown_s=self.circuit_cooldown_s,
            concurrency=self.concurrency,
            user_agent=self.user_agent,
        )


class AppConfig(BaseModel):
    """User-editable knobs loaded from config.yaml."""

    # Captain(s) to search for on the standings page (preferred discovery path).
    # Accepts a single string or a list in YAML; normalized to list[str].
    captain_name: list[str] = Field(default_factory=list, alias="captainName")
    # Explicit team name; used only when captainName is empty/absent.
    team_name: str = Field(default="", alias="teamName")
    # Game-day label (single-division notes). Optional when levels[] drives multi-div.
    day: str = Field(default="")
    # Human-readable league / division path for notes. Optional when levels[] is set.
    league: str = Field(default="")
    # Single-division standings URL (backward compatible). Optional when levels[] is set.
    league_url: str = Field(default="", alias="leagueUrl")
    # Path appended to leagueUrl to reach the schedule tab (default "/schedule").
    schedule_path_suffix: str = Field(default="/schedule", alias="schedulePathSuffix")
    # xAI Grok model id (api.x.ai). Stagehand v4 has no native xAI provider;
    # we call Grok through a BYO LLM callback (OpenAI-compatible client).
    model: str = Field(default="grok-4-fast-reasoning", min_length=1)
    # Scraping mode: "lean" = pub-api HTTP (no LLM); "llm" = Stagehand extract per page.
    mode: Literal["lean", "llm"] = "lean"
    # When non-empty, discover + scrape every division whose league/division name
    # contains any of these substrings (e.g. "Beer A", "Beer B") across the current season.
    levels: list[str] = Field(default_factory=list)
    # Club site origin for building division URLs (multi-div). E.g. https://flannagans.league.ninja
    site_url: str = Field(default="", alias="siteUrl")
    # Optional override for the LMS pub API base. Inferred for known clubs from siteUrl/leagueUrl.
    api_base_url: str = Field(default="", alias="apiBaseUrl")
    # Optional season name or uid override for multi-div discovery (lean/llm).
    # When empty, pick an in-range season that has divisions matching levels[]
    # (skips empty overlaps like Fall before Beer leagues exist).
    season: str = Field(default="")
    # Lean HTTP robustness knobs (timeouts, retries, concurrency, circuit breaker).
    http: HttpConfig = Field(default_factory=HttpConfig)
    # When true, the xAI HTTP client honors env proxy settings (HTTP(S)_PROXY).
    # Default false so a poisoned proxy env cannot intercept API traffic.
    http_trust_env: bool = Field(default=False, alias="httpTrustEnv")

    # Allow reading camelCase YAML keys while exposing snake_case attributes in Python.
    model_config = {"populate_by_name": True}

    @field_validator("captain_name", mode="before")
    @classmethod
    def coerce_captain_names(cls, value: Any) -> list[str]:
        """Accept string or list; trim; drop empties. Backward-compatible with a single string."""
        if value is None:
            return []
        if isinstance(value, str):
            trimmed = value.strip()
            return [trimmed] if trimmed else []
        if isinstance(value, list):
            out: list[str] = []
            for item in value:
                s = str(item).strip()
                if s:
                    out.append(s)
            return out
        raise ValueError("captainName must be a string or a list of strings")

    @field_validator("levels", mode="before")
    @classmethod
    def coerce_levels(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            trimmed = value.strip()
            return [trimmed] if trimmed else []
        if isinstance(value, list):
            return [str(x).strip() for x in value if str(x).strip()]
        raise ValueError("levels must be a string or a list of strings")

    @field_validator("mode", mode="before")
    @classmethod
    def coerce_mode(cls, value: Any) -> str:
        if value is None or value == "":
            return "lean"
        s = str(value).strip().lower()
        if s not in ("lean", "llm"):
            raise ValueError('mode must be "lean" or "llm"')
        return s

    @field_validator("league_url")
    @classmethod
    def optional_http_url(cls, value: str) -> str:
        """Allow empty leagueUrl (multi-div via levels); validate when present."""
        stripped = (value or "").strip()
        if not stripped:
            return ""
        parsed = urlparse(stripped)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                "leagueUrl must be a non-empty http(s) URL, e.g. "
                "https://<club>.league.ninja/leagues/division/<id>"
            )
        return stripped

    @model_validator(mode="after")
    def require_captain_or_team_and_target(self) -> AppConfig:
        """Captain/team required; need leagueUrl and/or levels[] for what to scrape."""
        has_captain = len(self.captain_name) > 0
        has_team = bool(self.team_name.strip())
        if not has_captain and not has_team:
            raise ValueError(
                "Set captainName (string or list, to discover team(s) by captain) "
                "and/or teamName (explicit team when captainName is empty)."
            )
        has_levels = len(self.levels) > 0
        has_url = bool(self.league_url.strip())
        if not has_levels and not has_url:
            raise ValueError(
                "Set leagueUrl (single division) and/or levels (e.g. [Beer A, Beer B] "
                "to discover matching divisions for the current season)."
            )
        if has_levels and not has_url and not self.site_url.strip() and not self.api_base_url.strip():
            raise ValueError(
                "When using levels without leagueUrl, set siteUrl and/or apiBaseUrl "
                "so the scraper can reach the club pub API."
            )
        return self


# Env keys we will accept from .env files (shell exports always win and are unrestricted).
ALLOWED_ENV_KEYS = frozenset(
    {
        "XAI_API_KEY",
        "BROWSERBASE_API_KEY",
        "STAGEHAND_MODEL",
        "SCRAPE_MODE",
        "SCRAPE_LLM_PREFILTER",
        "HEADLESS",
        CONFIG_ENV_VAR,
    }
)
ALLOWED_ENV_PREFIXES = ("BROWSERBASE_",)


def _env_key_allowed(key: str) -> bool:
    """True if key is on the allow-list (exact or BROWSERBASE_* prefix)."""
    if key in ALLOWED_ENV_KEYS:
        return True
    return any(key.startswith(prefix) for prefix in ALLOWED_ENV_PREFIXES)


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE pairs from a .env file (no shell expansion)."""
    try:
        from dotenv import dotenv_values  # type: ignore

        raw = dotenv_values(path)
        return {k: v for k, v in raw.items() if k and v is not None}
    except ImportError:
        out: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                out[key] = value
        return out


def load_dotenv_files(env_file: str | Path | None = None) -> None:
    """
    Optionally load allow-listed keys from .env into os.environ.

    Sources (never auto-loads cwd `.env` for packaged installs):
      - `--env-file PATH` when provided (must exist)
      - `python/.env` then repo-root `../.env` when running from a source checkout

    Shell exports always win. Only known keys are applied from files
    (XAI_API_KEY, STAGEHAND_MODEL, SCRAPE_MODE, HEADLESS, BROWSERBASE_*, …).
    """
    candidates: list[Path] = []
    if env_file is not None:
        path = Path(env_file).expanduser()
        if not path.is_file():
            raise ConfigError(f"Env file from --env-file not found: {path}")
        candidates.append(path)
    elif _RUNNING_FROM_SOURCE:
        candidates.extend([ROOT / ".env", ROOT.parent / ".env"])

    for path in _unique_paths(candidates):
        if not path.is_file():
            continue
        parsed = _parse_env_file(path)
        for key, value in parsed.items():
            if not _env_key_allowed(key):
                continue
            # Never clobber an env var the user already exported or an earlier file set.
            if key not in os.environ:
                os.environ[key] = value



def load_config(cli_path: str | Path | None = None) -> AppConfig:
    """
    Read and validate config.yaml.

    Resolves the path via resolve_config_path() (CLI / env / cwd / source tree)
    on every call so wheel and shiv installs honor the caller's cwd and flags.
    """
    global CONFIG_PATH
    config_path = resolve_config_path(cli_path)
    CONFIG_PATH = config_path
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as err:
        raise ConfigError(
            f"Missing or unreadable config.yaml at {config_path} ({err}). "
            "Create one with: cp config.example.yaml config.yaml, "
            "or pass --config PATH / set "
            f"{CONFIG_ENV_VAR}=PATH."
        ) from err

    try:
        # safe_load (never yaml.load) builds only plain dicts / lists / strings /
        # numbers, so a config file can't construct arbitrary Python objects.
        # Comments in the YAML are simply ignored.
        parsed: Any = yaml.safe_load(raw)
    except yaml.YAMLError as err:
        raise ConfigError(f"config.yaml is not valid YAML: {err}") from err

    try:
        return AppConfig.model_validate(parsed)
    except ValidationError as err:
        # Flatten pydantic errors into one readable line.
        details = "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', ())) or '(root)'}: {e.get('msg')}"
            for e in err.errors()
        )
        raise ConfigError(
            f"config.yaml is invalid: {details}. Expected day, league, leagueUrl, "
            "plus captainName and/or teamName (and optional schedulePathSuffix)."
        ) from err


# ---------------------------------------------------------------------------
# Extract schemas (pydantic models for Stagehand extract())
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
    teams (helps the LLM filter schedule cards to our matched teams).
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
def normalize_captain(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — so 'R Baas' ≡ 'R. Baas'."""
    lowered = s.lower()
    # Drop everything except letters, digits, and spaces.
    cleaned = re.sub(r"[^a-z0-9\s]", "", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def captain_matches(row_captain: str | None, wanted: str) -> bool:
    """
    Exact captain match after normalizing punctuation and case.

    Tokens must be equal (no substring / partial matches).
    "R. Baas" ≡ "R Baas"; "Robinson" does NOT match "H. Robinson".
    """
    if not row_captain or not wanted:
        return False
    a = normalize_captain(row_captain)
    b = normalize_captain(wanted)
    if not a or not b:
        return False
    # Exact normalized-string equality ⇒ same token sequence.
    return a == b


def resolve_teams_from_standings(
    rows: list[StandingsRow],
    *,
    use_captain: bool,
    captain_names: list[str],
    config_team_name: str,
) -> tuple[list[StandingsRow], Literal["captain", "teamName"], list[dict[str, Any]]]:
    """
    Pick which standings rows we scrape games for.

    Returns (matched_rows, resolution_mode, captain_match_details).
    captain_match_details lists each query and the teams it matched (may be empty).
    """
    if use_captain:
        captain_match_details: list[dict[str, Any]] = []
        for query in captain_names:
            hits = [r for r in rows if captain_matches(r.captain_name, query)]
            captain_match_details.append({"query": query, "matchedTeams": hits})

        # Deduplicate teams matched by any query.
        seen: set[str] = set()
        matched: list[StandingsRow] = []
        for detail in captain_match_details:
            for row in detail["matchedTeams"]:
                key = row.team_name.lower()
                if key not in seen:
                    seen.add(key)
                    matched.append(row)

        if not matched:
            captains = ", ".join(r.captain_name for r in rows if r.captain_name) or "(none extracted)"
            wanted = ", ".join(json.dumps(n) for n in captain_names)
            raise ResolveError(
                f"No standings row matched captainName=[{wanted}]. "
                f"Captains seen: {captains}."
            )
        return matched, "captain", captain_match_details

    # Explicit teamName path (captainName empty / omitted).
    # Exact case-insensitive match only — no substring / "partial" matches.
    wanted = config_team_name.lower()
    matched = [r for r in rows if r.team_name.lower() == wanted]
    if not matched:
        names = ", ".join(r.team_name for r in rows) or "(none extracted)"
        raise ResolveError(
            f'No standings row matched teamName="{config_team_name}" (exact). '
            f"Teams seen: {names}."
        )
    return matched, "teamName", []



def try_resolve_teams_from_standings(
    rows: list[StandingsRow],
    *,
    use_captain: bool,
    captain_names: list[str],
    config_team_name: str,
) -> tuple[list[StandingsRow], Literal["captain", "teamName"] | None, list[dict[str, Any]]]:
    """
    Like resolve_teams_from_standings, but returns ([], None, details) when nothing matches
    instead of raising — used when scanning many divisions.
    """
    try:
        return resolve_teams_from_standings(
            rows,
            use_captain=use_captain,
            captain_names=captain_names,
            config_team_name=config_team_name,
        )
    except ResolveError:
        # Still return per-query empty details for captain mode.
        if use_captain:
            details = [{"query": q, "matchedTeams": []} for q in captain_names]
            return [], None, details
        return [], None, []

# ---------------------------------------------------------------------------
# Logging / browser setup
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    """Print progress to stderr so stdout stays clean JSON."""
    print(f"[stagehand-volleyball] {msg}", file=sys.stderr)


DEFAULT_GROK_MODEL = "grok-4-fast-reasoning"
XAI_BASE_URL = "https://api.x.ai/v1"


def resolve_grok_model_id(config_model: str | None = None) -> str:
    """
    Resolve the Grok model id: STAGEHAND_MODEL > config.model > default.

    Accepts an optional "xai/" provider prefix (from older Stagehand docs) and
    strips it before calling api.x.ai, which wants the bare model id.
    """
    raw = (os.environ.get("STAGEHAND_MODEL") or "").strip() or (
        (config_model or "").strip() or DEFAULT_GROK_MODEL
    )
    if raw.lower().startswith("xai/"):
        return raw[4:]
    return raw


def make_grok_generate(api_key: str, model_id: str, *, trust_env: bool = False):
    """
    Build a Stagehand BYO LLM callback that calls xAI Grok.

    Stagehand v4 first-class providers are only openai/anthropic/google/groq/
    cerebras. xAI is reached via the documented OpenAI-compatible BYO callback
    pointed at https://api.x.ai/v1 (XAI_API_KEY). See Stagehand v4 models docs
    ("bring your own LLM" / "OpenAI-compatible SDKs") and docs.x.ai.

    trust_env=False (default) ignores HTTP(S)_PROXY from the environment unless
    the user sets httpTrustEnv: true in config.yaml.
    """
    http_client = httpx.AsyncClient(trust_env=trust_env)
    client = AsyncOpenAI(api_key=api_key, base_url=XAI_BASE_URL, http_client=http_client)

    def content_part(block: Any) -> dict[str, Any]:
        # Each block is LLMTextContent or LLMImageContent under `.root`.
        root = getattr(block, "root", block)
        if getattr(root, "type", None) == "text":
            return {"type": "input_text", "text": root.text}
        return {
            "type": "input_image",
            "image_url": f"data:{root.mime_type};base64,{root.data}",
            "detail": "auto",
        }

    def message_content(message: Any) -> list[dict[str, Any]]:
        content = message.content
        blocks = content if isinstance(content, list) else [content]
        return [content_part(block) for block in blocks]

    async def generate_with_grok(params: Any) -> Any:
        response_format = params.response_format
        # Stagehand act/extract/observe issue structured (json_schema) generations.
        schema = response_format.schema_
        schema_payload = schema.model_dump() if hasattr(schema, "model_dump") else schema
        response = await client.responses.create(
            model=model_id,
            instructions=params.system_prompt,
            input=[
                {"role": message.role.value, "content": message_content(message)}
                for message in params.messages
            ],
            temperature=params.temperature,
            text={
                "format": {
                    "type": "json_schema",
                    "name": response_format.name,
                    "schema": schema_payload,
                    "strict": True,
                }
            },
        )
        # Record real prompt/completion/total tokens from the API usage field.
        USAGE.record(getattr(response, "usage", None), model=model_id)
        return LLMStructuredGenerateResult.model_validate(
            {
                "role": "assistant",
                "content": {"type": "text", "text": response.output_text},
                "output_format": "json_schema",
                "structured_content": json.loads(response.output_text),
            }
        )

    return generate_with_grok


def model_kwargs(
    config_model: str | None = None, *, trust_env: bool = False
) -> dict[str, Any]:
    """
    Build Stagehand.create() kwargs for the LLM.

    Local Chrome needs XAI_API_KEY (Grok BYO callback). Browserbase can use its
    Model Gateway when XAI_API_KEY is absent.
    """
    api_key = (os.environ.get("XAI_API_KEY") or "").strip()
    if api_key:
        model_id = resolve_grok_model_id(config_model)
        log(f'LLM: xAI Grok model="{model_id}" via {XAI_BASE_URL}')
        # Pass the generate callback as `model=` (Stagehand BYO LLM path).
        return {"model": make_grok_generate(api_key, model_id, trust_env=trust_env)}
    if os.environ.get("BROWSERBASE_API_KEY"):
        # Omit model — Browserbase Model Gateway picks one.
        return {}
    raise MissingKeyError(
        "XAI_API_KEY is not set. Export it, or put it in .env "
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
    Read week / tournament tabs from the *week* tablist only (not Standings/Schedule).

    Python Stagehand's page.evaluate() takes a JS *expression string* (no
    extra args), so the selector logic lives entirely inside the expression.
    """
    raw = await page.evaluate(
        """(() => {
          const isWeek = (t) => /LEAGUE ROUND|TOURNAMENT|PLAYOFF|Week/i.test(t);
          const lists = Array.from(document.querySelectorAll('[role=tablist]'));
          const weekList = lists.find((list) =>
            Array.from(list.querySelectorAll('[role=tab]')).some((el) =>
              isWeek(el.innerText)
            )
          );
          if (!weekList) return [];
          return Array.from(weekList.querySelectorAll('[role=tab]'))
            .map((el) => el.innerText.replace(/\\s*\\n\\s*/g, ' ').trim())
            .filter((t) => isWeek(t));
        })()"""
    )
    if not isinstance(raw, list):
        return []
    return [str(t) for t in raw if isinstance(t, str) and _WEEK_TAB_RE.search(t)]


def normalize_tab_label(text: str) -> str:
    """
    Canonical form for comparing tab labels: collapse every run of whitespace
    (spaces, newlines, tabs) to one space, trim, and casefold. So
    "Week 5\\n- Oct 4" and "week 5 - oct 4" compare equal.
    """
    return " ".join(str(text).split()).casefold()


async def click_tab_by_exact_text(page, label: str) -> bool:
    """
    Deterministic DOM click: find the week-tablist role=tab whose
    whitespace-normalized innerText equals `label` and click() it.

    Returns True if a single matching tab was found (and clicked).
    Raises WeekTabError if multiple tabs match and none is an exact
    (non-normalized) unique hit. Returns False if no tab matches.
    No LLM call, so it's fast and can't pick a "similar-looking" tab.
    """
    label_js = json.dumps(label)
    result = await page.evaluate(
        f"""(() => {{
          const wanted = {label_js};
          const norm = (t) => t.replace(/\\s+/g, ' ').trim().toLowerCase();
          const exact = (t) => t.replace(/\\s+/g, ' ').trim();
          const isWeek = (t) => /LEAGUE ROUND|TOURNAMENT|PLAYOFF|Week/i.test(t);
          const lists = Array.from(document.querySelectorAll('[role=tablist]'));
          const weekList = lists.find((list) =>
            Array.from(list.querySelectorAll('[role=tab]')).some((el) =>
              isWeek(el.innerText)
            )
          );
          if (!weekList) return {{ status: 'not_found' }};
          const tabs = Array.from(weekList.querySelectorAll('[role=tab]'));
          const matches = tabs.filter((el) => norm(el.innerText) === norm(wanted));
          if (matches.length === 0) return {{ status: 'not_found' }};
          if (matches.length > 1) {{
            const exactHits = matches.filter((el) => exact(el.innerText) === exact(wanted));
            if (exactHits.length === 1) {{
              exactHits[0].click();
              return {{ status: 'ok' }};
            }}
            return {{ status: 'ambiguous', count: matches.length }};
          }}
          matches[0].click();
          return {{ status: 'ok' }};
        }})()"""
    )
    if isinstance(result, dict) and result.get("status") == "ambiguous":
        raise WeekTabError(
            f'Ambiguous week tab label "{label}": '
            f'{result.get("count", "?")} tabs match after normalization; '
            "no unique exact-text hit. Refusing to click the wrong week."
        )
    if isinstance(result, dict):
        return result.get("status") == "ok"
    return bool(result)


async def selected_week_tab_labels(page) -> list[str]:
    """
    Return the text of selected tabs *inside the week tablist only*
    ([role=tab][aria-selected=true]), whitespace-collapsed.

    Scoped to the week tablist so Standings/Schedule selection is ignored.
    """
    raw = await page.evaluate(
        """(() => {
          const isWeek = (t) => /LEAGUE ROUND|TOURNAMENT|PLAYOFF|Week/i.test(t);
          const lists = Array.from(document.querySelectorAll('[role=tablist]'));
          const weekList = lists.find((list) =>
            Array.from(list.querySelectorAll('[role=tab]')).some((el) =>
              isWeek(el.innerText)
            )
          );
          if (!weekList) return [];
          return Array.from(weekList.querySelectorAll('[role=tab][aria-selected=true]'))
            .map((el) => el.innerText.replace(/\\s+/g, ' ').trim());
        })()"""
    )
    if not isinstance(raw, list):
        return []
    return [str(t) for t in raw if isinstance(t, str)]


async def wait_for_selected_tab(page, label: str, timeout_ms: int = 3_000) -> bool:
    """
    Poll until a selected *week* tab's text equals `label` (case- and
    whitespace-insensitive), or until timeout_ms elapses.
    """
    wanted = normalize_tab_label(label)
    step_ms = 250
    waited = 0
    while True:
        selected = await selected_week_tab_labels(page)
        if any(normalize_tab_label(t) == wanted for t in selected):
            return True
        if waited >= timeout_ms:
            return False
        await page.wait_for_timeout(step_ms)
        waited += step_ms


async def schedule_panel_signature(page) -> str:
    """Snapshot of visible schedule panel text (for content-change waits)."""
    raw = await page.evaluate(
        """(() => {
          const panel =
            document.querySelector('[role=tabpanel]') ||
            document.querySelector('main') ||
            document.body;
          return (panel && panel.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 6000);
        })()"""
    )
    return str(raw) if raw is not None else ""


async def wait_for_schedule_content_change(
    page, previous: str, timeout_ms: int = 5_000
) -> bool:
    """
    Poll until the schedule panel text differs from `previous`, or timeout.

    Replaces a fixed sleep after week-tab clicks so slow/fast renders both work.
    Returns False on timeout (caller may still proceed if the tab is selected).
    """
    step_ms = 200
    waited = 0
    while True:
        current = await schedule_panel_signature(page)
        if current != previous:
            return True
        if waited >= timeout_ms:
            return False
        await page.wait_for_timeout(step_ms)
        waited += step_ms


async def click_week_tab(stagehand: Stagehand, page, label: str) -> None:
    """
    Select a schedule week tab and VERIFY it is actually selected.

    Why verify: extract() reads whatever week is on screen. If a click silently
    fails or hits a neighboring tab, we'd save the previous week's games under
    this label (or duplicate them) with no error. So after clicking we confirm
    the week tablist's aria-selected tab text == label before returning.

    Strategy per attempt:
      1. Exact-text DOM click (deterministic): fast, no LLM.
      2. If the tab still isn't selected, fall back to stagehand.act()
         (natural-language click), which survives DOM changes where the tab
         text/markup no longer matches exactly. We also check act()'s own
         success flag so a failed action is reported, not ignored.

    We make 2 attempts (the initial try + one retry). If the tab is still not
    selected, raise WeekTabError with what we expected vs. what is selected.
    """
    max_attempts = 2  # initial attempt + one retry
    problems: list[str] = []  # Collected per-step failures for the final error

    for attempt in range(1, max_attempts + 1):
        # --- Step 1: deterministic exact-text click ----------------------------
        clicked = await click_tab_by_exact_text(page, label)
        if clicked:
            if await wait_for_selected_tab(page, label):
                return
            problems.append(f"attempt {attempt}: exact-text click did not select the tab")
        else:
            problems.append(f"attempt {attempt}: no tab with exact text found")

        # --- Step 2: act() fallback --------------------------------------------
        try:
            act_result = await stagehand.act(
                f'Click the schedule week tab labeled exactly "{label}". '
                "It is one of the tabs in the week/round tab list on the schedule page."
            )
            data = getattr(act_result, "data", None)
            if data is not None and getattr(data, "success", True) is False:
                problems.append(
                    f"attempt {attempt}: act() reported failure: "
                    f"{getattr(data, 'message', '') or 'no message'}"
                )
        except Exception as err:  # noqa: BLE001 — record and keep trying / raise below
            problems.append(f"attempt {attempt}: act() raised {describe_error(err)}")

        if await wait_for_selected_tab(page, label):
            return
        problems.append(f"attempt {attempt}: act() did not select the tab")

    selected = await selected_week_tab_labels(page)
    raise WeekTabError(
        f'Could not select schedule week tab "{label}" after {max_attempts} attempts. '
        f"Currently selected week tab(s): {selected or 'none'}. "
        f"Details: {'; '.join(problems)}. "
        "Stopping so games aren't saved under the wrong week."
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
# Division discovery (shared by lean + llm multi-div)
# ---------------------------------------------------------------------------
def discover_target_divisions(
    config: AppConfig,
) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
    """
    Return (season_name, divisions[], season_status).

    - If config.levels is non-empty: pick a season with posted data for those levels.
    - Else: single synthetic division from config.leagueUrl.
    """
    levels = list(config.levels)
    if not levels:
        # Single-division backward-compatible mode.
        league_url = config.league_url.rstrip("/")
        # Extract div uid from .../division/<uuid>
        m = re.search(r"/division/([0-9a-fA-F-]{36})", league_url)
        div_uid = m.group(1) if m else ""
        return (
            config.league or "",
            [
                {
                    "divisionUid": div_uid,
                    "divisionName": config.league or league_url,
                    "leagueName": config.league or "",
                    "dayOfWeek": config.day or "",
                    "seasonName": "",
                    "url": league_url,
                    "singleDivision": True,
                }
            ],
            None,
        )

    api_base = infer_api_base(
        api_base_url=config.api_base_url or None,
        site_url=config.site_url or None,
        league_url=config.league_url or None,
    )
    seasons = list_seasons(api_base)
    season, matched, reason, season_status = pick_season_for_levels(
        api_base,
        seasons,
        levels,
        season=config.season or None,
        probe_standings=get_standings,
        log_fn=log,
    )
    season_uid = season["uid"]
    season_name = season.get("name") or ""
    log(f"Lean/API discovery: api={api_base} season={season_name!r} ({season_uid})")
    log(f"  reason: {reason}")
    site = (config.site_url or config.league_url or "https://flannagans.league.ninja").strip()
    out: list[dict[str, Any]] = []
    for d in matched:
        uid = d.get("divisionUid") or ""
        out.append(
            {
                **d,
                "url": lean_division_url(site, uid) if uid else "",
                "singleDivision": False,
            }
        )
    log(f"levels={levels!r}: {len(out)} division(s) matched in {season_name!r}")
    return season_name, out, season_status


def _merge_captain_details(
    acc: list[dict[str, Any]], detail: dict[str, Any], *, div_meta: dict[str, Any]
) -> None:
    """Append matched teams (with division meta) into the accumulator keyed by query."""
    query = detail["query"]
    slot = next((x for x in acc if x["query"] == query), None)
    if slot is None:
        slot = {"query": query, "matchedTeams": []}
        acc.append(slot)
    for t in detail["matchedTeams"]:
        # t may be StandingsRow or dict
        if isinstance(t, StandingsRow):
            entry = {
                "teamName": t.team_name,
                "captainName": t.captain_name,
                "record": t.record,
                "standing": t.standing,
            }
        else:
            entry = {
                "teamName": t.get("teamName"),
                "captainName": t.get("captainName"),
                "record": t.get("record"),
                "standing": t.get("standing"),
            }
        entry.update(
            {
                "divisionName": div_meta.get("divisionName"),
                "leagueName": div_meta.get("leagueName"),
                "day": div_meta.get("dayOfWeek") or div_meta.get("day"),
                "url": div_meta.get("url"),
                "divisionUid": div_meta.get("divisionUid"),
            }
        )
        # Dedup by team+division
        key = (entry["teamName"] or "").lower(), entry.get("divisionUid")
        exists = any(
            ((m.get("teamName") or "").lower(), m.get("divisionUid")) == key
            for m in slot["matchedTeams"]
        )
        if not exists:
            slot["matchedTeams"].append(entry)


# ---------------------------------------------------------------------------
# Lean scrape (HTTP pub-api, no LLM)
# ---------------------------------------------------------------------------
def scrape_lean(config: AppConfig) -> dict[str, Any]:
    """Full lean path: discover divisions, standings + schedule via pub API."""
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    t0 = time.perf_counter()
    reset_usage(model="")  # lean makes no LLM calls
    configure_http(config.http.to_settings())
    captain_names = list(config.captain_name)
    config_team_name = config.team_name.strip()
    use_captain = len(captain_names) > 0
    workers = max(1, int(config.http.concurrency))

    if use_captain:
        log(f'Config: mode=lean captains={json.dumps(captain_names)} levels={config.levels!r} concurrency={workers}')
    else:
        log(f'Config: mode=lean team="{config_team_name}" levels={config.levels!r} concurrency={workers}')

    api_base = infer_api_base(
        api_base_url=config.api_base_url or None,
        site_url=config.site_url or None,
        league_url=config.league_url or None,
    )
    season_name, divisions, season_status = discover_target_divisions(config)

    divisions_scanned: list[dict[str, Any]] = []
    division_errors: list[dict[str, Any]] = []
    all_matched_teams: list[dict[str, Any]] = []
    captain_match_details: list[dict[str, Any]] = (
        [{"query": q, "matchedTeams": []} for q in captain_names] if use_captain else []
    )
    games: list[dict[str, Any]] = []
    schedules_not_posted: list[dict[str, Any]] = []
    resolution: str | None = None

    def _process_division(div: dict[str, Any]) -> dict[str, Any]:
        """Per-division isolation: failures stay local to this result dict."""
        uid = div.get("divisionUid") or ""
        if not uid and div.get("singleDivision") and config.league_url:
            m = re.search(r"/division/([0-9a-fA-F-]{36})", config.league_url)
            uid = m.group(1) if m else ""
        division_name = div.get("divisionName") or ""
        league_name = div.get("leagueName") or config.league or ""
        day = div.get("dayOfWeek") or config.day or ""
        result: dict[str, Any] = {
            "uid": uid,
            "division_name": division_name,
            "league_name": league_name,
            "day": day,
            "div": div,
            "error": None,
            "matched": [],
            "res": None,
            "details": [],
            "games": [],
            "schedules_not_posted": [],
            "meta": None,
            "scanned": False,
        }
        if not uid:
            msg = f"skip division with no uid: {division_name or '(unnamed)'}"
            result["error"] = {
                "divisionUid": None,
                "divisionName": division_name,
                "leagueName": league_name,
                "stage": "discover",
                "error": msg,
            }
            return result

        url = div.get("url") or lean_division_url(
            config.site_url or config.league_url or "https://flannagans.league.ninja", uid
        )
        meta = {
            "divisionUid": uid,
            "divisionName": division_name,
            "leagueName": league_name,
            "dayOfWeek": day,
            "url": url,
        }
        result["meta"] = meta
        result["scanned"] = True
        result["url"] = url

        try:
            rows_raw = get_standings(api_base, uid)
        except LeanApiError as err:
            result["error"] = {**meta, "stage": "standings", "error": str(err)}
            return result

        rows = [StandingsRow.model_validate(standing_row_from_api(r)) for r in rows_raw]
        matched, res, details = try_resolve_teams_from_standings(
            rows,
            use_captain=use_captain,
            captain_names=captain_names,
            config_team_name=config_team_name,
        )
        if not matched:
            return result
        result["matched"] = matched
        result["res"] = res
        result["details"] = details
        team_names = [tm.team_name for tm in matched]

        try:
            schedule = get_schedule_v2(api_base, uid)
        except LeanApiError as err:
            result["error"] = {**meta, "stage": "schedule", "error": str(err)}
            return result

        # Empty schedule body → not posted yet (not an error).
        if not schedule:
            for tm in matched:
                result["schedules_not_posted"].append(
                    {
                        "teamName": tm.team_name,
                        "divisionName": division_name,
                        "leagueName": league_name,
                        "divisionUid": uid,
                        "status": "schedule not posted yet",
                    }
                )
            return result

        div_games = games_for_teams(schedule, team_names)
        if matched and not div_games:
            for tm in matched:
                result["schedules_not_posted"].append(
                    {
                        "teamName": tm.team_name,
                        "divisionName": division_name,
                        "leagueName": league_name,
                        "divisionUid": uid,
                        "status": "schedule not posted yet",
                    }
                )
        for g in div_games:
            result["games"].append(
                {
                    **g,
                    "divisionName": division_name,
                    "leagueName": league_name,
                    "day": day,
                    "url": url,
                }
            )
        return result

    # Preserve input order when merging concurrent results.
    ordered_results: list[dict[str, Any] | None] = [None] * len(divisions)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(_process_division, div): idx for idx, div in enumerate(divisions)
        }
        for fut in as_completed(future_map):
            idx = future_map[fut]
            try:
                ordered_results[idx] = fut.result()
            except Exception as err:  # noqa: BLE001 — isolate unexpected worker crashes
                div = divisions[idx]
                ordered_results[idx] = {
                    "uid": div.get("divisionUid"),
                    "division_name": div.get("divisionName") or "",
                    "league_name": div.get("leagueName") or "",
                    "day": div.get("dayOfWeek") or "",
                    "div": div,
                    "error": {
                        "divisionUid": div.get("divisionUid"),
                        "divisionName": div.get("divisionName"),
                        "stage": "worker",
                        "error": describe_error(err),
                    },
                    "matched": [],
                    "res": None,
                    "details": [],
                    "games": [],
                    "schedules_not_posted": [],
                    "meta": None,
                    "scanned": False,
                }

    for result in ordered_results:
        if result is None:
            continue
        div = result["div"]
        meta = result.get("meta")
        if result.get("scanned") and meta:
            divisions_scanned.append(
                {**meta, "seasonName": div.get("seasonName") or season_name}
            )
        if result.get("error"):
            err = result["error"]
            log(
                f"  {err.get('stage', 'error')} failed for "
                f"{err.get('divisionName') or err.get('divisionUid')}: {err.get('error')}"
            )
            division_errors.append(err)
        matched = result.get("matched") or []
        if not matched:
            continue
        if result.get("res"):
            resolution = result["res"]
        for d in result.get("details") or []:
            if meta:
                _merge_captain_details(captain_match_details, d, div_meta=meta)
        for tm in matched:
            all_matched_teams.append(
                {
                    "teamName": tm.team_name,
                    "captainName": tm.captain_name,
                    "record": tm.record,
                    "standing": tm.standing,
                    "divisionName": result["division_name"],
                    "leagueName": result["league_name"],
                    "day": result["day"],
                    "url": result.get("url") or (meta or {}).get("url"),
                    "divisionUid": result["uid"],
                }
            )
        log(
            f"  matched in {result['division_name']}: "
            + ", ".join(f'"{tm.team_name}" (captain={tm.captain_name or "?"})' for tm in matched)
        )
        for snp in result.get("schedules_not_posted") or []:
            schedules_not_posted.append(snp)
            log(
                f"    -> schedule not posted yet for \"{snp.get('teamName')}\" "
                f"in {snp.get('divisionName')}"
            )
        div_games = result.get("games") or []
        games.extend(div_games)
        if div_games:
            log(f"    -> {len(div_games)} game(s)")

    season_hint = (
        f' Season scanned: {season_name or "(unknown)"}. '
        f'If this is the wrong season (e.g. Fall with no Beer A/B), set config '
        f'season to a name or uid (see config.example.yaml), e.g. season: "Summer III- 2026".'
    )
    http_stats_pre = get_client().stats
    circuit_blocked = [
        e
        for e in division_errors
        if "Circuit open" in str(e.get("error") or "")
        or "circuit" in str(e.get("error") or "").lower()
    ]
    if http_stats_pre.circuit_trips > 0 and (
        circuit_blocked or http_stats_pre.circuit_open_rejections > 0
    ):
        raise CircuitTrippedError(
            f"HTTP circuit breaker tripped ({http_stats_pre.circuit_trips} trip(s)); "
            f"{len(circuit_blocked) or http_stats_pre.circuit_open_rejections} division "
            f"request(s) failed while the circuit was open. Refusing to overwrite games.json."
        )
    if use_captain and not all_matched_teams:
        raise ResolveError(
            f"No standings row matched captainName={json.dumps(captain_names)} "
            f"across {len(divisions_scanned)} division(s).{season_hint}"
        )
    if not use_captain and not all_matched_teams:
        raise ResolveError(
            f'No standings row matched teamName="{config_team_name}" '
            f"across {len(divisions_scanned)} division(s).{season_hint}"
        )

    elapsed = time.perf_counter() - t0
    team_names_flat = [tm["teamName"] for tm in all_matched_teams]
    http_stats = get_client().stats.as_dict()
    output: dict[str, Any] = {
        "mode": "lean",
        "levels": config.levels or None,
        "seasonName": season_name or None,
        "seasonStatus": season_status,
        "divisionsScanned": divisions_scanned,
        "divisionErrors": division_errors or None,
        "schedulesNotPosted": schedules_not_posted or None,
        "captainSearched": (
            (captain_names[0] if len(captain_names) == 1 else captain_names)
            if use_captain
            else None
        ),
        "captainMatchDetails": captain_match_details if use_captain else None,
        "resolution": resolution or ("captain" if use_captain else "teamName"),
        "matchedTeams": all_matched_teams,
        "team": team_names_flat[0] if len(team_names_flat) == 1 else team_names_flat,
        "day": config.day or None,
        "league": config.league or None,
        "url": config.league_url or None,
        "scrapedAt": utc_now_iso(),
        "games": games,
        "tokenUsage": USAGE.as_dict(),
        "runtimeSeconds": round(elapsed, 3),
        "http": http_stats,
    }
    if len(all_matched_teams) == 1:
        output["teamRecord"] = all_matched_teams[0].get("record")
        output["teamStanding"] = all_matched_teams[0].get("standing")
        output["leagueName"] = all_matched_teams[0].get("leagueName")
        output["divisionName"] = all_matched_teams[0].get("divisionName")
    USAGE.print_summary(log)
    for line in get_client().stats.log_lines():
        log(line)
    log(f"Lean scrape finished in {elapsed:.2f}s (concurrency={workers})")
    return output



# ---------------------------------------------------------------------------
# LLM scrape (Stagehand extract) — single or multi division
# ---------------------------------------------------------------------------
async def scrape_llm_division(
    stagehand: Stagehand,
    page,
    *,
    league_url: str,
    schedule_url: str,
    use_captain: bool,
    captain_names: list[str],
    config_team_name: str,
    day: str,
    league: str,
    div_meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    Scrape one division with Stagehand. Returns a partial result dict, or None if
    no teams matched (multi-div soft miss). Raises ResolveError in single-div mode
    when div_meta is None / singleDivision.
    """
    single = not div_meta or div_meta.get("singleDivision")
    log(f"Opening standings: {league_url}")
    await page.goto(league_url, wait_until="networkidle", timeout=60_000)
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

    if single:
        matched_teams, resolution, captain_match_details = resolve_teams_from_standings(
            standings.rows or [],
            use_captain=use_captain,
            captain_names=captain_names,
            config_team_name=config_team_name,
        )
    else:
        matched_teams, resolution, captain_match_details = try_resolve_teams_from_standings(
            standings.rows or [],
            use_captain=use_captain,
            captain_names=captain_names,
            config_team_name=config_team_name,
        )
        if not matched_teams:
            return None

    team_names = [t.team_name for t in matched_teams]
    log(
        "Resolved via "
        + (resolution or "?")
        + ": "
        + ", ".join(
            f'"{t.team_name}" (captain={t.captain_name or "?"})' for t in matched_teams
        )
    )

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
        previous_sig = await schedule_panel_signature(page)
        await click_week_tab(stagehand, page, label)
        # Wait for the schedule panel to refresh (or timeout); no fixed sleep.
        await wait_for_schedule_content_change(page, previous_sig, timeout_ms=5_000)

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
        for g in week_games:
            if not g.week:
                g.week = label
            if not g.team and len(team_names) == 1:
                g.team = team_names[0]
            games.append(g)

    meta = div_meta or {}
    day_out = meta.get("dayOfWeek") or day
    league_name_out = meta.get("leagueName") or standings.league_name or league
    division_name_out = meta.get("divisionName") or standings.division_name

    return {
        "matchedTeams": matched_teams,
        "resolution": resolution,
        "captainMatchDetails": captain_match_details,
        "games": games,
        "roundsWithoutTeamGames": rounds_without_team_games,
        "leagueName": standings.league_name,
        "divisionName": standings.division_name,
        "day": day_out,
        "leagueNameResolved": league_name_out,
        "divisionNameResolved": division_name_out,
        "url": league_url,
        "meta": meta,
    }


async def scrape_llm(config: AppConfig) -> dict[str, Any]:
    """Stagehand LLM extract path (single or multi-division)."""
    import time

    t0 = time.perf_counter()
    configure_http(config.http.to_settings())
    model_id = resolve_grok_model_id(config.model)
    reset_usage(model=model_id)

    captain_names = list(config.captain_name)
    config_team_name = config.team_name.strip()
    use_captain = len(captain_names) > 0

    if use_captain:
        ignored = (
            f' (config teamName="{config_team_name}" ignored while captainName is set)'
            if config_team_name
            else ""
        )
        log(
            f'Config: mode=llm captains={json.dumps(captain_names)}{ignored} '
            f'levels={config.levels!r} model={model_id!r}'
        )
    else:
        log(f'Config: mode=llm team="{config_team_name}" levels={config.levels!r} model={model_id!r}')

    create_kwargs = model_kwargs(config.model, trust_env=config.http_trust_env)
    season_name, divisions, season_status = discover_target_divisions(config)

    suffix = config.schedule_path_suffix
    if not suffix.startswith("/"):
        suffix = f"/{suffix}"

    browser = await launch_browser()
    try:
        stagehand = await Stagehand.create(browser=browser, **create_kwargs)
        try:
            pages = await browser.context.pages()
            page = pages[0]

            all_matched: list[dict[str, Any]] = []
            captain_match_details: list[dict[str, Any]] = (
                [{"query": q, "matchedTeams": []} for q in captain_names] if use_captain else []
            )
            all_games: list[dict[str, Any]] = []
            rounds_without: list[str] = []
            week_tab_errors: list[dict[str, Any]] = []
            divisions_scanned: list[dict[str, Any]] = []
            resolution: str | None = None
            last_standings_meta: dict[str, Any] = {}

            # Multi-div LLM: HTTP-prefilter standings so we only launch Stagehand
            # extracts on divisions that actually match a captain/team (avoids
            # dozens of useless LLM standings calls). Disable with SCRAPE_LLM_PREFILTER=0.
            prefilter = os.environ.get("SCRAPE_LLM_PREFILTER", "1").strip().lower() not in (
                "0",
                "false",
                "no",
            )
            if prefilter and config.levels and not any(d.get("singleDivision") for d in divisions):
                api_base = infer_api_base(
                    api_base_url=config.api_base_url or None,
                    site_url=config.site_url or None,
                    league_url=config.league_url or None,
                )
                kept: list[dict[str, Any]] = []
                for div in divisions:
                    uid = div.get("divisionUid") or ""
                    if not uid:
                        continue
                    try:
                        rows_raw = get_standings(api_base, uid)
                    except LeanApiError as err:
                        log(f"  prefilter standings failed {uid}: {err}")
                        continue
                    rows = [
                        StandingsRow.model_validate(standing_row_from_api(r)) for r in rows_raw
                    ]
                    matched, _, _ = try_resolve_teams_from_standings(
                        rows,
                        use_captain=use_captain,
                        captain_names=captain_names,
                        config_team_name=config_team_name,
                    )
                    if matched:
                        kept.append(div)
                log(
                    f"LLM prefilter: {len(kept)}/{len(divisions)} divisions have "
                    f"captain/team matches (HTTP standings); Stagehand will scrape those only"
                )
                divisions = kept

            for div in divisions:
                uid = div.get("divisionUid") or ""
                league_url = (div.get("url") or config.league_url or "").rstrip("/")
                if not league_url and uid:
                    league_url = lean_division_url(
                        config.site_url or "https://flannagans.league.ninja", uid
                    )
                if not league_url:
                    continue
                schedule_url = f"{league_url}{suffix}"
                meta = {
                    "divisionUid": uid,
                    "divisionName": div.get("divisionName"),
                    "leagueName": div.get("leagueName"),
                    "dayOfWeek": div.get("dayOfWeek") or config.day,
                    "url": league_url,
                    "singleDivision": bool(div.get("singleDivision")),
                    "seasonName": div.get("seasonName") or season_name,
                }
                divisions_scanned.append(meta)

                try:
                    partial = await scrape_llm_division(
                        stagehand,
                        page,
                        league_url=league_url,
                        schedule_url=schedule_url,
                        use_captain=use_captain,
                        captain_names=captain_names,
                        config_team_name=config_team_name,
                        day=config.day,
                        league=config.league,
                        div_meta=meta,
                    )
                except WeekTabError as err:
                    # Multi-div: record and continue. Single-div: re-raise.
                    if meta.get("singleDivision") or len(divisions) == 1:
                        raise
                    log(
                        f"  week-tab error in {meta.get('divisionName') or uid}: "
                        f"{describe_error(err)} — continuing other divisions"
                    )
                    week_tab_errors.append(
                        {
                            "divisionUid": uid,
                            "divisionName": meta.get("divisionName"),
                            "url": league_url,
                            "error": describe_error(err),
                        }
                    )
                    continue
                if partial is None:
                    continue

                resolution = partial["resolution"] or resolution
                last_standings_meta = partial
                for d in partial["captainMatchDetails"]:
                    _merge_captain_details(captain_match_details, d, div_meta=meta)
                for t in partial["matchedTeams"]:
                    all_matched.append(
                        {
                            "teamName": t.team_name,
                            "captainName": t.captain_name,
                            "record": t.record,
                            "standing": t.standing,
                            "divisionName": meta.get("divisionName") or partial.get("divisionName"),
                            "leagueName": meta.get("leagueName") or partial.get("leagueName"),
                            "day": meta.get("dayOfWeek") or config.day,
                            "url": league_url,
                            "divisionUid": uid,
                        }
                    )
                for g in partial["games"]:
                    gd = g.model_dump(by_alias=False)
                    gd.update(
                        {
                            "divisionName": meta.get("divisionName") or partial.get("divisionName"),
                            "leagueName": meta.get("leagueName") or partial.get("leagueName"),
                            "day": meta.get("dayOfWeek") or config.day,
                            "url": league_url,
                        }
                    )
                    all_games.append(gd)
                rounds_without.extend(partial.get("roundsWithoutTeamGames") or [])

            season_hint = (
                f' Season scanned: {season_name or "(unknown)"}. '
                f'If this is the wrong season (e.g. Fall with no Beer A/B), set config '
                f'season to a name or uid (see config.example.yaml), e.g. season: "Summer III- 2026".'
            )
            if use_captain and not all_matched:
                raise ResolveError(
                    f"No standings row matched captainName={json.dumps(captain_names)} "
                    f"across {len(divisions_scanned)} division(s).{season_hint}"
                )
            if not use_captain and not all_matched:
                raise ResolveError(
                    f'No standings row matched teamName="{config_team_name}" '
                    f"across {len(divisions_scanned)} division(s).{season_hint}"
                )

            elapsed = time.perf_counter() - t0
            team_names_flat = [t["teamName"] for t in all_matched]
            output: dict[str, Any] = {
                "mode": "llm",
                "levels": config.levels or None,
                "seasonName": season_name or None,
                "seasonStatus": season_status,
                "divisionsScanned": divisions_scanned,
                "captainSearched": (
                    (captain_names[0] if len(captain_names) == 1 else captain_names)
                    if use_captain
                    else None
                ),
                "captainMatchDetails": captain_match_details if use_captain else None,
                "resolution": resolution or ("captain" if use_captain else "teamName"),
                "matchedTeams": all_matched,
                "team": team_names_flat[0] if len(team_names_flat) == 1 else team_names_flat,
                "day": config.day or None,
                "league": config.league or None,
                "url": config.league_url or None,
                "scrapedAt": utc_now_iso(),
                "leagueName": last_standings_meta.get("leagueName"),
                "divisionName": last_standings_meta.get("divisionName"),
                "games": all_games,
                "roundsWithoutTeamGames": rounds_without,
                "weekTabErrors": week_tab_errors or None,
                "tokenUsage": USAGE.as_dict(),
                "runtimeSeconds": round(elapsed, 3),
            }
            if len(all_matched) == 1:
                output["teamRecord"] = all_matched[0].get("record")
                output["teamStanding"] = all_matched[0].get("standing")
            USAGE.print_summary(log)
            log(f"LLM scrape finished in {elapsed:.2f}s")
            return output
        finally:
            await stagehand.close()
    finally:
        await browser.close()


async def scrape(
    cli_config: str | Path | None = None,
    env_file: str | Path | None = None,
) -> dict[str, Any]:
    """
    End-to-end scrape. Returns the output dict that we also write to games.json.

    mode=lean → HTTP pub-api (no browser / no LLM)
    mode=llm  → Stagehand extract (browser + Grok BYO callback)
    Override mode with env SCRAPE_MODE=lean|llm.
    cli_config is an optional --config PATH from the CLI.
    env_file is an optional --env-file PATH from the CLI.
    """
    load_dotenv_files(env_file)
    config = load_config(cli_config)
    mode = (os.environ.get("SCRAPE_MODE") or config.mode or "lean").strip().lower()
    if mode not in ("lean", "llm"):
        raise ConfigError(f'Invalid mode {mode!r}; use "lean" or "llm"')
    # Mutate a copy so env override wins without rewriting the file.
    if mode != config.mode:
        log(f"SCRAPE_MODE override: {config.mode} → {mode}")
        config = config.model_copy(update={"mode": mode})

    log(f"Config path:   {CONFIG_PATH}")
    if config.mode == "lean":
        return scrape_lean(config)
    return await scrape_llm(config)



def write_output_atomic(target: Path, text: str) -> None:
    """
    Write games.json atomically and refuse to follow a symlink at the target.

    Creates a temp file in the same directory, then os.replace(). If `target`
    already exists as a symlink, raise ConfigError instead of writing through it.
    """
    target = Path(target)
    parent = target.parent if str(target.parent) else Path.cwd()
    parent.mkdir(parents=True, exist_ok=True)

    try:
        st = os.lstat(target)
        if stat.S_ISLNK(st.st_mode):
            raise ConfigError(
                f"Refusing to write output through symlink: {target}"
            )
    except FileNotFoundError:
        pass

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def parse_args(argv: list[str] | None = None) -> Any:
    """CLI flags shared by `python main.py` and the stagehand-volleyball console script."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="stagehand-volleyball",
        description=(
            "Scrape league.ninja volleyball schedules (lean HTTP or Stagehand LLM). "
            "Config resolution: --config, $"
            + CONFIG_ENV_VAR
            + ", ./config.yaml, then repo-root config.yaml when running from source."
        ),
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="Path to config.yaml (overrides env and cwd lookup)",
    )
    parser.add_argument(
        "--env-file",
        metavar="PATH",
        help=(
            "Optional .env file to load (allow-listed keys only). "
            "Cwd .env is not auto-loaded; source checkouts still load python/.env "
            "and repo-root .env when this flag is omitted."
        ),
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help=(
            "Load and validate config, print the resolved path and mode, then exit "
            "(no network / browser). Useful for packaging smoke tests."
        ),
    )
    return parser.parse_args(argv)


async def async_main(
    cli_config: str | Path | None = None,
    env_file: str | Path | None = None,
) -> None:
    """Run scrape(), print JSON to stdout, write games.json."""
    global OUTPUT_PATH
    # Recompute each run so packaged binaries write into the caller's cwd.
    OUTPUT_PATH = (ROOT / "games.json") if _RUNNING_FROM_SOURCE else (Path.cwd() / "games.json")
    output = omit_nulls(await scrape(cli_config, env_file))
    json_text = json.dumps(output, indent=2)
    print(json_text)
    write_output_atomic(OUTPUT_PATH, json_text + "\n")
    n_games = len(output.get("games") or [])
    n_teams = len(output.get("matchedTeams") or [])
    log(f"Wrote {n_games} games for {n_teams} team(s) to {OUTPUT_PATH}")


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint: translate known errors into clean stderr + exit 1."""
    try:
        args = parse_args(argv)
        if args.check_config:
            load_dotenv_files(args.env_file)
            config = load_config(args.config)
            print(f"config: {CONFIG_PATH}")
            print(f"mode: {config.mode}")
            print(f"captainName: {config.captain_name!r}")
            print(f"teamName: {config.team_name!r}")
            return
        asyncio.run(async_main(args.config, args.env_file))
    except (ConfigError, MissingKeyError, ResolveError, WeekTabError, CircuitTrippedError, LeanApiError) as err:
        # Friendly one-liners for config / key / resolve / week-tab failures.
        # describe_error() never returns an empty string.
        print(describe_error(err), file=sys.stderr)
        sys.exit(1)
    except Exception as err:  # noqa: BLE001 — surface unexpected failures fully
        # Prefix the type name ("KeyError: 'x'") so unexpected failures are
        # identifiable. When str(err) was empty, describe_error() already
        # returned repr(err) (e.g. "TimeoutError()"), so don't repeat the name.
        msg = describe_error(err)
        print(msg if msg == repr(err) else f"{type(err).__name__}: {msg}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
