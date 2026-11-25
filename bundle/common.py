"""What every artifact builder shares: the pool columns, the default paths, the
season window, the game-date column and the plain-array writer.
"""

from __future__ import annotations

import json
import os
from typing import Any

import duckdb
import numpy as np

from db.introspect import duck_columns

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
_SIT_COLS = ["count_balls", "count_strikes", "outs", "runners_state", "inning", "score_diff"]

DEFAULT_DUCKDB_PATH = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")

#: The artifact's season window (owner ruling 2026-08-20, SIM-516): the last
#: three COMPLETED seasons plus the current one, in practice the 4 newest
#: seasons in the pool (2023-2026 today). Four seasons against three thicken
#: the thinnest cell of the fielding draw's hard base-out filter from 323 to
#: 439 rows (+36% batted balls) at under 0.5% frequency drift (the window
#: census, `scripts/pool_window_census.py`). They cost about 35% more scoring
#: per plate appearance.
RECENCY_FLOOR_SEASONS = 4


def ymd_select(con, table: str) -> str:
    """The game-date column for ``table``, or nothing when it has none.

    Probed rather than assumed, matching how every other optional pool column
    is exported: a minimal or older ``sim.*`` table still builds, and the
    loader already treats the column as optional on the way back in.
    """
    schema, _, name = table.partition(".")
    have = duck_columns(con, schema, name)
    return f", {GAME_YMD_SQL} " if "game_date" in have else " "


#: Every pool row's game date as a plain YYYYMMDD integer.
#:
#: The pools hold four seasons of plays and the simulator draws from all of
#: them. Simulating a game played in April 2025 while the pool still offers
#: plays from that September is look-ahead (the pool cutoff, SIM-535): the
#: same defect as a profile built past its cutoff, and a larger one, because it
#: is the plays themselves.
#: Carrying the date per row lets the sampler exclude the future at draw time,
#: which costs one comparison per game instead of one bundle rebuild per date.
#:
#: An integer rather than a date: it shares cleanly across worker processes,
#: compares in one instruction, and sorts the same way the calendar does.
GAME_YMD_SQL = "CAST(strftime(game_date, '%Y%m%d') AS INTEGER) AS game_ymd"


def last_n_seasons(con: duckdb.DuckDBPyConnection, n: int = RECENCY_FLOOR_SEASONS) -> list[int]:
    seasons = [
        int(r[0])
        for r in con.execute(
            "SELECT DISTINCT season FROM sim.pitch_pool ORDER BY season DESC"
        ).fetchall()
    ]
    return sorted(seasons[:n])


def join_rows(keys: np.ndarray, targets: np.ndarray) -> np.ndarray:
    """For every ``keys[i]`` the index j with
    ``targets[j] == keys[i]`` (the first such j), or -1. int32."""
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
    """Either a shared view (zero-cost) or a fresh ``np.load`` (cold)."""
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
    """The shared view ``key``, else the metadata column ``col`` as ``dtype`` with NULL read as ``fill``."""
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
    """The shared view ``key``, else the ``recency_weight`` column as float32 with NULL and NaN read as 1.0."""
    v = views.get(key)
    if isinstance(v, np.ndarray):
        return v
    return np.nan_to_num(np.ma.filled(meta["recency_weight"], 1.0).astype(np.float32), nan=1.0)


def game_ymd_column(meta: dict) -> np.ndarray | None:
    """The ``game_ymd`` column as int32 with NULL read as 0, or None on a bundle exported before it existed."""
    if "game_ymd" not in meta:
        return None
    return np.asarray(np.ma.filled(meta["game_ymd"], 0), dtype=np.int32)
