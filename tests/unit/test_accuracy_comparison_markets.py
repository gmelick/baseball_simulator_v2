"""
tests/unit/test_accuracy_comparison_markets.py
==============================================
Unit tests for the plain data in ``scripts/accuracy/records.py``: the
15-market prop vocabulary map (the original seven plus the eight of the
prop-market work, SIM-421) and the market trust labels. Every report of the
accuracy scripts shares both, so they get their own test file.

The reports have their own test files: the sim-vs-closing-line accuracy
comparison (``test_accuracy_comparison.py``) and the hypothetical dollar
return (``test_accuracy_hypothetical_return.py``).

The DB readers + the sim replay are never imported here (they live behind lazy
``asyncpg`` / ``api.routes.games`` imports inside the async ``run`` path), so this
module imports cleanly with only numpy + the betting engine available.
"""

from __future__ import annotations

from tests.script_loader import scripts_on_path

scripts_on_path()

from accuracy.records import (  # noqa: E402
    MARKET_TRUST,
    PROP_VOCAB_MAP,
    TRUST_TIER_ORDER,
    trust_label,
)

from simulation.prop_distributions import ALL_PROPS  # noqa: E402

#: The 15-market odds vocabulary that the ``raw.prop_odds`` CHECK constraint
#: enforces (migration 0022): the original seven plus the eight more markets
#: the book offers (the prop-market work, SIM-421).
_EXPECTED_VOCAB_MAP = {
    # the original seven markets
    "strikeouts": "K",
    "walks": "BB",
    "earned_runs": "ER",
    "hits": "H",
    "home_runs": "HR",
    "total_bases": "TB",
    "rbis": "RBI",
    # the batter markets of the prop-market work (SIM-421)
    "singles": "1B",
    "doubles": "2B",
    "triples": "3B",
    "runs": "R",
    "stolen_bases": "SB",
    "hits_runs_rbis": "HRR",
    # the pitcher markets of the prop-market work (SIM-421)
    "outs_recorded": "OUTS",
    "hits_allowed": "H_ALLOWED",
}


def test_prop_vocab_map_covers_all_fifteen_markets():
    """Every odds prop_stat the platform prices maps to a model prop name."""
    assert len(PROP_VOCAB_MAP) == 15
    assert set(PROP_VOCAB_MAP) == set(_EXPECTED_VOCAB_MAP)
    # Every mapped target is a real model prop name.
    for model_stat in PROP_VOCAB_MAP.values():
        assert model_stat in ALL_PROPS
    # The expected one-to-one mapping.
    assert PROP_VOCAB_MAP == _EXPECTED_VOCAB_MAP
    # One model prop per odds market: no two markets share a target.
    assert len(set(PROP_VOCAB_MAP.values())) == 15


def test_prop_vocab_map_keeps_batter_hits_apart_from_pitcher_hits_allowed():
    """``hits`` is the BATTER market and ``hits_allowed`` the PITCHER market.

    They read different PlayerStatLine fields (a batter's own hits vs. the
    hits a pitcher gives up), so they must map to different model props.
    """
    assert PROP_VOCAB_MAP["hits"] == "H"
    assert PROP_VOCAB_MAP["hits_allowed"] == "H_ALLOWED"
    assert PROP_VOCAB_MAP["hits"] != PROP_VOCAB_MAP["hits_allowed"]


def test_trust_label_for_every_mapped_prop_and_game_market():
    """Every model prop + every game market_type has a (non-'unknown') trust tier."""
    for model_stat in PROP_VOCAB_MAP.values():
        assert trust_label(model_stat) != "unknown"
    for game_market in ("moneyline", "total", "runline"):
        assert trust_label(game_market) != "unknown"
    # An unrecognized market degrades gracefully.
    assert trust_label("nonsense") == "unknown"


def test_new_markets_carry_the_unvalidated_tier():
    """The eight markets of the prop-market work (SIM-421) carry the
    ``unvalidated`` label until the accuracy comparison reads them. The
    original seven keep their own labels."""
    for model_stat in ("1B", "2B", "3B", "R", "SB", "HRR", "OUTS", "H_ALLOWED"):
        assert trust_label(model_stat) == "unvalidated"
    for model_stat in ("H", "HR", "TB"):
        assert trust_label(model_stat) == "trustworthy"
    for model_stat in ("K", "BB", "ER", "RBI"):
        assert trust_label(model_stat) == "untrustworthy"


def test_every_trust_tier_has_a_sort_position():
    """The report groups rows by tier; a tier with no position would sort
    after ``unknown`` and hide a real market behind a placeholder."""
    tiers = set(MARKET_TRUST.values()) | {"unknown"}
    assert tiers <= set(TRUST_TIER_ORDER)
    # ``unvalidated`` sits between the labelled tiers and ``unknown``.
    order = TRUST_TIER_ORDER
    assert order["untrustworthy"] < order["unvalidated"] < order["unknown"]
