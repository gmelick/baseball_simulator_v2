# SIM-280 -- Phase 4 RAM Budget (resident play-pool + engine footprint)

*Ticket: SIM-280 · Owner: Performance Engineer · Date: 2026-05-27*
*Status: ACTIVE -- the quantified budget behind the SIM-300 §7 / SIM-114 hard cap
(total resident play-pool + FAISS memory ≤ 2 GB regardless of worker count). This
report is consumed by SIM-281 (parallelism ADR), which cites the per-worker scaling
table in §4.*

**Verdict: PASS up to 8 workers (≈1.6 GB); RISK at 16 workers (≈2.9 GB) and
RISK if the pitcher engine's arsenal cache is exhaustively precomputed (≈0.58 GB).
Both risks are mechanism choices SIM-281 must resolve, not data-volume problems --
the shared read-only payload is only ~0.29 GB.**

---

## 1. Methodology (measured vs estimated)

Numbers are grounded in the real code structures, not hand-waving:

- **MEASURED (numpy `.nbytes` / `faiss.serialize_index` / `tracemalloc`)** on the
  Linux workspace (numpy 2.2.6, faiss-cpu, scipy 1.15.3) by constructing synthetic
  profiles with the *real* engine dataclasses (`GMMComponent`, `GMMModel`,
  `PitcherProfile`, `BatterProfile`, …) imported from `similarity/engines/*.py` and
  the bench helpers in `tests/performance/bench_simulation.py` (SIM-118). FAISS tile
  sizes were measured by building `IndexFlatL2` indexes at representative `n` and
  serializing them.
- **ESTIMATED (computed from dataclass field shapes)** where building a full
  population was impractical -- e.g. league-wide cardinalities. Every cardinality
  states its assumption.

**Cardinality assumptions (stated per AC #1).** Population is the 9 loaded seasons
**2017--2025** (2020 short). Pitch volume is grounded in the real per-season row
counts from `docs/data_quality/2026-05-20-bat-side-coverage.md`
(~660k--744k pitches/season; **6.04 M** clean pitches total):

| Entity | Assumed cardinality | Basis |
|---|---:|---|
| Pitcher-seasons (qualified, ≥200 pitches) | ~3,500 | ~400 pitchers/season throwing ≥200 pitches × 9 seasons, minus 2020 short year; matches the `MIN_PITCHER_PITCHES=200` gate. The bench docstring cites ~2,400 RHP as one handedness partition -> ~3,500 across both hands and all seasons. |
| Batter-seasons (qualified, ≥100 PA) | ~4,000 | ~450 qualified batters/season × 9, `MIN_BATTER_PA=100`. |
| Catcher-seasons (≥300 pitches recv.) | ~600 | ~70 catchers/season × 9, `MIN_PITCHES_RECEIVED=300`. |
| Fielder-position-seasons (7 positions) | ~2,500 | ~280 position-seasons/year × 9; `MIN_FIELDER_BATTED_BALLS=50`. Position-gated, multi-position players counted per position. |
| Baserunner-seasons (≥? adv. opps) | ~1,500 | runners with meaningful advancement sample × 9. |
| Baserunner-steal-seasons (≥10 att.) | ~1,200 | `MIN_STEAL_ATTEMPTS=10`. |
| Pitcher-steal-seasons | ~3,500 | every pitcher with baserunner events -> ~ pitcher population. |
| Manager-seasons (≥50 games) | ~60 | ~30 managers/season, churn over 9 yrs; `MIN_GAMES_MANAGED=50`. |
| Situation rows (KDTree) | ~1.0 M PA | ~1.55 M PA across 9 seasons; the engine indexes PA-granularity, deduped/cleaned to ~1 M. |
| Batted balls in play (outcome pool, EV/LA present) | ~0.26 M | ~17% of PA. |

---

## 2. Per-engine measured footprint (AC #1)

The 11 engines split into three resident shapes:

1. **RBF / GMM engines (8)** -- hold a `dict` of profile objects *plus* stacked
   z-normalized float64 partition matrices. Per-profile cost = numpy array buffers
   + the `@dataclass(slots=True)` object shell (measured 144 B for `BatterProfile`)
   + dict-entry overhead (~100 B).
2. **Situation engine (1, KDTree)** -- a scipy `KDTree` over an `N × 11` float64
   matrix + a parallel `list[NearestSituation]` of slots objects. This is the single
   largest engine.
3. **FAISS geometric engines (2)** -- pitch-to-pitch (SIM-041) and batted-ball
   (SIM-042) are **not** resident as standalone engines in Phase 4; their content is
   what the SIM-301 cache materializes into tiles and the SIM-302 sampler loads on
   demand. They are accounted in §3 (tile budget), not double-counted here.

### 2.1 Bytes-per-profile (measured) and resident totals

| # | Engine | Vector dims (profile) | Bytes/profile (measured) | Cardinality | Resident total |
|---|---|---|---:|---:|---:|
| 1 | Pitcher (`pitcher_similarity`) | GMM: 3 comp × (mean 8 + cov 8×8) f64 + global mean/std + command 7 | **~3,644 B** (tracemalloc, 3-comp GMM incl. object graph) | 3,500 | **12.8 MB** |
| 2 | Batter (`batter_similarity`) | discipline 7 + battedball 8 + power 4 + platoon_L 7 + platoon_R 7 = 33 f64 (264 B) + shell + dict | ~510 B | 4,000 | **2.0 MB** |
| 3 | Catcher (`catcher_similarity`) | framing 4 + blocking 3 + throwing 4 + deterrence 1 + offense 4 = 16 f64 (128 B) | ~370 B | 600 | **0.22 MB** |
| 4 | Fielder (`fielder_similarity`) | range 5 + error 2 + (DP 4 / arm 4) + (specialty 2 / star 3) ≈ 13--14 f64 (104--112 B) | ~360 B | 2,500 | **0.89 MB** |
| 5 | Baserunner (`baserunner_similarity`) | speed 1 + aggression 6 + success 5 = 12 f64 (96 B) | ~340 B | 1,500 | **0.51 MB** |
| 6 | Baserunner-steal (`baserunner_steal_similarity`) | tendency 4 + jump 3 + success 2 = 9 f64 (72 B) | ~316 B | 1,200 | **0.38 MB** |
| 7 | Pitcher-steal (`pitcher_steal_similarity`) | delivery 4 + pickoff 4 + outcome 3 = 11 f64 (88 B) | ~332 B | 3,500 | **1.16 MB** |
| 8 | Manager (`manager_similarity`) | usage 6 + aggression 5 + platoon 5 = 16 f64 (128 B) | ~370 B | 60 | **0.02 MB** |
| 9 | Situation (`situation_similarity`, KDTree) | 11-dim f64 KDTree data + 1 `NearestSituation` slots obj/row | data 88 B/row + meta ~120 B/row | 1.0 M rows | **~208 MB** (88 MB data + 120 MB meta) |
| 10 | Pitch-to-pitch (`pitch_pitch_similarity`, FAISS) | -- becomes pitch tiles -- | see §3 | -- | counted in §3 |
| 11 | Batted-ball (`batted_ball_similarity`, FAISS) | -- becomes batted-ball tiles -- | see §3 | -- | counted in §3 |

**RBF/GMM engines subtotal (1--8): ~18.0 MB.**
**Situation engine (9): ~208 MB.** The 1 M-element `list[NearestSituation]` of
Python slots objects (~120 MB) dominates this engine -- it is **larger than the raw
KDTree data (88 MB)**. *(Optimization note for a follow-up: replacing the metadata
list with column-parallel numpy arrays would cut the situation engine roughly in
half. Out of scope for SIM-280 but flagged.)*

### 2.2 The arsenal-cache outlier (RISK)

`PitcherSimilarityEngine` carries an `ArsenalCache` of pairwise Wasserstein-2
distances. Measured cost is **~292 B per cached pair** (nested tuple key + float +
dict slot). Two regimes:

- **Lazy (default, per query)**: only pairs actually touched are cached. For one
  game (~18 pitchers faced × ~2,000 same-hand candidates) this is bounded at
  **~10--20 MB** -- negligible.
- **Exhaustive (`precompute_arsenal_cache()`)**: ~2,000 RHP -> ~2.0 M pairs ×
  292 B ≈ **0.58 GB for one handedness partition** (LHP adds more). This single
  structure is **~30% of the entire 2 GB envelope** and is *per-process* unless
  shared.

**Recommendation (carried into SIM-281):** Phase 4 simulation must run the pitcher
engine in **lazy** mode, or precompute the cache once offline and attach it
read-only (it is already `pickle`-serializable via `ArsenalCache.save()/load()`).
Never let each worker exhaustively precompute its own copy.

---

## 3. Play-pool tile budget (AC #2)

Derived from the SIM-300 §4.2 formulas and **measured** FAISS serialization:

- **Pitch tile (`IndexFlatL2`, 10-dim float32):** `n × 10 × 4` index bytes +
  `n × 8` rowid bytes + ~45 B FAISS header. Measured marginal cost
  **~48 B/vector** (40 B index + 8 B rowid). A 5,000-vector tile = **240 KB**
  (measured 200,045 B index + 40,000 B rowids), matching SIM-300 §7's "~240 KB"
  claim. A 1,843-vector tile serializes to **73,765 B**, matching the spec's
  example `bytes_on_disk: 73720` to within the header.
- **Batted-ball tile (3-dim float32):** ~20 B/vector (12 B index + 8 B rowid). A
  5,000-vector tile = **100 KB** (measured).

| Tile type | Bytes/vector (measured) | Typical n | Typical tile | Worst-case (5,000-vec) |
|---|---:|---:|---:|---:|
| Pitch (10-dim) | 48 B | 200--2,000 | 10--96 KB | 240 KB |
| Batted-ball (3-dim) | 20 B | 1,500--5,000 | 30--100 KB | 100 KB |

**Resident tile cap.** The SIM-302 sampler bounds residency with
`max_resident_tiles = 256` (LRU). Peak tile RAM:

```
peak_tile_RAM = max_resident_tiles × max_tile_bytes
             = 256 × 256 KB  ≈ 64 MB   (SIM-300 §7's stated figure)
             = 256 × 240 KB  ≈ 61.5 MB (measured worst case: all 256 are
                                        5,000-vec pitch tiles)
```

So **resident play-pool tile RAM ≤ ~64 MB**, independent of the full pool size
(6.04 M pitches on disk + ~1.38 M recency duplicates of the last 2 seasons). The
LRU cap is what makes the on-disk pool size irrelevant to the resident budget.

---

## 4. Total budget vs the 2 GB cap, with worker scaling (AC #3)

The decisive distinction is **shared read-only** vs **per-worker private**:

| Component | Bytes | Shareable? (read-only after build) |
|---|---:|:--|
| RBF/GMM engines (1--8) | ~18 MB | **Yes** -- frozen after build |
| Situation KDTree + meta (9) | ~208 MB | **Yes** -- built once, never mutated mid-game |
| Resident play-pool tiles (LRU 256) | ~64 MB | **Yes** -- tiles are read-only flat FAISS + `.npy`, mmap/`shared_memory`-able (SIM-300 §7) |
| **Shared read-only subtotal** | **~290 MB (~0.29 GB)** | shared once across all workers |
| Python interpreter + numpy + faiss + scipy import + scratch | ~150 MB | **No** -- per worker (private heap) |
| Arsenal cache, lazy | ~15 MB | **No** (lazy) / Yes if attached read-only | per worker |

**Key point (from SIM-300 §7):** because the ~290 MB shared payload is read-only,
the Phase 4 model (SIM-281) should place it in `multiprocessing.shared_memory` /
mmap so **N workers share one resident copy**, not N copies. Then total RAM grows
**only** with the per-worker private overhead (interpreter + scratch), staying
~flat in the shared component:

```
total(W) = shared_readonly (≈290 MB)  +  W × (interpreter ≈150 MB + lazy scratch ≈15 MB)
```

| Workers | Shared read-only | Per-worker private | **Total** | vs 2 GB cap |
|---:|---:|---:|---:|:--|
| 1 | 290 MB | 165 MB | **~0.45 GB** | PASS |
| 4 | 290 MB | 660 MB | **~0.93 GB** | PASS |
| 8 | 290 MB | 1,320 MB | **~1.58 GB** | PASS |
| 16 | 290 MB | 2,640 MB | **~2.86 GB** | **RISK -- exceeds 2 GB** |

The Backend Developer's committed model is `ProcessPoolExecutor(max_workers =
CPU_count - 1)` (`agent_team.md`). On a typical 8-core box that is **7 workers ->
~1.4 GB -> PASS**. The cap is only threatened on ≥16-core hosts.

**What does NOT scale the budget:** the shared 290 MB is constant in W. **What DOES
scale it:** the private per-worker Python footprint (~150 MB). At 16 workers that
private overhead alone is ~2.6 GB -- so the binding constraint at high core counts
is *interpreter multiplicity*, not data volume. This is the central trade-off
SIM-281 must adjudicate (cap worker count, or amortize interpreter cost).

---

## 5. Verdict (PASS / RISK)

| Scenario | Resident total | Verdict |
|---|---:|:--|
| 1--8 workers, lazy arsenal cache, shared read-only payload | 0.45--1.58 GB | **PASS** (under 2 GB with headroom) |
| 16 workers | ~2.86 GB | **RISK** -- breaches the 2 GB cap on per-worker interpreter overhead alone |
| Any worker count, exhaustive `precompute_arsenal_cache()` per-worker | +0.58 GB/worker | **RISK** -- can breach the cap even at 4 workers |

**Recommendations carried to SIM-281:**

1. **Share the read-only payload** (engine indexes + situation KDTree + play-pool
   tiles) via `multiprocessing.shared_memory` / mmap so the ~290 MB is resident
   once, not per worker. This is the lever that keeps the budget flat in W.
2. **Cap `max_workers`** so total private overhead stays under the cap -- the
   existing `CPU_count - 1` default is safe to ~8--10 cores; on ≥16-core hosts add
   an explicit ceiling (e.g. `min(CPU-1, 10)`).
3. **Run the pitcher engine lazy**, or attach a pre-built arsenal cache read-only;
   never let each worker exhaustively precompute its own ~0.58 GB copy.
4. (Follow-up, not blocking) Convert the situation engine's 1 M-row
   `list[NearestSituation]` to column-parallel numpy arrays to reclaim ~100 MB.

---

## 6. Reproducing the measurements

```sh
cd /sessions/.../baseball_simulator_v2
export PYTHONPATH=/tmp/sbshim:$PYTHONPATH
python3 - <<'PY'
import numpy as np, faiss
# Pitch tile (10-dim) — measured 48 B/vector
idx = faiss.IndexFlatL2(10); idx.add(np.random.randn(5000,10).astype('float32'))
print("5000-vec pitch tile serialize:", len(faiss.serialize_index(idx).tobytes()),
      "+ rowids", np.empty(5000,dtype=np.int64).nbytes)
# Real GMM profile via the engine dataclasses (see bench_simulation.py helpers)
from similarity.engines.pitcher_similarity import GMMComponent, GMMModel, GMM_FEATURE_NAMES
# ... build N profiles, measure with tracemalloc (see SIM-280 methodology §1)
PY
```

*End of SIM-280 report.*
