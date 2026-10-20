"""The state and the shared helpers every profile-computor mixin uses."""

from __future__ import annotations

import logging
from datetime import date, datetime

import duckdb

from db.introspect import duck_relation_readable

log = logging.getLogger("player_profile_computor")


class ComputorBase:
    """The computor's state and the helpers every builder shares: the
    play-event common table expressions, the one-cutoff-per-table guard and the
    leakage check. Every mixin subclasses this class, so each sees the state.
    """

    _pg_dsn: str
    _duckdb_path: str
    _conn: duckdb.DuckDBPyConnection | None
    _re_matrix: dict

    # ------------------------------------------------------------------
    # The play-event common table expressions
    # ------------------------------------------------------------------

    def _play_events_outs_cte(self, asof_sql: str | None = None) -> str:
        """The pickoff outs per (pitcher, season), from ``raw.play_events``.

        A pickoff out lives in no ``raw.pitches`` row (the innings-pitched
        residual documented in ``pipeline/statcast_events.py``), so the two
        ``outs_recorded`` readers add these counts. ``pitcher_id`` there is
        the pitcher who threw, so the out lands on the right arm. The relation
        is empty when the table is absent (a database before Alembic 0018, or
        a unit-test fixture), so the join adds nothing.

        ``asof_sql`` (an ISO date) keeps only the plays at or before the
        cutoff; ``None`` keeps every play of the season.
        """
        if not duck_relation_readable(self._conn, "pg.raw.play_events"):
            return (
                "SELECT NULL::INTEGER AS pitcher_id, NULL::SMALLINT AS season, "
                "0 AS n_outs WHERE FALSE"
            )
        cutoff = f"AND game_date <= DATE '{asof_sql}'" if asof_sql is not None else ""
        return (
            "SELECT pitcher_id::INTEGER AS pitcher_id, season::SMALLINT AS season, "
            "COUNT(*) AS n_outs FROM pg.raw.play_events "
            f"WHERE is_out {cutoff} GROUP BY pitcher_id, season"
        )

    def _play_events_disengagement_cte(self, asof_sql: str | None = None) -> str:
        """The pickoff throws and stepoffs per (pitcher, season).

        These are the pitcher's hold-runner habits: how often he throws over
        and how often he steps off. ``pitcher_id`` is the pitcher who threw.
        A ``pickoff_error`` is still a throw, so it counts. The relation is
        empty when the table is absent (a database before Alembic 0018, or a
        unit-test fixture).

        ``asof_sql`` keeps only the plays at or before the cutoff; ``None``
        keeps every play of the season.
        """
        if not duck_relation_readable(self._conn, "pg.raw.play_events"):
            return (
                "SELECT NULL::INTEGER AS pitcher_id, NULL::SMALLINT AS season, "
                "0 AS n_pickoffs, 0 AS n_stepoffs WHERE FALSE"
            )
        cutoff = f"AND game_date <= DATE '{asof_sql}'" if asof_sql is not None else ""
        return (
            "SELECT pitcher_id::INTEGER AS pitcher_id, season::SMALLINT AS season, "
            "SUM(CASE WHEN event_type IN ('pickoff', 'pickoff_error') "
            "THEN 1 ELSE 0 END) AS n_pickoffs, "
            "SUM(CASE WHEN event_type = 'stepoff' THEN 1 ELSE 0 END) AS n_stepoffs "
            f"FROM pg.raw.play_events WHERE pitcher_id IS NOT NULL {cutoff} "
            "GROUP BY pitcher_id, season"
        )

    def _pickoff_outcomes_cte(self, season_list: str) -> str:
        """Each pickoff throw, mapped to the steal pool's pair of its held runner.

        The runner state entering the play picks the pair:

          * a runner on 1B with 2B open -> the target-2 pair. An out at base 1
            is a plain pickoff; an out at base 2 is a picked-off caught
            stealing (the runner broke for the next base, and MLB scores a CS).
          * a runner on 2B with 3B open -> the target-3 pair: an out at 2 is
            plain, an out at 3 advancing.

        The throws that fit no pair are dropped: a runner held at 3B or thrown
        out at home (about 26 outs a season, measured on 2023) and a 1B pickoff
        with 1B and 2B both taken (the pool's row for that pitch belongs to the
        2B runner). Together they come to about 0.006 a team-game, a residual
        documented with the steals of home. The relation is empty when the
        table is absent (a database before Alembic 0018, or a unit-test fixture).

        Each row also carries the throw's own facts (its id, the runner, the
        pitcher, the half-inning, the outs and the score before the throw).
        The pool builder tags an outcome to one pitch of its pair in the plate
        appearance. An outcome with no such pitch (the throw came before the
        first pitch: 394 of 1,788 in 2023-2026, measured 2026-09-30) becomes a
        pickoff row of its own, built from these columns (the running game on
        the pitch, SIM-554). Every row is a throw; most have no outcome
        (``is_out_i`` and ``is_err_i`` both 0).
        """
        if not duck_relation_readable(self._conn, "pg.raw.play_events"):
            return (
                "SELECT NULL::BIGINT AS id, NULL::INTEGER AS game_pk, "
                "NULL::INTEGER AS at_bat_number, NULL::DATE AS game_date, "
                "NULL::SMALLINT AS season, NULL::INTEGER AS runner_id, "
                "NULL::INTEGER AS pitcher_id, NULL::SMALLINT AS inning, "
                "NULL::VARCHAR AS inning_topbot, NULL::SMALLINT AS outs_before, "
                "NULL::SMALLINT AS bat_score, NULL::SMALLINT AS fld_score, "
                "NULL::SMALLINT AS target_base, 0 AS is_out_i, 0 AS is_adv_i, "
                "0 AS is_err_i WHERE FALSE"
            )
        return f"""
                SELECT
                    id, game_pk, at_bat_number, game_date, season,
                    runner_id, pitcher_id, inning, inning_topbot,
                    outs_before, bat_score, fld_score,
                    CASE
                        WHEN base = 1 AND (runners_state & 1) = 1
                             AND (runners_state & 2) = 0 THEN 2
                        WHEN base = 2 AND (runners_state & 1) = 1
                             AND (runners_state & 2) = 0 THEN 2
                        WHEN base = 2 AND (runners_state & 2) = 2
                             AND (runners_state & 4) = 0 THEN 3
                        WHEN base = 3 AND (runners_state & 2) = 2
                             AND (runners_state & 4) = 0 THEN 3
                    END::SMALLINT AS target_base,
                    CASE WHEN event_type = 'pickoff' AND is_out
                         THEN 1 ELSE 0 END AS is_out_i,
                    CASE WHEN event_type = 'pickoff' AND is_out AND (
                              (base = 2 AND (runners_state & 1) = 1
                                        AND (runners_state & 2) = 0)
                           OR (base = 3 AND (runners_state & 2) = 2
                                        AND (runners_state & 4) = 0))
                         THEN 1 ELSE 0 END AS is_adv_i,
                    CASE WHEN event_type = 'pickoff_error'
                         THEN 1 ELSE 0 END AS is_err_i
                FROM pg.raw.play_events
                WHERE event_type IN ('pickoff', 'pickoff_error')
                  AND season IN ({season_list})
        """

    # ------------------------------------------------------------------
    # One cutoff per profile table (SIM-551)
    # ------------------------------------------------------------------

    #: The profile tables that carry a point-in-time stamp. ``asof_date`` is
    #: the date a row's data runs through. Every engine
    #: that reads one of these tables refuses a set of rows stamped at two
    #: different dates, so a build that leaves two stamps behind takes that
    #: engine down at the next boot. A unit test keeps this list equal to the
    #: tables that declare the column in ``02_duckdb_schema.sql``.
    STAMPED_TABLES: tuple[str, ...] = (
        "derived.pitcher_season_metrics",
        "derived.batter_season_metrics",
        "derived.baserunner_season_metrics",
        "derived.baserunner_steal_metrics",
        "derived.pitcher_steal_metrics",
        "derived.manager_season_metrics",
        "derived.fielder_season_metrics",
        "derived.catcher_season_metrics",
    )

    def _refuse_a_mixed_cutoff(self, table: str, seasons: list[int], asof_date: date) -> None:
        """Refuse a rebuild of ``seasons`` that would leave ``table`` at two
        cutoffs. The check runs BEFORE the delete and the insert, so a
        refusal leaves the table exactly as it found it.

        A table at two stamps takes its engine down at the next boot (the
        one-cutoff rule, SIM-551).

        The rule: after the build, every row must hold data through the new
        cutoff and none after it. The rows the build rewrites hold that by
        construction. A row the build leaves alone (a "survivor") holds it
        only when its season ended before the cutoff and the row is complete:

        * a survivor already stamped at the cutoff — nothing to check;
        * a survivor of the cutoff's own season — stale, or it leaks the
          future; refuse and name the season to add to the run;
        * a survivor of an earlier season with no stamp (built before the
          stamps existed, so a live build) or stamped after its season's last
          game — complete; ``_stamp_one_cutoff`` re-stamps it at the new
          cutoff, which says the true thing: as of that date, this is the
          whole season;
        * a survivor stamped inside its own season, before its last game — a
          truncated point-in-time build; refuse.
        """
        if not seasons:
            return
        season_list = ", ".join(str(int(s)) for s in seasons)
        try:
            survivors = self._conn.execute(f"""
                SELECT season, asof_date, COUNT(*)
                FROM {table}
                WHERE season NOT IN ({season_list}) AND season <= {asof_date.year}
                GROUP BY 1, 2
                ORDER BY 1, 2
            """).fetchall()
        except duckdb.Error as exc:
            raise RuntimeError(
                f"{table} cannot be checked for a mixed cutoff ({exc}). Apply the DuckDB "
                "migrations (0026-0028 add the asof_date stamp) before rebuilding it."
            ) from exc
        problems: list[str] = []
        for season, stamp, n_rows in survivors:
            season = int(season)
            if isinstance(stamp, datetime):
                stamp = stamp.date()
            if stamp == asof_date:
                continue
            if season == asof_date.year:
                problems.append(
                    f"season {season} carries {n_rows} rows as of {stamp}, and this build is "
                    f"as of {asof_date} — add {season} to the seasons, or build as of {stamp}"
                )
            elif stamp is None or stamp.year > season:
                continue  # a completed season, whole at any later date
            elif stamp.year == season:
                last_game = self._season_last_game_date(season)
                if last_game is not None and stamp >= last_game:
                    continue  # stamped in the off-season: the whole season
                problems.append(
                    f"season {season} carries {n_rows} rows as of {stamp}, inside its own "
                    f"season (last game {last_game or 'unknown'}) — a truncated build; "
                    f"add {season} to the seasons to rebuild it whole"
                )
            else:
                problems.append(
                    f"season {season} carries {n_rows} rows stamped {stamp}, before the "
                    f"season began — add {season} to the seasons to rebuild it"
                )
        if problems:
            raise RuntimeError(
                f"{table}: a rebuild of seasons [{season_list}] as of {asof_date} would leave "
                f"the table at two cutoffs (the engine refuses a mixed set): " + "; ".join(problems)
            )

    def _season_last_game_date(self, season: int) -> date | None:
        """The date of ``season``'s last game in ``raw.pitches``, or None when
        the source cannot answer (a narrow test fixture with no game_date)."""
        try:
            row = self._conn.execute(
                f"SELECT MAX(game_date) FROM pg.raw.pitches WHERE season = {int(season)}"
            ).fetchone()
        except Exception:  # noqa: BLE001 — a fixture's schema, not a data fact
            return None
        value = row[0] if row else None
        if isinstance(value, datetime):
            return value.date()
        return value if isinstance(value, date) else None

    def _stamp_one_cutoff(self, table: str, asof_sql: str) -> None:
        """Stamp every row of ``table`` at the build's cutoff — the whole
        table, not only the rows with no stamp yet.

        ``_refuse_a_mixed_cutoff`` has already refused any survivor this
        stamp would misdescribe. Every row here was either rewritten at
        the cutoff or is a completed season, which is whole as of any later
        date. Stamping the survivors says that: as of the cutoff, this is
        what we know.
        """
        self._conn.execute(f"""
            UPDATE {table}
            SET asof_date = DATE '{asof_sql}'
            WHERE asof_date IS DISTINCT FROM DATE '{asof_sql}'
        """)
        log.info("  %s stamped as of %s (one cutoff).", table, asof_sql)

    def _check_leakage(self, source: str, sql: str, asof: date) -> None:
        """The shared body of the baserunner, catcher and fielder
        ``*_have_no_leakage`` checks: raise when ``sql``'s newest date passes
        the cutoff.

        Three answers are not evidence of a leak; each is logged, not raised:

          * the query cannot run: a narrow test fixture's ``raw.pitches``
            often has no ``game_date`` column, a question of the schema, not
            of the data;
          * no rows matched the season and cutoff filter;
          * a stubbed unit-test connection returned something that is not a
            date.
        """
        try:
            newest = self._conn.execute(sql).fetchone()[0]
        except Exception as exc:  # noqa: BLE001 - a fixture's schema, not a real leak
            log.warning("    leakage check SKIPPED for %s: query failed (%s)", source, exc)
            return
        if newest is None:
            log.info("    no leakage from %-28s (no rows)", source)
            return
        if not isinstance(newest, date):
            log.warning("    leakage check SKIPPED for %s: got %r, not a date", source, newest)
            return
        if newest > asof:
            raise RuntimeError(
                f"data leakage: {source} contributed a row dated {newest}, after the {asof} cutoff."
            )
        log.info("    no leakage from %-28s newest = %s", source, newest)
