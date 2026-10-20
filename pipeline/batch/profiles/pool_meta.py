"""The sim-pool build metadata: the builder version, the recency weights, the
incremental-rebuild gate, the steal-pool migration guard and the index DDL helper.
"""

from __future__ import annotations

from db.introspect import duck_columns

# ---------------------------------------------------------------------------
# SIM-076 / SIM-095: sim-pool recency weighting + incremental rebuild helpers
# ---------------------------------------------------------------------------

# Bump on any formula change to the pool SELECTs — the incremental rebuild
# gate (_seasons_needing_rebuild) treats a season built by another version as
# stale. sim501.1 = events-derived result_outs (SIM-501a).
# sim509.1 = hit_by_pitch is its own pitch outcome_type (SIM-509) + the SIM-506
# steal labels and SIM-507 pickoff labels on the opportunity pool.
# sim491.1 = bat_home (the batting side) on sim.outcome_pool (SIM-491 /
# migration 0019 — the SIM-412 home-field rebuild as a draw weight).
# sim515.1 = sim.ibb_rates (the measured IBB rate table — SIM-515 / migration
# 0020) joins the pool-build chain.
# sim517.1 = catcher_id + got_away on sim.pitch_pool (SIM-517 / migration 0022).
# sim518.1 = bat_home + pitcher_pitch_count + times_through_order on
# sim.pitch_pool (SIM-518 / migration 0023 — the draw-conditioning columns).
# sim523g.1 = the fielding chain on sim.outcome_pool (SIM-523 part G).
# sim553.1 = a two-strike foul tip / foul bunt is strike three in
# sim.pitch_pool.outcome_type; 'O' / 'Q' / 'R' / 'P' coded explicitly; no ELSE
# (SIM-553 — SQL_OUTCOME_TYPE).
# sim554.1 = pitch_class + is_pickoff_row on sim.steal_opportunity_pool: the
# class of the pitch each row rode, and a row of its own for each pickoff
# outcome thrown before any pitch of its pair (SIM-554 / migration 0031).
POOL_BUILDER_VERSION = "sim554.1"


RECENCY_RECENT_SEASONS = 2  # seasons (incl. ref) that get the full peak weight


RECENCY_DECAY = 0.75  # geometric decay per season beyond the recent window


RECENCY_FLOOR = 0.25


RECENCY_PEAK = 2.0


def recency_weight(season: int, ref_season: int) -> float:
    """Sampling recency weight for a row from ``season`` vs ``ref_season`` (SIM-076).

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
    """The single recency reference season shared by ALL three sim pools (SIM-345).

    Previously each pool computed ``ref_season = max(seasons)`` from its own
    argument, so a partial/incremental call could give pools different
    references — and a row's ``recency_weight`` would then disagree between the
    pitch_pool and the outcome_pool built from it. This pins one canonical
    reference: the newest of the seasons requested in THIS run and every
    ``recency_ref_season`` already recorded in ``sim.pool_build_metadata``. Once
    a pool advances the reference, the others adopt the same value, so the
    weights stay consistent across pools and across incremental runs.
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

    Single scan over ``pg.raw.pitches`` (quality rows only). The count is the
    SIM-345 row-count guard so a same-``game_date`` late/doubleheader row — which
    does NOT advance ``MAX(game_date)`` — is still detected. Returns
    ``(None, 0)`` when the source has no usable rows (or an empty result).
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
    """Subset of ``seasons`` whose source changed since the last build (SIM-095).

    SIM-345 freshness contract — a season is fresh (skipped) only when BOTH:
      * the recorded watermark ``source_max_game_date`` is ``>=`` the current
        ``MAX(game_date)`` in ``pg.raw.pitches`` (date has not advanced), AND
      * the recorded ``source_row_count`` equals the current source row count.

    The previous strict ``>`` watermark silently skipped a season when a late or
    doubleheader row landed on an ALREADY-SEEN ``game_date`` (the max did not
    move). Switching the staleness test to ``src_max >= prev`` and adding the
    row-count guard catches those same-date additions without rebuilding an
    unchanged season every night. If the metadata table is absent or has no row
    for a season (or a legacy row predates ``source_row_count``), the season is
    (re)built.

    SIM-501a adds the builder-version guard: a season built by a DIFFERENT
    ``POOL_BUILDER_VERSION`` is stale even when its source is unchanged. The
    watermark test sees only the source data, so without this guard a FORMULA
    change (like the SIM-501a ``result_outs`` fix) never lands on the default
    incremental path — the sibling ``play_pool_cache`` already gates on its
    builder version for the same reason.
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
    """Upsert per-(pool, season) build watermark + recency reference (SIM-076/095).

    Records both the source watermark (``source_max_game_date``) and the source
    row count (``source_row_count``, SIM-345 guard) so the next incremental run
    can detect same-date late rows. ``row_count`` remains the materialized pool
    row count for observability.
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


#: SIM-554: the two columns migration 0031 appends to sim.steal_opportunity_pool,
#: and the file that adds them.
STEAL_POOL_0031_COLUMNS: tuple[str, ...] = ("pitch_class", "is_pickoff_row")


MIGRATION_0031 = "db/migrations/duckdb/0031_sim554_steal_pool_pitch_class.sql"


def _require_steal_pool_0031(conn) -> None:
    """SIM-554: raise unless sim.steal_opportunity_pool carries migration 0031.

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
# Index-recreate hardening (SIM-419)
# ---------------------------------------------------------------------------


def _ensure_index_if_not_exists(create_index_sql: str) -> str:
    """Inject ``IF NOT EXISTS`` into a ``CREATE [UNIQUE] INDEX`` statement.

    Makes an index recreate idempotent so re-running a profile rebuild (or a
    partially-applied prior run) can't fail on an already-present index. Returns
    the statement unchanged if it already has the clause or isn't a recognised
    CREATE INDEX form (the caller still DROPs IF EXISTS first as a safety net).
    """
    stmt = create_index_sql.strip()
    low = stmt.lower()
    if "if not exists" in low:
        return stmt
    for prefix in ("create unique index ", "create index "):
        if low.startswith(prefix):
            return stmt[: len(prefix)] + "IF NOT EXISTS " + stmt[len(prefix) :]
    return stmt
