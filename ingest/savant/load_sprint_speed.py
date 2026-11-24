"""Load Savant's sprint speed leaderboard into ``raw.sprint_speed``, one row per player-season.

The loader keeps these CSV columns and ignores the name, team, age and rank:

    player_id          the MLBAM id, as in raw.players.player_id
    sprint_speed       feet per second, Savant's average of the runner's top runs
    competitive_runs   the number of runs in Savant's sample
    bolts              the runs at 30 feet per second or faster
    hp_to_1b           the average time from home to first, in seconds

Savant updates the board daily in season. A pull after the last regular-season
day gives the final numbers.

USAGE
-----
    python -m ingest.savant.load_sprint_speed --seasons 2023 2024 2025
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
    """Fetch Savant's sprint speeds and upsert them into ``raw.sprint_speed``."""

    UPSERT_SQL = upsert_sql_for("raw.sprint_speed", _COLUMNS, ("player_id", "season"))

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh_seasons(self, seasons: list[int]) -> dict[int, int]:
        """Load each season in one transaction. Return the rows upserted per season."""
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
        """Fetch and upsert one season. Return the number of rows upserted."""
        log.info("Fetching Savant sprint_speed CSV for season %d …", season)
        csv_text = self._fetch_csv(season)
        rows = list(self._parse_csv(csv_text, season))

        if not rows:
            log.warning("Season %d returned zero rows — skipping.", season)
            return 0

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
        """Return one season's CSV. Raise ``ValueError`` on an empty body.

        ``min=0`` asks for every runner; Savant still applies its own floor of
        5 runs. The table keeps ``competitive_runs``, so a reader can set its
        own minimum.
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
        """Yield one upsert payload per row with a player id, a speed and a run count.

        Raise ``ValueError`` when the CSV lacks one of those columns.
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
