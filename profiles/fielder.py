"""The fielder season profile: the aggregate of the per-play metrics and the outfield arm.

``_aggregate_fielder_season_metrics`` joins the six per-play temp tables of
``profiles/fielder_plays.py`` and four Savant boards into
``derived.fielder_season_metrics``. After the pools are built,
``_fill_outfield_arm_block`` fills the outfield arm columns from
``sim.advancement_opportunity_pool``.
"""

from __future__ import annotations

import logging
from datetime import date

from db.introspect import duck_table_exists
from profiles.base import ComputorBase
from profiles.constants import MIN_FIELDER_PLAYS
from profiles.defense import (
    RUNS_PER_OAA_INFIELD,
    RUNS_PER_OAA_OUTFIELD,
)
from profiles.sql import sql_seasons

log = logging.getLogger("player_profile_computor")

#: True on an outfield row. The range runs use it to pick the runs per out. The
#: jump columns use it too: the jump is an outfield measurement, so an infield
#: row gets NULL, never the player's figure from an outfield season.
_SQL_IS_OF = "c.position IN ('LF', 'CF', 'RF')"

#: The six per-play temp tables the fielder aggregate joins, each as a CREATE
#: TABLE IF NOT EXISTS placeholder with the columns the aggregate reads. A
#: per-play step skips its table when its sample is too small, and the empty
#: placeholder keeps the join working. A table the step did create stays as it is.
_FIELDER_PLACEHOLDER_DDL = {
    # Built by _compute_outfield_catch_probability.
    "_tmp_of_plays": """
        CREATE TABLE IF NOT EXISTS _tmp_of_plays (
            fielder_id INTEGER, position VARCHAR, season SMALLINT,
            catch_probability FLOAT, caught BOOLEAN,
            oaa_credit FLOAT, direction_cat VARCHAR, star_rating INTEGER
        )
    """,
    # Built by _compute_infield_oaa.
    "_tmp_if_plays": """
        CREATE TABLE IF NOT EXISTS _tmp_if_plays (
            fielder_id INTEGER, position VARCHAR, season SMALLINT,
            out_probability FLOAT, out_recorded BOOLEAN,
            oaa_credit FLOAT, direction_category VARCHAR
        )
    """,
    # Built by _compute_dp_metrics.
    "_tmp_dp_plays": """
        CREATE TABLE IF NOT EXISTS _tmp_dp_plays (
            initiator_id INTEGER, initiator_position VARCHAR,
            pivot_id INTEGER, pivot_position VARCHAR,
            season SMALLINT,
            dp_probability FLOAT, dp_turned BOOLEAN,
            dp_run_value FLOAT
        )
    """,
    # Built by _compute_error_decomposition. The column names must match its
    # output exactly. A caller that runs the aggregate alone reads this
    # placeholder, and a misnamed column fails with a Binder Error.
    "_tmp_errors": """
        CREATE TABLE IF NOT EXISTS _tmp_errors (
            fielder_id INTEGER, position VARCHAR, season SMALLINT,
            total_plays_fielded INTEGER, fielding_error_count INTEGER,
            throwing_error_count INTEGER, fielding_error_rate FLOAT,
            throwing_error_rate FLOAT, error_rate FLOAT
        )
    """,
    # Built by _compute_bunt_defense. The rule of _tmp_errors holds: the names
    # must match its output.
    "_tmp_bunt_defense": """
        CREATE TABLE IF NOT EXISTS _tmp_bunt_defense (
            fielder_id INTEGER, position VARCHAR, season SMALLINT,
            bunt_opportunities INTEGER, bunt_outs_recorded INTEGER,
            bunt_fielding_rate FLOAT
        )
    """,
    # Built by _compute_first_base_scooping.
    "_tmp_1b_scoop": """
        CREATE TABLE IF NOT EXISTS _tmp_1b_scoop (
            season SMALLINT, fielder_id INTEGER, position VARCHAR,
            scoop_opportunities INTEGER, scoop_success_rate FLOAT
        )
    """,
}


class FielderProfilesMixin(ComputorBase):
    """Builds ``derived.fielder_season_metrics``."""

    # ------------------------------------------------------------------
    # The season aggregate
    # ------------------------------------------------------------------

    def _ensure_fielder_temp_tables_exist(self) -> None:
        """Create an empty placeholder for each per-play temp table that does
        not exist yet.

        A per-play step returns early, with no table, when its sample is too
        small. The placeholder lets the aggregate's join find no rows; it never
        stands in for real data.
        """
        self._ensure_placeholder_tables(_FIELDER_PLACEHOLDER_DDL)

    def _aggregate_fielder_season_metrics(
        self, seasons: list[int], asof: date | None = None
    ) -> None:
        """Join the per-play temp tables and four Savant boards into the rows of
        ``derived.fielder_season_metrics``.

        The six per-play temp tables carry no date; their builders already cut
        them at ``asof``. The four Savant boards (sprint speed, arm strength,
        outs above average, the outfield jump) carry only a season number, so
        each join takes the Savant season shift (``_savant_season_sql``): a
        season that ended before the cutoff joins its own row, and the season
        that contains the cutoff joins the prior season's row. Each figure
        repeats from year to year, so the substitute costs little.

        The per-100 figure of Savant's outs above average divides the outs by
        the chances of the same season. At a cutoff, the season that contains
        the cutoff takes the prior season's outs, so it takes the prior
        season's chances too, never its own partial count. They come from
        this build when the prior season is in ``seasons``, else from the
        prior season's surviving row in the table. The one-cutoff guard makes
        sure a surviving row is a completed season. With no prior-season
        chances the figure is NULL, and the model reads the league mean.

        The six outfield arm columns and ``of_arm_runs`` are written NULL.
        ``_fill_outfield_arm_block`` fills the six after the pools are built.
        ``of_arm_runs`` stays NULL, because the model does not read it.

        The INSERT has no column list, so the column order matters. The six
        Savant columns come last, after ``asof_date``, in
        ``OAA_JUMP_COLUMN_ORDER``. The outs above average join is per position:
        Savant publishes the board once per position, so a player's figure
        differs between his LF and CF rows. The jump join is per player, and an
        infield row gets NULL jump columns.
        """
        log.info("Aggregating fielder season metrics …")
        asof_date = asof or date.today()
        asof_sql = asof_date.isoformat()
        savant_season = self._savant_season_sql("c.season", asof)
        # The chances of the outs' own season (see the docstring): at a cutoff,
        # `prev` from this build, else `stored` from the table.
        savant_denominator = (
            f"(CASE WHEN c.season = {asof_date.year} "
            "THEN COALESCE(prev.opportunities, stored.opportunities) "
            "ELSE c.opportunities END)"
            if asof is not None
            else "c.opportunities"
        )

        # Without the placeholders, a small data load fails with a DuckDB
        # "Catalog Error: Table with name _tmp_of_plays does not exist!".
        self._ensure_fielder_temp_tables_exist()

        self._refuse_a_mixed_cutoff("derived.fielder_season_metrics", seasons, asof_date)

        # The INSERT replaces nothing, so the run's seasons go first. A cutoff
        # build also drops each later season, which would otherwise keep its
        # rows from a full build.
        self._delete_season_rows("derived.fielder_season_metrics", seasons, asof)

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
                CASE WHEN {_SQL_IS_OF}
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

        # The aggregate is the per-play temp tables' only reader.
        for table in _FIELDER_PLACEHOLDER_DDL:
            self._conn.execute(f"DROP TABLE IF EXISTS {table}")

        log.info("  Fielder season metrics aggregated.")

    def _assert_fielder_profiles_have_no_leakage(self, seasons: list[int], asof: date) -> None:
        """Run the cutoff leakage check on ``raw.pitches``.

        The six per-play builders and the aggregate read ``raw.pitches`` under
        the same season and cutoff filter, so one check covers them all. The
        four Savant boards carry no date to check; the season shift guards
        them. ``_fill_outfield_arm_block`` checks the arm block's own dated
        source, the advancement pool.
        """
        self._check_pitch_sources_leakage(seasons, asof)

    # ------------------------------------------------------------------
    # The outfield arm block, from the advancement pool
    # ------------------------------------------------------------------

    def _fill_outfield_arm_block(self, seasons: list[int], asof: date | None = None) -> None:
        """Fill the outfield arm columns from ``sim.advancement_opportunity_pool``,
        per (fielder, position, season).

        The pool holds one row per real chance to take an extra base, with the
        fielder who fielded the ball and his position. The step counts each
        outfielder's chances, the attempts against him and the runners he threw
        out, then writes the six arm columns on his fielder row.
        ``arm_advancement_prevention`` compares his attempts with the pool's own
        attempt rate in the same season, decision, outs AND position. Runners
        take the extra base less often against a left fielder because of the
        geometry, not his arm, so 0 means average at every position.

        The step first resets the seven arm columns on every fielder row of the
        requested seasons, then writes the block where the pool holds a chance.
        So a row the pool does not support (a stale block from an earlier
        source, or a position the player did not field that season) ends NULL.

        Run the step AFTER the pools are built. A cutoff filters the pool by
        ``game_date``, so the block is exact to the day, with no season shift.
        With no pool (a fresh database) the step logs a warning and leaves the
        block NULL; the next nightly fills it. ``of_arm_runs`` stays NULL: the
        model does not read it.
        """
        if not duck_table_exists(self._conn, "sim", "advancement_opportunity_pool"):
            log.warning("  arm block skipped: sim.advancement_opportunity_pool absent")
            return
        log.info("Filling the outfield arm block from the advancement pool …")
        season_list = sql_seasons(seasons)
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
        # The join UPDATE below touches only the rows the pool supports. Reset
        # every row of the run's seasons first, so none keeps a stale block.
        # The infield and the catcher never carry one, so the reset covers
        # every position.
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
                arm_advancement_prevention = (a.expected_attempts - a.attempts) / a.chances
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
