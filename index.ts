/**
 * Stagehand v4 scraper: volleyball game times for a team (or captain) on league.ninja.
 *
 * Run:  pnpm install && pnpm scrape
 * Env:  OPENAI_API_KEY      required for the default local-Chrome mode
 *       BROWSERBASE_API_KEY optional: run in a Browserbase cloud browser instead
 *       STAGEHAND_MODEL     optional, default "openai/gpt-5.6-luna"
 *       HEADLESS=false      optional: show the local Chrome window
 *
 * Target captain / team / day / league / URL come from config.json (see config.example.json).
 *
 * Resolution:
 *   - If captainName is set (non-empty), discover team(s) on the standings page whose
 *     captain matches (case-insensitive, partial OK: "Robinson" matches "H. Robinson"),
 *     then scrape all games for those team name(s).
 *   - If captainName is empty/absent, use teamName as an explicit team override.
 *
 * league.ninja layout (as of Oct 2026):
 *   <division URL>           -> Standings tab (league/division names + W-L record + captains)
 *   <division URL>/schedule  -> Schedule tab with one sub-tab per week
 *                               ("Week 5 - Oct 4", "TOURNAMENT Oct 18", ...),
 *                               showing only that week's games.
 * So we resolve the team via standings (optionally by captain), then click through each week tab.
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

// ---- Config (captain / team / day / league / URL) --------------------------
const ConfigSchema = z
  .object({
    captainName: z.string().optional().default(""),
    teamName: z.string().optional().default(""),
    day: z.string().min(1),
    league: z.string().min(1),
    leagueUrl: z.string().url(),
    schedulePathSuffix: z.string().optional().default("/schedule"),
  })
  .superRefine((val, ctx) => {
    const hasCaptain = Boolean(val.captainName?.trim());
    const hasTeam = Boolean(val.teamName?.trim());
    if (!hasCaptain && !hasTeam) {
      ctx.addIssue({
        code: "custom",
        message:
          "Set captainName (to discover team(s) by captain) and/or teamName (explicit team when captainName is empty).",
        path: ["captainName"],
      });
    }
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
        `Copy config.example.json to config.json and edit captainName and/or teamName, day, league, and leagueUrl.`,
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
      `config.json is invalid: ${details}. Expected day, league, leagueUrl, plus captainName and/or teamName (and optional schedulePathSuffix).`,
    );
  }
  return result.data;
}

class ConfigError extends Error {}
class MissingKeyError extends Error {}
class ResolveError extends Error {}

const config = loadConfig();
const CAPTAIN_NAME = config.captainName?.trim() ?? "";
const CONFIG_TEAM_NAME = config.teamName?.trim() ?? "";
/** Prefer captainName for discovery; fall back to teamName when captain is empty/absent. */
const USE_CAPTAIN = CAPTAIN_NAME.length > 0;
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
  team: z.string().optional().describe("Which of our matched teams is in this match"),
  opponent: z.string().describe("The other team in the match (not one of our matched teams)"),
  location: z.string().describe("Venue and court, e.g. 'The Fieldhouse - Court B'"),
  status: z
    .enum(["scheduled", "completed", "cancelled"])
    .describe("completed if a winner/score is shown, cancelled if marked cancelled/postponed, else scheduled"),
  result: z
    .string()
    .optional()
    .describe("Result if shown, e.g. 'Winner - <team>' or a score; omit if not shown"),
});

const GamesSchema = z.object({
  games: z.array(GameSchema).describe("Only matches where one of our matched teams is playing"),
});

const StandingsRowSchema = z.object({
  teamName: z.string().describe("Full team name as shown in the standings table"),
  captainName: z
    .string()
    .optional()
    .describe("Captain name shown for that team, e.g. 'H. Robinson'"),
  record: z.string().optional().describe("W-L record, e.g. '2-3'"),
  standing: z.string().optional().describe("Rank in the standings table, e.g. '4'"),
});

const StandingsSchema = z.object({
  leagueName: z.string().optional().describe("League / season name, e.g. 'Summer III- 2026'"),
  divisionName: z
    .string()
    .optional()
    .describe("Division name, e.g. 'Sunday Coed Sixes- Beer A- EVENING (5:00-7:00PM)'"),
  rows: z
    .array(StandingsRowSchema)
    .describe("Every team row in the standings table, including team name and captain"),
});

type Game = z.infer<typeof GameSchema>;
type StandingsRow = z.infer<typeof StandingsRowSchema>;

function captainMatches(rowCaptain: string | undefined, wanted: string): boolean {
  if (!rowCaptain || !wanted) return false;
  const a = rowCaptain.toLowerCase().replace(/\s+/g, " ").trim();
  const b = wanted.toLowerCase().replace(/\s+/g, " ").trim();
  return a.includes(b) || b.includes(a);
}

function resolveTeamsFromStandings(
  rows: StandingsRow[],
): { matchedTeams: StandingsRow[]; resolution: "captain" | "teamName" } {
  if (USE_CAPTAIN) {
    const matched = rows.filter((r) => captainMatches(r.captainName, CAPTAIN_NAME));
    if (matched.length === 0) {
      const captains = rows
        .map((r) => r.captainName)
        .filter(Boolean)
        .join(", ");
      throw new ResolveError(
        `No standings row matched captainName="${CAPTAIN_NAME}". Captains seen: ${captains || "(none extracted)"}.`,
      );
    }
    return { matchedTeams: matched, resolution: "captain" };
  }

  const wanted = CONFIG_TEAM_NAME.toLowerCase();
  const matched = rows.filter((r) => r.teamName.toLowerCase() === wanted);
  if (matched.length === 0) {
    // Fall back to case-insensitive substring if exact match fails.
    const partial = rows.filter((r) => r.teamName.toLowerCase().includes(wanted));
    if (partial.length === 0) {
      const names = rows.map((r) => r.teamName).join(", ");
      throw new ResolveError(
        `No standings row matched teamName="${CONFIG_TEAM_NAME}". Teams seen: ${names || "(none extracted)"}.`,
      );
    }
    return { matchedTeams: partial, resolution: "teamName" };
  }
  return { matchedTeams: matched, resolution: "teamName" };
}

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

function gamesSchemaFor(teamNames: string[]) {
  const list = teamNames.map((t) => `"${t}"`).join(" or ");
  return z.object({
    games: z
      .array(GameSchema)
      .describe(`Only matches where ${list} is one of the two teams`),
  });
}

// ---- Main ------------------------------------------------------------------
async function main() {
  if (USE_CAPTAIN) {
    log(
      `Config: captain="${CAPTAIN_NAME}"` +
        (CONFIG_TEAM_NAME ? ` (config teamName="${CONFIG_TEAM_NAME}" ignored while captainName is set)` : "") +
        ` day="${config.day}" league="${config.league}"`,
    );
  } else {
    log(`Config: team="${CONFIG_TEAM_NAME}" day="${config.day}" league="${config.league}"`);
  }
  log(`Standings URL: ${LEAGUE_URL}`);
  log(`Schedule URL:  ${SCHEDULE_URL}`);

  const model = modelConfig(); // fail fast before launching Chrome
  const browser = await launchBrowser();
  try {
    const stagehand = await Stagehand.create({ browser, ...(model ? { model } : {}) });
    try {
      const [page] = await browser.context.pages();

      // 1) Standings page: league, division, all rows (team + captain), then resolve target teams.
      log(`Opening standings: ${LEAGUE_URL}`);
      await page.goto(LEAGUE_URL, { waitUntil: "networkidle", timeout: 60_000 });
      await page.waitForTimeout(1_500);
      const { data: standings } = await stagehand.extract(
        `From this league standings page, get the league/season name, the division name, ` +
          `and every row in the standings table. For each row extract the full team name, ` +
          `the captain name (often shown next to or under the team), the W-L record, and the rank. ` +
          `Include every team, not just one.`,
        StandingsSchema,
      );

      const { matchedTeams, resolution } = resolveTeamsFromStandings(standings.rows ?? []);
      const teamNames = matchedTeams.map((t) => t.teamName);
      log(
        `Resolved via ${resolution}: ${matchedTeams
          .map((t) => `"${t.teamName}" (captain=${t.captainName ?? "?"})`)
          .join(", ")}`,
      );

      // 2) Schedule page: click through each week tab and extract that week's games for matched teams.
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
      const extractSchema = gamesSchemaFor(teamNames);
      const teamListForPrompt = teamNames.map((t) => `"${t}"`).join(" or ");

      for (const label of weekLabels) {
        await page.evaluate((wanted: string) => {
          const tab = Array.from(document.querySelectorAll<HTMLElement>("[role=tablist] [role=tab]")).find(
            (el) => el.innerText.replace(/\s*\n\s*/g, " ").trim() === wanted,
          );
          tab?.click();
        }, label);
        await page.waitForTimeout(1_500);

        // Skip the LLM call when none of our teams are listed this week (e.g. bracket not posted yet).
        const hasTeam = await page.evaluate((names: string[]) => {
          const body = document.body.innerText.toLowerCase();
          return names.some((n) => body.includes(n.toLowerCase()));
        }, teamNames);
        if (!hasTeam) {
          log(`  ${label}: no matched-team games listed`);
          roundsWithoutTeamGames.push(label);
          continue;
        }

        const { data } = await stagehand.extract(
          `This page shows the games for the selected week tab "${label}". ` +
            `List every match on the selected week where ${teamListForPrompt} is one of the two teams. ` +
            `Each match card shows date+time, location/court, the two team names (each followed by a captain name), ` +
            `and sometimes "Winner - <team>". For each match set "team" to whichever of [${teamNames.join(", ")}] is playing, ` +
            `and "opponent" to the other side. Ignore captain names on the schedule cards. Set week to "${label}".`,
          extractSchema,
        );
        log(`  ${label}: ${data.games.length} game(s)`);
        games.push(
          ...data.games.map((g) => ({
            ...g,
            week: g.week ?? label,
            team: g.team ?? (teamNames.length === 1 ? teamNames[0] : g.team),
          })),
        );
      }

      const output = {
        captainSearched: USE_CAPTAIN ? CAPTAIN_NAME : null,
        resolution,
        matchedTeams: matchedTeams.map((t) => ({
          teamName: t.teamName,
          captainName: t.captainName ?? null,
          record: t.record ?? null,
          standing: t.standing ?? null,
        })),
        // Back-compat single-team fields when exactly one team matched
        team: teamNames.length === 1 ? teamNames[0] : teamNames,
        day: config.day,
        league: config.league,
        url: LEAGUE_URL,
        scrapedAt: new Date().toISOString(),
        leagueName: standings.leagueName,
        divisionName: standings.divisionName,
        teamRecord: matchedTeams.length === 1 ? matchedTeams[0].record : undefined,
        teamStanding: matchedTeams.length === 1 ? matchedTeams[0].standing : undefined,
        games,
        roundsWithoutTeamGames,
      };
      const json = JSON.stringify(output, null, 2);
      console.log(json);
      await writeFile(OUTPUT_PATH, json + "\n", "utf8");
      log(`Wrote ${games.length} games for ${teamNames.length} team(s) to ${OUTPUT_PATH}`);
    } finally {
      await stagehand.close();
    }
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  if (err instanceof ConfigError || err instanceof MissingKeyError || err instanceof ResolveError) {
    console.error(err.message);
  } else {
    console.error(err);
  }
  process.exit(1);
});
