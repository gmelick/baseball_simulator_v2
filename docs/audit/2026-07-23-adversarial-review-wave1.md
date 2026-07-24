# 2026-07-23 — Adversarial review of `wave1-remediation` (4 reviewers, all BLOCK)

**What this is.** Before merging the Wave 1 remediation branch, four independent adversarial reviewers were
tasked with *refuting* its correctness (one per track). Each was told to assume the code is wrong until
they failed to break it, to prove defects with measured numbers rather than inspection, and to modify
nothing. **All four returned BLOCK.** The branch was NOT merged; `master` is unchanged.

Every number below was measured by the reviewer (against the live 6.5M-row Postgres, a real container, or
a Monte-Carlo harness), not estimated. This file is the durable record of that review — the findings live
nowhere else.

**Branch state:** `wave1-remediation`, 7 commits ahead of `master` (`0a52d13`), 37 files, +4515/−304.
Full suite green on the branch (unit 2344, regression 53, e2e 12, integration 24, ruff + mypy clean) —
which is itself a finding: **every defect below is invisible to the entire test suite.**

---

## Track C — simulator (`simulation/sim_loop.py`)

### BLOCKING

**C-B1 — the steal fix restores only ~10% of MLB steal volume.**
`sim_loop.py:3086,3100`. The C2 fallback was placed *after* the green-light Bernoulli gate, but
`_full_pool_steal_decision` is itself already calibrated to standalone MLB volume
(`_STEAL_ATTEMPT_K = 0.38`). Multiplying by `green` makes the rate `green x MLB`. Production ships
`steal_order_rate_per_1b_opp = 0.08` (`production_factory.py:490`), so `green ∈ [0.04, 0.12]`.
Measured over 40,000 identical opportunities: `manager=None` → **0.0764** attempts/opp;
production profile (SIM_MANAGER ON, as in prod) → **0.0080**. Ratio **0.104**.
*The bug the ticket claims to fix is still there, one order of magnitude smaller.*
**Fix:** call the fallback *before* the green draw, or fold `green` into `attempt_p` inside
`_full_pool_steal_decision` rather than using it as an independent gate.

**C-B2 — the fallback silently overrides a resolver's deliberate no-steal decision.**
`sim_loop.py:3088-3100`. `attempted=False` is the documented return for BOTH "nothing wired" and
"the engines decided not to run" (`PlayResolver.resolve_steal`, `:647-655`). The fix cannot distinguish
them, so it hard-codes an override of the decision three of the eleven similarity engines exist to make.
Measured with a resolver deliberately returning `attempted=False` over 300 green-lit spots:
pre-fix **0/300** steals staged, post-fix **103/300**. No test covers this.
**Fix:** sentinel `None` from the stub, or gate on `type(self.resolver) is PlayResolver`.

### NON-BLOCKING (follow-ups)

- **C-N1 — C6 (RE24 provenance) is materially incomplete.** Three callers still mutate bases then commit
  with no override: `_apply_sac_fly_bias` (`:2318` mutates 31 lines *before* the snapshot at `:2457` — a
  placement bug in the fix itself), `_resolve_strikeout` D3K (`:2057`), `_resolve_steal_outcome`
  (`:1891,:1919`). Sac-fly case: reported `re_start` 0.27 vs true 0.96; RE24 value 0.84 vs true 0.15.
  Blast radius is display-only (`snapshots.py:246` → play-by-play), not scoring/props/CLV.
- **C-N2 — C3 makes recorded `re_end` *more* wrong.** `advance_state()` conserves runners
  (`new_on_base = old + reached - runs`) and has no notion of a runner retired on the play, so removing the
  doubled-off runner desyncs `re_end`. Measured across 8 DP shapes: 4/8 consistent pre-fix → **2/8** post-fix.
- **C-N3 — C3 retires the WRONG runner in three shapes.** The `elif b.third is not None: retired = 3`
  branch (`:2359-2371`) fires for: `strikeout_double_play` with R1+R3 (erases R3; a
  strike-em-out-throw-em-out retires the *stealing* runner), `sac_fly_double_play` with R3 (erases R3 AND
  scores no run — strictly worse than pre-fix, on the production path), and a grounded DP with 1B empty
  (retires R3, the highest-value runner, on a play with no force).
- **C-N4 — C5 mis-credits an RBI (and ER) for a run scored on a steal of home.** `:1667` feeds `runs` at
  `:2722` (`bat.rbi += runs`). MLB Rule 9.04(b): no RBI on a stolen base. Latent (nothing stages a steal
  from 3B today) — which also makes C5's stated benefit latent.
- **C-N5 — C4 records RE24 run value of exactly 0.0 for every reach-on-error.** `result_hits=0` makes the
  batter reaching 1B invisible to `advance_state`. True value ≈ +0.38 (pre-fix was −0.24).
- **C-N6 — C4 latent run-loss on the per-tile path** if a caller supplies a sample carrying `result_runs`.
  Not production-reachable today.
- **C-N7 — the `assert_consistent()` guard added at `:2371` is vacuous.** `game_state.py:148-156` only
  rejects negative runner ids; it cannot detect a bad DP state.
- **C-N8 — C7 wires 3 streams, not 4.** `machine.sampler IS machine._pa.sampler`, so child[1] is assigned
  then immediately overwritten by child[2]; child[1] is never consumed. `machine._pa.rng` is never reseeded.
- **C-N9 — doc drift.** `pitcher_decisions.py:14` still claims GameState has no `home/away_pitcher_id`.
- **C-N10 — merge gate (not a defect).** Track C shifts the run environment: synthetic MLB-ish mix,
  300 games, **7.653 → 8.207 R/G (+7.2%)**. Direction helps the documented "runs 10-12% low" gap, but
  `/data/calibration.json` and the win-prob curve were fit on the pre-fix environment.

### Hollow tests found (pass against PRE-fix code)

| test | why hollow |
|---|---|
| `test_sim439_steal_fallback.py:90` | uses `manager=None` → `green == 0` → exercises the pre-existing SIM-426 branch, not C2. Its comment "pre-fix: never" is **false**. |
| `test_sim439_steal_fallback.py:207` | same — `manager=None`. Comment "pre-fix: zero" is **false**. |
| `test_sim439_rng_independence.py:164` | tests numpy's `SeedSequence.spawn` only; touches **zero** project code. |
| `test_sim439_re24_provenance.py:96` | bases-loaded → loaded, so `re_start` is identical with/without the override; cannot distinguish the bug. |
| `test_sim439_reach_on_error.py:162` | hand-builds `PlayResult`s; tests SIM-414 inning reconstruction, not C4. |

**Verified NOT a problem (attacked, held):** C1 `state.defense` correctness in both callers; C1 pull on the
last out / top vs bottom; C1 has no other readers of the repurposed fields (checked `pitcher_decisions.py`,
`api/routes/games.py`, `BatchRunner`, `sim_stats.py`); C2 cannot double-fire; C2 precedence (IBB,
hit-and-run) intact; C3 no double-removal; C4 accounting (AB/H/ER/RBI) correct; C5 cannot double-count
(fresh `PlayResult` per pitch); C7 determinism preserved, no golden references `simulate_game`.
Also verified the two `baseball_analyst` test relaxations are honest (40.0% tie rate on master vs 40.2%
post-fix over 400 seeds) — not masking a regression.

---

## Track D — derived metrics (`pipeline/batch/player_profile_computor.py`)

### BLOCKING

**D-B1 — the handedness fix keys on the WRONG COLUMN, zeroing 6 features for every switch hitter.**
`player_profile_computor.py:1888,1892,1942,1945,1974,1977`. The new signed-spray `CASE` keys on
`bat_hand`, which is the *roster/declared* side and is `'S'` for switch hitters — **not** the per-PA
resolved side (`stand`). Ground truth from `raw.pitches`: `bat_hand='S'` is **10.4-13.3% of rows in every
season 2017-2026**; `stand='S'` is **0.000%**. 183 of 184 switch hitters are 100% `'S'`.
Because the numerator `CASE` returns NULL but the denominator has **no** `bat_hand` filter, the rate is
`0/N` = **exactly 0.0**, not NULL.

Measured on real 2024 data (≥100 BIP):

| `bat_hand` | batters | `pull_rate` NEW | if keyed on `stand` | `oppo_rate` NEW | on `stand` |
|---|---|---|---|---|---|
| L | 145 | 0.4587 | 0.4587 | 0.2610 | 0.2610 |
| R | 221 | 0.4282 | 0.4282 | 0.2807 | 0.2807 |
| **S** | **39** | **0.0000** | **0.4700** | **0.0000** | **0.2477** |

Blast radius: **416 batter-seasons zeroed — 9.2-12.9% of qualified batters, every season**, across 6
columns (`pull_rate`, `oppo_rate`, and the four `_vs_l`/`_vs_r` splits).
Downstream: `pull_rate` (reliability weight 0.760) and `oppo_rate` (0.792) are the two highest-weighted of
the 8 `BATTED_BALL_FEATURES` (`batter_similarity.py:139-140`), a sub-score worth 35% of the batter
composite. Population 2024: pull mean 0.4431/sd 0.0597, oppo 0.2704/sd 0.0510 → a hard 0.0 is
**z = −7.4 and −5.3**. `_v()` does `v or 0.0` so there is no NULL escape; EB shrinkage (α≈0.99) does not
rescue it. Weighted RBF → batted-ball similarity vs a league-average batter collapses to **≈0.004**
(typical 0.4-0.7) while switch hitters become **≈1.0** similar to each other.
**This is a REGRESSION vs master**: the old (wrong-sign) expression returned a plausible 0.4589; the fix
turns a mild error into a −7σ outlier. No CHECK constraint, no NULL sentinel, no test covers `'S'`.
**Fix:** key on `stand`, or reuse the idiom already at line 4645:
`CASE WHEN bat_hand IN ('L','R') THEN bat_hand ELSE stand END`.

### NON-BLOCKING

- **D-N1 — three mutually inconsistent swinging-strike sets now live in one file.** pitcher `whiff_rate`
  (new) = `M,O,S,T,W`; batter `whiff_rate` (`:1861,1927,1959`) = `M,O,S,T` (no `W` — 43,802 real rows,
  +1.41pp); `_build_outcome_pool` (`:4687`) treats `T` as a **foul**. The fix corrected one, left two.
- **D-N2 — `T` (foul tip) in the whiff set is an unrecorded judgment call.** `M,O,S,W`/pitches = **11.10%**
  (matches MLB SwStr% ~11.0-11.5%); with `T` = **12.06%** (above it). Conversely CSW with `T` = **28.54%**
  (matches MLB ~28.5%). The two anchors disagree; the commit picked csw-consistency silently. Also the
  pitcher metric is now per-pitch (SwStr%) while the batter one is per-swing (Whiff%) — same column name,
  different statistic.
- **D-N3 — the identical inversion D-3 fixes is left in place two lines away.** `z_swing_rate`
  (`:1857,1923,1955`) uses the take predicate → computes Z-**take** (**32.50%** measured vs a true
  Z-Swing% of **67.50%**). `zone_take_rate` (`:1621`) counts only `type='C'` → 28.97% vs a true 32.50%.
  Neither is in the diff.
- **D-N4 — D-M1 (RE matrix) is real but ~50x smaller than the test implies.** Over 39,543 half-innings:
  only **141 (0.36%)** affected; mean **0.0045** runs/half-inning recovered; RE states move +0.003 to
  +0.021 (0.8-4.1%). New values track canonical MLB RE24 *better*. The test's `assert buggy == 0.0` is a
  synthetic dramatization that would mislead anyone sizing the fix.
- **D-N5 — D-5 (RBI) has zero read consumers and is inert until a full re-ingest.** `rbis_on_pitch` is
  written and declared but **no query/engine/sim path reads it**; current production
  `SUM(rbis_on_pitch) = 0` over all 6.55M rows. Correct fix, delivers nothing until ~21.6k games re-ingest.
- **D-N6 — recalibration dependency.** `/data/calibration.json` sigmas were fit on the pre-fix
  distributions of `first_pitch_take_rate`, `whiff_rate`, `pull_rate`, `oppo_rate`, `gb_rate`.
- **D-N7 — new collinearity.** `COMMAND_FEATURES` holds both `csw_rate` and `whiff_rate`; post-fix
  `csw_rate ≡ called_strike_rate + whiff_rate` exactly, double-weighting the whiff component.
- **D-N8 — the test file is largely hollow.** 5 of 7 tests execute **copy-pasted SQL string literals**
  never read from the computor — reverting the production query leaves them green. The copies have
  **already drifted** at character level (production `type IN ('D', 'E', 'X')` vs test `('D','E','X')`).
  Only D-M1 drives real production code. The D-5 test is self-fulfilling by construction.

**Verified NOT a problem (attacked, held):** D-1 bounds/double-count (all 5 codes exist, disjoint, cannot
exceed 1.0; old value 16.48% was the called-strike rate — unambiguously wrong). D-2 numerator leakage
(`bb_type` is non-NULL *only* on `D/E/X`; zero rates >1.0 across 448 pitchers; league GB% 65.59% →
**42.49%**, MLB ≈ 42.6%). D-2 vs xFIP (uses a count, not a rate). D-3 complement exactness (take+swing =
1.0000; league first-pitch take **69.42%**). **D-4's SIGN CONVENTION IS PROVEN CORRECT** — settled
empirically via `hit_location`: mean `spray_angle` is −35.24 at 3B, −30.26 at LF, +0.57 at CF, +31.19 at
RF, +42.84 at 1B → negative = left field, so `R:+spray, L:−spray` makes pull negative. The new code is
right and the `_build_outcome_pool` comment ("positive = pull") is **wrong**. D-4 platoon symmetry
(`p_throws` applied consistently in all 22 expressions). D-M1 double-count (walked the ETL: scores are
pre-pitch, `runs_on_pitch` is that pitch's runs — no double-add) and `MAX` safety (**0 of 39,543**
half-innings differ from the last-PA value). **D-5 verified CORRECT against the live MLB StatsAPI**:
runner top-level keys are `[credits, details, movement]` (no `rbi`); `details` contains `rbi` as a bool.
Corroborated in production: `SUM(earned_runs_on_pitch)` = 92.2% of runs (MLB ≈ 92%), proving the sibling
`details.earned` lookup works.

---

## Track E — model / calibration

### BLOCKING

**E-B1 — the new isotonic reliability curve emits exactly 1.0/0.0 and flat-extrapolates both tails.**
`prop_validation.py:324-326` (endpoint anchoring) + `:754` (the shipped curve switched to this fitter).
The isotonic path anchors at `[0.0, uy[0]]` / `[1.0, uy[-1]]` — the *fitted end-block values* — instead of
`[0,0]`/`[1,1]`. Terminal PAVA blocks are frequently a single observation, so those values are hard 0.0/1.0.
The `min_bin_count` 1→2 hardening protects only `fit_reliability_curve`, which is **no longer the default**.

Measured, `P(map(0.90) == 1.0)`, 200 trials/row:

| n games | NEW | OLD (master) |
|---|---|---|
| 60 | **0.620** | 0.020 |
| 120 | **0.610** | 0.000 |
| 400 | **0.615** | 0.000 |
| 2378 (full 2024) | **0.640** | 0.000 |

It does not wash out with sample size. End-to-end: an ordinary 83-of-100 sim (`p_home = 0.8267`) maps to
**1.0/0.0**; `prob_to_american` (`clv_engine.py:156`) rejects 0/1; the error is *caught* by
`betting.py:_safe_report` and `clv_backtest.py:990` → **the moneyline market silently disappears** from
the betting card and the CLV scoreboard for every lopsided game.
Where it stops short of degenerate it is worse because it is silent: seed 0, n=60 → `p=0.95` maps to
**0.4444** (a 95%-favourite priced at 44%; the model bets the dog). This curve is what
`write_reliability_curve_to_calibration_report` (`:762`) writes into `/data/calibration.json`, loaded at
boot (`api/main.py:236`) and per CLV worker (`clv_backtest.py:1154`). Full live path.
**Fix:** clamp fitted `y` into `[eps, 1-eps]`, and/or restore `[0,0]`/`[1,1]` anchors, and/or require a
minimum count in terminal isotonic blocks.

**E-B2 — +22% per plate appearance on the production sim hot path, for two additions with zero consumers.**
`full_pool_sampler.py:356-359` (ESS) and `:233-236` (`_mean_fill`). Benchmarked branch vs a scratchpad
subclass restoring master's bodies, 500K-row pool: **22.59 → 27.54 ms/PA (+4.95, +22%)**. Scaled to the
production ~935K pool: **+9.26 ms/PA → +0.77 s per iteration** at ~83 PA — a **40-50% per-iteration
regression** against the documented 1.5-1.9 s. n=100 `/simulate` ≈ 38 s becomes ≈ 55-65 s. (SIM-430/436
spent an epic getting 215 s → 38 s and the 30 s SLA is still missed.)
Both costs are avoidable: the ESS block upcasts the whole ~935K weight vector to float64 **every PA**
(6.4 ms; a float32-native `sum²/dot` is 0.35 ms — 18x cheaper) and is **unconditional** (the
`isEnabledFor(DEBUG)` guard covers only the log line). `_last_ess` is read by nothing outside tests;
`ess_temper` is never set by any production path. `_mean_fill` does a boolean-compress copy plus a
redundant second `.astype(float32)`; the mask and count are pool-constant and already cached in
`_pool_meta`.

**E-B3 — four sampler-weight changes land at once with no run-environment validation and no golden gate.**
E-1, E-CAL-ARSENAL, E-MISSING-1.0, E-ZFILL all move `FullPoolSampler`'s per-PA weight vector. Regression
fixtures cover only `baserunner_steal/catcher/manager/pitcher_steal/situation` — **no golden coverage of
the pitcher engine, batter engine, or the full-pool sampler**. CLAUDE.md §11 requires a multi-game ×
≥400-sim batch before reading R-level moves; none was run. Compounding: E-MISSING-1.0 is live on merge
while E-ZFILL/E-1/E-CAL-ARSENAL bite only on the next `engine_artifacts` rebuild — production passes
through a third, never-validated hybrid state.

### NON-BLOCKING

- **E-N1 — E-EB is a no-op on the only production boot path.** `api/main.py` builds engines *then* calls
  `apply_calibration_to_engines` (`:236`), but `_apply_shrinkage()` runs inside `build()`
  (`batter_similarity.py:770`) and `eb_alpha` is baked at load (`:955`). Setting `self._shrinkage`
  afterwards changes nothing. The gap moved down one level rather than closing.
- **E-N2 — the E-CAL-BATTER `weights` seam is unreachable.** `EngineArtifacts.load`
  (`engine_artifacts.py:821-837`) builds `actor_emb[actor]` with a fixed key set that never includes
  `weights`, so `_batter_vecs_z`'s `bemb.get("weights")` can never fire. A future implementer would ship a
  silent no-op.
- **E-N3 — E-CAL-ARSENAL adds an undocumented nightly ordering dependency** (`make calibrate` must precede
  the `engine_artifacts --what pitcher_sim` build, which has no Makefile target). `apply_calibration` runs
  before any W2 cache exists, so the `finite_distances()` median fallback is dead.
- **E-N4 — `winprob_oos_ece` returns `nan`** and `to_json` emits the non-standard `NaN` literal (breaks
  `JSON.parse`, `jq`, DuckDB `read_json`). It also uses one fixed split (seed 407, 30%) — a high-variance
  point estimate describing a *train-fold* curve while the **shipped** curve is fit on all data.
- **E-N5 — E-ZFILL contradicts the engines' own missing-data convention.** `RBFSimilarity.score` does
  `np.nan_to_num(diff, nan=0.0)` — it *masks* the dimension; mean-fill instead penalizes a
  missing-feature candidate whenever the query sits far from that column's mean. Both defensible; two
  conventions in one codebase is not. Also `np.nanmean` on an all-NaN column warns on every nightly build.
- **E-N6 — test hollowness.** `test_isotonic_reliability_curve_is_monotone_and_nonempty` clips `pred` so
  **12.5% of samples sit at exactly 0.0 and 12.1% at 1.0** — the curve spans the full domain and E-B1's
  failure mode is *structurally impossible* in the test. That is why ~2400 tests are green over a
  live-money defect. `test_redistribution_factor_is_unity` asserts `x = 1.0; assertAlmostEqual(x, 1.0)` —
  a tautology over a local literal touching no engine code.

### E-1 — RESOLVED: it is a RESTORATION, not a revert. SIM-346 was itself the regression.

The reviewer found the pre-SIM-346 source (commit `e21cb24`, weights 0.60/0.30/0.10):
```python
remaining = WEIGHT_COMMAND + WEIGHT_RESULTS
composite[mask] = (WEIGHT_COMMAND/remaining)*command + (WEIGHT_RESULTS/remaining)*results
```
That is a correct convex renormalization over **two** survivors. SIM-067 deleted the results sub-score;
the surviving `(WEIGHT_COMMAND/WEIGHT_COMMAND)*command == 1.0*command` was the **correct degenerate case**,
not a leftover no-op. **SIM-346's stated premise — "that left the composite at `WEIGHT_COMMAND*command`
(max 0.35)" — is factually false about the code it replaced** (it multiplied by 1.0, not 0.35). SIM-346
applied the right ratio to the wrong base.

Measured (200k Monte-Carlo pairs, shipped `RBF_SIGMA_COMMAND = 1.0453`, 7 command features):

| | mean | median | sd | frac clipped at 1.0 |
|---|---|---|---|---|
| command sub-score | 0.4435 | 0.4363 | 0.181 | 0.000 |
| FULL composite | 0.4726 | 0.4777 | 0.130 | 0.000 |
| **OLD no-arsenal (2.857x)** | 0.8996 | **1.0000** | 0.187 | **0.669** |
| **NEW no-arsenal (1.0x)** | 0.4435 | 0.4363 | 0.181 | 0.000 |
| ALT median-impute | 0.4802 | 0.4777 | 0.063 | 0.000 |

Under SIM-346, **67% of GMM-less pairs clipped to exactly 1.0**; `P(a GMM-less candidate outranks a
full-arsenal candidate)` = **0.936**; GMM-less candidates occupied **100% of the top 1%** of a mixed
ranking. SIM-346 did not fix under-scoring — it created total domination.

**Verdict: keep 1.0. No modeling sign-off is needed for the arithmetic** (it is provably the convex
renorm, provably restores the original design, and 2.857 is provably broken). **But merging without the
≥400-sim validation batch is the unsafe part.** Residual for the domain owner: with one sub-score instead
of two the variance is higher (sd 0.181 vs 0.130), so GMM-less candidates are still **~4.5x
over-represented in the top 1%**. Median-imputing the arsenal sub-score removes that entirely (0.0%) and
is *philosophically identical to what this same change set chose for E-ZFILL and E-MISSING-1.0* — Track E
mean-imputes every other missing quantity and drop-renormalizes this one. **That inconsistency is the real
open modeling question**, not the 2.857-vs-1.0 arithmetic.

**Verified NOT a problem (attacked, held):** the `ARSENAL_SCALE` global→instance threading is COMPLETE (no
path outside comments reads the global); the class-level default does not mask a missing
`apply_calibration` (absent report logs WARNING + stamps `calibration_id="default:no-report"`);
calibrating after `build()` is fine (scale read at query time; W2 is scale-independent);
`CALIBRATION_REPORT_PATH` IS on the app service so E-CAL-ARSENAL is reachable; the extra npz key doesn't
break `load` or the shared-memory publish; **E-ESS is byte-neutral to the draw**; PAVA is correct
(exact block means, both directions, idempotent, preserves [0,1], legacy back-compat holds); E-ZFILL
all-NaN column is safe; the OOS split is genuinely disjoint; `_mean_fill`'s "profiled" definition is right
(opposite-hand pool pitchers legitimately score 0.0 and belong in the mean).

---

## Track B — CLV instrument

### BLOCKING

**B-B1 — tail smoothing converts *skipped* markets into *placed* bets at near-maximal fake edges.**
`prop_distributions.py:131,172-173`. The Poisson tail uses **λ = the sample mean**, so when the sim never
observed the event λ→0 and the "floor" is meaningless — but nonzero, which is exactly enough to escape the
degenerate guard that used to skip the market.
Measured (100 iterations, batter with zero simulated HRs, real market +400/−550):

| | old (compact PMF) | new (`tail_smoothing=True`) |
|---|---|---|
| `p_over(0.5)` | 0.0 | **1.9999999990e-11** |
| `prop_edge_report` | `ValueError` → **SKIPPED** | **PLACED**, `edge=+0.1912`, `ev=+0.1818` |
| MC gate | n/a | SE = 5e-7 → **clears trivially** |

`+0.1912` is *exactly* `1 − fair_under` — the maximum edge the market allows — in the region where the
simulator has **zero information**. Same for a K line above the sampled max (`edge = +0.1370`). The stated
intent ("a possible-but-unsampled value should not price to a hard 0.0") is **not achieved**; 2e-11 is not
a floor. All that changed is that the safety guard was bypassed.

**B-B2 — the MC gate is anti-correlated with information: it rejects honest edges and admits phantom ones.**
`clv_backtest.py:167,180`, wired with `n_iter = --iterations` (default **100**).
Floor = `max(min_edge, 2*sqrt(p(1-p)/n))`:

| sim prob p | floor @ n=100 | @ n=65 |
|---|---|---|
| 0.50 | **0.1000** | **0.1240** |
| 0.20 | 0.0800 | — |
| 0.01 / 0.9996 | 0.0200 (min_edge) | 0.0200 |

A genuine **5pp moneyline edge at p=0.55 is REJECTED**; the **phantom 19.1pp tail-prop edge from B-B1 at
p≈1.0 is ACCEPTED**. The placed-bet population is systematically purged of information-rich moderate edges
and refilled with the least-informed extreme ones. Also `p` is the *model's* MC estimate, so the gate
conditions on the upper tail of sampling noise (winner's curse). Also the help text at `:1604` is wrong:
`--min-edge 0.0` does **not** restore the every-side view (2*SE still applies).

**B-B3 — `mean_clv_prob` includes the very line-moved bets SIM-449 declares meaningless.**
`clv_backtest.py:649`. The *rate* correctly excludes moved lines; the *economic* metric does not.
Proof — 10 honest bets with true mean CLV 0.0 + 10 moved-line bets carrying a +0.20 cross-line artefact:
`beat_close_rate = 0.5` (correct) but `mean_clv_prob = 0.100` — **+10 CLV points of edge where the honest
answer is zero**. `mean_clv_prob_se` inherits the contamination.

**B-B4 — the printed 95% CI ignores the clustering it computes (~3x too narrow); power floor ~4x too low.**
`clv_backtest.py:164,220,665,690`. `_clustered_se` is **correct** CR0 math, but `_row_for:665` builds the
printed CI from the **iid** `_wilson_interval` — 100 games × 10 perfectly correlated bets: printed
half-width **0.0309** vs honest clustered **0.0980** (ratio **0.32x**). Props within a game share the same
boxscores, so real backtests are heavily clustered — a 55% rate over ~20k prop bets would print a CI
excluding 50% when the clustered CI would not (**a manufactured "we have an edge"**).
`POWER_FLOOR_BETS = 1225` claims 95% power for a ~2pp edge. Correct n vs p=0.5, α=0.05 two-sided:
**80% → 4,905; 90% → 6,567; 95% → 8,122**. 1,225 gives 80% power only for a **4pp** edge (~22% power for
2pp), and `underpowered` uses *nominal* n, not effective n.

### NON-BLOCKING

- **B-N1 — push-aware EV moves zero numbers on the scoreboard.** `_pick_side` gates on `.edge` and
  `_row_for` never reads `model_ev`. Worse, `edge` is still not push-aware: a perfectly calibrated sim on
  an integer total of 9 reports **edge = −0.0450 on BOTH sides** → whole-number totals are systematically
  never placed. The push fix is half done, on the half that doesn't gate.
- **B-N2 — Kelly does not over-size** (still charges the push as a loss) but a signal can clear the
  `ev > min_ev` gate with a **0.0** stake. Inconsistent, conservative.
- **B-N3 — migration 0016 adds two dead columns.** Chain correct (0016→0015, single head, no SIM-438
  conflict), additive/nullable/idempotent, downgrade reverses. But `_persist_odds`/`_persist_prop_odds`
  have unchanged column lists and the CLV readers don't select them — **SIM-448's DB-side closing-line
  time axis is not delivered.**
- **B-N4 — `get_odds` records the wrong market's provenance.** The `_stamp` closure
  (`bettingpros_odds_provider.py:430-435`) overwrites `updated`/`book_id` across moneyline → runline →
  total, so a `market_type='moneyline'` row would carry the *total's* stamp. Latent; defeats the
  migration's "pin one book for both legs" purpose.
- **B-N5 — SIM-448 does NOT repair the existing poisoned backfill.** `load_historical_odds.py:172` skips
  persisting an empty quote, so a game the new guard rejects leaves the previously-persisted wrong-game row
  as the latest row the backtest reads. No purge ships. **Merging this does not make the 2024 odds
  trustworthy.**
- **B-N6 — unverified UTC assumption in the ~2h guard.** `_parse_iso` treats a tz-naive BettingPros
  `scheduled` as UTC. If it is US/Eastern-local, every candidate lands 4-5h off → the guard rejects 100% of
  events → a re-backfill silently writes zero odds. The tests bake the assumption in. **Check one real
  payload before the backfill.**
- **B-N7 — 2h guard false-negatives** on suspended/resumed and rescheduled games (MLB keeps the original
  `gameDate`; the book re-posts at the new time). Silently shrinks/biases the slate with only a warning.
- **B-N8 — the `line_movement.py` mirror re-introduces "ties-as-losses" on the API surface.**
  `LineMovementModel` doesn't expose `line_moved`; `GET /clv` filters `m.clv is not None`, so moved-line
  markets silently **vanish** from the user-facing snapshot — indistinguishable from a real loss.
- **B-N9 — prop-model parity break.** The backtest builds `tail_smoothing=True` (`clv_backtest.py:1246`)
  while production (`api/routes/games.py:1701`) and `validate_props.py:227` build the **unsmoothed** PMF.
  SIM-447 fixed win-prob parity; SIM-450 broke prop parity in the same change set. **Any prop edge the
  backtest reports is not achievable from the numbers the API serves.**
- **B-N10 — the new headline is not comparable to the historical ~49% read** (default `--min-edge`
  0.0→0.02, new slate sampler, new denominator). Holding beat-count fixed, 49% becomes **54.4%** at a 10%
  push share, **61.2%** at 20%, **70.0%** at 30% — with the null still 50%.
- **B-N11 — `edge_significant` uses `>` while the gate uses `>=`** (`:559` vs `:180`).
- **B-N12 — `beat_close_rate` returns 0.0 when `n_decisive == 0`**, printing "0.0%" —
  indistinguishable from a genuine 0% beat rate.
- **B-N13 — test hollowness.** `test_odds_readers_exclude_mock_lines` asserts via `inspect.getsource` that
  the literal `"is_mock = FALSE"` appears — pins a string, not behaviour; passes on a broken query, a
  filter in the wrong clause, or inverted semantics. **No test puts >1 bet in the same `game_pk`**, so
  `_clustered_se` is never exercised through `_row_for` and B-B4 is invisible.

### Could these manufacture a fake edge, or hide a real one? BOTH, simultaneously.

- **Manufacture:** B-B1 creates 13-19pp "edges" where the sim has zero information; B-B3 reports +10 CLV
  points where the honest answer is 0; B-B4 prints a CI ~3x too narrow and stamps "ok" at ~22% power.
  The denominator change alone lifts a 49% no-edge read to 61% at a 20% push share.
- **Hide:** B-B2's 10.0pp floor rejects every honest 2-8pp edge; B-N1 excludes whole-number totals
  entirely; B-N5 leaves the corrupt odds in place; B-N9 means a measured prop edge isn't the one
  production serves.

**Verified NOT a problem (attacked, held):** Wilson interval matches published values exactly
((60,120)→(0.4119387, 0.5880613)); `_clustered_se` CR0 math correct (reduces to iid for singletons, widens
to the true SE under perfect correlation) — the bug is that nothing *uses* it; push tolerance `1e-9` is
safe (smallest real non-zero CLV ≈ 2e-4, five orders larger); **excluding pushes from the denominator is
the RIGHT statistical choice** (sign-test convention; 0.5 remains the null) and `mean_clv_prob` correctly
includes pushes; push-aware EV arithmetic exact (`new − old == p_push` to 1e-12; integer-total EV
−0.041364 = the true −110 vig); tail-smoothed PMF sums to 1.0 and `p_over+p_under+p_push == 1` at every
integer and half-integer line across 5 prop shapes (**0 violations**); **central-mass distortion is
defensible** (worst `p_over` shift at a real book line **0.26pp**, typical 0.00-0.13pp, TVD 0.0001-0.0043);
`is_mock` is `BOOLEAN NOT NULL DEFAULT TRUE` so no NULL hazard; `officialDate` is the correct field with a
safe fallback and the DH "nearest scheduled" picks the nightcap correctly.

---

## Cross-cutting conclusion

1. **The full test suite (2344 unit + 53 regression + 12 e2e + 24 integration, all green) detects none of
   the above.** Several new tests are structurally incapable of failing against the bug they name.
2. **Two "fixes" are regressions vs master** (D-B1 zeroes 6 features for 10-13% of batters; E-B1 deletes
   the moneyline market and can invert a 95% favourite).
3. **Two fixes are ~90% incomplete** (C-B1 restores 10% of steal volume; B-N1's push fix touches the half
   that doesn't gate).
4. **The measurement layer can now both manufacture and hide an edge simultaneously** — the exact failure
   mode Wave 1 existed to eliminate.
5. **E-1 is vindicated** and needs no sign-off for its arithmetic; the open modeling question is the
   *inconsistency* between drop-renormalize (E-1) and mean-impute (E-ZFILL/E-MISSING-1.0).
