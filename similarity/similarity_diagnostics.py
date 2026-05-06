"""
similarity_diagnostics.py
==========================
Sanity-check suite for similarity engine output.

Validates that all sub-scores and the composite score are properly
distributed across the population, detects pathological patterns
(collapse, inflation, asymmetry, dimensional bias), and produces
an actionable report.

Can run against:
  1. A live BatterSimilarityEngine / PitcherSimilarityEngine instance
  2. Synthetic data (for testing the diagnostic itself)

Checks performed
----------------
  Distribution:
    - Per-sub-score and composite: mean, median, std, min, max, percentiles
    - Flags sub-scores where median < 0.05 (collapsed) or > 0.95 (inflated)
    - Flags sub-scores where std < 0.05 (no discrimination)

  Dimensional balance:
    - Compares medians across sub-scores — they should be in the same
      ballpark if dimensionality normalization is working correctly
    - Flags if any sub-score median deviates from the overall median by > 2×

  Cross-season self-similarity:
    - For players with multiple seasons, checks that cross-season
      self-pairs score higher than the population median
    - Reports the fraction that do and flags if < 70%

  Score symmetry:
    - Spot-checks that score(A, B) ≈ score(B, A) for random pairs
    - Flags if any asymmetry > 1e-6

  NaN / Inf detection:
    - Checks all scores for non-finite values

Usage
-----
  # Against a live engine:
  from batter_similarity import BatterSimilarityEngine
  from similarity_diagnostics import run_batter_diagnostics

  engine = BatterSimilarityEngine(duckdb_path="...")
  engine.build(seasons=[2022, 2023, 2024])
  report = run_batter_diagnostics(engine, n_query_samples=50)
  print(report)

  # Against a live pitcher engine:
  from pitcher_similarity import PitcherSimilarityEngine
  from similarity_diagnostics import run_pitcher_diagnostics

  engine = PitcherSimilarityEngine(duckdb_path="...")
  engine.build(seasons=[2022, 2023, 2024])
  report = run_pitcher_diagnostics(engine, n_query_samples=50)
  print(report)

  # Standalone test with synthetic data:
  python similarity_diagnostics.py
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

log = logging.getLogger("similarity_diagnostics")

# Thresholds for flagging problems
MEDIAN_COLLAPSED_THRESHOLD = 0.05    # sub-score median below this = collapsed
MEDIAN_INFLATED_THRESHOLD  = 0.95    # sub-score median above this = inflated
STD_NO_DISCRIMINATION      = 0.05   # sub-score std below this = no spread
MEDIAN_BALANCE_RATIO       = 2.5    # max ratio between any sub-score median and overall
CROSS_SEASON_PASS_RATE     = 0.70   # fraction of cross-season pairs above pop median
SYMMETRY_TOLERANCE         = 1e-6   # max allowed asymmetry in score(A,B) vs score(B,A)


# ============================================================================
# Distribution Statistics
# ============================================================================

@dataclass
class ScoreDistribution:
    """Statistics for one score dimension."""
    name: str
    n_values: int = 0
    mean: float = 0.0
    median: float = 0.0
    std: float = 0.0
    min: float = 0.0
    max: float = 0.0
    p5: float = 0.0
    p25: float = 0.0
    p75: float = 0.0
    p95: float = 0.0
    n_nan: int = 0
    n_inf: int = 0
    n_zero: int = 0          # exactly 0.0
    n_one: int = 0            # exactly 1.0
    n_below_01: int = 0       # < 0.1
    n_above_09: int = 0       # > 0.9

    # Flags
    is_collapsed: bool = False
    is_inflated: bool = False
    is_no_discrimination: bool = False

    @classmethod
    def from_array(cls, name: str, values: NDArray[np.float64]) -> ScoreDistribution:
        d = cls(name=name)
        if len(values) == 0:
            return d

        d.n_values = len(values)
        d.n_nan = int(np.isnan(values).sum())
        d.n_inf = int(np.isinf(values).sum())

        # Filter to finite values for stats
        finite = values[np.isfinite(values)]
        if len(finite) == 0:
            return d

        d.mean = float(np.mean(finite))
        d.median = float(np.median(finite))
        d.std = float(np.std(finite))
        d.min = float(np.min(finite))
        d.max = float(np.max(finite))
        d.p5 = float(np.percentile(finite, 5))
        d.p25 = float(np.percentile(finite, 25))
        d.p75 = float(np.percentile(finite, 75))
        d.p95 = float(np.percentile(finite, 95))

        d.n_zero = int(np.sum(finite == 0.0))
        d.n_one = int(np.sum(finite == 1.0))
        d.n_below_01 = int(np.sum(finite < 0.1))
        d.n_above_09 = int(np.sum(finite > 0.9))

        # Flag checks
        d.is_collapsed = d.median < MEDIAN_COLLAPSED_THRESHOLD
        d.is_inflated = d.median > MEDIAN_INFLATED_THRESHOLD
        d.is_no_discrimination = d.std < STD_NO_DISCRIMINATION

        return d

    def format_row(self) -> str:
        flags = []
        if self.is_collapsed:
            flags.append("COLLAPSED")
        if self.is_inflated:
            flags.append("INFLATED")
        if self.is_no_discrimination:
            flags.append("NO_SPREAD")
        if self.n_nan > 0:
            flags.append(f"NaN:{self.n_nan}")
        if self.n_inf > 0:
            flags.append(f"Inf:{self.n_inf}")
        flag_str = f"  !! {', '.join(flags)}" if flags else ""

        return (
            f"  {self.name:<16} "
            f"med={self.median:>6.3f}  mean={self.mean:>6.3f}  std={self.std:>6.3f}  "
            f"[{self.min:>5.3f}, {self.max:>5.3f}]  "
            f"p5={self.p5:>5.3f}  p95={self.p95:>5.3f}  "
            f"<.1={self.n_below_01:>4}  >.9={self.n_above_09:>4}"
            f"{flag_str}"
        )


# ============================================================================
# Diagnostic Report
# ============================================================================

@dataclass
class DiagnosticReport:
    """Full diagnostic output for one engine."""
    engine_type: str = ""                               # "batter" or "pitcher"
    n_profiles: int = 0
    n_queries_sampled: int = 0
    n_total_scores: int = 0
    elapsed_sec: float = 0.0

    # Per-dimension distributions
    distributions: list[ScoreDistribution] = field(default_factory=list)

    # Dimensional balance
    median_balance_ok: bool = True
    median_balance_detail: str = ""

    # Cross-season self-similarity
    n_cross_season_pairs: int = 0
    cross_season_above_median_frac: float = 0.0
    cross_season_mean_score: float = 0.0
    cross_season_ok: bool = True

    # Symmetry
    n_symmetry_checks: int = 0
    max_asymmetry: float = 0.0
    symmetry_ok: bool = True

    # Overall
    n_flags: int = 0

    def __str__(self) -> str:
        lines = [
            "=" * 90,
            f"SIMILARITY DIAGNOSTICS — {self.engine_type.upper()} ENGINE",
            "=" * 90,
            f"Profiles: {self.n_profiles}  |  Queries sampled: {self.n_queries_sampled}  |  "
            f"Total scores: {self.n_total_scores:,}  |  Time: {self.elapsed_sec:.1f}s",
            "",
            "--- Score Distributions ---",
            f"  {'Name':<16} {'med':>9}  {'mean':>9}  {'std':>9}  "
            f"{'[min, max]':>14}  {'p5':>8}  {'p95':>8}  "
            f"{'<.1':>6}  {'>.9':>6}",
            "-" * 90,
        ]

        for d in self.distributions:
            lines.append(d.format_row())

        lines.append("")

        # Dimensional balance
        lines.append("--- Dimensional Balance ---")
        if self.median_balance_ok:
            lines.append("  OK: All sub-score medians are within acceptable range of each other.")
        else:
            lines.append(f"  !! IMBALANCED: {self.median_balance_detail}")
        lines.append("")

        # Cross-season
        lines.append("--- Cross-Season Self-Similarity ---")
        if self.n_cross_season_pairs > 0:
            pct = self.cross_season_above_median_frac * 100
            lines.append(
                f"  {self.n_cross_season_pairs} cross-season pairs found.  "
                f"Mean score: {self.cross_season_mean_score:.4f}  |  "
                f"Above pop median: {pct:.1f}%"
            )
            if self.cross_season_ok:
                lines.append("  OK: Cross-season self-pairs score above population median at acceptable rate.")
            else:
                lines.append(
                    f"  !! FAIL: Only {pct:.1f}% of cross-season self-pairs beat the population "
                    f"median (threshold: {CROSS_SEASON_PASS_RATE * 100:.0f}%)."
                )
        else:
            lines.append("  SKIPPED: No multi-season players found in sample.")
        lines.append("")

        # Symmetry
        lines.append("--- Score Symmetry ---")
        lines.append(
            f"  {self.n_symmetry_checks} pairs checked.  "
            f"Max asymmetry: {self.max_asymmetry:.2e}"
        )
        if self.symmetry_ok:
            lines.append("  OK: All checked pairs are symmetric within tolerance.")
        else:
            lines.append(
                f"  !! FAIL: Asymmetry {self.max_asymmetry:.2e} exceeds tolerance "
                f"{SYMMETRY_TOLERANCE:.2e}."
            )
        lines.append("")

        # Summary
        self.n_flags = sum(1 for d in self.distributions if d.is_collapsed or d.is_inflated or d.is_no_discrimination or d.n_nan > 0 or d.n_inf > 0)
        if not self.median_balance_ok:
            self.n_flags += 1
        if not self.cross_season_ok and self.n_cross_season_pairs > 0:
            self.n_flags += 1
        if not self.symmetry_ok:
            self.n_flags += 1

        lines.append("=" * 90)
        if self.n_flags == 0:
            lines.append("ALL CHECKS PASSED")
        else:
            lines.append(f"{self.n_flags} FLAG(S) RAISED — review items marked with !!")
        lines.append("=" * 90)

        return "\n".join(lines)


# ============================================================================
# Batter Diagnostics
# ============================================================================

def run_batter_diagnostics(
    engine: Any,
    n_query_samples: int = 50,
    seed: int = 42,
) -> DiagnosticReport:
    """
    Run full diagnostics on a built BatterSimilarityEngine.

    Samples n_query_samples random profiles, queries each against all
    others, and analyzes the distribution of all sub-scores and the
    composite.

    Parameters
    ----------
    engine : BatterSimilarityEngine
        Must have build() already called.
    n_query_samples : int
        Number of random profiles to use as query seeds. More = slower
        but more representative. 50 is usually sufficient.
    seed : int
        Random seed for reproducible sampling.
    """
    t0 = time.time()
    report = DiagnosticReport(engine_type="batter", n_profiles=engine.profile_count)

    all_ids = engine.profile_ids()
    if not all_ids:
        log.warning("Engine has no profiles. Cannot run diagnostics.")
        return report

    # Sample query profiles
    rng = np.random.default_rng(seed)
    n_samples = min(n_query_samples, len(all_ids))
    sample_indices = rng.choice(len(all_ids), size=n_samples, replace=False)
    sample_ids = [all_ids[i] for i in sample_indices]
    report.n_queries_sampled = n_samples

    # Collect all scores
    all_composite = []
    all_discipline = []
    all_batted_ball = []
    all_platoon = []
    all_power = []

    for batter_id, season in sample_ids:
        results = engine.query(batter_id, season)
        for r in results:
            all_composite.append(r.score)
            all_discipline.append(r.discipline_score)
            all_batted_ball.append(r.batted_ball_score)
            all_platoon.append(r.platoon_score)
            all_power.append(r.power_score)

    report.n_total_scores = len(all_composite)

    # Build distributions
    composite_arr = np.array(all_composite)
    sub_arrays = {
        "composite": composite_arr,
        "discipline": np.array(all_discipline),
        "batted_ball": np.array(all_batted_ball),
        "platoon": np.array(all_platoon),
        "power": np.array(all_power),
    }

    for name, arr in sub_arrays.items():
        report.distributions.append(ScoreDistribution.from_array(name, arr))

    # Dimensional balance check
    _check_dimensional_balance(report)

    # Cross-season self-similarity check
    _check_cross_season(engine, all_ids, composite_arr, report, entity_type="batter")

    # Symmetry check
    _check_symmetry_batter(engine, all_ids, rng, report)

    report.elapsed_sec = time.time() - t0
    return report


# ============================================================================
# Pitcher Diagnostics
# ============================================================================

def run_pitcher_diagnostics(
    engine: Any,
    n_query_samples: int = 50,
    seed: int = 42,
) -> DiagnosticReport:
    """
    Run full diagnostics on a built PitcherSimilarityEngine.
    """
    t0 = time.time()
    report = DiagnosticReport(engine_type="pitcher", n_profiles=engine.profile_count)

    all_ids = engine.profile_ids()
    if not all_ids:
        log.warning("Engine has no profiles. Cannot run diagnostics.")
        return report

    rng = np.random.default_rng(seed)
    n_samples = min(n_query_samples, len(all_ids))
    sample_indices = rng.choice(len(all_ids), size=n_samples, replace=False)
    sample_ids = [all_ids[i] for i in sample_indices]
    report.n_queries_sampled = n_samples

    all_composite = []
    all_arsenal = []
    all_command = []

    for pitcher_id, season in sample_ids:
        results = engine.query(pitcher_id, season)
        for r in results:
            all_composite.append(r.score)
            all_arsenal.append(r.arsenal_score)
            all_command.append(r.command_score)

    report.n_total_scores = len(all_composite)

    composite_arr = np.array(all_composite)
    sub_arrays = {
        "composite": composite_arr,
        "arsenal": np.array(all_arsenal),
        "command": np.array(all_command),
    }

    for name, arr in sub_arrays.items():
        report.distributions.append(ScoreDistribution.from_array(name, arr))

    _check_dimensional_balance(report)
    _check_cross_season(engine, all_ids, composite_arr, report, entity_type="pitcher")
    _check_symmetry_pitcher(engine, all_ids, rng, report)

    report.elapsed_sec = time.time() - t0
    return report


# ============================================================================
# Fielder Diagnostics
# ============================================================================

def run_fielder_diagnostics(
    engine: Any,
    n_query_samples: int = 50,
    seed: int = 42,
    position: str | None = None,
) -> DiagnosticReport:
    """
    Run full diagnostics on a built FielderSimilarityEngine.

    Samples n_query_samples random profiles per position (or for a
    specific position if provided), queries each against all same-position
    profiles, and analyzes the distribution of all sub-scores and the
    composite.

    Parameters
    ----------
    engine : FielderSimilarityEngine
        Must have build() already called.
    n_query_samples : int
        Number of random profiles per position to use as query seeds.
    seed : int
        Random seed for reproducible sampling.
    position : str or None
        If specified, only diagnose this position. Otherwise diagnose all.
    """
    t0 = time.time()
    report = DiagnosticReport(engine_type="fielder", n_profiles=engine.profile_count)

    all_ids = engine.profile_ids()
    if not all_ids:
        log.warning("Engine has no profiles. Cannot run diagnostics.")
        return report

    # Group by position
    if position:
        positions_to_check = [position]
    else:
        positions_to_check = sorted(set(k[1] for k in all_ids))

    rng = np.random.default_rng(seed)

    all_composite = []
    all_range = []
    all_secondary = []
    all_tertiary = []
    all_quaternary = []
    total_queries = 0

    for pos in positions_to_check:
        pos_ids = [k for k in all_ids if k[1] == pos]
        if not pos_ids:
            continue

        n_samples = min(n_query_samples, len(pos_ids))
        sample_indices = rng.choice(len(pos_ids), size=n_samples, replace=False)
        sample_ids = [pos_ids[i] for i in sample_indices]
        total_queries += n_samples

        for player_id, p, season in sample_ids:
            results = engine.query(player_id, p, season)
            for r in results:
                all_composite.append(r.score)
                all_range.append(r.range_score)
                all_secondary.append(r.secondary_score)
                all_tertiary.append(r.tertiary_score)
                all_quaternary.append(r.quaternary_score)

    report.n_queries_sampled = total_queries
    report.n_total_scores = len(all_composite)

    # Build distributions
    composite_arr = np.array(all_composite)
    sub_arrays = {
        "composite": composite_arr,
        "range": np.array(all_range),
        "secondary": np.array(all_secondary),
        "tertiary": np.array(all_tertiary),
        "quaternary": np.array(all_quaternary),
    }

    for name, arr in sub_arrays.items():
        report.distributions.append(ScoreDistribution.from_array(name, arr))

    # Dimensional balance check
    _check_dimensional_balance(report)

    # Cross-season self-similarity
    # For fielder, the key is (player_id, position, season) — group by (player_id, position)
    _check_cross_season_fielder(engine, all_ids, composite_arr, report)

    # Symmetry check
    _check_symmetry_fielder(engine, all_ids, rng, report)

    report.elapsed_sec = time.time() - t0
    return report


# ============================================================================
# Baserunner Diagnostics
# ============================================================================

def run_baserunner_diagnostics(
        engine: Any,
        n_query_samples: int = 50,
        seed: int = 42,
) -> DiagnosticReport:
    """
    Run full diagnostics on a built BaserunnerSimilarityEngine.

    Samples n_query_samples random profiles, queries each against all
    others, and analyzes the distribution of all sub-scores and the
    composite.

    Parameters
    ----------
    engine : BaserunnerSimilarityEngine
        Must have build() already called.
    n_query_samples : int
        Number of random profiles to use as query seeds.
    seed : int
        Random seed for reproducible sampling.
    """
    t0 = time.time()
    report = DiagnosticReport(engine_type="baserunner", n_profiles=engine.profile_count)

    all_ids = engine.profile_ids()
    if not all_ids:
        log.warning("Engine has no profiles. Cannot run diagnostics.")
        return report

    rng = np.random.default_rng(seed)
    n_samples = min(n_query_samples, len(all_ids))
    sample_indices = rng.choice(len(all_ids), size=n_samples, replace=False)
    sample_ids = [all_ids[i] for i in sample_indices]
    report.n_queries_sampled = n_samples

    all_composite = []
    all_speed = []
    all_aggression = []
    all_success = []

    for player_id, season in sample_ids:
        results = engine.query(player_id, season)
        for r in results:
            all_composite.append(r.score)
            all_speed.append(r.speed_score)
            all_aggression.append(r.aggression_score)
            all_success.append(r.success_score)

    report.n_total_scores = len(all_composite)

    composite_arr = np.array(all_composite)
    sub_arrays = {
        "composite": composite_arr,
        "speed": np.array(all_speed),
        "aggression": np.array(all_aggression),
        "success": np.array(all_success),
    }

    for name, arr in sub_arrays.items():
        report.distributions.append(ScoreDistribution.from_array(name, arr))

    _check_dimensional_balance(report)
    _check_cross_season(engine, all_ids, composite_arr, report, entity_type="baserunner")
    _check_symmetry_batter(engine, all_ids, rng, report)  # same (player_id, season) key shape

    report.elapsed_sec = time.time() - t0
    return report


# ============================================================================
# Shared Check Functions
# ============================================================================

def _check_dimensional_balance(report: DiagnosticReport) -> None:
    """
    Verify sub-score medians are in comparable ranges.

    If dimensionality normalization is working, no sub-score should have
    a median that is drastically different from the others. A sub-score
    with median 0.02 next to one with median 0.80 means the first is
    collapsed and contributes nothing to the composite.
    """
    sub_dists = [d for d in report.distributions if d.name != "composite"]
    if not sub_dists:
        return

    medians = [d.median for d in sub_dists]
    overall_median = np.median(medians)

    if overall_median <= 0:
        report.median_balance_ok = False
        report.median_balance_detail = f"Overall sub-score median is {overall_median:.4f} (≤ 0)"
        return

    problems = []
    for d in sub_dists:
        if overall_median > 0:
            ratio = d.median / overall_median
            if ratio > MEDIAN_BALANCE_RATIO or ratio < 1.0 / MEDIAN_BALANCE_RATIO:
                problems.append(
                    f"{d.name} median={d.median:.3f} "
                    f"(ratio={ratio:.2f}× vs overall median {overall_median:.3f})"
                )

    if problems:
        report.median_balance_ok = False
        report.median_balance_detail = "; ".join(problems)


def _check_cross_season(
    engine: Any,
    all_ids: list[tuple[int, int]],
    composite_scores: NDArray[np.float64],
    report: DiagnosticReport,
    entity_type: str,
) -> None:
    """
    Check that same-player cross-season pairs score above the population median.

    If 2024 Soto and 2025 Soto don't score higher than the median random pair,
    something is wrong — a player should be most similar to themselves.
    """
    pop_median = float(np.median(composite_scores)) if len(composite_scores) > 0 else 0.5

    # Find players with multiple seasons
    player_seasons: dict[int, list[int]] = defaultdict(list)
    for pid, season in all_ids:
        player_seasons[pid].append(season)

    multi_season_players = {
        pid: sorted(seasons)
        for pid, seasons in player_seasons.items()
        if len(seasons) >= 2
    }

    if not multi_season_players:
        report.n_cross_season_pairs = 0
        return

    cross_scores = []
    for pid, seasons in multi_season_players.items():
        for i in range(len(seasons)):
            for j in range(i + 1, len(seasons)):
                result = engine.query_pair((pid, seasons[i]), (pid, seasons[j]))
                if result is not None:
                    cross_scores.append(result.score)

    if not cross_scores:
        report.n_cross_season_pairs = 0
        return

    cross_arr = np.array(cross_scores)
    report.n_cross_season_pairs = len(cross_arr)
    report.cross_season_mean_score = float(np.mean(cross_arr))
    report.cross_season_above_median_frac = float(np.mean(cross_arr > pop_median))
    report.cross_season_ok = report.cross_season_above_median_frac >= CROSS_SEASON_PASS_RATE


def _check_cross_season_fielder(
    engine: Any,
    all_ids: list[tuple[int, str, int]],
    composite_scores: NDArray[np.float64],
    report: DiagnosticReport,
) -> None:
    """
    Check that same-player same-position cross-season pairs score above
    the population median.
    """
    pop_median = float(np.median(composite_scores)) if len(composite_scores) > 0 else 0.5

    # Group by (player_id, position) to find multi-season entries
    player_pos_seasons: dict[tuple[int, str], list[int]] = defaultdict(list)
    for pid, pos, season in all_ids:
        player_pos_seasons[(pid, pos)].append(season)

    multi = {
        k: sorted(seasons)
        for k, seasons in player_pos_seasons.items()
        if len(seasons) >= 2
    }

    if not multi:
        report.n_cross_season_pairs = 0
        return

    cross_scores = []
    for (pid, pos), seasons in multi.items():
        for i in range(len(seasons)):
            for j in range(i + 1, len(seasons)):
                result = engine.query_pair(
                    (pid, pos, seasons[i]),
                    (pid, pos, seasons[j]),
                )
                if result is not None:
                    cross_scores.append(result.score)

    if not cross_scores:
        report.n_cross_season_pairs = 0
        return

    cross_arr = np.array(cross_scores)
    report.n_cross_season_pairs = len(cross_arr)
    report.cross_season_mean_score = float(np.mean(cross_arr))
    report.cross_season_above_median_frac = float(np.mean(cross_arr > pop_median))
    report.cross_season_ok = report.cross_season_above_median_frac >= CROSS_SEASON_PASS_RATE


def _check_symmetry_batter(
    engine: Any,
    all_ids: list[tuple[int, int]],
    rng: np.random.Generator,
    report: DiagnosticReport,
    n_checks: int = 100,
) -> None:
    """Spot-check that score(A, B) == score(B, A) for random pairs."""
    n = len(all_ids)
    if n < 2:
        return

    n_checks = min(n_checks, n * (n - 1) // 2)
    max_asym = 0.0

    for _ in range(n_checks):
        i, j = rng.choice(n, size=2, replace=False)
        r_ab = engine.query_pair(all_ids[i], all_ids[j])
        r_ba = engine.query_pair(all_ids[j], all_ids[i])
        if r_ab is not None and r_ba is not None:
            asym = abs(r_ab.score - r_ba.score)
            max_asym = max(max_asym, asym)

    report.n_symmetry_checks = n_checks
    report.max_asymmetry = max_asym
    report.symmetry_ok = max_asym <= SYMMETRY_TOLERANCE


def _check_symmetry_pitcher(
    engine: Any,
    all_ids: list[tuple[int, int]],
    rng: np.random.Generator,
    report: DiagnosticReport,
    n_checks: int = 100,
) -> None:
    """Same as batter symmetry but for pitcher engine."""
    _check_symmetry_batter(engine, all_ids, rng, report, n_checks)


def _check_symmetry_fielder(
    engine: Any,
    all_ids: list[tuple[int, str, int]],
    rng: np.random.Generator,
    report: DiagnosticReport,
    n_checks: int = 100,
) -> None:
    """Spot-check that score(A, B) == score(B, A) for random same-position pairs."""
    # Group by position for valid pairing
    by_pos: dict[str, list[tuple[int, str, int]]] = defaultdict(list)
    for k in all_ids:
        by_pos[k[1]].append(k)

    max_asym = 0.0
    checks_done = 0

    for pos, ids in by_pos.items():
        if len(ids) < 2:
            continue
        pos_checks = min(n_checks // max(len(by_pos), 1), len(ids) * (len(ids) - 1) // 2)
        for _ in range(pos_checks):
            i, j = rng.choice(len(ids), size=2, replace=False)
            r_ab = engine.query_pair(ids[i], ids[j])
            r_ba = engine.query_pair(ids[j], ids[i])
            if r_ab is not None and r_ba is not None:
                asym = abs(r_ab.score - r_ba.score)
                max_asym = max(max_asym, asym)
                checks_done += 1

    report.n_symmetry_checks = checks_done
    report.max_asymmetry = max_asym
    report.symmetry_ok = max_asym <= SYMMETRY_TOLERANCE


# ============================================================================
# Synthetic Self-Test
# ============================================================================

def _run_synthetic_batter_test() -> DiagnosticReport:
    """Build a synthetic batter engine and run diagnostics on it."""
    from similarity.engines.batter_similarity import (
        BatterSimilarityEngine,
        BatterPartition,
        BatterProfile,
        EmpiricalBayesShrinkage,
        FeatureNormalizer,
        WeightedRBFSimilarity,
        DISCIPLINE_FEATURES,
        BATTED_BALL_FEATURES,
        POWER_FEATURES,
        PLATOON_FEATURES,
        RBF_SIGMA_DISCIPLINE,
        RBF_SIGMA_BATTED_BALL,
        RBF_SIGMA_PLATOON,
        RBF_SIGMA_POWER,
    )

    rng = np.random.default_rng(42)
    n_players = 80
    seasons = [2023, 2024]
    profiles = []

    # Generate synthetic profiles with realistic structure
    for pid in range(1, n_players + 1):
        # Each player has a "true talent" baseline + per-season noise
        base_disc = rng.beta(5, 5, len(DISCIPLINE_FEATURES))
        base_bb = np.concatenate([
            rng.beta(5, 5, 4),                     # rate stats
            rng.normal(89, 3, 1),                   # exit velo
            rng.normal(12, 5, 1),                   # launch angle
            rng.beta(5, 5, 2),                      # hard hit, barrel
        ])
        base_power = np.concatenate([
            rng.beta(2, 20, 1),                     # HR rate
            rng.beta(10, 30, 1) + 0.2,              # xba
            rng.beta(5, 8, 1) + 0.3,                # xslg
            rng.normal(108, 4, 1),                   # max EV
        ])
        base_plat = rng.beta(5, 5, len(PLATOON_FEATURES))

        bats = rng.choice(["L", "R", "R", "R", "S"])  # ~60% R, ~25% L, ~15% S

        for season in seasons:
            noise_scale = 0.03
            disc = np.clip(base_disc + rng.normal(0, noise_scale, len(DISCIPLINE_FEATURES)), 0, 1)
            bb = base_bb + rng.normal(0, [noise_scale]*4 + [1.0, 1.5] + [noise_scale]*2, len(BATTED_BALL_FEATURES))
            power = base_power + rng.normal(0, [0.01, 0.01, 0.02, 1.5], len(POWER_FEATURES))
            plat_l = np.clip(base_plat + rng.normal(0, noise_scale * 1.5, len(PLATOON_FEATURES)), 0, 1)
            plat_r = np.clip(base_plat + rng.normal(0, noise_scale * 1.5, len(PLATOON_FEATURES)), 0, 1)
            pa = rng.integers(200, 650)

            profiles.append(BatterProfile(
                batter_id=pid,
                season=season,
                bats=str(bats),
                sample_pa=int(pa),
                sample_pitches=int(pa * 4),
                discipline_vec=disc.astype(np.float64),
                batted_ball_vec=bb.astype(np.float64),
                power_vec=power.astype(np.float64),
                platoon_vs_l_vec=plat_l.astype(np.float64),
                platoon_vs_r_vec=plat_r.astype(np.float64),
                sample_pa_vs_l=int(pa * 0.3),
                sample_pa_vs_r=int(pa * 0.7),
                eb_alpha=float(pa / (pa + 30)),
            ))

    # Assemble engine without DuckDB
    engine = BatterSimilarityEngine.__new__(BatterSimilarityEngine)
    engine._duckdb_path = ""
    engine._profiles = {(p.batter_id, p.season): p for p in profiles}
    engine._league_avg = {"discipline": {}, "batted_ball": {}, "power": {},
                          "platoon_l": {}, "platoon_r": {}}
    engine._normalizer = FeatureNormalizer()
    engine._shrinkage = EmpiricalBayesShrinkage()
    engine._partition = BatterPartition()
    engine._disc_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_DISCIPLINE,
        reliability_weights=np.array([w for _, w in DISCIPLINE_FEATURES]),
    )
    engine._bb_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_BATTED_BALL,
        reliability_weights=np.array([w for _, w in BATTED_BALL_FEATURES]),
    )
    engine._platoon_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_PLATOON,
        reliability_weights=np.array([w for _, w in PLATOON_FEATURES]),
    )
    engine._power_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_POWER,
        reliability_weights=np.array([w for _, w in POWER_FEATURES]),
    )

    engine._normalizer.fit(profiles)
    engine._partition.build(profiles, engine._normalizer)

    return run_batter_diagnostics(engine, n_query_samples=30, seed=42)


def _run_synthetic_pitcher_test() -> DiagnosticReport:
    """Build a synthetic pitcher engine and run diagnostics on it."""
    from similarity.engines.pitcher_similarity import (
        PitcherSimilarityEngine,
        PitcherProfile,
        GMMModel,
        GMMComponent,
        ArsenalCache,
        HandednessPartition,
        FeatureNormalizer,
        EmpiricalBayesShrinkage,
        RBFSimilarity,
        enforce_min_cluster_size,
        GMM_FEATURE_DIM,
        GMM_FEATURE_NAMES,
        COMMAND_FEATURES,
        RESULT_FEATURES,
        RBF_SIGMA_COMMAND,
        RBF_SIGMA_RESULTS,
    )

    rng = np.random.default_rng(42)
    n_players = 60
    seasons = [2023, 2024]
    profiles = []

    for pid in range(1, n_players + 1):
        hand = "R" if rng.random() > 0.3 else "L"
        base_cmd = rng.beta(5, 5, len(COMMAND_FEATURES))
        base_res = rng.beta(5, 5, len(RESULT_FEATURES))
        rx_sign = -1.0 if hand == "R" else 1.0

        # GMM: 2-4 components per pitcher
        n_comp = rng.integers(2, 5)
        weights = rng.dirichlet(np.ones(n_comp))
        base_means = []
        for c in range(n_comp):
            base_means.append(rng.normal(
                [92 - c*4, 10 - c*5, -5 + c*3, 2300, 200, rx_sign*1.5, 6.0, 6.3],
                [2, 3, 3, 200, 30, 0.2, 0.2, 0.3],
            ))

        for season in seasons:
            cmd = np.clip(base_cmd + rng.normal(0, 0.02, len(COMMAND_FEATURES)), 0, 1)
            res = np.clip(base_res + rng.normal(0, 0.02, len(RESULT_FEATURES)), 0, 1)
            pitches = rng.integers(500, 3500)

            components = []
            for c in range(n_comp):
                m = base_means[c] + rng.normal(0, [0.5, 1, 1, 50, 5, 0.05, 0.05, 0.05])
                cov = np.diag(rng.uniform(0.5, 3.0, GMM_FEATURE_DIM))
                components.append(GMMComponent(
                    component_id=c,
                    weight=float(weights[c]),
                    mean=m.astype(np.float64),
                    covariance=cov.astype(np.float64),
                    n_pitches=int(pitches * weights[c]),
                ))

            gmm = GMMModel(
                n_components=n_comp,
                feature_names=GMM_FEATURE_NAMES,
                feature_means=np.zeros(GMM_FEATURE_DIM),
                feature_stds=np.ones(GMM_FEATURE_DIM),
                components=components,
            )

            profiles.append(PitcherProfile(
                pitcher_id=pid,
                season=season,
                p_throws=hand,
                sample_pitches=int(pitches),
                gmm=gmm,
                command_vec=cmd.astype(np.float64),
                result_vec=res.astype(np.float64),
                eb_alpha=float(pitches / (pitches + 50)),
            ))

    engine = PitcherSimilarityEngine.__new__(PitcherSimilarityEngine)
    engine._duckdb_path = ""
    engine._profiles = {(p.pitcher_id, p.season): p for p in profiles}
    engine._league_avg_command = {}
    engine._league_avg_result = {}
    engine._normalizer = FeatureNormalizer()
    engine._shrinkage = EmpiricalBayesShrinkage()
    engine._command_rbf = RBFSimilarity(sigma=RBF_SIGMA_COMMAND)
    engine._result_rbf = RBFSimilarity(sigma=RBF_SIGMA_RESULTS)
    engine._partition_l = HandednessPartition("L")
    engine._partition_r = HandednessPartition("R")
    engine._arsenal_cache = ArsenalCache()

    # Replicate the build() pipeline: min-cluster-size → standardize → normalize → partition
    for key, p in engine._profiles.items():
        if p.gmm is not None:
            engine._profiles[key].gmm = enforce_min_cluster_size(p.gmm)

    engine._standardize_arsenals()

    all_p = list(engine._profiles.values())
    engine._normalizer.fit(all_p)
    profiles_l = [p for p in all_p if p.p_throws == "L"]
    profiles_r = [p for p in all_p if p.p_throws == "R"]
    engine._partition_l.build(profiles_l, engine._normalizer)
    engine._partition_r.build(profiles_r, engine._normalizer)

    return run_pitcher_diagnostics(engine, n_query_samples=25, seed=42)


def _run_synthetic_fielder_test() -> DiagnosticReport:
    """Build a synthetic fielder engine and run diagnostics on it."""
    from similarity.engines.fielder_similarity import (
        FielderSimilarityEngine,
        FielderProfile,
        FeatureNormalizer,
        EmpiricalBayesShrinkage,
        PositionPartition,
        WeightedRBFSimilarity,
        IF_RANGE_FEATURES, IF_DP_FEATURES, IF_PIVOT_FEATURES,
        IF_ERROR_FEATURES, IF_SPECIALTY_FEATURES,
        OF_RANGE_FEATURES, OF_ARM_FEATURES, OF_STAR_FEATURES, OF_ERROR_FEATURES,
        RBF_SIGMA_IF_RANGE, RBF_SIGMA_IF_DP, RBF_SIGMA_IF_ERRORS,
        RBF_SIGMA_IF_SPECIALTY,
        RBF_SIGMA_OF_RANGE, RBF_SIGMA_OF_ARM, RBF_SIGMA_OF_STARS,
        RBF_SIGMA_OF_ERRORS,
        INFIELD_POSITIONS, OUTFIELD_POSITIONS, ALL_POSITIONS,
    )

    rng = np.random.default_rng(42)
    seasons = [2023, 2024]
    profiles = []

    # Generate 20 players per position × 2 seasons
    pid = 1
    for pos in sorted(ALL_POSITIONS):
        is_if = pos in INFIELD_POSITIONS
        is_middle = pos in ("2B", "SS")

        for _ in range(20):
            # Base talent
            base_range = rng.normal(0, 3, len(IF_RANGE_FEATURES if is_if else OF_RANGE_FEATURES))
            base_error = rng.beta(2, 50, len(IF_ERROR_FEATURES if is_if else OF_ERROR_FEATURES))

            if is_if:
                dp_dim = len(IF_DP_FEATURES) + (len(IF_PIVOT_FEATURES) if is_middle else 0)
                base_dp = rng.normal(0, 2, dp_dim)
                base_spec = rng.beta(5, 5, len(IF_SPECIALTY_FEATURES))
                base_arm = None
                base_star = None
            else:
                base_dp = None
                base_spec = None
                base_arm = np.concatenate([
                    rng.beta(5, 5, 3),
                    rng.normal(0, 1, 1),
                ])
                base_star = rng.beta(5, 5, len(OF_STAR_FEATURES))

            for season in seasons:
                noise = 0.3
                range_vec = base_range + rng.normal(0, noise, base_range.shape)
                error_vec = np.clip(base_error + rng.normal(0, 0.005, base_error.shape), 0, 0.2)
                bb = rng.integers(80, 400)

                dp_vec = None
                specialty_vec = None
                arm_vec = None
                star_vec = None

                if is_if:
                    dp_vec = (base_dp + rng.normal(0, noise, base_dp.shape)).astype(np.float64)
                    specialty_vec = np.clip(
                        base_spec + rng.normal(0, 0.03, base_spec.shape), 0, 1
                    ).astype(np.float64)
                else:
                    arm_vec = np.concatenate([
                        np.clip(base_arm[:3] + rng.normal(0, 0.03, 3), 0, 1),
                        base_arm[3:] + rng.normal(0, 0.3, 1),
                    ]).astype(np.float64)
                    star_vec = np.clip(
                        base_star + rng.normal(0, 0.05, base_star.shape), 0, 1
                    ).astype(np.float64)

                profiles.append(FielderProfile(
                    player_id=pid,
                    position=pos,
                    season=season,
                    innings_played=float(rng.integers(200, 1200)),
                    sample_batted_balls=int(bb),
                    range_vec=range_vec.astype(np.float64),
                    error_vec=error_vec.astype(np.float64),
                    dp_vec=dp_vec,
                    specialty_vec=specialty_vec,
                    arm_vec=arm_vec,
                    star_vec=star_vec,
                    eb_alpha=float(bb / (bb + 15)),
                ))

            pid += 1

    # Assemble engine without DuckDB
    engine = FielderSimilarityEngine.__new__(FielderSimilarityEngine)
    engine._duckdb_path = ""
    engine._profiles = {(p.player_id, p.position, p.season): p for p in profiles}
    engine._pos_avg = {g: {} for g in ["range", "error", "dp", "specialty", "arm", "star"]}
    engine._normalizer = FeatureNormalizer()
    engine._shrinkage = EmpiricalBayesShrinkage()
    engine._partitions = {pos: PositionPartition(pos) for pos in ALL_POSITIONS}

    # Build RBF scorers
    engine._if_range_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_IF_RANGE,
        reliability_weights=np.array([w for _, w in IF_RANGE_FEATURES]),
    )
    engine._if_dp_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_IF_DP,
        reliability_weights=np.array(
            [w for _, w in IF_DP_FEATURES] + [w for _, w in IF_PIVOT_FEATURES]
        ),
    )
    engine._if_dp_rbf_corner = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_IF_DP,
        reliability_weights=np.array([w for _, w in IF_DP_FEATURES]),
    )
    engine._if_error_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_IF_ERRORS,
        reliability_weights=np.array([w for _, w in IF_ERROR_FEATURES]),
    )
    engine._if_specialty_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_IF_SPECIALTY,
        reliability_weights=np.array([w for _, w in IF_SPECIALTY_FEATURES]),
    )
    engine._of_range_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_OF_RANGE,
        reliability_weights=np.array([w for _, w in OF_RANGE_FEATURES]),
    )
    engine._of_arm_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_OF_ARM,
        reliability_weights=np.array([w for _, w in OF_ARM_FEATURES]),
    )
    engine._of_star_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_OF_STARS,
        reliability_weights=np.array([w for _, w in OF_STAR_FEATURES]),
    )
    engine._of_error_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_OF_ERRORS,
        reliability_weights=np.array([w for _, w in OF_ERROR_FEATURES]),
    )

    # Group by position, fit normalizer, build partitions
    profiles_by_pos: dict[str, list[FielderProfile]] = {pos: [] for pos in ALL_POSITIONS}
    for p in profiles:
        profiles_by_pos[p.position].append(p)

    engine._normalizer.fit(profiles_by_pos)

    for pos, partition in engine._partitions.items():
        partition.build(profiles_by_pos.get(pos, []), engine._normalizer)

    return run_fielder_diagnostics(engine, n_query_samples=15, seed=42)


def _run_synthetic_baserunner_test() -> DiagnosticReport:
    """Build a synthetic baserunner engine and run diagnostics on it."""
    from similarity.engines.baserunner_similarity import (
        BaserunnerSimilarityEngine,
        BaserunnerProfile,
        FeatureNormalizer,
        EmpiricalBayesShrinkage,
        BaserunnerPartition,
        WeightedRBFSimilarity,
        SPEED_FEATURES, AGGRESSION_FEATURES, SUCCESS_FEATURES,
        RBF_SIGMA_SPEED, RBF_SIGMA_AGGRESSION, RBF_SIGMA_SUCCESS,
    )

    rng = np.random.default_rng(42)
    seasons = [2023, 2024]
    profiles = []

    # Generate 60 runners × 2 seasons with correlated base talent
    for pid in range(1, 61):
        # Base talent: sprint speed drives everything
        base_speed = rng.normal(27.5, 1.5)  # ft/s, realistic range ~24-31
        # Faster runners attempt more and succeed more
        speed_factor = (base_speed - 25.0) / 6.0  # normalized ~[-0.17, 1.0]
        base_attempt = np.clip(0.45 + speed_factor * 0.2 + rng.normal(0, 0.08, 6), 0.05, 0.95)
        base_success = np.clip(0.70 + speed_factor * 0.15 + rng.normal(0, 0.06, 5), 0.30, 0.99)

        for season in seasons:
            noise = 0.03
            speed = np.array([base_speed + rng.normal(0, 0.3)], dtype=np.float64)
            agg = np.clip(base_attempt + rng.normal(0, noise, 6), 0.0, 1.0).astype(np.float64)
            suc = np.clip(base_success + rng.normal(0, noise, 5), 0.0, 1.0).astype(np.float64)

            # Make stop_rate consistent: 1 - attempt_rate
            agg[5] = 1.0 - agg[0]

            opps = int(rng.integers(25, 120))

            profiles.append(BaserunnerProfile(
                player_id=pid,
                season=season,
                sample_advancement_opps=opps,
                speed_vec=speed,
                aggression_vec=agg,
                success_vec=suc,
                sample_first_to_third_opps=int(rng.integers(5, 40)),
                sample_second_to_home_opps=int(rng.integers(5, 30)),
                sample_first_to_home_opps=int(rng.integers(2, 15)),
                sample_tag_up_opps=int(rng.integers(2, 10)),
                eb_alpha=float(opps / (opps + 15)),
            ))

    # Assemble engine without DuckDB
    engine = BaserunnerSimilarityEngine.__new__(BaserunnerSimilarityEngine)
    engine._duckdb_path = ""
    engine._profiles = {(p.player_id, p.season): p for p in profiles}
    engine._league_avg = {"speed": {}, "aggression": {}, "success": {}}
    engine._normalizer = FeatureNormalizer()
    engine._shrinkage = EmpiricalBayesShrinkage()
    engine._partition = BaserunnerPartition()

    engine._speed_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_SPEED,
        reliability_weights=np.array([w for _, w in SPEED_FEATURES]),
    )
    engine._agg_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_AGGRESSION,
        reliability_weights=np.array([w for _, w in AGGRESSION_FEATURES]),
    )
    engine._success_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_SUCCESS,
        reliability_weights=np.array([w for _, w in SUCCESS_FEATURES]),
    )

    all_p = list(engine._profiles.values())
    engine._normalizer.fit(all_p)
    engine._partition.build(all_p, engine._normalizer)

    return run_baserunner_diagnostics(engine, n_query_samples=25, seed=42)


# ============================================================================
# Generic Diagnostics — works with BaserunnerSteal, Catcher, PitcherSteal,
# Manager, and any future RBF engine that exposes:
#   - profile_ids() → list[tuple[int, int]]
#   - query(player_id, season) → list with .score and sub-score attributes
#   - query_pair(key_a, key_b) → SimilarityResult | None
#   - profile_count → int
# ============================================================================

def run_generic_diagnostics(
    engine: Any,
    sub_score_names: list[str],
    n_query_samples: int = 50,
    seed: int = 42,
    engine_name: str = "generic",
) -> DiagnosticReport:
    """
    Run full diagnostics on any RBF similarity engine.

    Works with BaserunnerStealSimilarityEngine, CatcherSimilarityEngine,
    PitcherStealSimilarityEngine, ManagerSimilarityEngine, and any future
    engine with the standard interface:
        - profile_ids()  -> list[tuple[int, int]]
        - query(player_id, season) -> list[SimilarityResult]
        - query_pair(key_a, key_b) -> SimilarityResult | None
        - profile_count -> int

    Parameters
    ----------
    engine
        Built similarity engine instance.
    sub_score_names : list[str]
        Names of sub-score attributes on each SimilarityResult.
        E.g. ["tendency_score", "jump_score", "success_score"] for steal engine.
    n_query_samples : int
        Number of random profiles to query. Capped at profile_count.
    seed : int
        RNG seed for reproducible sampling.
    engine_name : str
        Label used in DiagnosticReport.engine_type.

    Returns
    -------
    DiagnosticReport
        Populated with score distributions, dimensional balance check,
        cross-season self-similarity check, and symmetry check.
    """
    t0 = time.time()
    report = DiagnosticReport(engine_type=engine_name, n_profiles=engine.profile_count)

    all_ids = engine.profile_ids()
    if not all_ids:
        log.warning("Engine '%s' has no profiles. Cannot run diagnostics.", engine_name)
        return report

    rng = np.random.default_rng(seed)
    n_samples = min(n_query_samples, len(all_ids))
    sample_indices = rng.choice(len(all_ids), size=n_samples, replace=False)
    sample_ids = [all_ids[i] for i in sample_indices]
    report.n_queries_sampled = n_samples

    # Collect composite + per-sub-score arrays
    all_composite: list[float] = []
    sub_score_arrays: dict[str, list[float]] = {name: [] for name in sub_score_names}

    for player_id, season in sample_ids:
        try:
            results = engine.query(player_id, season)
        except Exception as exc:
            log.warning(
                "query(%d, %d) raised %s -- skipping.", player_id, season, exc,
            )
            continue
        for r in results:
            all_composite.append(r.score)
            for name in sub_score_names:
                sub_score_arrays[name].append(getattr(r, name, float("nan")))

    report.n_total_scores = len(all_composite)

    # Build ScoreDistribution objects
    composite_arr = np.array(all_composite, dtype=np.float64)
    report.distributions.append(ScoreDistribution.from_array("composite", composite_arr))
    for name in sub_score_names:
        arr = np.array(sub_score_arrays[name], dtype=np.float64)
        report.distributions.append(ScoreDistribution.from_array(name, arr))

    # Shared checks (same infrastructure as existing engine-specific runners)
    if len(report.distributions) > 1:
        _check_dimensional_balance(report)

    _check_cross_season(engine, all_ids, composite_arr, report, entity_type=engine_name)

    # Symmetry uses the (player_id, season) key shape -- same as batter/baserunner
    _check_symmetry_batter(engine, all_ids, rng, report)

    report.elapsed_sec = time.time() - t0
    return report
