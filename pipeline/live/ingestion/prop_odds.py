"""The live cycle's player props: the players each game needs, the cadence of the
load, and the closing-line stamp at first pitch.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import (
    UTC,
    datetime,
)
from typing import Any

from betting.load import promote_closing_prop_rows, prop_offers, write_offers
from betting.markets import PROP_ROLE_BATTER, PROP_ROLE_BOTH, PROP_ROLE_PITCHER
from pipeline.live.ingestion.base import PipelineBase
from pipeline.live.ingestion.constants import _PITCHER_POSITION_CODES, PROP_FETCH_CADENCE_S

log = logging.getLogger("live_ingestion")


class PropOddsMixin(PipelineBase):
    """Loads the player props of every watched game, and stamps their closing rows."""

    @staticmethod
    def _collect_prop_player_roles(game_state: dict) -> dict[int, str]:
        """
        player_id → role for every player eligible for prop-line
        capture in a built game_state.

        Props are offered on the pitcher on the mound and on the batters in
        both lineups.  We pull:
          * current_pitcher_id → PROP_ROLE_PITCHER
          * every player_id in home_lineup / away_lineup → PROP_ROLE_BATTER,
            read from the entry's boxscore ``position``

        The safe default is PROP_ROLE_BOTH (every market is requested):
          * a lineup entry with no ``position`` (the role is unknown);
          * a lineup entry whose position is a pitcher code — a two-way
            player bats AND pitches;
          * a player seen in two roles (the current pitcher who also bats).

        The dict keeps insertion order, is None-filtered and de-duplicated, so
        a fixed game_state always yields the same ids in the same order (keeps
        the dedup hash and tests stable).
        """
        roles: dict[int, str] = {}
        cp = game_state.get("current_pitcher_id")
        if cp:
            roles[int(cp)] = PROP_ROLE_PITCHER
        for side in ("home_lineup", "away_lineup"):
            for entry in game_state.get(side, []) or []:
                pid = entry.get("player_id")
                if not pid:
                    continue
                pid = int(pid)
                position = str(entry.get("position") or "").upper()
                if not position or position in _PITCHER_POSITION_CODES:
                    role = PROP_ROLE_BOTH
                else:
                    role = PROP_ROLE_BATTER
                if pid in roles and roles[pid] != role:
                    role = PROP_ROLE_BOTH
                roles[pid] = role
        return roles

    @staticmethod
    def _pregame_prop_player_roles(game: Mapping[str, Any]) -> dict[int, str]:
        """
        player_id → role for a game that has not started, read from
        the hydrated schedule entry (see ``SCHEDULE_HYDRATE``).

        A game before first pitch has no live feed, so the roles come from the
        schedule:
          * each side's probable pitcher (``teams.<side>.probablePitcher``)
            → PROP_ROLE_PITCHER;
          * each posted lineup player (``lineups.homePlayers`` /
            ``lineups.awayPlayers``) → PROP_ROLE_BATTER, or PROP_ROLE_BOTH when
            the player's primary position is a pitcher code or is missing.

        A player seen in two roles is PROP_ROLE_BOTH, as in
        :meth:`_collect_prop_player_roles`. Before the lineups are posted the
        map holds the probable pitchers only; it is empty when neither is
        known. The order is fixed (home pitcher, away pitcher, home lineup,
        away lineup), so a fixed entry always gives the same ids.
        """
        roles: dict[int, str] = {}

        def _add(pid: Any, role: str) -> None:
            if not pid:
                return
            pid = int(pid)
            if pid in roles and roles[pid] != role:
                role = PROP_ROLE_BOTH
            roles[pid] = role

        teams = game.get("teams") or {}
        for side in ("home", "away"):
            pitcher = (teams.get(side) or {}).get("probablePitcher") or {}
            _add(pitcher.get("id"), PROP_ROLE_PITCHER)
        lineups = game.get("lineups") or {}
        for key in ("homePlayers", "awayPlayers"):
            for player in lineups.get(key) or []:
                position = str(
                    ((player or {}).get("primaryPosition") or {}).get("abbreviation") or ""
                ).upper()
                role = (
                    PROP_ROLE_BOTH
                    if not position or position in _PITCHER_POSITION_CODES
                    else PROP_ROLE_BATTER
                )
                _add((player or {}).get("id"), role)
        return roles

    async def _persist_prop_odds_cycle(self, game_pk: int, game_state: dict) -> int:
        """
        Live prop-odds persistence cycle.

        Called from _refresh_game_state() on every WS signal, but self-throttled
        to at most once per PROP_FETCH_CADENCE_S seconds per game (prop lines move
        far more slowly than the per-pitch WS feed).  When the cadence gate opens,
        it:
          1. Collects prop-eligible players + their roles from the game_state
             (pitcher, batter or both).
          2. Fetches every book's rows of each offer via _prop_offers():
             the pitcher markets for pitchers and the batter markets
             for hitters.
          3. Runs the load guard on each row and persists each offer's kept
             rows in one batch via _persist_prop_odds_many().

        Returns the number of prop rows sent this cycle (0 when the cadence
        gate is closed or no eligible players were found).
        """
        return await self._persist_prop_roles_cycle(
            game_pk, self._collect_prop_player_roles(game_state)
        )

    async def _persist_prop_roles_cycle(self, game_pk: int, roles: Mapping[int, str]) -> int:
        """The prop cycle for a given role map (player_id → PROP_ROLE_*).

        The body of :meth:`_persist_prop_odds_cycle`, shared with the pre-game
        cycle (:meth:`_persist_pregame_odds`), whose roles come from the
        schedule. The same per-game cadence clock (``_last_prop_fetch``) gates
        both, so a game never pays the prop reads twice in one minute.
        """
        now = datetime.now(UTC)
        clock = getattr(self, "_last_prop_fetch", None)
        if clock is None:
            clock = {}
            self._last_prop_fetch = clock
        last = clock.get(game_pk)
        if last is not None and (now - last).total_seconds() < PROP_FETCH_CADENCE_S:
            # Cadence gate closed — too soon since the last prop fetch.
            return 0

        roles = dict(roles)
        player_ids = list(roles)
        if not player_ids:
            # Nothing to price yet (e.g. lineups not posted) — don't stamp the
            # cadence clock so the next signal retries promptly.
            return 0

        clock[game_pk] = now
        offers = await self._off_loop(prop_offers, self._odds_provider(), game_pk, roles)
        written = await write_offers(
            self._db,
            game_pk,
            offers,
            tally=self._refusal_tally(),
            restamp=await self._last_seen_available(),
        )
        if written:
            log.info(
                "persisted %d prop rows from %d offers for game %s (%d players)",
                written,
                len(offers),
                game_pk,
                len(player_ids),
            )
        return written

    async def mark_closing_prop_lines(
        self,
        game_pk: int,
        first_pitch_at: datetime,
        *,
        scheduled_start: datetime | None = None,
    ) -> int:
        """The prop analogue of :meth:`mark_closing_lines`.

        One closing row per (player, prop stat, book), by the same rule, with
        the prop hash rewritten. See :func:`promote_closing_prop_rows`.
        Returns the prop rows promoted.
        """
        updated = await promote_closing_prop_rows(
            self._db, game_pk, first_pitch_at, scheduled_start=scheduled_start
        )
        if updated:
            log.info(
                "Closing prop lines designated: game %s at %s (%d rows promoted)",
                game_pk,
                first_pitch_at.isoformat(),
                updated,
            )
        return updated
