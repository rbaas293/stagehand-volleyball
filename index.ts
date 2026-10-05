/**
 * Stagehand v4 scraper: volleyball game times for one team on league.ninja.
 *
 * Run:  pnpm install && pnpm scrape
 * Env:  OPENAI_API_KEY      required for the default local-Chrome mode
 *       BROWSERBASE_API_KEY optional: run in a Browserbase cloud browser instead
 *       STAGEHAND_MODEL     optional, default "openai/gpt-5.6-luna"
 *       HEADLESS=false      optional: show the local Chrome window
 *
 * Target team / day / league / URL come from config.json (see config.example.json).
 *
 * league.ninja layout (as of Oct 2026):
 *   <division URL>           -> Standings tab (league/division names + W-L record)
 *   <division URL>/schedule  -> Schedule tab with one sub-tab per week
 *                               ("Week 5 - Oct 4", "TOURNAMENT Oct 18", ...),
 *                               showing only that week's games.
 * So we read the standings, then click through each week tab and extract.
 */
import { readFileSync } from "node:fs";
import { writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  browserbase,
  localBrowser,
  Stagehand,
  type ModelName,
  type StagehandBrowser,
} from "@browserbasehq/stagehand";
import { z } from "zod/v4";

const ROOT = dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = join(ROOT, "config.json");

// ---- Config (team / day / league / URL) ------------------------------------
const ConfigSchema = z.object({
  teamName: z.string().min(1),
  day: z.string().min(1),
  league: z.string().min(1),
  leagueUrl: z.string().url(),
  schedulePathSuffix: z.string().optional().default("/schedule"),
});

type AppConfig = z.infer<typeof ConfigSchema>;

function loadConfig(): AppConfig {
  let raw: string;
  try {
    raw = readFileSync(CONFIG_PATH, "utf8");
  } catch (err) {
    const why = err instanceof Error ? err.message : String(err);
    throw new ConfigError(
      `Missing or unreadable config.json at ${CONFIG_PATH} (${why}). ` +
        `Copy config.example.json to config.json and edit teamName, day, league, and leagueUrl.`,
    );
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    const why = err instanceof Error ? err.message : String(err);
    throw new ConfigError(`config.json is not valid JSON: ${why}`);
  }
  const result = ConfigSchema.safeParse(parsed);
  if (!result.success) {
    const details = result.error.issues
      .map((i) => `${i.path.join(".") || "(root)"}: ${i.message}`)
      .join("; ");
    throw new ConfigError(
      `config.json is invalid: ${details}. Expected teamName, day, league, leagueUrl (and optional schedulePathSuffix).`,
    );
  }
  return result.data;
}

class ConfigError extends Error {}
class MissingKeyError extends Error {}

const config = loadConfig();
const TEAM_NAME = config.teamName;
const LEAGUE_URL = config.leagueUrl.replace(/\/+$/, "");
const SCHEDULE_URL = `${LEAGUE_URL}${config.schedulePathSuffix.startsWith("/") ? config.schedulePathSuffix : `/${config.schedulePathSuffix}`}`;
const MODEL_NAME = (process.env.STAGEHAND_MODEL ?? "openai/gpt-5.6-luna") as ModelName;
const HEADLESS = process.env.HEADLESS !== "false";
const OUTPUT_PATH = join(ROOT, "games.json");

// ---- Schemas ---------------------------------------------------------------
const GameSchema = z.object({
  date: z.string().describe("Game date as shown, e.g. 'Sun, Oct 11'"),
  time: z.string().describe("Start time as shown, e.g. '5:00 pm'"),
  week: z.string().optional().describe("Week / round label, e.g. 'Week 6 - Oct 11' or 'Tournament'"),
  opponent: z.string().describe(`The other team in the match (not ${TEAM_NAME})`),
  location: z.string().describe("Venue and court, e.g. 'The Fieldhouse - Court B'"),
  status: z
    .enum(["scheduled", "completed", "cancelled"])
    .describe("completed if a winner/score is shown, cancelled if marked cancelled/postponed, else scheduled"),
  result: z
    .string()
    .optional()
    .describe(`Result if shown, e.g. 'Winner - ${TEAM_NAME}' or a score; omit if not shown`),
});

const GamesSchema = z.object({
  games: z.array(GameSchema).describe(`Only matches where ${TEAM_NAME} is one of the two teams`),
});

const StandingsSchema = z.object({
  leagueName: z.string().optional().describe("League / season name, e.g. 'Summer III- 2026'"),
  divisionName: z
    .string()
    .optional()
    .describe("Division name, e.g. 'Sunday Coed Sixes- Beer A- EVENING (5:00-7:00PM)'"),
  teamRecord: z.string().optional().describe(`${TEAM_NAME}'s W-L record, e.g. '2-3'`),
  teamStanding: z.string().optional().describe(`${TEAM_NAME}'s rank in the standings table, e.g. '4'`),
});

type Game = z.infer<typeof GameSchema>;

// ---- Setup helpers ---------------------------------------------------------
function modelConfig() {
  const apiKey = process.env.OPENAI_API_KEY;
  if (apiKey) return { modelName: MODEL_NAME, apiKey };
  if (process.env.BROWSERBASE_API_KEY) return undefined; // Browserbase Model Gateway picks a model
  throw new MissingKeyError(
    "OPENAI_API_KEY is not set. Export it, or put it in .env and run `pnpm scrape:env`. " +
      "Alternatively set BROWSERBASE_API_KEY to use a Browserbase cloud browser + Model Gateway.",
  );
}

async function launchBrowser(): Promise<StagehandBrowser> {
  const bbKey = process.env.BROWSERBASE_API_KEY;
  if (bbKey) {
    log("Using Browserbase cloud browser");
    return browserbase.launch({ apiKey: bbKey });
  }
  log(`Using local Chrome (headless=${HEADLESS})`);
  return localBrowser.launch({ headless: HEADLESS });
}

function log(msg: string) {
  console.error(`[stagehand-volleyball] ${msg}`);
}

// ---- Main ------------------------------------------------------------------
async function main() {
  log(`Config: team="${TEAM_NAME}" day="${config.day}" league="${config.league}"`);
  log(`Standings URL: ${LEAGUE_URL}`);
  log(`Schedule URL:  ${SCHEDULE_URL}`);

  const model = modelConfig(); // fail fast before launching Chrome
  const browser = await launchBrowser();
  try {
    const stagehand = await Stagehand.create({ browser, ...(model ? { model } : {}) });
    try {
      const [page] = await browser.context.pages();

      // 1) Standings page: league, division, record.
      log(`Opening standings: ${LEAGUE_URL}`);
      await page.goto(LEAGUE_URL, { waitUntil: "networkidle", timeout: 60_000 });
      await page.waitForTimeout(1_500);
      const { data: standings } = await stagehand.extract(
        `From this league standings page, get the league/season name, the division name, ` +
          `and the W-L record and rank of the team "${TEAM_NAME}".`,
        StandingsSchema,
      );

      // 2) Schedule page: click through each week tab and extract that week's games.
      log(`Opening schedule: ${SCHEDULE_URL}`);
      await page.goto(SCHEDULE_URL, { waitUntil: "networkidle", timeout: 60_000 });
      await page.waitForTimeout(1_500);

      const weekLabels = await page.evaluate(() =>
        Array.from(document.querySelectorAll<HTMLElement>("[role=tablist] [role=tab]"))
          .map((el) => el.innerText.replace(/\s*\n\s*/g, " ").trim())
          .filter((t) => /LEAGUE ROUND|TOURNAMENT|PLAYOFF|Week/i.test(t)),
      );
      log(`Found ${weekLabels.length} week tabs: ${weekLabels.join(" | ")}`);

      const games: Game[] = [];
      const roundsWithoutTeamGames: string[] = [];

      for (const label of weekLabels) {
        await page.evaluate((wanted: string) => {
          const tab = Array.from(document.querySelectorAll<HTMLElement>("[role=tablist] [role=tab]")).find(
            (el) => el.innerText.replace(/\s*\n\s*/g, " ").trim() === wanted,
          );
          tab?.click();
        }, label);
        await page.waitForTimeout(1_500);

        // Skip the LLM call when the team isn't listed this week (e.g. bracket not posted yet).
        const hasTeam = await page.evaluate(
          (team: string) => document.body.innerText.toLowerCase().includes(team.toLowerCase()),
          TEAM_NAME,
        );
        if (!hasTeam) {
          log(`  ${label}: no ${TEAM_NAME} games listed`);
          roundsWithoutTeamGames.push(label);
          continue;
        }

        const { data } = await stagehand.extract(
          `This page shows the games for the selected week tab "${label}". ` +
            `List every match on the selected week where "${TEAM_NAME}" is one of the two teams. ` +
            `Each match card shows date+time, location/court, the two team names (each followed by a captain name), ` +
            `and sometimes "Winner - <team>". Ignore captain names. Set week to "${label}".`,
          GamesSchema,
        );
        log(`  ${label}: ${data.games.length} game(s)`);
        games.push(...data.games.map((g) => ({ ...g, week: g.week ?? label })));
      }

      const output = {
        team: TEAM_NAME,
        day: config.day,
        league: config.league,
        url: LEAGUE_URL,
        scrapedAt: new Date().toISOString(),
        ...standings,
        games,
        roundsWithoutTeamGames,
      };
      const json = JSON.stringify(output, null, 2);
      console.log(json);
      await writeFile(OUTPUT_PATH, json + "\n", "utf8");
      log(`Wrote ${games.length} games to ${OUTPUT_PATH}`);
    } finally {
      await stagehand.close();
    }
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  if (err instanceof ConfigError || err instanceof MissingKeyError) {
    console.error(err.message);
  } else {
    console.error(err);
  }
  process.exit(1);
});
