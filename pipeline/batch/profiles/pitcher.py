"""The pitcher season profiles, with the arsenal mixture fit."""

from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pandas as pd

from db.introspect import duck_relation_readable
from pipeline.batch.profiles.base import ComputorBase
from pipeline.batch.profiles.constants import (
    FIP_CONSTANT,
    MIN_PITCHER_PITCHES,
)
from pipeline.batch.profiles.gmm import (
    GMM_FEATURE_NAMES,
    _chunk_tasks,
    _fit_gmm_batch,
    _flush_gmm_results,
    _resolve_gmm_workers,
)
from pipeline.batch.profiles.sql import (
    SQL_IN_PLAY,
    SQL_OUTS_RECORDED,
    SQL_SWING,
    SQL_WHIFF,
)

log = logging.getLogger("player_profile_computor")


class PitcherProfilesMixin(ComputorBase):
    """Builds ``derived.pitcher_season_metrics`` and ``derived.pitcher_gmm_components``."""

    def _compute_pitcher_profiles(self, seasons: list[int], asof: date | None = None) -> None:
        """
        Two-pass approach:
          Pass 1 (SQL): compute all command metrics and result stats in DuckDB.
          Pass 2 (Python): fit GMM for each pitcher/season, write model JSON
                           and component rows.

        ``asof`` is the date the profile's data runs through, the same rule
        as the batter profiles. Every source here (``raw.pitches`` in both
        passes and ``raw.play_events`` in the pickoff-out CTE) is our own data,
        so, unlike the batter profile's Savant boards, no prior-season fallback
        is needed: a plain ``game_date <= cutoff`` filter is enough (design
        doc: docs/audit/2026-09-10-savant-point-in-time-data.md §6).
        """
        asof_date = asof or date.today()
        asof_sql = asof_date.isoformat()
        log.info("Computing pitcher profiles (as of %s) …", asof_sql)
        season_list = ", ".join(str(s) for s in seasons)
        date_cutoff = f"AND game_date <= DATE '{asof_sql}'" if asof is not None else ""
        pickoff_outs_cte = self._play_events_outs_cte(asof_sql if asof is not None else None)

        # Refuse, before any write, a rebuild that would leave the
        # table at two cutoffs.
        self._refuse_a_mixed_cutoff("derived.pitcher_season_metrics", seasons, asof_date)

        if asof is not None:
            # The removal rule of the batter profiles: INSERT OR REPLACE
            # only overwrites the rows the query below produces, so a
            # season that had not started at the cutoff (no 2025 pitcher on
            # 15 April 2024) would otherwise survive a backtest rebuild from
            # an earlier, full-season run. Delete the CHILD table first
            # (gmm_components references pitcher_season_metrics), same order
            # the unconditional delete below already uses.
            self._conn.execute(
                f"DELETE FROM derived.pitcher_gmm_components "
                f"WHERE season IN ({season_list}) OR season > {asof_date.year}"
            )
            self._conn.execute(
                f"DELETE FROM derived.pitcher_season_metrics "
                f"WHERE season IN ({season_list}) OR season > {asof_date.year}"
            )

        # Delete child rows first so INSERT OR REPLACE on pitcher_season_metrics
        # can implicitly remove the old parent row without a FK violation.
        self._conn.execute(
            f"DELETE FROM derived.pitcher_gmm_components WHERE season IN ({season_list})"
        )

        # --- Pass 1: command + result metrics ----------------------------
        self._conn.execute(f"""
                INSERT OR REPLACE INTO derived.pitcher_season_metrics (
                    pitcher_id, season, p_throws, sample_pitches,
                    gmm_model,
                    bb_rate, k_rate, csw_rate, zone_take_rate, chase_rate, zone_rate, whiff_rate,
                    era, fip, xfip, ground_ball_rate, fly_ball_rate,
                    line_drive_rate, hr_per_9, whip,
                    below_minimum_sample, asof_date
                )
                WITH pitch_stats AS (
                    SELECT
                        pitcher                         AS pitcher_id,
                        season,
                        MAX(p_throws)                   AS p_throws,
                        COUNT(*)                        AS sample_pitches,

                        -- Command metrics
                        -- O-swing: swung at pitches outside zone / pitches outside zone
                        SUM(CASE WHEN zone NOT BETWEEN 1 AND 9 AND type IN ('D','E','F','L','M','O','S','T','W','X')
                            THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN zone NOT BETWEEN 1 AND 9 THEN 1 ELSE 0 END),0)
                            AS chase_rate,

                        -- Z-take: pitches taken inside the zone / pitches inside the zone
                        SUM(CASE WHEN zone BETWEEN 1 AND 9 AND type = 'C' THEN 1.0 ELSE 0 END) /
                            NULLIF(SUM(CASE WHEN zone BETWEEN 1 AND 9 THEN 1 ELSE 0 END),0) AS zone_take_rate,

                        -- Whiff Rate
                        -- SIM-456: this read `type = 'C'` / COUNT(*) — the CALLED-STRIKE
                        -- rate over ALL pitches. The batter never swung. So swing-and-miss,
                        -- the most separating thing a pitcher does, was absent from the
                        -- pitcher engine, while one of its seven command features measured
                        -- something else entirely.
                        SUM(CASE WHEN {SQL_WHIFF} THEN 1.0 ELSE 0 END)
                            / NULLIF(SUM(CASE WHEN {SQL_SWING} THEN 1 ELSE 0 END), 0) AS whiff_rate,

                        -- Zone rate: pitches in zone / total
                        SUM(CASE WHEN zone BETWEEN 1 AND 9 THEN 1.0 ELSE 0 END) / COUNT(*) AS zone_rate,

                        -- Called Strike + Whiff Rate
                        SUM(CASE WHEN type IN ('C','M','O','S','T','W') THEN 1.0 ELSE 0 END) / COUNT(*) AS csw_rate,

                        -- Count PA-level outcomes (use last pitch of each PA)
                        COUNT(DISTINCT CASE WHEN events IS NOT NULL THEN game_pk||'-'||at_bat_number END) AS total_pa,

                        -- SIM-504: `= 'walk'` by DECISION (uBB — see the is_bb note).
                        SUM(CASE WHEN events = 'walk' THEN 1 ELSE 0 END) AS walks,
                        SUM(CASE WHEN events IN ('strikeout','strikeout_double_play') THEN 1 ELSE 0 END) AS strikeouts,
                        SUM(CASE WHEN events = 'home_run' THEN 1 ELSE 0 END) AS home_runs,
                        SUM(CASE WHEN events IN ('single','double','triple','home_run') THEN 1 ELSE 0 END) AS hits,
                        SUM(CASE WHEN events = 'hit_by_pitch' THEN 1 ELSE 0 END) AS hbp,

                        SUM(earned_runs_on_pitch)       AS earned_runs,

                        -- SIM-501c. Question: how many outs did each play record
                        -- while this pitcher threw? This is the innings-pitched
                        -- denominator for era/fip/xfip/hr_per_9/whip. Derived
                        -- from `events` + the caught-stealing columns; the old
                        -- SUM(outs_on_pitch) missed ~36% of real outs.
                        SUM({SQL_OUTS_RECORDED})        AS outs_recorded,

                        -- Batted ball types (SIM-457). Question: of the balls
                        -- this pitcher allowed IN PLAY, what mix? The denominator
                        -- is every ball in play; the old `type='X'` kept only the
                        -- out plays, so the rates inflated ~1.5x and the error
                        -- grew with the hits a pitcher allowed.
                        SUM(CASE WHEN bb_type = 'ground_ball' THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN {SQL_IN_PLAY}
                            THEN 1 ELSE 0 END),0) AS ground_ball_rate,
                        SUM(CASE WHEN bb_type IN ('fly_ball','popup') THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN
                            {SQL_IN_PLAY} THEN 1 ELSE 0 END),0) AS fly_ball_rate,
                        SUM(CASE WHEN bb_type = 'line_drive' THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN {SQL_IN_PLAY}
                            THEN 1 ELSE 0 END),0) AS line_drive_rate,
                        SUM(CASE WHEN bb_type IN ('fly_ball','popup') THEN 1 ELSE 0 END) AS fly_balls_total

                    FROM pg.raw.pitches
                    WHERE data_quality_flag = FALSE
                      AND season IN ({season_list})
                      -- SIM-537: the cutoff, same rule as the batter query.
                      {date_cutoff}
                    GROUP BY pitcher, season
                ),
                -- SIM-504: pickoff outs recorded by this pitcher, from
                -- raw.play_events (they exist in no raw.pitches row).
                pickoff_outs AS (
                    {pickoff_outs_cte}
                )
                SELECT
                    pitch_stats.pitcher_id,
                    pitch_stats.season,
                    p_throws,
                    sample_pitches,

                    -- GMM model filled in Pass 2; placeholder until then
                    '{{"n_components": 0, "note": "pending_gmm_fit"}}'::JSON AS gmm_model,

                    walks * 1.0 / NULLIF(total_pa, 0)      AS bb_rate,
                    strikeouts * 1.0 / NULLIF(total_pa, 0) AS k_rate,
                    csw_rate,
                    zone_take_rate,
                    chase_rate,
                    zone_rate,
                    whiff_rate,

                    -- ERA: (earned_runs / outs_recorded) * 27
                    (earned_runs * 27.0) / NULLIF((outs_recorded + COALESCE(po.n_outs, 0)), 0)  AS era,

                    -- FIP = (13*HR + 3*(BB+HBP) - 2*K) / IP + FIP_CONSTANT
                    -- IP = outs_recorded / 3
                    ({FIP_CONSTANT} + (13.0 * home_runs + 3.0 * (walks + hbp) - 2.0 * strikeouts) /
                        NULLIF((outs_recorded + COALESCE(po.n_outs, 0)) / 3.0, 0)) AS fip,

                    -- xFIP: substitute expected HR (league HR/FB * actual FB) for actual HR
                    -- Uses a fixed league HR/FB rate of ~0.105 (MLB average)
                    ({FIP_CONSTANT} + (13.0 * (fly_balls_total * 0.105) + 3.0 * (walks + hbp) - 2.0 * strikeouts)
                        / NULLIF((outs_recorded + COALESCE(po.n_outs, 0)) / 3.0, 0)) AS xfip,

                    ground_ball_rate,
                    fly_ball_rate,
                    line_drive_rate,

                    -- HR/9 = home_runs / IP * 9
                    (home_runs * 9.0) / NULLIF((outs_recorded + COALESCE(po.n_outs, 0)) / 3.0, 0) AS hr_per_9,

                    -- WHIP = (walks + hits) / IP
                    ((walks + hits) * 3.0) / NULLIF((outs_recorded + COALESCE(po.n_outs, 0)), 0)   AS whip,

                    (sample_pitches < {MIN_PITCHER_PITCHES})             AS below_minimum_sample,
                    -- SIM-537: the date this row's data runs through — see
                    -- _compute_batter_profiles for why every profile carries
                    -- this and why the engine refuses a mixed set.
                    DATE '{asof_sql}'                                    AS asof_date

                FROM pitch_stats
                LEFT JOIN pickoff_outs po
                    ON po.pitcher_id = pitch_stats.pitcher_id
                   AND po.season = pitch_stats.season
            """)
        log.info("  Pitcher command metrics inserted (GMM pending).")

        # --- Pass 2: GMM fitting -----------------------------------------
        # Performance design:
        #   BEFORE: one pg.raw.pitches query per pitcher/season (~5,600 round-trips
        #           for 700 pitchers × 8 seasons), all fits sequential.
        #   AFTER:  one query per season fetches all pitch vectors at once,
        #           split by pitcher in pandas. Fitting parallelised across
        #           CPU cores with ProcessPoolExecutor.
        #
        # Fetch qualifying (pitcher_id, season) pairs
        qualifying = self._conn.execute(
            f"""
                SELECT DISTINCT pitcher_id, season
                FROM derived.pitcher_season_metrics
                WHERE season IN ({season_list})
                  AND below_minimum_sample = FALSE
                ORDER BY season, pitcher_id
                """
        ).fetchall()

        log.info("  Fitting GMMs for %d pitcher/season pairs …", len(qualifying))

        # Group by season so we issue one bulk fetch per season rather than
        # one per pitcher. A single season is ~700K rows — well within memory.
        from collections import defaultdict

        by_season: dict[int, list[int]] = defaultdict(list)
        for pitcher_id, season in qualifying:
            by_season[season].append(pitcher_id)

        # Collect all (pitcher_id, season, pitches_df) tuples for parallel fitting
        fit_tasks: list[tuple[int, int, pd.DataFrame]] = []

        for season, pitcher_ids in sorted(by_season.items()):
            id_list = ", ".join(str(p) for p in pitcher_ids)
            log.info(
                "  Fetching pitch vectors for season %s (%d pitchers) …", season, len(pitcher_ids)
            )

            season_df = self._conn.execute(
                f"""
                    SELECT
                        pitcher                     AS pitcher_id,
                        release_speed               AS velo,
                        break_vertical_induced      AS ivb,
                        break_horizontal            AS hb,
                        release_spin_rate           AS spin_rate,
                        spin_axis,
                        release_pos_x               AS release_x,
                        release_pos_z               AS release_z,
                        release_extension           AS release_ext
                    FROM pg.raw.pitches
                    WHERE pitcher IN ({id_list})
                      AND season  = {season}
                      AND data_quality_flag = FALSE
                      AND release_speed IS NOT NULL
                      -- SIM-537: the cutoff, same rule as Pass 1's pitch_stats CTE.
                      {date_cutoff}
                    """
            ).df()

            for pitcher_id in pitcher_ids:
                pitcher_df = season_df[season_df["pitcher_id"] == pitcher_id].drop(
                    columns=["pitcher_id"]
                )
                fit_tasks.append((pitcher_id, season, pitcher_df))

        # The mixtures are fitted in parallel, in chunks across the CPU cores.
        from concurrent.futures import ProcessPoolExecutor, as_completed

        # Compact float32 feature arrays (not DataFrames) keep per-task IPC small.
        array_tasks = [
            (df[GMM_FEATURE_NAMES].to_numpy(dtype=np.float32), pid, ssn)
            for pid, ssn, df in fit_tasks
        ]
        n_workers = _resolve_gmm_workers(len(array_tasks))
        chunks = _chunk_tasks(array_tasks, n_workers)
        log.info(
            "  Fitting GMMs: %d pitchers across %d workers (%d chunks) …",
            len(array_tasks),
            n_workers,
            len(chunks),
        )

        fitted: list[tuple[int, int, dict, list[dict]]] = []
        fallbacks: list[tuple[int, int]] = []

        # _fit_gmm_batch is a module-level function so it pickles cleanly.
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            future_to_chunk = {pool.submit(_fit_gmm_batch, chunk): chunk for chunk in chunks}
            for future in as_completed(future_to_chunk):
                try:
                    chunk_results = future.result()
                except Exception as exc:
                    # Whole-chunk crash: flag every pitcher in it as fallback,
                    # preserving the prior per-pitcher crash semantics.
                    log.error("GMM worker chunk crashed: %s", exc)
                    fallbacks.extend((pid, ssn) for _arr, pid, ssn in future_to_chunk[future])
                    continue
                for pitcher_id, season, model_json, component_rows in chunk_results:
                    if model_json is None:
                        fallbacks.append((pitcher_id, season))
                    else:
                        fitted.append((pitcher_id, season, model_json, component_rows))

        # Batched DuckDB writes: a few set-based statements instead of one
        # UPDATE or INSERT per pitcher.
        _flush_gmm_results(self._conn, fitted, fallbacks)

        gmm_success = len(fitted)
        gmm_fallback = len(fallbacks)

        log.info(
            "  GMM fitting complete: %d fitted, %d fell back to minimum-sample flag.",
            gmm_success,
            gmm_fallback,
        )

        # Leave the whole table at ONE cutoff, as _compute_batter_profiles does.
        self._stamp_one_cutoff("derived.pitcher_season_metrics", asof_sql)

        self._assert_pitcher_profiles_have_no_leakage(seasons, asof_date)
        log.info("  Pitcher profiles done (as of %s).", asof_sql)

    def _assert_pitcher_profiles_have_no_leakage(self, seasons: list[int], asof: date) -> None:
        """Prove no source that fed the pitcher profiles postdates the cutoff.

        Mirrors ``_assert_batter_profiles_have_no_leakage``. Two sources feed
        this grouping: ``raw.pitches`` (both the command-metric pass and the
        GMM pitch-shape pass read the same table, so one check covers both)
        and ``raw.play_events`` (the pickoff-out CTE) — wrapped the same way
        ``_play_events_outs_cte`` already handles a pre-0018 database or a
        unit-test fixture: a missing table is a schema-availability question,
        not a leakage question, and skips rather than crashes.
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
            newest = self._conn.execute(sql).fetchone()[0]
            if newest is None:
                log.info("    no leakage from %-28s (no rows)", source)
                continue
            if not isinstance(newest, date):
                # A stubbed connection in a unit test, not a real result.
                log.warning("    leakage check SKIPPED for %s: got %r, not a date", source, newest)
                continue
            if newest > asof:
                raise RuntimeError(
                    f"data leakage: {source} contributed a row dated {newest}, "
                    f"after the {asof} cutoff."
                )
            log.info("    no leakage from %-28s newest = %s", source, newest)
