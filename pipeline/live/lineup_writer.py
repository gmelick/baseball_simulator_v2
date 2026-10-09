"""The published-lineup writer (SIM-519 Part B).

A game that has not started must be simulable, or the slate shows games the
platform cannot price. The simulator resolves a game from ``raw.game_lineups``,
and before this module only the historical loader wrote that table, from a
FINAL game's box. So every upcoming game answered 503 to ``/simulate``.

The live service calls :meth:`PublishedLineupWriter.on_preview` for each game
the schedule shows in Preview. When the league has posted both lineups, the
writer reads the game's feed once, builds the starting rows with the loader's
own builder (``_build_starting_lineup_rows``: each starter at his starting
position, the SIM-559 rule) and writes them with ``source = 'published'``.
Before first pitch the box lists no pitcher, so the schedule's probable
pitcher becomes each side's ``P`` row (the resolver's starter when the box has
none).

When the lineups are not posted and ``LIVE_PROJECTED_LINEUPS=1`` (decision D3;
default OFF), the writer copies each team's last final lineup, against a
starter of the same hand when one of the last ten games had one, with today's
probable pitcher as ``P``, and writes it with ``source = 'projected'``.

The final box is the authority: the loader deletes a game's non-box rows
before its own insert (``_ensure_game_lineups``). The writer never touches a
game that already has box rows.

Rows are written in one transaction: delete the game's published and
projected rows, insert the new set. A player the database does not know (a
debuting call-up) is added from the league's ``people`` endpoint first, since
``raw.game_lineups.player_id`` is a foreign key.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from pipeline.etl.etl_historical_loader import _build_starting_lineup_rows, _parse_height
from pipeline.mlb_schedule import SCHEDULED, card_state, parse_game

log = logging.getLogger(__name__)

FEED_URL = "https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"
PEOPLE_URL = "https://statsapi.mlb.com/api/v1/people/{player_id}"

#: The tuple order of a lineup row, as ``_build_starting_lineup_rows`` returns it.
Row = tuple[int, int, int, int, int | None, str, bool, int]

GetJson = Callable[[str], Awaitable[Any]]


def projected_lineups_enabled() -> bool:
    """``LIVE_PROJECTED_LINEUPS`` (default 0, decision D3)."""
    return os.environ.get("LIVE_PROJECTED_LINEUPS", "0") == "1"


def lineup_signature(entry: Mapping[str, Any]) -> tuple[Any, ...]:
    """What the writer has seen of a game: both lineups' ids and both probables.

    A scratch, a re-ordered lineup or a new probable pitcher changes it, and
    only a change costs a feed read.
    """
    lineups = entry.get("lineups") or {}
    teams = entry.get("teams") or {}

    def ids(key: str) -> tuple[int, ...]:
        return tuple(int(p["id"]) for p in lineups.get(key) or () if p.get("id") is not None)

    def probable(side: str) -> int | None:
        pp = (teams.get(side) or {}).get("probablePitcher") or {}
        return int(pp["id"]) if pp.get("id") is not None else None

    return ids("awayPlayers"), ids("homePlayers"), probable("away"), probable("home")


def with_probable_pitchers(
    rows: Sequence[Row], game_pk: int, season: int, probables: Mapping[int, int]
) -> list[Row]:
    """Add each side's probable pitcher as its ``P`` row when the side has none.

    ``probables`` maps team_id → the probable pitcher's id. A probable who
    already has a row (a two-way starter batting as the DH) keeps that row.
    """
    out = list(rows)
    for team_id, pitcher_id in probables.items():
        side_rows = [r for r in out if r[2] == team_id]
        if any(r[5] == "P" for r in side_rows) or any(r[3] == pitcher_id for r in side_rows):
            continue
        out.append((game_pk, season, int(team_id), int(pitcher_id), None, "P", True, 1))
    return out


class PublishedLineupWriter:
    """Writes the published (or projected) lineup of a Preview game."""

    def __init__(self, db: Any, get_json: GetJson, *, projected: bool | None = None) -> None:
        self._db = db
        self._get_json = get_json
        self._projected = projected_lineups_enabled() if projected is None else projected
        self._seen: dict[int, tuple[Any, ...]] = {}

    async def on_preview(self, entry: Mapping[str, Any]) -> str | None:
        """Write the game's lineup when it is new or changed.

        Returns the source written (``published`` / ``projected``), or None
        when nothing was written. Never raises: a failure is logged and the
        next poll tries again.
        """
        try:
            return await self._on_preview(entry)
        except Exception as exc:  # noqa: BLE001 -- the poll must not stop on one game
            log.warning("lineup writer: game %s not written: %s", entry.get("gamePk"), exc)
            return None

    async def _on_preview(self, entry: Mapping[str, Any]) -> str | None:
        game = parse_game(entry)
        if card_state(game) != SCHEDULED:
            return None
        sig = lineup_signature(entry)
        if self._seen.get(game.game_pk) == sig:
            return None

        probables = {
            t.team_id: t.probable_pitcher.player_id
            for t in (game.away, game.home)
            if t.probable_pitcher is not None and t.team_id
        }
        async with self._db.acquire() as conn:
            if await conn.fetchval(
                "SELECT 1 FROM raw.game_lineups WHERE game_pk = $1 AND source = 'box' LIMIT 1",
                game.game_pk,
            ):
                self._seen[game.game_pk] = sig
                return None

        rows: list[Row] = []
        source: str | None = None
        if game.lineups_posted:
            feed = await self._get_json(FEED_URL.format(game_pk=game.game_pk))
            rows = [
                r
                for r in _build_starting_lineup_rows(game.game_pk, game.season, feed)
                if r[4] is not None
            ]
            if not rows:
                rows = self._rows_from_schedule(game)
            rows = with_probable_pitchers(rows, game.game_pk, game.season, probables)
            source = "published"
        elif self._projected and len(probables) == 2:
            rows = await self._projected_rows(game, probables)
            source = "projected" if rows else None
        if source is None or not rows:
            return None

        await self._ensure_players({r[3] for r in rows})
        async with self._db.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM raw.game_lineups WHERE game_pk = $1 AND source IN ('published', 'projected')",
                game.game_pk,
            )
            await conn.executemany(
                """
                INSERT INTO raw.game_lineups
                    (game_pk, season, team_id, player_id, batting_order,
                     position_code, is_starter, sequence, source, published_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
                ON CONFLICT (game_pk, team_id, player_id, sequence) DO NOTHING
                """,
                [(*r, source) for r in rows],
            )
        self._seen[game.game_pk] = sig
        log.info("lineup writer: game %s — %d %s rows", game.game_pk, len(rows), source)
        return source

    @staticmethod
    def _rows_from_schedule(game: Any) -> list[Row]:
        """The schedule's posted lineups, when the feed's box carries none.

        The schedule names each player's PRIMARY position, not today's, so this
        is the fallback only."""
        rows: list[Row] = []
        for team in (game.away, game.home):
            for i, p in enumerate(team.lineup):
                rows.append(
                    (
                        game.game_pk,
                        game.season,
                        team.team_id,
                        p.player_id,
                        i + 1,
                        (p.position or "UT")[:5],
                        True,
                        1,
                    )
                )
        return rows

    async def _projected_rows(self, game: Any, probables: Mapping[int, int]) -> list[Row]:
        """Each team's last final lineup (same opposing hand when one exists), with today's probable as P."""
        rows: list[Row] = []
        async with self._db.acquire() as conn:
            for team, opp in ((game.away, game.home), (game.home, game.away)):
                opp_pitcher = probables.get(opp.team_id)
                hand = None
                if opp_pitcher is not None:
                    hand = await conn.fetchval(
                        "SELECT throws FROM raw.players WHERE player_id = $1", opp_pitcher
                    )
                recent = await conn.fetch(
                    _SQL_RECENT_LINEUP_GAMES, team.team_id, game.official_date
                )
                chosen = None
                for r in recent:
                    if hand is not None and r["opp_throws"] == hand:
                        chosen = r["game_pk"]
                        break
                if chosen is None and recent:
                    chosen = recent[0]["game_pk"]
                if chosen is None:
                    return []
                batting = await conn.fetch(_SQL_LINEUP_BATTERS, chosen, team.team_id)
                rows.extend(
                    (
                        game.game_pk,
                        game.season,
                        team.team_id,
                        int(b["player_id"]),
                        int(b["batting_order"]),
                        str(b["position_code"]),
                        True,
                        1,
                    )
                    for b in batting
                )
        return with_probable_pitchers(rows, game.game_pk, game.season, probables)

    async def _ensure_players(self, player_ids: set[int]) -> None:
        """Add any player the database does not know, from the league's people endpoint."""
        async with self._db.acquire() as conn:
            known = {
                int(r["player_id"])
                for r in await conn.fetch(
                    "SELECT player_id FROM raw.players WHERE player_id = ANY($1::int[])",
                    list(player_ids),
                )
            }
        missing = sorted(player_ids - known)
        for pid in missing:
            payload = await self._get_json(PEOPLE_URL.format(player_id=pid))
            people = (payload or {}).get("people") or []
            if not people:
                raise ValueError(f"the league has no person {pid}")
            person = people[0]
            bats = (person.get("batSide") or {}).get("code")
            throws = (person.get("pitchHand") or {}).get("code")
            if not bats or not throws:
                raise ValueError(f"player {pid} has no handedness; not guessing it")
            name = person.get("fullName") or ""
            async with self._db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO raw.players (
                        player_id, full_name, first_name, last_name, birth_date,
                        bats, throws, primary_position, height_inches, weight_lbs, mlb_debut_date
                    ) VALUES ($1, $2, $3, $4, $5::date, $6, $7, $8, $9, $10, $11::date)
                    ON CONFLICT (player_id) DO NOTHING
                    """,
                    pid,
                    name,
                    person.get("firstName") or name,
                    person.get("lastName") or name,
                    person.get("birthDate"),
                    bats,
                    throws,
                    ((person.get("primaryPosition") or {}).get("abbreviation") or "UT")[:5],
                    _parse_height(person.get("height")),
                    person.get("weight"),
                    person.get("mlbDebutDate"),
                )
            log.info("lineup writer: added player %s (%s)", pid, name)


#: A team's last ten final games that have box lineup rows, newest first, with
#: the hand of the starter the team faced (``$1`` team_id, ``$2`` before date).
_SQL_RECENT_LINEUP_GAMES = """
    SELECT g.game_pk, g.game_date, opp.throws AS opp_throws
      FROM raw.games g
      LEFT JOIN LATERAL (
            SELECT p.throws
              FROM raw.game_lineups gl
              JOIN raw.players p ON p.player_id = gl.player_id
             WHERE gl.game_pk = g.game_pk AND gl.team_id <> $1
               AND gl.position_code = 'P' AND gl.sequence = 1 AND gl.source = 'box'
             LIMIT 1
      ) opp ON TRUE
     WHERE g.status = 'Final' AND g.game_date < $2
       AND $1 IN (g.home_team_id, g.away_team_id)
       AND EXISTS (SELECT 1 FROM raw.game_lineups x
                    WHERE x.game_pk = g.game_pk AND x.team_id = $1 AND x.source = 'box')
     ORDER BY g.game_date DESC, g.game_pk DESC
     LIMIT 10
"""

#: A game's starting nine for one team, in batting order.
_SQL_LINEUP_BATTERS = """
    SELECT player_id, batting_order, position_code
      FROM raw.game_lineups
     WHERE game_pk = $1 AND team_id = $2 AND sequence = 1
       AND batting_order IS NOT NULL AND source = 'box'
     ORDER BY batting_order
"""
