"""
test_matchup_provider_sim421.py
===============================
SIM-421 — the tile-space normalization artifacts + the production
``MatchupProfileProvider``.

These are the disk artifacts ``play_pool_cache`` writes (the fitted normalizer
mean/std + the per-matchup RAW centroids) and the reader/provider the
``production_factory`` deriver-builder consumes so the loop's query lands in the
SAME normalized space as the FAISS tiles.  Pure file-I/O + numpy — no DuckDB,
no FAISS.

Run:
    pytest tests/unit/test_matchup_provider_sim421.py -v
"""

from __future__ import annotations

import numpy as np

from simulation.fingerprints import (
    BATTED_BALL_FINGERPRINT_DIM,
    PITCH_FINGERPRINT_DIM,
    MatchupProfile,
)
from simulation.matchup_provider import (
    BATTEDBALL_CENTROIDS_FILE,
    BATTEDBALL_NORM_FILE,
    PITCH_CENTROIDS_FILE,
    PITCH_NORM_FILE,
    PrecomputedMatchupProvider,
    battedball_key,
    load_battedball_norm,
    load_pitch_norm,
    load_provider,
    pitch_key,
    read_centroids,
    read_norm,
    write_centroids,
    write_norm,
)

SEASON = 2024
PITCHER = 477132


# ===========================================================================
# Norm-stats round trip
# ===========================================================================


class TestNormRoundTrip:
    def test_write_read_norm(self, tmp_path):
        mean = np.arange(10, dtype=float)
        std = np.full(10, 2.0)
        write_norm(str(tmp_path), PITCH_NORM_FILE, mean, std)
        got = read_norm(str(tmp_path), PITCH_NORM_FILE)
        assert got is not None
        gmean, gstd = got
        np.testing.assert_allclose(gmean, mean)
        np.testing.assert_allclose(gstd, std)

    def test_read_norm_missing_returns_none(self, tmp_path):
        assert read_norm(str(tmp_path), PITCH_NORM_FILE) is None

    def test_zero_std_floored_to_one(self, tmp_path):
        # A constant feature (std 0) must not produce a divide-by-zero query.
        write_norm(str(tmp_path), PITCH_NORM_FILE, np.zeros(3), np.array([0.0, 5.0, 0.0]))
        _, std = read_norm(str(tmp_path), PITCH_NORM_FILE)
        np.testing.assert_allclose(std, [1.0, 5.0, 1.0])

    def test_load_pitch_norm_dim_guard(self, tmp_path):
        # Wrong dim -> None (defensive: a stale/garbled artifact disables the deriver).
        write_norm(str(tmp_path), PITCH_NORM_FILE, np.zeros(3), np.ones(3))
        assert load_pitch_norm(str(tmp_path)) is None
        write_norm(
            str(tmp_path),
            PITCH_NORM_FILE,
            np.zeros(PITCH_FINGERPRINT_DIM),
            np.ones(PITCH_FINGERPRINT_DIM),
        )
        norm = load_pitch_norm(str(tmp_path))
        assert norm is not None and norm.mean.shape[0] == PITCH_FINGERPRINT_DIM

    def test_load_battedball_norm(self, tmp_path):
        write_norm(
            str(tmp_path),
            BATTEDBALL_NORM_FILE,
            np.zeros(BATTED_BALL_FINGERPRINT_DIM),
            np.ones(BATTED_BALL_FINGERPRINT_DIM),
        )
        norm = load_battedball_norm(str(tmp_path))
        assert norm is not None and norm.mean.shape[0] == BATTED_BALL_FINGERPRINT_DIM


# ===========================================================================
# Centroid round trip
# ===========================================================================


class TestCentroidRoundTrip:
    def test_write_read_centroids(self, tmp_path):
        mapping = {pitch_key(SEASON, PITCHER, "R"): [float(i) for i in range(10)]}
        write_centroids(str(tmp_path), PITCH_CENTROIDS_FILE, mapping)
        got = read_centroids(str(tmp_path), PITCH_CENTROIDS_FILE)
        assert pitch_key(SEASON, PITCHER, "R") in got
        np.testing.assert_allclose(got[pitch_key(SEASON, PITCHER, "R")], np.arange(10))

    def test_read_centroids_missing_returns_empty(self, tmp_path):
        assert read_centroids(str(tmp_path), PITCH_CENTROIDS_FILE) == {}


# ===========================================================================
# The provider
# ===========================================================================


def _provider() -> PrecomputedMatchupProvider:
    pitch = {
        pitch_key(SEASON, PITCHER, "R"): np.arange(10, dtype=float),
        pitch_key(SEASON, 0, "R"): np.full(10, 99.0),  # fall-back centroid
    }
    bb = {battedball_key(SEASON, "R"): np.array([89.0, 12.0, -3.0])}
    return PrecomputedMatchupProvider(
        pitch, bb, pitch_global=np.full(10, -1.0), battedball_global=np.full(3, -2.0)
    )


class TestProvider:
    def test_returns_matchup_profile_shapes(self):
        prof = _provider()(PITCHER, "R", SEASON, batter_id=123)
        assert isinstance(prof, MatchupProfile)
        assert prof.arsenal.shape[0] == 8
        assert prof.intended_location.shape[0] == 2
        assert prof.batted_ball.shape[0] == BATTED_BALL_FINGERPRINT_DIM

    def test_arsenal_then_location_split(self):
        prof = _provider()(PITCHER, "R", SEASON, None)
        np.testing.assert_allclose(prof.arsenal, np.arange(8))
        np.testing.assert_allclose(prof.intended_location, np.array([8.0, 9.0]))

    def test_unknown_pitcher_falls_back_to_pitcher_zero(self):
        prof = _provider()(999999, "R", SEASON, None)
        np.testing.assert_allclose(prof.arsenal, np.full(8, 99.0))

    def test_unknown_hand_and_pitcher_falls_back_to_global(self):
        prof = _provider()(999999, "L", SEASON, None)
        np.testing.assert_allclose(prof.arsenal, np.full(8, -1.0))

    def test_unknown_battedball_key_uses_global(self):
        prof = _provider()(PITCHER, "L", SEASON, None)
        np.testing.assert_allclose(prof.batted_ball, np.full(3, -2.0))


# ===========================================================================
# load_provider end to end
# ===========================================================================


class TestLoadProvider:
    def test_none_when_artifacts_absent(self, tmp_path):
        # No artifacts -> None so the factory falls back to the stub fingerprint.
        assert load_provider(str(tmp_path)) is None

    def test_builds_when_both_centroid_files_present(self, tmp_path):
        write_centroids(
            str(tmp_path),
            PITCH_CENTROIDS_FILE,
            {pitch_key(SEASON, PITCHER, "R"): [1.0] * 10},
        )
        write_centroids(
            str(tmp_path),
            BATTEDBALL_CENTROIDS_FILE,
            {battedball_key(SEASON, "R"): [89.0, 12.0, -3.0]},
        )
        provider = load_provider(str(tmp_path))
        assert isinstance(provider, PrecomputedMatchupProvider)
        prof = provider(PITCHER, "R", SEASON, None)
        np.testing.assert_allclose(prof.arsenal, np.ones(8))

    def test_none_when_only_one_centroid_file(self, tmp_path):
        write_centroids(
            str(tmp_path), PITCH_CENTROIDS_FILE, {pitch_key(SEASON, PITCHER, "R"): [1.0] * 10}
        )
        assert load_provider(str(tmp_path)) is None
