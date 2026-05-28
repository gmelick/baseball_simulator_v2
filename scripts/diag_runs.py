"""SIM-425 diagnosis: where are runs lost on the full-pool path?

Reports (1) the batted-ball OUT event/launch-angle mix (can we identify fly outs
for tag-ups + ground outs for forced advancement?), and (2) the baserunner
embedding features (find the sprint-speed signal for advancement scaling).
"""

import collections
import os

import numpy as np

from pipeline.batch.engine_artifacts import EngineArtifacts

a = EngineArtifacts.load(os.environ.get("ART_DIR", "/data/play_pool/engine_artifacts"))

print("=== batted-ball OUT events (R pool) — event x mean launch_angle ===")
pool = a.bb_pools["R"]
ev = np.asarray(pool.event, dtype=object)
la = pool.geom[:, 1]  # launch angle
hits = pool.result_hits
outs = pool.result_outs
is_out = hits == 0
by_ev = collections.Counter(ev[is_out])
for e, c in by_ev.most_common(15):
    m = is_out & (ev == e)
    print(f"  {str(e):28s} n={c:6d}  LA mean={la[m].mean():6.1f}  outs mean={outs[m].mean():.2f}")
print(
    f"  TOTAL out rows={is_out.sum()}  (fly LA>=20: {(is_out & (la>=20)).sum()}, "
    f"ground LA<10: {(is_out & (la<10)).sum()})"
)

print("\n=== baserunner embedding features ===")
bemb = a.actor_emb.get("baserunner")
if bemb is None:
    print("  (no baserunner embedding loaded)")
else:
    feats = bemb.get("features")
    print(f"  rows={len(bemb['key_index'])}  features={feats}")
    print(f"  mean={np.round(bemb['mean'], 3)}")
    print(f"  std ={np.round(bemb['std'], 3)}")
