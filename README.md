# stagehand-volleyball

Scrapes volleyball team schedules from [league.ninja](https://league.ninja) (Flannagan's and similar clubs).

Default config searches captains **H. Robinson** / **R. Baas** / **Ryan Baas** across **Beer A** and **Beer B** divisions for the current season.

## Modes

| `mode` | What it does |
|---|---|
| **`lean`** (default) | Calls the club's public LMS API (`…-lms-pub-api.league.ninja`) for seasons, division nav, standings, and schedules. **No browser, no LLM.** |
| **`llm`** | Stagehand v4 + xAI Grok BYO callback: browser extract on standings and each schedule week. Use when the API shape changes or you want an LLM fallback path. |

Set in `config.yaml` (`mode: lean|llm`) or override with `SCRAPE_MODE=lean|llm`.

Multi-division discovery: set `levels: ["Beer A", "Beer B"]` (plus `siteUrl`). The scraper picks an **active/in-range season that has divisions matching those levels** (so a newer empty overlap like Fall does not win over Summer III). Set `season` to force a name or uid. Single-division `leagueUrl` mode still works when `levels` is empty.

## Entrypoints

- **Python (preferred):** `python/main.py` — lean + llm, token usage summary, multi-div.
- **TypeScript:** `index.ts` — older Stagehand path (being removed; see the `python-only` PR).

## Config (`config.yaml`)

Copy `config.example.yaml` → `config.yaml`. You must set at least one of `captainName` or `teamName`.

| Field | Required | Purpose |
|---|---|---|
| `captainName` | one of captain/team | String or YAML list. Case- and punctuation-insensitive (`"R Baas"` ≡ `"R. Baas"`). |
| `teamName` | one of captain/team | Used only when `captainName` is empty. |
| `mode` | no | `lean` (default) or `llm`. |
| `levels` | no | Substrings to match in league/division names (e.g. `Beer A`, `Beer B`). Enables multi-div. |
| `siteUrl` | with levels | Club origin, e.g. `https://flannagans.league.ninja`. |
| `apiBaseUrl` | no | Override pub API base (inferred for known clubs from `siteUrl`/`leagueUrl`). |
| `season` | no | Optional season **name or uid** override. When empty, auto-pick prefers in-range seasons that have divisions matching `levels` (falls back if the newest overlap has none). |
| `leagueUrl` | single-div | Division standings URL when `levels` is empty. |
| `day` / `league` | no | Notes for single-div; multi-div uses API day/leagueName per division. |
| `model` | no | xAI Grok id (default `grok-4-fast-reasoning`). Overridden by `STAGEHAND_MODEL`. |
| `schedulePathSuffix` | no | Default `"/schedule"` (llm mode). |

## Environment

| Variable | Required | Purpose |
|---|---|---|
| `XAI_API_KEY` | llm / local Stagehand | xAI key for Grok (OpenAI-compatible client at `https://api.x.ai/v1`). |
| `BROWSERBASE_API_KEY` | optional | Cloud browser (+ Model Gateway if no xAI key). |
| `STAGEHAND_MODEL` | optional | Overrides `config.model`. |
| `SCRAPE_MODE` | optional | Overrides `config.mode` (`lean`\|`llm`). |
| `SCRAPE_LLM_PREFILTER` | optional | Multi-div llm: `1` (default) HTTP-prefilters standings so Stagehand only runs on divisions with captain/team hits; `0` disables. |
| `HEADLESS` | optional | `false` to show Chrome (llm mode). |

Lean mode does not need `XAI_API_KEY`. Put secrets in `.env` (gitignored) at the repo root or `python/.env`.

## Run (Python)

```bash
cd python
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# lean (default): no API key needed
python main.py
# llm:
SCRAPE_MODE=llm export XAI_API_KEY=...
python main.py
```

Output: JSON on stdout and `python/games.json`. Each run prints an LLM usage summary (`calls`, prompt/completion/total tokens, estimated USD from published xAI rates). Output omits JSON `null` keys; `scrapedAt` is UTC with millisecond precision (`…Z`).

## Team resolution

Same rules in lean and llm: prefer `captainName` (exact after normalizing punctuation/case; `"R Baas"` ≡ `"R. Baas"`, no substrings); else exact `teamName`. In multi-div mode, a miss in one division is fine — only a miss across **all** scanned divisions fails. Resolve errors name the season scanned and hint at the `season` config override.

## License

Private / personal use unless otherwise noted.
