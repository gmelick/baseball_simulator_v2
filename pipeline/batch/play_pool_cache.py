"""
play_pool_cache.py
==================
SIM-301 — Play-Pool Nightly Cache Serializer (Phase 3, BUILD side)
MLB Baseball Simulation Platform

This is the build side of the play-pool architecture defined in
``docs/architecture/2026-05-20-play-pool.md`` (SIM-300).  It reads the
pre-filtered pitch / batted-ball pools out of DuckDB and materializes one
FAISS ``IndexFlatL2`` tile per ``(season, pitcher_id, bat_hand)`` triple
(pitch pool) or ``(season, bat_hand)`` pair (batted-ball pool), plus a
``.rowids.npy`` row-id map and a ``.meta`` JSON sidecar, under

    ${BASEBALL_PLAY_POOL_DIR:-/data/play_pool}/<season>/<pitcher_id>/<bat_hand>.faiss

The read side (SIM-302, ``simulation/play_pool_sampler.py``) hot-loads
these tiles and serves k-NN samples to the Phase 4 simulation loop.

Contract highlights (see the spec for the authoritative version):

* §3  Pre-filter: pitch tiles key on ``pitcher_id + bat_hand``; batted-ball
      tiles key on ``bat_hand`` only.  Tiles with fewer than
      ``MIN_TILE_ROWS`` (50) outcome rows are NOT materialized standalone;
      their rows fall forward into the ``pitcher_id = 0`` league-average
      fall-back tile for that ``(season, bat_hand)``.
* §3.1 Spray column: the batted-ball spray dimension reuses the SIM-042
      engine's ``_select_spray_column()`` (prefer ``pull_relative_spray_angle``,
      fall back to raw ``spray_angle`` with an INFO log).
* §4  Disk layout, ``.meta`` schema, atomic writes (``*.tmp`` + fsync +
      ``os.replace``).
* §4.4 Idempotency: a tile is rebuilt iff a file is missing, the source
      watermark advanced past ``meta.source_max_updated_at``, the
      ``builder_version`` changed, or the recency-boost flags changed.
* §5  Recency boost: rows from the last ``RECENCY_BOOST_SEASONS`` (2)
      seasons are duplicated once before indexing.  Default ON in
      production; ``--no-recency-boost`` produces deterministic builds.

CLI
---
    python -m pipeline.batch.play_pool_cache \
        --duckdb-path /data/baseball_sim.duckdb \
        --pool-dir /data/play_pool \
        [--seasons 2023 2024] [--pool pitch|battedball|all] [--no-recency-boost]

Nightly cron (runs AFTER pipeline/batch/player_profile_computor.py).  See
the ``play-pool-cache`` Make target and the documented crontab line at the
bottom of this module.

Deviations from the SIM-300 spec (documented for the PM):

* The production ``sim.pitch_pool`` / ``sim.outcome_pool`` tables carry no
  ``updated_at`` column (see ``db/schemas/02_duckdb_schema.sql``).  The
  freshness watermark (``source_max_updated_at``, §4.4 rule 2) therefore
  prefers an ``updated_at`` column when present and otherwise falls back to
  ``MAX(game_date)`` rendered as an ISO timestamp — newer games still
  advance the watermark, so the staleness check stays sound.
* The spec uses ``bat_hand`` for the batter-handedness pre-filter key; the
  production schema spells this column ``stand``.  The builder selects
  ``bat_hand`` if it exists (the SIM-051 fixtures use it) and otherwise
  ``stand``, so both shapes build correctly.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import duckdb
import numpy as np
from numpy.typing import NDArray

# FAISS is an optional runtime dependency, guarded exactly like the two
# geometric engines (SIM-041 / SIM-042) so importing this module never
# hard-crashes on a box without faiss-cpu installed.
try:  # pragma: no cover — import-time concern
    import faiss

    _FAISS_AVAILABLE = True
except ImportError as e:  # pragma: no cover
    faiss = None  # type: ignore[assignment]
    _FAISS_AVAILABLE = False
    _FAISS_IMPORT_ERROR = e

# Reuse the batted-ball engine's spray-column discipline rather than
# re-implementing it (spec §3.1).  This import is feature-symmetric with the
# engine's own optional-FAISS guard, so it succeeds even when faiss is absent
# (the helper is a @staticmethod that only touches the DuckDB connection).
from similarity.engines.batted_ball_similarity import (
    BattedBallSimilarityEngine,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("play_pool_cache")


# ============================================================================
# Config / constants
# ============================================================================

#: Bump this when the build logic changes in a way that invalidates tiles on
#: disk.  A mismatch with ``meta.builder_version`` forces a rebuild (§4.4#3).
BUILDER_VERSION = "sim301.1"

#: ``.meta`` schema version (§4.3).
SCHEMA_VERSION = 1

#: Tiles smaller than this fall forward into the pitcher_id=0 fall-back tile
#: (§3 pool-sizing floor).
MIN_TILE_ROWS = 50

#: Sentinel pitcher_id for the league-average fall-back pitch tile (§3, §4.1).
FALLBACK_PITCHER_ID = 0

#: How many most-recent seasons get the recency boost (2× replication, §5).
RECENCY_BOOST_SEASONS = 2

#: Default play-pool root, overridable via env (§4.1).
DEFAULT_POOL_DIR = os.environ.get("BASEBALL_PLAY_POOL_DIR", "/data/play_pool")

#: Default DuckDB path (matches the profile computor's default).
DEFAULT_DUCKDB_PATH = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")

#: Pool identifiers used in paths + meta.
POOL_PITCH = "pitch"
POOL_BATTEDBALL = "battedball"

#: Pitch fingerprint = 10-dim pitch-to-pitch vector (SIM-041).  Order MUST
#: match the SIM-302 sampler's query-vector contract.
PITCH_FEATURES = [
    "velo", "ivb", "hb", "spin_rate", "spin_axis",
    "release_x", "release_z", "release_ext", "plate_x", "plate_z",
]
PITCH_DIM = len(PITCH_FEATURES)

#: Batted-ball fingerprint = 3-dim launch vector (SIM-042): exit_velo,
#: launch_angle, spray.  The spray column is selected at build time.
BATTEDBALL_DIM = 3

#: Candidate column names for the batter-handedness pre-filter key.  The spec
#: calls it ``bat_hand``; the production DDL spells it ``stand``.
BAT_HAND_COLUMN_CANDIDATES = ("bat_hand", "stand")

#: Candidate freshness-watermark columns, most-preferred first.
WATERMARK_COLUMN_CANDIDATES = ("updated_at", "game_date")

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


# ============================================================================
# Data structures
# ============================================================================


@dataclass(slots=True)
class TileKey:
    """Identifies one tile on disk."""

    pool: str           # "pitch" | "battedball"
    season: int
    bat_hand: str       # "L" | "R"
    pitcher_id: int | None = None   # None for battedball; int (incl. 0) for pitch

    def dir_path(self, pool_dir: str) -> str:
        if self.pool == POOL_PITCH:
            return os.path.join(pool_dir, str(self.season), str(self.pitcher_id))
        return os.path.join(pool_dir, str(self.season), POOL_BATTEDBALL)

    def faiss_path(self, pool_dir: str) -> str:
        return os.path.join(self.dir_path(pool_dir), f"{self.bat_hand}.faiss")

    def meta_path(self, pool_dir: str) -> str:
        return os.path.join(self.dir_path(pool_dir), f"{self.bat_hand}.faiss.meta")

    def rowids_path(self, pool_dir: str) -> str:
        return os.path.join(self.dir_path(pool_dir), f"{self.bat_hand}.rowids.npy")

    def label(self) -> str:
        if self.pool == POOL_PITCH:
            return f"{self.season}/{self.pitcher_id}/{self.bat_hand}"
        return f"{self.season}/battedball/{self.bat_hand}"


@dataclass(slots=True)
class TilePayload:
    """The materialized contents of a tile, ready to serialize."""

    vectors: NDArray[np.float32]      # (n_vectors, dim) — includes recency dups
    rowids: NDArray[np.int64]         # (n_vectors,) FAISS row -> source row id
    n_source_rows: int                # distinct source rows before recency dup
    source_max_updated_at: str        # ISO watermark for this tile's rows
    spray_column: str | None          # battedball only


@dataclass(slots=True)
class BuildResult:
    """Returned by :func:`build_play_pool_cache` for callers / tests."""

    rebuilt: int = 0
    skipped: int = 0
    rebuilt_tiles: list[str] = field(default_factory=list)
    skipped_tiles: list[str] = field(default_factory=list)


# ============================================================================
# Schema introspection helpers
# ============================================================================


def _table_columns(conn: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    """Return the set of column names on ``sim.<table>`` (empty on error)."""
    try:
        rows = conn.execute(
            """
            SELECT column_name FROM information_schema.columns
             WHERE table_schema = 'sim' AND table_name = ?
            """,
            [table],
        ).fetchall()
    except Exception:  # pragma: no cover — non-DuckDB / missing catalog
        return set()
    return {r[0] for r in rows}


def _select_bat_hand_column(cols: set[str]) -> str:
    """Pick the batter-handedness column: spec ``bat_hand`` if present, else
    the production ``stand`` column."""
    for cand in BAT_HAND_COLUMN_CANDIDATES:
        if cand in cols:
            return cand
    # Default to the production spelling; the query will surface a clear error
    # if it truly is absent.
    return "stand"


def _select_watermark_expr(cols: set[str]) -> str:
    """Build a SQL expression that yields the per-group freshness watermark as
    an ISO-8601 string.

    Prefers a real ``updated_at`` timestamp; falls back to ``MAX(game_date)``
    when the source tables carry no ``updated_at`` (the current production
    schema — see the module docstring deviation note)."""
    for cand in WATERMARK_COLUMN_CANDIDATES:
        if cand in cols:
            # strftime gives a stable, comparable ISO string in both branches.
            return f"strftime(MAX({cand}), '{_ISO_FMT}')"
    # Last resort: a constant epoch so the tile is at least buildable.
    return f"'{datetime(1970, 1, 1, tzinfo=timezone.utc).strftime(_ISO_FMT)}'"


# ============================================================================
# Watermark planning (one aggregate query per pool, §4.4)
# ============================================================================


def _plan_pitch_tiles(
    conn: duckdb.DuckDBPyConnection,
    seasons: list[int] | None,
) -> tuple[dict[tuple[int, int, str], str], str]:
    """Return ``{(season, pitcher_id, bat_hand): watermark_iso}`` for every
    candidate pitch group, plus the selected bat_hand column name.

    One ``GROUP BY`` aggregate (spec §4.4 / §7): a no-op nightly run costs a
    single grouped query per pool and touches zero ``.faiss`` files.
    """
    cols = _table_columns(conn, "pitch_pool")
    hand_col = _select_bat_hand_column(cols)
    wm_expr = _select_watermark_expr(cols)

    season_filter = ""
    if seasons:
        season_filter = f"WHERE season IN ({', '.join(str(int(s)) for s in seasons)})"

    rows = conn.execute(f"""
        SELECT season, pitcher_id, {hand_col} AS bat_hand,
               COUNT(*) AS n,
               {wm_expr} AS wm
          FROM sim.pitch_pool
        {season_filter}
         GROUP BY season, pitcher_id, {hand_col}
    """).fetchall()

    plan: dict[tuple[int, int, str], str] = {}
    for season, pitcher_id, bat_hand, _n, wm in rows:
        if bat_hand not in ("L", "R"):
            continue  # switch hitters resolved to L/R; no 'S' tile (§4.1)
        plan[(int(season), int(pitcher_id), str(bat_hand))] = str(wm)
    return plan, hand_col


def _plan_battedball_tiles(
    conn: duckdb.DuckDBPyConnection,
    seasons: list[int] | None,
) -> tuple[dict[tuple[int, str], str], str]:
    """Return ``{(season, bat_hand): watermark_iso}`` for the batted-ball pool,
    plus the selected bat_hand column name."""
    cols = _table_columns(conn, "outcome_pool")
    hand_col = _select_bat_hand_column(cols)
    wm_expr = _select_watermark_expr(cols)

    season_filter = ""
    if seasons:
        season_filter = f"WHERE season IN ({', '.join(str(int(s)) for s in seasons)})"

    rows = conn.execute(f"""
        SELECT season, {hand_col} AS bat_hand,
               COUNT(*) AS n,
               {wm_expr} AS wm
          FROM sim.outcome_pool
        {season_filter}
         GROUP BY season, {hand_col}
    """).fetchall()

    plan: dict[tuple[int, str], str] = {}
    for season, bat_hand, _n, wm in rows:
        if bat_hand not in ("L", "R"):
            continue
        plan[(int(season), str(bat_hand))] = str(wm)
    return plan, hand_col


# ============================================================================
# Recency boost (§5)
# ============================================================================


def _apply_recency_boost(
    vectors: NDArray[np.float32],
    rowids: NDArray[np.int64],
    seasons_per_row: NDArray[np.int64],
    build_seasons: list[int],
) -> tuple[NDArray[np.float32], NDArray[np.int64]]:
    """Duplicate each row whose season is within the most-recent
    ``RECENCY_BOOST_SEASONS`` of the build-season set once, before indexing.

    Mirrors the FAISS engines' ``_apply_recency_boost`` (vstack + index
    replication), keeping the boost geometry identical across build + query.
    """
    if vectors.shape[0] == 0 or not build_seasons:
        return vectors, rowids
    recent_cutoff = max(build_seasons) - (RECENCY_BOOST_SEASONS - 1)
    recent_mask = seasons_per_row >= recent_cutoff
    if not recent_mask.any():
        return vectors, rowids
    boosted_vecs = np.vstack([vectors, vectors[recent_mask]])
    boosted_ids = np.concatenate([rowids, rowids[recent_mask]])
    return boosted_vecs.astype(np.float32, copy=False), boosted_ids.astype(np.int64, copy=False)


# ============================================================================
# Pitch-pool tile materialization
# ============================================================================


def _fetch_pitch_group(
    conn: duckdb.DuckDBPyConnection,
    hand_col: str,
    season: int,
    pitcher_ids: list[int],
    bat_hand: str,
    wm_expr: str,
) -> tuple[NDArray[np.float32], NDArray[np.int64], NDArray[np.int64], str]:
    """Stream one tile's rows out of ``sim.pitch_pool``.

    ``pitcher_ids`` is a list so the fall-back tile (all sub-threshold
    pitchers folded into pitcher_id=0) can be fetched in one query.
    """
    feat_cols = ", ".join(PITCH_FEATURES)
    id_list = ", ".join(str(int(p)) for p in pitcher_ids)
    rows = conn.execute(f"""
        SELECT pitch_id, season, {feat_cols}
          FROM sim.pitch_pool
         WHERE season = {int(season)}
           AND {hand_col} = '{bat_hand}'
           AND pitcher_id IN ({id_list})
    """).fetchall()

    n = len(rows)
    vectors = np.empty((n, PITCH_DIM), dtype=np.float32)
    rowids = np.empty(n, dtype=np.int64)
    seasons_per_row = np.empty(n, dtype=np.int64)
    for i, r in enumerate(rows):
        rowids[i] = int(r[0])
        seasons_per_row[i] = int(r[1])
        vectors[i, :] = [_f(v) for v in r[2:]]
    vectors = np.nan_to_num(vectors, nan=0.0).astype(np.float32, copy=False)

    wm = conn.execute(f"""
        SELECT {wm_expr} FROM sim.pitch_pool
         WHERE season = {int(season)}
           AND {hand_col} = '{bat_hand}'
           AND pitcher_id IN ({id_list})
    """).fetchone()
    watermark = str(wm[0]) if wm and wm[0] is not None else _epoch_iso()
    return vectors, rowids, seasons_per_row, watermark


def _build_pitch_payload(
    conn: duckdb.DuckDBPyConnection,
    hand_col: str,
    key: TileKey,
    pitcher_ids: list[int],
    recency_boost: bool,
    build_seasons: list[int],
) -> TilePayload:
    cols = _table_columns(conn, "pitch_pool")
    wm_expr = _select_watermark_expr(cols)
    vectors, rowids, seasons_per_row, watermark = _fetch_pitch_group(
        conn, hand_col, key.season, pitcher_ids, key.bat_hand, wm_expr
    )
    n_source_rows = int(vectors.shape[0])
    if recency_boost:
        vectors, rowids = _apply_recency_boost(
            vectors, rowids, seasons_per_row, build_seasons
        )
    return TilePayload(
        vectors=vectors,
        rowids=rowids,
        n_source_rows=n_source_rows,
        source_max_updated_at=watermark,
        spray_column=None,
    )


# ============================================================================
# Batted-ball-pool tile materialization
# ============================================================================


def _build_battedball_payload(
    conn: duckdb.DuckDBPyConnection,
    hand_col: str,
    spray_col: str,
    key: TileKey,
    recency_boost: bool,
    build_seasons: list[int],
) -> TilePayload:
    wm_cols = _table_columns(conn, "outcome_pool")
    wm_expr = _select_watermark_expr(wm_cols)
    rows = conn.execute(f"""
        SELECT pitch_id, season, exit_velo, launch_angle, {spray_col} AS spray
          FROM sim.outcome_pool
         WHERE season = {int(key.season)}
           AND {hand_col} = '{key.bat_hand}'
           AND exit_velo IS NOT NULL
           AND launch_angle IS NOT NULL
           AND {spray_col} IS NOT NULL
    """).fetchall()

    n = len(rows)
    vectors = np.empty((n, BATTEDBALL_DIM), dtype=np.float32)
    rowids = np.empty(n, dtype=np.int64)
    seasons_per_row = np.empty(n, dtype=np.int64)
    for i, r in enumerate(rows):
        rowids[i] = int(r[0])
        seasons_per_row[i] = int(r[1])
        vectors[i, :] = [_f(r[2]), _f(r[3]), _f(r[4])]
    vectors = np.nan_to_num(vectors, nan=0.0).astype(np.float32, copy=False)

    n_source_rows = int(vectors.shape[0])
    if recency_boost:
        vectors, rowids = _apply_recency_boost(
            vectors, rowids, seasons_per_row, build_seasons
        )

    wm = conn.execute(f"""
        SELECT {wm_expr} FROM sim.outcome_pool
         WHERE season = {int(key.season)}
           AND {hand_col} = '{key.bat_hand}'
           AND exit_velo IS NOT NULL
           AND launch_angle IS NOT NULL
           AND {spray_col} IS NOT NULL
    """).fetchone()
    watermark = str(wm[0]) if wm and wm[0] is not None else _epoch_iso()

    return TilePayload(
        vectors=vectors,
        rowids=rowids,
        n_source_rows=n_source_rows,
        source_max_updated_at=watermark,
        spray_column=spray_col,
    )


# ============================================================================
# Staleness decision (§4.4)
# ============================================================================


def _read_meta(meta_path: str) -> dict | None:
    try:
        with open(meta_path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def is_tile_stale(
    key: TileKey,
    pool_dir: str,
    source_watermark: str,
    recency_boost: bool,
) -> bool:
    """Return True iff the on-disk tile must be rebuilt (spec §4.4).

    Stale iff ANY of:
      1. The .faiss / .meta / .rowids.npy file is missing.
      2. meta.source_max_updated_at is older than the source watermark.
      3. meta.builder_version differs from the running builder.
      4. meta.recency_boost / recency_boost_seasons differ from the request.
    """
    faiss_path = key.faiss_path(pool_dir)
    meta_path = key.meta_path(pool_dir)
    rowids_path = key.rowids_path(pool_dir)

    # Rule 1 — any artifact missing.
    if not (os.path.exists(faiss_path) and os.path.exists(meta_path)
            and os.path.exists(rowids_path)):
        return True

    meta = _read_meta(meta_path)
    if meta is None:
        return True

    # Rule 3 — builder logic changed.
    if meta.get("builder_version") != BUILDER_VERSION:
        return True

    # Rule 4 — recency policy changed.
    if bool(meta.get("recency_boost")) != bool(recency_boost):
        return True
    if int(meta.get("recency_boost_seasons", -1)) != RECENCY_BOOST_SEASONS:
        return True

    # Rule 2 — new source data landed (string ISO compares lexicographically
    # because both watermarks are zero-padded UTC ISO-8601).
    prior = str(meta.get("source_max_updated_at", ""))
    if prior < str(source_watermark):
        return True

    return False


# ============================================================================
# Atomic serialization (§4.3 / §4.4)
# ============================================================================


def _atomic_write_bytes(path: str, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically: tmp file in same dir, fsync,
    os.replace.  A crashed build never leaves a half-written artifact."""
    dir_name = os.path.dirname(path) or "."
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _write_faiss_index(index, faiss_path: str) -> int:
    """Serialize a FAISS index atomically; return its on-disk byte size."""
    # faiss.serialize_index returns a numpy uint8 buffer we can fsync ourselves.
    buf = faiss.serialize_index(index)
    data = buf.tobytes()
    _atomic_write_bytes(faiss_path, data)
    return len(data)


def _build_faiss_index(vectors: NDArray[np.float32], dim: int):
    """Build a flat-L2 index over ``vectors`` (spec §4.2)."""
    index = faiss.IndexFlatL2(dim)
    if vectors.size:
        index.add(np.ascontiguousarray(vectors, dtype=np.float32))
    return index


def write_tile(
    key: TileKey,
    pool_dir: str,
    payload: TilePayload,
    recency_boost: bool,
    dim: int,
) -> dict:
    """Build the FAISS index + rowids + meta and write all three atomically.

    Returns the meta dict that was written.
    """
    if not _FAISS_AVAILABLE:  # pragma: no cover — guarded at CLI/build entry
        raise RuntimeError(
            f"faiss-cpu is not installed. Original import error: {_FAISS_IMPORT_ERROR!r}"
        )

    faiss_path = key.faiss_path(pool_dir)
    meta_path = key.meta_path(pool_dir)
    rowids_path = key.rowids_path(pool_dir)

    index = _build_faiss_index(payload.vectors, dim)
    bytes_on_disk = _write_faiss_index(index, faiss_path)

    # rowids: int64 array, FAISS vector i -> source outcome/pitch row id.
    rowids = np.ascontiguousarray(payload.rowids, dtype=np.int64)
    buf = io.BytesIO()
    np.save(buf, rowids, allow_pickle=False)
    _atomic_write_bytes(rowids_path, buf.getvalue())

    meta = {
        "schema_version": SCHEMA_VERSION,
        "pool": key.pool,
        "season": key.season,
        "bat_hand": key.bat_hand,
        "dim": dim,
        "n_vectors": int(payload.vectors.shape[0]),
        "n_source_rows": int(payload.n_source_rows),
        "recency_boost": bool(recency_boost),
        "recency_boost_seasons": RECENCY_BOOST_SEASONS,
        "source_max_updated_at": payload.source_max_updated_at,
        "build_timestamp": _now_iso(),
        "builder_version": BUILDER_VERSION,
        "bytes_on_disk": int(bytes_on_disk),
    }
    if key.pool == POOL_PITCH:
        meta["pitcher_id"] = int(key.pitcher_id)
    else:
        # battedball: spray_column present, pitcher_id absent (§4.3).
        meta["spray_column"] = payload.spray_column

    _atomic_write_bytes(
        meta_path, json.dumps(meta, indent=2, sort_keys=True).encode("utf-8")
    )
    return meta


# ============================================================================
# Orchestration
# ============================================================================


def _build_pitch_pool(
    conn: duckdb.DuckDBPyConnection,
    pool_dir: str,
    seasons: list[int] | None,
    recency_boost: bool,
    result: BuildResult,
) -> None:
    """Build every pitch tile: standalone tiles for pitchers >= MIN_TILE_ROWS,
    plus the pitcher_id=0 fall-back tile collecting all sub-threshold rows
    per (season, bat_hand)."""
    plan, hand_col = _plan_pitch_tiles(conn, seasons)
    if not plan:
        log.info("Pitch pool: no candidate groups found.")
        return

    # Count rows per (season, pitcher_id, bat_hand) to partition standalone vs
    # fall-back.  Cheap second aggregate; one query.
    season_filter = ""
    if seasons:
        season_filter = f"WHERE season IN ({', '.join(str(int(s)) for s in seasons)})"
    counts = conn.execute(f"""
        SELECT season, pitcher_id, {hand_col} AS bat_hand, COUNT(*) AS n
          FROM sim.pitch_pool
        {season_filter}
         GROUP BY season, pitcher_id, {hand_col}
    """).fetchall()

    # Group fall-back pitcher ids per (season, bat_hand).
    standalone: list[tuple[int, int, str]] = []
    fallback_members: dict[tuple[int, str], list[int]] = {}
    for season, pitcher_id, bat_hand, n in counts:
        if bat_hand not in ("L", "R"):
            continue
        season = int(season)
        pitcher_id = int(pitcher_id)
        bat_hand = str(bat_hand)
        if int(n) >= MIN_TILE_ROWS:
            standalone.append((season, pitcher_id, bat_hand))
        else:
            fallback_members.setdefault((season, bat_hand), []).append(pitcher_id)

    build_seasons = sorted({s for (s, _p, _h) in standalone}
                           | {s for (s, _h) in fallback_members})

    # --- Standalone tiles -------------------------------------------------
    for season, pitcher_id, bat_hand in standalone:
        key = TileKey(pool=POOL_PITCH, season=season,
                      bat_hand=bat_hand, pitcher_id=pitcher_id)
        wm = plan.get((season, pitcher_id, bat_hand), _epoch_iso())
        if not is_tile_stale(key, pool_dir, wm, recency_boost):
            result.skipped += 1
            result.skipped_tiles.append(key.label())
            continue
        payload = _build_pitch_payload(
            conn, hand_col, key, [pitcher_id], recency_boost, build_seasons
        )
        write_tile(key, pool_dir, payload, recency_boost, PITCH_DIM)
        result.rebuilt += 1
        result.rebuilt_tiles.append(key.label())
        log.info("Built pitch tile %s (n_source=%d n_vectors=%d)",
                 key.label(), payload.n_source_rows, payload.vectors.shape[0])

    # --- Fall-back (pitcher_id = 0) tiles ---------------------------------
    cols = _table_columns(conn, "pitch_pool")
    wm_expr = _select_watermark_expr(cols)
    for (season, bat_hand), members in fallback_members.items():
        key = TileKey(pool=POOL_PITCH, season=season,
                      bat_hand=bat_hand, pitcher_id=FALLBACK_PITCHER_ID)
        # Watermark for the fall-back tile = max over its member rows.
        wm_row = conn.execute(f"""
            SELECT {wm_expr} FROM sim.pitch_pool
             WHERE season = {int(season)}
               AND {hand_col} = '{bat_hand}'
               AND pitcher_id IN ({", ".join(str(p) for p in members)})
        """).fetchone()
        wm = str(wm_row[0]) if wm_row and wm_row[0] is not None else _epoch_iso()

        if not is_tile_stale(key, pool_dir, wm, recency_boost):
            result.skipped += 1
            result.skipped_tiles.append(key.label())
            continue
        payload = _build_pitch_payload(
            conn, hand_col, key, members, recency_boost, build_seasons
        )
        write_tile(key, pool_dir, payload, recency_boost, PITCH_DIM)
        result.rebuilt += 1
        result.rebuilt_tiles.append(key.label())
        log.info("Built pitch FALL-BACK tile %s from %d sub-threshold pitchers "
                 "(n_source=%d n_vectors=%d)",
                 key.label(), len(members),
                 payload.n_source_rows, payload.vectors.shape[0])


def _build_battedball_pool(
    conn: duckdb.DuckDBPyConnection,
    pool_dir: str,
    seasons: list[int] | None,
    recency_boost: bool,
    result: BuildResult,
) -> None:
    """Build batted-ball tiles, one per (season, bat_hand)."""
    plan, hand_col = _plan_battedball_tiles(conn, seasons)
    if not plan:
        log.info("Batted-ball pool: no candidate groups found.")
        return

    # Reuse the SIM-042 engine's spray-column discipline (spec §3.1).
    spray_col = BattedBallSimilarityEngine._select_spray_column(conn)

    build_seasons = sorted({s for (s, _h) in plan})

    for (season, bat_hand), wm in plan.items():
        key = TileKey(pool=POOL_BATTEDBALL, season=season, bat_hand=bat_hand)
        if not is_tile_stale(key, pool_dir, wm, recency_boost):
            result.skipped += 1
            result.skipped_tiles.append(key.label())
            continue
        payload = _build_battedball_payload(
            conn, hand_col, spray_col, key, recency_boost, build_seasons
        )
        # Sub-threshold batted-ball tiles are still materialized: the spec's
        # fall-forward floor (§3) is defined for the pitch pool's pitcher
        # dimension; the batted-ball pool has only (season, bat_hand) and no
        # pitcher fan-out to fold into, so the season/bat_hand tile IS the
        # league-average pool.  We log a warning when it is thin.
        if payload.n_source_rows < MIN_TILE_ROWS:
            log.warning("Batted-ball tile %s has only %d rows (< MIN_TILE_ROWS=%d).",
                        key.label(), payload.n_source_rows, MIN_TILE_ROWS)
        write_tile(key, pool_dir, payload, recency_boost, BATTEDBALL_DIM)
        result.rebuilt += 1
        result.rebuilt_tiles.append(key.label())
        log.info("Built batted-ball tile %s (spray=%s n_source=%d n_vectors=%d)",
                 key.label(), spray_col,
                 payload.n_source_rows, payload.vectors.shape[0])


def build_play_pool_cache(
    duckdb_path: str,
    pool_dir: str = DEFAULT_POOL_DIR,
    *,
    seasons: list[int] | None = None,
    pools: tuple[str, ...] = (POOL_PITCH, POOL_BATTEDBALL),
    recency_boost: bool = True,
) -> BuildResult:
    """Build (only stale/missing) play-pool tiles into ``pool_dir``.

    This is the public, importable entry point the nightly Make target and the
    unit tests call.  Returns a :class:`BuildResult` with ``rebuilt`` /
    ``skipped`` counts so a no-op nightly run is observable (``rebuilt == 0``).
    """
    if not _FAISS_AVAILABLE:  # pragma: no cover
        raise RuntimeError(
            f"faiss-cpu is not installed. Original import error: {_FAISS_IMPORT_ERROR!r}"
        )

    t0 = time.time()
    result = BuildResult()
    conn = duckdb.connect(duckdb_path, read_only=True)
    try:
        if POOL_PITCH in pools:
            _build_pitch_pool(conn, pool_dir, seasons, recency_boost, result)
        if POOL_BATTEDBALL in pools:
            _build_battedball_pool(conn, pool_dir, seasons, recency_boost, result)
    finally:
        conn.close()

    log.info(
        "Play-pool cache build complete: rebuilt=%d skipped=%d in %.2fs "
        "(pool_dir=%s recency_boost=%s).",
        result.rebuilt, result.skipped, time.time() - t0, pool_dir, recency_boost,
    )
    return result


# ============================================================================
# Helpers
# ============================================================================


def _f(value) -> float:
    """Coerce a DuckDB scalar (Decimal/None) to a NaN-friendly float once."""
    if value is None:
        return float("nan")
    return float(value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime(_ISO_FMT)


def _epoch_iso() -> str:
    return datetime(1970, 1, 1, tzinfo=timezone.utc).strftime(_ISO_FMT)


# ============================================================================
# CLI entry point
# ============================================================================


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.batch.play_pool_cache",
        description=(
            "SIM-301 play-pool nightly cache serializer. Materializes FAISS "
            "tiles for the pitch and batted-ball pools from DuckDB. Idempotent: "
            "only stale or missing tiles are rebuilt."
        ),
    )
    parser.add_argument(
        "--duckdb-path",
        default=DEFAULT_DUCKDB_PATH,
        help="Path to the DuckDB file (default: BASEBALL_DUCKDB_PATH or "
             "/data/baseball_sim.duckdb).",
    )
    parser.add_argument(
        "--pool-dir",
        default=DEFAULT_POOL_DIR,
        help="Play-pool output root (default: BASEBALL_PLAY_POOL_DIR or "
             "/data/play_pool).",
    )
    parser.add_argument(
        "--seasons", type=int, nargs="+", default=None,
        help="One or more seasons to (re)build. Default: all seasons in the pool.",
    )
    parser.add_argument(
        "--pool", choices=["pitch", "battedball", "all"], default="all",
        help="Which pool(s) to build (default: all).",
    )
    parser.add_argument(
        "--no-recency-boost", dest="recency_boost", action="store_false",
        help="Disable the last-2-seasons recency duplication (deterministic builds). "
             "Recency boost is ON by default for production.",
    )
    parser.set_defaults(recency_boost=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if not _FAISS_AVAILABLE:
        sys.stderr.write(
            "ERROR: faiss-cpu is not installed; cannot build play-pool tiles. "
            f"Original import error: {_FAISS_IMPORT_ERROR!r}\n"
        )
        return 3

    if args.pool == "all":
        pools = (POOL_PITCH, POOL_BATTEDBALL)
    else:
        pools = (args.pool,)

    log.info(
        "Starting play-pool cache build - duckdb=%s pool_dir=%s seasons=%s "
        "pools=%s recency_boost=%s",
        args.duckdb_path, args.pool_dir, args.seasons, pools, args.recency_boost,
    )
    result = build_play_pool_cache(
        duckdb_path=args.duckdb_path,
        pool_dir=args.pool_dir,
        seasons=args.seasons,
        pools=pools,
        recency_boost=args.recency_boost,
    )
    log.info("Done. rebuilt=%d skipped=%d", result.rebuilt, result.skipped)
    return 0


# ============================================================================
# Nightly scheduling (documentation — wired via Makefile `play-pool-cache`)
# ============================================================================
#
# The play-pool cache build runs AFTER the profile computor each night, since
# it reads sim.pitch_pool / sim.outcome_pool which the computor rebuilds.
#
# Crontab (UTC) — profile computor at 07:00, play-pool cache at 08:00 so the
# DuckDB pools are fully written before tiling begins:
#
#   0 7 * * *  cd /app && python -m pipeline.batch.player_profile_computor \
#                  >> /var/log/baseball/profile_computor.log 2>&1
#   0 8 * * *  cd /app && python -m pipeline.batch.play_pool_cache \
#                  >> /var/log/baseball/play_pool_cache.log 2>&1
#
# Or, chained so the cache only runs on a successful profile build:
#
#   0 7 * * *  cd /app && python -m pipeline.batch.player_profile_computor \
#                  && python -m pipeline.batch.play_pool_cache \
#                  >> /var/log/baseball/nightly.log 2>&1
#
# Make target: `make play-pool-cache` (see Makefile).
# ============================================================================


if __name__ == "__main__":
    raise SystemExit(main())
