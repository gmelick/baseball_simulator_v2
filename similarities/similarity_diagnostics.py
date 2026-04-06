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
    all_results = []

    for pitcher_id, season in sample_ids:
        results = engine.query(pitcher_id, season)
        for r in results:
            all_composite.append(r.score)
            all_arsenal.append(r.arsenal_score)
            all_command.append(r.command_score)
            all_results.append(r.results_score)

    report.n_total_scores = len(all_composite)

    composite_arr = np.array(all_composite)
    sub_arrays = {
        "composite": composite_arr,
        "arsenal": np.array(all_arsenal),
        "command": np.array(all_command),
        "results": np.array(all_results),
    }

    for name, arr in sub_arrays.items():
        report.distributions.append(ScoreDistribution.from_array(name, arr))

    _check_dimensional_balance(report)
    _check_cross_season(engine, all_ids, composite_arr, report, entity_type="pitcher")
    _check_symmetry_pitcher(engine, all_ids, rng, report)

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


# ============================================================================
# Synthetic Self-Test
# ============================================================================

def _run_synthetic_batter_test() -> DiagnosticReport:
    """Build a synthetic batter engine and run diagnostics on it."""
    from batter_similarity import (
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
    from pitcher_similarity import (
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


# ============================================================================
# CLI
# ============================================================================
if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    print("Running synthetic batter diagnostics …\n")
    batter_report = _run_synthetic_batter_test()
    print(batter_report)

    print("\n\n")

    print("Running synthetic pitcher diagnostics …\n")
    pitcher_report = _run_synthetic_pitcher_test()
    print(pitcher_report)

    # Exit with non-zero if any flags
    total_flags = batter_report.n_flags + pitcher_report.n_flags
    if total_flags > 0:
        print(f"\n{total_flags} total flag(s) across both engines.")
    sys.exit(min(total_flags, 1))
