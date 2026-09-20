# Decision record and build plan — pitcher arm angle and spin shape (SIM-533)

> **STATUS 2026-09-19 — BUILT AND CLOSED: all three decisions TAKEN as recommended (§10), and the record
> landed the same day** (the decision block beside `GMM_FEATURE_NAMES` in the pitcher engine, one paragraph
> in `docs/technical/similarity.md` and one bullet in the cheat sheet, the one-line correction in the board
> audit, the guard `tests/unit/test_sim533_arm_angle_decision.py`, the probe
> `scripts/sim533_arm_angle_probe.py` with an `--offline` fixture; the SIM-533 row deleted; the record in
> `CHANGES.md`). No migration, no profile column, no recompute, no matrix, no restart. One fact the design
> missed and the build records: Savant's per-pitch arm angle already sits, unread, in
> `raw.savant_pitch_tracking.arm_angle` (Alembic 0020 carried it "because a later ticket wants it" — this
> one; 2.02 million of 2.12 million rows of 2023–2026 carry a value). The decision stands: the guard holds
> that no profile computor, engine or artifact builder reads it. The readable page is
> https://claude.ai/artifact/2cGe6u1p8wss8U1UyoTKAw (the design; this file carries the build stamp).
> The recommendation is a decision, not a data build: neither Savant number joins the pitcher
> model, and the ticket closes on the record below. The alternative build is specified in
> full (§4 and §5) so the owner can take it instead.
>
> **The ticket's premise does not hold.** The ticket says the pitcher model "deliberately
> leaves out where the ball is released". It does not. The arsenal fingerprint reads the
> release point of every pitch (release x, release z, extension are three of its eight
> dimensions); what the model dropped in 2026 was a SECOND, separate release sub-score that
> double-counted them. Savant's arm angle is that same release point measured from the
> pitcher's shoulder instead of from the ground, and its spin-shape numbers are functions of
> the movement, velocity, spin rate and spin axis the fingerprint already holds. Measured on
> 68,000 same-hand pitcher pairs, the part of the arm angle our model cannot see explains
> under 1% of the pitchers' outcome differences beyond the score they already get.

**Date:** 2026-09-18
**Ticket:** SIM-533 in `BACKLOG.xlsx` (the row carries priority "x", unranked). Next free ID: SIM-552.
**Evidence:** Savant's arm-angle, active-spin and spin-direction boards pulled for 2017 to
2025 with their parameters probed live; a read-only count of every pitch's arsenal columns
for 2017 to 2026 and two aggregate dumps from `raw.pitches`; the pitcher outcome profiles and
the production pitcher-similarity matrix (2,464 pitcher-seasons, 2023 to 2026) from the live
stack; the pitcher engine and the profile computor as they stand at commit 0c77ed0.
**Builds on:** the pitcher engine's SIM-067 record (the release sub-score removed as a
duplicate), `docs/audit/2026-09-10-savant-leaderboard-data-audit.md` (the board audit that
filed this ticket), the owner ruling of 2026-09-16 that every similarity-score power is 1
until the comprehensive sweep, and the SIM-550 / SIM-532 lesson that a candidate feature is
tested for what it adds, not only for whether it repeats.

---

## 0. The short version

**What the ticket asks for.** Two physical descriptions of a pitcher that Savant publishes
and we do not compute: his arm angle (degrees above horizontal at release) and the shape of
his spin (how much of each pitch's spin moves the ball, and where the measured spin axis
sits against the axis the movement implies). The definition of done is a recorded decision
on whether arm angle belongs in the pitcher model, and any adopted measurement stored,
weighted and checked.

**What the measurements say.** I answered three questions the same way as the lead-distance,
arm-block and outfield-jump work, plus a fourth that those tickets taught us to ask.

- Does our model already hold it? Yes, in substance. Savant's arm angle is exactly the
  angle from the shoulder to the release point (our reconstruction from Savant's own
  columns matches to 0.03 degrees). Our per-pitcher mean release point reproduces 73% of
  its variance (81% with the pitcher's height); the rest is where the shoulder sits at
  release, a posture the ball's flight does not carry. Our spin axis IS Savant's measured
  axis (agreement 0.992), Savant's movement-inferred axis is a formula on our movement
  (0.987), active spin follows our movement, velocity and spin rate (r 0.94), and the
  measured-minus-inferred deviation rebuilds from our two axes at r 0.80.
- Does it repeat? Everything here is a trait: arm angle 0.97 year to year, active spin
  0.98, the axis deviation 0.93 to 0.96, our own per-pitch-type means 0.98 to 0.99.
  Stability does not separate these candidates; redundancy does.
- Does it add anything to the score? On 67,941 same-hand pairs of 2024, the production
  pitcher score explains 47% of the distance between two pitchers' nine outcome rates. The
  part of the arm angle our model cannot see adds 0.05 points of that; active spin adds
  0.03; the fastball's axis deviation 0.01. On the four outcomes the score does not read
  (ground-ball, fly-ball and line-drive rates, home runs per nine) the score explains 1.9%
  and the strongest candidate, the fastball's axis deviation, adds 0.9 points, unstable
  between two random halves of the pitchers.
- The boards themselves start in 2020. The arm-angle board takes `season=` (asked for
  2025 it lists Kershaw, who has not pitched in 2026); the `year=` spelling that four of our
  registered boards use is ignored without error, and the board then serves the current
  season, the trap the audit warned about. It is also the only one of the three with a date
  control (`dateStart` / `dateEnd`).

**The plan.** Record the decision where the next reader will find it: a note beside the
arsenal's feature list in the pitcher engine, a paragraph in the technical reference and the
cheat sheet, a unit guard that fails if a later change names arm angle or active spin as a
feature without the evidence, and the two probe scripts copied into `scripts/` so the
numbers can be re-run. No migration, no profile column, no recompute, no matrix rebuild, no
restart. Delete the backlog row; the changelog carries the record.

**What it will show.** Nothing changes in production. The one modelling note the evidence
raises is for the comprehensive sweep, not for this ticket: given the score, a larger
arm-angle gap goes with a SMALLER outcome gap, which says the arsenal distance may weight
its release dimensions more than outcomes warrant. The Wasserstein distance treats its eight
standardised dimensions equally and has no per-dimension weight today.

**Decisions** in §10: all three taken as recommended on 2026-09-19.

---

## 1. The mechanism

```
raw.pitches (every pitch, 2017-2026; 99.7% carry all eight arsenal columns in every season)
   velocity · induced vertical break · horizontal break · spin rate · spin axis · release x · release z · extension
      │
      ▼  pipeline/batch/player_profile_computor.py  (_fit_gmm_for_pitcher: a Gaussian mixture per pitcher-season, 2-7 components)
derived.pitcher_season_metrics.gmm_model  +  derived.pitcher_gmm_components (mean and variance per component, all eight)
      │
      ▼  similarity/engines/pitcher_similarity.py
pitcher score = 0.65 × arsenal (Wasserstein-2 between the two mixtures, all eight dimensions z-scored) + 0.35 × command (RBF over seven rates)
      │
      ▼  pipeline/batch/engine_artifacts.py → pitcher_sim.npz (2,464 × 2,464, the pool window)
the pitch draw and the pitch-result draw (power 1) · the pitching change and the reliever "stuff" weight (power 1)

Savant's three numbers, as functions of the eight columns above:
   arm angle            = atan2(release z − shoulder z, |release x − shoulder x|)          ← release z, release x + the SHOULDER (not ours)
   active spin          ≈ f(total movement × velocity / spin rate)                          ← ivb, hb, velocity, spin rate
   axis deviation       = measured spin axis − axis implied by the movement                 ← spin axis, ivb, hb
```

The release sub-score the ticket refers to was a second RBF over the same release columns,
removed by SIM-067 because "release-point information is already inside the per-component
GMM means and was double-counting" (the engine's own record, `score_pair`'s docstring). The
model never stopped reading the release point; it stopped reading it twice.

---

## 2. The evidence

### 2.1 The three boards, as they answer (probed 2026-09-18)

| Board | Season parameter | Rows, 2024 | Smallest minimum | Seasons served | Keys |
|---|---|---|---|---|---|
| Arm angle (`/leaderboard/pitcher-arm-angles`) | `season=` — a **sixth style**, honoured (`season=2025` lists Kershaw, who has not pitched in 2026); the `year=` spelling four of our boards use is **ignored silently**: `year=2025`, `season=2026` and no parameter all return the same 282 current-season rows | 823 with `min=1` (284 qualifiers, 974+ pitches, by default; the page's own `min=q`) | 1 pitch | 2020-2025 (2017-2019 and 1990: an empty body, HTTP 200, the header only); **a date range is honoured** (`dateStart` / `dateEnd`: Verlander 2,608 pitches for 2025, 446 for April) | (pitcher, season) |
| Spin direction (`/leaderboard/spin-direction-pitches`) | `year=` | 3,180 with `min=1` (719 by default, 324+ pitches) | 10 pitches of the type | 2020-2025 (1990: empty); a date range is ignored | (player, season, pitch type) — the type is a CSV column, no split pull |
| Active spin (`/leaderboard/active-spin`) | `year=` | 708, with or without `min` | none honoured | 2020-2025 (2017-2019: empty) | (player, season); nine pitch-type columns |

Columns that matter: the arm-angle board gives `ball_angle` and the four coordinates it is
built from (`relative_release_ball_x`, `release_ball_z`, `relative_shoulder_x`, `shoulder_z`);
the spin-direction board gives, per pitch type, `active_spin`, `hawkeye_measured` (the
measured axis), `movement_inferred` (the axis the movement implies), `diff_measured_inferred`
(their difference, the seam-shifted-wake signal), with the release speed, spin rate and total
movement beside them. The active-spin board repeats the spin-direction board's `active_spin`
as one row per pitcher.

A loader entry for the arm-angle board would need a sixth season style (`season=`), and the
1990 probe would have to accept an empty body with no header as zero rows. The page's own
controls (`min=q` for qualifiers, `minGroupPitches`, `groupBy`, `pitchType`, `pitchHand`,
`dateStart` / `dateEnd`) are accepted; a `groupBy` value returns the HTML page rather than a
CSV, so a per-pitch-type grouping is not available as a file.

### 2.2 What our own data already determines

Matched on the pitcher (and the pitch type where the board is per type), 2024 unless
stated; our figures are per-pitcher-season means from `raw.pitches`.

| Savant number | Our reconstruction | Agreement | Read |
|---|---|---|---|
| Arm angle, from Savant's own four coordinates | `atan2(release z − shoulder z, ǀrelease x − shoulder xǀ)` | r 1.000, mean gap 0.03° (n 625) | the formula is the release point measured from the shoulder |
| Arm angle, from OUR mean release z, ǀrelease xǀ, extension | linear fit, 2023-2025, ≥200 pitches both sides (n 1,906) | R² 0.728, residual 6.7° of a 12.9° spread | the ball's release point carries three quarters of it |
| … plus the pitcher's height | the same fit + height | R² 0.806, residual 5.7° | the rest is the shoulder's position at release (its own spread 0.27 ft; r with height only 0.38): posture, not flight |
| Savant's release z vs our mean release z | | r 0.999 | the same measurement |
| Savant's spin rate, velocity vs ours (per pitch type, n 2,331) | | r 1.000, 1.000 | the same measurements |
| Savant's total movement vs our √(ivb² + hb²) | | r 0.985 | the same measurement |
| Savant's measured spin axis vs our `spin_axis` | mirrored convention (360 − ours) | mean cos 0.992 | our column IS the measured axis |
| Savant's movement-inferred axis vs our ivb / hb | `180 − atan2(hb, ivb)` | mean cos 0.987 | a formula on two columns we hold |
| Active spin vs our movement × velocity / spin rate | the physical proxy | r 0.942; a log fit R² 0.851 (0.876 on Savant's own inputs, the ceiling of that fit) | a function of three columns we hold |
| The axis deviation (measured − inferred) | our spin axis − our movement axis | r 0.803 (a linear fit on the raw columns reads only 0.153: the relation is angular, not linear) | a function of three columns we hold; the mixture's full covariance sees the pair jointly |

### 2.3 Which numbers repeat, year to year

| Number | 2023→24 | 2024→25 | Basis |
|---|---|---|---|
| Savant arm angle | 0.972 | 0.972 | ≥200 pitches both seasons (n 436 / 431) |
| Our mean release z | 0.979 | 0.977 | the same pitchers |
| Our geometric proxy for the angle | 0.944 | 0.952 | the same pitchers |
| Active spin, per pitcher × pitch type | 0.978 | 0.978 | ≥100 pitches of the type both seasons (n 1,148 / 1,164) |
| The axis deviation, per pitcher × pitch type | 0.929 | 0.961 | the same rows |
| Our per-pitch-type means: velocity, ivb, hb, spin rate, release z | 0.976-0.992 | 0.975-0.992 | the same rows |

Every candidate is a stable physical trait. So is everything the arsenal already reads. The
repeat test, which decided the outfield jump and the hand split, cannot decide this ticket.

### 2.4 The outcome test: what each number adds to the production score

Same-hand pairs of 2024 pitchers with 500 or more pitches, an arm angle and a row in the
production similarity matrix: 475 pitchers, 67,941 pairs (59,685 right-handed, 8,256
left-handed). For each pair: one minus the production score; the distance between the two
pitchers' z-scored outcome rates; the gap in each candidate. A linear fit of the outcome
distance on the score, then with each candidate added. The gain is the added R².

**Outcomes the score does not read** (ground-ball, fly-ball and line-drive rates, home runs
per nine; the score alone explains R² 0.019, r 0.137):

| Added to the score | Gain in R² | Standardised coefficient | Read |
|---|---|---|---|
| ǀΔ arm angleǀ | +0.0045 | +0.07 | tiny; +0.0099 on one random half of the pitchers, +0.0017 on the other |
| ǀΔ the shoulder part of the angleǀ (the residual after our release point + height) | +0.0056 | +0.08 | the only genuinely new information; tiny |
| ǀΔ heightǀ | +0.0000 | −0.01 | nothing |
| ǀΔ fastball active spinǀ | +0.0000 | +0.00 | nothing |
| ǀΔ fastball axis deviationǀ | +0.0086 | +0.09 | the strongest candidate; under one point |
| ǀΔ breaking-ball active spinǀ | +0.0027 | −0.05 | the wrong sign |
| ǀΔ breaking-ball axis deviationǀ | +0.0000 | −0.01 | nothing |
| all three (angle, fastball active spin, fastball deviation) | +0.0133 | | 1.3 points on a 1.9-point base |

**All nine outcome rates** (the command sub-score reads six of them; the score alone
explains R² 0.472, r 0.687):

| Added to the score | Gain in R² | Standardised coefficient | Read |
|---|---|---|---|
| ǀΔ arm angleǀ | +0.026 | **−0.17** | the gap is already in the score (r 0.35 with one minus the score); given the score, a larger angle gap goes with a SMALLER outcome gap |
| ǀΔ the shoulder partǀ | +0.0005 | +0.02 | nothing |
| ǀΔ fastball active spinǀ | +0.0003 | −0.02 | nothing |
| ǀΔ fastball axis deviationǀ | +0.0001 | +0.01 | nothing |
| ǀΔ breaking-ball active spinǀ | +0.0061 | −0.08 | the wrong sign |

The negative coefficient is the one modelling signal in this ticket, and it points the other
way from the ticket: the score already moves with arm-angle gaps through the release
dimensions of the arsenal distance, and that part of the distance does not translate into
outcome differences. The Wasserstein distance weights its eight z-scored dimensions equally;
a per-dimension weight is a tunable the comprehensive sweep could add, not a feature this
ticket adds.

### 2.5 The model, as it stands

- The pitcher score is 0.65 arsenal + 0.35 command. The arsenal is a Gaussian mixture per
  pitcher-season over eight per-pitch dimensions, compared by the Wasserstein-2 distance
  after every dimension is z-scored across the population, turned into a score by
  exp(−W₂ / 4.10) and calibrated so the median pair scores 0.50. The command sub-score is an
  RBF (σ 1.05) over seven rates. Shrinkage prior 5; fewer than 200 pitches means the league
  mixture.
- The eight columns are present on 99.7% of pitches in every season 2017 to 2026 (2024:
  717,881 of 721,565 pitches clean on all eight), so every one of the 8,303 pitcher-seasons
  in the profile carries a fitted mixture.
- The score feeds four draws, each at power 1 by the ruling of 2026-09-16: the pitch draw,
  the pitch-result draw, the pitching-change draw and the reliever "stuff" weight. The
  production matrix covers 2,464 pitcher-seasons of 2023 to 2026.
- The pitcher profile's INSERT names its columns (unlike the batter and fielder inserts), so
  a new profile column carries no positional-tail trap.
- The loader knows five season styles; the arm-angle board needs a sixth (`season=`).

---

## 3. Findings that shape the plan

**Finding 1 — the release point is in the model; the ticket's premise is the SIM-067
record misread.** Release x, release z and extension are three of the arsenal's eight
dimensions, fitted per pitch cluster. SIM-067 removed a separate release sub-score because it
double-counted them. "Review the release-point decision first" is done: the decision was to
read the release point once, and it stands.

**Finding 2 — arm angle is the release point measured from the shoulder.** Savant's formula
is exact on its own columns (0.03°). Our release point reproduces 73% of the angle's variance
and 81% with height; the remainder is the shoulder's position at release, a posture the
ball's flight does not carry to the plate. In the outcome test that remainder adds 0.0005 R²
on the nine rates and 0.0056 on the four unread ones.

**Finding 3 — the spin shape is three formulas on columns we hold.** Our spin axis is
Savant's measured axis (0.992); their inferred axis is a formula on our movement (0.987);
active spin follows movement × velocity / spin rate (r 0.94); the deviation rebuilds from our
two axes (r 0.80). The mixture's full covariance already sees the axis and the movement
jointly, which is where the deviation lives.

**Finding 4 — nothing here adds to the score at a size worth a feature.** The strongest
candidate, the fastball's axis deviation, adds under one R² point on the outcomes the score
does not read and nothing on the rates; arm angle's gain splits +0.0099 / +0.0017 across two
random halves of the pitchers. The one real signal is the negative coefficient on the
arm-angle gap given the score: a per-dimension weight inside the arsenal distance, for the
sweep.

**Finding 5 — the boards start in 2020 and one of them has the silent-season trap.** The
arm-angle board answers `season=` (2025 lists Kershaw, who has not pitched in 2026) and
ignores the `year=` spelling without error, serving the current season instead; it is the
only one of the three with a date control. The spin boards answer `year=` and ignore a date
range; all three return an empty body for 1990 and for 2017 to 2019. A loader entry is
specified in §5.1 for the alternative and not recommended.

---

## 4. Data changes

**Recommended path: none.** No migration, no raw table, no profile column, no recompute, no
matrix rebuild, no restart. The DuckDB schema stays v30 and Alembic at 0027.

**The alternative (decisions 1 and 2 taken the other way), specified for completeness.**

### 4.1 Postgres — Alembic 0028 (`0028_sim533_savant_arm_angle_and_spin.py`), alternative only

```sql
CREATE TABLE IF NOT EXISTS raw.savant_arm_angle (
    pitcher_id                  INTEGER   NOT NULL,
    season                      SMALLINT  NOT NULL,
    pitch_hand                  CHAR(1),
    n_pitches                   INTEGER,
    ball_angle                  FLOAT,                 -- degrees above horizontal; the one feature
    relative_release_ball_x     FLOAT,  release_ball_z FLOAT,
    relative_shoulder_x         FLOAT,  shoulder_z     FLOAT,   -- the four coordinates the angle is built from
    loaded_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (pitcher_id, season)
);
CREATE TABLE IF NOT EXISTS raw.savant_spin_direction (
    pitcher_id                  INTEGER   NOT NULL,
    season                      SMALLINT  NOT NULL,
    pitch_type                  VARCHAR(5) NOT NULL,   -- api_pitch_type, a CSV column: no split pull
    pitch_hand                  CHAR(1),
    n_pitches                   INTEGER,
    release_speed               FLOAT,  spin_rate FLOAT,  movement_inches FLOAT,
    active_spin                 FLOAT,                 -- 0..1
    hawkeye_measured            FLOAT,                 -- the measured axis, degrees
    movement_inferred           FLOAT,                 -- the axis the movement implies, degrees
    diff_measured_inferred      FLOAT,                 -- measured − inferred, degrees
    loaded_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (pitcher_id, season, pitch_type)
);
-- the active-spin board is the spin-direction board's active_spin pivoted by pitch type: not loaded
```

### 4.2 DuckDB — migration 0031 (schema v30 → v31), alternative only

```sql
ALTER TABLE derived.pitcher_season_metrics ADD COLUMN IF NOT EXISTS arm_angle_deg         FLOAT;  -- Savant ball_angle, season-shifted at a cutoff
ALTER TABLE derived.pitcher_season_metrics ADD COLUMN IF NOT EXISTS fb_axis_deviation_deg FLOAT;  -- the primary fastball's measured − inferred axis
```

The pitcher INSERT names its columns, so the two join the column list and the SELECT in one
place; no positional tail. The league rows `pitcher_L` / `pitcher_R` gain both keys. Point in
time: the spin-direction board has no date control, so its join takes the `savant_season`
expression (the completed season joins its own row; the season in progress joins the prior
one), the SIM-537 rule; the arm-angle board honours `dateStart` / `dateEnd`, so at a
backtest cutoff it can be pulled as of the date, the bat-tracking pattern
(`supports_date_range=True`, `asof_column`).

---

## 5. Code changes, file by file

**Recommended path** (the decision record; no behaviour change):

| File | Change | What |
|---|---|---|
| `similarity/engines/pitcher_similarity.py` | modified | a decision block beside `GMM_FEATURE_NAMES`: why arm angle, active spin and the axis deviation are not features, with the numbers |
| `docs/technical/similarity.md` | modified | one paragraph in the pitcher engine's entry |
| `docs/technical/sim-loop-cheat-sheet.md` | modified | one sentence under the pitcher score |
| `tests/unit/test_sim533_arm_angle_decision.py` | new | the guard (§7) |
| `scripts/sim533_arm_angle_probe.py` | new | the two probes merged: the board pulls with their probes, the reconstructions, the repeats, the pair test; runnable against the live stack |
| `BACKLOG.xlsx` | modified | the SIM-533 row deleted |
| `CHANGES.md` | modified | the record |

**Alternative path** (adds to the above): `pipeline/etl/savant_boards.py` (a sixth season
style + two entries), `pipeline/etl/savant_loader.py` (an empty body is zero rows),
`db/migrations/versions/0028_…`, `db/migrations/duckdb/0031_…`, `db/schemas/02_duckdb_schema.sql`,
`db/schemas/duckdb_schema_version.txt` (31), `pipeline/batch/player_profile_computor.py`
(the two columns; the league keys), `similarity/engines/pitcher_similarity.py` (a third
sub-score), `similarity/similarity_calibration.py` (its sigma and weights),
`scripts/sim533_pitcher_recompute.py`, and the tests for each.

### 5.1 The decision block (recommended)

```python
# similarity/engines/pitcher_similarity.py — beside GMM_FEATURE_NAMES
# SIM-533 (decision 2026-09-18): Savant's arm angle, active spin and spin-axis
# deviation are NOT features, by owner decision on the measurements in
# docs/audit/2026-09-18-sim533-pitcher-arm-angle-spin-shape-plan.md.
#   * The release point is already here (release_x, release_z, release_ext,
#     per pitch cluster). SIM-067 removed a second release sub-score that
#     double-counted it; the model reads the release point once.
#   * Arm angle = atan2(release z - shoulder z, |release x - shoulder x|)
#     (r 1.000 on Savant's own columns). Our release point reproduces 73% of
#     it, 81% with height; the rest is the shoulder's posture, which the
#     ball's flight does not carry. On 67,941 same-hand pairs of 2024 that
#     remainder adds 0.0005 R2 to the score's fit of nine outcome rates.
#   * Active spin follows movement x velocity / spin rate (r 0.94); the
#     measured axis IS spin_axis (0.992); the inferred axis is a formula on
#     ivb/hb (0.987); their difference rebuilds at r 0.80. None adds more than
#     0.009 R2 on outcomes the score does not read.
#   * The one signal points the other way: given the score, a larger
#     arm-angle gap goes with a SMALLER outcome gap (coefficient -0.17), so
#     the W2's equal weighting of its release dimensions may be too heavy.
#     A per-dimension weight is a candidate tunable for the comprehensive
#     sweep (SIM-548), not a feature.
# The guard is tests/unit/test_sim533_arm_angle_decision.py; the numbers
# re-run from scripts/sim533_arm_angle_probe.py.
```

### 5.2 The loader (alternative only)

```python
# pipeline/etl/savant_boards.py
# SavantBoard.season_params gains a SIXTH style:
if self.season_style == "season":      # SIM-533: the arm-angle board. season= is honoured (2025 lists Kershaw, who has not pitched in 2026); year= is IGNORED silently (serves the current season)
    return {"season": s}

"arm_angle": SavantBoard(
    name="arm_angle", url=f"{BASE}/leaderboard/pitcher-arm-angles", season_style="season",
    table="raw.savant_arm_angle", player_column="pitcher",
    supports_date_range=True, asof_column="asof_date",   # dateStart / dateEnd honoured (Verlander 2025: 2,608 pitches; April 446)
    extra={"min": "1", "gameType": "R", "perspective": "back", "playerType": "pitcher", "batSide": "", "size": "small", "sort": "ascending", "team": ""},
    columns=(("pitch_hand", "pitch_hand"), ("n_pitches", "n_pitches"), ("ball_angle", "ball_angle"),
             ("relative_release_ball_x", "relative_release_ball_x"), ("release_ball_z", "release_ball_z"),
             ("relative_shoulder_x", "relative_shoulder_x"), ("shoulder_z", "shoulder_z")),
),   # 2020+; min=1 lifts the qualifier (823 rows in 2024 against 284); the 1990 probe returns an EMPTY BODY, no header
"spin_direction": SavantBoard(
    name="spin_direction", url=f"{BASE}/leaderboard/spin-direction-pitches", season_style="year",
    table="raw.savant_spin_direction", player_column="player_id", season_column="year",
    split_column="pitch_type",           # read from the CSV's api_pitch_type; splits=() so no split pull
    extra={"min": "1", "hand": "", "pitch_type": "ALL"},
    columns=(("api_pitch_type", "pitch_type"), ("pitch_hand", "pitch_hand"), ("n_pitches", "n_pitches"),
             ("release_speed", "release_speed"), ("spin_rate", "spin_rate"), ("movement_inches", "movement_inches"),
             ("active_spin", "active_spin"), ("hawkeye_measured", "hawkeye_measured"),
             ("movement_inferred", "movement_inferred"), ("diff_measured_inferred", "diff_measured_inferred")),
),   # 2020+; min=1 gives every pitch type with 10+ pitches (3,180 rows in 2024 against 719)

# pipeline/etl/savant_loader.py — the probe: a 200 with an empty body (no header) counts as zero rows, not a parse error
```

### 5.3 The profile and the model (alternative only)

```python
# pipeline/batch/player_profile_computor.py — _compute_pitcher_profiles
#   the INSERT's column list + the SELECT gain arm_angle_deg (from raw.savant_arm_angle at savant_season)
#   and fb_axis_deviation_deg (raw.savant_spin_direction, the pitch type in FF/SI/FC with the most pitches);
#   NULL stays NULL (the engine maps it to the league mean)
# LeagueAverageProfiles.compute — pitcher_L / pitcher_R gain the two keys (AVG skips NULL)

# similarity/engines/pitcher_similarity.py — a third sub-score, weights re-split
PHYSICAL_FEATURES = [("arm_angle_deg", 0.97), ("fb_axis_deviation_deg", 0.95)]   # weight = the measured repeat
WEIGHT_ARSENAL, WEIGHT_COMMAND, WEIGHT_PHYSICAL = 0.585, 0.315, 0.10               # 0.65/0.35 scaled by 0.9
RBF_SIGMA_PHYSICAL = 1.0                                                            # make calibrate fits it
class PitcherProfile: + physical_vec: NDArray (2,)
FeatureNormalizer: + physical mean/std; NaN → 0 after z-scoring (the league mean)
HandednessPartition.score_all / score_pair: + WEIGHT_PHYSICAL * physical; the GMM-less redistribution spreads over command + physical
# similarity/similarity_calibration.py — _calibrate_pitcher_params: + sigma_physical, reliability_weights_physical
# then: make calibrate (write the win-probability curve back), python -m pipeline.batch.engine_artifacts --what pitcher_sim (the ~1 h job), restart
```

Even built, §2.4 says the third sub-score would move the score by information worth under
one R² point of outcome similarity; at power 1 the draw would not register it.

---

## 6. Weights, bandwidth, power

- **Recommended path:** nothing changes. Arsenal 0.65, command 0.35, σ_command 1.05,
  ARSENAL_SCALE 4.10 stay; every power stays 1.
- **A note for the sweep, recorded here and in the engine:** the arsenal distance treats its
  eight z-scored dimensions equally. The outcome test reads a negative coefficient on the
  arm-angle gap given the score, which is consistent with the release dimensions carrying
  more of the distance than outcomes warrant. A per-dimension weight vector inside the
  Wasserstein distance (a diagonal scaling before the Bures term) is a candidate tunable for
  the comprehensive sweep (SIM-548). It is not this ticket's change.
- **Alternative path:** the third sub-score at 0.10 with feature weights at the measured
  repeats (0.97, 0.95) and σ fitted by `make calibrate`; the arsenal scale refits with the
  population unchanged.

---

## 7. Tests (`tests/unit/test_sim533_arm_angle_decision.py`)

- **The decision stays honest.** `GMM_FEATURE_NAMES` and `COMMAND_FEATURES` name none of
  `arm_angle`, `ball_angle`, `active_spin`, `spin_deviation`, `axis_deviation` (substring
  match, case-insensitive), and the engine module's source carries the SIM-533 decision
  block naming this plan. A later change that adds one must edit the block, so the reader
  sees the numbers.
- **The record is where the docs say.** The technical reference and the cheat sheet each
  contain the string `SIM-533`.
- **The probe runs.** `scripts/sim533_arm_angle_probe.py --offline` reproduces §2.2's
  reconstructions on a bundled 20-row fixture (Savant's formula r 1.000; the mirrored axis
  convention), so the script's conventions cannot rot silently.
- **Alternative path adds:** the sixth season style (`season=` and nothing else); an empty
  probe body counts as zero rows; the two tables' primary keys; the named INSERT carries both
  columns; the third sub-score's weights sum to 1 and a NaN feature scores as the league mean.

---

## 8. Run book (recommended path)

```
# 1. the code lands: the decision block, the two doc paragraphs, the guard test, scripts/sim533_arm_angle_probe.py
# 2. the gates: ruff, mypy, the unit lane (the guard + the pitcher engine's existing tests); nothing to smoke — no behaviour changed
# 3. no migration, no recompute, no calibration, no matrices, no restart
# 4. close: CHANGES.md (the decision with the numbers); delete the SIM-533 row; the next free ID stays SIM-552
```

Alternative path: the SIM-531 / SIM-550 / SIM-532 run book shape — the migration, the two
board loads for 2020 to 2026 (the arm board is one pull a season, the spin board one), the
app STOPPED for the pitcher chain (the DuckDB writer lock, SIM-524), `make calibrate` (write
the win-probability curve back), the ~1 h `pitcher_sim` export, restart, a ten-game smoke.

---

## 9. What could go wrong (ranked)

1. **The ticket is re-filed from the board audit's sentence.** The audit's "the pitcher
   engine deliberately excludes its release-point sub-score" is what produced this ticket;
   the decision block and the guard test are the fix, and the audit document gets a
   one-line correction pointing here.
2. **The negative coefficient is over-read.** It is one linear fit on pairs that share
   pitchers (475 pitchers make 67,941 pairs, so the pairs are not independent). It is a
   lead for the sweep, not a finding about the arsenal's weights; §6 says so.
3. **The pair test's outcomes are season-level rates.** The draw works on pitches, not
   seasons. A per-pitch test would need the pitch-result pool joined to the score; the
   season-level test is the same one the platform uses to size every other candidate, and a
   feature that cannot move season rates will not move pitch draws at power 1.
4. **Someone loads the boards later with `year=` on the arm-angle board.** It serves the
   current season under any `year=` and does not error. §5.1 records the style; the sixth
   style's test would pin it.
5. **The boards start in 2020.** Any future use cannot cover 2017 to 2019; the pool window
   (2023 to 2026) is inside coverage.

---

## 10. Decisions for the owner

1. **Arm angle — TAKEN 2026-09-19, as recommended:** not adopted. The release point is already in the arsenal,
   and the shoulder posture that makes up the rest adds 0.0005 R² to the score's fit of the
   nine outcome rates (Finding 2, §2.4). The alternative not taken: the third sub-score of §5.3 at 0.10,
   which builds the raw table, the profile column, the calibration and the pitcher matrix
   for a change the outcome test cannot see.
2. **Active spin and the spin-axis deviation — TAKEN 2026-09-19, as recommended:** not adopted. All three are
   formulas on the movement, velocity, spin rate and spin axis the arsenal reads, and the
   strongest of them adds under one R² point on outcomes the score does not read (Finding
   3, §2.4). The alternative not taken: the fastball's axis deviation as the second feature of the third
   sub-score, on the same build.
3. **The record — TAKEN 2026-09-19, as recommended:** the decision block in the engine, one paragraph in each of
   the two technical documents, the guard test and the probe script in `scripts/`; no raw
   table. The alternative not taken: also load the arm-angle board into `raw.savant_arm_angle` for the
   record, unread, which adds a sixth season style and a table nothing reads for a number
   the probe re-pulls in seconds.

Recorded, not asked: every similarity power is 1 by the ruling of 2026-09-16; the
per-dimension weight inside the arsenal distance goes to the sweep's list of tunables, not
to this ticket; the board audit's sentence about the release sub-score gets its correction.
