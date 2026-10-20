"""The player-prop odds of the live cycle: the players each market needs, the
fetch, the writes and the closing-line marks.
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
from pipeline.live.ingestion.closing_rows import promote_closing_prop_rows
from pipeline.live.ingestion.constants import (
    _PITCHER_POSITION_CODES,
    PROP_FETCH_CADENCE_S,
    PROP_ROLE_BATTER,
    PROP_ROLE_BOTH,
    PROP_ROLE_PITCHER,
)
from pipeline.live.ingestion.odds_rows import (
    _PROP_ODDS_INSERT_SQL,
    _prop_odds_hash,
    _prop_row_has_odds,
    insert_prop_odds_rows,
    prop_odds_insert_args,
)
from pipeline.odds_provider import (
    BATTER_PROP_STATS,
    PITCHER_PROP_STATS,
    PROP_STATS,
    prop_rows_by_book,
)

log = logging.getLogger("live_ingestion")


class PropOddsMixin(PipelineBase):
    """Fetches and stores the player props of every watched game."""

    @staticmethod
    def _collect_prop_player_roles(game_state: dict) -> dict[int, str]:
        """
        Player_id → role for every player eligible for prop-line
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
        Player_id → role for a game that has not started, read from
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

    @classmethod
    def _collect_prop_player_ids(cls, game_state: dict) -> list[int]:
        """
        Extract the player_ids eligible for prop-line capture from a
        built game_state (the keys of :meth:`_collect_prop_player_roles`).

        Deduplicated, None-filtered, and order-stable so a fixed game_state
        always yields the same id list.
        """
        return list(cls._collect_prop_player_roles(game_state))

    @staticmethod
    def _prop_stats_for_role(role: str, prop_stats: tuple[str, ...]) -> tuple[str, ...]:
        """
        The subset of ``prop_stats`` a player in ``role`` is asked for.

        A pitcher gets the pitcher markets, a hitter the batter markets. Any
        other role (PROP_ROLE_BOTH, or an unknown string) gets every market —
        the safe default, so a mis-labelled player loses no quote.
        """
        if role == PROP_ROLE_PITCHER:
            return tuple(s for s in prop_stats if s in PITCHER_PROP_STATS)
        if role == PROP_ROLE_BATTER:
            return tuple(s for s in prop_stats if s in BATTER_PROP_STATS)
        return prop_stats

    def _prop_offers(
        self,
        game_pk: int,
        player_ids: list[int],
        *,
        line_type: str = "current",
        prop_stats: tuple[str, ...] = PROP_STATS,
        roles: Mapping[int, str] | None = None,
    ) -> list[tuple[int, str, list[dict]]]:
        """Every book's prop rows, one entry per offer.

        An offer is one (player, prop_stat) at one line type. Each entry is
        ``(player_id, prop_stat, rows)``: the provider's by-book rows through
        ``prop_rows_by_book`` (one row for the mock or a provider with only the
        one-row method), empty rows dropped. An offer with no row is left out.

        ``roles`` (player_id → PROP_ROLE_*) splits the markets by role;
        a player missing from ``roles``, or ``roles=None``, is asked for every
        market in ``prop_stats``. An unknown prop_stat is logged and skipped.
        """
        provider = self._odds_provider()  # Env-selected, mock by default
        offers: list[tuple[int, str, list[dict]]] = []
        for player_id in player_ids:
            role = roles.get(player_id, PROP_ROLE_BOTH) if roles else PROP_ROLE_BOTH
            for prop_stat in self._prop_stats_for_role(role, prop_stats):
                try:
                    rows = prop_rows_by_book(
                        provider, game_pk, player_id, prop_stat, line_type=line_type
                    )
                except ValueError:
                    # Unknown prop_stat — skip rather than abort the cycle.
                    log.warning(
                        "skipping unknown prop_stat '%s' for player %s game %s",
                        prop_stat,
                        player_id,
                        game_pk,
                    )
                    continue
                kept = [row for row in rows if _prop_row_has_odds(row)]
                if kept:
                    offers.append((player_id, prop_stat, kept))
        return offers

    def _fetch_prop_odds(
        self,
        game_pk: int,
        player_ids: list[int],
        *,
        line_type: str = "current",
        prop_stats: tuple[str, ...] = PROP_STATS,
        roles: Mapping[int, str] | None = None,
    ) -> list[dict]:
        """
        A flat list of every book's prop rows for the players.

        SIM-555 (2026-09-28): the provider names the books. The method reads
        every book's row of each (player, prop_stat) through
        ``prop_rows_by_book`` — BettingPros gives one row per book, labelled
        ``'bp:<id>'``; the mock gives its one ``'consensus'`` row. Empty rows
        are dropped; the load guard runs later, in the persisting cycle. The
        old fixed book list (PROP_BOOKS) and its ``books`` argument are gone.

        ``roles`` splits the markets by role (see
        :meth:`_prop_offers`).
        """
        return [
            row
            for _player_id, _prop_stat, rows in self._prop_offers(
                game_pk, player_ids, line_type=line_type, prop_stats=prop_stats, roles=roles
            )
            for row in rows
        ]

    async def _persist_prop_offers(
        self, game_pk: int, offers: list[tuple[int, str, list[dict]]]
    ) -> int:
        """Guard and persist each offer's rows in one batch; return the rows sent.

        One ``executemany`` per offer. A failed batch is logged and the next
        offer still runs.
        """
        written = 0
        for player_id, prop_stat, rows in offers:
            kept = self._guard_rows(game_pk, rows)
            if not kept:
                continue
            try:
                written += await self._persist_prop_odds_many(kept)
            except Exception as exc:  # noqa: BLE001 — one offer must not stop the rest
                log.warning(
                    "prop persist failed game %s player %s %s (%d rows): %s",
                    game_pk,
                    player_id,
                    prop_stat,
                    len(kept),
                    exc,
                )
        return written

    async def _persist_prop_odds_cycle(self, game_pk: int, game_state: dict) -> int:
        """
                Live prop-odds persistence cycle.

                Called from _refresh_game_state() on every WS signal, but self-throttled
                to at most once per PROP_FETCH_CADENCE_S seconds per game (prop lines move
                far more slowly than the per-pitch WS feed).  When the cadence gate opens,
                it:
                  1. Collects prop-eligible players + their roles from the game_state
                     (SIM-421: pitcher / batter / both).
                  2. Fetches every book's rows of each offer via _prop_offers()
        , the pitcher markets for pitchers and the batter markets
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
        offers = await self._off_loop(
            self._prop_offers, game_pk, player_ids, line_type="current", roles=roles
        )
        written = await self._persist_prop_offers(game_pk, offers)
        if written:
            log.info(
                "SIM-555: persisted %d prop rows from %d offers for game %s (%d players)",
                written,
                len(offers),
                game_pk,
                len(player_ids),
            )
        return written

    async def capture_opening_prop_lines(
        self,
        game_pk: int,
        player_ids: list[int],
        *,
        prop_stats: tuple[str, ...] = PROP_STATS,
        roles: Mapping[int, str] | None = None,
    ) -> int:
        """
        Opening-line capture hook for player props.

        Writes the raw.prop_odds rows of each (player, prop_stat) with
        line_type='opening'.  This is the prop analogue of the nightly opening
        game-line capture (opening_line_job.py / SIM-138): it records the FIRST
        line posted so opening→closing movement is recoverable for props, not
        just game markets.

        The rows are the provider's by-book opening rows (BettingPros:
        the opener's one row, labelled ``'bp:<id>'``); each passes the load
        guard and each offer's rows go to the database in one batch. The old
        ``books`` argument is gone with PROP_BOOKS.

        Pass ``roles`` (player_id → PROP_ROLE_*) to split the markets
        by role; without it every player is asked for every market.

        Idempotent by virtue of the dedup hash (migration 0013): re-running with
        an unchanged opening line is a no-op via ON CONFLICT DO NOTHING.

        Returns the number of opening prop rows sent.
        """
        offers = self._prop_offers(
            game_pk,
            player_ids,
            line_type="opening",
            prop_stats=prop_stats,
            roles=roles,
        )
        written = await self._persist_prop_offers(game_pk, offers)
        if written:
            log.info("SIM-340: captured %d opening prop lines for game %s", written, game_pk)
        return written

    _prop_odds_hash = staticmethod(_prop_odds_hash)

    async def _persist_prop_odds(self, prop: dict) -> None:
        """
                Inserts a fresh player prop odds snapshot into raw.prop_odds.

                Parameters
                ----------
                prop : dict returned by the configured provider's get_prop_odds()
                       (MockOddsAPI by default, or BettingProsOddsProvider under
                       ODDS_PROVIDER=bettingpros).  Required keys:
                         game_pk, player_id, prop_stat, line,
                         over_ml, under_ml, book, line_type, is_sharp_book,
                         source, is_mock

                Notes
                -----
                The live pipeline writes line_type='current' during active game windows
                via _persist_prop_odds_cycle() (SIM-340 wiring).  Opening lines are
                captured by capture_opening_prop_lines() and the nightly opening_line_job
        .  Closing lines are designated by mark_closing_prop_lines().

                Also writes ``odds_hash`` (SHA-256 of the prop payload) and uses
                ON CONFLICT (game_pk, player_id, source, odds_hash) DO NOTHING so the
                live cadence never accumulates duplicate rows for an unchanged line.
                Backed by the partial unique index ``idx_prop_odds_dedup`` (migration
                0013).

                Also writes ``book_line_at`` (migration 0028). The cycles
                persist one offer's rows at a time through
                :meth:`_persist_prop_odds_many`.

                The DB enforces prop_stat values via CHECK constraint (migration 0004).
                Any unknown prop_stat will fail here with a clear IntegrityError before
                the invalid value reaches the application layer.
        """
        await self._db.execute(_PROP_ODDS_INSERT_SQL, *self._prop_odds_params(prop))

    def _prop_odds_params(self, prop: Mapping[str, Any]) -> tuple[Any, ...]:
        """The bind values of one raw.prop_odds INSERT (see :func:`prop_odds_insert_args`)."""
        return prop_odds_insert_args(prop)

    async def _persist_prop_odds_many(self, rows: list[dict]) -> int:
        """Insert one offer's prop rows in one ``executemany``.

        The same SQL and dedup as :meth:`_persist_prop_odds`. Returns the number
        of rows sent (the dedup may insert fewer). An empty list sends nothing.
        """
        return await insert_prop_odds_rows(
            self._db, rows, restamp=await self._last_seen_available()
        )

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
