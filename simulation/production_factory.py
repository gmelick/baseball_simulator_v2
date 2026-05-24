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

from typing import Any, Callable

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


def _default_sampler_builder(spec: GameSpec, seed: "int | None") -> PlayPoolSampler:
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
    """Default fingerprint-deriver builder: ``None`` (use the stub fingerprints).

    The real SIM-317 :class:`~simulation.fingerprints.FingerprintDeriver` needs the
    engine registry + a live MatchupProfileProvider; returning ``None`` lets the
    :class:`StateMachine` fall back to its built-in deterministic-hash fingerprints
    so the factory runs without the full engine stack.  A production deployment that
    wants the real geometry installs its own builder via :func:`set_deriver_builder`.
    """
    return None


#: The active sampler builder (swap via :func:`set_sampler_builder`).
_SAMPLER_BUILDER: SamplerBuilder = _default_sampler_builder
#: The active deriver builder (swap via :func:`set_deriver_builder`).
_DERIVER_BUILDER: DeriverBuilder = _default_deriver_builder


def set_sampler_builder(builder: "SamplerBuilder | None") -> SamplerBuilder:
    """Install ``builder`` as the active sampler builder; return the previous one.

    ``None`` restores the production default (:func:`_default_sampler_builder`).
    Tests use this (or :func:`use_sampler_builder`) to inject an in-memory / mock
    sampler so the factory is exercised with no live DuckDB / FAISS.
    """
    global _SAMPLER_BUILDER
    prev = _SAMPLER_BUILDER
    _SAMPLER_BUILDER = builder if builder is not None else _default_sampler_builder
    return prev


def set_deriver_builder(builder: "DeriverBuilder | None") -> DeriverBuilder:
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

    def __init__(self, builder: "SamplerBuilder | None") -> None:
        self._builder = builder
        self._prev: "SamplerBuilder | None" = None

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


def production_machine_factory(seed: "int | None", spec: GameSpec) -> StateMachine:
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

    sampler = _SAMPLER_BUILDER(spec, seed)
    if spec.shared_segments:
        _attach_shared_tiles(sampler, spec)

    deriver = _DERIVER_BUILDER(spec)

    return StateMachine(
        sampler=sampler,
        k=k,
        rng=np.random.default_rng(seed),
        fingerprint_deriver=deriver,
    )


__all__ = [
    "production_machine_factory",
    "set_sampler_builder",
    "set_deriver_builder",
    "use_sampler_builder",
    "SamplerBuilder",
    "DeriverBuilder",
]
