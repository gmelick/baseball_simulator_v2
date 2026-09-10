# SIM-474 era — resumption state

**Written 2026-08-17 so this work can restart in a fresh context.** Read this, then the
2026-08-17 banners in `BACKLOG.md`. Next free ticket id: **SIM-506**.

---

## The one-paragraph version

Steals are back. SIM-468 built the opportunity pool (the denominator), SIM-474 replaced the
green-light gate + resolver stub with a similarity-weighted draw, SIM-483 fixed the steal-run
box credits, SIM-504 wired two of three `raw.play_events` consumers, SIM-505 fixed the lucky
test fixture. All committed and pushed; CI green on every push. **The certifying lane COMPLETED
2026-08-17 07:46Z (container `70ba9224d2a6`, kept for the logs): attempt volume CERTIFIED at
0.748/team-game vs MLB 0.76 (-1.6%); the safe/caught split CONFIRMED high (88.1% vs ~77.6%,
SB +11.7% / CS -47.8%) — the SB/CS bands stay open on the split alone. Next work: the
kernel-bandwidth fit (decision-tree branch three, below). Runs stayed green with steals live.**

---

## THE IN-FLIGHT LANE — read this first

Launched 2026-08-17 ~23:30 with production flags, `SIM_ACCEPTANCE_TIMEOUT=16200`, live
`tests/` + `scripts/` mounted. ~3.2 h sim phase; pytest reports nothing until it finishes.

```bash
docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' 70ba9224d2a6
docker logs 70ba9224d2a6 2>&1 | tail -40          # the scoreboard once it exits
docker logs 70ba9224d2a6 2>&1 | grep -oE "BandResult\(channel='(SB|CS)'[^)]*" | sort -u
```

**The decision tree on its results:**

* **SB and CS bands PASS** → SIM-474 fully closes. Update the BACKLOG row, CLAUDE.md §11's
  steals paragraph ("do not claim the bands pass until…" — they now do), commit.
* **SB/CS inside their bands but UNRESOLVED/UNDERPOWERED** → the lane needs the longer run for
  those channels; record the means, leave the rows open, no code change.
* **The safe/caught split confirms high** (the 600-sim smoke read 89% success vs MLB ~78%;
  SB high / CS low) → this is a KERNEL-BANDWIDTH question, NOT a knob to eyeball. The suspect
  mechanism: the catcher-similarity factor down-weights caught-stealing rows because they
  over-represent tail strong-arm catchers. `steal_sigma` / `steal_score_sigma`
  (`simulation/full_pool_sampler.py`, ~line 90) are declared SIM-476 temperature-fit targets —
  fit them against held-out data, do not hand-tune. An honest interim option the owner may
  approve: soften ONLY the catcher factor's effect on the success split by measuring, not
  guessing.
* **Other channels moved** (DP, R interact with the running game) → new findings; file before
  fixing.
* **The lane ERRORS at setup** → check the probe rename: the conftest wraps
  `_steal_opportunity_draw` now (not `_full_pool_steal_decision`).

The lane holds the DuckDB write lock while running — profile-computor writes fail with
"Conflicting lock" until it exits. Read-only connections work
(`duckdb.connect(path, read_only=True)`).

---

## What landed today (all pushed, CI green per push)

| Commit | What |
|---|---|
| `0938166` | SIM-468 (opportunity pool, migration 0015, schema v15, ~2.37M rows built) + SIM-474 (the draw; gate/fallback/`_STEAL_ATTEMPT_K` deleted) + SIM-495 guards removed + SIM-505 filed. Smoke: SB 0.70 + CS 0.09 = 0.79 att/team-game vs MLB 0.76. |
| `ec88ba2` | SIM-483: steal runs earn no RBI (Rule 9.04(b)); non-terminal steal-of-home now charges ER (Rule 9.16(a)). |
| `c496a9c` | SIM-504 items 1+2 (intent-walk DECISION documented at five sites; pickoff outs → both IP consumers via `_play_events_outs_cte`, EXPLAIN-validated) + SIM-505 closed (the `_injected_battedball` seam). |

## Key mechanics a new context must not re-derive

* **The steal draw**: `sim_loop._steal_opportunity_draw` → `FullPoolSampler.steal_draw`.
  Hard filter = target base (pre-split "2"/"3") + exact (outs, balls, strikes) cell. Weights =
  runner/pitcher-hold/catcher-arm gaussian kernels over z-scored steal-feature subsets +
  soft score kernel + recency + manager aggression (leverage-scaled tendency ÷ 0.08, clamped
  [0.05, 4]) multiplying ATTEMPTED rows only. One drawn row answers attempt AND outcome.
* **Artifact**: `steal_pool/{2,3}.sit.npy + .meta.parquet` under engine_artifacts; loaded into
  `EngineArtifacts.steal_pools`; shared-memory published (`steal_pool.<t>.<attr>`); the
  `pitcher_steal` embedding rides `_ACTOR_TABLES` (keys `pitcher_id:season`).
* **Rebuilding the pool after new data**: run `_build_steal_opportunity_pool` (wired into the
  computor's pool phase) then `python -m pipeline.batch.engine_artifacts --what pool` and
  `--what actors`. Remember the season-default trap: bare computor runs = current season only.
* **The loop's injection seam**: an injected resolver's `resolve_fielding` is consulted ONLY
  when the resolver carries `_injected_battedball`; otherwise in-play pitches on a sampler-less
  machine resolve to a terminal nothing (SIM-505's root cause).
* **EXPLAIN validation harness**: capture the computor's SQL with a recorder conn, then
  `EXPLAIN` the WITH-body on a read-only DuckDB with postgres attached — caught a real
  ambiguous-column bug this session. Reuse it for any profile-SQL change.

## Open queue after the lane verdict

1. **SIM-429** — 2B +8.2% / BB +10.8% / ROE +4.9% highs; K-prop ECE 0.109 → bet-grade; then
   the CLV re-measure (reconcile the `wave1-remediation` branch's CLV-instrument fixes first).
2. **SIM-491** — re-tune `_FIELDER_RBF_PER_OAA`/cap on the real post-recompute OAA scale
   (needs the CPU the lane is using; ≥400-sim sweeps).
3. **SIM-504 item 3** — hold-runner rates into the pitcher-steal engine (engine-feature change:
   migration + regression regen; SIM-476 era).
4. **home_win_pct certification** — the 12×2,168 (~16 h) run; or supersede via **SIM-497**
   (the date-range backtest, which also serves the CLV re-measure).
5. **SIM-459-adjacent**: the next full recompute picks up the SIM-504 IP wiring and the uBB
   walk rates (all inert until then).
