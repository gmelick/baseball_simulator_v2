"""The catcher metrics: framing, blocking, throwing by base, the season
aggregate and the uncaught-strike-three slice of the receiving profile.
"""

from __future__ import annotations

import logging
from datetime import date

import numpy as np

from pipeline.batch.profiles.base import ComputorBase
from pipeline.batch.profiles.constants import MIN_CATCHER_SB_ATTEMPTS
from pipeline.batch.profiles.defense import (
    GRAVITY_FPS2,
    RUNS_PER_BLOCK_SAVED,
    RUNS_PER_STRIKE_ABOVE_AVG,
    ZONE_HALF_WIDTH,
    _fit_logistic_model,
    _predict_proba,
)
from pipeline.statcast_events import (
    sql_steal_attempt,
    sql_steal_success,
)

log = logging.getLogger("player_profile_computor")


class CatcherProfilesMixin(ComputorBase):
    """Builds ``derived.catcher_season_metrics`` from per-play temp tables."""

    # ==================================================================
    # Defensive Metrics Methods
    # (Catcher framing/blocking/throwing, OF/IF OAA, DP, errors, aggregation)
    # ==================================================================

    def _compute_catcher_framing(self, seasons: list[int], asof: date | None = None) -> None:
        """
        Build a called-strike probability model and compute framing runs.

        Model inputs: pitch location relative to zone edges, count,
        pitcher/batter handedness.

        The 'shadow zone' (~3-6 inches outside the zone boundary) is
        where framing skill matters. Pitches well inside the zone are
        always strikes; pitches well outside are never strikes.

        SIM-537: ``asof`` restricts raw.pitches to ``game_date <= asof`` —
        this source is entirely our own data, so a plain date filter is
        the whole fix (docs/audit/2026-09-10-savant-point-in-time-data.md §6).
        """
        log.info("Computing catcher framing …")
        season_list = ", ".join(str(s) for s in seasons)
        date_cutoff = f"AND game_date <= DATE '{asof.isoformat()}'" if asof is not None else ""

        # Extract all taken pitches (not swung at)
        df = self._conn.execute(f"""
            SELECT
                game_pk,
                at_bat_number,
                pitch_number,
                season,
                fielder_2          AS catcher_id,
                plate_x,
                plate_z,
                sz_top,
                sz_bot,
                p_throws,
                stand,
                balls,
                strikes,
                -- Called strike = 1, called ball = 0
                CASE WHEN type = 'C' THEN 1 ELSE 0 END AS is_strike,
                CASE WHEN type IN ('B', 'C') THEN 1 ELSE 0 END AS is_taken
            FROM pg.raw.pitches
            WHERE season IN ({season_list})
              AND type IN ('B', 'C')
              AND plate_x IS NOT NULL
              AND plate_z IS NOT NULL
              AND sz_top IS NOT NULL
              AND sz_bot IS NOT NULL
              {date_cutoff}
        """).fetchdf()

        if len(df) < 1000:
            log.warning("  Too few taken pitches (%d) — skipping framing", len(df))
            return

        log.info("  Loaded %d taken pitches for framing model", len(df))

        # Compute distance from zone edges (the key framing features)
        df["zone_dist_x"] = (df["plate_x"].abs() - ZONE_HALF_WIDTH).clip(lower=0)
        df["zone_dist_z_top"] = (df["plate_z"] - df["sz_top"]).clip(lower=0)
        df["zone_dist_z_bot"] = (df["sz_bot"] - df["plate_z"]).clip(lower=0)
        df["zone_dist_z"] = df[["zone_dist_z_top", "zone_dist_z_bot"]].max(axis=1)
        df["zone_distance"] = np.sqrt(df["zone_dist_x"] ** 2 + df["zone_dist_z"] ** 2)

        # Additional features
        df["in_zone"] = (
            (df["plate_x"].abs() <= ZONE_HALF_WIDTH)
            & (df["plate_z"] >= df["sz_bot"])
            & (df["plate_z"] <= df["sz_top"])
        ).astype(int)
        df["count_state"] = df["balls"] * 3 + df["strikes"]  # 0-11 encoding
        df["p_throws_enc"] = (df["p_throws"] == "R").astype(int)
        df["stand_enc"] = (df["stand"] == "R").astype(int)
        df["pitch_side"] = df["plate_x"] * np.where(df["stand"] == "R", 1, -1)

        # Zone location categories for breakdown
        df["zone_low"] = (df["plate_z"] < df["sz_bot"] + 0.25).astype(int)
        df["zone_high"] = (df["plate_z"] > df["sz_top"] - 0.25).astype(int)
        df["zone_inside"] = (df["pitch_side"] > 0.4).astype(int)
        df["zone_outside"] = (df["pitch_side"] < -0.4).astype(int)

        # SIM-408: heart (clearly in-zone) vs shadow (just off the edge, where
        # framing skill lives) buckets for the catcher engine's zone-framing
        # features (shadow_zone_strike_rate / heart_zone_strike_rate).
        df["zone_heart"] = df["in_zone"]
        df["zone_shadow"] = ((df["in_zone"] == 0) & (df["zone_distance"] <= 0.6)).astype(int)

        # Fit the logistic model: P(called_strike) = f(location, count, handedness)
        feature_cols = [
            "plate_x",
            "plate_z",
            "zone_distance",
            "in_zone",
            "count_state",
            "p_throws_enc",
            "stand_enc",
        ]
        X = df[feature_cols].values
        y = df["is_strike"].values

        model = _fit_logistic_model(X, y)
        if model is None:
            log.error("  Framing model fit failed — skipping")
            return

        # Predict expected strike probability for each pitch
        df["expected_strike"] = _predict_proba(model, X)

        # Per-catcher aggregation
        framing_agg = (
            df.groupby(["catcher_id", "season"])
            .agg(
                called_pitches=("is_strike", "count"),
                called_strikes=("is_strike", "sum"),
                expected_called_strikes=("expected_strike", "sum"),
                # Zone breakdowns
                strikes_low=("is_strike", lambda x: x[df.loc[x.index, "zone_low"] == 1].sum()),
                expected_low=(
                    "expected_strike",
                    lambda x: x[df.loc[x.index, "zone_low"] == 1].sum(),
                ),
                strikes_high=("is_strike", lambda x: x[df.loc[x.index, "zone_high"] == 1].sum()),
                expected_high=(
                    "expected_strike",
                    lambda x: x[df.loc[x.index, "zone_high"] == 1].sum(),
                ),
                strikes_inside=(
                    "is_strike",
                    lambda x: x[df.loc[x.index, "zone_inside"] == 1].sum(),
                ),
                expected_inside=(
                    "expected_strike",
                    lambda x: x[df.loc[x.index, "zone_inside"] == 1].sum(),
                ),
                strikes_outside=(
                    "is_strike",
                    lambda x: x[df.loc[x.index, "zone_outside"] == 1].sum(),
                ),
                expected_outside=(
                    "expected_strike",
                    lambda x: x[df.loc[x.index, "zone_outside"] == 1].sum(),
                ),
                # SIM-408: heart/shadow called-strike rates (counts here; rate below)
                strikes_heart=("is_strike", lambda x: x[df.loc[x.index, "zone_heart"] == 1].sum()),
                pitches_heart=(
                    "is_strike",
                    lambda x: int((df.loc[x.index, "zone_heart"] == 1).sum()),
                ),
                strikes_shadow=(
                    "is_strike",
                    lambda x: x[df.loc[x.index, "zone_shadow"] == 1].sum(),
                ),
                pitches_shadow=(
                    "is_strike",
                    lambda x: int((df.loc[x.index, "zone_shadow"] == 1).sum()),
                ),
            )
            .reset_index()
        )

        framing_agg["strikes_above_average"] = (
            framing_agg["called_strikes"] - framing_agg["expected_called_strikes"]
        )
        framing_agg["framing_runs"] = (
            framing_agg["strikes_above_average"] * RUNS_PER_STRIKE_ABOVE_AVG
        )
        framing_agg["framing_low"] = framing_agg["strikes_low"] - framing_agg["expected_low"]
        framing_agg["framing_high"] = framing_agg["strikes_high"] - framing_agg["expected_high"]
        framing_agg["framing_inside"] = (
            framing_agg["strikes_inside"] - framing_agg["expected_inside"]
        )
        framing_agg["framing_outside"] = (
            framing_agg["strikes_outside"] - framing_agg["expected_outside"]
        )
        # SIM-408: zone-framing strike rates (NaN when the bucket is empty;
        # the engine COALESCEs NULL → 0 / falls back to league average).
        framing_agg["heart_zone_strike_rate"] = framing_agg["strikes_heart"] / framing_agg[
            "pitches_heart"
        ].replace(0, np.nan)
        framing_agg["shadow_zone_strike_rate"] = framing_agg["strikes_shadow"] / framing_agg[
            "pitches_shadow"
        ].replace(0, np.nan)

        # Store framing results in a temp table for later aggregation
        self._conn.execute("DROP TABLE IF EXISTS _tmp_framing")
        self._conn.execute(
            "CREATE TABLE _tmp_framing AS SELECT * FROM framing_agg",
        )
        self._conn.register("framing_agg", framing_agg)
        self._conn.execute("""
            DROP TABLE IF EXISTS _tmp_framing;
            CREATE TABLE _tmp_framing AS SELECT * FROM framing_agg;
        """)
        self._conn.unregister("framing_agg")

        log.info("  Framing computed for %d catcher-seasons", len(framing_agg))

    # ------------------------------------------------------------------
    # CATCHER BLOCKING
    # ------------------------------------------------------------------

    def _compute_catcher_blocking(self, seasons: list[int], asof: date | None = None) -> None:
        """
        Build a PB/WP probability model and compute blocks above average.

        Core model inputs (from Tango's post):
          - Pitch height at catcher position (primary sigmoid axis)
          - Whether the pitch bounced before reaching catcher
          - Horizontal distance from catcher's center
          - Pitch speed and movement
          - Pitcher/batter handedness

        SIM-537: ``asof`` restricts raw.pitches to ``game_date <= asof``.
        """
        log.info("Computing catcher blocking …")
        season_list = ", ".join(str(s) for s in seasons)
        date_cutoff = f"AND game_date <= DATE '{asof.isoformat()}'" if asof is not None else ""

        df = self._conn.execute(f"""
            SELECT
                game_pk,
                at_bat_number,
                pitch_number,
                season,
                fielder_2           AS catcher_id,
                plate_x,
                plate_z,
                release_speed,
                end_speed,
                vx0, vy0, vz0,
                ax, ay, az,
                pfx_x,
                break_vertical,
                break_horizontal,
                p_throws,
                stand,
                passed_ball_wild_pitch,
                on_1b, on_2b, on_3b
            FROM pg.raw.pitches
            WHERE season IN ({season_list})
              AND plate_x IS NOT NULL
              AND plate_z IS NOT NULL
              AND end_speed IS NOT NULL
              {date_cutoff}
        """).fetchdf()

        if len(df) < 1000:
            log.warning("  Too few pitches for blocking model (%d)", len(df))
            return

        log.info("  Loaded %d pitches for blocking model", len(df))

        # Estimate height at catcher position (~3.9 ft behind front of plate)
        # Using the simple drop approximation from end_speed
        extra_time = 3.9 / (df["end_speed"].clip(lower=50) * 1.467)
        gravity_drop = 0.5 * GRAVITY_FPS2 * extra_time**2
        # Additional drop from downward pitch velocity (vz0 at 50ft plane is often negative)
        pitch_drop = df["vz0"].clip(upper=0).abs() * extra_time * 0.3  # partial effect
        df["height_at_catcher"] = df["plate_z"] - gravity_drop - pitch_drop

        # Classify: bounced vs reached
        df["bounced"] = (df["height_at_catcher"] <= 0.3).astype(int)

        # Catcher's effective "radius" — distance from zone center
        df["dist_from_center"] = np.sqrt(df["plate_x"] ** 2 + (df["plate_z"] - 2.0) ** 2)

        # Lateral distance from center
        df["lateral_dist"] = df["plate_x"].abs()

        # Target variable
        df["pbwp"] = df["passed_ball_wild_pitch"].astype(int)

        # Features for the blocking model
        feature_cols = [
            "height_at_catcher",
            "bounced",
            "dist_from_center",
            "lateral_dist",
            "end_speed",
            "break_horizontal",
            "break_vertical",
        ]
        df_model = df.dropna(subset=feature_cols + ["pbwp"])
        X = df_model[feature_cols].fillna(0).values
        y = df_model["pbwp"].values

        model = _fit_logistic_model(X, y)
        if model is None:
            log.error("  Blocking model fit failed — skipping")
            return

        df_model["expected_pbwp"] = _predict_proba(model, X)

        # Per-catcher aggregation
        blocking_agg = (
            df_model.groupby(["catcher_id", "season"])
            .agg(
                pitches_received=("pbwp", "count"),
                expected_pbwp=("expected_pbwp", "sum"),
                actual_pbwp=("pbwp", "sum"),
            )
            .reset_index()
        )

        blocking_agg["blocks_above_average"] = (
            blocking_agg["expected_pbwp"] - blocking_agg["actual_pbwp"]
        )
        blocking_agg["blocking_runs"] = blocking_agg["blocks_above_average"] * RUNS_PER_BLOCK_SAVED

        # Category breakdowns
        for cat_name, _cat_mask_col in [
            ("bounced", "bounced"),
            ("high", "height_at_catcher"),
            ("lateral", "lateral_dist"),
        ]:
            if cat_name == "bounced":
                mask = df_model["bounced"] == 1
            elif cat_name == "high":
                mask = df_model["height_at_catcher"] > 4.0
            else:
                mask = df_model["lateral_dist"] > 1.5

            cat_agg = (
                df_model[mask]
                .groupby(["catcher_id", "season"])
                .agg(
                    cat_expected=("expected_pbwp", "sum"),
                    cat_actual=("pbwp", "sum"),
                )
                .reset_index()
            )
            cat_agg[f"blocks_aa_{cat_name}"] = cat_agg["cat_expected"] - cat_agg["cat_actual"]
            blocking_agg = blocking_agg.merge(
                cat_agg[["catcher_id", "season", f"blocks_aa_{cat_name}"]],
                on=["catcher_id", "season"],
                how="left",
            )

        self._conn.register("blocking_agg", blocking_agg)
        self._conn.execute("""
            DROP TABLE IF EXISTS _tmp_blocking;
            CREATE TABLE _tmp_blocking AS SELECT * FROM blocking_agg;
        """)
        self._conn.unregister("blocking_agg")

        log.info("  Blocking computed for %d catcher-seasons", len(blocking_agg))

    # ------------------------------------------------------------------
    # CATCHER THROWING (BY BASE)
    # ------------------------------------------------------------------

    def _compute_catcher_throwing(self, seasons: list[int], asof: date | None = None) -> None:
        """Compute SB prevention metrics broken down by base attempted.

        Includes the SIM-073 PA-level deterrence rate
        (steal_attempt_rate_against) used by the catcher similarity engine v2
        as a Deterrence sub-score (SIM-072).

        SIM-537: ``asof`` restricts both raw.pitches reads to
        ``game_date <= asof``.
        """
        log.info("Computing catcher throwing metrics …")
        season_list = ", ".join(str(s) for s in seasons)
        date_cutoff = f"AND game_date <= DATE '{asof.isoformat()}'" if asof is not None else ""

        self._conn.execute(f"""
            DROP TABLE IF EXISTS _tmp_catcher_throwing;
            CREATE TABLE _tmp_catcher_throwing AS

            WITH sb_events AS (
                -- SIM-506: attempt labels = columns OR events (a PA-ending
                -- caught stealing lives only in `events`; the columns alone
                -- hid 43% of 2B caught stealings and READ every catcher's
                -- cs_rate low).
                SELECT
                    fielder_2 AS catcher_id,
                    season,
                    {sql_steal_attempt("2b")}   AS att_2b,
                    {sql_steal_success("2b")}   AS suc_2b,
                    {sql_steal_attempt("3b")}   AS att_3b,
                    {sql_steal_success("3b")}   AS suc_3b,
                    {sql_steal_attempt("home")} AS att_home,
                    {sql_steal_success("home")} AS suc_home
                FROM pg.raw.pitches
                WHERE season IN ({season_list})
                  {date_cutoff}
                  AND ({sql_steal_attempt("2b")} OR {sql_steal_attempt("3b")}
                       OR {sql_steal_attempt("home")})
            ),
            sb_aggregated AS (
                SELECT
                    catcher_id,
                    season,
                    COUNT(*) AS sb_attempts_faced,

                    -- Overall CS
                    SUM(CASE WHEN (att_2b AND NOT suc_2b)
                               OR (att_3b AND NOT suc_3b)
                               OR (att_home AND NOT suc_home)
                             THEN 1 ELSE 0 END) AS cs_total,

                    -- 2B attempts
                    SUM(CASE WHEN att_2b THEN 1 ELSE 0 END) AS sb_attempts_2b,
                    SUM(CASE WHEN att_2b AND NOT suc_2b THEN 1 ELSE 0 END)
                        * 1.0 / NULLIF(SUM(CASE WHEN att_2b THEN 1 ELSE 0 END), 0)
                        AS cs_rate_2b,

                    -- 3B attempts
                    SUM(CASE WHEN att_3b THEN 1 ELSE 0 END) AS sb_attempts_3b,
                    SUM(CASE WHEN att_3b AND NOT suc_3b THEN 1 ELSE 0 END)
                        * 1.0 / NULLIF(SUM(CASE WHEN att_3b THEN 1 ELSE 0 END), 0)
                        AS cs_rate_3b,

                    -- Overall rates
                    SUM(CASE WHEN (att_2b AND NOT suc_2b)
                               OR (att_3b AND NOT suc_3b)
                               OR (att_home AND NOT suc_home)
                             THEN 1 ELSE 0 END) * 1.0 / NULLIF(COUNT(*), 0) AS cs_rate,

                    1.0 - (SUM(CASE WHEN (att_2b AND NOT suc_2b)
                                      OR (att_3b AND NOT suc_3b)
                                      OR (att_home AND NOT suc_home)
                                    THEN 1 ELSE 0 END) * 1.0 / NULLIF(COUNT(*), 0))
                        AS sb_allowed_rate,

                    -- SIM-073 numerator: total steal *attempts* against this catcher
                    -- across 2B + 3B (home steal attempts excluded — they're a
                    -- different baseball action, not a catcher-arm decision).
                    SUM(CASE WHEN att_2b OR att_3b THEN 1 ELSE 0 END)
                        AS steal_attempts_2b_or_3b
                FROM sb_events
                GROUP BY catcher_id, season
            ),
            -- SIM-073: PA-level opportunity counter.  One row per
            -- (catcher, game, at-bat) regardless of pitch count, so a
            -- 7-pitch PA contributes exactly 1 opportunity.
            catcher_pa_state AS (
                SELECT DISTINCT
                    fielder_2 AS catcher_id,
                    season,
                    game_pk,
                    at_bat_number,
                    on_1b,
                    on_2b,
                    on_3b
                FROM pg.raw.pitches
                WHERE season IN ({season_list})
                  {date_cutoff}
                  AND fielder_2 IS NOT NULL
            ),
            catcher_opportunities AS (
                SELECT
                    catcher_id,
                    season,
                    -- 1B opportunity: runner on 1B with 2B empty.  Excludes
                    -- forced-advance scenarios (2B occupied) where the runner
                    -- has no stealing lane.
                    SUM(CASE WHEN on_1b IS NOT NULL AND on_2b IS NULL
                             THEN 1 ELSE 0 END) AS opp_1b_pa,
                    -- 2B opportunity: runner on 2B with 3B empty.
                    SUM(CASE WHEN on_2b IS NOT NULL AND on_3b IS NULL
                             THEN 1 ELSE 0 END) AS opp_2b_pa
                FROM catcher_pa_state
                GROUP BY catcher_id, season
            )
            SELECT
                COALESCE(sa.catcher_id, co.catcher_id) AS catcher_id,
                COALESCE(sa.season, co.season)         AS season,
                COALESCE(sa.sb_attempts_faced, 0)      AS sb_attempts_faced,
                COALESCE(sa.cs_total, 0)               AS cs_total,
                sa.sb_attempts_2b,
                sa.cs_rate_2b,
                sa.sb_attempts_3b,
                sa.cs_rate_3b,
                sa.cs_rate,
                sa.sb_allowed_rate,

                -- SIM-073 deterrence rate
                COALESCE(co.opp_1b_pa, 0) + COALESCE(co.opp_2b_pa, 0)
                    AS sb_opportunities_pa,
                CASE WHEN COALESCE(co.opp_1b_pa, 0) + COALESCE(co.opp_2b_pa, 0) >= 100
                     THEN COALESCE(sa.steal_attempts_2b_or_3b, 0) * 1.0
                          / NULLIF(COALESCE(co.opp_1b_pa, 0) + COALESCE(co.opp_2b_pa, 0), 0)
                     ELSE NULL
                END AS steal_attempt_rate_against
            FROM sb_aggregated sa
            FULL OUTER JOIN catcher_opportunities co
                ON sa.catcher_id = co.catcher_id
               AND sa.season     = co.season
        """)

        log.info("  Catcher throwing metrics computed.")

    def _ensure_catcher_temp_tables_exist(self) -> None:
        """Defensive — same pattern as _ensure_fielder_temp_tables_exist.

        The catcher aggregator FULL-OUTER-JOINs three temp tables:
          _tmp_framing            ← created by _compute_catcher_framing
          _tmp_blocking           ← created by _compute_catcher_blocking
          _tmp_catcher_throwing   ← created by _compute_catcher_throwing

        Each compute step may early-`return` when the per-season sample
        size falls below its guard (typical on partial / small data
        loads).  Empty placeholders preserve the aggregator's join
        shape without injecting fake rows.
        """
        placeholders = {
            "_tmp_framing": """
                CREATE TABLE IF NOT EXISTS _tmp_framing (
                    catcher_id INTEGER, season SMALLINT,
                    called_pitches INTEGER, called_strikes INTEGER,
                    expected_called_strikes FLOAT, strikes_above_average FLOAT,
                    framing_runs FLOAT,
                    framing_low FLOAT, framing_high FLOAT,
                    framing_inside FLOAT, framing_outside FLOAT,
                    shadow_zone_strike_rate FLOAT, heart_zone_strike_rate FLOAT
                )
            """,
            "_tmp_blocking": """
                CREATE TABLE IF NOT EXISTS _tmp_blocking (
                    catcher_id INTEGER, season SMALLINT,
                    expected_pbwp FLOAT, actual_pbwp INTEGER,
                    blocks_above_average FLOAT, blocking_runs FLOAT,
                    blocks_aa_bounced FLOAT, blocks_aa_high FLOAT,
                    blocks_aa_lateral FLOAT
                )
            """,
            "_tmp_catcher_throwing": """
                CREATE TABLE IF NOT EXISTS _tmp_catcher_throwing (
                    catcher_id INTEGER, season SMALLINT,
                    sb_attempts_faced INTEGER, cs_total INTEGER,
                    sb_attempts_2b INTEGER, cs_rate_2b FLOAT,
                    sb_attempts_3b INTEGER, cs_rate_3b FLOAT,
                    cs_rate FLOAT, sb_allowed_rate FLOAT,
                    sb_opportunities_pa INTEGER,
                    steal_attempt_rate_against FLOAT
                )
            """,
        }
        for table, ddl in placeholders.items():
            try:
                self._conn.execute(ddl)
            except Exception as exc:  # noqa: BLE001
                log.warning("  Could not ensure placeholder %s exists: %s", table, exc)

    def _aggregate_catcher_season_metrics(
        self, seasons: list[int], asof: date | None = None
    ) -> None:
        """Combine framing, blocking, and throwing into catcher season metrics.

        SIM-537: the three upstream temp tables are already cutoff-filtered
        by the time this method reads them (they carry no date of their
        own). The two Savant joins below (pop time, arm strength) have no
        date column at all — see the season-shift note on those joins.
        """
        log.info("Aggregating catcher season metrics …")
        season_list = ", ".join(str(s) for s in seasons)
        asof_date = asof or date.today()
        asof_sql = asof_date.isoformat()
        # SIM-537: the Savant pop-time / arm-strength boards have no date
        # column (docs/audit/2026-09-10-savant-point-in-time-data.md §5) —
        # only a season number. A season that ENDED before the cutoff joins
        # its own row; the season CONTAINING the cutoff joins the PRIOR
        # season's row instead, since that season's own row is still
        # accumulating. Measured year-to-year correlation: arm strength
        # 0.894, pop time 0.726 — the substitute costs little. Left
        # unshifted (`COALESCE(f.season, b.season, t.season)`, no shift)
        # when asof is None, so a live build is unchanged.
        catcher_season_col = "COALESCE(f.season, b.season, t.season)"
        savant_season = (
            f"(CASE WHEN {catcher_season_col} = {asof_date.year} "
            f"THEN {catcher_season_col} - 1 ELSE {catcher_season_col} END)"
            if asof is not None
            else catcher_season_col
        )

        # SIM-2026-05 hardening: see _ensure_fielder_temp_tables_exist for
        # the rationale.  When a per-season sample is too thin, the upstream
        # _compute_catcher_{framing,blocking,throwing} method returns early
        # without creating its _tmp_* table.  Guarantee the aggregator's
        # FULL OUTER JOIN inputs all exist (possibly empty) before running.
        self._ensure_catcher_temp_tables_exist()

        # SIM-551: refuse, before any write, a rebuild that would leave the
        # table at two cutoffs.
        self._refuse_a_mixed_cutoff("derived.catcher_season_metrics", seasons, asof_date)

        delete_cutoff = f"OR season > {asof_date.year}" if asof is not None else ""
        self._conn.execute(f"""
            DELETE FROM derived.catcher_season_metrics
            WHERE season IN ({season_list}) {delete_cutoff}
        """)

        self._conn.execute(f"""
            INSERT INTO derived.catcher_season_metrics (
                player_id, season,
                pitches_received_total, called_pitches, called_strikes,
                expected_called_strikes, strikes_above_average, framing_runs,
                framing_low, framing_high, framing_inside, framing_outside,
                shadow_zone_strike_rate, heart_zone_strike_rate,
                expected_pbwp, actual_pbwp, blocks_above_average, blocking_runs,
                blocks_aa_bounced, blocks_aa_high, blocks_aa_lateral,
                pop_time_mean, pop_time_std, arm_strength_mean,
                sb_attempts_faced, cs_total, cs_rate, sb_allowed_rate,
                sb_attempts_2b, cs_rate_2b, sb_attempts_3b, cs_rate_3b,
                steal_attempt_rate_against,
                pickoff_attempts, pickoff_successes, pickoff_rate,
                below_minimum_sample, updated_at, asof_date
            )

            SELECT
                COALESCE(f.catcher_id, b.catcher_id, t.catcher_id) AS player_id,
                COALESCE(f.season, b.season, t.season) AS season,

                -- Framing
                f.called_pitches            AS pitches_received_total,
                f.called_pitches,
                f.called_strikes,
                f.expected_called_strikes,
                f.strikes_above_average,
                f.framing_runs,
                f.framing_low,
                f.framing_high,
                f.framing_inside,
                f.framing_outside,
                -- SIM-408: zone-framing strike rates (catcher engine)
                f.shadow_zone_strike_rate,
                f.heart_zone_strike_rate,

                -- Blocking
                b.expected_pbwp,
                CAST(b.actual_pbwp AS INTEGER) AS actual_pbwp,
                b.blocks_above_average,
                b.blocking_runs,
                b.blocks_aa_bounced,
                b.blocks_aa_high,
                b.blocks_aa_lateral,

                -- Throwing
                -- SIM-530: pop time is now MEASURED, not inferred.
                --
                -- What stood here was a proxy: 2.0 + (0.25 - cs_rate) * 2.0.
                -- That is a deterministic function of the caught-stealing rate,
                -- and the catcher engine weights BOTH pop time (0.900) and
                -- cs_rate (0.600) inside the same throwing sub-score. So the
                -- heaviest feature in that sub-score carried no information the
                -- second one did not already carry — it was cs_rate wearing a
                -- different name and a seconds unit. Savant measures the real
                -- thing. The proxy survives only as the last fallback, so a
                -- catcher Savant has no row for still gets a value rather than
                -- dropping out of the sub-score.
                COALESCE(
                    spt.pop_time_2b,
                    sct.pop_time,
                    CASE WHEN t.cs_rate IS NOT NULL
                         THEN 2.0 + (0.25 - t.cs_rate) * 2.0 END
                )                               AS pop_time_mean,
                0.08                            AS pop_time_std,
                -- SIM-530: NULL on all 420 catcher-seasons in the pool window,
                -- while the engine weights it at 0.800 inside the throwing
                -- sub-score. Pop time is the primary source (it covers 99 of
                -- our 100 catchers for 2024 at no minimum); the catcher-throwing
                -- board is the fallback for the rest.
                COALESCE(spt.arm_strength, sct.arm_strength)
                                                AS arm_strength_mean,
                CAST(t.sb_attempts_faced AS INTEGER),
                CAST(t.cs_total AS INTEGER),
                t.cs_rate,
                t.sb_allowed_rate,
                CAST(t.sb_attempts_2b AS INTEGER),
                t.cs_rate_2b,
                CAST(t.sb_attempts_3b AS INTEGER),
                t.cs_rate_3b,

                -- SIM-073 deterrence rate (PA-level steal-attempt rate).
                -- Min-sample guard already enforced inside _tmp_catcher_throwing
                -- (NULL when sb_opportunities_pa < 100).
                t.steal_attempt_rate_against,

                -- Pickoffs (not yet computed — placeholder)
                NULL::INTEGER AS pickoff_attempts,
                NULL::INTEGER AS pickoff_successes,
                NULL::FLOAT   AS pickoff_rate,

                -- Meta
                (COALESCE(CAST(t.sb_attempts_faced AS INTEGER), 0) < {MIN_CATCHER_SB_ATTEMPTS})
                    AS below_minimum_sample,
                CURRENT_TIMESTAMP AS updated_at,
                DATE '{asof_sql}' AS asof_date

            FROM _tmp_framing f
            FULL OUTER JOIN _tmp_blocking b
                ON f.catcher_id = b.catcher_id AND f.season = b.season
            FULL OUTER JOIN _tmp_catcher_throwing t
                ON COALESCE(f.catcher_id, b.catcher_id) = t.catcher_id
                AND COALESCE(f.season, b.season) = t.season
            -- SIM-530/537: the measured arm. The key is a FULL OUTER JOIN
            -- coalesce, and a join condition cannot reference a SELECT
            -- alias, so the coalesce is repeated here on purpose. The
            -- season on the right of `=` is the season-shift substitution
            -- described above — unshifted when asof is None.
            LEFT JOIN pg.raw.savant_poptime spt
                ON spt.player_id = COALESCE(f.catcher_id, b.catcher_id, t.catcher_id)
               AND spt.season    = {savant_season}
            LEFT JOIN pg.raw.savant_catcher_throwing sct
                ON sct.player_id = COALESCE(f.catcher_id, b.catcher_id, t.catcher_id)
               AND sct.season    = {savant_season}
        """)
        self._stamp_one_cutoff("derived.catcher_season_metrics", asof_sql)

        # Clean up temp tables
        for t in [
            "_tmp_framing",
            "_tmp_blocking",
            "_tmp_catcher_throwing",
            "_tmp_of_plays",
            "_tmp_if_plays",
            "_tmp_dp_plays",
            "_tmp_bunt_defense",
            "_tmp_1b_scoop",
            "_tmp_errors",
        ]:
            self._conn.execute(f"DROP TABLE IF EXISTS {t}")

        log.info("  Catcher season metrics aggregated.")

    #: SIM-517: the EB prior for the uncaught-strike-3 rate, in strike-3s.
    #: The event runs ~0.2% of K3s (~80 PAs league-wide per season), so a
    #: per-catcher-season raw rate is Poisson noise; a catcher needs ~2,000
    #: received strike-3s (~3 full seasons) to move halfway from league.
    _UNCAUGHT_K3_EB_PRIOR_N = 2000.0

    def _compute_catcher_uncaught_k3(self, seasons: list[int], asof: date | None = None) -> None:
        """SIM-517 part A: the strike-3 slice of the receiving profile.

        A strikeout PA whose final-pitch description names a wild pitch or a
        passed ball is an uncaught third strike. The label is exact for this
        data: mid-PA get-away pitches never leak into the strikeout text
        (measured 2026-09-03 — 410 strikeout PAs in 2025 contained a mid-PA
        `passed_ball_wild_pitch` pitch and ZERO of their descriptions mention
        it; the 78 that do are all terminal uncaught-K3 plays). The catcher
        is the final pitch's own ``fielder_2``.

        Updates the freshly aggregated ``derived.catcher_season_metrics``
        rows: raw counts land in ``sample_``-prefixed columns (excluded from
        the similarity embedding) and the one embedding-visible column is the
        EB-shrunk rate toward the SEASON league rate
        (:data:`_UNCAUGHT_K3_EB_PRIOR_N`).

        SIM-537: ``asof`` restricts raw.pitches to ``game_date <= asof`` —
        the same cutoff already applied to the row this UPDATE targets, so
        the K3 sample and the framing/blocking/throwing sample it joins stay
        at one point in time. This is the last catcher step in the nightly
        chain, so the leakage check for the whole grouping runs here.
        """
        log.info("Computing catcher uncaught strike-3 rates (SIM-517) …")
        season_list = ", ".join(str(s) for s in seasons)
        asof_date = asof or date.today()
        date_cutoff = f"AND game_date <= DATE '{asof_date.isoformat()}'" if asof is not None else ""
        self._conn.execute(f"""
            CREATE OR REPLACE TEMP TABLE _tmp_uncaught_k3 AS
            SELECT
                fielder_2 AS catcher_id,
                season,
                COUNT(*) AS k3_received,
                COUNT(*) FILTER (
                    WHERE des ILIKE '%wild pitch%' OR des ILIKE '%passed ball%'
                ) AS uncaught_k3
            FROM pg.raw.pitches
            WHERE season IN ({season_list})
              AND events IN ('strikeout', 'strikeout_double_play')
              AND fielder_2 IS NOT NULL
              {date_cutoff}
            GROUP BY 1, 2
        """)
        prior_n = float(self._UNCAUGHT_K3_EB_PRIOR_N)
        self._conn.execute(f"""
            UPDATE derived.catcher_season_metrics m SET
                sample_k3_received = k.k3_received,
                sample_uncaught_k3 = k.uncaught_k3,
                uncaught_k3_rate_eb =
                    (k.uncaught_k3 + {prior_n} * lg.league_rate)
                    / (k.k3_received + {prior_n})
            FROM _tmp_uncaught_k3 k
            JOIN (
                SELECT season,
                       SUM(uncaught_k3)::DOUBLE / NULLIF(SUM(k3_received), 0)
                           AS league_rate
                FROM _tmp_uncaught_k3 GROUP BY season
            ) lg ON lg.season = k.season
            WHERE m.player_id = k.catcher_id AND m.season = k.season
        """)
        n = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(sample_uncaught_k3), 0) "
            f"FROM derived.catcher_season_metrics WHERE season IN ({season_list}) "
            "AND sample_k3_received IS NOT NULL"
        ).fetchone()
        self._conn.execute("DROP TABLE IF EXISTS _tmp_uncaught_k3")
        rows, uncaught = (int(n[0]), int(n[1])) if n and len(n) >= 2 else (0, 0)
        log.info("  Uncaught K3: %d catcher-seasons updated, %d uncaught K3s.", rows, uncaught)
        self._assert_catcher_profiles_have_no_leakage(seasons, asof_date)

    def _assert_catcher_profiles_have_no_leakage(self, seasons: list[int], asof: date) -> None:
        """Prove raw.pitches contributed no row dated after the cutoff.

        Covers framing, blocking, throwing, and the uncaught-K3 update —
        they all read raw.pitches under the identical season+cutoff filter,
        so one check at the end of the chain covers all four. The Savant
        pop-time / arm-strength joins have no date of their own to check —
        they are made safe by the season-shift substitution in the
        aggregator's query, not by filtering.
        """
        season_list = ", ".join(str(s) for s in seasons)
        self._check_leakage(
            "raw.pitches",
            f"SELECT MAX(game_date) FROM pg.raw.pitches "
            f"WHERE season IN ({season_list}) AND game_date <= DATE '{asof}'",
            asof,
        )
