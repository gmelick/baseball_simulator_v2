"""
scripts/sim548_offline_fit.py — SIM-548 §5.2: the OFFLINE JOINT FIT of the pitch
draws' weights against the real outcomes of held-out plays (v2, 2026-09-15,
after the instruments' review).

The idea. Replay real pitches through the simulator's own draw machinery — the
same sampler, the same cell index and widening, the same weights — without
playing games. For each real pitch: set the point-in-time cutoff to the day
before its game (its own game never scores itself), build the half-inning base
and the plate-appearance weight exactly as the loop does, read the live count's
WHOLE jar (every candidate row's weight, not one draw) as a distribution over
the six pitch outcomes, and score the real outcome. Average over the test set;
one number per weight setting; every setting of a design; the grid shows
whether the best value of one weight moves with another.

The test set is a sample of STARTS: (pitcher, game day) pairs with at least
``--start-min-pitches`` pitches, every pitch of each start (both hand pools).
That lets the fit score at three levels — the review of 2026-09-15 found the
per-pitch score alone is dominated by estimator noise and cannot see the
between-pitcher discrimination the strikeout market rewards:

  * per pitch   — the six-outcome Brier of the jar (``pitch`` = the single-draw
                  path; ``result`` = the split anchored on the real pitch;
                  ``chain`` = the split through the pitch draw's own mixture,
                  K anchors drawn from it, unbiased at every power);
  * per plate appearance — the jar's twelve count buckets run as an absorbing
                  chain to strikeout / walk / hit by pitch / in play, scored
                  four ways against the plate appearance's real end;
  * per start   — the sum of P(strikeout) over the start's plate appearances
                  against the real strikeouts: the correlation, the slope of
                  real on predicted, and the bias — the market's own quantity.

Plus per pitcher the predicted whiff-plus-called share against his season
share from rows OUTSIDE the test set (the discrimination read), the effective
share and rows of the jar (admissible rows only), and a checkpoint per setting.
``--holdout-season`` scores production and the top settings on fresh starts of
another season. ``--profile-season`` keys the pitcher and batter to another
season's profile (the leak check of the review's §3).

Fidelity rule. This script drives a ``FullPoolSampler`` built by the production
factory and reads what the sampler assembled (``_pa_rows`` / ``_bucket_cdf`` for
the pitch draw, :meth:`FullPoolSampler.result_weights` for the result draw). It
re-implements no weight.

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" app \\
        python scripts/sim548_offline_fit.py --draw both --starts 120 \\
        --checkpoint /app/scripts/sim548_offline.npz --json-out /app/scripts/sim548_offline.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
for p in (str(_ROOT), str(_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np  # noqa: E402
from sim_stats import _FACTORY, _resolve, open_sim_duckdb, sim_kwargs_from_state  # noqa: E402

from simulation.batch_runner import GameSpec  # noqa: E402
from simulation.production_factory import production_machine_factory  # noqa: E402

OUTCOMES = ("ball", "called_strike", "swinging_strike", "foul", "in_play", "hit_by_pitch")
OUT_IDX = {o: i for i, o in enumerate(OUTCOMES)}
N_OUT = len(OUTCOMES)
OTHER = N_OUT  # any outcome string outside the six
BALL, CALLED, SWING, FOUL, IN_PLAY, HBP = range(6)
#: The plate appearance's ends: strikeout, walk, in play, hit by pitch.
PA_ENDS = ("K", "BB", "IN", "HBP")
PA_K, PA_BB, PA_IN, PA_HBP = range(4)

#: The weights of the pitch draws and their search ranges (the plan's §4).
PITCH_WEIGHTS: dict[str, list[float]] = {
    "pitch_pitcher_power": [1, 2, 4, 8, 16, 32],
    "pitch_batter_power": [0.5, 1, 2, 4],
    "fatigue_tto_sigma": [0, 0.35, 0.5, 0.7, 1.0],
    "sit_sigma": [1, 2, 4],
}
RESULT_WEIGHTS: dict[str, list[float]] = {
    "result_pitch_sigma": [0.5, 0.7, 1.0, 1.4, 2.0],
    "result_pitcher_power": [4, 8, 16, 32],
    "result_batter_power": [2, 4, 8, 16],
    "result_density_power": [0, 0.5, 1.0],
}
PITCH_PAIRS = [("pitch_pitcher_power", "pitch_batter_power")]
RESULT_PAIRS = [
    ("result_pitcher_power", "result_batter_power"),
    ("result_pitch_sigma", "result_batter_power"),
    ("pitch_pitcher_power", "result_batter_power"),
]
ALL_WEIGHTS = {**PITCH_WEIGHTS, **RESULT_WEIGHTS}


def _prev_day(ymd: int) -> int:
    d = date(ymd // 10000, (ymd // 100) % 100, ymd % 100) - timedelta(days=1)
    return int(d.strftime("%Y%m%d"))


# ---------------------------------------------------------------------------
# The test set: whole starts
# ---------------------------------------------------------------------------


def _row(pool: Any, hand: str, i: int, oc_code: int) -> dict[str, Any]:
    s = np.asarray(pool.sit)[i]
    bh = getattr(pool, "bat_home", None)
    pc = getattr(pool, "pitch_count", None)
    tto = getattr(pool, "tto", None)
    side = int(bh[i]) if bh is not None else -1
    return {
        "hand": hand,
        "i": int(i),
        "pitcher_id": int(pool.pitcher_id[i]),
        "batter_id": int(pool.batter_id[i]),
        "ymd": int(pool.game_ymd[i]),
        "balls": int(min(max(s[0], 0), 3)),
        "strikes": int(min(max(s[1], 0), 2)),
        "inning": int(s[4]),
        "base_out": (int(s[2]), int(s[3]), int(s[4]), int(max(-5, min(5, s[5])))),
        "bat_home": (bool(side) if side in (0, 1) else None),
        "pitch_count": (int(pc[i]) if pc is not None else None),
        "tto": (int(tto[i]) if tto is not None else None),
        "outcome": oc_code,
    }


class TestSet:
    """Whole starts of one season: every pitch, its plate appearance, its start."""

    def __init__(self, pitches: list[dict[str, Any]], season: int):
        self.season = season
        self.pitches = pitches
        # plate appearances: (hand, pitcher, batter, ymd, inning) -> pitch indices
        self.pa_of: list[tuple] = []
        pa_rows: dict[tuple, list[int]] = defaultdict(list)
        for k, r in enumerate(pitches):
            key = (r["hand"], r["pitcher_id"], r["batter_id"], r["ymd"], r["inning"])
            self.pa_of.append(key)
            pa_rows[key].append(k)
        self.pa_rows = dict(pa_rows)
        # the plate appearance's real end and its first count
        self.pa_end: dict[tuple, int | None] = {}
        self.pa_first: dict[tuple, tuple[int, int]] = {}
        for key, idxs in self.pa_rows.items():
            end = None
            for k in idxs:
                r = pitches[k]
                o, b, s = r["outcome"], r["balls"], r["strikes"]
                if o == IN_PLAY:
                    end = PA_IN
                elif o == HBP:
                    end = PA_HBP
                elif o in (CALLED, SWING) and s == 2:
                    end = PA_K
                elif o == BALL and b == 3:
                    end = PA_BB
            self.pa_end[key] = end
            first = min(
                (
                    pitches[k]["balls"] + pitches[k]["strikes"],
                    pitches[k]["balls"],
                    pitches[k]["strikes"],
                )
                for k in idxs
            )
            self.pa_first[key] = (first[1], first[2])
        # starts: (pitcher, ymd) -> plate appearance keys
        starts: dict[tuple, set] = defaultdict(set)
        for key in self.pa_rows:
            starts[(key[1], key[3])].add(key)
        self.starts = {k: sorted(v) for k, v in starts.items()}
        self.sampled_ids = {(r["hand"], r["i"]) for r in pitches}

    def fingerprint(self) -> str:
        h = hashlib.sha256()
        for r in self.pitches:
            h.update(f"{r['hand']}:{r['i']};".encode())
        return h.hexdigest()[:16]


def sample_starts(
    fp: Any, season: int, n_starts: int, min_pitches: int, rng: np.random.Generator
) -> TestSet:
    """``n_starts`` random (pitcher, game day) pairs of ``season`` with at least
    ``min_pitches`` pitches across both hand pools, every pitch of each."""
    per_start: dict[tuple, list[tuple[str, int]]] = defaultdict(list)
    for hand, pool in fp.a.pools.items():
        ymd = getattr(pool, "game_ymd", None)
        if ymd is None:
            raise RuntimeError(
                "the pitch pool carries no game dates — rebuild the bundle (SIM-535)"
            )
        rows = np.where(np.asarray(pool.season) == season)[0]
        pid = np.asarray(pool.pitcher_id)[rows]
        ymds = np.asarray(ymd)[rows]
        for i, p_, y_ in zip(rows.tolist(), pid.tolist(), ymds.tolist(), strict=True):
            per_start[(int(p_), int(y_))].append((hand, int(i)))
    keys = sorted(k for k, v in per_start.items() if len(v) >= min_pitches)
    if not keys:
        raise RuntimeError(f"no start of {season} has {min_pitches} pitches")
    chosen = rng.choice(len(keys), size=min(n_starts, len(keys)), replace=False)
    pitches: list[dict[str, Any]] = []
    oc_by_hand = {h: np.asarray(fp.a.pools[h].outcome_type, dtype=object) for h in fp.a.pools}
    for c in sorted(chosen.tolist()):
        for hand, i in per_start[keys[c]]:
            pitches.append(
                _row(fp.a.pools[hand], hand, i, OUT_IDX.get(str(oc_by_hand[hand][i]), OTHER))
            )
    # group by (hand, pitcher, day): the half-inning base is built once per group
    pitches.sort(key=lambda r: (r["hand"], r["pitcher_id"], r["ymd"], r["inning"], r["batter_id"]))
    return TestSet(pitches, season)


def season_shares(fp: Any, ts: TestSet) -> dict[int, float]:
    """Each test pitcher's real whiff-plus-called share over his rows of the
    season OUTSIDE the test set (the discrimination read's yardstick)."""
    num: dict[int, float] = defaultdict(float)
    den: dict[int, float] = defaultdict(float)
    want = {r["pitcher_id"] for r in ts.pitches}
    for hand, pool in fp.a.pools.items():
        rows = np.where(np.asarray(pool.season) == ts.season)[0]
        pid = np.asarray(pool.pitcher_id)[rows]
        oc = np.asarray(pool.outcome_type, dtype=object)[rows].astype(str)
        strike = (oc == "called_strike") | (oc == "swinging_strike")
        for i, p_, k_ in zip(rows.tolist(), pid.tolist(), strike.tolist(), strict=True):
            if int(p_) in want and (hand, int(i)) not in ts.sampled_ids:
                num[int(p_)] += float(k_)
                den[int(p_)] += 1.0
    return {p_: num[p_] / den[p_] for p_ in num if den[p_] >= 100}


# ---------------------------------------------------------------------------
# The absorbing count chain
# ---------------------------------------------------------------------------


def pa_end_probabilities(
    shares: list[np.ndarray | None], first: tuple[int, int], fallback: list[np.ndarray]
) -> tuple[np.ndarray, int]:
    """Run the twelve count buckets' outcome shares as an absorbing chain from
    the count ``first`` to the four ends (K, BB, in play, HBP). A bucket with no
    rows takes the hand's whole-pool share for that count; returns how many
    buckets fell back."""
    memo: dict[tuple[int, int], np.ndarray] = {}
    fell = 0

    def sh(b: int, s: int) -> np.ndarray:
        nonlocal fell
        v = shares[b * 3 + s]
        if v is None:
            fell += 1
            return fallback[b * 3 + s]
        return v

    def value(b: int, s: int) -> np.ndarray:
        key = (b, s)
        if key in memo:
            return memo[key]
        x = sh(b, s)
        out = np.zeros(4)
        # in play and hit by pitch absorb; "other" counts as in play
        out[PA_IN] += x[IN_PLAY] + x[OTHER]
        out[PA_HBP] += x[HBP]
        strike = x[CALLED] + x[SWING]
        if s == 2:
            out[PA_K] += strike
        else:
            out += strike * value(b, s + 1)
        if b == 3:
            out[PA_BB] += x[BALL]
        else:
            out += x[BALL] * value(b + 1, s)
        # a foul: a strike below two, a self-loop at two
        if s < 2:
            out += x[FOUL] * value(b, s + 1)
        else:
            rest = 1.0 - x[FOUL]
            out = out / rest if rest > 1e-9 else out
        memo[key] = out
        return out

    v = value(first[0], first[1])
    tot = float(v.sum())
    return (v / tot if tot > 0 else np.full(4, 0.25)), fell


# ---------------------------------------------------------------------------
# The scoring pass
# ---------------------------------------------------------------------------


class Pass:
    """One evaluation of one weight setting over the test set."""

    def __init__(
        self,
        fp: Any,
        ts: TestSet,
        chain_k: int,
        chain_n: int,
        pa_chain_k: int,
        profile_season: int | None,
    ):
        self.fp = fp
        self.ts = ts
        self.chain_k = chain_k
        self.chain_n = chain_n
        self.pa_chain_k = pa_chain_k
        self.profile_season = profile_season
        self._oc_cache: dict[str, np.ndarray] = {}
        self._fallback: dict[str, list[np.ndarray]] = {}

    def _oc(self, hand: str) -> np.ndarray:
        arr = self._oc_cache.get(hand)
        if arr is None:
            outs = self.fp._pool_meta(hand)["outcome"]
            arr = np.fromiter(
                (OUT_IDX.get(str(o), OTHER) for o in outs), dtype=np.int64, count=len(outs)
            )
            self._oc_cache[hand] = arr
        return arr

    def _fallback_shares(self, hand: str) -> list[np.ndarray]:
        """The hand's whole-pool outcome shares per count bucket (the fallback
        for an empty bucket)."""
        fb = self._fallback.get(hand)
        if fb is None:
            meta = self.fp._pool_meta(hand)
            oc = self._oc(hand)
            fb = []
            for b in range(12):
                rows = meta["bucket_rows"][b]
                cnt = np.bincount(oc[rows], minlength=N_OUT + 1)[: N_OUT + 1].astype(float)
                fb.append(cnt / max(cnt.sum(), 1.0))
            self._fallback[hand] = fb
        return fb

    @staticmethod
    def _shares(oc_rows: np.ndarray, w: np.ndarray) -> np.ndarray | None:
        tot = float(w.sum())
        if not np.isfinite(tot) or tot <= 0.0:
            return None
        return np.bincount(oc_rows, weights=w, minlength=N_OUT + 1)[: N_OUT + 1] / tot

    @staticmethod
    def _brier(shares: np.ndarray, y: int) -> float:
        d = shares.copy()
        d[y] -= 1.0
        return float(np.sum(d * d))

    @staticmethod
    def _ess(w: np.ndarray) -> tuple[float, float, float]:
        """(the effective rows, the admissible rows, the effective share of the
        admissible rows) — rows the cutoff zeroed do not count."""
        adm = float((w > 0).sum())
        tot = float(w.sum())
        if tot <= 0.0 or adm <= 0.0:
            return 0.0, adm, 0.0
        eff = tot * tot / float(np.sum(w * w))
        return eff, adm, eff / adm

    def _result_shares(
        self,
        b: int,
        rows: np.ndarray,
        oc_rows: np.ndarray,
        anchor_gi: int,
        anchor_outcome: int | None,
    ) -> np.ndarray | None:
        wr = self.fp.result_weights(b, rows, anchor_gi)
        if wr is None:
            # the loop's fallback: the pitch row itself stands
            if anchor_outcome is None:
                return None
            one = np.zeros(N_OUT + 1)
            one[anchor_outcome] = 1.0
            return one
        return self._shares(oc_rows, np.asarray(wr, dtype=np.float64))

    def _chain_shares(
        self,
        b: int,
        rows: np.ndarray,
        oc_rows: np.ndarray,
        w: np.ndarray,
        k: int,
        rng: np.random.Generator,
    ) -> np.ndarray | None:
        """The split through the pitch draw's own mixture: ``k`` anchors drawn
        from the pitch draw's weights (unbiased at every power), each anchoring
        the result draw; the average of their result shares."""
        tot = float(w.sum())
        if tot <= 0.0 or rows.size == 0:
            return None
        p_a = w / tot
        picks = rng.choice(rows.size, size=k, replace=True, p=p_a)
        acc = np.zeros(N_OUT + 1)
        n = 0
        for j in picks:
            sj = self._result_shares(b, rows, oc_rows, int(rows[j]), int(oc_rows[j]))
            if sj is not None:
                acc += sj
                n += 1
        return acc / n if n else None

    def run(self, setting: dict[str, float], *, seed: int) -> dict[str, Any]:
        fp, ts = self.fp, self.ts
        for k_, v_ in setting.items():
            setattr(fp, k_, float(v_))
        fp.pitch_result_split = True  # the result weights need the bucket weights kept
        n = len(ts.pitches)
        rng = np.random.default_rng(seed)
        brier_pitch = np.full(n, np.nan)
        brier_result = np.full(n, np.nan)
        brier_chain = np.full(n, np.nan)
        k_pitch = np.full(n, np.nan)
        k_result = np.full(n, np.nan)
        eff_p = np.full(n, np.nan)
        eff_r = np.full(n, np.nan)
        adm = np.full(n, np.nan)
        share_p = np.full(n, np.nan)
        share_r = np.full(n, np.nan)
        pa_pred_pitch: dict[tuple, np.ndarray] = {}
        pa_pred_chain: dict[tuple, np.ndarray] = {}
        pa_fell = 0
        n_scored = 0
        chain_every = max(1, n // max(1, self.chain_n))
        last_group = None
        seen_pa: set = set()
        t0 = time.time()
        pseason = self.profile_season
        for idx, r in enumerate(ts.pitches):
            hand = r["hand"]
            season = pseason if pseason is not None else ts.season
            group = (hand, r["pitcher_id"], r["ymd"])
            if group != last_group:
                fp.set_asof(_prev_day(r["ymd"]))
                fp.new_half_inning(hand, f"{r['pitcher_id']}:{season}", None)
                last_group = group
            extra: dict[str, Any] = {}
            if r["pitch_count"] is not None and r["tto"] is not None:
                extra["pitch_count"], extra["tto"] = r["pitch_count"], r["tto"]
            if r["bat_home"] is not None:
                extra["bat_home"] = r["bat_home"]
            fp.new_plate_appearance(
                f"{r['batter_id']}:{season}", np.array(r["base_out"], dtype=np.float32), **extra
            )
            oc_all = self._oc(hand)
            meta_rows = fp._pool_meta(hand)["bucket_rows"]

            def bucket(
                bb: int, _rows: Any = meta_rows
            ) -> tuple[np.ndarray | None, np.ndarray | None]:
                cdf = fp._bucket_cdf[bb] if fp._bucket_cdf is not None else None
                if cdf is None or cdf.size == 0 or cdf[-1] <= 0:
                    return None, None
                rows_ = fp._pa_rows[bb] if fp._pa_rows is not None else _rows[bb]
                return rows_, np.diff(np.asarray(cdf, dtype=np.float64), prepend=0.0)

            # --- per pitch
            b = r["balls"] * 3 + r["strikes"]
            rows, w = bucket(b)
            if rows is None or w is None:
                continue
            oc_rows = oc_all[rows]
            y = r["outcome"]
            sp = self._shares(oc_rows, w)
            if sp is None:
                continue
            brier_pitch[idx] = self._brier(sp, y)
            k_pitch[idx] = sp[CALLED] + sp[SWING]
            eff_p[idx], adm[idx], share_p[idx] = self._ess(w)
            n_scored += 1
            sr = self._result_shares(b, rows, oc_rows, r["i"], None)
            if sr is not None:
                brier_result[idx] = self._brier(sr, y)
                k_result[idx] = sr[CALLED] + sr[SWING]
                wr = fp.result_weights(b, rows, r["i"])
                if wr is not None:
                    eff_r[idx], _a, share_r[idx] = self._ess(np.asarray(wr, dtype=np.float64))
            if idx % chain_every == 0:
                sc = self._chain_shares(b, rows, oc_rows, w, self.chain_k, rng)
                if sc is not None:
                    brier_chain[idx] = self._brier(sc, y)
            # --- per plate appearance (once per PA, on its first pitch seen)
            pa = ts.pa_of[idx]
            if pa not in seen_pa and ts.pa_end.get(pa) is not None:
                seen_pa.add(pa)
                fb = self._fallback_shares(hand)
                shares_p: list[np.ndarray | None] = []
                shares_c: list[np.ndarray | None] = []
                for bb in range(12):
                    rows_b, w_b = bucket(bb)
                    if rows_b is None or w_b is None:
                        shares_p.append(None)
                        shares_c.append(None)
                        continue
                    oc_b = oc_all[rows_b]
                    shares_p.append(self._shares(oc_b, w_b))
                    if self.pa_chain_k > 0:
                        shares_c.append(
                            self._chain_shares(bb, rows_b, oc_b, w_b, self.pa_chain_k, rng)
                        )
                    else:
                        shares_c.append(None)
                v_p, fell = pa_end_probabilities(shares_p, ts.pa_first[pa], fb)
                pa_fell += fell
                pa_pred_pitch[pa] = v_p
                if self.pa_chain_k > 0:
                    v_c, _f = pa_end_probabilities(shares_c, ts.pa_first[pa], fb)
                    pa_pred_chain[pa] = v_c
        elapsed = time.time() - t0
        return {
            "setting": {k_: float(v_) for k_, v_ in setting.items()},
            "n_scored": int(n_scored),
            "n_pa": len(pa_pred_pitch),
            "pa_buckets_fell_back": int(pa_fell),
            "seconds": round(elapsed, 1),
            "brier_pitch": _nanmean(brier_pitch),
            "brier_result": _nanmean(brier_result),
            "brier_chain": _nanmean(brier_chain),
            "n_chain": int(np.isfinite(brier_chain).sum()),
            "eff_rows_pitch": _nanmean(eff_p),
            "eff_rows_result": _nanmean(eff_r),
            "admissible_rows": _nanmean(adm),
            "ess_share_pitch": _nanmean(share_p),
            "ess_share_result": _nanmean(share_r),
            "eff_rows_pitch_p10": _nanpct(eff_p, 10),
            "eff_rows_result_p10": _nanpct(eff_r, 10),
            "_per_pitch_pitch": brier_pitch,
            "_per_pitch_result": brier_result,
            "_k_pitch": k_pitch,
            "_k_result": k_result,
            "_pa_pred_pitch": pa_pred_pitch,
            "_pa_pred_chain": pa_pred_chain,
        }


def _nanmean(a: np.ndarray) -> float | None:
    return float(np.nanmean(a)) if np.isfinite(a).any() else None


def _nanpct(a: np.ndarray, q: float) -> float | None:
    return float(np.nanpercentile(a, q)) if np.isfinite(a).any() else None


# ---------------------------------------------------------------------------
# The reads
# ---------------------------------------------------------------------------


def pa_level(ts: TestSet, pred: dict[tuple, np.ndarray]) -> dict[str, Any]:
    """The four-way Brier per plate appearance, and the strikeout share
    predicted against real."""
    if not pred:
        return {"n": 0}
    b, pk, yk = [], [], []
    for pa, v in pred.items():
        end = ts.pa_end[pa]
        y = np.zeros(4)
        y[end] = 1.0
        b.append(float(np.sum((v - y) ** 2)))
        pk.append(float(v[PA_K]))
        yk.append(1.0 if end == PA_K else 0.0)
    pk_, yk_ = np.asarray(pk), np.asarray(yk)
    return {
        "n": len(b),
        "brier4": float(np.mean(b)),
        "k_pred": float(pk_.mean()),
        "k_real": float(yk_.mean()),
        "k_brier": float(np.mean((pk_ - yk_) ** 2)),
    }


def start_level(ts: TestSet, pred: dict[tuple, np.ndarray]) -> dict[str, Any]:
    """Per start: the sum of P(strikeout) over its plate appearances against
    the real strikeouts — the correlation, the slope of real on predicted, the
    bias, and the spread ratio."""
    xs, ys = [], []
    for _start, pas in ts.starts.items():
        pas_ok = [pa for pa in pas if pa in pred]
        if len(pas_ok) < 10:
            continue
        xs.append(sum(float(pred[pa][PA_K]) for pa in pas_ok))
        ys.append(sum(1.0 for pa in pas_ok if ts.pa_end[pa] == PA_K))
    if len(xs) < 8:
        return {"n_starts": len(xs)}
    x, y = np.asarray(xs), np.asarray(ys)
    corr = float(np.corrcoef(x, y)[0, 1]) if x.std() > 0 and y.std() > 0 else float("nan")
    slope = float(np.polyfit(x, y, 1)[0]) if x.std() > 0 else float("nan")
    return {
        "n_starts": len(xs),
        "k_pred_mean": float(x.mean()),
        "k_real_mean": float(y.mean()),
        "bias": float((x - y).mean()),
        "corr": corr,
        "slope_real_on_pred": slope,
        "spread_ratio": float(x.std() / y.std()) if y.std() > 0 else float("nan"),
        "mae": float(np.abs(x - y).mean()),
    }


def discrimination(
    ts: TestSet, pred: np.ndarray, yard: dict[int, float], min_pitches: int = 30
) -> dict[str, float]:
    """Per pitcher: the mean PREDICTED whiff-plus-called share on the test
    pitches against his season share from rows outside the test set."""
    by: dict[int, list[float]] = defaultdict(list)
    for r, q in zip(ts.pitches, pred, strict=True):
        if np.isfinite(q) and r["pitcher_id"] in yard:
            by[r["pitcher_id"]].append(float(q))
    xs = [yard[p_] for p_, v in by.items() if len(v) >= min_pitches]
    ys = [float(np.mean(v)) for p_, v in by.items() if len(v) >= min_pitches]
    if len(xs) < 5:
        return {"pitchers": len(xs)}
    x, y = np.asarray(xs), np.asarray(ys)
    corr = float(np.corrcoef(x, y)[0, 1]) if x.std() > 0 and y.std() > 0 else float("nan")
    slope = float(np.polyfit(y, x, 1)[0]) if y.std() > 0 else float("nan")
    return {
        "pitchers": len(xs),
        "corr": corr,
        "slope_real_on_pred": slope,
        "spread_ratio": float(y.std() / x.std()) if x.std() > 0 else float("nan"),
    }


def _cluster_delta(
    a: np.ndarray, b: np.ndarray, groups: np.ndarray, rng: np.random.Generator, n_boot: int = 400
) -> tuple[float, float, float]:
    """mean(a − b) with a pitcher-clustered bootstrap range (NaNs dropped pairwise)."""
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() == 0:
        return float("nan"), float("nan"), float("nan")
    d = (a - b)[ok]
    g = groups[ok]
    ug, inv = np.unique(g, return_inverse=True)
    num = np.zeros(len(ug))
    den = np.zeros(len(ug))
    np.add.at(num, inv, d)
    np.add.at(den, inv, 1.0)
    idx = rng.integers(0, len(ug), size=(n_boot, len(ug)))
    boots = num[idx].sum(axis=1) / np.maximum(den[idx].sum(axis=1), 1e-12)
    return float(d.mean()), float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))


def summarize(
    results: list[dict[str, Any]],
    production: dict[str, float],
    ts: TestSet,
    yard: dict[int, float],
    rng: np.random.Generator,
) -> list[dict[str, Any]]:
    base = results[0]
    groups = np.array([r["pitcher_id"] for r in ts.pitches])
    rows_out = []
    for r in results:
        diffs = {k: v for k, v in r["setting"].items() if production.get(k) != v}
        label = (
            ", ".join(
                f"{k.replace('_power', '^').replace('_sigma', ' σ')}={v:g}"
                for k, v in diffs.items()
            )
            or "production"
        )
        dp, lo_p, hi_p = _cluster_delta(
            r["_per_pitch_pitch"], base["_per_pitch_pitch"], groups, rng
        )
        dr, lo_r, hi_r = _cluster_delta(
            r["_per_pitch_result"], base["_per_pitch_result"], groups, rng
        )
        rows_out.append(
            {
                **{k: v for k, v in r.items() if not k.startswith("_")},
                "label": label,
                "delta_pitch": dp,
                "delta_pitch_lo": lo_p,
                "delta_pitch_hi": hi_p,
                "delta_result": dr,
                "delta_result_lo": lo_r,
                "delta_result_hi": hi_r,
                "pa_pitch": pa_level(ts, r["_pa_pred_pitch"]),
                "pa_chain": pa_level(ts, r["_pa_pred_chain"]),
                "start_pitch": start_level(ts, r["_pa_pred_pitch"]),
                "start_chain": start_level(ts, r["_pa_pred_chain"]),
                "disc_pitch": discrimination(ts, r["_k_pitch"], yard),
                "disc_result": discrimination(ts, r["_k_result"], yard),
            }
        )
    return rows_out


def _g(d: dict[str, Any], k: str, fmt: str = "{:+.3f}") -> str:
    v = d.get(k)
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "   —  "
    return fmt.format(v)


def print_report(
    rows: list[dict[str, Any]], production: dict[str, float], ts: TestSet, title: str
) -> None:
    base = rows[0]
    print(f"=== SIM-548 offline joint fit — {title} ===")
    print(f"  production: {production}")
    print(
        f"  {base['n_scored']} pitches, {base['n_pa']} plate appearances, {len(ts.starts)} starts; "
        f"real K per start {_g(base['start_pitch'], 'k_real_mean', '{:.2f}')}; "
        f"real K share per PA {_g(base['pa_pitch'], 'k_real', '{:.3f}')}"
    )
    print(
        "  per pitch: the six-outcome Brier (Δ vs production, pitcher-clustered range). per PA: the "
        "four-way Brier of the count chain (K/BB/in play/HBP). per start: the sum of P(K) vs real K — "
        "corr, slope of real on predicted, bias, spread ratio. disc: per-pitcher predicted vs season "
        "whiff+called share — corr, spread ratio. eff = effective rows (and the 10th percentile) of the "
        "admissible rows."
    )
    print(
        f"  {'setting':40s} | {'pitch':>6s} {'Δ':>7s} | {'result':>6s} {'Δ':>7s} | {'chain':>6s} | "
        f"{'PA4 p':>6s} {'PA4 c':>6s} | {'start corr p/c':>14s} {'slope p/c':>11s} {'bias p/c':>11s} "
        f"{'spr p/c':>9s} | {'disc corr p/r':>13s} {'spr p/r':>9s} | {'eff p':>6s} {'p10':>5s} "
        f"{'eff r':>6s} {'p10':>5s}"
    )
    for r in rows:
        sp, sc = r["start_pitch"], r["start_chain"]
        dp, dr = r["disc_pitch"], r["disc_result"]
        print(
            f"  {r['label'][:40]:40s} | {_g(r, 'brier_pitch', '{:.4f}')} {_g(r, 'delta_pitch', '{:+.4f}')} | "
            f"{_g(r, 'brier_result', '{:.4f}')} {_g(r, 'delta_result', '{:+.4f}')} | "
            f"{_g(r, 'brier_chain', '{:.4f}')} | "
            f"{_g(r['pa_pitch'], 'brier4', '{:.4f}')} {_g(r['pa_chain'], 'brier4', '{:.4f}')} | "
            f"{_g(sp, 'corr', '{:+.2f}')}/{_g(sc, 'corr', '{:+.2f}')} "
            f"{_g(sp, 'slope_real_on_pred', '{:+.2f}')}/{_g(sc, 'slope_real_on_pred', '{:+.2f}')} "
            f"{_g(sp, 'bias', '{:+.2f}')}/{_g(sc, 'bias', '{:+.2f}')} "
            f"{_g(sp, 'spread_ratio', '{:.2f}')}/{_g(sc, 'spread_ratio', '{:.2f}')} | "
            f"{_g(dp, 'corr', '{:+.2f}')}/{_g(dr, 'corr', '{:+.2f}')} "
            f"{_g(dp, 'spread_ratio', '{:.2f}')}/{_g(dr, 'spread_ratio', '{:.2f}')} | "
            f"{_g(r, 'eff_rows_pitch', '{:6.1f}')} {_g(r, 'eff_rows_pitch_p10', '{:5.1f}')} "
            f"{_g(r, 'eff_rows_result', '{:6.1f}')} {_g(r, 'eff_rows_result_p10', '{:5.1f}')}"
        )


def interaction_read(
    rows: list[dict[str, Any]], production: dict[str, float], pairs: list[tuple[str, str]], score
) -> dict[str, Any]:
    """For each pair (A, B): the best value of A at each value of B under
    ``score`` (a function of a row; lower is better)."""
    out: dict[str, Any] = {}
    for a, b in pairs:
        table: dict[str, dict[str, float]] = {}
        for r in rows:
            s = r["setting"]
            if not all(production.get(k) == v for k, v in s.items() if k not in (a, b)):
                continue
            v = score(r)
            if v is None or not np.isfinite(v):
                continue
            table.setdefault(f"{b}={s[b]:g}", {})[f"{a}={s[a]:g}"] = float(v)
        best = {bk: min(av, key=av.get) for bk, av in table.items() if av}
        out[f"{a} x {b}"] = {"best_a_by_b": best, "table": table}
    return out


# ---------------------------------------------------------------------------
# The design and the checkpoint
# ---------------------------------------------------------------------------


def build_design(
    draw: str, production: dict[str, float], pairs_only: bool
) -> list[dict[str, float]]:
    weights = (
        PITCH_WEIGHTS if draw == "pitch" else RESULT_WEIGHTS if draw == "result" else ALL_WEIGHTS
    )
    pairs = (
        PITCH_PAIRS
        if draw == "pitch"
        else RESULT_PAIRS
        if draw == "result"
        else PITCH_PAIRS + RESULT_PAIRS
    )
    settings: list[dict[str, float]] = [dict(production)]
    seen = {tuple(sorted(production.items()))}

    def add(s: dict[str, float]) -> None:
        key = tuple(sorted(s.items()))
        if key not in seen:
            seen.add(key)
            settings.append(s)

    if not pairs_only:
        for name, values in weights.items():
            for v in values:
                s = dict(production)
                s[name] = float(v)
                add(s)
    for a, b in pairs:
        for va, vb in itertools.product(ALL_WEIGHTS[a], ALL_WEIGHTS[b]):
            s = dict(production)
            s[a], s[b] = float(va), float(vb)
            add(s)
    return settings


def _ckpt_path(p: str | None) -> Path | None:
    if not p:
        return None
    path = Path(p)
    return path if path.suffix == ".npz" else path.with_suffix(path.suffix + ".npz")


def _ckpt_meta(args: argparse.Namespace, ts: TestSet) -> dict[str, Any]:
    return {
        "season": ts.season,
        "fingerprint": ts.fingerprint(),
        "chain_k": args.chain_k,
        "chain_n": args.chain_n,
        "pa_chain_k": args.pa_chain_k,
        "profile_season": args.profile_season,
        "seed": args.seed,
    }


def run_settings(
    runner: Pass,
    settings: list[dict[str, float]],
    production: dict[str, float],
    ckpt: Path | None,
    meta: dict[str, Any],
    seed: int,
) -> list[dict[str, Any]]:
    done: dict[str, dict[str, Any]] = {}
    if ckpt is not None and ckpt.exists():
        blob = np.load(ckpt, allow_pickle=True)
        if json.loads(str(blob["meta"])) == meta:
            done = blob["results"].item()
            print(f"checkpoint: {len(done)} settings already scored", flush=True)
        else:
            print(
                "checkpoint is for another test set or another chain setting; starting over",
                flush=True,
            )
    results = []
    for k, s in enumerate(settings):
        key = json.dumps({kk: float(vv) for kk, vv in sorted(s.items())})
        if key in done:
            res = done[key]
        else:
            res = runner.run(s, seed=seed + k)
            done[key] = res
            if ckpt is not None:
                np.savez(ckpt, results=np.array(done, dtype=object), meta=json.dumps(meta))
        results.append(res)
        print(
            f"  [{k + 1}/{len(settings)}] {res['seconds']:6.1f} s  pitch {_g(res, 'brier_pitch', '{:.4f}')}  "
            f"result {_g(res, 'brier_result', '{:.4f}')}  chain {_g(res, 'brier_chain', '{:.4f}')}  "
            f"{ {kk: vv for kk, vv in s.items() if production.get(kk) != vv} or 'production' }",
            flush=True,
        )
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--draw", choices=("pitch", "result", "both"), default="both")
    ap.add_argument("--season", type=int, default=2024)
    ap.add_argument("--starts", type=int, default=120, help="starts sampled (every pitch of each)")
    ap.add_argument("--start-min-pitches", type=int, default=60)
    ap.add_argument(
        "--chain-n",
        type=int,
        default=1500,
        help="pitches scored through the per-pitch chain per setting",
    )
    ap.add_argument(
        "--chain-k",
        type=int,
        default=24,
        help="anchors drawn from the pitch draw per chained pitch",
    )
    ap.add_argument(
        "--pa-chain-k",
        type=int,
        default=6,
        help="anchors per count bucket for the plate-appearance chain (0 = off)",
    )
    ap.add_argument("--pairs-only", action="store_true")
    ap.add_argument(
        "--settings-json",
        default=None,
        help="an explicit list of settings (JSON) instead of the design",
    )
    ap.add_argument(
        "--profile-season",
        type=int,
        default=None,
        help="key the pitcher and batter to this season's profile (the leak check)",
    )
    ap.add_argument(
        "--holdout-season",
        type=int,
        default=None,
        help="score production and the top settings on fresh starts of this season",
    )
    ap.add_argument("--holdout-starts", type=int, default=80)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--seed", type=int, default=548)
    ap.add_argument(
        "--game-pk",
        type=int,
        default=744795,
        help="any resolvable game — the factory needs a state",
    )
    ap.add_argument(
        "--checkpoint", default=None, help="an .npz that saves every finished setting (resumable)"
    )
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)

    rng = np.random.default_rng(args.seed)
    duck = open_sim_duckdb()
    try:
        state = asyncio.run(_resolve(args.game_pk, duck))
    finally:
        if duck is not None:
            duck.close()
    kw = sim_kwargs_from_state(state)
    machine = production_machine_factory(0, GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw)))
    fp = machine.full_pool_sampler
    production = {k: float(getattr(fp, k)) for k in ALL_WEIGHTS}
    production_split = bool(fp.pitch_result_split)
    print(
        f"production attributes: {production} "
        f"(split {'ON' if production_split else 'OFF'}, cell index {fp.pitch_cell_index})"
    )

    t0 = time.time()
    ts = sample_starts(fp, args.season, args.starts, args.start_min_pitches, rng)
    yard = season_shares(fp, ts)
    print(
        f"{len(ts.pitches)} pitches in {len(ts.pa_rows)} plate appearances of {len(ts.starts)} starts "
        f"of {args.season}; {sum(1 for v in ts.pa_end.values() if v is not None)} plate appearances "
        f"with a known end; {len(yard)} pitchers with a season yardstick; sampled in {time.time() - t0:.1f} s"
    )
    if args.settings_json:
        settings = [
            dict(production, **{k: float(v) for k, v in s.items()})
            for s in json.load(open(args.settings_json, encoding="utf-8"))
        ]
        settings.insert(0, dict(production))
    else:
        settings = build_design(args.draw, production, args.pairs_only)
    print(f"{len(settings)} settings (production first)")

    runner = Pass(fp, ts, args.chain_k, args.chain_n, args.pa_chain_k, args.profile_season)
    ckpt = _ckpt_path(args.checkpoint)
    results = run_settings(runner, settings, production, ckpt, _ckpt_meta(args, ts), args.seed)

    print()
    rows = summarize(results, production, ts, yard, rng)
    print_report(rows, production, ts, f"the pitch draws on {args.season}")
    pairs = (
        PITCH_PAIRS
        if args.draw == "pitch"
        else RESULT_PAIRS
        if args.draw == "result"
        else PITCH_PAIRS + RESULT_PAIRS
    )
    inter = {
        "pitch_brier": interaction_read(rows, production, pairs, lambda r: r["brier_pitch"]),
        "result_brier": interaction_read(rows, production, pairs, lambda r: r["brier_result"]),
        "pa_brier4_chain": interaction_read(
            rows, production, pairs, lambda r: r["pa_chain"].get("brier4")
        ),
        "start_k_neg_corr_chain": interaction_read(
            rows, production, pairs, lambda r: -(r["start_chain"].get("corr") or float("nan"))
        ),
    }
    print()
    print("=== the interaction read: the best value of A at each value of B ===")
    for score_name, table in inter.items():
        for pair, d in table.items():
            print(
                f"  [{score_name}] {pair}: "
                + "; ".join(f"{bk} → {av}" for bk, av in d["best_a_by_b"].items())
            )

    out: dict[str, Any] = {
        "season": args.season,
        "n_pitches": len(ts.pitches),
        "n_starts": len(ts.starts),
        "production": production,
        "production_split": production_split,
        "profile_season": args.profile_season,
        "outcomes": list(OUTCOMES) + ["other"],
        "settings": rows,
        "interactions": inter,
    }

    # --- the offline hold-out on another season
    if args.holdout_season:

        def key_of(r: dict[str, Any]) -> tuple:
            c = r["start_chain"].get("corr")
            return (
                -(c if c is not None and np.isfinite(c) else -9.0),
                r["pa_chain"].get("brier4") or 9.0,
            )

        ranked = sorted(rows[1:], key=key_of)[: args.top]
        chosen = [dict(r["setting"]) for r in ranked]
        print()
        print(
            f"=== the offline hold-out on {args.holdout_season}: production + the top {len(chosen)} "
            "by the per-start strikeout read ==="
        )
        rng_h = np.random.default_rng(args.seed + 7)
        ts_h = sample_starts(
            fp, args.holdout_season, args.holdout_starts, args.start_min_pitches, rng_h
        )
        yard_h = season_shares(fp, ts_h)
        runner_h = Pass(fp, ts_h, args.chain_k, args.chain_n, args.pa_chain_k, args.profile_season)
        res_h = run_settings(
            runner_h, [dict(production)] + chosen, production, None, {}, args.seed + 7
        )
        rows_h = summarize(res_h, production, ts_h, yard_h, rng_h)
        print_report(rows_h, production, ts_h, f"the hold-out on {args.holdout_season}")
        out["holdout"] = {
            "season": args.holdout_season,
            "n_pitches": len(ts_h.pitches),
            "settings": rows_h,
        }

    # restore production
    for k, v in production.items():
        setattr(fp, k, v)
    fp.pitch_result_split = production_split
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1, default=float)
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
