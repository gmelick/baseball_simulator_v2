"""The game odds of the live cycle: the fetch, the load guard, the writes and the
closing-line marks.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import (
    UTC,
    datetime,
)
from typing import Any

from pipeline.live.ingestion.base import PipelineBase
from pipeline.live.ingestion.closing_rows import promote_closing_game_rows
from pipeline.live.ingestion.constants import PROP_FETCH_CADENCE_S
from pipeline.live.ingestion.odds_rows import (
    _GAME_ODDS_INSERT_SQL,
    _game_row_has_odds,
    _odds_hash,
    game_odds_insert_args,
    insert_game_odds_rows,
)
from pipeline.odds_provider import (
    GAME_MARKET_TYPES,
    odds_rows_by_book,
)

log = logging.getLogger("live_ingestion")


class GameOddsMixin(PipelineBase):
    """Fetches and stores the game markets of every watched game."""

    def _forget_odds_game(self, game_pk: Any) -> None:
        """Make the odds provider read one game's schedule and event again.

        The BettingPros provider keeps a found game for the process lifetime
        (``forget_game``); a provider without that method keeps nothing, so
        the call is skipped.
        """
        forget = getattr(self._odds_provider(), "forget_game", None)
        if game_pk is not None and callable(forget):
            forget(int(game_pk))

    async def _fetch_odds(self, game_pk: int) -> dict:
        """
        Returns odds from the configured provider.  Defaults to the
        deterministic MockOddsAPI; set ODDS_PROVIDER=bettingpros to use the real
        BettingProsOddsProvider feed without touching this code.

        The refresh loop sends this one row to the browser and no
        longer persists it; :meth:`_persist_game_odds_cycle` stores every
        book's row. With BettingPros the row is the graded book's moneyline.
        """
        return await self._off_loop(self._odds_provider().get_odds, game_pk)

    def _game_market_offers(
        self, game_pk: int, *, line_type: str = "current"
    ) -> list[tuple[str, list[dict]]]:
        """Every book's rows of each ``GAME_MARKET_TYPES`` market, one entry per market.

        Each entry is ``(market_type, rows)``, empty rows dropped; a market with
        no row is left out. An unknown market_type is a programming error and a
        failed fetch is a network error: both are logged, not raised, so one
        bad market cannot stop the cycle.
        """
        provider = self._odds_provider()
        offers: list[tuple[str, list[dict]]] = []
        for market_type in GAME_MARKET_TYPES:
            try:
                rows = odds_rows_by_book(
                    provider, game_pk, line_type=line_type, market_type=market_type
                )
            except ValueError as exc:
                log.warning("skipping game market %r for game %s: %s", market_type, game_pk, exc)
                continue
            except Exception as exc:  # noqa: BLE001 — one market must not stop the rest
                log.warning(
                    "game market %r fetch failed for game %s: %s", market_type, game_pk, exc
                )
                continue
            kept = [row for row in rows if _game_row_has_odds(row)]
            if kept:
                offers.append((market_type, kept))
        return offers

    def _fetch_game_market_odds(self, game_pk: int, *, line_type: str = "current") -> list[dict]:
        """Every book's row of every game market, in market order.

        The three full-game markets come through here too, one row per book
        and market. A market the provider cannot resolve (no event, no offer)
        gives no row, so no empty row is ever written.
        """
        return [
            row
            for _market, rows in self._game_market_offers(game_pk, line_type=line_type)
            for row in rows
        ]

    async def _persist_game_odds_cycle(self, game_pk: int) -> int:
        """Persist every book's row of every game market, on the cadence.

        Gated by its own clock (``_last_game_odds_fetch``) at
        ``PROP_FETCH_CADENCE_S``, so the fifteen
        markets are fetched once a minute per game, not on every pitch signal.
        Each market's kept rows go to the database in one batch. Returns the
        rows sent this cycle (0 while the gate is closed).
        """
        now = datetime.now(UTC)
        clock = getattr(self, "_last_game_odds_fetch", None)
        if clock is None:
            clock = {}
            self._last_game_odds_fetch = clock
        last = clock.get(game_pk)
        if last is not None and (now - last).total_seconds() < PROP_FETCH_CADENCE_S:
            return 0
        clock[game_pk] = now
        written = 0
        offers = await self._off_loop(self._game_market_offers, game_pk, line_type="current")
        for market_type, rows in offers:
            kept = self._guard_rows(game_pk, rows)
            if not kept:
                continue
            try:
                written += await self._persist_odds_many(game_pk, kept)
            except Exception as exc:  # noqa: BLE001 — one market must not stop the rest
                log.warning(
                    "game market persist failed game %s %s (%d rows): %s",
                    game_pk,
                    market_type,
                    len(kept),
                    exc,
                )
        if written:
            log.info("SIM-555: persisted %d game-market rows for game %s", written, game_pk)
        return written

    _odds_hash = staticmethod(_odds_hash)

    def _odds_params(self, game_pk: int, odds: Mapping[str, Any]) -> tuple[Any, ...]:
        """The bind values of one raw.game_odds INSERT (see :func:`game_odds_insert_args`)."""
        return game_odds_insert_args(game_pk, odds)

    async def _persist_odds(self, game_pk: int, odds: dict) -> None:
        """
        Inserts a fresh odds snapshot into raw.game_odds.

        Writes all four CLV-enabling columns
          (book, line_type, market_type, is_sharp_book).

        Also writes ``odds_hash`` (SHA-256 of the odds payload)
          and uses ON CONFLICT (game_pk, source, odds_hash) DO NOTHING
          so successive identical snapshots are deduplicated at write time.
          Backed by the partial unique index ``idx_game_odds_dedup``.

        Also writes ``book_line_at`` (migration 0028). The live cycle
        and the historical loader persist one offer's rows at a time through
        :meth:`_persist_odds_many`; this one-row method stays for single rows.

        The live pipeline always writes line_type='current'.  Opening lines are
        captured by the nightly opening line job.  Closing lines are
        designated by mark_closing_lines(), which the schedule poll calls at
        first pitch.
        """
        await self._db.execute(_GAME_ODDS_INSERT_SQL, *self._odds_params(game_pk, odds))

    async def _persist_odds_many(self, game_pk: int, rows: list[dict]) -> int:
        """Insert one offer's game-odds rows in one ``executemany``.

        The same SQL and the same dedup as :meth:`_persist_odds` (the
        ``odds_hash`` ``ON CONFLICT DO NOTHING``), one database round trip for
        the offer's books instead of one per book. Returns the number of rows
        sent (the dedup may insert fewer). An empty list sends nothing.
        """
        return await insert_game_odds_rows(
            self._db, game_pk, rows, restamp=await self._last_seen_available()
        )

    async def mark_closing_lines(
        self,
        game_pk: int,
        first_pitch_at: datetime,
        *,
        scheduled_start: datetime | None = None,
    ) -> int:
        """Designate a game's closing rows in raw.game_odds.

        One closing row per (market, book). Per key, the latest row fetched at
        or before
        ``first_pitch_at`` among the 'current' and 'closing' rows is promoted
        when it is 'current' and the load guard keeps its stamp against
        ``scheduled_start`` (``None`` = no stamp check). The promoted row's
        ``odds_hash`` is rewritten to its closing hash, so the nightly loader's
        identical row deduplicates against it. A second call promotes nothing.
        See :func:`promote_closing_game_rows`.

        The schedule poll calls this the first time it sees a game Live
        (:meth:`_mark_closing_rows`). Returns the rows promoted.
        """
        updated = await promote_closing_game_rows(
            self._db, game_pk, first_pitch_at, scheduled_start=scheduled_start
        )
        if updated:
            log.info(
                "Closing lines designated: game %s at %s (%d rows promoted)",
                game_pk,
                first_pitch_at.isoformat(),
                updated,
            )
        return updated
