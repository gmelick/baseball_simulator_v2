"""SIM-423 perf gate: benchmark a full-pool factorized weighted draw over the REAL
per-hand pitch pool, to decide if full-pool scoring meets the 2s/game SLA.

Timing does not depend on the similarity VALUES, so placeholder per-actor vectors
are used; the cost is dominated by the vectorized passes over the full pool.
"""

import os
import time

import duckdb
import numpy as np

HAND = os.environ.get("PERF_HAND", "R")
con = duckdb.connect(
    os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb"), read_only=True
)

t0 = time.time()
cols = (
    "velo,ivb,hb,spin_rate,spin_axis,release_x,release_z,release_ext,plate_x,plate_z,"
    "pitcher_id,count_balls,count_strikes,outs,runners_state,inning,score_diff,recency_weight"
)
d = con.execute(f"SELECT {cols} FROM sim.pitch_pool WHERE stand='{HAND}'").fetchnumpy()
N = len(d["velo"])
geom = np.ascontiguousarray(
    np.stack(
        [
            d[c]
            for c in [
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
            ]
        ],
        axis=1,
    ).astype(np.float32)
)
sit = np.ascontiguousarray(
    np.stack(
        [
            d[c]
            for c in [
                "count_balls",
                "count_strikes",
                "outs",
                "runners_state",
                "inning",
                "score_diff",
            ]
        ],
        axis=1,
    ).astype(np.float32)
)
geom = np.nan_to_num(geom)
sit = np.nan_to_num(sit)
pid = d["pitcher_id"].astype(np.int64)
uniq, pid_idx = np.unique(pid, return_inverse=True)  # dense pitcher index
recency = np.nan_to_num(d["recency_weight"].astype(np.float32))
pitcher_sim = (
    np.random.default_rng(0).random(len(uniq)).astype(np.float32)
)  # placeholder per-pitcher score
batter_sim_const = np.float32(
    0.5
)  # placeholder (batter factor is a per-PA gather, ~= pitcher cost)
print(
    f"[load] hand={HAND} N={N:,} geom={geom.nbytes / 1e6:.0f}MB sit={sit.nbytes / 1e6:.0f}MB  in {time.time() - t0:.2f}s"
)

rng = np.random.default_rng(1)


def timeit(fn, reps=5):
    best = 1e9
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best * 1000  # ms


q = geom.mean(axis=0).astype(np.float32)  # a query (pitcher centroid stand-in)
s = sit[rng.integers(N)].astype(np.float32)  # a situation query


def f_geom():
    diff = geom - q
    return np.exp(-np.einsum("ij,ij->i", diff, diff) / 50.0)


def f_situation():
    diff = sit - s
    return np.exp(-np.einsum("ij,ij->i", diff, diff) / 10.0)


def f_pitcher():
    return pitcher_sim[pid_idx]


# Per-half-inning base = f_geom * f_pitcher * recency (computed once per pitcher change)
def half_inning_base():
    return (f_geom() * f_pitcher() * recency).astype(np.float32)


base = None


def pa_setup():
    global base
    base = half_inning_base()  # (cache across the half-inning; counted here once)
    w = base * f_situation() * batter_sim_const  # per-PA: situation + batter factors
    cdf = np.cumsum(w)  # alias alternative: cumsum for searchsorted draws
    return cdf


def per_pitch_draw(cdf):
    r = rng.random() * cdf[-1]
    return int(np.searchsorted(cdf, r))


print(f"[f_geom]          {timeit(f_geom):7.1f} ms")
print(f"[f_situation]     {timeit(f_situation):7.1f} ms")
print(f"[f_pitcher gather]{timeit(f_pitcher):7.1f} ms")
print(f"[half_inning_base]{timeit(half_inning_base):7.1f} ms")
cdf = pa_setup()
print(
    f"[pa_setup total]  {timeit(pa_setup):7.1f} ms   (situation+batter+cumsum; base cached/half-inning)"
)
print(f"[per_pitch_draw]  {timeit(lambda: per_pitch_draw(cdf)):7.3f} ms")

pa_ms = timeit(pa_setup)
base_ms = timeit(half_inning_base)
# ~80 PA/game, ~18 half-innings/game, ~290 pitches/game
game_ms = 80 * pa_ms + 18 * base_ms + 290 * timeit(lambda: per_pitch_draw(cdf))
print(f"\n=== EXTRAPOLATED per game: {game_ms / 1000:.2f} s  (SLA = 2.0 s/game) ===")
print(
    f"=== per 100-game batch (1 worker): {100 * game_ms / 1000:.0f} s  (SLA = 30 s, but batch parallelizes across workers) ==="
)
