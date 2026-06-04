"""Measure how concentrated the sampler's 1/(d+eps)*recency weights are over a
tile, to decide whether the k=25 cutoff discards meaningful mass.

For each tile we report, for the CENTROID query (the deriver's output) and for
50 JITTERED queries (what the loop actually sends, sigma=1.25):
  * N                : tile size (rows available)
  * ESS_25           : effective sample size of the CURRENT top-25 draw
                       (1/sum(p^2) over the 25 recency-adjusted weights) --
                       how many distinct plays the draw really pulls from
  * ESS_full         : effective sample size if we weighted ALL N rows
  * top25_wfrac      : % of the FULL-tile weight the 25 nearest rows hold
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT))
from simulation.batch_runner import GameSpec
from simulation.game_state import GameState
from simulation.play_pool_sampler import POOL_BATTEDBALL, POOL_PITCH
from simulation.production_factory import production_machine_factory
from simulation.sim_loop import _BB_FEATURE_SCALE, _PITCH_FEATURE_SCALE, _QUERY_JITTER_SIGMA

_FACTORY = "simulation.production_factory:production_machine_factory"


def ess(w):
    w = np.asarray(w, dtype=float)
    s = w.sum()
    if s <= 0:
        return 0.0
    p = w / s
    return float(1.0 / np.sum(p * p))


def _weights(sampler, handle, query, k):
    pos, w, _d = sampler._knn(handle, query, k)
    w = sampler._apply_recency(handle, pos, w)  # recency-adjusted, normalized
    return np.asarray(w, dtype=float)


def _report(label, sampler, handle, fp, scale):
    n = handle.n_vectors
    w25 = _weights(sampler, handle, fp, 25)
    wN = _weights(sampler, handle, fp, n)
    top25 = float(np.sort(wN)[::-1][:25].sum())
    print(
        f"  {label:9s} centroid : N={n:6d}  ESS_25={ess(w25):5.1f}  "
        f"ESS_full={ess(wN):7.1f}  top25_wfrac={100 * top25:4.0f}%"
    )
    rng = np.random.default_rng(0)
    sc = np.asarray(scale, dtype=np.float32) * _QUERY_JITTER_SIGMA
    e25, efull, t25 = [], [], []
    for _ in range(50):
        jq = (fp + rng.standard_normal(np.shape(fp)).astype(np.float32) * sc).astype(np.float32)
        e25.append(ess(_weights(sampler, handle, jq, 25)))
        wn = _weights(sampler, handle, jq, n)
        efull.append(ess(wn))
        t25.append(float(np.sort(wn)[::-1][:25].sum()))
    print(
        f"  {label:9s} jitter50 : N={n:6d}  ESS_25={np.mean(e25):5.1f}  "
        f"ESS_full={np.mean(efull):7.1f}  top25_wfrac={100 * np.mean(t25):4.0f}%"
    )


def main():
    season = 2026
    for pitcher, hand in [(647336, "L"), (647336, "R"), (434378, "L"), (434378, "R")]:
        spec = GameSpec(
            machine_factory=_FACTORY,
            sim_kwargs={"pitcher_id": pitcher, "bat_hand": hand, "season": season, "_k": 25},
        )
        m = production_machine_factory(0, spec)
        s = m._pa.sampler
        print(f"\n=== PITCH tile {season}/{pitcher}/{hand} ===")
        handle = s.load_tile(POOL_PITCH, season, hand, pitcher_id=pitcher)
        st = GameState(pitcher_id=pitcher, bat_hand=hand, season=season, batter_id=1)
        fp = m._pa._pitch_fingerprint(st)
        _report("pitch", s, handle, fp, _PITCH_FEATURE_SCALE)

    # One batted-ball tile (these are huge -- league-wide per hand/season).
    spec = GameSpec(
        machine_factory=_FACTORY,
        sim_kwargs={"pitcher_id": 647336, "bat_hand": "R", "season": season, "_k": 25},
    )
    m = production_machine_factory(0, spec)
    s = m._pa.sampler
    for hand in ("L", "R"):
        print(f"\n=== BATTEDBALL tile {season}/{hand} ===")
        handle = s.load_tile(POOL_BATTEDBALL, season, hand)
        st = GameState(pitcher_id=647336, bat_hand=hand, season=season, batter_id=1)
        bb = m._pa._battedball_fingerprint(st)
        _report("battedbl", s, handle, bb, _BB_FEATURE_SCALE)


if __name__ == "__main__":
    main()
