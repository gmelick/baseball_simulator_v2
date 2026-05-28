"""
Unit tests for SIM-092 and SIM-093 (Data Engineer sprint 2026-05-07)
=====================================================================

Permanent regression suite for:
  SIM-092 — Deduplicate raw.game_odds inserts via SHA-256 odds_hash
            and ON CONFLICT … DO NOTHING.
  SIM-093 — raw.etl_errors table + ETL hard-error path wiring +
            HistoricalDataLoader.reprocess_errored_games() helper.

These tests run without a live database — they parse the canonical schema
files, exercise pure-Python code paths, and use mocks where DB I/O would
otherwise be required.

Run:
    pytest tests/unit/test_data_engineer_sim092_sim093.py -v
"""

from __future__ import annotations

import asyncio
import pathlib
import re
import sys
from datetime import date
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path
# ---------------------------------------------------------------------------
_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_PG_SCHEMA_PATH = _ROOT / "db" / "schemas" / "01_postgres_schema.sql"
_MIG_DIR = _ROOT / "db" / "migrations" / "versions"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


# ===========================================================================
# SIM-092 — Deduplicate raw.game_odds via odds_hash
# ===========================================================================


class TestSim092OddsDedup:
    """odds_hash column + partial unique index + ON CONFLICT DO NOTHING."""

    # ----- Schema + migration shape -----

    def test_canonical_schema_has_odds_hash_column(self) -> None:
        sql = _read(_PG_SCHEMA_PATH)
        match = re.search(
            r"CREATE TABLE IF NOT EXISTS raw\.game_odds\s*\((.*?)\n\);",
            sql,
            re.DOTALL,
        )
        assert match, "raw.game_odds CREATE TABLE block missing"
        body = match.group(1)
        assert re.search(
            r"\bodds_hash\s+VARCHAR\(64\)", body, re.IGNORECASE
        ), "SIM-092: odds_hash VARCHAR(64) column missing from raw.game_odds"

    def test_canonical_schema_has_partial_unique_index(self) -> None:
        sql = _read(_PG_SCHEMA_PATH)
        assert re.search(
            r"CREATE\s+UNIQUE\s+INDEX\s+(IF\s+NOT\s+EXISTS\s+)?idx_game_odds_dedup"
            r".*?ON\s+raw\.game_odds\s*\(\s*game_pk\s*,\s*source\s*,\s*odds_hash\s*\)"
            r"\s*WHERE\s+odds_hash\s+IS\s+NOT\s+NULL",
            sql,
            re.DOTALL | re.IGNORECASE,
        ), "SIM-092: idx_game_odds_dedup partial unique index missing or wrong shape"

    def test_migration_0010_creates_column_and_index(self) -> None:
        mig = _read(_MIG_DIR / "0010_sim092_game_odds_dedup.py")
        assert "ADD COLUMN IF NOT EXISTS odds_hash" in mig
        assert "idx_game_odds_dedup" in mig
        assert "WHERE odds_hash IS NOT NULL" in mig
        assert 'down_revision = "0009"' in mig

    # ----- Hash function semantics -----

    def test_odds_hash_is_deterministic(self) -> None:
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        h1 = LiveIngestionPipeline._odds_hash(
            {
                "home_ml": -150,
                "away_ml": 130,
                "home_spread": -1.5,
                "total_line": 8.5,
                "book": "consensus",
                "line_type": "current",
                "market_type": "moneyline",
                "is_sharp_book": False,
            }
        )
        h2 = LiveIngestionPipeline._odds_hash(
            {
                "home_ml": -150,
                "away_ml": 130,
                "home_spread": -1.5,
                "total_line": 8.5,
                "book": "consensus",
                "line_type": "current",
                "market_type": "moneyline",
                "is_sharp_book": False,
            }
        )
        assert h1 == h2, "Same payload must produce the same hash"
        assert len(h1) == 64, "SHA-256 hex digest must be 64 chars"

    def test_odds_hash_changes_when_line_moves(self) -> None:
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        base = {
            "home_ml": -150,
            "away_ml": 130,
            "home_spread": -1.5,
            "total_line": 8.5,
            "book": "consensus",
            "line_type": "current",
            "market_type": "moneyline",
            "is_sharp_book": False,
        }
        h0 = LiveIngestionPipeline._odds_hash(base)
        h1 = LiveIngestionPipeline._odds_hash({**base, "home_ml": -160})
        assert h0 != h1, "Line move must produce a different hash"

    def test_odds_hash_normalizes_float_precision(self) -> None:
        """1.5 and 1.50 must hash identically — same line."""
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        h1 = LiveIngestionPipeline._odds_hash({"home_spread": 1.5})
        h2 = LiveIngestionPipeline._odds_hash({"home_spread": 1.50})
        assert h1 == h2

    def test_odds_hash_independent_of_dict_order(self) -> None:
        """Iteration order of the source dict must not change the hash."""
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        a = LiveIngestionPipeline._odds_hash(
            {
                "home_ml": -150,
                "away_ml": 130,
                "total_line": 8.5,
            }
        )
        b = LiveIngestionPipeline._odds_hash(
            {
                "total_line": 8.5,
                "away_ml": 130,
                "home_ml": -150,
            }
        )
        assert a == b

    def test_odds_hash_differs_by_book(self) -> None:
        """Two books at the same price are still distinct quotes."""
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        h_consensus = LiveIngestionPipeline._odds_hash(
            {
                "home_ml": -150,
                "book": "consensus",
            }
        )
        h_pinnacle = LiveIngestionPipeline._odds_hash(
            {
                "home_ml": -150,
                "book": "pinnacle",
            }
        )
        assert h_consensus != h_pinnacle

    def test_odds_hash_handles_missing_keys_consistently(self) -> None:
        """A payload missing every key still hashes — to a stable value."""
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        h1 = LiveIngestionPipeline._odds_hash({})
        h2 = LiveIngestionPipeline._odds_hash({})
        assert h1 == h2 and len(h1) == 64

    # ----- _persist_odds() integration -----

    def test_persist_odds_writes_hash_and_uses_on_conflict(self) -> None:
        """
        Regression: _persist_odds() must compute odds_hash and pass it as the
        17th positional argument; INSERT SQL must use ON CONFLICT … DO NOTHING.
        """
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        pipeline = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        pipeline._db = MagicMock()
        pipeline._db.execute = AsyncMock()

        odds_payload = {
            "source": "mock",
            "is_mock": True,
            "book": "consensus",
            "line_type": "current",
            "market_type": "moneyline",
            "is_sharp_book": False,
            "home_ml": -150,
            "away_ml": 130,
            "home_spread": -1.5,
            "home_spread_ml": -110,
            "away_spread": 1.5,
            "away_spread_ml": -110,
            "total_line": 8.5,
            "over_ml": -110,
            "under_ml": -110,
        }

        asyncio.run(pipeline._persist_odds(745001, odds_payload))

        assert pipeline._db.execute.call_count == 1
        call = pipeline._db.execute.call_args
        sql_text = call.args[0]
        assert (
            "ON CONFLICT" in sql_text and "DO NOTHING" in sql_text
        ), "SIM-092: _persist_odds must use ON CONFLICT … DO NOTHING"
        assert "odds_hash" in sql_text, "SIM-092: INSERT must include odds_hash column"
        # Last positional arg is the hash; must be a 64-char hex string.
        actual_hash = call.args[-1]
        assert isinstance(actual_hash, str) and len(actual_hash) == 64
        # Must equal the hash function output for the same payload.
        expected = LiveIngestionPipeline._odds_hash(odds_payload)
        assert (
            actual_hash == expected
        ), "SIM-092: _persist_odds must use _odds_hash() to compute the value"

    def test_persist_odds_identical_call_produces_same_hash(self) -> None:
        """Two identical _persist_odds() calls produce two identical hashes
        passed to PostgreSQL; the unique index is what dedupes server-side."""
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        pipeline = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        pipeline._db = MagicMock()
        pipeline._db.execute = AsyncMock()

        payload = {"home_ml": -150, "away_ml": 130, "total_line": 8.5}
        asyncio.run(pipeline._persist_odds(745001, payload))
        asyncio.run(pipeline._persist_odds(745001, payload))

        assert pipeline._db.execute.call_count == 2
        h1 = pipeline._db.execute.call_args_list[0].args[-1]
        h2 = pipeline._db.execute.call_args_list[1].args[-1]
        assert h1 == h2


# ===========================================================================
# SIM-093 — raw.etl_errors table + ETL wiring + reprocess_errored_games()
# ===========================================================================


class TestSim093EtlErrorsTable:
    """raw.etl_errors must exist in canonical schema and have the right shape."""

    def test_canonical_schema_has_etl_errors_table(self) -> None:
        sql = _read(_PG_SCHEMA_PATH)
        match = re.search(
            r"CREATE TABLE IF NOT EXISTS raw\.etl_errors\s*\((.*?)\);",
            sql,
            re.DOTALL,
        )
        assert match, "SIM-093: raw.etl_errors CREATE TABLE missing"
        body = match.group(1)
        for needed in (
            r"id\s+BIGSERIAL\s+PRIMARY KEY",
            r"game_pk\s+INTEGER",
            r"at_bat_number\s+INTEGER",
            r"pitch_number\s+INTEGER",
            r"error_type\s+VARCHAR\(10\)",
            r"error_messages\s+TEXT\[\]",
            r"created_at\s+TIMESTAMPTZ",
        ):
            assert re.search(
                needed, body, re.IGNORECASE
            ), f"SIM-093: column matching /{needed}/ missing from raw.etl_errors"
        assert re.search(
            r"CHECK\s*\(\s*error_type\s+IN\s*\(\s*'HARD'\s*,\s*'WARN'\s*\)\s*\)",
            body,
            re.IGNORECASE,
        ), "SIM-093: error_type CHECK constraint missing or malformed"

    def test_canonical_schema_has_etl_errors_indexes(self) -> None:
        sql = _read(_PG_SCHEMA_PATH)
        assert re.search(
            r"CREATE\s+INDEX\s+(IF\s+NOT\s+EXISTS\s+)?idx_etl_errors_game_pk"
            r"\s+ON\s+raw\.etl_errors\s*\(\s*game_pk\s*,\s*created_at\s*\)",
            sql,
            re.IGNORECASE,
        ), "SIM-093: idx_etl_errors_game_pk(game_pk, created_at) missing"

    def test_migration_0011_creates_table(self) -> None:
        mig = _read(_MIG_DIR / "0011_sim093_etl_errors_table.py")
        assert "CREATE TABLE IF NOT EXISTS raw.etl_errors" in mig
        assert "idx_etl_errors_game_pk" in mig
        assert 'down_revision = "0010"' in mig
        # FK must NOT be present — see migration docstring (loose-coupled by design)
        assert "REFERENCES raw.games" not in mig, (
            "SIM-093: raw.etl_errors must NOT reference raw.games — audit "
            "trail must outlive game-row deletes."
        )

    # ----- Loader integration -----

    def test_loader_has_log_etl_errors_method(self) -> None:
        from pipeline.etl.etl_historical_loader import HistoricalDataLoader

        assert callable(
            getattr(HistoricalDataLoader, "_log_etl_errors", None)
        ), "SIM-093: HistoricalDataLoader._log_etl_errors() missing"

    def test_loader_has_reprocess_errored_games_method(self) -> None:
        from pipeline.etl.etl_historical_loader import HistoricalDataLoader

        assert callable(
            getattr(HistoricalDataLoader, "reprocess_errored_games", None)
        ), "SIM-093: HistoricalDataLoader.reprocess_errored_games() missing"

    def test_log_etl_errors_inserts_per_skipped_pitch(self) -> None:
        """
        _log_etl_errors must run psycopg2.extras.execute_batch once with one
        row per skipped pitch, then commit exactly once.

        Uses hand-rolled stub classes (not MagicMock) for the cursor, because
        psycopg2.extras.execute_batch does ``b";".join([cur.mogrify(...)])``
        which fails when mogrify returns MagicMock instead of bytes.
        """
        from pipeline.etl.etl_historical_loader import HistoricalDataLoader

        class _CtxManager:
            def __init__(self, target):
                self.target = target

            def __enter__(self):
                return self.target

            def __exit__(self, *a):
                return False

        class _StubCursor:
            def __init__(self):
                self.mogrify_calls: list[tuple] = []
                self.execute_calls: list[tuple] = []

            def mogrify(self, sql, params=None):
                self.mogrify_calls.append((sql, params))
                # Real psycopg2.extras.execute_batch joins these with b";"
                return b"INSERT INTO raw.etl_errors VALUES ()"

            def execute(self, sql):
                self.execute_calls.append((sql,))

        class _StubConn:
            def __init__(self):
                self.cur = _StubCursor()
                self.commit_count = 0

            def cursor(self):
                return _CtxManager(self.cur)

            def commit(self):
                self.commit_count += 1

        conn = _StubConn()
        loader = HistoricalDataLoader.__new__(HistoricalDataLoader)
        loader._get_conn = lambda: _CtxManager(conn)

        errors = [
            (1, 1, ["NULL primary key column: pitch_number"]),
            (1, 2, ["Inning out of range: 99", "balls out of range: 5"]),
            (2, 1, ["Invalid inning_topbot: 'Middle'"]),
        ]
        loader._log_etl_errors(745001, errors)

        # One mogrify call per skipped pitch
        assert (
            len(conn.cur.mogrify_calls) == 3
        ), f"expected 3 mogrify calls; got {len(conn.cur.mogrify_calls)}"
        # Each mogrify call's first param is our INSERT template
        for sql_arg, params in conn.cur.mogrify_calls:
            assert "INSERT INTO raw.etl_errors" in sql_arg
            assert "'HARD'" in sql_arg
            assert params[0] == 745001
            assert isinstance(params[3], list)
        # Exactly one commit
        assert conn.commit_count == 1

    def test_log_etl_errors_no_op_on_empty_input(self) -> None:
        from pipeline.etl.etl_historical_loader import HistoricalDataLoader

        loader = HistoricalDataLoader.__new__(HistoricalDataLoader)
        loader._get_conn = MagicMock(
            side_effect=AssertionError(
                "SIM-093: _log_etl_errors must short-circuit on empty input — "
                "it should never open a DB connection just to write zero rows."
            )
        )
        loader._log_etl_errors(745001, [])  # no AssertionError → pass

    def test_reprocess_errored_games_returns_distinct_game_pks(self) -> None:
        """
        reprocess_errored_games(since=…) should issue a single SELECT DISTINCT
        and return the list of game_pks as ints.
        """
        from pipeline.etl.etl_historical_loader import HistoricalDataLoader

        loader = HistoricalDataLoader.__new__(HistoricalDataLoader)
        cur = MagicMock()
        cur.fetchall.return_value = [(745001,), (745002,), (745003,)]
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False
        ctx = MagicMock()
        ctx.__enter__.return_value = conn
        ctx.__exit__.return_value = False
        loader._get_conn = MagicMock(return_value=ctx)

        result = loader.reprocess_errored_games(since=date(2026, 5, 1))
        assert result == [745001, 745002, 745003]

        # Verify the SQL we issue
        cur.execute.assert_called_once()
        sql_called = cur.execute.call_args.args[0]
        assert "SELECT DISTINCT game_pk" in sql_called
        assert "FROM   raw.etl_errors" in sql_called or "FROM raw.etl_errors" in sql_called
        # Bind parameter shape: tuple containing the date
        assert cur.execute.call_args.args[1] == (date(2026, 5, 1),)


# ===========================================================================
# Migration chain integrity
# ===========================================================================


class TestMigrationChain:
    """The 0001 → 0011 chain must be unbroken."""

    def test_chain_unbroken(self) -> None:
        chain = {
            "0001_initial_schema.py": None,
            "0002_sim082_lineup_state_live_game_unique_index.py": "0001",
            "0003_sim083_etl_freshness_and_game_odds_to_schema.py": "0002",
            "0004_sim134_prop_odds_prop_stat_column_and_index.py": "0003",
            "0005_sim085_pitches_situation_index.py": "0004",
            "0006_sim086_games_venue_id_nullable.py": "0005",
            "0007_sim087_release_speed_threshold.py": "0006",
            "0008_sim088_drop_pitches_pitch_type_index.py": "0007",
            "0009_sim089_pitches_pitcher_season_clean_index.py": "0008",
            "0010_sim092_game_odds_dedup.py": "0009",
            "0011_sim093_etl_errors_table.py": "0010",
        }
        for filename, expected_parent in chain.items():
            mig = _read(_MIG_DIR / filename)
            if expected_parent is None:
                assert "down_revision = None" in mig, f"{filename}: expected down_revision = None"
            else:
                assert (
                    f'down_revision = "{expected_parent}"' in mig
                ), f'{filename}: expected down_revision = "{expected_parent}"'
