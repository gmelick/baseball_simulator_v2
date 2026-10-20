"""The constants and the small helpers every step of the steal-pool rebuild
shares: the paths, the files of a whole steal pool, the labels, the exit codes."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any

import numpy as np

import pipeline.batch.profiles.pool_meta as pool_meta

#: The repository root (this file is scripts/<package>/<module>.py).
_ROOT = Path(__file__).resolve().parents[2]


#: The builder this rebuild is for. The script refuses to run on any other.
EXPECTED_BUILDER = "sim554.1"
#: The pitch-pool builders whose class the steal pool may copy: sim553.1 (a
#: two-strike foul tip coded as strike three) and sim554.1 (a version bump on
#: the same pitch-pool coding).
PITCH_POOL_BUILDERS: tuple[str, ...] = ("sim553.1", "sim554.1")

DUCKDB_PATH = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
#: The bundle the app loads at boot: ``$BASEBALL_PLAY_POOL_DIR/engine_artifacts``
#: (``simulation/production_factory.py``), unless the operator names another.
ART_DIR = os.environ.get("BASEBALL_ENGINE_ARTIFACT_DIR") or os.path.join(
    os.environ.get("BASEBALL_PLAY_POOL_DIR", "/data/play_pool"), "engine_artifacts"
)
#: The copy of the bundle's steal pool, a sibling OUTSIDE the bundle: a
#: directory inside it would change every later comparison of the bundle.
BACKUP_SUFFIX = ".pre_sim554_steal_pool"
MIGRATION = _ROOT / "db" / "migrations" / "duckdb" / "0031_sim554_steal_pool_pitch_class.sql"
NEW_COLUMNS: tuple[str, ...] = ("pitch_class", "is_pickoff_row")

TARGETS: tuple[str, ...] = ("2", "3")
#: The files a whole steal pool holds, and no other.
STEAL_POOL_FILES: frozenset[str] = frozenset(
    {"manifest.json"} | {f"{t}.{p}" for t in TARGETS for p in ("meta.parquet", "sit.npy")}
)
#: The staging files of the export (``build_steal_pool_artifact``): it writes
#: each target's two files as ``tmp_<target>.*`` and moves them into place at
#: the end. DuckDB's COPY over an existing file writes ``tmp_<name>`` first, so
#: a COPY over a left-over staging file writes ``tmp_tmp_<target>.meta.parquet``.
#: A killed export leaves any of them; the next export overwrites them (and
#: moves its own into place), and the loader reads the pool's files by name.
EXPORT_STAGING_FILES: frozenset[str] = frozenset(
    {f"tmp_{t}.{p}" for t in TARGETS for p in ("meta.parquet", "sit.npy")}
    | {f"tmp_tmp_{t}.meta.parquet" for t in TARGETS}
)
#: The five labels the rebuild must keep on every pitch row (with pitch_id,
#: the six values of the snapshot).
LABELS: tuple[str, ...] = (
    "attempted",
    "success",
    "pickoff_out",
    "pickoff_advancing",
    "pickoff_error",
)
#: The classes on which no real runner ever earns an attempt (design §2.1).
DEAD_BALL_CLASSES: tuple[str, ...] = ("in_play", "hit_by_pitch")
#: Measured 2026-09-30 (design §2.8, §4.6): the pickoff rows the rebuild adds
#: in the pool's games, and the outcomes that fit a pair. Printed beside the
#: counted figures; the per-row check c is the gate, not these.
MEASURED_PICKOFF_ROWS: dict[int, int] = {2023: 91, 2024: 108, 2025: 95, 2026: 100}
MEASURED_FIT = 1_788
#: The steal band centres, when ``tests/acceptance/bands.py`` does not load.
FALLBACK_CENTRES: dict[str, float] = {
    "STEAL_ATT_OPP_2B": 0.0214,
    "STEAL_ATT_OPP_3B": 0.0044,
    "STEAL_SAFE_2B": 0.7982,
}

SNAP = "_sim554_before"
EXPECT = "_sim554_expect"
POOL = "sim.steal_opportunity_pool"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_STOPPED = 2
EXIT_LOCKED = 3
EXIT_BUNDLE = 4


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _ok(good: bool) -> str:
    return "OK" if good else "MISMATCH"


def _season_list(seasons: list[int]) -> str:
    return ", ".join(str(int(s)) for s in seasons)


def _builder() -> str:
    """The builder the computor writes. Read at call time, so a test can set it."""
    return str(pool_meta.POOL_BUILDER_VERSION)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _sql_path(path: str) -> str:
    """A file path as DuckDB reads it in SQL (forward slashes on every host)."""
    return str(path).replace("\\", "/")


def _pq(path: str) -> str:
    return f"read_parquet('{_sql_path(path)}')"


def _npy_shape(path: str) -> tuple[int, ...]:
    """The shape of a ``.npy`` file. The memory map fails on a short file."""
    arr = np.load(path, mmap_mode="r")
    shape = tuple(int(d) for d in arr.shape)
    del arr  # release the map (Windows holds the file while a map is open)
    return shape


def _columns(con: Any, path: str) -> list[str]:
    return [str(r[0]) for r in con.execute(f"DESCRIBE SELECT * FROM {_pq(path)}").fetchall()]


def _table_columns(con: Any, schema: str, table: str) -> set[str]:
    """The columns of a table of this DuckDB file (not of the attached Postgres)."""
    return {
        str(r[0])
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_catalog = current_database() AND table_schema = ? AND table_name = ?",
            [schema, table],
        ).fetchall()
    }


def _tree_files(root: str) -> dict[str, int]:
    """Every file under ``root``: its path relative to ``root`` (``/``-separated) -> its size."""
    out: dict[str, int] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            out[os.path.relpath(path, root).replace(os.sep, "/")] = os.path.getsize(path)
    return out
