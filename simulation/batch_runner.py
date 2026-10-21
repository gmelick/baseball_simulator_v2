"""The parallel Monte-Carlo batch runner.

:func:`simulation.sim_loop.simulate_game` plays ONE game. A Monte-Carlo run
plays the *same* matchup N times (default 100) and aggregates the N per-game
:class:`~simulation.results.GameSimResult`s into one
:class:`~simulation.results.GameSimSummary`. This module fans the games out
across worker processes and gathers the results back in. The budget is 2 s a
game and 30 s a 100-game batch.

THE DESIGN
----------
  * **Concurrency.** A ``concurrent.futures.ProcessPoolExecutor`` with
    ``max_workers = min(os.cpu_count() - 1, 10)`` (the process model,
    ``docs/technical/architecture.md``); ``SIM_RUNNER_WORKERS``
    overrides the count in the app. The pool starts each worker with
    ``forkserver``, from a lean server process, not as a copy of the
    engine-loaded parent: each worker holds about 575 MB private (measured
    2026-09-15; the app service's note in ``docker-compose.yml``). See
    :data:`_MP_START_METHOD` and :func:`default_max_workers`.
  * **One seed per game.** Iteration ``i`` gets the seed
    ``derive_seed(base_seed, i)``. A fixed ``base_seed`` reproduces the WHOLE
    batch, and each game still differs. See :func:`derive_seed`.
  * **Picklable work only.** A worker NEVER receives a live sampler or
    StateMachine across the process boundary: those hold rng state and large
    arrays. It receives a small, picklable :class:`GameSpec` (the
    ``simulate_game`` kwargs and a dotted reference to a *factory* that builds
    the machine) and BUILDS its own machine in its own process
    (:func:`_run_one`).
  * **Optional caching.** A summary caches for 60 s, a prop set for 15
    minutes and a pool query for 5 minutes, behind the small :class:`SimCache`
    interface. Redis is OPTIONAL: :func:`make_cache` returns a
    :class:`RedisCache` only when a live server answers, else an in-process
    :class:`InMemoryCache` (oldest-first eviction and a time to live).
    The tests run on the in-memory cache, with no external server.

THE SHARED-MEMORY ATTACH
------------------------
The parent publishes the bundle's big read-only arrays into named
``multiprocessing.shared_memory`` segments ONCE, and each worker ATTACHES them
with no copy:

  * :func:`publish_shared_arrays` (in the parent) copies each read-only numpy
    buffer (the engine-artifact arrays) ONCE into a named ``SharedMemory``
    segment. It returns a picklable ``{name: SharedArrayDescriptor}`` registry
    and the owned segment handles.
  * :meth:`BatchRunner._pool_kwargs` sets the pool's
    ``initializer=_worker_init`` / ``initargs=(registry,)``, so each worker
    attaches the shared segments ONCE at startup.
  * :func:`_worker_init` ATTACHES each segment by name and builds a zero-copy
    ``np.ndarray`` *view* over the shared buffer in the :data:`_WORKER_SHARED`
    process global. The worker's factory
    (:func:`simulation.production_factory._build_full_pool_sampler`) splices
    those views into the bundle it loads instead of reading the arrays again.
  * The PARENT owns create and ``unlink`` (:meth:`BatchRunner.close` /
    :func:`unlink_shared_segments`). Workers attach and ``close`` but NEVER
    ``unlink``.

With no ``shared_arrays``, the registry is empty and each worker loads the
bundle from disk itself. The tests take that path: they use the picklable no-DB
factory over a synthetic bundle.
"""

from __future__ import annotations

import importlib
import logging
import multiprocessing
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from multiprocessing import Manager, shared_memory
from typing import Any

import numpy as np

from simulation import cache_codec
from simulation.flags import read_int, read_str
from simulation.prop_distributions import PropDistributionSet
from simulation.results import BoxScore, GameSimResult, GameSimSummary
from simulation.sim_loop import simulate_game

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The cache time-to-live (TTL) constants
# ---------------------------------------------------------------------------

#: The summary cache's TTL, in seconds. A re-run with the SAME spec, base seed
#: and N is deterministic, so the cache only saves a recompute on a hot key; it
#: is not a store of record.
SIM_RESULT_TTL_S = 60
#: The prop-set cache's TTL, in seconds. The game page's projections panel
#: reads ONE run many times: the means first, then one distribution per prop the
#: user clicks, then that distribution again for each line typed. A seeded run
#: is deterministic, so the set lives 15 minutes, long enough to review a game.
SIM_PROP_SET_TTL_S = 900
#: This process's token in the prop-set cache key. Redis outlives an app
#: restart, and a restart can bring a new bundle or a flipped flag; neither is
#: in the key. A new process draws a new token, so it never reads a set an
#: earlier process cached.
_PROP_SET_CACHE_EPOCH = os.urandom(8).hex()
#: The pool-query cache's TTL, in seconds: the read-only play-pool and engine
#: query results live 5 minutes (they change only on the nightly build).
POOL_QUERY_TTL_S = 300

#: The hard ceiling on the default worker count. ``SIM_RUNNER_WORKERS`` sets
#: the app's own count; full-pool throughput plateaus past about 6 workers.
MAX_WORKER_CEILING = 10

#: How long a prewarm worker waits for the warm semaphore before it warms
#: anyway. It bounds the WAIT, not the warm, and is long so it rarely trips.
_PREWARM_ACQUIRE_TIMEOUT_S = 60.0

#: The most workers that run the heavy full-pool warm AT THE SAME TIME. Each warm
#: loads the bundle and builds the per-hand indexes in its worker. Warming every
#: worker at once spiked memory and got a worker killed for running out of
#: memory, so the warms run in waves (the rest spawn cheaply and wait).
#: ``SIM_PREWARM_MAX_CONCURRENT`` sets the cap; the code default is small.
_PREWARM_MAX_CONCURRENT_WARM = max(1, read_int("SIM_PREWARM_MAX_CONCURRENT") or 1)

#: The deadline of the pooled prewarm gather. Past it, prewarm returns whatever
#: warmed, and the remaining warm tasks finish on the persistent pool (or warm
#: lazily on first use). So a stalled environment (e.g. a full /dev/shm, a
#: worker that cannot spawn) can NEVER block prewarm forever. The app runs
#: prewarm as a background task, so a hang before the gather (the Manager
#: spawn, the shared-memory publish) does not block startup either.
_PREWARM_TIMEOUT_S = 120.0


#: The pool's multiprocessing start method (``SIM_MP_START_METHOD``; code
#: default ``forkserver``). A 'fork' worker is a copy-on-write fork of the
#: engine-loaded (~6 GB) parent. CPython's reference counts and cyclic garbage
#: collector write to every inherited object header and defeat copy-on-write,
#: so each worker's resident memory grows toward the ~6 GB it does NOT need (a
#: full-pool worker needs only the ~470 MB bundle it loads itself; the worker
#: start method, SIM-430). 'forkserver' forks each worker from a clean, lean
#: server process instead, so no worker inherits the engines. It is also safer
#: than forking the multi-threaded uvicorn process (Python 3.14 makes
#: forkserver the Linux default for the same reasons). The variable also takes
#: 'fork' or 'spawn'.
_MP_START_METHOD = read_str("SIM_MP_START_METHOD").strip().lower()


def _pool_mp_context() -> Any:
    """Return the multiprocessing context for the worker pool.

    Defaults to ``forkserver``, so workers do NOT inherit the parent's ~6 GB of
    engines. Falls back to the platform default when the requested method is
    unavailable (e.g. a non-Linux host without forkserver)."""
    try:
        if _MP_START_METHOD in multiprocessing.get_all_start_methods():
            return multiprocessing.get_context(_MP_START_METHOD)
    except Exception:  # noqa: BLE001 - any oddity -> platform default
        pass
    return multiprocessing.get_context()


def default_max_workers(cpu_count: int | None = None) -> int:
    """The default worker count: ``min(os.cpu_count() - 1, 10)``, floored at 1.

    ``cpu_count`` is injectable for a deterministic test; it defaults to
    ``os.cpu_count()`` (1 when the OS reports ``None``). The floor of 1 keeps a
    usable pool on a single-core host; :data:`MAX_WORKER_CEILING` is the
    ceiling.
    """
    cpu = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    return max(1, min(int(cpu) - 1, MAX_WORKER_CEILING))


def derive_seed(base_seed: int | None, i: int) -> int | None:
    """Derive iteration ``i``'s per-game seed from a ``base_seed``.

    Returns ``base_seed + i`` (a distinct, deterministic seed per iteration)
    when a base is given, so a fixed ``base_seed`` makes the WHOLE batch
    reproducible AND every game differs. Returns ``None`` (a nondeterministic
    per-game seed) when ``base_seed`` is ``None``: the path for a caller that
    does not need reproducibility.
    """
    if base_seed is None:
        return None
    return int(base_seed) + int(i)


# ---------------------------------------------------------------------------
# The picklable game spec: what crosses the process boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GameSpec:
    """A fully picklable description of ONE matchup to simulate (the worker input).

    This is the ONLY thing that crosses the ``ProcessPoolExecutor`` boundary per
    task. It carries plain values and a *dotted reference* to a module-level
    factory that BUILDS the per-worker :class:`~simulation.sim_loop.StateMachine`
    (in production, with its full-pool sampler over the engine-artifact bundle),
    never a live object.

    Fields:
      * ``machine_factory`` -- ``"pkg.mod:callable"``, resolving to a module-level
        ``factory(seed: int | None, spec: GameSpec) -> StateMachine | None``.
        ``None`` lets ``simulate_game`` build its default machine. That machine
        has no sampler, so its first pitch raises: a game needs a factory. The
        tests pass the picklable no-DB factory, :func:`rng_driven_machine_factory`.
      * ``sim_kwargs`` -- picklable keyword arguments forwarded to
        :func:`simulate_game` (e.g. ``pitcher_id`` / ``bat_hand`` / ``season`` /
        the two lineups / ``max_innings``). They MUST be picklable (no live
        sampler).

        **FACTORY-ONLY KEYS.** A key whose name starts with an underscore (e.g.
        ``_hit_rate``) tunes the machine the ``machine_factory`` builds and is
        **NOT** a :func:`simulate_game` parameter. :func:`_run_one` leaves every
        ``_``-prefixed key out of the ``simulate_game(**...)`` call, so a
        factory may keep its own settings here without a ``TypeError`` against
        ``simulate_game``'s fixed signature. The other keys pass through as
        they are.
    """

    machine_factory: str | None = None
    sim_kwargs: dict[str, Any] = field(default_factory=dict)

    def cache_key_fields(self) -> tuple:
        """The picklable, hashable identity of this spec for the cache key
        (``sim_kwargs`` sorted so the key is stable across dict order)."""
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
# The shared-memory zero-copy attach of the read-only payload
# ===========================================================================
#
# WHAT IS SHARED
# --------------
# The payload is the engine-artifact bundle's big read-only numeric arrays: the
# pitch pools, the batted-ball pools and the actor embeddings
# (``EngineArtifacts.extract_shared_arrays``). The parent publishes them ONCE
# into named ``multiprocessing.shared_memory`` segments. Every worker attaches
# them by name and builds a *view* (``np.ndarray(shape, dtype, buffer=shm.buf)``)
# with NO copy, so the bulky bytes are resident once, however many workers run.
#
# WHAT STAYS PER WORKER
# ---------------------
# The object-dtype arrays (``outcome_type`` / ``event``) and the pitcher-sim
# dict cannot go through ``shared_memory``. Each worker loads them from disk with
# the bundle's other small members.
#
# THE LIFECYCLE (the parent owns create and unlink; workers attach and close)
# ---------------------------------------------------------------------------
# The PARENT creates the ``SharedMemory`` segments and is their sole owner: it,
# and only it, calls ``unlink()`` exactly once at shutdown. Each worker ATTACHES
# the existing segment by name and ``close()``s its own handle when done. A
# worker MUST NOT ``unlink()``: that would free memory other workers still map.
# The registry the workers receive is a picklable ``{name: SharedArrayDescriptor}``
# map (the names, shapes and dtypes), never the buffers themselves.


#: The picklable descriptor of one segment: enough to build a zero-copy numpy
#: view over an attached :class:`multiprocessing.shared_memory.SharedMemory`
#: block. A registry is ``{logical_name: SharedArrayDescriptor}`` and crosses
#: the process boundary through the pool's ``initargs`` (names, shapes and
#: dtypes only; the bytes stay in the OS-backed segment and are never pickled).
@dataclass(frozen=True)
class SharedArrayDescriptor:
    """How to attach and view ONE read-only numpy array in shared memory.

    Fields:
      * ``shm_name`` -- the OS name of the ``SharedMemory`` segment (picklable;
        the worker attaches by this name).
      * ``shape`` -- the array shape of the view.
      * ``dtype`` -- the numpy dtype *string* (e.g. ``"float64"``): picklable,
        and it round-trips through ``np.dtype(...)``.
    """

    shm_name: str
    shape: tuple[int, ...]
    dtype: str


def publish_shared_arrays(
    arrays: dict[str, np.ndarray],
) -> tuple[dict[str, SharedArrayDescriptor], list[shared_memory.SharedMemory]]:
    """In the parent: copy each read-only array ONCE into a named
    ``SharedMemory`` segment and return ``(registry, owned_segments)``.

    The ``registry`` (``{name: SharedArrayDescriptor}``) is the picklable map
    the workers receive through :class:`BatchRunner` / :func:`_worker_init`.
    ``owned_segments`` is the list of live ``SharedMemory`` handles the
    **parent must keep alive** for the pool's lifetime and ``unlink()`` exactly
    once at shutdown (see :func:`unlink_shared_segments`). The copy here is the
    ONLY copy of the bytes: workers attach the same physical segment zero-copy.
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
    """The parent's teardown: ``close()`` then ``unlink()`` every owned segment
    exactly once (idempotent; never raises).

    ONLY the parent calls this; a worker must never ``unlink()`` (it would free
    memory other workers still map). The function swallows the "already
    unlinked" and "missing" errors, so a double teardown is safe.
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
# The pool initializer: the shared-memory ATTACH happens here
# ---------------------------------------------------------------------------

#: The process global the worker initializer fills with the ATTACHED shared
#: memory. ``"views"`` maps a logical name to a zero-copy ``np.ndarray`` view over
#: the shared buffer; ``"_handles"`` holds the live ``SharedMemory`` handles the
#: worker keeps open (it ``close()``s them at exit but NEVER ``unlink()``s them).
#: Empty when no registry is supplied: the worker then loads the bundle from disk.
_WORKER_SHARED: dict[str, Any] = {"views": {}, "_handles": []}


def _worker_init(shared_registry: dict[str, SharedArrayDescriptor] | None = None) -> None:
    """The pool ``initializer`` (runs ONCE per worker at startup): the attach.

    Each worker ATTACHES the parent's named ``multiprocessing.shared_memory``
    segments here (zero-copy) and builds a numpy *view* per segment over the
    shared buffer, so the read-only payload is resident ONCE, not once per
    worker. The views land in the :data:`_WORKER_SHARED` process global, where
    the production factory reads them (:func:`get_shared_view` reads one by
    name). A worker attaches and ``close()``s its own handle but NEVER
    ``unlink()``s: the parent owns that.

    A ``None`` or empty registry leaves the global empty, and the worker loads
    the bundle from disk.
    """
    # Reset (a re-used worker process must not carry stale views).
    _WORKER_SHARED["views"] = {}
    _WORKER_SHARED["_handles"] = []
    if not shared_registry:
        return
    for name, desc in shared_registry.items():
        shm = shared_memory.SharedMemory(name=desc.shm_name)  # ATTACH (no create)
        view: np.ndarray = np.ndarray(tuple(desc.shape), dtype=np.dtype(desc.dtype), buffer=shm.buf)
        view.setflags(write=False)  # read-only: no worker may write the shared buffer
        _WORKER_SHARED["views"][name] = view
        _WORKER_SHARED["_handles"].append(shm)


def get_shared_view(name: str) -> np.ndarray | None:
    """In a worker: the zero-copy view :func:`_worker_init` attached for
    ``name``, or ``None`` when no such shared segment was published.

    ``None`` tells the caller to load that array from disk.
    """
    return _WORKER_SHARED.get("views", {}).get(name)


def _prewarm_worker(sem: Any, pool_dir: str | None) -> int:
    """The worker's prewarm task: warm THIS worker's cache, gated by a shared
    semaphore, so only a few workers run the heavy warm AT ONCE.

    :meth:`BatchRunner.prewarm` submits one task per worker, which makes the
    executor spawn every worker. The semaphore then bounds how many run the
    *heavy* :func:`simulation.production_factory.warm_worker_cache` at the same
    time, so the peak memory of spawning and warming stays under the ``app``
    service's memory limit (a warm of every worker at once got a worker killed
    for running out of memory). Returns ``os.getpid()``, so the parent can
    count the DISTINCT warmed workers.

    A semaphore failure or a warm failure is swallowed: the worst case is a
    worker that warms lazily on its first real game.
    """
    acquired = False
    if sem is not None:
        try:
            acquired = bool(sem.acquire(timeout=_PREWARM_ACQUIRE_TIMEOUT_S))
        except Exception:
            acquired = False
    try:
        from simulation.production_factory import warm_worker_cache

        warm_worker_cache(pool_dir)
    except Exception:
        pass
    finally:
        if acquired:
            try:
                sem.release()
            except Exception:
                pass
    return os.getpid()


# ---------------------------------------------------------------------------
# The per-iteration worker: it runs in the worker and builds its own objects
# ---------------------------------------------------------------------------


def _run_one(spec: GameSpec, seed: int | None) -> GameSimResult:
    """Run ONE :func:`simulate_game` for ``spec`` at the per-game ``seed``.

    The pool maps this function across the N seeds. It is module-level (so it
    pickles by reference) and builds every live object IN the worker:

      * when ``spec.machine_factory`` is set, it resolves the factory and calls
        ``factory(seed, spec)`` to build a fresh :class:`StateMachine` (the
        production factory builds the full-pool sampler over the
        engine-artifact bundle; the no-DB factory builds one over a synthetic
        bundle);
      * otherwise :func:`simulate_game` builds its default machine.

    The ``seed`` goes to BOTH the factory AND ``simulate_game(seed=...)``, so
    the machine's loop rng and the sampler's rng both follow the one per-game
    seed.

    The ``sim_kwargs`` keys split by the underscore prefix (see
    :class:`GameSpec`). The factory sees the WHOLE ``spec``, so it can read its
    own ``_``-prefixed settings like ``_hit_rate``. Only the other keys go into
    :func:`simulate_game`, which has a fixed signature and no ``**kwargs``: a
    stray factory-only key would raise ``TypeError``.
    """
    machine = None
    if spec.machine_factory is not None:
        factory = _resolve_dotted(spec.machine_factory)
        machine = factory(seed, spec)
    # Drop the factory-only (``_``-prefixed) keys, so they never reach
    # ``simulate_game``'s fixed signature. The factory already saw them in
    # ``spec`` above.
    passthrough = {k: v for k, v in spec.sim_kwargs.items() if not k.startswith("_")}
    return simulate_game(machine, seed=seed, **passthrough)


# ===========================================================================
# Cache interface + fallbacks (Redis optional)
# ===========================================================================


class SimCache:
    """The small cache interface the runner depends on (Redis is one backend).

    Three methods only: ``get`` / ``set`` (with a TTL) / ``clear``. The small
    surface lets Redis be OPTIONAL: :class:`BatchRunner` never imports ``redis``;
    it depends on this interface, and :func:`make_cache` chooses a backend (Redis
    when reachable, else the in-memory cache).
    """

    def get(self, key: str) -> Any | None:
        raise NotImplementedError

    def set(self, key: str, value: Any, ttl_s: int) -> None:
        raise NotImplementedError

    def clear(self) -> None:
        raise NotImplementedError


class NullCache(SimCache):
    """A no-op cache: every ``get`` misses, every ``set`` is dropped.

    For a caller that wants no caching. The runner stays correct with it: it
    recomputes every batch.
    """

    def get(self, key: str) -> Any | None:
        return None

    def set(self, key: str, value: Any, ttl_s: int) -> None:
        return None

    def clear(self) -> None:
        return None


class InMemoryCache(SimCache):
    """A process-local TTL cache (the fallback when no Redis server answers).

    Stores ``(value, expires_at)`` in a dict and honors the per-entry TTL with a
    monotonic clock. A small ``maxsize`` (the oldest-inserted entry is evicted
    first) bounds memory, so a long-lived runner cannot grow without limit. It
    needs no external server; the tests cache against it.
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

    Wraps a live ``redis.Redis`` client. Every value is stored as JSON through
    :mod:`simulation.cache_codec`, never as a pickle: a pickle load runs code,
    so a pickle in Redis would let anyone who can write to Redis run code in
    the API (the security hardening, SIM-443f). The key prefix is ``sim443:``,
    so the cache never reads a pickle stored under another prefix, such as
    ``sim332:``.

    :func:`make_cache` builds one only AFTER a ``ping()`` confirms a live
    server, so the runner never fails hard on a missing Redis. A Redis error
    on any one call degrades to a miss or a dropped write instead of
    propagating: a batch must not die because the cache failed.
    """

    def __init__(self, client: Any, *, prefix: str = "sim443:"):
        self._client = client
        self._prefix = prefix

    def _k(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def get(self, key: str) -> Any | None:
        try:
            raw = self._client.get(self._k(key))
        except Exception:
            return None
        return cache_codec.decode(raw)

    def set(self, key: str, value: Any, ttl_s: int) -> None:
        try:
            text = cache_codec.encode(value)
        except (TypeError, ValueError) as exc:
            # A value JSON cannot hold is not cached; the caller recomputes.
            log.warning("sim cache: skipped %s (not JSON: %s)", key, type(exc).__name__)
            return None
        try:
            self._client.set(self._k(key), text, ex=int(ttl_s))
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

    The order (Redis is OPTIONAL):
      1. ``prefer_redis`` False  -> :class:`InMemoryCache` (the explicit
         no-server path the tests use).
      2. an injected ``client`` (or a ``redis_url`` / ``REDIS_URL`` env) that
         answers ``ping()`` -> :class:`RedisCache`.
      3. anything else (``redis`` not installed, no server, ping fails) ->
         :class:`InMemoryCache`.

    The fallback is the in-memory cache (correct and fast, only
    process-local), so a batch run NEVER requires a Redis server.
    """
    if not prefer_redis:
        return InMemoryCache()
    try:
        if client is None:
            import redis  # client lib only; may be absent.

            url = redis_url or os.environ.get("REDIS_URL")
            client = redis.Redis.from_url(url) if url else redis.Redis()
        client.ping()  # confirms a LIVE server before the cache uses it.
        return RedisCache(client)
    except Exception:
        # redis not installed / no server / ping failed -> the in-memory cache.
        return InMemoryCache()


# ===========================================================================
# The runner: fan out across processes, fan in to a GameSimSummary
# ===========================================================================


@dataclass(frozen=True)
class BatchResult:
    """The runner's return: the summary and the run's provenance.

    ``summary`` is the aggregate every consumer reads. The rest says how the
    run went (how many workers, whether the summary came from the cache), so a
    caller can report it without deriving it again.
    """

    summary: GameSimSummary
    n_iterations: int
    max_workers: int
    from_cache: bool
    base_seed: int | None


@dataclass(frozen=True)
class JobOutcome:
    """A run job's return: the summary and the prop set of the games played,
    how many were played, and whether a cancel stopped it early.

    ``summary`` and ``prop_set`` are None when no game was played (a cancel
    before the first chunk ended)."""

    summary: GameSimSummary | None
    prop_set: PropDistributionSet | None
    n_done: int
    n_requested: int
    cancelled: bool


class BatchRunner:
    """Run N iterations of :func:`simulate_game` and aggregate them to a summary.

    The fan-out and fan-in:
      * derive N distinct per-game seeds from ``base_seed`` (:func:`derive_seed`);
      * map :func:`_run_one` across them through
        ``ProcessPoolExecutor(max_workers=min(cpu-1, 10))``, or, with
        ``max_workers=1``, run them in this process (the fast, deterministic
        test path);
      * aggregate the N :class:`GameSimResult`s with
        :meth:`GameSimSummary.from_results`;
      * memoize the summary in the :class:`SimCache`, keyed on
        ``(spec + base_seed + N)``, for :data:`SIM_RESULT_TTL_S`.

    :meth:`_pool_kwargs` wires the shared-memory attach (initializer /
    initargs); it does nothing when no ``shared_arrays`` are given.

    THE PERSISTENT (WARM) POOL
    --------------------------
    A long-lived API that forked a fresh pool per request would pay the fork,
    the worker startup and the shared-memory publish and unlink on EVERY
    request. With ``reuse_pool`` True (the default), the runner creates ONE
    ``ProcessPoolExecutor`` lazily on the first pooled ``_execute`` and
    **reuses** it across later ``run()`` calls. It publishes the shared-memory
    segments ONCE (:meth:`_ensure_shared_published`), and they stay attached
    for the warm pool's lifetime. The policy:

      * **create** -- lazily, on the first ``_execute`` whose ``max_workers > 1``,
        with ``max_workers`` and the :meth:`_pool_kwargs` attach.
      * **reuse** -- a later pooled ``run()`` with the SAME worker count reuses
        the SAME ``ProcessPoolExecutor`` (no new fork, no new publish).
      * **recreate on a worker-count change** -- a pooled ``run()`` that
        resolves to a DIFFERENT ``max_workers`` shuts the old pool down
        (``wait=True``) and builds a fresh one at the new size. The shared-memory
        segments are NOT published again: they outlive the pool, and the new
        workers attach them through the same ``_pool_kwargs``.
      * **close** -- :meth:`close` shuts the pool down (``wait=True``), drops the
        reference and unlinks the shared segments (idempotent). ``__exit__``
        calls it, so a context manager or a one-shot runner cleans up.

    ``reuse_pool=False`` makes a fresh ``with ProcessPoolExecutor(...)`` per
    ``_execute``, torn down at the end of the call. Either way the
    ``max_workers <= 1`` path runs in this process and never creates a pool.
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
        # None -> the default worker count at run time; an explicit value (1 =
        # in this process) overrides it.
        self._max_workers_override = max_workers
        self.confidence_level = float(confidence_level)

        # The read-only arrays to publish into shared memory ONCE, for the pool
        # to attach zero-copy. None or empty: no shared segments, and each
        # worker loads the bundle itself. The registry and the owned segments
        # fill lazily on the first pooled run; :meth:`close` tears them down.
        self._shared_arrays = dict(shared_arrays) if shared_arrays else {}
        self._shared_registry: dict[str, SharedArrayDescriptor] = {}
        self._owned_segments: list[shared_memory.SharedMemory] = []

        # The persistent (warm) pool, created lazily on the first pooled
        # ``_execute`` and reused across ``run()`` calls. ``_pool`` is the live
        # executor (None until created and after close); ``_pool_workers`` is
        # the worker count it was sized for, so a later run with a different
        # count can detect the change and recreate it. ``reuse_pool=False``
        # makes a fresh pool per call.
        self._reuse_pool = bool(reuse_pool)
        self._pool: ProcessPoolExecutor | None = None
        self._pool_workers: int | None = None
        # Guards the lazy pool creation, so a background prewarm and a first
        # request at the same time cannot both create a pool or publish the
        # shared segments twice.
        self._pool_lock = threading.Lock()

    # ---- worker-count + cache-key helpers --------------------------------

    def resolve_max_workers(self, n_iterations: int) -> int:
        """The worker count for this batch: the override, else :func:`default_max_workers`.

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
        """Key the sim cache on (game spec + base seed + N).

        A pure function of exactly the inputs that determine the summary, so a
        repeat call with the same matchup, seed and N hits the memoized result.
        """
        return repr(("sim", spec.cache_key_fields(), base_seed, int(n)))

    def _prop_set_cache_key(self, spec: GameSpec, base_seed: int | None, n: int) -> str:
        """Key a prop set on the summary's inputs plus this process's token."""
        return repr(("props", _PROP_SET_CACHE_EPOCH, spec.cache_key_fields(), base_seed, int(n)))

    def _ensure_shared_published(self) -> dict[str, SharedArrayDescriptor]:
        """Publish the read-only arrays into shared memory ONCE (idempotent).

        On the first pooled run with ``shared_arrays`` set, copy each array into a
        named ``SharedMemory`` segment (the single copy of the bytes) and keep
        the registry and the owned handles. Returns the picklable registry the
        workers attach by name. An empty ``shared_arrays`` returns ``{}``: no
        segments (``initargs=(None,)``).
        """
        if not self._shared_arrays:
            return {}
        if not self._shared_registry:
            self._shared_registry, self._owned_segments = publish_shared_arrays(self._shared_arrays)
        return self._shared_registry

    def _pool_kwargs(self) -> dict[str, Any]:
        """Kwargs for the ``ProcessPoolExecutor``: the shared-memory attach and the
        start method.

        Returns ``initializer=_worker_init`` with ``initargs=(registry,)``, where
        ``registry`` is the picklable ``{name: SharedArrayDescriptor}`` that
        :meth:`_ensure_shared_published` published. Each worker attaches those
        named segments ONCE at startup and reads the SAME physical memory
        zero-copy instead of loading the arrays per task. With no shared arrays
        the registry is ``None``, and each worker loads the bundle itself.
        """
        registry = self._ensure_shared_published()
        # mp_context: forkserver by default, so workers fork from a lean server,
        # not the engine-loaded (~6 GB) parent (see _pool_mp_context).
        # _worker_init attaches the shared segments by name whatever the start
        # method.
        return {
            "initializer": _worker_init,
            "initargs": (registry or None,),
            "mp_context": _pool_mp_context(),
        }

    # ---- the persistent (warm) pool's lifecycle --------------------------------

    def _get_pool(self, max_workers: int) -> ProcessPoolExecutor:
        """Return the warm :class:`ProcessPoolExecutor` for this run.

        Creates the pool lazily on the first pooled call (sized for
        ``max_workers``, with the shared-memory attach from :meth:`_pool_kwargs`)
        and REUSES the SAME instance on later calls. When a later run needs a
        DIFFERENT worker count than the live pool has, it shuts the old pool
        down (``wait=True``) and creates a fresh one for the new count. The
        shared-memory segments are NOT published again on a recreate: they
        outlive the pool, and the new workers attach them through the same
        ``_pool_kwargs``.

        :meth:`_execute` (on the ``max_workers > 1`` path with ``reuse_pool``
        True) and :meth:`prewarm` call it, so the in-process path never builds
        a pool.
        """
        with self._pool_lock:
            if self._pool is not None and self._pool_workers != max_workers:
                # The worker count changed between runs -> drain and drop the stale
                # pool, then rebuild it at the new size.
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

    def prewarm(self, pool_dir: str | None = None, *, timeout: float | None = None) -> int:
        """Warm the sim machinery BEFORE the first request, so ``/simulate`` never
        pays the cold per-worker warm-up on the request path.

        The cold fan-out: a fresh n-game request spreads its n games across the
        pool, so with n near the worker count each worker gets ONE game. The
        per-worker full-pool cache (in ``production_factory``) pays off only from
        a worker's second game, so on that first batch every worker is cold. Each
        pays the full bundle load and per-hand precompute: the n=10 request
        stalled about 500 s. Prewarming does that warm-up ONCE per worker at
        startup, in parallel, off the request path.

        * **Pooled path** (``reuse_pool`` and a resolved worker count > 1): submit
          one warm task per worker, so the executor spawns all W workers, and run
          :func:`simulation.production_factory.warm_worker_cache` on each. A
          shared semaphore caps how many run the HEAVY warm at once, so the peak
          memory of spawning and warming stays bounded. The tasks use the SAME
          persistent pool a later :meth:`run` reuses, so the workers stay warm.
          Returns the count of DISTINCT workers warmed.
        * **In-process path** (a worker count <= 1, or ``reuse_pool`` off): there
          is no separate worker, so warm THIS process's cache and return 1 (0
          when the warm cached nothing, e.g. no bundle).

        The pooled gather stops at ``timeout`` seconds (default
        :data:`_PREWARM_TIMEOUT_S`), so a stalled worker or environment cannot
        block prewarm forever. Past the deadline it returns whatever warmed, and
        the rest warm on the pool or lazily. Never raises: on any failure the
        lazy per-game warm-up remains. The caller logs the returned count.
        """
        # The pool size this runner WOULD use, not capped by any n_iterations:
        # prewarm warms the whole pool, not one batch.
        target = (
            self._max_workers_override
            if self._max_workers_override is not None
            else default_max_workers()
        )
        target = max(1, int(target))

        # In-process (no separate worker) -> warm the current process directly.
        if target <= 1 or not self._reuse_pool:
            try:
                from simulation.production_factory import warm_worker_cache

                return 1 if warm_worker_cache(pool_dir) else 0
            except Exception:
                return 0

        # Pooled -> submit one warm task per worker, which makes all W workers
        # spawn. A shared semaphore caps how many run the HEAVY full-pool warm AT
        # ONCE, so the peak memory of spawning and warming stays bounded.
        deadline = float(timeout) if timeout is not None else _PREWARM_TIMEOUT_S
        try:
            pool = self._get_pool(target)
            # `manager: Any` -- SyncManager.Semaphore is registered at runtime but
            # missing from some typeshed versions (attr-defined false positive).
            manager: Any = Manager()
            pids: set[int] = set()
            try:
                sem = manager.Semaphore(_PREWARM_MAX_CONCURRENT_WARM)
                futures = [pool.submit(_prewarm_worker, sem, pool_dir) for _ in range(target)]
                # Bound the gather. Past the deadline, prewarm returns whatever
                # warmed; the remaining warm tasks keep running on the persistent
                # pool (and a worker whose task never finishes warms lazily).
                try:
                    for fut in as_completed(futures, timeout=deadline):
                        try:
                            pids.add(fut.result())
                        except Exception:
                            pass
                except Exception:
                    pass  # deadline (TimeoutError) or pool failure -> partial count
            finally:
                manager.shutdown()
            return len(pids)
        except Exception:
            return 0

    # ---- the shared-memory and pool lifecycle (the parent owns create/unlink) ---

    @property
    def shared_registry(self) -> dict[str, SharedArrayDescriptor]:
        """The published ``{name: SharedArrayDescriptor}`` registry (publishing it
        if not yet done), so a caller or a test can attach the segments."""
        return self._ensure_shared_published()

    def close(self) -> None:
        """Shut the warm pool down and release the parent-owned shared segments.

        ``shutdown(wait=True)`` the persistent pool (if one was created) and drop
        the reference, THEN ``unlink()`` each owned shared segment exactly once.
        The pool goes first, so no worker maps a segment when it is unlinked.
        ONLY the parent unlinks; workers attach and close but never unlink.
        Idempotent: safe to call repeatedly, or when no pool was created and
        nothing was published.
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
        """Run the batch and return a :class:`BatchResult` (summary and provenance).

        ``n_iterations`` defaults to 100. ``base_seed``, when set, makes the
        whole batch reproducible (each game's seed comes from
        :func:`derive_seed`). ``use_cache`` reads and fills the :class:`SimCache`
        (keyed on spec + base seed + N) at :data:`SIM_RESULT_TTL_S`; the cache
        only saves time and is never needed for a correct result.
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

    def run_prop_set(
        self,
        spec: GameSpec,
        *,
        n_iterations: int = 100,
        base_seed: int | None = None,
        use_cache: bool = True,
    ) -> PropDistributionSet:
        """Run the batch and return every player's prop distributions.

        The games run exactly as in :meth:`run` (the same per-game seeds, the same
        worker pool), and the per-game box scores feed
        :meth:`PropDistributionSet.from_boxscores`.  A game with no box score
        counts as an empty one, so the denominator stays ``n_iterations``.

        The cache holds a SEEDED set for :data:`SIM_PROP_SET_TTL_S`.  An unseeded
        call asks for a fresh draw, so it always runs the games.
        """
        if n_iterations < 1:
            raise ValueError(f"n_iterations must be >= 1, got {n_iterations}")

        cacheable = use_cache and base_seed is not None
        key = self._prop_set_cache_key(spec, base_seed, n_iterations)
        if cacheable:
            cached = self.cache.get(key)
            if cached is not None:
                return cached

        seeds = [derive_seed(base_seed, i) for i in range(n_iterations)]
        results = self._execute(spec, seeds, self.resolve_max_workers(n_iterations))
        prop_set = PropDistributionSet.from_boxscores(
            [r.boxscore if r.boxscore is not None else BoxScore() for r in results]
        )
        if cacheable:
            self.cache.set(key, prop_set, SIM_PROP_SET_TTL_S)
        return prop_set

    def run_job(
        self,
        spec: GameSpec,
        *,
        n_iterations: int = 100,
        base_seed: int | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> JobOutcome:
        """Play a run job's games in chunks, with progress and cancel.

        The games run as in :meth:`run` (the same per-game seeds, the same warm
        pool), in chunks of ``max_workers * 2``. After each chunk the runner
        calls ``on_progress(done, total)`` and asks ``should_cancel()``; a
        cancel stops before the next chunk (the games of the current chunk
        finish). The one batch gives both the summary and every player's prop
        distributions, so the panels never run a second batch. No cache: a job
        is stored by its caller.
        """
        if n_iterations < 1:
            raise ValueError(f"n_iterations must be >= 1, got {n_iterations}")
        max_workers = self.resolve_max_workers(n_iterations)
        seeds = [derive_seed(base_seed, i) for i in range(n_iterations)]
        chunk = max(1, max_workers * 2)
        results: list[GameSimResult] = []
        cancelled = False
        for start in range(0, n_iterations, chunk):
            results.extend(self._execute(spec, seeds[start : start + chunk], max_workers))
            if on_progress is not None:
                on_progress(len(results), n_iterations)
            if should_cancel is not None and len(results) < n_iterations and should_cancel():
                cancelled = True
                break
        if not results:
            return JobOutcome(None, None, 0, n_iterations, cancelled)
        summary = GameSimSummary.from_results(results, confidence_level=self.confidence_level)
        prop_set = PropDistributionSet.from_boxscores(
            [r.boxscore if r.boxscore is not None else BoxScore() for r in results]
        )
        return JobOutcome(summary, prop_set, len(results), n_iterations, cancelled)

    def record_game(self, spec: GameSpec, seed: int | None) -> Any:
        """Record ONE game with its plays on the warm pool.

        Returns :class:`simulation.play_recorder.RecordedGame`.  The game runs on
        a worker, whose full-pool cache is warm, so the API process never loads
        the sim bundle (that cost about 2 GB and 15 s on the first call).  It
        reuses the live pool at whatever size it has, and submits under the pool
        lock, so a batch that resizes the pool cannot shut it down between the
        read and the submit (a shutdown waits for the submitted game).  With no
        pool (one worker, or ``reuse_pool=False``) it records in this process.
        """
        # Local import: simulation.play_recorder imports this module.
        from simulation.play_recorder import record_spec

        target = max(
            1,
            int(
                self._max_workers_override
                if self._max_workers_override is not None
                else default_max_workers()
            ),
        )
        if target <= 1 or not self._reuse_pool:
            return record_spec(spec, seed)
        with self._pool_lock:
            if self._pool is None:
                self._pool = ProcessPoolExecutor(max_workers=target, **self._pool_kwargs())
                self._pool_workers = target
            future = self._pool.submit(record_spec, spec, seed)
        return future.result()

    def _execute(
        self, spec: GameSpec, seeds: list[int | None], max_workers: int
    ) -> list[GameSimResult]:
        """Run the N games: in this process when ``max_workers <= 1``, else pooled.

        The one-worker path runs every game in THIS process in seed order: fast,
        deterministic, no pickling, no fork. The tests use it to stay quick and
        exactly reproducible. The pooled path fans out across processes and
        puts the results back IN SEED ORDER, so the summary's raw per-iteration
        arrays line up with the seed list whatever the completion order.

        With ``reuse_pool`` True (the default), the pooled path uses the
        PERSISTENT warm pool from :meth:`_get_pool` (created once, reused across
        ``run()`` calls, recreated only on a worker-count change), so a
        long-lived API does not fork again per request. With ``reuse_pool``
        False, each call makes a fresh ``with ProcessPoolExecutor(...)``, torn
        down at the end of the call.
        """
        if max_workers <= 1:
            return [_run_one(spec, s) for s in seeds]

        if self._reuse_pool:
            pool = self._get_pool(max_workers)
            return self._map_pool(pool, spec, seeds)

        # reuse_pool=False -> a transient pool, fresh per call.
        with ProcessPoolExecutor(max_workers=max_workers, **self._pool_kwargs()) as pool:
            return self._map_pool(pool, spec, seeds)

    @staticmethod
    def _map_pool(
        pool: ProcessPoolExecutor,
        spec: GameSpec,
        seeds: list[int | None],
    ) -> list[GameSimResult]:
        """Submit one ``_run_one`` per seed to ``pool`` and gather IN SEED ORDER.

        Puts each result back at its seed's index, whatever the completion
        order, so the summary's raw per-iteration arrays line up with the seed
        list. The warm pool and the transient pool both use it.
        """
        results: list[GameSimResult | None] = [None] * len(seeds)
        futures = {pool.submit(_run_one, spec, s): idx for idx, s in enumerate(seeds)}
        for fut in as_completed(futures):
            idx = futures[fut]
            results[idx] = fut.result()
        # Every slot is filled (one future per seed), so the filter drops nothing.
        return [r for r in results if r is not None]


# ---------------------------------------------------------------------------
# A picklable no-DB machine factory: the synthetic-bundle path
# ---------------------------------------------------------------------------
#
# Module-level (so it pickles by dotted reference). It builds the SAME machine
# production builds, a StateMachine over a FullPoolSampler, but over the
# in-memory league bundle from :mod:`simulation.synthetic_bundle`. A whole game
# then runs with NO DuckDB and no artifact directory, and every play goes
# through the production in-play path (the transition draw).


def _inplay_model_for(hit_rate: float | None) -> dict[str, float] | None:
    """Scale the league in-play mix to a requested hit-on-contact share
    (``_hit_rate``), keeping the relative shape of hits and outs."""
    if hit_rate is None:
        return None
    from simulation.synthetic_bundle import LEAGUE_INPLAY_MODEL

    hit_rate = float(min(max(hit_rate, 0.0), 1.0))
    hits = {"single", "double", "triple", "home_run"}
    hit_mass = sum(v for k, v in LEAGUE_INPLAY_MODEL.items() if k in hits)
    out_mass = sum(v for k, v in LEAGUE_INPLAY_MODEL.items() if k not in hits)
    return {
        k: v * (hit_rate / hit_mass if k in hits else (1.0 - hit_rate) / out_mass)
        for k, v in LEAGUE_INPLAY_MODEL.items()
    }


def rng_driven_machine_factory(seed: int | None, spec: GameSpec):
    """Build a no-DB :class:`StateMachine` over the synthetic league bundle.

    The factory-only ``_hit_rate`` sim-kwarg sets the hit-on-contact share of
    the bundle's batted-ball pool; without it, the league mix applies.
    Referenced by dotted path ``"simulation.batch_runner:rng_driven_machine_factory"``
    on a :class:`GameSpec`; ``simulate_game`` re-seeds both generators from
    the per-game ``seed``.
    """
    from simulation.full_pool_sampler import FullPoolSampler
    from simulation.sim_loop import StateMachine
    from simulation.synthetic_bundle import league_artifacts

    kw = spec.sim_kwargs if isinstance(spec.sim_kwargs, dict) else {}
    hit_rate = kw.get("_hit_rate")
    art = league_artifacts(
        inplay_model=_inplay_model_for(float(hit_rate) if hit_rate is not None else None)
    )
    return StateMachine(
        FullPoolSampler(art, np.random.default_rng(seed)),
        rng=np.random.default_rng(seed),
    )


__all__ = [
    # tuning constants
    "SIM_RESULT_TTL_S",
    "SIM_PROP_SET_TTL_S",
    "POOL_QUERY_TTL_S",
    "MAX_WORKER_CEILING",
    # worker-count + seed helpers
    "default_max_workers",
    "derive_seed",
    # the picklable spec + worker
    "GameSpec",
    # the shared-memory zero-copy attach
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
    "JobOutcome",
    # the picklable no-DB factory (the test path)
    "rng_driven_machine_factory",
]
