"""
SIM-440 — corrective re-ingest path + the parser/validator fixes that need it.
=============================================================================

Covers:
  * ``HistoricalDataLoader.reload_game`` — DELETE + re-INSERT in ONE transaction,
    the only way to repair already-loaded ``raw.pitches`` rows (the INSERT
    carries ``ON CONFLICT … DO NOTHING``, so plain ``load_game`` writes nothing).
  * ``_batch_insert`` returning the MEASURED row count instead of the attempted
    one — a silent no-op re-ingest used to report full success.
  * ``refresh_seasons(reload=True)`` / ``load_date_range(reload=True)`` bypassing
    the ``_game_already_loaded`` skip, and containing per-game failures so one
    malformed feed cannot abort a 21.6k-game sweep.
  * Removal of ``backfill_lineups_and_scores`` (superseded by ``reload_game``).
  * The substitution scan landing on the event where the substitution actually
    occurs — including the first-event case that used to raise ``NameError``.
  * ``_validate_row`` treating all three in-play pitch codes (D/E/X) alike.
  * ``pull_relative_spray_angle`` keyed on ``stand`` (never 'S') rather than
    ``bat_hand`` (10.4-13.3% 'S' in every season).

Mocking style mirrors tests/unit/test_etl_historical_loader.py.
"""

from __future__ import annotations

import inspect
import re
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.etl.etl_historical_loader import (
    _CONSECUTIVE_FAILURE_LIMIT,
    HistoricalDataLoader,
    ReloadShrinkError,
    _fetch_game_pitches,
    _validate_row,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_loader_with_mock_conn(fetchone_side_effect=None):
    """Loader whose ``_get_conn`` yields a mocked psycopg2 connection."""
    loader = HistoricalDataLoader(dsn="postgresql://test/db")

    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = []
    if fetchone_side_effect is not None:
        mock_cur.fetchone.side_effect = fetchone_side_effect
    else:
        mock_cur.fetchone.return_value = (0,)
    mock_cur.rowcount = 0
    mock_cur.__enter__.return_value = mock_cur
    mock_cur.__exit__.return_value = False

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.__exit__.return_value = False

    ctx = MagicMock()
    ctx.__enter__.return_value = mock_conn
    ctx.__exit__.return_value = False
    loader._get_conn = MagicMock(return_value=ctx)

    return loader, mock_conn, mock_cur


_MINIMAL_ROW = {"game_pk": 745001, "at_bat_number": 1, "pitch_number": 1}


def _executed_sql(cur) -> list[str]:
    """Every SQL string passed to ``cur.execute``."""
    return [c.args[0] for c in cur.execute.call_args_list if c.args]


# ---------------------------------------------------------------------------
# _batch_insert — replace + truthful count
# ---------------------------------------------------------------------------


class TestBatchInsertReplace:
    def test_replace_issues_delete_before_insert(self):
        """reload must DELETE the game's rows before re-inserting."""
        loader, conn, cur = _make_loader_with_mock_conn(
            fetchone_side_effect=[(0,), (2,)]  # count before, count after
        )
        rows = [dict(_MINIMAL_ROW), dict(_MINIMAL_ROW, pitch_number=2)]

        with patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch") as eb:
            loader._batch_insert(rows, replace_game_pk=745001)

        sql = _executed_sql(cur)
        assert any("DELETE FROM raw.pitches" in s for s in sql), (
            f"reload must delete existing rows first, executed: {sql}"
        )
        # The DELETE must be issued before the INSERT batch runs.
        delete_idx = next(i for i, s in enumerate(sql) if "DELETE FROM raw.pitches" in s)
        assert delete_idx == 0, "DELETE must be the first statement in the transaction"
        eb.assert_called_once()

    def test_replace_delete_and_insert_share_one_transaction(self):
        """Atomicity: one connection, one commit — never a half-deleted game."""
        loader, conn, cur = _make_loader_with_mock_conn(fetchone_side_effect=[(0,), (1,)])
        with patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch"):
            loader._batch_insert([dict(_MINIMAL_ROW)], replace_game_pk=745001)

        assert loader._get_conn.call_count == 1, "delete + insert must share one connection"
        conn.commit.assert_called_once()
        conn.rollback.assert_not_called()

    def test_failed_insert_rolls_back_the_delete(self):
        """If the re-insert explodes, the DELETE must not be committed."""
        loader, conn, cur = _make_loader_with_mock_conn(fetchone_side_effect=[(0,), (0,)])
        with patch(
            "pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch",
            side_effect=RuntimeError("insert blew up"),
        ):
            with pytest.raises(RuntimeError, match="insert blew up"):
                loader._batch_insert([dict(_MINIMAL_ROW)], replace_game_pk=745001)

        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()

    def test_without_replace_no_delete_is_issued(self):
        """The ordinary incremental path must never delete."""
        loader, conn, cur = _make_loader_with_mock_conn(fetchone_side_effect=[(0,), (1,)])
        with patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch"):
            loader._batch_insert([dict(_MINIMAL_ROW)])

        assert not any("DELETE" in s.upper() for s in _executed_sql(cur))

    def test_returns_measured_count_not_attempted_count(self):
        """A re-ingest discarded by DO NOTHING must report 0, not len(rows).

        This is the defect that made corrective re-runs look successful: the old
        implementation returned ``len(rows)`` regardless of what the database
        actually accepted.
        """
        loader, conn, cur = _make_loader_with_mock_conn(
            fetchone_side_effect=[(290,), (290,)]  # unchanged → everything discarded
        )
        rows = [dict(_MINIMAL_ROW, pitch_number=n) for n in range(1, 291)]

        with patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch"):
            written = loader._batch_insert(rows)

        assert written == 0, (
            "ON CONFLICT DO NOTHING discarded every row; _batch_insert must report 0"
        )

    def test_returns_measured_count_on_a_real_write(self):
        loader, conn, cur = _make_loader_with_mock_conn(fetchone_side_effect=[(0,), (3,)])
        rows = [dict(_MINIMAL_ROW, pitch_number=n) for n in (1, 2, 3)]
        with patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch"):
            assert loader._batch_insert(rows) == 3

    def test_reload_refuses_to_shrink_a_game(self):
        """A reload that writes back FEWER rows than it deleted is data loss.

        Reachable with no exception at all: _fetch_game_pitches returns
        ([], game_dict) for an HTTP-200 feed whose allPlays is empty or
        pitch-less — a postponed/suspended game, a feed blip, or (via
        load_date_range, which has no Final gate) a game that has not been
        played yet.  Unguarded, the sweep would delete a complete game and
        commit nothing in its place.
        """
        loader, conn, cur = _make_loader_with_mock_conn(fetchone_side_effect=[(0,), (0,)])
        cur.rowcount = 290  # the DELETE removed a full game

        with patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch"):
            with pytest.raises(ReloadShrinkError, match="745001"):
                loader._batch_insert([], replace_game_pk=745001)

        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()

    def test_shrink_guard_can_be_overridden_explicitly(self):
        loader, conn, cur = _make_loader_with_mock_conn(fetchone_side_effect=[(0,), (0,)])
        cur.rowcount = 290
        with patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch"):
            assert loader._batch_insert([], replace_game_pk=745001, allow_shrink=True) == 0
        conn.commit.assert_called_once()
        conn.rollback.assert_not_called()

    def test_equal_row_count_reload_commits(self):
        """The ordinary reload — same pitch count in, same out — must commit."""
        loader, conn, cur = _make_loader_with_mock_conn(fetchone_side_effect=[(0,), (2,)])
        cur.rowcount = 2
        rows = [dict(_MINIMAL_ROW), dict(_MINIMAL_ROW, pitch_number=2)]
        with patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch"):
            assert loader._batch_insert(rows, replace_game_pk=745001) == 2
        conn.commit.assert_called_once()
        conn.rollback.assert_not_called()

    def test_shrink_guard_does_not_fire_on_the_non_replace_path(self):
        """A plain incremental insert has deleted nothing, so it can never shrink."""
        loader, conn, cur = _make_loader_with_mock_conn(fetchone_side_effect=[(290,), (290,)])
        cur.rowcount = 290
        rows = [dict(_MINIMAL_ROW, pitch_number=n) for n in range(1, 291)]
        with patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch"):
            assert loader._batch_insert(rows) == 0
        conn.commit.assert_called_once()

    def test_empty_rows_without_replace_is_a_noop(self):
        loader, conn, _ = _make_loader_with_mock_conn()
        assert loader._batch_insert([]) == 0
        assert not loader._get_conn.called


# ---------------------------------------------------------------------------
# reload_game / load_game
# ---------------------------------------------------------------------------


class TestReloadGame:
    @staticmethod
    def _loader():
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._ensure_prerequisites = MagicMock()
        loader._process_and_insert = MagicMock(
            return_value={"inserted": 290, "skipped": 0, "flagged": 0}
        )
        return loader

    def test_reload_game_requests_replacement(self):
        loader = self._loader()
        with patch(
            "pipeline.etl.etl_historical_loader._fetch_game_pitches",
            return_value=([{"game_pk": 745001}], {"_managers": {}, "gameData": {}}),
        ):
            result = loader.reload_game(745001, 2024)

        assert result["inserted"] == 290
        assert loader._process_and_insert.call_args.kwargs["replace"] is True

    def test_load_game_does_not_request_replacement(self):
        loader = self._loader()
        with patch(
            "pipeline.etl.etl_historical_loader._fetch_game_pitches",
            return_value=([{"game_pk": 745001}], {"_managers": {}, "gameData": {}}),
        ):
            loader.load_game(745001, 2024)

        assert loader._process_and_insert.call_args.kwargs["replace"] is False

    def test_reload_game_reruns_prerequisites(self):
        """reload_game must re-upsert venues/teams/players/games/lineups.

        This is what makes it a strict superset of the removed
        ``backfill_lineups_and_scores`` helper.
        """
        loader = self._loader()
        with patch(
            "pipeline.etl.etl_historical_loader._fetch_game_pitches",
            return_value=([{"game_pk": 745001}], {"_managers": {}, "gameData": {}}),
        ):
            loader.reload_game(745001, 2024)

        loader._ensure_prerequisites.assert_called_once()

    def test_process_and_insert_threads_replace_to_batch_insert(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._batch_insert = MagicMock(return_value=1)
        loader._log_freshness = MagicMock()

        good = {
            "game_pk": 745001,
            "at_bat_number": 1,
            "pitch_number": 1,
            "game_date": "2024-08-15",
            "venue_id": 17,
            "pitcher": 1,
            "p_throws": "R",
            "batter": 2,
            "stand": "R",
            "bat_hand": "R",
            "inning": 1,
            "top_bot": "top",
            "balls": 0,
            "strikes": 0,
            "outs": 0,
            "home_score": 0,
            "away_score": 0,
        }
        loader._process_and_insert(745001, 2024, [good], replace=True)
        assert loader._batch_insert.call_args.kwargs["replace_game_pk"] == 745001

        loader._batch_insert.reset_mock()
        loader._process_and_insert(745001, 2024, [good], replace=False)
        assert loader._batch_insert.call_args.kwargs["replace_game_pk"] is None

    def test_backfill_lineups_and_scores_is_removed(self):
        """Superseded by reload_game; must not linger as a second, weaker path."""
        assert not hasattr(HistoricalDataLoader, "backfill_lineups_and_scores")


# ---------------------------------------------------------------------------
# refresh_seasons / load_date_range reload wiring
# ---------------------------------------------------------------------------


def _schedule_one_final_game(game_pk: int = 745001) -> dict:
    return {
        "dates": [
            {
                "date": "2024-08-15",
                "games": [{"gamePk": game_pk, "status": {"abstractGameState": "Final"}}],
            }
        ]
    }


class TestReloadSweeps:
    def test_refresh_seasons_reload_bypasses_already_loaded_gate(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._game_already_loaded = MagicMock(return_value=True)
        loader.load_game = MagicMock()
        loader.reload_game = MagicMock(return_value={"inserted": 1, "skipped": 0, "flagged": 0})
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            return_value=_schedule_one_final_game(),
        ):
            summary = loader.refresh_seasons(2024, 2024, reload=True)

        loader.reload_game.assert_called_once()
        loader.load_game.assert_not_called()
        loader._game_already_loaded.assert_not_called()
        assert summary["loaded"] == 1
        assert summary["failed"] == 0

    def test_refresh_seasons_default_keeps_the_skip(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._game_already_loaded = MagicMock(return_value=True)
        loader.load_game = MagicMock()
        loader.reload_game = MagicMock()
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            return_value=_schedule_one_final_game(),
        ):
            summary = loader.refresh_seasons(2024, 2024)

        loader.load_game.assert_not_called()
        loader.reload_game.assert_not_called()
        assert summary["skipped"] == 1

    def test_load_date_range_reload_bypasses_already_loaded_gate(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._game_already_loaded = MagicMock(return_value=True)
        loader.load_game = MagicMock()
        loader.reload_game = MagicMock(return_value={"inserted": 1, "skipped": 0, "flagged": 0})
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            return_value=_schedule_one_final_game(),
        ):
            summary = loader.load_date_range(date(2024, 8, 15), date(2024, 8, 15), reload=True)

        loader.reload_game.assert_called_once()
        loader._game_already_loaded.assert_not_called()
        assert summary["loaded"] == 1

    def test_reload_sweep_contains_a_bad_game_and_continues(self):
        """One malformed feed must not abort a 21.6k-game re-ingest."""
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._game_already_loaded = MagicMock(return_value=True)
        loader.reload_game = MagicMock(
            side_effect=[
                KeyError("strikeZoneTop"),
                {"inserted": 1, "skipped": 0, "flagged": 0},
            ]
        )
        schedule = {
            "dates": [
                {
                    "date": "2024-08-15",
                    "games": [
                        {"gamePk": 745001, "status": {"abstractGameState": "Final"}},
                        {"gamePk": 745002, "status": {"abstractGameState": "Final"}},
                    ],
                }
            ]
        }
        with patch("pipeline.etl.etl_historical_loader._connect", return_value=schedule):
            summary = loader.refresh_seasons(2024, 2024, reload=True)

        assert loader.reload_game.call_count == 2, "sweep must continue past the bad game"
        assert summary["failed"] == 1
        assert summary["loaded"] == 1
        assert summary["attempted"] == 2

    @staticmethod
    def _schedule_n_final_games(n: int) -> dict:
        return {
            "dates": [
                {
                    "date": "2024-08-15",
                    "games": [
                        {"gamePk": 745000 + i, "status": {"abstractGameState": "Final"}}
                        for i in range(n)
                    ],
                }
            ]
        }

    def test_incremental_path_survives_one_bad_game(self):
        """SIM-441: one malformed feed must not abort the nightly run.

        The incremental path used to call load_game bare. Because
        nightly_ingest.sh runs under `set -eu` with this as step 1 of 3, a single
        KeyError meant the profile rebuild and the FAISS tile build never ran —
        and since the poison game genuinely has no pitch rows,
        `_game_already_loaded` reported it unloaded, so the chain re-wedged on
        the same game every night with nothing alerting.
        """
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._game_already_loaded = MagicMock(return_value=False)
        loader.load_game = MagicMock(
            side_effect=[
                KeyError("strikeZoneTop"),
                {"inserted": 290, "skipped": 0, "flagged": 0},
                {"inserted": 275, "skipped": 0, "flagged": 0},
            ]
        )
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            return_value=self._schedule_n_final_games(3),
        ):
            summary = loader.refresh_seasons(2024, 2024)

        assert loader.load_game.call_count == 3, "the run must continue past the bad game"
        assert summary["failed"] == 1
        assert summary["loaded"] == 2
        assert summary["rows_written"] == 565

    def test_incremental_path_aborts_on_a_systemic_failure(self):
        """Containment is bounded: an outage must still surface loudly.

        The distinction that matters — "one bad game" is survivable, "the API is
        down" or "the DB rejects every row" must not quietly produce a run that
        loaded almost nothing.
        """
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._game_already_loaded = MagicMock(return_value=False)
        loader.load_game = MagicMock(side_effect=RuntimeError("MLB API down"))
        with (
            patch(
                "pipeline.etl.etl_historical_loader._connect",
                return_value=self._schedule_n_final_games(20),
            ),
            pytest.raises(RuntimeError, match="consecutive game failures"),
        ):
            loader.refresh_seasons(2024, 2024)

        assert loader.load_game.call_count == _CONSECUTIVE_FAILURE_LIMIT, (
            "must abort at the threshold, not grind through all 20 games"
        )

    def test_a_success_resets_the_failure_counter(self):
        """Scattered bad games across a long sweep must not accumulate to an abort."""
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._game_already_loaded = MagicMock(return_value=False)
        ok = {"inserted": 1, "skipped": 0, "flagged": 0}
        loader.load_game = MagicMock(
            side_effect=[KeyError("a"), ok, KeyError("b"), ok, KeyError("c"), ok]
        )
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            return_value=self._schedule_n_final_games(6),
        ):
            summary = loader.refresh_seasons(2024, 2024)
        assert summary["failed"] == 3
        assert summary["loaded"] == 3


# ---------------------------------------------------------------------------
# Substitution scan
# ---------------------------------------------------------------------------


def _pitch_event(pitch_number: int, outs: int = 0, strikes: int = 1) -> dict:
    return {
        "isPitch": True,
        "index": pitch_number - 1,
        "pitchNumber": pitch_number,
        "details": {
            "code": "S",
            "description": "Swinging Strike",
            "type": {"code": "FF", "description": "4-Seam Fastball"},
            "eventType": "",
        },
        "offense": {
            "batter": {
                "id": 200001,
                "batSide": {"code": "R"},
                "link": "/api/v1/people/200001",
            },
            "first": {},
            "second": {},
            "third": {},
        },
        "defense": {
            "pitcher": {"id": 100001, "pitchHand": {"code": "R"}},
            "catcher": {"id": 200002},
            "first": {"id": 200003},
            "second": {"id": 200004},
            "third": {"id": 200005},
            "shortstop": {"id": 200006},
            "left": {"id": 200007},
            "center": {"id": 200008},
            "right": {"id": 200009},
        },
        "pitchData": {
            "strikeZoneTop": 3.5,
            "strikeZoneBottom": 1.6,
            "startSpeed": 94.0,
            "endSpeed": 86.0,
            "coordinates": {
                "x0": -1.5,
                "y0": 50.0,
                "z0": 6.0,
                "vX0": 5.0,
                "vY0": -135.0,
                "vZ0": -3.0,
                "aX": 5.0,
                "aY": 28.0,
                "aZ": -15.0,
                "pfxX": 0.5,
                "pfxZ": 1.5,
                "pX": 0.2,
                "pZ": 2.5,
                "x": 100,
                "y": 100,
            },
            "breaks": {
                "spinRate": 2400,
                "spinDirection": 200,
                "breakAngle": 22.0,
                "breakLength": 4.0,
                "breakY": 24.0,
                "breakVertical": 12.0,
                "breakVerticalInduced": 15.0,
                "breakHorizontal": 4.0,
            },
            "zone": 5,
            "extension": 6.5,
        },
        "count": {"balls": 0, "strikes": strikes, "outs": outs},
    }


def _sub_event(event_type: str, pos_code: str = "") -> dict:
    evt: dict = {"isPitch": False, "details": {"eventType": event_type}}
    if pos_code:
        evt["position"] = {"code": pos_code}
    return evt


def _feed_with_events(play_events: list[dict]) -> dict:
    pitch_index = [i for i, e in enumerate(play_events) if e["isPitch"]]
    return {
        "gameData": {
            "datetime": {"officialDate": "2024-08-15"},
            "teams": {
                "home": {"id": 112, "abbreviation": "CHC"},
                "away": {"id": 158, "abbreviation": "MIL"},
            },
            "venue": {"id": 17, "name": "Wrigley"},
        },
        "liveData": {
            "plays": {
                "allPlays": [
                    {
                        "atBatIndex": 0,
                        "about": {"inning": 7, "halfInning": "top"},
                        "result": {
                            "description": "Player1 strikes out swinging.",
                            "eventType": "strikeout",
                        },
                        "playEvents": play_events,
                        "pitchIndex": pitch_index,
                        "runners": [],
                    }
                ]
            }
        },
    }


def _parse(feed: dict):
    coaches = {"roster": [{"jobId": "MNGR", "person": {"id": 9999, "fullName": "M"}}]}
    with patch(
        "pipeline.etl.etl_historical_loader._connect",
        side_effect=[feed, coaches, coaches],
    ):
        rows, _ = _fetch_game_pitches(745001, batter_hand_cache={200001: "R"})
    return rows


class TestSubstitutionScan:
    def test_leading_substitution_does_not_raise(self):
        """A play whose FIRST event is a substitution used to raise NameError.

        The rewritten scan referenced an offset variable that is only assigned
        later, by the stolen-base scan — so the very first non-pitch event of the
        first play crashed the whole game load.
        """
        feed = _feed_with_events([_sub_event("defensive_substitution"), _pitch_event(1)])
        rows = _parse(feed)  # must not raise
        assert len(rows) == 1
        assert rows[0]["defensive_sub"] is True

    def test_substitution_flags_only_the_pitch_after_it(self):
        """A mid-PA substitution must not be back-stamped onto earlier pitches."""
        feed = _feed_with_events(
            [
                _pitch_event(1),
                _sub_event("defensive_substitution"),
                _pitch_event(2),
            ]
        )
        rows = _parse(feed)
        assert len(rows) == 2
        assert rows[0]["defensive_sub"] is False, (
            "the pitch thrown BEFORE the substitution must not carry the flag"
        )
        assert rows[1]["defensive_sub"] is True

    def test_substitution_does_not_broadcast_across_the_whole_pa(self):
        """The flag must land once, not on every pitch of the plate appearance."""
        feed = _feed_with_events(
            [
                _sub_event("pitching_substitution"),
                _pitch_event(1),
                _pitch_event(2),
                _pitch_event(3),
            ]
        )
        rows = _parse(feed)
        assert [r["pitcher_sub"] for r in rows] == [True, False, False]

    def test_pinch_hitter_and_pinch_runner_position_codes(self):
        for pos_code, column in (("11", "pinch_hitter"), ("12", "pinch_runner")):
            feed = _feed_with_events(
                [_sub_event("offensive_substitution", pos_code), _pitch_event(1)]
            )
            rows = _parse(feed)
            assert rows[0][column] is True, f"pos_code {pos_code} → {column}"

    def test_substitution_event_without_details_is_tolerated(self):
        feed = _feed_with_events([{"isPitch": False}, _pitch_event(1)])
        rows = _parse(feed)  # must not raise
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Runner thrown out advancing — the isOut nesting level
# ---------------------------------------------------------------------------


def _runner(rid: int, end: str, *, is_out: bool = False, play_index: int = 0) -> dict:
    """A gumbo runner object, shaped like the real MLB feed.

    Nesting matters and is the whole point of these tests: ``movement`` carries
    origin/start/end/outBase/**isOut**/outNumber; ``details`` carries
    event/runner/responsiblePitcher/**rbi**/**earned**/playIndex.
    """
    return {
        "movement": {
            "originBase": None,
            "start": None,
            "end": end,
            "outBase": "3B" if is_out else None,
            "isOut": is_out,
            "outNumber": 2 if is_out else None,
        },
        "details": {
            "event": "Single",
            "runner": {"id": rid},
            "rbi": False,
            "earned": False,
            "playIndex": play_index,
        },
        "credits": [],
    }


class TestRunnerThrownOutAdvancing:
    """`isOut` lives in runner['movement'], not runner['details'].

    Reading it from ``details`` returned the ``False`` default on every row, so
    all three ``runner_*_out_advancing`` columns were constant-FALSE across the
    whole corpus.  Downstream that makes baserunner "attempts" identical to
    "successes", collapsing five success-rate features to exactly 1.0 for every
    player-season — including ``extra_base_success_rate``, weighted 0.500 in the
    baserunner engine.
    """

    @staticmethod
    def _feed_with_runner_out(base_key: str, runner_id: int = 200042) -> dict:
        feed = _feed_with_events([_pitch_event(1)])
        play = feed["liveData"]["plays"]["allPlays"][0]
        # Put the runner on base pre-pitch, then have him thrown out advancing.
        play["playEvents"][0]["offense"][base_key] = {"id": runner_id}
        play["runners"] = [_runner(runner_id, end=None, is_out=True)]
        return feed

    @pytest.mark.parametrize(
        ("base_key", "column"),
        [
            ("first", "runner_1b_out_advancing"),
            ("second", "runner_2b_out_advancing"),
            ("third", "runner_3b_out_advancing"),
        ],
    )
    def test_runner_out_advancing_is_detected(self, base_key, column):
        rows = _parse(self._feed_with_runner_out(base_key))
        assert len(rows) == 1
        assert rows[0][column] is True, (
            f"{column} must be True when movement.isOut is set; reading isOut from "
            "runner['details'] silently returns the False default on every row"
        )

    def test_runner_not_out_leaves_the_flag_false(self):
        feed = _feed_with_events([_pitch_event(1)])
        play = feed["liveData"]["plays"]["allPlays"][0]
        play["playEvents"][0]["offense"]["first"] = {"id": 200042}
        play["runners"] = [_runner(200042, end="2B", is_out=False)]
        rows = _parse(feed)
        assert rows[0]["runner_1b_out_advancing"] is False

    def test_batter_runner_out_is_not_credited_to_a_base(self):
        """The batter being thrown out is not a runner 'out advancing'."""
        feed = _feed_with_events([_pitch_event(1)])
        play = feed["liveData"]["plays"]["allPlays"][0]
        play["runners"] = [_runner(200001, end=None, is_out=True)]  # 200001 == batter
        rows = _parse(feed)
        assert rows[0]["runner_1b_out_advancing"] is False
        assert rows[0]["runner_2b_out_advancing"] is False
        assert rows[0]["runner_3b_out_advancing"] is False


class TestPostPlayBaseState:
    """A runner who was put out must NOT still be standing on a base.

    Before the fix the `isOut` branch set the `*_out_advancing` flag and left
    `post_runner_*` untouched, so a 0-out GIDP with a runner on 1st recorded a
    physically impossible state: that runner still on first, with two outs. The
    id was real, so steal/situation logic downstream then acted on a player who
    was no longer on the field, and he could go on to 'score'.
    """

    def test_runner_thrown_out_is_removed_from_the_post_play_state(self):
        feed = _feed_with_events([_pitch_event(1)])
        play = feed["liveData"]["plays"]["allPlays"][0]
        play["playEvents"][0]["offense"]["second"] = {"id": 200042}
        play["runners"] = [_runner(200042, end=None, is_out=True)]
        rows = _parse(feed)
        assert rows[0]["runner_2b_out_advancing"] is True
        assert rows[0]["post_play_runner_on_second"] == "", (
            "a runner retired on the play must be cleared from post_on_2b"
        )

    def test_surviving_runner_is_retained(self):
        """Only the retired runner is cleared — not the whole base state."""
        feed = _feed_with_events([_pitch_event(1)])
        play = feed["liveData"]["plays"]["allPlays"][0]
        play["playEvents"][0]["offense"]["first"] = {"id": 200042}
        play["playEvents"][0]["offense"]["third"] = {"id": 200099}
        play["runners"] = [_runner(200042, end=None, is_out=True)]
        rows = _parse(feed)
        assert rows[0]["post_play_runner_on_first"] == ""
        assert rows[0]["post_play_runner_on_third"] == 200099

    def test_out_runner_carrying_an_end_base_is_not_reinstated(self):
        """Regression guard for the ordering of the isOut branch.

        The `isOut` branch clears `post_runner_*`, but the `movement.end`
        branches below it run unconditionally afterwards. If the feed ever sets
        BOTH `isOut` and an `end` base on the same runner (gumbo normally puts
        the base in `outBase` and leaves `end` null), the runner would be
        cleared and then immediately re-added to that base.
        """
        feed = _feed_with_events([_pitch_event(1)])
        play = feed["liveData"]["plays"]["allPlays"][0]
        play["playEvents"][0]["offense"]["first"] = {"id": 200042}
        r = _runner(200042, end="2B", is_out=True)
        play["runners"] = [r]
        rows = _parse(feed)
        assert rows[0]["post_play_runner_on_second"] == "", (
            "a runner marked isOut must not end up on a base, even if the feed "
            "also populates movement.end for him"
        )


class TestOutsAccumulator:
    """Outs recorded on NON-pitch events must not be charged to the next pitch.

    `play_event["count"]["outs"]` only advances on pitch events, so a runner
    retired on a trailing pickoff / caught-stealing event left the accumulator
    one low — the next pitch then reported a phantom `outs_on_pitch` and a stale
    pre-pitch `outs`. Downstream, `outs_on_pitch > 0` is what the outfield
    catch-probability model reads as "a ball was caught", and `>= 2` is what the
    double-play logic keys on.
    """

    def test_trailing_non_pitch_out_advances_the_accumulator(self):
        feed = _feed_with_events(
            [
                _pitch_event(1, outs=0),
                _sub_event("caught_stealing_2b"),
                _pitch_event(2, outs=1),
            ]
        )
        play = feed["liveData"]["plays"]["allPlays"][0]
        play["playEvents"][0]["offense"]["first"] = {"id": 200042}
        # The runner is retired on the non-pitch event at index 1.
        play["runners"] = [_runner(200042, end=None, is_out=True, play_index=1)]
        rows = _parse(feed)
        assert len(rows) == 2
        assert rows[1]["outs"] == 1, (
            "the pickoff/CS out happened before pitch 2, so its pre-pitch outs is 1"
        )
        assert rows[1]["outs_on_pitch"] == 0, (
            "pitch 2 itself recorded no out — charging the runner's out to it "
            "makes a clean pitch look like a fielded out"
        )

    def test_out_on_the_pitch_itself_is_not_double_counted(self):
        feed = _feed_with_events([_pitch_event(1, outs=1)])
        play = feed["liveData"]["plays"]["allPlays"][0]
        play["playEvents"][0]["offense"]["first"] = {"id": 200042}
        play["runners"] = [_runner(200042, end=None, is_out=True, play_index=0)]
        rows = _parse(feed)
        assert rows[0]["outs_on_pitch"] == 1


class TestScoreResync:
    """The running score must re-sync from the authoritative play result.

    It used to be a pure accumulator incremented only inside the runner loop, so
    a run scored on a pitch-less play (balk, standalone pickoff error, automatic
    IBB) or on a leading non-pitch event was never added — and every remaining
    pitch row of that game then carried a score one low. That shifts
    `bat_score`/`fld_score`, `sim.pitch_pool.score_diff`, and the leverage bucket
    the manager model reads.
    """

    @staticmethod
    def _two_play_feed() -> dict:
        feed = _feed_with_events([_pitch_event(1)])
        first = feed["liveData"]["plays"]["allPlays"][0]
        second = {
            "atBatIndex": 1,
            "about": {"inning": 7, "halfInning": "top"},
            "result": {"description": "Player2 flies out.", "eventType": "field_out"},
            "playEvents": [_pitch_event(1)],
            "pitchIndex": [0],
            "runners": [],
        }
        feed["liveData"]["plays"]["allPlays"].append(second)
        return feed, first, second

    def test_score_resyncs_from_play_result(self):
        feed, first, _second = self._two_play_feed()
        # The parser saw no scoring runner, but the play result says 2-0 —
        # e.g. a run that came in on a pitch-less event the runner window missed.
        first["result"]["homeScore"] = 0
        first["result"]["awayScore"] = 2
        rows = _parse(feed)
        assert len(rows) == 2
        assert rows[1]["away_score"] == 2, (
            "the second play must start from the authoritative post-play score, "
            "not from the parser's own accumulator"
        )

    def test_missing_score_keys_fall_back_to_the_accumulator(self):
        feed, _first, _second = self._two_play_feed()
        rows = _parse(feed)  # no homeScore/awayScore keys at all
        assert rows[1]["away_score"] == 0
        assert rows[1]["home_score"] == 0


# ---------------------------------------------------------------------------
# Validator — in-play pitch codes
# ---------------------------------------------------------------------------


_CLEAN_IN_PLAY_ROW = {
    "game_pk": 745001,
    "at_bat_number": 1,
    "pitch_number": 1,
    "game_date": "2024-08-15",
    "venue_id": 17,
    "pitcher": 1,
    "p_throws": "R",
    "batter": 2,
    "stand": "R",
    "bat_hand": "R",
    "inning": 1,
    "inning_topbot": "Top",
    "balls": 0,
    "strikes": 0,
    "outs": 0,
    "home_score": 0,
    "away_score": 0,
}


class TestInPlayValidation:
    @pytest.mark.parametrize("code", ["D", "E", "X"])
    def test_all_three_in_play_codes_require_batted_ball_data(self, code):
        """'X' is only one of three in-play codes — D and E are in play too.

        Gating on 'X' alone made the quality flag asymmetric: an untracked ball
        that was an OUT got flagged (and excluded everywhere downstream) while an
        identical untracked HIT was retained.
        """
        row = dict(
            _CLEAN_IN_PLAY_ROW,
            type=code,
            launch_speed=None,
            launch_angle=None,
            bb_type=None,
        )
        result = _validate_row(row)
        assert not result.hard_errors
        assert len(result.warnings) == 3, f"code {code!r} → {result.warnings}"
        assert row["data_quality_flag"] is True

    @pytest.mark.parametrize("code", ["D", "E", "X"])
    def test_complete_in_play_row_is_not_flagged(self, code):
        row = dict(
            _CLEAN_IN_PLAY_ROW,
            type=code,
            launch_speed=95.0,
            launch_angle=12.0,
            bb_type="line_drive",
        )
        result = _validate_row(row)
        assert not result.warnings

    def test_non_in_play_code_is_not_checked(self):
        row = dict(
            _CLEAN_IN_PLAY_ROW,
            type="S",
            launch_speed=None,
            launch_angle=None,
            bb_type=None,
        )
        assert not _validate_row(row).warnings


# ---------------------------------------------------------------------------
# _ensure_game — the conflict clause must cover every column
# ---------------------------------------------------------------------------


_GAME_COLUMNS = [
    "game_pk",
    "season",
    "game_date",
    "game_type",
    "status",
    "venue_id",
    "home_team_id",
    "away_team_id",
    "home_manager_id",
    "away_manager_id",
    "home_score_final",
    "away_score_final",
    "innings_played",
    "home_hits",
    "away_hits",
    "home_errors",
    "away_errors",
    "inning_scores",
    "winning_pitcher_id",
    "losing_pitcher_id",
    "save_pitcher_id",
    "weather_temp",
    "weather_condition",
    "wind_speed",
    "wind_direction",
]

# The loader is the authority for these: it reads gameData.datetime.officialDate,
# whereas the live pipeline writes the UTC `gameDate`. A wrong-but-non-NULL value
# must be overwritten outright, so COALESCE is the WRONG policy for them.
_AUTHORITATIVE = {"season", "game_date", "game_type", "status"}


class TestEnsureGameUpsertCoversEveryColumn:
    """A re-fetch must be able to REPAIR raw.games, not just its score.

    The clause used to set 6 of 25 columns. Everything else was computed,
    bound into the INSERT and then discarded on conflict — so innings, hits,
    errors, inning_scores, weather, venue and manager ids could never be
    corrected by any code path, including a full reload sweep.
    """

    @staticmethod
    def _upsert_sql() -> str:
        import inspect

        src = inspect.getsource(HistoricalDataLoader._ensure_game)
        assert "ON CONFLICT (game_pk) DO UPDATE SET" in src, "conflict clause moved"
        clause = src[src.index("ON CONFLICT (game_pk) DO UPDATE SET") :]
        return clause[: clause.index('"""')]

    def test_insert_column_list_matches_the_expected_25(self):
        import inspect

        src = inspect.getsource(HistoricalDataLoader._ensure_game)
        header = src[src.index("INSERT INTO raw.games (") : src.index(") VALUES (")]
        named = [c.strip() for c in header.split("(", 1)[1].replace("\n", " ").split(",")]
        named = [c for c in named if c]
        assert named == _GAME_COLUMNS, f"INSERT column list drifted: {named}"

    def test_placeholder_count_matches_the_column_count(self):
        import inspect

        src = inspect.getsource(HistoricalDataLoader._ensure_game)
        values = src[src.index(") VALUES (") : src.index("-- SIM-440")]
        assert values.count("%s") == len(_GAME_COLUMNS), (
            f"{values.count('%s')} placeholders for {len(_GAME_COLUMNS)} columns"
        )

    @pytest.mark.parametrize("column", [c for c in _GAME_COLUMNS if c != "game_pk"])
    def test_every_non_key_column_is_refreshed_on_conflict(self, column):
        clause = self._upsert_sql()
        assert re.search(rf"^\s*{column}\s*=", clause, re.M), (
            f"`{column}` is INSERTed but never refreshed on conflict — a re-fetch "
            "silently discards it, so it can never be repaired"
        )

    @pytest.mark.parametrize("column", sorted(_AUTHORITATIVE))
    def test_authoritative_columns_overwrite_unconditionally(self, column):
        """COALESCE would preserve a wrong-but-non-NULL value forever.

        The live pipeline writes `game_date` from the UTC `gameDate`, so a
        CT/MT/PT night game it created carries the NEXT calendar day. This
        loader's `officialDate` must win outright.
        """
        clause = self._upsert_sql()
        line = next(ln for ln in clause.splitlines() if re.match(rf"\s*{column}\s*=", ln))
        assert "COALESCE" not in line, (
            f"`{column}` must overwrite unconditionally — this loader is the authority "
            f"for it, and COALESCE cannot repair a wrong-but-non-NULL stored value. Got: {line.strip()}"
        )

    @pytest.mark.parametrize(
        "column",
        [c for c in _GAME_COLUMNS if c not in _AUTHORITATIVE and c != "game_pk"],
    )
    def test_enrichment_columns_never_blank_out_stored_data(self, column):
        """A scheduled/in-progress payload carries NULLs for these."""
        clause = self._upsert_sql()
        line = next(ln for ln in clause.splitlines() if re.match(rf"\s*{column}\s*=", ln))
        assert f"COALESCE(EXCLUDED.{column}" in line, (
            f"`{column}` must be COALESCEd — a thin payload would otherwise blank out "
            f"a good stored value. Got: {line.strip()}"
        )

    def test_updated_at_advances(self):
        assert "updated_at" in self._upsert_sql()


# ---------------------------------------------------------------------------
# Validator <-> DB trigger lock-step
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[2]
_PG_SCHEMA = _REPO_ROOT / "db" / "schemas" / "01_postgres_schema.sql"
_MIG_DIR = _REPO_ROOT / "db" / "migrations" / "versions"


def _trigger_bounds(sql: str) -> dict[str, float]:
    """Parse the plausibility bounds out of a flag_pitch_quality() body."""
    body = re.search(
        r"CREATE OR REPLACE FUNCTION raw\.flag_pitch_quality\(\).*?\$\$(.*?)\$\$",
        sql,
        re.DOTALL,
    )
    assert body, "flag_pitch_quality() not found"
    text = body.group(1)
    out: dict[str, float] = {}
    for key, pattern in (
        ("release_hi", r"release_speed\s*>\s*([\d.]+)"),
        ("release_lo", r"release_speed\s*<\s*([\d.]+)"),
        ("launch_hi", r"launch_speed\s*>\s*([\d.]+)"),
    ):
        m = re.search(pattern, text)
        assert m, f"could not parse {key} from the trigger body"
        out[key] = float(m.group(1))
    return out


class TestValidatorTriggerLockStep:
    """The Python validator and the DB trigger must state the SAME band.

    Both write ``data_quality_flag`` and the trigger only ever SETS it — it never
    clears it — so whichever layer is stricter is silently authoritative.  That
    asymmetry is how SIM-087's widened 50 mph floor sat unreachable for years
    behind a Python validator that still warned below 60.  Without this test the
    two can drift apart again with a fully green suite.
    """

    @staticmethod
    def _python_bounds() -> dict[str, float]:
        from pipeline.etl.etl_historical_loader import _validate_row

        src = inspect.getsource(_validate_row)
        rs = re.search(r"not \(([\d.]+) <= rs <= ([\d.]+)\)", src)
        ls = re.search(r"ls > ([\d.]+)", src)
        assert rs and ls, f"could not parse the validator bounds from:\n{src}"
        return {
            "release_lo": float(rs.group(1)),
            "release_hi": float(rs.group(2)),
            "launch_hi": float(ls.group(1)),
        }

    def test_canonical_schema_matches_the_validator(self):
        assert _trigger_bounds(_PG_SCHEMA.read_text(encoding="utf-8")) == self._python_bounds()

    def test_latest_trigger_migration_matches_the_canonical_schema(self):
        mig = _MIG_DIR / "0016_sim440_pitch_quality_velocity_band.py"
        assert mig.exists(), "SIM-440 trigger migration is missing"
        # The migration's upgrade body is the FIRST function definition in the file.
        assert _trigger_bounds(mig.read_text(encoding="utf-8")) == self._python_bounds()

    def test_launch_speed_bound_is_inside_the_column_check(self):
        """A bound at or above the CHECK ceiling is dead code.

        raw.pitches.launch_speed CHECK (... BETWEEN 0 AND 130) rejects anything
        over 130 outright, so a warning threshold of `> 130` can never fire and
        the (previous) soft-flag band above it is simply gone.
        """
        schema = _PG_SCHEMA.read_text(encoding="utf-8")
        m = re.search(r"launch_speed\s+BETWEEN\s+([\d.]+)\s+AND\s+([\d.]+)", schema)
        assert m, "launch_speed CHECK constraint not found"
        check_hi = float(m.group(2))
        assert self._python_bounds()["launch_hi"] < check_hi, (
            f"launch_speed warning threshold ({self._python_bounds()['launch_hi']}) is at or "
            f"above the column CHECK ceiling ({check_hi}), so it is unreachable dead code and "
            "the band below it is no longer flagged at all"
        )


class TestAlembicHistoryIsLinear:
    def test_exactly_one_head(self):
        """Two children of the same parent make `alembic upgrade head` refuse to run."""
        revs: dict[str, str | None] = {}
        for path in _MIG_DIR.glob("[0-9]*.py"):
            text = path.read_text(encoding="utf-8")
            rev = re.search(r'^revision\s*=\s*["\']([^"\']+)', text, re.M)
            down = re.search(r'^down_revision\s*=\s*(?:["\']([^"\']+)|None)', text, re.M)
            assert rev, f"{path.name}: no revision id"
            assert rev.group(1) not in revs, f"duplicate revision id {rev.group(1)}"
            revs[rev.group(1)] = down.group(1) if down and down.group(1) else None

        parents = [d for d in revs.values() if d is not None]
        heads = [r for r in revs if r not in parents]
        assert len(heads) == 1, f"expected exactly one alembic head, found {sorted(heads)}"

        duplicated = {p for p in parents if parents.count(p) > 1}
        assert not duplicated, (
            f"revisions with more than one child (branched history): {duplicated}"
        )


# ---------------------------------------------------------------------------
# Spray angle handedness key
# ---------------------------------------------------------------------------


class TestPullRelativeSprayAngle:
    """The sign key must be the resolved side, which is never 'S'.

    Reads the PRODUCTION method source rather than a copied SQL literal, so
    reverting the query cannot leave this test green.
    """

    @staticmethod
    def _outcome_pool_sql() -> str:
        from pipeline.batch.player_profile_computor import PlayerProfileComputor

        return inspect.getsource(PlayerProfileComputor._build_outcome_pool)

    @classmethod
    def _spray_case_body(cls) -> str:
        """The CASE expression that actually feeds the alias, comments excluded."""
        src = cls._outcome_pool_sql()
        alias = "AS pull_relative_spray_angle"
        assert alias in src, "expression moved or was renamed — retarget this test"
        window = src[: src.index(alias)]
        body = window[window.rindex("CASE") :]
        # Drop SQL comment lines so a comment mentioning bat_hand cannot decide
        # the assertions below.
        return "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("--"))

    def test_keys_on_stand_not_bat_hand(self):
        case_body = self._spray_case_body()

        assert "rp.stand = 'R'" in case_body and "rp.stand = 'L'" in case_body, (
            "pull_relative_spray_angle must key on `stand` (resolved per-PA side, "
            f"never 'S'), got: {case_body}"
        )
        assert "rp.bat_hand" not in case_body, (
            "`bat_hand` is the roster-DECLARED side and is 'S' for 10.4-13.3% of "
            "rows every season; keying on it NULLs every switch-hitter batted ball, "
            "which build_battedball_pool_artifact then filters out entirely."
        )

    def test_sign_convention_is_preserved(self):
        """Positive = pull side: unnegated for R, negated for L."""
        case_body = self._spray_case_body()

        r_line = next(ln for ln in case_body.splitlines() if "rp.stand = 'R'" in ln)
        l_line = next(ln for ln in case_body.splitlines() if "rp.stand = 'L'" in ln)
        assert "-rp.spray_angle" not in r_line
        assert "-rp.spray_angle" in l_line
