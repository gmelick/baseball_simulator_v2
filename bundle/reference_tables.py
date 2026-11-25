"""Export the intentional-walk rates and the venue run factors into the bundle, and read them.

The export writes ``ibb_rates.json`` and ``venue_run_factors.json``, each a ``{"rows": [...]}``
document.
"""

from __future__ import annotations

import json
import logging
import os

import duckdb

from bundle.common import read_json

log = logging.getLogger("engine_artifacts")


#: The reference tables a worker reads from the bundle, so it never opens the
#: DuckDB file (a rebuild may hold its lock).
IBB_RATES_FILE = "ibb_rates.json"
VENUE_RUN_FACTORS_FILE = "venue_run_factors.json"


def ibb_rates_from_rows(rows) -> dict[tuple[int, int, bool, bool], float] | None:
    """The intentional-walk share per cell, from ``(rs, outs, late, close, opp, issued)``.

    The key is (runners_state, outs, late, close). Returns None when no row has an
    opportunity.
    """
    out = {
        (int(rs), int(o), bool(late), bool(close)): float(iss) / float(opp)
        for rs, o, late, close, opp, iss in rows
        if opp
    }
    return out or None


def venue_run_factors_from_rows(rows) -> dict[tuple[int, int], float] | None:
    """The ``(venue_id, season) -> factor`` map, without the implausible factors.

    The map keeps only the factors from 0.5 to 2.0. Returns None when none is left.
    """
    out = {
        (int(v), int(s)): float(f) for v, s, f in rows if f is not None and 0.5 <= float(f) <= 2.0
    }
    return out or None


def build_reference_tables(con: duckdb.DuckDBPyConnection, out_dir: str) -> dict[str, int]:
    """Write the intentional-walk rates and the venue run factors into the bundle.

    The rows are the ones the worker loaders read from DuckDB
    (``simulation/production_factory._load_ibb_rates`` and ``_load_venue_run_factors``).
    A table the database lacks writes no file, and the worker then falls back. Each file
    goes in under a temporary name and moves into place in one step. Returns the row
    count per file.
    """
    os.makedirs(out_dir, exist_ok=True)
    counts: dict[str, int] = {}
    tables = (
        (
            IBB_RATES_FILE,
            "SELECT runners_state, outs, is_late, is_close, opportunities, issued "
            "FROM sim.ibb_rates WHERE opportunities > 0 ORDER BY 1, 2, 3, 4",
        ),
        (
            VENUE_RUN_FACTORS_FILE,
            "SELECT venue_id, season, regressed_factor FROM derived.park_factors "
            "WHERE factor_type = 'R' AND regressed_factor IS NOT NULL ORDER BY 1, 2",
        ),
    )
    for name, sql in tables:
        try:
            rows = [list(r) for r in con.execute(sql).fetchall()]
        except duckdb.Error as exc:
            log.warning("SIM-563: %s not written (%s)", name, exc)
            continue
        tmp = os.path.join(out_dir, f"tmp_{name}")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"rows": rows}, fh)
        os.replace(tmp, os.path.join(out_dir, name))
        counts[name] = len(rows)
    log.info("SIM-563: reference tables written: %s", counts)
    return counts


def _read_reference_rows(art_dir: str, name: str) -> list | None:
    """The ``rows`` list of the bundle file ``name``, or None when the file is absent."""
    doc = read_json(art_dir, name)
    return None if doc is None else list(doc.get("rows", []))
