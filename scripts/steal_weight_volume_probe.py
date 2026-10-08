"""
scripts/steal_weight_volume_probe.py — why the steal draw's look-alike weights
lower the number of steal attempts, and what restores it.

THE QUESTION
============
The steal draw weighs every row of its count cell by three look-alike scores
(the runner, the pitcher's hold, the catcher's arm). The SIM-554 replay read
the draw 20% under its own unweighted rate at production's powers (1.225 steals
per 100 opportunity pitches against 1.524). This script finds which part of the
weight does it and scores the candidate fixes on the same real pitches.

It plays no games. It imports the replay (``scripts/sim554_running_game_replay.py``,
the SIM-554 branch) for the sampler, the in-memory steal pools and the sample of
real pitches, and reads the draw's whole jar through ``FullPoolSampler.steal_weights``.
For each real pitch it takes the base weight from that seam (every look-alike
power 0: the recency, the score-margin bell curve, the point-in-time cutoff) and
the raw scores from the matrices the sampler holds, then composes each variant.
The production variants are checked against ``steal_weights`` itself.

THE VARIANTS (the rule for one look-alike factor on one draw)
  prod      what ``_matrix_gather`` does: the score to its power; a row with no
            score weighs 1.0 at power 1 and the mean scored weight at any other
  neutral   the same, but a row with no score weighs the mean scored weight at
            every power (suspect 1)
  density   the score to its power, times the inverse of the row actor's mean
            score against the pool (one step of the balance below)
  balanced  the score to its power, times a per-actor weight d(y) fitted so
            that the pool, replayed through its own draw, puts on every actor's
            rows the mass those rows hold (a Sinkhorn balance; suspect 3)
  noconf    the engine's score without its sample-confidence multiplier (the
            engines multiply every pair by sqrt(min(confidence of the two
            profiles)), so a row whose actor had few chances scores low against
            every live actor), to its power; unscored rows as ``neutral``
  ncbal     ``noconf``, then balanced
  sbal      "season balanced": the balance fitted for the actors who can be
            live — the replayed season's — over the rows that season can draw.
            In a simulated game only one season's actors are live; this is the
            rule the finding recommends

The draw's other soft weight, the bell curve on the score margin, moves mass the
same way. Five variants are repeated with the curve off ("no margin curve") and
with the curve balanced inside every count cell ("margin balanced").

THE READS
  1. the closed form: the pool replayed through one factor alone, no cells —
     the volume each factor keeps, by power and rule
  2. where the rows with no score sit (attempted against not)
  3. the mean score on attempted and on other rows
  4. the mass each tier of pool runner receives against the mass it holds
  5. every variant on the real pitches: steals per 100, by target base, by
     runner tier, the per-runner correlation, the "how many steals" Brier score

RUN (PowerShell, from the main checkout; the app stays up — every read is
read-only). About 30 ms a pitch: 40 minutes for 2026, 50 for 2025. The pass
saves a checkpoint every 5,000 pitches and resumes from it (the host reboots):

    docker compose run -d --name steal_probe_2026 `
      -v <sim554 worktree>/scripts:/app/scripts `
      -v <sim554 worktree>/simulation:/app/simulation `
      -v <this worktree>/scripts:/app/probe `
      app python /app/probe/steal_weight_volume_probe.py --season 2026

The record: ``docs/audit/2026-09-30-steal-weight-volume-finding.md``.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

for _p in ("/app", "/app/scripts"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: (matrix name, the steal meta's embedding-row key, a short label)
FACTORS = (
    ("runner_steal", "runner_rows", "runner"),
    ("pitcher_steal", "pitcher_rows", "pitcher"),
    ("catcher_throwing", "catcher_rows", "catcher"),
)
BAL_POWERS = (1.0, 2.0, 4.0, 12.0)
#: the seasons whose actors are tried as the only live ones (the two replayed)
LIVE_SEASONS = (2025, 2026)


def _all(kind: str, powers: tuple[float, float, float]) -> list[tuple[int, str, float]]:
    return [(k, kind, float(p)) for k, p in enumerate(powers) if p > 0]


#: name -> the factors it applies: (factor index, rule, power)
VARIANTS: dict[str, list[tuple[int, str, float]]] = {
    "off": [],
    "prod 1/1/1": _all("prod", (1, 1, 1)),
    "prod 4/4/2": _all("prod", (4, 4, 2)),
    "prod 12/12/2": _all("prod", (12, 12, 2)),
    "prod runner only 1": [(0, "prod", 1.0)],
    "prod pitcher only 1": [(1, "prod", 1.0)],
    "prod catcher only 1": [(2, "prod", 1.0)],
    "prod runner only 4": [(0, "prod", 4.0)],
    "prod pitcher only 4": [(1, "prod", 4.0)],
    "prod catcher only 2": [(2, "prod", 2.0)],
    "prod runner only 12": [(0, "prod", 12.0)],
    "prod pitcher only 12": [(1, "prod", 12.0)],
    "neutral 1/1/1": _all("neut", (1, 1, 1)),
    "neutral runner only 1": [(0, "neut", 1.0)],
    "neutral pitcher only 1": [(1, "neut", 1.0)],
    "neutral catcher only 1": [(2, "neut", 1.0)],
    "density 1/1/1": _all("dens", (1, 1, 1)),
    "density 4/4/2": _all("dens", (4, 4, 2)),
    "density 12/12/2": _all("dens", (12, 12, 2)),
    "balanced 1/1/1": _all("bal", (1, 1, 1)),
    "balanced 4/4/2": _all("bal", (4, 4, 2)),
    "balanced 12/12/2": _all("bal", (12, 12, 2)),
    "balanced runner only 1": [(0, "bal", 1.0)],
    "balanced pitcher only 1": [(1, "bal", 1.0)],
    "balanced catcher only 1": [(2, "bal", 1.0)],
    "balanced runner only 4": [(0, "bal", 4.0)],
    "balanced runner only 12": [(0, "bal", 12.0)],
    # the runner balanced, the pitcher and the catcher as production has them
    "runner balanced, rest prod 1/1/1": [(0, "bal", 1.0), (1, "prod", 1.0), (2, "prod", 1.0)],
    "runner noconf, rest prod 1/1/1": [(0, "noconf", 1.0), (1, "prod", 1.0), (2, "prod", 1.0)],
    "noconf 1/1/1": _all("noconf", (1, 1, 1)),
    "noconf 4/4/2": _all("noconf", (4, 4, 2)),
    "noconf 12/12/2": _all("noconf", (12, 12, 2)),
    "noconf runner only 1": [(0, "noconf", 1.0)],
    "noconf runner only 4": [(0, "noconf", 4.0)],
    "noconf pitcher only 1": [(1, "noconf", 1.0)],
    "noconf balanced 1/1/1": _all("ncbal", (1, 1, 1)),
    "noconf balanced 4/4/2": _all("ncbal", (4, 4, 2)),
    "noconf balanced 12/12/2": _all("ncbal", (12, 12, 2)),
    "noconf balanced runner only 1": [(0, "ncbal", 1.0)],
    "noconf balanced runner only 4": [(0, "ncbal", 4.0)],
    # the balance fitted with only the replayed season's actors live
    "season balanced 1/1/1": _all("sbal", (1, 1, 1)),
    "season balanced 4/4/2": _all("sbal", (4, 4, 2)),
    "season balanced 12/12/2": _all("sbal", (12, 12, 2)),
    "season balanced runner only 1": [(0, "sbal", 1.0)],
}
#: The draw's other soft weight is the bell curve on the score margin. Each
#: variant above keeps it as production has it; these repeat five of them with
#: the curve off, and with the curve balanced inside every count cell.
_MARGIN_LABEL = {"flat": "no margin curve", "kbal": "margin balanced"}
BASE_OF: dict[str, str] = dict.fromkeys(VARIANTS, "kernel")
for _base, _label in _MARGIN_LABEL.items():
    for _v in (
        "off",
        "prod 1/1/1",
        "balanced 1/1/1",
        "balanced 4/4/2",
        "balanced 12/12/2",
        "season balanced 1/1/1",
        "season balanced 4/4/2",
        "season balanced 12/12/2",
    ):
        VARIANTS[f"{_label}: {_v}"] = list(VARIANTS[_v])
        BASE_OF[f"{_label}: {_v}"] = _base
V_NAMES = tuple(VARIANTS)
V_IDX = {n: i for i, n in enumerate(V_NAMES)}
#: the production variants, checked against ``steal_weights`` itself
CHECKS = {"prod 1/1/1": (1, 1, 1), "prod 4/4/2": (4, 4, 2), "prod 12/12/2": (12, 12, 2)}
#: the variants whose mass by pool-runner tier is recorded
MASS_VARIANTS = (
    "off",
    "prod runner only 1",
    "prod 1/1/1",
    "prod 4/4/2",
    "neutral 1/1/1",
    "balanced runner only 1",
    "balanced 1/1/1",
    "balanced 4/4/2",
    "noconf runner only 1",
    "noconf 1/1/1",
    "noconf balanced 1/1/1",
    "margin balanced: balanced 1/1/1",
)
MARGIN_BINS = 11  # the score margin, clipped to -5..5


def fit_margin(fp: Any) -> tuple[dict[str, dict[tuple[int, int, int], np.ndarray]], dict[str, Any]]:
    """The balance of the score-margin bell curve inside every count cell:
    per target base and cell, one weight per margin (-5..5) for which the
    cell's rows, replayed through the curve, keep the mass they hold. Also the
    closed form: the attempts the curve alone predicts over the attempts held."""
    sigma = float(fp.steal_score_sigma)
    s = np.arange(-5, 6, dtype=np.float64)
    kern = np.exp(-((s[:, None] - s[None, :]) ** 2) / (2.0 * sigma**2))
    dm: dict[str, dict[tuple[int, int, int], np.ndarray]] = {}
    closed: dict[str, Any] = {}
    for tb, pool in fp.a.steal_pools.items():
        meta = fp._steal_meta(tb)
        rec = np.asarray(pool.recency, dtype=np.float64)
        a = np.asarray(pool.attempted) > 0
        bins = np.clip(np.rint(np.asarray(pool.sit)[:, 3]).astype(np.int64), -5, 5) + 5
        dm[tb] = {}
        pred = held = 0.0
        n_all = np.zeros(MARGIN_BINS)
        att_all = np.zeros(MARGIN_BINS)
        for cell, rows in meta["cells"].items():
            n = np.bincount(bins[rows], weights=rec[rows], minlength=MARGIN_BINS)
            att = np.bincount(bins[rows], weights=rec[rows] * a[rows], minlength=MARGIN_BINS)
            n_all += n
            att_all += att
            act = n > 0
            k = kern[np.ix_(act, act)]
            d, _miss, _it = balance_weight(k, n[act])
            full = np.ones(MARGIN_BINS, dtype=np.float64)
            full[act] = d
            dm[tb][cell] = full
            if att.sum() > 0:
                pred += replay_ratio(k, n[act], att[act], None) * float(att.sum())
                held += float(att.sum())
        closed[tb] = {
            "volume_kept_prod": pred / max(held, 1e-12),
            "share_of_rows_by_margin": [float(x) for x in n_all / n_all.sum()],
            "per_100_by_margin": [
                float(100.0 * x) for x in np.divide(att_all, np.maximum(n_all, 1e-12))
            ],
        }
    return dm, closed


# ---------------------------------------------------------------------------
# The balance
# ---------------------------------------------------------------------------


def kernel(mat: np.ndarray, power: float) -> np.ndarray:
    """The score matrix to ``power`` (float64). A pair the engine did not score
    (NaN) reads the mean scored value, so it leans no way."""
    k = np.asarray(mat, dtype=np.float64)
    bad = ~np.isfinite(k)
    k = np.power(np.clip(np.where(bad, 0.0, k), 0.0, None), power)
    if bad.any():
        k[bad] = float(k[~bad].mean()) if (~bad).any() else 1.0
    return k


def density_weight(k: np.ndarray, n: np.ndarray) -> np.ndarray:
    """One step: the inverse of each actor's mean score against the pool
    (``n`` = the mass of each actor's rows), normalized to a pool mean of 1."""
    w = n / n.sum()
    d = 1.0 / np.maximum(w @ k, 1e-300)
    return d / float((w * d).sum())


def balance_weight(
    k: np.ndarray, n: np.ndarray, iters: int = 500, tol: float = 1e-8
) -> tuple[np.ndarray, float, int]:
    """The per-actor weight d(y) for which the pool, replayed through its own
    draw, leaves every actor's rows the mass they hold:

        sum_x n(x) * K(x,y) d(y) n(y) / sum_y' K(x,y') d(y') n(y')  =  n(y)

    A Sinkhorn balance of n(x) K(x,y) n(y). Returns (d with a pool mean of 1,
    the largest relative miss, the iterations used)."""
    w = n / n.sum()
    d = np.ones_like(w)
    miss = float("inf")
    it = 0
    while it < iters:
        it += 1
        r = 1.0 / np.maximum(k @ (d * w), 1e-300)  # each live actor's normalizer
        recv = (w * r) @ k  # the mass an actor's row receives per unit d
        miss = float(np.abs(recv * d - 1.0).max())
        if miss < tol:
            break
        d = 1.0 / np.maximum(recv, 1e-300)
        d = d / float((w * d).sum())
    return d / float((w * d).sum()), miss, it


def balance_weight_live(
    k_live: np.ndarray, n: np.ndarray, n_live: np.ndarray, iters: int = 500, tol: float = 1e-8
) -> tuple[np.ndarray, float, int]:
    """The same balance when only SOME actors are ever live — the current
    season's. ``k_live`` holds one row per live actor and one column per pool
    actor; ``n_live`` is how often each live actor draws. Every pool actor's
    rows then receive, over the live actors' draws, the mass they hold."""
    w = n / n.sum()
    wl = n_live / n_live.sum()
    d = np.ones_like(w)
    miss = float("inf")
    it = 0
    while it < iters:
        it += 1
        r = 1.0 / np.maximum(k_live @ (d * w), 1e-300)
        recv = (wl * r) @ k_live
        miss = float(np.abs(recv * d - 1.0).max())
        if miss < tol:
            break
        d = 1.0 / np.maximum(recv, 1e-300)
        d = d / float((w * d).sum())
    return d / float((w * d).sum()), miss, it


def replay_ratio_live(
    k_live: np.ndarray, n: np.ndarray, att: np.ndarray, n_live: np.ndarray, d: np.ndarray | None
) -> float:
    """The live actors alone drawing from the whole pool through one factor:
    their predicted attempt rate over the pool's own rate."""
    g = k_live * (n if d is None else n * d)[None, :]
    a = np.divide(att, n, out=np.zeros_like(att), where=n > 0)
    pred = (g @ a) / np.maximum(g.sum(axis=1), 1e-300)
    return float((n_live * pred).sum() / n_live.sum() / (att.sum() / n.sum()))


def replay_ratio(k: np.ndarray, n: np.ndarray, att: np.ndarray, d: np.ndarray | None) -> float:
    """The pool replayed through one factor alone (no cells): the attempts the
    draw predicts over the attempts the pool holds."""
    g = k * (n if d is None else n * d)[None, :]
    a = np.divide(att, n, out=np.zeros_like(att), where=n > 0)
    pred = (g @ a) / np.maximum(g.sum(axis=1), 1e-300)
    return float((n * pred).sum() / att.sum())


def received_by_group(
    k: np.ndarray, n: np.ndarray, d: np.ndarray | None, group: np.ndarray, ngroups: int
) -> np.ndarray:
    """The pool replayed through one factor alone: the mass each group of
    actors receives over the mass it holds."""
    g = k * (n if d is None else n * d)[None, :]
    p = g / np.maximum(g.sum(axis=1, keepdims=True), 1e-300)
    recv = n @ p  # mass received by each actor's rows
    out = np.full(ngroups, np.nan)
    for t in range(ngroups):
        m = group == t
        if m.any() and n[m].sum() > 0:
            out[t] = float(recv[m].sum() / n[m].sum())
    return out


# ---------------------------------------------------------------------------
# The factors
# ---------------------------------------------------------------------------

#: matrix -> (module, class, scores its thin profiles). The catcher's throwing
#: sub-score carries no confidence multiplier (only the composite does).
_CONF_ENGINES: dict[str, tuple[str, str, bool]] = {
    "runner_steal": (
        "similarity.engines.baserunner_steal_similarity",
        "BaserunnerStealSimilarityEngine",
        True,
    ),
    "pitcher_steal": (
        "similarity.engines.pitcher_steal_similarity",
        "PitcherStealSimilarityEngine",
        False,
    ),
}


def load_confidence(name: str, seasons: list[int]) -> dict[str, float]:
    """key -> the profile's sample confidence (``eb_alpha``), read from the
    engine the matrix was built with. Empty for a matrix with no multiplier."""
    spec = _CONF_ENGINES.get(name)
    if spec is None:
        return {}
    module, cls, thin = spec
    eng = getattr(importlib.import_module(module), cls)(
        duckdb_path=os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
    )
    if thin:
        eng.build(seasons=seasons, include_below_minimum=True)
    else:
        eng.build(seasons=seasons)
    return {":".join(str(x) for x in k): float(p.eb_alpha) for k, p in eng._profiles.items()}


class Factor:
    """One look-alike factor: its matrix, its column per pool row, and the
    per-actor weights of the density and balanced rules."""

    def __init__(self, fp: Any, k: int):
        self.k = k
        self.name, rows_key, self.label = FACTORS[k]
        entry = fp._actor_matrix(self.name)
        if entry is None:
            raise RuntimeError(f"the bundle has no {self.name} matrix")
        self.index: dict[str, int] = entry["index"]
        self.mat = np.asarray(entry["matrix"])
        # the engine's sample confidence per matrix column (1.0 = no multiplier)
        seasons = sorted({int(key.rsplit(":", 1)[1]) for key in self.index})
        conf = load_confidence(self.name, seasons)
        self.alpha = np.ones(self.mat.shape[0], dtype=np.float64)
        found = 0
        for key, col in self.index.items():
            al = conf.get(key)
            if al is not None and al > 0.0:
                self.alpha[col] = al
                found += 1
        self.conf_found = found if conf else None
        self.col_season = np.zeros(self.mat.shape[0], dtype=np.int64)
        for key, col in self.index.items():
            self.col_season[col] = int(key.rsplit(":", 1)[1])
        self.live_season = 0  # the season of the replayed pitches (set by the pass)
        # the check: a score never exceeds its pair's confidence multiplier
        cap = np.sqrt(np.minimum.outer(self.alpha, self.alpha))
        off_diag = ~np.eye(self.mat.shape[0], dtype=bool)
        self.conf_violation = float(
            (np.where(np.isfinite(self.mat) & off_diag, self.mat, 0.0) - cap).max()
        )
        e2m = fp._emb_to_mat(self.name)
        self.cols: dict[str, np.ndarray] = {}
        self.n: dict[str, np.ndarray] = {}
        self.att: dict[str, np.ndarray] = {}
        self.act: dict[str, np.ndarray] = {}
        self.d: dict[tuple[str, str, float], np.ndarray] = {}
        self.notes: list[str] = []
        self.closed: dict[str, dict[str, Any]] = {}
        size = self.mat.shape[0]
        for tb, pool in fp.a.steal_pools.items():
            meta = fp._steal_meta(tb)
            emb_rows = meta[rows_key]
            if emb_rows is None or e2m is None:
                cols = np.full(pool.n, -1, dtype=np.int64)
            else:  # the same gather ``_matrix_gather`` does
                cols = np.where(emb_rows >= 0, e2m[np.clip(emb_rows, 0, len(e2m) - 1)], -1)
            self.cols[tb] = cols
            v = cols >= 0
            rec = np.asarray(pool.recency, dtype=np.float64)
            a = np.asarray(pool.attempted) > 0
            self.n[tb] = np.bincount(cols[v], weights=rec[v], minlength=size)
            self.att[tb] = np.bincount(cols[v], weights=rec[v] * a[v], minlength=size)
            self.act[tb] = np.where(self.n[tb] > 0)[0]

    def fit(self, tiers_by_col: dict[str, np.ndarray] | None = None) -> None:
        """The density and balanced weights per target base and power, and the
        closed-form read."""
        size = self.mat.shape[0]
        for tb in self.cols:
            act = self.act[tb]
            n = self.n[tb][act]
            att = self.att[tb][act]
            sub = self.mat[np.ix_(act, act)]
            nan_share = float((~np.isfinite(sub)).mean())
            off = sub[~np.eye(len(act), dtype=bool)]
            off = off[np.isfinite(off)]
            q = np.quantile(off, [0.05, 0.25, 0.5, 0.75, 0.95]) if off.size else np.zeros(5)
            self.closed[tb] = {
                "actors_with_rows": int(len(act)),
                "unscored_pair_share": nan_share,
                "score_quantiles_5_25_50_75_95": [float(x) for x in q],
                "by_power": {},
            }
            al = self.alpha[act]
            sub_nc = np.clip(sub / np.sqrt(np.minimum.outer(al, al)), 0.0, 1.0)
            np.fill_diagonal(sub_nc, 1.0)
            if self.k == 0:
                # the pool's rows by the row runner's confidence (his chances)
                bands = (
                    (0.0, 1 / 3, "under 25 chances"),
                    (1 / 3, 0.5, "25 to 50"),
                    (0.5, 0.75, "50 to 150"),
                    (0.75, 1.01, "150 and over"),
                )
                by_conf = []
                for lo, hi, lab in bands:
                    m = (al >= lo) & (al < hi)
                    by_conf.append(
                        {
                            "band": lab,
                            "share_of_rows": float(n[m].sum() / n.sum()),
                            "share_of_attempts": float(att[m].sum() / att.sum()),
                            "per_100": float(100.0 * att[m].sum() / max(n[m].sum(), 1e-12)),
                        }
                    )
                self.closed[tb]["by_confidence"] = by_conf
            for p in BAL_POWERS:
                k = kernel(sub, p)
                knc = kernel(sub_nc, p)
                d1 = density_weight(k, n)
                db, miss, it = balance_weight(k, n)
                dnc, _miss_nc, _it_nc = balance_weight(knc, n)
                for kind, dv in (("dens", d1), ("bal", db), ("ncbal", dnc)):
                    full = np.ones(size, dtype=np.float64)
                    full[act] = dv
                    self.d[(tb, kind, p)] = full
                row: dict[str, Any] = {
                    "volume_kept_prod": replay_ratio(k, n, att, None),
                    "volume_kept_density": replay_ratio(k, n, att, d1),
                    "volume_kept_balanced": replay_ratio(k, n, att, db),
                    "volume_kept_noconf": replay_ratio(knc, n, att, None),
                    "volume_kept_noconf_balanced": replay_ratio(knc, n, att, dnc),
                    "balance_miss": miss,
                    "balance_iterations": it,
                    "balanced_weight_quantiles_1_50_99": [
                        float(x) for x in np.quantile(db, [0.01, 0.5, 0.99])
                    ],
                }
                # the same read with ONE season's actors live (what a simulated
                # game is: the live players are the current season's), over the
                # rows that season can draw (the point-in-time cutoff hides
                # every later season)
                row["one_season_live"] = {}
                col_season = self.col_season[act]
                for season in LIVE_SEASONS:
                    lv = col_season == season
                    vis = col_season <= season
                    if not lv.any():
                        continue
                    kl = k[np.ix_(lv, vis)]
                    nv, av = n[vis], att[vis]
                    ds, miss_s, it_s = balance_weight_live(kl, nv, n[lv])
                    full = np.ones(size, dtype=np.float64)
                    full[act[vis]] = ds
                    self.d[(tb, f"sbal{season}", p)] = full
                    row["one_season_live"][str(season)] = {
                        "volume_kept_prod": replay_ratio_live(kl, nv, av, n[lv], None),
                        "volume_kept_balanced": replay_ratio_live(kl, nv, av, n[lv], db[vis]),
                        "volume_kept_season_balanced": replay_ratio_live(kl, nv, av, n[lv], ds),
                        "balance_miss": miss_s,
                        "balance_iterations": it_s,
                        "weight_quantiles_1_50_99": [
                            float(x) for x in np.quantile(ds, [0.01, 0.5, 0.99])
                        ],
                    }
                if tiers_by_col is not None and self.k == 0:
                    grp = tiers_by_col[tb][act]
                    row["received_over_held_by_tier_prod"] = [
                        float(x) for x in received_by_group(k, n, None, grp, 4)
                    ]
                    row["received_over_held_by_tier_noconf"] = [
                        float(x) for x in received_by_group(knc, n, None, grp, 4)
                    ]
                self.closed[tb]["by_power"][f"{p:g}"] = row

    def raw(self, tb: str, rows: np.ndarray, live_key: str | None) -> dict[str, Any] | None:
        """The live actor's raw scores on the draw's rows, or None when the
        live actor has no score (the factor is then neutral, as in production)."""
        if live_key is None:
            return None
        li = self.index.get(live_key)
        if li is None:
            return None
        cols = self.cols[tb][rows]
        valid = cols >= 0
        sc = np.full(rows.size, np.nan, dtype=np.float64)
        sc[valid] = self.mat[li, cols[valid]]
        return {"cols": cols, "valid": valid, "sc": sc, "fin": np.isfinite(sc), "live": li}

    def weight(self, tb: str, raw: dict[str, Any], kind: str, p: float) -> np.ndarray:
        valid, sc, fin = raw["valid"], raw["sc"], raw["fin"]
        if kind == "prod":
            out = np.ones(sc.size, dtype=np.float64)
            s = np.where(fin[valid], sc[valid], 1.0)
            if p != 1.0:
                s = np.power(np.clip(s, 0.0, None), p)
            out[valid] = s
            if p != 1.0 and not valid.all() and valid.any():
                out[~valid] = s.mean()
            return out
        s = sc[fin]
        cf = raw["cols"][fin]
        if kind in ("noconf", "ncbal"):
            cap = np.sqrt(np.minimum(self.alpha[raw["live"]], self.alpha[cf]))
            s = np.where(cf == raw["live"], 1.0, np.clip(s / cap, 0.0, 1.0))
        s = np.power(np.clip(s, 0.0, None), p)
        if kind in ("dens", "bal", "ncbal"):
            s = s * self.d[(tb, kind, p)][cf]
        elif kind == "sbal":
            s = s * self.d[(tb, f"sbal{self.live_season}", p)][cf]
        out = np.full(sc.size, float(s.mean()) if s.size else 1.0, dtype=np.float64)
        out[fin] = s
        return out


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> dict[str, Any]:
    replay = importlib.import_module("sim554_running_game_replay")
    fp, data, _production = replay._build(args)
    rng = np.random.default_rng(args.seed + args.season)
    recs = replay.sample_pitches(
        data, args.season, args.games, args.min_opp, args.nonatt_every, rng
    )
    recs = [r for r in recs if r["cls"] < replay.OTHER]
    if args.limit:
        recs = recs[: args.limit]
    print(f"{len(recs)} pitches of {args.season}", flush=True)

    # the pool's own rates by season (what "no weight" should return)
    pool_rates: dict[str, Any] = {}
    for tb, pool in fp.a.steal_pools.items():
        season = np.asarray(pool.season)
        a = np.asarray(pool.attempted) > 0
        pool_rates[tb] = {
            str(int(s)): {
                "rows": int((season == s).sum()),
                "attempts": int(a[season == s].sum()),
                "per_100": float(100.0 * a[season == s].mean()),
                "mean_recency": float(np.asarray(pool.recency)[season == s].mean()),
            }
            for s in np.unique(season)
        }
        pool_rates[tb]["all"] = {
            "rows": int(pool.n),
            "attempts": int(a.sum()),
            "per_100": float(100.0 * a.mean()),
        }

    # the tier of every pool row's runner (0 = under 100 opportunity pitches)
    _btier, rtier = data.tiers()
    row_tier: dict[str, np.ndarray] = {}
    for tb, pool in fp.a.steal_pools.items():
        row_tier[tb] = np.fromiter(
            (
                rtier.get((int(a), int(s)), 0)
                for a, s in zip(pool.runner_id, pool.season, strict=True)
            ),
            dtype=np.int8,
            count=pool.n,
        )

    t0 = time.time()
    facs = [Factor(fp, k) for k in range(3)]
    # the runner matrix's columns by tier, for the closed-form read
    tiers_by_col: dict[str, np.ndarray] = {}
    for tb in fp.a.steal_pools:
        size = facs[0].mat.shape[0]
        tcol = np.zeros(size, dtype=np.int8)
        cols = facs[0].cols[tb]
        v = cols >= 0
        tcol[cols[v]] = row_tier[tb][v]
        tiers_by_col[tb] = tcol
    for f in facs:
        f.live_season = int(args.season)
        f.fit(tiers_by_col)
        print(
            f"  {f.name}: matrix {f.mat.shape[0]} x {f.mat.shape[1]}; confidence found for "
            f"{f.conf_found} keys, largest score over its cap {f.conf_violation:+.4f}; fitted in "
            f"{time.time() - t0:.0f} s",
            flush=True,
        )

    margin_d, margin_closed = fit_margin(fp)
    margin_bins = {
        tb: np.clip(np.rint(np.asarray(pool.sit)[:, 3]).astype(np.int64), -5, 5) + 5
        for tb, pool in fp.a.steal_pools.items()
    }
    sigma_prod = float(fp.steal_score_sigma)

    # the pool-level share of rows with no score, attempted against not
    unscored_pool: dict[str, Any] = {}
    for f in facs:
        unscored_pool[f.label] = {}
        for tb, pool in fp.a.steal_pools.items():
            a = np.asarray(pool.attempted) > 0
            u = f.cols[tb] < 0
            unscored_pool[f.label][tb] = {
                "rows_unscored_share": float(u.mean()),
                "attempted_rows_unscored_share": float(u[a].mean()),
                "other_rows_unscored_share": float(u[~a].mean()),
                "attempt_rate_unscored_per_100": float(100.0 * a[u].mean()) if u.any() else None,
                "attempt_rate_scored_per_100": float(100.0 * a[~u].mean()),
            }

    n = len(recs)
    nv = len(V_NAMES)
    p_att = np.full((n, nv), np.nan)
    p_safe = np.full((n, nv), np.nan)
    live_scored = np.zeros((n, 3), dtype=bool)
    # per factor: [attempted, other] x [mass, unscored mass, NaN-pair mass, scored mass, score sum]
    diag = np.zeros((3, 2, 5))
    diag_w = np.zeros(3)
    # per mass variant: [held or received] by pool-runner tier 0..3, all rows and attempted rows
    mass = np.zeros((len(MASS_VARIANTS), 4, 2))
    mass_w = 0.0
    check_max = dict.fromkeys(CHECKS, 0.0)
    saved = {name: fp.actor_power.get(name) for name, _r, _l in FACTORS}

    # The checkpoint (the host reboots often): the same pitches and the same
    # variants resume where the last save stopped.
    ckpt = Path(args.out_dir) / f"steal_weight_volume_probe_{args.season}.ckpt.npz"
    stamp = replay.fingerprint(recs) + ":" + "|".join(V_NAMES)
    done = 0
    if ckpt.exists():
        try:
            z = np.load(ckpt, allow_pickle=False)
            if str(z["stamp"]) == stamp and z["p_att"].shape == p_att.shape:
                p_att, p_safe = z["p_att"].copy(), z["p_safe"].copy()
                live_scored = z["live_scored"].copy()
                diag, diag_w = z["diag"].copy(), z["diag_w"].copy()
                mass, mass_w = z["mass"].copy(), float(z["mass_w"])
                check_max = dict(zip(CHECKS, z["check_max"].tolist(), strict=True))
                done = int(z["done"])
                print(f"  checkpoint: {done} of {n} pitches already scored", flush=True)
        except Exception as exc:  # noqa: BLE001 — a bad checkpoint is a fresh start
            print(f"  the checkpoint could not be read ({exc}); starting over", flush=True)

    def save_ckpt(d: int) -> None:
        tmp = ckpt.with_suffix(".tmp.npz")
        np.savez(
            tmp,
            stamp=stamp,
            done=d,
            p_att=p_att,
            p_safe=p_safe,
            live_scored=live_scored,
            diag=diag,
            diag_w=diag_w,
            mass=mass,
            mass_w=mass_w,
            check_max=np.array([check_max[c] for c in CHECKS]),
        )
        os.replace(tmp, ckpt)

    t0 = time.time()
    try:
        for i, r in enumerate(recs):
            if i < done:
                continue
            tb = str(r["target"])
            season = r["season"]
            keys = (
                f"{r['runner']}:{season}",
                f"{r['pitcher']}:{season}",
                f"{r['catcher']}:{season}" if r["catcher"] > 0 else None,
            )
            fp.set_asof(replay._prev_day(r["ymd"]))
            kw = {
                "outs": r["base_out"][0],
                "balls": r["balls"],
                "strikes": r["strikes"],
                "score_diff": r["base_out"][3],
                "aggression": 1.0,
            }
            for name, _rk, _l in FACTORS:
                fp.actor_power[name] = 0.0
            got = fp.steal_weights(r["target"], keys[0], keys[1], keys[2], **kw)
            if got is None:
                continue
            pool, rows, base = got
            base = np.asarray(base, dtype=np.float64)
            tot = float(base.sum())
            if not np.isfinite(tot) or tot <= 0.0:
                continue
            att = np.asarray(pool.attempted)[rows] > 0
            safe = (np.asarray(pool.success)[rows] > 0) & att
            # the same jar with the margin curve off, and with it balanced
            fp.steal_score_sigma = 1e9
            try:
                got0 = fp.steal_weights(r["target"], keys[0], keys[1], keys[2], **kw)
            finally:
                fp.steal_score_sigma = sigma_prod
            assert got0 is not None
            cell = (int(kw["outs"]), int(kw["balls"]), int(kw["strikes"]))
            bases = {
                "kernel": base,
                "flat": np.asarray(got0[2], dtype=np.float64),
                "kbal": base * margin_d[tb][cell][margin_bins[tb][rows]],
            }
            raws = [facs[k].raw(tb, rows, keys[k]) for k in range(3)]
            live_scored[i] = [x is not None for x in raws]
            wt = float(r["weight"])

            cache: dict[tuple[int, str, float], np.ndarray] = {}

            def fw(
                k: int,
                kind: str,
                p: float,
                _cache: dict = cache,
                _raws: list = raws,
                _tb: str = tb,
            ) -> np.ndarray | None:
                if _raws[k] is None:
                    return None
                key = (k, kind, p)
                if key not in _cache:
                    _cache[key] = facs[k].weight(_tb, _raws[k], kind, p)
                return _cache[key]

            m_base = base / tot
            for vi, vname in enumerate(V_NAMES):
                w = bases[BASE_OF[vname]]
                for k, kind, p in VARIANTS[vname]:
                    f = fw(k, kind, p)
                    if f is not None:
                        w = w * f
                wt_tot = float(w.sum())
                if wt_tot <= 0.0 or not np.isfinite(wt_tot):
                    continue
                p_att[i, vi] = float(w[att].sum()) / wt_tot
                p_safe[i, vi] = float(w[safe].sum()) / wt_tot
                if vname in MASS_VARIANTS:
                    mi = MASS_VARIANTS.index(vname)
                    tr = row_tier[tb][rows]
                    wn = w / wt_tot
                    mass[mi, :, 0] += wt * np.bincount(tr, weights=wn, minlength=4)[:4]
                    mass[mi, :, 1] += wt * np.bincount(tr[att], weights=wn[att], minlength=4)[:4]
            mass_w += wt

            for k in range(3):
                raw = raws[k]
                if raw is None:
                    continue
                diag_w[k] += wt
                for gi, g in enumerate((att, ~att)):
                    uns = g & ~raw["valid"]
                    nanp = g & raw["valid"] & ~raw["fin"]
                    sc = g & raw["fin"]
                    diag[k, gi, 0] += wt * m_base[g].sum()
                    diag[k, gi, 1] += wt * m_base[uns].sum()
                    diag[k, gi, 2] += wt * m_base[nanp].sum()
                    diag[k, gi, 3] += wt * m_base[sc].sum()
                    diag[k, gi, 4] += wt * (m_base[sc] * raw["sc"][sc]).sum()

            if i < args.check:
                for cname, level in CHECKS.items():
                    for (name, _rk, _l), pw in zip(FACTORS, level, strict=True):
                        fp.actor_power[name] = float(pw)
                    g2 = fp.steal_weights(r["target"], keys[0], keys[1], keys[2], **kw)
                    assert g2 is not None
                    w2 = np.asarray(g2[2], dtype=np.float64)
                    ref = float(w2[att].sum()) / float(w2.sum())
                    check_max[cname] = max(check_max[cname], abs(ref - p_att[i, V_IDX[cname]]))

            if (i + 1) % args.ckpt_every == 0:
                save_ckpt(i + 1)
            if (i + 1) % args.progress_every == 0:
                el = time.time() - t0
                print(
                    f"  {i + 1}/{n}  {el:6.0f} s  {1000.0 * el / (i + 1 - done):5.1f} ms/pitch",
                    flush=True,
                )
    finally:
        for name, pw in saved.items():
            if pw is None:
                fp.actor_power.pop(name, None)
            else:
                fp.actor_power[name] = pw

    arrs = {
        "p_att": p_att,
        "p_safe": p_safe,
        "weight": np.array([r["weight"] for r in recs], dtype=np.float64),
        "s": np.array([r["s"] for r in recs], dtype=np.int8),
        "runner": np.array([r["runner"] for r in recs], dtype=np.int64),
        "group": np.array([r["group"] for r in recs], dtype=np.int32),
        "target": np.array([r["target"] for r in recs], dtype=np.int8),
        "rtier": np.array([r["rtier"] for r in recs], dtype=np.int8),
        "live_scored": live_scored,
    }
    out_dir = Path(args.out_dir)
    np.savez_compressed(
        out_dir / f"steal_weight_volume_probe_{args.season}.npz",
        variants=np.array(V_NAMES),
        **arrs,
    )
    side = {
        "season": args.season,
        "pitches": n,
        "steal_score_sigma": float(fp.steal_score_sigma),
        "pool_rates": pool_rates,
        "margin_closed_form": margin_closed,
        "closed_form": {f.label: f.closed for f in facs},
        "confidence": {
            f.label: {"keys_found": f.conf_found, "largest_score_over_cap": f.conf_violation}
            for f in facs
        },
        "unscored_pool": unscored_pool,
        "diag": diag.tolist(),
        "diag_weight": diag_w.tolist(),
        "mass": mass.tolist(),
        "mass_weight": mass_w,
        "check_max_abs_gap": check_max,
        "checked_pitches": int(min(args.check, n)),
    }
    return {"arrs": arrs, "side": side}


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _runner_read(wt: np.ndarray, y: np.ndarray, ps: np.ndarray, runner: np.ndarray) -> dict:
    _u, inv = np.unique(runner, return_inverse=True)
    n = np.bincount(inv, weights=wt)
    real = np.bincount(inv, weights=wt * y)
    pred = np.bincount(inv, weights=wt * ps)
    big = n >= 150
    rr, pr = real[big] / n[big], pred[big] / n[big]
    if rr.size < 10 or rr.std() == 0 or pr.std() == 0:
        return {"runners": int(rr.size), "correlation": None, "slope": None, "mae_per_100": None}
    return {
        "runners": int(rr.size),
        "correlation": float(np.corrcoef(rr, pr)[0, 1]),
        "slope": float(np.polyfit(pr, rr, 1)[0]),
        "mae_per_100": float(100.0 * np.abs(rr - pr).mean()),
    }


def report(res: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    a, side = res["arrs"], res["side"]
    rng = np.random.default_rng(args.seed)
    keep = np.isfinite(a["p_att"]).all(axis=1)
    wt = a["weight"][keep]
    s = a["s"][keep]
    y = (s > 0).astype(np.float64)
    y3 = np.eye(3)[s]
    tw = float(wt.sum())
    grp = a["group"][keep]
    ug, ginv = np.unique(grp, return_inverse=True)
    gden = np.bincount(ginv, weights=wt)
    picks = [rng.integers(0, ug.size, size=ug.size) for _ in range(400)]

    def brier(vi: int) -> np.ndarray:
        pa, psf = a["p_att"][keep, vi], a["p_safe"][keep, vi]
        p3 = np.stack([1.0 - pa, psf, pa - psf], axis=1)
        return ((p3 - y3) ** 2).sum(axis=1)

    base_b = brier(V_IDX["prod 1/1/1"])
    out: dict[str, Any] = {
        "pitches": int(keep.sum()),
        "steals": int((s > 0).sum()),
        "real_per_100": float(100.0 * (wt * y).sum() / tw),
        "variants": {},
    }
    for tbv in (2, 3):
        m = a["target"][keep] == tbv
        out[f"real_per_100_target_{tbv}"] = float(100.0 * (wt[m] * y[m]).sum() / wt[m].sum())
    for vi, vname in enumerate(V_NAMES):
        pa = a["p_att"][keep, vi]
        b = brier(vi)
        num = np.bincount(ginv, weights=wt * (b - base_b))
        boots = np.array([num[p].sum() / gden[p].sum() for p in picks])
        v: dict[str, Any] = {
            "per_100": float(100.0 * (wt * pa).sum() / tw),
            "safe_share": float((wt * a["p_safe"][keep, vi]).sum() / max((wt * pa).sum(), 1e-12)),
            "brier_x1e4": float(1e4 * (wt * b).sum() / tw),
            "brier_vs_prod111_x1e4": float(1e4 * num.sum() / gden.sum()),
            "brier_vs_prod111_se_x1e4": float(1e4 * boots.std(ddof=1)),
            "runner": _runner_read(wt, y, pa, a["runner"][keep]),
            "tiers": {},
        }
        for tbv in (2, 3):
            m = a["target"][keep] == tbv
            v[f"per_100_target_{tbv}"] = float(100.0 * (wt[m] * pa[m]).sum() / wt[m].sum())
        for t in (0, 1, 2, 3):
            m = a["rtier"][keep] == t
            if m.any():
                v["tiers"][str(t)] = {
                    "share_of_pitches": float(wt[m].sum() / tw),
                    "real": float(100.0 * (wt[m] * y[m]).sum() / wt[m].sum()),
                    "predicted": float(100.0 * (wt[m] * pa[m]).sum() / wt[m].sum()),
                }
        out["variants"][vname] = v
    out["real_safe_share"] = float((s[s > 0] == 1).mean())
    out["live_scored_share"] = {
        FACTORS[k][2]: float((wt * a["live_scored"][keep, k]).sum() / tw) for k in range(3)
    }

    # --- print ---------------------------------------------------------------
    lines: list[str] = []
    pr = lines.append
    off = out["variants"]["off"]["per_100"]
    pr(
        f"THE STEAL DRAW'S VOLUME — {out['pitches']} real opportunity pitches of {side['season']}, "
        f"{out['steals']} recorded steals"
    )
    pr(
        f"  real: {out['real_per_100']:.3f} per 100 pitches (second {out['real_per_100_target_2']:.3f}, "
        f"third {out['real_per_100_target_3']:.3f}); the draw with no look-alike weight: {off:.3f}"
    )
    pr(
        "  the production rule re-composed here against steal_weights itself, largest gap in the "
        f"attempt odds over {side['checked_pitches']} pitches: "
        + ", ".join(f"{k} {v:.2e}" for k, v in side["check_max_abs_gap"].items())
    )
    pr("")
    pr("1. THE POOL'S OWN RATE BY SEASON (steals per 100 opportunity pitches; mean recency weight)")
    for tb, d in side["pool_rates"].items():
        parts = [
            f"{k}: {v['per_100']:.3f}" + (f" (w {v['mean_recency']:.2f})" if k != "all" else "")
            for k, v in d.items()
        ]
        pr(f"  target {tb}: " + "  ".join(parts))
    pr("")
    pr("2. ROWS WITH NO SCORE (the pool; then the draw's own mass on the real pitches)")
    for k, (_n, _r, label) in enumerate(FACTORS):
        for tb, d in side["unscored_pool"][label].items():
            ur = d["attempt_rate_unscored_per_100"]
            pr(
                f"  {label:8s} target {tb}: unscored {100 * d['rows_unscored_share']:.2f}% of rows; "
                f"{100 * d['attempted_rows_unscored_share']:.2f}% of attempted rows, "
                f"{100 * d['other_rows_unscored_share']:.2f}% of the others; attempt rate "
                f"{'n/a' if ur is None else f'{ur:.3f}'} on unscored rows against "
                f"{d['attempt_rate_scored_per_100']:.3f} on scored"
            )
        dg = np.asarray(side["diag"])[k]
        pr(
            f"  {label:8s} in the draw (live actor scored on "
            f"{100 * out['live_scored_share'][label]:.1f}% of pitches): unscored "
            f"{100 * dg[0, 1] / max(dg[0, 0], 1e-12):.2f}% of the attempted mass, "
            f"{100 * dg[1, 1] / max(dg[1, 0], 1e-12):.2f}% of the rest; unscored pairs "
            f"{100 * dg[0, 2] / max(dg[0, 0], 1e-12):.2f}% / {100 * dg[1, 2] / max(dg[1, 0], 1e-12):.2f}%"
        )
    pr("")
    pr("3. THE MEAN SCORE ON ATTEMPTED ROWS AGAINST THE OTHER ROWS (scored rows, base-weighted)")
    for k, (_n, _r, label) in enumerate(FACTORS):
        dg = np.asarray(side["diag"])[k]
        ma = dg[0, 4] / max(dg[0, 3], 1e-12)
        mo = dg[1, 4] / max(dg[1, 3], 1e-12)
        pr(f"  {label:8s} attempted {ma:.4f}   others {mo:.4f}   ratio {ma / max(mo, 1e-12):.4f}")
    pr("")
    pr("4. THE CLOSED FORM — the pool replayed through ONE factor alone (no cells, scored rows):")
    pr("   the attempts the draw predicts over the attempts the pool holds")
    for _n, _r, label in FACTORS:
        for tb, d in side["closed_form"][label].items():
            q = d["score_quantiles_5_25_50_75_95"]
            pr(
                f"  {label:8s} target {tb}: {d['actors_with_rows']} actor-seasons; scores "
                f"5/25/50/75/95% = {q[0]:.2f}/{q[1]:.2f}/{q[2]:.2f}/{q[3]:.2f}/{q[4]:.2f}; "
                f"unscored pairs {100 * d['unscored_pair_share']:.2f}%"
            )
            for p, row in d["by_power"].items():
                bq = row["balanced_weight_quantiles_1_50_99"]
                pr(
                    f"      power {p:>2s}: prod {row['volume_kept_prod']:.3f}   noconf "
                    f"{row['volume_kept_noconf']:.3f}   density "
                    f"{row['volume_kept_density']:.3f}   balanced {row['volume_kept_balanced']:.3f}"
                    f"   noconf balanced {row['volume_kept_noconf_balanced']:.3f}"
                    f"   (balance miss {row['balance_miss']:.1e} in {row['balance_iterations']} steps;"
                    f" d 1/50/99% = {bq[0]:.2f}/{bq[1]:.2f}/{bq[2]:.2f})"
                )
                for season, o in row.get("one_season_live", {}).items():
                    oq = o["weight_quantiles_1_50_99"]
                    pr(
                        f"                only {season}'s actors live: prod "
                        f"{o['volume_kept_prod']:.3f}   balanced {o['volume_kept_balanced']:.3f}   "
                        f"season balanced {o['volume_kept_season_balanced']:.3f}   (miss "
                        f"{o['balance_miss']:.1e} in {o['balance_iterations']} steps; d 1/50/99% = "
                        f"{oq[0]:.2f}/{oq[1]:.2f}/{oq[2]:.2f})"
                    )
                if "received_over_held_by_tier_prod" in row:
                    a_ = row["received_over_held_by_tier_prod"]
                    b_ = row["received_over_held_by_tier_noconf"]
                    pr(
                        "                mass received / held by runner tier (none,1,2,3): prod "
                        + "/".join(f"{x:.3f}" for x in a_)
                        + "   noconf "
                        + "/".join(f"{x:.3f}" for x in b_)
                    )
            for b in d.get("by_confidence", []):
                pr(
                    f"      rows of runners with {b['band']:17s}: {100 * b['share_of_rows']:5.1f}% of "
                    f"rows, {100 * b['share_of_attempts']:5.1f}% of attempts, {b['per_100']:.3f} per 100"
                )
    pr(
        f"  the score-margin bell curve alone (sigma {side['steal_score_sigma']:g} runs), the pool "
        "replayed inside every count cell:"
    )
    for tb, d in side["margin_closed_form"].items():
        pr(
            f"  margin   target {tb}: prod {d['volume_kept_prod']:.3f}   balanced 1.000 by "
            "construction; by margin -5..5, share of rows % / steals per 100:"
        )
        pr(
            "      "
            + "  ".join(
                f"{100 * sh:.0f}/{r100:.2f}"
                for sh, r100 in zip(
                    d["share_of_rows_by_margin"], d["per_100_by_margin"], strict=True
                )
            )
        )
    pr("")
    pr("5. THE DRAW'S MASS BY THE POOL ROW'S RUNNER TIER, on the real pitches (1 seldom, 3 most;")
    pr("   'none' = a runner-season under 100 opportunity pitches): share of all mass, and the")
    pr("   attempted mass as steals per 100")
    mass = np.asarray(side["mass"]) / side["mass_weight"]
    for mi, vname in enumerate(MASS_VARIANTS):
        pr(
            f"  {vname:26s} "
            + "  ".join(
                f"{lab} {100 * mass[mi, t, 0]:5.2f}% -> {100 * mass[mi, t, 1]:.3f}"
                for t, lab in ((0, "none"), (1, "t1"), (2, "t2"), (3, "t3"))
            )
        )
    pr("")
    pr("6. EVERY VARIANT ON THE REAL PITCHES")
    pr(
        f"  {'variant':34s} per 100  vs real   vs off  second   third   corr  slope   t1    t2    "
        "t3   none   Brier   vs prod 1/1/1"
    )
    real = out["real_per_100"]
    for vname in V_NAMES:
        v = out["variants"][vname]
        rr = v["runner"]
        t = v["tiers"]
        corr = "  n/a" if rr["correlation"] is None else f"{rr['correlation']:+.3f}"
        slope = "  n/a" if rr["slope"] is None else f"{rr['slope']:5.2f}"
        pr(
            f"  {vname:34s} {v['per_100']:6.3f}  {100 * (v['per_100'] / real - 1):+6.1f}%  "
            f"{100 * (v['per_100'] / off - 1):+6.1f}%  "
            f"{v['per_100_target_2']:6.3f}  {v['per_100_target_3']:6.3f}  {corr}  {slope}  "
            + "  ".join(
                f"{t[str(x)]['predicted']:4.2f}" if str(x) in t else "  n/a" for x in (1, 2, 3, 0)
            )
            + f"  {v['brier_x1e4']:7.2f}  {v['brier_vs_prod111_x1e4']:+6.2f}±{v['brier_vs_prod111_se_x1e4']:.2f}"
        )
    t = out["variants"]["off"]["tiers"]
    pr(
        "  real by tier: "
        + "  ".join(
            f"{lab} {t[str(x)]['real']:.2f} ({100 * t[str(x)]['share_of_pitches']:.0f}% of pitches)"
            for x, lab in ((1, "t1"), (2, "t2"), (3, "t3"), (0, "none"))
            if str(x) in t
        )
        + f"; {out['variants']['off']['runner']['runners']} runners in the correlation"
    )
    text = "\n".join(lines) + "\n"
    print(text, end="")
    out["side"] = side
    out_dir = Path(args.out_dir)
    (out_dir / f"steal_weight_volume_probe_{side['season']}.txt").write_text(text, encoding="utf-8")
    with open(
        out_dir / f"steal_weight_volume_probe_{side['season']}.json", "w", encoding="utf-8"
    ) as fh:
        json.dump(out, fh, indent=1, default=float)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--games", type=int, default=100000, help="pitcher-games sampled")
    ap.add_argument("--min-opp", type=int, default=1)
    ap.add_argument("--nonatt-every", type=int, default=3)
    ap.add_argument("--seed", type=int, default=554, help="the replay's seed: the same pitches")
    ap.add_argument("--limit", type=int, default=0, help="only the first N pitches (smoke)")
    ap.add_argument("--check", type=int, default=500, help="pitches checked against steal_weights")
    ap.add_argument("--progress-every", type=int, default=5000)
    ap.add_argument("--ckpt-every", type=int, default=5000)
    ap.add_argument("--game-pk", type=int, default=744795, help="any resolvable game")
    ap.add_argument("--out-dir", default="/app/probe")
    args = ap.parse_args(argv)
    res = run(args)
    report(res, args)
    ckpt = Path(args.out_dir) / f"steal_weight_volume_probe_{args.season}.ckpt.npz"
    ckpt.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
