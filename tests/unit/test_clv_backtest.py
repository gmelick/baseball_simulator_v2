"""
tests/unit/test_clv_backtest.py
===============================
Unit tests for the plain data ``scripts/clv_backtest.py`` exposes: the SIM-134
prop vocab map and the market trust labels. Both are shared by every report
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


def test_prop_vocab_map_covers_all_seven_markets():
    """The 7 SIM-134 odds prop_stats each map to a model PropDistribution stat."""
    expected_odds_stats = {
        "strikeouts",
        "walks",
        "earned_runs",
        "hits",
        "home_runs",
        "total_bases",
        "rbis",
    }
    assert set(PROP_VOCAB_MAP) == expected_odds_stats
    assert len(PROP_VOCAB_MAP) == 7
    # Every mapped target is a real model prop name.
    for model_stat in PROP_VOCAB_MAP.values():
        assert model_stat in ALL_PROPS
    # The expected one-to-one mapping.
    assert PROP_VOCAB_MAP == {
        "strikeouts": "K",
        "walks": "BB",
        "earned_runs": "ER",
        "hits": "H",
        "home_runs": "HR",
        "total_bases": "TB",
        "rbis": "RBI",
    }


def test_trust_label_for_every_mapped_prop_and_game_market():
    """Every model prop + every game market_type has a (non-'unknown') trust tier."""
    for model_stat in PROP_VOCAB_MAP.values():
        assert clv_backtest.trust_label(model_stat) != "unknown"
    for game_market in ("moneyline", "total", "runline"):
        assert clv_backtest.trust_label(game_market) != "unknown"
    # An unrecognized market degrades gracefully.
    assert clv_backtest.trust_label("nonsense") == "unknown"
