"""The batter season profiles, with the physical swing and stance block."""

from __future__ import annotations

import logging
from datetime import date

from pipeline.batch.profiles.base import ComputorBase
from pipeline.batch.profiles.constants import MIN_BATTER_PA
from pipeline.batch.profiles.sql import (
    COMPETITIVE_BAT_SPEED_MIN,
    HARD_HIT_MIN_VELO,
    SQL_IN_PLAY,
    SQL_PULL_SPRAY,
    SQL_WHIFF,
    _barrel_case_sql,
    _sql_pitch_swing_agg,
    _sql_stance_pivot,
    _sql_stance_select,
    _sql_swing_pivot,
    _sql_swing_select,
)

log = logging.getLogger("player_profile_computor")


class BatterProfilesMixin(ComputorBase):
    """Builds ``derived.batter_season_metrics``."""

    def _compute_batter_profiles(self, seasons: list[int], asof: date | None = None) -> None:
        """Rebuild ``derived.batter_season_metrics`` for ``seasons``.

        ``asof`` is the date the profile's data runs through. Every source in
        this query is filtered by it, so a profile built for a backtest cannot
        contain a pitch, a swing or a stance measurement recorded after the
        date being simulated. Left unset it means "everything known today",
        which is what a live simulation wants.

        The cutoff is not a per-season idea. A season that ENDED before the
        cutoff is admissible whole — a 2023 aggregate holds nothing that
        postdates a 2025 date. Only the season containing the cutoff is
        truncated, and the date arithmetic does that without special cases.
        """
        asof_date = asof or date.today()
        asof_sql = asof_date.isoformat()
        log.info("Computing batter profiles (as of %s) …", asof_sql)
        season_list = ", ".join(str(s) for s in seasons)

        # Refuse, before any write, a rebuild that would leave the
        # table at two cutoffs.
        self._refuse_a_mixed_cutoff("derived.batter_season_metrics", seasons, asof_date)

        if asof is not None:
            # A point-in-time build has to REMOVE as well as write.
            # INSERT OR REPLACE only overwrites rows the query produces, so two
            # kinds of stale row would otherwise survive a backtest rebuild and
            # leak the future:
            #   * a season that had not started at the cutoff (there is no 2025
            #     batter on 15 April 2024), and
            #   * a batter inside the cutoff season with no plate appearances
            #     yet, whose full-season row from an earlier build would remain.
            # Only the cutoff path deletes; the live nightly run only inserts
            # or replaces.
            self._conn.execute(f"""
                DELETE FROM derived.batter_season_metrics
                WHERE season IN ({season_list}) OR season > {asof_date.year}
            """)

        self._conn.execute(f"""
            INSERT OR REPLACE INTO derived.batter_season_metrics

            WITH batter_pitches AS (
                SELECT
                    batter                              AS batter_id,
                    season,
                    MAX(bat_hand)                       AS bats,
                    COUNT(*)                            AS sample_pitches,

                    -- PA count (events is non-null on the last pitch of each PA)
                    COUNT(DISTINCT CASE WHEN events IS NOT NULL THEN game_pk || '-' || at_bat_number END) AS sample_pa,

                    -- Plate discipline
                    -- First-pitch TAKE: the batter did NOT swing on pitch_number=1.
                    -- SIM-501: this counted `type NOT IN (...)`, which is the set of
                    -- codes that REQUIRE a swing — so the column held the first-pitch
                    -- SWING rate under a name and a comment that both say take. Every
                    -- consumer read it backwards. The non-swing codes are ball, called
                    -- strike, hit by pitch, pitchout and blocked ball.
                    SUM(CASE WHEN pitch_number = 1 AND type IN ('B', 'C', 'H', 'P', '*B') THEN 1.0 ELSE 0 END) /
                        NULLIF(COUNT(CASE WHEN pitch_number = 1 THEN 1 END), 0) AS first_pitch_take_rate,

                    -- O-swing
                    SUM(CASE WHEN zone NOT BETWEEN 1 AND 9 AND type NOT IN ('B', 'C', 'H', 'P', '*B') THEN 1.0 ELSE 0
                        END) / NULLIF(SUM(CASE WHEN zone NOT BETWEEN 1 AND 9 THEN 1 ELSE 0 END), 0) AS o_swing_rate,

                    -- Z-SWING: the batter DID swing at a pitch in the zone.
                    -- SIM-501: this counted `type IN (...)`, the NON-swing set, so the
                    -- column held the z-TAKE rate. `o_swing_rate` on the line above uses
                    -- `NOT IN` and is correct, which is what makes this one easy to miss:
                    -- the two sit three lines apart and read almost identically.
                    SUM(CASE WHEN zone BETWEEN 1 AND 9 AND type NOT IN ('B', 'C', 'H', 'P', '*B') THEN 1.0 ELSE 0 END)
                        * 1.0 / NULLIF(SUM(CASE WHEN zone BETWEEN 1 AND 9 THEN 1 ELSE 0 END), 0) AS z_swing_rate,

                    -- Whiff: swinging strikes / swings
                    SUM(CASE WHEN {SQL_WHIFF} THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN type NOT IN
                        ('B', 'C', 'H', 'P', '*B') THEN 1 ELSE 0 END), 0) AS whiff_rate,

                    -- Contact: in-play / swings
                    SUM(CASE WHEN type IN ('D', 'E', 'F', 'X') THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN type NOT IN
                        ('B', 'C', 'H', 'P', '*B') THEN 1 ELSE 0 END), 0) AS contact_rate,

                    -- Walk / K rates (per PA)
                    -- SIM-504: `= 'walk'` by DECISION (uBB — see the is_bb note).
                    SUM(CASE WHEN events = 'walk' THEN 1.0 ELSE 0 END) /
                        NULLIF(COUNT(DISTINCT CASE WHEN events IS NOT NULL THEN game_pk || '-' || at_bat_number END), 0)
                        AS walk_rate,

                    SUM(CASE WHEN events IN ('strikeout','strikeout_double_play') THEN 1.0 ELSE 0 END) /
                        NULLIF(COUNT(DISTINCT CASE WHEN events IS NOT NULL THEN game_pk || '-' || at_bat_number END), 0)
                        AS k_rate,

                    -- Batted ball profile (of BIP)
                    SUM(CASE WHEN bb_type = 'ground_ball' THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN {SQL_IN_PLAY} THEN 1 ELSE 0 END), 0) AS gb_rate,

                    SUM(CASE WHEN bb_type = 'fly_ball' THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN {SQL_IN_PLAY} THEN 1 ELSE 0 END), 0) AS fb_rate,

                    -- Spray direction (SIM-503). Question: what share of this
                    -- batter's balls in play went to the PULL side? The raw
                    -- spray sign is FIELD-side (negative = left field), so the
                    -- old `spray_angle < -15` read the LEFT-FIELD rate — pull
                    -- for a righty, OPPO for a lefty. These two columns are the
                    -- ones the batter engine consumes (weights 0.760 / 0.792),
                    -- so the sign fix must land HERE, not only on the platoon
                    -- splits. The denominator keeps only rows with a measured
                    -- spray angle, so pull + center + oppo sums to 1.
                    SUM(CASE WHEN {SQL_PULL_SPRAY} < -15 THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN {SQL_IN_PLAY} AND spray_angle IS NOT NULL
                        THEN 1 ELSE 0 END), 0) AS pull_rate,

                    SUM(CASE WHEN {SQL_PULL_SPRAY} > 15 THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN {SQL_IN_PLAY} AND spray_angle IS NOT NULL
                        THEN 1 ELSE 0 END), 0) AS oppo_rate,

                    -- Exit velocity & launch angle (on contact only)
                    AVG(CASE WHEN launch_speed IS NOT NULL THEN launch_speed END) AS avg_exit_velo,
                    MAX(launch_speed)                   AS max_exit_velo,
                    AVG(CASE WHEN launch_angle IS NOT NULL THEN launch_angle END) AS avg_launch_angle,

                    -- Hard hit rate (>= 95 mph exit velo)
                    SUM(CASE WHEN launch_speed >= {HARD_HIT_MIN_VELO} THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN {SQL_IN_PLAY} THEN 1 ELSE 0 END), 0) AS hard_hit_rate,

                    -- Barrel rate (Statcast sliding-scale: EV>=98 & LA band widening with EV)
                    SUM(CASE WHEN {_barrel_case_sql()} THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN {SQL_IN_PLAY} THEN 1 ELSE 0 END), 0) AS barrel_rate,

                    -- HR rate per PA
                    SUM(CASE WHEN events = 'home_run' THEN 1.0 ELSE 0 END) /
                        NULLIF(COUNT(DISTINCT CASE WHEN events IS NOT NULL THEN game_pk || '-' || at_bat_number END), 0)
                        AS hr_rate,

                    -- Platoon split sample sizes
                    COUNT(DISTINCT CASE WHEN p_throws = 'L' AND events IS NOT NULL THEN game_pk || '-' || at_bat_number
                        END) AS sample_pa_vs_l,
                    COUNT(DISTINCT CASE WHEN p_throws = 'R' AND events IS NOT NULL THEN game_pk || '-' || at_bat_number
                        END) AS sample_pa_vs_r,

                    -- Platoon splits — vs LHP
                    SUM(CASE WHEN p_throws='L' AND zone NOT BETWEEN 1 AND 9 AND type NOT IN ('B', 'C', 'H', 'P', '*B')
                        THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN p_throws='L' AND zone NOT BETWEEN 1 AND 9 THEN 1
                        ELSE 0 END), 0) AS o_swing_rate_vs_l,
                    -- SIM-522: this leg kept the inverted `type IN (...)` predicate after
                    -- SIM-501 fixed the season-level z_swing_rate, so it held the z-TAKE
                    -- rate (live 2024: 0.32 vs 0.68). Same fix: a swing is NOT a non-swing.
                    SUM(CASE WHEN p_throws='L' AND zone BETWEEN 1 AND 9 AND type NOT IN ('B', 'C', 'H', 'P', '*B')
                        THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN p_throws='L' AND zone BETWEEN 1 AND 9 THEN 1
                        ELSE 0 END), 0) AS z_swing_rate_vs_l,
                    SUM(CASE WHEN p_throws='L' AND {SQL_WHIFF} THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE
                        WHEN p_throws='L' AND type NOT IN ('B', 'C', 'H', 'P', '*B') THEN 1 ELSE 0 END), 0)
                        AS whiff_rate_vs_l,
                    SUM(CASE WHEN p_throws='L' AND type IN ('D', 'E', 'F', 'X') THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='L' AND type NOT IN ('B', 'C', 'H', 'P', '*B') THEN 1 ELSE 0 END)
                        , 0) AS contact_rate_vs_l,
                    SUM(CASE WHEN p_throws='L' AND events = 'walk' THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='L' AND events IS NOT NULL THEN 1 ELSE 0 END), 0)
                        AS walk_rate_vs_l,
                    SUM(CASE WHEN p_throws='L' AND events IN ('strikeout','strikeout_double_play') THEN 1.0 ELSE 0 END)
                        / NULLIF(SUM(CASE WHEN p_throws='L' AND events IS NOT NULL THEN 1 ELSE 0 END), 0) AS k_rate_vs_l,
                    SUM(CASE WHEN p_throws='L' AND bb_type='ground_ball' THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN
                        p_throws='L' AND {SQL_IN_PLAY} THEN 1 ELSE 0 END), 0) AS gb_rate_vs_l,
                    SUM(CASE WHEN p_throws='L' AND bb_type = 'fly_ball' THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='L' AND {SQL_IN_PLAY} THEN 1 ELSE 0 END), 0) AS fb_rate_vs_l,
                    -- SIM-503 + SIM-457. Question: of this batter's balls in play
                    -- vs THIS pitcher hand, what share went to the pull side?
                    -- Three defects fixed here: (1) neither term filtered on
                    -- p_throws, so _vs_l and _vs_r were the same number; (2) the
                    -- raw spray sign is field-side, so `spray_angle < -15` read
                    -- LEFT FIELD — pull for a righty, oppo for a lefty (see
                    -- SQL_PULL_SPRAY); (3) the denominator kept only out plays.
                    SUM(CASE WHEN p_throws='L' AND {SQL_PULL_SPRAY} < -15 THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='L' AND {SQL_IN_PLAY} AND spray_angle IS NOT NULL
                        THEN 1 ELSE 0 END), 0) AS pull_rate_vs_l,
                    SUM(CASE WHEN p_throws='L' AND {SQL_PULL_SPRAY} > 15 THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='L' AND {SQL_IN_PLAY} AND spray_angle IS NOT NULL
                        THEN 1 ELSE 0 END), 0) AS oppo_rate_vs_l,
                    AVG(CASE WHEN p_throws='L' AND launch_speed IS NOT NULL THEN launch_speed END) AS avg_exit_velo_vs_l,
                    SUM(CASE WHEN {_barrel_case_sql("p_throws='L' AND ")} THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='L' AND {SQL_IN_PLAY} THEN 1 ELSE 0 END), 0) AS barrel_rate_vs_l,

                    -- Platoon splits — vs RHP
                    SUM(CASE WHEN p_throws='R' AND zone NOT BETWEEN 1 AND 9 AND type NOT IN ('B', 'C', 'H', 'P', '*B')
                        THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN p_throws='R' AND zone NOT BETWEEN 1 AND 9 THEN 1
                        ELSE 0 END), 0) AS o_swing_rate_vs_r,
                    -- SIM-522: this leg kept the inverted `type IN (...)` predicate after
                    -- SIM-501 fixed the season-level z_swing_rate, so it held the z-TAKE
                    -- rate (live 2024: 0.32 vs 0.68). Same fix: a swing is NOT a non-swing.
                    SUM(CASE WHEN p_throws='R' AND zone BETWEEN 1 AND 9 AND type NOT IN ('B', 'C', 'H', 'P', '*B')
                        THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN p_throws='R' AND zone BETWEEN 1 AND 9 THEN 1
                        ELSE 0 END), 0) AS z_swing_rate_vs_r,
                    SUM(CASE WHEN p_throws='R' AND {SQL_WHIFF} THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE
                        WHEN p_throws='R' AND type NOT IN ('B', 'C', 'H', 'P', '*B') THEN 1 ELSE 0 END), 0)
                        AS whiff_rate_vs_r,
                    SUM(CASE WHEN p_throws='R' AND type IN ('D', 'E', 'F', 'X') THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='R' AND type NOT IN ('B', 'C', 'H', 'P', '*B') THEN 1 ELSE 0 END)
                        , 0) AS contact_rate_vs_r,
                    SUM(CASE WHEN p_throws='R' AND events = 'walk' THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='R' AND events IS NOT NULL THEN 1 ELSE 0 END), 0)
                        AS walk_rate_vs_r,
                    SUM(CASE WHEN p_throws='R' AND events IN ('strikeout','strikeout_double_play') THEN 1.0 ELSE 0 END)
                        / NULLIF(SUM(CASE WHEN p_throws='R' AND events IS NOT NULL THEN 1 ELSE 0 END), 0) AS k_rate_vs_r,
                    SUM(CASE WHEN p_throws='R' AND bb_type='ground_ball' THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN
                        p_throws='R' AND {SQL_IN_PLAY} THEN 1 ELSE 0 END), 0) AS gb_rate_vs_r,
                    SUM(CASE WHEN p_throws='R' AND bb_type = 'fly_ball' THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='R' AND {SQL_IN_PLAY} THEN 1 ELSE 0 END), 0) AS fb_rate_vs_r,
                    -- SIM-503 + SIM-457: same three fixes as the _vs_l block.
                    SUM(CASE WHEN p_throws='R' AND {SQL_PULL_SPRAY} < -15 THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='R' AND {SQL_IN_PLAY} AND spray_angle IS NOT NULL
                        THEN 1 ELSE 0 END), 0) AS pull_rate_vs_r,
                    SUM(CASE WHEN p_throws='R' AND {SQL_PULL_SPRAY} > 15 THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='R' AND {SQL_IN_PLAY} AND spray_angle IS NOT NULL
                        THEN 1 ELSE 0 END), 0) AS oppo_rate_vs_r,
                    AVG(CASE WHEN p_throws='R' AND launch_speed IS NOT NULL THEN launch_speed END) AS avg_exit_velo_vs_r,
                    SUM(CASE WHEN {_barrel_case_sql("p_throws='R' AND ")} THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='R' AND {SQL_IN_PLAY} THEN 1 ELSE 0 END), 0) AS barrel_rate_vs_r

                FROM pg.raw.pitches
                WHERE data_quality_flag = FALSE
                  AND season IN ({season_list})
                  -- SIM-534: the cutoff. Every other source in this query
                  -- carries the same restriction.
                  AND game_date <= DATE '{asof_sql}'
                GROUP BY batter, season
            ),

            -- SIM-534: the PER-PITCH swing measurements, truncated at the
            -- cutoff. This is the point-in-time source and it wins wherever it
            -- has data: aggregating individual pitches up to a date is the same
            -- thing the rest of this query already does for every other batter
            -- feature. The leaderboard CTEs below are the fallback for 2023,
            -- which the per-pitch export does not cover.
            savant_swing_pitch AS (
                SELECT
                    p.batter AS swp_batter_id,
                    p.season AS swp_season,
{_sql_pitch_swing_agg()}
                FROM pg.raw.savant_pitch_tracking t
                JOIN pg.raw.pitches p
                    ON  p.game_pk       = t.game_pk
                    AND p.at_bat_number = t.at_bat_number
                    AND p.pitch_number  = t.pitch_number
                WHERE p.season IN ({season_list})
                  AND p.data_quality_flag = FALSE
                  AND t.bat_speed >= {COMPETITIVE_BAT_SPEED_MIN}
                  AND p.game_date <= DATE '{asof_sql}'
                GROUP BY 1, 2
            ),

            -- SIM-529: the physical swing boards. Both are pulled three times —
            -- every swing, then filtered to left-handed and to right-handed
            -- pitchers — so a switch hitter's two sides stay apart. The FULL
            -- OUTER JOIN keeps a batter who appears on one board but not the
            -- other; the two return almost the same 650 batters, but "almost"
            -- is not "exactly".
            --
            -- SIM-534: only rows whose data ENDS at or before the cutoff, most
            -- recent first. A 2023 full-season row is stamped 31 December 2023
            -- and so is admissible for any cutoff after that; a 2025
            -- full-season row is stamped 31 December 2025 and is not admissible
            -- for a mid-2025 cutoff. The arithmetic enforces the rule.
            savant_swing_raw AS (
                SELECT
                    COALESCE(bt.player_id, sp.player_id) AS player_id,
                    COALESCE(bt.season, sp.season)       AS season,
                    COALESCE(bt.split, sp.split)         AS split,
                    bt.avg_bat_speed,
                    bt.swing_length,
                    sp.swing_tilt,
                    sp.attack_angle,
                    sp.attack_direction,
                    -- SIM-534: vs BATTER, matching the per-pitch export, which
                    -- publishes only that one. See _SWING_FEATURES.
                    sp.intercept_y_vs_batter,
                    COALESCE(bt.competitive_swings, sp.competitive_swings)
                        AS competitive_swings
                FROM (
                    SELECT * FROM (
                        SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY player_id, season, split ORDER BY asof_date DESC
                        ) AS rn
                        FROM pg.raw.savant_bat_tracking
                        WHERE season IN ({season_list}) AND asof_date <= DATE '{asof_sql}'
                    ) WHERE rn = 1
                ) bt
                FULL OUTER JOIN (
                    SELECT * FROM (
                        SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY player_id, season, split ORDER BY asof_date DESC
                        ) AS rn
                        FROM pg.raw.savant_swing_path
                        WHERE season IN ({season_list}) AND asof_date <= DATE '{asof_sql}'
                    ) WHERE rn = 1
                ) sp
                    ON  sp.player_id = bt.player_id
                    AND sp.season    = bt.season
                    AND sp.split     = bt.split
            ),
            savant_swing AS (
                SELECT
                    player_id AS sw_batter_id,
                    season    AS sw_season,
{_sql_swing_pivot()}
                FROM savant_swing_raw
                GROUP BY 1, 2
            ),
            savant_stance AS (
                SELECT
                    player_id AS st_batter_id,
                    season    AS st_season,
{_sql_stance_pivot()}
                FROM (
                    SELECT * FROM (
                        SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY player_id, season, bat_side ORDER BY asof_date DESC
                        ) AS rn
                        FROM pg.raw.savant_batting_stance
                        WHERE season IN ({season_list}) AND asof_date <= DATE '{asof_sql}'
                    ) WHERE rn = 1
                )
                GROUP BY 1, 2
            )

            SELECT
                batter_id,
                season,
                sample_pitches,
                sample_pa,
                bats,
                first_pitch_take_rate,
                o_swing_rate,
                z_swing_rate,
                whiff_rate,
                contact_rate,
                walk_rate,
                k_rate,
                gb_rate,
                fb_rate,
                pull_rate,
                oppo_rate,
                avg_exit_velo,
                max_exit_velo,
                avg_launch_angle,
                hard_hit_rate,
                barrel_rate,
                hr_rate,
                -- vs LHP
                o_swing_rate_vs_l,
                z_swing_rate_vs_l,
                whiff_rate_vs_l,
                contact_rate_vs_l,
                walk_rate_vs_l,
                k_rate_vs_l,
                gb_rate_vs_l,
                fb_rate_vs_l,
                pull_rate_vs_l,
                oppo_rate_vs_l,
                avg_exit_velo_vs_l,
                barrel_rate_vs_l,
                sample_pa_vs_l,
                -- vs RHP
                o_swing_rate_vs_r,
                z_swing_rate_vs_r,
                whiff_rate_vs_r,
                contact_rate_vs_r,
                walk_rate_vs_r,
                k_rate_vs_r,
                gb_rate_vs_r,
                fb_rate_vs_r,
                pull_rate_vs_r,
                oppo_rate_vs_r,
                avg_exit_velo_vs_r,
                barrel_rate_vs_r,
                sample_pa_vs_r,
                (sample_pa < {MIN_BATTER_PA}) AS below_minimum_sample,
                CURRENT_TIMESTAMP                                        AS updated_at,

                -- SIM-529 (migration 0025): the physical swing and stance
                -- columns. ⚠ APPENDED LAST, in PHYSICAL_COLUMN_ORDER, because
                -- this INSERT carries no column list and DuckDB matches by
                -- position. The generators below and migration 0025 both read
                -- that tuple; a unit test asserts the live table agrees.
{_sql_swing_select()}
{_sql_stance_select()}
                GREATEST(
                    COALESCE(swp.competitive_swings_all, 0),
                    COALESCE(sw.competitive_swings_all, 0)
                ) AS physical_swings,
                GREATEST(
                    COALESCE(swp.competitive_swings_vs_l, 0),
                    COALESCE(sw.competitive_swings_vs_l, 0)
                ) AS physical_swings_vs_l,
                GREATEST(
                    COALESCE(swp.competitive_swings_vs_r, 0),
                    COALESCE(sw.competitive_swings_vs_r, 0)
                ) AS physical_swings_vs_r,
                -- SIM-534: the date this row's data runs through. Written on
                -- every profile so a later reader never has to guess, and so the
                -- engine can refuse a set of profiles built at different cutoffs.
                DATE '{asof_sql}' AS asof_date
            FROM batter_pitches bp
            LEFT JOIN savant_swing_pitch swp
                ON swp.swp_batter_id = bp.batter_id AND swp.swp_season = bp.season
            LEFT JOIN savant_swing sw
                ON sw.sw_batter_id = bp.batter_id AND sw.sw_season = bp.season
            LEFT JOIN savant_stance st
                ON st.st_batter_id = bp.batter_id AND st.st_season = bp.season
        """)
        # Leave the whole table at ONE cutoff. The seasons
        # this run did not touch passed _refuse_a_mixed_cutoff above, so every
        # survivor is a whole season, and stamping it at this cutoff says the
        # true thing: this is what we know as of the cutoff.
        self._stamp_one_cutoff("derived.batter_season_metrics", asof_sql)

        self._assert_batter_profiles_have_no_leakage(seasons, asof_date)
        log.info("  Batter profiles done (as of %s).", asof_sql)

    def _assert_batter_profiles_have_no_leakage(self, seasons: list[int], asof: date) -> None:
        """Prove no source that fed the batter profiles postdates the cutoff.

        The filters are in the query above, so in principle this can only pass.
        That is exactly why it is here: the query is long, it has four sources,
        and the next person to add a fifth will not remember. A missing cutoff
        produces no error and no visibly wrong number — it produces a backtest
        that quietly knows the future. This turns that into a crash.
        """
        season_list = ", ".join(str(s) for s in seasons)
        checks = (
            (
                "raw.pitches",
                f"SELECT MAX(game_date) FROM pg.raw.pitches "
                f"WHERE season IN ({season_list}) AND game_date <= DATE '{asof}'",
            ),
            (
                "raw.savant_pitch_tracking",
                f"SELECT MAX(t.game_date) FROM pg.raw.savant_pitch_tracking t "
                f"JOIN pg.raw.pitches p ON p.game_pk = t.game_pk "
                f"AND p.at_bat_number = t.at_bat_number "
                f"AND p.pitch_number = t.pitch_number "
                f"WHERE p.season IN ({season_list}) AND p.game_date <= DATE '{asof}'",
            ),
            (
                "raw.savant_bat_tracking",
                f"SELECT MAX(asof_date) FROM pg.raw.savant_bat_tracking "
                f"WHERE season IN ({season_list}) AND asof_date <= DATE '{asof}'",
            ),
            (
                "raw.savant_swing_path",
                f"SELECT MAX(asof_date) FROM pg.raw.savant_swing_path "
                f"WHERE season IN ({season_list}) AND asof_date <= DATE '{asof}'",
            ),
            (
                "raw.savant_batting_stance",
                f"SELECT MAX(asof_date) FROM pg.raw.savant_batting_stance "
                f"WHERE season IN ({season_list}) AND asof_date <= DATE '{asof}'",
            ),
        )
        for source, sql in checks:
            newest = self._conn.execute(sql).fetchone()[0]
            if newest is None:
                log.info("    no leakage from %-28s (no rows)", source)
                continue
            if not isinstance(newest, date):
                # A stubbed connection in a unit test, not a real result. Say so
                # rather than crashing on the comparison — and rather than
                # passing silently, which would let the check rot unnoticed.
                log.warning("    leakage check SKIPPED for %s: got %r, not a date", source, newest)
                continue
            if newest > asof:
                raise RuntimeError(
                    f"data leakage: {source} contributed a row dated {newest}, "
                    f"after the {asof} cutoff."
                )
            log.info("    no leakage from %-28s newest = %s", source, newest)
