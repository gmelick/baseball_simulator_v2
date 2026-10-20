"""SIM-555 (2026-10-01) — the loader's retry option and its incomplete games.

A vendor read can fail for a passing reason (a time-out, a burst of HTTP 502 /
503 answers). The BettingPros provider catches the failure, logs a WARNING and
goes on with an empty or partial result. The historical loader
(``scripts/load_historical_odds.py``) used to list that game in its done-file
anyway, so the gap was silent and permanent: game 717171 lost every row to one
schedule time-out.

These tests pin the loader half of the fix:

  * ``--retries`` (default 3) and ``--retry-wait`` (default 5 s) reach the
    provider through ``set_retry_policy``; a bad value stops the run with the
    parser's exit code 2; a provider without the method still runs;
  * a game whose ``read_failures`` count grew while it loaded is INCOMPLETE:
    its rows so far stay written, the done-file does not list it, the end of
    the run names it, and the run exits 1 (``EXIT_INCOMPLETE``), which the
    crash-safe wrapper reads as "run again in a minute";
  * a fetch or a write the loader itself caught leaves the game incomplete too;
  * the loader drops the provider's stored failed lookups before each game, so
    a player whose read failed in one game is read again in the next;
  * a ``--skip-loaded-since`` run says that it skips an incomplete game with
    rows;
  * a clean game is listed and a clean run exits 0.

No network and no database: a stub provider serves one moneyline row (and one
prop row) per request and adds to its own ``read_failures`` for the games told
to fail, as the BettingPros provider does when it gives up on a read. One class
(``TestTheRealProvider``) runs the real BettingPros provider instead, built by
the registry, over a scripted ``urlopen``: it pins the seam between the two
halves, from the flags to the provider's retry and back to the done-file.
"""

from __future__ import annotations

import email.message
import json
import logging
import sys
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import scripts.load_historical_odds as loader

from pipeline.odds_provider import register_odds_provider

#: The registry name the stub providers are served under.
_STUB = "sim555_retry_stub"


def _moneyline(game_pk: int, line_type: str, market_type: str) -> dict[str, Any]:
    """One book's moneyline row that the load guard keeps."""
    return {
        "game_pk": game_pk,
        "source": "bettingpros",
        "is_mock": False,
        "book": "bp:12",
        "line_type": line_type,
        "market_type": market_type,
        "is_sharp_book": False,
        "home_ml": -120,
        "away_ml": 105,
    }


def _prop(game_pk: int, player_id: int, prop_stat: str, line_type: str) -> dict[str, Any]:
    """One book's prop row that the load guard keeps."""
    return {
        "game_pk": game_pk,
        "player_id": player_id,
        "prop_stat": prop_stat,
        "line": 5.5,
        "over_ml": -110,
        "under_ml": -110,
        "book": "bp:12",
        "line_type": line_type,
        "is_sharp_book": False,
        "source": "bettingpros",
        "is_mock": False,
    }


class _CountingProvider:
    """A provider that takes a retry policy and counts the reads it gave up on.

    ``fail_game_odds`` maps game_pk to the failed reads one game-odds request
    for that game costs; ``fail_props`` does the same for one prop request.
    ``raise_game_odds`` names the games whose game-odds request raises past
    the provider (an error the loader catches). ``start_failures`` is the
    count before the run (a failure from before the first game must not mark
    that game). ``calls`` records each ``forget_failed_lookups`` and each
    game-odds request, in order.
    """

    def __init__(
        self,
        *,
        fail_game_odds: dict[int, int] | None = None,
        fail_props: dict[int, int] | None = None,
        raise_game_odds: set[int] | None = None,
        start_failures: int = 0,
    ) -> None:
        self.read_failures = start_failures
        self.policies: list[tuple[int, float]] = []
        self.calls: list[str] = []
        self._fail_game_odds = dict(fail_game_odds or {})
        self._fail_props = dict(fail_props or {})
        self._raise_game_odds = set(raise_game_odds or ())

    def set_retry_policy(self, max_retries: int, retry_wait_s: float) -> None:
        self.policies.append((max_retries, retry_wait_s))

    def forget_failed_lookups(self) -> None:
        self.calls.append("forget")

    def get_odds(self, game_pk, *, line_type="current", market_type="moneyline"):
        self.calls.append(f"odds {game_pk}")
        if game_pk in self._raise_game_odds:
            raise KeyError("participants")
        self.read_failures += self._fail_game_odds.get(game_pk, 0)
        return _moneyline(game_pk, line_type, market_type)

    def get_prop_odds(self, game_pk, player_id, prop_stat, *, line_type="current"):
        self.read_failures += self._fail_props.get(game_pk, 0)
        return _prop(game_pk, player_id, prop_stat, line_type)


class _PlainProvider:
    """A provider with neither ``set_retry_policy`` nor ``read_failures`` (the mock's shape)."""

    def get_odds(self, game_pk, *, line_type="current", market_type="moneyline"):
        return _moneyline(game_pk, line_type, market_type)


class _Recorder:
    """Records each batch the loader hands its writers; the games in ``fail_writes`` raise."""

    def __init__(self) -> None:
        self.game_rows: list[tuple[int, dict]] = []
        self.prop_rows: list[dict] = []
        self.fail_writes: set[int] = set()

    async def game(self, game_pk: int, rows: list[dict]) -> int:
        if game_pk in self.fail_writes:
            raise ConnectionError("the database connection dropped")
        self.game_rows.extend((game_pk, row) for row in rows)
        return len(rows)

    async def prop(self, rows: list[dict]) -> int:
        self.prop_rows.extend(rows)
        return len(rows)


def _wire(monkeypatch, provider: Any, game_pks: list[int], *, with_players: bool = False):
    """Point ``run()`` at ``provider``, the given games and a recording writer; no DB."""
    rec = _Recorder()
    register_odds_provider(_STUB, lambda: provider)

    async def _games(dsn, seasons, max_games, **kw):
        return [{"game_pk": pk} for pk in game_pks]

    class _Pool:
        async def close(self) -> None:
            return None

    async def _create_pool(*args, **kwargs):
        return _Pool()

    async def _players(pool, game_pk):
        return [(682243, True)] if with_players else []

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(create_pool=_create_pool))
    # run() sets the offline cache time-to-live only when it is unset; pin it
    # here so monkeypatch restores the environment afterwards.
    monkeypatch.setenv("ODDS_OFFERS_CACHE_TTL_S", "0")
    monkeypatch.delenv("ODDS_PROVIDER", raising=False)
    monkeypatch.setattr(loader, "_fetch_final_games", _games)
    monkeypatch.setattr(loader, "_fetch_lineup_players", _players)
    monkeypatch.setattr(loader, "_build_persisters", lambda dsn, pool: (rec.game, rec.prop))
    return rec


def _args(*extra: str, props: bool = False) -> list[str]:
    """The command line of a one-market, one-line-type run against the stub."""
    base = [
        "--seasons",
        "2024",
        "--provider",
        _STUB,
        "--game-markets",
        "moneyline",
        "--line-types",
        "closing",
    ]
    if props:
        base += ["--prop-stats", "strikeouts"]
    else:
        base.append("--no-props")
    return [*base, *extra]


# ===========================================================================
# The flags
# ===========================================================================


class TestRetryFlags:
    def test_the_defaults_are_three_retries_and_five_seconds(self) -> None:
        args = loader.parse_args(["--seasons", "2024"])
        assert args.retries == 3 == loader.DEFAULT_RETRIES
        assert args.retry_wait == 5.0 == loader.DEFAULT_RETRY_WAIT_S

    def test_the_flags_take_a_value(self) -> None:
        args = loader.parse_args(["--seasons", "2024", "--retries", "0", "--retry-wait", "0.5"])
        assert args.retries == 0
        assert args.retry_wait == 0.5

    @pytest.mark.parametrize(
        "flags",
        [
            ["--retries", "-1"],
            ["--retries", "two"],
            ["--retry-wait", "0"],
            ["--retry-wait", "-2"],
            ["--retry-wait", "nan"],
            ["--retry-wait", "inf"],
        ],
    )
    def test_a_bad_value_is_a_usage_error(self, flags, capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            loader.parse_args(["--seasons", "2024", *flags])
        assert exc.value.code == 2
        assert flags[0] in capsys.readouterr().err

    def test_the_policy_check_names_the_bad_value(self) -> None:
        assert loader._select_retry_policy(3, 5) == (3, 5.0)
        with pytest.raises(ValueError, match="--retries must be 0 or more"):
            loader._select_retry_policy(-1, 5.0)
        with pytest.raises(ValueError, match="--retry-wait must be a positive number"):
            loader._select_retry_policy(3, 0.0)

    def test_the_exit_codes(self) -> None:
        # 1 is also Python's exit status after an uncaught exception: the
        # wrapper runs the loader again on both, and never on 0 or 2.
        assert loader.EXIT_INCOMPLETE == 1


# ===========================================================================
# The hand-off to the provider
# ===========================================================================


class TestRetryPolicyHandOff:
    @pytest.mark.asyncio
    async def test_run_hands_the_flag_values_to_the_provider(self, monkeypatch) -> None:
        provider = _CountingProvider()
        _wire(monkeypatch, provider, [746001])
        args = loader.parse_args(_args("--retries", "5", "--retry-wait", "1.5"))
        assert await loader.run(args) == 0
        assert provider.policies == [(5, 1.5)]

    @pytest.mark.asyncio
    async def test_run_hands_over_the_defaults(self, monkeypatch, caplog) -> None:
        provider = _CountingProvider()
        _wire(monkeypatch, provider, [746001])
        caplog.set_level(logging.INFO, logger="load_historical_odds")
        assert await loader.run(loader.parse_args(_args())) == 0
        assert provider.policies == [(3, 5.0)]
        assert "up to 3 retries each, the first after 5.0 s" in caplog.text

    @pytest.mark.asyncio
    async def test_a_provider_without_a_policy_still_runs(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        rec = _wire(monkeypatch, _PlainProvider(), [746001, 746002])
        done = tmp_path / "load.done"
        caplog.set_level(logging.INFO, logger="load_historical_odds")
        assert await loader.run(loader.parse_args(_args("--done-file", str(done)))) == 0
        assert "_PlainProvider does not retry a failed read" in caplog.text
        assert [pk for pk, _row in rec.game_rows] == [746001, 746002]
        # No read_failures count: every game is complete.
        assert done.read_text(encoding="utf-8").split() == ["746001", "746002"]


# ===========================================================================
# Incomplete games
# ===========================================================================


class TestIncompleteGames:
    @pytest.mark.asyncio
    async def test_an_incomplete_game_stays_off_the_done_list_and_the_run_exits_1(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        provider = _CountingProvider(fail_game_odds={746002: 2})
        rec = _wire(monkeypatch, provider, [746001, 746002, 746003])
        done = tmp_path / "load.done"
        caplog.set_level(logging.INFO, logger="load_historical_odds")
        rc = await loader.run(loader.parse_args(_args("--done-file", str(done))))
        assert rc == loader.EXIT_INCOMPLETE == 1
        # The incomplete game is not listed; the clean games are, in order.
        assert done.read_text(encoding="utf-8").split() == ["746001", "746003"]
        # Its rows so far stay written.
        assert [pk for pk, _row in rec.game_rows] == [746001, 746002, 746003]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        per_game = [
            r.getMessage() for r in warnings if "game 746002 is incomplete" in r.getMessage()
        ]
        assert len(per_game) == 1
        assert "(failed vendor reads: 2)" in per_game[0]
        assert "stays off the done-list, so the next run loads it again" in per_game[0]
        summary = warnings[-1].getMessage()
        assert summary.startswith("incomplete games: 1 ")
        assert "746002;" in summary
        assert "the next run loads them again" in summary
        assert "(1 incomplete)" in caplog.text

    @pytest.mark.asyncio
    async def test_the_next_run_loads_only_the_incomplete_game(self, monkeypatch, tmp_path) -> None:
        done = tmp_path / "load.done"
        provider = _CountingProvider(fail_game_odds={746002: 1})
        _wire(monkeypatch, provider, [746001, 746002, 746003])
        assert await loader.run(loader.parse_args(_args("--done-file", str(done)))) == 1
        # The vendor answers this time (the wrapper's run a minute later).
        rec = _wire(monkeypatch, _CountingProvider(), [746001, 746002, 746003])
        assert await loader.run(loader.parse_args(_args("--done-file", str(done)))) == 0
        assert [pk for pk, _row in rec.game_rows] == [746002]
        assert loader.read_done_file(str(done)) == {746001, 746002, 746003}

    @pytest.mark.asyncio
    async def test_a_failed_prop_read_also_leaves_the_game_incomplete(
        self, monkeypatch, tmp_path
    ) -> None:
        provider = _CountingProvider(fail_props={746001: 1})
        rec = _wire(monkeypatch, provider, [746001, 746002], with_players=True)
        done = tmp_path / "load.done"
        rc = await loader.run(loader.parse_args(_args("--done-file", str(done), props=True)))
        assert rc == 1
        assert done.read_text(encoding="utf-8").split() == ["746002"]
        assert [row["game_pk"] for row in rec.prop_rows] == [746001, 746002]

    @pytest.mark.asyncio
    async def test_a_clean_run_lists_every_game_and_exits_0(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        # A count left from before the first game marks no game.
        provider = _CountingProvider(start_failures=7)
        _wire(monkeypatch, provider, [746001, 746002])
        done = tmp_path / "load.done"
        caplog.set_level(logging.INFO, logger="load_historical_odds")
        assert await loader.run(loader.parse_args(_args("--done-file", str(done)))) == 0
        assert done.read_text(encoding="utf-8").split() == ["746001", "746002"]
        assert "incomplete games: none" in caplog.text
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    @pytest.mark.asyncio
    async def test_without_a_done_file_the_run_reports_the_list_and_exits_1(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        monkeypatch.chdir(tmp_path)
        provider = _CountingProvider(fail_game_odds={746001: 1, 746003: 4})
        _wire(monkeypatch, provider, [746001, 746002, 746003])
        caplog.set_level(logging.INFO, logger="load_historical_odds")
        assert await loader.run(loader.parse_args(_args())) == 1
        assert list(tmp_path.iterdir()) == []  # no done-file is written
        text = caplog.text
        assert "game 746001 is incomplete" in text
        assert "the run lists it at the end" in text
        assert "incomplete games: 2 (a vendor read failed after its retries, or a fetch" in text
        assert "): 746001, 746003;" in text
        assert "this run keeps no done-file" in text

    @pytest.mark.asyncio
    async def test_a_fetch_error_the_loader_caught_leaves_the_game_incomplete(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        """An error that escapes the provider is caught by the loader, which skips
        the market. That game has a hole too, so it stays off the done-list."""
        provider = _CountingProvider(raise_game_odds={746002})
        rec = _wire(monkeypatch, provider, [746001, 746002, 746003])
        done = tmp_path / "load.done"
        caplog.set_level(logging.INFO, logger="load_historical_odds")
        rc = await loader.run(loader.parse_args(_args("--done-file", str(done))))
        assert rc == loader.EXIT_INCOMPLETE
        assert done.read_text(encoding="utf-8").split() == ["746001", "746003"]
        assert [pk for pk, _row in rec.game_rows] == [746001, 746003]
        assert "game odds fetch failed game 746002 closing/moneyline" in caplog.text
        assert "game 746002 is incomplete (failed fetches: 1); it stays off" in caplog.text

    @pytest.mark.asyncio
    async def test_a_write_error_the_loader_caught_leaves_the_game_incomplete(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        provider = _CountingProvider(fail_game_odds={746001: 2})
        rec = _wire(monkeypatch, provider, [746001, 746002, 746003])
        rec.fail_writes = {746001, 746003}
        done = tmp_path / "load.done"
        caplog.set_level(logging.INFO, logger="load_historical_odds")
        rc = await loader.run(loader.parse_args(_args("--done-file", str(done))))
        assert rc == loader.EXIT_INCOMPLETE
        assert done.read_text(encoding="utf-8").split() == ["746002"]
        assert "game 746003 is incomplete (failed writes: 1)" in caplog.text
        # Both kinds name their count, the provider's first.
        assert "game 746001 is incomplete (failed vendor reads: 2; failed writes: 1)" in (
            caplog.text
        )

    @pytest.mark.asyncio
    async def test_the_loader_forgets_the_failed_lookups_before_each_game(
        self, monkeypatch
    ) -> None:
        provider = _CountingProvider()
        _wire(monkeypatch, provider, [746001, 746002])
        assert await loader.run(loader.parse_args(_args())) == 0
        assert provider.calls == ["forget", "odds 746001", "forget", "odds 746002"]

    @pytest.mark.asyncio
    async def test_a_skip_loaded_since_run_says_it_skips_incomplete_games_with_rows(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        """The resume SQL drops a game that holds rows before the done-file is read,
        so the next --skip-loaded-since run would skip an incomplete game with rows.
        The run warns at the start and the summary says how to fill the games."""
        provider = _CountingProvider(fail_game_odds={746002: 1})
        _wire(monkeypatch, provider, [746001, 746002])
        done = tmp_path / "load.done"
        caplog.set_level(logging.INFO, logger="load_historical_odds")
        argv = _args("--done-file", str(done), "--skip-loaded-since", "2026-09-28T10:00:00+00:00")
        assert await loader.run(loader.parse_args(argv)) == loader.EXIT_INCOMPLETE
        assert "--skip-loaded-since also skips a game an earlier run left incomplete" in (
            caplog.text
        )
        summary = [r for r in caplog.records if r.levelno == logging.WARNING][-1].getMessage()
        assert summary.startswith("incomplete games: 1 ")
        assert "re-run without it (with --done-file) to fill them" in summary
        assert "so the next run loads them again" not in summary


# ===========================================================================
# The helpers
# ===========================================================================


class TestHelpers:
    def test_the_summary_names_at_most_twenty_games(self) -> None:
        pks = list(range(700001, 700026))  # 25 games
        text = loader._incomplete_summary(pks, done_file="x.done")
        assert text.startswith("incomplete games: 25 ")
        assert "700020 and 5 more;" in text
        assert "700021" not in text
        assert loader._incomplete_summary([], done_file=None) == (
            "incomplete games: none (no vendor read, fetch or write failed)"
        )

    def test_the_reasons_name_each_kind_of_failure(self) -> None:
        assert loader._incomplete_reasons(0, {}) == ""
        assert loader._incomplete_reasons(0, {"fetch": 0, "write": 0}) == ""
        assert loader._incomplete_reasons(3, {}) == "failed vendor reads: 3"
        assert loader._incomplete_reasons(1, {"fetch": 2, "write": 1}) == (
            "failed vendor reads: 1; failed fetches: 2; failed writes: 1"
        )

    def test_read_failures_reads_only_a_whole_number(self) -> None:
        assert loader._read_failures(_PlainProvider()) == 0
        assert loader._read_failures(_CountingProvider(start_failures=4)) == 4
        # A MagicMock attribute is not a count (int() of one would read 1).
        assert loader._read_failures(MagicMock()) == 0
        assert loader._read_failures(types.SimpleNamespace(read_failures=True)) == 0


# ===========================================================================
# The seam: the real BettingPros provider under the loader
# ===========================================================================


class _Response:
    """The context-manager response ``urlopen`` returns."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _Vendor:
    """``urlopen`` that answers HTTP 502 to the first ``fail_first`` requests.

    Every later request gets a schedule without the game: a legitimate
    absence, so the provider writes no row and counts no failed read.
    """

    def __init__(self, fail_first: int) -> None:
        self.fail_first = fail_first
        self.urls: list[str] = []

    def __call__(self, req: urllib.request.Request, timeout: float | None = None) -> _Response:
        self.urls.append(req.full_url)
        if len(self.urls) <= self.fail_first:
            raise urllib.error.HTTPError(
                req.full_url, 502, "Bad Gateway", email.message.Message(), None
            )
        return _Response({"dates": []})


class TestTheRealProvider:
    """The registry's BettingPros provider, driven by ``run()`` (SIM-555).

    Only the network is fake. The flags reach ``set_retry_policy``, the
    provider's ``_http_get_json`` retries, its ``read_failures`` grows when a
    read outlasts the retries, and the loader reads that count per game.
    """

    @pytest.fixture
    def vendor(self, monkeypatch):
        """Wire ``run()`` to the real provider over a scripted vendor; return the waits."""
        _wire(monkeypatch, _PlainProvider(), [746001, 746002])
        monkeypatch.setenv("ODDS_API_KEY", "k")
        monkeypatch.delenv("BETTINGPROS_MAX_RETRIES", raising=False)
        monkeypatch.delenv("BETTINGPROS_RETRY_WAIT_S", raising=False)
        waits: list[float] = []
        # The provider takes time.sleep when it is built, inside run().
        monkeypatch.setattr(time, "sleep", waits.append)

        def install(fail_first: int) -> tuple[_Vendor, list[float]]:
            fake = _Vendor(fail_first)
            monkeypatch.setattr(urllib.request, "urlopen", fake)
            return fake, waits

        return install

    @pytest.mark.asyncio
    async def test_a_burst_the_retries_outlast_leaves_every_game_complete(
        self, vendor, tmp_path, caplog
    ) -> None:
        fake, waits = vendor(fail_first=2)
        done = tmp_path / "load.done"
        caplog.set_level(logging.INFO, logger="load_historical_odds")
        argv = _args("--done-file", str(done), "--retries", "3", "--retry-wait", "0.25")
        argv[argv.index(_STUB)] = "bettingpros"
        assert await loader.run(loader.parse_args(argv)) == 0
        # Two 502s, then the schedule answers: two waits, doubled.
        assert waits == [0.25, 0.5]
        assert len(fake.urls) == 4  # three attempts for game 1, one for game 2
        assert done.read_text(encoding="utf-8").split() == ["746001", "746002"]
        assert "incomplete games: none" in caplog.text

    @pytest.mark.asyncio
    async def test_a_read_that_outlasts_the_retries_leaves_the_game_off_the_done_list(
        self, vendor, tmp_path, caplog
    ) -> None:
        fake, waits = vendor(fail_first=10**9)
        done = tmp_path / "load.done"
        caplog.set_level(logging.INFO, logger="load_historical_odds")
        argv = _args("--done-file", str(done), "--retries", "2", "--retry-wait", "0.25")
        argv[argv.index(_STUB)] = "bettingpros"
        assert await loader.run(loader.parse_args(argv)) == loader.EXIT_INCOMPLETE
        # One schedule read per game, three attempts each, then the provider gives up.
        assert waits == [0.25, 0.5, 0.25, 0.5]
        assert len(fake.urls) == 6
        assert not done.exists() or done.read_text(encoding="utf-8").split() == []
        assert "game 746001 is incomplete (failed vendor reads: 1)" in caplog.text
        assert "game 746002 is incomplete (failed vendor reads: 1)" in caplog.text
        assert "incomplete games: 2 (a vendor read failed after its retries" in caplog.text
        assert "): 746001, 746002;" in caplog.text

    @pytest.mark.asyncio
    async def test_a_player_whose_read_failed_in_one_game_loads_in_the_next(
        self, monkeypatch, tmp_path
    ) -> None:
        """Two games share a pitcher. His people read fails in the first game only.
        The loader's cache time-to-live (600 s) stores that failure across games; the
        loader drops it before the second game, which reads him again and loads his
        props. Before that, the second game got the stored failure, no count, no
        props, and went on the done-list."""
        rec = _wire(monkeypatch, _PlainProvider(), [746001, 746002], with_players=True)
        monkeypatch.setenv("ODDS_OFFERS_CACHE_TTL_S", "600")  # the loader's own value
        monkeypatch.setenv("ODDS_API_KEY", "k")
        monkeypatch.delenv("BETTINGPROS_MAX_RETRIES", raising=False)
        fake = _FixtureVendor(people_failures=1)
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        done = tmp_path / "load.done"
        argv = _args("--done-file", str(done), "--retries", "0", "--no-game-odds", props=True)
        argv[argv.index(_STUB)] = "bettingpros"
        assert await loader.run(loader.parse_args(argv)) == loader.EXIT_INCOMPLETE
        # The first game is incomplete; the second loads the pitcher's props and is listed.
        assert done.read_text(encoding="utf-8").split() == ["746002"]
        assert {row["game_pk"] for row in rec.prop_rows} == {746002}
        assert {row["player_id"] for row in rec.prop_rows} == {682243}
        assert fake.paths.count("/api/v1/people/682243") == 2


_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "bettingpros"

#: The captured offers of BettingPros event 92857 (game 746437), by market id.
_OFFERS_92857 = {122: "offers_ml_92857.json", 285: "offers_k_92857.json"}


class _FixtureVendor:
    """``urlopen`` that serves game 746437's captured payloads by path.

    Every game_pk gets the same schedule, so two game_pks share the slate, the
    event and the strikeout market (Bryce Miller). The first
    ``people_failures`` people reads answer HTTP 502.
    """

    def __init__(self, people_failures: int) -> None:
        self.people_failures = people_failures
        self.paths: list[str] = []

    def __call__(self, req: urllib.request.Request, timeout: float | None = None) -> _Response:
        url = urllib.parse.urlsplit(req.full_url)
        self.paths.append(url.path)
        if "/people/" in url.path:
            if sum("/people/" in p for p in self.paths) <= self.people_failures:
                raise urllib.error.HTTPError(
                    req.full_url, 502, "Bad Gateway", email.message.Message(), None
                )
            return _Response({"people": [{"fullName": "Bryce Miller"}]})
        if url.path.endswith("/schedule"):
            return _Response(_load("mlb_schedule_746437.json"))
        if url.path.endswith("/events"):
            return _Response(_load("events_2024-08-15.json"))
        market = int(urllib.parse.parse_qs(url.query)["market_id"][0])
        return _Response(_load(_OFFERS_92857[market]))


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIX / name).read_text(encoding="utf-8"))
