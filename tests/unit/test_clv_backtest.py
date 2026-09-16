"""
tests/unit/test_clv_backtest.py
===============================
Unit tests for the plain data ``scripts/clv_backtest.py`` exposes: the
15-market prop vocab map (the SIM-134 seven plus the eight SIM-421 added) and
the market trust labels. Both are shared by every report
this file produces, so they get their own test file instead of living inside
one report's own test module.

SIM-541 (2026-09-11): this file used to also test the SIM-429 CLV scoreboard
(the per-bet CLV decision, ``evaluate_two_way_market``, and its aggregation,
``aggregate_scoreboard``). The owner retired that report — the platform no
longer measures the entry-to-close line move at all — and its code was
deleted from ``scripts/clv_backtest.py``. Those tests are deleted with it.
The surviving reports (the sim-vs-closing-line accuracy comparison, SIM-538,
and the hypothetical dollar return, SIM-540) have their own test files:
``test_sim538_accuracy_comparison.py`` / ``test_sim540_hypothetical_return.py``.

The DB readers + the sim replay are never imported here (they live behind lazy
``asyncpg`` / ``api.routes.games`` imports inside the async ``run`` path), so this
module imports cleanly with only numpy + the betting engine available.
"""

from __future__ import annotations

import importlib.util
import os
import sys

# Import the script module by path (scripts/ is not a package). It MUST be
# registered in sys.modules before exec_module so that @dataclass(slots=True) can
# resolve the module's namespace (dataclasses looks the module up by name).
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_MODULE_PATH = os.path.join(_REPO_ROOT, "scripts", "clv_backtest.py")
_spec = importlib.util.spec_from_file_location("clv_backtest", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
clv_backtest = importlib.util.module_from_spec(_spec)
sys.modules["clv_backtest"] = clv_backtest
_spec.loader.exec_module(clv_backtest)

from simulation.prop_distributions import ALL_PROPS  # noqa: E402

PROP_VOCAB_MAP = clv_backtest.PROP_VOCAB_MAP

#: The 15-market odds vocabulary the SIM-421 CHECK constraint enforces
#: (migration 0022): the SIM-134 seven plus the eight the book already
#: offered. This is the contract every engineer on SIM-421 codes against.
_EXPECTED_VOCAB_MAP = {
    # SIM-134
    "strikeouts": "K",
    "walks": "BB",
    "earned_runs": "ER",
    "hits": "H",
    "home_runs": "HR",
    "total_bases": "TB",
    "rbis": "RBI",
    # SIM-421 batter markets
    "singles": "1B",
    "doubles": "2B",
    "triples": "3B",
    "runs": "R",
    "stolen_bases": "SB",
    "hits_runs_rbis": "HRR",
    # SIM-421 pitcher markets
    "outs_recorded": "OUTS",
    "hits_allowed": "H_ALLOWED",
}


def test_prop_vocab_map_covers_all_fifteen_markets():
    """Every odds prop_stat the platform prices maps to a model prop name.

    SIM-421 grew the map from the SIM-134 seven to fifteen; this test used to
    pin the seven-market count and is updated deliberately.
    """
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
        assert clv_backtest.trust_label(model_stat) != "unknown"
    for game_market in ("moneyline", "total", "runline"):
        assert clv_backtest.trust_label(game_market) != "unknown"
    # An unrecognized market degrades gracefully.
    assert clv_backtest.trust_label("nonsense") == "unknown"


def test_new_markets_carry_the_unvalidated_tier():
    """SIM-421: the eight markets the platform prices but has never scored
    carry the ``unvalidated`` label until the accuracy comparison has read
    them. The legacy seven keep their carried-over labels."""
    for model_stat in ("1B", "2B", "3B", "R", "SB", "HRR", "OUTS", "H_ALLOWED"):
        assert clv_backtest.trust_label(model_stat) == "unvalidated"
    for model_stat in ("H", "HR", "TB"):
        assert clv_backtest.trust_label(model_stat) == "trustworthy"
    for model_stat in ("K", "BB", "ER", "RBI"):
        assert clv_backtest.trust_label(model_stat) == "untrustworthy"


def test_every_trust_tier_has_a_sort_position():
    """The report groups rows by tier; a tier with no position would sort
    after ``unknown`` and hide a real market behind a placeholder."""
    tiers = set(clv_backtest.MARKET_TRUST.values()) | {"unknown"}
    assert tiers <= set(clv_backtest.TRUST_TIER_ORDER)
    # ``unvalidated`` sits between the labelled tiers and ``unknown``.
    order = clv_backtest.TRUST_TIER_ORDER
    assert order["untrustworthy"] < order["unvalidated"] < order["unknown"]
