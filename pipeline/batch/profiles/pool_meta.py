"""The sim-pool build metadata: the builder version, the recency weights, the
incremental-rebuild gate, the steal-pool migration guard and the index DDL helper.
"""

from __future__ import annotations

from db.introspect import duck_columns

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


#: The two columns migration 0031 appends to sim.steal_opportunity_pool, and
#: the file that adds them.
STEAL_POOL_0031_COLUMNS: tuple[str, ...] = ("pitch_class", "is_pickoff_row")
MIGRATION_0031 = "db/migrations/duckdb/0031_sim554_steal_pool_pitch_class.sql"


def _require_steal_pool_0031(conn) -> None:
    """Raise unless sim.steal_opportunity_pool carries migration 0031.

    The nightly job applies no DuckDB migration. The steal-pool builder deletes
    a season's rows, then runs a positional INSERT. On an un-migrated table the
    INSERT fails AFTER the delete, and the season's steal rows are gone. The
    builder calls this probe first, so the build fails with the rows intact.

    A database with no such table has no rows to lose: the probe passes, and
    the DELETE then fails on the missing table by itself.
    """
    have = duck_columns(conn, "sim", "steal_opportunity_pool", this_file_only=True)
    if not have:
        return
    missing = [c for c in STEAL_POOL_0031_COLUMNS if c not in have]
    if missing:
        raise RuntimeError(
            f"sim.steal_opportunity_pool has no {', '.join(missing)} column: apply "
            f"{MIGRATION_0031} before the pool build (SIM-554). No row was deleted."
        )


# ---------------------------------------------------------------------------
# The index recreate after a bulk delete
# ---------------------------------------------------------------------------


def _ensure_index_if_not_exists(create_index_sql: str) -> str:
    """Inject ``IF NOT EXISTS`` into a ``CREATE [UNIQUE] INDEX`` statement.

    It makes an index recreate idempotent, so a rerun of a profile build (or
    of a half-applied earlier run) cannot fail on an index that is already
    there. A statement that already has the clause, or is not a CREATE INDEX
    form this recognises, comes back unchanged (the caller drops the index IF
    EXISTS first anyway).
    """
    stmt = create_index_sql.strip()
    low = stmt.lower()
    if "if not exists" in low:
        return stmt
    for prefix in ("create unique index ", "create index "):
        if low.startswith(prefix):
            return stmt[: len(prefix)] + "IF NOT EXISTS " + stmt[len(prefix) :]
    return stmt
