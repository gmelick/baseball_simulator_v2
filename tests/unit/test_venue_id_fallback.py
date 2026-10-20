"""A game with no venue is stored with ``venue_id`` NULL, never a 0 sentinel:
``raw.games.venue_id`` is nullable (migration 0006), the live pipeline's
``_upsert_game_record`` binds None when the feed has no venue, and the venue
backfill job imports cleanly."""

from __future__ import annotations

import pathlib
import re
from unittest.mock import AsyncMock, MagicMock

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PG_SCHEMA_PATH = _ROOT / "db" / "schemas" / "01_postgres_schema.sql"
_MIG_DIR = _ROOT / "db" / "migrations" / "versions"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _games_insert_arg(call_args, column: str):
    """Return the positional value bound to ``column`` in the raw.games INSERT.

    ``_db.execute`` is called as ``(sql, *values)`` where ``values`` follow the
    INSERT's column-list order, so adding a column shifts every later index.
    The helper reads the position from the SQL itself, so a new column cannot
    point an assertion at the wrong argument.
    """
    sql = call_args.args[0]
    match = re.search(r"INSERT INTO raw\.games\s*\((.*?)\)\s*VALUES", sql, re.DOTALL)
    assert match, "could not parse the raw.games INSERT column list"
    columns = [c.strip() for c in match.group(1).replace("\n", " ").split(",") if c.strip()]
    assert column in columns, f"{column!r} is not in the INSERT column list {columns}"
    return call_args.args[1 + columns.index(column)]


class TestVenueIdFallback:
    """The live pipeline never inserts a ``venue_id`` of 0."""

    def test_canonical_schema_drops_not_null(self) -> None:
        sql = _read(_PG_SCHEMA_PATH)
        # Find the raw.games CREATE TABLE block
        match = re.search(
            r"CREATE TABLE raw\.games\s*\((.*?)\n\);",
            sql,
            re.DOTALL,
        )
        assert match, "raw.games CREATE TABLE block not found in canonical schema"
        body = match.group(1)
        # Find the venue_id line
        venue_line = next(
            (ln for ln in body.splitlines() if re.match(r"\s*venue_id\b", ln)),
            None,
        )
        assert venue_line is not None, "venue_id column missing from raw.games"
        assert "NOT NULL" not in venue_line.upper(), (
            f"SIM-086: raw.games.venue_id must be nullable. Current line: {venue_line!r}"
        )

    def test_migration_0006_drops_not_null(self) -> None:
        mig = _read(_MIG_DIR / "0006_sim086_games_venue_id_nullable.py")
        assert "ALTER COLUMN venue_id DROP NOT NULL" in mig
        assert 'down_revision = "0005"' in mig

    def test_upsert_game_record_emits_none_when_venue_missing(self) -> None:
        """A game with no venue binds None: a 0 breaks the foreign key on
        ``raw.venues``."""
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        # Build a minimal pipeline instance without invoking __init__
        pipeline = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        pipeline._db = MagicMock()
        pipeline._db.execute = AsyncMock()

        # Game dict with NO venue key — the production failure mode
        game = {
            "gamePk": 745001,
            "gameDate": "2026-05-06T18:05:00Z",
            "gameType": "R",
            "status": {"abstractGameState": "Preview"},
            "teams": {
                "home": {"team": {"id": 147}},
                "away": {"team": {"id": 111}},
            },
            # 'venue' key intentionally absent
        }

        import asyncio

        asyncio.run(pipeline._upsert_game_record(game))

        # The value bound to the venue_id column is None, not 0, read by
        # column name.
        assert pipeline._db.execute.call_count == 1
        call_args = pipeline._db.execute.call_args
        venue_id_arg = _games_insert_arg(call_args, "venue_id")
        assert venue_id_arg is None, (
            f"SIM-086 regression: venue_id should be None when venue key is missing, got {venue_id_arg!r}. "
            "The previous .get('id', 0) sentinel re-introduced the FK violation."
        )

    def test_upsert_game_record_emits_real_venue_when_present(self) -> None:
        """A game with a venue binds the venue's id."""
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        pipeline = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        pipeline._db = MagicMock()
        pipeline._db.execute = AsyncMock()

        game = {
            "gamePk": 745002,
            "gameDate": "2026-05-06T18:05:00Z",
            "gameType": "R",
            "status": {"abstractGameState": "Live"},
            "venue": {"id": 4169, "name": "Yankee Stadium"},
            "teams": {
                "home": {"team": {"id": 147}},
                "away": {"team": {"id": 111}},
            },
        }

        import asyncio

        asyncio.run(pipeline._upsert_game_record(game))

        venue_id_arg = _games_insert_arg(pipeline._db.execute.call_args, "venue_id")
        assert venue_id_arg == 4169
        # The season comes from the feed's string field as an int
        # (raw.games.season is NOT NULL).
        assert _games_insert_arg(pipeline._db.execute.call_args, "season") == 2026

    def test_venue_backfill_job_imports(self) -> None:
        """The venue backfill job imports and constructs without a connection."""
        from pipeline.etl import venue_backfill_job  # noqa: F401
        from pipeline.etl.venue_backfill_job import (
            VenueBackfillJob,
            schedule_venue_backfill_job,
        )

        # __init__ opens no connection, so a placeholder DSN is enough.
        job = VenueBackfillJob(dsn="postgresql://u:p@localhost/db", dry_run=True)
        assert job._dry_run is True
        assert callable(schedule_venue_backfill_job)
