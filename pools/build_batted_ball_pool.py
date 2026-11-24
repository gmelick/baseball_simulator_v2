"""Build the batted-ball pool, ``sim.outcome_pool``, the rows the pitch-result and fielding draws sample."""

from __future__ import annotations

import logging

from ingest.statcast_events import (
    sql_batter_dest,
    sql_play_outs,
    sql_runner_dest,
)
from pools.pool_metadata import (
    _canonical_ref_season,
    _recency_weight_sql,
    _record_pool_build,
    _seasons_needing_rebuild,
)
from profiles.base import ComputorBase
from profiles.sql import (
    _ASSIST_SLOTS,
    _PUTOUT_SLOTS,
    sql_credit_mask,
)

log = logging.getLogger("player_profile_computor")


class BattedBallPoolMixin(ComputorBase):
    """Build the batted-ball pool."""

    def _build_outcome_pool(self, seasons: list[int], incremental: bool = False) -> None:
        """
        Rebuilds sim.outcome_pool as the in-play subset of sim.pitch_pool
        with full batted ball detail and fielding outcome columns added.
        Adds recency_weight. When ``incremental``, only the seasons whose
        source changed rebuild.

        Fielder position is looked up from the fielder_N columns so the
        simulation engine knows which positional number fielded the ball.
        """
        ref_season = _canonical_ref_season(self._conn, seasons)
        if incremental:
            seasons = _seasons_needing_rebuild(self._conn, "outcome_pool", seasons)
            if not seasons:
                log.info("Building sim.outcome_pool … all seasons fresh; skipped.")
                return
        log.info("Building sim.outcome_pool … (seasons=%s)", seasons)
        season_list = ", ".join(str(s) for s in seasons)
        recency_expr = _recency_weight_sql("pp.season", ref_season)

        self._conn.execute(f"DELETE FROM sim.outcome_pool WHERE season IN ({season_list})")

        self._conn.execute(f"""
            INSERT INTO sim.outcome_pool

            WITH bip AS (
                SELECT
                    pp.pitch_id,
                    pp.game_pk, pp.at_bat_number, pp.pitch_number,
                    pp.game_date, pp.season,
                    pp.pitcher_id, pp.p_throws,
                    pp.batter_id, pp.stand,
                    pp.velo, pp.ivb, pp.hb, pp.spin_rate, pp.spin_axis,
                    pp.release_x, pp.release_z, pp.release_ext,
                    pp.plate_x, pp.plate_z, pp.zone,
                    pp.count_balls, pp.count_strikes, pp.outs,
                    pp.runners_state, pp.inning, pp.score_diff,
                    pp.events,
                    -- Batted ball detail from raw.pitches
                    rp.launch_speed                 AS exit_velo,
                    rp.launch_angle,
                    rp.spray_angle,
                    -- SIM-051: handedness-corrected pull-relative spray angle.
                    -- Sign convention (corrected 2026-08-13, SIM-503 review):
                    -- the raw spray sign is FIELD-side, negative = left field
                    -- (measured), so under this formula NEGATIVE = pull side
                    -- for both hands. The old comment here claimed positive =
                    -- pull; the FORMULA was always hand-consistent, only the
                    -- stated sign was wrong. Check any directional threshold
                    -- against the measurement before trusting a sign.
                    --
                    -- Keyed on `stand`, NOT `bat_hand`.  `stand` is the side the
                    -- batter actually hit from in THIS plate appearance (the MLB
                    -- feed resolves switch hitters against the pitcher), and is
                    -- never 'S'.  `bat_hand` is the roster-DECLARED side and is
                    -- 'S' for switch hitters — 10.4-13.3% of all rows in every
                    -- season 2017-2025 (docs/data_quality/2026-05-20-bat-side-
                    -- coverage.md, measured; `stand` is 'S' on 0 rows).
                    --
                    -- Keying on bat_hand sent every switch-hitter batted ball to
                    -- NULL here, and build_battedball_pool_artifact filters on
                    -- `pull_relative_spray_angle IS NOT NULL` — so ~1 in 8 batted
                    -- balls was silently missing from the production batted-ball
                    -- draw.  The old comment claimed this was "gate-controlled to
                    -- <= 1% per season by SIM-160"; that gate measures the NULL
                    -- rate of the source columns, not the 'S' rate, and was never
                    -- a constraint on this expression.
                    CASE
                        WHEN rp.stand = 'R' THEN  rp.spray_angle
                        WHEN rp.stand = 'L' THEN -rp.spray_angle
                        ELSE NULL
                    END                              AS pull_relative_spray_angle,
                    rp.bb_type,
                    rp.hit_distance_sc              AS hit_distance,
                    rp.hc_x,
                    rp.hc_y,
                    -- Fielder who fielded the ball — translate player_id to position number
                    CASE
                        WHEN rp.fielded_by = rp.fielder_2 THEN 2
                        WHEN rp.fielded_by = rp.fielder_3 THEN 3
                        WHEN rp.fielded_by = rp.fielder_4 THEN 4
                        WHEN rp.fielded_by = rp.fielder_5 THEN 5
                        WHEN rp.fielded_by = rp.fielder_6 THEN 6
                        WHEN rp.fielded_by = rp.fielder_7 THEN 7
                        WHEN rp.fielded_by = rp.fielder_8 THEN 8
                        WHEN rp.fielded_by = rp.fielder_9 THEN 9
                        ELSE NULL
                    END::SMALLINT                   AS fielded_by_position,
                    CASE
                        WHEN rp.fielding_error = rp.fielder_2 THEN 2
                        WHEN rp.fielding_error = rp.fielder_3 THEN 3
                        WHEN rp.fielding_error = rp.fielder_4 THEN 4
                        WHEN rp.fielding_error = rp.fielder_5 THEN 5
                        WHEN rp.fielding_error = rp.fielder_6 THEN 6
                        WHEN rp.fielding_error = rp.fielder_7 THEN 7
                        WHEN rp.fielding_error = rp.fielder_8 THEN 8
                        WHEN rp.fielding_error = rp.fielder_9 THEN 9
                        ELSE NULL
                    END::SMALLINT                   AS fielding_error_position,
                    -- Result encoding
                    CASE rp.events
                        WHEN 'single'    THEN 1
                        WHEN 'double'    THEN 2
                        WHEN 'triple'    THEN 3
                        WHEN 'home_run'  THEN 4
                        ELSE 0
                    END::SMALLINT                   AS result_hits,
                    -- SIM-501a. Question: how many outs did the play record?
                    -- Derived from `events` (an X-typed reach event carries one
                    -- hidden runner out); `outs_on_pitch` is zero on 92.6% of
                    -- batted-ball outs in the swept data. No consumer reads
                    -- this column today: the production full-pool draw
                    -- discards it (sim_loop.py `_ro`, the SIM-496 site) and
                    -- the per-tile sampler never projects it. A correct value
                    -- here is the prerequisite for the SIM-473 advancement
                    -- draw and the SIM-494 double-play fix; a resolver that
                    -- starts trusting it must first retire the implied runner
                    -- (see the FieldingSignal note in sim_loop.py). No
                    -- caught-stealing term: no in-play row carries a steal
                    -- attempt (measured, 2024).
                    ({sql_play_outs("rp.")})::SMALLINT AS result_outs,
                    rp.runs_on_pitch::SMALLINT      AS result_runs,
                    {recency_expr} AS recency_weight,
                    -- SIM-411/413/425b: realism facts appended AFTER recency_weight to
                    -- match the migration-0012 column order (the table's INSERT is a
                    -- positional `SELECT * FROM bip`). venue_id → park run-environment
                    -- (SIM-411); fielded_by → the play's fielder identity for the
                    -- SIM-425b relative defensive nudge (fielded_by_position already
                    -- records the position 1–9). p_throws (SIM-413 platoon) is carried
                    -- by the pp.* columns above.
                    rp.venue_id::INTEGER            AS venue_id,
                    rp.fielded_by::INTEGER          AS fielder_player_id,
                    -- SIM-510 transition facts (migration 0018), appended LAST in
                    -- DDL order (the positional-INSERT trap). Identities + flags
                    -- are ETL play-truth; the dest columns encode where each body
                    -- ended (NULL = no runner; 4 = scored; 3/2/1 = post base;
                    -- 0 = retired). The SIM-511 draw applies them as the play.
                    rp.on_1b::INTEGER               AS on_1b,
                    rp.on_2b::INTEGER               AS on_2b,
                    rp.on_3b::INTEGER               AS on_3b,
                    rp.post_on_1b::INTEGER          AS post_on_1b,
                    rp.post_on_2b::INTEGER          AS post_on_2b,
                    rp.post_on_3b::INTEGER          AS post_on_3b,
                    COALESCE(rp.runner_1b_scored, FALSE)        AS runner_1b_scored,
                    COALESCE(rp.runner_2b_scored, FALSE)        AS runner_2b_scored,
                    COALESCE(rp.runner_3b_scored, FALSE)        AS runner_3b_scored,
                    COALESCE(rp.runner_1b_out_advancing, FALSE) AS runner_1b_out_advancing,
                    COALESCE(rp.runner_2b_out_advancing, FALSE) AS runner_2b_out_advancing,
                    COALESCE(rp.runner_3b_out_advancing, FALSE) AS runner_3b_out_advancing,
                    ({sql_runner_dest("1b", "rp.")})::SMALLINT  AS runner_1b_dest,
                    ({sql_runner_dest("2b", "rp.")})::SMALLINT  AS runner_2b_dest,
                    ({sql_runner_dest("3b", "rp.")})::SMALLINT  AS runner_3b_dest,
                    ({sql_batter_dest("rp.", batter_expr="pp.batter_id")})::SMALLINT
                                                    AS batter_dest,
                    -- SIM-491 (migration 0019): the batting side. 'Bot' = the
                    -- home team bats. Selected here (the rp join lives here)
                    -- but re-ordered LAST by the outer SELECT's EXCLUDE so the
                    -- positional INSERT matches the DDL (bat_home after
                    -- dest_outs_consistent).
                    (rp.inning_topbot = 'Bot')      AS bat_home,
                    -- SIM-523 part G (migration 0024): the defensive ALIGNMENT
                    -- and the fielding CHAIN (the positions credited a putout /
                    -- an assist). Appended LAST in the final SELECT below.
                    rp.fielder_2::INTEGER          AS fielder_2,
                    rp.fielder_3::INTEGER          AS fielder_3,
                    rp.fielder_4::INTEGER          AS fielder_4,
                    rp.fielder_5::INTEGER          AS fielder_5,
                    rp.fielder_6::INTEGER          AS fielder_6,
                    rp.fielder_7::INTEGER          AS fielder_7,
                    rp.fielder_8::INTEGER          AS fielder_8,
                    rp.fielder_9::INTEGER          AS fielder_9,
                    ({sql_credit_mask("rp.", _PUTOUT_SLOTS, "pp.pitcher_id")})::SMALLINT
                                                    AS putout_pos_mask,
                    ({sql_credit_mask("rp.", _ASSIST_SLOTS, "pp.pitcher_id")})::SMALLINT
                                                    AS assist_pos_mask
                FROM sim.pitch_pool pp
                JOIN pg.raw.pitches rp
                    ON rp.game_pk       = pp.game_pk
                   AND rp.at_bat_number = pp.at_bat_number
                   AND rp.pitch_number  = pp.pitch_number
                WHERE pp.outcome_type = 'in_play'
                  AND pp.season IN ({season_list})
            )
            -- SIM-510: the guard column — retired bodies must equal the
            -- events-derived result_outs. The artifact export excludes rows
            -- where this is FALSE; validation measures their rate.
            -- SIM-491: bat_home is EXCLUDEd from the star and re-appended
            -- LAST so the positional INSERT matches the 0019 DDL order
            -- (bat_home after dest_outs_consistent).
            SELECT
                bip.* EXCLUDE (bat_home, fielder_2, fielder_3, fielder_4, fielder_5, fielder_6, fielder_7, fielder_8, fielder_9, putout_pos_mask, assist_pos_mask),
                (
                    (CASE WHEN bip.runner_1b_dest = 0 THEN 1 ELSE 0 END)
                  + (CASE WHEN bip.runner_2b_dest = 0 THEN 1 ELSE 0 END)
                  + (CASE WHEN bip.runner_3b_dest = 0 THEN 1 ELSE 0 END)
                  + (CASE WHEN bip.batter_dest    = 0 THEN 1 ELSE 0 END)
                ) = bip.result_outs                 AS dest_outs_consistent,
                bip.bat_home,
                -- SIM-523 part G: the alignment + the chain, LAST (migration 0024).
                bip.fielder_2,
                bip.fielder_3,
                bip.fielder_4,
                bip.fielder_5,
                bip.fielder_6,
                bip.fielder_7,
                bip.fielder_8,
                bip.fielder_9,
                bip.putout_pos_mask,
                bip.assist_pos_mask
            FROM bip
        """)
        log.info("  sim.outcome_pool done.")
        _record_pool_build(self._conn, "outcome_pool", seasons, ref_season)
