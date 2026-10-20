"""The pitcher engine's calibration: the command sigma, the prior and the arsenal anchor."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
from numpy.typing import NDArray

from db.introspect import duck_columns
from similarity.calibration.base import CalibratorBase
from similarity.calibration.fitting import calibrate_arsenal_gamma, calibrate_eb_prior
from similarity.calibration.report import CalibrationReport

log = logging.getLogger("similarity_calibration")


class PitcherCalibratorMixin(CalibratorBase):
    """The pitcher engine's part of ``SimilarityCalibrator``."""

    def _calibrate_pitcher_params(
        self,
        conn: Any,
        seasons: list[int],
        target: float,
        report: CalibrationReport,
    ) -> CalibrationReport:
        """Fit the command sigma and the prior over the pitcher profiles.

        The engine has two sub-scores: the arsenal (the W₂ distance, fitted by
        ``_calibrate_arsenal_params``) and the command (a kernel over
        ``COMMAND_FEATURES``). ``sigma_results`` stays 0.0: the engine has no
        results sub-score.
        """
        from similarity.engines.pitcher_similarity import COMMAND_FEATURES

        sl = ", ".join(str(s) for s in seasons)
        # A command column absent from the table is selected as NULL (then
        # ``or 0.0``), so a trimmed DuckDB file still fits instead of failing.
        try:
            _present = duck_columns(conn, "derived", "pitcher_season_metrics")
        except Exception:  # noqa: BLE001 - assume all present if introspection fails
            _present = set(COMMAND_FEATURES)

        cmd_cols = ", ".join(f if f in _present else f"NULL AS {f}" for f in COMMAND_FEATURES)
        rows = conn.execute(f"""
            SELECT
                pitcher_id, season, sample_pitches,
                {cmd_cols}
            FROM derived.pitcher_season_metrics
            WHERE season IN ({sl}) AND below_minimum_sample = FALSE
        """).fetchall()

        if not rows:
            log.warning("No pitcher profiles found for calibration.")
            return report

        samples = np.array([r[2] for r in rows])

        n_cmd = len(COMMAND_FEATURES)
        cmd_raw = np.array([[r[3 + i] or 0.0 for i in range(n_cmd)] for r in rows])

        cmd_z = self._zscore_matrix(cmd_raw)
        report.sigma_command = self._fit_sigma(cmd_z, target)

        report.eb_n_prior_pitcher = calibrate_eb_prior(cmd_raw, samples, min_sample=200)

        log.info(
            "Pitcher calibration complete: %d profiles, sigma_cmd=%.3f, eb_prior=%.1f",
            len(rows),
            report.sigma_command,
            report.eb_n_prior_pitcher,
        )
        return report

    def _calibrate_arsenal_params(
        self,
        arsenal_w2_distances: NDArray[np.float64] | None,
        target: float,
        report: CalibrationReport,
    ) -> CalibrationReport:
        """Fit the arsenal anchor: the W₂ distance that scores ``target``.

        The fit solves ``arsenal_gamma`` (the coefficient of
        ``exp(-gamma · W₂²)``) so the median pairwise W₂ scores ``target``, and
        records that median. The pitcher engine's ``apply_calibration`` turns
        the pair into its linear ``ARSENAL_SCALE``; both forms give the median
        pair the same score (the arsenal calibration, SIM-346).

        The W₂ distances come from the pitcher engine, not from DuckDB. With
        none passed, the two fields stay 0.0 and the engine keeps its default
        scale.
        """
        if arsenal_w2_distances is None:
            return report

        dists = np.asarray(arsenal_w2_distances, dtype=np.float64)
        finite = dists[np.isfinite(dists)]
        if finite.size == 0:
            log.warning("No finite arsenal W2 distances; leaving arsenal_gamma at 0.0.")
            return report

        report.arsenal_gamma = calibrate_arsenal_gamma(finite, target_median_score=target)
        report.arsenal_median_w2 = float(np.median(finite))
        log.info(
            "Arsenal calibration complete: %d distances, median_W2=%.4f, "
            "arsenal_gamma=%.5f (target median=%.2f).",
            finite.size,
            report.arsenal_median_w2,
            report.arsenal_gamma,
            target,
        )
        return report
