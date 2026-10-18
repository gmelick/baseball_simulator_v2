# CLAUDE.md — MLB Baseball Simulation Platform

> Project guide for Claude Code. This is a pitch-by-pitch Monte-Carlo MLB game simulator built and run
> as a **sports-trading hedge fund**: the product is player-prop prediction and betting-edge validation,
> anchored to **Closing Line Value (CLV)** as the gold-standard metric. Work is executed by a **9-agent
> team** with cross-validation. Read this file first before starting work.

---

## 1. Purpose & goals

- **What it is:** ingests real Statcast pitch-by-pitch data, builds **11 similarity engines** across
  player/situation dimensions, and runs a stochastic pitch-by-pitch game simulator that produces 100
  independent game iterations per request. Outputs feed win probabilities, per-player prop distributions
  (PMFs), boxscores, and a betting/CLV surface.
- **Primary use case:** predict player props (pitcher Ks, batter hits/HR/TB, etc.) and identify +EV
  betting edges, validated against CLV. Every model number must ultimately be *calibrated* — uncalibrated
  numbers must not reach users.
- **End state:** a live game dashboard (day slate → 3-state game cards → game page with play-by-play,
  per-player projections, linescore, field graphic, managerial override) on top of the completed API.

## 2. Current status (read this)

- **Phases 1–6 are complete; Phase 7 is in progress** (§12). The platform runs on Python 3.13
  and numpy 2.x. The frontend is React 18 + Vite + TypeScript (SIM-378). DuckDB schema **v32**,
  Alembic head **0030** (§5).
- **The simulator has one path: the full-pool similarity sampler** (SIM-486). The no-DB tests
  and the batch runner's no-DB factory build the same `StateMachine` over
  `simulation/synthetic_bundle.py`.
- **The production flags live in the `app` service env of `docker-compose.yml`.**
  `tests/conftest.py` pins `SIM_MANAGER` and `SIM_BB_PLATOON` OFF, so the unit lane stays
  byte-identical.
- **Calibration is live.** The app applies `/data/calibration.json` at boot. `make calibrate`
  drops the win-probability reliability curve; the §2b manager-draw bullet says how to restore it.
- **§2b holds the current state, the owner's standing rulings and the in-flight work.** The open
  tickets are the rows of `BACKLOG.xlsx`. The dated status log of 2026-05 to 2026-09 is in
  `docs/archive/CLAUDE-md-history.md`.

## 2b. ⚠ IN-FLIGHT WORK — read before touching the ETL or running a recompute

**The open-ticket list is `BACKLOG.xlsx` (owner decision 2026-09-10):** one tab, one row per
open ticket, ranked by priority, with a plain-English description, a definition of done, and a
proposed solution for each. The closed-ticket narrative and sprint history that `BACKLOG.md`
carried is frozen at `docs/archive/BACKLOG-history.md`; nothing new is appended there. Read
`BACKLOG.xlsx` for the current ticket list; read `docs/archive/BACKLOG-history.md` only for
history.
**The subtitle row (row 2) carries ONLY the updated date, the two pointers and the next free
ticket ID (owner ruling 2026-09-12).** Do not write closure or status notes into it — a
closure, a status change or a data-run result goes to `CHANGES.md`, and a ticket's own row
carries its status while it is open. Update the ID in the subtitle each time you file a
ticket; a closed ticket's row is deleted, and `CHANGES.md` is its record.

**The CURRENT state authority is `BACKLOG.xlsx`'s open tickets, plus two docs:
`docs/audit/2026-08-20-sim429-514-diagnosis-results.md` (why the model reads the way it
does) and `docs/audit/2026-08-28-sim476-fit-plan.md` (the SIM-476 kernel fit, done 2026-08-30).** The
standing owner rulings that govern all new work:

- **The architecture rule, both clauses (2026-08-10 + 2026-08-29):** every decision is a
  similarity-weighted draw from a hard-filtered pool — never a hand-tuned formula — and
  **the drawn row IS the play**: no post-draw adjustment of any kind; every factor is a
  draw WEIGHT or OFF until its weight lands. **Third clause (owner ruling 2026-09-16):
  every change lands ON at its best-known default, and no change waits on a per-change
  accuracy run.** The paired accuracy comparison on 250 games resolves only large effects
  (its `min n` column: the strikeout market needs 83,000 pitcher-games to resolve a
  0.0005 Brier effect; 250 games give 482), so three hours per change bought a coin flip
  on the small ones. Per change, the gates are cheap: the unit and regression lanes, and a
  short sim smoke of the affected channels (`scripts/sim_stats.py`, ten games) to catch a
  collapse. The weights are fitted TOGETHER, once, when the owner calls the model final:
  one designed experiment over every factor (`scripts/sim548_design.py` — each arm flips
  several factors, so 16–20 arms read every factor's effect and the pairwise interactions;
  about three days of unattended compute). "OFF until its weight lands" now means OFF
  until its best-known default exists, not OFF until a paired read confirms it.
- **No betting-value measurement until everything is green (owner ruling 2026-09-08):** the
  closing-line-value re-measure and any edge read built on it wait until every band the lanes
  grade is inside its range. As of 2026-09-10 the certified production arm is green on every
  pool band and on runs (the balanced 45 × 130 lane); the one open red is the pitch /
  pitch-result split's strikeout shortfall (SIM-527) — the split was FLIPPED ON on 2026-09-14
  by owner decision on the accuracy comparison, so that red is now IN production and the
  lane has not re-graded it; the home-win channel is underpowered below 13,365 game-sims.
  The strikeout-prop refit may proceed.
- **The play-picker redesign (owner rulings 2026-09-08; the plan is
  `docs/audit/2026-09-08-sim523-play-picker-redesign-plan.md`):** the weight order in the
  pitch draws is pitcher first, batter, recency, catcher receiving last, with powers fitted
  under that constraint; every actor factor is its engine's 0-to-1 score, emitted nightly as a
  matrix and looked up at draw time; the loop is manager decisions per plate appearance, then
  per pitch the steal draw, the pitch draw, the pitch-result draw (the batted ball is born
  there; since SIM-554 the pickoff draw comes before the pitch and the steal draw after its
  result), then park geometry, the fielding draw and the advancement draws; catcher receiving
  is a mass-preserving ball-strike ratio on taken pitches that ships OFF (enable = SIM-526);
  a concentration check (no factor may put more than its natural share of a draw on the live
  player's own team) joins the grade. **COMPLETE 2026-09-09:** production runs the certified
  arm (the actor score matrices at their fitted powers, the fielding draw's fits); the actor
  bell-curve kernels are deleted; the split stays OFF (SIM-527).
- **The balanced certifying set (owner ruling 2026-09-09):** the lane grades on 45 games
  from three full-day slates (every team three times), chosen so every graded channel's
  actor-matched expectation sits within 0.6% of the pool's totals
  (`scripts/sim523_game_set.py`; the park factors and the prefix order in
  `tests/acceptance/bands.py`); the default lane is 45 × 130 = 5,850 game-sims. It replaces
  the fixed 12-game set and absorbs the date-range lane (SIM-497a/b).
- **One in-play path (SIM-486, 2026-09-06):** the per-tile FAISS fallback, the injected
  `PlayResolver`, the legacy advancement code (`_advance_runners` and its constants), the
  SIM-318 foul re-weight and the `SIM_FULL_POOL` switch are DELETED. Every no-DB test and
  the batch runner's no-DB factory build the production `StateMachine` over an in-memory
  `simulation/synthetic_bundle.py` bundle, so the suite runs the code users get. A missing
  artifact bundle now FAILS the worker loudly (no fallback simulator). The nightly chain
  rebuilds the bundle (`make engine-artifacts`); it used to rebuild dead FAISS tiles.
- **The grade is POOL TOTALS (2026-08-20):** the sim's frequencies certify against the play
  pool's own totals (`tests/acceptance/bands.py` `POOL_REFERENCES`, 16 per-opportunity
  bands); R / home_win_pct stay game-graded. **CERTIFIED 2026-09-09 (45 × 130 on the
  balanced set): all 16 pool bands PASS, R +0.1%;** the only non-pass is home_win_pct
  (underpowered below 13,365 game-sims). **⚠ Two corrections from 2026-09-21 (the fence
  certification, CHANGES.md):** that lane, and every lane before 2026-09-21, ran the
  fence stage INERT — the lane never put the venue on the game state, so the stage
  passed every air ball (production and the accuracy comparison do resolve the venue;
  the lane now does, and refuses to run the stage without one). And the first 45 × 130
  lane since the powers-to-1 ruling (2026-09-21, the stage active) reads **four bands
  RED: walks per PA +4.0%, hit by pitch +6.7%, steal attempts at second −9.1% and at
  third −17.9%**; the other twelve pass (home runs +1.2%). Those four are the state of
  production after the flips since 2026-09-09, unread until this lane. **The 2026-09-22
  lane (the fence amendments: the carry offset, the class ruler, the wall-margin band —
  `scripts/sim478_lane.txt`) reads the same four red plus doubles per ball in play +5.8%**:
  the wall play is now right (near-wall balls draw doubles as the real ones do) and the
  fix unmasked an over-production of doubles on ordinary air balls that the born kernel's
  bandwidth owns (the sweep's); singles, hits, runs and home runs pass.
- **The pool's class labels are checked against real plate appearances (SIM-553, 2026-09-25).**
  A two-strike foul tip or foul bunt is strike three in the pitch pool (`SQL_OUTCOME_TYPE`,
  builder `sim553.1`, all ten seasons rebuilt 2026-09-25); it used to be coded `foul`, and the
  count chain put the simulator's loss at 4.4% of its strikeouts (7.1% of 2025's strikeouts end
  this way). The four chain band centres moved to K_PA 0.2262 / BB_PA 0.0829 / HBP_PA 0.0112 /
  PITCHES_PA 3.906. A paired read (old vs new bundle, 10 games × 300) measured the sim's
  strikeouts +3.5% per plate appearance (±0.6%; the pool moved 4.6%) and runs −2.8%, so the
  next lane's K_PA band should read about −1.8% (floor 2%). `pipeline/batch/pool_chain.py` holds the count chain and the label check;
  `tests/acceptance/test_sim553_pool_label_check.py` and `scripts/pool_window_census.py
  --strict` fail when the pool's chain drifts more than 0.5% from real play — the pool-totals
  grade cannot see a labelling defect, because it reads the same labels. The steal and
  advancement pools in DuckDB are AHEAD of the bundle (the 2026 games of 08-14 to 08-29,
  never exported); the next `--what pool` export ships that refresh.
- **The running game on the pitch (SIM-554, CLOSED 2026-10-03: built 2026-10-01, the run
  book RAN 2026-10-03, production runs it).** On every pitch with a runner who
  could steal, the loop now makes four draws in the order things happen. (1) The pickoff
  draw, BEFORE the pitch: one row of the steal opportunity pool's count group (target base,
  outs, balls, strikes), pitch rows and pickoff rows alike, with the steal draw's weights
  and no manager weight. An outcome resolves at once. A pickoff that makes the third out
  ends the half-inning with no pitch thrown: the result reads `pitch_outcome = NO_PITCH` and
  `pa_voided = "pickoff_third_out"`, no pitch, event or plate appearance is credited (the
  ledger credits the out to the pitcher when it records it, SIM-557; a picked-off caught
  stealing charges the runner a CS), and the
  same batter leads off his team's next inning. Every reader of a per-pitch result must skip a no-pitch result
  (`PlayResult.no_pitch`). (2) The pitch and (3) its result, as before, on the bases the
  pickoff left. (4) The steal draw, AFTER the result, among real pitches of the same base,
  outs, count and CLASS (ball, called strike, swinging strike, foul, in play, hit by pitch).
  A class group answers from its own rows however few; nothing falls back and nothing is
  cancelled. The old single pre-pitch draw read only the count, so 36% of its attempts
  landed on a pitch that never carries a real one. Two rules of baseball land with it, in
  both orders (the plan's decisions 3 and 4, taken by the owner 2026-10-01). With two outs,
  a third strike the catcher holds is the third out first, so a staged steal is void
  (`steal_voided = "third_out_first"`). On a dropped-third-strike reach the got-away advance
  runs first: every runner moves up one base, a run from third pays no RBI, and then the
  batter takes first; a steal or a pickoff on the same pitch skips the advance. The switch
  is `SIM_STEAL_PITCH_CLASS` (default 1; 0 = the single pre-pitch draw of SIM-474). A bundle
  exported before migration 0031, or one with any unclassed pitch row, also runs the single draw. **The data:**
  DuckDB migration 0031 (schema v31) adds two columns to `sim.steal_opportunity_pool`:
  `pitch_class` (the class of the pitch the row rode; NULL on a pickoff row) and
  `is_pickoff_row`. A pickoff row is a pickoff throw with an outcome before the plate
  appearance had any pitch with that runner situation: `pitch_id` = minus the
  `raw.play_events` id, the count 0-0, the catcher of the nearest pitch of the half-inning.
  The pool held about 72% of real pickoff outcomes: 22% of those with a stealable runner
  came before the first pitch and were dropped, and 7% had no stealable runner. The pickoff
  rows raise the pool's pickoff outcomes from about 0.15 to about 0.19 a game. Builder
  `sim554.1`. The export carries `pitch_id`, `pitch_class` and `is_pickoff_row`, in
  `pitch_id` order; `StealPool.pitch_class` holds codes 1-6 (0 = no class; None = an old
  bundle) and `StealPool.is_pickoff_row` marks the pickoff rows. `RunningGameTally` (`machine.running_game_tally`) counts the pickoffs, the
  no-pitch third outs, the voided steals and the dropped-third-strike advances; it changes
  no play. **The run book RAN 2026-10-03** (the app stopped; the writer lock is SIM-524's):
  `scripts/sim554_rebuild_steal_pool.py --apply-migration` applied 0031 inside its one
  transaction, wrote the 394 pickoff rows (91 / 108 / 95 / 100 for 2023-2026) and passed
  every check; the export round-tripped row for row. The census (both arms, 45 × 20,
  `scripts/sim554_census_report.txt`) moved the steals' share on a foul, a ball in play or a
  hit by pitch from 38.8% to 0.85% (real 1.0%), the pickoffs per draw ×1.24 (the pool's
  ×1.28), the attempt volume within noise; the 45 × 130 lane (`scripts/sim554_lane.txt`)
  reads the same six non-passes as before the change (walks +3.4%, hit by pitch +8.5%,
  doubles +5.0%, steal attempts −10.0% / −21.3% — the steal-weight volume ticket, SIM-556 —
  and the underpowered home-win share), runs 4.47 a team-game. To rerun the rebuild on
  another database, use the one command; never apply 0031 as a separate step. The builder
  version `sim554.1` marks every pool stale for the current season, and the new code
  refuses an un-migrated table before it deletes anything (`_require_steal_pool_0031`). The
  plan (version 3, §13 the build and run record):
  `docs/audit/2026-09-29-sim554-running-game-on-the-pitch-plan.md`.
- **Every out on a pitcher's line (SIM-557, CLOSED 2026-10-07).** A caught stealing or a
  pickoff out on a pitch that does not end the plate appearance reached no pitcher's line
  (0.32 outs a game). The ledger (`_record_outs`) is now the ONE writer of a pitcher's outs.
  `GameSimResult.outs_played` counts the outs played; a unit test and the smoke's gate
  (`scripts/sim_stats.py` exits 1) hold the pitchers' outs equal to it. Each play result names
  its own pitcher and side (`PlayResult.pitcher_id`, `fielding_team`) for the win module; the
  box line counts batters faced (`bf`). The 10 × 50 smoke: credited outs 52.94 → 53.25 a game,
  equal to the outs played in all 500 (125 differed before); no play changed.
- **The pool window (2026-08-20):** the last three COMPLETED seasons plus the current one
  (`RECENCY_FLOOR_SEASONS = 4`; full 2023-2026 today). Schema v20 and `POOL_BUILDER_VERSION`
  sim515.1 then; v31 and sim554.1 since the running game on the pitch (SIM-554).
- **The slate is schedule-driven (2026-08-29):** the frontend day slate reads the MLB Stats
  API for which games exist (preview / live / final cards); the DB supplies our sims,
  projections and odds on top (→ SIM-519).

The tickets landed since the 2026-08 handovers, with their measurements, are in `CHANGES.md`.
The open tickets, ranked, are the rows of `BACKLOG.xlsx`.

**THE PLAY-PICKER REDESIGN (SIM-523) — BUILT 2026-09-08/09, COMPLETE 2026-09-09.** The
plan with its part stamps is `docs/audit/2026-09-08-sim523-play-picker-redesign-plan.md`; the
detail is `CHANGES.md`. What production runs today: every actor factor is its engine's
0-to-1 score matrix (`actor_sim/` in the bundle, `--what actors_sim` in the nightly `all`;
the concentration report gates a strict build at a 3.0 own-staff ratio) raised to a power
(`SIM_ACTOR_POWER_<NAME>`; 0 = off). **Every similarity power is 1 (owner ruling 2026-09-16)
until the comprehensive sweep of the tunable parameters fits them together.** The fitted values
(batter 1.0, fielder 1.2, steal-runner 12, advancement-runner 20, catcher-throwing 2,
pitcher-steal 12; the split's 16 / 16 / 8; the fielding draw's batter 4; manager-usage 4) are
the sweep's starting points, and the cheat sheet's summary table carries both columns. At
power 1 the sampler skips the power step, the live n=100 `/simulate` reads 25–27 s warm, and
the factors read nearly flat (the part F finding). Reliever stuff is ON at power 1. The runner
engines score thin profiles so the matrices cover the pools; the actor bell-curve kernels and
their bandwidths are
DELETED. The fielding draw filters the cell by the born ball's class (`SIM_BB_CLASS_FILTER`),
weights by the born ball's kernel (`SIM_BB_BORN_SIGMA` 1.0 — since 2026-09-22 measured on
the born ball's CLASS, a fly ball against the fly balls' spread, with the exponent per
feature `SIM_BB_BORN_PER_FEATURE`), the batter matrix
(`SIM_BB_BATTER_POWER`), the fielder matrix and the park only in the wall zone
(`SIM_PARK_WALL_ZONE_ONLY`), then runs the fence stage (`SIM_FENCE_STAGE`;
`park_geometry.json`, `--what park` — version 2 since 2026-09-21: eleven sectors over
±55°, a line per park and SEASON GROUP, a published-distance prior for thin sectors;
since 2026-09-22 the born ball's carry is read in the LIVE park's air through the
document's per-park carry offsets, `SIM_CARRY_OFFSET` — Coors +21 ft — and a wall-zone air
ball called short draws only rows within `SIM_BB_MARGIN_BAND` 15 ft of the same distance from
THEIR park's fence; certified by `scripts/sim478_fence_check.py`, A1–A7, which runs in the
run book of every geometry rebuild). The pitch draw runs one draw per pitch at pitcher
power 1 through the cell index (`SIM_PITCH_CELL_INDEX`). The pitch /
pitch-result split (`SIM_PITCH_RESULT_SPLIT`; fitted at pitcher 16 / 16, batter 8; it reds
strikeouts −2.4% on the balanced set — SIM-527) was built and OFF until **2026-09-14, when
the owner FLIPPED IT ON** on the accuracy comparison's read (the starter strikeout market
−0.0155 Brier on 250 games of 2024; the strikeout probabilities sit 6.5 points low — the
calibration layer's first job; the 2025 hold-out has not run). Built and OFF: the catcher receiving ratio
(`SIM_CATCHER_RECEIVING`, `receiving.json`, `--what receiving`; enable = SIM-526) and the
pitching change as a draw (`SIM_MANAGER_DRAW`, `--what manager`) — **FLIPPED ON
2026-09-13 with the real managers and pens (SIM-427; see the bullet below)**. The data
adds: sprint speed in both profiles (migration 0024, schema v24), the fielding chain on the
batted-ball pool (builder sim523g.1; the chain FACTOR is a follow-on), the catcher embedding
without its got-away columns, the SIM-469 rebuild. The grade: the balanced 45-game set at
45 × 130 (two lanes 2026-09-09, `scripts/sim523_lane_h.txt`) — the certified arm is green on
every pool band, runs +0.1% (with the fence stage inert; the 2026-09-21 lane with the stage
active, `scripts/sim478_lane.txt`, reads four bands red — see the §2b grade bullet).

- **The eight unpriced prop markets + the official box-score ground truth (SIM-421 + SIM-545,
  BOTH CLOSED 2026-09-12 — owner decision; the wrong-game odds match, SIM-536, closed with
  them on the 2024 re-load; the 2019–2026 odds load runs as two detached processes).** The platform prices fifteen player-prop markets (was seven): singles, doubles,
  triples, runs, stolen bases, hits+runs+RBI, pitcher outs recorded and pitcher hits allowed
  joined the original seven. The odds vocabulary has one source (`pipeline/odds_provider.py`:
  `PROP_STATS` 15 / `PITCHER_PROP_STATS` 5 / `BATTER_PROP_STATS` 10 /
  `PROP_STAT_TO_MODEL_PROP`); markets are requested by role, so the batter "hits" market
  (BettingPros 287) and the pitcher "hits allowed" market (404) never cross; Alembic 0022
  widens the `raw.prop_odds.prop_stat` CHECK constraint (its downgrade DELETES the eight new
  markets' rows). Alembic 0023 adds `raw.game_player_stats`, the official per-player box score
  (one row per player who APPEARED; no row = did not play = the book voids the bet;
  `player_id` has no foreign key on purpose), written by the historical loader from the feed
  it already holds and backfilled by `scripts/load_official_boxscores.py`. The accuracy
  comparison and `validate_props` grade every market on the box per game, with the
  event-label reader (H/HR/TB, K/BB) as the fallback, and both report how many games each
  source graded. **Ran 2026-09-12:** both migrations applied (`alembic current` = 0023); the
  backfill loaded the FULL 2024 season (2,472 games, 72,651 rows, zero failures; on every
  game the box-score runs equal the `raw.games` final score); the audit on all 2,472 games
  reads the `raw.pitches` derivation exact on every hit-type prop and RBI, and inexact on
  SB 99.08% / R 99.97% / K 99.67% / BB 99.71% / OUTS 97.93% / ER 87.57% (exact-match rates;
  the causes — K: strikeouts anchored to a trailing `no_pitch` event; OUTS: the
  displaced-runner out; ER: an inherited runner charged to the pitcher of the pitch — and
  the open hypotheses for R/SB/BB are in `docs/audit/2026-09-12-sim545-boxscore-audit.md`).
  That study is the evidence for the owner decision: grade props on the box, never the
  derivation (the old event-label path mis-graded the K prop on about one pitcher-game in
  300). **Second batch, the same day (owner ruling: "all the different markets in the
  odds tables, because we are simulating games"):** `raw.game_odds` carries EVERY game
  market the book posts — the three full-game markets plus twelve segment and team markets
  (the first-inning and first-five moneyline [three-way, with a `draw_ml` column], total and
  run line; each side's full-game and first-five team total; the first team to score; "a run
  in the first inning", stored as over / under at 0.5). Alembic 0024 (applied; head 0024)
  widens the `market_type` CHECK and adds `draw_ml`; the vocabulary is `GAME_MARKET_TYPES`
  (+ `_KIND` / `_SEGMENT` / `_SIDE`) in `pipeline/odds_provider.py`; the provider, the mock,
  the loader (`--game-markets`) and the live cycle (on the prop cadence) serve all fifteen.
  `simulation/game_market_distributions.py` prices every one of them from the per-iteration
  linescore, and the backtest grades them against `raw.games.inning_scores`. **Data runs
  started 2026-09-12 at the owner's instruction:** the full 2024 odds re-load (all fifteen
  game markets + all fifteen prop markets, ~18 s per game) and the box scores for the other
  nine seasons — the latter FINISHED the same afternoon (20,270 games, 596,480 rows, zero
  failures; all ten seasons: 22,742 games, 669,131 lines, box runs = final score on every
  game), which CLOSES SIM-545. Two facts from the loader smoke: BettingPros has no walks / outs-recorded /
  hits-allowed offers on the 2024 games checked (why the old load had zero walks rows), and
  the old load saw only page 1 of each batter market (~8 of ~15-18 hitters). **Not built:**
  the API's `/edges` and the game page still served the three full-game markets — SIM-546,
  CLOSED 2026-10-09: the inning grid now rides in the summary through the cache, which stores
  it as JSON since SIM-443f (`simulation/cache_codec.py`). Details: `CHANGES.md` (2026-09-12,
  two entries) and the build record in `docs/audit/2026-09-11-sim421-prop-market-scrape.md`.

- **Each team's real manager decides the pitching change (SIM-427, CLOSED 2026-09-13 —
  FLIPPED into production the same day; plan `docs/audit/2026-09-13-sim427-build-plan.md`,
  record `CHANGES.md` 2026-09-13, two entries).** What production runs: the pitching change
  is a draw from the change opportunity pool (`SIM_MANAGER_DRAW=1`) with the live manager's
  usage-similarity score at power 4 (`SIM_ACTOR_POWER_MANAGER_USAGE`; the `manager_usage`
  matrix, 123 manager-seasons); the pen is the MLB box's per-game listing (Alembic 0025
  `raw.game_bullpen`, backfilled 2023-2026, `SIM_BULLPEN_SOURCE=box`; `pipeline/bullpen_usage.py`
  derives the pen — minus the last-five-games rotation, an opener's start under 45 pitches
  not counting — and each arm's rest / pitched-in-two-days / pitches-in-three); the entering
  arm is a second draw over that pen (`SIM_RELIEF_ROLE_SIGMA` 0.1, `SIM_RELIEF_REST_SIGMA`
  0.5, `SIM_RELIEF_PITCHED2D_OFF_WEIGHT` 0.25, `SIM_RELIEF_PITCHES3D_SIGMA` 10; hand and
  stuff off); the steal weight is the batting manager's measured rate over the league mean
  (`derived.league_averages`, entity `manager`). DELETED: the SIM-434 pull formula and its
  helpers, the by-name tendency reader, the reliever ranking, the pinch hit, the sac-bunt
  setup, the hit-and-run, the pitch-out and the green-light. `SIM_MANAGER_DRAW=0` = no
  pitching change (the unit lane). The evidence: the usage read on the balanced 45 games
  (4.45 pitchers per team-game vs the box's 4.28; starter pitches 84.6 ± 19.2 vs 84.7 ±
  17.4; the manager terciles' pull depth 80.8 / 85.4 / 87.9 vs their own 79.3 / 84.5 /
  88.4 — the formula read 2.28 / 80.5 ± 6.0 / flat) and the paired accuracy run on 250
  games of 2024 (the starter strikeout market −0.0156 Brier for the draw, range
  [−0.0255, −0.0066]; the moneyline −0.0062; the totals flat; every batter prop within
  ±0.0011). The fatigue composition read the fatigue plan flagged is repaired by the draw
  (the pitch share by band / times through sits on the pool's). Instruments:
  `scripts/sim427_manager_probe.py`, `scripts/sim427_pen_check.py`,
  `scripts/sim427_accuracy_pair.sh` + `scripts/sim518_pair_accuracy.py`. Two traps found on
  the way: `make calibrate` DROPS the win-probability reliability curve from
  `/data/calibration.json` (write it back from `/data/prop_validation.json` through
  `write_reliability_curve_to_calibration_report`, or the app boots uncalibrated); the
  change pool must carry `game_ymd` or the SIM-535 cutoff refuses every backtest game.
- **DO NOT run the profile recompute** (`make profile-computor`) — but the reason changed on
  2026-08-13. SIM-501a/c CLOSED: SIM-457 is re-landed on the events-based out label, no profile
  site reads `outs_on_pitch` any more (a unit test enforces this), and `outs_recorded` now matches
  official innings pitched within ~0.5% (verified against the 2024 IP leaders live). The recompute
  stays blocked on two things: **SIM-458** (the run-expectancy fix, still reverted) and the
  **SIM-488 re-sweep** — the swept `raw.pitches.outs` column (pre-play outs) is still
  stale-by-one-play on 46% of plate appearances, and the situation/RE24 features group by it.
  Sequence: close SIM-502a..d → re-sweep → re-land SIM-458 → then SIM-459.
- **Alembic 0018 is APPLIED, the SIM-488 re-sweep is COMPLETE (2026-08-13, 22,533 games), and
  SIM-459 is COMPLETE AND VERIFIED (2026-08-15).** The full all-seasons recompute chain ran:
  profiles, RE24 matrix, sim pools, FAISS tiles and the engine-artifact bundle all rebuilt from
  the re-swept data. Verified: 2024 median ERA 4.07 vs MLB ~4.08 (was 6.24 on the broken
  denominator); the 2024 pool's hit-with-out rows exactly match the raw-data measurement (290+66,
  zero home runs); league pull 0.443 > oppo 0.270. ⚠ A bare `player_profile_computor` run
  defaults to THE CURRENT SEASON ONLY — a full recompute must pass `--seasons 2017 … 2026
  --full-rebuild`, then verify `sim.pool_build_metadata.builder_version`. Remaining before the
  new numbers reach users: the SIM-450 acceptance lane, regression vs the new artifacts,
  calibration refit, SIM-491 flag re-validation. All four SIM-502
  defects (a..d) closed; the THIRD adversarial review ran (four angles, ~1,100 game-loads) — one
  confirmed fix landed (mid-PA pitcher attribution on pickoff/stepoff rows) and SIM-504 was filed
  for the consumer wiring. The re-sweep (2017-2026, ~6 h, `scripts/resumable_sweep.py`, log in
  `.sweep_progress/sweep_20260813.log`; the pre-sweep progress files are archived in
  `.sweep_progress_pre_sim501_20260813/`) rewrites `raw.pitches` with the fixed parser AND fills
  `raw.play_events` — the first `intent_walk` rows this database has ever held. **SIM-458 is
  RE-LANDED** (2026-08-13, the c11c919 run-expectancy fix, verbatim). After the sweep: SIM-459
  (the recompute picks up SIM-501a/457/503 + 458 + the corrected pool `result_outs` in one pass).
- **The re-sweep takes ~6 hours, not 55.** Measured from `.sweep_progress/`: 2017-2025 ran in
  6 h 9 m. The old figure took a SPAN between file timestamps as a duration.
- **One cutoff per profile table (SIM-551, 2026-09-16).** Eight `derived.*` profile tables
  carry `asof_date`, the date a row's data runs through, and every engine refuses a table
  stamped at two dates. A partial recompute (`--seasons` a subset) on a later day used to
  leave exactly that behind — the batter table sat at 2026-09-10 / 2026-09-11 from 09-11 to
  09-16 and the app booted `build_all_engines: 10/11`. Now the computor refuses such a
  rebuild BEFORE it writes and stamps the whole table after its insert. The rule for a
  partial recompute: include the season in progress in `--seasons` (the CLI default is the
  current season). The batter table was rebuilt at one date with
  `scripts/sim551_batter_recompute.py` (11 s, the app stopped); the batter matrix came back
  byte-identical.
- **The lineup table's position is the one each starter STARTED at (SIM-559, FOUND + BUILT
  2026-10-06; MERGED and the backfill RAN 2026-10-07: 17,279 rows repaired in 10,415 games, zero
  failures; every team-game now holds eight gloves and a catcher; CLOSED 2026-10-07).** The loader wrote `raw.game_lineups.position_code`
  from the box feed's `position`; the feed's `allPositions[0]` is the starting position. 26% of
  team-games (10,802 + 1,091 of 45,484) have a fielding hole in the defense map and 301 have no
  catcher; the balanced set has 29 defective team-games in 90; the box-score table carries the
  same last-position value and keeps it (the official box shows it). The loader now stores
  `allPositions[0]` (a two-way starter who pitches and bats as the designated hitter is 'P');
  `scripts/sim559_backfill_start_positions.py` repaired the loaded games (one UPDATE per game,
  1 h 44 min at `--sleep 0.1`, Postgres only, the app served throughout; the record is
  `scripts/sim559_backfill_run.txt`, the census after `scripts/sim559_census_after.txt`). A game
  the nightly loaded between the merge and the backfill's pass keeps the old coding: `--seasons
  2026` repairs it. The 45 × 130 lane of 2026-10-07 (`scripts/sim559_lane.txt`) reads the same six
  non-passes as 2026-10-03 at like sizes; no band moved. The API answers 503 with Retry-After,
  not 404, for a known game whose lineup has no pitcher or no batting order
  (`LineupIncompleteError`, a sibling of the unpublished-lineup error; a game not in `raw.games`
  still answers 404).
  The ten-game smoke (`scripts/sim559_smoke.txt`): the fielder factor read a missing or wrong
  defender on 7.4% of balls in play, the steal and pickoff draws had no catcher on 4.9% of their
  reads, no channel moved beyond noise (every power is 1). `scripts/sim523_game_set.py` reads the
  starters from the box now (89 of 90 before). Plan and run book:
  `docs/audit/2026-10-06-sim559-starting-position-plan.md`.
- **The game page reviews a simulated game (SIM-560 + SIM-561, CLOSED 2026-10-08).** "Load
  projections" runs its 100 games on the worker pool (`BatchRunner.run_prop_set`, 33-36 s; it
  replayed them one by one in the API process, about 150 s), a click on a stat reads the cached
  seeded run (0.1 s), and the panel names each player by team. The linescore and play-by-play
  panels show ONE simulated game: the "Simulate a game" button (`POST /sample-game`, about 5 s)
  or the projections' first game. The replay tables live in a file of their own,
  `/data/replay.duckdb` (`REPLAY_DUCKDB_PATH`, `REPLAY_PERSISTENCE_ENABLED=1` in compose), which
  only the app opens writable; the analytics file stays read-only, so the rebuilds and scripts
  are not blocked. DuckDB 0032 (schema v32) adds each replayed pitch's inning, half, outs,
  batter, pitcher and score; the API records the game on a worker, so its own process never
  loads the sim bundle. The file numbers its own runs; only `/simulate` writes a Postgres
  `sim.sim_runs` row. The page reads `/card` (linescore, run id, seed), then `/plays?run_id=`;
  the file keeps 5 runs per game.
- **The Daily Diamond slate and the live, schedule-driven game day view (SIM-519, BUILT,
  MERGED and DEPLOYED 2026-10-09; the game-day live check PASSED and the ticket CLOSED 2026-10-13).** The
  owner's Claude Design ("Daily Diamond MLB tracker") is the frontend's look: the tokens keep
  their `--sim-*` names with the design's values, the fonts and the 30 team logos are bundled.
  The slate reads the league schedule (`pipeline/mlb_schedule.py`, `api/league_feed.py`;
  `source` says schedule / schedule_cached / db); each card opens in place to the real game
  (`GET /api/games/{pk}/feed`) and the book's lines with their settlement (`GET
  /api/betting/games/{pk}/card-odds`). A Preview game's posted lineup is written by the live
  service (`raw.game_lineups.source = 'published'`), so it can be simulated; the projected
  lineup is built and OFF (`LIVE_PROJECTED_LINEUPS`). The live service runs in its own compose
  service `live` and publishes on Redis; never set `LIVE_PIPELINE_ENABLED` in the app. A
  simulation run is a durable `sim.sim_runs` row (`POST /simulate`, `/simulate/runs/*`,
  `api/sim_jobs.py`). The nightly finals job runs through `scripts/with_retry.sh`; the scheduler
  starts with the stack. Alembic **0029** carries every new column. The record and the run book:
  `CHANGES.md` 2026-10-09. A game that STARTED with no stored lineup gets its starting rows from
  its feed (`PublishedLineupWriter.on_live`), and the what-if builds its base state from the feed
  when the table is empty (`fallback_rows`; 2026-10-13).
- **A rebuild runs while the app serves (SIM-524, CLOSED 2026-10-10).** A DuckDB writer
  opens the file only when no other process holds it, read-only handles included. The
  blocker was never the forkserver: `/proc/*/fd` in the app container showed ONE handle,
  read-only, in the uvicorn server process, the park-factor connection that
  `prime_park_factor_source` kept open for the life of the app. The prime now reads the run
  factors into memory and closes the file (`simulation/sim_kwargs.py`); a lookup re-reads
  them when the file's mtime changes, at most once a minute, and keeps the old snapshot
  while a rebuild holds the lock. Every other open (the engine builds, the worker loads)
  already closed after its load. The nightly profile + bundle rebuild job is ON
  (`deploy/ofelia/config.ini`, 08:00 UTC). The three gaps it left are closed by SIM-563
  (the next bullet).
- **The nightly rebuild reaches users by itself (SIM-563, 2026-10-10).** The bundle
  `/data/play_pool/engine_artifacts` is a LINK to a version under
  `engine_artifacts.versions/<build id>/` (`pipeline/batch/bundle_versions.py`). A build with
  no `--out-dir` copies the current version, builds into the copy and moves the link in one
  step; a failed build moves nothing; pruning keeps three versions plus any a running app
  marks in use. The app pins the version it booted with (`BASEBALL_ENGINE_ARTIFACT_DIR`, set
  before the pool starts), so a respawned worker reads the parent's version. The workers read
  the IBB rates and the venue run factors from the bundle (`--what reference`), never DuckDB.
  When the link moves, the app waits for its run jobs (at most 10 minutes) and for any DuckDB
  writer, then restarts itself (`api/bundle_watch.py`, `SIM_RESTART_ON_NEW_BUNDLE=1` in
  compose; SIGTERM to PID 1, `restart: unless-stopped`). A boot while a writer holds the file
  waits for it (`SIM_BOOT_LOCK_WAIT_S`, 30 minutes) instead of crashing, and a failed
  park-factor read retries every minute. An explicit `--out-dir` still writes in place.
  The rebuild's first step waits for the night's finals load (`scripts/wait_for_ingest.sh`,
  reading the `/data/ingest_marks` that `scripts/with_mark.sh` writes) and runs it when it has
  not run that day. The jobs in the jobs container run as `appuser` (the Postgres job of the
  scheduled backups runs as `postgres` in the db container). Since the scheduled backups
  (SIM-525) the rebuild runs inside `with_mark.sh` and leaves `nightly-rebuild.running` while
  it writes; the data job waits on that mark before it opens the DuckDB file.
- **The three data stores back up nightly, and a real restore was drilled (SIM-525, CLOSED
  2026-10-15).** A store is one of the three: the Postgres statistics database, the DuckDB
  analytics file and the engine-artifact bundle. A set is one backup of one store. Two Ofelia
  jobs run at 10:00 UTC, each with `no-overlap`. The Postgres job runs `db/backup/pg_backup.sh`
  in the `db` container as the `postgres` OS user over the unix socket (no password; the client
  matches the server). The data job runs `python -m pipeline.backup.data_stores` in the `jobs`
  container as `appuser`, inside `with_mark.sh` and `with_retry.sh 2`. The layout under
  `BACKUP_DIR`: `postgres/<TS>/` (a `pg_dump -Fd` dump, the `count(*)` of every table before
  and after it, `manifest.json`), `duckdb/<TS>/` (the file, its `.wal` when present, the small
  `/data` files, `manifest.json`), `bundle/<TS>/<build id>/` and `drills/<TS>.json`; TS is the
  UTC time `%Y%m%dT%H%M%SZ`. A set is written as `<TS>.partial`, `manifest.json` goes last,
  then the folder is renamed; the next run deletes every leftover `.partial`, and a set with no
  manifest never counts. The data job waits on the rebuild's, the finals' and the sweep's
  running marks, opens the DuckDB file read-only, copies it and requires equal counts on the
  copy; `replay.duckdb` is not backed up. Each store keeps 3 sets (`BACKUP_KEEP`). The
  free-space guard needs max(10 GiB, 1.5 × the newest set) free on `/backups`. When space is
  short, the guard deletes the oldest set while more than 2 remain. Then it refuses the run.
  The gauge `baseball_sim_backup_age_hours` in `/metrics` and `make backup-status` give the
  age of each store's newest set. The sets protect against five losses: a Docker disk-image
  loss or reset, `docker compose down -v` / `make nuke`, a bad migration or rebuild, a writer
  that dies in the middle of a rebuild, and an accidental deletion. They do NOT protect
  against the laptop's disk dying or the loss of the laptop. `BACKUP_DIR` is on C:, the only
  disk. The off-site copy (SIM-568, CLOSED 2026-10-17) closes that gap. At 10:30 UTC the
  Ofelia job `nightly-backup-offsite` waits for the two backup jobs. Then it copies the newest
  three complete sets of each store into the OneDrive folder
  `C:/Users/grego/OneDrive/Documents/baseball_backups` (`OFFSITE_BACKUP_DIR`), and the OneDrive
  client uploads them. The gauge `baseball_sim_offsite_backup_age_hours` gives the age of each
  store's newest off-site copy. `make backup-offsite` copies by hand, and
  `make backup-drill OFFSITE=1` restores from the copy. Its design and run book:
  `docs/audit/2026-10-17-sim568-offsite-copy.md`. The record of 2026-10-17: nine sets
  (20.8 GiB) copied at about 22 MB/s through the Docker file server in two runs (774 s +
  507 s); a rerun that copies nothing takes 2 s; the drill from the copy PASSED in 881 s (every
  table count exact; the DuckDB and bundle copies verified through `/offsite`); the OneDrive
  upload runs at about 3.3 MB/s. One trap: `pg_dump` makes its dump folder 0700, which the
  jobs container's `appuser` cannot open, so `pg_backup.sh` now opens each set before its
  manifest lands. The copy runs 10:30 to about 10:45 UTC, so recreate `jobs`, `app` or
  `scheduler` only after about 11:00 UTC, with no `.running` mark. The restore run book:
  `docs/audit/2026-10-14-sim525-backups-plan.md`. The record of 2026-10-15: the DuckDB set
  copied 3,839,122,442 bytes in 201 s with equal counts; the bundle set 588,464,368 bytes in
  46 s; the whole data run 308 s; the Postgres set 2,895,149,569 bytes (2.7 GiB) in 267 s. The
  restore drill: PASS in 450 s on 2026-10-15 (pg_restore 349 s;
  all 36 tables inside their count brackets; the DuckDB and bundle copies verified; the scratch
  cluster removed; the report `baseball_backups/drills/20261015T051919Z.json`). The record: `CHANGES.md` 2026-10-15.
- **The game page opens with the real game and runs a "what if" from any play (SIM-562,
  2026-10-09).** A live or final game shows the league feed's linescore, box score and
  play-by-play above every simulation (`GET /feed/plays`). "What if from here" on a play reads
  the game at the start of that plate appearance (`GET /feed/state?at_bat=`,
  `pipeline/mlb_game_feed.state_at_pa`: the lineups, the defense, the runners, the score, the
  pitch counts, the bench and the pen as they stood) and draws it on the slate's field. The user
  stages a pinch hitter, a reliever, a pinch runner or a defensive change
  (`simulation/whatif.apply_changes`; before first pitch, either side's lineup and starter), and
  `POST /what-if` queues TWO runs of 100 from that spot with the same seed, as it stood and with
  the changes (`sim.sim_runs.kind` = `whatif_base` / `whatif_change`, Alembic **0030**). The loop
  takes the spot as the plain-data sim-kwarg `start_state` (`simulation/start_state.py`); the
  real runs before it are spliced into each game's inning grid. Every "latest run" read takes
  only `kind = 'pregame'`. The Betting card grades each side on the real final (`result`,
  `final` on `/edges`) and shows no price on a market with no stored line. Projections and
  Betting are collapsible and start collapsed; the Line movement and Managerial override boxes
  are gone (the `/simulate/with_override` endpoint stays). The record: `CHANGES.md` 2026-10-09.
- **The stack is hardened (SIM-443a–f, BUILT 2026-10-13, DEPLOYED 2026-10-14; the run
  record is in `CHANGES.md`).** What the stack runs: Postgres, Redis, the app,
  Prometheus and Grafana listen on `127.0.0.1` only; nginx on port 80 stays on the LAN. Three
  Postgres roles (`db/roles/`, `make db-roles`): `baseball_user` the owner, used only by the
  `migrate` service; `baseball_app` (row reads and writes in `raw` and `sim`, no DDL) for the
  app, live and jobs services; `baseball_ro` (SELECT on `raw` only) for the SQL console. No
  password has a default: compose builds every DSN from required `.env` secrets
  (`scripts/gen_secrets.py`), and a script with no `BASEBALL_DB_DSN` fails
  (`pipeline/db_dsn.require_dsn`). The three nightly data jobs exec into the `jobs`
  container as `appuser`; the Ofelia INI holds no credential. Every API route and the live
  WebSocket require a login (the session cookie, `X-API-Key` or a Bearer key);
  `AUTH_DISABLED=1` is the one opt-out, warned at boot and shown on `/health`
  (`auth_enforced`). The SQL console reads `raw.*` only (a sqlglot parse of every query) and
  runs only on the read-only pool; without that pool it answers 503. Redis requires a
  password, and the sim cache stores typed JSON (`simulation/cache_codec.py`, keys `sim443:`),
  never a pickle. The tests' conftest sets `AUTH_DISABLED=1`; two auth test files remove it.
  Follow-ups: SIM-566 (P3); the bundle loads with `allow_pickle=False` (SIM-565) CLOSED 2026-10-18; the rate limiter's bucket (SIM-564) and its proxy-address follow-on (SIM-569, `TRUSTED_PROXY_HOSTS`) both CLOSED 2026-10-17; nginx re-resolves the app after a recreate (SIM-570) CLOSED 2026-10-18. The design and the cutover run book:
  `docs/audit/2026-10-13-sim443-security-hardening.md`; the record: `CHANGES.md` 2026-10-13.
- **Sample hundreds of games when validating ETL work, never dozens.** Two adversarial review rounds
  found four defects each, all from real payloads at scale, none from reading code. A 70-game sample
  reported "100%" on a metric that 950 games disproved.

## 2a. Operational caveats (Windows + Docker)

- **Every compose command needs the five passwords in `.env` (SIM-443b, 2026-10-13).** The
  compose file has no default password: `POSTGRES_PASSWORD`, `BASEBALL_APP_DB_PASSWORD`,
  `BASEBALL_RO_DB_PASSWORD`, `REDIS_PASSWORD` and `GRAFANA_ADMIN_PASSWORD` use `${VAR:?}`, so a
  missing one fails `docker compose` at once, `make test-unit` included. A worktree needs its
  own `.env`; `python scripts/gen_secrets.py --append .env` adds any secret that is missing and
  never changes one that has a value.
- **The app, live and jobs services blank three passwords over their `env_file`.** They set
  `POSTGRES_PASSWORD`, `BASEBALL_RO_DB_PASSWORD` and `GRAFANA_ADMIN_PASSWORD` to `""`, so the
  owner's password never reaches a serving container. The app logs in as `baseball_app`, which
  cannot create or alter a table.
- **`make migrate` runs the `migrate` service**, the only service that logs in as the cluster
  owner. Run another alembic command through it: `docker compose run --rm migrate alembic current`.
- **The nightly data jobs exec into the `jobs` container (SIM-443b).** Ofelia runs
  `nightly-finals`, `weekly-refresh`, `nightly-rebuild`, `nightly-backup-data` and
  `nightly-backup-offsite` with `docker exec` into `baseball_simulator_v2-jobs-1` as `appuser`
  (an exec with no `user =` line runs as root). A stopped `jobs` container fails all five
  jobs, the data job of the scheduled backups (SIM-525) and the off-site copy (SIM-568)
  included. Ofelia logs the exec error. The Sunday 08:00 pair shares the container's memory
  cap (`JOBS_MEM_LIMIT`, default 10g; the first rebuild peaked at 6.5 GiB on 2026-10-14).
  Recreate the scheduler after an INI edit.
- **The backup sets land in `BACKUP_DIR` (SIM-525, the scheduled backups).** `.env` sets
  `BACKUP_DIR=C:/Users/grego/Documents/baseball_backups`, a host folder outside Docker's disk
  image; compose mounts it at `/backups`. Without the value, compose uses `./backups`, which
  git and docker ignore. Run `make backup` from cmd.exe: Git Bash rewrites the container paths
  in its `docker compose exec` lines. The data job skips a store that has a set under an hour
  old; `--new-set` turns the skip off, and `make backup` passes it. The restore drill
  (`make backup-drill`) needs about 20 GB inside Docker's disk image while it runs, because
  its scratch Postgres cluster lives on a Docker volume.
- **The off-site copy lands in a OneDrive folder (SIM-568).** `.env` sets
  `OFFSITE_BACKUP_DIR=C:/Users/grego/OneDrive/Documents/baseball_backups`; compose mounts it at
  `/offsite`, read-write in `jobs` and read-only in the app. Without the value, compose uses
  `./backups_offsite`, which is NOT off the machine. The folder is a cloud-files folder.
  Storage Sense (the Windows disk clean-up) can turn its files into online-only placeholders,
  and a read through the Docker mount then downloads the file. The copy is off the machine
  only once the OneDrive client has uploaded it, and the job cannot see the upload. The
  OneDrive tray icon and Explorer's status column are the truth. Never point
  `OFFSITE_BACKUP_DIR` at `BACKUP_DIR`: the copy refuses to run when the two are one folder.
  Do not run `make backup` and `make backup-offsite` at the same time: the backup can prune a
  local set that the copy reads. Run both from cmd.exe.
- **Prometheus needs `deploy/monitoring/secrets/app_api_key` (SIM-443c).** The file holds one
  `API_KEYS` value from `.env` and is gitignored. Without it the `baseball-sim-app` target shows
  DOWN. See `deploy/monitoring/secrets/README.md`.
- **`scripts/` is NOT volume-mounted** into the running app container; only `api/`, `pipeline/`,
  `similarity/`, `simulation/`, `db/` are (see `docker-compose.yml` line ~110).  Edits to
  `scripts/` are picked up by the running container only after `docker compose build app` +
  `docker compose up -d app` (recreate).  Edits to the mounted dirs are picked up by
  `docker compose restart app` alone.
- **The app holds `/data/replay.duckdb` writable (SIM-561).** No other process can open it
  while the app runs, even read-only; read a stored game through the API (`/plays`,
  `/linescore`). Stop the app to inspect the file. A host-side edit to a mounted `api/`
  file DID trigger the reload on 2026-10-08; the reload closes and reopens the file cleanly.
- **`betting/` is NOT mounted either, and the app hot-reloads `api/`.** An edit to a
  mounted `api/` file that imports a NEW `betting/` name breaks the reload: the app
  fails to import until the image is rebuilt (a ten-minute outage on 2026-09-25).
  Rebuild first (`docker compose build app`, then `docker compose up -d app`), then
  edit `api/`.
- **Git Bash on Windows mangles container paths.** Any `docker compose exec` / `docker compose run`
  command from Git Bash that uses a Linux container path like `/app/scripts/foo.py` gets translated
  to `C:/Program Files/Git/app/scripts/foo.py` before Docker sees it.  Prefix with
  `MSYS_NO_PATHCONV=1` to disable the translation.  Tell: the error message includes
  `C:/Program Files/Git/`.
- **Avoid rebuilding the app image repeatedly.** Each build adds to the Docker build cache on C:,
  and the cache once filled the disk. `docker builder prune -f` reclaims the space. The
  bind-mounted directories (`api/`, `pipeline/`, `similarity/`, `simulation/`, `db/`) need no
  rebuild.
- **Never run interpreter-wide tracemalloc (`PYTHONTRACEMALLOC=1`) on the live multi-GB boot.**
  It wedged Docker Desktop on 2026-06-01. Profile with memray or py-spy in a memory-capped
  container.
- **The stochastic segfault (the SIM-445 crash class), as known on 2026-09-16.** A process in
  the app image dies now and then with `Fatal Python error: Segmentation fault` (exit 139). It
  dies at a random point in plain Python code. Known sites: the ETL sweep of 2026-07 (closed by
  the HTTP-transport swap, SIM-446), the container unit lane (a rerun passes), the batter
  actor-matrix build (2026-09-13) and the steal-runner matrix build (three of three on
  2026-09-16, inside a generator expression that `str.join` pulled item by item). That build
  passed once `_key_of` took a list comprehension. **The shape is NOT the trigger.** The same
  evening the original reproduction ran clean eight times out of eight, and every synthetic
  shape ran clean over 6.7 million evaluations (`scripts/genexpr_crash_probe.py`; the record is
  `CHANGES.md`, 2026-09-16). A sweep that rewrote the other hot-path generators was written
  and NOT landed (owner decision 2026-10-07): it fixes nothing proven. The one pattern in the
  record: the crashes cluster on rebuild days, minutes after a DuckDB write, on a file that
  later reads clean. Treat a segfault as a retry, not a failure. On the day it recurs, run the
  probe first: it tells you whether the shape matters that day. The root cause is open, and
  no ticket tracks it (the ETL tickets SIM-445/446 are closed).
- Canonical git repo: this directory. Primary shell: **Windows Command Prompt (cmd.exe)**;
  development + tests run through Docker (`docker compose run --rm app ...`).

## 3. Tech stack

Python 3.13 · FastAPI · Pydantic v2 · PostgreSQL (async SQLAlchemy + Alembic) · **DuckDB** (in-process,
no container — postgres extension) · Redis · scikit-learn (GMMs) · **FAISS** · NumPy/pandas · scipy · POT
(Wasserstein) · pybaseball · Docker / docker-compose · nginx · Prometheus + Grafana · pytest (+asyncio,
cov, timeout, benchmark, mock, hypothesis) · ruff (lint+format) · mypy. **Frontend: React 18 + Vite +
TypeScript** (chosen in SIM-378 / `docs/architecture/2026-09-02-adr-frontend-framework.md`, "Accepted"),
with Playwright e2e — talks to the API over REST + a typed WebSocket.

## 4. Architecture (layered)

```
Data sources (MLB Stats API REST+WS · Statcast/pybaseball)
  → Data layer: PostgreSQL raw.* + DuckDB derived.*/sim.* ; ETL + nightly profile pre-compute (pipeline/)
  → 11 similarity engines (similarity/engines/) : GMM-W2 pitcher, RBF batter/fielder/baserunner/
    catcher/pitcher-steal/manager, KDTree situation, FAISS pitch-to-pitch + batted-ball
  → Play pool (sim.pitch_pool / sim.outcome_pool)  [Phase 3]
  → Full-pool similarity sampler (simulation/full_pool_sampler.py over the SIM-422 engine-artifact
    bundle) : scores the WHOLE same-hand pool by the applicable engines (factorized weights:
    f_pitcher·f_batter·f_situation·recency; count-bucketed pitch draw) — the ONLY path since
    SIM-486; tests use `simulation/synthetic_bundle.py` (an in-memory bundle).  [SIM-422→429, 486]
  → Core sim loop (simulation/sim_loop.py) : 8-step pitch-by-pitch state machine + manager/situational
    decisions → GameSimResult                                     [Phase 4]
  → Runner + API (simulation/batch_runner.py, api/) : 100-iteration ProcessPool runner (forkserver
    workers — SIM-430), REST + WebSocket, Redis cache, persistence (DuckDB v32 / Alembic 0030),
    betting/CLV surface, auth/rate-limit/CORS, nginx, Prometheus/Grafana   [Phase 5 — COMPLETE]
  → Frontend (frontend/) : React 18 + Vite + TypeScript, Playwright e2e   [Phase 6 — COMPLETE]
```

## 5. Repository map

- `api/` — FastAPI app. `main.py` (create_app + lifespan), `routes/` (games, betting, metrics, similarity),
  `schemas.py` (Pydantic), `serialization.py` (numpy-safe `to_jsonable`), `auth.py`, `state.py` (engine build).
- `simulation/` — `sim_loop.py` (the simulator, biggest file; full-pool draw + engine-backed
  advancement and steal draws live here; also the reliever pick, `_pick_reliever`),
  `full_pool_sampler.py` (SIM-423 full-pool similarity sampler:
  count-bucket CDFs over the pitch-draw cell index, the actor score-matrix factors, the
  fielding draw with the fence stage, the pickoff, steal and advancement draws; reads the SIM-430 dense
  `pitcher_sim_matrix` fast path), `start_state.py` + `whatif.py` (SIM-562: a game started from a real plate appearance, and the managerial changes applied to that spot), `synthetic_bundle.py` (SIM-486: the in-memory bundle every
  no-DB test and the batch runner's no-DB factory draw from — the same loop, the same sampler),
  `game_market_distributions.py` (SIM-421, 2026-09-12: per-iteration segment runs from the
  linescore and the probability of every game market the book posts — first-inning / first-five
  moneyline, total, run line, team totals, first team to score, a run in the first inning),
  `game_state.py` (carries bat/throw hands + per-team pitcher/catcher ids + the SIM-434 per-pitcher
  rest / pitch-count fields), `results.py` (since SIM-443f `GameSimSummary.to_json` / `from_json`),
  `cache_codec.py` (SIM-443f: the sim cache's typed JSON envelope; anything that does not decode
  reads as a miss), `batch_runner.py` (ProcessPool runner — `mp_context=forkserver`
  per SIM-430), `production_factory.py` (builds the full-pool sampler from disk per worker, and
  RAISES when the bundle is missing — no fallback; `_manager_enabled()` and `_manager_draw_enabled()` read the `SIM_MANAGER` / `SIM_MANAGER_DRAW` gates), `lineup_resolver.py`
  (also resolves the per-team catcher via the SIM-363 defense map; since SIM-558 each side's starter comes from the official box, `raw.game_player_stats.p_started`, and a game with a starter missing on either side is refused), `linescore.py`, `pitcher_decisions.py`
  (W/L/S decisions), `play_recorder.py`, `prop_distributions.py` (the per-player prop
  PMFs — fifteen props since SIM-421: pitchers K/BB/ER/OUTS/H_ALLOWED, batters H/HR/RBI/TB/1B/2B/3B/R/SB/HRR),
  `prop_validation.py` (the prop ground-truth readers: the official box score, SIM-545, and the
  event-label fallback), `win_probability.py`,
  `snapshots.py`, `validation/replay_chi_squared.py`.
- `similarity/` — `engines/` (the 11 engines), `similarity_calibration.py`, `backtesting/` (backtester +
  walk-forward), `registry.py`.
- `betting/` — `clv_engine.py`, `bet_signal.py`, `line_movement.py`.
- SIM-519 (2026-10-09): `pipeline/mlb_schedule.py` (the one schedule parser and status mapper),
  `pipeline/mlb_game_feed.py` (the league feed → the slate card's detail), `api/league_feed.py`
  (the app's league reader), `api/sim_jobs.py` (the run-job registry),
  `pipeline/live/lineup_writer.py` (the published-lineup writer), `pipeline/live/broadcast.py`
  (the Redis bridge and the heartbeat), `pipeline/live/run_live.py` (the `live` service),
  `frontend/src/components/slate/` (the Daily Diamond card), `frontend/src/teams.ts`.
- `pipeline/db_dsn.py` (SIM-443b) — `require_dsn()` (the DSN or a loud error; no default
  password anywhere) and `dsn_label()` (a DSN with the password removed, for a log or a report).
- `pipeline/backup/` (SIM-525, the scheduled backups) — `data_stores.py` (the data job: the
  DuckDB file, the bundle and the small `/data` files; also `--status`, `--verify-set`,
  `--restore-duckdb` and `--restore-bundle`; its module docstring is the design),
  `status.py` (`newest_complete_sets`, read by `--status` and both backup age gauges),
  `offsite.py` (the off-site copy, SIM-568: the newest complete sets from `/backups` to
  `/offsite`; also `--status` and `--dry-run`) and `common.py` (the standard-library helpers
  that `data_stores.py` and `offsite.py` share: the chunked copy, the sha256, the mark check).
- `pipeline/` — `etl/` (historical loader + `coercion.py`, the SIM-437 shared type-coercion helpers
  imported by both ETL loaders; `boxscore_ingest.py`, the SIM-545 official box-score parser + upsert into
  `raw.game_player_stats`, which the loader calls per game), `live/live_ingestion_pipeline.py` (MLB WS + REST + odds),
  `live/bullpen_availability_ingest.py` (SIM-433 MLB-API active-roster/IL → `raw.game_bullpen_availability`),
  `batch/player_profile_computor.py` (+ the SIM-433 `_compute_bullpen_workload` from `raw.pitches`) +
  `engine_artifacts.py` (SIM-422
  builder + per-worker loader for the full-pool bundle: hand pools, pitcher×pitcher sim — incl. the
  SIM-430 dense `pitcher_sim_matrix` — batter/catcher/fielder/baserunner/manager embeddings, batted-ball
  pools), `odds_provider.py` (the provider seam AND, since SIM-421, the one source of the 15-market prop
  vocabulary), `bettingpros_odds_provider.py` (SIM-435 `closing`-line branch; SIM-421 per-(event, market)
  offers cache + pagination).
- `scripts/` — operational scripts (NOT bind-mounted; see §2a): `sim_stats.py` (v2 sim harness),
  `load_historical_odds.py` (SIM-435 opening+closing backfill → `raw.game_odds`/`raw.prop_odds`; SIM-421
  `--prop-stats` / `--line-types` / `--no-game-odds` for the eight-new-markets pass),
  `clv_backtest.py` (the SIM-538 sim-vs-closing-line accuracy comparison — the SIM-429 CLV scoreboard was
  deleted by SIM-544; `--workers` parallelizes across games — SIM-436; props graded on the official box
  score since SIM-545), `validate_props.py`, `load_official_boxscores.py` (SIM-545: backfill
  `raw.game_player_stats` from the MLB box-score endpoint), `sim545_boxscore_audit.py` (SIM-545: the
  derived-vs-official per-player totals study), `check_file_integrity.py`, `gen_secrets.py`
  (SIM-443b: writes the eight `.env` secrets; `--append` never changes a key that has a
  value, `--check` lists the missing ones), `backup_drill.py` (SIM-525: the restore drill; it
  runs on the host, restores a Postgres set into a scratch cluster and verifies the data sets).
  *(scripts/ is baked into the image; run a not-yet-rebuilt new script via
  `docker compose run --rm -v "$PWD/scripts:/app/scripts" app python scripts/<x>.py`.)*
- `db/` — `migrations/` (Alembic, head **0030** — 0030 = the what-if runs: `kind`, `start_at_bat` and `changes` on `sim.sim_runs` (SIM-562, 2026-10-09); 0029 = the SIM-519 live slate: the schedule fields on `raw.games`, `raw.game_lineups.source` / `published_at`, the run-job columns on `sim.sim_runs` and `last_seen_at` on both odds tables (2026-10-09); 0028 = the SIM-555 odds stamp column `book_line_at` on `raw.game_odds` / `raw.prop_odds`, two per-book read indexes and the two archive tables `raw.game_odds_archive` / `raw.prop_odds_archive` (2026-09-28); 0027 = the two Savant fielding landing tables `raw.savant_outs_above_average` + `raw.savant_outfield_jump` (SIM-532, 2026-09-17); 0026 = the two Savant running-game landing tables `raw.savant_basestealing` + `raw.savant_pitcher_running_game` (SIM-531, 2026-09-16); 0022 = the 15-market `raw.prop_odds` CHECK constraint,
  0023 = `raw.game_player_stats`, 0024 = the 15-market `raw.game_odds` CHECK + `draw_ml`; all three applied to the live DB on 2026-09-12; 0025 = `raw.game_bullpen`, the MLB box's per-game bullpen listing for the SIM-427 real pen, applied 2026-09-13) + `migrations/duckdb/`
  (numbered SQL, schema **v32**; 0032 = the SIM-561 replay play stream's pitch
  context — inning, half, outs before, batter, pitcher, score after; 0031 = the
  SIM-554 steal pool's `pitch_class` and `is_pickoff_row` columns) +
  `schemas/duckdb_schema_version.txt`. `roles/` (SIM-443b): `setup_roles.sh` + `setup_roles.sql`
  create the `baseball_app` and `baseball_ro` roles and their grants (a new schema needs a line
  there); run on a fresh volume and by `make db-roles`; `README.md` lists the grant set.
  `backup/` (SIM-525): `pg_backup.sh` (the Postgres job's dump) and `pg_restore.sh` (the
  restore into an empty database; it refuses a populated one); both run in the `db` container
  as the `postgres` OS user.
- `tests/` — `unit/`, `regression/` (engine invariant gate), `integration/` (E2E TestClient),
  `performance/` (pytest-benchmark). `conftest.py` has shared fixtures + the event-loop guard.
- `deploy/` — nginx + Prometheus/Grafana. `frontend/` — **React 18 + Vite + TypeScript** app
  (`src/`, `components/`, `pages/`, `graphics/`, `e2e/` Playwright, `vite.config.ts`, `openapi.json`).
- `docs/` — `HANDOFF_PHASE6.md` (the Phase 6 onboarding, kept for history), `audit/` (active investigation docs),
  `architecture/` (active design records), `technical/` (the per-subsystem code reference —
  see §13), `archive/` (superseded audit/architecture/perf/sprint/handoff docs, kept for
  history, mirrored under `audit/`, `architecture/`, `perf/`, `sprints/`; plus
  `BACKLOG-history.md`, the frozen narrative BACKLOG.md used to be — see §2b). Root:
  `BACKLOG.xlsx` (the open-ticket source of truth — see §2b), `CHANGES.md`, `agent_team.md`,
  `README.md`, `WORKFLOW.md`, `PRODUCT_GUIDE.md`.

## 6. The 9-agent team (see `agent_team.md` for full scopes)

1. **Product Manager** — requirements, backlog, phase sequencing, prioritization.
2. **Baseball Analyst** — domain validation, feature selection, run-environment realism, manager logic.
3. **ML / Modeling Engineer** — the 11 engines, GMM/RBF/FAISS math, calibration, backtesting/ablation.
4. **Data Engineer** — Postgres/DuckDB schema, ETL, live ingestion, nightly profiles, migrations.
5. **Backend Developer** — sim-loop wiring, FastAPI, WebSocket, Redis, the runner.
6. **Performance Engineer** — throughput SLA (2s/game, 30s/100-game batch), FAISS tuning, vectorization.
7. **UX Designer** — frontend wireframes, design system, components (owns Phase 6 build design).
8. **Betting / Markets Analyst** — CLV framework, odds integration, edge/+EV identification, props.
9. **QA / DevOps** — tests, CI/CD, Docker, deployment, monitoring; the independent cross-validation pass.

**Invoke a role by name** (e.g. "Baseball Analyst: review the manager pull-timing logic"). The PM
consolidates; QA cross-validates and never self-certifies its own work.

## 7. Development workflow & conventions

- **Sprint workflow:** for each sprint, role agents implement their owned tickets (partition by file
  ownership to avoid concurrent edits to the same file), then an **independent QA cross-validation pass**
  runs the full suite. Document in `CHANGES.md` (grows, per-agent detail); mark closed tickets done and
  add any newly-filed ones in `BACKLOG.xlsx` (see §2b); add
  `docs/SPRINT_<date>_<name>.md` for the sprint narrative as before.
- **Branches (owner ruling 2026-10-07).** A branch is a named line of work off `master`. A
  **local branch** exists only in the repository on this laptop. A **GitHub branch** is a copy
  that exists only after a push; the two copies sync only on a push or a fetch. A **worktree**
  (`.claude/worktrees/<name>`) is a second folder with its own branch checked out. Uncommitted
  changes in a worktree are in neither git's history nor GitHub. Every branch has one lifecycle:
  1. Create one branch off `master` for one ticket.
  2. Commit as you go. Do not end a session with uncommitted work in a worktree.
  3. Push the branch to GitHub when the work outlives the session. The push is the backup,
     and this host blue-screens often.
  4. Merge into `master` within days. A branch that must wait takes in `master` regularly.
  5. Delete the branch on the day it merges: the local copy, the GitHub copy and its worktree.
     A merged branch loses nothing when you delete it, because its commits live in `master`.

  Abandoned or superseded work: delete the branch. To keep it for reference, tag it first
  (`git tag archive/<branch> <branch>`). Two counts give a branch's state. **Ahead**
  (`git rev-list --count master..<branch>`) counts the commits `master` lacks: the real work.
  **Behind** counts the commits the branch lacks. A merged branch is 0 ahead, and its behind
  count means nothing. When every line of `git cherry master <branch>` starts with `-`, each
  commit is already on `master` by another route, so the branch is merged in content. An
  unmerged branch that falls far behind is a risk: `wave1-remediation` sat 11 weeks and then
  conflicted with `master` in 12 files. A Code session owns its worktree and its branch. Archive
  the session first; do not delete the branch of a session that is not archived.
- **TDD:** tests first, then implementation (Backend Developer convention). Unit tests use the `__new__`
  constructor-bypass + in-memory mock pattern (no live DB) — see `tests/conftest.py`.
- **Ticketing:** every change maps to a `SIM-NNN` ticket. The next free ID lives in the SUBTITLE
  ROW of `BACKLOG.xlsx` (row 2), which carries ONLY that ID, the updated date and the two doc
  pointers (owner ruling 2026-09-12 — closure and status notes go to `CHANGES.md`, never the
  subtitle) — read it there; do not trust a number copied into this file (this
  line once said SIM-438 while the true next ID was SIM-504, and SIM-534/535 were used in commits
  and code before either was ever filed). File the ticket when you use the number, not after. NOTE: the
  in-code `SIM-421` tag has TWO meanings; read the tag's own words to tell them apart. (a) A
  realism-work batch (2026-05) was tagged `SIM-421` before the full-pool epic was filed. Its
  tags talk about per-batter hands, the starters carried onto the state, the outcome-type fix
  and the replay proportions; they live in `simulation/sim_loop.py`, `game_state.py`,
  `lineup_resolver.py`, `sim_kwargs.py`, `validation/replay_chi_squared.py`,
  `pipeline/batch/engine_artifacts.py`, `pipeline/batch/player_profile_computor.py` and
  `scripts/archive/rebuild_pools.py`. (b) `SIM-421` the ticket is the prop-market work (P1,
  code complete 2026-09-12). Its tags talk about prop markets, odds, the box score and the
  per-player prop lines; they live in `simulation/prop_distributions.py`,
  `pipeline/odds_provider.py`, `pipeline/bettingpros_odds_provider.py`,
  `pipeline/live/live_ingestion_pipeline.py`, `pipeline/etl/opening_line_job.py`,
  `scripts/load_historical_odds.py`, `scripts/clv_backtest.py`, `api/routes/games.py`,
  `api/schemas.py`, migration 0022, and any `SIM-421 fix` on a box-line credit in
  `simulation/sim_loop.py`. Reconcile a realism tag if you touch it.
- **Migrations (mandatory):** every Postgres schema change ships an Alembic migration in
  `db/migrations/versions/`; every DuckDB schema change ships a numbered SQL file in
  `db/migrations/duckdb/` AND increments `db/schemas/duckdb_schema_version.txt`. *Gotcha:* a past sprint
  bumped a DuckDB migration but forgot the version file + its sanity test — always verify
  version-file == latest-migration-number after a DuckDB schema ticket.
- **Regression gate:** `tests/regression/` holds INVARIANT tests — properties an engine's scores must
  satisfy whatever the weights and data are: bounded [0, 1], symmetric, sorted, self-excluded, finite
  sub-scores, weights summing to 1.0. Run it after any engine refactor.
  **There are no golden-file snapshots, and do not add any (owner ruling 2026-09-10).** They were
  removed with the fixtures and their generator. The model is under continuous deliberate change, so
  a score moving is the normal case rather than a signal; the snapshots failed on every intentional
  tune, and "regenerate the fixture" became a ritual that teaches people to regenerate without
  reading. Behaviour is graded downstream by the acceptance lane against the play pool's own totals —
  an outcome test, not a memory of last week's numbers.
- **Secrets:** never commit credentials; the DSN is read from `BASEBALL_DB_DSN`. There is a CI
  `secrets-check` job and a `file-integrity` guard (`scripts/check_file_integrity.py`, ast.parse +
  null-byte scan).

## 8. Commands (run from the repo root; Windows cmd.exe)

The `Makefile` wraps Docker (no local Python install needed):

```
make dev               # build + start all services (db, redis, app) foreground
make down              # stop + remove containers/networks
make migrate           # apply all Alembic migrations (db must be healthy)
make test              # full suite (unit + integration)
make test-unit         # unit tests only (in the app container)
make test-regression   # engine invariant gate (no snapshots — see §7)
make test-integration  # testcontainers (Postgres + Redis)
make lint              # ruff check
make format            # ruff format
make type-check        # mypy
make profile-computor  # nightly: rebuild DuckDB profiles + sim pools
make engine-artifacts  # nightly (after profile-computor): build + publish a new bundle version (SIM-486, SIM-563)
make calibrate         # fit /data/calibration.json (arsenal W2 + per-engine sigmas) — SIM-406/432
make validate-props    # SIM-407 prop-PMF / win-prob validation (add --write-calibration for the curve)
make ingest-catch-up DAYS=14  # SIM-519: load the last N days' finals, crash-wrapped (Postgres only)
make live-logs         # SIM-519: follow the live service's log
make gen-secrets      # SIM-443b: add any missing secret to .env (never changes a set one)
make db-roles         # SIM-443b: create/refresh the app + read-only roles (ROTATE_ADMIN_PASSWORD=1 resets the owner's)
make backup           # SIM-525: back up the three stores into BACKUP_DIR now (run from cmd.exe)
make backup-offsite   # SIM-568: copy the newest sets into OFFSITE_BACKUP_DIR (the OneDrive folder) now (run from cmd.exe)
make backup-status    # SIM-525: show each store's newest set and its age; SIM-568: whether the off-site copy mirrors it
make backup-drill     # SIM-525: restore the newest set into a scratch cluster and check it (SET=<ts> picks one)
make backup-drill OFFSITE=1  # SIM-568: the same drill, from the off-site copy
make restore-postgres SET=<ts>  # SIM-525: restore one Postgres set (refuses a populated database)
```

Raw equivalents (if running Python directly, target **Python 3.13**):

```
pytest tests/unit/ -m "not slow" --cov=similarity --cov=pipeline --cov=simulation --cov=betting --cov=api
ruff check .   &&   ruff format --check .
mypy similarity/ pipeline/ api/        # CI scope; config in pyproject.toml; pin mypy>=1.8,<2
```

## 9. Testing & CI

- **CI = `.github/workflows/ci.yml`**, 12 jobs on every push/PR: lint (ruff), type-check (mypy),
  **unit-tests + 80% coverage gate**, slow-tests, regression, e2e (SIM-371 TestClient),
  acceptance-arithmetic, secrets-check, file-integrity, frontend, frontend-e2e and
  docker-build-check. The other workflows: `integration-weekly.yml` (testcontainers),
  `perf-weekly.yml` (hard-gates the `/simulate` SLA under `PERF_STRICT`), `acceptance-nightly.yml`,
  `docker-release.yml` (pushes the API image to ghcr on main) and `frontend-release.yml`.
- **CI Python is 3.13.x (SIM-431, migrated from 3.11 on 2026-05-30 so CI + Docker + local dev all
  match).** numpy is now 2.x (cp313 has no numpy-1.26 wheel; the codebase uses no numpy-2-removed APIs).
  The coverage gate is 80 (currently met at 89%). CI uses `--tb=native`
  (a pytest `--tb=short` renderer bug — `tb_lineno=None` INTERNALERROR — can otherwise mask real failures).
- **Slow tests** are `@pytest.mark.slow`. The unit lane runs `-m "not slow"` at `--timeout=30`;
  the `slow-tests` job runs `-m "slow"` at `--timeout=120`. A per-test `@pytest.mark.timeout(N)`
  overrides the CLI timeout (used for the 5000-game exhaustive test).
- **Coverage tip:** measure with `coverage run --parallel-mode` + `coverage combine` (NOT `--cov-append`
  across processes, which under-counts).

## 10. Established design decisions (do NOT relitigate without strong justification)

- GMM covariances stored in standardized space; arsenal W₂ calibrated to Statcast (~0.5–12, median ~2.84);
  linear-exponential `exp(-W₂/4.10)` (not squared). Target median similarity 0.50 across engines.
- EB_N_PRIOR=15 for the fielder engine (defensive metrics stabilize slowly); lower for pitcher/batter.
- Position-partitioned fielder engine (no cross-position scoring). Release-point sub-score excluded from
  the pitcher engine. Compositionally redundant batter features removed (ld_rate, iffb_rate, center_rate).
- DuckDB is in-process (no container). MLB WebSocket is treated as a pure change-signal; all state is
  re-fetched from REST. All `CREATE TABLE` use `IF NOT EXISTS`.
- Run-value constants: 0.75 runs/out saved (IF), 0.90 (OF), 0.25 runs/block, 0.125 runs/strike.

## 11. Defect magnitudes the acceptance bands are sized on

Open defects are rows in `BACKLOG.xlsx`. The Phase-5-close defect list, the 2026-05 live
bring-up record and the steal-volume history are in `docs/archive/CLAUDE-md-history.md`.

`tests/acceptance/bands.py` sizes two band floors on the magnitudes below, and the acceptance
tests cite them by line number. If you move either line, re-point those citations in the same
commit.

- **Runs run ~7-8% low (down from ~12% pre-fix).** This was the run-conversion gap after the
  DP-rate fix of 2026-05-28. The gap closed with the 2026-08 data rebuild. A band, floor or
  calibration target that measures this gap must red on a 7% shortfall, so a regression reds.
- **home_win_pct stuck at the structural-only ~.510-.515** is the baseline before the home
  advantage landed. The batted-ball draw hard-conditions on the batting side
  (`SIM_HOME_OFF_WEIGHT=0.0`). The home_win_pct band must reject that baseline.
- **Read R-level moves only from a multi-game batch of at least 400 sims per game.** The
  per-channel breakouts of `scripts/sim_stats.py` are the lens, not the global R mean.

## 12. Phase roadmap

| Phase | Name | Status |
|------|------|--------|
| 1 | Data Infrastructure & Pipeline | ✅ Complete |
| 2 | Similarity Engine Suite (11 engines) | ✅ Complete |
| 3 | Play Pool Architecture | ✅ Complete |
| 4 | Core Simulation Loop | ✅ Complete |
| 5 | Simulation Runner & Backend API | ✅ Complete (CI-green on Python 3.13) |
| 6 | **Frontend Build + P1 backend prerequisites** | ✅ **Complete** — SIM-378→401 + 415→420 + 414 + 402 + 408 closed; SIM-406 + 407 calibration LIVE (unblocked by SIM-432, 2026-06-01) |
| 7 | Integration, Testing & Deployment | **Largely COMPLETE; the DATA FOUNDATION was REBUILT 2026-08-13→16.** Closed earlier: SIM-402/408/431/432/430, SIM-433/434/427/411/413/425b, SIM-435, the 2026-06-03 audit remediation. **Closed in the rebuild:** SIM-501a/c (events-based out label; ERA 4.07 vs MLB 4.08), SIM-502a-d + Alembic 0018 (`raw.play_events` live), SIM-503, SIM-457/458 re-landed, SIM-488 re-sweep (22,533 games), SIM-459 all-seasons recompute, calibration refit (win-prob ECE 0.0377; BB prop now bettable), **the runs band PASSES**, CI all-green. **Open work:** the rows of `BACKLOG.xlsx`. |

## 13. Key references

- `docs/HANDOFF_PHASE6.md`, `docs/audit/2026-09-02-phase6-prioritized-tickets.md` and
  `docs/audit/2026-09-02-phase5-close-program-audit.md` — the Phase 5 close and the Phase 6
  plan, kept for history.
- `BACKLOG.xlsx` — the authoritative open-ticket list (one tab, ranked by priority, with a
  plain-English description, definition of done, and proposed solution per ticket); verify a
  ticket's status here before acting on it. `docs/archive/BACKLOG-history.md` holds the frozen
  narrative history of the closed tickets.
- `CHANGES.md` — the running changelog (**newest entries prepended at the top**; per-agent detail).
- `agent_team.md` — full agent scopes + the cross-agent collaboration map.
- `WORKFLOW.md` — the operator's manual (clean-checkout bring-up, health checks).
- `docs/technical/README.md` — the technical code reference: per-subsystem module maps (purpose,
  key functions, callers, dependencies) for api/, simulation/, similarity/, pipeline/, betting/,
  and the operational scripts — start here to find where any piece of logic lives.
- `docs/technical/sim-loop-cheat-sheet.md` — the loop on one page (2026-09-14): every draw, its
  hard filters, the weights and their production values, and the feature groups of each
  similarity score. Keep it current when a weight lands.

## 14. Writing standard (applies to ALL prose: chat replies, docs, commit messages, code comments)

Write in **Simplified Technical English (ASD-STE100 style)** and follow **Zinsser's four principles**.
This is a hard requirement, not a preference. It applies to every summary, explanation, audit document,
ticket description, docstring, and inline comment.

**Simplified Technical English — the rules to keep:**

- **One idea per sentence.** Keep sentences to 20 words or fewer. Split a long sentence into two.
- **Use the active voice.** Write "the manager pulls the pitcher", not "the pitcher is pulled".
- **Use the present tense.** Write "the gate blocks the steal", not "the gate would block the steal".
- **Use one term for one thing, every time.** Do not switch between "green-light", "green", and
  "aggression rate" for the same value. Pick one name and keep it.
- **Define a technical term the first time you use it.** Then reuse the same term.
- **No unexplained jargon (owner directive 2026-09-08).** A term like "anisotropic kernel" or
  "Markov structure" gets a plain-English gloss the first time it appears in any summary ("a
  kernel with a separate bandwidth for each feature group"). If the gloss is longer than the term
  is useful, use the gloss alone and drop the term.
- **Describe the work, never a bare ticket number (owner directive 2026-09-08).** The owner does
  not memorize what each ticket is about. Write "the catcher receiving profile (SIM-517)", never
  "SIM-517" alone; in a list of open work, lead with the deliverable in plain words and put the
  ID in parentheses after it.
- **Say who does what.** Name the actor in every sentence. Avoid "it" and "this" with no clear subject.
- **Use simple words.** Write "use", not "utilize". Write "start", not "initiate".
- **Write one instruction per step.** Do not join two actions with "and" in a procedure step.
- **Do not use noun stacks.** Write "the rate of the steal attempt", not "steal attempt rate scaling".
- **Keep the article.** Write "the pool", not "pool".

**Zinsser's four principles — the test to apply before you send:**

1. **Clarity** — the reader must not have to read a sentence twice. If a sentence is unclear, the thought
   behind it is unclear. Fix the thought first.
2. **Simplicity** — cut every word that does no work. Delete "very", "quite", "in order to", "the fact
   that", "it should be noted that".
3. **Brevity** — say it once. Do not restate a point in a summary that the body already made.
4. **Humanity** — write to a person, not to a file. Say "you", say "I", and admit uncertainty plainly.

**Do not do these things:**

- Do not open with a windup ("It's worth noting that…"). Start with the point.
- Do not hedge to sound careful. Say "I did not verify this" instead of "this may potentially differ".
- Do not pad a list to look complete. A short list of real items beats a long list with filler.
- Do not use a term of art without its gloss ("anisotropic", "RBF", "Markov", "ECE", "CLV" on
  first use).

## 15. Working conventions for Claude Code

- Confirm a ticket's status in `BACKLOG.xlsx` before acting — it changes, and it is the single
  source of truth (see §2b).
- Keep the agent-team rhythm: implement → independent QA cross-validation → run the full suite → document
  (CHANGES/BACKLOG/SPRINT).
- Run `make test-unit` + `make lint` + `make type-check` before committing; run `make test-regression`
  after any engine/model change. Target Python 3.13 to match CI (SIM-431; numpy 2.x).
- Don't commit credentials; honor the migration + regression conventions above.
- Prefer surgical edits to `simulation/sim_loop.py` (it's the largest, most-touched file).
- Follow the branch lifecycle in §7: one branch per ticket, commit before the session ends, and
  delete the branch on the day it merges.
