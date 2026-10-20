"""The sampler's module-level helpers.

The power helpers raise a weight factor to a fitted power and keep an unscored
row draw-neutral. The pitch engine's feature weights score the pitch-result
draw. The grouping helpers index the steal pool by count.
"""

from __future__ import annotations

import numpy as np

#: The pitch-to-pitch engine's feature weights, in the pitch pool's
#: ``_GEOM_COLS`` order (velo, ivb, hb, spin_rate, spin_axis, release_x,
#: release_z, release_ext, plate_x, plate_z). A copy, so the sampler never
#: imports FAISS; a unit test pins it to the engine's ``FEATURE_WEIGHTS``.
_PITCH_FEATURE_WEIGHTS = np.array(
    [1.20, 1.10, 1.00, 0.70, 0.50, 0.60, 0.60, 0.40, 0.90, 0.90], dtype=np.float32
)


def _group_rows(key: np.ndarray, rows: np.ndarray) -> dict[int, np.ndarray]:
    """The ``rows`` of a pool grouped by ``key`` (one int per pool row) ->
    {key: the group's row indices}. A stable sort keeps each group's rows in
    pool order."""
    k = key[rows]
    order = np.argsort(k, kind="stable")
    ks = k[order]
    uniq, starts = np.unique(ks, return_index=True)
    ends = np.append(starts[1:], len(ks))
    return {int(u): rows[order[a:b]] for u, a, b in zip(uniq, starts, ends, strict=True)}


def _count_cell(k: int) -> tuple[int, int, int]:
    """Decode the steal pool's count key ``outs*100 + balls*10 + strikes``."""
    return (k // 100, (k % 100) // 10, k % 10)


def _repower_neutral(
    w: np.ndarray,
    f: np.ndarray,
    unscored: np.ndarray | None,
    power: float,
    neutral: float,
) -> np.ndarray:
    """``w`` with its factor ``f`` raised to ``power``, unscored rows draw-neutral.

    ``w`` already carries ``f`` once, and ``f`` reads 1.0 on a row whose actor
    has no score. A scored row is multiplied by f^(power-1); a zero factor stays
    zero. An unscored row is multiplied by ``neutral``, the mean scored
    f^power, so it keeps the mean scored weight instead of the maximum: at
    power 12 a 3% unscored share would otherwise outweigh the whole scored
    pool. Power 1.0 returns ``w`` itself."""
    if power == 1.0:
        return w
    adj = np.where(f > 0.0, np.power(np.maximum(f, 1e-30), np.float32(power - 1.0)), 0.0)
    if unscored is not None and unscored.any():
        adj = np.where(unscored, np.float32(neutral), adj)
    return (w * adj).astype(np.float32)


def _raise_neutral(
    f: np.ndarray, unscored: np.ndarray | None, power: float, neutral: float
) -> np.ndarray:
    """f^power, with the unscored rows (``f`` reads 1.0 there) set to
    ``neutral``, the mean scored f^power, so they stay draw-neutral. Power 1.0
    returns ``f`` itself."""
    if power == 1.0:
        return f
    out = np.power(np.clip(f, 0.0, None), np.float32(power)).astype(np.float32)
    if unscored is not None and unscored.any():
        out[unscored] = np.float32(neutral)
    return out
