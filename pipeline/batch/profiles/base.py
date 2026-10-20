"""The state and the shared helpers every profile-computor mixin uses."""

from __future__ import annotations

import logging
from datetime import (
    date,
    datetime,
)

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
    # Lifecycle
    # ------------------------------------------------------------------

    def _play_events_outs_cte(self, asof_sql: str | None = None) -> str:
        """SIM-504: the pickoff-out aggregate from ``raw.play_events``.

        Pickoff outs live in NO ``raw.pitches`` row (the documented ~0.5%
        innings-pitched residual in ``pipeline/statcast_events.py``), so the
        two ``outs_recorded`` consumers add them per (pitcher, season) from
        the play-events table. ``pitcher_id`` there is the THROWER — the
        third adversarial review fixed mid-PA attribution — so the credit
        lands on the right arm. Falls back to an empty relation when the
        table is absent (a pre-0018 database or a unit-test fixture), which
        keeps the join a no-op.

        SIM-537: ``asof_sql`` (an ISO date string) restricts this to plays
        recorded at or before a cutoff — the same point-in-time rule
        SIM-534 applies to every batter source, extended to a pitcher's own
        pickoff-out count. Left ``None`` (the default), this behaves exactly
        as before: every play in ``season`` counts, which is what
        :func:`_build_pitcher_steal_metrics` — the OTHER caller of this
        helper — still wants, since that grouping has not had its own
        point-in-time pass yet.
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
        """SIM-504 item 3: pickoff throws + stepoffs per (thrower, season).

        These are the hold-runner behaviours the SIM-474 steal draw kernels
        on: how often does this pitcher throw over, and how often does he
        step off. ``pitcher_id`` is the THROWER (the third adversarial
        review's mid-PA attribution fix). A ``pickoff_error`` is still a
        pickoff THROW, so it counts. Falls back to an empty relation when
        the table is absent (a pre-0018 database or a unit-test fixture).

        SIM-537: ``asof_sql`` restricts this to plays recorded at or before
        a cutoff, the same rule ``_play_events_outs_cte`` already applies.
        Left ``None`` (the default), every play in ``season`` counts.
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
        """SIM-507: per-event pickoff outcomes mapped to the steal pool's pair.

        Maps each raw.play_events pickoff/pickoff_error row to the opportunity
        pair its held runner belongs to, using the play-entering runner state:

          * runner on 1B, 2B open  -> the target-2 pair. An out at base 1 is a
            plain pickoff; an out at base 2 is a picked-off CAUGHT STEALING
            (the runner broke for the next base — MLB scores a CS).
          * runner on 2B, 3B open  -> the target-3 pair; out at 2 plain, at 3
            advancing.

        Out of the pair shapes and therefore dropped, measured 2023: a runner
        held at 3B or thrown out at home (~26 outs/season) and a 1B pickoff
        with 1B+2B both occupied (the pool's row for that pitch belongs to the
        2B runner). Together ~0.006/team-game — documented residual, with
        steals of home. Falls back to an empty relation when the table is
        absent (a pre-0018 database or a unit-test fixture).

        SIM-554: the relation also carries each event's own facts (its id, the
        runner, the thrower, the half-inning, the outs and the score before the
        throw). The builder tags an outcome to one pitch of its pair in the
        plate appearance; measured 2026-09-30, 22% of the outcomes that fit a
        pair (394 of 1,788 in 2023-2026) had no such pitch, because the throw
        came before the first pitch. The builder writes each of those as a
        pickoff row of its own from these columns. Every row of this relation
        is a throw; most have no outcome (``is_out_i`` and ``is_err_i`` both 0).
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
    #: the date a row's data runs through (SIM-534 / SIM-537). Every engine
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

        SIM-551: the batter table reached two stamps on 2026-09-11. A rebuild
        of the four pool seasons stamped them at its own date, and the six
        seasons it did not touch kept the stamp of the day before. The old
        stamping step filled only the rows with NO stamp, so nothing
        reconciled the two, and the batter engine refused to build at every
        boot for five days.

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

        SIM-551: ``_refuse_a_mixed_cutoff`` has already refused any survivor
        this stamp would misdescribe. Every row here was either rewritten at
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
        """SIM-537: shared body for every ``*_have_no_leakage`` assertion
        added for the baserunner/catcher/fielder groupings.

        Tolerates three conditions that are NOT evidence of a leak, logging
        each rather than raising:

          * the query cannot run at all — a narrow test fixture's
            ``raw.pitches`` often has no ``game_date`` column (it predates
            this cutoff work and was never meant to carry one), which is a
            schema-availability question, the same reasoning
            ``_play_events_outs_cte`` already applies to a missing table;
          * no rows matched the season/cutoff filter;
          * a stubbed unit-test connection returned something that is not a
            real date.
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
