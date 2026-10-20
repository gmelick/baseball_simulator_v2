"""The sampler's module-level helpers.

The power helpers re-raise a weight factor to a fitted power and keep an
unscored row draw-neutral. The pitch engine's feature weights score the
pitch-result draw. The grouping helpers index the steal pool by count.
"""

from __future__ import annotations

import numpy as np

_OUTCOMES = ("ball", "called_strike", "swinging_strike", "foul", "in_play", "hit_by_pitch")

#: SIM-523 part B: the pitch-to-pitch engine's feature weights, in the pitch
#: pool's ``_GEOM_COLS`` order (velo, ivb, hb, spin_rate, spin_axis, release_x,
#: release_z, release_ext, plate_x, plate_z). Copied so the sampler never
#: imports FAISS; a unit test pins them to the engine's ``FEATURE_WEIGHTS``.
_PITCH_FEATURE_WEIGHTS = np.array(
    [1.20, 1.10, 1.00, 0.70, 0.50, 0.60, 0.60, 0.40, 0.90, 0.90], dtype=np.float32
)


def _group_rows(key: np.ndarray, rows: np.ndarray) -> dict[int, np.ndarray]:
    """SIM-554: the ``rows`` of a pool grouped by ``key`` (one int per pool
    row) -> {key: the group's row indices}. A stable sort keeps the rows of a
    group in pool order, so with ``rows`` = every row it rebuilds the SIM-474
    count index exactly."""
    k = key[rows]
    order = np.argsort(k, kind="stable")
    ks = k[order]
    uniq, starts = np.unique(ks, return_index=True)
    ends = np.append(starts[1:], len(ks))
    return {int(u): rows[order[a:b]] for u, a, b in zip(uniq, starts, ends, strict=True)}


def _count_cell(k: int) -> tuple[int, int, int]:
    """Decode the steal pool's count key ``outs*100 + balls*10 + strikes``."""
    return (k // 100, (k % 100) // 10, k % 10)


def _repower(w: np.ndarray, f: np.ndarray, power: float) -> np.ndarray:
    """SIM-523 part B: ``w`` already carries the factor ``f`` once; return
    ``w`` with that factor raised to ``power`` instead (× f^(power-1)). A
    zero factor stays zero at any power (its row has zero weight already);
    power 1.0 returns ``w`` itself, untouched."""
    if power == 1.0:
        return w
    adj = np.where(f > 0.0, np.power(np.maximum(f, 1e-30), np.float32(power - 1.0)), 0.0)
    return (w * adj).astype(np.float32)


def _repower_neutral(
    w: np.ndarray,
    f: np.ndarray,
    unscored: np.ndarray | None,
    power: float,
    neutral: float,
) -> np.ndarray:
    """SIM-523 part F: :func:`_repower` with the draw-NEUTRAL rule. ``w``
    carries ``f`` once, and ``f`` reads 1.0 on the rows whose actor has no
    score; a scored row is re-raised by f^(power-1), an unscored row is
    multiplied by ``neutral`` — the mean scored f^power — so at any power it
    keeps the MEAN scored weight instead of the maximum (at power 12 a 3%
    unscored share would otherwise outweigh the whole scored pool). power
    1.0 returns ``w`` itself (byte-identical)."""
    if power == 1.0:
        return w
    adj = np.where(f > 0.0, np.power(np.maximum(f, 1e-30), np.float32(power - 1.0)), 0.0)
    if unscored is not None and unscored.any():
        adj = np.where(unscored, np.float32(neutral), adj)
    return (w * adj).astype(np.float32)


def _raise_neutral(
    f: np.ndarray, unscored: np.ndarray | None, power: float, neutral: float
) -> np.ndarray:
    """SIM-523 part F: f^power with the unscored rows (``f`` reads 1.0 there)
    set to ``neutral`` — the mean scored f^power — so they stay draw-neutral.
    power 1.0 returns ``f`` itself (byte-identical)."""
    if power == 1.0:
        return f
    out = np.power(np.clip(f, 0.0, None), np.float32(power)).astype(np.float32)
    if unscored is not None and unscored.any():
        out[unscored] = np.float32(neutral)
    return out


#: SIM-512: positional number -> the fielder-embedding position name. Keep in
#: sync with ``simulation.sim_loop._POS_NUM_TO_STR`` — the fielder embedding
#: keys are ``"{player_id}:{position}:{season}"`` with these names.
_POS_NUM_TO_NAME: dict[int, str] = {
    1: "P",
    2: "C",
    3: "1B",
    4: "2B",
    5: "3B",
    6: "SS",
    7: "LF",
    8: "CF",
    9: "RF",
}
