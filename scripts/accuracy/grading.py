"""What really happened, and one accuracy record per priced market.

Every scorer reads the CLOSING price and scores a FIXED reference side (home on a
moneyline and a run line, over on a total and a prop), never the side the model
would have bet. The simulator's probability and the market's de-vigged
probability are scored against the same real outcome. A push (the real value
lands on the line) gives no record: the scored probability is a strict over or
cover probability, so a push has no 0/1 label.

THE PROP GROUND TRUTH
---------------------
A prop is scored against what the player really did. The official box score
(``raw.game_player_stats``; one row per player who appeared) grades every priced
market, RBI and ER included, and a player who did not play is absent, so his bet
is skipped the way the book voids it. The box comes first by the owner's ruling
on the box-score audit (SIM-545). A game with no box-score rows falls back to the
event labels of ``raw.pitches.events``, which carry only H/HR/TB and K/BB, so a
fallback game grades those five props. The run counts games per source
(``counters.games_official_boxscore`` and ``games_event_label``), so a report
graded mostly on the fallback says it is a five-prop report.

A RUN LINE IS A PAIR, OR TWO SEPARATE BETS (SIM-549)
----------------------------------------------------
The store keeps each team's run line under its own spread. A PAIR is one bet with
two sides, such as home −0.5 at −115 and away +0.5 at −105: exactly one side
wins, so the two prices de-vig against each other. TWO SEPARATE BETS share a sign
or carry two different lines, such as home −1.5 at +350 and away −1.5 at +150:
both lose on a tie or a one-run lead. Pairing their prices inflates each side's
probability; on the 2024 first-five run line the inflation was about a fifth, the
whole of the simulator's apparent lead over the line.

The rule: a pair if and only if the away spread is the negative of the home
spread (the pick'em 0 / 0 is a pair). Each separate bet is priced from its own
price over the book's margin on the same game's two-way markets
(:func:`accuracy.pricing.reference_margin`). The plan:
``docs/audit/2026-09-23-sim549-first-five-run-line-two-bets-plan.md``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

from betting.clv_engine import (
    REFERENCE_MARGIN_BAND,
    MarketSide,
    OddsQuote,
    TwoWayMarket,
    devig_multiway,
    devig_one_sided,
    devig_two_way,
    moneyline_edge_report,
    prob_to_american,
    prop_edge_report,
    run_line_bet_cover_prob,
    run_line_is_pair,
    total_over_under_edge_report,
)
from pipeline.odds_provider import (
    GAME_MARKET_KIND,
    GAME_MARKET_SEGMENT,
    GAME_MARKET_TYPES,
    LEGACY_GAME_MARKET_TYPES,
)
from simulation.game_market_distributions import (
    SegmentRuns,
    market_outcome,
    market_probability,
)
from simulation.prop_validation import (
    real_props_from_boxscore_rows,
    real_props_from_pa_events,
)

from .pricing import ClosingPrices, _closing_prices, _implied, reference_margin
from .records import (
    BOXSCORE_SCORED_PROPS,
    EVENT_LABEL_SCORED_PROPS,
    GROUND_TRUTH_EVENT_LABEL,
    GROUND_TRUTH_OFFICIAL,
    PROP_VOCAB_MAP,
    RUN_LINE_MARKET_TYPES,
    AccuracyRecord,
    PropGroundTruth,
    run_line_market_key,
)

log = logging.getLogger("accuracy_comparison")

# ---------------------------------------------------------------------------
# The real outcomes
# ---------------------------------------------------------------------------


async def _fetch_final_score(pool: Any, game_pk: int) -> tuple[int, int] | None:
    """The real final score of one game, or None when it is missing.

    It reads the columns the game list gates on (``home_score_final``,
    ``away_score_final``), per game. The parent does not pass the scores through
    the worker parameters: those are pickled once per submitted game, and a whole
    season's scores in them would copy a growing dict on every submission.
    """
    row = await pool.fetchrow(
        "SELECT home_score_final, away_score_final FROM raw.games WHERE game_pk = $1",
        int(game_pk),
    )
    if row is None or row["home_score_final"] is None or row["away_score_final"] is None:
        return None
    return int(row["home_score_final"]), int(row["away_score_final"])


async def _fetch_official_grid(pool: Any, game_pk: int) -> dict[str, list[int]] | None:
    """The official per-inning run grid of one game (``raw.games.inning_scores`` =
    ``{"home": [...], "away": [...]}``), or ``None`` when the game has none. The
    segment markets (first inning, first five innings, first team to score) are
    graded against it."""
    row = await pool.fetchrow(
        "SELECT inning_scores FROM raw.games WHERE game_pk = $1", int(game_pk)
    )
    if row is None or row["inning_scores"] is None:
        return None
    grid = row["inning_scores"]
    if isinstance(grid, str):
        grid = json.loads(grid)
    if not isinstance(grid, dict) or "home" not in grid or "away" not in grid:
        return None
    return {"home": list(grid["home"]), "away": list(grid["away"])}


async def _fetch_pa_events(pool: Any, game_pk: int) -> list[tuple]:
    """One ``(batter, pitcher, events)`` tuple per completed plate appearance (the
    reader of ``scripts/validate_props.py``), for
    :func:`simulation.prop_validation.real_props_from_pa_events`.

    It is the fallback ground truth, read only for a game with no official
    box-score rows (:func:`_fetch_prop_ground_truth`)."""
    rows = await pool.fetch(
        "SELECT batter, pitcher, events FROM raw.pitches "
        "WHERE game_pk = $1 AND events IS NOT NULL AND events <> ''",
        int(game_pk),
    )
    return [(r["batter"], r["pitcher"], r["events"]) for r in rows]


#: Every column of ``raw.game_player_stats`` (migration 0023), read in full so
#: :func:`simulation.prop_validation.real_props_from_boxscore_rows` sees the whole
#: row and a new prop needs no second SELECT.
_OFFICIAL_BOXSCORE_SQL = """
    SELECT game_pk, player_id, team_id, side, season, game_date,
           batting_order, position_code, played_bat, played_pitch,
           pa, ab, r, h, b2, b3, hr, rbi, sb, cs, bb, k, hbp, sf, tb,
           p_outs, p_h, p_r, p_er, p_bb, p_k, p_hr, p_pitches,
           p_batters_faced, p_started, fetched_at
    FROM raw.game_player_stats
    WHERE game_pk = $1
"""

#: Set once the process has warned that the box-score table is missing (migration
#: 0023 not applied); every game would log the same line otherwise. After the one
#: warning every game falls back to the event label, and the report's per-source
#: counts say so.
_OFFICIAL_BOXSCORE_TABLE_MISSING_WARNED: bool = False


async def _fetch_official_boxscore(pool: Any, game_pk: int) -> list[dict[str, Any]]:
    """Every ``raw.game_player_stats`` row of one game: the official per-player box
    score, one row per player who appeared.

    Plain dicts keyed by the table's column names (the shape
    :func:`simulation.prop_validation.real_props_from_boxscore_rows` reads). An
    empty list means the game has no box-score rows, and the caller falls back to
    the event label. A missing TABLE also reads as empty: the run then grades every
    game on the fallback, warns once, and the report's per-source counts show zero
    games on the official record.
    """
    global _OFFICIAL_BOXSCORE_TABLE_MISSING_WARNED
    import asyncpg

    try:
        rows = await pool.fetch(_OFFICIAL_BOXSCORE_SQL, int(game_pk))
    except asyncpg.exceptions.UndefinedTableError:
        if not _OFFICIAL_BOXSCORE_TABLE_MISSING_WARNED:
            _OFFICIAL_BOXSCORE_TABLE_MISSING_WARNED = True
            log.warning(
                "SIM-545: raw.game_player_stats does not exist on this database "
                "(migration 0023 not applied). Every game grades its props on the "
                "event-label fallback this run."
            )
        return []
    return [dict(r) for r in rows]


async def _fetch_prop_ground_truth(pool: Any, game_pk: int) -> PropGroundTruth:
    """One game's real per-player prop totals, from the best source the database
    holds.

    The official box score (``raw.game_player_stats``) wins when the game has rows:
    it grades every market the platform prices, and a player who did not play is
    absent (the book voids his bet). A game with no rows falls back to the event
    label (``raw.pitches.events``), which grades only the five props an event label
    carries. ``source`` says which path the game took; the run counts games per
    source for the report.
    """
    rows = await _fetch_official_boxscore(pool, game_pk)
    if rows:
        batter_actuals, pitcher_actuals = real_props_from_boxscore_rows(rows)
        return PropGroundTruth(
            batter_actuals=batter_actuals,
            pitcher_actuals=pitcher_actuals,
            source=GROUND_TRUTH_OFFICIAL,
            scorable_props=BOXSCORE_SCORED_PROPS,
        )
    pa_events = await _fetch_pa_events(pool, game_pk)
    batter_actuals, pitcher_actuals = real_props_from_pa_events(pa_events)
    return PropGroundTruth(
        batter_actuals=batter_actuals,
        pitcher_actuals=pitcher_actuals,
        source=GROUND_TRUTH_EVENT_LABEL,
        scorable_props=EVENT_LABEL_SCORED_PROPS,
    )


# ---------------------------------------------------------------------------
# The run lines: a pair, or two separate bets
# ---------------------------------------------------------------------------

#: The report's stamp of this scoring of the run lines. The skill table and the
#: paired read refuse to mix a stamped report with an unstamped one; the re-score
#: probe refuses a report that already carries it.
RUN_LINE_SCORING_VERSION = "sim549.1"

#: A one-sided bet's closing line whose mean probability sits more than this many
#: standard errors from its own outcome rate, over at least
#: :data:`ONE_SIDED_LINE_MIN_N` records, is named at load
#: (:func:`warn_market_shapes`), and the skill table refuses such a row. On real
#: one-sided rows the corrected line sits within a point of the outcome rate
#: (plan §2.2); a gap this large is a mis-stored row, such as the 2025
#: first-inning (+1, +1) rows.
ONE_SIDED_LINE_Z = 4.0
ONE_SIDED_LINE_MIN_N = 30


def score_run_line_market(
    game_pk: int,
    market_type: str,
    cp: ClosingPrices,
    odds: dict[str, dict[str, dict[str, Any]]],
    *,
    sim_cover: Callable[[str, float], float],
    real_cover: Callable[[str, float], int | None],
    open_sim_prob_only: bool = False,
) -> list[AccuracyRecord]:
    """The accuracy records of one run-line market, by the shape of its closing row.

    ``sim_cover(side, line)`` is the simulator's probability that ``side``
    (``"home"`` or ``"away"``) covers at ITS OWN ``line``. ``real_cover(side,
    line)`` is the real 0/1 outcome, or ``None`` on a push. With
    ``open_sim_prob_only`` a simulator probability of exactly 0 or 1 drops the
    record: the full-game run line's edge report refuses such a probability, and
    its pairs keep that rule.

    A PAIR (:func:`run_line_is_pair`) gives one record: the home side, the two
    prices de-vigged against each other. TWO SEPARATE BETS give up to two records,
    each priced from its own price over the reference margin of the run-line row's
    own book (:func:`reference_margin`). Neither carries a fade price
    (``market_other_price`` is ``None``): the other price belongs to a different
    bet. A push drops that side only. A row without an away spread gives no record,
    because its shape is unknown.
    """
    home_line, away_line = cp.line, cp.other_line
    if home_line is None or away_line is None:
        return []
    records: list[AccuracyRecord] = []
    if run_line_is_pair(home_line, away_line):
        try:
            outcome = real_cover("home", home_line)
            if outcome is None:
                return []  # a push has no 0/1 label
            sim_prob = float(sim_cover("home", home_line))
            if open_sim_prob_only:
                prob_to_american(sim_prob)  # raises on 0 or 1, as the edge report does
            market_prob = float(devig_two_way(cp.side, cp.other)[0])
        except ValueError as exc:
            log.info("game %s %s accuracy skipped (degenerate): %s", game_pk, market_type, exc)
            return []
        records.append(
            AccuracyRecord(
                game_pk=int(game_pk),
                market=market_type,
                market_type=market_type,
                sim_prob=sim_prob,
                market_prob=market_prob,
                outcome=int(outcome),
                market_side_price=float(cp.side),
                market_other_price=float(cp.other),
            )
        )
        return records
    # The run-line row's own book gives the margin, never another book's graded
    # total.
    run_line_book = ((odds.get(market_type) or {}).get("closing") or {}).get("book")
    margin, _source = reference_margin(odds, GAME_MARKET_SEGMENT[market_type], book=run_line_book)
    for side, price, line in (("home", cp.side, home_line), ("away", cp.other, away_line)):
        try:
            outcome = real_cover(side, line)
            if outcome is None:
                continue  # a push on this side's integer spread; the other side stands
            sim_prob = float(sim_cover(side, line))
            if open_sim_prob_only:
                prob_to_american(sim_prob)
            market_prob = devig_one_sided(price, margin)
        except ValueError as exc:
            log.info(
                "game %s %s %s accuracy skipped (degenerate): %s", game_pk, market_type, side, exc
            )
            continue
        records.append(
            AccuracyRecord(
                game_pk=int(game_pk),
                market=run_line_market_key(market_type, side),
                market_type=market_type,
                sim_prob=sim_prob,
                market_prob=float(market_prob),
                outcome=int(outcome),
                market_side_price=float(price),
                market_other_price=None,
            )
        )
    return records


def one_sided_line_check(records: Sequence[AccuracyRecord]) -> dict[str, Any] | None:
    """Whether a one-sided bet's closing line matches its own outcome rate (pure).

    Returns ``n``, the line's mean probability, the outcome rate, the bias (mean
    minus rate) and ``z``, the bias in standard errors. Under a calibrated line the
    outcomes' variance is the mean of ``p(1 − p)``, so the standard error is
    ``sqrt(mean(p(1 − p)) / n)``. ``None`` for no records. One record per game and
    side, so the games are independent.
    """
    if not records:
        return None
    p = np.array([float(r.market_prob) for r in records])
    y = np.array([float(r.outcome) for r in records])
    n = len(records)
    bias = float(p.mean() - y.mean())
    se = float(np.sqrt(max(float(np.mean(p * (1.0 - p))), 1e-12) / n))
    return {
        "n": n,
        "line_mean": float(p.mean()),
        "outcome_rate": float(y.mean()),
        "bias": bias,
        "z": bias / se,
    }


def market_shape_tally(records: Sequence[AccuracyRecord]) -> dict[str, Any]:
    """The shape of every run-line market and the mean margin of every priced pair,
    from the records alone (pure).

    ``run_lines[<market type>]`` holds:

      * ``pairs`` — the games the book listed as a pair (one record each);
      * ``one_sided_games`` and ``one_sided_records`` — the games listed as two
        separate bets, and their records;
      * ``one_record_games`` — such games with one record, not two. One bet
        pushed, or its probability was degenerate. In a re-scored report every
        such game has one record: the re-score cannot build the away bet;
      * ``paired_margin_mean`` — the pairs' mean margin;
      * ``one_sided_line`` — :func:`one_sided_line_check` of the home bets and of
        the away bets.

    ``margin_mean[<market key>]`` is the mean margin over every record that carries
    both prices. A pair adds to about 1.05; a market far from that does not price
    the two sides of one bet.
    """
    run_lines: dict[str, Any] = {}
    for market_type in RUN_LINE_MARKET_TYPES:
        away_key = run_line_market_key(market_type, "away")
        pairs = [r for r in records if r.market == market_type and r.market_other_price is not None]
        home_one = [r for r in records if r.market == market_type and r.market_other_price is None]
        away_one = [r for r in records if r.market == away_key]
        if not pairs and not home_one and not away_one:
            continue
        per_game: dict[int, int] = {}
        for r in home_one + away_one:
            per_game[int(r.game_pk)] = per_game.get(int(r.game_pk), 0) + 1
        sums = [
            _implied(float(r.market_side_price)) + _implied(float(r.market_other_price))
            for r in pairs
            if r.market_side_price is not None and r.market_other_price is not None
        ]
        run_lines[market_type] = {
            "pairs": len(pairs),
            "one_sided_games": len(per_game),
            "one_sided_records": len(home_one) + len(away_one),
            "one_record_games": sum(1 for c in per_game.values() if c == 1),
            "paired_margin_mean": float(np.mean(sums)) if sums else None,
            "one_sided_line": {
                "home": one_sided_line_check(home_one),
                "away": one_sided_line_check(away_one),
            },
        }
    by_market: dict[str, list[float]] = {}
    for r in records:
        if r.market_side_price is None or r.market_other_price is None:
            continue
        try:
            s = _implied(float(r.market_side_price)) + _implied(float(r.market_other_price))
        except ValueError:
            continue
        by_market.setdefault(str(r.market), []).append(s)
    return {
        "run_lines": run_lines,
        "margin_mean": {m: float(np.mean(v)) for m, v in sorted(by_market.items())},
    }


def warn_market_shapes(tally: dict[str, Any]) -> None:
    """Name three things at load time, not at read time: a run-line market the book
    lists as two separate bets, a one-sided line far from its own outcome rate,
    and a market whose paired prices do not add up to a two-way margin."""
    for market_type, shape in tally.get("run_lines", {}).items():
        if shape["one_sided_games"]:
            log.warning(
                "SIM-549: %s — %d games listed as two separate bets (%d records; %d games "
                "with one record), %d pairs. Each bet is priced from its own price.",
                market_type,
                shape["one_sided_games"],
                shape["one_sided_records"],
                shape["one_record_games"],
                shape["pairs"],
            )
        for side, check in (shape.get("one_sided_line") or {}).items():
            if (
                check is not None
                and check["n"] >= ONE_SIDED_LINE_MIN_N
                and abs(check["z"]) > ONE_SIDED_LINE_Z
            ):
                log.warning(
                    "SIM-549: %s %s bets — the line says %.3f and the bets came true %.3f "
                    "of the time (n %d, z %+.1f). Mis-stored lines? Do not read this row.",
                    market_type,
                    side,
                    check["line_mean"],
                    check["outcome_rate"],
                    check["n"],
                    check["z"],
                )
    for market, margin in tally.get("margin_mean", {}).items():
        if not (REFERENCE_MARGIN_BAND[0] <= margin <= REFERENCE_MARGIN_BAND[1]):
            log.warning(
                "SIM-549: %s — the paired prices add to %.3f on average, outside %.2f-%.2f. "
                "They are not the two sides of one bet; read the row with care.",
                market,
                margin,
                REFERENCE_MARGIN_BAND[0],
                REFERENCE_MARGIN_BAND[1],
            )


# ---------------------------------------------------------------------------
# The game markets
# ---------------------------------------------------------------------------


def score_game_accuracy(
    game_pk: int,
    win_prob: Any,
    summary: Any,
    odds: dict[str, dict[str, dict[str, Any]]],
    home_score: int,
    away_score: int,
) -> list[AccuracyRecord]:
    """Score the three full-game markets on a FIXED reference side against the
    CLOSING line and the real final score.

    The reference side is HOME on the moneyline and the run line, OVER on the
    total, chosen once and never by model preference (:class:`AccuracyRecord`). A
    run line the book listed as two separate bets also scores the away bet, under
    the key ``runline_away`` (:func:`score_run_line_market`). A market with no
    closing price is skipped. A total or run line that landed EXACTLY on the
    closing line (a push) gives no record: the scored probability is a STRICT over
    or cover probability (the push rule of
    :func:`betting.clv_engine.total_over_under_edge_report` and
    :func:`betting.clv_engine.spread_cover_prob`), so a push has no 0/1 label.
    """
    records: list[AccuracyRecord] = []

    # --- moneyline (HOME at home_ml, vs away_ml) ---
    cp = _closing_prices(odds, "moneyline", "home_ml", "away_ml", None)
    if cp is not None:
        try:
            market = TwoWayMarket(
                side=MarketSide.HOME,
                entry=OddsQuote(side=cp.side, other=cp.other, line=None),
            )
            er = moneyline_edge_report(win_prob, market, side=MarketSide.HOME)
            outcome = 1 if home_score > away_score else 0
            # The raw decisive home-win share, before the reliability curve: the
            # calibration layer is fitted on it.
            hw = float(getattr(summary, "home_win_pct", 0.0) or 0.0)
            aw = float(getattr(summary, "away_win_pct", 0.0) or 0.0)
            raw = hw / (hw + aw) if (hw + aw) > 0 else None
            records.append(
                AccuracyRecord(
                    game_pk=int(game_pk),
                    market="moneyline",
                    market_type="moneyline",
                    sim_prob=float(er.sim_prob),
                    market_prob=float(er.market_fair_prob),
                    outcome=outcome,
                    market_side_price=float(cp.side),
                    market_other_price=float(cp.other),
                    sim_prob_raw=raw,
                )
            )
        except ValueError as exc:
            log.info("game %s moneyline accuracy skipped (degenerate): %s", game_pk, exc)

    # --- total (OVER at total_line) ---
    cp = _closing_prices(odds, "total", "over_ml", "under_ml", "total_line")
    if cp is not None and cp.line is not None:
        actual_total = float(home_score + away_score)
        if actual_total != cp.line:
            try:
                market = TwoWayMarket(
                    side=MarketSide.OVER,
                    entry=OddsQuote(side=cp.side, other=cp.other, line=cp.line),
                )
                er = total_over_under_edge_report(summary, market, side=MarketSide.OVER)
                outcome = 1 if actual_total > cp.line else 0
                records.append(
                    AccuracyRecord(
                        game_pk=int(game_pk),
                        market="total",
                        market_type="total",
                        sim_prob=float(er.sim_prob),
                        market_prob=float(er.market_fair_prob),
                        outcome=outcome,
                        market_side_price=float(cp.side),
                        market_other_price=float(cp.other),
                    )
                )
            except ValueError as exc:
                log.info("game %s total accuracy skipped (degenerate): %s", game_pk, exc)

    # --- the run line: the home bet at home_spread, and the away bet at
    # away_spread when the book lists the two as separate bets ---
    cp = _closing_prices(
        odds, "runline", "home_spread_ml", "away_spread_ml", "home_spread", "away_spread"
    )
    if cp is not None:
        final_margin = float(home_score - away_score)

        def sim_cover(side: str, line: float) -> float:
            # Each bet at its OWN spread (the away bet at the mirrored home line).
            return run_line_bet_cover_prob(summary, MarketSide[side.upper()], line)

        def real_cover(side: str, line: float) -> int | None:
            own_margin = final_margin if side == "home" else -final_margin
            adjusted = own_margin + float(line)
            if adjusted == 0.0:
                return None  # a push on an integer spread
            return 1 if adjusted > 0 else 0

        records.extend(
            score_run_line_market(
                game_pk,
                "runline",
                cp,
                odds,
                sim_cover=sim_cover,
                real_cover=real_cover,
                open_sim_prob_only=True,
            )
        )

    return records


#: The segment and team markets (the prop-market work, SIM-421): every game
#: market except the three full-game ones :func:`score_game_accuracy` scores.
SEGMENT_MARKET_TYPES: tuple[str, ...] = tuple(
    m for m in GAME_MARKET_TYPES if m not in LEGACY_GAME_MARKET_TYPES
)

#: (side column, other column, line column, other line column) per market kind,
#: for the FIXED reference side: HOME on a side or run-line market, OVER on a
#: total. The run line also reads the away spread, which tells a pair from two
#: separate bets.
_SEGMENT_COLUMNS: dict[str, tuple[str, str, str | None, str | None]] = {
    "moneyline": ("home_ml", "away_ml", None, None),
    "three_way": ("home_ml", "away_ml", None, None),
    "runline": ("home_spread_ml", "away_spread_ml", "home_spread", "away_spread"),
    "total": ("over_ml", "under_ml", "total_line", None),
    "yes_no": ("over_ml", "under_ml", "total_line", None),
}


def score_segment_market_accuracy(
    game_pk: int,
    runs: SegmentRuns,
    odds: dict[str, dict[str, dict[str, Any]]],
    official_grid: dict[str, Sequence[int]] | None,
) -> list[AccuracyRecord]:
    """Score the twelve segment and team markets (owner ruling 2026-09-12) on a
    FIXED reference side against the CLOSING line and the official per-inning grid.

    The simulator's probability comes from
    :func:`simulation.game_market_distributions.market_probability` (the
    per-iteration linescore); the market's from the de-vigged closing prices:
    two-way for a moneyline, total, run-line pair or yes / no market, and
    THREE-way for the first-inning and first-five moneylines, whose tie is a priced
    outcome. The outcome comes from
    :func:`simulation.game_market_distributions.market_outcome` on the official
    grid. A push (``None`` outcome) is skipped, as on the full-game markets. A
    three-way record carries no "other" price: its fade side (away OR draw) has no
    single price, so the hypothetical return leaves it out rather than price it
    wrong. A run-line market goes through :func:`score_run_line_market`, which
    scores a pair as one record and two separate bets as two.
    """
    records: list[AccuracyRecord] = []
    if official_grid is None:
        return records
    actual = SegmentRuns.from_official_grid(official_grid)
    for market_type in SEGMENT_MARKET_TYPES:
        kind = GAME_MARKET_KIND[market_type]
        side_col, other_col, line_col, other_line_col = _SEGMENT_COLUMNS[kind]
        cp = _closing_prices(odds, market_type, side_col, other_col, line_col, other_line_col)
        if cp is None:
            continue
        if line_col is not None and cp.line is None:
            continue
        if kind == "runline":

            def sim_cover(side: str, line: float, _mt: str = market_type) -> float:
                return market_probability(runs, _mt, line=line, side=side)

            def real_cover(side: str, line: float, _mt: str = market_type) -> int | None:
                return market_outcome(actual, _mt, line=line, side=side)

            records.extend(
                score_run_line_market(
                    game_pk, market_type, cp, odds, sim_cover=sim_cover, real_cover=real_cover
                )
            )
            continue
        try:
            sim_prob = market_probability(runs, market_type, line=cp.line)
            if kind == "three_way":
                cl = odds[market_type]["closing"]
                draw = cl.get("draw_ml")
                if draw is None:
                    continue
                fair = devig_multiway(
                    [
                        _implied(cp.side),
                        _implied(cp.other),
                        _implied(float(draw)),
                    ]
                )
                market_prob = float(fair[0])
                other_price: float | None = None
            else:
                market_prob = float(devig_two_way(cp.side, cp.other)[0])
                other_price = float(cp.other)
            outcome = market_outcome(actual, market_type, line=cp.line)
        except ValueError as exc:
            log.info("game %s %s accuracy skipped (degenerate): %s", game_pk, market_type, exc)
            continue
        if outcome is None:
            continue  # a push has no 0/1 label
        records.append(
            AccuracyRecord(
                game_pk=int(game_pk),
                market=market_type,
                market_type=market_type,
                sim_prob=float(sim_prob),
                market_prob=market_prob,
                outcome=int(outcome),
                market_side_price=float(cp.side),
                market_other_price=other_price,
            )
        )
    return records


# ---------------------------------------------------------------------------
# The player props
# ---------------------------------------------------------------------------


def score_prop_accuracy(
    game_pk: int,
    pset: Any,
    prop_odds: dict[tuple[int, str], dict[str, dict[str, Any]]],
    batter_actuals: dict[int, dict[str, int]],
    pitcher_actuals: dict[int, dict[str, int]],
    *,
    scorable_props: frozenset[str],
) -> list[AccuracyRecord]:
    """Score the player props on the OVER side against the CLOSING line and the
    real per-player total.

    ``batter_actuals`` / ``pitcher_actuals`` are one game's ground truth
    (:class:`PropGroundTruth`), and ``scorable_props`` is the set of model props
    THAT game's source can grade: :data:`BOXSCORE_SCORED_PROPS` on the official box
    score, :data:`EVENT_LABEL_SCORED_PROPS` on the event-label fallback. The set is
    per game, not a module constant, so a fallback game never grades a prop its
    source cannot see.

    A player or prop outside that set, or with no real-outcome entry at all (he
    did not play; the book voids the bet), is skipped, not scored with a guessed
    ground truth. A push (the real total lands on the closing line) is skipped
    too, as in :func:`score_game_accuracy`.
    """
    records: list[AccuracyRecord] = []
    for (player_id, odds_stat), by_lt in prop_odds.items():
        model_stat = PROP_VOCAB_MAP.get(odds_stat)
        if model_stat is None or model_stat not in scorable_props:
            continue
        cl = by_lt.get("closing")
        if cl is None:
            continue
        close_over, close_under, line = cl.get("over_ml"), cl.get("under_ml"), cl.get("line")
        if None in (close_over, close_under, line):
            continue

        dist = pset.get(int(player_id), model_stat) if pset is not None else None
        if dist is None:
            continue

        pid = int(player_id)
        actual = batter_actuals.get(pid, {}).get(model_stat)
        if actual is None:
            actual = pitcher_actuals.get(pid, {}).get(model_stat)
        if actual is None:
            continue  # did not play this game — the book voids the bet

        line_f = float(line)
        if float(actual) == line_f:
            continue  # push

        try:
            market = TwoWayMarket(
                side=MarketSide.OVER,
                entry=OddsQuote(side=float(close_over), other=float(close_under), line=line_f),
            )
            er = prop_edge_report(dist, market, side=MarketSide.OVER)
        except ValueError as exc:
            log.info(
                "game %s prop-accuracy %s/%s skipped (degenerate): %s",
                game_pk,
                player_id,
                model_stat,
                exc,
            )
            continue

        outcome = 1 if float(actual) > line_f else 0
        records.append(
            AccuracyRecord(
                game_pk=int(game_pk),
                market=model_stat,
                market_type="prop",
                sim_prob=float(er.sim_prob),
                market_prob=float(er.market_fair_prob),
                outcome=outcome,
                player_id=pid,
                market_side_price=float(close_over),
                market_other_price=float(close_under),
            )
        )
    return records
