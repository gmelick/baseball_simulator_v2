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
3. If none exists, fetches the moneyline's opening rows and stores them as
   line_type='opening'.
4. When a starting pitcher has been announced (status = 'Preview' and lineup posted),
   also stores player prop lines as line_type='opening' in raw.prop_odds.
5. Logs a raw.pipeline_run_log row with opening_line_games_captured count.

SIM-555 (2026-09-28): one row per book
--------------------------------------
The job reads the provider's by-book rows (``odds_rows_by_book`` /
``prop_rows_by_book``). BettingPros gives at most one opening row per market:
the opener's, labelled ``book = 'bp:<id>'`` and stamped ``book_line_at``. The
opener can be any kind of book (a sportsbook, the blend, an exchange, a
prediction market); the job stores it under its label, as the historical
loader does, and the readers keep only the bettable books. When the two sides
name two openers the provider gives an empty row, and the job writes nothing:
an empty row would break the NOT NULL ``raw.prop_odds.line`` and abort the
run, or store an all-empty game row that blocks tomorrow's retry. Every
non-empty row passes the load guard (``pipeline/odds_row_guard.py``) first; a
refused row is logged and counted. The mock provider keeps its behaviour: its
one ``consensus`` row per market.

The rows go to the database through the live pipeline's writers
(``insert_game_odds_rows`` / ``insert_prop_odds_rows``): the same SQL, the
``odds_hash`` the live schema requires (NOT NULL on ``raw.game_odds`` since
migration 0012; before SIM-555 this job sent none, so every game-row INSERT
failed), and the ``ON CONFLICT ... DO NOTHING`` dedup.

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
import pathlib

# Odds/prop lines are fetched through the SIM-370 provider seam
# (get_odds_provider) so ODDS_PROVIDER selects the source — the same seam
# scripts/load_historical_odds.py uses.  The seam's default is the deterministic
# MockOddsAPI, so behaviour is unchanged when ODDS_PROVIDER is unset.
import sys
from datetime import date, timedelta
from typing import Any

import asyncpg

# Ensure project root is on sys.path when run as a standalone script.
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# SIM-555: the live pipeline's writers: the one INSERT, the odds_hash, the dedup.
from pipeline.live.live_ingestion_pipeline import (  # noqa: E402
    insert_game_odds_rows,
    insert_prop_odds_rows,
)
from pipeline.odds_provider import (  # noqa: E402
    BATTER_PROP_STATS,
    GAME_ODDS_FIELDS,
    PITCHER_PROP_STATS,
    OddsProvider,
    get_odds_provider,
    odds_rows_by_book,
    prop_rows_by_book,
)

# SIM-555: the load guard (pure; no I/O).
from pipeline.odds_row_guard import RefusalTally, check_row  # noqa: E402

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
GAME_TYPES = ["R", "F", "D", "L", "W", "P"]
LOOKAHEAD_DAYS = 7  # how many days ahead to capture opening lines

# SIM-134 / SIM-421: the prop markets, aligned with the 15-value CHECK constraint
# on raw.prop_odds (migration 0022). The lists live in pipeline/odds_provider.py
# (the single source); this job keeps its historical names as aliases.
#
# Pitcher props captured when a starter is announced (starting pitcher known at schedule time).
# Batter props deferred: lineup position not reliably known 5–7 days out.
PITCHER_PROP_TYPES: tuple[str, ...] = PITCHER_PROP_STATS
BATTER_PROP_TYPES: tuple[str, ...] = BATTER_PROP_STATS

# Legacy _MOCK_PROP_LINES and local _mock_prop_odds() removed in SIM-134.
# All prop generation is now delegated to the configured provider's
# get_prop_odds() (the default mock keeps the vig model, RNG seeding, and line
# centres in a single place) — the provider is selected via the SIM-370 seam.

#: SIM-555: the odds fields of a prop row; a row with all three empty is not a quote.
_PROP_ODDS_FIELDS: tuple[str, ...] = ("line", "over_ml", "under_ml")


def _has_odds(row: dict[str, Any]) -> bool:
    """SIM-555: True when a game or prop row carries at least one price or line."""
    fields = _PROP_ODDS_FIELDS if row.get("prop_stat") is not None else GAME_ODDS_FIELDS
    return any(row.get(f) is not None for f in fields)


# ---------------------------------------------------------------------------
# Main job class
# ---------------------------------------------------------------------------


class OpeningLineJob:
    """
    Nightly job that captures opening lines for all games in the 7-day lookahead.

    Designed to be idempotent: calling it multiple times on the same date does
    not duplicate rows. The "already exists" checks skip a game or a pitcher
    that already has an opening row; SIM-555: the writes also carry the
    ``odds_hash`` dedup (INSERT … ON CONFLICT DO NOTHING) of the live writers.

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
        self._dsn = dsn
        self._lookahead = lookahead_days
        self._dry_run = dry_run
        self._db: asyncpg.Pool | None = None
        # SIM-555: one provider per run (its offers cache then serves the game
        # and prop reads of one event), and the guard's refusals for the run.
        self._provider: OddsProvider | None = None
        self.refusals = RefusalTally()

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
            "games_checked": 0,
            "opening_line_games_captured": 0,
            "opening_prop_lines_captured": 0,
            "games_already_had_opening": 0,
        }

        try:
            run_id = await self._start_log_row()
            games = await self._fetch_upcoming_games()
            log.info(
                "Opening line job: found %d games in next %d days",
                len(games),
                self._lookahead,
            )

            for game in games:
                summary["games_checked"] += 1
                game_pk = game["game_pk"]
                already = await self._has_opening_line(game_pk)

                if already:
                    summary["games_already_had_opening"] += 1
                    log.debug("game %s: opening line already captured", game_pk)
                    continue

                # Capture the game-level opening rows (SIM-555: one per book the
                # provider gives; none when it has no one-book opening row).
                rows = self._fetch_opening_rows(game_pk)
                for odds in rows:
                    await self._store_opening_line(game_pk, odds)
                if rows:
                    summary["opening_line_games_captured"] += 1
                    log.info(
                        "game %s: %d opening row(s) captured (books %s)",
                        game_pk,
                        len(rows),
                        ", ".join(str(r.get("book")) for r in rows),
                    )
                else:
                    log.info("game %s: no opening line to capture yet", game_pk)

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
                dict(summary.items()),
            )
            if self.refusals.refused:
                log.info("%s", self.refusals.summary())

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

        today = date.today()
        end_date = today + timedelta(days=self._lookahead)
        params = {
            "sportId": 1,
            "gameTypes": ",".join(GAME_TYPES),
            "startDate": today.strftime("%Y-%m-%d"),
            "endDate": end_date.strftime("%Y-%m-%d"),
            "hydrate": "probablePitcher(note)",
        }

        async with (
            aiohttp.ClientSession() as session,
            session.get(
                MLB_SCHEDULE_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp,
        ):
            resp.raise_for_status()
            data = await resp.json()

        games: list[dict[str, Any]] = []
        for date_entry in data.get("dates", []):
            for g in date_entry.get("games", []):
                if "rescheduleGameDate" in g or "resumeGameDate" in g:
                    continue
                game_pk = g["gamePk"]
                status = g.get("status", {}).get("abstractGameState", "Preview")

                # Extract probable pitchers if announced
                home_pitcher_id = (
                    g.get("teams", {}).get("home", {}).get("probablePitcher", {}).get("id")
                )
                away_pitcher_id = (
                    g.get("teams", {}).get("away", {}).get("probablePitcher", {}).get("id")
                )

                games.append(
                    {
                        "game_pk": game_pk,
                        "game_date": date_entry["date"],
                        "status": status,
                        "home_pitcher_id": home_pitcher_id,
                        "away_pitcher_id": away_pitcher_id,
                    }
                )

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
    # Odds fetch  (via the SIM-370 provider seam; ODDS_PROVIDER selects source)
    # ------------------------------------------------------------------

    def _odds_provider(self) -> OddsProvider:
        """The configured provider (the SIM-370 seam), built once per job."""
        if self._provider is None:
            self._provider = get_odds_provider()
        return self._provider

    def _keep(self, game_pk: int, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """SIM-555: the rows worth storing: non-empty, and kept by the load guard.

        A row with no price or line is dropped silently. A refused row is
        logged at INFO and counted in ``self.refusals``.
        """
        kept: list[dict[str, Any]] = []
        for row in rows:
            if not _has_odds(row):
                continue
            self.refusals.offered()
            refusal = check_row(row)
            if refusal is None:
                kept.append(row)
                continue
            prop_stat = row.get("prop_stat")
            market = str(prop_stat if prop_stat is not None else row.get("market_type"))
            self.refusals.add(refusal, market, str(row.get("book")))
            log.info(
                "refused game %s %s/%s %s: %s",
                game_pk,
                row.get("line_type"),
                market,
                row.get("book"),
                refusal.message,
            )
        return kept

    def _fetch_opening_rows(self, game_pk: int) -> list[dict[str, Any]]:
        """
        SIM-555: the moneyline's opening rows for ``game_pk`` that are worth storing.

        Read through ``odds_rows_by_book`` from the configured provider (the
        SIM-370 seam; the deterministic mock when ODDS_PROVIDER is unset, which
        gives its one ``consensus`` row). Empty rows are dropped and the load
        guard checks the rest, so the list is empty when the provider has no
        one-book opening row.
        """
        rows = odds_rows_by_book(
            self._odds_provider(), game_pk, line_type="opening", market_type="moneyline"
        )
        return self._keep(game_pk, rows)

    # ------------------------------------------------------------------
    # Database writes
    # ------------------------------------------------------------------

    async def _store_opening_line(self, game_pk: int, odds: dict[str, Any]) -> None:
        """
        Inserts a single raw.game_odds row with line_type='opening'.
        Skips the write if dry_run=True.

        SIM-555: the row keeps its book label and writes ``book_line_at``
        (migration 0028). It goes through the live writer
        (``insert_game_odds_rows``), so it carries the ``odds_hash`` the live
        schema requires and the ``ON CONFLICT`` dedup. A row with no
        ``line_type`` key is stored as ``'opening'``, as before.
        """
        if self._dry_run:
            log.info(
                "[DRY RUN] Would insert opening line for game %s (%s)", game_pk, odds.get("book")
            )
            return

        await insert_game_odds_rows(self._db, game_pk, [{"line_type": "opening", **odds}])

    async def _capture_prop_opening_lines(
        self,
        game_pk: int,
        home_pitcher_id: int | None,
        away_pitcher_id: int | None,
    ) -> int:
        """
        SIM-134: Stores opening prop lines for announced starters.

        Covers:
          - Pitcher props: every market in PITCHER_PROP_TYPES (strikeouts,
            earned_runs, walks, outs_recorded, hits_allowed — SIM-421)
            (captured when starter is announced — known 5–7 days out)
          - Batter props deferred: lineup order not reliably known this far
            in advance; captured intraday once lineup is posted.

        Prop generation is delegated to the configured provider via the SIM-370
        seam (the default mock keeps the vig model and RNG seeding in a single
        canonical place — SIM-134).

        SIM-555: each (pitcher, market) stores every by-book opening row the
        provider gives that the load guard keeps, with its book label and
        ``book_line_at``, in one batch through the live writer
        (``insert_prop_odds_rows``: the ``odds_hash`` and the dedup); a market
        with no such row stores nothing.

        Returns the number of prop rows sent (the dedup may insert fewer).
        """
        provider = self._odds_provider()
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
                game_pk,
                pitcher_id,
            )
            if existing:
                log.debug(
                    "Opening prop lines already exist for pitcher %s game %s — skipping",
                    pitcher_id,
                    game_pk,
                )
                continue

            for prop_stat in PITCHER_PROP_TYPES:
                # SIM-370 seam: delegate to the configured provider (the default
                # mock is the single source of truth for line centres, vig, and
                # RNG seeding — SIM-134). SIM-555: every book's opening row.
                rows = prop_rows_by_book(
                    provider, game_pk, pitcher_id, prop_stat, line_type="opening"
                )
                # SIM-555: the row is keyed to this game and pitcher, and a row
                # with no line_type key is stored as 'opening', as before.
                kept = [
                    {"line_type": "opening", **prop, "game_pk": game_pk, "player_id": pitcher_id}
                    for prop in self._keep(game_pk, rows)
                ]
                if not kept:
                    continue
                if self._dry_run:
                    for prop in kept:
                        log.info(
                            "[DRY RUN] Would insert opening prop %s=%.1f for pitcher %s "
                            "game %s (%s)",
                            prop_stat,
                            prop["line"],
                            pitcher_id,
                            game_pk,
                            prop.get("book"),
                        )
                    continue
                # SIM-555: one batch per market, through the live writer (the
                # odds_hash and the ON CONFLICT dedup).
                inserted += await insert_prop_odds_rows(self._db, kept)
                log.debug(
                    "Inserted %d opening prop row(s) %s for pitcher %s game %s (%s)",
                    len(kept),
                    prop_stat,
                    pitcher_id,
                    game_pk,
                    ", ".join(str(prop.get("book")) for prop in kept),
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
        misfire_grace_time=3600,  # allow up to 1 hour late (e.g. restart after 08:00)
    )
    log.info(
        "Opening line job scheduled: daily at %02d:%02d %s",
        hour,
        minute,
        timezone_str,
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
    parser = argparse.ArgumentParser(description="SIM-138: Nightly Opening Line Ingestion Job")
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
