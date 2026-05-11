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
import time
from collections.abc import Iterator
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psycopg2
from psycopg2.extras import execute_batch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sprint_speed_loader")

SAVANT_URL = "https://baseballsavant.mlb.com/leaderboard/sprint_speed"


# Savant returns empty strings for missing numerics; handle uniformly.
def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


class SprintSpeedLoader:
    """Fetch + upsert Sprint Speed data from Baseball Savant."""

    UPSERT_SQL = """
        INSERT INTO raw.sprint_speed (
            player_id, season, sprint_speed, competitive_runs,
            bolts, hp_to_1b, scraped_at
        ) VALUES (
            %(player_id)s, %(season)s, %(sprint_speed)s, %(competitive_runs)s,
            %(bolts)s, %(hp_to_1b)s, NOW()
        )
        ON CONFLICT (player_id, season) DO UPDATE SET
            sprint_speed     = EXCLUDED.sprint_speed,
            competitive_runs = EXCLUDED.competitive_runs,
            bolts            = EXCLUDED.bolts,
            hp_to_1b         = EXCLUDED.hp_to_1b,
            scraped_at       = EXCLUDED.scraped_at;
    """

    def __init__(self, dsn: str, batch_size: int = 200) -> None:
        self._dsn = dsn
        self._batch_size = batch_size

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
        valid_rows = self._filter_to_known_players(conn, rows)
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
            execute_batch(cur, self.UPSERT_SQL, valid_rows, page_size=self._batch_size)

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
        Savant's variable default threshold.
        """
        params = {
            "year": str(season),
            "position": "",
            "team": "",
            "min": "0",
            "csv": "true",
        }
        url = f"{SAVANT_URL}?{urlencode(params)}"

        # Savant blocks the default urllib User-Agent.
        req = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (baseball-sim-etl)",
                "Accept": "text/csv,application/octet-stream,*/*",
            },
        )

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                with urlopen(req, timeout=30) as resp:
                    data = resp.read().decode("utf-8-sig")  # strip BOM if present
                    if not data.strip():
                        raise ValueError("Empty response from Savant")
                    return data
            except (HTTPError, URLError, ValueError) as e:
                if attempt == max_retries:
                    log.error("Savant fetch failed for season %d: %s", season, e)
                    raise
                wait = 2**attempt
                log.warning(
                    "Savant fetch attempt %d/%d failed (%s). Retrying in %ds …",
                    attempt,
                    max_retries,
                    e,
                    wait,
                )
                time.sleep(wait)

        raise RuntimeError("unreachable")

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
            player_id = _to_int(raw.get("player_id"))
            sprint_speed = _to_float(raw.get("sprint_speed"))
            comp_runs = _to_int(raw.get("competitive_runs"))

            # Hard skip: no player_id or no measurement at all.
            if player_id is None or sprint_speed is None or comp_runs is None:
                continue

            yield {
                "player_id": player_id,
                "season": season,
                "sprint_speed": sprint_speed,
                "competitive_runs": comp_runs,
                "bolts": _to_int(raw.get("bolts")),
                "hp_to_1b": _to_float(raw.get("hp_to_1b")),
            }

    # ------------------------------------------------------------------
    # FK guard
    # ------------------------------------------------------------------

    def _filter_to_known_players(
        self,
        conn: psycopg2.extensions.connection,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Drop rows whose player_id is not in raw.players (FK protection)."""
        ids = list({r["player_id"] for r in rows})
        if not ids:
            return []

        with conn.cursor() as cur:
            cur.execute(
                "SELECT player_id FROM raw.players WHERE player_id = ANY(%s)",
                (ids,),
            )
            known = {r[0] for r in cur.fetchall()}

        return [r for r in rows if r["player_id"] in known]


if __name__ == "__main__":
    loader = SprintSpeedLoader(
        "postgresql://localhost/baseball_simulator?user=postgres&password=baseball"
    )
    results = loader.refresh_seasons([2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025])
    for season, n in sorted(results.items()):
        log.info("  season %d → %d rows", season, n)
