"""The production machine factory for the batch runner.

:mod:`simulation.batch_runner` fans :func:`simulation.sim_loop.simulate_game`
out across a ``ProcessPoolExecutor``. Each worker builds its own
:class:`~simulation.sim_loop.StateMachine` in its own process from a small,
picklable :class:`~simulation.batch_runner.GameSpec`, through a *dotted-ref*
``machine_factory``; a live sampler never crosses the process boundary. This
module's :func:`production_machine_factory` builds the REAL machine: a
:class:`~simulation.full_pool_sampler.FullPoolSampler` over the on-disk
engine-artifact bundle (:class:`~pipeline.batch.engine_artifacts.EngineArtifacts`).
Each worker process caches the sampler once, with the parent's shared-memory
arrays spliced in when the parent published them. Reference the factory on a
:class:`GameSpec` by dotted path::

    GameSpec(
        machine_factory="simulation.production_factory:production_machine_factory",
        sim_kwargs={"pitcher_id": 477132, "bat_hand": "R", "season": 2024,
                    "away_lineup": [...], "home_lineup": [...]},
    )

THERE IS NO FALLBACK
--------------------
A missing or corrupt bundle raises here, at machine build, so a worker that
cannot serve the production path fails loudly instead of serving a different
one. The no-DB seam for tests is
:func:`simulation.batch_runner.rng_driven_machine_factory`, which builds the
SAME machine over an in-memory :mod:`simulation.synthetic_bundle` bundle.

FACTORY-ONLY SIM-KWARGS (the ``_``-prefixed keys)
-------------------------------------------------
  * ``_pool_dir`` -- the play-pool root; the bundle lives in its
    ``engine_artifacts/`` subdirectory. Without it the factory reads
    ``$BASEBALL_ENGINE_ARTIFACT_DIR`` (the bundle version the app pinned), else
    ``$BASEBALL_PLAY_POOL_DIR`` (default ``/data/play_pool``).
  * ``_asof_ymd`` -- the point-in-time cutoff (a YYYYMMDD integer): the sampler
    draws no pool row from after that date.

The non-underscore keys (``pitcher_id`` / ``bat_hand`` / ``season`` / the two
lineups / ``max_innings`` / ...) flow through to ``simulate_game``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np

from simulation.batch_runner import GameSpec
from simulation.flags import FLAGS, actor_power_overrides, read_bool, read_float, read_int
from simulation.sim_loop import StateMachine

logger = logging.getLogger(__name__)

#: The pitch draw's situation bandwidth: the registry's code default, equal to
#: the sampler's own default.
DEFAULT_SIT_SIGMA = float(FLAGS["SIM_SIT_SIGMA"].default)  # type: ignore[arg-type]


def apply_sit_sigma_env(sampler: Any, env: Mapping[str, str] | None = None) -> None:
    """Read ``SIM_SIT_SIGMA``, the pitch draw's situation bandwidth.

    The bandwidth is the width of the Gaussian on the base-out situation
    (``FullPoolSampler.sit_sigma``). The default is 2.0, the sampler's own
    default. A value that does not parse keeps the default and logs a warning.
    The offline fit (the joint weight fit, SIM-548) tries a series of values
    and sets each arm's value here.
    """
    sampler.sit_sigma = read_float("SIM_SIT_SIGMA", env)


def _kwarg(spec: GameSpec, name: str, default: Any) -> Any:
    """Read a ``sim_kwargs`` value (factory-only ``_``-prefixed keys included)."""
    kw = spec.sim_kwargs if isinstance(spec.sim_kwargs, dict) else {}
    val = kw.get(name, default)
    return default if val is None else val


# ---------------------------------------------------------------------------
# The per-worker full-pool sampler cache
# ---------------------------------------------------------------------------

#: The per-worker cache of the EngineArtifacts and the FullPoolSampler.
#: Without it, :func:`_run_one` calls this factory once per seed, and the
#: worker reloads the ~290 MB bundle from disk and rebuilds the sampler's
#: per-hand caches for every game: a ~1.5 s game becomes a ~9 s stall that
#: breaks the 2 s game / 30 s batch budget (the worker warm-up, SIM-402). With
#: the cache, the first seed pays the load and every later seed reuses the
#: warm sampler. Only the per-game rng is re-seeded; the loop resets the
#: per-game state (the half-inning and plate-appearance caches) at its own
#: boundaries inside ``simulate_game``. The cache lives per process, so each
#: pool worker builds its own at first use; the parent never holds it.
_CACHED_FULL_POOL_SAMPLER = None
_CACHED_FULL_POOL_ART_DIR: str | None = None


#: The environment variable that pins the bundle VERSION a worker reads.
#: The app resolves the published link at boot and sets it before the pool
#: starts, so a worker that respawns after a rebuild reads the same version as
#: the parent's shared arrays, never the newly published one.
PINNED_ARTIFACT_DIR_ENV = "BASEBALL_ENGINE_ARTIFACT_DIR"


def _artifact_dir(spec: GameSpec) -> str:
    explicit = _kwarg(spec, "_pool_dir", None)
    if explicit:
        return os.path.join(explicit, "engine_artifacts")
    pinned = os.environ.get(PINNED_ARTIFACT_DIR_ENV)
    if pinned:
        return pinned
    pool_dir = os.environ.get("BASEBALL_PLAY_POOL_DIR", "/data/play_pool")
    return os.path.join(pool_dir, "engine_artifacts")


def apply_actor_matrix_env(sampler: Any, env: Mapping[str, str] | None = None) -> None:
    """Read the powers of the actor score matrices.

    Every actor factor is its engine's composite score from the nightly matrix,
    raised to the power ``SIM_ACTOR_POWER_<NAME>``. The names: batter,
    catcher_throwing, runner_steal, runner_adv, pitcher_steal, manager_usage
    and fielder. The fielder power applies to every per-position fielder
    matrix that has no power of its own. An unset power is 1.0, except
    manager_usage, which is off (0.0); a power of 0 turns its factor off.
    """
    powers = actor_power_overrides(env)
    if "fielder" in powers:
        for pos in ("1B", "2B", "3B", "SS", "LF", "CF", "RF"):
            powers.setdefault(f"fielder_{pos}", powers["fielder"])
    sampler.actor_power = powers


def apply_result_split_env(sampler: Any, env: Mapping[str, str] | None = None) -> None:
    """Read the pitch / pitch-result split, its bandwidth and powers, the
    catcher receiving switch and the born-ball kernel.

    OFF (the code default) keeps one draw per pitch. ON splits it: the pitch
    draw, then the result draw conditioned on the pitch through the pitch
    engine's metric (``SIM_RESULT_PITCH_SIGMA``, 1.0). The result draw's
    density correction has the power ``SIM_RESULT_DENSITY_POWER`` (1.0; 0 =
    off). The result draw raises the pitcher and batter factors again
    (``SIM_RESULT_PITCHER_POWER`` / ``SIM_RESULT_BATTER_POWER``, 1.0); the
    result pitcher power is absolute. On both paths the pitch draw raises its
    pitcher factor to ``SIM_PITCH_PITCHER_POWER`` and its batter factor to
    ``SIM_PITCH_BATTER_POWER`` (1.0 each).

    ``SIM_BB_BORN_SIGMA`` (0 = off) weights the fielding draw by similarity to
    the batted ball the result row was born with; ``SIM_BB_BORN_DENSITY_POWER``
    (1.0) is the power of that kernel's density correction.
    """
    # The catcher receiving ratio (OFF until the receiving enable, SIM-526).
    sampler.catcher_receiving = read_bool("SIM_CATCHER_RECEIVING", env)
    sampler.pitch_result_split = read_bool("SIM_PITCH_RESULT_SPLIT", env)
    for key, attr in (
        ("SIM_RESULT_PITCH_SIGMA", "result_pitch_sigma"),
        ("SIM_RESULT_DENSITY_POWER", "result_density_power"),
        ("SIM_PITCH_PITCHER_POWER", "pitch_pitcher_power"),
        ("SIM_RESULT_PITCHER_POWER", "result_pitcher_power"),
        ("SIM_RESULT_BATTER_POWER", "result_batter_power"),
        ("SIM_PITCH_BATTER_POWER", "pitch_batter_power"),
        ("SIM_BB_BORN_SIGMA", "bb_born_sigma"),
        ("SIM_BB_BORN_DENSITY_POWER", "bb_born_density_power"),
    ):
        setattr(sampler, attr, read_float(key, env))


def apply_running_game_env(sampler: Any, env: Mapping[str, str] | None = None) -> None:
    """Read ``SIM_STEAL_PITCH_CLASS``: the running game on the pitch (SIM-554).

    The default is ON, unlike the other sampler flags: the change landed on at
    its best-known default (the owner's ruling of 2026-09-16). On, with a
    bundle whose steal pool carries the pitch class, the loop draws the
    pickoff before the pitch and the steal after it, among real pitches of the
    pitch's class. "0", "false", "no", "off" or "" turn it off: the loop then
    makes the single pre-pitch steal draw.
    """
    sampler.steal_pitch_class = read_bool("SIM_STEAL_PITCH_CLASS", env)


def apply_fielding_env(sampler: Any, env: Mapping[str, str] | None = None) -> None:
    """Read the fielding draw's class filter, its batter power, the park
    wall-zone rule, the fence stage and the fence amendments.

    The code defaults: ``SIM_BB_CLASS_FILTER`` (0/1, off),
    ``SIM_BB_BATTER_POWER`` (1.0), ``SIM_PARK_WALL_ZONE_ONLY`` (0/1, off) with
    ``SIM_WALL_ZONE_DISTANCE`` (feet, 300), and the fence stage
    ``SIM_FENCE_STAGE`` (0/1, off) with ``SIM_FENCE_MARGIN`` (feet, 0 =
    decisive).

    The fence certification's amendments (SIM-478), all off at their code
    defaults. ``SIM_CARRY_OFFSET`` (0/1) reads the born ball's carry in the
    live park's air, from the document's per-park offsets.
    ``SIM_BB_BORN_PER_FEATURE`` (0/1) takes the born kernel's exponent per
    feature: its divisor is 2 sigma^2, not 2 sigma^2 x the feature count.
    ``SIM_BB_MARGIN_BAND`` (feet, 0 = off) lets an air ball the fence did not
    call over draw only rows within this band of its margin to the wall.
    ``SIM_BB_MARGIN_MIN_ROWS`` (20) is the fewest rows the band may leave;
    with fewer, the rows stay as they were.
    """
    sampler.bb_class_filter = read_bool("SIM_BB_CLASS_FILTER", env)
    sampler.park_wall_zone_only = read_bool("SIM_PARK_WALL_ZONE_ONLY", env)
    # The fence stage (0/1); its band, SIM_FENCE_MARGIN, is read below.
    sampler.fence_stage = read_bool("SIM_FENCE_STAGE", env)
    # The fence amendments: the carry offset and the born kernel's exponent per feature.
    sampler.carry_offset = read_bool("SIM_CARRY_OFFSET", env)
    sampler.bb_born_per_feature = read_bool("SIM_BB_BORN_PER_FEATURE", env)
    for key, attr in (
        ("SIM_BB_BATTER_POWER", "bb_batter_power"),
        ("SIM_WALL_ZONE_DISTANCE", "wall_zone_distance"),
        ("SIM_FENCE_MARGIN", "fence_margin"),
        # The wall-margin band (feet; 0 = off).
        ("SIM_BB_MARGIN_BAND", "bb_margin_band"),
    ):
        setattr(sampler, attr, read_float(key, env))
    sampler.bb_margin_min_rows = read_int("SIM_BB_MARGIN_MIN_ROWS", env)


def _build_full_pool_sampler(spec: GameSpec, seed: int | None):
    """Build (or reuse) the worker's full-pool sampler from the on-disk
    engine-artifact bundle. Raises when the bundle cannot be loaded: there is
    no other simulator to fall back to.

    When the parent published the bundle's arrays into shared memory
    (``BatchRunner(shared_arrays=...)``), the worker's :func:`_worker_init` has
    already attached them and put zero-copy views in
    :data:`simulation.batch_runner._WORKER_SHARED` ``["views"]``. The loader
    then skips those big arrays on disk, reads the other members (e.g. the
    pitcher-sim dict and the object-dtype ``outcome_type`` / ``event`` arrays)
    and splices the shared views in. Each worker's resident memory drops by the
    size of the shared arrays (hundreds of MB at production scale).

    The loaded artifacts and the FullPoolSampler are cached at module scope.
    The first call in a worker pays the disk load, the splice and the
    per-hand precomputes; every later call reuses the warm sampler and
    re-seeds only its rng. Without the cache, a 100-game ``/simulate`` request
    paid the ~6-9 s warm-up 100 times (the worker warm-up, SIM-402).
    """
    art_dir = _artifact_dir(spec)
    global _CACHED_FULL_POOL_SAMPLER, _CACHED_FULL_POOL_ART_DIR, _CACHED_IBB_RATES
    # Reuse the cached sampler when the artifact directory matches. Re-seed its
    # rng, so the per-game seed still governs the weighted draws. The loop
    # resets the sampler's per-game half-inning and plate-appearance state at
    # its own boundaries (`new_half_inning` / `battedball_new_pa`), so no state
    # leaks from one game to the next.
    if _CACHED_FULL_POOL_SAMPLER is not None and art_dir == _CACHED_FULL_POOL_ART_DIR:
        _CACHED_FULL_POOL_SAMPLER.rng = np.random.default_rng(seed)
        return _CACHED_FULL_POOL_SAMPLER

    from pipeline.batch.engine_artifacts import EngineArtifacts
    from simulation.batch_runner import _WORKER_SHARED
    from simulation.full_pool_sampler import FullPoolSampler

    # Pass the shared views to load(), so the loader SKIPS the big .npy reads
    # the parent already published into shared memory. Without it, 9 cold
    # workers each read ~150 MB from the Windows Docker volume, and the reads
    # serialize into a ~500 s stall on a 10-game request (the worker warm-up,
    # SIM-402).
    views = _WORKER_SHARED.get("views") or {}
    try:
        art = EngineArtifacts.load(art_dir, shared_views=views)
    except Exception as exc:
        raise RuntimeError(
            f"SIM-486: cannot load the engine-artifact bundle from {art_dir!r} "
            f"({type(exc).__name__}: {exc}). There is no fallback simulator; build "
            "the bundle (python -m pipeline.batch.engine_artifacts --what all)."
        ) from exc
    # The home-field draw weight. 1.0, the code default and the value of any
    # unparsable setting, turns the reweight off exactly. Production runs
    # w=0.0 (the owner's ruling of 2026-08-30): the draw hard-conditions on the
    # batting side and delivers the pool's own home/away gap, +0.107 runs a
    # game, the MLB size. A soft weight w in (0, 1) delivers only (1-w)/(1+w)
    # of it (the 12 x 400 comparison of the home-field fit, SIM-476).
    home_w = read_float("SIM_HOME_OFF_WEIGHT")
    sampler = FullPoolSampler(art, np.random.default_rng(seed), home_off_weight=home_w)
    # A bundle with the reference data carries the intentional-walk (IBB)
    # rates, so the worker never opens DuckDB for them (a rebuild may hold
    # the file's lock).
    if getattr(art, "has_reference", False):
        _CACHED_IBB_RATES = art.ibb_rates
    # The park kernel. 0.0, the code default and the value of any unparsable
    # setting, turns it off exactly. When on, the sampler takes the
    # (venue_id, season) -> regressed run-factor map from the bundle, or from
    # DuckDB (read-only) for a bundle without the reference data. A failed
    # load leaves the map None and the kernel neutral.
    park_sigma = read_float("SIM_PARK_KERNEL_SIGMA")
    if park_sigma > 0.0:
        sampler.venue_run_factors = (
            art.venue_run_factors
            if getattr(art, "has_reference", False)
            else _load_venue_run_factors()
        )
        if sampler.venue_run_factors:
            sampler.park_sigma = park_sigma
    # The draw-conditioning kernels: fatigue (the pitch count and the times
    # through the order) on the pitch draw, and the drawn pitch's similarity
    # on the fielding draw. 0.0, the code default, turns each off exactly.
    for env, attr in (
        ("SIM_FATIGUE_PC_SIGMA", "fatigue_pc_sigma"),
        ("SIM_FATIGUE_TTO_SIGMA", "fatigue_tto_sigma"),
        ("SIM_BB_PITCH_SIGMA", "bb_pitch_sigma"),
    ):
        setattr(sampler, attr, read_float(env))
    # The pitch draw's situation bandwidth (2.0 = the code default).
    apply_sit_sigma_env(sampler)
    # The batting-side weight on the PITCH draw. 1.0, the code default and the
    # value of any unparsable setting, turns it off exactly; 0.0 is a hard
    # match on the side, as the owner ruled for the fielding draw.
    sampler.pitch_home_off_weight = read_float("SIM_PITCH_HOME_OFF_WEIGHT")
    # The pitch-draw cell index. OFF, the code default, weights the whole
    # pool; ON hard-filters each plate appearance to its (runners, outs, score
    # band, side) cell, and widens a cell that holds fewer than
    # SIM_PITCH_MIN_CELL rows.
    sampler.pitch_cell_index = read_bool("SIM_PITCH_CELL_INDEX")
    sampler.pitch_min_cell = read_int("SIM_PITCH_MIN_CELL")
    apply_actor_matrix_env(sampler)
    apply_result_split_env(sampler)
    apply_fielding_env(sampler)
    apply_manager_env(sampler)
    apply_running_game_env(sampler)
    _CACHED_FULL_POOL_SAMPLER = sampler
    _CACHED_FULL_POOL_ART_DIR = art_dir
    return sampler


#: The per-worker cache of the IBB rate table (``sim.ibb_rates``, about 48
#: rows). The sampler build fills it from a bundle with the reference data.
#: False = not yet tried; None = tried and unavailable.
_CACHED_IBB_RATES: dict[tuple[int, int, bool, bool], float] | None | bool = False


def _load_ibb_rates() -> dict[tuple[int, int, bool, bool], float] | None:
    """Return the measured IBB rate per cell, cached per worker.

    The rates come from the bundle when the sampler build cached them, else
    from ``sim.ibb_rates``, read-only. Returns None on any failure (no duckdb,
    a missing file, a database before migration 0020, an empty table); then no
    intentional walk fires. There is no formula to fall back to."""
    global _CACHED_IBB_RATES
    if _CACHED_IBB_RATES is not False:
        return _CACHED_IBB_RATES  # type: ignore[return-value]
    out: dict[tuple[int, int, bool, bool], float] | None
    try:
        from pipeline.batch.engine_artifacts import ibb_rates_from_rows
        from simulation.sim_kwargs import open_sim_duckdb

        con = open_sim_duckdb()
        if con is None:
            # Not cached: a rebuild that holds the lock is temporary, so the next
            # machine build tries again instead of losing the IBB for good.
            return None
        try:
            rows = con.execute(
                "SELECT runners_state, outs, is_late, is_close, opportunities, issued "
                "FROM sim.ibb_rates WHERE opportunities > 0"
            ).fetchall()
        finally:
            con.close()
        out = ibb_rates_from_rows(rows)
    except Exception:
        out = None
    _CACHED_IBB_RATES = out
    return out


def _load_venue_run_factors() -> dict[tuple[int, int], float] | None:
    """Load the (venue_id, season) -> regressed run park-factor map from
    DuckDB, read-only. Returns None on any failure (no duckdb, a missing file,
    an empty table); the park kernel then stays neutral. The sampler build
    calls it once per worker, and only for a bundle without the reference
    data."""
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
        from pipeline.batch.engine_artifacts import venue_run_factors_from_rows

        return venue_run_factors_from_rows(rows)
    except Exception:
        return None


def reset_caches() -> None:
    """Clear THIS process's cached full-pool sampler and IBB rates.

    The cache lives at module scope, so it lasts for the life of the worker
    process (as intended) AND across tests in the same process. A test that
    runs the full-pool path calls this in setup or teardown, so one test's
    cached sampler cannot leak into the next. Production never calls it: a
    worker builds its cache once and keeps it.
    """
    global _CACHED_FULL_POOL_SAMPLER, _CACHED_FULL_POOL_ART_DIR, _CACHED_IBB_RATES
    _CACHED_FULL_POOL_SAMPLER = None
    _CACHED_FULL_POOL_ART_DIR = None
    _CACHED_IBB_RATES = False  # back to "not yet tried"


def _warm_sampler(sampler: Any) -> None:
    """Run the sampler's lazy one-time precomputes, so a prewarmed worker is
    FULLY hot.

    ``FullPoolSampler.__init__`` is cheap: it defers the expensive ``O(pool)``
    index builds to first use. The pitch pool's ``_pool_meta(hand)`` fires on
    the first ``new_half_inning``, the batted-ball ``_bb_pool_bat_idx(hand)``
    on the first ``battedball_new_pa``, and the steal pool's
    ``_steal_meta(target)`` on its first use. A sampler built alone leaves all
    three for the worker's FIRST real game. This function runs them for every
    key of each pool map (a batter hand, or a target base for the steal
    pools), so the first game runs at the usual per-game cost. It only fills
    caches and touches no rng or matchup state, so it cannot change a later
    game. A partial bundle or a missing attribute leaves that part to warm
    lazily.
    """
    art = getattr(sampler, "a", None)
    for pools_attr, method_name in (
        ("pools", "_pool_meta"),
        ("bb_pools", "_bb_pool_bat_idx"),
        # The steal pool's cell indexes and embedding-row gathers, per target base.
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
    """Fill THIS process's full-pool sampler cache up front.

    Builds the full-pool sampler and runs its lazy precomputes
    (:func:`_warm_sampler`) into the per-process cache that
    :func:`production_machine_factory` reads. The FIRST real ``/simulate`` game
    on this process is then a warm cache hit: it does not pay the bundle load
    and the index builds on the request path.
    :meth:`simulation.batch_runner.BatchRunner.prewarm` runs this on every pool
    worker at startup. Without it, a fresh pool gives each worker one game of
    an n>1 request, and the per-worker cache, which pays off only from a
    worker's second game, never warms in time.

    Returns ``True`` when a full-pool sampler is now cached, else ``False`` (a
    missing or corrupt bundle: the request path then raises at machine build).
    Never raises: any build failure returns ``False``.
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
# The manager wiring (gated by SIM_MANAGER; default OFF)
# ---------------------------------------------------------------------------
#
# With SIM_MANAGER off (the unit-test default), the factory wires NO manager
# onto the StateMachine, so every manager hook returns at once. With it on
# (production), the factory attaches the manager marker and stages a
# placeholder pen per team on the machine.

#: The machine's manager gate. ``StateMachine.manager`` is ``None`` (every
#: manager hook does nothing) or this marker (the hooks run). The marker holds
#: no rates: the pitching change is a draw from the change pool, and the steal
#: weight reads each side's real profile and the league mean off the game state.
_DEFAULT_MANAGER_PROFILE: dict[str, float] = {}


def _manager_enabled() -> bool:
    """Whether ``SIM_MANAGER`` turns the manager wiring on (default OFF).

    Unset, ``0``, ``false``, ``no`` or ``off`` turns it off.
    """
    return read_bool("SIM_MANAGER")


def _manager_draw_enabled() -> bool:
    """Whether ``SIM_MANAGER_DRAW`` makes the pitching change a draw from the
    change opportunity pool. Production runs it ON (the real manager and pen,
    SIM-427); OFF means no pitching change, the unit lane's state."""
    return read_bool("SIM_MANAGER_DRAW")


def apply_manager_env(sampler: Any, env: Mapping[str, str] | None = None) -> None:
    """Read the pitching-change draw's bandwidth (``SIM_CHANGE_SIT_SIGMA``,
    1.0), the live pitcher's power (``SIM_CHANGE_PITCHER_POWER``, 1.0; 0 =
    off), the cell floor (``SIM_CHANGE_MIN_CELL``, 20) and the reliever draw's
    weights."""
    sampler.change_sit_sigma = read_float("SIM_CHANGE_SIT_SIGMA", env)
    sampler.change_pitcher_power = read_float("SIM_CHANGE_PITCHER_POWER", env)
    sampler.change_min_cell = read_int("SIM_CHANGE_MIN_CELL", env)
    # The reliever draw's weights (every one off at its code default).
    for key, attr in (
        ("SIM_RELIEF_ROLE_SIGMA", "relief_role_sigma"),
        ("SIM_RELIEF_PITCHER_POWER", "relief_pitcher_power"),
        ("SIM_RELIEF_REST_SIGMA", "relief_rest_sigma"),
        ("SIM_RELIEF_PITCHED2D_OFF_WEIGHT", "relief_pitched2d_off_weight"),
        ("SIM_RELIEF_PITCHES3D_SIGMA", "relief_pitches3d_sigma"),
        ("SIM_RELIEF_HAND_OFF_WEIGHT", "relief_hand_off_weight"),
    ):
        setattr(sampler, attr, read_float(key, env))


def _default_bullpen_for_spec(spec: GameSpec) -> dict[int, list[int]]:
    """Build a placeholder pen per team, keyed by the ``Team`` int value.

    Each side gets six synthetic arms from a deterministic id offset, enough
    for a full game of changes, so a pulled starter gives way to a distinct
    arm. The keys are the int Team values (0 == AWAY, 1 == HOME), so the map
    survives pickling through the GameSpec without importing the Team enum
    here. ``simulate_game`` uses this pen only when the request passes no pen
    of its own.
    """
    away_starter = int(_kwarg(spec, "away_pitcher_id", 0) or 0)
    home_starter = int(_kwarg(spec, "home_pitcher_id", 0) or 0)
    # Synthetic, deterministic ids. They are negative, so they never match a
    # real player id in the pool, and a placeholder arm draws as a
    # league-average arm.
    base = _PLACEHOLDER_ID_BASE
    away_pen = [-(base + away_starter * 10 + i) for i in range(1, 7)]
    home_pen = [-(base + home_starter * 10 + i) for i in range(1, 7)]
    return {0: away_pen, 1: home_pen}


#: The id base of the placeholder pen: arm ``i`` (1-6) of the side whose starter
#: is ``s`` gets the id ``-(base + s * 10 + i)``.
_PLACEHOLDER_ID_BASE = 9_000_000


def placeholder_reliever_number(player_id: int) -> int | None:
    """The 1-6 number of a placeholder arm, read from its id; None for a real id.

    The game page names a placeholder arm "Generic reliever N".
    """
    offset = -int(player_id) - _PLACEHOLDER_ID_BASE
    if offset < 1:  # a real id, or a negative id below the placeholder range
        return None
    n = offset % 10
    return n if 1 <= n <= 6 else None


#: Injectable bullpen builder (the no-DB test seam).  A test swaps this to assert
#: the factory wires a known bullpen.
_BULLPEN_BUILDER: Callable[[GameSpec], dict[int, list[int]]] = _default_bullpen_for_spec


def set_bullpen_builder(
    builder: Callable[[GameSpec], dict[int, list[int]]] | None,
) -> Callable[[GameSpec], dict[int, list[int]]]:
    """Install ``builder`` as the active bullpen builder; return the previous one.

    ``None`` restores :func:`_default_bullpen_for_spec`."""
    global _BULLPEN_BUILDER
    prev = _BULLPEN_BUILDER
    _BULLPEN_BUILDER = builder if builder is not None else _default_bullpen_for_spec
    return prev


def production_machine_factory(seed: int | None, spec: GameSpec) -> StateMachine:
    """Build the REAL :class:`StateMachine` for a worker.

    The machine carries the full-pool sampler over the engine-artifact bundle
    (cached per process), the manager wiring when ``SIM_MANAGER`` is on, and
    the IBB rate table. It shares the ``factory(seed, spec) -> StateMachine``
    contract with :func:`simulation.batch_runner.rng_driven_machine_factory`,
    so it drops into the same :class:`GameSpec.machine_factory` seam. Its
    dotted path is ``"simulation.production_factory:production_machine_factory"``.

    ``simulate_game`` re-seeds the machine's loop rng AND the sampler's rng
    from the per-game ``seed``, so determinism holds end to end, whatever seed
    the factory used here.
    """
    full_pool = _build_full_pool_sampler(spec, seed)
    # The point-in-time cutoff. ``build_sim_kwargs`` resolves it to the day
    # before the game and passes it as a factory-only key. Without it (a live
    # request, a test, any caller that does not set it), the sampler draws
    # from the whole pool.
    #
    # ``full_pool`` is a per-worker singleton, cached across every game this
    # worker builds (see ``_build_full_pool_sampler``). So this call always
    # runs, and passes ``None`` when the request carries no cutoff. Skipping
    # it would leave the previous game's cutoff in place: a backtest game
    # whose own cutoff failed to resolve would inherit an unrelated game's
    # cutoff (found in review of the backtest cutoff, SIM-538). ``set_asof``
    # accepts ``None`` and clears the cutoff.
    if full_pool is not None:
        asof = (spec.sim_kwargs or {}).get("_asof_ymd")
        full_pool.set_asof(int(asof) if asof is not None else None)
    # The gated manager wiring. With SIM_MANAGER off, ``manager`` stays None and
    # every manager hook does nothing. With it on, the factory attaches the
    # manager marker and stages the placeholder pen on the machine;
    # ``simulate_game`` seeds that pen onto the GameState unless its own
    # ``bullpen=`` is set.
    manager = _DEFAULT_MANAGER_PROFILE if _manager_enabled() else None
    machine = StateMachine(
        full_pool,
        rng=np.random.default_rng(seed),
        manager=manager,
        # The measured IBB rate table. None with no manager (the hook does
        # nothing then) and when the table does not load (no IBB fires).
        ibb_rates=_load_ibb_rates() if manager is not None else None,
    )
    if manager is not None:
        # Stage the placeholder pen on the machine. ``simulate_game`` falls back
        # to ``machine.bullpen`` when no ``bullpen=`` is passed.
        machine.bullpen = _BULLPEN_BUILDER(spec)
        # The pitching change as a draw (SIM_MANAGER_DRAW).
        machine.manager_draw = _manager_draw_enabled()
    return machine


__all__ = [
    "placeholder_reliever_number",
    "production_machine_factory",
    "set_bullpen_builder",
    "warm_worker_cache",
    "reset_caches",
]
