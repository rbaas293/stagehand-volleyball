# stagehand-volleyball

Scrapes a volleyball team's schedule from a [league.ninja](https://league.ninja) division page with
[Stagehand v4](https://docs.stagehand.dev/v4/first-steps/quickstart) (`@browserbasehq/stagehand`).

Default target (editable in `config.json`): **Win or Lose We Booze**, Sunday Beer A at Flannagan's, Summer III 2026.

What it does:

1. Loads `config.json` (team name, day, league label, division URL).
2. Launches Chrome (`localBrowser.launch()`), or a Browserbase cloud browser if `BROWSERBASE_API_KEY` is set.
3. Opens the division **Standings** page and uses `stagehand.extract()` (zod schema) to get the league name, division name, and the team's W-L record and rank.
4. Opens the **Schedule** page, clicks through every week tab (Week 1 … Week 6, tournaments), and extracts each game for the team:
   `date`, `time`, `week`, `opponent`, `location`, `status` (`scheduled` / `completed` / `cancelled`), and `result` when one is shown.
5. Prints the JSON to stdout and writes it to `games.json` in this folder. Rounds where the team isn't listed yet (e.g. a tournament bracket that hasn't been posted) go in `roundsWithoutTeamGames`.
6. Closes Stagehand and the browser.

## Requirements

- **Node.js 22.18 or newer**, which Stagehand v4 requires (it uses the built-in `WebSocket`). Node 20 fails with `WebSocket is not defined`.
- Google Chrome installed (for local mode).
- pnpm (or npm).

## Config (`config.json`)

Edit **`config.json`** to change the team, day, league label, or division URL. Copy from `config.example.json` if you need a fresh template.

| Field | Required | Purpose |
|---|---|---|
| `teamName` | yes | Exact team name as shown on league.ninja |
| `day` | yes | Game day label (e.g. `"Sunday"`) — stored in output for convenience |
| `league` | yes | Human-readable league / division path for your own notes |
| `leagueUrl` | yes | Division standings URL (the script appends the schedule suffix) |
| `schedulePathSuffix` | no | Default `"/schedule"` |

Example:

```json
{
  "teamName": "Win or Lose We Booze",
  "day": "Sunday",
  "league": "Summer III- 2026 › Sunday Coed Sixes- Beer A- Evening › Sunday Beer (A)- Court E",
  "leagueUrl": "https://flannagans.league.ninja/leagues/division/8f285cc6-16d2-43ff-88ad-66a2d8a41b9a",
  "schedulePathSuffix": "/schedule"
}
```

`leagueUrl` should be the division page (the Standings tab). The script builds the schedule URL from `leagueUrl` + `schedulePathSuffix`.

If `config.json` is missing or invalid, the scraper exits with a clear error before launching Chrome.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | yes (local mode) | LLM used by `extract()`. Stagehand never reads env vars itself; `index.ts` reads this one and passes it to `Stagehand.create()`. |
| `BROWSERBASE_API_KEY` | optional | Uses `browserbase.launch()` (a cloud browser) instead of local Chrome. If `OPENAI_API_KEY` isn't set, Browserbase's Model Gateway picks the model. |
| `STAGEHAND_MODEL` | optional | Overrides the model. Default: `openai/gpt-5.6-luna`. |
| `HEADLESS` | optional | Set to `false` to watch the Chrome window. |

Set these in your shell, or copy `.env.example` to `.env` (it's git-ignored) and use `pnpm scrape:env`. Don't commit keys.

## Run

```bash
pnpm install
export OPENAI_API_KEY=...   # or: cp .env.example .env and fill it in
pnpm scrape                 # or: pnpm scrape:env  (loads .env)
```

With npm: `npm install` then `npm run scrape`.

Type-check only: `pnpm typecheck`.

## Example output (shape)

```json
{
  "team": "Win or Lose We Booze",
  "day": "Sunday",
  "league": "Summer III- 2026 › Sunday Coed Sixes- Beer A- Evening › Sunday Beer (A)- Court E",
  "url": "https://flannagans.league.ninja/leagues/division/...",
  "scrapedAt": "2026-10-05T17:45:00.000Z",
  "leagueName": "Summer III- 2026",
  "divisionName": "Sunday Coed Sixes- Beer A- EVENING (5:00-7:00PM)",
  "teamRecord": "2-3",
  "teamStanding": "4",
  "games": [
    {
      "date": "Sun, Oct 04", "time": "6:00 pm", "week": "LEAGUE ROUND Week 5 - Oct 4",
      "opponent": "Tipsy Tippers", "location": "Outdoors - Court E",
      "status": "completed", "result": "Winner - Win or Lose We Booze"
    },
    {
      "date": "Sun, Oct 11", "time": "5:00 pm", "week": "LEAGUE ROUND Week 6 - Oct 11",
      "opponent": "Spike of the Beast", "location": "The Fieldhouse - Court B",
      "status": "scheduled"
    }
  ],
  "roundsWithoutTeamGames": ["TOURNAMENT Oct 18"]
}
```
