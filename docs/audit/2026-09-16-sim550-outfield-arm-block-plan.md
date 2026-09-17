# Build plan — the outfield arm block, rebuilt from our own advancement pool (SIM-550)

> **STATUS 2026-09-17 — BUILT, REVIEWED AND RUN.** The code landed, a 61-agent adversarial
> review confirmed twelve findings (all fixed — the expectation cell gained the POSITION;
> the fill resets the block before it writes; the reliability refit pairs consecutive
> seasons at one position with 50 chances), and the run book's steps 2 to 5 ran the same
> day: the fill on every season (verify: every check passed; the Savant fielder-view
> cross-check r = 0.980 on chances, 0.782 on the thrown-out rate), `make calibrate`
> (sigma_of_arm 0.9912; weights velocity 0.844 / prevention 0.137 / thrown-out 0.240,
> copied into the module defaults), the three outfield matrices (concentration PASS), the
> app restarted at 11/11 engines. **A correction to §2.2:** the prevention's repeat of
> 0.50–0.65 pooled the three positions under a position-blind cell and was mostly the
> position label; WITHIN a position it repeats at 0.1–0.3 (LF about 0, CF about 0.2, RF
> 0.2–0.5), the hold rate lower still, the thrown-out rate 0.1–0.3, the velocity 0.85–0.90.
> The refit says the same. The arm group is the velocity with two weak companions; the
> group's 0.30 share is the sweep's question. The record is `CHANGES.md` 2026-09-17.
> *(The earlier stamp follows.)* The build follows
> this plan as written, under two owner rulings of 2026-09-16 that supersede it where they
> touch it: (a) **no per-change accuracy run** — every change lands ON at its best-known
> default and the weights are fitted together in one designed experiment later, so §8
> steps 0 and 6c (the OFF and ON accuracy arms and the paired read) do not run and nothing
> was built for them; (b) **every similarity-score power is 1 in production**, so §6's
> "power 1.2 on the fielder factor" reads 1 and no power moves. The lead-distance work (SIM-531) landed and ran on
> 2026-09-16 (commit c4a6a76), as this plan required. *Approved 2026-09-16: the owner took
> all four recommendations (§10). The readable page is
> https://claude.ai/artifact/Up1J2fALc1Ea5DJik22Qza (the plan as approved; this stamp is
> newer). Revised 2026-09-16: the owner found that Savant's baserunning board does have a
> fielder view (`type=Fld`); §2.5 measures it against the pool, and decision 4 settles the
> source: the pool.*

**Date:** 2026-09-16
**Ticket:** SIM-550 (P2) in `BACKLOG.xlsx`. Next free ID: SIM-551.
**Evidence:** two read-only probes of the live DuckDB on 2026-09-16
(`sim.advancement_opportunity_pool`, `derived.fielder_season_metrics`,
`derived.league_averages`); Savant's baserunning board pulled the same day in both its
views for 2017, 2019 and 2023 to 2026; the code in the working tree (the SIM-531 build in
progress).
**Builds on:** `docs/audit/2026-09-16-sim531-lead-distance-build-plan.md` (Finding 1),
`docs/audit/2026-09-10-sim528-530-savant-build-plan.md` (§4, the join this plan replaces),
`docs/audit/2026-09-10-savant-point-in-time-data.md`.

---

## 0. The short version

**The defect.** The fielding model compares outfielders partly on their arm: whether
runners hold rather than take the extra base against them, how often they are thrown
out, how much runners challenge them below expectation, and a run value. Since
2026-09-11 those figures have come from Savant's baserunning board, which our loader
pulled in its default view: the RUNNER's side. That view is correct for what it is; the
error was our parameter. Its rows describe the runner (catchers and a designated hitter
who never fielded carry 60 to 160 extra-base chances on it), and on a runner's row the
fielder columns describe the fielders on his plays. Our join wrote those figures into the
outfielder's arm block, so the block holds how he runs the bases. All four arm features
the model reads are filled from the wrong view. Only the throw velocity, from the
arm-strength board, was pulled as intended, and the model does not read it.

**The data was never wrong: the board has a fielder view.** Passing `type=Fld` (the spelling `type=fielder`
is silently ignored, which is how the September audit missed it) returns the fielder's
side: about 400 outfielders a season since 2017, with the chances against each, the
attempts, the runners thrown out, a play-level expectation and a real arm run value.

**The fix, and why the pool still wins.** Our advancement opportunity pool already holds
one row per real chance to take an extra base, with the fielder who fielded the ball, his
position, whether the runner went and whether he was safe: every season since 2017, about
42,000 outfield-fielded chances a season, the fielder known on 99% of them. Scored on the
same single-position regulars, the pool's situation-adjusted hold repeats year to year as
well as or better than Savant's fielder view (0.62 to 0.66 against 0.61 to 0.66, then 0.50
to 0.55 against 0.26 to 0.47). The pool keeps positions apart, which matters because 73 of
the 127 regulars in 2024 played more than one outfield position; Savant's figure is per
player. The pool is point-in-time exact by game date; the board needs the season
substitute. And the two sources agree on who has chances (r = 0.98) and on the thrown-out
rate (r = 0.78), so the fielder view becomes the cross-check in the verify block.

**Why the three features.** On the pool, per position, for outfielders with 100 or more
chances: prevention repeats at 0.64 to 0.65; the thrown-out rate at 0.08 to 0.34 (thin:
about five assists a season); the throw velocity at 0.86. The raw hold rate is the same
signal as prevention (r = 0.90), so one goes. The run value stays out of the model: it is
an outcome summary, and on the board's fielder view it repeats at 0.38 to 0.49.

**What it costs.** No schema change. One new builder step and one dead step deleted; the
model's arm group re-listed with its own sample-size confidence; the calibration fit
mirrored; a minutes-long recompute; the three outfield matrices rebuilt; the paired
accuracy comparison as the grade.

**Decisions taken.** The owner approved all four recommendations on 2026-09-16 (§10): the
throw velocity joins the arm group; the run value and the raw hold rate leave it; the dead
extraction is deleted; the pool is the source. The build can start with the run book in §8.

---

## 1. The mechanism

```
TODAY (wrong)
raw.savant_baserunning, the RUNNER view ┄┄► derived.fielder_season_metrics arm block
                                         ┄┄► fielder model, arm group 0.30 ┄┄► fielder_LF/CF/RF matrices
                                         ┄┄► fielding draw ^1.2 · advancement draw ^1.2

THIS PLAN
sim.outcome_pool ──► sim.advancement_opportunity_pool (one row per chance; fielder_id, fielder_pos, attempted, safe)
   ──_fill_outfield_arm_block (new step, after the pools; game_date <= cutoff)──► the arm block, per fielder × position × season
raw.savant_arm_strength ──► arm_strength (unchanged; now READ by the model)
   ──► fielder model: arm group = velocity 0.86 · prevention 0.60 · thrown-out 0.25, shrunk by the arm's OWN chances
   ──► fielder_LF / fielder_CF / fielder_RF matrices ──► fielding draw ^1.2 · advancement draw ^1.2 (unchanged)
Savant's FIELDER view (type=Fld) ┄┄► the verify block's cross-check (chances r >= 0.95); not the source (decision 4, taken)
```

The draws do not change. A backtest cutoff filters the pool by `game_date`, so the arm
block is point-in-time exact; the season-shift substitute and the run-value guard the
Savant join needed both go away.

---

## 2. The evidence

### 2.1 The pool has what the board's runner view cannot give

| | |
|---|---|
| Seasons | 2017 to 2026, all ten (48,000 to 51,000 rows a season; 18,000 in 2020) |
| Rows fielded by an outfielder | about 42,000 a season (84%); the fielder unknown on under 1% |
| Per outfielder-position-season | median 25 chances, upper quartile 81, top decile 204; about 220 outfielder-seasons a season have 50 or more, about 70 have 100 or more |
| The eight decisions (2024, attempt rate / thrown-out share of attempts) | first to third on a single 0.378 / 0.014 · second to home on a single 0.689 / 0.021 · first to home on a double 0.403 / 0.049 · tag from first 0.052 / 0.202 · tag second to third 0.357 / 0.033 · tag third to home 0.816 / 0.027 · the batter's stretch on a single 0.023 / 0.387 · on a double 0.012 / 0.320 |
| League, chances-weighted (2024) | runners hold on 0.795 of chances; 0.049 of attempts are thrown out |

The fielder is the one who fielded the ball (`fielded_by` matched against the nine
position slots on the pitch row), so a center fielder's block is his center-field chances,
and a player who works both corners gets one block per corner.

### 2.2 Which features repeat, on the pool (per position)

Year-to-year correlation of an outfielder's value, by the minimum chances he had in both
seasons (2023→24 / 2024→25).

| Feature | ≥50 chances | ≥100 chances | Verdict |
|---|---|---|---|
| Prevention (expected attempts − attempts, per chance; expectation = the pool's rate for the same season × decision × outs) | 0.505 / 0.546 (n≈123) | 0.641 / 0.650 (n≈70) | **keep**, weight 0.60 |
| Hold rate (1 − attempts / chances) | 0.342 / 0.416 | 0.469 / 0.614 | store only: r = 0.90 with prevention |
| Thrown-out rate (thrown out / attempts) | 0.243 / 0.254 | 0.082 / 0.336 | **keep**, weight 0.25; nearly independent of prevention (r = 0.21) |
| Throw velocity (Savant arm-strength board, per position) | 0.857 (measured 2026-09-10) | — | **keep**, weight 0.86; near-independent of prevention (r = −0.14) |

The baseline behind prevention barely matters (0.49 to 0.55 at 50 chances whether it is
per decision, per season × decision, or per season × decision × outs); the finest cell is
chosen because it is the situationally exact one and it reads best at 50 chances.

### 2.3 The current block, measured

On the live table the block is filled on about 280 of 600 outfielder rows a season for
2023 to 2026 and on none before 2023. Its average "chances" are 93, its hold rate 0.62 and
its thrown-out rate 0.026: the player's own running figures (the true league values
against an outfield arm are 0.80 and 0.049). One example, the 2024 center fielder with the
most chances: the pool gives 505 chances, 120 attempts, 5 thrown out (hold 0.762,
prevention −0.011); the current block says 123, hold 0.610, thrown-out 0.000.

### 2.4 The model, as it stands

- `OF_ARM_FEATURES` reads hold rate, thrown-out rate, prevention and the run value at
  0.5 each, in a group worth 0.30 of an outfielder's score. The throw velocity is stored
  and unread.
- The kernel maps a missing feature to zero distance, so two outfielders with no block
  score a perfect arm match.
- Shrinkage toward the position's league mean uses the profile's batted-ball sample
  (600+ for a regular), never the arm's chances, and the league row carries only
  `arm_hold_rate` (today the wrong value); the other three arm keys are absent, so a thin
  profile is pulled toward 0.0 on them. A 25-chance arm is read at face value.
- `_compute_outfield_arm_metrics` builds `_tmp_of_arm` from every outfield-fielded ball
  in play with a runner on, and nothing reads it. Dead code since the schema
  reconciliation.
- In the recompute the pools are built after the fielder aggregation, so a fill that
  reads the pool must run as its own step after the pools.

### 2.5 Savant's fielder view, measured against the pool

The owner's URL of 2026-09-16 carried `type=Fld`. On the CSV endpoint that parameter is
honoured (398 to 412 outfielders a season at `n=1`, about 184 with 50 or more chances,
back to 2017 at least), `type=Run` returns the runner view the loader has been storing,
and the absent `type` defaults to `Run`. In the fielder view the catchers and the
designated hitter are gone and Tatis appears with 223 chances against him, 80 attempts, 5
thrown out and +2.61 arm runs.

**Agreement between the sources, 2024, fielders with 50 or more chances in both (n = 178):**

| Figure | r | Savant mean | Pool mean | Why they differ |
|---|---|---|---|---|
| chances against | 0.98 | 189 | 222 | the pool counts the batter's stretch and the tag from first, which Savant's opportunity definition leaves out |
| attempt rate against | 0.73 | 0.360 | 0.206 | the same narrower denominator |
| thrown-out rate | 0.78 | 0.026 | 0.049 | the batter thrown out stretching is in the pool's numerator |
| prevention | 0.30 | −0.002 | −0.003 | two different expectations: Savant's is per play (ball, runner), the pool's is per season × decision × outs cell |

**Stability on the SAME single-position regulars, scored both ways** (a player with one
outfield position at the minimum and no other outfield position that season):

| Feature | Min chances | 2023→24 pool / Savant | 2024→25 pool / Savant |
|---|---|---|---|
| prevention | 50 | 0.623 / 0.613 (n=31) | 0.496 / 0.256 (n=38) |
| prevention | 100 | 0.664 / 0.659 (n=26) | 0.554 / 0.469 (n=30) |
| attempt rate against | 50 | 0.564 / 0.359 | 0.531 / 0.470 |
| attempt rate against | 100 | 0.592 / 0.443 | 0.569 / 0.405 |
| thrown-out rate | 50 | 0.294 / 0.058 | 0.523 / 0.522 |
| Savant arm runs (total / hold part) | 50 | 0.377 / 0.355 | 0.386 / 0.328 |
| Savant arm runs (total / hold part) | 100 | 0.376 / 0.161 | 0.492 / 0.383 |

The samples are small (26 to 38), but the direction is consistent: the pool's
per-position figures repeat as well as or better than Savant's per-player figures on the
same players. Summing the pool across positions to match Savant's per-player row drops the
pool's prevention repeat to 0.26 to 0.38 at 100 chances: the position split carries real
information, and 73 of the 127 regulars with a 100-chance position in 2024 played another
outfield position too. That is the case for the pool, over and above the exact cutoff and
the seasons it covers without a new load.

---

## 3. Findings that shape the plan

**Finding 1 — our loader pulled the wrong view, so all four features are filled from it.**
The lead-distance plan's Finding 1 said the run value was right and that the board had no
fielder view. Neither holds, and the fault is ours, not Savant's. On the runner view the
`fielder_runs_*` columns belong to the runner's row: they mirror his own block (Ohtani,
who never fielded in 2024: runner advances +4.83, fielder advances −3.87; thrown out −1.65
against +1.69; hold −2.22 against +3.33), which is what a runner's row should say and
nothing about his arm. The fielder view exists under `type=Fld`; the September probe used
`type=fielder`, which the board silently ignores. The lead-distance document, the SIM-550 ticket text, the changelog entry and the
loader registry's docstring are corrected with this plan; the registry now pins
`type=Run` on the runner entry so the two views can never be confused again.

**Finding 2 — the arm needs its own confidence.** The fielder model's one confidence per
profile comes from batted balls. An everyday outfielder has 600 batted balls and 25 to
200 arm chances; his arm block would be read unshrunk whatever its sample. The arm group
gets its own confidence, chances / (chances + 50), the count at which the repeat reaches
about 0.5, applied only to the two rate features (velocity is Savant's, measured over 50
or more throws).

**Finding 3 — the league row must carry the arm keys.** The same class as the
lead-distance plan's Finding 3: without `arm_advancement_prevention`,
`arm_thrown_out_rate` and `arm_strength` on the `fielder_LF/CF/RF` league rows, shrinkage
pulls a thin arm toward 0.0 (a 0 prevention is average, but a 0 thrown-out rate is the
weakest arm in the league).

**Finding 4 — where the fitted bandwidth and weights go.** The calibration fits
`sigma_of_arm` and, by season-to-season correlation, the per-feature
`reliability_weights_of_arm`; the API applies them, the matrix builder does not. The run
book copies the fitted values into the module defaults before the matrix rebuild, as for
the lead-distance work.

---

## 4. Data changes

**No schema change.** The six columns the fill writes exist: `arm_opportunities`,
`arm_holds`, `arm_hold_rate`, `arm_assists`, `arm_thrown_out_rate`,
`arm_advancement_prevention`. `of_arm_runs` stays in the table and is written NULL.
`arm_strength` keeps its source (the arm-strength board, per position, season-shifted at
a cutoff as today). `raw.savant_baserunning` keeps the runner view for the lead-distance
work; no fielder-view table is loaded (decision 4, taken 2026-09-16).

**The column meanings change** (the canonical schema's comments are updated):

| Column | Was (the runner view) | Becomes (the pool) |
|---|---|---|
| `arm_opportunities` | the player's own extra-base chances as a runner | chances to advance on a ball THIS fielder fielded at THIS position |
| `arm_holds` | his own chances minus his own attempts | chances on which the runner held |
| `arm_hold_rate` | 1 − his own attempt rate | holds / chances |
| `arm_assists` | his own times thrown out | runners thrown out on his throws |
| `arm_thrown_out_rate` | — | thrown out / attempts against him |
| `arm_advancement_prevention` | a generic-fielder rate minus his own attempt rate | (expected attempts − attempts) / chances, expectation per season × decision × outs |
| `of_arm_runs` | the runner-view figure (the fielders on his own running plays) | NULL (not read by the model) |

**The league rows** `fielder_LF`, `fielder_CF`, `fielder_RF` gain three keys:
`arm_advancement_prevention`, `arm_thrown_out_rate`, `arm_strength` (averages over
outfield rows above the sample floor; `AVG` skips NULL).

**Point in time.** The fill filters the pool by `game_date <= cutoff`. The leakage
assertion for the fielder profiles gains the pool as a dated source. The
`of_arm_runs_in_cutoff_season` guard and the baserunning-board season shift are deleted.

---

## 5. Code changes, file by file

| File | Kind | Change |
|---|---|---|
| `pipeline/etl/savant_boards.py`, `tests/unit/test_sim528_savant_loader.py` | **done 2026-09-16** | the runner entry pins `type=Run`; the docstring describes both views; the test renamed |
| `pipeline/batch/player_profile_computor.py` | modified | new step `_fill_outfield_arm_block`; the aggregator writes the six arm columns NULL and drops the `sbr` join and the run-value guard; `_compute_outfield_arm_metrics` and the `_tmp_of_arm` placeholder deleted; the fielder league rows gain three keys; the leakage check gains the pool |
| `similarity/engines/fielder_similarity.py` | modified | `OF_ARM_FEATURES` = velocity, prevention, thrown-out; `sample_arm_chances` on the profile; the arm's own shrinkage confidence; the loader reads `arm_strength` |
| `similarity/similarity_calibration.py` | modified | the outfield SELECT and the arm fit mirror the new feature list |
| `db/schemas/02_duckdb_schema.sql` | modified | the arm columns' comments only |
| `scripts/sim550_arm_recompute.py` | new | the fill for every season, the league rows, the verify block with the fielder-view cross-check |
| `scripts/sim523_fit_probe.py` | modified | one tier read: attempts against the live fielder by his prevention tier |
| `tests/unit/test_sim550_arm_block.py` | new | §7 |
| `tests/unit/test_sim529_batter_physical.py` | modified | the three arm-block tests re-pointed at the fill step |
| `tests/unit/test_sim537_baserunner_catcher_fielder_point_in_time.py` | modified | the method-order list drops the dead step; the run-value guard test becomes the pool-cutoff test |
| `tests/unit/test_sim542_fielder_placeholder_schema.py` | modified | the `_tmp_of_arm` placeholder leaves the list |
| `docs/technical/sim-loop-cheat-sheet.md`, `similarity.md` | modified | the outfield arm group; the defect note replaced |
| `docs/audit/2026-09-16-sim531-lead-distance-build-plan.md`, `CHANGES.md`, `BACKLOG.xlsx` | modified | Finding 1 corrected (done 2026-09-16); the close entry; delete the row |
| `simulation/*`, `pipeline/batch/engine_artifacts.py`, `docker-compose.yml` | unchanged | the draws, the matrix specs and the powers do not change |

### 5.1 The fill step

```python
# pipeline/batch/player_profile_computor.py — run(), after _build_advancement_opportunity_pool
self._fill_outfield_arm_block(seasons, asof=asof)          # 6b. SIM-550: the arm block from the pool

ARM_ALPHA_PRIOR_CHANCES = 50   # shared with the engine: the chance count at which the repeat reaches ~0.5

def _fill_outfield_arm_block(self, seasons, asof=None):
    """SIM-550: the outfield arm block from sim.advancement_opportunity_pool, per
    (fielder, position, season). Runs AFTER the pools. A cutoff filters by game_date —
    exact point-in-time, no season shift. Without the pool (a fresh database) it logs
    and leaves the block NULL; the next nightly fills it."""
    if not _table_exists(self._conn, "sim", "advancement_opportunity_pool"):
        log.warning("  arm block skipped: sim.advancement_opportunity_pool absent"); return
    season_list = ", ".join(str(s) for s in seasons)
    date_cutoff = f"AND game_date <= DATE '{asof.isoformat()}'" if asof is not None else ""
    self._conn.execute(f"""
        CREATE OR REPLACE TEMP TABLE _arm AS
        WITH pool AS (
            SELECT season, scenario, from_base, target_base, outs, fielder_id, fielder_pos, attempted, safe
            FROM sim.advancement_opportunity_pool
            WHERE season IN ({season_list}) AND fielder_pos IN (7, 8, 9) AND fielder_id IS NOT NULL {date_cutoff}
        ),
        expected AS (                                   -- what a generic outfielder would concede in the same cell
            SELECT season, scenario, from_base, target_base, outs, AVG(attempted::INT) AS rate
            FROM pool GROUP BY 1, 2, 3, 4, 5
        )
        SELECT p.fielder_id AS player_id,
               CASE p.fielder_pos WHEN 7 THEN 'LF' WHEN 8 THEN 'CF' ELSE 'RF' END AS position,
               p.season,
               COUNT(*)                                                    AS chances,
               SUM(p.attempted::INT)                                       AS attempts,
               SUM(CASE WHEN p.attempted AND NOT p.safe THEN 1 ELSE 0 END) AS thrown_out,
               SUM(e.rate)                                                 AS expected_attempts
        FROM pool p JOIN expected e USING (season, scenario, from_base, target_base, outs)
        GROUP BY 1, 2, 3
    """)
    self._conn.execute(f"""
        UPDATE derived.fielder_season_metrics f SET
            arm_opportunities          = a.chances,
            arm_holds                  = a.chances - a.attempts,
            arm_hold_rate              = 1.0 - a.attempts * 1.0 / a.chances,
            arm_assists                = a.thrown_out,
            arm_thrown_out_rate        = a.thrown_out * 1.0 / NULLIF(a.attempts, 0),   -- NULL until he has been challenged
            arm_advancement_prevention = (a.expected_attempts - a.attempts) / a.chances,
            of_arm_runs                = NULL
        FROM _arm a
        WHERE f.player_id = a.player_id AND f.position = a.position AND f.season = a.season
    """)
    self._check_leakage("sim.advancement_opportunity_pool", f"SELECT MAX(game_date) FROM sim.advancement_opportunity_pool WHERE season IN ({season_list}) {date_cutoff}", asof or date.today())
    log.info("  outfield arm block filled from the advancement pool.")
```

The aggregator, `_aggregate_fielder_season_metrics`: the six arm columns become
`NULL::… AS arm_opportunities` etc. with a comment naming the fill step (an outfielder
who fielded no chance keeps NULL, never 0); `of_arm_runs` NULL; `arm_strength`
unchanged; the `LEFT JOIN pg.raw.savant_baserunning sbr` and the
`of_arm_runs_in_cutoff_season` expression deleted. `_compute_outfield_arm_metrics`, its
call in `run()` and the `_tmp_of_arm` placeholder deleted (decision 3, taken). The fielder league
rows:

```python
'arm_hold_rate',              AVG(arm_hold_rate),
'arm_advancement_prevention', AVG(arm_advancement_prevention),   # NEW: the shrink targets the engine reads
'arm_thrown_out_rate',        AVG(arm_thrown_out_rate),
'arm_strength',               AVG(arm_strength),
```

### 5.2 The fielder model

```python
# similarity/engines/fielder_similarity.py
OF_ARM_FEATURES = [                         # weight = measured year-to-year repeat (§2.2)
    ("arm_strength", 0.857),                # throw velocity, mph (Savant, per position)
    ("arm_advancement_prevention", 0.60),   # expected − actual attempts per chance, from our pool
    ("arm_thrown_out_rate", 0.25),          # thrown out / attempts against him
]
ARM_ALPHA_PRIOR_CHANCES = 50

class FielderProfile: + sample_arm_chances: int     # arm_opportunities (0 when NULL)

_load_profiles: SELECT … fsm.arm_strength, fsm.arm_advancement_prevention, fsm.arm_thrown_out_rate, fsm.arm_opportunities …
                arm_vec = _v([arm_strength, arm_prev, arm_to])      # NULL → NaN (already the loader's rule)
                sample_arm_chances = arm_opps or 0

_load_positional_averages: the "arm" vector from the league row's three keys (NaN when a key is absent, not 0.0)

_apply_shrinkage (outfield branch):
    avg = self._pos_avg["arm"][pos][s]
    if avg is not None and p.arm_vec is not None:
        n_arm = p.sample_arm_chances
        rates = self._shrinkage.shrink(p.arm_vec[1:], avg[1:], n_arm, n_prior=ARM_ALPHA_PRIOR_CHANCES)  # the two rates: the arm's own confidence
        velocity = np.where(np.isnan(p.arm_vec[:1]), avg[:1], p.arm_vec[:1])                      # measured by Savant; missing → league mean
        p.arm_vec = np.concatenate([velocity, rates])
    # 25 chances → alpha 0.33: two thirds of the way to the league mean; 200 chances → 0.80

apply_calibration: unchanged in shape (sigma_of_arm; reliability_weights_of_arm now three long)
```

`EmpiricalBayesShrinkage.shrink` gains an optional `n_prior` argument (default: the
instance's, so nothing else moves).

### 5.3 Calibration

```python
# similarity/similarity_calibration.py — _calibrate_fielder_params, the outfield SELECT
arm_strength, arm_advancement_prevention, arm_thrown_out_rate,      # in OF_ARM_FEATURES order; of_arm_runs and arm_hold_rate leave the SELECT
# arm_raw: NaN for NULL; arm_measured = rows where all three are finite (2023+ outfielders with a velocity and a challenge)
# sigma_of_arm and reliability_weights_of_arm fit as today, over arm_measured
```

### 5.4 The recompute script

```python
# scripts/sim550_arm_recompute.py — the sim531 pattern; the app STOPPED (the DuckDB writer lock, SIM-524)
def main(seasons):                                       # 2017 … 2026: the pool holds them all
    c = PlayerProfileComputor(dsn, duckdb_path)
    c._fill_outfield_arm_block(seasons)                  # seconds: one aggregation over the pool
    LeagueAverageProfiles(duckdb_path).compute(seasons)  # the fielder rows gain the three arm keys
    verify():
        per season: outfield rows with a block (expect ~560-650 of ~600; 2020 fewer), the chances quartiles (~7 / 25 / 81)
        the league figures: hold 0.78-0.81, thrown-out share 0.04-0.06, prevention mean ≈ 0 (it is a deviation)
        a catcher and a designated hitter have NO arm block (position C / no outfield row)
        the five 2024 center fielders with the most chances against a direct pool query, value for value
        of_arm_runs IS NULL everywhere; arm_strength coverage unchanged (~75% of outfield rows, 2023+)
        the three arm keys on fielder_LF/CF/RF league rows; one cutoff per table
        THE CROSS-CHECK: pull Savant's fielder view for 2024 (type=Fld, n=1) and correlate, per player with
            positions summed, chances (expect r >= 0.95; measured 0.98) and the thrown-out rate (expect r >= 0.7;
            measured 0.78) — a sign the fielder attribution is right without loading the board
```

### 5.5 The probe

`scripts/sim523_fit_probe.py` gains one tier read on the advancement draws: attempts per
chance against the LIVE fielder, by tercile of his prevention, against the pool's own
rate for those fielders. A flat read means the factor does not reach the draw; an
over-steep read means the power 1.2 now over-concentrates.

---

## 6. Weights, bandwidth, power

- **Feature weights** = the measured repeats (velocity 0.857, prevention 0.60, thrown-out
  0.25), the group's own rule; `make calibrate` refits them by season-to-season correlation
  for the API, and the fitted values are copied into the module defaults.
- **Group weight** 0.30 of an outfielder's score, unchanged: the other three groups'
  repeats are not measured here, and the group's share is not what this ticket fixes.
- **Bandwidth** `sigma_of_arm`: today's 1.007 was fitted on the wrong data; refit over the
  measured rows; copied into the module default.
- **Power** 1.2 on the fielder factor, unchanged for the landing. The probe's tier read
  and the power scan say whether it moves, in its own commit.

---

## 7. Tests (`tests/unit/test_sim550_arm_block.py` unless named)

- **The fill, on an in-memory DuckDB** (no Postgres: the step reads only DuckDB tables): a
  six-row pool and a three-row fielder table; the block's counts and rates come out as
  hand-computed; a catcher's row and an infielder's row stay NULL; a chance with
  `fielder_id` NULL is ignored; the expectation is per season × decision × outs; a cutoff
  drops later-dated rows; `of_arm_runs` is NULL; a database without the pool logs and
  leaves the block NULL.
- **The aggregator** (source assertions, the SIM-529 file re-pointed): no
  `savant_baserunning` join in the fielder aggregator; the six arm columns are written NULL
  there and filled by the step; `arm_strength` still comes from the arm-strength board;
  `_compute_outfield_arm_metrics` and `_tmp_of_arm` are gone; the run order has the fill
  after the pools.
- **The registry** (done in the SIM-528 file): the runner entry pins `type=Run`.
- **Point in time** (the SIM-537 file): the fill filters by `game_date` under a cutoff and
  not without; the leakage check names the pool; the run-value guard test is deleted.
- **The model:** `OF_ARM_FEATURES` is the three; the arm confidence is chances-based (25
  chances → 0.33, 200 → 0.80) and leaves the other groups' confidence alone; a missing
  velocity becomes the league mean, never 0; a missing prevention on a profile with zero
  chances becomes the league mean; the league row's absent key yields NaN, not 0.0;
  `score_all` and `query_pair` agree; the invariant gate (`make test-regression`) holds.
- **Calibration:** the outfield SELECT names the three columns; the fit uses measured rows
  and returns the sentinel when fewer than 20.
- **Ticket condition:** a test asserts a catcher's or designated hitter's arm block is
  NULL after the fill.

---

## 8. Run book

Runs after the lead-distance work's matrices and accuracy arm (SIM-531 steps 6 to 8c).
Steps 2 and 3 need the application stopped (the DuckDB writer lock).

```
# 0. the OFF arm: the lead-distance ON-arm report IS this ticket's OFF arm when SIM-550 runs
#    straight after it (same games, seeds, bundle); otherwise run one on the pre-change bundle
# 1. the code lands; the gates
make lint && make type-check && make test-unit && make test-regression
# 2. stop the app; the fill for every season the pool holds, the league rows, the verify block (minutes)
docker compose stop app
MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" app \
  python scripts/sim550_arm_recompute.py --seasons 2017 2018 2019 2020 2021 2022 2023 2024 2025 2026
#    the verify block pulls Savant's fielder view for 2024 as the cross-check (one HTTP request; --no-cross-check to skip)
# 3. refit; copy sigma_of_arm and the three reliability weights into the module defaults (one commit)
make calibrate
#    ⚠ make calibrate DROPS the win-probability reliability curve (the SIM-427 trap): write it back
# 4. the three outfield matrices + the concentration report (the infield matrices do not read the arm group)
docker compose run --rm app python -m pipeline.batch.engine_artifacts --what actors_sim \
  --matrix fielder_LF --matrix fielder_CF --matrix fielder_RF
#    read actor_sim/concentration.json: the p90 own-staff ratio under 3.0 (§9, risk 2)
# 5. start the app; "build_all_engines: 11/11" + the calibration lines in the boot log
docker compose start app
# 6. the checks: (a) the fit probe's fielder tier read, (b) the power scan on the fielder factor,
#    (c) the ON arm on the same 250 games of 2024 at 100 iterations, paired with sim518_pair_accuracy.py
#        (cross-bundle by nature, as for SIM-531: --force with the confound stated) — read the totals,
#        the moneyline and the batter run / RBI props
# 7. close: CHANGES.md; delete the SIM-550 row; the cheat sheet and similarity.md
```

**Not in this ticket.** The catcher-throwing and first-base boards re-loaded at their
lifted minimums reach the catcher and first-base profile sections only at a full
`make profile-computor` (five hours). This plan no longer needs that rebuild, so it is a
separate, whenever-convenient run; note it in the changelog when it happens.

---

## 9. What could go wrong (ranked)

1. **The fill runs before the pool exists** (a fresh database, or a partial recompute
   that skipped the pools). The step logs and leaves NULL; the next full run fills it.
   Never a zero.
2. **A park signature in prevention.** Half an outfielder's chances are in his home park,
   and a deep right field makes runners hold. The concentration report on the three
   matrices is the read; if the own-staff ratio trips 3.0, the follow-on is a park term in
   the expectation cell (season × decision × outs × park), not a different feature.
3. **A thin arm read at face value.** The arm's own confidence (Finding 2) and the league
   keys (Finding 3) are the guard; a test shrinks a 25-chance arm two thirds of the way.
4. **The OFF arm on the wrong bundle.** Use the lead-distance ON report or run one first.
5. **The two views confused again.** The registry pins `type=Run` on the runner entry and
   a test holds it; any future fielder-view pull is its own entry with `type=Fld`.
6. **The thrown-out rate is thin.** Five assists a season; at weight 0.25 and shrunk, it
   cannot dominate; the refit may lower it further, which is fine.
7. **Velocity missing before 2023 and on a quarter of outfield rows.** League mean, neutral;
   the matrices cover 2023 to 2026, where coverage is 75%.

---

## 10. Decisions taken (owner, 2026-09-16)

All four recommendations were approved. Each is recorded as the ruling the build follows,
with the alternative that was not taken.

1. **Add the throw velocity to the arm group.** Decided: yes. It repeats at 0.86, it is
   independent of the pool-derived rates, it is already loaded and stored, and the model
   does not read it today. Not taken: the two pool-derived rates alone.
2. **Drop the run value and the raw hold rate from the group.** Decided: yes. The run
   value we loaded is the runner-view figure, not the fielder's; the fielder view's own
   repeats at only 0.38 to 0.49 and is an outcome summary; the hold rate is prevention
   without the situation adjustment (r = 0.90). Both stay stored for the record. Not
   taken: the hold rate as a fourth feature at its own repeat (0.47 to 0.61 at 100 chances).
3. **Delete the dead arm extraction** (`_compute_outfield_arm_metrics`, `_tmp_of_arm`).
   Decided: yes; nothing reads it and it confuses the next reader into thinking the block
   is computed there. Not taken: leave it.
4. **The source: our pool.** Decided: the pool. It keeps
   positions apart (most regulars play two), it repeats as well or better on the same
   players, it is point-in-time exact, it covers every season without a new load, and the
   fielder view cross-checks it in the verify block. Not taken: the fielder view, which
   is the smaller code change (a second registry entry with `type=Fld`, one Alembic
   migration for `raw.savant_baserunning_fielder`, the aggregator's join re-pointed and
   the September mapping kept as written), per player, season-shifted at a cutoff, with a
   play-level expectation and a real arm run value.

Also recorded: the grade is the paired accuracy comparison, the lane optional, per the
ruling on the lead-distance work; the group weight 0.30 and the power 1.2 stay for the
landing.
