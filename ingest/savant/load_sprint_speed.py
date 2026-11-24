"""
etl_sprint_speed_loader.py
==========================
MLB Baseball Simulation Platform

Fetches Baseball Savant's Sprint Speed leaderboard CSV for a given season
and upserts it into raw.sprint_speed.  One row per player per season.

Savant CSV columns (as of 2024 leaderboard format):
    "last_name, first_name"      — combined, comma-separated (quoted field)
    player_id                    — MLBAM ID, matches raw.players.player_id
    team_id                      — MLB team ID
    team                         — team abbreviation
    age                          — player age
    competitive_runs             — Savant's sample count (confidence signal)
    bolts                        — runs with sprint_speed >= 30 ft/s
    sprint_speed                 — ft/s, Savant's top-2-run average
    hp_to_1b                     — home-to-first avg (seconds)
    percent_rank_sprint_speed    — percentile rank (ignored)

Usage
-----
    loader = SprintSpeedLoader(dsn="postgresql://user:pass@localhost/baseball")
    loader.refresh_seasons([2023, 2024, 2025])

    # Or via CLI
    python etl_sprint_speed_loader.py --seasons 2023 2024 2025

Run cadence
-----------
    - Nightly during the season (Savant updates daily)
    - Once after the final regular-season day for frozen year-end numbers
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

import psycopg2
from psycopg2.extras import execute_batch

from db.dsn import require_dsn
from ingest.games.coercion import to_float, to_int
from ingest.savant.load_leaderboards import fetch_csv, filter_to_known_players, upsert_sql_for

log = logging.getLogger(__name__)

SAVANT_URL = "https://baseballsavant.mlb.com/leaderboard/sprint_speed"

#: The columns of one ``raw.sprint_speed`` row, the key first.
_COLUMNS = ("player_id", "season", "sprint_speed", "competitive_runs", "bolts", "hp_to_1b")


class SprintSpeedLoader:
    """Fetch + upsert Sprint Speed data from Baseball Savant."""

    UPSERT_SQL = upsert_sql_for("raw.sprint_speed", _COLUMNS, ("player_id", "season"))

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh_seasons(self, seasons: list[int]) -> dict[int, int]:
        """
        Fetch each season's CSV and upsert into raw.sprint_speed.

        Returns
        -------
        dict mapping season → rows upserted.
        """
        results: dict[int, int] = {}
        conn = psycopg2.connect(self._dsn)
        try:
            for season in seasons:
                n = self._refresh_one_season(conn, season)
                results[season] = n
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return results

    # ------------------------------------------------------------------
    # Per-season pipeline
    # ------------------------------------------------------------------

    def _refresh_one_season(
        self,
        conn: psycopg2.extensions.connection,
        season: int,
    ) -> int:
        log.info("Fetching Savant sprint_speed CSV for season %d …", season)
        csv_text = self._fetch_csv(season)
        rows = list(self._parse_csv(csv_text, season))

        if not rows:
            log.warning("Season %d returned zero rows — skipping.", season)
            return 0

        # FK guard — drop rows whose player_id is not yet in raw.players.
        # These typically appear when Savant publishes a prospect before
        # the ETL has seen them in a pitch feed.  Safer to skip and log
        # than to fail the whole batch.
        valid_rows = filter_to_known_players(conn, rows)
        n_skipped = len(rows) - len(valid_rows)
        if n_skipped > 0:
            log.warning(
                "Season %d: %d rows skipped (player_id not in raw.players).",
                season,
                n_skipped,
            )

        if not valid_rows:
            return 0

        with conn.cursor() as cur:
            execute_batch(cur, self.UPSERT_SQL, valid_rows, page_size=200)

        log.info("Season %d: upserted %d sprint_speed rows.", season, len(valid_rows))
        return len(valid_rows)

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def _fetch_csv(self, season: int) -> str:
        """
        Hit Savant's sprint_speed endpoint with CSV output.

        min=0 pulls every player who has any competitive runs; downstream
        consumers filter by competitive_runs >= 10 rather than relying on
        Savant's variable default threshold. The leaderboard loader's
        ``fetch_csv`` does the GET; an empty body raises ``ValueError``.
        """
        params = {
            "year": str(season),
            "position": "",
            "team": "",
            "min": "0",
            "csv": "true",
        }
        data = fetch_csv(f"{SAVANT_URL}?{urlencode(params)}")
        if not data.strip():
            raise ValueError("Empty response from Savant")
        return data

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------

    def _parse_csv(self, csv_text: str, season: int) -> Iterator[dict[str, Any]]:
        """
        Yield one upsert-ready dict per row.

        Savant's CSV puts "last_name, first_name" in a single quoted field.
        Python's csv module handles that correctly out of the box. We don't
        actually need the name columns here (player_id is the key) so we
        ignore them.
        """
        reader = csv.DictReader(io.StringIO(csv_text))
        required = {"player_id", "sprint_speed", "competitive_runs"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"Savant CSV for {season} missing columns: {sorted(missing)}. "
                f"Got: {reader.fieldnames}"
            )

        for raw in reader:
            player_id = to_int(raw.get("player_id"))
            sprint_speed = to_float(raw.get("sprint_speed"))
            comp_runs = to_int(raw.get("competitive_runs"))

            # Hard skip: no player_id or no measurement at all.
            if player_id is None or sprint_speed is None or comp_runs is None:
                continue

            yield {
                "player_id": player_id,
                "season": season,
                "sprint_speed": sprint_speed,
                "competitive_runs": comp_runs,
                "bolts": to_int(raw.get("bolts")),
                "hp_to_1b": to_float(raw.get("hp_to_1b")),
            }


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(
        description="Load Baseball Savant sprint speed data into raw.sprint_speed."
    )
    parser.add_argument(
        "--seasons",
        type=int,
        nargs="+",
        required=True,
        help="One or more seasons to refresh, e.g. --seasons 2023 2024 2025",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="Postgres DSN. Defaults to the BASEBALL_DB_DSN environment variable.",
    )
    args = parser.parse_args()

    loader = SprintSpeedLoader(args.dsn or require_dsn())
    results = loader.refresh_seasons(args.seasons)
    for season, n in sorted(results.items()):
        log.info("  season %d → %d rows", season, n)
