# FAISS Index Design Spec (Phase 4)

*Ticket: SIM-114 · Owners: Performance Engineer + ML Engineer · Date: 2026-06-03*
*Status: PROPOSED — awaiting PM sign-off (§7). Applies to the per-tile FAISS path only
(the fallback/unit-test path; production pitch-selection is full-pool, SIM_FULL_POOL=1).
Closes the IVF/HNSW deferral left
open by SIM-300 §4.2 / §9 and confirmed-but-not-quantified by SIM-281 D3. Consumes
the RAM budget from SIM-280 (`docs/perf/2026-05-27-ram-budget.md`) and the
parallelism contract from SIM-281 (`docs/architecture/2026-05-27-parallelism.md`).*

---

## 0. Purpose

SIM-300 shipped the play-pool tiling architecture with **`IndexFlatL2` for every
tile** and explicitly deferred the index-type question — IVF / HNSW and `nprobe`
tuning — to "a Performance Engineer ticket" (SIM-300 §4.2, §9). SIM-281 reaffirmed
flat L2 for Phase 4 but flagged that the crossover ("revisit IVF/HNSW only if a
tile's `n_vectors` regularly exceeds ~50k", SIM-281 D3) was an *unmeasured*
assertion.

This doc closes that gap with **real benchmark numbers generated in the sandbox**
(faiss 1.13.2, numpy 2.2.6, single OpenMP thread). It answers four questions and
ties each to the existing contracts:

1. **`IndexFlatL2` vs `IndexIVFFlat`** — measured recall@k and query latency at
   3k / 50k / 250k / 1M vectors, with an `nprobe` sweep, and a concrete
   `nlist`/`nprobe` recommendation (§2).
2. **Reconciliation with SIM-300** — why per-tile flat is *still* correct for
   typical tiles, and the **crossover threshold** (vectors/index) above which
   IVFFlat is warranted, justified by the latency table and the per-step time
   budget (§3).
3. **The mandatory pre-filter** restated as a hard requirement (§4).
4. **Shared-memory strategy + ≤2 GB budget** carried forward unchanged from
   SIM-281 / SIM-280 (§5, §6).

---

## 1. Methodology (measured, not estimated)

All numbers below are **measured** on the Linux workspace, not modelled:

- Synthetic float32 vectors at the **pitch fingerprint dimension (10-dim, SIM-041)**,
  generated with mild Gaussian clustering (50 centers, σ=1 within-cluster) so that
  IVF's coarse quantizer has real structure to partition — an i.i.d. uniform cloud
  would flatter IVF's recall artificially.
- **`faiss.omp_set_num_threads(1)`** — single-thread search, because the Phase 4
  model (SIM-281 D1) runs one game per process under
  `ProcessPoolExecutor(max_workers=min(CPU-1, 10))`; each worker searches
  single-threaded. Multi-thread FAISS would understate per-worker latency.
- **k = 25** (the `PlayPoolSampler.sample_pitch` default, SIM-300 §6.2).
- **Recall@k = mean fraction of the k flat-truth neighbours recovered** by the
  approximate index, with `IndexFlatL2` results as exact ground truth. Latency =
  best-of-N per-query wall time over 1,000 queries after a warm-up search.
- IVF parameters: `nlist = 512`, `nprobe ∈ {1, 8, 16, 32, 64}`.

Reproduce with the snippet in §8.

---

## 2. Index-type decision — measured benchmark (AC #1)

### 2.1 Recall@25 and query latency, IndexFlatL2 vs IndexIVFFlat (nlist=512)

Single-thread, 10-dim, k=25, 1,000 queries. `IndexFlatL2` is exact (recall = 1.000
by definition) and is the ground truth the IVF recall is measured against.

| n (vectors) | Index | Build (ms) | recall@25 | Latency (µs/query) |
|---:|---|---:|---:|---:|
| **3,000** | IndexFlatL2 | 0.2 | 1.000 | **6.4** |
| 3,000 | IVFFlat nprobe=1 | 4.3 | 0.158 | 0.4 |
| 3,000 | IVFFlat nprobe=8 | 4.3 | 0.842 | 2.5 |
| 3,000 | IVFFlat nprobe=16 | 4.3 | 0.973 | 3.6 |
| 3,000 | IVFFlat nprobe=32 | 4.3 | 0.998 | 6.1 |
| 3,000 | IVFFlat nprobe=64 | 4.3 | 1.000 | 10.6 |
| **50,000** | IndexFlatL2 | 0.1 | 1.000 | **53.6** |
| 50,000 | IVFFlat nprobe=1 | 66.2 | 0.382 | 2.1 |
| 50,000 | IVFFlat nprobe=8 | 66.2 | 0.941 | 6.0 |
| 50,000 | IVFFlat nprobe=16 | 66.2 | 0.991 | 8.4 |
| 50,000 | IVFFlat nprobe=32 | 66.2 | 1.000 | 15.2 |
| 50,000 | IVFFlat nprobe=64 | 66.2 | 1.000 | 26.7 |
| **250,000** | IndexFlatL2 | 3.2 | 1.000 | **244.2** |
| 250,000 | IVFFlat nprobe=1 | 194.4 | 0.426 | 4.8 |
| 250,000 | IVFFlat nprobe=8 | 194.4 | 0.957 | 16.8 |
| 250,000 | IVFFlat nprobe=16 | 194.4 | 0.995 | 28.0 |
| 250,000 | IVFFlat nprobe=32 | 194.4 | 1.000 | 51.6 |
| 250,000 | IVFFlat nprobe=64 | 194.4 | 1.000 | 94.3 |
| **1,000,000** | IndexFlatL2 | 102.9 | 1.000 | **966.2** |
| 1,000,000 | IVFFlat nprobe=1 | 404.8 | 0.430 | 10.4 |
| 1,000,000 | IVFFlat nprobe=8 | 404.8 | 0.969 | 55.2 |
| 1,000,000 | IVFFlat nprobe=16 | 404.8 | 0.996 | 98.8 |
| 1,000,000 | IVFFlat nprobe=32 | 404.8 | **1.000** | **187.9** |
| 1,000,000 | IVFFlat nprobe=64 | 404.8 | 1.000 | 369.9 |

### 2.2 What the numbers say

- **Flat latency scales linearly with n**, as expected for brute force:
  6.4 µs @ 3k → 53.6 µs @ 50k → 244 µs @ 250k → **966 µs @ 1M**.
- **IVF's win only opens up at scale.** At the **largest scale (1M)** IVFFlat at
  `nprobe=32` delivers **recall 1.000 at 188 µs/query vs flat's 966 µs/query — a
  ~5.1× speedup at exact recall**; `nprobe=16` gives recall 0.996 at 99 µs/query
  (~9.8× faster). At 250k the same `nprobe=32` is 51.6 µs vs flat's 244 µs (4.7×).
- **`nprobe` is a clean recall/latency dial.** Across every scale, recall climbs
  monotonically with `nprobe`: `nprobe=1` is unusable (0.16–0.43 recall — it probes
  one of 512 lists), `nprobe=8` reaches ~0.84–0.97, `nprobe=16` reaches **≥0.99**,
  and `nprobe=32` reaches **1.000 (exact)** at every tested scale ≥50k.
- **IVF has real fixed costs flat does not:** a **training step** (66–405 ms build
  vs flat's sub-millisecond add) and an **accuracy floor** below `nprobe≈16`. Those
  costs are only worth paying when the per-query latency they buy back actually
  matters — i.e. at large n (§3).

### 2.3 Recommendation: `nlist=512`, `nprobe=32` (when IVF is used at all)

For any index that crosses the threshold in §3 and therefore ships as IVFFlat:

- **`nlist = 512`.** The √n rule-of-thumb (`nlist ≈ √n`) lands near 500 at n≈250k
  and ~1,000 at n=1M; 512 sat in the sweet spot across the whole tested band and is
  a clean power-of-two. (Note FAISS's training-point warning fires for n < ~20k —
  another signal that IVF is the wrong tool for small tiles, §3.)
- **`nprobe = 32`** as the production default: it is the **smallest `nprobe` that
  recovered recall 1.000 (exact) at every scale ≥50k** in the sweep, so the sampler
  draws from the identical neighbour set it would under flat — no silent
  outcome-distribution drift. **`nprobe = 16`** is the acceptable fast-path
  (recall ≥0.99, ~2× faster than 32) for any future latency-critical monolithic
  index where a <1% neighbour miss is tolerable; it must **not** be used where the
  backtester (SIM-220) consumes the full outcome distribution, which assumes the
  exact neighbour set.

---

## 3. Reconciliation with SIM-300 — the crossover threshold (AC #2)

### 3.1 The apparent tension, and why there is none

SIM-300 §4.2 ships `IndexFlatL2` per tile; §2 above shows IVFFlat is 5–10× faster
at 1M. These do not conflict, because **the mandatory pre-filter (§4) means a pitch
tile is never anywhere near 1M vectors.** Per SIM-300 §3, every pitch tile is scoped
to a single `(season, pitcher_id, bat_hand)` triple, which is **typically
200–5,000 vectors** (SIM-300 §4.2; SIM-280 §3 measures these same sizes). The IVF
benchmark scales of 250k and 1M describe a *hypothetical un-tiled monolithic index*,
not a real tile.

At the sizes tiles actually take, flat L2 is the right answer on every axis:

- **Latency is already negligible.** Measured flat single-thread latency across the
  real tile-size regime (k=25):

  | Tile n | Flat latency (µs/query) | Serialized index bytes |
  |---:|---:|---:|
  | 200 | **1.9** | 8,045 |
  | 500 | 2.8 | 20,045 |
  | 1,000 | 3.8 | 40,045 |
  | 2,000 | 4.8 | 80,045 |
  | **5,000** | **8.8** | 200,045 |
  | 10,000 | 14.4 | 400,045 |
  | 20,000 | 23.5 | 800,045 |
  | 50,000 | 54.3 | 2,000,045 |
  | 100,000 | 101.7 | 4,000,045 |
  | 250,000 | 248.2 | 10,000,045 |

  A worst-case 5,000-vector tile resolves a 25-NN query in **8.8 µs**. A typical
  ~1,000-vector tile is **3.8 µs**. (The serialized-bytes column also confirms
  SIM-280 §3's ~48 B/vector: a 5,000-vec tile = 200,045 B index ≈ the spec's
  "240 KB" once the 40 KB rowid `.npy` is added.)

- **IVF would be strictly worse for a small tile.** At 3k, IVF needs `nprobe=64` to
  match flat's exact recall and is then **10.6 µs vs flat's 6.4 µs — slower** —
  while also paying a 4.3 ms training step on every nightly rebuild and triggering
  FAISS's "too few training points" warning. IVF buys nothing below its crossover.
- **Flat is exact, training-free, and trivially mmap-able** — the last property is
  what SIM-281 D2's zero-copy shared-memory attach depends on. A trained IVF index
  carries a quantizer that complicates the mmap-share story.

### 3.2 The crossover threshold

**Recommended crossover: `IVF_CROSSOVER_VECTORS = 50,000` vectors per single index.**
Below it, ship `IndexFlatL2`; at or above it, ship `IndexIVFFlat(nlist=512,
nprobe=32)`. Justification, grounded in §2 and the per-step budget:

- **Per-step time budget.** The SLA is single game < 2 s (SIM-281 Context). A game
  is ~300 pitches × (1 pitch search + up to 1 batted-ball search) ≈ **≤ 600 FAISS
  searches/game**. To leave generous headroom for the RBF/GMM engine math, the
  distance→weight draw, and RNG sampling that share the 2 s, we budget **≤ ~100 µs
  per FAISS search** as the point at which search starts to be a non-trivial line
  item (600 × 100 µs = 60 ms/game of pure FAISS — comfortably <2 s, with room).
- **Where flat crosses 100 µs:** the §3.1 table shows flat single-thread latency
  reaches ~100 µs at **n ≈ 100,000** and is still only ~54 µs at 50,000. So flat is
  *latency-safe* up to ~100k on its own.
- **Why set the line at 50k, not 100k:** at 50k, IVF(nprobe=32) is already
  **3.4× faster (15.2 µs vs 54.3 µs) at recall 1.000**, and the IVF training
  warning has cleared (n > ~20k training points). 50k is the size at which IVF's
  speedup is both *real* (>3×) and *free of accuracy compromise*, while still
  leaving a safety margin below the 100 µs budget line. Setting the threshold here
  means we switch to IVF *before* flat latency becomes a measurable fraction of the
  game budget, not after.

This 50k figure **confirms SIM-281 D3's previously-unmeasured "~50k" assertion with
real numbers.**

### 3.3 Verdict: SIM-300's per-tile flat decision STANDS

**For every typical pitch tile (200–5,000 vectors) and batted-ball tile
(1,500–5,000 vectors, 3-dim), `IndexFlatL2` remains the correct, exact, and fastest
choice.** No production tile under the mandatory pre-filter approaches the 50k
crossover. IVFFlat is warranted **only** in the following regimes:

| Regime | n per index | Index type |
|---|---:|---|
| Typical pitch / batted-ball tile (pre-filtered) | 200–5,000 | **IndexFlatL2** (exact, SIM-300 §4.2 stands) |
| Large league fall-back tile (`pitcher_id=0`), pathological seasons | up to ~tens of thousands | **IndexFlatL2** if < 50k; **IVFFlat(512, nprobe=32)** if ≥ 50k |
| Any future *non-tiled monolithic* index (e.g. a whole-pool research index, ~1M+) | ≥ 50,000 | **IndexIVFFlat(nlist=512, nprobe=32)** |

In practice only the league-average fall-back tiles (which aggregate all rows for a
`(season, bat_hand)` whose specific pitcher tiles fell below `MIN_TILE_ROWS=50`,
SIM-300 §3) could plausibly approach the threshold, and even those are well under
50k under the current 9-season pool. **The cache builder (SIM-301) SHOULD apply the
crossover per tile at build time:** if a tile's `n_vectors ≥ IVF_CROSSOVER_VECTORS`,
emit IVFFlat with `nlist=512` (train on the tile's own vectors) and record
`index_type: "ivfflat"` + `nlist` + `nprobe` in the `.meta` sidecar (SIM-300 §4.3);
otherwise emit flat as today. The `.meta` `index_type` field lets `PlayPoolSampler`
set `index.nprobe` on load without guessing. **No tile under the pre-filter is
expected to trigger this path today — it is a forward-compatible guard, not a Phase 4
behaviour change.**

---

## 4. The mandatory pre-filter (AC #3) — HARD REQUIREMENT

The pre-filter is what makes the §3 conclusion hold; it is **not optional** and is
restated here as a binding constraint, identical to SIM-300 §3:

| Pool | Pre-filter key (mandatory) | Why it bounds tile size |
|---|---|---|
| **Pitch-to-pitch** (SIM-041, 10-dim) | **`pitcher_id` + `bat_hand`** | A pitch's geometry is a property of who threw it to which-handed batter. This split shrinks ~6.04 M pooled pitches to per-pitcher-per-hand tiles of ~200–5,000 vectors — three orders of magnitude below the 50k crossover. Cross-pitcher or cross-hand sampling would fabricate arsenals. |
| **Batted-ball** (SIM-042, 3-dim) | **`bat_hand` only** | Launch geometry (EV/LA/spray) is a batter-side property; pitcher identity is already baked into which pitches reach contact. Spray MUST use `pull_relative_spray_angle` with raw `spray_angle` fall-back (SIM-051 / `_select_spray_column()`), never re-implemented. |

**This pre-filter is the load-bearing reason per-tile flat L2 is correct.** Removing
or weakening it (e.g. building a monolithic per-season index) would push a single
index toward the 1M scale where §2 shows flat costs ~966 µs/query — at which point
the §3 IVF crossover applies. The pre-filter is therefore a *hard requirement for
the index-type decision to remain valid*, in addition to its correctness role in
SIM-300 §3. The `MIN_TILE_ROWS=50` floor and league-average fall-back (SIM-300 §3)
are unchanged.

---

## 5. Shared-memory strategy (AC #4) — consistent with SIM-281

Index type and the shared-memory model interlock: the choice of flat L2 is partly
*because* it is trivially mmap-able, which is what SIM-281 D2's zero-copy attach
relies on. This section carries SIM-281 D2 forward unchanged and notes the one IVF
caveat.

- **FAISS tiles are attached, not copied.** Tiles are read-only between nightly
  builds (SIM-300 §4.4 atomic-write contract), so they are published once and
  shared across all `ProcessPoolExecutor` workers via
  **`multiprocessing.shared_memory` / mmap** — `attach-by-name`, zero-copy
  (SIM-281 D2.1). The serialized `IndexFlatL2` vector block is a contiguous float32
  array; the sampler attaches it with `faiss.read_index(path, IO_FLAG_MMAP)` (or
  reconstructs over an `np.memmap` of the raw vectors), and `.rowids.npy` is
  attached with `np.load(path, mmap_mode='r')`. The OS page cache keeps **one
  physical copy** of each tile's pages mapped read-only into every worker.
- **Lazy first-touch load + LRU.** Workers `load_tile()` on first
  `sample_pitch`/`sample_batted_ball` for a `(season, pitcher_id, bat_hand)` and
  insert into a `max_resident_tiles=256` LRU (SIM-300 §6.1; SIM-281 startup
  sequence step 5). First touch is a page fault, not a deserialize — sub-millisecond
  and amortized across workers by the page cache.
- **IVF caveat (only relevant above the §3 crossover).** A trained IVFFlat index
  carries an inverted-list structure and a coarse quantizer in addition to the
  vector block, so the clean "mmap the contiguous float32 block" path is less
  direct than for flat. `faiss.read_index(path, IO_FLAG_MMAP)` still memory-maps an
  IVF index, but the per-worker resident header is larger than flat's. Since **no
  Phase 4 tile is expected to cross 50k (§3.3)**, this is a documented forward-note,
  not a Phase 4 work item — it is one more reason to keep flat as the default and
  reserve IVF strictly for the monolithic-index regime.

This is fully consistent with SIM-281 D2/D3; the only addition is the measured
crossover that makes D3's deferral concrete.

---

## 6. The ≤ 2 GB budget (AC #5) — consistent with SIM-280

**Hard cap: total resident play-pool + FAISS memory ≤ 2 GB regardless of worker
count** (SIM-300 §7 / SIM-114 envelope, quantified by SIM-280). The index-type
decision honours this with margin:

- **Flat tiles are tiny** (measured ~48 B/vector for 10-dim pitch tiles, SIM-280 §3
  / §3.1 above): a worst-case 5,000-vector tile is ~240 KB. The LRU cap of 256 tiles
  bounds resident tile RAM at **≤ ~64 MB**, independent of the 6.04 M-pitch on-disk
  pool (SIM-280 §3). Shared once across workers via §5, this 64 MB is part of the
  ~290 MB shared read-only payload, **not** multiplied by W.
- **IVF would cost more, not less, RAM** for small tiles (it adds quantizer +
  inverted-list overhead on top of the vector block), so even ignoring latency, IVF
  is the wrong call inside the budget for typical tiles. The crossover (§3) keeps IVF
  confined to the rare large-index regime where its latency win is decisive and its
  modest RAM overhead is acceptable.
- **Budget formula unchanged (SIM-280 §4):**
  `total(W) = ~290 MB shared read-only + W × ~165 MB private`. PASS to ~8–10 workers
  (1.58 GB @ 8 W); the 10-worker ceiling (SIM-281 D1) keeps ≥16-core hosts under the
  cap. The index-type choice does not move this curve — flat tiles are already
  counted in the 290 MB shared payload, and the crossover never activates IVF for a
  resident tile under Phase 4 sizing.

The index decision therefore costs **zero** additional budget over what SIM-280
already certified.

---

## 7. PM approval (required before the crossover guard lands in SIM-301)

This spec resolves the IVF/HNSW deferral from SIM-300 §4.2/§9 and quantifies
SIM-281 D3. It must be signed off by the Product Manager before the optional
crossover guard (§3.3) is added to the SIM-301 cache builder.

- [ ] **Reviewed & approved** — per-tile **`IndexFlatL2` stands** for typical tiles
      (200–5,000 vec); **crossover at `IVF_CROSSOVER_VECTORS = 50,000`** vectors/index,
      above which **`IndexIVFFlat(nlist=512, nprobe=32)`** (recall 1.000 in benchmark).
- [ ] Acknowledged the **measured headline** (1M scale): IVFFlat nprobe=32 =
      recall 1.000 @ 188 µs/query vs flat 966 µs/query (~5.1× faster); nprobe=16 =
      recall 0.996 @ 99 µs/query.
- [ ] Acknowledged the **mandatory pre-filter** (§4) as a hard requirement that keeps
      every production tile far below the crossover, so no Phase 4 tile ships as IVF.
- [ ] Acknowledged the **shared-memory** (§5) and **≤2 GB budget** (§6) conclusions
      are carried unchanged from SIM-281 / SIM-280 — this spec adds no budget cost.

**Signed off by (PM):** ______________________   **Date:** ____________

---

## 8. Reproducing the benchmark

```sh
cd /sessions/.../baseball_simulator_v2
export PATH="$HOME/.local/bin:$PATH"
export PYTHONPATH=/tmp/sbshim:$PYTHONPATH
python3 - <<'PY'
import time, numpy as np, faiss
faiss.omp_set_num_threads(1)               # per-worker single-thread reality (SIM-281)
rng = np.random.default_rng(20260603)
DIM, K, NLIST, NQ = 10, 25, 512, 1000      # pitch fingerprint dim, sampler k
def make(n):                               # clustered float32 so IVF has structure
    nc=50; c=rng.normal(0,6,(nc,DIM)).astype('float32'); a=rng.integers(0,nc,n)
    return (c[a]+rng.normal(0,1,(n,DIM))).astype('float32')
def lat(ix,q,k,r=3):
    ix.search(q[:8],k); b=1e9
    for _ in range(r):
        t=time.perf_counter(); D,I=ix.search(q,k); b=min(b,time.perf_counter()-t)
    return b/len(q)*1e6, I
def recall(A,T,k):
    return sum(len(set(a[:k])&set(t[:k])) for a,t in zip(A,T))/(len(T)*k)
for n in (3000,50000,250000,1000000):
    xb=make(n); xq=make(NQ)
    flat=faiss.IndexFlatL2(DIM); flat.add(xb); fl,truth=lat(flat,xq,K)
    print(f"{n} flat: 1.000 recall, {fl:.1f} us/q")
    q=faiss.IndexFlatL2(DIM); ivf=faiss.IndexIVFFlat(q,DIM,NLIST,faiss.METRIC_L2)
    ivf.train(xb); ivf.add(xb)
    for np_ in (1,8,16,32,64):
        ivf.nprobe=np_; il,I=lat(ivf,xq,K)
        print(f"  ivf nprobe={np_}: {recall(I,truth,K):.3f} recall, {il:.1f} us/q")
PY
```

---

## 9. Acceptance trace

| Acceptance criterion (SIM-114) | Where satisfied |
|---|---|
| Index-type decision with a real benchmark (flat vs IVFFlat, recall + latency, scales) | §1, §2 (measured table) |
| `nlist` / `nprobe` recommendation | §2.3 (`nlist=512`, `nprobe=32`) |
| Reconcile with SIM-300; crossover threshold justified by latency + step budget | §3 (`IVF_CROSSOVER_VECTORS=50,000`) |
| SIM-300 per-tile flat decision stated as still standing | §3.3 |
| Mandatory pre-filter as a hard requirement | §4 |
| Shared-memory strategy (shared_memory, attach-by-name, zero-copy) per SIM-281 | §5 |
| ≤ 2 GB budget regardless of worker count, per SIM-280 | §6 |
| PM approval section | §7 |

---

*End of SIM-114 spec.*
