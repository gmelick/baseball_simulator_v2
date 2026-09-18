# Build plan — the fielder's batter-hand split and the outfield jump (SIM-532)

> **STATUS 2026-09-17 (v2) — APPROVED: all four decisions TAKEN as recommended (§10). Ready to build.** Nothing is
> built. The readable page is https://claude.ai/artifact/GA1aZoPWcGmCA9PJaTzWVL (the same content as
> this file). Runs AFTER the outfield arm rebuild (SIM-550) has landed and run, because both
> change the fielder profile, the outfield feature groups, the calibration fit and the
> outfield matrices, and each accuracy read must stay attributable.
>
> **BUILT AND REVIEWED 2026-09-17 (the code; the run book follows) — corrections: §4 the cutoff
> denominator; §5.4 the weight fit; §8 step 4 the seasons.** The review found the per-100 figure
> dividing the prior season's outs by the current season's partial chances at a cutoff; the
> denominator now follows the numerator's season. The recompute's seasons start at 2017, where
> `raw.pitches` starts.
>
> **Revised the same day, after the owner asked whether the hand split was measured for
> infielders too.** It was, but the pooled infield figures carried the position label (the trap
> the arm rebuild found that morning), so §2.2b re-measures every candidate within one position,
> over the shift era and the shift-ban era. Two things changed. The hand split repeats only at
> shortstop (0.36 to 0.38), and Savant's per-position outs above average beats every one of our
> range components at every position, infield included, so decision 4 puts it in the infield
> group too (taken the same day). The starting weight for Savant's figure drops from 0.65 to 0.50, its
> within-position repeat; `make calibrate` sets the final value.
>
> **The ticket's premise is half right.** Measured on three seasons, the batter-hand split of
> outs above average does not repeat year to year (the gap between a fielder's figure against
> left-handers and against right-handers repeats at 0.04 to 0.14 for outfielders, and within a
> position only shortstop shows a repeat), so this plan recommends loading it and not reading
> it. The outfield jump repeats at 0.80 to 0.92, the most stable defensive numbers the platform
> would hold, and the same board pull brings Savant's official per-position outs above average,
> which repeats better than every one of our range components at every position. Those two go
> into the model.

**Date:** 2026-09-17
**Ticket:** SIM-532 (P2) in `BACKLOG.xlsx`. Next free ID: SIM-552.
**Evidence:** Savant's Outs Above Average board pulled position by position for 2018, 2019
and 2021 to 2025 and whole for 2017, 2024 and 2026; the Outfield Jump board for 2017 and 2023 to 2025; both
boards' parameters probed live; a read-only dump of our fielder range block for 2016 to 2026;
the code in the working tree with the arm rebuild (SIM-550) landing.
**Builds on:** `docs/audit/2026-09-16-sim550-outfield-arm-block-plan.md` (the fielder model
as it now stands), `docs/audit/2026-09-10-sim528-530-savant-build-plan.md` (the loader and
the batter physical block: the positional-tail pattern), the owner ruling of 2026-09-16 that
every similarity-score power is 1 until the comprehensive sweep fits them.

---

## 0. The short version

**What the ticket asks for.** Two things Savant publishes that we do not compute: how well
each fielder performs against left-handed versus right-handed batters, and how much of an
outfielder's range comes from his reaction, his acceleration and the route he takes in the
first three seconds after contact.

**What the measurements say.** I pulled both boards for three seasons and scored every
candidate the same way as the lead-distance and arm-block work: does it repeat from one
season to the next, and does it restate something the model already reads.

- The hand split does not repeat. An outfielder's outs above average against left-handers
  and against right-handers are two noisy halves of one skill (they correlate 0.61 with each
  other within a season), and their difference repeats year to year at 0.04 to 0.14. For
  infielders the gap is larger, as positioning predicts, and within a position it repeats
  only at shortstop (0.36 to 0.38 in both the shift era and the ban era) and at third base
  in the shift era alone (§2.2b). A feature that does not repeat can only add noise to a
  draw weight.
- The jump repeats. Reaction 0.80 to 0.92, route 0.71 to 0.90, burst 0.64 to 0.82, with
  25 or more plays. They are near-independent of our range block (r about 0) and only burst
  carries the sprint speed we already hold (r 0.53).
- Savant's official outs above average, pulled position by position, repeats within a
  position at 0.38 to 0.61 for outfielders and 0.34 to 0.63 for infielders, against 0.04 to
  0.47 for every one of our own range components, and agrees with ours at only r 0.31: it
  measures the same thing with better tracking. (The first draft read 0.60 to 0.76 and kept
  the infield unchanged; those pooled figures carried the position label, §2.2b.)

**The plan.** Load both boards for 2016 to 2026 (two raw tables, one Alembic migration; a new
season style and a generalised split pull in the loader). Add six columns to the fielder
profile (one DuckDB migration, appended after `asof_date` because the fielder insert is
positional). Add four features to the OUTFIELD range group: Savant's outs above average per
100 chances, and the three jump parts, each at its measured within-position repeat, with
their own sample-size confidence; add Savant's figure to the INFIELD range group too
(decision 4). Store the hand split in the raw table for the record and read nothing from it.
Then the fielder chain of the recompute (well under an hour, not the five-hour full rebuild),
the calibration refit, the seven fielder matrices, and the paired accuracy comparison as the
grade.

**What it will show.** Every similarity-score power is 1 by the ruling of 2026-09-16, and at
power 1 an identity factor reads nearly flat in the draw. So the accuracy read on this ticket
is a regression guard, not a value read; the value of sharper outfield similarity shows when
the comprehensive sweep fits the fielder power.

**Decisions** in §10: all four taken as recommended on 2026-09-17.

---

## 1. The mechanism

```
Savant Outs Above Average, pulled once per position (pos=3..9; min=0; startYear/endYear)
   ──loader──► raw.savant_outs_above_average (player, season, POSITION; the hand split kept here, unread)
Savant Outfield Jump (year=; min=0)
   ──loader──► raw.savant_outfield_jump (player, season; reaction, burst, route, jump, plays)
   ──fielder aggregator (season-shifted at a cutoff, like arm strength)──►
derived.fielder_season_metrics + savant_oaa, savant_oaa_per_100, jump_reaction_ft, jump_burst_ft, jump_route_ft, jump_plays
   ──► fielder model, OUTFIELD range group 0.40: our five components + Savant OAA/100 (0.50; the infield group too, decision 4) + reaction 0.81 · burst 0.69 · route 0.77,
       the jump parts shrunk on their own plays (prior 25), Savant's figure on our chances
   ──► fielder_LF / fielder_CF / fielder_RF matrices ──► fielding draw · advancement draw (fielder power 1, the ruling of 2026-09-16)
The infield groups do not change.
```

The draws do not change. The two boards carry no date control, so a backtest cutoff joins
the prior season's row for the season containing the cutoff (the substitute the fielder
aggregator already applies to the arm-strength board); the measured repeats say the
substitute costs little.

---

## 2. The evidence

### 2.1 The two boards, as they answer (probed 2026-09-17)

| | Outs Above Average | Outfield Jump |
|---|---|---|
| Endpoint | `/leaderboard/outs_above_average` | `/leaderboard/outfield_jump` |
| Season parameter | `startYear` / `endYear` — a NEW style for the loader | `year` |
| The 1990 probe | 0 rows: honoured | 0 rows: honoured |
| Season on the row | the `year` column is BLANK on every row, so no per-row check; the probe is the guard | `year` present: per-row check runs |
| Smallest minimum | `min=0` (551 fielders in 2024 against 271 qualified) | `min=0` (212 outfielders in 2024 against 100 qualified; `min=1` the same) |
| Coverage | 2016 to 2026 (533 to 567 a season) | 2016 to 2026 (212 to 234 a season) |
| Per position? | Yes, by pulling once per position (`pos=3` … `pos=9`): the figure is the fielder's AT that position, and 92 of the 147 center-field rows differ from the same player's all-positions figure. The `primary_pos_formatted` column is his primary position, not the pulled one, so the loader writes the position from the query | Per player (an outfielder's jump is the same at any outfield position) |
| Columns kept | `outs_above_average`, in front / behind / toward each line, `_rhh`, `_lhh`, `fielding_runs_prevented` (all integers) | reaction, burst, route, jump (all feet against the league), feet covered, plays, outs, the outs above average on those plays |
| Columns dropped | the three `*_formatted` success rates (percent strings) | `outs_per_play` |
| Other controls on the page | `range` (a month), `roles`, `team`, `split` (split years) — none used | none |

### 2.2 Which features repeat, per position-season

Year-to-year correlation of a fielder's value, on players present at the same position in
both seasons, by the minimum chances he had in both (our own chance count). 2023→24 /
2024→25.

⚠ The two position tables below pool the positions of a group. §2.2b, added the same day,
re-measures within one position, and the pooled infield verdicts do not survive it.

**Outfielders**

| Feature | ≥100 chances (n≈94) | ≥250 chances (n≈37) | Verdict |
|---|---|---|---|
| Savant OAA at the position (count) | 0.599 / 0.643 | 0.702 / 0.712 | **keep**, as OAA per 100 of our chances (0.538 / 0.643; 0.681 / 0.761); weight 0.50, its within-position repeat (§2.2b) |
| Savant OAA vs right-handed batters | 0.407 / 0.484 | 0.549 / 0.521 | half the sample of the total; not a feature |
| Savant OAA vs left-handed batters | 0.615 / 0.574 | 0.693 / 0.659 | the same |
| The hand gap (left minus right) | 0.041 / 0.141 | 0.127 / 0.118 | **noise**: store, do not read |
| Savant in front / behind | 0.087 / 0.321 · 0.180 / 0.210 | −0.054 / 0.347 · 0.136 / 0.204 | noise; not a feature |
| Our OAA (count) | 0.258 / 0.125 | 0.392 / 0.133 | today's figure; stays as a component |
| Our OAA per 100 (= `catch_pct_added`) | 0.357 / 0.091 | 0.438 / 0.177 | already read |
| Our going-back component (`oaa_deep`) | 0.588 / 0.655 | 0.777 / 0.766 | our most stable component pooled; 0.04 to 0.37 within a position (§2.2b); already read |

**Infielders**

| Feature | ≥100 chances | ≥250 chances | Verdict |
|---|---|---|---|
| Savant OAA at the position | 0.625 / 0.361 | 0.631 / 0.299 | **keep** (decision 4): within a position it repeats better than ours (§2.2b) |
| Our OAA per 100 | 0.711 / 0.616 | 0.727 / 0.494 | already read; 0.16 to 0.47 within a position (§2.2b) |
| The hand gap (left minus right) | 0.358 / 0.134 | 0.510 / 0.047 | shortstop only within a position (§2.2b); store, do not read |

**The outfield jump**, per player, by the minimum plays in both seasons

| Feature | ≥25 plays (n≈80) | ≥50 plays (n≈30) | Verdict |
|---|---|---|---|
| Reaction (feet, first 1.5 s, against the league) | 0.802 / 0.822 | 0.921 / 0.846 | **keep**, weight 0.81 |
| Burst (feet, the next 1.5 s) | 0.638 / 0.745 | 0.821 / 0.684 | **keep**, weight 0.69 |
| Route (the direction taken) | 0.710 / 0.823 | 0.847 / 0.897 | **keep**, weight 0.77 |
| Jump (the total of the three) | 0.638 / 0.745 | 0.834 / 0.678 | store only: r 0.95 with burst |
| Feet covered | 0.728 / 0.810 | 0.880 / 0.757 | store only: r 0.96 with jump |

Plays per outfielder-season: median 31, lower decile 8, upper decile 64; about 80 outfielders
a season clear 25 plays.

### 2.2b Within one position — the re-measure of 2026-09-17

The owner asked whether the hand split was measured for infielders as well as outfielders,
expecting a larger effect on the infield from positioning. It was (the second table above).
But the arm rebuild found the same morning that a repeat measured across pooled positions
can be the position label rather than the player: the position means differ by 20 to 40
outs (our going-back component averages +20 in centre field and −22 at second base), so a
pooled correlation partly reads which position a man plays, and the engine scores within a
position. Every figure below is measured within one position, on players at that position
in consecutive seasons with 100 or more of our chances in both, two season pairs pooled per
era. The shift era is 2018→19 and 2021→22 (two more per-position pulls of the board); the
ban era is 2023→24 and 2024→25, the pool window.

| Year-to-year r within the position | 1B | 2B | 3B | SS | LF | CF | RF |
|---|---|---|---|---|---|---|---|
| The hand gap (left − right), shift era (n 43–70) | 0.04 | −0.19 | **0.41** | **0.38** | −0.04 | 0.14 | −0.08 |
| The hand gap, ban era (n 41–68) | 0.21 | 0.15 | 0.06 | **0.36** | 0.12 | 0.18 | −0.05 |
| The hand gap per 100 chances, ban era | 0.18 | 0.17 | −0.13 | **0.38** | 0.12 | 0.06 | −0.12 |
| Savant OAA per 100, ban era | 0.34 | **0.63** | **0.51** | **0.44** | **0.38** | **0.54** | **0.53** |
| Savant OAA per 100, shift era | 0.09 | 0.42 | 0.42 | 0.48 | 0.26 | 0.52 | 0.40 |
| Our OAA per 100, ban era | 0.16 | 0.47 | 0.19 | 0.21 | 0.17 | 0.19 | 0.27 |
| Our best range component, ban era | 0.46 | 0.47 | 0.39 | 0.37 | 0.39 | 0.27 | 0.30 |
| The gap's size: mean \|left − right\| per 100 chances, regulars | 1.2 | 1.5 | 1.7 | 1.1 | 0.7 | 0.6 | 0.7 |

The best of our five components within each position: glove side at first (0.46) and at
third (0.39), the catch percentage added at second (0.47), going back at short (0.37) and
in centre (0.27), arm side in left (0.39), charging in right (0.30). Pooled across the
infield the same components read 0.58 to 0.75.

Three reads.

1. **The infield gap is larger, as the owner expected**: 1.1 to 1.7 outs per 100 chances
   against 0.6 to 0.7 in the outfield (1.2 to 2.0 against 0.65 to 0.9 in the shift era). But
   size is not repeatability. Within a position the gap repeats at shortstop in both eras
   (0.36 to 0.38) and at third base in the shift era only (0.41, then 0.06 after the ban); at
   first, at second and at every outfield spot it reads −0.19 to 0.21 in both eras. With
   about 55 pairs an era, a repeat of 0.37 has a 95% interval of roughly 0.10 to 0.58: a weak
   feature at one position.
2. **Finding 2's "infield unchanged" is withdrawn.** Our pooled infield figure (0.62 to
   0.71) was the position label. Within a position our five range components repeat at 0.04
   to 0.47, and `make calibrate` had already fitted every one of them at its 0.10 floor on
   2026-09-17 (the engine module carries the fitted values). Savant's per-position figure
   repeats at 0.34 to 0.63 in the ban era: it beats our best component at five of the seven
   positions, ties in left field and trails our glove-side component at first base (0.34
   against 0.46). It belongs in both range groups: decision 4.
3. **The weights.** Savant's figure within a position reads 0.38 to 0.61 in the outfield
   (median 0.53, start 0.50) and 0.34 to 0.63 on the infield (median 0.47, start 0.45). The
   fit measures within (player, position) since the arm rebuild, so the fitted values land
   near these, not near the pooled 0.65.

The probe is `sim532_by_position.py` beside the other SIM-532 probes in the session
scratchpad, with the four extra board pulls (`oaa_{2018,2019,2021,2022}_pos{3..9}.csv`); the
build copies it into `scripts/` with the recompute script.

### 2.3 Independence

- Jump parts against one another (2024, 50 or more plays): reaction–burst 0.44,
  reaction–route −0.79, burst–route −0.10. Reaction and route pull against each other by
  Savant's construction (a fast first step and a straight line trade off), so the pair
  carries about a third less than two independent features would; the calibration's
  reliability fit sees that and the doc weights are the starting point, not the last word.
- Against sprint speed: burst 0.53, feet 0.49, reaction 0.22, route −0.23. Burst is half
  speed, which the profile already stores; reaction and route are new.
- Against our range block: jump against our OAA per 100 reads 0.03 (total), −0.16 (reaction),
  0.18 (route). The jump is not a restatement of range as we measure it.
- Savant's OAA against ours, 2024, 100 or more chances: r 0.31 for outfielders and 0.30 for
  infielders; Savant's spread is 5.0 outs against ours 8.4. Two measurements of one skill,
  theirs from full tracking, ours from an approximation of hang time and distance.

### 2.4 The model, as it stands after the arm rebuild

- The outfield range group (weight 0.40) reads five of our own components: outs above
  average to the glove side, the arm side, charging, going back, and the catch percentage
  added. The infield range group (0.45) reads the same five. Sprint speed sits on the
  profile but in no feature list.
- The kernel maps a missing feature to zero distance; the profile's confidence is the
  batted-ball sample (600 and more for a regular). The arm rebuild added a per-group
  confidence (`n_prior` on the shrink call) and the rule that a missing measurement becomes
  the league mean rather than 0 — the pattern this plan reuses for the jump.
- The fielder insert is positional: `sprint_speed` and then `asof_date` are the last two
  columns; anything new goes after `asof_date`, in one declared order, with a test.
- The loader's split pull is hard-wired to the pitcher's hand (`pitchHand=`); the per-position
  pull needs the same mechanism with a different parameter. The loader knows four season
  styles; `startYear` / `endYear` is a fifth.
- The fielding draw already runs on a pool split by the batter's hand (the pool's one hard
  filter). A hand-split fielder score would need a matrix per hand, selected by the live
  batter's hand — feasible, and not worth building for a feature that does not repeat.

---

## 3. Findings that shape the plan

**Finding 1 — the hand split is noise everywhere but shortstop.** Within a season a fielder's
outs above average against left-handers and against right-handers correlate 0.61 in the
outfield and 0.14 to 0.41 by infield position: two halves of one skill, each on half the
chances. Their difference repeats year to year at 0.04 to 0.14 for outfielders, and of 77
regular outfielders in 2024 only 12 have opposite signs, all small. On the infield the gap is
larger, as positioning predicts, and within a position it repeats only at shortstop, 0.36 to
0.38 in both the shift era and the ban era, and at third base in the shift era alone (§2.2b).
The ticket's condition that "the fielding model uses them" would, at six of the seven
positions, mean reading noise into a draw weight, and at the seventh a weak feature the
engine's group-level lists cannot carry. The plan stores the two columns in the raw table (the
pull brings them free) and reads nothing from them; decision 1 records the shortstop
alternative, not taken.

**Finding 2 — Savant's per-position outs above average is the better range measurement at
every position, and it is free.** The hand-split question forces the per-position pull, and
that pull's headline column, per 100 of our chances, repeats within a position at 0.38 to
0.61 for outfielders and 0.34 to 0.63 for infielders, against 0.04 to 0.47 for every one of
our own range components (§2.2b), with r 0.31 between theirs and ours. The first draft read
the pooled figures (0.60 to 0.76 for the outfield; "ours is the more stable" for the infield)
and those carried the position label. Adding it costs one column in each range group; the
infield half is decision 4.

**Finding 3 — the jump covers a third of outfield rows and needs its own confidence.** About
213 outfielders a season have a jump row against about 600 outfielder-position rows, and half
of those have fewer than 31 plays. The jump features shrink on their own plays (prior 25, the
count at which the repeat passes 0.8) toward the outfield league mean, and a missing jump is
the league mean, never 0. Savant's OAA per 100 shrinks on our chance count, the same basis as
our own figure.

**Finding 4 — one raw column and one loader mechanism are missing.** The OAA board leaves its
`year` column blank, so the loader's per-row season check cannot run on it; the probe is the
only guard, and the registry says so. The per-position pull generalises the hand-split
mechanism: a `split_param` and a tuple of (label, value) pairs.

**Finding 5 — the power is 1.** By the ruling of 2026-09-16 every similarity-score power is 1
until the comprehensive sweep fits them together. At power 1 an identity factor reads nearly
flat, so the paired accuracy comparison on this ticket guards against regression and cannot be
expected to show a gain; the gain shows when the sweep fits the fielder power on the sharper
matrices this plan produces.

---

## 4. Data changes

### 4.1 Postgres — Alembic 0027 (`0027_sim532_savant_oaa_and_jump.py`)

```sql
CREATE TABLE IF NOT EXISTS raw.savant_outs_above_average (
    player_id                  INTEGER     NOT NULL REFERENCES raw.players(player_id),
    season                     INTEGER     NOT NULL,
    position                   VARCHAR(2)  NOT NULL,   -- the position PULLED (1B..RF), written from the query
    primary_position           VARCHAR(2),             -- the board's own label for the player
    fielding_runs_prevented    INTEGER,
    outs_above_average         INTEGER,                -- at this position
    oaa_in_front               INTEGER,
    oaa_toward_3b_line         INTEGER,
    oaa_toward_1b_line         INTEGER,
    oaa_behind                 INTEGER,
    oaa_vs_rhh                 INTEGER,                -- the hand split: kept for the record, read by nothing (Finding 1)
    oaa_vs_lhh                 INTEGER,
    scraped_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (player_id, season, position)
);
CREATE INDEX IF NOT EXISTS idx_savant_outs_above_average_season ON raw.savant_outs_above_average(season);

CREATE TABLE IF NOT EXISTS raw.savant_outfield_jump (
    player_id                  INTEGER     NOT NULL REFERENCES raw.players(player_id),
    season                     INTEGER     NOT NULL,
    n_plays                    INTEGER,                -- the plays Savant scored
    n_outs                     INTEGER,
    outs_above_average         INTEGER,                -- on those plays
    reaction_ft                FLOAT,                  -- feet against the league, the first 1.5 s
    burst_ft                   FLOAT,                  -- the next 1.5 s
    route_ft                   FLOAT,                  -- the direction taken
    jump_ft                    FLOAT,                  -- the total (Savant's "jump")
    feet_covered               FLOAT,                  -- feet in the first 3 s, unadjusted
    scraped_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (player_id, season)
);
CREATE INDEX IF NOT EXISTS idx_savant_outfield_jump_season ON raw.savant_outfield_jump(season);
-- downgrade: DROP INDEX + DROP TABLE for both (the 0019 pattern)
```

### 4.2 DuckDB — migration 0030 (schema v29 → v30)

```sql
-- derived.fielder_season_metrics is a POSITIONAL insert: these append after asof_date,
-- in OAA_JUMP_COLUMN_ORDER, and the SELECT tail is generated from the same tuple.
ALTER TABLE derived.fielder_season_metrics ADD COLUMN IF NOT EXISTS savant_oaa          INTEGER;  -- at this position
ALTER TABLE derived.fielder_season_metrics ADD COLUMN IF NOT EXISTS savant_oaa_per_100  FLOAT;    -- savant_oaa * 100 / our opportunities
ALTER TABLE derived.fielder_season_metrics ADD COLUMN IF NOT EXISTS jump_reaction_ft    FLOAT;    -- outfield rows only
ALTER TABLE derived.fielder_season_metrics ADD COLUMN IF NOT EXISTS jump_burst_ft       FLOAT;
ALTER TABLE derived.fielder_season_metrics ADD COLUMN IF NOT EXISTS jump_route_ft       FLOAT;
ALTER TABLE derived.fielder_season_metrics ADD COLUMN IF NOT EXISTS jump_plays          INTEGER;  -- the jump features' confidence basis
```

Mirror the six into `db/schemas/02_duckdb_schema.sql` after `asof_date`; bump
`duckdb_schema_version.txt` to 30. `FIELDER_TAIL_COLUMNS = ("asof_date", *OAA_JUMP_COLUMN_ORDER)`
is the one definition; a test holds the migration, the canonical schema and the SELECT to it.
The hand split gets no profile column (decision 1, taken; the shortstop-only alternative, two more
columns `savant_oaa_vs_lhh` and `savant_oaa_vs_rhh` in the order, was not taken, §10).

**The league rows** `fielder_LF`, `fielder_CF`, `fielder_RF` gain four keys:
`savant_oaa_per_100`, `jump_reaction_ft`, `jump_burst_ft`, `jump_route_ft` (`AVG` skips NULL);
the four infield rows gain `savant_oaa_per_100` alone (decision 4).

**Point in time.** Neither board has a date control. The joins take the `savant_season`
expression the aggregator already uses for arm strength: a completed season joins its own row,
the season containing the cutoff joins the prior season's. The shifted numerator takes the
shifted denominator: for the season containing the cutoff, `savant_oaa_per_100` divides the
prior season's outs by the prior season's chances, so the substitute is the prior season's
per-100 figure, never a mixed-season quotient. The aggregator reads those chances from the
build when the prior season is in the run, else from the prior season's surviving row in
`derived.fielder_season_metrics`; with neither, the figure is NULL and the model reads the
league mean. The jump's 0.8-and-up repeat and the OAA's within-position repeats (§2.2b) say
the substitute costs little; the leakage assertion needs no new source (no date to check).

---

## 5. Code changes, file by file

| File | Kind | Change |
|---|---|---|
| `db/migrations/versions/0027_sim532_savant_oaa_and_jump.py` | new | §4.1 |
| `db/migrations/duckdb/0030_sim532_fielder_oaa_jump.sql` | new | §4.2 |
| `db/schemas/02_duckdb_schema.sql`, `duckdb_schema_version.txt` | modified | mirror; 30 |
| `pipeline/etl/savant_boards.py` | modified | the fifth season style (`startyear`); `split_param` + `splits` generalising `hand_splits`; two entries; the `FIELDING_BOARDS` group grows |
| `pipeline/etl/savant_loader.py` | modified | `build_url` takes the split parameter's name from the board; four count columns into `_INT_TARGETS` |
| `pipeline/batch/player_profile_computor.py` | modified | two joins in the fielder aggregator; the six-column positional tail from `OAA_JUMP_COLUMN_ORDER`; four league keys |
| `similarity/engines/fielder_similarity.py` | modified | four outfield range features; `sample_jump_plays` on the profile; the jump's own shrinkage confidence; the loader reads the four through the column guard |
| `similarity/similarity_calibration.py` | modified | the outfield SELECT and the range fit read the nine features; measured-rows fit |
| `scripts/sim532_fielder_recompute.py` | new | the fielder chain only, the league rows, the verify block |
| `tests/unit/test_sim532_fielder_oaa_jump.py` | new | §7 |
| `tests/unit/test_sim528_savant_loader.py` | modified | the new season style and the split mechanism |
| `tests/unit/test_sim537_baserunner_catcher_fielder_point_in_time.py` | modified | the two joins season-shift |
| `tests/unit/test_sim542_fielder_placeholder_schema.py` | modified | the placeholder DDL gains the six columns |
| `docs/technical/sim-loop-cheat-sheet.md`, `similarity.md`, `pipeline-betting-db.md` | modified | the outfield range group; the board list |
| `CHANGES.md`, `BACKLOG.xlsx` | modified | the close entry; delete the row |
| `simulation/*`, `pipeline/batch/engine_artifacts.py`, `docker-compose.yml` | unchanged | the draws, the matrix specs and the powers (all 1) do not change |

### 5.1 The loader

```python
# pipeline/etl/savant_boards.py
class SavantBoard:
    …
    #: The query parameter a split pull varies, and its (label, value) pairs. The
    #: pitcher-hand split (SIM-529) is split_param="pitchHand"; the per-position
    #: outs-above-average pull (SIM-532) is split_param="pos".
    split_param: str = "pitchHand"
    splits: tuple[tuple[str, str], ...] = ()          # replaces hand_splits (kept as an alias one release)

    def season_params(self, season):
        …
        if self.season_style == "startyear":            # NEW: the outs-above-average board
            return {"startYear": s, "endYear": s}

_POSITION_SPLITS = (("1B", "3"), ("2B", "4"), ("3B", "5"), ("SS", "6"), ("LF", "7"), ("CF", "8"), ("RF", "9"))

"outs_above_average": SavantBoard(
    name="outs_above_average",
    url=f"{BASE}/leaderboard/outs_above_average",
    season_style="startyear",                           # startYear/endYear; the probe passes (1990 → 0 rows)
    table="raw.savant_outs_above_average",
    player_column="player_id",
    season_column=None,                                 # the board's year column is BLANK on every row: the probe is the only guard
    split_column="position", split_param="pos", splits=_POSITION_SPLITS,   # one pull per position; the figure is AT that position
    extra={"type": "Fielder", "min": "0", "split": "no", "range": "year", "viz": "hide"},
    columns=(("primary_pos_formatted", "primary_position"), ("fielding_runs_prevented", "fielding_runs_prevented"),
             ("outs_above_average", "outs_above_average"), ("outs_above_average_infront", "oaa_in_front"),
             ("outs_above_average_lateral_toward3bline", "oaa_toward_3b_line"),
             ("outs_above_average_lateral_toward1bline", "oaa_toward_1b_line"), ("outs_above_average_behind", "oaa_behind"),
             ("outs_above_average_rhh", "oaa_vs_rhh"), ("outs_above_average_lhh", "oaa_vs_lhh")),
),
"outfield_jump": SavantBoard(
    name="outfield_jump", url=f"{BASE}/leaderboard/outfield_jump",
    season_style="year", table="raw.savant_outfield_jump",
    player_column="resp_fielder_id", season_column="year",
    extra={"min": "0"},
    columns=(("n", "n_plays"), ("n_outs", "n_outs"), ("outs_above_average", "outs_above_average"),
             ("rel_league_reaction_distance", "reaction_ft"), ("rel_league_burst_distance", "burst_ft"),
             ("rel_league_routing_distance", "route_ft"), ("rel_league_bootup_distance", "jump_ft"),
             ("f_bootup_distance", "feet_covered")),
),

# pipeline/etl/savant_loader.py
def build_url(board, season, split_value="", asof=None):
    …
    if split_value:
        params[board.split_param] = split_value       # was: params["pitchHand"] = hand
_INT_TARGETS |= {"n_plays", "n_outs", "fielding_runs_prevented", "oaa_in_front", "oaa_toward_3b_line",
                 "oaa_toward_1b_line", "oaa_behind", "oaa_vs_rhh", "oaa_vs_lhh"}   # outs_above_average is already there
_TEXT_TARGETS |= {"position", "primary_position"}
```

### 5.2 The profile builder

```python
# pipeline/batch/player_profile_computor.py
OAA_JUMP_COLUMN_ORDER = ("savant_oaa", "savant_oaa_per_100", "jump_reaction_ft", "jump_burst_ft", "jump_route_ft", "jump_plays")
FIELDER_TAIL_COLUMNS = ("asof_date", *OAA_JUMP_COLUMN_ORDER)      # the positional-insert contract; a test holds the live table to it
JUMP_ALPHA_PRIOR_PLAYS = 25                                        # shared with the model
_SQL_IS_OF = "c.position IN ('LF', 'CF', 'RF')"                    # re-create: SIM-550 (2026-09-17) deleted this constant with the Savant runner-view arm join

# _aggregate_fielder_season_metrics — the SELECT tail, after asof_date, in OAA_JUMP_COLUMN_ORDER
       DATE '{asof_sql}' AS asof_date,
       soaa.outs_above_average                                              AS savant_oaa,
       soaa.outs_above_average * 100.0 / NULLIF(c.opportunities, 0)         AS savant_oaa_per_100,
       CASE WHEN {_SQL_IS_OF} THEN sj.reaction_ft END                        AS jump_reaction_ft,   -- outfield rows only
       CASE WHEN {_SQL_IS_OF} THEN sj.burst_ft END                           AS jump_burst_ft,
       CASE WHEN {_SQL_IS_OF} THEN sj.route_ft END                           AS jump_route_ft,
       CASE WHEN {_SQL_IS_OF} THEN sj.n_plays END                            AS jump_plays
FROM combined_oaa c … existing joins …
LEFT JOIN pg.raw.savant_outs_above_average soaa
       ON c.player_id = soaa.player_id AND soaa.position = c.position AND soaa.season = {savant_season}   -- per position
LEFT JOIN pg.raw.savant_outfield_jump sj
       ON c.player_id = sj.player_id AND sj.season = {savant_season}                                       -- per player; one row per outfield position

# LeagueAverageProfiles.compute — the fielder rows gain, for LF/CF/RF (AVG skips NULL; the four infield rows gain savant_oaa_per_100 alone, decision 4):
'savant_oaa_per_100', AVG(savant_oaa_per_100), 'jump_reaction_ft', AVG(jump_reaction_ft),
'jump_burst_ft', AVG(jump_burst_ft), 'jump_route_ft', AVG(jump_route_ft)
```

### 5.3 The fielder model

```python
# similarity/engines/fielder_similarity.py
OF_RANGE_FEATURES = [
    ("oaa_glove_side", 0.100), ("oaa_arm_side", 0.100), ("oaa_charging", 0.100), ("oaa_deep", 0.100),   # ours, unchanged
    ("catch_pct_added", 0.100),
    ("savant_oaa_per_100", 0.50),     # SIM-532: Savant's outs above average at this position per 100 of our chances (within-position repeat 0.38-0.61)
    ("jump_reaction_ft", 0.81),       # feet against the league in the first 1.5 s (repeat 0.80-0.92)
    ("jump_burst_ft", 0.69),          # the next 1.5 s (0.64-0.82; half sprint speed)
    ("jump_route_ft", 0.77),          # the direction taken (0.71-0.90; pulls against reaction, r -0.79)
]
IF_RANGE_FEATURES += [("savant_oaa_per_100", 0.45)]   # decision 4: within a position it repeats 0.34-0.63; our five components 0.04-0.47 (Part 2.2b)
JUMP_ALPHA_PRIOR_PLAYS = 25

class FielderProfile: + sample_jump_plays: int = 0
_load_profiles: + the four columns and jump_plays through the column guard (NULL AS … when absent, so the model
                builds against a database that has not run 0030); NULL → NaN (the loader's rule)
_load_positional_averages: the outfield "range" vector grows to nine and the infield one to six; an absent league key is NaN, not 0.0

_apply_shrinkage (outfield branch, the range vector):
    avg = self._pos_avg["range"][pos][s]
    base   = self._shrinkage.shrink(p.range_vec[:6], avg[:6], n)                                   # our five + Savant/100 on batted balls, as today
    jump   = self._shrinkage.shrink(p.range_vec[6:], avg[6:], p.sample_jump_plays, n_prior=JUMP_ALPHA_PRIOR_PLAYS)   # the jump on its own plays
    p.range_vec = np.concatenate([base, jump])                                                      # a NaN becomes the league mean in shrink()
    # 10 plays → alpha 0.29 (two thirds of the way to the league mean); 31 → 0.55; 64 → 0.72

_apply_shrinkage (infield branch): the six-long range vector shrinks on the batted-ball sample, as today
apply_calibration: unchanged in shape (sigma_of_range; reliability_weights_of_range now nine long for the outfield, six for the infield)
```

### 5.4 Calibration

```python
# similarity/similarity_calibration.py — _calibrate_fielder_params, the outfield SELECT (the infield SELECT gains savant_oaa_per_100 alone, decision 4)
oaa_glove_side, oaa_arm_side, oaa_charging, oaa_deep, catch_pct_added,
savant_oaa_per_100, jump_reaction_ft, jump_burst_ft, jump_route_ft,        # in OF_RANGE_FEATURES order
# range_raw: NaN for NULL (was `or 0.0`); range_measured = rows finite on all nine — the 2017+ outfielders
# with a jump row and a Savant figure (about 200 a season, well over the 20-row floor);
# sigma_of_range fits over range_measured (the rows measured on every range feature), the SIM-530 rule;
# reliability_weights_of_range fits over the whole NaN-carrying matrix, each feature on the season
# pairs that carry it (the jump trio on pairs with 25 or more plays in both seasons); a new feature
# with fewer than 20 pairs keeps its module default
```

### 5.5 The recompute script

```python
# scripts/sim532_fielder_recompute.py — the sim531 / sim550 pattern; the app STOPPED (the DuckDB writer lock, SIM-524)
def main(seasons):                                            # 2017 … 2026: raw.pitches starts in 2017; the boards' 2016 rows stay in the raw tables, unread
    apply_migration("0030_sim532_fielder_oaa_jump.sql")       # idempotent
    c = PlayerProfileComputor(dsn, duckdb_path)
    c.run_fielder_chain(seasons)      # the seven per-play builders → the aggregator → the arm fill (the pools exist); not the pitcher GMM
    LeagueAverageProfiles(duckdb_path).compute(seasons)       # the outfield rows gain the four keys
    verify():
        the fielder tail order == FIELDER_TAIL_COLUMNS (the positional-insert trap)
        per season: fielder-position rows with a Savant figure (expect ~1,150 of ~1,200) and outfield rows with a jump (~210)
        infield rows have NULL jump columns; a 2015 row (if any) has NULL everything new
        three named outfielders against the CSVs value for value (2024: the player with the most plays; one 2-position player,
            whose Savant figure DIFFERS between his LF and RF rows; one with no jump row)
        the four keys on the fielder_LF/CF/RF league rows; one cutoff per table
```

---

## 6. Weights, bandwidth, power

- **Feature weights** are the measured repeats (Savant OAA/100 0.50 in the outfield and 0.45
  on the infield, the within-position medians of §2.2b; reaction 0.81, burst 0.69, route
  0.77, the 25-play figures averaged over the two season pairs), the group's own rule.
  `make calibrate` refits them by season-to-season correlation and the fitted values are
  copied into the module defaults before the matrix rebuild (the matrices read the module
  defaults, not the report).
- **The group weight** 0.40 for the outfield range is unchanged: the jump decomposes range,
  the ticket's own framing, so it lives inside that group rather than as a fifth group with a
  re-split (decision 3).
- **The bandwidth** `sigma_of_range`, 1.027 today, refits over the nine-feature measured rows.
- **The power** is 1 by the ruling of 2026-09-16 and stays 1; the comprehensive sweep fits it.

---

## 7. Tests (`tests/unit/test_sim532_fielder_oaa_jump.py` unless named)

- **Loader** (the SIM-528 file): the `startyear` style builds `startYear=…&endYear=…`; a
  split pull varies `board.split_param` (the hand boards still send `pitchHand`, the OAA board
  sends `pos`); the OAA board pulls seven times a season and writes the position label from the
  query; a board with no season column skips the row check; the count columns coerce to
  integers; a blank measurement becomes None.
- **Migrations:** 0030 adds the six columns in `OAA_JUMP_COLUMN_ORDER`; the canonical schema
  carries them in that order after `asof_date`; the version file reads 30; Alembic 0027
  upgrades and downgrades.
- **Decision 4:** the infield range list names `savant_oaa_per_100`, the four infield league
  rows carry it, and an infield profile's range vector is six long.
- **Builder** (the SIM-537 file for the shift): both joins season-shift under a cutoff and not
  without; the OAA join is per position; the SELECT tail equals `FIELDER_TAIL_COLUMNS`; the jump
  columns are NULL on infield rows; the per-100 figure divides by our chances and is NULL at
  zero chances; the placeholder DDL (the SIM-542 file) carries the six.
- **The model:** the outfield range group is the nine, the infield five; the jump confidence
  is plays-based (10 plays gives 0.29, 64 gives 0.72) and leaves the base features on batted
  balls; a missing jump becomes the league mean, never 0; an absent league key yields NaN;
  `score_all` and `query_pair` agree; the invariant gate holds.
- **Calibration:** the outfield SELECT names the nine; the fit uses measured rows and returns
  the sentinel below 20.
- **The finding stays honest:** a test asserts that no feature list names `oaa_vs_lhh` or
  `oaa_vs_rhh`, with Finding 1's numbers in its docstring, so a later reader cannot add the
  split without seeing why it was left out.

---

## 8. Run book

Runs after the arm rebuild (SIM-550) has landed, run and been read. Steps 4 and 5 need the
application stopped (the DuckDB writer lock).

```
# 0. the OFF arm: the arm rebuild's ON-arm report IS this ticket's OFF arm when SIM-532 runs straight after it
# 1. the code lands; the gates
make lint && make type-check && make test-unit && make test-regression
# 2. Postgres migration (the app may stay up)
docker compose run --rm app alembic upgrade head            # → 0027
# 3. load the two boards, 2016 to 2026 (the OAA board is seven pulls a season; one to three minutes each)
docker compose run --rm app python -m pipeline.etl.savant_loader \
  --boards outs_above_average outfield_jump --seasons 2016 2017 2018 2019 2020 2021 2022 2023 2024 2025 2026
#    expect per season about 1,150 OAA rows across the seven positions and 210 to 235 jump rows; the probes pass
# 4. stop the app; apply 0030; the fielder chain and the league rows; the verify block (well under an hour)
docker compose stop app
MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" app \
  python scripts/sim532_fielder_recompute.py --seasons 2017 2018 2019 2020 2021 2022 2023 2024 2025 2026
# 5. refit; copy sigma_of_range and the nine range weights into the module defaults (one commit)
make calibrate
#    ⚠ make calibrate DROPS the win-probability reliability curve (the SIM-427 trap): write it back
# 6. the seven fielder matrices (decision 4 puts the new feature in the infield list too) + the concentration report
docker compose run --rm app python -m pipeline.batch.engine_artifacts --what actors_sim \
  --matrix fielder_LF --matrix fielder_CF --matrix fielder_RF
# 7. start the app; "build_all_engines: 11/11" + the calibration lines
docker compose start app
# 8. the checks: the fit probe's fielding read; the ON arm on the same 250 games of 2024, paired with
#    sim518_pair_accuracy.py (--force, cross-bundle) — a regression guard at power 1, not a value read
# 9. close: CHANGES.md; delete the SIM-532 row; the cheat sheet, similarity.md, pipeline-betting-db.md
```

---

## 9. What could go wrong (ranked)

1. **The positional insert writes values into the wrong columns.** The tail tuple, its test,
   and the verify block's named players (a two-position player whose Savant figure differs by
   position is the sharpest check).
2. **The OAA board serves the wrong season silently.** Its `year` column is blank, so the
   per-row check cannot run; the probe (1990 → 0 rows) runs once per board per load and fails
   loudly. A future change to the board's season parameter would be caught there.
3. **A thin jump read at face value.** A ten-play outfielder's reaction is noise; the
   plays-based confidence shrinks him two thirds of the way to the league mean, and a test
   holds it.
4. **The hand split creeps in later.** The test in §7 names the two columns and the numbers.
5. **Reaction and route double-count.** They pull against each other (r −0.79); the
   calibration's reliability fit and the sigma refit absorb it; if the fitted weights collapse
   one of them, that is the data speaking, and the doc weights are only the start.
6. **The season-shift substitute at a cutoff** reads the prior season's jump; at 0.8 to 0.9
   repeat that costs little, and a first-season outfielder reads the league mean.
7. **The read is flat.** Expected at power 1 (Finding 5); the sweep is where the value shows.

---

## 10. Decisions for the owner

1. **The hand split — TAKEN 2026-09-17, as recommended:** load it into the raw table and read
   nothing from it. Within a position the left-minus-right gap repeats nowhere but shortstop (0.36 to 0.38
   on about 55 pairs an era, a 95% interval of roughly 0.10 to 0.58) and, in the shift era
   only, third base (§2.2b). The engine's feature lists are per group (infield, outfield), so
   reading the gap at shortstop alone means a per-position feature list, two profile columns
   and the infield league keys, for one weak feature at one position. Revisit when the 2026
   season completes and gives a third ban-era pair at shortstop. The alternative not taken:
   build the per-position list and read the gap at shortstop only, at 0.35.
2. **Savant's per-position outs above average as an outfield range feature — TAKEN
   2026-09-17, as recommended.** The starting weight is 0.50, not the 0.65 first proposed:
   0.65 was the pooled repeat and 0.50 the within-position one (§2.2b); `make calibrate` sets
   the final value either way.
3. **Where the jump lives — TAKEN 2026-09-17, as recommended:** inside the outfield range
   group at weight 0.40, unchanged, as three features at their repeats.
4. **The same figure in the infield range group — TAKEN 2026-09-17, as recommended:** yes, at 0.45. The first
   draft kept the infield unchanged because our pooled infield figure repeated at 0.62 to
   0.71; within a position it repeats at 0.16 to 0.47, `make calibrate` has every one of our
   five infield components at its 0.10 floor, and Savant's figure repeats at 0.34 to 0.63
   (§2.2b). The cost is one feature in one list, the four infield league keys, the infield
   calibration SELECT and four more matrices in the rebuild; the column and the pull are
   already in the plan. The alternative not taken: outfield only.

Recorded, not asked: the grade is the paired accuracy comparison with the lane optional, per
the rulings on the lead-distance and arm work; every similarity power is 1 by the ruling of
2026-09-16, so the read is a regression guard and the sweep is where the value shows; this
ticket runs after the arm rebuild.
