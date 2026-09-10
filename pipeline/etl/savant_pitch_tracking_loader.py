"""
savant_pitch_tracking_loader.py
===============================
SIM-534 — the per-PITCH tracking measurements, loaded one day at a time.

WHY PER PITCH AND NOT PER SEASON
--------------------------------
The bat-tracking and swing-path leaderboards publish season aggregates. A season
aggregate cannot be used to simulate a game inside that season: a bat speed
averaged over all of 2025 encodes how the batter swung in September. Savant's
pitch-level search export carries the same measurements on each individual
pitch, so aggregating them ourselves up to a cutoff date removes the problem at
the source rather than working around it.

It also removes a dependency. Once these columns are in the database, the batter
profile computes its physical features from our own tables, the same way it
already computes every other feature.

ONE DAY PER REQUEST — THIS IS NOT TUNING, IT IS CORRECTNESS
-----------------------------------------------------------
The export truncates at exactly 25,000 rows and says nothing about it. One day
of a full 2024 schedule is roughly 4,200 rows; one week is over the cap. A
week-at-a-time loader would silently drop most of the week. This loader requests
a single day and **fails** when a day comes back at the cap, because at that
point the data is incomplete and there is no way to tell which rows are missing.

Every returned row's date is checked against the day requested. The search
export honours its date parameters, but the leaderboards taught us not to
assume that (see SIM-528).

WHAT EXISTS WHEN (measured 2026-09-10)
--------------------------------------
The bat-tracking columns — bat speed, swing length, attack angle and direction,
swing path tilt, both intercepts — start in **2024**. A 2023 regular-season day
returns 3,120 pitches with ZERO of them, even though the bat-tracking
leaderboard publishes 2023 season values. So for 2023 the leaderboard is the
only source, and this loader is worth running on that season only for
``arm_angle`` and ``hyper_speed``, which do go back.

That is less of a problem than it sounds. Look-ahead is about the simulation
date, not the season: a 2023 full-season aggregate contains nothing that
postdates a 2025 cutoff, so it is legitimate for any cutoff after 2023 ended.
Only the season CONTAINING the cutoff has to be truncated, and every season from
2024 on can be.

USAGE
-----
    python -m pipeline.etl.savant_pitch_tracking_loader --seasons 2024
    python -m pipeline.etl.savant_pitch_tracking_loader --start 2024-04-01 --end 2024-04-30
    python -m pipeline.etl.savant_pitch_tracking_loader --seasons 2023 2024 --resume
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time
from urllib.parse import urlencode

import psycopg2
from psycopg2.extras import execute_batch

from pipeline.etl.coercion import to_float, to_int
from pipeline.etl.savant_loader import PAUSE_BETWEEN_FETCHES_S, fetch_csv, parse_rows

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("savant_pitch_tracking")

SEARCH_URL = "https://baseballsavant.mlb.com/statcast_search/csv"

#: The export's hard row cap. A day that hits it is incomplete, and the export
#: gives no indication of which rows were dropped.
ROW_CAP = 25_000

#: (source column in the export, target column). The join key is handled
#: separately.
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

#: The fallback day window, used only when the database has no games for the
#: season yet. Regular-season play never falls outside it.
SEASON_FIRST_MONTH_DAY = (3, 15)
SEASON_LAST_MONTH_DAY = (11, 15)


class TruncatedDayError(RuntimeError):
    """A single day hit the export's row cap, so its data is incomplete."""


def build_url(day: dt.date) -> str:
    params = {
        "all": "true",
        "hfSea": f"{day.year}|",
        "player_type": "batter",
        "game_date_gt": day.isoformat(),
        "game_date_lt": day.isoformat(),
        # Regular season only, matching the leaderboards' gameType=Regular. Left
        # off, a March request returns spring-training pitches that no pool row
        # will ever join to.
        "hfGT": "R|",
        "type": "details",
    }
    return f"{SEARCH_URL}?{urlencode(params)}"


def coerce_row(row: dict[str, str], day: dt.date) -> dict[str, object] | None:
    """One export row -> one payload, or None when it has no usable key."""
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
    """Refuse a truncated day, and refuse rows from another date."""
    if len(rows) >= ROW_CAP:
        raise TruncatedDayError(
            f"{day}: {len(rows)} rows, at or over the {ROW_CAP} cap. The export "
            f"drops the remainder silently, so this day is incomplete."
        )
    wrong = {str(r.get("game_date")) for r in rows if r.get("game_date") != day.isoformat()}
    if wrong:
        raise RuntimeError(f"{day}: rows carry other dates {sorted(wrong)[:5]!r}")


UPSERT_SQL = """
    INSERT INTO raw.savant_pitch_tracking ({cols}, scraped_at)
    VALUES ({vals}, NOW())
    ON CONFLICT (game_pk, at_bat_number, pitch_number) DO UPDATE SET {updates};
"""


def upsert_sql() -> str:
    cols = [*_KEY_COLUMNS, *_META_COLUMNS, *(t for _, t in TRACKING_COLUMNS)]
    updates = [c for c in cols if c not in _KEY_COLUMNS] + ["scraped_at"]
    return UPSERT_SQL.format(
        cols=", ".join(cols),
        vals=", ".join(f"%({c})s" for c in cols),
        updates=", ".join(f"{c} = EXCLUDED.{c}" for c in updates),
    )


def load_day(conn, day: dt.date, *, dry_run: bool = False, fetcher=fetch_csv) -> int:
    rows = parse_rows(fetcher(build_url(day)))
    if not rows:
        return 0
    check_day(rows, day)
    payload = [p for p in (coerce_row(r, day) for r in rows) if p is not None]
    if not payload:
        return 0
    if dry_run:
        return len(payload)
    with conn.cursor() as cur:
        execute_batch(cur, upsert_sql(), payload, page_size=500)
    return len(payload)


def season_days(season: int, conn=None, today: dt.date | None = None) -> list[dt.date]:
    """The days to request for ``season``.

    Prefers the dates our own database says had games — roughly 185 a season
    against 245 calendar days, and every skipped day is a request that would
    have returned nothing. Falls back to the calendar window when the database
    has no games for the season.
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
    today = today or dt.date.today()
    first = dt.date(season, *SEASON_FIRST_MONTH_DAY)
    last = min(dt.date(season, *SEASON_LAST_MONTH_DAY), today)
    if last < first:
        return []
    return [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]


def loaded_days(conn, season: int) -> set[dt.date]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT game_date FROM raw.savant_pitch_tracking WHERE season = %s",
            (season,),
        )
        return {r[0] for r in cur.fetchall()}


def main(argv: list[str] | None = None) -> int:
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

    dsn = os.environ.get("BASEBALL_DB_DSN")
    if not dsn and not args.dry_run:
        raise SystemExit("BASEBALL_DB_DSN is not set.")
    conn = None if args.dry_run else psycopg2.connect(dsn)

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
