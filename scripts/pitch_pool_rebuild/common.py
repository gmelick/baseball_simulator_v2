"""The constants and the small helpers every step of the pitch-pool rebuild
shares: the paths, the files of a whole pitch pool, the expected class moves,
the exit codes."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any

import numpy as np

from pipeline.batch.engine_artifacts import _ADV_KEYS
from pipeline.batch.player_profile_computor import SQL_OUTCOME_TYPE

#: The repository root (this file is scripts/<package>/<module>.py).
_ROOT = Path(__file__).resolve().parents[2]


#: The builder this rebuild is for. The script refuses to run on any other.
EXPECTED_BUILDER = "sim553.1"

DUCKDB_PATH = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
#: The bundle the app loads at boot: ``$BASEBALL_PLAY_POOL_DIR/engine_artifacts``
#: (``simulation/production_factory.py``), unless the operator names another.
ART_DIR = os.environ.get("BASEBALL_ENGINE_ARTIFACT_DIR") or os.path.join(
    os.environ.get("BASEBALL_PLAY_POOL_DIR", "/data/play_pool"), "engine_artifacts"
)
BACKUP_SUFFIX = ".pre_sim553"

#: Decision 4 (owner, 2026-09-23): the rebuild covers all ten seasons.
DEFAULT_SEASONS: tuple[int, ...] = tuple(range(2017, 2027))

#: The bundle's other pools. The export writes the pitch pool only (step 4),
#: so none of these is written; the round-trip proves each is byte-identical
#: to the copy. (``--what pool`` would rewrite all three from DuckDB, whose
#: steal and advancement pools are ahead of the bundle: see the drift line.)
UNCHANGED_POOLS: tuple[str, ...] = ("battedball_pool", "steal_pool", "advancement_pool")

#: The batting sides the pitch-pool export writes, and each side's files in
#: ``pitch_pool/`` beside ``manifest.json``. A whole pitch pool holds these
#: files and no other (:func:`_pitch_pool_problem`).
HANDS: tuple[str, ...] = ("L", "R")
PITCH_POOL_PARTS: tuple[str, ...] = ("meta.parquet", "geom.npy", "sit.npy", "bb_row.npy")

#: The advancement pool's sub-pool key, as the export names its files.
_ADV_KEY_SQL = (
    "(CAST(scenario AS VARCHAR) || '_' || CAST(from_base AS VARCHAR) || '_' "
    "|| CAST(target_base AS VARCHAR))"
)

#: The drift line's reading of each pool this rebuild leaves alone:
#: its DuckDB table, the export's sub-pool key and the export's row filter
#: (``engine_artifacts`` ``build_battedball_pool_artifact`` /
#: ``build_steal_pool_artifact`` / ``build_advancement_pool_artifact``), so the
#: counts compare with the bundle's ``manifest.json`` ``counts``.
DRIFT_POOLS: tuple[tuple[str, str, str, str], ...] = (
    (
        "battedball_pool",
        "sim.outcome_pool",
        "stand",
        "stand IN ('L', 'R') AND exit_velo IS NOT NULL AND launch_angle IS NOT NULL "
        "AND pull_relative_spray_angle IS NOT NULL",
    ),
    (
        "steal_pool",
        "sim.steal_opportunity_pool",
        "CAST(target_base AS VARCHAR)",
        "target_base IN (2, 3)",
    ),
    (
        "advancement_pool",
        "sim.advancement_opportunity_pool",
        _ADV_KEY_SQL,
        f"{_ADV_KEY_SQL} IN (" + ", ".join(f"'{k}'" for k in _ADV_KEYS) + ")",
    ),
)

#: The Gameday codes whose class the sim553.1 coding changes at some count.
MOVE_CODES: tuple[str, ...] = ("T", "L", "O", "Q", "R", "H")

#: Measured 2026-09-23 (the design's §2.1): the rows that change class in the
#: window 2023-2026, and about the same in all ten seasons. Printed beside the
#: measured counts; the per-row verdict of check b is the gate, not these.
WINDOW_EXPECTED_MOVES = 11_258
ALL_SEASONS_EXPECTED_MOVES = 25_961

#: The class move the new coding makes for one ``raw.pitches`` row, as
#: ``'old>new'``, or NULL for no move. It restates the design's table (§2.1) in
#: its own words, NOT through SQL_OUTCOME_TYPE, so check b tests the builder
#: against an independent statement of the intent. The left side is the old
#: expression's class: 'T' and 'L' were 'foul'; 'O', 'Q', 'R' and an 'H' with
#: no hit-by-pitch event fell to its ``ELSE 'ball'``. An event of
#: 'hit_by_pitch' took the first branch in both codings, so it never moves.
SQL_EXPECTED_MOVE = """CASE
            WHEN events = 'hit_by_pitch' THEN NULL
            WHEN code = 'H' THEN 'ball>hit_by_pitch'
            WHEN code IN ('T', 'L') AND strikes = 2 THEN 'foul>swinging_strike'
            WHEN code = 'O' AND strikes = 2 THEN 'ball>swinging_strike'
            WHEN code = 'O' THEN 'ball>foul'
            WHEN code = 'Q' THEN 'ball>swinging_strike'
            WHEN code = 'R' THEN 'ball>foul'
        END"""

#: The pool build's got-away label, computed from ``raw.pitches`` (the same
#: expression ``_build_pitch_pool`` writes). The 2017-2022 pool rows hold no
#: got-away value yet (their builder, sim509.1, predates the column), so
#: ``--check-only`` reads the label from the source.
SQL_RAW_GOT_AWAY = """(COALESCE(passed_ball_wild_pitch, FALSE)
                     OR (events IN ('strikeout', 'strikeout_double_play')
                         AND (des ILIKE '%wild pitch%' OR des ILIKE '%passed ball%')))"""

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


def _season_list(seasons: list[int]) -> str:
    return ", ".join(str(int(s)) for s in seasons)


def _sql_in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _pool_counts(con: Any, seasons: list[int]) -> dict[int, int]:
    """Rows per season of ``sim.pitch_pool``."""
    return {
        int(s): int(n)
        for s, n in con.execute(
            f"SELECT season, COUNT(*) FROM sim.pitch_pool WHERE season IN ({_season_list(seasons)}) "
            "GROUP BY 1 ORDER BY 1"
        ).fetchall()
    }


def _source_counts(con: Any, seasons: list[int]) -> dict[int, int]:
    """Rows per season of ``pg.raw.pitches`` under the pool build's own filter."""
    return {
        int(s): int(n)
        for s, n in con.execute(
            "SELECT season, COUNT(*) FROM pg.raw.pitches "
            f"WHERE data_quality_flag = FALSE AND season IN ({_season_list(seasons)}) "
            "GROUP BY 1 ORDER BY 1"
        ).fetchall()
    }


def _builder_versions(con: Any, seasons: list[int]) -> dict[int, str | None]:
    """The builder that wrote each season of the pitch pool."""
    return {
        int(s): v
        for s, v in con.execute(
            "SELECT season, builder_version FROM sim.pool_build_metadata "
            f"WHERE pool_name = 'pitch_pool' AND season IN ({_season_list(seasons)}) ORDER BY 1"
        ).fetchall()
    }


def _raw_code_rows_sql(seasons: list[int]) -> str:
    """The ``raw.pitches`` rows of the MOVE_CODES: the code, the count, the new
    class (SQL_OUTCOME_TYPE), the expected move and the raw got-away label."""
    return f"""
        SELECT game_pk, at_bat_number, pitch_number, code, strikes, events,
               got_away_raw, new_class, {SQL_EXPECTED_MOVE} AS expected_move
        FROM (
            SELECT game_pk, at_bat_number, pitch_number, TRIM(type) AS code, strikes, events,
                   {SQL_RAW_GOT_AWAY} AS got_away_raw,
                   {SQL_OUTCOME_TYPE} AS new_class
            FROM pg.raw.pitches
            WHERE data_quality_flag = FALSE AND season IN ({_season_list(seasons)})
              AND TRIM(type) IN {_sql_in(MOVE_CODES)}
        )"""


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _pq(path: str) -> str:
    return f"read_parquet('{path}')"


def _tree_files(root: str) -> dict[str, int]:
    """Every file under ``root``: its path relative to ``root`` (``/``-separated) -> its size."""
    out: dict[str, int] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            out[os.path.relpath(path, root).replace(os.sep, "/")] = os.path.getsize(path)
    return out


def _npy_shape(path: str) -> tuple[int, ...]:
    """The shape of a ``.npy`` file. The memory map fails on a short file."""
    arr = np.load(path, mmap_mode="r")
    shape = tuple(int(d) for d in arr.shape)
    del arr  # release the map (Windows holds the file while a map is open)
    return shape


def _npy_rows(path: str) -> int:
    """The first dimension of a ``.npy`` file. The memory map fails on a short file."""
    return _npy_shape(path)[0]
