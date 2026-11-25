"""Build the intentional-walk rates, ``sim.ibb_rates``."""

from __future__ import annotations

import logging

from pools.pool_metadata import sql_runners_state
from profiles.base import ComputorBase
from profiles.sql import sql_seasons

log = logging.getLogger("player_profile_computor")


class IbbRatesMixin(ComputorBase):
    """Build the intentional-walk rates."""

    def _build_ibb_rates(self, seasons: list[int]) -> None:
        """The measured intentional-walk rate per hard-filtered cell (the IBB
        rate table, SIM-515).

        The loop draws ONCE per plate appearance at ``issued / opportunities``
        for the PA's entering cell (runners_state, outs, inning>=7,
        |bat_score-fld_score|<=1).

        The numerator reads ``raw.play_events`` ``intent_walk`` rows (they
        carry the pre-play cell directly). The denominator is every PA that
        ENTERED the cell: the pitched PAs (their first pitch's state) plus the
        no-pitch IBB PAs, which have no ``raw.pitches`` rows at all. A tiny
        numerator/denominator cell mismatch is possible when a mid-PA steal
        moved the runners before the IBB — ``GREATEST`` clamps so a rate never
        exceeds 1. Full recompute every run (the table is ~48 rows and
        window-scoped, so the per-season watermark machinery does not apply)."""
        season_list = sql_seasons(seasons)
        log.info("Building sim.ibb_rates … (seasons=%s)", seasons)
        self._conn.execute("DELETE FROM sim.ibb_rates")
        self._conn.execute(f"""
            INSERT INTO sim.ibb_rates
            WITH first_pitch AS (
                SELECT game_pk, at_bat_number,
                       {sql_runners_state()}
                                                            AS runners_state,
                       LEAST(outs, 2)::SMALLINT             AS outs,
                       inning >= 7                          AS is_late,
                       ABS(bat_score - fld_score) <= 1      AS is_close,
                       ROW_NUMBER() OVER (
                           PARTITION BY game_pk, at_bat_number
                           ORDER BY pitch_number
                       ) AS rn
                FROM pg.raw.pitches
                WHERE data_quality_flag = FALSE AND season IN ({season_list})
            ),
            pitched AS (SELECT * FROM first_pitch WHERE rn = 1),
            ibb AS (
                SELECT game_pk, at_bat_number,
                       (runners_state & 7)::SMALLINT        AS runners_state,
                       LEAST(outs_before, 2)::SMALLINT      AS outs,
                       inning >= 7                          AS is_late,
                       ABS(bat_score - fld_score) <= 1      AS is_close
                FROM pg.raw.play_events
                WHERE event_type = 'intent_walk' AND season IN ({season_list})
            ),
            no_pitch_ibb AS (
                SELECT i.* FROM ibb i
                WHERE NOT EXISTS (
                    SELECT 1 FROM pitched p
                    WHERE p.game_pk = i.game_pk AND p.at_bat_number = i.at_bat_number
                )
            ),
            opp_agg AS (
                SELECT runners_state, outs, is_late, is_close, COUNT(*) AS opportunities
                FROM (
                    SELECT runners_state, outs, is_late, is_close FROM pitched
                    UNION ALL
                    SELECT runners_state, outs, is_late, is_close FROM no_pitch_ibb
                )
                GROUP BY 1, 2, 3, 4
            ),
            ibb_agg AS (
                SELECT runners_state, outs, is_late, is_close, COUNT(*) AS issued
                FROM ibb GROUP BY 1, 2, 3, 4
            )
            SELECT COALESCE(o.runners_state, i.runners_state),
                   COALESCE(o.outs, i.outs),
                   COALESCE(o.is_late, i.is_late),
                   COALESCE(o.is_close, i.is_close),
                   GREATEST(COALESCE(o.opportunities, 0), COALESCE(i.issued, 0)),
                   COALESCE(i.issued, 0)
            FROM opp_agg o
            FULL OUTER JOIN ibb_agg i
              USING (runners_state, outs, is_late, is_close)
        """)
        row = self._conn.execute("SELECT COUNT(*), SUM(issued) FROM sim.ibb_rates").fetchone()
        n, issued = (row[0], row[1]) if row and len(row) >= 2 else (0, 0)
        log.info("  sim.ibb_rates done: %s cells, %s IBB events.", n, issued)
