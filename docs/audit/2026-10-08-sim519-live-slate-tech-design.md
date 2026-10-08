# Tech design — the live, schedule-driven game day view (SIM-519)

> **STATUS 2026-10-08 — PROPOSED, not built.** This document is the technical design for the
> live-slate epic (SIM-519, P2 in `BACKLOG.xlsx`). It has eight parts, each closable on its own.
> Seven decisions wait for the owner (§16). Nothing in this document changes production.
> The readable page is https://claude.ai/artifact/6McDv3bmQBxoRZZ3QS5NCQ.
>
> **What the owner ruled on 2026-08-29.** The day slate is schedule-driven: Major League
> Baseball's own public schedule says which games exist, when they start and what state they
> are in. Our database supplies our data on top: simulations, projections, odds. A card has
> three states: preview (not started), live (in progress) and final (the result).
>
> **What this design adds to the ruling.** A game that has not started must be simulable, or
> the slate shows upcoming games the platform cannot price. Today it cannot: the simulator
> refuses every game with no lineup rows, and nothing writes lineup rows before a game is
> final (§2). The design also makes the simulation a visible, durable run (one per game, with
> progress) instead of several hidden four-minute batches behind panel buttons.

**Date:** 2026-10-08
**Ticket:** SIM-519 (P2) in `BACKLOG.xlsx`. Next free ID: SIM-560 (the subtitle row). This design
files no ticket; §17 names the follow-ons to file when the build starts.
**Evidence:** the API (`api/routes/games.py`, `api/main.py`), the live pipeline
(`pipeline/live/live_ingestion_pipeline.py`), the loader (`pipeline/etl/etl_historical_loader.py`),
the resolver (`simulation/lineup_resolver.py`), the runner (`simulation/batch_runner.py`), the
frontend (`frontend/src/pages/DaySummaryPage.tsx`, `GamePage.tsx`,
`components/games/GameCard.tsx`, `BoxscorePanel.tsx`), the compose file, the nightly chain
(`scripts/nightly_ingest.sh`, `deploy/ofelia/config.ini`), the filing record
(`CHANGES.md` 2026-08-29; `docs/archive/BACKLOG-history.md`, the SIM-519 row) and the leftovers
other tickets assigned to this one (`CHANGES.md` 2026-09-30 and 2026-10-07; the one-book odds
plan, §12). All read at commit 80e3f46.
**Decisions** in §16: seven; none taken.

---

## 0. The short version

**What the ticket asks for.** The day view lists every game the league's real schedule shows
for that day, with the correct state, sourced live from the league rather than from our stored
data alone. Each part of the epic can be finished and checked off on its own.

**What this design builds, in the order it should land.**

| Part | Deliverable | Why this order / size |
|---|---|---|
| D | **The nightly finals job runs every night and retries a crash.** The scheduler is on by default. | It stops the data drift that started the ticket. 1.5 days. |
| A | **The slate reads the league schedule**, merges our data per game, and never fails when the league feed is down. Cards carry start times, doubleheader numbers, scores, probable pitchers and our win probability. | The owner's ruling. 3 to 4 days. |
| B | **A game that has not started can be simulated**: the live service writes the published lineup and the probable pitcher into the lineup table. | Without it the slate shows games the platform cannot price. 2 days. |
| C | **The live service** runs as its own container. Live cards and the game page update while a game is played. | 3 days. |
| E | **One simulation run per game**: asynchronous, with progress, cancel, a run-size choice and durable results that every panel reads. | 4 to 5 days. |
| F | **Names in the projections**, grouped by team and batting order; the invented bullpen is one labelled row. | 1 day. |
| G | **A price the book still offers is marked as seen** on every pass (the one-book odds plan left this here). | 1 day. |
| H | The Data Lab seasons list, newest first. | Minutes. |

**What stays outside.** The twelve segment markets on the game page and the closing-price
marker (SIM-546); the preview game's manager and bullpen fallbacks (SIM-547); the database lock
that blocks the nightly profile rebuild (SIM-524); a re-simulation from the live mid-game state
(not filed; §17).

---

## 1. Terms

- **The schedule** — the league's public schedule endpoint
  (`https://statsapi.mlb.com/api/v1/schedule`). It returns, per calendar date, every game with
  its start time, state, teams, score and, when asked, the probable pitchers and the posted
  lineups. It needs no key.
- **The official date** — the schedule's `officialDate`, the local calendar day of the game.
  The schedule's `gameDate` is the start time in UTC. A night game on the West Coast has a
  `gameDate` on the next UTC day. Our tables key on the official date.
- **A card state** — one of four: `scheduled` (the ticket's "preview"), `live`, `final`,
  `postponed`. The code already names them (`GameStatus` in `api/routes/games.py`).
- **The live service** — the process that polls the schedule, subscribes to the league's
  per-game push feed and writes the game state (`pipeline/live/live_ingestion_pipeline.py`).
  Today it exists as a class the app can start in-process; nothing starts it.
- **A simulation run** — one Monte-Carlo batch for one game: N iterations from first pitch,
  one summary (win probability, score distributions), one set of per-player prop
  distributions, and one representative game's linescore, decisions and play-by-play.
- **The runner** — `BatchRunner` (`simulation/batch_runner.py`), the process pool that plays
  the N games. The app holds one warm runner (`app.state.sim_runner`).
- **The lineup table** — `raw.game_lineups`, the rows the simulator resolves a game from. The
  resolver refuses a game with no rows (`LineupNotIngestedError`, a 503 with Retry-After).
- **The sim-run table** — `sim.sim_runs` in Postgres (Alembic 0014), one row per run with the
  summary as JSON. Today it is written only when the DuckDB replay store is also open (§2).
- **A message channel** — Redis publish/subscribe: a process publishes a message on a named
  channel, and every process subscribed to that channel receives it. Nothing is stored.

---

## 2. What the code and the data say

Each row is a fact read from the code at commit 80e3f46, with the place it was read.

| Fact | Where | What it means for the design |
|---|---|---|
| The slate endpoint lists `raw.games` rows for the date, nothing else. | `api/routes/games.py:1070-1116`; `_GAMES_ON_DATE_SQL` at 955-981 | A game the pipeline never upserted is absent. An off day and a stale database look the same ("No games scheduled"). |
| The slate card carries no score, start time, doubleheader number or probable pitcher. | `GameCard`, `games.py:232-266`; `frontend/src/components/games/GameCard.tsx` | Final cards show team records only: the 2026-08-29 test findings. |
| The frontend re-implements the server's eight-value status map. | `frontend/src/api/games.ts` (`rawStatusToGameStatus`); the server's `_RAW_STATUS_TO_GAME_STATUS` at `games.py:373` | Two copies drift. The server should send the mapped state. |
| The live pipeline polls `date.today()` on the container's clock, which is UTC. | `live_ingestion_pipeline.py:1615` | From 8 pm Eastern (midnight UTC) it polls tomorrow's schedule while tonight's games are live. The one-book odds plan logged this ("the schedule poll uses the machine's date"). |
| The live pipeline writes `raw.games.game_date` from `gameDate[:10]`, the UTC date. | `live_ingestion_pipeline.py:2450`; the loader's comment at `etl_historical_loader.py:2918-2922` says so and overwrites the date on the final load | A Central, Mountain or Pacific night game it created sits on the next day's slate until the nightly load corrects it. |
| Nothing starts the live pipeline. The app gate `LIVE_PIPELINE_ENABLED` defaults false and is set in neither `.env` nor `docker-compose.yml`. | `api/main.py:318-358`; the compose file | Live cards never tick; `sim.lineup_state` is never written; the frontend WebSocket has no publisher. |
| The pipeline's broadcast reaches browsers only through an in-process singleton. | `connection_manager`, `live_ingestion_pipeline.py:1351-1413` | A pipeline in its own container cannot reach the app's WebSocket clients without a bridge. |
| The pipeline's vendor reads are synchronous and run on its event loop. | the one-book plan, §12; `_persist_pregame_odds` and the odds cycles | A slow vendor stalls the live refresh. In the app's process it would stall every request. |
| Nothing writes `raw.game_lineups` before a game is final. The loader writes the rows from the final box. The pipeline writes only `sim.lineup_state`. | `etl_historical_loader.py:1743` (`_build_starting_lineup_rows`); a grep for the table under `pipeline/live/` returns nothing | Every upcoming game answers 503 to `/simulate`, `/boxscore`, `/props` and `/edges` until it is over. The main use case (pre-game props) cannot run from the slate. |
| The resolver takes each side's starter from the box; with no box, the lineup's pitcher row stands. | `simulation/lineup_resolver.py:745-760`; `_require_pitcher` at 449 | A pre-game lineup row with the probable pitcher as `P` is enough for the resolver. |
| A game with no bullpen listing gets the synthetic pen (negative ids). | `pipeline/bullpen_usage.py:210-214`; `lineup_resolver.py:891-949` | The "Player -15677556" rows. A preview game has no listing until the box exists. The real fallback is SIM-547's. |
| The nightly scheduler is opt-in (`profiles: ["scheduler"]`), and the chain's steps 2 and 3 need the DuckDB write lock. | the `scheduler` service in `docker-compose.yml`; `scripts/nightly_ingest.sh`; SIM-524 | The chain was never scheduled on this host (the 16-day drift). Even scheduled, it fails at step 2 while the app runs. Step 1 (the finals load) writes Postgres only and is safe. |
| The chain has no retry. The crash class strikes about one run in two. | `nightly_ingest.sh` (`set -eu`, one pass); the filing record; the header of `scripts/resumable_sweep.py` | One crash is one night lost. |
| The Postgres sim-run write sits inside the replay-persist function, after an early return when the DuckDB store is not open. | `games.py:836-862` (`if con is None: return` precedes `store_sim_run`) | With `REPLAY_PERSISTENCE_ENABLED` unset (production), no run is ever stored. The slate card's `sim_summary` is always null. |
| The sim-result cache lives 60 seconds. | `SIM_RESULT_TTL_S = 60`, `batch_runner.py:105` | An identical repeat after a minute recomputes. |
| `/boxscore` plays N games serially in one thread, uncached, outside the runner. | `_build_prop_set`, `games.py:1657-1696` | At about 2.5 seconds a game, 100 games take four minutes. This is the "~5 minutes per identical rerun" the test measured. The docstring names the fix: have the runner keep the per-game box scores. |
| The projections panel renders `Player {id}`. | `frontend/src/components/games/BoxscorePanel.tsx:94` and 123 | Part F. A name resolver exists (`app.state.player_name_resolver`, `api/state.py:317`). |
| The linescore and play-by-play panels say "run a simulation to populate it". Their endpoints answer 503 "replay store unavailable" with the store off. | `GamePage.tsx:206`; `_load_game_card_or_error`, `games.py:1565-1585` | The message is wrong on the default stack. The store will stay off (the writer lock, SIM-524; the file lacks the replay tables). |
| The closing-line marker has no production caller, and its promotion is wrong per book. | `mark_closing_lines`, `live_ingestion_pipeline.py:2678`; the SIM-546 row | SIM-546 rewrites it. This design gives it the Preview-to-Live hook to hang on. |
| A price that moves A to B to A is stored once; a withdrawn price keeps its last row. | the one-book plan, §12; `api/routes/betting.py:84-97` | Assigned to SIM-519 by that plan: a last-seen stamp (Part G). |
| The frontend's "today" is the browser's local date; the API takes a `YYYY-MM-DD` path. | `DaySummaryPage.tsx`, `todayIso()` | It matches the schedule's official-date parameter. No change. |

Two points of scale from the record: the warm n=100 `/simulate` reads 25 to 27 seconds
(`CLAUDE.md` §2b), and the host is core-bound at about ten workers (the host-shape note). A
second concurrent run doubles the wall time of both.

---

## 3. The design in one picture

```
Browser (React)                      app (FastAPI)                         live (new container)
───────────────                      ──────────────                        ────────────────────
Slate  ── GET /api/games/{date} ──►  schedule client ──► MLB schedule ◄── schedule poll (30 s,
  (polls every 30 s while a           + Redis cache                        D-1..D+1 Eastern)
   card is live)                      + per-game merge from Postgres:        │ writes raw.games
                                        sim.sim_runs (latest run),           │ (official date,
                                        raw.game_lineups (lineup_ready),     │  start, DH, probables)
                                        raw.games (fallback when the         │ writes raw.game_lineups
                                        feed is down)                        │ (published lineup +
                                                                             │  probable pitcher) ─► Part B
Game page                                                                    │ subscribes to the
  ── GET /status, /live ───────────►  Postgres (sim.lineup_state)  ◄─────────┤ league push feed per
  ── WS /ws/games/{pk} ────────────►  Redis subscriber ◄── message channel ◄─┤ live game; writes
                                                                             │ sim.lineup_state;
  ── POST /simulate ───────────────►  job registry ─► BatchRunner (pool)      │ publishes the state;
  ── GET  /simulate/runs/{id} ─────►  sim.sim_runs (status, progress,        │ heartbeat key
  ── GET  /boxscore, /props, ──────►    summary, prop set, linescore,        └─ Redis
       /linescore, /plays               decisions, play-by-play)

scheduler (Ofelia, on by default)
  07:00 UTC daily  ─► nightly_finals.sh  ─► loader load_date_range(D-3..D) ─► Postgres (pitches,
                      (retry wrapper)          lineups, box, pen listing, final score)
  Sunday weekly    ─► refresh_seasons(YEAR)
  (profiles + artifacts: DISABLED until SIM-524)
```

The app never calls the league feed on the request path except for the schedule, and the
schedule is cached. The live service is the only process that holds a league push connection.
The simulator reads Postgres only.

---

## 4. Part A — the schedule-driven slate

### 4.1 One schedule client (`pipeline/mlb_schedule.py`, new)

Today three modules build schedule requests by hand (the loader, the live pipeline, the
opening-line job), and the odds provider parses schedule entries in a fourth place. The slate
adds a fifth reader. The new module is the one place that knows the schedule's shape.

```python
@dataclass(frozen=True)
class ScheduleGame:
    game_pk: int
    season: int
    official_date: date            # the slate's key
    start_utc: datetime | None     # gameDate; None when startTimeTBD
    start_time_tbd: bool
    game_type: str                 # R, F, D, L, W, C, P
    abstract_state: str            # Preview | Live | Final
    detailed_state: str            # "Warmup", "In Progress", "Final", "Postponed", ...
    coded_state: str               # S, P, I, F, D (postponed), C (cancelled), U/T (suspended)
    reason: str | None             # "Rain"
    double_header: str             # N | Y (straight) | S (split)
    game_number: int               # 1, or 2 on a doubleheader
    home: ScheduleTeam             # team_id, name, abbreviation, wins, losses, score,
    away: ScheduleTeam             #   probable_pitcher_id, probable_pitcher_name, is_winner
    venue_id: int | None
    venue_name: str | None
    inning: int | None             # from the linescore hydrate, live games
    inning_half: str | None        # "Top" | "Bottom"
    outs: int | None
    rescheduled_to: date | None    # rescheduleGameDate
    rescheduled_from: date | None  # rescheduledFrom
    resumed_from: date | None
    lineups_posted: bool           # the lineups hydrate carries both sides
    raw: Mapping[str, Any]         # the entry, for writers that need more
```

- `async def fetch_schedule(session, *, start: date, end: date, hydrate=DEFAULT_HYDRATE) -> list[ScheduleGame]`
  over `aiohttp` (the live pipeline's transport; the app is async). One request per call:
  `sportId=1`, `gameTypes` = the pipeline's `GAME_TYPES`, `startDate`, `endDate`,
  `hydrate=team,linescore,probablePitcher,lineups`.
- `def parse_schedule(payload) -> list[ScheduleGame]` is pure and tested on recorded responses.
  The first build task records five real days as fixtures (§14): a normal day, a day with a
  split and a straight doubleheader, a day with a postponed game and its make-up, an off day,
  a day with a suspended game. The fixtures pin the field names. A field the hydrate does not
  carry (the linescore's `outs` is the one I did not confirm) reads `None`, and the card hides it.
- `def card_state(game: ScheduleGame) -> GameStatus` is the one status mapper (§4.2).
- A sync wrapper `fetch_schedule_sync` over the loader's `_connect` serves the loader and the
  nightly job, so the ETL keeps its `urllib` transport (the crash-class finding, SIM-446).

The loader's two schedule reads (`etl_historical_loader.py:2097`, `2174`) and the pipeline's
poll move onto this module. The odds provider keeps its own entry parser for now (it reads
vendor-matching fields this client need not know); a later hygiene pass can fold it in.

### 4.2 The status mapper

The league encodes a postponed game as `abstractGameState = "Final"` with
`codedGameState = "D"` and a `detailedState` that starts with "Postponed". The existing
staleness test reads exactly those fields (`_odds_facts_may_be_stale`,
`live_ingestion_pipeline.py:257-265`). The mapper reads the coded state first:

| Coded state / detailed state | Card state |
|---|---|
| `D` (postponed), `C` (cancelled), `U` or `T` (suspended), or a detailed state that starts "Postponed", "Cancelled" or "Suspended" | `postponed` |
| abstract `Preview` (detailed "Scheduled", "Pre-Game", "Warmup", "Delayed Start") | `scheduled` |
| abstract `Live` (detailed "In Progress", "Delayed", "Manager challenge", "Umpire review") | `live` |
| abstract `Final` (detailed "Final", "Game Over", "Completed Early") | `final` |
| anything else | `scheduled`, logged once per unknown value |

The same mapper serves the `raw.games.status` column: the pipeline's upsert writes the
league's eight-value vocabulary as today, and the fallback listing (§4.3) maps it with the
same function. The frontend's copy of the map is deleted; the card reads `game_status`.

### 4.3 The slate endpoint

`GET /api/games/{date}` keeps its path and envelope. Its body changes:

1. **Read the schedule for the date** through the client, from the cache when present. The
   cache is `app.state.sim_cache` (Redis when reachable), key `slate:v2:{date}`. The time to
   live depends on the date class:

   | Date class | TTL | Why |
   |---|---|---|
   | past (before today, Eastern) | 24 hours | the schedule of a played day does not change |
   | today | 20 seconds | live scores and states move; the frontend polls at 30 s |
   | future | 10 minutes | probable pitchers and start times move slowly |

2. **Merge our data per game in two queries**, not N:
   - the latest run per game from the sim-run table
     (`SELECT DISTINCT ON (game_pk) ... WHERE game_pk = ANY($1) AND status = 'done' ORDER BY game_pk, created_at DESC`)
     gives `sim_summary` (the lite projection the status card already uses: win probabilities,
     score means, `n_iterations`, `simulated_at`);
   - the lineup flag and the stored enrichment from `raw.games`, `raw.teams`, `raw.venues`
     and the records CTE (`_team_records_cte`) for the schedule's game_pks give
     `lineup_ready`, `venue_city`, `lineup_source`, and the records when the owner picks the
     database for them (decision D1).
3. **Build the cards from the schedule entries.** A game in the schedule but not in
   `raw.games` is still a card (`db_known=false`) with null enrichment. A row in `raw.games`
   for the date that the schedule does not list is dropped and logged at warning with its
   game_pk (a stale row from the old UTC-date bug; the nightly load corrects it).
4. **Degrade, never fail.** When the league feed errors or times out (5 s), the endpoint
   serves, in order: the last good schedule for the date from a second key `slate:last:{date}`
   (TTL 24 hours, refreshed on every good read); else the stored `raw.games` listing mapped
   through the same mapper. The envelope says which: `source: "schedule" | "schedule_cached" | "db"`
   and `feed_error: str | None`. The frontend shows a banner on anything but `schedule`.
5. **Postponed entries are cards**, not skipped. The pipeline and the loader skip entries with
   `rescheduleGameDate` because they have no pitch data to load. The slate shows them as `PPD`
   with "rescheduled to <date>" when known, as the league's own scoreboard does (decision D7).
   The make-up game appears on its own date with `rescheduled_from`.

The endpoint keeps no authentication, as today. `use_cache=false` bypasses the Redis read and
forces a feed read (for operators).

### 4.4 The card contract (backward compatible)

Every new field is optional, so the Playwright mocks and the unit stubs keep deserialising.

```
GameCard (+)
  game_status: "scheduled" | "live" | "final" | "postponed"     # the mapped state
  detailed_state: str | None                                     # "Warmup", "Postponed: Rain"
  start_utc: str | None                                          # ISO-8601, UTC
  start_time_tbd: bool
  double_header: "N" | "Y" | "S"
  game_number: int
  away_score: int | None     home_score: int | None              # live or final
  inning: int | None   inning_half: str | None   outs: int | None # live
  away_probable_pitcher_id / _name, home_probable_pitcher_id / _name
  rescheduled_to: str | None   rescheduled_from: str | None
  lineup_source: "box" | "published" | "projected" | None       # Part B
  sim_summary: GameSimSummaryLite | None                         # Part E makes it non-null
  db_known: bool
GamesOnDateResponse (+)
  source: "schedule" | "schedule_cached" | "db"
  feed_error: str | None
  fetched_at: str
```

The status card `GET /{game_pk}/status` gains the same fields for one game, read from the
same schedule cache (the game page header shows the start time and the probable pitchers).

### 4.5 The frontend

- `DaySummaryPage` sorts cards: live first, then scheduled by start time, then final, then
  postponed. It polls the slate every 30 seconds while any card is live or the date is today,
  and only while the tab is visible (`document.visibilityState`). The empty state reads
  "No MLB games on <date>." The degraded banner reads "The league schedule feed is
  unavailable. Showing stored games." with the error.
- `GameCard` renders by `game_status`. A scheduled card shows the start time in the viewer's
  zone ("7:05 PM") or "TBD", "Game 2" on a doubleheader, and both probable pitchers. A live
  card shows the score, inning and half. A final card shows the score with the winner in bold.
  A postponed card shows "PPD · Rain" and the make-up date. Every card shows our win
  probability when a run exists ("Sim: NYY 54%", with the run's age) and "Not simulated"
  when none does.
- The frontend's status map (`rawStatusToGameStatus`) is deleted. `frontend/openapi.json` and
  `src/api/schema.d.ts` are regenerated (`scripts/export_openapi.py`; `npm run gen:api`).

---

## 5. Part B — a game that has not started can be simulated

### 5.1 The problem

The resolver reads `raw.game_lineups`. The only writer is the loader, which runs on a final
game's feed. So every upcoming game answers 503 to the four endpoints that price it. The
owner's slate shows "preview" cards the platform cannot act on. The projections, the betting
card and the override panel are dead for the whole upcoming slate.

### 5.2 The published-lineup writer (in the live service)

The schedule poll already asks for `probablePitcher,lineups`. For each `Preview` game whose
entry carries both sides' lineups, the live service:

1. **Upserts the game row first and awaits it** (the foreign key; the pre-game odds task
   learned this the hard way, `live_ingestion_pipeline.py:2270-2275`). The upsert writes the
   official date (§6.3), the start time, the doubleheader code, the game number, the detailed
   state and the probable pitchers (new columns, §12).
2. **Fetches the game's `feed/live` once** when the lineup changed (a hash of the entry's two
   player lists, kept per game in memory; a restart re-reads once). The feed's box score
   carries each starter's batting order and his starting position (`allPositions[0]`, the
   SIM-559 rule) once the lineup is posted. The loader's `_build_starting_lineup_rows` already
   turns that box into lineup rows; the writer calls it. The function adds `pitchers[0]` as
   the `P` row; before first pitch that list is empty, so the writer adds the schedule's
   probable pitcher as the `P` row (batting order null) when no pitcher row came out.
3. **Ensures the players exist** (`raw.players` is a foreign key). A debuting call-up is not in
   the table. The writer upserts the missing ids from `people/{id}` with the loader's existing
   player upsert (`etl_historical_loader.py:2624` reads that endpoint today).
4. **Writes the rows in one transaction**: delete the game's rows where `source = 'published'`,
   then insert the new set with `source = 'published'`, `sequence = 1`, `is_starter = true`,
   `published_at = now()`. A scratch (a lineup change) is one more pass of the same step.
5. **Invalidates the slate cache** for the game's date (`lineup_ready` flips to true) and
   publishes a `lineup_published` message on the game's channel (the game page re-enables its
   Run button).

When the game ends and the nightly loader writes the final box, the loader's write is the
authority. Build-time check: the per-game load path must delete the game's `published` rows
before it inserts (the reload path already deletes and re-inserts a game in one transaction;
confirm the first-load path does the same, or add the delete). The `source` column makes the
rule testable: after a final load, no `published` row remains for the game.

### 5.3 What the simulator then does with a preview game

- **Starter:** the box has no `p_started` row, so the lineup's `P` row stands
  (`fetch_box_starters` returns an empty map; `_require_pitcher` passes). This is the probable
  pitcher. If the actual starter differs (a late scratch), the next poll's feed carries the
  change and step 2 rewrites the row.
- **Lineup:** the published nine with their starting positions, so the defense map is full.
- **Bullpen:** `fetch_pen_for_game` returns `None` (no listing before the box exists), so the
  synthetic pen stands in and `bullpen_source = 'synthetic'`. The real fallback (the team's
  most recent listing, with a staleness flag) is SIM-547's and is not built here. Part F shows
  the synthetic pen honestly.
- **Manager:** `raw.games.home_manager_id` is null for a game the pipeline created (the
  schedule has no manager). The manager draw runs with no manager profile, as an average
  manager. SIM-547 owns the fallback (the active rows of `raw.managers` per team).

The response of every pricing endpoint carries `lineup_source`, so a reader knows the run
used a published lineup, and the game page labels the projections "Published lineup" or
"Final lineup".

### 5.4 Before the lineup posts — the projected lineup (decision D3)

Lineups post two to four hours before first pitch. A morning user sees probable pitchers and
no projections. The option: when a preview game has a probable pitcher and no published
lineup, the writer builds a **projected** lineup from the team's most recent final game: its
`sequence = 1` rows against the same pitcher hand when one exists in the last ten games, else
its last game; the probable pitcher as `P`; `source = 'projected'`. The card and the panels
label it "Projected (last game's lineup)". The published lineup replaces it on post.

This is a modelling input the owner should rule on, so it is a decision, not a default. The
writer and the column are built either way; the flag `LIVE_PROJECTED_LINEUPS` (default off
until the ruling) turns the projection on.

---

## 6. Part C — the live service

### 6.1 Where it runs (decision D2)

Two options were weighed.

**In the app's lifespan (`LIVE_PIPELINE_ENABLED=true`).** The wiring exists. Against it: the
pipeline's synchronous vendor reads would run on the app's event loop and stall every request
(the one-book plan ruled "the provider's retries must stay off in the API container"); the app
hot-reloads on every mounted edit and restarts on every simulator change, which drops every
league push subscription and every browser socket; and the app's memory cap is sized for the
simulation parent and ten workers, not for a second workload.

**Its own container (`live`, the app image, a new entry point).** Recommended. The live
service gets its own restart policy, its own memory cap (1 GB), its own logs and its own
health check. It writes Postgres and Redis, which the app already reads. One bridge is
needed: the frontend WebSocket terminates in the app, so the pipeline's broadcast must cross
processes. Redis publish/subscribe does that in about forty lines.

### 6.2 The bridge and the heartbeat

- The pipeline publishes every browser-bound payload (`game_state_update`, `resim_pending`,
  the new `lineup_published`) on channel `live:game:{game_pk}` instead of calling the
  in-process `connection_manager` directly. A `Broadcaster` protocol with two
  implementations (`LocalBroadcaster`, today's manager; `RedisBroadcaster`) keeps the unit
  tests and the standalone mode working.
- In the app, the WebSocket endpoint `/ws/games/{game_pk}` gains a `RedisBridge`: the first
  browser subscriber for a game starts one Redis subscriber task for that channel; each
  message is forwarded to the game's local subscribers through the existing
  `connection_manager.broadcast`; the last browser disconnect stops the task. The bridge is
  created in the lifespan when Redis is up and is a no-op without it.
- The pipeline sets `live:heartbeat` to the poll time with a 90-second TTL on every schedule
  poll, and `live:watching` to the list of live game_pks. The app's `/ready` reports
  `live_service: "ok" | "stale" | "absent"` from the key's age. The Prometheus freshness
  gauge (`baseball_sim_pipeline_freshness_seconds`) reads the same key instead of the
  in-process stamp it reads today, so the Grafana panel finally has data.

### 6.3 Three fixes inside the pipeline

1. **The poll window.** The poll asks for `startDate = D-1, endDate = D+1`, where `D` is today
   in `America/New_York`, and acts on every entry by its own state. This covers a West Coast
   game past UTC midnight and tomorrow's early lineup posts. The `_completed_games` set keeps
   the final games from re-upserting (today's rule).
2. **The official date.** `_upsert_game_record` writes `officialDate`, never `gameDate[:10]`.
   The loader's "authority" comment at `etl_historical_loader.py:2918` becomes history; the
   loader keeps overwriting the date on the final load, which stays correct.
3. **Vendor reads off the loop.** Every synchronous odds-provider call runs through
   `asyncio.to_thread` on a one-thread executor the pipeline owns (one thread keeps the
   provider's per-process caches single-threaded). A stalled vendor no longer delays a live
   refresh.

### 6.4 The Preview-to-Live hook (for SIM-546)

The schedule poll sees each game's first `Live` state. The pipeline gets one hook,
`on_game_live(game_pk, first_pitch_at)`, called once per game at that transition and
recorded in `raw.games.first_pitch_at`. SIM-546 plugs its rewritten closing-line marker into
this hook. This design does not call the existing marker: its promotion rule is wrong per
book (one row per game across every market), and SIM-546 owns the rewrite.

### 6.5 The frontend's live behaviour

- The slate polls the endpoint every 30 seconds while a card is live (§4.5). No per-card
  socket: one request refreshes the whole slate from the 20-second schedule cache.
- The game page keeps its socket (`useGameSocket`) for the per-pitch field state, and adds a
  REST fallback: while the socket is `closed` on a live game, it re-fetches `/live` every 15
  seconds. The live banner shows "Live updates connected" / "Reconnecting…" as today.
- A live card's "Sim" line is the pre-game run's win probability, and the card labels it
  "pre-game". A re-projection from the mid-game state is a different feature (§17).

### 6.6 The entry point and the container

`python -m pipeline.live.run_live` (new, about 80 lines) waits for Postgres to accept
connections (up to 10 minutes: after a host reboot the database recovers after the containers
start, the blue-screen note), starts the pipeline, the heartbeat and a small HTTP server on
8001 with `/health` (the heartbeat age) and the existing `/api/pipeline/status`, and stops
cleanly on SIGTERM. The compose service:

```yaml
live:
  build: {context: ., dockerfile: Dockerfile, target: runtime}
  restart: unless-stopped
  mem_limit: "1g"
  env_file: [.env]
  environment:
    BASEBALL_DB_DSN: postgresql://baseball_user:baseball_pass@db:5432/baseball_sim
    REDIS_URL: redis://redis:6379/0
    LIVE_PROJECTED_LINEUPS: "0"      # decision D3
    # ODDS_PROVIDER / ODDS_API_KEY come from .env (the pre-game odds cycle)
  command: python -m pipeline.live.run_live
  depends_on: {db: {condition: service_healthy}, redis: {condition: service_healthy}}
  healthcheck:
    test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8001/health')\""]
    interval: 30s
    start_period: 60s
  volumes: [./pipeline:/app/pipeline, ./db:/app/db, ./api:/app/api, ./simulation:/app/simulation]
  networks: [baseball_net]
```

`LIVE_PIPELINE_ENABLED` stays in the app, default false, and its comment now says "never set
this on the compose stack; the `live` service is the one publisher". Two publishers would
double-write every table.

---

## 7. Part D — ingestion currency

### 7.1 The job

`scripts/nightly_finals.sh` replaces step 1 of the current chain and runs on its own:

- `load_date_range(today_eastern - 3, today_eastern)` through the loader. The three-day
  window catches a suspended game completed later, a game the previous night missed, and a
  host that was off for a night. The loader skips games already loaded, so the window costs
  three schedule reads and a handful of per-game checks.
- Exit non-zero when any game failed (today's rule), so the record is honest.

`scripts/weekly_refresh.sh` runs `refresh_seasons(YEAR)` on Sunday: the full-season sweep
that catches anything the window missed.

Steps 2 and 3 of today's chain (the profile computor, the engine artifacts) move to
`scripts/nightly_rebuild.sh` and stay **disabled in the scheduler**, with a comment that names
SIM-524: they need the DuckDB write lock the app's forkserver holds. Until that ticket lands,
they run by hand with the app stopped, as every rebuild has since 2026-09-07. The slate's
correctness needs only the finals load (scores, lineups, box, pen listing). The simulator's
pools lag the current season until the rebuild runs; `/api/data-health` already shows the
pool's `built_at`.

### 7.2 The retry wrapper

`scripts/with_retry.sh <max_attempts> <command...>` runs the command. On exit code 139, or
on any non-zero exit whose output carries `Fatal Python error`, it waits 30 seconds and runs
the command again, up to the limit (default 6). Any other non-zero exit stops at once. The
loader is idempotent per game, so a retry resumes where the crash stopped. The wrapper logs
each attempt's exit code and the total. The Ofelia job calls
`with_retry.sh 6 sh scripts/nightly_finals.sh`.

A unit test runs the wrapper over a fake command that exits 139 twice and 0 the third time,
and over one that exits 2 (it stops after one attempt).

### 7.3 The scheduler on by default (decision D6)

The `scheduler` service loses its `profiles:` gate, so `docker compose up -d` starts it. The
Ofelia config gains the two jobs above and keeps the rebuild job commented. The image name in
that config is the compose default (`baseball_simulator_v2-app`); a worktree build names its
image after the worktree, so the config reads the name from an environment variable with
that default (`COMPOSE_PROJECT_NAME`). The job runs at 07:00 UTC (3 am Eastern), after the
last West Coast final.

### 7.4 Making staleness visible

- The slate footer shows "Data through <latest final loaded> · pools built <date>" from
  `/api/data-health`. A stale database is visible on the page that was stale for sixteen days.
- A Prometheus gauge `baseball_sim_finals_age_hours` (the age of the newest final game loaded
  for the current season) joins `/metrics`; the Grafana dashboard gets a panel and a threshold
  at 36 hours.
- `make ingest-catch-up DAYS=14` runs the window job by hand for a longer gap.

---

## 8. Part E — one simulation run per game

### 8.1 The problem, precisely

Four endpoints each run their own batch when a panel asks (`/simulate`, `/boxscore`, `/props`,
`/edges`). None persists on the default stack (the Postgres write hides behind the DuckDB
gate, §2). The summary cache lives a minute, and `/boxscore` plays its games serially outside
the runner. A reload loses the pending state because the request is a blocking GET; a second
click starts a second batch. The user cannot see progress, choose a size, or cancel.

### 8.2 The job model

A **run** is a row in the sim-run table with a state: `queued → running → done | failed | cancelled`.

```
POST   /api/games/{pk}/simulate            body {n_iterations: 100, base_seed: null}
       → 202 {run_id, status, position_in_queue, n_iterations, progress_done: 0, existing: bool}
GET    /api/games/{pk}/simulate/runs/latest → the newest run of any state (404 when none)
GET    /api/games/{pk}/simulate/runs/{id}  → status, progress, timings, summary when done
DELETE /api/games/{pk}/simulate/runs/{id}  → cancel (idempotent; 409 when already done)
GET    /api/games/{pk}/simulate            → unchanged for API callers; it now records a run
                                             row (status done), so results are durable
```

- **One run per key.** The key is the sha-256 of the runner's cache key (the game spec, the
  seed, N). A POST whose key has a `queued` or `running` row returns that row with
  `existing = true`. The guard is a partial unique index on `(spec_key) WHERE status IN
  ('queued', 'running')`, so two app processes cannot both start it. A row with status
  `running` that is older than the app's start is marked `failed('restart')` at boot (the
  registry is in-process; the rows outlive it).
- **A queue, not a thundering herd.** `SIM_JOB_MAX_CONCURRENT` (default 1) run jobs execute
  on the shared runner; the others wait as `queued` with their position. The host is
  core-bound, so two concurrent n=100 runs each take twice as long; one at a time finishes
  the first sooner.
- **Progress and cancel are real.** `BatchRunner.run` gains `on_progress(done, total)` and
  `should_cancel()` callbacks. `_execute` submits the seeds in chunks of `max_workers × 2`
  and, between chunks, reports progress and checks the cancel flag. A cancelled run returns
  what it has, and the job marks the row `cancelled`. Within a chunk, the running games finish
  (a few seconds). The job writes `progress_done` to the row every chunk, so a reload shows
  the bar where it is.
- **The run's artifacts are stored in Postgres**, on the run row (decision D4):
  `summary` (today), `prop_set` (every player's prop distributions, the data `/boxscore` and
  `/props` serve), `linescore` and `decisions` (the representative game's card), `play_by_play`
  (the representative game's entries, about 300 rows as JSON), `inning_grids` (the N games'
  runs by inning, the data SIM-546 needs for the segment markets), `lineup_source`,
  `bullpen_source`, `requested_by`. The sizes are small (the prop set for thirty players is
  about 100 KB). The DuckDB replay store keeps only the per-pitch state snapshots behind its
  flag; the Postgres write no longer waits on it.
- **The runner keeps the per-game box scores.** `_execute` already returns every game's
  `GameSimResult` to the parent; `GameSimSummary.from_results` reads them and drops them.
  The job builds the prop set from the same results (`PropDistributionSet.from_boxscores`)
  and the inning grids from each result's linescore, then drops them. Build-time check:
  confirm `simulate_game` fills `GameSimResult.boxscore` on the runner path as
  `record_game_plays` does; if it does not, a kwarg turns it on for the job's spec. This
  removes the second serial batch `/boxscore` runs today.

### 8.3 The panels read the run

`/boxscore`, `/props/{player}/{prop}`, `/linescore`, `/decisions`, `/card` and `/plays` take an
optional `run_id` and default to the game's latest `done` run. With no run they answer
`404 {detail: "no simulation run for this game", hint: "POST /simulate"}`; the frontend shows a
"Run simulation" prompt in the panel, not a message about a replay store. The betting edge
endpoint reads the latest run's summary for the three full-game markets (SIM-546 adds the
twelve from `inning_grids`). The slate card's `sim_summary` is the same latest run.

The three endpoints that run a batch today when asked with `n_iterations` and `base_seed`
keep that path for API callers, and the OpenAPI description marks it as the synchronous form.

### 8.4 The frontend

- A **Simulation** card at the top of the game page: a size menu (100 · 300 · 1000, with a
  time estimate from the last measured per-run cost), **Run**, and the run's state. Queued
  shows the position; running shows a progress bar (`progress_done / n`) polled every 2
  seconds; done shows "N iterations · <age> · published lineup" and a **Re-run**; failed shows
  the error. On mount the page calls `/simulate/runs/latest`, so a reload resumes the bar.
  **Cancel** calls the DELETE.
- The Projections, Betting, Linescore and Play-by-play panels render from the latest run;
  each shows the "Run simulation" prompt when there is none. The panels' own run buttons go.
- A preview game whose lineup is not posted shows the 503's words ("lineup not yet
  published — try again closer to game time") with the Retry-After as a countdown, and the
  probable pitchers. Part B's `lineup_published` message re-enables Run.

### 8.5 Limits stated plainly

- Cancel stops between chunks; the games of the current chunk finish. The UI says
  "Cancelling…" until the row flips.
- The in-process queue is lost on an app restart; the rows are not. Queued rows older than
  the restart are marked `failed('restart')`, and the page offers Re-run. A durable queue is
  not worth its weight for one host.
- The API cap of 10,000 iterations stays; the UI offers up to 1,000 (decision D5).

---

## 9. Part F — the projections content

- `BoxscoreCardModel.players[*]` gains `name`, `side` (`home` / `away`), `role`
  (`batter` / `pitcher`), `batting_order` (null for a pitcher), `position` and `synthetic: bool`.
  The endpoint resolves names in one query through `app.state.player_name_resolver`
  (`raw.players.full_name`), and the side, order and position from the resolved lineup. The
  prop edge endpoint's response gains `player_name`.
- The panel groups by side, orders batters by batting order and pitchers after them, shows
  the name with the position, and links the name to `/player/{id}`. The chart header reads
  "Gerrit Cole — K", not "Player 543037 — K".
- **The synthetic pen is one row per side.** Lines with a negative id are summed into a row
  named "Bullpen (generic)" with `synthetic = true`, and the panel notes "This game's bullpen
  is not listed yet; the relievers are a generic pen." When the box lists the pen (every final
  game; a preview game after SIM-547), the real relievers show by name.

---

## 10. Part G — odds currency: the last-seen stamp

The one-book odds plan (§12) left this with the live-slate epic. The writers insert a row
per (game, market, book, price) and skip an identical row (`ON CONFLICT DO NOTHING` on the
hash). So a book's current price that moves A to B to A keeps B as its newest row, and a
price the book withdraws keeps its last row forever. `/edges` on a preview game may offer
either.

- Alembic 0029 adds `last_seen_at TIMESTAMPTZ` to `raw.game_odds` and `raw.prop_odds`,
  default `NOW()`.
- The two live writers change the conflict clause to `DO UPDATE SET last_seen_at = NOW()`,
  so a price the book still posts is re-stamped on every pass. The historical loader's
  closing rows are untouched (one per game, by design).
- The pre-game read in `api/routes/betting.py` (`_latest_pregame_rows` and its SQL) keeps a
  book's `current` row only when `last_seen_at` is within two pre-game cadences (20 minutes)
  of the game's newest pass; a `closing` row is kept regardless. A withdrawn or superseded
  price falls out on its own.
- A unit test: three passes A, B, A; the read returns A. A second: A, then nothing; after
  twenty minutes the read returns no current row for that book.

---

## 11. Part H — the Data Lab seasons order

`SummaryPage.tsx` lists seasons ascending and defaults to the last. The select lists newest
first; the default stays the newest. One line in the frontend; the API's ascending order is
untouched.

---

## 12. Data model changes

**Alembic 0029 (Postgres), additive, `IF NOT EXISTS` throughout.**

| Table | Columns | Written by |
|---|---|---|
| `raw.games` | `start_utc TIMESTAMPTZ`, `start_time_tbd BOOLEAN`, `double_header CHAR(1)`, `game_number SMALLINT`, `detailed_state VARCHAR(40)`, `home_probable_pitcher_id INTEGER`, `away_probable_pitcher_id INTEGER`, `first_pitch_at TIMESTAMPTZ`, `schedule_seen_at TIMESTAMPTZ` | the live service's upsert (unconditional for the schedule fields); the loader's final load with `COALESCE` for the probables (the box's starters win where they differ) |
| `raw.game_lineups` | `source VARCHAR(16) NOT NULL DEFAULT 'box'` (`box` / `published` / `projected`), `published_at TIMESTAMPTZ` | the live service (`published`, `projected`); the loader (`box`, the default) |
| `sim.sim_runs` | `status VARCHAR(16) NOT NULL DEFAULT 'done'`, `progress_done INTEGER NOT NULL DEFAULT 0`, `requested_at`, `started_at`, `finished_at TIMESTAMPTZ`, `error TEXT`, `spec_key VARCHAR(64)`, `requested_by TEXT`, `lineup_source VARCHAR(16)`, `bullpen_source VARCHAR(16)`, `prop_set JSONB`, `linescore JSONB`, `decisions JSONB`, `play_by_play JSONB`, `inning_grids JSONB`; index `(game_pk, status, created_at DESC)`; partial unique index `(spec_key) WHERE status IN ('queued', 'running')` | the job (Part E); the synchronous `/simulate` (status `done`) |
| `raw.game_odds`, `raw.prop_odds` | `last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()` | the live writers (Part G) |

The existing rows take the defaults: every stored run is `done`, every lineup row is `box`.
The downgrade drops the columns and the two indexes.

**DuckDB: no change.** The schema stays v31. The replay store's tables are not touched; the
panel data moves to Postgres by design (decision D4).

---

## 13. Configuration and compose changes

| Change | Where |
|---|---|
| New service `live` (§6.6); `scheduler` loses its profile gate; the Ofelia config gains `nightly-finals` (daily 07:00 UTC, wrapped) and `weekly-refresh` (Sunday) and keeps `nightly-rebuild` commented with the SIM-524 note; the image name reads `${COMPOSE_PROJECT_NAME:-baseball_simulator_v2}-app` | `docker-compose.yml`, `deploy/ofelia/config.ini` |
| `SIM_JOB_MAX_CONCURRENT=1`, `SLATE_FEED_TIMEOUT_S=5`, `LIVE_PROJECTED_LINEUPS=0`; `ODDS_PROVIDER` / `ODDS_API_KEY` documented for the live service; the `LIVE_PIPELINE_ENABLED` comment rewritten | `.env.example`; the `app` and `live` environments in `docker-compose.yml` |
| `make ingest-catch-up DAYS=n`, `make live-logs` | `Makefile` |
| `frontend/openapi.json` and `src/api/schema.d.ts` regenerated; the e2e mocks gain the new optional fields | `frontend/` |

The nginx config needs no change: `/ws/` already proxies to the app, which now bridges.

---

## 14. Tests and gates

**Unit (host Python, no network, no database).**

- `tests/unit/test_sim519_schedule_client.py`: `parse_schedule` over the five recorded
  fixtures (`tests/fixtures/mlb_schedule/*.json`): field by field on a normal game; the split
  and straight doubleheaders (`game_number` 1 and 2, `double_header` S and Y); the postponed
  entry maps to `postponed` with `rescheduled_to`; the make-up carries `rescheduled_from`;
  the off day parses to an empty list; the suspended game maps to `postponed`; a `startTimeTBD`
  entry has `start_utc = None`. `card_state` over a table of thirty (coded, detailed,
  abstract) triples.
- `tests/unit/test_sim519_slate_endpoint.py` (the `test_api_games.py` idiom: a tiny app with
  the games router, a fake pool, a fake schedule client on `app.state`): the merge by
  game_pk; a schedule game with no DB row is a card with `db_known = false`; a DB row the
  schedule lacks is dropped and logged; the latest run per game in one query; the TTL by date
  class; the degraded chain (feed error, then cached, then DB) with `source` and
  `feed_error`; the envelope still validates against the old mocks.
- `tests/unit/test_sim519_lineup_writer.py`: the writer's row set from a recorded pre-game
  feed; the probable pitcher as the `P` row when `pitchers` is empty; idempotence (the same
  entry twice makes one write); a scratch rewrites; `source = 'published'`; the players upsert
  for an unknown id; after a simulated final load no `published` row remains.
- `tests/unit/test_sim519_live_bridge.py`: `RedisBroadcaster` publishes on the right channel;
  the app's bridge forwards to local subscribers and stops its task on the last disconnect;
  the heartbeat key and its TTL; the poll window in Eastern time around midnight UTC; the
  official-date write; the Preview-to-Live hook fires once.
- `tests/unit/test_sim519_sim_jobs.py`: the state machine; the dedup by key
  (`existing = true`); the queue position; progress callbacks per chunk; cancel between
  chunks; restart recovery marks orphans `failed`; the artifacts on the row; the panels read
  the latest `done` run and 404 without one; the `/simulate` GET still records a row.
- `tests/unit/test_sim519_boxscore_names.py`: names, side, order, position; the generic pen
  row sums the negative ids per side.
- `tests/unit/test_sim519_last_seen.py`: the A, B, A read; the withdrawn price ages out.
- `tests/unit/test_sim519_retry_wrapper.py`: exits 139, 139, 0 make three attempts and a
  success; exit 2 makes one attempt.

**Regression lane:** unchanged (no engine touched). **Integration (testcontainers, the weekly
lane):** migration 0029 up and down on a seeded database; the writer's rows make
`resolve_lineup` succeed for a preview game; the job's row round-trips its JSON.

**Frontend:** `npm run type-check`, `npm run lint`, `npm run build` (CI's `frontend` job);
Playwright specs for the three card states with a doubleheader label and a start time, the
postponed card, the degraded banner, the empty off day, and the simulate flow (POST, queued,
running with a progress bar, done, with mocked polling; cancel).

**Live verification (the ticket was filed from a live frontend test; it closes with one).**

| Check | Pass when |
|---|---|
| Today's slate in the browser against the live stack | every game the league's own scoreboard lists is a card with the right state, start time and doubleheader number; an off day reads "No MLB games" |
| The live service | `docker compose ps` healthy; `/ready` reads `live_service: ok`; a live game's page ticks within 30 s of a pitch; `sim.lineup_state` updates |
| A game that has not started | after the lineup posts, `raw.game_lineups` holds its `published` rows; `POST /simulate` runs; the projections show names and one "Bullpen (generic)" row per side |
| The nightly job | `with_retry.sh 6 sh scripts/nightly_finals.sh` by hand, with an injected exit 139 on the first attempt, loads the window on the second; the next morning's slate shows yesterday's finals with scores |
| The simulate flow | a reload during a run resumes the bar; a second Run during a run returns the same run; Cancel stops it within one chunk; the slate card shows the run's win probability |
| The odds read | a book's price that returns to A reads A on `/edges` |

**Static gates:** `ruff check`, `ruff format --check`, `mypy similarity/ pipeline/ api/`,
the unit lane with its 80% coverage gate.

---

## 15. Run book

1. **Merge the code** on a branch per part (the branch rule, `CLAUDE.md` §7). Parts D, A, B,
   C, E, F, G and H can merge in that order; A and B share the client and the migration, so B
   follows A.
2. **Apply the migration**: `make migrate` (Alembic 0029). Additive; the app serves throughout.
3. **Rebuild the image** (`scripts/` and `betting/` are not mounted; the new entry point and
   scripts live in the image): `docker compose build app`, then `docker compose up -d app live`.
4. **Start the scheduler**: `docker compose up -d scheduler`; confirm with
   `docker compose logs scheduler` that both jobs are registered. Run the finals job once by
   hand to catch up: `make ingest-catch-up DAYS=14`.
5. **Confirm the live service**: `/ready` reads `live_service: ok`; on a game day,
   `docker compose logs -f live` shows the schedule poll, a `lineup_published` write and, at
   first pitch, the push subscription.
6. **Regenerate the frontend client** (`scripts/export_openapi.py`, `npm run gen:api`), build
   the frontend image (`docker compose build nginx`), `docker compose up -d nginx`.
7. **Run the live verification table** (§14) and record it in `CHANGES.md`.
8. Delete the per-part branches on merge.

No DuckDB write, so no app stop and no SIM-524 exposure.

---

## 16. Decisions for the owner

| # | Question | Recommendation |
|---|---|---|
| **D1** | **Whose records on the cards?** The schedule's `leagueRecord` (what the league's scoreboard shows; present for every game, past and future) or our season-to-date CTE from `raw.games` finals (ours; wrong when the database trails). | **The schedule's**, with ours as the fallback when the feed is down. One source for every number on a card. |
| **D2** | **Where the live service runs.** Its own container with a Redis bridge, or inside the app under `LIVE_PIPELINE_ENABLED`. | **Its own container** (§6.1). The in-app path stalls requests on vendor reads and loses its subscriptions on every reload. |
| **D3** | **A projected lineup before the real one posts** (the team's last game's nine against the probable pitcher), labelled as projected, so morning users get projections. | **Build the writer and the label; land it OFF** (`LIVE_PROJECTED_LINEUPS=0`) and turn it on by your call. It is a modelling input: the sim would price a lineup that may be wrong by two or three names. |
| **D4** | **Where a run's panel data lives.** Postgres on the run row (this design) or the DuckDB replay store behind `REPLAY_PERSISTENCE_ENABLED`. | **Postgres.** The DuckDB path is blocked by the writer lock (SIM-524) and the file lacks the replay tables; the data is small. The per-pitch snapshots stay DuckDB-optional. |
| **D5** | **Run sizes and concurrency.** The UI's menu (100 / 300 / 1000) and one run at a time. | **As stated.** 100 is the certified batch; 1000 is about four minutes warm; two at once each take twice as long. |
| **D6** | **The scheduler on by default**, with the profile/artifact rebuild job disabled until SIM-524. | **Yes.** The drift began because the job was opt-in. The rebuild cannot run unattended while the app holds the lock. |
| **D7** | **Postponed games as cards** (PPD with the make-up date), not hidden. | **Yes.** It is what the league's own day view shows, and a hidden game is a question the user cannot answer. |

A decision changes the build; none changes the order of work.

---

## 17. Risks, and what this design does not do

**Risks.**

- **The schedule's field set.** The hydrate names come from the league's documented API and
  this repository's own readers (`probablePitcher`, `lineups`, `team`); the linescore's outs
  and runners on the schedule endpoint are the one field set I did not confirm in this
  codebase. The first build task records the fixtures; a missing field reads null and the card
  hides it.
- **A new season's foreign keys.** `raw.games` references `raw.teams` and `raw.venues` by
  `(id, season)`. On the first day of a season the live service may see games before those
  rows exist. The writer must ensure the season's team and venue rows (the loader has the
  upserts) before the game row, or the upsert fails silently as it did before SIM-438. A unit
  test covers the order.
- **Two writers of the lineup table.** The `source` column and the delete-before-insert rule
  keep them apart; the build-time check in §5.2 confirms the first-load path deletes.
- **The vendor behind the live service.** The pre-game odds cycle reads the vendor every ten
  minutes per game. With the provider's retries off (the one-book ruling), a vendor outage
  costs that pass only.
- **The host.** The blue screens continue until the owner removes the driver. Every service
  here has `restart: unless-stopped`, waits for Postgres and is idempotent, so a reboot costs
  minutes, not a night.

**Not in this design.**

- The twelve segment markets on the game page and the closing-price marker: **SIM-546**. Part
  E stores `inning_grids`, and Part C exposes the Preview-to-Live hook, so SIM-546 builds on
  both.
- The preview game's manager and bullpen fallbacks: **SIM-547**. Part B leaves the synthetic
  pen and the average manager in place and labels them.
- The DuckDB writer lock and the unattended profile rebuild: **SIM-524**. Part D schedules
  the finals and leaves the rebuild disabled, with the reason written next to it.
- **A re-simulation from the live mid-game state.** The loop simulates from first pitch. A
  live card's win probability is the pre-game run's, labelled so. The feature (build a
  `GameState` from `sim.lineup_state` and run from the current inning) is a ticket to file
  when the build starts, not a part of this one.
- **Odds and props on the slate card.** The card shows our win probability; the book's line
  and the edge belong on the game page until the edge surface (SIM-546) settles its shape.
- Login by default (SIM-443c). The slate stays open as today; the simulate POST requires the
  same auth as the GET.

---

## 18. Order of work and size

| Order | Part | Days | Depends on |
|---|---|---|---|
| 1 | D — the nightly finals job, the wrapper, the scheduler on | 1.5 | — |
| 2 | A — the schedule client, the mapper, the endpoint, the cards | 3–4 | — |
| 3 | B — the published-lineup writer; the probable pitcher | 2 | A (the client), the migration |
| 4 | C — the live container, the bridge, the three fixes, the hook | 3 | A, B |
| 5 | E — the run job, the stored artifacts, the panels, the frontend | 4–5 | the migration |
| 6 | F — names and the generic pen | 1 | E (the panel reads the run) |
| 7 | G — the last-seen stamp | 1 | the migration |
| 8 | H — the seasons order | 0.1 | — |

About sixteen to eighteen days of build, plus a game day of live verification for each part
that touches the live path (A, B, C, E). Each part closes on its own with a `CHANGES.md`
entry; the row in `BACKLOG.xlsx` carries the parts done while any remain open.

---

## 19. Documents to update when the parts land

- `CLAUDE.md` §2b (the slate bullet; the live service as a running process), §5 (the new
  module, the entry point, the scripts), §8 (the new make targets).
- `docs/technical/api.md` (the slate endpoint, the run endpoints, the bridge);
  `docs/technical/pipeline-betting-db.md` (the schedule client, the writer, migration 0029);
  `docs/technical/scripts-frontend.md` (the three scripts, the page changes).
- `WORKFLOW.md` (the `live` and `scheduler` services in the bring-up; the catch-up target).
- `deploy/monitoring/grafana` (the finals-age panel).
- The ticket's row: the parts done, then its deletion on the last.
