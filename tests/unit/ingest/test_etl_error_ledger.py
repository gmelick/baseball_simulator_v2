"""The ETL error ledger: ``raw.etl_errors`` holds one row per pitch the
historical loader skips, and the loader writes it on the pitch insert's cursor."""

from __future__ import annotations

import pathlib
import re
from unittest.mock import MagicMock

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_PG_SCHEMA_PATH = _ROOT / "db" / "schemas" / "01_postgres_schema.sql"
_MIG_DIR = _ROOT / "db" / "migrations" / "versions"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


class TestEtlErrorLedger:
    """``raw.etl_errors`` is in the canonical schema with its columns, and the
    loader writes and reads it."""

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
            assert re.search(needed, body, re.IGNORECASE), (
                f"SIM-093: column matching /{needed}/ missing from raw.etl_errors"
            )
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
        # No foreign key: the ledger outlives a deleted game row (the
        # migration's docstring says why).
        assert "REFERENCES raw.games" not in mig, (
            "SIM-093: raw.etl_errors must NOT reference raw.games — audit "
            "trail must outlive game-row deletes."
        )

    # ----- Loader integration -----

    def test_loader_has_error_ledger_writer(self) -> None:
        """The ledger writer is the staticmethod ``_write_error_ledger``, and the
        old ``_log_etl_errors`` is gone.

        The writer runs on the CALLER's cursor, so the ledger shares the
        pitch-insert transaction. A writer with its own connection commits
        before the pitch insert, so a rolled-back insert leaves error rows for
        a game that was never written.
        """
        from ingest.games.load_games import HistoricalDataLoader

        assert callable(getattr(HistoricalDataLoader, "_write_error_ledger", None)), (
            "SIM-093/441: HistoricalDataLoader._write_error_ledger() missing"
        )
        assert not hasattr(HistoricalDataLoader, "_log_etl_errors"), (
            "SIM-441: the old self-committing _log_etl_errors must be gone, not "
            "left alongside the transactional one"
        )

    def test_write_error_ledger_inserts_per_skipped_pitch(self) -> None:
        """One raw.etl_errors row per skipped pitch, on the caller's cursor.

        The writer opens no connection and does not commit: the ledger and the
        pitch rows commit or roll back together. The insert carries
        ON CONFLICT DO UPDATE on the natural key. A game whose rows ALL
        hard-error writes no pitch rows, so the nightly job processes it again,
        and the key keeps that from adding a duplicate error set.

        The test uses hand-rolled stubs, not MagicMock:
        psycopg2.extras.execute_batch does ``b";".join([cur.mogrify(...)])``,
        which fails when mogrify returns a MagicMock instead of bytes.
        """
        from ingest.games.load_games import HistoricalDataLoader

        class _StubCursor:
            def __init__(self):
                self.mogrify_calls: list[tuple] = []
                self.execute_calls: list[tuple] = []

            def mogrify(self, sql, params=None):
                self.mogrify_calls.append((sql, params))
                return b"INSERT INTO raw.etl_errors VALUES ()"

            def execute(self, sql):
                self.execute_calls.append((sql,))

        cur = _StubCursor()
        errors = [
            (1, 1, ["NULL primary key column: pitch_number"]),
            (1, 2, ["Inning out of range: 99"]),
            (2, 1, ["Invalid inning_topbot: None"]),
        ]
        HistoricalDataLoader._write_error_ledger(cur, 745001, errors)

        assert len(cur.mogrify_calls) == 3, "one row per skipped pitch"
        sql = cur.mogrify_calls[0][0]
        assert "INSERT INTO raw.etl_errors" in sql
        assert "ON CONFLICT (game_pk, at_bat_number, pitch_number, error_type)" in sql, (
            "SIM-441: the ledger insert must be idempotent"
        )
        params = cur.mogrify_calls[0][1]
        assert params[0] == 745001
        assert params[1] == 1 and params[2] == 1
        assert params[3] == ["NULL primary key column: pitch_number"]

    def test_write_error_ledger_no_op_on_empty_input(self) -> None:
        from unittest.mock import patch

        from ingest.games.load_games import HistoricalDataLoader

        cur = MagicMock()
        with patch("ingest.games.load_games.psycopg2.extras.execute_batch") as eb:
            HistoricalDataLoader._write_error_ledger(cur, 745001, [])
        eb.assert_not_called()
