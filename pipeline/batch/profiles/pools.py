"""The simulation pools the sampler draws from, and the tables built beside them."""

from __future__ import annotations

import logging

from pipeline.batch.profiles.base import ComputorBase
from pipeline.batch.profiles.pool_meta import (
    _canonical_ref_season,
    _recency_weight_sql,
    _record_pool_build,
    _require_steal_pool_0031,
    _seasons_needing_rebuild,
)
from pipeline.batch.profiles.sql import (
    _ASSIST_SLOTS,
    _PUTOUT_SLOTS,
    SQL_OUTCOME_TYPE,
    SQL_PITCHER_PITCH_COUNT,
    SQL_TIMES_THROUGH_ORDER,
    sql_credit_mask,
)
from pipeline.statcast_events import (
    sql_batter_dest,
    sql_play_outs,
    sql_runner_dest,
    sql_steal_attempt,
    sql_steal_success,
)

log = logging.getLogger("player_profile_computor")


class SimPoolsMixin(ComputorBase):
    """Builds the ``sim.*`` pools, ``sim.ibb_rates`` and ``derived.at_bat_situations``."""

    def _build_pitch_pool(self, seasons: list[int], incremental: bool = False) -> None:
        """
        Rebuild sim.pitch_pool from raw.pitches.
        Excludes quality-flagged rows.  Adds:
          - runners_state bitmask
          - score_diff
          - outcome_type classification
          - prev_pitch_velo / ivb / hb / outcome (LAG window)
          - pitch_id surrogate key (row_number over natural sort)
          - recency_weight (SIM-076)
        When ``incremental`` (SIM-095), only seasons whose source changed rebuild.
        """
        ref_season = _canonical_ref_season(self._conn, seasons)
        if incremental:
            seasons = _seasons_needing_rebuild(self._conn, "pitch_pool", seasons)
            if not seasons:
                log.info("Building sim.pitch_pool … all seasons fresh; skipped.")
                return
        log.info("Building sim.pitch_pool … (seasons=%s)", seasons)
        season_list = ", ".join(str(s) for s in seasons)
        recency_expr = _recency_weight_sql(
            "CAST(EXTRACT(year FROM game_date) AS SMALLINT)", ref_season
        )

        # Delete existing rows for these seasons, then insert
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
                    (CASE WHEN on_1b IS NOT NULL THEN 1 ELSE 0 END
                     + CASE WHEN on_2b IS NOT NULL THEN 2 ELSE 0 END
                     + CASE WHEN on_3b IS NOT NULL THEN 4 ELSE 0 END)::SMALLINT
                                                        AS runners_state,
                    inning,
                    -- Score diff capped at ±5 for similarity scoring
                    GREATEST(-5, LEAST(5, bat_score - fld_score))::SMALLINT
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
                    (COALESCE(passed_ball_wild_pitch, FALSE)
                     OR (events IN ('strikeout', 'strikeout_double_play')
                         AND (des ILIKE '%wild pitch%' OR des ILIKE '%passed ball%')))
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

    # ------------------------------------------------------------------
    # 9. sim.outcome_pool
    # ------------------------------------------------------------------

    def _build_outcome_pool(self, seasons: list[int], incremental: bool = False) -> None:
        """
        Rebuilds sim.outcome_pool as the in-play subset of sim.pitch_pool
        with full batted ball detail and fielding outcome columns added.
        Adds recency_weight (SIM-076). When ``incremental`` (SIM-095), only
        seasons whose source changed rebuild.

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

    # ------------------------------------------------------------------
    # 9b. sim.ibb_rates (SIM-515)
    # ------------------------------------------------------------------

    def _build_ibb_rates(self, seasons: list[int]) -> None:
        """SIM-515: the measured intentional-walk rate per hard-filtered cell.

        The loop draws ONCE per plate appearance at ``issued / opportunities``
        for the PA's entering cell (runners_state, outs, inning>=7,
        |bat_score-fld_score|<=1) — the data replaces the hand-tuned per-pitch
        formula that issued 2.64x MLB's IBB volume.

        The numerator reads ``raw.play_events`` ``intent_walk`` rows (they
        carry the pre-play cell directly). The denominator is every PA that
        ENTERED the cell: the pitched PAs (their first pitch's state) plus the
        no-pitch IBB PAs, which have no ``raw.pitches`` rows at all. A tiny
        numerator/denominator cell mismatch is possible when a mid-PA steal
        moved the runners before the IBB — ``GREATEST`` clamps so a rate never
        exceeds 1. Full recompute every run (the table is ~48 rows and
        window-scoped, so the per-season watermark machinery does not apply)."""
        season_list = ", ".join(str(s) for s in seasons)
        log.info("Building sim.ibb_rates … (seasons=%s)", seasons)
        self._conn.execute("DELETE FROM sim.ibb_rates")
        self._conn.execute(f"""
            INSERT INTO sim.ibb_rates
            WITH first_pitch AS (
                SELECT game_pk, at_bat_number,
                       (CASE WHEN on_1b IS NOT NULL THEN 1 ELSE 0 END
                        + CASE WHEN on_2b IS NOT NULL THEN 2 ELSE 0 END
                        + CASE WHEN on_3b IS NOT NULL THEN 4 ELSE 0 END)::SMALLINT
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

    # ------------------------------------------------------------------
    # 10. sim.stolen_base_pool
    # ------------------------------------------------------------------

    def _build_stolen_base_pool(self, seasons: list[int], incremental: bool = False) -> None:
        """
        Builds sim.stolen_base_pool from all SB attempts in raw.pitches.
        Denormalizes runner/catcher/pitcher metrics from the derived tables
        so the simulation engine can score similarity without joins at runtime.
        Adds recency_weight (SIM-076). When ``incremental`` (SIM-095), only
        seasons whose source changed rebuild.
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
        """SIM-468: rebuild sim.steal_opportunity_pool — one row per pitch where a
        steal was POSSIBLE, attempted or not.

        `sim.stolen_base_pool` holds only ATTEMPTS, so a draw over it attempts a
        steal 100% of the time — it has no denominator. This pool keeps the
        `attempted` flag beside `success`, so ONE draw (SIM-474) answers both
        "does the runner go" and "safe or caught", and the catcher affects the
        DECISION, not only the outcome.

        An opportunity is the LEAD open-base pair, matching the live decision:
          * a runner on 1B with 2B open  -> target_base 2 (the runner on 1B)
          * a runner on 2B with 3B open  -> target_base 3 (the runner on 2B),
            including 1B+2B (the lead runner drives a double steal)
        Steals of home are out of scope (SIM-468 ticket text).

        The pre-pitch situation columns (`outs`, the count, the base state) are
        trustworthy ONLY after the SIM-488 re-sweep — the pre-sweep `outs`
        column was stale-by-one-play on 46% of plate appearances.

        SIM-554: each pitch row carries the class of its pitch (`pitch_class`,
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
        pickoff_recency_expr = _recency_weight_sql("e.season", ref_season)  # SIM-554
        pickoff_cte = self._pickoff_outcomes_cte(season_list)  # SIM-507

        # SIM-554: probe before the DELETE (a failed positional INSERT would
        # leave the season with no steal rows).
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

    def _build_advancement_opportunity_pool(
        self, seasons: list[int], incremental: bool = False
    ) -> None:
        """SIM-510: rebuild sim.advancement_opportunity_pool — one row per
        discretionary runner-advancement decision, attempted or not.

        The five scenarios are the ONLY discretionary movements the SIM-511
        fielding draw normalizes away (the double-count guard); this pool is
        where those movements become data. Non-attempt rows are the point:
        one SIM-512 draw answers "does he go" and "safe or out" together —
        the SIM-468 denominator lesson.

        Reads the SIM-510 transition columns on sim.outcome_pool, so it must
        run AFTER ``_build_outcome_pool`` in the same recompute. Rows whose
        transition failed the outs-accounting guard are excluded.

        ⚠ The INSERT is positional (no column list): the SELECT order MUST
        match the DDL column order in db/schemas/02_duckdb_schema.sql
        (migration 0018).
        """
        ref_season = _canonical_ref_season(self._conn, seasons)
        if incremental:
            seasons = _seasons_needing_rebuild(self._conn, "advancement_opportunity_pool", seasons)
            if not seasons:
                log.info("Building sim.advancement_opportunity_pool … all seasons fresh; skipped.")
                return
        log.info("Building sim.advancement_opportunity_pool … (seasons=%s)", seasons)
        season_list = ", ".join(str(s) for s in seasons)
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

    def _build_at_bat_situations(self, seasons: list[int]) -> None:
        """
        SIM-408: materialize derived.at_bat_situations — one row per plate
        appearance capturing the pre-PA game state the Step 2.9 Situation KDTree
        engine indexes (similarity/engines/situation_similarity.py).  The engine
        SELECTed this table but the computor never built it, so the situation
        engine indexed 0 rows and normalized a degenerate empty matrix; this
        closes that gap.

        One row per (game_pk, at_bat_number), taken at the FIRST pitch of the PA
        so outs / base-state / score reflect the situation the batter came up to.
        ``leverage_index`` replicates :func:`compute_leverage_index` as a SQL
        expression (inning × score-diff × runners × outs factors).  Idempotent
        via INSERT OR REPLACE on the (game_pk, at_bat_number) PK.
        """
        log.info("Building derived.at_bat_situations …")
        season_list = ", ".join(str(s) for s in seasons)

        self._conn.execute(f"""
            INSERT OR REPLACE INTO derived.at_bat_situations (
                play_id, game_pk, season, venue_id, at_bat_number,
                inning, top_or_bottom, outs_when_up,
                on_1b, on_2b, on_3b, home_score, away_score,
                leverage_index, pitcher_pitch_count, batter_pa_count
            )
            WITH clean AS (
                SELECT
                    game_pk, at_bat_number, pitch_number, season, venue_id,
                    inning, inning_topbot, outs,
                    on_1b, on_2b, on_3b,
                    home_score, away_score, bat_score, fld_score,
                    batter,
                    -- pitches thrown by this pitcher in this game up to & incl. this pitch
                    COUNT(*) OVER (
                        PARTITION BY game_pk, pitcher
                        ORDER BY at_bat_number, pitch_number
                        ROWS UNBOUNDED PRECEDING
                    ) AS pitcher_pitches_thru
                FROM pg.raw.pitches
                WHERE data_quality_flag = FALSE
                  AND season IN ({season_list})
            ),
            pa AS (
                SELECT
                    *,
                    -- batter's PA sequence number in the game (ties share a rank,
                    -- so every pitch of the same PA gets the same value)
                    DENSE_RANK() OVER (
                        PARTITION BY game_pk, batter ORDER BY at_bat_number
                    ) AS batter_pa_seq
                FROM clean
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY game_pk, at_bat_number ORDER BY pitch_number
                ) = 1
            )
            SELECT
                CAST(game_pk AS VARCHAR) || '-' || CAST(at_bat_number AS VARCHAR) AS play_id,
                game_pk,
                season,
                venue_id,
                at_bat_number,
                inning,
                CASE WHEN inning_topbot = 'Bot' THEN 1 ELSE 0 END                 AS top_or_bottom,
                outs                                                              AS outs_when_up,
                CASE WHEN on_1b IS NOT NULL THEN 1 ELSE 0 END                     AS on_1b,
                CASE WHEN on_2b IS NOT NULL THEN 1 ELSE 0 END                     AS on_2b,
                CASE WHEN on_3b IS NOT NULL THEN 1 ELSE 0 END                     AS on_3b,
                home_score,
                away_score,
                -- compute_leverage_index(inning, outs, runners_state, bat-fld) in SQL
                (CASE WHEN inning <= 3 THEN 0.6 WHEN inning <= 6 THEN 0.9
                      WHEN inning <= 8 THEN 1.3 ELSE 1.5 END)
                  * GREATEST(0.3, 1.5 - LEAST(ABS(bat_score - fld_score), 5) * 0.24)
                  * (1.0 + ((CASE WHEN on_1b IS NOT NULL THEN 1 ELSE 0 END)
                          + (CASE WHEN on_2b IS NOT NULL THEN 1 ELSE 0 END)
                          + (CASE WHEN on_3b IS NOT NULL THEN 1 ELSE 0 END)) * 0.15)
                  * (CASE WHEN outs < 2 THEN 1.0 ELSE 1.1 END)                    AS leverage_index,
                GREATEST(pitcher_pitches_thru - 1, 0)                             AS pitcher_pitch_count,
                batter_pa_seq                                                     AS batter_pa_count
            FROM pa
        """)
        log.info("  derived.at_bat_situations done.")
