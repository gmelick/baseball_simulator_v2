"""The helpers the bundle's exports and loaders share.

The exports share the pool columns, the DuckDB path, the season window, the game-date
column and the pickle-free npz writer. The loaders share the column readers. Each reader
takes the shared view (an array the parent process placed in shared memory) when the
parent published one, and reads the file otherwise.
"""

from __future__ import annotations

import json
import os
from typing import Any

import duckdb
import numpy as np

from db.introspect import duck_columns

#: The ten pitch geometry columns: the pitch pool's ``geom`` array.
_GEOM_COLS = [
    "velo",
    "ivb",
    "hb",
    "spin_rate",
    "spin_axis",
    "release_x",
    "release_z",
    "release_ext",
    "plate_x",
    "plate_z",
]
#: The game-situation columns: the ``sit`` array of each hand's pool.
_SIT_COLS = ["count_balls", "count_strikes", "outs", "runners_state", "inning", "score_diff"]

DEFAULT_DUCKDB_PATH = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")

#: The season window: the last three COMPLETED seasons plus the current one, so the 4
#: newest seasons in the pool. The fourth season thickens the thinnest cells of the
#: fielding draw's base-out filter, with little drift in the frequencies, at a higher
#: scoring cost per plate appearance (``scripts/pool_window_census.py`` measures both).
#: ``tests/acceptance/bands.py`` ``POOL_WINDOW`` must name as many seasons.
RECENCY_FLOOR_SEASONS = 4


def ymd_select(con, table: str) -> str:
    """The SELECT fragment for the game-date column of ``table``, or a blank when it has none.

    The function probes the table, as the export does for every optional pool column, so
    an older or reduced ``sim.*`` table still exports. The loaders treat the column as
    optional too.
    """
    schema, _, name = table.partition(".")
    have = duck_columns(con, schema, name)
    return f", {GAME_YMD_SQL} " if "game_date" in have else " "


#: Every pool row's game date as a YYYYMMDD integer.
#:
#: The pools hold four seasons of plays, and the simulator draws from all of them. A
#: simulation of an April 2025 game must not draw a play from that September. That draw
#: is look-ahead: the future leaks into the simulation, as in a profile built past its
#: cutoff. The date on each row lets the sampler drop the future at draw time. That costs
#: one comparison per game instead of one bundle rebuild per date.
#:
#: An integer, not a date: it shares across worker processes, compares in one
#: instruction and sorts in calendar order.
GAME_YMD_SQL = "CAST(strftime(game_date, '%Y%m%d') AS INTEGER) AS game_ymd"


def last_n_seasons(con: duckdb.DuckDBPyConnection, n: int = RECENCY_FLOOR_SEASONS) -> list[int]:
    """The ``n`` newest seasons in ``sim.pitch_pool``, oldest first."""
    seasons = [
        int(r[0])
        for r in con.execute(
            "SELECT DISTINCT season FROM sim.pitch_pool ORDER BY season DESC"
        ).fetchall()
    ]
    return sorted(seasons[:n])


def join_rows(keys: np.ndarray, targets: np.ndarray) -> np.ndarray:
    """For each ``keys[i]``, the first index j with ``targets[j] == keys[i]``, else -1.

    A negative key never matches. Returns an int32 array as long as ``keys``.
    """
    keys = np.asarray(keys, dtype=np.int64)
    targets = np.asarray(targets, dtype=np.int64)
    out = np.full(len(keys), -1, dtype=np.int32)
    if len(targets) == 0 or len(keys) == 0:
        return out
    order = np.argsort(targets, kind="stable")
    st = targets[order]
    pos = np.clip(np.searchsorted(st, keys), 0, len(st) - 1)
    hit = (st[pos] == keys) & (keys >= 0)
    out[hit] = order[pos[hit]].astype(np.int32)
    return out


def _savez_plain(path: str, **arrays: Any) -> None:
    """Write a compressed npz file that loads without a pickle.

    The bundle loader reads every file with ``allow_pickle=False``. An object
    array needs a pickle, so the loader refuses it. This writer refuses it
    first, at build time, and names the file and the member. A JSON string
    member is safe: numpy stores a Python ``str`` as fixed-width unicode.
    """
    plain = {name: np.asarray(value) for name, value in arrays.items()}
    for name, arr in plain.items():
        if arr.dtype.hasobject:
            raise ValueError(
                f"{path}: member {name!r} is an object array ({arr.dtype}); "
                "the bundle loader refuses a pickle, so the builder cannot write one"
            )
    np.savez_compressed(path, **plain)


def take_view(views: dict[str, np.ndarray], key: str, disk_path: str) -> np.ndarray:
    """The shared view ``key`` when the parent published one, else the array at ``disk_path``."""
    v = views.get(key)
    if isinstance(v, np.ndarray):
        return v
    return np.load(disk_path, allow_pickle=False)


def float_columns(d: dict, cols: list[str]) -> np.ndarray:
    """The ``cols`` of a ``fetchnumpy`` result as one float32 matrix, NULL and NaN read as 0."""
    return np.nan_to_num(
        np.stack([np.ma.filled(d[c], np.nan).astype(np.float32) for c in cols], axis=1)
    ).astype(np.float32)


def write_json(path: str, doc: Any) -> None:
    """Write ``doc`` to ``path`` as JSON indented by two spaces."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)


def read_json(art_dir: str, name: str) -> Any:
    """The JSON document ``name`` in ``art_dir``, or None when the file is absent."""
    path = os.path.join(art_dir, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def meta_column(
    views: dict[str, np.ndarray], key: str, meta: dict, col: str, dtype: type, fill: float = 0
) -> np.ndarray:
    """The shared view ``key``, else the metadata column ``col`` as ``dtype``.

    A NULL reads as ``fill``.
    """
    v = views.get(key)
    if isinstance(v, np.ndarray):
        return v
    return np.asarray(np.ma.filled(meta[col], fill), dtype=dtype)


def optional_meta_column(
    views: dict[str, np.ndarray], key: str, meta: dict, col: str, dtype: type, fill: float = 0
) -> np.ndarray | None:
    """As :func:`meta_column`, but None when an older bundle lacks the column."""
    v = views.get(key)
    if isinstance(v, np.ndarray):
        return v
    if col not in meta:
        return None
    return np.asarray(np.ma.filled(meta[col], fill), dtype=dtype)


def recency_column(views: dict[str, np.ndarray], key: str, meta: dict) -> np.ndarray:
    """The shared view ``key``, else the ``recency_weight`` column as float32.

    A NULL or NaN weight reads as 1.0.
    """
    v = views.get(key)
    if isinstance(v, np.ndarray):
        return v
    return np.nan_to_num(np.ma.filled(meta["recency_weight"], 1.0).astype(np.float32), nan=1.0)


def game_ymd_column(meta: dict) -> np.ndarray | None:
    """The ``game_ymd`` column as int32, with NULL read as 0.

    Returns None for a bundle exported before the column existed.
    """
    if "game_ymd" not in meta:
        return None
    return np.asarray(np.ma.filled(meta["game_ymd"], 0), dtype=np.int32)
