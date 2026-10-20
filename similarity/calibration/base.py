"""The helpers every per-engine calibrator mixin shares."""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import NDArray

from db.introspect import duck_columns
from similarity.calibration.fitting import calibrate_sigma


class CalibratorBase:
    """The column probe, the z-scoring and the sigma fit of the calibrator.

    ``SimilarityCalibrator`` and each of its per-engine mixins inherit these
    three static methods, so a mixin calls them as ``self._fit_sigma(...)``.
    """

    _duckdb_path: str

    @staticmethod
    def _has_columns(conn: Any, table: str, columns: tuple[str, ...]) -> bool:
        """Whether ``derived.<table>`` carries every one of ``columns``.

        The engines use the same probe, so a database that has not run a
        migration still calibrates the groups it has (the running-game data,
        SIM-531).
        """
        try:
            present = duck_columns(conn, "derived", table)
        except Exception:  # noqa: BLE001
            return False
        return set(columns) <= present

    @staticmethod
    def _zscore_matrix(mat: NDArray[np.float64]) -> NDArray[np.float64]:
        """Z-score each column, NaN-aware; a column with no spread divides by 1.0."""
        m = np.nanmean(mat, axis=0)
        s = np.nanstd(mat, axis=0)
        s[s == 0] = 1.0
        return (mat - m) / s

    @staticmethod
    def _fit_sigma(zmatrix: NDArray[np.float64], target: float) -> float:
        """Fit the RBF sigma, or return 0.0 when the matrix cannot give one.

        0.0 is the engines' keep-the-default sentinel: ``apply_calibration``
        applies a sigma only when it is above zero. A sub-score the profile
        build leaves all NULL, or mostly constant (more than half the sampled
        pairs identical), has no pairwise spread, and ``calibrate_sigma`` would
        return its default 1.0. The engine would then apply 1.0 as a real
        override over a tuned default (the baserunner's speed sigma is
        0.8171), so this wrapper passes ``degenerate_value=0.0`` (the
        calibration reconciliation, SIM-432). An empty matrix also returns 0.0.
        """
        if zmatrix.size == 0:
            return 0.0
        return calibrate_sigma(zmatrix, target_median_score=target, degenerate_value=0.0)
