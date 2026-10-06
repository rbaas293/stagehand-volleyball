# stagehand-volleyball

Python scraper for volleyball schedules on [league.ninja](https://league.ninja) (Flannagan's and similar clubs).

Default config searches captains **H. Robinson** / **R. Baas** / **Ryan Baas** across **Beer A** and **Beer B** for the current season.

## Modes

| `mode` | What it does |
|---|---|
| **`lean`** (default) | Club public LMS API for seasons, division nav, standings, and schedules. **No browser, no LLM.** |
| **`llm`** | [Stagehand v4](https://docs.stagehand.dev/v4/first-steps/quickstart) + xAI Grok BYO callback: browser `extract()` on standings and each schedule week. |

Set in `config.yaml` (`mode: lean|llm`) or override with `SCRAPE_MODE`.

Multi-division: `levels: ["Beer A", "Beer B"]` plus `siteUrl`. Auto-pick prefers an **active/in-range season that has divisions matching those levels** (so a newer empty overlap like Fall does not win over Summer III). Set `season` to force a name or uid. Single-division `leagueUrl` still works when `levels` is empty.

## Requirements

- **Python 3.11+** (required by the `stagehand` package; 3.12/3.13 fine)
- **Google Chrome** only for **llm** mode (local browser). Lean mode needs neither Chrome nor an LLM API key.
- `XAI_API_KEY` for **llm** mode (or `BROWSERBASE_API_KEY`)

## Layout

| Path | Role |
|---|---|
| `config.example.yaml` | Public template at **repo root** (copy → local `config.yaml`) |
| `config.yaml` | Local only (gitignored) |
| `python/main.py` | CLI entrypoint |
| `python/lean_api.py` | Pub-api client (lean mode) |
| `python/token_usage.py` | xAI token / cost accumulator |
| `python/games.json` | Output (gitignored) |
| `.env` (root or `python/`) | Secrets (gitignored); root `.env` is the fallback |

The scraper code stays under **`python/`** (not moved to repo root) so existing packaging/CI that targets `python/` (see PR #3) keeps working without path rewrites. Root owns config and docs only.

## Config

`config.yaml` is **local only** (gitignored). Start from the public template:

```bash
cp config.example.yaml config.yaml
# edit captainName / levels / siteUrl / mode / …
```

If `config.yaml` is missing, the scraper exits with an error that includes that `cp` hint — it does **not** fall back to the example file.


| Field | Required | Purpose |
|---|---|---|
| `captainName` | one of captain/team | String or YAML list. Exact after normalizing punctuation/case (`"R Baas"` ≡ `"R. Baas"`; no substrings). |
| `teamName` | one of captain/team | Exact match; used only when `captainName` is empty. |
| `mode` | no | `lean` (default) or `llm`. |
| `levels` | no | Name substrings for multi-div discovery (e.g. `Beer A`, `Beer B`). |
| `siteUrl` | with levels | Club origin, e.g. `https://example.league.ninja`. |
| `apiBaseUrl` | no | Override pub API base (inferred for known clubs). |
| `season` | no | Optional season **name or uid** override. When empty, auto-pick prefers in-range seasons that have divisions matching `levels` (falls back if the newest overlap has none). |
| `leagueUrl` | single-div | Division URL when `levels` is empty. |
| `day` / `league` | no | Notes for single-div; multi-div uses API fields. |
| `model` | no | xAI Grok id (default `grok-4-fast-reasoning`). |
| `schedulePathSuffix` | no | Default `"/schedule"` (llm mode). |

## Environment

| Variable | Required | Purpose |
|---|---|---|
| `XAI_API_KEY` | llm mode | xAI key (`https://api.x.ai/v1`). |
| `BROWSERBASE_API_KEY` | optional | Cloud browser (+ Model Gateway if no xAI key). |
| `STAGEHAND_MODEL` | optional | Overrides `config.model`. |
| `SCRAPE_MODE` | optional | Overrides `config.mode`. |
| `SCRAPE_LLM_PREFILTER` | optional | Multi-div llm: default `1` HTTP-prefilters standings; `0` disables. |
| `HEADLESS` | optional | `false` to show Chrome (llm). |

`.env` load order (first wins): **shell exports → `python/.env` → repo-root `.env`**.

Lean mode does not need `XAI_API_KEY`.

## Run

```bash
cd python
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py                          # lean by default
SCRAPE_MODE=llm XAI_API_KEY=… python main.py
```

Stdout is JSON; also writes `python/games.json`. Null keys are omitted; `scrapedAt` is UTC with millisecond precision (`YYYY-MM-DDTHH:MM:SS.mmmZ`). Each run logs LLM usage (calls, tokens, estimated USD from published xAI rates).

## Team resolution

Prefer `captainName` (exact after normalizing punctuation/case); else exact `teamName`. Multi-div: a miss in one division is OK — failure only if nothing matches across all scanned divisions. Resolve errors name the season scanned and hint at the `season` config override.

## License

Private / personal use unless otherwise noted.
