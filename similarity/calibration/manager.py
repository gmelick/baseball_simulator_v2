"""The manager engine's calibration: the usage, aggression and platoon sigmas."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
from numpy.typing import NDArray

from similarity.calibration.base import CalibratorBase
from similarity.calibration.report import CalibrationReport

log = logging.getLogger("similarity_calibration")


class ManagerCalibratorMixin(CalibratorBase):
    """The manager engine's part of ``SimilarityCalibrator``."""

    def _calibrate_manager_params(
        self,
        conn: Any,
        seasons: list[int],
        target: float,
        report: CalibrationReport,
    ) -> CalibrationReport:
        """Fit the manager engine's usage, aggression and platoon sigmas."""
        sl = ", ".join(str(s) for s in seasons)
        try:
            rows = conn.execute(f"""
                SELECT
                    starter_avg_pitch_count, starter_pull_pct_before_100,
                    closer_entry_leverage_index, high_leverage_reliever_rate,
                    opener_usage_rate, bulk_innings_rate,
                    available_reliever_usage_rate,
                    steal_order_rate_per_1b_opp, hit_and_run_rate_per_opportunity,
                    sac_bunt_rate_high_leverage, sac_bunt_rate_low_leverage,
                    squeeze_play_rate_per_3b_opp,
                    pinch_hit_rate_vs_same_hand, pinch_hit_rate_high_leverage,
                    defensive_sub_rate_late_innings, double_switch_rate_per_reliever_change,
                    platoon_advantage_exploitation_rate
                FROM derived.manager_season_metrics
                WHERE NOT below_minimum_sample AND season IN ({sl})
            """).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("Manager calibration skipped (%s: %s).", type(exc).__name__, exc)
            return report
        if not rows:
            log.warning("No manager profiles found for calibration.")
            return report

        def col(start: int, n: int) -> NDArray[np.float64]:
            return self._zscore_matrix(
                np.array([[(r[start + i] or 0.0) for i in range(n)] for r in rows], dtype=float)
            )

        # The usage block is the first seven columns, the aggression block the
        # next five, the platoon block the last five. A usage block that is all
        # NULL (a DuckDB file built before the real-manager data, SIM-427) has
        # no spread, so its sigma is the 0.0 sentinel and the engine keeps
        # RBF_SIGMA_USAGE.
        report.sigma_manager_usage = self._fit_sigma(col(0, 7), target)
        report.sigma_manager_aggression = self._fit_sigma(col(7, 5), target)
        report.sigma_manager_platoon = self._fit_sigma(col(12, 5), target)
        log.info(
            "Manager calibration complete: %d profiles, sigma_usage=%.3f, "
            "sigma_aggression=%.3f, sigma_platoon=%.3f",
            len(rows),
            report.sigma_manager_usage,
            report.sigma_manager_aggression,
            report.sigma_manager_platoon,
        )
        return report
