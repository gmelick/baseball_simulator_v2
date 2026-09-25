# Design — the two-strike foul tip and foul bunt as strike three in the pitch pool (SIM-553)

> **STATUS 2026-09-25 — APPROVED on all five decisions (§14, as recommended) and BUILT as
> SIM-553.** The build record is `CHANGES.md` (2026-09-25). The adversarial review of the build
> corrected three things in this plan, marked **[corrected 2026-09-25]** where they sit: the
> export touches the pitch pool only (§7), the label check compares like with like (§8.2), and the
> rollback does not last on its own (§7). The readable page
> (https://claude.ai/artifact/NqnR74vFn4b4XRqx7fUZ1C) shows the plan as proposed on 2026-09-23.
>
> **The pitch pool codes a strikeout as a foul, and the loop plays on.** A foul tip or a foul
> bunt with two strikes is strike three by rule. The pool build labels both `foul`. The
> simulator's count machine keeps a two-strike foul alive, so every real strikeout of that
> kind becomes one more pitch. About 2,900 strikeouts a season end this way (7.1% in 2025).
> The loop turns 63% of them into something else, so the simulator makes **4.4% too few
> strikeouts**, 2.6% too many walks and 1.1% too many balls in play, measured against its own
> pool's real plays. The pool-totals grade cannot see it, because its strikeout centre is
> computed from the same labels. The fix is one expression in the pool build, a three-minute
> pool rebuild with the app stopped, a pool export and four band centres.

**Date:** 2026-09-23
**Ticket:** SIM-553 (approved and built 2026-09-25; it closes with its `CHANGES.md` entry, so it has no backlog row).
**Evidence:** the pool build as it stands at commit f8281fb
(`pipeline/batch/player_profile_computor.py`, the `outcome_type` expression in
`_build_pitch_pool`); the count machine (`simulation/sim_loop.py` `advance_count`); the
sampler (`simulation/full_pool_sampler.py`); every script that reads the pool's classes;
`raw.pitches` (Postgres) and the live `sim.pitch_pool` (DuckDB, opened read-only while the app
ran). Every number below was measured on 2026-09-23 unless it says otherwise.
**Builds on:** the dropped-third-strike review of 2026-09-23 that found the defect
(`docs/audit/2026-09-22-sim484-dropped-third-strike-box-credits-plan.md`); the hit-by-pitch
relabel (SIM-509, 2026-08-18), the same class of defect in the same expression; the Phase 4
loop spec §5.4 (`docs/architecture/2026-06-17-phase4-sim-loop-spec.md`), which says a
two-strike foul tip and foul bunt must be encoded upstream, not inferred in the loop.

---

## 0. The short version

**What the finding says.** The pool build maps the MLB feed's pitch codes to six classes the
simulator draws. The codes `T` (foul tip) and `L` (foul bunt) map to `foul` at every count.
With two strikes both are strike three. The count machine (the loop's rule that turns a
pitch's class into balls, strikes and the end of a plate appearance) treats a two-strike foul
as "the count stays, pitch again". So the drawn row is a strikeout in reality and a foul in
the game. That breaks the owner's rule that the drawn row IS the play.

**What the data says.**

- **The size.** In the regular season 2025, 2,871 strikeouts ended on a foul tip and 15 on a
  foul bunt: 2,886 of 40,593 (7.1%). The share grows every season, from 6.3% in 2017. In the
  pool window (2023 to 2026, 2,771,222 pitch rows), 11,168 rows are a two-strike `T`, `L` or
  `O`; 11,165 of them end the plate appearance as a strikeout.
- **The loss.** I ran the pool's per-count class shares through the count machine's rules
  (the count chain, §2.2). Today's coding gives 0.2163 strikeouts per plate appearance. The
  corrected coding gives 0.2262. Real plate appearances in the same seasons read 0.2255. The
  loop keeps 37% of the foul-tip strikeouts (the batter strikes out later in the same plate
  appearance) and turns 63% into a walk, a hit-by-pitch or a ball in play.
- **The corrected pool matches real play on every channel.** Corrected against real:
  strikeouts +0.3%, walks +0.4%, hit-by-pitch −0.1%, pitches per plate appearance +0.3%.
  Today against real: −4.1%, +2.9%, +0.9%, +1.1%.
- **Two rarer codes fall through to `ball`.** `O` (a foul tip on a bunt, 91 pitches in the
  window) and `Q` (a swing and a miss at a pitchout, 1 pitch) hit the expression's
  `ELSE 'ball'`. All 92 of those swings play as balls; three of them are strike three.
- **The loss is not even across pitchers.** Among 282 pitchers with 200+ strikeouts, the
  foul-tip share of their strikeouts runs from 4.7% (10th percentile) to 9.4% (90th). A single
  calibration factor cannot restore it pitcher by pitcher; only the labels can.
- **Why the grade missed it.** The pool-totals grade (the ruling of 2026-08-20: the sim's
  frequencies graded against the pool's own totals) computes its strikeout centre, 0.2165,
  from the same labels. A faithful sampler of a mislabelled pool reads green. The
  game-graded strikeout band, measured against real 2025 games, did see it: −2.5% on the
  certified lane of 2026-09-09. That band is informational since the 2026-08-20 ruling.

**The plan.** Lift the class expression into one constant built from named code sets. Add one
branch before the foul branch: a `T`, `O` or `L` with two strikes is `swinging_strike`. Map
`O` like `T`, `Q` to `swinging_strike`, the foul pitchout `R` to `foul`, the pitchout `P` to
`ball` explicitly. Replace the source-reading tests with a truth table the tests execute. Bump
the pool builder version. Stop the app, rebuild the pitch pool (about three minutes for ten
seasons), verify it against the raw codes and against real per-plate-appearance rates, export
the pool into the bundle, start the app. Repair the census script (its chain solver was
deleted on 2026-09-06) and restate four band centres. No loop code, no vocabulary change, no
migration, no profile recompute, no matrix rebuild, no weight.

**What it will show.** The simulator's strikeouts rise about 4.4% and its walks fall about
2.5%. The pool bands' deltas hold, because each centre moves with the sim. Runs fall about
3% (an estimate). The strikeout market's over-probability rises about 4 points on an average
line. This should land before the joint fit (SIM-548) resumes, so the fit does not bend the
pitcher and batter weights to absorb a labelling error.

**Five decisions** in §14, with my recommendation on each.

---

## 1. The mechanism

```
raw.pitches.type  (the MLB feed's one-letter result code per pitch)
  └─ _build_pitch_pool: the CASE expression → sim.pitch_pool.outcome_type    player_profile_computor.py:6749
        WHEN TRIM(type) IN ('F', 'T', 'L') THEN 'foul'     ⚠ a two-strike T or L is strike three; coded as a foul
        ELSE 'ball'                                        ⚠ O (a foul tip) and Q (a swing and a miss) become balls
  └─ engine_artifacts --what pool → pitch_pool/{L,R}.meta.parquet (outcome_type)   the bundle the app loads at boot
        └─ FullPoolSampler.draw(balls, strikes=2) → the drawn row's class: 'foul'
             └─ advance_count(b, 2, 'foul') → the two-strike foul rule: count unchanged, PA continues   sim_loop.py:397
                  → the next pitch: 37% of these plate appearances still end in a strikeout,
                    63% end as a walk (21% of the lost), a hit-by-pitch (1%) or a ball in play (78%)
```

The count machine is right. The Phase 4 spec (§5.4) put this rule upstream on purpose: "Foul tip
caught with two strikes = strikeout, and foul bunt with two strikes = strike three — these
must be encoded in `outcome_type` upstream by the ETL (not inferred in the loop)". The pool build is that upstream step. It never implemented
the line.

---

## 2. The evidence

### 2.1 The codes in the pool window (`raw.pitches`, 2023–2026, the pool's own filter)

The pool build takes every pitch with `data_quality_flag = FALSE` in the window, postseason
included. The strikeout column counts rows whose `events` is a strikeout.

| Code | What it is | Class today | Below two strikes | At two strikes | Strikeouts | Class after |
|---|---|---|---|---|---|---|
| `B` | ball | ball | 662,870 | 266,074 | — | ball |
| `*B` | ball in the dirt | ball | 33,122 | 26,411 | — | ball |
| `P` | pitchout | ball (by `ELSE`) | 138 | 50 | 0 | ball (explicit) |
| `C` | called strike | called_strike | 415,357 | 37,254 | 37,239 | called_strike |
| `S` | swinging strike | swinging_strike | 191,216 | 100,022 | 99,940 | swinging_strike |
| `W` | swinging strike, blocked | swinging_strike | 4,401 | 11,425 | 11,423 | swinging_strike |
| `M` | missed bunt | swinging_strike | 809 | 13 | 12 | swinging_strike |
| `Q` | swing and miss at a pitchout | **ball** (by `ELSE`) | 0 | 1 | 1 | swinging_strike |
| `F` | foul | foul | 306,535 | 192,190 | 0 | foul |
| `T` | foul tip | **foul** | 17,472 | 11,111 | 11,109 | foul below two; **swinging_strike at two** |
| `L` | foul bunt | **foul** | 4,778 | 55 | 55 | foul below two; **swinging_strike at two** |
| `O` | foul tip on a bunt | **ball** (by `ELSE`) | 89 | 2 | 2 | foul below two; **swinging_strike at two** |
| `R` | foul pitchout | ball (by `ELSE`) | 0 (2 in 2018–2021) | 0 | 0 | foul |
| `X`/`D`/`E` | in play | in_play | 299,361 | 182,544 | — | in_play |
| `H` | hit by pitch | hit_by_pitch (the events branch) | 4,827 | 3,095 | — | hit_by_pitch |

These are all the codes the table holds in ten seasons; there is no `V` or `Z`. The live pool
agrees row for row: its two-strike `foul` rows number 203,356 (192,190 + 11,111 + 55) and its
two-strike `ball` rows 292,538 (the `B`, `*B`, `P`, `O` and `Q` rows). The change moves
**11,258 of 2,771,222 window rows (0.41%)**.

Three of the 11,168 two-strike `T`/`L`/`O` rows are not strikeouts in the feed (one catcher's
interference, two with no event). The corrected rule makes them strikeouts in the game. I
recommend the rule over the event label anyway (§4.2).

### 2.2 The count chain: today, corrected, real

The count chain is a calculation. It takes the pool's class shares at each of the twelve
counts, applies the count machine's rules, and returns the per-plate-appearance rates a
faithful sampler of that pool produces. The census script used it to set the band centres on
2026-08-20.

| Per plate appearance, 2023–2026 | Strikeouts | Walks | Hit by pitch | In play | Pitches |
|---|---|---|---|---|---|
| The band centres (the census, 2026-08-20) | 0.2165 | 0.0850 | 0.0113 | — | 3.938 |
| The pool today (re-read 2026-09-23, unweighted) | 0.2163 | 0.0850 | 0.0113 | 0.6874 | 3.938 |
| The pool, corrected coding (unweighted) | **0.2262** | **0.0829** | **0.0112** | 0.6797 | **3.906** |
| The pool, corrected coding (recency-weighted) | 0.2257 | 0.0830 | 0.0111 | 0.6801 | 3.904 |
| Real plate appearances (official scoring) | 0.2255 | 0.0826 | 0.0112 | — | 3.895 |
| Today against real | −4.1% | +2.9% | +0.9% | | +1.1% |
| Corrected against real | +0.3% | +0.4% | −0.1% | | +0.3% |

The "real" row counts the 708,439 plate appearances in the window that end in a batting
event (intentional walks have no pitch rows; steals and pickoffs that end an inning are not
plate appearances). I computed the corrected row twice: from `raw.pitches` with the new
expression, and from the live `sim.pitch_pool` with the moved rows relabelled. Both give
0.2262.

### 2.3 By season

| Season | Strikeouts lost | Walks added | Pitches added | Strikeouts ending on T/L/O (regular season) |
|---|---|---|---|---|
| 2023 | −4.24% | +2.53% | +0.82% | 6.84% |
| 2024 | −4.32% | +2.59% | +0.84% | 6.96% |
| 2025 | −4.47% | +2.70% | +0.84% | 7.11% |
| 2026 (to date) | −4.48% | +2.44% | +0.85% | 7.14% |

The share was 6.24–6.48% in 2017–2021. It rises with the game's strikeout rate.

### 2.4 By pitcher and by batter (2023–2026, 200+ strikeouts)

| Group | n | Mean share of strikeouts on T/L/O | Spread (sd) | Binomial noise | 10th / 90th percentile |
|---|---|---|---|---|---|
| Pitchers | 282 | 6.9% | 1.92 points | 1.36 points | 4.7% / 9.4% |
| Batters | 317 | 7.1% | 1.85 points | 1.34 points | 4.7% / 9.5% |

About 1.3 points of the spread is real (the sd with the noise taken out). The loop loses 63%
of each actor's foul-tip strikeouts, so the loss runs from about 3% of a 10th-percentile
pitcher's strikeouts to about 6% of a 90th-percentile pitcher's. Fastball velocity does not
predict it (correlation −0.03).

### 2.5 The got-away flag on the moved rows

The dropped-third-strike rule (SIM-484) lets the batter reach only on a `swinging_strike` row
whose got-away flag (a passed ball or a wild pitch on that pitch) is set. By rule a foul tip
is caught; if the catcher drops it, it is a foul ball. The pool agrees: **0 of the 11,163
two-strike foul rows with a strikeout event carry the got-away flag.** Relabelling them
`swinging_strike` cannot create a dropped third strike.

### 2.6 Why every grade missed it

- **The pool bands** grade the sim against the chain of the pool's own labels. The labels are
  the defect, so the band reads the defect as the truth (K_PA −0.9% on the 2026-09-21 lane,
  a pass).
- **The game-graded strikeout band** (real 2025 strikeouts per team-game) read −2.5% on the
  certified lane of 2026-09-09 (`scripts/sim523_lane_h.txt`) and about −2.0% on the lane of
  2026-09-21. It is informational since the 2026-08-20 ruling.
- **The accuracy comparison** reads the strikeout probabilities low: 6.5 points on the split's
  250-game read (2026-09-14), 9.5 points on the 1,000-game baseline (2026-09-15). The SIM-548
  plan gives that bias to a calibration layer. Part of it is this defect.

The hit-by-pitch relabel (SIM-509) was the same kind of defect in the same expression: a real
event coded into the wrong class, invisible to a grade built on the same labels. §8.2 adds a
check that compares the pool's chain with real plate appearances, so the next one shows up.

### 2.7 The balanced certifying set stays valid

The lane grades on 45 games chosen so every graded channel's expected rate, matched to the
games' own players, sits within 0.6% of the pool's totals (the ruling of 2026-09-09). I scored
the recorded set (`scripts/sim523_game_set.json`) with the script's own formula on the live
pool, today and with the moved rows relabelled:

| Channel | Today | Corrected |
|---|---|---|
| Strikeouts per plate appearance | +0.41% | +0.30% |
| Walks | −0.40% | −0.39% |
| Hit by pitch | −0.21% | −0.18% |

Every channel stays inside 0.6%. The set does not need a re-pick.

---

## 3. Findings that shape the plan

**Finding 1 — the class is `swinging_strike`, and nothing downstream needs to learn it.** A
two-strike foul tip or foul bunt is strike three on a swing: the batter swung, and no umpire
called the strike. The count machine already ends the plate appearance on
a two-strike `swinging_strike`. Every script that counts strikeouts from the pool's classes —
the census chain, the balanced-set script, the joint fit's offline chain
(`scripts/sim548_offline_fit.py` `pa_end_probabilities`), the fatigue scan — counts
`called_strike` plus `swinging_strike` at two strikes. With this class they are all right the
moment the pool is rebuilt. A new seventh class would need each of them changed, and a missed
one keeps the loss silently.

**Finding 2 — only the two-strike rows need to move.** Below two strikes a foul tip, a foul
bunt and a foul all add one strike. The count machine cannot tell them apart, so the 17,472
non-terminal foul tips can keep the `foul` class. Keeping them there keeps the profile's
definition of a swing and a miss (`WHIFF_TYPES`: `S`, `W`, `M`, plus `Q` under decision 2)
equal to the pool's
unconditional `swinging_strike` codes, which is what the profile test enforces.

**Finding 3 — the expression must be one constant, tested by execution.** The current tests
read the pool build's source with a regular expression. A new two-strike branch written as
`… IN ('T', 'O', 'L') AND strikes = 2 THEN 'swinging_strike'` does not match that pattern, so
the whiff test stays green while the pool's class and the profile's definition diverge. And a
CASE takes the first branch that matches: the new branch placed after the foul branch never
fires, the SQL is valid, the build succeeds, and the loss stays. Only an executed truth table
catches both.

**Finding 4 — the silent `ELSE 'ball'` is how both defects hid.** `O`, `Q` and `R` reached
`ball` through it, as the hit-by-pitch rows did before SIM-509. One 2022 hit-by-pitch row has
no event label (`game_pk` 662280); only its `H` code identifies it.

**Finding 5 — the census cannot run.** `scripts/pool_window_census.py` imports its chain
solver from `scripts/sim429_chain_analysis.py`, which commit 6ab341c deleted on 2026-09-06.
The script that sets four band centres fails on import.

**Finding 6 — only the pitch pool changes.** The outcome pool is the in-play subset (no
in-play row moves). The steal, stolen-base and advancement pools join the pitch pool by the
pitch's key, not its class. The receiving document reads only taken pitches (`ball`,
`called_strike`); the 92 `O` and `Q` rows (swings coded as balls) leave it, and the receiving
ratio ships OFF. The manager's change pool counts pitches. The actor score matrices come from the profiles, which read the
raw codes. So: one table rebuilt, one export, nothing else.

**[corrected 2026-09-25]** "One export" must mean the pitch pool alone. The engine-artifact
export `--what pool` rewrites four pools, and the review found DuckDB's steal and advancement
pools ahead of the bundle: DuckDB holds the 2026 games of 08-14 to 08-29 (14,004 + 9,417 more
steal-opportunity rows and 4,444 more advancement rows); the bundle stops at 2026-08-13. A
`--what pool` export would ship that refresh with this change, unapproved, and confound the
before/after smoke. The rebuild therefore exports the pitch pool only and recomputes its
batted-ball join; it reports the other pools' gap and leaves the refresh to the owner.

---

## 4. The coding change

### 4.1 The code sets and the expression

```python
# pipeline/batch/player_profile_computor.py — the Statcast code block (near line 170)

#: SIM-553: the MLB feed's pitch-result codes, one set per pool class. The pool
#: build's outcome_type expression is rendered from these (SQL_OUTCOME_TYPE) and
#: the profile SQL reads WHIFF_TYPES / NON_SWING_TYPES, so the two cannot drift.
BALL_TYPES: tuple[str, ...] = ("B", "*B", "P")            # P: a pitchout, not swung at
CALLED_STRIKE_TYPES: tuple[str, ...] = ("C",)
#: A swing and a miss: S, W (the catcher did not hold it), M (a missed bunt),
#: Q (at a pitchout). Foul tips are CONTACT and stay out.
WHIFF_TYPES: tuple[str, ...] = ("S", "W", "M", "Q")
#: The bat touched the ball and it was not put in play: F foul, T foul tip,
#: L foul bunt, O foul tip on a bunt, R foul pitchout. Each adds a strike below
#: two strikes.
FOUL_TYPES: tuple[str, ...] = ("F", "T", "L", "O", "R")
#: The fouls that are STRIKE THREE with two strikes: a foul tip is a strike at any
#: count, and so is a bunt fouled off (the rulebook's definition of a strike; Rule
#: 5.09(a)(4)). A plain foul (F, R) with two strikes is not.
STRIKE_THREE_FOUL_TYPES: tuple[str, ...] = ("T", "O", "L")
IN_PLAY_TYPES: tuple[str, ...] = ("X", "D", "E")

#: The pool's class for one raw.pitches row (defined after sql_in). The first
#: matching branch wins, so the two-strike branch MUST precede the foul branch. No ELSE: an unknown code
#: yields NULL and the NOT NULL column refuses the insert (decision 3).
SQL_OUTCOME_TYPE = f"""
    CASE
        WHEN events = 'hit_by_pitch' OR TRIM(type) = 'H' THEN 'hit_by_pitch'
        WHEN TRIM(type) IN {sql_in(BALL_TYPES)} THEN 'ball'
        WHEN TRIM(type) IN {sql_in(CALLED_STRIKE_TYPES)} THEN 'called_strike'
        WHEN TRIM(type) IN {sql_in(WHIFF_TYPES)} THEN 'swinging_strike'
        WHEN TRIM(type) IN {sql_in(STRIKE_THREE_FOUL_TYPES)} AND strikes = 2 THEN 'swinging_strike'
        WHEN TRIM(type) IN {sql_in(FOUL_TYPES)} THEN 'foul'
        WHEN TRIM(type) IN {sql_in(IN_PLAY_TYPES)} THEN 'in_play'
    END"""
```

```python
# _build_pitch_pool — the SELECT
                    {SQL_OUTCOME_TYPE}                  AS outcome_type,
```

`strikes` is the raw count before the pitch, the same column the pool stores as
`count_strikes`. `POOL_BUILDER_VERSION` moves from `sim523g.1` to `sim553.1`, so the
incremental gate treats every season built before as stale.

### 4.2 Why the count rule, not the event label

The alternative condition is `AND events IN ('strikeout', 'strikeout_double_play')`. It
moves exactly the 11,165 rows the feed scored as strikeouts and leaves three anomalies as
fouls. I recommend the count rule: it is the rule the umpire applies, it labels the pitch by
what the pitch did, and the count machine then decides whether the plate appearance ends. It
agrees with the feed on 11,165 of 11,168 rows; the three it overrules are feed anomalies (one
catcher's interference, two rows with no event). The difference is three rows in 2.8 million.

### 4.3 What stays the same

- The class vocabulary: `ball`, `called_strike`, `swinging_strike`, `foul`, `in_play`,
  `hit_by_pitch` (`simulation/game_state.py` `PITCH_OUTCOMES`, the sampler's `_OUTCOMES`,
  `simulation/synthetic_bundle.py`). No loop or sampler code changes.
- `raw.pitches`: the feed's codes stay as they are. No re-sweep.
- The DuckDB schema: `outcome_type` is `VARCHAR(20) NOT NULL` with no check constraint. No
  migration, no schema version bump.
- `prev_pitch_outcome` stores the previous pitch's raw code, not its class.

---

## 5. Every reader of the pool's class

| Reader | What it reads | Effect | Action |
|---|---|---|---|
| `simulation/sim_loop.py` `advance_count` | the drawn class | a two-strike `swinging_strike` ends the plate appearance as a strikeout | none |
| `simulation/full_pool_sampler.py` `draw` | the drawn row's class | returns the new class | none |
| the pitch / pitch-result split (`_result_draw`, ON since 2026-09-14) | draws the result row among the count bucket's rows; the class is the row's own | the two-strike result rows now carry the right class; no class logic in the weights | none |
| the sampler's `taken` mask and the receiving ratio (`_recv_factor`) | `ball` and `called_strike` rows | loses the 92 `O` and `Q` rows (swings coded as balls); the ratio ships OFF (its enable is SIM-526) | none |
| the dropped-third-strike rule (`_dropped_third_strike`, SIM-484) | `swinging_strike` plus the got-away flag | the moved rows carry no got-away flag (0 of 11,163) | none; §2.5 |
| the got-away label (the pool build) | `passed_ball_wild_pitch` and the strikeout's play description | unchanged; computed from raw columns | none |
| `pipeline/batch/engine_artifacts.py` pitch-pool export | `outcome_type` into `{hand}.meta.parquet` | the bundle carries the new class | export the pitch pool only (the rebuild script; §7, corrected) |
| `engine_artifacts` receiving document (`--what receiving`) | taken rows | the 92 `O` and `Q` rows leave; the ratio is OFF | none now; the next receiving build picks it up |
| `engine_artifacts` manager change pool | pitch counts per plate appearance | unchanged | none |
| the outcome pool build | `outcome_type = 'in_play'` | unchanged | none |
| the steal, stolen-base and advancement pool builds | the pitch pool's key | unchanged | none |
| the profile SQL (`WHIFF_TYPES`, `NON_SWING_TYPES`, csw, chase, contact) | the raw codes | `WHIFF_TYPES` gains `Q` (2 pitches in ten seasons); nothing else moves | no profile recompute; the next recompute picks up `Q` |
| `tests/unit/test_sim501_profile_code_sets.py` | the pool build's source, by regular expression | cannot see a conditional branch (Finding 3) | rewritten to execute the constant (§9) |
| `tests/unit/test_sim509_hit_by_pitch.py` | the source order of the HBP branch | the text moves into the constant | rewritten to execute it (§9) |
| `scripts/pool_window_census.py` | the pool's per-count classes, through the chain | the four centres move (§8) | repair the import; add the real-rate check |
| `tests/acceptance/bands.py` `POOL_REFERENCES` | the census's numbers | K_PA, BB_PA, HBP_PA, PITCHES_PA restate | four constants (§8) |
| `scripts/sim523_game_set.py` | two-strike strike classes as strikeouts | picks up the fix; the recorded set stays within 0.6% (§2.7) | none |
| `scripts/sim548_offline_fit.py` (the joint fit's offline chain) | the same strikeout rule | picks up the fix | none; its earlier reads are on the old coding (§11.4) |
| `scripts/sim518_fatigue_scan.py`, `sim518_fatigue_probe.py` | class indexes | picks up the fix | none |
| `similarity/engines/pitch_pitch_similarity.py` | a neighbour's `outcome_type`, for display | a two-strike foul-tip neighbour shows `swinging_strike` | none |
| the play-by-play (`simulation/snapshots.py`) and the API | `pitch_outcome` strings | a foul-tip strikeout shows `swinging_strike` | none; §12.5 |
| the Data Lab (`api/routes/analytics.py`, `frontend/src/components/lab`) | Statcast `description` in `raw.pitches` | unaffected | none |

---

## 6. Code changes, file by file

| File | Change | What |
|---|---|---|
| `pipeline/batch/player_profile_computor.py` | modified | the code sets and `SQL_OUTCOME_TYPE` (§4.1); `_build_pitch_pool` renders the constant; `POOL_BUILDER_VERSION` → `sim553.1` with its history line; the stale pointer to `test_profile_code_sets_match_pool_build` (no test has that name) re-pointed to `tests/unit/test_sim501_profile_code_sets.py`; the `WHIFF_TYPES` comment updated |
| `scripts/sim553_rebuild_pitch_pool.py` | new | the pool-only rebuild with its verification (§7), modelled on `scripts/archive/sim518_rebuild_pools.py` |
| `scripts/pool_window_census.py` | modified | the chain solver restored inline (the 40-line `solve_chain` from the deleted `scripts/sim429_chain_analysis.py`, verbatim); a per-window line comparing the chain with real plate appearances (§8.2) |
| `tests/acceptance/bands.py` | modified | four centres and the census source string (§8.1) |
| `tests/unit/test_sim553_foul_tip_strike_three.py` | new | §9 |
| `tests/unit/test_sim501_profile_code_sets.py` | modified | the regular-expression helper replaced by the executed truth table (§9) |
| `tests/unit/test_sim509_hit_by_pitch.py` | modified | the order test executes the constant; the version pin |
| `tests/unit/test_sim518_conditioning.py`, `tests/unit/test_sim523_part_g.py` | modified | the `sim523g.1` version pins |
| `docs/architecture/2026-06-17-phase4-sim-loop-spec.md` | modified | §5.4's foul-tip line marked implemented (the pool build, SIM-553) |
| `docs/technical/sim-loop-cheat-sheet.md` | modified | one line under the pitch draw: a two-strike foul tip or foul bunt is strike three in the pool |
| `BACKLOG.xlsx`, `CHANGES.md` | modified | the row (when filed) and the record |

No change to `simulation/`, `api/`, `similarity/` or the frontend.

---

## 7. The rebuild

**Why the app stops.** The app's worker server holds the DuckDB writer lock while it runs
(SIM-524, the open ticket for that lock). The last pool rebuild (SIM-523 part G) ran with the
app stopped for that reason. The app loads the bundle at boot, so the restart also loads the
new pool.

**How long.** The last pitch-pool rebuild (SIM-523 part G, 2026-09-09) took 1.2 minutes for
four seasons. **[corrected 2026-09-25]** The review ran the ten-season rebuild as one
transaction on a copy: the COMMIT alone took about 195 seconds (peak memory 4.2 GB). With the
checks, the copy and the export the app is down about ten to fifteen minutes.

**`scripts/sim553_rebuild_pitch_pool.py`, step by step:**

1. Assert `POOL_BUILDER_VERSION == "sim553.1"`. Snapshot `(pitch_id, outcome_type)` for the
   chosen seasons into a temporary table. Read the rows per season.
2. Rebuild: `_build_pitch_pool(seasons, incremental=False)` (the `__new__` pattern the SIM-518
   rebuild used).
3. Verify, and stop before the export if any check fails:
   - a. Rows per season equal the snapshot's.
   - b. **The moved rows are exactly the expected rows.** Join the snapshot, the new pool and
     `pg.raw.pitches` on the key. Every row whose class changed has an expected code and count
     (a two-strike `T`/`L`/`O` from `foul` or `ball` to `swinging_strike`; a sub-two-strike
     `O` from `ball` to `foul`; `Q` to `swinging_strike`; `R` to `foul`), and every expected
     row changed. In the window that is 11,258 rows.
   - c. Every moved foul tip, foul bunt and bunt foul tip's got-away flag is false.
     **[corrected 2026-09-25]** Not every moved row: one moved `Q` (a swing and a miss at a
     pitchout, 2026, a strikeout with a passed ball) carries a real got-away, which is right.
   - d. **The end-to-end check.** The count chain of the new window pool against real plate
     appearances from `raw.pitches` (the §2.2 definition): strikeouts, walks, hit-by-pitch and
     pitches per plate appearance each within 0.5%. **[corrected 2026-09-25]** The chain runs
     over the plate appearances that end in a batting event only (see §8.2). Expected: K +0.09%,
     BB −0.25%, HBP +0.08%, pitches +0.07%.
   - e. `sim.pool_build_metadata` reads `sim553.1` for every rebuilt season.
4. Copy the bundle aside (`/data/play_pool/engine_artifacts` → `engine_artifacts.pre_sim553`,
   536 MB; the volume has 932 GB free). **[corrected 2026-09-25]** Then export the pitch pool
   ONLY (`build_pitch_pool_artifact`) and recompute its batted-ball join (`bb_row`) against
   the bundle's untouched batted-ball pool. Not `engine_artifacts --what pool`, which also
   rewrites the batted-ball, steal and advancement pools (Finding 6).
5. Round-trip, without loading the bundle: every exported class equals DuckDB's for the same
   pitch; every class change from the copy to the export is that pitch's expected move; the
   batted-ball, steal and advancement files are byte-identical to the copy's.

**Rollback.** Stop the app, swap the copy back, start the app: about two minutes, and the app
runs the old coding again. **[corrected 2026-09-25]** The rollback does not last on its own.
DuckDB keeps the new coding (the app's boot loads it only into the pitch-to-pitch engine, which
no route and no draw uses), so the next pool export — `make engine-artifacts`, the nightly
chain's step 3, any `engine_artifacts --what pool` or `--what all` — writes it back. Hold every
export until the table is rebuilt on the coding you keep; a nightly run rebuilds only the
current season and would leave a mixed pool.

---

## 8. The references

### 8.1 The four centres

Run the repaired census after the rebuild. Expected, from §2.2 (unweighted, the census's
convention):

| Band | Today | Expected after | Floor (unchanged rule) |
|---|---|---|---|
| K_PA | 0.2165 | 0.2262 | 2% of the centre |
| BB_PA | 0.0850 | 0.0829 | 2% |
| HBP_PA | 0.0113 | 0.0112 | 6% |
| PITCHES_PA | 3.938 | 3.906 | 1.5% |

The source string `_CENSUS` gets the new date. The census reads the per-ball-in-play centres
from the outcome pool, which does not change. IBB_PA comes from `sim.ibb_rates` and does not
change. CALLED_STRIKE_TAKEN moves by the 92 `O` and `Q` rows in about 1.44 million taken
pitches (under 0.01%); I restate it only if the census shows a move in its fourth decimal.

### 8.2 The label check the census gains

Two coding defects in one expression (the hit-by-pitch rows and now the foul tips) passed the
pool-totals grade, because the grade and the pool share the labels. The census gains one line
per window: the chain's strikeout, walk, hit-by-pitch and pitch rates beside the real
plate-appearance rates from `raw.pitches`, with the percentage gap. The rebuild script's check
3d is the same comparison as a hard stop. The owner decides whether it also becomes a
standing test (decision 5).

**[corrected 2026-09-25] Compare like with like.** The review found that the chain over every
pool row and the count of real plate appearances cover different groups. About 0.6% of at-bat
groups never reach a batting event (an inning ended by a pickoff or a caught stealing, a game
ended mid-plate-appearance); their pitches sit in the pool, and the chain plays them on to an
end, which adds about 0.6 points to walks. Since 2023 a second effect pulls the other way:
pitch-clock automatic balls have no pitch row (547 in 2023, fading to 185 in 2026 so far). The
two cancelled on today's window (walks +0.36%), but 2020–2022 already read +0.5% to +0.7% with
the corrected coding, so the check would have gone red on a future window with no coding
defect. The label check therefore runs the chain over the groups that end in a batting event
only (`pool_chain.chain_rates(..., pa_only=True)`), judged by the same rule as the real count.
The band centres stay the full-pool chain, because the simulator draws every row. On the
corrected window the restricted check reads K +0.09%, BB −0.25%, HBP +0.08%, pitches +0.07%; on
the old coding it still fails every channel (K −4.29%, BB +2.31%).

---

## 9. Tests

**New — `tests/unit/test_sim553_foul_tip_strike_three.py`:**

- **The truth table.** Every code the table holds (`B`, `*B`, `P`, `C`, `S`, `W`, `M`, `Q`,
  `F`, `T`, `L`, `O`, `R`, `X`, `D`, `E`, `H`) at strikes 0, 1 and 2, executed through
  `SQL_OUTCOME_TYPE` in an in-memory DuckDB, against the class §2.1 names. 51 cases.
- **The order.** A two-strike `T` is `swinging_strike`, a one-strike `T` is `foul`, a
  two-strike `F` is `foul`. Moving the two-strike branch after the foul branch fails this.
- **The events branch.** A row with a ball code and the event `hit_by_pitch` is
  `hit_by_pitch`; an `H` row with no event is `hit_by_pitch` (the 2022 row).
- **An unknown code fails loudly** (decision 3): an insert of a `Z` row into a `NOT NULL`
  column raises.
- **The pool build uses the constant.** `_build_pitch_pool`'s source contains
  `{SQL_OUTCOME_TYPE}` and no `WHEN TRIM(type)`.
- **The count machine end to end.** `advance_count(b, 2, …)` on the class the truth table
  gives a two-strike `T`, `L` and `O` returns a strikeout; on a two-strike `F`, the count
  unchanged.
- **The chain solver.** Toy matrices with known answers: every pitch a ball (walk 1.0, four
  pitches); every pitch a called strike (strikeout 1.0, three pitches); a two-strike foul
  share of one half doubles the expected two-strike pitches.

**Modified:**

- `test_sim501_profile_code_sets.py`: `_pool_build_codes` (the regular expression) becomes a
  helper that executes the constant. The headline invariant becomes: the codes that map to
  `swinging_strike` at strikes 0 and 1 equal `WHIFF_TYPES`. New: every
  `STRIKE_THREE_FOUL_TYPES` code is outside `WHIFF_TYPES` (contact is not a whiff). The
  take-code test keeps its meaning: the `ball` and `called_strike` codes are a subset of
  `NON_SWING_TYPES` (`P` is in both).
- `test_sim509_hit_by_pitch.py`: the source-order test becomes the executed events test; the
  version assertion reads `sim553`.
- `test_sim518_conditioning.py`, `test_sim523_part_g.py`: the version pins.

The regression lane is untouched (no engine change).

---

## 10. Run book

```
# 0. BEFORE: the ten-game smoke on the current bundle, the baseline for step 7
#    (the first ten games of the balanced set, 200 iterations each; scripts/ is not bind-mounted)
MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" app python scripts/sim_stats.py --iters 200 --json-out /app/scripts/sim553_smoke_before.json <ten game_pks>
# 1. the code lands; ruff, mypy, the unit lane, the regression lane
# 2. stop the app (the DuckDB writer lock, SIM-524); check no other session is mid-lane or mid-smoke
docker compose stop app
# 3. the rebuild, detached; read its log to the end (verification 3a-3e, export, round-trip)
MSYS_NO_PATHCONV=1 docker compose run -d --rm -v "$PWD/scripts:/app/scripts" app python scripts/sim553_rebuild_pitch_pool.py
# 4. start the app; the boot log reads build_all_engines: 11/11 and the bundle loads
docker compose up -d app
# 5. the census, then the four centres into tests/acceptance/bands.py
MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" app python scripts/pool_window_census.py
# 6. the balanced set's score on the rebuilt pool (expected strikeouts +0.30%, every channel inside 0.6%)
# 7. AFTER: the same ten-game smoke; read strikeouts, walks, hits and runs per team-game against step 0
# 8. close: CHANGES.md; the cheat-sheet line; the spec line; the ticket row
```

What step 7 should read, on about 4,000 team-games (strikeouts per team-game carry a
standard error near 0.6% at that size, so a 4% move is plain):

| Per team-game | Expected move |
|---|---|
| Strikeouts | +4 to +5% |
| Walks | −2 to −3% |
| Hits | about −1% |
| Runs | about −3% |

A strikeout move under 2% or over 7% means a reader did not pick up the class, or a branch is
out of order. Stop and read the smoke's per-pitcher lines.

---

## 11. What it will show

### 11.1 The pool bands

Each of the four centres moves with the sim, so the deltas the lane reads should hold:
strikeouts about −0.9% (a pass), walks about +3% (still red), hit by pitch about +8% (still
red), pitches per plate appearance a pass. **The fix does not cure the walk and hit-by-pitch
reds**; they are the sampler's tilt against its own pool, and the pool moves with the sim.
The per-ball-in-play bands, the steal bands and the got-away band should not move beyond
noise.

### 11.2 The game-graded channels (informational)

- **Strikeouts per team-game** rise from about −2.0% of real 2025 (the 2026-09-21 lane) to
  about +2% (my estimate). The sim's strikeouts per plate appearance land on the real rate; the overshoot
  per team-game comes from the extra plate appearances the walk and hit-by-pitch surplus
  gives each game.
- **Runs** fall about 3%. My estimate: the change turns about one plate appearance in a
  hundred (0.0099) from a walk (0.0021), a hit-by-pitch (0.0001) or a ball in play (0.0077)
  into a strikeout. At standard run values (a strikeout −0.27, a walk +0.30, a ball in play
  about +0.04 on the pool's mix), each changed plate appearance costs about 0.37 runs, so
  every plate appearance costs 0.0037. At 38 plate appearances that is −0.14 runs a team-game.
  The 2026-09-22 lane read 4.556 runs a team-game (+2.4% of 4.4473); the estimate puts it
  near 4.42 (−0.6%), inside the band's 2.75% floor. I did not run this.
- **Hits** fall about 1% (fewer balls in play).

### 11.3 The strikeout market

- The simulator's strikeouts per plate appearance rise about 4.4%. A starter's projected
  strikeouts rise about 0.23 a start.
- The chance of the over rises by about 4 points on an average line. The arithmetic: a
  0.23-strikeout shift times the probability mass at the line (about 0.17 a strikeout for a
  mean near five).
- The low bias shrinks by about that much. It read 9.5 points on the 1,000-game baseline and
  6.5 points on the split's 250-game read. Production changed after both reads (every power
  at 1 since 2026-09-16), so today's bias is unmeasured.
- **The ranking of pitchers (the AUC: the chance a real over drew the higher probability) should
  not move measurably.** Each pitcher's correction is +3% to +6% of his strikeouts; that
  spread is small against what separates pitchers.
- The totals market's projected runs fall about 3%. The 1,000-game baseline's check games
  read the over 3.9 points high (`scripts/sim548_market_calibration_2024split.txt`), so the
  move is in the right direction; I have not sized it.

### 11.4 The joint fit (SIM-548) and the calibration

The joint fit is paused until the model is final (owner decision, 2026-09-16). It fits every
weight against the accuracy comparison. A 4.4% strikeout deficit in the labels is a level
error the fit would absorb into the pitcher and batter powers or the calibration map, where it
does not belong. **Land this before the fit resumes.** The strikeout calibration map (the
slope 0.15, fitted on the old coding) and the offline fit's reads are stale after the rebuild;
the resume refits both, as it would anyway. The accuracy comparison stamps the bundle on every
record, so the resume cannot mix old and new records.

---

## 12. What could go wrong (ranked)

1. **The two-strike branch sits after the foul branch.** The SQL is valid and the loss stays.
   Caught three times: the executed order test, the rebuild's check 3b (the moved rows), and
   check 3d (the chain against real play).
2. **A reader treats every `swinging_strike` row as a swing and a miss.** I searched every
   reader (§5). The dropped-third-strike rule needs the got-away flag, which no moved row
   carries. The joint fit's "whiff-plus-called share" counts strikes, which these are. No
   model decision reads the class as "the bat missed".
3. **A new feed code stops the nightly pool build** (decision 3). That is the intent; the fix
   is one entry in a code set. Today such a code becomes a ball silently.
4. **Runs read low after the fix.** If the walk and ball-in-play surplus was hiding a run
   shortfall elsewhere, runs could fall below the estimate. The smoke measures it. After the
   estimated drop, runs sit about 2 points above the band's lower edge (−0.6% against a
   −2.75% floor). A red there would be a finding, not a reason to keep the wrong labels.
5. **The play-by-play loses the words "foul tip".** A foul-tip strikeout shows
   `swinging_strike` per pitch. It is a strike three on a swing, which the class says, and
   nothing in the frontend reads the class for a foul tip. Cosmetic.
6. **The app stop collides with another session.** The dropped-third-strike work (SIM-484)
   restarts the app too. Check before step 2. The two changes do not interact: the moved rows
   carry no got-away flag.
7. **[corrected 2026-09-25] The version bump marks every other pool stale.** The builder version is one constant for
   all pools, so the next incremental pool build rebuilds the outcome, steal and advancement
   pools for its seasons. For 2023–2026 their rows come out identical; it costs minutes. For
   2017–2022 the dependent pools were built by older builders (outcome sim491.1, steal and
   stolen-base sim501.1, advancement sim510.1), so a full ten-season run would change them;
   the nightly rebuilds the current season only. Merge the code before the next nightly pool
   build, or that build rebuilds the current season on the OLD coding (the incremental gate
   sees the new version as stale under the old code).

---

## 13. Found on the way (not in scope)

- **The batter's contact rate leaves out foul tips and foul bunts.** `contact_rate` counts
  `D`, `E`, `F`, `X` over swings; `T`, `L`, `O`, `Q`, `R` are swings in the denominator and in
  neither the whiff nor the contact numerator. Whiff plus contact sums to about 97.5% of
  swings, not 100%. A batter-engine feature; a separate row if the owner wants it.
- **Two definitions of a whiff in one product.** The Data Lab's analytics
  (`api/routes/analytics.py` `_WHIFF`) count a foul tip as a whiff; the profile does not. A
  display metric, not the model.
- **The called-strike-plus-whiff rate** (`csw_rate`) counts `T` and `O` at every count. That
  overlap was found on 2026-07-23 and parked; it stays parked.
- **The 2017–2022 pitch-pool seasons were built by `sim509.1`**: no catcher, got-away,
  batting-side or fatigue columns (all NULL). Decision 4's ten-season rebuild fills them. The
  outcome, steal and advancement pools for those seasons stay on older builders until the
  pool-window test (the joint fit's first step) rebuilds the ten-season chain.

---

## 14. Decisions for the owner

1. **The class for a two-strike foul tip or foul bunt.** Recommended: `swinging_strike`, only
   at two strikes (§4.1). No vocabulary change, no loop change; it is a strike three on a swing;
   every script that counts strikeouts from the pool is right at once
   (Finding 1). Alternative: a seventh class (for example `foul_strike_three`). The label reads
   more honestly, but the class list in five modules and every strikeout-counting reader must
   learn it, and a missed reader keeps the loss silently. A second alternative, every foul tip
   as `swinging_strike` at every count (a foul tip is a live-ball strike by rule), moves
   17,472 non-terminal rows the count machine cannot tell apart, and puts `T` into the whiff
   definition or breaks the profile test.
2. **The rare codes.** Recommended: `O` like `T`; `Q` a `swinging_strike` at every count and a
   member of `WHIFF_TYPES` (a swing and a miss; two pitches in ten seasons, no profile
   recompute); `R` a `foul`; `P` a `ball`, spelled out; `H` a `hit_by_pitch` beside the events
   branch. Alternative: fix only `T` and `L` and leave the others on `ELSE 'ball'`.
3. **An unknown code.** Recommended: no `ELSE`, so an unknown code makes the insert fail and a
   person looks. The silent `ELSE 'ball'` is how `O`, `Q` and the hit-by-pitch rows hid.
   Alternative: keep `ELSE 'ball'` and have the rebuild's verification count unknown codes and
   stop.
4. **The seasons to rebuild.** Recommended: all ten (2017–2026). About three minutes; the whole
   table on one coding; the older seasons gain the columns the pool-window test needs.
   Alternative: the window only (2023–2026), the seasons production draws from, and the older
   seasons wait for the pool-window test's own rebuild.
5. **The gate after the rebuild.** Recommended, per the ruling of 2026-09-16 (every change lands
   ON with the cheap gates only): the unit and regression lanes, the rebuild's end-to-end check
   (3d), and the ten-game smoke before and after. No accuracy arm; the joint fit's resume reads
   the strikeout market. No 45 × 130 lane now; the next lane reads the new centres. And:
   make the label check (§8.2) a standing test that fails when the pool's chain drifts more
   than 0.5% from real plate appearances. Alternative: add a 45 × 130 lane (about 1.5 hours)
   to certify the four new centres before closing.

Recorded, not asked: no flag (the coding is a fact about the play, not a weight); no
migration; no profile recompute; no matrix rebuild; no calibration refit now (the joint fit's
resume refits); the receiving document unchanged until its next build (the ratio is OFF); the
balanced set stays (+0.30% on strikeouts); the three anomalous two-strike rows follow the count
rule (§4.2).
