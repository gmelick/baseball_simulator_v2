"""
production_factory.py
=====================
The production, DB-backed machine factory for the batch runner (SIM-352 /
SIM-424 / SIM-486).

WHAT THIS IS
------------
:mod:`simulation.batch_runner` fans :func:`simulation.sim_loop.simulate_game` out
across a ``ProcessPoolExecutor``; each worker rebuilds its own live
:class:`~simulation.sim_loop.StateMachine` IN-PROCESS from a small, picklable
:class:`~simulation.batch_runner.GameSpec` via a *dotted-ref* ``machine_factory``
(never a live sampler across the fork -- SIM-281 D1).  This module's
:func:`production_machine_factory` builds the REAL machine: a
:class:`~simulation.full_pool_sampler.FullPoolSampler` over the on-disk
engine-artifact bundle (:class:`~pipeline.batch.engine_artifacts.EngineArtifacts`),
cached once per worker process (SIM-402), with the SIM-403b shared-memory views
spliced in when the parent published them.  Reference it on a :class:`GameSpec`
by dotted path::

    GameSpec(
        machine_factory="simulation.production_factory:production_machine_factory",
        sim_kwargs={"pitcher_id": 477132, "bat_hand": "R", "season": 2024,
                    "away_lineup": [...], "home_lineup": [...]},
    )

THERE IS NO FALLBACK (SIM-486)
------------------------------
Until SIM-486 a missing or corrupt bundle silently degraded to a second,
per-tile simulator.  Now a missing bundle raises here, at machine build, so a
worker that cannot serve the production path fails loudly instead of serving a
different one.  The no-DB seam for tests is
:func:`simulation.batch_runner.rng_driven_machine_factory`, which builds the SAME
machine over an in-memory :mod:`simulation.synthetic_bundle` bundle.

FACTORY-ONLY SIM-KWARGS (the ``_``-prefixed keys, SIM-377)
----------------------------------------------------------
  * ``_pool_dir`` -- the play-pool root (default ``$BASEBALL_PLAY_POOL_DIR`` /
    ``/data/play_pool``); the bundle lives in its ``engine_artifacts/`` subdir.

The non-underscore keys (``pitcher_id`` / ``bat_hand`` / ``season`` / the two
lineups / ``max_innings`` / ...) flow through to ``simulate_game``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import numpy as np

from simulation.batch_runner import GameSpec
from simulation.sim_loop import StateMachine


def _kwarg(spec: GameSpec, name: str, default: Any) -> Any:
    """Read a ``sim_kwargs`` value (factory-only ``_``-prefixed keys included)."""
    kw = spec.sim_kwargs if isinstance(spec.sim_kwargs, dict) else {}
    val = kw.get(name, default)
    return default if val is None else val


# ---------------------------------------------------------------------------
# The per-worker full-pool sampler cache (SIM-402)
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


def _artifact_dir(spec: GameSpec) -> str:
    pool_dir = _kwarg(spec, "_pool_dir", None) or os.environ.get(
        "BASEBALL_PLAY_POOL_DIR", "/data/play_pool"
    )
    return os.path.join(pool_dir, "engine_artifacts")


def _build_full_pool_sampler(spec: GameSpec, seed: int | None):
    """Build (or reuse) the worker's full-pool sampler from the on-disk
    engine-artifact bundle.  Raises when the bundle cannot be loaded: SIM-486
    removed the per-tile fallback, so there is nothing to degrade to.

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
    art_dir = _artifact_dir(spec)
    global _CACHED_FULL_POOL_SAMPLER, _CACHED_FULL_POOL_ART_DIR
    # Reuse the cached sampler when the artifact dir matches.  Re-seed its
    # rng so the per-game seed still governs the weighted draws (the per-game
    # half-inning / PA state caches inside the sampler are reset at the loop's
    # natural boundaries — `new_half_inning` / `battedball_new_pa` — so
    # cross-game leakage is impossible).
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
    try:
        art = EngineArtifacts.load(art_dir, shared_views=views)
    except Exception as exc:
        raise RuntimeError(
            f"SIM-486: cannot load the engine-artifact bundle from {art_dir!r} "
            f"({type(exc).__name__}: {exc}). There is no fallback simulator; build "
            "the bundle (python -m pipeline.batch.engine_artifacts --what all)."
        ) from exc
    # SIM-491 (the SIM-412 rebuild): the home-field draw weight. 1.0 (the
    # default, and any unparsable value) disables the reweight EXACTLY —
    # byte-identical to pre-SIM-491. SIM-476 (owner ruling 2026-08-30):
    # production runs w=0.0 — the draw hard-conditions on the batting side,
    # delivering the pool's own home/away differential (+0.107 R/g
    # measured, the MLB size). Soft weights in (0,1) deliver only
    # (1-w)/(1+w) of it — the 12x400 A/B in the SIM-476 fit plan.
    try:
        home_w = float(os.environ.get("SIM_HOME_OFF_WEIGHT", "1.0"))
    except ValueError:
        home_w = 1.0
    sampler = FullPoolSampler(art, np.random.default_rng(seed), home_off_weight=home_w)
    # SIM-491 part 2 (the SIM-411 rebuild): the park kernel. 0.0 (the
    # default, and any unparsable value) disables it EXACTLY. When enabled,
    # load the (venue_id, season) -> regressed run-factor map read-only;
    # a load failure leaves the map None and the kernel neutral.
    try:
        park_sigma = float(os.environ.get("SIM_PARK_KERNEL_SIGMA", "0"))
    except ValueError:
        park_sigma = 0.0
    if park_sigma > 0.0:
        sampler.venue_run_factors = _load_venue_run_factors()
        if sampler.venue_run_factors:
            sampler.park_sigma = park_sigma
    # SIM-491 part 3 (the SIM-425b rebuild): the fielder-quality kernel
    # bandwidth. 0.0 (the default, and any unparsable value) disables it
    # EXACTLY; the fielder embedding is already in the artifact bundle.
    try:
        sampler.fielder_sigma = float(os.environ.get("SIM_FIELDER_KERNEL_SIGMA", "0"))
    except ValueError:
        sampler.fielder_sigma = 0.0
    # SIM-517: the catcher RECEIVING kernel bandwidths — an anisotropic
    # metric with the framing dims and the blocking dims under their own
    # sigma (the part-E ladder measured they need different ones). 0.0
    # (the default, and any unparsable value) removes that group; both
    # 0.0 disables the kernel EXACTLY.
    for env, attr in (
        ("SIM_CATCHER_FRAMING_SIGMA", "catcher_framing_sigma"),
        ("SIM_CATCHER_BLOCK_SIGMA", "catcher_block_sigma"),
    ):
        try:
            setattr(sampler, attr, float(os.environ.get(env, "0")))
        except ValueError:
            setattr(sampler, attr, 0.0)
    _CACHED_FULL_POOL_SAMPLER = sampler
    _CACHED_FULL_POOL_ART_DIR = art_dir
    return sampler


#: SIM-515: the per-worker cached IBB rate table (sim.ibb_rates is ~48 rows;
#: loaded once, like the venue-factor map). The sentinel False = not yet
#: attempted; None = attempted and unavailable.
_CACHED_IBB_RATES: dict[tuple[int, int, bool, bool], float] | None | bool = False


def _load_ibb_rates() -> dict[tuple[int, int, bool, bool], float] | None:
    """SIM-515: load the measured IBB rate per cell from sim.ibb_rates,
    read-only, cached per worker. Returns None on any failure (no duckdb, a
    missing file, a pre-0020 DB, an empty table) — no IBB then fires, which is
    the no-op-safe contract, not a silent formula fallback."""
    global _CACHED_IBB_RATES
    if _CACHED_IBB_RATES is not False:
        return _CACHED_IBB_RATES  # type: ignore[return-value]
    out: dict[tuple[int, int, bool, bool], float] | None
    try:
        from simulation.sim_kwargs import open_sim_duckdb

        con = open_sim_duckdb()
        if con is None:
            _CACHED_IBB_RATES = None
            return None
        try:
            rows = con.execute(
                "SELECT runners_state, outs, is_late, is_close, opportunities, issued "
                "FROM sim.ibb_rates WHERE opportunities > 0"
            ).fetchall()
        finally:
            con.close()
        out = {
            (int(rs), int(o), bool(late), bool(close)): float(iss) / float(opp)
            for rs, o, late, close, opp, iss in rows
        } or None
    except Exception:
        out = None
    _CACHED_IBB_RATES = out
    return out


def _load_venue_run_factors() -> dict[tuple[int, int], float] | None:
    """SIM-491 part 2 (SIM-411): load the (venue_id, season) -> regressed run
    park-factor map from the sim DuckDB, read-only. Returns None on any failure
    (no duckdb, a missing file, an empty table) — the park kernel then stays
    neutral. Loaded once per worker alongside the cached sampler."""
    try:
        from simulation.sim_kwargs import open_sim_duckdb

        con = open_sim_duckdb()
        if con is None:
            return None
        try:
            rows = con.execute(
                "SELECT venue_id, season, regressed_factor FROM derived.park_factors "
                "WHERE factor_type = 'R' AND regressed_factor IS NOT NULL"
            ).fetchall()
        finally:
            con.close()
        out = {(int(v), int(s)): float(f) for v, s, f in rows if 0.5 <= float(f) <= 2.0}
        return out or None
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
    global _CACHED_FULL_POOL_SAMPLER, _CACHED_FULL_POOL_ART_DIR, _CACHED_IBB_RATES
    _CACHED_FULL_POOL_SAMPLER = None
    _CACHED_FULL_POOL_ART_DIR = None
    _CACHED_IBB_RATES = False  # SIM-515: back to "not yet attempted"


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

    Returns ``True`` when a full-pool sampler is now cached, else ``False`` (a
    missing/corrupt bundle -> the request path raises loudly at machine build).
    Never raises: any build failure is swallowed and reported as ``False``.
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
# With SIM_MANAGER off (the unit-test default), the factory wires NO manager onto
# the StateMachine, so every SIM-323 §3/§5.3 hook early-returns and the simulated
# game is byte-identical to before.  With SIM_MANAGER on (production), the factory
# attaches a default manager-tendency profile + a generic per-team bullpen so the
# starter is actually pulled at a realistic workload (the SIM-429 K/BB-distribution
# unlock).  Populating REAL per-team bullpen rosters + true reliever similarity
# profiles is the SIM-427 follow-on; the generic default here is a valid
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

    Unset / ``0`` / ``false`` / ``no`` / ``off`` -> disabled.  Centralised so the
    factory and any test read the gate the same way.
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


#: Injectable bullpen builder (the no-DB test seam).  A test swaps this to assert
#: the factory wires a known bullpen.
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
    """Build the REAL :class:`StateMachine` for the worker: the full-pool sampler
    over the engine-artifact bundle (cached per process), the SIM-434 manager
    wiring when ``SIM_MANAGER`` is on, and the SIM-515 IBB rate table.

    Shares the ``factory(seed, spec) -> StateMachine`` contract with
    :func:`simulation.batch_runner.rng_driven_machine_factory`, so it drops into
    the same :class:`GameSpec.machine_factory` seam.  Referenced by dotted path
    ``"simulation.production_factory:production_machine_factory"``.

    ``simulate_game`` re-seeds both the machine's loop rng AND the sampler's rng
    from the per-game ``seed`` again on its way through, so determinism holds
    end-to-end regardless of how the factory seeded them here.
    """
    full_pool = _build_full_pool_sampler(spec, seed)
    # SIM-434: GATED manager wiring.  With SIM_MANAGER off ``manager`` stays None
    # -> the StateMachine makes every §3/§5.3 hook a no-op.  With it on, attach a
    # default tendency profile and stage a generic per-team bullpen on the
    # machine; ``simulate_game`` seeds that bullpen onto the GameState (its own
    # ``bullpen=`` param takes priority).
    manager = _DEFAULT_MANAGER_PROFILE if _manager_enabled() else None
    machine = StateMachine(
        full_pool,
        rng=np.random.default_rng(seed),
        manager=manager,
        # SIM-515: the measured IBB rate table. None with no manager (the hook
        # is a no-op then anyway) and on a pre-0020 DB (no IBB fires).
        ibb_rates=_load_ibb_rates() if manager is not None else None,
    )
    if manager is not None:
        # Stage the generic bullpen on the machine so ``simulate_game`` can seed it
        # onto the GameState (it falls back to ``machine.bullpen`` when no explicit
        # ``bullpen=`` is passed — the production path through ``_run_one``).
        machine.bullpen = _BULLPEN_BUILDER(spec)
    return machine


__all__ = [
    "production_machine_factory",
    "set_bullpen_builder",
    "warm_worker_cache",
    "reset_caches",
]
