# Build plan — the Savant loader, the empty measurement blocks, and the batter swing features

> **STATUS 2026-09-10 — BUILT. Read this banner before the plan below it.**
>
> The three owner decisions came back as: build in the order 528 → 530 → 529;
> **split a switch hitter's two sides into separate rows** (this overrode the
> recommendation in §5.4, so the physical features are platoon-aware from the
> start); and fit the sub-score weights reliability-proportionally.
>
> **Landed and verified:** the shared loader with its four season-parameter
> styles and both season guards; eight raw tables (Alembic 0019, applied);
> 21,454 rows loaded for 2023-2026; the outfield and catcher arm joins; the
> batter model's fifth sub-score; DuckDB migration 0025 (applied, schema v25);
> the batter profiles rebuilt for the four pool seasons. Lint, types, the unit
> suite and the regression gate are green.
>
> **Two corrections to this plan, both found by building it.** First-base scoop
> rate was NOT an empty block — §4.1 counted a table-wide null rate that is
> simply every fielder who is not a first baseman, so SIM-530 fills TWO blocks,
> not three. And catcher pop time turned out to be a formula derived from the
> caught-stealing rate rather than a measurement, so §4.3's "do not replace ours"
> is reversed: the build now prefers Savant's measured pop time.
>
> **Still to run:** the fielder and catcher profile sections (they depend on temp
> tables that only a full `make profile-computor` builds), the calibration refit,
> the actor score-matrix rebuild, and one certifying lane per data ticket. See
> §6.

**Date:** 2026-09-10
**Tickets:** SIM-528 (the shared Savant loader), SIM-530 (fill three empty measurement
blocks), SIM-529 (physical swing and stance features for batter matching)
**Source of the evidence:** `docs/audit/2026-09-10-savant-leaderboard-data-audit.md`
**Owner sign-off needed on:** the build order, the switch-hitter decision in §4.3, and the
weight-fitting method in §4.6.

---

## 1. What these three tickets deliver

One shared way to pull Baseball Savant's season summaries, then two uses of it: filling
three measurement blocks that are empty today, and giving the batter-matching model physical
swing data instead of only outcome rates.

**I recommend building them in the order 528 → 530 → 529, not the order they are numbered.**

The loader has to come first — both other tickets are joins onto data it fetches. After that,
filling the empty blocks is the smaller and safer job: it changes no database schema, adds no
new model concept, and proves the loader end to end across five different Savant files. The
batter work is the larger change, and it lands against a known-good baseline once the empty
blocks are fixed.

**The two data tickets cannot share one certifying run.** Both change similarity scores, and
similarity scores are draw weights. If both land at once and a band moves, we cannot say which
one moved it. Each needs its own 45-game × 130-iteration lane. That is the single biggest
constraint on the schedule, and it is the reason the order matters.

If you would rather have the batter work sooner, 529 can move ahead of 530. What cannot happen
is both in one lane.

---

## 2. The ground rules this build works inside

These are existing rulings, not new decisions. They shape every step below.

1. **Every factor is a draw weight, fitted, or it is off.** Nothing here adjusts a play after
   it is drawn. New data changes an engine's similarity score, which changes a draw weight.
2. **The grade is the play pool's own totals**, measured on the balanced 45-game set at 130
   iterations a game (`tests/acceptance/bands.py`, `tests/acceptance/conftest.py`,
   `DEFAULT_GAMES = 45`, `DEFAULT_ITERS = 130`).
3. **No betting-value read until every band is green.** That still waits on the strikeout
   shortfall in the pitch and pitch-result split (SIM-527). This build is upstream of it.
4. **The batter table's insert is positional.** `_compute_batter_profiles` runs
   `INSERT OR REPLACE INTO derived.batter_season_metrics` with **no column list**
   (`pipeline/batch/player_profile_computor.py:2138`). A new column added by
   `ALTER TABLE ... ADD COLUMN` lands at the end of the table, so the final `SELECT` must
   append it in exactly the same order, after `updated_at`. Migration 0024 already carries this
   warning for the pool tables. Getting it wrong silently writes every value into the wrong
   column.
5. **The application holds the database writer lock.** Any profile rebuild needs the stack
   stopped (the DuckDB lock item, SIM-524). Budget for the outage in every run below.

---

## 3. SIM-528 — the shared Savant loader

**No DuckDB schema change. One Alembic migration. No effect on any simulation until a later
ticket joins the data.** This can land and sit inert.

### 3.1 What already exists

`pipeline/etl/etl_sprint_speed_loader.py` is a working example of exactly this job: it fetches
a Savant CSV, guards against unknown players, and upserts into `raw.sprint_speed`. It already
sends a browser-like user agent, which Savant requires, and it already retries with a backoff.
The new loader generalises that file rather than replacing it.

### 3.2 The board registry

Put one declarative entry per Savant file in a new `pipeline/etl/savant_boards.py`:

| Entry | Endpoint | Season parameter style | Row key |
|---|---|---|---|
| `bat_tracking` | `/leaderboard/bat-tracking` | `seasonStart` / `seasonEnd` | player, season |
| `swing_path` | `/leaderboard/bat-tracking/swing-path-attack-angle` | `seasonStart` / `seasonEnd` | player, season |
| `batting_stance` | `/visuals/batting-stance` | `seasonStart` / `seasonEnd` | player, season, batting side |
| `arm_strength` | `/leaderboard/arm-strength` | `year` | player, season |
| `baserunning` | `/leaderboard/baserunning` | `season_start` / `season_end` | player, season |
| `poptime` | `/leaderboard/poptime` | `year` | player, season |
| `catcher_throwing` | `/leaderboard/catcher-throwing` | `season_start` / `season_end` | player, season |
| `first_base_receiving` | `/leaderboard/first-base-scoops-receiving` | `season_start` / `season_end` | player, season |

Each entry carries its endpoint, its season-parameter style, the extra parameters that widen
the result to the full roster, the columns to keep, and the target table.

### 3.3 The three traps the loader must handle

These are confirmed by testing, not assumed.

- **Three season-parameter styles exist, and the wrong one fails silently.** Sending `year=`
  to a board that wants `seasonStart=` returns the *current* season with a 200 status and a
  well-formed CSV. **The loader must assert the season on a returned row against the season it
  asked for, and fail loudly on a mismatch.** This is the single most dangerous failure mode in
  the whole build: it would quietly write 2026 data into a 2023 row.
- **Two boards reject every parameter except the season pair.** Baserunning and Fielding Run
  Value answer only a bare `csv=true` plus `season_start` / `season_end`. Adding `year=`
  returns zero rows or a server error.
- **The default is qualified players only.** `minSwings=0` on the bat-tracking family and
  `min=0` elsewhere widens the result from roughly 215 batters to roughly 650. The loader must
  set these, and should log the row count so a silent narrowing is visible.

### 3.4 The raw tables

One typed table per board, matching the `raw.sprint_speed` pattern: a primary key on
(player, season) — plus batting side for the stance board — a `scraped_at` timestamp, and an
upsert on conflict. Ship as **Alembic migration 0019** (0018 is head).

I considered one generic table holding a board name and a JSON payload. I rejected it: it
pushes type coercion into the profile builder, where a bad cast is much harder to see, and it
makes the DuckDB joins awkward. Typed tables cost more migration lines and are worth it.

Each loader run must keep the sprint-speed loader's foreign-key guard: drop and log rows whose
player is not yet in `raw.players`, rather than failing the batch. Savant publishes players
before our pitch feed has seen them.

### 3.5 Tests

- One test per season-parameter style, against a recorded CSV fixture, asserting the loader
  builds the right query string.
- A test that a returned season different from the requested season **raises**.
- A test that an unknown player is dropped and logged, not fatal.
- A test that an empty response is a clear failure, not a silent zero-row upsert.

No live network calls in the test lane. Record fixtures from the pulls already saved during the
audit.

### 3.6 Done when

The loader fetches all eight boards for 2023 through 2026, the row counts match the audit
(about 650 batters a season for the swing boards, 387 players for arm strength, 99 catchers for
pop time), and every row's season matches what was asked for.

---

## 4. SIM-530 — fill the three empty measurement blocks

**No schema change at all.** Every column already exists in the DuckDB tables. They are written
today as literal `NULL` placeholders. This ticket replaces those placeholders with a join.

### 4.1 Where the placeholders are

| Column block | Written as NULL at | Weighted by |
|---|---|---|
| Outfield arm: `arm_strength`, `arm_opportunities`, `arm_holds`, `arm_hold_rate`, `arm_assists`, `arm_thrown_out_rate`, `arm_advancement_prevention`, `of_arm_runs` | `player_profile_computor.py:4851-4858` | the fielder model's outfield arm group, 30% of an outfielder's score |
| Catcher arm: `arm_strength_mean` | `player_profile_computor.py:5030` | the catcher model's throwing group, feature weight 0.800 |
| First-base receiving: `scoop_success_rate`, `scoop_opportunities` | computed but 87% empty, `player_profile_computor.py:4516` | the infield specialty group |

### 4.2 The mapping

**Outfield arm** — two Savant boards.

| Our column | Savant source | Note |
|---|---|---|
| `arm_strength` | arm strength board, `arm_of` (or `arm_inf` for infielders) | miles per hour |
| `arm_opportunities` | baserunning, `n_opp_xb` | chances a runner had to advance on him |
| `arm_holds` | `n_opp_xb − n_att_xb` | runners who chose not to try |
| `arm_hold_rate` | `1 − rate_att_xb` | the share who held |
| `arm_assists` | baserunning, `n_out` | runners thrown out |
| `arm_thrown_out_rate` | `n_out / n_att_xb` | guard the zero denominator |
| `arm_advancement_prevention` | `est_rate_att_generic_fielder − rate_att_xb` | positive means runners challenge him less than they would a generic fielder |
| `of_arm_runs` | `fielder_runs_hold + fielder_runs_advances + fielder_runs_thrown_out` | run value of the arm |

**One caveat to write into the code as a comment.** Our fielder table is keyed by player,
position and season. The arm strength board is per position, so it joins cleanly. The
baserunning board is per player-season, not per position: a player who plays both corners gets
the same advancement figures on his left-field row and his right-field row. That is acceptable
— an arm does not change between corners — but it must be stated, or a later reader will read
it as a bug.

**Catcher arm** — `arm_strength_mean` from the pop time board's `maxeff_arm_2b_3b_sba`, with
the catcher throwing board's `arm_strength` as a fallback for catchers the pop time board
omits.

**First-base receiving** — `scoop_success_rate` from `outs_scoop / n_scoop` and
`scoop_opportunities` from `n_scoop`.

### 4.3 Two things I am deliberately not doing here

**Exchange time stays out of the model for now.** The 2026-05-29 schema reconciliation deleted
the catcher's exchange-time sub-score because the column did not exist. Savant publishes it.
Restoring a deleted sub-score is a modelling decision with its own weight to fit, not a data
fix. It belongs in its own ticket. This plan stores nothing for it.

**Savant's pop time does not replace ours.** We already compute `pop_time_mean` from our own
data, and the values look right (2.13 to 2.21 seconds). Use Savant's `pop_2b_sba` as a
one-off cross-check during the build, and do not store it. Two columns measuring the same thing
invite the wrong one being read.

### 4.4 What changes downstream, and what does not

Filling these columns changes the outfielder's and the catcher's similarity scores, which
changes the fielding draw and the steal draw. **Today the missing values are treated as
neutral** — the model's kernel maps a missing feature to zero distance, so every pair of
outfielders currently scores a perfect match on the arm group. Filling it will move scores by a
lot, not a little. That is the point of the ticket, and it is also the risk.

There is one stale copy to be aware of. `sim.stolen_base_pool` denormalises
`catcher_arm_strength` from the catcher profile at build time. Filling the profile does not
update that copy. The engine that actually weights the steal draw reads the profile table
directly, so the draw is correct either way — but if anything is ever built on that pool
column, it needs a pool rebuild.

### 4.5 The calibration consequence

`sigma_of_arm` and `sigma_catcher_throwing` exist in the calibration report today, but they
were fitted over columns that were entirely empty, so they will have fallen back to the
engine's tuned default through the degenerate-value guard. **Once real data is present, the
calibration fit must be re-run** (`make calibrate`) so these two get a real bandwidth. Skipping
this leaves a real feature scored with a placeholder setting.

### 4.6 Tests and gates

- The catcher model **has a golden-file regression fixture** (`tests/regression/fixtures/catcher.json`).
  Filling the arm column **will break it**, correctly. Regenerate deliberately with
  `python tests/regression/generate_fixtures.py --force` and review the diff before accepting.
- The fielder model has **no** golden fixture. Neither does the batter model. Only five engines
  are covered (baserunner steal, catcher, manager, pitcher steal, situation). **I recommend
  adding a fielder fixture as part of this ticket**, because this is the change that makes the
  outfield arm group live for the first time, and nothing else would catch a later regression
  in it.
- A unit test asserting that a fielder-season with no Savant row keeps a null arm block rather
  than a zero. A zero would read as "worst arm in the league".
- The 45 × 130 lane, run once, attributed to this ticket alone.

### 4.7 Done when

The three blocks hold real values for 2023 through 2026, the calibration report carries a
fitted bandwidth for the outfield arm and the catcher throwing groups, the catcher golden file
is regenerated with a reviewed diff, and the lane is green.

---

## 5. SIM-529 — physical swing and stance features for batter matching

The larger change. One DuckDB migration, one new sub-score inside the batter model, and a
weight fit.

### 5.1 The ten columns

From the audit's evidence. Both tests had to pass: the measurement repeats year to year, and it
is not a restatement of something we already read.

| New column | Savant source | Repeats year to year | Overlap with what we read |
|---|---|---|---|
| `stance_foot_sep` | batting stance | 0.855 | 0.087 |
| `stance_angle` | batting stance | 0.833 | 0.109 |
| `stance_batter_y` (depth in the box) | batting stance | 0.886 | 0.123 |
| `stance_batter_x` (distance off the plate) | batting stance | 0.863 | 0.246 |
| `swing_tilt` | swing path | 0.903 | 0.240 |
| `attack_angle` | swing path | 0.718 | 0.597 |
| `intercept_y_vs_plate` (contact depth) | swing path | 0.792 | 0.271 |
| `attack_direction` | swing path | 0.603 | 0.526 |
| `avg_bat_speed` | bat tracking | 0.901 | 0.765 |
| `swing_length` | bat tracking | 0.882 | 0.421 |

Plus `sample_competitive_swings` as the denominator, and `stance_side` recording which batting
side the stance row came from.

**Explicitly excluded, on evidence:** whiff per swing (a 0.983 duplicate of our contact rate),
the blast and squared-up rates (largely restate hard-hit rate and strikeout rate), hard-swing
rate (Savant derives it from bat speed, so adding both double-counts), percent of swings
competitive (year-to-year correlation 0.003 — noise), swords and batter run value (outcome
summaries, which this model deliberately avoids).

### 5.2 The migration

DuckDB migration **0025**, schema version **24 → 25**. `ALTER TABLE ... ADD COLUMN IF NOT
EXISTS` only, so it is non-destructive and existing rows hold null until a rebuild.

Remember to bump `db/schemas/duckdb_schema_version.txt` **and** mirror the columns into
`db/schemas/02_duckdb_schema.sql`. A past sprint bumped a migration and forgot the version
file.

### 5.3 The profile builder, and the trap

`_compute_batter_profiles` at `player_profile_computor.py:2133`. Add a `LEFT JOIN` to the three
new raw tables, keyed on batter and season, in the pattern the sprint-speed join already uses.

**The insert is positional.** Append the new columns to the final `SELECT` **after**
`updated_at`, in exactly the order the migration adds them. Verify by selecting three known
players after the first rebuild and checking the values are in the columns they belong to — not
by trusting the column count.

### 5.4 The switch-hitter decision — needs your call

The stance board emits **one row per batter per batting side**. A switch hitter has two, and
they differ: José Ramírez's stance angle is −8.7 batting left and −5.5 batting right.

There is a neat mapping available, because a switch hitter's batting side is decided by the
pitcher's hand. A left-side stance row is the stance he uses against a right-handed pitcher.
So the stance columns could live in the platoon block as `_vs_l` and `_vs_r` pairs, and slot
straight into the model's existing platoon design.

**My recommendation for this ticket: do not do that yet.** Store one row per batter-season,
using the side with the most competitive swings, and record `stance_side` so the platoon split
can be built later without re-deriving anything. The audit's measurement supports this: the
pitcher-hand split is real, but a chunk of the difference is small-sample noise on the
versus-left side, and separating the two is its own piece of work.

If you would rather have the platoon split in the first landing, say so — it roughly doubles
the column count and adds a fit, and I would want it as its own ticket rather than folded in.

### 5.5 The model changes

Seven places in `similarity/engines/batter_similarity.py`, in this order:

1. **The feature list and weights** — a `PHYSICAL_FEATURES` list with per-feature reliability
   weights, plus `WEIGHT_PHYSICAL` and `RBF_SIGMA_PHYSICAL`. The four existing sub-score
   weights must be reduced so the five still sum to 1.0. There is an assertion on this at
   module level; it will catch an arithmetic slip.
2. **`BatterProfile`** — a `physical_vec` field.
3. **`SimilarityResult`** — a `physical_score` field, so the new sub-score is visible in query
   output and not just buried in the composite.
4. **`FeatureNormalizer`** — fit the new group and add `normalize_physical`.
5. **`_load_profiles`** — select the new columns through the existing `_opt()` guard, which
   emits `NULL AS <name>` when the column is absent. This matters: it means the model still
   builds against a database that has not been rebuilt yet, so the code can land before the
   data run.
6. **`score_all` and `_score_pair`** — add the term to the composite. `score_all` is the
   vectorized path and is the one the nightly matrix build uses; `_score_pair` is the
   pair-at-a-time path. **Both must change, and they must agree.** A test comparing the two on
   the same pair is the cheapest guard against them drifting.
7. **`apply_calibration`** — read `sigma_physical` from the calibration report.

Then in `similarity/similarity_calibration.py`: add `sigma_physical` and
`reliability_weights_physical` to the report, add the sub-calibrator, and add the line to the
report's printed summary. Keep the degenerate-value guard, so a group that cannot be fitted
falls back to the tuned default rather than to a spurious 1.0.

### 5.6 Fitting the weight — needs your call

The new sub-score needs a weight relative to the four existing ones, and the four existing ones
have to give up room. Two ways to choose it:

- **Reliability-proportional.** Set the per-feature weights from the year-to-year correlations
  already measured, and set the group weight from the group's average reliability against the
  others. This is the method the model's existing weights document themselves as using, so it
  is consistent with what is there.
- **Fitted against the pool's own conditional rates**, the way the play-picker redesign fitted
  every actor factor.

The second is more rigorous and much slower. **I recommend the first for the initial landing**,
because the physical group is a similarity input rather than a draw factor in its own right,
and because the certifying lane will catch a weight that distorts the run environment. Say if
you want the second.

### 5.7 What has to rebuild

Both play pools already carry the batter and the season on every row, and the batter's
similarity score is a lookup in a nightly matrix keyed on exactly that. **No pool rebuild is
needed.** The chain is the profile rebuild, then the actor score matrices:

```
make profile-computor    # writes derived.batter_season_metrics
make calibrate           # fits sigma_physical
make engine-artifacts FLAGS="--what actors_sim"
```

The matrix build writes a concentration report and warns when any matrix puts more than three
times its natural share of a draw on the live player's own team. **Read that report.** A
similarity model that has quietly learned team identity would show up there, and physical
swing data is exactly the kind of input that could carry a coaching-staff signature.

### 5.8 Tests and gates

- A unit test that a batter-season with no Savant row scores neutrally rather than as an
  extreme, using the existing missing-value handling.
- A test that `score_all` and `_score_pair` agree on the same pair.
- A test that the five sub-score weights sum to 1.0 in both the plain and the platoon-context
  weight sets.
- **The batter model has no golden-file regression fixture.** I recommend adding one in this
  ticket. Without it, the only thing standing between a future batter-model regression and
  production is the acceptance lane, which is expensive and runs late.
- The 45 × 130 lane, run once, attributed to this ticket alone.

### 5.9 Done when

Ten columns hold real values for 2023 through 2026, the model scores a fifth sub-score, the
calibration report carries a fitted `sigma_physical`, the concentration report is inside its
threshold, and the lane is green.

---

## 6. The run book

Each data run needs the application stopped, because it holds the database writer lock.

**After SIM-528 lands** — no simulation effect, so no lane:

```
docker compose stop app
python -m pipeline.etl.savant_loader --boards all --seasons 2023 2024 2025 2026
docker compose start app
```

**After SIM-530 lands:**

```
docker compose stop app
make profile-computor      # fielder + catcher sections pick up the joins
make calibrate             # sigma_of_arm and sigma_catcher_throwing get real values
make engine-artifacts FLAGS="--what actors_sim"
docker compose start app
python tests/regression/generate_fixtures.py --force   # review the catcher diff
```

Then the lane. Then stop and read it before starting SIM-529.

**After SIM-529 lands:** the same shape, with the migration first and the positional-insert
check straight after the profile rebuild.

---

## 7. What could go wrong

Ranked by how much damage it does before anyone notices.

1. **The positional insert writes values into the wrong columns.** Silent, and it corrupts
   every batter profile. Mitigation: check three known players by name after the first rebuild,
   against the Savant CSV, before anything else runs.
2. **A season-parameter mistake writes the current season into a past season's row.** Silent,
   returns HTTP 200. Mitigation: the loader asserts the season on a returned row.
3. **A missing Savant row is stored as zero instead of null.** A batter with no bat-tracking row
   would look like the slowest swing in the league. Mitigation: an explicit test per block.
4. **Both data tickets land in one lane and a band moves.** Not damaging, but it costs a second
   full lane to work out which one did it. Mitigation: the build order in §1.
5. **The pool window widens past four seasons later.** Savant publishes none of this before
   2023. The physical columns would go null for the oldest season. The model's missing-value
   handling covers it, but the fit would be measuring a different population. Mitigation: note
   it against the window setting, and re-fit if the window ever changes.

---

## 8. What I need from you before starting

1. **The build order.** I recommend 528 → 530 → 529. Say if you want the batter work first.
2. **The switch-hitter decision (§5.4).** I recommend storing one row per batter-season now and
   deferring the platoon split to its own ticket.
3. **The weight-fitting method (§5.6).** I recommend reliability-proportional for the first
   landing, with the certifying lane as the guard.

Two smaller recommendations that do not need a decision, only an objection if you disagree:
adding golden-file regression coverage for the batter and fielder models as part of the
tickets that first make those groups live, and leaving the catcher's exchange time out of this
build as a separate modelling ticket.
