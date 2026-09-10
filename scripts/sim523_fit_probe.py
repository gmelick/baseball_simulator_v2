"""
scripts/sim523_fit_probe.py — SIM-523 part F: the conditional-rate FIT instrument.

For N real games x --iters iterations on the configuration the environment
describes (the factory reads every SIM_* switch and power exactly as
production does), measure the sim's frequencies CONDITIONED on the live
actor against the pool's OWN frequencies for that actor's own rows — the
SIM-476 fit principle: a factor is right when the sim, conditioned on what
the factor reads, matches the pool's own conditional. Read per opportunity:

  * PITCHERS and BATTERS (the pitch and pitch-result draws): per pitch, the
    outcome mix (ball, called strike, swinging strike, foul, in play,
    hit-by-pitch) for the live actor, standardized to that actor's own
    count mix, against his own pool rows (recency-weighted). Reported by
    quintile of the actor's own rate per channel: the sim's tier spread
    against the pool's own is the fit signal (a flat factor compresses it;
    a power that is too strong over-concentrates and starves the other
    factors). A per-tier delta that is the same in every tier is
    composition (these games' opponents), not the factor.
  * RUNNERS, CATCHERS and PITCHERS in the steal draw: attempts per
    opportunity and the safe share, own rows vs the sim, by tier.
  * RUNNERS in the advancement draws: attempts per opportunity, own vs sim.
  * The FIELDING draw: the drawn event mix per BORN batted-ball class vs the
    pool's own per-class mix (and the share of draws whose row has the born
    class); the ground-ball reach rate by the live batter's sprint-speed
    tier vs the pool's own by row-batter tier.
  * The FACTOR STRENGTH (effective sample share, 100% = flat) of the
    pitcher, batter and recency factors on every 25th plate appearance —
    the ordering check (the pitcher strongest, then the batter, then
    recency).

The fielder factor's own conditional read stays in
scripts/sim476_fielder_probe.py (per-position OAA tiers); run it on the
same environment.

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -e SIM_MANAGER=1 \\
        -e SIM_PITCH_RESULT_SPLIT=1 -e SIM_PITCH_PITCHER_POWER=2 \\
        -e SIM_RESULT_PITCHER_POWER=2 -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim523_fit_probe.py --iters 60 \\
        --json-out /app/scripts/sim523_fit_p2.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
for p in (str(_ROOT), str(_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np  # noqa: E402
from sim_stats import (  # noqa: E402
    _FACTORY,
    _game_summary,
    _resolve,
    open_sim_duckdb,
    sim_kwargs_from_state,
)

from simulation.batch_runner import GameSpec  # noqa: E402
from simulation.production_factory import production_machine_factory  # noqa: E402
from simulation.sim_loop import BoxScore, simulate_game  # noqa: E402

#: The lane's twelve games (tests/acceptance/bands.py BALANCED_GAME_ORDER).
_DEFAULT_GAME_PKS = (
    745199,
    746494,
    745036,
    745444,
    745280,
    746560,
    745118,
    746088,
    744795,
    745521,
    746331,
    745441,
)
OUTCOMES = ("ball", "called_strike", "swinging_strike", "foul", "in_play", "hit_by_pitch")
_OUT_IDX = {o: i for i, o in enumerate(OUTCOMES)}
N_OUT = len(OUTCOMES) + 1  # + other
CHANNELS = {
    "whiff": "swinging_strike",
    "ball": "ball",
    "in_play": "in_play",
    "called": "called_strike",
}
N_TIERS = 5
ESS_EVERY = 25
_EVENT_CLASSES = ("out", "single", "double", "triple", "home_run", "error")
_REACH_IDX = (1, 2, 3, 5)
_SPEED_TIERS = ("low", "mid", "high", "unknown")


def _event_class(ev: str) -> str:
    if ev in ("single", "double", "triple", "home_run"):
        return ev
    if ev == "field_error":
        return "error"
    return "out"


def _ess_share(w: np.ndarray) -> float:
    w = np.asarray(w, dtype=np.float64)
    s = float(w.sum())
    return float(s * s / float(w @ w) / w.size) if s > 0.0 else 0.0


def _keys_by_row(emb: dict | None) -> list[str] | None:
    if emb is None:
        return None
    keys = emb.get("keys")
    if keys is not None:
        return [str(k) for k in keys]
    return [str(k) for k, _ in sorted(emb["key_index"].items(), key=lambda kv: kv[1])]


def _count_buckets(pool: Any) -> np.ndarray:
    balls = np.clip(pool.sit[:, 0].astype(np.int64), 0, 3)
    strikes = np.clip(pool.sit[:, 1].astype(np.int64), 0, 2)
    return balls * 3 + strikes


def _outcome_codes(arr: np.ndarray) -> np.ndarray:
    return np.fromiter(
        (_OUT_IDX.get(str(o), N_OUT - 1) for o in arr), dtype=np.int64, count=len(arr)
    )


# ---------------------------------------------------------------------------
# The pool's own references
# ---------------------------------------------------------------------------


class PitchRef:
    """Own-rows outcome counts per count bucket for every pitcher-season
    (the pitcher-sim index) and batter-season (the batter embedding)."""

    def __init__(self, fp: Any) -> None:
        self.pidx = dict(fp.a.pitcher_sim_index)
        bemb = fp.a.actor_emb.get("batter") or {}
        self.bidx = dict(bemb.get("key_index", {}))
        self.pitcher = np.zeros((max(1, len(self.pidx)), 12, N_OUT))
        self.batter = np.zeros((max(1, len(self.bidx)), 12, N_OUT))
        self.pitcher_pa = np.zeros((max(1, len(self.pidx)), 4))
        self.batter_pa = np.zeros((max(1, len(self.bidx)), 4))
        for hand in fp.a.pools:
            meta = fp._pool_meta(hand)
            pool = meta["pool"]
            oc = _outcome_codes(meta["outcome"])
            cb = _count_buckets(pool)
            balls = np.clip(pool.sit[:, 0].astype(np.int64), 0, 3)
            strikes = np.clip(pool.sit[:, 1].astype(np.int64), 0, 2)
            rcy = pool.recency.astype(np.float64)
            pp = meta["pool_prof"]
            ok = pp >= 0
            np.add.at(self.pitcher, (pp[ok], cb[ok], oc[ok]), rcy[ok])
            pb = meta["pool_bat"]
            ok = pb >= 0
            np.add.at(self.batter, (pb[ok], cb[ok], oc[ok]), rcy[ok])
            # the plate-appearance ends: a strike at two strikes, a ball at three
            # balls, a hit-by-pitch, a ball in play -> (pa, k, bb, hbp) per actor
            k = np.isin(oc, (1, 2)) & (strikes == 2)
            bb_ = (oc == 0) & (balls == 3)
            hbp = oc == 5
            ip = oc == 4
            pa = k | bb_ | hbp | ip
            ends = np.column_stack([pa, k, bb_, hbp]).astype(np.float64) * rcy[:, None]
            okp = pp >= 0
            np.add.at(self.pitcher_pa, pp[okp], ends[okp])
            np.add.at(self.batter_pa, pb[ok], ends[ok])


class StealRef:
    """Own-rows attempts / successes per runner, catcher and pitcher key."""

    def __init__(self, fp: Any) -> None:
        self.by: dict[str, dict[str, np.ndarray]] = {"runner": {}, "catcher": {}, "pitcher": {}}
        embs = {"runner": "baserunner", "catcher": "catcher", "pitcher": "pitcher_steal"}
        keys = {a: _keys_by_row(fp.a.actor_emb.get(e)) for a, e in embs.items()}
        for target, pool in fp.a.steal_pools.items():
            meta = fp._steal_meta(target)
            if meta is None:
                continue
            rcy = pool.recency.astype(np.float64)
            att = pool.attempted.astype(np.float64)
            suc = pool.success.astype(np.float64) * att
            vals = np.column_stack([rcy, rcy * att, rcy * suc])
            for actor, rows_key in (
                ("runner", "runner_rows"),
                ("catcher", "catcher_rows"),
                ("pitcher", "pitcher_rows"),
            ):
                rows = meta.get(rows_key)
                kl = keys[actor]
                if rows is None or kl is None:
                    continue
                acc = np.zeros((len(kl), 3))
                ok = rows >= 0
                np.add.at(acc, rows[ok], vals[ok])
                d = self.by[actor]
                for i in np.nonzero(acc[:, 0] > 0)[0]:
                    d[kl[i]] = acc[i] + d.get(kl[i], np.zeros(3))


class AdvRef:
    """Own-rows attempts / safe per runner key over every advancement pool."""

    def __init__(self, fp: Any) -> None:
        self.by: dict[str, np.ndarray] = {}
        kl = _keys_by_row(fp.a.actor_emb.get("baserunner"))
        for key, pool in fp.a.adv_pools.items():
            meta = fp._adv_meta(key)
            if meta is None or kl is None or meta.get("runner_rows") is None:
                continue
            rows = meta["runner_rows"]
            rcy = pool.recency.astype(np.float64)
            att = pool.attempted.astype(np.float64)
            safe = pool.safe.astype(np.float64) * att
            vals = np.column_stack([rcy, rcy * att, rcy * safe])
            acc = np.zeros((len(kl), 3))
            ok = rows >= 0
            np.add.at(acc, rows[ok], vals[ok])
            for i in np.nonzero(acc[:, 0] > 0)[0]:
                self.by[kl[i]] = acc[i] + self.by.get(kl[i], np.zeros(3))


def _pool_speed_z(fp: Any, pool: Any) -> np.ndarray | None:
    """The z-scored sprint speed of each pool row's batter, read from the
    baserunner embedding (NaN where the batter has no speed); None when the
    bundle has no speed feature. The sampler's speed kernel is retired
    (SIM-523), so the probe reads the speed itself for its tier reference."""
    emb = fp.a.actor_emb.get("baserunner")
    z = fp._emb_z("baserunner")
    if emb is None or z is None:
        return None
    feats = list(emb.get("features") or [])
    if "sprint_speed" not in feats:
        return None
    col = feats.index("sprint_speed")
    ki = emb["key_index"]
    out = np.full(pool.n, np.nan, dtype=np.float64)
    for i, (b, s) in enumerate(zip(pool.batter_id.tolist(), pool.season.tolist(), strict=True)):
        j = ki.get(f"{int(b)}:{int(s)}")
        if j is not None:
            out[i] = float(z[j, col])
    return out


class BBRef:
    """The pool's per-class event mix and the ground-ball reach rate by the
    row batter's sprint-speed tercile."""

    def __init__(self, fp: Any) -> None:
        self.cls_mix: dict[int, np.ndarray] = {}
        self.speed_edges: tuple[float, float] | None = None
        self.speed_ref: dict[str, list[float]] = {}
        gb_z, gb_w, gb_r = [], [], []
        for pool in fp.a.bb_pools.values():
            rcy = pool.recency.astype(np.float64)
            evc = np.fromiter(
                (_EVENT_CLASSES.index(_event_class(str(e))) for e in pool.event),
                dtype=np.int64,
                count=pool.n,
            )
            cls = (
                pool.bb_class.astype(np.int64)
                if pool.bb_class is not None
                else np.zeros(pool.n, dtype=np.int64)
            )
            for c in np.unique(cls):
                m = cls == c
                acc = np.zeros(len(_EVENT_CLASSES))
                np.add.at(acc, evc[m], rcy[m])
                self.cls_mix[int(c)] = acc + self.cls_mix.get(int(c), 0.0)
            zs = _pool_speed_z(fp, pool)
            if zs is not None:
                m = (cls == 1) & np.isfinite(zs)
                gb_z.append(zs[m].astype(np.float64))
                gb_w.append(rcy[m])
                gb_r.append(np.isin(evc[m], _REACH_IDX).astype(np.float64))
        if gb_z:
            v = np.concatenate(gb_z)
            w = np.concatenate(gb_w)
            r = np.concatenate(gb_r)
            if v.size and w.sum() > 0:
                order = np.argsort(v)
                cum = np.cumsum(w[order])
                lo = float(v[order][min(int(np.searchsorted(cum, cum[-1] / 3.0)), v.size - 1)])
                hi = float(
                    v[order][min(int(np.searchsorted(cum, 2.0 * cum[-1] / 3.0)), v.size - 1)]
                )
                self.speed_edges = (lo, hi)
                for t, m in (("low", v < lo), ("mid", (v >= lo) & (v < hi)), ("high", v >= hi)):
                    self.speed_ref[t] = [float((w * r)[m].sum()), float(w[m].sum())]
        emb = fp.a.actor_emb.get("baserunner")
        self.z = fp._emb_z("baserunner")
        self.ki = dict(emb["key_index"]) if emb else {}
        feats = list(emb.get("features", [])) if emb else []
        self.col = feats.index("sprint_speed") if "sprint_speed" in feats else None

    def live_tier(self, batter_key: str | None) -> str:
        if self.speed_edges is None or self.col is None or self.z is None or not batter_key:
            return "unknown"
        i = self.ki.get(batter_key)
        if i is None:
            return "unknown"
        zv = float(self.z[i, self.col])
        if not np.isfinite(zv):
            return "unknown"
        lo, hi = self.speed_edges
        return "low" if zv < lo else ("mid" if zv < hi else "high")


class Refs:
    def __init__(self, fp: Any) -> None:
        t0 = time.perf_counter()
        self.pitch = PitchRef(fp)
        self.steal = StealRef(fp)
        self.adv = AdvRef(fp)
        self.bb = BBRef(fp)
        print(f"  references built in {time.perf_counter() - t0:.0f}s", flush=True)


# ---------------------------------------------------------------------------
# The sim-side recorder and the wrap-once instrumentation
# ---------------------------------------------------------------------------


class Rec:
    def __init__(self) -> None:
        self.pitch_by_pitcher: dict[str, np.ndarray] = defaultdict(lambda: np.zeros((12, N_OUT)))
        self.pitch_by_batter: dict[str, np.ndarray] = defaultdict(lambda: np.zeros((12, N_OUT)))
        self.pa_by_pitcher: dict[str, np.ndarray] = defaultdict(lambda: np.zeros(4))
        self.pa_by_batter: dict[str, np.ndarray] = defaultdict(lambda: np.zeros(4))
        self.steal: dict[str, dict[str, list[int]]] = {
            a: defaultdict(lambda: [0, 0, 0]) for a in ("runner", "catcher", "pitcher")
        }
        self.adv: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
        self.bb_class: dict[int, np.ndarray] = defaultdict(lambda: np.zeros(len(_EVENT_CLASSES)))
        self.bb_agree: dict[int, list[int]] = defaultdict(lambda: [0, 0])
        self.gb_speed: dict[str, list[int]] = {t: [0, 0] for t in _SPEED_TIERS}
        self.ess: dict[str, list[float]] = {"pitcher": [], "batter": [], "recency": []}
        self.cur_pitcher: str | None = None
        self.cur_batter: str | None = None
        self.cur_hand: str | None = None
        self.pa_counter = 0
        self.bb_batter: str | None = None
        self.bb_born: dict | None = None
        self.tier_seen: Counter = Counter()
        self.eff_rows: list[float] = []
        self.cand_rows: list[int] = []

    def sample_ess(self, fp: Any) -> None:
        hand = self.cur_hand
        if hand is None:
            return
        meta = fp._pool_meta(hand)
        fpv = fp._f_pitcher_vec
        if fpv is not None:
            p = float(fp.pitch_pitcher_power)
            f = np.power(np.clip(fpv, 0.0, None), p)
            uns = meta["prof_unscored"]
            if p != 1.0 and uns.any():
                f = f.copy()
                f[uns] = fp._neutral_mean_pitcher(hand, self.cur_pitcher or "", p)
            self.ess["pitcher"].append(_ess_share(f))
        if self.cur_batter:
            fb = fp._f_batter(hand, self.cur_batter)
            q = (
                float(fp.result_batter_power)
                if fp.pitch_result_split
                else float(fp.pitch_batter_power)
            )
            f = np.power(np.clip(fb, 0.0, None), q)
            uns = fp._batter_unscored(hand)
            if q != 1.0 and uns.any():
                f = f.copy()
                f[uns] = fp._neutral_mean_batter(hand, self.cur_batter, q)
            self.ess["batter"].append(_ess_share(f))
        self.ess["recency"].append(_ess_share(fp.a.pools[hand].recency))
        # the 0-0 candidate set: rows and effective rows (the starvation read)
        cdf = fp._bucket_cdf[0] if fp._bucket_cdf is not None else None
        if cdf is not None and len(cdf):
            w = np.diff(np.concatenate([[0.0], cdf]))
            sq = float(w @ w)
            self.eff_rows.append(float(w.sum()) ** 2 / sq if sq > 0 else 0.0)
            self.cand_rows.append(int(len(w)))


def install(fp: Any, rec: Rec, refs: Refs) -> None:
    """Wrap ONCE at the sampler seams (the cached sampler is shared across
    games — the SIM-514 stacking trap); the recorder is re-pointed per run."""
    fp._sim523_fit_rec = rec
    if getattr(fp, "_sim523_fit_installed", False):
        return
    fp._sim523_fit_installed = True

    o_nhi = fp.new_half_inning

    def new_half_inning(hand: str, pitcher_key: str, catcher_key: str | None = None) -> Any:
        r = fp._sim523_fit_rec
        r.cur_pitcher = pitcher_key
        r.cur_hand = hand
        return o_nhi(hand, pitcher_key, catcher_key=catcher_key)

    fp.new_half_inning = new_half_inning

    o_npa = fp.new_plate_appearance

    def new_plate_appearance(batter_key: str, base_out: Any, **kw: Any) -> Any:
        r = fp._sim523_fit_rec
        r.cur_batter = batter_key
        out = o_npa(batter_key, base_out, **kw)
        r.pa_counter += 1
        if r.pa_counter % ESS_EVERY == 0:
            r.sample_ess(fp)
        return out

    fp.new_plate_appearance = new_plate_appearance

    o_draw = fp.draw

    def draw(balls: int = 0, strikes: int = 0) -> Any:
        o = o_draw(balls, strikes)
        r = fp._sim523_fit_rec
        b = min(max(int(balls), 0), 3) * 3 + min(max(int(strikes), 0), 2)
        oi = _OUT_IDX.get(str(o), N_OUT - 1)
        if r.cur_pitcher:
            r.pitch_by_pitcher[r.cur_pitcher][b, oi] += 1
        if r.cur_batter:
            r.pitch_by_batter[r.cur_batter][b, oi] += 1
        end = -1
        if oi in (1, 2) and int(strikes) >= 2:
            end = 1
        elif oi == 0 and int(balls) >= 3:
            end = 2
        elif oi == 5:
            end = 3
        elif oi == 4:
            end = 0
        if end >= 0:
            for key, store in ((r.cur_pitcher, r.pa_by_pitcher), (r.cur_batter, r.pa_by_batter)):
                if key:
                    store[key][0] += 1
                    if end > 0:
                        store[key][end] += 1
        return o

    fp.draw = draw

    o_steal = fp.steal_draw

    def steal_draw(*a: Any, **kw: Any) -> Any:
        res = o_steal(*a, **kw)
        if res is not None:
            r = fp._sim523_fit_rec
            att = bool(res[0])
            succ = att and bool(res[1])
            names = ("target_base", "runner_key", "pitcher_key", "catcher_key")
            got = {n: (a[i] if i < len(a) else kw.get(n)) for i, n in enumerate(names)}
            for actor, key in (
                ("runner", got["runner_key"]),
                ("catcher", got["catcher_key"]),
                ("pitcher", got["pitcher_key"]),
            ):
                if key:
                    t = r.steal[actor][str(key)]
                    t[0] += 1
                    t[1] += int(att)
                    t[2] += int(succ)
        return res

    fp.steal_draw = steal_draw

    o_adv = fp.advancement_draw

    def advancement_draw(*a: Any, **kw: Any) -> Any:
        res = o_adv(*a, **kw)
        if res is not None:
            runner_key = a[3] if len(a) > 3 else kw.get("runner_key")
            if runner_key:
                t = fp._sim523_fit_rec.adv[str(runner_key)]
                t[0] += 1
                t[1] += int(bool(res[0]))
                t[2] += int(bool(res[0]) and bool(res[1]))
        return res

    fp.advancement_draw = advancement_draw

    o_bnpa = fp.battedball_new_pa

    def battedball_new_pa(*a: Any, **kw: Any) -> Any:
        r = fp._sim523_fit_rec
        r.bb_batter = str(a[1] if len(a) > 1 else kw.get("batter_key"))
        born = kw.get("born_bb")
        if born is None and len(a) > 9:
            born = a[9]
        r.bb_born = born
        return o_bnpa(*a, **kw)

    fp.battedball_new_pa = battedball_new_pa

    o_bdraw = fp.battedball_draw

    def battedball_draw(*a: Any, **kw: Any) -> Any:
        res = o_bdraw(*a, **kw)
        r = fp._sim523_fit_rec
        i = fp._bb_last_i
        hand = fp._bb_hand
        if i is not None and hand is not None:
            pool = fp.a.bb_pools[hand]
            ev = _event_class(str(res[0]))
            ei = _EVENT_CLASSES.index(ev)
            born = r.bb_born if r.bb_born is not None else fp.last_born_batted_ball()
            born_cls = int((born or {}).get("cls") or 0)
            r.bb_class[born_cls][ei] += 1
            drawn_cls = int(pool.bb_class[i]) if pool.bb_class is not None else 0
            ag = r.bb_agree[born_cls]
            ag[0] += 1
            ag[1] += int(drawn_cls == born_cls)
            tier = refs.bb.live_tier(r.bb_batter)
            r.tier_seen[tier] += 1
            if drawn_cls == 1:
                g = r.gb_speed[tier]
                g[0] += 1
                g[1] += int(ei in _REACH_IDX)
        return res

    fp.battedball_draw = battedball_draw


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def _tiers(rows: list[tuple], weight_i: int, key_fn: Any, n_tiers: int = N_TIERS) -> list[list]:
    """Split ``rows`` (sorted by ``key_fn``) into ``n_tiers`` groups of
    equal sim weight."""
    arr = sorted(rows, key=key_fn)
    n = np.array([float(t[weight_i]) for t in arr])
    if n.size == 0 or n.sum() <= 0:
        return []
    cum = np.cumsum(n)
    edges = np.searchsorted(cum, cum[-1] * np.arange(1, n_tiers) / n_tiers)
    return [list(g) for g in np.split(np.asarray(arr, dtype=object), edges) if len(g)]


def pitch_report(
    sim: dict[str, np.ndarray], ref_mat: np.ndarray, ref_index: dict[str, int]
) -> dict[str, Any]:
    rows = []
    for key, S in sim.items():
        i = ref_index.get(key)
        if i is None or S.sum() < 100:
            continue
        R = ref_mat[i]
        if R.sum() < 200:
            continue
        share_ref = R.sum(axis=1) / R.sum()
        present = S.sum(axis=1) > 0
        sh = share_ref * present
        sh = sh / sh.sum() if sh.sum() > 0 else sh
        rates_b = S / np.maximum(S.sum(axis=1, keepdims=True), 1.0)
        sim_std = sh @ rates_b
        own = R.sum(axis=0) / R.sum()
        rows.append((key, float(S.sum()), float(R.sum()), sim_std, own))
    out: dict[str, Any] = {"n_actors": len(rows)}
    for ch, oc in CHANNELS.items():
        oi = _OUT_IDX[oc]
        table = []
        for t_i, grp in enumerate(_tiers(rows, 1, lambda t, oi=oi: t[4][oi])):
            nn = np.array([float(t[1]) for t in grp])
            sim_v = np.array([float(t[3][oi]) for t in grp])
            own_v = np.array([float(t[4][oi]) for t in grp])
            table.append(
                {
                    "tier": t_i,
                    "actors": len(grp),
                    "pitches": float(nn.sum()),
                    "sim": float(np.average(sim_v, weights=nn)),
                    "own": float(np.average(own_v, weights=nn)),
                }
            )
        if not table:
            continue
        s_sim = table[-1]["sim"] - table[0]["sim"]
        s_own = table[-1]["own"] - table[0]["own"]
        deltas = np.array([t["sim"] - t["own"] for t in table])
        out[ch] = {
            "tiers": table,
            "spread_sim": s_sim,
            "spread_own": s_own,
            "spread_ratio": (s_sim / s_own) if s_own else None,
            "mean_delta": float(deltas.mean()),
            "tier_residual": float(np.abs(deltas - deltas.mean()).mean()),
        }
    return out


def pa_report(sim: dict[str, np.ndarray], ref: np.ndarray, index: dict[str, int]) -> dict:
    """Strikeouts and walks per plate appearance: the sim's level against the
    ACTOR-MATCHED expectation (each live actor's own rate, weighted by his sim
    plate appearances) — the composition read the pool-total grade lacks."""
    rows = []
    for key, s in sim.items():
        i = index.get(key)
        if i is None or s[0] < 30 or ref[i][0] < 100:
            continue
        rows.append(
            (
                key,
                float(s[0]),
                s[1] / s[0],
                s[2] / s[0],
                ref[i][1] / ref[i][0],
                ref[i][2] / ref[i][0],
            )
        )
    if not rows:
        return {}
    pa = np.array([r[1] for r in rows])
    out = {"n_actors": len(rows), "pas": float(pa.sum())}
    for name, si, oi in (("k_pa", 2, 4), ("bb_pa", 3, 5)):
        sim_v = np.array([r[si] for r in rows])
        own_v = np.array([r[oi] for r in rows])
        tiers = []
        for t_i, grp in enumerate(_tiers(rows, 1, lambda t, oi=oi: t[oi])):
            nn = np.array([t[1] for t in grp])
            tiers.append(
                {
                    "tier": t_i,
                    "actors": len(grp),
                    "pas": float(nn.sum()),
                    "sim": float(np.average([t[si] for t in grp], weights=nn)),
                    "own": float(np.average([t[oi] for t in grp], weights=nn)),
                }
            )
        out[name] = {
            "sim": float(np.average(sim_v, weights=pa)),
            "own_matched": float(np.average(own_v, weights=pa)),
            "tiers": tiers,
        }
    return out


def rate_report(sim: dict[str, list[int]], ref: dict[str, np.ndarray], label: str) -> dict:
    """Attempt rate (and the success share among attempts) by tier of the
    actor's own attempt rate; sim vs own rows."""
    rows = []
    for key, (n, att, suc) in sim.items():
        r = ref.get(key)
        if r is None or n < 30 or r[0] < 30:
            continue
        rows.append(
            (
                key,
                float(n),
                att / n,
                (suc / att) if att else np.nan,
                r[1] / r[0],
                (r[2] / r[1]) if r[1] else np.nan,
            )
        )
    table = []
    for t_i, grp in enumerate(_tiers(rows, 1, lambda t: t[4], 3)):
        nn = np.array([t[1] for t in grp])
        table.append(
            {
                "tier": t_i,
                "actors": len(grp),
                "opps": float(nn.sum()),
                "sim_att": float(np.average([t[2] for t in grp], weights=nn)),
                "own_att": float(np.average([t[4] for t in grp], weights=nn)),
                "sim_safe": float(
                    np.nansum([t[3] * t[1] for t in grp])
                    / max(1e-9, np.sum([t[1] for t in grp if np.isfinite(t[3])]))
                ),
                "own_safe": float(
                    np.nansum([t[5] * t[1] for t in grp])
                    / max(1e-9, np.sum([t[1] for t in grp if np.isfinite(t[5])]))
                ),
            }
        )
    tot_n = sum(t[1] for t in rows)
    return {
        "label": label,
        "n_actors": len(rows),
        "sim_att_all": (sum(t[1] * t[2] for t in rows) / tot_n) if tot_n else None,
        "own_att_all": (sum(t[1] * t[4] for t in rows) / tot_n) if tot_n else None,
        "tiers": table,
    }


def bb_report(rec: Rec, refs: Refs) -> dict:
    by_class = {}
    for c, counts in sorted(rec.bb_class.items()):
        n = counts.sum()
        ref = refs.bb.cls_mix.get(int(c))
        by_class[int(c)] = {
            "n": float(n),
            "sim": {e: float(counts[i] / n) for i, e in enumerate(_EVENT_CLASSES)} if n else {},
            "own": (
                {e: float(ref[i] / ref.sum()) for i, e in enumerate(_EVENT_CLASSES)}
                if ref is not None and ref.sum() > 0
                else {}
            ),
            "agree": (rec.bb_agree[c][1] / rec.bb_agree[c][0]) if rec.bb_agree[c][0] else None,
        }
    speed = {}
    for t in _SPEED_TIERS:
        n, reach = rec.gb_speed[t]
        ref = refs.bb.speed_ref.get(t)
        speed[t] = {
            "n": n,
            "sim_reach": (reach / n) if n else None,
            "own_reach": (ref[0] / ref[1]) if ref and ref[1] > 0 else None,
        }
    return {
        "by_born_class": by_class,
        "gb_reach_by_speed": speed,
        "speed_edges": refs.bb.speed_edges,
        "tier_seen": dict(rec.tier_seen),
    }


def _fmt_pitch(name: str, rep: dict) -> None:
    print(f"  --- {name}: {rep.get('n_actors', 0)} actors with >=100 sim pitches and own rows")
    for ch in CHANNELS:
        r = rep.get(ch)
        if not r:
            continue
        ratio = r["spread_ratio"]
        print(
            f"    {ch:>8}: spread sim {r['spread_sim']:+.4f} vs own {r['spread_own']:+.4f} "
            f"(ratio {ratio if ratio is None else round(ratio, 3)}); mean delta {r['mean_delta']:+.4f}; "
            f"tier residual {r['tier_residual']:.4f}"
        )
        for t in r["tiers"]:
            print(
                f"      tier {t['tier']} ({t['actors']:3d} actors, {int(t['pitches']):6d} pitches): "
                f"sim {t['sim']:.4f} own {t['own']:.4f} delta {t['sim'] - t['own']:+.4f}"
            )


def _fmt_rate(rep: dict) -> None:
    print(
        f"  --- {rep['label']}: {rep['n_actors']} actors; attempt rate sim "
        f"{rep['sim_att_all'] if rep['sim_att_all'] is None else round(rep['sim_att_all'], 4)} "
        f"vs own {rep['own_att_all'] if rep['own_att_all'] is None else round(rep['own_att_all'], 4)}"
    )
    for t in rep["tiers"]:
        print(
            f"      tier {t['tier']} ({t['actors']:3d} actors, {int(t['opps']):6d} opps): att sim "
            f"{t['sim_att']:.4f} own {t['own_att']:.4f}; safe sim {t['sim_safe']:.3f} own {t['own_safe']:.3f}"
        )


def _ids(state: Any) -> tuple[set[int], set[int]]:
    home = {int(x) for x in (getattr(state, "home_lineup", []) or [])}
    away = {int(x) for x in (getattr(state, "away_lineup", []) or [])}
    if getattr(state, "home_pitcher_id", None):
        home.add(int(state.home_pitcher_id))
    if getattr(state, "away_pitcher_id", None):
        away.add(int(state.away_pitcher_id))
    return home, away


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("game_pks", type=int, nargs="*")
    ap.add_argument("--iters", type=int, default=60)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()
    game_pks = tuple(args.game_pks) or _DEFAULT_GAME_PKS
    set_path = _ROOT / "scripts" / "sim523_game_set.json"
    if not args.game_pks and set_path.exists():
        # the balanced certifying set (owner ruling 2026-09-09) when it exists
        with open(set_path, encoding="utf-8") as fh:
            game_pks = tuple(int(g["game_pk"]) for g in json.load(fh)["order"])

    config = {k: v for k, v in sorted(os.environ.items()) if k.startswith("SIM_")}
    print(
        "=== the fit probe on:",
        {
            k: v
            for k, v in config.items()
            if "POWER" in k
            or "SIGMA" in k
            or k.endswith(("SPLIT", "MATRICES", "FILTER", "ONLY", "STAGE", "DRAW", "INDEX"))
        },
    )
    duck = open_sim_duckdb()
    try:
        states = [asyncio.run(_resolve(gp, duck)) for gp in game_pks]
    finally:
        if duck is not None:
            duck.close()

    rec = Rec()
    refs: Refs | None = None
    box: list[dict] = []
    t0 = time.perf_counter()
    for gp, state in zip(game_pks, states, strict=True):
        kw = sim_kwargs_from_state(state)
        machine = production_machine_factory(
            0, GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
        )
        fp = machine.full_pool_sampler
        if refs is None:
            refs = Refs(fp)
        install(fp, rec, refs)
        home_ids, away_ids = _ids(state)
        machine._fp_pitcher_key = None
        machine._fp_pa_key = None
        for seed in range(args.iters):
            machine.boxscore = BoxScore()
            res = simulate_game(state_machine=machine, seed=seed, **kw)
            box.append(_game_summary(res, home_ids=home_ids, away_ids=away_ids))
        print(f"  game {gp} done ({time.perf_counter() - t0:.0f}s)", flush=True)
    assert refs is not None

    pitches = sum(float(m.sum()) for m in rec.pitch_by_pitcher.values())
    pas = sum(float(m[0].sum()) for m in rec.pitch_by_pitcher.values())
    print(
        f"\n=== {len(game_pks)} games x {args.iters}: {len(box)} game-sims, {int(pitches)} pitches, "
        f"{int(pas)} plate appearances, {(time.perf_counter() - t0) / max(1, len(box)):.2f} s/iter ==="
    )
    keys = ("BB", "K", "H", "HR", "R", "SB", "CS")
    means = {k: float(np.mean([b.get(k, 0) for b in box])) for k in keys}
    print(
        "  per game:",
        {k: round(v, 2) for k, v in means.items()},
        f"pitches/PA {pitches / max(1, pas):.3f}",
    )

    report: dict[str, Any] = {
        "config": config,
        "games": list(game_pks),
        "iters": args.iters,
        "box": means,
        "pitches": pitches,
        "pas": pas,
    }
    report["pitcher"] = pitch_report(rec.pitch_by_pitcher, refs.pitch.pitcher, refs.pitch.pidx)
    _fmt_pitch("PITCHERS", report["pitcher"])
    report["batter"] = pitch_report(rec.pitch_by_batter, refs.pitch.batter, refs.pitch.bidx)
    _fmt_pitch("BATTERS", report["batter"])
    for actor, sim_pa, ref_pa, idx in (
        ("pitcher", rec.pa_by_pitcher, refs.pitch.pitcher_pa, refs.pitch.pidx),
        ("batter", rec.pa_by_batter, refs.pitch.batter_pa, refs.pitch.bidx),
    ):
        rep = pa_report(sim_pa, ref_pa, idx)
        report[f"pa_{actor}"] = rep
        if rep:
            print(
                f"  --- K/PA and BB/PA by {actor} ({rep['n_actors']} actors, {int(rep['pas'])} PAs): the sim vs the ACTOR-MATCHED expectation"
            )
            for name in ("k_pa", "bb_pa"):
                r = rep[name]
                print(
                    f"      {name}: sim {r['sim']:.4f} vs own-matched {r['own_matched']:.4f} ({(r['sim'] / r['own_matched'] - 1) * 100:+.1f}%); tiers sim/own "
                    + " | ".join(f"{t['sim']:.4f}/{t['own']:.4f}" for t in r["tiers"])
                )
    for actor in ("runner", "catcher", "pitcher"):
        rep = rate_report(rec.steal[actor], refs.steal.by[actor], f"steal draw by {actor}")
        report[f"steal_{actor}"] = rep
        _fmt_rate(rep)
    rep = rate_report(rec.adv, refs.adv.by, "advancement draws by runner")
    report["adv_runner"] = rep
    _fmt_rate(rep)
    report["fielding"] = bb_report(rec, refs)
    print("  --- fielding: the drawn event mix per BORN class (sim | own), the class agreement")
    for c, r in report["fielding"]["by_born_class"].items():
        if not r["sim"]:
            continue
        line = " ".join(
            f"{e} {r['sim'][e]:.3f}|{r['own'].get(e, float('nan')):.3f}" for e in _EVENT_CLASSES
        )
        print(
            f"      class {c} (n {int(r['n'])}, agree {r['agree'] if r['agree'] is None else round(r['agree'], 3)}): {line}"
        )
    print("  --- ground-ball reach rate by the live batter's speed tier (sim vs own)")
    for t, r in report["fielding"]["gb_reach_by_speed"].items():
        print(
            f"      {t:>7}: n {r['n']:6d} sim {r['sim_reach'] if r['sim_reach'] is None else round(r['sim_reach'], 4)} own {r['own_reach'] if r['own_reach'] is None else round(r['own_reach'], 4)}"
        )
    print(
        f"      speed edges {report['fielding']['speed_edges']}; live tiers seen "
        f"{report['fielding']['tier_seen']}"
    )
    ess = {k: (float(np.median(v)) if v else None) for k, v in rec.ess.items()}
    report["ess"] = ess
    print(
        "  --- factor strength (median effective sample share; smaller = stronger):",
        {k: (None if v is None else round(v, 4)) for k, v in ess.items()},
    )
    report["candidate_rows"] = {
        "median_rows": float(np.median(rec.cand_rows)) if rec.cand_rows else None,
        "median_effective": float(np.median(rec.eff_rows)) if rec.eff_rows else None,
        "widen_counts": [int(x) for x in getattr(fp, "widen_counts", [])],
    }
    print("  --- the 0-0 candidate set:", report["candidate_rows"])
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, default=float)
        print(f"  wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("SIM_PITCH_CELL_INDEX", "1")
    sys.exit(main())
