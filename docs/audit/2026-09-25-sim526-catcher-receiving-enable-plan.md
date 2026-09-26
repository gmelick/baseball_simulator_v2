# Build plan — turn on the catcher pitch-receiving factor (SIM-526)

> **STATUS 2026-09-25 — PROPOSED, awaiting four owner decisions (§10).** Nothing is
> built. The readable page is https://claude.ai/artifact/EYY2S5MMc1g5Ecaad9akvB (the same content as this file).
> Two builder constants, one draw-time knob, the switch flipped in the compose file and the
> lane's flag table, one document rebuild (seconds, the app stays up), one app recreate,
> the probe and one 45 × 130 lane. No pool rebuild, no profile recompute, no matrix.
>
> **The factor is built right and shrunk wrong.** The receiving ratio moves ball-or-strike
> within a catcher's taken pitches by his own called-strike rate over the league's, and it
> leaves the league's totals alone by construction (the pitch-weighted mean multiplier reads
> 1.000 to 1.001 in every season). But its shrinkage trusts a catcher's rate five times too
> much: the document pulls a rate toward the league with 200 taken pitches of prior weight,
> and the split-half reliability of the outside-zone rate says 1,000 to 1,500 is the weight
> where half of what you see is skill. The blocking ratio's prior of five expected got-aways
> should be about twenty. With the fitted priors, a regular's outside multiplier runs 0.86 to
> 1.19 (5th to 95th percentile) instead of 0.82 to 1.23, and a part-timer's near 1. About
> three quarters of the outside signal is the catcher and a quarter his staff's command;
> the location mix inside a zone group is not a confound (a fine-location model keeps 95%
> of the raw signal). The profile's own framing block, which the catcher engine still reads,
> is not per season and is inflated by the league's 2025 shift. I recommend landing the
> ratio ON at the measured strength (power 1, a sweep knob added), with the fitted priors.

**Date:** 2026-09-25
**Ticket:** SIM-526 (P2) in `BACKLOG.xlsx`. Next free ID: SIM-556.
**Evidence:** the ratio as it stands at commit a769041 (`simulation/full_pool_sampler.py`
`_recv_factor` / `_apply_receiving`, `pipeline/batch/engine_artifacts.py`
`build_receiving_profiles`, `simulation/production_factory.py`); the live document
`receiving.json` (built 2026-09-09, 422 catcher-seasons); the pitch pool's 1,440,733 taken
pitches of 2023 to 2026 with a catcher and a zone (DuckDB, read-only); the profile's framing
block (`derived.catcher_season_metrics`); the part-E probe of 2026-09-09 and a 150-iteration
re-run of its OFF / ON pair (`scripts/sim523_receiving_probe.py`, this session).
**Builds on:** the play-picker redesign's part E (SIM-523, `CHANGES.md` 2026-09-09; the plan
`docs/audit/2026-09-08-sim523-play-picker-redesign-plan.md` §Part E), which built the ratio
OFF and named this ticket for its fit and enable; the powers-to-1 ruling of 2026-09-16 and the
"lands ON at its best-known default" clause (`CLAUDE.md` §2b).

---

## 0. The short version

**What the ticket asks for.** The catcher pitch-receiving factor has a chosen strength, is
switched on in production, and the platform's accuracy checks still pass.

**What the code and the data say.**

- The ratio is a mass-preserving reweighting of a catcher's TAKEN pitches: a called-strike
  row × his framing multiplier at the row's zone group (his called-strike rate over the
  league's, per season), a ball row × the mirror, a got-away row × his blocking ratio; then
  the taken group is rescaled so its total weight is unchanged. It never touches the
  swing-or-take split. With the split ON (production) it weights the result draw only, last.
- It reproduces the catcher's own shrunk rate exactly: within a zone group, the drawn
  called-strike share becomes L·m, the league rate times the multiplier. So "the strength"
  is the shrinkage of m, and the measured strength is the best-known default (the owner's
  rule of 2026-09-16); a power knob on m is the sweep's lever.
- The shrinkage is five times too light. Halving each catcher-season's pitches by game
  parity, the two halves' outside-zone rates correlate 0.53 to 0.69, which puts the prior
  weight where reliability is 0.5 at 1,000 to 1,500 taken pitches; the edge zone at about
  900; the blocking ratio's at 20 to 24 expected got-aways. The document uses 200 and 5. A
  regular (3,800 outside taken pitches) keeps 95% of his raw deviation under the document
  and should keep 76%; a 500-pitch part-timer keeps 71% and should keep 29%.
- The confounds: a fine-location model (0.25 ft bins, per season) keeps 95% of the raw
  outside signal, so the coarse zone groups are not the problem; a pitcher-adjusted rate
  (each pitcher's rate with his OTHER catchers as the baseline) keeps 73%, so about a
  quarter of the spread is the staff's command. The pitcher-adjusted version repeats half
  as well year to year (0.22 against 0.45) and matches the profile's framing block worse
  (r 0.54 against 0.72), so the raw group multiplier stays, with the quarter recorded.
- The magnitude matches the profile's framing block in rank (r 0.72) and reads smaller in
  size (slope 0.53), because the profile's expected-strike model is not per season: its
  regulars' strikes above average average +34 and +42 in 2023 and 2024 and −66 in 2025,
  the year the league's outside called-strike rate fell from 7.3% to 4.5%. The document's
  per-season rates are the right ones; the profile's block is a finding for another day.
- The league level is untouched: the pitch-weighted mean multiplier is 1.0000 to 1.0010 in
  every season and group, so no pool band moves. Regulars with 3,000 or more taken pitches
  receive 79 to 87% of a season's taken pitches.
- The part-E probe read the real catchers neutral and the best and worst framers moving the
  called-strike share of taken pitches by +0.012 and −0.009, and left one read open: stolen
  bases 1.49 a game with the ratio ON against 1.19 OFF, with no mechanism (a stolen base is
  credited only by the steal resolution; the steal draw's hard filter is the target base
  and the exact count, so the ratio reaches it only through the count path). The
  150-iteration re-run reads it flat: 1.17 against 1.21 a game (§2.5).

**The plan.** Two builder constants (the framing prior 200 → 1,200 taken pitches, the
blocking prior 5 → 20 expected got-aways) and a rebuild of `receiving.json`; a draw-time
power on the multiplier and the blocking ratio (`SIM_CATCHER_RECEIVING_POWER`, 1 = the
measured ratio, 0 = neutral) for the sweep; the switch ON in the compose file and the lane's
flag table with a parity test; the probe, then one 45 × 130 lane with the ratio ON, expected
to read every band where the 2026-09-22 lane left it. Seven tests. Nothing else changes.

**Four decisions** in §10, with my recommendation on each.

---

## 1. The mechanism

**The document** (`build_receiving_profiles`, `--what receiving`, in the nightly `all`) is
built from the pitch pool itself. Per SEASON and zone group — the heart (zone 5), the
in-zone edge (zones 1 to 4 and 6 to 9), outside (zones 11 to 14) — the league called-strike
rate among taken pitches; per season and blocking cell (four pitch heights × in or out of
the zone) the league got-away rate. Per catcher-season: the framing multiplier per group,
`(cs + L·prior) / (n + prior) / L`, clamped so the ball rows' mirror stays non-negative; the
blocking ratio, `(got_aways + prior) / (expected + prior)`, got-aways above the expectation
of the pitches he received.

| Season | Heart | Edge | Outside | Taken per team-game |
|---|---:|---:|---:|---:|
| 2023 | 99.98% | 88.4% | 7.10% | 76.2 (all seasons) |
| 2024 | 99.97% | 88.3% | 7.26% | |
| 2025 | 99.99% | 86.1% | 4.54% | |
| 2026 | 100.0% | 92.1% | 4.07% | |

The heart holds 3.3% of taken pitches (all called strikes), the edge 27.1% (346,085 called
strikes), outside 69.5% (58,464 called strikes). The framing question lives in the outside
group and, less, the edge.

**The factor** (`_recv_factor`, cached per hand and catcher): on every taken row of the
hand's pool, a called strike × m at its group, a ball × (1 − L·m) / (1 − L) with L the row's
own season's league rate, a got-away × b, the other taken rows × b's mirror; swung-at,
hit-by-pitch and unknown-zone rows × 1. `_apply_receiving` rescales each candidate set's
taken group to its previous total weight. Within a group the drawn called-strike share is
therefore L·m: the catcher's own shrunk rate. The draw conditions on the pitcher first
(pitcher similarity, the cell), so a staff's command is already in the rows; the multiplier
is the catcher's rate RELATIVE to the league, applied on top.

**The switch and the loop.** `SIM_CATCHER_RECEIVING` ("0" in the compose file, "0" in the
unit lane, `SIM523_LANE_RECEIVING` or "0" in the acceptance lane's flag table). The loop
stages the catcher key `"{id}:{season}"` at each half inning (`sim_loop.py` 752 / 2648); a
key the document lacks is neutral. The got-away resolution (`SIM_GOT_AWAY=1`) is a separate
flag and stays as it is.

---

## 2. The evidence

### 2.1 The shrinkage: what the data says the prior should be

Each catcher-season's taken pitches were split by the parity of the game id; the two halves'
raw rates above the league were correlated. The prior weight where a rate's reliability is
0.5 is `n_half × (1 − r) / r`.

| Group | Halves with at least | Catcher-seasons | r(half, half) | Median half n | Implied prior | Full-season reliability |
|---|---:|---:|---:|---:|---:|---:|
| outside | 500 taken each | 271 | 0.530 | 1,636 | **1,452** | 0.69 |
| outside | 1,500 taken each | 131 | 0.686 | 2,258 | **1,036** | 0.81 |
| outside | 3,000 taken each | 13 | 0.611 | 3,390 | 2,155 | 0.76 |
| edge | 500 taken each | 151 | 0.476 | 842 | **925** | 0.65 |
| blocking (got-aways above expected) | 5 expected each | 216 | 0.354 | 11.0 | **20** | 0.52 |
| blocking | 10 expected each | 108 | 0.380 | 14.8 | **24** | 0.55 |
| blocking | 15 expected each | 40 | 0.473 | 18.2 | **20** | 0.64 |

The document's priors are 200 taken pitches and 5 expected got-aways. What the change does
to the outside multiplier:

| Prior | Regulars (209, 3,000+ taken): sd, 5th–95th, min / max | All 422 catcher-seasons: 5th–95th |
|---|---|---|
| 200 (today) | 0.136, 0.817–1.233, 0.63 / 1.43 | 0.769–1.226 |
| 600 | 0.123, 0.836–1.217, 0.67 / 1.40 | 0.840–1.177 |
| **1,200** | **0.108, 0.860–1.193, 0.71 / 1.37** | **0.866–1.143** |
| 2,000 | 0.093, 0.881–1.174, 0.75 / 1.33 | 0.888–1.120 |

The 2023 best framer the probe used reads ×1.421 today and ×1.30 at 1,200; the multipliers
the part-E entry quoted (0.79 to 1.24) become 0.86 to 1.19.

### 2.2 The confounds: location and staff

For the 209 regular catcher-seasons, the raw outside rate above the league (sd 0.0084, 5th to
95th −0.012 to +0.016) against two adjusted versions:

| Adjusted version | r with the raw rate | Slope of the adjusted on the raw | Year-to-year repeat (145 pairs) |
|---|---:|---:|---:|
| none (the raw rate) | — | 1.000 | 0.453 |
| the shrunk multiplier (prior 200) | — | — | 0.418 |
| location-adjusted (the league rate per season and 0.25 ft × 0.25 ft plate bin) | 0.860 | **0.947** | 0.439 |
| pitcher-adjusted (each pitcher's rate with his other catchers, leave-one-out; the league rate under 50 such pitches) | 0.656 | **0.728** | 0.215 |

The edge group: location 0.926, pitcher 0.841; repeats 0.43 / 0.50 / 0.24. The location mix
inside a group takes 5% of the signal; the staff takes 27%. The pitcher-adjusted rate is
also the noisier estimate (its baseline is the backup catcher's small sample) and repeats
half as well, so it is not the better predictor of next season's rate. The staff share is
recorded, not removed (§10, decision 3).

### 2.3 The magnitude against the profile's framing block

`derived.catcher_season_metrics` carries `strikes_above_average` from a logistic model on
location, count and hands. On the 209 regulars:

| Measure of extra called strikes per catcher-season | sd | 5th–95th | r with the profile | slope on the profile |
|---|---:|---:|---:|---:|
| the profile's `strikes_above_average` | 65.2 | −106 to +120 | — | — |
| the document's, shrunk at 200 (Σ (m − 1)·L·n over the groups) | 47.9 | −70 to +80 | 0.720 | 0.529 |
| location-adjusted, unshrunk | 54.0 | | 0.743 | 0.616 |
| pitcher-adjusted, unshrunk | 54.2 | | 0.539 | 0.448 |

`framing_runs` is 0.1224 runs per document strike (the profile's own 0.125 constant). The
profile's larger spread is not skill the document misses: its regulars' strikes above
average average **+33.9 (2023), +42.0 (2024), −66.4 (2025), −0.6 (2026)** — the expected
model is fitted across seasons, so 2025's league shift (the outside called-strike rate
7.3% → 4.5%, the profile's own shadow-zone rate 0.155 → 0.121) reads as every 2025 catcher
framing badly. The document's per-season rates were built to avoid exactly that. The
document's raw size — about +80 called strikes a season at the 95th percentile, +10 runs at
0.125 a strike — is the size of a top-five framing season on Savant's published board as I
remember it; I did not load that board, and nothing here depends on it.

### 2.4 The league level and the coverage

Pitch-weighted over each season's taken pitches, the mean multiplier is 1.0000 to 1.0004
(edge) and 1.0005 to 1.0010 (outside): the ratio redistributes called strikes between
catchers and adds none. Regulars with 3,000 or more taken pitches receive 86.8% (2023), 83.4%,
82.4% and 79.4% (2026) of a season's taken pitches; the rest goes to part-timers, whose
multipliers at the fitted prior sit near 1. The blocking ratio's year-to-year repeat is 0.38
(132 pairs with 10+ expected got-aways each; 5th to 95th 0.68 to 1.35 at the prior of 5).

### 2.5 The probe, and the open stolen-base read

The part-E probe (four games, one per pool season, 60 iterations per arm, the manager and the
cell index on) read: OFF — taken share 0.521, called strikes among taken 0.309, pitches per
plate appearance 3.97, walks 6.98 a game, strikeouts 16.6, runs 9.07, stolen bases 1.19; ON
with the real catchers 0.520 / 0.307 / 3.97 / 6.97 / 16.3 / 8.97 / **1.49**; the best framer
ON against OFF +0.012 on the called-strike share and −0.81 walks; the worst −0.009 and +0.10.
The taken share and the pitches per plate appearance held (the invariant). The stolen-base
read (2.7 standard errors) had no mechanism: `_resolve_steal_outcome` is the only site that
credits a stolen base (`sim_loop.py` 1506); a got-away advance credits none; the steal draw's
hard filter is the target base and the exact (outs, balls, strikes) cell, so the ratio can
reach it only by moving the count distribution, which it did not (0.309 → 0.307).

**The re-run (this session, the production configuration, OFF against ON with the real
catchers, 150 iterations on the same four games — 600 games an arm):** OFF — taken share
0.5204, called strikes among taken 0.3099, pitches per plate appearance 3.897, walks 6.63 a
game, strikeouts 16.93, runs 8.98, **stolen bases 1.17**, caught stealing 0.34; ON with the
real catchers 0.5215 / 0.3108 / 3.889 / 6.59 / 17.05 / 8.96 / **1.21** / 0.36; 1.0 s an
iteration at the production configuration. The stolen-base gap is 0.04 a game against a
standard error of about 0.07 on the difference: the 2026-09-09 read was sampling noise, and
no mechanism is missing. Every other column holds within its noise (the called-strike share
+0.0009, walks −0.04, pitches per plate appearance −0.008). The re-run used the live
document (prior 200); the shrinkage question does not touch this read.

### 2.6 What the rebuilt pool changed

The pitch pool was rebuilt on 2026-09-25 for the foul-tip fix (SIM-553) and still carries
`zone`, `got_away` and `catcher_id`; the document is dated 2026-09-09. A foul tip is a swing,
so the taken rows the document reads are unchanged; the rebuild in the run book is for the
new priors, not the pool.

---

## 3. Findings that shape the plan

1. **The strength of the factor is its shrinkage, and the shrinkage is measurable.** At
   power 1 the draw reproduces the catcher's shrunk rate. The split-half test puts the
   framing prior at 1,000 to 1,500 taken pitches (1,200 chosen; the edge group's 925 is
   inside the same band) and the blocking prior at about 20 expected got-aways. These are
   data facts about reliability, not tuning weights, so they land as the builder's
   constants with the evidence beside them.
2. **A power knob is the sweep's lever, not a fitted value today.** `m^p` and `b^p` with
   p = 1 by default; p = 0 is neutral and byte-identical to OFF. The pool-conditional fit
   returns 1 by construction and the accuracy comparison cannot resolve a factor this size
   (the 2026-09-16 ruling), so the knob's only job is to join the designed experiment.
3. **The zone groups are fine as they are.** A fine-location model keeps 95% of the raw
   signal; the multiplier concentrates its effect where called strikes exist (a far-outside
   ball row's mirror is 1 − L(m − 1) ≈ 0.99) without a shadow-zone definition.
4. **A quarter of the outside signal is the staff, and the cure is worse.** The
   pitcher-adjusted rate repeats half as well and matches the profile worse. The draw
   already conditions on the pitcher, so the double-count is real but small: the staff
   share of a ±0.016 rate is ±0.004, under half a called strike a game at the extremes.
5. **The profile's framing block is not per season** (found on the way). The catcher engine
   still reads `strikes_above_average` and `framing_runs`; in 2025 they call every regular
   a poor framer. Not this ticket's; not filed — say the word and it gets a row. The catcher
   table's `asof_date` is also NULL on every row, which the one-cutoff guard accepts as one
   value.
6. **The document is season-level, like every actor profile.** The backtest's cutoff masks
   pool rows after the game's date but the document (and the batter, fielder and runner
   matrices) hold the whole season. A shared limitation, recorded in §9, not this ticket's.

---

## 4. Data changes

None in Postgres or DuckDB. One bundle file changes: `receiving.json` is rebuilt with the
new priors (`frame_prior` 1200, `block_prior` 20 in the document; the same 422 catcher-seasons;
the multipliers shrink as §2.1). The builder reads DuckDB read-only, so the app stays up.
The compose file gains `SIM_CATCHER_RECEIVING_POWER: "1"` and flips
`SIM_CATCHER_RECEIVING` to `"1"`.

---

## 5. Code changes, file by file

### 5.1 `pipeline/batch/engine_artifacts.py` — the priors

```python
# SIM-526 (2026-09-25): the priors are the split-half reliabilities of the pool itself. A
# catcher-season's outside called-strike rate above the league correlates 0.53–0.69 between
# its two game-parity halves (median half 1,600–2,300 taken pitches), which puts the weight
# where half of a rate is skill at 1,000–1,500 taken pitches; the edge group at ~900; the
# blocking ratio (got-aways above expectation) at 20–24 expected got-aways. The old values
# (200 / 5) kept 95% of a regular's raw deviation where the data says 76%.
RECV_FRAME_PRIOR = 1200.0   # was 200.0
RECV_BLOCK_PRIOR = 20.0     # was 5.0
```

The builder and the document's `frame_prior` / `block_prior` fields need no other change.

### 5.2 `simulation/full_pool_sampler.py` — the power

```python
# in __init__, beside catcher_receiving
self.catcher_receiving_power = 1.0   # SIM-526: m^p and b^p; 1 = the measured ratio; 0 = neutral (== OFF)

def _receiving_on(self) -> bool:
    return bool(self.catcher_receiving) and self._catcher_key is not None and float(self.catcher_receiving_power) != 0.0

# _recv_factor: the cache key carries the power; the multiplier and the blocking ratio are raised before the mirrors
key = (hand, catcher_key, float(self.catcher_receiving_power))
p = float(self.catcher_receiving_power)
mg = np.array([1.0] + [float(frame.get(name, 1.0)) ** p for name in groups])     # m^p per group
...
bf = float(b) ** p                                                                # b^p
```

The mirrors `(1 − L·m^p) / (1 − L)` and `(1 − G·b^p) / (1 − G)` follow from the powered
values, so the taken group's mass is preserved at every power and the clamp at 0 still holds.

### 5.3 `simulation/production_factory.py` — the env

```python
# apply_result_split_env: after the switch
sampler.catcher_receiving_power = float(src.get("SIM_CATCHER_RECEIVING_POWER", "1.0"))
```

The switch's code default stays "0" (the unit lane and every no-DB test are byte-identical);
the compose file and the lane's flag table carry the production value, the SIM-427 / SIM-518
pattern.

### 5.4 `docker-compose.yml`, the lanes, the parity test

```yaml
# SIM-526 (2026-09-25, owner decision): ON at the measured ratio, the priors fitted to the pool's
# split-half reliability (1,200 taken pitches / 20 expected got-aways). The power is the sweep's knob.
SIM_CATCHER_RECEIVING:        "1"
SIM_CATCHER_RECEIVING_POWER:  "1"
```

```python
# tests/acceptance/conftest.py PRODUCTION_FLAGS
"SIM_CATCHER_RECEIVING": os.environ.get("SIM523_LANE_RECEIVING", "1"),            # was "0"
"SIM_CATCHER_RECEIVING_POWER": os.environ.get("SIM526_LANE_RECEIVING_POWER", "1"),
# tests/conftest.py (the unit lane): SIM_CATCHER_RECEIVING stays "0"; SIM_CATCHER_RECEIVING_POWER pinned "1"
# tests/acceptance/test_band_arithmetic_sim450.py: test_the_compose_file_carries_the_same_receiving_values_sim526 (both keys)
```

### 5.5 Documents

The sim-loop cheat sheet (`docs/technical/sim-loop-cheat-sheet.md`: the ⑤b row gains "× the
catcher receiving ratio at power 1 (`SIM_CATCHER_RECEIVING_POWER`; the framing multiplier per
zone group and the blocking ratio, shrunk with 1,200 taken pitches / 20 expected got-aways)";
the two "built and OFF" rows drop it; the summary table gains its row);
`docs/technical/simulation.md` (line 54) and `docs/technical/pipeline-betting-db.md` (line
294) one line each; the sweep table of `docs/audit/2026-09-14-sim548-joint-fit-plan.md` §6
(the row "catcher receiving ratio `SIM_CATCHER_RECEIVING` | OFF | built; enable = SIM-526 |
OFF only here" becomes "catcher receiving power `SIM_CATCHER_RECEIVING_POWER` | 1 | the
measured ratio at the fitted priors (SIM-526) | 0, 0.5, 1, 2 | walks, strikeouts");
`CHANGES.md`; the SIM-526 row deleted from `BACKLOG.xlsx` on close.

---

## 6. Weights, bandwidth, power

One weight lands, at its best-known default: the receiving ratio at power 1, the measured
multiplier shrunk at the fitted priors. Nothing is fitted against an accuracy read (the
2026-09-16 ruling); the power joins the sweep's parameter table. No other weight, bandwidth
or power changes. Every similarity power stays 1.

---

## 7. Tests

| Test | Holds |
|---|---|
| `test_priors_are_the_fitted_values` (`tests/unit/test_sim523_receiving_ratio.py`) | `RECV_FRAME_PRIOR == 1200.0`, `RECV_BLOCK_PRIOR == 20.0`, with §2.1's evidence in the docstring; the document written by the builder carries both |
| `test_rates_priors_and_clamps` (existing) | unchanged in logic — it reads the constants symbolically; its comments name the new values (a 300-pitch catcher with no strikes now reads ×0.80, not ×0.40; the ten-for-ten catcher ×1.04, not ×1.25) |
| `test_power_zero_is_neutral` | `catcher_receiving_power = 0` draws the same sequence as OFF for any catcher, on the whole-pool path, the cell path and the result draw |
| `test_power_squares_the_ratio` | at power 2 the drawn called-strike share among taken pitches for catcher 700 matches L·m² within the binomial; the taken share and every swung-at weight unchanged (the invariant at every power) |
| `test_power_env` | `apply_result_split_env` reads `SIM_CATCHER_RECEIVING_POWER` (default 1.0; "0" → 0.0; "2" → 2.0); the switch's default stays False in an empty env |
| `test_the_cache_keys_the_power` | one sampler asked at two powers returns two factors, not one |
| `test_the_compose_file_carries_the_same_receiving_values_sim526` (`tests/acceptance/test_band_arithmetic_sim450.py`) | the compose file and `PRODUCTION_FLAGS` agree on both keys; the lane's switch defaults to "1" |

The existing thirteen tests stay (`test_off_is_byte_identical`, the mass invariant, the
split's placement, the deleted kernel). The unit lane pins the switch OFF, so nothing there
draws differently.

---

## 8. Run book

```text
# 1. the code lands; ruff, mypy; the unit lane (test_sim523_receiving_ratio, test_band_arithmetic_sim450); no engine change, no regression lane
# 2. rebuild the document (DuckDB read-only — the app stays up; seconds):
docker compose run --rm app python -m pipeline.batch.engine_artifacts --what receiving
#    check the log line: 422 catcher-seasons; the outside multipliers of the regulars 0.86..1.19 (5th–95th); frame_prior 1200 / block_prior 20 in the file
# 3. the flip: docker compose up -d app   (recreates the container with SIM_CATCHER_RECEIVING=1 and the power; the bundle reloads the new document)
# 4. the probe (four games, six arms, 60 iterations):
MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "C:/Users/grego/Documents/baseball_simulator_v2/scripts:/app/scripts" app \
    python scripts/sim523_receiving_probe.py --iters 60 --arms "OFF,ON real,OFF best framer,ON best framer,OFF worst framer,ON worst framer" 717809 744795 777557 825108
#    expected: the taken share and pitches per PA unchanged (the invariant); the best framer's ratio +0.008 on the called-strike share of taken pitches
#    (was +0.012 at prior 200), the worst −0.006; the real catchers within 0.003; stolen bases OFF and ON real within two standard errors (§2.5)
# 5. the lane (45 × 130, ~1.5 h; the ratio ON by the flag table's new default):
MSYS_NO_PATHCONV=1 docker compose run --rm -v ".../tests:/app/tests" -v ".../pyproject.toml:/app/pyproject.toml" -v ".../docker-compose.yml:/app/docker-compose.yml" app pytest tests/acceptance -v -rxX
#    expected: the sixteen pool bands where the 2026-09-22 lane left them — walks, hit by pitch, the steal attempts at second and third and doubles RED
#    (pre-existing; the ratio cannot reach them at the league level), the other eleven PASS; CALLED_STRIKE_TAKEN and GOT_AWAY_PITCH inside their floors (2% / 10%);
#    runs 4.5–4.6 a team-game. A band that moves beyond its floor between the two lanes is a finding, not a pass.
# 6. close: CHANGES.md; the SIM-526 row deleted; the cheat sheet, the two technical docs, the sweep table's row
```

---

## 9. What could go wrong (ranked)

1. **The lane reads a band moved.** The invariant says the league level cannot move, but
   the count path can: more called strikes for a good framer reach two-strike counts where
   batters swing, and the probe measured half a point on the live taken share over a game.
   Weighted over all catchers that nets to zero; a lane read beyond a floor says otherwise
   and stops the close.
2. **The stolen-base read — closed.** The 150-iteration pair (§2.5) reads 1.17 against 1.21
   a game; the 2026-09-09 gap of +0.30 was sampling noise. The probe in the run book reads
   it once more at the new priors.
3. **The priors are one number for two seasons' worth of league shifts.** The split-half
   implied prior ranged 925 to 2,155 across thresholds; 1,200 is inside the band, not the
   centre of a narrow one. A wrong prior by a factor of two moves a regular's kept share
   between 0.61 and 0.86 — a smaller error than today's 0.95.
4. **The document is not point-in-time.** In a 2024 backtest the catcher's 2024 multiplier
   holds the whole 2024 season, as every actor profile does. The framing effect is small
   (±0.4 called strikes a team-game at the extremes), so the leak is small; it is shared
   with the matrices and belongs to a point-in-time ticket, not this one.
5. **A catcher the document lacks is neutral**, by design: a call-up's first games, or a
   season outside the pool window. The probe's first lesson (a 2019 game read half the
   effect) stands; the lane's 45 games are 2026 games with 2026 catchers.
6. **The staff share.** A quarter of a catcher's outside multiplier is his staff's command,
   and the draw already conditions on the pitcher. At the extremes that is ±0.1 called
   strikes a team-game double-counted. Recorded; the pitcher-adjusted alternative is worse.

---

## 10. Decisions for the owner

1. **The shrinkage priors.** Recommended: the fitted values — 1,200 taken pitches on the
   framing multiplier and 20 expected got-aways on the blocking ratio (§2.1) — as the
   builder's constants, with the split-half evidence beside them. Alternatives: keep the
   built values (200 / 5), which trust a regular's rate at 95% where the data says 76% and a
   part-timer's at 71% where it says 29%; or fit the prior inside the builder from the
   split-half at every rebuild (a moving constant for a stable fact).
2. **The strength.** Recommended: ON at the measured ratio (power 1), with
   `SIM_CATCHER_RECEIVING_POWER` as the sweep's knob (levels 0, 0.5, 1, 2 in the design
   table). At power 1 the draw reproduces the catcher's own shrunk rate, the best-known
   default under the 2026-09-16 ruling; the pool-conditional fit returns 1 by construction
   and the accuracy comparison cannot resolve a factor this size. Alternative: a switch
   only, no knob — the sweep then flips ON / OFF and cannot read a half-strength arm.
3. **The staff share of the multiplier.** Recommended: keep the raw group multiplier and
   record that about a quarter of the outside signal is the staff's command (§2.2).
   Alternative: build the multiplier pitcher-adjusted (each pitcher's rate with his other
   catchers as the baseline), which removes the staff share and half of the year-to-year
   repeat with it (0.22 against 0.45) and matches the profile's framing block worse (r 0.54
   against 0.72).
4. **The certification.** Recommended: the probe and one 45 × 130 lane with the ratio ON
   before the row closes (about 1.5 hours), read as "no band moves beyond its floor between
   the 2026-09-22 lane and this one" — the ticket's own definition of done, and the lane's
   flag table has carried the hook for it since 2026-09-09. Alternative: the ruling's cheap
   gates only (the unit lane and a ten-game smoke), with the ratio read at the next
   scheduled lane.

Recorded, not asked: the ratio weights the result draw only under the split (the plan's
ruling); the got-away resolution stays as it is; the profile's framing block's pooled-season
model and the catcher table's NULL cutoff stamp are findings for another day, not filed; the
document's season-level leak is shared with every actor profile; every similarity power stays
1 and the receiving power lands at 1.
