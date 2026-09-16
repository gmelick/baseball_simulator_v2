"""
tests/unit/test_sim548_sit_sigma_env.py
=======================================
SIM-548: the pitch draw's situation bandwidth has an environment variable.

``FullPoolSampler.sit_sigma`` is the Gaussian on the base-out situation. Its
code default is 2.0. Before SIM-548 no variable set it, so the offline fit
could ladder it but no arm could run it. ``SIM_SIT_SIGMA`` closes that gap:
the factory applies it to the production sampler next to the fatigue sigmas.

The sampler is built over a tiny in-memory bundle, the way
``tests/unit/test_sim523_fit_powers.py`` builds one.
"""

from __future__ import annotations

import logging

import numpy as np
import pytest

from pipeline.batch.engine_artifacts import EngineArtifacts, HandPool
from simulation.full_pool_sampler import FullPoolSampler
from simulation.production_factory import DEFAULT_SIT_SIGMA, apply_sit_sigma_env

_SEASON = 2024
_LIVE = "100:2024"
_GEOM = np.array([95.0, 15.0, 5.0, 2300.0, 200.0, -1.0, 6.0, 6.5, 0.0, 2.5], dtype=np.float32)


def _sampler() -> FullPoolSampler:
    """One pitcher, one batter, two rows: the smallest bundle the sampler accepts."""
    n = 2
    sit = np.zeros((n, 6), dtype=np.float32)
    sit[:, 4] = 5.0
    pool = HandPool(
        geom=np.stack([_GEOM] * n).astype(np.float32),
        sit=sit,
        pitcher_id=np.full(n, 100, dtype=np.int64),
        batter_id=np.full(n, 200, dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        outcome_type=np.asarray(["strikeout", "single"], dtype=object),
        recency=np.ones(n, dtype=np.float32),
    )
    art = EngineArtifacts(
        pools={"R": pool},
        pitcher_sim={_LIVE: {_LIVE: 1.0}},
        pitcher_sim_index={_LIVE: 0},
        bb_pools={},
        actor_emb={
            "batter": {
                "key_index": {"200:2024": 0},
                "vecs": np.zeros((1, 2), dtype=np.float32),
                "mean": np.zeros(2, dtype=np.float32),
                "std": np.ones(2, dtype=np.float32),
            }
        },
    )
    return FullPoolSampler(art, np.random.default_rng(0))


def test_code_default_is_two() -> None:
    """The sampler's constructor default and the factory's default agree."""
    assert _sampler().sit_sigma == 2.0
    assert DEFAULT_SIT_SIGMA == 2.0


def test_env_sets_sit_sigma() -> None:
    """``SIM_SIT_SIGMA`` lands on the sampler as a float."""
    fp = _sampler()
    apply_sit_sigma_env(fp, {"SIM_SIT_SIGMA": "0.75"})
    assert fp.sit_sigma == pytest.approx(0.75)
    assert isinstance(fp.sit_sigma, float)


def test_unset_keeps_default() -> None:
    """No variable keeps 2.0, even after a fitted value was applied before."""
    fp = _sampler()
    fp.sit_sigma = 0.5
    apply_sit_sigma_env(fp, {})
    assert fp.sit_sigma == 2.0


def test_bad_value_keeps_default_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    """A value that does not parse keeps 2.0 and logs a warning."""
    fp = _sampler()
    with caplog.at_level(logging.WARNING, logger="simulation.production_factory"):
        apply_sit_sigma_env(fp, {"SIM_SIT_SIGMA": "wide"})
    assert fp.sit_sigma == 2.0
    assert any("SIM_SIT_SIGMA" in rec.getMessage() for rec in caplog.records)


def test_env_reads_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no explicit mapping the helper reads the process environment."""
    monkeypatch.setenv("SIM_SIT_SIGMA", "1.25")
    fp = _sampler()
    apply_sit_sigma_env(fp)
    assert fp.sit_sigma == pytest.approx(1.25)


def test_sit_sigma_changes_the_situation_weight() -> None:
    """The bandwidth reaches the draw: a row far from the live situation
    weighs less under a narrow bandwidth than under a wide one."""
    fp = _sampler()
    far = np.zeros(6, dtype=np.float32)
    far[4] = 5.0
    far[0] = 3.0  # a different base state from every pool row
    apply_sit_sigma_env(fp, {"SIM_SIT_SIGMA": "2.0"})
    wide = float(fp._f_situation("R", far)[0])
    apply_sit_sigma_env(fp, {"SIM_SIT_SIGMA": "0.5"})
    narrow = float(fp._f_situation("R", far)[0])
    assert narrow < wide
