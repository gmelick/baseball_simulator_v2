"""
SIM-441 — ETL hardening batch.
==============================

Covers the fixes that are NOT about the reload path (those live in
``test_sim440_reload_game.py``):

  1  per-game exception isolation on the incremental path + circuit breaker
  2  ``load_date_range`` window chunking and the Final gate
  3  ``_ensure_players`` — never fabricate handedness
  4  manager fallback + closeout probe + ``season_end`` semantics
  5  ``raw.etl_errors`` idempotency, the transactional ledger, DB-CHECK mirroring,
     and the game-level ingest outcome
  6  ``_ensure_venue`` retry/parse hardening
  7  wind-string parsing
  8  spray angle behind home plate, comma stripping, fielding-credit overflow,
     ``GAME_TYPES``, HTTP hygiene

Mocking style mirrors tests/unit/test_etl_historical_loader.py.
"""

from __future__ import annotations

import inspect
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.etl.etl_historical_loader import (
    _MAX_SCHEDULE_WINDOW_DAYS,
    GAME_TYPES,
    HistoricalDataLoader,
    SavantScrapeError,
    _date_chunks,
    _fetch_game_pitches,
    _parse_savant_embedded_json,
    _parse_wind,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _feed_with_one_pitch() -> dict:
    """Smallest feed/live payload that drives one pitch through the parser."""
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
                        "about": {"inning": 1, "halfInning": "top"},
                        "result": {"description": "Groundout.", "eventType": "field_out"},
                        "playEvents": [
                            {
                                "isPitch": True,
                                "index": 0,
                                "pitchNumber": 1,
                                "details": {
                                    "code": "X",
                                    "description": "In play, out(s)",
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
                                    "coordinates": {},
                                    "breaks": {},
                                    "zone": 5,
                                    "extension": 6.5,
                                },
                                "count": {"balls": 0, "strikes": 0, "outs": 0},
                            }
                        ],
                        "pitchIndex": [0],
                        "runners": [],
                    }
                ]
            }
        },
    }


def _parse_feed(feed: dict) -> list[dict]:
    coaches = {"roster": [{"jobId": "MNGR", "person": {"id": 9999, "fullName": "M"}}]}
    with patch(
        "pipeline.etl.etl_historical_loader._connect",
        side_effect=[feed, coaches, coaches],
    ):
        rows, _ = _fetch_game_pitches(745001, batter_hand_cache={200001: "R"})
    return rows


# ---------------------------------------------------------------------------
# 7. Wind parsing
# ---------------------------------------------------------------------------


class TestParseWind:
    """The feed exposes ONE formatted string under gameData.weather.wind.

    The loader read ``weather["speed"]`` / ``weather["direction"]`` — keys the
    MLB feed has never provided — so both columns were NULL on every game ever
    loaded. (Both variables were also bound to the same dict, which is how the
    mistake survived review.)
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("8 mph, L To R", (8, "L To R")),
            ("12 mph, Out To CF", (12, "Out To CF")),
            ("3 mph, In From LF", (3, "In From LF")),
            ("15 mph, R To L", (15, "R To L")),
            ("7.5 mph, Varies", (8, "Varies")),  # rounded, not truncated
        ],
    )
    def test_parses_speed_and_direction(self, raw, expected):
        assert _parse_wind(raw) == expected

    def test_calm_is_a_real_observation_not_missing_data(self):
        assert _parse_wind("Calm") == (0, "Calm")

    @pytest.mark.parametrize("raw", [None, "", "   ", 42, {}])
    def test_missing_returns_none_none(self, raw):
        assert _parse_wind(raw) == (None, None)

    def test_unrecognised_shape_keeps_the_direction_text(self):
        """Don't invent a speed, but don't discard the only wind info either."""
        speed, direction = _parse_wind("Windy from the north")
        assert speed is None
        assert direction == "Windy from the north"

    def test_direction_is_truncated_to_the_column_width(self):
        _speed, direction = _parse_wind("8 mph, " + "X" * 200)
        assert len(direction) <= 50


# ---------------------------------------------------------------------------
# 2. Schedule window chunking
# ---------------------------------------------------------------------------


class TestDateChunks:
    """MLB's schedule endpoint silently clamps >365-day windows to HTTP 200.

    A multi-season load_date_range therefore loaded about one season and exited
    0 — the module's own header recipe was broken by this.
    """

    def test_short_window_is_one_chunk(self):
        chunks = _date_chunks(date(2024, 4, 1), date(2024, 9, 30))
        assert chunks == [(date(2024, 4, 1), date(2024, 9, 30))]

    def test_every_chunk_is_within_the_endpoint_limit(self):
        chunks = _date_chunks(date(2017, 1, 1), date(2026, 12, 31))
        assert len(chunks) > 1
        for start, stop in chunks:
            assert (stop - start).days <= _MAX_SCHEDULE_WINDOW_DAYS

    def test_chunks_are_contiguous_and_cover_the_whole_range(self):
        start, end = date(2017, 1, 1), date(2026, 12, 31)
        chunks = _date_chunks(start, end)
        assert chunks[0][0] == start
        assert chunks[-1][1] == end
        for (_, prev_stop), (next_start, _) in zip(chunks[:-1], chunks[1:], strict=True):
            assert (next_start - prev_stop).days == 1, "no gap and no overlap"

    def test_single_day(self):
        d = date(2024, 8, 15)
        assert _date_chunks(d, d) == [(d, d)]

    def test_backwards_range_is_rejected(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        with pytest.raises(ValueError, match="precedes"):
            loader.load_date_range(date(2024, 9, 1), date(2024, 8, 1))


class TestLoadDateRangeChunking:
    def test_a_multi_year_window_issues_multiple_schedule_calls(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._dispatch_game = MagicMock()
        with patch(
            "pipeline.etl.etl_historical_loader._connect", return_value={"dates": []}
        ) as conn:
            loader.load_date_range(date(2017, 1, 1), date(2026, 12, 31))
        assert conn.call_count > 1, (
            "a 10-year window must be sliced; one call would be silently clamped "
            "to startDate + 365 with no error"
        )

    def test_in_progress_games_are_gated_out(self):
        """load_date_range lacked the Final gate refresh_seasons has.

        An in-progress game ingested half-played was then skipped forever by
        _game_already_loaded.
        """
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._dispatch_game = MagicMock()
        schedule = {
            "dates": [
                {
                    "date": "2024-08-15",
                    "games": [
                        {"gamePk": 1, "status": {"abstractGameState": "Live"}},
                        {"gamePk": 2, "status": {"abstractGameState": "Preview"}},
                        {"gamePk": 3, "status": {"abstractGameState": "Final"}},
                    ],
                }
            ]
        }
        with patch("pipeline.etl.etl_historical_loader._connect", return_value=schedule):
            loader.load_date_range(date(2024, 8, 15), date(2024, 8, 15))
        assert loader._dispatch_game.call_count == 1
        assert loader._dispatch_game.call_args.args[0] == 3


# ---------------------------------------------------------------------------
# 6. Savant scrape hardening
# ---------------------------------------------------------------------------


class TestSavantParsing:
    """Venue dimensions are scraped from an inline `var data = …` literal.

    Unguarded `.find()` slicing would mis-slice silently on a page-shape change
    and either raise an opaque JSONDecodeError deep in the stack or parse a
    DIFFERENT object and write plausible-but-wrong stadium dimensions.
    """

    def test_parses_a_well_formed_array(self):
        html = 'junk var data = [{"venue_id": 17, "lf": 355}]; more junk'
        out = _parse_savant_embedded_json(html, "var data = [", "}];", 2)
        assert out == [{"venue_id": 17, "lf": 355}]

    def test_missing_start_marker_fails_loudly(self):
        with pytest.raises(SavantScrapeError, match="start marker"):
            _parse_savant_embedded_json("<html>nothing here</html>", "var data = [", "}];", 2)

    def test_missing_end_marker_fails_loudly(self):
        with pytest.raises(SavantScrapeError, match="end marker"):
            _parse_savant_embedded_json('var data = [{"a": 1}', "var data = [", "}];", 2)

    def test_malformed_json_fails_loudly(self):
        with pytest.raises(SavantScrapeError, match="not valid JSON"):
            _parse_savant_embedded_json("var data = [not json}];", "var data = [", "}];", 2)

    def test_ensure_venue_no_longer_loops_without_breaking(self):
        src = inspect.getsource(HistoricalDataLoader._ensure_venue)
        assert "for attempt in range(1, MAX_API_RETRIES" not in src, (
            "the hand-rolled retry loops (which never broke on success, and "
            "discarded a good response if a later attempt failed) must be gone"
        )
        assert "if resp is None:\n            raise" not in src, (
            "the bare `raise` outside any except block must be gone"
        )


# ---------------------------------------------------------------------------
# 5. Ledger idempotency, DB-CHECK mirroring, ingest outcome
# ---------------------------------------------------------------------------


def _valid_row(**over):
    row = {
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
    row.update(over)
    return row


class TestIngestOutcome:
    """`_game_already_loaded` keyed only on pitch rows existing.

    That cannot distinguish "never loaded" from "loaded and legitimately produced
    zero pitch rows" — so a game whose every row hard-errors was re-fetched and
    re-processed every night, forever, appending a duplicate error set each time.
    """

    def test_zero_pitch_game_is_recorded_as_empty(self):
        cur = MagicMock()
        HistoricalDataLoader._write_ingest_outcome(
            cur, 745001, 2024, pitch_rows=0, skipped_rows=290
        )
        params = cur.execute.call_args.args[1]
        assert params[2] == "empty"

    def test_loaded_game_is_recorded_as_loaded(self):
        cur = MagicMock()
        HistoricalDataLoader._write_ingest_outcome(
            cur, 745001, 2024, pitch_rows=290, skipped_rows=0
        )
        assert cur.execute.call_args.args[1][2] == "loaded"

    def test_outcome_upsert_counts_attempts(self):
        cur = MagicMock()
        HistoricalDataLoader._write_ingest_outcome(cur, 1, 2024, pitch_rows=1, skipped_rows=0)
        sql = cur.execute.call_args.args[0]
        assert "attempts     = raw.etl_game_ingest.attempts + 1" in sql

    def test_game_already_loaded_consults_the_outcome_table(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        cur = MagicMock()
        cur.fetchone.side_effect = [None, (1,)]  # no pitch rows, but an 'empty' record
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        ctx = MagicMock()
        ctx.__enter__.return_value = conn
        ctx.__exit__.return_value = False
        loader._get_conn = MagicMock(return_value=ctx)

        assert loader._game_already_loaded(745001) is True
        assert "etl_game_ingest" in cur.execute.call_args.args[0]


# ---------------------------------------------------------------------------
# 8. Parser hygiene: spray angle, commas, credit overflow, game types
# ---------------------------------------------------------------------------


class TestParserHygiene:
    def test_game_types_excludes_the_obsolete_championship_code(self):
        """'C' is not in the raw.games CHECK, so one such game raised a
        CheckViolation that — before per-game isolation — killed the whole run."""
        assert "C" not in GAME_TYPES
        schema = (_REPO_ROOT / "db" / "schemas" / "01_postgres_schema.sql").read_text(
            encoding="utf-8"
        )
        allowed = schema.split("CHECK (game_type IN (")[1].split(")")[0]
        for gt in GAME_TYPES:
            assert f"'{gt}'" in allowed, f"GAME_TYPES has {gt!r}, which the DB CHECK rejects"

    def test_spray_angle_is_skipped_behind_home_plate(self):
        """coord_y > 198.27 is behind the plate (a foul pop into the backstop).

        `arctan` of a negative denominator mirrors those into the fair-field
        quadrant, so the row reported a plausible pull or oppo angle. `arctan2`
        is not the fix either — the true angle is outside +/-90 deg and the
        pull/oppo thresholds cannot represent it.
        """
        src = inspect.getsource(
            __import__(
                "pipeline.etl.etl_historical_loader", fromlist=["_fetch_game_pitches"]
            )._fetch_game_pitches
        )
        assert "if coord_y > 198.27:" in src
        idx = src.index("if coord_y > 198.27:")
        assert 'spray_angle = ""' in src[idx : idx + 200]

    def test_comma_stripping_is_gone(self):
        """A CSV-lineage leftover: both destinations are bound parameters, so
        stripping commas was pure lossy mutation on ~6.55M rows."""
        src = (_REPO_ROOT / "pipeline" / "etl" / "etl_historical_loader.py").read_text(
            encoding="utf-8"
        )
        code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
        assert '.replace(",", "")' not in code

    @pytest.mark.parametrize(
        ("n_assists", "expected"),
        [(1, False), (5, False), (6, True), (9, True)],
    )
    def test_field_assist_overflow_flag_tracks_dropped_credits(self, n_assists, expected):
        """Behavioural, not a source grep.

        raw.pitches holds field_assist_1..5. Credits past the 5th used to be
        written to keys _build_row_dict never reads, so they vanished with no
        warning and no ledger row. The flag makes the overflow visible so a
        consumer can exclude or measure those plays (a multi-runner rundown)
        instead of silently under-counting assists.
        """
        feed = _feed_with_one_pitch()
        play = feed["liveData"]["plays"]["allPlays"][0]
        play["runners"] = [
            {
                "movement": {"end": None, "isOut": False},
                "details": {"runner": {"id": 200042}, "playIndex": 0},
                "credits": [
                    {"credit": "f_assist", "player": {"id": 200100 + i}} for i in range(n_assists)
                ],
            }
        ]
        rows = _parse_feed(feed)
        assert rows[0]["field_assist_6_plus"] is expected

    def test_overflow_flag_reaches_the_pitch_insert(self):
        src = (_REPO_ROOT / "pipeline" / "etl" / "etl_historical_loader.py").read_text(
            encoding="utf-8"
        )
        assert "%(field_assist_6_plus)s" in src, "must be bound into the pitch INSERT"

    def test_overflow_column_exists_in_schema_and_migration(self):
        schema = (_REPO_ROOT / "db" / "schemas" / "01_postgres_schema.sql").read_text(
            encoding="utf-8"
        )
        assert "field_assist_6_plus" in schema
        mig = (
            _REPO_ROOT / "db" / "migrations" / "versions" / "0017_sim441_etl_hardening.py"
        ).read_text(encoding="utf-8")
        assert "field_assist_6_plus" in mig
        assert "DROP COLUMN IF EXISTS active" in mig
        assert "uq_etl_errors_natural_key" in mig
        assert "etl_game_ingest" in mig

    def test_players_active_column_is_gone(self):
        schema = (_REPO_ROOT / "db" / "schemas" / "01_postgres_schema.sql").read_text(
            encoding="utf-8"
        )
        players = schema.split("CREATE TABLE raw.players (")[1].split(");")[0]
        code = "\n".join(ln for ln in players.splitlines() if not ln.strip().startswith("--"))
        assert "active" not in code
        assert "idx_players_active" not in schema


# ---------------------------------------------------------------------------
# 4. Manager handling
# ---------------------------------------------------------------------------


class TestManagerHandling:
    def test_no_coach_match_does_not_fabricate_a_manager(self):
        """It used to install coaches[0] — typically a pitching coach — with no
        log line, and the closeout branch then marked the REAL manager departed."""
        src = inspect.getsource(
            __import__(
                "pipeline.etl.etl_historical_loader", fromlist=["_fetch_game_pitches"]
            )._fetch_game_pitches
        )
        assert "temp_manager_id" not in src
        assert "temp_manager_name" not in src

    @staticmethod
    def _closeout_probe_sql() -> str:
        """The closeout SELECT, with comment lines stripped.

        Stripping comments is load-bearing: the block above this query explains
        the old bug and therefore CONTAINS the strings "season_end IS NULL",
        "ORDER BY" and "LIMIT". An earlier version of this test sliced from the
        first `SELECT manager_id` (the existence check at the top of the method)
        and was satisfied by that prose — it passed with the predicate deleted.
        """
        src = inspect.getsource(HistoricalDataLoader._ensure_managers)
        code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
        # The closeout probe is the query immediately before `m = cur.fetchone()`.
        head = code[: code.index("m = cur.fetchone()")]
        return head[head.rindex("SELECT manager_id") :]

    def test_closeout_probe_only_closes_an_active_stint(self):
        probe = self._closeout_probe_sql()
        assert "season_end IS NULL" in probe, (
            "only a currently-active stint may be closed; without this the probe "
            f"clobbers a correct handoff date on an already-closed row. Got: {probe}"
        )

    def test_closeout_probe_is_deterministic(self):
        probe = self._closeout_probe_sql()
        assert "ORDER BY" in probe and "LIMIT 1" in probe, (
            "a bare fetchone() over an unordered set closed an arbitrary manager — "
            f"with three managers in one season that is a coin flip. Got: {probe}"
        )

    def test_upsert_reactivates_a_returning_manager(self):
        """season_end IS NULL means 'currently the manager' (idx_managers_active).

        Seeing him in the dugout again is the evidence he is active — that is how
        a return from suspension or missed time is recorded.
        """
        import re as _re

        src = inspect.getsource(HistoricalDataLoader._ensure_managers)
        # Each ON CONFLICT clause ends at the closing triple-quote of its SQL.
        clauses = [c.split('"""')[0] for c in src.split("DO UPDATE SET")[1:]]
        assert len(clauses) == 2, f"expected both manager upserts, got {len(clauses)}"
        for clause in clauses:
            assert _re.search(r"season_end\s*=\s*NULL", clause), (
                f"every manager upsert must reactivate on conflict; got: {clause}"
            )
        # Scope to the SQL clauses — the method docstring quotes the old broken
        # expression to explain it, and must not satisfy this assertion.
        for clause in clauses:
            assert "EXCLUDED.season_end" not in clause, (
                "season_end is not in the INSERT column list, so "
                "EXCLUDED.season_end wrote NULL by accident while silently "
                f"failing to record season_start. Got: {clause}"
            )


# ---------------------------------------------------------------------------
# 8. HTTP hygiene
# ---------------------------------------------------------------------------


class TestAuditTrailSurvivesATotallyFailingGame:
    """The audit trail must exist for the games that most need it.

    `_batch_insert` derived the game pk ONLY by inferring it from `rows`. When
    every row of a game hard-errors, `rows` is empty, the inference yields None,
    and BOTH the error-ledger and ingest-outcome writes were gated off — so the
    one game you would actually want to investigate produced no record at all.
    That was a regression (the old `_log_etl_errors` call passed game_pk
    outright) and it made `outcome='empty'` unreachable on the non-reload path,
    defeating the purpose of raw.etl_game_ingest.
    """

    @staticmethod
    def _loader():
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        cur = MagicMock()
        cur.fetchone.return_value = (0,)
        cur.rowcount = 0
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        ctx = MagicMock()
        ctx.__enter__.return_value = conn
        ctx.__exit__.return_value = False
        loader._get_conn = MagicMock(return_value=ctx)
        return loader, cur

    def test_zero_insertable_rows_still_writes_the_ledger_and_outcome(self):
        loader, cur = self._loader()
        loader._write_error_ledger = MagicMock()
        loader._write_ingest_outcome = MagicMock()

        with patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch"):
            loader._batch_insert(
                [],
                error_rows=[(1, 1, ["NULL primary key column: pitch_number"])],
                season=2024,
                game_pk=745001,
            )

        loader._write_error_ledger.assert_called_once()
        assert loader._write_error_ledger.call_args.args[1] == 745001
        loader._write_ingest_outcome.assert_called_once()
        assert loader._write_ingest_outcome.call_args.args[1] == 745001
        assert loader._write_ingest_outcome.call_args.kwargs["pitch_rows"] == 0

    def test_process_and_insert_states_the_game_pk_outright(self):
        """It must not rely on inferring the pk from a possibly-empty row list."""
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._batch_insert = MagicMock(return_value=0)
        loader._log_freshness = MagicMock()
        bad = {"game_pk": 745001, "at_bat_number": 1}  # no pitch_number -> hard error
        loader._process_and_insert(745001, 2024, [bad])
        assert loader._batch_insert.call_args.kwargs["game_pk"] == 745001

    def test_an_empty_game_is_recorded_so_it_stops_re_running_nightly(self):
        loader, cur = self._loader()
        with patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch"):
            loader._batch_insert([], error_rows=[(1, 1, ["boom"])], season=2024, game_pk=745001)
        outcomes = [
            c.args[1][2]
            for c in cur.execute.call_args_list
            if c.args and "etl_game_ingest" in c.args[0]
        ]
        assert outcomes == ["empty"]


class TestPitchlessFirstPlay:
    """A play with no pitch events must not crash the parser.

    `home_score_after`/`away_score_after` are assigned inside the pitch branch
    but read at play scope by the score re-sync — and Python evaluates a
    `.get(key, default)` default EAGERLY, so the read happens even when the key
    is present. A pitch-less FIRST play raised NameError.
    """

    def test_a_play_with_no_pitches_does_not_raise(self):
        feed = _feed_with_one_pitch()
        plays = feed["liveData"]["plays"]["allPlays"]
        pitchless = {
            "atBatIndex": 0,
            "about": {"inning": 1, "halfInning": "top"},
            "result": {"description": "Balk.", "eventType": "balk"},
            "playEvents": [{"isPitch": False, "details": {"eventType": "balk"}}],
            "pitchIndex": [],
            "runners": [],
        }
        plays.insert(0, pitchless)
        plays[1]["atBatIndex"] = 1
        rows = _parse_feed(feed)  # must not raise
        assert len(rows) == 1, "the pitch-less play contributes no rows, the real pitch does"


class TestManagerIdsAreNullable:
    """Losing a whole game's pitches over an unidentifiable manager is worse
    than a NULL in two denormalised convenience columns.

    With the coaches[0] fallback removed, an unusual roster leaves the id empty
    -> to_int('') -> None. raw.pitches had these NOT NULL while raw.games (the
    same data) had them nullable, so the empty id turned a survivable gap into a
    NotNullViolation that rolled back the entire game's insert.
    """

    def test_raw_pitches_manager_ids_are_nullable(self):
        schema = (_REPO_ROOT / "db" / "schemas" / "01_postgres_schema.sql").read_text(
            encoding="utf-8"
        )
        pitches = schema.split("CREATE TABLE raw.pitches (")[1].split("\n);")[0]
        for col in ("home_manager_id", "away_manager_id"):
            line = next(ln for ln in pitches.splitlines() if ln.strip().startswith(col))
            assert "NOT NULL" not in line, (
                f"raw.pitches.{col} must be nullable to match raw.games — otherwise an "
                f"unidentifiable manager rolls back the whole game. Got: {line.strip()}"
            )

    def test_migration_0017_drops_the_not_null(self):
        mig = (
            _REPO_ROOT / "db" / "migrations" / "versions" / "0017_sim441_etl_hardening.py"
        ).read_text(encoding="utf-8")
        assert "home_manager_id DROP NOT NULL" in mig
        assert "away_manager_id DROP NOT NULL" in mig

    def test_manager_ids_are_not_in_always_required(self):
        """They must stay out, or an empty id becomes a per-row hard error and
        the whole game is skipped — the outcome this change exists to avoid."""
        from pipeline.etl.etl_historical_loader import ALWAYS_REQUIRED

        assert "home_manager_id" not in ALWAYS_REQUIRED
        assert "away_manager_id" not in ALWAYS_REQUIRED


class TestNightlyIngestScript:
    """The nightly chain must fail on an incomplete season, and must actually run.

    The gate was first written with `${YEAR}` interpolated INSIDE a quoted
    heredoc (`<<'PY'`). A quoted delimiter suppresses shell parameter expansion,
    so Python received the literal string `${YEAR}` and `int()` raised
    ValueError — the nightly job would have died on line 1, every night. The year
    is passed through the environment instead.
    """

    @staticmethod
    def _script() -> str:
        return (_REPO_ROOT / "scripts" / "nightly_ingest.sh").read_text(encoding="utf-8")

    def test_no_shell_interpolation_inside_a_quoted_heredoc(self):
        script = self._script()
        for block in script.split("<<'PY'")[1:]:
            body = block.split("\nPY")[0]
            assert "${" not in body, (
                "a quoted heredoc does NOT expand shell variables — this would "
                f"reach Python as a literal. Pass it via the environment instead:\n{body}"
            )

    def test_year_is_exported_for_the_python_step(self):
        script = self._script()
        assert "export YEAR" in script
        assert 'os.environ["YEAR"]' in script

    def test_chain_aborts_when_any_game_failed(self):
        """Rebuilding profiles and FAISS tiles on a knowingly incomplete season
        would bake the gap into every downstream artifact."""
        script = self._script()
        assert 'summary["failed"]' in script
        assert "sys.exit(1)" in script

    def test_still_runs_under_set_eu(self):
        assert "set -eu" in self._script()


class TestHttpHygiene:
    def test_every_outbound_call_goes_through_the_retry_policy(self):
        """No bare per-call transport use: everything funnels through
        ``_http_get``/``_connect`` so retry, Retry-After and the permanent-4xx
        rule apply uniformly."""
        from pipeline.etl import etl_historical_loader as mod

        src = inspect.getsource(mod)
        code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
        assert "requests.get(" not in code
        # urlopen appears exactly once — inside _urllib_fetch, below the retry loop.
        assert code.count("urlopen(") == 1, (
            "urlopen must only be called from _urllib_fetch; a direct call would "
            "bypass the bounded retry, the Retry-After cap and the permanent-4xx rule"
        )

    def test_requests_is_not_imported_by_the_default_transport(self):
        """SIM-446: importing ``requests`` drags in the mypyc-compiled
        ``charset_normalizer`` (four resident copies), ``_brotli`` and
        ``simplejson._speedups`` — the exact stack whose removal let a full
        2,468-game season complete without the interpreter-corruption crash.
        The import must therefore stay INSIDE the opt-in branch."""
        from pipeline.etl import etl_historical_loader as mod

        assert mod._HTTP_TRANSPORT == "urllib", "stdlib urllib is the default transport"
        src = inspect.getsource(mod)
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import requests", "from requests")):
                assert line.startswith(" "), (
                    "`import requests` must be indented (i.e. lazy, inside "
                    "_requests_session) so the default path never loads it"
                )

    def test_opt_in_requests_transport_still_pools(self):
        """The escape hatch has to remain a genuine A/B: one pooled Session, not
        a fresh TCP+TLS handshake per call across ~65,000 backfill requests."""
        src = inspect.getsource(
            __import__("pipeline.etl.etl_historical_loader", fromlist=["x"])._requests_session
        )
        assert "Session()" in src
        assert "_requests_session_cache" in src, "the Session must be cached, not rebuilt"

    def test_pool_construction_is_locked(self):
        src = inspect.getsource(HistoricalDataLoader._ensure_pool)
        assert "self._pool_lock" in src, (
            "the lazy `if self._pool is None` was a check-then-set race; two "
            "threads could each build a pool and the loser's connections would leak"
        )
