#!/usr/bin/env python3
"""
Stagehand v4 (Python) scraper: volleyball game times for a team (or captain) on league.ninja.

This mirrors the TypeScript scraper in ../index.ts:
  1. Load config.yaml (captainName / teamName / day / league / leagueUrl).
  2. Launch a local Chrome browser (or Browserbase cloud browser).
  3. Open standings → extract rows with pydantic → resolve team(s) by captain or team name.
  4. Open schedule → click each week tab (exact text, act() fallback), verify it is
     selected → extract() that week's games.
  5. Write games.json next to this script (and print JSON to stdout).

Run (from this folder):
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  export XAI_API_KEY=...   # or put it in ../.env / .env
  python main.py

Env:
  XAI_API_KEY          required for local-Chrome mode (Grok via BYO LLM callback)
  BROWSERBASE_API_KEY  optional: run in a Browserbase cloud browser instead
  STAGEHAND_MODEL      optional, overrides config.yaml model (default "grok-4-fast-reasoning")
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
import json             # Serialize games.json (and the team-name list passed to the page)
import os               # Read env vars (XAI_API_KEY, HEADLESS, …)
import re               # Match week-tab labels (Week / TOURNAMENT / …)
import sys              # Exit codes + stderr logging
from urllib.parse import urlparse        # Validate leagueUrl is a real http(s) URL
from datetime import datetime, timezone  # scrapedAt timestamp (UTC ISO-8601)
from pathlib import Path                 # Config / output paths without string concat
from typing import Any, Literal          # Typing for status enum + loose YAML/JSON bits

# ---- Third-party ------------------------------------------------------------
import yaml  # PyYAML: parse config.yaml (safe_load only — plain data, never arbitrary objects)
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from stagehand import LLMStructuredGenerateResult, Stagehand, browserbase, local_browser

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# Directory that contains this script (python/).
ROOT = Path(__file__).resolve().parent

# Prefer the shared repo-root config so one config.yaml drives both TS and Python.
# Fall back to python/config.yaml if someone wants a Python-only override.
PARENT_CONFIG = ROOT.parent / "config.yaml"
LOCAL_CONFIG = ROOT / "config.yaml"
CONFIG_PATH = PARENT_CONFIG if PARENT_CONFIG.is_file() else LOCAL_CONFIG

# Output lands next to this script so TS games.json and Python games.json don't clash.
OUTPUT_PATH = ROOT / "games.json"


# ---------------------------------------------------------------------------
# Errors (matched to the TypeScript ConfigError / MissingKeyError / ResolveError)
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
# Config model (mirrors ConfigSchema in index.ts)
# ---------------------------------------------------------------------------
class AppConfig(BaseModel):
    """User-editable knobs loaded from config.yaml."""

    # Captain(s) to search for on the standings page (preferred discovery path).
    # Accepts a single string or a list in YAML; normalized to list[str].
    captain_name: list[str] = Field(default_factory=list, alias="captainName")
    # Explicit team name; used only when captainName is empty/absent.
    team_name: str = Field(default="", alias="teamName")
    # Game-day label stored in the output for convenience (e.g. "Sunday").
    day: str = Field(min_length=1)
    # Human-readable league / division path for your own notes.
    league: str = Field(min_length=1)
    # Division standings URL; the script appends schedulePathSuffix for the schedule page.
    # min_length=1 rejects "" outright; the validator below also rejects
    # whitespace-only / non-URL values (zod's .url() does the same in index.ts).
    league_url: str = Field(min_length=1, alias="leagueUrl")
    # Path appended to leagueUrl to reach the schedule tab (default "/schedule").
    schedule_path_suffix: str = Field(default="/schedule", alias="schedulePathSuffix")
    # xAI Grok model id (api.x.ai). Stagehand v4 has no native xAI provider;
    # we call Grok through a BYO LLM callback (OpenAI-compatible client).
    model: str = Field(default="grok-4-fast-reasoning", min_length=1)

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

    @field_validator("league_url")
    @classmethod
    def require_http_url(cls, value: str) -> str:
        """Reject empty / whitespace-only / non-http(s) leagueUrl values."""
        stripped = value.strip()
        parsed = urlparse(stripped)
        # Need both a scheme (http/https) and a host, e.g. https://x.league.ninja/...
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                "leagueUrl must be a non-empty http(s) URL, e.g. "
                "https://<club>.league.ninja/leagues/division/<id>"
            )
        return stripped

    @model_validator(mode="after")
    def require_captain_or_team(self) -> "AppConfig":
        """Same rule as the TS superRefine: at least one of captain/team must be set."""
        has_captain = len(self.captain_name) > 0
        has_team = bool(self.team_name.strip())
        if not has_captain and not has_team:
            raise ValueError(
                "Set captainName (string or list, to discover team(s) by captain) "
                "and/or teamName (explicit team when captainName is empty)."
            )
        return self


def load_dotenv_files() -> None:
    """
    Optionally load python/.env and ../.env into os.environ. Keeps secrets out
    of the repo; .env is gitignored.

    Precedence (highest first): shell exports > python/.env > ../.env (repo root).
    Every load is "first one wins" (never overrides a var that's already set),
    so we load the higher-priority file FIRST: python/.env, then the root .env
    only fills in whatever is still missing.
    """
    # Try python-dotenv if installed; otherwise do a tiny manual parser so the
    # scraper still works without python-dotenv (only stagehand + PyYAML needed).
    # Order matters: python/.env before ../.env (see precedence above).
    candidates = [ROOT / ".env", ROOT.parent / ".env"]
    try:
        from dotenv import load_dotenv  # type: ignore

        for path in candidates:
            if path.is_file():
                # override=False: shell exports (and earlier files) win.
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
            # Never clobber an env var the user exported or an earlier file set.
            if key and key not in os.environ:
                os.environ[key] = value


def load_config() -> AppConfig:
    """Read and validate config.yaml (parent preferred, then python/config.yaml)."""
    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError as err:
        raise ConfigError(
            f"Missing or unreadable config.yaml at {CONFIG_PATH} ({err}). "
            "Copy config.example.yaml to config.yaml at the repo root and edit "
            "captainName and/or teamName, day, league, and leagueUrl."
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
        # Flatten pydantic errors into one readable line (like the TS zod issues join).
        details = "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', ())) or '(root)'}: {e.get('msg')}"
            for e in err.errors()
        )
        raise ConfigError(
            f"config.yaml is invalid: {details}. Expected day, league, leagueUrl, "
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
def normalize_captain(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — so 'R Baas' ≡ 'R. Baas'."""
    lowered = s.lower()
    # Drop everything except letters, digits, and spaces.
    cleaned = re.sub(r"[^a-z0-9\s]", "", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def captain_matches(row_captain: str | None, wanted: str) -> bool:
    """
    Case- and punctuation-insensitive partial match (same as index.ts).
    "Robinson" matches "H. Robinson"; "R Baas" matches "R. Baas".
    """
    if not row_captain or not wanted:
        return False
    a = normalize_captain(row_captain)
    b = normalize_captain(wanted)
    if not a or not b:
        return False
    return a == b or a in b or b in a


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
        return partial, "teamName", []
    return matched, "teamName", []


# ---------------------------------------------------------------------------
# Logging / browser setup
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    """Print progress to stderr so stdout stays clean JSON (same as TS)."""
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


def make_grok_generate(api_key: str, model_id: str):
    """
    Build a Stagehand BYO LLM callback that calls xAI Grok.

    Stagehand v4 first-class providers are only openai/anthropic/google/groq/
    cerebras. xAI is reached via the documented OpenAI-compatible BYO callback
    pointed at https://api.x.ai/v1 (XAI_API_KEY). See Stagehand v4 models docs
    ("bring your own LLM" / "OpenAI-compatible SDKs") and docs.x.ai.
    """
    client = AsyncOpenAI(api_key=api_key, base_url=XAI_BASE_URL)

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
        return LLMStructuredGenerateResult.model_validate(
            {
                "role": "assistant",
                "content": {"type": "text", "text": response.output_text},
                "output_format": "json_schema",
                "structured_content": json.loads(response.output_text),
            }
        )

    return generate_with_grok


def model_kwargs(config_model: str | None = None) -> dict[str, Any]:
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
        return {"model": make_grok_generate(api_key, model_id)}
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


def normalize_tab_label(text: str) -> str:
    """
    Canonical form for comparing tab labels: collapse every run of whitespace
    (spaces, newlines, tabs) to one space, trim, and casefold. So
    "Week 5\n- Oct 4" and "week 5 - oct 4" compare equal.
    """
    return " ".join(str(text).split()).casefold()


async def click_tab_by_exact_text(page, label: str) -> bool:
    """
    Deterministic click, same as the TypeScript scraper: find the role=tab
    whose whitespace-normalized innerText equals `label` and click() it.

    Returns True if a matching tab element was found (and clicked), else False.
    No LLM call, so it's fast and can't pick a "similar-looking" tab.
    """
    # json.dumps gives a safely quoted JS string literal for the label.
    label_js = json.dumps(label)
    found = await page.evaluate(
        f"""(() => {{
          const wanted = {label_js};
          const norm = (t) => t.replace(/\\s+/g, ' ').trim().toLowerCase();
          const tab = Array.from(document.querySelectorAll('[role=tablist] [role=tab]'))
            .find((el) => norm(el.innerText) === norm(wanted));
          if (!tab) return false;
          tab.click();
          return true;
        }})()"""
    )
    return bool(found)


async def selected_tab_labels(page) -> list[str]:
    """
    Return the text of every currently selected tab
    ([role=tab][aria-selected=true]), whitespace-collapsed.

    The page can have more than one tablist (e.g. Standings/Schedule at the
    top plus the week tabs), so there may be several selected tabs; the
    caller checks whether ANY of them is the week we asked for.
    """
    raw = await page.evaluate(
        """(() => {
          return Array.from(document.querySelectorAll('[role=tab][aria-selected=true]'))
            .map((el) => el.innerText.replace(/\\s+/g, ' ').trim());
        })()"""
    )
    if not isinstance(raw, list):
        return []
    return [str(t) for t in raw if isinstance(t, str)]


async def wait_for_selected_tab(page, label: str, timeout_ms: int = 3_000) -> bool:
    """
    Poll until a selected tab's text equals `label` (case- and
    whitespace-insensitive), or until timeout_ms elapses.

    Polling (instead of one fixed sleep) tolerates slow React re-renders
    without waiting the full timeout when the tab switches quickly.
    """
    wanted = normalize_tab_label(label)
    step_ms = 250
    waited = 0
    while True:
        selected = await selected_tab_labels(page)
        if any(normalize_tab_label(t) == wanted for t in selected):
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
    [role=tab][aria-selected=true] text == label before returning.

    Strategy per attempt:
      1. Exact-text DOM click (same as the TypeScript version): fast, no LLM.
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
        # --- Step 1: deterministic exact-text click (TS parity) ---------------
        if await click_tab_by_exact_text(page, label):
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
            # ActResult.data.success / .message (Stagehand v4 Python SDK).
            # getattr keeps this tolerant of minor SDK shape changes.
            data = getattr(act_result, "data", None)
            if data is not None and getattr(data, "success", True) is False:
                problems.append(
                    f"attempt {attempt}: act() reported failure: "
                    f"{getattr(data, 'message', '') or 'no message'}"
                )
        except Exception as err:  # noqa: BLE001 — record and keep trying / raise below
            problems.append(f"attempt {attempt}: act() raised {describe_error(err)}")

        # Trust the DOM, not act()'s self-report: only a matching selected tab counts.
        if await wait_for_selected_tab(page, label):
            return
        problems.append(f"attempt {attempt}: act() did not select the tab")

    # Out of attempts: report what IS selected so the mismatch is obvious.
    selected = await selected_tab_labels(page)
    raise WeekTabError(
        f'Could not select schedule week tab "{label}" after {max_attempts} attempts. '
        f"Currently selected tab(s): {selected or 'none'}. "
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
# Main scrape flow
# ---------------------------------------------------------------------------
async def scrape() -> dict[str, Any]:
    """
    End-to-end scrape. Returns the output dict that we also write to games.json.
    """
    # Load .env before reading keys / HEADLESS so local runs Just Work.
    load_dotenv_files()
    config = load_config()

    # Normalize the discovery knobs once (captain_name is already a list[str]).
    captain_names = list(config.captain_name)
    config_team_name = config.team_name.strip()
    use_captain = len(captain_names) > 0

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
        log(f'Config: captains={json.dumps(captain_names)}{ignored} day="{config.day}" league="{config.league}"')
    else:
        log(f'Config: team="{config_team_name}" day="{config.day}" league="{config.league}"')
    log(f"Standings URL: {league_url}")
    log(f"Schedule URL:  {schedule_url}")
    log(f"Config path:   {CONFIG_PATH}")

    # Fail fast on missing API key *before* launching Chrome.
    create_kwargs = model_kwargs(config.model)

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

            matched_teams, resolution, captain_match_details = resolve_teams_from_standings(
                standings.rows or [],
                use_captain=use_captain,
                captain_names=captain_names,
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
            for detail in captain_match_details:
                hits = detail["matchedTeams"]
                q = json.dumps(detail["query"])
                if not hits:
                    log(f"  captain query {q}: no team matched")
                else:
                    log(
                        f"  captain query {q}: "
                        + ", ".join(
                            f'"{t.team_name}" (captain={t.captain_name or "?"})'
                            for t in hits
                        )
                    )

            # ---------------------------------------------------------------
            # 2) Schedule: select + verify each week tab, extract() games.
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
                # Exact-text click (act() fallback), then verify the selected
                # tab really is `label`; raises WeekTabError if it never is.
                await click_week_tab(stagehand, page, label)
                # Short settle so the week's match cards finish rendering
                # after aria-selected flips.
                await page.wait_for_timeout(750)

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
                "captainSearched": (
                    (captain_names[0] if len(captain_names) == 1 else captain_names)
                    if use_captain
                    else None
                ),
                "captainMatchDetails": (
                    [
                        {
                            "query": d["query"],
                            "matchedTeams": [
                                {
                                    "teamName": t.team_name,
                                    "captainName": t.captain_name,
                                    "record": t.record,
                                    "standing": t.standing,
                                }
                                for t in d["matchedTeams"]
                            ],
                        }
                        for d in captain_match_details
                    ]
                    if use_captain
                    else None
                ),
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
    except (ConfigError, MissingKeyError, ResolveError, WeekTabError) as err:
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
