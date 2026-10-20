"""The pitcher steal-prevention engine's calibration: the outcome and hold sigmas."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from similarity.calibration.base import CalibratorBase
from similarity.calibration.report import CalibrationReport

log = logging.getLogger("similarity_calibration")


class PitcherStealCalibratorMixin(CalibratorBase):
    """The pitcher steal-prevention engine's part of ``SimilarityCalibrator``."""

    def _calibrate_pitcher_steal_params(
        self,
        conn: Any,
        seasons: list[int],
        target: float,
        report: CalibrationReport,
    ) -> CalibrationReport:
        """Fit the pitcher steal-prevention engine's outcome and hold sigmas.

        The hold sigma (the lead and the jump the pitcher allows, SIM-531) fits
        over the rows that carry both; with none it stays 0.0.
        """
        sl = ", ".join(str(s) for s in seasons)
        hold_cols = (
            "lead_allowed_primary_ft, lead_allowed_jump_ft"
            if self._has_columns(
                conn,
                "pitcher_steal_metrics",
                ("lead_allowed_primary_ft", "lead_allowed_jump_ft"),
            )
            else "NULL AS lead_allowed_primary_ft, NULL AS lead_allowed_jump_ft"
        )
        try:
            rows = conn.execute(f"""
                SELECT sb_against_per_9, cs_rate_forced, steal_attempt_rate_allowed,
                       {hold_cols}
                FROM derived.pitcher_steal_metrics
                WHERE NOT below_minimum_sample AND season IN ({sl})
            """).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("Pitcher-steal calibration skipped (%s: %s).", type(exc).__name__, exc)
            return report
        if not rows:
            log.warning("No pitcher-steal profiles found for calibration.")
            return report

        out = self._zscore_matrix(
            np.array([[(r[0] or 0.0), (r[1] or 0.0), (r[2] or 0.0)] for r in rows])
        )
        report.sigma_pitcher_steal_outcome = self._fit_sigma(out, target)
        hold_rows = np.array(
            [[float(r[3]), float(r[4])] for r in rows if r[3] is not None and r[4] is not None],
            dtype=np.float64,
        ).reshape(-1, 2)
        report.sigma_pitcher_steal_hold = (
            self._fit_sigma(self._zscore_matrix(hold_rows), target) if len(hold_rows) else 0.0
        )
        log.info(
            "Pitcher-steal calibration complete: %d profiles, sigma_outcome=%.3f, "
            "sigma_hold=%.3f (%d rows with a lead allowed)",
            len(rows),
            report.sigma_pitcher_steal_outcome,
            report.sigma_pitcher_steal_hold,
            len(hold_rows),
        )
        return report
