# SIM-281 -- Phase 4 Parallelism & Shared-Memory ADR

*Ticket: SIM-281 · Owner: Performance Engineer · Date: 2026-05-27*
*Format: Architecture Decision Record (Status / Context / Decision / Consequences /
Alternatives). Consumes the budget from SIM-280
(`docs/perf/2026-05-27-ram-budget.md`).*

---

## Status

**PROPOSED** -- awaiting PM sign-off (§9) before Phase 4 (SIM-303 wiring) begins.

Resolves the two open follow-ups left by SIM-300:
- §9 "Cross-process `shared_memory` attach mechanism -> SIM-281"
- §4.2 IVF/HNSW deferral note "(SIM-281 records this as an open follow-up)"

---

## Context

Phase 4 runs a Monte-Carlo simulation loop with a hard SLA (`agent_team.md`):
**single game < 2 s, 100-game batch < 30 s**. The 100-game batch is embarrassingly
parallel -- games are independent -- so the Backend Developer's architecture already
commits to:

```python
ProcessPoolExecutor(max_workers=CPU_count - 1)
```

The constraint that makes the parallelism design non-trivial is the **SIM-114 / SIM-300 §7
hard cap: total resident play-pool + FAISS memory ≤ 2 GB regardless of worker count.**
SIM-280 quantified the budget. The load-bearing findings this ADR builds on:

- **Shared read-only payload ≈ 290 MB** (SIM-280 §4): RBF/GMM engines ~18 MB +
  situation KDTree+meta ~208 MB + LRU-capped play-pool tiles ~64 MB. All of it is
  **frozen after build** -- read-only during simulation.
- **Per-worker private overhead ≈ 150 MB** (Python interpreter + numpy + faiss +
  scipy + scratch).
- **Budget formula:** `total(W) = 290 MB + W × ~165 MB`.
  -> 1 W ≈ 0.45 GB, 4 W ≈ 0.93 GB, 8 W ≈ 1.58 GB (**PASS**); 16 W ≈ 2.86 GB
  (**RISK -- breaches 2 GB**).
- The binding constraint at high core counts is **interpreter multiplicity**, not
  data volume. The shared 290 MB is constant in W; the private 165 MB/worker is what
  grows.
- **Arsenal-cache RISK:** an exhaustive `precompute_arsenal_cache()` is ~0.58 GB
  *per process* -- must be lazy or attached read-only, never duplicated per worker.

Because the data the workers read (engine indexes, situation KDTree, play-pool
tiles) is read-only after the nightly build, sharing one resident copy across all
workers is both correct and necessary to honour the cap.

---

## Decision

### D1. Concurrency backend: keep `ProcessPoolExecutor` for Phase 4.

We **retain `concurrent.futures.ProcessPoolExecutor(max_workers = min(CPU_count - 1, 10))`**
as the Phase 4 batch concurrency primitive. The only change from the committed model
is an **explicit worker ceiling of 10** to keep `total(W)` under the 2 GB cap on
≥16-core hosts (SIM-280 §4: 16 workers = ~2.86 GB).

Rationale:

- **The workload fits the tool.** A 100-game batch is coarse-grained, CPU-bound,
  fully independent work -- exactly what a process pool does best. There is no
  inter-task communication, no dynamic task graph, no distributed scheduling need.
- **Zero new dependencies / ops surface.** `ProcessPoolExecutor` is stdlib. Ray and
  Dask add a scheduler, a cluster lifecycle, serialization layers, and version-pin
  maintenance for a single-box workload that does not need them.
- **GIL is sidestepped by processes**, which we need anyway because FAISS search and
  numpy RBF math release the GIL inconsistently and the per-game state is mutable
  Python.
- **It composes with `multiprocessing.shared_memory`** (D2) -- the same `mp`
  primitives, no impedance mismatch.
- **The SLA is met without a cluster.** 100 games / ~8 workers ≈ 13 games/worker;
  at <2 s/game that is ~25 s wall, inside the 30 s budget, with the shared-memory
  attach (D3) removing per-worker load cost.

### D2. Shared-memory layout for read-only indexes (zero-copy attach).

The ~290 MB read-only payload is built **once in the parent** and published into
named `multiprocessing.shared_memory.SharedMemory` segments; workers **attach**
(do not copy) them. This is the mechanism that keeps the 290 MB resident **once**
rather than `W ×` (SIM-280 §4).

Layout, per artifact kind:

1. **FAISS play-pool tiles (pitch + batted-ball, SIM-301).** Tiles are flat
   `IndexFlatL2` + `.npy` rowids and are already mmap-friendly (SIM-300 §7). Two
   compatible options; we choose **(a) mmap of the on-disk tile files** as the
   default because it is the simplest zero-copy path and the OS page cache already
   shares the pages across processes:
   - The vector block of a serialized `IndexFlatL2` is a contiguous float32 array;
     the sampler attaches it with `faiss.read_index(path, IO_FLAG_MMAP)` (or
     reconstructs an index over an mmap'd `np.memmap` of the raw vectors).
   - `.rowids.npy` is attached as `np.load(path, mmap_mode='r')`.
   - Because tiles are immutable between nightly builds, the kernel keeps one set of
     physical pages and maps them read-only into every worker -> one resident copy.
   - The LRU `max_resident_tiles=256` cap (SIM-302) then bounds *mapped* tiles, so
     resident tile RAM stays ≤ ~64 MB shared (SIM-280 §3).

2. **Situation KDTree (~208 MB) + RBF/GMM engine matrices (~18 MB).** These are
   in-memory numpy structures, not files. Publish their backing buffers into named
   `SharedMemory` segments in the parent after `build()`, then have each worker
   reconstruct the lightweight wrapper objects over `np.ndarray(shape, dtype,
   buffer=shm.buf)` (zero-copy view). For the KDTree specifically, share the
   underlying `N×11` float64 data buffer and rebuild the `scipy.spatial.KDTree`
   index header in each worker over that shared buffer (the tree nodes are small
   relative to the 88 MB data; the 88 MB data is the part that must not be
   duplicated). The `list[NearestSituation]` metadata is replaced by column-parallel
   numpy arrays in shared memory (this also reclaims the ~100 MB flagged in
   SIM-280 §2.1).

3. **Arsenal cache (pitcher engine).** Either run **lazy** (default, ~15 MB/worker,
   acceptable) or, if pre-warmed, `ArsenalCache.save()` it once and attach the
   pickled dict read-only via a shared segment. Never exhaustively precompute per
   worker (SIM-280 §2.2).

Ownership & lifecycle: the **parent process owns** every shared segment, calls
`shm.close()` in workers and `shm.unlink()` exactly once at parent shutdown. A
small registry (segment name -> (shape, dtype)) is passed to workers at fork so
they attach by name. All segments are mapped **read-only** in workers; no worker
ever writes them, which keeps the contract sound and avoids copy-on-write page
faults that would silently re-duplicate memory.

### D3. Index type stays `IndexFlatL2` for Phase 4 (IVF/HNSW deferred).

Confirming SIM-300 §4.2: tiles ship as `IndexFlatL2`. The pre-filter
(`pitcher_id + bat_hand`) already does the coarse partitioning IVF would provide,
tiles are small (200--5,000 vectors), and flat L2 is exact, training-free, and
trivially mmap-able (which D2 depends on). **Revisit IVF/HNSW only if** a tile's
`n_vectors` regularly exceeds ~50k (it does not under the current pre-filter) or
profiling shows FAISS search -- not interpreter overhead -- as the SLA bottleneck.

---

## Worker startup sequence and tile-load timing (AC #4)

Recommended **cold-start sequence** (parent then workers):

```
PARENT (once):
  1. Build / load the read-only engine payload:
        - RBF/GMM engines .build()      (~18 MB)
        - SituationSimilarityEngine.build()  -> KDTree data (~88 MB) + col arrays
  2. Publish read-only payload into named SharedMemory segments (D2.2).
     (Play-pool tiles are NOT loaded here -- they live on disk and are mmap'd
      lazily by workers; see step 5.)
  3. Fork the pool:  ProcessPoolExecutor(max_workers=min(CPU-1, 10)).

EACH WORKER (at first task):
  4. Attach the shared segments by name -> zero-copy numpy views; rebuild the
     KDTree header over the shared buffer. Construct a PlayPoolSampler with
     max_resident_tiles=256 and a read-only DuckDB connection.
  5. LAZY tile load: on the first sample_pitch / sample_batted_ball for a given
     (season, pitcher_id, bat_hand), PlayPoolSampler.load_tile() mmaps that tile,
     inserts it into the worker's LRU (evicting LRU tile if at 256). Tiles are
     mapped read-only; the OS page cache shares physical pages across workers.
```

**Lazy vs eager preload -- recommendation: LAZY, with one narrow exception.**

- **Lazy (default).** A single game touches only the handful of tiles for the two
  starting pitchers + the league-average fall-back tiles. Eagerly preloading all
  tiles would defeat the LRU cap and load megabytes the game never samples. Lazy
  `load_tile()` on first use is the right default and is exactly what SIM-302's API
  is designed for (idempotent, LRU-evicting). First-touch mmap cost is a page-fault,
  not a deserialize -- sub-millisecond and amortized by the page cache once any
  worker has faulted the page in.
- **Narrow eager exception.** If a batch is known to be many games of the *same*
  matchup (e.g. prop-betting on one starter), the runner MAY warm that matchup's
  tiles once before fanning out, so the first game in each worker does not pay the
  cold page-fault. This is an optimization, not the default.

**Where SIM-302's methods fit:**
- `load_tile()` -- step 5; the lazy first-touch attach + LRU insert.
- `sample_pitch()` / `sample_batted_ball()` -- the per-pitch hot path; each calls
  `load_tile()` internally (idempotent) then does the k-NN + distance->weight draw.
- `reload_recent(current_season)` -- **not used during a batch.** It exists so a
  long-lived worker can pick up nightly current-season rebuilds without restart
  (SIM-300 §5). In the Phase 4 batch model, workers are short-lived per batch, so
  `reload_recent()` is only relevant to a persistent/daemonized runner; if such a
  runner is added, it must re-publish the affected shared segments from the parent
  (workers must not mutate shared memory), then signal workers to re-attach.

---

## Consequences

**Positive**
- Honours the 2 GB cap: total = 290 MB shared + W × 165 MB private; PASS to ~8--10
  workers (SIM-280 §4). The shared payload stays flat in W -- the entire point of D2.
- No new runtime dependencies, no cluster to operate, stdlib-only.
- Read-only shared memory + mmap'd tiles = zero-copy attach; the kernel page cache
  gives free cross-worker sharing of tile pages.
- Composes cleanly with the existing `ProcessPoolExecutor(CPU-1)` commitment.

**Negative / costs**
- A **10-worker ceiling** caps batch throughput on ≥16-core hosts; on such a host
  we leave cores idle to stay under the cap. Acceptable -- the 100-game SLA is met
  at ~8 workers; raising the cap needs a per-host RAM check, deferred.
- Implementation work: the situation engine's metadata `list` must be converted to
  column-parallel numpy arrays to be shareable (also reclaims ~100 MB -- SIM-280
  §2.1). KDTree header must be rebuilt per worker over the shared buffer.
- mmap'd FAISS tiles depend on tiles staying immutable between builds (they are, by
  SIM-300 §4.4 atomic-write contract) -- a non-atomic rebuild during a live batch
  would be a correctness hazard; the nightly cron runs off-hours, so this is safe.

**Neutral**
- IVF/HNSW remains a future ticket; flat L2 is correct and sufficient now.

---

## Alternatives considered

| Option | Why not (for Phase 4) |
|---|---|
| **Ray** | Built for distributed/dynamic task graphs and actor state across machines. Its object store *would* give shared-memory zero-copy for numpy -- attractive -- but it adds a scheduler process, a cluster lifecycle, serialization rules, and dependency-version maintenance for a single-box, statically-partitioned, embarrassingly-parallel batch. The complexity buys nothing the `ProcessPoolExecutor` + `shared_memory` combo doesn't already give us here. **Reconsider when** simulation must scale beyond one host (multi-node batches) or needs a dynamic task graph (e.g. adaptive resampling). |
| **Dask** | Same verdict, different flavor: excellent for out-of-core dataframes and lazy task graphs. Our hot path is in-RAM numpy/FAISS, not a dataframe graph; Dask's scheduler overhead and chunk model add latency to a sub-2s-per-game loop. **Reconsider when** the play pool no longer fits the 2 GB envelope and must be processed out-of-core, or when chained heterogeneous stages need a task graph. |
| **`ProcessPoolExecutor`, copy payload per worker (no shared memory)** | Simplest to code but **breaks the cap**: 290 MB × W instead of +290 MB once -> ~3.3 GB at 10 workers. Rejected on SIM-280 grounds. |
| **`ThreadPoolExecutor` (shared address space, no IPC)** | Free sharing, but the GIL serializes the mutable-Python per-game state and FAISS/numpy GIL release is inconsistent -> no real CPU parallelism for the loop. Rejected. |
| **Single process, vectorize all games** | Cannot hit the 100-game SLA on one core and complicates per-game RNG/state isolation. Rejected. |

---

## 9. PM sign-off (required before Phase 4 / SIM-303)

This ADR and its budget basis (SIM-280) must be signed off by the Product Manager
before Phase 4 wiring begins.

- [ ] **Reviewed & approved** -- backend = `ProcessPoolExecutor(max_workers=min(CPU-1, 10))`,
      read-only payload shared via `multiprocessing.shared_memory` + mmap'd tiles,
      flat `IndexFlatL2` retained, lazy tile load.
- [ ] Acknowledged the **16-worker RISK** (SIM-280 §4: ~2.86 GB) and the **10-worker
      ceiling** mitigation.
- [ ] Acknowledged the **arsenal-cache RISK** (SIM-280 §2.2): pitcher engine runs
      lazy or attaches a pre-built cache read-only; no per-worker exhaustive precompute.

**Signed off by (PM):** ______________________   **Date:** ____________

---

*End of SIM-281 ADR.*
