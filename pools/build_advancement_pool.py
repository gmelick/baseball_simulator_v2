"""Build the advancement pool, the rows the runner-advancement draws sample."""

from __future__ import annotations

import logging

from pools.pool_metadata import (
    _recency_weight_sql,
    _record_pool_build,
    _seasons_to_build,
)
from profiles.base import ComputorBase
from profiles.sql import sql_seasons

log = logging.getLogger("player_profile_computor")


class AdvancementPoolMixin(ComputorBase):
    """Build the advancement pool."""

    def _build_advancement_opportunity_pool(
        self, seasons: list[int], incremental: bool = False
    ) -> None:
        """Rebuild sim.advancement_opportunity_pool: one row per discretionary
        runner-advancement decision, attempted or not.

        The five scenarios are the ONLY discretionary movements the fielding
        draw normalizes away (the double-count guard); this pool is where those
        movements become data. The non-attempt rows are the point: one draw
        answers "does he go" and "safe or out" together, as the steal pool's
        denominator does.

        Reads the transition columns on sim.outcome_pool, so it must
        run AFTER ``_build_outcome_pool`` in the same recompute. Rows whose
        transition failed the outs-accounting guard are excluded.

        ⚠ The INSERT is positional (no column list): the SELECT order MUST
        match the DDL column order in db/schemas/02_duckdb_schema.sql
        (migration 0018).
        """
        plan = _seasons_to_build(self._conn, "advancement_opportunity_pool", seasons, incremental)
        if plan is None:
            return
        seasons, ref_season = plan
        season_list = sql_seasons(seasons)
        recency_expr = _recency_weight_sql("op.season", ref_season)

        # A tag-up decision exists in exactly one shape: a caught ball, the
        # batter out, and the catch not the inning's third out.
        s4 = (
            "op.batter_dest = 0 AND op.result_hits = 0 "
            "AND op.bb_type IN ('fly_ball', 'popup', 'line_drive') AND op.outs <= 1"
        )
        # A runner erased RETURNING to his base (dest 0 without the advancing
        # flag — a doubled-off runner) never had a tag decision; the fielding
        # row plays that out, so those rows leave the tag denominators.
        no_double_off = "NOT (op.runner_{b}_dest = 0 AND NOT op.runner_{b}_out_advancing)"

        # (scenario, from_base, target_base, runner, where, attempted, safe, error_extra)
        decisions: list[tuple[int, int, int, str, str, str, str, str]] = [
            (
                1,
                1,
                3,
                "op.on_1b",
                "op.events = 'single' AND op.on_1b IS NOT NULL "
                "AND (op.post_on_3b IS NULL OR op.post_on_3b = op.on_1b)",
                "op.runner_1b_dest IN (3, 4) "
                "OR (op.runner_1b_dest = 0 AND op.runner_1b_out_advancing)",
                "op.runner_1b_dest IN (3, 4)",
                "op.runner_1b_dest = 4",
            ),
            (
                2,
                2,
                4,
                "op.on_2b",
                "op.events = 'single' AND op.on_2b IS NOT NULL "
                "AND (op.on_3b IS NULL OR op.runner_3b_dest IN (0, 4))",
                "op.runner_2b_dest = 4 OR (op.runner_2b_dest = 0 AND op.runner_2b_out_advancing)",
                "op.runner_2b_dest = 4",
                "FALSE",
            ),
            (
                3,
                1,
                4,
                "op.on_1b",
                "op.events = 'double' AND op.on_1b IS NOT NULL "
                "AND (op.post_on_3b IS NULL OR op.post_on_3b = op.on_1b)",
                "op.runner_1b_dest = 4 OR (op.runner_1b_dest = 0 AND op.runner_1b_out_advancing)",
                "op.runner_1b_dest = 4",
                "FALSE",
            ),
            (
                4,
                3,
                4,
                "op.on_3b",
                f"{s4} AND op.on_3b IS NOT NULL AND {no_double_off.format(b='3b')}",
                "op.runner_3b_dest = 4 OR (op.runner_3b_dest = 0 AND op.runner_3b_out_advancing)",
                "op.runner_3b_dest = 4",
                "FALSE",
            ),
            (
                4,
                2,
                3,
                "op.on_2b",
                f"{s4} AND op.on_2b IS NOT NULL "
                "AND (op.post_on_3b IS NULL OR op.post_on_3b = op.on_2b) "
                f"AND {no_double_off.format(b='2b')}",
                "op.runner_2b_dest IN (3, 4) "
                "OR (op.runner_2b_dest = 0 AND op.runner_2b_out_advancing)",
                "op.runner_2b_dest IN (3, 4)",
                "op.runner_2b_dest = 4",
            ),
            (
                4,
                1,
                2,
                "op.on_1b",
                f"{s4} AND op.on_1b IS NOT NULL "
                "AND (op.post_on_2b IS NULL OR op.post_on_2b = op.on_1b) "
                f"AND {no_double_off.format(b='1b')}",
                "op.runner_1b_dest IN (2, 3, 4) "
                "OR (op.runner_1b_dest = 0 AND op.runner_1b_out_advancing)",
                "op.runner_1b_dest IN (2, 3, 4)",
                "op.runner_1b_dest IN (3, 4)",
            ),
            (
                5,
                0,
                2,
                "op.batter_id",
                "op.events = 'single' AND (op.post_on_2b IS NULL OR op.post_on_2b = op.batter_id)",
                "op.batter_dest >= 2 OR op.batter_dest = 0",
                "op.batter_dest >= 2",
                "op.batter_dest >= 3",
            ),
            (
                5,
                0,
                3,
                "op.batter_id",
                "op.events = 'double' AND (op.post_on_3b IS NULL OR op.post_on_3b = op.batter_id)",
                "op.batter_dest >= 3 OR op.batter_dest = 0",
                "op.batter_dest >= 3",
                "op.batter_dest = 4",
            ),
        ]

        selects = []
        for scen, frm, tgt, runner, where, att, safe, extra in decisions:
            selects.append(f"""
            SELECT
                op.pitch_id, op.game_pk, op.at_bat_number, op.pitch_number,
                op.game_date, op.season,
                {scen}::SMALLINT                    AS scenario,
                {frm}::SMALLINT                     AS from_base,
                {tgt}::SMALLINT                     AS target_base,
                {runner}::INTEGER                   AS runner_id,
                op.fielder_player_id::INTEGER       AS fielder_id,
                op.fielded_by_position::SMALLINT    AS fielder_pos,
                op.outs::SMALLINT                   AS outs,
                op.exit_velo, op.launch_angle, op.spray_angle, op.hit_distance,
                COALESCE({att}, FALSE)              AS attempted,
                COALESCE({safe}, FALSE)             AS safe,
                COALESCE({extra}, FALSE)            AS error_extra,
                {recency_expr}                      AS recency_weight
            FROM sim.outcome_pool op
            WHERE op.season IN ({season_list})
              AND op.batter_dest IS NOT NULL
              AND COALESCE(op.dest_outs_consistent, FALSE)
              AND {where}""")

        self._conn.execute(
            f"DELETE FROM sim.advancement_opportunity_pool WHERE season IN ({season_list})"
        )
        self._conn.execute(
            "INSERT INTO sim.advancement_opportunity_pool\n" + "\nUNION ALL\n".join(selects)
        )
        log.info("  sim.advancement_opportunity_pool done.")
        _record_pool_build(self._conn, "advancement_opportunity_pool", seasons, ref_season)
