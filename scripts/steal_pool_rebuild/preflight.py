"""Step 0, the pre-flight: every reason the write path must not start."""

from __future__ import annotations

import json
import os
import shutil
from typing import Any

import pipeline.batch.player_profile_computor as ppc
from pipeline.batch.engine_artifacts import RECENCY_FLOOR_SEASONS

from .bundle import _export_replaces, _strays, backup_plan, steal_pool_problem
from .common import (
    EXPECTED_BUILDER,
    MIGRATION,
    PITCH_POOL_BUILDERS,
    POOL,
    _builder,
    _log,
    _season_list,
    _tree_files,
)
from .migration import missing_columns

# ---------------------------------------------------------------------------
# Pre-flight (no write)
# ---------------------------------------------------------------------------


def _steal_counts(con: Any, seasons: list[int]) -> dict[int, int]:
    return {
        int(s): int(n)
        for s, n in con.execute(
            f"SELECT season, COUNT(*) FROM {POOL} WHERE season IN ({_season_list(seasons)}) "
            "GROUP BY 1 ORDER BY 1"
        ).fetchall()
    }


def _steal_versions(con: Any, seasons: list[int]) -> dict[int, str | None]:
    return {
        int(s): v
        for s, v in con.execute(
            "SELECT season, builder_version FROM sim.pool_build_metadata "
            f"WHERE pool_name = 'steal_opportunity_pool' AND season IN ({_season_list(seasons)})"
        ).fetchall()
    }


def pitch_pool_problems(con: Any, seasons: list[int]) -> list[str]:
    """The window's pitch pool must be present per season and built by a
    builder whose class the steal pool may copy."""
    sl = _season_list(seasons)
    rows = {
        int(s): int(n)
        for s, n in con.execute(
            f"SELECT season, COUNT(*) FROM sim.pitch_pool WHERE season IN ({sl}) GROUP BY 1"
        ).fetchall()
    }
    builders = {
        int(s): v
        for s, v in con.execute(
            "SELECT season, builder_version FROM sim.pool_build_metadata "
            f"WHERE pool_name = 'pitch_pool' AND season IN ({sl})"
        ).fetchall()
    }
    problems: list[str] = []
    for s in seasons:
        good = rows.get(s, 0) > 0 and builders.get(s) in PITCH_POOL_BUILDERS
        _log(
            f"  pitch pool {s}: {rows.get(s, 0):,} rows, builder {builders.get(s)}  "
            f"{'OK' if good else 'STOP'}"
        )
        if not good:
            problems.append(
                f"{s}: the pitch pool holds {rows.get(s, 0):,} rows built by "
                f"{builders.get(s)!r}; the steal pool's class comes from a pitch pool built by "
                f"{' or '.join(PITCH_POOL_BUILDERS)}. Rebuild the pitch pool first"
            )
    return problems


def watermark_problems(con: Any, seasons: list[int]) -> list[str]:
    """The steal pool's build watermark must equal ``raw.pitches`` today, per
    season. Otherwise the rebuild adds or drops pitch rows, and check b
    (every new row is a pickoff row) stops it after the work."""
    meta = {
        int(s): (d, n)
        for s, d, n in con.execute(
            "SELECT season, source_max_game_date, source_row_count FROM sim.pool_build_metadata "
            f"WHERE pool_name = 'steal_opportunity_pool' AND season IN ({_season_list(seasons)})"
        ).fetchall()
    }
    problems: list[str] = []
    for s in seasons:
        src_max, src_n = ppc._source_state(con, s)
        prev = meta.get(s)
        good = prev is not None and prev[0] == src_max and prev[1] == src_n
        _log(
            f"  steal pool {s}: built from raw.pitches through {prev[0] if prev else None} "
            f"({(prev[1] or 0) if prev else 0:,} rows); raw.pitches now through {src_max} "
            f"({int(src_n or 0):,} rows)  {'OK' if good else 'STOP'}"
        )
        if not good:
            problems.append(
                f"{s}: raw.pitches changed since the steal pool's last build (or the season has "
                "no build record), so the rebuild would add pitch rows the checks forbid. Run the "
                "pool chain for the season first"
            )
    return problems


def bundle_problems(art_dir: str, backup_dir: str, window: list[int], force: bool) -> list[str]:
    """The bundle's steal pool and the rollback point.

    The bundle's pool must be present and writable, with no stray file the
    export does not replace. It must be whole, unless a whole copy is kept as
    the rollback point: then it may be half-written by an earlier run of this
    rebuild, and the export rewrites it. The rollback point must hold the
    window's seasons. The copy's parent must be writable, with room for it.
    """
    pool_dir = os.path.join(art_dir, "steal_pool")
    if not os.path.isdir(pool_dir):
        return [f"the bundle has no steal pool: {pool_dir} is missing"]
    plan = backup_plan(art_dir, backup_dir, force)
    if plan.note:
        _log(f"  backup: {plan.note}")
    problems = list(plan.problems)
    kept = [s for s in _strays(pool_dir) if not _export_replaces(s)]
    if kept:
        problems.append(
            f"the bundle's steal_pool/ holds {kept}, which the export neither writes nor "
            "replaces. Move them out of the bundle first"
        )
    why = steal_pool_problem(pool_dir)
    if why is None:
        _log("  the bundle's steal pool is whole by its own manifest")
    elif plan.action == "keep" and not kept:
        _log(f"  the bundle's steal pool is not whole ({why}); the export rewrites it")
    elif not plan.problems:
        problems.append(f"the bundle's steal pool is not whole ({why}), and no copy is kept")
    source = backup_dir if plan.action == "keep" else pool_dir
    try:
        with open(os.path.join(source, "manifest.json"), encoding="utf-8") as fh:
            seasons = [int(s) for s in json.load(fh).get("seasons", [])]
        if seasons != window:
            problems.append(
                f"{source} holds the seasons {seasons}; the export writes {window}, the same window"
            )
    except (OSError, ValueError) as exc:
        if not plan.problems:
            problems.append(f"{source}/manifest.json does not read ({exc})")
    if not os.access(pool_dir, os.W_OK):
        problems.append(f"no write access to {pool_dir}")
    for name in sorted(os.listdir(pool_dir)):
        p = os.path.join(pool_dir, name)
        if os.path.isfile(p) and not os.access(p, os.W_OK):
            problems.append(f"no write access to {p}")
    parent = os.path.dirname(os.path.abspath(backup_dir))
    if not os.access(parent, os.W_OK):
        problems.append(f"no write access to {parent} (the copy goes there)")
    elif plan.action != "keep":
        size = sum(_tree_files(pool_dir).values())
        free = shutil.disk_usage(parent).free
        if free < 2 * size:
            problems.append(
                f"{free / 1e9:.2f} GB free under {parent}; the copy needs {size / 1e9:.2f} GB"
            )
    return problems


def preflight(
    con: Any,
    seasons: list[int],
    window: list[int],
    art_dir: str,
    backup_dir: str,
    *,
    force: bool,
    apply_migration_flag: bool,
) -> list[str]:
    """Every reason the write path must not start. An empty list means go.

    A missing migration is a problem only without ``--apply-migration``; with
    it, the write path applies the file INSIDE the rebuild's transaction,
    after the snapshot (step 2), so a failed rebuild rolls the two columns
    back with the rows. Never apply 0031 separately: a failed rebuild cannot
    remove columns that were on the table before it began.
    """
    problems: list[str] = []
    if _builder() != EXPECTED_BUILDER:
        problems.append(
            f"POOL_BUILDER_VERSION is {_builder()!r}; this script rebuilds for {EXPECTED_BUILDER!r}"
        )
    _log(f"  window (the {RECENCY_FLOOR_SEASONS} newest pool seasons, the export's): {window}")
    left_out = [s for s in window if s not in seasons]
    if left_out:
        problems.append(
            f"the seasons {seasons} leave out the window seasons {left_out}; the export writes "
            "the whole window, on one builder"
        )
    missing = missing_columns(con)
    if missing and apply_migration_flag:
        _log(
            f"  migration 0031: the pool lacks {missing}; --apply-migration applies it inside "
            "the rebuild's transaction (step 2)"
        )
        if not MIGRATION.is_file():
            problems.append(f"{MIGRATION} is missing; it adds {missing}")
    elif missing:
        problems.append(
            f"migration 0031 is not applied (sim.steal_opportunity_pool lacks {missing}). Pass "
            "--apply-migration: the rebuild applies it inside its transaction, so a failed "
            "rebuild rolls the columns back. Do not apply 0031 separately"
        )
    else:
        _log(
            "  migration 0031: applied before this run (pitch_class and is_pickoff_row are on "
            "the pool); a failed rebuild keeps these columns"
        )
    problems += pitch_pool_problems(con, seasons)
    problems += watermark_problems(con, seasons)
    problems += bundle_problems(art_dir, backup_dir, window, force)
    return problems
