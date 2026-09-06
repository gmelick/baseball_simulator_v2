"""
tests/unit/test_sim498_rng_streams.py
=====================================
SIM-498 — the loop rng and the full-pool rng must be INDEPENDENT streams.

The defect
----------
``simulate_game`` built every generator from the SAME integer::

    state_machine.rng   = np.random.default_rng(seed)   # loop draws
    fp.rng              = np.random.default_rng(seed)   # pitch + batted-ball draws

Two generators seeded with one integer emit IDENTICAL sequences. The loop rng
draws advancement, steal outcomes, manager decisions and the framing nudge; the
full-pool rng draws the pitch and the batted ball. The model treats those as
independent events. They came off one sequence.

Honest scope note, restated from the ticket: the two streams emitted identical
SEQUENCES, but the loop and the sampler CONSUME them at different rates, so the
alignment drifts across a game and the dependence is not one-to-one. These tests
prove the streams are no longer built from one integer and that reproducibility
survives. They do NOT claim a magnitude for the old dependence — nobody measured
it.

What each test must be able to detect
-------------------------------------
* :class:`TestSimulateGameBindsIndependentStreams` FAILS when the two generators
  are seeded from the same integer. That is the pre-fix code, and it is the whole
  point of the module.
* :class:`TestPerGameSeedReproducibility` pins that a fixed (game, seed) replays
  byte-identically, and — so the pin is not vacuous — that a DIFFERENT seed
  produces a different game. A constant simulator would pass the first assertion
  alone.
"""

from __future__ import annotations

import copy

import numpy as np

from pipeline.batch.engine_artifacts import EngineArtifacts
from simulation.full_pool_sampler import FullPoolSampler
from simulation.sim_loop import (
    FULL_POOL_STREAM,
    LOOP_STREAM,
    StateMachine,
    simulate_game,
    spawn_rng_streams,
)
from simulation.synthetic_bundle import synthetic_artifacts

_SEASON = 2024
_PITCHER = 900
_AWAY_LINEUP = [101, 102, 103, 104, 105, 106, 107, 108, 109]
_HOME_LINEUP = [201, 202, 203, 204, 205, 206, 207, 208, 209]

#: The five pitch outcomes the count machine understands.
_OUTCOMES = ("ball", "called_strike", "swinging_strike", "foul", "in_play")


# ===========================================================================
# A small but REAL full-pool sampler, so the detector runs the production seam
# ===========================================================================


def _artifacts() -> EngineArtifacts:
    """A small but REAL bundle: every count draws every outcome with equal
    weight, every base-out cell resolves contact, no advancement pools (this
    module cares about WHICH generator drives the draw, nothing else)."""
    return synthetic_artifacts(
        pitch_model=dict.fromkeys(_OUTCOMES, 1.0),
        inplay_model={"field_out": 1.0, "single": 1.0, "double": 1.0, "home_run": 1.0},
        advancement=False,
    )


# ===========================================================================
# Spies that snapshot every Generator bound to ``.rng``
# ===========================================================================


def _snapshot(gen: np.random.Generator) -> dict:
    """The generator's bit-generator state, captured BEFORE anything consumes it."""
    return copy.deepcopy(gen.bit_generator.state)


def _draws_from(state: dict, n: int = 8) -> list[float]:
    """Replay the first ``n`` draws of a generator restored from ``state``."""
    gen = np.random.default_rng()
    gen.bit_generator.state = state
    return [float(gen.random()) for _ in range(n)]


class _SpyMachine(StateMachine):
    """A ``StateMachine`` that records the state of every Generator bound to
    ``.rng``. ``simulate_game`` rebinds it, so the LAST record is the loop stream
    the game actually ran on."""

    @property
    def rng(self) -> np.random.Generator:
        return self.__dict__["_rng_obj"]

    @rng.setter
    def rng(self, value: np.random.Generator) -> None:
        self.__dict__["_rng_obj"] = value
        self.__dict__.setdefault("rng_states", []).append(_snapshot(value))


class _SpyPool(FullPoolSampler):
    """A ``FullPoolSampler`` that records the state of every Generator bound to
    ``.rng``. ``simulate_game`` rebinds it once per game."""

    @property
    def rng(self) -> np.random.Generator:
        return self.__dict__["_rng_obj"]

    @rng.setter
    def rng(self, value: np.random.Generator) -> None:
        self.__dict__["_rng_obj"] = value
        self.__dict__.setdefault("rng_states", []).append(_snapshot(value))


def _spy_game(seed: int) -> tuple[_SpyMachine, _SpyPool, object]:
    """Run one complete game and return the two spies plus the result."""
    fp = _SpyPool(_artifacts(), np.random.default_rng(0))
    machine = _SpyMachine(fp)
    result = simulate_game(
        state_machine=machine,
        seed=seed,
        pitcher_id=_PITCHER,
        season=_SEASON,
        away_lineup=list(_AWAY_LINEUP),
        home_lineup=list(_HOME_LINEUP),
        home_pitcher_id=_PITCHER,
        away_pitcher_id=_PITCHER,
    )
    return machine, fp, result


# ===========================================================================
# 1. The helper itself
# ===========================================================================


class TestSpawnRngStreams:
    def test_two_streams_from_one_integer_are_not_identical(self):
        """The fix: one seed -> two streams that do NOT emit the same numbers."""
        loop, pool = spawn_rng_streams(1234)
        assert [loop.random() for _ in range(8)] != [pool.random() for _ in range(8)]

    def test_the_defect_control_two_default_rngs_ARE_identical(self):
        """The pre-fix construction, pinned so the defect is not re-introduced by
        someone who believes two ``default_rng(seed)`` calls differ. They do not."""
        a, b = np.random.default_rng(1234), np.random.default_rng(1234)
        assert [a.random() for _ in range(8)] == [b.random() for _ in range(8)]

    def test_stream_indices_are_distinct(self):
        assert LOOP_STREAM != FULL_POOL_STREAM

    def test_spawn_is_reproducible_for_a_fixed_seed(self):
        first = [g.random() for g in spawn_rng_streams(77)]
        second = [g.random() for g in spawn_rng_streams(77)]
        assert first == second

    def test_different_seeds_give_different_streams(self):
        assert [g.random() for g in spawn_rng_streams(1)] != [
            g.random() for g in spawn_rng_streams(2)
        ]

    def test_seed_none_draws_fresh_entropy(self):
        """``default_rng(None)`` drew OS entropy; the spawn path must keep that."""
        assert [g.random() for g in spawn_rng_streams(None)] != [
            g.random() for g in spawn_rng_streams(None)
        ]


# ===========================================================================
# 2. THE DETECTOR — this class goes RED against the pre-fix code
# ===========================================================================


class TestSimulateGameBindsIndependentStreams:
    """Fails when ``simulate_game`` builds the loop rng and the full-pool rng from
    the same integer.

    The spies snapshot each generator at BIND time, before the game consumes it,
    so the comparison is of the streams themselves and not of the draws each
    consumer happened to take."""

    def test_loop_and_full_pool_streams_differ_at_bind_time(self):
        machine, fp, _ = _spy_game(seed=4242)
        loop_state = machine.__dict__["rng_states"][-1]
        pool_state = fp.__dict__["rng_states"][-1]
        # Pre-fix these are two ``default_rng(4242)`` objects: identical state,
        # identical draws, and this assertion fails.
        assert _draws_from(loop_state) != _draws_from(pool_state)

    def test_neither_stream_equals_a_plain_default_rng_of_the_seed(self):
        """A spawned child is not ``default_rng(seed)``. If either consumer is ever
        wired back to the raw integer, this catches it."""
        seed = 4242
        machine, fp, _ = _spy_game(seed=seed)
        naive = [float(np.random.default_rng(seed).random()) for _ in range(1)]
        loop_first = _draws_from(machine.__dict__["rng_states"][-1], 1)
        pool_first = _draws_from(fp.__dict__["rng_states"][-1], 1)
        assert loop_first != naive
        assert pool_first != naive

    def test_the_bound_streams_are_the_spawned_children(self):
        """Pin WHICH child goes where, so a future edit cannot silently swap the
        loop stream and the full-pool stream."""
        seed = 4242
        machine, fp, _ = _spy_game(seed=seed)
        expected = spawn_rng_streams(seed)
        assert _draws_from(machine.__dict__["rng_states"][-1]) == [
            float(expected[LOOP_STREAM].random()) for _ in range(8)
        ]
        assert _draws_from(fp.__dict__["rng_states"][-1]) == [
            float(expected[FULL_POOL_STREAM].random()) for _ in range(8)
        ]


# ===========================================================================
# 3. Per-(game, seed) reproducibility must survive the change
# ===========================================================================


def _fingerprint(result) -> tuple:
    """A byte-level signature of a simulated game."""
    box = result.boxscore
    lines = ()
    if box is not None:
        lines = tuple(
            (
                pid,
                ln.ab,
                ln.h,
                ln.hr,
                ln.rbi,
                ln.k,
                ln.bb,
                ln.outs_recorded,
                ln.er,
            )
            for pid, ln in sorted(box.lines.items())
        )
    return (
        result.home_score,
        result.away_score,
        result.innings_played,
        result.total_pitches,
        result.walk_off,
        result.extra_innings,
        lines,
    )


class TestPerGameSeedReproducibility:
    def test_same_game_same_seed_replays_byte_identically(self):
        _, _, first = _spy_game(seed=20240610)
        _, _, second = _spy_game(seed=20240610)
        assert _fingerprint(first) == _fingerprint(second)

    def test_a_different_seed_produces_a_different_game(self):
        """Gives the reproducibility pin teeth. A simulator that ignored its seed
        would pass the test above and fail this one."""
        seeds = (11, 22, 33, 44, 55, 66)
        prints = {_fingerprint(_spy_game(seed=s)[2]) for s in seeds}
        assert len(prints) > 1

    def test_reproducibility_holds_on_an_unseeded_machine(self):
        """A machine built with fresh-entropy generators: ``simulate_game``
        rebinds BOTH from the per-game seed, so the game is still reproducible."""
        kwargs = {
            "seed": 987,
            "pitcher_id": _PITCHER,
            "season": _SEASON,
            "away_lineup": list(_AWAY_LINEUP),
            "home_lineup": list(_HOME_LINEUP),
        }

        def _fresh() -> StateMachine:
            return StateMachine(_SpyPool(_artifacts(), np.random.default_rng()))

        first = simulate_game(_fresh(), **kwargs)
        second = simulate_game(_fresh(), **kwargs)
        assert _fingerprint(first) == _fingerprint(second)
