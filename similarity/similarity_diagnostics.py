"""The sanity checks on a built similarity engine's scores.

A run queries a sample of profiles against the whole population and checks
the spread of the composite score and of every sub-score. It reports four
kinds of fault:

- **Distribution.** A sub-score whose median is under 0.05 is collapsed; one
  over 0.95 is inflated; one whose standard deviation is under 0.05 does not
  tell players apart. NaN and infinite scores are counted.
- **Dimensional balance.** Each sub-score's median must sit within 2.5 times
  the median of the sub-score medians. A sub-score far below the others adds
  nothing to the composite.
- **Cross-season self-similarity.** A player's two seasons must score above
  the population median for at least 70% of the pairs.
- **Symmetry.** score(A, B) must equal score(B, A) within 1e-6 on random
  pairs.

Each engine's ``__main__`` block runs its check and prints the report:
``run_batter_diagnostics``, ``run_fielder_diagnostics``,
``run_baserunner_diagnostics``, or ``run_generic_diagnostics`` for an engine
keyed by ``(player_id, season)`` with named sub-scores (the catcher, the
manager and the two steal engines).

    engine = BatterSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
    engine.build(seasons=[2024, 2025])
    print(run_batter_diagnostics(engine, n_query_samples=50))
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
MEDIAN_COLLAPSED_THRESHOLD = 0.05  # sub-score median below this = collapsed
MEDIAN_INFLATED_THRESHOLD = 0.95  # sub-score median above this = inflated
STD_NO_DISCRIMINATION = 0.05  # sub-score std below this = no spread
MEDIAN_BALANCE_RATIO = 2.5  # max ratio between any sub-score median and overall
CROSS_SEASON_PASS_RATE = 0.70  # fraction of cross-season pairs above pop median
SYMMETRY_TOLERANCE = 1e-6  # max allowed asymmetry in score(A,B) vs score(B,A)


# ============================================================================
# Distribution Statistics
# ============================================================================


@dataclass
class ScoreDistribution:
    """The spread of one score (the composite or one sub-score) and its flags."""

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
    n_zero: int = 0  # exactly 0.0
    n_one: int = 0  # exactly 1.0
    n_below_01: int = 0  # < 0.1
    n_above_09: int = 0  # > 0.9

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
    """The checks' results for one engine; ``str()`` renders the report."""

    engine_type: str = ""  # the engine's label, e.g. "batter" or "Catcher"
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
                lines.append(
                    "  OK: Cross-season self-pairs score above population median at acceptable rate."
                )
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
            f"  {self.n_symmetry_checks} pairs checked.  Max asymmetry: {self.max_asymmetry:.2e}"
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
        self.n_flags = sum(
            1
            for d in self.distributions
            if d.is_collapsed
            or d.is_inflated
            or d.is_no_discrimination
            or d.n_nan > 0
            or d.n_inf > 0
        )
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
    _check_symmetry(engine, all_ids, rng, report)

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
    positions_to_check = [position] if position else sorted({k[1] for k in all_ids})

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
    _check_symmetry(engine, all_ids, rng, report)  # same (player_id, season) key shape

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
        pid: sorted(seasons) for pid, seasons in player_seasons.items() if len(seasons) >= 2
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

    multi = {k: sorted(seasons) for k, seasons in player_pos_seasons.items() if len(seasons) >= 2}

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


def _check_symmetry(
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

    for _pos, ids in by_pos.items():
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
# Generic Diagnostics: any engine keyed by (player_id, season)
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
        E.g. ["tendency_score", "lead_score", "success_score"] for the steal engine.
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
                "query(%d, %d) raised %s -- skipping.",
                player_id,
                season,
                exc,
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
    _check_symmetry(engine, all_ids, rng, report)

    report.elapsed_sec = time.time() - t0
    return report
