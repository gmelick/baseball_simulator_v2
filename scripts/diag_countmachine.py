"""SIM-429: prove that count-conditioning fixes the walk inflation.

Pulls the pool's per-count outcome distribution, then runs the count machine two
ways — count-BLIND (every pitch uses the 0-0 dist, = the current full-pool draw)
vs count-CONDITIONED (each pitch uses the live count's dist) — and reports the
resulting per-PA walk/strikeout/in_play rates.
"""

import os

import duckdb
import numpy as np

con = duckdb.connect(
    os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb"), read_only=True
)
OUT = ["ball", "called_strike", "swinging_strike", "foul", "in_play"]
dist = {}
for b, s, *cnts in con.execute(f"""
    SELECT count_balls, count_strikes,
      {", ".join(f"SUM(CASE WHEN outcome_type='{o}' THEN 1 ELSE 0 END)" for o in OUT)}
    FROM sim.pitch_pool WHERE stand='R' AND season>=2024 AND count_balls<=3 AND count_strikes<=2
    GROUP BY 1, 2
""").fetchall():
    arr = np.array(cnts, dtype=np.float64)
    dist[(int(b), int(s))] = arr / arr.sum()

rng = np.random.default_rng(0)


def play_pa(conditioned: bool):
    b = s = 0
    for _ in range(40):
        p = dist[(b, s)] if conditioned else dist[(0, 0)]
        o = OUT[rng.choice(5, p=p)]
        if o == "ball":
            b += 1
            if b == 4:
                return "walk"
        elif o in ("called_strike", "swinging_strike"):
            s += 1
            if s == 3:
                return "strikeout"
        elif o == "foul":
            s = min(s + 1, 2)
        else:
            return "in_play"
    return "in_play"


for mode in ("count-BLIND (current)", "count-CONDITIONED (fix)"):
    cond = mode.startswith("count-COND")
    import collections

    c = collections.Counter(play_pa(cond) for _ in range(100_000))
    n = sum(c.values())
    print(
        f"{mode:28s}: walk {100 * c['walk'] / n:4.1f}%  K {100 * c['strikeout'] / n:4.1f}%  "
        f"in_play {100 * c['in_play'] / n:4.1f}%   (MLB ~ walk 8.5  K 23  in_play 68)"
    )
