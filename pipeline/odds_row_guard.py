"""
pipeline/odds_row_guard.py
==========================
SIM-555 (2026-09-28) — the load guard: refuse an odds row that cannot be one bet.

Both writers (the historical loader and the live cycle) call :func:`check_row`
on every non-empty row before they persist it. A refused row is not written;
the writer logs it and counts it in a :class:`RefusalTally`, whose summary the
loader prints at the end of a run. The module is pure: no I/O, no database.

What the guard refuses (the evidence is the scope note and the plan,
``docs/audit/2026-09-25-sim555-*.md``):

  * ``missing_side``: a side of the market has no price (or a total has no line).
  * ``spread_size``: a run line whose two spreads differ in size (home -1 against
    away +1.5): no single book posts that pair.
  * ``equal_spreads_priced_like_a_pair``: equal non-zero spreads (both teams +1)
    whose implied probabilities add to 1.00-1.10, a pair's margin. That is one
    book's mislabelled entry. Two real separate bets ("both +1.5" at -200 each)
    add far above the band.
  * ``f5_win_or_tie``: a first-five "both teams +0.5" pair whose implied
    probabilities add above 1.40. The pair implies a tie above 35%: a
    first-inning bet (the real first-five tie rate is 0.152; after one inning
    0.533).
  * ``total_line_mismatch``: an over and an under at two different lines.
  * ``f5_total_first_inning_line``: a first-five total of both teams at a line
    of 1.5 or below. A real first-five total sits at 3.5-6.5; the 300-game
    census found first-inning totals under the first-five label at 0.5-1.5
    (78 rows in 2021, 32 in 2022, 4 in 2025). The first-five TEAM totals are
    not checked by this rule: one team's first-five total sits low.
  * ``team_total_placeholder_line``: a full-game team total at a line of 1 or
    below. Caesars' OPENING team totals of 2024 all sit at 1 with prices no
    line of 1 can carry (-182 / -1200; +700 to score twice for a team the market
    expects to score four), 876 rows: the vendor's opener field carries a
    placeholder line the book never took. A line of 1.5 is real: books post it as
    an alternate, priced in step with the main line (DraftKings 2025-06-19 at 1.5,
    -160, beside 2.5 at +100 to +125 elsewhere), so the rule stops at 1.
    The old provider read those lines too (the 2022-2024 "team totals at a
    line of 1"); the opener field has no ``is_off`` flag, so only this rule
    catches them. The first-five team totals are not checked: 0.5 is real there.
  * ``f5_tie_price``: a first-five tie priced above 0.35 implied: a first-inning tie.
  * ``three_way_sum_below_one``: a three-way row (the first-inning or the
    first-five moneyline with a tie) whose three implied probabilities add
    below 0.98. No book sells every outcome of one market for less than the
    stake. FanDuel's 2025 first-five rows carried its first-inning team prices
    beside a first-five tie (home +210 / away +410 / tie +560 on 2025-07-03:
    the three add to 0.67). The 0.02 allowance spares the near-fair prices of
    the exchanges and the prediction markets (Kalshi at 0.989 and 0.998).
  * ``three_way_two_way_team_prices``: a three-way row whose two team prices
    alone add to 1.00 or more. Those are the prices of the bet that refunds a
    tie (a two-way bet), listed beside a separate tie price: in a real three-way
    market the team prices leave room for the tie (a first-five pair adds to about
    0.92, a first-inning pair to about 0.51). theScore, Caesars, FanDuel, Fliff and
    the vendor's blend post such rows in 2025 (the book-order read of 2026-09-29):
    theScore -130 / +100 with a tie at -145 on a first-inning moneyline.
  * ``three_way_sum_above_max``: a three-way row whose three prices add above
    1.18. A real three-way row adds to 1.02-1.15 (99% of every book's
    first-inning rows; the first-five rows at most 1.18); a two-way pair beside
    a tie adds to about 1.22 (first five) or 1.64 (first inning).
  * ``late_closing_stamp``: a closing line stamped more than 15 minutes after
    the scheduled start. Since 2025 the vendor stamps a snapshot at game time,
    0-5 minutes after the scheduled start; a later stamp is an in-play line.
  * ``stamped_before_postponement`` (SIM-555, 2026-10-01): a row of a game
    postponed and made up later, stamped no later than 15 minutes after the
    postponed original start (``CLOSING_STAMP_GRACE``). That price is for the
    game that was not played: bet365's closing price of game 745175 is
    stamped 2024-05-24 23:46, before the original start of 2024-05-25 00:15
    UTC; the game was played on 2024-07-13. The grace covers the vendor's
    game-time snapshot, which it takes at the ORIGINAL start too: 16 of 17
    books' closes of game 823539 (postponed from 2026-06-06 23:35 UTC, played
    2026-08-29) are stamped 2026-06-06 23:35:20. Across the made-up games of
    2019-2026, 763 closing game rows sit in those 15 minutes (16 games);
    prices stamped hours later belong to the makeup and pass. Opening and
    closing rows alike. The provider sets ``postponed_start`` on every row
    of a made-up game whose schedule it read after the postponement; a row
    without it is not checked. The live pipeline's provider lives for days, so
    its schedule poll drops a postponed game's cached facts (``forget_game``),
    and the next lookup reads the new schedule.

An implied probability is the chance a price stands for: 100 / (price + 100)
for a plus price, -price / (-price + 100) for a minus price.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from pipeline.odds_provider import (
    GAME_MARKET_KIND,
    GAME_MARKET_SEGMENT,
    GAME_MARKET_SIDE,
    GAME_ODDS_FIELDS,
)

_log = logging.getLogger("pipeline.odds_row_guard")

#: Equal non-zero spreads whose implied probabilities add inside this band are
#: priced like one bet with two sides: one book's mislabelled entry.
PAIR_PRICE_BAND = (1.00, 1.10)

#: A first-five tie cannot be priced above this implied probability (the real
#: first-five tie rate is 0.152; a first-inning tie is 0.533).
F5_TIE_IMPLIED_MAX = 0.35

#: Both teams "+0.5" over five innings: the two implied probabilities add to
#: 1 + P(tie) + the margin. Above this sum the pair implies a first-inning tie.
F5_WIN_OR_TIE_SUM_MAX = 1.40

#: SIM-555 (census fix 2026-09-28): the three implied probabilities of a
#: three-way row add to at least this. Below 1.00 a book would sell every
#: outcome for less than the stake; the 0.02 allowance spares the near-fair
#: prices of the exchanges and the prediction markets.
THREE_WAY_SUM_MIN = 0.98

#: SIM-555 (the book-order read, 2026-09-29): the three implied probabilities of a
#: three-way row add to at most this (a real row adds to 1.02-1.15; a two-way pair
#: listed beside a tie adds to about 1.22 over five innings, 1.64 over one).
THREE_WAY_SUM_MAX = 1.18

#: SIM-555 (the book-order read, 2026-09-29): the two team prices of a three-way
#: row add to less than this; at or above it they are a two-way (tie-refunded) pair.
THREE_WAY_TEAM_SUM_MAX = 1.00

#: SIM-555 (census fix 2026-09-28): a first-five total of both teams at this
#: line or below is a first-inning total (a real first-five total sits at 3.5-6.5).
F5_TOTAL_LINE_MAX = 1.5

#: SIM-555 (the 2024 season census, 2026-09-29): a full-game team total at this
#: line or below is a placeholder, not a bet (1.5 is a real alternate line).
TEAM_TOTAL_LINE_MAX = 1.0

#: A closing line may carry a stamp up to this long after the scheduled start.
CLOSING_STAMP_GRACE = timedelta(minutes=15)

#: Every rule the guard names, grouped by the market kind it checks, in the
#: order it checks them within the kind.
RULES = (
    "missing_side",
    "spread_size",
    "equal_spreads_priced_like_a_pair",
    "f5_win_or_tie",
    "total_line_mismatch",
    "f5_total_first_inning_line",
    "team_total_placeholder_line",
    "f5_tie_price",
    "three_way_sum_below_one",
    "three_way_two_way_team_prices",
    "three_way_sum_above_max",
    "late_closing_stamp",
    "stamped_before_postponement",
)


class Refusal(NamedTuple):
    """Why the guard refused a row."""

    rule: str  # one of RULES
    message: str  # the plain-English reason, with the numbers


def implied_probability(american: float) -> float:
    """The chance an American price stands for: +150 → 0.40; -150 → 0.60."""
    price = float(american)
    if price < 0:
        return -price / (-price + 100.0)
    return 100.0 / (price + 100.0)


def _aware(value: Any) -> datetime | None:
    """A datetime as an aware UTC datetime (a naive one is read as UTC)."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _is_empty(row: Mapping[str, Any], is_prop: bool) -> bool:
    """True when the row carries no odds field at all (the writers skip it)."""
    fields = ("line", "over_ml", "under_ml") if is_prop else GAME_ODDS_FIELDS
    return all(row.get(f) is None for f in fields)


def _check_runline(row: Mapping[str, Any], segment: str | None) -> Refusal | None:
    hs, as_ = row.get("home_spread"), row.get("away_spread")
    hml, aml = row.get("home_spread_ml"), row.get("away_spread_ml")
    if hs is None or as_ is None or hml is None or aml is None:
        return Refusal("missing_side", "a side of the run line is missing")
    hs, as_ = float(hs), float(as_)
    if abs(hs) != abs(as_):
        return Refusal("spread_size", f"spreads of different size ({hs:+g} / {as_:+g})")
    total = implied_probability(hml) + implied_probability(aml)
    if hs == as_ != 0 and PAIR_PRICE_BAND[0] <= total <= PAIR_PRICE_BAND[1]:
        return Refusal(
            "equal_spreads_priced_like_a_pair",
            f"equal spreads ({hs:+g} / {as_:+g}) priced like a pair: "
            f"the implied probabilities add to {total:.3f}",
        )
    if segment == "f5" and hs == as_ == 0.5 and total > F5_WIN_OR_TIE_SUM_MAX:
        return Refusal(
            "f5_win_or_tie",
            f"a first-five win-or-tie pair (+0.5 / +0.5) whose implied probabilities add "
            f"to {total:.3f}: it implies a first-inning tie",
        )
    return None


def _is_f5_game_total(market: str | None) -> bool:
    """True for the first-five total of both teams (``f5_total``), not a team total."""
    if market is None:
        return False
    return (
        GAME_MARKET_KIND.get(market) == "total"
        and GAME_MARKET_SEGMENT.get(market) == "f5"
        and GAME_MARKET_SIDE.get(market) is None
    )


def _is_full_game_team_total(market: str | None) -> bool:
    """True for a full-game team total (``team_total_home`` / ``team_total_away``)."""
    if market is None:
        return False
    return (
        GAME_MARKET_KIND.get(market) == "total"
        and GAME_MARKET_SEGMENT.get(market) == "game"
        and GAME_MARKET_SIDE.get(market) is not None
    )


def _check_total(
    row: Mapping[str, Any], is_prop: bool, market: str | None = None
) -> Refusal | None:
    line = row.get("line") if is_prop else row.get("total_line")
    if row.get("over_ml") is None or row.get("under_ml") is None or line is None:
        return Refusal("missing_side", "a side or the line of the total is missing")
    if "over_line" in row and "under_line" in row:
        over_line, under_line = row.get("over_line"), row.get("under_line")
        if over_line != under_line:
            return Refusal(
                "total_line_mismatch",
                f"the over and the under at different lines ({over_line} / {under_line})",
            )
    if not is_prop and _is_f5_game_total(market) and float(line) <= F5_TOTAL_LINE_MAX:
        return Refusal(
            "f5_total_first_inning_line",
            f"a first-five total at {float(line):g}: a first-inning total under the "
            "first-five label (a real first-five total sits at 3.5 or above)",
        )
    if not is_prop and _is_full_game_team_total(market) and float(line) <= TEAM_TOTAL_LINE_MAX:
        return Refusal(
            "team_total_placeholder_line",
            f"a full-game team total at {float(line):g}: a placeholder line, not a bet "
            "(a real one sits at 1.5 or above)",
        )
    return None


def _check_three_way(row: Mapping[str, Any], segment: str | None) -> Refusal | None:
    home, away = row.get("home_ml"), row.get("away_ml")
    if home is None or away is None:
        return Refusal("missing_side", "a side of the moneyline is missing")
    draw = row.get("draw_ml")
    if draw is None:
        return None
    if segment == "f5":
        p_draw = implied_probability(draw)
        if p_draw > F5_TIE_IMPLIED_MAX:
            return Refusal(
                "f5_tie_price",
                f"a first-five tie at {float(draw):+g} implies {p_draw:.3f}: "
                "a first-inning tie price",
            )
    teams = implied_probability(home) + implied_probability(away)
    total = teams + implied_probability(draw)
    if total < THREE_WAY_SUM_MIN:
        return Refusal(
            "three_way_sum_below_one",
            f"the three prices add to {total:.3f}: no book sells every outcome "
            "for less than the stake",
        )
    if teams >= THREE_WAY_TEAM_SUM_MAX:
        return Refusal(
            "three_way_two_way_team_prices",
            f"the two team prices alone add to {teams:.3f}: they are a two-way "
            "(tie-refunded) pair listed beside a separate tie price",
        )
    if total > THREE_WAY_SUM_MAX:
        return Refusal(
            "three_way_sum_above_max",
            f"the three prices add to {total:.3f}: a real three-way market adds to "
            f"at most {THREE_WAY_SUM_MAX:g}",
        )
    return None


def _check_stamp(row: Mapping[str, Any]) -> Refusal | None:
    if row.get("line_type") != "closing":
        return None
    stamp = _aware(row.get("book_line_at"))
    start = _aware(row.get("scheduled_start"))
    if stamp is None or start is None:
        return None
    if stamp > start + CLOSING_STAMP_GRACE:
        minutes = (stamp - start).total_seconds() / 60.0
        return Refusal(
            "late_closing_stamp",
            f"stamped {minutes:.0f} minutes after the scheduled start",
        )
    return None


def _check_postponement(row: Mapping[str, Any]) -> Refusal | None:
    """SIM-555: refuse a made-up game's row stamped up to 15 minutes after its postponed start.

    The 15 minutes (``CLOSING_STAMP_GRACE``) cover the vendor's game-time
    snapshot at the original start. Opening and closing rows alike. A row
    without ``postponed_start`` or without a stamp is not checked.
    """
    postponed = _aware(row.get("postponed_start"))
    stamp = _aware(row.get("book_line_at"))
    if postponed is None or stamp is None or stamp > postponed + CLOSING_STAMP_GRACE:
        return None
    return Refusal(
        "stamped_before_postponement",
        f"stamped {stamp:%Y-%m-%d %H:%M} UTC, no later than 15 minutes after the postponed "
        f"original start {postponed:%Y-%m-%d %H:%M} UTC: the price of the game that was not "
        "played",
    )


def check_row(row: Mapping[str, Any]) -> Refusal | None:
    """The guard's verdict on one row: a :class:`Refusal`, or ``None`` to keep it.

    A prop row (it has ``prop_stat``) is checked as a full-game total at its
    ``line``. A game row is checked by its market's kind and segment. Then every
    closing row is checked for a late stamp, and every row of a made-up game
    for a stamp at or before its postponed original start (SIM-555). A row with
    every odds field empty is not refused here: the writers skip empty rows
    before the guard.
    """
    is_prop = row.get("prop_stat") is not None
    if _is_empty(row, is_prop):
        return None
    canonical: str | None = None
    if is_prop:
        kind: str | None = "total"
        segment: str | None = "game"
    else:
        market = row.get("market_type")
        canonical = "runline" if market == "run_line" else str(market)
        kind = GAME_MARKET_KIND.get(canonical)
        segment = GAME_MARKET_SEGMENT.get(canonical)

    refusal: Refusal | None = None
    if kind == "runline":
        refusal = _check_runline(row, segment)
    elif kind in ("total", "yes_no"):
        refusal = _check_total(row, is_prop, canonical)
    elif kind == "three_way":
        refusal = _check_three_way(row, segment)
    elif kind == "moneyline" and (row.get("home_ml") is None or row.get("away_ml") is None):
        refusal = Refusal("missing_side", "a side of the moneyline is missing")
    if refusal is not None:
        return refusal
    return _check_stamp(row) or _check_postponement(row)


def refuse_reason(row: Mapping[str, Any]) -> str | None:
    """The plain-English reason the guard refuses ``row``, or ``None`` to keep it."""
    refusal = check_row(row)
    return None if refusal is None else refusal.message


class RefusalTally:
    """The guard's refusals of one run, counted by rule, market and book.

    A writer calls :meth:`offered` for every row it shows the guard and
    :meth:`add` for every row the guard refuses. :meth:`summary` is the text the
    loader prints at the end of a run; :meth:`warn_if_share_above` warns when
    the refused share passes a limit (5% by default).
    """

    def __init__(self) -> None:
        self._counts: Counter[tuple[str, str, str]] = Counter()
        self._offered = 0

    def add(self, refusal: Refusal, market: str, book: str) -> None:
        """Count one refused row."""
        self._counts[(refusal.rule, str(market), str(book))] += 1

    def offered(self, n: int = 1) -> None:
        """Count ``n`` rows offered to the guard."""
        self._offered += int(n)

    @property
    def refused(self) -> int:
        """The number of rows the guard refused."""
        return sum(self._counts.values())

    @property
    def n_offered(self) -> int:
        """The number of rows offered to the guard (never fewer than the refusals)."""
        return max(self._offered, self.refused)

    def counts(self) -> dict[tuple[str, str, str], int]:
        """(rule, market, book) → the number of refused rows."""
        return dict(self._counts)

    def summary(self) -> str:
        """A multi-line summary: the total, then by rule, then market, then book."""
        offered = self.n_offered
        share = (self.refused / offered) if offered else 0.0
        lines = [f"guard refusals: {self.refused} of {offered} rows offered ({share:.1%})"]
        by_rule: dict[str, dict[str, dict[str, int]]] = {}
        for (rule, market, book), n in self._counts.items():
            by_rule.setdefault(rule, {}).setdefault(market, {})[book] = n
        for rule in sorted(
            by_rule, key=lambda r: (RULES.index(r) if r in RULES else len(RULES), r)
        ):
            markets = by_rule[rule]
            lines.append(f"  {rule}: {sum(sum(b.values()) for b in markets.values())}")
            for market in sorted(markets):
                books = markets[market]
                lines.append(f"    {market}: {sum(books.values())}")
                for book in sorted(books, key=lambda b: (-books[b], b)):
                    lines.append(f"      {book}: {books[book]}")
        return "\n".join(lines)

    def warn_if_share_above(self, share: float = 0.05, log: logging.Logger | None = None) -> bool:
        """Log a warning and return True when the refused share passes ``share``."""
        offered = self.n_offered
        if offered == 0:
            return False
        refused_share = self.refused / offered
        if refused_share <= share:
            return False
        (log or _log).warning(
            "the odds guard refused %d of %d rows (%.1f%%), above the %.1f%% limit",
            self.refused,
            offered,
            100.0 * refused_share,
            100.0 * share,
        )
        return True


__all__ = [
    "CLOSING_STAMP_GRACE",
    "F5_TIE_IMPLIED_MAX",
    "F5_TOTAL_LINE_MAX",
    "TEAM_TOTAL_LINE_MAX",
    "F5_WIN_OR_TIE_SUM_MAX",
    "PAIR_PRICE_BAND",
    "RULES",
    "THREE_WAY_SUM_MIN",
    "THREE_WAY_SUM_MAX",
    "THREE_WAY_TEAM_SUM_MAX",
    "Refusal",
    "RefusalTally",
    "check_row",
    "implied_probability",
    "refuse_reason",
]
