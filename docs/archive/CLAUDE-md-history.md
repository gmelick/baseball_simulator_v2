# CLAUDE.md history (2026-05 to 2026-10)

This file holds the dated status narrative that `CLAUDE.md` carried until 2026-10. It is a
record, not a guide: much of it is superseded. The current state is `CLAUDE.md` §2b and
`BACKLOG.xlsx`; the full change record is `CHANGES.md`. The text below is verbatim from
`CLAUDE.md` at commit 1447211; its `CLAUDE.md:<line>` references point at the file as it
was when each line was written.

## From §2 (Current status)


- **▶ STATE AS OF 2026-06-06 — SUPERSEDED where §2b (2026-08-16) says otherwise: data foundation rebuilt, the runs band PASSES, CI all-green.**
  - **Phases 1–6 COMPLETE and CI-green** on **Python 3.13 / numpy 2.x** (SIM-431). Frontend shipped as
    **React 18 + Vite + TypeScript** (SIM-378 / ADR-001). DuckDB schema **v32** (2026-10-08, SIM-561; was v31), Alembic head **0030** (2026-10-09, the game page review, SIM-562 — the what-if run columns on `sim.sim_runs`; was 0029, SIM-519).
  - **Calibration is LIVE, REFIT 2026-08-16 on the rebuilt data** (SIM-432/459): `/data/calibration.json`
    fitted + applied at boot; win-prob map = fitted reliability-curve. 120-game validation: win-prob
    **ECE 0.0377** (was 0.047); batter **H/HR/TB 0.066/0.024/0.060** (bettable); pitcher **BB 0.044 —
    now bettable (was 0.21)** and **K 0.109 (halved from 0.22, not yet bet-grade)**.
  - **The sim path = the full-pool similarity sampler**, the ONLY path since SIM-486 (2026-09-06) deleted the per-tile
    fallback + `SIM_FULL_POOL` (tests use `simulation/synthetic_bundle.py`). **All production realism flags are ON** in the docker-compose `app`
    env and **pinned OFF in `tests/conftest.py`** (so CI + the flag-off baseline stay byte-identical):
    `SIM_MANAGER` (SIM-434 manager pull/reliever decisions), `SIM_PARK_FACTOR` / `SIM_BB_PLATOON` /
    `SIM_FIELDER_RBF` (SIM-411/413/425b realism nudges), `SIM_FRAMING` (default ON). All enabled +
    validated 2026-06-04 (manager: pitchers/game 2→9, runs unchanged; realism: no run distortion).
    **SIM-427** manager-USAGE capstone deployed (7th feature `available_reliever_usage_rate`,
    `sigma_usage=1.030`, profiles recomputed all 10 seasons). **SIM-433** bullpen-availability ingest
    complete (21,612 games).
  - **CLV pipeline COMPLETE + measuring at scale (the fund's gold-standard metric).** **SIM-435**: full
    2024-season historical odds backfilled (**2,378 games**, real BettingPros opening+closing — game
    moneyline/run-line/total + 7 props). **SIM-429**: the CLV backtest scoreboard shipped
    (`scripts/clv_backtest.py`) — sim → model prices → opening/closing → CLV per market/side, trust-labeled.
    **First result (120 games, PRE-rebuild): ~49% beat-close = no edge THEN** (stable n=65/n=100;
    trustworthy markets all ≤50%). That read predates the 2026-08 data rebuild — RE-MEASURE it; the
    remaining **SIM-429** work (run-conversion gap CLOSED 2026-08-16 + sharpen the edge
    estimates) is the path to a real edge. The full-season CLV run is executing. *(This bullet said "~10–12%" until 2026-08-10; see the §11 reconciliation note.)*
  - **`/simulate` perf (SIM-430 / SIM-436):** forkserver workers + a 10 GB app `mem_limit` → **n=100 ≈
    38 s at 6 workers, no OOM**. SIM-436 PROFILED the per-game cost: it is the IRREDUCIBLE per-PA
    full-pool scoring (~1.5–1.9 s/iter × ~83 PAs); the machine build is free and the host is **core-bound
    at ~6**, so a single game can't go <30 s on this hardware without fewer iters or a smaller pool. The
    throughput fix: **the CLV backtest is parallelized ACROSS games** (`--workers`, forkserver,
    byte-identical, ~373 MB/worker) → ~6× → **~20–32 s effective/game**; n=65 gives the same CLV as n=100.
  - **Next free ticket ID: read it in `BACKLOG.xlsx` — its subtitle row carries it (SIM-542 as of 2026-09-10).** Open work then: **SIM-429** (granular run-conversion + K/BB prop
    calibration to DEVELOP a CLV edge — the now-measurable gold-standard says there is none yet); the
    realism follow-ons fold into it (≥400×≥20 magnitude calibration of the SIM-411/413/425b nudges; wiring
    the real per-team SIM-427 profiles into the SIM-434 decision model, which currently uses a league-flat
    default). **CLOSED:** SIM-435/433/434/427/411/413/425b/430/432/431 + **SIM-437** (2026-06-22: the two
    ETL loaders' duplicate `_to_float`/`_to_int`/`_to_bool`/`_to_str` coercion helpers consolidated into
    `pipeline/etl/coercion.py`) + the 2026-06-03 comprehensive-audit remediation (**`backlog.xlsx` RETIRED
    → `BACKLOG.md` is the single source of truth**).
- **Phases 1–5 + Phase 6 Frontend Build (SIM-378→401 + hardening 415→420) are COMPLETE and CI-green
  on Python 3.13 (migrated from 3.11.15 — SIM-431, 2026-05-31; numpy is now 2.x).** Unit suite green (the unit lane runs the per-tile path; see below).
- **2026-05-28 closure batch — SIX P1/P2 tickets closed in one day:**
  - SIM-403 — real parallelism (worker-count fix: `SIM_RUNNER_WORKERS` unset → `default_max_workers()`)
  - **SIM-403b** — `EngineArtifacts.{extract,attach}_shared_views` for zero-copy across workers
    via `multiprocessing.shared_memory` (publishes 41 arrays = ~166 MB in the lifespan)
  - SIM-404 — stress/concurrency/leak suite (5 slow-marked integration tests)
  - SIM-409 — `LineupNotIngestedError` → 503 + `Retry-After: 900`; `lineup_ready: bool | None` on `GameCard`
  - SIM-414 — W/L/S + ER + per-runner R reconciliation (sub-5-IP starter rule, inning-reconstruction
    unearned runs, walk-forced R credit)
  - **SIM-412** — home-field run advantage (`_apply_home_field_bias` flips HOME batted-ball outs
    to singles at default 0.025; env override `SIM_HOME_FIELD_BIAS`).  Tuning note: 4×400-sim
    harness run shows current default slightly overshoots (delta R = +0.198 vs target +0.13);
    a future tweak to ~0.017 would land closer.
- **Similarity-engine-wiring / full-pool realism epic (SIM-422→429) — LANDED on `master`.** The
  simulator scores the **entire same-hand play pool** by the applicable similarity engines (no top-K;
  the batter's hand is the only hard filter — the pitcher hand self-zeroes via the pitcher engine)
  and is the **only path** (SIM-486 deleted the per-tile fallback and the `SIM_FULL_POOL`
  switch on 2026-09-06; tests draw from a synthetic in-memory bundle).
- **DP-rate bug fix propagated 2026-05-28:** the player-profile computor's
  `dp_turned = outs_on_pitch >= 2` always-False bug was fixed and the 5.7-hour 2017-2025
  recompute completed.  Per-season DP rates now 42-48% (was 0.0).  Actor embeddings rebuilt
  (`fielder_emb` = 11346 × 51 features).  Box output now MLB-realistic: H/HR/2B/BB/K within
  ~3-5% of MLB-2023.  **Runs run ~7-8% low** (down from ~12% pre-fix) — CLOSED 2026-08-16: after SIM-459 the acceptance lane measured R INSIDE its band (the strict xfail XPASSed and was deleted).
  This figure stays on THIS line as the floor-sizing anchor `tests/acceptance/bands.py` cites as
  CLAUDE.md:85 — the R floor still rejects a 7% shortfall so a REGRESSION to the old gap reds.
  ⚠ **This bullet used to end "steals match MLB volume" — wrong from 2026-06-04 (the manager
  green-light closed the steal gate; SIM-495 measured SB/CS exactly 0.0000 across 400 game-sims)
  until 2026-08-17, when SIM-468/474 LANDED:** the steal decision is now a similarity-weighted
  draw from `sim.steal_opportunity_pool` (~2.37M per-pitch opportunities, attempted or not). The
  600-sim smoke reads **SB 0.70 + CS 0.09 = 0.79 attempts/team-game vs MLB 0.76**; the safe split
  (89% vs ~78%) is a certifying-lane question before any kernel bandwidth moves.
  **Next free ticket ID at the time: SIM-433** (SIM-542 as of 2026-09-10 — `BACKLOG.xlsx` is now
  the authority, see §2b; SIM-430 = the full-pool `/simulate` throughput / 2s-30s SLA perf gap,
  filed 2026-05-30 off the SIM-402 live re-measure).

- **SIM-402 — CLOSED 2026-05-30 (code complete + re-measured live); the residual throughput
  gap is spun off to SIM-430.** Live API probed at
  `http://localhost:8000/api/games/{pk}/simulate`.
  - **Cold-worker fix shipped.** `production_machine_factory` passes `fingerprint_deriver=None`
    on the full-pool path (the deriver is unused there but `_default_deriver_builder` did 3 eager
    per-seed disk loads), and a BACKGROUND pre-warm (`BatchRunner.prewarm()` +
    `production_factory.warm_worker_cache()`, bounded-concurrency
    + a `_get_pool` lock) populates each worker's per-process full-pool cache off the request path.
    This eliminated the n=10 ≈ **498-507s** cold-fan-out stall (a fresh n-iteration request used to
    spread games one-per-worker, so the per-worker cache never warmed in time and every worker paid
    the full artifact-load + per-hand-precompute on the request path).  +13 unit tests
    (`tests/unit/test_sim402_prewarm.py`); ruff + mypy clean.
  - **Live re-measure (2026-05-30, all-seasons DuckDB, 1 worker):** warm n=1 ≈ **2.2-2.3s**,
    n=100 ≈ **215s** serial.  The 2s-game / 30s-batch SLA is **NOT met** on the full-pool path —
    per-game cost is ~2.2s and the n-iteration fan-out does not parallelize at 1 worker.
  - **`SIM_RUNNER_WORKERS=10` is non-viable on this 15.5 GiB host:** a pre-warm worker is
    OOM-killed → the ProcessPool deadlocks → every `/simulate` hangs >400s (the 10-worker
    re-measure returned all-n TimeoutError).  The host `.env` is pinned to **1 worker**, with the
    reason documented inline there.
  - **Remaining work → SIM-430** (new perf ticket): cut the full-pool per-game cost and/or give
    `/simulate` a fan-out that scales without OOM (lighter per-worker footprint or intra-request
    game batching).

- **2026-06-01 session — SIM-432 CLOSED: CALIBRATION IS NOW LIVE.** The SIM-406 fit + SIM-407
  validate **scripts** were reconciled to the live SIM-408-trimmed schema and **actually run on the
  running stack**, so `/data/calibration.json` now exists and is applied at boot. Ground truth was
  read from the LIVE containers (not the `.sql` files, which diverge from the rebuilt all-seasons
  DB) — which corrected the filed cascade: `first_pitch_take_rate`/`max_exit_velo`/the batter
  `*_vs_r` block are PRESENT post-rebuild (so cascade item (c) was stale); only the pitcher
  `RESULT_FEATURES` import (b) and the `raw.games` final-score columns (d) were real divergences,
  plus a degenerate-sigma regression guard surfaced by running the fit. **Boot now logs**
  `build_all_engines: 11/11` → `Loaded calibration report from /data/calibration.json` →
  `SIM-346: applied … ARSENAL_SCALE=4.0655` → `SIM-406: applied fitted calibration to 8 engines;
  win-prob map: reliability-curve(2026..2017)` (was `No CalibrationReport found … identity`). Fit:
  arsenal median W₂ 2.818, σ_command 1.078, 7 keep-default sentinels; validate: 60 games of 2024,
  win-prob ECE 0.171, 7 reliability anchors. See `docs/audit/2026-06-01-sim432-calibration-schema-reconciliation.md`.
  ⚠ Caveats (NOT SIM-432): the win-prob curve is fit over a bounded 60-game sample (full-pool replay
  ~2 s/iter — the open SIM-430 gap; a fuller multi-season fit is a follow-up batch), and pitcher
  K/BB props are over-predicted (→ SIM-429). **The §11 SIM-406/407/432 lines below predate this
  closure where they still say "NOT live / identity" — treat THIS bullet as authoritative.**
- **2026-05-31 session — Python 3.13 migration + SIM-430 part-1 landed.** What landed + pushed
  (commits `2f4a8f1`..`1d60fb1`):
  - **SIM-431 — CLOSED.** Whole platform migrated to **Python 3.13 + numpy 2.x** (CI + Docker +
    local unified). The real blocker was the `numpy<2` pin (no cp313 wheel), not version strings;
    floors raised (scipy≥1.14.1, pandas≥2.2.3, scikit-learn≥1.6, faiss-cpu≥1.9, POT≥0.9.5).
  - **SIM-430 — WORKER-SCALING RESOLVED 2026-06-02 (workers no longer OOM); 30 s SLA not fully
    met (plateaus past ~6 workers).** Three parts: (1) per-game caching 1.21x; (2) densified
    `pitcher_sim` (the ~2 GB dict → an 11.2 MB shared matrix via the SIM-403b seam; load
    2364→367 MB, parent 2.4 GB→270 MB, byte-identical); (3) **the real OOM-at-scale fix** — the
    pool used the default **`fork`**, so each worker COW-forked from the ~6 GB engine-loaded
    parent and CPython refcount/GC **defeated copy-on-write**, materialising ~6 GB/worker (10
    workers OOM-deadlocked). Fixed by **`mp_context=forkserver`** in `BatchRunner._pool_kwargs`
    (workers fork from a lean ~30 MB server → **373 MB each**, measured; env `SIM_MP_START_METHOD`)
    + a **`mem_limit: 10g`** cgroup cap on the `app` service (a runaway is contained to the
    container, never the host). Live: **n=100 `/simulate` 215 s → ~38 s (5.6×)**, healthy, no OOM;
    `SIM_RUNNER_WORKERS=6` (8 gave no gain). ⚠ **30 s SLA NOT met** — throughput plateaus past ~6
    workers (6 ≈ 8 ≈ ~38 s): a serial bottleneck (parent-side result un-pickling/aggregation +
    per-game machine rebuild), the remaining SIM-430 "per-game cost" work — NOT a worker-memory
    issue. +4 tests (`test_sim430_forkserver.py`). ⚠ Tracing this, `PYTHONTRACEMALLOC=1` on the
    live multi-GB boot wedged Docker Desktop (reverted; `mem_limit` now prevents that class of
    accident — use memray/py-spy in a capped container, never interpreter-wide tracemalloc on the
    live boot). Diagnosis: `docs/audit/2026-06-01-roadmap-sim430-429-411-413-425b-427.md`.
  - **SIM-432 — FILED 2026-05-31, CLOSED 2026-06-01 (see the top bullet of this section).**
    Calibration is now live; the cascade is fully resolved.

## From §2b (the landings paragraph and the open board of 2026-09-10)

Landed since the old handovers: SIM-510..513 (the transition draw — the drawn row is the
play, mechanically), SIM-515 (IBB is a per-PA draw at `sim.ibb_rates`' real cell rates; the
2.64× formula deleted), SIM-516 (the pool-totals grade + the W1 window), the SIM-491
draw weights (the home weight `SIM_HOME_OFF_WEIGHT` and the park kernel
`SIM_PARK_KERNEL_SIGMA`, fitted by SIM-476; the fielder kernel was retired 2026-09-09 for
the fielder score matrix), and the framing flip OFF (its weight rebuild is SIM-517). The 2026-08-29 hygiene sweep closed or
merged 8 stale rows. SIM-456 CLOSED 2026-09-04 (the whiff fix was live since the SIM-459
recompute; the platoon z-swing legs' inversion found in its close is SIM-522, CLOSED the same
day by owner decision: fixed in code, the data lands with the next recompute; the D-N7
csw/whiff overlap is parked on SIM-429). **The open board after the redesign's completion (2026-09-10),
deliverable first:** the fit of the draw weights AGAINST THE ACCURACY COMPARISON, the
strikeout market first (SIM-548, P1, planned 2026-09-14 — the comparison found the
simulator's strikeout projection does not know who is pitching: its projected strikeouts
correlate +0.07 with the real count against the closing line's +0.45, and its strikeout
probabilities score worse than a coin flip; the plan is
`docs/audit/2026-09-14-sim548-accuracy-fit-plan.md`: ladder a weight on 250 games of 2024,
confirm on 250 of 2025, the pitcher's power in the pitch draw first — superseded by plan v2,
`docs/audit/2026-09-14-sim548-joint-fit-plan.md`: one model, a composite objective across the
markets, an offline joint fit, a four-factor designed experiment on the full simulator, the
2025 hold-out, a per-market calibration layer; **PAUSED 2026-09-16 by owner decision — the
fit runs once the model is in its final state; the resume order is in that plan's §11: the
pool-window test first, then the design on the chosen pool**); the pitch / pitch-result split's strikeout shortfall — a results-aware
pitcher score, a refit of the split's powers on the balanced set, a lane (SIM-527 — CLOSED
2026-09-10 by owner decision with SIM-429: the pool-totals metric no longer decides; the
shortfall reappears under the accuracy comparison as a 6.5-point low bias on the strikeout
market, the calibration layer's job in SIM-548); the
live-slate epic (SIM-519, P1); the calibration payoff — the strikeout-prop refit and the CLV
re-measure, the latter waiting on every band green (SIM-429 — CLOSED 2026-09-10; the prop
calibration layer is SIM-548's plan v2, §7); the pitcher-fatigue
factor — re-scoped to ONE factor by the owner's rulings of 2026-09-12 (the batting side is a
hard filter of the cell index; the pitch-similarity kernel is superseded by the born ball),
fitted 2026-09-13 and **LANDED 2026-09-14 by owner decision at a times-through-the-order
bandwidth of 0.5 with the pitch-count term OFF** (`SIM_FATIGUE_TTO_SIGMA=0.5`; the
recommendation was HOLD OFF: on the flipped composition neither bandwidth passes the
within-pitcher fit rule and the two paired accuracy arms, σ 0.5 and 0.7 on 250 games of
2024, read flat on every market; the plan with its stamps is
`docs/audit/2026-09-12-sim518-fit-plan.md`, SIM-518 CLOSED); the DuckDB lock that blocked every rebuild while the app ran
(SIM-524 — CLOSED 2026-10-10, see the bullet below); backups (SIM-525); the receiving ratio's enable (SIM-526); the manager-draw
follow-ons — the pen on the API, the preview-game fallbacks, the pen order, a finer rest
fit (SIM-547, P3; the parent, each team's real manager for the pitching change, SIM-427,
CLOSED 2026-09-13 — see the bullet below); the
fielding chain factor (the credits are
on the pool, the consumer is not built); the fielder arm features (SIM-521); the
cell-occupancy census re-run (SIM-451 — CLOSED 2026-10-03; RE-RAN 2026-09-13 and again 2026-10-03 at 2,880 cells: 0.25% / 0.17% of
draws widen at MIN_CELL 20, so 20 stands and hand-as-a-weight has no case); the sim-loop
decomposition (SIM-493, P3); the
twelve segment markets' API / game-page surface (SIM-546, P2 — the prop-market work itself,
SIM-421, CLOSED 2026-09-12, see the bullet below); the outfield-fence certification
(SIM-478/479/480 — BUILT AND RUN 2026-09-21: the lines pass every offline check and the
lane's home-run band with the stage active; the wall play and the per-park pattern do NOT
pass, and both defects sit in the born-ball draw, not the fence — the born kernel is flat
in distance and the born ball's carry is park-blind; the row stays open for the owner's
decision on those two; `CHANGES.md` 2026-09-21). **Closed by the redesign's completion:** SIM-523 itself; the run-grading
question on defense-aware sims (SIM-520 — runs +0.1% with the fielder factor ON on the
balanced set); the date-range lane (SIM-497a/b — absorbed by the balanced-set ruling); the
cell index's ruling (SIM-467 — flipped 2026-09-08). The catcher receiving profile (SIM-517)
CLOSED-LANDED 2026-09-08 with its residuals on SIM-523 and SIM-467. The 2026-08-17 and 2026-08-11 handover docs stay valid as
history only. **SIM-518 CODE LANDED 2026-09-07** (plan `docs/audit/2026-09-04-sim467-518-plan.md`):
migration 0023 (schema v23) + the sim518.1 builder + the artifact columns, and three draw
weights gated OFF (`SIM_FATIGUE_PC_SIGMA` / `SIM_FATIGUE_TTO_SIGMA` / `SIM_PITCH_HOME_OFF_WEIGHT`
/ `SIM_BB_PITCH_SIGMA`). The SIM-469 pool-only rebuild (`scripts/sim518_rebuild_pools.py`)
RAN 2026-09-09 with the redesign's part G (the app stopped for it — the DuckDB writer lock,
SIM-524, CLOSED 2026-10-10); the three weights' fits and the
lane still follow. **SIM-467 CODE LANDED + MEASURED
2026-09-07:** the pitch-draw cell index (`SIM_PITCH_CELL_INDEX` / `SIM_PITCH_MIN_CELL`,
`simulation/filter_cells.py`) cuts a game iteration from 2.62 s to 0.86 s (3.05×) with
bit-identical in-cell weights. Two 12×500 lanes: with the fitted receiving kernel the index reds
PITCHES_PA +5.5% / BB_PA −4.6% (the SIM-523 confound amplified by the hard cell); with the
receiving kernel OFF it passes R, BB, pitches and every other band with K_PA −2.1% (floor 2.0%).
**FLIPPED 2026-09-08 (owner go): production runs the receiving kernel OFF and the cell index ON
(`SIM_CATCHER_*_SIGMA=0`, `SIM_GOT_AWAY=1`, `SIM_PITCH_CELL_INDEX=1`); the live n=100 `/simulate`
reads 31 s warm (81-90 s before; the five-minute figure was never the steady state) — the
30-second exit criterion at the line; the fan-out's ~3 effective workers are the next perf item.

## From §2a (Operational caveats)

- **SIM-433/434/435 — CODE-COMPLETE 2026-06-02 (data-runs pending); commit `812f0e8`.** The
  bullpen/manager/odds foundations landed (unit+regression green, ruff+mypy clean): **SIM-433**
  per-game bullpen availability (Alembic 0015 `raw.game_bullpen_availability` + `_compute_bullpen_workload`
  from raw.pitches + `pipeline/live/bullpen_availability_ingest.py` MLB-API roster/IL ingest);
  **SIM-434** manager pull/reliever decision model (fatigue/rest + TTO decay + reliever scoring +
  the `pitcher_pitch_count` bug fix), **gated `SIM_MANAGER` (default OFF) — flag-off is byte-identical**
  (verified by the green regression lane); **SIM-435** historical odds loader (`scripts/
  load_historical_odds.py` + a provider `closing`-line branch) → unblocks the SIM-429 CLV backtest.
  Pending data-runs: the MLB-API ingest (`pipeline.live.bullpen_availability_ingest`, ~21k games),
  the odds backfill (`scripts/load_historical_odds.py`, needs `ODDS_API_KEY`), and enabling
  `SIM_MANAGER=1` with a ≥400-sim validation + golden-fixture regen. **SIM-436** (P3 low) tracks the
  /simulate 30 s SLA (per-game cost). ⚠ Avoid rebuilding the app image repeatedly — it fills the
  Docker build cache on C: (it hit 100% disk this session; `docker builder prune -f` reclaims it;
  api/pipeline/similarity/simulation/db are bind-mounted so most edits need no rebuild).
- **Open follow-ons (tracked, blocked on data/infra, not shipped hollow):** SIM-427 engine-backed
  manager (the SIM-433 bullpen-availability table is now its prerequisite-in-code; still needs the
  ingest run); SIM-425b Fielder RBF (needs per-row fielder identity baked into the batted-ball
  artifact → a play-pool rebuild); SIM-411 park factor + SIM-413 pitcher-hand platoon (both also
  blocked on a play-pool rebuild — engine artifact has no `venue_id` / `p_throws` per row);
  SIM-429 granular run-conversion calibration + the CLV backtest (the larger sim harness landed
  2026-05-28 as `scripts/sim_stats.py` v2 — defaults to 200 sims/game, reports per-channel + home/
  away splits + R standard error; the CLV backtest is unblocked once the SIM-435 odds backfill runs).
- **Live-env verification debt — largely retired 2026-05-30.**  `docker compose up`
  (nginx+app+monitoring) runs; the 2026-05-29 bring-up fixed a `/dev/shm` overflow
  (`shm_size: 1gb` on the `app` service) and made the pre-warm a BACKGROUND task with
  bounded-concurrency warming + a `_get_pool` lock (a blocking pre-warm hung startup ~22-30 min —
  `asyncio.wait_for` can't interrupt a multiprocessing-blocked thread — and warming all 10 workers
  at once OOM-killed one).
  - **SIM-402 CLOSED** — re-measured live; the 2s/30s SLA is not met and the throughput gap is now
    **SIM-430** (see §2).
  - **SIM-408 CLOSED** — the engine↔DuckDB schema divergence (was **only 7/11 engines build**:
    catcher/manager/baserunner_steal/pitcher_steal failing, situation indexing 0 rows) was
    reconciled and a full all-seasons (2017-2026) profile rebuild ran; the live app now logs
    `build_all_engines: 11/11`.  See §11 for what was trimmed/built per engine.
  - **SIM-406 + SIM-407 — ✅ LIVE as of 2026-06-01 (SIM-432 closed).** The SIM-406 calibration seam
    (`apply_calibration` on all 8 RBF engines + 4 sub-calibrators) and the SIM-407 prop-PMF /
    win-prob validation + reliability-curve fit are now actually applied: `/data/calibration.json`
    is fitted (`make calibrate`) + the win-prob reliability curve written (`make validate-props
    --write-calibration`), and the app applies both at boot. SIM-432 was the schema-reconciliation
    that unblocked it (see §2 top bullet + §11). *(The earlier "scripts never run / identity
    calibration" wording here described the pre-2026-06-01 state.)*

## From §11 (Known defects / dead-wiring + verification debt)

The audit-era list of issues (kept here for historical context; tickets marked ✓ have closed):
- ✓ The "gold-standard" CLV is computed off an **uncalibrated** win prob — `betting.py` calls
  `win_probability()` without threading `app.state.calibration_map` (**SIM-387** — closed).
- ✓ `require_api_key` is defined but applied to **zero** routes; dev CORS is `*`+credentials
  (**SIM-389** — closed).
- ✓ `SIM_RUNNER_WORKERS=1` serializes `/simulate`; the lifespan runner is built without
  `shared_arrays=` (**SIM-403** worker-count fix + **SIM-403b** `EngineArtifacts.{extract,attach}_shared_views`
  zero-copy across workers — both closed 2026-05-28).
- `GameState.park` is a **dead field** (run environment is park-blind) (**SIM-411** — open, blocked
  on play-pool rebuild for venue_id per-row); pitcher throwing-hand unused in the batted-ball matchup
  (**SIM-413** — open, same blocker).
- ✓ Home-field run advantage missing — `home_win_pct` stuck at the structural-only ~.510-.515
  (**SIM-412** — closed 2026-05-28; `_apply_home_field_bias` flips a small fraction of HOME
  batted-ball outs to singles, default 0.025 calibrated to MLB ~.535-.540; env override
  `SIM_HOME_FIELD_BIAS`).
- ✓ The `/metrics` p95 gauge is an unwired placeholder (**SIM-410** — closed).
- ✓ Pre-existing Phase-6 tickets SIM-127–131 cite **phantom parent tickets** SIM-108/109/112/122–126
  (**SIM-382** backfill — closed).
- ✓ Walk-forced runs missed in per-runner R, ER under-counting, sub-5-IP starter winners
  (**SIM-414a/b/c** — closed 2026-05-28; `_resolve_walk` records forced advances,
  `_half_inning_error_outs_lost` inning-reconstruction for ER, `STARTER_WIN_MIN_OUTS=15` reassignment
  in `pitcher_decisions.py`).
- ✓ Lineup ingestion silent 500s on scheduled games whose lineup hasn't been published
  (**SIM-409** — closed 2026-05-28; `LineupNotIngestedError` → 503 + `Retry-After: 900`;
  `lineup_ready: bool | None` field on `GameCard`).

**Live-environment verification debt** — mostly retired over the 2026-05-29/30 live bring-up.
`docker compose up` of nginx+app+monitoring runs. (SIM-405 real odds provider, SIM-410 p95 timing,
and the SIM-403 worker-count fix closed earlier.) **2026-05-29 → 2026-05-30 update:**
- **SIM-402 — CLOSED.** `/dev/shm` overflow + pre-warm hang/OOM fixed (`shm_size: 1gb`; pre-warm is
  a BACKGROUND task with bounded-concurrency warming + a `_get_pool` lock; healthcheck `start_period`
  180s). Re-measured live (all-seasons DuckDB, 1 worker): n=1 ≈ 2.2-2.3s, n=100 ≈ 215s — the 2s/30s
  SLA is **not met** on the full-pool path, and 10 workers OOM-deadlock on this 15.5 GiB host. The
  throughput gap is now **SIM-430**; the host `.env` is pinned to 1 worker. See §2.
- **SIM-408 — CLOSED.** The engine↔DuckDB schema divergence (was **7/11**: catcher / manager /
  baserunner_steal / pitcher_steal failing, situation indexing 0 rows) was reconciled via the TRIM
  approach and a full all-seasons (2017-2026) profile rebuild — the live app now logs
  `build_all_engines: 11/11`. What changed, per engine: situation now reads a new
  `derived.at_bat_situations` table (+ a fixed park-factor join `pf.factor_type='R'`/`regressed_factor`)
  and raises on a zero-row index; baserunner_steal + pitcher_steal read new metrics tables with the
  biomech (jump/delivery/pickoff) features trimmed (pitcher_steal is now outcome-only); catcher
  derives its rates from existing count columns + two new shadow/heart zone-framing columns (the
  Offense + exchange_time sub-scores were trimmed, weights renormalized); manager's computor was
  rewritten to the engine's usage/aggression/platoon vocabulary with the USAGE sub-score gated NULL
  on SIM-427. Shipped as DuckDB migration `0011` (non-destructive — CREATE new tables +
  `ALTER ... ADD COLUMN IF NOT EXISTS`); schema version 10 → 11. Diagnosis +
  reconciliation map in `docs/audit/2026-05-29-sim408-engine-schema-divergence.md` and
  `docs/audit/2026-05-29-sim408-reconciliation-plan.md`.
- **SIM-406 + SIM-407 — ✅ LIVE 2026-06-01 (SIM-432 reconciled the scripts to the live schema).**
  The SIM-406 `apply_calibration` seam (all 8 RBF engines + 4 sub-calibrators) and the SIM-407
  prop-PMF / win-prob validation + reliability-curve fit are now actually applied at boot. SIM-432
  resolved the cascade (ground truth was read from the LIVE containers, which corrected several
  filed-but-stale items):
  - ✓ batter `xba`/`xslg` — already `_opt`-guarded (commit `ee1188f`); the only genuinely-absent
    batter columns.
  - ✓ pitcher sub-calibrator's `RESULT_FEATURES` import — **FIXED**: removed (SIM-067 deleted the
    results sub-score); now fits `sigma_command` over the engine's 7 `COMMAND_FEATURES` behind an
    info_schema guard, `sigma_results` left as a vestigial keep-default.
  - ✓ `first_pitch_take_rate` / `max_exit_velo` / the `*_vs_r` platoon block — **STALE finding**:
    they are PRESENT in the rebuilt all-seasons `derived.batter_season_metrics` (the filing predated
    the rebuild), so no change was needed.
  - ✓ `validate_props._fetch_final_games` `raw.games.home_score` — **FIXED**: the live schema stores
    the final score as `home_score_final` / `away_score_final`; the query now selects + aliases those.
  - ✓ the 6 non-batter sub-calibrators were verified against the live column set (all match); a
    regression guard was added so 7 degenerate sub-scores (sprint_speed etc.) keep the engine's tuned
    default (`_fit_sigma` 0.0 sentinel via `calibrate_sigma(degenerate_value=…)`) instead of a
    spurious 1.0.
  **Result:** `make calibrate` → `/data/calibration.json` (arsenal median W₂ 2.818, ARSENAL_SCALE
  4.0655, σ_command 1.078); `make validate-props --write-calibration` (60 games 2024 → ECE 0.171,
  7 anchors); boot logs `applied fitted calibration to 8 engines; win-prob map:
  reliability-curve(2026..2017)`. Audit: `docs/audit/2026-06-01-sim432-calibration-schema-reconciliation.md`.
  SIM-407 is **not** data-blocked — all 21,562 Final games (2017-2026) have ingested lineups in
  `raw.game_lineups`. *(Follow-ups, NOT SIM-432: a fuller multi-season win-prob curve fit is gated
  on SIM-430 throughput; pitcher K/BB props are over-predicted → SIM-429.)*

**⚠ FIGURE RECONCILED 2026-08-10 — the run-conversion gap is ~7-8%.**
This guide carried two sizes for one defect. The **DP-rate bug fix bullet in §2** (dated 2026-05-28,
at `CLAUDE.md:85`) said "~7-8% low". The paragraph below said "~10-12% low". The
§2 CLV bullet said "~10–12%". The owner ruled on 2026-08-10 that **the §2 DP-fix bullet wins**: the
gap is **~7-8%**, measured after the DP-rate fix, down from ~12% before it. The 10-12% figure
predates that fix and is stale everywhere it appears. Both stale copies are corrected. Do not
re-split the number. Any band, floor or calibration target that measures this gap must be
calibrated to catch **7-8%**, not 10-12% — a band that only reds at 10% reports PASS on the defect
this platform actually has. *(`tests/acceptance/bands.py` cites `CLAUDE.md:85` by line number, so
the 2026-08-10 edit kept that statement on line 85 on purpose. If you add a line above it, re-point
that citation in the same commit. Better: cite the section.)*

**Full-pool realism residual (SIM-422→429, the production path) — THE RUN GAP IS CLOSED
(2026-08-16).** This paragraph used to record **runs ~7-8% low**, a hits→runs conversion gap in
batted-ball-with-RISP / sequencing (a global advancement multiplier `SIM_RUN_CALIB` was
investigated and rejected as the wrong lever; the phantom-DP contributor — ~55% of drawn DP events
with no runner to double off — was fixed in `_full_pool_fielding`). The gap's real root turned out
to be the DATA: after the SIM-501a events-based out label, the SIM-488 re-sweep and the SIM-459
all-seasons recompute, the SIM-450 acceptance lane (5,098 game-sims, production flags) measured
**R INSIDE its band** and the strict expected-red marker was deleted (commit b61001d). The R floor
still rejects a 7% shortfall, so a regression reds immediately. **The remaining SIM-429 residuals
are small and channel-level: 2B +8.2%, BB +10.8%, ROE +4.9% high** — target them with
`scripts/sim_stats.py` v2 per-channel breakouts. *Validation caveat unchanged:* run a multi-game ×
≥400-sim batch before reading R-level moves; per-channel breakouts are the lens, not the global R
mean.

**⚠ STEALS — the zero era (2026-06-04 → 2026-08-16) and the SIM-474 fix (2026-08-17).** From
2026-06-04 the manager green-light gated the steal decision into a resolver stub and production
attempted **exactly zero steals** (SIM-495 measured SB/CS 0.0000 across 400 game-sims; the chain
is preserved in `tests/acceptance/test_production_config_bands_sim450.py`'s history section).
**SIM-468/474 replaced the whole chain:** `sim.steal_opportunity_pool` (migration 0015, one row
per pitch where a steal was POSSIBLE, attempted or not — the missing denominator) plus a
similarity-weighted draw (`_steal_opportunity_draw` → `FullPoolSampler.steal_draw`) whose drawn
row answers "does he go" AND "safe or caught"; manager aggression is a WEIGHT on attempted rows,
never a gate. The 600-sim smoke: **SB 0.70 + CS 0.09 = 0.79 attempts/team-game vs MLB 0.76**.
The certifying lane has since run (2026-09-09, 45 × 130 on the balanced set): steal attempts
at second +0.4% against the pool's own rate, the safe share 0.812 against 0.799, every steal
band green; the runner factors are the steal and advancement engines' score matrices at
their fitted powers (12 / 20), not bandwidths. Since the running game on the pitch (SIM-554,
built 2026-10-01) the pickoff is its own draw before the pitch, and the steal draw reads the
class of the pitch that came out (§2b).

## From §12 (Phase roadmap)

**Realism sub-track (interleaved, landed on `master`):** the SIM-422→429 full-pool similarity-wiring
epic replaced the per-tile k-NN draw with whole-pool engine-weighted sampling and made it the
production default — see §2/§11; SIM-486 (2026-09-06) then deleted the per-tile path entirely.
This is independent of the frontend critical path below.

**Phase 6 critical path:** SIM-378 (React-vs-vanilla ADR) → 379/380/381 (scaffold + design system +
API→UI serving) + 382/383/384/385/387/389 (backfill deps; enriched games list+records; aggregate card +
status enum; typed WebSocket schema; calibration-wiring fix; auth enforcement) → 386 (live read path) →
391/392 (Day Summary + 3-state cards + linescore/field graphics) → 393/394 (game page + boxscore) →
395/396/397/398 (betting card + CLV chart + override v1 then v2). The data/ML/perf prerequisite track
(402–409) runs alongside and must be live-env verified before its numbers reach users.
