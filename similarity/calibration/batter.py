"""The batter engine's calibration: its five sigmas, its prior and its reliability weights."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from db.introspect import duck_columns
from similarity.calibration.base import CalibratorBase
from similarity.calibration.fitting import calibrate_eb_prior, calibrate_reliability_weights
from similarity.calibration.report import CalibrationReport

log = logging.getLogger("similarity_calibration")


class BatterCalibratorMixin(CalibratorBase):
    """The batter engine's part of ``SimilarityCalibrator``."""

    def _calibrate_batter_params(
        self,
        conn: Any,
        seasons: list[int],
        target: float,
        report: CalibrationReport,
    ) -> CalibrationReport:
        """Fit the batter engine's sigmas, prior and reliability weights.

        The discipline, batted-ball and power blocks fit over every batter-season
        above the minimum sample. The platoon block fits over the batters with
        30 or more plate appearances against right-handers; with fewer than 20
        such rows it takes the discipline sigma. The physical block (swing and
        stance, SIM-529) fits over the rows that carry all ten features: Savant
        publishes no swing tracking before 2023.
        """
        from similarity.engines.batter_similarity import (
            BATTED_BALL_FEATURES,
            DISCIPLINE_FEATURES,
            PHYSICAL_FEATURES,
            PLATOON_FEATURES,
            POWER_FEATURES,
        )

        sl = ", ".join(str(s) for s in seasons)
        # The profile build does not always write xba / xslg or the physical
        # block. An absent column is selected as NULL, so the columns keep their
        # positions; ``r[col + i] or 0.0`` turns the None into 0.0.
        try:
            _present = duck_columns(conn, "derived", "batter_season_metrics")
        except Exception:
            _present = {"xba", "xslg"}

        def _opt(name: str) -> str:
            return name if name in _present else f"NULL AS {name}"

        rows = conn.execute(f"""
            SELECT
                batter_id, season, sample_pa,
                -- Discipline (7)
                first_pitch_take_rate, o_swing_rate, z_swing_rate,
                contact_rate, whiff_rate, k_rate, walk_rate,
                -- Batted ball (8)
                gb_rate, fb_rate, pull_rate, oppo_rate,
                avg_exit_velo, avg_launch_angle, hard_hit_rate, barrel_rate,
                -- Power (4)
                hr_rate, {_opt("xba")}, {_opt("xslg")}, max_exit_velo,
                -- Platoon vs R (7) — larger split, better for calibration
                o_swing_rate_vs_r, z_swing_rate_vs_r, whiff_rate_vs_r,
                walk_rate_vs_r, k_rate_vs_r, gb_rate_vs_r, barrel_rate_vs_r,
                sample_pa_vs_r,
                -- SIM-529 physical (10). Guarded, so this fit still runs against
                -- a database that has not applied migration 0025.
                {", ".join(_opt(name) for name, _ in PHYSICAL_FEATURES)}
            FROM derived.batter_season_metrics
            WHERE season IN ({sl}) AND below_minimum_sample = FALSE
        """).fetchall()

        if not rows:
            log.warning("No batter profiles found for calibration.")
            return report

        report.n_profiles_used = len(rows)

        ids = np.array([r[0] for r in rows])
        sample_pa = np.array([r[2] for r in rows])

        n_disc = len(DISCIPLINE_FEATURES)
        n_bb = len(BATTED_BALL_FEATURES)
        n_pow = len(POWER_FEATURES)

        n_plat = len(PLATOON_FEATURES)

        col = 3  # first feature column index
        disc_raw = np.array([[r[col + i] or 0.0 for i in range(n_disc)] for r in rows])
        col += n_disc
        bb_raw = np.array([[r[col + i] or 0.0 for i in range(n_bb)] for r in rows])
        col += n_bb
        pow_raw = np.array([[r[col + i] or 0.0 for i in range(n_pow)] for r in rows])
        col += n_pow
        plat_raw = np.array([[r[col + i] or 0.0 for i in range(n_plat)] for r in rows])
        col += n_plat
        sample_pa_vs_r = np.array([r[col] or 0 for r in rows])
        col += 1
        # The physical block keeps NULL as NaN, not 0.0: a batter with no Savant
        # row is not the slowest swing in the league. The z-scoring is NaN-aware.
        n_phys = len(PHYSICAL_FEATURES)
        phys_raw = np.array(
            [
                [np.nan if r[col + i] is None else float(r[col + i]) for i in range(n_phys)]
                for r in rows
            ],
            dtype=np.float64,
        )

        # The platoon block: the batters with a real split sample.
        plat_mask = sample_pa_vs_r >= 30
        plat_filtered = plat_raw[plat_mask]
        plat_ids = ids[plat_mask]

        disc_z = self._zscore_matrix(disc_raw)
        bb_z = self._zscore_matrix(bb_raw)
        pow_z = self._zscore_matrix(pow_raw)

        # _fit_sigma returns 0.0 (keep the engine default) for a block with no
        # spread; see CalibratorBase._fit_sigma.
        report.sigma_discipline = self._fit_sigma(disc_z, target)
        report.sigma_batted_ball = self._fit_sigma(bb_z, target)
        report.sigma_power = self._fit_sigma(pow_z, target)
        # The physical sigma fits over the measured rows only. About two thirds
        # of the batter-seasons carry an all-NaN physical block; over every row
        # the pairwise distances come back NaN and the fit degenerates to the
        # 0.0 sentinel.
        phys_mask = np.isfinite(phys_raw).all(axis=1)
        phys_filtered = phys_raw[phys_mask]
        if len(phys_filtered) >= 20:
            report.sigma_physical = self._fit_sigma(self._zscore_matrix(phys_filtered), target)
        else:
            log.warning(
                "Only %d batter-seasons carry the physical block; keeping the "
                "engine's default physical sigma. Run the Savant loader "
                "(SIM-528) and rebuild the batter profiles.",
                len(phys_filtered),
            )

        if len(plat_filtered) >= 20:
            plat_z = self._zscore_matrix(plat_filtered)
            report.sigma_platoon = self._fit_sigma(plat_z, target)
        else:
            report.sigma_platoon = report.sigma_discipline
            log.warning("Insufficient platoon data for sigma calibration; using discipline sigma.")

        all_raw = np.hstack([disc_raw, bb_raw, pow_raw])
        report.eb_n_prior_batter = calibrate_eb_prior(all_raw, sample_pa)

        report.reliability_weights_discipline = calibrate_reliability_weights(
            disc_raw,
            ids,
        )
        report.reliability_weights_batted_ball = calibrate_reliability_weights(
            bb_raw,
            ids,
        )
        report.reliability_weights_power = calibrate_reliability_weights(
            pow_raw,
            ids,
        )
        if len(phys_filtered) >= 20:
            report.reliability_weights_physical = calibrate_reliability_weights(
                phys_filtered,
                ids[phys_mask],
            )
        if len(plat_filtered) >= 20:
            report.reliability_weights_platoon = calibrate_reliability_weights(
                plat_filtered,
                plat_ids,
            )
        else:
            log.warning("Insufficient platoon data for reliability calibration; using defaults.")

        log.info(
            "Batter calibration complete: %d profiles, sigma_disc=%.3f, "
            "sigma_bb=%.3f, sigma_plat=%.3f, sigma_pow=%.3f, sigma_phys=%.3f "
            "(over %d measured batter-seasons), "
            "eb_prior=%.1f",
            len(rows),
            report.sigma_discipline,
            report.sigma_batted_ball,
            report.sigma_platoon,
            report.sigma_power,
            report.sigma_physical,
            len(phys_filtered),
            report.eb_n_prior_batter,
        )
        return report
