"""
Integration test: live-pipeline game upsert supplies season — SIM-438
=====================================================================
Drives the real ``LiveIngestionPipeline._upsert_game_record()``
(pipeline/live/live_ingestion_pipeline.py) against the migrated schema.

The bug (SIM-438)
-----------------
``raw.games.season`` is ``INTEGER NOT NULL`` (migration 0001, never relaxed) and
is half of all three composite foreign keys::

    FOREIGN KEY (venue_id, season)     REFERENCES raw.venues(venue_id, season)
    FOREIGN KEY (home_team_id, season) REFERENCES raw.teams(team_id, season)
    FOREIGN KEY (away_team_id, season) REFERENCES raw.teams(team_id, season)

but ``_upsert_game_record()`` never supplied it, so **every** INSERT of a game
the pipeline had not seen before raised ``NotNullViolationError``.  The failure
was silent because the call site is fire-and-forget
(``asyncio.create_task(self._upsert_game_record(game))``) — the exception never
propagated.  The ``ON CONFLICT (game_pk) DO UPDATE`` path kept working for games
the historical ETL had already loaded, so status transitions looked healthy
while no new game was ever created.

What this covers
----------------
  * a brand-new game is actually INSERTed, with season taken from the schedule
    API's ``season`` field, which arrives as a STRING ("2024") and must land in
    an INTEGER column;
  * the composite FKs resolve against the seeded venue/team rows for that season;
  * a payload missing ``season`` falls back to the game-date year rather than
    re-breaking the NOT NULL column;
  * the ON CONFLICT update path (Preview -> Final) still works and does not
    corrupt the stored season.

Isolation note
--------------
The pipeline writes through its own asyncpg pool and each statement commits, so
these tests cannot ride the rolled-back ``pg_connection`` transaction; the
fixture deletes the rows it created (and their FK parents) on teardown.

Run:
    pytest tests/integration/test_sim438_live_game_upsert.py -v -m integration
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants — ids owned by this module, removed on teardown.
# ---------------------------------------------------------------------------

_SEASON = 2024
_GAME_PKS = (748001, 748002, 748003)
_VENUE_ID = 9748001
_HOME_TEAM_ID = 9748010
_AWAY_TEAM_ID = 9748011


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def live_pipeline(asyncpg_pool) -> AsyncGenerator[LiveIngestionPipeline]:
    """A pipeline whose ``_db`` is the test pool, with the composite-FK parents
    (venue + both teams for ``_SEASON``) seeded so an INSERT into raw.games can
    actually satisfy its foreign keys.

    ``__init__`` insists on a DSN + Redis URL and builds an odds provider, none
    of which the upsert path touches, so we use the repo's standard
    constructor-bypass pattern (``__new__``) and inject only the pool.
    """
    await asyncpg_pool.execute(
        """
        INSERT INTO raw.venues (venue_id, season, venue_name, city, surface, roof_type)
        VALUES ($1, $2, 'SIM-438 Test Park', 'Testville', 'Grass', 'Open')
        ON CONFLICT (venue_id, season) DO NOTHING
        """,
        _VENUE_ID,
        _SEASON,
    )
    for team_id, abbrev in ((_HOME_TEAM_ID, "TH"), (_AWAY_TEAM_ID, "TA")):
        await asyncpg_pool.execute(
            """
            INSERT INTO raw.teams
                (team_id, season, team_name, team_abbrev, league, division, venue_id)
            VALUES ($1, $2, $3, $4, 'AL', 'AL East', $5)
            ON CONFLICT (team_id, season) DO NOTHING
            """,
            team_id,
            _SEASON,
            f"SIM-438 {abbrev}",
            abbrev,
            _VENUE_ID,
        )

    pipeline = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
    pipeline._db = asyncpg_pool
    try:
        yield pipeline
    finally:
        await asyncpg_pool.execute(
            "DELETE FROM raw.games WHERE game_pk = ANY($1::int[])", list(_GAME_PKS)
        )
        await asyncpg_pool.execute(
            "DELETE FROM raw.teams WHERE team_id = ANY($1::int[]) AND season = $2",
            [_HOME_TEAM_ID, _AWAY_TEAM_ID],
            _SEASON,
        )
        await asyncpg_pool.execute(
            "DELETE FROM raw.venues WHERE venue_id = $1 AND season = $2", _VENUE_ID, _SEASON
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _schedule_game(
    game_pk: int,
    *,
    season: str | None = "2024",
    game_date: str = "2024-08-15T23:05:00Z",
    abstract_state: str = "Preview",
) -> dict[str, Any]:
    """A schedule-API game payload shaped like the real MLB feed.

    Note ``season`` is a STRING — that is what the MLB schedule API returns, and
    raw.games.season is INTEGER, so the production code must coerce it.
    """
    payload: dict[str, Any] = {
        "gamePk": game_pk,
        "gameDate": game_date,
        "gameType": "R",
        "status": {"abstractGameState": abstract_state},
        "venue": {"id": _VENUE_ID},
        "teams": {
            "home": {"team": {"id": _HOME_TEAM_ID}},
            "away": {"team": {"id": _AWAY_TEAM_ID}},
        },
    }
    if season is not None:
        payload["season"] = season
    return payload


async def _get_game(pool, game_pk: int) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        "SELECT game_pk, season, game_date, status, venue_id, home_team_id, away_team_id "
        "FROM raw.games WHERE game_pk = $1",
        game_pk,
    )
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUpsertGameRecordSeason:
    """_upsert_game_record() supplies season, so a NEW game is actually created."""

    async def test_new_game_is_inserted_with_season_from_the_api_string(
        self, live_pipeline: LiveIngestionPipeline, asyncpg_pool
    ) -> None:
        """The regression: before SIM-438 this raised NotNullViolationError and
        left raw.games empty for every game the pipeline had not seen before."""
        game_pk = _GAME_PKS[0]
        assert await _get_game(asyncpg_pool, game_pk) is None

        await live_pipeline._upsert_game_record(_schedule_game(game_pk, season="2024"))

        row = await _get_game(asyncpg_pool, game_pk)
        assert row is not None, "the new game was not inserted"
        # The API sends season as a string; it must land as an INTEGER.
        assert row["season"] == _SEASON
        assert isinstance(row["season"], int)
        # The composite FKs (venue_id, season) / (team_id, season) resolved.
        assert row["venue_id"] == _VENUE_ID
        assert row["home_team_id"] == _HOME_TEAM_ID
        assert row["away_team_id"] == _AWAY_TEAM_ID
        assert row["status"] == "Preview"

    async def test_season_falls_back_to_the_game_date_year_when_absent(
        self, live_pipeline: LiveIngestionPipeline, asyncpg_pool
    ) -> None:
        """A payload with no ``season`` key must still insert (the column is NOT
        NULL), deriving the season from the game date rather than crashing."""
        game_pk = _GAME_PKS[1]

        await live_pipeline._upsert_game_record(
            _schedule_game(game_pk, season=None, game_date="2024-05-01T18:10:00Z")
        )

        row = await _get_game(asyncpg_pool, game_pk)
        assert row is not None, "a payload without 'season' must not break the insert"
        assert row["season"] == 2024

    async def test_status_transition_updates_without_corrupting_season(
        self, live_pipeline: LiveIngestionPipeline, asyncpg_pool
    ) -> None:
        """The ON CONFLICT path (Preview -> Final) still updates in place, keeps
        exactly one row, and leaves the stored season intact."""
        game_pk = _GAME_PKS[2]

        await live_pipeline._upsert_game_record(
            _schedule_game(game_pk, season="2024", abstract_state="Preview")
        )
        await live_pipeline._upsert_game_record(
            _schedule_game(game_pk, season="2024", abstract_state="Final")
        )

        count = await asyncpg_pool.fetchval(
            "SELECT COUNT(*) FROM raw.games WHERE game_pk = $1", game_pk
        )
        assert count == 1, "the upsert duplicated the game instead of updating it"

        row = await _get_game(asyncpg_pool, game_pk)
        assert row is not None
        assert row["status"] == "Final"
        assert row["season"] == _SEASON
