"""
batch_runner.py
===============
SIM-332 -- the **parallel 100-iteration Monte-Carlo batch runner** (Phase 4,
Sprint 3).

WHAT THIS IS
------------
:func:`simulation.sim_loop.simulate_game` (SIM-320) plays ONE game; a Monte-Carlo
run plays the *same* matchup N times (default 100) and aggregates the N
per-game :class:`~simulation.results.GameSimResult`s into a single
:class:`~simulation.results.GameSimSummary` (SIM-327).  This module is the
fan-out / fan-in that does that, honoring the locked SIM-281 parallelism ADR
(``docs/architecture/2026-05-27-parallelism.md``) and the SIM-119 / SIM-280
2 s-game / 30 s-batch / <=2 GB budgets.

THE LOCKED DESIGN THIS HONORS
-----------------------------
  * **Concurrency backend (SIM-281 D1).**  ``concurrent.futures.ProcessPoolExecutor``
    with ``max_workers = min(os.cpu_count() - 1, 10)`` -- the explicit 10-worker
    ceiling keeps ``total(W) = 290 MB + W x 165 MB`` under the 2 GB cap on
    >=16-core hosts (SIM-280 §4).  See :func:`default_max_workers`.
  * **Per-game seed isolation (spec §6.3).**  Each iteration ``i`` gets a distinct
    deterministic seed ``derive_seed(base_seed, i)`` so (a) a fixed ``base_seed``
    reproduces the WHOLE batch (every per-game seed is a pure function of the
    base) and (b) each game still differs (distinct per-iteration seeds).  See
    :func:`derive_seed`.
  * **Pickle-safe workers (SIM-281 D1 "no impedance mismatch").**  A worker
    NEVER receives a live sampler / resolver / StateMachine across the process
    boundary (those wrap DuckDB / FAISS / rng state and are not picklable).  It
    receives only a small, fully-picklable :class:`GameSpec` (a base seed + the
    ``simulate_game`` kwargs + a dotted reference to a *factory* that builds the
    per-worker machine) and BUILDS its own machine/sampler IN-PROCESS via
    :func:`_run_one`.  This is the same dependency-injection seam SIM-319/320 use
    for the no-DB test path, lifted across the process boundary.
  * **Redis TTL caching (SIM-113 spirit).**  Sim summaries cache at 60 s TTL,
    pool queries at 5 min (300 s) TTL, behind the small :class:`SimCache`
    interface.  Redis is OPTIONAL: :func:`make_cache` returns a real
    :class:`RedisCache` only when a live server answers, else an in-process
    :class:`InMemoryCache` (LRU + TTL) or a :class:`NullCache` no-op.  The
    sandbox has the ``redis`` client lib but NO server, so tests run on the
    in-memory fallback with zero external dependencies.

SIM-333 -- shared-memory zero-copy ATTACH (IMPLEMENTED)
-------------------------------------------------------
SIM-281 D2/D3 publish the ~290 MB read-only payload into named
``multiprocessing.shared_memory`` segments and have each worker ATTACH (zero-copy)
rather than copy.  THAT attach is **SIM-333** and is implemented here:

  * :func:`publish_shared_arrays` (parent-side) copies each read-only numpy buffer
    (situation-KDTree data / RBF matrices / FAISS-tile-backing arrays) ONCE into a
    named ``SharedMemory`` segment and returns a picklable
    ``{name: SharedArrayDescriptor}`` registry + the owned segment handles;
  * :meth:`BatchRunner._pool_kwargs` fills the pool's
    ``initializer=_worker_init`` / ``initargs=(registry,)`` so each worker attaches
    the shared segments ONCE at startup;
  * :func:`_worker_init` ATTACHES each segment by name and reconstructs a
    zero-copy ``np.ndarray`` *view* over the shared buffer in the
    :data:`_WORKER_SHARED` process-global; :func:`get_shared_view` is how a
    per-worker sampler/engine factory reads those views (and rebuilds the thin
    FAISS/KDTree header over them) instead of re-loading tiles itself;
  * lifecycle: the PARENT owns create + ``unlink`` (:meth:`BatchRunner.close` /
    :func:`unlink_shared_segments`); workers attach + ``close`` but NEVER
    ``unlink``.
  * :class:`GameSpec` still carries the opaque ``shared_segments`` field for a spec
    that wants to name which segments its factory should attach.

BACKWARD-COMPATIBLE: with no ``shared_arrays`` supplied, the registry is empty and
the runner falls back to the SIM-332 per-process load path, so the always-on tests
(which use the injected picklable no-DB factory) stay green.
"""

from __future__ import annotations

import importlib
import os
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from multiprocessing import shared_memory
from typing import Any

import numpy as np

from simulation.results import GameSimResult, GameSimSummary
from simulation.sim_loop import simulate_game

# ---------------------------------------------------------------------------
# TTL constants (SIM-113 caching spirit)
# ---------------------------------------------------------------------------

#: Sim-result (summary) cache TTL, in seconds: a freshly-computed summary stays
#: valid for one minute (SIM-332 AC #4).  Short because a re-run with the SAME
#: (spec + base seed + N) is deterministic, so the cache is purely a recompute
#: dodge for a hot key, not a correctness store.
SIM_RESULT_TTL_S = 60
#: Pool-query cache TTL, in seconds: the read-only play-pool / engine query
#: results live 5 minutes (they change only on the nightly build).
POOL_QUERY_TTL_S = 300

#: The hard worker ceiling from SIM-281 D1 (keeps total RAM under the 2 GB cap on
#: >=16-core hosts; SIM-280 §4: 16 workers ~= 2.86 GB breaches).
MAX_WORKER_CEILING = 10


def default_max_workers(cpu_count: int | None = None) -> int:
    """The SIM-281 D1 worker count: ``min(os.cpu_count() - 1, 10)``, floored at 1.

    ``cpu_count`` is injectable for deterministic tests; defaults to
    ``os.cpu_count()`` (treated as 1 if the OS reports ``None``).  The floor of 1
    guarantees a usable pool on a single-core host; the ceiling of
    :data:`MAX_WORKER_CEILING` honors the 2 GB cap.
    """
    cpu = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    return max(1, min(int(cpu) - 1, MAX_WORKER_CEILING))


def derive_seed(base_seed: int | None, i: int) -> int | None:
    """Derive iteration ``i``'s per-game seed from a ``base_seed`` (spec §6.3).

    Returns ``base_seed + i`` (a distinct, deterministic seed per iteration) when
    a base is given, so a fixed ``base_seed`` makes the WHOLE batch reproducible
    AND every game differs.  Returns ``None`` (a nondeterministic per-game seed)
    when ``base_seed`` is ``None`` -- the "I do not care about reproducibility"
    path.  The addition is the documented derivation; a hash-mix could be swapped
    in later without changing the contract (still a pure function of base + i).
    """
    if base_seed is None:
        return None
    return int(base_seed) + int(i)


# ---------------------------------------------------------------------------
# The picklable GAME SPEC — what crosses the process boundary (SIM-281 D1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GameSpec:
    """A fully-picklable description of ONE matchup to simulate (the worker input).

    This is the ONLY thing that crosses the ``ProcessPoolExecutor`` boundary per
    task.  It carries scalars + a *dotted reference* to a module-level factory
    that BUILDS the per-worker :class:`~simulation.sim_loop.StateMachine` (and, in
    production, its sampler over the SIM-333 shared tiles) -- never a live object.

    Fields:
      * ``machine_factory`` -- ``"pkg.mod:callable"`` resolving to a module-level
        ``factory(seed: int | None, spec: GameSpec) -> StateMachine | None``.
        ``None`` means "let ``simulate_game`` build a default machine from
        ``sim_kwargs``".  A factory is REQUIRED for the no-sampler path (the
        machine then needs an outcome source); the always-on tests pass a
        picklable rng-driven factory.
      * ``sim_kwargs`` -- extra picklable keyword args forwarded to
        :func:`simulate_game` (e.g. ``pitcher_id`` / ``bat_hand`` / ``season`` /
        the two lineups / ``k`` / ``max_innings``).  MUST be picklable (no live
        sampler/resolver).

        **FACTORY-ONLY KEYS (SIM-377 convention).**  A key whose name starts with
        an underscore (e.g. ``_hit_rate``) is read by the ``machine_factory`` to
        tune the machine it builds, but is **NOT** a :func:`simulate_game`
        parameter.  :func:`_run_one` therefore filters every ``_``-prefixed key
        out of the splat into ``simulate_game(**...)``, so a factory may stash its
        own knobs here without raising a ``TypeError`` against ``simulate_game``'s
        fixed signature.  Non-underscore keys are passed through verbatim.
      * ``shared_segments`` -- SIM-333 seam: the ``{name: (shape, dtype)}`` shared
        ``multiprocessing.shared_memory`` registry the worker factory attaches to
        build a zero-copy sampler.  ``None`` until SIM-333.
    """

    machine_factory: str | None = None
    sim_kwargs: dict[str, Any] = field(default_factory=dict)
    shared_segments: dict[str, Any] | None = None

    def cache_key_fields(self) -> tuple:
        """The picklable, hashable identity of this spec for the cache key.

        Excludes ``shared_segments`` (an attach detail, NOT a determinant of the
        result) and sorts ``sim_kwargs`` so the key is stable across dict order.
        """
        kw = tuple(sorted((k, _hashable(v)) for k, v in self.sim_kwargs.items()))
        return (self.machine_factory, kw)


def _hashable(value: Any) -> Any:
    """Coerce a sim-kwarg value into a hashable form for the cache key."""
    if isinstance(value, list | tuple):
        return tuple(_hashable(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    return value


def _resolve_dotted(ref: str) -> Callable:
    """Resolve a ``"pkg.module:callable"`` dotted reference to the callable.

    Used by the worker to rebuild its machine factory from the picklable string
    on :class:`GameSpec` (a string crosses the process boundary cleanly; a bound
    method or closure would not).
    """
    if ":" not in ref:
        raise ValueError(f"machine_factory ref {ref!r} must be 'module.path:callable'.")
    mod_path, _, attr = ref.partition(":")
    module = importlib.import_module(mod_path)
    factory = getattr(module, attr)
    if not callable(factory):
        raise TypeError(f"machine_factory {ref!r} resolved to a non-callable.")
    return factory


# ===========================================================================
# SIM-333 — shared-memory zero-copy attach for the read-only payload
# ===========================================================================
#
# WHAT IS SHARED (and why ≤2 GB holds)
# ------------------------------------
# The read-only payload SIM-280 §4 quantifies at ~290 MB is, by *byte volume*,
# almost entirely raw numpy buffers: the situation KDTree's ``N×11`` float64 data
# (~88 MB), the RBF/GMM stacked partition matrices (~18 MB), and the FAISS tiles'
# backing float32 vector blocks + int64 rowids (~64 MB resident under the LRU cap).
# THOSE BUFFERS are what we publish into named ``multiprocessing.shared_memory``
# segments ONCE in the parent; every worker attaches them by name and rebuilds a
# *view* (``np.ndarray(shape, dtype, buffer=shm.buf)``) with NO copy.  So the
# bulky bytes are resident exactly once, and the budget stays
# ``290 MB (shared, flat in W) + W × ~165 MB private`` -- the whole point of
# SIM-281 D2.
#
# WHAT STAYS PER-WORKER (and why that's still small)
# --------------------------------------------------
# A live ``faiss.IndexFlatL2`` / ``scipy.spatial.KDTree`` *header* (the small node
# / quantizer wrapper around the big buffer) is NOT itself a numpy buffer and is
# not zero-copyable through ``shared_memory``.  Each worker reconstructs that thin
# header OVER the shared buffer.  Per SIM-281 D2.2 / SIM-280 §2.1 the header is
# tiny relative to its data (the KDTree nodes are small vs the 88 MB data; a flat
# FAISS index header is ~45 B + the python object shell), so the per-worker header
# duplication is in the noise -- the 88 MB / 64 MB data buffers behind them are the
# part that must not be duplicated, and they are not.  The arsenal cache (SIM-280
# §2.2) stays per-worker LAZY (~15 MB) -- never exhaustively precomputed -- which is
# already counted in the ~165 MB private overhead.  Net: total stays ≤2 GB to
# ~8--10 workers exactly as SIM-280 §4 certified.
#
# LIFECYCLE (parent owns create/unlink; workers attach/close, never unlink)
# ------------------------------------------------------------------------
# The PARENT creates the ``SharedMemory`` segments and is the sole owner: it (and
# only it) calls ``unlink()`` exactly once at shutdown.  Each worker ATTACHES the
# existing segment by name and ``close()``s its own handle when done; a worker MUST
# NOT ``unlink()`` (that would free memory still mapped by other workers).  The
# registry passed across the fork is a picklable ``{name: (shape, dtype)}`` map --
# names + descriptors only, never the buffers themselves.


#: Picklable per-segment descriptor: enough to reconstruct a zero-copy numpy view
#: over an attached :class:`multiprocessing.shared_memory.SharedMemory` block.  A
#: registry is ``{logical_name: SharedArrayDescriptor}`` and is what crosses the
#: process boundary via the pool ``initargs`` (names + shapes + dtypes only -- the
#: bytes stay in the OS-backed segment, never pickled).
@dataclass(frozen=True)
class SharedArrayDescriptor:
    """How to attach + view ONE shared-memory-backed read-only numpy array.

    Fields:
      * ``shm_name`` -- the OS name of the ``SharedMemory`` segment (picklable;
        the worker attaches by this name).
      * ``shape`` -- the array shape to reconstruct the view with.
      * ``dtype`` -- the numpy dtype *string* (e.g. ``"float64"``) -- picklable and
        round-trips through ``np.dtype(...)`` cleanly.
    """

    shm_name: str
    shape: tuple[int, ...]
    dtype: str


def publish_shared_arrays(
    arrays: dict[str, np.ndarray],
) -> tuple[dict[str, SharedArrayDescriptor], list[shared_memory.SharedMemory]]:
    """Parent-side: copy each read-only array ONCE into a named ``SharedMemory``
    segment and return ``(registry, owned_segments)``.

    The ``registry`` (``{name: SharedArrayDescriptor}``) is the picklable map handed
    to workers via :meth:`BatchRunner` / :func:`_worker_init`; ``owned_segments`` is
    the list of live ``SharedMemory`` handles the **parent must keep alive** for the
    pool's lifetime and ``unlink()`` exactly once at shutdown (see
    :func:`unlink_shared_segments`).  The one-time copy here is the ONLY copy of the
    bytes -- workers attach the same physical segment zero-copy.
    """
    registry: dict[str, SharedArrayDescriptor] = {}
    owned: list[shared_memory.SharedMemory] = []
    for name, arr in arrays.items():
        a = np.ascontiguousarray(arr)
        nbytes = max(int(a.nbytes), 1)  # SharedMemory requires size >= 1
        shm = shared_memory.SharedMemory(create=True, size=nbytes)
        view: np.ndarray = np.ndarray(a.shape, dtype=a.dtype, buffer=shm.buf)
        view[...] = a  # the single copy into shared memory
        owned.append(shm)
        registry[name] = SharedArrayDescriptor(
            shm_name=shm.name, shape=tuple(a.shape), dtype=str(a.dtype)
        )
    return registry, owned


def unlink_shared_segments(
    segments: list[shared_memory.SharedMemory],
) -> None:
    """Parent-side teardown: ``close()`` then ``unlink()`` every owned segment
    exactly once (idempotent / never raises).

    ONLY the parent calls this; a worker must never ``unlink()`` (it would free
    memory other workers still map).  Swallows the "already unlinked / missing"
    errors so a double-teardown is safe.
    """
    for shm in segments:
        try:
            shm.close()
        except Exception:  # pragma: no cover -- defensive
            pass
        try:
            shm.unlink()
        except FileNotFoundError:  # pragma: no cover -- already gone
            pass
        except Exception:  # pragma: no cover -- defensive
            pass


# ---------------------------------------------------------------------------
# Pool initializer — the shared-memory ATTACH (SIM-333) happens here
# ---------------------------------------------------------------------------

#: Process-global the worker initializer populates with the ATTACHED shared-memory
#: state.  ``"views"`` maps logical name -> a zero-copy ``np.ndarray`` view over the
#: shared buffer; ``"_handles"`` holds the live ``SharedMemory`` handles the worker
#: keeps open (and ``close()``s at exit -- but NEVER ``unlink()``s).  Empty when no
#: registry is supplied (the SIM-332 per-process fallback).
_WORKER_SHARED: dict[str, Any] = {"views": {}, "_handles": []}


def _worker_init(shared_registry: dict[str, SharedArrayDescriptor] | None = None) -> None:
    """Pool ``initializer`` (runs ONCE per worker at startup) -- the SIM-333 attach.

    SIM-281 D2 / §"Worker startup sequence": each worker ATTACHES the parent's named
    ``multiprocessing.shared_memory`` segments here (zero-copy) and reconstructs a
    numpy *view* per segment over the shared buffer, so the ~290 MB read-only
    payload is resident ONCE rather than ``W x``.  The views land in the
    :data:`_WORKER_SHARED` process-global where the per-worker sampler factory reads
    them (see :func:`get_shared_view`).  A worker attaches + ``close()``s its own
    handle but NEVER ``unlink()``s -- the parent owns that (SIM-281 lifecycle).

    Backward-compatible: a ``None`` / empty registry leaves the global empty, so the
    SIM-332 per-process load path is unaffected (the always-on tests stay green).
    """
    # Reset (a re-used worker process must not carry stale views).
    _WORKER_SHARED["views"] = {}
    _WORKER_SHARED["_handles"] = []
    if not shared_registry:
        return
    for name, desc in shared_registry.items():
        shm = shared_memory.SharedMemory(name=desc.shm_name)  # ATTACH (no create)
        view: np.ndarray = np.ndarray(tuple(desc.shape), dtype=np.dtype(desc.dtype), buffer=shm.buf)
        view.setflags(write=False)  # read-only contract (no CoW re-duplication)
        _WORKER_SHARED["views"][name] = view
        _WORKER_SHARED["_handles"].append(shm)


def get_shared_view(name: str) -> np.ndarray | None:
    """Worker-side accessor: the zero-copy view attached by :func:`_worker_init`
    for ``name``, or ``None`` if no such shared segment was published.

    A per-worker sampler / engine factory calls this to build its read-only
    structures OVER the shared buffer instead of re-loading from disk.  ``None`` is
    the signal to fall back to the SIM-332 per-process disk load (backward-compat).
    """
    return _WORKER_SHARED.get("views", {}).get(name)


# ---------------------------------------------------------------------------
# The per-iteration worker — runs IN-PROCESS, builds its own objects
# ---------------------------------------------------------------------------


def _run_one(spec: GameSpec, seed: int | None) -> GameSimResult:
    """Run ONE :func:`simulate_game` for ``spec`` at the derived per-game ``seed``.

    This is the function the pool maps across the N seeds.  It is module-level
    (so it pickles by reference) and rebuilds every live object IN the worker:

      * if ``spec.machine_factory`` is set, resolve it and call
        ``factory(seed, spec)`` to build a fresh :class:`StateMachine` (the
        production factory builds a sampler over the SIM-333 shared tiles; the
        test factory builds an rng-driven no-DB machine);
      * otherwise let :func:`simulate_game` build a default machine from
        ``spec.sim_kwargs``.

    The ``seed`` is threaded into BOTH the factory AND ``simulate_game(seed=...)``
    so the machine's loop rng and the sampler's k-NN rng are reproducible from the
    one per-game seed (spec §6.3).

    SIM-377: ``sim_kwargs`` keys are split by the underscore-prefix convention
    (see :class:`GameSpec`).  The factory sees the WHOLE ``spec`` (so it can read
    its own ``_``-prefixed knobs like ``_hit_rate``), but only the NON-underscore
    keys are splatted into :func:`simulate_game` -- which has a fixed signature and
    no ``**kwargs``, so a stray factory-only key would otherwise raise
    ``TypeError``.
    """
    machine = None
    if spec.machine_factory is not None:
        factory = _resolve_dotted(spec.machine_factory)
        machine = factory(seed, spec)
    # SIM-377: drop factory-only (``_``-prefixed) keys before the splat so they
    # never reach ``simulate_game``'s fixed signature.  The factory already saw
    # them via ``spec`` above.
    passthrough = {k: v for k, v in spec.sim_kwargs.items() if not k.startswith("_")}
    return simulate_game(machine, seed=seed, **passthrough)


# ===========================================================================
# Cache interface + fallbacks (Redis optional)
# ===========================================================================


class SimCache:
    """The tiny cache interface the runner depends on (Redis is one impl).

    Three methods only: ``get`` / ``set`` (with a TTL) / ``clear``.  Keeping the
    surface this small is what lets Redis be OPTIONAL -- the runner never imports
    ``redis`` directly; it depends on this interface and :func:`make_cache`
    chooses a backend (Redis if reachable, else in-memory, else no-op).
    """

    def get(self, key: str) -> Any | None:
        raise NotImplementedError

    def set(self, key: str, value: Any, ttl_s: int) -> None:
        raise NotImplementedError

    def clear(self) -> None:
        raise NotImplementedError


class NullCache(SimCache):
    """A no-op cache: every ``get`` misses, every ``set`` is dropped.

    The safe default when caching is undesired or no backend is available; the
    runner stays correct (it just recomputes), so a missing Redis never breaks a
    batch.
    """

    def get(self, key: str) -> Any | None:
        return None

    def set(self, key: str, value: Any, ttl_s: int) -> None:
        return None

    def clear(self) -> None:
        return None


class InMemoryCache(SimCache):
    """A process-local TTL cache (the Redis fallback for the sandbox / tests).

    Stores ``(value, expires_at)`` in a dict and honors the per-entry TTL with a
    monotonic clock.  A small ``maxsize`` (LRU-ish: oldest-inserted evicted)
    bounds memory so a long-lived runner cannot grow without limit.  No external
    server -- this is what the SIM-332 tests memoize against (the sandbox has the
    ``redis`` client but NO server).
    """

    def __init__(self, maxsize: int = 256, *, clock: Callable[[], float] | None = None):
        self._store: dict[str, tuple[Any, float]] = {}
        self._maxsize = int(maxsize)
        self._clock = clock or time.monotonic

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if self._clock() >= expires_at:
            # Expired -> evict + miss.
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl_s: int) -> None:
        if key in self._store:
            # Refresh insertion order so a re-set counts as recently used.
            self._store.pop(key, None)
        elif len(self._store) >= self._maxsize:
            # Evict the oldest-inserted entry (dicts preserve insertion order).
            oldest = next(iter(self._store))
            self._store.pop(oldest, None)
        self._store[key] = (value, self._clock() + float(ttl_s))

    def clear(self) -> None:
        self._store.clear()


class RedisCache(SimCache):
    """A Redis-backed TTL cache (the production backend).

    Wraps a live ``redis.Redis`` client; values are pickled (the summary is a
    dataclass with numpy arrays, not JSON-trivial).  Constructed only by
    :func:`make_cache` AFTER a ``ping()`` confirms a live server, so the runner
    never hard-fails on a missing Redis.  Any per-op Redis error degrades to a
    miss / drop rather than propagating -- the batch must not die because the
    cache hiccuped.
    """

    def __init__(self, client: Any, *, prefix: str = "sim332:"):
        import pickle  # local import: only needed on the live-Redis path.

        self._client = client
        self._prefix = prefix
        self._pickle = pickle

    def _k(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def get(self, key: str) -> Any | None:
        try:
            raw = self._client.get(self._k(key))
        except Exception:
            return None
        if raw is None:
            return None
        try:
            return self._pickle.loads(raw)
        except Exception:
            return None

    def set(self, key: str, value: Any, ttl_s: int) -> None:
        try:
            self._client.set(self._k(key), self._pickle.dumps(value), ex=int(ttl_s))
        except Exception:
            return None

    def clear(self) -> None:
        try:
            keys = list(self._client.scan_iter(match=f"{self._prefix}*"))
            if keys:
                self._client.delete(*keys)
        except Exception:
            return None


def make_cache(
    *,
    prefer_redis: bool = True,
    redis_url: str | None = None,
    client: Any = None,
) -> SimCache:
    """Choose a cache backend: Redis if reachable, else in-memory (never raises).

    Resolution order (SIM-332 AC #4 -- "Redis MUST be optional"):
      1. ``prefer_redis`` False  -> :class:`InMemoryCache` (the explicit no-server
         path the tests use).
      2. an injected ``client`` (or a ``redis_url`` / ``REDIS_URL`` env) that
         answers ``ping()`` -> :class:`RedisCache`.
      3. anything else (``redis`` not installed, no server, ping fails) ->
         :class:`InMemoryCache`.

    The fallback is the in-memory cache (correct + fast, just process-local), so
    a batch run NEVER requires a Redis server -- exactly what the sandbox needs.
    """
    if not prefer_redis:
        return InMemoryCache()
    try:
        if client is None:
            import redis  # client lib only; may be absent.

            url = redis_url or os.environ.get("REDIS_URL")
            client = redis.Redis.from_url(url) if url else redis.Redis()
        client.ping()  # confirms a LIVE server before we trust it.
        return RedisCache(client)
    except Exception:
        # redis not installed / no server / ping failed -> graceful fallback.
        return InMemoryCache()


# ===========================================================================
# The runner — fan out across processes, fan in to a GameSimSummary (SIM-327)
# ===========================================================================


@dataclass(frozen=True)
class BatchResult:
    """The runner's return: the SIM-327 summary + run provenance.

    ``summary`` is the aggregate every downstream consumer reads; the rest is
    perf / cache provenance (how many workers, whether the summary came from the
    cache) so a caller can report honestly without re-deriving it.
    """

    summary: GameSimSummary
    n_iterations: int
    max_workers: int
    from_cache: bool
    base_seed: int | None


class BatchRunner:
    """Run N iterations of :func:`simulate_game` and aggregate to a SIM-327 summary.

    The fan-out / fan-in honoring SIM-281 D1:
      * derive N distinct per-game seeds from ``base_seed`` (:func:`derive_seed`);
      * map :func:`_run_one` across them via
        ``ProcessPoolExecutor(max_workers=min(cpu-1, 10))`` (or, with
        ``max_workers=1``, run them synchronously in-process -- the fast,
        deterministic test path);
      * aggregate the N :class:`GameSimResult`s with
        :meth:`GameSimSummary.from_results`;
      * memoize the summary in the :class:`SimCache` keyed on
        ``(spec + base_seed + N)`` for :data:`SIM_RESULT_TTL_S`.

    The SIM-333 shared-memory attach is wired behind :meth:`_pool_kwargs`
    (initializer / initargs) but inert until that ticket fills the registry.

    SIM-360 -- PERSISTENT (warm) POOL for the long-lived API
    --------------------------------------------------------
    A one-shot script forks a fresh ``ProcessPoolExecutor`` per ``run()`` and tears
    it down at the end -- fine for a CLI, but a long-lived API would then pay the
    fork + worker-startup + shared-mem publish/unlink cost on EVERY request.  When
    ``reuse_pool`` is True (the default), the runner instead creates ONE
    ``ProcessPoolExecutor`` lazily on the first pooled ``_execute`` and **reuses**
    it across subsequent ``run()`` calls; the SIM-333 shared-mem segments are
    published ONCE (:meth:`_ensure_shared_published`) and stay attached for the warm
    pool's lifetime.  The lifecycle policy:

      * **create** -- lazily, on the first ``_execute`` whose ``max_workers > 1``;
        built with ``max_workers`` + the existing :meth:`_pool_kwargs` attach seam.
      * **reuse** -- a later pooled ``run()`` with the SAME worker count reuses the
        SAME ``ProcessPoolExecutor`` instance (no new fork, no re-publish).
      * **recreate-on-worker-count-change** -- a pooled ``run()`` that resolves to a
        DIFFERENT ``max_workers`` shuts the old pool down (``wait=True``) and builds
        a fresh one sized for the new count (the shared-mem segments are NOT
        republished -- they outlive the pool and the new workers re-attach them via
        the unchanged ``_pool_kwargs``).  Documented over silently honoring a stale
        size.
      * **close** -- :meth:`close` ``shutdown(wait=True)``s the pool, drops the
        reference, and unlinks the shared segments (idempotent).  ``__exit__`` calls
        it, so a context-manager / transient runner cleans up exactly as before.

    Backward-compatible: ``reuse_pool=False`` restores the SIM-332 behaviour (a
    fresh ``with ProcessPoolExecutor(...)`` per ``_execute``, torn down at the end of
    the call).  Either way the synchronous ``max_workers <= 1`` in-process path is
    untouched (no pool is ever created), so the always-on deterministic tests are
    unaffected, and existing one-shot ``BatchRunner(...).run(...)`` callers behave
    identically (a transient pool is still cleaned up on ``close()`` / ``__exit__``).
    """

    def __init__(
        self,
        *,
        cache: SimCache | None = None,
        max_workers: int | None = None,
        confidence_level: float = 0.95,
        shared_arrays: dict[str, np.ndarray] | None = None,
        reuse_pool: bool = True,
    ) -> None:
        self.cache = cache if cache is not None else make_cache()
        # None -> compute the SIM-281 ceiling at run time; an explicit value
        # (incl. 1 for the in-process fallback) overrides it.
        self._max_workers_override = max_workers
        self.confidence_level = float(confidence_level)

        # SIM-333: the read-only arrays to publish into shared memory ONCE for the
        # pool to attach zero-copy.  ``None`` / empty -> the SIM-332 per-process
        # fallback (no shared segments; backward-compatible).  The published
        # registry + owned segments are populated lazily on the first pooled run and
        # torn down by :meth:`close`.
        self._shared_arrays = dict(shared_arrays) if shared_arrays else {}
        self._shared_registry: dict[str, SharedArrayDescriptor] = {}
        self._owned_segments: list[shared_memory.SharedMemory] = []

        # SIM-360: the persistent (warm) pool, created lazily on the first pooled
        # ``_execute`` and reused across ``run()`` calls.  ``_pool`` is the live
        # executor (None until created / after close); ``_pool_workers`` is the
        # worker count it was sized for (so a later run with a different count can
        # detect the change and recreate).  ``reuse_pool=False`` opts back into the
        # SIM-332 fresh-pool-per-call behaviour.
        self._reuse_pool = bool(reuse_pool)
        self._pool: ProcessPoolExecutor | None = None
        self._pool_workers: int | None = None

    # ---- worker-count + cache-key helpers --------------------------------

    def resolve_max_workers(self, n_iterations: int) -> int:
        """The worker count for this batch: the override, else the SIM-281 ceiling.

        Never exceeds ``n_iterations`` (no point spawning idle workers) and is
        floored at 1.
        """
        base = (
            self._max_workers_override
            if self._max_workers_override is not None
            else default_max_workers()
        )
        return max(1, min(int(base), int(n_iterations)))

    def _cache_key(self, spec: GameSpec, base_seed: int | None, n: int) -> str:
        """Key the sim cache on (game spec + base seed + N) -- SIM-332 AC #4.

        A pure function of exactly the inputs that determine the summary, so a
        repeat call with the same matchup/seed/N hits the memoized result.
        """
        return repr(("sim", spec.cache_key_fields(), base_seed, int(n)))

    def _ensure_shared_published(self) -> dict[str, SharedArrayDescriptor]:
        """Publish the read-only arrays into shared memory ONCE (idempotent).

        On the first pooled run with ``shared_arrays`` set, copy each array into a
        named ``SharedMemory`` segment (the single copy of the bytes) and remember
        the registry + owned handles.  Returns the picklable registry the workers
        attach by name.  An empty ``shared_arrays`` returns ``{}`` -> the SIM-332
        fallback (no segments, ``initargs=(None,)``).
        """
        if not self._shared_arrays:
            return {}
        if not self._shared_registry:
            self._shared_registry, self._owned_segments = publish_shared_arrays(self._shared_arrays)
        return self._shared_registry

    def _pool_kwargs(self) -> dict[str, Any]:
        """Kwargs for the ``ProcessPoolExecutor`` -- the SIM-333 attach seam (FILLED).

        Returns ``initializer=_worker_init`` with ``initargs=(registry,)`` where
        ``registry`` is the picklable ``{name: SharedArrayDescriptor}`` published by
        :meth:`_ensure_shared_published`.  Each worker attaches those named shared
        segments ONCE at startup (SIM-281 D2 worker-startup sequence) and reads the
        SAME physical memory zero-copy rather than re-loading per task.  With no
        shared arrays the registry is ``None`` -> the SIM-332 per-process fallback.
        """
        registry = self._ensure_shared_published()
        return {"initializer": _worker_init, "initargs": (registry or None,)}

    # ---- persistent (warm) pool lifecycle (SIM-360) -----------------------

    def _get_pool(self, max_workers: int) -> ProcessPoolExecutor:
        """Return the warm :class:`ProcessPoolExecutor` for this run (SIM-360).

        Creates the pool lazily on the first pooled call (sized for ``max_workers``
        with the SIM-333 attach seam from :meth:`_pool_kwargs`) and REUSES the SAME
        instance on subsequent calls.  If a later run needs a DIFFERENT worker count
        than the live pool was built for, the old pool is shut down (``wait=True``)
        and a fresh one is created for the new count -- the documented
        recreate-on-worker-count-change policy.  The shared-mem segments are NOT
        republished on a recreate (they outlive the pool; the new workers re-attach
        them via the unchanged ``_pool_kwargs``).

        Only ever called from :meth:`_execute` on the ``max_workers > 1`` path with
        ``reuse_pool`` True, so the synchronous in-process path never builds a pool.
        """
        if self._pool is not None and self._pool_workers != max_workers:
            # Worker count changed between runs -> drain + drop the stale pool and
            # rebuild at the new size.
            self._pool.shutdown(wait=True)
            self._pool = None
            self._pool_workers = None
        if self._pool is None:
            # `_pool_kwargs()` publishes the shared segments ONCE (idempotent) and
            # wires the worker initializer; the warm pool keeps those segments
            # attached for its whole lifetime.
            self._pool = ProcessPoolExecutor(max_workers=max_workers, **self._pool_kwargs())
            self._pool_workers = max_workers
        return self._pool

    # ---- shared-memory + pool lifecycle (parent owns create/unlink) -------

    @property
    def shared_registry(self) -> dict[str, SharedArrayDescriptor]:
        """The published ``{name: SharedArrayDescriptor}`` registry (publishing it
        if not yet done).  Exposed so a caller / test can round-trip + attach."""
        return self._ensure_shared_published()

    def close(self) -> None:
        """Shut the warm pool down + release the parent-owned shared segments.

        SIM-360: ``shutdown(wait=True)`` the persistent pool (if one was created)
        and drop the reference, THEN ``unlink()`` each owned shared segment exactly
        once.  Pool first, segments second, so no worker is mapping a segment when
        it is unlinked.  ONLY the parent owns unlink (SIM-281 lifecycle); workers
        attach + close but never unlink.  Idempotent -- safe to call repeatedly /
        when no pool was created and nothing was published.
        """
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None
            self._pool_workers = None
        if self._owned_segments:
            unlink_shared_segments(self._owned_segments)
            self._owned_segments = []
        self._shared_registry = {}

    def __enter__(self) -> BatchRunner:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---- the run -----------------------------------------------------------

    def run(
        self,
        spec: GameSpec,
        *,
        n_iterations: int = 100,
        base_seed: int | None = None,
        use_cache: bool = True,
    ) -> BatchResult:
        """Run the batch and return a :class:`BatchResult` (summary + provenance).

        ``n_iterations`` defaults to 100 (the SIM-281 batch size).  ``base_seed``,
        when set, makes the whole batch reproducible (each game's seed is
        :func:`derive_seed`).  ``use_cache`` consults + populates the
        :class:`SimCache` (keyed on spec + base seed + N) at
        :data:`SIM_RESULT_TTL_S`; the cache is a no-op-safe optimization, never a
        correctness requirement.
        """
        if n_iterations < 1:
            raise ValueError(f"n_iterations must be >= 1, got {n_iterations}")

        key = self._cache_key(spec, base_seed, n_iterations)
        max_workers = self.resolve_max_workers(n_iterations)

        if use_cache:
            cached = self.cache.get(key)
            if cached is not None:
                return BatchResult(
                    summary=cached,
                    n_iterations=n_iterations,
                    max_workers=max_workers,
                    from_cache=True,
                    base_seed=base_seed,
                )

        seeds = [derive_seed(base_seed, i) for i in range(n_iterations)]
        results = self._execute(spec, seeds, max_workers)

        summary = GameSimSummary.from_results(results, confidence_level=self.confidence_level)
        if use_cache:
            self.cache.set(key, summary, SIM_RESULT_TTL_S)
        return BatchResult(
            summary=summary,
            n_iterations=n_iterations,
            max_workers=max_workers,
            from_cache=False,
            base_seed=base_seed,
        )

    def _execute(
        self, spec: GameSpec, seeds: list[int | None], max_workers: int
    ) -> list[GameSimResult]:
        """Run the N games -- in-process when ``max_workers == 1``, else pooled.

        The synchronous (workers=1) path runs every game in THIS process in seed
        order: fast, deterministic, no pickling, no fork -- the path the always-on
        tests use to stay quick and exactly reproducible.  The pooled path fans
        out across processes (SIM-281 D1) and reassembles results IN SEED ORDER so
        the summary's raw per-iteration arrays line up with the seed list
        regardless of completion order.

        SIM-360: with ``reuse_pool`` True (the default) the pooled path uses the
        PERSISTENT warm pool from :meth:`_get_pool` (created once, reused across
        ``run()`` calls, recreated only on a worker-count change), so a long-lived
        API does not re-fork per request.  With ``reuse_pool`` False the SIM-332
        behaviour is preserved: a fresh ``with ProcessPoolExecutor(...)`` per call,
        torn down at the end of the call.
        """
        if max_workers <= 1:
            return [_run_one(spec, s) for s in seeds]

        if self._reuse_pool:
            pool = self._get_pool(max_workers)
            return self._map_pool(pool, spec, seeds)

        # reuse_pool=False -> SIM-332 transient pool (fresh per call).
        with ProcessPoolExecutor(max_workers=max_workers, **self._pool_kwargs()) as pool:
            return self._map_pool(pool, spec, seeds)

    @staticmethod
    def _map_pool(
        pool: ProcessPoolExecutor,
        spec: GameSpec,
        seeds: list[int | None],
    ) -> list[GameSimResult]:
        """Submit one ``_run_one`` per seed to ``pool`` and gather IN SEED ORDER.

        Reassembles results by their seed index regardless of completion order so
        the summary's raw per-iteration arrays line up with the seed list -- the
        same ordering contract the SIM-332 fresh-pool path guaranteed, factored out
        so both the warm-pool (SIM-360) and transient-pool paths share it.
        """
        results: list[GameSimResult | None] = [None] * len(seeds)
        futures = {pool.submit(_run_one, spec, s): idx for idx, s in enumerate(seeds)}
        for fut in as_completed(futures):
            idx = futures[fut]
            results[idx] = fut.result()
        # Every slot is filled (one future per seed); the cast is safe.
        return [r for r in results if r is not None]


# ---------------------------------------------------------------------------
# A picklable, no-DB machine factory — the always-on (no live sampler) path
# ---------------------------------------------------------------------------
#
# These are module-level (so they pickle by dotted reference) and build an
# rng-driven StateMachine with NO sampler -- the same no-DB pattern SIM-320's
# tests use, lifted to a picklable factory so it works across the process
# boundary.  A production factory (SIM-333) replaces these with one that builds a
# PlayPoolSampler over the attached shared tiles.


def rng_driven_machine_factory(seed: int | None, spec: GameSpec):
    """Build a no-sampler, rng-driven :class:`StateMachine` for the worker.

    The returned machine draws each pitch outcome from its own loop rng (seeded
    from the per-game ``seed``) and resolves in-play balls via a deterministic,
    picklable resolver -- so an entire game runs with NO DuckDB / FAISS, fully
    reproducible from ``seed``.  Referenced by dotted path
    ``"simulation.batch_runner:rng_driven_machine_factory"`` on a
    :class:`GameSpec`.
    """
    rng = np.random.default_rng(seed)
    hit_rate = (
        float(spec.sim_kwargs.get("_hit_rate", 0.30)) if isinstance(spec.sim_kwargs, dict) else 0.30
    )
    return _RngOutcomeStateMachine(resolver=_CyclingResolver(rng, hit_rate=hit_rate), rng=rng)


# Imported lazily-at-module-load (they live in sim_loop) so the factory above can
# reference them; defined here as module-level classes so they pickle by name.
from simulation.sim_loop import FieldingSignal, PlayResolver, StateMachine  # noqa: E402


class _CyclingResolver(PlayResolver):
    """A no-DB resolver: an in-play ball is a single ``hit_rate`` of the time,
    else an out -- so games make progress, score, and END without a sampler.

    Module-level + holds only an rng + a float, so it pickles cleanly across the
    process boundary (the whole point of the GameSpec/factory seam).
    """

    def __init__(self, rng: np.random.Generator, hit_rate: float = 0.30):
        self.rng = rng
        self.hit_rate = float(hit_rate)

    def resolve_fielding(self, state, battedball_sample) -> FieldingSignal:
        if float(self.rng.random()) < self.hit_rate:
            return FieldingSignal(event="single", result_hits=1, result_outs=0, result_runs=0)
        return FieldingSignal(event="field_out", result_hits=0, result_outs=1, result_runs=0)


class _RngOutcomeStateMachine(StateMachine):
    """A :class:`StateMachine` that draws each pitch outcome from its loop rng (no
    sampler), so a full game runs deterministically from one seed with no live DB.

    Module-level so it pickles by reference; mirrors SIM-320's no-DB test driver.
    """

    def step_pitch(self, state, **_kw):  # type: ignore[override]
        r = float(self.rng.random())
        if r < 0.55:
            outcome = "in_play"
        elif r < 0.75:
            outcome = "ball"
        elif r < 0.92:
            outcome = "called_strike"
        else:
            outcome = "foul"
        return super().step_pitch(state, pitch_outcome=outcome)


__all__ = [
    # tuning constants
    "SIM_RESULT_TTL_S",
    "POOL_QUERY_TTL_S",
    "MAX_WORKER_CEILING",
    # worker-count + seed helpers
    "default_max_workers",
    "derive_seed",
    # the picklable spec + worker
    "GameSpec",
    # SIM-333 shared-memory zero-copy attach
    "SharedArrayDescriptor",
    "publish_shared_arrays",
    "unlink_shared_segments",
    "get_shared_view",
    # cache interface + backends
    "SimCache",
    "NullCache",
    "InMemoryCache",
    "RedisCache",
    "make_cache",
    # the runner
    "BatchRunner",
    "BatchResult",
    # the picklable no-DB factory (the always-on path)
    "rng_driven_machine_factory",
]
