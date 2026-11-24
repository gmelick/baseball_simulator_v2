"""Load the Baseball Savant leaderboards of ``ingest.savant.boards`` into their raw tables.

For each board and season, the loader fetches the CSV and coerces it. It drops
the rows of players the database has never seen, and upserts the rest.

THE SEASON GUARDS
-----------------
Savant answers an unknown season parameter with HTTP 200 and the CURRENT
season's rows. With no guard, a typo writes the current season's numbers into a
2023 row, and nothing downstream notices. Two guards stop that:

  1. **The probe.** Once per board per run, the loader asks for
     ``PROBE_SEASON``, a year with no Statcast data. An honoured parameter
     returns no rows. Any row raises ``SeasonParamError``.
  2. **The per-row check.** On a board with a season column, every row must
     carry the season the loader asked for, or the load raises.

USAGE
-----
    python -m ingest.savant.load_leaderboards --boards all --seasons 2023 2024 2025 2026
    python -m ingest.savant.load_leaderboards --boards batter --seasons 2024
    python -m ingest.savant.load_leaderboards --boards bat_tracking swing_path --seasons 2024

``--boards`` takes board names or the groups ``batter``, ``fielding`` and
``running``. ``--dry-run`` fetches and reports row counts without writing.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
import time
from collections.abc import Sequence
from datetime import date
from urllib.parse import urlencode

import psycopg2
from psycopg2.extras import execute_batch

from db.dsn import require_dsn
from ingest.games.coercion import to_float, to_int, to_str
from ingest.mlb import client as mlb_client
from ingest.mlb.client import RetryPolicy
from ingest.savant.boards import (
    BATTER_BOARDS,
    BOARDS,
    FIELDING_BOARDS,
    PROBE_SEASON,
    RUNNING_BOARDS,
    SavantBoard,
)

log = logging.getLogger(__name__)

#: Savant blocks the default urllib user agent.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (baseball-sim-etl)",
    "Accept": "text/csv,application/octet-stream,*/*",
}
#: A full-roster season pull takes one to three minutes.
FETCH_TIMEOUT_S = 180
#: Two retries of a transient failure, the first after two seconds, the second after four.
FETCH_POLICY = RetryPolicy(retries=2, first_wait_s=2.0)
#: The pause between two fetches, so a whole run does not look like a hammering client.
PAUSE_BETWEEN_FETCHES_S = 1.5

#: The target columns that hold a label, not a number.
_TEXT_TARGETS = frozenset({"bat_side", "split", "position", "primary_position"})
#: The target columns that hold whole numbers.
_INT_TARGETS = frozenset(
    {
        "competitive_swings",
        "total_throws",
        "n_opp_xb",
        "n_att_xb",
        "n_out",
        "n_safe",
        "pop_time_2b_count",
        "sb_attempts",
        "n_cs",
        "n_plays",
        "n_outs",
        "n_scoop",
        "outs_scoop",
        "height_in_inches",
        # The running-game boards' counts.
        "n_init",
        "n_sb",
        "n_pk",
        "n_bk",
        # The outs-above-average board's counts are whole outs, and so is the
        # outfield jump's ``outs_above_average``. Only these two boards use that
        # target: first base receiving stores ``total_oaa``, a float.
        "outs_above_average",
        "fielding_runs_prevented",
        "oaa_in_front",
        "oaa_toward_3b_line",
        "oaa_toward_1b_line",
        "oaa_behind",
        "oaa_vs_rhh",
        "oaa_vs_lhh",
    }
)


class SeasonParamError(RuntimeError):
    """The board ignored the season parameter, so its rows are the wrong year."""


class SavantFetchError(RuntimeError):
    """The board returned something that is not CSV."""


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def build_url(
    board: SavantBoard, season: int, split_value: str = "", asof: date | None = None
) -> str:
    """Return the URL of one pull of ``board`` for ``season``.

    ``split_value`` is one value of the board's split pull: the pitcher's hand
    on the swing boards, Savant's position code on the outs-above-average board.
    The board names the parameter (``board.split_param``). An empty value sends
    nothing. ``asof`` pulls the season up to that date. It raises ``ValueError``
    on a board with no date control.
    """
    params: dict[str, str] = {}
    params.update(board.extra)
    params.update(board.season_params(season))
    if split_value:
        params[board.split_param] = split_value
    if asof is not None:
        # The window starts on 1 January, so it holds the season up to and
        # including the cutoff: what a simulator on that date would have seen.
        if not board.supports_date_range:
            raise ValueError(f"{board.name} has no date control; it cannot be pulled as of a date")
        params["dateStart"] = f"{season}-01-01"
        params["dateEnd"] = asof.isoformat()
    params["csv"] = "true"
    return f"{board.url}?{urlencode(params)}"


def fetch_csv(url: str) -> str:
    """GET ``url`` and return its body without a leading byte-order mark.

    The MLB client does the GET and retries a transient failure by
    ``FETCH_POLICY``. Any other failure, and the last retry's, raises the
    original error. The mark must go: a kept mark sticks to a quoted first
    header, and it hides an HTML page from the guard in ``parse_rows``.
    """
    resp = mlb_client.get(
        url,
        headers=HEADERS,
        timeout_s=FETCH_TIMEOUT_S,
        policy=FETCH_POLICY,
        logger=log,
        label="Savant",
    )
    return resp.text.removeprefix("﻿")


def parse_rows(body: str) -> list[dict[str, str]]:
    """Parse a CSV body into rows. Raise ``SavantFetchError`` on an HTML page.

    The guard checks the payload, not the transport, so it lives here and not in
    ``fetch_csv``. Several Savant boards answer a parameter they reject with the
    rendered page and HTTP 200. Parsed as CSV, that page gives no rows and no
    error, which reads as an empty season.
    """
    if body.lstrip()[:1] == "<":
        raise SavantFetchError("HTML, not CSV — the board rejected a parameter")
    return list(csv.DictReader(io.StringIO(body)))


# ---------------------------------------------------------------------------
# The season guards
# ---------------------------------------------------------------------------


def probe_season_param(board: SavantBoard, fetcher=fetch_csv) -> None:
    """Raise ``SeasonParamError`` unless the board honours its season parameter.

    The probe asks for a season with no Statcast data. An honoured parameter
    returns no rows. Any row means the board ignores the parameter, and every
    pull would carry the current season.
    """
    body = fetcher(build_url(board, PROBE_SEASON))
    rows = parse_rows(body)
    if rows:
        raise SeasonParamError(
            f"{board.name}: asked for {PROBE_SEASON} and got {len(rows)} rows. "
            f"The season parameter (style {board.season_style!r}) is being ignored, so "
            f"every pull would write the current season under the wrong year."
        )
    log.info("%s: season parameter honoured (style %s).", board.name, board.season_style)


def check_row_seasons(board: SavantBoard, rows: list[dict[str, str]], season: int) -> None:
    """Raise ``SeasonParamError`` when a row's season is not ``season``.

    A board with no season column passes, and so does a row with a blank season.
    """
    if not board.season_column:
        return
    bad = {
        str(r.get(board.season_column))
        for r in rows
        if to_int(r.get(board.season_column)) not in (None, season)
    }
    if bad:
        raise SeasonParamError(f"{board.name}: asked for {season}, rows carry {sorted(bad)!r}.")


# ---------------------------------------------------------------------------
# Coerce
# ---------------------------------------------------------------------------


def coerce_row(
    board: SavantBoard,
    row: dict[str, str],
    season: int,
    split: str | None,
    asof: date | None = None,
) -> dict[str, object] | None:
    """Turn one CSV row into one upsert payload. Return None when it has no player id."""
    player_id = to_int(row.get(board.player_column))
    if player_id is None:
        return None
    out: dict[str, object] = {"player_id": player_id, "season": season}
    if board.asof_column:
        # A full pull of a completed season is data as of its last day. A full
        # pull of the season in progress is data as of TODAY. A 31 December
        # stamp would date it in the future. The profile builder admits only
        # rows dated at or before its cutoff, so every live build would drop
        # the current season silently.
        out[board.asof_column] = (
            asof if asof is not None else min(date(season, 12, 31), date.today())
        )
    if board.split_column:
        # A split board takes its label from the query: the pitcher's hand on
        # the swing boards, the PULLED position on the outs-above-average board
        # (its CSV names only the primary position). The stance board carries
        # its label in the CSV.
        out[board.split_column] = (
            split if split is not None else to_str(row.get(board.split_column))
        )
    for src, target in board.columns:
        raw = row.get(src)
        if target in _TEXT_TARGETS:
            out[target] = to_str(raw)
        elif target in _INT_TARGETS:
            out[target] = to_int(raw)
        else:
            out[target] = to_float(raw)
    return out


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def upsert_sql_for(table: str, columns: Sequence[str], key: Sequence[str]) -> str:
    """Return the upsert of ``columns`` into ``table``, keyed on ``key``.

    Each row takes ``NOW()`` as its ``scraped_at``. On a key conflict the
    statement overwrites every column outside the key, and ``scraped_at``.
    The Savant boards, the per-pitch tracking and the sprint speeds use it.
    """
    updates = [c for c in columns if c not in key]
    placeholders = ", ".join(f"%({c})s" for c in columns)
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in [*updates, "scraped_at"])
    return (
        f"INSERT INTO {table} ({', '.join(columns)}, scraped_at) "
        f"VALUES ({placeholders}, NOW()) "
        f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {set_clause};"
    )


def upsert_sql(board: SavantBoard) -> str:
    """Return one board's upsert, keyed on the player, the season, the split and the cutoff."""
    key = ["player_id", "season"]
    if board.split_column:
        key.append(board.split_column)
    if board.asof_column:
        key.append(board.asof_column)
    return upsert_sql_for(board.table, board.target_columns, key)


def filter_to_known_players(conn, rows: list[dict]) -> list[dict]:
    """Return the rows whose player is in ``raw.players``.

    Savant can publish a player before our pitch feed has seen him, and the raw
    tables carry a foreign key to ``raw.players``. A dropped row beats a failed
    batch; the caller logs the count.
    """
    if not rows:
        return []
    ids = sorted({int(r["player_id"]) for r in rows})
    with conn.cursor() as cur:
        cur.execute("SELECT player_id FROM raw.players WHERE player_id = ANY(%s)", (ids,))
        known = {r[0] for r in cur.fetchall()}
    return [r for r in rows if int(r["player_id"]) in known]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def load_board_season(
    conn,
    board: SavantBoard,
    season: int,
    *,
    dry_run: bool = False,
    fetcher=fetch_csv,
    asof: date | None = None,
) -> int:
    """Fetch and upsert one board for one season. Return the number of rows written.

    A split board takes one pull per (label, value) pair. A dry run writes
    nothing and returns the number of rows fetched.
    """
    pulls: list[tuple[str | None, str]] = (
        [(label, value) for label, value in board.splits] if board.splits else [(None, "")]
    )

    payload: list[dict] = []
    for label, value in pulls:
        body = fetcher(build_url(board, season, value, asof))
        rows = parse_rows(body)
        check_row_seasons(board, rows, season)
        log.info(
            "%s %d%s: %d rows returned.",
            board.name,
            season,
            f" [{label}]" if label else "",
            len(rows),
        )
        for row in rows:
            coerced = coerce_row(board, row, season, label, asof)
            if coerced is not None:
                payload.append(coerced)
        if len(pulls) > 1:
            time.sleep(PAUSE_BETWEEN_FETCHES_S)

    if not payload:
        log.warning("%s %d: no usable rows.", board.name, season)
        return 0

    if dry_run:
        log.info("%s %d: %d rows (dry run, nothing written).", board.name, season, len(payload))
        return len(payload)

    valid = filter_to_known_players(conn, payload)
    dropped = len(payload) - len(valid)
    if dropped:
        log.warning(
            "%s %d: %d rows dropped (player not in raw.players).", board.name, season, dropped
        )
    if not valid:
        return 0

    with conn.cursor() as cur:
        execute_batch(cur, upsert_sql(board), valid, page_size=200)
    log.info("%s %d: upserted %d rows.", board.name, season, len(valid))
    return len(valid)


def resolve_boards(names: list[str]) -> list[SavantBoard]:
    """Return the named boards in order, each once.

    ``all`` (or no name) is every board. ``batter``, ``fielding`` and
    ``running`` are the groups. An unknown name exits the program.
    """
    if not names or names == ["all"]:
        return list(BOARDS.values())
    picked: list[str] = []
    for n in names:
        if n == "batter":
            picked += list(BATTER_BOARDS)
        elif n == "fielding":
            picked += list(FIELDING_BOARDS)
        elif n == "running":
            picked += list(RUNNING_BOARDS)
        elif n in BOARDS:
            picked.append(n)
        else:
            raise SystemExit(f"unknown board {n!r}; known: {', '.join(sorted(BOARDS))}")
    seen: dict[str, None] = {}
    for n in picked:
        seen.setdefault(n, None)
    return [BOARDS[n] for n in seen]


def main(argv: list[str] | None = None) -> int:
    """Load the boards and seasons the command line names, in one transaction."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="Load Baseball Savant leaderboards (SIM-528).")
    ap.add_argument(
        "--boards",
        nargs="+",
        default=["all"],
        help="board names, or 'all' / 'batter' / 'fielding' / 'running'",
    )
    ap.add_argument("--seasons", nargs="+", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--asof",
        type=date.fromisoformat,
        help="point-in-time pull: keep only games up to this date (date-capable boards only)",
    )
    ap.add_argument("--skip-probe", action="store_true", help="skip the season-parameter probe")
    args = ap.parse_args(argv)

    boards = resolve_boards(args.boards)
    dsn = None if args.dry_run else require_dsn()
    conn = None if dsn is None else psycopg2.connect(dsn)
    total = 0
    try:
        for board in boards:
            if not args.skip_probe:
                probe_season_param(board)
                time.sleep(PAUSE_BETWEEN_FETCHES_S)
            for season in args.seasons:
                total += load_board_season(
                    conn, board, season, dry_run=args.dry_run, asof=args.asof
                )
                time.sleep(PAUSE_BETWEEN_FETCHES_S)
        if conn is not None:
            conn.commit()
    except Exception:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if conn is not None:
            conn.close()

    log.info("Savant load complete: %d rows across %d boards.", total, len(boards))
    return 0


if __name__ == "__main__":
    sys.exit(main())
