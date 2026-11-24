"""Load Savant's per-pitch tracking measurements, one day per request.

The loader writes ``raw.savant_pitch_tracking``, one row per pitch.

WHY PER PITCH AND NOT PER SEASON
--------------------------------
The bat-tracking and swing-path leaderboards publish season averages. A season
average cannot feed a game inside that season: a bat speed averaged over 2025
holds how the batter swung in September. Savant's pitch-level search export
carries the same measurements on each pitch. The batter profile averages them
from our own tables, up to its cutoff date.

ONE DAY PER REQUEST: CORRECTNESS, NOT TUNING
--------------------------------------------
The export stops at exactly 25,000 rows and says nothing. One day of a full
schedule is far below the cap; one week is over it. So the loader asks for one
day. It **fails** when a day comes back at the cap, because that day is
incomplete and nothing tells which rows are missing.

The loader also checks every row's date against the day it asked for. Some
leaderboards ignore their parameters silently, so no date parameter is trusted.

WHAT EXISTS WHEN
----------------
The bat-tracking columns (bat speed, swing length, attack angle and direction,
swing path tilt, both intercepts) start in **2024**. A 2023 day returns none of
them, although the bat-tracking leaderboard publishes 2023 season values. For
2023 the leaderboard is the only source. On 2023 this loader is worth running
only for ``arm_angle`` and ``hyper_speed``.

A full 2023 season average is still safe for any cutoff after 2023: it holds
nothing from after the cutoff. Only the season that contains the cutoff needs
per-pitch data, and every season from 2024 on has it.

USAGE
-----
    python -m ingest.savant.load_pitch_tracking --seasons 2024
    python -m ingest.savant.load_pitch_tracking --start 2024-04-01 --end 2024-04-30
    python -m ingest.savant.load_pitch_tracking --seasons 2023 2024 --resume

``--resume`` skips the days that already have rows.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time
from urllib.parse import urlencode

import psycopg2
from psycopg2.extras import execute_batch

from db.dsn import require_dsn
from ingest.games.coercion import to_float, to_int
from ingest.savant.load_leaderboards import (
    PAUSE_BETWEEN_FETCHES_S,
    fetch_csv,
    parse_rows,
    upsert_sql_for,
)

log = logging.getLogger(__name__)

SEARCH_URL = "https://baseballsavant.mlb.com/statcast_search/csv"

#: The export's hard row cap. A day that hits it is incomplete, and the export
#: does not say which rows it dropped.
ROW_CAP = 25_000

#: (export column, target column). The key columns are separate.
TRACKING_COLUMNS: tuple[tuple[str, str], ...] = (
    ("bat_speed", "bat_speed"),
    ("swing_length", "swing_length"),
    ("attack_angle", "attack_angle"),
    ("attack_direction", "attack_direction"),
    ("swing_path_tilt", "swing_path_tilt"),
    ("intercept_ball_minus_batter_pos_x_inches", "intercept_x"),
    ("intercept_ball_minus_batter_pos_y_inches", "intercept_y"),
    ("arm_angle", "arm_angle"),
    ("hyper_speed", "hyper_speed"),
)

_KEY_COLUMNS = ("game_pk", "at_bat_number", "pitch_number")
_META_COLUMNS = ("game_date", "season", "batter", "pitcher")

#: The fallback window of days, for a season with no games in the database yet.
#: Regular-season play never falls outside it.
SEASON_FIRST_MONTH_DAY = (3, 15)
SEASON_LAST_MONTH_DAY = (11, 15)


class TruncatedDayError(RuntimeError):
    """A single day hit the export's row cap, so its data is incomplete."""


def build_url(day: dt.date) -> str:
    """Return the search-export URL for the regular-season pitches of ``day``."""
    params = {
        "all": "true",
        "hfSea": f"{day.year}|",
        "player_type": "batter",
        "game_date_gt": day.isoformat(),
        "game_date_lt": day.isoformat(),
        # Regular season only, as the leaderboards' ``gameType=Regular``. Without
        # it, a March request returns spring-training pitches no pool row joins to.
        "hfGT": "R|",
        "type": "details",
    }
    return f"{SEARCH_URL}?{urlencode(params)}"


def coerce_row(row: dict[str, str], day: dt.date) -> dict[str, object] | None:
    """Turn one export row into one payload. Return None when its key is incomplete."""
    game_pk = to_int(row.get("game_pk"))
    ab = to_int(row.get("at_bat_number"))
    pitch = to_int(row.get("pitch_number"))
    if game_pk is None or ab is None or pitch is None:
        return None
    out: dict[str, object] = {
        "game_pk": game_pk,
        "at_bat_number": ab,
        "pitch_number": pitch,
        "game_date": day,
        "season": day.year,
        "batter": to_int(row.get("batter")),
        "pitcher": to_int(row.get("pitcher")),
    }
    for src, target in TRACKING_COLUMNS:
        out[target] = to_float(row.get(src))
    return out


def check_day(rows: list[dict[str, str]], day: dt.date) -> None:
    """Raise ``TruncatedDayError`` on a day at the cap, and ``RuntimeError`` on another date."""
    if len(rows) >= ROW_CAP:
        raise TruncatedDayError(
            f"{day}: {len(rows)} rows, at or over the {ROW_CAP} cap. The export "
            f"drops the remainder silently, so this day is incomplete."
        )
    wrong = {str(r.get("game_date")) for r in rows if r.get("game_date") != day.isoformat()}
    if wrong:
        raise RuntimeError(f"{day}: rows carry other dates {sorted(wrong)[:5]!r}")


#: The upsert of one pitch, keyed on the game, the plate appearance and the pitch.
UPSERT_SQL = upsert_sql_for(
    "raw.savant_pitch_tracking",
    (*_KEY_COLUMNS, *_META_COLUMNS, *(t for _, t in TRACKING_COLUMNS)),
    _KEY_COLUMNS,
)


def load_day(conn, day: dt.date, *, dry_run: bool = False) -> int:
    """Fetch and upsert one day's pitches. Return the number of rows (a dry run writes none)."""
    rows = parse_rows(fetch_csv(build_url(day)))
    if not rows:
        return 0
    check_day(rows, day)
    payload = [p for p in (coerce_row(r, day) for r in rows) if p is not None]
    if not payload:
        return 0
    if dry_run:
        return len(payload)
    with conn.cursor() as cur:
        execute_batch(cur, UPSERT_SQL, payload, page_size=500)
    return len(payload)


def season_days(season: int, conn=None) -> list[dt.date]:
    """Return the days to request for ``season``.

    The function prefers the days with games in ``raw.pitches``: any other day
    is a request that returns nothing. With no games for the season in the
    database, it falls back to the calendar window.
    """
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT game_date FROM raw.pitches WHERE season = %s ORDER BY game_date",
                (season,),
            )
            days = [r[0] for r in cur.fetchall()]
        if days:
            return days
        log.warning("season %d: no games in raw.pitches; falling back to the calendar.", season)
    first = dt.date(season, *SEASON_FIRST_MONTH_DAY)
    last = min(dt.date(season, *SEASON_LAST_MONTH_DAY), dt.date.today())
    if last < first:
        return []
    return [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]


def loaded_days(conn, season: int) -> set[dt.date]:
    """Return the days of ``season`` that already have tracking rows."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT game_date FROM raw.savant_pitch_tracking WHERE season = %s",
            (season,),
        )
        return {r[0] for r in cur.fetchall()}


def main(argv: list[str] | None = None) -> int:
    """Load the days the command line names, with a commit every ten days."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="Load per-pitch Savant tracking (SIM-534).")
    ap.add_argument("--seasons", nargs="+", type=int)
    ap.add_argument("--start", type=dt.date.fromisoformat)
    ap.add_argument("--end", type=dt.date.fromisoformat)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--resume",
        action="store_true",
        help="skip days that already have rows (a day with no games stays empty and is retried)",
    )
    args = ap.parse_args(argv)

    dsn = None if args.dry_run else require_dsn()
    conn = None if dsn is None else psycopg2.connect(dsn)

    if args.start and args.end:
        days = [args.start + dt.timedelta(days=i) for i in range((args.end - args.start).days + 1)]
    elif args.seasons:
        days = [d for s in args.seasons for d in season_days(s, conn)]
    else:
        raise SystemExit("give --seasons or --start/--end")

    total = 0
    skipped = 0
    try:
        done: set[dt.date] = set()
        if args.resume and conn is not None:
            for season in {d.year for d in days}:
                done |= loaded_days(conn, season)
        for i, day in enumerate(days, 1):
            if day in done:
                skipped += 1
                continue
            n = load_day(conn, day, dry_run=args.dry_run)
            total += n
            if conn is not None and i % 10 == 0:
                conn.commit()
            if n:
                log.info("%s: %d pitches (%d/%d days, %d rows so far)", day, n, i, len(days), total)
            time.sleep(PAUSE_BETWEEN_FETCHES_S)
        if conn is not None:
            conn.commit()
    except Exception:
        if conn is not None:
            conn.commit()  # keep the days already fetched; --resume picks up the rest
        raise
    finally:
        if conn is not None:
            conn.close()

    log.info("pitch tracking load complete: %d rows, %d days skipped.", total, skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
