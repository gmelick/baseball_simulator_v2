# Sim-loop remediation — sequenced tickets

**Source:** `docs/audit/2026-08-10-sim-loop-remediation-decisions.md` (19 items, all decided 2026-08-10)
**Baseline:** master, next free ID **SIM-449** (per `BACKLOG.md`)
**Scope:** 45 tickets, SIM-449 … SIM-493, in 9 phases. **Next free ID after filing → SIM-494.**

The 19 open items resolved into **one architecture**: hard-filter the play pool on the situation,
score the survivors with a small subset of similarity engines, draw one weighted by similarity. These
tickets are that architecture, sequenced by dependency.

---

## Phase 0 — Unblock measurement

Nothing below can be validated until these land. Two of the six realism flags are provably
unmeasurable today.

### SIM-449 — Fix the validation harness so it passes the defense maps and park factor
`scripts/sim_stats.py::_sim_kwargs` drops the already-resolved `home_defense` / `away_defense` and
never passes `park_run_factor`. Both consumers in `sim_loop.py` are therefore inert under it, so any
A/B test of `SIM_PARK_FACTOR` or `SIM_FIELDER_RBF` compares two identical no-ops and tautologically
reports "no distortion". The API already builds the correct arguments — extract that logic to a shared
`_sim_kwargs_from_state` and call it from both.
**Depends on:** nothing. **Blocks:** SIM-450, SIM-488, and every park ticket.

### SIM-450 — Statistical acceptance-band validation lane
The four core production methods have zero test references, and `tests/conftest.py:33-53` pins the
production mode OFF suite-wide. Build a nightly lane that runs the production configuration and asserts
**per-channel acceptance bands against real MLB rates** (hits, home runs, walks, strikeouts, steals,
runs, double plays, reach-on-error) rather than a byte-exact golden. Bands survive intentional model
changes; a golden does not, and a golden generated now would freeze the current bugs.
**Depends on:** SIM-449.

### SIM-451 — Measure the filter cell-occupancy distribution
Before fixing a minimum cell size, measure the truth. Cross-tab the pool by the full filter — base
occupancy (8) × outs (3) × count (12) × score band (5) × home-or-away (2) = 2,880 cells — and report
the occupancy percentiles at 3 seasons and at 10. Output sets `MIN_CELL` in SIM-472 and confirms the
widening order.
**Depends on:** nothing. **Blocks:** SIM-472.

---

## Phase 1 — Independent fixes, no rebuild required

### SIM-452 — Two independent random-number streams
`SeedSequence(seed).spawn(2)` for the loop rng and the full-pool rng. Today both are built from the
same integer at `:3723` and `:3757` and produce identical sequences. Leave the per-tile generators —
unused in production, deleted by SIM-483. Per-seed reproducibility is preserved.
**Then measure:** rerun the same seeds and compare per-prop mean, standard deviation and tail
quantiles. If spread moves materially, the existing K/BB calibration readings need redoing.

### SIM-453 — Explicit pre- and post-states on the run-value ledger
Make both states required arguments on `_commit_run_delta` (`:1567`) and **delete the conservation
derivation** in `run_resolution.advance_state`. This one change dissolves three tracked defects: the
"before" state read after mutation by four callers, the double-play desync, and the reach-on-error
recorded at exactly 0.00 run value. Pool draws supply both states from the transition; walks and
strikeouts supply them deterministically. No fallback path, so a wrong ledger cannot be produced
silently.

### SIM-454 — Real base-state invariants and a transition assertion
`Bases.assert_consistent()` (`game_state.py:148`) rejects only a negative runner id, yet later code
calls it as a correctness guard. Add: no duplicate runner ids, the batter not already on base, at most
three runners. Add `assert_transition(pre, post)` asserting
`runners_before + reached == runners_after + scored + retired` — the SIM-453 formula, correct as a
check even though it is wrong as a derivation.
**Depends on:** SIM-453.

### SIM-455 — Split the weight cache into three lifetimes
`key = (pitcher_id, hand, batter_id)` at `:1341` gates `new_half_inning`, which recomputes the
half-inning-constant pitcher factor over the whole pool on **every new batter** — roughly 78 wasted
full-pool passes per game. The key also omits base-out state, so a steal or a wild pitch mid-plate-
appearance leaves the situation factor **stale for the rest of the PA** (unfiled; becomes a wrong-cell
draw once the filter lands). Cache the pitcher factor on `(pitcher, hand)`, the batter factor on batter
change, and the filter cell on base-out change.

---

## Phase 2 — Data corrections, one profile recompute

### SIM-456 — `whiff_rate` measures called strikes, not swings and misses
`player_profile_computor.py:1630` computes `SUM(CASE WHEN type = 'C' …) / COUNT(*)`. Type `'C'` is a
called strike; a whiff is `'S'`, `'W'` or `'M'`, as the pool build itself classifies at `:4702`. Two
consequences, the second worse: one of seven command features measures the wrong thing, and
**swing-and-miss ability is absent from the pitcher engine entirely**. A correct version already exists
at `:1858` for a different table — two definitions in one file, one right and one wrong.

### SIM-457 — Ground-ball / fly-ball / line-drive rates use an outs-only denominator
`:1650` divides by `SUM(CASE WHEN type='X' …)`. The data uses three in-play codes — `X`, `D` and `E` —
and `X` covers only balls in play that produced an out. Rates inflate ~1.4×, and **the inflation is
larger for pitchers who allow more hits**, so the error correlates with pitcher quality. Detection: the
three rates sum to ~1.4 instead of ~1.0.

### SIM-458 — The run-expectancy matrix misses last-plate-appearance runs
`build_run_expectancy_matrix` (`:464`) takes each half-inning's final score as `MAX(bat_score)` over
its plate-appearance rows. `bat_score` is the score *entering* a plate appearance, so runs scored on the
**last** plate appearance of a half-inning appear on no later row and are invisible. Every value is
biased low, worst where a run most often scores on a two-out play — a runner on third with two outs.
This table now feeds both the run-value ledger and the baserunner weighting, so it is load-bearing
twice. Fix the build to read the true end-of-half-inning score.

### SIM-459 — Run the profile recompute (~5.7 hours)
One pass covering SIM-456, SIM-457 and SIM-458.
**Depends on:** SIM-456, SIM-457, SIM-458.

---

## Phase 3 — Pool and artifact schema, one rebuild

No ETL change and no re-sweep for any ticket in this phase. Every column already exists upstream.

### SIM-460 — Raise the recency floor from 3 seasons to 10
`RECENCY_FLOOR_SEASONS = 3` (`engine_artifacts.py:60`) loads three of the ten swept seasons. Its own
comment gives the reason as the per-draw scan cost — **the exact problem SIM-467 removes**. Raising it
multiplies every filter cell by ~3.3× for free. Also remove the redundancy: a hard cutoff and a smooth
`recency_weight` express the same idea, and the smooth one does it better.
**Depends on:** SIM-467 (measure first, then raise).

> **Data constraint — do not re-propose.** Adding 2015 and 2016 is **not possible**. The MLB API this
> project scrapes does not serve those seasons. 2017 is the earliest available year, so the ten swept
> seasons are the whole dataset. Cell occupancy must come from SIM-460, SIM-461 and coarser bands —
> never from more years.

### SIM-461 — Make batter hand a weight, not a pool partition
Pools are partitioned by `stand` (`engine_artifacts.py:87`), halving every cell. Batter handedness is
already expressed by the batter similarity engine and the platoon relationship. Softening the partition
to a weight roughly doubles every cell.
**Risk:** a left-handed batter can draw a right-handed batter's play. That is precisely what the batter
engine exists to weight against. Validate against SIM-450 bands before keeping.

### SIM-462 — Runner identity columns on `sim.outcome_pool`
Add `on_1b/2b/3b` and `post_on_1b/2b/3b` as player ids from `raw.pitches`. No pool carries runner
identities today — `runners_state` is a 3-bit occupancy mask. These six columns are what make the
transition mapping (SIM-470) possible.

### SIM-463 — Ten pitch-feature columns plus `pitcher_id` into the batted-ball artifact
`_BB_GEOM_COLS` loads three columns (`engine_artifacts.py:116`) — exit velocity, launch angle, spray.
`sim.outcome_pool` holds the ten pitch characteristics and the build query already selects them; the
artifact loads none. So the simulator can draw a 98 mph fastball upstairs and then a batted ball that
really came off an 84 mph changeup. ~55 MB. The pitch pool already carries identical geometry.

### SIM-464 — Home-or-away flag on both pools
`raw.pitches.inning_topbot` holds `'Top'` / `'Bot'` and is ETL-validated
(`etl_historical_loader.py:520`). **Zero pool tables carry it.** One column makes home-field advantage
a filter dimension on both draws, so it emerges across every channel — including umpire bias on called
strikes, which never reaches a batted-ball-only model.

### SIM-465 — Pitch count and times-through-the-order columns
Derive both in the pool build with the window-function pattern the build already uses for previous-pitch
columns (`player_profile_computor.py:4688`). Two columns, one query.

### SIM-466 — Wire the four existing previous-pitch columns into the artifact
`prev_pitch_velo/ivb/hb/outcome` are computed nightly and read **nowhere** in `simulation/`,
`similarity/` or the rest of `pipeline/`. They are the within-inning clustering lever that fatigue alone
cannot supply.

### SIM-467 — Extend the bucket index from 12 cells to the full filter
`new_plate_appearance` already splits weights into **twelve precomputed count buckets** and `draw`
restricts to the live count's bucket (`full_pool_sampler.py:242`, `:249`). The count is already a hard
filter served by a row index. Generalize it to 2,880 cells. Each draw then scans its cell instead of the
pool — roughly a **thousandfold** reduction in per-draw work, and **the answer to the open 30-second
simulation target**, which the notes wrongly call "irreducible". The index is ~1M integers.
**Blocks:** SIM-460, SIM-483.

### SIM-468 — New table `sim.steal_opportunity_pool`
`sim.stolen_base_pool` holds **only attempts**, so a draw over it attempts a steal 100% of the time. It
has no denominator. Build one row per pitch where a steal was possible — a runner on first with second
open, or on second with third open — attempted or not, with an `attempted` flag beside the existing
`success` flag. One draw then answers both "does the runner go" and "safe or caught", and the catcher
finally affects the **decision**, not only the outcome.

### SIM-469 — Run the pool and artifact rebuild
One rebuild covering SIM-462 … SIM-468.
**Depends on:** SIM-459, SIM-462, SIM-463, SIM-464, SIM-465, SIM-466, SIM-467, SIM-468.

---

## Phase 4 — The sampler rewrite

### SIM-470 — Per-decision weight-table framework
Implement the subset rule: each sampling step declares its hard filter and **three to five** weighted
scores in explicit high and low tiers. Recency is a sample weight, not a similarity score, and sits
outside the subset. The situation engine leaves the draw — four of its six columns become the hard
filter, and the residual (inning, within-band score) is too thin to justify a pool pass.

| Sampling step | High | Low | Hard filter |
|---|---|---|---|
| Pitch outcome | pitcher, batter | catcher framing, previous pitch, pitch count + TTO | count, outs, base state, score band, home/away |
| Batted ball | batter, pitch | pitcher, previous pitch | same |
| Advancement on outs | baserunner | batted ball | base-out state |
| Fence resolution | *geometry* | batted ball | park + spray sector |
| Fielding out/hit/error | fielder | batted ball | — |
| Steal attempt + outcome | baserunner steal, catcher | pitcher steal, manager | base state, outs, count, score band |
| Dropped third strike | catcher | pitcher | two strikes, 1B open or two outs |
| Wild pitch, balk, pickoff | pitcher | — | base state |
| Passed ball | catcher | pitcher | base state |

**Depends on:** SIM-469.

### SIM-471 — Pitch-outcome draw
Filter plus five weights per the table. Replaces the current `f_pitcher · f_batter · f_situation`
product.

### SIM-472 — Batted-ball draw with pitch similarity as a primary weight
The pitch draw must first **expose the row it drew** — mirror the `_bb_last_i` pattern at
`full_pool_sampler.py:345`; `:252` currently discards the index. `pitch_pitch_similarity` supplies the
metric, but it is built as a FAISS nearest-neighbour lookup; the full-pool path needs the same metric as
a **vectorized kernel over every row**, the way `f_sit` works at `:322`.
**Depends on:** SIM-463, SIM-471.

### SIM-473 — Advancement draw: the transition mapping
Replace `_full_pool_out_advancement` (`:1507`) entirely. It never reassigns `new_first`, so a runner on
first cannot advance on any out, and the batter is always the out, so a force play and a fielder's
choice cannot happen. Thirty lines hold seven tuned constants plus a global multiplier already judged
the wrong lever. Draw a base-to-base **pattern** from the pool and apply it to the live runners. Combine
per-base baserunner similarities by a **run-value weighted average** using
`derived.run_expectancy_matrix`, so the runner on third dominates and the weight never collapses toward
zero the way a product does.
**Depends on:** SIM-462, SIM-458.

### SIM-474 — Steal draw: attempt and outcome from the opportunity pool
Delete `_full_pool_steal_decision` (`:3091`), the green-light gate (`:2945`) and `_STEAL_ATTEMPT_K`.
Today, production games contain **zero steal attempts** — the manager green light routes control to
`resolver.resolve_steal`, and production wires no resolver, so the base stub answers "no attempt" every
time. Manager aggression becomes a weight on the draw, not a gate in front of it.
**Depends on:** SIM-468.

### SIM-475 — Thin-cell widening and effective-sample-size emission
Set `MIN_CELL` from the SIM-451 measurement. When a cell falls short, relax in a fixed order — score
band, then home-or-away, then count — and log each relaxation. Emit `(Σw)² / Σw²` per draw alongside the
post-filter cell size. **Report only**, no automatic response; the subset rule is what keeps the
effective count from collapsing.
**Depends on:** SIM-451, SIM-470.

### SIM-476 — Weight-temperature backtest harness
Unbuilt work, on no prior ticket. Give each factor in each subset a temperature. Fit the temperatures so
drawn outcomes best match **held-out** real outcomes given the same filter cell. Needs a train/test
split by season, a scoring function per sampling step, and a search over the temperature vector. The
owner's stated ordering — pitch and batter primary, pitcher and previous pitch secondary — is the
starting point, not the answer.
**Depends on:** SIM-470.

### SIM-477 — Fit the weight temperatures
Run SIM-476 across every sampling step and commit the fitted values.
**Depends on:** SIM-476, SIM-471, SIM-472, SIM-473, SIM-474.

---

## Phase 5 — Park geometry

### SIM-478 — `derived.park_geometry` table
~360 rows, hand-curated from public data: per park, the fence distance and height by spray sector. Small
enough to type and verify by hand.

### SIM-479 — Batted-ball trajectory model
**Confirmed by the owner:** the Statcast distance column records where the ball **stopped**, not where
it would have landed. For a ball off a wall or caught at the track that is the wrong number — and those
are exactly the park-sensitive cases. So a trajectory model is required, not optional: estimate carry
distance and height at the fence from exit velocity, launch angle and spray angle. Validate against
home runs, where stop distance and carry distance agree.
**Depends on:** SIM-463 (needs the pitch and batted-ball geometry in the artifact).
**Risk:** the largest technical unknown in this plan.

### SIM-480 — Fence resolution stage
Replace `_apply_park_factor` (`:2168`), which flips outs to singles and therefore leaves **home-run
projections perfectly park-invariant** — while home-run props are the most park-elastic market traded.
Resolve each drawn batted ball against the target park's geometry, and **neutralize the comparison
play's own park before applying the target's**. `derived.park_factors` already computes per-event
factors with left/right splits; the simulator consumes one of nine.
**Depends on:** SIM-478, SIM-479.

---

## Phase 6 — Delete the hand-tuned code, add the missing channels

### SIM-481 — Delete the hand-tuned nudges from `sim_loop.py`
`_apply_home_field_bias` (~43 lines), `_apply_park_factor` (~54), `_apply_sac_fly_bias` (~46),
`_full_pool_out_advancement` (~55), `_tag_rate` (~14), `_fielder_rbf_nudge` (~50),
`_full_pool_steal_decision` (~49), the green-light gate (~15). Also delete `SIM_HOME_FIELD_BIAS`, the
0.025 default and the **never-applied** 0.017 retune — the simulator has run ~50% too much home-field
advantage since the day that overshoot was measured.
**Depends on:** SIM-471 … SIM-474, SIM-480.

### SIM-482 — Manager small-ball as draw weights
`_maybe_sac_bunt` (`:3333`) records a decision whose docstring defers the outcome to work that was never
done. `pitch_out_signalled` (`:2931`) is read nowhere. The hit-and-run only suppresses the steal
initiate. All three reach the play-by-play, so **the front end tells users a bunt was called when
nothing happened.** Delete all three; express manager aggression as a weight.

### SIM-483 — Exclude steal runs from the run-batted-in and earned-run credit
MLB Rule 9.04(b) awards no RBI on a stolen base; `bat.rbi += runs` at `:2590` credits one. Latent today
because nothing stages a steal from third — **SIM-468 makes it live**, since the opportunity pool
carries `base_attempted = 'home'`.
**Depends on:** SIM-474.

### SIM-484 — Wire the dropped third strike through the catcher engine
`_dropped_third_strike` (`:2008`) is gated on an optional `resolver.dropped_third_strike` hook that **no
production resolver implements**, so the edge can never fire.

### SIM-485 — Hit-by-pitch channel
Ships **without** a re-sweep: `raw.pitches.events` already records `hit_by_pitch`; the pool's
`outcome_type` classification drops it. Widen the classification.

### SIM-486 — Retire the per-tile fallback path
Delete `PlayResolver`, the fingerprint stubs, `PlateAppearanceSimulator` and the `SIM_FULL_POOL` flag.
Both reasons for its existence are gone: SIM-467 removes its speed advantage, and its being the **test
default** is exactly why four critical bugs survived eight weeks in production. Tests then run the
production path because it is the only path.
**Depends on:** SIM-467, SIM-450.

---

## Phase 7 — The batched ETL re-sweep

The window for free columns closed on 30 July when the sweep completed. A new ETL column now costs a
full re-sweep, so collect every one before running it.

### SIM-487 — ETL parser changes, batched
Split `passed_ball_wild_pitch` into two columns — a passed ball is the catcher's fault and a wild pitch
is the pitcher's, and they need different engines. Add `balk` and `pickoff` to the event-type loop that
already reads stolen-base events; `pickoff_*` columns exist in the schema and are hard-coded NULL.

### SIM-488 — Run the batched re-sweep (~55 hours)
**Depends on:** SIM-487. Run once, for everything.

### SIM-489 — Wild pitch, passed ball, balk and pickoff channels
Worth ~0.15–0.25 runs per team per game together with the hit-by-pitch channel.
**Depends on:** SIM-488.

---

## Phase 8 — Validation and calibration

### SIM-490 — Wire real per-team manager profiles
The decision model uses a league-flat default. Real per-team profiles were computed and never
connected.

### SIM-491 — Re-validate all six realism flags, one at a time
400 simulations × 20 games each, against a **frozen** calibration, changing one flag per run. All five
were originally enabled together at 3–4 games, the day after that bar was set. At that power the
manager's reported "runs unchanged, −0.10 per team" is statistically consistent with anything from
−0.26 to +0.06.
**Depends on:** SIM-449, SIM-450.

### SIM-492 — Calibration refit and multi-season win-probability curve
One refit at the end, after every fix batch has been measured against the frozen control. The existing
curve was fitted on 60 games of a single season, and on the **pre-fix** run environment. Fit
multi-season over several hundred games.
**Depends on:** everything above.

### SIM-493 — Decompose `sim_loop.py`
Deferred deliberately. The architecture work deletes ~350–400 lines and SIM-486 removes several hundred
more, so decomposing earlier would mean carefully relocating code that these tickets delete. Decompose
once, on the final shape. The manager model (~670 lines, `:2807`–`:3477`) is the largest clean seam.
**Depends on:** SIM-481, SIM-486.

---

## Critical path

```
SIM-449 (harness)
  -> SIM-450 (acceptance bands)
       -> SIM-486 (retire fallback), SIM-491 (flag re-validation)

SIM-456/457/458 -> SIM-459 (profile recompute, 5.7h)
  -> SIM-469 (pool + artifact rebuild)

SIM-462..468 -> SIM-469 -> SIM-470 (weight framework)
  -> SIM-471..474 (the four draws)
       -> SIM-476/477 (temperature fitting)
       -> SIM-481 (delete the tuned code)

SIM-467 (filter index) -> SIM-460 (10 seasons), SIM-486

SIM-478/479 (geometry + trajectory) -> SIM-480 (fence resolution)

SIM-487 -> SIM-488 (re-sweep, 55h) -> SIM-489
```

**Longest chain:** SIM-449 → 459 → 469 → 470 → 471 → 472 → 477 → 481 → 492.

**The two long-running jobs are independent of each other** — the 5.7-hour profile recompute and the
55-hour re-sweep can run in parallel with code work, provided SIM-487 lands before the sweep starts.
