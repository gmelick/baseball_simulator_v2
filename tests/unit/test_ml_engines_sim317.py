"""
test_ml_engines_sim317.py
=========================
Unit tests for SIM-317 -- real query-fingerprint derivation from game state
(``simulation/fingerprints.py``), and its wiring into the SIM-316 loop
(``simulation/sim_loop.py``).

Strategy (no live DB / FAISS)
-----------------------------
SIM-317's expensive matchup geometry comes from an *injected*
:class:`~simulation.fingerprints.MatchupProfileProvider`, and SIM-321 fusion
consumes *injected* per-engine signals -- so every test here runs with a fixed,
hand-built ``MatchupProfile`` and explicit engine signals.  No DuckDB, no faiss,
no engine ``build()``.  This mirrors the "inject the dependency" pattern the
SIM-302/303 sampler tests and the SIM-321 fusion tests use.

Coverage (the SIM-317 acceptance criteria):
  * the pitch vector is 10-dim in ``PITCH_FEATURES`` order; the batted-ball
    vector is 3-dim in ``BATTED_BALL_FEATURES`` order;
  * derivation is deterministic from a fixed GameState;
  * the per-PA matchup cache hits on repeated pitches and resets at the PA
    boundary;
  * the pre-filter keys (pitcher_id, bat_hand, season) are NOT vector dims;
  * the deriver wires into the loop without breaking the count-machine-only
    no-DB path.
"""

from __future__ import annotations

import numpy as np

from simulation.fingerprints import (
    BATTED_BALL_FEATURE_NAMES,
    BATTED_BALL_FINGERPRINT_DIM,
    PITCH_FEATURE_NAMES,
    PITCH_FINGERPRINT_DIM,
    FingerprintDeriver,
    MatchupProfile,
)
from simulation.game_state import GameState

SEASON = 2024
PITCHER = 477132
BATTER = 660271

# A fixed, physically-plausible matchup geometry (raw units: mph / deg / ft).
# arsenal order = [velo, ivb, hb, spin_rate, spin_axis, release_x, release_z,
#                  release_ext]; location = [plate_x, plate_z];
# batted_ball = [exit_velo, launch_angle, spray_angle].
_ARSENAL = np.array([94.1, 18.0, -3.4, 2380.0, 210.0, -1.5, 5.9, 6.5])
_LOCATION = np.array([0.4, 2.7])
_BATTED_BALL = np.array([89.0, 14.0, -5.0])


def _fixed_profile(*_args, **_kwargs) -> MatchupProfile:
    """A provider that ignores its keys and returns a fixed matchup profile."""
    return MatchupProfile(
        arsenal=_ARSENAL.copy(),
        intended_location=_LOCATION.copy(),
        batted_ball=_BATTED_BALL.copy(),
    )


def _counting_provider():
    """A provider that records how many times it was actually invoked (to prove
    the per-PA cache is what serves repeated lookups)."""
    calls = {"n": 0}

    def provider(pitcher_id, bat_hand, season, batter_id):
        calls["n"] += 1
        return _fixed_profile()

    return provider, calls


def _fresh_state(**kw) -> GameState:
    base = {"pitcher_id": PITCHER, "bat_hand": "R", "season": SEASON, "batter_id": BATTER}
    base.update(kw)
    return GameState(**base)


# ===========================================================================
# (1) dims + order
# ===========================================================================


class TestVectorShapeAndOrder:
    def test_pitch_vector_is_ten_dim_in_pitch_features_order(self):
        assert PITCH_FINGERPRINT_DIM == 10
        assert PITCH_FEATURE_NAMES == (
            "velo",
            "ivb",
            "hb",
            "spin_rate",
            "spin_axis",
            "release_x",
            "release_z",
            "release_ext",
            "plate_x",
            "plate_z",
        )
        d = FingerprintDeriver(_fixed_profile)
        vec = d.pitch_fingerprint(_fresh_state())
        assert vec.shape == (10,)
        assert vec.dtype == np.float32

    def test_battedball_vector_is_three_dim_in_bb_features_order(self):
        assert BATTED_BALL_FINGERPRINT_DIM == 3
        assert BATTED_BALL_FEATURE_NAMES == ("exit_velo", "launch_angle", "spray_angle")
        d = FingerprintDeriver(_fixed_profile)
        vec = d.battedball_fingerprint(_fresh_state())
        assert vec.shape == (3,)
        assert vec.dtype == np.float32

    def test_pitch_vector_preserves_arsenal_then_location_order(self):
        # With no fitted normalizer, normalize == raw * sqrt(weight).  The first 8
        # dims must track the arsenal centroid and the last 2 the location, in
        # order -- so the sqrt-weight-scaled raw is recoverable monotonically.
        from similarity.engines.pitch_pitch_similarity import FEATURE_SCALE

        d = FingerprintDeriver(_fixed_profile)
        vec = d.pitch_fingerprint(_fresh_state())
        expected = (np.concatenate([_ARSENAL, _LOCATION]) * FEATURE_SCALE).astype(np.float32)
        np.testing.assert_allclose(vec, expected, rtol=1e-6)


# ===========================================================================
# (2) determinism
# ===========================================================================


class TestDeterminism:
    def test_pitch_vector_is_deterministic_from_a_fixed_state(self):
        d1 = FingerprintDeriver(_fixed_profile)
        d2 = FingerprintDeriver(_fixed_profile)
        v1 = d1.pitch_fingerprint(_fresh_state())
        v2 = d2.pitch_fingerprint(_fresh_state())
        np.testing.assert_array_equal(v1, v2)

    def test_battedball_vector_is_deterministic_from_a_fixed_state(self):
        d = FingerprintDeriver(_fixed_profile)
        v1 = d.battedball_fingerprint(_fresh_state())
        d.new_plate_appearance()
        v2 = d.battedball_fingerprint(_fresh_state())
        np.testing.assert_array_equal(v1, v2)

    def test_count_does_not_change_the_fingerprint(self):
        # Count is NOT a fingerprint dim (spec §4.3): its effect enters at step 4
        # (foul re-weight, SIM-318), not the FAISS distance.
        d = FingerprintDeriver(_fixed_profile)
        v_00 = d.pitch_fingerprint(_fresh_state(balls=0, strikes=0))
        d.new_plate_appearance()
        v_32 = d.pitch_fingerprint(_fresh_state(balls=3, strikes=2))
        np.testing.assert_array_equal(v_00, v_32)


# ===========================================================================
# (3) per-PA matchup cache
# ===========================================================================


class TestPerPACache:
    def test_repeated_pitches_in_one_pa_hit_the_cache(self):
        provider, calls = _counting_provider()
        d = FingerprintDeriver(provider)
        state = _fresh_state()
        # 6 pitches in one PA (pitch + batted-ball both query the matchup).
        for _ in range(3):
            d.pitch_fingerprint(state)
            d.battedball_fingerprint(state)
        # The provider was invoked exactly once; the rest are cache hits.
        assert calls["n"] == 1
        assert d.cache_misses == 1
        assert d.cache_hits == 5

    def test_new_plate_appearance_resets_the_cache(self):
        provider, calls = _counting_provider()
        d = FingerprintDeriver(provider)
        state = _fresh_state()
        d.pitch_fingerprint(state)  # miss
        d.pitch_fingerprint(state)  # hit
        assert calls["n"] == 1
        d.new_plate_appearance()
        d.pitch_fingerprint(state)  # miss again after reset
        assert calls["n"] == 2
        assert d.cache_misses == 2
        assert d.cache_hits == 1

    def test_different_matchup_is_a_distinct_cache_entry(self):
        provider, calls = _counting_provider()
        d = FingerprintDeriver(provider)
        d.pitch_fingerprint(_fresh_state(batter_id=1))  # miss
        d.pitch_fingerprint(_fresh_state(batter_id=2))  # miss (different batter)
        assert calls["n"] == 2


# ===========================================================================
# (4) pre-filter keys are NOT in the vector (spec §4.3)
# ===========================================================================


class TestPrefilterNotInVector:
    def test_pitcher_id_and_season_do_not_change_the_vector(self):
        # The pre-filter keys are tile args, not vector dims; changing them (with
        # the SAME matchup geometry) must not change the derived vector.
        d = FingerprintDeriver(_fixed_profile)
        v1 = d.pitch_fingerprint(_fresh_state(pitcher_id=111, season=2021))
        d.new_plate_appearance()
        v2 = d.pitch_fingerprint(_fresh_state(pitcher_id=999, season=2024))
        np.testing.assert_array_equal(v1, v2)

    def test_vector_has_exactly_the_physics_dims(self):
        # No extra dims for pitcher_id / bat_hand / season / count.
        d = FingerprintDeriver(_fixed_profile)
        assert d.pitch_fingerprint(_fresh_state()).shape[0] == len(PITCH_FEATURE_NAMES)
        assert d.battedball_fingerprint(_fresh_state()).shape[0] == len(BATTED_BALL_FEATURE_NAMES)


# ===========================================================================
# (5) SIM-321 fusion shapes the draw (injected signals, no DB)
# ===========================================================================


class TestFusionShaping:
    def test_pitch_fusion_signal_tilts_the_location_dims_only(self):
        d = FingerprintDeriver(_fixed_profile)
        base = d.pitch_fingerprint(_fresh_state())
        d.new_plate_appearance()
        # Non-neutral injected engine signals tilt the fingerprint.
        tilted = d.pitch_fingerprint(
            _fresh_state(),
            pitch_signals={
                "pitcher": (0.95, "similarity"),
                "batter": (0.90, "similarity"),
                "situation": (0.10, "distance"),
            },
        )
        # The arsenal dims (0..7) are unchanged; only the location dims (8,9) move.
        np.testing.assert_array_equal(base[:8], tilted[:8])
        assert not np.allclose(base[8:], tilted[8:])

    def test_neutral_fusion_signal_leaves_the_vector_unchanged(self):
        d = FingerprintDeriver(_fixed_profile)
        base = d.pitch_fingerprint(_fresh_state())
        d.new_plate_appearance()
        # A perfectly neutral 0.5 fused scalar -> no tilt.
        neutral = d.pitch_fingerprint(
            _fresh_state(),
            pitch_signals={"pitcher": (0.5, "similarity")},
        )
        np.testing.assert_allclose(base, neutral, rtol=1e-6)


# ===========================================================================
# (6) z-score normalization into the engine space (fitted stats)
# ===========================================================================


class TestNormalizationSpace:
    def test_fitted_normalizer_matches_the_engine_normalize(self):
        # When the engine's fitted mean/std are supplied, the deriver must apply
        # the IDENTICAL transform the SIM-041 engine applies at index/query time.
        from similarity.engines.pitch_pitch_similarity import (
            PitchNormalizer,
        )
        from simulation.fingerprints import _PitchNorm

        raw_full = np.concatenate([_ARSENAL, _LOCATION])
        mean = raw_full - 1.0  # arbitrary but fixed fitted stats
        std = np.full(raw_full.shape, 2.0)

        d = FingerprintDeriver(
            _fixed_profile,
            pitch_norm=_PitchNorm(mean=mean, std=std),
        )
        got = d.pitch_fingerprint(_fresh_state())

        # Reference: the engine's own normalizer with the same fitted stats.
        ref_norm = PitchNormalizer(mean=mean, std=std)
        expected = ref_norm.normalize(raw_full)
        np.testing.assert_allclose(got, expected, rtol=1e-6)


# ===========================================================================
# (7) loop wiring — no-DB count-machine path stays intact
# ===========================================================================


class TestLoopWiring:
    def test_state_machine_accepts_a_deriver_without_a_sampler_noop(self):
        # A deriver may be threaded in even in count-machine-only mode; the
        # no-sampler path never touches it, so the SIM-316 behaviour is unchanged.
        from simulation.sim_loop import EVENT_WALK, StateMachine

        d = FingerprintDeriver(_fixed_profile)
        sm = StateMachine(fingerprint_deriver=d)  # NO sampler
        state = _fresh_state()
        for _ in range(3):
            r = sm.step_pitch(state, pitch_outcome="ball")
            assert r.pa_terminal is False
        r = sm.step_pitch(state, pitch_outcome="ball")
        assert r.pa_terminal is True
        assert r.event == EVENT_WALK
        # The deriver was never invoked on the count-machine-only path.
        assert d.cache_misses == 0

    def test_end_of_pa_resets_the_deriver_cache(self):
        # On a terminal PA the loop must reset the per-PA matchup cache so the
        # next batter recomputes.  Drive a strikeout PA and assert the reset.
        from simulation.sim_loop import StateMachine

        provider, calls = _counting_provider()
        d = FingerprintDeriver(provider)
        sm = StateMachine(fingerprint_deriver=d)
        state = _fresh_state()
        # Prime the cache mid-PA (a manual matchup lookup), then end the PA.
        d.pitch_fingerprint(state)
        assert calls["n"] == 1
        for _ in range(3):
            sm.step_pitch(state, pitch_outcome="called_strike")  # strikeout -> PA ends
        # The end-of-PA hook cleared the cache; a fresh lookup misses again.
        d.pitch_fingerprint(state)
        assert calls["n"] == 2
