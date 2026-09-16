"""
scripts/sim518_fatigue_scan.py — SIM-518: the OFFLINE fatigue-kernel scan.

The pitcher-fatigue factor weights every candidate row of the live count
sub-cell by a Gaussian on |live − row| pitch count (``SIM_FATIGUE_PC_SIGMA``)
times a Gaussian on |live − row| times through the order
(``SIM_FATIGUE_TTO_SIGMA``). This script reads what that kernel WOULD do at a
ladder of bandwidths from the pool alone — no game is simulated — because the
reference and the kernel are functions of the same pool columns.

Three reads per bandwidth pair (plan: docs/audit/2026-09-12-sim518-fit-plan.md §5.1):

  * The MARGINAL conditional (reference A): the pool's own outcome mix by
    pitch-count band and by times through the order, and the kernel's
    EXPECTED mix at each live state. At bandwidth → 0 the kernel reproduces
    A exactly; flat, it gives the sub-cell marginal.
  * The WITHIN-PITCHER conditional (reference B): each pitcher-season's mix
    per band minus his own overall mix, pooled by his rows. This is the
    fatigue effect with the "who gets to pitch deep" selection removed.
    THE FIT TARGET: the kernel's expected band deltas should match B, not
    A. A sim whose pitcher factor is nearly flat gives every pitcher the
    kernel's curve, so its within-pitcher deltas equal the kernel's
    marginal deltas — matching A would import the selection.
  * The SURVIVORSHIP read: the kernel-weighted mean of the ROW pitcher's own
    season whiff share and ball share, by live band, against the sub-cell
    marginal. A kernel that raises row-pitcher quality at high live counts
    imports selection. The owner sets the ceiling (decision point A).

The arithmetic runs on per-sub-cell histograms (pitch count 0-125 × TTO 1-4 ×
six outcomes), so a bandwidth pair costs one smoothing per hand.

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim518_fatigue_scan.py \\
        --json-out /app/scripts/sim518_fatigue_scan.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np  # noqa: E402

from simulation.filter_cells import (  # noqa: E402
    N_BAND,
    N_COUNT,
    N_OUTS,
    score_band_array,
)

OUTCOMES: tuple[str, ...] = (
    "ball",
    "called_strike",
    "swinging_strike",
    "foul",
    "in_play",
    "hit_by_pitch",
)
_OUT_IDX = {o: i for i, o in enumerate(OUTCOMES)}
N_OUT = len(OUTCOMES)
#: The channels the fit reads (the ball and whiff shares drive walks and
#: strikeouts through the count chain).
CHANNELS: dict[str, int] = {
    "ball": 0,
    "called": 1,
    "whiff": 2,
    "foul": 3,
    "in_play": 4,
}
#: Pitch counts BEFORE the plate appearance, 0..MAX_PC (the pool's max is 125).
MAX_PC = 125
N_PC = MAX_PC + 1
#: Band edges on the pitch count: 0-25 / 26-50 / 51-75 / 76-100 / 100+.
PC_BAND_EDGES: tuple[int, ...] = (26, 51, 76, 101)
PC_BANDS: tuple[str, ...] = ("0-25", "26-50", "51-75", "76-100", "100+")
N_PC_BAND = len(PC_BANDS)
#: Times through the order 1..4 (4 = 4th or later).
N_TTO = 4
TTO_LABELS: tuple[str, ...] = ("1st", "2nd", "3rd", "4th+")
#: A within-pitcher row needs this many rows in a band to count toward B.
MIN_ROWS_PER_BAND = 20
#: "flat" = the term is OFF (a bandwidth of 0 in the sampler).
FLAT = 0.0
DEFAULT_PC_SIGMAS: tuple[float, ...] = (FLAT, 10.0, 20.0, 35.0, 60.0)
DEFAULT_TTO_SIGMAS: tuple[float, ...] = (FLAT, 0.5, 1.0, 2.0)
DEFAULT_ART_DIR = os.path.join(
    os.environ.get("BASEBALL_PLAY_POOL_DIR", "/data/play_pool"), "engine_artifacts"
)


# ---------------------------------------------------------------------------
# The kernel matrices
# ---------------------------------------------------------------------------


def gaussian_matrix(n: int, sigma: float, scale: float = 1.0) -> np.ndarray:
    """``K[i, j] = exp(-((i - j) * scale)^2 / (2 sigma^2))``; all ones when the
    term is flat (sigma 0 = OFF in the sampler)."""
    if sigma <= 0.0:
        return np.ones((n, n), dtype=np.float32)
    idx = np.arange(n, dtype=np.float64) * scale
    d = idx[:, None] - idx[None, :]
    return np.exp(-(d * d) / (2.0 * sigma * sigma)).astype(np.float32)


def pc_band_of(pc: np.ndarray) -> np.ndarray:
    return np.digitize(np.asarray(pc), PC_BAND_EDGES)


# ---------------------------------------------------------------------------
# The per-sub-cell histograms
# ---------------------------------------------------------------------------


def outcome_codes(outcome_type: np.ndarray) -> np.ndarray:
    """0..5 for the six outcomes, -1 for anything else."""
    return np.fromiter(
        (_OUT_IDX.get(str(o), -1) for o in outcome_type), dtype=np.int64, count=len(outcome_type)
    )


def subcell_ids(sit: np.ndarray, bat_home: np.ndarray | None) -> np.ndarray:
    """The sampler's own sub-cell id (``FullPoolSampler._cell_meta``, the
    three-valued side axis), so the scan partitions the pool exactly the way
    the draw does."""
    rs = sit[:, 3].astype(np.int64) & 0b111
    outs = np.clip(sit[:, 2].astype(np.int64), 0, N_OUTS - 1)
    cb = np.clip(sit[:, 0].astype(np.int64), 0, 3) * 3 + np.clip(sit[:, 1].astype(np.int64), 0, 2)
    band = score_band_array(sit[:, 5])
    if bat_home is None or not bool((bat_home >= 0).any()):
        n_side = 1
        side = np.zeros(sit.shape[0], dtype=np.int64)
    else:
        n_side = 3
        side = np.where(bat_home > 0, 1, np.where(bat_home == 0, 0, 2)).astype(np.int64)
    return (((rs * N_OUTS + outs) * N_BAND + band) * n_side + side) * N_COUNT + cb


def build_histograms(
    sub: np.ndarray,
    pc: np.ndarray,
    tto: np.ndarray,
    out: np.ndarray,
    weight: np.ndarray,
    quality: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per sub-cell (compacted to the occupied ones): ``H[S, pc, tto, out]``
    (row mass), ``M[S, pc, tto]`` (total mass) and ``Q[S, pc, tto, k]``
    (mass-weighted quality sums, one per quality column)."""
    keep = (pc >= 0) & (tto >= 1) & (out >= 0)
    sub, pc, tto, out, weight = sub[keep], pc[keep], tto[keep], out[keep], weight[keep]
    quality = quality[keep]
    pc = np.clip(pc, 0, MAX_PC).astype(np.int64)
    tto = (np.clip(tto, 1, N_TTO) - 1).astype(np.int64)
    uniq, sidx = np.unique(sub, return_inverse=True)
    n_s = uniq.size
    flat = ((sidx * N_PC + pc) * N_TTO + tto) * N_OUT + out
    H = (
        np.bincount(flat, weights=weight, minlength=n_s * N_PC * N_TTO * N_OUT)
        .reshape(n_s, N_PC, N_TTO, N_OUT)
        .astype(np.float32)
    )
    M = H.sum(axis=3)
    flat_q = (sidx * N_PC + pc) * N_TTO + tto
    Q = np.stack(
        [
            np.bincount(
                flat_q, weights=weight * quality[:, k], minlength=n_s * N_PC * N_TTO
            ).reshape(n_s, N_PC, N_TTO)
            for k in range(quality.shape[1])
        ],
        axis=-1,
    ).astype(np.float32)
    return H, M, Q


def smooth(H: np.ndarray, k_pc: np.ndarray, k_tto: np.ndarray) -> np.ndarray:
    """The kernel-weighted sum at every live (pc, tto): ``E[S, c, t, ...] =
    sum_{pc', t'} k_pc[c, pc'] k_tto[t, t'] H[S, pc', t', ...]``."""
    tmp = np.tensordot(k_pc, H, axes=([1], [1]))  # (c, S, t', ...)
    tmp = np.moveaxis(tmp, 0, 1)  # (S, c, t', ...)
    res = np.tensordot(k_tto, tmp, axes=([1], [2]))  # (t, S, c, ...)
    return np.moveaxis(res, 0, 2)  # (S, c, t, ...)


# ---------------------------------------------------------------------------
# The references and the expected conditionals
# ---------------------------------------------------------------------------


def band_matrix() -> np.ndarray:
    """``B[b, c]`` = 1 when pitch count ``c`` is in band ``b``."""
    bands = pc_band_of(np.arange(N_PC))
    return (bands[None, :] == np.arange(N_PC_BAND)[:, None]).astype(np.float64)


def marginal_reference(H: np.ndarray) -> dict[str, np.ndarray]:
    """Reference A: the pool's own outcome mix by pitch-count band, by TTO
    and by (band, TTO), aggregated over sub-cells by row mass."""
    Bm = band_matrix()
    by_c = H.sum(axis=(0, 2))  # (c, out)
    by_t = H.sum(axis=(0, 1))  # (t, out)
    by_ct = H.sum(axis=0)  # (c, t, out)
    by_b = Bm @ by_c  # (b, out)
    by_bt = np.einsum("bc,cto->bto", Bm, by_ct)
    return {
        "band": _mix(by_b),
        "tto": _mix(by_t),
        "band_tto": _mix(by_bt),
        "all": _mix(H.sum(axis=(0, 1, 2))[None, :])[0],
        "mass_band": by_b.sum(axis=1),
        "mass_tto": by_t.sum(axis=1),
        "mass_band_tto": by_bt.sum(axis=2),
    }


def _mix(counts: np.ndarray) -> np.ndarray:
    tot = counts.sum(axis=-1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(tot > 0, counts / np.where(tot > 0, tot, 1.0), np.nan)


def expected_conditional(
    H: np.ndarray, M: np.ndarray, Q: np.ndarray, pc_sigma: float, tto_sigma: float
) -> dict[str, Any]:
    """The kernel's expected outcome mix at each live state, aggregated to the
    bands with the pool's own live mass; the effective sample share; the
    kernel-weighted row-pitcher quality by live band."""
    k_pc = gaussian_matrix(N_PC, pc_sigma)
    k_tto = gaussian_matrix(N_TTO, tto_sigma)
    EH = smooth(H, k_pc, k_tto)  # (S, c, t, out): kernel-weighted outcome mass
    EM = EH.sum(axis=3)  # (S, c, t): kernel-weighted total mass
    EQ = smooth(Q, k_pc, k_tto)  # (S, c, t, k)
    # The expected mix per live state, weighted by the LIVE mass M[S, c, t].
    with np.errstate(invalid="ignore", divide="ignore"):
        mix = np.where(EM[..., None] > 0, EH / np.where(EM > 0, EM, 1.0)[..., None], 0.0)
        qual = np.where(EM[..., None] > 0, EQ / np.where(EM > 0, EM, 1.0)[..., None], 0.0)
    live = M[..., None]
    by_ct = (mix * live).sum(axis=0)  # (c, t, out) — mass-weighted expected mix
    q_ct = (qual * live).sum(axis=0)  # (c, t, k)
    mass_ct = M.sum(axis=0)  # (c, t)
    Bm = band_matrix()
    by_bt = np.einsum("bc,cto->bto", Bm, by_ct)
    mass_bt = Bm @ mass_ct
    q_bt = np.einsum("bc,ctk->btk", Bm, q_ct)
    by_b = by_bt.sum(axis=1)
    by_t = by_bt.sum(axis=0)
    q_b = q_bt.sum(axis=1)
    mass_b = mass_bt.sum(axis=1)
    mass_t = mass_bt.sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        mix_b = by_b / mass_b[:, None]
        mix_t = by_t / mass_t[:, None]
        mix_bt = by_bt / mass_bt[..., None]
        mix_all = by_b.sum(axis=0) / mass_b.sum()
        qual_b = q_b / mass_b[:, None]
    # Effective sample share per live state: (sum w m)^2 / (sum w^2 m) / N_S.
    k2_pc = k_pc * k_pc
    k2_tto = k_tto * k_tto
    E2 = smooth(M, k2_pc, k2_tto)  # (S, c, t): sum of w^2 * mass
    n_s = M.sum(axis=(1, 2))  # (S,)
    with np.errstate(invalid="ignore", divide="ignore"):
        ess = (
            np.where(E2 > 0, (EM * EM) / np.where(E2 > 0, E2, 1.0), 0.0)
            / np.where(n_s > 0, n_s, 1.0)[:, None, None]
        )
    ess_share = float((ess * M).sum() / M.sum()) if M.sum() > 0 else float("nan")
    return {
        "band": mix_b,
        "tto": mix_t,
        "band_tto": mix_bt,
        "all": mix_all,
        "quality_band": qual_b,
        "ess_share": ess_share,
    }


def within_pitcher_reference(
    pitcher_key: np.ndarray,
    pc: np.ndarray,
    tto: np.ndarray,
    out: np.ndarray,
    weight: np.ndarray,
    min_rows: int = MIN_ROWS_PER_BAND,
) -> dict[str, Any]:
    """Reference B: for each pitcher-season, his outcome mix per pitch-count
    band (and per TTO) minus his own overall mix; pooled across pitchers
    weighted by his rows in that band. A band with fewer than ``min_rows`` of
    his rows does not count. Returns the pooled deltas ``delta_band[b, out]``,
    ``delta_tto[t, out]`` and the pooled masses."""
    keep = (pc >= 0) & (tto >= 1) & (out >= 0)
    pitcher_key, pc, tto, out, weight = (
        pitcher_key[keep],
        pc[keep],
        tto[keep],
        out[keep],
        weight[keep],
    )
    band = pc_band_of(np.clip(pc, 0, MAX_PC))
    t_idx = (np.clip(tto, 1, N_TTO) - 1).astype(np.int64)
    uniq, pidx = np.unique(pitcher_key, return_inverse=True)
    n_p = uniq.size
    # Mass per (pitcher, band, out) and per (pitcher, tto, out).
    c_pb = np.bincount(
        (pidx * N_PC_BAND + band) * N_OUT + out, weights=weight, minlength=n_p * N_PC_BAND * N_OUT
    ).reshape(n_p, N_PC_BAND, N_OUT)
    c_pt = np.bincount(
        (pidx * N_TTO + t_idx) * N_OUT + out, weights=weight, minlength=n_p * N_TTO * N_OUT
    ).reshape(n_p, N_TTO, N_OUT)
    n_pb = np.bincount(pidx * N_PC_BAND + band, minlength=n_p * N_PC_BAND).reshape(n_p, N_PC_BAND)
    n_pt = np.bincount(pidx * N_TTO + t_idx, minlength=n_p * N_TTO).reshape(n_p, N_TTO)
    c_p = c_pb.sum(axis=1)  # (p, out)
    mix_p = _mix(c_p)

    def pooled(c_px: np.ndarray, n_px: np.ndarray) -> tuple[np.ndarray, np.ndarray, int]:
        mix_px = _mix(c_px)  # (p, x, out)
        mass_px = c_px.sum(axis=2)  # (p, x)
        ok = n_px >= min_rows
        # A pitcher contributes to a band's delta only if he has rows in at
        # least two qualifying bands (otherwise his delta is zero by identity).
        multi = ok.sum(axis=1) >= 2
        ok = ok & multi[:, None]
        delta = mix_px - mix_p[:, None, :]
        w = np.where(ok, mass_px, 0.0)
        num = np.nansum(delta * w[..., None], axis=0)
        den = w.sum(axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            pooled_delta = np.where(
                den[:, None] > 0, num / np.where(den > 0, den, 1.0)[:, None], np.nan
            )
        return pooled_delta, den, int(multi.sum())

    d_band, m_band, n_multi_band = pooled(c_pb, n_pb)
    d_tto, m_tto, n_multi_tto = pooled(c_pt, n_pt)
    return {
        "delta_band": d_band,
        "delta_tto": d_tto,
        "mass_band": m_band,
        "mass_tto": m_tto,
        "n_pitcher_seasons": int(n_p),
        "n_pitchers_multi_band": n_multi_band,
        "n_pitchers_multi_tto": n_multi_tto,
    }


#: A pitcher-season whose rows sit past the first time through the order at
#: least this often is a STARTER (relievers almost never face the order twice).
STARTER_TTO2_SHARE = 0.35
QUALITY_COLS: tuple[str, ...] = (
    "whiff",
    "ball",
    "starter",
    "whiff_x_starter",
    "ball_x_starter",
)


def pitcher_quality(
    pitcher_key: np.ndarray, out: np.ndarray, weight: np.ndarray, tto: np.ndarray | None = None
) -> np.ndarray:
    """Per row: the row pitcher's own season whiff share and ball share over
    all his rows, his ROLE (1 = starter, by the share of his rows past the
    first time through) and the two products the within-starter quality
    read needs (the survivorship read's columns, ``QUALITY_COLS``)."""
    keep = out >= 0
    uniq, pidx = np.unique(pitcher_key, return_inverse=True)
    n_p = uniq.size
    w = np.where(keep, weight, 0.0)
    tot = np.bincount(pidx, weights=w, minlength=n_p)
    whiff = np.bincount(pidx, weights=w * (out == CHANNELS["whiff"]), minlength=n_p)
    ball = np.bincount(pidx, weights=w * (out == CHANNELS["ball"]), minlength=n_p)
    if tto is None:
        deep = np.zeros(n_p)
    else:
        deep = np.bincount(pidx, weights=w * (np.asarray(tto) >= 2), minlength=n_p)
    with np.errstate(invalid="ignore", divide="ignore"):
        safe = np.where(tot > 0, tot, 1.0)
        q_whiff = np.where(tot > 0, whiff / safe, 0.0)
        q_ball = np.where(tot > 0, ball / safe, 0.0)
        starter = np.where(tot > 0, (deep / safe) >= STARTER_TTO2_SHARE, False).astype(np.float64)
    q = np.stack(
        [q_whiff, q_ball, starter, q_whiff * starter, q_ball * starter],
        axis=1,
    )
    return q[pidx]


# ---------------------------------------------------------------------------
# The reads
# ---------------------------------------------------------------------------


def _spread(v: np.ndarray, mass: np.ndarray) -> float:
    """Top band minus bottom band among the bands that carry mass."""
    ok = np.where(mass > 0)[0]
    if ok.size < 2:
        return float("nan")
    return float(v[ok[-1]] - v[ok[0]])


def read_arm(
    exp: dict[str, Any], ref_a: dict[str, np.ndarray], ref_b: dict[str, Any]
) -> dict[str, Any]:
    """Per channel: the kernel's expected band deltas against A's and B's,
    the spread ratios, and the survivorship read."""
    out: dict[str, Any] = {"ess_share": exp["ess_share"], "channels": {}}
    for ch, o in CHANNELS.items():
        dA_b = ref_a["band"][:, o] - ref_a["all"][o]
        dE_b = exp["band"][:, o] - exp["all"][o]
        dB_b = ref_b["delta_band"][:, o]
        dA_t = ref_a["tto"][:, o] - ref_a["all"][o]
        dE_t = exp["tto"][:, o] - exp["all"][o]
        dB_t = ref_b["delta_tto"][:, o]
        sA_b, sE_b, sB_b = (
            _spread(dA_b, ref_a["mass_band"]),
            _spread(dE_b, ref_a["mass_band"]),
            _spread(dB_b, ref_b["mass_band"]),
        )
        sA_t, sE_t, sB_t = (
            _spread(dA_t, ref_a["mass_tto"]),
            _spread(dE_t, ref_a["mass_tto"]),
            _spread(dB_t, ref_b["mass_tto"]),
        )
        out["channels"][ch] = {
            "band": {
                "pool_marginal_delta": dA_b.tolist(),
                "within_pitcher_delta": dB_b.tolist(),
                "kernel_delta": dE_b.tolist(),
                "spread_pool": sA_b,
                "spread_within": sB_b,
                "spread_kernel": sE_b,
                "ratio_vs_within": (sE_b / sB_b)
                if sB_b not in (0.0,) and np.isfinite(sB_b)
                else float("nan"),
                "ratio_vs_marginal": (sE_b / sA_b)
                if sA_b not in (0.0,) and np.isfinite(sA_b)
                else float("nan"),
            },
            "tto": {
                "pool_marginal_delta": dA_t.tolist(),
                "within_pitcher_delta": dB_t.tolist(),
                "kernel_delta": dE_t.tolist(),
                "spread_pool": sA_t,
                "spread_within": sB_t,
                "spread_kernel": sE_t,
                "ratio_vs_within": (sE_t / sB_t)
                if sB_t not in (0.0,) and np.isfinite(sB_t)
                else float("nan"),
                "ratio_vs_marginal": (sE_t / sA_t)
                if sA_t not in (0.0,) and np.isfinite(sA_t)
                else float("nan"),
            },
        }
    # Survivorship: the kernel-weighted row-pitcher quality by live band minus
    # the flat (sub-cell marginal) quality by live band.
    qb = exp["quality_band"]  # (band, QUALITY_COLS): kernel-weighted means
    with np.errstate(invalid="ignore", divide="ignore"):
        starter_share = qb[:, 2]
        whiff_in_starters = np.where(starter_share > 0, qb[:, 3] / starter_share, np.nan)
        ball_in_starters = np.where(starter_share > 0, qb[:, 4] / starter_share, np.nan)
    out["survivorship"] = {
        "whiff_by_band": qb[:, 0].tolist(),
        "ball_by_band": qb[:, 1].tolist(),
        # ROLE: the starter share of the kernel-weighted rows per live band.
        "starter_share_by_band": starter_share.tolist(),
        # QUALITY WITHIN ROLE: the starters' own whiff / ball share among the
        # kernel-weighted starter rows per live band.
        "starter_whiff_by_band": whiff_in_starters.tolist(),
        "starter_ball_by_band": ball_in_starters.tolist(),
    }
    return out


def _fmt(x: float, w: int = 8, pct: bool = True) -> str:
    if x is None or not np.isfinite(x):
        return f"{'nan':>{w}}"
    return f"{x * 100:>{w}.2f}" if pct else f"{x:>{w}.3f}"


def print_report(result: dict[str, Any]) -> None:
    ref_a = result["reference_marginal"]
    ref_b = result["reference_within"]
    print("\n=== SIM-518 fatigue scan — the pool's own conditionals (points of share, ×100) ===")
    print(
        f"rows {result['n_rows']:,}  pitcher-seasons {ref_b['n_pitcher_seasons']:,}  "
        f"multi-band pitchers {ref_b['n_pitchers_multi_band']:,}  "
        f"multi-TTO pitchers {ref_b['n_pitchers_multi_tto']:,}"
    )
    print(
        "\nBy pitch-count band — delta from the pool's overall mix; A = marginal, B = within-pitcher"
    )
    print(f"{'channel':<9}{'':>4}" + "".join(f"{b:>10}" for b in PC_BANDS))
    for ch in CHANNELS:
        a = ref_a["delta_band"][ch]
        b = ref_b["delta_band"][ch]
        print(f"{ch:<9}{'A':>4}" + "".join(_fmt(x, 10) for x in a))
        print(f"{'':<9}{'B':>4}" + "".join(_fmt(x, 10) for x in b))
    print(f"{'mass %':<13}" + "".join(_fmt(x, 10) for x in ref_a["mass_share_band"]))
    print("\nBy times through the order")
    print(f"{'channel':<9}{'':>4}" + "".join(f"{t:>10}" for t in TTO_LABELS))
    for ch in CHANNELS:
        a = ref_a["delta_tto"][ch]
        b = ref_b["delta_tto"][ch]
        print(f"{ch:<9}{'A':>4}" + "".join(_fmt(x, 10) for x in a))
        print(f"{'':<9}{'B':>4}" + "".join(_fmt(x, 10) for x in b))
    print(f"{'mass %':<13}" + "".join(_fmt(x, 10) for x in ref_a["mass_share_tto"]))
    print(
        "\n=== The ladder — the kernel's expected delta against B (within-pitcher), "
        "in points of share ==="
    )
    print(
        "    per arm: mean |kernel - B| over the bands / over the TTOs (mass-weighted); "
        "the 3rd-TTO and 76-100-band deltas for called / whiff / ball / in play (kernel over B);"
    )
    print(
        "    SURVIVORSHIP at the 76-100 live band against the flat draw: the starter share of "
        "the drawn rows (role), and the starters' own whiff / ball share (quality within role)."
    )
    flat = next(
        (a for a in result["arms"] if a["pc_sigma"] == FLAT and a["tto_sigma"] == FLAT), None
    )
    mass_b = np.array(ref_a["mass_share_band"])
    mass_t = np.array(ref_a["mass_share_tto"])
    hdr = (
        f"{'pc':>5}{'tto':>5}{'ESS%':>6} | {'MAEband':>8}{'MAEtto':>7} | "
        f"{'3rd: called':>13}{'whiff':>13}{'ball':>13}{'inplay':>13} | "
        f"{'76-100: called':>15}{'whiff':>13}{'ball':>13} | "
        f"{'role':>7}{'st.whiff':>9}{'st.ball':>8}"
    )
    print(hdr)
    for arm in result["arms"]:
        c = arm["channels"]
        mae_b = []
        mae_t = []
        for ch in CHANNELS:
            kb = np.array(c[ch]["band"]["kernel_delta"])
            bb = np.array(ref_b["delta_band"][ch])
            kt = np.array(c[ch]["tto"]["kernel_delta"])
            bt = np.array(ref_b["delta_tto"][ch])
            ok = np.isfinite(bb)
            mae_b.append(float(np.average(np.abs(kb - bb)[ok], weights=mass_b[ok])))
            ok = np.isfinite(bt)
            mae_t.append(float(np.average(np.abs(kt - bt)[ok], weights=mass_t[ok])))

        def pair(ch: str, axis: str, i: int, _c: dict = c) -> str:
            k = _c[ch][axis]["kernel_delta"][i] * 100
            b = ref_b[f"delta_{axis}"][ch][i] * 100
            return f"{k:>+6.2f}/{b:>+5.2f}"

        sv = arm["survivorship"]
        role = st_w = st_b = float("nan")
        if flat is not None:
            fs = flat["survivorship"]
            role = (sv["starter_share_by_band"][3] - fs["starter_share_by_band"][3]) * 100
            st_w = (sv["starter_whiff_by_band"][3] - fs["starter_whiff_by_band"][3]) * 100
            st_b = (sv["starter_ball_by_band"][3] - fs["starter_ball_by_band"][3]) * 100
        print(
            f"{arm['pc_sigma']:>5.1f}{arm['tto_sigma']:>5.2f}{arm['ess_share'] * 100:>6.1f} | "
            f"{np.mean(mae_b) * 100:>8.3f}{np.mean(mae_t) * 100:>7.3f} | "
            f"{pair('called', 'tto', 2):>13}{pair('whiff', 'tto', 2):>13}"
            f"{pair('ball', 'tto', 2):>13}{pair('in_play', 'tto', 2):>13} | "
            f"{pair('called', 'band', 3):>15}{pair('whiff', 'band', 3):>13}"
            f"{pair('ball', 'band', 3):>13} | "
            f"{role:>+7.2f}{st_w:>+9.3f}{st_b:>+8.3f}"
        )
    if flat is not None:
        fs = flat["survivorship"]
        print(
            "\nflat draw at the 76-100 live band: starter share "
            f"{fs['starter_share_by_band'][3] * 100:.1f}%, starters' whiff "
            f"{fs['starter_whiff_by_band'][3] * 100:.2f}, ball "
            f"{fs['starter_ball_by_band'][3] * 100:.2f} (points of share)"
        )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def load_pool_columns(art_dir: str, hand: str) -> dict[str, np.ndarray]:
    """The pitch pool's scan columns for one hand, read straight from the
    bundle (no sampler needed): sit, pitcher/season, outcome, recency and the
    SIM-518 conditioning columns from the meta parquet."""
    import pyarrow.parquet as pq

    base = os.path.join(art_dir, "pitch_pool")
    sit = np.load(os.path.join(base, f"{hand}.sit.npy"), mmap_mode="r")
    meta = pq.read_table(
        os.path.join(base, f"{hand}.meta.parquet"),
        columns=[
            "pitcher_id",
            "season",
            "outcome_type",
            "recency_weight",
            "bat_home",
            "pitcher_pitch_count",
            "times_through_order",
        ],
    ).to_pandas()
    return {
        "sit": np.asarray(sit, dtype=np.float32),
        # pitcher-season as one integer key (np.unique on strings is slow).
        "pitcher_key": (
            meta["pitcher_id"].to_numpy(dtype=np.int64) * 10_000
            + meta["season"].to_numpy(dtype=np.int64)
        ),
        "outcome": outcome_codes(meta["outcome_type"].to_numpy()),
        "recency": meta["recency_weight"].to_numpy(dtype=np.float64),
        "bat_home": meta["bat_home"].fillna(-1).to_numpy(dtype=np.int64),
        "pc": meta["pitcher_pitch_count"].fillna(-1).to_numpy(dtype=np.int64),
        "tto": meta["times_through_order"].fillna(0).to_numpy(dtype=np.int64),
    }


def run_scan(
    cols_by_hand: dict[str, dict[str, np.ndarray]],
    pc_sigmas: tuple[float, ...],
    tto_sigmas: tuple[float, ...],
) -> dict[str, Any]:
    t0 = time.perf_counter()
    H_all: list[np.ndarray] = []
    M_all: list[np.ndarray] = []
    Q_all: list[np.ndarray] = []
    keys: list[np.ndarray] = []
    pcs: list[np.ndarray] = []
    ttos: list[np.ndarray] = []
    outs: list[np.ndarray] = []
    ws: list[np.ndarray] = []
    n_rows = 0
    for hand, c in cols_by_hand.items():
        sub = subcell_ids(c["sit"], c["bat_home"])
        q = pitcher_quality(c["pitcher_key"], c["outcome"], c["recency"], c["tto"])
        H, M, Q = build_histograms(sub, c["pc"], c["tto"], c["outcome"], c["recency"], q)
        H_all.append(H)
        M_all.append(M)
        Q_all.append(Q)
        keys.append(c["pitcher_key"])
        pcs.append(c["pc"])
        ttos.append(c["tto"])
        outs.append(c["outcome"])
        ws.append(c["recency"])
        n_rows += int(c["outcome"].size)
        print(
            f"  hand {hand}: {c['outcome'].size:,} rows, {H.shape[0]:,} occupied sub-cells",
            flush=True,
        )
    H = np.concatenate(H_all)
    M = np.concatenate(M_all)
    Q = np.concatenate(Q_all)
    ref_a_raw = marginal_reference(H)
    ref_b_raw = within_pitcher_reference(
        np.concatenate(keys),
        np.concatenate(pcs),
        np.concatenate(ttos),
        np.concatenate(outs),
        np.concatenate(ws),
    )
    print(f"  references built in {time.perf_counter() - t0:.1f}s", flush=True)
    arms: list[dict[str, Any]] = []
    for ps in pc_sigmas:
        for ts in tto_sigmas:
            t1 = time.perf_counter()
            exp = expected_conditional(H, M, Q, ps, ts)
            arm = read_arm(exp, ref_a_raw, ref_b_raw)
            arm["pc_sigma"] = float(ps)
            arm["tto_sigma"] = float(ts)
            arm["expected_band_mix"] = {
                ch: exp["band"][:, o].tolist() for ch, o in CHANNELS.items()
            }
            arm["expected_tto_mix"] = {ch: exp["tto"][:, o].tolist() for ch, o in CHANNELS.items()}
            arms.append(arm)
            print(f"  arm pc={ps:g} tto={ts:g}: {time.perf_counter() - t1:.1f}s", flush=True)
    mass_b = ref_a_raw["mass_band"]
    mass_t = ref_a_raw["mass_tto"]
    result = {
        "n_rows": n_rows,
        "pc_bands": list(PC_BANDS),
        "tto_labels": list(TTO_LABELS),
        "reference_marginal": {
            "delta_band": {
                ch: (ref_a_raw["band"][:, o] - ref_a_raw["all"][o]).tolist()
                for ch, o in CHANNELS.items()
            },
            "delta_tto": {
                ch: (ref_a_raw["tto"][:, o] - ref_a_raw["all"][o]).tolist()
                for ch, o in CHANNELS.items()
            },
            "mix_band": {ch: ref_a_raw["band"][:, o].tolist() for ch, o in CHANNELS.items()},
            "mix_tto": {ch: ref_a_raw["tto"][:, o].tolist() for ch, o in CHANNELS.items()},
            "mix_all": {ch: float(ref_a_raw["all"][o]) for ch, o in CHANNELS.items()},
            "mass_share_band": (mass_b / mass_b.sum()).tolist(),
            "mass_share_tto": (mass_t / mass_t.sum()).tolist(),
        },
        "reference_within": {
            "delta_band": {
                ch: ref_b_raw["delta_band"][:, o].tolist() for ch, o in CHANNELS.items()
            },
            "delta_tto": {ch: ref_b_raw["delta_tto"][:, o].tolist() for ch, o in CHANNELS.items()},
            "n_pitcher_seasons": ref_b_raw["n_pitcher_seasons"],
            "n_pitchers_multi_band": ref_b_raw["n_pitchers_multi_band"],
            "n_pitchers_multi_tto": ref_b_raw["n_pitchers_multi_tto"],
            "min_rows_per_band": MIN_ROWS_PER_BAND,
        },
        "arms": arms,
        "elapsed_s": time.perf_counter() - t0,
    }
    return result


def _parse_sigmas(text: str) -> tuple[float, ...]:
    return tuple(float(x) for x in text.split(",") if x.strip())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--art-dir", default=DEFAULT_ART_DIR)
    ap.add_argument("--hands", default="L,R")
    ap.add_argument("--pc-sigmas", default=",".join(str(x) for x in DEFAULT_PC_SIGMAS))
    ap.add_argument("--tto-sigmas", default=",".join(str(x) for x in DEFAULT_TTO_SIGMAS))
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)
    hands = [h.strip() for h in args.hands.split(",") if h.strip()]
    print(f"sim518_fatigue_scan: bundle {args.art_dir}, hands {hands}")
    cols = {h: load_pool_columns(args.art_dir, h) for h in hands}
    result = run_scan(cols, _parse_sigmas(args.pc_sigmas), _parse_sigmas(args.tto_sigmas))
    result["art_dir"] = args.art_dir
    result["hands"] = hands
    print_report(result)
    print(f"\nelapsed: {result['elapsed_s']:.1f}s")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, indent=2, default=float))
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
