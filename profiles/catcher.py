"""The catcher season profile: framing, blocking, throwing by base and the uncaught third strike.

Framing is the called strikes a catcher gains beyond what the pitch location
predicts. Blocking is the wild pitches and passed balls he prevents. Three
steps read ``raw.pitches`` and write ``_tmp_framing``, ``_tmp_blocking`` and
``_tmp_catcher_throwing``. The aggregate joins them with two Savant boards into
``derived.catcher_season_metrics``. The uncaught-third-strike step then
updates those rows.
"""

from __future__ import annotations

import logging
from datetime import date

import numpy as np

from ingest.statcast_events import (
    sql_any_steal_attempt,
    sql_steal_attempt,
    sql_steal_success,
)
from profiles.base import ComputorBase
from profiles.constants import MIN_CATCHER_SB_ATTEMPTS
from profiles.defense import (
    GRAVITY_FPS2,
    RUNS_PER_BLOCK_SAVED,
    RUNS_PER_STRIKE_ABOVE_AVG,
    ZONE_HALF_WIDTH,
    _fit_logistic_model,
    _predict_proba,
)
from profiles.sql import sql_seasons

log = logging.getLogger("player_profile_computor")

#: The three temp tables the catcher aggregate joins (FULL OUTER JOIN), each as
#: a CREATE TABLE IF NOT EXISTS placeholder:
#:   _tmp_framing            <- built by _compute_catcher_framing
#:   _tmp_blocking           <- built by _compute_catcher_blocking
#:   _tmp_catcher_throwing   <- built by _compute_catcher_throwing
#: A step returns early, with no table, when its sample is too small. The empty
#: placeholder keeps the join working and adds no fake row.
_CATCHER_PLACEHOLDER_DDL = {
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


class CatcherProfilesMixin(ComputorBase):
    """Builds ``derived.catcher_season_metrics`` from per-play temp tables."""

    # ------------------------------------------------------------------
    # The catcher framing
    # ------------------------------------------------------------------

    def _compute_catcher_framing(self, seasons: list[int], asof: date | None = None) -> None:
        """Fit a called-strike model on the taken pitches and compute each
        catcher's framing runs.

        The model reads the pitch's location, its distance outside the zone
        edges, the count and both hands. Framing skill shows in the shadow
        zone, just off the edge: a pitch deep in the zone is always a strike,
        and a pitch far outside never is.

        Writes ``_tmp_framing``. ``asof`` keeps ``raw.pitches`` to
        ``game_date <= asof``. The source is our own data, so the date filter
        is enough.
        """
        log.info("Computing catcher framing …")
        season_list = sql_seasons(seasons)
        date_cutoff = f"AND game_date <= DATE '{asof.isoformat()}'" if asof is not None else ""

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
                CASE WHEN type = 'C' THEN 1 ELSE 0 END AS is_strike
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

        # The distance outside the zone edges, in feet: the key framing feature.
        df["zone_dist_x"] = (df["plate_x"].abs() - ZONE_HALF_WIDTH).clip(lower=0)
        df["zone_dist_z_top"] = (df["plate_z"] - df["sz_top"]).clip(lower=0)
        df["zone_dist_z_bot"] = (df["sz_bot"] - df["plate_z"]).clip(lower=0)
        df["zone_dist_z"] = df[["zone_dist_z_top", "zone_dist_z_bot"]].max(axis=1)
        df["zone_distance"] = np.sqrt(df["zone_dist_x"] ** 2 + df["zone_dist_z"] ** 2)

        df["in_zone"] = (
            (df["plate_x"].abs() <= ZONE_HALF_WIDTH)
            & (df["plate_z"] >= df["sz_bot"])
            & (df["plate_z"] <= df["sz_top"])
        ).astype(int)
        df["count_state"] = df["balls"] * 3 + df["strikes"]  # 0-11 encoding
        df["p_throws_enc"] = (df["p_throws"] == "R").astype(int)
        df["stand_enc"] = (df["stand"] == "R").astype(int)
        df["pitch_side"] = df["plate_x"] * np.where(df["stand"] == "R", 1, -1)

        # The four regions of the framing breakdown.
        df["zone_low"] = (df["plate_z"] < df["sz_bot"] + 0.25).astype(int)
        df["zone_high"] = (df["plate_z"] > df["sz_top"] - 0.25).astype(int)
        df["zone_inside"] = (df["pitch_side"] > 0.4).astype(int)
        df["zone_outside"] = (df["pitch_side"] < -0.4).astype(int)

        # The heart (in the zone) and the shadow (within 0.6 ft outside the
        # edge) feed the catcher engine's shadow_zone_strike_rate and
        # heart_zone_strike_rate.
        df["zone_heart"] = df["in_zone"]
        df["zone_shadow"] = ((df["in_zone"] == 0) & (df["zone_distance"] <= 0.6)).astype(int)

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

        df["expected_strike"] = _predict_proba(model, X)

        framing_agg = (
            df.groupby(["catcher_id", "season"])
            .agg(
                called_pitches=("is_strike", "count"),
                called_strikes=("is_strike", "sum"),
                expected_called_strikes=("expected_strike", "sum"),
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
        # NaN for an empty bucket: the engine reads the NULL as 0 or falls back
        # to the league average.
        framing_agg["heart_zone_strike_rate"] = framing_agg["strikes_heart"] / framing_agg[
            "pitches_heart"
        ].replace(0, np.nan)
        framing_agg["shadow_zone_strike_rate"] = framing_agg["strikes_shadow"] / framing_agg[
            "pitches_shadow"
        ].replace(0, np.nan)

        self._conn.register("framing_agg", framing_agg)
        self._conn.execute("""
            DROP TABLE IF EXISTS _tmp_framing;
            CREATE TABLE _tmp_framing AS SELECT * FROM framing_agg;
        """)
        self._conn.unregister("framing_agg")

        log.info("  Framing computed for %d catcher-seasons", len(framing_agg))

    # ------------------------------------------------------------------
    # The catcher blocking
    # ------------------------------------------------------------------

    def _compute_catcher_blocking(self, seasons: list[int], asof: date | None = None) -> None:
        """Fit a wild-pitch and passed-ball model and compute each catcher's
        blocks above average.

        The model follows Tango. It reads the pitch's estimated height at the
        catcher (the main input), whether it bounced, its distance from the
        zone centre, its lateral distance, its end speed and its break. Blocks
        above average are the expected wild pitches and passed balls minus the
        actual ones.

        Writes ``_tmp_blocking``. ``asof`` keeps ``raw.pitches`` to
        ``game_date <= asof``.
        """
        log.info("Computing catcher blocking …")
        season_list = sql_seasons(seasons)
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

        # The height at the catcher, about 3.9 ft behind the front of the plate:
        # a simple drop over the extra flight time (mph × 1.467 = ft/s).
        extra_time = 3.9 / (df["end_speed"].clip(lower=50) * 1.467)
        gravity_drop = 0.5 * GRAVITY_FPS2 * extra_time**2
        # A downward velocity (vz0 at the 50 ft plane, often negative) adds drop.
        pitch_drop = df["vz0"].clip(upper=0).abs() * extra_time * 0.3  # partial effect
        df["height_at_catcher"] = df["plate_z"] - gravity_drop - pitch_drop

        df["bounced"] = (df["height_at_catcher"] <= 0.3).astype(int)

        # The distance from the zone centre, taken as 2.0 ft high.
        df["dist_from_center"] = np.sqrt(df["plate_x"] ** 2 + (df["plate_z"] - 2.0) ** 2)

        df["lateral_dist"] = df["plate_x"].abs()

        df["pbwp"] = df["passed_ball_wild_pitch"].astype(int)

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

        # The breakdown: bounced, high (over 4 ft) and wide (over 1.5 ft from the centre).
        for cat_name in ("bounced", "high", "lateral"):
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
    # The catcher throwing, by base
    # ------------------------------------------------------------------

    def _compute_catcher_throwing(self, seasons: list[int], asof: date | None = None) -> None:
        """Compute each catcher's stolen-base prevention, by the base attempted.

        The step also computes ``steal_attempt_rate_against``, the catcher
        engine's deterrence sub-score: the steal attempts of second and third
        base per steal chance. The chances count each distinct (plate
        appearance, base state) with a runner on first and second base empty,
        or a runner on second and third base empty. The rate is NULL below 100
        chances.

        Writes ``_tmp_catcher_throwing``. ``asof`` keeps both ``raw.pitches``
        reads to ``game_date <= asof``.
        """
        log.info("Computing catcher throwing metrics …")
        season_list = sql_seasons(seasons)
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
                  AND {sql_any_steal_attempt()}
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

    def _aggregate_catcher_season_metrics(
        self, seasons: list[int], asof: date | None = None
    ) -> None:
        """Join the framing, blocking and throwing temp tables and two Savant
        boards into ``derived.catcher_season_metrics``.

        The temp tables carry no date; their builders already cut them at
        ``asof``. The Savant pop-time and catcher-throwing boards carry only a
        season number, so each join takes the Savant season shift
        (``_savant_season_sql``): a season that ended before the cutoff joins
        its own row, and the season that contains the cutoff joins the prior
        season's row, because its own row is still growing. Arm strength and
        pop time repeat from year to year, so the substitute costs little.
        """
        log.info("Aggregating catcher season metrics …")
        asof_date = asof or date.today()
        asof_sql = asof_date.isoformat()
        savant_season = self._savant_season_sql("COALESCE(f.season, b.season, t.season)", asof)

        # A builder skips its table on a small sample; the FULL OUTER JOIN
        # needs all three.
        self._ensure_placeholder_tables(_CATCHER_PLACEHOLDER_DDL)

        self._refuse_a_mixed_cutoff("derived.catcher_season_metrics", seasons, asof_date)

        self._delete_season_rows("derived.catcher_season_metrics", seasons, asof)

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

        # The aggregate is the temp tables' only reader.
        for t in _CATCHER_PLACEHOLDER_DDL:
            self._conn.execute(f"DROP TABLE IF EXISTS {t}")

        log.info("  Catcher season metrics aggregated.")

    #: The weight, in third strikes, that shrinks a catcher's uncaught-third-strike
    #: rate toward the league rate (an empirical-Bayes prior). The event is rare,
    #: about 0.2% of third strikes (about 80 a season, league-wide), so one
    #: catcher-season's raw rate is mostly noise. A catcher needs about 2,000
    #: received third strikes (about three full seasons) to move halfway from
    #: the league rate.
    _UNCAUGHT_K3_EB_PRIOR_N = 2000.0

    def _compute_catcher_uncaught_k3(self, seasons: list[int], asof: date | None = None) -> None:
        """Add the uncaught-third-strike slice to the catcher receiving profile.

        A strikeout whose final-pitch description names a wild pitch or a
        passed ball is an uncaught third strike. The label is exact for this
        data: a get-away pitch earlier in the plate appearance never shows in
        the strikeout's description. The catcher is the final pitch's own
        ``fielder_2``.

        The step updates the rows of ``derived.catcher_season_metrics`` that the
        aggregate just wrote. The raw counts go to the ``sample_`` columns,
        which the similarity score leaves out. The one column the score reads,
        ``uncaught_k3_rate_eb``, is the rate shrunk toward the season's league
        rate by :data:`_UNCAUGHT_K3_EB_PRIOR_N`.

        ``asof`` keeps ``raw.pitches`` to ``game_date <= asof``, the cutoff of
        the rows the step updates, so the third-strike sample and the rest of
        the row hold one point in time. This is the last catcher step, so the
        leakage check of every catcher step runs here.
        """
        log.info("Computing catcher uncaught strike-3 rates (SIM-517) …")
        season_list = sql_seasons(seasons)
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
        """Run the cutoff leakage check on ``raw.pitches``.

        Framing, blocking, throwing and the uncaught-third-strike step read
        ``raw.pitches`` under the same season and cutoff filter, so one check
        covers all four. The two Savant boards carry no date to check; the
        season shift guards them.
        """
        self._check_pitch_sources_leakage(seasons, asof)
