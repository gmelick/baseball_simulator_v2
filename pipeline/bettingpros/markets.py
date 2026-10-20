"""The BettingPros market ids of every game market and every prop market."""

from __future__ import annotations

from pipeline.odds_provider import GAME_MARKET_TYPES, PROP_STATS

#: market_type (the GAME_MARKET_TYPES vocabulary) → BettingPros market id.
#: ``run_line`` is a legacy alias of ``runline``. Both team-total markets of a
#: side pair share one BettingPros market: the response holds one offer per
#: team, and :meth:`_team_offer_selections` picks the side's offer.
_GAME_MARKET_IDS: dict[str, int] = {
    "moneyline": 122,
    "total": 175,
    "runline": 176,
    "run_line": 176,
    "f1_moneyline": 278,
    "f5_moneyline": 279,
    "f1_total": 280,
    "f5_total": 281,
    "f1_runline": 282,
    "f5_runline": 283,
    "team_total_home": 277,
    "team_total_away": 277,
    "f5_team_total_home": 407,
    "f5_team_total_away": 407,
    "first_to_score": 286,
    "first_inning_run": 369,
}
if set(GAME_MARKET_TYPES) - set(_GAME_MARKET_IDS):
    raise RuntimeError(
        "bettingpros_odds_provider: _GAME_MARKET_IDS lacks a market id for "
        f"{sorted(set(GAME_MARKET_TYPES) - set(_GAME_MARKET_IDS))} — keep it in step "
        "with pipeline.odds_provider.GAME_MARKET_TYPES"
    )


#: prop_stat (the PROP_STATS vocabulary) → BettingPros market id.
_PROP_MARKET_IDS: dict[str, int] = {
    "strikeouts": 285,
    "hits": 287,
    "home_runs": 299,
    "earned_runs": 290,
    "walks": 408,
    "total_bases": 293,
    "rbis": 289,
    # The eight markets the book already offers.
    "singles": 295,
    "doubles": 291,
    "triples": 292,
    "runs": 288,
    "stolen_bases": 294,
    "hits_runs_rbis": 403,
    "outs_recorded": 405,
    "hits_allowed": 404,
}
if set(_PROP_MARKET_IDS) != set(PROP_STATS):  # pragma: no cover — an import-time guard
    raise RuntimeError(
        "BettingPros market ids and PROP_STATS disagree: "
        f"{sorted(set(_PROP_MARKET_IDS) ^ set(PROP_STATS))}"
    )
