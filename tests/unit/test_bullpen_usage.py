"""SIM-427 — the pen's facts from the box (``pipeline/bullpen_usage.py``).

The pure parts: the pen from the box's listing (available arms minus the
rotation minus today's starter, the used arms first), the two SQL spellings,
and the async fetchers over a fake connection. No DB.
"""

from __future__ import annotations

import asyncio
from datetime import date

from pipeline import bullpen_usage as bu


class TestPenFromListing:
    def test_available_arms_minus_rotation_minus_starter(self):
        listing = [
            (1, "started"),
            (2, "pitched"),
            (3, "pitched"),
            (4, "bullpen"),
            (5, "bullpen"),  # a resting rotation starter
            (6, "bullpen"),
        ]
        pen = bu.pen_from_listing(listing, rotation=[5, 1], exclude=[1])
        assert pen == [2, 3, 4, 6]  # the used arms first, then the rest, in listing order

    def test_duplicates_and_unknown_listings_are_dropped(self):
        listing = [(2, "pitched"), (2, "bullpen"), (7, "other"), (8, "bullpen")]
        assert bu.pen_from_listing(listing, rotation=[], exclude=[]) == [2, 8]

    def test_an_empty_listing_is_an_empty_pen(self):
        assert bu.pen_from_listing([], rotation=[1], exclude=[2]) == []


class TestSql:
    def test_two_spellings_of_every_query(self):
        for fn in (
            bu.usage_for_appearances_sql,
            bu.usage_for_candidates_sql,
            bu.rotation_starters_sql,
            bu.game_listing_sql,
            bu.game_managers_sql,
        ):
            a = fn("asyncpg")
            p = fn("psycopg2")
            assert "$1" in a and "%s" not in a
            assert "%s" in p and "$1" not in p

    def test_the_windows_are_strictly_before_the_game(self):
        sql = bu.usage_for_candidates_sql()
        assert "b.game_date < $2::date" in sql
        assert f"$2::date - {bu.PITCHED_WINDOW_DAYS}" in sql
        assert f"$2::date - {bu.PITCHES_WINDOW_DAYS}" in sql
        assert f"LEAST(COALESCE(($2::date - MAX(b.game_date))::int, {bu.REST_CAP_DAYS})" in sql
        bulk = bu.usage_for_appearances_sql()
        assert "b.game_date < a.game_date" in bulk and "played_pitch" in bulk

    def test_the_rotation_is_the_last_five_games_starters(self):
        sql = bu.rotation_starters_sql()
        assert "p_started" in sql and f"rk <= {bu.ROTATION_GAMES}" in sql
        assert "game_date < $2::date" in sql
        # an opener's start (under 45 pitches) does not make him a rotation starter
        assert f"p_pitches >= {bu.ROTATION_START_MIN_PITCHES}" in sql
        assert bu.ROTATION_START_MIN_PITCHES == 45


class _Conn:
    """asyncpg stand-in: routes by the query text."""

    def __init__(self, listing, rotation, usage):
        self.listing, self.rotation, self.usage = listing, rotation, usage
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *params):
        self.calls.append((sql, params))
        if "raw.game_bullpen" in sql:
            return [{"pitcher_id": p, "listed": listed} for p, listed in self.listing]
        if "p_started" in sql:
            return [{"player_id": p} for p in self.rotation]
        ids = params[0]
        return [
            {
                "player_id": p,
                "days_rest": self.usage.get(p, (5, False, 0))[0],
                "pitched_2d": self.usage.get(p, (5, False, 0))[1],
                "pitches_3d": self.usage.get(p, (5, False, 0))[2],
            }
            for p in ids
        ]


class TestFetchers:
    def test_fetch_pen_for_game_assembles_the_pen_and_its_usage(self):
        conn = _Conn(
            listing=[(1, "started"), (2, "pitched"), (4, "bullpen"), (5, "bullpen")],
            rotation=[5],
            usage={2: (0, True, 22), 4: (3, False, 0)},
        )
        out = asyncio.run(
            bu.fetch_pen_for_game(
                conn, game_pk=9, team_id=3, game_date=date(2024, 8, 15), exclude=[1]
            )
        )
        assert out is not None
        pen, usage = out
        assert pen == [2, 4]
        assert usage[2] == bu.ArmUsage(2, 0, True, 22)
        assert usage[4].as_tuple() == (3, 0, 0)
        # the candidates query got the pen ids and the game date
        sql, params = conn.calls[-1]
        assert "unnest($1::int[])" in sql and params == ([2, 4], date(2024, 8, 15))

    def test_no_listing_is_none(self):
        conn = _Conn(listing=[], rotation=[], usage={})
        assert (
            asyncio.run(
                bu.fetch_pen_for_game(conn, game_pk=9, team_id=3, game_date=date(2024, 8, 15))
            )
            is None
        )

    def test_usage_for_no_candidates_is_empty_without_a_query(self):
        conn = _Conn(listing=[], rotation=[], usage={})
        assert asyncio.run(bu.fetch_usage_for_candidates(conn, [], date(2024, 8, 15))) == {}
        assert conn.calls == []
