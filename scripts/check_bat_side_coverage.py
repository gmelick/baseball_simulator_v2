r"""scripts/check_bat_side_coverage.py - SIM-160
Audit raw.pitches.bat_hand coverage per season.

Usage (cmd.exe):
    set BASEBALL_DB_DSN=postgresql://user:pass@host:5432/db
    python scripts\check_bat_side_coverage.py ^
        --out docs\data_quality\2026-05-20-bat-side-coverage.md
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg


NULL_RATE_BUDGET_PCT = 1.0


def _mask_dsn_password(dsn: str) -> str:
    return re.sub(r"(://[^:/?#]+):[^@/?#]+@", r"\1:***@", dsn)


@dataclass(frozen=True)
class SeasonCoverage:
    season: int
    total_rows: int
    null_stand: int
    null_bat_hand: int
    switch_stand: int
    switch_bat_hand: int

    @property
    def null_pct(self) -> float:
        if self.total_rows == 0:
            return 0.0
        return 100.0 * max(self.null_stand, self.null_bat_hand) / self.total_rows

    @property
    def switch_pct(self) -> float:
        if self.total_rows == 0:
            return 0.0
        return 100.0 * self.switch_bat_hand / self.total_rows

    @property
    def gate_passes(self) -> bool:
        return self.null_pct <= NULL_RATE_BUDGET_PCT


async def _scan_coverage(conn):
    rows = await conn.fetch("""
        SELECT season,
               COUNT(*) AS total_rows,
               COUNT(*) FILTER (WHERE stand IS NULL) AS null_stand,
               COUNT(*) FILTER (WHERE bat_hand IS NULL) AS null_bat_hand,
               COUNT(*) FILTER (WHERE stand = 'S') AS switch_stand,
               COUNT(*) FILTER (WHERE bat_hand = 'S') AS switch_bat_hand
          FROM raw.pitches
         GROUP BY season
         ORDER BY season
    """)
    return [
        SeasonCoverage(
            season=int(r["season"]),
            total_rows=int(r["total_rows"]),
            null_stand=int(r["null_stand"]),
            null_bat_hand=int(r["null_bat_hand"]),
            switch_stand=int(r["switch_stand"]),
            switch_bat_hand=int(r["switch_bat_hand"]),
        )
        for r in rows
    ]


def _make_markdown(coverage):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    any_fail = any(not c.gate_passes for c in coverage)
    verdict = "All seasons clear the 1% NULL gate" if not any_fail else "At least one season fails the gate"
    rows = []
    for c in coverage:
        marker = "PASS" if c.gate_passes else "FAIL"
        rows.append(
            f"| {c.season} | {c.total_rows:,} | {c.null_stand} | "
            f"{c.null_bat_hand} | {c.null_pct:.3f}% | {c.switch_stand} | "
            f"{c.switch_bat_hand} | {c.switch_pct:.3f}% | {marker} |"
        )
    rows_md = "\n".join(rows)
    return (
        "# SIM-160 - raw.pitches.bat_hand Coverage Audit\n\n"
        f"*Generated: {now} (UTC)*\n\n"
        f"**Outcome:** {verdict}\n\n"
        "## Coverage by season\n\n"
        "| Season | Total rows | NULL stand | NULL bat_hand | NULL % | "
        "'S' stand | 'S' bat_hand | 'S' % | Gate |\n"
        "|--------|-----------:|-----------:|--------------:|-------:|"
        "----------:|-------------:|------:|:----:|\n"
        f"{rows_md}\n\n"
        f"**Gate:** fails if any season's NULL % > {NULL_RATE_BUDGET_PCT:.1f}%.\n"
    )


async def main_async(dsn, out_path):
    try:
        conn = await asyncpg.connect(dsn=dsn)
    except asyncpg.exceptions.InvalidPasswordError as e:
        masked = _mask_dsn_password(dsn)
        msg = (
            "\nERROR: password authentication failed against " + repr(masked) + ".\n\n"
            "The DSN reached the database, but the password is wrong.\n\n"
            "Reset the password in-band (cmd.exe):\n"
            '  docker compose exec db psql -U baseball_user -d baseball_sim -c "ALTER USER baseball_user WITH PASSWORD ' + chr(39) + 'baseball_pass' + chr(39) + ';"\n\n'
            "If THAT fails too, try with the OS-trusted postgres user:\n"
            '  docker compose exec -u postgres db psql -c "ALTER USER baseball_user WITH PASSWORD ' + chr(39) + 'baseball_pass' + chr(39) + ';"\n\n'
            "Then verify the host-side DSN matches:\n"
            "  echo %BASEBALL_DB_DSN%\n"
            "  (expected: postgresql://baseball_user:baseball_pass@localhost:5432/baseball_sim)\n\n"
            f"Original asyncpg error: {e}\n"
        )
        sys.stderr.write(msg)
        return 2
    except OSError as e:
        masked = _mask_dsn_password(dsn)
        sys.stderr.write(
            "\nERROR: cannot reach Postgres at " + repr(masked) + ".\n"
            "  - Is 'docker compose up db' running?\n"
            "  - Is the host/port in BASEBALL_DB_DSN correct?\n"
            f"  - Original error: {e}\n"
        )
        return 2

    try:
        coverage = await _scan_coverage(conn)
    finally:
        await conn.close()

    if not coverage:
        print("WARNING: raw.pitches is empty - nothing to audit.")
        return 0

    any_fail = False
    print(f"{'season':<8} {'rows':>12} {'null %':>10} {'switch %':>10}  gate")
    for c in coverage:
        marker = "PASS" if c.gate_passes else "FAIL"
        if not c.gate_passes:
            any_fail = True
        line = (
            f"{c.season:<8} {c.total_rows:>12,} "
            f"{c.null_pct:>9.3f}% {c.switch_pct:>9.3f}%  {marker}"
        )
        print(line)

    report = _make_markdown(coverage)
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"\nWrote coverage report -> {out_path}")
    else:
        print(report)
    return 1 if any_fail else 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dsn", default=os.environ.get("BASEBALL_DB_DSN"))
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()
    if not args.dsn:
        p.error(
            "DSN required: pass --dsn or set BASEBALL_DB_DSN.\n"
            "  Example (cmd.exe):\n"
            "    set BASEBALL_DB_DSN=postgresql://user:pass@localhost:5432/baseball_sim"
        )
    rc = asyncio.run(main_async(args.dsn, out_path=args.out))
    sys.exit(rc)


if __name__ == "__main__":
    main()
