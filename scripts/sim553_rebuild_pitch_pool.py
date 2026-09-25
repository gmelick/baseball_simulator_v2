"""
scripts/sim553_rebuild_pitch_pool.py — the SIM-553 pitch-pool rebuild, end to end.

WHAT IT DOES
============
The pool build now codes a two-strike foul tip or foul bunt (Gameday ``T``,
``L``, ``O``) as strike three: class ``swinging_strike``. It also codes ``O``,
``Q``, ``R``, ``P`` and ``H`` explicitly and has no ELSE
(``pipeline/batch/player_profile_computor.py`` ``SQL_OUTCOME_TYPE``; builder
``sim553.1``). This script rebuilds ``sim.pitch_pool`` on that coding. It
proves the rebuild moved exactly the rows the coding says it moves. It exports
the pitch pool ONLY into the engine-artifact bundle, and it proves the export
changed nothing else. The design is
``docs/audit/2026-09-23-foul-tip-strike-three-pool-coding-plan.md`` §7.

  0. Pre-flight, with no write: the builder version, the bundle, the backup
     target, write access, the window, the source rows, the unknown codes,
     and a fresh batted-ball join that must reproduce the rollback point's
     own. The rollback point is an existing WHOLE copy (kept), else the
     bundle, whose own pitch pool must then be whole (no stray file, every
     file with its manifest's rows). The rollback point's pitch pool must
     match DuckDB's window pitch by pitch: the same pitch ids per batting
     side; the same classes, or on a re-run after a committed rebuild
     classes that differ by exactly the expected moves; and the same
     geometry and situation rows. Any other difference would fail the
     round-trip after the COMMIT, so it stops the run here. It also prints a
     DRIFT line, for information only: DuckDB's window rows against the
     bundle's manifests for the batted-ball, steal and advancement pools.
     (On 2026-09-25 DuckDB's steal pool held +14,004 / +9,417 rows and its
     advancement pool +4,444 rows more than the bundle: the 2026 games of
     08-14 to 08-29.) This rebuild does not export those pools. Refreshing
     them is a separate owner decision; the next nightly ``--what pool`` or
     ``make engine-artifacts`` export would carry the refresh.
     Then open DuckDB writable. A held lock stops the run (exit 3).
  1. Snapshot ``(pitch_id, season, outcome_type)`` of the seasons into a TEMP
     table.
  2. Rebuild ``sim.pitch_pool`` for the seasons (default 2017-2026) inside ONE
     transaction.
  3. Check, still inside the transaction:
       a. the rows per season equal the snapshot's; no NULL class;
       b. the moved rows are exactly the expected rows, code by code;
       c. no moved foul tip or foul bunt (T / L / O) carries the got-away
          flag (a moved 'Q' is a real swing and miss; its flag is reported);
       d. the count chain of the window pool's plate appearances against
          real plate appearances (``pipeline/batch/pool_chain.py``
          ``chain_rates(..., pa_only=True)`` and ``label_check``): the two
          sides count the same plate appearances (``pa_group_count`` equals
          ``real_pa_rates``' ``n_pa``) and every channel is within 0.5%. The
          full-pool chain, the band centres, prints for the record;
       e. ``sim.pool_build_metadata`` reads the builder version for every season.
     A failed check rolls the transaction back, so the table keeps its old
     coding and the bundle is untouched (exit 2). Then COMMIT. The review
     measured the ten-season COMMIT at about 195 s (peak memory 4.2 GB), so
     with the checks, the copy and the export the app is down about ten to
     fifteen minutes.
  4. Copy the bundle aside (``<bundle>.pre_sim553``), or keep an existing
     whole copy as the rollback point. A copy is whole when it holds the
     bundle's files outside ``pitch_pool/`` byte for byte and its own pitch
     pool is whole by its own manifest; its pitch pool is never compared
     with the bundle's, which a failed run may have half-written. A fresh
     copy must pass the same test. Export the pitch pool ONLY
     (``build_pitch_pool_artifact``), then recompute
     ``pitch_pool/{hand}.bb_row.npy`` (each pitch's row in the batted-ball
     pool) exactly as ``build_battedball_pool_artifact`` computes it, against
     the bundle's untouched ``battedball_pool/{hand}.pitch_id.npy``. The
     batted-ball, steal and advancement pools are not written.
  5. The round-trip, graded on the export alone, so a re-run passes too:
     the exported pitch pool is whole by its own manifest; every exported
     class equals DuckDB's class for the same pitch; every class change from
     the copy to the export is that pitch's expected move from its raw code;
     the copy reads one coding (the old or the new, never a mix); each pitch
     keeps its batted ball, its geometry and its situation rows (aligned on
     ``pitch_id``); the batted-ball, steal and advancement files are
     byte-identical to the copy's. A difference fails the run (exit 4):
     roll the bundle back.
  6. Print the rollback (with the coding the copy holds, read from the copy)
     and the next run-book steps.

``--check-only`` writes nothing. It runs the pre-flight (with the drift line),
check d on the pool as it stands, and the class of every ``T`` / ``L`` / ``O``
/ ``Q`` / ``R`` / ``H`` pitch at each strike count, beside the class the new
coding gives. It exits 0 when the label check passes and 2 when it fails.

``--no-transaction`` is the fallback if the one-transaction rebuild runs out
of memory (a killed process leaves the table as it was). It rebuilds in
autocommit, the SIM-518 pattern, and a failed check puts the snapshot's
classes and builders back. Its weak point: an error in the middle of the
builder's INSERT leaves the seasons deleted. The pre-flight's source-row and
unknown-code checks guard that.

``--seasons`` must hold the whole window. ``--force-backup`` renames aside (it
never deletes) an existing copy that is NOT whole (a partial copy, or one the
bundle has moved on from), then copies the bundle again. It refuses when the
bundle's own pitch pool is not whole (a stray file such as a killed COPY's
``tmp_L.meta.parquet``, or files whose rows disagree with the manifest): such
a bundle never becomes the rollback point. A whole copy is always kept, with
or without the flag. ``--duckdb-path`` and ``--art-dir`` point the run at
another database and bundle.

RUN
===
The app must be STOPPED: its worker server holds the DuckDB writer lock
(SIM-524). From the repo root (``scripts/`` is NOT bind-mounted):

    docker compose stop app
    MSYS_NO_PATHCONV=1 docker compose run -d --rm \\
        -v "$PWD/scripts:/app/scripts" app python scripts/sim553_rebuild_pitch_pool.py
    docker logs -f <the run container>      # read to the COMPLETE / FAILED line
    docker compose up -d app

EXIT CODES
==========
  0  complete.
  1  an unexpected error before the export. The rebuild is undone, and the
     bundle is untouched.
  2  a pre-flight or pool check failed, or the bundle copy failed. Nothing was
     exported. The log says whether the DuckDB table was undone. After a
     failed copy, a partial copy may remain: re-run with ``--force-backup``
     (it cannot help when the bundle's own pitch pool is not whole).
  3  the DuckDB file is locked: the app, or another reader, is running.
  4  the export or its round-trip failed. The bundle needs the rollback the
     log prints (or, after a failed export, a re-run: it keeps the copy).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duckdb  # noqa: E402
import numpy as np  # noqa: E402

from pipeline.batch.engine_artifacts import (  # noqa: E402
    _ADV_KEYS,
    _GEOM_COLS,
    _SIT_COLS,
    RECENCY_FLOOR_SEASONS,
    build_pitch_pool_artifact,
    join_rows,
    last_n_seasons,
)
from pipeline.batch.player_profile_computor import (  # noqa: E402
    POOL_BUILDER_VERSION,
    SQL_OUTCOME_TYPE,
    STRIKE_THREE_FOUL_TYPES,
    PlayerProfileComputor,
)
from pipeline.batch.pool_chain import (  # noqa: E402
    LABEL_CHECK_TOLERANCE,
    OUTCOMES,
    attach_pg,
    chain_rates,
    format_label_check,
    format_pa_count,
    label_check,
    pa_group_count,
    real_pa_rates,
)

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

#: SIM-553: the drift line's reading of each pool this rebuild leaves alone:
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

#: SIM-553: the class move the new coding makes for one ``raw.pitches`` row, as
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


# ---------------------------------------------------------------------------
# Pre-flight (no write)
# ---------------------------------------------------------------------------


def _unknown_codes(con: Any, seasons: list[int]) -> list[tuple[Any, Any, int]]:
    """Source rows the new coding gives no class (decision 3: no ELSE).

    Any such row makes the pool INSERT fail on the NOT NULL column. The
    pre-flight names them before the rebuild deletes a row.
    """
    return [
        (code, strikes, int(n))
        for code, strikes, n in con.execute(
            f"SELECT TRIM(type) AS code, strikes, COUNT(*) FROM pg.raw.pitches "
            f"WHERE data_quality_flag = FALSE AND season IN ({_season_list(seasons)}) "
            f"AND ({SQL_OUTCOME_TYPE}) IS NULL GROUP BY 1, 2 ORDER BY 3 DESC"
        ).fetchall()
    ]


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


def _pitch_pool_strays(root: str) -> list[str]:
    """The entries of ``root/pitch_pool/`` that no export writes.

    A killed export leaves them: DuckDB's COPY TO an existing file writes a
    temporary ``tmp_<name>`` first, and a SIGKILL during that COPY leaves it
    beside the old file (measured on DuckDB 1.5.5).
    """
    pitch_dir = os.path.join(root, "pitch_pool")
    if not os.path.isdir(pitch_dir):
        return []
    whole = {"manifest.json"} | {f"{h}.{p}" for h in HANDS for p in PITCH_POOL_PARTS}
    return sorted(set(os.listdir(pitch_dir)) - whole)


def _export_replaces(name: str) -> bool:
    """TRUE for a stray the next export replaces.

    The export's COPY TO ``<side>.meta.parquet`` writes ``tmp_<side>.meta.parquet``
    first (over any old one), then renames it over the target. So a killed
    COPY's temporary file is gone after the next export (measured 2026-09-25 on
    DuckDB 1.1.3 and 1.5.5). No other stray is replaced.
    """
    return name in {f"tmp_{h}.meta.parquet" for h in HANDS}


def _pitch_pool_problem(root: str) -> str | None:
    """Why ``root``'s pitch pool is not whole by its OWN manifest, or None.

    SIM-553: whole means the manifest reads and names the two batting sides;
    each side's meta, geometry, situation and batted-ball-join files hold the
    manifest's rows (the geometry and situation files with the manifest's
    columns); and ``pitch_pool/`` holds no other file. A stray file such as
    ``tmp_L.meta.parquet`` marks a killed export. The test reads ``root``
    alone and never compares it with another bundle. It checks counts, not
    order: :func:`_rollback_point_problems` checks each row against DuckDB.
    """
    strays = _pitch_pool_strays(root)
    if strays:
        return (
            f"its pitch_pool/ holds files no export writes: {strays} (a killed export leaves these)"
        )
    pitch_dir = os.path.join(root, "pitch_pool")
    mem = duckdb.connect()
    try:
        with open(os.path.join(pitch_dir, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        counts = {str(k): int(v) for k, v in manifest["counts"].items()}
        if set(counts) != set(HANDS):
            return f"its pitch_pool manifest names the sides {sorted(counts)}, not {list(HANDS)}"
        width = {
            "geom": len(manifest.get("geom_cols", [])),
            "sit": len(manifest.get("sit_cols", [])),
        }
        for hand in HANDS:
            n = counts[hand]
            meta = os.path.join(pitch_dir, f"{hand}.meta.parquet")
            rows = {"meta": int(mem.execute(f"SELECT COUNT(*) FROM {_pq(meta)}").fetchone()[0])}
            for part in ("geom", "sit", "bb_row"):
                shape = _npy_shape(os.path.join(pitch_dir, f"{hand}.{part}.npy"))
                rows[part] = shape[0]
                want_ndim = 1 if part == "bb_row" else 2
                if len(shape) != want_ndim or (
                    want_ndim == 2 and width[part] and shape[1] != width[part]
                ):
                    return (
                        f"its pitch_pool/{hand}.{part}.npy has shape {shape}, not the manifest's "
                        f"{n:,} rows" + (f" x {width[part]} columns" if want_ndim == 2 else "")
                    )
            short = {k: v for k, v in rows.items() if v != n}
            if short:
                return f"its pitch_pool[{hand}] does not hold the manifest's {n:,} rows: {short}"
    except Exception as exc:  # noqa: BLE001 - any read failure means the pool is not whole
        return f"its pitch pool does not read whole ({type(exc).__name__}: {exc})"
    finally:
        mem.close()
    return None


def _copy_problem(art_dir: str, backup_dir: str) -> str | None:
    """Why ``backup_dir`` cannot be the rollback point, or None when it can.

    SIM-553: the rebuild writes ``pitch_pool/`` only. So a copy is WHOLE when
    (1) outside ``pitch_pool/`` it holds the bundle's files, byte for byte,
    and (2) its own pitch pool is whole by its own manifest
    (:func:`_pitch_pool_problem`). The copy's pitch pool is NEVER compared
    with the bundle's: after a failure past the COMMIT the bundle's may hold
    the new coding, a half-written export or a killed COPY's temporary file,
    and a whole copy is still the rollback point. A copy that fails (1) is a
    partial copy, or one the bundle has moved on from (a later export
    rewrote another pool); a copy that fails (2) is a partial copy.
    """

    def outside(root: str) -> dict[str, int]:
        return {k: v for k, v in _tree_files(root).items() if not k.startswith("pitch_pool/")}

    bundle = outside(art_dir)
    copy = outside(backup_dir)
    if set(bundle) != set(copy):
        missing = sorted(set(bundle) - set(copy))
        extra = sorted(set(copy) - set(bundle))
        return (
            f"outside pitch_pool/ its file list differs from the bundle's ({len(missing)} missing, "
            f"e.g. {missing[:3]}; {len(extra)} extra, e.g. {extra[:3]}): a partial copy, or the "
            "bundle changed after the copy was taken"
        )
    for rel in sorted(bundle):
        if bundle[rel] != copy[rel] or _sha256(os.path.join(art_dir, rel)) != _sha256(
            os.path.join(backup_dir, rel)
        ):
            return (
                f"its {rel} differs from the bundle's: a partial copy, or the bundle changed "
                "after the copy was taken"
            )
    why = _pitch_pool_problem(backup_dir)
    if why is not None:
        return f"its own pitch pool is not whole, a partial copy: {why}"
    return None


def _join_bb_row(con: Any, pitch_meta: str, bb_pitch_ids: str) -> np.ndarray:
    """SIM-553: the pitch pool's batted-ball join, computed exactly as
    ``engine_artifacts.build_battedball_pool_artifact`` computes it.

    For every row of ``pitch_meta`` (the pitch pool's meta parquet, in read
    order), the row of the batted-ball pool that holds its batted ball, or
    -1: ``join_rows`` over the meta's ``pitch_id`` and the batted-ball pool's
    ``{hand}.pitch_id.npy``.
    """
    pp_pid = np.asarray(
        np.ma.filled(
            con.execute(f"SELECT pitch_id FROM read_parquet('{pitch_meta}')").fetchnumpy()[
                "pitch_id"
            ],
            -1,
        ),
        dtype=np.int64,
    )
    bb_pid = np.asarray(np.load(bb_pitch_ids), dtype=np.int64)
    return join_rows(pp_pid, bb_pid)


def _bb_row_problems(con: Any, source: str) -> list[str]:
    """A fresh join of ``source``'s pitch and batted-ball ids must reproduce its
    own ``bb_row``. That proves, before any write, that the export's recomputed
    join (step 4) is the join the full pool export writes."""
    problems: list[str] = []
    for hand in ("L", "R"):
        pitch_dir = os.path.join(source, "pitch_pool")
        fresh = _join_bb_row(
            con,
            os.path.join(pitch_dir, f"{hand}.meta.parquet"),
            os.path.join(source, "battedball_pool", f"{hand}.pitch_id.npy"),
        )
        held = np.load(os.path.join(pitch_dir, f"{hand}.bb_row.npy"))
        same = held.shape == fresh.shape and bool(np.array_equal(held, fresh))
        _log(
            f"  the batted-ball join [{hand}]: a fresh join of {source}'s ids links "
            f"{int((fresh >= 0).sum()):,} of {len(fresh):,} pitches; it "
            f"{'reproduces' if same else 'DOES NOT reproduce'} the held bb_row  "
            f"{'OK' if same else 'MISMATCH'}"
        )
        if not same:
            problems.append(
                f"{source}: pitch_pool/{hand}.bb_row.npy is not the join of its own pitch and "
                "batted-ball ids, so a pitch-pool-only export cannot keep the join in step "
                "(an owner decision: export every pool)"
            )
    return problems


def _rollback_point_problems(con: Any, source: str, window: list[int]) -> list[str]:
    """The rollback point's pitch pool against DuckDB's window, pitch by pitch (SIM-553).

    ``source`` is the rollback point: the kept copy on a re-run, else the
    bundle. The export writes DuckDB's window, and the round-trip compares
    it with the rollback point after the COMMIT. So every difference the
    round-trip would find stops the run here, before a write:

      * the pitch ids, per batting side: the same set;
      * the classes: the same, EXCEPT the expected state on a re-run after a
        committed rebuild. There every window season reads the running
        builder in DuckDB, the rollback point reads the old coding, and the
        two differ by exactly the window's expected moves: every moving
        pitch is on its old class in the rollback point and on its new class
        in DuckDB, and no other pitch differs;
      * the geometry and situation files: DuckDB's values for the same
        pitch, row for row, with the columns the export writes. The coding
        never changes them, so a difference is a misaligned or stale file.
    """
    problems: list[str] = []
    wl = _season_list(window)
    hands = _sql_in(HANDS)
    pitch_dir = os.path.join(source, "pitch_pool")
    versions = _builder_versions(con, window)
    duck_new = all(versions.get(s) == EXPECTED_BUILDER for s in window)
    src_sql = " UNION ALL ".join(
        f"SELECT '{h}' AS hand, pitch_id, outcome_type "
        f"FROM {_pq(os.path.join(pitch_dir, f'{h}.meta.parquet'))}"
        for h in HANDS
    )
    rows = con.execute(
        f"""
        WITH s AS ({src_sql}),
        d AS (SELECT stand AS hand, pitch_id, outcome_type FROM sim.pitch_pool
              WHERE season IN ({wl}) AND stand IN {hands}),
        r AS ({_raw_code_rows_sql(window)}),
        m AS (SELECT p.pitch_id, r.expected_move AS raw_move
              FROM r JOIN sim.pitch_pool p
                ON p.game_pk = r.game_pk AND p.at_bat_number = r.at_bat_number
               AND p.pitch_number = r.pitch_number AND p.season IN ({wl}) AND p.stand IN {hands}
              WHERE r.expected_move IS NOT NULL)
        SELECT COALESCE(s.hand, d.hand) AS hand,
               COUNT(*) FILTER (WHERE s.pitch_id IS NULL),
               COUNT(*) FILTER (WHERE d.pitch_id IS NULL),
               COUNT(*) FILTER (WHERE s.pitch_id IS NOT NULL AND d.pitch_id IS NOT NULL
                                AND s.outcome_type IS DISTINCT FROM d.outcome_type),
               COUNT(*) FILTER (WHERE s.pitch_id IS NOT NULL AND d.pitch_id IS NOT NULL
                                AND m.pitch_id IS NOT NULL
                                AND s.outcome_type = split_part(m.raw_move, '>', 1)
                                AND d.outcome_type = split_part(m.raw_move, '>', 2)),
               COUNT(m.pitch_id)
        FROM s FULL OUTER JOIN d ON s.pitch_id = d.pitch_id AND s.hand = d.hand
        LEFT JOIN m ON m.pitch_id = COALESCE(s.pitch_id, d.pitch_id)
        GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    ids_ok: dict[str, bool] = dict.fromkeys(HANDS, False)
    only_duck = only_src = differ = by_move = n_moving = 0
    for hand, od, osrc, df, bm, nm in rows:
        ids_ok[str(hand)] = int(od) == 0 and int(osrc) == 0
        only_duck, only_src = only_duck + int(od), only_src + int(osrc)
        differ, by_move, n_moving = differ + int(df), by_move + int(bm), n_moving + int(nm)
        _log(
            f"  the rollback point against DuckDB [{hand}]: {int(od):,} pitches only in DuckDB, "
            f"{int(osrc):,} only in {source}; {int(df):,} with another class "
            f"({int(bm):,} of them the expected move, of {int(nm):,} moving pitches)"
        )
    if only_duck or only_src:
        problems.append(
            f"{source}'s pitch pool and DuckDB's window hold different pitches ({only_duck:,} only "
            f"in DuckDB, {only_src:,} only in the rollback point). The export writes DuckDB's "
            "window, and the round-trip compares it with the rollback point pitch by pitch, so "
            "it would fail after the COMMIT. If the rollback point is a KEPT copy from an "
            "earlier run, it is stale: move it away by hand (mv it to a dated name; never "
            "delete it — it may be the only copy on the old coding), then re-run. If it is the "
            "bundle itself, bring the bundle's pitch pool into step with DuckDB first (a pool "
            "export: an owner decision)"
        )
    if differ == 0:
        _log(
            "  the classes: the rollback point and DuckDB read the same class on every pitch  OK"
            + (
                " (both read the new coding: the round-trip warns that a rollback keeps it)"
                if duck_new and n_moving
                else ""
            )
        )
    elif duck_new and differ == by_move == n_moving:
        _log(
            f"  the classes differ by exactly the {n_moving:,} expected moves: the rollback point "
            "reads the old coding, DuckDB the new (a re-run after a committed rebuild)  OK"
        )
    else:
        problems.append(
            f"{source}'s pitch pool and DuckDB's window disagree on {differ:,} pitch classes "
            + (
                f"({by_move:,} of them the expected move, of {n_moving:,} moving pitches; the "
                "expected state is all or none of them)"
                if duck_new
                else f"(DuckDB's window builders are {versions}, so no class may differ)"
            )
            + ". The round-trip would fail after the COMMIT"
        )
    # The geometry and the situation, row for row against DuckDB.
    try:
        with open(os.path.join(pitch_dir, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        return problems + [f"{source}'s pitch pool manifest does not read ({exc})"]
    cols_ok = (
        list(manifest.get("geom_cols", _GEOM_COLS)) == _GEOM_COLS
        and list(manifest.get("sit_cols", _SIT_COLS)) == _SIT_COLS
    )
    if not cols_ok:
        problems.append(
            f"{source}'s pitch pool holds the columns {manifest.get('geom_cols')} / "
            f"{manifest.get('sit_cols')}, but the export writes {_GEOM_COLS} / {_SIT_COLS}"
        )
        return problems
    cols = _GEOM_COLS + _SIT_COLS
    for hand in HANDS:
        if not ids_ok[hand]:
            continue  # the rows cannot be aligned; the id stop above names it
        meta = os.path.join(pitch_dir, f"{hand}.meta.parquet")
        d = con.execute(
            f"SELECT {', '.join('p.' + c for c in cols)} "
            f"FROM read_parquet('{meta}', file_row_number = true) AS m "
            f"JOIN sim.pitch_pool p ON p.pitch_id = m.pitch_id AND p.season IN ({wl}) "
            f"AND p.stand = '{hand}' ORDER BY m.file_row_number"
        ).fetchnumpy()
        bad: dict[str, int] = {}
        for part, part_cols in (("geom", _GEOM_COLS), ("sit", _SIT_COLS)):
            # The export's own conversion (engine_artifacts.build_pitch_pool_artifact).
            want = np.nan_to_num(
                np.stack([np.ma.filled(d[c], np.nan).astype(np.float32) for c in part_cols], axis=1)
            ).astype(np.float32)
            held = np.load(os.path.join(pitch_dir, f"{hand}.{part}.npy"))
            if held.shape != want.shape:
                bad[part] = -1
            elif not np.array_equal(held, want):
                bad[part] = int((held != want).any(axis=1).sum())
            del want, held
        del d
        _log(
            f"  the geometry and situation [{hand}] against DuckDB, row for row: "
            + (
                "the same on every pitch  OK"
                if not bad
                else f"{bad} pitches differ (-1: another shape)  MISMATCH"
            )
        )
        if bad:
            problems.append(
                f"{source}'s pitch_pool[{hand}] geometry or situation rows differ from DuckDB's "
                f"for the same pitch ({bad} rows; -1 = another shape). The coding never changes "
                "these columns, so the file is misaligned (a killed export) or stale, and the "
                "round-trip would fail after the COMMIT"
            )
    return problems


def _bundle_problems(
    con: Any, art_dir: str, backup_dir: str, window: list[int], force: bool
) -> list[str]:
    """The bundle, its window, the rollback point, the batted-ball join and write access."""
    problems: list[str] = []
    if not os.path.isdir(os.path.join(art_dir, "pitch_pool")):
        return [f"the bundle has no pitch pool: {os.path.join(art_dir, 'pitch_pool')} is missing"]
    # The rollback point. A whole copy from an earlier run of this rebuild is
    # KEPT, with or without --force-backup: the bundle may by now hold the new
    # coding or a half-written export, so it must never be copied over the
    # copy. --force-backup replaces only a copy that is not whole, and only
    # with a bundle whose own pitch pool is whole.
    reuse = False
    exists = os.path.exists(backup_dir)
    if exists:
        why = _copy_problem(art_dir, backup_dir)
        if why is None:
            reuse = True
            _log(
                f"  the copy {backup_dir} is whole (the bundle's files outside pitch_pool/, byte "
                "for byte, and a pitch pool whole by its own manifest): the run keeps it as the "
                "rollback point (the bundle is not copied over it)"
            )
        elif force:
            _log(
                f"  the copy {backup_dir} cannot be the rollback point ({why}); --force-backup "
                "renames it aside (never deletes it) and copies the bundle, whose own pitch pool "
                "must be whole"
            )
        else:
            problems.append(
                f"the copy {backup_dir} exists but cannot be the rollback point: {why}. Pass "
                "--force-backup to rename it aside (never deleted) and copy the bundle again"
            )
    strays = _pitch_pool_strays(art_dir)
    source_whole = True
    if reuse:
        if strays:
            kept = [s for s in strays if not _export_replaces(s)]
            _log(
                f"  the bundle's pitch_pool/ holds {strays}, files no export writes (a killed "
                "export leaves these). The copy is judged on its own, so they do not count "
                "against it"
                + ("" if kept else "; the export's COPY replaces them (tmp_<side>.meta.parquet)")
            )
            if kept:
                problems.append(
                    f"the bundle's pitch_pool/ holds {kept}, which the export neither writes nor "
                    "replaces, so the exported pitch pool would not be whole. Move them out of "
                    "the bundle first"
                )
    else:
        # The bundle becomes the rollback point: its own pitch pool must be whole.
        why_bundle = _pitch_pool_problem(art_dir)
        if why_bundle is None:
            _log("  the bundle's own pitch pool is whole: the run copies it as the rollback point")
        else:
            source_whole = False
            problems.append(
                f"the bundle cannot become the rollback point: its own pitch pool is not whole "
                f"({why_bundle}). "
                + (
                    "--force-backup replaces a copy that is not whole only with a whole bundle"
                    if exists
                    else "No copy exists to roll back to"
                )
                + ". Put a whole copy of the pre-rebuild bundle back first"
            )
    # The seasons, the join and the comparison with DuckDB are read where the
    # rollback point is: the bundle's own pitch pool may be half-written on a
    # re-run.
    source = backup_dir if reuse else art_dir
    if source_whole:
        try:
            with open(os.path.join(source, "pitch_pool", "manifest.json"), encoding="utf-8") as fh:
                bundle_seasons = [int(s) for s in json.load(fh).get("seasons", [])]
        except (OSError, ValueError) as exc:
            return problems + [f"{source}'s pitch pool manifest does not read ({exc})"]
        if bundle_seasons != window:
            problems.append(
                f"{source}'s pitch pool holds seasons {bundle_seasons}, but the export would "
                f"write {window}; the export must rewrite the same window"
            )
        try:
            problems += _bb_row_problems(con, source)
        except Exception as exc:  # noqa: BLE001 - a missing or unreadable file is a stop, named
            problems.append(
                f"the batted-ball join of {source} does not read ({type(exc).__name__}: {exc})"
            )
        try:
            problems += _rollback_point_problems(con, source, window)
        except Exception as exc:  # noqa: BLE001 - an unreadable pool is a stop, named
            problems.append(
                f"the rollback point {source} does not compare with DuckDB's window "
                f"({type(exc).__name__}: {exc})"
            )
    parent = os.path.dirname(os.path.abspath(backup_dir))
    if not os.access(parent, os.W_OK):
        problems.append(f"no write access to {parent} (the backup copy goes there)")
    # The export writes pitch_pool/ only.
    d = os.path.join(art_dir, "pitch_pool")
    if not os.access(d, os.W_OK):
        problems.append(f"no write access to {d}")
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if os.path.isfile(p) and not os.access(p, os.W_OK):
            problems.append(f"no write access to {p}")
    # The export reads the batted-ball ids; the round-trip compares the rest.
    for sub in UNCHANGED_POOLS:
        if not os.path.isdir(os.path.join(art_dir, sub)):
            problems.append(f"the bundle has no {sub}/; the round-trip needs it to compare")
    for hand in ("L", "R"):
        p = os.path.join(art_dir, "battedball_pool", f"{hand}.pitch_id.npy")
        if not os.path.isfile(p):
            problems.append(f"{p} is missing; the export joins the pitch pool to it")
    if not reuse:
        size = sum(_tree_files(art_dir).values())
        free = shutil.disk_usage(parent).free
        if free < 2 * size:
            problems.append(
                f"{free / 1e9:.1f} GB free under {parent}; the copy needs {size / 1e9:.1f} GB"
            )
    return problems


def _newer_rows(con: Any, table: str, keep: str, window: list[int], pool_dir: str) -> str:
    """The DuckDB rows dated after the bundle pool's newest game, as a phrase.

    Empty when the bundle or the table carries no game date.
    """
    try:
        newest = con.execute(
            "SELECT MAX(game_ymd) FROM read_parquet("
            f"'{os.path.join(pool_dir, '*.meta.parquet')}', union_by_name = true)"
        ).fetchone()[0]
        if newest is None:
            return ""
        n, first, last = con.execute(
            f"SELECT COUNT(*), MIN(game_date), MAX(game_date) FROM {table} "
            f"WHERE season IN ({_season_list(window)}) AND {keep} "
            f"AND CAST(strftime(game_date, '%Y%m%d') AS INTEGER) > {int(newest)}"
        ).fetchone()
    except Exception:  # noqa: BLE001 - the dates are a courtesy; the counts carry the finding
        return ""
    if not n:
        return f"; the bundle's newest game {newest}, none newer in DuckDB"
    return f"; {int(n):,} DuckDB rows are newer than the bundle's newest game ({newest}): the games of {first} to {last}"


def _pool_drift(con: Any, art_dir: str, window: list[int]) -> None:
    """INFORMATION, never a stop: DuckDB's window rows against the bundle's
    manifests, for the pools this rebuild leaves alone (``DRIFT_POOLS``).

    SIM-553: the review found DuckDB's steal and advancement pools ahead of the
    bundle (the 2026 games of 08-14 to 08-29). The pitch-only export leaves
    those files as they are, so the drift does not touch this rebuild. The
    line names it so the owner can decide; the next full pool export carries it.
    """
    wl = _season_list(window)
    ahead: list[str] = []
    for sub, table, key_sql, keep in DRIFT_POOLS:
        pool_dir = os.path.join(art_dir, sub)
        try:
            with open(os.path.join(pool_dir, "manifest.json"), encoding="utf-8") as fh:
                manifest = json.load(fh)
            bundle = {str(k): int(v) for k, v in manifest.get("counts", {}).items()}
            duck = {
                str(k): int(n)
                for k, n in con.execute(
                    f"SELECT {key_sql}, COUNT(*) FROM {table} "
                    f"WHERE season IN ({wl}) AND {keep} GROUP BY 1"
                ).fetchall()
            }
        except Exception as exc:  # noqa: BLE001 - information only
            _log(f"  drift. {sub}: not read ({type(exc).__name__}: {exc})")
            continue
        diff = {
            k: duck.get(k, 0) - bundle.get(k, 0)
            for k in sorted(set(bundle) | set(duck))
            if duck.get(k, 0) != bundle.get(k, 0)
        }
        bundle_seasons = [int(s) for s in manifest.get("seasons", [])]
        seasons_note = (
            "" if bundle_seasons == window else f"; the bundle's seasons {bundle_seasons}"
        )
        if not diff and not seasons_note:
            _log(f"  drift. {sub} ({table}): {sum(duck.values()):,} rows in DuckDB and the bundle")
            continue
        ahead.append(sub)
        per_key = ", ".join(f"{k} {v:+,}" for k, v in diff.items())
        _log(
            f"  drift. {sub} ({table}): DuckDB {sum(duck.values()):,} rows, the bundle "
            f"{sum(bundle.values()):,} ({sum(diff.values()):+,}; by sub-pool: {per_key or 'none'})"
            f"{_newer_rows(con, table, keep, window, pool_dir)}{seasons_note}"
        )
    if ahead:
        _log(
            f"  drift. INFORMATION, not a stop: DuckDB's {', '.join(ahead)} differ from the "
            "bundle's. This rebuild exports the pitch pool only and leaves them as they are. "
            "Refreshing them is a separate owner decision; the next nightly '--what pool' "
            "export or 'make engine-artifacts' would carry the refresh."
        )


def _preflight(
    con: Any,
    seasons: list[int],
    window: list[int],
    art_dir: str,
    backup_dir: str,
    force: bool,
) -> list[str]:
    """Every reason the write path must not start. An empty list means go.

    It also prints the drift line (information only).
    """
    problems: list[str] = []
    if POOL_BUILDER_VERSION != EXPECTED_BUILDER:
        problems.append(
            f"POOL_BUILDER_VERSION is {POOL_BUILDER_VERSION!r}; this script rebuilds for "
            f"{EXPECTED_BUILDER!r}"
        )
    _log(f"  window (the {RECENCY_FLOOR_SEASONS} newest pool seasons, the export's): {window}")
    missing = [s for s in window if s not in seasons]
    if missing:
        problems.append(
            f"the seasons {seasons} leave out the window seasons {missing}; the export and "
            "check d need the whole window on one coding"
        )
    versions = _builder_versions(con, seasons)
    _log(f"  builder per season before: {versions}")
    pool = _pool_counts(con, seasons)
    source = _source_counts(con, seasons)
    for s in seasons:
        same = pool.get(s, 0) == source.get(s, 0) and pool.get(s, 0) > 0
        _log(
            f"  {s}: pool rows {pool.get(s, 0):,}  source rows {source.get(s, 0):,}  "
            f"{'OK' if same else 'MISMATCH'}"
        )
        if not same:
            problems.append(
                f"{s}: the pool holds {pool.get(s, 0):,} rows and raw.pitches {source.get(s, 0):,}. "
                "The source changed since the last pool build, so a pitch-pool-only rebuild would "
                "leave the outcome, steal and advancement pools behind it; run the full pool chain"
            )
    unknown = _unknown_codes(con, seasons)
    for code, strikes, n in unknown:
        problems.append(
            f"{n:,} source rows with code {code!r} at {strikes} strikes get no class under "
            "SQL_OUTCOME_TYPE (no ELSE, decision 3); add the code to a code set first"
        )
    if not unknown:
        _log("  every source code maps to a class under SQL_OUTCOME_TYPE")
    problems += _bundle_problems(con, art_dir, backup_dir, window, force)
    _pool_drift(con, art_dir, window)
    return problems


# ---------------------------------------------------------------------------
# The checks (inside the rebuild's transaction)
# ---------------------------------------------------------------------------


def _check_rows(con: Any, seasons: list[int], before: dict[int, int]) -> bool:
    """Check a: the rows per season equal the snapshot's; every row keeps its
    pitch_id; no class is NULL or outside the chain's six."""
    sl = _season_list(seasons)
    after = _pool_counts(con, seasons)
    ok = True
    for s in seasons:
        same = before.get(s) == after.get(s)
        ok = ok and same
        _log(
            f"  a. {s}: rows before {before.get(s, 0):,}  after {after.get(s, 0):,}  "
            f"{'OK' if same else 'MISMATCH'}"
        )
    matched = con.execute(
        "SELECT COUNT(*) FROM _sim553_before b "
        "JOIN sim.pitch_pool n ON n.pitch_id = b.pitch_id AND n.season = b.season"
    ).fetchone()[0]
    total = sum(before.values())
    good = int(matched) == total
    ok = ok and good
    _log(
        f"  a. rows matched to the snapshot by pitch_id: {matched:,} of {total:,} {'OK' if good else 'MISMATCH'}"
    )
    nulls = con.execute(
        f"SELECT COUNT(*) FROM sim.pitch_pool WHERE season IN ({sl}) AND outcome_type IS NULL"
    ).fetchone()[0]
    odd = con.execute(
        f"SELECT outcome_type, COUNT(*) FROM sim.pitch_pool WHERE season IN ({sl}) "
        f"AND outcome_type NOT IN {_sql_in(OUTCOMES)} GROUP BY 1"
    ).fetchall()
    good = nulls == 0 and not odd
    ok = ok and good
    _log(
        f"  a. NULL classes {nulls}; classes outside {OUTCOMES}: {odd or 'none'} "
        f"{'OK' if good else 'MISMATCH'}"
    )
    return ok


def _move_verdict(expected: str | None, actual: str | None) -> str:
    if expected is None:
        return "unchanged" if actual is None else "UNEXPECTED"
    if actual is None:
        return "MISSED"
    return "moved" if actual == expected else "WRONG"


def _check_moves(con: Any, seasons: list[int], window: list[int], rerun: list[int]) -> bool:
    """Check b: the rows that changed class are exactly the expected rows.

    Joins the snapshot to the new pool on ``pitch_id`` (every row), and the
    MOVE_CODES rows of ``raw.pitches`` to the new pool on the pitch's key. A
    season the snapshot already held at the running builder (a re-run)
    expects no move (``expected_move`` is blanked). ``raw_move`` keeps each
    pitch's move from its raw code, un-blanked: the round-trip grades the
    copy-to-export changes on it (:func:`_window_moves`), so a re-run grades
    the same as a first run.
    """
    sl = _season_list(seasons)
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE _sim553_changed AS
        SELECT b.pitch_id, n.season, n.stand, b.outcome_type AS old_class,
               n.outcome_type AS new_class, n.game_pk, n.at_bat_number, n.pitch_number, n.got_away
        FROM _sim553_before b
        JOIN sim.pitch_pool n ON n.pitch_id = b.pitch_id AND n.season = b.season
        WHERE b.outcome_type IS DISTINCT FROM n.outcome_type
        """
    )
    rerun_list = _season_list(rerun) if rerun else "NULL"
    # Every MOVE_CODES pool row with its source code, count and expected move
    # (check c reads the code from here too).
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _sim553_expected AS
        WITH r AS ({_raw_code_rows_sql(seasons)})
        SELECT p.pitch_id, p.season, p.stand, r.code, r.strikes, r.events,
               CASE WHEN p.season IN ({rerun_list}) THEN NULL ELSE r.expected_move END
                   AS expected_move,
               r.expected_move AS raw_move
        FROM r JOIN sim.pitch_pool p
          ON p.game_pk = r.game_pk AND p.at_bat_number = r.at_bat_number
         AND p.pitch_number = r.pitch_number AND p.season IN ({sl})
        """
    )
    rows = con.execute(
        """
        SELECT COALESCE(c.season, e.season) AS season, e.code, e.strikes, e.expected_move,
               CASE WHEN c.pitch_id IS NULL THEN NULL
                    ELSE c.old_class || '>' || c.new_class END AS actual_move,
               COUNT(*) AS n
        FROM _sim553_changed c FULL OUTER JOIN _sim553_expected e ON e.pitch_id = c.pitch_id
        GROUP BY ALL
        """
    ).fetchall()
    # A changed row outside the MOVE_CODES has no code yet: look it up.
    outside = con.execute(
        f"""
        SELECT c.season, TRIM(r.type) AS code, r.strikes,
               c.old_class || '>' || c.new_class AS actual_move, COUNT(*)
        FROM _sim553_changed c
        JOIN pg.raw.pitches r
          ON r.game_pk = c.game_pk AND r.at_bat_number = c.at_bat_number
         AND r.pitch_number = c.pitch_number
        WHERE r.data_quality_flag = FALSE AND r.season IN ({sl})
          AND (r.type IS NULL OR TRIM(r.type) NOT IN {_sql_in(MOVE_CODES)})
        GROUP BY ALL
        """
    ).fetchall()
    table: dict[tuple[str, str, str, str], list[int]] = {}
    unexplained = 0
    for season, code, strikes, expected, actual, n in rows:
        if code is None:
            unexplained += int(n)  # re-counted with its code from ``outside``
            continue
        key = (str(code), str(strikes), expected or "-", actual or "-")
        cell = table.setdefault(key, [0, 0])
        cell[1] += int(n)
        if int(season) in window:
            cell[0] += int(n)
    looked_up = 0
    for season, code, strikes, actual, n in outside:
        looked_up += int(n)
        key = (str(code), str(strikes), "-", actual)
        cell = table.setdefault(key, [0, 0])
        cell[1] += int(n)
        if int(season) in window:
            cell[0] += int(n)
    ok = looked_up == unexplained
    if not ok:
        _log(
            f"  b. {unexplained:,} changed rows have no MOVE_CODES source row, and the code "
            f"lookup found {looked_up:,}: a changed row has no raw.pitches row  MISMATCH"
        )
    _log("  b. the class moves, by code and strike count (window 2023-2026 | all seasons):")
    _log(
        f"     {'code':<5}{'strikes':<9}{'expected move':<24}{'actual move':<24}{'window':>9}{'all':>9}  verdict"
    )
    moved = [0, 0]
    for key in sorted(table):
        code, strikes, expected, actual = key
        win_n, all_n = table[key]
        verdict = _move_verdict(
            None if expected == "-" else expected, None if actual == "-" else actual
        )
        if verdict in ("UNEXPECTED", "MISSED", "WRONG"):
            ok = False
        if actual != "-":
            moved[0] += win_n
            moved[1] += all_n
        _log(
            f"     {code:<5}{strikes:<9}{expected:<24}{actual:<24}{win_n:>9,}{all_n:>9,}  {verdict}"
        )
    if rerun:
        _log(
            f"  b. seasons already at {POOL_BUILDER_VERSION} before the rebuild expect no move: {rerun}"
        )
    _log(
        f"  b. rows that changed class: window {moved[0]:,} (measured 2026-09-23: "
        f"{WINDOW_EXPECTED_MOVES:,}), all seasons {moved[1]:,} (about "
        f"{ALL_SEASONS_EXPECTED_MOVES:,}) {'OK' if ok else 'MISMATCH'}"
    )
    return ok


def _window_moves(con: Any, window: list[int]) -> tuple[list[int], list[str]]:
    """Every window pitch the export writes (the batting sides 'L' and 'R')
    whose raw code moves under the new coding: its ``pitch_id`` and its move
    ``'old>new'`` (check b's ``raw_move``, never blanked on a re-run). The
    round-trip reads these after the writable connection closes."""
    rows = con.execute(
        "SELECT pitch_id, raw_move FROM _sim553_expected "
        f"WHERE raw_move IS NOT NULL AND season IN ({_season_list(window)}) "
        "AND stand IN ('L', 'R') ORDER BY pitch_id"
    ).fetchall()
    return [int(r[0]) for r in rows], [str(r[1]) for r in rows]


def _check_got_away(con: Any, window: list[int]) -> bool:
    """Check c: no moved foul tip or foul bunt carries the got-away flag.

    A moved T / L / O row is a strike three on a swing now, and a got-away
    flag would let the dropped-third-strike rule (SIM-484) put the batter on
    base. By rule a foul tip is caught, so the flag must be absent (the
    design's §2.5). The gate covers those rows only. A moved 'Q' is a real
    swing and miss (at a pitchout), so a passed ball on it is a real got-away,
    exactly as on an 'S' or 'W' row: the check reports such rows by name and
    does not fail on them. (The one on 2026-09-25: game 824606, at-bat 14,
    pitch 8, a strikeout on a swinging pitchout with a passed ball.)
    """
    wl = _season_list(window)
    foul_tips = _sql_in(STRIKE_THREE_FOUL_TYPES)
    gated, gated_win, other, null_n, n_all = con.execute(
        f"""
        SELECT COUNT(*) FILTER (WHERE c.got_away AND e.code IN {foul_tips}),
               COUNT(*) FILTER (WHERE c.got_away AND e.code IN {foul_tips}
                                AND c.season IN ({wl})),
               COUNT(*) FILTER (WHERE c.got_away AND e.code IS DISTINCT FROM NULL
                                AND e.code NOT IN {foul_tips}),
               COUNT(*) FILTER (WHERE c.got_away IS NULL),
               COUNT(*)
        FROM _sim553_changed c LEFT JOIN _sim553_expected e ON e.pitch_id = c.pitch_id
        """
    ).fetchone()
    ok = int(gated) == 0
    _log(
        f"  c. moved {'/'.join(STRIKE_THREE_FOUL_TYPES)} rows with the got-away flag: {gated:,} "
        f"(window {gated_win:,}) of {n_all:,} moved rows; flag NULL on {null_n:,} "
        f"{'OK' if ok else 'MISMATCH'}"
    )
    if int(other):
        _log(
            f"  c. reported, not gated: {other:,} other moved rows carry the flag (a real swing "
            "and miss with a passed ball or a wild pitch):"
        )
        for season, pid, code, strikes, events, move in con.execute(
            """
            SELECT c.season, c.pitch_id, e.code, e.strikes, e.events,
                   c.old_class || '>' || c.new_class
            FROM _sim553_changed c JOIN _sim553_expected e ON e.pitch_id = c.pitch_id
            WHERE c.got_away AND e.code NOT IN """
            + foul_tips
            + " ORDER BY c.pitch_id LIMIT 20"
        ).fetchall():
            _log(
                f"     {season} pitch_id {pid}: code {code} at {strikes} strikes, "
                f"events {events}, {move}"
            )
    return ok


def _check_label(con: Any, window: list[int], prefix: str = "d.") -> bool:
    """Check d: the count chain of the window pool's plate appearances against
    real plate appearances, every channel within LABEL_CHECK_TOLERANCE.

    SIM-553: both sides cover the same groups. The chain reads only the pool
    rows of the at-bats that end in a batting event (``pa_only``), the rule
    ``real_pa_rates`` applies. A chain over every row also plays on the
    at-bats that never reached an end (an inning-ending pickoff, a game ended
    mid-at-bat), about half of them balls, which pushes walks up about 0.6%.
    The full-pool chain is the band centre (the simulator draws every row),
    so it prints for the record.

    The rule holds only while both sides read the same plate appearances.
    So the check also compares the counts: the groups the ``pa_only`` chain
    reads (``pa_group_count``, from the chain's own SQL) must equal
    ``real_pa_rates``' ``n_pa``, or the check FAILS (for example, when
    ``raw.pitches`` gained games the pool has not been rebuilt for).
    """
    pred = f"season IN ({_season_list(window)})"
    chain = chain_rates(con, pred, pa_only=True)
    real = real_pa_rates(con, pred)
    n_real = int(real["n_pa"])
    n_pool = pa_group_count(con, pred)
    rows = label_check(chain, real)
    _log(
        f"  {prefix} the count chain of the pool's plate appearances (the at-bats that end in a "
        f"batting event) against {n_real:,} real plate appearances ({pred}), "
        f"tolerance {LABEL_CHECK_TOLERANCE:.1%}:"
    )
    _log(f"     {format_pa_count(n_pool, n_real)}")
    for line in format_label_check(rows):
        _log(f"     {line}")
    full = chain_rates(con, pred)
    weighted = chain_rates(con, pred, weighted=True)
    _log(
        f"  {prefix} for the record — the band centres (the chain over every pool row, which the "
        f"simulator draws): unweighted K/PA {full['k']:.4f}  BB/PA {full['walk']:.4f}  "
        f"HBP/PA {full['hbp']:.4f}  in play {full['in_play']:.4f}  "
        f"pitches/PA {full['pitches']:.3f}; recency-weighted K/PA {weighted['k']:.4f}  "
        f"BB/PA {weighted['walk']:.4f}  HBP/PA {weighted['hbp']:.4f}  "
        f"in play {weighted['in_play']:.4f}  pitches/PA {weighted['pitches']:.3f}"
    )
    ok = n_pool == n_real and all(row[4] for row in rows)
    _log(f"  {prefix} label check {'PASS' if ok else 'FAIL'}")
    return ok


def _check_metadata(con: Any, seasons: list[int]) -> bool:
    """Check e: the build metadata names the running builder for every season,
    and its row count is the table's."""
    after = _pool_counts(con, seasons)
    meta = {
        int(s): (v, int(n))
        for s, v, n in con.execute(
            "SELECT season, builder_version, row_count FROM sim.pool_build_metadata "
            f"WHERE pool_name = 'pitch_pool' AND season IN ({_season_list(seasons)})"
        ).fetchall()
    }
    ok = True
    for s in seasons:
        version, n = meta.get(s, (None, -1))
        good = version == POOL_BUILDER_VERSION and n == after.get(s)
        ok = ok and good
        _log(f"  e. {s}: builder {version}  row_count {n:,}  {'OK' if good else 'MISMATCH'}")
    return ok


def _matches_snapshot(
    con: Any, seasons: list[int], before: dict[int, int], versions: dict[int, str | None]
) -> bool:
    """TRUE when the pool holds the snapshot's rows, classes and builders."""
    if _pool_counts(con, seasons) != before or _builder_versions(con, seasons) != versions:
        return False
    differ = con.execute(
        "SELECT COUNT(*) FROM _sim553_before b "
        "LEFT JOIN sim.pitch_pool n ON n.pitch_id = b.pitch_id AND n.season = b.season "
        "WHERE n.outcome_type IS DISTINCT FROM b.outcome_type"
    ).fetchone()[0]
    return int(differ) == 0


def _rollback(
    con: Any, seasons: list[int], before: dict[int, int], versions: dict[int, str | None]
) -> None:
    """Roll the rebuild's transaction back and show the table is as it was."""
    try:
        con.execute("ROLLBACK")
    except Exception as exc:  # noqa: BLE001 - report, then verify what the table holds
        _log(f"  ROLLBACK raised {type(exc).__name__}: {exc}")
    restored = _matches_snapshot(con, seasons, before, versions)
    _log(
        "  the transaction is rolled back: sim.pitch_pool "
        + (
            "holds its old rows, classes and builders"
            if restored
            else "DOES NOT match the snapshot — read it before the next export"
        )
    )


def _restore(
    con: Any, seasons: list[int], before: dict[int, int], versions: dict[int, str | None]
) -> None:
    """--no-transaction: put the snapshot's classes and builders back after a
    failed check. The rebuild committed row by row (the SIM-518 pattern), so
    there is no transaction to roll back. The other columns stay rebuilt from
    the source; only the class and the builder carry the old coding."""
    try:
        matched = con.execute(
            "SELECT COUNT(*) FROM _sim553_before b "
            "JOIN sim.pitch_pool n ON n.pitch_id = b.pitch_id AND n.season = b.season"
        ).fetchone()[0]
        if _pool_counts(con, seasons) != before or int(matched) != sum(before.values()):
            _log(
                "  RESTORE NOT POSSIBLE: sim.pitch_pool's rows differ from the snapshot. The "
                "bundle is untouched and the app is safe, but do not run the nightly "
                "engine-artifact export until the pool is rebuilt and checked"
            )
            return
        con.execute(
            "UPDATE sim.pitch_pool AS n SET outcome_type = b.outcome_type "
            "FROM _sim553_before AS b "
            "WHERE n.pitch_id = b.pitch_id AND n.outcome_type IS DISTINCT FROM b.outcome_type"
        )
        for s, v in versions.items():
            con.execute(
                "UPDATE sim.pool_build_metadata SET builder_version = ? "
                "WHERE pool_name = 'pitch_pool' AND season = ?",
                [v, s],
            )
    except Exception as exc:  # noqa: BLE001 - report, then verify what the table holds
        _log(f"  the restore raised {type(exc).__name__}: {exc}")
    restored = _matches_snapshot(con, seasons, before, versions)
    _log(
        "  restored from the snapshot: sim.pitch_pool "
        + (
            "holds its old classes and builders"
            if restored
            else "DOES NOT match the snapshot — read it before the next export"
        )
    )


# ---------------------------------------------------------------------------
# The bundle: copy, export, round-trip
# ---------------------------------------------------------------------------


def _backup_bundle(art_dir: str, backup_dir: str, force: bool) -> str:
    """Copy the bundle aside, and return the rollback point (``backup_dir``).

    A whole copy from an earlier run of this rebuild (:func:`_copy_problem`)
    IS the rollback point, with or without ``--force-backup``, and the bundle
    is never copied over it: after a failure past the COMMIT the bundle may
    hold the new coding or a half-written export. ``--force-backup`` only
    renames aside (never deletes) a copy that is NOT whole. The bundle
    becomes the rollback point only when its own pitch pool is whole
    (:func:`_pitch_pool_problem`); the fresh copy must then pass the same
    test as a kept one.
    """
    if os.path.exists(backup_dir):
        why = _copy_problem(art_dir, backup_dir)
        if why is None:
            strays = _pitch_pool_strays(art_dir)
            _log(
                f"  kept {backup_dir} as the rollback point: it is whole (the bundle's files "
                "outside pitch_pool/, byte for byte, and a pitch pool whole by its own manifest); "
                "the bundle is NOT copied over it"
                + (f". The bundle's stray files, not counted: {strays}" if strays else "")
            )
            return backup_dir
        if not force:
            raise RuntimeError(
                f"{backup_dir} exists and cannot be the rollback point ({why}); "
                "re-run with --force-backup"
            )
        why_bundle = _pitch_pool_problem(art_dir)
        if why_bundle is not None:
            raise RuntimeError(
                f"{backup_dir} cannot be the rollback point ({why}), and the bundle cannot replace "
                f"it: its own pitch pool is not whole ({why_bundle}). Nothing was renamed or copied"
            )
        aside = f"{backup_dir}.{time.strftime('%Y%m%d-%H%M%S')}"
        os.rename(backup_dir, aside)
        _log(
            f"  {backup_dir} cannot be the rollback point ({why}); moved it to {aside} (not deleted)"
        )
    else:
        why_bundle = _pitch_pool_problem(art_dir)
        if why_bundle is not None:
            raise RuntimeError(
                f"the bundle cannot become the rollback point: its own pitch pool is not whole "
                f"({why_bundle}), and no copy exists. Nothing was copied"
            )
    t = time.time()
    shutil.copytree(art_dir, backup_dir, symlinks=True)
    _log(f"  copied {art_dir} -> {backup_dir} in {time.time() - t:.0f} s")
    why = _copy_problem(art_dir, backup_dir)
    if why is not None:
        raise RuntimeError(f"the fresh copy {backup_dir} is not whole ({why})")
    _log(f"  the fresh copy is whole by the kept copy's test ({time.time() - t:.0f} s in all)")
    return backup_dir


def _export(duckdb_path: str, art_dir: str, window: list[int]) -> None:
    """Step 4: export the pitch pool ONLY, then recompute its batted-ball join.

    ``build_pitch_pool_artifact`` (the nightly export's own function) writes
    ``pitch_pool/`` from DuckDB, read-only. The rebuild re-inserted the rows,
    so their file order moved, and ``pitch_pool/{hand}.bb_row.npy`` (each
    pitch's row in the batted-ball pool) must move with them. It is
    recomputed exactly as ``build_battedball_pool_artifact`` computes it
    (:func:`_join_bb_row`), against the bundle's untouched
    ``battedball_pool/{hand}.pitch_id.npy``. The batted-ball, steal and
    advancement pools are not written: DuckDB's steal and advancement pools
    are ahead of the bundle (the drift line), and refreshing them is a
    separate owner decision. The round-trip proves all three untouched.
    """
    ro = duckdb.connect(duckdb_path, read_only=True)
    try:
        counts = build_pitch_pool_artifact(ro, art_dir, window)
        for hand in ("L", "R"):
            bb_row = _join_bb_row(
                ro,
                os.path.join(art_dir, "pitch_pool", f"{hand}.meta.parquet"),
                os.path.join(art_dir, "battedball_pool", f"{hand}.pitch_id.npy"),
            )
            np.save(os.path.join(art_dir, "pitch_pool", f"{hand}.bb_row.npy"), bb_row)
            _log(
                f"  pitch_pool[{hand}]: {counts[hand]:,} rows exported; bb_row recomputed "
                f"({int((bb_row >= 0).sum()):,} pitches link a batted ball)"
            )
    finally:
        ro.close()


def _npy_equal(a_path: str, b_path: str) -> bool:
    a = np.load(a_path, mmap_mode="r")
    b = np.load(b_path, mmap_mode="r")
    if a.shape != b.shape or a.dtype != b.dtype:
        return False
    return bool(np.array_equal(a, b, equal_nan=bool(np.issubdtype(a.dtype, np.floating))))


def _columns(mem: Any, path: str) -> list[tuple[str, str]]:
    return [
        (str(r[0]), str(r[1]))
        for r in mem.execute(f"DESCRIBE SELECT * FROM {_pq(path)}").fetchall()
    ]


def _parquet_equal(mem: Any, a_path: str, b_path: str) -> tuple[bool, str]:
    """Row-for-row equality of two parquet files, in file order."""
    cols = _columns(mem, a_path)
    if cols != _columns(mem, b_path):
        return False, "the columns differ"
    cond = " OR ".join(f'a."{c}" IS DISTINCT FROM b."{c}"' for c, _t in cols) or "FALSE"
    diff = mem.execute(
        f"""
        SELECT COUNT(*) FILTER (WHERE a.file_row_number IS NULL OR b.file_row_number IS NULL
                                OR {cond})
        FROM read_parquet('{a_path}', file_row_number = true) a
        FULL OUTER JOIN read_parquet('{b_path}', file_row_number = true) b
          ON a.file_row_number = b.file_row_number
        """
    ).fetchone()[0]
    if int(diff) == 0:
        return True, "the same rows in the same order"
    only_a = mem.execute(
        f"SELECT COUNT(*) FROM (SELECT * FROM {_pq(a_path)} EXCEPT ALL SELECT * FROM {_pq(b_path)})"
    ).fetchone()[0]
    only_b = mem.execute(
        f"SELECT COUNT(*) FROM (SELECT * FROM {_pq(b_path)} EXCEPT ALL SELECT * FROM {_pq(a_path)})"
    ).fetchone()[0]
    if int(only_a) == 0 and int(only_b) == 0:
        return False, f"the same rows in a DIFFERENT order ({diff:,} positions differ)"
    return (
        False,
        f"{diff:,} rows differ ({only_a:,} only in the copy, {only_b:,} only in the export)",
    )


def _compare_pool_dir(mem: Any, old_dir: str, new_dir: str) -> bool:
    """One pool the export does not write, against the rollback point, file by file.

    Every file must be byte-identical to the copy's, or equal in content (the
    same array, the same parquet rows in the same order, the same JSON).
    """
    name = os.path.basename(new_dir)
    old_files = sorted(f for f in os.listdir(old_dir) if os.path.isfile(os.path.join(old_dir, f)))
    new_files = sorted(f for f in os.listdir(new_dir) if os.path.isfile(os.path.join(new_dir, f)))
    ok = old_files == new_files
    if not ok:
        _log(
            f"  {name}: the file lists differ — only in the copy "
            f"{sorted(set(old_files) - set(new_files))}, only in the export "
            f"{sorted(set(new_files) - set(old_files))}  MISMATCH"
        )
    identical = same_content = 0
    for f in sorted(set(old_files) & set(new_files)):
        a, b = os.path.join(old_dir, f), os.path.join(new_dir, f)
        if _sha256(a) == _sha256(b):
            identical += 1
            continue
        if f.endswith(".npy"):
            equal, detail = _npy_equal(a, b), "array content"
        elif f.endswith(".parquet"):
            equal, detail = _parquet_equal(mem, a, b)
        elif f.endswith(".json"):
            with open(a, encoding="utf-8") as fa, open(b, encoding="utf-8") as fb:
                equal, detail = json.load(fa) == json.load(fb), "json content"
        else:
            equal, detail = False, "no content comparison for this file type"
        if equal:
            same_content += 1
        else:
            ok = False
            _log(f"  {name}/{f}: the bytes differ; {detail}  MISMATCH")
    _log(
        f"  {name}: {len(new_files)} files — {identical} byte-identical, {same_content} the "
        f"same content  {'OK' if ok else 'MISMATCH'}"
    )
    return ok


def _file_order_ids(mem: Any, path: str) -> np.ndarray:
    d = mem.execute(
        f"SELECT pitch_id FROM read_parquet('{path}', file_row_number = true) ORDER BY file_row_number"
    ).fetchnumpy()
    return np.asarray(np.ma.filled(d["pitch_id"], -1), dtype=np.int64)


def _coding_label(n_moving: int, n_old: int, n_new: int) -> tuple[str, bool]:
    """The coding a pitch pool reads, from its window pitches that move.

    ``n_moving`` counts those pitches in the pool, ``n_old`` the ones on
    their old class and ``n_new`` the ones on their new class. Returns the
    label and whether the pool reads ONE coding (never a mix).
    """
    if n_moving == 0:
        return "no pitch moves in the window: the two codings agree", True
    if n_old == n_moving:
        return "the OLD coding (the rollback restores it)", True
    if n_new == n_moving:
        return (
            "the NEW coding. WARNING: the rollback point already holds the new coding, so a "
            "rollback does NOT restore the old one",
            True,
        )
    return (
        f"a MIX of the two codings ({n_old:,} moving pitches on the old class, {n_new:,} on the "
        f"new, {n_moving - n_old - n_new:,} on neither)",
        False,
    )


def _copy_coding(rollback_dir: str, moves: tuple[list[int], list[str]]) -> str:
    """The coding the rollback point's pitch pool holds, read from its files now.

    The rollback text names it on every path, the export-failure path too,
    where no round-trip runs.
    """
    ids, raw_moves = moves
    mem = duckdb.connect()
    try:
        mem.execute(
            "CREATE TEMP TABLE _sim553_mv AS "
            "SELECT UNNEST(?::BIGINT[]) AS pitch_id, UNNEST(?::VARCHAR[]) AS raw_move",
            [ids, raw_moves],
        )
        found = n_old = n_new = 0
        for hand in HANDS:
            meta = os.path.join(rollback_dir, "pitch_pool", f"{hand}.meta.parquet")
            row = mem.execute(
                "SELECT COUNT(*), "
                "COUNT(*) FILTER (WHERE c.outcome_type = split_part(m.raw_move, '>', 1)), "
                "COUNT(*) FILTER (WHERE c.outcome_type = split_part(m.raw_move, '>', 2)) "
                f"FROM {_pq(meta)} c JOIN _sim553_mv m ON m.pitch_id = c.pitch_id"
            ).fetchone()
            f, o, n = row if row is not None else (0, 0, 0)
            found, n_old, n_new = found + int(f), n_old + int(o), n_new + int(n)
    except Exception as exc:  # noqa: BLE001 - the rollback text must still print
        return f"NOT READ ({type(exc).__name__}: {exc}); read the copy before you roll back"
    finally:
        mem.close()
    label, _one = _coding_label(found, n_old, n_new)
    if found != len(ids):
        label += f" ({found:,} of the {len(ids):,} moving window pitches are in the copy)"
    return label


def _roundtrip_pitch_pool(
    mem: Any,
    duckdb_path: str,
    art_dir: str,
    rollback_dir: str,
    window: list[int],
    moves: tuple[list[int], list[str]],
) -> bool:
    """The exported pitch pool, graded on its own (SIM-553).

    ``moves`` holds every window pitch whose raw code moves under the new
    coding, with its move ``'old>new'`` (:func:`_window_moves`). The grade
    never reads this run's DuckDB difference, so a first run and a re-run
    (the copy on the old coding or on the new) grade the same way.

    Hard, per batting side:
      * the exported pitch pool is whole by its own manifest, with no stray
        file (:func:`_pitch_pool_problem`, the rollback point's own test);
      * every exported pitch has DuckDB's class for the same ``pitch_id``
        (a full join: no pitch missing on either side, no class different);
      * every per-row file has the meta's rows;
      * the export and the copy hold the same ``pitch_id`` set;
      * every class change from the copy to the export is that pitch's
        expected move, and every pitch with a move carries its new class;
      * each pitch keeps its batted-ball row (``bb_row`` aligned on
        ``pitch_id``), and the linked pitches number the batted-ball
        manifest's ``pitch_join``;
      * each pitch keeps its geometry and situation rows (``geom`` and
        ``sit`` aligned on ``pitch_id``). The rebuild leaves these columns as
        they are (the review's dry run, and the pre-flight's row-for-row
        read against DuckDB), so a difference means the copy or the export
        is misaligned.
    Hard, over both sides: the copy reads ONE coding, every moving pitch on
    its old class or every one on its new class, never a mix.
    Reported only: the copy-to-export class pairs, and any other meta column
    that differs from the copy (the rebuild recomputes those columns from
    ``raw.pitches``; checks a-e read the table against the source).
    """
    wl = _season_list(window)
    ids, raw_moves = moves
    mem.execute(
        "CREATE OR REPLACE TEMP TABLE _sim553_exp AS "
        "SELECT UNNEST(?::BIGINT[]) AS pitch_id, UNNEST(?::VARCHAR[]) AS raw_move",
        [ids, raw_moves],
    )
    try:
        with open(
            os.path.join(art_dir, "battedball_pool", "manifest.json"), encoding="utf-8"
        ) as fh:
            pitch_join = {str(k): int(v) for k, v in json.load(fh).get("pitch_join", {}).items()}
    except (OSError, ValueError):
        pitch_join = {}
    new_dir = os.path.join(art_dir, "pitch_pool")
    old_dir = os.path.join(rollback_dir, "pitch_pool")
    whole = _pitch_pool_problem(art_dir)
    ok = whole is None
    _log(
        "  the exported pitch pool by its own manifest: "
        + ("whole, no stray file  OK" if whole is None else f"{whole}  MISMATCH")
    )
    pairs: dict[tuple[str, str], int] = {}
    n_meta: dict[str, int] = {}
    n_expected = copy_old = copy_new = 0
    mem.execute(f"ATTACH '{duckdb_path}' AS sim553_db (READ_ONLY)")
    try:
        for hand in ("L", "R"):
            new_meta = os.path.join(new_dir, f"{hand}.meta.parquet")
            old_meta = os.path.join(old_dir, f"{hand}.meta.parquet")
            duck_rows = (
                "SELECT pitch_id, outcome_type FROM sim553_db.sim.pitch_pool "
                f"WHERE season IN ({wl}) AND stand = '{hand}'"
            )
            counts = {
                str(c): int(n)
                for c, n in mem.execute(
                    f"SELECT outcome_type, COUNT(*) FROM {_pq(new_meta)} GROUP BY 1"
                ).fetchall()
            }
            duck = {
                str(c): int(n)
                for c, n in mem.execute(
                    f"SELECT outcome_type, COUNT(*) FROM ({duck_rows}) GROUP BY 1"
                ).fetchall()
            }
            _log(f"  pitch_pool[{hand}] classes, export: {dict(sorted(counts.items()))}")
            _log(f"  pitch_pool[{hand}] classes, DuckDB: {dict(sorted(duck.items()))}")
            n_meta[hand] = sum(counts.values())
            for part in ("geom", "sit", "bb_row"):
                rows = _npy_rows(os.path.join(new_dir, f"{hand}.{part}.npy"))
                good = rows == n_meta[hand]
                ok = ok and good
                if not good:
                    _log(
                        f"  pitch_pool[{hand}].{part}.npy: {rows:,} rows, the meta "
                        f"{n_meta[hand]:,}  MISMATCH"
                    )
            # (i) Every exported pitch against DuckDB, by pitch_id.
            only_duck, only_export, differ = mem.execute(
                "SELECT COUNT(*) FILTER (WHERE n.pitch_id IS NULL), "
                "COUNT(*) FILTER (WHERE d.pitch_id IS NULL), "
                "COUNT(*) FILTER (WHERE n.pitch_id IS NOT NULL AND d.pitch_id IS NOT NULL "
                "AND n.outcome_type IS DISTINCT FROM d.outcome_type) "
                f"FROM {_pq(new_meta)} n FULL OUTER JOIN ({duck_rows}) d ON n.pitch_id = d.pitch_id"
            ).fetchone()
            good = int(only_duck) == 0 and int(only_export) == 0 and int(differ) == 0
            ok = ok and good
            _log(
                f"  (i) pitch_pool[{hand}] against DuckDB by pitch_id: {int(only_duck):,} only in "
                f"DuckDB, {int(only_export):,} only in the export, {int(differ):,} with another "
                f"class  {'OK' if good else 'MISMATCH'}"
            )
            # Against the copy, row by row on pitch_id (the rebuild re-inserts
            # the rows, so the file order moves; the pitch_id does not).
            new_cols = [c for c, _t in _columns(mem, new_meta)]
            old_cols = {c for c, _t in _columns(mem, old_meta)}
            common = [
                c for c in new_cols if c in old_cols and c not in ("pitch_id", "outcome_type")
            ]
            diff_sql = "".join(
                f', COUNT(*) FILTER (WHERE o."{c}" IS DISTINCT FROM n."{c}")' for c in common
            )
            row = mem.execute(
                f"SELECT COUNT(*) FILTER (WHERE o.pitch_id IS NULL), "
                f"COUNT(*) FILTER (WHERE n.pitch_id IS NULL){diff_sql} "
                f"FROM {_pq(old_meta)} o FULL OUTER JOIN {_pq(new_meta)} n "
                "ON o.pitch_id = n.pitch_id"
            ).fetchone()
            only_new, only_old = int(row[0]), int(row[1])
            good = only_new == 0 and only_old == 0
            ok = ok and good
            _log(
                f"  pitch_pool[{hand}] against the copy: {only_old:,} pitch_ids only in the copy, "
                f"{only_new:,} only in the export  {'OK' if good else 'MISMATCH'}"
            )
            drift = {c: int(v) for c, v in zip(common, row[2:], strict=True) if int(v)}
            # (ii) Every copy-to-export class change is that pitch's expected
            # move; every pitch with a move carries its new class.
            unexplained, wrong, n_exp, not_new, c_old, c_new = mem.execute(
                "SELECT COUNT(*) FILTER (WHERE o.outcome_type <> n.outcome_type "
                "AND e.raw_move IS NULL), "
                "COUNT(*) FILTER (WHERE o.outcome_type <> n.outcome_type AND e.raw_move "
                "IS NOT NULL AND e.raw_move <> (o.outcome_type || '>' || n.outcome_type)), "
                "COUNT(e.pitch_id), "
                "COUNT(*) FILTER (WHERE e.pitch_id IS NOT NULL "
                "AND n.outcome_type IS DISTINCT FROM split_part(e.raw_move, '>', 2)), "
                "COUNT(*) FILTER (WHERE e.pitch_id IS NOT NULL "
                "AND o.outcome_type = split_part(e.raw_move, '>', 1)), "
                "COUNT(*) FILTER (WHERE e.pitch_id IS NOT NULL "
                "AND o.outcome_type = split_part(e.raw_move, '>', 2)) "
                f"FROM {_pq(old_meta)} o JOIN {_pq(new_meta)} n ON o.pitch_id = n.pitch_id "
                "LEFT JOIN _sim553_exp e ON e.pitch_id = o.pitch_id"
            ).fetchone()
            good = int(unexplained) == 0 and int(wrong) == 0 and int(not_new) == 0
            ok = ok and good
            _log(
                f"  (ii) pitch_pool[{hand}] class changes from the copy: {int(unexplained):,} with "
                f"no expected move, {int(wrong):,} unlike the expected move; {int(not_new):,} of "
                f"{int(n_exp):,} moving pitches without their new class in the export  "
                f"{'OK' if good else 'MISMATCH'}"
            )
            n_expected += int(n_exp)
            copy_old += int(c_old)
            copy_new += int(c_new)
            for o, n, k in mem.execute(
                f"SELECT o.outcome_type, n.outcome_type, COUNT(*) FROM {_pq(old_meta)} o "
                f"JOIN {_pq(new_meta)} n ON o.pitch_id = n.pitch_id "
                "WHERE o.outcome_type <> n.outcome_type GROUP BY 1, 2"
            ).fetchall():
                pairs[(str(o), str(n))] = pairs.get((str(o), str(n)), 0) + int(k)
            # Each pitch keeps its batted ball.
            pid_old = _file_order_ids(mem, old_meta)
            pid_new = _file_order_ids(mem, new_meta)
            o_ord = np.argsort(pid_old, kind="stable")
            n_ord = np.argsort(pid_new, kind="stable")
            if np.array_equal(pid_old[o_ord], pid_new[n_ord]):
                for part in ("bb_row", "geom", "sit"):
                    a = np.load(os.path.join(old_dir, f"{hand}.{part}.npy"))
                    b = np.load(os.path.join(new_dir, f"{hand}.{part}.npy"))
                    if len(a) != len(pid_old) or len(b) != len(pid_new) or a.shape != b.shape:
                        ok = False
                        _log(
                            f"  pitch_pool[{hand}].{part}.npy: shape {a.shape} in the copy, "
                            f"{b.shape} in the export, against {len(pid_new):,} meta rows: the "
                            "rows cannot be aligned on pitch_id  MISMATCH"
                        )
                        del a, b
                        continue
                    a, b = a[o_ord], b[n_ord]
                    if not np.array_equal(a, b):
                        ok = False
                        if part == "bb_row":
                            _log(
                                f"  pitch_pool[{hand}].bb_row: a pitch points at a different "
                                "batted ball  MISMATCH"
                            )
                        else:
                            n_bad = int((a != b).any(axis=1).sum()) if a.ndim == 2 else -1
                            _log(
                                f"  pitch_pool[{hand}].{part}.npy: {n_bad:,} pitches hold other "
                                "values in the export than in the copy (aligned on pitch_id). "
                                "The rebuild leaves these columns as they are, so the copy or "
                                "the export is misaligned  MISMATCH"
                            )
                    del a, b
            else:
                ok = False
                _log(
                    f"  pitch_pool[{hand}]: the per-row files cannot be aligned on pitch_id  MISMATCH"
                )
            linked = int((np.load(os.path.join(new_dir, f"{hand}.bb_row.npy")) >= 0).sum())
            if hand in pitch_join:
                good = linked == pitch_join[hand]
                ok = ok and good
                _log(
                    f"  pitch_pool[{hand}].bb_row: {linked:,} pitches link a batted ball; the "
                    f"batted-ball manifest's pitch_join {pitch_join[hand]:,}  "
                    f"{'OK' if good else 'MISMATCH'}"
                )
            if drift:
                _log(
                    f"  pitch_pool[{hand}] WARNING (reported, not gated): meta columns other than "
                    "the class differ from the copy on these pitches; the rebuild recomputes them "
                    f"from raw.pitches: {drift}"
                )
    finally:
        mem.execute("DETACH sim553_db")
    # (iii) The copy reads one coding.
    good = n_expected == len(ids)
    ok = ok and good
    if not good:
        _log(
            f"  (iii) {len(ids):,} window pitches move under the new coding, and the export and "
            f"the copy hold {n_expected:,} of them  MISMATCH"
        )
    label, one_coding = _coding_label(n_expected, copy_old, copy_new)
    ok = ok and one_coding
    _log(f"  (iii) the copy reads {label}{'' if one_coding else '  MISMATCH'}")
    _log(
        "  pitch_pool class changes from the copy to the export (for the record): "
        f"{dict(sorted(pairs.items()))}"
    )
    with open(os.path.join(new_dir, "manifest.json"), encoding="utf-8") as fh:
        new_manifest = json.load(fh)
    with open(os.path.join(old_dir, "manifest.json"), encoding="utf-8") as fh:
        old_manifest = json.load(fh)
    good = (
        [int(s) for s in new_manifest.get("seasons", [])] == window
        and new_manifest.get("counts") == n_meta
        and old_manifest.get("counts") == n_meta
    )
    ok = ok and good
    _log(
        f"  pitch_pool manifest: seasons {new_manifest.get('seasons')} counts "
        f"{new_manifest.get('counts')} (the copy {old_manifest.get('counts')}) "
        f"{'OK' if good else 'MISMATCH'}"
    )
    return ok


def _roundtrip(
    duckdb_path: str,
    art_dir: str,
    rollback_dir: str,
    window: list[int],
    moves: tuple[list[int], list[str]],
) -> bool:
    """Step 5: the exported pitch pool (:func:`_roundtrip_pitch_pool`), then the
    pools the export does not write, file by file against the rollback point."""
    mem = duckdb.connect()  # in-memory: the bundle's files, DuckDB attached read-only
    try:
        ok = _roundtrip_pitch_pool(mem, duckdb_path, art_dir, rollback_dir, window, moves)
        for sub in UNCHANGED_POOLS:
            ok = (
                _compare_pool_dir(mem, os.path.join(rollback_dir, sub), os.path.join(art_dir, sub))
                and ok
            )
    finally:
        mem.close()
    return ok


def _print_rollback(art_dir: str, rollback_dir: str, moves: tuple[list[int], list[str]]) -> None:
    rejected = f"{art_dir}.sim553_rejected.{time.strftime('%Y%m%d-%H%M%S')}"
    _log(f"ROLLBACK (the bundle): stop the app, move {rollback_dir} back, start the app:")
    _log("    docker compose stop app")
    _log(
        "    MSYS_NO_PATHCONV=1 docker compose run --rm -T app sh -c "
        f"'mv {art_dir} {rejected} && mv {rollback_dir} {art_dir}'"
    )
    _log("    docker compose up -d app")
    _log(
        f"  The app then draws on the coding the copy holds, read from the copy now: "
        f"{_copy_coding(rollback_dir, moves)}."
    )
    _log(
        "  sim.pitch_pool in DuckDB keeps the new coding. The app's boot loads that table only "
        "into the pitch-to-pitch engine, which no route and no sim draw uses."
    )
    _log(
        "  HOLD every pool export until sim.pitch_pool is rebuilt on the coding you keep: "
        "make engine-artifacts, the nightly chain (scripts/nightly_ingest.sh step 3, also run "
        "by the scheduler) and any engine_artifacts --what pool or --what all. Each one exports "
        "the table's coding again and undoes this rollback."
    )
    _log(
        "  To make the rollback last: revert the code, then rebuild sim.pitch_pool for every "
        "season (2017-2026), then export. A nightly run rebuilds the current season only and "
        "leaves a mixed pool."
    )


def _smoke_games() -> str:
    """The before smoke's ten games (``scripts/sim553_smoke_before.json``), for the after smoke."""
    try:
        with open(_ROOT / "scripts" / "sim553_smoke_before.json", encoding="utf-8") as fh:
            before = json.load(fh)
        games = " ".join(str(int(g)) for g in before["game_pks"])
        return f"--iters {int(before['iters_per_game'])} {games}"
    except (OSError, ValueError, KeyError, TypeError):
        return "--iters <the before smoke's> <the ten game_pks of scripts/sim553_smoke_before.json>"


def _print_next_steps() -> None:
    _log("NEXT (the run book, design §10), from the repo root:")
    _log(
        "  4. docker compose up -d app — the boot log reads build_all_engines: 11/11 and the "
        "bundle loads"
    )
    _log("  5. the census on the pool window, strict (exit 1 on a label-check FAIL):")
    _log(
        '       MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" '
        "app python scripts/pool_window_census.py --windows W1 --strict"
    )
    _log(
        "     then restate the four band centres (K_PA, BB_PA, HBP_PA, PITCHES_PA) in "
        "tests/acceptance/bands.py from the census's FULL-POOL chain (every pool row, which the "
        "simulator draws), NOT the label check's plate-appearance chain; expected about "
        "0.2262 / 0.0829 / 0.0112 / 3.906"
    )
    _log(
        "     then the standing label check (SIM_ACCEPTANCE=1: a missing store fails, never skips):"
    )
    _log(
        "       MSYS_NO_PATHCONV=1 docker compose run --rm -T -e SIM_ACCEPTANCE=1 "
        '-v "$PWD/tests:/app/tests" -v "$PWD/pipeline:/app/pipeline" '
        '-v "$PWD/pyproject.toml:/app/pyproject.toml" app python -m pytest '
        "tests/acceptance/test_sim553_pool_label_check.py -p no:cacheprovider"
    )
    _log(
        "  6. the balanced set's score on the rebuilt pool: the chosen dates must be the set's in "
        "tests/acceptance/bands.py, and every channel's deviation inside 0.6% (strikeouts about "
        "+0.30%):"
    )
    _log(
        '       MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" '
        "app python scripts/sim523_game_set.py --slates 3 "
        "--json-out /app/scripts/sim553_game_set_after.json"
    )
    _log(
        "  7. the after smoke, the before smoke's games and iterations; expected strikeouts +4 to "
        "+5% a team-game, walks -2 to -3%, runs about -3%:"
    )
    _log(
        '       MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" '
        "app python scripts/sim_stats.py --json-out /app/scripts/sim553_smoke_after.json "
        f"{_smoke_games()}"
    )


# ---------------------------------------------------------------------------
# The two modes
# ---------------------------------------------------------------------------


def _code_report(con: Any, seasons: list[int], window: list[int]) -> None:
    """--check-only: the class of every MOVE_CODES pitch at each strike count,
    today and under the new coding, joined pool to source."""
    sl = _season_list(seasons)
    rows = con.execute(
        f"""
        WITH r AS ({_raw_code_rows_sql(seasons)})
        SELECT p.season, r.code, r.strikes, p.outcome_type AS pool_class, r.new_class,
               COUNT(*) AS n, COUNT(*) FILTER (WHERE r.got_away_raw) AS n_got_away
        FROM r LEFT JOIN sim.pitch_pool p
          ON p.game_pk = r.game_pk AND p.at_bat_number = r.at_bat_number
         AND p.pitch_number = r.pitch_number AND p.season IN ({sl})
        GROUP BY ALL
        """
    ).fetchall()
    table: dict[tuple[str, str, str, str], list[int]] = {}
    moves = [0, 0]
    # [window, all] moved rows the source labels got-away: the foul tips and
    # foul bunts (check c's gate) and the other codes (reported only).
    got_away_gated = [0, 0]
    got_away_other = [0, 0]
    unmatched = 0
    for season, code, strikes, pool_class, new_class, n, n_ga in rows:
        if season is None:
            unmatched += int(n)
            continue
        key = (str(code), str(strikes), str(pool_class), str(new_class))
        cell = table.setdefault(key, [0, 0])
        in_window = int(season) in window
        cell[1] += int(n)
        if in_window:
            cell[0] += int(n)
        if pool_class != new_class:
            flagged = got_away_gated if code in STRIKE_THREE_FOUL_TYPES else got_away_other
            moves[1] += int(n)
            flagged[1] += int(n_ga)
            if in_window:
                moves[0] += int(n)
                flagged[0] += int(n_ga)
    _log(
        f"  the class of every {'/'.join(MOVE_CODES)} pitch by strike count (pool = today's class; "
        f"new = the {POOL_BUILDER_VERSION} coding):"
    )
    _log(
        f"     {'code':<5}{'strikes':<9}{'pool class':<17}{'new class':<17}{'window':>9}{'all':>9}"
    )
    for key in sorted(table):
        code, strikes, pool_class, new_class = key
        win_n, all_n = table[key]
        mark = "  moves" if pool_class != new_class else ""
        _log(
            f"     {code:<5}{strikes:<9}{pool_class:<17}{new_class:<17}{win_n:>9,}{all_n:>9,}{mark}"
        )
    _log(
        f"  the rebuild would move {moves[0]:,} window rows (measured 2026-09-23: "
        f"{WINDOW_EXPECTED_MOVES:,}) and {moves[1]:,} rows in {seasons[0]}-{seasons[-1]} "
        f"(about {ALL_SEASONS_EXPECTED_MOVES:,} for 2017-2026)"
    )
    _log(
        f"  of those, {'/'.join(STRIKE_THREE_FOUL_TYPES)} rows the source labels got-away (check c "
        f"must read 0): window {got_away_gated[0]:,}, all {got_away_gated[1]:,}; other codes "
        f"(reported, not gated): window {got_away_other[0]:,}, all {got_away_other[1]:,}"
    )
    if unmatched:
        _log(f"  WARNING: {unmatched:,} source rows of these codes have no pool row")


def _check_only(args: argparse.Namespace) -> int:
    seasons = sorted(set(args.seasons))
    art_dir = args.art_dir.rstrip("/")
    backup_dir = art_dir + BACKUP_SUFFIX
    _log(f"=== SIM-553 check-only (read-only; writes nothing): {args.duckdb_path} ===")
    con = duckdb.connect(args.duckdb_path, read_only=True)
    try:
        attach_pg(con)
        window = last_n_seasons(con)
        _log("=== the write path's pre-flight (informational here) ===")
        problems = _preflight(con, seasons, window, art_dir, backup_dir, args.force_backup)
        for p in problems:
            _log(f"  PRE-FLIGHT: {p}")
        if not problems:
            _log("  the pre-flight is clear (the DuckDB lock is tested only on the write path)")
        _log("=== check d on the pool as it stands ===")
        ok = _check_label(con, window)
        _log("=== the codes the new coding moves ===")
        _code_report(con, seasons, window)
    finally:
        con.close()
    _log(f"SIM-553 CHECK-ONLY COMPLETE: label check {'PASS' if ok else 'FAIL'}")
    return EXIT_OK if ok else EXIT_STOPPED


def _open_writable(path: str) -> Any:
    """Open the DuckDB file writable, or return None when another process holds its lock."""
    try:
        return duckdb.connect(path)
    except duckdb.IOException as exc:
        if "lock" in str(exc).lower():
            return None
        raise


def _rebuild(args: argparse.Namespace, t0: float) -> int:
    seasons = sorted(set(args.seasons))
    duckdb_path, art_dir = args.duckdb_path, args.art_dir.rstrip("/")
    backup_dir = art_dir + BACKUP_SUFFIX
    _log(f"=== SIM-553 pitch-pool rebuild: seasons {seasons}, builder {POOL_BUILDER_VERSION} ===")
    _log("=== 0. pre-flight ===")
    if POOL_BUILDER_VERSION != EXPECTED_BUILDER:
        _log(
            f"SIM-553 REBUILD FAILED: the builder is {POOL_BUILDER_VERSION!r}, not {EXPECTED_BUILDER!r}"
        )
        return EXIT_STOPPED
    if not os.path.isfile(duckdb_path):
        # duckdb.connect would CREATE an empty database at a wrong path.
        _log(f"SIM-553 REBUILD FAILED: {duckdb_path} does not exist")
        return EXIT_STOPPED
    con = _open_writable(duckdb_path)
    if con is None:
        _log(
            "the app is running: stop it first — SIM-524 (the app's worker server holds the DuckDB "
            "writer lock; any other container that reads the DuckDB file holds a lock too)"
        )
        _log("SIM-553 REBUILD FAILED: the DuckDB file is locked")
        return EXIT_LOCKED
    try:
        attach_pg(con)
        window = last_n_seasons(con)
        problems = _preflight(con, seasons, window, art_dir, backup_dir, args.force_backup)
        if problems:
            for p in problems:
                _log(f"  PRE-FLIGHT: {p}")
            _log("SIM-553 REBUILD FAILED at the pre-flight: nothing was written")
            return EXIT_STOPPED

        _log("=== 1. snapshot ===")
        before = _pool_counts(con, seasons)
        versions = _builder_versions(con, seasons)
        rerun = [s for s in seasons if versions.get(s) == POOL_BUILDER_VERSION]
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _sim553_before AS "
            "SELECT pitch_id, season, outcome_type FROM sim.pitch_pool "
            f"WHERE season IN ({_season_list(seasons)})"
        )
        _log(f"  snapshot: {sum(before.values()):,} rows")

        # One transaction by default: a failed check, an exception or a killed
        # process leaves the table exactly as it was. --no-transaction runs the
        # SIM-518 autocommit pattern instead and restores from the snapshot.
        use_tx = not args.no_transaction
        undo = _rollback if use_tx else _restore
        mode = "one transaction" if use_tx else "autocommit (--no-transaction)"
        _log(f"=== 2. rebuild ({POOL_BUILDER_VERSION}), {mode} ===")
        t = time.time()
        if use_tx:
            con.execute("BEGIN TRANSACTION")
        try:
            comp = PlayerProfileComputor.__new__(PlayerProfileComputor)
            comp._conn = con
            comp._build_pitch_pool(seasons, incremental=False)
            _log(f"  rebuilt in {(time.time() - t) / 60:.1f} min")
            _log("=== 3. checks ===")
            ok_a = _check_rows(con, seasons, before)
            ok_b = _check_moves(con, seasons, window, rerun)
            ok_c = _check_got_away(con, window)
            ok_d = _check_label(con, window)
            ok_e = _check_metadata(con, seasons)
            # Read here, before the COMMIT: an error still undoes the rebuild.
            moves = _window_moves(con, window)
            _log(f"  the window's moving pitches, for the round-trip: {len(moves[0]):,}")
        except Exception:
            _log("  the rebuild or a check raised; undoing it")
            undo(con, seasons, before, versions)
            raise
        failed = [
            name
            for name, good in zip("abcde", (ok_a, ok_b, ok_c, ok_d, ok_e), strict=True)
            if not good
        ]
        if failed:
            undo(con, seasons, before, versions)
            _log(
                f"SIM-553 REBUILD FAILED at check {', '.join(failed)}: undone; the bundle is "
                "untouched. Hold the next nightly pool build too: it rebuilds on the new coding"
            )
            return EXIT_STOPPED
        if use_tx:
            con.execute("COMMIT")
        _log("  every check passed" + (": COMMIT" if use_tx else ""))
    finally:
        con.close()

    _log("=== 4. bundle copy + pitch-pool export ===")
    try:
        rollback_dir = _backup_bundle(art_dir, backup_dir, args.force_backup)
    except Exception as exc:  # noqa: BLE001 - the bundle is untouched; say so and stop
        _log(f"  the copy failed: {type(exc).__name__}: {exc}")
        _log(
            "SIM-553 REBUILD FAILED at the bundle copy: sim.pitch_pool is rebuilt (committed); the "
            f"bundle is untouched. A partial copy may remain at {backup_dir}. Fix the cause, then "
            "re-run with --force-backup (it renames that copy aside, never deletes it, and copies "
            "again). The rebuild repeats with no class move. If the reason above says the "
            "bundle's own pitch pool is not whole, --force-backup cannot help: put a whole copy "
            "of the pre-rebuild bundle back first."
        )
        return EXIT_STOPPED
    try:
        t = time.time()
        _export(duckdb_path, art_dir, window)
        _log(f"  exported in {time.time() - t:.0f} s")
    except Exception:
        traceback.print_exc(file=sys.stdout)
        _print_rollback(art_dir, rollback_dir, moves)
        _log(
            "SIM-553 REBUILD FAILED at the export: the bundle's pitch_pool/ may be half-written. "
            f"Roll it back (above), or re-run: the re-run keeps {rollback_dir} as the rollback "
            "point and exports again"
        )
        return EXIT_BUNDLE
    _log("=== 5. round-trip ===")
    try:
        ok = _roundtrip(duckdb_path, art_dir, rollback_dir, window, moves)
    except Exception:
        traceback.print_exc(file=sys.stdout)
        _log("  the round-trip raised")
        ok = False
    if not ok:
        _print_rollback(art_dir, rollback_dir, moves)
        _log(
            "SIM-553 REBUILD FAILED at the round-trip: read the MISMATCH lines, then roll the "
            "bundle back (above)"
        )
        return EXIT_BUNDLE

    _log("=== 6. done ===")
    _print_rollback(art_dir, rollback_dir, moves)
    _print_next_steps()
    _log(f"SIM-553 REBUILD COMPLETE in {(time.time() - t0) / 60:.1f} min")
    return EXIT_OK


def _parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    ap.add_argument(
        "--seasons",
        type=int,
        nargs="+",
        default=list(DEFAULT_SEASONS),
        help="the pool seasons to rebuild (default 2017-2026; must hold the window)",
    )
    ap.add_argument(
        "--check-only",
        action="store_true",
        help="read-only: the pre-flight, check d and the per-code classes; no rebuild, no export",
    )
    ap.add_argument(
        "--force-backup",
        action="store_true",
        help=f"when <bundle>{BACKUP_SUFFIX} exists but is not whole (a partial copy, or one the "
        "bundle has moved on from), rename it aside (never delete it) and copy the bundle again, "
        "provided the bundle's own pitch pool is whole; a whole copy is always kept",
    )
    ap.add_argument(
        "--no-transaction",
        action="store_true",
        help="rebuild in autocommit (the SIM-518 pattern) and undo a failed check from the "
        "snapshot; the fallback if the one-transaction rebuild runs out of memory",
    )
    ap.add_argument("--duckdb-path", default=DUCKDB_PATH)
    ap.add_argument("--art-dir", default=ART_DIR, help="the engine-artifact bundle")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    # One stream in one order: the builder's and the exporter's log lines go to
    # stdout beside this script's own lines (force: a pipeline import may have
    # configured logging already).
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="[%(asctime)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    t0 = time.time()
    try:
        return _check_only(args) if args.check_only else _rebuild(args, t0)
    except Exception as exc:  # noqa: BLE001 - one FAILED line, then the traceback
        traceback.print_exc(file=sys.stdout)
        _log(
            f"SIM-553 {'CHECK-ONLY' if args.check_only else 'REBUILD'} FAILED: {type(exc).__name__}: {exc}"
        )
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
