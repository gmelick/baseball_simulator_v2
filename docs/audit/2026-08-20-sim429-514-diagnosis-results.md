# SIM-429 / SIM-514 — the diagnosis results (2026-08-20)

**Read this with the plan it executes:** `docs/audit/2026-08-19-sim429-514-491-diagnosis-plan.md`.
This document reports what the plan's instruments measured. One sentence up front:
**the walk surplus is NOT a per-count sampling defect — the draw reproduces the
pool almost exactly; the surplus decomposes into manager IBB volume (~half),
a structural conditional-independence artifact (~a third), and a small pool-era
component — and the same structure explains the K deficit, while DP and 3B are
confirmed as traffic/era effects with clean machinery.**

## The instruments (all new, all reusable)

| Script | What it measures |
|---|---|
| `scripts/sim429_count_diagnosis.py` | Per-count outcome rates (sim raw / sim final / pool unweighted / pool recency / MLB-2025), count visitation, walk-per-count-path. Wraps `_apply_framing` per machine. |
| `scripts/sim429_chain_analysis.py` | Solves the count-machine Markov chain for each rate matrix → implied BB/PA, K/PA, pitches/PA. |
| `scripts/sim429_ibb_probe.py` | Counts `_issue_intentional_walk` calls + box BB per iteration → sim IBB/team-game. |
| `scripts/sim514_decomposition.py` | DP per opportunity vs the pool's per-cell rate; SB attempts per opportunity; drawn triple rate per cell. Wraps `_full_pool_fielding` + `steal_draw`. |

Main run: 12 park-balanced games (`BALANCED_GAME_ORDER`) × 150 iters = 1,800
sims, 539,350 pitches, 136,878 PAs. MLB-2025 reference: 2,430 regular-season
Final games, 708,970 pitches, 182,647 PAs, from `raw.pitches` with the pool
builder's own outcome classification.

## SIM-429 — where the +11.7% walk surplus actually comes from

### 1. The per-count draw is CLEAN (branches 1 and 2 of the decision tree fail)

Per-count ball rates, sim vs the pool's own (recency-weighted), differ by
+0.16pp on the visitation-weighted mean — every one of the 12 counts is within
±0.5pp. Framing is slightly ball-REDUCING (−0.13pp). Count visitation matches
MLB within ±0.23pp per count. The kernels do not tilt ball-heavy; the count
machine does not mis-transition.

### 2. The Markov-chain read: the structure, not the rates

The sim draws each pitch independently given (matchup, count) — it IS a Markov
chain over counts. Solving that chain for each rate matrix:

| matrix | P(walk)/PA | P(K)/PA | pitches/PA |
|---|---|---|---|
| chain(sim final rates) | 0.0852 | 0.2164 | 3.935 |
| chain(pool recency) | 0.0849 | 0.2152 | 3.934 |
| chain(MLB-2025 rates) | 0.0839 | 0.2142 | 3.931 |
| **observed sim** | **0.0850** | **0.2160** | **3.940** |
| **observed MLB-2025** | **0.0813** | **0.2232** | **3.883** |

(Observed MLB rates are terminal-PA-only — 788 of 182,647 PAs have no terminal
event, matching the sim tracker's exclusion of its 818 truncated PAs.)

The chain on MLB'S OWN per-count rates produces 0.0839 walks and 0.2142
strikeouts per PA — but real MLB PAs walk 0.0813 and strike out 0.2232. Real
pitches within a PA correlate beyond the count (the same pitcher's command that
day, the same batter's approach); independence spreads terminal events toward
walks and away from strikeouts. The sim lands exactly where any
count-conditioned independent sampler must land.

**Decomposition of the sim's +4.6% BB/PA surplus (0.0850 vs 0.0813):**

| component | size | share |
|---|---|---|
| conditional-independence structure (chain(MLB) − obs(MLB)) | +0.0027 | ~71% |
| pool era 2024-26 vs 2025 (chain(pool) − chain(MLB)) | +0.0010 | ~26% |
| kernel tilt + framing (sim − chain(pool)) | +0.0001 | ~3% |

**The K deficit is the same artifact with the opposite sign:** sim K/PA 0.2160
vs MLB 0.2232 (−3.2%); the chain on MLB rates gives 0.2142 (−4.0% structural,
partly offset by era/tilt). This closes SIM-514(b): there is no strikeout
defect to tune — K moves when (and only when) the structure moves.

### 3. The IBB gap: the pitch tracker vs the lane box

The diagnosis tracker (which sees only PITCHED walks) reads 3.2331 BB/team-game.
The certified lane box (which counts `walk` + `intentional_walk`) read 3.5358.
Both runs use the same production env (`SIM_MANAGER=1`). The probe (12×50)
closes the reconciliation exactly:

    IBB/team-game     = 0.3233   (MLB 2025 = 0.1224 — the sim is 2.64x)
    box BB/team-game  = 3.5875   (reproduces the lane's 3.5358)
    pitched BB/tg     = 3.2642   (reproduces the tracker's 3.2331)
    box == pitched + IBB, no residual — IBB is the whole tracker-vs-lane gap.

**The mechanism:** `_should_issue_ibb` (sim_loop.py:3670) is a hand-tuned
probability formula — `platoon_advantage_exploitation_rate × leverage` vs an
rng roll — that fires near-certainly in every textbook spot. This predates and
violates the owner's 2026-08-10 architecture rule (every decision is a
similarity-weighted draw from a hard-filtered pool, never a hand-tuned
formula). The data for a proper draw now exists: `raw.play_events` holds real
`intent_walk` rows since the SIM-488 re-sweep.

### The SIM-429 verdict and the fix path

The lane's +0.3702/tg surplus decomposes: **IBB volume +0.201/tg (54%)**,
**Markov structure ~+0.120/tg (33%)**, **pool era ~+0.044/tg (12%)**,
**kernel tilt ~+0.005/tg (1%)**.

1. **Fix the IBB volume first** (new ticket — the largest, cheapest win): replace
   `_should_issue_ibb` with a draw over real PAs in the hard-filtered IBB cell
   (1B open × RISP × score/inning class), rate from `raw.play_events`
   `intent_walk` (6,160 events, 2017-2026, with the full cell columns).
   Expected effect: BB −~0.20/tg; also removes IBB-driven runner-on-1B
   traffic (see DP below). Filed as **SIM-515**. The mechanism defect is
   double: the formula is hand-tuned (architecture-rule violation, predates
   the rule) AND it re-rolls PER PITCH, so the per-PA fire probability
   compounds toward certainty in every qualifying spot (tendency 0.30 ×
   leverage, ~0.3-0.6 per pitch, over a ~4-pitch PA).
2. **The structural component is a design decision for the OWNER**: modeling
   within-PA correlation means conditioning the pitch draw on more than
   (matchup, count) — e.g. the pool rows already carry `prev_pitch_outcome`
   (the builder's LAG window), so the count bucket could refine on the previous
   pitch class, staying inside the draw architecture. Do not hand-tune a
   compensation constant — the rates themselves are right.
3. **The era component (~+1% BB)** is the SIM-507-precedent reference question —
   pre-registered here: the pool's 2024-26 recency mix walks slightly more than
   the 2025 band centre.
4. K-prop calibration and the CLV re-measure stay sequenced AFTER 1 (and 2 if
   taken); re-run `make calibrate` + `make validate-props` then
   `scripts/clv_backtest.py`.

## SIM-514 — the four decompositions

Run: same 12×150 lane-style batch, 94,095 balls in play, **0 legacy
resolutions** (every resolution used a transition row).

* **(a) DP — CONFIRMED as traffic.** Sim DP-per-opportunity 0.1417 vs 0.1391
  expected from the pool's own per-cell rates at the sim's cell mix (ratio
  1.019). MLB-2025: 4.847 opportunities/tg (BIP × runner-on-1B × <2 outs) vs
  sim 5.366 (**+10.7% traffic** — walks and IBBs put runners on first). The
  machinery is clean; the row closes when BB/IBB normalize. Note a definitional
  wrinkle for the lane probe: DP-class event labels read 0.8219/tg while
  strict r1+batter-retired transition rows read 0.7606/tg.
* **(b) K — closed above** (no defect; structural, same as BB).
* **(c) SB — attempts-per-opportunity vs the pool reads LOW, traffic HIGH.**
  Sim attempts/opportunity at 2B: 0.0184 vs the pool's own 0.0217; at 3B
  0.0042 vs 0.0043. Opportunities/tg ≈ +9% high (the same BB traffic).
  Corrected attempts/tg ≈ 0.74 (matches the certified 0.748). Instrument
  caveat: the sampler is cached per process, so the steal wrapper stacked
  across games and inflated COUNTS k-fold; the RATIOS above are unbiased
  (each real draw recorded k times in both numerator and denominator).
  Re-measure absolute volumes after the BB fix; if SB stays red, the 2B
  attempt-weighting (aggression/kernel) is the SIM-476 target.
* **(d) 3B — the draw is clean.** Drawn triple rate per cell = 0.973× the
  pool's own at the sim's cell mix. Sim triple/BIP 0.00518 vs MLB-2025
  0.00505 (+2.6% era) with +2.3% BIP traffic. The lane's +11.9% is at the
  noisy end (this run reads +4.7% ± 4.7%). (d)(2) is closed by inspection:
  the batter-stretch draw fires only under `hit == 2` and nothing relabels
  an event to `triple`.

## Addendum (2026-08-20, same day): three owner questions answered by measurement

### 1. prev-pitch conditioning is REFUTED — do not build it

`scripts/sim429_prev_chain_probe.py` solved the count machine over
(balls, strikes, PREVIOUS pitch class) on MLB-2025's own data:

    chain(count only)    BB/PA 0.0839   K/PA 0.2142   pitches/PA 3.931
    chain(count x prev)  BB/PA 0.0839   K/PA 0.2140   pitches/PA 3.931
    observed MLB-2025    BB/PA 0.0813   K/PA 0.2232   pitches/PA 3.883

Given the count, the previous pitch's outcome adds ~nothing. The within-PA
correlation is whole-PA-scale (the matchup's day: a wild PA is wild
throughout), which a first-order refinement cannot capture. The structural
component stays a documented design limit of any (matchup, count)-conditioned
independent sampler — and under pool-referenced grading (below) it is
definitionally accepted: the sim faithfully reproduces the pool.

### 2. The grade moves to POOL TOTALS (owner ruling, 2026-08-20)

The owner ruled: grade the sim's frequencies against the play pool's OWN
totals — the sim should match the data it draws from. Measured by
`scripts/pool_window_census.py`, the 12x150 diagnosis run against the live
(2024-2026) pool's centres:

    BB/PA +0.4%   K/PA +0.3%   HBP/PA +1.2%   pitches/PA +0.1%
    DP/opportunity +1.6%   3B/BIP -3.1%

Every channel sits within ~1.5% (3B slightly LOW — its old +11.9% red vs the
2025 band was the pool's era, not the draw). The remaining true reds under
pool grading: **IBB volume** (SIM-515 — the manager formula, not a pool draw)
and **steal attempts/opportunity −15%** (0.0184 vs 0.0217). The certification
lane's re-referencing (per-opportunity bands + opportunity-count probes) is
**SIM-516**, landing in ONE commit with the window rebuild.

### 3. The pool window: measured, and W1 (full seasons 2023-2026) is recommended

All three candidate windows were censused (volumes, per-count chains, per-BIP
mixes, thinnest hard-filter cells — full tables in the census output):

| | W1 2023-2026 | W2 2024-2026 (live) | W3 rolling 3y |
|---|---|---|---|
| pitches | 2.71M | 1.98M | 2.16M |
| consistent BIP | 467k | 342k | 374k |
| thinnest hard cell (L, rs=4, 0 out) | **439** | 323 | 351 |
| 3B-steal thinnest cell (3-0, 0 out) | 1,019 | 741 | 811 |
| smallest advancement pool (4_3_4) | 8,247 | 6,029 | 6,583 |
| chain BB/PA | 0.0850 | 0.0847 | 0.0848 |
| 2B/BIP | 0.0625 | 0.0614 | 0.0616 |

The frequency centres differ by <0.5% across windows — the choice is about
CELL THICKNESS and operations, not era drift. 2023 is rules-homogeneous with
today (pitch clock / bigger bases / shift limits all started 2023). W3 is
just W2 plus the 2023 TAIL (Aug-Oct) — it buys 8% more rows where W1 buys
36%, and it costs daily-moving artifacts (reproducibility, golden-fixture
churn, season-based recency weights misaligned, and honest backtests would
need per-date as-of artifacts). W1's standing rule — "the last three
COMPLETED seasons plus the current season to date" — also self-heals the
season boundary (W2's formulation holds only ~2 full seasons every April).
Recency weighting keeps the draw tilted current either way.

## Operational notes

* Serial 12×150 instrumented runs take ~75 min per container; two run
  comfortably in parallel (~680 MB each).
* The steal-wrapper stacking bug above is the worked example for a new trap:
  **instance-wrap per-GAME objects (the machine), never per-PROCESS cached
  objects (the sampler)** — or install the wrapper once.
