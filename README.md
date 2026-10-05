# stagehand-volleyball

Scrapes a volleyball team's schedule from a [league.ninja](https://league.ninja) division page with
[Stagehand v4](https://docs.stagehand.dev/v4/first-steps/quickstart) (`@browserbasehq/stagehand`).

Default target (editable in `config.json`): captain **H. Robinson** → **Win or Lose We Booze**, Sunday Beer A at Flannagan's, Summer III 2026.

What it does:

1. Loads `config.json` (optional captain name and/or team name, day, league label, division URL).
2. Launches Chrome (`localBrowser.launch()`), or a Browserbase cloud browser if `BROWSERBASE_API_KEY` is set.
3. Opens the division **Standings** page and uses `stagehand.extract()` (zod schema) to get the league name, division name, and every standings row (team + captain + W-L + rank).
4. **Resolves which team(s) to scrape** (see [Team resolution](#team-resolution) below).
5. Opens the **Schedule** page, clicks through every week tab (Week 1 … Week 6, tournaments), and extracts each game for the matched team(s):
   `date`, `time`, `week`, `team`, `opponent`, `location`, `status` (`scheduled` / `completed` / `cancelled`), and `result` when one is shown.
6. Prints the JSON to stdout and writes it to `games.json` in this folder. Rounds where none of the matched teams are listed yet (e.g. a tournament bracket that hasn't been posted) go in `roundsWithoutTeamGames`.
7. Closes Stagehand and the browser.

## Requirements

- **Node.js 22.18 or newer**, which Stagehand v4 requires (it uses the built-in `WebSocket`). Node 20 fails with `WebSocket is not defined`.
- Google Chrome installed (for local mode).
- pnpm (or npm).

## Config (`config.json`)

Edit **`config.json`** to change the captain, team, day, league label, or division URL. Copy from `config.example.json` if you need a fresh template.

| Field | Required | Purpose |
|---|---|---|
| `captainName` | one of captain/team | Captain to search for on the standings page (e.g. `"H. Robinson"`). Case-insensitive; partial match OK (`"Robinson"` matches `"H. Robinson"`). |
| `teamName` | one of captain/team | Explicit team name. Used only when `captainName` is empty/absent. Ignored for discovery while `captainName` is set. |
| `day` | yes | Game day label (e.g. `"Sunday"`) — stored in output for convenience |
| `league` | yes | Human-readable league / division path for your own notes |
| `leagueUrl` | yes | Division standings URL (the script appends the schedule suffix) |
| `schedulePathSuffix` | no | Default `"/schedule"` |

You must set **at least one** of `captainName` or `teamName`.

Example (captain-driven — recommended when you know the captain from standings):

```json
{
  "captainName": "H. Robinson",
  "teamName": "Win or Lose We Booze",
  "day": "Sunday",
  "league": "Summer III- 2026 › Sunday Coed Sixes- Beer A- Evening › Sunday Beer (A)- Court E",
  "leagueUrl": "https://flannagans.league.ninja/leagues/division/8f285cc6-16d2-43ff-88ad-66a2d8a41b9a",
  "schedulePathSuffix": "/schedule"
}
```

Example (team-only — leave `captainName` empty or omit it):

```json
{
  "teamName": "Win or Lose We Booze",
  "day": "Sunday",
  "league": "Summer III- 2026 › Sunday Coed Sixes- Beer A- Evening › Sunday Beer (A)- Court E",
  "leagueUrl": "https://flannagans.league.ninja/leagues/division/8f285cc6-16d2-43ff-88ad-66a2d8a41b9a"
}
```

`leagueUrl` should be the division page (the Standings tab). The script builds the schedule URL from `leagueUrl` + `schedulePathSuffix`.

If `config.json` is missing or invalid, the scraper exits with a clear error before launching Chrome.

### Team resolution

| Config | Behavior |
|---|---|
| `captainName` set (non-empty) | Prefer captain discovery. Extract all standings rows, keep every team whose captain matches `captainName` (case-insensitive, partial). Scrape **all games** for every matched team. `teamName` in config is ignored for discovery while captain is set. |
| `captainName` empty / omitted | Use `teamName` as an explicit override. Match that team on the standings page (exact, then case-insensitive substring). |

If a captain captains more than one team in the division, every matching team is included and their games are merged into one `games` array (each game has a `team` field).

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
  "captainSearched": "H. Robinson",
  "resolution": "captain",
  "matchedTeams": [
    {
      "teamName": "Win or Lose We Booze",
      "captainName": "H. Robinson",
      "record": "2-3",
      "standing": "4"
    }
  ],
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
      "team": "Win or Lose We Booze",
      "opponent": "Tipsy Tippers", "location": "Outdoors - Court E",
      "status": "completed", "result": "Winner - Win or Lose We Booze"
    },
    {
      "date": "Sun, Oct 11", "time": "5:00 pm", "week": "LEAGUE ROUND Week 6 - Oct 11",
      "team": "Win or Lose We Booze",
      "opponent": "Spike of the Beast", "location": "The Fieldhouse - Court B",
      "status": "scheduled"
    }
  ],
  "roundsWithoutTeamGames": ["TOURNAMENT Oct 18"]
}
```

When resolving by captain, `captainSearched` is the config string you searched for and `matchedTeams` lists every standings row that matched. When using `teamName` only, `captainSearched` is `null` and `resolution` is `"teamName"`.
