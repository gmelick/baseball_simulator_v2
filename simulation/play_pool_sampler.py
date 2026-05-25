"""
play_pool_sampler.py
====================
SIM-302 -- PlayPoolSampler API (Phase 3, READ side)
MLB Baseball Simulation Platform

This is the read side of the play-pool architecture defined in
``docs/architecture/2026-05-20-play-pool.md`` (SIM-300, §6).  It hot-loads
the FAISS tiles that the nightly serializer (SIM-301,
``pipeline/batch/play_pool_cache.py``) materializes to disk and serves k-NN
samples to the Phase 4 simulation loop and the SIM-220 backtester.

Tile format read back (must match SIM-301's ``write_tile`` byte-for-byte):

* ``<pool_dir>/<season>/<pitcher_id>/<bat_hand>.faiss``     -- FAISS IndexFlatL2,
      read with ``faiss.read_index`` (SIM-301 writes it via
      ``faiss.serialize_index(index).tobytes()``; the on-disk bytes are exactly
      what ``read_index`` parses).
* ``<...>/<bat_hand>.rowids.npy``  -- int64 ``np.save`` array, FAISS vector i ->
      ``sim.pitch_pool`` / ``sim.outcome_pool`` row id (``pitch_id``).
* ``<...>/<bat_hand>.faiss.meta``  -- JSON sidecar (§4.3); we read
      ``n_source_rows`` (fall-back trigger), ``n_vectors``, ``season``,
      ``build_timestamp`` (reload trigger), ``pool``, ``dim``.
* ``<season>/<pitcher_id=0>/<bat_hand>.faiss`` -- league-average fall-back pitch
      tile; ``<season>/battedball/<bat_hand>.faiss`` -- batted-ball tile.
* ``bat_hand`` is the single uppercase char ``L`` / ``R`` (no ``S`` tile).

The sampler is the **only** component that converts a FAISS L2 distance into a
sampling weight, keeping the engines distance-pure (HANDOFF §4 / spec §4.2).
The tile stores no weights.  Per the SIM-111 query contract §8 (SIM-076), the
final per-neighbour weight is the **product** of the distance weight and the
neighbour's per-row ``recency_weight``, normalized over the k neighbours::

    w_i = (1 / (d_i + EPS)) * recency_weight_i        (then normalized)

Distance gives "how similar"; ``recency_weight`` gives "how recent".  When a
row's ``recency_weight`` is absent (or None) it defaults to ``1.0`` so the
weighting is a strict generalization of the pure-distance behaviour.

Outcome payloads
----------------
A sampled FAISS vector maps (via ``rowids``) to a source ``pitch_id``.  The
payload (the pitch outcome string, or the batted-ball event type) is fetched
through an injectable ``outcome_fetch`` callable so the sampler is testable
without a live DB.  In production the default fetch reads a read-only DuckDB
connection (``sim.pitch_pool.outcome_type`` for pitches,
``sim.outcome_pool.events`` for batted balls); in tests an in-memory dict or a
tiny temp DuckDB can be injected instead.

Recency weights
---------------
The per-row ``recency_weight`` column (``sim.pitch_pool`` / ``sim.outcome_pool``,
SIM-076) is fetched the same way: an injectable ``recency_fetch`` callable
mirroring ``outcome_fetch``, with a lazy read-only DuckDB default.  Missing or
None values resolve to ``1.0``.
"""

from __future__ import annotations

import json
import os
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

# FAISS is an optional runtime dependency, guarded exactly like the geometric
# engines (SIM-041 / SIM-042) and the SIM-301 builder so importing this module
# never hard-crashes on a box without faiss-cpu installed.
try:  # pragma: no cover -- import-time concern
    import faiss

    _FAISS_AVAILABLE = True
except ImportError as e:  # pragma: no cover
    faiss = None  # type: ignore[assignment]
    _FAISS_AVAILABLE = False
    _FAISS_IMPORT_ERROR = e

# DuckDB is only needed for the production outcome-payload fetch; the sampler is
# fully usable in tests with an injected fetch and no DuckDB connection.
try:  # pragma: no cover -- import-time concern
    import duckdb

    _DUCKDB_AVAILABLE = True
except ImportError as e:  # pragma: no cover
    duckdb = None  # type: ignore[assignment]
    _DUCKDB_AVAILABLE = False
    _DUCKDB_IMPORT_ERROR = e


# ============================================================================
# Config / constants  (mirror the SIM-301 builder so the contract stays aligned)
# ============================================================================

#: Tiles with fewer than this many distinct source rows fall forward to the
#: pitcher_id=0 league-average tile (spec §3 / §6.2).  Must equal the builder's
#: ``MIN_TILE_ROWS``.
MIN_TILE_ROWS = 50

#: Sentinel pitcher_id for the league-average fall-back pitch tile (§3, §4.1).
FALLBACK_PITCHER_ID = 0

#: Distance->weight epsilon: ``w_i = 1 / (d_i + EPS)`` (spec §6.2).
EPS = 1e-6

#: Default per-row recency weight when the column is absent / None (SIM-076).
#: Using 1.0 makes the recency-aware weighting a strict generalization of the
#: pure-distance behaviour (contract §8).
DEFAULT_RECENCY_WEIGHT = 1.0

#: Pool identifiers used in paths + meta (match the builder).
POOL_PITCH = "pitch"
POOL_BATTEDBALL = "battedball"

#: Default play-pool root, overridable via env (§4.1 / §6.1).
DEFAULT_POOL_DIR = os.environ.get("BASEBALL_PLAY_POOL_DIR", "/data/play_pool")

#: Default DuckDB path (matches the builder + profile computor default).
DEFAULT_DUCKDB_PATH = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")

#: Default outcome column for each pool's production DuckDB fetch.  The pitch
#: pool's terminal pitch classification is ``outcome_type`` (ball,
#: called_strike, swinging_strike, foul, in_play); the batted-ball event is the
#: PA result ``events`` (single, double, field_out, ...).  Both tables key on
#: ``pitch_id`` (the value stored in ``.rowids.npy``).
PITCH_OUTCOME_COLUMN = "outcome_type"
BATTEDBALL_OUTCOME_COLUMN = "events"

#: Recency-weight column on both pool tables (SIM-076 / schema 02).  Keyed on
#: ``pitch_id`` like the outcome columns.
RECENCY_WEIGHT_COLUMN = "recency_weight"

#: Returned outcome key names (spec §6.2 result shape).
_PITCH_OUTCOME_KEY = "pitch_outcome"
_BATTEDBALL_OUTCOME_KEY = "event"


# Type of an injectable outcome fetch: (pool, [row_id, ...]) -> {row_id: outcome}
OutcomeFetch = Callable[[str, "list[int]"], "dict[int, str]"]

# Type of an injectable recency fetch: (pool, [row_id, ...]) -> {row_id: weight}
RecencyFetch = Callable[[str, "list[int]"], "dict[int, float]"]


# ============================================================================
# Data structures
# ============================================================================


@dataclass(slots=True)
class TileHandle:
    """A loaded tile: FAISS index + rowids + meta + its resolved cache key.

    ``label`` is the spec's ``"<season>/<pitcher_id>/<bat_hand>"`` (pitch) or
    ``"<season>/battedball/<bat_hand>"`` (battedball) string used in the sample
    result's ``"tile"`` field.
    """

    index: Any  # faiss.IndexFlatL2
    rowids: NDArray[np.int64]
    meta: dict
    pool: str
    label: str
    is_fallback: bool  # True when this is a pitcher_id=0 tile

    @property
    def n_vectors(self) -> int:
        return int(self.rowids.shape[0])


# ============================================================================
# Path resolution (mirrors SIM-301 TileKey on disk)
# ============================================================================


def _pitch_dir(pool_dir: str, season: int, pitcher_id: int) -> str:
    return os.path.join(pool_dir, str(season), str(pitcher_id))


def _battedball_dir(pool_dir: str, season: int) -> str:
    return os.path.join(pool_dir, str(season), POOL_BATTEDBALL)


def _faiss_path(tile_dir: str, bat_hand: str) -> str:
    return os.path.join(tile_dir, f"{bat_hand}.faiss")


def _meta_path(tile_dir: str, bat_hand: str) -> str:
    return os.path.join(tile_dir, f"{bat_hand}.faiss.meta")


def _rowids_path(tile_dir: str, bat_hand: str) -> str:
    return os.path.join(tile_dir, f"{bat_hand}.rowids.npy")


def _read_meta(meta_path: str) -> dict | None:
    try:
        with open(meta_path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _normalize_bat_hand(bat_hand: str) -> str:
    """Coerce to the single uppercase ``L`` / ``R`` tile encoding (§4.1)."""
    if bat_hand is None:
        raise ValueError("bat_hand is required (expected 'L' or 'R').")
    bh = str(bat_hand).strip().upper()
    if bh not in ("L", "R"):
        raise ValueError(f"bat_hand must be 'L' or 'R' (no 'S' tile exists); got {bat_hand!r}.")
    return bh


# ============================================================================
# PlayPoolSampler
# ============================================================================


class PlayPoolSampler:
    """Read-side play-pool sampler (spec §6).

    Holds an LRU cache of loaded tiles (FAISS index + rowids + meta) and a
    read-only DuckDB connection for outcome payloads.  ``rng`` is injectable
    (a ``numpy.random.Generator``) so tests get reproducible draws.
    """

    def __init__(
        self,
        pool_dir: str | os.PathLike = None,
        duckdb_path: str | os.PathLike | None = None,
        *,
        max_resident_tiles: int = 256,
        rng: np.random.Generator | None = None,
        outcome_fetch: OutcomeFetch | None = None,
        recency_fetch: RecencyFetch | None = None,
    ) -> None:
        # Resolve defaults at construction time so env vars set by tests
        # (e.g. monkeypatch.setenv) are honoured.
        self.pool_dir = os.fspath(
            pool_dir
            if pool_dir is not None
            else os.environ.get("BASEBALL_PLAY_POOL_DIR", DEFAULT_POOL_DIR)
        )
        if duckdb_path is not None:
            self.duckdb_path = os.fspath(duckdb_path)
        else:
            self.duckdb_path = os.environ.get("BASEBALL_DUCKDB_PATH", DEFAULT_DUCKDB_PATH)

        if max_resident_tiles < 1:
            raise ValueError("max_resident_tiles must be >= 1.")
        self.max_resident_tiles = int(max_resident_tiles)

        self.rng = rng if rng is not None else np.random.default_rng()

        # LRU of resident tiles: cache_key -> TileHandle.  OrderedDict gives us
        # O(1) move-to-end (mark recently used) + popitem(last=False) (evict LRU).
        self._lru: OrderedDict[tuple, TileHandle] = OrderedDict()

        # Outcome-payload fetch.  Injectable for tests; lazily-built DuckDB
        # fetch in production.
        self._outcome_fetch: OutcomeFetch | None = outcome_fetch
        # Per-row recency-weight fetch (SIM-076 / contract §8).  Injectable for
        # tests; lazily-built DuckDB fetch in production, sharing ``self._conn``.
        self._recency_fetch: RecencyFetch | None = recency_fetch
        self._conn: duckdb.DuckDBPyConnection | None = None  # lazy read-only DuckDB connection

    # ---- LRU bookkeeping --------------------------------------------------

    @property
    def resident_count(self) -> int:
        """Number of tiles currently held in the LRU (test invariant #6)."""
        return len(self._lru)

    def _cache_get(self, cache_key: tuple) -> TileHandle | None:
        handle = self._lru.get(cache_key)
        if handle is not None:
            self._lru.move_to_end(cache_key)  # mark most-recently-used
        return handle

    def _cache_put(self, cache_key: tuple, handle: TileHandle) -> None:
        self._lru[cache_key] = handle
        self._lru.move_to_end(cache_key)
        # Evict least-recently-used until within the resident cap (§6.1 / §7).
        while len(self._lru) > self.max_resident_tiles:
            self._lru.popitem(last=False)

    # ---- Tile loading + fall-back resolution (§6.2) -----------------------

    def load_tile(
        self,
        pool: str,
        season: int,
        bat_hand: str,
        pitcher_id: int | None = None,
    ) -> TileHandle:
        """Resolve + load the tile for ``(pool, season, bat_hand[, pitcher_id])``
        into the LRU, returning a :class:`TileHandle`.

        Fall-back (pitch pool only): if the specific
        ``(season, pitcher_id, bat_hand)`` tile is missing or its
        ``meta.n_source_rows < MIN_TILE_ROWS``, transparently resolve to the
        ``pitcher_id = 0`` league-average tile for that ``(season, bat_hand)``.
        Idempotent -- repeated calls return the cached handle.
        """
        if not _FAISS_AVAILABLE:  # pragma: no cover -- guarded
            raise RuntimeError(
                "faiss-cpu is not installed; cannot load play-pool tiles. "
                f"Original import error: {_FAISS_IMPORT_ERROR!r}"
            )
        bat_hand = _normalize_bat_hand(bat_hand)
        season = int(season)

        if pool == POOL_BATTEDBALL:
            cache_key = (POOL_BATTEDBALL, season, bat_hand)
            cached = self._cache_get(cache_key)
            if cached is not None:
                return cached
            tile_dir = _battedball_dir(self.pool_dir, season)
            label = f"{season}/{POOL_BATTEDBALL}/{bat_hand}"
            handle = self._load_from_disk(
                cache_key,
                POOL_BATTEDBALL,
                tile_dir,
                bat_hand,
                label,
                is_fallback=False,
            )
            self._cache_put(cache_key, handle)
            return handle

        if pool != POOL_PITCH:
            raise ValueError(f"Unknown pool {pool!r} (expected 'pitch' or 'battedball').")

        if pitcher_id is None:
            raise ValueError("pitcher_id is required for the pitch pool.")
        pitcher_id = int(pitcher_id)

        # SIM-333: a tile attached zero-copy via :meth:`attach_shared_tile` is
        # resident under its OWN (un-resolved) key.  Honour it BEFORE the disk
        # fall-back probe so a shared-memory tile is served without any filesystem
        # stat -- the worker reads the shared buffer, never disk.
        attached = self._cache_get((POOL_PITCH, season, bat_hand, pitcher_id))
        if attached is not None:
            return attached

        # Resolve specific-vs-fall-back; the *resolved* identity is the cache key
        # so a fall-back hit and a direct pitcher_id=0 request share one resident
        # copy.
        use_fallback = self._should_fall_back(season, bat_hand, pitcher_id)
        resolved_pid = FALLBACK_PITCHER_ID if use_fallback else pitcher_id

        cache_key = (POOL_PITCH, season, bat_hand, resolved_pid)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        tile_dir = _pitch_dir(self.pool_dir, season, resolved_pid)
        label = f"{season}/{resolved_pid}/{bat_hand}"
        handle = self._load_from_disk(
            cache_key,
            POOL_PITCH,
            tile_dir,
            bat_hand,
            label,
            is_fallback=use_fallback,
        )
        self._cache_put(cache_key, handle)
        return handle

    def _should_fall_back(self, season: int, bat_hand: str, pitcher_id: int) -> bool:
        """True if the specific pitch tile is missing or too small (< MIN_TILE_ROWS)
        and we must serve the pitcher_id=0 fall-back tile instead (§6.2)."""
        if pitcher_id == FALLBACK_PITCHER_ID:
            return False  # already the fall-back tile; nothing to fall back to
        tile_dir = _pitch_dir(self.pool_dir, season, pitcher_id)
        faiss_path = _faiss_path(tile_dir, bat_hand)
        meta_path = _meta_path(tile_dir, bat_hand)
        rowids_path = _rowids_path(tile_dir, bat_hand)
        if not (
            os.path.exists(faiss_path) and os.path.exists(meta_path) and os.path.exists(rowids_path)
        ):
            return True  # specific tile missing -> fall forward (§3 floor)
        meta = _read_meta(meta_path)
        if meta is None:
            return True
        # Sub-threshold standalone tiles should not exist (the builder folds
        # them into pitcher_id=0), but honour the floor defensively anyway.
        return int(meta.get("n_source_rows", 0)) < MIN_TILE_ROWS

    def _load_from_disk(
        self,
        cache_key: tuple,
        pool: str,
        tile_dir: str,
        bat_hand: str,
        label: str,
        *,
        is_fallback: bool,
    ) -> TileHandle:
        faiss_path = _faiss_path(tile_dir, bat_hand)
        meta_path = _meta_path(tile_dir, bat_hand)
        rowids_path = _rowids_path(tile_dir, bat_hand)

        if not os.path.exists(faiss_path):
            raise FileNotFoundError(
                f"Play-pool tile not found: {faiss_path} "
                f"(pool={pool} label={label}). Build it with SIM-301 first."
            )
        index = faiss.read_index(faiss_path)
        rowids = np.load(rowids_path, allow_pickle=False).astype(np.int64, copy=False)
        meta = _read_meta(meta_path) or {}

        return TileHandle(
            index=index,
            rowids=rowids,
            meta=meta,
            pool=pool,
            label=label,
            is_fallback=is_fallback,
        )

    # ---- SIM-333: shared-memory zero-copy ATTACH path ---------------------
    #
    # SIM-281 D2 / SIM-114 §5: a tile's read-only buffers (the flat float32 vector
    # block + the int64 rowids) are published ONCE by the parent into named
    # ``multiprocessing.shared_memory`` segments; a worker ATTACHES them zero-copy
    # and rebuilds the thin FAISS header over the shared vectors here -- so the bulky
    # bytes are resident once, not per worker.  The disk path above stays the
    # FALLBACK (used whenever no shared segment is supplied for a tile).

    def attach_shared_tile(
        self,
        pool: str,
        season: int,
        bat_hand: str,
        *,
        vectors: NDArray,
        rowids: NDArray,
        meta: dict | None = None,
        pitcher_id: int | None = None,
        is_fallback: bool = False,
        label: str | None = None,
    ) -> TileHandle:
        """Build + cache a :class:`TileHandle` over SHARED-MEMORY buffers (zero-copy).

        ``vectors`` is the float32 ``n×dim`` view over the shared FAISS-tile vector
        block; ``rowids`` is the int64 view over the shared rowids buffer (both
        typically obtained from
        :func:`simulation.batch_runner.get_shared_view`).  A flat ``IndexFlatL2`` is
        reconstructed over ``vectors`` -- the index HEADER is thin and per-worker
        (SIM-281 D2.2), but the ``n×dim`` float32 DATA behind it is the shared buffer
        (FAISS ``add`` is the one place a copy could occur; see the contract note
        below), so the bytes that matter (the rowids + the shared source vectors)
        are not duplicated per worker.

        IMPORTANT (FAISS caveat, SIM-114 §5): ``faiss.IndexFlatL2.add`` copies the
        vectors into FAISS's own storage, so the *index's* float32 copy is NOT
        zero-copy.  What IS shared zero-copy is (a) the int64 ``rowids`` (the
        per-vector -> ``pitch_id`` map, which the hot path reads directly) and (b)
        the source ``vectors`` buffer itself, attached once and reused to seed every
        worker's index rather than each worker re-reading + re-deserializing the tile
        from disk.  Since flat-tile data under the LRU cap is ~64 MB (SIM-280 §3),
        even the per-worker FAISS copies stay within the ~165 MB private budget; the
        big *non-FAISS* payload (situation KDTree ~88 MB, RBF ~18 MB) is shared via
        plain numpy views with no such copy, which is where the ≤2 GB headroom comes
        from.  Set ``vectors=None`` to attach a rowids-only handle when the caller
        will supply its own index.
        """
        if not _FAISS_AVAILABLE:  # pragma: no cover -- guarded
            raise RuntimeError(
                "faiss-cpu is not installed; cannot attach play-pool tiles. "
                f"Original import error: {_FAISS_IMPORT_ERROR!r}"
            )
        bat_hand = _normalize_bat_hand(bat_hand)
        season = int(season)
        meta = dict(meta) if meta else {}

        # Zero-copy int64 view of the shared rowids buffer (the hot-path lookup).
        rowids_view = np.asarray(rowids).reshape(-1)
        if rowids_view.dtype != np.int64:
            # A non-int64 shared buffer would force a copy; the publisher should
            # share int64 rowids, but coerce defensively without mutating shared mem.
            rowids_view = rowids_view.astype(np.int64, copy=True)

        index = None
        if vectors is not None:
            vec = np.ascontiguousarray(np.asarray(vectors), dtype=np.float32)
            if vec.ndim != 2:
                raise ValueError(
                    f"shared tile vectors must be 2-D (n x dim); got shape {vec.shape}."
                )
            dim = int(vec.shape[1])
            index = faiss.IndexFlatL2(dim)
            index.add(vec)  # FAISS copies into its own storage (see caveat above)

        if pool == POOL_BATTEDBALL:
            cache_key = (POOL_BATTEDBALL, season, bat_hand)
            resolved_label = label or f"{season}/{POOL_BATTEDBALL}/{bat_hand}"
        elif pool == POOL_PITCH:
            resolved_pid = (
                FALLBACK_PITCHER_ID if (is_fallback or pitcher_id is None) else int(pitcher_id)
            )
            cache_key = (POOL_PITCH, season, bat_hand, resolved_pid)
            resolved_label = label or f"{season}/{resolved_pid}/{bat_hand}"
        else:
            raise ValueError(f"Unknown pool {pool!r} (expected 'pitch' or 'battedball').")

        handle = TileHandle(
            index=index,
            rowids=rowids_view,
            meta=meta,
            pool=pool,
            label=resolved_label,
            is_fallback=bool(is_fallback),
        )
        self._cache_put(cache_key, handle)
        return handle

    # ---- k-NN search + distance->weight (§6.2) ----------------------------

    def _knn(self, handle: TileHandle, query_vec: NDArray, k: int):
        """Run the k-NN search, returning (neighbor_positions, weights).

        ``weights`` are the normalized ``1/(d+EPS)`` distance weights over the k
        neighbours -- the ONE place distance -> weight conversion happens.
        ``k`` is clamped to the tile's vector count (never raises, test #7).
        Also returns the raw distances aligned to the neighbour positions.
        """
        n = handle.n_vectors
        if n == 0:
            raise ValueError(f"Tile {handle.label} has zero vectors; cannot sample.")
        k_eff = max(1, min(int(k), n))

        q = np.ascontiguousarray(np.asarray(query_vec, dtype=np.float32).reshape(1, -1))
        distances, positions = handle.index.search(q, k_eff)
        distances = distances[0]
        positions = positions[0]

        # FAISS pads with -1 when fewer than k_eff results exist; drop padding.
        valid = positions >= 0
        distances = distances[valid]
        positions = positions[valid]

        # IndexFlatL2 returns squared L2 distances (>= 0); clamp tiny negatives
        # from float error before the weight transform.
        distances = np.clip(distances, 0.0, None)
        weights = 1.0 / (distances + EPS)
        total = float(weights.sum())
        if total <= 0.0 or not np.isfinite(total):  # pragma: no cover -- degenerate
            weights = np.full(weights.shape, 1.0 / len(weights), dtype=np.float64)
        else:
            weights = weights / total
        return positions, weights, distances

    def _apply_recency(self, handle: TileHandle, positions: NDArray, weights: NDArray) -> NDArray:
        """Multiply the normalized distance ``weights`` by each neighbour's
        per-row ``recency_weight`` and renormalize (contract §8 / SIM-076).

        ``positions`` are FAISS vector positions; ``handle.rowids`` maps each to
        a source ``pitch_id`` whose recency weight is fetched via
        :meth:`_fetch_recency`.  Missing / None weights default to
        ``DEFAULT_RECENCY_WEIGHT`` (1.0), making this a strict generalization of
        the pure-distance behaviour.  The distance->weight conversion in
        :meth:`_knn` stays the only place weights are *formed*; this only scales
        them, so the engines remain distance-pure.
        """
        row_ids = [int(handle.rowids[int(p)]) for p in positions]
        recency = self._fetch_recencies(handle.pool, row_ids)
        recency = np.asarray(recency, dtype=np.float64)

        scaled = np.asarray(weights, dtype=np.float64) * recency
        total = float(scaled.sum())
        if total <= 0.0 or not np.isfinite(total):  # pragma: no cover -- degenerate
            # All-zero / non-finite product (e.g. recency all 0) -> fall back to
            # the un-scaled distance weights so we never produce a bad p-vector.
            return np.asarray(weights, dtype=np.float64)
        return scaled / total

    def _draw_one(self, positions: NDArray, weights: NDArray):
        """Draw one neighbour index (into ``positions``) via rng.choice(p=weights)."""
        choice_idx = int(self.rng.choice(len(positions), p=weights))
        return choice_idx

    # ---- Public sampling: pitch (§6.2) ------------------------------------

    def sample_pitch(
        self,
        pitcher_id: int,
        bat_hand: str,
        season: int,
        query_vec: NDArray,
        *,
        k: int = 25,
    ) -> dict:
        """k-NN sample one historical pitch outcome from the pitch tile.

        Returns ``{"row_id", "pitch_outcome", "distance", "weight", "tile",
        "fellback"}`` (spec §6.2).  ``fellback`` is True when the pitcher_id=0
        fall-back tile served the request; ``k`` is clamped to the tile size.
        """
        handle = self.load_tile(POOL_PITCH, season, bat_hand, pitcher_id=pitcher_id)
        positions, weights, distances = self._knn(handle, query_vec, k)
        weights = self._apply_recency(handle, positions, weights)
        choice_idx = self._draw_one(positions, weights)

        pos = int(positions[choice_idx])
        row_id = int(handle.rowids[pos])
        outcome = self._fetch_outcome(POOL_PITCH, row_id)

        return {
            "row_id": row_id,
            _PITCH_OUTCOME_KEY: outcome,
            "distance": float(distances[choice_idx]),
            "weight": float(weights[choice_idx]),
            "tile": handle.label,
            "fellback": bool(handle.is_fallback),
        }

    # ---- Public sampling: batted ball (§6.2) ------------------------------

    def sample_batted_ball(
        self,
        bat_hand: str,
        season: int,
        query_vec: NDArray,
        *,
        k: int = 25,
        return_distribution: bool = False,
    ) -> dict:
        """k-NN sample (or distribution over) batted-ball events.

        Default: draw one event, returning the same shape as
        :meth:`sample_pitch` but with an ``"event"`` outcome key.

        ``return_distribution=True``: instead of a single draw, return the
        **outcome distribution** over the k neighbours -- a probability dict
        keyed by event type (e.g. ``{"single": 0.31, "out": 0.55, ...}``)
        computed from the normalized distance weights.  The probabilities sum to
        1.0 over the closed set of event types present in the neighbourhood
        (spec §6.2; the SIM-220 backtester consumes this form).
        """
        handle = self.load_tile(POOL_BATTEDBALL, season, bat_hand)
        positions, weights, distances = self._knn(handle, query_vec, k)
        weights = self._apply_recency(handle, positions, weights)

        if return_distribution:
            return self._event_distribution(handle, positions, weights)

        choice_idx = self._draw_one(positions, weights)
        pos = int(positions[choice_idx])
        row_id = int(handle.rowids[pos])
        outcome = self._fetch_outcome(POOL_BATTEDBALL, row_id)

        return {
            "row_id": row_id,
            _BATTEDBALL_OUTCOME_KEY: outcome,
            "distance": float(distances[choice_idx]),
            "weight": float(weights[choice_idx]),
            "tile": handle.label,
            "fellback": bool(handle.is_fallback),
        }

    def _event_distribution(self, handle: TileHandle, positions: NDArray, weights: NDArray) -> dict:
        """Collapse the k neighbours' weights onto their event types, normalized
        to a probability dict summing to 1.0 (spec §6.2 / test invariant #4)."""
        row_ids = [int(handle.rowids[int(p)]) for p in positions]
        outcomes = self._fetch_outcomes(POOL_BATTEDBALL, row_ids)

        dist: dict[str, float] = {}
        for outcome, w in zip(outcomes, weights, strict=False):
            dist[outcome] = dist.get(outcome, 0.0) + float(w)

        # ``weights`` already sum to 1.0; re-normalize to absorb float drift so
        # the closed-vocab sum is 1.0 +/- 1e-9 even after the group-by.
        total = sum(dist.values())
        if total > 0.0:
            dist = {ev: p / total for ev, p in dist.items()}
        return dist

    # ---- reload_recent (§6.2 / §5) ----------------------------------------

    def reload_recent(self, current_season: int) -> int:
        """Re-stat every resident tile whose ``meta.season == current_season``;
        for any whose on-disk ``build_timestamp`` is newer than the resident
        copy, evict + reload.  Returns the number of tiles reloaded (§5)."""
        current_season = int(current_season)
        reloaded = 0
        # Snapshot keys: we mutate the LRU while iterating.
        for cache_key in list(self._lru.keys()):
            handle = self._lru.get(cache_key)
            if handle is None:
                continue
            if int(handle.meta.get("season", -1)) != current_season:
                continue

            tile_dir = self._tile_dir_for_handle(handle)
            bat_hand = self._bat_hand_for_key(cache_key)
            meta_path = _meta_path(tile_dir, bat_hand)
            on_disk_meta = _read_meta(meta_path)
            if on_disk_meta is None:
                continue

            disk_ts = str(on_disk_meta.get("build_timestamp", ""))
            resident_ts = str(handle.meta.get("build_timestamp", ""))
            if disk_ts > resident_ts:
                # Evict + reload in place, preserving the fall-back flag + label.
                self._lru.pop(cache_key, None)
                fresh = self._load_from_disk(
                    cache_key,
                    handle.pool,
                    tile_dir,
                    bat_hand,
                    handle.label,
                    is_fallback=handle.is_fallback,
                )
                self._cache_put(cache_key, fresh)
                reloaded += 1
        return reloaded

    def _tile_dir_for_handle(self, handle: TileHandle) -> str:
        season = int(handle.meta.get("season"))
        if handle.pool == POOL_BATTEDBALL:
            return _battedball_dir(self.pool_dir, season)
        pitcher_id = int(handle.meta.get("pitcher_id", FALLBACK_PITCHER_ID))
        return _pitch_dir(self.pool_dir, season, pitcher_id)

    @staticmethod
    def _bat_hand_for_key(cache_key: tuple) -> str:
        # cache_key is (pool, season, bat_hand[, pid]); bat_hand is index 2.
        return cache_key[2]

    # ---- Outcome-payload fetch (injectable / DuckDB) ----------------------

    def _fetch_outcome(self, pool: str, row_id: int) -> str:
        """Single-row outcome lookup (delegates to the batch fetch)."""
        return self._fetch_outcomes(pool, [row_id])[0]

    def _fetch_outcomes(self, pool: str, row_ids: list[int]) -> list[str]:
        """Resolve a list of row ids to their outcome strings, order-preserving.

        Uses the injected ``outcome_fetch`` if present; otherwise a read-only
        DuckDB connection (lazily opened).  Unknown / missing rows resolve to
        the string ``"unknown"`` so the sampler never raises on a sparse DB.
        """
        if not row_ids:
            return []
        if self._outcome_fetch is not None:
            mapping = self._outcome_fetch(pool, list(row_ids))
        else:
            mapping = self._duckdb_fetch(pool, list(row_ids))
        return [str(mapping.get(int(rid), "unknown")) for rid in row_ids]

    def _duckdb_fetch(self, pool: str, row_ids: list[int]) -> dict[int, str]:
        if not _DUCKDB_AVAILABLE:  # pragma: no cover -- guarded
            raise RuntimeError(
                "duckdb is not installed and no outcome_fetch was injected. "
                f"Original import error: {_DUCKDB_IMPORT_ERROR!r}"
            )
        if self._conn is None:
            # Read-only connection for fetching outcome payloads (§6.1).
            self._conn = duckdb.connect(self.duckdb_path, read_only=True)

        if pool == POOL_PITCH:
            table, col = "sim.pitch_pool", PITCH_OUTCOME_COLUMN
        else:
            table, col = "sim.outcome_pool", BATTEDBALL_OUTCOME_COLUMN

        placeholders = ", ".join("?" for _ in row_ids)
        rows = self._conn.execute(
            f"SELECT pitch_id, {col} FROM {table} WHERE pitch_id IN ({placeholders})",
            [int(r) for r in row_ids],
        ).fetchall()
        return {int(rid): ("unknown" if val is None else str(val)) for rid, val in rows}

    # ---- Recency-weight fetch (injectable / DuckDB) -----------------------

    def _fetch_recencies(self, pool: str, row_ids: list[int]) -> list[float]:
        """Resolve a list of row ids to their ``recency_weight`` values,
        order-preserving (contract §8 / SIM-076).

        Resolution order:
          * injected ``recency_fetch`` -> use it;
          * else an ``outcome_fetch`` was injected (no-DB / test mode) ->
            uniform ``DEFAULT_RECENCY_WEIGHT`` (recency neutral);
          * else -> a read-only DuckDB connection (lazily opened, shared with
            the outcome fetch) reading ``recency_weight``.
        Unknown / missing / None weights resolve to
        ``DEFAULT_RECENCY_WEIGHT`` (1.0) so the sampler never raises on a sparse
        DB and behaves identically to the pure-distance path when recency is
        absent.
        """
        if not row_ids:
            return []
        if self._recency_fetch is not None:
            mapping = self._recency_fetch(pool, list(row_ids))
        elif self._outcome_fetch is not None:
            # An outcome source was injected but no recency source: treat recency
            # as uniform (all 1.0) so the weighting is a strict no-op generalizing
            # the pure-distance path.  This keeps injected-fetch tests (and any
            # caller that supplies outcomes without a DB) recency-neutral.
            mapping = {}
        else:
            mapping = self._duckdb_fetch_recency(pool, list(row_ids))
        out: list[float] = []
        for rid in row_ids:
            val = mapping.get(int(rid))
            if val is None:
                out.append(DEFAULT_RECENCY_WEIGHT)
            else:
                out.append(float(val))
        return out

    def _duckdb_fetch_recency(self, pool: str, row_ids: list[int]) -> dict[int, float]:
        if not _DUCKDB_AVAILABLE:  # pragma: no cover -- guarded
            raise RuntimeError(
                "duckdb is not installed and no recency_fetch was injected. "
                f"Original import error: {_DUCKDB_IMPORT_ERROR!r}"
            )
        if self._conn is None:
            # Read-only connection, shared with the outcome-payload fetch (§6.1).
            self._conn = duckdb.connect(self.duckdb_path, read_only=True)

        table = "sim.pitch_pool" if pool == POOL_PITCH else "sim.outcome_pool"

        # The production schema (02_duckdb_schema.sql, SIM-076) always carries
        # ``recency_weight`` (DEFAULT 1.0), but a pool DB predating that column is
        # recency-neutral: return empty so every row defaults to 1.0 rather than
        # raising on a missing column.
        if not self._table_has_recency(table):
            return {}

        placeholders = ", ".join("?" for _ in row_ids)
        rows = self._conn.execute(
            f"SELECT pitch_id, {RECENCY_WEIGHT_COLUMN} FROM {table} "
            f"WHERE pitch_id IN ({placeholders})",
            [int(r) for r in row_ids],
        ).fetchall()
        return {
            int(rid): (DEFAULT_RECENCY_WEIGHT if val is None else float(val)) for rid, val in rows
        }

    def _table_has_recency(self, qualified_table: str) -> bool:
        """True if ``qualified_table`` ('sim.<name>') has a ``recency_weight``
        column.  Cached per table on the instance to keep the per-draw cost zero
        after the first probe."""
        cache = getattr(self, "_recency_col_cache", None)
        if cache is None:
            cache = {}
            self._recency_col_cache = cache
        if qualified_table in cache:
            return cache[qualified_table]
        schema, _, name = qualified_table.partition(".")
        try:
            found = self._conn.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ? AND column_name = ? "
                "LIMIT 1",
                [schema, name, RECENCY_WEIGHT_COLUMN],
            ).fetchone()
            has = found is not None
        except Exception:  # pragma: no cover -- defensive
            has = False
        cache[qualified_table] = has
        return has

    # ---- lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Release the DuckDB connection and drop resident tiles."""
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
        self._lru.clear()

    def __enter__(self) -> PlayPoolSampler:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


__all__ = [
    "PlayPoolSampler",
    "TileHandle",
    "MIN_TILE_ROWS",
    "FALLBACK_PITCHER_ID",
    "EPS",
    "DEFAULT_RECENCY_WEIGHT",
    "POOL_PITCH",
    "POOL_BATTEDBALL",
]
