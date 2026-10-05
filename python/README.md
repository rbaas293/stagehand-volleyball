# Python Stagehand volleyball scraper

Python port of the root TypeScript scraper (`../index.ts`), using the
[Stagehand v4 Python SDK](https://docs.stagehand.dev/first-steps/quickstart)
(`pip install stagehand`).

Same flow as TypeScript:

1. Load `config.yaml` with PyYAML `safe_load` (prefers **`../config.yaml`** so one file drives both languages; falls back to `python/config.yaml`).
2. Launch Chrome via `local_browser.launch()`, or Browserbase if `BROWSERBASE_API_KEY` is set.
3. Open the division **Standings** page → `stagehand.extract()` with pydantic models → resolve team(s) by `captainName` (preferred) or `teamName`.
4. Open **Schedule** → `stagehand.act()` to click each week tab → `extract()` games for matched teams.
5. Print JSON to stdout and write `python/games.json`.

## Requirements

- Python 3.11+ (required by the `stagehand` package; 3.12/3.13 fine)
- Google Chrome installed (local mode)
- `OPENAI_API_KEY` (local mode) **or** `BROWSERBASE_API_KEY`

## Setup

```bash
cd python
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # or reuse the repo-root .env
# edit ../config.yaml (shared) — captainName / teamName / day / league / leagueUrl
export OPENAI_API_KEY=...          # if not using .env
python main.py
```

`main.py` reads both `python/.env` and the repo-root `../.env`. Precedence, highest first: **shell exports > `python/.env` > `../.env`**. A value in `python/.env` overrides the same key in the root `.env`, and a variable you `export` in the shell overrides both. Do not commit `.env` or `.venv`.

## Config

Uses the same fields as the TypeScript scraper — see the root [README](../README.md#config-configyaml). The file is YAML (comments allowed); start from `../config.example.yaml`.

| Field | Required | Purpose |
|---|---|---|
| `captainName` | one of captain/team | Discover team(s) from standings by captain (partial, case-insensitive) |
| `teamName` | one of captain/team | Explicit team when `captainName` is empty |
| `day` | yes | Stored in output |
| `league` | yes | Human label |
| `leagueUrl` | yes | Division standings URL |
| `schedulePathSuffix` | no | Default `"/schedule"` |

## Environment

| Variable | Required | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | yes (local) | Passed to `Stagehand.create(model_api_key=...)` |
| `BROWSERBASE_API_KEY` | optional | `browserbase.launch()` instead of local Chrome |
| `STAGEHAND_MODEL` | optional | Default `openai/gpt-5.6-luna` |
| `HEADLESS` | optional | Set `false` to show Chrome |

## Output

Same shape as the TypeScript `games.json` (see root README). Written to **`python/games.json`** so it does not overwrite the TS output at the repo root.
