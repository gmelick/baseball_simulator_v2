"""The accuracy table: the records rolled into one row per market, and the text
table the run prints.

Each row compares the simulator's Brier score and log loss with the closing
line's, paired on the same observations (see ``scripts/accuracy_comparison.py``
for the method). The aggregation is pure: no database, no simulator. Its interval
uses a seeded bootstrap, so a rerun gives the same numbers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from pipeline.odds_provider import book_display_name, book_label
from simulation.prop_validation import OUTCOME_PROB_EPS, binary_brier, binary_log_loss

from .confidence import (
    DEFAULT_ALPHA,
    DEFAULT_BOOTSTRAP_SAMPLES,
    DETECTION_MARGIN,
    MIN_DETECTABLE_EDGE,
    _bonferroni_alpha,
    _bootstrap_paired_diff_ci,
    _json_safe,
    _minimum_observations_clustered,
)
from .records import TRUST_TIER_ORDER, AccuracyRecord, trust_label


@dataclass(frozen=True, slots=True)
class AccuracyComparisonRow:
    """The paired accuracy comparison of one group of records.

    A NEGATIVE ``*_diff_mean`` means the simulator's score was lower than the
    market's on average, so the simulator was more accurate: Brier score and log
    loss are lower-is-better proper scoring rules. ``*_ci_low`` and ``*_ci_high``
    are a 95% cluster-bootstrap interval of that mean
    (:func:`aggregate_accuracy_comparison`); an interval that does not cross zero
    is the "likely real, not chance" read.

    ``brier_min_n`` and ``log_loss_min_n`` are the fewest paired OBSERVATIONS at
    which the row's own game-clustered spread clears the minimum-detectable-edge
    floor at ``alpha_used`` (:mod:`accuracy.confidence`). ``min_n_recommended`` is
    the larger of the two, the number to check ``n`` against. They are ``inf``
    with too few observations or distinct games to measure a spread.
    ``underpowered`` is True while ``n`` is below ``min_n_recommended``.

    ``n_games`` is the distinct game count behind the row. Read it beside
    ``min_n_recommended``, which counts observations: a prop bucket (several
    players' records from one game) reaches its observation minimum from far
    fewer games than the number suggests.
    """

    group: str
    trust: str
    n: int
    n_games: int
    sim_brier: float
    market_brier: float
    brier_diff_mean: float
    brier_diff_ci_low: float
    brier_diff_ci_high: float
    sim_log_loss: float
    market_log_loss: float
    log_loss_diff_mean: float
    log_loss_diff_ci_low: float
    log_loss_diff_ci_high: float
    # --- the minimum sample size ------------------------------------------------
    brier_min_n: float = float("inf")
    log_loss_min_n: float = float("inf")
    min_n_recommended: float = float("inf")
    underpowered: bool = True
    alpha_used: float = DEFAULT_ALPHA

    def to_jsonable(self) -> dict[str, Any]:
        """The row as a JSON-safe dict: several fields are ``nan`` at n = 0 or
        ``inf`` when underpowered, and neither is legal JSON (:func:`_json_safe`)."""
        return _json_safe(asdict(self))


def _accuracy_row_for(
    group: str,
    trust: str,
    records: Sequence[AccuracyRecord],
    *,
    n_bootstrap: int,
    seed: int,
    alpha: float,
) -> AccuracyComparisonRow:
    """One bucket of accuracy records rolled into one row (pure).

    The per-observation Brier and log-loss DIFFERENCES are computed here, not in
    :func:`binary_brier` / :func:`binary_log_loss` (they return only the
    aggregate), so the bootstrap has something to resample: each difference is one
    observation's simulator error minus its market error. The log-loss difference
    clips both probabilities with the :data:`OUTCOME_PROB_EPS` that
    :func:`binary_log_loss` uses, so the differences sum to the aggregate reported
    beside them.

    ``alpha`` (Bonferroni-corrected on a by-market row) sizes the minimum sample:
    :data:`MIN_DETECTABLE_EDGE` shrunk by :data:`DETECTION_MARGIN`, fed through
    :func:`_minimum_observations_clustered` at the bucket's own clustered spread.
    """
    n = len(records)
    sim_p = np.array([r.sim_prob for r in records], dtype=np.float64)
    mkt_p = np.array([r.market_prob for r in records], dtype=np.float64)
    y = np.array([r.outcome for r in records], dtype=np.int64)
    game_pks = np.array([r.game_pk for r in records], dtype=np.int64)

    sim_brier = binary_brier(sim_p, y)
    market_brier = binary_brier(mkt_p, y)
    sim_ll = binary_log_loss(sim_p, y)
    market_ll = binary_log_loss(mkt_p, y)

    if n:
        # The [0, 1] clip is the one binary_brier applies before it scores
        # sim_brier and market_brier above. Every producer of these probabilities
        # stays inside [0, 1] today (the win-probability map clamps; the total and
        # run-line probabilities are count ratios; devig_two_way normalises), so
        # the clip changes nothing now. It keeps the per-observation differences
        # summing to the reported aggregate if a future producer is unbounded.
        sim_p_b = np.clip(sim_p, 0.0, 1.0)
        mkt_p_b = np.clip(mkt_p, 0.0, 1.0)
        brier_diffs = (sim_p_b - y) ** 2 - (mkt_p_b - y) ** 2
        eps = OUTCOME_PROB_EPS
        sim_pc = np.clip(sim_p, eps, 1.0 - eps)
        mkt_pc = np.clip(mkt_p, eps, 1.0 - eps)
        sim_ll_i = -(y * np.log(sim_pc) + (1 - y) * np.log(1.0 - sim_pc))
        mkt_ll_i = -(y * np.log(mkt_pc) + (1 - y) * np.log(1.0 - mkt_pc))
        log_loss_diffs = sim_ll_i - mkt_ll_i
    else:
        brier_diffs = np.array([], dtype=np.float64)
        log_loss_diffs = np.array([], dtype=np.float64)

    brier_mean = float(brier_diffs.mean()) if n else float("nan")
    ll_mean = float(log_loss_diffs.mean()) if n else float("nan")
    brier_lo, brier_hi = _bootstrap_paired_diff_ci(
        brier_diffs, game_pks, n_bootstrap=n_bootstrap, seed=seed
    )
    # The log-loss draw's seed sits far from every row's Brier seed. The caller
    # gives each row a seed one step apart (overall = seed, by-market row i =
    # seed + i), so an offset of 1 would make row i's log-loss draw and row i+1's
    # Brier draw resample identically.
    ll_lo, ll_hi = _bootstrap_paired_diff_ci(
        log_loss_diffs, game_pks, n_bootstrap=n_bootstrap, seed=seed + 1_000_003
    )

    # The minimum sample size from the bucket's own spread, clustered by game the
    # way the bootstrap resamples games: several observations of one game (three
    # game markets off one score; several props of one start) are fewer
    # independent trials than their row count. None (too few observations or
    # games to measure a spread) reads as inf.
    shrunk_floor = MIN_DETECTABLE_EDGE / DETECTION_MARGIN
    brier_by_game: dict[int, list[float]] = {}
    ll_by_game: dict[int, list[float]] = {}
    for gp, bd, ld in zip(
        game_pks.tolist(), brier_diffs.tolist(), log_loss_diffs.tolist(), strict=True
    ):
        brier_by_game.setdefault(int(gp), []).append(float(bd))
        ll_by_game.setdefault(int(gp), []).append(float(ld))
    brier_min_n_or_none = _minimum_observations_clustered(
        brier_by_game, floor=shrunk_floor, alpha=alpha
    )
    ll_min_n_or_none = _minimum_observations_clustered(ll_by_game, floor=shrunk_floor, alpha=alpha)
    brier_min_n = float("inf") if brier_min_n_or_none is None else brier_min_n_or_none
    log_loss_min_n = float("inf") if ll_min_n_or_none is None else ll_min_n_or_none
    min_n_recommended = max(brier_min_n, log_loss_min_n)
    underpowered = n < min_n_recommended

    return AccuracyComparisonRow(
        group=group,
        trust=trust,
        n=n,
        n_games=len(set(game_pks.tolist())),
        sim_brier=sim_brier,
        market_brier=market_brier,
        brier_diff_mean=brier_mean,
        brier_diff_ci_low=brier_lo,
        brier_diff_ci_high=brier_hi,
        sim_log_loss=sim_ll,
        market_log_loss=market_ll,
        log_loss_diff_mean=ll_mean,
        log_loss_diff_ci_low=ll_lo,
        log_loss_diff_ci_high=ll_hi,
        brier_min_n=brier_min_n,
        log_loss_min_n=log_loss_min_n,
        min_n_recommended=min_n_recommended,
        underpowered=underpowered,
        alpha_used=alpha,
    )


def aggregate_accuracy_comparison(
    records: Sequence[AccuracyRecord],
    *,
    n_bootstrap: int = DEFAULT_BOOTSTRAP_SAMPLES,
    seed: int = 538,
    base_alpha: float = DEFAULT_ALPHA,
) -> dict[str, Any]:
    """The accuracy report of a list of accuracy records (pure).

    An ``overall`` row over every record, at the uncorrected ``base_alpha``: it
    is one top-line figure, not one of many rows a reader scans. Then one
    ``by_market`` row per market key, Bonferroni-corrected by the number of rows,
    sorted by trust tier and then market key. Every row carries the 95% bootstrap
    interval of the paired Brier and log-loss differences and its stated minimum
    sample size (:attr:`AccuracyComparisonRow.min_n_recommended`). The overall row
    resamples with ``seed`` and the i-th market row with ``seed + i``.
    """
    records = list(records)
    overall = _accuracy_row_for(
        "overall", "—", records, n_bootstrap=n_bootstrap, seed=seed, alpha=base_alpha
    )

    by_market_key: dict[str, list[AccuracyRecord]] = {}
    for r in records:
        by_market_key.setdefault(r.market, []).append(r)

    by_market_alpha = _bonferroni_alpha(base_alpha, len(by_market_key))
    rows = [
        _accuracy_row_for(
            market,
            trust_label(market),
            bucket,
            n_bootstrap=n_bootstrap,
            seed=seed + i,
            alpha=by_market_alpha,
        )
        for i, (market, bucket) in enumerate(sorted(by_market_key.items()), start=1)
    ]
    rows.sort(key=lambda r: (TRUST_TIER_ORDER.get(r.trust, 9), r.group))

    return {
        "overall": overall.to_jsonable(),
        "by_market": [r.to_jsonable() for r in rows],
    }


def _fmt_min_n(min_n: float | None) -> str:
    """A minimum sample size for a text table.

    ``inf`` (too little data to estimate a spread) prints as ``"inf"``, not as a
    huge integer. ``None``, the JSON report's form of ``inf`` (:func:`_json_safe`),
    prints the same, so a row read back from the report prints alike.
    """
    if min_n is None or min_n == float("inf"):
        return "inf"
    return f"{min_n:.0f}"


def _fmt_or_na(x: float | None, spec: str) -> str:
    """A float that may be ``None``, for a text table.

    ``None`` is the JSON report's form of a ``nan`` (``sim_brier`` or
    ``brier_diff_ci_low`` at ``n == 0``), so a row read back from the report
    prints ``"n/a"`` instead of crashing the table. A run with no scoreable game
    prints such a row before it checks ``n``.
    """
    return "n/a" if x is None else format(x, spec)


def _graded_book_line(params: dict[str, Any]) -> str:
    """The header line that names the graded book (one graded book per market,
    SIM-555)."""
    bench = params.get("benchmark_book")
    if bench:
        return (
            f"graded against ONE book: {book_display_name(bench)} ({bench}), the benchmark "
            f"(odds rows {params.get('odds_row_version')})"
        )
    order = ", ".join(
        book_display_name(book_label(b)) for b in params.get("graded_book_preference") or []
    )
    return (
        f"graded against the first book with a row, in this order: {order} "
        f"(odds rows {params.get('odds_row_version')})"
    )


def format_accuracy_comparison(
    comparison: dict[str, Any],
    *,
    params: dict[str, Any],
    counters: dict[str, Any] | None = None,
) -> str:
    """The accuracy comparison as a text table, grouped by trust tier.

    ``counters`` (the run's counters block) adds a header line with the games
    graded on the official box score and on the event-label fallback, and one
    line per run-line market with its pairs and its games listed as two separate
    bets. Omit it for a table with no run behind it (a unit test on synthetic
    records).
    """
    lines = [
        "=" * 100,
        "SIM-538 SIM-VS-CLOSING-LINE ACCURACY COMPARISON  (the headline metric)",
        "Brier / log-loss are lower-is-better. A NEGATIVE diff means the simulator was",
        "MORE ACCURATE than the de-vigged closing line, on average, over these games.",
        "The 95% range is a paired bootstrap interval on that mean difference. minN is",
        "SIM-539's stated minimum sample size for THIS market (a Bonferroni-corrected,",
        "game-clustering-aware read — see the module docstring), counted in OBSERVATIONS",
        "(n), not games — several props can share one game, so compare minN against n,",
        "not against games. UNDERPWR means n has not yet reached minN, so a range that",
        "does not cross zero here may still be chance, not signal.",
        "=" * 100,
        f"seasons={params.get('seasons')}  iterations={params.get('iterations')}  "
        f"markets={params.get('markets')}  base_seed={params.get('base_seed')}  "
        f"calibrated={params.get('calibration_applied')}",
    ]
    if params.get("odds_row_version"):
        # The book each market is graded against.
        lines.append(_graded_book_line(params))
    if counters is not None:
        # The fallback grades five props per game; the official record grades
        # every priced market. The reader must see the split.
        lines.append(
            f"prop ground truth: official box score={counters.get('games_official_boxscore', 0)} "
            f"games  event-label fallback={counters.get('games_event_label', 0)} games"
        )
        # How the book listed each run line: pairs, or two separate bets.
        for market_type, shape in (
            (counters.get("market_shapes") or {}).get("run_lines", {}).items()
        ):
            lines.append(
                f"run line {market_type}: {shape['pairs']} pairs, "
                f"{shape['one_sided_games']} games listed as two separate bets "
                f"({shape['one_sided_records']} records)"
            )
    lines.append("")
    header = (
        f"{'market':<17}{'trust':<14}{'n':>7}{'games':>7}{'simBrier':>10}{'mktBrier':>10}"
        f"{'brierDiff':>11}{'95% range':>18}{'minN':>7}{'pwr':>10}"
    )

    def _fmt_row(r: dict[str, Any]) -> str:
        # At n == 0 the scores and the interval bounds are nan, which _json_safe
        # turned into None; _fmt_or_na prints those, so one empty row never
        # crashes the whole table.
        rng = f"[{_fmt_or_na(r['brier_diff_ci_low'], '.4f')},{_fmt_or_na(r['brier_diff_ci_high'], '.4f')}]"
        pwr = "UNDERPWR" if r.get("underpowered") else "ok"
        return (
            f"{r['group']:<17}{r['trust']:<14}{r['n']:>7}{r['n_games']:>7}"
            f"{_fmt_or_na(r['sim_brier'], '.4f'):>10}{_fmt_or_na(r['market_brier'], '.4f'):>10}"
            f"{_fmt_or_na(r['brier_diff_mean'], '.4f'):>11}{rng:>18}"
            f"{_fmt_min_n(r['min_n_recommended']):>7}{pwr:>10}"
        )

    o = comparison["overall"]
    lines += [
        "--- OVERALL ---",
        header,
        _fmt_row(o),
        "",
        "--- BY MARKET (grouped by trust tier; log-loss in the JSON report) ---",
        header,
    ]
    last_trust = None
    for r in comparison["by_market"]:
        if r["trust"] != last_trust:
            lines.append(f"  [{r['trust']}]")
            last_trust = r["trust"]
        lines.append(_fmt_row(r))
    lines.append("=" * 100)
    return "\n".join(lines)
