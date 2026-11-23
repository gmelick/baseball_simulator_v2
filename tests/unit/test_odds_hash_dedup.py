"""The live pipeline stores a game-odds quote once: ``raw.game_odds`` carries
an ``odds_hash`` (SHA-256 of the quote) and a partial unique index on
(game_pk, source, odds_hash), and ``betting.load_odds_to_db.insert_game_odds_rows`` inserts
with ON CONFLICT … DO NOTHING."""

from __future__ import annotations

import asyncio
import pathlib
import re
from unittest.mock import AsyncMock, MagicMock

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PG_SCHEMA_PATH = _ROOT / "db" / "schemas" / "01_postgres_schema.sql"
_MIG_DIR = _ROOT / "db" / "migrations" / "versions"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


class TestOddsDedup:
    """The odds_hash column, the partial unique index and ON CONFLICT DO NOTHING."""

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
        assert re.search(r"\bodds_hash\s+VARCHAR\(64\)", body, re.IGNORECASE), (
            "SIM-092: odds_hash VARCHAR(64) column missing from raw.game_odds"
        )

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
        from betting.load_odds_to_db import _odds_hash

        h1 = _odds_hash(
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
        h2 = _odds_hash(
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
        from betting.load_odds_to_db import _odds_hash

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
        h0 = _odds_hash(base)
        h1 = _odds_hash({**base, "home_ml": -160})
        assert h0 != h1, "Line move must produce a different hash"

    def test_odds_hash_normalizes_float_precision(self) -> None:
        """1.5 and 1.50 must hash identically — same line."""
        from betting.load_odds_to_db import _odds_hash

        h1 = _odds_hash({"home_spread": 1.5})
        h2 = _odds_hash({"home_spread": 1.50})
        assert h1 == h2

    def test_odds_hash_independent_of_dict_order(self) -> None:
        """Iteration order of the source dict must not change the hash."""
        from betting.load_odds_to_db import _odds_hash

        a = _odds_hash(
            {
                "home_ml": -150,
                "away_ml": 130,
                "total_line": 8.5,
            }
        )
        b = _odds_hash(
            {
                "total_line": 8.5,
                "away_ml": 130,
                "home_ml": -150,
            }
        )
        assert a == b

    def test_odds_hash_differs_by_book(self) -> None:
        """Two books at the same price are still distinct quotes."""
        from betting.load_odds_to_db import _odds_hash

        h_consensus = _odds_hash(
            {
                "home_ml": -150,
                "book": "consensus",
            }
        )
        h_pinnacle = _odds_hash(
            {
                "home_ml": -150,
                "book": "pinnacle",
            }
        )
        assert h_consensus != h_pinnacle

    def test_odds_hash_handles_missing_keys_consistently(self) -> None:
        """A payload missing every key still hashes — to a stable value."""
        from betting.load_odds_to_db import _odds_hash

        h1 = _odds_hash({})
        h2 = _odds_hash({})
        assert h1 == h2 and len(h1) == 64

    # ----- insert_game_odds_rows() integration -----

    def test_insert_writes_hash_last_and_uses_on_conflict(self) -> None:
        """The game-odds insert passes odds_hash as the LAST positional argument
        (book_line_at comes just before it), and its INSERT uses
        ON CONFLICT … DO NOTHING. The row is a real recorded BettingPros row."""
        from tests.bp_recorded import GAME_92857, recorded_game_rows

        from betting.load_odds_to_db import _odds_hash, insert_game_odds_rows

        row = recorded_game_rows("moneyline")[0]
        conn = MagicMock()
        conn.executemany = AsyncMock()

        asyncio.run(insert_game_odds_rows(conn, GAME_92857, [row]))

        assert conn.executemany.call_count == 1
        sql_text, args = conn.executemany.call_args.args
        assert "ON CONFLICT" in sql_text and "DO NOTHING" in sql_text
        assert "odds_hash" in sql_text
        actual_hash = args[0][-1]
        assert isinstance(actual_hash, str) and len(actual_hash) == 64
        assert actual_hash == _odds_hash(dict(row))

    def test_an_identical_row_sent_twice_carries_the_same_hash(self) -> None:
        """Two identical inserts pass identical hashes to PostgreSQL; the unique
        index is what dedupes server-side."""
        from tests.bp_recorded import GAME_92857, recorded_game_rows

        from betting.load_odds_to_db import insert_game_odds_rows

        row = recorded_game_rows("total")[0]
        conn = MagicMock()
        conn.executemany = AsyncMock()
        asyncio.run(insert_game_odds_rows(conn, GAME_92857, [row]))
        asyncio.run(insert_game_odds_rows(conn, GAME_92857, [row]))

        assert conn.executemany.call_count == 2
        h1 = conn.executemany.call_args_list[0].args[1][0][-1]
        h2 = conn.executemany.call_args_list[1].args[1][0][-1]
        assert h1 == h2
