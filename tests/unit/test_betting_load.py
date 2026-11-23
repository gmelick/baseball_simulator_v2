"""The one load path (SIM-645): ``betting/load_odds_to_db.py`` fetches, keeps, checks and writes.

The live cycle and the historical loader both load odds through
:func:`betting.load_odds_to_db.load_odds` (or its parts, :func:`game_offers`,
:func:`prop_offers` and :func:`write_offers`). These tests pin each part:

  * ``odds_source`` is the BettingPros provider;
  * ``has_price`` tells a quote from an empty row;
  * the two fetch steps give one offer per market, drop the unpriced rows, and
    count a fetch that raises without stopping the next market;
  * ``keep_rows`` filters one book and counts and logs each refused row;
  * ``write_offers`` sends each offer to its own INSERT, re-stamps when told,
    counts a failed write without stopping the next offer, and counts the rows
    per book;
  * ``load_odds`` skips the game markets or the props when told;
  * the historical loader's ``_fetch_lineup_roles`` gives each player a role.

No network and no database. The rows come from the real BettingPros provider
fed by recorded real responses (``tests/bp_recorded.py``). A stub provider
appears only where a fetch must raise.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any
from unittest.mock import AsyncMock

import pytest
from tests.bp_recorded import (
    BRYCE_MILLER,
    GAME_92857,
    GAME_99026,
    JULIO_RODRIGUEZ,
    Recorded92857,
    Recorded99026,
    recorded_game_rows,
    recorded_prop_rows,
)

import betting.historical_loader as history
from betting.bettingpros.fetch_odds import BettingProsOddsProvider
from betting.load_odds_to_db import (
    _GAME_ODDS_INSERT_SQL,
    _GAME_ODDS_RESTAMP_SQL,
    _PROP_ODDS_INSERT_SQL,
    _PROP_ODDS_RESTAMP_SQL,
    game_offers,
    has_price,
    keep_rows,
    load_odds,
    odds_source,
    prop_offers,
    write_offers,
)
from betting.markets import (
    PROP_ROLE_BATTER,
    PROP_ROLE_BOTH,
    PROP_ROLE_PITCHER,
)
from betting.validate_odds import RefusalTally


class _RecordingPool:
    """A stand-in asyncpg pool: records each executemany; ``fail_first`` raises once."""

    def __init__(self, *, fail_first: bool = False) -> None:
        self.batches: list[tuple[str, list[tuple]]] = []
        self._fail_first = fail_first

    async def executemany(self, sql: str, params: Any) -> None:
        if self._fail_first:
            self._fail_first = False
            raise ConnectionError("the database connection dropped")
        self.batches.append((sql, [tuple(p) for p in params]))


class _RaisingOnMoneyline(Recorded92857):
    """The recorded provider, with a moneyline read that raises."""

    def get_odds_by_book(self, game_pk, *, line_type="current", market_type="moneyline"):
        if market_type == "moneyline":
            raise TimeoutError("vendor timed out")
        return super().get_odds_by_book(game_pk, line_type=line_type, market_type=market_type)


# ===========================================================================
# The source and the price check
# ===========================================================================


def test_odds_source_is_the_bettingpros_provider() -> None:
    assert isinstance(odds_source(), BettingProsOddsProvider)


class TestHasPrice:
    def test_a_recorded_game_row_and_prop_row_have_a_price(self) -> None:
        assert has_price(recorded_game_rows("moneyline")[0])
        assert has_price(recorded_game_rows("total")[0])
        assert has_price(recorded_prop_rows()[0])

    def test_a_row_with_every_price_empty_has_none(self) -> None:
        game = recorded_game_rows("moneyline")[0]
        assert not has_price({**game, "home_ml": None, "away_ml": None})
        prop = recorded_prop_rows()[0]
        assert not has_price({**prop, "line": None, "over_ml": None, "under_ml": None})
        assert not has_price({})

    def test_a_prop_row_reads_only_the_prop_fields(self) -> None:
        # A prop row with a line but no prices is a quote (the guard then judges it).
        prop = recorded_prop_rows()[0]
        assert has_price({**prop, "over_ml": None, "under_ml": None})


# ===========================================================================
# The fetch steps
# ===========================================================================


class TestGameOffers:
    def test_one_offer_per_line_type_and_market_with_a_price(self) -> None:
        offers = game_offers(Recorded92857(), GAME_92857, line_types=("opening", "closing"))
        labels = [label for label, _rows in offers]
        # The recording holds the three full-game markets; the twelve segment
        # markets read no row and give no offer.
        assert labels == [
            "opening/moneyline",
            "opening/runline",
            "opening/total",
            "closing/moneyline",
            "closing/runline",
            "closing/total",
        ]
        assert [len(rows) for _label, rows in offers] == [1, 1, 1, 10, 10, 10]
        assert all(has_price(row) for _label, rows in offers for row in rows)

    def test_the_markets_narrow_the_fetch(self) -> None:
        offers = game_offers(Recorded99026(), GAME_99026, game_markets=("f5_total",))
        assert [label for label, _rows in offers] == ["current/f5_total"]

    def test_a_fetch_that_raises_is_counted_and_the_next_market_runs(self, caplog) -> None:
        failures: Counter[str] = Counter()
        caplog.set_level(logging.WARNING, logger="betting.load_odds_to_db")
        offers = game_offers(
            _RaisingOnMoneyline(),
            GAME_92857,
            game_markets=("moneyline", "total"),
            failures=failures,
        )
        assert [label for label, _rows in offers] == ["current/total"]
        assert failures == Counter({"fetch": 1})
        assert f"game odds fetch failed game {GAME_92857} current/moneyline" in caplog.text


class TestPropOffers:
    def test_each_player_is_asked_only_the_markets_of_his_role(self) -> None:
        provider = Recorded92857()
        offers = prop_offers(
            provider,
            GAME_92857,
            {BRYCE_MILLER: PROP_ROLE_PITCHER, JULIO_RODRIGUEZ: PROP_ROLE_BATTER},
        )
        assert [label for label, _rows in offers] == [
            f"current/strikeouts (player {BRYCE_MILLER})",
            f"current/hits_allowed (player {BRYCE_MILLER})",
            f"current/singles (player {JULIO_RODRIGUEZ})",
        ]
        assert [len(rows) for _label, rows in offers] == [8, 2, 3]

    def test_a_prop_fetch_that_raises_is_counted_and_the_next_market_runs(self) -> None:
        class _Raising(Recorded92857):
            def get_prop_odds_by_book(self, game_pk, player_id, prop_stat, **kw):
                if prop_stat == "strikeouts":
                    raise TimeoutError("vendor timed out")
                return super().get_prop_odds_by_book(game_pk, player_id, prop_stat, **kw)

        failures: Counter[str] = Counter()
        offers = prop_offers(
            _Raising(), GAME_92857, {BRYCE_MILLER: PROP_ROLE_PITCHER}, failures=failures
        )
        assert [label for label, _rows in offers] == [
            f"current/hits_allowed (player {BRYCE_MILLER})"
        ]
        assert failures == Counter({"fetch": 1})


# ===========================================================================
# The keep step
# ===========================================================================


class TestKeepRows:
    def test_every_recorded_row_is_kept_and_offered(self) -> None:
        rows = recorded_game_rows("moneyline")
        tally = RefusalTally()
        kept = keep_rows(rows, game_pk=GAME_92857, tally=tally)
        assert kept == rows
        assert kept[0] is not rows[0]  # a copy: the caller's row is not shared
        assert (tally.n_offered, tally.refused) == (len(rows), 0)

    def test_the_book_filter_keeps_one_book(self) -> None:
        rows = recorded_game_rows("moneyline")
        tally = RefusalTally()
        kept = keep_rows(rows, game_pk=GAME_92857, tally=tally, book_id=10)
        assert [row["book"] for row in kept] == ["bp:10"]
        # The other books' rows never reach the guard.
        assert tally.n_offered == 1

    def test_a_refused_row_is_counted_and_logged(self, caplog) -> None:
        # The recorded first-five moneyline: the blend (bp:0) and Caesars (bp:13)
        # post two-way team prices beside a tie price.
        rows = Recorded99026().get_odds_by_book(GAME_99026, market_type="f5_moneyline")
        tally = RefusalTally()
        caplog.set_level(logging.INFO, logger="betting.load_odds_to_db")
        kept = keep_rows(rows, game_pk=GAME_99026, tally=tally)
        assert [row["book"] for row in kept] == ["bp:10"]
        assert tally.counts() == {
            ("three_way_two_way_team_prices", "f5_moneyline", "bp:0"): 1,
            ("three_way_two_way_team_prices", "f5_moneyline", "bp:13"): 1,
        }
        assert (tally.n_offered, tally.refused) == (3, 2)
        refused = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(refused) == 2
        assert (
            refused[0]
            .getMessage()
            .startswith(f"refused game {GAME_99026} current/f5_moneyline bp:0: ")
        )


# ===========================================================================
# The write step
# ===========================================================================


class TestWriteOffers:
    @pytest.mark.asyncio
    async def test_game_and_prop_offers_go_to_their_own_insert(self) -> None:
        pool = _RecordingPool()
        game = recorded_game_rows("moneyline")
        props = recorded_prop_rows()
        offers = [("current/moneyline", game), ("current/strikeouts", props)]
        written = await write_offers(pool, GAME_92857, offers, tally=RefusalTally(), restamp=False)
        assert written == len(game) + len(props)
        (game_sql, game_params), (prop_sql, prop_params) = pool.batches
        assert (game_sql, prop_sql) == (_GAME_ODDS_INSERT_SQL, _PROP_ODDS_INSERT_SQL)
        assert [p[3] for p in game_params] == [row["book"] for row in game]
        assert [p[8] for p in prop_params] == [row["book"] for row in props]

    @pytest.mark.asyncio
    async def test_restamp_picks_the_restamp_sql(self) -> None:
        pool = _RecordingPool()
        offers = [("m", recorded_game_rows("total")), ("p", recorded_prop_rows())]
        await write_offers(pool, GAME_92857, offers, tally=RefusalTally(), restamp=True)
        assert [sql for sql, _params in pool.batches] == [
            _GAME_ODDS_RESTAMP_SQL,
            _PROP_ODDS_RESTAMP_SQL,
        ]
        assert "last_seen_at" in _GAME_ODDS_RESTAMP_SQL

    @pytest.mark.asyncio
    async def test_a_failed_write_is_counted_and_the_next_offer_is_written(self, caplog) -> None:
        pool = _RecordingPool(fail_first=True)
        moneyline = recorded_game_rows("moneyline")
        total = recorded_game_rows("total")
        failures: Counter[str] = Counter()
        by_book: Counter[str] = Counter()
        caplog.set_level(logging.WARNING, logger="betting.load_odds_to_db")
        written = await write_offers(
            pool,
            GAME_92857,
            [("current/moneyline", moneyline), ("current/total", total)],
            tally=RefusalTally(),
            restamp=False,
            failures=failures,
            written_by_book=by_book,
        )
        assert written == len(total)  # only the second offer counts
        assert failures == Counter({"write": 1})
        assert by_book == Counter(row["book"] for row in total)
        ((_sql, params),) = pool.batches
        assert {p[5] for p in params} == {"total"}
        assert f"odds write failed game {GAME_92857} current/moneyline (10 rows)" in caplog.text

    @pytest.mark.asyncio
    async def test_written_by_book_counts_the_rows_sent_per_book(self) -> None:
        by_book: Counter[str] = Counter()
        offers = [("m", recorded_game_rows("moneyline")), ("t", recorded_game_rows("total"))]
        await write_offers(
            _RecordingPool(),
            GAME_92857,
            offers,
            tally=RefusalTally(),
            restamp=False,
            written_by_book=by_book,
        )
        assert sum(by_book.values()) == 20
        assert by_book["bp:10"] == 2  # FanDuel posts both markets

    @pytest.mark.asyncio
    async def test_the_book_filter_and_the_guard_run_before_the_write(self) -> None:
        pool = _RecordingPool()
        rows = Recorded99026().get_odds_by_book(GAME_99026, market_type="f5_moneyline")
        tally = RefusalTally()
        # Caesars' only row is refused: nothing is written, no empty batch is sent.
        assert (
            await write_offers(
                pool, GAME_99026, [("x", rows)], tally=tally, restamp=False, book_id=13
            )
            == 0
        )
        assert pool.batches == []
        assert tally.refused == 1


# ===========================================================================
# The load path
# ===========================================================================


class TestLoadOdds:
    @pytest.mark.asyncio
    async def test_the_game_markets_and_the_props_load_together(self) -> None:
        pool = _RecordingPool()
        written = await load_odds(
            pool,
            Recorded92857(),
            GAME_92857,
            tally=RefusalTally(),
            restamp=False,
            roles={BRYCE_MILLER: PROP_ROLE_PITCHER},
        )
        assert written == 30 + 10
        sqls = [sql for sql, _params in pool.batches]
        assert sqls.count(_GAME_ODDS_INSERT_SQL) == 3
        assert sqls.count(_PROP_ODDS_INSERT_SQL) == 2

    @pytest.mark.asyncio
    async def test_game_markets_none_skips_the_game_markets(self) -> None:
        provider = Recorded92857()
        pool = _RecordingPool()
        written = await load_odds(
            pool,
            provider,
            GAME_92857,
            tally=RefusalTally(),
            restamp=False,
            game_markets=None,
            roles={BRYCE_MILLER: PROP_ROLE_PITCHER},
        )
        assert written == 10
        assert {sql for sql, _params in pool.batches} == {_PROP_ODDS_INSERT_SQL}
        # No game market is read at all.
        game_ids = {122, 175, 176}
        assert not [p for path, p in provider.bp_calls if p.get("market_id") in game_ids]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("roles", [None, {}])
    async def test_no_roles_skips_the_props(self, roles) -> None:
        provider = Recorded92857()
        pool = _RecordingPool()
        written = await load_odds(
            pool, provider, GAME_92857, tally=RefusalTally(), restamp=False, roles=roles
        )
        assert written == 30
        assert {sql for sql, _params in pool.batches} == {_GAME_ODDS_INSERT_SQL}
        # No player is looked up.
        assert not [p for path, p in provider.bp_calls if p.get("market_id") == 285]

    @pytest.mark.asyncio
    async def test_a_fetch_and_a_write_failure_both_reach_failures(self) -> None:
        failures: Counter[str] = Counter()
        written = await load_odds(
            _RecordingPool(fail_first=True),
            _RaisingOnMoneyline(),
            GAME_92857,
            tally=RefusalTally(),
            restamp=False,
            game_markets=("moneyline", "total", "runline"),
            failures=failures,
        )
        assert failures == Counter({"fetch": 1, "write": 1})
        assert written == 10  # the run line; the total's write failed


# ===========================================================================
# The historical loader's lineup roles
# ===========================================================================


class TestFetchLineupRoles:
    @pytest.mark.asyncio
    async def test_each_player_gets_his_role(self) -> None:
        pool = AsyncMock()
        pool.fetch.return_value = [
            {"player_id": 1, "position_code": "P"},
            {"player_id": 2, "position_code": "SP"},
            {"player_id": 3, "position_code": "CF"},
            {"player_id": 4, "position_code": None},
            {"player_id": 5, "position_code": "P"},  # a two-way player:
            {"player_id": 5, "position_code": "DH"},  # listed to pitch and to bat
            {"player_id": 6, "position_code": "rp"},
        ]
        roles = await history._fetch_lineup_roles(pool, GAME_92857)
        assert roles == {
            1: PROP_ROLE_PITCHER,
            2: PROP_ROLE_PITCHER,
            3: PROP_ROLE_BATTER,
            4: PROP_ROLE_BATTER,
            5: PROP_ROLE_BOTH,
            6: PROP_ROLE_PITCHER,
        }
        sql, game_pk = pool.fetch.await_args.args
        assert "raw.game_lineups" in sql and game_pk == GAME_92857

    @pytest.mark.asyncio
    async def test_the_order_of_the_two_rows_does_not_matter(self) -> None:
        pool = AsyncMock()
        pool.fetch.return_value = [
            {"player_id": 5, "position_code": "DH"},
            {"player_id": 5, "position_code": "P"},
            {"player_id": 5, "position_code": "1B"},
        ]
        assert await history._fetch_lineup_roles(pool, 1) == {5: PROP_ROLE_BOTH}

    @pytest.mark.asyncio
    async def test_no_lineup_rows_give_no_roles(self) -> None:
        pool = AsyncMock()
        pool.fetch.return_value = []
        assert await history._fetch_lineup_roles(pool, 1) == {}
