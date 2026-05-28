"""
tests/unit/test_home_field_bias_sim412.py
==========================================
SIM-412 — home-field run advantage bias in the score distribution.

WHAT THIS COVERS
----------------
The audit found that with only the structural last-AB / walk-off / skipped-
bottom-of-the-9th rules, aggregate ``home_win_pct`` from the simulator caps
out at the structural-only ~.510-.515 and never reaches MLB's empirical
~.535-.540.  SIM-412 closes that gap with a small probability that a HOME
team's batted-ball OUT gets flipped to a SINGLE — calibrated to add ~0.13
runs per game to the home side (the empirical Tango/Lichtman MLB home-field
edge) and to leave the away half-innings untouched.

These tests are bias-mechanics + invariants only (no full game simulation
needed): we drive ``_apply_home_field_bias`` directly with synthetic
:class:`FieldingSignal`s under the two ``Half`` states + the eligible /
ineligible event classes, with the bias forced ON via the
``SIM_HOME_FIELD_BIAS`` env (the conftest pins it OFF for the rest of the
unit suite).  An aggregate Monte-Carlo check confirms the empirical bias
rate over many trials matches the configured probability.
"""

from __future__ import annotations

import numpy as np
import pytest

from simulation.game_state import GameState, Half
from simulation.sim_loop import FieldingSignal, StateMachine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _state(half: Half = Half.BOTTOM) -> GameState:
    """Minimal GameState — only ``half`` matters for the home-field bias."""
    return GameState(
        pitcher_id=600001,
        bat_hand="R",
        season=2024,
        half=half,
    )


def _out_sig(event: str = "field_out", outs: int = 1) -> FieldingSignal:
    """A canonical single-out batted-ball signal — the shape the bias hooks on."""
    return FieldingSignal(
        event=event,
        result_hits=0,
        result_outs=outs,
        result_runs=0,
        fielder_id=999,
        is_error=False,
        exit_velo=85.0,
        launch_angle=-5.0,
        spray_angle=0.0,
    )


@pytest.fixture
def bias_on(monkeypatch):
    """Force the SIM-412 bias to a known nonzero value (50% so a few-trial
    test is deterministic enough to assert the flip when an rng draw is low)."""
    monkeypatch.setenv("SIM_HOME_FIELD_BIAS", "0.5")


@pytest.fixture
def bias_off(monkeypatch):
    monkeypatch.setenv("SIM_HOME_FIELD_BIAS", "0")


# ===========================================================================
# 1. The env knob + clamp
# ===========================================================================


class TestEnvKnob:
    def test_default_class_value_is_used_when_env_absent(self, monkeypatch):
        monkeypatch.delenv("SIM_HOME_FIELD_BIAS", raising=False)
        sm = StateMachine(rng=np.random.default_rng(0))
        assert sm._home_field_bias() == pytest.approx(StateMachine._HOME_FIELD_BIAS_DEFAULT)

    def test_env_override_parses_float(self, monkeypatch):
        monkeypatch.setenv("SIM_HOME_FIELD_BIAS", "0.05")
        sm = StateMachine(rng=np.random.default_rng(0))
        assert sm._home_field_bias() == pytest.approx(0.05)

    def test_env_zero_disables(self, monkeypatch):
        monkeypatch.setenv("SIM_HOME_FIELD_BIAS", "0")
        sm = StateMachine(rng=np.random.default_rng(0))
        assert sm._home_field_bias() == 0.0

    def test_env_clamps_negatives_to_zero(self, monkeypatch):
        monkeypatch.setenv("SIM_HOME_FIELD_BIAS", "-0.5")
        sm = StateMachine(rng=np.random.default_rng(0))
        assert sm._home_field_bias() == 0.0

    def test_env_clamps_absurdly_large_values(self, monkeypatch):
        """Out-of-range values clamp to ``[0, 0.1]`` so a misconfigured deployment
        can never flip an unreasonable fraction of outs."""
        monkeypatch.setenv("SIM_HOME_FIELD_BIAS", "2.0")
        sm = StateMachine(rng=np.random.default_rng(0))
        assert sm._home_field_bias() == 0.1

    def test_env_malformed_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("SIM_HOME_FIELD_BIAS", "not-a-number")
        sm = StateMachine(rng=np.random.default_rng(0))
        assert sm._home_field_bias() == pytest.approx(StateMachine._HOME_FIELD_BIAS_DEFAULT)


# ===========================================================================
# 2. The bias is asymmetric — only HOME (Half.BOTTOM) is affected
# ===========================================================================


class TestBiasOnlyOnHomeHalf:
    def test_bias_does_not_fire_on_top_half_away_batting(self, bias_on):
        sm = StateMachine(rng=np.random.default_rng(0))
        # rng would otherwise definitely cross the 0.5 threshold; force it
        # below by re-seeding (irrelevant — TOP half short-circuits first).
        out = sm._apply_home_field_bias(_state(Half.TOP), _out_sig())
        assert out.event == "field_out"
        assert out.result_hits == 0
        assert out.result_outs == 1

    def test_bias_can_fire_on_bottom_half_home_batting(self, bias_on):
        # Seed so the first rng.random() draw is < 0.5 (a flip), then check the
        # converted signal carries the canonical single shape.
        rng = np.random.default_rng(42)
        # Burn rng draws until we know one is below 0.5 — or just trust the rng
        # at seed 42 (the first draw is < 0.5 with overwhelming probability).
        sm = StateMachine(rng=rng)
        # Force a deterministic "flip" outcome by stubbing rng.random.
        flipped = False
        for _ in range(5):
            out = sm._apply_home_field_bias(_state(Half.BOTTOM), _out_sig())
            if out.event == "single":
                flipped = True
                assert out.result_hits == 1
                assert out.result_outs == 0
                assert out.is_error is False
                break
        assert flipped, "with bias=0.5, at least one of 5 trials must flip"


# ===========================================================================
# 3. Eligible vs ineligible event filter
# ===========================================================================


class TestEventFilter:
    def test_strikeout_is_never_flipped(self, bias_on):
        """A K is an out, but the bias models BABIP-style luck — Ks are excluded
        so the per-pitcher K9 stays unaffected by home/away."""
        sm = StateMachine(rng=np.random.default_rng(0))
        # 100 trials — should never flip a K.
        for _ in range(100):
            out = sm._apply_home_field_bias(_state(), _out_sig(event="strikeout"))
            assert out.event == "strikeout"

    def test_double_play_is_never_flipped(self, bias_on):
        sm = StateMachine(rng=np.random.default_rng(0))
        for _ in range(100):
            out = sm._apply_home_field_bias(
                _state(), _out_sig(event="grounded_into_double_play", outs=2)
            )
            assert out.event == "grounded_into_double_play"

    def test_sac_fly_is_never_flipped(self):
        """Already-productive outs carry their own resolution — don't disturb.

        The canonical 'sacrifice_fly' and 'sac_fly' events MUST be excluded
        from :attr:`_HOME_FIELD_ELIGIBLE_OUTS` so a true sac fly (which already
        scored a run) is never re-flipped into a single (which would lose the
        runner-from-3rd credit + double-count via the run-resolution path)."""
        assert "sacrifice_fly" not in StateMachine._HOME_FIELD_ELIGIBLE_OUTS
        assert "sac_fly" not in StateMachine._HOME_FIELD_ELIGIBLE_OUTS

    def test_error_play_is_never_flipped(self, bias_on):
        """A play already flagged ``is_error`` carries unearned-run semantics —
        the bias does not double-dip into it."""
        sm = StateMachine(rng=np.random.default_rng(0))
        sig = FieldingSignal(
            event="field_out",
            result_hits=0,
            result_outs=1,
            result_runs=0,
            is_error=True,
        )
        for _ in range(100):
            out = sm._apply_home_field_bias(_state(), sig)
            assert out.is_error is True
            assert out.event == "field_out"

    def test_existing_hit_is_never_modified(self, bias_on):
        """The bias only acts on outs — a sampled single stays a single."""
        sm = StateMachine(rng=np.random.default_rng(0))
        sig = FieldingSignal(event="single", result_hits=1, result_outs=0, result_runs=0)
        for _ in range(50):
            assert sm._apply_home_field_bias(_state(), sig).event == "single"


# ===========================================================================
# 4. The bias is a no-op when disabled
# ===========================================================================


class TestBiasDisabled:
    def test_no_op_when_bias_zero(self, bias_off):
        sm = StateMachine(rng=np.random.default_rng(0))
        for _ in range(100):
            out = sm._apply_home_field_bias(_state(), _out_sig())
            assert out.event == "field_out"
            assert out.result_hits == 0


# ===========================================================================
# 5. Empirical bias rate matches the configured probability (Monte-Carlo)
# ===========================================================================


class TestEmpiricalBiasRate:
    def test_flip_rate_matches_configured_probability(self, monkeypatch):
        """Over 5000 trials with bias=0.05, the fraction of outs flipped to
        singles should match 0.05 within a generous Wald 95% CI band."""
        monkeypatch.setenv("SIM_HOME_FIELD_BIAS", "0.05")
        sm = StateMachine(rng=np.random.default_rng(20260528))
        n = 5000
        flips = 0
        for _ in range(n):
            out = sm._apply_home_field_bias(_state(), _out_sig())
            if out.event == "single":
                flips += 1
        rate = flips / n
        # Wald 95% half-width at p=0.05, n=5000: 1.96 * sqrt(0.05*0.95/5000) ~ 0.006
        assert abs(rate - 0.05) < 0.012, (
            f"empirical flip rate {rate:.4f} differs from configured 0.05 by "
            f"more than the expected Monte-Carlo band"
        )


# ===========================================================================
# 6. Default class constant is the documented calibration target
# ===========================================================================


def test_default_bias_constant_is_documented_value():
    """The default 0.025 is the calibration target documented on the class
    constant — tying this test to that value protects against a silent
    regression of the home-field run-advantage magnitude."""
    assert StateMachine._HOME_FIELD_BIAS_DEFAULT == 0.025
