#!/usr/bin/env python
"""
scripts/sim555_book_sharpness.py — SIM-555: which book's closing line forecasts
the real outcome best? A pure odds-against-outcome read; no simulator.

WHY THIS EXISTS
---------------
The accuracy comparison grades the simulator against ONE book: the first book on
``GRADED_BOOK_PREFERENCE`` with a closing row for the market (owner decision 1 of
the SIM-555 plan). DraftKings led the list until this read ranked the books on
the 2024-2025 re-load; the owner then sets the order from it.

WHAT IT MEASURES
----------------
For every book and every FIXED-LINE market (the full-game, first-inning and
first-five moneylines, the first team to score, a run in the first inning), the
book's closing row gives a probability of the reference side (home, or "yes, a
run scores"), with the book's cut removed (de-vigged). The read scores that
probability against the real outcome over every game the book quoted:

  * the Brier score (the mean squared gap between the probability and the 0/1
    outcome; lower is better) and the log loss (minus the mean log of the
    probability the line gave to what happened; lower is better);
  * a 90% range for the Brier score from a game-clustered bootstrap (the games
    are resampled, not the rows).

The first-inning and first-five moneylines are three-way markets (home, away or a
tie): the home side is scored against the three-way de-vig, and a book row with
no tie price is skipped.

Two markets carry a line, so a book's price depends on the line it posts. For
the totals (every total-kind game market) and the strikeout prop, the read keeps
only the SHARED LINE: the line most sportsbooks posted for that game (and, for a
prop, that pitcher). A book at another line is left out of that game; a game
that lands exactly on the line (a push) is left out for every book.

THE RANKING
-----------
Books are compared on the SAME games. In each market, a sportsbook that quoted
at least ``--min-coverage`` (default 50%) of the market's games is compared, on
the games every compared book quoted (the common-game subset). The ranking is by
Brier score on that subset; the table also shows each book's gap to the leader
with a game-clustered 90% range, so a gap inside its range reads as a tie. The
pooled ranking runs across the fixed-line markets together. Its books are the
books compared in the ANCHOR market, the market the most books quote (the
full-game moneyline on real data). Its units are the (game, market) pairs that
every one of those books quoted: a market a pooled book never quotes drops out
of the pool, and a book is never dropped or promoted for the niche markets it
quotes. The read suggests a preference list only when no book of today's list
is left out of the pool. The vendor's blend (``bp:0``) is scored on
the same games and shown apart: it is never bettable, never graded. The
daily-fantasy apps, the exchanges and the prediction markets are not read.

    MSYS_NO_PATHCONV=1 docker compose run --rm -v "$PWD/scripts:/app/scripts" app \\
        python scripts/sim555_book_sharpness.py --seasons 2024 2025 --out scripts/sim555_book_sharpness.json

The plan: docs/audit/2026-09-25-sim555-one-book-per-odds-row-plan.md §5.6 and §8 step 5.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from betting.clv_engine import (  # noqa: E402
    devig_multiway,
    devig_two_way,
    implied_prob_from_american,
)
from pipeline.odds_provider import (  # noqa: E402
    GAME_MARKET_KIND,
    GRADED_BOOK_PREFERENCE,
    ODDS_ROW_VERSION,
    STORED_BOOK_FILTER_SQL,
    book_display_name,
    book_id_from_label,
    book_kind,
    book_label,
)
from simulation.game_market_distributions import SegmentRuns, market_outcome  # noqa: E402
from simulation.prop_validation import OUTCOME_PROB_EPS  # noqa: E402

#: The markets whose closing price carries no line.
FIXED_LINE_MARKETS: tuple[str, ...] = (
    "moneyline",
    "f1_moneyline",
    "f5_moneyline",
    "first_to_score",
    "first_inning_run",
)

#: The total-kind game markets, read on the shared line.
SHARED_LINE_MARKETS: tuple[str, ...] = tuple(
    m for m, kind in GAME_MARKET_KIND.items() if kind == "total"
)

#: The prop read on the shared line.
STRIKEOUTS = "strikeouts"

#: The vendor's blended line: reported apart, never ranked.
BLEND = book_label(0)

DEFAULT_MIN_COVERAGE = 0.5
DEFAULT_BOOTSTRAP = 2000
DEFAULT_SEED = 555
RANGE_LEVEL = 0.90
#: Bootstrap draws per chunk (bounds the memory of one chunk to draws × games).
_BOOT_CHUNK = 250


@dataclass(frozen=True)
class GameResult:
    """One Final game's real result: the final score and the per-inning grid."""

    home_score: int
    away_score: int
    grid: Mapping[str, Sequence[int]] | None = None


@dataclass(frozen=True)
class Obs:
    """One book's probability of the reference side of one unit, and the real outcome.

    ``unit`` names what was forecast: ``(game_pk, market)`` for a game market,
    ``(game_pk, player_id)`` for the strikeout prop. The bootstrap resamples
    ``game_pk``.
    """

    unit: tuple[int, Any]
    game_pk: int
    market: str
    book: str
    prob: float
    outcome: int


# ---------------------------------------------------------------------------
# PURE: probabilities, outcomes, observations
# ---------------------------------------------------------------------------


def reference_prob(market_type: str, row: Mapping[str, Any]) -> float | None:
    """The de-vigged probability of the reference side of one closing row, or ``None``.

    Home on a moneyline and the first team to score (two-way); home on a
    three-way moneyline (home, away and the tie de-vigged together; ``None``
    without a tie price); the over on a total-kind market and "yes" on a run in
    the first inning (two-way). A missing side or a degenerate price gives ``None``.
    """
    kind = GAME_MARKET_KIND.get(market_type)
    try:
        if kind in ("moneyline", "three_way"):
            home, away = row.get("home_ml"), row.get("away_ml")
            if home is None or away is None:
                return None
            if kind == "three_way":
                draw = row.get("draw_ml")
                if draw is None:
                    return None
                fair = devig_multiway(
                    [
                        implied_prob_from_american(float(home)),
                        implied_prob_from_american(float(away)),
                        implied_prob_from_american(float(draw)),
                    ]
                )
                return float(fair[0])
            return float(devig_two_way(float(home), float(away))[0])
        if kind in ("total", "yes_no"):
            over, under = row.get("over_ml"), row.get("under_ml")
            if over is None or under is None:
                return None
            return float(devig_two_way(float(over), float(under))[0])
    except ValueError:
        return None
    return None


def game_outcome(market_type: str, result: GameResult, line: float | None = None) -> int | None:
    """The 0/1 outcome of the reference side, or ``None`` (a push, or no grid).

    The full-game moneyline and total read the final score (as the accuracy
    comparison does); every other market reads the official per-inning grid
    through :func:`simulation.game_market_distributions.market_outcome`.
    """
    if market_type == "moneyline":
        margin = result.home_score - result.away_score
        return None if margin == 0 else int(margin > 0)
    if market_type == "total":
        if line is None:
            return None
        total = float(result.home_score + result.away_score)
        return None if total == float(line) else int(total > float(line))
    if result.grid is None:
        return None
    actual = SegmentRuns.from_official_grid(dict(result.grid))
    return market_outcome(actual, market_type, line=line)


def _scored_kind(book: str) -> bool:
    """True for the books the read scores: the sportsbooks and the blend."""
    return book_kind(book) == "sportsbook" or book == BLEND


def fixed_line_observations(
    rows: Iterable[Mapping[str, Any]],
    results: Mapping[int, GameResult],
    skipped: Counter[str] | None = None,
) -> list[Obs]:
    """PURE: one observation per (game, fixed-line market, book) closing row.

    ``skipped`` counts the rows left out, by reason and book.
    """
    skipped = skipped if skipped is not None else Counter()
    out: list[Obs] = []
    for row in rows:
        market, book = str(row["market_type"]), str(row["book"])
        if market not in FIXED_LINE_MARKETS:
            continue
        if not _scored_kind(book):
            skipped[f"not_scored_kind:{book}"] += 1
            continue
        game_pk = int(row["game_pk"])
        result = results.get(game_pk)
        if result is None:
            skipped[f"no_result:{market}"] += 1
            continue
        prob = reference_prob(market, row)
        if prob is None:
            skipped[f"unpriced:{market}:{book}"] += 1
            continue
        outcome = game_outcome(market, result, 0.5 if market == "first_inning_run" else None)
        if outcome is None:
            skipped[f"no_outcome:{market}"] += 1
            continue
        out.append(Obs((game_pk, market), game_pk, market, book, prob, int(outcome)))
    return out


def modal_line(lines: Iterable[float]) -> float | None:
    """The most common line; a tie goes to the smallest. ``None`` for no lines."""
    counts = Counter(float(x) for x in lines)
    if not counts:
        return None
    return min(counts, key=lambda line: (-counts[line], line))


def shared_line_observations(
    rows: Iterable[Mapping[str, Any]],
    results: Mapping[int, GameResult],
    skipped: Counter[str] | None = None,
) -> list[Obs]:
    """PURE: the total-kind observations at each game's shared line.

    The shared line is the modal closing line of the sportsbooks on that game
    and market. A row at another line is left out; a push is left out for
    every book.
    """
    skipped = skipped if skipped is not None else Counter()
    groups: dict[tuple[int, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        market = str(row["market_type"])
        if market not in SHARED_LINE_MARKETS or not _scored_kind(str(row["book"])):
            continue
        if row.get("total_line") is None:
            continue
        groups.setdefault((int(row["game_pk"]), market), []).append(row)
    out: list[Obs] = []
    for (game_pk, market), group in groups.items():
        line = modal_line(
            float(r["total_line"]) for r in group if book_kind(str(r["book"])) == "sportsbook"
        )
        result = results.get(game_pk)
        if line is None or result is None:
            skipped[f"no_line_or_result:{market}"] += 1
            continue
        outcome = game_outcome(market, result, line)
        if outcome is None:
            skipped[f"push_or_no_grid:{market}"] += 1
            continue
        for r in group:
            book = str(r["book"])
            if float(r["total_line"]) != line:
                skipped[f"off_shared_line:{market}:{book}"] += 1
                continue
            prob = reference_prob(market, r)
            if prob is None:
                skipped[f"unpriced:{market}:{book}"] += 1
                continue
            out.append(Obs((game_pk, market), game_pk, market, book, prob, int(outcome)))
    return out


def strikeout_observations(
    rows: Iterable[Mapping[str, Any]],
    strikeouts: Mapping[tuple[int, int], int],
    skipped: Counter[str] | None = None,
) -> list[Obs]:
    """PURE: the strikeout-prop observations at each pitcher's shared line.

    ``strikeouts`` maps ``(game_pk, player_id)`` to the pitcher's real total
    from the official box score; a pitcher without one did not pitch (the book
    voids the bet) and is left out.
    """
    skipped = skipped if skipped is not None else Counter()
    groups: dict[tuple[int, int], list[Mapping[str, Any]]] = {}
    for row in rows:
        if not _scored_kind(str(row["book"])) or row.get("line") is None:
            continue
        groups.setdefault((int(row["game_pk"]), int(row["player_id"])), []).append(row)
    out: list[Obs] = []
    for key, group in groups.items():
        actual = strikeouts.get(key)
        if actual is None:
            skipped["did_not_pitch"] += 1
            continue
        line = modal_line(
            float(r["line"]) for r in group if book_kind(str(r["book"])) == "sportsbook"
        )
        if line is None:
            continue
        if float(actual) == line:
            skipped["push"] += 1
            continue
        outcome = int(float(actual) > line)
        for r in group:
            book = str(r["book"])
            if float(r["line"]) != line:
                skipped[f"off_shared_line:{STRIKEOUTS}:{book}"] += 1
                continue
            over, under = r.get("over_ml"), r.get("under_ml")
            try:
                if over is None or under is None:
                    raise ValueError("a side is missing")
                prob = float(devig_two_way(float(over), float(under))[0])
            except ValueError:
                skipped[f"unpriced:{STRIKEOUTS}:{book}"] += 1
                continue
            out.append(Obs(key, key[0], STRIKEOUTS, book, prob, outcome))
    return out


# ---------------------------------------------------------------------------
# PURE: scores, the bootstrap and the ranking
# ---------------------------------------------------------------------------


def _brier(obs: Sequence[Obs]) -> float:
    return float(np.mean([(o.prob - o.outcome) ** 2 for o in obs])) if obs else float("nan")


def _log_loss(obs: Sequence[Obs]) -> float:
    if not obs:
        return float("nan")
    p = np.clip(np.array([o.prob for o in obs]), OUTCOME_PROB_EPS, 1.0 - OUTCOME_PROB_EPS)
    y = np.array([o.outcome for o in obs], dtype=float)
    return float(-np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)))


def score(obs: Sequence[Obs]) -> dict[str, Any]:
    """n, the Brier score and the log loss of a set of observations."""
    return {"n": len(obs), "brier": _brier(obs), "log_loss": _log_loss(obs)}


def clustered_range(
    values: Sequence[float],
    games: Sequence[int],
    *,
    n_boot: int = DEFAULT_BOOTSTRAP,
    seed: int = DEFAULT_SEED,
    level: float = RANGE_LEVEL,
) -> tuple[float, float] | None:
    """PURE: a ``level`` range for the mean of ``values``, resampling whole games.

    Each draw takes as many games as the data holds, with replacement, and
    averages every value of the drawn games. ``None`` with fewer than two games.
    """
    by_game: dict[int, list[float]] = {}
    for v, g in zip(values, games, strict=True):
        by_game.setdefault(int(g), []).append(float(v))
    if len(by_game) < 2 or n_boot < 1:
        return None
    sums = np.array([sum(v) for v in by_game.values()])
    counts = np.array([len(v) for v in by_game.values()], dtype=float)
    n_games = len(sums)
    rng = np.random.default_rng(seed)
    means: list[np.ndarray] = []
    done = 0
    while done < n_boot:
        k = min(_BOOT_CHUNK, n_boot - done)
        idx = rng.integers(0, n_games, size=(k, n_games))
        means.append(sums[idx].sum(axis=1) / counts[idx].sum(axis=1))
        done += k
    all_means = np.concatenate(means)
    tail = (1.0 - level) / 2.0
    return float(np.quantile(all_means, tail)), float(np.quantile(all_means, 1.0 - tail))


def _preference_key(book: str) -> tuple[int, int]:
    """Where a book sits on GRADED_BOOK_PREFERENCE (the tie-break of a ranking)."""
    book_id = book_id_from_label(book)
    if book_id in GRADED_BOOK_PREFERENCE:
        return (GRADED_BOOK_PREFERENCE.index(book_id), 0)
    return (len(GRADED_BOOK_PREFERENCE), -1 if book_id is None else book_id)


def _book_entry(book: str) -> dict[str, Any]:
    return {"book": book, "name": book_display_name(book), "kind": book_kind(book)}


def rank_books(
    obs: Sequence[Obs],
    *,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    n_boot: int = DEFAULT_BOOTSTRAP,
    seed: int = DEFAULT_SEED,
    books: Sequence[str] | None = None,
    why_not: str = "not in the pool",
) -> dict[str, Any]:
    """PURE: the per-book scores and the ranking on the common units of one set of observations.

    ``books`` fixes the compared books (the pooled read passes them, and
    ``why_not`` says why the other sportsbooks are not compared); otherwise
    every sportsbook that quoted at least ``min_coverage`` of the units is
    compared. The result holds, per sportsbook, its scores over every unit it
    quoted; the compared books, the common units and the ranking on them (with
    each book's Brier gap to the leader and its game-clustered range); and the
    blend's scores on the common units it quoted.
    """
    by_book: dict[str, dict[tuple[int, Any], Obs]] = {}
    for o in obs:
        by_book.setdefault(o.book, {})[o.unit] = o
    sportsbooks = sorted((b for b in by_book if book_kind(b) == "sportsbook"), key=_preference_key)
    all_units: set[tuple[int, Any]] = set()
    for b in sportsbooks:
        all_units |= set(by_book[b])
    n_units = len(all_units)
    per_book: dict[str, Any] = {}
    for b in sportsbooks:
        rows = list(by_book[b].values())
        entry = _book_entry(b) | score(rows)
        entry["coverage"] = len(rows) / n_units if n_units else 0.0
        entry["brier_range90"] = clustered_range(
            [(o.prob - o.outcome) ** 2 for o in rows],
            [o.game_pk for o in rows],
            n_boot=n_boot,
            seed=seed,
        )
        per_book[b] = entry
    if books is None:
        compared = [b for b in sportsbooks if per_book[b]["coverage"] >= min_coverage]
        why = f"coverage below {min_coverage:.0%}"
    else:
        compared = [b for b in sorted(books, key=_preference_key) if b in by_book]
        why = why_not
    excluded = {
        b: f"{why} (coverage {per_book[b]['coverage']:.0%})"
        for b in sportsbooks
        if b not in compared
    }
    common: set[tuple[int, Any]] = (
        set.intersection(*(set(by_book[b]) for b in compared)) if compared else set()
    )
    units = sorted(common, key=lambda u: (u[0], str(u[1])))
    ranking: list[dict[str, Any]] = []
    for b in compared:
        rows = [by_book[b][u] for u in units]
        ranking.append(_book_entry(b) | score(rows))
    ranking.sort(key=lambda e: (e["brier"], _preference_key(e["book"])))
    if ranking and units:
        leader = ranking[0]["book"]
        lead_sq = [(by_book[leader][u].prob - by_book[leader][u].outcome) ** 2 for u in units]
        for rank, entry in enumerate(ranking, start=1):
            entry["rank"] = rank
            b = entry["book"]
            diffs = [
                (by_book[b][u].prob - by_book[b][u].outcome) ** 2 - lead
                for u, lead in zip(units, lead_sq, strict=True)
            ]
            entry["brier_gap_to_leader"] = float(np.mean(diffs))
            entry["gap_range90"] = (
                None
                if b == leader
                else clustered_range(diffs, [u[0] for u in units], n_boot=n_boot, seed=seed)
            )
    blend: dict[str, Any] | None = None
    if BLEND in by_book:
        blend_units = [u for u in units if u in by_book[BLEND]]
        blend = _book_entry(BLEND) | score([by_book[BLEND][u] for u in blend_units])
        blend["all_units"] = score(list(by_book[BLEND].values()))
        if ranking and blend_units:
            leader = ranking[0]["book"]
            diffs = [
                (by_book[BLEND][u].prob - by_book[BLEND][u].outcome) ** 2
                - (by_book[leader][u].prob - by_book[leader][u].outcome) ** 2
                for u in blend_units
            ]
            blend["brier_gap_to_leader"] = float(np.mean(diffs))
            blend["gap_range90"] = clustered_range(
                diffs, [u[0] for u in blend_units], n_boot=n_boot, seed=seed
            )
    return {
        "n_units": n_units,
        "books": per_book,
        "compared": compared,
        "excluded": excluded,
        "n_common_units": len(units),
        "n_common_games": len({u[0] for u in units}),
        "ranking": ranking,
        "blend": blend,
    }


def pooled_ranking(
    obs_by_market: Mapping[str, Sequence[Obs]],
    markets: Sequence[str] = FIXED_LINE_MARKETS,
    *,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    n_boot: int = DEFAULT_BOOTSTRAP,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """PURE: the ranking across several markets at once, on the units every pooled book quoted.

    The pool's books are the books compared (coverage at least
    ``min_coverage``) in its ANCHOR market: the market with the most compared
    books (a tie goes to the earlier market in ``markets``; on real data it is
    the full-game moneyline). The ranking runs on the units every pooled book
    quoted, across all the markets. A market that a pooled book never quoted
    drops out of those common units (``dropped_markets`` names the books that
    lack it); a market that a pooled book quoted on part of the games keeps
    those games only. So a book is never dropped because it skips a niche
    market, and a book is never promoted because it quotes every market. A
    book compared in another market but not in the anchor is listed in
    ``left_out``. The bootstrap resamples games across the markets together.
    """
    used = [m for m in markets if obs_by_market.get(m)]
    compared_by_market = {
        m: list(
            rank_books(obs_by_market[m], min_coverage=min_coverage, n_boot=0, seed=seed)["compared"]
        )
        for m in used
    }
    candidates = [m for m in used if compared_by_market[m]]
    anchor = (
        max(candidates, key=lambda m: (len(compared_by_market[m]), -used.index(m)))
        if candidates
        else None
    )
    books = sorted(compared_by_market[anchor], key=_preference_key) if anchor else []
    everyone: set[str] = set()
    for compared in compared_by_market.values():
        everyone |= set(compared)
    # A book compared somewhere but not in the anchor: the markets that compare it.
    left_out = {
        b: [m for m in used if b in compared_by_market[m]]
        for b in sorted(everyone - set(books), key=_preference_key)
    }
    pooled_obs = [o for m in used for o in obs_by_market[m] if o.book in books or o.book == BLEND]
    units_of = {b: {o.unit for o in pooled_obs if o.book == b} for b in books}
    common: set[tuple[int, Any]] = set.intersection(*units_of.values()) if books else set()
    units_by_market: Counter[str] = Counter(
        o.market for o in pooled_obs if books and o.book == books[0] and o.unit in common
    )
    dropped_markets: dict[str, str] = {}
    for m in used:
        if units_by_market[m]:
            continue
        lacking = [b for b in books if not any(o.book == b for o in obs_by_market[m])]
        dropped_markets[m] = (
            f"never quoted by {', '.join(lacking)}"
            if lacking
            else "no game that every pooled book quoted"
        )
    read = (
        rank_books(
            pooled_obs,
            books=books,
            n_boot=n_boot,
            seed=seed,
            why_not=f"not compared in {anchor}, the pool's anchor market",
        )
        if books
        else None
    )
    return {
        "markets": used,
        "anchor": anchor,
        "books": books,
        "units_by_market": {m: int(units_by_market[m]) for m in used},
        "dropped_markets": dropped_markets,
        "left_out": left_out,
        "read": read,
    }


def suggested_preference(pooled: Mapping[str, Any]) -> tuple[list[int] | None, str]:
    """PURE: the pooled ranking as a preference list, or ``None`` and the reason.

    ``None`` when the pool ranks no book on common units, or when a book on
    ``GRADED_BOOK_PREFERENCE`` is compared in some fixed-line market but left
    out of the pool: that book may be sharper than every book ranked, so no
    order is suggested (read the per-market tables). Otherwise the ranked
    books in their order, then the books of today's list that no market
    compares (too few quotes to rank) in today's order.
    """
    read = pooled.get("read") or {}
    ranking = read.get("ranking") or []
    if not ranking or not read.get("n_common_units"):
        return None, "the pool ranks no book on common games"
    left = [
        b for b in pooled.get("left_out", {}) if book_id_from_label(b) in GRADED_BOOK_PREFERENCE
    ]
    if left:
        named = ", ".join(
            f"{book_display_name(b)} ({b}, compared in {', '.join(pooled['left_out'][b])})"
            for b in left
        )
        return None, (
            f"{named} on today's list is left out of the pool (not compared in "
            f"{pooled.get('anchor')}, the anchor market): read the per-market tables"
        )
    order: list[int] = []
    for entry in ranking:
        book_id = book_id_from_label(entry["book"])
        if book_id is not None and book_id not in order:
            order.append(book_id)
    unranked = [b for b in GRADED_BOOK_PREFERENCE if b not in order]
    order.extend(unranked)
    note = "the pooled ranking"
    if unranked:
        note += (
            ", then "
            + ", ".join(book_label(b) for b in unranked)
            + " (too few quotes to rank) in today's order"
        )
    return order, note


def build_report(
    game_rows: Sequence[Mapping[str, Any]],
    prop_rows: Sequence[Mapping[str, Any]],
    results: Mapping[int, GameResult],
    strikeouts: Mapping[tuple[int, int], int],
    *,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    n_boot: int = DEFAULT_BOOTSTRAP,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """PURE: the whole read from the rows, the results and the strikeout totals."""
    skipped: Counter[str] = Counter()
    obs_by_market: dict[str, list[Obs]] = {}
    for o in fixed_line_observations(game_rows, results, skipped):
        obs_by_market.setdefault(o.market, []).append(o)
    for o in shared_line_observations(game_rows, results, skipped):
        obs_by_market.setdefault(o.market, []).append(o)
    for o in strikeout_observations(prop_rows, strikeouts, skipped):
        obs_by_market.setdefault(o.market, []).append(o)
    markets: dict[str, Any] = {}
    for m in (*FIXED_LINE_MARKETS, *SHARED_LINE_MARKETS, STRIKEOUTS):
        if obs_by_market.get(m):
            markets[m] = rank_books(
                obs_by_market[m], min_coverage=min_coverage, n_boot=n_boot, seed=seed
            )
    pooled = pooled_ranking(
        obs_by_market, FIXED_LINE_MARKETS, min_coverage=min_coverage, n_boot=n_boot, seed=seed
    )
    suggestion, note = suggested_preference(pooled)
    return {
        "markets": markets,
        "pooled_fixed_line": pooled,
        "suggested_preference": suggestion,
        "suggestion_note": note,
        "skipped": dict(sorted(skipped.items())),
    }


# ---------------------------------------------------------------------------
# Database reads (asyncpg; SELECT only, one read-only transaction)
# ---------------------------------------------------------------------------

GAME_ROWS_SQL = f"""
SELECT DISTINCT ON (o.game_pk, o.market_type, o.book)
       o.game_pk, o.market_type, o.book,
       o.home_ml, o.away_ml, o.draw_ml, o.total_line, o.over_ml, o.under_ml
FROM raw.game_odds o
JOIN raw.games g ON g.game_pk = o.game_pk
WHERE g.season = ANY($1::int[]) AND g.status = 'Final'
  AND o.line_type = 'closing' AND o.{STORED_BOOK_FILTER_SQL}
  AND o.market_type = ANY($2::varchar[])
ORDER BY o.game_pk, o.market_type, o.book, o.fetched_at DESC
"""

PROP_ROWS_SQL = f"""
SELECT DISTINCT ON (o.game_pk, o.player_id, o.book)
       o.game_pk, o.player_id, o.book, o.line, o.over_ml, o.under_ml
FROM raw.prop_odds o
JOIN raw.games g ON g.game_pk = o.game_pk
WHERE g.season = ANY($1::int[]) AND g.status = 'Final'
  AND o.prop_stat = 'strikeouts' AND o.line_type = 'closing' AND o.{STORED_BOOK_FILTER_SQL}
ORDER BY o.game_pk, o.player_id, o.book, o.fetched_at DESC
"""

RESULTS_SQL = """
SELECT game_pk, home_score_final, away_score_final, inning_scores
FROM raw.games
WHERE season = ANY($1::int[]) AND status = 'Final'
  AND home_score_final IS NOT NULL AND away_score_final IS NOT NULL
"""

STRIKEOUTS_SQL = """
SELECT game_pk, player_id, p_k
FROM raw.game_player_stats
WHERE season = ANY($1::int[]) AND played_pitch
"""


def _numeric_row(record: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in dict(record).items():
        if k in ("game_pk", "player_id"):
            out[k] = int(v)
        elif k in ("market_type", "book"):
            out[k] = str(v)
        else:
            out[k] = None if v is None else float(v)
    return out


def _grid(value: Any) -> dict[str, list[int]] | None:
    if value is None:
        return None
    grid = json.loads(value) if isinstance(value, str) else value
    if not isinstance(grid, dict) or "home" not in grid or "away" not in grid:
        return None
    return {"home": list(grid["home"]), "away": list(grid["away"])}


async def load_rows(
    dsn: str, seasons: Sequence[int]
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], dict[int, GameResult], dict[tuple[int, int], int]
]:
    """The closing rows, the results and the strikeout totals of the seasons."""
    import asyncpg

    markets = [*FIXED_LINE_MARKETS, *SHARED_LINE_MARKETS]
    conn = await asyncpg.connect(dsn, timeout=60)
    try:
        async with conn.transaction(readonly=True):
            game_rows = [
                _numeric_row(r) for r in await conn.fetch(GAME_ROWS_SQL, list(seasons), markets)
            ]
            prop_rows = [_numeric_row(r) for r in await conn.fetch(PROP_ROWS_SQL, list(seasons))]
            results = {
                int(r["game_pk"]): GameResult(
                    int(r["home_score_final"]),
                    int(r["away_score_final"]),
                    _grid(r["inning_scores"]),
                )
                for r in await conn.fetch(RESULTS_SQL, list(seasons))
            }
            strikeouts = {
                (int(r["game_pk"]), int(r["player_id"])): int(r["p_k"])
                for r in await conn.fetch(STRIKEOUTS_SQL, list(seasons))
            }
    finally:
        await conn.close()
    return game_rows, prop_rows, results, strikeouts


# ---------------------------------------------------------------------------
# The printout
# ---------------------------------------------------------------------------


def _fmt_range(r: Sequence[float] | None) -> str:
    return "" if r is None else f"[{r[0]:+.4f}, {r[1]:+.4f}]"


def format_read(title: str, read: Mapping[str, Any]) -> str:
    """One market's (or the pool's) table."""
    lines = [
        f"== {title}: {read['n_units']} units; {len(read['compared'])} books compared on "
        f"{read['n_common_units']} common units ({read['n_common_games']} games)"
    ]
    lines.append(
        "  rank  book            label    n      Brier    log loss  gap to leader [90% range]"
    )
    for e in read["ranking"]:
        lines.append(
            f"  {e.get('rank', '-'):>4}  {e['name'][:14]:14s}  {e['book']:7s}  {e['n']:5d}  "
            f"{e['brier']:.5f}  {e['log_loss']:.5f}  {e.get('brier_gap_to_leader', 0.0):+.5f} "
            f"{_fmt_range(e.get('gap_range90'))}"
        )
    blend = read.get("blend")
    if blend and blend.get("n"):
        lines.append(
            f"  blend {blend['name'][:14]:14s}  {blend['book']:7s}  {blend['n']:5d}  "
            f"{blend['brier']:.5f}  {blend['log_loss']:.5f}  "
            f"{blend.get('brier_gap_to_leader', 0.0):+.5f} {_fmt_range(blend.get('gap_range90'))}"
            "  (never graded)"
        )
    for b, why in read["excluded"].items():
        e = read["books"][b]
        lines.append(
            f"  not compared: {e['name']} ({b}), {why}; alone: n {e['n']}, Brier {e['brier']:.5f}"
        )
    return "\n".join(lines)


def format_report(report: Mapping[str, Any]) -> str:
    parts = [format_read(m, read) for m, read in report["markets"].items()]
    pooled = report["pooled_fixed_line"]
    if pooled["read"] is not None:
        parts.append(
            format_read(
                f"pooled fixed-line (anchor {pooled['anchor']}; common units by market "
                f"{pooled['units_by_market']})",
                pooled["read"],
            )
        )
    else:
        parts.append("== pooled fixed-line: no market compares a sportsbook")
    if pooled["dropped_markets"]:
        parts.append(f"  markets with no common unit in the pool: {pooled['dropped_markets']}")
    if pooled["left_out"]:
        parts.append(
            f"  left out of the pool (not compared in {pooled['anchor']}; compared in): "
            f"{pooled['left_out']}"
        )
    suggestion = report["suggested_preference"]
    if suggestion is None:
        parts.append(
            f"Suggested GRADED_BOOK_PREFERENCE: no suggestion — {report['suggestion_note']} "
            f"(today: {GRADED_BOOK_PREFERENCE})"
        )
    else:
        parts.append(
            f"Suggested GRADED_BOOK_PREFERENCE ({report['suggestion_note']}): "
            f"{tuple(suggestion)} (today: {GRADED_BOOK_PREFERENCE})"
        )
    return "\n\n".join(parts)


async def _main_async(args: argparse.Namespace) -> int:
    game_rows, prop_rows, results, strikeouts = await load_rows(args.dsn, args.seasons)
    if not game_rows and not prop_rows:
        print(
            f"no bp: closing rows for the seasons {list(args.seasons)}: "
            "run this read after the re-load of those seasons"
        )
    report = build_report(
        game_rows,
        prop_rows,
        results,
        strikeouts,
        min_coverage=args.min_coverage,
        n_boot=args.bootstrap,
        seed=args.seed,
    )
    report["params"] = {
        "seasons": list(args.seasons),
        "min_coverage": args.min_coverage,
        "bootstrap_samples": args.bootstrap,
        "bootstrap_seed": args.seed,
        "range_level": RANGE_LEVEL,
        "odds_row_version": ODDS_ROW_VERSION,
        "graded_book_preference": list(GRADED_BOOK_PREFERENCE),
        "n_game_rows": len(game_rows),
        "n_prop_rows": len(prop_rows),
        "n_games_with_result": len(results),
    }
    print(format_report(report))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        print(f"\nwrote {args.out}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--seasons", type=int, nargs="+", default=[2024, 2025])
    ap.add_argument("--out", default="", help="write the JSON report here")
    ap.add_argument(
        "--min-coverage",
        type=float,
        default=DEFAULT_MIN_COVERAGE,
        help="the share of a market's units a book must quote to be compared (default 0.5)",
    )
    ap.add_argument("--bootstrap", type=int, default=DEFAULT_BOOTSTRAP)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--dsn", default=os.environ.get("BASEBALL_DB_DSN", ""))
    args = ap.parse_args(argv)
    if not args.dsn:
        ap.error("no DSN: pass --dsn or set BASEBALL_DB_DSN")
    return args


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
