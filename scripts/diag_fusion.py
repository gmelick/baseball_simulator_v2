"""SIM-429 diagnostic: which full-pool factor distorts the pitch-outcome marginal?

Compares the recency-weighted outcome distribution of the R-hand pitch pool under
each factor in isolation, against the true pool marginal (recency only). A factor
that shifts the mix away from the marginal is a distortion source.
"""

import collections
import os

import numpy as np

from pipeline.batch.engine_artifacts import EngineArtifacts

a = EngineArtifacts.load(os.environ.get("ART_DIR", "/data/play_pool/engine_artifacts"))
pool = a.pools["R"]
oc = pool.outcome_type
rec = pool.recency.astype(np.float64)
sit = pool.sit
UNIQ = ["ball", "called_strike", "swinging_strike", "foul", "in_play"]


def marg(w):
    w = np.asarray(w, dtype=np.float64)
    tot = w.sum()
    return {o: round(100 * w[oc == o].sum() / tot) for o in UNIQ}


def f_sit(state, dims=None, sigma=2.0):
    s = sit if dims is None else sit[:, dims]
    q = np.asarray(state, dtype=np.float32)
    diff = s - q
    d2 = np.einsum("ij,ij->i", diff, diff)
    return np.exp(-d2 / (2.0 * sigma**2 * s.shape[1]))


print("=== pitch-outcome marginal under each factor (R pool, N=%d) ===" % pool.n)
print("MLB-ish target ~  ball 36  cs 17  ss 10  foul 17  in_play 17 (rough)")
print("recency only (true pool marginal):", marg(rec))

# f_situation WITH count, fixed at PA-start 0-0 (what the sampler currently does)
print("× f_situation [6d incl count @0-0]:", marg(rec * f_sit([0, 0, 1, 0, 5, 0])))
# f_situation WITHOUT count (base-out/inning/score only)
print("× f_situation [4d no count]       :", marg(rec * f_sit([1, 0, 5, 0], dims=[2, 3, 4, 5])))

# f_batter — a representative batter
bemb = a.actor_emb["batter"]
bk = next(iter(bemb["key_index"]))
vz = (bemb["vecs"] - bemb["mean"]) / bemb["std"]
q = vz[bemb["key_index"][bk]]
aff = np.exp(-np.einsum("ij,ij->i", vz - q, vz - q) / (2.0 * 3.0**2 * vz.shape[1]))
ki = bemb["key_index"]
pool_bat = np.fromiter(
    (ki.get(f"{int(b)}:{int(s)}", -1) for b, s in zip(pool.batter_id, pool.season, strict=False)),
    dtype=np.int64, count=pool.n,
)
f_bat = np.where(pool_bat >= 0, aff[np.clip(pool_bat, 0, len(aff) - 1)], 1.0)
print("× f_batter [%s]:" % bk, marg(rec * f_bat))

# f_pitcher — a representative pitcher
pk = next(iter(a.pitcher_sim))
sims = a.pitcher_sim[pk]
n_prof = len(a.pitcher_sim_index)
ps = np.zeros(n_prof, dtype=np.float64)
for k, v in sims.items():
    j = a.pitcher_sim_index.get(k)
    if j is not None:
        ps[j] = v
pool_prof = np.fromiter(
    (a.pitcher_sim_index.get(f"{int(p)}:{int(s)}", -1) for p, s in zip(pool.pitcher_id, pool.season, strict=False)),
    dtype=np.int64, count=pool.n,
)
f_pit = np.where(pool_prof >= 0, ps[np.clip(pool_prof, 0, n_prof - 1)], 1.0)
print("× f_pitcher [%s] (frac matched %.2f):" % (pk, (pool_prof >= 0).mean()), marg(rec * f_pit))

# ALL multiplied (count-inclusive situation, the current sampler)
allw = rec * f_sit([0, 0, 1, 0, 5, 0]) * f_bat * f_pit
print("× ALL (current sampler):", marg(allw))
