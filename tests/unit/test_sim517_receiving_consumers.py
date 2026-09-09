"""
tests/unit/test_sim517_receiving_consumers.py
=============================================
SIM-517 parts C + D — the catcher RECEIVING kernel on the pitch draw and the
got-away resolution in the loop.

Part C (sampler) — the bell-curve receiving kernel — was DELETED by SIM-523
part E (2026-09-09; the ratio factor replaces it, tests in
test_sim523_receiving_ratio.py); only the got-away reads stay here.

Part D (loop): the drawn pitch row's ``got_away`` fact IS the play — runners
advance one base (scoring from third, no RBI, the run routed through
``_commit_run_delta``), a got-away swinging strike-3 awards first when the
official rule allows, and with the rule blocking the reach the runners still
advance. ``SIM_GOT_AWAY`` off (the default) touches nothing.
"""

from __future__ import annotations

import numpy as np

from pipeline.batch.engine_artifacts import EngineArtifacts, HandPool
from simulation.full_pool_sampler import FullPoolSampler
from simulation.game_state import Bases, GameState, PlayResult
from simulation.sim_loop import StateMachine

_PITCHER = "100:2024"
_SEASON = 2024

#: Three catchers: 700 (an average receiver), 701 (a poor receiver — every
#: rate one full unit of raw scale away), 702 (a twin of 700 — the live one).
_RECV_FEATURES = [
    "shadow_zone_strike_rate",
    "heart_zone_strike_rate",
    "actual_pbwp",
    "pitches_received_total",
    "uncaught_k3_rate_eb",
]


def _catcher_emb() -> dict:
    # columns: shadow, heart, actual_pbwp, pitches_received_total, uncaught_eb
    vecs = np.array(
        [
            [0.60, 0.95, 10.0, 5000.0, 0.0019],  # 700
            [0.40, 0.85, 60.0, 5000.0, 0.0035],  # 701 — the poor receiver
            [0.60, 0.95, 10.0, 5000.0, 0.0019],  # 702 — 700's twin (live)
        ],
        dtype=np.float32,
    )
    keys = ["700:2024", "701:2024", "702:2024"]
    return {
        "keys": np.asarray(keys, dtype=object),
        "key_index": {k: i for i, k in enumerate(keys)},
        "vecs": vecs,
        "mean": np.zeros(5, dtype=np.float32),
        "std": np.ones(5, dtype=np.float32),
        "features": list(_RECV_FEATURES),
    }


def _hand_pool(with_receiving: bool = True, all_got_away: bool = False) -> HandPool:
    """8 rows, all in the 0-0 count bucket: the first 4 caught by 700 (outcome
    'ball'), the last 4 by 701 (outcome 'called_strike') — the drawn OUTCOME
    reveals which catcher's rows the kernel favored."""
    n = 8
    sit = np.zeros((n, 6), dtype=np.float32)
    sit[:, 4] = 5.0
    return HandPool(
        geom=np.zeros((n, 10), dtype=np.float32),
        sit=sit,
        pitcher_id=np.full(n, 100, dtype=np.int64),
        batter_id=np.full(n, 200, dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        outcome_type=np.asarray(["ball"] * 4 + ["called_strike"] * 4, dtype=object),
        recency=np.ones(n, dtype=np.float32),
        catcher_id=(
            np.asarray([700, 700, 700, 700, 701, 701, 701, 701], dtype=np.int64)
            if with_receiving
            else None
        ),
        got_away=(np.ones(n, dtype=np.int8) if all_got_away else np.zeros(n, dtype=np.int8)),
    )


def _sampler(pool: HandPool, emb: dict | None = None) -> FullPoolSampler:
    art = EngineArtifacts(
        pools={"R": pool},
        pitcher_sim={_PITCHER: {_PITCHER: 1.0}},
        pitcher_sim_index={_PITCHER: 0},
        actor_emb=({"catcher": emb} if emb is not None else {}),
        bb_pools={},
    )
    return FullPoolSampler(art, np.random.default_rng(0))


_BASE_OUT = np.array([0, 0, 5, 0], dtype=np.float32)


class TestGotAwayReads:
    # SIM-523 part E deleted the SIM-517 receiving kernel and its tests; the
    # got-away reads below are the part that stays.
    def test_last_pitch_got_away_reads_the_drawn_row(self):
        fp = _sampler(_hand_pool(all_got_away=True), _catcher_emb())
        fp.new_half_inning("R", _PITCHER)
        fp.new_plate_appearance("200:2024", _BASE_OUT)
        assert fp.last_pitch_got_away() is False  # no draw yet
        fp.draw(0, 0)
        assert fp.last_pitch_got_away() is True

    def test_got_away_false_on_a_pre_0022_bundle(self):
        pool = _hand_pool()
        pool.got_away = None
        fp = _sampler(pool, _catcher_emb())
        fp.new_half_inning("R", _PITCHER)
        fp.new_plate_appearance("200:2024", _BASE_OUT)
        fp.draw(0, 0)
        assert fp.last_pitch_got_away() is False


def _state(**kw) -> GameState:
    base = {"pitcher_id": 100, "bat_hand": "R", "season": _SEASON, "batter_id": 900}
    base.update(kw)
    return GameState(**base)


class TestGotAwayAdvance:
    def test_runners_advance_one_base_and_third_scores(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _state()
        state.bases = Bases(first=201, second=202, third=203)
        result = PlayResult(pitch_outcome="ball")
        sm._resolve_got_away_advance(state, result)
        assert state.bases.first is None
        assert state.bases.second == 201
        assert state.bases.third == 202
        assert state.away_score == 1  # 203 scored (top of the 1st)
        assert state.outs == 0
        # No RBI on the run; the scorer's box run is credited.
        assert result.steal_runs_scored == 1
        assert result.baserunner_advances[203] == 0
        assert result.baserunner_advances[202] == 3
        assert result.baserunner_advances[201] == 2

    def test_empty_bases_is_a_no_op(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _state()
        result = PlayResult(pitch_outcome="ball")
        sm._resolve_got_away_advance(state, result)
        assert state.away_score == 0 and state.outs == 0
        assert not result.baserunner_advances

    def test_runner_on_first_only_takes_second(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _state()
        state.bases = Bases(first=201)
        result = PlayResult(pitch_outcome="ball")
        sm._resolve_got_away_advance(state, result)
        assert state.bases.first is None and state.bases.second == 201
        assert state.away_score == 0


class TestDroppedThirdStrikeSignal:
    def test_the_drawn_got_away_fires_the_reach_when_first_is_open(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        sm._last_pitch_got_away = True
        state = _state()
        result = PlayResult(pitch_outcome="swinging_strike")
        assert sm._dropped_third_strike(state, result) is True

    def test_no_reach_with_first_occupied_under_two_outs(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        sm._last_pitch_got_away = True
        state = _state()
        state.bases = Bases(first=201)
        result = PlayResult(pitch_outcome="swinging_strike")
        assert sm._dropped_third_strike(state, result) is False

    def test_reach_allowed_with_first_occupied_at_two_outs(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        sm._last_pitch_got_away = True
        state = _state()
        state.bases = Bases(first=201)
        state.outs = 2
        result = PlayResult(pitch_outcome="swinging_strike")
        assert sm._dropped_third_strike(state, result) is True

    def test_a_called_strike_three_never_reaches(self):
        # The predicate keeps the swinging-strike gate (the sim's §5.4 scope).
        sm = StateMachine(rng=np.random.default_rng(0))
        sm._last_pitch_got_away = True
        state = _state()
        result = PlayResult(pitch_outcome="called_strike")
        assert sm._dropped_third_strike(state, result) is False

    def test_flag_off_keeps_the_conservative_default(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        assert sm._last_pitch_got_away is False
        state = _state()
        result = PlayResult(pitch_outcome="swinging_strike")
        assert sm._dropped_third_strike(state, result) is False

    def test_blocked_reach_still_advances_the_runners(self):
        """A got-away strike-3 with 1B occupied under two outs: the batter is
        out (no reach) but the runners move — the ball still got away."""
        sm = StateMachine(rng=np.random.default_rng(0))
        sm._last_pitch_got_away = True
        state = _state()
        state.bases = Bases(first=201, third=203)
        result = PlayResult(pitch_outcome="swinging_strike", pa_terminal=True)
        sm._resolve_strikeout(state, result)
        assert state.outs == 1  # the K stands
        assert state.bases.second == 201  # the runner moved up
        assert state.away_score == 1  # 203 scored on the loose ball
        assert result.steal_runs_scored == 1  # no RBI
