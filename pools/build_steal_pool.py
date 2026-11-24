"""Build the steal pools: the stolen-base pool and the steal-opportunity pool the running-game draws sample."""

from __future__ import annotations

import logging

from ingest.statcast_events import (
    sql_steal_attempt,
    sql_steal_success,
)
from pools.pool_metadata import (
    _canonical_ref_season,
    _recency_weight_sql,
    _record_pool_build,
    _require_steal_pool_0031,
    _seasons_needing_rebuild,
)
from profiles.base import ComputorBase

log = logging.getLogger("player_profile_computor")


class StealPoolMixin(ComputorBase):
    """Build the steal pools: the stolen-base pool and the steal-opportunity pool the running-game draws sample."""

    def _build_stolen_base_pool(self, seasons: list[int], incremental: bool = False) -> None:
        """
        Builds sim.stolen_base_pool from all SB attempts in raw.pitches.
        Denormalizes runner/catcher/pitcher metrics from the derived tables
        so the simulation engine can score similarity without joins at runtime.
        Adds recency_weight. When ``incremental``, only the seasons whose
        source changed rebuild.
        """
        ref_season = _canonical_ref_season(self._conn, seasons)
        if incremental:
            seasons = _seasons_needing_rebuild(self._conn, "stolen_base_pool", seasons)
            if not seasons:
                log.info("Building sim.stolen_base_pool … all seasons fresh; skipped.")
                return
        log.info("Building sim.stolen_base_pool … (seasons=%s)", seasons)
        season_list = ", ".join(str(s) for s in seasons)
        recency_expr = _recency_weight_sql("s.season", ref_season)

        self._conn.execute(f"DELETE FROM sim.stolen_base_pool WHERE season IN ({season_list})")

        self._conn.execute(f"""
            INSERT INTO sim.stolen_base_pool

            WITH sb_pitches AS (
                SELECT
                    -- Use a surrogate pitch_id matching sim.pitch_pool
                    pp.pitch_id,
                    rp.game_pk,
                    rp.at_bat_number,
                    rp.pitch_number,
                    rp.game_date,
                    rp.season,
                    rp.fielder_2                AS catcher_id,
                    rp.pitcher,
                    rp.inning,
                    rp.outs,
                    (CASE WHEN rp.on_1b IS NOT NULL THEN 1 ELSE 0 END
                     + CASE WHEN rp.on_2b IS NOT NULL THEN 2 ELSE 0 END
                     + CASE WHEN rp.on_3b IS NOT NULL THEN 4 ELSE 0 END)::SMALLINT
                                                AS runners_state,
                    GREATEST(-5, LEAST(5, rp.bat_score - rp.fld_score))::SMALLINT
                                                AS score_diff,
                    rp.balls                    AS count_balls,
                    rp.strikes                  AS count_strikes,
                    rp.release_speed            AS velo,
                    rp.break_vertical_induced   AS ivb,
                    -- Determine which base was attempted and the runner ID.
                    -- SIM-506: attempt labels = columns OR events (a PA-ending
                    -- caught stealing lives only in `events`).
                    CASE
                        WHEN {sql_steal_attempt("home", "rp.")} AND rp.on_3b IS NOT NULL THEN rp.on_3b
                        WHEN {sql_steal_attempt("3b", "rp.")}   AND rp.on_2b IS NOT NULL THEN rp.on_2b
                        WHEN {sql_steal_attempt("2b", "rp.")}   AND rp.on_1b IS NOT NULL THEN rp.on_1b
                    END                         AS runner_id,
                    CASE
                        WHEN {sql_steal_attempt("home", "rp.")} THEN 'home'
                        WHEN {sql_steal_attempt("3b", "rp.")}   THEN '3B'
                        WHEN {sql_steal_attempt("2b", "rp.")}   THEN '2B'
                    END                         AS base_attempted,
                    CASE
                        WHEN {sql_steal_attempt("home", "rp.")} THEN {sql_steal_success("home", "rp.")}
                        WHEN {sql_steal_attempt("3b", "rp.")}   THEN {sql_steal_success("3b", "rp.")}
                        WHEN {sql_steal_attempt("2b", "rp.")}   THEN {sql_steal_success("2b", "rp.")}
                    END                         AS success
                FROM pg.raw.pitches rp
                JOIN sim.pitch_pool pp
                    ON pp.game_pk = rp.game_pk
                   AND pp.at_bat_number = rp.at_bat_number
                   AND pp.pitch_number  = rp.pitch_number
                WHERE rp.data_quality_flag = FALSE
                  AND rp.season IN ({season_list})
                  AND ({sql_steal_attempt("2b", "rp.")} OR {sql_steal_attempt("3b", "rp.")}
                       OR {sql_steal_attempt("home", "rp.")})
            )
            SELECT
                s.pitch_id,
                s.game_pk,
                s.at_bat_number,
                s.pitch_number,
                s.game_date,
                s.season,
                s.runner_id,
                s.pitcher,
                s.catcher_id,
                s.base_attempted,
                s.inning,
                s.outs,
                s.runners_state,
                s.score_diff,
                s.count_balls,
                s.count_strikes,
                s.velo,
                s.ivb,
                -- Denormalized runner metrics (most recent season available)
                ss.sprint_speed             AS runner_sprint_speed,
                brm.sb_success_rate         AS runner_sb_success_rate,
                -- Denormalized catcher metrics
                csm.pop_time_mean           AS catcher_pop_time_mean,
                csm.arm_strength_mean       AS catcher_arm_strength,
                -- Denormalized pitcher-allowing-SB metric
                -- Proxy: SB allowed rate from catcher_season_metrics on pitcher
                -- (pitcher's own cs_rate as opposition catcher doesn't apply here;
                -- real pitcher-SB-allowed rate requires Phase 2 Step 2.7)
                NULL::FLOAT                 AS pitcher_sb_allowed_rate,
                s.success,
                {recency_expr} AS recency_weight
            FROM sb_pitches s
            LEFT JOIN derived.baserunner_season_metrics brm
                ON brm.player_id = s.runner_id AND brm.season = s.season
            LEFT JOIN derived.catcher_season_metrics csm
                ON csm.player_id = s.catcher_id AND csm.season = s.season
            LEFT JOIN pg.raw.sprint_speed ss
                ON ss.player_id = s.runner_id AND ss.season = s.season
            WHERE s.runner_id IS NOT NULL
              AND s.base_attempted IS NOT NULL
        """)
        log.info("  sim.stolen_base_pool done.")
        _record_pool_build(self._conn, "stolen_base_pool", seasons, ref_season)

    def _build_steal_opportunity_pool(self, seasons: list[int], incremental: bool = False) -> None:
        """Rebuild sim.steal_opportunity_pool: one row per pitch where a steal
        was POSSIBLE, attempted or not.

        `sim.stolen_base_pool` holds only ATTEMPTS, so a draw over it attempts a
        steal every time: it has no denominator. This pool keeps the
        `attempted` flag beside `success`, so ONE draw answers both "does the
        runner go" and "safe or caught", and the catcher affects the DECISION,
        not only the outcome.

        An opportunity is the LEAD open-base pair, matching the live decision:
          * a runner on 1B with 2B open  -> target_base 2 (the runner on 1B)
          * a runner on 2B with 3B open  -> target_base 3 (the runner on 2B),
            including 1B+2B (the lead runner drives a double steal)
        Steals of home are out of scope.

        The pre-pitch situation columns (`outs`, the count, the base state) are
        trustworthy only on data loaded by the fixed parser (the re-sweep of
        2026-08-13, SIM-488).

        Each pitch row carries the class of its pitch (`pitch_class`,
        the pitch pool's outcome_type); the steal draw after the pitch filters
        on it. A pickoff outcome whose plate appearance holds no pitch of its
        pair (the throw came before the first pitch) becomes a PICKOFF ROW of
        its own: `is_pickoff_row` TRUE, `pitch_id` the negative of the play
        record's id, `pitch_number` 0, the count 0-0 (the count at the throw is
        not stored), the outs and the score before the throw, never attempted,
        `pitch_class` NULL. An outcome that has a pitch of its pair keeps its
        tag on that pitch and makes no row, so no outcome counts twice. The
        catcher is the one on the nearest pitch of the same half-inning (the
        earlier plate appearance on a tie); with none, he is unknown.

        ⚠ The INSERT is positional (no column list): the SELECT order MUST match
        the DDL column order in db/schemas/02_duckdb_schema.sql (migrations
        0015 + 0017 + 0031). The builder refuses a table without the 0031
        columns BEFORE it deletes anything.
        """
        ref_season = _canonical_ref_season(self._conn, seasons)
        if incremental:
            seasons = _seasons_needing_rebuild(self._conn, "steal_opportunity_pool", seasons)
            if not seasons:
                log.info("Building sim.steal_opportunity_pool … all seasons fresh; skipped.")
                return
        log.info("Building sim.steal_opportunity_pool … (seasons=%s)", seasons)
        season_list = ", ".join(str(s) for s in seasons)
        recency_expr = _recency_weight_sql("f.season", ref_season)
        pickoff_recency_expr = _recency_weight_sql("e.season", ref_season)
        pickoff_cte = self._pickoff_outcomes_cte(season_list)

        # Probe before the DELETE (a failed positional INSERT would leave the
        # season with no steal rows).
        _require_steal_pool_0031(self._conn)

        self._conn.execute(
            f"DELETE FROM sim.steal_opportunity_pool WHERE season IN ({season_list})"
        )

        self._conn.execute(f"""
            INSERT INTO sim.steal_opportunity_pool

            -- SIM-554: MATERIALIZED, so the Postgres pitch rows are read once.
            -- Two CTEs read this one (f and the pickoff rows' anti-join); an
            -- inlined CTE would read the season's pitches once for each.
            WITH opportunities AS MATERIALIZED (
                SELECT
                    pp.pitch_id,
                    rp.game_pk,
                    rp.at_bat_number,
                    rp.pitch_number,
                    rp.game_date,
                    rp.season,
                    -- The lead stealable runner: 1B when 2B is open, else the
                    -- runner on 2B (3B is open by the WHERE below).
                    CASE
                        WHEN rp.on_1b IS NOT NULL AND rp.on_2b IS NULL THEN rp.on_1b
                        ELSE rp.on_2b
                    END::INTEGER                    AS runner_id,
                    rp.pitcher::INTEGER             AS pitcher_id,
                    rp.fielder_2::INTEGER           AS catcher_id,
                    CASE
                        WHEN rp.on_1b IS NOT NULL AND rp.on_2b IS NULL THEN 2
                        ELSE 3
                    END::SMALLINT                   AS target_base,
                    rp.inning::SMALLINT             AS inning,
                    rp.outs::SMALLINT               AS outs,
                    rp.balls::SMALLINT              AS count_balls,
                    rp.strikes::SMALLINT            AS count_strikes,
                    GREATEST(-5, LEAST(5, rp.bat_score - rp.fld_score))::SMALLINT
                                                    AS score_diff,
                    -- The flags for THIS pair's target base only: an attempt of
                    -- another base on the same pitch is a different decision.
                    -- SIM-506: the labels OR the `events` column in — a PA-ending
                    -- caught stealing lives ONLY there (measured disjoint from the
                    -- sb_* columns; 43% of 2B caught stealings). Columns alone
                    -- read the pool 87.6% safe against a real ~82.7%.
                    -- SIM-507: a strikeout_double_play is a K + a caught runner —
                    -- measured 2023: 100 of 100 rows sit in an opportunity shape
                    -- (97 target-2, 3 target-3), so the pair's target is the
                    -- caught runner's target. Attempted, never successful.
                    CASE
                        WHEN rp.on_1b IS NOT NULL AND rp.on_2b IS NULL
                            THEN {sql_steal_attempt("2b", "rp.")}
                              OR COALESCE(rp.events = 'strikeout_double_play', FALSE)
                        ELSE {sql_steal_attempt("3b", "rp.")}
                              OR COALESCE(rp.events = 'strikeout_double_play', FALSE)
                    END                             AS attempted,
                    CASE
                        WHEN rp.on_1b IS NOT NULL AND rp.on_2b IS NULL
                            THEN {sql_steal_success("2b", "rp.")}
                        ELSE {sql_steal_success("3b", "rp.")}
                    END                             AS success,
                    -- SIM-554: the class of the pitch the row rode.
                    pp.outcome_type                 AS pitch_class
                FROM pg.raw.pitches rp
                JOIN sim.pitch_pool pp
                    ON pp.game_pk       = rp.game_pk
                   AND pp.at_bat_number = rp.at_bat_number
                   AND pp.pitch_number  = rp.pitch_number
                WHERE rp.data_quality_flag = FALSE
                  AND rp.season IN ({season_list})
                  AND rp.outs BETWEEN 0 AND 2
                  AND (
                        (rp.on_1b IS NOT NULL AND rp.on_2b IS NULL)
                     OR (rp.on_2b IS NOT NULL AND rp.on_3b IS NULL)
                  )
            ),
            -- SIM-507: pickoff outcomes per (plate appearance, target), from
            -- raw.play_events. Precedence: an out beats an error in the same
            -- plate appearance (both in one PA is a couple of rows a season).
            pickoffs AS (
                {pickoff_cte}
            ),
            po AS (
                SELECT game_pk, at_bat_number, target_base,
                       MAX(is_out_i) AS po_out,
                       MAX(is_adv_i) AS po_adv,
                       MAX(is_err_i) AS po_err
                FROM pickoffs
                WHERE target_base IS NOT NULL
                GROUP BY game_pk, at_bat_number, target_base
            ),
            -- SIM-507: attribute each PA's pickoff outcome to ONE opportunity
            -- pitch (the first non-attempted pitch of the pair). Which pitch
            -- carries the label does not matter to the draw — only the count
            -- of labeled rows over opportunity rows does — but keeping it off
            -- attempted rows avoids a steal/pickoff collision on one row.
            f AS (
                SELECT
                    o.*,
                    po.po_out, po.po_adv, po.po_err,
                    ROW_NUMBER() OVER (
                        PARTITION BY o.game_pk, o.at_bat_number, o.target_base
                        ORDER BY o.attempted ASC, o.pitch_number ASC
                    ) AS rn
                FROM opportunities o
                LEFT JOIN po
                    ON po.game_pk       = o.game_pk
                   AND po.at_bat_number = o.at_bat_number
                   AND po.target_base   = o.target_base
                WHERE o.runner_id IS NOT NULL
                  AND o.pitcher_id IS NOT NULL
            ),
            -- SIM-554: the pickoff OUTCOMES (an out or an error; most throws
            -- have neither) with a pair, a runner and a thrower, whose plate
            -- appearance holds no pitch of that pair among the rows f keeps.
            -- Each becomes one pickoff row. An outcome with such a pitch is
            -- tagged on it above and makes no row: never both.
            pickoff_events AS (
                SELECT e.*
                FROM pickoffs e
                WHERE (e.is_out_i = 1 OR e.is_err_i = 1)
                  AND e.target_base IS NOT NULL
                  AND e.runner_id IS NOT NULL
                  AND e.pitcher_id IS NOT NULL
                  AND NOT EXISTS (
                        SELECT 1 FROM opportunities o
                        WHERE o.game_pk       = e.game_pk
                          AND o.at_bat_number = e.at_bat_number
                          AND o.target_base   = e.target_base
                          AND o.runner_id  IS NOT NULL
                          AND o.pitcher_id IS NOT NULL
                  )
            ),
            -- SIM-554: the play record names no catcher. Take the catcher of
            -- the nearest pitch of the same half-inning: the fewest plate
            -- appearances away, the earlier one on a tie, its first pitch.
            -- Only the pickoff rows' games are read. A half-inning with no
            -- pitch leaves the catcher NULL; the sampler's catcher weight is
            -- then neutral.
            half_pitches AS (
                SELECT rp.game_pk, rp.inning, rp.inning_topbot,
                       rp.at_bat_number, rp.pitch_number,
                       rp.fielder_2::INTEGER AS catcher_id
                FROM pg.raw.pitches rp
                WHERE rp.season IN ({season_list})
                  AND rp.game_pk IN (SELECT game_pk FROM pickoff_events)
                  AND rp.fielder_2 IS NOT NULL
            ),
            pickoff_catcher AS (
                SELECT e.id AS event_id, h.catcher_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY e.id
                           ORDER BY ABS(h.at_bat_number - e.at_bat_number),
                                    h.at_bat_number, h.pitch_number
                       ) AS rn
                FROM pickoff_events e
                JOIN half_pitches h
                    ON h.game_pk       = e.game_pk
                   AND h.inning        = e.inning
                   AND h.inning_topbot = e.inning_topbot
            )
            SELECT
                f.pitch_id, f.game_pk, f.at_bat_number, f.pitch_number,
                f.game_date, f.season,
                f.runner_id, f.pitcher_id, f.catcher_id,
                f.target_base, f.inning, f.outs,
                f.count_balls, f.count_strikes, f.score_diff,
                f.attempted, f.success,
                {recency_expr} AS recency_weight,
                (f.rn = 1 AND COALESCE(f.po_out, 0) = 1)  AS pickoff_out,
                (f.rn = 1 AND COALESCE(f.po_adv, 0) = 1)  AS pickoff_advancing,
                (f.rn = 1 AND COALESCE(f.po_err, 0) = 1
                    AND COALESCE(f.po_out, 0) = 0)        AS pickoff_error,
                -- SIM-554 (migration 0031): appended last, in the DDL's order.
                f.pitch_class,
                FALSE AS is_pickoff_row
            FROM f
            UNION ALL
            -- SIM-554: the pickoff rows, the same 23 columns in the same order.
            SELECT
                (-e.id)::BIGINT                           AS pitch_id,
                e.game_pk::INTEGER                        AS game_pk,
                e.at_bat_number::INTEGER                  AS at_bat_number,
                0                                         AS pitch_number,
                e.game_date                               AS game_date,
                e.season::SMALLINT                        AS season,
                e.runner_id::INTEGER                      AS runner_id,
                e.pitcher_id::INTEGER                     AS pitcher_id,
                pc.catcher_id                             AS catcher_id,
                e.target_base                             AS target_base,
                e.inning::SMALLINT                        AS inning,
                e.outs_before::SMALLINT                   AS outs,
                0::SMALLINT                               AS count_balls,
                0::SMALLINT                               AS count_strikes,
                GREATEST(-5, LEAST(5, e.bat_score - e.fld_score))::SMALLINT
                                                          AS score_diff,
                FALSE                                     AS attempted,
                FALSE                                     AS success,
                {pickoff_recency_expr}                    AS recency_weight,
                (e.is_out_i = 1)                          AS pickoff_out,
                (e.is_adv_i = 1)                          AS pickoff_advancing,
                (e.is_err_i = 1 AND e.is_out_i = 0)       AS pickoff_error,
                NULL::VARCHAR                             AS pitch_class,
                TRUE                                      AS is_pickoff_row
            FROM pickoff_events e
            LEFT JOIN pickoff_catcher pc
                ON pc.event_id = e.id
               AND pc.rn       = 1
        """)
        log.info("  sim.steal_opportunity_pool done.")
        _record_pool_build(self._conn, "steal_opportunity_pool", seasons, ref_season)
