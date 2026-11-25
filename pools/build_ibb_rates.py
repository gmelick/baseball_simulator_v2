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
        """Rebuild ``sim.ibb_rates``: the real intentional-walk rate of each hard-filtered cell.

        A cell is the state a plate appearance enters: the base state, the outs,
        a late inning (the 7th or later) and a close score (a margin of 1 run or
        less). The loop draws once per plate appearance, at ``issued /
        opportunities`` for the cell it enters.

        The numerator counts the ``intent_walk`` rows of ``raw.play_events``,
        which carry the cell before the play. The denominator counts every plate
        appearance that entered the cell: the pitched ones (the state at the
        first pitch) and the intentional walks with no pitch, which have no
        ``raw.pitches`` row. A steal during the plate appearance can put a walk
        and its opportunity in different cells, so ``GREATEST`` keeps each rate
        at 1 or less.

        The table holds at most 96 cells, so each run deletes it whole and
        rebuilds it with no per-season watermark. The rebuild reads only the
        ``seasons`` of this run: the nightly run passes one season.
        """
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
