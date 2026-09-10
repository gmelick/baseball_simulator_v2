"""
savant_loader.py
================
SIM-528 — one loader for every Baseball Savant leaderboard in
``savant_boards.BOARDS``.

WHAT IT DOES
------------
For each requested board and season: fetch the CSV, coerce it, drop rows whose
player the database has never seen, and upsert into the board's raw table.

WHAT IT REFUSES TO DO
---------------------
Write a season it cannot prove it asked for. Savant answers an unrecognised
season parameter with HTTP 200 and the CURRENT season's rows, so a typo would
silently write 2026 numbers into a 2023 row and nothing downstream would ever
notice. Two guards stand against that:

  1. **The probe.** Once per board per run, the loader asks for
     ``board.probe_season`` — a year with no possible Statcast data. An honoured
     parameter returns nothing. Anything else raises ``SeasonParamError``.
  2. **The per-row check.** Where the board returns a season column, every row
     must carry the season we asked for, or the load raises.

Both are cheap. Neither is optional.

USAGE
-----
    python -m pipeline.etl.savant_loader --boards all --seasons 2023 2024 2025 2026
    python -m pipeline.etl.savant_loader --boards batter --seasons 2024
    python -m pipeline.etl.savant_loader --boards bat_tracking swing_path --seasons 2024

``--dry-run`` fetches and reports row counts without writing.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psycopg2
from psycopg2.extras import execute_batch

from pipeline.etl.coercion import to_float, to_int, to_str
from pipeline.etl.savant_boards import BATTER_BOARDS, BOARDS, FIELDING_BOARDS, SavantBoard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("savant_loader")

#: Savant blocks the default urllib user agent outright.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (baseball-sim-etl)",
    "Accept": "text/csv,application/octet-stream,*/*",
}
FETCH_TIMEOUT_S = 180
MAX_RETRIES = 3
#: A full-roster season pull takes one to three minutes. Space them out so a
#: whole-run fetch does not read as a hammering client.
PAUSE_BETWEEN_FETCHES_S = 1.5

#: Columns whose target name says they hold a label, not a number.
_TEXT_TARGETS = frozenset({"bat_side", "split"})
#: Target columns that are whole numbers.
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
    }
)


class SeasonParamError(RuntimeError):
    """The board ignored the season parameter, so its rows are the wrong year."""


class SavantFetchError(RuntimeError):
    """The board could not be fetched, or returned something that is not CSV."""


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def build_url(board: SavantBoard, season: int, hand: str = "") -> str:
    params: dict[str, str] = {}
    params.update(board.extra)
    params.update(board.season_params(season))
    if hand:
        params["pitchHand"] = hand
    params["csv"] = "true"
    return f"{board.url}?{urlencode(params)}"


def fetch_csv(url: str) -> str:
    """GET ``url`` and return its body, retrying on transport failure."""
    req = Request(url, headers=HEADERS)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
                return resp.read().decode("utf-8-sig", errors="replace")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            if attempt == MAX_RETRIES:
                raise SavantFetchError(f"fetch failed for {url}: {exc}") from exc
            wait = 2**attempt
            log.warning(
                "fetch attempt %d/%d failed (%s); retrying in %ds", attempt, MAX_RETRIES, exc, wait
            )
            time.sleep(wait)
    raise SavantFetchError(f"fetch failed for {url}")  # pragma: no cover - loop always returns


def parse_rows(body: str) -> list[dict[str, str]]:
    """CSV body -> rows.

    The HTML guard lives here, not in ``fetch_csv``, because it is a fact about
    the payload rather than the transport. Several Savant boards answer a
    parameter they dislike with the rendered page and HTTP 200 — Park Factors
    and the Custom builder always do. Parsing that as CSV yields no rows and no
    error, which would read as "this season is empty".
    """
    if body.lstrip()[:1] == "<":
        raise SavantFetchError("HTML, not CSV — the board rejected a parameter")
    return list(csv.DictReader(io.StringIO(body)))


# ---------------------------------------------------------------------------
# The season guards
# ---------------------------------------------------------------------------


def probe_season_param(board: SavantBoard, fetcher=fetch_csv) -> None:
    """Prove the board honours its season parameter.

    Asks for a season with no possible Statcast data. An honoured parameter
    returns no rows. Anything else means the parameter was ignored and every
    later pull would silently carry the current season.
    """
    body = fetcher(build_url(board, board.probe_season))
    rows = parse_rows(body)
    if rows:
        raise SeasonParamError(
            f"{board.name}: asked for {board.probe_season} and got {len(rows)} rows. "
            f"The season parameter (style {board.season_style!r}) is being ignored, so "
            f"every pull would write the current season under the wrong year."
        )
    log.info("%s: season parameter honoured (style %s).", board.name, board.season_style)


def check_row_seasons(board: SavantBoard, rows: list[dict[str, str]], season: int) -> None:
    """Where the board returns a season column, every row must match."""
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
    board: SavantBoard, row: dict[str, str], season: int, split: str | None
) -> dict[str, object] | None:
    """One CSV row -> one upsert payload, or None when it has no player id."""
    player_id = to_int(row.get(board.player_column))
    if player_id is None:
        return None
    out: dict[str, object] = {"player_id": player_id, "season": season}
    if board.split_column:
        # A hand-split board carries its label from the query; the stance board
        # carries it in the CSV itself.
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


def upsert_sql(board: SavantBoard) -> str:
    cols = board.target_columns
    key = ["player_id", "season"] + ([board.split_column] if board.split_column else [])
    updates = [c for c in cols if c not in key]
    placeholders = ", ".join(f"%({c})s" for c in cols)
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in [*updates, "scraped_at"])
    return (
        f"INSERT INTO {board.table} ({', '.join(cols)}, scraped_at) "
        f"VALUES ({placeholders}, NOW()) "
        f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {set_clause};"
    )


def filter_to_known_players(conn, rows: list[dict]) -> list[dict]:
    """Drop rows whose player is not in ``raw.players``.

    Savant publishes a player before our pitch feed has seen him, and the raw
    tables carry a foreign key. Dropping and logging beats failing the batch.
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
) -> int:
    """Fetch and upsert one board for one season. Returns rows written."""
    pulls: list[tuple[str | None, str]] = (
        [(label, hand) for label, hand in board.hand_splits] if board.hand_splits else [(None, "")]
    )

    payload: list[dict] = []
    for label, hand in pulls:
        body = fetcher(build_url(board, season, hand))
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
            coerced = coerce_row(board, row, season, label)
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
    if not names or names == ["all"]:
        return list(BOARDS.values())
    picked: list[str] = []
    for n in names:
        if n == "batter":
            picked += list(BATTER_BOARDS)
        elif n == "fielding":
            picked += list(FIELDING_BOARDS)
        elif n in BOARDS:
            picked.append(n)
        else:
            raise SystemExit(f"unknown board {n!r}; known: {', '.join(sorted(BOARDS))}")
    seen: dict[str, None] = {}
    for n in picked:
        seen.setdefault(n, None)
    return [BOARDS[n] for n in seen]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Load Baseball Savant leaderboards (SIM-528).")
    ap.add_argument(
        "--boards",
        nargs="+",
        default=["all"],
        help="board names, or 'all' / 'batter' / 'fielding'",
    )
    ap.add_argument("--seasons", nargs="+", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-probe", action="store_true", help="skip the season-parameter probe")
    args = ap.parse_args(argv)

    boards = resolve_boards(args.boards)
    dsn = os.environ.get("BASEBALL_DB_DSN")
    if not dsn and not args.dry_run:
        raise SystemExit("BASEBALL_DB_DSN is not set.")

    conn = None if args.dry_run else psycopg2.connect(dsn)
    total = 0
    try:
        for board in boards:
            if not args.skip_probe:
                probe_season_param(board)
                time.sleep(PAUSE_BETWEEN_FETCHES_S)
            for season in args.seasons:
                total += load_board_season(conn, board, season, dry_run=args.dry_run)
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
