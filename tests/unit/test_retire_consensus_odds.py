"""``scripts/probes/retire_consensus_odds.py``: the refusal, the dry run and the
archive-then-delete, on a stub connection (never a real database).

One of the five scripts of the one-row-per-book re-load (SIM-555, Partition E).
No test touches the network or a database.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tests.script_loader import load_script

retire = load_script("retire_consensus_odds")


_LIVE_COLUMNS = ["id", "game_pk", "book", "book_line_at"]


class _StubTransaction:
    def __init__(self, conn: _StubConn) -> None:
        self.conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.outcomes.append("rollback" if exc_type else "commit")
        return False


class _StubConn:
    """Answers the retirement script's queries from canned values; records every call."""

    def __init__(
        self,
        *,
        missing: list[int],
        counts: dict[str, int],
        affected: dict[str, int] | None = None,
        archive_columns: list[str] | None = None,
        missing_props: list[int] | None = None,
    ) -> None:
        self.missing = missing
        self.missing_props = missing_props or []
        self.counts = counts
        self.affected = affected or counts
        self.archive_columns = archive_columns or _LIVE_COLUMNS
        self.calls: list[tuple[str, str]] = []
        self.transactions: list[dict[str, Any]] = []
        self.outcomes: list[str] = []

    def transaction(self, **kwargs: Any) -> _StubTransaction:
        self.transactions.append(kwargs)
        return _StubTransaction(self)

    @staticmethod
    def _table(sql: str) -> str:
        return "prop_odds" if "raw.prop_odds" in sql else "game_odds"

    async def fetch(self, sql: str, *args: Any):
        self.calls.append(("fetch", sql))
        if sql == retire.MISSING_SQL:
            return [{"game_pk": g} for g in self.missing]
        if sql == retire.MISSING_PROPS_SQL:
            return [{"game_pk": g} for g in self.missing_props]
        if sql == retire.COLUMNS_SQL:
            names = self.archive_columns if args[0].endswith("_archive") else _LIVE_COLUMNS
            return [{"column_name": c} for c in names]
        raise AssertionError(sql)

    async def fetchval(self, sql: str, *args: Any):
        self.calls.append(("fetchval", sql))
        return self.counts[self._table(sql)]

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append(("execute", sql))
        table = self._table(sql)
        n = self.affected[table]
        return f"INSERT 0 {n}" if sql.startswith("INSERT") else f"DELETE {n}"

    def executed(self) -> list[str]:
        return [sql for kind, sql in self.calls if kind == "execute"]


def _run(coro):
    return asyncio.run(coro)


class TestRetire:
    def test_a_season_not_reloaded_is_refused_and_untouched(self, capsys):
        conn = _StubConn(missing=[11, 12, 13], counts={"game_odds": 5, "prop_odds": 9})
        code = _run(retire.run(conn, [2024], dry_run=False))
        assert code == retire.EXIT_REFUSED
        assert conn.executed() == []
        out = capsys.readouterr().out
        assert "REFUSED" in out and "3 Final games" in out and "[11, 12, 13]" in out

    def test_the_dry_run_counts_and_writes_nothing(self, capsys):
        conn = _StubConn(missing=[], counts={"game_odds": 5, "prop_odds": 9})
        code = _run(retire.run(conn, [2024], dry_run=True))
        assert code == retire.EXIT_OK
        assert conn.executed() == []
        assert conn.transactions == [{"isolation": "repeatable_read", "readonly": True}]
        out = capsys.readouterr().out
        assert "dry run" in out and "raw.game_odds 5" in out and "raw.prop_odds 9" in out

    def test_a_real_run_archives_then_deletes_each_table(self, capsys):
        conn = _StubConn(missing=[], counts={"game_odds": 5, "prop_odds": 9})
        result = _run(retire.retire_season(conn, 2024, dry_run=False))
        assert result.archived == {"game_odds": 5, "prop_odds": 9}
        assert result.deleted == {"game_odds": 5, "prop_odds": 9}
        sqls = conn.executed()
        assert [s.split()[0] for s in sqls] == ["INSERT", "DELETE", "INSERT", "DELETE"]
        assert "raw.game_odds_archive (id, game_pk, book, book_line_at)" in sqls[0]
        assert all("book = 'consensus'" in s for s in sqls)
        assert conn.transactions == [{"isolation": "repeatable_read", "readonly": False}]
        assert conn.outcomes == ["commit"]

    def test_a_count_mismatch_rolls_the_season_back(self, capsys):
        conn = _StubConn(
            missing=[],
            counts={"game_odds": 5, "prop_odds": 9},
            affected={"game_odds": 4, "prop_odds": 9},
        )
        code = _run(retire.run(conn, [2024], dry_run=False))
        assert code == retire.EXIT_ERROR
        assert conn.outcomes == ["rollback"]
        assert "ERROR" in capsys.readouterr().out

    def test_an_archive_missing_a_live_column_is_an_error(self):
        conn = _StubConn(
            missing=[], counts={"game_odds": 5, "prop_odds": 9}, archive_columns=["id", "game_pk"]
        )
        with pytest.raises(retire.RetireError, match="book"):
            _run(retire.retire_season(conn, 2024, dry_run=False))
        assert conn.executed() == []
        assert conn.outcomes == ["rollback"]

    def test_a_refused_season_does_not_stop_the_others(self, capsys):
        class _PerSeason(_StubConn):
            async def fetch(self, sql, *args):
                if sql == retire.MISSING_SQL:
                    return [{"game_pk": 1}] if args[0] == 2020 else []
                return await super().fetch(sql, *args)

        conn = _PerSeason(missing=[], counts={"game_odds": 2, "prop_odds": 3})
        code = _run(retire.run(conn, [2020, 2021], dry_run=False))
        assert code == retire.EXIT_REFUSED
        assert len(conn.executed()) == 4  # 2021 ran
        assert "VACUUM" in capsys.readouterr().out

    def test_games_without_new_props_are_a_warning_not_a_refusal(self, capsys):
        conn = _StubConn(
            missing=[], counts={"game_odds": 5, "prop_odds": 9}, missing_props=[21, 22]
        )
        code = _run(retire.run(conn, [2024], dry_run=True))
        assert code == retire.EXIT_OK
        out = capsys.readouterr().out
        assert "WARNING: 2 Final games have consensus closing props" in out and "[21, 22]" in out

    def test_rows_affected(self):
        assert retire.rows_affected("INSERT 0 12") == 12
        assert retire.rows_affected("DELETE 7") == 7
        with pytest.raises(retire.RetireError):
            retire.rows_affected("")

    def test_the_cli_needs_seasons_and_a_dsn(self, monkeypatch):
        monkeypatch.delenv("BASEBALL_DB_DSN", raising=False)
        with pytest.raises(SystemExit):
            retire.parse_args(["--seasons", "2024"])
        with pytest.raises(SystemExit):
            retire.parse_args(["--dsn", "postgresql://x"])
        args = retire.parse_args(
            ["--seasons", "2024", "2025", "--dry-run", "--dsn", "postgresql://x"]
        )
        assert args.seasons == [2024, 2025] and args.dry_run


class _NamedStub(_StubConn):
    """The stub, plus a missing list per season and the season of each named game."""

    def __init__(
        self, *, missing_by_season: dict[int, list[int]], season_of: dict[int, int], **kw: Any
    ) -> None:
        super().__init__(missing=[], **kw)
        self.missing_by_season = missing_by_season
        self.season_of = season_of
        self.named_args: list[tuple[Any, ...]] = []

    async def fetch(self, sql: str, *args: Any):
        if sql == retire.NAMED_GAMES_SQL:
            self.calls.append(("fetch", sql))
            self.named_args.append(args)
            return [
                {"game_pk": g, "season": self.season_of[g]} for g in args[0] if g in self.season_of
            ]
        if sql == retire.MISSING_SQL:
            self.calls.append(("fetch", sql))
            return [{"game_pk": g} for g in self.missing_by_season.get(args[0], [])]
        return await super().fetch(sql, *args)


class TestRetireAllowMissing:
    """``--allow-missing`` lets named games through the refusal, nothing else."""

    _COUNTS = {"game_odds": 5, "prop_odds": 9}

    def test_a_named_missing_game_lets_its_season_through_and_is_printed(self, capsys):
        conn = _NamedStub(
            missing_by_season={2019: [567323]}, season_of={567323: 2019}, counts=self._COUNTS
        )
        code = _run(retire.run(conn, [2019], dry_run=False, allow_missing=[567323]))
        assert code == retire.EXIT_OK
        # The named game's rows are archived and deleted with the season: the same
        # four statements, with no game left out of them.
        sqls = conn.executed()
        assert [s.split()[0] for s in sqls] == ["INSERT", "DELETE", "INSERT", "DELETE"]
        assert not any("567323" in s or "ANY(" in s for s in sqls)
        assert conn.outcomes == ["commit"]
        out = capsys.readouterr().out
        assert "REFUSED" not in out and "archived and deleted" in out
        assert "ALLOWED by --allow-missing (1): [567323]" in out
        assert "stale" not in out

    def test_the_result_carries_the_named_game_apart_from_the_refusal(self):
        conn = _NamedStub(
            missing_by_season={2019: [567323]}, season_of={567323: 2019}, counts=self._COUNTS
        )
        result = _run(retire.retire_season(conn, 2019, dry_run=False, allow_missing=[567323]))
        assert not result.refused and result.missing_games == 0 and result.examples == []
        assert result.allowed == [567323] and result.stale_names == []
        assert result.archived == self._COUNTS and result.deleted == self._COUNTS

    def test_an_unnamed_missing_game_still_refuses(self, capsys):
        conn = _NamedStub(
            missing_by_season={2024: [745169, 746755, 745175]},
            season_of={745175: 2024},
            counts=self._COUNTS,
        )
        code = _run(retire.run(conn, [2024], dry_run=False, allow_missing=[745175]))
        assert code == retire.EXIT_REFUSED
        assert conn.executed() == []
        out = capsys.readouterr().out
        assert "REFUSED — 2 Final games" in out and "[745169, 746755]" in out
        assert "ALLOWED by --allow-missing (1): [745175]" in out

    def test_a_named_game_that_is_not_missing_is_a_stale_name(self, capsys):
        # 745175 sits in 2024 but has a bp: closing moneyline (it is not missing).
        conn = _NamedStub(
            missing_by_season={2024: []}, season_of={745175: 2024}, counts=self._COUNTS
        )
        code = _run(retire.run(conn, [2024], dry_run=False, allow_missing=[745175]))
        assert code == retire.EXIT_OK
        assert len(conn.executed()) == 4
        out = capsys.readouterr().out
        assert "WARNING: stale --allow-missing names [745175]" in out
        assert "ALLOWED" not in out

    def test_a_named_game_in_no_named_season_is_a_stale_name(self, capsys):
        # 567323 is a 2019 game, 999999999 is in no season; only 2024 runs.
        conn = _NamedStub(
            missing_by_season={2024: []}, season_of={567323: 2019}, counts=self._COUNTS
        )
        code = _run(retire.run(conn, [2024], dry_run=False, allow_missing=[999999999, 567323]))
        assert code == retire.EXIT_OK
        assert len(conn.executed()) == 4
        assert conn.named_args == [([567323, 999999999],)]
        out = capsys.readouterr().out
        assert "WARNING: stale --allow-missing names [567323, 999999999]" in out
        assert "in no named season" in out

    def test_each_season_gets_only_its_own_named_games(self, capsys):
        conn = _NamedStub(
            missing_by_season={2019: [567323], 2024: [745175]},
            season_of={567323: 2019, 745175: 2024},
            counts=self._COUNTS,
        )
        code = _run(retire.run(conn, [2019, 2024], dry_run=False, allow_missing=[745175, 567323]))
        assert code == retire.EXIT_OK
        assert len(conn.executed()) == 8
        out = capsys.readouterr().out
        assert "ALLOWED by --allow-missing (1): [567323]" in out
        assert "ALLOWED by --allow-missing (1): [745175]" in out
        assert "stale" not in out

    def test_the_dry_run_writes_nothing(self, capsys):
        conn = _NamedStub(
            missing_by_season={2019: [567323]}, season_of={567323: 2019}, counts=self._COUNTS
        )
        code = _run(retire.run(conn, [2019], dry_run=True, allow_missing=[567323]))
        assert code == retire.EXIT_OK
        assert conn.executed() == []
        assert conn.transactions == [{"isolation": "repeatable_read", "readonly": True}]
        out = capsys.readouterr().out
        assert "dry run" in out and "ALLOWED by --allow-missing (1): [567323]" in out

    def test_no_names_means_no_named_games_query(self, capsys):
        conn = _NamedStub(missing_by_season={2024: []}, season_of={}, counts=self._COUNTS)
        code = _run(retire.run(conn, [2024], dry_run=True))
        assert code == retire.EXIT_OK
        assert conn.named_args == []
        assert "WARNING" not in capsys.readouterr().out

    def test_the_cli_accepts_game_pks(self):
        base = ["--seasons", "2019", "--dsn", "postgresql://x"]
        assert retire.parse_args(base).allow_missing == []
        args = retire.parse_args([*base, "--allow-missing", "567323", "745175"])
        assert args.allow_missing == [567323, 745175]

    @pytest.mark.parametrize("values", [[], ["abc"], ["0"], ["-5"], ["567323", "1.5"]], ids=str)
    def test_the_cli_rejects_a_value_that_is_not_a_game_pk(self, values):
        with pytest.raises(SystemExit):
            retire.parse_args(
                ["--seasons", "2019", "--dsn", "postgresql://x", "--allow-missing", *values]
            )
