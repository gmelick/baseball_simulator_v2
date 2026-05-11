"""scripts/backfill_odds_hash.py — SIM-157
=============================================
One-shot backfill that fills `raw.game_odds.odds_hash` for every legacy row
that predates SIM-092 (which began populating the column at write time but
left history alone).  After the hashes are populated, the script collapses
duplicate rows down to the *earliest* row per `(game_pk, source, odds_hash)`
group, leaving the table ready for the partial unique index to be promoted
to a full unique index in Alembic migration `0012`.

Pipeline (idempotent — safe to re-run):

  1. Compute `odds_hash` for every row where `odds_hash IS NULL`,
     using `LiveIngestionPipeline._odds_hash()` so the format matches new
     writes byte-for-byte.  Updates are performed in batches of 10 000.

  2. After the column is fully populated, collapse duplicates: for each
     `(game_pk, source, odds_hash)` group with COUNT > 1, retain the row
     with the smallest `received_at` (the earliest snapshot — the moment
     the line first existed in the wild) and DELETE the rest.

  3. Report row-counts before/after so the storage win is measurable.

Usage::

    BASEBALL_DB_DSN=postgresql://... python scripts/backfill_odds_hash.py
    # Dry-run (no UPDATE / DELETE):
    BASEBALL_DB_DSN=postgresql://... python scripts/backfill_odds_hash.py --dry-run

Acceptance gates (SIM-157):

  * `SELECT COUNT(*) FROM raw.game_odds WHERE odds_hash IS NULL` == 0
  * `SELECT game_pk, source, odds_hash, COUNT(*)
       FROM raw.game_odds GROUP BY 1,2,3 HAVING COUNT(*) > 1` returns 0 rows.

Once both gates pass, run Alembic migration `0012_sim157_game_odds_full_unique`
to drop the partial unique index and replace it with a full unique index.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# `LiveIngestionPipeline._odds_hash` is the canonical hash function — reuse
# it so backfill hashes are byte-identical to live-pipeline hashes.
import asyncpg  # noqa: E402

from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline  # noqa: E402

log = logging.getLogger("backfill_odds_hash")

# Column ordering must match the LiveIngestionPipeline._odds_hash() input
# `odds` dict — these are the keys used to compute the fingerprint.
ODDS_COLUMNS = (
    "book",
    "line_type",
    "market_type",
    "is_sharp_book",
    "home_ml",
    "away_ml",
    "home_spread",
    "home_spread_ml",
    "away_spread",
    "away_spread_ml",
    "total_line",
    "over_ml",
    "under_ml",
)

BATCH_SIZE = 10_000


async def _connect(dsn: str) -> asyncpg.Connection:
    return await asyncpg.connect(dsn=dsn)


async def _row_counts(conn: asyncpg.Connection) -> tuple[int, int]:
    """Return (total_rows, null_hash_rows)."""
    total = await conn.fetchval("SELECT COUNT(*) FROM raw.game_odds")
    null_count = await conn.fetchval("SELECT COUNT(*) FROM raw.game_odds WHERE odds_hash IS NULL")
    return total, null_count


async def _backfill_hashes(conn: asyncpg.Connection, dry_run: bool) -> int:
    """Pass 1 — populate odds_hash on legacy NULL rows in batches.

    Returns the number of rows updated.
    """
    cols = ", ".join(ODDS_COLUMNS)
    fetch_sql = f"""
        SELECT id, {cols}
          FROM raw.game_odds
         WHERE odds_hash IS NULL
         ORDER BY id
         LIMIT $1
    """
    update_sql = "UPDATE raw.game_odds SET odds_hash = $2 WHERE id = $1"

    total_updated = 0
    while True:
        rows = await conn.fetch(fetch_sql, BATCH_SIZE)
        if not rows:
            break

        # Compute hashes outside the DB round-trip
        updates = []
        for r in rows:
            payload = {k: r[k] for k in ODDS_COLUMNS}
            h = LiveIngestionPipeline._odds_hash(payload)
            updates.append((r["id"], h))

        if dry_run:
            total_updated += len(updates)
            log.info(
                "  [dry-run] would update %d rows (running total %d)", len(updates), total_updated
            )
            # In dry-run we cannot actually write, so we'd loop forever.
            # Bail after the first batch — it's enough to confirm the path.
            break

        async with conn.transaction():
            await conn.executemany(update_sql, updates)
        total_updated += len(updates)
        log.info("  updated %d rows (running total %d)", len(updates), total_updated)

    return total_updated


async def _dedup_rows(conn: asyncpg.Connection, dry_run: bool) -> tuple[int, int]:
    """Pass 2 — keep only the earliest row per (game_pk, source, odds_hash).

    Returns (groups_with_duplicates, rows_deleted).
    """
    # First, count how many duplicates we'll remove so we can report.
    dup_stats_sql = """
        WITH dups AS (
            SELECT game_pk, source, odds_hash, COUNT(*) AS n
              FROM raw.game_odds
             WHERE odds_hash IS NOT NULL
          GROUP BY game_pk, source, odds_hash
            HAVING COUNT(*) > 1
        )
        SELECT COUNT(*)::BIGINT AS groups,
               COALESCE(SUM(n - 1), 0)::BIGINT AS extras
          FROM dups
    """
    row = await conn.fetchrow(dup_stats_sql)
    groups = int(row["groups"] or 0)
    extras = int(row["extras"] or 0)

    if extras == 0:
        log.info("  no duplicate (game_pk, source, odds_hash) groups — skipping DELETE")
        return groups, 0

    if dry_run:
        log.info("  [dry-run] would DELETE %d row(s) across %d duplicate groups", extras, groups)
        return groups, extras

    # Use a CTE that picks the smallest id per duplicate group as the survivor
    # (id is monotonic and serves as a deterministic tiebreaker even when
    # received_at ties across rows).  Anything else in the group is deleted.
    delete_sql = """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY game_pk, source, odds_hash
                       ORDER BY received_at NULLS LAST, id
                   ) AS rn
              FROM raw.game_odds
             WHERE odds_hash IS NOT NULL
        )
        DELETE FROM raw.game_odds
         WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
    """
    async with conn.transaction():
        result = await conn.execute(delete_sql)
    # `execute` returns "DELETE N" — parse it for the actual row count
    deleted = int(result.split()[-1]) if result.startswith("DELETE") else extras
    log.info("  deleted %d duplicate row(s) across %d group(s)", deleted, groups)
    return groups, deleted


async def _validate(conn: asyncpg.Connection) -> bool:
    """SIM-157 acceptance gates.  Returns True iff both pass."""
    null_count = await conn.fetchval("SELECT COUNT(*) FROM raw.game_odds WHERE odds_hash IS NULL")
    dup_count = await conn.fetchval(
        """
        SELECT COUNT(*)
          FROM (
              SELECT 1
                FROM raw.game_odds
               WHERE odds_hash IS NOT NULL
            GROUP BY game_pk, source, odds_hash
              HAVING COUNT(*) > 1
          ) t
        """
    )
    log.info("Validation:")
    log.info("  rows with NULL odds_hash         : %d (must be 0)", null_count)
    log.info("  groups with duplicate odds_hash  : %d (must be 0)", dup_count)
    return null_count == 0 and dup_count == 0


async def main_async(dsn: str, dry_run: bool) -> int:
    conn = await _connect(dsn)
    try:
        before_total, before_null = await _row_counts(conn)
        log.info("Pre-backfill row counts:")
        log.info("  total rows           : %d", before_total)
        log.info("  rows with NULL hash  : %d", before_null)

        t0 = time.time()
        log.info("Pass 1 — backfilling odds_hash on legacy rows ...")
        updated = await _backfill_hashes(conn, dry_run=dry_run)
        log.info("  pass 1 completed: %d row(s) updated in %.1fs", updated, time.time() - t0)

        t0 = time.time()
        log.info("Pass 2 — collapsing duplicate (game_pk, source, odds_hash) groups ...")
        groups, deleted = await _dedup_rows(conn, dry_run=dry_run)
        log.info(
            "  pass 2 completed: %d duplicate group(s), %d row(s) deleted in %.1fs",
            groups,
            deleted,
            time.time() - t0,
        )

        after_total, after_null = await _row_counts(conn)
        log.info("Post-backfill row counts:")
        log.info("  total rows           : %d (delta %+d)", after_total, after_total - before_total)
        log.info("  rows with NULL hash  : %d (delta %+d)", after_null, after_null - before_null)

        if dry_run:
            log.info("Dry-run — skipping acceptance-gate validation.")
            return 0

        ok = await _validate(conn)
        return 0 if ok else 1
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be backfilled / deleted but do not write to the database.",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("BASEBALL_DB_DSN"),
        help="PostgreSQL DSN (defaults to $BASEBALL_DB_DSN).",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable DEBUG-level logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.dsn:
        parser.error("DSN is required: pass --dsn or set BASEBALL_DB_DSN")

    rc = asyncio.run(main_async(args.dsn, dry_run=args.dry_run))
    sys.exit(rc)


if __name__ == "__main__":
    main()
