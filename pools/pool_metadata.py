"""The sim-pool build metadata: the builder version, the recency weights, the
incremental-rebuild gate, the build-record reader and the situation SQL the pool
builders share.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from profiles.sql import sql_seasons

log = logging.getLogger("player_profile_computor")

# ---------------------------------------------------------------------------
# The recency weights and the incremental-rebuild gate of the sim pools
# ---------------------------------------------------------------------------

# Bump on any formula change to a pool SELECT: the incremental-rebuild gate
# (_seasons_needing_rebuild) treats a season built by another version as stale.
# sim554.1 adds the steal pool's pitch class and pickoff rows (the running game
# on the pitch, SIM-554; migration 0031). CHANGES.md records every earlier
# version.
POOL_BUILDER_VERSION = "sim554.1"


RECENCY_RECENT_SEASONS = 2  # seasons (incl. ref) that get the full peak weight
RECENCY_DECAY = 0.75  # geometric decay per season beyond the recent window
RECENCY_FLOOR = 0.25
RECENCY_PEAK = 2.0


def recency_weight(season: int, ref_season: int) -> float:
    """The sampling recency weight of a row from ``season`` against ``ref_season``.

    2.0 for the most-recent two seasons, then x0.75 per additional season,
    floored at 0.25. This mirrors the SQL emitted into the pool builders so the
    Python reference and the materialized ``recency_weight`` column never diverge.
    """
    age = ref_season - season
    if age <= RECENCY_RECENT_SEASONS - 1:
        return RECENCY_PEAK
    return max(
        RECENCY_FLOOR, RECENCY_PEAK * (RECENCY_DECAY ** (age - (RECENCY_RECENT_SEASONS - 1)))
    )


def _recency_weight_sql(season_expr: str, ref_season: int) -> str:
    """SQL CASE expression mirroring ``recency_weight()`` for a season column."""
    age = f"({ref_season} - {season_expr})"
    return (
        f"CASE WHEN {age} <= {RECENCY_RECENT_SEASONS - 1} THEN {RECENCY_PEAK} "
        f"ELSE GREATEST({RECENCY_FLOOR}, {RECENCY_PEAK} * "
        f"POWER({RECENCY_DECAY}, {age} - {RECENCY_RECENT_SEASONS - 1})) END"
    )


def _canonical_ref_season(conn, seasons: list[int]) -> int:
    """The one recency reference season every sim pool shares.

    It is the newest of the seasons this run asks for and of every
    ``recency_ref_season`` already recorded in ``sim.pool_build_metadata``. Once
    one pool advances the reference, the others adopt it, so a row's
    ``recency_weight`` agrees between the pitch pool and the outcome pool built
    from it, across pools and across incremental runs.
    """
    ref = max(seasons)
    try:
        prev = conn.execute(
            "SELECT MAX(recency_ref_season) FROM sim.pool_build_metadata"
        ).fetchone()[0]
    except Exception:
        prev = None
    if prev is not None:
        ref = max(ref, int(prev))
    return ref


def _seasons_to_build(
    conn, pool_name: str, seasons: list[int], incremental: bool
) -> tuple[list[int], int] | None:
    """The seasons a build of ``sim.<pool_name>`` writes and its recency reference; None when all are fresh."""
    # The reference reads the FULL season list, before the incremental gate narrows it.
    ref_season = _canonical_ref_season(conn, seasons)
    if incremental:
        seasons = _seasons_needing_rebuild(conn, pool_name, seasons)
        if not seasons:
            log.info("Building sim.%s … all seasons fresh; skipped.", pool_name)
            return None
    log.info("Building sim.%s … (seasons=%s)", pool_name, seasons)
    return seasons, ref_season


def _source_state(conn, season: int):
    """``(MAX(game_date), COUNT(*))`` of usable source rows for ``season``.

    One scan over ``pg.raw.pitches`` (the quality rows only). The count
    catches a late or double-header row on a ``game_date`` already seen, which
    does NOT advance ``MAX(game_date)``. Returns ``(None, 0)`` when the source
    has no usable rows.
    """
    row = conn.execute(
        "SELECT MAX(game_date), COUNT(*) FROM pg.raw.pitches "
        "WHERE season = ? AND data_quality_flag = FALSE",
        [season],
    ).fetchone()
    if not row or len(row) < 2:
        return None, 0
    return row[0], row[1]


def _seasons_needing_rebuild(conn, pool_name: str, seasons: list[int]) -> list[int]:
    """The seasons whose source or builder changed since their last build.

    A season is fresh (skipped) only when all three hold:
      * the recorded ``source_max_game_date`` is ``>=`` the current
        ``MAX(game_date)`` in ``pg.raw.pitches`` (the date has not advanced);
      * the recorded ``source_row_count`` equals the current source row count
        (which catches a late or double-header row on a date already seen);
      * the season was built by the running ``POOL_BUILDER_VERSION`` (the
        watermark sees only the source data, so a FORMULA change needs this
        guard to land on the incremental path).

    A season with no metadata row (or no metadata table, or a row with no
    ``source_row_count``) is built.
    """
    try:
        meta = {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute(
                "SELECT season, source_max_game_date, source_row_count, builder_version "
                "FROM sim.pool_build_metadata WHERE pool_name = ?",
                [pool_name],
            ).fetchall()
        }
    except Exception:
        return list(seasons)
    stale: list[int] = []
    for s in seasons:
        src_max, src_cnt = _source_state(conn, s)
        prev = meta.get(s)
        if prev is None:
            stale.append(s)
            continue
        prev_max, prev_cnt, prev_builder = prev
        if src_max is None or prev_max is None:
            stale.append(s)
            continue
        # Fresh only when the date has not advanced AND the source row count is
        # unchanged AND the builder that produced the season is the running one.
        date_advanced = src_max > prev_max
        count_changed = prev_cnt is None or src_cnt != prev_cnt
        builder_changed = prev_builder != POOL_BUILDER_VERSION
        if date_advanced or count_changed or builder_changed:
            stale.append(s)
    return stale


def _record_pool_build(conn, pool_name: str, seasons: list[int], ref_season: int) -> None:
    """Upsert the build record of each (pool, season): the source watermark, the
    source row count and the recency reference.

    The source watermark (``source_max_game_date``) and row count
    (``source_row_count``) let the next incremental run detect a late row on a
    date already seen. ``row_count`` is the pool's own row count, for the logs.
    """
    for s in seasons:
        src_max, src_cnt = _source_state(conn, s)
        cnt = conn.execute(
            f"SELECT COUNT(*) FROM sim.{pool_name} WHERE season = ?", [s]
        ).fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO sim.pool_build_metadata "
            "(pool_name, season, row_count, source_max_game_date, "
            " source_row_count, recency_ref_season, builder_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [pool_name, s, cnt, src_max, src_cnt, ref_season, POOL_BUILDER_VERSION],
        )


# ---------------------------------------------------------------------------
# The build records
# ---------------------------------------------------------------------------


def read_pool_builds(
    conn: Any, pool_name: str, seasons: Iterable[int], columns: str = "builder_version"
) -> dict[int, tuple]:
    """``season -> (columns...)`` from ``sim.pool_build_metadata`` for one pool's ``seasons``, in season order."""
    rows = conn.execute(
        f"SELECT season, {columns} FROM sim.pool_build_metadata "
        f"WHERE pool_name = '{pool_name}' AND season IN ({sql_seasons(seasons)})"
    ).fetchall()
    return {int(row[0]): tuple(row[1:]) for row in sorted(rows, key=lambda row: row[0])}


def pool_builder_versions(conn: Any, pool_name: str, seasons: Iterable[int]) -> dict[int, Any]:
    """``season -> builder_version`` of one pool's ``seasons`` (:func:`read_pool_builds`)."""
    return {season: row[0] for season, row in read_pool_builds(conn, pool_name, seasons).items()}


# ---------------------------------------------------------------------------
# The situation columns the pool builders share
# ---------------------------------------------------------------------------


def sql_runners_state(q: str = "") -> str:
    """The base-state bitmask (bit 0 = 1B, bit 1 = 2B, bit 2 = 3B); ``q`` prefixes the columns."""
    return (
        f"(CASE WHEN {q}on_1b IS NOT NULL THEN 1 ELSE 0 END"
        f" + CASE WHEN {q}on_2b IS NOT NULL THEN 2 ELSE 0 END"
        f" + CASE WHEN {q}on_3b IS NOT NULL THEN 4 ELSE 0 END)::SMALLINT"
    )


def sql_score_diff(q: str = "") -> str:
    """The batting side's lead, clamped to -5..5; ``q`` prefixes the columns."""
    return f"GREATEST(-5, LEAST(5, {q}bat_score - {q}fld_score))::SMALLINT"
