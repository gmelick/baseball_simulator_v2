"""Write the reference rows a game needs before its pitches: the teams, the managers, the
game row and the starting lineups.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

from ingest.games.base import LoaderBase
from ingest.games.game_values import (
    _build_starting_lineup_rows,
    _map_game_status,
    _parse_wind,
)
from ingest.games.venues import (
    MissingVenueError,
    _resolve_venue_id,
)

log = logging.getLogger(__name__)


class ReferenceRowsMixin(LoaderBase):
    """Upserts ``raw.teams``, ``raw.managers``, ``raw.games`` and ``raw.game_lineups``."""

    if TYPE_CHECKING:
        # Defined on HistoricalDataLoader, beside the HTTP calls the tests patch.
        def _ensure_venue(self, venue_id: int, season: int, *, force: bool = False) -> None: ...

    def _ensure_teams(self, home_team_id: int, away_team_id: int, season: int, gd: dict) -> None:
        """Upsert ``raw.teams`` for each of the game's two teams that is missing.

        The feed carries every team value. ``_ensure_venue`` first makes sure the
        team's home park exists.
        """
        needed = {home_team_id, away_team_id}
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT team_id FROM raw.teams WHERE season = %s AND team_id = ANY(%s)",
                (
                    season,
                    list(needed),
                ),
            )
            existing = {r[0] for r in cur.fetchall()}

        missing = needed - existing
        if not missing:
            return

        # The feed's division name → the raw.teams value.
        div_map = {
            "American League East": "AL East",
            "American League Central": "AL Central",
            "American League West": "AL West",
            "National League East": "NL East",
            "National League Central": "NL Central",
            "National League West": "NL West",
        }

        rows = []
        for side in ("home", "away"):
            t = gd["teams"][side]
            tid = t["id"]
            if tid not in missing:
                continue

            league_name = t.get("league", {}).get("name", "")
            league_code = "AL" if "American" in league_name else "NL"
            division = div_map.get(t.get("division", {}).get("name", ""), "AL East")
            # The team's OWN venue is its home park, the right value for
            # raw.teams. The game's venue is only a fallback, and a neutral-site
            # game may have none, so the lookup is guarded.
            venue_id = t.get("venue", {}).get("id") or (gd.get("venue") or {}).get("id")
            if venue_id is None:
                raise MissingVenueError(
                    f"team {tid} is missing from raw.teams and neither it nor the game "
                    f"feed carries a venue, so its home park cannot be established. "
                    f"Backfill the team first, or register the game in _VENUE_OVERRIDES."
                )
            self._ensure_venue(venue_id, season)

            rows.append(
                (tid, season, t["name"], t["abbreviation"], league_code, division, venue_id)
            )

        if not rows:
            return

        log.info("  Inserting missing teams: %s", [r[0] for r in rows])
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                for row in rows:
                    cur.execute(
                        """
                        INSERT INTO raw.teams
                            (team_id, season, team_name, team_abbrev, league, division, venue_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (team_id, season) DO UPDATE SET
                            team_name   = EXCLUDED.team_name,
                            team_abbrev = EXCLUDED.team_abbrev,
                            updated_at  = NOW()
                        """,
                        row,
                    )
            conn.commit()

    def _ensure_managers(
        self, managers: dict, home_team_id: int, away_team_id: int, season: int, game_date: str
    ) -> None:
        """Upsert ``raw.managers`` for the game's home and away managers.

        The key is ``(manager_id, season)``. A NULL ``season_end`` means "manages
        the team now" (``idx_managers_active`` indexes it). A conflict resets
        ``season_end`` to NULL: a manager seen in the dugout again is active, which
        records a return from a suspension or other missed time.

        A new manager closes the active stint of the team's incumbent on the
        game's date.
        """
        candidates = [
            (managers["home_manager_id"], managers["home_manager_name"], home_team_id),
            (managers["away_manager_id"], managers["away_manager_name"], away_team_id),
        ]
        # The feed may name no manager (an empty id).
        candidates = [(mid, name, tid) for mid, name, tid in candidates if mid]

        if not candidates:
            return
        check_managers = [mid for mid, _, _ in candidates]
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    SELECT manager_id, season
                    FROM raw.managers
                    WHERE season = %s AND manager_id = ANY(%s)
                    """,
                (season, check_managers),
            )
            existing = {(r[0], r[1]) for r in cur.fetchall()}

        missing = [
            (mid, name, tid) for mid, name, tid in candidates if (mid, season) not in existing
        ]
        if not missing:
            return

        log.info("  Inserting missing managers: %s", [(m[0], m[2]) for m in missing])
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                for mid, name, tid in missing:
                    # The ACTIVE incumbent to hand over from. Only a row with a
                    # NULL season_end is active, and the newest stint is the one
                    # replaced: with three managers in a season, any other
                    # choice closes the wrong stint.
                    cur.execute(
                        """
                        SELECT manager_id
                        FROM   raw.managers
                        WHERE  manager_id <> %s
                          AND  season     = %s
                          AND  team_id    = %s
                          AND  season_end IS NULL
                        ORDER BY season_start DESC NULLS LAST
                        LIMIT 1
                        """,
                        (mid, season, tid),
                    )
                    m = cur.fetchone()
                    if m is not None:
                        cur.execute(
                            """
                            UPDATE raw.managers
                               SET season_end = %s,
                                   updated_at = NOW()
                             WHERE manager_id = %s
                               AND season     = %s
                               AND team_id    = %s
                            """,
                            (date.fromisoformat(game_date), m[0], season, tid),
                        )
                        cur.execute(
                            """
                            INSERT INTO raw.managers
                                (manager_id, season, full_name, team_id, season_start)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (manager_id, season) DO UPDATE SET
                                full_name    = EXCLUDED.full_name,
                                team_id      = EXCLUDED.team_id,
                                season_start = COALESCE(raw.managers.season_start,
                                                        EXCLUDED.season_start),
                                season_end   = NULL,
                                updated_at   = NOW()
                            """,
                            (mid, season, name, tid, date.fromisoformat(game_date)),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO raw.managers
                                (manager_id, season, full_name, team_id)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (manager_id, season) DO UPDATE SET
                                full_name  = EXCLUDED.full_name,
                                team_id    = EXCLUDED.team_id,
                                season_end = NULL,
                                updated_at = NOW()
                            """,
                            (mid, season, name, tid),
                        )
            conn.commit()

    def _ensure_game(self, game_pk: int, season: int, game_dict: dict, managers: dict) -> None:
        """Upsert the ``raw.games`` row, after its venue, teams and managers exist.

        A completed game also gets its final score, its inning scores and the
        winning, losing and save pitchers. They come from ``liveData``: the
        linescore and the decisions live there, not in ``gameData``. The upsert
        always runs, so a re-fetch of a loaded game fills in its final score.
        """
        gd = game_dict["gameData"]
        live = game_dict.get("liveData", {})

        game_date = gd["datetime"]["officialDate"]
        venue_id = _resolve_venue_id(game_pk, gd)
        home_team_id = gd["teams"]["home"]["id"]
        away_team_id = gd["teams"]["away"]["id"]
        game_type = gd.get("game", {}).get("type", "R")
        status = _map_game_status(gd)

        # The final score, present once the game is complete.
        linescore = live.get("linescore", {})
        home_score = linescore.get("teams", {}).get("home", {}).get("runs")
        away_score = linescore.get("teams", {}).get("away", {}).get("runs")
        home_hits = linescore.get("teams", {}).get("home", {}).get("hits")
        away_hits = linescore.get("teams", {}).get("away", {}).get("hits")
        home_errors = linescore.get("teams", {}).get("home", {}).get("errors")
        away_errors = linescore.get("teams", {}).get("away", {}).get("errors")
        innings = linescore.get("currentInning")

        # The inning-by-inning scores, stored as JSONB.
        innings_data = linescore.get("innings", [])
        inning_scores = (
            {
                "home": [i.get("home", {}).get("runs") for i in innings_data],
                "away": [i.get("away", {}).get("runs") for i in innings_data],
            }
            if innings_data
            else None
        )

        # The winning, losing and save pitchers (under liveData.decisions).
        decisions = live.get("decisions", {})
        winning_pid = decisions.get("winner", {}).get("id")
        losing_pid = decisions.get("loser", {}).get("id")
        save_pid = decisions.get("save", {}).get("id")

        # The weather (this one IS under gameData).
        weather = gd.get("weather", {})
        wind_speed, wind_direction = _parse_wind(weather.get("wind"))

        log.info("  Upserting game record %s", game_pk)
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                import json as _json

                cur.execute(
                    """
                    INSERT INTO raw.games (
                        game_pk, season, game_date, game_type, status,
                        venue_id, home_team_id, away_team_id,
                        home_manager_id, away_manager_id,
                        home_score_final, away_score_final,
                        innings_played,
                        home_hits, away_hits, home_errors, away_errors,
                        inning_scores,
                        winning_pitcher_id, losing_pitcher_id, save_pitcher_id,
                        weather_temp, weather_condition,
                        wind_speed, wind_direction
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s
                    )
                    -- SIM-440: refresh ALL 24 non-key columns on conflict.
                    --
                    -- This clause used to cover 6 of 25. Everything else was
                    -- computed correctly above, bound into the INSERT, and then
                    -- silently discarded on every re-fetch — so a game loaded
                    -- once could never have its innings, hits, errors, inning
                    -- scores, weather, venue or manager ids corrected, on any
                    -- code path in the repo.
                    --
                    -- Two update policies, deliberately different:
                    --
                    --   EXCLUDED.x            (unconditional overwrite) for the
                    --     identity/classification columns this loader is the
                    --     AUTHORITY for. It reads gameData.datetime.officialDate;
                    --     the live pipeline writes the UTC `gameDate` instead, so
                    --     a CT/MT/PT night game it created is stamped with the
                    --     NEXT calendar day. COALESCE would preserve that wrong
                    --     value forever, because it is wrong but not NULL.
                    --
                    --   COALESCE(EXCLUDED.x, raw.games.x)  for enrichment that
                    --     only exists once a game is Final. A scheduled or
                    --     in-progress payload carries NULLs there, and must never
                    --     blank out a good stored value.
                    ON CONFLICT (game_pk) DO UPDATE SET
                        season             = EXCLUDED.season,
                        game_date          = EXCLUDED.game_date,
                        game_type          = EXCLUDED.game_type,
                        status             = EXCLUDED.status,
                        venue_id           = COALESCE(EXCLUDED.venue_id,           raw.games.venue_id),
                        home_team_id       = COALESCE(EXCLUDED.home_team_id,       raw.games.home_team_id),
                        away_team_id       = COALESCE(EXCLUDED.away_team_id,       raw.games.away_team_id),
                        home_manager_id    = COALESCE(EXCLUDED.home_manager_id,    raw.games.home_manager_id),
                        away_manager_id    = COALESCE(EXCLUDED.away_manager_id,    raw.games.away_manager_id),
                        home_score_final   = COALESCE(EXCLUDED.home_score_final,   raw.games.home_score_final),
                        away_score_final   = COALESCE(EXCLUDED.away_score_final,   raw.games.away_score_final),
                        innings_played     = COALESCE(EXCLUDED.innings_played,     raw.games.innings_played),
                        home_hits          = COALESCE(EXCLUDED.home_hits,          raw.games.home_hits),
                        away_hits          = COALESCE(EXCLUDED.away_hits,          raw.games.away_hits),
                        home_errors        = COALESCE(EXCLUDED.home_errors,        raw.games.home_errors),
                        away_errors        = COALESCE(EXCLUDED.away_errors,        raw.games.away_errors),
                        inning_scores      = COALESCE(EXCLUDED.inning_scores,      raw.games.inning_scores),
                        winning_pitcher_id = COALESCE(EXCLUDED.winning_pitcher_id, raw.games.winning_pitcher_id),
                        losing_pitcher_id  = COALESCE(EXCLUDED.losing_pitcher_id,  raw.games.losing_pitcher_id),
                        save_pitcher_id    = COALESCE(EXCLUDED.save_pitcher_id,    raw.games.save_pitcher_id),
                        weather_temp       = COALESCE(EXCLUDED.weather_temp,       raw.games.weather_temp),
                        weather_condition  = COALESCE(EXCLUDED.weather_condition,  raw.games.weather_condition),
                        wind_speed         = COALESCE(EXCLUDED.wind_speed,         raw.games.wind_speed),
                        wind_direction     = COALESCE(EXCLUDED.wind_direction,     raw.games.wind_direction),
                        updated_at         = NOW()
                    """,
                    (
                        game_pk,
                        season,
                        date.fromisoformat(game_date),
                        game_type,
                        status,
                        venue_id,
                        home_team_id,
                        away_team_id,
                        managers["home_manager_id"] or None,
                        managers["away_manager_id"] or None,
                        home_score,
                        away_score,
                        innings,
                        home_hits,
                        away_hits,
                        home_errors,
                        away_errors,
                        _json.dumps(inning_scores) if inning_scores else None,
                        winning_pid,
                        losing_pid,
                        save_pid,
                        weather.get("temp"),
                        weather.get("condition"),
                        wind_speed,
                        wind_direction,
                    ),
                )
            conn.commit()

    def _ensure_game_lineups(self, game_pk: int, season: int, game_dict: dict) -> None:
        """Insert the starting-lineup rows into ``raw.game_lineups``.

        ``ON CONFLICT DO NOTHING`` on the natural key keeps a re-fetch from
        duplicating rows. :mod:`simulation.lineup_resolver` reads these rows to
        build the opening ``GameState`` of ``/simulate``.
        """
        rows = _build_starting_lineup_rows(game_pk, season, game_dict)
        if not rows:
            return
        with self._get_conn() as conn, conn.cursor() as cur:
            # The live service writes a game's published (or projected) lineup
            # before the first pitch. The final box score is the authority, so
            # this transaction deletes those rows first. Otherwise a pre-game row
            # keeps the unique slot, and a player scratched after the last
            # pre-game poll stays in the lineup.
            if self._exists_once(
                cur, "_lineup_source_column_exists", "raw.game_lineups", column="source"
            ):
                cur.execute(
                    "DELETE FROM raw.game_lineups WHERE game_pk = %s AND source <> 'box'",
                    (game_pk,),
                )
            cur.executemany(
                """
                INSERT INTO raw.game_lineups
                    (game_pk, season, team_id, player_id, batting_order,
                     position_code, is_starter, sequence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (game_pk, team_id, player_id, sequence) DO NOTHING
                """,
                rows,
            )
            conn.commit()
        log.info("  Inserted %d starting-lineup rows for game %s", len(rows), game_pk)

    #: Whether ``raw.game_lineups.source`` exists: ``None`` until the first probe
    #: (see ``LoaderBase._exists_once``).
    _lineup_source_column_exists: bool | None = None
