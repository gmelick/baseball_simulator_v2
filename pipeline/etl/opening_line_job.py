"""
opening_line_job.py
===================
SIM-138 — Nightly Opening Line Ingestion Job
MLB Baseball Simulation Platform

Scheduled to run at 08:00 ET every morning via cron / scheduler.

Why this job is time-critical
------------------------------
CLV (Closing Line Value) is defined as:  closing_line − bet_placement_line.
Opening lines are posted 5–7 days before game time by books.  No odds provider
offers historical odds retroactively.  Every day this job does not run means a
permanent, unrecoverable loss of opening line data.  This job MUST run daily.

What it does
------------
1. Queries the MLB schedule for all games in the next 7 days.
2. For each game_pk, checks raw.game_odds for an existing line_type='opening' row.
3. If none exists, fetches the current market line and stores it as line_type='opening'.
4. When a starting pitcher has been announced (status = 'Preview' and lineup posted),
   also stores player prop lines as line_type='opening' in raw.prop_odds.
5. Logs a raw.pipeline_run_log row with opening_line_games_captured count.

Acceptance gate
---------------
After 3 consecutive days running: raw.game_odds must contain line_type='opening'
rows for every game in the 7-day lookahead window.

Usage
-----
    # Standalone (for testing / manual backfill):
    python opening_line_job.py --dsn "postgresql://..." [--days 7] [--dry-run]

    # Via APScheduler (integrated with FastAPI lifespan):
    from opening_line_job import schedule_opening_line_job
    schedule_opening_line_job(dsn=dsn, scheduler=scheduler)

    # Cron (system-level):
    0 8 * * * cd /path/to/project && python -m pipeline.etl.opening_line_job
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import asyncpg

# MockOddsAPI lives in live_ingestion_pipeline; import it from there so mock
# logic stays in a single place.  In Phase 7, swap _fetch_current_odds() to
# call a real provider.
import sys
import pathlib

# Ensure project root is on sys.path when run as a standalone script.
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from pipeline.live.live_ingestion_pipeline import MockOddsAPI

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("opening_line_job")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
GAME_TYPES        = ["R", "F", "D", "L", "W", "P"]
LOOKAHEAD_DAYS    = 7   # how many days ahead to capture opening lines

# SIM-134: Prop stat lists aligned with the 7-value CHECK constraint on raw.prop_odds.
# Betting Analyst (Agent 8) confirmed scope — see CHANGES.md.
#
# Pitcher props captured when a starter is announced (starting pitcher known at schedule time).
# Batter props deferred: lineup position not reliably known 5–7 days out.
PITCHER_PROP_TYPES = ["strikeouts", "earned_runs", "walks"]
BATTER_PROP_TYPES  = ["hits", "home_runs", "total_bases", "rbis"]

# Legacy _MOCK_PROP_LINES and local _mock_prop_odds() removed in SIM-134.
# All mock prop generation is now delegated to MockOddsAPI.get_prop_odds()
# so the vig model, RNG seeding, and line centres live in a single place.
# Phase 7 swap: replace the MockOddsAPI call with a real provider in
# _capture_prop_opening_lines() just as _fetch_current_odds() is swapped for
# game-level markets.


# ---------------------------------------------------------------------------
# Main job class
# ---------------------------------------------------------------------------

class OpeningLineJob:
    """
    Nightly job that captures opening lines for all games in the 7-day lookahead.

    Designed to be idempotent: calling it multiple times on the same date does
    not duplicate rows (uses "already exists" checks rather than INSERT … ON CONFLICT
    to keep the logic explicit and auditable).

    Parameters
    ----------
    dsn
        asyncpg PostgreSQL DSN string.
    lookahead_days
        Number of days ahead to check for upcoming games.
    dry_run
        If True, performs all checks and logging but writes no rows.
    """

    def __init__(
        self,
        dsn: str,
        lookahead_days: int = LOOKAHEAD_DAYS,
        dry_run: bool = False,
    ) -> None:
        self._dsn           = dsn
        self._lookahead     = lookahead_days
        self._dry_run       = dry_run
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
        Execute the nightly opening line capture.

        Returns a summary dict logged to raw.pipeline_run_log:
          {
              "games_checked":              int,
              "opening_line_games_captured": int,
              "opening_prop_lines_captured": int,
              "games_already_had_opening":  int,
          }
        """
        await self._connect()
        run_id: int | None = None
        summary = {
            "games_checked":               0,
            "opening_line_games_captured": 0,
            "opening_prop_lines_captured": 0,
            "games_already_had_opening":   0,
        }

        try:
            run_id = await self._start_log_row()
            games = await self._fetch_upcoming_games()
            log.info(
                "Opening line job: found %d games in next %d days",
                len(games), self._lookahead,
            )

            for game in games:
                summary["games_checked"] += 1
                game_pk = game["game_pk"]
                already = await self._has_opening_line(game_pk)

                if already:
                    summary["games_already_had_opening"] += 1
                    log.debug("game %s: opening line already captured", game_pk)
                    continue

                # Capture game-level opening line
                odds = self._fetch_current_odds(game_pk)
                await self._store_opening_line(game_pk, odds)
                summary["opening_line_games_captured"] += 1
                log.info("game %s: opening line captured (mock=%s)", game_pk, odds["is_mock"])

                # Capture prop opening lines if starting pitcher announced
                if game.get("home_pitcher_id") or game.get("away_pitcher_id"):
                    prop_count = await self._capture_prop_opening_lines(
                        game_pk,
                        game.get("home_pitcher_id"),
                        game.get("away_pitcher_id"),
                    )
                    summary["opening_prop_lines_captured"] += prop_count

            await self._finish_log_row(run_id, "success", summary)
            log.info(
                "Opening line job complete: %s",
                {k: v for k, v in summary.items()},
            )

        except Exception as exc:
            log.error("Opening line job failed: %s", exc, exc_info=True)
            if run_id:
                await self._finish_log_row(run_id, "error", summary, str(exc))
            raise

        finally:
            await self._close()

        return summary

    # ------------------------------------------------------------------
    # Schedule API — fetch upcoming games
    # ------------------------------------------------------------------

    async def _fetch_upcoming_games(self) -> list[dict[str, Any]]:
        """
        Queries the MLB schedule API for all games from today through
        today + lookahead_days.  Returns a list of dicts with at minimum:
          game_pk, game_date, status, home_pitcher_id, away_pitcher_id
        """
        import aiohttp

        today     = date.today()
        end_date  = today + timedelta(days=self._lookahead)
        params    = {
            "sportId":    1,
            "gameTypes":  ",".join(GAME_TYPES),
            "startDate":  today.strftime("%Y-%m-%d"),
            "endDate":    end_date.strftime("%Y-%m-%d"),
            "hydrate":    "probablePitcher(note)",
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(MLB_SCHEDULE_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                data = await resp.json()

        games: list[dict[str, Any]] = []
        for date_entry in data.get("dates", []):
            for g in date_entry.get("games", []):
                if "rescheduleGameDate" in g or "resumeGameDate" in g:
                    continue
                game_pk = g["gamePk"]
                status  = g.get("status", {}).get("abstractGameState", "Preview")

                # Extract probable pitchers if announced
                home_pitcher_id = (
                    g.get("teams", {}).get("home", {})
                     .get("probablePitcher", {}).get("id")
                )
                away_pitcher_id = (
                    g.get("teams", {}).get("away", {})
                     .get("probablePitcher", {}).get("id")
                )

                games.append({
                    "game_pk":          game_pk,
                    "game_date":        date_entry["date"],
                    "status":           status,
                    "home_pitcher_id":  home_pitcher_id,
                    "away_pitcher_id":  away_pitcher_id,
                })

        return games

    # ------------------------------------------------------------------
    # Existence check
    # ------------------------------------------------------------------

    async def _has_opening_line(self, game_pk: int) -> bool:
        """Returns True if raw.game_odds already has a line_type='opening' row."""
        row = await self._db.fetchrow(
            "SELECT 1 FROM raw.game_odds WHERE game_pk = $1 AND line_type = 'opening' LIMIT 1",
            game_pk,
        )
        return row is not None

    # ------------------------------------------------------------------
    # Odds fetch  (mock — swap with real provider in Phase 7)
    # ------------------------------------------------------------------

    def _fetch_current_odds(self, game_pk: int) -> dict[str, Any]:
        """
        Returns odds dict for the given game.  In Phase 7, replace this with
        an async call to a real odds provider (The Odds API, Sportradar, etc.).
        """
        return MockOddsAPI.get_odds(
            game_pk,
            line_type="opening",
            market_type="moneyline",
            book="consensus",
            is_sharp_book=False,
        )

    # ------------------------------------------------------------------
    # Database writes
    # ------------------------------------------------------------------

    async def _store_opening_line(self, game_pk: int, odds: dict[str, Any]) -> None:
        """
        Inserts a single raw.game_odds row with line_type='opening'.
        Skips the write if dry_run=True.
        """
        if self._dry_run:
            log.info("[DRY RUN] Would insert opening line for game %s", game_pk)
            return

        await self._db.execute(
            """
            INSERT INTO raw.game_odds
                (game_pk, source, is_mock,
                 book, line_type, market_type, is_sharp_book,
                 home_ml, away_ml,
                 home_spread, home_spread_ml, away_spread, away_spread_ml,
                 total_line, over_ml, under_ml)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
            """,
            game_pk,
            odds.get("source", "mock"),
            odds.get("is_mock", True),
            odds.get("book", "consensus"),
            odds.get("line_type", "opening"),
            odds.get("market_type", "moneyline"),
            odds.get("is_sharp_book", False),
            odds.get("home_ml"),
            odds.get("away_ml"),
            odds.get("home_spread"),
            odds.get("home_spread_ml"),
            odds.get("away_spread"),
            odds.get("away_spread_ml"),
            odds.get("total_line"),
            odds.get("over_ml"),
            odds.get("under_ml"),
        )

    async def _capture_prop_opening_lines(
        self,
        game_pk: int,
        home_pitcher_id: int | None,
        away_pitcher_id: int | None,
    ) -> int:
        """
        SIM-134: Stores opening prop lines for announced starters.

        Covers:
          - Pitcher props: strikeouts, earned_runs, walks
            (captured when starter is announced — known 5–7 days out)
          - Batter props deferred: lineup order not reliably known this far
            in advance; captured intraday once lineup is posted.

        All mock generation is delegated to MockOddsAPI.get_prop_odds() so the
        vig model and RNG seeding stay in a single canonical place (SIM-134).

        Returns the number of prop rows inserted.
        """
        inserted = 0

        for pitcher_id in filter(None, [home_pitcher_id, away_pitcher_id]):
            # Idempotency check: skip if ANY opening prop already exists for
            # this pitcher+game (avoids partial re-inserts on job retries).
            existing = await self._db.fetchrow(
                """
                SELECT 1 FROM raw.prop_odds
                WHERE game_pk = $1 AND player_id = $2 AND line_type = 'opening'
                LIMIT 1
                """,
                game_pk, pitcher_id,
            )
            if existing:
                log.debug(
                    "Opening prop lines already exist for pitcher %s game %s — skipping",
                    pitcher_id, game_pk,
                )
                continue

            for prop_stat in PITCHER_PROP_TYPES:
                # SIM-134: delegate to MockOddsAPI.get_prop_odds() — single
                # source of truth for line centres, vig, and RNG seeding.
                prop = MockOddsAPI.get_prop_odds(
                    game_pk, pitcher_id, prop_stat, line_type="opening"
                )
                if not self._dry_run:
                    await self._db.execute(
                        """
                        INSERT INTO raw.prop_odds
                            (game_pk, player_id, source, is_mock,
                             prop_stat, line, over_ml, under_ml,
                             book, line_type, is_sharp_book)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                        """,
                        game_pk,
                        pitcher_id,
                        prop.get("source", "mock"),
                        prop.get("is_mock", True),
                        prop["prop_stat"],          # SIM-134: was prop["prop_type"]
                        prop["line"],
                        prop.get("over_ml"),
                        prop.get("under_ml"),
                        prop.get("book", "consensus"),
                        prop.get("line_type", "opening"),
                        prop.get("is_sharp_book", False),
                    )
                    inserted += 1
                    log.debug(
                        "Inserted opening prop %s=%.1f for pitcher %s game %s",
                        prop_stat, prop["line"], pitcher_id, game_pk,
                    )
                else:
                    log.info(
                        "[DRY RUN] Would insert opening prop %s=%.1f for pitcher %s game %s",
                        prop_stat, prop["line"], pitcher_id, game_pk,
                    )

        return inserted

    # ------------------------------------------------------------------
    # Pipeline run log
    # ------------------------------------------------------------------

    async def _start_log_row(self) -> int:
        """Inserts a 'running' log row and returns its id."""
        row = await self._db.fetchrow(
            """
            INSERT INTO raw.pipeline_run_log (job_name, status)
            VALUES ('opening_line_job', 'running')
            RETURNING id
            """,
        )
        return row["id"]

    async def _finish_log_row(
        self,
        run_id: int,
        status: str,
        summary: dict[str, int],
        error_message: str | None = None,
    ) -> None:
        """Updates the log row with final counts and status."""
        await self._db.execute(
            """
            UPDATE raw.pipeline_run_log
               SET finished_at                  = NOW(),
                   status                       = $1,
                   opening_line_games_captured  = $2,
                   opening_prop_lines_captured  = $3,
                   error_message                = $4
             WHERE id = $5
            """,
            status,
            summary.get("opening_line_games_captured", 0),
            summary.get("opening_prop_lines_captured", 0),
            error_message,
            run_id,
        )


# ---------------------------------------------------------------------------
# APScheduler integration — call from FastAPI lifespan
# ---------------------------------------------------------------------------

def schedule_opening_line_job(
    dsn: str,
    scheduler: Any,  # APScheduler AsyncIOScheduler
    hour: int = 8,
    minute: int = 0,
    timezone_str: str = "America/New_York",
) -> None:
    """
    Registers the opening line job with an APScheduler AsyncIOScheduler.

    Usage in FastAPI lifespan:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from pipeline.etl.opening_line_job import schedule_opening_line_job

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            scheduler = AsyncIOScheduler()
            schedule_opening_line_job(dsn=BASEBALL_DB_DSN, scheduler=scheduler)
            scheduler.start()
            yield
            scheduler.shutdown()

    The job fires once daily at 08:00 ET.  If the server restarts between midnight
    and 08:00 ET, the scheduler will fire it at the next 08:00 ET window — no
    backfill occurs automatically.  For manual backfill, run:
        python -m pipeline.etl.opening_line_job --days 7
    """
    from apscheduler.triggers.cron import CronTrigger  # type: ignore[import]

    async def _run_job() -> None:
        job = OpeningLineJob(dsn=dsn)
        await job.run()

    scheduler.add_job(
        _run_job,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=timezone_str),
        id="opening_line_job",
        name="Nightly Opening Line Capture (SIM-138)",
        replace_existing=True,
        misfire_grace_time=3600,   # allow up to 1 hour late (e.g. restart after 08:00)
    )
    log.info(
        "Opening line job scheduled: daily at %02d:%02d %s",
        hour, minute, timezone_str,
    )


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

async def _main(dsn: str, days: int, dry_run: bool) -> None:
    job = OpeningLineJob(dsn=dsn, lookahead_days=days, dry_run=dry_run)
    summary = await job.run()
    print("\nSummary:")
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SIM-138: Nightly Opening Line Ingestion Job"
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("BASEBALL_DB_DSN", "postgresql://localhost/baseball"),
        help="asyncpg PostgreSQL DSN (default: BASEBALL_DB_DSN env var)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=LOOKAHEAD_DAYS,
        help=f"Lookahead window in days (default: {LOOKAHEAD_DAYS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log actions without writing to the database",
    )
    args = parser.parse_args()

    asyncio.run(_main(dsn=args.dsn, days=args.days, dry_run=args.dry_run))
