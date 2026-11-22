"""The load guard: it refuses an odds row that cannot be one bet.

The one load path (:func:`betting.load.keep_rows`) calls :func:`check_row` on
every priced row before the write. It logs a refused row, skips it, and counts it
in a :class:`RefusalTally`. The API's stored-odds read calls :func:`check_row`
again. The module does no I/O.

An implied probability is the chance a price stands for: 100 / (price + 100)
for a plus price, -price / (-price + 100) for a minus price. The sum over the
sides of one market is 1 plus the vig (the vig: the book's margin).

The rules (the audit docs ``docs/audit/2026-09-25-sim555-*.md`` hold the evidence):

* ``zero_price``: a price of 0. BettingPros sends 0 as a placeholder for a price
  a book does not offer; no American price is 0.
* ``missing_side``: a side has no price, or a total has no line.
* ``spread_size``: a run line with spreads of two sizes (-1 / +1.5). No book posts that pair.
* ``equal_spreads_priced_like_a_pair``: equal non-zero spreads whose prices add
  to 1.00-1.10. That is one book's mislabelled pair; two real bets add far more.
* ``f5_win_or_tie``: a first-five "+0.5 / +0.5" pair that adds above 1.40.
  It implies a first-inning tie rate (the first-five tie rate is 0.152).
* ``total_line_mismatch``: an over and an under at two different lines.
* ``f5_total_first_inning_line``: a first-five total of both teams at 1.5 or
  below. That is a first-inning total; a real one sits at 3.5-6.5.
* ``team_total_placeholder_line``: a full-game team total at 1 or below. That
  is a placeholder in the vendor's opener field, which has no off flag to
  catch it. A line of 1.5 is a real alternate line.
* ``f5_tie_price``: a first-five tie above 0.35 implied. That is a first-inning tie price.
* ``three_way_sum_below_one``: a three-way row whose three prices add below 0.98.
  No book sells every outcome for less than the stake; 0.02 spares the exchanges.
* ``three_way_two_way_team_prices``: a three-way row whose two team prices add to
  1.00 or more. They are the prices of the bet that refunds a tie.
* ``three_way_sum_above_max``: a three-way row that adds above 1.18. A real row
  adds to 1.02-1.15; a two-way pair beside a tie adds more.
* ``late_closing_stamp``: a closing row stamped over 15 minutes after the
  scheduled start. That is an in-play line.
* ``stamped_before_postponement``: a row of a made-up game, stamped up to 15
  minutes after the postponed original start. It prices the game not played.
  The provider must set ``postponed_start``; a row without it is not checked.

A three-way row is a first-inning or first-five moneyline with a tie price.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from betting.markets import (
    GAME_MARKET_KIND,
    GAME_MARKET_SEGMENT,
    GAME_MARKET_SIDE,
    GAME_ODDS_FIELDS,
)
from betting.pricing import implied_prob_from_american

_log = logging.getLogger("betting.validate")

#: Equal non-zero spreads whose prices add inside this band are one book's mislabelled pair.
PAIR_PRICE_BAND = (1.00, 1.10)

#: The highest implied probability of a first-five tie (the real rate is 0.152).
F5_TIE_IMPLIED_MAX = 0.35

#: A first-five "+0.5 / +0.5" pair adds to 1 + P(tie) + the vig.
#: Above this sum, the pair implies a first-inning tie.
F5_WIN_OR_TIE_SUM_MAX = 1.40

#: The lowest sum of a three-way row. The 0.02 below 1.00 spares the near-fair
#: prices of the exchanges and the prediction markets.
THREE_WAY_SUM_MIN = 0.98

#: The highest sum of a three-way row (a real row adds to 1.02-1.15).
THREE_WAY_SUM_MAX = 1.18

#: The two team prices of a three-way row add to less than this.
#: At or above it, they are the prices of a bet that refunds a tie.
THREE_WAY_TEAM_SUM_MAX = 1.00

#: A first-five total of both teams at this line or below is a first-inning total.
F5_TOTAL_LINE_MAX = 1.5

#: A full-game team total at this line or below is a placeholder, not a bet.
TEAM_TOTAL_LINE_MAX = 1.0

#: How long after the scheduled start a closing row may be stamped.
CLOSING_STAMP_GRACE = timedelta(minutes=15)

#: Every rule, grouped by the market kind it checks, in the order of the checks.
RULES = (
    "zero_price",
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
    """Why the load guard refused a row."""

    rule: str  # one of RULES
    message: str  # the reason in plain English, with the numbers


#: The price fields of a game row and of a prop row.
_GAME_PRICE_FIELDS = (
    "home_ml",
    "away_ml",
    "draw_ml",
    "over_ml",
    "under_ml",
    "home_spread_ml",
    "away_spread_ml",
)
_PROP_PRICE_FIELDS = ("over_ml", "under_ml")


def _check_zero_price(row: Mapping[str, Any], is_prop: bool) -> Refusal | None:
    """Refuse a row with a price of 0.

    BettingPros sends 0 as a placeholder for a price a book does not offer. No
    American price is 0, so the row has no real price on that side.
    """
    for field in _PROP_PRICE_FIELDS if is_prop else _GAME_PRICE_FIELDS:
        value = row.get(field)
        if value is not None and float(value) == 0.0:
            return Refusal("zero_price", f"{field} is 0, the vendor's placeholder for no price")
    return None


def _aware(value: Any) -> datetime | None:
    """A datetime as an aware UTC datetime (a naive one is read as UTC)."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _is_empty(row: Mapping[str, Any], is_prop: bool) -> bool:
    """True when the row has no price field at all."""
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
    total = implied_prob_from_american(hml) + implied_prob_from_american(aml)
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
    """True for the first-five total of both teams (``f5_total``)."""
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
        p_draw = implied_prob_from_american(draw)
        if p_draw > F5_TIE_IMPLIED_MAX:
            return Refusal(
                "f5_tie_price",
                f"a first-five tie at {float(draw):+g} implies {p_draw:.3f}: "
                "a first-inning tie price",
            )
    teams = implied_prob_from_american(home) + implied_prob_from_american(away)
    total = teams + implied_prob_from_american(draw)
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
    """Refuse a made-up game's row stamped up to 15 minutes after its postponed start.

    The 15 minutes cover the vendor's snapshot at the original start time.
    The rule checks opening and closing rows. A row without a stamp is not checked.
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
    """The load guard's verdict on one row: a :class:`Refusal`, or ``None`` to keep it.

    The guard checks a prop row (it has ``prop_stat``) as a full-game total.
    It checks a game row by its market's kind and segment, then checks the stamp.
    The guard keeps a row with no price field; the writers skip such a row.
    """
    is_prop = row.get("prop_stat") is not None
    if _is_empty(row, is_prop):
        return None
    zero = _check_zero_price(row, is_prop)
    if zero is not None:
        return zero
    canonical: str | None = None
    if is_prop:
        kind: str | None = "total"
        segment: str | None = "game"
    else:
        market = row.get("market_type")
        canonical = str(market)
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


class RefusalTally:
    """The load guard's refusals in one run, counted by rule, market and book.

    A writer calls :meth:`offered` for every row it checks.
    It calls :meth:`add` for every refused row.
    The loader prints :meth:`summary` at the end of a run.
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
        """The number of rows offered to the guard, at least the refusals."""
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
]
