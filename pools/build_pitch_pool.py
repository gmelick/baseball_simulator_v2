"""Build the pitch pool, ``sim.pitch_pool``, the rows the pitch draw samples."""

from __future__ import annotations

import logging

from pools.pool_metadata import (
    _recency_weight_sql,
    _record_pool_build,
    _seasons_to_build,
    sql_runners_state,
    sql_score_diff,
)
from profiles.base import ComputorBase
from profiles.sql import (
    SQL_OUTCOME_TYPE,
    SQL_PITCHER_PITCH_COUNT,
    SQL_TIMES_THROUGH_ORDER,
    sql_seasons,
)

log = logging.getLogger("player_profile_computor")

#: The got-away label of a ``raw.pitches`` row: TRUE on the parser's per-pitch
#: passed-ball or wild-pitch flag, or on an uncaught third strike (the last pitch
#: of a strikeout whose description names a wild pitch or a passed ball).
SQL_GOT_AWAY = """(COALESCE(passed_ball_wild_pitch, FALSE)
                     OR (events IN ('strikeout', 'strikeout_double_play')
                         AND (des ILIKE '%wild pitch%' OR des ILIKE '%passed ball%')))"""


class PitchPoolMixin(ComputorBase):
    """Build the pitch pool."""

    def _build_pitch_pool(self, seasons: list[int], incremental: bool = False) -> None:
        """Rebuild ``sim.pitch_pool`` from ``raw.pitches``, without the quality-flagged rows.

        The build adds to each pitch:
          - ``pitch_id``, a key made of the game, the at-bat and the pitch number;
          - the base-state bitmask and the score margin;
          - the pitch class (``outcome_type``);
          - the previous pitch's speed, movement and result code (``type``);
          - the recency weight;
          - the catcher and the got-away label;
          - the batting side and the two fatigue columns.

        With ``incremental``, only the seasons whose source or builder changed
        rebuild.

        ⚠ The INSERT is positional (no column list): the SELECT order MUST match
        the DDL column order in db/schemas/02_duckdb_schema.sql.
        """
        plan = _seasons_to_build(self._conn, "pitch_pool", seasons, incremental)
        if plan is None:
            return
        seasons, ref_season = plan
        season_list = sql_seasons(seasons)
        recency_expr = _recency_weight_sql(
            "CAST(EXTRACT(year FROM game_date) AS SMALLINT)", ref_season
        )

        self._conn.execute(f"DELETE FROM sim.pitch_pool WHERE season IN ({season_list})")

        self._conn.execute(f"""
            INSERT INTO sim.pitch_pool

            WITH ordered AS (
                SELECT
                    (game_pk || LPAD(at_bat_number::VARCHAR, 3, '0') || LPAD(pitch_number::VARCHAR, 3, '0'))::BIGINT AS pitch_id,
                    game_pk,
                    at_bat_number,
                    pitch_number,
                    game_date,
                    CAST(EXTRACT(year FROM game_date) AS SMALLINT) AS season,
                    pitcher                             AS pitcher_id,
                    p_throws,
                    batter                              AS batter_id,
                    -- SIM-345 pool `stand` contract: the pool's `stand` column is
                    -- the RESOLVED batting side for the PA — never 'S'.
                    --
                    -- SIM-440: this was a `CASE WHEN bat_hand IN ('L','R') THEN
                    -- bat_hand ELSE stand END`, written on the belief that
                    -- `bat_hand` was the per-PA value and `stand` the declared
                    -- one. That is exactly backwards (see the canonical note on
                    -- raw.pitches in db/schemas/01_postgres_schema.sql). The old
                    -- expression still produced the right answer, but only by
                    -- accident: a switch hitter's bat_hand is 'S', which is not
                    -- IN ('L','R'), so it fell through to `stand` — the correct
                    -- column — and for everyone else the two agree. Reading
                    -- `stand` directly is identical in output and removes the
                    -- trap that the next reader would copy.
                    stand,
                    release_speed                       AS velo,
                    break_vertical_induced              AS ivb,
                    break_horizontal                    AS hb,
                    release_spin_rate                   AS spin_rate,
                    spin_axis,
                    release_pos_x                       AS release_x,
                    release_pos_z                       AS release_z,
                    release_extension                   AS release_ext,
                    plate_x,
                    plate_z,
                    zone,
                    balls                               AS count_balls,
                    strikes                             AS count_strikes,
                    outs,
                    -- Runners state bitmask: bit0=1B, bit1=2B, bit2=3B
                    {sql_runners_state()}
                                                        AS runners_state,
                    inning,
                    -- Score diff capped at ±5 for similarity scoring
                    {sql_score_diff()}
                                                        AS score_diff,

                    -- Prev pitch context via LAG window over (game, at_bat, pitch_number)
                    LAG(release_speed,          1) OVER w AS prev_pitch_velo,
                    LAG(break_vertical_induced, 1) OVER w AS prev_pitch_ivb,
                    LAG(break_horizontal,       1) OVER w AS prev_pitch_hb,
                    LAG(type,                   1) OVER w AS prev_pitch_outcome,

                    -- Outcome type classification from the MLB Gameday pitch
                    -- `type` code (SIM-421 fix).  The in-play code splits THREE
                    -- ways -- X=out, D=hit (no out), E=hit with run(s) -- so the
                    -- old `type='X'`-only mapping silently dropped ~all hits +
                    -- home runs into 'ball' (the no-balls-in-play defect).  TRIM
                    -- guards the space-padded Gameday codes.
                    -- SIM-509: an HBP pitch carries a ball-class type code, so
                    -- the old mapping turned every simulated HBP into ball four
                    -- — a WALK. Worth the whole 0.397/team-game 2025 HBP rate
                    -- (~82% of the measured BB surplus). The events branch must
                    -- come FIRST.
                    -- SIM-553: the expression is the module constant
                    -- SQL_OUTCOME_TYPE, rendered from the named code sets; a
                    -- two-strike foul tip or foul bunt is strike three
                    -- ('swinging_strike'), and an unknown code fails the insert.
                    {SQL_OUTCOME_TYPE}                  AS outcome_type,
                    events,
                    {recency_expr} AS recency_weight,

                    -- SIM-517 (migration 0022): the receiving context. The
                    -- catcher is the pitch's own fielder_2; got_away = the
                    -- parser's per-pitch passed-ball/wild-pitch flag OR an
                    -- uncaught third strike (a strikeout-final pitch whose PA
                    -- description names a wild pitch / passed ball — the
                    -- exact label, zero mid-PA leakage; see migration 0021).
                    fielder_2                           AS catcher_id,
                    {SQL_GOT_AWAY}
                                                        AS got_away,

                    -- SIM-518 (migration 0023): the draw-conditioning
                    -- columns, appended LAST (the INSERT is positional).
                    -- bat_home: the home team bats. The two fatigue columns
                    -- are the module constants SQL_PITCHER_PITCH_COUNT /
                    -- SQL_TIMES_THROUGH_ORDER, unit-tested against the live
                    -- helper. Both count quality-passing pitches only (the
                    -- WHERE below runs first) — a <=0.12% drift.
                    (inning_topbot = 'Bot')             AS bat_home,
                    ({SQL_PITCHER_PITCH_COUNT})::SMALLINT AS pitcher_pitch_count,
                    ({SQL_TIMES_THROUGH_ORDER})::SMALLINT AS times_through_order

                FROM pg.raw.pitches
                WHERE data_quality_flag = FALSE
                  AND season IN ({season_list})
                WINDOW w AS (
                    PARTITION BY game_pk, at_bat_number
                    ORDER BY pitch_number
                    ROWS BETWEEN 1 PRECEDING AND CURRENT ROW
                )
            )
            SELECT * FROM ordered
        """)
        log.info("  sim.pitch_pool done.")
        _record_pool_build(self._conn, "pitch_pool", seasons, ref_season)
