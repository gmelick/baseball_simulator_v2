"""The league-average profile rows: one row per entity type and season, read by
the similarity engines as the shrinkage target.
"""

from __future__ import annotations

import logging

import duckdb

from db.introspect import duck_columns

log = logging.getLogger("player_profile_computor")


class LeagueAverageProfiles:
    """Computes the league-average profile of each entity type and season.

    The similarity engines shrink a thin profile toward these rows and fall
    back to them for a player with below_minimum_sample=TRUE. Call
    ``compute(seasons)`` after ``PlayerProfileComputor.run()``; the rows go to
    derived.league_averages, which ``compute`` creates when it is absent.
    """

    LEAGUE_AVG_DDL = """
        CREATE TABLE IF NOT EXISTS derived.league_averages (
            entity_type  VARCHAR(20)  NOT NULL,  -- 'pitcher', 'batter', 'fielder_IF', etc.
            season       SMALLINT     NOT NULL,
            profile_json JSON         NOT NULL,
            updated_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (entity_type, season)
        );
        COMMENT ON TABLE derived.league_averages IS
            'League-average profiles per entity type per season. '
            'Similarity engines fall back to these when below_minimum_sample=TRUE.';
    """

    def __init__(self, duckdb_path: str) -> None:
        self._path = duckdb_path

    @staticmethod
    def _derived_columns(conn: duckdb.DuckDBPyConnection, table: str) -> set[str]:
        """The columns ``derived.<table>`` carries (empty when the table is
        absent): the probe the engines and the calibrator use, so a database
        before migration 0029, or a fixture that builds only some tables,
        still gets its league averages."""
        return duck_columns(conn, "derived", table)

    def compute(self, seasons: list[int]) -> None:
        conn = duckdb.connect(self._path)
        try:
            conn.execute(self.LEAGUE_AVG_DDL)
            season_list = ", ".join(str(s) for s in seasons)
            # The newer columns and tables, probed rather than assumed.
            brm_cols = self._derived_columns(conn, "baserunner_season_metrics")
            bss_cols = self._derived_columns(conn, "baserunner_steal_metrics")
            psm_cols = self._derived_columns(conn, "pitcher_steal_metrics")
            fsm_cols = self._derived_columns(conn, "fielder_season_metrics")

            # Pitcher average (used for GMM fallback)
            conn.execute(f"""
                INSERT OR REPLACE INTO derived.league_averages
                SELECT
                    'pitcher' AS entity_type, season,
                    JSON_OBJECT(
                        'bb_rate',          AVG(bb_rate),
                        'k_rate',           AVG(k_rate),
                        'csw_rate',         AVG(csw_rate),
                        'zone_rate',        AVG(zone_rate),
                        'chase_rate',       AVG(chase_rate),
                        'era',              AVG(era),
                        'fip',              AVG(fip),
                        'ground_ball_rate', AVG(ground_ball_rate),
                        'fly_ball_rate',    AVG(fly_ball_rate)
                    ) AS profile_json,
                    CURRENT_TIMESTAMP AS updated_at
                FROM derived.pitcher_season_metrics
                WHERE season IN ({season_list})
                  AND below_minimum_sample = FALSE
                GROUP BY season
            """)

            # Batter average
            conn.execute(f"""
                INSERT OR REPLACE INTO derived.league_averages
                SELECT
                    'batter' AS entity_type, season,
                    JSON_OBJECT(
                        'first_pitch_take_rate', AVG(first_pitch_take_rate),
                        'o_swing_rate',          AVG(o_swing_rate),
                        'z_swing_rate',          AVG(z_swing_rate),
                        'whiff_rate',            AVG(whiff_rate),
                        'contact_rate',          AVG(contact_rate),
                        'walk_rate',             AVG(walk_rate),
                        'k_rate',                AVG(k_rate),
                        'avg_exit_velo',         AVG(avg_exit_velo),
                        'hard_hit_rate',         AVG(hard_hit_rate)
                    ) AS profile_json,
                    CURRENT_TIMESTAMP AS updated_at
                FROM derived.batter_season_metrics
                WHERE season IN ({season_list})
                  AND below_minimum_sample = FALSE
                GROUP BY season
            """)

            # Fielder average — one row per position so shrinkage falls
            # back to position-specific league averages (a 1B should not
            # be compared to an OF league average).
            #
            # The three arm keys the fielder model shrinks toward join the
            # row. Without them a thin arm is pulled toward 0.0,
            # and a 0 thrown-out rate is the weakest arm in the league. The
            # per-position WHERE below keeps the positions apart: an infield
            # row never enters an outfield row's AVG. AVG skipping NULL does
            # a different job: an outfielder without a block (he fielded no
            # chance) or without a velocity does not pull his position's
            # mean. An infield row's two pool-derived keys come out null
            # (the block is never filled there); its `arm_strength` is the
            # infield velocity, which the aggregator fills for every
            # position. Each key is written only when the fielder table
            # carries its column (a narrow fixture may not).
            arm_keys = "".join(
                f", '{col}', AVG({col})"
                for col in ("arm_advancement_prevention", "arm_thrown_out_rate", "arm_strength")
                if col in fsm_cols
            )
            # The Savant keys the fielder model shrinks toward.
            # Every fielding position (not the catcher) gets Savant's outs
            # above average per 100 chances; the three outfield positions
            # also get the three jump parts. AVG skips NULL: an outfielder
            # without a jump row (about two thirds of outfield rows) does
            # not pull the mean, and a row without a Savant figure does not
            # pull the per-100 mean. Each key is written only when the
            # fielder table carries its column (a database that has not
            # run migration 0030, or a narrow fixture).
            savant_cols: dict[str, tuple[str, ...]] = {
                "C": (),
                "1B": ("savant_oaa_per_100",),
                "2B": ("savant_oaa_per_100",),
                "3B": ("savant_oaa_per_100",),
                "SS": ("savant_oaa_per_100",),
                "LF": ("savant_oaa_per_100", "jump_reaction_ft", "jump_burst_ft", "jump_route_ft"),
                "CF": ("savant_oaa_per_100", "jump_reaction_ft", "jump_burst_ft", "jump_route_ft"),
                "RF": ("savant_oaa_per_100", "jump_reaction_ft", "jump_burst_ft", "jump_route_ft"),
            }
            for position in ("C", "1B", "2B", "3B", "SS", "LF", "CF", "RF"):
                savant_keys = "".join(
                    f", '{col}', AVG({col})" for col in savant_cols[position] if col in fsm_cols
                )
                conn.execute(f"""
                    INSERT OR REPLACE INTO derived.league_averages
                    SELECT
                        'fielder_{position}' AS entity_type, season,
                        JSON_OBJECT(
                            'outs_above_average', AVG(outs_above_average),
                            'error_rate',         AVG(error_rate),
                            'arm_hold_rate',      AVG(arm_hold_rate),
                            'dp_run_value',       AVG(dp_run_value){arm_keys}{savant_keys}
                        ) AS profile_json,
                        CURRENT_TIMESTAMP AS updated_at
                    FROM derived.fielder_season_metrics
                    WHERE position = '{position}'
                      AND season IN ({season_list})
                      AND below_minimum_sample = FALSE
                    GROUP BY season
                """)

            # Baserunner average. The extra-base attempt rate above
            # expectation joins so the advancement model can shrink a thin
            # runner's figure toward the league's (AVG skips NULLs, so a
            # runner with no Savant row does not pull the mean). The key is
            # written only when the table carries the 0029 column.
            xb_key = (
                ", 'xb_attempt_rate_above_expected', AVG(xb_attempt_rate_above_expected)"
                if "xb_attempt_rate_above_expected" in brm_cols
                else ""
            )
            conn.execute(f"""
                INSERT OR REPLACE INTO derived.league_averages
                SELECT
                    'baserunner' AS entity_type, season,
                    JSON_OBJECT(
                        'sprint_speed',            AVG(sprint_speed),
                        'extra_base_attempt_rate', AVG(extra_base_attempt_rate),
                        'extra_base_success_rate', AVG(extra_base_success_rate),
                        'sb_success_rate',         AVG(sb_success_rate){xb_key}
                    ) AS profile_json,
                    CURRENT_TIMESTAMP AS updated_at
                FROM derived.baserunner_season_metrics
                WHERE season IN ({season_list})
                  AND below_minimum_sample = FALSE
                GROUP BY season
            """)

            # The steal-runner and pitcher-hold rows: the shrinkage target the
            # two steal engines read (``_load_league_averages`` filters on these
            # entity types). Savant's lead boards load at a minimum of one
            # initiation, so a 20-initiation lead must not be read at face
            # value. Each row is written when its table exists; the lead keys
            # when the migration 0029 columns do.
            if bss_cols:
                lead_keys = (
                    ", 'lead_primary_ft', AVG(lead_primary_ft), 'lead_jump_ft', AVG(lead_jump_ft)"
                    if {"lead_primary_ft", "lead_jump_ft"} <= bss_cols
                    else ""
                )
                conn.execute(f"""
                    INSERT OR REPLACE INTO derived.league_averages
                    SELECT
                        'baserunner_steal' AS entity_type, season,
                        JSON_OBJECT(
                            'steal_attempt_rate',    AVG(steal_attempt_rate),
                            'steal_attempt_rate_2b', AVG(steal_attempt_rate_2b),
                            'steal_success_rate',    AVG(steal_success_rate),
                            'steal_success_rate_2b', AVG(steal_success_rate_2b){lead_keys}
                        ) AS profile_json,
                        CURRENT_TIMESTAMP AS updated_at
                    FROM derived.baserunner_steal_metrics
                    WHERE season IN ({season_list})
                      AND below_minimum_sample = FALSE
                    GROUP BY season
                """)
            if psm_cols:
                hold_keys = (
                    ", 'lead_allowed_primary_ft', AVG(lead_allowed_primary_ft), "
                    "'lead_allowed_jump_ft', AVG(lead_allowed_jump_ft)"
                    if {"lead_allowed_primary_ft", "lead_allowed_jump_ft"} <= psm_cols
                    else ""
                )
                conn.execute(f"""
                    INSERT OR REPLACE INTO derived.league_averages
                    SELECT
                        'pitcher_steal' AS entity_type, season,
                        JSON_OBJECT(
                            'sb_against_per_9',           AVG(sb_against_per_9),
                            'cs_rate_forced',             AVG(cs_rate_forced),
                            'steal_attempt_rate_allowed', AVG(steal_attempt_rate_allowed){hold_keys}
                        ) AS profile_json,
                        CURRENT_TIMESTAMP AS updated_at
                    FROM derived.pitcher_steal_metrics
                    WHERE season IN ({season_list})
                      AND below_minimum_sample = FALSE
                    GROUP BY season
                """)

            # Catcher average
            conn.execute(f"""
                INSERT OR REPLACE INTO derived.league_averages
                SELECT
                    'catcher' AS entity_type, season,
                    JSON_OBJECT(
                        'framing_runs',  AVG(framing_runs),
                        'blocking_runs', AVG(blocking_runs),
                        'cs_rate',       AVG(cs_rate)
                    ) AS profile_json,
                    CURRENT_TIMESTAMP AS updated_at
                FROM derived.catcher_season_metrics
                WHERE season IN ({season_list})
                  AND below_minimum_sample = FALSE
                GROUP BY season
            """)

            # Manager average: the GAME-weighted mean of the seventeen
            # tendency columns per season — the manager engine's
            # shrinkage target (``ManagerSimilarityEngine._load_league_averages``
            # reads the feature names as JSON keys) and the steal weight's
            # measured denominator (``steal_order_rate_per_1b_opp``). A column
            # that is NULL on every row (hit-and-run, the double switch) averages
            # to NULL, which the engine reads as 0.0 — the same value the
            # profiles carry for it.
            manager_cols = (
                "starter_avg_pitch_count",
                "starter_pull_pct_before_100",
                "closer_entry_leverage_index",
                "high_leverage_reliever_rate",
                "opener_usage_rate",
                "bulk_innings_rate",
                "available_reliever_usage_rate",
                "steal_order_rate_per_1b_opp",
                "hit_and_run_rate_per_opportunity",
                "sac_bunt_rate_high_leverage",
                "sac_bunt_rate_low_leverage",
                "squeeze_play_rate_per_3b_opp",
                "pinch_hit_rate_vs_same_hand",
                "pinch_hit_rate_high_leverage",
                "defensive_sub_rate_late_innings",
                "double_switch_rate_per_reliever_change",
                "platoon_advantage_exploitation_rate",
            )
            weighted = ", ".join(
                f"'{c}', SUM({c} * sample_games) / NULLIF(SUM(CASE WHEN {c} IS NOT NULL "
                f"THEN sample_games END), 0)"
                for c in manager_cols
            )
            conn.execute(f"""
                INSERT OR REPLACE INTO derived.league_averages
                SELECT
                    'manager' AS entity_type, season,
                    JSON_OBJECT(
                        {weighted}
                    ) AS profile_json,
                    CURRENT_TIMESTAMP AS updated_at
                FROM derived.manager_season_metrics
                WHERE season IN ({season_list})
                  AND below_minimum_sample = FALSE
                GROUP BY season
            """)

            log.info("League averages computed for seasons: %s", seasons)
        finally:
            conn.close()
