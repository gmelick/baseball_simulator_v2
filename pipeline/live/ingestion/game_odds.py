"""The live cycle's game odds: the broadcast row, the cadence of the load, and the
closing-line stamp at first pitch.
"""

from __future__ import annotations

import logging
from datetime import (
    UTC,
    datetime,
)
from typing import Any

from betting.load import game_offers, promote_closing_game_rows, write_offers
from pipeline.live.ingestion.base import PipelineBase
from pipeline.live.ingestion.constants import PROP_FETCH_CADENCE_S

log = logging.getLogger("live_ingestion")


class GameOddsMixin(PipelineBase):
    """Loads the game markets of every watched game, and stamps their closing rows."""

    def _forget_odds_game(self, game_pk: Any) -> None:
        """Make the provider read one game's schedule and event again.

        The provider keeps a found game for the process lifetime (``forget_game``).
        A provider without that method keeps nothing, so the call is skipped.
        """
        forget = getattr(self._odds_provider(), "forget_game", None)
        if game_pk is not None and callable(forget):
            forget(int(game_pk))

    async def _fetch_odds(self, game_pk: int) -> dict:
        """The one row the refresh loop sends to the browser: the graded book's moneyline.

        It is not stored; :meth:`_persist_game_odds_cycle` stores every book's row.
        """
        return await self._off_loop(self._odds_provider().get_odds, game_pk)

    async def _persist_game_odds_cycle(self, game_pk: int) -> int:
        """Load every book's row of every game market, at most once a minute per game.

        The fetch runs on the vendor thread; the check and the writes run here
        (:func:`betting.load.write_offers`). Returns the rows sent (0 while the
        cadence gate is closed).
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
        offers = await self._off_loop(game_offers, self._odds_provider(), game_pk)
        written = await write_offers(
            self._db,
            game_pk,
            offers,
            tally=self._refusal_tally(),
            restamp=await self._last_seen_available(),
        )
        if written:
            log.info("persisted %d game-market rows for game %s", written, game_pk)
        return written

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
