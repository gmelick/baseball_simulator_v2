# Certification plan — the outfield fence: the park geometry, the carry model and the fence stage (SIM-478/479/480)

> **STATUS 2026-09-21 — APPROVED (all four decisions TAKEN, §10), BUILT AND RUN the same day.** The
> record with the before / after table is `CHANGES.md` (2026-09-21). What the run found beyond the plan:
> (1) **the acceptance lane had never given the fence stage a venue** — every lane since the stage flipped on
> (2026-09-09) graded it inert (100% of air balls passed); production and the accuracy comparison resolve
> the venue, the lane now does too and refuses to run the stage without one; (2) **the wall-play check (C)
> FAILS** — for a born air ball 0–30 ft short of the fence the fielding draw returns a row that carries 60 ft
> shorter on average (singles 12.5% where the born rows' own read 1.8%, doubles 9.9% against 25.9%): the
> born-ball kernel z-scores distance by the whole pool's spread (135 ft) over four features, so at bandwidth
> 1.0 a row 60 ft shorter keeps 98% of an exact match's weight. The kernel is flat in distance; the class
> filter and the fence stage are the only shape. The lever is the bandwidth (the sweep's, SIM-548) or a hard
> distance band for wall-zone air balls (a fact filter — an owner decision, not taken here). The check's
> Camden read is 18.4% → 7.2% on the eleven-sector selection (§2.4's 20.1% / 7.5% was the design probe's
> clamped nine-sector selection). The change detector listed twelve moves at ten parks, not Camden alone;
> each is in the document's `moves` block (§9.2). **AMENDED the same day at the owner's instruction (§11,
> §12 — decisions 5–9 TAKEN 2026-09-22 by the owner's instruction "implement this updated design"; BUILT AND RUN 2026-09-22, the record is `CHANGES.md` 2026-09-22: A1–A7 PASS, the wall play C PASS, the home-run band PASS, Coors' per-park residual −2.5 → +0.35 points; the lane reads doubles per ball in play +5.8% RED — the wall fix unmasked an over-production of doubles on ordinary air balls that the kernel's bandwidth owns (the sweep's, §12.5) — beside the walks / hit-by-pitch / steal reds that predate this ticket. **CLOSED 2026-09-22: the owner accepted the doubles read; the row is deleted and the finding sits on the comprehensive sweep's row.**):** §11 the Coors air — the born ball's carry expressed in the live
> park's air by a per-park carry offset (Coors +20 ft), the measured answer to "should the check run on
> exit velocity, launch angle and spray?" (no: the ball's own distance carries 13–15 ft the factors do not);
> §12 the wall play — the born-ball kernel measured on the born ball's class (the class ruler) with the
> exponent per feature, which closes two thirds of the wall-play gap; §12.7 (2026-09-22) the distance to
> the WALL as the wall-zone similarity — measured: at a fixed raw distance the play depends on the fence,
> at a fixed margin (feet short of the fence) it does not; the margin in feet beats the ratio and the raw
> distance adds nothing beyond the ball's own factors. Decisions 5–9 at the end of each section. The readable page is
> https://claude.ai/artifact/KT5bq1meb9Rx6yFn2NBbBZ (the design; this file carries the build stamp).
> The three pieces have been ON in production since 2026-09-09 and the certified lane's
> home-run band passes with them on. This plan makes the certification dedicated and
> repeatable, and it closes the three defects the check surfaced on the way: balls hit
> down the lines beyond the sector grid, one park whose wall moved inside the pool window,
> and the new parks whose fences rest on the league line.
>
> **The headline number.** On every real air ball of the pool window (242,957 balls, 21,892
> home runs, 2023 to 2026), the fence stage's decision is right on 91.8% of the balls it
> calls over the fence and misses 9.0% of the home runs, and nearly every error sits within
> ten feet of the fence line. Held-out seasons read the same. The lane's home-run rate per
> ball in play sits 1.5% under the pool's, inside its band.

**Date:** 2026-09-20
**Ticket:** SIM-478/479/480 (P2) in `BACKLOG.xlsx`, one row for the three. Next free ID: SIM-552.
**Evidence:** the live `park_geometry.json` from the production bundle (40 venues); three
read-only probes over `sim.outcome_pool` inside the app container (the decision on every
air ball, a leave-one-season-out rebuild of the lines, the per-season lines, the carry
model on balls that stayed in, the park run factors); the MLB Stats API's published fence
distances for the 2026 venues; the certified lane's record (`scripts/sim523_lane_h.txt`);
the code at commit 5782a72.
**Builds on:** the redesign plan's part C (`docs/audit/2026-09-08-sim523-play-picker-redesign-plan.md`)
and its build record in `CHANGES.md` (2026-09-08), the pool-totals grading ruling
(2026-08-20), the balanced certifying set (2026-09-09), and the ruling of 2026-09-16 that
no change waits on a per-change accuracy run: the cheap gates are the lanes and a smoke.

---

## 0. The short version

**What the ticket asks for.** When a fly ball or line drive heads for the wall, the
simulator checks the live park's real fence at that direction before it draws the play:
over the fence is a certain home run; short of it, no home run; the draw decides the rest.
The three pieces (a fence line per park and direction, a model of how far a ball flies,
and the decision itself) were built on 2026-09-08 and flipped on with the certified arm on
2026-09-09. The row says they are "still marked as pending their own dedicated accuracy
test", and the definition of done is a dedicated accuracy check on the standard test set
for home-run and wall-play outcomes.

**What the measurements say.**

- The decision is right where it can be. Against every real air ball of the window, a ball
  the stage calls over the fence was a home run 91.8% of the time, and 9.0% of the home
  runs were called short. Beyond twenty feet from the line the call is right 99.5% of the
  time or better; within five feet it is a coin flip (63% of the "over" balls and 30% of
  the "short" balls were home runs). A fence line is one number per park and ten-degree
  sector; the real wall varies inside a sector, and a projected landing point has its own
  error. Lines rebuilt without a season and tested on it read 0.90 to 0.92 and 7 to 10%,
  so the lines do not overfit their own window.
- The lane already grades the outcome. On the balanced 45-game set at 130 iterations, home
  runs per ball in play read 0.0452 against the pool's 0.0459, inside the band; home runs
  per team-game +1.5%, inside the band.
- The carry model almost never runs. 99.9% of the pool's air balls carry their own
  reported distance; the model stands in for 0.09%. It is exact on home runs (mean error
  0.0 ft) and long by 13 ft on balls that stayed in, which the 0.09% cannot turn into a
  visible bias.
- The check surfaced three defects, all in the geometry, none in the stage:
  1. **The sector grid stops at ±45 degrees, and 5.2% of air balls are hit beyond it** (a
     ball down the line reads −50; the grid clamps it to the edge sector, whose line sits
     20 to 60 ft beyond the foul-pole fence because the builder excluded those very
     balls). Their home-run rate is 9.0%, the same as everyone else's, and the stage calls
     24.5% of those home runs short: about 280 of the window's 21,892.
  2. **Camden Yards moved its left-field wall in for 2025, and the line blends the two
     walls.** On its 2025-26 left-field balls the live line calls 20.1% of the home runs
     short; a line from 2025-26 alone calls 7.5% short. One park, one third of its
     left-field home runs since the change.
  3. **A new park rests on the league line.** Las Vegas Ballpark, a 2026 home park with
     six games in the pool, has seven of nine sectors on the league line; Mexico City and
     Williamsport the same with two games and one. Steinbrenner Field (2025) is thin at six
     sectors. The published dimensions (the MLB Stats API: 340 / 380 / 415 / 380 / 340 for
     Las Vegas) are a floor the builder does not use.
- Coors carries: a home run there lands 21 ft beyond what its exit velocity and launch
  angle predict elsewhere. The wall-zone park kernel (bandwidth 0.02 on the run factor)
  gives a row 0.10 of run factor away a weight of 0.0000, so a Coors game draws its
  wall-zone balls from Coors-like rows and a sea-level game never draws a Coors row. The
  carry environment is contained by a weight that already exists.

**The plan.** Make the certification a script that runs in two minutes and a lane read
that already exists, fix the three geometry defects in one rebuild of `park_geometry.json`
(a grid to ±55 degrees; a season dimension with a change detector; a published-distance
prior for thin parks), add a wall-play check at the sampler level (the born ball's own
outcome against the drawn one, for balls within thirty feet of the live fence), and
surface the stage's own counts in the harness. No migration, no profile recompute, no
matrix rebuild; one bundle file, one app restart, one lane.

**What it will show.** After the rebuild the offline check should read 92% or better on
the over calls and 8% or fewer missed home runs, the down-the-line balls' missed share
should drop from 24.5% to under 10%, Camden's left field from 20% to under 8%, and the
lane's home-run band should stay green with the home-run rate a little closer to the
pool's (the missed home runs come back). No accuracy comparison against the betting line:
the fence is a fact filter, not a weight, and the ruling of 2026-09-16 puts the weights in
the sweep.

**Decisions** in §10: all four taken as recommended on 2026-09-21.

---

## 1. The mechanism

```
the pitch-result draw → the BORN ball: class (fly / line / ground / popup / bunt), exit velocity, launch angle, spray, its row's own distance
      │
      ▼  simulation/full_pool_sampler.py — battedball_draw, before the fielding draw
the fence stage (SIM_FENCE_STAGE=1, SIM_FENCE_MARGIN=0):
   carry  = the born ball's own distance            (99.9% of air balls)      → else the carry model on (ev, la)   (0.09%)
   fence  = park_geometry.json[venue][sector(spray)] (10-degree sectors, −45 … +45, clamped at the edges; the league line where a venue sector is thin)
   carry ≥ fence  → OVER : the cell's home-run rows only (the whole cell's when the class-filtered rows hold none)
   carry <  fence → SHORT: the home-run rows leave the candidate set
   no class / no venue / no geometry / no direction → the stage PASSES (fence_counts[3])
      │
      ▼  the fielding draw among the rows that remain (the born-ball kernel, the batter and fielder matrices, the situation, the platoon, the park kernel in the wall zone)
the drawn row IS the play: a home run, a double off the wall, a catch at the track …

the geometry (pipeline/batch/engine_artifacts.py, build_park_geometry, `--what park`): per venue × sector,
   the midpoint of the home runs' 10th-percentile carry and the kept air balls' 99th-percentile carry (10+ of each, else the league line)
the carry model: distance ≈ quadratic(ev, la), fitted on the window's 21,424 home runs (MAE 13.6 ft)
```

The stage never adjusts a drawn play. It removes rows the live park makes impossible (a
home run on a ball that cannot clear this fence) or certain (a ball that cleared it), and
the draw runs among what is left. That is why the certification question is "is the fence
line right?", not "is the weight right?".

---

## 2. The evidence

### 2.1 The three pieces, as they run today

| Piece | As built (2026-09-08) | As it runs (2026-09-09 →) | Already graded |
|---|---|---|---|
| The park geometry (SIM-478) | `park_geometry.json` in the bundle: 40 venues × 9 sectors, every MLB sector from both estimates ("both"); the league line for thin sectors; an overrides file merged on top (empty) | read by `fence_at`; the DuckDB `derived.park_geometry` table waits on the writer lock (SIM-524) | 20 unit tests: the quantile rule, the fallback, the overrides, the sector index |
| The carry model (SIM-479) | a quadratic in exit velocity and launch angle on 21,424 home runs; MAE 13.6 ft, RMSE 17.3 | the fallback when a born ball has no distance | validated on home runs only |
| The fence stage (SIM-480) | over / short / band / pass; the band 0 ft after a probe read a 10-ft band losing about 30% of home runs | `SIM_FENCE_STAGE=1`, `SIM_FENCE_MARGIN=0`, with the class filter and the wall-zone rule | the certified lane (45 × 130): HR per ball in play 0.04522 vs the pool's 0.04590 (−1.5%, PASS); HR per team-game 1.180 vs 1.163 (+1.5%, PASS) |

The stage's own counters (`fence_counts`: over, short, band, passed, no matching rows) are
kept by the sampler and read by one unit test; no harness prints them.

### 2.2 The decision on every real air ball of the window

The live geometry against the pool's own fly balls and line drives, 2023 to 2026, under
the production rule (a spray beyond ±45 clamps to the edge sector): 242,957 balls,
21,892 home runs, 242,652 decided (the rest have no distance and no exit velocity).

| Reading | Value |
|---|---|
| P(home run ǀ over) | **0.918** |
| P(home run ǀ short) | 0.008 |
| Home runs called short (missed) | **9.0%** (1,703 of 21,892 before the clamp; the same order after it) |
| "Over" balls that stayed in | 8.2% |
| Balls the stage passes (no carry) | 0.1% |

**By distance from the fence line** (ǀcarry − fenceǀ, the same balls):

| Band | Balls | Over: P(HR) | Short: P(HR) |
|---|---|---|---|
| 0–5 ft | 6,640 | 0.635 | 0.297 |
| 5–10 ft | 6,755 | 0.857 | 0.117 |
| 10–20 ft | 13,818 | 0.965 | 0.020 |
| 20–40 ft | 26,711 | 0.995 | 0.0014 |
| 40+ ft | 176,211 | 0.999 | 0.0001 |

Nearly every error lives within ten feet of the line, where one number per ten-degree
sector cannot follow the wall and a projected landing point carries its own error. 29.5%
of the window's home runs cleared their fence by under ten feet, 39% by ten to thirty, 31%
by thirty or more.

**By sector** (all venues): the lines read 0.945 to 0.952 on the over calls; the alleys
and centre 0.834 to 0.904, where the fences are deepest, vary most inside a sector and the
leaping catch lives. **By venue** (32 with 200+ decided balls): P(HR ǀ over) from 0.830
(Fenway) to 0.948 (the Coliseum); missed home runs from 5.8% to 11.4% (Daikin Park,
Fenway, Progressive Field, Camden Yards at the top).

### 2.3 Held-out seasons

Lines rebuilt from three seasons with the builder's own rule, tested on the fourth:

| Held out | Decided | P(HR ǀ over) | Missed home runs | Over-but-kept |
|---|---|---|---|---|
| 2023 | 59,722 | 0.899 | 7.4% | 10.1% |
| 2024 | 59,915 | 0.906 | 9.2% | 9.5% |
| 2025 | 60,873 | 0.913 | 9.9% | 8.7% |
| 2026 | 49,625 | 0.920 | 9.2% | 8.1% |

Within two points of the in-window reading on every season: the lines carry from one
season to the next.

### 2.4 What the check surfaces

**Defect 1 — the grid ends at ±45 degrees; 12,722 air balls (5.2%) are hit beyond it.**
Their home-run rate is 8.96%, the fair balls' 9.0%. The builder drops them, so the edge
sector's line (358.9 ft on the left, 352.4 on the right, the league) describes balls at
−40, not the ball at −50 aimed at a 302-to-355-ft foul pole. Under the production clamp the
stage calls 98.7% of their over calls right and **24.5% of their home runs short** (the
median of those balls sits 1.8 degrees past the edge, the 90th percentile 4.9, the 99th
12.7). About 280 home runs of the window.

**Defect 2 — a wall that moved inside the window.** The per-season lines find one park
whose sectors moved ten feet or more between 2023-24 and 2025-26 with ten or more home runs
on both sides: Camden Yards, left field, sectors 0 to 2 (392 → 372, 394 → 380, 397 → 377
ft). The live line blends the two walls (381, 384, 389). On Camden's 2025-26 left-field
balls (1,267; 134 home runs) it calls **20.1% of the home runs short** and none of its over
calls wrong; a 2025-26 line (368, 380, 377) calls 7.5% short and 4.6% of the over calls
wrong, the window's normal reading. No other park moved a sector that far.

**Defect 3 — the new parks.** 2026 venues in the pool with sectors on the league line or
under 20 home runs of support:

| Venue | 2026 games in the pool | League-line sectors | Thin sectors |
|---|---|---|---|
| Las Vegas Ballpark (5355), the Athletics' 2026 home | 6 | 7 of 9 | all 9 |
| Estadio Alfredo Harp Helú (5340), Mexico City | 2 | 8 of 9 | all 9 |
| Journey Bank Ballpark (2735), Williamsport | 1 | 9 of 9 | all 9 |
| Steinbrenner Field (2523), the Rays' 2025 home | — | 0 | 6 of 9 |
| Daikin, Comerica, Chase, Progressive, Oracle | 67–68 each | 0 | 1–2 (centre / an alley) |

The MLB Stats API publishes each venue's five fence distances (left line, left-centre,
centre, right-centre, right line; no wall heights). Against the live lines at the matching
sectors, the effective fence sits 15 to 60 ft beyond the published LINE distances (the
sector's midpoint is 5 degrees off the pole, the wall has height, and the landing point is
past the fence) and up to 23 ft short of the published alley and centre points (a published
point is the deepest spot; a sector spans about 70 ft of wall). Published distances cannot
certify a sector line, but they bound a new park's line where the pool has nothing.

**The carry model on balls that stayed in.** Fitted on home runs, it predicts +13.4 ft
long on kept air balls (fly balls −8.8, line drives +34.5: a line drive's reported distance
is where it was fielded, not where it would have landed). It runs on 0.09% of balls, so the
bias cannot show; it is recorded so nobody widens its use without refitting it on kept
balls too.

**The carry environment.** Mean (own distance − model) by venue on air balls of 250+ ft:
Coors +7.1 ft (home runs +21.3), the other 31 parks −15 to −3 with a spread of 4.0 ft. A
Coors row drawn into a sea-level park would carry too far; the wall-zone park kernel
(bandwidth 0.02; Coors' run factor 1.17–1.21 against a league range of 0.86–1.21) gives a
row 0.10 of run factor away a weight of 0.0000 and one 0.04 away 0.135, so the draw keeps
Coors rows at Coors. Contained by a weight that exists; no change.

### 2.5 The model, as it stands

- The fielding draw's candidate set is the exact base-out cell, filtered to the born ball's
  class, then by the fence stage; the weights are the born-ball kernel (bandwidth 1.0), the
  batter and fielder matrices (power 1), the situation, the platoon and the park kernel in
  the wall zone (300+ ft; bandwidth 0.02).
- The geometry is season-blind: one line per venue and sector over the whole window.
- `fence_at` takes a venue and a spray angle; the loop passes `venue_id` only when the
  stage is on, and `live_season` already travels on the same call.
- The lane's flags (`tests/acceptance/conftest.py`) run the class filter, the wall-zone
  rule, the fence stage at margin 0 and the born kernel at 1.0 — the production set.
- The balanced set's 45 games sit at 30 parks (one to three games each), Las Vegas and
  Camden among them.

---

## 3. Findings that shape the plan

**Finding 1 — the stage is right; the lines carry the error.** 91.8% right on the over
calls, 9.0% of home runs missed, nearly all within ten feet, the same on held-out seasons.
The three defects are all in the geometry table, and each has a specific fix.

**Finding 2 — the ticket's "dedicated accuracy check" should not be the betting-line
comparison.** The fence stage is a hard filter on facts (this fence, this carry), not a
draw weight, and the ruling of 2026-09-16 puts every weight in the comprehensive sweep and
leaves the cheap gates to certify a change. The dedicated check for a fact filter is the
decision itself against the real balls, plus the lane's band and a per-park read from the
lane's own output.

**Finding 3 — 5% of the balls are outside the grid, and they are home runs as often as the
rest.** Extending the grid to ±55 degrees (eleven sectors) puts the down-the-line balls in
sectors built from down-the-line balls.

**Finding 4 — a fence line needs a season.** Camden's wall moved; the next park will too
(the builder's per-season lines are the detector: a sector whose early and late lines
differ by ten feet with ten home runs on each side). The stage needs the line for the live
season, and the loop already carries the season.

**Finding 5 — a thin park needs a prior, not the league line.** The league line is the
median of thirty parks; a new park's published dimensions plus the league's typical gap
between the effective and the published fence at that sector is a better line, blended
toward the park's own balls as they arrive (support n against a prior of 10).

**Finding 6 — the wall PLAY is graded by the pool bands, not by the stage.** Doubles and
triples per ball in play pass the lane (+0.0%, −3.0%). A dedicated wall-play check reads
the fielding draw on born balls within thirty feet of the live fence, the born row's own
outcome against the drawn one, the split probe's method.

---

## 4. Data changes

No Postgres or DuckDB change. The geometry stays a bundle document (`park_geometry.json`);
the DuckDB `derived.park_geometry` table stays deferred behind the writer lock (SIM-524).
The document changes shape:

```json
{
  "sector_deg": 10, "spray_min": -55.0, "spray_max": 55.0, "n_sectors": 11,          // was -45 … 45, 9
  "league": [ … 11 … ],
  "venues": {"2": {"2023-2024": [ … 11 … ], "2025-2026": [ … 11 … ]},                 // a season GROUP per venue; one group for a park that did not move
             "5355": {"2026-2026": [ … ]}, …},
  "source": {"2": {"2025-2026": ["both", "both", "both", "prior", …]}, …},           // "prior" joins both / hr / kept / league / override
  "support_hr": { … per group … },
  "prior": {"5355": {"published": [340, 380, 415, 380, 340], "line": [ … 11 … ]}, …}, // the published distances + the league offset, per sector
  "moves": [{"venue": 2, "sectors": [0, 1, 2], "from": "2024", "to": "2025", "early": [392.1, 393.5, 396.9], "late": [371.7, 379.6, 376.9]}],
  "carry": { unchanged },
  "overrides": { unchanged, keyed venue → season group → sector }
}
```

A new bundle file `venue_dimensions.json` (the Stats API's `fieldInfo` for every venue in
the pool, fetched at build; the last copy kept when the API is down) feeds the prior.
`park_geometry.json` for a venue the loop asks about with a season outside its groups takes
the latest group.

---

## 5. Code changes, file by file

| File | Change | What |
|---|---|---|
| `pipeline/batch/engine_artifacts.py` | modified | the grid to ±55 (11 sectors); per-venue season groups with the change detector; the published-distance prior for thin sectors; `venue_dimensions.json`; the `moves` and `prior` blocks in the document |
| `pipeline/etl/mlb_venue_dimensions.py` | new | one function: the Stats API venues call (`hydrate=fieldInfo`) → `{venue_id: [leftLine, leftCenter, center, rightCenter, rightLine]}`; cached to the bundle |
| `simulation/full_pool_sampler.py` | modified | `fence_at(venue_id, spray, season)` picks the season group; `fence_decision` and `_fence_rows` pass the live season through; the counters unchanged |
| `simulation/sim_loop.py` | modified | the fence call carries `live_season` (already on the batted-ball call; one keyword) |
| `scripts/sim478_fence_check.py` | new | the certification script: the offline decision check (§2.2, §2.3, per venue and sector, the beyond-grid balls, the moved-wall test) with PASS / FAIL against the thresholds of §7; and `--lane <json>` the per-park read of a lane or harness output |
| `scripts/sim478_wall_zone_probe.py` | new | the wall-play check: the split probe's born-vs-drawn tap restricted to born air balls within 30 ft of the live fence; the drawn shares (home run, double, triple, out) against the born rows' own |
| `scripts/sim_stats.py` | modified | prints the fence counters (over / short / band / passed / no rows) per run, and writes them into `--json-out` |
| `tests/unit/test_sim523_fence_stage.py` | modified | 11 sectors; the season-group lookup (a park that moved; a season outside the groups); the prior; the detector on a toy pool |
| `tests/unit/test_sim478_fence_check.py` | new | the check's metrics on a 200-ball fixture with known answers; the thresholds' PASS / FAIL |
| `docs/technical/sim-loop-cheat-sheet.md`, `docs/technical/similarity.md` (the fielding section) | modified | the fence line per season; the certification's numbers |
| `BACKLOG.xlsx`, `CHANGES.md` | modified | the row deleted on close; the record |

### 5.1 The geometry builder

```python
# pipeline/batch/engine_artifacts.py
PARK_SPRAY_MIN, PARK_SPRAY_MAX = -55.0, 55.0          # SIM-478 certification: the down-the-line balls get their own sectors
PARK_N_SECTORS = 11
PARK_MOVE_FT, PARK_MOVE_MIN_HR = 10.0, 10              # the change detector
PARK_PRIOR_N = 10                                      # the published-distance prior's weight, in home runs

def build_park_geometry(con, out_dir, seasons):
    balls = the window's fly balls and line drives with a venue, a distance and a spray in [-55, 55)   # the builder no longer drops the line balls
    per (venue, sector, season): hr_n, hr_q10, kept_n, kept_q99                                        # the same quantile rule, per season
    league[sector] = median over venues of the whole-window sector estimates                            # as today, over 11 sectors
    for venue:
        groups = split_seasons(venue)        # one group [first..last] unless a sector's early/late lines (10+ HR each) differ by PARK_MOVE_FT:
                                             # then the change year opens a new group, found by the season where the sector's running line crosses the midpoint
        for group: line[s] = _line(hr, q10, kept, q99, fallback=prior_or_league(venue, s))              # the same midpoint rule inside the group
    prior(venue, s) = published_at(venue, s) + league_offset[s]                                         # published: the five API points interpolated to the sector's midpoint angle
                                                                                                        # league_offset[s]: the median over parks of (effective line − published) at that sector
    thin blend: line = (n_hr * own + PARK_PRIOR_N * prior) / (n_hr + PARK_PRIOR_N) when 0 < n_hr < PARK_PRIOR_N; prior alone at n_hr = 0
    doc["moves"], doc["prior"], doc["source"] record every choice; overrides merge last, keyed venue → group → sector
```

### 5.2 The sampler

```python
# simulation/full_pool_sampler.py
def fence_at(self, venue_id, spray, season=None):
    pg = self.a.park_geometry; sec = the clamped sector over pg["n_sectors"]       # 11 on the new document, 9 on an old one — the document says
    groups = pg["venues"].get(str(venue_id))                                        # {"2023-2024": [...], "2025-2026": [...]} or, on an old document, a plain list
    line = groups if isinstance(groups, list) else pick_group(groups, season)       # the group containing the season; the latest group when none does
    return line[sec] if line and line[sec] is not None else pg["league"][sec]
def fence_decision(self, born, venue_id, season=None): … self.fence_at(venue_id, born.get("spray_raw"), season) …
# _fence_rows and battedball_draw pass live_season (already a keyword of the call) through; off, byte-identical
```

### 5.3 The certification script

```python
# scripts/sim478_fence_check.py — read-only; runs inside the app container in about two minutes
def decision_check(pool, geometry, seasons):          # §2.2: every air ball of the window under the production rule
    → P(HR|over), missed share, over-but-kept share; the same by |carry − fence| band, by sector, by venue; the beyond-grid balls on their own
def held_out(pool, seasons):                          # §2.3: the builder's rule on three seasons, tested on the fourth, for each season
def moved_walls(pool):                                # §2.4: the early/late lines per venue sector; anything past PARK_MOVE_FT is listed with its per-season lines
def thin_parks(geometry, pool):                       # §2.4: venues in the current season with league-line or prior sectors, their game counts
def park_read(lane_json, pool):                       # --lane: per venue, the sim's HR per ball in play (from the per-game series joined to raw.games.venue_id)
                                                      #         against the game set's actor-matched expectation summed per park; the residual against the pool's park HR factor
def verdict(...):                                     # the §7 thresholds → PASS / FAIL per check, one line each, exit code
```

### 5.4 The wall-play probe

```python
# scripts/sim478_wall_zone_probe.py — the split probe's tap (born row's own outcome vs the drawn one), restricted:
#   born air balls with |carry − fence_at(live venue, spray, season)| ≤ 30 ft, the production flags, --games (the balanced set) --iters 30
#   → shares of home run / double / triple / out among drawn plays vs among the born rows; per side of the fence (short by 0-30, over by 0-30)
#   the standard error of a share on n balls; a check passes when every drawn share sits within 2 SE + 0.01 of the born share
```

---

## 6. Weights, bandwidth, power

Nothing moves. The fence margin stays 0 ft (decisive; the 10-ft band lost ~30% of home
runs in the build probe). The wall-zone park kernel stays at 0.02, the born-ball kernel at
1.0, every power at 1. The certification changes the fence LINES (facts), not a weight; the
sweep (SIM-548) owns the weights.

---

## 7. The certification thresholds (`scripts/sim478_fence_check.py`)

| Check | PASS when | Today (before the fixes) |
|---|---|---|
| A1 the decision, whole window | P(HR ǀ over) ≥ 0.90 and missed home runs ≤ 10% | 0.918 / 9.0% — PASS |
| A2 the decision, per venue (200+ decided balls) | every venue P(HR ǀ over) ≥ 0.80 and missed ≤ 15% | worst 0.830 / 11.4% — PASS |
| A3 held-out seasons | each within 3 points of the whole-window reading | within 2 — PASS |
| A4 the beyond-grid balls | missed home runs ≤ 12% (the fair balls' reading plus the pole's uncertainty) | 24.5% — **FAIL** → the ±55 grid |
| A5 moved walls | no listed sector where the live line's missed share on the late seasons exceeds the late line's by 5 points | Camden 20.1% vs 7.5% — **FAIL** → the season groups |
| A6 thin parks | no current-season venue with a league-line sector after the prior | Las Vegas 7 of 9 — **FAIL** → the prior |
| B1 the lane's band | `HR_BIP` PASS on the balanced 45 × 130 | PASS (−1.5%) |
| B2 the per-park read | the residual (sim − expectation) against the pool's park home-run factor, r ≥ 0.5 over the 30 parks, and no park beyond 3 SE | not yet read |
| C the wall play | every drawn share within 2 SE + 0.01 of the born rows' own, both sides of the fence | not yet read |
| D the counters | passed ≤ 0.1% of air balls; no-matching-rows ≤ 0.5%; over ≈ the pool's home-run share of air balls (9 ± 1%) | not yet read |

Tests: the unit file holds the thresholds and a fixture on which A1–A6 have known answers;
the lane's band is the acceptance lane's own; B2, C and D are the script's reads on the
lane and smoke outputs, recorded in the changelog.

---

## 8. Run book

```
# 0. the baseline: scripts/sim478_fence_check.py on the LIVE bundle (the numbers of §2, ~2 min, the app up — a read-only open works)
# 1. the code lands; ruff, mypy, the unit lane (the fence tests + the check's fixture)
# 2. the geometry rebuild: python -m pipeline.batch.engine_artifacts --what park   (~1 min; writes park_geometry.json + venue_dimensions.json; NO recompute, NO matrices)
# 3. the check again on the new document: A1–A6 all PASS (the expectation: ≥ 0.92 / ≤ 8%; the beyond-grid missed share < 10%; Camden's left field < 8%; Las Vegas on the prior)
# 4. restart the app (the workers load the bundle at start); the ten-game smoke with the fence counters (D)
# 5. the wall-play probe on the balanced set at 30 iterations (C; ~15 min)
# 6. the lane, 45 × 130, --json-out; the band (B1) and the per-park read (B2; the script's --lane)
# 7. close: CHANGES.md with the before/after table; delete the SIM-478/479/480 row; the cheat sheet and the technical reference
```

No accuracy comparison. No calibration refit (the geometry is not a profile). No
matrices. The DuckDB writer lock does not bite: the rebuild writes a bundle file, not the
database.

---

## 9. What could go wrong (ranked)

1. **The home-run band moves when the missed home runs come back.** The three fixes
   return roughly 1.3% (the grid) plus a park's share (Camden) of the window's home runs
   to the over side; the lane's band has a 3.5% floor and today's reading is −1.5%, so the
   expected move (+1 to +2%) lands inside it. If it does not, the line rule (the midpoint
   of the two quantiles) is the lever, not the margin.
2. **The change detector fires on noise.** Ten feet with ten home runs on each side is
   the same support rule the builder uses; the probe found exactly one park at that bar.
   A false split costs support (two shorter groups), not correctness; the document's
   `moves` block lists every split for review.
3. **The prior for a dome or an odd wall.** Published distances carry no wall height; the
   league offset assumes an ordinary wall. A new park with a tall wall (a Green Monster)
   would sit short until its own balls arrive; the blend at n = 10 corrects it within
   weeks. Las Vegas's first six games already carry 28 home runs.
4. **The per-park read's confound.** A park's sim home-run rate depends on the teams that
   played there; the actor-matched expectation from the game-set builder is the reference,
   not the pool's raw park rate. With one to three games per park and 130 iterations the
   standard error is about 0.3 points of home-run rate; the read is a sanity check on the
   sign and the extremes, not a fit.
5. **The ten-degree sector stays coarse.** Fenway reads 0.83 and the alleys 0.83–0.90
   because a sector spans about 70 ft of a wall that varies inside it. Five-degree sectors
   would halve the support behind every line; the fix is the published polygon of each
   wall, which no public source carries. Recorded as a follow-on, not built here.

---

## 10. Decisions for the owner

1. **What "the dedicated accuracy check" is — TAKEN 2026-09-21, as recommended:** the offline decision check on
   the pool's real air balls (A), the lane's home-run band and a per-park read from the
   lane's own output (B), the wall-play probe (C) and the stage's counters (D), with the
   thresholds of §7 — not the paired accuracy comparison against the betting line the
   row's wording could be read to mean. The fence is a fact filter; the ruling of
   2026-09-16 leaves per-change accuracy runs to the sweep. The alternative not taken: also run the
   250-game accuracy comparison on the home-run prop, three hours for a read the row's
   `min n` column says cannot resolve a change of this size.
2. **The three geometry fixes — TAKEN 2026-09-21, as recommended:** all three in one rebuild — the grid to ±55
   degrees, the season groups with the change detector, the published-distance prior for
   thin sectors. The alternative not taken: hand-written overrides for Camden's three sectors and Las
   Vegas's seven, which fixes today's two parks and not the next ones, and leaves the 5% of
   balls beyond the grid as they are.
3. **The wall-play check — TAKEN 2026-09-21, as recommended:** yes, as `scripts/sim478_wall_zone_probe.py` on the
   balanced set at 30 iterations (about fifteen minutes), because the ticket names wall
   plays and the pool bands grade doubles and triples over every ball, not the wall's.
   The alternative not taken: rely on the doubles and triples bands alone.
4. **The certification's home — TAKEN 2026-09-21, as recommended:** the check script stays in `scripts/` and
   runs in the run book of every future geometry rebuild (a nightly `--what park` changes
   the lines as the season's balls arrive); its thresholds live in the unit test. The alternative not taken:
   fold A1–A3 into the acceptance lane as a pre-flight step, which puts a two-minute
   read-only pass in front of every lane run.

Recorded, not asked: weather stays out of the carry model — the owner's roof-closed test of
2026-09-21 (the six retractable-roof parks, roof closed against roof open, the same fence
lines) found the fence decision no better indoors (P(home run ǀ over) 0.915 against 0.927,
missed home runs 8.7% against 9.2%, errors within ten feet 23.9% against 23.0%, none
significant); the carry itself runs 3 ft longer indoors with 1 ft less spread, the weather
footprint measured the day before, and the comparison is re-run after the fixes; the fence margin stays 0; the carry model stays a fallback fitted on
home runs (its +13 ft bias on kept balls is recorded beside it); the DuckDB
`derived.park_geometry` table stays deferred behind the writer lock; five-degree sectors
and the wall polygon are a follow-on.

---

## 11. Amendment (2026-09-21, after the run): the Coors air — the born ball's carry in the live park's air

> **STATUS — PROPOSED by the owner's question of 2026-09-21; decisions 5 and 6 TAKEN as recommended 2026-09-22 ("implement this updated design"); BUILT 2026-09-22 — the record is `CHANGES.md`.**
> The owner asked whether the fence check should run on exit velocity, launch angle and spray
> angle (a modelled distance that can carry each park's air) and how much distance varies for
> balls with the same three factors at the same park. The measurements below answer both and
> lead to a different fix: keep the ball's own distance, and add the park's air to it.

### 11.1 The question, measured

Every real fly ball and line drive of the pool window with the three factors, a distance and a
park (242,652 balls, 21,872 home runs). Three independent reviewers re-derived every figure.

**How much does distance vary at the same park with the same three factors?** About **13 ft**
(one standard deviation) for home runs, whose distance is the landing point (a nearest-neighbour
estimate at the same park, 21,860 balls; coarser bins 12.5–13.8 ft); about **14.5 ft** for all
air balls of 300+ ft (a caught ball's distance is the catch point, which for a fly ball sits
within a few feet of where it would have landed). The spread is not one number: 10 ft below
98 mph, 12 ft at 98–104, 14–15 ft at 104+ mph and at launch angles of 15–22°. Batter handedness
(the pull side) explains about a foot of it. **The park explains 4–5 ft in quadrature:** across
parks the same balls spread 14.0 ft, within a park 13.3. The park offsets themselves are small
and stable — every outcome-free design puts Coors at **+19 to +23 ft** (best estimate **+20 ft**,
standard error under 1 ft; flat across carry bands), Kauffman +10, Yankee Stadium −6, Fenway −4,
everything else within ±6 ft of the median park; fitted on 2023–24 and scored on 2025–26 the
offsets hold within 2.5 ft.

**Can the fence check run on the three factors?** Not as a yes / no call. The decision lives
within ten feet of the line and the factors leave 13–15 ft of scatter. The best factor-only
decision anyone could build — a cross-validated classifier that knows the park, the fence and
the season — gets its "over" calls right **73.5%** of the time and misses **14.7%** of home runs
(a gradient-boosted expected distance against the fence minus 8 ft: 71.1% / 13.6%); the
factor-only frontier reaches 81% only at 24–27% missed. The ball's own distance reads 91.7% /
7.8%. (A first read of 0.545 for a modelled distance was an artefact of fitting the carry model
on home runs alone — the balls that carried farthest for their factors — and is withdrawn.) Two
caveats the reviewers added: part of the measured distance's accuracy on real balls is
circular — a ball that hits the wall is measured at the wall and can never be called over
(43% of the non-home-runs measured within 15 ft short of the fence are doubles or triples) —
so the fair comparison for the simulator is the transfer test below; and a factor-only
PROBABILITY is well calibrated per park (Coors 8.95% against 8.87%), so a factor-only draw
could reproduce each park's home-run rate — but that would replace a fact filter by a model,
which the architecture rule does not allow.

**The transfer test — what the simulator actually does.** The simulator's born ball comes from
the pitch-result draw, which is park-blind: its distance was measured in the air of the park it
was hit in. Every pool air ball "born" into one park, against that park's fence:

| Park | Fair target: the pool's ball mix at the park's own home-run rates | (a) the ball's own distance — today | (b) a modelled distance in the park's air | (c) own distance + the park offset difference |
|---|---|---|---|---|
| Coors | 9.5% | **5.0%** (53% of the target) | 7.6–8.5% | **9.6%** (the reviewers' +20 ft) / **10.1%** (the built fit, +21.4) |
| Fenway | 8.1% | **10.5%** (+2.5 points) | — | **8.8%** (−4 ft) / **9.5%** (the built fit, −2.8) |
| PNC | 7.7% | 7.8% | — | 7.8% / 8.3% |
| Great American | 10.6% | 10.6% | — | 10.6% / 9.9% |

(The fair target is not the park's own home-run share — Coors' own balls are softer than the
pool's, 8.9% — but the pool's mix scored at the park's own rates per exit-velocity / launch-angle
/ spray cell.) Today's simulator gives Coors half its home runs and Fenway a third too many —
the lane's B2 extremes (Coors −2.5 points, Fenway +1). A modelled distance does not reach the
target and depends on the balls it is fitted on. **The ball's own distance plus the offset
difference lands near the target at every park** (the reviewers' best offsets: Coors 9.6 vs 9.5,
Fenway 8.8 vs 8.1, PNC and Great American exact; the built fit of decision 6 — the park term of
the home-run carry model, Coors +21.4, Fenway −2.8 — reads Coors 10.1, Fenway 9.5, PNC 8.3, Great
American 9.9). **Built and read 2026-09-22 on all 32 parks with 200+ decided balls:** the mean
gap to the target falls from 0.97 points with the ball's own distance to 0.46 with the offset,
the worst from 4.5 (Coors) to 1.4 (Yankee Stadium −1.4, Fenway +1.4; Kauffman and the Coliseum
+1.1). The 1.0-point gate the plan first set was the reviewers' fit's; the built fit misses it at
four parks, so A7's gate is 1.5 points — an owner call recorded in the check script and in
`CHANGES.md`, not a silent widening. On the real-ball checks (A1–A6) the adjustment is zero by
construction (the ball's park is the live park), so the certified accuracy does not move.

### 11.2 The mechanism

```
today:      carry = the born ball's own distance (measured in the air of the park it was hit in, venue_row)
            fence = the live park's effective line (built from balls hit in the LIVE park's air)
            → a sea-level ball in a Coors game is measured against a thin-air line: called short too often

amended:    carry_live = own distance + carry_offset[venue_live] − carry_offset[venue_row]      (feet; two stored facts)
            fence      = unchanged
            over / short as today; the stage's counters unchanged
            carry_live is ALSO the born ball's distance in the fielding draw's kernel (§12) and in the
            wall margin (§12.7); the candidate rows keep their own distance (see "one frame" below)
```

`carry_offset[venue]` is one number per park: the park's mean home-run carry against the
league's for the same exit velocity, launch angle and spray — the park term of the carry
model refitted with a park offset (design of §11.1; outcome-free: every home run counted,
none selected by outcome), centred on the median park. It is a fact about the park's air, built
nightly from the pool's own balls the same way the fence lines are, and written into
`park_geometry.json` as `"carry_offset_ft": {"19": 20.0, "7": 10.0, "3": -4.0, ...}` (null for
a park under 100 home runs → 0). The batted-ball pool already carries every row's `venue_id`,
so the stage reads `carry_offset[venue_row]` from the born ball's row without a new column.

**One frame for every distance the draw reads (the owner's question of 2026-09-21).** The
adjustment is not only the fence check's. The born ball's distance is read three times — by
the fence check, by the fielding draw's kernel as the fourth feature (§12), and by the wall
margin (§12.7) — and all three read `carry_live`, the born ball's landing point in the LIVE
park's air. The candidate rows are NOT adjusted: a row's own distance already IS its actual
landing point, in the air of the park where its play happened, against that park's wall and
outfielders. So every distance the draw compares is an actual landing — the born ball's in the
live park, the row's in its own — and a Coors game's warning-track ball is matched to plays
that landed at the warning track, not to 365-ft outs. The alternative, expressing every row
in neutral air (its distance minus its park's offset) and the born ball likewise, compares the
balls' intrinsic carry instead: fine for "which ball is like this one", wrong for "which PLAY
is like this one", because a Coors row's 400-ft double off the wall would be read as a 380-ft
ball. Neutral air is the right frame for the fence LINE builder's view of a ball's carry only
in the sense already used — each park's line is built from its own balls in its own air and
`carry_live` is brought to that air. Left as it stood, §11 would have adjusted the fence check
and not the kernel, and a sea-level ball born at Coors would clear the right fence and then
draw its play among balls 20 ft too short; that is corrected here. The other three features
(exit velocity, launch angle, spray) are the ball's own and need no frame.

**The rule question, stated plainly.** "The drawn row IS the play" forbids a post-draw
adjustment of a drawn play. This adjustment does not touch the play: it expresses the born
ball's carry — a measurement — in the live park's air before the fence check and the draw,
using two stored facts, exactly as the fence line expresses the wall in the same air. It adds
no weight and no formula on an outcome. The owner decides whether that reading holds
(decision 5).

### 11.3 Code changes (the amendment)

| File | Change |
|---|---|
| `pipeline/batch/engine_artifacts.py` | `carry_offsets(balls)` — the park term of the home-run carry fit (the §11.1 design, the spray terms included), centred on the median park; null under 100 home runs; written into the document as `carry_offset_ft`; a `moves`-style listing in the log |
| `simulation/full_pool_sampler.py` | `carry_of(born, venue_live)` adds `offset[venue_live] − offset[venue_row]` when the document carries the table (an old document: no change); `battedball_new_pa` computes it once per born ball and writes it back as the born ball's `dist` before the fence check AND the kernel (`_f_born_similarity` reads `born["dist"]`), so the fence, the kernel and the §12.7 margin read one number; the candidate rows' distances are untouched; `fence_decision` passes the live venue through; a seventh counter is NOT added — the shift is a fact, not a decision |
| `scripts/sim478_fence_check.py` | A7 the transfer check: every pool air ball born into each park with 200+ decided balls, the share called over against the fair target (the pool's mix at the park's own cell rates), with and without the offset; PASS when every park sits within 1.5 points of its target with the offset (built 2026-09-22: the 1.0-point gate of the reviewers' fit misses at four parks on the decision-6 fit; the widening is recorded) |
| `tests/unit/test_sim478_geometry_builder.py`, `test_sim478_fence_season_groups.py`, `test_sim478_fence_check.py` | the offset's fit on a toy pool (a park whose balls carry +20 ft gets +20), the stage's shift (a sea-level row born at Coors gains 20 ft; a Coors row born at PNC loses 20; the same park → 0; an old document → 0), the kernel reads the shifted distance (the same born ball draws 20 ft longer rows at Coors than at PNC) while the rows' distances stay as stored, A7 on a fixture |
| `docs/technical/sim-loop-cheat-sheet.md`, `simulation.md` | one sentence each |

### 11.4 Certification and run book

A1–A6 unchanged (the adjustment is zero on a ball measured in the live park). New: **A7** as
above. **B2 redesigned** so it can decide: every park with three or more games in the set
(more games per park than the balanced set gives today — the set builder gains a `--min-games
-per-park` option), the expectation's own sampling error counted in the SE, the grade on the
extremes (no park beyond 3 combined SE) with the correlation reported, not graded. Run book:
`--what park` (the offsets join the document), `scripts/sim478_fence_check.py` (A1–A7), the app
restarted, the lane with the record, `--lane` for B2.

### 11.5 Decisions for the owner (the amendment)

5. **The born ball's carry in the live park's air (§11.2) — TAKEN 2026-09-22, as recommended:** adopt as a fact of the air, or rule
   it a post-draw adjustment and leave Coors at half its home runs until the born ball itself is
   park-conditioned (a weight in the pitch-result draw — the sweep's). Recommended: adopt.
6. **The offset's fit — TAKEN 2026-09-22, as recommended:** the park term of the home-run carry model (outcome-free, +20 ft at
   Coors), not the kept-ball or all-air-ball residuals (+15 / +16 ft), which the deep fence and
   the catch points truncate. Recommended: the home-run fit, as specified.

---

## 12. Amendment (2026-09-21, after the run): the wall play — the born-ball kernel measured on the born ball's class

> **STATUS — decisions 7, 8 and 9 TAKEN as recommended 2026-09-22 ("implement this updated design"); BUILT 2026-09-22 — the record is `CHANGES.md`. PROPOSED by the owner's instruction of 2026-09-21 ("adopt the class filter for
> batted-ball similarity for the wall plays"), specified here; nothing of §12 is built.**

### 12.1 The defect, restated

Check C failed on the short side: for a born air ball 0–30 ft short of the live fence the
fielding draw returned singles 12.5%, doubles 9.9%, outs 76.7%, where the born rows' own
outcomes read 1.8 / 25.9 / 69.6 and the pool's balls within 30 ft of their own park's fence read
2.2 / 28.3 / 63.1. The drawn row's ball carried 60 ft shorter than the born ball on average. The
cause is the born-ball kernel's ruler: it z-scores the four features (exit velocity, launch
angle, spray, distance) by the WHOLE pool's spread — distance 135 ft, launch angle 28°, with
ground balls in the population — and divides the exponent by the four features, so at bandwidth
1.0 a row 60 ft shorter keeps 98% of an exact match's weight. The class filter puts only fly
balls in a fly ball's candidate set, but the kernel then measures those fly balls on a ruler
made for every ball in play.

### 12.2 The change: the class ruler

The kernel's mean and spread come from the born ball's CLASS — fly balls measured against fly
balls, line drives against line drives, ground balls against ground balls:

| Ruler (one SD) | exit velocity | launch angle | spray | distance |
|---|---|---|---|---|
| the whole pool (today) | 14.8 mph | 28° | 28° | **135 ft** |
| fly balls | 10.2 | 9.8 | 26 | **64 ft** |
| line drives | 11.6 | 5.6 | 26 | 71 ft |
| ground balls | 15.5 | 17.8 | 25 | 38 ft |

The change is in `_bb_born_z_stats(hand, cls)`: per (hand, class) statistics over the pool's
rows of that class, the same statistics for the live ball and the candidate rows; the density
correction (`_born_inv_density`) recomputes under the same kernel, with the class in its cache
key. Nothing else in the draw moves. With the class filter on, the candidate rows are already
the born class, so the class MEAN cancels and the change is a per-feature rescale of the
kernel: a fly ball's launch angle is measured at 0.34 of the pool's scale, its distance at
0.48, its exit velocity at 0.69, its spray at 0.92 — a kernel with a separate width per
feature, not a new similarity. **The exponent's spread over the four features is the second
half of the fix**: with the class ruler alone (V1 below) a row 60 ft shorter still keeps 90% of
an exact match's weight; with the exponent per feature (equivalently bandwidth 0.5 under
today's formula) it keeps 64%, and a row 30 ft shorter 90%. That second half is a bandwidth,
which the ruling of 2026-09-16 assigns to the sweep — decision 7 asks the owner to place it.

### 12.3 The evidence (eight balanced-set games × 12 iterations, the production flags; the same seeds; two independent replications on eight other games)

| Variant | near-wall short balls: drawn singles / doubles / triples / outs (the born rows' own, the same run: 1.2 / 24.4 / 3.1 / 71.3) | the drawn row's carry − the born ball's | per game: 1B / 2B / 3B |
|---|---|---|---|
| V0 today — the pool ruler, bandwidth 1.0 | 12.6 / 7.8 / 1.0 / 78.6 | −69 ft; 22% within 30 ft | 11.01 / 3.02 / 0.31 |
| V1 the class ruler, bandwidth 1.0 | 8.3 / 11.5 / 0.8 / 79.4 | −54 ft; 34% | 10.70 / 3.25 / 0.27 |
| **V2 the class ruler, the exponent per feature** | **1.4 / 20.1 / 1.8 / 76.7** | **−22 ft; 61%** | 11.07 / 3.41 / 0.31 |
| V3 the pool ruler, bandwidth 0.5 | 9.4 / 10.1 / 2.7 / 77.5 | −54 ft; 31% | 10.83 / 3.29 / 0.33 |

The ruler alone helps a little (V1); the bandwidth alone helps a little (V3); together (V2)
the drawn row's ball carries within 30 ft of the born ball 61% of the time instead of 22%.
**Two replications on the balanced set's later eight games (seeds of their own) hold the
mechanism and temper the size:** V1 replicates exactly (−53 ft, 34% within 30 ft); under V2
the drawn near-wall doubles read 18.8% and 15.1% against the born rows' own 28.5% and 30.1%,
outs 75–77% against 65% — V2 closes about two thirds of today's gap (V0's doubles gap on the
same games is 27 points), not all of it. **The residual is structural, not a bandwidth
question:** on the short side the fence stage removes every home-run row from the candidates,
which cuts off the long side of the born ball's neighbourhood, so the drawn carry stays 20–25
ft short at any bandwidth. And a candidate row at the same absolute distance is not the same
wall play: a 365-ft ball in a park with a 400-ft fence was a routine out, in the live park with
a 380-ft fence it is a ball off the wall. The wall play is about the distance to the WALL, not
the distance from the plate — the next step in §12.7.

The per-game totals on 96 game-sims per arm cannot resolve a change (the standard error of a
two-arm difference: 0.27 doubles, 0.52 singles, 0.68 hits a game; z-scores above 2 appeared
with opposite signs across seed sets). The one consistent sign across the three reads is
doubles, +0.09 / +0.22 / +0.39 a game (mean +0.23, pooled SE about 0.15), which is the size the
mechanism predicts — about three near-wall short balls a game gaining eight to ten points of
doubles — and is the wall's doubles coming back from the short balls where the smear put them.
Home runs are NOT a total the kernel can move: with the margin at 0 every air ball gets a
decisive fence read before the draw (the home-run count equals the "over" count within one to
four per 96 games); the home-run moves between variants are trajectory noise. The lane's
doubles / triples / singles bands are the grade of whether the totals stay inside the pool's;
resolving a +0.25 doubles-a-game move at two standard errors takes about 300 game-sims an arm.
Two details for the build from the replication: the density correction's 5%-of-median floor
bites more rows at the tighter kernel (read the floored share before the default is set), and
the kernel's valid mask should require a distance above 0 when the pool carries one (0.1% of
rows hold a filled 0).

### 12.4 Code changes (the amendment)

| File | Change |
|---|---|
| `simulation/full_pool_sampler.py` | `_bb_born_z_stats(hand, cls)` per class (the pool-wide statistics when the born ball has no class); `_f_born_similarity` and `_born_inv_density` take the class; `SIM_BB_BORN_PER_FEATURE` (0/1) selects the exponent per feature — OFF keeps today's formula byte-identical; the cheat sheet's row |
| `simulation/production_factory.py`, `docker-compose.yml`, `tests/acceptance/conftest.py` | the flag, its production value per decision 7, the lane's flag table |
| `tests/unit/test_sim523_fielding_split.py` (or the born-kernel test file) | the class statistics (a fly ball's ruler is the fly balls'); a 60-ft gap's weight under each setting (0.976 / 0.896 / 0.644); byte-identity with the flag off and the class filter off |
| `scripts/sim478_wall_zone_probe.py` | unchanged — it is the acceptance test of §12 (C must PASS on the short side) |

### 12.5 Certification and run book

The cheap gates (the unit and regression lanes, the ten-game smoke); the wall-play probe on
the balanced set at 30 iterations — C PASS on the short side is the acceptance; the lane at
45 × 130 — the singles, doubles and triples bands must stay inside the pool's (the wall's
doubles come back from the short balls where the smear had put them; if the total moves out of
band, that is the bandwidth's business and the sweep's).

### 12.6 Decisions for the owner (the amendment)

7. **The class ruler — TAKEN 2026-09-22 (V2, as recommended):** adopt (the owner's instruction). **The exponent per feature:** land it
   with the ruler now (V2 — the variant that closes two thirds of the wall-play gap), or leave
   the exponent as it is (V1 — a modest gain) and hand the bandwidth to the sweep. Recommended:
   V2 now, because the ruler without it barely moves the wall play, and the totals bands grade
   the whole.
8. **The order of the two amendments — TAKEN 2026-09-22:** §11 first (it changes which balls the fence calls
   over), then §12 (it changes what happens to the ones it calls short), each with its own
   probe read and one lane at the end.

### 12.7 The distance to the wall as the wall-zone similarity — measured (the owner's question of 2026-09-22)

The third of the gap that §12 leaves is the part no bandwidth reaches: a candidate row is
chosen by its distance from the plate, but a wall play is decided by its distance from the
WALL. The owner put it exactly: a 385-ft ball is a fly out with a 420-ft fence and a wall ball
with a 390-ft one — so should the draw compare the raw distance, the feet short of the fence
(the margin), or the distance as a share of the fence (the ratio)? Measured on every real fly
ball and line drive of the window that was not a home run (220,780 balls; the fence from the
live document at the ball's own park, spray and season; the outcome graded as an extra-base
hit — a double or a triple — against an out):

**At a fixed raw distance the outcome depends on the fence; at a fixed margin it does not.**
The extra-base-hit share of balls carrying 345–360 ft is 33% in front of a shallow fence, 19%
a middle one, 9% a deep one; at 360–375 ft, 49 / 32 / 11%. Hold the MARGIN fixed instead and
the three fence depths read alike: 10–20 ft short 33 / 36 / 26%, 20–30 ft short 25 / 23 / 16%,
0–10 ft short 46 / 54 / 44%. Averaged over the bins, the spread across fence depths is 0.20
for the raw distance, 0.09 for the margin and 0.10 for the ratio. In the band the wall-play
probe reads (0–30 ft short), the extra-base-hit share is 32 / 35 / 27% for shallow, middle and
deep fences while the mean distance runs 340 / 368 / 389 ft.

**What each descriptor adds to a prediction of the play** (a five-fold logistic fit, folds by
game, with the ball's exit velocity, launch angle and class as the controls; AUC is the chance
the model ranks a random extra-base hit above a random out):

| Descriptor added to the controls | log-loss (lower is better) | AUC |
|---|---|---|
| none | 0.3241 | 0.849 |
| the raw distance | 0.3237 | 0.848 |
| the ratio (distance / fence) | 0.3116 | 0.862 |
| **the margin (feet short of the fence)** | **0.3089** | **0.865** |
| the raw distance AND the margin | 0.2987 | 0.876 |
| the margin as a curve (10-ft steps) | 0.2922 | 0.879 |

The raw distance adds nothing the ball's own exit velocity and launch angle do not already say;
the margin adds the wall. Feet beat the ratio by a little, and feet are the physical quantity
(the warning track, the fielder's reach and a wall's height are all in feet — a 20-ft margin is
the same play at a 340-ft fence and a 420-ft one; a 5% margin is 17 ft at one and 21 at the
other). And the two together beat either alone: the margin says where the ball sits against
the wall, the raw distance where the fielder was standing, and both matter for what happens.

**The design that follows.** Every row of the batted-ball pool gets its own margin — the row's
carry minus its own park's fence at its own spray and season (`fence_at(venue_row, spray_row,
season_row)` on the geometry document), a fact computed once at bundle build and stored as
`margin_ft`; a born ball's margin is `carry_live` (§11) minus the live fence. For an air ball
the draw then reads rows by the margin to their own wall as well as by the born ball's four
features (the raw distance stays — the fit says both carry information): 15 ft short of the
live wall draws plays that were 15 ft short of theirs. As a fifth kernel feature under the
class ruler it is a weight (the sweep's); as a hard band on the margin for wall-zone balls
(the class filter's shape: rows within ±15 ft of the born margin, the cell when that is empty)
it is a fact filter, and it removes the truncation the fence stage causes — the short side's
neighbourhood is then balls that stayed in near THEIR wall, which is exactly the population
the wall-play probe reads (28% doubles). The band's width is the one number to choose; the
margin table above says 10 ft changes the play (46% → 33% → 25%), so ±15 ft is where to start
and the wall-play probe is the grade. Recorded as the follow-on that would take C from two
thirds to the whole, for the owner to place (a fact filter now, or a kernel feature in the
sweep) — decision 9.

9. **The wall margin — TAKEN 2026-09-22, as recommended (the band now):** a hard band on the margin for wall-zone air balls now (±15 ft, the
   raw distance kept in the kernel), or the margin as a kernel feature in the sweep.
   Recommended: the band now — it is the same shape as the class filter and the fence stage,
   and it is the part of the wall play that no bandwidth reaches.
