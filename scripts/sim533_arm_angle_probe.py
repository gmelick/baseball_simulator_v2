"""
scripts/sim533_arm_angle_probe.py — the evidence behind the arm-angle decision (SIM-533):
what Savant's arm angle, active spin and spin-axis deviation add to the pitcher model.

The decision (owner, 2026-09-19; the plan is
docs/audit/2026-09-18-sim533-pitcher-arm-angle-spin-shape-plan.md): none of the three
numbers joins the pitcher model. This script is the design session's two probes merged so
the numbers re-run: the board pulls with their probes, the reconstructions, the year-to-year
repeats and the pair test. It changes nothing in production.

What it measures.

* Part A, the arm angle. Savant's arm angle is the angle from the shoulder to the release
  point (A1: their own four coordinates rebuild their angle at r 1.000, mean gap 0.03
  degrees, n 625). Our per-pitcher mean release point reproduces 73% of its variance, 81%
  with the pitcher's height (A2); the rest is where the shoulder sits at release, a
  posture the ball's flight does not carry. The angle repeats 0.972 year to year (A3), as
  does our own release z (0.979 / 0.977) and a geometric proxy built from it (0.944 /
  0.952). A4 gives the 2024 spread by hand.
* Part B, the spin shape (the spin-direction board, per pitcher and pitch type). Savant's
  spin rate, velocity and total movement are our own columns (B1: r 1.000, 1.000, 0.985,
  n 2,331). Our ``spin_axis`` IS their measured axis under the mirrored convention 360
  minus ours (B2: mean cos 0.992). Their movement-inferred axis is the formula 180 minus
  atan2(hb, ivb) on our two movement columns (B3: 0.987). Active spin follows our movement
  times velocity over spin rate (B4: r 0.942; a log fit R2 0.851 against 0.876 on their own
  inputs). The measured-minus-inferred deviation rebuilds from our two axes at r 0.803,
  while a linear fit on the raw columns reads only 0.153 (B5): the relation is angular.
  Active spin repeats 0.978 / 0.978, the deviation 0.929 / 0.961, our per-type means 0.975
  to 0.992 (B6).
* Part C, the pair test. Same-hand pairs of 2024 pitchers with 500 or more pitches, an arm
  angle and a row in the production similarity matrix: 475 pitchers, 67,941 pairs (59,685
  right-handed, 8,256 left-handed). The production score alone explains R2 0.019 of the
  distance between two pitchers' four unread outcome rates (ground-ball, fly-ball,
  line-drive rates and home runs per nine) and 0.472 of all nine rates. Each candidate's
  gap is added to that fit; the gain is the added R2. The largest gain on the unread rates
  is the fastball's axis deviation, +0.0086; the arm angle's +0.0045 splits +0.0099 /
  +0.0017 across two random halves of the pitchers (seed 533). On the nine rates the arm
  angle gap carries a NEGATIVE standardised coefficient (-0.17) given the score, which
  already tracks it (r 0.35 with one minus the score): a lead for the sweep's per-dimension
  weight, not a feature.

The offline check (``--offline``). Six checks on a bundled fixture of real 2024 rows, so
the script's conventions cannot rot silently: ``ARM_FIXTURE`` is the 20 pitchers with the
most pitches on the 2024 arm board with all five coordinates; ``SPIN_FIXTURE`` is 20
(pitcher, pitch type) rows of 2024 with 200 or more pitches on both sides, the three
fastballs and the three breaking balls with the most board pitches first, then the top
overall. What the fixture reads on 2026-09-19: Savant's formula r 1.0000 (mean gap 0.04
degrees); the measured-axis convention 360 minus ours 0.999; the inferred-axis formula
0.993; spin / velocity / movement r 1.000 / 1.000 / 0.993; the active-spin proxy r 0.986
(the pass line is 0.80); the deviation rebuilt r 0.736 (the pass line is 0.60). The two
pass lines sit below the full-board figures (0.942 and 0.803) because twenty rows are few;
neither had to be lowered from the plan's thresholds.

The sources (``--csv-dir DIR``, the design session's layout): ``arm_{2020..2025}.csv``,
``arm_1990.csv`` (the empty-body probe), ``arm_2024_year.csv`` (the ``year=`` probe),
``arm_2024_default.csv`` (no ``min=1``), ``arm_noseason.csv`` (no season parameter; the
``year=`` comparison), ``spindir_{2020..2025}.csv``, ``spindir_1990.csv``,
``spindir_2024_default.csv``, ``active_2024.csv``, ``active_1990.csv``; our four inputs
``our_release.csv``, ``our_pitchtypes.csv``, ``heights.csv``, ``outcomes.csv``; and the
production matrix ``pitcher_sim.npz``. Each part runs only when its ``--dump`` inputs are
present (A: the release and height files; B: the pitch-type file; C: the matrix, the
outcomes and the release and height files). A missing part prints a ``skipped`` line, so
``--pull`` alone completes. ``--pull`` fetches the boards into DIR first (urllib, a browser
User-Agent, a 170 s timeout) and prints the board probes: the 1990 pulls must read zero
rows (a header-only file and a wholly empty body both count; an HTML body is rejected);
the arm board's ``year=`` pull must have the row count of the no-season pull (the board
ignores ``year=`` and serves the current season); ``season=2024`` with ``min=1`` read 823
rows in the design session against 284 by default. ``--dump`` writes our four inputs
from the live stack: three from Postgres (``--dsn`` or ``BASEBALL_DB_DSN``), the outcomes
from DuckDB (``--duckdb-path``), and copies the matrix from ``--matrix-path``. The DuckDB
read opens the file read-only; the running app also holds it read-only, so the dump runs
with the app up. Only a writer (a profile recompute or a pool rebuild) blocks the open.

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim533_arm_angle_probe.py --csv-dir /tmp/sim533 --pull --dump
    python scripts/sim533_arm_angle_probe.py --offline
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np  # noqa: E402

DEFAULT_DUCKDB = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
DEFAULT_DSN = os.environ.get("BASEBALL_DB_DSN", "")
DEFAULT_MATRIX = "/data/play_pool/engine_artifacts/pitcher_sim.npz"

#: The seasons the three boards serve (they start in 2020).
SEASONS: tuple[int, ...] = (2020, 2021, 2022, 2023, 2024, 2025)
#: The season the empty-body probe asks for.
EMPTY_SEASON = 1990
#: The seed of the random half split in part C.
HALF_SPLIT_SEED = 533
#: The pitch-type groups of part C.
FASTBALLS = frozenset({"FF", "SI", "FC"})
BREAKING_BALLS = frozenset({"SL", "ST", "CU", "KC", "SV", "CS"})

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
PULL_TIMEOUT_S = 170

#: The exact URLs of the design session (2026-09-18).
ARM_URL = (
    "https://baseballsavant.mlb.com/leaderboard/pitcher-arm-angles?batSide=&gameType=R"
    "&min=1&perspective=back&playerType=pitcher&season={Y}&size=small&sort=ascending"
    "&team=&csv=true"
)
SPINDIR_URL = (
    "https://baseballsavant.mlb.com/leaderboard/spin-direction-pitches"
    "?year={Y}&min=1&hand=&pitch_type=ALL&csv=true"
)
ACTIVE_URL = "https://baseballsavant.mlb.com/leaderboard/active-spin?year={Y}&min=1&hand=&csv=true"

# ---------------------------------------------------------------------------
# The bundled fixture: real 2024 rows (see the module docstring for the rule)
# ---------------------------------------------------------------------------

# fmt: off
#: (pitcher, hand, n_pitches, ball_angle, relative_release_ball_x, release_ball_z,
#:  relative_shoulder_x, shoulder_z) — the 2024 arm-angle board.
ARM_FIXTURE: list[tuple[int, str, int, float, float, float, float, float]] = [
    (657277, 'R', 3198, 22.1, -2.6642, 4.9782, -0.515, 4.1037),
    (605400, 'R', 3189, 20.1, -2.4012, 5.0246, -0.2443, 4.2362),
    (656302, 'R', 3188, 53.4, -1.2926, 6.1003, 0.0209, 4.3277),
    (554430, 'R', 3134, 24.7, -2.674, 5.2283, -0.415, 4.1909),
    (666142, 'L', 3118, 45.1, 1.6507, 6.3353, -0.0286, 4.6486),
    (607625, 'R', 3112, 32.6, -2.203, 5.4859, -0.1693, 4.1822),
    (607074, 'L', 3102, 45.4, 1.6955, 6.3259, -0.0169, 4.5897),
    (642547, 'R', 3066, 36.9, -2.1728, 5.2798, -0.389, 3.9411),
    (663623, 'R', 3046, 28.5, -2.169, 5.7247, -0.1095, 4.6039),
    (669302, 'R', 3036, 43.8, -1.4977, 5.8254, 0.1798, 4.2118),
    (669203, 'R', 2994, 43.7, -1.577, 5.9678, 0.0676, 4.3953),
    (669022, 'L', 2993, 47.2, 1.5529, 5.8507, 0.0262, 4.2034),
    (641154, 'R', 2985, 38.3, -2.1004, 5.5832, -0.2157, 4.0937),
    (669923, 'R', 2967, 36.6, -1.7962, 5.8668, 0.1481, 4.4204),
    (663903, 'R', 2966, 27.8, -1.9702, 5.2623, -0.1464, 4.299),
    (605135, 'R', 2959, 35.3, -2.0626, 5.5224, 0.041, 4.0329),
    (579328, 'L', 2923, 41.9, 1.6505, 5.4267, 0.0473, 3.9908),
    (542881, 'L', 2913, 57.5, 0.9518, 6.4043, -0.2322, 4.5346),
    (640455, 'L', 2909, 22.7, 2.9288, 5.4367, 0.7584, 4.5273),
    (621244, 'R', 2907, 39.6, -2.1501, 5.6118, -0.3532, 4.1277),
]

#: (pitcher, pitch_type, board n_pitches, release_speed, spin_rate, movement_inches,
#:  active_spin, hawkeye_measured, movement_inferred, diff_measured_inferred,
#:  our velo, ivb, hb, spin, axis_circ) — the 2024 spin-direction board joined to
#: our per-pitch-type means from ``raw.pitches``.
SPIN_FIXTURE: list[tuple] = [
    (669022, 'FF', 1653, 96.0, 2309.0, 18.2, 0.9292, 208.0088, 199.7306, 8.2782, 95.9996, 17.5862, -5.9103, 2307.9879, 150.2502),
    (642547, 'FF', 1632, 94.3, 2436.0, 17.6, 0.8902, 150.2551, 156.3509, -6.0959, 94.358, 16.6706, 6.7679, 2436.3226, -149.225),
    (607074, 'FF', 1525, 95.4, 2384.0, 19.6, 0.9645, 212.6697, 208.0334, 4.6363, 95.5244, 17.8557, -8.8258, 2383.9252, 145.8632),
    (592332, 'FF', 1484, 94.0, 2287.0, 19.8, 0.9692, 142.8992, 145.6873, -2.7881, 93.9575, 16.9758, 10.7079, 2286.6169, -142.5492),
    (668881, 'FF', 1412, 97.6, 2378.0, 18.6, 0.9476, 145.0284, 150.0485, -5.02, 97.6081, 16.5761, 8.8798, 2378.149, -144.4418),
    (571760, 'FF', 1392, 91.6, 2401.0, 20.9, 0.9952, 234.9359, 231.8363, 3.0996, 91.5583, 13.8015, -15.6903, 2400.1545, 124.354),
    (656302, 'FF', 1379, 96.9, 2557.0, 18.2, 0.8731, 170.6407, 172.0629, -1.4222, 96.8873, 18.3676, 2.4919, 2555.7915, -169.5516),
    (656302, 'SL', 1357, 87.7, 2781.0, 3.3, 0.2485, 257.6375, 252.2787, 5.3588, 87.7001, 1.9104, -2.1345, 2778.7579, 96.9442),
    (669194, 'FF', 1340, 95.2, 2216.0, 18.8, 0.9778, 160.5944, 161.3033, -0.7089, 95.1896, 18.3007, 5.7255, 2215.9441, -160.5662),
    (579328, 'FF', 1339, 95.5, 2291.0, 18.3, 0.9017, 215.9558, 206.6704, 9.2854, 95.5168, 16.9299, -7.8737, 2288.7853, 142.0257),
    (684007, 'FF', 1334, 91.7, 2442.0, 20.6, 0.9883, 212.1062, 211.0632, 1.0431, 91.6539, 18.2949, -10.1945, 2441.3979, 147.0098),
    (650911, 'SI', 1323, 94.5, 2111.0, 19.7, 0.9789, 237.9517, 250.8821, -12.9304, 94.5129, 7.4606, -17.7866, 2108.8235, 120.8959),
    (669203, 'FC', 1317, 95.3, 2738.0, 12.4, 0.6017, 177.4381, 193.7398, -16.3017, 95.2362, 12.395, -2.4591, 2741.1303, -175.7276),
    (554430, 'FF', 1295, 95.3, 2431.0, 17.2, 0.8324, 135.1919, 144.8781, -9.6862, 95.341, 14.6884, 9.5993, 2432.898, -133.6038),
    (661563, 'FF', 1284, 96.6, 2438.0, 17.8, 0.8422, 157.332, 160.9614, -3.6294, 96.5806, 17.2665, 5.4777, 2441.1041, -155.8996),
    (666142, 'FF', 1281, 95.4, 2555.0, 21.3, 0.9931, 219.8745, 218.2823, 1.5922, 95.3897, 17.4242, -12.5509, 2554.6132, 139.5301),
    (641482, 'FF', 1264, 92.1, 2309.0, 19.3, 0.9668, 200.6205, 195.2283, 5.3922, 92.0789, 19.1554, -4.8636, 2308.685, 158.2681),
    (676979, 'FF', 1264, 97.1, 2500.0, 17.3, 0.8714, 217.9559, 209.2249, 8.731, 97.156, 15.5658, -8.1157, 2499.3341, 139.3068),
    (663903, 'SL', 1193, 83.0, 2407.0, 5.9, 0.3185, 264.9718, 299.1955, -34.2237, 82.9818, -1.0089, -4.4549, 2406.4979, 89.4006),
    (450203, 'CU', 1155, 81.5, 3106.0, 18.3, 0.7128, 299.6404, 307.9559, -8.3154, 81.5177, -8.8615, -13.6517, 3084.8918, 58.4224),
]
# fmt: on

# ---------------------------------------------------------------------------
# Small helpers (the design session's, unchanged)
# ---------------------------------------------------------------------------


def _f(x: object) -> float | None:
    """A finite float, or None."""
    try:
        v = float(x)  # type: ignore[arg-type]
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _r(xs: list[float], ys: list[float]) -> tuple[int, float | None]:
    """(n, Pearson r rounded to 3 places); r is None under six pairs."""
    if len(xs) <= 5:
        return len(xs), None
    return len(xs), round(float(np.corrcoef(np.asarray(xs, float), np.asarray(ys, float))[0, 1]), 3)


def _corr(xs, ys) -> float:
    return float(np.corrcoef(np.asarray(xs, float), np.asarray(ys, float))[0, 1])


def _ols(X, y) -> tuple[np.ndarray, float, float]:
    """Least squares with an intercept: (beta, R2, residual SD)."""
    y = np.asarray(y, float)
    X = np.column_stack([np.ones(len(y)), np.asarray(X, float)])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    return beta, float(1 - resid.var() / y.var()), float(resid.std())


def _circ_r(a, b) -> float:
    """The mean cosine of the angle difference (a symmetric circular association),
    rounded to 3 places."""
    a, b = np.radians(np.asarray(a, float)), np.radians(np.asarray(b, float))
    return round(float(np.mean(np.cos(a - b))), 3)


def _wrap(deg) -> np.ndarray:
    """An angle difference wrapped into (-180, 180]."""
    return ((np.asarray(deg, float) + 180.0) % 360.0) - 180.0


#: The four candidate conventions for the measured axis (Savant against our spin_axis).
AXIS_CONVENTIONS = {
    "as is": lambda v: np.asarray(v, float) % 360,
    "360-ours": lambda v: (360 - np.asarray(v, float)) % 360,
    "ours+180": lambda v: (np.asarray(v, float) + 180) % 360,
    "180-ours": lambda v: (180 - np.asarray(v, float)) % 360,
}


def _inferred_candidates(ivb, hb) -> dict[str, np.ndarray]:
    """The four candidate formulas for the movement-inferred axis from our ivb / hb."""
    a = np.degrees(np.arctan2(np.asarray(hb, float), np.asarray(ivb, float)))
    a_neg = np.degrees(np.arctan2(-np.asarray(hb, float), np.asarray(ivb, float)))
    return {
        "atan2(hb, ivb)": a % 360,
        "atan2(-hb, ivb)": a_neg % 360,
        "180 - atan2(hb, ivb)": (180 - a) % 360,
        "180 + atan2(hb, ivb)": (180 + a) % 360,
    }


# ---------------------------------------------------------------------------
# The offline check on the bundled fixture
# ---------------------------------------------------------------------------


def offline_checks(arm_rows, spin_rows) -> list[tuple[str, bool, str]]:
    """Six checks of the script's conventions on the fixture: (name, passed, detail)."""
    out: list[tuple[str, bool, str]] = []

    # (1) Savant's angle is atan2(release z - shoulder z, |release x - shoulder x|).
    rebuilt = [
        math.degrees(math.atan2(rz - sz, abs(rrx - rsx)))
        for _, _, _, _, rrx, rz, rsx, sz in arm_rows
    ]
    angle = [t[3] for t in arm_rows]
    r1 = _corr(rebuilt, angle)
    gap = float(np.mean(np.abs(np.asarray(rebuilt) - np.asarray(angle))))
    out.append(
        (
            "savant_angle_formula",
            r1 >= 0.999 and gap <= 0.1,
            f"r {r1:.4f}, mean |gap| {gap:.3f} deg (n {len(arm_rows)})",
        )
    )

    hawk = [t[7] for t in spin_rows]
    inferred = [t[8] for t in spin_rows]
    diff = [t[9] for t in spin_rows]
    velo_o = np.array([t[10] for t in spin_rows])
    ivb = np.array([t[11] for t in spin_rows])
    hb = np.array([t[12] for t in spin_rows])
    spin_o = np.array([t[13] for t in spin_rows])
    axis_o = np.array([t[14] for t in spin_rows])

    # (2) The measured axis is our spin_axis under the mirrored convention.
    conv = {k: _circ_r(hawk, fn(axis_o)) for k, fn in AXIS_CONVENTIONS.items()}
    best2 = max(conv, key=lambda k: conv[k])
    out.append(
        (
            "measured_axis_convention",
            best2 == "360-ours" and conv["360-ours"] >= 0.98,
            "mean cos " + ", ".join(f"{k} {v:.3f}" for k, v in conv.items()) + f"; best {best2}",
        )
    )

    # (3) The inferred axis is 180 - atan2(hb, ivb) on our movement columns.
    cands = {k: _circ_r(inferred, v) for k, v in _inferred_candidates(ivb, hb).items()}
    best3 = max(cands, key=lambda k: cands[k])
    out.append(
        (
            "inferred_axis_formula",
            best3 == "180 - atan2(hb, ivb)" and cands["180 - atan2(hb, ivb)"] >= 0.97,
            "mean cos " + ", ".join(f"{k} {v:.3f}" for k, v in cands.items()) + f"; best {best3}",
        )
    )

    # (4) The board's spin rate, velocity and movement are our own columns.
    mov_o = np.hypot(ivb, hb)
    r_spin = _corr(spin_o, [t[4] for t in spin_rows])
    r_velo = _corr(velo_o, [t[3] for t in spin_rows])
    r_mov = _corr(mov_o, [t[5] for t in spin_rows])
    out.append(
        (
            "same_measurements",
            r_spin >= 0.99 and r_velo >= 0.99 and r_mov >= 0.95,
            f"r spin {r_spin:.3f}, velocity {r_velo:.3f}, movement {r_mov:.3f}",
        )
    )

    # (5) Active spin follows movement x velocity / spin rate.
    r5 = _corr(mov_o * velo_o / spin_o, [t[6] for t in spin_rows])
    out.append(("active_spin_proxy", r5 >= 0.80, f"r {r5:.3f} (pass line 0.80)"))

    # (6) The deviation rebuilds from our two axes.
    dev_ours = _wrap(
        AXIS_CONVENTIONS["360-ours"](axis_o) - _inferred_candidates(ivb, hb)["180 - atan2(hb, ivb)"]
    )
    r6 = _corr(dev_ours, diff)
    out.append(("deviation_rebuilt", r6 >= 0.6, f"r {r6:.3f} (pass line 0.60)"))
    return out


def run_offline() -> int:
    checks = offline_checks(ARM_FIXTURE, SPIN_FIXTURE)
    for name, ok, detail in checks:
        print(f"{'OK' if ok else 'FAIL'} {name}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


# ---------------------------------------------------------------------------
# The CSV readers (the design session's layout)
# ---------------------------------------------------------------------------


def _read_csv(path: Path, encoding: str = "utf-8") -> list[dict[str, str]]:
    with path.open(encoding=encoding, newline="") as fh:
        return list(csv.DictReader(fh))


def read_arm(csv_dir: Path) -> dict[tuple[int, int], dict]:
    """(pitcher, season) -> the arm board's row, seasons 2020..2025."""
    arm: dict[tuple[int, int], dict] = {}
    for y in SEASONS:
        for row in _read_csv(csv_dir / f"arm_{y}.csv", "utf-8-sig"):
            arm[(int(row["pitcher"]), y)] = {
                "hand": row["pitch_hand"],
                "n": _f(row["n_pitches"]),
                "angle": _f(row["ball_angle"]),
                "rrx": _f(row["relative_release_ball_x"]),
                "rz": _f(row["release_ball_z"]),
                "rsx": _f(row["relative_shoulder_x"]),
                "sz": _f(row["shoulder_z"]),
            }
    return arm


def read_our_release(csv_dir: Path) -> dict[tuple[int, int], dict]:
    """(pitcher, season) -> our per-pitcher-season release means."""
    return {
        (int(row["pitcher"]), int(row["season"])): {
            "hand": row["p_throws"],
            "n": _f(row["n"]),
            "rx": _f(row["rx"]),
            "rz": _f(row["rz"]),
            "ext": _f(row["ext"]),
            "velo": _f(row["velo"]),
        }
        for row in _read_csv(csv_dir / "our_release.csv")
    }


def read_heights(csv_dir: Path) -> dict[int, float | None]:
    return {
        int(row["player_id"]): _f(row["height_inches"])
        for row in _read_csv(csv_dir / "heights.csv")
    }


def read_spindir(csv_dir: Path) -> dict[tuple[int, int, str], dict]:
    """(player, season, pitch type) -> the spin-direction board's row, seasons 2020..2025."""
    sd: dict[tuple[int, int, str], dict] = {}
    for y in SEASONS:
        for row in _read_csv(csv_dir / f"spindir_{y}.csv", "utf-8-sig"):
            sd[(int(row["player_id"]), y, row["api_pitch_type"])] = {
                "hand": row["pitch_hand"],
                "n": _f(row["n_pitches"]),
                "velo": _f(row["release_speed"]),
                "spin": _f(row["spin_rate"]),
                "mov": _f(row["movement_inches"]),
                "active": _f(row["active_spin"]),
                "hawk": _f(row["hawkeye_measured"]),
                "inferred": _f(row["movement_inferred"]),
                "diff": _f(row["diff_measured_inferred"]),
            }
    return sd


def read_our_pitchtypes(csv_dir: Path) -> dict[tuple[int, int, str], dict]:
    """(pitcher, season, pitch type) -> our per-pitch-type means."""
    return {
        (int(row["pitcher"]), int(row["season"]), row["pitch_type"]): {
            "hand": row["p_throws"],
            "n": _f(row["n"]),
            "velo": _f(row["velo"]),
            "ivb": _f(row["ivb"]),
            "hb": _f(row["hb"]),
            "spin": _f(row["spin"]),
            "axis": _f(row["axis_circ"]),
            "rx": _f(row["rx"]),
            "rz": _f(row["rz"]),
        }
        for row in _read_csv(csv_dir / "our_pitchtypes.csv")
    }


# ---------------------------------------------------------------------------
# Part A — the arm angle
# ---------------------------------------------------------------------------


def _fixed_shoulder_proxy(o: dict) -> float:
    """The geometric proxy with a fixed 4.4 ft shoulder: atan2(release z - 4.4, |release x|)."""
    return math.degrees(math.atan2(o["rz"] - 4.4, abs(o["rx"])))


def part_a(csv_dir: Path) -> None:
    arm = read_arm(csv_dir)
    ours = read_our_release(csv_dir)
    height = read_heights(csv_dir)

    print("=== A. ARM ANGLE ===")
    print("board rows by season:", {y: sum(1 for k in arm if k[1] == y) for y in SEASONS})

    # A1 Savant's own geometry: angle == atan2(release z - shoulder z, |release x - shoulder x|)?
    xs: list[float] = []
    ys: list[float] = []
    for (_pid, y), a in arm.items():
        if y != 2024 or a["n"] is None or a["n"] < 200:
            continue
        if None in (a["angle"], a["rrx"], a["rz"], a["rsx"], a["sz"]):
            continue
        xs.append(math.degrees(math.atan2(a["rz"] - a["sz"], abs(a["rrx"] - a["rsx"]))))
        ys.append(a["angle"])
    print(
        "A1 Savant's angle vs atan2(release z - shoulder z, |release x - shoulder x|), 2024, >=200 pitches:",
        _r(xs, ys),
        f"| mean abs diff {np.mean(np.abs(np.array(xs) - np.array(ys))):.2f} deg",
    )

    # A2 our proxy: OLS of Savant's angle on our mean release z, |release x|, extension, height
    rows = []
    for (pid, y), a in arm.items():
        o = ours.get((pid, y))
        if y not in (2023, 2024, 2025) or o is None or a["angle"] is None:
            continue
        if a["n"] is None or o["n"] is None or a["n"] < 200 or o["n"] < 200:
            continue
        rows.append(
            (
                y,
                a["hand"],
                a["angle"],
                o["rz"],
                abs(o["rx"]),
                o["ext"],
                height.get(pid),
                a["sz"],
                a["rz"],
                a["rrx"],
                a["rsx"],
            )
        )
    rows_h = [t for t in rows if t[6] is not None]
    print(
        f"A2 pitcher-seasons matched 2023-2025 (>=200 pitches both sides): {len(rows)}; with a height: {len(rows_h)}"
    )
    y_ = np.array([t[2] for t in rows_h])
    X4 = np.array([[t[3], t[4], t[5], t[6]] for t in rows_h])
    X3 = X4[:, :3]
    _, r2_3, sd3 = _ols(X3, y_)
    _, r2_4, sd4 = _ols(X4, y_)
    print(
        f"   OLS angle ~ our release z + |release x| + extension:            R2 {r2_3:.3f}, residual SD {sd3:.2f} deg (angle SD {y_.std():.2f})"
    )
    print(
        f"   OLS angle ~ the same + height:                                    R2 {r2_4:.3f}, residual SD {sd4:.2f} deg"
    )
    # the same regression, geometric form: atan2(rz - c*height_ft, |rx|)
    best: tuple[float, float] | None = None
    for c in np.arange(0.55, 0.80, 0.01):
        p = [math.degrees(math.atan2(t[3] - c * t[6] / 12.0, t[4])) for t in rows_h]
        rr = _corr(p, y_)
        if best is None or rr > best[1]:
            best = (float(c), rr)
    assert best is not None
    print(
        f"   geometric proxy atan2(our release z - {best[0]:.2f} x height, |our release x|): r {best[1]:.3f}"
    )
    p_no_h = [math.degrees(math.atan2(t[3] - 4.4, t[4])) for t in rows]
    print(
        f"   geometric proxy with a fixed 4.4 ft shoulder (no height), n {len(rows)}: r {_corr(p_no_h, [t[2] for t in rows]):.3f}"
    )
    # how much the release point alone (the mixture's view) leaves: shoulder z spread vs release z spread
    sz_sd = np.std([t[7] for t in rows_h])
    rz_sd = np.std([t[8] for t in rows_h])
    r_sz_h = _corr([t[7] for t in rows_h], [t[6] for t in rows_h])
    print(
        f"   Savant shoulder z SD {sz_sd:.2f} ft vs release z SD {rz_sd:.2f} ft; "
        f"r(shoulder z, height) {r_sz_h:.3f}"
    )
    # our release z vs Savant's release z (the same measurement?)
    print(
        "   our mean release z vs Savant's release_ball_z:",
        _r([t[3] for t in rows], [t[8] for t in rows]),
        "| our |release x| vs Savant's |relative release x - shoulder x|:",
        _r([t[4] for t in rows], [abs(t[9] - t[10]) for t in rows]),
    )

    # A3 year-to-year repeats
    for a_, b_ in ((2023, 2024), (2024, 2025)):
        xa, ya, xr, yr, xp, yp = [], [], [], [], [], []
        for (pid, y), a in arm.items():
            if y != a_ or (pid, b_) not in arm:
                continue
            bb = arm[(pid, b_)]
            if a["n"] is None or bb["n"] is None or a["n"] < 200 or bb["n"] < 200:
                continue
            if a["angle"] is None or bb["angle"] is None:
                continue
            xa.append(a["angle"])
            ya.append(bb["angle"])
            oa, ob = ours.get((pid, a_)), ours.get((pid, b_))
            if oa and ob and oa["n"] >= 200 and ob["n"] >= 200:
                xr.append(oa["rz"])
                yr.append(ob["rz"])
                xp.append(_fixed_shoulder_proxy(oa))
                yp.append(_fixed_shoulder_proxy(ob))
        print(
            f"A3 year-to-year {a_}->{b_} (>=200 pitches both): Savant angle {_r(xa, ya)} | our release z {_r(xr, yr)} | our geometric proxy {_r(xp, yp)}"
        )

    # A4 the 2024 spread by hand
    by_hand: dict[str, list[float]] = defaultdict(list)
    for (_pid, y), a in arm.items():
        if y == 2024 and a["n"] is not None and a["n"] >= 200 and a["angle"] is not None:
            by_hand[a["hand"]].append(a["angle"])
    print(
        "A4 2024 arm angle by hand: "
        + "; ".join(
            f"{h}: n {len(v)} mean {np.mean(v):.1f} SD {np.std(v):.1f} range {min(v):.0f}..{max(v):.0f}"
            for h, v in by_hand.items()
        )
    )


# ---------------------------------------------------------------------------
# Part B — the spin shape
# ---------------------------------------------------------------------------


def part_b(csv_dir: Path) -> None:
    sd = read_spindir(csv_dir)
    op = read_our_pitchtypes(csv_dir)

    print("\n=== B. SPIN SHAPE (the spin-direction board, per pitcher x pitch type) ===")
    print("board rows by season:", {y: sum(1 for k in sd if k[1] == y) for y in SEASONS})
    J = []
    for k, s in sd.items():
        o = op.get(k)
        if o is None or k[1] != 2024 or s["n"] < 50 or o["n"] < 50:
            continue
        if None in (
            s["velo"],
            s["spin"],
            s["mov"],
            s["active"],
            s["hawk"],
            s["inferred"],
            s["diff"],
        ):
            continue
        if None in (o["velo"], o["ivb"], o["hb"], o["spin"], o["axis"]):
            continue
        J.append((k, s, o))
    print(f"B0 2024 rows matched on (pitcher, pitch type), >=50 pitches both sides: {len(J)}")
    print(
        "B1 sanity - the same measurements? spin rate:",
        _r([s["spin"] for _, s, _ in J], [o["spin"] for _, _, o in J]),
        "| velocity:",
        _r([s["velo"] for _, s, _ in J], [o["velo"] for _, _, o in J]),
        "| movement (Savant inches vs our sqrt(ivb^2+hb^2)):",
        _r([s["mov"] for _, s, _ in J], [math.hypot(o["ivb"], o["hb"]) for _, _, o in J]),
    )

    # B2 axis conventions: our spin_axis (Statcast, 180 = 12:00 backspin) vs Savant's hawkeye_measured
    ax_s = [s["hawk"] for _, s, _ in J]
    ax_o = np.array([o["axis"] for _, _, o in J])
    conv = {k: _circ_r(ax_s, fn(ax_o)) for k, fn in AXIS_CONVENTIONS.items()}
    print(
        "B2 axis: mean cos(Savant hawkeye_measured - our spin_axis) under four conventions: "
        + " | ".join(f"{k} {v:.3f}" for k, v in conv.items())
    )
    # B3 the movement-inferred axis from our ivb/hb
    inf_s = [s["inferred"] for _, s, _ in J]
    cands = _inferred_candidates([o["ivb"] for _, _, o in J], [o["hb"] for _, _, o in J])
    cand_r = {k: _circ_r(inf_s, v) for k, v in cands.items()}
    print(
        "B3 Savant's movement-inferred axis vs a function of our ivb/hb: "
        + " | ".join(f"{k}: {v}" for k, v in cand_r.items())
    )

    # B4 active spin from our features (movement, velocity, spin rate)
    act = np.array([s["active"] for _, s, _ in J])
    mov_o = np.array([math.hypot(o["ivb"], o["hb"]) for _, _, o in J])
    velo_o = np.array([o["velo"] for _, _, o in J])
    spin_o = np.array([o["spin"] for _, _, o in J])
    proxy = mov_o * velo_o / spin_o
    print(
        f"B4 active spin vs the physical proxy (our movement x velocity / spin rate): r {_corr(proxy, act):.3f}"
    )
    _, r2, sdres = _ols(np.column_stack([np.log(mov_o), np.log(velo_o), np.log(spin_o)]), act)
    print(
        f"   OLS active spin ~ log(our movement) + log(velocity) + log(spin rate): R2 {r2:.3f}, residual SD {sdres:.3f} (active spin SD {act.std():.3f})"
    )
    # Savant's own inputs for the same fit (their movement/velo/spin): the ceiling
    _, r2s, _ = _ols(
        np.column_stack(
            [
                np.log([s["mov"] for _, s, _ in J]),
                np.log([s["velo"] for _, s, _ in J]),
                np.log([s["spin"] for _, s, _ in J]),
            ]
        ),
        act,
    )
    print(
        f"   the same fit on Savant's own movement/velocity/spin columns (the ceiling): R2 {r2s:.3f}"
    )

    # B5 the deviation (seam-shifted wake) from our features
    dev = np.array([s["diff"] for _, s, _ in J])
    ax_o_r = np.radians(ax_o)
    Xd = np.column_stack(
        [
            np.sin(ax_o_r),
            np.cos(ax_o_r),
            [o["ivb"] for _, _, o in J],
            [o["hb"] for _, _, o in J],
            velo_o,
            spin_o,
            [1.0 if o["hand"] == "L" else 0.0 for _, _, o in J],
        ]
    )
    _, r2d, sdd = _ols(Xd, dev)
    print(
        f"B5 the axis deviation (measured - inferred) ~ our axis (sin, cos) + ivb + hb + velocity + spin + hand: R2 {r2d:.3f}, residual SD {sdd:.1f} deg (deviation SD {dev.std():.1f})"
    )
    # the deviation IS measured - inferred: reproduce it from our spin_axis and our atan2(ivb, hb)
    best_conv = max(cand_r, key=lambda k: cand_r[k])
    hawk_conv = max(conv, key=lambda k: conv[k])
    dev_ours = _wrap(AXIS_CONVENTIONS[hawk_conv](ax_o) - cands[best_conv])
    print(
        f"   the deviation rebuilt from OUR spin axis ({hawk_conv}) minus OUR movement axis ({best_conv}): r {_corr(dev_ours, dev):.3f}"
    )

    # B6 year-to-year repeats of active spin and the deviation, per pitcher x pitch type
    for a_, b_ in ((2023, 2024), (2024, 2025)):
        xa, ya, xd, yd = [], [], [], []
        for (pid, y, pt), s in sd.items():
            if y != a_ or (pid, b_, pt) not in sd:
                continue
            t = sd[(pid, b_, pt)]
            if (
                s["n"] < 100
                or t["n"] < 100
                or None in (s["active"], t["active"], s["diff"], t["diff"])
            ):
                continue
            xa.append(s["active"])
            ya.append(t["active"])
            xd.append(s["diff"])
            yd.append(t["diff"])
        print(
            f"B6 year-to-year {a_}->{b_} (>=100 pitches of the type both seasons): active spin {_r(xa, ya)} | axis deviation {_r(xd, yd)}"
        )
    # the same for OUR features (the arsenal's own inputs), per pitcher x pitch type
    for a_, b_ in ((2023, 2024), (2024, 2025)):
        out: dict[str, tuple[int, float | None]] = {}
        for feat in ("velo", "ivb", "hb", "spin", "rz"):
            xs, ys = [], []
            for (pid, y, pt), o in op.items():
                if y != a_ or (pid, b_, pt) not in op:
                    continue
                t = op[(pid, b_, pt)]
                if o["n"] < 100 or t["n"] < 100 or o[feat] is None or t[feat] is None:
                    continue
                xs.append(o[feat])
                ys.append(t[feat])
            out[feat] = _r(xs, ys)
        print(
            f"   our per-pitch-type means {a_}->{b_}: "
            + ", ".join(f"{k} {v[1]}" for k, v in out.items())
            + f" (n {out['velo'][0]})"
        )


# ---------------------------------------------------------------------------
# Part C — the pair test against the production score
# ---------------------------------------------------------------------------

PAIR_COLUMNS = [
    "1-score",
    "d_out_bb",
    "d_out_all",
    "d_angle",
    "d_angle_resid",
    "d_height",
    "d_active_fb",
    "d_dev_fb",
    "d_active_br",
    "d_dev_br",
]
OUTCOME_UNREAD = ("ground_ball_rate", "fly_ball_rate", "line_drive_rate", "hr_per_9")
OUTCOME_ALL = (
    "k_rate",
    "bb_rate",
    "whiff_rate",
    "csw_rate",
    "chase_rate",
    "zone_rate",
    "ground_ball_rate",
    "fly_ball_rate",
    "hr_per_9",
)


def part_c(csv_dir: Path) -> None:
    rng = np.random.default_rng(HALF_SPLIT_SEED)
    z = np.load(csv_dir / "pitcher_sim.npz", allow_pickle=True)
    index = json.loads(str(z["index"]))
    M = z["pitcher_sim_matrix"]
    print("\n=== C. THE PAIR TEST (what each number adds to the production score) ===")
    print(
        "matrix",
        M.shape,
        "| seasons in the index:",
        sorted({k.split(":")[1] for k in index}),
        "| symmetric max |M-M.T|:",
        float(np.abs(M - M.T).max()),
    )

    # outcomes 2024 (>= 500 pitches), z-scored across the 2024 population
    out: dict[int, dict] = {}
    for r in _read_csv(csv_dir / "outcomes.csv"):
        if (
            int(r["season"]) != 2024
            or _f(r["sample_pitches"]) is None
            or float(r["sample_pitches"]) < 500
        ):
            continue
        v_bb = [_f(r[c]) for c in OUTCOME_UNREAD]
        v_all = [_f(r[c]) for c in OUTCOME_ALL]
        if None in v_bb or None in v_all:
            continue
        out[int(r["pitcher_id"])] = {
            "hand": r["p_throws"],
            "bb": np.array(v_bb),
            "all": np.array(v_all),
        }
    for key in ("bb", "all"):
        A = np.array([o[key] for o in out.values()])
        mu, sd = A.mean(0), A.std(0)
        for o in out.values():
            o[key] = (o[key] - mu) / sd

    # arm angle 2024 (>= 200 pitches) and its residual after our release geometry + height
    arm: dict[int, dict] = {}
    for r in _read_csv(csv_dir / "arm_2024.csv", "utf-8-sig"):
        if _f(r["n_pitches"]) and float(r["n_pitches"]) >= 200 and _f(r["ball_angle"]) is not None:
            arm[int(r["pitcher"])] = {
                "angle": _f(r["ball_angle"]),
                "sz": _f(r["shoulder_z"]),
                "n": _f(r["n_pitches"]),
            }
    ours: dict[int, tuple] = {}
    for r in _read_csv(csv_dir / "our_release.csv"):
        if int(r["season"]) == 2024 and _f(r["n"]) and float(r["n"]) >= 200:
            ours[int(r["pitcher"])] = (_f(r["rz"]), abs(_f(r["rx"])), _f(r["ext"]))  # type: ignore[arg-type]
    height = read_heights(csv_dir)
    ids = [p for p in arm if p in ours and height.get(p)]
    X = np.array([[1.0, *ours[p], height[p]] for p in ids])
    y = np.array([arm[p]["angle"] for p in ids])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    print(
        f"arm-angle residual after our release z, |x|, extension + height (2024, n {len(ids)}): SD {resid.std():.2f} deg of {y.std():.2f}"
    )
    for p, rr in zip(ids, resid, strict=True):
        arm[p]["resid"] = float(rr)

    # fastball and breaking-ball spin shape from the spin-direction board (2024): the type
    # with the most pitches in each group
    spin: dict[int, dict[str, tuple]] = {}
    for r in _read_csv(csv_dir / "spindir_2024.csv", "utf-8-sig"):
        pid, pt, n = int(r["player_id"]), r["api_pitch_type"], _f(r["n_pitches"])
        if (
            n is None
            or n < 100
            or _f(r["active_spin"]) is None
            or _f(r["diff_measured_inferred"]) is None
        ):
            continue
        grp = "fb" if pt in FASTBALLS else ("br" if pt in BREAKING_BALLS else None)
        if grp is None:
            continue
        cur = spin.setdefault(pid, {})
        if grp not in cur or cur[grp][0] < n:
            cur[grp] = (n, _f(r["active_spin"]), _f(r["diff_measured_inferred"]))

    # pairs
    keys = {p: index.get(f"{p}:2024") for p in out}
    pids = [p for p in out if keys[p] is not None and p in arm and "resid" in arm[p]]
    print(
        f"2024 pitchers with a matrix row, outcomes (>=500 pitches) and an arm angle: {len(pids)}"
    )

    def _score(pa: int, pb: int) -> float:
        i, j = keys[pa], keys[pb]
        return 0.5 * (float(M[i, j]) + float(M[j, i]))

    def _gap(sa: dict, sb: dict, grp: str, slot: int) -> float:
        if grp in sa and grp in sb:
            return abs(sa[grp][slot] - sb[grp][slot])
        return float("nan")

    rows = []
    pair_pids = []
    for a_i, pa in enumerate(pids):
        for pb in pids[a_i + 1 :]:
            if out[pa]["hand"] != out[pb]["hand"]:
                continue
            s = _score(pa, pb)
            if s <= 0.0:
                continue
            sa, sb = spin.get(pa, {}), spin.get(pb, {})
            pair_pids.append((pa, pb))
            rows.append(
                (
                    out[pa]["hand"],
                    1.0 - s,
                    float(np.linalg.norm(out[pa]["bb"] - out[pb]["bb"])),
                    float(np.linalg.norm(out[pa]["all"] - out[pb]["all"])),
                    abs(arm[pa]["angle"] - arm[pb]["angle"]),
                    abs(arm[pa]["resid"] - arm[pb]["resid"]),
                    abs(height[pa] - height[pb]),  # type: ignore[operator]
                    _gap(sa, sb, "fb", 1),
                    _gap(sa, sb, "fb", 2),
                    _gap(sa, sb, "br", 1),
                    _gap(sa, sb, "br", 2),
                )
            )
    R = np.array([r[1:] for r in rows], dtype=float)
    hands = np.array([r[0] for r in rows])
    col = {n: i for i, n in enumerate(PAIR_COLUMNS)}
    print(
        f"same-hand pairs: {len(rows)} (R {int((hands == 'R').sum())}, L {int((hands == 'L').sum())})"
    )

    def ols_r2(Xcols: list[str], ycol: str, mask=None) -> tuple[float, list[float], int]:
        m = np.ones(len(R), bool) if mask is None else mask.copy()
        for c in [*Xcols, ycol]:
            m &= np.isfinite(R[:, col[c]])
        Xm = np.column_stack([np.ones(m.sum())] + [R[m, col[c]] for c in Xcols])
        ym = R[m, col[ycol]]
        b, *_ = np.linalg.lstsq(Xm, ym, rcond=None)
        res = ym - Xm @ b
        r2 = 1 - res.var() / ym.var()
        # standardized coefficients
        std_b = [b[k + 1] * R[m, col[c]].std() / ym.std() for k, c in enumerate(Xcols)]
        return float(r2), std_b, int(m.sum())

    for ycol, label in (
        ("d_out_bb", "outcomes the score does NOT read (GB, FB, LD, HR/9)"),
        ("d_out_all", "all nine outcome rates (the command sub-score reads six of them)"),
    ):
        print(f"\n=== outcome distance: {label} ===")
        parts = []
        for c in PAIR_COLUMNS:
            if c in ("d_out_bb", "d_out_all"):
                continue
            m = np.isfinite(R[:, col[c]])
            parts.append(f"{c} {_corr(R[m, col[c]], R[m, col[ycol]]):+.3f}")
        print("univariate r with the outcome distance: " + " | ".join(parts))
        base, _, n = ols_r2(["1-score"], ycol)
        print(f"R2 of (1 - the production score) alone: {base:.4f} (n {n})")
        for add in (
            ["d_angle"],
            ["d_angle_resid"],
            ["d_height"],
            ["d_angle", "d_angle_resid"],
            ["d_active_fb"],
            ["d_dev_fb"],
            ["d_active_br"],
            ["d_dev_br"],
            ["d_angle", "d_active_fb", "d_dev_fb"],
        ):
            same = np.all(np.isfinite(R[:, [col[c] for c in add]]), axis=1)
            b0, _, _ = ols_r2(["1-score"], ycol, mask=same)
            r2, sbs, n = ols_r2(["1-score", *add], ycol)
            print(
                f"  + {'+'.join(add):32s}: R2 {r2:.4f} (base on the same pairs {b0:.4f}, gain {r2 - b0:+.4f}; n {n}); "
                "standardized coefficients "
                + ", ".join(f"{c} {v:+.3f}" for c, v in zip(["1-score", *add], sbs, strict=True))
            )

    # how much the score already tracks the arm-angle gap
    m = np.isfinite(R[:, col["d_angle"]])
    print(
        f"\nr(1 - score, |delta arm angle|) = {_corr(R[m, col['1-score']], R[m, col['d_angle']]):.3f}; "
        f"r(1 - score, |delta residual angle|) = {_corr(R[m, col['1-score']], R[m, col['d_angle_resid']]):.3f}"
    )
    # stability: two random halves of the pitchers (seed 533)
    pid_arr = np.array(pids)
    half = set(rng.choice(pid_arr, len(pid_arr) // 2, replace=False).tolist())
    assert len(pair_pids) == len(rows)
    for hname, sel in (
        ("half A", np.array([pa in half and pb in half for pa, pb in pair_pids])),
        ("half B", np.array([pa not in half and pb not in half for pa, pb in pair_pids])),
    ):
        b0, _, _ = ols_r2(["1-score"], "d_out_bb", mask=sel)
        r2, sbs, n = ols_r2(["1-score", "d_angle"], "d_out_bb", mask=sel)
        print(
            f"{hname} (pitchers split at random, n pairs {n}): gain from |delta arm angle| on the unread outcomes {r2 - b0:+.4f}, standardized coefficient {sbs[1]:+.3f}"
        )


# ---------------------------------------------------------------------------
# The board pulls and their probes
# ---------------------------------------------------------------------------


def pull_urls() -> dict[str, str]:
    """The file name -> URL map of one ``--pull``."""
    urls: dict[str, str] = {}
    for y in (*SEASONS, EMPTY_SEASON):
        urls[f"arm_{y}.csv"] = ARM_URL.format(Y=y)
        urls[f"spindir_{y}.csv"] = SPINDIR_URL.format(Y=y)
    urls["arm_2024_year.csv"] = ARM_URL.format(Y=2024).replace("season=2024", "year=2023")
    urls["arm_2024_default.csv"] = ARM_URL.format(Y=2024).replace("min=1&", "")
    urls["arm_noseason.csv"] = ARM_URL.format(Y=2024).replace("season=2024&", "")
    urls["spindir_2024_default.csv"] = SPINDIR_URL.format(Y=2024).replace("min=1&", "")
    urls["active_2024.csv"] = ACTIVE_URL.format(Y=2024)
    urls[f"active_{EMPTY_SEASON}.csv"] = ACTIVE_URL.format(Y=EMPTY_SEASON)
    return urls


def pull_boards(csv_dir: Path) -> None:
    csv_dir.mkdir(parents=True, exist_ok=True)
    for name, url in pull_urls().items():
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=PULL_TIMEOUT_S) as resp:
            body = resp.read()
        (csv_dir / name).write_bytes(body)
        print(f"pulled {name}: {len(body)} bytes")


def board_rows(path: Path) -> int | None:
    """The row count of a board file. A header-only file and a wholly empty body both count
    as zero rows; an HTML body returns None (the board answered with its page)."""
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    if text.lstrip()[:1] == "<" or "<html" in text[:2000].lower():
        return None
    if not text.strip():
        return 0
    return sum(1 for _ in csv.DictReader(text.splitlines()))


def board_probes(csv_dir: Path) -> None:
    print("=== the board probes ===")
    names = (
        [f"arm_{y}.csv" for y in SEASONS]
        + [f"spindir_{y}.csv" for y in SEASONS]
        + ["active_2024.csv"]
    )
    for name in names:
        p = csv_dir / name
        if p.exists():
            print(f"  {name}: {board_rows(p)} rows")
    for name in (
        f"arm_{EMPTY_SEASON}.csv",
        f"spindir_{EMPTY_SEASON}.csv",
        f"active_{EMPTY_SEASON}.csv",
    ):
        p = csv_dir / name
        if not p.exists():
            print(f"  {name}: missing")
            continue
        n = board_rows(p)
        if n is None:
            print(f"  FAIL {name}: an HTML body, not a CSV")
        elif n == 0:
            print(f"  OK {name}: 0 rows (an empty body; the boards start in 2020)")
        else:
            print(f"  FAIL {name}: {n} rows, expected 0")
    year_p, none_p = csv_dir / "arm_2024_year.csv", csv_dir / "arm_noseason.csv"
    if year_p.exists():
        n_year = board_rows(year_p)
        if none_p.exists():
            n_none = board_rows(none_p)
            verdict = "OK" if n_year == n_none else "FAIL"
            print(
                f"  {verdict} arm year=2023 pull: {n_year} rows vs the no-season pull {n_none} rows (year= ignored: serves the current season)"
            )
        else:
            print(
                f"  arm year=2023 pull: {n_year} rows (year= ignored: serves the current season; no arm_noseason.csv to compare, pull to check)"
            )
    for a, b, note in (
        (
            "arm_2024.csv",
            "arm_2024_default.csv",
            "season=2024 with min=1 vs the default (823 vs 284 in the design session)",
        ),
        (
            "spindir_2024.csv",
            "spindir_2024_default.csv",
            "year=2024 with min=1 vs the default (3,180 vs 719 in the design session)",
        ),
    ):
        pa, pb = csv_dir / a, csv_dir / b
        if pa.exists() and pb.exists():
            print(f"  {a} {board_rows(pa)} rows vs {b} {board_rows(pb)} rows: {note}")


# ---------------------------------------------------------------------------
# The dump of our four inputs from the live stack
# ---------------------------------------------------------------------------

Q_OUR_RELEASE = (
    "SELECT pitcher, season, p_throws, count(*) AS n, avg(release_pos_x) AS rx, "
    "avg(release_pos_z) AS rz, avg(release_extension) AS ext, stddev(release_pos_z) AS rz_sd, "
    "stddev(release_pos_x) AS rx_sd, avg(release_speed) AS velo FROM raw.pitches "
    "WHERE season BETWEEN 2020 AND 2025 AND data_quality_flag = FALSE "
    "AND release_pos_x IS NOT NULL AND release_pos_z IS NOT NULL GROUP BY 1,2,3"
)
Q_OUR_PITCHTYPES = (
    "SELECT pitcher, season, p_throws, pitch_type, count(*) AS n, avg(release_speed) AS velo, "
    "avg(break_vertical_induced) AS ivb, avg(break_horizontal) AS hb, "
    "avg(release_spin_rate) AS spin, "
    "degrees(atan2(avg(sin(radians(spin_axis))), avg(cos(radians(spin_axis))))) AS axis_circ, "
    "count(spin_axis) AS n_axis, avg(release_pos_x) AS rx, avg(release_pos_z) AS rz, "
    "avg(release_extension) AS ext FROM raw.pitches "
    "WHERE season BETWEEN 2020 AND 2025 AND data_quality_flag = FALSE "
    "AND pitch_type IS NOT NULL GROUP BY 1,2,3,4 HAVING count(*) >= 20"
)
Q_HEIGHTS = (
    "SELECT player_id, height_inches, weight_lbs FROM raw.players WHERE height_inches IS NOT NULL"
)
Q_OUTCOMES = (
    "SELECT pitcher_id, season, p_throws, sample_pitches, k_rate, bb_rate, whiff_rate, csw_rate, "
    "chase_rate, zone_rate, zone_take_rate, ground_ball_rate, fly_ball_rate, line_drive_rate, "
    "hr_per_9 FROM derived.pitcher_season_metrics WHERE season BETWEEN 2020 AND 2025"
)


def _write_rows(path: Path, columns: list[str], rows) -> int:
    n = 0
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(columns)
        for row in rows:
            w.writerow(["" if v is None else v for v in row])
            n += 1
    return n


def dump_inputs(csv_dir: Path, dsn: str, duckdb_path: str, matrix_path: str) -> None:
    """Our four inputs and the matrix into ``csv_dir``. The DuckDB open is read-only and
    runs with the app up; only a writer (a recompute or a rebuild) blocks it."""
    import duckdb
    import psycopg2

    csv_dir.mkdir(parents=True, exist_ok=True)
    pg = psycopg2.connect(dsn)
    try:
        for name, query in (
            ("our_release.csv", Q_OUR_RELEASE),
            ("our_pitchtypes.csv", Q_OUR_PITCHTYPES),
            ("heights.csv", Q_HEIGHTS),
        ):
            cur = pg.cursor()
            cur.execute(query)
            columns = [d[0] for d in cur.description]
            n = _write_rows(csv_dir / name, columns, cur.fetchall())
            print(f"dumped {name}: {n} rows")
    finally:
        pg.close()
    try:
        con = duckdb.connect(duckdb_path, read_only=True)
    except duckdb.IOException as exc:
        raise SystemExit(
            f"cannot open {duckdb_path} read-only ({exc}): a writer (a profile recompute or "
            "a pool rebuild) holds the file; wait for it or re-run --dump afterwards."
        ) from exc
    try:
        cur = con.execute(Q_OUTCOMES)
        columns = [d[0] for d in cur.description]
        n = _write_rows(csv_dir / "outcomes.csv", columns, cur.fetchall())
        print(f"dumped outcomes.csv: {n} rows")
    finally:
        con.close()
    shutil.copyfile(matrix_path, csv_dir / "pitcher_sim.npz")
    print(f"copied {matrix_path} -> {csv_dir / 'pitcher_sim.npz'}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _skipped(part: str, missing: list[Path], csv_dir: Path) -> None:
    """The one line a part prints when its ``--dump`` inputs are absent."""
    names = ", ".join(p.name for p in missing)
    print(f"\n=== {part} skipped: {names} missing in {csv_dir} (run --dump) ===")


def analyse(csv_dir: Path) -> None:
    board_probes(csv_dir)
    print()
    release, heights = csv_dir / "our_release.csv", csv_dir / "heights.csv"
    pitchtypes = csv_dir / "our_pitchtypes.csv"
    if release.exists() and heights.exists():
        part_a(csv_dir)
    else:
        _skipped("A. THE ARM ANGLE", [p for p in (release, heights) if not p.exists()], csv_dir)
    if pitchtypes.exists():
        part_b(csv_dir)
    else:
        _skipped("B. THE SPIN SHAPE", [pitchtypes], csv_dir)
    matrix, outcomes = csv_dir / "pitcher_sim.npz", csv_dir / "outcomes.csv"
    part_c_inputs = (matrix, outcomes, release, heights)
    if all(p.exists() for p in part_c_inputs):
        part_c(csv_dir)
    else:
        _skipped("C. THE PAIR TEST", [p for p in part_c_inputs if not p.exists()], csv_dir)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--offline",
        action="store_true",
        help="the six fixture checks only; no network, no DB, no matrix",
    )
    ap.add_argument(
        "--csv-dir",
        default=None,
        help="the directory with the boards, our four inputs and the matrix",
    )
    ap.add_argument(
        "--pull",
        action="store_true",
        help="with --csv-dir: fetch the boards into the directory first",
    )
    ap.add_argument(
        "--dump",
        action="store_true",
        help="with --csv-dir: write our four inputs + the matrix from the live stack",
    )
    ap.add_argument(
        "--dsn",
        default=DEFAULT_DSN,
        help="Postgres (raw.pitches, raw.players); default BASEBALL_DB_DSN",
    )
    ap.add_argument(
        "--duckdb-path",
        default=DEFAULT_DUCKDB,
        help="DuckDB (derived.pitcher_season_metrics), opened read-only",
    )
    ap.add_argument(
        "--matrix-path", default=DEFAULT_MATRIX, help="the production pitcher_sim.npz to copy"
    )
    args = ap.parse_args(argv)

    if args.offline:
        return run_offline()
    if not args.csv_dir:
        print(
            "pass --offline, or --csv-dir DIR (with --pull and/or --dump to fill it)",
            file=sys.stderr,
        )
        return 2
    csv_dir = Path(args.csv_dir)
    if args.pull:
        pull_boards(csv_dir)
    if args.dump:
        if not args.dsn:
            print("no Postgres DSN (BASEBALL_DB_DSN or --dsn)", file=sys.stderr)
            return 2
        dump_inputs(csv_dir, args.dsn, args.duckdb_path, args.matrix_path)
    analyse(csv_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
