"""
simulation/filter_cells.py — the hard-filter cell algebra (SIM-451 / SIM-467 / SIM-475).

ONE definition of the pitch-draw filter cell, shared by the measurement script
(``scripts/measure_filter_cells.py``, SIM-451), the sampler's cell index
(:class:`simulation.full_pool_sampler.FullPoolSampler`, SIM-467) and the
thin-cell widening ladder (SIM-475). The script used to own these constants;
``scripts/`` is not importable from the container's ``simulation`` package, so
the algebra moved here and the script re-exports it. A cell id computed under
different edges is not comparable with the SIM-451 report, so nothing here may
change without re-running that measurement.

The cell: base occupancy (8) × outs (3) × count (12) × score band (5) ×
batting side (2) = 2,880 cells. The encode runs most-significant dimension
first, so sorting by cell id groups the cells by base state.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

N_BASE = 8  # runners_state bitmask: bit0=1B, bit1=2B, bit2=3B
N_OUTS = 3
N_COUNT = 12  # 4 ball states x 3 strike states
N_BAND = 5
N_HOME = 2
N_CELLS = N_BASE * N_OUTS * N_COUNT * N_BAND * N_HOME  # 2880

#: SIM-451 ORIGINATES the score-band split. These are the symmetric 5-band split
#: of the +/-5 clamp the sim already applies. SIM-467 and SIM-475 import this
#: constant instead of redefining the edges. Read it as: band = the number of
#: edges the clamped score difference exceeds.
SCORE_BAND_EDGES: tuple[int, ...] = (-3, -1, 0, 2)
SCORE_BAND_LABELS: tuple[str, ...] = ("<=-3", "-2..-1", "0", "+1..+2", ">=+3")


def _validate_score_band_edges(edges: tuple[int, ...]) -> None:
    """Raise when the band edges are not strictly increasing.

    :func:`score_band` counts how many edges the score difference exceeds, which
    ignores the order of the tuple. Any reader who expects a first-match ladder
    reads a different band from an unsorted tuple. The guard removes that whole
    class of silent disagreement by refusing to load an unsorted tuple.
    """
    listed = list(edges)
    if listed != sorted(set(listed)):
        raise ValueError(f"SCORE_BAND_EDGES must be strictly increasing and unique; got {edges!r}")


_validate_score_band_edges(SCORE_BAND_EDGES)

SCORE_CLAMP = 5  # the loop clamps score_diff to +/-5 before the sampler sees it

#: The candidate minimum cell sizes SIM-475 chooses between.
DEFAULT_MIN_CELLS: tuple[int, ...] = (20, 50, 100, 200)

#: SIM-467: the minimum sub-cell size below which the widening ladder steps
#: (decision #19, 2026-08-10: score band first, then the batting side, then the
#: count). 20 is the plan's starting arm — about the p10 cell on the W1 window
#: (docs/audit/2026-09-04-sim467-518-plan.md §5.1); the certified value follows
#: the lane's draw-weighted widening share. ``SIM_PITCH_MIN_CELL`` overrides it.
DEFAULT_MIN_CELL = 20

#: The human-readable encode formula, carried into the JSON so a later reader can
#: reproduce a cell id without this file.
CELL_KEY_FORMULA = (
    "((((runners_state * 3 + outs) * 12 + count_bucket) * 5 + score_band) * 2 + bat_is_home)"
)

_BASE_LABELS = ("---", "1--", "-2-", "12-", "--3", "1-3", "-23", "123")


def score_band(score_diff: int) -> int:
    """Return the 0..4 score band of a batting-team run difference.

    The clamp copies the loop: the sim compresses every difference beyond five
    runs onto the +/-5 boundary, so a 9-run deficit and a 5-run deficit are the
    same situation to the sampler. The pool builder already stores the clamped
    value, so the clamp is a no-op on pool data and a correctness guard on any
    other caller.
    """
    sd = max(-SCORE_CLAMP, min(SCORE_CLAMP, int(score_diff)))
    return sum(1 for edge in SCORE_BAND_EDGES if sd > edge)


def score_band_array(score_diff: np.ndarray) -> np.ndarray:
    """:func:`score_band`, vectorized over a pool column (int64 output)."""
    sd = np.clip(np.asarray(score_diff).astype(np.int64), -SCORE_CLAMP, SCORE_CLAMP)
    band = np.zeros(sd.shape, dtype=np.int64)
    for edge in SCORE_BAND_EDGES:
        band += sd > edge
    return band


def count_bucket(balls: int, strikes: int) -> int:
    """Return the 0..11 count bucket — the sampler's own definition.

    The saturating min/max is part of the definition. It maps an out-of-range
    count onto the nearest legal one instead of raising.
    """
    return min(max(int(balls), 0), 3) * 3 + min(max(int(strikes), 0), 2)


def cell_key(
    runners_state: int,
    outs: int,
    balls: int,
    strikes: int,
    score_diff: int,
    bat_is_home: int,
) -> int:
    """Return the 0..2879 cell id for one situation.

    The encode runs most-significant dimension first, so sorting by cell id
    groups the cells by base state. :func:`decode_cell` is its exact inverse.
    """
    rs = int(runners_state) & 0b111
    o = min(max(int(outs), 0), N_OUTS - 1)
    cb = count_bucket(balls, strikes)
    band = score_band(score_diff)
    home = 1 if int(bat_is_home) else 0
    return ((((rs * N_OUTS + o) * N_COUNT + cb) * N_BAND + band) * N_HOME) + home


@dataclass(frozen=True)
class CellCoords:
    """The decoded coordinates of one filter cell."""

    runners_state: int
    outs: int
    balls: int
    strikes: int
    band: int
    bat_is_home: int

    @property
    def label(self) -> str:
        """Render the cell for a human, e.g. ``bases=123 outs=2 count=3-2 band=<=-3 HOME``."""
        return (
            f"bases={_BASE_LABELS[self.runners_state]} outs={self.outs} "
            f"count={self.balls}-{self.strikes} band={SCORE_BAND_LABELS[self.band]} "
            f"{'HOME' if self.bat_is_home else 'AWAY'}"
        )


def decode_cell(cell_id: int) -> CellCoords:
    """Return the coordinates of a cell id. The exact inverse of :func:`cell_key`."""
    cid = int(cell_id)
    if not 0 <= cid < N_CELLS:
        raise ValueError(f"cell_id {cid} out of range 0..{N_CELLS - 1}")
    home = cid % N_HOME
    cid //= N_HOME
    band = cid % N_BAND
    cid //= N_BAND
    cb = cid % N_COUNT
    cid //= N_COUNT
    outs = cid % N_OUTS
    rs = cid // N_OUTS
    return CellCoords(
        runners_state=rs,
        outs=outs,
        balls=cb // 3,
        strikes=cb % 3,
        band=band,
        bat_is_home=home,
    )


__all__ = [
    "CELL_KEY_FORMULA",
    "DEFAULT_MIN_CELL",
    "DEFAULT_MIN_CELLS",
    "N_BAND",
    "N_BASE",
    "N_CELLS",
    "N_COUNT",
    "N_HOME",
    "N_OUTS",
    "SCORE_BAND_EDGES",
    "SCORE_BAND_LABELS",
    "SCORE_CLAMP",
    "CellCoords",
    "cell_key",
    "count_bucket",
    "decode_cell",
    "score_band",
    "score_band_array",
]
