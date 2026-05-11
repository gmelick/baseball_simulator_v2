"""scripts/run_index_acceptance.py — SIM-158
==============================================
Runs the EXPLAIN (ANALYZE, BUFFERS) acceptance gates for the SIM-085 and
SIM-089 indexes against a populated staging database, captures the plan
text and per-query latency, and emits a Markdown report fragment that the
Performance Engineer can paste into ``docs/perf/2026-05-13-index-acceptance.md``.

Acceptance criteria (from SIM-158):

  * SIM-085: ``EXPLAIN (ANALYZE, BUFFERS)`` on a representative situation
    lookup (count + outs + baserunner state) reports
    ``Index Scan using idx_pitches_situation`` (not ``Seq Scan``).
    Single-query latency < 30 ms on a populated season.

  * SIM-089: ``EXPLAIN (ANALYZE, BUFFERS)`` on the player-profile computor's
    primary fetch reports ``Index Scan using idx_pitches_pitcher_season_clean``.
    3,000-pitch fetch < 50 ms.

If either gate fails, exit 1 and print the offending plan + measured latency
so the operator can file a follow-up ticket and revert the index claim from
``CHANGES.md`` per AC #4.

Usage::

    BASEBALL_DB_DSN=postgresql://... python scripts/run_index_acceptance.py \\
        --season 2024 --pitcher-id 605400 \\
        --out docs/perf/2026-05-13-index-acceptance.md
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import re
import sys
from pathlib import Path

import asyncpg

# ---------------------------------------------------------------------------
# Acceptance thresholds (locked in by SIM-085 / SIM-089 acceptance criteria)
# ---------------------------------------------------------------------------

SITUATION_LATENCY_MS_BUDGET = 30.0  # SIM-085
PITCHER_LATENCY_MS_BUDGET = 50.0  # SIM-089
SITUATION_INDEX_NAME = "idx_pitches_situation"
PITCHER_INDEX_NAME = "idx_pitches_pitcher_season_clean"


# Representative situation: 7th inning, 1 out, runner on 2B only.
DEFAULT_SITUATION = {
    "inning": 7,
    "outs": 1,
    "balls": 1,
    "strikes": 2,
    "on_1b": None,
    "on_2b": 12345,
    "on_3b": None,
}


# ---------------------------------------------------------------------------
# Query templates
# ---------------------------------------------------------------------------

SITUATION_QUERY = """
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT pitch_id, game_pk, at_bat_number, pitch_number
  FROM raw.pitches
 WHERE inning  = $1
   AND outs    = $2
   AND balls   = $3
   AND strikes = $4
   AND (on_1b IS NULL  OR on_1b = $5)
   AND (on_2b IS NULL  OR on_2b = $6)
   AND (on_3b IS NULL  OR on_3b = $7)
   AND data_quality_flag = FALSE
"""

PITCHER_SEASON_QUERY = """
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT pitch_id, velo, ivb, hb, spin_rate, plate_x, plate_z
  FROM raw.pitches
 WHERE pitcher = $1
   AND season  = $2
   AND data_quality_flag = FALSE
"""


# ---------------------------------------------------------------------------
# Plan parsing helpers
# ---------------------------------------------------------------------------

_TOTAL_TIME_RE = re.compile(r"Execution Time:\s+([\d.]+)\s+ms", re.MULTILINE)
_PLAN_TIME_RE = re.compile(r"Planning Time:\s+([\d.]+)\s+ms", re.MULTILINE)


def _extract_total_ms(plan_text: str) -> float:
    m = _TOTAL_TIME_RE.search(plan_text)
    if not m:
        raise RuntimeError(f"could not parse Execution Time from plan:\n{plan_text}")
    return float(m.group(1))


def _plan_uses_index(plan_text: str, index_name: str) -> bool:
    return (
        f"Index Scan using {index_name}" in plan_text
        or f"Index Only Scan using {index_name}" in plan_text
        or f"Bitmap Index Scan on {index_name}" in plan_text
    )


def _plan_is_seq_scan(plan_text: str) -> bool:
    # Match `Seq Scan on raw.pitches` or `Seq Scan on pitches` either as the
    # top node or under any parallel-aware wrapper.
    return bool(re.search(r"Seq Scan on (?:raw\.)?pitches", plan_text))


# ---------------------------------------------------------------------------
# Gate runner
# ---------------------------------------------------------------------------


async def _run_gate(
    conn: asyncpg.Connection,
    label: str,
    sql: str,
    params: tuple,
    expected_index: str,
    latency_budget_ms: float,
) -> tuple[bool, str, float]:
    """Run an EXPLAIN ANALYZE round-trip and return (passed, plan_text, ms)."""
    rows = await conn.fetch(sql, *params)
    plan_text = "\n".join(r[0] for r in rows)
    measured_ms = _extract_total_ms(plan_text)

    used_index = _plan_uses_index(plan_text, expected_index)
    used_seq = _plan_is_seq_scan(plan_text)
    latency_ok = measured_ms < latency_budget_ms

    passed = used_index and not used_seq and latency_ok

    print(f"\n=== {label} ===")
    print(f"  expected index : {expected_index}")
    print(f"  used index?    : {used_index}")
    print(f"  Seq Scan?      : {used_seq}")
    print(f"  measured       : {measured_ms:.2f} ms")
    print(f"  budget         : {latency_budget_ms:.2f} ms")
    print(f"  PASS           : {passed}")

    return passed, plan_text, measured_ms


# ---------------------------------------------------------------------------
# Markdown emission
# ---------------------------------------------------------------------------


def _make_markdown(
    *,
    season: int,
    pitcher_id: int,
    situation: dict,
    sim085_pass: bool,
    sim085_plan: str,
    sim085_ms: float,
    sim089_pass: bool,
    sim089_plan: str,
    sim089_ms: float,
) -> str:
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    overall = (
        "✅ Both gates passed"
        if (sim085_pass and sim089_pass)
        else "❌ At least one gate failed — see below"
    )
    return f"""# SIM-158 — Index Acceptance Gates (sprint 2026-05-13)

*Generated: {now} (UTC)*

This report captures the EXPLAIN ANALYZE acceptance gates for SIM-085
(`idx_pitches_situation`) and SIM-089 (`idx_pitches_pitcher_season_clean`),
run against the staging database after 2024 data was loaded.

**Outcome:** {overall}

---

## SIM-085 — `idx_pitches_situation` (composite situation index)

* **Query shape:** `inning + outs + balls + strikes + on_1b + on_2b + on_3b`,
  filtered by `data_quality_flag = FALSE`.
* **Representative situation:** {situation}
* **Acceptance gate:** Index Scan using `{SITUATION_INDEX_NAME}` AND latency < {SITUATION_LATENCY_MS_BUDGET:.0f} ms.
* **Measured latency:** {sim085_ms:.2f} ms
* **Result:** {"✅ PASS" if sim085_pass else "❌ FAIL — file a follow-up ticket and revert the SIM-085 index claim from CHANGES.md per AC #4."}

```text
{sim085_plan}
```

---

## SIM-089 — `idx_pitches_pitcher_season_clean`

* **Query shape:** `pitcher = ? AND season = ? AND data_quality_flag = FALSE`
  (the player-profile computor's primary fetch).
* **Representative pitcher / season:** pitcher_id={pitcher_id}, season={season}
* **Acceptance gate:** Index Scan using `{PITCHER_INDEX_NAME}` AND latency < {PITCHER_LATENCY_MS_BUDGET:.0f} ms on a 3,000-pitch fetch.
* **Measured latency:** {sim089_ms:.2f} ms
* **Result:** {"✅ PASS" if sim089_pass else "❌ FAIL — file a follow-up ticket and revert the SIM-089 index claim from CHANGES.md per AC #4."}

```text
{sim089_plan}
```

---

## Reproducing this report

```sh
BASEBALL_DB_DSN=postgresql://... python scripts/run_index_acceptance.py \\
    --season {season} --pitcher-id {pitcher_id} \\
    --out docs/perf/2026-05-13-index-acceptance.md
```

The script exits non-zero when either gate fails so it can be wired into CI.
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main_async(
    dsn: str,
    season: int,
    pitcher_id: int,
    situation: dict,
    out_path: Path | None,
) -> int:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        sim085_pass, sim085_plan, sim085_ms = await _run_gate(
            conn,
            label="SIM-085: idx_pitches_situation",
            sql=SITUATION_QUERY,
            params=(
                situation["inning"],
                situation["outs"],
                situation["balls"],
                situation["strikes"],
                situation["on_1b"],
                situation["on_2b"],
                situation["on_3b"],
            ),
            expected_index=SITUATION_INDEX_NAME,
            latency_budget_ms=SITUATION_LATENCY_MS_BUDGET,
        )

        sim089_pass, sim089_plan, sim089_ms = await _run_gate(
            conn,
            label="SIM-089: idx_pitches_pitcher_season_clean",
            sql=PITCHER_SEASON_QUERY,
            params=(pitcher_id, season),
            expected_index=PITCHER_INDEX_NAME,
            latency_budget_ms=PITCHER_LATENCY_MS_BUDGET,
        )

        report = _make_markdown(
            season=season,
            pitcher_id=pitcher_id,
            situation=situation,
            sim085_pass=sim085_pass,
            sim085_plan=sim085_plan,
            sim085_ms=sim085_ms,
            sim089_pass=sim089_pass,
            sim089_plan=sim089_plan,
            sim089_ms=sim089_ms,
        )
        if out_path is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(report)
            print(f"\nWrote acceptance report → {out_path}")
        else:
            print(report)

        return 0 if (sim085_pass and sim089_pass) else 1
    finally:
        await conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dsn", default=os.environ.get("BASEBALL_DB_DSN"))
    p.add_argument("--season", type=int, default=2024)
    p.add_argument(
        "--pitcher-id",
        type=int,
        required=False,
        default=605400,
        help="MLBAM player_id of a pitcher with ~3,000 clean pitches "
        "in the given season (per SIM-089 AC).",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional Markdown report path (e.g. docs/perf/2026-05-13-index-acceptance.md).",
    )
    args = p.parse_args()
    if not args.dsn:
        p.error("DSN required: pass --dsn or set BASEBALL_DB_DSN")

    rc = asyncio.run(
        main_async(
            dsn=args.dsn,
            season=args.season,
            pitcher_id=args.pitcher_id,
            situation=DEFAULT_SITUATION,
            out_path=args.out,
        )
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
