# SIM-427 — each team's real manager decides the pitching change: the build plan (2026-09-13)

**Owner summary.** Today the simulator changes pitchers with one hand-set formula for every
team. The fatigue probe of 2026-09-13 measured what that does: every starter is pulled at
80-81 pitches — the spread across 90 starter-games is 1.6 pitches, where real starters
average 85 with a spread of 17 — and 1.4 relievers a side then throw about 50 pitches each,
where the majors use 3.3 relievers at about 18. The sim's late innings are therefore wrong
in a way no per-pitch factor can repair: it spends a third as much time at 76+ pitches as
real baseball and twice as much on a pitcher's second time through the order.

The replacement is largely built. The play-picker redesign (part D, 2026-09-09) made the
pitching change a draw from an opportunity pool — one row per plate-appearance boundary
while a pitcher is on the mound, changed or not — and measured it at 4.35 pitchers a side
against the majors' 4.2-4.3, with no constant. It ships OFF because three things are
missing, and this ticket builds them:

1. **The manager.** The draw weights every row the same whatever dugout is deciding. This
   plan puts the real manager into it: the fielding side's manager goes onto every pool
   row, the manager engine's usage score becomes a nightly score matrix like every other
   actor's, and the live manager's row of that matrix weights the draw at a fitted power —
   the same pattern as the batter, fielder and runner factors.
2. **Real bullpens.** The pen is six made-up arms per side with negative ids; the pitcher
   factor cannot score them, so every relief inning is a league-average draw. This plan
   resolves each team's real available relievers for the game, with hands and rest.
3. **Which reliever.** The drawn row says a change happened and which real arm came in
   for that team on that day; the sim still picks its own arm by list position. This plan
   makes "which arm" a draw too: from the live pen, weighted by how much each candidate
   resembles the drawn row's arm in role, stuff, hand and recent usage.

Also in scope, from the 2026-09-04 plan the owner approved in outline: the steal-aggression
weight becomes the manager's measured ratio to a measured league mean, and the four dead
manager hooks (hit-and-run, pitch-out, sacrifice bunt, pinch hit) are deleted.

**Owner decisions, 2026-09-13 (this revision applies them):** (1) the manager's USAGE
similarity is the weight; (2) bullpen availability comes from the MLB API's per-game
listing of the arms — the box-score feed's `bullpen` and `pitchers` lists, which the
platform already fetches for every game; (3) the replacement pitcher is a draw on the
three factors (role, stuff, hand) AND on recent usage — whether the arm pitched in the
last two days and how many pitches he has thrown recently; (4) the four dead hooks are
deleted; (5) the production switch ("the flip") is explained in §5 in plain words.

The grade is the sim-vs-closing-line accuracy comparison (owner ruling 2026-09-12). Unlike
the fatigue factor, this change is large enough for that instrument to see: a starter pulled
at 85 ± 17 pitches instead of 81 ± 2 has a different strikeout distribution, and two more
relief arms a game move the run environment. Read this file with
`docs/audit/2026-09-04-sim427-plan.md` (the earlier plan; §4 there is superseded by the
redesign's part D, §3's evidence is re-read below) and the fatigue probe's composition read
(`CHANGES.md`, 2026-09-13).

---

## 1. The rules this plan works under

1. **The architecture rule, both clauses.** Every decision is a similarity-weighted draw
   from a hard-filtered pool, never a formula; the drawn row is the play; every factor is
   a draw weight or OFF until its weight lands. The pitching change is already a draw. The
   manager enters it as a WEIGHT (a score matrix at a power), never as a tuned constant.
   The reliever choice becomes a draw from the live pen; the positional pick is its OFF
   state.
2. **Every actor factor is its engine's 0-to-1 score** (owner ruling 2026-09-08), emitted
   nightly as a matrix and looked up at draw time. The manager is an actor.
3. **Two instruments, two jobs.** The pool's own conditionals set every power and
   bandwidth (the SIM-476 method, per opportunity); the accuracy comparison judges the
   model (paired, same games, same seeds, frozen bundle).
4. **Measure before building; hundreds of games, never dozens.**

---

## 2. The starting state, verified 2026-09-13

**Production today.** `SIM_MANAGER=1`: one hand-set profile for both dugouts
(`_DEFAULT_MANAGER_PROFILE`, six rates), a synthetic six-arm pen per side (negative ids),
the SIM-434 pull formula (`_maybe_pull_starter`: a leverage-scaled tendency times a
fatigue boost, a 75-pitch floor, a 110-pitch ceiling), the positional reliever pick
(`_pick_reliever`), and `SIM_MANAGER_DRAW=0`. Measured on the balanced 45 games
(2026-09-13, 1,800 game-sims): 80.8 starter pitches with a spread of 1.6 across starters,
14.2 starter outs, 2.6% of pitches at 76-100 (the pool: 7.4%), 41% on the second time
through (the pool: 24%).

**Built and OFF (the redesign's part D).** `build_pitching_change_pool` (`--what manager`,
in the nightly `all`; read-only DuckDB, so it runs while the app is up) writes
`manager_pool/change.*`: 678,014 boundaries over 9,242 games, 2023-2026; the pool's own
change rate 8.9% per boundary (28.7% at a half-inning boundary, 3.5% mid-inning; starters
4.5%, relievers 15.4%). `ChangePool` carries per row: pitch count, batters faced, inning,
outs, runners, the fielding side's margin, the pitcher on the mound, the incoming arm,
season, starter flag, half-inning flag, the change flag, recency — and, in the meta parquet
only, `game_pk`. **It does not carry the manager.** `FullPoolSampler.pitching_change_draw`
hard-filters (starter / reliever, half boundary / mid-inning, pitch-count bucket of ten,
times through the order; widened below 20 rows in a fixed order), weights by recency, a
Gaussian on the six situation columns (`SIM_CHANGE_SIT_SIGMA` 1.0), the live pitcher's
similarity row (`SIM_CHANGE_PITCHER_POWER` 1.0) and an optional per-row `manager_weight`
that nothing supplies; `last_change_row()` exposes the drawn incoming arm. The probe of
2026-09-09 (4 games × 30): the draw 4.35 pitchers a side, 82.9 starter pitches at the pull,
69% of changes at a half-inning boundary — the pool's own 4.27 / 84-85 / 69%.

**The manager data.**

| Fact | Value (live, 2026-09-13) |
|---|---|
| `derived.manager_season_metrics` | 30-38 rows per season 2017-2026; every usage column filled (the SIM-427 capstone); last written 2026-08-15 |
| League means 2023-2026 | starter pitches 84.9 / 85.2 / 84.8 / 83.8; pulled before 100: 0.845 / 0.871 / 0.885 / 0.887; steal order rate per 1B opportunity 0.059 / 0.064 / 0.062 / 0.059 |
| The spread across managers, 2025 | starter pitches p10 80.3, median 83.9, p90 88.7; pulled before 100: p10 0.78, p90 0.96 |
| `derived.league_averages` manager rows | **none** — the engine's shrinkage is a no-op for managers; the steal weight has no measured denominator |
| The live table's point-in-time column (`asof_date`, migration 0027) | **absent** — the manager profiles were not recomputed after the SIM-537 landing |
| The manager embedding in the bundle | `manager_emb.npz`: 327 manager-seasons × 27 features |
| `raw.games` manager ids, Final games 2023-2026 | 100% carry both |
| `raw.managers` rows with no `season_end` | 302 (not 30) — not a usable "current manager" lookup |
| `raw.game_bullpen_availability` | 2023-2025 complete (~2,470 games each); 2026 stops at 2026-06-02 (911 games); `is_starter` never filled |
| The official box (`raw.game_player_stats`, SIM-545) | every season, every game: `p_started`, `p_pitches`, `p_outs`, `p_batters_faced` per pitcher-game |
| The 2024 box's own numbers | **4.28 pitchers per team-game; the starter 85.2 ± 17.0 pitches, 15.6 outs** |

**The engine.** `ManagerSimilarityEngine.query(manager_id, season)` returns results with a
composite `score` and the three sub-scores; the usage sub-score (`usage_score`: starter
pitch count, pulled-before-100, closer entry leverage, high-leverage reliever share,
opener and bulk rates, available-reliever usage) is the one the pitching change should
read. `build_actor_sim_matrices` takes a spec `{"engine", "score", "keys"}` per matrix, so a
`manager_usage` matrix is one entry in `_ACTOR_SIM_SPECS`.

**The loop.** `_start_of_pa_hook` runs the manager decisions once per plate appearance,
before the intentional-walk and steal decisions. `StateMachine.manager` is one dict for
both dugouts; `_tendency(name, default)` reads it with no notion of which side decides.
`simulate_game` already accepts `bullpen`, `pitcher_rest_days` and `throw_hands`;
`SIM_KWARG_KEYS` pins the kwargs contract (a dropped key fails CI).

---

## 3. The design on one page

**Step 1 of the loop, for the fielding side, at each plate appearance's first pitch:**

1. **Change?** — the existing draw from the change pool's hard cell, weighted by recency,
   the situation kernel, the live pitcher's similarity row AND the live manager's
   usage-similarity to each row's manager, raised to a fitted power. The drawn row's
   `changed` flag is the decision.
2. **Which arm?** — when the row says "changed", a second draw from the live pen's
   available arms (the hard filter: listed as available for the game by the MLB API, not
   used this game, not a rotation starter), weighted by the candidate's similarity to the
   drawn row's incoming arm in **role** (a Gaussian on the difference in high-leverage
   entry share, from the pool itself), in **stuff** (the pitcher engine's score, at a
   power), in **recent usage** (Gaussians on the difference in days since his last
   outing, on whether he pitched in the last two days, and on his pitches over the last
   three days — the drawn row's arm carries the same three facts as of that game), with
   a hand mismatch as a soft weight. The drawn arm enters with his real id, hand and rest.
   Recent usage enters as a similarity, not a penalty: how often a real manager brings in
   an arm on no rest with forty pitches in three days is already in the pool's rows, and
   the draw reproduces it without a rule.
3. The intentional-walk and steal decisions follow as today; the steal draw's manager
   weight becomes the manager's measured steal-order rate over the season's measured
   league mean (a ratio near 1; no leverage term — the SIM-476 form).

**What goes:** the pull formula and its constants, the reliever scoring formula, the
synthetic pen, the single hand-set profile, and the four dead hooks.

---

## 4. The parts (each closable on its own)

### 4a. Data — the manager onto the pool, the manager matrix, the league row (2 days + ~1.5 h of runs)

1. **The fielding manager on every change-pool row.** `build_pitching_change_pool` joins
   `raw.games` (`home_manager_id` / `away_manager_id`) by `game_pk` and the half: the top
   of the inning is the home side fielding. The builder's DuckDB connection is read-only;
   read the ~10k-row game map through a read-only Postgres attach (the computor's own
   pattern) or one `asyncpg` fetch registered as a DuckDB view. New meta column
   `manager_id`; `ChangePool.manager_id` (optional, shareable; None on an older bundle).
   Verify on the whole pool: every row's manager matches the row's game and half in
   `raw.games`; a spot check of 300 games by hand-rolled SQL.
2. **The manager-usage score matrix.** `_ACTOR_SIM_SPECS["manager_usage"] = {"engine":
   "manager", "score": "usage_score", "keys": ("manager_id", "season")}` and the engine in
   `_ACTOR_SIM_ENGINES`. Size: 327 × 327. **The concentration check does not apply to
   this matrix by design** — the owner's definition of done asks for each team's OWN
   tendencies, so the live manager's own rows carrying more weight is the point, not a
   defect. The guard that replaces it is the effective-sample share of the cell under
   the fitted power (4f), which stops the draw collapsing onto a manager's twenty own rows.
3. **The manager row in `derived.league_averages`** (`LeagueAverageProfiles.compute`): the
   game-weighted mean of the seventeen tendency columns per season. It turns on the
   engine's shrinkage for managers and supplies the steal weight's denominator.
4. **Recompute the manager profiles** (all seasons, the app stopped — a DuckDB write, the
   SIM-524 lock). This also lands migration 0027's `asof_date` column, which the live
   table lacks. Then `make calibrate` (the three manager bandwidths re-fit on shrunk
   profiles, the 2026-06-03 capstone's precedent), then `--what actors_sim` and
   `--what manager` (both read-only). Freeze the bundle afterwards for 4f.
5. **The reliever role share and the incoming arm's rest, from the pool and the box.**
   At artifact time, for every incoming arm in the change pool: entries, the share of
   entries in a high-leverage spot (inning ≥ 7 and margin within two — the manager
   computor's own leverage bucket), the mean pitches per outing — the role share, written
   to `manager_pool/roles.parquet` (per pitcher-season). And per change-pool ROW, the
   incoming arm's rest as of that game from the box-derived usage table (4b-3): days since
   his last outing, whether he pitched on either of the two preceding days, his pitches
   over the preceding three days — three new `ChangePool` columns (`in_days_rest`,
   `in_pitched_2d`, `in_pitches_3d`; −1 = unknown, neutral). These are what 4d's recent-
   usage weights compare the live candidates against.

Gate: the pool carries a manager on 100% of rows; the matrix covers every manager-season
in the pool window; `league_averages` holds a manager row per season; the box-derived
reads in §2 re-read unchanged (nothing here touches the sim).

> **Landed 2026-09-13.** The change pool carries the fielding manager on 100% of its
> 693,774 rows and the incoming arm's rest / pitched-in-two-days / pitches-in-three on
> 100% (`pipeline/bullpen_usage.fetch_game_context_sync`; the pool was rebuilt a second
> time the same day to add `in_throws`, the incoming arm's hand: 27% left); the reliever
> ROLE sidecar holds 2,780 pitcher-seasons. The `manager_usage` matrix is 123 × 123 (the
> manager-seasons with a profile in the window; built alone with `--matrix manager_usage`
> after the full `actors_sim` build hit the SIM-445 segfault class). The manager league
> row exists for all ten seasons (`scripts/sim427_manager_recompute.py`: migration 0027
> applied, profiles 14 s); `make calibrate` re-ran. Not done: the 300-game hand-rolled
> spot check of the manager join (the coverage read stands in).

### 4b. Real identities — the manager per side and the real pen (2.5 days)

Files: `simulation/lineup_resolver.py`, `simulation/sim_kwargs.py`,
`simulation/production_factory.py`, `simulation/sim_loop.py`, `simulation/game_state.py`,
`api/schemas.py`, `api/routes/games.py`.

1. **The manager per side.** `fetch_game_sides` returns `home_manager_id` /
   `away_manager_id` from `raw.games` (100% on Final games). A preview game with NULL ids
   takes the team's most recent Final game's manager (`raw.managers`' `season_end` is not a
   usable lookup — 302 "current" rows). The ids travel in `sim_kwargs`
   (`home_manager_id`, `away_manager_id`; `SIM_KWARG_KEYS` grows by two). The sampler keys
   the matrix row as `f"{manager_id}:{season}"`, with the season-before fallback the other
   actors use, then neutral.
2. **Two managers per machine.** `StateMachine.manager` becomes a per-side map;
   `_tendency(name, default, side=)` reads the deciding dugout (the fielding side for the
   change, the batting side for the steal weight). `manager is None` stays the
   nothing-wired gate for every no-DB test.
3. **The real pen — the MLB API's per-game listing (owner decision 2).** The box-score
   feed (`/api/v1/game/{game_pk}/boxscore`, the payload `pipeline/etl/boxscore_ingest.py`
   already fetches for every game) carries per side `bullpen` — the pitchers on the roster
   who did NOT pitch that day — and `pitchers` — those who did, the starter first. The arms
   available to the manager at first pitch are `bullpen` plus the relievers in `pitchers`;
   the injured list is already excluded (an IL arm is not on the box). A new table,
   `raw.game_bullpen` (Alembic migration: `game_pk`, `team_id`, `pitcher_id`, `listed`
   ∈ {`bullpen`, `pitched`, `started`}), written by the box-score ingest alongside
   `raw.game_player_stats` — the historical loader for new games, and a backfill pass over
   the pool window's games (2023-2026, ~9,900 games, about an hour and a half at the
   loader's pace; the other seasons only if a later use needs them). Two derived facts per
   arm come from the box rows already loaded, by SQL over `raw.game_player_stats` (no
   pitch-level pass): the ROLE — an arm that started any of the team's last five games is
   a rotation starter and leaves the pen — and the RECENT USAGE — days since his last
   outing, whether he pitched on either of the two preceding days, and his pitches over
   the preceding three days. Hands from `raw.players`. The resolver writes `bullpen`
   (`{side: [pitcher_id, ...]}`), `pitcher_rest_days`, `pitcher_recent_usage`
   (`{pitcher_id: (pitched_2d, pitches_3d)}`), the hands merged into `throw_hands`, and
   `bullpen_source` (`box` / `synthetic`) into `sim_kwargs`. For a PREVIEW game the
   pregame box carries the lists once the lineups post; before that, the team's most
   recent game's pen with a staleness flag. The SIM-433 roster/IL ingest stays for the
   manager profile's available-reliever-usage feature and is not the sim's pen source.
   The synthetic pen stays ONLY as the no-DB seam (the synthetic bundle's games); a
   production game with no box pen is a logged warning and the synthetic pen, never a
   silent success. Verify the derived pens on hundreds of games against the SIM-433
   table's active arms on the 2023-2025 overlap (expect 90%+ agreement, the difference
   being the resting starters the role rule removes).
4. **The interim pick.** Until 4d lands, `_pick_reliever` must not slide into the dormant
   scored formula because hands now exist (`has_meta`); replace the sniff with an explicit
   mode flag: the rank-ordered positional pick.
5. **API.** `LiveGameStateResponse.home_bullpen` / `away_bullpen` from the resolver, plus
   `bullpen_source`.

Gate: a 45 × 20 probe run reads pitchers per team-game and starter outs unchanged within
noise (this part changes identities only); every reliever id in the box score is positive;
per-reliever prop rows carry real ids.

> **Landed 2026-09-13, with three departures.** (1) The manager ids ride `sim_kwargs`
> (`home_manager_id` / `away_manager_id`) and the sampler keys `manager_id:season`; a
> preview game with NULL ids is neutral (the most-recent-Final-game fallback is NOT built).
> (2) Departure from 4b-2: there is no per-side manager map on the machine. Each side's
> REAL profile and the league means ride the GAME STATE (`home_manager_profile`,
> `away_manager_profile`, `manager_league_profile`, filled by
> `sim_kwargs.resolve_manager_profiles_onto_state` from DuckDB; `sim_kwargs` gained
> `manager_profiles` / `manager_league_profile`), and `StateMachine.manager` is only the
> gate. (3) The real pen: migration 0025 `raw.game_bullpen` (the listing: started /
> pitched / bullpen), written by the historical loader and backfilled for 2023-2026
> (`scripts/load_official_boxscores.py --bullpen-only`: 9,454 games, zero failures);
> `pipeline/bullpen_usage.py` derives the pen (minus the last-five-games rotation, minus
> today's starter) and each arm's rest / pitched-2d / pitches-3d; the resolver's
> `_attach_bullpens` fills `bullpen`, `pitcher_rest_days`, `pitcher_recent_usage`, the
> hands and `bullpen_source` — BEHIND `SIM_BULLPEN_SOURCE` (`synthetic` today, `box` at the
> flip), a switch this plan did not name but §5 requires (the OFF arm must be production
> today). The preview-game staleness fallback is NOT built (no listing = the synthetic pen
> with a warning). The pen check: on 400 games of 2023-2026 (`scripts/sim427_pen_check.py`): every Final game has a listing; the pen averages 8.4 arms (the majors' 8); 99.1% of the relievers who actually pitched are in the pen (96.9% of sides complete) once an opener's start under 45 pitches stopped counting as a rotation start (`ROTATION_START_MIN_PITCHES`; 97.2% / 90.8% before); against the SIM-433 table the raw agreement reads 71% / 70%, and the whole gap is by design — the pen keeps 1,252 arms SIM-433 marks 'rest' (the reliever draw's rest weight handles them) and SIM-433 keeps 990 rotation starters and 597 of today's starters the pen removes; only 21 pen arms are unknown to SIM-433 and 48 SIM-433 arms are off the box.
> (4) 4b-4: the pick is by `source`: a draw-sourced change asks `relief_arm_draw`; the
> formula's pick keeps the SIM-434 ranking only when an arm carries meta (the synthetic
> pen carries none, so production today is byte-identical). (5) 4b-5 (the API's bullpen
> fields) is NOT built. The 45 × 20 identity-only probe run was not run on its own; the
> 4f probe's OFF arm is that read.

### 4c. The manager weight on the change draw (1 day)

`FullPoolSampler._change_meta` maps each pool row's `manager_id:season` to the
`manager_usage` matrix column (−1 = unscored); `pitching_change_draw` gathers the live
manager's matrix row over the cell's rows, applies the draw-neutral rule for unscored rows
(the part-F pattern: an unscored row draws at the mean scored weight), raises to
`actor_power["manager_usage"]` (`SIM_ACTOR_POWER_MANAGER_USAGE`; 0 = off, the byte-identical
state), normalizes to a mean of 1 inside the cell, and multiplies it in as the
`manager_weight`. The loop passes the fielding side's manager key from the per-side map.
Tests: power 0 is byte-identical to today's draw; a quick-hook manager's row draws more
changes than a long-leash manager's in the same cell at power > 0; an unscored live manager
is neutral; the widening counters still report.

> **Landed 2026-09-13** as written (`FullPoolSampler._change_manager_factor`, the
> `mgr_col` map in `_change_meta`, `manager_key` on `pitching_change_draw`; the loop passes
> the fielding side's manager). One difference: the factor is NOT re-normalized to a mean of
> 1 inside the cell (the other actor factors are not either; a constant scale cancels in
> the draw). The tests are `tests/unit/test_sim427_relief_draw.py::TestManagerFactor`.

### 4d. Which arm — the incoming-arm draw (2.5 days)

`FullPoolSampler.relief_arm_draw(candidates, drawn_row, *, hands, usage)`: the hard filter
is the caller's candidate list (listed available for the game, unused this game, not a
rotation starter). The weights, every one a similarity between the live candidate and the
arm the drawn pool row brought in, all normalized to a mean of 1 over the candidates, all
OFF = the positional pick byte for byte:

* **role** — a Gaussian on |the candidate's high-leverage entry share − the drawn arm's|
  (`SIM_RELIEF_ROLE_SIGMA`; from 4a-5; a candidate with no role row is neutral);
* **stuff** — the pitcher engine's score between the drawn arm and the candidate, from
  the pitcher-sim matrix (`SIM_RELIEF_PITCHER_POWER`; 0 = off);
* **recent usage** (owner decision 3) — a Gaussian on |days since last outing − the drawn
  arm's| (`SIM_RELIEF_REST_SIGMA`), a mismatch weight on "pitched in the last two days"
  (`SIM_RELIEF_PITCHED2D_OFF_WEIGHT`; 1.0 = off), and a Gaussian on |pitches over the last
  three days − the drawn arm's| (`SIM_RELIEF_PITCHES3D_SIGMA`); the drawn arm's three facts
  come from the pool row (4a-5), the candidate's from the resolver (4b-3); an unknown on
  either side is neutral;
* **hand** — a mismatch × `SIM_RELIEF_HAND_OFF_WEIGHT` (1.0 = off).

`_change_pitcher` calls it when the change came from the draw; the formula path is deleted
with 4e. The drawn arm's rest, usage and hand travel with him so the pitcher factor scores
him and the platoon logic sees him; his usage counters update as he pitches (a second
appearance in the same game is impossible by the hard filter).

Tests: all-off equals the positional pick; a role-only weight prefers the closer-like
candidate when the drawn arm was a closer; a rest-only weight prefers a rested candidate
when the drawn arm was rested and a tired one when the drawn arm pitched yesterday (the
pool's behaviour, not a rule); a hand weight below 1 prefers the matching hand; an
exhausted pen returns None and the change is skipped (as today).

> **Landed 2026-09-13** as written (`FullPoolSampler.relief_arm_draw`; the six weights
> read by `production_factory.apply_manager_env`; `_pick_reliever(source="draw")`). The
> drawn arm's own facts travel through the pool row; the live candidate's through the
> resolver. The formula's pick is NOT deleted yet — see 4e and §5. Tests:
> `tests/unit/test_sim427_relief_draw.py::TestRelieverDraw` / `TestTheLoopPopsTheDrawnArm`.

### 4e. The steal weight and the dead-hook deletion (1 day)

1. `_steal_opportunity_draw`: the manager weight = the batting manager's
   `steal_order_rate_per_1b_opp` over the season's measured league mean (from 4a-3,
   carried in `sim_kwargs`), clamped as today; the hard-coded 0.08 goes. Guard: the three
   steal reads of the fatigue probe (attempts per opportunity at second and third, the
   safe share) unchanged within noise on the 45 games.
2. Delete `_maybe_hit_and_run`, the pitch-out block, `_maybe_sac_bunt`, `_maybe_pinch_hit`,
   `StateMachine.bench`, the `ManagerContext` signal fields, `pitcher_fatigue`,
   `tto_effectiveness`, `platoon_factor`, `score_reliever`, `_PULL_PITCH_FLOOR` /
   `_PULL_PITCH_CEILING`, `_DEFAULT_MANAGER_PROFILE` and `_default_bullpen_for_spec` (the
   last two once 4b's fallback is in), with their tests (`test_sim434_manager_model.py`,
   the hit-and-run cases in `test_baseball_analyst_sim349.py`, the pinch-hit / sac-bunt /
   pitch-out cases in `test_baseball_analyst_sim323.py`). Keep the engine's tendency
   vocabulary (`_MANAGER_TENDENCY_INDEX`) — the engine does not change.
3. `_should_issue_ibb` keeps the SIM-515 cell draw; the per-manager IBB weight stays
   deferred (it needs a profile column and a recompute — its own row on close).

> **Landed 2026-09-13, one deliberate hold-back.** The steal weight is
> `StateMachine._steal_aggression` (the batting side's measured rate over the league mean,
> clamped [0.05, 4.0]; the 0.08 default is gone). DELETED: `_maybe_hit_and_run`, the
> pitch-out block and the green-light, `_maybe_sac_bunt`, `_maybe_pinch_hit`,
> `StateMachine.bench`, the three `ManagerContext` signal fields, and their tests (the
> SIM-323 / SIM-349 cases; `test_sim434_manager_model.py` rewritten). HELD BACK until the
> flip: the pull formula (`_PULL_PITCH_FLOOR` / `_CEILING`, `pitcher_fatigue`,
> `tto_effectiveness`, `platoon_factor`, `score_reliever`, the `_tendency` reader and
> `_MANAGER_TENDENCY_INDEX`, the one rate in `_DEFAULT_MANAGER_PROFILE`,
> `_default_bullpen_for_spec`). Reason: §5 promises the owner that production behaves
> exactly as today until the flip, and the paired accuracy run's OFF arm IS production
> today; with the formula gone the OFF arm would be "no pitching change at all", a state
> nobody ran. The flip commit deletes them with `test_sim434_manager_model.py`'s sections
> 2 and 3.

### 4f. Fit, measure, grade (2 days + one probe + the paired accuracy run)

**The instrument** — `scripts/sim427_manager_probe.py` (the fatigue probe's arm-per-process
shape, its composition read reused): per arm, on the balanced 45 games × 40 iterations:

* pitchers per team-game; starter pitches at the pull (mean AND spread across
  starter-games); starter outs; the change share at half-inning boundaries; against the
  box score's own 2023-2026 numbers (`raw.game_player_stats`) and the pool's;
* **the manager read** — the change rate per boundary by manager tier (the live manager's
  own starter pitch count: quick hook / league / long leash, terciles), against the pool's
  own change rate per boundary for those managers' rows — the per-opportunity conditional
  the power is fitted on;
* the reliever read — the entering arm's role share by leverage tier, and the hand-match
  share, against the pool's own;
* the fatigue composition read (pitch-count bands, times through the order) — the read
  that motivated this ticket, so the plan's success is visible in the same table;
* per starter-game, the strikeout mean and the probability of clearing the closing line,
  arm minus arm — the size the accuracy run must resolve.

**The fits, in order, each the SIM-476 way (the largest bandwidth / smallest power whose
per-tier reads sit inside the floors, seed-split confirmed):** the manager power on the
change draw (ladder 0 / 1 / 2 / 4 / 8; the effective-sample share of the cell is the
starvation guard); then the reliever weights (role σ over {0.05, 0.10, 0.20}, pitcher
power {0, 1, 2}, hand weight {1.0, 0.5, 0.25}, rest σ over {0.5, 1, 2} days, the
pitched-in-the-last-two-days mismatch weight {1.0, 0.5, 0.25}, pitches-in-three-days σ
over {10, 20, 40}), read against the pool's own entering-arm mix by leverage tier AND its
own rest mix at entry (the share of entries on zero and one day's rest, the mean pitches in
the prior three days); the steal weight has no bandwidth.

**The grade.** The paired accuracy comparison (`scripts/sim518_pair_accuracy.py` over two
`clv_backtest.py` reports): the OFF arm — production today — and the ON arm — the manager
draw with the fitted weights, real pens, the arm draw. Same 2024 games, same seeds, one
frozen bundle, after the owner's odds re-load. Read first: the starter strikeout and outs
markets, the game totals, the first-five-inning totals; then everything else as the
"not worse" check. This change is first-order on those markets — the fatigue probe's
staging table puts an expected probability shift of several points in the row the
full-season run at 100 iterations resolves — so the run is worth both arms. The OFF arm
doubles as the platform's baseline.

**The flip.** `SIM_MANAGER_DRAW=1` plus the fitted powers and bandwidths in the compose
env and the lane's `PRODUCTION_FLAGS`; the unit lane pinned off; `CHANGES.md`, the
backlog row, CLAUDE.md §2b. Then **re-run the fatigue probe** (35 minutes) on the new
composition and take the fatigue decision the owner held on 2026-09-13.

> **Built 2026-09-13:** the instrument is `scripts/sim427_manager_probe.py` (`run --arm
> off|key=value,...`, `report`; the five reads above plus a sixth — the starter's pitches
> at the pull by manager tier against those managers' own measured starter pitch count,
> the read the usage weight actually moves; the box reference read live from
> `raw.game_player_stats` for the set's seasons: 4.28 pitchers per team-game, starter
> pitches 84.7 ± 17.4, 15.5 outs over 13,972 starts of 2024-2026). The flip also sets
> `SIM_BULLPEN_SOURCE=box`.
>
> **The manager power — MEASURED 2026-09-13 (45 games × 40 iterations per arm;
> `scripts/sim427_probe_{off,d0,d1,d2,d4,d8}.json`).** The OFF arm reproduces production:
> 2.28 pitchers per team-game, starter pitches 80.5 ± 6.0 (a spread of 1.6 across
> starter-games), 14.2 outs, and a flat 80.2 / 80.4 / 80.6 pitches at the pull for the
> quick-hook / league / long-leash manager terciles whose own numbers are 79.3 / 84.5 /
> 88.4. The draw at the pool's own rates (power 0): 4.55 pitchers, 83.2 ± 22.2, 14.7 outs,
> the composition read on the pool's row share (58.3 / 19.3 / 15.1 / 7.1 / 0.3 by
> pitch-count band against 57.2 / 19.5 / 15.7 / 7.4 / 0.1; 65.1 / 23.7 / 10.9 / 0.3 by
> times through against 63.9 / 24.0 / 11.8 / 0.3 — the fatigue read that motivated this
> ticket is repaired by the draw alone), but the tiers still flat (82.6 / 83.4 / 82.8).
> The ladder, by tier (the spread the weight must produce is 9.1 pitches):
>
> | power | effective-sample share of the cell | quick hook | league | long leash | all starters | pitchers / team-game | starter outs |
> |---|---|---|---|---|---|---|---|
> | 0 | 1.00 | 82.6 ± 22.8 | 83.4 ± 21.4 | 82.8 ± 21.8 | 83.2 ± 22.2 | 4.55 | 14.7 |
> | 1 | 0.44 | 83.3 ± 20.4 | 85.0 ± 18.6 | 86.5 ± 17.3 | 85.0 ± 19.1 | 4.51 | 15.0 |
> | 2 | (not recorded) | 83.5 ± 20.1 | 85.1 ± 18.1 | 85.8 ± 17.9 | 84.8 ± 19.0 | 4.47 | 15.0 |
> | **4** | **0.20** | **81.1 ± 22.8** | **85.4 ± 17.2** | **88.3 ± 15.2** | **84.8 ± 19.2** | **4.49** | **15.0** |
> | 8 | 0.08 | 79.7 ± 24.9 | 85.1 ± 17.6 | 87.0 ± 16.8 | 83.8 ± 20.7 | 4.51 | 14.8 |
> | the managers' own / the box | | 79.3 | 84.5 | 88.4 | 84.7 ± 17.4 | 4.28 | 15.5 |
>
> **The fit is power 4:** the smallest power whose three tier means sit on the managers'
> own (within 1.8 / 0.9 / 0.1 pitches; the per-tier standard error is about 0.6), with a
> fifth of the cell's rows still effective (about 540 rows in an average cell). Power 8
> concentrates the draw on 8% of the cell and loses the long-leash tier. The change rate
> per boundary does not separate the tiers at any power (4.4-4.6% everywhere; the pool's own
> 4.7 / 4.5 / 4.4) — the manager's tendency lives in WHEN he changes, not how often, which
> is why the pull-depth read is the fit read. Against the box the draw at power 4 sits on
> the starter's pitches (84.8 vs 84.7) and their spread (19.2 vs 17.4), and reads 5% more
> pitchers per team-game (4.49 vs 4.28) and 3% fewer starter outs (15.0 vs 15.5): the
> sim's starters spend the same pitches for fewer outs, a pool property, not the
> manager's. The prediction shift against the OFF arm is large enough for the accuracy
> run to see: the starter's strikeout mean +0.31 per start, the probability of clearing
> the closing line +5.8 points (mean absolute shift 11.3 points) over 89 starter-games.
>
> **The reliever read on the first batch found and fixed a defect:** a draw-sourced change
> with every reliever weight off fell into the SIM-434 platoon ranking (the real pen
> carries hands), so 93% of entering arms matched the batter's hand; the pick is
> positional now (`_pick_reliever`; the test pins it). The first batch's reliever reads
> therefore describe that ranking, not the positional pick; the reliever ladders below
> re-read them. The pool's own entering arms: high-leverage role share 0.31 / 0.42 / 0.48
> by inning tier (≤6 / 7-8 / 9+), 16.5% pitched yesterday, 43.8% pitched in the last two
> days, 12.4 pitches over three days, 29.5% left-handed.
>
> **The reliever weights — MEASURED 2026-09-13 (45 games × 20 iterations per arm, one
> weight at a time over the power-2 draw; `scripts/sim427_ladder.sh` ran the arms,
> `scripts/sim427_probe_r_*.json`).** The positional pick (every weight off) enters an
> arm who pitched yesterday 42.2% of the time (the pool 16.5%): the pen lists the arms the
> box saw pitch first and the low-leverage pick takes from the BACK — the arms who sat
> today, who are disproportionately yesterday's arms. Any single weight replaces that pick
> with a draw over the whole pen and reads ~36% on its own. The ladder (the pool's own in
> the last column):
>
> | weight | arms | high-leverage role by inning ≤6 / 7-8 / 9+ | pitched yesterday | rest 2 | pitched in 2 days | pitches in 3 days | left |
> |---|---|---|---|---|---|---|---|
> | every weight off | 1 | 0.38 / 0.38 / 0.39 | 42.2% | 23.7% | 66.0% | 20.8 | 29.0% |
> | role σ 0.05 / 0.1 / 0.2 | 3 | 0.38 / 0.42 / 0.42 · 0.38 / 0.42 / 0.41 · 0.38 / 0.41 / 0.40 | 37-39% | 23-24% | 61-62% | 18.7-18.9 | 30-31% |
> | rest σ 0.5 / 1 / 2 (days) | 3 | 0.37 / 0.37 / 0.38 (all) | **25.1%** / 28.1% / 33.5% | **27.3%** / 25.3% / 24.2% | 52.5% / 53.5% / 57.8% | 17.9 / 18.1 / 18.9 | 30-31% |
> | pitched-in-2-days mismatch 0.5 / 0.25 | 2 | 0.38 / 0.38 / 0.39 | 35.4% / 34.6% | 22.8% / 22.1% | 58.1% / 56.7% | 18.6 / 18.2 | 30-31% |
> | pitches-in-3-days σ 10 / 20 / 40 | 3 | 0.40 / 0.39 / 0.38 · 0.39 / 0.39 / 0.38 · 0.38 / 0.39 / 0.39 | 33.8% / 35.7% / 36.5% | 22.5% / 22.1% / 22.7% | 56.3% / 57.9% / 59.3% | **15.2** / 16.3 / 17.7 | 30-31% |
> | hand mismatch 0.5 / 0.25 | 2 | 0.38 / 0.39 / 0.38 | 36.5% / 36.3% | 23.6% / 23.1% | 60.0% / 59.4% | 18.9 / 18.7 | 28.0% / 26.8% |
> | stuff power 1 / 2 | 2 | 0.38 / 0.38 / 0.39 | 36.4% / 36.2% | 23.6% / 23.7% | 60.0% / 59.9% | 19.0 / 19.1 | 28.5% / 28.3% |
> | **the pool's own entering arms** | | **0.31 / 0.42 / 0.48** | **16.5%** | **27.3%** | **43.8%** | **12.4** | **29.5%** |
>
> The picks, by the SIM-476 rule (the largest bandwidth whose read sits nearest the
> pool's; a weight whose read the pool does not require stays OFF): **role σ 0.1** (0.05
> and 0.1 tie on the 7-8 and 9+ tiers; the early-inning tier never falls to 0.31 and the
> 9+ tier never reaches 0.48 — the pen's arms cluster near 0.4 and a candidate without a
> role row is neutral), **rest σ 0.5** (the ladder's floor; the rest-2 share lands exactly),
> **pitches-in-3-days σ 10**, the **pitched-in-2-days mismatch 0.25** (weak on its own — the
> drawn arms split 44 / 56 on the fact, so a mismatch weight pulls both ways — kept as a
> small extra), **hand OFF** (the positional pick already reads the pool's 29.5% left),
> **stuff OFF** (no read moves; the probe has no pool reference for it).
>
> **The combined ON arm — MEASURED 2026-09-13 (45 × 40; `scripts/sim427_probe_on.json`;
> `draw=1,pen=box,mgr=4,role=0.1,rest=0.5,p2d=0.25,p3d=10`).** Against the OFF arm and the
> box: pitchers per team-game **4.45** (OFF 2.28; box 4.28), starter pitches **84.6 ±
> 19.2** (OFF 80.5 ± 6.0; box 84.7 ± 17.4), starter outs **15.0** (OFF 14.2; box 15.5),
> the half-boundary share of changes 68.9% (the pool 69.2%), the pull depth by manager
> tier **80.8 / 85.4 / 87.9** against the managers' own 79.3 / 84.5 / 88.4 (OFF 80.2 /
> 80.4 / 80.6), the effective-sample share 0.196, the composition read on the pool's row
> share; the entering arm: role 0.37 / 0.40 / 0.40, pitched yesterday 27.3%, rest-2 24.7%,
> pitched in two days 52.0%, pitches in three days 15.8, left 31.6%, same-hand 50.9%. The
> prediction shift against OFF: the starter's strikeout mean +0.29 per start, the
> probability of clearing the closing line +5.4 points (mean absolute 10.8). **The first
> flip condition (the usage numbers sit on the box score's) reads met on the starter's
> pitches and their spread and on the manager tiers, and within 4% on pitchers per
> team-game and 3% on starter outs; the second (the paired accuracy run) has not run.**
> Follow-ons the ladders point at, not blockers: a finer rest ladder (σ 0.25) and a pen
> order that does not put yesterday's arms at the back.
>
> **The paired accuracy run — LAUNCHED 2026-09-13 18:22 UTC (owner instruction), RE-SCOPED
> at 18:50 to the first 250 Final games of 2024 (owner decision; the full season would take
> 43 hours on this VM and resolves a 5.7-point calibration change against the subset's 10;
> the balanced set's 58 lined starter-games read a −0.015 Brier gain for the draw at a
> per-record noise of 0.113, which 250 games show at three standard errors).**
> `scripts/sim427_accuracy_pair.sh` with `SIM427_MAX_GAMES=250`: the ON arm at the fitted
> values, then the OFF arm, 100 iterations, seed 0, five workers; the reports land as
> `scripts/sim427_accuracy_{on,off}.json`. Before it could start: the change pool gained each
> row's game date so the point-in-time cutoff applies to the change draw (the ON arm refused
> without it — the leak SIM-535 closes); the backtest reads the manager profiles like the API
> and stamps the arm's settings and the change pool's manifest time into its provenance; and
> the win-probability reliability curve, which this morning's `make calibrate` had dropped
> from `/data/calibration.json`, was written back from the 2026-08-16 validation report.

---

## 5. The owner's decisions (2026-09-13) and the production switch, in plain words

| # | Question | Decision |
|---|---|---|
| 1 | Which manager similarity weights the change draw? | **The usage sub-score** (starter pitch counts, pulled-before-100, closer entry leverage, high-leverage reliever share, opener and bulk rates, available-reliever usage) — the pull is a usage decision. |
| 2 | Where does the pen come from? | **The MLB API's per-game listing** — the box-score feed's `bullpen` and `pitchers` lists, one fetch per game the platform already makes. |
| 3 | How is the replacement chosen? | **A draw** on role, stuff and hand, **and on recent usage** — pitched in the last two days, pitches in the last three. |
| 4 | Delete the four dead hooks? | **Yes.** |
| 5 | When does production change? | See below. |

**What "the flip" is.** Everything in this plan is built behind switches that are OFF by
default, so while it is being built and tested the live simulator behaves exactly as it does
today. "The flip" is the one commit that turns the new behaviour ON for the live simulator:
it sets `SIM_MANAGER_DRAW=1`, the fitted manager power, the fitted reliever weights and the
real-pen source in the production environment file (`docker-compose.yml`) and in the
acceptance lane's flag list, and recreates the app container. From that moment every
simulation users see — the game page, the props, the accuracy backtest — uses the real
managers, the real pens and the two draws instead of the formula and the invented pen.

**What has to be true before it.** Two reads, both from the plan's own instruments:

1. *The usage numbers match reality.* On the balanced 45 games, the simulator's pitchers
   per team-game, its starters' pitches at the pull (the average AND the spread across
   starters) and its starter outs sit on the official box score's own numbers (for 2024:
   4.28 pitchers, 85.2 ± 17.0 pitches, 15.6 outs), and its change rate per boundary by
   manager type (quick hook / league / long leash) matches the pool's own. This is the
   "does it reproduce real usage" check, and today's numbers (2.4 pitchers, 80.8 ± 1.6
   pitches, 14.2 outs) are the failure it must fix.
2. *The predictions do not get worse.* The paired accuracy comparison — the same 2024
   games simulated twice, once with the switch off and once on, same seeds, same frozen
   bundle, each scored against what really happened and against the closing line — shows
   the ON arm is not less accurate than the OFF arm on the starter strikeout and outs
   markets and on the game totals. "Not less accurate" has a number: the average paired
   difference in the Brier score (the squared error of a probability against the 0-or-1
   outcome) is not above zero beyond its confidence range. If the ON arm is *more*
   accurate, that is the ticket's payoff showing up where the fund reads it.

**My recommendation is simply:** flip when both reads hold; do not flip on the first
alone, and do not wait for anything else.

> **FLIPPED 2026-09-13 (both reads held).** The usage read (part 4f's ON arm): 4.45
> pitchers per team-game against the box's 4.28, starter pitches 84.6 ± 19.2 against
> 84.7 ± 17.4, 15.0 outs against 15.5, the manager terciles' pull depth 80.8 / 85.4 /
> 87.9 against their own 79.3 / 84.5 / 88.4. The paired accuracy run (250 games of 2024,
> the owner's choice over the 43-hour season; `scripts/sim427_accuracy_pair.txt`): the
> starter strikeout market −0.0156 Brier for the draw [−0.0255, −0.0066], the moneyline
> −0.0062 [−0.0120, −0.0005], the game total flat (−0.0012 [−0.0091, +0.0070]), the
> first-five total flat leaning worse (+0.0069 [−0.0024, +0.0165]), every batter prop
> within ±0.0011; the outs market has no 2024 lines to read. Production now runs
> `SIM_MANAGER_DRAW=1`, `SIM_BULLPEN_SOURCE=box`, `SIM_ACTOR_POWER_MANAGER_USAGE=4`,
> `SIM_RELIEF_ROLE_SIGMA=0.1`, `SIM_RELIEF_REST_SIGMA=0.5`,
> `SIM_RELIEF_PITCHED2D_OFF_WEIGHT=0.25`, `SIM_RELIEF_PITCHES3D_SIGMA=10`; the formula,
> its helpers, the tendency reader and the reliever ranking are deleted; the lane's
> `PRODUCTION_FLAGS` match and a test holds them to the compose file. `CHANGES.md`
> 2026-09-13 (the flip entry) is the record; the leftovers are SIM-547. The accuracy run needs your odds re-load
finished and the bundle frozen for its ~13 hours; the usage read needs 45 minutes.

## 6. Traps

* **The manager matrix and the concentration check.** Do not run the manager matrix
  through `--strict-concentration`'s own-staff limit; it will fail by construction. The
  effective-sample floor is its guard.
* **The point-in-time gap.** A 2024 game's manager profile is the 2024 season's —
  in-sample. The recompute in 4a-4 lands the `asof_date` column; the backtest's
  bundle-as-of-one-date caveat (SIM-538) covers managers the same way it covers everyone.
  State it on the read; do not fix it here.
* **The positional INSERT / column-order class.** The change pool's meta parquet is
  written by name, not position — fine. `SIM_KWARG_KEYS` is a frozen set with a test: add
  the new keys there in the same commit as the resolver.
* **Two writes need the app stopped:** the manager profile recompute and the
  `league_averages` write (4a-3/4). The change-pool and matrix builds are read-only and
  do not. The app is down today (the image was rebuilt 2026-09-13 and not restarted) —
  the cheapest window is now.
* **The box's `bullpen` list includes the resting rotation starters.** A thirteen-arm
  staff lists nine in `bullpen` on a day four pitched: four of the nine are starters
  between turns. The role rule (started any of the team's last five games) removes them;
  an "opener" (a start under forty pitches) is not a starter — the role share from 4a-5
  tells them apart. Verify on hundreds of games against the SIM-433 table's active arms
  on the 2023-2025 overlap (90%+ agreement expected).
* **The synthetic pen must survive as the no-DB seam** or every no-DB game test breaks.
  Delete it from production only.
* **`GameSpec.key` sorts `sim_kwargs`:** the new keys invalidate every cached sim once —
  correct, and the Redis cache will refill.
* **Two heavy containers.** The accuracy run's six workers are one; nothing heavy beside
  it.

---

## 7. Sequence and cost

| Order | Part | Hands-on | Machine | Needs |
|---|---|---|---|---|
| 1 | 4a data: the pool's manager column, the matrix spec, the league row, the recompute, the role sidecar | 2 d | pool rebuild ~10 min; recompute ~30 min (app stopped); matrices ~5 min | — |
| 2 | 4b identities: the `raw.game_bullpen` table and its backfill over 2023-2026; managers and real pens; the kwargs contract; the API fields | 2.5 d | the box re-fetch ~1.5 h; a 45 × 20 identity smoke, ~15 min | 1 |
| 3 | 4c the manager weight | 1 d | — | 1, 2 |
| 4 | 4d the arm draw (role, stuff, hand, recent usage) | 2.5 d | — | 1 (the pool's rest columns), 2 |
| 5 | 4e the steal weight and the deletions | 1 d | — | 1 |
| 6 | 4f probe + fits | 2 d | ~45 min per two-arm probe; three or four rounds | 3, 4, 5 |
| 7 | the paired accuracy run | 0.5 d | ~13 h (two arms) | the odds re-load; a frozen bundle |
| 8 | the flip; the fatigue probe re-run | 0.5 d | 35 min | 7 |

About twelve working days of hands-on work. The old plan's estimate was nine to eleven
with part 5c (the draw) at four to five days; the redesign built that part, and the arm
draw and the real pen take its place.

---

## 8. The definition of done, restated under the current metric

The row says: "the pitching-change decision uses each team's own manager tendencies
instead of one shared league average, and the resulting pitching-change rate still matches
real-world data within the platform's accuracy band." The wording this plan delivers
against:

> Each dugout's pitching change is drawn with its own manager's usage-similarity weight at
> a fitted power, from a pool whose every row names its manager; each side's pen is the
> arms the MLB API lists as available for that game, with hands and recent usage; the
> entering arm is drawn by its resemblance to the arm the pool row brought in — in role,
> stuff, hand and recent usage. On the balanced 45 games the sim's
> pitchers per team-game, starter pitches at the pull (mean and spread) and starter outs
> sit on the official box score's own numbers, and the change rate per boundary by manager
> tier matches the pool's. A paired sim-vs-closing-line accuracy run shows the model is not
> less accurate with it on, and reports the direction of the change on the starter
> markets and the totals.
