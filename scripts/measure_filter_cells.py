#!/usr/bin/env python
"""
scripts/measure_filter_cells.py
===============================
SIM-451 — measure how full the 2,880 hard-filter cells actually are.

WHAT THIS IS
------------
The new sampling architecture replaces the similarity kernel over the situation
with a HARD FILTER on the situation, then weights only the survivors. The filter
is a cross-tab of five dimensions:

    base occupancy (8) x outs (3) x count (12) x score band (5) x home-or-away (2)
    = 2,880 cells

Every downstream design choice depends on how many real plays land in each cell.
Nobody measured it. This script measures it, on the LIVE pools, and writes the
result as committed evidence.

THE HEADLINE NUMBER
-------------------
Counting thin cells is the WRONG metric. A cell that holds two rows does not
matter if no game ever reaches it. So this script reports, for each candidate
minimum cell size, the SHARE OF ALL DRAWS that land in an under-full cell: it
weights each cell by how often real play reaches it. That share is the number
SIM-475 needs to set ``MIN_CELL``.

    Assumption, stated plainly: one pool row IS one real occurrence of that
    situation, so pool frequency stands in for simulator reach frequency. That
    holds only while the simulator's base-out / count / score distribution
    matches the historical one. It is close, not identical, and the SIM-449 /
    SIM-450 work can move it. Do not read the share as exact.

THE WIDENING LADDER (the second half of the deliverable)
--------------------------------------------------------
Knowing the under-full draw share is only half of what SIM-475 needs. SIM-475
also widens a thin cell in a fixed order: it relaxes the score band first, then
home-or-away, then the count (decision record
``docs/audit/2026-08-10-sim-loop-remediation-decisions.md:422``).

So this script measures the ladder. For each relaxation step it reports the
under-full draw share that SURVIVES the step, and the share the step RECOVERS.
A widened group always contains its exact cell, so a row that is full enough at
one step stays full enough at every later step: the surviving share only falls.

It also measures each single dimension on its own and ranks them by recovery.
That ranking is what turns the decided order from an assertion into evidence.
:data:`WIDEN_LADDER` holds the decided order; the ranking says whether the data
agrees.

The ranking comes in two forms, and the difference matters. The ADMISSIBLE
ranking covers only the three dimensions the ladder may relax — the score band,
home-or-away and the count. The verdict on the decided order reads off that one,
because it is a choice SIM-475 can make. The five-dimension ranking is a
DIAGNOSTIC that says where the thin draw share sits; the base state and the
number of outs appear in it and must never be relaxed, because they are the
situation the hard filter exists to hold fixed.

The whole ladder is derived from the same length-2880 occupancy vector, because
every widening step merges whole cells. No extra query is needed.

THE HEALTH FLAG AND THE EXIT CODE
---------------------------------
``coverage_ok`` in the report is a real gate, not a label. It is false when any
of three things is true:

  * a season's half-inning join coverage falls below ``--min-coverage``;
  * a pool is missing a season the pitch pool holds;
  * a configuration measures fewer seasons than it asked for.

The third check exists because the second one cannot see a per-configuration
gap. A configuration that asks for 2024, 2025 and 2026 and finds rows in only
two of them is a TWO-season measurement wearing a three-season label. The report
now names the gap in ``configurations[key]["seasons_missing"]`` and the run exits
``2``. It still writes the JSON first, so the evidence lands either way.

WHAT IT MEASURES AND WHY
------------------------
Twelve configurations: {sim.pitch_pool, sim.outcome_pool} x {recent seasons, all
seasons} x {batter hand L, R, both}.

  * ``sim.pitch_pool`` drives the pitch draw. ``sim.outcome_pool`` is the in-play
    subset and drives the batted-ball draw.
  * recent vs all seasons is the evidence SIM-460 needs to decide the recency
    floor. ``RECENCY_FLOOR_SEASONS`` is 4 since SIM-516 (the owner's window
    ruling: the last three completed seasons plus the current one), so the
    artifact holds 4 of the 10 swept seasons.
  * hand L / R vs both is the evidence SIM-461 needs to decide whether to keep
    the batter-hand partition. The pools are partitioned by batter hand today
    (``pipeline/batch/engine_artifacts.py`` filters on ``stand``), so PER-HAND
    row counts are what the sampler sees, not totals.

It measures the POOL, not the on-disk engine artifact. The artifact is a cached
copy and can lag a pool rebuild. A forward-looking design decision must read the
source the next rebuild will use.

THE DEFINITIONS ARE COPIED, NOT INVENTED
----------------------------------------
A measurement that uses a different definition than the code measures nothing.
So:

  * :func:`count_bucket` copies ``simulation/full_pool_sampler.py:248``.
  * :func:`score_band` copies the clamp at ``simulation/sim_loop.py:1348``.
    The pool builder already clamps ``score_diff`` to -5..+5
    (``pipeline/batch/player_profile_computor.py:4683``), so the clamp is a
    verified no-op on pool data. The function applies it anyway so it is correct
    standalone.
  * The recent-season set comes from ``last_n_seasons()`` in
    ``pipeline/batch/engine_artifacts.py`` — the same call the artifact builder
    makes — never from a re-derivation.
  * The outcome-pool row filter copies the three NOT NULL predicates from
    ``build_battedball_pool_artifact``, so the measurement counts the rows the
    builder would keep.

  * THE SCORE BAND IS THE ONE EXCEPTION. No score-band definition exists in the
    code today; the design doc asserts "5 bands" without giving the edges.
    SIM-451 ORIGINATES the definition here as :data:`SCORE_BAND_EDGES`.
    SIM-475 must IMPORT that constant, never redefine it. Different edges make
    every occupancy number in this report wrong.

HOME-OR-AWAY NEEDS A JOIN
-------------------------
No pool table carries the half-inning. Two sources supply it, and they agree
where both exist:

  * ``postgres`` — ``pg.raw.pitches.inning_topbot`` through the DuckDB postgres
    extension. Complete, including the current season. The default.
  * ``duckdb``  — ``derived.at_bat_situations.top_or_bottom``, DuckDB-local and
    keyed on the pool's own join key. The fallback when Postgres is unreachable.
    It is a derived table and can lag a pool rebuild, so its coverage of the
    newest season can be partial.

Either way the measurement INNER JOINs to that map, so unmatched pool rows drop
out. The report prints per-season join coverage BEFORE the occupancy tables, and
warns loudly below ``--min-coverage``. A run whose coverage is thin is not a
valid measurement of the seasons it misses.

PURE vs. IMPACTFUL
------------------
The cell algebra — :func:`score_band`, :func:`count_bucket`, :func:`cell_key`,
:func:`decode_cell`, :func:`occupancy_stats`, :func:`widening_recovery`,
:func:`emptiest_reachable` — is PURE. It has no database, no I/O and no RNG. So
the unit tests import this module and exercise the algebra with no database at
all.

``duckdb`` really is absent from a bare import of this module, and the unit
suite proves it in a clean subprocess. That needs TWO lazy imports, not one:
:func:`open_connection` imports ``duckdb`` itself, and :func:`_engine_artifacts`
imports ``pipeline.batch.engine_artifacts``, which imports ``duckdb`` at module
scope (``engine_artifacts.py:38``). A direct module-scope import of
``engine_artifacts`` pulls ``duckdb`` in transitively and makes the claim false
while every ``hasattr(module, "duckdb")`` check still passes.

The DuckDB connection is opened READ ONLY. This script cannot damage the pools.

USAGE
-----
    # In the app container (DuckDB at /data/..., Postgres at db:5432):
    python scripts/measure_filter_cells.py
    python scripts/measure_filter_cells.py --output docs/audit/sim451.json
    python scripts/measure_filter_cells.py --half-inning-source duckdb
    python scripts/measure_filter_cells.py --recent-seasons 5 --min-cells 25 100

    # From the host (scripts/ is NOT bind-mounted, docs/ must be mounted to keep
    # the JSON; do NOT `docker compose build`):
    MSYS_NO_PATHCONV=1 docker compose run --rm \
      -v "$PWD/scripts:/app/scripts" -v "$PWD/docs:/app/docs" \
      app python scripts/measure_filter_cells.py

EXIT CODES
----------
    0   the measurement is sound.
    2   the JSON is written and ``coverage_ok`` is FALSE. A season is missing, a
        configuration measured fewer seasons than it asked for, or a join
        coverage fell below the threshold. Read ``health.problems``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import numpy as np

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

log = logging.getLogger("sim451")

SCRIPT_VERSION = "SIM-451/2"

#: The process exit code when the report is written but the measurement is not
#: sound. A caller must be able to tell a complete run from an incomplete one
#: without parsing the JSON.
EXIT_UNHEALTHY = 2


def _engine_artifacts() -> Any:
    """Import ``pipeline.batch.engine_artifacts`` lazily and return the module.

    The import stays inside this function on purpose. ``engine_artifacts``
    imports ``duckdb`` at module scope, so importing it at the top of this file
    would pull ``duckdb`` into every bare import of this script and break the
    no-database promise the pure layer makes.
    """
    from pipeline.batch import engine_artifacts

    return engine_artifacts


def recency_floor_seasons() -> int:
    """Return ``RECENCY_FLOOR_SEASONS`` from the artifact builder.

    The script never hardcodes the floor. It reads the same constant the
    artifact builder reads, so a change there reaches this measurement.
    """
    return int(_engine_artifacts().RECENCY_FLOOR_SEASONS)


def last_n_seasons(con: Any, n: int) -> list[int]:
    """Return the newest ``n`` seasons through the artifact builder's own call.

    This is a thin lazy wrapper around
    ``pipeline.batch.engine_artifacts.last_n_seasons``. The measurement must use
    the builder's definition of "recent", never a re-derivation.
    """
    return list(_engine_artifacts().last_n_seasons(con, int(n)))


# ---------------------------------------------------------------------------
# The cell definition (2,880 cells)
# ---------------------------------------------------------------------------

N_BASE = 8  # runners_state bitmask: bit0=1B, bit1=2B, bit2=3B
N_OUTS = 3
N_COUNT = 12  # 4 ball states x 3 strike states
N_BAND = 5
N_HOME = 2
N_CELLS = N_BASE * N_OUTS * N_COUNT * N_BAND * N_HOME  # 2880

#: SIM-451 ORIGINATES the score-band split. No definition exists in the code
#: today. These are the symmetric 5-band split of the +/-5 clamp the sim already
#: applies. SIM-475 must import this constant instead of redefining the edges.
#: A cell id computed under different edges is not comparable with this report.
#: Read it as: band = the number of edges the clamped score difference exceeds.
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

SCORE_CLAMP = 5  # simulation/sim_loop.py:1348

#: The candidate minimum cell sizes SIM-475 chooses between.
DEFAULT_MIN_CELLS: tuple[int, ...] = (20, 50, 100, 200)

#: Reported occupancy percentiles. 0 is the minimum, 100 the maximum.
PERCENTILES: tuple[int, ...] = (0, 1, 5, 10, 25, 50, 75, 90, 99, 100)

#: The human-readable encode formula, carried into the JSON so a later reader can
#: reproduce a cell id without this file.
CELL_KEY_FORMULA = (
    "((((runners_state * 3 + outs) * 12 + count_bucket) * 5 + score_band) * 2 + bat_is_home)"
)

_BASE_LABELS = ("---", "1--", "-2-", "12-", "--3", "1-3", "-23", "123")


def score_band(score_diff: int) -> int:
    """Return the 0..4 score band of a batting-team run difference.

    The clamp copies ``simulation/sim_loop.py:1348``: the sim compresses every
    difference beyond five runs onto the +/-5 boundary, so a 9-run deficit and a
    5-run deficit are the same situation to the sampler. The pool builder already
    stores the clamped value, so the clamp is a no-op on pool data and a
    correctness guard on any other caller.
    """
    sd = max(-SCORE_CLAMP, min(SCORE_CLAMP, int(score_diff)))
    return sum(1 for edge in SCORE_BAND_EDGES if sd > edge)


def count_bucket(balls: int, strikes: int) -> int:
    """Return the 0..11 count bucket. Copies ``full_pool_sampler.py:248``.

    The saturating min/max is part of the copied definition. It maps an
    out-of-range count onto the nearest legal one instead of raising.
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


# ---------------------------------------------------------------------------
# SQL forms of the SAME definitions
# ---------------------------------------------------------------------------
# These build the SQL from the Python constants above, so the two forms cannot
# drift apart. The aggregation runs in DuckDB because the pools hold millions of
# rows; pulling them into Python would be slow and pointless.


def _score_band_sql(expr: str) -> str:
    """Return a SQL expression that computes :func:`score_band` of ``expr``.

    The SQL is a term-by-term transcription of the Python: it ADDS one indicator
    per edge, exactly as ``sum(1 for edge in SCORE_BAND_EDGES if sd > edge)``
    does. The earlier form was a first-match ``CASE`` ladder. A ladder reads the
    edges in tuple order while the Python does not, so the two forms could
    disagree the moment somebody reordered the tuple. A sum of indicators cannot
    drift from a sum of indicators.
    """
    clamped = f"GREATEST(-{SCORE_CLAMP}, LEAST({SCORE_CLAMP}, CAST({expr} AS INTEGER)))"
    terms = " + ".join(
        f"(CASE WHEN {clamped} > {edge} THEN 1 ELSE 0 END)" for edge in SCORE_BAND_EDGES
    )
    return f"({terms})"


def _count_bucket_sql(balls_expr: str, strikes_expr: str) -> str:
    """Return a SQL expression that computes :func:`count_bucket`."""
    b = f"LEAST(GREATEST(CAST({balls_expr} AS INTEGER), 0), 3)"
    s = f"LEAST(GREATEST(CAST({strikes_expr} AS INTEGER), 0), 2)"
    return f"({b} * 3 + {s})"


def _cell_key_sql(alias: str, home_alias: str) -> str:
    """Return a SQL expression that computes :func:`cell_key` for a pool row."""
    rs = f"(CAST({alias}.runners_state AS INTEGER) % {N_BASE})"
    outs = f"LEAST(GREATEST(CAST({alias}.outs AS INTEGER), 0), {N_OUTS - 1})"
    cb = _count_bucket_sql(f"{alias}.count_balls", f"{alias}.count_strikes")
    band = _score_band_sql(f"{alias}.score_diff")
    return (
        f"((((({rs}) * {N_OUTS} + {outs}) * {N_COUNT} + {cb}) * {N_BAND} + {band})"
        f" * {N_HOME} + CAST({home_alias}.bat_is_home AS INTEGER))"
    )


# ---------------------------------------------------------------------------
# Pure statistics
# ---------------------------------------------------------------------------


def occupancy_stats(
    occ: np.ndarray, min_cells: tuple[int, ...] = DEFAULT_MIN_CELLS
) -> dict[str, Any]:
    """Summarise one length-2880 occupancy vector.

    ``under[m]["draw_share_pct"]`` is the headline: the percentage of ALL rows
    that sit in a cell holding fewer than ``m`` rows. It weights each cell by how
    often play reaches it, so a rare empty cell costs almost nothing while a
    common thin cell costs a lot. ``under[m]["cells"]`` counts the cells instead,
    which is the misleading view — both are reported so the difference is visible.
    """
    arr = np.asarray(occ, dtype=np.int64)
    total = int(arr.sum())
    n_cells = int(arr.size)
    stats: dict[str, Any] = {
        "total_rows": total,
        "n_cells": n_cells,
        "cells_nonempty": int((arr > 0).sum()),
        "mean_per_cell": (total / n_cells) if n_cells else 0.0,
        # Linear-interpolated percentile, truncated to a whole row count.
        "percentiles": {str(p): int(np.percentile(arr, p)) for p in PERCENTILES},
        "under": {},
    }
    for m in min_cells:
        mask = arr < int(m)
        rows_under = int(arr[mask].sum())
        stats["under"][str(int(m))] = {
            "cells": int(mask.sum()),
            "rows": rows_under,
            "draw_share_pct": (100.0 * rows_under / total) if total else 0.0,
        }
    return stats


def emptiest_reachable(
    occ_target: np.ndarray, occ_reference: np.ndarray, k: int
) -> list[dict[str, Any]]:
    """Return the ``k`` thinnest cells of ``occ_target`` that real play REACHES.

    A cell counts as reachable when ``occ_reference`` holds at least one row.
    The reference is the widest configuration measured — every season, both
    hands, the pitch pool — so a cell empty there is a cell the game never
    produces, not a thin cell. Sorting ascending by target occupancy puts the
    cells a human should sanity-check first at the top.
    """
    target = np.asarray(occ_target, dtype=np.int64)
    ref = np.asarray(occ_reference, dtype=np.int64)
    ids = np.flatnonzero(ref > 0)
    if ids.size == 0:
        return []
    # Sort ascending by occupancy, then by cell id, so the output is deterministic.
    order = np.lexsort((ids, target[ids]))
    picked = ids[order][: max(0, int(k))]
    return [
        {
            "cell_id": int(cid),
            "count": int(target[cid]),
            "reference_count": int(ref[cid]),
            "label": decode_cell(int(cid)).label,
        }
        for cid in picked
    ]


def unreachable_cells(occ_reference: np.ndarray) -> list[dict[str, Any]]:
    """Return the cells the reference configuration never reaches.

    A non-empty result is the cell-definition bug detector: it means the encode
    produces a coordinate real baseball never visits, or it means a genuinely
    impossible situation. A human must read the labels and decide which.
    """
    ref = np.asarray(occ_reference, dtype=np.int64)
    return [
        {"cell_id": int(cid), "label": decode_cell(int(cid)).label}
        for cid in np.flatnonzero(ref == 0)
    ]


# ---------------------------------------------------------------------------
# The widening ladder (the evidence SIM-475 needs)
# ---------------------------------------------------------------------------

#: The five filter dimensions, most-significant first. The order matches
#: :data:`CELL_KEY_FORMULA`, so a reader can line the two up.
CELL_DIMENSIONS: tuple[str, ...] = ("base", "outs", "count", "score_band", "home_or_away")

#: How many values each dimension takes.
DIMENSION_SIZES: dict[str, int] = {
    "base": N_BASE,
    "outs": N_OUTS,
    "count": N_COUNT,
    "score_band": N_BAND,
    "home_or_away": N_HOME,
}

#: The widening order the remediation decision record sets at line 422: relax
#: the score band first, then home-or-away, then the count. Each entry lists the
#: dimensions the step has dropped, CUMULATIVELY. Step 0 is the exact cell.
#: SIM-475 must import this tuple instead of restating the order in prose.
WIDEN_LADDER: tuple[tuple[str, ...], ...] = (
    (),
    ("score_band",),
    ("score_band", "home_or_away"),
    ("score_band", "home_or_away", "count"),
)

#: The decided drop order, flattened. The report compares it against the
#: measured single-drop ranking.
WIDEN_ORDER: tuple[str, ...] = ("score_band", "home_or_away", "count")

#: The dimensions the ladder is ALLOWED to relax. The base state and the number
#: of outs define the situation the hard filter exists to hold fixed, so the
#: ladder never drops them. The report measures them anyway, as a diagnostic
#: that says where the thin draw share actually sits, and it keeps the two
#: rankings separate so a reader cannot act on the diagnostic by mistake.
WIDEN_ADMISSIBLE: tuple[str, ...] = ("score_band", "home_or_away", "count")

WIDEN_INADMISSIBLE: tuple[str, ...] = ("base", "outs")

WIDEN_ORDER_SOURCE = "docs/audit/2026-08-10-sim-loop-remediation-decisions.md:422"


def _cell_coordinate_vectors() -> dict[str, np.ndarray]:
    """Return the coordinate of every cell id, one length-2880 vector per dimension.

    This is :func:`decode_cell` vectorised over all 2,880 ids at once. The
    unit suite checks the two against each other, so the fast form cannot drift
    from the readable one.
    """
    cid = np.arange(N_CELLS, dtype=np.int64)
    home = cid % N_HOME
    rest = cid // N_HOME
    band = rest % N_BAND
    rest = rest // N_BAND
    count = rest % N_COUNT
    rest = rest // N_COUNT
    outs = rest % N_OUTS
    base = rest // N_OUTS
    return {
        "base": base,
        "outs": outs,
        "count": count,
        "score_band": band,
        "home_or_away": home,
    }


_CELL_COORDS = _cell_coordinate_vectors()


def widen_group_ids(dropped: tuple[str, ...]) -> tuple[np.ndarray, int]:
    """Return ``(group_id_of_each_cell, n_groups)`` after dropping ``dropped``.

    Widening merges whole cells: two cells that agree on every KEPT dimension
    become one group. So the group id is the same positional encode as
    :func:`cell_key`, run over the kept dimensions only. Dropping everything
    leaves one group, which is the whole pool.
    """
    unknown = [d for d in dropped if d not in DIMENSION_SIZES]
    if unknown:
        raise ValueError(f"unknown widening dimension(s) {unknown}; expected {CELL_DIMENSIONS}")
    kept = [d for d in CELL_DIMENSIONS if d not in set(dropped)]
    gid = np.zeros(N_CELLS, dtype=np.int64)
    n_groups = 1
    for dim in kept:
        gid = gid * DIMENSION_SIZES[dim] + _CELL_COORDS[dim]
        n_groups *= DIMENSION_SIZES[dim]
    return gid, n_groups


def widened_group_size_per_cell(
    occ: np.ndarray, dropped: tuple[str, ...]
) -> tuple[np.ndarray, int]:
    """Return, for every cell, the row count of the widened group it falls into.

    A row in cell ``c`` draws from ``group_size[c]`` rows after the relaxation,
    so ``group_size[c] < MIN_CELL`` is exactly "this row is still under-full".
    """
    arr = np.asarray(occ, dtype=np.int64)
    gid, n_groups = widen_group_ids(dropped)
    totals = np.zeros(n_groups, dtype=np.int64)
    np.add.at(totals, gid, arr)
    return totals[gid], n_groups


def _widen_step_stats(
    occ: np.ndarray,
    dropped: tuple[str, ...],
    min_cells: tuple[int, ...],
    baseline: dict[str, float] | None,
) -> dict[str, Any]:
    """Measure one relaxation step against the exact-cell baseline."""
    arr = np.asarray(occ, dtype=np.int64)
    total = int(arr.sum())
    size_per_cell, n_groups = widened_group_size_per_cell(arr, dropped)
    gid, _ = widen_group_ids(dropped)
    group_totals = np.zeros(n_groups, dtype=np.int64)
    np.add.at(group_totals, gid, arr)

    label = "exact cell" if not dropped else "relax " + " + ".join(dropped)
    step: dict[str, Any] = {
        "drops": list(dropped),
        "label": label,
        "n_groups": int(n_groups),
        "under": {},
    }
    for m in min_cells:
        key = str(int(m))
        thin = size_per_cell < int(m)
        rows_under = int(arr[thin].sum())
        share = (100.0 * rows_under / total) if total else 0.0
        entry: dict[str, Any] = {
            "rows": rows_under,
            "draw_share_pct": share,
            "groups_under": int((group_totals < int(m)).sum()),
        }
        if baseline is not None:
            base_share = baseline[key]
            entry["recovered_pct_points"] = base_share - share
            entry["recovered_share_of_baseline_pct"] = (
                (100.0 * (base_share - share) / base_share) if base_share > 0 else 0.0
            )
        step["under"][key] = entry
    return step


def widening_recovery(
    occ: np.ndarray,
    min_cells: tuple[int, ...] = DEFAULT_MIN_CELLS,
    ladder: tuple[tuple[str, ...], ...] = WIDEN_LADDER,
) -> dict[str, Any]:
    """Measure how much under-full draw share each relaxation step recovers.

    Two views come back.

    ``ladder`` walks the DECIDED order cumulatively. Step 0 is the exact cell
    and gives the baseline share. Every later step reports the share that
    SURVIVES it and the share it RECOVERS, both in percentage points and as a
    fraction of the baseline. A widened group contains its exact cell, so the
    surviving share can only fall.

    ``single_drop`` drops one dimension at a time and ranks the five by
    recovery. Two rankings come back, and the difference matters.

      * ``admissible_first_drop_ranking`` covers only the three dimensions the
        ladder may relax (:data:`WIDEN_ADMISSIBLE`).
        ``score_band_is_the_best_first_drop`` reads off THIS ranking, so it is a
        verdict on a choice SIM-475 can actually make.
      * ``first_drop_ranking`` covers all five, including the base state and the
        outs. That is a DIAGNOSTIC. It says where the thin draw share sits. It
        is not a recommendation: relaxing the base state throws away the
        situation the hard filter exists to hold fixed.

    That split is what turns the decided order from an assertion into evidence
    without inviting a reader to relax a dimension the design forbids.
    """
    arr = np.asarray(occ, dtype=np.int64)
    exact = _widen_step_stats(arr, (), min_cells, None)
    baseline = {k: float(v["draw_share_pct"]) for k, v in exact["under"].items()}

    ladder_out: list[dict[str, Any]] = []
    for i, dropped in enumerate(ladder):
        step = _widen_step_stats(arr, tuple(dropped), min_cells, baseline)
        step["step"] = i
        ladder_out.append(step)

    single_out: list[dict[str, Any]] = []
    for dim in CELL_DIMENSIONS:
        step = _widen_step_stats(arr, (dim,), min_cells, baseline)
        step["admissible"] = dim in WIDEN_ADMISSIBLE
        single_out.append(step)

    def _rank(pool: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        return sorted(
            pool,
            key=lambda s: (-float(s["under"][key]["recovered_pct_points"]), str(s["drops"][0])),
        )

    ranking: dict[str, list[str]] = {}
    admissible_ranking: dict[str, list[str]] = {}
    confirmed: dict[str, bool] = {}
    admissible = [s for s in single_out if s["admissible"]]
    for m in min_cells:
        key = str(int(m))
        ranking[key] = [str(s["drops"][0]) for s in _rank(single_out, key)]
        ranked_admissible = _rank(admissible, key)
        admissible_ranking[key] = [str(s["drops"][0]) for s in ranked_admissible]
        best = float(ranked_admissible[0]["under"][key]["recovered_pct_points"])
        band = next(s for s in single_out if s["drops"] == ["score_band"])
        confirmed[key] = float(band["under"][key]["recovered_pct_points"]) >= best

    return {
        "decided_order": list(WIDEN_ORDER),
        "decided_order_source": WIDEN_ORDER_SOURCE,
        "admissible_dimensions": list(WIDEN_ADMISSIBLE),
        "inadmissible_dimensions": list(WIDEN_INADMISSIBLE),
        "ladder": ladder_out,
        "single_drop": single_out,
        "admissible_first_drop_ranking": admissible_ranking,
        "first_drop_ranking": ranking,
        "first_drop_ranking_is_diagnostic_only": (
            "covers base and outs, which the ladder must never relax"
        ),
        "score_band_is_the_best_first_drop": confirmed,
    }


# ---------------------------------------------------------------------------
# Database layer (duckdb imported lazily — the unit tests need no database)
# ---------------------------------------------------------------------------

DEFAULT_DUCKDB_PATH = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
DEFAULT_DSN = os.environ.get("BASEBALL_DB_DSN", "")
DEFAULT_OUTPUT = "docs/audit/2026-08-10-sim451-filter-cell-occupancy.json"

#: The three NOT NULL predicates copied from
#: ``pipeline/batch/engine_artifacts.build_battedball_pool_artifact``. The
#: batted-ball artifact keeps only rows that carry all three geometry columns, so
#: the measurement must apply the same filter or it counts rows the sampler never
#: sees.
_OUTCOME_GEOM_FILTER = (
    "p.exit_velo IS NOT NULL AND p.launch_angle IS NOT NULL "
    "AND p.pull_relative_spray_angle IS NOT NULL"
)


#: The placeholder the redactor emits when it cannot prove a DSN is safe to
#: print. Deny by default: an unparsed DSN never reaches the report verbatim.
REDACTED = "<redacted>"

#: The ONLY libpq keywords that reach the report. This is an allowlist, not a
#: block list. A block list has to name every secret-bearing keyword —
#: ``password``, ``passfile``, ``sslpassword``, ``sslkey``, ``options`` — and a
#: new one leaks the day libpq adds it. An allowlist cannot.
_DSN_SAFE_KEYWORDS: tuple[str, ...] = ("host", "hostaddr", "port", "dbname")

#: One ``key=value`` pair of a libpq keyword DSN. libpq allows spaces around the
#: ``=``, and allows a single-quoted value with backslash escapes.
_DSN_KEYWORD_PAIR = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>'(?:[^'\\]|\\.)*'|[^\s]*)"
)

#: What a redacted DSN is allowed to look like: ``host``, ``host:port``,
#: ``host/dbname`` or ``host:port/dbname``. The final gate. Anything that does
#: not match becomes :data:`REDACTED`, so a form nobody anticipated cannot leak
#: through the parser.
_SAFE_DSN_SHAPE = re.compile(r"^[A-Za-z0-9._\-\[\]]+(?::[0-9]+)?(?:/[A-Za-z0-9._\-]*)?$")


def _assemble_dsn_label(host: str, port: str, dbname: str) -> str:
    """Join the three safe parts, then refuse anything that fails the shape gate."""
    label = host
    if port:
        label = f"{label}:{port}"
    if dbname:
        label = f"{label}/{dbname}"
    if not label or not _SAFE_DSN_SHAPE.match(label):
        return REDACTED
    return label


def _redact_url_dsn(dsn: str) -> str:
    """Redact a URL-form DSN, e.g. ``postgresql://user:pw@host:5432/db?sslmode=require``."""
    try:
        parts = urlsplit(dsn)
        host = parts.hostname or ""
        port = str(parts.port) if parts.port else ""
    except ValueError:
        # urlsplit raises when the netloc holds a non-numeric port. That happens
        # when the password itself contains a "://", which shifts the parse.
        # Refuse rather than guess.
        return REDACTED
    # Take the LAST path segment only. The userinfo never appears there in a
    # well-formed URL, and taking the last segment keeps it out of a malformed
    # one too.
    dbname = parts.path.rsplit("/", 1)[-1]
    return _assemble_dsn_label(host, port, dbname)


def _redact_keyword_dsn(dsn: str) -> str:
    """Redact a libpq keyword DSN, e.g. ``host=db port=5432 user=x password=pw dbname=y``.

    The old redactor split on ``@``. A keyword DSN holds no ``@``, so the split
    returned the WHOLE string and wrote the password into the committed report.
    This form reads the keywords instead and emits only the allowlisted ones.
    """
    found: dict[str, str] = {}
    for match in _DSN_KEYWORD_PAIR.finditer(dsn):
        key = match.group("key").lower()
        if key not in _DSN_SAFE_KEYWORDS:
            continue
        value = match.group("value")
        if value.startswith("'") and value.endswith("'") and len(value) >= 2:
            value = value[1:-1].replace("\\'", "'").replace("\\\\", "\\")
        found[key] = value
    host = found.get("host") or found.get("hostaddr") or ""
    return _assemble_dsn_label(host, found.get("port", ""), found.get("dbname", ""))


def _redact_dsn(dsn: str) -> str:
    """Return the host part of a DSN. The password never reaches the report.

    The report is committed evidence and the repository runs a CI secrets-check
    job, so this function is a security control, not cosmetics. It handles both
    DSN forms ``BASEBALL_DB_DSN`` takes:

      * the URL form ``postgresql://user:password@host:port/dbname?options``;
      * the libpq keyword form ``host=db port=5432 user=x password=pw dbname=y``.

    Anything else returns :data:`REDACTED`. The function emits an allowlist of
    three fields — host, port and database name — and a final shape gate rejects
    an assembled label that does not look like ``host[:port][/dbname]``. So an
    unanticipated DSN form is dropped, never echoed.
    """
    text = (dsn or "").strip()
    if not text:
        return ""
    if "://" in text:
        return _redact_url_dsn(text)
    if "=" in text:
        return _redact_keyword_dsn(text)
    # A bare value with no structure. libpq accepts no such form, so the string
    # is something else — and "something else" includes a password somebody put
    # in the wrong variable. Refuse rather than echo it.
    return REDACTED


def _redact_text(text: str, dsn: str) -> str:
    """Replace any verbatim copy of ``dsn`` inside ``text`` with its redaction.

    A DuckDB ATTACH failure quotes the connection string back in the exception
    message. Logging that message unredacted writes the password to stdout and
    into any captured CI log.
    """
    body = str(text)
    secret = (dsn or "").strip()
    if secret:
        body = body.replace(secret, _redact_dsn(secret))
    return body


def open_connection(duckdb_path: str, dsn: str, source: str) -> tuple[Any, str]:
    """Open the pools READ ONLY and return ``(connection, half_inning_source)``.

    ``source`` is ``postgres``, ``duckdb`` or ``auto``. Under ``auto`` the
    Postgres attach is tried first and the DuckDB table is the fallback. The
    returned source names what the caller actually got, which may differ from
    what it asked for.
    """
    import duckdb  # lazy: the unit tests import this module with no database

    con = duckdb.connect(duckdb_path, read_only=True)
    if source == "duckdb":
        return con, "duckdb"
    if not dsn:
        if source == "postgres":
            raise SystemExit("--half-inning-source postgres needs --dsn or BASEBALL_DB_DSN")
        log.warning("no DSN set; falling back to the DuckDB half-inning source")
        return con, "duckdb"
    try:
        con.execute("INSTALL postgres; LOAD postgres;")
        con.execute(f"ATTACH '{dsn}' AS pg (TYPE postgres, READ_ONLY);")
    except Exception as exc:  # noqa: BLE001 - any attach failure means fall back
        # DuckDB quotes the connection string back inside the ATTACH error, so
        # the message can carry the password. Redact before it reaches a log or
        # a traceback, and drop the original exception with `from None` so the
        # chained message cannot carry it either.
        detail = _redact_text(exc, dsn)
        if source == "postgres":
            raise RuntimeError(f"Postgres attach failed: {detail}") from None
        log.warning("Postgres attach failed (%s); falling back to the DuckDB source", detail)
        return con, "duckdb"
    return con, "postgres"


def build_half_inning_map(con: Any, source: str) -> int:
    """Build TEMP TABLE ``ha(game_pk, at_bat_number, bat_is_home)`` and return its row count.

    ``bat_is_home`` is 1 when the home team bats. Postgres marks that with
    ``inning_topbot = 'Bot'``; the DuckDB table stores the same convention in
    ``top_or_bottom`` (``player_profile_computor.py:5037`` builds it from the
    same expression), so the two sources are definitionally identical and only
    their coverage differs.

    A TEMP TABLE works on a read-only connection: DuckDB keeps the temporary
    catalog separate from the database file. The caller builds it once per run,
    so there is nothing to drop first.
    """
    if source == "postgres":
        con.execute(
            "CREATE TEMP TABLE ha AS "
            "SELECT game_pk, at_bat_number, "
            "       CAST(MAX(CASE WHEN inning_topbot = 'Bot' THEN 1 ELSE 0 END) AS TINYINT) "
            "         AS bat_is_home "
            "FROM pg.raw.pitches WHERE data_quality_flag = FALSE GROUP BY 1, 2"
        )
    else:
        con.execute(
            "CREATE TEMP TABLE ha AS "
            "SELECT game_pk, at_bat_number, CAST(top_or_bottom AS TINYINT) AS bat_is_home "
            "FROM derived.at_bat_situations WHERE top_or_bottom IS NOT NULL"
        )
    return int(con.execute("SELECT COUNT(*) FROM ha").fetchone()[0])


def season_row_counts(con: Any, table: str) -> dict[str, int]:
    """Return the per-season row count of one pool. Prints the gaps into the report."""
    rows = con.execute(f"SELECT season, COUNT(*) FROM sim.{table} GROUP BY 1 ORDER BY 1").fetchall()
    return {str(int(r[0])): int(r[1]) for r in rows}


def join_coverage(con: Any, table: str) -> list[dict[str, Any]]:
    """Return the per-season share of pool rows the half-inning map matches.

    The occupancy measurement INNER JOINs, so an unmatched row drops out
    silently. This is what makes the drop visible.
    """
    rows = con.execute(
        f"SELECT p.season, COUNT(*) AS pool_rows, COUNT(h.game_pk) AS matched "
        f"FROM sim.{table} p LEFT JOIN ha h USING (game_pk, at_bat_number) "
        f"GROUP BY 1 ORDER BY 1"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for season, pool_rows, matched in rows:
        n = int(pool_rows)
        m = int(matched)
        out.append(
            {
                "season": int(season),
                "pool_rows": n,
                "matched": m,
                "dropped": n - m,
                "coverage_pct": (100.0 * m / n) if n else 0.0,
            }
        )
    return out


def cell_occupancy(
    con: Any, table: str, seasons: list[int], hand: str
) -> tuple[np.ndarray, dict[str, int]]:
    """Cross-tab one pool configuration into a length-2880 occupancy vector.

    ``hand`` is ``L``, ``R`` or ``ALL``. ``ALL`` removes the batter-hand
    partition, which is the SIM-461 evidence. Cells with no rows read 0 rather
    than dropping out, because a zero cell is the interesting case.

    The query also groups by season, so the caller learns which of the REQUESTED
    seasons actually delivered rows. Without that split a configuration asking
    for three seasons and finding two reports a three-season label over a
    two-season measurement, and nothing in the report contradicts it.
    """
    season_list = ", ".join(str(int(s)) for s in seasons)
    where = [f"p.season IN ({season_list})"]
    if hand in ("L", "R"):
        where.append(f"p.stand = '{hand}'")
    if table == "outcome_pool":
        where.append(_OUTCOME_GEOM_FILTER)
    sql = (
        f"SELECT {_cell_key_sql('p', 'h')} AS cell_id, CAST(p.season AS INTEGER) AS season, "
        f"COUNT(*) AS n "
        f"FROM sim.{table} p JOIN ha h USING (game_pk, at_bat_number) "
        f"WHERE {' AND '.join(where)} GROUP BY 1, 2"
    )
    occ = np.zeros(N_CELLS, dtype=np.int64)
    per_season: dict[str, int] = {}
    for cid, season, n in con.execute(sql).fetchall():
        c = int(cid)
        # A cell id outside the range means a pool row carries a situation value
        # the cell definition does not cover. Raise instead of corrupting the
        # measurement silently.
        if not 0 <= c < N_CELLS:
            raise ValueError(f"{table}/{hand}: cell id {c} outside 0..{N_CELLS - 1}")
        occ[c] += int(n)
        skey = str(int(season))
        per_season[skey] = per_season.get(skey, 0) + int(n)
    return occ, per_season


def health_report(
    *,
    season_counts: dict[str, dict[str, int]],
    coverage: dict[str, list[dict[str, Any]]],
    configurations: dict[str, Any],
    min_coverage: float,
) -> dict[str, Any]:
    """Decide whether the measurement is sound, and say why when it is not.

    The old ``coverage_ok`` flag answered ONE question: does every season the
    pool holds join to a half-inning at the required rate? That question cannot
    see a season the pool does not hold at all. The committed 2026-08-10 report
    is the proof: ``sim.outcome_pool`` held no 2026 rows, the join-coverage loop
    never visited 2026 because it iterates the seasons present, and three
    configurations labelled ``[2024, 2025, 2026]`` measured two seasons under a
    green flag.

    So this function asks three questions.

    1. ``join_coverage_ok`` — the original one.
    2. ``pools_hold_every_season`` — does every pool hold every season the pitch
       pool holds? The pitch pool is the reference because it is the widest.
    3. ``configurations_complete`` — did every configuration find rows in every
       season it asked for? This is the sharp one. It compares the REQUESTED
       season list against the seasons that actually delivered rows.

    ``coverage_ok`` is the AND of the three.
    """
    problems: list[str] = []

    join_ok = True
    for table, rows in coverage.items():
        for row in rows:
            if float(row["coverage_pct"]) < float(min_coverage):
                join_ok = False
                problems.append(
                    f"{table} season {row['season']}: half-inning join coverage "
                    f"{row['coverage_pct']:.3f}% is below {min_coverage:.1f}%"
                )

    reference_seasons = sorted(int(s) for s in season_counts.get("pitch_pool", {}))
    missing_seasons: dict[str, list[int]] = {}
    for table, counts in season_counts.items():
        present = {int(s) for s, n in counts.items() if int(n) > 0}
        gaps = [s for s in reference_seasons if s not in present]
        if gaps:
            missing_seasons[table] = gaps
            problems.append(
                f"sim.{table} holds ZERO rows for season(s) {gaps}, which sim.pitch_pool holds"
            )

    config_gaps: dict[str, dict[str, Any]] = {}
    for key, cfg in configurations.items():
        missing = list(cfg.get("seasons_missing") or [])
        if missing:
            config_gaps[key] = {
                "seasons_requested": list(cfg.get("seasons") or []),
                "seasons_measured": list(cfg.get("seasons_measured") or []),
                "seasons_missing": missing,
            }
            problems.append(
                f"configuration {key} asked for {cfg.get('seasons')} but measured "
                f"{cfg.get('seasons_measured')}; season(s) {missing} contributed no rows"
            )

    pools_ok = not missing_seasons
    configs_ok = not config_gaps
    return {
        "coverage_ok": bool(join_ok and pools_ok and configs_ok),
        "join_coverage_ok": bool(join_ok),
        "pools_hold_every_season": bool(pools_ok),
        "configurations_complete": bool(configs_ok),
        "reference_seasons": reference_seasons,
        "missing_seasons": missing_seasons,
        "configuration_season_gaps": config_gaps,
        "problems": problems,
    }


def all_seasons(con: Any) -> list[int]:
    """Return every season the pitch pool holds. Never hardcoded."""
    return [
        int(r[0])
        for r in con.execute(
            "SELECT DISTINCT season FROM sim.pitch_pool ORDER BY season"
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_TABLES = ("pitch_pool", "outcome_pool")
_HANDS = ("L", "R", "ALL")

#: Configurations whose emptiest-cell listing goes to stdout. The rest are in the
#: JSON. These three are the ones a human needs to eyeball.
_PRINT_EMPTIEST = (
    ("pitch_pool", "recent", "ALL"),
    ("outcome_pool", "recent", "ALL"),
    ("outcome_pool", "all", "ALL"),
)

#: The figure the remediation decision record states for the per-hand batted-ball
#: pool. The report compares it against the measurement so a reader cannot trust
#: the doc over the data.
_DOC_MEAN_PER_CELL = 240.0


def _print_header(
    *,
    duckdb_path: str,
    half_inning_source: str,
    ha_rows: int,
    recent: list[int],
    every: list[int],
) -> None:
    print("\n" + "=" * 78)
    print(" SIM-451 — FILTER CELL OCCUPANCY")
    print("=" * 78)
    print(f"  DuckDB              : {duckdb_path} (read only)")
    print(f"  Half-inning source  : {half_inning_source}  ({ha_rows:,} plate appearances)")
    print(f"  Recent seasons      : {recent}   (RECENCY_FLOOR_SEASONS={recency_floor_seasons()})")
    print(f"  All seasons         : {every}")
    print(
        f"  Cells               : {N_BASE} base x {N_OUTS} outs x {N_COUNT} count x "
        f"{N_BAND} band x {N_HOME} home = {N_CELLS}"
    )
    print(f"  Score-band edges    : {SCORE_BAND_EDGES}  labels {SCORE_BAND_LABELS}")
    print("                        SIM-451 originates this split. SIM-475 must import it.")
    print(f"  Cell id             : {CELL_KEY_FORMULA}")
    print("\n  The headline column is draw-share, not cell count. A thin cell only")
    print("  matters in proportion to how often real play reaches it.")


def _print_season_counts(counts: dict[str, dict[str, int]]) -> None:
    print("\n--- Per-season pool rows ---")
    seasons = sorted({s for c in counts.values() for s in c})
    print(f"  {'season':<8}" + "".join(f"{t:>18}" for t in _TABLES))
    for s in seasons:
        row = f"  {s:<8}"
        for t in _TABLES:
            row += f"{counts[t].get(s, 0):>18,}"
        print(row)
    # Name any season the pitch pool holds and the outcome pool does not. That
    # gap makes every recent-season batted-ball figure a pessimistic floor.
    missing = [s for s in counts["pitch_pool"] if counts["outcome_pool"].get(s, 0) == 0]
    if missing:
        print(
            f"\n  !! sim.outcome_pool has ZERO rows for season(s) {missing}, which "
            f"sim.pitch_pool does hold."
        )
        print("     Every batted-ball figure covering those seasons is a PESSIMISTIC FLOOR.")
        print("     Rebuild the outcome pool before SIM-475 reads a MIN_CELL off it.")


def _print_coverage(coverage: dict[str, list[dict[str, Any]]], min_coverage: float) -> bool:
    print("\n--- Half-inning join coverage (unmatched pool rows are DROPPED) ---")
    ok = True
    for table, rows in coverage.items():
        print(f"  {table}")
        for r in rows:
            flag = "" if r["coverage_pct"] >= min_coverage else "   <-- THIN"
            if flag:
                ok = False
            print(
                f"    {r['season']}  pool={r['pool_rows']:>10,}  matched={r['matched']:>10,}"
                f"  dropped={r['dropped']:>7,}  {r['coverage_pct']:6.3f}%{flag}"
            )
    if not ok:
        print(f"\n  !! COVERAGE WARNING: a season falls below {min_coverage:.1f}%.")
        print("     The occupancy figures for that season measure only the matched rows.")
    return ok


def _print_config(name: str, stats: dict[str, Any], min_cells: tuple[int, ...]) -> None:
    pct = stats["percentiles"]
    print(f"\n  {name}")
    gap = stats.get("seasons_missing") or []
    if gap:
        print(
            f"    !! asked for {stats.get('seasons')} but measured "
            f"{stats.get('seasons_measured')} — season(s) {gap} contributed NO rows."
        )
    print(
        f"    rows={stats['total_rows']:>10,}   nonempty={stats['cells_nonempty']:>5}/{N_CELLS}"
        f"   mean/cell={stats['mean_per_cell']:>8.1f}"
    )
    print("    pct  " + "  ".join(f"p{p}={pct[str(p)]:,}" for p in PERCENTILES))
    for m in min_cells:
        u = stats["under"][str(int(m))]
        print(f"    under {m:>4}: {u['cells']:>5} cells   DRAW SHARE {u['draw_share_pct']:>6.3f}%")


def _print_widening(name: str, widening: dict[str, Any], min_cells: tuple[int, ...]) -> None:
    """Print the widening ladder for one configuration."""
    print(f"\n--- {name}: widening ladder ---")
    print(f"      decided order: {' -> '.join(widening['decided_order'])}  ({WIDEN_ORDER_SOURCE})")
    for m in min_cells:
        key = str(int(m))
        base = widening["ladder"][0]["under"][key]["draw_share_pct"]
        print(f"    MIN_CELL={m}   baseline under-full draw share {base:6.3f}%")
        for step in widening["ladder"]:
            u = step["under"][key]
            if step["step"] == 0:
                continue
            print(
                f"      step {step['step']} {step['label']:<42} groups={step['n_groups']:>5}"
                f"  left {u['draw_share_pct']:6.3f}%"
                f"  recovers {u['recovered_pct_points']:6.3f} pts"
                f"  ({u['recovered_share_of_baseline_pct']:5.1f}% of the baseline)"
            )
        confirmed = widening["score_band_is_the_best_first_drop"][key]
        verdict = "CONFIRMS" if confirmed else "CONTRADICTS"
        print(
            f"      relaxable first drops, best first: {widening['admissible_first_drop_ranking'][key]}"
        )
        print(f"      -> the data {verdict} 'relax the score band first'")
        print(
            f"      diagnostic only (all five): {widening['first_drop_ranking'][key]}"
            f"  — never relax {list(WIDEN_INADMISSIBLE)}"
        )


def _print_emptiest(name: str, cells: list[dict[str, Any]]) -> None:
    print(f"\n--- {name}: the {len(cells)} emptiest REACHABLE cells ---")
    print("      (reference = pitch pool, all seasons, both hands)")
    for c in cells:
        print(
            f"    cell {c['cell_id']:>4}  n={c['count']:>6,}  ref={c['reference_count']:>8,}"
            f"   {c['label']}"
        )


def _print_doc_correction(stats: dict[str, Any], label: str) -> None:
    measured = stats["mean_per_cell"]
    median = stats["percentiles"]["50"]
    print("\n--- Correction to the remediation decision record ---")
    print(f"  The record states ~{_DOC_MEAN_PER_CELL:.0f} rows per cell on average for the")
    print(
        f"  per-hand batted-ball pool. MEASURED on {label}: mean {measured:.1f}, median {median:,}."
    )
    if measured > 0:
        print(f"  The stated figure is {_DOC_MEAN_PER_CELL / measured:.1f}x the measurement.")
    print("  Read the measurement, not the record, when SIM-475 sets MIN_CELL.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    min_cells = tuple(int(m) for m in args.min_cells)
    con, source = open_connection(args.duckdb, args.dsn, args.half_inning_source)
    try:
        ha_rows = build_half_inning_map(con, source)
        log.info("half-inning map (%s): %d plate appearances", source, ha_rows)

        recent = last_n_seasons(con, int(args.recent_seasons))
        every = all_seasons(con)
        season_sets = {"recent": recent, "all": every}

        counts = {t: season_row_counts(con, t) for t in _TABLES}
        coverage = {t: join_coverage(con, t) for t in _TABLES}

        # The reference for "reachable": the widest configuration there is.
        reference, _ = cell_occupancy(con, "pitch_pool", every, "ALL")

        configurations: dict[str, Any] = {}
        emptiest: dict[str, Any] = {}
        for table in _TABLES:
            for span, seasons in season_sets.items():
                for hand in _HANDS:
                    occ, per_season = cell_occupancy(con, table, seasons, hand)
                    key = f"{table}|{span}|{hand}"
                    measured = sorted(int(s) for s, n in per_season.items() if n > 0)
                    missing = [int(s) for s in seasons if int(s) not in set(measured)]
                    configurations[key] = {
                        "table": table,
                        "season_span": span,
                        "seasons": list(seasons),
                        "seasons_measured": measured,
                        "seasons_missing": missing,
                        "rows_by_season": {
                            str(int(s)): int(per_season[str(int(s))]) for s in measured
                        },
                        "hand": hand,
                        **occupancy_stats(occ, min_cells),
                        "widening": widening_recovery(occ, min_cells),
                    }
                    emptiest[key] = emptiest_reachable(occ, reference, int(args.top_empty))
                    log.info(
                        "%s: %d rows over seasons %s (missing %s)",
                        key,
                        configurations[key]["total_rows"],
                        measured,
                        missing or "none",
                    )
    finally:
        con.close()

    unreachable = unreachable_cells(reference)

    # ---- human-readable report ----
    _print_header(
        duckdb_path=args.duckdb,
        half_inning_source=source,
        ha_rows=ha_rows,
        recent=recent,
        every=every,
    )
    _print_season_counts(counts)
    _print_coverage(coverage, float(args.min_coverage))

    health = health_report(
        season_counts=counts,
        coverage=coverage,
        configurations=configurations,
        min_coverage=float(args.min_coverage),
    )

    for table in _TABLES:
        print(f"\n--- sim.{table} ---")
        for span in ("recent", "all"):
            for hand in _HANDS:
                key = f"{table}|{span}|{hand}"
                label = f"{span} seasons {season_sets[span]}  hand={hand}"
                _print_config(label, configurations[key], min_cells)

    _print_doc_correction(configurations["outcome_pool|recent|L"], "outcome_pool, recent, hand L")

    for table, span, hand in _PRINT_EMPTIEST:
        key = f"{table}|{span}|{hand}"
        _print_widening(
            f"sim.{table} {span} hand={hand}", configurations[key]["widening"], min_cells
        )

    for table, span, hand in _PRINT_EMPTIEST:
        key = f"{table}|{span}|{hand}"
        _print_emptiest(f"sim.{table} {span} hand={hand}", emptiest[key])

    print(f"\n--- Cells the reference never reaches: {len(unreachable)} ---")
    for c in unreachable[: int(args.top_empty)]:
        print(f"    cell {c['cell_id']:>4}   {c['label']}")
    if not unreachable:
        print("    none — the cell definition reaches every cell it should.")

    # ---- machine-readable report ----
    report = {
        "ticket": "SIM-451",
        "script_version": SCRIPT_VERSION,
        "params": {
            "duckdb_path": args.duckdb,
            "dsn_host": _redact_dsn(args.dsn),
            "half_inning_source_requested": args.half_inning_source,
            "half_inning_source_used": source,
            "half_inning_rows": ha_rows,
            "recency_floor_seasons": recency_floor_seasons(),
            "recent_seasons": recent,
            "all_seasons": every,
            "min_cells": list(min_cells),
            "top_empty": int(args.top_empty),
            "min_coverage_pct": float(args.min_coverage),
        },
        "cell_definition": {
            "n_cells": N_CELLS,
            "dimensions": {
                "base": N_BASE,
                "outs": N_OUTS,
                "count": N_COUNT,
                "score_band": N_BAND,
                "home_or_away": N_HOME,
            },
            "score_band_edges": list(SCORE_BAND_EDGES),
            "score_band_labels": list(SCORE_BAND_LABELS),
            "score_clamp": SCORE_CLAMP,
            "cell_key_formula": CELL_KEY_FORMULA,
            "originated_by": "SIM-451 — SIM-475 must import SCORE_BAND_EDGES, not redefine it",
        },
        "widening_definition": {
            "dimensions": list(CELL_DIMENSIONS),
            "dimension_sizes": dict(DIMENSION_SIZES),
            "decided_order": list(WIDEN_ORDER),
            "decided_order_source": WIDEN_ORDER_SOURCE,
            "ladder": [list(step) for step in WIDEN_LADDER],
            "recovered_pct_points": (
                "baseline under-full draw share minus the share that survives the step"
            ),
            "originated_by": "SIM-451 — SIM-475 must import WIDEN_LADDER, not restate the order",
        },
        "season_row_counts": counts,
        "join_coverage": coverage,
        "coverage_ok": health["coverage_ok"],
        "health": health,
        "configurations": configurations,
        "emptiest_cells": emptiest,
        "unreachable_cells": unreachable,
    }
    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(f"\nWrote {args.output}")

    # The report is on disk either way. The exit code is what a wrapper reads.
    if not health["coverage_ok"]:
        # ASCII only in the failure banner. A Windows console defaults to a
        # legacy code page and mangles a non-ASCII dash, which is the one line
        # that must stay readable in a captured log.
        print("\n" + "!" * 78)
        print(f" MEASUREMENT NOT SOUND - coverage_ok is FALSE. Exit code {EXIT_UNHEALTHY}.")
        print("!" * 78)
        for problem in health["problems"]:
            print(f"  - {problem}")
        print(
            "\n  The JSON is written and it records the gap. Do not read a MIN_CELL"
            "\n  off a configuration listed above until the gap is closed."
        )
        return EXIT_UNHEALTHY
    print("\nCoverage is sound. Exit code 0.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="SIM-451 — measure the occupancy of the 2,880 hard-filter cells."
    )
    p.add_argument(
        "--duckdb", default=DEFAULT_DUCKDB_PATH, help="Sim DuckDB path (opened read only)."
    )
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN for the half-inning map.")
    p.add_argument(
        "--half-inning-source",
        choices=("postgres", "duckdb", "auto"),
        default="auto",
        help="Where home-or-away comes from (default %(default)s: Postgres, then DuckDB).",
    )
    p.add_argument(
        "--recent-seasons",
        type=int,
        default=recency_floor_seasons(),
        help="Seasons in the 'recent' configuration (default %(default)s = the recency floor).",
    )
    p.add_argument(
        "--min-cells",
        type=int,
        nargs="+",
        default=list(DEFAULT_MIN_CELLS),
        help="Candidate minimum cell sizes (default %(default)s).",
    )
    p.add_argument(
        "--top-empty",
        type=int,
        default=20,
        help="How many emptiest cells to name (default %(default)s).",
    )
    p.add_argument(
        "--min-coverage",
        type=float,
        default=95.0,
        help="Warn when a season's join coverage falls below this percent (default %(default)s).",
    )
    p.add_argument("--output", default=DEFAULT_OUTPUT, help="JSON report path.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
