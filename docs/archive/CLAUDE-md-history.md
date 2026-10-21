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

---

## From §2 and §2b at commit 440ecb4 (removed 2026-10-21)

The documentation rebuild (SIM-624) rewrote `CLAUDE.md` from the code. Its §2 and §2b had
grown into a dated log of each ticket's landing. The text below is that log, verbatim from
`CLAUDE.md` at commit `440ecb4`. The rewritten `CLAUDE.md` keeps every standing ruling and
every current fact; `CHANGES.md` holds the full record of each ticket.

### 2. Current status (read this)

- **Phases 1–6 are complete; Phase 7 is in progress** (§12). The platform runs on Python 3.13
  and numpy 2.x. The frontend is React 18 + Vite + TypeScript (SIM-378). DuckDB schema **v32**,
  Alembic head **0030** (§5).
- **The simulator has one path: the full-pool similarity sampler** (SIM-486). The no-DB tests
  and the batch runner's no-DB factory build the same `StateMachine` over
  `simulation/synthetic_bundle.py`.
- **The production flags live in the `app` service env of `docker-compose.yml`.**
  `tests/conftest.py` pins `SIM_MANAGER` and `SIM_BB_PLATOON` OFF, so the unit lane stays
  byte-identical. The conftest also pins the odds provider to the mock. The conftest
  removes the vendor key, `ODDS_API_KEY`, too (the test lane's odds provider pin, SIM-567).
- **Calibration is live.** The app applies `/data/calibration.json` at boot. `make calibrate`
  drops the win-probability reliability curve; the §2b manager-draw bullet says how to restore it.
- **§2b holds the current state, the owner's standing rulings and the in-flight work.** The open
  tickets are the rows of `BACKLOG.xlsx`. The dated status log of 2026-05 to 2026-09 is in
  `docs/archive/CLAUDE-md-history.md`.

### 2b. ⚠ IN-FLIGHT WORK — read before touching the ETL or running a recompute

**The open-ticket list is `BACKLOG.xlsx` (owner decision 2026-09-10):** one tab, one row per
open ticket, ranked by priority, with a plain-English description, a definition of done, and a
proposed solution for each. The closed-ticket narrative and sprint history that `BACKLOG.md`
carried is frozen at `docs/archive/BACKLOG-history.md`; nothing new is appended there. Read
`BACKLOG.xlsx` for the current ticket list; read `docs/archive/BACKLOG-history.md` only for
history.
**The subtitle row (row 2) carries ONLY the updated date, the two pointers and the next free
ticket ID (owner ruling 2026-09-12).** Do not write closure or status notes into it — a
closure, a status change or a data-run result goes to `CHANGES.md`, and a ticket's own row
carries its status while it is open. Update the ID in the subtitle each time you file a
ticket; a closed ticket's row is deleted, and `CHANGES.md` is its record.

**The CURRENT state authority is `BACKLOG.xlsx`'s open tickets, plus two docs:
`docs/audit/2026-08-20-sim429-514-diagnosis-results.md` (why the model reads the way it
does) and `docs/audit/2026-08-28-sim476-fit-plan.md` (the SIM-476 kernel fit, done 2026-08-30).** The
standing owner rulings that govern all new work:

- **The architecture rule, both clauses (2026-08-10 + 2026-08-29):** every decision is a
  similarity-weighted draw from a hard-filtered pool — never a hand-tuned formula — and
  **the drawn row IS the play**: no post-draw adjustment of any kind; every factor is a
  draw WEIGHT or OFF until its weight lands. **Third clause (owner ruling 2026-09-16):
  every change lands ON at its best-known default, and no change waits on a per-change
  accuracy run.** The paired accuracy comparison on 250 games resolves only large effects
  (its `min n` column: the strikeout market needs 83,000 pitcher-games to resolve a
  0.0005 Brier effect; 250 games give 482), so three hours per change bought a coin flip
  on the small ones. Per change, the gates are cheap: the unit and regression lanes, and a
  short sim smoke of the affected channels (`scripts/sim_stats.py`, ten games) to catch a
  collapse. The weights are fitted TOGETHER, once, when the owner calls the model final:
  one designed experiment over every factor (`scripts/designed_experiment.py` — each arm flips
  several factors, so 16–20 arms read every factor's effect and the pairwise interactions;
  about three days of unattended compute). "OFF until its weight lands" now means OFF
  until its best-known default exists, not OFF until a paired read confirms it.
- **No betting-value measurement until everything is green (owner ruling 2026-09-08):** the
  closing-line-value re-measure and any edge read built on it wait until every band the lanes
  grade is inside its range. As of 2026-09-10 the certified production arm is green on every
  pool band and on runs (the balanced 45 × 130 lane); the one open red is the pitch /
  pitch-result split's strikeout shortfall (SIM-527) — the split was FLIPPED ON on 2026-09-14
  by owner decision on the accuracy comparison, so that red is now IN production and the
  lane has not re-graded it; the home-win channel is underpowered below 13,365 game-sims.
  The strikeout-prop refit may proceed.
- **The play-picker redesign (owner rulings 2026-09-08; the plan is
  `docs/audit/2026-09-08-sim523-play-picker-redesign-plan.md`):** the weight order in the
  pitch draws is pitcher first, batter, recency, catcher receiving last, with powers fitted
  under that constraint; every actor factor is its engine's 0-to-1 score, emitted nightly as a
  matrix and looked up at draw time; the loop is manager decisions per plate appearance, then
  per pitch the steal draw, the pitch draw, the pitch-result draw (the batted ball is born
  there; since SIM-554 the pickoff draw comes before the pitch and the steal draw after its
  result), then park geometry, the fielding draw and the advancement draws; catcher receiving
  is a mass-preserving ball-strike ratio on taken pitches that ships OFF (enable = SIM-526);
  a concentration check (no factor may put more than its natural share of a draw on the live
  player's own team) joins the grade. **COMPLETE 2026-09-09:** production runs the certified
  arm (the actor score matrices at their fitted powers, the fielding draw's fits); the actor
  bell-curve kernels are deleted; the split stays OFF (SIM-527).
- **The balanced certifying set (owner ruling 2026-09-09):** the lane grades on 45 games
  from three full-day slates (every team three times), chosen so every graded channel's
  actor-matched expectation sits within 0.6% of the pool's totals
  (`scripts/balanced_game_set.py`; the park factors and the prefix order in
  `tests/acceptance/bands.py`); the default lane is 45 × 130 = 5,850 game-sims. It replaces
  the fixed 12-game set and absorbs the date-range lane (SIM-497a/b).
- **One in-play path (SIM-486, 2026-09-06):** the per-tile FAISS fallback, the injected
  `PlayResolver`, the legacy advancement code (`_advance_runners` and its constants), the
  SIM-318 foul re-weight and the `SIM_FULL_POOL` switch are DELETED. Every no-DB test and
  the batch runner's no-DB factory build the production `StateMachine` over an in-memory
  `simulation/synthetic_bundle.py` bundle, so the suite runs the code users get. A missing
  artifact bundle now FAILS the worker loudly (no fallback simulator). The nightly chain
  rebuilds the bundle (`make engine-artifacts`); it used to rebuild dead FAISS tiles.
- **The grade is POOL TOTALS (2026-08-20):** the sim's frequencies certify against the play
  pool's own totals (`tests/acceptance/bands.py` `POOL_REFERENCES`, 16 per-opportunity
  bands); R / home_win_pct stay game-graded. **CERTIFIED 2026-09-09 (45 × 130 on the
  balanced set): all 16 pool bands PASS, R +0.1%;** the only non-pass is home_win_pct
  (underpowered below 13,365 game-sims). **⚠ Two corrections from 2026-09-21 (the fence
  certification, CHANGES.md):** that lane, and every lane before 2026-09-21, ran the
  fence stage INERT — the lane never put the venue on the game state, so the stage
  passed every air ball (production and the accuracy comparison do resolve the venue;
  the lane now does, and refuses to run the stage without one). And the first 45 × 130
  lane since the powers-to-1 ruling (2026-09-21, the stage active) reads **four bands
  RED: walks per PA +4.0%, hit by pitch +6.7%, steal attempts at second −9.1% and at
  third −17.9%**; the other twelve pass (home runs +1.2%). Those four are the state of
  production after the flips since 2026-09-09, unread until this lane. **The 2026-09-22
  lane (the fence amendments: the carry offset, the class ruler, the wall-margin band —
  `docs/audit/records/sim478_lane.txt`) reads the same four red plus doubles per ball in play +5.8%**:
  the wall play is now right (near-wall balls draw doubles as the real ones do) and the
  fix unmasked an over-production of doubles on ordinary air balls that the born kernel's
  bandwidth owns (the sweep's); singles, hits, runs and home runs pass.
- **The pool's class labels are checked against real plate appearances (SIM-553, 2026-09-25).**
  A two-strike foul tip or foul bunt is strike three in the pitch pool (`SQL_OUTCOME_TYPE`,
  builder `sim553.1`, all ten seasons rebuilt 2026-09-25); it used to be coded `foul`, and the
  count chain put the simulator's loss at 4.4% of its strikeouts (7.1% of 2025's strikeouts end
  this way). The four chain band centres moved to K_PA 0.2262 / BB_PA 0.0829 / HBP_PA 0.0112 /
  PITCHES_PA 3.906. A paired read (old vs new bundle, 10 games × 300) measured the sim's
  strikeouts +3.5% per plate appearance (±0.6%; the pool moved 4.6%) and runs −2.8%, so the
  next lane's K_PA band should read about −1.8% (floor 2%). `pipeline/batch/pool_chain.py` holds the count chain and the label check;
  `tests/acceptance/test_pool_label_check.py` and `scripts/pool_window_census.py
  --strict` fail when the pool's chain drifts more than 0.5% from real play — the pool-totals
  grade cannot see a labelling defect, because it reads the same labels. The steal and
  advancement pools in DuckDB are AHEAD of the bundle (the 2026 games of 08-14 to 08-29,
  never exported); the next `--what pool` export ships that refresh.
- **The running game on the pitch (SIM-554, CLOSED 2026-10-03: built 2026-10-01, the run
  book RAN 2026-10-03, production runs it).** On every pitch with a runner who
  could steal, the loop now makes four draws in the order things happen. (1) The pickoff
  draw, BEFORE the pitch: one row of the steal opportunity pool's count group (target base,
  outs, balls, strikes), pitch rows and pickoff rows alike, with the steal draw's weights
  and no manager weight. An outcome resolves at once. A pickoff that makes the third out
  ends the half-inning with no pitch thrown: the result reads `pitch_outcome = NO_PITCH` and
  `pa_voided = "pickoff_third_out"`, no pitch, event or plate appearance is credited (the
  ledger credits the out to the pitcher when it records it, SIM-557; a picked-off caught
  stealing charges the runner a CS), and the
  same batter leads off his team's next inning. Every reader of a per-pitch result must skip a no-pitch result
  (`PlayResult.no_pitch`). (2) The pitch and (3) its result, as before, on the bases the
  pickoff left. (4) The steal draw, AFTER the result, among real pitches of the same base,
  outs, count and CLASS (ball, called strike, swinging strike, foul, in play, hit by pitch).
  A class group answers from its own rows however few; nothing falls back and nothing is
  cancelled. The old single pre-pitch draw read only the count, so 36% of its attempts
  landed on a pitch that never carries a real one. Two rules of baseball land with it, in
  both orders (the plan's decisions 3 and 4, taken by the owner 2026-10-01). With two outs,
  a third strike the catcher holds is the third out first, so a staged steal is void
  (`steal_voided = "third_out_first"`). On a dropped-third-strike reach the got-away advance
  runs first: every runner moves up one base, a run from third pays no RBI, and then the
  batter takes first; a steal or a pickoff on the same pitch skips the advance. The switch
  is `SIM_STEAL_PITCH_CLASS` (default 1; 0 = the single pre-pitch draw of SIM-474). A bundle
  exported before migration 0031, or one with any unclassed pitch row, also runs the single draw. **The data:**
  DuckDB migration 0031 (schema v31) adds two columns to `sim.steal_opportunity_pool`:
  `pitch_class` (the class of the pitch the row rode; NULL on a pickoff row) and
  `is_pickoff_row`. A pickoff row is a pickoff throw with an outcome before the plate
  appearance had any pitch with that runner situation: `pitch_id` = minus the
  `raw.play_events` id, the count 0-0, the catcher of the nearest pitch of the half-inning.
  The pool held about 72% of real pickoff outcomes: 22% of those with a stealable runner
  came before the first pitch and were dropped, and 7% had no stealable runner. The pickoff
  rows raise the pool's pickoff outcomes from about 0.15 to about 0.19 a game. Builder
  `sim554.1`. The export carries `pitch_id`, `pitch_class` and `is_pickoff_row`, in
  `pitch_id` order; `StealPool.pitch_class` holds codes 1-6 (0 = no class; None = an old
  bundle) and `StealPool.is_pickoff_row` marks the pickoff rows. `RunningGameTally` (`machine.running_game_tally`) counts the pickoffs, the
  no-pitch third outs, the voided steals and the dropped-third-strike advances; it changes
  no play. **The run book RAN 2026-10-03** (the app stopped; the writer lock is SIM-524's):
  `scripts/rebuild_steal_pool.py --apply-migration` applied 0031 inside its one
  transaction, wrote the 394 pickoff rows (91 / 108 / 95 / 100 for 2023-2026) and passed
  every check; the export round-tripped row for row. The census (both arms, 45 × 20,
  `CHANGES.md` 2026-10-03) moved the steals' share on a foul, a ball in play or a
  hit by pitch from 38.8% to 0.85% (real 1.0%), the pickoffs per draw ×1.24 (the pool's
  ×1.28), the attempt volume within noise; the 45 × 130 lane (same entry)
  reads the same six non-passes as before the change (walks +3.4%, hit by pitch +8.5%,
  doubles +5.0%, steal attempts −10.0% / −21.3% — the steal-weight volume ticket, SIM-556 —
  and the underpowered home-win share), runs 4.47 a team-game. To rerun the rebuild on
  another database, use the one command; never apply 0031 as a separate step. The builder
  version `sim554.1` marks every pool stale for the current season, and the new code
  refuses an un-migrated table before it deletes anything (`_require_steal_pool_0031`). The
  plan (version 3, §13 the build and run record):
  `docs/audit/2026-09-29-sim554-running-game-on-the-pitch-plan.md`.
- **Every out on a pitcher's line (SIM-557, CLOSED 2026-10-07).** A caught stealing or a
  pickoff out on a pitch that does not end the plate appearance reached no pitcher's line
  (0.32 outs a game). The ledger (`_record_outs`) is now the ONE writer of a pitcher's outs.
  `GameSimResult.outs_played` counts the outs played; a unit test and the smoke's gate
  (`scripts/sim_stats.py` exits 1) hold the pitchers' outs equal to it. Each play result names
  its own pitcher and side (`PlayResult.pitcher_id`, `fielding_team`) for the win module; the
  box line counts batters faced (`bf`). The 10 × 50 smoke: credited outs 52.94 → 53.25 a game,
  equal to the outs played in all 500 (125 differed before); no play changed.
- **The pool window (2026-08-20):** the last three COMPLETED seasons plus the current one
  (`RECENCY_FLOOR_SEASONS = 4`; full 2023-2026 today). Schema v20 and `POOL_BUILDER_VERSION`
  sim515.1 then; v31 and sim554.1 since the running game on the pitch (SIM-554).
- **The slate is schedule-driven (2026-08-29):** the frontend day slate reads the MLB Stats
  API for which games exist (preview / live / final cards); the DB supplies our sims,
  projections and odds on top (→ SIM-519).

The tickets landed since the 2026-08 handovers, with their measurements, are in `CHANGES.md`.
The open tickets, ranked, are the rows of `BACKLOG.xlsx`.

**THE PLAY-PICKER REDESIGN (SIM-523) — BUILT 2026-09-08/09, COMPLETE 2026-09-09.** The
plan with its part stamps is `docs/audit/2026-09-08-sim523-play-picker-redesign-plan.md`; the
detail is `CHANGES.md`. What production runs today: every actor factor is its engine's
0-to-1 score matrix (`actor_sim/` in the bundle, `--what actors_sim` in the nightly `all`;
the concentration report gates a strict build at a 3.0 own-staff ratio) raised to a power
(`SIM_ACTOR_POWER_<NAME>`; 0 = off). **Every similarity power is 1 (owner ruling 2026-09-16)
until the comprehensive sweep of the tunable parameters fits them together.** The fitted values
(batter 1.0, fielder 1.2, steal-runner 12, advancement-runner 20, catcher-throwing 2,
pitcher-steal 12; the split's 16 / 16 / 8; the fielding draw's batter 4; manager-usage 4) are
the sweep's starting points, and the cheat sheet's summary table carries both columns. At
power 1 the sampler skips the power step, the live n=100 `/simulate` reads 25–27 s warm, and
the factors read nearly flat (the part F finding). Reliever stuff is ON at power 1. The runner
engines score thin profiles so the matrices cover the pools; the actor bell-curve kernels and
their bandwidths are
DELETED. The fielding draw filters the cell by the born ball's class (`SIM_BB_CLASS_FILTER`),
weights by the born ball's kernel (`SIM_BB_BORN_SIGMA` 1.0 — since 2026-09-22 measured on
the born ball's CLASS, a fly ball against the fly balls' spread, with the exponent per
feature `SIM_BB_BORN_PER_FEATURE`), the batter matrix
(`SIM_BB_BATTER_POWER`), the fielder matrix and the park only in the wall zone
(`SIM_PARK_WALL_ZONE_ONLY`), then runs the fence stage (`SIM_FENCE_STAGE`;
`park_geometry.json`, `--what park` — version 2 since 2026-09-21: eleven sectors over
±55°, a line per park and SEASON GROUP, a published-distance prior for thin sectors;
since 2026-09-22 the born ball's carry is read in the LIVE park's air through the
document's per-park carry offsets, `SIM_CARRY_OFFSET` — Coors +21 ft — and a wall-zone air
ball called short draws only rows within `SIM_BB_MARGIN_BAND` 15 ft of the same distance from
THEIR park's fence; certified by `scripts/fence_check.py`, A1–A7, which runs in the
run book of every geometry rebuild). The pitch draw runs one draw per pitch at pitcher
power 1 through the cell index (`SIM_PITCH_CELL_INDEX`). The pitch /
pitch-result split (`SIM_PITCH_RESULT_SPLIT`; fitted at pitcher 16 / 16, batter 8; it reds
strikeouts −2.4% on the balanced set — SIM-527) was built and OFF until **2026-09-14, when
the owner FLIPPED IT ON** on the accuracy comparison's read (the starter strikeout market
−0.0155 Brier on 250 games of 2024; the strikeout probabilities sit 6.5 points low — the
calibration layer's first job; the 2025 hold-out has not run). Built and OFF: the catcher receiving ratio
(`SIM_CATCHER_RECEIVING`, `receiving.json`, `--what receiving`; enable = SIM-526) and the
pitching change as a draw (`SIM_MANAGER_DRAW`, `--what manager`) — **FLIPPED ON
2026-09-13 with the real managers and pens (SIM-427; see the bullet below)**. The data
adds: sprint speed in both profiles (migration 0024, schema v24), the fielding chain on the
batted-ball pool (builder sim523g.1; the chain FACTOR is a follow-on), the catcher embedding
without its got-away columns, the SIM-469 rebuild. The grade: the balanced 45-game set at
45 × 130 (two lanes 2026-09-09, `CHANGES.md` 2026-09-09) — the certified arm is green on
every pool band, runs +0.1% (with the fence stage inert; the 2026-09-21 lane with the stage
active, `docs/audit/records/sim478_lane.txt`, reads four bands red — see the §2b grade bullet).

- **The eight unpriced prop markets + the official box-score ground truth (SIM-421 + SIM-545,
  BOTH CLOSED 2026-09-12 — owner decision; the wrong-game odds match, SIM-536, closed with
  them on the 2024 re-load; the 2019–2026 odds load runs as two detached processes).** The platform prices fifteen player-prop markets (was seven): singles, doubles,
  triples, runs, stolen bases, hits+runs+RBI, pitcher outs recorded and pitcher hits allowed
  joined the original seven. The odds vocabulary has one source (`pipeline/odds_provider.py`:
  `PROP_STATS` 15 / `PITCHER_PROP_STATS` 5 / `BATTER_PROP_STATS` 10 /
  `PROP_STAT_TO_MODEL_PROP`); markets are requested by role, so the batter "hits" market
  (BettingPros 287) and the pitcher "hits allowed" market (404) never cross; Alembic 0022
  widens the `raw.prop_odds.prop_stat` CHECK constraint (its downgrade DELETES the eight new
  markets' rows). Alembic 0023 adds `raw.game_player_stats`, the official per-player box score
  (one row per player who APPEARED; no row = did not play = the book voids the bet;
  `player_id` has no foreign key on purpose), written by the historical loader from the feed
  it already holds and backfilled by `scripts/load_official_boxscores.py`. The accuracy
  comparison and `validate_props` grade every market on the box per game, with the
  event-label reader (H/HR/TB, K/BB) as the fallback, and both report how many games each
  source graded. **Ran 2026-09-12:** both migrations applied (`alembic current` = 0023); the
  backfill loaded the FULL 2024 season (2,472 games, 72,651 rows, zero failures; on every
  game the box-score runs equal the `raw.games` final score); the audit on all 2,472 games
  reads the `raw.pitches` derivation exact on every hit-type prop and RBI, and inexact on
  SB 99.08% / R 99.97% / K 99.67% / BB 99.71% / OUTS 97.93% / ER 87.57% (exact-match rates;
  the causes — K: strikeouts anchored to a trailing `no_pitch` event; OUTS: the
  displaced-runner out; ER: an inherited runner charged to the pitcher of the pitch — and
  the open hypotheses for R/SB/BB are in `docs/audit/2026-09-12-sim545-boxscore-audit.md`).
  That study is the evidence for the owner decision: grade props on the box, never the
  derivation (the old event-label path mis-graded the K prop on about one pitcher-game in
  300). **Second batch, the same day (owner ruling: "all the different markets in the
  odds tables, because we are simulating games"):** `raw.game_odds` carries EVERY game
  market the book posts — the three full-game markets plus twelve segment and team markets
  (the first-inning and first-five moneyline [three-way, with a `draw_ml` column], total and
  run line; each side's full-game and first-five team total; the first team to score; "a run
  in the first inning", stored as over / under at 0.5). Alembic 0024 (applied; head 0024)
  widens the `market_type` CHECK and adds `draw_ml`; the vocabulary is `GAME_MARKET_TYPES`
  (+ `_KIND` / `_SEGMENT` / `_SIDE`) in `pipeline/odds_provider.py`; the provider, the mock,
  the loader (`--game-markets`) and the live cycle (on the prop cadence) serve all fifteen.
  `simulation/game_market_distributions.py` prices every one of them from the per-iteration
  linescore, and the backtest grades them against `raw.games.inning_scores`. **Data runs
  started 2026-09-12 at the owner's instruction:** the full 2024 odds re-load (all fifteen
  game markets + all fifteen prop markets, ~18 s per game) and the box scores for the other
  nine seasons — the latter FINISHED the same afternoon (20,270 games, 596,480 rows, zero
  failures; all ten seasons: 22,742 games, 669,131 lines, box runs = final score on every
  game), which CLOSES SIM-545. Two facts from the loader smoke: BettingPros has no walks / outs-recorded /
  hits-allowed offers on the 2024 games checked (why the old load had zero walks rows), and
  the old load saw only page 1 of each batter market (~8 of ~15-18 hitters). **Not built:**
  the API's `/edges` and the game page still served the three full-game markets — SIM-546,
  CLOSED 2026-10-09: the inning grid now rides in the summary through the cache, which stores
  it as JSON since SIM-443f (`simulation/cache_codec.py`). Details: `CHANGES.md` (2026-09-12,
  two entries), which also hold the build record.

- **Each team's real manager decides the pitching change (SIM-427, CLOSED 2026-09-13 —
  FLIPPED into production the same day; plan `docs/audit/2026-09-13-sim427-build-plan.md`,
  record `CHANGES.md` 2026-09-13, two entries).** What production runs: the pitching change
  is a draw from the change opportunity pool (`SIM_MANAGER_DRAW=1`) with the live manager's
  usage-similarity score at power 4 (`SIM_ACTOR_POWER_MANAGER_USAGE`; the `manager_usage`
  matrix, 123 manager-seasons); the pen is the MLB box's per-game listing (Alembic 0025
  `raw.game_bullpen`, backfilled 2023-2026, `SIM_BULLPEN_SOURCE=box`; `pipeline/bullpen_usage.py`
  derives the pen — minus the last-five-games rotation, an opener's start under 45 pitches
  not counting — and each arm's rest / pitched-in-two-days / pitches-in-three); the entering
  arm is a second draw over that pen (`SIM_RELIEF_ROLE_SIGMA` 0.1, `SIM_RELIEF_REST_SIGMA`
  0.5, `SIM_RELIEF_PITCHED2D_OFF_WEIGHT` 0.25, `SIM_RELIEF_PITCHES3D_SIGMA` 10; hand and
  stuff off); the steal weight is the batting manager's measured rate over the league mean
  (`derived.league_averages`, entity `manager`). DELETED: the SIM-434 pull formula and its
  helpers, the by-name tendency reader, the reliever ranking, the pinch hit, the sac-bunt
  setup, the hit-and-run, the pitch-out and the green-light. `SIM_MANAGER_DRAW=0` = no
  pitching change (the unit lane). The evidence: the usage read on the balanced 45 games
  (4.45 pitchers per team-game vs the box's 4.28; starter pitches 84.6 ± 19.2 vs 84.7 ±
  17.4; the manager terciles' pull depth 80.8 / 85.4 / 87.9 vs their own 79.3 / 84.5 /
  88.4 — the formula read 2.28 / 80.5 ± 6.0 / flat) and the paired accuracy run on 250
  games of 2024 (the starter strikeout market −0.0156 Brier for the draw, range
  [−0.0255, −0.0066]; the moneyline −0.0062; the totals flat; every batter prop within
  ±0.0011). The fatigue composition read the fatigue plan flagged is repaired by the draw
  (the pitch share by band / times through sits on the pool's). Instruments:
  `scripts/probes/manager_probe.py`, `scripts/probes/bullpen_check.py`,
  `scripts/pair_accuracy.py` (the paired accuracy comparison). Two traps found on
  the way: `make calibrate` DROPS the win-probability reliability curve from
  `/data/calibration.json` (write it back from `/data/prop_validation.json` through
  `write_reliability_curve_to_calibration_report`, or the app boots uncalibrated); the
  change pool must carry `game_ymd` or the SIM-535 cutoff refuses every backtest game.
- **DO NOT run the profile recompute** (`make profile-computor`) — but the reason changed on
  2026-08-13. SIM-501a/c CLOSED: SIM-457 is re-landed on the events-based out label, no profile
  site reads `outs_on_pitch` any more (a unit test enforces this), and `outs_recorded` now matches
  official innings pitched within ~0.5% (verified against the 2024 IP leaders live). The recompute
  stays blocked on two things: **SIM-458** (the run-expectancy fix, still reverted) and the
  **SIM-488 re-sweep** — the swept `raw.pitches.outs` column (pre-play outs) is still
  stale-by-one-play on 46% of plate appearances, and the situation/RE24 features group by it.
  Sequence: close SIM-502a..d → re-sweep → re-land SIM-458 → then SIM-459.
- **Alembic 0018 is APPLIED, the SIM-488 re-sweep is COMPLETE (2026-08-13, 22,533 games), and
  SIM-459 is COMPLETE AND VERIFIED (2026-08-15).** The full all-seasons recompute chain ran:
  profiles, RE24 matrix, sim pools, FAISS tiles and the engine-artifact bundle all rebuilt from
  the re-swept data. Verified: 2024 median ERA 4.07 vs MLB ~4.08 (was 6.24 on the broken
  denominator); the 2024 pool's hit-with-out rows exactly match the raw-data measurement (290+66,
  zero home runs); league pull 0.443 > oppo 0.270. ⚠ A bare `player_profile_computor` run
  defaults to THE CURRENT SEASON ONLY — a full recompute must pass `--seasons 2017 … 2026
  --full-rebuild`, then verify `sim.pool_build_metadata.builder_version`. Remaining before the
  new numbers reach users: the SIM-450 acceptance lane, regression vs the new artifacts,
  calibration refit, SIM-491 flag re-validation. All four SIM-502
  defects (a..d) closed; the THIRD adversarial review ran (four angles, ~1,100 game-loads) — one
  confirmed fix landed (mid-PA pitcher attribution on pickoff/stepoff rows) and SIM-504 was filed
  for the consumer wiring. The re-sweep (2017-2026, ~6 h, `scripts/resumable_sweep.py`; its
  logs and progress files were removed in the 2026-10-19 clean-up, so a new sweep starts
  clean) rewrites `raw.pitches` with the fixed parser AND fills
  `raw.play_events` — the first `intent_walk` rows this database has ever held. **SIM-458 is
  RE-LANDED** (2026-08-13, the c11c919 run-expectancy fix, verbatim). After the sweep: SIM-459
  (the recompute picks up SIM-501a/457/503 + 458 + the corrected pool `result_outs` in one pass).
- **The re-sweep takes ~6 hours, not 55.** Measured from its progress files: 2017-2025 ran in
  6 h 9 m. The old figure took a SPAN between file timestamps as a duration.
- **One cutoff per profile table (SIM-551, 2026-09-16).** Eight `derived.*` profile tables
  carry `asof_date`, the date a row's data runs through, and every engine refuses a table
  stamped at two dates. A partial recompute (`--seasons` a subset) on a later day used to
  leave exactly that behind — the batter table sat at 2026-09-10 / 2026-09-11 from 09-11 to
  09-16 and the app booted `build_all_engines: 10/11`. Now the computor refuses such a
  rebuild BEFORE it writes and stamps the whole table after its insert. The rule for a
  partial recompute: include the season in progress in `--seasons` (the CLI default is the
  current season). The batter table was rebuilt at one date by a one-off script
  (11 s, the app stopped; removed in the 2026-10-19 clean-up); the batter matrix came back
  byte-identical.
- **The lineup table's position is the one each starter STARTED at (SIM-559, FOUND + BUILT
  2026-10-06; MERGED and the backfill RAN 2026-10-07: 17,279 rows repaired in 10,415 games, zero
  failures; every team-game now holds eight gloves and a catcher; CLOSED 2026-10-07).** The loader wrote `raw.game_lineups.position_code`
  from the box feed's `position`; the feed's `allPositions[0]` is the starting position. 26% of
  team-games (10,802 + 1,091 of 45,484) have a fielding hole in the defense map and 301 have no
  catcher; the balanced set has 29 defective team-games in 90; the box-score table carries the
  same last-position value and keeps it (the official box shows it). The loader now stores
  `allPositions[0]` (a two-way starter who pitches and bats as the designated hitter is 'P');
  `scripts/probes/backfill_start_positions.py` repaired the loaded games (one UPDATE per game,
  1 h 44 min at `--sleep 0.1`, Postgres only, the app served throughout; the record and the
  census after are in `CHANGES.md` 2026-10-07). A game
  the nightly loaded between the merge and the backfill's pass keeps the old coding: `--seasons
  2026` repairs it. The 45 × 130 lane of 2026-10-07 (`CHANGES.md`) reads the same six
  non-passes as 2026-10-03 at like sizes; no band moved. The API answers 503 with Retry-After,
  not 404, for a known game whose lineup has no pitcher or no batting order
  (`LineupIncompleteError`, a sibling of the unpublished-lineup error; a game not in `raw.games`
  still answers 404).
  The ten-game smoke (`CHANGES.md` 2026-10-07): the fielder factor read a missing or wrong
  defender on 7.4% of balls in play, the steal and pickoff draws had no catcher on 4.9% of their
  reads, no channel moved beyond noise (every power is 1). `scripts/balanced_game_set.py` reads the
  starters from the box now (89 of 90 before). Plan and run book:
  `docs/audit/2026-10-06-sim559-starting-position-plan.md`.
- **The game page reviews a simulated game (SIM-560 + SIM-561, CLOSED 2026-10-08).** "Load
  projections" runs its 100 games on the worker pool (`BatchRunner.run_prop_set`, 33-36 s; it
  replayed them one by one in the API process, about 150 s), a click on a stat reads the cached
  seeded run (0.1 s), and the panel names each player by team. The linescore and play-by-play
  panels show ONE simulated game: the "Simulate a game" button (`POST /sample-game`, about 5 s)
  or the projections' first game. The replay tables live in a file of their own,
  `/data/replay.duckdb` (`REPLAY_DUCKDB_PATH`, `REPLAY_PERSISTENCE_ENABLED=1` in compose), which
  only the app opens writable; the analytics file stays read-only, so the rebuilds and scripts
  are not blocked. DuckDB 0032 (schema v32) adds each replayed pitch's inning, half, outs,
  batter, pitcher and score; the API records the game on a worker, so its own process never
  loads the sim bundle. The file numbers its own runs; only `/simulate` writes a Postgres
  `sim.sim_runs` row. The page reads `/card` (linescore, run id, seed), then `/plays?run_id=`;
  the file keeps 5 runs per game.
- **The Daily Diamond slate and the live, schedule-driven game day view (SIM-519, BUILT,
  MERGED and DEPLOYED 2026-10-09; the game-day live check PASSED and the ticket CLOSED 2026-10-13).** The
  owner's Claude Design ("Daily Diamond MLB tracker") is the frontend's look: the tokens keep
  their `--sim-*` names with the design's values, the fonts and the 30 team logos are bundled.
  The slate reads the league schedule (`pipeline/mlb_schedule.py`, `api/league_feed.py`;
  `source` says schedule / schedule_cached / db); each card opens in place to the real game
  (`GET /api/games/{pk}/feed`) and the book's lines with their settlement (`GET
  /api/betting/games/{pk}/card-odds`). A Preview game's posted lineup is written by the live
  service (`raw.game_lineups.source = 'published'`), so it can be simulated; the projected
  lineup is built and OFF (`LIVE_PROJECTED_LINEUPS`). The live service runs in its own compose
  service `live` and publishes on Redis; never set `LIVE_PIPELINE_ENABLED` in the app. A
  simulation run is a durable `sim.sim_runs` row (`POST /simulate`, `/simulate/runs/*`,
  `api/sim_jobs.py`). The nightly finals job runs through `scripts/with_retry.sh`; the scheduler
  starts with the stack. Alembic **0029** carries every new column. The record and the run book:
  `CHANGES.md` 2026-10-09. A game that STARTED with no stored lineup gets its starting rows from
  its feed (`PublishedLineupWriter.on_live`), and the what-if builds its base state from the feed
  when the table is empty (`fallback_rows`; 2026-10-13).
- **A rebuild runs while the app serves (SIM-524, CLOSED 2026-10-10).** A DuckDB writer
  opens the file only when no other process holds it, read-only handles included. The
  blocker was never the forkserver: `/proc/*/fd` in the app container showed ONE handle,
  read-only, in the uvicorn server process, the park-factor connection that
  `prime_park_factor_source` kept open for the life of the app. The prime now reads the run
  factors into memory and closes the file (`simulation/sim_kwargs.py`); a lookup re-reads
  them when the file's mtime changes, at most once a minute, and keeps the old snapshot
  while a rebuild holds the lock. Every other open (the engine builds, the worker loads)
  already closed after its load. The nightly profile + bundle rebuild job is ON
  (`deploy/ofelia/config.ini`, 08:00 UTC). The three gaps it left are closed by SIM-563
  (the next bullet).
- **The nightly rebuild reaches users by itself (SIM-563, 2026-10-10).** The bundle
  `/data/play_pool/engine_artifacts` is a LINK to a version under
  `engine_artifacts.versions/<build id>/` (`pipeline/batch/bundle_versions.py`). A build with
  no `--out-dir` copies the current version, builds into the copy and moves the link in one
  step; a failed build moves nothing; pruning keeps three versions plus any a running app
  marks in use. The app pins the version it booted with (`BASEBALL_ENGINE_ARTIFACT_DIR`, set
  before the pool starts), so a respawned worker reads the parent's version. The workers read
  the IBB rates and the venue run factors from the bundle (`--what reference`), never DuckDB.
  When the link moves, the app waits for its run jobs (at most 10 minutes) and for any DuckDB
  writer, then restarts itself (`api/bundle_watch.py`, `SIM_RESTART_ON_NEW_BUNDLE=1` in
  compose; SIGTERM to PID 1, `restart: unless-stopped`). A boot while a writer holds the file
  waits for it (`SIM_BOOT_LOCK_WAIT_S`, 30 minutes) instead of crashing, and a failed
  park-factor read retries every minute. An explicit `--out-dir` still writes in place.
  The rebuild's first step waits for the night's finals load (`scripts/wait_for_ingest.sh`,
  reading the `/data/ingest_marks` that `scripts/with_mark.sh` writes) and runs it when it has
  not run that day. The jobs in the jobs container run as `appuser` (the Postgres job of the
  scheduled backups runs as `postgres` in the db container). Since the scheduled backups
  (SIM-525) the rebuild runs inside `with_mark.sh` and leaves `nightly-rebuild.running` while
  it writes; the data job waits on that mark before it opens the DuckDB file.
- **The three data stores back up nightly, and a real restore was drilled (SIM-525, CLOSED
  2026-10-15).** A store is one of the three: the Postgres statistics database, the DuckDB
  analytics file and the engine-artifact bundle. A set is one backup of one store. Two Ofelia
  jobs run at 10:00 UTC, each with `no-overlap`. The Postgres job runs `db/backup/pg_backup.sh`
  in the `db` container as the `postgres` OS user over the unix socket (no password; the client
  matches the server). The data job runs `python -m pipeline.backup.data_stores` in the `jobs`
  container as `appuser`, inside `with_mark.sh` and `with_retry.sh 2`. The layout under
  `BACKUP_DIR`: `postgres/<TS>/` (a `pg_dump -Fd` dump, the `count(*)` of every table before
  and after it, `manifest.json`), `duckdb/<TS>/` (the file, its `.wal` when present, the small
  `/data` files, `manifest.json`), `bundle/<TS>/<build id>/` and `drills/<TS>.json`; TS is the
  UTC time `%Y%m%dT%H%M%SZ`. A set is written as `<TS>.partial`, `manifest.json` goes last,
  then the folder is renamed; the next run deletes every leftover `.partial`, and a set with no
  manifest never counts. The data job waits on the rebuild's, the finals' and the sweep's
  running marks, opens the DuckDB file read-only, copies it and requires equal counts on the
  copy; `replay.duckdb` is not backed up. Each store keeps 3 sets (`BACKUP_KEEP`). The
  free-space guard needs max(10 GiB, 1.5 × the newest set) free on `/backups`. When space is
  short, the guard deletes the oldest set while more than 2 remain. Then it refuses the run.
  The gauge `baseball_sim_backup_age_hours` in `/metrics` and `make backup-status` give the
  age of each store's newest set. The sets protect against five losses: a Docker disk-image
  loss or reset, `docker compose down -v` / `make nuke`, a bad migration or rebuild, a writer
  that dies in the middle of a rebuild, and an accidental deletion. They do NOT protect
  against the laptop's disk dying or the loss of the laptop. `BACKUP_DIR` is on C:, the only
  disk. The off-site copy (SIM-568, CLOSED 2026-10-17) closes that gap. At 10:30 UTC the
  Ofelia job `nightly-backup-offsite` waits for the two backup jobs. Then it copies the newest
  three complete sets of each store into the OneDrive folder
  `C:/Users/grego/OneDrive/Documents/baseball_backups` (`OFFSITE_BACKUP_DIR`), and the OneDrive
  client uploads them. The gauge `baseball_sim_offsite_backup_age_hours` gives the age of each
  store's newest off-site copy. `make backup-offsite` copies by hand, and
  `make backup-drill OFFSITE=1` restores from the copy. Its design and run book:
  `docs/audit/2026-10-17-sim568-offsite-copy.md`. The record of 2026-10-17: nine sets
  (20.8 GiB) copied at about 22 MB/s through the Docker file server in two runs (774 s +
  507 s); a rerun that copies nothing takes 2 s; the drill from the copy PASSED in 881 s (every
  table count exact; the DuckDB and bundle copies verified through `/offsite`); the OneDrive
  upload runs at about 3.3 MB/s. One trap: `pg_dump` makes its dump folder 0700, which the
  jobs container's `appuser` cannot open, so `pg_backup.sh` now opens each set before its
  manifest lands. The copy runs 10:30 to about 10:45 UTC, so recreate `jobs`, `app` or
  `scheduler` only after about 11:00 UTC, with no `.running` mark. The restore run book:
  `docs/audit/2026-10-14-sim525-backups-plan.md`. The record of 2026-10-15: the DuckDB set
  copied 3,839,122,442 bytes in 201 s with equal counts; the bundle set 588,464,368 bytes in
  46 s; the whole data run 308 s; the Postgres set 2,895,149,569 bytes (2.7 GiB) in 267 s. The
  restore drill: PASS in 450 s on 2026-10-15 (pg_restore 349 s;
  all 36 tables inside their count brackets; the DuckDB and bundle copies verified; the scratch
  cluster removed; the report `baseball_backups/drills/20261015T051919Z.json`). The record: `CHANGES.md` 2026-10-15.
- **The game page opens with the real game and runs a "what if" from any play (SIM-562,
  2026-10-09).** A live or final game shows the league feed's linescore, box score and
  play-by-play above every simulation (`GET /feed/plays`). "What if from here" on a play reads
  the game at the start of that plate appearance (`GET /feed/state?at_bat=`,
  `pipeline/mlb_game_feed.state_at_pa`: the lineups, the defense, the runners, the score, the
  pitch counts, the bench and the pen as they stood) and draws it on the slate's field. The user
  stages a pinch hitter, a reliever, a pinch runner or a defensive change
  (`simulation/whatif.apply_changes`; before first pitch, either side's lineup and starter), and
  `POST /what-if` queues TWO runs of 100 from that spot with the same seed, as it stood and with
  the changes (`sim.sim_runs.kind` = `whatif_base` / `whatif_change`, Alembic **0030**). The loop
  takes the spot as the plain-data sim-kwarg `start_state` (`simulation/start_state.py`); the
  real runs before it are spliced into each game's inning grid. Every "latest run" read takes
  only `kind = 'pregame'`. The Betting card grades each side on the real final (`result`,
  `final` on `/edges`) and shows no price on a market with no stored line. Projections and
  Betting are collapsible and start collapsed; the Line movement and Managerial override boxes
  are gone (the `/simulate/with_override` endpoint stays). The record: `CHANGES.md` 2026-10-09.
- **The stack is hardened (SIM-443a–f, BUILT 2026-10-13, DEPLOYED 2026-10-14; the run
  record is in `CHANGES.md`).** What the stack runs: Postgres, Redis, the app,
  Prometheus and Grafana listen on `127.0.0.1` only; nginx on port 80 stays on the LAN. Three
  Postgres roles (`db/roles/`, `make db-roles`): `baseball_user` the owner, used only by the
  `migrate` service; `baseball_app` (row reads and writes in `raw` and `sim`, no DDL) for the
  app, live and jobs services; `baseball_ro` (SELECT on `raw` only) for the SQL console. No
  password has a default: compose builds every DSN from required `.env` secrets
  (`scripts/gen_secrets.py`), and a script with no `BASEBALL_DB_DSN` fails
  (`pipeline/db_dsn.require_dsn`). The three nightly data jobs exec into the `jobs`
  container as `appuser`; the Ofelia INI holds no credential. Every API route and the live
  WebSocket require a login (the session cookie, `X-API-Key` or a Bearer key);
  `AUTH_DISABLED=1` is the one opt-out, warned at boot and shown on `/health`
  (`auth_enforced`). The SQL console reads `raw.*` only (a sqlglot parse of every query) and
  runs only on the read-only pool; without that pool it answers 503. Redis requires a
  password, and the sim cache stores typed JSON (`simulation/cache_codec.py`, keys `sim443:`),
  never a pickle. The tests' conftest sets `AUTH_DISABLED=1`; two auth test files remove it.
  Follow-ups: the Data Lab read routes on the read-only pool (SIM-566) CLOSED 2026-10-18; the bundle loads with `allow_pickle=False` (SIM-565) CLOSED 2026-10-18; the rate limiter's bucket (SIM-564) and its proxy-address follow-on (SIM-569, `TRUSTED_PROXY_HOSTS`) both CLOSED 2026-10-17; nginx re-resolves the app after a recreate (SIM-570) CLOSED 2026-10-18; the Data Lab rates on the pitch-result code (SIM-571) CLOSED 2026-10-19; the Data Lab reads the regular season alone by default, `game_type=all` adds the postseason (SIM-572) CLOSED 2026-10-19. The design and the cutover run book:
  `docs/audit/2026-10-13-sim443-security-hardening.md`; the record: `CHANGES.md` 2026-10-13.
- **The project-wide code clean-up (SIM-493, CLOSED 2026-10-20).** No hand-written file is
  over 2,000 lines. The eight files over 2,500 lines became packages, one job per file:
  `simulation/loop/` and `simulation/sampler/` hold the mixins of `StateMachine` and
  `FullPoolSampler` (each class keeps its name and its module); `pipeline/batch/profiles/`,
  `pipeline/batch/artifacts/`, `pipeline/etl/historical/`, `pipeline/live/ingestion/` and
  `pipeline/bettingpros/` hold the five pipeline files' parts; `api/routes/games_*.py`,
  `api/routes/betting_*.py` and `api/schemas/` the API's; `similarity/calibration/` the
  calibrator's; `scripts/accuracy/` the accuracy comparison's. Import a name from its home:
  the old modules re-export nothing. The scripts and tests are named by purpose; run outputs
  go to `results/` (ignored by git and Docker); `simulation/flags.py` registers every `SIM_*`
  flag; `db/introspect.py` is the one existence probe; mypy checks all five program packages.
  `scripts/behaviour_fingerprint.py` proved the simulator byte-identical to master `fe81d79`
  (400 + 200 seeded games on the in-memory bundles, 180 on the real one). The review found 49
  defects, filed unfixed as SIM-575 to SIM-623; the ten P2 rows change what production
  computes. The record: `CHANGES.md` 2026-10-20 (the close entry and the five area entries);
  the plan and the rename tables: `docs/audit/2026-10-19-sim493-code-cleanup-plan.md`.
- **Sample hundreds of games when validating ETL work, never dozens.** Two adversarial review rounds
  found four defects each, all from real payloads at scale, none from reading code. A 70-game sample
  reported "100%" on a metric that 950 games disproved.

