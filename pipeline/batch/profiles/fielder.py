"""The fielder season profile: the aggregate of the per-play metrics and the
outfield arm block.
"""

from __future__ import annotations

import logging
from datetime import date

from db.introspect import duck_table_exists
from pipeline.batch.profiles.base import ComputorBase
from pipeline.batch.profiles.constants import MIN_FIELDER_PLAYS
from pipeline.batch.profiles.defense import (
    RUNS_PER_OAA_INFIELD,
    RUNS_PER_OAA_OUTFIELD,
)
from pipeline.batch.profiles.sql import _SQL_IS_OF

log = logging.getLogger("player_profile_computor")


class FielderProfilesMixin(ComputorBase):
    """Builds ``derived.fielder_season_metrics``."""

    # ------------------------------------------------------------------
    # AGGREGATE SEASON-LEVEL METRICS
    # ------------------------------------------------------------------

    def _ensure_fielder_temp_tables_exist(self) -> None:
        """Create empty `_tmp_*` placeholders for any temp table the
        fielder aggregator JOINs against that hasn't been created yet
        (e.g. because the upstream compute step returned early on a
        sample-size guard).

        Columns match what the aggregator's CTEs SELECT — the goal is
        to make the JOIN a no-op when the corresponding compute step
        was skipped, NOT to substitute real data.
        """
        # Each entry: (table_name, CREATE TABLE DDL with the columns
        # the aggregator references).  We use CREATE TABLE IF NOT EXISTS
        # so this is idempotent: if the compute step DID create the
        # table, this no-ops and the real data is preserved.
        placeholders = {
            # Outfield catch probability — _compute_outfield_catch_probability
            "_tmp_of_plays": """
                CREATE TABLE IF NOT EXISTS _tmp_of_plays (
                    fielder_id INTEGER, position VARCHAR, season SMALLINT,
                    catch_probability FLOAT, caught BOOLEAN,
                    oaa_credit FLOAT, direction_cat VARCHAR, star_rating INTEGER
                )
            """,
            # Infield OAA — _compute_infield_oaa
            "_tmp_if_plays": """
                CREATE TABLE IF NOT EXISTS _tmp_if_plays (
                    fielder_id INTEGER, position VARCHAR, season SMALLINT,
                    out_probability FLOAT, out_recorded BOOLEAN,
                    oaa_credit FLOAT, direction_category VARCHAR
                )
            """,
            # Double-play metrics — _compute_dp_metrics
            "_tmp_dp_plays": """
                CREATE TABLE IF NOT EXISTS _tmp_dp_plays (
                    initiator_id INTEGER, initiator_position VARCHAR,
                    pivot_id INTEGER, pivot_position VARCHAR,
                    season SMALLINT,
                    dp_probability FLOAT, dp_turned BOOLEAN,
                    dp_run_value FLOAT
                )
            """,
            # Errors — _compute_error_decomposition
            # Schema follows the SELECT shape inside the aggregator's `err` CTE
            # (SELECT * FROM _tmp_errors).  Keep the column set minimal but
            # include the foreign-key columns the outer SELECT joins on.
            #
            # SIM-542: these five names (fielding_error_count, throwing_error_count,
            # fielding_error_rate, throwing_error_rate — plus the unused
            # total_plays_fielded) must match _compute_error_decomposition's REAL
            # output columns exactly. This placeholder named them
            # fielding_errors/throwing_errors/total_plays instead, and was missing
            # fielding_error_rate/throwing_error_rate outright — silently correct
            # in production only because that builder has no sample-size guard and
            # always creates the real table first, so this placeholder's
            # IF NOT EXISTS never actually fires there. A caller that runs the
            # aggregator standalone hits `Binder Error: Values list "e" does not
            # have a column named "fielding_error_count"` the moment this
            # placeholder is the only table in play.
            "_tmp_errors": """
                CREATE TABLE IF NOT EXISTS _tmp_errors (
                    fielder_id INTEGER, position VARCHAR, season SMALLINT,
                    total_plays_fielded INTEGER, fielding_error_count INTEGER,
                    throwing_error_count INTEGER, fielding_error_rate FLOAT,
                    throwing_error_rate FLOAT, error_rate FLOAT
                )
            """,
            # Bunt defense — _compute_bunt_defense
            # SIM-542: same class of bug as _tmp_errors above — bunt_outs and
            # bunt_success_rate must be named bunt_outs_recorded and
            # bunt_fielding_rate to match what _compute_bunt_defense really
            # produces and what the aggregator's outer SELECT reads from `b`.
            "_tmp_bunt_defense": """
                CREATE TABLE IF NOT EXISTS _tmp_bunt_defense (
                    fielder_id INTEGER, position VARCHAR, season SMALLINT,
                    bunt_opportunities INTEGER, bunt_outs_recorded INTEGER,
                    bunt_fielding_rate FLOAT
                )
            """,
            # 1B scooping — _compute_first_base_scooping
            "_tmp_1b_scoop": """
                CREATE TABLE IF NOT EXISTS _tmp_1b_scoop (
                    season SMALLINT, fielder_id INTEGER, position VARCHAR,
                    scoop_opportunities INTEGER, scoop_successes INTEGER,
                    scoop_success_rate FLOAT
                )
            """,
        }
        for table, ddl in placeholders.items():
            try:
                self._conn.execute(ddl)
            except Exception as exc:  # noqa: BLE001  - defensive log + continue
                log.warning("  Could not ensure placeholder %s exists: %s", table, exc)

    def _aggregate_fielder_season_metrics(
        self, seasons: list[int], asof: date | None = None
    ) -> None:
        """
        Combine all per-play detail tables into the final
        derived.fielder_season_metrics rows.

        SIM-537: the six per-play temp tables are already cutoff-filtered
        by the time this method reads them (they carry no date of their
        own). Four Savant joins here have no date column at all — sprint
        speed, arm strength, outs above average and the outfield jump — so
        each follows the season-shift substitution described on
        ``_compute_baserunner_profiles``: a season that ENDED before the
        cutoff joins its own row; the season CONTAINING the cutoff joins
        the PRIOR season's row. Measured year-to-year correlations: the
        arm strength 0.857, the jump 0.80 to 0.92, Savant's outs above
        average per 100 chances 0.38 to 0.61 within an outfield position
        and 0.34 to 0.63 within an infield position (plan §2.2b), so the
        substitute costs little.

        The shifted numerator takes the shifted denominator. At a cutoff
        the per-100 figure divides the PRIOR season's outs by the PRIOR
        season's chances, never by this season's partial count. A cutoff
        build reads the prior season's chances from the build when that
        season is in ``seasons``, else from its surviving row in the table
        (the delete above removes only the run's seasons, and the SIM-551
        guard makes a survivor a completed season). With no prior-season
        chances anywhere the figure is NULL, and the model reads the league
        mean for it.

        SIM-550: the six outfield arm columns and ``of_arm_runs`` are
        written NULL here. ``_fill_outfield_arm_block`` fills the six from
        the advancement pool after the pools are built; ``of_arm_runs``
        stays NULL (the model does not read it).

        SIM-532: the six Savant columns come LAST, after ``asof_date``, in
        ``OAA_JUMP_COLUMN_ORDER``. The outs above average join is per
        position (the board is pulled once per position, so a player's
        figure differs between his LF and CF rows); the jump join is per
        player and the jump columns are NULL on an infield row.
        """
        log.info("Aggregating fielder season metrics …")
        season_list = ", ".join(str(s) for s in seasons)
        asof_date = asof or date.today()
        asof_sql = asof_date.isoformat()
        savant_season = (
            f"(CASE WHEN c.season = {asof_date.year} THEN c.season - 1 ELSE c.season END)"
            if asof is not None
            else "c.season"
        )
        # SIM-532: the per-100 denominator follows the numerator's season.
        # For the season that contains the cutoff, the outs come from the
        # prior season, so the chances must too: from the build (`prev`)
        # when the prior season is in the run, else from its surviving row
        # (`stored`). A live build divides by the row's own chances.
        savant_denominator = (
            f"(CASE WHEN c.season = {asof_date.year} "
            "THEN COALESCE(prev.opportunities, stored.opportunities) "
            "ELSE c.opportunities END)"
            if asof is not None
            else "c.opportunities"
        )

        # ----------------------------------------------------------------
        # Defensive: ensure every _tmp_* table the aggregator JOINs against
        # exists, even if the upstream _compute_* step returned early due to
        # the sample-size guard (e.g. _compute_outfield_catch_probability
        # bails out when len(df) < 500).  Without this step the aggregator
        # crashes with "Catalog Error: Table with name _tmp_of_plays does
        # not exist!" on small / partial data loads.  Creating an empty
        # placeholder with the right columns is a no-op JOIN target and
        # preserves the aggregator's shape.
        # ----------------------------------------------------------------
        self._ensure_fielder_temp_tables_exist()

        # SIM-551: refuse, before any write, a rebuild that would leave the
        # table at two cutoffs.
        self._refuse_a_mixed_cutoff("derived.fielder_season_metrics", seasons, asof_date)

        # Delete existing rows for these seasons (full rebuild pattern).
        # SIM-537: a cutoff build also removes a season that had not
        # started yet — INSERT alone only overwrites rows the new query
        # produces, so a stale full-season row would otherwise survive.
        delete_cutoff = f"OR season > {asof_date.year}" if asof is not None else ""
        self._conn.execute(f"""
            DELETE FROM derived.fielder_season_metrics
            WHERE season IN ({season_list}) {delete_cutoff}
        """)

        self._conn.execute(f"""
            INSERT INTO derived.fielder_season_metrics

            WITH
            -- Outfield OAA aggregation
            of_agg AS (
                SELECT
                    fielder_id AS player_id,
                    position,
                    season,
                    COUNT(*)                        AS opportunities,
                    SUM(catch_probability)           AS expected_outs,
                    SUM(CASE WHEN caught THEN 1 ELSE 0 END) AS actual_outs,
                    SUM(oaa_credit)                  AS outs_above_average,
                    AVG(catch_probability)            AS expected_catch_pct,
                    SUM(CASE WHEN caught THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS actual_catch_pct,

                    -- Directional breakdown
                    SUM(CASE WHEN direction_cat = 'glove_side' THEN oaa_credit ELSE 0 END) AS oaa_glove_side,
                    SUM(CASE WHEN direction_cat = 'arm_side' THEN oaa_credit ELSE 0 END) AS oaa_arm_side,
                    SUM(CASE WHEN direction_cat = 'charging' THEN oaa_credit ELSE 0 END) AS oaa_charging,
                    SUM(CASE WHEN direction_cat = 'deep' THEN oaa_credit ELSE 0 END) AS oaa_deep,

                    -- Star ratings
                    SUM(CASE WHEN star_rating = 5 THEN 1 ELSE 0 END) AS five_star_opps,
                    SUM(CASE WHEN star_rating = 5 AND caught THEN 1 ELSE 0 END) AS five_star_catches,
                    SUM(CASE WHEN star_rating = 4 THEN 1 ELSE 0 END) AS four_star_opps,
                    SUM(CASE WHEN star_rating = 4 AND caught THEN 1 ELSE 0 END) AS four_star_catches,
                    SUM(CASE WHEN star_rating IN (1, 2) THEN 1 ELSE 0 END) AS routine_opps,
                    SUM(CASE WHEN star_rating IN (1, 2) AND caught THEN 1 ELSE 0 END) AS routine_catches

                FROM _tmp_of_plays
                GROUP BY fielder_id, position, season
            ),
            -- Infield OAA aggregation
            if_agg AS (
                SELECT
                    fielder_id AS player_id,
                    position,
                    season,
                    COUNT(*)                        AS opportunities,
                    SUM(out_probability)             AS expected_outs,
                    SUM(CASE WHEN out_recorded THEN 1 ELSE 0 END) AS actual_outs,
                    SUM(oaa_credit)                  AS outs_above_average,
                    AVG(out_probability)              AS expected_catch_pct,
                    SUM(CASE WHEN out_recorded THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS actual_catch_pct,

                    SUM(CASE WHEN direction_category = 'glove_side' THEN oaa_credit ELSE 0 END) AS oaa_glove_side,
                    SUM(CASE WHEN direction_category = 'arm_side' THEN oaa_credit ELSE 0 END) AS oaa_arm_side,
                    SUM(CASE WHEN direction_category = 'charging' THEN oaa_credit ELSE 0 END) AS oaa_charging,
                    SUM(CASE WHEN direction_category = 'deep' THEN oaa_credit ELSE 0 END) AS oaa_deep

                FROM _tmp_if_plays
                GROUP BY fielder_id, position, season
            ),
            -- Combined OAA (union OF and IF)
            combined_oaa AS (
                SELECT * FROM of_agg
                UNION ALL
                SELECT player_id, position, season,
                       opportunities, expected_outs, actual_outs, outs_above_average,
                       expected_catch_pct, actual_catch_pct,
                       oaa_glove_side, oaa_arm_side, oaa_charging, oaa_deep,
                       NULL, NULL, NULL, NULL, NULL, NULL  -- star ratings NULL for IF
                FROM if_agg
            ),
            -- DP aggregation by initiator
            dp_init_agg AS (
                SELECT
                    initiator_id AS player_id,
                    initiator_position AS position,
                    season,
                    COUNT(*)                    AS dp_opportunities,
                    SUM(dp_probability)          AS dp_expected,
                    SUM(CASE WHEN dp_turned THEN 1 ELSE 0 END) AS dp_turned,
                    SUM(CASE WHEN dp_turned THEN 1 ELSE 0 END) - SUM(dp_probability) AS dp_above_expected,
                    SUM(dp_run_value)            AS dp_run_value
                FROM _tmp_dp_plays
                GROUP BY initiator_id, initiator_position, season
            ),
            -- DP pivot aggregation
            dp_pivot_agg AS (
                SELECT
                    pivot_id AS player_id,
                    pivot_position AS position,
                    season,
                    COUNT(*) AS dp_pivot_opportunities,
                    SUM(CASE WHEN dp_turned THEN 1 ELSE 0 END) AS dp_pivot_completions,
                    SUM(dp_probability) AS dp_pivot_expected,
                    SUM(CASE WHEN dp_turned THEN 1 ELSE 0 END) - SUM(dp_probability)
                        AS dp_pivot_above_expected
                FROM _tmp_dp_plays
                WHERE pivot_id IS NOT NULL
                GROUP BY pivot_id, pivot_position, season
            ),
            -- Errors
            err AS (
                SELECT * FROM _tmp_errors
            ),
            -- Bunt defense
            bunt AS (
                SELECT * FROM _tmp_bunt_defense
            ),
            -- 1B scooping
            scoop AS (
                SELECT * FROM _tmp_1b_scoop
            )

            SELECT
                c.player_id,
                c.position,
                c.season,
                0.0                         AS innings_played,
                c.opportunities             AS sample_batted_balls,

                -- Range / OAA
                c.opportunities,
                c.expected_outs,
                c.actual_outs,
                c.outs_above_average,
                c.expected_catch_pct,
                c.actual_catch_pct,
                c.actual_catch_pct - c.expected_catch_pct AS catch_pct_added,

                -- Range runs
                CASE WHEN c.position IN ('LF','CF','RF')
                     THEN c.outs_above_average * {RUNS_PER_OAA_OUTFIELD}
                     ELSE c.outs_above_average * {RUNS_PER_OAA_INFIELD}
                END AS range_runs,

                -- Directional
                c.oaa_glove_side,
                c.oaa_arm_side,
                c.oaa_charging,
                c.oaa_deep,

                -- Star ratings (OF only)
                c.five_star_opps,
                c.five_star_catches,
                c.four_star_opps,
                c.four_star_catches,
                c.routine_opps,
                c.routine_catches,

                -- Errors
                COALESCE(e.error_rate, 0)           AS error_rate,
                COALESCE(e.fielding_error_count, 0) AS fielding_error_count,
                COALESCE(e.throwing_error_count, 0) AS throwing_error_count,
                COALESCE(e.fielding_error_rate, 0)  AS fielding_error_rate,
                COALESCE(e.throwing_error_rate, 0)  AS throwing_error_rate,
                NULL::FLOAT                          AS error_oaa_cost,

                -- OF Arm. The throw velocity comes from Savant's arm-strength
                -- board (SIM-530). It is per position on the board, so the CASE
                -- takes the column for THIS row's position and falls back to the
                -- player's overall figure. This one is filled for infielders
                -- too: it is a physical fact, and the infield feature groups do
                -- not read it, so it stays inert there until SIM-532.
                COALESCE(
                    CASE c.position
                        WHEN '1B' THEN sas.arm_1b
                        WHEN '2B' THEN sas.arm_2b
                        WHEN '3B' THEN sas.arm_3b
                        WHEN 'SS' THEN sas.arm_ss
                        WHEN 'LF' THEN sas.arm_lf
                        WHEN 'CF' THEN sas.arm_cf
                        WHEN 'RF' THEN sas.arm_rf
                    END,
                    sas.arm_overall
                )                                       AS arm_strength,
                -- SIM-550: the six advancement columns are written NULL here.
                -- `_fill_outfield_arm_block` fills them from the advancement
                -- pool, per (fielder, position, season), after the pools are
                -- built. An outfielder who fielded no chance keeps NULL, never
                -- 0; a catcher or an infielder never gets a block.
                NULL::INTEGER                           AS arm_opportunities,
                NULL::INTEGER                           AS arm_holds,
                NULL::FLOAT                             AS arm_hold_rate,
                NULL::INTEGER                           AS arm_assists,
                NULL::FLOAT                             AS arm_thrown_out_rate,
                NULL::FLOAT                             AS arm_advancement_prevention,
                -- SIM-550: not read by the model; the runner-view figure that
                -- used to sit here described the fielder's own running.
                NULL::FLOAT                             AS of_arm_runs,

                -- DP (NULL for outfielders)
                dp.dp_opportunities,
                NULL::INTEGER AS dp_attempts,
                dp.dp_turned,
                dp.dp_expected,
                dp.dp_above_expected,
                CASE WHEN dp.dp_opportunities > 0
                     THEN dp.dp_turned * 1.0 / dp.dp_opportunities END AS dp_attempt_rate,
                NULL::FLOAT AS dp_success_rate,
                dp.dp_run_value,

                piv.dp_pivot_opportunities,
                piv.dp_pivot_completions,
                piv.dp_pivot_expected,
                piv.dp_pivot_above_expected,
                NULL::FLOAT AS dp_pivot_run_value,

                -- Bunt defense
                b.bunt_opportunities,
                b.bunt_outs_recorded,
                b.bunt_fielding_rate,

                -- 1B scooping
                s.scoop_opportunities,
                s.scoop_success_rate,

                -- Meta
                (c.opportunities < {MIN_FIELDER_PLAYS}) AS below_minimum_sample,
                CURRENT_TIMESTAMP AS updated_at,
                -- SIM-523 part G (migration 0024): the fielder's sprint speed (one join;
                -- appended LAST — the INSERT carries no column list).
                ss.sprint_speed AS sprint_speed,
                -- SIM-537 (migration 0028): appended after sprint_speed for
                -- the same positional-INSERT reason.
                DATE '{asof_sql}' AS asof_date,
                -- SIM-532 (migration 0030): the six Savant columns, appended
                -- LAST. The order below IS OAA_JUMP_COLUMN_ORDER: savant_oaa,
                -- savant_oaa_per_100, jump_reaction_ft, jump_burst_ft,
                -- jump_route_ft, jump_plays. A unit test holds the live
                -- table's tail to FIELDER_TAIL_COLUMNS; change both together.
                -- The board is pulled once per position, so this is the
                -- figure AT this row's position. NULLIF guards a row with no
                -- chance (the per-100 figure would divide by zero).
                -- The numerator and the denominator must cover the same
                -- season and span: at a cutoff the CASE in the denominator
                -- takes the prior season's chances with the prior season's
                -- outs; a live build takes the row's own chances.
                soaa.outs_above_average                                       AS savant_oaa,
                soaa.outs_above_average * 100.0 / NULLIF({savant_denominator}, 0)  AS savant_oaa_per_100,
                -- The jump is an outfield measurement: outfield rows only.
                CASE WHEN {_SQL_IS_OF} THEN sj.reaction_ft END                 AS jump_reaction_ft,
                CASE WHEN {_SQL_IS_OF} THEN sj.burst_ft END                    AS jump_burst_ft,
                CASE WHEN {_SQL_IS_OF} THEN sj.route_ft END                    AS jump_route_ft,
                CASE WHEN {_SQL_IS_OF} THEN sj.n_plays END                     AS jump_plays

            FROM combined_oaa c
            LEFT JOIN dp_init_agg dp
                ON c.player_id = dp.player_id AND c.position = dp.position AND c.season = dp.season
            LEFT JOIN dp_pivot_agg piv
                ON c.player_id = piv.player_id AND c.position = piv.position AND c.season = piv.season
            LEFT JOIN err e
                ON c.player_id = e.fielder_id AND c.position = e.position AND c.season = e.season
            LEFT JOIN bunt b
                ON c.player_id = b.fielder_id AND c.position = b.position AND c.season = b.season
            LEFT JOIN scoop s
                ON c.player_id = s.fielder_id AND c.position = s.position AND c.season = s.season
            -- SIM-532: the prior season's chances for the per-100 denominator
            -- at a cutoff. `prev` is the prior season when it is in the build
            -- (complete at the cutoff: every one of its games precedes it).
            -- `stored` is the prior season's SURVIVING row when it is not in
            -- the build (the delete above removes only the run's seasons; the
            -- INSERT reads the pre-insert snapshot). Both keys are the full
            -- (player, position, season), so neither join multiplies rows.
            LEFT JOIN combined_oaa prev
                ON prev.player_id = c.player_id
               AND prev.position = c.position
               AND prev.season = c.season - 1
            LEFT JOIN derived.fielder_season_metrics stored
                ON stored.player_id = c.player_id
               AND stored.position = c.position
               AND stored.season = c.season - 1
            -- SIM-537: the season-shift substitution described in the class
            -- docstring — unshifted (c.season) when asof is None.
            LEFT JOIN pg.raw.sprint_speed ss
                ON c.player_id = ss.player_id AND ss.season = {savant_season}
            -- SIM-530: the throw velocity board.
            LEFT JOIN pg.raw.savant_arm_strength sas
                ON c.player_id = sas.player_id AND sas.season = {savant_season}
            -- SIM-532: Savant's outs above average, one board row per
            -- (player, season, position) — the join carries the position.
            LEFT JOIN pg.raw.savant_outs_above_average soaa
                ON c.player_id = soaa.player_id
               AND soaa.position = c.position
               AND soaa.season = {savant_season}
            -- SIM-532: the outfield jump, one board row per (player, season);
            -- the same row serves each of his outfield positions.
            LEFT JOIN pg.raw.savant_outfield_jump sj
                ON c.player_id = sj.player_id AND sj.season = {savant_season}
        """)
        self._stamp_one_cutoff("derived.fielder_season_metrics", asof_sql)
        self._assert_fielder_profiles_have_no_leakage(seasons, asof_date)

        log.info("  Fielder season metrics aggregated.")

    def _assert_fielder_profiles_have_no_leakage(self, seasons: list[int], asof: date) -> None:
        """Prove raw.pitches contributed no row dated after the cutoff.

        Covers all six per-play builders and this aggregator — they all
        read raw.pitches under the identical season+cutoff filter, so one
        check at the end of the chain covers all of them. The four Savant
        joins (sprint speed, arm strength, outs above average, the outfield
        jump) have no date of their own to check — they are made safe by
        the season-shift substitution in the query above, not by
        filtering. The arm block's own dated source, the advancement pool,
        is checked by ``_fill_outfield_arm_block``.
        """
        season_list = ", ".join(str(s) for s in seasons)
        self._check_leakage(
            "raw.pitches",
            f"SELECT MAX(game_date) FROM pg.raw.pitches "
            f"WHERE season IN ({season_list}) AND game_date <= DATE '{asof}'",
            asof,
        )

    # ------------------------------------------------------------------
    # OUTFIELD ARM BLOCK (SIM-550) — from the advancement pool
    # ------------------------------------------------------------------

    def _fill_outfield_arm_block(self, seasons: list[int], asof: date | None = None) -> None:
        """SIM-550: fill the outfield arm block from
        ``sim.advancement_opportunity_pool``, per (fielder, position, season).

        The pool holds one row per real chance to take an extra base, with
        the fielder who fielded the ball and his position. This step counts
        each outfielder's chances, the attempts against him and the runners
        he threw out, then writes the six arm columns on his fielder row.
        The expectation behind ``arm_advancement_prevention`` is the pool's
        own attempt rate in the same season, decision, out state AND
        position: runners take the extra base less often against a left
        fielder for the geometry, not for his arm, so a 0 prevention is
        average at every position.

        The step first resets the seven arm columns on every fielder row of
        the requested seasons, then writes the block where the pool holds a
        chance. So the fill is self-contained: a row the pool no longer
        supports (a stale block from an earlier source, or a position the
        player did not field that season) is left NULL, not kept.

        Runs AFTER the pools. A cutoff filters the pool by ``game_date``, so
        the block is exact to the day — no season shift. Without the pool (a
        fresh database) the step logs and leaves the block NULL; the next
        nightly fills it. ``of_arm_runs`` is written NULL: the model does not
        read it.
        """
        if not duck_table_exists(self._conn, "sim", "advancement_opportunity_pool"):
            log.warning("  arm block skipped: sim.advancement_opportunity_pool absent")
            return
        log.info("Filling the outfield arm block from the advancement pool …")
        season_list = ", ".join(str(s) for s in seasons)
        date_cutoff = f"AND game_date <= DATE '{asof.isoformat()}'" if asof is not None else ""
        self._conn.execute(f"""
            CREATE OR REPLACE TEMP TABLE _arm AS
            WITH pool AS (
                SELECT season, scenario, from_base, target_base, outs,
                       fielder_id, fielder_pos, attempted, safe
                FROM sim.advancement_opportunity_pool
                WHERE season IN ({season_list})
                  AND fielder_pos IN (7, 8, 9)
                  AND fielder_id IS NOT NULL
                  {date_cutoff}
            ),
            -- What a generic fielder AT HIS POSITION concedes in the same
            -- cell: season x decision x outs x position. Without the
            -- position the prevention carries a position offset (2024:
            -- LF about +0.045, CF and RF about -0.02), because runners
            -- challenge a left fielder less on the same decision.
            expected AS (
                SELECT season, scenario, from_base, target_base, outs, fielder_pos,
                       AVG(attempted::INT) AS rate
                FROM pool
                GROUP BY 1, 2, 3, 4, 5, 6
            )
            SELECT p.fielder_id AS player_id,
                   CASE p.fielder_pos WHEN 7 THEN 'LF' WHEN 8 THEN 'CF' ELSE 'RF' END AS position,
                   p.season,
                   COUNT(*)                                                    AS chances,
                   SUM(p.attempted::INT)                                       AS attempts,
                   SUM(CASE WHEN p.attempted AND NOT p.safe THEN 1 ELSE 0 END) AS thrown_out,
                   SUM(e.rate)                                                 AS expected_attempts
            FROM pool p
            JOIN expected e USING (season, scenario, from_base, target_base, outs, fielder_pos)
            GROUP BY 1, 2, 3
        """)
        # The reset: the fill alone must leave the table right. The join
        # UPDATE below touches only rows the pool supports, so a row it
        # does not (a block from an earlier source, or a position the player
        # did not field that season) would keep stale figures. The infield
        # and the catcher never carry a block, so every position is reset.
        self._conn.execute(f"""
            UPDATE derived.fielder_season_metrics SET
                arm_opportunities          = NULL,
                arm_holds                  = NULL,
                arm_hold_rate              = NULL,
                arm_assists                = NULL,
                arm_thrown_out_rate        = NULL,
                arm_advancement_prevention = NULL,
                of_arm_runs                = NULL
            WHERE season IN ({season_list})
        """)
        self._conn.execute("""
            UPDATE derived.fielder_season_metrics f SET
                arm_opportunities          = a.chances,
                arm_holds                  = a.chances - a.attempts,
                arm_hold_rate              = 1.0 - a.attempts * 1.0 / a.chances,
                arm_assists                = a.thrown_out,
                -- NULL until a runner has challenged him.
                arm_thrown_out_rate        = a.thrown_out * 1.0 / NULLIF(a.attempts, 0),
                arm_advancement_prevention = (a.expected_attempts - a.attempts) / a.chances,
                of_arm_runs                = NULL
            FROM _arm a
            WHERE f.player_id = a.player_id
              AND f.position = a.position
              AND f.season = a.season
        """)
        self._conn.execute("DROP TABLE IF EXISTS _arm")
        self._check_leakage(
            "sim.advancement_opportunity_pool",
            f"SELECT MAX(game_date) FROM sim.advancement_opportunity_pool "
            f"WHERE season IN ({season_list}) {date_cutoff}",
            asof or date.today(),
        )
        log.info("  outfield arm block filled from the advancement pool.")
