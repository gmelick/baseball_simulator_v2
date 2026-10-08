"""SIM-557 — the outs check in the smoke harness (the plan's tests 29 and 30).

Every out the game plays belongs to one pitcher's line.  The smoke harness
``scripts/sim_stats.py`` reads the outs on the pitchers' lines against the outs
each game-simulation played.  The two numbers are an identity, so the check is
the harness's one gate: a game-simulation where they differ exits the harness
with code 1.

The tests drive two seams with hand-made game summaries and no live data:

* ``outs_credit_check``, the pure function that counts the mismatches.
* ``main``, with every simulator call replaced by a stub, so the gate's exit
  code is read where the harness raises it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sim_stats = _load("sim_stats")


def _summary(p_outs: int, outs_played: int | None) -> dict:
    """One game summary: the two keys the outs check reads.

    ``main`` also prints a per-game progress line from seven channels, so
    the summary carries them at zero.
    """
    progress = dict.fromkeys(("R", "H", "HR", "BB", "K", "home_R", "away_R"), 0)
    return {**progress, "p_outs": p_outs, "outs_played": outs_played}


def _run_main(monkeypatch, tmp_path, per_game: list[list[dict]]) -> dict:
    """Run ``main`` over hand-made summaries; return the JSON record it writes.

    Each game in ``per_game`` is one game_pk, and each summary is one
    iteration.  The stubs replace the database, the state resolve, the
    machine build and the simulation; ``_game_summary`` hands back the
    prepared summaries in order.
    """
    queue = [s for game in per_game for s in game]
    iters = len(per_game[0])
    out_path = tmp_path / "record.json"

    async def _fake_resolve(gp, duck):
        return SimpleNamespace(park_run_factor=1.0, home_defense={}, away_defense={})

    monkeypatch.setattr(sim_stats, "open_sim_duckdb", lambda: None)
    monkeypatch.setattr(sim_stats, "_resolve", _fake_resolve)
    monkeypatch.setattr(sim_stats, "sim_kwargs_from_state", lambda state: {})
    monkeypatch.setattr(sim_stats, "production_machine_factory", lambda i, spec: SimpleNamespace())
    monkeypatch.setattr(sim_stats, "simulate_game", lambda **kw: object())
    monkeypatch.setattr(sim_stats, "_game_summary", lambda res, **kw: queue.pop(0))
    monkeypatch.setattr(sim_stats, "_aggregate", lambda per_game: {})
    monkeypatch.setattr(sim_stats, "_print_report", lambda agg, **kw: None)
    monkeypatch.setattr(sim_stats, "_fence_counter_summary", lambda counts: ("fence", {}))
    monkeypatch.setattr(sim_stats, "_fence_d_read", lambda record: ("read", True))
    argv = ["sim_stats.py"]
    argv += [str(700000 + i) for i in range(len(per_game))]
    argv += ["--iters", str(iters), "--json-out", str(out_path)]
    monkeypatch.setattr(sys, "argv", argv)
    try:
        sim_stats.main()
    finally:
        record = json.loads(out_path.read_text()) if out_path.exists() else {}
    return record


class TestTheOutsCheck:
    def test_every_summary_whose_credited_outs_equal_the_played_outs_reads_zero_mismatches(
        self,
    ):
        per_game = [
            [_summary(51, 51), _summary(54, 54)],
            [_summary(53, 53), _summary(48, 48)],
        ]
        check = sim_stats.outs_credit_check(per_game)
        assert check["available"] is True
        assert check["mismatch_game_sims"] == 0
        assert check["credited_per_game"] == pytest.approx(51.5)
        assert check["played_per_game"] == pytest.approx(51.5)

    def test_one_summary_one_out_short_counts_one_mismatch(self):
        per_game = [
            [_summary(51, 51), _summary(53, 54)],
            [_summary(53, 53), _summary(48, 48)],
        ]
        check = sim_stats.outs_credit_check(per_game)
        assert check["available"] is True
        assert check["mismatch_game_sims"] == 1
        assert check["credited_per_game"] == pytest.approx(51.25)
        assert check["played_per_game"] == pytest.approx(51.5)

    def test_summaries_without_a_played_count_read_as_not_available(self):
        per_game = [[_summary(51, None), _summary(54, None)]]
        check = sim_stats.outs_credit_check(per_game)
        assert check["available"] is False
        assert check["played_per_game"] is None
        assert check["mismatch_game_sims"] == 0
        assert check["credited_per_game"] == pytest.approx(52.5)

    def test_one_summary_without_a_played_count_makes_the_whole_check_not_available(self):
        per_game = [[_summary(51, 51), _summary(53, None)]]
        check = sim_stats.outs_credit_check(per_game)
        assert check["available"] is False
        assert check["mismatch_game_sims"] == 0

    def test_the_check_names_the_first_differing_game_sims_in_run_order(self):
        per_game = [
            [_summary(51, 51), _summary(53, 54)],
            [_summary(52, 51), _summary(48, 48)],
        ]
        check = sim_stats.outs_credit_check(per_game)
        assert check["mismatch_game_sims"] == 2
        assert check["first_mismatches"] == [
            {"game_index": 0, "iteration": 1, "credited": 53, "played": 54},
            {"game_index": 1, "iteration": 0, "credited": 52, "played": 51},
        ]

    def test_the_check_names_at_most_the_cap_but_counts_every_mismatch(self):
        per_game = [[_summary(50, 51) for _ in range(sim_stats.MAX_NAMED_MISMATCHES + 3)]]
        check = sim_stats.outs_credit_check(per_game)
        assert check["mismatch_game_sims"] == sim_stats.MAX_NAMED_MISMATCHES + 3
        assert len(check["first_mismatches"]) == sim_stats.MAX_NAMED_MISMATCHES
        assert [m["iteration"] for m in check["first_mismatches"]] == list(
            range(sim_stats.MAX_NAMED_MISMATCHES)
        )

    def test_a_check_that_is_not_available_names_no_game_sim(self):
        check = sim_stats.outs_credit_check([[_summary(51, None)]])
        assert check["first_mismatches"] == []

    def test_an_empty_run_reads_as_not_available(self):
        check = sim_stats.outs_credit_check([])
        assert check["available"] is False
        assert check["mismatch_game_sims"] == 0

    def test_the_report_line_names_both_means_and_the_mismatch_count(self):
        line = sim_stats._outs_credit_line(
            {
                "credited_per_game": 51.25,
                "played_per_game": 51.5,
                "mismatch_game_sims": 1,
                "available": True,
            }
        )
        assert line == (
            "pitcher outs a game: credited 51.25, played 51.50; game-sims where they differ: 1"
        )

    def test_the_report_line_says_the_played_count_is_not_available_on_an_older_simulator(
        self,
    ):
        line = sim_stats._outs_credit_line(
            {
                "credited_per_game": 52.5,
                "played_per_game": None,
                "mismatch_game_sims": 0,
                "available": False,
            }
        )
        assert line == (
            "pitcher outs a game: credited 52.50, "
            "played: not available (the simulator predates SIM-557)"
        )


class TestTheGate:
    def test_a_run_where_every_game_sim_matches_exits_cleanly(self, monkeypatch, tmp_path):
        per_game = [[_summary(51, 51), _summary(54, 54)]]
        record = _run_main(monkeypatch, tmp_path, per_game)
        assert record["outs_mismatch_game_sims"] == 0
        assert record["outs_credited_per_game"] == pytest.approx(52.5)
        assert record["outs_played_per_game"] == pytest.approx(52.5)

    def test_one_game_sim_one_out_short_exits_with_code_one(self, monkeypatch, tmp_path, capsys):
        per_game = [
            [_summary(51, 51), _summary(53, 54)],
            [_summary(48, 48), _summary(51, 51)],
        ]
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, tmp_path, per_game)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "FAIL: the pitchers' outs differ from the outs played in 1 game-sims" in out

    def test_the_record_is_written_before_the_gate_exits(self, monkeypatch, tmp_path):
        per_game = [[_summary(51, 51), _summary(53, 54)]]
        with pytest.raises(SystemExit):
            _run_main(monkeypatch, tmp_path, per_game)
        record = json.loads((tmp_path / "record.json").read_text())
        assert record["outs_mismatch_game_sims"] == 1
        assert record["outs_played_per_game"] == pytest.approx(52.5)

    def test_the_fail_output_names_each_differing_game_sim_by_game_pk_and_seed(
        self, monkeypatch, tmp_path, capsys
    ):
        # The plan's test 30: the line names the game-simulation to replay.
        per_game = [
            [_summary(51, 51), _summary(53, 54)],
            [_summary(48, 48), _summary(50, 51)],
        ]
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, tmp_path, per_game)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "FAIL: the pitchers' outs differ from the outs played in 2 game-sims" in out
        assert "game 700000 seed 1: credited 53, played 54" in out
        assert "game 700001 seed 1: credited 50, played 51" in out
        record = json.loads((tmp_path / "record.json").read_text())
        assert record["outs_first_mismatches"] == [
            {"game_index": 0, "iteration": 1, "credited": 53, "played": 54, "game_pk": 700000},
            {"game_index": 1, "iteration": 1, "credited": 50, "played": 51, "game_pk": 700001},
        ]

    def test_an_older_simulator_without_a_played_count_never_exits(
        self, monkeypatch, tmp_path, capsys
    ):
        per_game = [[_summary(51, None), _summary(40, None)]]
        record = _run_main(monkeypatch, tmp_path, per_game)
        assert record["outs_played_per_game"] is None
        assert record["outs_mismatch_game_sims"] == 0
        out = capsys.readouterr().out
        assert "played: not available (the simulator predates SIM-557)" in out
        assert "FAIL" not in out


class TestTheRealSummaryCarriesTheTwoKeys:
    """The gate reads two keys that ``_game_summary`` writes from a real game
    result.  The tests above hand ``main`` prepared summaries, so they cannot
    see a renamed key or attribute.  This test runs a real game on the
    synthetic bundle and reads the real summary."""

    def test_the_summary_of_a_real_game_carries_the_box_outs_and_the_played_outs(self):
        from simulation.batch_runner import GameSpec, rng_driven_machine_factory
        from simulation.sim_loop import simulate_game

        home_lineup = list(range(401, 410))
        away_lineup = list(range(301, 310))
        machine = rng_driven_machine_factory(3, GameSpec(sim_kwargs={}))
        result = simulate_game(
            machine,
            seed=3,
            pitcher_id=100,
            home_pitcher_id=100,
            away_pitcher_id=200,
            away_lineup=away_lineup,
            home_lineup=home_lineup,
        )
        summary = sim_stats._game_summary(
            result,
            home_ids={*home_lineup, 100},
            away_ids={*away_lineup, 200},
        )
        box_outs = sum(ln.outs_recorded for ln in result.boxscore.lines.values())
        assert result.outs_played > 0
        assert summary["outs_played"] == result.outs_played
        assert summary["p_outs"] == box_outs
        check = sim_stats.outs_credit_check([[summary]])
        assert check["available"] is True
        assert check["mismatch_game_sims"] == 0
