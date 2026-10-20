"""The manager season profiles and the per-game bullpen workload."""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from pipeline.batch.profiles.base import ComputorBase
from pipeline.batch.profiles.constants import MIN_MANAGER_GAMES
from pipeline.statcast_events import sql_steal_attempt

log = logging.getLogger("player_profile_computor")


class ManagerProfilesMixin(ComputorBase):
    """Builds ``derived.manager_season_metrics`` and the bullpen workload table."""

    def _compute_bullpen_workload(self, seasons: list[int]) -> pd.DataFrame:
        """
        SIM-433: derive the per-game rest / recent-workload signal that the
        availability table (``raw.game_bullpen_availability``) needs, straight
        from ``raw.pitches`` (no network).

        ``raw.game_lineups`` records who *played*; the manager-profile USAGE
        sub-score (SIM-427/434) also needs the available-but-unused signal, which
        starts with workload: an arm that threw 45 pitches yesterday is
        rest-unavailable today regardless of whether the manager "chose" not to
        use it. This method builds, per (game_pk, team_id, pitcher_id) where the
        pitcher actually appeared:

          * ``last_game_date``   — this pitcher's appearance date (the game's date)
          * ``days_rest``        — days since the pitcher's PREVIOUS appearance
                                   (NULL for the first appearance in the window)
          * ``pitches_last_3d``  — pitches thrown in the 3 calendar days BEFORE
                                   this game's date (excludes this game)
          * ``back_to_back``     — pitched on the immediately preceding calendar
                                   day

        ``team_id`` is the FIELDING team for the pitcher in each appearance: the
        home team when the half-inning is the top (``inning_topbot='Top'``), else
        the away team. (A pitcher only ever fields for one team in a game, so the
        per-(game, pitcher) MIN over the half-inning derivation is unambiguous.)

        This is the raw.pitches-derived HALF of SIM-433. It does NOT persist —
        the computor attaches PostgreSQL READ_ONLY, so it returns the workload
        rows as a DataFrame for
        :class:`pipeline.live.bullpen_availability_ingest.BullpenAvailabilityIngest`
        to merge with the MLB-API active-roster / IL fetch and persist to
        ``raw.game_bullpen_availability``. Returns an EMPTY DataFrame (with the
        documented columns) when there are no qualifying rows.
        """
        log.info("Deriving bullpen workload (SIM-433) …")
        season_list = ", ".join(str(s) for s in seasons)

        # One row per (game_pk, pitcher) appearance with that game's date, the
        # pitcher's fielding team, and pitches thrown; then a self-referential
        # window over the pitcher's appearance timeline derives rest/workload.
        df = self._conn.execute(f"""
            WITH appearances AS (
                SELECT
                    game_pk,
                    pitcher                                                  AS pitcher_id,
                    season,
                    MIN(game_date)                                           AS game_date,
                    -- Fielding team: home fields the top half, away the bottom.
                    MIN(CASE WHEN inning_topbot = 'Top' THEN home_id ELSE away_id END)
                                                                             AS team_id,
                    COUNT(*)                                                 AS pitches_in_game
                FROM pg.raw.pitches
                WHERE data_quality_flag = FALSE
                  AND season IN ({season_list})
                GROUP BY game_pk, pitcher, season
            ),
            with_prev AS (
                SELECT
                    a.*,
                    LAG(a.game_date) OVER (
                        PARTITION BY a.pitcher_id ORDER BY a.game_date, a.game_pk
                    )                                                        AS prev_game_date
                FROM appearances a
            )
            SELECT
                w.game_pk,
                w.team_id,
                w.pitcher_id,
                w.season,
                w.game_date,
                w.pitches_in_game,
                CASE WHEN w.prev_game_date IS NOT NULL
                     THEN DATE_DIFF('day', w.prev_game_date, w.game_date)
                END                                                          AS days_rest,
                CASE WHEN w.prev_game_date IS NOT NULL
                       AND DATE_DIFF('day', w.prev_game_date, w.game_date) = 1
                     THEN TRUE ELSE FALSE END                                AS back_to_back,
                -- Pitches thrown by this arm in the 3 calendar days BEFORE this
                -- game (a correlated sum over the same appearances set).
                COALESCE((
                    SELECT SUM(p.pitches_in_game)
                    FROM appearances p
                    WHERE p.pitcher_id = w.pitcher_id
                      AND p.game_date < w.game_date
                      AND p.game_date >= w.game_date - INTERVAL 3 DAY
                ), 0)                                                        AS pitches_last_3d
            FROM with_prev w
            ORDER BY w.pitcher_id, w.game_date, w.game_pk
        """).fetchdf()

        if df.empty:
            log.info("  bullpen workload: no qualifying appearances.")
            return df
        log.info(
            "  bullpen workload: %d appearance rows (%d pitchers).",
            len(df),
            df["pitcher_id"].nunique(),
        )
        return df

    def _compute_manager_profiles(self, seasons: list[int], asof: date | None = None) -> None:
        """
        SIM-408: compute manager tendency profiles in the manager engine's
        vocabulary (usage / aggression / platoon).

        SIM-537: ``asof`` is the date the profile's data runs through, same
        rule as the batter (SIM-534) and pitcher (SIM-537) groupings. Every
        source here — ``raw.pitches`` and, transitively through the ``staff``
        join, ``raw.game_bullpen_availability`` — is our own data, so a plain
        ``game_date <= cutoff`` filter is enough; no Savant fallback applies
        (design doc: docs/audit/2026-09-10-savant-point-in-time-data.md §6).
        ``raw.game_bullpen_availability`` needs no cutoff filter of its own:
        it only ever contributes through an INNER JOIN onto ``staff``, which
        already carries the cutoff, so a game outside the window is never a
        match regardless.

        Offensive decisions (pinch-hit, steal order, sac bunt, squeeze, platoon
        exploitation) are attributed to the BATTING manager; defensive ones
        (defensive sub) to the FIELDING manager.  The USAGE sub-score (SIM-427:
        starter pitch-count / quick-hook, opener + bulk usage, closer-entry
        leverage, high-leverage bullpen reliance) is derived from raw.pitches
        pitcher-STINTS attributed to the fielding manager (see the ``staff`` /
        ``game_usage`` / ``mgr_usage`` / ``hi_lev`` CTEs); two further tendencies
        are not flagged in Statcast (hit-and-run, double-switch) and stay NULL.
        Idempotent via INSERT OR REPLACE on (manager_id, season).

        SIM-427 capstone: ``available_reliever_usage_rate`` (the ``bullpen_opp`` CTE)
        normalizes bullpen usage by OPPORTUNITY — relievers used / (used + available-
        but-held) — by joining the SIM-433-v2 ``raw.game_bullpen_availability`` table
        (the full per-game roster, including arms that were available but never
        appeared). It degrades to NULL (LEFT JOIN) for a manager-season with no
        availability rows; the engine's degenerate-sigma guard then keeps the default.
        The other six USAGE metrics stand on observed raw.pitches usage alone.
        """
        asof_date = asof or date.today()
        asof_sql = asof_date.isoformat()
        log.info("Computing manager profiles (as of %s) …", asof_sql)
        season_list = ", ".join(str(s) for s in seasons)
        date_cutoff = f"AND game_date <= DATE '{asof_sql}'" if asof is not None else ""

        # SIM-551: refuse, before any write, a rebuild that would leave the
        # table at two cutoffs.
        self._refuse_a_mixed_cutoff("derived.manager_season_metrics", seasons, asof_date)

        if asof is not None:
            # SIM-537: same removal rule as the batter/pitcher groupings —
            # INSERT OR REPLACE only overwrites rows this query produces, so a
            # season that had not started at the cutoff would otherwise
            # survive a backtest rebuild from an earlier, full-season run.
            self._conn.execute(
                f"DELETE FROM derived.manager_season_metrics "
                f"WHERE season IN ({season_list}) OR season > {asof_date.year}"
            )

        self._conn.execute(f"""
            INSERT OR REPLACE INTO derived.manager_season_metrics (
                manager_id, season, sample_games, sample_starter_decisions,
                starter_avg_pitch_count, starter_pull_pct_before_100,
                closer_entry_leverage_index, high_leverage_reliever_rate,
                opener_usage_rate, bulk_innings_rate,
                available_reliever_usage_rate,
                steal_order_rate_per_1b_opp, hit_and_run_rate_per_opportunity,
                sac_bunt_rate_high_leverage, sac_bunt_rate_low_leverage,
                squeeze_play_rate_per_3b_opp,
                pinch_hit_rate_vs_same_hand, pinch_hit_rate_high_leverage,
                defensive_sub_rate_late_innings, double_switch_rate_per_reliever_change,
                platoon_advantage_exploitation_rate, below_minimum_sample, asof_date
            )
            WITH p AS (
                SELECT
                    game_pk, at_bat_number, pitch_number, season,
                    inning, inning_topbot, on_1b, on_2b, on_3b,
                    stand, p_throws, pinch_hitter, defensive_sub,
                    sb_attempt_2b, sb_attempt_3b, sb_attempt_home, events,
                    -- batting manager makes offensive calls; fielding manager defensive
                    CASE WHEN inning_topbot = 'Top' THEN away_manager_id
                         ELSE home_manager_id END AS bat_mgr,
                    CASE WHEN inning_topbot = 'Top' THEN home_manager_id
                         ELSE away_manager_id END AS fld_mgr,
                    -- simplified leverage bucket (matches the situation-engine LI drivers)
                    CASE
                        WHEN ABS(bat_score - fld_score) <= 1 AND inning >= 7 THEN 'high'
                        WHEN ABS(bat_score - fld_score) >= 4 OR inning <= 5  THEN 'low'
                        ELSE 'mid'
                    END AS li_bucket,
                    -- the PA's terminal event is on the last pitch; broadcast it
                    MAX(events) OVER (PARTITION BY game_pk, at_bat_number) AS pa_event
                FROM pg.raw.pitches
                WHERE data_quality_flag = FALSE
                  AND season IN ({season_list})
                  AND home_manager_id IS NOT NULL
                  -- SIM-537: the cutoff, same rule as the batter/pitcher queries.
                  {date_cutoff}
            ),
            -- one row per PA (first pitch) for per-PA offensive rates
            pa AS (
                SELECT * FROM p
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY game_pk, at_bat_number ORDER BY pitch_number
                ) = 1
            ),
            -- offensive (batting-manager) aggregates, per PA
            bat_agg AS (
                SELECT
                    bat_mgr AS manager_id, season,
                    COUNT(*)                                                       AS n_pa,
                    SUM(CASE WHEN on_1b IS NOT NULL THEN 1 ELSE 0 END)             AS opp_1b,
                    SUM(CASE WHEN on_3b IS NOT NULL THEN 1 ELSE 0 END)             AS opp_3b,
                    SUM(CASE WHEN li_bucket = 'high' THEN 1 ELSE 0 END)            AS n_pa_high,
                    SUM(CASE WHEN li_bucket = 'low'  THEN 1 ELSE 0 END)            AS n_pa_low,
                    SUM(CASE WHEN pinch_hitter THEN 1 ELSE 0 END)                  AS n_ph,
                    SUM(CASE WHEN pinch_hitter AND stand = p_throws THEN 1 ELSE 0 END) AS n_ph_same_hand,
                    SUM(CASE WHEN pinch_hitter AND li_bucket = 'high' THEN 1 ELSE 0 END) AS n_ph_high,
                    SUM(CASE WHEN pa_event = 'sac_bunt' AND li_bucket = 'high' THEN 1 ELSE 0 END) AS n_bunt_high,
                    SUM(CASE WHEN pa_event = 'sac_bunt' AND li_bucket = 'low'  THEN 1 ELSE 0 END) AS n_bunt_low,
                    SUM(CASE WHEN pa_event = 'sac_bunt' AND on_3b IS NOT NULL THEN 1 ELSE 0 END) AS n_squeeze,
                    SUM(CASE WHEN stand <> p_throws THEN 1 ELSE 0 END)             AS n_platoon_adv
                FROM pa
                WHERE bat_mgr IS NOT NULL
                GROUP BY bat_mgr, season
            ),
            -- steal attempts (per-pitch events, batting side).  SIM-506:
            -- attempt labels = columns OR events (a PA-ending caught stealing
            -- lives only in `events`) — this rate is the SIM-474 aggression
            -- weight's numerator.
            steal_agg AS (
                SELECT bat_mgr AS manager_id, season,
                    SUM(CASE WHEN {sql_steal_attempt("2b")} OR {sql_steal_attempt("3b")}
                              OR {sql_steal_attempt("home")}
                             THEN 1 ELSE 0 END) AS n_steal_orders
                FROM p
                WHERE bat_mgr IS NOT NULL
                GROUP BY bat_mgr, season
            ),
            -- defensive (fielding-manager) aggregates
            fld_agg AS (
                SELECT fld_mgr AS manager_id, season,
                    COUNT(DISTINCT game_pk)                                        AS n_fld_games,
                    -- SIM-441: count distinct PLATE APPEARANCES with a late
                    -- defensive sub, not pitch ROWS.
                    --
                    -- `defensive_sub` is a per-pitch column, and how many pitch
                    -- rows carry the flag is a property of the ETL parser, not of
                    -- the manager. Before SIM-440 the parser broadcast the flag to
                    -- every pitch of the plate appearance, so `SUM(CASE ...)` here
                    -- counted ~4 rows per substitution and inflated
                    -- defensive_sub_rate_late_innings accordingly — the manager
                    -- engine's heaviest feature at weight 0.550. The parser now
                    -- sets it on exactly one pitch.
                    --
                    -- Counting distinct PAs makes this expression INVARIANT to
                    -- that difference: 4 broadcast rows and 1 latched row both
                    -- yield 1. That matters because the corrective reload sweep
                    -- rewrites games one at a time, so mid-sweep the corpus holds
                    -- BOTH conventions at once — and a row-counting aggregate
                    -- would silently mix two incompatible units in one column.
                    --
                    -- Note this deliberately does NOT aggregate over the `pa` CTE
                    -- the way the pinch-hit features do. `pa` keeps only the first
                    -- pitch of each PA; a pinch hitter is by definition substituted
                    -- before the PA begins, but a DEFENSIVE sub can occur between
                    -- pitches, and under the new parser semantics its flag lands on
                    -- whichever pitch follows — which `pa` would drop.
                    COUNT(DISTINCT CASE
                        WHEN defensive_sub AND inning >= 7
                        THEN CAST(game_pk AS VARCHAR) || '-' || CAST(at_bat_number AS VARCHAR)
                    END)                                                           AS n_def_sub_late
                FROM p
                WHERE fld_mgr IS NOT NULL
                GROUP BY fld_mgr, season
            ),
            -- games per manager (either dugout)
            games AS (
                SELECT manager_id, season, COUNT(DISTINCT game_pk) AS sample_games
                FROM (
                    SELECT bat_mgr AS manager_id, season, game_pk FROM p WHERE bat_mgr IS NOT NULL
                    UNION
                    SELECT fld_mgr AS manager_id, season, game_pk FROM p WHERE fld_mgr IS NOT NULL
                )
                GROUP BY manager_id, season
            ),
            -- ============================================================
            -- SIM-427: USAGE sub-score — bullpen-management tendencies derived
            -- from raw.pitches pitcher-STINTS, attributed to the FIELDING manager
            -- (home manager fields the Top half, away the Bottom). One stint row
            -- per (game, fielding-manager, pitcher): pitch count, innings, the
            -- in-game entry order, and the leverage bucket at the FIRST pitch.
            -- The SIM-433-v2 raw.game_bullpen_availability table (full per-game
            -- roster) adds the opportunity-normalized signal in ``bullpen_opp`` below
            -- (available_reliever_usage_rate); the stint metrics here stand on actual
            -- usage and need no availability table.
            -- ============================================================
            staff AS (
                SELECT
                    game_pk, season,
                    CASE WHEN inning_topbot = 'Top' THEN home_manager_id
                         ELSE away_manager_id END                     AS manager_id,
                    -- the fielding team (joins the SIM-433 availability table)
                    MIN(CASE WHEN inning_topbot = 'Top' THEN home_id
                             ELSE away_id END)                        AS team_id,
                    pitcher,
                    COUNT(*)                                          AS n_pitches,
                    COUNT(DISTINCT inning)                            AS n_innings,
                    MIN(at_bat_number * 1000 + pitch_number)          AS entry_seq,
                    -- leverage bucket (low=0 / mid=1 / high=2) at this arm's FIRST pitch
                    arg_min(
                        CASE WHEN ABS(bat_score - fld_score) <= 1 AND inning >= 7 THEN 2
                             WHEN ABS(bat_score - fld_score) >= 4 OR inning <= 5 THEN 0
                             ELSE 1 END,
                        at_bat_number * 1000 + pitch_number
                    )                                                 AS entry_li
                FROM pg.raw.pitches
                WHERE data_quality_flag = FALSE
                  AND season IN ({season_list})
                  AND (CASE WHEN inning_topbot = 'Top' THEN home_manager_id
                            ELSE away_manager_id END) IS NOT NULL
                  -- SIM-537: the cutoff. raw.game_bullpen_availability needs no
                  -- filter of its own -- it only contributes via a join onto
                  -- this (now cutoff-filtered) staff CTE.
                  {date_cutoff}
                GROUP BY game_pk, season,
                         CASE WHEN inning_topbot = 'Top' THEN home_manager_id
                              ELSE away_manager_id END,
                         pitcher
            ),
            staff_ranked AS (
                SELECT *,
                    ROW_NUMBER() OVER (PARTITION BY game_pk, manager_id ORDER BY entry_seq) AS arm_rank,
                    MAX(entry_seq) OVER (PARTITION BY game_pk, manager_id)                  AS last_entry_seq
                FROM staff
            ),
            -- per-(game, fielding-manager) usage facts (starter = first arm)
            game_usage AS (
                SELECT game_pk, manager_id, season,
                    MAX(CASE WHEN arm_rank = 1 THEN n_pitches END)                   AS starter_pitches,
                    MAX(CASE WHEN arm_rank = 1 THEN n_innings END)                   AS starter_innings,
                    -- a non-starter throwing >= 3 innings (bulk / long-man)
                    MAX(CASE WHEN arm_rank > 1 AND n_innings >= 3 THEN 1 ELSE 0 END) AS has_bulk,
                    -- entry leverage of the LAST reliever to enter (the closing arm;
                    -- NULL on a complete game so the starter isn't miscounted)
                    MAX(CASE WHEN entry_seq = last_entry_seq AND arm_rank > 1
                             THEN entry_li END)                                      AS closer_entry_li
                FROM staff_ranked
                GROUP BY game_pk, manager_id, season
            ),
            mgr_usage AS (
                SELECT manager_id, season,
                    AVG(starter_pitches * 1.0)                                  AS starter_avg_pitch_count,
                    AVG(CASE WHEN starter_pitches < 100 THEN 1.0 ELSE 0.0 END)  AS starter_pull_pct_before_100,
                    AVG(CASE WHEN starter_innings <= 2 THEN 1.0 ELSE 0.0 END)   AS opener_usage_rate,
                    AVG(has_bulk * 1.0)                                         AS bulk_innings_rate,
                    AVG(closer_entry_li * 1.0)                                  AS closer_entry_leverage_index
                FROM game_usage
                GROUP BY manager_id, season
            ),
            -- high-leverage bullpen reliance: of the manager's high-LI fielding
            -- pitches, the fraction thrown by a non-starter (a quick-to-the-pen hook).
            hi_lev AS (
                SELECT sp.manager_id, sp.season,
                    SUM(CASE WHEN sp.li_bucket = 'high' AND sr.arm_rank > 1 THEN 1 ELSE 0 END) * 1.0
                        / NULLIF(SUM(CASE WHEN sp.li_bucket = 'high' THEN 1 ELSE 0 END), 0)
                                                                                AS high_leverage_reliever_rate
                FROM (
                    SELECT game_pk, season, pitcher,
                        CASE WHEN inning_topbot = 'Top' THEN home_manager_id
                             ELSE away_manager_id END AS manager_id,
                        CASE WHEN ABS(bat_score - fld_score) <= 1 AND inning >= 7 THEN 'high'
                             WHEN ABS(bat_score - fld_score) >= 4 OR inning <= 5 THEN 'low'
                             ELSE 'mid' END            AS li_bucket
                    FROM pg.raw.pitches
                    WHERE data_quality_flag = FALSE
                      AND season IN ({season_list})
                      AND (CASE WHEN inning_topbot = 'Top' THEN home_manager_id
                                ELSE away_manager_id END) IS NOT NULL
                ) sp
                JOIN staff_ranked sr
                  ON sr.game_pk = sp.game_pk AND sr.manager_id = sp.manager_id
                 AND sr.pitcher = sp.pitcher
                GROUP BY sp.manager_id, sp.season
            ),
            -- SIM-427 (capstone): bullpen usage normalized by OPPORTUNITY —
            -- relievers USED / (relievers used + relievers AVAILABLE-but-held), from
            -- the SIM-433-v2 full-roster table. Separates "chose to hold an arm" from
            -- "no arm could pitch". USED = appeared non-starters (arm_rank>1); HELD =
            -- available=TRUE arms that did NOT appear (anti-join to the appeared
            -- staff). Bounded [0,1] by construction (a used-but-tired arm counts in
            -- USED, never inflating the rate >1) and starter-free (the starter is
            -- arm_rank=1, in neither term). Aggregated over the season; NULL when no
            -- availability rows exist yet for a manager's games (ingested separately)
            -- → the engine's degenerate-sigma guard keeps the default.
            bullpen_opp AS (
                SELECT u.manager_id, u.season,
                    SUM(u.used_rel) * 1.0
                        / NULLIF(SUM(u.used_rel + h.held_rel), 0)
                                                                AS available_reliever_usage_rate
                FROM (
                    SELECT game_pk, manager_id, season,
                        ANY_VALUE(team_id)                       AS team_id,
                        COUNT(*) FILTER (WHERE arm_rank > 1)     AS used_rel
                    FROM staff_ranked
                    GROUP BY game_pk, manager_id, season
                ) u
                JOIN (
                    -- available-but-HELD relievers: available=TRUE arms that did not
                    -- appear in the game (anti-join to the appeared staff).
                    SELECT a.game_pk, a.team_id,
                        COUNT(*) FILTER (WHERE a.available AND s.pitcher IS NULL) AS held_rel
                    FROM pg.raw.game_bullpen_availability a
                    LEFT JOIN staff s
                      ON s.game_pk = a.game_pk AND s.pitcher = a.pitcher_id
                    GROUP BY a.game_pk, a.team_id
                ) h ON h.game_pk = u.game_pk AND h.team_id = u.team_id
                GROUP BY u.manager_id, u.season
            )
            SELECT
                g.manager_id,
                g.season,
                g.sample_games,
                g.sample_games                                                AS sample_starter_decisions,  -- proxy (~1 SP pull/game)
                -- Usage (7) — SIM-427: the first six are derived from raw.pitches
                -- pitcher-stints (fielding-manager attribution); the seventh
                -- (available_reliever_usage_rate) is opportunity-normalized via the
                -- SIM-433-v2 availability join. NULL only when a manager has no
                -- qualifying fielding games / reliever entries in the window.
                u.starter_avg_pitch_count,
                u.starter_pull_pct_before_100,
                u.closer_entry_leverage_index,
                h.high_leverage_reliever_rate,
                u.opener_usage_rate,
                u.bulk_innings_rate,
                -- SIM-427 capstone: usage normalized by availability (opportunity).
                bo.available_reliever_usage_rate,
                -- Aggression (5)
                s.n_steal_orders * 1.0 / NULLIF(b.opp_1b, 0)                  AS steal_order_rate_per_1b_opp,
                NULL::FLOAT                                                   AS hit_and_run_rate_per_opportunity,  -- not flagged in Statcast
                b.n_bunt_high * 1.0 / NULLIF(b.n_pa_high, 0)                  AS sac_bunt_rate_high_leverage,
                b.n_bunt_low  * 1.0 / NULLIF(b.n_pa_low, 0)                   AS sac_bunt_rate_low_leverage,
                b.n_squeeze   * 1.0 / NULLIF(b.opp_3b, 0)                     AS squeeze_play_rate_per_3b_opp,
                -- Platoon (5)
                b.n_ph_same_hand * 1.0 / NULLIF(b.n_ph, 0)                    AS pinch_hit_rate_vs_same_hand,
                b.n_ph_high * 1.0 / NULLIF(b.n_pa_high, 0)                    AS pinch_hit_rate_high_leverage,
                f.n_def_sub_late * 1.0 / NULLIF(f.n_fld_games, 0)            AS defensive_sub_rate_late_innings,
                NULL::FLOAT                                                   AS double_switch_rate_per_reliever_change,  -- not detectable
                b.n_platoon_adv * 1.0 / NULLIF(b.n_pa, 0)                     AS platoon_advantage_exploitation_rate,
                (g.sample_games < {MIN_MANAGER_GAMES})                        AS below_minimum_sample,
                -- SIM-537: the date this row's data runs through — see
                -- _compute_batter_profiles for why every profile carries this.
                DATE '{asof_sql}'                                             AS asof_date
            FROM games g
            LEFT JOIN bat_agg b   ON b.manager_id = g.manager_id AND b.season = g.season
            LEFT JOIN steal_agg s ON s.manager_id = g.manager_id AND s.season = g.season
            LEFT JOIN fld_agg f   ON f.manager_id = g.manager_id AND f.season = g.season
            LEFT JOIN mgr_usage u ON u.manager_id = g.manager_id AND u.season = g.season
            LEFT JOIN hi_lev h    ON h.manager_id = g.manager_id AND h.season = g.season
            LEFT JOIN bullpen_opp bo ON bo.manager_id = g.manager_id AND bo.season = g.season
        """)

        # SIM-537 / SIM-551: leave the whole table at ONE cutoff (see
        # _compute_batter_profiles for the reasoning).
        self._stamp_one_cutoff("derived.manager_season_metrics", asof_sql)

        self._assert_manager_profiles_have_no_leakage(seasons, asof_date)
        log.info("  Manager profiles done (as of %s).", asof_sql)

    def _assert_manager_profiles_have_no_leakage(self, seasons: list[int], asof: date) -> None:
        """Prove no source that fed the manager profiles postdates the cutoff.

        Mirrors ``_assert_batter_profiles_have_no_leakage``. Only ``raw.pitches``
        needs checking: ``raw.game_bullpen_availability`` contributes only
        through a join onto the (already cutoff-filtered) ``staff`` CTE, so a
        row from it can never reach the output past the cutoff regardless of
        its own date.
        """
        season_list = ", ".join(str(s) for s in seasons)
        newest = self._conn.execute(
            f"SELECT MAX(game_date) FROM pg.raw.pitches "
            f"WHERE season IN ({season_list}) AND game_date <= DATE '{asof}'"
        ).fetchone()[0]
        if newest is None:
            log.info("    no leakage from raw.pitches (no rows)")
        elif not isinstance(newest, date):
            log.warning("    leakage check SKIPPED for raw.pitches: got %r, not a date", newest)
        elif newest > asof:
            raise RuntimeError(
                f"data leakage: raw.pitches contributed a row dated {newest}, "
                f"after the {asof} cutoff."
            )
        else:
            log.info("    no leakage from raw.pitches newest = %s", newest)
