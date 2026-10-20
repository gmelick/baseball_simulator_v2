"""The baserunner engine's calibration (extra bases on a ball in play):
its three sigmas, its prior and its reliability weights."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from similarity.calibration.base import CalibratorBase
from similarity.calibration.fitting import calibrate_eb_prior, calibrate_reliability_weights
from similarity.calibration.report import CalibrationReport

log = logging.getLogger("similarity_calibration")


class BaserunnerCalibratorMixin(CalibratorBase):
    """The baserunner engine's part of ``SimilarityCalibrator``."""

    def _calibrate_baserunner_params(
        self,
        conn: Any,
        seasons: list[int],
        target_median_score: float,
        report: CalibrationReport,
    ) -> CalibrationReport:
        """Fit the speed, aggression and success sigmas, the prior and the weights.

        The aggression group's last feature, Savant's extra-base attempt rate
        above expected (the running-game data, SIM-531), is NULL for an
        unmeasured runner. The aggression sigma and weights fit over the rows
        that carry it.
        """
        from similarity.engines.baserunner_similarity import (
            AGGRESSION_FEATURES,
            SPEED_FEATURES,
            SUCCESS_FEATURES,
        )

        sl = ", ".join(str(s) for s in seasons)
        # The Savant column exists only after DuckDB migration 0029.
        xb_col = (
            "xb_attempt_rate_above_expected"
            if self._has_columns(
                conn, "baserunner_season_metrics", ("xb_attempt_rate_above_expected",)
            )
            else "NULL AS xb_attempt_rate_above_expected"
        )
        rows = conn.execute(f"""
            SELECT
                player_id, season, sample_advancement_opps,
                -- Speed (1)
                sprint_speed,
                -- Aggression (7 since SIM-531)
                extra_base_attempt_rate, first_to_third_attempt_rate,
                second_to_home_attempt_rate, first_to_home_attempt_rate,
                tag_up_attempt_rate, stop_rate,
                {xb_col},
                -- Success (5)
                extra_base_success_rate, first_to_third_success_rate,
                second_to_home_success_rate, first_to_home_success_rate,
                tag_up_success_rate
            FROM derived.baserunner_season_metrics
            WHERE season IN ({sl}) AND NOT below_minimum_sample
        """).fetchall()

        if not rows:
            log.warning("No baserunner profiles found for calibration.")
            return report

        ids = np.array([r[0] for r in rows])
        samples = np.array([r[2] for r in rows])

        n_speed = len(SPEED_FEATURES)
        n_agg = len(AGGRESSION_FEATURES)
        n_suc = len(SUCCESS_FEATURES)
        # The Savant feature keeps NULL as NaN (unmeasured), never 0.0: 0.0 is
        # a real reading there.
        nan_cols = {
            i
            for i, (f, _) in enumerate(AGGRESSION_FEATURES)
            if f == "xb_attempt_rate_above_expected"
        }

        col = 3
        speed_raw = np.array([[r[col + i] or 0.0 for i in range(n_speed)] for r in rows])
        col += n_speed
        agg_raw = np.array(
            [
                [
                    (np.nan if r[col + i] is None else float(r[col + i]))
                    if i in nan_cols
                    else (r[col + i] or 0.0)
                    for i in range(n_agg)
                ]
                for r in rows
            ],
            dtype=np.float64,
        )
        col += n_agg
        suc_raw = np.array([[r[col + i] or 0.0 for i in range(n_suc)] for r in rows])

        # The aggression sigma and weights fit over the MEASURED rows (every
        # feature finite). A row with no Savant figure would read as an exact
        # match on that feature (calibrate_sigma zeroes a NaN difference) and
        # pull the median distance down. With no measured row at all (a
        # database without the Savant load), the fit runs over the other six
        # features on every row.
        agg_measured = np.isfinite(agg_raw).all(axis=1)
        if agg_measured.any():
            agg_fit, agg_fit_ids = agg_raw[agg_measured], ids[agg_measured]
        else:
            agg_fit, agg_fit_ids = agg_raw[:, [i for i in range(n_agg) if i not in nan_cols]], ids

        # _fit_sigma returns 0.0 (keep the engine default) for a block with no
        # spread, e.g. an unpopulated sprint_speed column.
        report.sigma_baserunner_speed = self._fit_sigma(
            self._zscore_matrix(speed_raw), target_median_score
        )
        report.sigma_baserunner_aggression = self._fit_sigma(
            self._zscore_matrix(agg_fit), target_median_score
        )
        report.sigma_baserunner_success = self._fit_sigma(
            self._zscore_matrix(suc_raw), target_median_score
        )

        # The prior is NaN-aware over the population.
        all_raw = np.hstack([speed_raw, agg_raw, suc_raw])
        report.eb_n_prior_baserunner = calibrate_eb_prior(all_raw, samples, min_sample=20)

        # The aggression weights are written ONLY when the fit covered every
        # feature: the engine's ``apply_calibration`` refuses a six-weight vector
        # for its seven features, so a database without the Savant load keeps
        # the engine's module weights.
        report.reliability_weights_baserunner_speed = calibrate_reliability_weights(speed_raw, ids)
        if agg_fit.shape[1] == n_agg:
            report.reliability_weights_baserunner_aggression = calibrate_reliability_weights(
                agg_fit, agg_fit_ids
            )
        else:
            report.reliability_weights_baserunner_aggression = None
            log.warning(
                "SIM-531: no baserunner row carries xb_attempt_rate_above_expected; the "
                "aggression reliability weights are left unset (the engine keeps its module "
                "weights). Load the Savant baserunning board and rebuild the profiles."
            )
        report.reliability_weights_baserunner_success = calibrate_reliability_weights(suc_raw, ids)

        log.info(
            "Baserunner calibration complete: %d profiles, sigma_speed=%.3f, "
            "sigma_agg=%.3f, sigma_suc=%.3f, eb_prior=%.1f",
            len(rows),
            report.sigma_baserunner_speed,
            report.sigma_baserunner_aggression,
            report.sigma_baserunner_success,
            report.eb_n_prior_baserunner,
        )
        return report
