"""SIM-554 — every reader of a per-pitch result handles the no-pitch result.

The running game on the pitch (``docs/audit/2026-09-29-sim554-running-game-on-the-pitch-plan.md``,
§5.6 and test 12) draws the pickoff BEFORE the pitch. A pickoff that makes the
third out ends the half-inning with no pitch thrown. The loop then returns a
result whose ``pitch_outcome`` is ``NO_PITCH``: it carries the out, but no
pitch, no event and no plate appearance.

A reader that takes every result for a pitch is off by one there. These tests
drive the real loop over a synthetic bundle whose pickoff rows make that play
common, and pin one reader each:

  * the play-by-play (``simulation/snapshots.py``) skips the result and keeps
    its sequence contiguous; it also starts a new at-bat after a thrown pitch
    that ends the half (a two-out caught stealing on a ball);
  * the replay builder (``api/routes/games.py`` ``_record_and_build``) keeps
    each per-pitch snapshot on its own pitch;
  * the linescore puts the pickoff's play in the half it ended, with no run
    and no hit;
  * the pitcher decisions read the whole stream;
  * the game driver's pitch count (``simulate_game``) counts thrown pitches;
  * the per-pitch trace (``scripts/trace_game.py``) counts thrown pitches;
  * the smoke (``scripts/sim_stats.py``) records the flag it ran under;
  * the flag itself: the factory reads ``SIM_STEAL_PITCH_CLASS`` (default on)
    into the sampler it builds, the compose file carries "1", and the unit
    lane leaves it unpinned.

The acceptance lane's probes and its flag table are pinned beside the other
lane tests, in ``tests/acceptance/test_band_arithmetic_sim450.py``.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from simulation.game_state import NO_PITCH, Bases, GameState, Half, PlayResult
from simulation.linescore import InningLine, linescore_from_plays
from simulation.pitcher_decisions import decisions_from_plays
from simulation.play_recorder import RecordingMachine
from simulation.production_factory import apply_running_game_env
from simulation.sim_loop import StateMachine, simulate_game
from simulation.snapshots import PlayByPlay, thrown_pitches
from simulation.synthetic_bundle import (
    LEAGUE_INPLAY_MODEL,
    LEAGUE_PITCH_MODEL,
    synthetic_artifacts,
    synthetic_sampler,
)

SEASON = 2024
PITCHER = 477132
AWAY = [101 + i for i in range(9)]
HOME = [201 + i for i in range(9)]
SIM_KWARGS = {
    "season": SEASON,
    "pitcher_id": 600001,
    "bat_hand": "R",
    "away_lineup": AWAY,
    "home_lineup": HOME,
    "max_innings": 12,
}
_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Helpers — real no-pitch results from the real loop
# ---------------------------------------------------------------------------


def _pickoff_artifacts(weight: float):
    """A league bundle with a classed steal pool and one pickoff row in the
    0-0 count group of weight ``weight``. A runner with second open is picked
    off at a 0-0 count with probability ``weight / (1 + weight)``."""
    return synthetic_artifacts(
        pitch_model=LEAGUE_PITCH_MODEL,
        inplay_model=LEAGUE_INPLAY_MODEL,
        advancement=True,
        steal=(0.05, 0.78),
        steal_kw={"pickoff_rows": 1, "pickoff_weight": weight, "pickoff_counts": ((0, 0),)},
    )


def _machine(art, seed: int) -> StateMachine:
    return StateMachine(synthetic_sampler(art, seed), rng=np.random.default_rng(seed))


def _no_pitch_third_out() -> PlayResult:
    """One real no-pitch result: two outs, a runner on first, a 0-0 count and a
    pickoff row that outweighs every pitch row a million to one."""
    machine = _machine(_pickoff_artifacts(1e6), 0)
    state = GameState(
        pitcher_id=PITCHER,
        bat_hand="R",
        season=SEASON,
        batter_id=AWAY[3],
        away_lineup=list(AWAY),
        home_lineup=list(HOME),
        away_lineup_slot=3,
        outs=2,
        bases=Bases(first=AWAY[2]),
    )
    result = machine.step_pitch(state)
    assert result.no_pitch, "the harness must produce a no-pitch third out"
    result.next_state = copy.deepcopy(result.next_state)
    return result


def _record(seed: int) -> tuple[object, list[PlayResult]]:
    """One whole game on the pickoff bundle, every result recorded (the
    recorder deep-copies each committed state)."""
    recorder = RecordingMachine(_machine(_pickoff_artifacts(0.5), seed))
    result = simulate_game(recorder, seed=seed, **SIM_KWARGS)
    return result, list(recorder.recorded_plays)


def _recorded_game_with_a_no_pitch() -> tuple[object, list[PlayResult]]:
    """The first recorded game whose stream holds a no-pitch result with a
    thrown pitch after it, so a reader that slips by one shows it."""
    for seed in range(40):
        result, plays = _record(seed)
        idx = [i for i, p in enumerate(plays) if p.no_pitch]
        if idx and idx[0] < len(plays) - 1:
            return result, plays
    raise AssertionError("no seed in 0..39 produced a no-pitch result mid-game")


def _thrown(plays: list[PlayResult]) -> list[PlayResult]:
    return [p for p in plays if not p.no_pitch]


def _result(
    outcome: str, *, terminal: bool = False, outs: int = 0, after: GameState | None = None
) -> PlayResult:
    return PlayResult(
        pitch_outcome=outcome, pa_terminal=terminal, outs_recorded=outs, next_state=after
    )


def _after(outs: int, half: Half = Half.TOP) -> GameState:
    """The committed state after a pitch: ``outs`` outs in the first inning's
    ``half``. A play that ends the half commits the NEXT half at 0 outs."""
    return GameState(pitcher_id=PITCHER, bat_hand="R", season=SEASON, outs=outs, half=half)


def _steal_game(seed: int) -> list[PlayResult]:
    """One whole game on a bundle whose runners go often and are caught often,
    every result recorded. A two-out caught stealing on a pitch that does not
    end the plate appearance is then common."""
    art = synthetic_artifacts(
        pitch_model=LEAGUE_PITCH_MODEL,
        inplay_model=LEAGUE_INPLAY_MODEL,
        advancement=True,
        steal=(0.4, 0.2),
    )
    recorder = RecordingMachine(_machine(art, seed))
    simulate_game(recorder, seed=seed, **SIM_KWARGS)
    return list(recorder.recorded_plays)


def _rolls_on_a_thrown_pitch(play: PlayResult) -> bool:
    """True when a thrown pitch that does not end the plate appearance ends
    the half: it records an out and commits the next half at 0 outs."""
    ns = play.next_state
    return (
        not play.no_pitch
        and not play.pa_terminal
        and int(play.outs_recorded) > 0
        and ns is not None
        and int(ns.outs) == 0
    )


def _half_before_each_thrown_pitch(plays: list[PlayResult]) -> list[tuple[int, Half, int | None]]:
    """The (inning, half, batter) each thrown pitch was played in: the state
    the result before it committed. The first pitch is the top of the first;
    its batter is not on any result, so it reads None."""
    out: list[tuple[int, Half, int | None]] = []
    before: tuple[int, Half, int | None] = (1, Half.TOP, None)
    for play in plays:
        if not play.no_pitch:
            out.append(before)
        ns = play.next_state
        before = (int(ns.inning), Half(ns.half), int(ns.batter_id))
    return out


def _load_script(name: str):
    """Load ``scripts/<name>.py`` as a module, or skip when ``scripts/`` is not
    reachable (it is not bind-mounted into the app container)."""
    path = _ROOT / "scripts" / f"{name}.py"
    if not path.is_file():
        pytest.skip(f"{path} is not reachable (scripts/ is not bind-mounted)")
    spec = importlib.util.spec_from_file_location(f"_sim554_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# The play-by-play (simulation/snapshots.py)
# ---------------------------------------------------------------------------


class TestThePlayByPlay:
    def test_a_no_pitch_result_makes_no_entry(self):
        results = [
            _result("ball"),
            _result("in_play", terminal=True, outs=1),
            _result(NO_PITCH, outs=1),
            _result("called_strike"),
            _result("in_play", terminal=True),
        ]
        pbp = PlayByPlay.from_play_results(results)
        assert [e.pitch_outcome for e in pbp.entries] == [
            "ball",
            "in_play",
            "called_strike",
            "in_play",
        ]
        assert pbp.n_pitches == 4
        assert pbp.n_plate_appearances == 2

    def test_the_sequence_stays_contiguous(self):
        results = [_result("ball"), _result(NO_PITCH, outs=1), _result("ball"), _result("ball")]
        pbp = PlayByPlay.from_play_results(results)
        assert [e.sequence for e in pbp.entries] == [0, 1, 2]

    def test_a_pickoff_at_0_0_takes_no_pitch_number(self):
        """The common case: the pickoff ends the half before the PA's first
        pitch. The next half's leadoff batter starts at pitch 1 of a new
        at-bat; the voided batter leads off his team's next inning later."""
        results = [
            _result("in_play", terminal=True, outs=1),
            _result(NO_PITCH, outs=1),
            _result("ball"),
            _result("in_play", terminal=True),
        ]
        pbp = PlayByPlay.from_play_results(results)
        assert [(e.at_bat, e.pitch) for e in pbp.entries] == [(0, 1), (1, 1), (1, 2)]

    def test_a_pickoff_after_pitches_closes_that_at_bat(self):
        """The pickoff ends the half after two pitches of a plate appearance.
        The voided plate appearance keeps its two pitches; the next thrown
        pitch belongs to the next half (the other team's batter) and starts a
        new at-bat at pitch 1."""
        results = [
            _result("ball"),
            _result("ball"),
            _result(NO_PITCH, outs=1),
            _result("called_strike"),
            _result("in_play", terminal=True),
        ]
        pbp = PlayByPlay.from_play_results(results)
        assert [(e.at_bat, e.pitch) for e in pbp.entries] == [(0, 1), (0, 2), (1, 1), (1, 2)]
        assert pbp.n_plate_appearances == 1

    def test_a_caught_stealing_that_ends_the_half_closes_the_at_bat(self):
        """Two outs, a runner on first. The runner is caught on ball two: the
        third out lands on a pitch that does not end the plate appearance, and
        the half rolls. The next half's first pitch belongs to the other
        team's batter, so it starts a new at-bat at pitch 1."""
        results = [
            _result("ball", after=_after(2)),
            _result("ball", outs=1, after=_after(0, Half.BOTTOM)),
            _result("called_strike", after=_after(0, Half.BOTTOM)),
            _result("in_play", terminal=True, outs=1, after=_after(1, Half.BOTTOM)),
        ]
        pbp = PlayByPlay.from_play_results(results)
        assert [(e.at_bat, e.pitch) for e in pbp.entries] == [(0, 1), (0, 2), (1, 1), (1, 2)]
        assert pbp.n_plate_appearances == 1

    def test_an_out_that_does_not_end_the_half_keeps_the_at_bat(self):
        """A runner picked off or caught stealing with no out before it: the
        out is the first, not the third (the committed state holds 1 out). The
        batter stays up, so his next pitch stays in his at-bat."""
        results = [
            _result("ball", outs=1, after=_after(1)),
            _result("in_play", terminal=True, outs=1, after=_after(2)),
        ]
        pbp = PlayByPlay.from_play_results(results)
        assert [(e.at_bat, e.pitch) for e in pbp.entries] == [(0, 1), (0, 2)]

    def test_a_terminal_pitch_that_ends_the_half_counts_once(self):
        """The third out on a ball in play ends the plate appearance AND the
        half. The at-bat moves on by one, not two."""
        results = [
            _result("in_play", terminal=True, outs=1, after=_after(0, Half.BOTTOM)),
            _result("ball", after=_after(0, Half.BOTTOM)),
            _result("in_play", terminal=True, outs=1, after=_after(1, Half.BOTTOM)),
        ]
        pbp = PlayByPlay.from_play_results(results)
        assert [(e.at_bat, e.pitch) for e in pbp.entries] == [(0, 1), (1, 1), (1, 2)]
        assert pbp.n_plate_appearances == 2

    def test_no_real_at_bat_spans_two_halves(self):
        """Real games with many caught stealings. Every at-bat holds the
        pitches of one half-inning and one batter, and the pitch after a
        caught stealing that ends the half opens a new at-bat at pitch 1."""
        rolls = 0
        for seed in range(4):
            plays = _steal_game(seed)
            pbp = PlayByPlay.from_play_results(plays)
            pitched = thrown_pitches(plays)
            where = _half_before_each_thrown_pitch(plays)
            by_at_bat: dict[int, set[tuple[int, Half]]] = {}
            batters: dict[int, set[int]] = {}
            for entry, (inning, half, batter) in zip(pbp.entries, where, strict=True):
                by_at_bat.setdefault(entry.at_bat, set()).add((inning, half))
                if batter is not None:
                    batters.setdefault(entry.at_bat, set()).add(batter)
            assert all(len(h) == 1 for h in by_at_bat.values()), seed
            assert all(len(b) == 1 for b in batters.values()), seed
            for i, play in enumerate(pitched[:-1]):
                if _rolls_on_a_thrown_pitch(play):
                    rolls += 1
                    now, nxt = pbp.entries[i], pbp.entries[i + 1]
                    assert (nxt.at_bat, nxt.pitch) == (now.at_bat + 1, 1), (seed, i)
        assert rolls > 0, "no caught stealing ended a half; the test proves nothing"

    def test_an_ordinary_game_numbers_as_before(self):
        """A game with no steal pool has no out on a pitch that leaves the
        plate appearance open. Its at-bats and pitch numbers are exactly the
        ones the terminal flag alone gives."""
        art = synthetic_artifacts(
            pitch_model=LEAGUE_PITCH_MODEL, inplay_model=LEAGUE_INPLAY_MODEL, advancement=True
        )
        recorder = RecordingMachine(_machine(art, 3))
        simulate_game(recorder, seed=3, **SIM_KWARGS)
        plays = list(recorder.recorded_plays)
        assert not any(_rolls_on_a_thrown_pitch(p) for p in plays)
        want, at_bat, pitch = [], 0, 0
        for play in thrown_pitches(plays):
            pitch += 1
            want.append((at_bat, pitch))
            if play.pa_terminal:
                at_bat, pitch = at_bat + 1, 0
        pbp = PlayByPlay.from_play_results(plays)
        assert [(e.at_bat, e.pitch) for e in pbp.entries] == want

    def test_a_real_game_serves_thrown_pitches_only(self):
        _game, plays = _recorded_game_with_a_no_pitch()
        pbp = PlayByPlay.from_play_results(plays)
        assert pbp.n_pitches == len(_thrown(plays)) < len(plays)
        assert NO_PITCH not in {e.pitch_outcome for e in pbp.entries}
        assert [e.sequence for e in pbp.entries] == list(range(pbp.n_pitches))

    def test_the_thrown_pitches_pair_one_to_one_with_the_entries(self):
        """``thrown_pitches`` is the list the replay builder zips with the
        entries: the same length, the same pitch in each position."""
        _game, plays = _recorded_game_with_a_no_pitch()
        pbp = PlayByPlay.from_play_results(plays)
        pitched = thrown_pitches(plays)
        assert pitched == _thrown(plays)
        for play, entry in zip(pitched, pbp.entries, strict=True):
            assert entry.pitch_outcome == play.pitch_outcome
            assert entry.is_pa_end == play.pa_terminal
            assert entry.runs_scored == play.runs_scored
            assert entry.outs_recorded == play.outs_recorded


# ---------------------------------------------------------------------------
# The replay builder (api/routes/games.py _record_and_build)
# ---------------------------------------------------------------------------


class TestTheReplayBuilder:
    def _build(self, monkeypatch):
        import api.routes.games as games_mod

        game, plays = _recorded_game_with_a_no_pitch()
        monkeypatch.setattr(games_mod, "record_game_plays", lambda **_kw: (game, plays))
        seen: dict[str, int] = {}
        real_ls, real_dec = games_mod.linescore_from_plays, games_mod.decisions_from_plays

        def ls(stream):
            seen["linescore"] = len(stream)
            return real_ls(stream)

        def dec(stream):
            seen["decisions"] = len(stream)
            return real_dec(stream)

        monkeypatch.setattr(games_mod, "linescore_from_plays", ls)
        monkeypatch.setattr(games_mod, "decisions_from_plays", dec)
        built = games_mod._record_and_build(factory_ref="unused:unused", base_seed=1, sim_kwargs={})
        return plays, seen, built

    def test_the_play_by_play_holds_thrown_pitches_only(self, monkeypatch):
        plays, _seen, (pbp, snapshots, _ls, _dec) = self._build(monkeypatch)
        assert pbp.n_pitches == len(_thrown(plays))
        assert len(snapshots) == pbp.n_pitches

    def test_every_snapshot_stays_on_its_own_pitch(self, monkeypatch):
        """Each snapshot is the state after its OWN pitch. A builder that
        paired the full stream with the thrown pitches' entries would shift
        every snapshot after the pickoff by one pitch."""
        from api.serialization import to_jsonable
        from simulation.snapshots import StateAtPitch

        plays, _seen, (pbp, snapshots, _ls, _dec) = self._build(monkeypatch)
        for play, entry, snap in zip(_thrown(plays), pbp.entries, snapshots, strict=True):
            want = StateAtPitch.from_game_state(
                play.next_state, at_bat=entry.at_bat, pitch=entry.pitch, sequence=entry.sequence
            )
            assert snap == to_jsonable(want)

    def test_the_linescore_and_decisions_read_the_whole_stream(self, monkeypatch):
        """The pickoff's out lives on the no-pitch result, so the game card
        reads every result."""
        plays, seen, _built = self._build(monkeypatch)
        assert seen == {"linescore": len(plays), "decisions": len(plays)}


# ---------------------------------------------------------------------------
# The linescore and the pitcher decisions
# ---------------------------------------------------------------------------


class TestTheGameCard:
    def test_the_linescore_puts_the_pickoff_in_the_half_it_ended(self):
        """The no-pitch result's committed state is the NEXT half (the bottom
        of the first). The linescore puts the play in the top it ended: the
        bottom stays unplayed, and the play adds no run, no hit and no error."""
        result = _no_pitch_third_out()
        assert result.outs_recorded == 1 and result.runs_scored == 0
        assert result.next_state.half == Half.BOTTOM and result.next_state.outs == 0
        ls = linescore_from_plays([result])
        assert ls.innings == [InningLine(inning=1, away=0, home=None)]
        assert (ls.away_runs, ls.home_runs) == (0, 0)
        assert (ls.away_hits, ls.home_hits) == (0, 0)
        assert (ls.away_errors, ls.home_errors) == (0, 0)

    def test_the_linescore_of_a_real_game_matches_its_score(self):
        game, plays = _recorded_game_with_a_no_pitch()
        ls = linescore_from_plays(plays)
        assert (ls.away_runs, ls.home_runs) == (game.away_score, game.home_score)

    def test_the_pitcher_decisions_accept_the_stream(self):
        game, plays = _recorded_game_with_a_no_pitch()
        dec = decisions_from_plays(plays)
        assert (dec.away_score, dec.home_score) == (game.away_score, game.home_score)
        if game.away_score != game.home_score:
            assert dec.winning_pitcher_id is not None
            assert dec.losing_pitcher_id is not None


# ---------------------------------------------------------------------------
# The game driver's pitch count (simulation/sim_loop.py simulate_game)
# ---------------------------------------------------------------------------


def test_simulate_game_counts_thrown_pitches_only():
    """``GameSimResult.total_pitches`` counts the pitches thrown. A no-pitch
    result is a step of the loop and no pitch."""
    no_pitch_games = 0
    for seed in range(6):
        game, plays = _record(seed)
        thrown = sum(not p.no_pitch for p in plays)
        assert game.total_pitches == thrown
        no_pitch_games += thrown < len(plays)
    assert no_pitch_games > 0, "no game produced a no-pitch result; the test proves nothing"


# ---------------------------------------------------------------------------
# The per-pitch trace (scripts/trace_game.py) and the smoke (scripts/sim_stats.py)
# ---------------------------------------------------------------------------


def test_the_trace_summary_counts_thrown_pitches():
    trace = _load_script("trace_game")
    rows = [
        {"pitch": 1, "pitch_outcome": "ball"},
        {"pitch": 2, "pitch_outcome": NO_PITCH},
        {"pitch": 3, "pitch_outcome": "in_play"},
    ]
    assert trace._pitches_thrown(rows) == 2


def test_the_smoke_records_the_flag():
    """The smoke prints every flag in ``_REALISM_FLAGS``, so its record names
    the order the running game ran in. Read by parsing: the script needs the
    database to import."""
    path = _ROOT / "scripts" / "sim_stats.py"
    if not path.is_file():
        pytest.skip(f"{path} is not reachable (scripts/ is not bind-mounted)")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    flags = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "_REALISM_FLAGS" for t in node.targets)
    )
    assert "SIM_STEAL_PITCH_CLASS" in flags


# ---------------------------------------------------------------------------
# The flag: SIM_STEAL_PITCH_CLASS
# ---------------------------------------------------------------------------


class TestTheFlag:
    def test_it_defaults_on(self):
        s = SimpleNamespace()
        apply_running_game_env(s, env={})
        assert s.steal_pitch_class is True

    @pytest.mark.parametrize("raw", ["0", "false", "off", "no", "", " OFF "])
    def test_the_off_values(self, raw):
        s = SimpleNamespace()
        apply_running_game_env(s, env={"SIM_STEAL_PITCH_CLASS": raw})
        assert s.steal_pitch_class is False

    @pytest.mark.parametrize("raw", ["1", "true", "on", "yes"])
    def test_the_on_values(self, raw):
        s = SimpleNamespace()
        apply_running_game_env(s, env={"SIM_STEAL_PITCH_CLASS": raw})
        assert s.steal_pitch_class is True

    def test_the_unit_lane_does_not_pin_it(self):
        """``tests/conftest.py`` pins no value: the unit lane builds its
        samplers directly, so the environment never reaches them, and the
        no-database tests run the new order on purpose (plan §4.5)."""
        src = (_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
        assert 'os.environ["SIM_STEAL_PITCH_CLASS"]' not in src

    def test_the_compose_file_carries_it_on(self):
        path = _ROOT / "docker-compose.yml"
        if not path.is_file():
            pytest.skip(f"{path} is not reachable (repo-root files are not bind-mounted)")
        text = path.read_text(encoding="utf-8")
        line = next(
            ln for ln in text.splitlines() if ln.strip().startswith("SIM_STEAL_PITCH_CLASS:")
        )
        assert line.split(":", 1)[1].strip().strip('"') == "1", line

    def test_the_sampler_default_is_on(self):
        assert synthetic_sampler(_pickoff_artifacts(0.01), 0).steal_pitch_class is True

    @pytest.mark.parametrize(("raw", "want"), [("0", False), ("1", True), ("off", False)])
    def test_the_factory_applies_it(self, monkeypatch, tmp_path, raw, want):
        """The factory is the only place production reads the flag. The
        sampler's own default is on, so without this read "0" (the rollback
        to the single pre-pitch draw) does nothing. The factory builds its
        sampler here over a classed synthetic bundle: the bundle load is the
        one step replaced."""
        import simulation.production_factory as pf
        from pipeline.batch.engine_artifacts import EngineArtifacts
        from simulation.batch_runner import GameSpec

        art = _pickoff_artifacts(0.01)
        monkeypatch.setattr(EngineArtifacts, "load", classmethod(lambda cls, *a, **k: art))
        monkeypatch.setattr(pf, "_CACHED_FULL_POOL_SAMPLER", None)
        monkeypatch.setattr(pf, "_CACHED_FULL_POOL_ART_DIR", None)
        monkeypatch.setenv("SIM_STEAL_PITCH_CLASS", raw)
        sampler = pf._build_full_pool_sampler(GameSpec(sim_kwargs={"_pool_dir": str(tmp_path)}), 0)
        assert sampler.a is art
        assert sampler.steal_pitch_class is want
        assert sampler.has_steal_classes() is True
        assert StateMachine(sampler)._steal_order_active() is want


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
