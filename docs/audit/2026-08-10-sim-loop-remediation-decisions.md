# sim_loop.py remediation — owner decisions

**Date:** 2026-08-10 · **Baseline:** master @ `e4be4c1` · **Scope:** `simulation/sim_loop.py` open items

This document records the owner's decisions during the item-by-item walkthrough. It is a decision
log, not a plan. Each entry states the issue, the decision, and what the decision commits us to.

---

## Governing architecture rule (applies to every item below)

**Every decision in the simulator must be a similarity-weighted draw from a filtered play pool.
Never a hand-tuned probability formula.**

The required shape:

1. **Hard filter** the pool on the situation — outs, runners on base, score differential, count.
2. **Score every surviving play** with the applicable similarity engines.
3. **Draw one play at random**, weighted so a more-similar play is more likely to be drawn.

A property of the *comparison play* (the park it happened in, the pitcher's hand) belongs in the
**weight**, not the filter. A fact about the *game state* belongs in the **filter**.

---

## Already fixed on master (verified in the working tree)

| Item | Fix | Verified |
|---|---|---|
| Pitcher resurrection | `_maybe_pull_starter` now writes `home_pitcher_id` / `away_pitcher_id` by half | Correct. Matches `_set_half_matchup`'s read; ordering is safe because the half flips before the manager hook |
| Runs overwritten | `result.runs_scored += int(result_runs)` | Correct. No double-count risk — the K paths are mutually exclusive and each pitch gets a fresh `PlayResult` |

**Follow-up:** two stale comments now contradict the code — `game_state.py:247-252` still calls the
pitcher-id fields "starters", and `pitcher_decisions.py:14` says `GameState` does not carry them.

---

## Decisions

### Steal logic (closes original items #2, #8, #9)

**Issue.** The manager green-light is a probability (0.04–0.12 in production) used as an independent
Bernoulli gate in front of a second, already-MLB-calibrated probability. The two multiply. Worse, the
gate routes to `resolver.resolve_steal`, and production wires no resolver, so the stub answers "no
attempt" every time. **Production has recorded zero steal attempts since 2026-06-04.**

**Decision.** Replace the whole formula with a similarity-weighted pool draw. Build a new
**`sim.steal_opportunity_pool`**: one row per pitch where a steal was possible, attempted or not,
carrying `runner_id`, `pitcher_id`, `catcher_id`, the situation columns, an `attempted` flag, the
`success` flag, and `recency_weight`. One weighted draw then answers both "does the runner go" and
"safe or caught". Estimated 1.0–1.5M rows over ten seasons.

**No interim bridge.** Production keeps zero steals until the pool draw lands. Do not write code
that will be deleted.

**Commits us to:** a DuckDB migration + schema version bump, a builder in the profile computor,
artifact loader work, and a pool rebuild. The four relevant engines (`baserunner_steal`,
`pitcher_steal`, `catcher`, `manager`) already exist and build.

### #10 — Calibration is fitted to the broken simulator

**Issue.** `/data/calibration.json` and the win-probability reliability curve were both fitted
against the current, buggy simulator, so they absorbed its errors. The Track C fixes move scoring
7.653 → 8.207 runs per game (+7.2%) on a 300-game synthetic mix.

**Decision.** **Frozen control, one refit at the end.** Pin the calibration file, measure each fix
batch against that fixed baseline so the raw simulator change is visible on its own, then refit once
after all fixes land. Final win-probability curve fitted **multi-season over several hundred games**,
not the current 60-game slice of 2024.

### #11 — The production simulator has no tests

**Issue.** `tests/conftest.py` forces all six production flags off suite-wide. No test anywhere turns
`SIM_FULL_POOL` on. The four methods shaping every production pitch have zero test references, and no
`simulate_game` golden exists at any configuration.

**Decision.** **Statistical acceptance bands**, in a **new nightly production lane**. Run games in
full production configuration and assert aggregate rates land inside realistic bands — runs, hits,
home runs, walks, strikeouts, steal attempts, reach-on-error, pitchers per game, and no runner left
on an occupied base after a double play.

**Rationale.** A golden file detects change from a baseline; it cannot detect a wrong baseline. The
existing golden gate caught none of the four critical bugs. An assertion that steal attempts exceed
zero would have caught one of them on the first run.

**Also:** replace the six scattered flag pins with one fixture that flips every production flag on
together, so the flag list lives in one place.

### #12 — Home-run projections ignore the ballpark

**Issue.** `_apply_park_factor` only converts batted-ball outs to singles and singles to outs. Home
runs are park-invariant. Three findings reshaped the fix:

- `derived.park_factors` computes **nine** factor types (HR, 1B, 2B, 3B, BB, K, GB, FB, R) with real
  L/R batter splits. The simulator reads **one** (`R`).
- The documented pool-neutralization policy on `regressed_factor` — divide out the comparison play's
  origin-park factor before applying the target park — is **implemented nowhere**. The current code
  double-counts.
- `venue_id` is already per-row in the batted-ball artifact and the sampler never reads it.

**Owner's objection (correct).** A scalar factor cannot tell Fenway from Coors. Both read as
hitter-friendly for opposite reasons. A 320-foot fly to left is a double off Fenway's 37-foot wall at
310 feet, and an out at Coors' 8-foot wall at 347 feet.

**Pool-size finding.** Per-park geometry-conditioned sampling goes thin exactly at the fence: roughly
30–80 rows per park per boundary cell over ten seasons. Parks also move fences (Camden Yards moved
its left-field wall ~30 feet in 2022), so any park reference must be keyed by venue **and season**.

**Decision.** **Separate the physics from the outcome.**

- **Draw stage** — draw the geometry (exit velocity, launch angle, spray angle) from the **whole**
  similarity-weighted pool. No park filter. The pool never shrinks.
- **Resolve stage** — **hybrid: geometry at the fence.** Use a per-park wall profile for the
  home-run / off-the-wall / caught decision, where geometry dominates and data is thinnest. Use
  shrunk per-event park factors for everything else. Wire the **four batted-ball factor types**
  (HR, 1B, 2B, 3B) as that statistical layer.
- **New reference table `derived.park_geometry`**, hand-curated and reviewed by eye: `(venue_id,
  season, spray_sector)` → `wall_distance_ft`, `wall_height_ft`, `carry_factor`. About 360 rows.

**Open data question, unresolved.** The artifact's geometry is only three columns wide, so measured
`hit_distance` never reaches the workers. It exists in `sim.outcome_pool`. Adding it is a one-column
artifact change with no migration. **Caveat:** Statcast distance for a ball that hits a wall or is
caught records where it stopped, not projected carry — wrong for exactly the wall-scraper cases that
matter. Verify how the ETL populates it before relying on it.

### #13 — The validation harness cannot see two of the features it validates

**Issue.** `simulate_game` accepts `home_defense`, `away_defense` and `park_run_factor`. Production
passes all three (`api/routes/games.py:542`). The realism harness passes none
(`scripts/sim_stats.py:100`), so `SIM_PARK_FACTOR` and `SIM_FIELDER_RBF` are **provably inert** under
it. Any A/B of those flags compares two identical no-ops.

This blocks validating the #12 park work.

**Corrections to the earlier report.** Only two of six flags are blind, not all. `SIM_MANAGER`,
`SIM_BB_PLATOON`, `SIM_FRAMING` and `SIM_HOME_FIELD_BIAS` are exercised. But `SIM_MANAGER` runs a
single **league-flat default** profile for every team (`production_factory.py:568`), so team-to-team
strategy variation is untested.

**Fix (routine, not asked).** Delete `_sim_kwargs` from `sim_stats.py` and import
`_sim_kwargs_from_state` from `api.routes.games`, as `clv_backtest.py:803` and
`validate_props.py:165` already do. The duplicate is what drifted. Needs an image rebuild —
`scripts/` is not mounted.

**Decisions.**
- **Re-validate all six flags, one at a time**, at 400 simulations × 20 games, against the frozen
  calibration from #10. Four of the six have never been measured at real power.
- **Wire the real per-team manager profiles now**, in both the harness and production. The
  league-flat default means no team currently behaves differently from any other.

### #14 — Five scoring channels are missing

**Issue.** Hit-by-pitch, wild pitch, passed ball, balk and pickoff do not exist in the simulator
(~0.15–0.25 R/team-game). The dropped-third-strike branch is gated on a hook nothing implements, so
it can never fire. The five split into three groups by data availability:

- **Hit-by-pitch — data present, label discarded.** `outcome_type` has five buckets derived from
  Statcast `type`; an HBP is type `'B'` and falls into `ELSE 'ball'`
  (`player_profile_computor.py:4701`). `raw.pitches.events` does carry `'hit_by_pitch'`. Run values
  and the non-at-bat classification are already in place. **Needs no new ingest** — only a widened
  `outcome_type` and `events` carried on non-in-play rows.
- **Wild pitch / passed ball — present but merged** into one boolean
  (`etl_historical_loader.py:1120`). The distinction assigns blame to different players and therefore
  different engines, so a merged flag cannot produce a correct weighted draw.
- **Balk / pickoff — not ingested.** The schema comment "not in Statcast" is true of Statcast and
  misleading about the pipeline: the ETL reads the MLB play-by-play feed and already walks
  `details.eventType` for six stolen-base types in the same loop. `GameState` already accepts
  `"pickoff"` as a steal outcome.

**Decisions.**
- **Batch the ETL work: write the parser changes now, run nothing.** Collect every ETL column change
  the remaining items need, then re-sweep once.
- **Ship hit-by-pitch independently** — it needs no sweep.
- **Wire the dropped third strike** properly, scored by the catcher engine.

### #15 — A runner on first never advances on an out

**Issue.** `_full_pool_out_advancement` sets `new_first` from `old.first` at `:1524` and **never
reassigns it**. A runner on first cannot advance on any out. The batter is always the out, so a force
play at second and a fielder's choice cannot happen. Thirty lines hold seven tuned constants (`24.0`,
`28.0`, `0.30`, `0.28`, `0.35`, `0.92`, `0.45`) plus the global `_run_calib_value` multiplier already
judged to be the wrong lever.

**Decision.** Replace all of it with a **transition mapping** drawn from the pool. The pool gives a
base-to-base *pattern*, not identities — pool runners are other players — so apply the pattern to the
live runners. **Weight the draw by the baserunner advancement similarity scores**, then apply the
sampled play's advancement.

**Hard filter width:** base occupancy (8) × outs (3) × count (12) × score-differential band (5) =
1,440 cells, ~960 candidate plays each out of ~1.38M batted balls. Ample.

**Runner combination:** **run-value weighted average** over occupied bases, with the per-base weights
taken from `derived.run_expectancy_matrix`. The runner on third dominates; the weight never collapses
toward zero the way a product does.

**Also decided:** emit the **effective sample size** per draw — `(Σw)² / Σw²` — alongside the
post-filter cell size. Report only, no automatic response (see the subset rule below, which is what
makes report-only safe).

**Column requirement:** six columns on `sim.outcome_pool` from `raw.pitches` — `on_1b/2b/3b` and
`post_on_1b/2b/3b` as player IDs. No pool table carries runner identities today (`runners_state` is a
3-bit occupancy mask). **DuckDB + artifact change only. No ETL change, no re-sweep.**

### #16 — Fatigue never makes a pitcher worse

**Issue.** `pitcher_fatigue` (`:409`) is read only by the pull decision (`:3205`).
`tto_effectiveness` (`:445`) is read only by reliever selection (`:3272`). Neither touches the
pitch-outcome draw, so a pitcher at 105 pitches throws exactly as well as on pitch one. Because every
pitch is drawn from the same season-long distribution, hits arrive independently — real hits cluster,
and runs are convex in baserunners, so even a correct hit rate under-produces runs. Per-channel rate
calibration cannot reach this.

**Correction to the earlier report.** I said this item would not fit the pool-draw rule. It does. Both
facts are derivable in the pool build with the window-function pattern the build already uses for the
previous-pitch columns (`player_profile_computor.py:4688`):
`ROW_NUMBER() OVER (PARTITION BY game_pk, pitcher_id ORDER BY at_bat_number, pitch_number)`.
**Two columns, one query, no ETL change and no re-sweep.**

**Decisions.**
- **Weight, do not filter.** A hard band creates a false cliff between pitch 75 and pitch 76. Fatigue
  is smooth.
- **Principle adopted: discrete facts filter, continuous facts weight.** Base occupancy, outs and
  count filter. Pitch count, times-through-the-order and park geometry weight.
- **Wire the four unused previous-pitch columns** as a weight term. `prev_pitch_velo/ivb/hb/outcome`
  are computed nightly by the pool build and read **nowhere** in `simulation/`, `similarity/` or the
  rest of `pipeline/`. They are the within-inning clustering lever that fatigue alone cannot provide.

### #17 — The batted-ball draw ignores the pitch that was thrown

**Issue.** The batted-ball weight is one line (`full_pool_sampler.py:325`):
`w = f_bat * f_sit * pool.recency`, plus an optional soft mask on throwing hand. No pitcher factor and
— more fundamentally — **no pitch factor**. The pitch draw reads its row's outcome type and discards
the row (`:252` never stores the index, unlike `_bb_last_i` at `:345`). So the simulator can draw a
98 mph fastball at the top of the zone and then draw a batted ball that really came off an 84 mph
changeup below the knees. **The two draws can contradict each other.**

**The register said this was blocked on the ground-ball-rate bug. It is not.** Under the rule you
condition on pitcher *similarity*, and ground-ball rate is not in the pitcher engine at all — it is
0.65 arsenal (a mixture model over velocity, break, spin, release) plus 0.35 command over seven rate
features. A sinkerballer resembles other sinkerballers through arsenal geometry, which is a better
mechanism than a rate because it carries *how* the ground balls happen.

**Owner's design.** The batted ball is caused by the physical pitch meeting the bat. So **pitch
similarity to the pitch actually drawn is a primary weight**, batter similarity is primary, and
**pitcher similarity is a small residual term** — once the pitch is known, the pitcher's contribution
is largely already expressed in it. The residual captures only deception, extension and sequencing.

**Requirements.**
1. **The batted-ball artifact discards every pitch characteristic.**
   `_BB_GEOM_COLS = ["exit_velo", "launch_angle", "pull_relative_spray_angle"]`
   (`engine_artifacts.py:116`). `sim.outcome_pool` holds the ten pitch columns and the build query
   selects them all; the artifact loads none. Cost to add: ten float columns on ~1.38M rows, ~55 MB.
   The pitch pool already carries the identical ten-column geometry.
2. **The pitch draw must expose its drawn row** — mirror the `_bb_last_i` pattern. `HandPool.geom` is
   already `(N, 10)` with those columns, so nothing new loads on that side.
3. **`pitcher_id` on the batted-ball pool** for the small residual term. Present in
   `sim.outcome_pool`, absent from `BattedBallPool`. One column, no migration.
4. **`pitch_pitch_similarity` gives the metric, not the index.** It is built as a FAISS
   nearest-neighbour lookup, which suits the fallback path. The full-pool path needs the same metric
   as a **vectorized kernel over every row**, the way `f_sit` works at `:322`.

**Live data defects that corrupt `f_pitcher` — a different dependency than the register named:**

- **`whiff_rate` is the called-strike rate** (`player_profile_computor.py:1630`):
  `SUM(CASE WHEN type = 'C' ...) / COUNT(*)`. Type `'C'` is a called strike; a whiff is `'S'`, `'W'`
  or `'M'`, as the pool build itself classifies at `:4702`. Two consequences, the second worse: one of
  seven command features measures the wrong thing, and **swing-and-miss ability is absent from the
  pitcher engine entirely**. A correct version of this calculation already exists at `:1858` for a
  different table — two definitions in one file, one right and one wrong.
- **Ground-ball / fly-ball / line-drive rates use an outs-only denominator** (`:1650`):
  `... / NULLIF(SUM(CASE WHEN type='X' THEN 1 END), 0)`. The data uses three in-play codes — `X`, `D`
  and `E` — and `X` is only the balls in play that produced an out. Rates inflate ~1.4×, and the
  inflation is **larger for pitchers who allow more hits**, so the error correlates with pitcher
  quality. Signature: the three rates sum to ~1.4 instead of ~1.0.
- **Cost to fix both:** a profile recompute (~5.7 hours), not a re-sweep, then the scheduled
  calibration refit.

### Governing refinement — the per-decision weight subset

**Not every score weights every draw.** Each sampling step uses a **subset of roughly three to five
scores**, with explicit high and low tiers. Recency is a sample weight, not a similarity score, and
sits outside the subset.

For the batted-ball draw the owner specified: **batter (high), pitch (high), pitcher (low), previous
pitch (low).**

**Why this matters beyond tidiness.** Multiplying nine kernels over ~960 candidates can collapse the
effective candidate count to a handful — at which point the simulator is copying one historical play,
not sampling a distribution. The subset rule prevents that by construction, which is what makes the
"report only" choice on the effective-sample-size check safe.

**Weights are fitted by backtest**, not hand-set. Give each factor a temperature and fit the
temperatures so drawn outcomes best match held-out real outcomes. The owner's stated ordering is the
starting point; the data settles the magnitudes.

**Runner advancement is its OWN sampling step**, not a fifth weight on the batted-ball draw. The
batted-ball draw must not let the runners on base influence what the batter hits.

**The situation engine leaves the draw.** `f_sit` compares six columns — balls, strikes, outs, base
occupancy, inning, score differential. Four become the hard filter, leaving only inning and
within-band score differential as a soft signal. That residual is too thin to justify a full pool pass,
and the filter does the job more precisely than the kernel did. One of the eleven engines therefore
changes role; this is deliberate, not an oversight.

#### The per-decision weight table

| Sampling step | High weight | Low weight | Hard filter |
|---|---|---|---|
| Pitch outcome | pitcher, batter | catcher (framing), previous pitch, pitch count + TTO | count, outs, base state, score band |
| Batted ball | batter, pitch | pitcher, previous pitch | count, outs, base state, score band |
| Advancement on outs | baserunner | batted ball | base-out state |
| Fence resolution | *geometry, not similarity* | batted ball | park + spray sector |
| Fielding: out / hit / error | fielder | batted ball | — |
| Steal: attempt + outcome | baserunner steal, catcher | pitcher steal, manager | base state, outs, count, score band |
| Dropped third strike | catcher | pitcher | two strikes, 1B open or two outs |
| Wild pitch, balk, pickoff | pitcher | — | base state |
| Passed ball | catcher | pitcher | base state |

Rows for fielding, steals, wild pitch / balk / pickoff and passed ball are proposals awaiting
confirmation. Every other row is decided.

### #6 + #22 + #23 — one defect: the run-value ledger derives states instead of being told them

**Symptoms.**
- **#6** — `_commit_run_delta` (`:1567`) reads `state.outs` / `state.runners_state` at `:1596` as the
  "before" state. Four callers mutate first: `_resolve_walk` (`:1929`→`:1940`), `_resolve_in_play` plus
  `_apply_sac_fly_bias` (`:2323`, `:2351`, `:2358`→`:2378`), `_resolve_strikeout`'s dropped-third-strike
  path (`:1988`→`:1989`), and `_resolve_steal_outcome` (`:1830`, `:1858`).
- **#22** — `run_resolution.advance_state` derives the after-state by conservation
  (`new_on_base = old + reached − runs`) and has no concept of a runner **retired** on the play, so a
  double play desyncs it. Measured: 4 of 8 DP shapes consistent before the proposed fix, 2 of 8 after.
- **#23** — a reach-on-error passes `result_hits=0`, so the batter reaching first never enters the
  conservation sum. **Every ROE records a run value of exactly 0.00.** True value ≈ +0.38.

**Decision.** **Explicit pre- and post-states, mandatory** on every commit. Delete the conservation
derivation. Every path can supply both: pool draws from the #15 transition, walks and strikeouts
deterministically (a walk pushes forced runners, a strikeout changes nothing). No fallback path, so a
wrong ledger cannot be produced silently. This dissolves all three symptoms.

**Blast radius today** is the tracked claim, not independently re-verified: the run *value* is
display-only (play-by-play), not scores, props or CLV. But it is the **input to the run-conversion
calibration** the whole effort exists to enable.

### NEW — the run-expectancy matrix is biased low

`build_run_expectancy_matrix` (`player_profile_computor.py:464`) computes each half-inning's final
score as `MAX(bat_score)` over its plate-appearance rows. `bat_score` is the score **entering** that
plate appearance, so the runs scored on the **last** plate appearance of a half-inning appear on no
later row and are invisible. Every value is slightly too low, and the bias is largest where a run most
often scores on a two-out play — a runner on third with two outs. The build also excludes the ninth
inning onward ("for unbiased estimates"), so extra innings contribute nothing. **Magnitude not
measured.**

This became load-bearing during this session: the matrix now feeds both the run-value ledger and the
run-value weighting chosen in #15. The two uses are affected differently — the baserunner weights are
relative so a proportional bias mostly cancels; the run values inherit it directly.

**Decision.** **Fix the build now without measuring first**, and fold it into the scheduled ~5.7-hour
profile recompute.

### #18 — Home-field advantage is one channel and knowingly too strong

**Issue.** `_apply_home_field_bias` (`:2125`) converts a fraction of home-team batted-ball **outs into
singles** and reads nothing but the inning half (`:2146`). Three problems:

1. **The magnitude is known wrong and was never corrected.** Default 0.025; your own 4×400-sim harness
   measured +0.198 R against a +0.13 target and recorded a retune to ~0.017. Nobody applied it. The
   simulator has run ~50% too much home-field advantage ever since.
2. **All of it lands on one channel** — home-batter singles, i.e. home-batter H and TB, the exact
   markets the CLV read labels "trustworthy".
3. **It stacks with the park on the same channel** — HFA at `:2330`, park at `:2336`, both flipping
   out↔single.

**Decision.** **Home-or-away becomes a hard filter dimension, on BOTH draws** — the pitch-outcome draw
as well as the batted-ball draw, so umpire bias on called strikes reaches the model (walks and
strikeouts are traded markets). Delete `_apply_home_field_bias`, `SIM_HOME_FIELD_BIAS`, the 0.025
default and the unapplied 0.017 retune.

**Column requirement.** `raw.pitches.inning_topbot` holds `'Top'`/`'Bot'` and is ETL-validated
(`etl_historical_loader.py:520`). **No pool table carries it** — zero matches in the DuckDB schema. One
column on the pool + artifact. **No ETL change, no re-sweep.**

**No collision with the park work.** Pool rows for "home team batting" carry both the generic
home-field effect and their own park. The park half is removed by the origin-versus-target
neutralization committed in #12; the generic half survives, which is what we want. No team-quality
confound either — every team plays equal home and away games.

### #19 — Redundant pool passes, plus a staleness bug nobody filed

**Tracked issue.** `key = (state.pitcher_id, hand, state.batter_id)` at `:1341` gates
`new_half_inning`, which caches `f_pitcher × recency` over the **whole** pool. Because `batter_id` is
in the key, every new batter re-runs it. ~83 plate appearances versus ~5 pitchers per game means
roughly **78 unnecessary full-pool passes per game.**

**NEW, unfiled.** The key omits base-out state, but `new_plate_appearance` uses `base_out` to build the
situation factor. Base-out changes **inside** a plate appearance — a steal, a runner advancing on a
wild pitch, a caught stealing adding an out — and nothing refreshes, so **the situation factor goes
stale for the rest of the PA.** Under the new design base-out is a hard filter, so this becomes drawing
from the **wrong cell**, not a slightly-off weight.

**The mechanism we need already exists.** `new_plate_appearance` splits weights into **twelve
precomputed count buckets** and `draw` restricts to the live count's bucket (`:242`, `:249`). The count
is already a hard filter served by a precomputed row index. It generalizes to the full filter with no
new invention.

**What that buys.** Full filter = base occupancy (8) × outs (3) × count (12) × score band (5) ×
home-or-away (2) = **2,880 cells**. Precompute `bucket_rows` for all of them and each draw scans its
cell instead of the pool — roughly a **thousandfold** reduction in per-draw work. The index is ~1M
integers, a few megabytes. **This is also the answer to the open 30-second simulation target**, which
the notes call "irreducible per-PA full-pool scoring." It is only irreducible because nothing narrows
the candidate set.

**Correction to an earlier figure.** I said ~480 plays per cell. Wrong — the pools are **partitioned by
batter hand**, so the batted-ball pool holds ~690k rows per hand, giving **~240 per cell on average**
and far fewer in rare cells (3-and-0, bases loaded, two outs, blowout, home batting could hold under
twenty).

**Decisions.**
- **Three separate cache lifetimes:** pitcher factor on `(pitcher, hand)` change, batter factor on
  batter change (already memoized), filter cell on base-out change. Fixes both bugs, and it is the
  structure the new weight stack needs.
- **Thin cells widen in a fixed order** with a minimum cell size: relax the score band first, then
  home-or-away, then the count. Log every relaxation.

### Sweep-state correction (2026-08-10)

`.sweep_progress/` shows the reload sweep **completed all ten seasons** — 27 July 17:53 to 30 July
01:20, about **55 hours**, ~21,336 games (2020 = 953 short season, 2026 = 1,625 in progress). It ran
with the `c551f8d` parser, and the later `sim-447` commits patched the stragglers.

**Three items are NOT sweep-blocked, contrary to the earlier report:**

1. `outs_on_pitch` — measurable now on any season. The item-#3 decision is unblocked today.
2. `post_on_*` — the retention fix is in the swept data. Needs only a pool + artifact rebuild.
3. The steal-opportunity pool — same. Rebuild only.

**One item got worse:** the window for free ETL columns is closed. Any new column costs a fresh
~55-hour re-sweep, which is why #14's parser work is batched rather than run.

### #20 — The file holds seven concerns in 3,925 lines

**Measured shape** (not the tracked description): the manager model is the largest single block at
~670 lines (`:2807`–`:3477`). The rest splits into module helpers (~390), the per-tile resolver (~155),
fingerprint stubs and the PA simulator (~140), setup + `step_pitch` (~300), the full-pool draws (~280),
the run commit (~125), baserunning + steal outcomes (~255), walk/strikeout resolution (~160), the three
realism nudges (~140), in-play resolution + boxscore accumulation (~370), inning/out/order primitives
(~125), and the result types + `simulate_game` (~160).

**The architecture work shrinks this file.** Today's decisions delete ~350–400 lines:
`_apply_home_field_bias` (~43, #18), `_apply_park_factor` (~54, #12), `_apply_sac_fly_bias` (~46, #15),
`_full_pool_out_advancement` (~55, #15), `_tag_rate` (~14, #15), `_fielder_rbf_nudge` (~50),
`_full_pool_steal_decision` (~49), and the green-light gate (~15). The code being *added* — the
per-decision draws — belongs in `full_pool_sampler.py`, not here.

**Decisions.**
- **Extract nothing until the architecture lands.** Decompose once, on the final shape, rather than
  relocating code the decisions delete.
- **Retire the per-tile fallback path after the #19 filter lands.** Its two reasons for existing both
  dissolve: #19 removes its speed advantage, and #11 established that its being the test default is
  precisely why four critical bugs survived. Deleting it removes the test-versus-production divergence
  at the root instead of papering over it with a second lane. **This also dissolves item #25.**

### #7 — The random-number streams are not independent

**Issue.** Four generators are built from the same integer: the loop rng (`:3723`), `sampler.rng`
(`:3743`), `_pa.sampler.rng` (`:3750`), and the full-pool rng (`:3757`). Two structural oddities:
`PlateAppearanceSimulator` is constructed with the **same** sampler object (`:1052`), so `:3743` and
`:3750` write the same attribute and the second discards the first; and `_pa.rng` is bound to the
machine's rng at construction (`:1056`) while `:3723` **rebinds** `state_machine.rng`, so `_pa.rng`
points at the construction-time generator and is never reseeded at all.

**Production-consequential scope is narrower than the tracked note claims.** Only the loop rng
(advancement, steal outcomes, manager decisions, framing) and the full-pool rng (pitch and batted-ball
draws) are used on the production path. They produce **identical sequences**.

**Honest limit.** The tracked note says the streams are "perfectly correlated" and that this corrupts
prop distributions. The first half is true by construction. The second I cannot confirm: the two
generators are consumed at very different rates, so their alignment drifts and the dependence is not
one-to-one. What is firm: **the streams are not independent, and nobody specified or measured the
dependence.** For a simulator whose output is a distribution priced against a market, that is not
acceptable even if small. Compressed variance does not move the mean — it makes tail prices look better
than they are, which is where prop overs and unders live. **Magnitude not measured.**

**Decisions.** **Two streams, fix now** — `SeedSequence(seed).spawn(2)` for the loop rng and the
full-pool rng; leave the per-tile generators, which are unused in production and are being deleted. The
two-stream form is the permanent answer after the per-tile retirement, so nothing here is throwaway.
**Measure after, as a check:** rerun the same seeds and compare per-prop mean, standard deviation and
tail quantiles. If the spread moves materially, the existing K/BB expected-calibration-error readings
need redoing.

### #21 — Manager small-ball is decorative and the frontend narrates it

**Issue.** Three mechanics are signalled and never resolved. `_maybe_sac_bunt` (`:3333`) records a
decision and its own docstring defers the outcome to "SIM-319's job", which never implemented it.
`pitch_out_signalled` is written at `:2931` and read **nowhere** in `simulation/`. The hit-and-run only
*suppresses* the steal initiate (`:2917`) — the runner does not go and the batter is not biased toward
contact. All three land in `manager_decisions`, which feeds the play-by-play, so **the front end tells
users a bunt was called when nothing happened.**

**Decision.** **Express manager small-ball as draw weights.** Aggression shifts the draw toward or away
from bunt attempts, pitch-outs and contact-oriented plate appearances; the outcome comes from real
plays. Delete `_maybe_sac_bunt`, `pitch_out_signalled` and the hit-and-run suppression branch. The
decide-then-resolve split disappears, and what the front end narrates becomes what happened.

### #24 — A run batted in credited on a steal of home

**Issue.** MLB Rule 9.04(b) awards no RBI on a stolen base. `bat.rbi += runs` at `:2590` takes `runs`
from `result.runs_scored`, which a steal contributes to.

**Status change.** Latent today because nothing stages a steal from third. The steal-opportunity pool
carries `base_attempted` values of `'2B'`, `'3B'` and `'home'`, so **the pool work makes it live.** The
owner's `+=` fix was correct and also removed one of the two things hiding it — before that fix the
steal run was overwritten and could never reach the credit.

**Decision (routine, not asked).** Exclude steal runs from the RBI and earned-run credit, as part of
the steal pool work.

### #26 — The consistency guard cannot fail

**Issue.** `Bases.assert_consistent()` (`game_state.py:148`) rejects exactly one thing: a negative
runner id. Its docstring is candid — invalid-state detection "is SIM-326's harness". The defect is not
the function but that later code calls it as a correctness check. It cannot detect a runner standing on
a base he was doubled off.

**Decision.** **Add real invariants** — no duplicate runner ids, the batter not already on base, at most
three runners, ids non-negative — plus a transition assertion:

```
runners_before + batter_reached == runners_after + runners_scored + runners_retired
```

That is the #22 conservation formula. **Wrong as a way to compute the after-state; right as a way to
check one.** The transition-application code is new, which is exactly what wants an assertion.

### #3 — dissolves into #15

The question "should we use the outs count the pool returns?" is answered by the transition mapping: the
pool supplies the whole transition, so outs come from it too, not from event inference. The separate
per-season flag and measurement plan are no longer needed. (The sweep correction above also removed the
data blocker.)

---

## Consolidated change inventory

The nineteen items resolved into **one architecture**, not nineteen fixes. The physical work is:

### Pool + artifact columns — no ETL change, no re-sweep

| Column(s) | Source | For |
|---|---|---|
| `on_1b/2b/3b`, `post_on_1b/2b/3b` (player ids) | `raw.pitches` | #15 transition mapping |
| 10 pitch-feature columns into the batted-ball artifact | `sim.outcome_pool` (already selected) | #17 pitch similarity |
| `pitcher_id` on the batted-ball artifact | `sim.outcome_pool` | #17 pitcher residual |
| `inning_topbot` / `batting_team_is_home` | `raw.pitches` | #18 home-field advantage |
| `pitcher_pitch_count`, `times_through_order` | window functions in the pool build | #16 fatigue |
| widened `outcome_type` + `events` on non-in-play rows | `raw.pitches.events` | #14 hit-by-pitch |
| `prev_pitch_velo/ivb/hb/outcome` into the artifact | already built, read nowhere | #16 clustering |
| `hit_distance` into the artifact | `sim.outcome_pool` | #12 — **semantics unverified, see #12** |
| **New table** `sim.steal_opportunity_pool` | `raw.pitches` | the steal draw |
| **New table** `derived.park_geometry` (~360 rows, hand-curated) | public data | #12 fence resolution |
| `bucket_rows` extended from 12 to 2,880 cells | pool build | #19 filter index |

### Profile recompute (~5.7 hours), one pass

- `whiff_rate` is the called-strike rate (`:1630`) — swing-and-miss is absent from the pitcher engine.
- GB/FB/LD denominator counts outs only (`:1650`) — rates inflate ~1.4×, biased by pitcher quality.
- The run-expectancy matrix misses last-plate-appearance runs (`:464`).

### ETL re-sweep (~55 hours), batched, run once

- Split `passed_ball_wild_pitch` into two columns.
- Add `balk` and `pickoff` event types to the loop that already reads stolen-base event types.

### Code

- Rewrite the draw as the per-decision weight table (in `full_pool_sampler.py`).
- Delete ~350–400 lines of hand-tuned nudges from `sim_loop.py`; retire the per-tile path after #19.
- Explicit pre/post states on the run-value ledger; delete the conservation derivation.
- Three cache lifetimes; thin-cell widening in a fixed order; emit effective sample size.
- Two independent RNG streams.
- Real base-state invariants plus the transition assertion.
- Fix `scripts/sim_stats.py` to import `_sim_kwargs_from_state`; wire real per-team manager profiles.

### Validation

- Statistical acceptance bands in a new nightly production lane.
- Re-validate all six realism flags one at a time at 400 simulations × 20 games, against a frozen
  calibration.
- One calibration refit at the end; win-probability curve fitted multi-season over several hundred games.

---

## Sequencing constraints

1. **Fix the harness (#13) before the park work (#12).** Otherwise the park work cannot be measured —
   both its consumers are provably inert under the current harness.
2. **Batch the pool rebuilds.** Every column above lands in one rebuild, not eleven.
3. **Batch the ETL re-sweep.** The window for free columns closed on 30 July; a new column now costs
   ~55 hours, so collect them all first.
4. **Retire the per-tile path only after the #19 filter proves fast enough.**
5. **Generate no golden fixtures until the fixes land.** A golden made now freezes the bugs — and the
   chosen validation is acceptance bands, not goldens, for exactly that reason.
6. **Refit calibration last**, against the frozen-control measurements taken per fix batch.
