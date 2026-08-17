"""
production_factory.py
=====================
SIM-352 -- the **production, DB-backed machine factory** for the SIM-332 batch
runner (Phase 5, Sprint 1).

WHAT THIS IS
------------
:mod:`simulation.batch_runner` fans :func:`simulation.sim_loop.simulate_game` out
across a ``ProcessPoolExecutor``; each worker rebuilds its own live
:class:`~simulation.sim_loop.StateMachine` IN-PROCESS from a small, picklable
:class:`~simulation.batch_runner.GameSpec` via a *dotted-ref* ``machine_factory``
(never a live sampler across the fork -- SIM-281 D1).  Until now the ONLY factory
was :func:`simulation.batch_runner.rng_driven_machine_factory`: a no-DB,
rng-driven machine the always-on tests use.  That factory cannot run a *real*
game (no PlayPoolSampler / FAISS tiles), so ``/simulate`` could not run a real
matchup and the SIM-119/280 2 s-game / 30 s-batch SLA stayed unverified.

THIS module fills that gap: :func:`production_machine_factory` mirrors the
``factory(seed, spec) -> StateMachine`` contract but builds a REAL machine driven
by a :class:`~simulation.play_pool_sampler.PlayPoolSampler` over the player
profiles (DuckDB) + the FAISS play-pool tiles, and -- when
``spec.shared_segments`` is set -- ATTACHES the SIM-333 shared-memory tiles
zero-copy (:func:`simulation.batch_runner.get_shared_view` ->
:meth:`PlayPoolSampler.attach_shared_tile`) instead of re-reading them from disk.
Reference it on a :class:`GameSpec` by dotted path::

    GameSpec(
        machine_factory="simulation.production_factory:production_machine_factory",
        sim_kwargs={"pitcher_id": 477132, "bat_hand": "R", "season": 2024,
                    "away_lineup": [...], "home_lineup": [...]},
    )

THE DEPENDENCY-INJECTION SEAM (why this is testable with no live DB)
-------------------------------------------------------------------
There is no live DuckDB / FAISS in the sandbox, so the SAMPLER construction is
behind a **module-level builder hook** :data:`_SAMPLER_BUILDER`
(default :func:`_default_sampler_builder`, which builds the real
:class:`PlayPoolSampler`).  A test installs an in-memory / mock sampler builder
via :func:`set_sampler_builder` (or the :func:`use_sampler_builder` context
manager) and asserts the factory wires that sampler into the StateMachine -- the
SAME injection seam SIM-319/320 use for the no-DB path, lifted to the factory.
The fingerprint deriver is behind a second hook :data:`_DERIVER_BUILDER`
(default :func:`_default_deriver_builder`, which returns ``None`` -> the
StateMachine's built-in stub-hash fingerprints), so the factory runs end-to-end
without the full SIM-317 engine stack while still allowing a production caller to
wire the real :class:`~simulation.fingerprints.FingerprintDeriver`.

FACTORY-ONLY ``sim_kwargs`` KNOBS (SIM-377 convention)
------------------------------------------------------
Per SIM-377, ``_``-prefixed ``sim_kwargs`` keys are factory-only and are filtered
out of the ``simulate_game(**...)`` splat by
:func:`simulation.batch_runner._run_one`.  This factory reads:

  * ``_pool_dir`` -- play-pool tile root (else ``BASEBALL_PLAY_POOL_DIR`` / the
    sampler default);
  * ``_duckdb_path`` -- DuckDB path for outcome / recency payloads (else
    ``BASEBALL_DUCKDB_PATH`` / the sampler default);
  * ``_k`` -- k-NN neighbour count (default 25, matching ``simulate_game``);
  * ``_max_resident_tiles`` -- the sampler LRU cap (default 256).

The non-underscore keys (``pitcher_id`` / ``bat_hand`` / ``season`` / the two
lineups / ``k`` / ``max_innings`` / ...) flow through to ``simulate_game`` and are
read here only to size the shared-tile attach.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import numpy as np

from simulation.batch_runner import GameSpec, get_shared_view
from simulation.play_pool_sampler import (
    POOL_BATTEDBALL,
    POOL_PITCH,
    PlayPoolSampler,
)
from simulation.sim_loop import StateMachine

# Default k-NN neighbour count (mirrors ``simulate_game``'s ``k`` default §6.2).
_DEFAULT_K = 25
# Default sampler LRU cap (mirrors ``PlayPoolSampler.max_resident_tiles``).
_DEFAULT_MAX_RESIDENT_TILES = 256


# ---------------------------------------------------------------------------
# Injectable builder hooks (the no-DB test seam) -- SIM-319/320 pattern
# ---------------------------------------------------------------------------
#
# A factory must pickle by dotted reference, so these are MODULE-LEVEL callables a
# test swaps out (rather than closures passed through the GameSpec, which would not
# survive the fork).  Production leaves them at their defaults: a real DuckDB-backed
# PlayPoolSampler + no fingerprint deriver (the StateMachine's stub-hash fall-back).

#: Signature of a sampler builder: ``(spec, seed) -> PlayPoolSampler``.
SamplerBuilder = Callable[[GameSpec, "int | None"], PlayPoolSampler]
#: Signature of a deriver builder: ``(spec) -> FingerprintDeriver | None``.
DeriverBuilder = Callable[[GameSpec], Any]


def _kwarg(spec: GameSpec, name: str, default: Any) -> Any:
    """Read a ``sim_kwargs`` value (factory-only ``_``-prefixed keys included)."""
    kw = spec.sim_kwargs if isinstance(spec.sim_kwargs, dict) else {}
    val = kw.get(name, default)
    return default if val is None else val


def _default_sampler_builder(spec: GameSpec, seed: int | None) -> PlayPoolSampler:
    """Build the REAL production :class:`PlayPoolSampler` for this matchup.

    Reads the factory-only ``_pool_dir`` / ``_duckdb_path`` / ``_max_resident_tiles``
    knobs from ``spec.sim_kwargs`` (each falling back to the sampler's own
    env-var / default resolution when absent) and seeds the sampler's k-NN
    Generator from the per-game ``seed`` so the FAISS draws are reproducible
    (§6.3).  Outcome + recency payloads come from the sampler's lazily-opened
    read-only DuckDB connection (the production path); a worker that attaches
    SIM-333 shared tiles never touches disk for the tile bytes themselves.
    """
    pool_dir = _kwarg(spec, "_pool_dir", None)
    duckdb_path = _kwarg(spec, "_duckdb_path", None)
    max_resident = int(_kwarg(spec, "_max_resident_tiles", _DEFAULT_MAX_RESIDENT_TILES))
    return PlayPoolSampler(
        pool_dir=pool_dir,
        duckdb_path=duckdb_path,
        max_resident_tiles=max_resident,
        rng=np.random.default_rng(seed),
    )


def _default_deriver_builder(spec: GameSpec) -> Any:
    """Build the SIM-421 :class:`~simulation.fingerprints.FingerprintDeriver` from
    the on-disk play-pool artifacts, or ``None`` when they are absent.

    The deriver normalizes the loop's query into the SAME space the tiles were
    indexed in (z-score + sqrt-weight, using the mean/std ``play_pool_cache``
    persisted) and assembles it from per-matchup RAW centroids via a
    :class:`~simulation.matchup_provider.PrecomputedMatchupProvider`.  Without it
    the query is a scale-mismatched hash stub and the k-NN collapses to the
    lowest-norm tile vectors (the "no balls in play" defect).

    Built per-worker from disk so NO live object crosses the ProcessPool boundary
    -- fork- AND spawn-safe.  Returns ``None`` (-> the StateMachine's stub
    fingerprints) when the artifacts are missing (tiles built before SIM-421, or
    an artifact-less test env), so the factory still runs without them.
    """
    pool_dir = _kwarg(spec, "_pool_dir", None) or os.environ.get(
        "BASEBALL_PLAY_POOL_DIR", "/data/play_pool"
    )
    try:
        from simulation.fingerprints import FingerprintDeriver
        from simulation.matchup_provider import (
            load_battedball_norm,
            load_pitch_norm,
            load_provider,
        )

        provider = load_provider(pool_dir)
        pitch_norm = load_pitch_norm(pool_dir)
        if provider is None or pitch_norm is None:
            return None
        return FingerprintDeriver(
            provider,
            pitch_norm=pitch_norm,
            battedball_norm=load_battedball_norm(pool_dir),
        )
    except Exception:
        # The deriver is an enhancement; never let a build hiccup break the sim.
        return None


#: The active sampler builder (swap via :func:`set_sampler_builder`).
_SAMPLER_BUILDER: SamplerBuilder = _default_sampler_builder
#: The active deriver builder (swap via :func:`set_deriver_builder`).
_DERIVER_BUILDER: DeriverBuilder = _default_deriver_builder


def set_sampler_builder(builder: SamplerBuilder | None) -> SamplerBuilder:
    """Install ``builder`` as the active sampler builder; return the previous one.

    ``None`` restores the production default (:func:`_default_sampler_builder`).
    Tests use this (or :func:`use_sampler_builder`) to inject an in-memory / mock
    sampler so the factory is exercised with no live DuckDB / FAISS.
    """
    global _SAMPLER_BUILDER
    prev = _SAMPLER_BUILDER
    _SAMPLER_BUILDER = builder if builder is not None else _default_sampler_builder
    return prev


def set_deriver_builder(builder: DeriverBuilder | None) -> DeriverBuilder:
    """Install ``builder`` as the active fingerprint-deriver builder; return the
    previous one.  ``None`` restores the default (:func:`_default_deriver_builder`,
    i.e. no deriver -> stub fingerprints)."""
    global _DERIVER_BUILDER
    prev = _DERIVER_BUILDER
    _DERIVER_BUILDER = builder if builder is not None else _default_deriver_builder
    return prev


class use_sampler_builder:
    """Context manager: temporarily install a sampler builder, restoring on exit.

    Sugar over :func:`set_sampler_builder` so a test never leaks a mock builder
    into the module global::

        with use_sampler_builder(lambda spec, seed: mock_sampler):
            machine = production_machine_factory(7, spec)
    """

    def __init__(self, builder: SamplerBuilder | None) -> None:
        self._builder = builder
        self._prev: SamplerBuilder | None = None

    def __enter__(self) -> SamplerBuilder:
        self._prev = set_sampler_builder(self._builder)
        return _SAMPLER_BUILDER

    def __exit__(self, *_exc) -> None:
        set_sampler_builder(self._prev)


# ---------------------------------------------------------------------------
# SIM-333 shared-tile attach (zero-copy) over the published segments
# ---------------------------------------------------------------------------


def _attach_shared_tiles(sampler: PlayPoolSampler, spec: GameSpec) -> int:
    """Attach the SIM-333 shared-memory tiles named in ``spec.shared_segments``.

    ``spec.shared_segments`` is the opaque registry the GameSpec carries to tell a
    worker WHICH segments its factory should attach.  We expect, per logical tile,
    a ``vectors`` segment (the float32 ``n×dim`` FAISS-tile block) and a ``rowids``
    segment (the int64 vector -> ``pitch_id`` map), named by a small convention so a
    spec can describe one pitch tile and one batted-ball tile:

      * pitch tile:       ``"pitch_vectors"`` + ``"pitch_rowids"``
      * batted-ball tile: ``"battedball_vectors"`` + ``"battedball_rowids"``

    The actual buffers are NOT in the spec (they are OS-backed shared segments the
    pool initializer attached); we fetch the zero-copy views with
    :func:`simulation.batch_runner.get_shared_view` and hand them to
    :meth:`PlayPoolSampler.attach_shared_tile`, which caches a TileHandle over the
    shared buffer so a later ``sample_pitch`` / ``sample_batted_ball`` serves it
    WITHOUT any disk read.  Returns the number of tiles attached.  Missing views ->
    that tile is simply not attached (the sampler's disk path remains the
    fall-back), so this is backward-compatible with a spec that names a segment the
    worker did not publish.
    """
    if not spec.shared_segments:
        return 0

    season = int(_kwarg(spec, "season", 2024))
    bat_hand = str(_kwarg(spec, "bat_hand", "R"))
    pitcher_id = int(_kwarg(spec, "pitcher_id", 0))

    attached = 0
    plan = (
        (POOL_PITCH, "pitch_vectors", "pitch_rowids", pitcher_id),
        (POOL_BATTEDBALL, "battedball_vectors", "battedball_rowids", None),
    )
    for pool, vec_name, row_name, pid in plan:
        # Only attach a tile the spec actually named (honour the opaque registry).
        if vec_name not in spec.shared_segments and row_name not in spec.shared_segments:
            continue
        vectors = get_shared_view(vec_name)
        rowids = get_shared_view(row_name)
        if rowids is None:
            # Without rowids there is no vector -> pitch_id map; skip (disk fall-back).
            continue
        sampler.attach_shared_tile(
            pool,
            season,
            bat_hand,
            vectors=vectors,  # may be None -> rowids-only handle (sampler contract)
            rowids=rowids,
            pitcher_id=pid,
            meta={"season": season},
        )
        attached += 1
    return attached


# ---------------------------------------------------------------------------
# The production factory (the picklable, dotted-ref-able entry point)
# ---------------------------------------------------------------------------


#: SIM-402: per-worker cache for the EngineArtifacts + FullPoolSampler the
#: full-pool path needs.  WITHOUT this cache, :func:`_run_one` calls this
#: factory PER SEED and the worker re-disk-loads the ~290 MB artifact bundle
#: AND re-precomputes the FullPoolSampler's per-hand `_pool_cache` on every
#: iteration — turning a ~1.5 s/game pool draw into a ~9 s/game stall that
#: blows the SIM-372 2 s/30 s SLA.  WITH the cache, the first seed pays the
#: load+precompute and every subsequent seed reuses the warm sampler (only
#: the per-game rng is re-seeded; per-game state — half-inning / PA caches —
#: is reset at the loop's natural boundaries inside `simulate_game`).  Lives
#: per-process (not module-level across the fork), so each ProcessPool worker
#: gets its own copy at first use; the parent never holds this object.
_CACHED_FULL_POOL_SAMPLER = None
_CACHED_FULL_POOL_ART_DIR: str | None = None


def _build_full_pool_sampler(spec: GameSpec, seed: int | None):
    """SIM-424: build the full-pool sampler from the on-disk engine-artifact bundle
    when opted in (``SIM_FULL_POOL`` env or the ``_full_pool`` sim-kwarg).  Returns
    None otherwise (the validated per-tile path).  Built from disk -> fork-safe.

    SIM-429: ``SIM_FULL_POOL`` is parsed as a real boolean so ``=0``/``=false``
    disables it (production sets ``=1``; the unit-test conftest sets ``=0``).

    SIM-403b: when the parent has published the artifact arrays into shared memory
    via ``BatchRunner(shared_arrays=...)``, the worker's :func:`_worker_init` has
    already attached them and put zero-copy views in
    :data:`simulation.batch_runner._WORKER_SHARED` ``["views"]``. We disk-load the
    bundle (small picklable members like the pitcher-sim dict + the object-dtype
    outcome_type / event arrays only — the loader does the same I/O either way)
    and then SPLICE the shared views over the big numerical arrays. Net effect:
    per-worker resident-set drops by the size of the shared subset (~hundreds of
    MB at production scale).

    SIM-402: the loaded artifacts AND the FullPoolSampler itself are cached
    at module scope.  First call per worker pays the disk load + shared-view
    splice + per-hand `_pool_meta` precompute; every later call reuses the
    warm sampler (only the per-game rng is re-assigned).  This is the cache
    the SLA verification revealed was missing — without it, a 100-iteration
    /simulate request paid the ~6-9 s warm-up cost N times.
    """
    env = os.environ.get("SIM_FULL_POOL", "").strip().lower()
    env_on = env not in ("", "0", "false", "no", "off")
    if not (env_on or _kwarg(spec, "_full_pool", None)):
        return None
    pool_dir = _kwarg(spec, "_pool_dir", None) or os.environ.get(
        "BASEBALL_PLAY_POOL_DIR", "/data/play_pool"
    )
    art_dir = os.path.join(pool_dir, "engine_artifacts")
    try:
        global _CACHED_FULL_POOL_SAMPLER, _CACHED_FULL_POOL_ART_DIR
        # Reuse the cached sampler when the artifact dir matches.  Re-seed its
        # rng so the per-game seed still governs the FAISS / weighted draws
        # (the per-game half-inning / PA state caches inside the sampler are
        # reset at the loop's natural boundaries — `new_half_inning` /
        # `battedball_new_pa` — so cross-game leakage is impossible).
        if _CACHED_FULL_POOL_SAMPLER is not None and art_dir == _CACHED_FULL_POOL_ART_DIR:
            _CACHED_FULL_POOL_SAMPLER.rng = np.random.default_rng(seed)
            return _CACHED_FULL_POOL_SAMPLER

        from pipeline.batch.engine_artifacts import EngineArtifacts
        from simulation.batch_runner import _WORKER_SHARED
        from simulation.full_pool_sampler import FullPoolSampler

        # SIM-402: pass the shared views to load() so the disk-load path SKIPS
        # the big .npy reads when the parent has already published them into
        # shared memory.  This is the meaningful cold-worker speedup — without
        # it, 9 cold workers each reading ~150 MB from the Windows-Docker
        # volume serialize into a ~500 s stall on a 10-iteration request.
        views = _WORKER_SHARED.get("views") or {}
        art = EngineArtifacts.load(art_dir, shared_views=views)
        sampler = FullPoolSampler(art, np.random.default_rng(seed))
        _CACHED_FULL_POOL_SAMPLER = sampler
        _CACHED_FULL_POOL_ART_DIR = art_dir
        return sampler
    except Exception:
        return None


def reset_caches() -> None:
    """SIM-402: clear THIS process's cached full-pool sampler.

    The cache lives at module scope, so it persists for the life of the worker
    process (the intended behaviour) AND across tests in the same process.  Tests
    that exercise the full-pool path call this in setup/teardown so one test's
    cached sampler can't leak into the next; production never needs it (a worker
    builds its cache once and keeps it).
    """
    global _CACHED_FULL_POOL_SAMPLER, _CACHED_FULL_POOL_ART_DIR
    _CACHED_FULL_POOL_SAMPLER = None
    _CACHED_FULL_POOL_ART_DIR = None


def _warm_sampler(sampler: Any) -> None:
    """SIM-402: force the FullPoolSampler's lazy, one-time per-hand precomputes so a
    pre-warmed worker is FULLY hot.

    ``FullPoolSampler.__init__`` is cheap — the expensive ``O(pool)`` index builds
    are DEFERRED to first use: the pitch-pool ``_pool_meta(hand)`` (fires on the
    first ``new_half_inning``) and the batted-ball ``_bb_pool_bat_idx(hand)``
    (fires on the first ``battedball_new_pa``).  Building the sampler alone would
    therefore leave both for the worker's FIRST real game — defeating the point of
    pre-warming.  We trigger both for every hand in the bundle, so the first game
    runs at the amortized per-game cost.  This only POPULATES caches (no rng /
    matchup state is touched), so it cannot perturb a later game.  Defensive: a
    partial bundle / missing attr just leaves that part to warm lazily.
    """
    art = getattr(sampler, "a", None)
    for pools_attr, method_name in (
        ("pools", "_pool_meta"),
        ("bb_pools", "_bb_pool_bat_idx"),
        # SIM-474: the steal-opportunity cell index + embedding-row gathers.
        ("steal_pools", "_steal_meta"),
    ):
        warm = getattr(sampler, method_name, None)
        if warm is None:
            continue
        for hand in getattr(art, pools_attr, {}) or {}:
            try:
                warm(hand)
            except Exception:
                pass


def warm_worker_cache(pool_dir: str | None = None) -> bool:
    """SIM-402: populate THIS process's full-pool sampler cache up front.

    Builds the full-pool sampler AND triggers its lazy per-hand precomputes
    (:func:`_warm_sampler`) into the per-process cache
    :func:`production_machine_factory` reads, so the FIRST real ``/simulate`` game
    on this process is a warm cache hit instead of paying the ~per-worker
    artifact-load + per-hand index-build cost on the request path.  This is the
    function :meth:`simulation.batch_runner.BatchRunner.prewarm` runs on every
    warm-pool worker at startup -- the fix for the n>1 cold-fan-out stall, where a
    fresh pool gives each worker exactly one game so the per-worker cache (which
    only pays off on the 2nd+ seed) never warms in time.

    Returns ``True`` when a full-pool sampler is now cached (``SIM_FULL_POOL`` on
    AND the engine-artifact bundle present), else ``False`` (full-pool disabled or
    a missing/corrupt bundle -> the per-tile fallback warms lazily instead).  Never
    raises: any build failure is swallowed and reported as ``False``.
    """
    try:
        spec = GameSpec(sim_kwargs={"_pool_dir": pool_dir} if pool_dir else {})
        sampler = _build_full_pool_sampler(spec, 0)
        if sampler is None:
            return False
        _warm_sampler(sampler)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# SIM-434 — manager wiring (GATED by SIM_MANAGER; default OFF == unchanged)
# ---------------------------------------------------------------------------
#
# With SIM_MANAGER off (the production default today + the unit-test default),
# the factory wires NO manager onto the StateMachine, so every SIM-323 §3/§5.3
# hook early-returns and the simulated game is byte-identical to before.  With
# SIM_MANAGER on, the factory attaches a default manager-tendency profile + a
# generic per-team bullpen so the starter is actually pulled at a realistic
# workload (the SIM-429 K/BB-distribution unlock).  Populating REAL per-team
# bullpen rosters + true reliever similarity profiles is the SIM-427 follow-on
# (blocked on a profile recompute); the generic default here is a valid
# league-flat stand-in until then.

#: A default manager-tendency profile: a plain dict of the SIM-2.8 manager-
#: similarity rate names the SIM-323 hooks read by name.  Tuned to league-typical
#: behaviour (a starter is reliably pulled around the floor/ceiling; modest
#: small-ball aggression).  A duck-typed dict so no engine import is needed.
_DEFAULT_MANAGER_PROFILE: dict[str, float] = {
    "starter_pull_pct_before_100": 0.35,
    "pinch_hit_rate_high_leverage": 0.20,
    "sac_bunt_rate_high_leverage": 0.10,
    "sac_bunt_rate_low_leverage": 0.05,
    "steal_order_rate_per_1b_opp": 0.08,
    "platoon_advantage_exploitation_rate": 0.30,
}


def _manager_enabled() -> bool:
    """Whether SIM_MANAGER enables the SIM-434 manager wiring (default OFF).

    Parsed identically to ``SIM_FULL_POOL`` (unset / ``0`` / ``false`` / ``no`` /
    ``off`` -> disabled).  Centralised so the factory and any test read the gate
    the same way.
    """
    env = os.environ.get("SIM_MANAGER", "").strip().lower()
    return env not in ("", "0", "false", "no", "off")


def _default_bullpen_for_spec(spec: GameSpec) -> dict[int, list[int]]:
    """Build a generic per-team bullpen (SIM-434), keyed by the ``Team`` int value.

    Until SIM-427 ingests real per-(team, season) bullpen rosters, we synthesise a
    small generic pen per team from a deterministic id offset so a pulled reliever
    is a distinct (league-flat) arm rather than re-using the starter.  Keyed by the
    int Team value (0 == AWAY, 1 == HOME) so it survives pickling through the
    GameSpec without importing the Team enum here.  Six arms per side (closer
    first), enough to cover a full game's worth of changes.
    """
    away_starter = int(_kwarg(spec, "away_pitcher_id", 0) or 0)
    home_starter = int(_kwarg(spec, "home_pitcher_id", 0) or 0)
    # Synthetic, deterministic, collision-avoiding ids (negative so they never
    # alias a real player id in the pool — a pulled arm degrades to a league-flat
    # draw, which is valid per the SIM-427 note).
    base = 9_000_000
    away_pen = [-(base + away_starter * 10 + i) for i in range(1, 7)]
    home_pen = [-(base + home_starter * 10 + i) for i in range(1, 7)]
    return {0: away_pen, 1: home_pen}


#: Injectable bullpen builder (the no-DB test seam, mirroring _SAMPLER_BUILDER).
#: A test swaps this to assert the factory wires a known bullpen.
_BULLPEN_BUILDER: Callable[[GameSpec], dict[int, list[int]]] = _default_bullpen_for_spec


def set_bullpen_builder(
    builder: Callable[[GameSpec], dict[int, list[int]]] | None,
) -> Callable[[GameSpec], dict[int, list[int]]]:
    """Install ``builder`` as the active bullpen builder; return the previous one
    (SIM-434).  ``None`` restores :func:`_default_bullpen_for_spec`."""
    global _BULLPEN_BUILDER
    prev = _BULLPEN_BUILDER
    _BULLPEN_BUILDER = builder if builder is not None else _default_bullpen_for_spec
    return prev


def production_machine_factory(seed: int | None, spec: GameSpec) -> StateMachine:
    """Build a REAL, DuckDB/FAISS-backed :class:`StateMachine` for the worker.

    The production counterpart of
    :func:`simulation.batch_runner.rng_driven_machine_factory`, sharing its
    ``factory(seed, spec) -> StateMachine`` contract so it drops into the same
    :class:`GameSpec.machine_factory` seam.  Referenced by dotted path
    ``"simulation.production_factory:production_machine_factory"``.

    Steps:
      1. build a :class:`PlayPoolSampler` via the (injectable) :data:`_SAMPLER_BUILDER`,
         seeding its k-NN rng from the per-game ``seed`` (§6.3 reproducibility);
      2. when ``spec.shared_segments`` is set, ATTACH the SIM-333 shared tiles
         zero-copy (:func:`_attach_shared_tiles`) so the worker samples over the
         shared buffer instead of re-reading the tiles from disk;
      3. build the (injectable) fingerprint deriver via :data:`_DERIVER_BUILDER`
         (default ``None`` -> the StateMachine's stub-hash fingerprints);
      4. construct the :class:`StateMachine` wired to that sampler + deriver, with a
         loop rng seeded from the same ``seed`` and ``k`` from the factory-only
         ``_k`` knob (default 25).

    ``simulate_game`` re-seeds both the machine's loop rng AND the sampler's k-NN
    rng from the per-game ``seed`` again on its way through, so determinism holds
    end-to-end regardless of how the factory seeded them here.
    """
    k = int(_kwarg(spec, "_k", _DEFAULT_K))

    # SIM-402: build the full-pool sampler FIRST so we can short-circuit the
    # per-seed deriver build below on the production (full-pool) path.
    full_pool = _build_full_pool_sampler(spec, seed)

    sampler = _SAMPLER_BUILDER(spec, seed)
    if spec.shared_segments:
        _attach_shared_tiles(sampler, spec)

    # SIM-402: on the full-pool path the per-tile FingerprintDeriver is never
    # consulted -- ``_full_pool_outcome`` (and the engine-backed batted-ball
    # draw) read from ``full_pool`` exclusively, and every ``fingerprint_deriver``
    # call site in the loop is None-guarded -- yet ``_default_deriver_builder``
    # does THREE eager disk loads (provider + pitch_norm + battedball_norm) on
    # EVERY seed.  Skipping it when the full-pool sampler is active removes that
    # per-game waste from the cold-worker path (the SIM-372 SLA).  The per-tile
    # fallback (``full_pool is None`` -- the unit-test default, ``SIM_FULL_POOL=0``)
    # is UNCHANGED: it still builds the deriver every call.  The per-tile sampler
    # itself stays per-seed -- its construction is free (lazy DuckDB connection,
    # opened only on a ``sample_*`` call that the full-pool path never makes), so
    # it needs no cache; it exists only to satisfy the StateMachine's ``_pa`` guard.
    deriver = None if full_pool is not None else _DERIVER_BUILDER(spec)

    # SIM-434: GATED manager wiring.  With SIM_MANAGER off (default) ``manager``
    # stays None -> the StateMachine makes every §3/§5.3 hook a no-op and the game
    # is byte-identical to before.  With it on, attach a default tendency profile
    # and stage a generic per-team bullpen on the machine; ``simulate_game`` seeds
    # that bullpen onto the GameState (its own ``bullpen=`` param takes priority).
    manager = _DEFAULT_MANAGER_PROFILE if _manager_enabled() else None
    machine = StateMachine(
        sampler=sampler,
        k=k,
        rng=np.random.default_rng(seed),
        fingerprint_deriver=deriver,
        full_pool_sampler=full_pool,
        manager=manager,
    )
    if manager is not None:
        # Stage the generic bullpen on the machine so ``simulate_game`` can seed it
        # onto the GameState (it falls back to ``machine.bullpen`` when no explicit
        # ``bullpen=`` is passed — the production path through ``_run_one``).
        machine.bullpen = _BULLPEN_BUILDER(spec)
    return machine


__all__ = [
    "production_machine_factory",
    "set_sampler_builder",
    "set_deriver_builder",
    "set_bullpen_builder",
    "use_sampler_builder",
    "warm_worker_cache",
    "reset_caches",
    "SamplerBuilder",
    "DeriverBuilder",
]
