> **SUPERSEDED-BY** `docs/audit/2026-06-01-roadmap-sim430-429-411-413-425b-427.md` — the measured root cause was fork-COW-defeat → `forkserver` (NOT the object-dtype / `_pool_meta` duplication this doc theorizes).

# SIM-430 — full-pool `/simulate` fan-out / OOM design (the open P1 half)

**Date:** 2026-05-31 · **Owner:** Performance Engineer (Agent 6) · **Status:** design (implementation not started)

## Scope

SIM-430 has two halves:

1. **Per-game cost** — DONE (commit `4cb27c5`): cached the per-PA constants in
   `FullPoolSampler` → **1.21x** (1846→1530 ms/game, 5-trial median). Byte-identical draws.
2. **Fan-out / OOM** — THIS DOC, OPEN. The n-iteration `/simulate` request does not parallelize
   at 1 worker (n=100 ≈ 153 s serial after half 1), and bumping `SIM_RUNNER_WORKERS` to 10
   OOM-deadlocks the 15.5 GiB host. The 2 s/game · 30 s/100-game SLA needs ≈5x parallelism.

## Why 10 workers OOM (root cause)

SIM-403b already publishes the **41 big read-only numpy arrays (~166 MB)** of the engine-artifact
bundle into `multiprocessing.shared_memory` and attaches them zero-copy in every worker
(`EngineArtifacts.extract_shared_arrays` / `attach_shared_views`). So the *raw* pool geometry is
shared, not duplicated. What is **NOT** shared and is therefore duplicated × N workers:

| Per-worker structure | Source | Approx size (all-seasons) | Shareable today? |
|---|---|---|---|
| `outcome_type` (pitch pool) | object-dtype `str` array, ~935K rows | ~54 MB (≈58 B/str) | **No** — object dtype |
| `event` (batted-ball pool) | object-dtype `str` array, ~156K rows | ~9 MB | **No** — object dtype |
| `pitcher_sim` dict-of-dicts | per-worker disk load | small–moderate | No (Python dict) |
| `_pool_meta` derived precompute | `balls`/`strikes` int64 + `sit_baseout` float32, ×2 hands | ~60 MB | Not yet (derived) |
| `_aff_cache` (per-batter RBF affinity) | grows per game, float32 `n` each | ~3.7 MB × batters | No (per-request) |

So each worker carries **~125 MB+** of duplicated/derived state on top of the shared 166 MB. At 10
workers that is **~1.25 GB+**, layered on DuckDB (all-seasons, multi-GB resident), the 6.3M-pitch
FAISS index, Postgres, Redis, and the app's own heap. The lifespan **pre-warm warming all workers at
once** spikes RSS hardest — one worker gets OOM-killed, the `ProcessPool` loses a worker mid-task and
**deadlocks**, and every `/simulate` then TimeoutErrors (>400 s). This is exactly the failure recorded
in CLAUDE.md; the host `.env` is pinned to 1 worker as the safe floor.

## Approaches considered

**A. Category-code the object-dtype arrays (`outcome_type`, `event`).**
Replace the `str` object arrays with `int8`/`int16` category codes + a tiny shared category table
(≤ ~20 distinct outcome labels). Effect: (1) makes them **shareable via SIM-403b** (int arrays go
into `shared_memory`), removing ~63 MB/worker; (2) **shrinks them ~14x** even unshared (935K×1 B vs
58 B); (3) turns the per-PA string equality/`np.isin` work into integer ops (a small per-game speed
win too). **Cost:** requires baking codes into the engine-artifact bundle → a **play-pool rebuild**
(same dependency as SIM-411/413/425b). **Risk:** low (mechanical); the draw maps codes→labels at the
boundary. **Leverage: highest.**

**B. Publish the derived `_pool_meta` precompute into shared memory (extend SIM-403b).**
`balls`/`strikes`/`sit_baseout` are pure deterministic functions of the shared `sit` array. Compute
them ONCE in the parent and publish/attach them like the SIM-403b arrays, so workers share them
zero-copy instead of each rebuilding ~60 MB. **Cost:** moderate (extend the extract/attach contract +
have the sampler read attached meta when present). **Risk:** low–moderate. **Leverage: high.** No
play-pool rebuild needed → **shippable independently of A.**

**C. Adaptive worker cap by available RAM + keep the staggered pre-warm.**
Pick `workers = clamp(cores-2, fit_by_ram(per_worker_budget, free_ram))` instead of a fixed 10, and
keep the bounded-concurrency pre-warm (already shipped). With A+B shrinking per-worker marginal memory
to ~scratch, **4–6 workers fit** in 15.5 GiB → ~4–6x. **Cost:** low. **Risk:** low. **Leverage:
medium but essential as the safety rail.**

**D. Thread-pool instead of process-pool.** Shared address space → no per-worker duplication. But the
per-PA draw is Python-level (count-bucket CDF selection, dict lookups) holding the GIL; only the big
numpy reductions release it. Expected speedup is sub-linear and fragile. **Rejected** as the primary
lever (revisit only if the hot path is fully vectorized — see E).

**E. Intra-request vectorization across iterations.** Run the n game-iterations in lockstep,
batching the pool-scoring matrix ops across iterations. Potentially large speedup with **zero** extra
processes. **Cost: very high** (a sim-loop rewrite to a batched state machine). **Deferred** — biggest
payoff, biggest risk; only after A+B+C are exhausted.

## Recommendation (staged)

1. **B + C first** (no play-pool rebuild, shippable now): share the derived precompute + adaptive
   worker cap + RAM-fit. Target 4–5 workers safely on this host → n=100 from ~153 s to ~35–45 s.
2. **A next**, folded into the next play-pool rebuild (with SIM-411/413/425b): category codes remove
   the last big per-worker chunk and add a small per-game win → enables 5–6 workers → n=100 ≈ 25–35 s,
   i.e. **the 30 s SLA becomes reachable** combined with the 1.21x per-game cut.
3. **E** only if 1+2 still miss the SLA on production hardware.

## Validation plan

- Re-measure RSS-per-worker before/after B (parent + N workers) on the all-seasons bundle.
- n=1 / n=10 / n=100 wall-clock at 1/4/6 workers, watching for OOM-kill (the SIM-404 stress lane).
- Byte-identical draw check preserved (the SIM-430 equivalence test pattern).
- Hard-gate `/simulate` SLA under `PERF_STRICT` once a worker count is chosen.

## Dependencies / tickets

- **A** is blocked on a play-pool / engine-artifact rebuild (shared with SIM-411 park-factor,
  SIM-413 pitcher-hand, SIM-425b fielder RBF). **B + C** are independent and can ship first.
