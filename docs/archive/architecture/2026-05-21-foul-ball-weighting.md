# Count-Stratified Foul-Ball Weighting (Phase 3/4)

*Ticket: SIM-056 · Owner: Baseball Analyst · Date: 2026-05-21*
*Status: IMPLEMENTED in SIM-318 (was PROPOSED — design only). Plugs into the SIM-300
play-pool sampling contract (`docs/architecture/2026-05-20-play-pool.md`) and the
sim-loop outcome-determination step (`docs/perf/2026-06-03-sim-loop-time-budget.md`
§2 step 4). Consumes `sim.pitch_pool.outcome_type='foul'` /
`count_balls` / `count_strikes` (`db/schemas/02_duckdb_schema.sql`).*

---

## 0. Purpose

The play-pool sampler (SIM-302) draws a historical pitch outcome
(`ball | called_strike | swinging_strike | foul | in_play`) per pitch, pre-filtered
only by `pitcher_id + bat_hand` (SIM-300 §3). It does **not** condition the draw on
the live ball-strike count. For most outcomes that is fine — the count is a weak
covariate of pitch *geometry*, which is what the 10-dim fingerprint already captures.

It is **not** fine for fouls, because a foul is the one pitch outcome whose
*consequence depends on the count it is thrown in*:

- On a **non-two-strike** count, a foul is just another strike (it advances
  `count_strikes` by 1, up to 2). It is functionally a strike for state purposes.
- On a **two-strike** count, a foul is an **absorbing, count-neutral** event: the
  at-bat does **not** end and `count_strikes` does **not** advance. The batter can
  foul off an unbounded number of two-strike pitches, extending the PA.

If the simulator samples and applies fouls without conditioning on the count, two
failure modes follow:

1. **Two-strike PAs end too quickly.** A pool sampled in proportion to all-count
   foul frequency under-represents the foul-off behavior that *only happens* with
   two strikes, so two-strike at-bats terminate (K or contact) faster than reality.
2. **Pitch-count / PA-length distributions are wrong.** Mean pitches-per-PA is
   driven heavily by two-strike foul-offs (the long 8–12 pitch at-bat is almost
   entirely two-strike fouls). Mis-modeling them biases pitch counts, which cascade
   into starter-fatigue / bullpen-usage logic (SIM-303 / manager engine) and the
   game-length SLA assumptions.

SIM-056 specifies the **count-stratified foul-frequency weighting** that fixes this:
a count → foul-frequency table (§2), how the sampler / loop applies it (§3), the
non-incrementing two-strike-foul loop rule (§3.3), and a validation plan (§4).

---

## 1. The baseball reality (with sourcing)

### 1.1 Foul rate rises sharply with two strikes

This is one of the most robust, repeatedly-confirmed count effects in the public
pitch-by-pitch literature. The mechanism is behavioral, not stochastic:

- With **two strikes**, the batter switches to a **protective ("two-strike
  approach") swing** — shorter, defensive, aimed at making *any* contact to avoid
  the strikeout. The explicit intent is often to **foul off** a borderline pitch
  rather than take it for strike three. This raises both the swing rate on
  close pitches and the foul share *of* those swings.
- On **non-two-strike** counts the batter swings to do damage; a foul there is an
  incidental mishit, and (crucially) it is **bounded** — it can only push the count
  to at most two strikes, after which further fouls become count-neutral.

The net empirical pattern, consistent across sources, is that the **foul share of
pitches (and of swings) is markedly higher in two-strike counts than in
non-two-strike counts** — commonly described as a step-up of roughly +50% or more
in the per-swing foul rate once the second strike is in.

### 1.2 Sourcing (Baseball Analyst references)

The kinds of sources that establish this, and that the calibration in §4 should be
re-pulled against rather than relying on the placeholders below:

- **Statcast / Baseball Savant pitch-level data** — the primary source. The
  `description` field (`foul`, `foul_tip`, `swinging_strike`, `called_strike`,
  `ball`, `hit_into_play`, …) cross-tabbed by the pre-pitch `balls`/`strikes` count
  yields the exact foul-per-pitch and foul-per-swing rates by count. This is the
  authoritative table the simulator should ultimately match (it is also the source
  the ETL builds `sim.pitch_pool.outcome_type` from, so the sim and the reference
  share a definition of "foul").
- **FanGraphs** plate-discipline and count-split leaderboards (Foul%, Contact%,
  Swing% by count) corroborate the same direction at the league/player level.
- **Tom Tango / "The Book" (Tango, Lichtman, Dolphin)** and the broader
  count-leverage literature — the canonical treatment of how batter approach and
  outcome distributions shift by count, including the two-strike protective swing.

### 1.3 Representative figures — **ILLUSTRATIVE, not live data**

The numbers below (and in `docs/data/foul_rate_by_count.csv`) are
**analyst-plausible placeholders** chosen to encode the *direction and rough
magnitude* of the two-strike step-up. They are **NOT pulled from a live Statcast
query** and MUST be replaced with measured league rates at calibration time (§4).
They are presented per *strike bucket* because the two-strike effect is driven by
`count_strikes`, with ball-count having a second-order effect:

| Strike bucket | foul / pitch (illus.) | foul / swing (illus.) | Note |
|---|---:|---:|---|
| 0 strikes | ~0.15 | ~0.33 | bounded: a foul here can only become strike 1 |
| 1 strike  | ~0.165 | ~0.345 | bounded: a foul here can only become strike 2 |
| **2 strikes** | **~0.235** | **~0.47** | **absorbing: foul is count-neutral, PA continues** |

The load-bearing fact for the design is the **ratio**, not the absolute level: the
two-strike per-swing foul rate runs on the order of **~1.4–1.6×** the
non-two-strike rate. The design is built so that only this *relative* shape matters
(§2.2), so swapping in the real numbers is a data change, not a logic change.

---

## 2. The count → foul-frequency table

### 2.1 The 12 counts

The simulator must hold a foul-frequency value for each of the 12 ball-strike
counts. Stored as `docs/data/foul_rate_by_count.csv` (illustrative) and loaded as a
`{(balls, strikes): factor}` map:

| count | strikes | two-strike? | `strikes_bucket_factor` (illus.) |
|---|---:|:--:|---:|
| 0-0, 1-0, 2-0, 3-0 | 0 | no | 1.00 |
| 0-1, 1-1, 2-1, 3-1 | 1 | no | 1.05 |
| 0-2, 1-2, 2-2, 3-2 | 2 | **yes** | **1.55** |

`strikes_bucket_factor` is the **count-conditional foul multiplier**: the ratio of
that count's foul-per-swing rate to the pool's pooled (all-count) foul-per-swing
rate. A factor of 1.55 at two strikes means "a foul is ~1.55× as likely, per swing,
in this count as the pool average." The CSV carries the underlying
`foul_per_pitch` / `foul_per_swing` columns so the factor can be recomputed from
measured data; the simulator only needs the factor.

**Granularity decision.** The table is keyed by **`count_strikes` bucket** (0/1/2),
not by all 12 counts independently, because (a) the dominant, behaviorally-grounded
signal is the two-strike step (§1.1), (b) per-(balls,strikes) foul rates are noisy
once split 12 ways, and (c) it keeps the absorbing-state rule (§3.3) and the
re-weighting (§3.2) trivially aligned with `count_strikes`. The CSV is still written
out for all 12 counts (with equal factors within a strike bucket) so a future
calibration can refine to full per-count factors **without any code change** — the
loader and the math already key on `(balls, strikes)`.

### 2.2 Why a *relative* factor, not an absolute probability

The sampler does not assign outcome probabilities from a model — it **samples
empirically** from the historical pool (SIM-300 §0). So SIM-056 must not *invent* a
foul probability; it must **re-shape the empirical foul mass the sampler already
draws** so that its count distribution matches reality. Expressing the table as a
multiplicative factor relative to the pool's own pooled foul rate means:

- the **base level stays empirical** (the pitcher's / matchup's real foul tendency
  is preserved by the pool), and
- only the **count-conditional tilt** is injected, which is exactly the piece the
  unconditioned pool is missing.

This mirrors how `recency_weight` (SIM-076) is a *multiplier* on an
otherwise-empirical draw rather than a replacement for it.

---

## 3. Integration point

Two changes, at two clearly-separated layers. The **re-weighting** (3.1/3.2) makes
the *sampled mix* of fouls match the count; the **loop rule** (3.3) makes the
*state effect* of a sampled foul correct. Both are required — neither alone is
sufficient.

### 3.1 Where it plugs in

The sampler (SIM-302 `PlayPoolSampler.sample_pitch`) is intentionally count-blind
and **stays distance-pure** (it owns only distance→weight; HANDOFF §4 / SIM-300
§6.2). SIM-056 therefore does **not** modify the FAISS k-NN or the tile pre-filter.
The count-conditional tilt is applied at **sim-loop step 4 ("Outcome
determination")** — the step that already classifies the sampled pitch result and
advances the count (`docs/perf/2026-06-03-sim-loop-time-budget.md` §2). This is the
natural seam: it is the one place that both (a) knows the live count and (b) is
about to act on the sampled outcome.

Concretely the loop, on the result of `sample_pitch(...)`, applies a
**count-conditional foul acceptance / re-weight** before committing the outcome:

```
outcome ← sampler.sample_pitch(pitcher_id, bat_hand, season, fp, k)
outcome ← apply_count_foul_weighting(outcome, count_balls, count_strikes)
advance_count(outcome, count_balls, count_strikes)   # §3.3 two-strike-foul rule
```

### 3.2 The re-weighting mechanism (two equivalent options)

Either of the following implements §2's factor; the loop owns the choice. Both are
in-memory arithmetic on an already-drawn k-NN neighbourhood, so both fit inside the
step-4 budget (~10 µs/pitch, time-budget §3) with no extra FAISS work.

- **Option A — distribution re-weight (preferred for two-strike counts).** Request
  the neighbourhood as a distribution (`sample_pitch` exposes the same k neighbours
  the draw uses; cf. `sample_batted_ball(return_distribution=True)`, SIM-300 §6.2),
  multiply the `foul` bucket's mass by `strikes_bucket_factor` for the live count,
  renormalize the closed outcome vocabulary to sum to 1.0, then draw once from the
  re-weighted distribution. This shifts probability **into** `foul` at two strikes
  and (via renormalization) **out of** the terminal `swinging_strike` / `in_play`
  buckets — exactly the real-world effect (more foul-offs ⇒ fewer strike-threes and
  fewer balls-in-play per swing decision).

- **Option B — accept/resample on the single draw (lightweight).** If the single
  draw is a `foul`, accept it with probability `min(1, strikes_bucket_factor)` when
  the factor ≥ 1, or with probability `strikes_bucket_factor` when < 1; on rejection,
  redraw once from the same neighbourhood excluding the foul mass. Cheaper but
  coarser; acceptable where the full distribution isn't needed.

In both options the factor is `1.0` for non-two-strike counts under the §2.1
placeholders (no tilt where the empirical pool is already adequate), so the
mechanism is a **no-op outside two-strike counts** until/unless calibration assigns
non-unit factors to 0-/1-strike counts.

### 3.3 The loop rule: a two-strike foul does NOT increment strikes

This is independent of the re-weighting and is **non-negotiable** for correct PA
length. In `advance_count`:

```
if outcome == "foul":
    if count_strikes < 2:
        count_strikes += 1          # foul is an ordinary strike (up to 2)
    else:
        pass                        # TWO-STRIKE FOUL: count unchanged, PA continues
elif outcome in ("called_strike", "swinging_strike"):
    count_strikes += 1              # may terminate the PA (strike 3 -> strikeout)
elif outcome == "ball":
    count_balls += 1               # may terminate the PA (ball 4 -> walk)
# in_play -> PA ends via contact resolution (steps 5-7)
```

The two-strike branch is the absorbing behavior: the PA stays alive, the count is
unchanged, and the loop draws the next pitch. (Edge cases the loop must also honor,
out of SIM-056 scope but noted: a `foul_tip` caught by the catcher with two strikes
is a strikeout, not a foul, and a foul **bunt** with two strikes *is* strike three —
both must be encoded in `outcome_type` upstream by the ETL, not inferred here.)

### 3.4 What is explicitly NOT changed

- **FAISS tiles / pre-filter / index type** — untouched (SIM-300 §3, SIM-114 §4
  stand). No new tile dimension for count.
- **`sample_pitch` distance→weight** — untouched; the sampler stays count-blind and
  distance-pure. SIM-056 lives entirely in the loop's step 4.
- **`recency_weight`** — orthogonal; the count factor multiplies *after* the
  recency-weighted empirical draw, it does not replace it.

---

## 4. Validation plan

Calibration target: the simulator's *emergent* foul behavior matches the league
reference within tolerance, using the **measured** Statcast table (§1.2), not the
§1.3 placeholders.

### 4.1 Metrics

1. **Simulated foul rate by count** vs the reference foul-per-swing-by-count table.
   Computed from a large simulated sample (≥ 50k simulated PAs, all counts
   represented), bucketed by `count_strikes` (and, once calibrated to 12 counts, by
   full `(balls, strikes)`).
2. **Mean pitches-per-PA**, overall and split by whether the PA reached two strikes.
   This is the headline integration metric — it is the quantity the two-strike foul
   rule most directly drives.
3. **Two-strike PA-length distribution** (share of PAs lasting 5, 6, 7, 8+ pitches),
   compared to the league distribution. The tail (8+ pitch PAs) is almost entirely
   two-strike fouls and is the most sensitive check that §3.3 fires correctly.

### 4.2 Tolerances

| Metric | Reference | Tolerance |
|---|---|---|
| Foul-per-swing rate, **two-strike** | Statcast league two-strike foul/swing | within **±2 percentage points** |
| Foul-per-swing rate, **non-two-strike** | Statcast league non-two-strike foul/swing | within **±2 percentage points** |
| Two-strike / non-two-strike foul-rate **ratio** | Statcast league ratio | within **±0.10** of the measured ratio |
| **Mean pitches per PA** (overall) | League ≈ 3.8–3.9 P/PA (re-pull at calibration) | within **±0.15 pitches** |
| Mean pitches per **two-strike** PA | League two-strike-reaching P/PA | within **±0.25 pitches** |

### 4.3 Procedure

1. Pull the measured Statcast foul-per-swing-by-count table and league P/PA for the
   reference seasons; write the real numbers into `docs/data/foul_rate_by_count.csv`
   (replacing the illustrative values) and recompute `strikes_bucket_factor` as
   each count's foul/swing ÷ the pooled foul/swing.
2. Run the simulator over the calibration sample with the table loaded.
3. Compare the §4.1 metrics against §4.2. If two-strike P/PA is **low**, the
   `strikes_bucket_factor` is too small or the §3.3 rule is mis-firing (check it
   first — a single bug there dwarfs any factor mis-tune); if **high**, the factor
   is too large.
4. Iterate the two-strike factor only (it is the single dominant knob); the
   non-two-strike factors stay at 1.0 unless §4.2's non-two-strike foul-rate check
   fails, which would indicate the empirical pool itself is mis-weighted (a SIM-076
   recency or pool-build issue, not SIM-056).

### 4.4 Regression guard

A lightweight test (sim-loop level, synthetic pool) should lock the two invariants
SIM-056 introduces, independent of calibration data:

- a sampled `foul` with `count_strikes == 2` leaves `count_strikes` unchanged and
  does **not** terminate the PA (the §3.3 absorbing rule); and
- with a `strikes_bucket_factor > 1` at two strikes, the re-weighted foul mass is
  strictly greater than the unweighted foul mass for the same neighbourhood (the
  §3.2 tilt has the correct sign).

---

## 5. Acceptance trace

| SIM-056 requirement | Where satisfied |
|---|---|
| Baseball reality + sourcing (Statcast/FanGraphs/Tango), illustrative figures clearly labeled | §1 (§1.2 sources, §1.3 marked ILLUSTRATIVE) |
| Count → foul-frequency table over the 12 counts; conditioned on `count_strikes`; two-strike absorbing behavior | §2, `docs/data/foul_rate_by_count.csv` |
| Integration point: sampler/loop applies the count-conditional foul frequency; re-weight vs pre-filter decision | §3.1–§3.2 (loop step 4 re-weight; sampler stays distance-pure) |
| Loop rule: a two-strike foul does not increment strikes | §3.3 |
| Validation: simulated foul-rate-by-count + mean pitches-per-PA match reference within stated tolerance | §4 |

---

*End of SIM-056 spec. Design only — no source/test changes.*
