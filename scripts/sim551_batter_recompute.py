"""
scripts/sim551_batter_recompute.py — SIM-551: the batter profiles rebuilt at ONE
cutoff, on their own (not the five-hour full rebuild).

Why: on 2026-09-11 a rebuild of the four pool seasons left
``derived.batter_season_metrics`` at two stamps (2017-2022 as of 2026-09-10,
2023-2026 as of 2026-09-11). The batter engine refuses a mixed set, so the app
booted ``build_all_engines: 10/11`` from then on. The computor now refuses such a
rebuild before it writes and stamps the whole table after it (SIM-551); this
script runs that corrected batter section for every season.

What it does, in order (the app must be STOPPED — a DuckDB write; the forkserver's
lock, SIM-524):

  1. reads the table's stamps before the rebuild and prints them;
  2. rebuilds ``derived.batter_season_metrics`` for the seasons through the
     computor's own ``_compute_batter_profiles`` (the SIM-534 point-in-time query,
     the SIM-529 physical block, the SIM-522 platoon fix — one query for every
     season, so the content is consistent across seasons too), in one
     transaction: the seasons' old rows deleted first, so no row from an older
     build survives;
  3. recomputes the ``batter`` rows of ``derived.league_averages`` for the
     seasons (the engine's shrinkage target). ONLY the batter rows: the writer's
     other blocks would overwrite the runner rows that the SIM-531 recompute
     wrote with their new keys;
  4. verifies: rows per season, ONE stamp on the table, the physical block's
     coverage for 2023 on, and a build of the batter engine over every season —
     the same build the app runs at boot.

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim551_batter_recompute.py --seasons 2017 2018 ... 2026

Then: ``docker compose start app`` and check the boot log for
``build_all_engines: 11/11``; then rebuild the batter actor matrix
(``python -m pipeline.batch.engine_artifacts --what actors_sim --matrix batter``,
read-only, the app may run).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duckdb  # noqa: E402

from pipeline.batch.player_profile_computor import PlayerProfileComputor  # noqa: E402

log = logging.getLogger("sim551_batter_recompute")

DEFAULT_DUCKDB = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
DEFAULT_DSN = os.environ.get("BASEBALL_DB_DSN", "")
TABLE = "derived.batter_season_metrics"

#: The first season Savant publishes swing tracking; the physical block is
#: expected NULL before it.
FIRST_PHYSICAL_SEASON = 2023

#: The least batter-seasons with a bat-speed figure a COMPLETED season may
#: carry from 2023 on (the live table reads 571-671 per season).
PHYSICAL_COVERAGE_FLOOR = 500


def _connect_writable(duckdb_path: str) -> duckdb.DuckDBPyConnection:
    try:
        return duckdb.connect(duckdb_path)
    except duckdb.IOException as exc:
        raise SystemExit(
            f"cannot open {duckdb_path} for writing ({exc}). The app holds the DuckDB "
            "writer lock (SIM-524): stop it first — docker compose stop app."
        ) from exc


def stamps(duckdb_path: str) -> list[tuple]:
    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        return con.execute(
            f"SELECT asof_date, MIN(season), MAX(season), COUNT(*) FROM {TABLE} "
            "GROUP BY 1 ORDER BY 1"
        ).fetchall()
    finally:
        con.close()


def recompute_batter_league_averages(duckdb_path: str, seasons: list[int]) -> None:
    """The ``batter`` block of ``LeagueAverageProfiles.compute``, alone."""
    season_list = ", ".join(str(s) for s in seasons)
    con = _connect_writable(duckdb_path)
    try:
        con.execute(f"""
            INSERT OR REPLACE INTO derived.league_averages
            SELECT
                'batter' AS entity_type, season,
                JSON_OBJECT(
                    'first_pitch_take_rate', AVG(first_pitch_take_rate),
                    'o_swing_rate',          AVG(o_swing_rate),
                    'z_swing_rate',          AVG(z_swing_rate),
                    'whiff_rate',            AVG(whiff_rate),
                    'contact_rate',          AVG(contact_rate),
                    'walk_rate',             AVG(walk_rate),
                    'k_rate',                AVG(k_rate),
                    'avg_exit_velo',         AVG(avg_exit_velo),
                    'hard_hit_rate',         AVG(hard_hit_rate)
                ) AS profile_json,
                CURRENT_TIMESTAMP AS updated_at
            FROM {TABLE}
            WHERE season IN ({season_list})
              AND below_minimum_sample = FALSE
            GROUP BY season
        """)
        n = con.execute(
            "SELECT COUNT(*) FROM derived.league_averages WHERE entity_type = 'batter' "
            f"AND season IN ({season_list})"
        ).fetchone()[0]
    finally:
        con.close()
    log.info("batter league-average rows for the seasons: %d", n)


def verify(duckdb_path: str, seasons: list[int]) -> None:
    problems: list[str] = []
    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        rows = con.execute(
            f"SELECT season, COUNT(*), COUNT(bat_speed), SUM(below_minimum_sample::int), "
            f"MAX(asof_date) FROM {TABLE} GROUP BY 1 ORDER BY 1"
        ).fetchall()
        for season, n, n_phys, below, asof_max in rows:
            log.info(
                "  batters %s: %d rows, %d with a bat-speed figure, %d below minimum, asof %s",
                season,
                n,
                n_phys,
                below,
                asof_max,
            )
            if season in seasons and n == 0:
                problems.append(f"season {season}: no rows")
            if (
                season in seasons
                and FIRST_PHYSICAL_SEASON <= season < date.today().year
                and n_phys < PHYSICAL_COVERAGE_FLOOR
            ):
                problems.append(
                    f"season {season}: only {n_phys} rows carry bat_speed "
                    f"(floor {PHYSICAL_COVERAGE_FLOOR}) — the Savant swing joins missed"
                )
        n_stamps = con.execute(
            f"SELECT COUNT(DISTINCT COALESCE(asof_date::VARCHAR, 'NULL')) FROM {TABLE}"
        ).fetchone()[0]
        if n_stamps != 1:
            problems.append(f"{n_stamps} distinct asof_date stamps on {TABLE}; must be 1")
        n_league = con.execute(
            "SELECT COUNT(*) FROM derived.league_averages WHERE entity_type = 'batter'"
        ).fetchone()[0]
        have = {
            int(s)
            for (s,) in con.execute(
                "SELECT season FROM derived.league_averages WHERE entity_type = 'batter'"
            ).fetchall()
        }
        missing = [s for s in seasons if s not in have]
        log.info("batter league-average rows: %d", n_league)
        if missing:
            problems.append(f"no batter league-average row for seasons {missing}")
    finally:
        con.close()

    # The build the app runs at boot: every season, no filter. This is the
    # call that raised "built at different cutoffs" for five days.
    from similarity.engines.batter_similarity import BatterSimilarityEngine

    t0 = time.perf_counter()
    engine = BatterSimilarityEngine(duckdb_path=duckdb_path)
    try:
        engine.build()
    except Exception as exc:  # noqa: BLE001 — the verdict, not a crash
        problems.append(f"the batter engine refuses to build: {exc}")
    else:
        log.info(
            "batter engine built: %d profiles as of %s in %.1fs",
            len(engine._profiles),
            engine.asof_date,
            time.perf_counter() - t0,
        )

    if problems:
        for p in problems:
            log.error("VERIFY: %s", p)
        raise SystemExit(f"{len(problems)} verification problem(s); see the log")
    log.info("verify: every check passed")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--seasons", type=int, nargs="+", required=True)
    ap.add_argument("--duckdb-path", default=DEFAULT_DUCKDB)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument(
        "--skip-profiles", action="store_true", help="only the league averages + verify"
    )
    args = ap.parse_args(argv)
    if not args.dsn and not args.skip_profiles:
        log.error("no Postgres DSN (BASEBALL_DB_DSN or --dsn)")
        return 2
    t0 = time.perf_counter()
    for asof, lo, hi, n in stamps(args.duckdb_path):
        log.info("before: asof %s on seasons %s-%s (%d rows)", asof, lo, hi, n)
    if not args.skip_profiles:
        computor = PlayerProfileComputor(pg_dsn=args.dsn, duckdb_path=args.duckdb_path)
        computor._connect()
        try:
            # One transaction: the old rows of the seasons go, the new rows
            # come, the whole table is stamped — or none of it happens. A
            # failure half-way would otherwise leave the app a hollow table.
            computor._conn.begin()
            try:
                season_list = ", ".join(str(s) for s in args.seasons)
                computor._conn.execute(f"DELETE FROM {TABLE} WHERE season IN ({season_list})")
                computor._compute_batter_profiles(args.seasons, asof=None)
            except BaseException:
                computor._conn.rollback()
                raise
            computor._conn.commit()
        finally:
            computor._close()
        log.info("batter profiles recomputed in %.0fs", time.perf_counter() - t0)
    recompute_batter_league_averages(args.duckdb_path, args.seasons)
    for asof, lo, hi, n in stamps(args.duckdb_path):
        log.info("after: asof %s on seasons %s-%s (%d rows)", asof, lo, hi, n)
    verify(args.duckdb_path, args.seasons)
    log.info("done in %.0fs", time.perf_counter() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
