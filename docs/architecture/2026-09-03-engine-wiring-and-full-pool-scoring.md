# Wiring the 11 Similarity Engines into the Live Sim + Full-Pool Similarity-Weighted Selection

> **Status:** PROPOSED (design doc for review — no implementation yet).
> **Core principle:** every loop decision scores the **entire** candidate pool by the
> applicable similarity engines and draws from that full weighted distribution. There
> are **NO hard filters and NO top-K truncation** anywhere — the *only* hard filter is
> the **batter's handedness**. Pitcher handedness is *not* filtered: the pitcher engine
> scores only like-handed pitchers, so opposite-handed candidates earn ~0 weight on
> their own.
> **Supersedes (in part):** the SIM-300 per-`(season, pitcher_id, bat_hand)` play-pool
> tiling for pitch selection, and the interim run-conversion constants + query-jitter
> introduced during the SIM-421 realism fix.
> **Builds on:** SIM-317 `FingerprintDeriver`, SIM-321 `ScoreFusion`, the SIM-421
> tile-normalization + fork-safe deriver-from-disk pattern.

---

## 1. Motivation — the audit finding

The platform's premise is **similarity-anchored projection**: 11 engines score how
similar the current actors/situation are to historical ones, and those scores drive
sampling. A step-by-step audit of the *live* loop (the production `simulate_game`
path, not the unit/QA harnesses) shows that premise is almost entirely **unrealized**:

- Production builds the `StateMachine` with `manager=None`, `sim=None`, and the
  **default** `PlayResolver` (`production_factory.py:324` passes only
  `sampler`/`k`/`rng`/`fingerprint_deriver`). The only `PlayResolver` subclasses that
  exist are no-DB test/QA harnesses.
- Consequently the live sim actively uses **2 of 11** engines — Pitch and Batted
  Ball, via the hard-filtered play-pool sampler — plus the Pitcher arsenal
  *implicitly* (the centroid query). The other nine are built at app startup and
  never reach the simulation.

### 1.1 Per-step gap (intended vs. actual)

| Step | Intended engines | Actual today | Status |
|---|---|---|---|
| 1. Pre-pitch decisions (pitching change, IBB, PH, PR, pitchout, bunt) | Situation, Manager, Pitcher, Batter (+ Baserunner/Catcher) | `manager=None` → tendencies read 0 → **all no-ops** | ❌ inert |
| 1b. Steal initiate | Situation, Baserunner(steal), Pitcher, Catcher(steal) | `manager=None` → never green-lit | ❌ inert |
| 2. Pitch decision (handedness) | Situation, Pitcher, Batter | hard filter `pitcher_id`+`season`+`bat_hand`; arsenal centroid query | ⚠ Pitcher implicit only; Batter, Situation missing; pitcher is a *filter* not a similarity |
| 3. Pitch outcome | Situation, Pitcher, Batter, Pitch, Catcher(receiving) | sampled pitch's `outcome_type` | ⚠ Catcher framing, Batter, Situation, explicit Pitcher missing |
| 4. Steal resolution | Situation, Baserunner(steal), Pitcher, Catcher(steal), Pitch | `sim=None` → never attempted | ❌ inert |
| 5. Fielding resolution | Situation, Batted Ball, Fielder, Baserunner(extra) | sampled batted-ball event read verbatim | ⚠ Fielder RBF, Situation missing |
| 6. Baserunner resolution | Situation, Baserunner(extra), Fielder | hardcoded Retrosheet `_EXTRA_ADVANCE_P` constants | ❌ engines unused |

### 1.2 Root cause — fork safety

The loop has the right **seams** (`PlayResolver`, `manager`, `sim`, the fingerprint
deriver) but production passes empty/default implementations because the sim runs in
**`ProcessPoolExecutor` workers** that cannot receive the live engine objects built in
the app process (SIM-281 D1: "never a live sampler across the fork"). Every engine's
contribution must therefore reach the worker as a **disk artifact** the worker loads,
or a **worker-rebuildable** object. SIM-421 already established this pattern for the
deriver (norm stats + centroids written by the nightly build, loaded per-worker by
`_default_deriver_builder`). This plan generalizes it to all 11 engines.

---

## 2. Goals & non-goals

**Goals**
1. Every pitch-loop step is scored by its intended engine set (§1 table).
2. Every decision scores the **entire** candidate pool (the full bat_hand pool) by its
   applicable engines and draws from that full weighted distribution. **No top-K, no
   ANN shortlist, no hard filters except batter handedness.** Pitcher identity (and
   handedness) is expressed purely as a *similarity score* — the pitcher engine scores
   only like-handed pitchers, so opposite-handed candidates self-zero.
3. Fork-safe end to end: workers load disk artifacts; no live engine crosses the pool.
4. Determinism preserved (fixed `(game, seed)` → identical game).
5. Stays within the throughput SLA (2 s/game, 30 s/100-game batch).
6. Outputs remain **calibratable** (CLV is the gold standard); every wiring step is
   followed by re-calibration + realism re-validation.

**Non-goals**
- Re-deriving the engine math (GMM-W2 / RBF / KDTree internals are unchanged).
- Replacing FAISS or the DuckDB pools wholesale — we extend the build + sampler.
- Real-time online learning. All artifacts are nightly-precomputed.

---

## 3. Core architectural pattern: **full-pool similarity-weighted draw**, fork-safe via disk

The single mechanism that realizes every step. There is **no retrieval/shortlist
stage** — every candidate in the bat_hand pool is scored and eligible:

```
candidate pool = ALL plays for the batter's hand (resident in the worker)
   │  for every candidate i:  w_i = fuse( applicable engine affinities to the
   │                                      CURRENT matchup ) × recency_i
   │  (affinities: pitch/batted-ball geometry, pitcher, batter, situation,
   │   catcher, fielder, baserunner — whichever the step calls for, §4)
   ▼
draw  i ~ Categorical(w / Σw)  over the WHOLE pool → the sampled play + its outcome
```

- **No top-K, no ANN.** The k=25 measurement proved the kernel is flat (the 25 nearest
  held only 1–7 % of the weight), so any cutoff discards most of the distribution. The
  full-pool draw uses every statistically relevant play; the *weights* alone decide
  what is likely. This also retires the SIM-421 query-jitter hack (breadth comes from
  the pool + the kernel, never from scattering a centroid).
- **Pitcher handedness is not filtered.** The pitcher engine (GMM-W2, hard L/R
  partition) returns ~0 similarity for an opposite-handed pitcher, so those candidates
  earn ~0 weight without an explicit filter. The *only* hard filter is the batter's
  resolved handedness (pitch/contact geometry is mirror-dependent).
- **Weights factorize, which is what makes full-pool affordable** (see §6): a
  candidate's weight is a product of per-actor factors —
  `w_i = f_pitcher[p_i] · f_batter[b_i] · f_catcher[c_i] · f_situation(s_i) ·
  f_geom_i · recency_i`. The pitcher/catcher factors are fixed for a whole
  half-inning; the batter factor for a whole PA; only the situation factor changes per
  PA; geometry + recency are pool-constants. So the expensive part is computed once per
  matchup and reused, and sampling is made O(1)/pitch with an alias table.

### 3.1 The fork-safe artifact bundle

A new nightly-built, worker-loaded bundle (extends `play_pool_cache` + the SIM-421
artifacts). The worker's factory loads it once and threads it into an engine-backed
`PlayResolver` + manager + sampler. Nothing live crosses the fork.

```
${BASEBALL_PLAY_POOL_DIR}/engine_artifacts/
  pitch_pool/<hand>.{vecs.npy,meta.parquet}   # FULL per-hand pitch pool, RESIDENT (no index):
                                              #   vecs = normalized geometry (for f_geom)
                                              #   meta = {pitcher_id, batter_id, catcher_id,
                                              #           situation_vec, outcome_type, recency}
  battedball_pool/<hand>.{vecs.npy,meta.parquet}  # FULL per-hand batted-ball pool, resident
  steal_pool.parquet                  # SB attempts → {runner,catcher,pitcher,situation,safe,recency}
  pitcher_sim.npz                     # pitcher×pitcher GMM-W2 similarity (same-hand only; a few MB)
  batter_embeddings.npz               # per-batter RBF feature vectors (+ norm)
  catcher_embeddings.npz              # framing + throwing vectors per catcher
  fielder_rbf.npz                     # per-fielder/position RBF params (out/error model)
  baserunner_embeddings.npz           # speed / steal-jump vectors per runner
  situation_kdtree.pkl                # the SIM-070 situation KDTree (or its vectors)
  manager_profiles.parquet            # per-manager tendency vectors
  norm/, centroids/                   # the SIM-421 norm + matchup centroids (already exist)
```

There is **no FAISS ANN index** — the whole point is to score the full pool, so the
worker holds the pool's geometry + per-row metadata **resident** and computes
`f_geom` by a vectorized (BLAS) distance over all rows. The per-hand pool is large
(~0.5–3 M rows × 10 float32 ≈ 100–400 MB/hand); load it **once per process via SIM-333
shared memory** (zero-copy across workers), not per-worker. Each engine contributes
**either** a pairwise similarity lookup (e.g. the pitcher×pitcher W2 matrix —
~N_pitchers² floats, a few MB; the worker indexes it by the candidate's `pitcher_id`)
**or** per-actor embeddings the worker compares on the fly. Cold-start actors (a rookie
with no fitted profile) fall back to a league-average embedding (empirical-Bayes
shrinkage, already used elsewhere) so they never earn zero weight everywhere.

### 3.2 Composite scoring — reuse SIM-321 `ScoreFusion`

`ScoreFusion` already defines per-step weight profiles + rules
(`pitch_draw`/`batted_ball`/`fielding`, geometric/linear). Today it's only invoked for
the deriver's *location tilt*. The plan **feeds it real per-candidate affinities** and
uses the **fused scalar as the sampling weight**:

```
weight(candidate) = ScoreFusion(profile).fuse({
    "pitch":      kernel(fingerprint_distance),       # vectorized over the FULL pool
    "pitcher":    sim_pitcher(query_pitcher, cand.pitcher),
    "batter":     sim_batter(query_batter, cand.batter),
    "situation":  sim_situation(state_vec, cand.situation_vec),
    "catcher":    sim_catcher_recv(query_catcher, cand.catcher),
    ...
}).fused  ×  recency_weight(candidate)
```

The fusion weights become first-class **calibration parameters** (target median
similarity ≈ 0.50; arsenal kernel `exp(-W₂/4.10)` per the locked design decisions).

---

## 4. Per-step design

### Step 2 + 3 — Pitch decision + outcome (one sampled pitch carries both)
- **Candidate pool:** the FULL per-hand pitch pool (all pitchers, all seasons). The
  *only* hard filter is the batter's hand; `pitcher_id`/`season`/pitcher-hand are NOT
  filtered (pitcher engine self-zeroes opposite-hand candidates; season → recency).
- **Score every candidate (profile `pitch_draw`):** `pitcher` (GMM-W2 between thrower
  and each candidate's pitcher — the arsenal-similarity gradient, same-hand only),
  `batter` (RBF), `situation` (KDTree over count/base-out/score/inning), `catcher`
  receiving (framing affinity), `pitch` (fingerprint-geometry kernel), × recency.
- **Draw** over the full weighted pool → the candidate's `outcome_type` ∈ {ball,
  called_strike, swinging_strike, foul, in_play}. **Catcher framing** is applied as a
  post-draw ball↔called_strike adjustment scaled by the current catcher's framing delta
  vs. league (or folded into the catcher affinity so framed pitches up-weight called
  strikes).

### Step 5 — Fielding resolution
- **Batted-ball selection:** score the FULL per-hand batted-ball pool by the 3-dim
  contact-geometry kernel (EV/LA/spray) × `batter` × `situation` × recency (profile
  `batted_ball`) and draw → a sampled contact event. No top-K.
- **Out/hit/error:** the **Fielder RBF** maps `(EV, LA, spray, the current defense)`
  → out / hit / error probability (replacing the default's verbatim read of the
  sampled event), conditioned on the actual fielders' range/arm.

### Step 6 — Baserunner resolution
- Replace the hardcoded `_EXTRA_ADVANCE_P` with the **Baserunner(extra-base)** engine:
  P(advance | runner speed-similarity, OF-arm of the fielding **Fielder** engine,
  `situation` outs/score). Deterministic via the loop rng.

### Step 1 — Pre-pitch manager decisions
- Wire an **engine-backed manager**: each decision (pitching change, IBB, pinch-hit,
  pinch-run, pitchout, bunt) scored by `manager` tendency × `situation` leverage,
  conditioned on `pitcher`/`batter` matchup. Replaces the all-no-op `manager=None`.

### Step 1b + 4 — Steal initiate + resolution
- Wire the `stolen_base_pool`: **initiate** scored by `manager`+`situation`+
  `baserunner(steal)`+`pitcher`+`catcher(steal)`; **resolution** draws safe/caught
  from the SB pool re-scored by `baserunner(steal)`×`catcher(steal)`×`pitcher-steal`×
  `situation`×pitch-context×recency.

---

## 5. The candidate pool (one per batter hand, full, resident)

- **One pool per `bat_hand`**, holding *every* play for that batting side across all
  seasons/pitchers — replacing the thousands of per-`(season,pitcher,hand)` tiles.
  Same for batted balls. **No index, no shortlist:** the pool's geometry + per-row
  metadata are held resident and scored in full on each draw.
- Built in the SIM-041/042 normalized space (z-score × √weight) so the geometry kernel
  is comparable to the deriver's query.
- Season is **no longer a partition** — it becomes the `recency_weight` (soft), so a
  pitcher's multi-season history all contributes, weighted toward recent.
- Loaded **once per process via SIM-333 shared memory** (zero-copy across workers).

---

## 6. Determinism, performance, calibration

Full-pool scoring per pitch is the central performance risk; it is made affordable by
**factorization + per-matchup caching + alias sampling**, not by truncation.

- **Factorized weights.** `w_i = f_pitcher[p_i]·f_batter[b_i]·f_catcher[c_i]·
  f_situation(s_i)·f_geom_i·recency_i`. The per-actor factors are O(1) gathers from
  small lookup vectors (length = #pitchers / #batters / #catchers), and they are
  **constant within a matchup**: `f_pitcher`/`f_catcher` for a whole half-inning,
  `f_batter` for a whole PA. `f_geom` + `recency` are pool-constants (computed once).
  So only `f_situation` (one vectorized distance over the pool) is recomputed per PA.
- **Compute once per PA, draw O(1) per pitch.** Assemble the full weight vector once at
  each new matchup (a few vectorized passes over the pool), build an **alias table**
  (O(N) once), then every pitch in the PA is an O(1) alias draw. Budget ≈ ~80 PA-setups
  per game rather than ~290 per-pitch full scores.
- **Measured benchmark (SIM-423 perf gate, real R-hand pool N=3.65 M, `scripts/perf_fullpool.py`).**
  Per-op: `f_geom` 54 ms, `f_situation` 41 ms, per-actor gather 2.5 ms, weight+cumsum
  ~12 ms, per-pitch alias draw 1.8 ms. **Naive full-pool ≈ 6 s/game** (base cached) —
  ~3× over the 2 s SLA; the per-PA situation distance over the full pool dominates.
- **The three levers that bring it under SLA** (projected **~1.1–1.5 s/game** from the
  measured per-op costs):
  1. **Cache the half-inning base** (`f_geom·f_pitcher·f_catcher·recency`) — recomputed
     only on a pitcher change (~18×/game), not per PA.
  2. **Bucket the situation factor** — precompute `f_situation` per discrete base-out ×
     count × inning/score bucket (~hundreds) once per PA; the candidate's factor is then
     an O(1) **gather** (~2.5 ms) instead of the 41 ms full-pool distance.
  3. **Build-time recency floor** — keep ~the last 3 seasons (N: 3.65 M → ~1 M), scaling
     every O(N) pass ~3×. A *build*-time cut, **not** a per-draw top-K.
  Re-benchmark under `PERF_STRICT` once implemented; the 100-game batch parallelizes
  across workers.
- **Determinism:** the only RNG is the loop + sampler generators, seeded per game; the
  alias table is deterministic given the weight vector. No approximate index, so no
  recall/nondeterminism concerns.
- **Calibration:** every wired engine shifts outputs, so each phase ends with (a)
  `scripts/sim_stats.py` realism re-check, (b) the SIM-325 chi-squared gate, (c)
  re-fitting the calibration map, and ultimately (d) a CLV backtest. The
  `ScoreFusion` weights + kernel bandwidths are the tunable knobs.

---

## 7. Sequencing & ticket breakdown

Provisional IDs — **confirm against `BACKLOG.md`** (note: the SIM-421 realism work in
this session was provisionally tagged SIM-421 in code comments, which collides with the
prop-markets ticket; reconcile the IDs when filing).

| # | Ticket | Scope | Depends on |
|---|---|---|---|
| A | Fork-safe **engine-artifact bundle** + loader | nightly build writes §3.1 artifacts; worker loads them; `EngineArtifacts` object threaded via the factory | SIM-421 deriver pattern |
| B | **Full-pool resident sampler** + alias draw | build per-hand full pools (geometry + metadata) into shared memory; factorized-weight + alias-table sampler API; drop per-pitcher tiles | A |
| C | **Pitch full-pool scoring** (steps 2/3) | feed ScoreFusion `pitch_draw` with pitcher/batter/situation/catcher affinities over the full pool; delete jitter | B + pitcher_sim/batter/situation/catcher artifacts |
| D | **Engine-backed `PlayResolver`** (steps 5/6) | full batted-ball pool scoring; Fielder RBF out/error; Baserunner(extra) advancement (replace constants) | A, B |
| E | **Steal path** (steps 1b/4) | wire `stolen_base_pool` + baserunner/catcher/pitcher-steal scoring | A |
| F | **Manager decisions** (step 1) | engine-backed manager + situation leverage | A |
| G | **Catcher framing** (step 3) | framing-conditioned ball/called-strike adjustment | C |
| H | **Re-calibration + CLV** | re-fit calibration map; CLV backtest; perf-gate the new path | C–G |

Each of B–G is independently shippable behind the resolver/manager seams (the loop
already tolerates partial wiring), so realism can be validated incrementally.

---

## 8. Risks & open questions

1. **Full-pool performance at 1–3 M rows/hand** — this is the central risk and the
   reason the design leans on factorization + per-PA caching + the alias table (§6).
   The full weighted pass per PA must be benchmarked against the 2 s/game and
   30 s/100-game SLA under `PERF_STRICT` *before* committing; if it's too slow the
   levers are shared memory (done), float32 BLAS, and a build-time recency floor —
   **never** a per-draw top-K (which would reintroduce exactly the truncation we are
   removing).
2. **Calibration drift / double-counting** — engines partly overlap (pitcher arsenal
   is in both the geometry kernel *and* the pitcher engine); the ScoreFusion weights
   must be re-fit to avoid double-counting (the "target median 0.50" anchor).
3. **Cold-start actors** — rookies/low-sample players need league-average fallbacks per
   engine (empirical-Bayes), or they get zero affinity everywhere.
4. **Artifact build time + size** — the nightly job grows (full resident pools +
   similarity matrices); verify it fits the nightly window, the per-hand pool fits in
   shared memory, and the pitcher×pitcher matrix fits in RAM.
5. **Backwards compat** — the per-pitcher tiles + the SIM-421 jitter/constants are
   retired by B–D; the QA harness (SIM-324/325) and regression fixtures will shift
   again and need re-baselining per phase.
