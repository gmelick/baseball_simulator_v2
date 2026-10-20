"""The fielder engine's calibration: the infield and outfield sigmas and reliability weights."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
from numpy.typing import NDArray

from similarity.calibration.base import CalibratorBase
from similarity.calibration.fitting import (
    calibrate_reliability_weights,
    keep_defaults_on_thin_entries,
)
from similarity.calibration.report import CalibrationReport
from similarity.engines.fielder_similarity import IF_PIVOT_FEATURES

log = logging.getLogger("similarity_calibration")


#: The scorer's position number, used to build one pair id per player and position.
_FIELDER_POSITION_CODES = {"1B": 3, "2B": 4, "3B": 5, "SS": 6, "LF": 7, "CF": 8, "RF": 9}


def _nan_block(rows: list[tuple], start: int, width: int) -> NDArray[np.float64]:
    """``width`` columns of every row from ``start`` on, NULL as NaN.

    The calibrator reads its blocks by index. A block whose columns can be NULL
    (the range block, the arm block) must not map NULL to 0.0: that would put
    every unmeasured fielder on one point.
    """
    return np.array(
        [
            [np.nan if r[start + i] is None else float(r[start + i]) for i in range(width)]
            for r in rows
        ],
        dtype=np.float64,
    )


def _fielder_pair_ids(rows: list[tuple]) -> NDArray[np.int64]:
    """The id the reliability fit pairs on: one per player AND position.

    The fielder table holds one row per (player, position, season). A
    player-only id would pair a centre fielder's row with his own right-field
    row (the outfield arm block, SIM-550). ``rows`` are the calibrator's SELECT
    rows: player_id first, position second.
    """
    return np.array(
        [int(r[0]) * 10 + _FIELDER_POSITION_CODES.get(str(r[1]), 0) for r in rows],
        dtype=np.int64,
    )


class FielderCalibratorMixin(CalibratorBase):
    """The fielder engine's part of ``SimilarityCalibrator``."""

    def _calibrate_fielder_params(
        self,
        conn: Any,
        seasons: list[int],
        target_median_score: float,
        report: CalibrationReport,
    ) -> CalibrationReport:
        """Fit the fielder engine's sigmas and reliability weights.

        The infield (1B, 2B, 3B, SS) and the outfield (LF, CF, RF) fit apart,
        one sigma and one weight vector per sub-score. A reliability pair is two
        consecutive seasons of one player at one position.
        """
        from similarity.engines.fielder_similarity import (
            ARM_ALPHA_PRIOR_CHANCES,
            IF_DP_FEATURES,
            IF_ERROR_FEATURES,
            IF_RANGE_FEATURES,
            IF_SPECIALTY_FEATURES,
            JUMP_ALPHA_PRIOR_PLAYS,
            OF_ARM_FEATURES,
            OF_ERROR_FEATURES,
            OF_RANGE_BASE_COUNT,
            OF_RANGE_FEATURES,
        )

        sl = ", ".join(str(s) for s in seasons)

        # The infield range block is IF_RANGE_FEATURES' six columns, in order:
        # our five, then Savant's outs above average at the position per 100 of
        # our chances (the Savant fielding data, SIM-532).
        if_rows = conn.execute(f"""
            SELECT
                player_id, position, season, sample_batted_balls,
                oaa_glove_side, oaa_arm_side, oaa_charging, oaa_deep, catch_pct_added,
                savant_oaa_per_100,
                fielding_error_rate, throwing_error_rate,
                dp_above_expected, dp_attempt_rate, dp_success_rate,
                dp_pivot_above_expected,
                bunt_fielding_rate, scoop_success_rate
            FROM derived.fielder_season_metrics
            WHERE season IN ({sl})
              AND position IN ('1B','2B','3B','SS')
              AND NOT below_minimum_sample
            ORDER BY player_id, position, season
        """).fetchall()

        # The outfield blocks sit in the engine's feature order, because the
        # index arithmetic below walks them by the feature lists' lengths:
        # - range: OF_RANGE_FEATURES' nine columns, our five, Savant's figure,
        #   then the three jump parts (reaction, burst, route);
        # - errors;
        # - arm: OF_ARM_FEATURES' three columns, the throw velocity, the
        #   advancement prevention, the thrown-out rate;
        # - the six star-catch counts.
        # The two count columns come last: the arm's chances, then the jump's
        # plays. They floor the arm and jump reliability pairs, and the code
        # reads them by an index computed from the block lengths.
        of_rows = conn.execute(f"""
            SELECT
                player_id, position, season, sample_batted_balls,
                oaa_glove_side, oaa_arm_side, oaa_charging, oaa_deep, catch_pct_added,
                savant_oaa_per_100, jump_reaction_ft, jump_burst_ft, jump_route_ft,
                fielding_error_rate, throwing_error_rate,
                arm_strength, arm_advancement_prevention, arm_thrown_out_rate,
                five_star_opps, five_star_catches, four_star_opps, four_star_catches,
                routine_opps, routine_catches,
                arm_opportunities, jump_plays
            FROM derived.fielder_season_metrics
            WHERE season IN ({sl})
              AND position IN ('LF','CF','RF')
              AND NOT below_minimum_sample
            ORDER BY player_id, position, season
        """).fetchall()

        # The infield.
        if if_rows:
            n_range = len(IF_RANGE_FEATURES)
            n_err = len(IF_ERROR_FEATURES)
            n_dp = len(IF_DP_FEATURES)
            n_pivot = len(IF_PIVOT_FEATURES)
            n_spec = len(IF_SPECIALTY_FEATURES)

            # The range block keeps NULL as NaN: Savant's figure is NULL on a
            # fielder-season its board does not cover, and 0.0 would put him at
            # league average. The sigma fits over the rows that carry every
            # range feature; the reliability fit drops NaN pairs per feature.
            range_raw = _nan_block(if_rows, 4, n_range)
            err_raw = np.array([[r[4 + n_range + i] or 0.0 for i in range(n_err)] for r in if_rows])
            dp_raw = np.array(
                [[r[4 + n_range + n_err + i] or 0.0 for i in range(n_dp)] for r in if_rows]
            )
            pivot_raw = np.array(
                [
                    [r[4 + n_range + n_err + n_dp + i] or 0.0 for i in range(n_pivot)]
                    for r in if_rows
                ]
            )
            spec_raw = np.array(
                [
                    [r[4 + n_range + n_err + n_dp + n_pivot + i] or 0.0 for i in range(n_spec)]
                    for r in if_rows
                ]
            )

            ids = _fielder_pair_ids(if_rows)
            season_of = np.array([int(r[2]) for r in if_rows], dtype=np.int64)

            # Under 20 fully measured rows, the range sigma keeps the engine
            # default (the 0.0 sentinel), as the batter's physical block does.
            range_measured = range_raw[np.isfinite(range_raw).all(axis=1)]
            if len(range_measured) >= 20:
                report.sigma_if_range = self._fit_sigma(
                    self._zscore_matrix(range_measured), target_median_score
                )
            else:
                log.warning(
                    "Only %d infielder-seasons carry every range feature; keeping the "
                    "engine's default infield range sigma. Load the Savant outs-above-"
                    "average board (SIM-532) and rebuild the fielder profiles.",
                    len(range_measured),
                )
            report.sigma_if_dp = self._fit_sigma(
                self._zscore_matrix(np.concatenate((dp_raw, pivot_raw), axis=1)),
                target_median_score,
            )
            report.sigma_if_errors = self._fit_sigma(
                self._zscore_matrix(err_raw), target_median_score
            )
            report.sigma_if_specialty = self._fit_sigma(
                self._zscore_matrix(spec_raw), target_median_score
            )

            # The range weights fit over the whole matrix, so our five
            # components keep every pair and Savant's figure fits on the pairs
            # measured in both seasons. Then Savant's entry (the last) keeps its
            # engine default when fewer than RELIABILITY_MIN_PAIRS pairs carry it.
            savant_col = n_range - 1
            report.reliability_weights_if_range = keep_defaults_on_thin_entries(
                calibrate_reliability_weights(range_raw, ids, seasons=season_of),
                IF_RANGE_FEATURES,
                range_raw,
                ids,
                season_of,
                {savant_col: np.ones(len(if_rows), dtype=bool)},
                "infield",
            )
            report.reliability_weights_if_dp = calibrate_reliability_weights(
                dp_raw, ids, seasons=season_of
            )
            report.reliability_weights_if_pivot = calibrate_reliability_weights(
                pivot_raw, ids, seasons=season_of
            )
            report.reliability_weights_if_error = calibrate_reliability_weights(
                err_raw, ids, seasons=season_of
            )
            report.reliability_weights_if_specialty = calibrate_reliability_weights(
                spec_raw, ids, seasons=season_of
            )

            log.info(
                "IF calibration: %d profiles, sigma_range=%.3f, sigma_dp=%.3f, "
                "sigma_err=%.3f, sigma_spec=%.3f",
                len(if_rows),
                report.sigma_if_range,
                report.sigma_if_dp,
                report.sigma_if_errors,
                report.sigma_if_specialty,
            )

        # The outfield.
        if of_rows:
            n_range = len(OF_RANGE_FEATURES)
            n_err = len(OF_ERROR_FEATURES)
            n_arm = len(OF_ARM_FEATURES)

            # The range block keeps NULL as NaN: about a third of the
            # outfielder-seasons carry a jump row, and Savant's figure starts in
            # 2016. The sigma fits over the rows that carry all nine (about 200
            # a season); the reliability fit takes the whole matrix.
            range_raw = _nan_block(of_rows, 4, n_range)
            err_raw = np.array([[r[4 + n_range + i] or 0.0 for i in range(n_err)] for r in of_rows])
            # The arm block keeps NULL as NaN too: a missing measurement is not
            # a zero arm, and 0.0 would put the unmeasured rows on one point.
            # The sigma fits over the rows that carry all three features: a
            # 2023+ outfielder with a velocity on the arm-strength board and at
            # least one challenge in the advancement pool (the thrown-out rate
            # is NULL until a runner challenges him).
            arm_raw = np.array(
                [
                    [
                        np.nan
                        if r[4 + n_range + n_err + i] is None
                        else float(r[4 + n_range + n_err + i])
                        for i in range(n_arm)
                    ]
                    for r in of_rows
                ],
                dtype=np.float64,
            )
            arm_mask = np.isfinite(arm_raw).all(axis=1)
            arm_measured = arm_raw[arm_mask]

            # The catch rate on five-star, four-star and routine chances.
            star_raw = []
            for r in of_rows:
                base = 4 + n_range + n_err + n_arm
                f5o, f5c = r[base], r[base + 1]
                f4o, f4c = r[base + 2], r[base + 3]
                ro, rc = r[base + 4], r[base + 5]
                star_raw.append(
                    [
                        (f5c / f5o) if f5o and f5o > 0 else 0.0,
                        (f4c / f4o) if f4o and f4o > 0 else 0.0,
                        (rc / ro) if ro and ro > 0 else 0.0,
                    ]
                )
            star_raw = np.array(star_raw)

            ids = _fielder_pair_ids(of_rows)
            season_of = np.array([int(r[2]) for r in of_rows], dtype=np.int64)
            # The two count columns sit after the six star counts: the arm's
            # chances first, the jump's plays second.
            count_base = 4 + n_range + n_err + n_arm + 6
            arm_chances = np.array(
                [0.0 if r[count_base] is None else float(r[count_base]) for r in of_rows],
                dtype=np.float64,
            )
            jump_plays = np.array(
                [0.0 if r[count_base + 1] is None else float(r[count_base + 1]) for r in of_rows],
                dtype=np.float64,
            )
            # An arm reliability pair needs ARM_ALPHA_PRIOR_CHANCES chances in
            # BOTH seasons (a pair needs both rows, so a row floor is a pair
            # floor). The sigma fit keeps every measured row.
            arm_reliable = arm_mask & (arm_chances >= ARM_ALPHA_PRIOR_CHANCES)

            # Under 20 fully measured rows, the range sigma keeps the engine
            # default (the 0.0 sentinel).
            range_measured = range_raw[np.isfinite(range_raw).all(axis=1)]
            if len(range_measured) >= 20:
                report.sigma_of_range = self._fit_sigma(
                    self._zscore_matrix(range_measured), target_median_score
                )
            else:
                log.warning(
                    "Only %d outfielder-seasons carry every range feature; keeping the "
                    "engine's default outfield range sigma. Load the Savant outs-above-"
                    "average and outfield-jump boards (SIM-532) and rebuild the fielder "
                    "profiles.",
                    len(range_measured),
                )
            if len(arm_measured) >= 20:
                report.sigma_of_arm = self._fit_sigma(
                    self._zscore_matrix(arm_measured), target_median_score
                )
            else:
                log.warning(
                    "Only %d outfielder-seasons carry the arm block; keeping the "
                    "engine's default arm sigma.",
                    len(arm_measured),
                )
            report.sigma_of_stars = self._fit_sigma(
                self._zscore_matrix(star_raw), target_median_score
            )
            report.sigma_of_errors = self._fit_sigma(
                self._zscore_matrix(err_raw), target_median_score
            )

            # The range weights fit each feature on the pairs that carry it.
            # Our five components keep every pair; Savant's figure pairs where
            # both seasons carry it. The three jump parts pair only where BOTH
            # seasons carry JUMP_ALPHA_PRIOR_PLAYS or more plays: below the
            # floor the jump columns are set to NaN before the fit, so the row
            # still pairs on the other six. Then each of Savant's figure and the
            # jump parts keeps its engine default when fewer than
            # RELIABILITY_MIN_PAIRS pairs carry it.
            jump_ok = jump_plays >= JUMP_ALPHA_PRIOR_PLAYS
            range_for_fit = range_raw.copy()
            range_for_fit[~jump_ok, OF_RANGE_BASE_COUNT:] = np.nan
            every_row = np.ones(len(of_rows), dtype=bool)
            new_entries = {OF_RANGE_BASE_COUNT - 1: every_row}
            for j in range(OF_RANGE_BASE_COUNT, n_range):
                new_entries[j] = jump_ok
            report.reliability_weights_of_range = keep_defaults_on_thin_entries(
                calibrate_reliability_weights(range_for_fit, ids, seasons=season_of),
                OF_RANGE_FEATURES,
                range_raw,
                ids,
                season_of,
                new_entries,
                "outfield",
            )
            if arm_reliable.sum() >= 20:
                report.reliability_weights_of_arm = calibrate_reliability_weights(
                    arm_raw[arm_reliable], ids[arm_reliable], seasons=season_of[arm_reliable]
                )
            elif len(arm_measured) >= 20:
                log.warning(
                    "Only %d outfielder-seasons carry %d or more arm chances; keeping the "
                    "engine's default arm reliability weights.",
                    int(arm_reliable.sum()),
                    ARM_ALPHA_PRIOR_CHANCES,
                )
            report.reliability_weights_of_star = calibrate_reliability_weights(
                star_raw, ids, seasons=season_of
            )
            report.reliability_weights_of_error = calibrate_reliability_weights(
                err_raw, ids, seasons=season_of
            )

            log.info(
                "OF calibration: %d profiles, sigma_range=%.3f, sigma_arm=%.3f, "
                "sigma_stars=%.3f, sigma_err=%.3f",
                len(of_rows),
                report.sigma_of_range,
                report.sigma_of_arm,
                report.sigma_of_stars,
                report.sigma_of_errors,
            )

        return report
