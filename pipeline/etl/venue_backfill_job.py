"""
venue_backfill_job.py
=====================
SIM-086 — Backfill NULL venue_id rows on raw.games.

Why this job exists
-------------------
The live ingestion pipeline (live_ingestion_pipeline.py::_upsert_game_record)
inserts ``raw.games`` rows from the schedule API as soon as a game appears on
the slate.  For some games the schedule response does not yet include a venue
(early international/spring training schedule entries), so the column is
written as NULL (SIM-086 made it nullable; pre-SIM-086 the row was silently
dropped because of an FK violation against raw.venues).

Once the MLB API publishes the venue assignment, this job picks the row up
and fills it.

What it does
------------
1. SELECT game_pk, game_date FROM raw.games WHERE venue_id IS NULL.
2. For each game, GET ``/api/v1/schedule?gamePk=<id>&hydrate=venue``.
3. If a venue id is present in the response, UPDATE the row.
4. Logs a raw.pipeline_run_log entry with games_processed and a per-game
   summary in metadata.

Idempotent: running it twice in a row is a no-op for any game already filled.

Usage
-----
    # Standalone:
    python -m pipeline.etl.venue_backfill_job [--dsn DSN] [--dry-run]

    # APScheduler (default cadence: every 6 hours):
    from pipeline.etl.venue_backfill_job import schedule_venue_backfill_job
    schedule_venue_backfill_job(dsn=dsn, scheduler=scheduler)

    # Cron (system-level, every 6 hours):
    0 */6 * * * cd /path/to/project && python -m pipeline.etl.venue_backfill_job
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date
from typing import Any

import aiohttp
import asyncpg

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("venue_backfill_job")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
JOB_NAME         = "venue_backfill_job"
DEFAULT_INTERVAL_HOURS = 6


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------

class VenueBackfillJob:
    """
    Idempotent backfill of NULL venue_id rows on raw.games.
    """

    def __init__(
        self,
        dsn: str,
        dry_run: bool = False,
        http_timeout: float = 10.0,
    ) -> None:
        self._dsn          = dsn
        self._dry_run      = dry_run
        self._http_timeout = http_timeout
        self._db: asyncpg.Pool | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        self._db = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)

    async def _close(self) -> None:
        if self._db:
            await self._db.close()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self) -> dict[str, int]:
        """
        Execute the backfill pass.

        Returns a summary dict logged to raw.pipeline_run_log.
        """
        await self._connect()
        summary = {
            "games_processed": 0,
            "venues_filled":   0,
            "still_unknown":   0,
            "http_errors":     0,
        }
        run_id: int | None = None
        try:
            run_id = await self._start_log_row()
            rows = await self._fetch_null_venue_games()
            log.info("Found %d games with NULL venue_id.", len(rows))

            if not rows:
                return summary

            async with aiohttp.ClientSession() as session:
                for r in rows:
                    summary["games_processed"] += 1
                    game_pk = r["game_pk"]
                    game_date = r["game_date"]
                    try:
                        venue_id = await self._fetch_venue_id(
                            session, game_pk=game_pk, game_date=game_date
                        )
                    except Exception as exc:
                        summary["http_errors"] += 1
                        log.warning("game_pk=%s: HTTP error fetching venue: %s",
                                    game_pk, exc)
                        continue

                    if venue_id is None:
                        summary["still_unknown"] += 1
                        log.debug("game_pk=%s: venue still not assigned", game_pk)
                        continue

                    if self._dry_run:
                        log.info("[dry-run] would fill game_pk=%s with venue_id=%s",
                                 game_pk, venue_id)
                        summary["venues_filled"] += 1
                        continue

                    await self._update_venue(game_pk, venue_id)
                    summary["venues_filled"] += 1
                    log.info("game_pk=%s: backfilled venue_id=%s", game_pk, venue_id)

            return summary

        finally:
            if run_id is not None:
                await self._finish_log_row(run_id, summary)
            await self._close()

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _fetch_null_venue_games(self) -> list[asyncpg.Record]:
        assert self._db is not None
        return await self._db.fetch(
            """
            SELECT game_pk, game_date
            FROM   raw.games
            WHERE  venue_id IS NULL
            ORDER  BY game_date DESC
            """,
        )

    async def _update_venue(self, game_pk: int, venue_id: int) -> None:
        """
        Backfill the venue.  No-op if the venue row doesn't exist in raw.venues
        (the FK would fail) — caller should ensure raw.venues is loaded first.
        """
        assert self._db is not None
        # Pre-check FK target so we surface a useful warning instead of
        # raising the asyncpg ForeignKeyViolationError up the stack.
        venue_exists = await self._db.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM raw.venues v
                WHERE v.venue_id = $1
            )
            """,
            venue_id,
        )
        if not venue_exists:
            log.warning(
                "game_pk=%s: venue_id=%s not present in raw.venues — skipping update. "
                "Seed raw.venues then re-run.",
                game_pk, venue_id,
            )
            return

        await self._db.execute(
            """
            UPDATE raw.games
            SET    venue_id = $2,
                   updated_at = NOW()
            WHERE  game_pk  = $1
              AND  venue_id IS NULL
            """,
            game_pk, venue_id,
        )

    async def _start_log_row(self) -> int | None:
        if self._dry_run or self._db is None:
            return None
        row = await self._db.fetchrow(
            """
            INSERT INTO raw.pipeline_run_log (job_name, status)
            VALUES ($1, 'running')
            RETURNING id
            """,
            JOB_NAME,
        )
        return row["id"] if row else None

    async def _finish_log_row(self, run_id: int, summary: dict[str, int]) -> None:
        if self._dry_run or self._db is None:
            return
        status = "success" if summary["http_errors"] == 0 else "partial"
        await self._db.execute(
            """
            UPDATE raw.pipeline_run_log
            SET    finished_at     = NOW(),
                   status          = $2,
                   games_processed = $3,
                   metadata        = $4::jsonb
            WHERE  id = $1
            """,
            run_id,
            status,
            summary["games_processed"],
            json.dumps(summary),
        )

    # ------------------------------------------------------------------
    # MLB API
    # ------------------------------------------------------------------

    async def _fetch_venue_id(
        self,
        session: aiohttp.ClientSession,
        *,
        game_pk: int,
        game_date: date,
    ) -> int | None:
        params = {
            "gamePk":   str(game_pk),
            "sportId":  "1",
            "date":     game_date.isoformat(),
            "hydrate":  "venue",
        }
        timeout = aiohttp.ClientTimeout(total=self._http_timeout)
        async with session.get(MLB_SCHEDULE_URL, params=params, timeout=timeout) as resp:
            resp.raise_for_status()
            payload = await resp.json()

        for date_block in payload.get("dates", []):
            for game in date_block.get("games", []):
                if int(game.get("gamePk", 0)) != game_pk:
                    continue
                venue = game.get("venue") or {}
                vid = venue.get("id")
                if vid:
                    return int(vid)
        return None


# ---------------------------------------------------------------------------
# Scheduler integration
# ---------------------------------------------------------------------------

def schedule_venue_backfill_job(
    *,
    dsn: str,
    scheduler: Any,
    interval_hours: int = DEFAULT_INTERVAL_HOURS,
) -> None:
    """
    Register the backfill job with an APScheduler instance.

    Cadence default of every 6 hours catches venue assignments within a
    quarter of a day of the MLB API publishing them, with negligible API load.
    """
    async def _runner() -> None:
        job = VenueBackfillJob(dsn=dsn)
        await job.run()

    scheduler.add_job(
        _runner,
        "interval",
        hours=interval_hours,
        id=JOB_NAME,
        replace_existing=True,
    )
    log.info("Registered %s with %dh cadence", JOB_NAME, interval_hours)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill NULL venue_id rows on raw.games")
    p.add_argument("--dsn", default=os.environ.get("BASEBALL_DB_DSN"),
                   help="asyncpg DSN; defaults to $BASEBALL_DB_DSN")
    p.add_argument("--dry-run", action="store_true",
                   help="Print would-be UPDATEs without writing")
    return p.parse_args()


async def _main() -> int:
    args = _parse_args()
    if not args.dsn:
        log.error("No DSN provided. Set BASEBALL_DB_DSN or pass --dsn.")
        return 2

    job = VenueBackfillJob(dsn=args.dsn, dry_run=args.dry_run)
    summary = await job.run()
    log.info("Done. summary=%s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
