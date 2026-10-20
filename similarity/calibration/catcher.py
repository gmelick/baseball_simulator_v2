"""The catcher engine's calibration: its four defensive sigmas."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
from numpy.typing import NDArray

from similarity.calibration.base import CalibratorBase
from similarity.calibration.report import CalibrationReport

log = logging.getLogger("similarity_calibration")


class CatcherCalibratorMixin(CalibratorBase):
    """The catcher engine's part of ``SimilarityCalibrator``."""

    def _calibrate_catcher_params(
        self,
        conn: Any,
        seasons: list[int],
        target: float,
        report: CalibrationReport,
    ) -> CalibrationReport:
        """Fit the catcher engine's four defensive sigmas.

        Mirrors ``catcher_similarity._load_profiles`` exactly (the same derived
        framing / blocking expressions) so each sigma is fit over the SAME
        feature space the engine scores in.  Guarded: a missing
        ``derived.catcher_season_metrics`` leaves the four sigmas at 0.0 (the
        engine keeps its module defaults) rather than aborting the whole fit.
        """
        sl = ", ".join(str(s) for s in seasons)
        try:
            rows = conn.execute(f"""
                SELECT
                    (csm.called_strikes - csm.expected_called_strikes)
                        / NULLIF(csm.called_pitches, 0)                          AS strike_rate_vs_expected,
                    csm.framing_runs                                             AS runs_saved_framing,
                    csm.shadow_zone_strike_rate,
                    csm.heart_zone_strike_rate,
                    csm.blocks_above_average / NULLIF(csm.expected_pbwp, 0)      AS block_success_rate,
                    csm.actual_pbwp * 1.0 / NULLIF(csm.pitches_received_total, 0) AS passed_ball_rate,
                    COALESCE(csm.blocks_aa_bounced, 0)
                        / NULLIF(csm.expected_pbwp, 0)                           AS wild_pitch_prevention_rate,
                    csm.pop_time_mean,
                    csm.cs_rate,
                    csm.arm_strength_mean,
                    csm.steal_attempt_rate_against
                FROM derived.catcher_season_metrics csm
                WHERE NOT csm.below_minimum_sample AND csm.season IN ({sl})
            """).fetchall()
        except Exception as exc:  # noqa: BLE001 - missing table -> keep defaults
            log.warning("Catcher calibration skipped (%s: %s).", type(exc).__name__, exc)
            return report
        if not rows:
            log.warning("No catcher profiles found for calibration.")
            return report

        def col(start: int, n: int) -> NDArray[np.float64]:
            return self._zscore_matrix(
                np.array([[(r[start + i] or 0.0) for i in range(n)] for r in rows], dtype=float)
            )

        report.sigma_catcher_framing = self._fit_sigma(col(0, 4), target)
        report.sigma_catcher_blocking = self._fit_sigma(col(4, 3), target)
        report.sigma_catcher_throwing = self._fit_sigma(col(7, 3), target)
        report.sigma_catcher_deterrence = self._fit_sigma(col(10, 1), target)
        log.info(
            "Catcher calibration complete: %d profiles, sigma_framing=%.3f, "
            "sigma_blocking=%.3f, sigma_throwing=%.3f, sigma_deterrence=%.3f",
            len(rows),
            report.sigma_catcher_framing,
            report.sigma_catcher_blocking,
            report.sigma_catcher_throwing,
            report.sigma_catcher_deterrence,
        )
        return report
