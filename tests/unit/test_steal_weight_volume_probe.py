"""The steal-weight volume probe's arithmetic, on synthetic inputs (no DB, no bundle).

``scripts/steal_weight_volume_probe.py`` asks why the steal draw's look-alike
weights lower the number of steal attempts. These tests pin the mechanism it
reports and the balance it proposes:

  * a look-alike weight takes mass from the rows of a rare kind of actor, so a
    pool replayed through its own draw returns fewer of the plays those rows hold;
  * the loss is zero at no contrast and at full contrast, and largest between;
  * the balance weight returns every actor's rows their own mass, and the
    replayed pool its own attempts, at any power;
  * the balance keeps who steals: a rare actor still draws more from his like.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pb = _load("steal_weight_volume_probe")


def _two_kinds(k_cross: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Four actors who seldom run (80% of the rows) and one who runs often
    (20%). A pair of the same kind scores 1.0, a pair across kinds ``k_cross``."""
    n = np.array([20.0, 20.0, 20.0, 20.0, 20.0])
    att = np.array([0.2, 0.2, 0.2, 0.2, 4.0])  # the rare actor holds most attempts
    score = np.full((5, 5), 1.0)
    score[:4, 4] = k_cross
    score[4, :4] = k_cross
    return score, n, att


def test_a_look_alike_weight_takes_attempts_from_the_rare_actor():
    score, n, att = _two_kinds(0.5)
    kept = pb.replay_ratio(pb.kernel(score, 1.0), n, att, None)
    # by hand: the rare rows receive 0.8*0.5/0.9 + 0.2/0.6 = 0.778 of their mass
    rare = 0.8 * 0.5 / 0.9 + 0.2 / 0.6
    common = 0.8 / 0.9 + 0.2 * 0.5 / 0.6
    assert kept == pytest.approx((0.8 * common + 4.0 * rare) / 4.8)
    assert kept < 0.85


@pytest.mark.parametrize("k_cross", [1.0, 1e-9])
def test_no_loss_at_no_contrast_and_at_full_contrast(k_cross):
    score, n, att = _two_kinds(k_cross)
    assert pb.replay_ratio(pb.kernel(score, 1.0), n, att, None) == pytest.approx(1.0, abs=1e-6)


@pytest.mark.parametrize("power", [1.0, 2.0, 4.0])
def test_the_balance_returns_every_row_its_mass_and_the_pool_its_attempts(power):
    rng = np.random.default_rng(7)
    size = 40
    x = rng.gamma(1.0, 1.0, size=size)  # a skewed trait: a long tail of rare actors
    score = np.exp(-((x[:, None] - x[None, :]) ** 2) / 2.0)
    n = rng.uniform(5.0, 50.0, size=size)
    att = n * 0.01 * (1.0 + 3.0 * x)  # the tail holds the attempts
    k = pb.kernel(score, power)
    assert pb.replay_ratio(k, n, att, None) < 0.99  # the raw weight loses attempts
    d, miss, _it = pb.balance_weight(k, n)
    assert miss < 1e-6
    assert pb.replay_ratio(k, n, att, d) == pytest.approx(1.0, abs=1e-5)
    got = pb.received_by_group(k, n, d, np.arange(size), size)
    assert np.allclose(got, 1.0, atol=1e-5)
    assert float((n / n.sum() * d).sum()) == pytest.approx(1.0)


def test_the_balance_keeps_who_steals():
    score, n, att = _two_kinds(0.5)
    k = pb.kernel(score, 1.0)
    d, _miss, _it = pb.balance_weight(k, n)
    g = k * (n * d)[None, :]
    rate = att / n
    pred = (g @ rate) / g.sum(axis=1)
    assert pred[4] > 2.0 * pred[0]  # the actor who runs is still told apart
    assert d[4] > d[0]  # the rare actor's rows weigh more for every live actor


def test_the_balance_for_the_live_season_is_exact_when_only_that_season_draws():
    """In a simulated game only the current season's actors are live. The
    pool-wide balance then misses; the balance fitted for the live actors
    returns the pool's own attempt rate."""
    rng = np.random.default_rng(11)
    size = 60
    x = rng.gamma(1.0, 1.0, size=size)
    score = np.exp(-((x[:, None] - x[None, :]) ** 2) / 2.0)
    n = rng.uniform(5.0, 50.0, size=size)
    att = n * 0.01 * (1.0 + 3.0 * x)
    live = np.zeros(size, dtype=bool)
    live[np.argsort(x)[:20]] = True  # the live season holds the actors who seldom run
    k = pb.kernel(score, 1.0)
    d_pool, _miss, _it = pb.balance_weight(k, n)
    d_live, miss, _it = pb.balance_weight_live(k[live], n, n[live])
    assert miss < 1e-6
    assert abs(pb.replay_ratio_live(k[live], n, att, n[live], d_pool) - 1.0) > 0.02
    assert pb.replay_ratio_live(k[live], n, att, n[live], d_live) == pytest.approx(1.0, abs=1e-5)
    # with every actor live the two fits are the same balance
    d_all, _m, _i = pb.balance_weight_live(k, n, n)
    assert np.allclose(d_all, d_pool, rtol=1e-5)


def test_an_unscored_pair_leans_no_way():
    score, _n, _att = _two_kinds(0.5)
    score[0, 1] = np.nan
    k = pb.kernel(score, 2.0)
    assert np.isfinite(k).all()
    assert k[0, 1] == pytest.approx(np.nanmean(np.where(np.isnan(score), np.nan, score**2)))


def test_the_one_step_density_weight_overshoots_less_than_the_raw_loss():
    score, n, att = _two_kinds(0.5)
    k = pb.kernel(score, 1.0)
    raw = pb.replay_ratio(k, n, att, None)
    one = pb.replay_ratio(k, n, att, pb.density_weight(k, n))
    assert abs(one - 1.0) < abs(raw - 1.0)
