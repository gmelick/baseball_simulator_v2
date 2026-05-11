# SIM-158 — Index Acceptance Gates (sprint 2026-05-13)

*Owner: Performance Engineer (Agent 6) · Reports back to: ML Engineer + Data Engineer*

This file is the canonical record for the SIM-085 and SIM-089 EXPLAIN ANALYZE
acceptance gates.  The two indexes were merged in sprint 2026-05-06+07 with
their gates *expressed* but not *executed* because the sandbox lacked a
populated database; SIM-158 closes the loop now that 2024 data is loaded in
staging.

## Status

**Pending execution against staging (2024 season).**

The harness script `scripts/run_index_acceptance.py` ships with this ticket
and replaces this file in-place when the operator runs it against staging.
Until that run completes, the SIM-085 / SIM-089 entries in `CHANGES.md`
remain provisional.

## Gates

| Index | Acceptance criterion | Latency budget |
|-------|----------------------|----------------|
| `idx_pitches_situation` (SIM-085) | `EXPLAIN (ANALYZE, BUFFERS)` on a representative situation lookup reports `Index Scan using idx_pitches_situation` (not `Seq Scan on pitches`). | < 30 ms |
| `idx_pitches_pitcher_season_clean` (SIM-089) | `EXPLAIN (ANALYZE, BUFFERS)` on `_compute_pitcher_profiles()`'s primary fetch reports `Index Scan using idx_pitches_pitcher_season_clean`.  3,000-pitch fetch budget. | < 50 ms |

## How to run

```sh
# From staging with 2024 data loaded:
BASEBALL_DB_DSN=postgresql://staging-host:5432/baseball \
    python scripts/run_index_acceptance.py \
        --season 2024 \
        --pitcher-id 605400 \
        --out docs/perf/2026-05-13-index-acceptance.md
```

Pick a `--pitcher-id` whose 2024 clean-pitch count is close to 3,000 (per the
SIM-089 acceptance criterion).  605400 (Justin Steele) is a workable default;
swap for any starter who threw a full season if his profile has changed.

The script exits non-zero when either gate fails so it can be wired into CI
or run as a pre-merge guard.

## Failure handling (AC #4)

If either index loses to a `Seq Scan` or exceeds its latency budget:

1. The harness script overwrites this file with the failing plan + measured
   latency captured verbatim (`docs/perf/2026-05-13-index-acceptance.md`).
2. File a follow-up ticket on the relevant index immediately
   (re-tune `WHERE`, change `INCLUDE` columns, etc.).
3. Revert the index claim from `CHANGES.md` for that ticket (SIM-085 or
   SIM-089) so downstream readers know the index is provisional again.

## Companion CHANGES.md links

* SIM-085 — composite situation index on `raw.pitches`
* SIM-089 — `(pitcher, season)` partial index on `raw.pitches`
