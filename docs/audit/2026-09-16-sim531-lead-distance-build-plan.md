# Build plan — lead distance in the stolen-base and baserunning models (SIM-531)

> **STATUS 2026-09-16 — APPROVED, and the CODE LANDED the same day.** The owner took all
> four recommendations (§10). Every file in §5 is built as written (the record is
> `CHANGES.md`, 2026-09-16): Alembic 0026, DuckDB 0029 (schema v29), the two boards, the
> three profile joins with the widened steal driver, the three models, the calibrator, the
> recompute script, 52 tests; ruff, mypy and the unit + regression lanes green. An adversarial
> review (59 agents) confirmed five defects and four prose errors the same day; all are fixed
> (`CHANGES.md` names them — the one that mattered: the confidence basis is the runner's CHANCES,
> first plus second base and never below his attempts, carried as `sample_second_base_opps`, so no
> row reads confidence 0). Two calls
> inside the plan's intent: the tendency group shrinks on first-base opportunities like the
> lead (an attempts-based shrink would erase a non-runner's measured 0.0), and the builder
> tests live in `tests/unit/test_sim531_lead_distance.py`, which also EXECUTES the three
> builders on a fake `pg` catalog. **The run book (§8) RAN the same evening, without its two
> accuracy arms** (the owner's ruling of 2026-09-16: every change lands ON at its best-known
> default, and the weights are fitted together in one designed experiment when the model is
> final — CLAUDE.md §2b, the architecture rule's third clause). Alembic 0026 applied; the
> five boards loaded at `n=1` (9,525 rows, every 2024 count as expected); 0028 then 0029
> applied and the three runner tables + the league rows rebuilt (verify: every check passed);
> `make calibrate` fitted the lead bandwidth 0.9836 and the hold bandwidth 0.9897 (copied into
> the module defaults; the win-probability curve written back); the three matrices rebuilt
> (2,585 / 2,486 / 2,390 profiles); the app restarted with the calibration applied. The
> record with the numbers is `CHANGES.md` (2026-09-16, the entry above the build entry).
> The paired read folds into the designed experiment; the ticket closes on this record.
> The arm-block defect (§3, Finding 1) is FILED as SIM-550 (P2). The readable page is
> https://claude.ai/artifact/RdSK6WUptRHqNVwux17p9z (the plan as approved).

**Date:** 2026-09-16
**Ticket:** SIM-531 (P1) in `BACKLOG.xlsx`. Next free ID: SIM-551 (SIM-550 filed by this plan).
**Evidence:** the three Savant boards pulled live on 2026-09-16 for 2023, 2024 and 2025,
at the qualified default and at the lifted minimum; the minimum parameter of every
board the platform pulls probed live on 2024; the code at commit 036b8e4.
**Builds on:** `docs/audit/2026-09-10-sim528-530-savant-build-plan.md`,
`docs/audit/2026-09-10-savant-point-in-time-data.md`,
`docs/audit/2026-09-08-sim523-play-picker-redesign-plan.md`,
`docs/audit/2026-09-13-sim427-build-plan.md`.

---

## 0. The short version

**What we add.** Two physical measurements the steal decision has never seen: the
runner's lead off the bag before the pitch and the extra distance he gets on the
pitcher's delivery (the "jump"), and the mirror image for the pitcher (how much lead he
allows and how much jump he gives up). For the extra-base decision, we add how often a
runner tried for the extra base against how often a typical runner would have tried in
the same chances. That expectation is the denominator the platform never had.

**Everyone, not only the qualifiers (owner ruling 2026-09-16).** Savant's default is
qualified players. Passing `n=1` returns every player with one chance: 638 runners
instead of 432, 849 pitchers instead of 496, and 623 extra-base runners instead of 305
(2024). Every row carries a measured lead. The same ruling applies to every board the
platform pulls; three of the eight already loaded were still restricted (§2.1) and the
registry is fixed. The cost of the wider net is that a low-count row is noise, so the
model must weight each measurement by its sample size; that is what the confidence and
shrinkage changes in §3 and §5 do, and they move from optional to required.

**Why it is worth doing.** On the qualified rows the runner's jump repeats year to year
at 0.80–0.85. The pitcher's jump allowed repeats at 0.89–0.90, the most stable number
either model would hold. The steal success rate, which the runner model weights at 38%
today, repeats at 0.13–0.26: noise carrying a large weight. The extra-base attempt rate
above expectation repeats at 0.75–0.77, against 0.50–0.59 for the raw rate.

**What it costs.** Two new Savant boards and one Postgres migration; twelve profile
columns in one DuckDB migration; three model files gain one feature group each; two
league-average rows; one section-scoped recompute (minutes); one calibration fit; one
matrix rebuild. Then the standard checks: the offline power scan, the in-loop fit probe,
the paired accuracy comparison on 250 games of 2024. The pool-totals lane is optional
under the 2026-09-10 ruling.

**Found on the way.** The outfield arm block built on 2026-09-11 reads the wrong side of
Savant's baserunning board: it stores the outfielder's own running figures as the runners
he held. Three of its four arm features are wrong. That is a separate defect (§3,
Finding 1). Also, a runner who never attempted a steal has no steal profile today.

---

## 1. The mechanism

Every actor factor in a draw is its model's similarity score, built nightly into a
matrix and looked up at draw time. New data reaches the simulator only through this
chain, in this order:

```
Savant board ──loader (n=1)──► raw table (Postgres) ──builder──► profile table (DuckDB)
   ──matrix build──► model score matrix ──score^power──► draw

Basestealing Run Value (638 runners) ─► raw.savant_basestealing (NEW, Alembic 0026)
   ─► derived.baserunner_steal_metrics + lead_primary_ft, lead_jump_ft (0029)
   ─► steal-runner model + Lead sub-score ─► runner_steal ─► steal draw ^12
Pitcher Running Game (849 pitchers) ─► raw.savant_pitcher_running_game (NEW)
   ─► derived.pitcher_steal_metrics + lead_allowed_primary_ft, lead_allowed_jump_ft
   ─► pitcher-hold model + Hold sub-score ─► pitcher_steal ─► steal draw ^12
Baserunning (623 runners at n=1; 305 loaded today) ─► raw.savant_baserunning (RE-LOAD)
   ─► derived.baserunner_season_metrics + xb_attempt_rate_above_expected
   ─► advancement-runner model + 1 aggression feature ─► runner_adv ─► advancement draw ^20

raw.savant_baserunning ┄┄► derived.fielder_season_metrics arm block (SIM-530):
                            reads the runner's own side — DEFECT, SIM-550 (filed)
```

At a backtest cutoff the loader's rows are season-shifted (the prior season's row
stands in), because no Savant board carries a date. The draws do not change; they read
the rebuilt matrices at today's powers.

---

## 2. The evidence

### 2.1 Every board at its smallest minimum (probed live on 2024, 2026-09-16)

| Board | Minimum parameter | Qualified rows → lifted | Floor reached | Registry state |
|---|---|---|---|---|
| Basestealing Run Value (new) | `n=1` | 432 → 638 | 1 chance | in this plan |
| Pitcher Running Game (new) | `n=1` | 496 → 849 | 1 chance | in this plan |
| Baserunning | `n=1` | 305 → 623 | 1 chance | **fixed 2026-09-16** (was bare) |
| Catcher throwing | `n=1` | 66 → 94 | 1 attempt | **fixed 2026-09-16** (was bare) |
| First-base receiving | `min=1` | 42 → 152 | 1 play | **fixed 2026-09-16** (was bare) |
| Bat tracking, swing path | `minSwings=0` | 215 → 650 | 1 swing | already lifted |
| Batting stance | `minSwings=0`, `minContact=0` | 239 → 716 | — | already lifted |
| Pop time | `min2b=0`, `min3b=0` | 83 → 100 | 0 attempts | already lifted |
| Arm strength | none honoured | 388 either way | 50 throws | Savant's floor: its dropdown's smallest option is 50; `minThrows` 0, 1, 5 and unset all return 388 |
| Sprint speed (own loader) | `min=0` | 566 → 606 | 5 runs | already lifted; `min=1` gives the same 606 |

Two traps found by probing. `n=0` is IGNORED by the run-value boards: it reads as "not
set" and serves the qualified default (432 rows for `n=0` and unset alike). The smallest
honoured value is `n=1`. And the earlier audit's claim that the baserunning board rejects
every parameter was wrong about `n` and `type`: it rejects `year=` and ignores `min=`,
but honours `n=`, and `type=Run` / `type=Fld` select the runner or the fielder view
(found by the owner 2026-09-16; the spelling `type=fielder` is silently ignored). The registry docstring, the baserunning entry and the loader tests are
corrected (`pipeline/etl/savant_boards.py`, `tests/unit/test_sim528_savant_loader.py`;
31 tests green). Both new boards honour the season parameter (1990 returns zero rows)
and carry the season on every row (`start_year`).

### 2.2 Which columns repeat, on the qualified rows (year-to-year r, 23→24 / 24→25)

These are the figures the feature weights are set from: they measure how repeatable
the trait is when it is well measured. The noise a small sample adds is handled by
shrinkage (§2.5), not by the weight.

| Actor | Column | 23→24 | 24→25 | Verdict |
|---|---|---|---|---|
| Runner | `r_primary_lead` (lead before the pitch, ft) | 0.732 | 0.740 | **keep** |
| Runner | `r_sec_minus_prim_lead` (the jump) | 0.801 | 0.846 | **keep** |
| Runner | `r_secondary_lead` | 0.736 | 0.773 | store only (= primary + jump) |
| Runner | `rate_sbx` (attempts per chance) | 0.789 | 0.769 | store only (we compute ours) |
| Runner | `r_primary_lead_sbx` (attempts only) | 0.319 | 0.286 | store only: noise |
| Runner | success rate (≥10 attempts) | 0.262 | 0.125 | in the model; weight falls |
| Runner | `n_pk` | 0.223 | 0.171 | store only: noise |
| Pitcher | `r_sec_minus_prim_lead` (jump allowed) | 0.893 | 0.899 | **keep** |
| Pitcher | `r_primary_lead` (lead allowed) | 0.431 | 0.426 | **keep**, lower weight |
| Pitcher | `rate_sbx` | 0.428 | 0.486 | in the model |
| Pitcher | CS rate when challenged | 0.320 | −0.031 | in the model; noise |
| Pitcher | `n_pk` | 0.335 | 0.275 | store only |
| XB runner | `rate_att_xb − est_rate_att_generic_runner` | 0.753 | 0.768 | **keep** |
| XB runner | `rate_att_xb` | 0.591 | 0.498 | store only |
| XB runner | `rate_safe_per_attempt` | −0.057 | 0.002 | noise |
| XB runner | `runner_runs` | 0.592 | 0.652 | outcome summary; excluded |

### 2.3 Independence (2024)

- Runner primary lead vs jump r = 0.02: both earn a place; the secondary lead is their
  sum, so reading all three would double-count.
- Runner primary lead vs attempt rate r = 0.49; jump vs attempt rate r = 0.21.
- Pitcher jump allowed vs attempt rate allowed r = 0.45; primary vs jump r = 0.17.

### 2.4 Coverage

- At `n=1`: ~640 runners, ~850 pitchers and ~630 extra-base runners a season, every one
  with a measured lead or rate (no blanks on the low-count rows).
- 68 of the 432 qualified runners in 2024 never attempted a steal; our steal profile
  table has no row for them (§3, Finding 2). At `n=1` the count of such runners is higher.
- The baserunning board's default rows are the RUNNER view: catchers and a DH carry
  60–160 chances on it. The fielder view is a separate pull (`type=Fld`, SIM-550).
- All three boards start in 2023 = the pool window. Earlier seasons stay null and neutral.

### 2.5 What the lifted minimum adds, and what it costs

The same year-to-year measurement on the `n=1` pulls, by how many chances the player had
in both seasons:

| Actor / column | 1–49 chances | 50–161 | ≥162 (the old floor) | all rows |
|---|---|---|---|---|
| Runner primary lead | 0.00 / 0.19 (n≈13) | 0.78 / 0.48 (n≈21) | 0.73 / 0.74 | 0.63 / 0.63 |
| Runner jump | 0.45 / 0.87 (n≈13) | 0.69 / 0.57 (n≈21) | 0.80 / 0.85 | 0.68 / 0.72 |
| Pitcher lead allowed | 0.46 / 0.07 (n≈32) | 0.00 / 0.15 (n≈33) | 0.43 / 0.43 | 0.29 / 0.17 |
| Pitcher jump allowed | 0.39 / 0.50 (n≈32) | 0.68 / 0.61 (n≈33) | 0.89 / 0.90 | 0.71 / 0.76 |
| XB attempt rate above expectation | −0.05 / −0.10 (1–19 chances, n≈55) | 0.59 / 0.42 (20–53, n≈35) | 0.75 / 0.77 (≥54) | 0.32 / 0.29 |

Half the extra-base runners on the `n=1` board have fewer than 51 chances (median 51,
lower quartile 16); a quarter of the runners on the steal board have fewer than 110
initiations. Below about 50 chances a measurement is close to noise. So the wider net is
right for coverage, and it makes two things mandatory that the first draft of this plan
treated as optional: the confidence basis must be the sample size the measurement was
taken over (Finding 2), and the two steal models must actually shrink a thin profile
toward the league mean (Finding 3), which they cannot do today because their
league-average rows do not exist.

---

## 3. Findings that shape the plan

**Finding 1 — a defect in existing work.** The fielder profile builder joins
`raw.savant_baserunning` and writes `n_opp_xb`, `n_att_xb`, `rate_att_xb`, `n_out` and
`est_rate_att_generic_fielder` as the outfielder's holds, thrown-out rate and advancement
prevention (`player_profile_computor.py` ~5857–5876). Those columns are the player's own
extra-base chances as a runner: the loader pulls the board's RUNNER view, its default. The
FIELDER view exists under `type=Fld` (the owner found it 2026-09-16; `type=fielder` is
silently ignored, which misled the September audit); the registry now pins `type=Run`. Three of the four `OF_ARM_FEATURES` (30% of an
outfielder's score; the fielder factor weights the fielding and advancement draws at
1.2) are filled from the wrong view. The arm run value too: on a runner's row the
`fielder_runs_*` columns belong to the runner (a designated hitter who never fielded carries
them; found 2026-09-16 while designing the fix). The data is right for the view we asked
for; the parameter was ours. Only `arm_strength`, from the arm-strength board, was pulled as
intended, and the model does not read it. Not fixed here (it
would make the accuracy read unattributable). **Filed as SIM-550 (P2) on 2026-09-16**,
with this fix: derive the fielder's figures from `sim.advancement_opportunity_pool`
(`fielder_id`, `attempted`, `safe`) — chances against him, attempts against him, runners
thrown out — and run it after SIM-531 so each accuracy read stays attributable. The
design is `docs/audit/2026-09-16-sim550-outfield-arm-block-plan.md`.

**Finding 2 — a gap this ticket closes, and the confidence basis.**
`_build_baserunner_steal_metrics` drives from `attempt_agg`, so a zero-attempt runner
has no row. With a lead he can be compared. Trap: the confidence
`attempts / (attempts + 20)` multiplies the whole score; zero attempts → zero score for
every row → the draw's weight sum is 0 → `steal_draw` returns `None` → no steal
decision, not even a pickoff. The confidence basis moves to first-base opportunities
(prior 50, the model's own stabilisation figure). With the `n=1` load this is not
optional: a runner with 20 initiations must carry a confidence near 0.1, not 1.0.

**Finding 3 — the missing-value rule and the missing shrinkage.**
`derived.league_averages` has no `baserunner_steal` / `pitcher_steal` rows, so those two
engines never shrink a thin profile toward the league mean: `_apply_shrinkage` finds no
average and leaves every feature vector raw. Their loaders also map NULL → 0.0, and a
lead of 0.0 ft is 12 standard deviations below the league (mean 11.6, sd 0.9). Two
rules: NULL loads as NaN (the normaliser makes it neutral), and a pair where either side
lacks a group is scored over the groups both have. And two league-average rows are
written so the shrinkage engages (§5.2); with the `n=1` load it is the mechanism that
keeps a 20-initiation lead from being read at face value.

**Finding 4 — where the fitted bandwidth goes.** The matrix builder does not apply
`/data/calibration.json` (recorded in the 2026-09-11 close). The run book fits the new
sigmas with `make calibrate` and copies them into the module defaults before the matrix
rebuild.

---

## 4. Data changes

### 4.1 Postgres — Alembic 0026 (`0026_sim531_savant_running_game.py`)

```sql
CREATE TABLE IF NOT EXISTS raw.savant_basestealing (
    player_id                  INTEGER NOT NULL REFERENCES raw.players(player_id),
    season                     INTEGER NOT NULL,
    n_init                     INTEGER,   -- pitches on which the runner could have gone
    rate_sbx                   FLOAT,     -- (n_sb + n_cs) / n_init
    n_sb INTEGER, n_cs INTEGER, n_pk INTEGER, n_bk INTEGER,
    runs_stolen_on_running_act FLOAT,
    r_primary_lead FLOAT, r_secondary_lead FLOAT, r_sec_minus_prim_lead FLOAT,
    r_primary_lead_sbx FLOAT, r_secondary_lead_sbx FLOAT, r_sec_minus_prim_lead_sbx FLOAT,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (player_id, season));
CREATE INDEX IF NOT EXISTS idx_savant_basestealing_season ON raw.savant_basestealing(season);
CREATE TABLE IF NOT EXISTS raw.savant_pitcher_running_game (
    -- the same shape plus runs_prevented_on_running_attr FLOAT, n_pitcher_cs_aa FLOAT
    PRIMARY KEY (player_id, season));
-- downgrade: DROP INDEX + DROP TABLE for both (the 0019 pattern)
```

`n_fb`, `n_plus`, `n_minus`, `net_*` are not stored (unglossed / outcome decomposition).

### 4.2 DuckDB — migration 0029 (schema v28 → v29)

```sql
-- steal runner (explicit-column INSERT: order free)
ALTER TABLE derived.baserunner_steal_metrics ADD COLUMN IF NOT EXISTS lead_primary_ft   FLOAT;
ALTER TABLE derived.baserunner_steal_metrics ADD COLUMN IF NOT EXISTS lead_secondary_ft FLOAT;
ALTER TABLE derived.baserunner_steal_metrics ADD COLUMN IF NOT EXISTS lead_jump_ft      FLOAT;
ALTER TABLE derived.baserunner_steal_metrics ADD COLUMN IF NOT EXISTS savant_steal_opps INTEGER;
-- pitcher hold (explicit-column INSERT)
ALTER TABLE derived.pitcher_steal_metrics ADD COLUMN IF NOT EXISTS lead_allowed_primary_ft   FLOAT;
ALTER TABLE derived.pitcher_steal_metrics ADD COLUMN IF NOT EXISTS lead_allowed_secondary_ft FLOAT;
ALTER TABLE derived.pitcher_steal_metrics ADD COLUMN IF NOT EXISTS lead_allowed_jump_ft      FLOAT;
ALTER TABLE derived.pitcher_steal_metrics ADD COLUMN IF NOT EXISTS savant_hold_opps          INTEGER;
-- advancement runner (POSITIONAL INSERT: appended after asof_date, in XB_COLUMN_ORDER)
ALTER TABLE derived.baserunner_season_metrics ADD COLUMN IF NOT EXISTS xb_opportunities               INTEGER;
ALTER TABLE derived.baserunner_season_metrics ADD COLUMN IF NOT EXISTS xb_attempt_rate                FLOAT;
ALTER TABLE derived.baserunner_season_metrics ADD COLUMN IF NOT EXISTS xb_expected_attempt_rate       FLOAT;
ALTER TABLE derived.baserunner_season_metrics ADD COLUMN IF NOT EXISTS xb_attempt_rate_above_expected FLOAT;
```

Mirror into `db/schemas/02_duckdb_schema.sql`; bump `duckdb_schema_version.txt` to 29.
`derived.baserunner_season_metrics` is a positional INSERT: the SELECT appends the four
columns after `asof_date` in `XB_COLUMN_ORDER`; a test holds the live table to
`BASERUNNER_TAIL_COLUMNS`. The actor embeddings pick up the numeric columns
automatically (harmless: the draws read the matrices).

### 4.3 Point-in-time

The three joins follow the 0028 season-shift rule (the cutoff season joins the prior
season's row; a live build joins the same season). The measured stability on qualified
rows (0.73–0.90; 0.75 for the adjusted attempt rate) is the evidence the substitute
costs little; for a thin row the shrinkage dominates either way.

---

## 5. Code changes, file by file

| File | Kind | Change |
|---|---|---|
| `pipeline/etl/savant_boards.py`, `tests/unit/test_sim528_savant_loader.py` | **done 2026-09-16** | baserunning `n=1`, catcher throwing `n=1`, first-base receiving `min=1`; the docstring's minimum table; three tests |
| `db/migrations/versions/0026_sim531_savant_running_game.py` | new | §4.1 |
| `db/migrations/duckdb/0029_sim531_lead_distance.sql` | new | §4.2 |
| `db/schemas/02_duckdb_schema.sql`, `duckdb_schema_version.txt` | modified | mirror; 29 |
| `pipeline/etl/savant_boards.py` | modified | two entries at `n=1`; `RUNNING_BOARDS` |
| `pipeline/etl/savant_loader.py` | modified | five count columns into `_INT_TARGETS` |
| `pipeline/batch/player_profile_computor.py` | modified | three joins; the steal driver widens; the tail tuple; three league-average changes |
| `similarity/engines/baserunner_steal_similarity.py` | modified | Lead sub-score; opportunity confidence; NaN; missing-group rule |
| `similarity/engines/pitcher_steal_similarity.py` | modified | Hold sub-score; NaN; missing-group rule |
| `similarity/engines/baserunner_similarity.py` | modified | one aggression feature; masked kernel |
| `similarity/similarity_calibration.py` | modified | two fields; two fits over measured rows; the aggression fit; summary |
| `scripts/sim531_runner_recompute.py` | new | apply 0029; rebuild the three tables + league averages; verify |
| `tests/unit/test_sim531_lead_distance.py` | new | §7 |
| `tests/unit/test_sim537_baserunner_catcher_fielder_point_in_time.py` | modified | three season-shift tests |
| `docs/technical/sim-loop-cheat-sheet.md`, `similarity.md`, `pipeline-betting-db.md` | modified | the three model sections; the board list and its minimums; the stale "arm block is still empty" line |
| `CHANGES.md`, `BACKLOG.xlsx` | modified | the close entry; delete the SIM-531 row (SIM-550 is already filed) |
| `simulation/full_pool_sampler.py`, `sim_loop.py`, `engine_artifacts.py`, `docker-compose.yml` | unchanged | the draws, the matrix specs and the powers do not change in the first landing |

### 5.1 The loader registry

```python
# pipeline/etl/savant_boards.py
"basestealing": SavantBoard(
    name="basestealing", url=f"{BASE}/leaderboard/basestealing-run-value",
    season_style="snake", table="raw.savant_basestealing",
    player_column="player_id", season_column="start_year",
    extra={"n": "1"},        # every runner with one chance (638 vs 432 qualifiers, 2024); n=0 is IGNORED
    columns=(("n_init","n_init"), ("rate_sbx","rate_sbx"), ("n_sb","n_sb"), ("n_cs","n_cs"),
             ("n_pk","n_pk"), ("n_bk","n_bk"), ("runs_stolen_on_running_act","runs_stolen_on_running_act"),
             ("r_primary_lead","r_primary_lead"), ("r_secondary_lead","r_secondary_lead"),
             ("r_sec_minus_prim_lead","r_sec_minus_prim_lead"), ("r_primary_lead_sbx","r_primary_lead_sbx"),
             ("r_secondary_lead_sbx","r_secondary_lead_sbx"), ("r_sec_minus_prim_lead_sbx","r_sec_minus_prim_lead_sbx"))),
"pitcher_running_game": SavantBoard(
    name="pitcher_running_game", url=f"{BASE}/leaderboard/pitcher-running-game",
    season_style="snake", table="raw.savant_pitcher_running_game",
    player_column="player_id", season_column="start_year",
    extra={"n": "1"},        # every pitcher with one chance (849 vs 496)
    columns=(# the same twelve, with runs_prevented_on_running_attr and n_pitcher_cs_aa)),
RUNNING_BOARDS = ("basestealing", "pitcher_running_game", "baserunning")
# pipeline/etl/savant_loader.py
_INT_TARGETS |= {"n_init", "n_sb", "n_cs", "n_pk", "n_bk"}
```

### 5.2 The profile builder

```python
# _build_baserunner_steal_metrics(seasons, asof)
savant_season = (f"(CASE WHEN r.season = {asof.year} THEN r.season - 1 ELSE r.season END)"
                 if asof is not None else "r.season")
INSERT OR REPLACE INTO derived.baserunner_steal_metrics (
    player_id, season, sample_steal_attempts, sample_first_base_opps,
    steal_attempt_rate, steal_attempt_rate_2b, steal_success_rate, steal_success_rate_2b,
    below_minimum_sample, asof_date,
    lead_primary_ft, lead_secondary_ft, lead_jump_ft, savant_steal_opps)
WITH clean, attempts, attempt_agg, pa_state, opp_1b, opp_2b AS (… unchanged …),
runners AS (SELECT player_id, season FROM attempt_agg
            UNION SELECT player_id, season FROM opp_1b
            UNION SELECT player_id, season FROM opp_2b),          -- NEW driver: had a chance
savant AS (SELECT player_id, season, r_primary_lead, r_secondary_lead, r_sec_minus_prim_lead, n_init
           FROM pg.raw.savant_basestealing)
SELECT r.player_id, r.season, COALESCE(a.n_attempts, 0), COALESCE(o1.opps_1b, 0),
       COALESCE(a.n_attempts, 0)    * 1.0 / NULLIF(o1.opps_1b, 0),   -- 0.0 when he never went
       COALESCE(a.n_attempts_2b, 0) * 1.0 / NULLIF(o2.opps_2b, 0),
       a.n_success    * 1.0 / NULLIF(a.n_attempts, 0),               -- NULL when he never went
       a.n_success_2b * 1.0 / NULLIF(a.n_attempts_2b, 0),
       (COALESCE(a.n_attempts, 0) < 10), DATE '{asof_sql}',
       sv.r_primary_lead, sv.r_secondary_lead, sv.r_sec_minus_prim_lead, sv.n_init
FROM runners r
LEFT JOIN attempt_agg a ON … LEFT JOIN opp_1b o1 ON … LEFT JOIN opp_2b o2 ON …
LEFT JOIN savant sv ON sv.player_id = r.player_id AND sv.season = {savant_season}

# _build_pitcher_steal_metrics: + the four columns in the explicit list;
#   LEFT JOIN pg.raw.savant_pitcher_running_game sv ON sv.player_id = p.pitcher_id AND sv.season = {savant_season}
#   SELECT …, sv.r_primary_lead, sv.r_secondary_lead, sv.r_sec_minus_prim_lead, sv.n_init

# _compute_baserunner_profiles (POSITIONAL): after asof_date, in XB_COLUMN_ORDER
XB_COLUMN_ORDER = ("xb_opportunities", "xb_attempt_rate", "xb_expected_attempt_rate", "xb_attempt_rate_above_expected")
BASERUNNER_TAIL_COLUMNS = ("asof_date", *XB_COLUMN_ORDER)
#   …, DATE '{asof_sql}' AS asof_date,
#   sbr.n_opp_xb, sbr.rate_att_xb, sbr.est_rate_att_generic_runner,
#   sbr.rate_att_xb - sbr.est_rate_att_generic_runner
#   LEFT JOIN pg.raw.savant_baserunning sbr ON ap.player_id = sbr.player_id AND sbr.season = {sprint_speed_season}

# LeagueAverageProfiles.compute — THREE changes (Finding 3):
#   'baserunner' JSON gains 'xb_attempt_rate_above_expected', AVG(xb_attempt_rate_above_expected)
#   NEW row 'baserunner_steal': steal_attempt_rate, steal_attempt_rate_2b, steal_success_rate,
#       steal_success_rate_2b, lead_primary_ft, lead_jump_ft   (AVG over rows WHERE NOT below_minimum_sample)
#   NEW row 'pitcher_steal':    sb_against_per_9, cs_rate_forced, steal_attempt_rate_allowed,
#       lead_allowed_primary_ft, lead_allowed_jump_ft
#   The two engines already read these entity types; the rows have simply never existed.
# Leakage assertions: no new check (no date to check; the season shift is the guard).
```

### 5.3 The steal-runner model

```python
LEAD_FEATURES = [("lead_primary_ft", 0.735), ("lead_jump_ft", 0.823)]   # weight = repeat on qualified rows
WEIGHT_TENDENCY, WEIGHT_LEAD, WEIGHT_SUCCESS = 0.45, 0.45, 0.10          # §6; decided 2026-09-16
RBF_SIGMA_LEAD = 1.0        # placeholder until the fitted sigma_baserunner_steal_lead is copied in
EB_N_PRIOR_OPPS = 50        # confidence basis = first-base opportunities (Finding 2)

BaserunnerStealProfile: + lead_vec, has_lead      SimilarityResult: + lead_score
FeatureNormalizer:      + lead_mean/std, normalize_lead (nanmean/nanstd already NaN-safe)

_load_profiles:
    lead_sql = "bss.lead_primary_ft, bss.lead_jump_ft" if present else "NULL, NULL"   # information_schema guard
    _v = lambda *vals: np.array([np.nan if v is None else float(v) for v in vals])    # NULL → NaN, never 0.0
    has_lead = isfinite(lead_vec).all();  eb_alpha = shrinkage.alpha(n_opps)          # was n_attempts
_load_league_averages: + the "lead" group from the new 'baserunner_steal' row
_apply_shrinkage: + ("lead", "lead_vec") — a thin runner's lead is pulled toward the league mean
                  (alpha 0.1 at 5 first-base PAs; 0.86 at 300), and a NaN becomes the league mean

StealPartition.build: + _lead_mat, + _lead_has
score_all:
    t, l, s = tend.score_batch(...), lead.score_batch(...), succ.score_batch(...)
    both = query.has_lead & self._lead_has
    full    = wT*t + wL*l + wS*s
    without = (wT*t + wS*s) / (wT + wS)          # a pair missing the lead on either side: score what both have
    composite = clip(where(both, full, without) * sqrt(minimum(query.eb_alpha, alphas)), 0, 1)
query_pair: the same arithmetic, scalar (a test holds the two paths equal)
apply_calibration: + _sig("sigma_baserunner_steal_lead", …)
```

Side effect: a NULL `steal_attempt_rate_2b` (never began a PA on second) now reads as
unmeasured and neutral instead of "never steals third". A small correction.

### 5.4 The pitcher-hold model

```python
HOLD_FEATURES = [("lead_allowed_primary_ft", 0.43), ("lead_allowed_jump_ft", 0.895)]
WEIGHT_OUTCOME, WEIGHT_HOLD = 0.35, 0.65     # §6; decided 2026-09-16
RBF_SIGMA_HOLD = 1.0                         # placeholder until the fitted sigma_pitcher_steal_hold is copied in
PitcherStealProfile: + hold_vec, has_hold    SimilarityResult: + hold_score
_load_profiles: the two columns through the guard; NULL → NaN
_load_league_averages / _apply_shrinkage: + the "hold" group from the new 'pitcher_steal' row
    (confidence stays sample_baserunner_events with prior 25: a 20-event reliever's lead allowed shrinks 56%)
score_all / query_pair: composite = where(both, wO*o + wH*h, o) * sqrt(min alpha), clipped
apply_calibration: + _sig("sigma_pitcher_steal_hold", …)
# pickoff_rate / stepoff_rate stay stored and unread (pickoffs repeat at 0.3)
```

### 5.5 The advancement-runner model

```python
AGGRESSION_FEATURES += [("xb_attempt_rate_above_expected", 0.76)]   # the group split 0.35/0.40/0.25 unchanged
_load_profiles: + brm.xb_attempt_rate_above_expected through the guard; THIS feature NULL → NaN
# shrinkage: the 'baserunner' league row gains the key, so a 5-chance runner (alpha 0.25 at prior 15)
# is pulled three quarters of the way to the league mean before scoring
class WeightedRBFSimilarity:
    def score_batch(self, query, candidates):
        diff = candidates - query[None, :]
        present = np.isfinite(diff)                         # a feature missing on either side drops out …
        d2 = np.where(present, diff * diff, 0.0)
        wsum = (self.weights[None, :] * present).sum(axis=1)     # … of the distance AND its normalisation
        dist_sq = (self.weights[None, :] * d2).sum(axis=1) / np.maximum(wsum, 1e-12)
        return np.exp(-self.gamma * dist_sq)                # a fully-measured pair gets exactly today's number
```

### 5.6 Calibration

```python
CalibrationReport: + sigma_baserunner_steal_lead: float = 0.0   + sigma_pitcher_steal_hold: float = 0.0
_calibrate_baserunner_steal_params: SELECT + lead_primary_ft, lead_jump_ft; NaN for None;
    fit over rows where both are finite (the SIM-530 measured-rows rule) → sigma_baserunner_steal_lead
_calibrate_pitcher_steal_params: the mirror → sigma_pitcher_steal_hold
_calibrate_baserunner_params: the aggression SELECT gains the new column; NULL rows dropped from that fit
summary(): + "BR-steal lead", + "P-steal hold"
```

### 5.7 The recompute script (`scripts/sim531_runner_recompute.py`)

The `sim427_manager_recompute.py` pattern; the app must be stopped (the DuckDB writer
lock, SIM-524): apply 0029 (idempotent); `_compute_baserunner_profiles`,
`_build_baserunner_steal_metrics`, `_build_pitcher_steal_metrics` for the seasons;
`LeagueAverageProfiles.compute` (now writes the two new rows); verify rows per season,
the Savant coverage share (expect ~640 / 850 / 630 of the season's rows), the two new
league-average rows, three named players value for value against the CSV (e.g. runner
691026: primary 12.752, jump 3.289 in 2024; pitcher 676775: 11.629 / 4.240; extra bases
691026: 60/152 = 0.395 vs 0.346 expected), the zero-attempt row count, and the
baserunner tail order.

---

## 6. Weights, bandwidths, powers

### 6.1 Sub-score weights (the reliability-proportional rule the owner approved 2026-09-10)

Set from the qualified-row repeats in §2.2. A weight measures how repeatable the trait is
when it is well measured; the extra noise of a thin row is removed by shrinkage before
scoring, not by lowering the weight for everyone.

| Model | Group | Basis | Mean repeat | Share | Proposed | Today |
|---|---|---|---|---|---|---|
| Steal runner | Tendency | attempts per chance | 0.78 | 0.446 | 0.45 | 0.615 |
| Steal runner | Lead | primary 0.735, jump 0.823 | 0.78 | 0.445 | 0.45 | — |
| Steal runner | Success | success rate (≥10 attempts) | 0.19 | 0.109 | 0.10 | 0.385 |
| Pitcher hold | Outcome | run value 0.45, CS 0.15, attempts allowed 0.46 | 0.35 | 0.35 | 0.35 | 1.00 |
| Pitcher hold | Hold | primary 0.43, jump 0.895 | 0.66 | 0.65 | 0.65 | — |
| Advancement | Aggression feature | attempt rate above expectation | 0.76 | — | 0.76 | — |

The steal runner's original design (before the 2026-05 reconciliation removed its jump
sub-score) was Tendency 0.40 / Jump 0.35 / Success 0.25; the Lead group fills that hole.
That was the conservative alternative; the owner chose the measured split on 2026-09-16 (§10).

### 6.2 Bandwidths

Module default 1.0 per new group; `make calibrate` fits to the 0.50 median target over
measured rows; the two fitted values are copied into the module defaults before the
matrix rebuild (Finding 4).

### 6.3 Powers

12 / 12 / 20 stay for the first landing. A sharper score at the same power concentrates
the draw more, so the power may need to come down. `scripts/sim523_power_scan.py`
(already covers the three factors) re-reads the tier-spread ratio at 1, 2, 3, 5, 8, 12,
20; `scripts/sim523_fit_probe.py` confirms in the loop. A power moves only on that
read, in its own commit.

---

## 7. Tests (`tests/unit/test_sim531_lead_distance.py` unless named)

- Loader (three already landed in the SIM-528 file): every board's minimum is set;
  `n=0` is never sent; baserunning sends `n=1` and nothing else. New: both new boards
  resolve, sit in `RUNNING_BOARDS` and send `n=1`; snake query; a row whose
  `start_year` differs raises; count columns coerce to int; a quoted lead to float; a
  blank lead to None.
- Migrations: 0029 adds the twelve columns in order; the canonical schema agrees; the
  version file reads 29; the baserunner tail equals `BASERUNNER_TAIL_COLUMNS`; Alembic
  0026 upgrades/downgrades.
- Builder (in the SIM-537 file): each join season-shifts under a cutoff and not without;
  the steal INSERT names its four columns and drives from the union of chances; the
  pitcher INSERT names its four; a zero-attempt runner appears with rate 0 and NULL
  success; the league-average writer emits the `baserunner_steal` and `pitcher_steal`
  rows with the lead keys.
- Steal-runner model: weights sum to one; no-lead profiles score over tendency +
  success only, no phantom perfect match; measured pairs use all three; `score_all` ==
  `query_pair`; 0 attempts / 300 opps → confidence 0.857; a 5-PA runner's lead is
  shrunk 90% toward the league mean; NULL → NaN → neutral; sigma sentinel / applied.
- Pitcher-hold model: the mirror set.
- Advancement model: the masked kernel drops a missing feature from distance and
  normalisation; a fully-measured pair is unchanged; a half-missing pair is not inflated.
- Calibration: the two sigmas round-trip; fits use measured rows; sentinel when none.
- `make test-regression`: the invariants hold with the new sub-scores (no golden files).

---

## 8. Run book

**Superseded in part on 2026-09-16 (owner ruling, recorded in CLAUDE.md §2b):** no change
waits on a per-change accuracy run. Steps 0 and 8c below (the OFF arm, the ON arm and the
paired read) do NOT run for this ticket; the lead-distance models land ON at their
best-known defaults and their weights are fitted with every other factor in the one
designed experiment that runs when the owner calls the model final. The per-change gates
are the unit and regression lanes and a short sim smoke of the steal and advancement
channels (`scripts/sim_stats.py`, ten games). Steps 2–7 ran on 2026-09-16 (the record is
in `CHANGES.md`). The text below is kept as written for the designed experiment's reader.

Step 0 first: the OFF arm must run on today's bundle before anything is rebuilt, so the
read attributes to the model change alone. **This ticket's pair is cross-bundle by
nature** — the ON arm runs on rebuilt matrices and a refitted calibration file — so
`scripts/sim518_pair_accuracy.py` will refuse the two reports (their `actor_sim`
manifest time and calibration hash differ) and step 8c must pass `--force`; the paired
read records that the calibration file changed between the arms (the two new sigma
fields, the win-probability curve written back). Copying `actor_sim/` aside does not
help the pairing; it only preserves the OFF bundle for a re-run. (Review of 2026-09-16.)

```
# 0. OFF arm on today's bundle (hours; detached)
MSYS_NO_PATHCONV=1 docker compose run -d --rm --name sim531_off -v "$PWD/scripts:/app/scripts" app \
  python scripts/clv_backtest.py --seasons 2024 --iterations 100 --workers 5 --max-games 250 \
  --output /app/scripts/sim531_accuracy_off.json
# 1. gates (the registry fix is already in the tree; the rest of the code lands here)
make lint && make type-check && make test-unit && make test-regression
# 2. Postgres migration (app may stay up)
docker compose run --rm app alembic upgrade head            # → 0026
# 3. load the two new boards AND re-load the three whose minimum was lifted (1–3 min per board-season)
docker compose run --rm app python -m pipeline.etl.savant_loader \
  --boards basestealing pitcher_running_game baserunning catcher_throwing first_base_receiving \
  --seasons 2023 2024 2025 2026
#    expect per season about 640 / 850 / 630 / 95 / 150 rows (2024: 638 / 849 / 623 / 94 / 152);
#    the probe passes; a count near the qualified figure (432 / 496 / 305 / 66 / 42) means n=1 was lost
#    verify: SELECT season, COUNT(*), MIN(n_init) FROM raw.savant_basestealing GROUP BY 1 ORDER BY 1;
# 4. stop the app; apply 0028 THEN 0029 (the live DB never applied 0028 — no asof_date on the
#    five tables, checked 2026-09-16); rebuild the three runner tables + the league averages (minutes)
docker compose stop app
MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" app \
  python scripts/sim531_runner_recompute.py --seasons 2023 2024 2025 2026
# 5. fit; copy sigma_baserunner_steal_lead + sigma_pitcher_steal_hold into the module defaults (one commit)
make calibrate
#    ⚠ make calibrate DROPS the win-prob reliability curve (SIM-427 trap): write it back from /data/prop_validation.json
# 6. the three matrices + the concentration report (p90 own-staff ratio < 3.0)
docker compose run --rm app python -m pipeline.batch.engine_artifacts --what actors_sim \
  --matrix runner_steal --matrix pitcher_steal --matrix runner_adv
# 7. start; boot log "build_all_engines: 11/11" + the calibration lines
docker compose start app
# 8. checks: (a) power scan, (b) fit probe, (c) ON arm + sim518_pair_accuracy.py --force (the pair is
#    cross-bundle: the matrices and the calibration file both change), (d) optional 45×130 lane
# 9. close: CHANGES.md; delete the SIM-531 row; the three docs
```

The wider catcher-throwing and first-base rows loaded in step 3 reach the catcher and
fielder profiles only at the next full `make profile-computor` (five hours; their
sections build on temp tables the runner script does not). Schedule that rebuild with
the Finding 1 fix (SIM-550), whose data run needs it anyway, so one read attributes both.

"The standard accuracy checks still pass" = the gates green; the paired read not worse
on any market beyond its bootstrap range; the stolen-base prop is the read to lead with.
The joint fit (SIM-548) is paused until the model is final; this ticket is one of the
changes it waits for.

---

## 9. What could go wrong (ranked)

1. The positional insert writes values into the wrong columns (silent) — the verify
   block and the tail-order test.
2. A missing lead reads as zero — the NaN rule has a test per model.
3. A thin row's lead is read at face value — the two league-average rows plus the
   opportunity confidence; a test shrinks a 5-PA runner 90%. Without them the `n=1`
   load would make the steal draw noisier, not sharper.
4. The OFF arm runs on the rebuilt bundle — step 0; the pairing script refuses mismatches.
5. A sharper runner score over-concentrates the steal draw at power 12 — the scan and
   the probe read it first.
6. `n=1` is lost on a re-load and the qualified default comes back silently — the row
   count check in step 3; the registry test forbids `n=0`.
7. A runner's first measured season at a backtest cutoff reads neutral — intended.
8. The pool window widens past four seasons — re-fit.

---

## 10. Decisions taken (owner, 2026-09-16)

All four recommendations were approved. Each is recorded as the ruling the build
follows, with the alternative that was not taken.

1. **The steal runner's weights.** Decided: the measured split 0.45 / 0.45 / 0.10.
   Not taken: restore the original 0.40 / 0.35 / 0.25.
2. **The pitcher's weights.** Decided: Outcome 0.35 / Hold 0.65. Not taken: 0.50 / 0.50.
3. **Runners who never went.** Decided: give them a steal profile and move the
   confidence basis to opportunities. Not taken: leave them unscored, as today. (With
   the `n=1` load the confidence change was in any case required for the runners who
   DID go but rarely: their leads must be shrunk by the same confidence.)
4. **The grade.** Decided: the paired accuracy comparison on 250 games of 2024, with the
   power scan and the fit probe as the diagnostic read. The 45 × 130 lane is not
   required (the ruling of 2026-09-10); run-book step 8d stays optional.

Also recorded: the ruling of 2026-09-16 that every Savant pull uses the smallest minimum
the endpoint honours. The registry change is in the working tree, uncommitted; the three
affected boards need the re-load in step 3.

Filed: the outfield arm-block defect (Finding 1) is SIM-550 (P2) in `BACKLOG.xlsx` as of
2026-09-16; the subtitle now reads SIM-551. Its fix and the full profile rebuild it needs
are scheduled together, after this ticket (§8).
