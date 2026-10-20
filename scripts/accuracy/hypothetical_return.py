"""The hypothetical dollar return: an opt-in companion table
(``--report-hypothetical-return``) that shows a probability disagreement in money.

For every accuracy record where the simulator's probability differs from the
market's by at least ``--edge-threshold`` (default
:data:`DEFAULT_RETURN_EDGE_THRESHOLD`), the table prices a 1-unit bet on the side
the simulator favours, at the CLOSING price, and reports two numbers per market:

* ``mean_model_ev`` / ``total_model_ev``: :func:`betting.clv_engine.expected_value`
  on the simulator's own probability. It never reads the real outcome: it is the
  model grading its own confidence.
* ``mean_realized_return`` / ``total_realized_return``: what the same bet paid on
  the real outcome (a win pays decimal odds − 1 per unit, a loss −1). Read this
  one: it is the number that checks itself against reality.

When the two agree, the model behaves as it says it does; a large, steady gap
means its confidence does not turn into real outcomes, which the accuracy score
alone shows less plainly. The realized return's interval and minimum sample use
the game-clustered statistics of :mod:`accuracy.confidence`: several bets of one
game are one independent trial. ``report["hypothetical_return"]["certified"]`` is
always ``False``, a machine-readable marker for a reader of the JSON who never
sees the console's caveat.

WHAT THIS DOES NOT CLAIM
------------------------
* A certified betting edge. No betting-value measurement is trusted until every
  band the lanes grade is green (owner ruling 2026-09-08, CLAUDE.md §2b), and
  ``docs/audit/2026-09-10-sim536-541-market-accuracy-plan.md`` §6 proposes a
  stronger gate the owner has not adopted. Read the table as what the model
  believes, not as proof.
* How anyone would bet. Real staking is sized to a bankroll
  (``betting/bet_signal.py``); a real bet transacts at the price available when
  the model can act, a moment the platform keeps no snapshot of; and a real book
  charges more than the quoted price at size. The table holds every other
  variable at its simplest to isolate one question: was the disagreement worth
  anything. "1 unit" is not a dollar amount; multiply by a stake to read it as one.
* That 0.02 is the right disagreement to select on. It is the approximate floor
  of the minimum sample size (:mod:`accuracy.confidence`), used for the same
  reason (no measured edge magnitude exists yet). Shrunk by
  :data:`DETECTION_MARGIN`, it also serves as the realized return's minimum
  detectable effect (:func:`_return_row_for`): a probability-scale constant on a
  return-scale quantity, with no scale check.
* That the fade side's probability is exact on a line that can push (an integer
  total, run line or prop line). ``sim_prob`` excludes the push mass from both
  sides (the convention of ``betting.clv_engine.total_over_under_edge_report``,
  ``spread_cover_prob`` and ``PropDistribution.p_over``), so ``1 - sim_prob``
  includes it and overstates a fade bet's ``model_ev`` by about the push
  probability. A fix needs the push probability on :class:`AccuracyRecord`.
* Full protection against multiple comparisons. The by-market correction counts
  the markets with at least one bet over the threshold: a count the threshold
  picks from the data, unlike the accuracy table's count of every market with a
  record.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from betting.bet_signal import DEFAULT_MIN_EDGE
from betting.clv_engine import american_to_decimal, expected_value

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
from .report import _fmt_min_n

#: The default smallest disagreement a bet needs to enter the table
#: (``--edge-threshold``). It starts at the value of MIN_DETECTABLE_EDGE but is a
#: separate constant: one is the target of a power calculation, the other a
#: selection filter, and a change to one need not move the other.
DEFAULT_RETURN_EDGE_THRESHOLD: float = DEFAULT_MIN_EDGE


@dataclass(frozen=True, slots=True)
class ReturnRecord:
    """One hypothetical 1-unit bet, priced and graded.

    Built only for an :class:`AccuracyRecord` whose disagreement
    (``|sim_prob - market_prob|``) cleared the threshold and that carries both
    closing prices (:func:`score_hypothetical_return`). ``favored_side`` is
    ``"reference"`` when the simulator's probability exceeds the market's (it
    favours the side the record scores), and ``"fade"`` when it is lower (it
    favours the other side of the same two-way market).
    """

    game_pk: int
    market: str
    market_type: str
    disagreement: float
    favored_side: str  # "reference" | "fade"
    #: The expected value per unit at the favoured side's closing price, under the
    #: simulator's own probability (``betting.clv_engine.expected_value``). It
    #: never reads the real outcome.
    model_ev: float
    #: The profit per unit of placing the bet, graded on the real outcome: a win
    #: pays decimal odds − 1, a loss −1.
    realized_return: float
    player_id: int | None = None

    def to_jsonable(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


def _favored_side_prob_and_price(record: AccuracyRecord) -> tuple[float, float, str] | None:
    """The side the simulator favours, its probability, and its closing price (pure).

    Returns ``None`` when a closing price is missing, or when the two
    probabilities are exactly equal (no favoured side; a positive threshold
    already excludes this, and the function stays correct on its own).

    On a line that can push, the fade probability ``1.0 - record.sim_prob``
    includes the push mass and overstates the fade bet's value (see the module
    docstring).
    """
    if record.market_side_price is None or record.market_other_price is None:
        return None
    if record.sim_prob > record.market_prob:
        return record.sim_prob, record.market_side_price, "reference"
    if record.sim_prob < record.market_prob:
        return 1.0 - record.sim_prob, record.market_other_price, "fade"
    return None


def score_hypothetical_return(
    records: Sequence[AccuracyRecord], *, edge_threshold: float
) -> list[ReturnRecord]:
    """The qualifying records as priced, graded bets (pure).

    A record qualifies when its disagreement (``|sim_prob - market_prob|``) is at
    least ``edge_threshold`` and it carries both closing prices. Each gives one
    bet on the favoured side (:func:`_favored_side_prob_and_price`), graded on the
    record's own outcome: a reference bet wins when ``outcome == 1``, a fade bet
    when ``outcome == 0``.
    """
    out: list[ReturnRecord] = []
    for r in records:
        disagreement = abs(r.sim_prob - r.market_prob)
        if disagreement < float(edge_threshold):
            continue
        favored = _favored_side_prob_and_price(r)
        if favored is None:
            continue
        favored_prob, favored_price, favored_side = favored
        model_ev = expected_value(favored_prob, favored_price)
        won = (favored_side == "reference" and r.outcome == 1) or (
            favored_side == "fade" and r.outcome == 0
        )
        realized_return = (american_to_decimal(favored_price) - 1.0) if won else -1.0
        out.append(
            ReturnRecord(
                game_pk=r.game_pk,
                market=r.market,
                market_type=r.market_type,
                disagreement=disagreement,
                favored_side=favored_side,
                model_ev=model_ev,
                realized_return=realized_return,
                player_id=r.player_id,
            )
        )
    return out


def _bootstrap_mean_ci(
    values: np.ndarray, game_pks: np.ndarray, *, n_bootstrap: int, seed: int
) -> tuple[float, float]:
    """The 95% cluster-bootstrap interval of the mean of ``values`` (a per-bet
    realized return). :func:`_bootstrap_paired_diff_ci` does the resampling: its
    name says "diff", but it works on any per-observation array."""
    return _bootstrap_paired_diff_ci(values, game_pks, n_bootstrap=n_bootstrap, seed=seed)


@dataclass(frozen=True, slots=True)
class ReturnComparisonRow:
    """The hypothetical return of one group of bets.

    ``mean_model_ev`` and ``total_model_ev`` never read the real outcome
    (:class:`ReturnRecord`); ``mean_realized_return`` and ``total_realized_return``
    do, and are the numbers to read. ``realized_return_ci_low`` / ``_ci_high`` and
    ``min_n_recommended`` / ``underpowered`` use the game-clustered statistics of
    :mod:`accuracy.confidence` (:func:`_bootstrap_mean_ci`,
    :func:`_minimum_observations_clustered`).
    """

    group: str
    trust: str
    n: int
    n_games: int
    mean_model_ev: float
    total_model_ev: float
    mean_realized_return: float
    total_realized_return: float
    realized_return_ci_low: float
    realized_return_ci_high: float
    min_n_recommended: float
    underpowered: bool
    alpha_used: float

    def to_jsonable(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


def _return_row_for(
    group: str,
    trust: str,
    records: Sequence[ReturnRecord],
    *,
    n_bootstrap: int,
    seed: int,
    alpha: float,
) -> ReturnComparisonRow:
    """One bucket of bets rolled into one row (pure).

    ``min_n_recommended`` uses :data:`MIN_DETECTABLE_EDGE` /
    :data:`DETECTION_MARGIN` as the realized return's minimum detectable effect: a
    probability-scale constant on a per-unit return. The module docstring says why
    that reuse is unexamined.
    """
    n = len(records)
    game_pks = np.array([r.game_pk for r in records], dtype=np.int64)
    evs = np.array([r.model_ev for r in records], dtype=np.float64)
    returns = np.array([r.realized_return for r in records], dtype=np.float64)

    mean_ev = float(evs.mean()) if n else float("nan")
    total_ev = float(evs.sum()) if n else 0.0
    mean_return = float(returns.mean()) if n else float("nan")
    total_return = float(returns.sum()) if n else 0.0

    returns_by_game: dict[int, list[float]] = {}
    for gp, ret in zip(game_pks.tolist(), returns.tolist(), strict=True):
        returns_by_game.setdefault(int(gp), []).append(float(ret))

    ci_lo, ci_hi = _bootstrap_mean_ci(returns, game_pks, n_bootstrap=n_bootstrap, seed=seed)

    shrunk_floor = MIN_DETECTABLE_EDGE / DETECTION_MARGIN
    min_n_or_none = _minimum_observations_clustered(
        returns_by_game, floor=shrunk_floor, alpha=alpha
    )
    min_n = float("inf") if min_n_or_none is None else min_n_or_none
    underpowered = n < min_n

    return ReturnComparisonRow(
        group=group,
        trust=trust,
        n=n,
        n_games=len({r.game_pk for r in records}),
        mean_model_ev=mean_ev,
        total_model_ev=total_ev,
        mean_realized_return=mean_return,
        total_realized_return=total_return,
        realized_return_ci_low=ci_lo,
        realized_return_ci_high=ci_hi,
        min_n_recommended=min_n,
        underpowered=underpowered,
        alpha_used=alpha,
    )


def aggregate_hypothetical_return(
    accuracy_records: Sequence[AccuracyRecord],
    *,
    edge_threshold: float = DEFAULT_RETURN_EDGE_THRESHOLD,
    n_bootstrap: int = DEFAULT_BOOTSTRAP_SAMPLES,
    seed: int = 540,
    base_alpha: float = DEFAULT_ALPHA,
) -> dict[str, Any]:
    """The return report of the accuracy records (pure).

    It keeps the bets that clear ``edge_threshold`` (:func:`score_hypothetical_return`)
    and rolls them up the way the accuracy table does: an ``overall`` row over
    every qualifying bet at the uncorrected ``base_alpha``, plus ``by_market`` rows
    Bonferroni-corrected by the number of markets with a qualifying bet. That
    count depends on the data, unlike the accuracy table's (see the module
    docstring). ``n_accuracy_records`` and ``n_qualifying`` show how much of the
    comparison the table covers: a strict threshold can leave little.
    """
    return_records = score_hypothetical_return(accuracy_records, edge_threshold=edge_threshold)
    overall = _return_row_for(
        "overall", "—", return_records, n_bootstrap=n_bootstrap, seed=seed, alpha=base_alpha
    )

    by_market_key: dict[str, list[ReturnRecord]] = {}
    for r in return_records:
        by_market_key.setdefault(r.market, []).append(r)

    by_market_alpha = _bonferroni_alpha(base_alpha, len(by_market_key))
    rows = [
        _return_row_for(
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
        # A machine-readable "not a betting edge" marker: a script or dashboard
        # that reads this JSON never sees the console's caveat. Always False.
        "certified": False,
        "edge_threshold": float(edge_threshold),
        "n_accuracy_records": len(accuracy_records),
        "n_qualifying": len(return_records),
        "overall": overall.to_jsonable(),
        "by_market": [r.to_jsonable() for r in rows],
    }


def format_hypothetical_return(comparison: dict[str, Any], *, params: dict[str, Any]) -> str:
    """The hypothetical-return report as a text table."""
    lines = [
        "=" * 100,
        "SIM-540 HYPOTHETICAL DOLLAR RETURN  (a diagnostic, NOT a certified betting edge)",
        f"Bets where the sim/market probability disagreed by >= {comparison['edge_threshold']:.4f}",
        f"({comparison['n_qualifying']} of {comparison['n_accuracy_records']} accuracy records",
        "qualify), priced at the CLOSING price, 1 unit flat stake. modelEV never checks the",
        "real outcome (the model grading its own confidence); realizedReturn does -- read",
        'THAT one. See the module docstring\'s "HYPOTHETICAL DOLLAR RETURN" section for the',
        "full scope and the reasons this is not a betting-value certification.",
        "=" * 100,
        f"seasons={params.get('seasons')}  iterations={params.get('iterations')}  "
        f"markets={params.get('markets')}  base_seed={params.get('base_seed')}",
        "",
    ]
    header = (
        f"{'market':<17}{'trust':<14}{'n':>6}{'games':>7}{'modelEV':>10}{'realzdROI':>11}"
        f"{'95% range':>18}{'minN':>7}{'pwr':>10}"
    )

    def _fmt_row(r: dict[str, Any]) -> str:
        rng = f"[{r['realized_return_ci_low']:.4f},{r['realized_return_ci_high']:.4f}]"
        pwr = "UNDERPWR" if r.get("underpowered") else "ok"
        return (
            f"{r['group']:<17}{r['trust']:<14}{r['n']:>6}{r['n_games']:>7}"
            f"{r['mean_model_ev']:>10.4f}{r['mean_realized_return'] * 100:>10.1f}%"
            f"{rng:>18}{_fmt_min_n(r['min_n_recommended']):>7}{pwr:>10}"
        )

    o = comparison["overall"]
    lines += [
        "--- OVERALL ---",
        header,
        _fmt_row(o),
        "",
        "--- BY MARKET (grouped by trust tier) ---",
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
