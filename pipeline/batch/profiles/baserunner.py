"""The baserunner season profiles and the two steal-engine tables."""

from __future__ import annotations

import logging
from datetime import date

from db.introspect import duck_relation_readable
from pipeline.batch.profiles.base import ComputorBase
from pipeline.batch.profiles.constants import (
    MIN_RUNNER_ADV_OPPS,
    MIN_RUNNER_SB_ATTEMPTS,
)
from pipeline.batch.profiles.sql import SQL_OUTS_RECORDED
from pipeline.statcast_events import (
    sql_steal_attempt,
    sql_steal_success,
)

log = logging.getLogger("player_profile_computor")


class BaserunnerProfilesMixin(ComputorBase):
    """Builds ``derived.baserunner_season_metrics``,
    ``derived.baserunner_steal_metrics`` and ``derived.pitcher_steal_metrics``.
    """

    def _compute_baserunner_profiles(self, seasons: list[int], asof: date | None = None) -> None:
        """
        Computes extra-base advancement rates (attempt/success split) and
        stolen base rates from post-play runner columns, runner-out-advancing
        flags, and SB columns in raw.pitches.

        Attempt = runner tried to advance (succeeded OR thrown out).
        Success = runner reached the next base safely.
        Stop rate = 1 - attempt_rate (held up on advancement opportunity).

        Sprint speed comes from a join to raw.sprint_speed (Baseball
        Savant), a season-level leaderboard with no per-row date of its
        own — only a season number and an unrelated load timestamp.

        The four trailing columns come from Savant's baserunning
        board (raw.savant_baserunning, the RUNNER's own side): his extra-base
        chances, his attempt rate, the rate a typical runner would have
        attempted in the same chances, and the difference — the one feature
        the advancement model reads (it repeats year to year at 0.75-0.77
        against 0.50-0.59 for the raw rate). The join follows the sprint
        speed season shift. The INSERT is positional, so these four stay
        LAST, in ``XB_COLUMN_ORDER``; NULL means no Savant row.

        ``asof`` keeps raw.pitches to ``game_date <= asof``, the rule of the
        batter profiles. The sprint speed join cannot follow that rule (its
        table has no date to filter on), so it follows the design doc's
        alternative
        (docs/audit/2026-09-10-savant-point-in-time-data.md §5): a season
        that ENDED before the cutoff joins its own row, unchanged; the
        season CONTAINING the cutoff joins the PRIOR season's row instead,
        since that season's own row is still accumulating and could leak.
        Measured year-to-year correlation for sprint speed is 0.910 — the
        substitute costs almost nothing.
        """
        log.info("Computing baserunner profiles …")
        season_list = ", ".join(str(s) for s in seasons)
        asof_date = asof or date.today()
        asof_sql = asof_date.isoformat()
        date_cutoff = f"AND game_date <= DATE '{asof_sql}'" if asof is not None else ""
        # The season shift described above; a live build (asof None) joins
        # `ap.season` itself.
        sprint_speed_season = (
            f"(CASE WHEN ap.season = {asof_date.year} THEN ap.season - 1 ELSE ap.season END)"
            if asof is not None
            else "ap.season"
        )

        # Refuse, before any write, a rebuild that would leave the
        # table at two cutoffs.
        self._refuse_a_mixed_cutoff("derived.baserunner_season_metrics", seasons, asof_date)

        if asof is not None:
            self._conn.execute(f"""
                DELETE FROM derived.baserunner_season_metrics
                WHERE season IN ({season_list}) OR season > {asof_date.year}
            """)

        self._conn.execute(f"""
            INSERT OR REPLACE INTO derived.baserunner_season_metrics

            WITH runner_events AS (
                SELECT
                    season,
                    -- Runners on each base pre-pitch
                    on_1b, on_2b, on_3b,
                    -- Post-pitch runner positions
                    post_on_1b, post_on_2b, post_on_3b,
                    -- Runner scoring flags
                    runner_1b_scored, runner_2b_scored, runner_3b_scored,
                    -- Runner thrown out advancing
                    runner_1b_out_advancing, runner_2b_out_advancing, runner_3b_out_advancing,
                    -- Hit type for advancement context
                    events, bb_type,
                    -- SB data
                    sb_attempt_2b, sb_attempt_3b, sb_attempt_home,
                    sb_success_2b, sb_success_3b, sb_success_home
                FROM pg.raw.pitches
                WHERE data_quality_flag = FALSE
                  AND season IN ({season_list})
                  {date_cutoff}
                  AND (
                      on_1b IS NOT NULL OR on_2b IS NOT NULL OR on_3b IS NOT NULL
                      OR sb_attempt_2b = TRUE OR sb_attempt_3b = TRUE OR sb_attempt_home = TRUE
                  )
            ),
            -- First-to-third on singles (runner on 1B, no one on 2B)
            first_to_third AS (
                SELECT season, on_1b AS player_id,
                    COUNT(*) AS opps,
                    SUM(CASE WHEN runner_1b_scored = TRUE
                             OR post_on_3b = on_1b
                             OR runner_1b_out_advancing = TRUE THEN 1 ELSE 0 END) AS attempts,
                    SUM(CASE WHEN runner_1b_scored = TRUE
                             OR post_on_3b = on_1b THEN 1 ELSE 0 END) AS success
                FROM runner_events
                WHERE events = 'single' AND on_1b IS NOT NULL AND on_2b IS NULL
                GROUP BY season, on_1b
            ),
            -- Second-to-home on singles
            second_to_home AS (
                SELECT season, on_2b AS player_id,
                    COUNT(*) AS opps,
                    SUM(CASE WHEN runner_2b_scored = TRUE
                             OR runner_2b_out_advancing = TRUE THEN 1 ELSE 0 END) AS attempts,
                    SUM(CASE WHEN runner_2b_scored = TRUE THEN 1 ELSE 0 END) AS success
                FROM runner_events
                WHERE events = 'single' AND on_2b IS NOT NULL
                GROUP BY season, on_2b
            ),
            -- First-to-home on doubles
            first_to_home AS (
                SELECT season, on_1b AS player_id,
                    COUNT(*) AS opps,
                    SUM(CASE WHEN runner_1b_scored = TRUE
                             OR runner_1b_out_advancing = TRUE THEN 1 ELSE 0 END) AS attempts,
                    SUM(CASE WHEN runner_1b_scored = TRUE THEN 1 ELSE 0 END) AS success
                FROM runner_events
                WHERE events = 'double' AND on_1b IS NOT NULL
                GROUP BY season, on_1b
            ),
            -- Tag-up on fly balls (third-to-home)
            tag_up AS (
                SELECT season, on_3b AS player_id,
                    COUNT(*) AS opps,
                    SUM(CASE WHEN runner_3b_scored = TRUE
                             OR runner_3b_out_advancing = TRUE THEN 1 ELSE 0 END) AS attempts,
                    SUM(CASE WHEN runner_3b_scored = TRUE THEN 1 ELSE 0 END) AS success
                FROM runner_events
                WHERE bb_type IN ('fly_ball','popup') AND on_3b IS NOT NULL
                GROUP BY season, on_3b
            ),
            -- Overall extra-base (aggregate all advancement opportunities)
            extra_base AS (
                SELECT season, player_id,
                    SUM(opps) AS total_opps,
                    SUM(attempts) AS total_attempts,
                    SUM(success) AS total_success
                FROM (
                    SELECT season, player_id, opps, attempts, success FROM first_to_third
                    UNION ALL
                    SELECT season, player_id, opps, attempts, success FROM second_to_home
                    UNION ALL
                    SELECT season, player_id, opps, attempts, success FROM first_to_home
                    UNION ALL
                    SELECT season, player_id, opps, attempts, success FROM tag_up
                ) combined
                GROUP BY season, player_id
            ),
            -- Stolen base stats.  SIM-506: attempt labels = columns OR events
            -- (a PA-ending caught stealing lives only in `events`; reading the
            -- columns alone inflated every runner's success rate ~5-7 points).
            sb_stats AS (
                SELECT season,
                    -- Player attempting the steal is the runner on the lead base
                    CASE
                        WHEN {sql_steal_attempt("home")} AND on_3b IS NOT NULL THEN on_3b
                        WHEN {sql_steal_attempt("3b")}   AND on_2b IS NOT NULL THEN on_2b
                        WHEN {sql_steal_attempt("2b")}   AND on_1b IS NOT NULL THEN on_1b
                    END AS player_id,
                    CASE
                        WHEN {sql_steal_attempt("2b")}   THEN {sql_steal_success("2b")}
                        WHEN {sql_steal_attempt("3b")}   THEN {sql_steal_success("3b")}
                        WHEN {sql_steal_attempt("home")} THEN {sql_steal_success("home")}
                    END AS sb_success
                FROM runner_events
                WHERE {sql_steal_attempt("2b")} OR {sql_steal_attempt("3b")}
                   OR {sql_steal_attempt("home")}
            ),
            sb_agg AS (
                SELECT season, player_id,
                    COUNT(*) AS sb_attempts,
                    SUM(CASE WHEN sb_success THEN 1 ELSE 0 END) AS sb_successes
                FROM sb_stats
                WHERE player_id IS NOT NULL
                GROUP BY season, player_id
            ),
            all_players AS (
                SELECT DISTINCT season, player_id FROM extra_base
                UNION
                SELECT DISTINCT season, player_id FROM sb_agg
            )
            SELECT
                ap.player_id,
                ap.season,
                -- Sprint speed proxy: scale from extra-base aggression
                -- (higher attempt rate + success rate → faster implied sprint speed)
                -- Bounded to reasonable ft/s range: 24-31 ft/s
                ss.sprint_speed                                   AS sprint_speed,

                -- Overall extra-base attempt / success
                eb.total_attempts * 1.0 / NULLIF(eb.total_opps, 0)
                                                                AS extra_base_attempt_rate,
                eb.total_success * 1.0 / NULLIF(eb.total_attempts, 0)
                                                                AS extra_base_success_rate,

                -- Per-situation attempt / success rates
                ftt.attempts * 1.0 / NULLIF(ftt.opps, 0)       AS first_to_third_attempt_rate,
                ftt.success  * 1.0 / NULLIF(ftt.attempts, 0)   AS first_to_third_success_rate,

                sth.attempts * 1.0 / NULLIF(sth.opps, 0)       AS second_to_home_attempt_rate,
                sth.success  * 1.0 / NULLIF(sth.attempts, 0)   AS second_to_home_success_rate,

                fth.attempts * 1.0 / NULLIF(fth.opps, 0)       AS first_to_home_attempt_rate,
                fth.success  * 1.0 / NULLIF(fth.attempts, 0)   AS first_to_home_success_rate,

                tu.attempts  * 1.0 / NULLIF(tu.opps, 0)        AS tag_up_attempt_rate,
                tu.success   * 1.0 / NULLIF(tu.attempts, 0)    AS tag_up_success_rate,

                -- Stop rate = 1 - overall attempt rate
                1.0 - COALESCE(eb.total_attempts * 1.0 / NULLIF(eb.total_opps, 0), 0)
                                                                AS stop_rate,

                -- SB rates
                sba.sb_attempts * 1.0 / NULLIF(eb.total_opps, 0)    AS sb_attempt_rate,
                sba.sb_successes * 1.0 / NULLIF(sba.sb_attempts, 0) AS sb_success_rate,
                (sba.sb_attempts - sba.sb_successes) * 1.0
                    / NULLIF(sba.sb_attempts, 0)                AS cs_rate,

                -- Per-situation sample sizes
                COALESCE(eb.total_opps, 0)                      AS sample_advancement_opps,
                COALESCE(ftt.opps, 0)                           AS sample_first_to_third_opps,
                COALESCE(sth.opps, 0)                           AS sample_second_to_home_opps,
                COALESCE(fth.opps, 0)                           AS sample_first_to_home_opps,
                COALESCE(tu.opps, 0)                            AS sample_tag_up_opps,
                COALESCE(sba.sb_attempts, 0)                    AS sample_sb_attempts,

                (COALESCE(eb.total_opps, 0) < {MIN_RUNNER_ADV_OPPS}
                 OR COALESCE(sba.sb_attempts, 0) < {MIN_RUNNER_SB_ATTEMPTS})
                                                                AS below_minimum_sample,
                CURRENT_TIMESTAMP                               AS updated_at,
                DATE '{asof_sql}'                               AS asof_date,

                -- SIM-531 (migration 0029): Savant's baserunning board, the
                -- runner's own extra-base chances. POSITIONAL: these four are
                -- the table's last columns, in XB_COLUMN_ORDER. NULL = no row.
                sbr.n_opp_xb                                    AS xb_opportunities,
                sbr.rate_att_xb                                 AS xb_attempt_rate,
                sbr.est_rate_att_generic_runner                 AS xb_expected_attempt_rate,
                sbr.rate_att_xb - sbr.est_rate_att_generic_runner
                                                                AS xb_attempt_rate_above_expected
            FROM all_players ap
            LEFT JOIN extra_base eb ON ap.player_id = eb.player_id AND ap.season = eb.season
            LEFT JOIN first_to_third ftt ON ap.player_id = ftt.player_id AND ap.season = ftt.season
            LEFT JOIN second_to_home sth ON ap.player_id = sth.player_id AND ap.season = sth.season
            LEFT JOIN first_to_home fth  ON ap.player_id = fth.player_id AND ap.season = fth.season
            LEFT JOIN tag_up tu          ON ap.player_id = tu.player_id  AND ap.season = tu.season
            LEFT JOIN sb_agg sba         ON ap.player_id = sba.player_id AND ap.season = sba.season
            LEFT JOIN pg.raw.sprint_speed ss
                ON ap.player_id = ss.player_id AND ss.season = {sprint_speed_season}
            LEFT JOIN pg.raw.savant_baserunning sbr
                ON ap.player_id = sbr.player_id AND sbr.season = {sprint_speed_season}
        """)
        self._stamp_one_cutoff("derived.baserunner_season_metrics", asof_sql)
        self._assert_baserunner_profiles_have_no_leakage(seasons, asof_date)
        log.info("  Baserunner profiles done.")

    def _assert_baserunner_profiles_have_no_leakage(self, seasons: list[int], asof: date) -> None:
        """Prove raw.pitches contributed no row dated after the cutoff.

        Mirrors ``_assert_pitcher_profiles_have_no_leakage``. The sprint
        speed join has no date of its own to check — it is made safe by the
        season-shift substitution in the query itself, not by filtering, so
        there is nothing to assert here about that source.
        """
        season_list = ", ".join(str(s) for s in seasons)
        self._check_leakage(
            "raw.pitches",
            f"SELECT MAX(game_date) FROM pg.raw.pitches "
            f"WHERE season IN ({season_list}) AND game_date <= DATE '{asof}'",
            asof,
        )

    def _build_baserunner_steal_metrics(self, seasons: list[int], asof: date | None = None) -> None:
        """
        Build derived.baserunner_steal_metrics, the table the baserunner-steal
        similarity engine reads.

        Per runner-season, from raw.pitches SB flags:
          * sample_steal_attempts  — SB attempts as the lead runner (2B/3B/home)
          * sample_first_base_opps — PAs begun on 1B (the steal-2B opportunity)
          * steal_attempt_rate     — attempts / first_base_opps
          * steal_attempt_rate_2b  — 2B→3B attempts / PAs begun on 2B
          * steal_success_rate / _2b — success fractions

        Biomech jump features (reaction_time / burst_distance / break_angle) are
        NOT computed: Statcast does not publish them. The lead in feet comes
        from Savant's Basestealing Run Value board
        (raw.savant_basestealing): the primary lead before the pitch, the
        secondary lead after the pitcher's first move, and the jump (their
        difference), plus ``n_init`` — the pitches the lead was measured over.
        The steal-runner model's Lead sub-score reads them. Idempotent via
        INSERT OR REPLACE on (player_id, season).

        The DRIVER holds a row for every runner who HAD a chance (a plate
        appearance begun on first or second), not only the runners who went
        (the lead distance build, SIM-531). A runner who never attempted a
        steal has attempt rate 0.0 and a NULL success rate (unmeasured), and
        with a lead he can be compared. The model's confidence basis is his
        chances for the same reason: ``sample_first_base_opps`` plus
        ``sample_second_base_opps``, never below his attempts, so no row the
        driver admits reads confidence 0 (a runner whose chances were all on
        second, or whose only attempts were of third or home).

        ``asof`` keeps raw.pitches, the only DATED source here, to
        ``game_date <= asof``. The Savant board carries no date, so it follows
        the season-shift rule of the sprint speed: the season containing the
        cutoff joins the PRIOR season's row.
        """
        log.info("Building derived.baserunner_steal_metrics …")
        season_list = ", ".join(str(s) for s in seasons)
        asof_date = asof or date.today()
        asof_sql = asof_date.isoformat()
        date_cutoff = f"AND game_date <= DATE '{asof_sql}'" if asof is not None else ""
        # The Savant season shift (no shift on a live build).
        savant_season = (
            f"(CASE WHEN r.season = {asof_date.year} THEN r.season - 1 ELSE r.season END)"
            if asof is not None
            else "r.season"
        )
        # Matches the engine's MIN_STEAL_ATTEMPTS gate (rows flagged below this
        # are filtered out by the engine's `WHERE NOT below_minimum_sample`).
        min_attempts = 10

        # Refuse, before any write, a rebuild that would leave the
        # table at two cutoffs.
        self._refuse_a_mixed_cutoff("derived.baserunner_steal_metrics", seasons, asof_date)

        if asof is not None:
            self._conn.execute(f"""
                DELETE FROM derived.baserunner_steal_metrics
                WHERE season IN ({season_list}) OR season > {asof_date.year}
            """)

        self._conn.execute(f"""
            INSERT OR REPLACE INTO derived.baserunner_steal_metrics (
                player_id, season, sample_steal_attempts, sample_first_base_opps,
                steal_attempt_rate, steal_attempt_rate_2b,
                steal_success_rate, steal_success_rate_2b, below_minimum_sample,
                asof_date,
                lead_primary_ft, lead_secondary_ft, lead_jump_ft, savant_steal_opps,
                sample_second_base_opps
            )
            WITH clean AS (
                SELECT
                    game_pk, at_bat_number, pitch_number, season,
                    on_1b, on_2b, on_3b,
                    -- SIM-506: attempt labels = columns OR events (a PA-ending
                    -- caught stealing lives only in `events`).
                    {sql_steal_attempt("2b")}   AS att_2b,
                    {sql_steal_attempt("3b")}   AS att_3b,
                    {sql_steal_attempt("home")} AS att_home,
                    {sql_steal_success("2b")}   AS suc_2b,
                    {sql_steal_success("3b")}   AS suc_3b,
                    {sql_steal_success("home")} AS suc_home
                FROM pg.raw.pitches
                WHERE data_quality_flag = FALSE
                  AND season IN ({season_list})
                  {date_cutoff}
            ),
            -- One SB-attempt event per qualifying pitch, attributed to the lead runner
            attempts AS (
                SELECT
                    CASE
                        WHEN att_2b   THEN on_1b
                        WHEN att_3b   THEN on_2b
                        WHEN att_home THEN on_3b
                    END                                       AS runner_id,
                    season,
                    att_3b                                    AS is_2b_steal,  -- runner from 2B → 3B
                    CASE
                        WHEN att_2b   THEN suc_2b
                        WHEN att_3b   THEN suc_3b
                        WHEN att_home THEN suc_home
                    END                                       AS success
                FROM clean
                WHERE att_2b OR att_3b OR att_home
            ),
            attempt_agg AS (
                SELECT
                    runner_id AS player_id,
                    season,
                    COUNT(*)                                        AS n_attempts,
                    SUM(CASE WHEN success THEN 1 ELSE 0 END)        AS n_success,
                    SUM(CASE WHEN is_2b_steal THEN 1 ELSE 0 END)    AS n_attempts_2b,
                    SUM(CASE WHEN is_2b_steal AND success THEN 1 ELSE 0 END) AS n_success_2b
                FROM attempts
                WHERE runner_id IS NOT NULL
                GROUP BY runner_id, season
            ),
            -- Opportunities = distinct PAs begun on the base (first pitch of each PA)
            pa_state AS (
                SELECT game_pk, at_bat_number, season, on_1b, on_2b
                FROM clean
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY game_pk, at_bat_number ORDER BY pitch_number
                ) = 1
            ),
            opp_1b AS (
                SELECT on_1b AS player_id, season, COUNT(*) AS opps_1b
                FROM pa_state WHERE on_1b IS NOT NULL GROUP BY on_1b, season
            ),
            opp_2b AS (
                SELECT on_2b AS player_id, season, COUNT(*) AS opps_2b
                FROM pa_state WHERE on_2b IS NOT NULL GROUP BY on_2b, season
            ),
            -- SIM-531: the driver is every runner who HAD a chance, not only
            -- the runners who went.
            runners AS (
                SELECT player_id, season FROM attempt_agg
                UNION
                SELECT player_id, season FROM opp_1b
                UNION
                SELECT player_id, season FROM opp_2b
            ),
            savant AS (
                SELECT player_id, season, r_primary_lead, r_secondary_lead,
                       r_sec_minus_prim_lead, n_init
                FROM pg.raw.savant_basestealing
            )
            SELECT
                r.player_id,
                r.season,
                COALESCE(a.n_attempts, 0)                             AS sample_steal_attempts,
                COALESCE(o1.opps_1b, 0)                               AS sample_first_base_opps,
                -- 0.0 when he never went (a measured rate over his chances).
                COALESCE(a.n_attempts, 0)    * 1.0 / NULLIF(o1.opps_1b, 0)
                                                                      AS steal_attempt_rate,
                COALESCE(a.n_attempts_2b, 0) * 1.0 / NULLIF(o2.opps_2b, 0)
                                                                      AS steal_attempt_rate_2b,
                -- NULL when he never went (unmeasured, never 0.0).
                a.n_success     * 1.0 / NULLIF(a.n_attempts, 0)       AS steal_success_rate,
                a.n_success_2b  * 1.0 / NULLIF(a.n_attempts_2b, 0)    AS steal_success_rate_2b,
                (COALESCE(a.n_attempts, 0) < {min_attempts})          AS below_minimum_sample,
                DATE '{asof_sql}'                                     AS asof_date,
                sv.r_primary_lead                                     AS lead_primary_ft,
                sv.r_secondary_lead                                   AS lead_secondary_ft,
                sv.r_sec_minus_prim_lead                              AS lead_jump_ft,
                sv.n_init                                             AS savant_steal_opps,
                -- The model's confidence basis is his TOTAL chances (first +
                -- second, never below his attempts), so a runner whose chances
                -- were all on second never reads confidence 0.
                COALESCE(o2.opps_2b, 0)                               AS sample_second_base_opps
            FROM runners r
            LEFT JOIN attempt_agg a ON a.player_id = r.player_id AND a.season = r.season
            LEFT JOIN opp_1b o1 ON o1.player_id = r.player_id AND o1.season = r.season
            LEFT JOIN opp_2b o2 ON o2.player_id = r.player_id AND o2.season = r.season
            LEFT JOIN savant sv ON sv.player_id = r.player_id AND sv.season = {savant_season}
        """)
        self._stamp_one_cutoff("derived.baserunner_steal_metrics", asof_sql)
        self._assert_baserunner_steal_profiles_have_no_leakage(seasons, asof_date)
        log.info("  derived.baserunner_steal_metrics done.")

    def _assert_baserunner_steal_profiles_have_no_leakage(
        self, seasons: list[int], asof: date
    ) -> None:
        """Prove raw.pitches contributed no row dated after the cutoff."""
        season_list = ", ".join(str(s) for s in seasons)
        self._check_leakage(
            "raw.pitches",
            f"SELECT MAX(game_date) FROM pg.raw.pitches "
            f"WHERE season IN ({season_list}) AND game_date <= DATE '{asof}'",
            asof,
        )

    def _build_pitcher_steal_metrics(self, seasons: list[int], asof: date | None = None) -> None:
        """
        Build derived.pitcher_steal_metrics, the table the pitcher-steal
        (hold-runner) similarity engine reads.

        OUTCOME-only, per pitcher-season, from raw.pitches:
          * sample_baserunner_events       — PAs pitched with a runner on base
          * sample_steal_attempts_against  — SB attempts while this pitcher threw
          * sb_against_per_9               — SB allowed × 27 / outs recorded
          * cs_rate_forced                 — (attempts − successes) / attempts
          * steal_attempt_rate_allowed     — attempts / baserunner_events
          * pickoff_rate / stepoff_rate    — disengagements per pitch
            with a runner on 1B/2B, from raw.play_events (the thrower). The
            feed records stepoffs only from 2023 (the pitch clock), so the
            rate is a true 0 before then — a coverage zero, not a behaviour.

        The attempt labels use the shared expressions of
        pipeline/statcast_events.py (the columns OR the events: a caught
        stealing that ends the plate appearance lives only in `events`). The
        delivery (biomechanical timings) is NOT computed. The lead the pitcher
        ALLOWS, in feet, comes from Savant's Pitcher Running Game board
        (raw.savant_pitcher_running_game): the primary lead, the secondary
        lead and the jump he gives up, plus ``n_init``. The pitcher-hold
        model's Hold sub-score reads them. Idempotent via INSERT OR REPLACE on
        (pitcher_id, season).

        ``asof`` keeps both dated sources to ``game_date <= asof``:
        raw.pitches (this method's own query) and raw.play_events (through the
        two CTE helpers). The Savant
        board carries no date, so it follows the season-shift rule: the
        season containing the cutoff joins the PRIOR season's row.
        """
        log.info("Building derived.pitcher_steal_metrics …")
        season_list = ", ".join(str(s) for s in seasons)
        asof_date = asof or date.today()
        asof_sql = asof_date.isoformat()
        date_cutoff = f"AND game_date <= DATE '{asof_sql}'" if asof is not None else ""
        pickoff_outs_cte = self._play_events_outs_cte(asof_sql if asof is not None else None)
        disengagement_cte = self._play_events_disengagement_cte(
            asof_sql if asof is not None else None
        )
        # The Savant season shift (no shift on a live build).
        savant_season = (
            f"(CASE WHEN p.season = {asof_date.year} THEN p.season - 1 ELSE p.season END)"
            if asof is not None
            else "p.season"
        )
        min_events = 30  # matches the engine's MIN_BASERUNNER_EVENTS gate

        # Refuse, before any write, a rebuild that would leave the
        # table at two cutoffs.
        self._refuse_a_mixed_cutoff("derived.pitcher_steal_metrics", seasons, asof_date)

        if asof is not None:
            self._conn.execute(f"""
                DELETE FROM derived.pitcher_steal_metrics
                WHERE season IN ({season_list}) OR season > {asof_date.year}
            """)

        self._conn.execute(f"""
            INSERT OR REPLACE INTO derived.pitcher_steal_metrics (
                pitcher_id, season, throws,
                sample_baserunner_events, sample_steal_attempts_against,
                sb_against_per_9, cs_rate_forced, steal_attempt_rate_allowed,
                pickoff_rate, stepoff_rate,
                below_minimum_sample, asof_date,
                lead_allowed_primary_ft, lead_allowed_secondary_ft,
                lead_allowed_jump_ft, savant_hold_opps
            )
            WITH clean AS (
                SELECT
                    pitcher, season, p_throws,
                    game_pk, at_bat_number, pitch_number,
                    on_1b, on_2b, on_3b, events, type,
                    sb_attempt_2b, sb_attempt_3b, sb_attempt_home,
                    sb_success_2b, sb_success_3b, sb_success_home
                FROM pg.raw.pitches
                WHERE data_quality_flag = FALSE
                  AND season IN ({season_list})
                  {date_cutoff}
            ),
            -- Outs recorded (for IP) + SB attempts/successes against, per pitcher
            pitch_agg AS (
                SELECT
                    pitcher AS pitcher_id,
                    season,
                    MIN(p_throws)                                            AS throws,
                    -- SIM-501c. Question: how many outs did each play record?
                    -- The innings-pitched denominator for sb_against_per_9,
                    -- derived from `events` + the caught-stealing columns.
                    SUM({SQL_OUTS_RECORDED})                                 AS outs_recorded,
                    -- SIM-506: attempt labels = columns OR events (a PA-ending
                    -- caught stealing lives only in `events`).
                    SUM(CASE WHEN {sql_steal_attempt("2b")}
                              OR {sql_steal_attempt("3b")}
                              OR {sql_steal_attempt("home")}
                             THEN 1 ELSE 0 END)                              AS sb_attempts_against,
                    SUM(CASE WHEN {sql_steal_success("2b")}
                              OR {sql_steal_success("3b")}
                              OR {sql_steal_success("home")}
                             THEN 1 ELSE 0 END)                              AS sb_allowed,
                    -- SIM-504: the disengagement denominator — pitches thrown
                    -- with a runner on 1B or 2B (someone to hold).
                    SUM(CASE WHEN on_1b IS NOT NULL OR on_2b IS NOT NULL
                             THEN 1 ELSE 0 END)                              AS runner_on_pitches
                FROM clean
                GROUP BY pitcher, season
            ),
            -- Baserunner events = distinct PAs pitched with a runner on base
            br_events AS (
                SELECT pitcher AS pitcher_id, season, COUNT(*) AS n_br_events
                FROM (
                    SELECT
                        pitcher, season,
                        (on_1b IS NOT NULL OR on_2b IS NOT NULL OR on_3b IS NOT NULL) AS has_runner
                    FROM clean
                    QUALIFY ROW_NUMBER() OVER (
                        PARTITION BY game_pk, at_bat_number ORDER BY pitch_number
                    ) = 1
                ) pa
                WHERE has_runner
                GROUP BY pitcher, season
            ),
            -- SIM-504: pickoff outs from raw.play_events count toward the
            -- innings-pitched denominator of sb_against_per_9.
            pickoff_outs AS (
                {pickoff_outs_cte}
            ),
            -- SIM-504 item 3: pickoff throws + stepoffs per (thrower, season)
            -- from raw.play_events — the hold-runner behaviour the SIM-474
            -- steal draw kernels on.
            disengagements AS (
                {disengagement_cte}
            )
            SELECT
                p.pitcher_id,
                p.season,
                p.throws,
                COALESCE(b.n_br_events, 0)                                   AS sample_baserunner_events,
                p.sb_attempts_against                                        AS sample_steal_attempts_against,
                p.sb_allowed * 27.0
                    / NULLIF(p.outs_recorded + COALESCE(po.n_outs, 0), 0)        AS sb_against_per_9,
                (p.sb_attempts_against - p.sb_allowed) * 1.0
                    / NULLIF(p.sb_attempts_against, 0)                       AS cs_rate_forced,
                p.sb_attempts_against * 1.0 / NULLIF(b.n_br_events, 0)       AS steal_attempt_rate_allowed,
                -- COALESCE to 0.0, never NULL: a NaN feature poisons the whole
                -- steal-draw weight vector (exp of a NaN distance).
                COALESCE(dg.n_pickoffs * 1.0
                    / NULLIF(p.runner_on_pitches, 0), 0.0)                   AS pickoff_rate,
                COALESCE(dg.n_stepoffs * 1.0
                    / NULLIF(p.runner_on_pitches, 0), 0.0)                   AS stepoff_rate,
                (COALESCE(b.n_br_events, 0) < {min_events})                  AS below_minimum_sample,
                DATE '{asof_sql}'                                            AS asof_date,
                -- SIM-531: the lead he allows (NULL = no Savant row).
                sv.r_primary_lead                                            AS lead_allowed_primary_ft,
                sv.r_secondary_lead                                          AS lead_allowed_secondary_ft,
                sv.r_sec_minus_prim_lead                                     AS lead_allowed_jump_ft,
                sv.n_init                                                    AS savant_hold_opps
            FROM pitch_agg p
            LEFT JOIN br_events b ON b.pitcher_id = p.pitcher_id AND b.season = p.season
            LEFT JOIN pickoff_outs po ON po.pitcher_id = p.pitcher_id AND po.season = p.season
            LEFT JOIN disengagements dg ON dg.pitcher_id = p.pitcher_id AND dg.season = p.season
            LEFT JOIN pg.raw.savant_pitcher_running_game sv
                ON sv.player_id = p.pitcher_id AND sv.season = {savant_season}
        """)
        self._stamp_one_cutoff("derived.pitcher_steal_metrics", asof_sql)
        self._assert_pitcher_steal_profiles_have_no_leakage(seasons, asof_date)
        log.info("  derived.pitcher_steal_metrics done.")

    def _assert_pitcher_steal_profiles_have_no_leakage(
        self, seasons: list[int], asof: date
    ) -> None:
        """Prove no source that fed this grouping postdates the cutoff.

        Mirrors ``_assert_pitcher_profiles_have_no_leakage``: raw.pitches
        (this method's own query) and raw.play_events (the two CTE
        helpers), the latter skipped when the table is absent.
        """
        season_list = ", ".join(str(s) for s in seasons)
        checks: list[tuple[str, str]] = [
            (
                "raw.pitches",
                f"SELECT MAX(game_date) FROM pg.raw.pitches "
                f"WHERE season IN ({season_list}) AND game_date <= DATE '{asof}'",
            ),
        ]
        if not duck_relation_readable(self._conn, "pg.raw.play_events"):
            log.info("    no leakage check for raw.play_events (table absent)")
        else:
            checks.append(
                (
                    "raw.play_events",
                    f"SELECT MAX(game_date) FROM pg.raw.play_events "
                    f"WHERE season IN ({season_list}) AND game_date <= DATE '{asof}'",
                )
            )
        for source, sql in checks:
            self._check_leakage(source, sql, asof)
