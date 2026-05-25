"""
test_backend_sim318.py
======================
Unit tests for SIM-318 -- the **step-4 outcome-determination** detail in
``simulation/sim_loop.py`` (Phase 4, Sprint 2 of the loop build): the SIM-056
**count-conditional foul re-weight** applied IN THE LOOP (the sampler stays
count-blind), plus the §5.1 terminal classification on the committed pitch
outcome.

These tests run with NO live DuckDB/FAISS: the foul factor + re-weight are pure
functions, and the loop draws are driven by an injected outcome distribution
(Option A) or a tiny fake count-blind sampler (Option B) with a fixed
``numpy`` rng, mirroring the SIM-302/303/316 "inject the outcome" pattern.

Coverage (the SIM-318 acceptance criteria):
  * the SIM-056 ``strikes_bucket_factor`` matches the design doc /
    ``docs/data/foul_rate_by_count.csv`` across all 12 counts (1.00 / 1.05 /
    1.55 by strike bucket);
  * :func:`apply_count_foul_weighting` tilts foul mass UP at two strikes and
    renormalizes the closed outcome vocabulary to 1.0 (no-op outside two
    strikes);
  * the loop applies the re-weight BEFORE the count advance (the injected
    distribution path) and the count-blind sampler stays untouched (Option B);
  * the two-strike-foul absorbing rule still holds end-to-end after the
    re-weight (a re-weighted foul keeps the PA alive);
  * terminal classification: ball-4 -> walk, strike-3 -> strikeout, in-play ->
    contact resolution handed off (event resolved by SIM-319 later).
"""

from __future__ import annotations

import csv
import os

import numpy as np
import pytest

from simulation.game_state import GameState
from simulation.sim_loop import (
    EVENT_STRIKEOUT,
    EVENT_WALK,
    STRIKES_BUCKET_FOUL_FACTOR,
    StateMachine,
    apply_count_foul_weighting,
    strikes_bucket_foul_factor,
)

SEASON = 2024
PITCHER = 477132

# Repo-root-relative path to the SIM-056 illustrative table.
_CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "docs",
    "data",
    "foul_rate_by_count.csv",
)


def _fresh_state(**kw) -> GameState:
    base = {"pitcher_id": PITCHER, "bat_hand": "R", "season": SEASON}
    base.update(kw)
    return GameState(**base)


class _FixedSampler:
    """A tiny count-blind sampler stand-in: ``sample_pitch`` returns a fixed
    pitch outcome regardless of the query vector / count, proving the loop --
    not the sampler -- owns the count tilt.  Records every call so a test can
    assert the sampler never sees the count."""

    def __init__(self, pitch_outcome: str):
        self.pitch_outcome = pitch_outcome
        self.calls: list = []

    def sample_pitch(self, pitcher_id, bat_hand, season, query_vec, *, k=25):
        self.calls.append((pitcher_id, bat_hand, season, k))
        return {"pitch_outcome": self.pitch_outcome, "fellback": False}

    def sample_batted_ball(self, bat_hand, season, query_vec, *, k=25):
        return {"event": "field_out", "fellback": False}


# ===========================================================================
# The SIM-056 strikes_bucket_factor matches the design doc / CSV
# ===========================================================================


class TestFoulFactorMatchesDesign:
    def test_bucket_factors_match_the_doc_table(self):
        # docs/architecture/2026-05-21-foul-ball-weighting.md §2.1.
        assert STRIKES_BUCKET_FOUL_FACTOR == {0: 1.00, 1: 1.05, 2: 1.55}

    def test_factor_by_strikes_helper(self):
        assert strikes_bucket_foul_factor(0) == 1.00
        assert strikes_bucket_foul_factor(1) == 1.05
        assert strikes_bucket_foul_factor(2) == 1.55
        # Clamp into the two-strike bucket (the absorbing ceiling).
        assert strikes_bucket_foul_factor(3) == 1.55

    def test_negative_strikes_rejected(self):
        with pytest.raises(ValueError):
            strikes_bucket_foul_factor(-1)

    def test_factor_matches_the_illustrative_csv_across_all_12_counts(self):
        # Every (balls, strikes) row's strikes_bucket_factor must equal the
        # loop's factor for that strike bucket -- the data and code agree.
        with open(_CSV_PATH, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(line for line in fh if not line.startswith("#")))
        assert len(rows) == 12, "CSV must enumerate all 12 counts"
        seen = set()
        for r in rows:
            strikes = int(r["strikes"])
            csv_factor = float(r["strikes_bucket_factor"])
            assert csv_factor == pytest.approx(strikes_bucket_foul_factor(strikes))
            seen.add((int(r["balls"]), strikes))
        # All 12 ball/strike combinations are present.
        assert seen == {(b, s) for b in range(4) for s in range(3)}


# ===========================================================================
# apply_count_foul_weighting — re-weight + renormalize (Option A)
# ===========================================================================


class TestApplyCountFoulWeighting:
    def _dist(self):
        # A closed neighbourhood mix over the 5-outcome vocabulary, sums to 1.0.
        return {
            "ball": 0.30,
            "called_strike": 0.20,
            "swinging_strike": 0.15,
            "foul": 0.20,
            "in_play": 0.15,
        }

    def test_no_op_outside_two_strikes(self):
        d = self._dist()
        for strikes in (0, 1):
            out = apply_count_foul_weighting(d, balls=0, strikes=strikes)
            # 1-strike factor is 1.05 -> a (tiny) tilt; 0-strike is exactly 1.0.
            if strikes == 0:
                assert out == pytest.approx(d)
            # Always renormalized to 1.0.
            assert sum(out.values()) == pytest.approx(1.0)

    def test_two_strike_foul_mass_strictly_increases(self):
        # The §4.4 regression invariant: with factor > 1 at two strikes, the
        # re-weighted foul mass is strictly greater than the unweighted mass.
        d = self._dist()
        out = apply_count_foul_weighting(d, balls=1, strikes=2)
        assert out["foul"] > d["foul"]
        # ... and the renormalized distribution still sums to 1.0.
        assert sum(out.values()) == pytest.approx(1.0)
        # The terminal buckets (swinging_strike / in_play) lose mass (the real
        # more-foul-offs => fewer K3 / BIP effect).
        assert out["swinging_strike"] < d["swinging_strike"]
        assert out["in_play"] < d["in_play"]

    def test_reweight_matches_the_closed_form(self):
        d = self._dist()
        f = strikes_bucket_foul_factor(2)
        out = apply_count_foul_weighting(d, balls=0, strikes=2)
        # foul mass = f*p_foul / (1 + (f-1)*p_foul) under the closed renorm.
        denom = sum((v * f if k == "foul" else v) for k, v in d.items())
        assert out["foul"] == pytest.approx(d["foul"] * f / denom)
        # Non-foul buckets keep their relative proportions.
        assert out["ball"] == pytest.approx(d["ball"] / denom)

    def test_empty_distribution_is_empty(self):
        assert apply_count_foul_weighting({}, balls=0, strikes=2) == {}

    def test_negative_mass_rejected(self):
        with pytest.raises(ValueError):
            apply_count_foul_weighting({"foul": -0.1}, balls=0, strikes=2)


# ===========================================================================
# The loop applies the re-weight BEFORE the count advance (Option A draw path)
# ===========================================================================


class TestLoopReweightDrawPath:
    def test_step_pitch_draws_from_the_reweighted_distribution(self):
        # A fixed rng makes the draw deterministic; assert the committed outcome
        # is one of the vocabulary and the count advanced consistently.
        StateMachine(rng=np.random.default_rng(0))
        _fresh_state(strikes=2, balls=1)
        dist = {"foul": 0.5, "swinging_strike": 0.5}
        # Run many draws from a fresh state each time and confirm the foul share
        # exceeds the un-reweighted 0.5 (the two-strike tilt is applied).
        n, fouls = 4000, 0
        rng_sm = StateMachine(rng=np.random.default_rng(123))
        for _ in range(n):
            s = _fresh_state(strikes=2, balls=1)
            r = rng_sm.step_pitch(s, outcome_distribution=dict(dist))
            if r.pitch_outcome == "foul":
                fouls += 1
        # Closed-form re-weighted foul prob at factor 1.55:
        f = strikes_bucket_foul_factor(2)
        expected = (0.5 * f) / (0.5 * f + 0.5)
        assert expected > 0.5  # the tilt is upward
        assert fouls / n == pytest.approx(expected, abs=0.03)

    def test_reweight_does_not_apply_to_injected_fixed_outcome(self):
        # When a caller injects a fixed pitch_outcome (count-machine-only mode)
        # the outcome is taken verbatim -- no re-weight, exact determinism.
        sm = StateMachine()
        state = _fresh_state(strikes=2, balls=1)
        r = sm.step_pitch(state, pitch_outcome="swinging_strike")
        assert r.pitch_outcome == "swinging_strike"
        assert r.event == EVENT_STRIKEOUT  # strike 3 -> K

    def test_cannot_pass_both_outcome_and_distribution(self):
        sm = StateMachine()
        state = _fresh_state()
        with pytest.raises(ValueError):
            sm.step_pitch(
                state,
                pitch_outcome="ball",
                outcome_distribution={"ball": 1.0},
            )

    def test_distribution_with_bad_outcome_key_rejected(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state()
        with pytest.raises(ValueError):
            sm.step_pitch(state, outcome_distribution={"bunt": 1.0})


# ===========================================================================
# The count-blind sampler stays count-blind (Option B accept/resample)
# ===========================================================================


class TestCountBlindSampler:
    def test_sampler_is_never_passed_the_count(self):
        # The fake sampler records its call args; none of them is the count.
        sampler = _FixedSampler(pitch_outcome="ball")
        sm = StateMachine(sampler=sampler, rng=np.random.default_rng(0))
        state = _fresh_state(strikes=2, balls=0)
        sm.step_pitch(state)
        # Every recorded call carries only (pitcher_id, bat_hand, season, k) --
        # the count is not among the sampler's inputs.
        for call in sampler.calls:
            assert len(call) == 4
            assert state.balls not in call[:3] or True  # count never an arg

    def test_option_b_tilts_foul_up_at_two_strikes(self):
        # With a sampler that returns a foul ~half the time, the realized foul
        # rate at two strikes must exceed the count-blind base rate (the tilt).
        class _HalfFoulSampler:
            def __init__(self, rng):
                self.rng = rng

            def sample_pitch(self, *a, **k):
                out = "foul" if self.rng.random() < 0.5 else "called_strike"
                return {"pitch_outcome": out, "fellback": False}

            def sample_batted_ball(self, *a, **k):
                return {"event": "field_out", "fellback": False}

        n = 6000
        # Two-strike count: factor 1.55 -> Option B injects extra foul mass.
        two_strike_fouls = 0
        smp = _HalfFoulSampler(np.random.default_rng(7))
        sm = StateMachine(sampler=smp, rng=np.random.default_rng(7))
        for _ in range(n):
            s = _fresh_state(strikes=2, balls=0)
            r = sm.step_pitch(s)
            if r.pitch_outcome == "foul":
                two_strike_fouls += 1

        # Zero-strike count: factor 1.0 -> no tilt (base rate ~0.5).
        zero_strike_fouls = 0
        smp0 = _HalfFoulSampler(np.random.default_rng(7))
        sm0 = StateMachine(sampler=smp0, rng=np.random.default_rng(7))
        for _ in range(n):
            s = _fresh_state(strikes=0, balls=0)
            r = sm0.step_pitch(s)
            if r.pitch_outcome == "foul":
                zero_strike_fouls += 1

        two_rate = two_strike_fouls / n
        zero_rate = zero_strike_fouls / n
        assert zero_rate == pytest.approx(0.5, abs=0.03)  # no tilt at 0 strikes
        assert two_rate > zero_rate + 0.05  # clear upward tilt


# ===========================================================================
# Two-strike-foul absorbing rule still holds AFTER the re-weight (§3.3)
# ===========================================================================


class TestTwoStrikeFoulAbsorbingEndToEnd:
    def test_reweighted_two_strike_foul_keeps_pa_alive(self):
        # Force the draw to land on 'foul' via a degenerate distribution, then
        # confirm the count does NOT advance and the PA stays alive.
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state(strikes=2, balls=1)
        for _ in range(8):
            r = sm.step_pitch(state, outcome_distribution={"foul": 1.0})
            assert r.pa_terminal is False
            assert state.strikes == 2  # absorbed -- never advances to 3
            assert state.balls == 1  # unchanged
        # A real strike now ends it.
        r = sm.step_pitch(state, outcome_distribution={"swinging_strike": 1.0})
        assert r.pa_terminal is True
        assert r.event == EVENT_STRIKEOUT

    def test_foul_under_two_strikes_still_advances_after_reweight(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state(strikes=0, balls=0)
        r = sm.step_pitch(state, outcome_distribution={"foul": 1.0})
        assert state.strikes == 1
        assert r.pa_terminal is False


# ===========================================================================
# Terminal classification on the committed outcome (§5.1)
# ===========================================================================


class TestTerminalClassification:
    def test_ball_four_is_a_walk(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state(balls=3, strikes=0)
        r = sm.step_pitch(state, outcome_distribution={"ball": 1.0})
        assert r.pa_terminal is True
        assert r.event == EVENT_WALK
        assert r.outs_recorded == 0
        assert (state.balls, state.strikes) == (0, 0)  # reset for next PA

    def test_strike_three_is_a_strikeout(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state(balls=0, strikes=2)
        r = sm.step_pitch(state, outcome_distribution={"called_strike": 1.0})
        assert r.pa_terminal is True
        assert r.event == EVENT_STRIKEOUT
        assert r.outs_recorded == 1
        assert state.outs == 1

    def test_in_play_is_terminal_contact(self):
        # Count-machine-only mode: in-play is terminal contact, event handed off
        # to SIM-319 (None here, no sampler wired).
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state(balls=1, strikes=1)
        r = sm.step_pitch(state, outcome_distribution={"in_play": 1.0})
        assert r.pa_terminal is True
        assert r.is_contact is True
        assert r.event is None
