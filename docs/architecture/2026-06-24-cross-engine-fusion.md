# Cross-Engine Score Fusion — The Per-Pitch Shaping Signal

*Ticket: SIM-321 · Owner: ML / Modeling Engineer (lead) + Backend Developer · Date: 2026-06-24*
*Status: AUTHORITATIVE for fusion. Builds against the SIM-310 loop spec
(`docs/architecture/2026-06-17-phase4-sim-loop-spec.md`, §4 / §7), the
`SimilarityEngineRegistry` (`similarity/registry.py`), and the play-pool query
contract (`docs/architecture/2026-05-20-play-pool.md` §4.2 / §6.2). It defines
`simulation/score_fusion.py`.*

---

## 0. The problem this module solves

The project has **11 similarity engines** (`registry.list_engines()`). Each one,
on its own, answers "how like *X* is candidate *Y*?" — but they answer in
**two incompatible currencies**:

* RBF / GMM engines (`pitcher`, `batter`, `fielder`, `baserunner`,
  `baserunner_steal`, `catcher`, `pitcher_steal`, `manager`) emit a **bounded
  similarity ∈ [0, 1]** (1 = identical). `EngineSpec.score_type == "similarity"`.
* KDTree / FAISS engines (`situation`, `pitch_to_pitch`, `batted_ball`) emit a
  **distance** (≥ 0, lower = more similar, unbounded above).
  `EngineSpec.score_type == "distance"`.

Today nothing combines them into a **single per-pitch decision**. SIM-317
(fingerprint derivation) and SIM-318 (outcome determination) both need *one*
shaping signal per candidate, not eleven raw scores in two currencies. This
module — `ScoreFusion` — is that combiner.

---

## 1. The hard boundary (locked architecture constraint)

> **The sampler (`simulation/play_pool_sampler.py`) is the ONE and ONLY place a
> distance becomes a sampling weight** (`w_i = 1/(d_i + EPS)`, then normalized,
> then scaled by `recency_weight`). See `play_pool_sampler.py` module docstring
> and `_knn`, and the loop spec §2.1.

Fusion must **not** usurp that role. Concretely, this module:

* **MUST NOT** emit a sampling probability vector, and **MUST NOT** apply the
  `1/(d+EPS)` transform the sampler owns. Searching this module for `1/(d+EPS)`,
  `1.0 / (d`, or any normalize-to-a-p-vector step must come up empty.
* **MAY** map a distance to a **comparable, bounded `[0, 1]` similarity-like
  affinity** so heterogeneous engine outputs live in one space before they are
  blended. This is a *comparability* transform, not a *weighting* transform:
  - It is **monotone decreasing** in distance (more distance → less affinity),
    so it never re-orders neighbours and never introduces information the sampler
    would otherwise add.
  - It is **deterministic and per-candidate**; it does not normalize across
    candidates (no `Σ = 1`), so it is *not* a probability distribution and cannot
    be mistaken for the sampler's normalized weight vector.
  - It uses the **same exp-decay family the similarity engines already use
    internally** to turn their own distances into `[0,1]` scores — e.g. the
    pitcher engine maps its W2 arsenal *distance* to a score via
    `exp(-W2 / ARSENAL_SCALE)` (`pitcher_similarity.py::ArsenalSimilarity.score`).
    Fusion reuses that exact idea at the cross-engine layer:
    `affinity = exp(-distance / scale)`.

The output of fusion is a **shaping signal** (a scalar in `[0, 1]` per candidate,
or per-engine diagnostics) that *tilts* which neighbourhood the fingerprint
favours (SIM-317) and how the closed outcome vocabulary is re-weighted
(SIM-318). The sampler still does the FAISS k-NN and still owns distance→weight.
Fusion shapes **before** that draw; it does not replace it.

---

## 2. Which engines participate in the per-pitch draw

Per the loop spec §7 engine-plug-in map, the engines split into two roles:

### 2.1 Per-pitch *shaping* engines (the fusion inputs)

These three shape **which historical neighbourhood the pitch draw should favour**
(spec steps 2/3, and step 5 for the batted-ball draw):

| Engine | `score_type` | Role in the pitch draw |
|---|---|---|
| `pitcher` | similarity | Arsenal/command geometry — *what this pitcher throws* |
| `batter`  | similarity | Plate-discipline / platoon tilt — *what this batter does vs this hand* |
| `situation` | distance | Game-state neighbourhood — *what happens in spots like this* |

`pitch_to_pitch` and `batted_ball` (both FAISS, `distance`) are **not fusion
inputs** — they ARE the tiles the sampler draws from (spec §7 rows 10–11). Their
distances belong to the sampler, never to fusion (boundary §1).

### 2.2 Resolution-only engines (NOT in the per-pitch draw)

`fielder`, `baserunner`, `baserunner_steal`, `catcher`, `pitcher_steal`,
`manager` serve the **conditional resolution / hook steps** (spec steps 6/7 and
the pre-pitch / end-of-PA manager hooks), not the per-pitch pitch draw. The same
fusion machinery is reused for those steps via a **named profile** (§4.3) — e.g.
a `fielding` profile fuses `fielder` + `catcher` for step 6 — but they are out of
the default per-pitch shaping set.

---

## 3. Making heterogeneous outputs comparable

Every engine signal is reduced to a **comparable affinity ∈ [0, 1]** (1 = most
alike) before blending:

```
similarity engine s  (already in [0,1]):   a = clip(s, 0, 1)
distance engine    d  (>= 0):              a = exp(-d / scale)        # boundary §1
```

* The exp transform is monotone decreasing, so distance ordering is preserved
  (a closer situation always yields a higher affinity).
* `scale > 0` is a per-engine constant (default `1.0`; the situation engine's
  normalized+sqrt-weighted Euclidean distances sit in an O(1) range, so a unit
  scale gives useful spread). It is a *calibration knob*, not a weighting step.
* Degenerate distances: `+inf → affinity 0.0`; `NaN → treated as missing` (the
  engine is dropped from that candidate's blend with its weight redistributed,
  exactly like the pitcher engine redistributes arsenal weight when a GMM is
  missing — `pitcher_similarity.py` "Path 2").

A `similarity`-typed input is passed through unchanged (already comparable); a
`distance`-typed input is exp-mapped. The fusion **respects `score_type`**: it
reads it from the registry `EngineSpec` (or from an explicit per-input tag) and
never exp-maps a similarity or passes a raw distance into the blend.

---

## 4. The combination rule

### 4.1 Default: weighted geometric mean (log-linear blend)

Given comparable affinities `a_e ∈ [0,1]` and per-engine weights `w_e ≥ 0`
(over the engines actually present for this candidate, weights renormalized to
sum to 1):

```
fused = exp( Σ_e w_e · ln(max(a_e, FLOOR)) )      # weighted geometric mean
```

Rationale:

* **AND-semantics.** A candidate must be plausible on *every* dimension to score
  high — a great pitcher match with a terrible situation match should be pulled
  down, not rescued by averaging. The geometric mean enforces this; a single
  near-zero affinity drags the product toward zero (the `FLOOR`, default `1e-6`,
  keeps it finite and avoids `ln(0)`).
* **Order-invariance.** The blend is a symmetric function of its `(weight,
  affinity)` pairs — feeding the engines in any order yields the identical fused
  value (test invariant). Equivalent to summing logs.
* **Monotonicity.** `fused` is strictly increasing in each `a_e` (holding others
  fixed), and increasing a heavily-weighted engine's affinity moves `fused` more
  than the same change on a low-weight engine (test invariant).
* **Bounded.** `fused ∈ [0, 1]` automatically (geometric mean of `[0,1]` values).

### 4.2 Alternative: weighted linear blend

`fused = Σ_e w_e · a_e`. Same bounds / order-invariance / monotonicity, but
*OR-leaning* (one strong engine can carry a candidate). Selectable via
`rule="linear"`; the default is `"geometric"` for the per-pitch draw.

### 4.3 Per-engine weights and named profiles

Weights are documented constants, overridable per call:

```
PITCH_DRAW_WEIGHTS = {"pitcher": 0.50, "batter": 0.30, "situation": 0.20}
BATTED_BALL_WEIGHTS = {"batter": 0.55, "situation": 0.25, "pitcher": 0.20}
FIELDING_WEIGHTS    = {"fielder": 0.70, "catcher": 0.30}     # resolution-only
```

`pitcher` is the dominant per-pitch signal (matches the perf-budget's "pitcher
`query()` is ~81% of the per-pitch cost" framing and the spec §4.1 statement that
arsenal geometry supplies the fingerprint centroid). A **named profile** bundles
{participating engines + weights + rule}; the default profile is `pitch_draw`.

---

## 5. How SIM-317 / SIM-318 consume the output

`fuse_scores(...)` returns a `FusionResult` carrying:

* `fused: float` — the single `[0,1]` shaping scalar for the candidate.
* `affinities: dict[str, float]` — each engine's comparable `[0,1]` affinity
  (diagnostics + so SIM-318 can re-weight a *specific* outcome dimension).
* `weights: dict[str, float]` — the renormalized weights actually used (after any
  missing-engine redistribution).
* `rule`, `profile` — provenance.

**SIM-317 (fingerprint derivation, step 2).** Fusion does **not** build the
10-dim fingerprint — feature list/order is locked by the engines (spec §4.1).
Fusion supplies the **shaping scalar** that tilts the expected pitch/location the
fingerprint encodes (e.g. blending the pitcher arsenal centroid toward the
situation/batter neighbourhood by `fused`). SIM-317 owns the geometry; fusion
owns the *blend weight*.

**SIM-318 (outcome determination, step 4).** After the sampler returns the
closed outcome vocabulary, SIM-318 applies its count-conditional foul re-weight
(spec §5.2). Fusion's per-engine `affinities` give SIM-318 a principled,
already-comparable multiplier for *which* engines should tilt the outcome mass
— without SIM-318 ever touching a raw distance or the sampler's weight vector.

Both consumers receive a value that is **already on the right side of the
distance→weight boundary**: a bounded, monotone, sampler-independent shaping
signal.

---

## 6. Acceptance trace (SIM-321 → where satisfied)

| Requirement | Where |
|---|---|
| Which engines participate vs resolution-only | §2 (table + profiles) |
| Heterogeneous outputs made comparable, respecting `score_type` | §3 (exp-map distances; pass similarities) |
| Distance→weight boundary preserved (sampler-owned) | §1 + §3 (comparability, not weighting; no `1/(d+EPS)`) |
| Combination rule + documented per-engine weights | §4 (geometric default, linear alt, weight tables) |
| Output consumed by SIM-317 / SIM-318 | §5 (`FusionResult` contract) |

*End of SIM-321 fusion spec.*
