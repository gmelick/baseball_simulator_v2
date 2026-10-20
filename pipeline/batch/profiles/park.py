"""The park factors, regressed toward league average."""

from __future__ import annotations

import logging

from pipeline.batch.profiles.base import ComputorBase
from pipeline.batch.profiles.constants import PARK_PRIOR_PA

log = logging.getLogger("player_profile_computor")


class ParkFactorsMixin(ComputorBase):
    """Builds ``derived.park_factors``."""

    def _compute_park_factors(self, seasons: list[int]) -> None:
        """
        Computes park factors per venue per season per event type
        (HR, 1B, 2B, 3B, R, K, BB, GB, FB).

        Method: compare the rate of each event at a given park to the
        league-average rate.  Bayesian shrinkage toward 1.0 based on
        sample_pa (PARK_PRIOR_PA controls the shrinkage rate).

        Handedness splits (SIM-336)
        ---------------------------
        ``factor_vs_l`` / ``factor_vs_r`` are split on the **batter's**
        handedness (``stand``), the standard sabermetric convention — short
        porches and outfield dimensions interact with the batter's pull side,
        and ``stand`` is the same key the pitch/outcome read path pre-filters
        on (SIM-337 indexes, SIM-345 contract).

        SIM-440 correction: this docstring previously said switch hitters land
        in ``stand='S'`` and contribute only to ``factor_overall``. That is
        backwards — ``stand`` is the per-PA resolved side and is 'S' on **zero**
        rows; ``bat_hand`` is the roster-declared side and is 'S' for every
        switch hitter (see db/schemas/01_postgres_schema.sql for the canonical
        definition). So every PA, switch hitters included, already lands in a
        real L or R bucket here and the 'S' carve-out never applied.
        Each L/R split is the venue's vs-hand rate over its own vs-hand league
        average, so it is independently centered on 1.0.

        Pool-neutralization policy (SIM-336)
        ------------------------------------
        The sim pools (``sim.pitch_pool`` / ``sim.outcome_pool``) are drawn
        from *real* PAs that ALREADY occurred in some park, so their empirical
        rates are park-influenced. To apply a venue's park factor at sim time
        WITHOUT double-counting, the caller must first NEUTRALIZE each sampled
        comp by dividing out the park factor of the venue the comp was played
        in, then multiply by the target venue's factor:
            adjusted = base_rate * (target_factor / comp_origin_factor)
        Equivalently, sample from a park-neutral pool (each row pre-divided by
        its origin venue's ``regressed_factor``) and multiply by the target
        venue factor once. ``factor_overall == 1.0`` for a perfectly neutral
        park, so a league-average comp pool needs no adjustment. This policy is
        documented on ``derived.park_factors`` (schema) and enforced by the
        park-application engine, not by this builder (which only materializes
        the factors). Always use ``regressed_factor`` (Bayesian-shrunk), never
        the raw ``factor_overall``, in the sim path.
        """
        log.info("Computing park factors …")
        season_list = ", ".join(str(s) for s in seasons)

        # Build a per-venue per-season per-factor table in one DuckDB query.
        # Each PA is counted once from the pitcher's perspective (avoiding
        # double-counting home/away splits).
        self._conn.execute(f"""
            INSERT OR REPLACE INTO derived.park_factors

            WITH pa AS (
                -- One row per plate appearance (last pitch of each PA)
                SELECT
                    p.venue_id,
                    p.season,
                    p.p_throws,
                    p.stand,
                    p.events,
                    p.bb_type,
                    p.launch_speed,
                    p.at_bat_number,
                    p.game_pk,
                    -- Count each event type
                    CASE WHEN p.events = 'home_run'                              THEN 1 ELSE 0 END AS is_hr,
                    CASE WHEN p.events = 'single'                                THEN 1 ELSE 0 END AS is_1b,
                    CASE WHEN p.events = 'double'                                THEN 1 ELSE 0 END AS is_2b,
                    CASE WHEN p.events = 'triple'                                THEN 1 ELSE 0 END AS is_3b,
                    -- SIM-504 DECISION: intentional walks are EXCLUDED from every
                    -- similarity walk rate. They live only in raw.play_events (this
                    -- branch was dead — raw.pitches has never held an intent_walk
                    -- row), and they measure the SITUATION and the batter's power,
                    -- which the power features already carry; the sim issues IBBs
                    -- through the SIM-434 manager decision, not through profile
                    -- rates. `= 'walk'` states the decision; do not "fix" it by
                    -- wiring a play_events union here.
                    CASE WHEN p.events = 'walk'                                  THEN 1 ELSE 0 END AS is_bb,
                    CASE WHEN p.events IN ('strikeout','strikeout_double_play')  THEN 1 ELSE 0 END AS is_k,
                    CASE WHEN p.bb_type = 'ground_ball'                          THEN 1 ELSE 0 END AS is_gb,
                    CASE WHEN p.bb_type IN ('fly_ball','popup')                  THEN 1 ELSE 0 END AS is_fb,
                    -- Batter-handedness gates (switch hitters excluded from both splits)
                    CASE WHEN p.stand = 'L' THEN 1 ELSE 0 END                   AS is_vs_l,
                    CASE WHEN p.stand = 'R' THEN 1 ELSE 0 END                   AS is_vs_r,
                    p.runs_on_pitch
                FROM pg.raw.pitches p
                WHERE p.events IS NOT NULL
                  AND p.data_quality_flag = FALSE
                  AND p.season IN ({season_list})
            ),
            venue_rates AS (
                SELECT
                    venue_id,
                    season,
                    COUNT(*)                            AS sample_pa,
                    SUM(is_vs_l)                        AS sample_pa_l,
                    SUM(is_vs_r)                        AS sample_pa_r,
                    -- Overall numerators
                    SUM(is_hr)                          AS hr_total,
                    SUM(is_1b)                          AS singles,
                    SUM(is_2b)                          AS doubles,
                    SUM(is_3b)                          AS triples,
                    SUM(is_bb)                          AS walks,
                    SUM(is_k)                           AS strikeouts,
                    SUM(is_gb)                          AS groundballs,
                    SUM(is_fb)                          AS flyballs,
                    SUM(runs_on_pitch)                  AS runs,
                    -- vs-LHB numerators
                    SUM(is_hr * is_vs_l)                AS hr_l,
                    SUM(is_1b * is_vs_l)                AS singles_l,
                    SUM(is_2b * is_vs_l)                AS doubles_l,
                    SUM(is_3b * is_vs_l)                AS triples_l,
                    SUM(is_bb * is_vs_l)                AS walks_l,
                    SUM(is_k  * is_vs_l)                AS strikeouts_l,
                    SUM(is_gb * is_vs_l)                AS groundballs_l,
                    SUM(is_fb * is_vs_l)                AS flyballs_l,
                    SUM(runs_on_pitch * is_vs_l)        AS runs_l,
                    -- vs-RHB numerators
                    SUM(is_hr * is_vs_r)                AS hr_r,
                    SUM(is_1b * is_vs_r)                AS singles_r,
                    SUM(is_2b * is_vs_r)                AS doubles_r,
                    SUM(is_3b * is_vs_r)                AS triples_r,
                    SUM(is_bb * is_vs_r)                AS walks_r,
                    SUM(is_k  * is_vs_r)                AS strikeouts_r,
                    SUM(is_gb * is_vs_r)                AS groundballs_r,
                    SUM(is_fb * is_vs_r)                AS flyballs_r,
                    SUM(runs_on_pitch * is_vs_r)        AS runs_r
                FROM pa
                GROUP BY venue_id, season
            ),
            league_rates AS (
                SELECT
                    season,
                    SUM(hr_total) * 1.0 / NULLIF(SUM(sample_pa), 0)     AS lg_hr_rate,
                    SUM(singles)  * 1.0 / NULLIF(SUM(sample_pa), 0)     AS lg_1b_rate,
                    SUM(doubles)  * 1.0 / NULLIF(SUM(sample_pa), 0)     AS lg_2b_rate,
                    SUM(triples)  * 1.0 / NULLIF(SUM(sample_pa), 0)     AS lg_3b_rate,
                    SUM(walks)    * 1.0 / NULLIF(SUM(sample_pa), 0)     AS lg_bb_rate,
                    SUM(strikeouts) * 1.0 / NULLIF(SUM(sample_pa), 0)   AS lg_k_rate,
                    SUM(groundballs) * 1.0 / NULLIF(SUM(sample_pa), 0)  AS lg_gb_rate,
                    SUM(flyballs)    * 1.0 / NULLIF(SUM(sample_pa), 0)  AS lg_fb_rate,
                    SUM(runs)        * 1.0 / NULLIF(SUM(sample_pa), 0)  AS lg_r_rate,
                    -- vs-LHB league rates
                    SUM(hr_l) * 1.0 / NULLIF(SUM(sample_pa_l), 0)       AS lg_hr_rate_l,
                    SUM(singles_l) * 1.0 / NULLIF(SUM(sample_pa_l), 0)  AS lg_1b_rate_l,
                    SUM(doubles_l) * 1.0 / NULLIF(SUM(sample_pa_l), 0)  AS lg_2b_rate_l,
                    SUM(triples_l) * 1.0 / NULLIF(SUM(sample_pa_l), 0)  AS lg_3b_rate_l,
                    SUM(walks_l)   * 1.0 / NULLIF(SUM(sample_pa_l), 0)  AS lg_bb_rate_l,
                    SUM(strikeouts_l) * 1.0 / NULLIF(SUM(sample_pa_l), 0) AS lg_k_rate_l,
                    SUM(groundballs_l) * 1.0 / NULLIF(SUM(sample_pa_l), 0) AS lg_gb_rate_l,
                    SUM(flyballs_l)    * 1.0 / NULLIF(SUM(sample_pa_l), 0) AS lg_fb_rate_l,
                    SUM(runs_l)        * 1.0 / NULLIF(SUM(sample_pa_l), 0) AS lg_r_rate_l,
                    -- vs-RHB league rates
                    SUM(hr_r) * 1.0 / NULLIF(SUM(sample_pa_r), 0)       AS lg_hr_rate_r,
                    SUM(singles_r) * 1.0 / NULLIF(SUM(sample_pa_r), 0)  AS lg_1b_rate_r,
                    SUM(doubles_r) * 1.0 / NULLIF(SUM(sample_pa_r), 0)  AS lg_2b_rate_r,
                    SUM(triples_r) * 1.0 / NULLIF(SUM(sample_pa_r), 0)  AS lg_3b_rate_r,
                    SUM(walks_r)   * 1.0 / NULLIF(SUM(sample_pa_r), 0)  AS lg_bb_rate_r,
                    SUM(strikeouts_r) * 1.0 / NULLIF(SUM(sample_pa_r), 0) AS lg_k_rate_r,
                    SUM(groundballs_r) * 1.0 / NULLIF(SUM(sample_pa_r), 0) AS lg_gb_rate_r,
                    SUM(flyballs_r)    * 1.0 / NULLIF(SUM(sample_pa_r), 0) AS lg_fb_rate_r,
                    SUM(runs_r)        * 1.0 / NULLIF(SUM(sample_pa_r), 0) AS lg_r_rate_r
                FROM venue_rates
                GROUP BY season
            ),
            factors AS (
                SELECT
                    vr.venue_id,
                    vr.season,
                    vr.sample_pa,
                    -- Overall factor = venue rate / league rate
                    (vr.hr_total * 1.0 / NULLIF(vr.sample_pa,0)) / NULLIF(lr.lg_hr_rate, 0)  AS factor_hr,
                    (vr.singles  * 1.0 / NULLIF(vr.sample_pa,0)) / NULLIF(lr.lg_1b_rate, 0)  AS factor_1b,
                    (vr.doubles  * 1.0 / NULLIF(vr.sample_pa,0)) / NULLIF(lr.lg_2b_rate, 0)  AS factor_2b,
                    (vr.triples  * 1.0 / NULLIF(vr.sample_pa,0)) / NULLIF(lr.lg_3b_rate, 0)  AS factor_3b,
                    (vr.walks    * 1.0 / NULLIF(vr.sample_pa,0)) / NULLIF(lr.lg_bb_rate, 0)  AS factor_bb,
                    (vr.strikeouts * 1.0 / NULLIF(vr.sample_pa,0)) / NULLIF(lr.lg_k_rate, 0) AS factor_k,
                    (vr.groundballs * 1.0 / NULLIF(vr.sample_pa,0)) / NULLIF(lr.lg_gb_rate, 0) AS factor_gb,
                    (vr.flyballs * 1.0 / NULLIF(vr.sample_pa,0)) / NULLIF(lr.lg_fb_rate, 0)  AS factor_fb,
                    (vr.runs     * 1.0 / NULLIF(vr.sample_pa,0)) / NULLIF(lr.lg_r_rate, 0)   AS factor_r,
                    -- vs-LHB factor = venue vs-L rate / league vs-L rate
                    (vr.hr_l * 1.0 / NULLIF(vr.sample_pa_l,0)) / NULLIF(lr.lg_hr_rate_l, 0)  AS factor_hr_l,
                    (vr.singles_l * 1.0 / NULLIF(vr.sample_pa_l,0)) / NULLIF(lr.lg_1b_rate_l, 0) AS factor_1b_l,
                    (vr.doubles_l * 1.0 / NULLIF(vr.sample_pa_l,0)) / NULLIF(lr.lg_2b_rate_l, 0) AS factor_2b_l,
                    (vr.triples_l * 1.0 / NULLIF(vr.sample_pa_l,0)) / NULLIF(lr.lg_3b_rate_l, 0) AS factor_3b_l,
                    (vr.walks_l   * 1.0 / NULLIF(vr.sample_pa_l,0)) / NULLIF(lr.lg_bb_rate_l, 0) AS factor_bb_l,
                    (vr.strikeouts_l * 1.0 / NULLIF(vr.sample_pa_l,0)) / NULLIF(lr.lg_k_rate_l, 0) AS factor_k_l,
                    (vr.groundballs_l * 1.0 / NULLIF(vr.sample_pa_l,0)) / NULLIF(lr.lg_gb_rate_l, 0) AS factor_gb_l,
                    (vr.flyballs_l * 1.0 / NULLIF(vr.sample_pa_l,0)) / NULLIF(lr.lg_fb_rate_l, 0) AS factor_fb_l,
                    (vr.runs_l     * 1.0 / NULLIF(vr.sample_pa_l,0)) / NULLIF(lr.lg_r_rate_l, 0)  AS factor_r_l,
                    -- vs-RHB factor = venue vs-R rate / league vs-R rate
                    (vr.hr_r * 1.0 / NULLIF(vr.sample_pa_r,0)) / NULLIF(lr.lg_hr_rate_r, 0)  AS factor_hr_r,
                    (vr.singles_r * 1.0 / NULLIF(vr.sample_pa_r,0)) / NULLIF(lr.lg_1b_rate_r, 0) AS factor_1b_r,
                    (vr.doubles_r * 1.0 / NULLIF(vr.sample_pa_r,0)) / NULLIF(lr.lg_2b_rate_r, 0) AS factor_2b_r,
                    (vr.triples_r * 1.0 / NULLIF(vr.sample_pa_r,0)) / NULLIF(lr.lg_3b_rate_r, 0) AS factor_3b_r,
                    (vr.walks_r   * 1.0 / NULLIF(vr.sample_pa_r,0)) / NULLIF(lr.lg_bb_rate_r, 0) AS factor_bb_r,
                    (vr.strikeouts_r * 1.0 / NULLIF(vr.sample_pa_r,0)) / NULLIF(lr.lg_k_rate_r, 0) AS factor_k_r,
                    (vr.groundballs_r * 1.0 / NULLIF(vr.sample_pa_r,0)) / NULLIF(lr.lg_gb_rate_r, 0) AS factor_gb_r,
                    (vr.flyballs_r * 1.0 / NULLIF(vr.sample_pa_r,0)) / NULLIF(lr.lg_fb_rate_r, 0) AS factor_fb_r,
                    (vr.runs_r     * 1.0 / NULLIF(vr.sample_pa_r,0)) / NULLIF(lr.lg_r_rate_r, 0)  AS factor_r_r
                FROM venue_rates vr
                JOIN league_rates lr ON vr.season = lr.season
            ),
            -- Melt the overall / vs-L / vs-R factor groups together so each
            -- factor_type yields ONE row carrying overall + both splits.
            melted AS (
                SELECT
                    venue_id, season, sample_pa,
                    factor_type, factor_overall, factor_vs_l, factor_vs_r
                FROM factors
                -- INCLUDE NULLS so every factor_type always yields a row even
                -- when a venue/league rate is undefined (0 events). The outer
                -- COALESCE then makes factor_overall the neutral 1.0, honoring
                -- the NOT NULL constraint on derived.park_factors.factor_overall.
                UNPIVOT INCLUDE NULLS (
                    (factor_overall, factor_vs_l, factor_vs_r)
                    FOR factor_type IN (
                        (factor_hr, factor_hr_l, factor_hr_r) AS 'HR',
                        (factor_1b, factor_1b_l, factor_1b_r) AS '1B',
                        (factor_2b, factor_2b_l, factor_2b_r) AS '2B',
                        (factor_3b, factor_3b_l, factor_3b_r) AS '3B',
                        (factor_bb, factor_bb_l, factor_bb_r) AS 'BB',
                        (factor_k,  factor_k_l,  factor_k_r)  AS 'K',
                        (factor_gb, factor_gb_l, factor_gb_r) AS 'GB',
                        (factor_fb, factor_fb_l, factor_fb_r) AS 'FB',
                        (factor_r,  factor_r_l,  factor_r_r)  AS 'R'
                    )
                )
            )
            -- One row per (venue, season, factor_type) with real L/R splits.
            SELECT
                venue_id,
                season,
                factor_type,
                COALESCE(factor_overall, 1.0) AS factor_overall,
                factor_vs_l,
                factor_vs_r,
                sample_pa,
                -- Bayesian shrinkage: posterior = (observed * N + 1.0 * prior_weight) / (N + prior_weight)
                -- where prior_weight = PARK_PRIOR_PA. Applied to the overall factor;
                -- the L/R splits are stored raw (callers shrink with their own
                -- vs-hand sample if desired).
                (COALESCE(factor_overall, 1.0) * sample_pa + 1.0 * {PARK_PRIOR_PA})
                    / (sample_pa + {PARK_PRIOR_PA}) AS regressed_factor
            FROM melted
        """)
        log.info("  Park factors done.")
