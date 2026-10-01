"""SIM-554 — the running-game replay's arithmetic, on synthetic inputs (no DB, no bundle).

``scripts/sim554_running_game_replay.py`` replays real pitches through the
sampler and scores eleven ways of sampling the steal. The sampler seams it reads
are the production ones (``tests/unit/test_sim554_steal_weights_seam.py`` pins
the new one). These tests pin the script's own arithmetic:

  * a jar of weighted rows becomes a probability table over (result x steal);
  * the two-step read lets the look-alike weights move ONLY the share of steals,
    and leaves the ordinary pitch result alone;
  * the per-pitch record and the scores built from it (a perfect table scores
    zero; a table that said "impossible" pays a bounded price);
  * the report, end to end on planted data: the design that holds the true
    table wins its family, and the pick is the two-step pitch draw's winner.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rp = _load("sim554_running_game_replay")
NC, NCELL = rp.NC, rp.NCELL
BALL, CALLED, SWING, IN_PLAY = rp.BALL, rp.CALLED, rp.SWING, rp.IN_PLAY


def _jar():
    """Ten rows: six balls (two with a steal, one safe and one caught), three
    balls in play, one called strike."""
    cls = np.array([BALL] * 6 + [IN_PLAY] * 3 + [CALLED], dtype=np.int64)
    a = np.array([1, 1, 0, 0, 0, 0, 0, 0, 0, 0], dtype=bool)
    z = np.array([1, 0, 0, 0, 0, 0, 0, 0, 0, 0], dtype=bool)
    cell = cls * 3 + np.where(a, np.where(z, 1, 2), 0)
    return cls, a, cell


class TestTheTable:
    def test_a_jar_becomes_a_table_that_sums_to_one(self):
        cls, _a, cell = _jar()
        j = rp.Pass._joint(cell, np.ones(10), 0).reshape(NC, 3)
        assert j.sum() == np.float64(1.0)
        assert np.isclose(j[BALL, 0], 0.4) and np.isclose(j[BALL, 1], 0.1)
        assert np.isclose(j[BALL, 2], 0.1) and np.isclose(j[IN_PLAY, 0], 0.3)
        assert j[IN_PLAY, 1:].sum() == 0.0  # no steal ever rides a ball in play

    def test_an_empty_jar_falls_back_to_the_one_row(self):
        _cls, _a, cell = _jar()
        j = rp.Pass._joint(cell, np.zeros(10), 7)
        assert j[7] == 1.0 and j.sum() == 1.0


class TestTheTwoStepRead:
    def test_with_no_look_alike_weight_it_is_the_plain_jar(self):
        cls, a, cell = _jar()
        w = np.linspace(1.0, 2.0, 10)
        plain = rp.Pass._joint(cell, w, 0)
        two = rp.Pass._two_step(cell, cls, a, w, np.ones(10), 0)
        assert np.allclose(plain, two)

    def test_the_look_alike_weight_moves_only_the_share_of_steals(self):
        cls, a, cell = _jar()
        w = np.ones(10)
        f = np.where(a, 10.0, 1.0)  # the live runner resembles the runners who went
        two = rp.Pass._two_step(cell, cls, a, w, f, 0).reshape(NC, 3)
        assert np.isclose(two[:, 1:].sum(), 20.0 / 28.0)  # the steal share follows the weight
        non = two[:, 0] / two[:, 0].sum()
        assert np.isclose(non[BALL], 4 / 8) and np.isclose(non[IN_PLAY], 3 / 8)
        assert np.isclose(two.sum(), 1.0)

    def test_the_plain_jar_with_the_weight_blurs_the_result_and_the_two_step_does_not(self):
        cls, a, cell = _jar()
        w = np.ones(10)
        # a weight that happens to favour the in-play rows (their runners look alike)
        f = np.where(cls == IN_PLAY, 5.0, 1.0)
        plain = rp.Pass._joint(cell, w * f, 0).reshape(NC, 3)
        two = rp.Pass._two_step(cell, cls, a, w, f, 0).reshape(NC, 3)
        assert plain[IN_PLAY].sum() > 0.6  # the pitch's result moved: 3 of 10 rows became 15 of 22
        non = two[:, 0] / two[:, 0].sum()
        assert np.isclose(non[IN_PLAY], 3 / 8)  # the ordinary weights alone

    def test_no_running_row_means_no_steal(self):
        cls, _a, _cell = _jar()
        a = np.zeros(10, dtype=bool)
        cell = cls * 3
        two = rp.Pass._two_step(cell, cls, a, np.ones(10), np.full(10, 3.0), 0).reshape(NC, 3)
        assert two[:, 1:].sum() == 0.0 and np.isclose(two.sum(), 1.0)


def _scores(joint: np.ndarray, c: int, s: int) -> dict:
    res = rp.slots_of(joint, c, s)[None, :].astype(np.float32)
    m = {
        "weight": np.ones(1, dtype=np.float32),
        "cls": np.array([c], dtype=np.int8),
        "s": np.array([s], dtype=np.int8),
    }
    keep = np.ones(1, dtype=bool)
    out = rp._metrics(res, m, keep)
    out["running"] = float(rp._per_pitch(res, m, keep)["brier_running"][0])
    return out


class TestTheRecord:
    def test_the_slots_carry_the_tables_pieces(self):
        _cls, _a, cell = _jar()
        joint = rp.Pass._joint(cell, np.ones(10), 0)
        o = rp.slots_of(joint, BALL, 1)
        assert np.isclose(o[rp.S_REAL], 0.1)  # a ball with a stolen base
        assert np.isclose(o[rp.S_CLASS], 0.6)  # a ball, steal or not
        assert np.isclose(o[rp.S_SAFE], 0.1)
        assert np.isclose(o[rp.S_JS0 : rp.S_JS0 + 6].sum(), 0.2)  # any steal
        assert np.isclose(o[rp.S_JS0 + IN_PLAY], 0.0)
        assert np.isclose(o[rp.S_CSAFE], 0.1)
        assert np.isclose(o[rp.S_SQ], float(np.dot(joint, joint)))

    def test_a_perfect_table_scores_zero(self):
        joint = np.zeros(NCELL)
        joint[BALL * 3 + 1] = 1.0
        sc = _scores(joint, BALL, 1)
        assert sc["brier_joint"] == 0.0 and sc["brier_result"] == 0.0
        assert sc["brier_steal"] == 0.0 and sc["running"] == 0.0

    def test_a_table_that_said_impossible_pays_a_bounded_price(self):
        joint = np.zeros(NCELL)
        joint[BALL * 3 + 0] = 1.0  # "a ball, and no steal, for certain"
        sc = _scores(joint, BALL, 1)  # a stolen base on a ball happened
        assert sc["said_impossible_share_of_steals"] == 1.0
        assert np.isclose(sc["brier_joint"], 2.0)  # bounded
        assert np.isclose(sc["log_floor_1e6"], -np.log(1e-6))  # the log score is the floor's
        assert np.isclose(sc["log_floor_1e3"], -np.log(1e-3))
        assert sc["brier_result"] == 0.0  # it had the pitch's result right

    def test_a_result_given_no_chance_does_not_break_the_running_score(self):
        joint = np.zeros(NCELL)
        joint[CALLED * 3 + 0] = 1.0
        sc = _scores(joint, SWING, 0)  # a swing and miss the table gave no chance
        assert np.isfinite(sc["running"]) and sc["running"] == 0.0


# ---------------------------------------------------------------------------
# the report, end to end on planted data
# ---------------------------------------------------------------------------

#: the planted truth: three results; a steal rides a ball often, a called strike
#: sometimes, a ball in play never; four steals in five are safe.
P_CLASS = {BALL: 0.5, CALLED: 0.3, IN_PLAY: 0.2}
P_STEAL = {BALL: 0.15, CALLED: 0.05, IN_PLAY: 0.0}
P_SAFE = 0.8


def _true_table() -> np.ndarray:
    j = np.zeros((NC, 3))
    for c, pc in P_CLASS.items():
        j[c, 0] = pc * (1 - P_STEAL[c])
        j[c, 1] = pc * P_STEAL[c] * P_SAFE
        j[c, 2] = pc * P_STEAL[c] * (1 - P_SAFE)
    return j.reshape(-1)


def _blind_table() -> np.ndarray:
    """The same results and the same number of steals, but the steal does not
    know the result (today's draw): a steal can land on a ball in play."""
    go = sum(P_CLASS[c] * P_STEAL[c] for c in P_CLASS)
    j = np.zeros((NC, 3))
    for c, pc in P_CLASS.items():
        j[c] = pc * np.array([1 - go, go * P_SAFE, go * (1 - P_SAFE)])
    return j.reshape(-1)


GOOD = frozenset({"steal_reads_class", "single_reads_class"})


def _write_pass(
    path: Path, season: int, n: int, rng: np.random.Generator, good: frozenset = GOOD
) -> None:
    truth = _true_table()
    cells = rng.choice(NCELL, size=n, p=truth)
    cls, s = cells // 3, cells % 3
    res = np.empty((n, len(rp.DESIGNS), len(rp.LEVELS), len(rp.SLOTS)), dtype=np.float32)
    for d, name in enumerate(rp.DESIGNS):
        table = truth if name in good else _blind_table()
        for i in range(n):
            res[i, d, :] = rp.slots_of(table, int(cls[i]), int(s[i]))
    np.savez(
        path / f"sim554_replay_{season}_neutral.npz",
        res=res,
        done=n,
        fingerprint=f"planted{season}",
        config="neutral",
        designs=np.array(rp.DESIGNS),
        levels=np.array(rp.LEVELS, dtype=np.float32),
        slots=np.array(rp.SLOTS),
        weight=np.ones(n, dtype=np.float32),
        cls=cls.astype(np.int8),
        s=s.astype(np.int8),
        btier=rng.integers(1, 4, size=n).astype(np.int8),
        rtier=rng.integers(1, 4, size=n).astype(np.int8),
        runner=rng.integers(0, 20, size=n).astype(np.int64),
        group=rng.integers(0, 300, size=n).astype(np.int32),
        target=np.full(n, 2, dtype=np.int8),
    )


class TestTheReport:
    def test_the_design_that_holds_the_true_table_wins_its_family(self, tmp_path, capsys):
        rng = np.random.default_rng(554)
        _write_pass(tmp_path, 2025, 6000, rng)
        _write_pass(tmp_path, 2026, 12000, rng)
        out_json = tmp_path / "report.json"
        args = argparse.Namespace(
            dir=str(tmp_path), tune_season=2025, test_season=2026, seed=1, json_out=str(out_json)
        )
        assert rp.report(args) == 0
        text = capsys.readouterr().out
        rep = json.loads(out_json.read_text(encoding="utf-8"))
        assert rep["pick"] == "steal_reads_class"
        assert "THE PICK (the two-step pitch draw, today's weights): steal_reads_class" in text
        fam = rep["sections"]["neutral"]["families"]
        split, single = fam["split"], fam["single"]
        assert split["ranking"][0] == "steal_reads_class"
        assert split["tied_with_the_best"] == ["steal_reads_class"]
        assert single["pick"] == "single_reads_class"
        win = split["designs"]["steal_reads_class"]
        d = win["vs_baseline_joint"]
        assert d["difference"] < -2.0 * d["standard_error"]  # better than today, beyond noise
        assert abs(win["vs_baseline_result"]["difference"]) < 1e-9  # the same pitch result
        # the blind draw puts steals on balls in play; the true table puts none there
        assert split["designs"]["today"]["test"]["steal_mix_predicted"][IN_PLAY] > 0.1
        assert win["test"]["steal_mix_predicted"][IN_PLAY] == 0.0
        assert win["test"]["steal_mix_distance"] < 0.03
        # both keep the number of steals
        t = win["test"]
        assert abs(t["steals_per_100_real"] - t["steals_per_100_predicted"]) < 0.6

    def test_a_full_tie_goes_to_the_simpler_build(self, tmp_path, capsys):
        # Two designs hold the same true table: tied on the whole table, tied on
        # who steals. The rule's last word is the simpler build.
        rng = np.random.default_rng(7)
        good = GOOD | {"steal_first", "result_steal_2step"}
        _write_pass(tmp_path, 2025, 3000, rng, good)
        _write_pass(tmp_path, 2026, 6000, rng, good)
        out_json = tmp_path / "report.json"
        args = argparse.Namespace(
            dir=str(tmp_path), tune_season=2025, test_season=2026, seed=1, json_out=str(out_json)
        )
        assert rp.report(args) == 0
        capsys.readouterr()
        split = json.loads(out_json.read_text(encoding="utf-8"))["sections"]["neutral"]["families"][
            "split"
        ]
        assert set(split["tied_with_the_best"]) == {
            "steal_reads_class",
            "steal_first",
            "result_steal_2step",
        }
        assert set(split["tied_on_who_steals"]) == set(split["tied_with_the_best"])
        assert split["pick"] == "steal_reads_class"
        order = rp.BUILD_ORDER["split"]
        assert order.index("steal_reads_class") < order.index("steal_first")
        assert set(order) == set(rp.SPLIT) and set(rp.BUILD_ORDER["single"]) == set(rp.SINGLE)

    def test_an_unfinished_pass_is_refused(self, tmp_path):
        rng = np.random.default_rng(1)
        _write_pass(tmp_path, 2025, 200, rng)
        p = tmp_path / "sim554_replay_2025_neutral.npz"
        z = dict(np.load(p, allow_pickle=False))
        z["done"] = np.array(150)
        np.savez(p, **z)
        args = argparse.Namespace(
            dir=str(tmp_path), tune_season=2025, test_season=2025, seed=1, json_out=None
        )
        try:
            rp.report(args)
        except RuntimeError as exc:
            assert "not finished" in str(exc)
        else:
            raise AssertionError("an unfinished pass must be refused")
