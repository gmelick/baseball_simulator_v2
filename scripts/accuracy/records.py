"""The accuracy record, and the market vocabulary every stage shares.

An :class:`AccuracyRecord` is one paired observation: the simulator's
probability and the closing line's probability of one event, and whether the
event happened. The grading stage writes the records; the two report tables
read them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from pipeline.odds_provider import GAME_MARKET_KIND, GAME_MARKET_TYPES, LEGACY_GAME_MARKET_TYPES
from simulation.prop_validation import (
    BOXSCORE_BATTER_PROPS,
    BOXSCORE_PITCHER_PROPS,
    DERIVABLE_BATTER_PROPS,
    DERIVABLE_PITCHER_PROPS,
)

# ---------------------------------------------------------------------------
# The prop vocabulary
# ---------------------------------------------------------------------------

#: The odds vocabulary (``raw.prop_odds.prop_stat``) mapped to the model props of
#: ``simulation.prop_distributions``: the fifteen markets the platform prices
#: (the prop-market work, SIM-421; the CHECK constraint of migration 0022 holds
#: the same list). Batter: hits, home runs, total bases, RBI, singles, doubles,
#: triples, runs, stolen bases and hits+runs+RBI. Pitcher: strikeouts, walks,
#: earned runs, outs recorded and hits allowed.
#:
#: ``hits`` is the BATTER market (a batter's own hits, model prop ``H``);
#: ``hits_allowed`` is the PITCHER market (the hits a pitcher gives up, model prop
#: ``H_ALLOWED``). They read different ``PlayerStatLine`` fields, so they never
#: share a model prop.
PROP_VOCAB_MAP: dict[str, str] = {
    "strikeouts": "K",
    "walks": "BB",
    "earned_runs": "ER",
    "hits": "H",
    "home_runs": "HR",
    "total_bases": "TB",
    "rbis": "RBI",
    "singles": "1B",
    "doubles": "2B",
    "triples": "3B",
    "runs": "R",
    "stolen_bases": "SB",
    "hits_runs_rbis": "HRR",
    "outs_recorded": "OUTS",
    "hits_allowed": "H_ALLOWED",
}

# ---------------------------------------------------------------------------
# The trust labels
# ---------------------------------------------------------------------------

#: The trust tier of each market key (a game market type or a model prop stat).
#:
#: The tiers are labels from the pool-totals view of the simulator. They group
#: the report's rows and grade nothing: a market's own Brier and log-loss row
#: against the closing line says whether the simulator is accurate on it. Use the
#: label to find a row, then read the row.
#:   trustworthy   — box rate stats within ~4% of MLB in the pool view (H/HR/TB).
#:   loose         — the moneyline (the win-probability fit on a bounded sample).
#:   caution       — the total and the run line (the hits-to-runs gap lives here;
#:                   runs read ~7-8% low, CLAUDE.md §11).
#:   untrustworthy — K/BB/ER/RBI (props the pool view over-predicts).
#:   unvalidated   — the markets the comparison has not scored yet: the props
#:                   1B/2B/3B/R/SB/HRR/OUTS/H_ALLOWED and the twelve segment and
#:                   team markets. Once a market is scored, its row is the
#:                   verdict, not its label.
MARKET_TRUST: dict[str, str] = {
    # trustworthy
    "H": "trustworthy",
    "HR": "trustworthy",
    "TB": "trustworthy",
    # loose
    "moneyline": "loose",
    # caution
    "total": "caution",
    "runline": "caution",
    # The away bet of a run line the book lists as two separate bets carries its
    # own key and its market's label.
    "runline_away": "caution",
    # unvalidated: the twelve segment and team markets, priced from the
    # simulator's per-iteration linescore
    **{m: "unvalidated" for m in GAME_MARKET_TYPES if m not in LEGACY_GAME_MARKET_TYPES},
    "f5_runline_away": "unvalidated",
    "f1_runline_away": "unvalidated",
    # untrustworthy
    "K": "untrustworthy",
    "BB": "untrustworthy",
    "ER": "untrustworthy",
    "RBI": "untrustworthy",
    # unvalidated: the props
    "1B": "unvalidated",
    "2B": "unvalidated",
    "3B": "unvalidated",
    "R": "unvalidated",
    "SB": "unvalidated",
    "HRR": "unvalidated",
    "OUTS": "unvalidated",
    "H_ALLOWED": "unvalidated",
}

#: The order the report groups its rows in: one tier after another, then the
#: market key. ``unknown`` (a key with no label) sorts last, so a new market never
#: hides an established one. The accuracy table and the hypothetical-return table
#: share it.
TRUST_TIER_ORDER: dict[str, int] = {
    "trustworthy": 0,
    "loose": 1,
    "caution": 2,
    "untrustworthy": 3,
    "unvalidated": 4,
    "unknown": 5,
}


def trust_label(market: str) -> str:
    """The trust tier of a market key (a game market type or a model prop stat).

    An unknown market reads ``"unknown"`` instead of raising, so a new market
    never crashes the report.
    """
    return MARKET_TRUST.get(str(market), "unknown")


# ---------------------------------------------------------------------------
# The prop ground truth: the official box score, or the event label
# ---------------------------------------------------------------------------

#: The two sources a game's props can be graded on. The report counts games per
#: source, so a reader sees how many games the official record graded and how
#: many the fallback graded.
GROUND_TRUTH_OFFICIAL = "official_boxscore"
GROUND_TRUTH_EVENT_LABEL = "event_label"

#: The props each source can grade. The official box score
#: (``raw.game_player_stats``, SIM-545) grades every batter and pitcher market the
#: platform prices, RBI and ER included. The event-label fallback
#: (``raw.pitches.events``) grades only the five props an event label carries
#: (H/HR/TB, K/BB). The grading stage takes the set of the game it scores, so a
#: fallback game never grades a prop its source cannot see.
BOXSCORE_SCORED_PROPS: frozenset[str] = frozenset(BOXSCORE_BATTER_PROPS) | frozenset(
    BOXSCORE_PITCHER_PROPS
)
EVENT_LABEL_SCORED_PROPS: frozenset[str] = frozenset(DERIVABLE_BATTER_PROPS) | frozenset(
    DERIVABLE_PITCHER_PROPS
)


@dataclass(frozen=True, slots=True)
class PropGroundTruth:
    """One game's real per-player prop totals, the source that gave them, and the
    props that source can grade.

    ``batter_actuals`` and ``pitcher_actuals`` are ``{player_id: {prop: total}}``.
    A player who did not play is absent from both: the book voids his bet, and the
    scorer skips him.
    """

    batter_actuals: dict[int, dict[str, int]]
    pitcher_actuals: dict[int, dict[str, int]]
    #: :data:`GROUND_TRUTH_OFFICIAL` or :data:`GROUND_TRUTH_EVENT_LABEL`.
    source: str
    #: :data:`BOXSCORE_SCORED_PROPS` or :data:`EVENT_LABEL_SCORED_PROPS`.
    scorable_props: frozenset[str]


# ---------------------------------------------------------------------------
# The run-line record keys
# ---------------------------------------------------------------------------

#: The three run-line markets (full game, first five innings, first inning).
RUN_LINE_MARKET_TYPES: tuple[str, ...] = tuple(
    m for m in GAME_MARKET_TYPES if GAME_MARKET_KIND[m] == "runline"
)


def run_line_market_key(market_type: str, side: str) -> str:
    """The record key of one run-line bet: the market type for the home bet,
    ``<market_type>_away`` for the away bet (the owner's decision 2 in the run-line
    plan, SIM-549)."""
    return market_type if side == "home" else f"{market_type}_away"


# ---------------------------------------------------------------------------
# The accuracy record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccuracyRecord:
    """One paired observation of one market of one game: the simulator's
    probability, the closing line's probability and the real outcome.

    Every market with a usable closing price and a real outcome gives one record,
    scored on a FIXED reference side (home, or over). The side never depends on
    what the model would have bet: scoring the model's own pick would measure
    accuracy only on the games where it disagreed with the market, a biased
    sample.

    One exception: a run line the book lists as two separate bets (home −1.5 and
    away −1.5, not the two sides of one bet) gives one record per bet. The home
    bet's key is the market type; the away bet's key is ``<market_type>_away``
    (:func:`run_line_market_key`). The key (game, market, player) still names one
    record.
    """

    game_pk: int
    #: The market key: 'moneyline', 'total', 'runline', a segment market, a model
    #: prop stat (K, H, ...) or a run line's away bet (``runline_away``,
    #: ``f5_runline_away``, ``f1_runline_away``).
    market: str
    #: The game market type ('moneyline', 'total', 'runline', a segment market
    #: such as 'f5_runline') or 'prop'. The tables group by ``market``. A run
    #: line's away bet shares its market's type, so ``market`` and
    #: ``market_type`` differ on that record.
    market_type: str
    #: The simulator's own probability of the reference event.
    sim_prob: float
    #: The closing line's probability of the same event, margin removed.
    market_prob: float
    #: 1 if the reference event happened, else 0. A push (the real value landed
    #: on the line) never becomes a record.
    outcome: int
    #: The player (props only).
    player_id: int | None = None
    #: The closing American prices of the reference side and of the other side,
    #: with the book's margin still in. A hypothetical bet transacts at the
    #: offered price, never at the fair ``market_prob``. ``None`` on a record
    #: built without them; the hypothetical return skips such a record.
    #: ``market_other_price`` is ``None`` on a three-way record and on a run-line
    #: bet the book listed on its own: neither has a single fade price.
    market_side_price: float | None = None
    market_other_price: float | None = None
    #: The simulator's probability before the calibration map, where a map
    #: applies (the moneyline, through the win-probability reliability curve):
    #: the decisive home-win share of the iterations. The per-market calibration
    #: layer (SIM-548) is fitted on this value, never on the mapped one. ``None``
    #: where no map applies.
    sim_prob_raw: float | None = None
    #: The best price a bettor could take on the reference side at the closing
    #: line: the highest payout over every sportsbook's closing row at the same
    #: line as the graded row (the graded book's own price included; a tie goes
    #: to the book earlier on the preference list), and that book's label
    #: (``'bp:19'``). ``None`` when no sportsbook quotes the side at that line.
    #: The scoring never reads these two fields.
    market_best_price: float | None = None
    market_best_book: str | None = None

    def to_jsonable(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_jsonable(cls, d: dict[str, Any]) -> AccuracyRecord:
        """The record a parallel worker returned as a dict, rebuilt. A report
        written before a field existed loads with that field's default."""
        return cls(**d)
