"""
reload_games.py — corrective reload for SPECIFIC games, or for every gap it can find.

WHY THIS EXISTS
---------------
After a full sweep, "a few games failed" is not a self-describing state. Three very
different things look similar in the logs, and only one of them needs a reload:

  A. STALE      — rows exist in ``raw.pitches`` but there is NO ``raw.etl_game_ingest``
                  row. The sweep never actually loaded this game, so its rows are
                  left over from an older ingest and still carry the pre-SIM-440
                  parser bugs (``isOut`` read from the wrong dict → baserunner-out
                  flags all FALSE; ``rbi`` from the wrong level → zero RBIs;
                  4x-inflated substitution flags; switch-hitter spray angles NULL).
                  **This is the dangerous case: it looks loaded and is not.**
  B. FAILED     — a ledger row whose outcome is not ``loaded`` for a game whose
                  status IS ``Final``. A real failure. Needs a reload.
  C. NOT PLAYED — a ledger row with outcome ``empty`` for a game whose status is
                  ``Cancelled``/``Postponed``. Zero pitches is the CORRECT answer;
                  the ``empty`` outcome is the SIM-441 terminal marker that stops it
                  being retried nightly forever. **Reloading these fixes nothing.**

This script separates them, reloads A and B, and reports C so you can see it was
considered rather than missed.

Class A is what a resumable sweep can silently leave behind: ``_dispatch_game``
swallows per-game errors by design, so a wrapper that records progress on "the
dispatcher returned" marks failures as done. That is fixed in
``resumable_sweep.py``, but any game already lost that way needs this script.

SAFETY
------
``reload_game`` is DELETE + re-INSERT inside ONE transaction, and the shrink guard
refuses to replace a game with fewer rows than it removed (``ReloadShrinkError``)
rather than silently dropping data. So this is safe to interrupt and safe to re-run.
If a game legitimately should shrink, pass ``--allow-shrink`` — deliberately not the
default, because "the new parse produced fewer rows" is usually a bug, not an intent.

USAGE
-----
    python scripts/reload_games.py --dry-run          # what needs reloading, and why
    python scripts/reload_games.py                    # reload every discovered gap
    python scripts/reload_games.py --game-pk 529440 632924
    python scripts/reload_games.py --season 2021      # restrict discovery to one season
    python scripts/reload_games.py --allow-shrink     # permit a smaller re-parse

Verification is built in: each game reports rows-before → rows-after, and the run
ends with a per-season ledger-vs-pitches reconciliation, which is the check that
surfaced the stale games in the first place.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Rows exist but no ledger row -> never actually loaded by the sweep (class A).
_Q_STALE = """
    SELECT p.season, p.game_pk, count(*) AS rows
    FROM raw.pitches p
    LEFT JOIN raw.etl_game_ingest e ON e.game_pk = p.game_pk
    WHERE e.game_pk IS NULL
    GROUP BY 1, 2
    ORDER BY 1, 2
"""

# Ledger says not-loaded, but the game WAS played -> a real failure (class B).
_Q_FAILED = """
    SELECT e.season, e.game_pk, coalesce(e.pitch_rows, 0), coalesce(g.status, '?')
    FROM raw.etl_game_ingest e
    LEFT JOIN raw.games g ON g.game_pk = e.game_pk
    WHERE e.outcome <> 'loaded' AND coalesce(g.status, '') = 'Final'
    ORDER BY 1, 2
"""

# Ledger says not-loaded and the game was NOT played -> correct as-is (class C).
_Q_NOT_PLAYED = """
    SELECT e.season, e.game_pk, e.outcome, coalesce(g.status, '?')
    FROM raw.etl_game_ingest e
    LEFT JOIN raw.games g ON g.game_pk = e.game_pk
    WHERE e.outcome <> 'loaded' AND coalesce(g.status, '') <> 'Final'
    ORDER BY 1, 2
"""

# A Final game with neither pitch rows nor a ledger row -> never touched at all.
_Q_NEVER_LOADED = """
    SELECT g.season, g.game_pk
    FROM raw.games g
    LEFT JOIN raw.etl_game_ingest e ON e.game_pk = g.game_pk
    LEFT JOIN (SELECT DISTINCT game_pk FROM raw.pitches) p ON p.game_pk = g.game_pk
    WHERE g.status = 'Final' AND e.game_pk IS NULL AND p.game_pk IS NULL
    ORDER BY 1, 2
"""

_Q_RECONCILE = """
    SELECT coalesce(l.season, p.season) AS season,
           coalesce(l.n_loaded, 0)      AS ledger_loaded,
           coalesce(p.n_games, 0)       AS games_in_pitches,
           coalesce(p.n_rows, 0)        AS pitch_rows
    FROM (SELECT season, count(*) AS n_loaded FROM raw.etl_game_ingest
          WHERE outcome = 'loaded' GROUP BY 1) l
    FULL JOIN (SELECT season, count(DISTINCT game_pk) AS n_games, count(*) AS n_rows
               FROM raw.pitches GROUP BY 1) p ON p.season = l.season
    ORDER BY 1
"""


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    envfile = REPO / ".env"
    if not envfile.exists():
        raise SystemExit(f"no .env at {envfile}")
    for line in envfile.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _host_dsn(env: dict[str, str]) -> str:
    """Rewrite the in-container DSN so the host reaches the published port."""
    dsn = env.get("BASEBALL_DB_DSN")
    if not dsn:
        raise SystemExit("BASEBALL_DB_DSN not found in .env")
    return re.sub(r"@[^/:]+:\d+/", f"@127.0.0.1:{env.get('DB_HOST_PORT', '5432')}/", dsn)


def _fetch(dsn: str, sql: str) -> list[tuple]:
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    finally:
        conn.close()


def _row_count(dsn: str, game_pk: int) -> int:
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM raw.pitches WHERE game_pk = %s", (game_pk,))
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def _discover(dsn: str, season: int | None) -> tuple[list[tuple[int, int, str]], list[tuple]]:
    """Return (targets, not_played). Each target is (season, game_pk, reason)."""
    targets: list[tuple[int, int, str]] = []

    for s, pk, rows in _fetch(dsn, _Q_STALE):
        targets.append((s, pk, f"STALE — {rows} rows but no ledger row (pre-fix parser data)"))
    for s, pk, rows, status in _fetch(dsn, _Q_FAILED):
        targets.append((s, pk, f"FAILED — status={status}, ledger rows={rows}"))
    for s, pk in _fetch(dsn, _Q_NEVER_LOADED):
        targets.append((s, pk, "NEVER LOADED — Final game with no rows and no ledger row"))

    not_played = _fetch(dsn, _Q_NOT_PLAYED)

    if season is not None:
        targets = [t for t in targets if t[0] == season]
        not_played = [n for n in not_played if n[0] == season]
    # Stable, de-duplicated, chronological.
    seen: set[int] = set()
    unique = []
    for s, pk, why in sorted(targets):
        if pk not in seen:
            seen.add(pk)
            unique.append((s, pk, why))
    return unique, not_played


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--game-pk",
        type=int,
        nargs="+",
        default=None,
        help="reload exactly these games instead of discovering gaps",
    )
    ap.add_argument("--season", type=int, default=None, help="restrict discovery to one season")
    ap.add_argument(
        "--dry-run", action="store_true", help="report what would happen, change nothing"
    )
    ap.add_argument(
        "--allow-shrink",
        action="store_true",
        help="permit a re-parse that yields FEWER rows (default: refuse and roll back)",
    )
    ap.add_argument(
        "--refresh-venues",
        action="store_true",
        help="re-fetch raw.venues rows whose venue_name is blank (the SIM-447 typo) "
        "and exit; these rows cannot be deleted and reloaded because raw.pitches "
        "has an FK to them",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s", datefmt="%H:%M:%S"
    )
    dsn = _host_dsn(_load_env())

    if args.refresh_venues:
        return _refresh_venues(dsn, dry_run=args.dry_run)

    if args.game_pk:
        rows = _fetch(
            dsn,
            "SELECT season, game_pk FROM raw.games WHERE game_pk IN ("
            + ",".join(str(int(p)) for p in args.game_pk)
            + ")",
        )
        found = {pk: s for s, pk in rows}
        missing = [p for p in args.game_pk if p not in found]
        if missing:
            print(f"unknown game_pk(s), not in raw.games: {missing}")
            return 1
        targets = [(found[p], p, "explicitly requested") for p in args.game_pk]
        not_played: list[tuple] = []
    else:
        targets, not_played = _discover(dsn, args.season)

    if not_played:
        print(f"\nNOT reloading {len(not_played)} game(s) — never played, zero pitches is correct:")
        for s, pk, outcome, status in not_played:
            print(f"   {s}  game {pk}  outcome={outcome}  status={status}")

    if not targets:
        print("\nNothing to reload — no stale, failed or never-loaded games found.")
        _reconcile(dsn)
        return 0

    print(f"\n{len(targets)} game(s) to reload:")
    for s, pk, why in targets:
        print(f"   {s}  game {pk}  <- {why}")

    if args.dry_run:
        print("\n--dry-run: nothing was changed.")
        _reconcile(dsn)
        return 0

    os.environ["BASEBALL_DB_DSN"] = dsn
    # Imported here, after the DSN is in the environment: the loader resolves it
    # at construction, and this keeps --dry-run from touching the loader at all.
    from pipeline.etl.etl_historical_loader import HistoricalDataLoader  # noqa: PLC0415

    loader = HistoricalDataLoader()
    ok, failed = 0, []
    try:
        for season, pk, _why in targets:
            before = _row_count(dsn, pk)
            try:
                loader.reload_game(pk, season, allow_shrink=args.allow_shrink)
            except Exception as exc:  # noqa: BLE001 — report per game, keep going
                failed.append((pk, f"{type(exc).__name__}: {exc}"))
                print(f"   FAILED  game {pk}: {type(exc).__name__}: {exc}")
                continue
            after = _row_count(dsn, pk)
            ok += 1
            delta = after - before
            print(f"   ok      game {pk}: {before} -> {after} rows ({delta:+d})")
    finally:
        loader.close()

    print(f"\nreloaded {ok}/{len(targets)}")
    if failed:
        print("still failing — these need individual investigation:")
        for pk, err in failed:
            print(f"   game {pk}: {err}")

    _reconcile(dsn)
    return 1 if failed else 0


def _refresh_venues(dsn: str, *, dry_run: bool) -> int:
    """Re-fetch every raw.venues row whose venue_name is blank (SIM-447).

    The name was written by ``dimensions.get("venu_name_short", " ")`` — a typo, so
    the key never matched and every row stored a single space. ``venue_name`` is
    served to the front end (``api/routes/games.py``), so this is user-visible, not
    cosmetic. The row cannot be deleted and reloaded (raw.pitches holds an FK), so
    it is re-fetched in place via ``_ensure_venue(..., force=True)``.
    """
    blank = _fetch(
        dsn,
        """
        SELECT venue_id, season FROM raw.venues
        WHERE trim(coalesce(venue_name, '')) = ''
        ORDER BY venue_id, season
        """,
    )
    if not blank:
        print("No blank venue names — nothing to refresh.")
        return 0

    seasons = sorted({s for _, s in blank})
    print(f"{len(blank)} raw.venues row(s) with a blank name, seasons {seasons[0]}-{seasons[-1]}")
    if dry_run:
        print("--dry-run: nothing was changed.")
        return 0

    os.environ["BASEBALL_DB_DSN"] = dsn
    from pipeline.etl.etl_historical_loader import HistoricalDataLoader  # noqa: PLC0415

    loader = HistoricalDataLoader()
    ok, failed = 0, []
    try:
        for venue_id, season in blank:
            try:
                loader._ensure_venue(venue_id, season, force=True)
                ok += 1
            except Exception as exc:  # noqa: BLE001 — report per venue, keep going
                failed.append((venue_id, season, f"{type(exc).__name__}: {exc}"))
    finally:
        loader.close()

    still = _fetch(
        dsn,
        "SELECT count(*) FROM raw.venues WHERE trim(coalesce(venue_name, '')) = ''",
    )[0][0]
    print(f"refreshed {ok}/{len(blank)}; rows still blank: {still}")
    for venue_id, season, err in failed[:10]:
        print(f"   venue {venue_id} season {season}: {err}")
    sample = _fetch(
        dsn,
        "SELECT venue_id, season, venue_name FROM raw.venues "
        "WHERE trim(coalesce(venue_name,'')) <> '' ORDER BY venue_id LIMIT 5",
    )
    if sample:
        print("   sample of repaired rows:")
        for venue_id, season, name in sample:
            print(f"      {venue_id}  {season}  {name}")
    return 1 if still else 0


def _reconcile(dsn: str) -> None:
    """Ledger vs raw.pitches, per season. A mismatch is the tell for class A."""
    print("\n=== reconciliation: ledger 'loaded' vs distinct games in raw.pitches ===")
    print(f"   {'season':<8}{'ledger':>9}{'in pitches':>13}{'rows':>12}   status")
    clean = True
    for season, ledger, in_pitches, rows in _fetch(dsn, _Q_RECONCILE):
        # A game may legitimately be in the ledger but not in pitches (a cancelled
        # game loads zero rows). The reverse -- in pitches but not the ledger --
        # is the unswept/stale case and is never expected.
        flag = "OK" if in_pitches <= ledger else f"** {in_pitches - ledger} unswept game(s)"
        if in_pitches > ledger:
            clean = False
        print(f"   {season:<8}{ledger:>9,}{in_pitches:>13,}{rows:>12,}   {flag}")
    print(
        "\n   all seasons reconcile."
        if clean
        else "\n   ** rerun this script (no args) to clear the flagged seasons."
    )


if __name__ == "__main__":
    raise SystemExit(main())
