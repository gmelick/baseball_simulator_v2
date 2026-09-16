"""SIM-518 — pairing two accuracy-comparison reports (scripts/sim518_pair_accuracy.py).

Pins three things: a report pair whose bundle, calibration file, seed or
game list differs is refused; records pair on the full (game, market,
player) key with the outcome and market probability checked equal; and the
game-clustered range widens when several records share one game (the
SIM-538 precedent).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

import numpy as np
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_MODULE_PATH = os.path.join(_REPO_ROOT, "scripts", "sim518_pair_accuracy.py")
_spec = importlib.util.spec_from_file_location("sim518_pair_accuracy", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
pair = importlib.util.module_from_spec(_spec)
sys.modules["sim518_pair_accuracy"] = pair
_spec.loader.exec_module(pair)


def _prov(**over: object) -> dict:
    base = {
        "artifact_dir": "/data/play_pool/engine_artifacts",
        "manifest_mtimes": {"pitch_pool": 1.0, "battedball_pool": 2.0, "actor_sim": 3.0},
        "calibration_sha256": "abc",
        "fatigue_pc_sigma": "0",
        "fatigue_tto_sigma": "0",
    }
    base.update(over)
    return base


def _report(records: list[dict], **params: object) -> dict:
    p = {
        "base_seed": 0,
        "iterations": 100,
        "seasons": [2024],
        "markets": "all",
        "calibration_applied": True,
        "provenance": _prov(),
    }
    p.update(params)
    return {"params": p, "accuracy_records": records}


def _rec(
    game_pk: int, market: str, sim: float, mkt: float, y: int, player: int | None = None
) -> dict:
    return {
        "game_pk": game_pk,
        "market": market,
        "market_type": "prop" if player else market,
        "sim_prob": sim,
        "market_prob": mkt,
        "outcome": y,
        "player_id": player,
    }


class TestProvenance:
    def test_twins_pair(self) -> None:
        a = _report([_rec(1, "K", 0.5, 0.5, 1, 7)])
        b = _report([_rec(1, "K", 0.6, 0.5, 1, 7)], provenance=_prov(fatigue_tto_sigma="0.5"))
        assert pair.provenance_mismatches(a, b) == []

    def test_a_different_bundle_is_refused(self) -> None:
        a = _report([_rec(1, "K", 0.5, 0.5, 1, 7)])
        b = _report(
            [_rec(1, "K", 0.5, 0.5, 1, 7)],
            provenance=_prov(manifest_mtimes={"pitch_pool": 9.0}),
        )
        problems = pair.provenance_mismatches(a, b)
        assert any("manifest_mtimes" in p for p in problems)

    def test_a_different_seed_or_game_list_is_refused(self) -> None:
        a = _report([_rec(1, "K", 0.5, 0.5, 1, 7), _rec(2, "K", 0.5, 0.5, 0, 8)])
        b = _report([_rec(1, "K", 0.5, 0.5, 1, 7)], base_seed=1)
        problems = pair.provenance_mismatches(a, b)
        assert any("base_seed" in p for p in problems)
        assert any("game sets differ" in p for p in problems)

    def test_missing_provenance_is_refused(self) -> None:
        a = _report([])
        b = _report([])
        del b["params"]["provenance"]
        assert any("provenance missing" in p for p in pair.provenance_mismatches(a, b))


class TestPairing:
    def test_records_pair_on_the_full_key(self) -> None:
        a = [
            _rec(1, "K", 0.4, 0.5, 1, 7),
            _rec(1, "K", 0.4, 0.5, 0, 8),
            _rec(1, "moneyline", 0.6, 0.55, 1),
        ]
        b = [
            _rec(1, "K", 0.5, 0.5, 1, 7),
            _rec(1, "moneyline", 0.6, 0.55, 1),
            _rec(2, "K", 0.5, 0.5, 1, 7),
        ]
        rows = pair.pair_records(a, b)
        keys = {(r["game_pk"], r["market"], r["player_id"]) for r in rows}
        assert keys == {(1, "K", 7), (1, "moneyline", None)}
        k = next(r for r in rows if r["player_id"] == 7)
        # outcome 1: Brier (0.4-1)^2 = 0.36 -> (0.5-1)^2 = 0.25; diff -0.11
        assert k["d_brier"] == pytest.approx(-0.11)
        assert k["d_prob"] == pytest.approx(0.1)

    def test_a_changed_outcome_or_market_price_raises(self) -> None:
        a = [_rec(1, "K", 0.4, 0.5, 1, 7)]
        b = [_rec(1, "K", 0.4, 0.5, 0, 7)]
        with pytest.raises(ValueError):
            pair.pair_records(a, b)
        b = [_rec(1, "K", 0.4, 0.52, 1, 7)]
        with pytest.raises(ValueError):
            pair.pair_records(a, b)


class TestSummary:
    def test_identical_arms_read_zero_with_a_zero_width_range(self) -> None:
        recs = [_rec(g, "K", 0.5, 0.5, g % 2, 7) for g in range(1, 31)]
        rows = pair.pair_records(recs, recs)
        s = pair.summarize(rows, n_bootstrap=200, seed=1, floor=0.0005, alpha=0.05)
        assert s["n"] == 30 and s["n_games"] == 30
        assert s["mean_d_brier"] == 0.0
        assert s["d_brier_ci95"] == [0.0, 0.0]

    def test_the_range_widens_when_records_cluster_in_fewer_games(self) -> None:
        rng = np.random.default_rng(5)

        # 60 records: one per game (independent) versus 3 per game (20 games).
        def build(n_games: int, per_game: int) -> tuple[list[dict], list[dict]]:
            a, b = [], []
            for g in range(1, n_games + 1):
                # A game-level shared shift in the ON arm's probability, with
                # the game's records resolving the same way — the real shape
                # (several props off one start share the start's outcome).
                shift = rng.normal(0.0, 0.08)
                y = int(rng.random() < 0.5)
                for j in range(per_game):
                    pa = 0.5
                    pb = float(np.clip(0.5 + shift + rng.normal(0, 0.01), 0.01, 0.99))
                    a.append(_rec(g, "K", pa, 0.5, y, 100 + j))
                    b.append(_rec(g, "K", pb, 0.5, y, 100 + j))
            return a, b

        a1, b1 = build(60, 1)
        a3, b3 = build(20, 3)
        s1 = pair.summarize(
            pair.pair_records(a1, b1), n_bootstrap=400, seed=2, floor=0.0005, alpha=0.05
        )
        s3 = pair.summarize(
            pair.pair_records(a3, b3), n_bootstrap=400, seed=2, floor=0.0005, alpha=0.05
        )
        width1 = s1["d_brier_ci95"][1] - s1["d_brier_ci95"][0]
        width3 = s3["d_brier_ci95"][1] - s3["d_brier_ci95"][0]
        assert s1["n"] == s3["n"] == 60
        assert width3 > width1
        assert s3["d_brier_clustered_se"] > s1["d_brier_clustered_se"]


class TestCli:
    def test_refuses_a_mismatched_pair_and_forces_on_request(self, tmp_path, capsys) -> None:
        a = _report([_rec(1, "K", 0.5, 0.5, 1, 7)])
        b = _report([_rec(1, "K", 0.6, 0.5, 1, 7)], base_seed=3)
        pa, pb = tmp_path / "a.json", tmp_path / "b.json"
        pa.write_text(json.dumps(a))
        pb.write_text(json.dumps(b))
        assert pair.main([str(pa), str(pb), "--dsn", ""]) == 2
        out = tmp_path / "pair.json"
        assert pair.main([str(pa), str(pb), "--dsn", "", "--force", "--json-out", str(out)]) == 0
        written = json.loads(out.read_text())
        assert written["n_paired"] == 1
        assert written["provenance_problems"]
