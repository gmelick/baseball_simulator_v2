# Build plan — the running game on the pitch: the pickoff before the pitch, the steal on the pitch it rides, the third out, and the runners on a dropped third strike (SIM-554)

> **STATUS 2026-10-03 — CLOSED. Built 2026-10-01; the run book RAN 2026-10-03; production runs
> the new order.** All four decisions are taken (§10). The code of §4 to §7 is built, two review
> rounds ran (six reviewers, then three; two skeptics checked every finding), and every
> confirmed defect is fixed. §13 is the build and run record: what was built, where it departs
> from this plan and why, and what the run book read (§13.4). The
> design history (versions 1 to 3, the replay, the decisions) follows unchanged; the readable
> page is https://claude.ai/artifact/UP5z7ErWR9cgTnKwYENc8Y (v3).
>
> **The order version 3 plans, on every pitch with a runner who could steal.** (1) The pickoff
> draw, before the pitch. (2) The pitch. (3) Its result. (4) The steal draw, among real pitches
> with that result. Each step is a draw of real rows; the batter's result is drawn with no
> knowledge of the steal, and the steal never changes it.
>
> **Why.** The steal draw runs before the pitch today and knows only the count, so 36% of the
> simulator's attempts land on a foul, a ball in play or a hit by pitch, where real runners never
> earn one (§2.1). A replay of 74,296 real pitches picked the fix among eleven designs: keep the
> steal its own draw and let it read the pitch (§11). The owner's question about pickoffs then
> found that a fifth of real pickoff outcomes never reach the pool, because they come before a
> plate appearance's first pitch and the pool has only pitch rows (§2.8).

**Date:** 2026-09-29 (version 1); 2026-09-30 (version 2: the replay, §11); 2026-10-01 (version 3:
the pickoff draw, §12)
**Ticket:** SIM-554 (P2) in `BACKLOG.xlsx`. Next free ID: SIM-557 (the subtitle row of the main
checkout's working copy; SIM-556 is the balance weight of the steal draw, filed 2026-10-01).
**Evidence:** the loop (`simulation/sim_loop.py`), the sampler (`simulation/full_pool_sampler.py`),
the pool builder (`pipeline/batch/player_profile_computor.py`) and the artifact export
(`pipeline/batch/engine_artifacts.py`) at commit d44855b; the dropped-third-strike plan's §11.3
(`docs/audit/2026-09-22-sim484-dropped-third-strike-box-credits-plan.md`); read-only queries on
`raw.pitches` and `raw.play_events` for 2023–2026 (the session scratchpad's `sim554/`); the two
45 × 130 lanes of 2026-09-21 and 2026-09-22 (`scripts/sim478_lane.txt`); the replay of 2026-09-30
(`scripts/sim554_running_game_replay.py`, `scripts/sim554_replay_report.txt`); the pickoff check
of 2026-09-30 (§2.8).
**Decisions** in §10: four; all taken.

---

## 0. The short version

**What the ticket asks for.** A steal attempt never resolves on a ball in play or on a two-out
third strike the catcher holds, and the steal bands stay green. A pickoff or a caught stealing
that makes the third out before the pitch leaves the batter's turn unused and credits no pitch
event. A dropped-third-strike reach moves the unforced runners the way a got-away pitch moves
them elsewhere. A unit test pins each case, and the ten-game smoke shows no collapse.

**Terms.** The *steal opportunity pool* holds one row per real pitch on which a steal was
possible (a runner on first with second open, or on second with third open), attempted or not.
The *class* of a pitch is one of six: ball, called strike, swinging strike, foul, in play, hit
by pitch. A *hard filter* keeps only the pool rows that match the live situation exactly; the
similarity weights then rank those rows. A *dead ball* is a pitch on which play stops (a foul,
a hit by pitch); a runner who was going returns. The *got-away advance* is the loop's rule for a
wild pitch or a passed ball: every runner moves up one base. A *pickoff row* (new in version 3)
is a row of the pool that stands for a pickoff throw with an outcome, not for a pitch.

**What the code and the data say.**

- **The steal draw runs before the pitch and reads only the count.** The pre-pitch hook draws a
  row from the (target base, outs, balls, strikes) cell; the pitch draw then runs on its own; the
  steal resolves on whatever pitch came out.
- **Where the loop's attempts land, against where real attempts ride** (2023–2026, 1,004,622
  opportunity pitches, 14,577 real attempts): balls 36% against 64%; called strikes 16% against
  25%; swings and misses 12% against 10%; fouls 19% against 1%; balls in play 17% against 0%. So
  36% of the loop's attempts land on a pitch that never carries a real one, about 0.29 per
  team-game.
- **The real rate by class is the pool's own answer.** Runners go on 2.6% of balls, 2.3% of
  called strikes, 1.2% of swings and misses, 0.08% of fouls, and on none of 176,696 balls in
  play or 3,190 hit by pitches. Summed over the loop's own pitch mix these rates give today's
  attempt volume.
- **A steal is never the runner's decision alone.** The records show one only when the runner
  went AND the batter did not hit the ball. The batter barely changes how often a steal is
  recorded for the same kind of runner (3.13, 3.12 and 3.13 per 100 pitches for the runners who
  go most, across contact, middle and swing-and-miss hitters); he changes what it rides.
- **The replay picked the design** (§11). Eleven ways of sampling the steal, scored on 74,296
  real pitches of 2026. The steal as its own draw reading the pitch's class is best (−2.06 ± 0.35
  against today). Every design that puts the steal on a pitch row is worse than today: the pitch
  draw's bucket holds about five steal rows, too few for "runners like this one" to count.
- **Two of the four defects vanish with the class.** Real data hold zero attempts on a two-out
  third strike and zero on ball four, so a class-filtered draw stages none there.
- **A fifth of real pickoffs never reach the pool** (§2.8). The pool tags a pickoff to a pitch
  thrown before the throw. When the throw comes before the plate appearance's first pitch there
  is no such pitch, and the outcome is dropped: 394 of 1,788, about 95 a season. The tagged ones
  are right (the runner picked off is the row's runner 99.4% of the time).
- **The third out before the pitch is a pickoff, never a caught stealing.** About 130 a season.
  Today, if the drawn pitch would have ended the plate appearance, the loop credits that
  strikeout or walk and moves the batting order.
- **Real runners move on a dropped-third-strike reach**: 28 of 32 unforced chances.

**The plan (version 3).** On every pitch with a runner who could steal, four draws in the order
things happen. (1) The pickoff draw, before the pitch, from the pool's rows for this base, outs
and count: it answers "picked off, a throw that got away, or neither". The dropped pickoffs come
back into the pool as pickoff rows, in the situation before the throw. A pickoff that makes the
third out ends the half-inning there: no pitch is drawn, nothing is credited, the same batter
leads off. (2) The pitch and (3) its result, as today, on the bases the pickoff left. (4) The
steal draw, among real pitches with the same base, outs, count and result: it answers "went or
stayed, safe or caught". A group answers from its own rows however few; nothing falls back and
nothing is cancelled. Migration 0031 adds two columns to the pool (the pitch's class; the mark
of a pickoff row). With two outs a held third strike is the third out before any throw
(decision 3, taken). The dropped-third-strike reach runs the got-away advance first (decision 4,
taken). No weight, bandwidth or power changes; one flag (`SIM_STEAL_PITCH_CLASS`, default on).
The steal pool is rebuilt for the four window seasons and exported alone into the bundle, with
the app stopped for the write. Twenty-five tests, a running-game census on the balanced 45
games, the ten-game smoke, one 45 × 130 lane.

**Decisions** in §10: four; all taken.

---

## 1. The mechanism

### 1.1 The loop's order today

```python
# simulation/sim_loop.py — step_pitch (abridged)
self._pre_pitch_hook(state)                       # the IBB draw, then _steal_opportunity_draw(state)
pitch_outcome = self._full_pool_outcome(state)    # the pitch draw: knows nothing of a staged steal
adv = advance_count(state.balls, state.strikes, pitch_outcome)
if not adv.terminal:
    self._resolve_steal_outcome(state, result)    # the steal resolves on whatever pitch came out
    if state.is_half_inning_over():               # a caught stealing as the third out
        self.advance_half_inning(state); return   # RIGHT: no PA credit, the same batter leads off
    ...got-away advance unless a steal or pickoff moved someone; commit the count; return
# terminal
first_open_at_pitch, outs_at_pitch = ...          # SIM-484: the state at the pitch
self._resolve_steal_outcome(state, result)        # the steal (or pickoff) BEFORE the K / walk / ball in play
if not state.is_half_inning_over():
    ...resolve in play / walk / hit by pitch / strikeout
self._end_of_pa(state, result)                    # ALWAYS: accumulate the PA, advance the order, roll the half
```

Three things follow from this shape. First, the steal draw's cell is `(target, outs, balls,
strikes)` and nothing else, so its rows include every class of pitch and the drawn attempt lands
on whatever the pitch draw produces. Second, a pickoff (staged by the same draw, resolved in
`_resolve_steal_outcome` before the pitch's event) that makes the third out on a terminal pitch
falls through to `_end_of_pa`, which credits the drawn pitch's strikeout or walk to the batter
and the pitcher (the `PlayResult` was created with that event) and advances the batting order.
Third, with two outs and a held third strike, a staged steal resolves first: a safe steal credits
a stolen base and a caught stealing records the third out, and the strikeout that ended the
inning first is never resolved, though `_end_of_pa` still credits it.

### 1.2 The steal draw

```python
# simulation/full_pool_sampler.py — steal_draw (abridged)
rows = meta["cells"].get((outs, balls, strikes))          # the hard filter: the exact count cell
w = recency * exp(-(score_diff gap)^2 / 2σ²)               # σ = 2 runs
w *= runner_steal · pitcher_steal · catcher_throwing        # the actor score matrices, power 1
w = where(attempted, w * aggression, w)                     # the batting manager's rate over the league's
draw one row -> (attempted, success, pickoff_out, pickoff_advancing, pickoff_error)
```

The pool (`sim.steal_opportunity_pool`, migration 0015 + 0017) holds per row the runner, the
pitcher, the catcher, the target base, the count, the outs, the score margin, `attempted`,
`success`, the three pickoff labels and the recency weight. It is built by a join of
`raw.pitches` to `sim.pitch_pool` on the pitch's natural key; the pitch pool row carries
`outcome_type`, the class, and the builder does not copy it.

### 1.3 The pickoff labels

The play records (`raw.play_events`) hold every pickoff throw with an outcome. The builder maps
each one to the pair it belongs to, from the base state entering the throw: a runner on first
with second open is the target-2 pair, a runner on second with third open the target-3 pair.
It then tags ONE pitch row of the same plate appearance and the same pair: the first pitch on
which the runner did not steal. That pitch was thrown before the throw, in the situation the
throw happened in. A tagged row says one of three things: an out at the runner's base; an out
while he broke for the next base (scored a caught stealing); an errant throw that moves him up.

The steal draw reads the tag with the steal: a drawn row that carries a pickoff outcome retires
or moves the runner, and no steal is attempted on that pitch. The pitch is then thrown.

Two things follow. A pickoff that comes before the plate appearance's first pitch has no pitch
row in its situation, and the builder drops it (§2.8). And the tag sits on the plate
appearance's first pitch, so the simulator's pickoffs almost all happen at a 0-0 count.

### 1.4 The dropped-third-strike branch

```python
# _resolve_strikeout (abridged)
d3k = self._dropped_third_strike(state, result, ...)      # swinging, got away, first open or two outs
if not d3k and got_away and no steal / pickoff this pitch:
    self._resolve_got_away_advance(state, result)         # every runner one base — NOT on the reach branch
if d3k:
    forced_run = self._force_on_reach(state, result)      # the batter to first; only FORCED runners move
```

A runner on second with first open, or a runner on third, stays where he is while the ball is
loose behind the plate.

---

## 2. The evidence

All counts are 2023–2026 regular-season rows with `data_quality_flag = FALSE`, the pool's own
window; the opportunity shape is the pool's (a runner on first with second open → target 2; a
runner on second with third open → target 3). The class is the pool's coding (a two-strike foul
tip or foul bunt is a swinging strike, the SIM-553 rule).

### 2.1 Where attempts ride

| Class | Opportunity pitches | Share of pitches | Real attempts | Share of real attempts | Real rate | The loop's attempts, expected | Share |
|---|---:|---:|---:|---:|---:|---:|---:|
| ball | 360,289 | 35.9% | 9,385 | 64.4% | 2.61% | 5,316 | 36.5% |
| called strike | 154,760 | 15.4% | 3,583 | 24.6% | 2.32% | 2,308 | 15.8% |
| swinging strike | 117,013 | 11.7% | 1,456 | 10.0% | 1.24% | 1,689 | 11.6% |
| foul | 192,674 | 19.2% | 153 | 1.05% | 0.08% | 2,733 | 18.8% |
| in play | 176,696 | 17.6% | 0 | 0 | 0 | 2,486 | 17.1% |
| hit by pitch | 3,190 | 0.3% | 0 | 0 | 0 | 46 | 0.3% |
| **all** | **1,004,622** | | **14,577** | | **1.45%** | **14,577** | |

"The loop's attempts, expected" applies the pool's attempt rate of each (outs, balls, strikes)
cell to that cell's real class mix — what a draw that reads only the count produces. It matches
the review's 2025 estimate (17% in play, 18% foul). Target 2: 12,761 attempts on 599,654 pitches
(2.13%, the band centre 0.0214); target 3: 1,816 on 404,968 (0.45%, the centre 0.0044).

### 2.2 The two-out third strike and ball four

Attempts on a two-strike pitch of a strike class, by outs: 40 called and 105 swinging at no outs,
69 and 198 at one out — all 412 are `strikeout_double_play` rows, a strikeout and a caught
stealing. At two outs: none. The strikeout is the third out, so no stolen base or caught
stealing is ever recorded on that pitch. Attempts on a three-ball ball (ball four), any outs,
either target: none. A runner from first is awarded second on the walk, and no runner from
second was credited a steal of third on ball four in four seasons.

### 2.3 The foul class

Of the 153 attempts on a foul-class pitch, 62 rode a foul tip (a live ball; three of them a
caught stealing), 2 a foul bunt, and 89 a plain foul — a data-entry oddity at 0.6% of attempts.
The pool cannot tell a foul tip from a foul below two strikes, so a class-filtered draw stages a
steal on about 0.08% of foul pitches and resolves it as a steal. That is 1% of attempts, of
which two in five are right.

### 2.4 The class cells

| Class | Cells (2 targets × 36 counts) | Under 20 rows | Under 100 rows | Thinnest | Mean |
|---|---:|---:|---:|---:|---:|
| ball | 72 | 0 | 0 | 396 | 5,004 |
| called strike | 72 | 0 | 0 | 200 | 2,149 |
| foul | 72 | 0 | 5 | 42 | 2,676 |
| in play | 72 | 0 | 4 | 32 | 2,454 |
| swinging strike | 72 | 1 | 6 | 16 | 1,625 |
| hit by pitch | 71 | 20 | 66 | 1 | 45 |

The one thin swing-and-miss cell is target 3 at no outs and 3-0 (16 rows, no attempt). Every
thin cell below 20 rows but that one is a hit by pitch.

### 2.5 The runners on a dropped-third-strike reach

Of 200 reaches (a strikeout whose description names a wild pitch or a passed ball, the batter on
first after the play): a runner on second with first open had 21 chances and reached third or
scored on 19; a runner on third (bases not loaded) had 11 chances and scored on 9. Together 28 of
32. A forced runner on first with two outs (33 chances) went past second on 5: the reach's push
gives him second, and the extra base is not modelled here.

### 2.6 The third out before the pitch

Pickoff outs with two outs before the throw: 130 (2023), 126 (2024), 140 (2025), 121 (2026 to
date) — 0.053 a game, one in 19. The loop's defect needs the drawn pitch on that step to end the
plate appearance as well, about one plate appearance in 190 games (the review's estimate). A
caught stealing recorded as the plate appearance's own event at two outs: 902 at second and 47 at
third over the four seasons (0.10 a game) — those happen on a non-terminal pitch, where the loop
already leaves the batter's turn unused.

### 2.7 The steal bands on the last lanes

The 45 × 130 lane of 2026-09-21 (the first since the powers-to-1 ruling, the split, the fatigue
term and the manager draw) read steal attempts at second −9.1% and at third −17.9%, both red;
the lane of 2026-09-22 read the same. The certified lane of 2026-09-09 read +0.4% and −12.3%.
Those reds predate this ticket and are not its subject: the bands count attempts at the draw, so
they cannot see where an attempt lands.

### 2.8 The pickoffs in the pool (measured 2026-09-30)

Every pickoff outcome of `raw.play_events` against the pool row that carries it (read-only; the
script is the session scratchpad's `sim554/replay/pickoff_label_check.py`).

| Season | Pickoff outs | Errant throws | Fit a pair | Tagged pool rows: outs (of which caught stealing) | errors |
|---|---:|---:|---:|---:|---:|
| 2023 | 339 | 115 | 420 | 242 (109) | 87 |
| 2024 | 354 | 153 | 476 | 263 (111) | 105 |
| 2025 | 384 | 143 | 484 | 281 (123) | 108 |
| 2026 | 315 | 128 | 408 | 216 (84) | 92 |

Every game of the play records is a pool game (both end on 2026-08-29), so the two halves of
the table are like for like. Over the four seasons:

| Real pickoff outcomes that fit a pair | Count | Share |
|---|---:|---:|
| tagged to a pitch row | 1,394 | 78.0% |
| dropped: the plate appearance has no pitch of that pair | 394 | 22.0% |

- **The tagged rows are right.** The row's runner is the runner picked off on 1,386 of 1,394
  (99.4%); the row's outs equal the outs at the throw on 1,390 (99.7%).
- **The dropped ones are the throws before the first pitch.** On all 394 the plate appearance
  holds no pitch of that pair at all. By season, outs and errors: 71 + 20, 71 + 37, 70 + 25,
  74 + 26. About 95 a season.
- **Another 7% fit no pair** (143 of 1,931): a runner held at third or thrown out at home, and a
  throw to first with first and second both occupied. Left out by design since SIM-507.
- **So the pool holds about 72% of real pickoff outcomes.** The migration-0017 note says the
  tagging "preserves per-opportunity-pitch rates exactly". It does not.
- **The timing.** 1,358 of the 1,394 tagged rows (97.4%) are 0-0 pitches. Real pickoffs happen at
  any count. The count at the throw is not stored: `raw.play_events` carries the plate
  appearance, the outs and the bases, not balls and strikes, and `raw.pitches` carries no index
  that orders a pitch against a throw.

---

## 3. Findings that shape the plan

**Finding 1 — the defect is a conditioning gap, not a volume gap.** The attempt bands pass or
fail on how often the draw says "go". They count at the sampler seam, before the pitch resolves,
so a steal on a foul or a ball in play counts the same as one on a ball. The gap is in *where*
the attempts land, and only a read that puts the pitch class beside the attempt can see it.

**Finding 2 — the pool already carries the answer.** The builder joins each opportunity pitch to
the pitch pool, whose row carries the class. One more column, filled from that join, and the
steal draw's group can be (target, outs, balls, strikes, class). Summed over the loop's pitch mix
the class-conditional rates give the class-free rate exactly, so the attempt volume is unchanged
in expectation.

**Finding 3 — the order.** Every decision is a similarity-weighted draw from a hard-filtered
pool, and the drawn row is the play. Drawing the pitch first and filtering the steal pool by its
class makes both drawn rows consistent with one real pitch. The order is a sampling order, not a
claim that the batter acts first: each steal row is a real pitch on which the runner's choice
and the batter's reaction happened together. The replay measured the alternatives (§11).

**Finding 4 — two defects vanish by the pool's own rows.** With the class in the group, the
two-out third-strike groups and the ball-four groups hold no attempted row (§2.2), so no steal
is staged there. Balls in play and hit by pitches hold none either. Version 3 has no fallback to
a wider group, so a steal cannot be staged on any of them; the rebuild refuses a pool in which
an attempted row sits on a ball in play or a hit by pitch (today: none of 179,886).

**Finding 5 — the third out before the pitch is a pickoff.** The ticket says "a pickoff or
caught stealing that makes the third out before the pitch". A caught stealing is on the pitch,
never before it. On a non-terminal pitch the loop already handles it (the same batter leads off,
no credit). On a terminal pitch two cases exist: strike three held (the strikeout is the third
out; the caught stealing is void — decision 3) and ball four (the walk stands, the caught
stealing is the third out; real data hold no attempt on ball four, so the case is pinned by a
test and never drawn). For the pickoff, version 3 needs no rule: the pickoff is drawn and
resolved before the pitch, so a third out ends the half-inning before any pitch exists.

**Finding 6 — the got-away advance is the right mechanism for the reach.** It exists, it routes
its run through the ledger with the no-RBI marker, and it already runs on a got-away third strike
that cannot award first. Running it before the batter takes first moves every runner one base
(28 of 32 real chances) and leaves `_force_on_reach` nothing to push. The one-mover guard
stands.

**Finding 7 — the pickoff does not belong behind the class filter.** A pickoff comes before the
pitch, so the pitch's result says nothing about it. Version 2 kept the pickoff on the steal row
and argued the filter left its rate unbiased; it then needed a fallback and a cancel rule to
protect the pickoff rate in thin groups. Version 3 draws the pickoff from the count group, where
every group is large, and the steal from the class group, where a sparse group is the data's own
answer. Both rules go.

**Finding 8 — no byte identity.** The new order changes the order the loop consumes its random
streams, so every seeded game differs from today's. The regression lane has no golden files
(owner ruling 2026-09-10); the grade is the census, the smoke and the lane.

**Finding 9 — the steal attempt bands are red today for another reason.** §2.7 and §11.6: the
look-alike weights lower the number of steals (14% at today's strength). That is the balance
weight's ticket (SIM-556, filed 2026-10-01). This change moves placement, not volume.

**Finding 10 — the pool drops the pickoffs that come before a first pitch** (§2.8). 22% of the
outcomes that fit a pair. A tag on the pitch after the throw would put the pickoff in the wrong
situation on the wrong runner, so the builder is right not to tag one. The outcome needs a row
of its own: the throw, in the situation before it.

**Finding 11 — the pickoff's timing stays a first-pitch event.** The count at the throw is not
stored (§2.8), so tagged rows sit on the plate appearance's first pitch and pickoff rows are
written at 0-0. The number per plate appearance is right; the count they happen at is not.
Storing the count needs a column on `raw.play_events` and a re-read of every game's feed, outside
this ticket.

---

## 4. Data changes

**4.1 DuckDB migration 0031 — `db/migrations/duckdb/0031_sim554_steal_pool_pitch_class.sql`**
(schema v30 → v31; `db/schemas/duckdb_schema_version.txt` = 31; `db/schemas/02_duckdb_schema.sql`
updated in the same commit; the version test `tests/unit/test_docs_duckdb_version.py` reads the
prose in `docs/technical/pipeline-betting-db.md`).

```sql
-- 0031 — SIM-554: the class of the pitch each steal opportunity rode, and the pickoff rows
ALTER TABLE sim.steal_opportunity_pool ADD COLUMN IF NOT EXISTS pitch_class VARCHAR(20);
-- ball | called_strike | swinging_strike | foul | in_play | hit_by_pitch; NULL on a pickoff row
-- and on a row an old builder wrote.
ALTER TABLE sim.steal_opportunity_pool ADD COLUMN IF NOT EXISTS is_pickoff_row BOOLEAN DEFAULT FALSE;
-- TRUE = this row is a pickoff throw with an outcome that came before any pitch of its pair in
-- the plate appearance. It is not a pitch: pitch_id is the negative of raw.play_events.id.
```

**4.2 The builder** (`_build_steal_opportunity_pool`).

- *The class.* `pp.outcome_type AS pitch_class` joins the `opportunities` CTE (the join to
  `sim.pitch_pool` exists).
- *The pickoff rows.* One more CTE reads the pickoff outcomes of `_pickoff_outcomes_cte` whose
  (game, plate appearance, pair) holds no opportunity pitch, and writes one row each:

```sql
pickoff_rows AS (
    SELECT -e.id AS pitch_id, e.game_pk, e.at_bat_number, 0 AS pitch_number, e.game_date, e.season,
           e.runner_id, e.pitcher_id,
           (SELECT rp.fielder_2 FROM pg.raw.pitches rp          -- the catcher: the nearest pitch
             WHERE rp.game_pk = e.game_pk AND rp.inning = e.inning   -- of the same half-inning
               AND rp.inning_topbot = e.inning_topbot
             ORDER BY ABS(rp.at_bat_number - e.at_bat_number), rp.pitch_number LIMIT 1) AS catcher_id,
           e.target_base, e.inning, e.outs_before AS outs,
           0 AS count_balls, 0 AS count_strikes,                -- the count at the throw is not stored
           GREATEST(-5, LEAST(5, e.bat_score - e.fld_score)) AS score_diff,
           FALSE AS attempted, FALSE AS success, <recency>,
           e.is_out_i = 1, e.is_adv_i = 1, e.is_err_i = 1 AND e.is_out_i = 0,
           NULL AS pitch_class, TRUE AS is_pickoff_row
    FROM pickoff_events e                                        -- play_events rows with a pair
    WHERE e.runner_id IS NOT NULL AND e.pitcher_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM opportunities o
                      WHERE o.game_pk = e.game_pk AND o.at_bat_number = e.at_bat_number
                        AND o.target_base = e.target_base)
)
```

  An outcome that has a pitch of its pair keeps today's tag and makes no row; never both. The
  positional INSERT's SELECT appends the two new columns last, matching the DDL order.
- `POOL_BUILDER_VERSION = "sim554.1"`, with the comment line the others carry. Consequence: the
  next nightly rebuilds every pool for the current season; the window seasons are rebuilt by the
  run book's script before that.

**4.3 The export** (`build_steal_pool_artifact`): the meta parquet gains
`COALESCE(pitch_class, '') AS pitch_class`. A pickoff row exports with the empty class. A
pre-0031 DuckDB fails the SELECT, as the pickoff columns would; the migration precedes the
export in the run book.

**4.4 The loader and the resident pool** (`StealPool`): a new field `pitch_class: np.ndarray |
None` (int8; 1–6 in the order of `simulation.game_state.PITCH_OUTCOMES`; 0 = no class: a pickoff
row or an unknown), `None` on a bundle without it. The field joins
`_STEAL_POOL_SHAREABLE_ATTRS`, so the forkserver workers read one shared copy.

**4.5 The synthetic bundle** (`simulation/synthetic_bundle.steal_pools`): three rows per (outs,
balls, strikes, class): the attempt rate on ball, called strike and swinging strike; zero on
foul, in play and hit by pitch; a `class_rates` mapping overrides per class; a `pickoff_rows`
count adds pickoff rows per count group. The no-DB tests then run the new order.

**4.6 What the rebuild moves.** The pool gains about 394 pickoff rows for 2023–2026 (91, 108, 95
and 100 by season in the pool's games), so its pickoff outcomes rise from 1,394 to 1,788: from
about 0.15 to 0.19 a game. The caught-stealing reference of the smoke counts the pickoffs
scored as a caught stealing; the simulator's caught stealings move toward it. DuckDB's steal
pool also holds the 2026 games of 08-14 to 08-29 that the bundle does not; this export ships
that refresh. The band centres are the artifact pools' own recency-weighted rates; the run book
re-prints them after the export and updates `tests/acceptance/bands.py` if they move beyond the
fourth decimal.

No Postgres change. No profile change. No engine change.

---

## 5. Code changes, file by file

### 5.1 The order — `simulation/sim_loop.py`, `step_pitch`

```
today:  ① change? ② reliever? ③ intentional walk? ④ steal AND pickoff, one draw (count only) → ⑤ the pitch → its result → ...
plan:   ① change? ② reliever? ③ intentional walk? ④a pickoff draw (count) → ⑤ the pitch → its result → ④b steal draw (count + result) → ...
```

```python
self._pre_pitch_hook(state)                                  # the IBB draw only
if state.manager.intentional_walk_signalled:
    return self._issue_intentional_walk(state)
result = PlayResult(pitch_outcome=NO_PITCH)                  # filled in once a pitch is thrown
picked = self._pickoff_before_the_pitch(state, result)       # SIM-554: ④a — draw and resolve
if picked and state.is_half_inning_over():
    # The pickoff made the third out. No pitch is thrown: no pitch count, no event, no plate
    # appearance. The same batter leads off the next inning with a fresh count.
    result.pa_voided = "pickoff_third_out"
    self.advance_half_inning(state)
    result.next_state = state
    return result
state.pitcher_pitch_count += 1
pitch_outcome = ... (drawn or injected, as today; the draw now sees the bases the pickoff left)
if not picked:
    self._steal_opportunity_draw(state, pitch_class=pitch_outcome)   # ④b — one mover per pitch
adv = advance_count(state.balls, state.strikes, pitch_outcome)
result.pitch_outcome, result.is_contact, result.pa_terminal = pitch_outcome, adv.is_contact, adv.terminal
result.event = adv.event if adv.event != EVENT_IN_PROGRESS else None
```

The result object is made before the pitch so the pickoff's out and run value commit to it
through `_resolve_pickoff` and `_commit_run_delta`, as they do today. A steal a test staged with
`stage_steal` before the pitch is honoured as today (`_pending_steal is not None` → no draw). With
the flag off, or on a bundle whose steal pool has no classes, the loop runs today's single
pre-pitch draw, row for row.

### 5.2 The pickoff draw

```python
# simulation/full_pool_sampler.py
def pickoff_draw(self, target_base, runner_key, pitcher_key, catcher_key, *,
                 outs, balls, strikes, score_diff) -> tuple[bool, bool, bool] | None:
    """SIM-554: ONE row of the count group -> (out, advancing, error). Every row of the group
    is a candidate: the pitch rows and the pickoff rows. The weights are steal_weights' own
    (recency, the score-margin curve, the three look-alike weights), with no manager weight."""
    got = self.steal_weights(target_base, runner_key, pitcher_key, catcher_key,
                             outs=outs, balls=balls, strikes=strikes, score_diff=score_diff)
    ...one random number; read pool.pickoff_out / pickoff_advancing / pickoff_error of the row

# simulation/sim_loop.py
def _pickoff_before_the_pitch(self, state, result) -> bool:
    """Draw the pickoff for the lead stealable runner and resolve it now. True when a pickoff
    outcome happened (an out, or a throw that got away)."""
    ...the same runner / target / keys as _steal_opportunity_draw
    drawn = fp.pickoff_draw(...)
    if drawn is None or not (drawn[0] or drawn[2]):
        return False
    self._resolve_pickoff(state, result, StealResolution(attempted=True, runner_id=..., from_base=...,
                          to_base=..., safe=drawn[2] and not drawn[0], pickoff=True,
                          pickoff_advancing=drawn[1]))
    return True
```

`_resolve_pickoff` is unchanged: an out retires the runner (a caught stealing when he was
advancing), an errant throw moves him up, no steal credit either way.

### 5.3 The steal draw in the class group

```python
def _steal_meta(self, target):
    ...cells as today (the count group: every row)
    if pool.pitch_class is not None:
        has = pool.pitch_class > 0                               # a pickoff row has no class
        key2 = key[has] * 10 + pool.pitch_class[has]             # (outs, balls, strikes, class)
        meta["class_cells"] = {...}

def steal_draw(self, target_base, runner_key, pitcher_key, catcher_key, *, outs, balls, strikes,
               score_diff, aggression=1.0, pitch_class: str | None = None):
    if pitch_class is not None and self.steal_pitch_class and "class_cells" in meta:
        rows = meta["class_cells"].get((outs, balls, strikes, _PITCH_CLASS_CODE.get(pitch_class, 0)))
        if rows is None or len(rows) == 0:
            return None                                          # no real pitch like it: no steal
        ...weights as steal_weights computes them, on these rows; one random number
        return (attempted, success, False, False, False)         # the pickoff was drawn before
    ...today's draw (the flag off, or no classes in the bundle)
```

`steal_weights` gains an optional `rows` argument so the count group and the class group share
one weight path. A group answers from its own rows however few it holds. `steal_pitch_class`
(default True) is a sampler attribute; `production_factory` wires it from
`SIM_STEAL_PITCH_CLASS`.

### 5.4 The third out first — `step_pitch`, the terminal branch (decision 3, taken)

```python
pending = self._pending_steal
if (
    adv.event == EVENT_STRIKEOUT
    and int(state.outs) == OUTS_PER_INNING - 1
    and not self._last_pitch_got_away                          # the catcher holds it
    and pending is not None and pending.attempted
):
    # SIM-554: the strikeout is the third out before any throw; the steal is void.
    self._pending_steal = None
    result.steal_voided = "third_out_first"
    self.running_game_tally.voided["third_out_first"] += 1
```

In production the class group holds no attempted row there, so the rule never fires; a count
proves it. A caught stealing as the third out on ball four keeps today's path: the walk is
credited, the batting order advances, the half rolls (finding 5; pinned by a test). A got-away
third strike with two outs keeps the SIM-484 order.

### 5.5 The runners on the reach — `_resolve_strikeout` (decision 4, taken)

```python
d3k = self._dropped_third_strike(...)
if (
    self._last_pitch_got_away                                   # SIM-554: the reach branch included
    and not result.steal_attempted and not result.pickoff_out and not result.pickoff_error
):
    self._resolve_got_away_advance(state, result)               # every runner one base; a run from third, no RBI
pre_outs, pre_bases = ...
if d3k:
    forced_run = self._force_on_reach(state, result)            # first base is now open: nobody is pushed
```

The commit chain is the one the got-away strike three that cannot award first already uses. With
bases loaded and two outs the run now scores in the advance's commit instead of the reach's
push; the runners end on the same bases, the run still pays no RBI and stays earned.

### 5.6 The results — `simulation/game_state.py`, `PlayResult`

`NO_PITCH = "no_pitch"` (a result with no pitch thrown: a pickoff that ended the half-inning);
`pa_voided: str | None = None` ("pickoff_third_out"); `steal_voided: str | None = None`
("third_out_first"). **Every reader of a per-pitch result must skip a no-pitch result**: the
play recorder, the acceptance lane's counters, `scripts/sim_stats.py`, the play-by-play the API
serves. The build greps every reader of `pitch_outcome` and pins each with a test.

### 5.7 The tally — `simulation/sim_loop.py`

`RunningGameTally` (a small dataclass on the machine: pickoff outcomes, no-pitch third outs,
steals voided by reason, dropped-third-strike advances), read by the census. It changes no play.

### 5.8 The rebuild — `scripts/sim554_rebuild_steal_pool.py`

Modelled on the SIM-553 pitch-pool rebuild. Pre-flight with no write: the builder version, the
migration applied, the bundle's steal pool whole, a backup target, the window seasons. Open
DuckDB writable (a held lock stops the run: the app must be down, SIM-524). Snapshot every row's
`(pitch_id, attempted, success, pickoff_out, pickoff_advancing, pickoff_error)` for 2023–2026.
Rebuild the pool for those seasons in ONE transaction. Check inside it:

- every snapshot row is back with the same six values;
- the new rows are pickoff rows only, and per season they number the play records' outcomes
  that fit a pair and have no pitch of it (91 / 108 / 95 / 100 in the pool's games today);
- tagged rows plus pickoff rows equal the outcomes that fit a pair (1,788 today), less any with
  no runner or pitcher id, which the script lists;
- `pitch_class` is non-null on every pitch row and null on every pickoff row; the class shares
  per (target, outs, balls, strikes) equal the pitch pool's for the same pitches;
- no attempted row sits on a ball in play or a hit by pitch.

Commit, else roll back. Export ONLY `steal_pool/` into the bundle (the old directory kept as the
backup), read it back and prove the parquet rows and the manifest counts match DuckDB's. Print
the band centres the artifact pools give beside `bands.py`'s.

### 5.9 The census — `scripts/sim554_running_game_census.py`

On the balanced 45 games (`scripts/sim523_game_set.json`) × 20 iterations on the production
bundle, two arms on the same seeds: `SIM_STEAL_PITCH_CLASS=0` (today) and `=1`. Per arm:
opportunities and attempts by class, attempts per opportunity per target, safe share, stolen
bases, caught stealings per team-game, pickoff outcomes per game and by count, no-pitch third
outs, steals voided by reason, dropped-third-strike reaches and the runners they moved. The
expected reads on the new arm: attempts by class within a few points of the pool's shares (ball
64%, called strike 25%, swinging strike 10%, foul 1%, in play 0, hit by pitch 0); attempts per
opportunity within the census's noise of the old arm; pickoff outcomes about a quarter above
the old arm (the 22% that were dropped); the third-out-first count 0.

### 5.10 Documentation

The cheat sheet (`docs/technical/sim-loop-cheat-sheet.md`): the loop diagram (④ becomes ④a
before the pitch and ④b after it), a table for each of the two draws, step ⑧, the flag table.
`docs/technical/simulation.md`: the `step_pitch`, `_pre_pitch_hook`, `_steal_opportunity_draw`,
`steal_draw` and `pickoff_draw` rows, and the file note's worked example.
`docs/technical/pipeline-betting-db.md`: the two columns and the schema version. The header of
migration 0017 (its "rates exactly" sentence). `CLAUDE.md` §2b (a bullet) and §5 (v31).
`CHANGES.md`.

### 5.11 Built already (the replay, §11)

`FullPoolSampler.steal_weights` (the steal draw samples from it),
`scripts/sim554_running_game_replay.py`, `tests/unit/test_sim554_steal_weights_seam.py` (6),
`tests/unit/test_sim554_replay_instrument.py` (13), `scripts/sim554_replay_report.txt` / `.json`.

---

## 6. Weights, bandwidth, power

None change. The class is a hard filter on the steal draw; the pickoff draw uses the count
group today's draw uses. Inside each group the similarity weights (recency, the score-margin
curve at 2 runs, the three actor matrices at power 1) rank the rows as today; the manager's
weight applies to the steal draw's attempted rows only. One knob joins the flag table for the
sweep's record:

| Flag | Default | Meaning |
|---|---|---|
| `SIM_STEAL_PITCH_CLASS` | 1 | the new order: the pickoff draw before the pitch, the steal draw after it in the pitch's class group; 0 = today's single pre-pitch draw |

Version 2's `SIM_STEAL_MIN_CELL` is gone with the fallback. The loop rule of decision 3 and the
reach's advance of decision 4 are rules of baseball with no knob. The balance weight of the
steal draw (SIM-556) is its own ticket; it would act through `steal_weights`, which both draws
share.

---

## 7. Tests (`tests/unit/test_sim554_running_game_on_the_pitch.py` unless named)

The harness is the SIM-484 suite's: a synthetic bundle whose pitch model is one class, a
machine with `_got_away` on where the case needs it, `stage_steal` for a staged attempt.

**The order and the two draws**
1. The pickoff draw runs before the pitch draw, the steal draw after it with the drawn pitch's
   class (a duck-typed sampler records the call order and the keyword).
2. A pickoff out before the pitch: the pitch draw sees the bases without the runner; the pitch
   is thrown; one result carries the pickoff and the pitch.
3. A pickoff outcome on a pitch means no steal draw on that pitch.
4. A pickoff throw that gets away moves the runner up before the pitch; no steal credit.
5. A synthetic pool with attempts on balls only: a machine drawing only balls in play stages no
   steal over 500 pitches; drawing only balls stages at the pool's rate (±2 points).
6. A class group of three rows answers from its own rows; a class group with no row stages no
   steal; nothing falls back to the count group.
7. Pickoff rows are candidates of the pickoff draw and never of the steal draw.
8. With the flag off, or on a bundle whose steal pool has no classes, the loop runs today's
   single pre-pitch draw.
9. `tests/unit/test_sim474_steal_draw.py`: the existing wiring tests pass with the new keyword;
   one asserts its value.

**The pickoff's third out**
10. Two outs, a pickoff out: the pitch draw is not called, the pitch count is unchanged, no
    event, no at-bat, the batting-order slot and the batters-faced count unchanged, the result is
    a no-pitch result with `pa_voided == "pickoff_third_out"`, the half rolled, the same batter
    due.
11. The same in a full count: the batter's next pitch, an inning later, starts at 0-0.
12. Every reader of a per-pitch result skips a no-pitch result (one test per reader the build's
    grep finds: the recorder, the lane's counters, the smoke's totals, the served play-by-play).

**The third out first (decision 3)**
13. Two outs, a held called third strike, a staged safe steal: the strikeout is the third out,
    the runner's line has no stolen base, the half rolls.
14. Two outs, a held swinging third strike, a staged caught stealing: no caught stealing charged,
    the pitcher's strikeout credited, one out recorded.
15. One out, a held third strike, a staged caught stealing: a strikeout and a caught stealing,
    as today.
16. Two outs, a got-away swinging third strike with first open, a staged steal: the SIM-484
    order, unchanged.
17. Two outs, a staged caught stealing at third on ball four: the walk credited, the caught
    stealing the third out, the batting order advanced.

**The runners on the reach (decision 4)**
18. A runner on second, first open, one out, a swinging third strike that got away: the batter on
    first, the runner on third, no stolen base, the strikeout credited to both lines.
19. A runner on third, one out: the run scores, no RBI, the run earned, the batter on first.
20. Bases loaded, two outs: every runner up one base, one run, no RBI (the SIM-484 test
    re-asserted against the same bases and score).
21. After a steal on the same pitch the advance is skipped and the forced push remains.

**The data**
22. The builder's SELECT lists the two new columns last, in the DDL's order.
23. A pickoff outcome whose plate appearance has no pitch of its pair becomes one pickoff row:
    the runner picked off, the outs before the throw, the pair, 0-0, the thrower, the catcher of
    the nearest pitch of the half-inning. One that has such a pitch keeps today's tag and makes
    no row.
24. The export carries the class; the loader maps the six strings to codes 1–6 and a pickoff row
    to 0; the field is shareable across workers.
25. The synthetic pool builds rows per (count, class), honours `class_rates` and adds pickoff
    rows on request.

Twenty-five tests, plus the nineteen of the replay instrument (built) and the existing suites
the change touches: the SIM-474 draw, the SIM-484 credits, the SIM-507 pickoff channel, the
SIM-517 got-away consumers, the run-credit ledger.

---

## 8. Run book

1. **Code and gates.** The changes of §4–§5 on the branch. Grep every reader of `pitch_outcome`
   and of a per-pitch result, and give each the no-pitch case (§5.6). `ruff check`, `ruff format
   --check`, `mypy`; the unit lane (the container mounts `tests/` and `pyproject.toml`); the
   regression lane; the band-arithmetic lane.
2. **The migration.** `db/migrations/duckdb/0031_...sql` applied to the live DuckDB with the app
   stopped (`docker compose stop app`; the writer lock is SIM-524's). The version file 31.
   *Built differently (§13.2 item 5): step 3's script applies 0031 inside its transaction with
   `--apply-migration`; do not apply it as a separate step.*
3. **The rebuild and the export** (`scripts/sim554_rebuild_steal_pool.py`, the app still down):
   the four window seasons, one transaction, the checks of §5.8, the export of `steal_pool/`
   alone, the round-trip check, the band centres printed. Minutes, not hours; the script prints
   the time. A failed check rolls back and restores the backup.
4. **The app up.** `docker compose up -d app`; the boot logs `build_all_engines: 11/11` and the
   calibration applied.
5. **The census** (`scripts/sim554_running_game_census.py`, both arms, 45 × 20): the reads of
   §5.9. A misplaced share above 5% on the new arm, a pickoff rate that is not 15% to 40% above
   the old arm's (the pool's own ratio is 1,788 / 1,394 = 1.28), or an attempt volume moved by
   more than the census's noise stops here for a diagnosis.
6. **The ten-game smoke** (`scripts/sim_stats.py`, 10 × 50): runs, hits, strikeouts, walks,
   stolen bases and caught stealings against the MLB references; no collapse.
7. **The lane** (45 × 130, `tests/acceptance/test_production_config_bands_sim450.py`, 1 h 32 m):
   the sixteen pool bands read against the last lane's; the steal attempt bands are expected
   where the last lane left them (§2.7), the safe share within its band. The record goes to
   `scripts/sim554_lane.txt`.
8. **Close.** `CHANGES.md`; the row deleted from `BACKLOG.xlsx`; the docs of §5.10; the plan's
   build record; commit on the branch; merge before the next nightly pool build (the builder
   version).

---

## 9. What could go wrong (ranked)

1. **A reader counts a no-pitch result as a pitch.** A pickoff that ends the half-inning now
   returns a result with no pitch in it, about once in 19 games. A reader that assumes a pitch
   (a pitch count, a play-by-play row, a pitches-per-plate-appearance counter) would be off by
   one there. The build greps every reader and pins each (§5.6, test 12).
2. **The loop's class mix moves the attempt volume.** The class-conditional rates reproduce
   today's volume only if the loop throws balls, strikes and fouls in the pool's proportions per
   count. The walk band read +4.0% on the last lane, so attempts may rise by a point or two. The
   census reads it; the lane grades it.
3. **The pickoff's timing.** Tagged rows and pickoff rows sit at the plate appearance's first
   pitch (finding 11), so the simulator's pickoffs happen at 0-0. The number is right. A pickoff
   at 0-0 instead of 2-1 changes little, but it is not what happened.
4. **The look-alike weights lower the pickoff rate as they lower the steal rate.** The pickoff
   draw uses the same weights, and they take mass from rare kinds of player (SIM-556). The census
   reads the pickoff rate; the fix is that ticket's.
5. **The foul-class attempts.** About 1% of attempts resolve on a foul-class pitch; three in
   five are a plain foul the data recorded oddly (§2.3). A fix needs the foul tip told apart
   below two strikes, a pool coding change.
6. **The reach's advance overshoots.** Four of 32 real unforced runners held. Seven plays a
   season.
7. **The nightly rebuilds the current season before the merge.** The builder version bump marks
   every pool stale for the current season; a nightly from code without the columns writes the
   old shape. Merge before the next nightly pool build.
8. **A plate appearance with no pitch at all.** A pickoff for the third out before the first
   pitch leaves the plate appearance with no pitch row. The pickoff row's catcher then comes from
   the nearest pitch of the same half-inning; with none, the catcher is unknown and his weight
   neutral.

---

## 10. Decisions for the owner

1. **The order and the filter for the steal.** *TAKEN 2026-09-30 by the replay (§11), as
   recommended:* the loop draws the pitch first and the steal draw hard-filters on the drawn
   pitch's class, from a new column on the steal opportunity pool. *Alternatives, each scored by
   the replay:* one row for the pitch, its result and the steal; the steal on the pitch row; the
   steal on the result row; the runner deciding first. None beats it, and every design that puts
   the steal on a pitch row is worse than today's draw.
2. **The pickoff.** *TAKEN 2026-10-01 by the owner ("yes, write this in as version 3"), as
   recommended:* the pickoff is its own draw before the pitch, from the count group, and the
   pickoff outcomes the pool drops (22%, §2.8) come back as pickoff rows in the situation before
   the throw. This replaces version 2's decision on the thin class cell: the steal draw answers
   from its own group however few rows it holds, with no fallback and no cancel rule.
   *Alternatives:* keep the pickoff on the steal row, with the fallback under 20 rows and a rule
   that cancels a steal the fallback stages on a dead ball (version 2), and the dropped pickoffs
   stay dropped; or tag the pitch after the throw, which puts the pickoff in the wrong situation
   on the wrong runner.
3. **The two-out held third strike.** *TAKEN 2026-10-01 by the owner ("approve decisions 3 and
   4 as recommended"):* the loop resolves the strikeout first and
   voids a staged steal, always, as a rule of baseball (§5.4); in production the class group
   holds no attempted row there, so the rule never fires and its count proves it. *Alternative:*
   rely on the pool's rows alone, no rule; a test that stages a steal there would then record a
   stolen base the rules do not.
4. **The runners on the dropped-third-strike reach.** *TAKEN 2026-10-01 by the owner, as
   recommended:* the got-away advance runs
   first — every runner one base, a run from third with no RBI — then the batter takes first
   (§5.5); 28 of 32 real unforced runners moved. *Alternatives:* leave the unforced runners where
   they are (today; 4 of 32 real); or read the drawn row's own runner destinations, which needs
   the post-play bases on the pitch pool and a rebuild — out of this ticket's scope.

**Recorded, not asked.** The rebuild covers the four window seasons only; the 2017–2022 steal
pool stays on the old builder until the pool-window test. The export ships the steal pool's 2026
refresh (§4.6). No accuracy arm runs (the ruling of 2026-09-16); the grade is the census, the
smoke and one lane. The steal attempt bands are red on the last lane because the look-alike
weights lower the number of steals (finding 9; SIM-556), so "the steal bands stay green" in the
ticket's definition of done reads here as "this change does not move them". The ticket's "a
pickoff or caught stealing that makes the third out before the pitch" is a pickoff (finding 5);
the caught-stealing cases are pinned by tests 14 and 17. The pickoffs that fit no pair (7%: a
runner at third, a throw to first with first and second occupied) stay out, as since SIM-507.
The count at a pickoff throw is not stored, so pickoffs stay first-pitch events (finding 11).
The replay scored the steal with version 2's fallback under 20 rows; version 3 drops the
fallback, which touches at most 400 of a million rows, and the replay was not re-run. The
replay did not score pickoffs. The rng order changes, so no seeded game is byte-identical to
today's (finding 8).

---

## 11. The replay: the design picked by a test on real pitches (2026-09-30)

### 11.1 Why it ran

Version 1 recommended the pitch first, then a steal draw that reads the pitch's class. The owner
put three questions to it. In a game the runner decides first and the batter reacts, so should
the steal draw come first and condition the batter's result? Can the pitch and the steal be one
draw, one real pitch answering everything? And if the steal rides a swing and miss in the drawn
row, was that batter not a worse contact hitter than ours? The options were: three separate
draws; two draws grouped either way (the pitch with the steal, or the batter's result with the
steal); one draw for everything. The owner's instruction: "build the replay test and let it pick
the design".

One fact frames all of it (§2 and the read of 2026-09-30): the records show a steal only when
the runner went AND the batter did not hit the ball. A runner going on a ball in play or a foul
leaves no record. So a recorded steal is never the runner's decision alone.

### 11.2 The instrument

`scripts/sim554_running_game_replay.py`, modelled on the offline replay of the weight fitting
(`scripts/sim548_offline_fit.py`). It plays no games. For each real pitch on which a steal was
possible it sets the point-in-time cutoff to the day before the pitch's game, builds the
half-inning base and the plate-appearance weight as the loop does, and reads the WHOLE jar of
candidate rows with their weights as a probability table over (the pitch's result) × (no steal /
stolen base / caught stealing). Then it scores what happened.

- **Fidelity.** It drives a `FullPoolSampler` built by the production factory and reads what the
  sampler assembled: the pitch step's rows and weights, `result_weights` for the result step,
  `_matrix_gather` for every look-alike weight, and a new seam, `FullPoolSampler.steal_weights`,
  which `steal_draw` now samples from (one code path; it consumes no random number). It
  re-implements no weight.
- **The steal facts on the pitch rows.** Joined in memory by pitch id: the bundle's own pitch-pool
  file carries the id, and `sim.steal_opportunity_pool` is read read-only. Every
  opportunity-shaped pitch row found its steal row (442,133 + 562,489 rows; 14,577 attempts, the
  database's own count). No table is rebuilt; the app stays up.
- **The pitches.** Every pitcher-game with an opportunity pitch. 2025 picks each design's
  strength: 15,823 pitcher-games, 89,881 pitches scored, 3,835 recorded steals. 2026 gives the
  score: 12,944 pitcher-games, 74,296 pitches, 3,083 recorded steals. Every steal pitch is
  scored and one in three of the others, which then weigh three.
- **The settings.** Four strengths of the runner / pitcher-hold / catcher look-alike weights:
  off, 1 / 1 / 1 (production today), 4 / 4 / 2, 12 / 12 / 2 (the earlier fitted values). Three
  base settings: neutral (production: every pitcher and batter power 1), the earlier fitted
  two-step powers (16 / 16 / 8), and a single-draw setting (16 / 8).
- **Cost.** About 17 ms a pitch per base setting: 82 minutes for both seasons in two containers.
- **Tests.** `tests/unit/test_sim554_steal_weights_seam.py` (6) and
  `tests/unit/test_sim554_replay_instrument.py` (13): the seam, the table arithmetic, the scores,
  and the report end to end on planted data.

### 11.3 The eleven designs

On the two-step pitch draw production runs (the pitch thrown, then its result):

| Design | What it does |
|---|---|
| `today` | three separate draws; the steal draw reads only the count (production) |
| `steal_reads_class` | three separate draws; the steal draw reads the count and the class of the pitch that came out (this plan) |
| `steal_first` | the runner decides first from the steal pool; when he goes, the drawn steal row's own pitch result is the result; when he stays, the batter's result comes from the non-running pitch rows |
| `pitch_steal` | the pitch step's row carries the steal; the batter's result is then drawn among real running pitches, or among the others |
| `pitch_steal_no_contact` | the same, but when the runner goes the batter's result is only barred from contact |
| `result_steal` | the pitch step alone; then one row gives the batter's result and the steal, every look-alike weight on it |
| `result_steal_2step` | the same jar read in two steps: the look-alike weights decide only whether a steal is recorded; the row then comes from the rows of that kind under the ordinary weights |

On the single pitch draw (one step for the pitch and its result), with its own baselines:
`single_today`, `single_reads_class`, `one_draw` (one row for the pitch, the result and the
steal), `one_draw_2step`.

### 11.4 The rule

The score that decides is the Brier score of the whole table: the squared distance between the
odds a design gave to (result × steal) and what happened. It is bounded, so a design that says
"impossible" for something that then happens pays a fixed price.

1. Designs are compared inside the family of their pitch draw, against that family's baseline.
   The question is the running game; which pitch draw to run is the weight-fitting ticket's.
2. Production runs the two-step pitch draw, so that family's winner at today's weights is the
   pick. Two standard errors or less is a tie (errors from resampling whole pitcher-games).
3. A tie goes to the design whose per-runner expected steals track the real ones best (two
   standard errors or less is again a tie), then to the simpler build.
4. Each design's strength is its best on 2025.

**How the rule was set, plainly.** Two trial runs on about 2,000 pitches a season (110 steals)
came before the full run. They showed the log score could not decide: the merged designs give
about 7% of real steals no chance at all, so their log score is whatever floor one picks. They
also showed that a score "given the real result" punishes a design whose steal odds and result
odds come from two pools, in a way its simulated games would not feel. So the bounded
whole-table score decides, and the log score, the parts of the table and the per-player reads
are printed beside it. One clause was completed after the full run: the first script stopped the
tie-break at the raw correlation and printed `steal_first`, on a gap of 0.0017 ± 0.0014. The rule
as written ends on the simpler build; the script now applies it and records both ties.

### 11.5 The result

**Today's weights, the two-step pitch draw (3,083 steals; 1.423 per 100 pitches).** Differences
are against `today`, in 1/10,000 of the score; lower is better.

| Design | Strength | Whole table | Pitch result | How many steals | Steal given the result | Per-runner correlation | Mix error | Said impossible |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `steal_reads_class` | 4/4/2 | **−2.06 ± 0.35** | 0 | +0.07 ± 0.06 | −7.06 ± 0.41 | 0.843 | 1.8 pts | 0% |
| `steal_first` | 4/4/2 | −1.15 ± 0.57 | −1.50 ± 0.53 | 0 | +12.77 ± 2.46 | 0.845 | 1.9 pts | 0% |
| `today` | 4/4/2 | 0 | 0 | 0 | 0 | 0.845 | 37.4 pts | 0% |
| `pitch_steal_no_contact` | off | +1.37 ± 0.30 | +1.11 ± 0.36 | +8.72 ± 0.37 | +6.49 ± 0.50 | −0.123 | 8.6 pts | 7.2% |
| `pitch_steal` | off | +2.18 ± 0.60 | +0.05 ± 0.15 | +8.72 ± 0.34 | +12.83 ± 0.98 | −0.123 | 1.1 pts | 7.2% |
| `result_steal` | off | +2.41 ± 0.64 | 0 | +9.21 ± 0.38 | +13.37 ± 0.98 | −0.131 | 0.7 pts | 7.2% |
| `result_steal_2step` | off | +2.41 ± 0.64 | 0 | +9.21 ± 0.39 | +13.37 ± 0.96 | −0.131 | 0.7 pts | 7.2% |

"Mix error" is the distance between the results real steals ride (ball 64.6%, called strike
25.0%, swing and miss 9.3%, foul 1.0%) and the design's. "Per-runner correlation" is over the
417 runners with at least 150 opportunity pitches: each runner's expected steals against his
real ones, the quantity the stolen-base prop pays on.

The two leaders head to head: `steal_first` minus `steal_reads_class` on the whole table
+0.91 ± 0.50 (1.8 standard errors, a tie); the correlation gap 0.0017 ± 0.0014 (a tie); the
simpler build is `steal_reads_class`. **The pick: `steal_reads_class`.**

**The single pitch draw, the same read.** `single_reads_class` −1.78 ± 0.35 against its baseline;
`one_draw` and `one_draw_2step` +2.48 ± 0.62, with the same −0.123 correlation and 7.2% of
steals given no chance. Merging loses in that family too.

**Why the merged designs lose: the runner.** The same four strengths, the separate steal pool
against the pitch row (`today` against `pitch_steal`; real 1.423 steals per 100 pitches):

| Strength | Steal pool: per 100 | correlation | how-many score | Pitch row: per 100 | correlation | how-many score |
|---|---:|---:|---:|---:|---:|---:|
| off | 1.524 | −0.20 | 278.9 | 1.446 | −0.12 | 282.4 |
| 1 / 1 / 1 | 1.225 | 0.71 | 276.5 | 1.180 | 0.60 | 281.5 |
| 4 / 4 / 2 | 1.137 | 0.85 | 273.7 | 1.101 | 0.79 | 289.9 |
| 12 / 12 / 2 | 1.338 | 0.93 | 298.1 | 1.178 | 0.80 | 337.8 |

On the pitch row the runner weight does tell runners apart (0.79), but the odds get noisier
faster than they get sharper: every strength scores worse than none. In the steal pool the score
improves up to 4 / 4 / 2. The pitch draw's bucket (base state, outs, score band, side, count)
holds about five steal rows; the steal pool's cell holds hundreds.

**The look-alike weights inside the pitch draw.** The unprotected merged designs put the runner,
pitcher-hold and catcher weights on every row of the pitch's jar. The pitch result's own score
for `result_steal`: 0.7599 (off), 0.7676, 0.8024, 0.9361 at the four strengths. The two-step read
removes that damage (0.7599, 0.7600, 0.7603, 0.7620) and keeps the steal noise.

**The earlier fitted pitcher and batter weights (a sensitivity).** `steal_reads_class` −0.66 ± 0.43
(a tie with `today` on the whole table; at those weights the pitch-result odds dominate the
score), the mix error still 36.5 → 1.1 points and the steal given the result −4.58 ± 0.27.
`steal_first` +5.92 ± 1.54; the merged designs +16 to +42.

### 11.6 Findings

1. **Merging the steal into the pitch row loses who steals.** Sparse situations are not the
   problem: a situation with few steals is the data saying runners seldom go. The cost is the
   player. A handful of steal rows cannot say how often runners like this one went.
2. **Every design that ties the steal to the pitch fixes what the steal rides** (37 → 1–2 points
   of mix error). The merged designs are a point better on that read, and lose more elsewhere.
3. **The runner-first design ties on the deciding score and is worse on three reads.** Its steal
   odds and its result odds come from two pools, so given the real result its odds are off
   (+12.77 ± 2.46); the batter has no say in what the steal rides (finding 4); at the fitted
   weights it is worse than today. It also needs the pitch draw to obey the drawn steal row.
4. **The batter's say.** Real steals ride a swing and miss 6.7% of the time with a contact hitter
   up and 10.7% with a swing-and-miss hitter. At today's neutral weights no design reproduces the
   slope (the batter weight is nearly flat). With the batter weight on, `steal_reads_class` does
   (8.3 / 9.8 / 11.7%), because the pitch's result comes from the batter-weighted pitch draw;
   `steal_first` cannot (10.0 / 10.1 / 10.1%).
5. **Outside this ticket: the look-alike weights cut the number of steals.** With no weight the
   steal pool predicts 1.52 per 100; at today's strength 1.23, 14% under the real 1.42; at
   4 / 4 / 2, 1.14. Every design shows the same drop, so it is the weights, not the structure. It
   matches the lane's red steal-attempt bands (−9.1% and −17.9%, §2.7). It belongs to the weight
   fitting (SIM-548) or a ticket of its own; not filed here.
6. **Outside this ticket: the single pitch draw scores better on the pitch's result at neutral
   weights** (0.7585 against 0.7599), as the offline fit read on 2026-09-16. Part of the gap is
   the eight-anchor estimate the replay uses for the two-step draw.

### 11.7 What this changes in the plan

Nothing in §4 to §9: the picked design is version 1's. Decision 1 is taken; decisions 2 to 4
stay open. Built on the branch, uncommitted: the seam (`simulation/full_pool_sampler.py`,
`steal_weights`), the instrument, the two test files, and the records
`scripts/sim554_replay_report.txt` and `.json`.

### 11.8 Limits

The player profiles are the season's, not point-in-time (the same leak for every design). The
batting manager's steal weight is neutral. Pickoff labels are not scored. The two-step pitch
draw is estimated with eight anchors a pitch. The four strengths are a coarse grid: the replay
picks the structure, not the weights. No test can see a runner moving on a ball in play.

---

## 12. Version 3: the pickoff as its own draw (2026-10-01)

### 12.1 How it came up

Explaining the thin-cell recommendation of version 2, the reason given for the fallback was the
pickoff: the steal row also carries a pickoff tag, and a one-row group would repeat its tag
every time. The owner asked how pickoffs enter the draw, then put the case that breaks a tag on
the pitch after the throw: runners on first and second, nobody out, the runner on second picked
off. The next pitch is thrown with a runner on first and one out. A tag there would fire in the
wrong situation and pick off the wrong runner.

### 12.2 What the check found (§2.8)

The builder does not tag the pitch after the throw. It tags the first pitch of the plate
appearance thrown in the situation before the throw, and those tags are right (the runner
matches on 99.4%, the outs on 99.7%). But when the throw comes before the plate appearance's
first pitch, no such pitch exists, and the builder drops the outcome: 394 of 1,788 (22%), about
95 a season. With the 7% that fit no pair, the pool holds about 72% of real pickoff outcomes.
Version 2's text, and the answer first given to the owner, said the tagged share equals the real
rate. That repeated the builder's note without checking it.

### 12.3 The decision

The owner, 2026-10-01: "Yes, write this in as version 3." The pickoff becomes its own draw before
the pitch; the dropped outcomes come back as pickoff rows; the steal draw keeps the class group
and loses the fallback and the cancel rule.

### 12.4 What changed from version 2

- **§1.3** tells the tagging as it is. **§2.8** is new. **Findings 4, 5 and 7** are rewritten;
  **10 and 11** are new.
- **§4:** migration 0031 gains `is_pickoff_row`; the builder writes the pickoff rows; §4.6 states
  what the rebuild moves (pickoff outcomes 0.15 → 0.19 a game).
- **§5:** the order is pickoff, pitch, result, steal (§5.1); `pickoff_draw` and
  `_pickoff_before_the_pitch` are new (§5.2); the steal draw has no fallback (§5.3); the dead-ball
  rule is gone; the pickoff's third out is the order itself, with a no-pitch result, in place of
  version 2's reversal of a pitch already drawn (§5.6).
- **§6:** `SIM_STEAL_MIN_CELL` is gone.
- **§7:** twenty-five tests (version 2: twenty-four); the fallback and dead-ball tests are
  replaced by the two draws' and the no-pitch tests.
- **§8, §9:** the rebuild's checks count the pickoff rows; the first risk is a reader that
  counts a no-pitch result as a pitch.
- **§10:** decision 2 is the pickoff draw, taken; the thin-cell decision is withdrawn. Two
  decisions stay open.
- §11 (the replay) is unchanged. Its §11.7 says three decisions are open; two are, now.

### 12.5 Limits

No replay scored the pickoff: the 22% is a count of records, and the draw's rate will be read by
the census. The count at the throw is not stored, so the timing stays at the first pitch. The
outcomes that fit no pair stay out. A throw with no outcome and a step-off are still not drawn:
they change no base, out or count. Drawing "throw over, step off or pitch" as events, with the
throw limit of 2023 as state, remains the larger follow-on, not filed.

### 12.6 Decisions 3 and 4 (taken 2026-10-01)

The owner, 2026-10-01: "Approve decisions 3 and 4 as recommended."

- **Decision 3, the two-out held third strike.** With two outs, a third strike the catcher holds
  is the third out before any throw. The loop resolves the strikeout first and voids a steal
  staged on that pitch: no stolen base, no caught stealing. The rule is in the loop always
  (§5.4). In production the class group holds no attempted row there, so the rule is not
  expected to fire; the census prints its count, and a count above zero is a defect to diagnose.
- **Decision 4, the runners on a dropped third strike.** When the batter reaches on a dropped
  third strike, the got-away advance runs first: every runner moves up one base, a runner on
  third scores with no RBI. Then the batter takes first (§5.5). 28 of 32 real unforced runners
  moved. A steal or a pickoff on the same pitch skips the advance (one mover per pitch).

All four decisions are taken. §12.4's "two decisions stay open" and §11.7's "decisions 2 to 4
stay open" are the record of their dates. Nothing in §4 to §9 changes: the text already describes
both rules. The design is ready to build; nothing is built by this record.


---

## 13. Build record (2026-10-01)

### 13.1 What is built

On the branch `sim554-running-game-impl` (from master 3a1d4c9; the first commit holds this
plan, the replay and its seam).

- **The loop** (`simulation/sim_loop.py`). The order of §5.1 behind `_steal_order_active()`:
  the pickoff draw before the pitch (`_pickoff_before_the_pitch`), the pitch, the steal draw
  after it with the pitch's class (`_steal_opportunity_draw(state, pitch_class=...)`). A pickoff
  third out returns a no-pitch result (`NO_PITCH`, `pa_voided = "pickoff_third_out"`). Decision 3
  sits before `_resolve_steal_outcome` on the terminal branch (`steal_voided =
  "third_out_first"`); decision 4 runs the got-away advance on the reach branch of
  `_resolve_strikeout`. `RunningGameTally` counts the paths. `simulate_game` leaves a no-pitch
  step out of `total_pitches`.
- **The sampler** (`simulation/full_pool_sampler.py`). `_steal_meta` builds three indexes
  (§5.3); `pickoff_draw`; `steal_draw(..., pitch_class=)`; `steal_weights(rows=)`;
  `has_steal_classes()`; the attribute `steal_pitch_class`, which
  `production_factory.apply_running_game_env` sets from `SIM_STEAL_PITCH_CLASS` (default 1).
- **The data.** Migration 0031 (schema v31); builder `sim554.1` with the pickoff rows and a
  guard that refuses an un-migrated table before its DELETE; the export of `pitch_id`,
  `pitch_class` and `is_pickoff_row`; `StealPool.pitch_class` and `StealPool.is_pickoff_row`.
- **The synthetic pool** (`simulation/synthetic_bundle.steal_pools`): rows per (count, class),
  `class_rates`, pickoff rows on request.
- **The readers of a pitch result.** The play-by-play and the per-pitch snapshots skip a
  no-pitch result (`simulation/snapshots.thrown_pitches`, `api/routes/games._record_and_build`);
  the linescore and the pitcher decisions read the whole stream; the game's pitch total, the
  trace script, the smoke's flag list and the acceptance lane's probes and flag table.
- **The run-book scripts.** `scripts/sim554_rebuild_steal_pool.py` (§5.8) and
  `scripts/sim554_running_game_census.py` (§5.9), with unit tests on planted data. Neither has
  run against the live stack.
- **The docs** of §5.10, the schema-version prose (v31) and a §2b bullet in `CLAUDE.md`.
- **Tests.** The twenty-five of §7 and more, in four new files:
  `test_sim554_running_game_on_the_pitch.py`, `test_sim554_steal_pool_data.py`,
  `test_sim554_no_pitch_readers.py`, `test_sim554_run_book_scripts.py`; plus additions to the
  SIM-474, SIM-421, play-recorder, band-arithmetic and accuracy-comparison suites. One older
  test changed its expected values by design: a dropped third strike with first base open now
  moves the runners on second and third up a base (decision 4).

### 13.2 Where the build departs from this plan, and why

1. **The flag-off draw reads pitch rows only.** On a classed bundle the single pre-pitch draw
   leaves the pickoff rows out, so `SIM_STEAL_PITCH_CLASS=0` reproduces the draw before
   migration 0031 and the census's two arms compare the recovered pickoffs.
2. **The pickoff rows carry their own mark through to the sampler.** §4.3-§4.4 planned the class
   alone (code 0 = a pickoff row). A pitch row an old builder wrote also has no class, so the
   export carries `is_pickoff_row` and the loader reads it. A pool where any pitch row has no
   class keeps the single draw over every pitch row and logs it; the new order runs only when
   every target is fully classed (review finding, round 1).
3. **The pickoff third out credits the pitcher with the out.** §0 said the pickoff third out
   credits no pitch event. It still credits no pitch, no event and no plate appearance, but the
   out counts toward the pitcher's outs (innings pitched, the pitcher-outs prop), as the official
   box counts it. Without it the new path would have lost outs the old path credited (review
   finding, round 1). The older gap, a caught stealing or pickoff out on a pitch that does not
   end the plate appearance, is filed as SIM-557.
4. **The export is staged and ordered.** Both files are written in `pitch_id` order (before, two
   unsorted scans happened to agree), under temporary names, and moved into place only when
   both targets succeed; a table without migration 0031 is refused before any write. The loader
   refuses a target whose two files disagree on the row count (review finding, round 1).
5. **The rebuild applies migration 0031 inside its transaction** (`--apply-migration`), so a
   failed rebuild rolls the two columns back with the rows. §8 steps 2 and 3 become one command;
   do not apply 0031 separately (review finding, round 1).
6. **The rebuild's re-run check compares pitch rows only.** A pickoff row's id is the play
   record's database id, which a game reload changes; check c proves every pickoff row of the
   build against the play records instead (review finding, round 1).
7. **The pickoff draw skips a count group with no pickoff outcome** and uses no random number
   there.
8. **The census reads expected rates, not only counts.** At 45 × 20 the pickoff counts cannot
   resolve the §8 step-5 window (about ±0.15 on 1.28). At every draw the census adds the
   weighted share of pickoff rows from the rng-free `steal_weights`, which gives a near-noiseless
   ratio of the two arms.
9. **Guards beyond the plan.** The acceptance lane fails when `SIM_STEAL_PITCH_CLASS` asks for
   the new order and the bundle cannot run it (the fence stage ran inert in two weeks of lanes
   in September). The accuracy comparison's provenance stamps the steal pool and the running
   game's order, so reports from before and after the steal-pool export do not pair. An
   injected pitch outcome is checked before any hook or draw.
10. **The play-by-play closes an at-bat on any pitch that ends the half.** A caught stealing
    that made the third out on a pitch that did not end the plate appearance merged the next
    half's leadoff pitches into the previous at-bat; the fix covers the no-pitch case and this
    older one in the same rule (review finding, round 2).

### 13.3 The reviews

Round 1: six reviewers (the loop, the sampler, the data layer, the readers, the scripts, the
docs against this plan), two skeptics per finding. Seven defects confirmed and fixed (13.2 items
2-6, 9 and the "nothing is credited" wording); three findings refuted (the class draw's weights
at a power other than 1, the outs of a pickoff row, the missing changelog entry before the
close). Round 2: the seven fixes, the production paths (shared memory, the workers, speed) and
the strength of the tests by mutation. Eleven smaller findings confirmed and fixed: two in the
rebuild script, the run-book wording, the provenance stamp, the play-by-play at-bat break, and
six tests that could not fail.

### 13.4 The run book (2026-10-03)

Master had moved four commits (the odds work, SIM-555) and was merged into the branch first; the
unit lanes on the merged code read 5,354 passed, 0 failed.

1. **The rebuild** (`scripts/sim554_rebuild_steal_pool.py --apply-migration`, the app stopped,
   1.1 minutes). The read-only pre-flight passed every check but the migration. The run applied
   0031 inside its transaction, rebuilt 2023-2026 and passed checks a-h: 1,004,622 snapshot pitch
   rows back unchanged; 394 new rows, all pickoff rows (91 / 108 / 95 / 100, the counts of §4.6);
   tagged rows + pickoff rows = the 1,788 outcomes that fit a pair; every pitch row classed and
   equal to its pitch-pool class; no attempted row on a ball in play or a hit by pitch. COMMIT;
   the bundle's old steal pool copied to `engine_artifacts.pre_sim554_steal_pool`; the export
   round-tripped row for row and left the rest of the bundle byte-identical. The steal pool now
   holds the 2026 games of 08-14 to 08-29 too, so the safe share at second moved at the fourth
   decimal (0.7989 -> 0.7982, `tests/acceptance/bands.py` updated); the attempt centres held.
   Pickoff outcomes per pitch row x1.271.
2. **The census** (45 x 20 a side, the app stopped for memory; `scripts/sim554_census_report.txt`,
   the two JSON files beside it). Every stop rule passed. The new order's attempts on a foul, a
   ball in play or a hit by pitch: 0.85% (the old order 38.8%; real 1.0%); the class mix ball /
   called strike / swinging strike 0.630 / 0.251 / 0.110 (the pool's 0.645 / 0.246 / 0.099).
   Expected pickoffs per draw x1.242 (the pool's x1.28). Attempts per opportunity within noise
   (target 2: 0.0191 -> 0.0195; target 3: 0.0032 -> 0.0036). 41 no-pitch third outs, every one with
   the half rolled and no plate appearance credited. Steals voided as the third out first: 0 (the
   old order staged 33 such steals in 900 games, which decision 3 voided).
3. **The ten-game smoke** (10 x 50, `scripts/sim554_smoke.txt`): no collapse; runs +2.5%, hits
   +1.9%, home runs +3.1%, strikeouts +2.5%, walks +3.0% against MLB 2023; stolen bases -8.8%.
4. **The lane** (45 x 130, 1 h 29 m, `scripts/sim554_lane.txt`): 36 passed, 6 failed, the six
   channels red before this change at like sizes (walks +3.4%, hit by pitch +8.5%, doubles +5.0%,
   steal attempts -10.0% at second and -21.3% at third, the home-win share underpowered). The
   steal-attempt bands sit where the last lane left them, as §8 step 7 expected; their deficit is
   the look-alike weights' (SIM-556). Strikeouts -0.9%, the safe share at second -0.1%, runs 4.47
   a team-game.
5. **The close.** The branch merged into master; the app restarted on the merged code; the
   SIM-554 row deleted from `BACKLOG.xlsx`; SIM-557 (the pitcher's out on a caught stealing or
   pickoff that does not end the plate appearance) filed, P2; next free ID SIM-558.
