"""
tests/unit/test_realism_consumers_sim411_413_425b.py
====================================================
SIM-411 / SIM-413 / SIM-425b — the realism CONSUMERS (flag-on mechanism tests).

The artifact-column plumbing is covered by
``test_engine_artifacts_realism_sim411_413_425b``; this module pins the consumer
behaviour that reads those columns, each behind its own env gate:

  * **SIM-413** — ``FullPoolSampler.battedball_new_pa(pitcher_throws=...)`` softly
    reweights the batted-ball draw toward the live pitcher-hand matchup.
  * **SIM-425b** — the ``last_battedball_fielder`` / ``fielder_quality`` accessors
    and ``StateMachine._fielder_rbf_nudge`` flip a fieldable single<->out by the
    live defender's OAA vs the pool play's fielder.
  * **SIM-411** — ``StateMachine._apply_park_factor`` flips outs<->singles by the
    venue run factor (relative to the league-average park the pool reflects).

All three are graceful-optional: with the gate off / the data absent they are a
no-op (covered here + by the unchanged existing suites).
"""

from __future__ import annotations

import numpy as np

from pipeline.batch.engine_artifacts import BattedBallPool, EngineArtifacts
from simulation.full_pool_sampler import FullPoolSampler
from simulation.game_state import GameState, Half
from simulation.sim_loop import FieldingSignal, StateMachine

_SEASON = 2024


class _FixedRNG:
    """A stand-in rng whose ``random()`` always returns ``v`` — lets a test force a
    flip (v small) or suppress it (v large) deterministically."""

    def __init__(self, v: float) -> None:
        self.v = float(v)

    def random(self) -> float:
        return self.v


# ===========================================================================
# SIM-413 — platoon reweight in the batted-ball draw (sampler level)
# ===========================================================================


def _platoon_bb_pool(with_throws: bool = True) -> BattedBallPool:
    """8 rows: the first 4 pitched by a RHP (event 'single'), the last 4 by a LHP
    (event 'double'), so the drawn EVENT reveals which hand the row came from."""
    n = 8
    p_throws = (
        np.asarray(["R", "R", "R", "R", "L", "L", "L", "L"], dtype=object) if with_throws else None
    )
    return BattedBallPool(
        geom=np.zeros((n, 3), dtype=np.float32),
        sit=np.zeros((n, 6), dtype=np.float32),
        batter_id=np.full(n, 700, dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        event=np.asarray(["single"] * 4 + ["double"] * 4, dtype=object),
        result_hits=np.array([1, 1, 1, 1, 2, 2, 2, 2], dtype=np.int8),
        result_outs=np.zeros(n, dtype=np.int8),
        recency=np.ones(n, dtype=np.float32),
        p_throws=p_throws,
    )


def _sampler(bb_pool: BattedBallPool, **kw) -> FullPoolSampler:
    art = EngineArtifacts(pools={}, bb_pools={"R": bb_pool})
    return FullPoolSampler(art, np.random.default_rng(0), **kw)


class TestPlatoonReweight:
    def test_off_weight_zero_draws_only_same_hand(self):
        # platoon_off_weight=0 => opposite-hand (LHP) rows get zero weight, so a
        # RHP matchup draws ONLY the 'single' (RHP) rows.
        fp = _sampler(_platoon_bb_pool(), platoon_off_weight=0.0)
        events = set()
        for _ in range(40):
            fp.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32), pitcher_throws="R")
            events.add(fp.battedball_draw()[0])
        assert events == {"single"}

    def test_neutral_when_no_pitcher_hand(self):
        # pitcher_throws=None => no reweight => both event types appear.
        fp = _sampler(_platoon_bb_pool(), platoon_off_weight=0.0)
        events = set()
        for _ in range(60):
            fp.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32), pitcher_throws=None)
            events.add(fp.battedball_draw()[0])
        assert events == {"single", "double"}

    def test_neutral_when_pool_has_no_p_throws(self):
        # A legacy pool (p_throws None) ignores the reweight even with a hand given.
        fp = _sampler(_platoon_bb_pool(with_throws=False), platoon_off_weight=0.0)
        events = set()
        for _ in range(60):
            fp.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32), pitcher_throws="R")
            events.add(fp.battedball_draw()[0])
        assert events == {"single", "double"}


# ===========================================================================
# SIM-425b — fielder accessors + the OAA nudge
# ===========================================================================


def _fielder_bb_pool() -> BattedBallPool:
    return BattedBallPool(
        geom=np.zeros((1, 3), dtype=np.float32),
        sit=np.zeros((1, 6), dtype=np.float32),
        batter_id=np.array([700], dtype=np.int64),
        season=np.array([_SEASON], dtype=np.int64),
        event=np.asarray(["single"], dtype=object),
        result_hits=np.array([1], dtype=np.int8),
        result_outs=np.array([0], dtype=np.int8),
        recency=np.ones(1, dtype=np.float32),
        fielder_pos=np.array([6], dtype=np.int8),  # SS
        fielder_id=np.array([555], dtype=np.int64),
    )


def _fielder_artifacts() -> EngineArtifacts:
    femb = {
        "key_index": {"555:SS:2024": 0, "666:SS:2024": 1},
        "vecs": np.array([[-5.0], [10.0]], dtype=np.float32),
        "mean": np.zeros(1, dtype=np.float32),
        "std": np.ones(1, dtype=np.float32),
        "features": ["outs_above_average"],
    }
    return EngineArtifacts(
        pools={}, bb_pools={"R": _fielder_bb_pool()}, actor_emb={"fielder": femb}
    )


class TestFielderAccessors:
    def test_last_battedball_fielder(self):
        fp = FullPoolSampler(_fielder_artifacts(), np.random.default_rng(0))
        fp.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32))
        fp.battedball_draw()
        assert fp.last_battedball_fielder() == (6, 555)

    def test_fielder_quality_reads_oaa_by_position_key(self):
        fp = FullPoolSampler(_fielder_artifacts(), np.random.default_rng(0))
        assert fp.fielder_quality(555, "SS", _SEASON) == -5.0
        assert fp.fielder_quality(666, "SS", _SEASON) == 10.0
        assert fp.fielder_quality(999, "SS", _SEASON) is None  # unknown
        assert fp.fielder_quality(555, "2B", _SEASON) is None  # wrong position


class _FakeFielderFP:
    def __init__(self, drawn, quality):
        self._drawn = drawn
        self._quality = quality

    def last_battedball_fielder(self):
        return self._drawn

    def fielder_quality(self, fid, pos_str, season):
        return self._quality.get(int(fid))


def _sm(flag_attr: str, rng_v: float = 0.0) -> StateMachine:
    sm = StateMachine()
    setattr(sm, flag_attr, True)
    sm.rng = _FixedRNG(rng_v)  # type: ignore[assignment]
    return sm


class TestFielderRbfNudge:
    def _state(self, defense):
        # half=TOP => away batting => the HOME team is in the field.
        return GameState(
            pitcher_id=1, bat_hand="R", season=_SEASON, half=Half.TOP, home_defense=defense
        )

    def test_better_live_fielder_turns_single_into_out(self):
        sm = _sm("_fielder_rbf", rng_v=0.0)
        fp = _FakeFielderFP(drawn=(6, 555), quality={555: -5.0, 666: 10.0})
        state = self._state({"SS": 666})  # drawn pos 6 -> 'SS'
        ev, rh, is_err, fid = sm._fielder_rbf_nudge(state, fp, "single", 1, _SEASON)
        assert (ev, rh, is_err, fid) == ("field_out", 0, False, 666)

    def test_worse_live_fielder_turns_out_into_single(self):
        sm = _sm("_fielder_rbf", rng_v=0.0)
        fp = _FakeFielderFP(drawn=(6, 666), quality={555: -5.0, 666: 10.0})
        state = self._state({"SS": 555})  # drawn pos 6 -> 'SS'
        ev, rh, is_err, fid = sm._fielder_rbf_nudge(state, fp, "field_out", 0, _SEASON)
        assert (ev, rh, is_err, fid) == ("single", 1, False, 555)

    def test_no_flip_when_rng_above_probability(self):
        sm = _sm("_fielder_rbf", rng_v=0.99)
        fp = _FakeFielderFP(drawn=(6, 555), quality={555: -5.0, 666: 10.0})
        state = self._state({"SS": 666})  # drawn pos 6 -> 'SS'
        ev, rh, _e, fid = sm._fielder_rbf_nudge(state, fp, "single", 1, _SEASON)
        assert (ev, rh, fid) == ("single", 1, 666)  # attributed, not flipped

    def test_neutral_without_defense_map(self):
        sm = _sm("_fielder_rbf", rng_v=0.0)
        fp = _FakeFielderFP(drawn=(6, 555), quality={555: -5.0, 666: 10.0})
        state = self._state({})  # no defender at the position
        assert sm._fielder_rbf_nudge(state, fp, "single", 1, _SEASON) == ("single", 1, False, None)

    def test_neutral_when_quality_unknown(self):
        sm = _sm("_fielder_rbf", rng_v=0.0)
        fp = _FakeFielderFP(drawn=(6, 555), quality={})  # no OAA for anyone
        state = self._state({"SS": 666})  # drawn pos 6 -> 'SS'
        ev, rh, _e, fid = sm._fielder_rbf_nudge(state, fp, "single", 1, _SEASON)
        assert (ev, rh, fid) == ("single", 1, 666)


# ===========================================================================
# SIM-411 — park run-factor nudge
# ===========================================================================


def _out_sig() -> FieldingSignal:
    return FieldingSignal(event="field_out", result_hits=0, result_outs=1, result_runs=0)


def _single_sig() -> FieldingSignal:
    return FieldingSignal(event="single", result_hits=1, result_outs=0, result_runs=0)


class TestParkFactor:
    def _state(self, factor: float) -> GameState:
        return GameState(pitcher_id=1, bat_hand="R", season=_SEASON, park_run_factor=factor)

    def test_hitters_park_flips_out_to_single(self):
        sm = _sm("_park_factor", rng_v=0.0)
        out = sm._apply_park_factor(self._state(1.2), _out_sig())
        assert (out.event, out.result_hits, out.result_outs) == ("single", 1, 0)

    def test_pitchers_park_flips_single_to_out(self):
        sm = _sm("_park_factor", rng_v=0.0)
        out = sm._apply_park_factor(self._state(0.8), _single_sig())
        assert (out.event, out.result_hits, out.result_outs) == ("field_out", 0, 1)

    def test_neutral_factor_is_noop(self):
        sm = _sm("_park_factor", rng_v=0.0)
        sig = _out_sig()
        assert sm._apply_park_factor(self._state(1.0), sig) is sig

    def test_flag_off_is_noop(self):
        sm = StateMachine()  # _park_factor defaults False
        sm.rng = _FixedRNG(0.0)  # type: ignore[assignment]
        sig = _out_sig()
        assert sm._apply_park_factor(self._state(1.2), sig) is sig

    def test_no_flip_when_rng_above_probability(self):
        sm = _sm("_park_factor", rng_v=0.99)
        sig = _out_sig()
        assert sm._apply_park_factor(self._state(1.2), sig) is sig


if __name__ == "__main__":  # pragma: no cover
    import pytest

    pytest.main([__file__, "-v"])
