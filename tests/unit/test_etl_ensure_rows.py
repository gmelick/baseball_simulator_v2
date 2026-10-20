"""The historical loader's per-table prerequisite writers: _ensure_teams, _ensure_players,
_ensure_managers and _ensure_game.

Each writer checks whether its rows exist and inserts the missing ones before a game's pitches
load. The tests drive the loader against queued mock connections (psycopg2 is never imported for
real), so each test seeds the rows that each successive connection returns.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pipeline.etl.etl_historical_loader import (
    HistoricalDataLoader,
)

# ===========================================================================
# _ensure_* short-circuit (existing-row) paths and basic flows
# ===========================================================================


def _make_loader_with_seq_conns(fetchall_seq=None, fetchone_seq=None):
    """Build a loader that exposes a queue of _get_conn() yields.

    Each successive call to ``loader._get_conn()`` returns a context manager
    that yields a fresh mock conn; that conn's cursor's fetchone() / fetchall()
    can be pre-seeded with values via the sequence arguments.
    """
    loader = HistoricalDataLoader(dsn="postgresql://test/db")
    fetchall_seq = list(fetchall_seq or [])
    fetchone_seq = list(fetchone_seq or [])

    def _make_ctx(_self=None):
        cur = MagicMock()
        cur.fetchall.return_value = fetchall_seq.pop(0) if fetchall_seq else []
        cur.fetchone.return_value = fetchone_seq.pop(0) if fetchone_seq else None
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False

        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__.return_value = conn
        conn.__exit__.return_value = False

        ctx = MagicMock()
        ctx.__enter__.return_value = conn
        ctx.__exit__.return_value = False
        return ctx

    loader._get_conn = MagicMock(side_effect=_make_ctx)
    return loader


class TestEnsureTeams:
    def test_short_circuit_when_both_teams_exist(self):
        loader = _make_loader_with_seq_conns(fetchall_seq=[[(112,), (158,)]])
        gd = {
            "teams": {
                "home": {"id": 112, "name": "Cubs", "abbreviation": "CHC"},
                "away": {"id": 158, "name": "Brewers", "abbreviation": "MIL"},
            },
            "venue": {"id": 17},
        }
        loader._ensure_teams(112, 158, season=2024, gd=gd)
        # No insert should have happened — only the existence check
        assert loader._get_conn.call_count == 1

    def test_inserts_missing_teams(self):
        loader = _make_loader_with_seq_conns(fetchall_seq=[[]])  # nothing exists
        loader._ensure_venue = MagicMock()
        gd = {
            "teams": {
                "home": {
                    "id": 112,
                    "name": "Cubs",
                    "abbreviation": "CHC",
                    "league": {"name": "National League"},
                    "division": {"name": "National League Central"},
                    "venue": {"id": 17},
                },
                "away": {
                    "id": 158,
                    "name": "Brewers",
                    "abbreviation": "MIL",
                    "league": {"name": "National League"},
                    "division": {"name": "National League Central"},
                    "venue": {"id": 32},
                },
            },
            "venue": {"id": 17},
        }
        loader._ensure_teams(112, 158, season=2024, gd=gd)
        # First call = existence check; subsequent = insert
        assert loader._get_conn.call_count >= 2

    def test_american_league_team_route(self):
        loader = _make_loader_with_seq_conns(fetchall_seq=[[]])
        loader._ensure_venue = MagicMock()
        gd = {
            "teams": {
                "home": {
                    "id": 147,
                    "name": "Yankees",
                    "abbreviation": "NYY",
                    "league": {"name": "American League"},
                    "division": {"name": "American League East"},
                    "venue": {"id": 3313},
                },
                "away": {
                    "id": 110,
                    "name": "Orioles",
                    "abbreviation": "BAL",
                    "league": {"name": "American League"},
                    "division": {"name": "American League East"},
                    "venue": {"id": 2},
                },
            },
            "venue": {"id": 3313},
        }
        loader._ensure_teams(147, 110, season=2024, gd=gd)


class TestEnsurePlayers:
    """SIM-441 rewrote _ensure_players.

    It used to (a) fabricate ``bats/throws='R'`` on any lookup failure, (b) return
    early when the player_id already existed — and ``raw.players`` has no season
    column, so a 2017 value applied to all ten seasons — and (c) carry an
    ``ON CONFLICT DO UPDATE`` that was unreachable, because every id it inserted
    had been proven absent moments earlier. A wrong hand was therefore silent,
    permanent, and unrepairable by any code path in the repo, while
    ``simulation/lineup_resolver`` reads that column straight into the full-pool
    sampler's only hard pre-filter.
    """

    @staticmethod
    def _boxscore(players: dict) -> dict:
        return {
            "liveData": {
                "boxscore": {"teams": {"home": {"players": players}, "away": {"players": {}}}}
            }
        }

    @staticmethod
    def _person(pid: int, **over) -> dict:
        person = {
            "id": pid,
            "fullName": "John Doe",
            "firstName": "John",
            "lastName": "Doe",
            "batSide": {"code": "R"},
            "pitchHand": {"code": "R"},
            "birthDate": "1995-01-01",
            "height": "6' 2\"",
            "weight": 200,
            "mlbDebutDate": "2020-03-26",
        }
        person.update(over)
        return person

    def test_empty_boxscore_noop(self):
        loader = _make_loader_with_seq_conns()
        loader._ensure_players({"liveData": {"boxscore": {"teams": {}}}})
        loader._get_conn.assert_not_called()

    def test_every_boxscore_player_is_upserted(self):
        """Not just the missing ones — that is what makes a wrong row repairable."""
        loader = _make_loader_with_seq_conns()
        gd = self._boxscore(
            {
                "ID100001": {"person": self._person(100001), "position": {"abbreviation": "P"}},
                "ID200001": {
                    "person": self._person(200001, batSide={"code": "L"}),
                    "position": {"abbreviation": "C"},
                },
            }
        )
        with patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch") as eb:
            loader._ensure_players(gd)
        eb.assert_called_once()
        rows = eb.call_args.args[2]
        assert {r[0] for r in rows} == {100001, 200001}

    def test_upsert_refreshes_handedness_and_position(self):
        loader = _make_loader_with_seq_conns()
        gd = self._boxscore(
            {"ID100001": {"person": self._person(100001), "position": {"abbreviation": "P"}}}
        )
        with patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch") as eb:
            loader._ensure_players(gd)
        sql = eb.call_args.args[1]
        for col in ("bats", "throws", "primary_position", "first_name", "last_name"):
            assert (
                f"{col}             = EXCLUDED.{col}" in sql
                or f"{col} = EXCLUDED.{col}" in sql
                or (f"{col}" in sql.split("DO UPDATE SET")[1])
            ), f"{col} must be refreshed on conflict"

    def test_prefers_the_feeds_own_person_record_over_an_http_call(self):
        """gameData.players.ID<pid> already carries the full record — use it."""
        loader = _make_loader_with_seq_conns()
        gd = self._boxscore(
            {"ID100001": {"person": {"id": 100001}, "position": {"abbreviation": "P"}}}
        )
        gd["gameData"] = {"players": {"ID100001": self._person(100001, batSide={"code": "L"})}}
        with (
            patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch") as eb,
            patch("pipeline.etl.etl_historical_loader._connect") as conn,
        ):
            loader._ensure_players(gd)
        conn.assert_not_called()
        assert eb.call_args.args[2][0][5] == "L"  # bats

    def test_falls_back_to_the_people_endpoint(self):
        loader = _make_loader_with_seq_conns()
        gd = self._boxscore(
            {
                "ID100001": {
                    "person": {"id": 100001, "fullName": "John Doe"},  # no bats/throws
                    "position": {"abbreviation": "P"},
                }
            }
        )
        with (
            patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch") as eb,
            patch(
                "pipeline.etl.etl_historical_loader._connect",
                return_value={"people": [self._person(100001)]},
            ),
        ):
            loader._ensure_players(gd)
        assert eb.call_args.args[2][0][5] == "R"

    def test_unknown_handedness_is_skipped_never_fabricated(self):
        """The headline fix.

        The old code wrote bats/throws='R' and the full name into BOTH first_name
        and last_name whenever the people endpoint failed — silently asserting a
        fact about a real player that nothing could ever correct.
        """
        loader = _make_loader_with_seq_conns()
        gd = self._boxscore(
            {
                "ID100001": {
                    "person": {"id": 100001, "fullName": "John Doe"},
                    "position": {"abbreviation": "P"},
                }
            }
        )
        with (
            patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch") as eb,
            patch(
                "pipeline.etl.etl_historical_loader._connect",
                side_effect=Exception("network down"),
            ),
        ):
            loader._ensure_players(gd)
        eb.assert_not_called()
        loader._get_conn.assert_not_called()

    def test_empty_people_list_is_skipped_not_defaulted(self):
        """A 200 with `people: []` raised IndexError and hit the same fabrication."""
        loader = _make_loader_with_seq_conns()
        gd = self._boxscore(
            {
                "ID100001": {
                    "person": {"id": 100001, "fullName": "John Doe"},
                    "position": {"abbreviation": "P"},
                }
            }
        )
        with (
            patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch") as eb,
            patch("pipeline.etl.etl_historical_loader._connect", return_value={"people": []}),
        ):
            loader._ensure_players(gd)
        eb.assert_not_called()

    def test_absent_position_defaults_to_utility_not_pitcher(self):
        """SIM-441: 'P' silently asserted a pitcher; 'UT' matches the sibling helper."""
        loader = _make_loader_with_seq_conns()
        gd = self._boxscore({"ID100001": {"person": self._person(100001)}})
        with patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch") as eb:
            loader._ensure_players(gd)
        assert eb.call_args.args[2][0][7] == "UT"


class TestEnsureManagers:
    def test_all_managers_blank_noop(self):
        loader = _make_loader_with_seq_conns()
        loader._ensure_managers(
            managers={
                "home_manager_id": "",
                "home_manager_name": "",
                "away_manager_id": "",
                "away_manager_name": "",
            },
            home_team_id=112,
            away_team_id=158,
            season=2024,
            game_date="2024-08-15",
        )
        loader._get_conn.assert_not_called()

    def test_managers_present_runs_existence_check(self):
        loader = _make_loader_with_seq_conns(fetchall_seq=[[(9999, 112, 2024)]])
        loader._ensure_managers(
            managers={
                "home_manager_id": 9999,
                "home_manager_name": "M Counsell",
                "away_manager_id": 9998,
                "away_manager_name": "P Murphy",
            },
            home_team_id=112,
            away_team_id=158,
            season=2024,
            game_date="2024-08-15",
        )
        # An existence check was issued
        assert loader._get_conn.call_count >= 1


class TestEnsureGame:
    def test_ensure_game_writes_final_score_from_livedata(self):
        """SIM-409: final scores are read from liveData.linescore (not gameData).

        The pre-fix code read ``gd.get('linescore')`` where gd was gameData, so
        the score was always NULL. _ensure_game now takes the full game_dict and
        upserts the score pulled from liveData.linescore.
        """
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        captured: list = []
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        cur.execute.side_effect = lambda sql, params=None: captured.append((sql, params))
        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__.return_value = conn
        conn.__exit__.return_value = False
        ctx = MagicMock()
        ctx.__enter__.return_value = conn
        ctx.__exit__.return_value = False
        loader._get_conn = MagicMock(return_value=ctx)

        game_dict = {
            "gameData": {
                "datetime": {"officialDate": "2024-08-15"},
                "status": {"abstractGameState": "Final", "codedGameState": "F"},
                "venue": {"id": 17},
                "teams": {"home": {"id": 112}, "away": {"id": 158}},
                "game": {"type": "R"},
                "weather": {},
            },
            "liveData": {
                "linescore": {
                    "teams": {"home": {"runs": 7}, "away": {"runs": 2}},
                    "currentInning": 9,
                },
                "decisions": {"winner": {"id": 600}, "loser": {"id": 601}},
            },
        }
        managers = {"home_manager_id": 9999, "away_manager_id": 9998}
        loader._ensure_game(745001, season=2024, game_dict=game_dict, managers=managers)

        insert = next((p for sql, p in captured if "INSERT INTO raw.games" in sql), None)
        assert insert is not None, "expected an INSERT INTO raw.games"
        # The home (7) / away (2) runs from liveData.linescore made it into the row.
        assert 7 in insert and 2 in insert
