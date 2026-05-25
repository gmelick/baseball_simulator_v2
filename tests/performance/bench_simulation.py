"""
bench_simulation.py
===================
SIM-118 — Performance benchmark harness.

This is the canonical performance baseline that all downstream work measures
against. It uses ``pytest-benchmark`` to time the project's hot paths and
records the timings so CI can track regressions over time.

Hot paths benchmarked
----------------------
  Bench 1  pitcher engine ``query()``           target  p50 <= 5ms,  p99 <= 20ms
  Bench 2  arsenal cache lookup (all-vs-one)     target  <= 0.1ms
  Bench 3  GMM fit per-season (single fit)       target  <= 10min (CI-scale)
  Bench 4  Phase 4 sim-loop per-pitch step       target  <= 1.23ms/pitch (SIM-119)
  Bench 5  Phase 4 batch-runner throughput       target  <= 0.37s/game (SIM-119)

Threshold philosophy — soft locally, hard in CI
------------------------------------------------
The latency targets (p50/p99, 0.1ms) are *hardware dependent*. Asserting them
unconditionally would make this suite flaky on shared/loaded runners and on
developer laptops. So:

  * By default the benches RUN the hot path and RECORD the timing via
    ``benchmark(...)`` but only emit a soft, non-fatal note when a target is
    exceeded. The suite is therefore always green here.
  * When ``PERF_STRICT=1`` (set by the weekly CI workflow on dedicated
    hardware) the strict thresholds are asserted and a regression fails CI.

All thresholds live as module-level constants below with rationale comments.

Run locally:
    pytest tests/performance --benchmark-only
Run as CI gate:
    PERF_STRICT=1 pytest tests/performance --benchmark-only
"""

from __future__ import annotations

import os
import warnings

import numpy as np
import pytest

from similarity.engines.pitcher_similarity import (
    GMM_FEATURE_DIM,
    GMM_FEATURE_NAMES,
    ArsenalCache,
    EmpiricalBayesShrinkage,
    FeatureNormalizer,
    GMMComponent,
    GMMModel,
    HandednessPartition,
    PitcherProfile,
    PitcherSimilarityEngine,
    RBFSimilarity,
)

# ---------------------------------------------------------------------------
# Performance thresholds (documented targets — SIM-118 acceptance criteria)
#
# These encode the latency/throughput budgets the simulator must hold.  They
# are asserted as HARD failures only under PERF_STRICT=1 (CI on dedicated
# hardware); otherwise they are recorded + soft-warned so the harness is
# always green in shared sandboxes / laptops.
# ---------------------------------------------------------------------------

# Bench 1 — pitcher engine query() latency.
#   The simulation issues one query() per pitcher per iteration.  The
#   vectorized RBF + cached W2 path must stay interactive.
QUERY_P50_TARGET_MS = 5.0  # median query() latency budget
QUERY_P99_TARGET_MS = 20.0  # tail (99th pct) query() latency budget

# Bench 2 — arsenal cache lookup (warm cache, all-vs-one partition scan).
#   Once the W2 cache is warm, scoring a query against the full handedness
#   partition is a pure dict lookup + vectorized arithmetic path and must be
#   effectively free relative to the W2 compute cost.
ARSENAL_CACHE_LOOKUP_TARGET_MS = 0.1  # warm all-vs-one lookup budget

# Bench 3 — GMM fit per pitcher-season.
#   The CI-scale budget for fitting a FULL season of GMMs is 10 minutes.
#   We do NOT run a full season here; we benchmark a single small synthetic
#   fit and assert it is far under budget.  The 10-min figure is the
#   season-scale target the offline fitter is held to in CI.
GMM_FIT_BUDGET_SECONDS = 10 * 60.0  # 10 min — CI-scale full-season target
GMM_SINGLE_FIT_TARGET_MS = 5_000.0  # one small synthetic fit budget (5s, very loose)

# Bench 4 — Phase 4 sim-loop per-pitch step (SIM-119 §3 per-pitch budget).
#   The SIM-119 budget allocates ~1.233 ms across the 8 per-pitch loop steps
#   (docs/perf/2026-06-03-sim-loop-time-budget.md §3).  ~81% of that is the
#   pitcher query() (Bench 1), which is DB/FAISS-backed and NOT exercised here:
#   this bench times the *count machine + outcome determination + fielding/
#   baserunning + state-update/loop-control* steps (1, 4, 5, 6, 7, 8) of
#   StateMachine.step_pitch with the sampler call replaced by an injected
#   resolver (the SIM-319/320 no-DB injection seam).  Those steps' combined
#   SIM-119 allocation is ~233 µs/pitch (everything except step 2/query and
#   step 3/sampler).  We assert against the FULL ~1.23 ms per-pitch envelope so
#   the bench is a true budget gate: the in-loop work it measures must stay well
#   inside the whole-pitch budget (it has the query()+sampler ~1.0 ms of slack).
SIM_LOOP_PER_PITCH_BUDGET_MS = 1.233  # SIM-119 §3 per-pitch roll-up (≈1.23 ms)

# Bench 5 — Phase 4 batch-runner per-game throughput (SIM-119 §4.2).
#   The single-game SLA is 2 s; the SIM-119 *budgeted* per-game roll-up is
#   ~0.37 s (1.233 ms × 300 pitches).  The no-DB rng-driven batch game here runs
#   far more loop iterations than a real ~300-pitch game (no real outs source to
#   end innings cleanly), so we assert PER-PITCH throughput against the same
#   SIM-119 per-pitch budget rather than per-game wall time — an honest,
#   loop-bounded measure that does not over-claim a 300-pitch game it does not
#   simulate.  The per-game budget constant is kept for documentation / the soft
#   note.
SIM_GAME_BUDGET_SECONDS = 0.37  # SIM-119 §4.2 budgeted per-game roll-up

# When set, latency/throughput targets are asserted as hard failures.
PERF_STRICT = os.environ.get("PERF_STRICT", "") == "1"

# The latency *targets* are calibrated for dedicated CI hardware (the perf-weekly
# workflow).  A shared/loaded sandbox or laptop is too noisy to assert sub-ms
# budgets as a HARD gate without flaking, so under PERF_STRICT we additionally
# require an explicit opt-in (PERF_STRICT_SANDBOX=1) before the *microsecond*
# loop budgets (Bench 4/5) become hard failures.  On CI hardware set BOTH
# (perf-weekly does); in the sandbox a PERF_STRICT=1 smoke run still RUNS the
# benches and records the timing, but the sub-ms assertion stays soft so the
# slow box does not produce a false regression.  Bench 1-3 are unaffected (their
# budgets are ms/seconds-scale and hold on shared hardware).
PERF_STRICT_SANDBOX = os.environ.get("PERF_STRICT_SANDBOX", "") == "1"
PERF_STRICT_LOOP = PERF_STRICT and PERF_STRICT_SANDBOX

# Size of the synthetic handedness partition the query bench scores against.
# ~2,400 RHP profiles is the production-scale population (see module docstring
# of pitcher_similarity.py).  We use a smaller-but-representative population
# here so the sandbox run is quick while still exercising the vectorized path.
SYNTHETIC_PARTITION_SIZE = 250


# ---------------------------------------------------------------------------
# Soft / strict threshold helper
# ---------------------------------------------------------------------------


def _check_threshold(name: str, measured: float, target: float, unit: str) -> None:
    """Assert a perf threshold under PERF_STRICT, else record a soft warning.

    This keeps the harness always-green on shared hardware while still letting
    CI (PERF_STRICT=1) enforce the budget as a hard regression gate.
    """
    ok = measured <= target
    msg = f"{name}: measured {measured:.4f}{unit} vs target {target:.4f}{unit}"
    if ok:
        return
    if PERF_STRICT:
        raise AssertionError(f"PERF REGRESSION — {msg}")
    warnings.warn(f"PERF (soft) — {msg}", stacklevel=2)


def _check_loop_threshold(name: str, measured: float, target: float, unit: str) -> None:
    """Assert a SIM-119 loop micro-budget — HARD only under PERF_STRICT_LOOP.

    Identical to :func:`_check_threshold` but gated on :data:`PERF_STRICT_LOOP`
    (PERF_STRICT *and* PERF_STRICT_SANDBOX) rather than bare ``PERF_STRICT``.
    The sub-millisecond per-pitch / per-game loop budgets (Bench 4/5) are too
    fine-grained to assert as a hard gate on a shared/loaded sandbox without
    flaking; CI hardware (perf-weekly) sets both flags so the budget is enforced
    there, while a sandbox smoke run with only PERF_STRICT=1 still records the
    timing and soft-warns on a breach.
    """
    ok = measured <= target
    msg = f"{name}: measured {measured:.4f}{unit} vs target {target:.4f}{unit}"
    if ok:
        return
    if PERF_STRICT_LOOP:
        raise AssertionError(f"PERF REGRESSION — {msg}")
    warnings.warn(f"PERF (soft) — {msg}", stacklevel=2)


# ---------------------------------------------------------------------------
# Synthetic input builders (no DuckDB — mirror tests/unit fixtures)
#
# These reuse the project's in-memory engine-construction pattern: build the
# engine object with __new__ to bypass the DuckDB-dependent constructor, then
# feed synthetic numpy GMM profiles.  See tests/unit/test_pitcher_similarity.py
# (_build_synthetic_engine) for the canonical reference.
# ---------------------------------------------------------------------------


def _make_component(
    cid: int,
    weight: float,
    mean: list[float],
    var_diag: float = 1.5,
    n_pitches: int = 250,
) -> GMMComponent:
    """One GMM component with a diagonal covariance matrix."""
    return GMMComponent(
        component_id=cid,
        weight=weight,
        mean=np.array(mean, dtype=np.float64),
        covariance=np.eye(GMM_FEATURE_DIM, dtype=np.float64) * var_diag,
        n_pitches=n_pitches,
    )


def _make_gmm(components: list[GMMComponent]) -> GMMModel:
    return GMMModel(
        n_components=len(components),
        feature_names=GMM_FEATURE_NAMES,
        feature_means=np.zeros(GMM_FEATURE_DIM),
        feature_stds=np.ones(GMM_FEATURE_DIM),
        components=components,
    )


def _random_gmm(rng: np.random.Generator) -> GMMModel:
    """A 3-component arsenal with randomly perturbed centroids around a base."""
    base = np.array([93.0, 12.0, -6.0, 2300.0, 200.0, -1.5, 6.0, 6.2])
    comps = []
    weights = rng.dirichlet(np.ones(3))
    for i in range(3):
        jitter = rng.standard_normal(GMM_FEATURE_DIM) * np.array(
            [2.0, 3.0, 4.0, 150.0, 25.0, 0.2, 0.2, 0.3]
        )
        comps.append(_make_component(i, float(weights[i]), (base + jitter).tolist(), var_diag=1.5))
    return _make_gmm(comps)


def _make_profile(
    rng: np.random.Generator,
    pitcher_id: int,
    season: int,
    p_throws: str,
) -> PitcherProfile:
    """A synthetic RHP/LHP profile with a random arsenal + command vector."""
    # Command vector matches len(COMMAND_FEATURES) used by the real engine.
    command_vec = np.array(
        [
            0.08 + rng.standard_normal() * 0.02,  # bb_rate
            0.22 + rng.standard_normal() * 0.04,  # k_rate
            0.30 + rng.standard_normal() * 0.03,  # csw_rate
            0.45 + rng.standard_normal() * 0.04,  # zone_take_rate
            0.28 + rng.standard_normal() * 0.03,  # chase_rate
            0.48 + rng.standard_normal() * 0.03,  # zone_rate
            0.25 + rng.standard_normal() * 0.03,  # whiff_rate
        ],
        dtype=np.float64,
    )
    return PitcherProfile(
        pitcher_id=pitcher_id,
        season=season,
        p_throws=p_throws,
        sample_pitches=500,
        gmm=_random_gmm(rng),
        command_vec=command_vec,
        eb_alpha=1.0,
    )


def _build_synthetic_engine(
    n_rhp: int = SYNTHETIC_PARTITION_SIZE,
    n_lhp: int = 20,
    seed: int = 118,
) -> PitcherSimilarityEngine:
    """Construct a built engine entirely in memory (no DuckDB).

    Uses __new__ to bypass the DB-dependent __init__, then replicates the
    in-memory portion of build(): standardize arsenals, fit the normalizer,
    and build the handedness partitions.
    """
    rng = np.random.default_rng(seed)

    engine = PitcherSimilarityEngine.__new__(PitcherSimilarityEngine)
    engine._duckdb_path = ""
    engine._profiles = {}
    engine._league_avg_command = {}
    engine._normalizer = FeatureNormalizer()
    engine._shrinkage = EmpiricalBayesShrinkage()
    engine._command_rbf = RBFSimilarity(sigma=0.8)
    engine._partition_l = HandednessPartition("L")
    engine._partition_r = HandednessPartition("R")
    engine._arsenal_cache = ArsenalCache()

    pid = 1000
    for _ in range(n_rhp):
        p = _make_profile(rng, pid, 2025, "R")
        engine._profiles[(p.pitcher_id, p.season)] = p
        pid += 1
    for _ in range(n_lhp):
        p = _make_profile(rng, pid, 2025, "L")
        engine._profiles[(p.pitcher_id, p.season)] = p
        pid += 1

    # Replicate the in-memory part of build():
    engine._standardize_arsenals()
    all_profiles = list(engine._profiles.values())
    engine._normalizer.fit(all_profiles)
    profiles_l = [p for p in all_profiles if p.p_throws == "L"]
    profiles_r = [p for p in all_profiles if p.p_throws == "R"]
    engine._partition_l.build(profiles_l, engine._normalizer)
    engine._partition_r.build(profiles_r, engine._normalizer)

    return engine


# ---------------------------------------------------------------------------
# Bench 1 — pitcher engine query()
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
def test_bench_pitcher_query(benchmark):
    """Bench 1: time PitcherSimilarityEngine.query() (warm arsenal cache).

    Measures the per-iteration simulation hot path: scoring a query pitcher
    against the entire same-handedness partition.  We warm the W2 cache once
    so the benchmarked path reflects the steady-state (iterations 2..N) cost,
    which is what dominates a 100-iteration simulation.

    Targets: p50 <= QUERY_P50_TARGET_MS, p99 <= QUERY_P99_TARGET_MS
    (asserted hard only under PERF_STRICT=1).
    """
    engine = _build_synthetic_engine()
    query_id, query_season = 1000, 2025

    # Warm the arsenal W2 cache (first query pays the lazy-compute cost).
    engine.query(query_id, query_season)

    result = benchmark(lambda: engine.query(query_id, query_season))
    assert len(result) > 0  # sanity: partition produced results

    stats = benchmark.stats.stats
    p50_ms = stats.median * 1_000.0
    # pytest-benchmark exposes percentiles via the Metadata helper; fall back
    # to max if the runtime doesn't compute a 99th percentile.
    p99_ms = (getattr(stats, "percentile", lambda _q: stats.max)(0.99)) * 1_000.0

    _check_threshold("query() p50", p50_ms, QUERY_P50_TARGET_MS, "ms")
    _check_threshold("query() p99", p99_ms, QUERY_P99_TARGET_MS, "ms")


# ---------------------------------------------------------------------------
# Bench 2 — arsenal cache lookup (all-vs-one, warm cache)
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
def test_bench_arsenal_cache_lookup(benchmark):
    """Bench 2: time an all-vs-one warm arsenal-cache scan.

    Once the W2 cache is fully warm, scoring a query against the partition is
    just N dict lookups (no W2 compute).  This isolates that pure-lookup cost,
    which must be negligible (<= ARSENAL_CACHE_LOOKUP_TARGET_MS) so that the
    steady-state simulation iteration is bounded by the vectorized RBF math,
    not cache overhead.

    Target: <= ARSENAL_CACHE_LOOKUP_TARGET_MS (hard only under PERF_STRICT=1).
    """
    engine = _build_synthetic_engine()
    partition = engine._partition_r
    cache = engine._arsenal_cache

    query_key = partition.keys[0]
    query_gmm = partition.profiles[0].gmm

    # Warm every (query, candidate) pair so the benched loop is all hits.
    for i in range(len(partition.profiles)):
        cand_key = partition.keys[i]
        if cand_key == query_key:
            continue
        cache.get_or_compute(query_key, cand_key, query_gmm, partition.profiles[i].gmm)

    def all_vs_one_lookup():
        total = 0.0
        for i in range(len(partition.profiles)):
            cand_key = partition.keys[i]
            if cand_key == query_key:
                continue
            d = cache.get(query_key, cand_key)
            if d is not None and np.isfinite(d):
                total += d
        return total

    benchmark(all_vs_one_lookup)

    stats = benchmark.stats.stats
    per_lookup_ms = (stats.median / max(len(partition.profiles) - 1, 1)) * 1_000.0
    _check_threshold(
        "arsenal cache per-lookup", per_lookup_ms, ARSENAL_CACHE_LOOKUP_TARGET_MS, "ms"
    )


# ---------------------------------------------------------------------------
# Bench 3 — GMM fit per pitcher-season
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
def test_bench_gmm_fit_single_season(benchmark):
    """Bench 3: time a single small synthetic GMM fit.

    The CI-scale budget for fitting a FULL season of pitcher GMMs is
    GMM_FIT_BUDGET_SECONDS (10 min).  We do NOT run a full season here — we
    benchmark one representative small fit (a few hundred synthetic points,
    8 features, 3 components, the production arsenal shape) and assert it is
    far under budget.  A full season is thousands of such fits, parallelized
    offline; the 10-min figure is the season-scale CI target.

    Target: single fit <= GMM_SINGLE_FIT_TARGET_MS (hard only under PERF_STRICT=1).
    """
    from sklearn.mixture import GaussianMixture

    rng = np.random.default_rng(2025)
    # ~600 synthetic pitches across 3 latent clusters in 8-D feature space.
    centers = np.array(
        [
            [96.0, 16.0, -8.0, 2400.0, 210.0, -1.5, 6.2, 6.5],
            [86.0, -2.0, 6.0, 2500.0, 140.0, -1.3, 6.0, 6.2],
            [88.0, 8.0, -14.0, 1800.0, 220.0, -1.6, 6.1, 5.9],
        ]
    )
    sizes = [300, 200, 100]
    X = np.vstack(
        [
            c + rng.standard_normal((n, GMM_FEATURE_DIM)) * 1.5
            for c, n in zip(centers, sizes, strict=True)
        ]
    )

    def fit_once():
        gm = GaussianMixture(
            n_components=3,
            covariance_type="full",
            random_state=0,
            n_init=1,
            max_iter=100,
        )
        gm.fit(X)
        return gm

    gm = benchmark(fit_once)
    assert gm.converged_ or gm.n_iter_ > 0

    fit_ms = benchmark.stats.stats.median * 1_000.0
    # Sanity: a single small fit is microscopic vs the 10-min season budget.
    assert fit_ms < GMM_FIT_BUDGET_SECONDS * 1_000.0
    _check_threshold("single GMM fit", fit_ms, GMM_SINGLE_FIT_TARGET_MS, "ms")


# ---------------------------------------------------------------------------
# Bench 4 — Phase 4 simulation-loop per-pitch step (SIM-335 / SIM-119)
#
# Times ONE StateMachine.step_pitch — the §2 step-1→step-8 per-pitch loop body
# — with NO live DuckDB/FAISS.  The DB/FAISS-backed steps (step 2 pitcher
# query() / step 3 sampler) are replaced by the SIM-319/320 injection seam: the
# machine is driven in count-machine-only mode (an explicit pitch_outcome is
# supplied per pitch) and contact is resolved through an injected PlayResolver
# that returns a deterministic FieldingSignal.  This exercises the count machine
# (§5.1, incl. the SIM-056 two-strike-foul absorbing rule), outcome
# determination (step 4), fielding/baserunning resolution (steps 6/7 via
# resolve_runs), and state-update / half-inning loop control (step 8) — exactly
# the in-loop work SIM-119 §3 allocates the non-query()/non-sampler budget to.
# ---------------------------------------------------------------------------


class _OutFieldingResolver:
    """A no-DB PlayResolver that resolves every in-play ball to a 1-out field-out.

    Mirrors the SIM-319/320/332 no-DB injection seam (see
    ``simulation.batch_runner._CyclingResolver``): the loop's step-6/7 fielding /
    baserunning resolution consumes this bounded signal so the per-pitch step
    runs without a sampler, FAISS index, or DuckDB — and records outs so the
    half-inning machine (step 8) actually rolls during the bench.
    """

    def resolve_fielding(self, state, battedball_sample):
        from simulation.sim_loop import FieldingSignal

        return FieldingSignal(event="field_out", result_hits=0, result_outs=1, result_runs=0)

    def resolve_steal(self, state):
        from simulation.sim_loop import StealResolution

        return StealResolution(attempted=False)


# A representative single-PA pitch-outcome cycle (no DB).  The mix walks a PA to
# a variety of terminals — ball/called/swinging strikes, a non-terminal foul,
# and a contact (in_play) that the injected resolver turns into an out — so the
# benched step sees every branch of the count machine + the contact path, not a
# single trivial outcome.  Cycled deterministically across the bench rounds.
_BENCH4_OUTCOME_CYCLE = (
    "ball",
    "called_strike",
    "foul",
    "swinging_strike",
    "swinging_strike",  # strike 3 -> strikeout terminal (rolls the PA)
    "ball",
    "in_play",  # contact -> resolver records a field-out (rolls outs)
)


@pytest.mark.benchmark(max_time=1.0)
def test_bench_phase4_simulation_loop(benchmark):
    """Bench 4: time ONE StateMachine.step_pitch (the SIM-119 per-pitch loop body).

    Drives the SIM-316 state machine through a representative outcome cycle with
    NO live DB/FAISS (count-machine-only mode + an injected out-resolver for the
    contact path), so the benched call is the real per-pitch step (count machine,
    outcome determination, fielding/baserunning via resolve_runs, state update +
    half-inning loop control).  A fresh GameState is recreated whenever the
    machine drives the game to completion so the benched step always operates on
    a live (in-play) state.

    Target: per-pitch step <= SIM_LOOP_PER_PITCH_BUDGET_MS (SIM-119 §3, ~1.23 ms;
    the budget includes the DB-backed query()+sampler ~1.0 ms NOT timed here, so
    this in-loop work has ample slack).  Asserted HARD only under PERF_STRICT_LOOP
    (PERF_STRICT=1 AND PERF_STRICT_SANDBOX=1 — CI hardware); soft elsewhere.
    """
    from simulation.game_state import GameState
    from simulation.sim_loop import StateMachine

    rng = np.random.default_rng(335)

    def _fresh_state() -> GameState:
        return GameState(pitcher_id=1, bat_hand="R", season=2024)

    machine = StateMachine(rng=rng, resolver=_OutFieldingResolver())
    # Mutable cursor over the outcome cycle + the live game state.  The benched
    # closure recreates the state if a prior step drove the game to a finished
    # inning past regulation (a defensive guard; with field-outs the half-inning
    # rolls and continues — assert_invariants stays valid throughout).
    cursor = {"i": 0, "state": _fresh_state()}

    def one_pitch_step():
        st = cursor["state"]
        outcome = _BENCH4_OUTCOME_CYCLE[cursor["i"] % len(_BENCH4_OUTCOME_CYCLE)]
        cursor["i"] += 1
        machine.step_pitch(st, pitch_outcome=outcome)
        # Keep the bench bounded to a live, in-play game: if the loop has driven
        # the state deep into extras, reset to a fresh PA (cheap, off the timed
        # hot path's steady state).
        if st.inning > 18:
            cursor["state"] = _fresh_state()

    benchmark(one_pitch_step)

    # Sanity: the machine actually advanced the game during the bench.
    assert cursor["i"] > 0

    per_pitch_ms = benchmark.stats.stats.median * 1_000.0
    _check_loop_threshold(
        "sim-loop per-pitch step", per_pitch_ms, SIM_LOOP_PER_PITCH_BUDGET_MS, "ms"
    )


# ---------------------------------------------------------------------------
# Bench 5 — Phase 4 batch-runner throughput (SIM-335 / SIM-332 / SIM-119 §4)
#
# Times the SIM-332 BatchRunner fan-out/fan-in for a small N-game Monte-Carlo
# batch on the in-process (max_workers=1) path, with NO live DB/FAISS: the
# picklable rng-driven no-DB factory (simulation.batch_runner.
# rng_driven_machine_factory) builds a sampler-less StateMachine per game that
# draws each pitch outcome from its own loop rng and resolves contact via a
# deterministic resolver.  This is the real batch hot path (derive seeds ->
# simulate_game per iteration -> GameSimSummary.from_results), measured as a
# per-pitch throughput so the figure is comparable to the SIM-119 per-pitch
# budget without over-claiming a 300-pitch game the no-DB driver does not model.
# ---------------------------------------------------------------------------


@pytest.mark.benchmark(min_rounds=3, max_time=2.0, warmup=False)
def test_bench_phase4_batch_runner(benchmark):
    """Bench 5: time a small no-DB Monte-Carlo batch via the SIM-332 BatchRunner.

    Runs ``_BATCH_N`` games in-process (max_workers=1 — fast, deterministic, no
    fork/pickle round-trip) through the rng-driven no-DB factory, then aggregates
    to a GameSimSummary.  Measures the whole batch hot path (seed derivation ->
    simulate_game per game -> summary aggregation).  Reported as per-pitch
    throughput (total wall / total pitches across the batch) and asserted against
    the SIM-119 per-pitch budget.

    Target: per-pitch throughput <= SIM_LOOP_PER_PITCH_BUDGET_MS (SIM-119 §3).
    HARD only under PERF_STRICT_LOOP (CI hardware); soft otherwise.
    """
    from simulation.batch_runner import BatchRunner, GameSpec, _run_one, derive_seed

    _BATCH_N = 3  # small batch — enough to exercise fan-in without a slow bench
    _BASE_SEED = 335

    spec = GameSpec(
        machine_factory="simulation.batch_runner:rng_driven_machine_factory",
        # Cap the per-game inning ceiling so the no-DB rng game (which has no real
        # outs source to end innings cleanly) is bounded to a regulation-length
        # pitch budget rather than the default 50-inning safety ceiling — keeps the
        # bench fast/deterministic while still exercising the full batch hot path.
        sim_kwargs={"max_innings": 9},
    )

    # Pre-count the pitches one batch run does so per-pitch throughput is exact.
    # GameSimSummary does not retain the per-game results, so count via the same
    # deterministic _run_one path the runner uses (rng-driven game length is a
    # pure function of the per-game seed).
    total_pitches = sum(
        int(getattr(_run_one(spec, derive_seed(_BASE_SEED, i)), "total_pitches", 0))
        for i in range(_BATCH_N)
    )

    def run_batch():
        # A fresh runner per call avoids the SIM_RESULT_TTL cache short-circuit
        # (use_cache=False also disables it) so every round does real work.
        r = BatchRunner(max_workers=1)
        try:
            return r.run(spec, n_iterations=_BATCH_N, base_seed=_BASE_SEED, use_cache=False)
        finally:
            r.close()

    result = benchmark(run_batch)
    assert result.n_iterations == _BATCH_N
    assert result.from_cache is False

    batch_median_s = benchmark.stats.stats.median
    per_game_s = batch_median_s / _BATCH_N
    # Soft per-game note against the SIM-119 §4.2 budgeted ~0.37 s/game.
    _check_loop_threshold("batch per-game wall", per_game_s, SIM_GAME_BUDGET_SECONDS, "s")
    # Hard(-able) per-pitch throughput against the SIM-119 §3 per-pitch budget.
    if total_pitches > 0:
        per_pitch_ms = (batch_median_s / total_pitches) * 1_000.0
        _check_loop_threshold(
            "batch per-pitch throughput",
            per_pitch_ms,
            SIM_LOOP_PER_PITCH_BUDGET_MS,
            "ms",
        )
