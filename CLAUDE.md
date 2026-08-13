# CLAUDE.md — MLB Baseball Simulation Platform

> Project guide for Claude Code. This is a pitch-by-pitch Monte-Carlo MLB game simulator built and run
> as a **sports-trading hedge fund**: the product is player-prop prediction and betting-edge validation,
> anchored to **Closing Line Value (CLV)** as the gold-standard metric. Work is executed by a **9-agent
> team** with cross-validation. Read this file first, then `docs/HANDOFF_PHASE6.md` before starting work.

---

## 1. Purpose & goals

- **What it is:** ingests real Statcast pitch-by-pitch data, builds **11 similarity engines** across
  player/situation dimensions, and runs a stochastic pitch-by-pitch game simulator that produces 100
  independent game iterations per request. Outputs feed win probabilities, per-player prop distributions
  (PMFs), boxscores, and a betting/CLV surface.
- **Primary use case:** predict player props (pitcher Ks, batter hits/HR/TB, etc.) and identify +EV
  betting edges, validated against CLV. Every model number must ultimately be *calibrated* — uncalibrated
  numbers must not reach users.
- **End state:** a live game dashboard (day slate → 3-state game cards → game page with play-by-play,
  per-player projections, linescore, field graphic, managerial override) on top of the completed API.

## 2. Current status (read this)

- **▶ STATE AS OF 2026-06-06 (TL;DR — this bullet is authoritative; the rest of §2 is the dated log).**
  - **Phases 1–6 COMPLETE and CI-green** on **Python 3.13 / numpy 2.x** (SIM-431). Frontend shipped as
    **React 18 + Vite + TypeScript** (SIM-378 / ADR-001). DuckDB schema **v13**, Alembic head **0015**.
  - **Calibration is LIVE** (SIM-432): `/data/calibration.json` fitted + applied at boot; win-prob map =
    fitted reliability-curve. Post-SIM_MANAGER validation (120 games): win-prob **ECE 0.047**, batter
    **H/HR/TB ECE 0.02–0.05** (bettable); pitcher **K/BB ECE 0.22/0.21** (improved from 0.52/0.37, not yet
    bet-grade).
  - **Production sim path = the full-pool similarity sampler** (`SIM_FULL_POOL=1`); per-tile FAISS k-NN is
    the fallback / unit-test default. **All production realism flags are ON** in the docker-compose `app`
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
    **First result (120 games): ~49% beat-close = NO demonstrable edge yet** (stable across n=65/n=100;
    the trustworthy markets — moneyline + batter H/HR/TB — all ≤50%). The model isn't beating the sharp
    close; the remaining **SIM-429** work (close the **~7-8%** run-conversion gap + sharpen the edge
    estimates) is the path to a real edge. The full-season CLV run is executing. *(This bullet said "~10–12%" until 2026-08-10; see the §11 reconciliation note.)*
  - **`/simulate` perf (SIM-430 / SIM-436):** forkserver workers + a 10 GB app `mem_limit` → **n=100 ≈
    38 s at 6 workers, no OOM**. SIM-436 PROFILED the per-game cost: it is the IRREDUCIBLE per-PA
    full-pool scoring (~1.5–1.9 s/iter × ~83 PAs); the machine build is free and the host is **core-bound
    at ~6**, so a single game can't go <30 s on this hardware without fewer iters or a smaller pool. The
    throughput fix: **the CLV backtest is parallelized ACROSS games** (`--workers`, forkserver,
    byte-identical, ~373 MB/worker) → ~6× → **~20–32 s effective/game**; n=65 gives the same CLV as n=100.
  - **Next free ticket ID: SIM-438.** Open work: **SIM-429** (granular run-conversion + K/BB prop
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
  and is the **production default** (`SIM_FULL_POOL=1` in the docker-compose `app` env; per-tile
  path is the fallback and unit-test default, pinned off in `tests/conftest.py`).
- **DP-rate bug fix propagated 2026-05-28:** the player-profile computor's
  `dp_turned = outs_on_pitch >= 2` always-False bug was fixed and the 5.7-hour 2017-2025
  recompute completed.  Per-season DP rates now 42-48% (was 0.0).  Actor embeddings rebuilt
  (`fielder_emb` = 11346 × 51 features).  Box output now MLB-realistic: H/HR/2B/BB/K within
  ~3-5% of MLB-2023.  **Runs run ~7-8% low** (down from ~12% pre-fix) — this is the AUTHORITATIVE
  run-conversion figure; see the reconciliation note at the head of §11.  The remaining
  hits→runs *conversion* residual lives in batted-ball-with-RISP / sequencing (see §11).
  ⚠ **This bullet used to end "steals match MLB volume". That claim is now wrong.** It was true
  when it was written, on 2026-05-28. The owner enabled `SIM_MANAGER` on 2026-06-04 and the
  steal gate closed. The SIM-450 acceptance lane measured the production configuration on
  2026-08-10 and read **SB = 0.0000 against 0.59 and CS = 0.0000 against 0.17** — exactly zero
  steals, across 400 game-sims. **SIM-495** holds that measurement; **SIM-474** is the fix.
  **Next free ticket ID at the time: SIM-433** (now **SIM-438** — see the TL;DR at the top
  of §2; SIM-430 = the full-pool `/simulate` throughput / 2s-30s SLA perf gap, filed 2026-05-30 off the
  SIM-402 live re-measure).

- **SIM-402 — CLOSED 2026-05-30 (code complete + re-measured live); the residual throughput
  gap is spun off to SIM-430.** Live API probed at
  `http://localhost:8000/api/games/{pk}/simulate`.
  - **Cold-worker fix shipped.** `production_machine_factory` passes `fingerprint_deriver=None`
    on the full-pool path (the deriver is unused there but `_default_deriver_builder` did 3 eager
    per-seed disk loads), and a BACKGROUND pre-warm (`BatchRunner.prewarm()` +
    `production_factory.warm_worker_cache()`, lifespan-gated on `SIM_FULL_POOL`, bounded-concurrency
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

## 2b. ⚠ IN-FLIGHT WORK — read before touching the ETL or running a recompute (2026-08-11)

**`docs/audit/2026-08-11-sim501-502-resumption-state.md` is the handover.** Read it first.

- **DO NOT run the profile recompute** (`make profile-computor`) — but the reason changed on
  2026-08-13. SIM-501a/c CLOSED: SIM-457 is re-landed on the events-based out label, no profile
  site reads `outs_on_pitch` any more (a unit test enforces this), and `outs_recorded` now matches
  official innings pitched within ~0.5% (verified against the 2024 IP leaders live). The recompute
  stays blocked on two things: **SIM-458** (the run-expectancy fix, still reverted) and the
  **SIM-488 re-sweep** — the swept `raw.pitches.outs` column (pre-play outs) is still
  stale-by-one-play on 46% of plate appearances, and the situation/RE24 features group by it.
  Sequence: close SIM-502a..d → re-sweep → re-land SIM-458 → then SIM-459.
- **Alembic 0018 is APPLIED (2026-08-13) and the SIM-488 re-sweep is RUNNING.** All four SIM-502
  defects (a..d) closed; the THIRD adversarial review ran (four angles, ~1,100 game-loads) — one
  confirmed fix landed (mid-PA pitcher attribution on pickoff/stepoff rows) and SIM-504 was filed
  for the consumer wiring. The re-sweep (2017-2026, ~6 h, `scripts/resumable_sweep.py`, log in
  `.sweep_progress/sweep_20260813.log`; the pre-sweep progress files are archived in
  `.sweep_progress_pre_sim501_20260813/`) rewrites `raw.pitches` with the fixed parser AND fills
  `raw.play_events` — the first `intent_walk` rows this database has ever held. **SIM-458 is
  RE-LANDED** (2026-08-13, the c11c919 run-expectancy fix, verbatim). After the sweep: SIM-459
  (the recompute picks up SIM-501a/457/503 + 458 + the corrected pool `result_outs` in one pass).
- **The re-sweep takes ~6 hours, not 55.** Measured from `.sweep_progress/`: 2017-2025 ran in
  6 h 9 m. The old figure took a SPAN between file timestamps as a duration.
- **Sample hundreds of games when validating ETL work, never dozens.** Two adversarial review rounds
  found four defects each, all from real payloads at scale, none from reading code. A 70-game sample
  reported "100%" on a metric that 950 games disproved.

## 2a. Operational caveats (Windows + Docker)

- **`scripts/` is NOT volume-mounted** into the running app container; only `api/`, `pipeline/`,
  `similarity/`, `simulation/`, `db/` are (see `docker-compose.yml` line ~110).  Edits to
  `scripts/` are picked up by the running container only after `docker compose build app` +
  `docker compose up -d app` (recreate).  Edits to the mounted dirs are picked up by
  `docker compose restart app` alone.
- **Git Bash on Windows mangles container paths.** Any `docker compose exec` / `docker compose run`
  command from Git Bash that uses a Linux container path like `/app/scripts/foo.py` gets translated
  to `C:/Program Files/Git/app/scripts/foo.py` before Docker sees it.  Prefix with
  `MSYS_NO_PATHCONV=1` to disable the translation.  Tell: the error message includes
  `C:/Program Files/Git/`.
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
- Canonical git repo: this directory. Primary shell: **Windows Command Prompt (cmd.exe)**;
  development + tests run through Docker (`docker compose run --rm app ...`).

## 3. Tech stack

Python 3.13 · FastAPI · Pydantic v2 · PostgreSQL (async SQLAlchemy + Alembic) · **DuckDB** (in-process,
no container — postgres extension) · Redis · scikit-learn (GMMs) · **FAISS** · NumPy/pandas · scipy · POT
(Wasserstein) · pybaseball · Docker / docker-compose · nginx · Prometheus + Grafana · pytest (+asyncio,
cov, timeout, benchmark, mock, hypothesis) · ruff (lint+format) · mypy. **Frontend: React 18 + Vite +
TypeScript** (chosen in SIM-378 / `docs/architecture/2026-09-02-adr-frontend-framework.md`, "Accepted"),
with Playwright e2e — talks to the API over REST + a typed WebSocket.

## 4. Architecture (layered)

```
Data sources (MLB Stats API REST+WS · Statcast/pybaseball)
  → Data layer: PostgreSQL raw.* + DuckDB derived.*/sim.* ; ETL + nightly profile pre-compute (pipeline/)
  → 11 similarity engines (similarity/engines/) : GMM-W2 pitcher, RBF batter/fielder/baserunner/
    catcher/pitcher-steal/manager, KDTree situation, FAISS pitch-to-pitch + batted-ball
  → Play pool (sim.pitch_pool / sim.outcome_pool + FAISS tiles)  [Phase 3]
  → Full-pool similarity sampler (simulation/full_pool_sampler.py over the SIM-422 engine-artifact
    bundle) : scores the WHOLE same-hand pool by the applicable engines (factorized weights:
    f_pitcher·f_batter·f_situation·recency; count-bucketed pitch draw) — the PRODUCTION path
    (SIM_FULL_POOL=1). The per-tile FAISS k-NN sampler is the fallback / unit-test path.  [SIM-422→429]
  → Core sim loop (simulation/sim_loop.py) : 8-step pitch-by-pitch state machine + manager/situational
    decisions → GameSimResult                                     [Phase 4]
  → Runner + API (simulation/batch_runner.py, api/) : 100-iteration ProcessPool runner (forkserver
    workers — SIM-430), REST + WebSocket, Redis cache, persistence (DuckDB v13 / Alembic 0015),
    betting/CLV surface, auth/rate-limit/CORS, nginx, Prometheus/Grafana   [Phase 5 — COMPLETE]
  → Frontend (frontend/) : React 18 + Vite + TypeScript, Playwright e2e   [Phase 6 — COMPLETE]
```

## 5. Repository map

- `api/` — FastAPI app. `main.py` (create_app + lifespan), `routes/` (games, betting, metrics, similarity),
  `schemas.py` (Pydantic), `serialization.py` (numpy-safe `to_jsonable`), `auth.py`, `state.py` (engine build).
- `simulation/` — `sim_loop.py` (the simulator, biggest file; full-pool draw + engine-backed
  advancement/steal/framing live here; also the SIM-434 manager fatigue/rest/TTO + reliever-selection
  helpers, all gated `SIM_MANAGER`), `full_pool_sampler.py` (SIM-423 full-pool similarity sampler:
  count-bucket CDFs, batted-ball draw, `runner_rate`/`catcher_framing`; reads the SIM-430 dense
  `pitcher_sim_matrix` fast path), `matchup_provider.py` (SIM-421 fork-safe deriver/centroid provider),
  `game_state.py` (carries bat/throw hands + per-team pitcher/catcher ids + the SIM-434 per-pitcher
  rest / pitch-count fields), `results.py`, `batch_runner.py` (ProcessPool runner — `mp_context=forkserver`
  per SIM-430), `production_factory.py` (builds the full-pool sampler from disk per worker when
  `SIM_FULL_POOL` is set; `_manager_enabled()` + synthetic-bullpen builder gate SIM-434), `lineup_resolver.py`
  (also resolves the per-team catcher via the SIM-363 defense map), `linescore.py`, `pitcher_decisions.py`
  (W/L/S + the manager pull model), `play_recorder.py`, `prop_distributions.py`, `win_probability.py`,
  `snapshots.py`, `score_fusion.py`, `fingerprints.py`, `validation/replay_chi_squared.py`.
- `similarity/` — `engines/` (the 11 engines), `similarity_calibration.py`, `backtesting/` (backtester +
  walk-forward), `registry.py`.
- `betting/` — `clv_engine.py`, `bet_signal.py`, `line_movement.py`.
- `pipeline/` — `etl/` (historical loader + `coercion.py`, the SIM-437 shared type-coercion helpers
  imported by both ETL loaders), `live/live_ingestion_pipeline.py` (MLB WS + REST + odds),
  `live/bullpen_availability_ingest.py` (SIM-433 MLB-API active-roster/IL → `raw.game_bullpen_availability`),
  `batch/player_profile_computor.py` (+ the SIM-433 `_compute_bullpen_workload` from `raw.pitches`) +
  `play_pool_cache.py` (normalized tiles + persisted norms/centroids) + `engine_artifacts.py` (SIM-422
  builder + per-worker loader for the full-pool bundle: hand pools, pitcher×pitcher sim — incl. the
  SIM-430 dense `pitcher_sim_matrix` — batter/catcher/fielder/baserunner/manager embeddings, batted-ball
  pools), `odds_provider.py`, `bettingpros_odds_provider.py` (SIM-435 `closing`-line branch).
- `scripts/` — operational scripts (NOT bind-mounted; see §2a): `sim_stats.py` (v2 sim harness),
  `load_historical_odds.py` (SIM-435 opening+closing backfill → `raw.game_odds`/`raw.prop_odds`),
  `clv_backtest.py` (SIM-429 CLV scoreboard: sim → model prices → opening/closing → CLV per market/side;
  `--workers` parallelizes across games — SIM-436), `validate_props.py`, `check_file_integrity.py`.
  *(scripts/ is baked into the image; run a not-yet-rebuilt new script via
  `docker compose run --rm -v "$PWD/scripts:/app/scripts" app python scripts/<x>.py`.)*
- `db/` — `migrations/` (Alembic, head **0015**) + `migrations/duckdb/` (numbered SQL, schema **v13**) +
  `schemas/duckdb_schema_version.txt`.
- `tests/` — `unit/`, `regression/` (golden-file engine-drift gate), `integration/` (E2E TestClient),
  `performance/` (pytest-benchmark). `conftest.py` has shared fixtures + the event-loop guard.
- `deploy/` — nginx + Prometheus/Grafana. `frontend/` — **React 18 + Vite + TypeScript** app
  (`src/`, `components/`, `pages/`, `graphics/`, `e2e/` Playwright, `vite.config.ts`, `openapi.json`).
- `docs/` — `HANDOFF_PHASE*.md`, `SPRINT_*.md`, `audit/`, `architecture/`. Root: `BACKLOG.md`,
  `CHANGES.md`, `agent_team.md`, `README.md`, `WORKFLOW.md`, `PRODUCT_GUIDE.md`.

## 6. The 9-agent team (see `agent_team.md` for full scopes)

1. **Product Manager** — requirements, backlog, phase sequencing, prioritization.
2. **Baseball Analyst** — domain validation, feature selection, run-environment realism, manager logic.
3. **ML / Modeling Engineer** — the 11 engines, GMM/RBF/FAISS math, calibration, backtesting/ablation.
4. **Data Engineer** — Postgres/DuckDB schema, ETL, live ingestion, nightly profiles, migrations.
5. **Backend Developer** — sim-loop wiring, FastAPI, WebSocket, Redis, the runner.
6. **Performance Engineer** — throughput SLA (2s/game, 30s/100-game batch), FAISS tuning, vectorization.
7. **UX Designer** — frontend wireframes, design system, components (owns Phase 6 build design).
8. **Betting / Markets Analyst** — CLV framework, odds integration, edge/+EV identification, props.
9. **QA / DevOps** — tests, CI/CD, Docker, deployment, monitoring; the independent cross-validation pass.

**Invoke a role by name** (e.g. "Baseball Analyst: review the manager pull-timing logic"). The PM
consolidates; QA cross-validates and never self-certifies its own work.

## 7. Development workflow & conventions

- **Sprint workflow:** for each sprint, role agents implement their owned tickets (partition by file
  ownership to avoid concurrent edits to the same file), then an **independent QA cross-validation pass**
  runs the full suite. Document in `CHANGES.md` (grows, per-agent detail), trim `BACKLOG.md` to one-line
  rows under a sprint banner, and add `docs/SPRINT_<date>_<name>.md`. (`backlog.xlsx` was RETIRED
  2026-06-04 — it had no generator and drifted badly; **`BACKLOG.md` is the single source of truth**.)
- **TDD:** tests first, then implementation (Backend Developer convention). Unit tests use the `__new__`
  constructor-bypass + in-memory mock pattern (no live DB) — see `tests/conftest.py`.
- **Ticketing:** every change maps to a `SIM-NNN` ticket. Next free ID is tracked in `BACKLOG.md`
  — read it there; do not trust a number copied into this file (this line once said SIM-438 while
  the true next ID was SIM-504). Recent IDs: SIM-437 = consolidate the two ETL loaders' duplicate type-coercion
  helpers into `pipeline/etl/coercion.py` [CLOSED 2026-06-22], SIM-430 = full-pool `/simulate` throughput / 2s-30s SLA
  (worker-scaling CLOSED, per-game cost → SIM-436), SIM-431 = the Python-3.13 migration [CLOSED],
  SIM-432 = the calibrator/validate_props ↔ live-schema reconciliation [CLOSED 2026-06-01, the
  SIM-406/407 unlock], SIM-433/434/435 = bullpen-availability / manager-decision-model / historical-odds
  loader [code-complete, data-runs pending], SIM-436 = revisit `/simulate` per-game cost for the 30 s
  SLA [P3, open], and the SIM-422→429 full-pool epic is filed under its own banner. NOTE: a
  realism-work batch was tagged `SIM-421` *in code comments* before the epic was filed — `SIM-421` the
  ticket is the P3 book-offered-market projection, so treat in-code `SIM-421` tags as the realism work
  and reconcile if you touch them.
- **Migrations (mandatory):** every Postgres schema change ships an Alembic migration in
  `db/migrations/versions/`; every DuckDB schema change ships a numbered SQL file in
  `db/migrations/duckdb/` AND increments `db/schemas/duckdb_schema_version.txt`. *Gotcha:* a past sprint
  bumped a DuckDB migration but forgot the version file + its sanity test — always verify
  version-file == latest-migration-number after a DuckDB schema ticket.
- **Regression gate:** `tests/regression/` holds golden-file + property tests detecting engine drift.
  Regenerate fixtures with `python tests/regression/generate_fixtures.py --force` (only when a model
  change is intentional). After any engine refactor, run the regression suite — a past columnarization
  silently broke the situation-engine golden files.
- **Secrets:** never commit credentials; the DSN is read from `BASEBALL_DB_DSN`. There is a CI
  `secrets-check` job and a `file-integrity` guard (`scripts/check_file_integrity.py`, ast.parse +
  null-byte scan).

## 8. Commands (run from the repo root; Windows cmd.exe)

The `Makefile` wraps Docker (no local Python install needed):

```
make dev               # build + start all services (db, redis, app) foreground
make down              # stop + remove containers/networks
make migrate           # apply all Alembic migrations (db must be healthy)
make test              # full suite (unit + integration)
make test-unit         # unit tests only (no Docker)
make test-regression   # golden-file engine-drift gate
make test-integration  # testcontainers (Postgres + Redis)
make lint              # ruff check
make format            # ruff format
make type-check        # mypy
make profile-computor  # nightly: rebuild DuckDB profiles + sim pools
make play-pool-cache   # nightly (after profile-computor): materialize FAISS tiles
make calibrate         # fit /data/calibration.json (arsenal W2 + per-engine sigmas) — SIM-406/432
make validate-props    # SIM-407 prop-PMF / win-prob validation (add --write-calibration for the curve)
```

Raw equivalents (if running Python directly, target **Python 3.13**):

```
pytest tests/unit/ -m "not slow" --cov=similarity --cov=pipeline --cov=simulation --cov=betting --cov=api
ruff check .   &&   ruff format --check .
mypy similarity/ pipeline/ api/        # CI scope; config in pyproject.toml; pin mypy>=1.8,<2
```

## 9. Testing & CI

- **CI = `.github/workflows/ci.yml`**, 8 jobs on every push/PR: lint (ruff), type-check (mypy),
  **unit-tests + 80% coverage gate**, regression, e2e (SIM-371 TestClient), secrets-check, file-integrity,
  docker-build-check. Plus weekly `integration-weekly.yml` (testcontainers) and a perf job that hard-gates
  the `/simulate` SLA under `PERF_STRICT`. `docker-release.yml` pushes the API image to ghcr on main.
- **CI Python is 3.13.x (SIM-431, migrated from 3.11 on 2026-05-30 so CI + Docker + local dev all
  match).** numpy is now 2.x (cp313 has no numpy-1.26 wheel; the codebase uses no numpy-2-removed APIs).
  The coverage gate is 80 (currently met at 89%). CI uses `--tb=native`
  (a pytest `--tb=short` renderer bug — `tb_lineno=None` INTERNALERROR — can otherwise mask real failures).
- **Slow tests** (~15) are `@pytest.mark.slow` and currently run in the default unit lane at
  `--timeout=30`; SIM-418 will split them into a dedicated lane. A per-test `@pytest.mark.timeout(N)`
  overrides the CLI timeout (used for the 5000-game exhaustive test).
- **Coverage tip:** measure with `coverage run --parallel-mode` + `coverage combine` (NOT `--cov-append`
  across processes, which under-counts).

## 10. Established design decisions (do NOT relitigate without strong justification)

- GMM covariances stored in standardized space; arsenal W₂ calibrated to Statcast (~0.5–12, median ~2.84);
  linear-exponential `exp(-W₂/4.10)` (not squared). Target median similarity 0.50 across engines.
- EB_N_PRIOR=15 for the fielder engine (defensive metrics stabilize slowly); lower for pitcher/batter.
- Position-partitioned fielder engine (no cross-position scoring). Release-point sub-score excluded from
  the pitcher engine. Compositionally redundant batter features removed (ld_rate, iffb_rate, center_rate).
- DuckDB is in-process (no container). MLB WebSocket is treated as a pure change-signal; all state is
  re-fetched from REST. All `CREATE TABLE` use `IF NOT EXISTS`.
- Run-value constants: 0.75 runs/out saved (IF), 0.90 (OF), 0.25 runs/block, 0.125 runs/strike.

## 11. Known defects / dead-wiring + verification debt (from the Phase-5-close audit)

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

**Full-pool realism residual (SIM-422→429, the production path):** box rate stats (H/HR/2B/BB/K) are
within ~4% of MLB, but **runs sit ~7-8% low** — a hits→runs *conversion*
gap, not a rate-stat or baserunning-aggression problem (advancement rates are already MLB-realistic; a
global advancement multiplier `SIM_RUN_CALIB` was investigated and rejected as the wrong lever). The
gap lives in batted-ball-with-RISP / sequencing. One concrete contributor identified + fixed: the
batted-ball draw conditions only softly on base-out, so ~55% of drawn double-play events landed with no
runner to double off — `_full_pool_fielding` now records a 2nd out only when a forceable runner exists
(else a 1-out field_out). The harness for the next calibration pass landed 2026-05-28 as
`scripts/sim_stats.py` v2 (defaults to 200 iters/game, reports per-channel + per-half home/away
splits + R standard error so a calibration sweep can target the right channel). Remaining
conversion gap → granular per-channel calibration on this larger harness (SIM-429 follow-on).
*Validation caveat:* run a multi-game × ≥400-sim batch before reading R-level moves; the per-channel
breakouts (RISP, advancement, DP rate) are the right lens, not the global R mean.

**⚠ STEALS DO NOT MATCH MLB VOLUME — corrected 2026-08-10.** The paragraph above used to open
"box rate stats are within ~4% of MLB and steals match MLB volume". The second half is wrong for
the configuration users get, so it is deleted here and at line 85. The SIM-450 acceptance lane ran
the production flags on 2026-08-10 and measured **SB = 0.0000 against 0.59, and CS = 0.0000 against
0.17** — no stolen base is ever attempted, so none is ever caught. `_full_pool_steal_decision`
(`simulation/sim_loop.py:3091`) was called **0 times** in 400 game-sims, while `_full_pool_outcome`
was called 123,205 times in the same run. The chain: `SIM_MANAGER=1` wires the default manager
profile with `steal_order_rate_per_1b_opp=0.08`, so `green > 0` at `sim_loop.py:2909`, so the
SIM-426 fallback at `:2949` never runs, so control reaches `resolver.resolve_steal` and the base
stub answers `attempted=False`. Production has attempted no steal since `SIM_MANAGER` was enabled
on 2026-06-04. **SIM-495** holds the measurement. **SIM-474** is the fix. Do not restore the
steals-match-MLB claim until the SB and CS bands pass with the production flags on.

## 12. Phase roadmap

| Phase | Name | Status |
|------|------|--------|
| 1 | Data Infrastructure & Pipeline | ✅ Complete |
| 2 | Similarity Engine Suite (11 engines) | ✅ Complete |
| 3 | Play Pool Architecture | ✅ Complete |
| 4 | Core Simulation Loop | ✅ Complete |
| 5 | Simulation Runner & Backend API | ✅ Complete (CI-green on Python 3.13) |
| 6 | **Frontend Build + P1 backend prerequisites** | ✅ **Complete** — SIM-378→401 + 415→420 + 414 + 402 + 408 closed; SIM-406 + 407 calibration LIVE (unblocked by SIM-432, 2026-06-01) |
| 7 | Integration, Testing & Deployment | **Largely COMPLETE.** Closed: SIM-402/408/431/432/430 (calibration live, forkserver perf), SIM-433/434/427/411/413/425b (bullpen ingest · manager-decision + realism models ENABLED + validated, all flags ON in prod / pinned off in tests), SIM-435 (full-2024-season odds backfill — 2,378 games), and the 2026-06-03 comprehensive-audit remediation (backlog.xlsx RETIRED). **CLV is now measured at scale** — SIM-429 scoreboard (`scripts/clv_backtest.py`) shipped; SIM-436 parallelized it across games (~6×); first read = ~49% beat-close (no demonstrable edge yet). **Remaining:** SIM-429 (run-conversion + prop calibration to DEVELOP a CLV edge; the realism magnitude-calibration + real-per-team-manager-profile follow-ons fold in). The single-game <30 s SLA is hardware-bound (core-bound at ~6) and de-prioritized — throughput is solved via across-game parallelism. |

**Realism sub-track (interleaved, landed on `master`):** the SIM-422→429 full-pool similarity-wiring
epic replaced the per-tile k-NN draw with whole-pool engine-weighted sampling and made it the
production default — see §2/§11. This is independent of the frontend critical path below.

**Phase 6 critical path:** SIM-378 (React-vs-vanilla ADR) → 379/380/381 (scaffold + design system +
API→UI serving) + 382/383/384/385/387/389 (backfill deps; enriched games list+records; aggregate card +
status enum; typed WebSocket schema; calibration-wiring fix; auth enforcement) → 386 (live read path) →
391/392 (Day Summary + 3-state cards + linescore/field graphics) → 393/394 (game page + boxscore) →
395/396/397/398 (betting card + CLV chart + override v1 then v2). The data/ML/perf prerequisite track
(402–409) runs alongside and must be live-env verified before its numbers reach users.

## 13. Key references (read before working)

- `docs/HANDOFF_PHASE6.md` — Phase 6 onboarding (what Phase 5 leaves you, scope, risks, how to start).
- `docs/audit/2026-09-02-phase6-prioritized-tickets.md` — the full tiered 43-ticket list + sprint plan.
- `docs/audit/2026-09-02-phase5-close-program-audit.md` — the audit narrative + findings.
- `BACKLOG.md` — the authoritative ticket status, single source of truth (verify before acting on any
  ticket). `backlog.xlsx` was RETIRED 2026-06-04 (drifted, hand-maintained, no generator script).
- `CHANGES.md` — the running changelog (**newest entries prepended at the top**; per-agent detail).
- `agent_team.md` — full agent scopes + the cross-agent collaboration map.
- `WORKFLOW.md` — the operator's manual (clean-checkout bring-up, health checks).

## 14. Writing standard (applies to ALL prose: chat replies, docs, commit messages, code comments)

Write in **Simplified Technical English (ASD-STE100 style)** and follow **Zinsser's four principles**.
This is a hard requirement, not a preference. It applies to every summary, explanation, audit document,
ticket description, docstring, and inline comment.

**Simplified Technical English — the rules to keep:**

- **One idea per sentence.** Keep sentences to 20 words or fewer. Split a long sentence into two.
- **Use the active voice.** Write "the manager pulls the pitcher", not "the pitcher is pulled".
- **Use the present tense.** Write "the gate blocks the steal", not "the gate would block the steal".
- **Use one term for one thing, every time.** Do not switch between "green-light", "green", and
  "aggression rate" for the same value. Pick one name and keep it.
- **Define a technical term the first time you use it.** Then reuse the same term.
- **Say who does what.** Name the actor in every sentence. Avoid "it" and "this" with no clear subject.
- **Use simple words.** Write "use", not "utilize". Write "start", not "initiate".
- **Write one instruction per step.** Do not join two actions with "and" in a procedure step.
- **Do not use noun stacks.** Write "the rate of the steal attempt", not "steal attempt rate scaling".
- **Keep the article.** Write "the pool", not "pool".

**Zinsser's four principles — the test to apply before you send:**

1. **Clarity** — the reader must not have to read a sentence twice. If a sentence is unclear, the thought
   behind it is unclear. Fix the thought first.
2. **Simplicity** — cut every word that does no work. Delete "very", "quite", "in order to", "the fact
   that", "it should be noted that".
3. **Brevity** — say it once. Do not restate a point in a summary that the body already made.
4. **Humanity** — write to a person, not to a file. Say "you", say "I", and admit uncertainty plainly.

**Do not do these things:**

- Do not open with a windup ("It's worth noting that…"). Start with the point.
- Do not hedge to sound careful. Say "I did not verify this" instead of "this may potentially differ".
- Do not use jargon as a shortcut in a summary for the owner. Explain the term in plain words.
- Do not pad a list to look complete. A short list of real items beats a long list with filler.

## 15. Working conventions for Claude Code

- Confirm a ticket's status in `BACKLOG.md` before acting — it changes (it is the single source of truth;
  `backlog.xlsx` was retired 2026-06-04).
- Keep the agent-team rhythm: implement → independent QA cross-validation → run the full suite → document
  (CHANGES/BACKLOG/SPRINT).
- Run `make test-unit` + `make lint` + `make type-check` before committing; run `make test-regression`
  after any engine/model change. Target Python 3.13 to match CI (SIM-431; numpy 2.x).
- Don't commit credentials; honor the migration + regression conventions above.
- Prefer surgical edits to `simulation/sim_loop.py` (it's the largest, most-touched file).
