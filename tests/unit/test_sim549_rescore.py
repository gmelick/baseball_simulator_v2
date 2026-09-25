"""SIM-549 — the re-score of stored accuracy reports (``scripts/sim549_rescore_runlines.py``).

The accuracy comparison used to price every closing run line as the two sides
of one bet. The reports written before the fix are re-priced beside their
originals: a pair keeps its record, a row the book listed as two separate bets
re-prices its home record from its own price over the same game's reference
margin, and the away bet's record joins from the next backtest run. These tests
drive the pure re-score on a toy report and a toy store; no database.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rescore = _load("sim549_rescore_runlines")
bt = rescore.bt


def _implied(american: float) -> float:
    from betting.clv_engine import implied_prob_from_american

    return implied_prob_from_american(american)


def _rec(game_pk, market, sim, mkt, outcome, side, other):
    return {
        "game_pk": game_pk,
        "market": market,
        "market_type": market,
        "sim_prob": sim,
        "market_prob": mkt,
        "outcome": outcome,
        "player_id": None,
        "market_side_price": side,
        "market_other_price": other,
    }


def _toy_report() -> dict:
    """Game 1: a pair (-0.5 / +0.5). Game 2: two separate bets (-1.5 / -1.5)
    with its first-five total in the report. Game 3: a moneyline only. Game 4:
    a full-game run line listed as two bets. Game 5: a first-inning one."""
    from betting.clv_engine import devig_two_way

    pair_mkt = devig_two_way(105.0, -125.0)[0]
    paired_two_bets = devig_two_way(350.0, 150.0)[0]  # the old, wrong number
    return {
        "params": {"base_seed": 0, "bootstrap_samples": 20, "bootstrap_seed": 1},
        "counters": {"games_scored": 3, "n_accuracy_records": 4},
        "accuracy_comparison": {},
        "accuracy_records": [
            _rec(1, "f5_runline", 0.55, pair_mkt, 1, 105.0, -125.0),
            _rec(2, "f5_runline", 0.20, paired_two_bets, 0, 350.0, 150.0),
            _rec(2, "f5_total", 0.50, 0.49, 1, -109.0, -121.0),
            _rec(3, "moneyline", 0.60, 0.58, 1, -140.0, 120.0),
            _rec(4, "runline", 0.30, devig_two_way(160.0, 110.0)[0], 1, 160.0, 110.0),
            _rec(5, "f1_runline", 0.85, devig_two_way(-400.0, 250.0)[0], 1, -400.0, 250.0),
        ],
    }


def _store() -> dict:
    """The closing rows the store holds for the toy report's games."""
    return {
        1: {
            "f5_runline": {
                "closing": {
                    "home_spread": -0.5,
                    "home_spread_ml": 105.0,
                    "away_spread": 0.5,
                    "away_spread_ml": -125.0,
                }
            },
        },
        2: {
            "f5_runline": {
                "closing": {
                    "home_spread": -1.5,
                    "home_spread_ml": 350.0,
                    "away_spread": -1.5,
                    "away_spread_ml": 150.0,
                }
            },
            "f5_total": {"closing": {"over_ml": -109.0, "under_ml": -121.0, "total_line": 4.5}},
        },
        4: {
            "runline": {
                "closing": {
                    "home_spread": -1.5,
                    "home_spread_ml": 160.0,
                    "away_spread": -1.5,
                    "away_spread_ml": 110.0,
                }
            },
            "total": {"closing": {"over_ml": -108.0, "under_ml": -112.0, "total_line": 8.5}},
        },
        5: {
            "f1_runline": {
                "closing": {
                    "home_spread": 0.5,
                    "home_spread_ml": -400.0,
                    "away_spread": 0.5,
                    "away_spread_ml": 250.0,
                }
            },
            "f1_total": {"closing": {"over_ml": -120.0, "under_ml": -105.0, "total_line": 0.5}},
        },
    }


def test_rescore_script_on_a_toy_report():
    report = _toy_report()
    out, summary = rescore.rescore_report(report, _store(), source="toy.json", date="2026-09-25")
    recs = {(r["game_pk"], r["market"]): r for r in out["accuracy_records"]}
    # the pair is untouched
    assert recs[(1, "f5_runline")] == report["accuracy_records"][0]
    # the two separate bets: the home record re-priced from its own price over
    # the same game's first-five total margin; its fade price gone; the
    # simulator's number and the outcome unchanged
    two = recs[(2, "f5_runline")]
    margin = _implied(-109.0) + _implied(-121.0)
    assert two["market_prob"] == pytest.approx((100 / 450) / margin)
    assert two["market_other_price"] is None
    assert two["market_side_price"] == 350.0
    assert two["sim_prob"] == 0.20 and two["outcome"] == 0
    # no away record is made: the report cannot give its simulator probability
    assert (2, "f5_runline_away") not in recs
    # every other record is as it was
    assert recs[(2, "f5_total")] == report["accuracy_records"][2]
    assert recs[(3, "moneyline")] == report["accuracy_records"][3]
    # the stamp, the provenance of the re-score, the fresh tally and table
    assert out["params"]["run_line_scoring"] == bt.RUN_LINE_SCORING_VERSION
    assert out["params"]["rescored"]["from"] == "toy.json"
    assert out["params"]["rescored"]["records"] == 3
    assert out["params"]["rescored"]["reference_margin_sources"] == {
        "f5_total": 1,
        "total": 1,
        "f1_total": 1,
    }
    assert out["params"]["rescored"]["problems"] == []
    shape = out["counters"]["market_shapes"]["run_lines"]["f5_runline"]
    assert shape["pairs"] == 1 and shape["one_sided_records"] == 1
    # the full-game and the first-inning run lines are re-priced from their
    # own segment's total
    full = recs[(4, "runline")]
    assert full["market_prob"] == pytest.approx(
        _implied(160.0) / (_implied(-108.0) + _implied(-112.0))
    )
    assert full["market_other_price"] is None
    first = recs[(5, "f1_runline")]
    assert first["market_prob"] == pytest.approx(
        _implied(-400.0) / (_implied(-120.0) + _implied(-105.0))
    )
    assert summary["tally"]["runline:repriced"] == 1
    assert summary["tally"]["f1_runline:repriced"] == 1
    assert "by_market" in out["accuracy_comparison"]
    assert summary["tally"]["f5_runline:repriced"] == 1
    assert summary["tally"]["f5_runline:pairs"] == 1
    # the report's own first-five total record agrees with the store's margin
    assert summary["tally"]["margin_checked_against_the_report"] == 1
    assert "margin_differs_from_the_report" not in summary["tally"]
    assert summary["problems"] == []
    # the input is not changed in place
    assert report["accuracy_records"][1]["market_other_price"] == 150.0


def test_a_stamped_report_is_refused():
    report = _toy_report()
    out, _ = rescore.rescore_report(report, _store(), source="toy.json", date="2026-09-25")
    with pytest.raises(rescore.RescoreRefused, match="already scored"):
        rescore.rescore_report(out, _store(), source="toy.sim549.json", date="2026-09-25")


def test_a_paired_read_is_refused():
    paired_read = {"report_a": "a.json", "report_b": "b.json", "summary": {}}
    with pytest.raises(rescore.RescoreRefused, match="sim518_pair_accuracy"):
        rescore.rescore_report(paired_read, {}, source="pair.json", date="2026-09-25")


def test_a_price_the_store_does_not_hold_is_a_problem():
    report = _toy_report()
    store = _store()
    store[2]["f5_runline"]["closing"]["home_spread_ml"] = 340.0  # the store moved
    _, summary = rescore.rescore_report(report, store, source="toy.json", date="2026-09-25")
    assert len(summary["problems"]) == 1 and "game 2" in summary["problems"][0]
    del store[1]
    _, summary = rescore.rescore_report(report, store, source="toy.json", date="2026-09-25")
    assert any("no closing row" in p for p in summary["problems"])


def test_a_row_without_an_away_spread_loses_its_record():
    report = _toy_report()
    store = _store()
    store[2]["f5_runline"]["closing"]["away_spread"] = None
    out, summary = rescore.rescore_report(report, store, source="toy.json", date="2026-09-25")
    assert (2, "f5_runline") not in {(r["game_pk"], r["market"]) for r in out["accuracy_records"]}
    assert summary["tally"]["f5_runline:dropped_no_away_spread"] == 1
    assert out["params"]["rescored"]["dropped"] == 1


def test_the_output_sits_beside_the_input():
    assert rescore.output_path("scripts/sim548_accuracy_split.json") == str(
        Path("scripts/sim548_accuracy_split.sim549.json")
    )
    assert rescore.run_line_games(_toy_report()) == {1, 2, 4, 5}


def test_an_away_price_the_store_does_not_hold_is_a_problem():
    store = _store()
    store[2]["f5_runline"]["closing"]["away_spread_ml"] = 145.0
    _, summary = rescore.rescore_report(_toy_report(), store, source="t.json", date="2026-09-25")
    assert len(summary["problems"]) == 1 and "game 2" in summary["problems"][0]


def test_the_derived_blocks_are_recomputed():
    report = _toy_report()
    report["params"]["edge_threshold"] = 0.0
    report["hypothetical_return"] = {"stale": True}
    store = _store()
    store[2]["f5_runline"]["closing"]["away_spread"] = None  # one record dropped
    out, _ = rescore.rescore_report(report, store, source="t.json", date="2026-09-25")
    assert "stale" not in out["hypothetical_return"]
    assert out["counters"]["n_accuracy_records"] == len(out["accuracy_records"]) == 5
    objs = [bt.AccuracyRecord.from_jsonable(r) for r in out["accuracy_records"]]
    # the report's own bootstrap size and seed, not the defaults
    assert out["accuracy_comparison"] == bt.aggregate_accuracy_comparison(
        objs, n_bootstrap=20, seed=1
    )


def _write_report(tmp_path: Path) -> Path:
    import json

    src = tmp_path / "rep.json"
    src.write_text(json.dumps(_toy_report()), encoding="utf-8")
    return src


def test_main_keeps_an_existing_output_unless_told(tmp_path, monkeypatch):
    import json

    async def _fetch(_dsn, _games):
        return _store()

    monkeypatch.setattr(rescore, "fetch_closing_rows", _fetch)
    src = _write_report(tmp_path)
    dest = Path(rescore.output_path(str(src)))
    dest.write_text("keep me", encoding="utf-8")
    assert rescore.main([str(src), "--dsn", "postgresql://x/y"]) == 1
    assert dest.read_text(encoding="utf-8") == "keep me"
    assert rescore.main([str(src), "--dsn", "postgresql://x/y", "--overwrite"]) == 0
    written = json.loads(dest.read_text(encoding="utf-8"))
    assert written["params"]["run_line_scoring"] == bt.RUN_LINE_SCORING_VERSION


def test_main_writes_a_report_with_problems_only_when_accepted(tmp_path, monkeypatch):
    import json

    store = _store()
    store[2]["f5_runline"]["closing"]["home_spread_ml"] = 340.0

    async def _fetch(_dsn, _games):
        return store

    monkeypatch.setattr(rescore, "fetch_closing_rows", _fetch)
    src = _write_report(tmp_path)
    dest = Path(rescore.output_path(str(src)))
    assert rescore.main([str(src), "--dsn", "postgresql://x/y"]) == 1
    assert not dest.exists()
    # --overwrite alone does not accept a problem
    assert rescore.main([str(src), "--dsn", "postgresql://x/y", "--overwrite"]) == 1
    assert not dest.exists()
    assert rescore.main([str(src), "--dsn", "postgresql://x/y", "--accept-problems"]) == 0
    written = json.loads(dest.read_text(encoding="utf-8"))
    assert len(written["params"]["rescored"]["problems"]) == 1
