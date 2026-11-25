# The profile build and the bundle build: the byte check (SIM-647 part 3, Step A), 2026-11-25

## What ran

The check copies the production DuckDB file twice (`old.duckdb`, `new.duckdb`).

- The old code (master e21efb4) runs on `old.duckdb`:
  `python -m pipeline.batch.player_profile_computor --seasons 2026 --full-rebuild --asof 2026-11-24`,
  then `python -m pipeline.batch.engine_artifacts --what all`.
- The new code (branch `sim647-part3-profiles`, 55a1a7e) runs on `new.duckdb`:
  `python -m profiles.build_profiles` with the same arguments,
  then `python -m bundle.build_bundle --what all`.
- The comparer reads every DuckDB table with `EXCEPT ALL` both ways, timestamp columns
  left out. It compares every bundle file: the `.npz` arrays by bytes, the JSON files
  with their time keys left out, and every other file by bytes.

The old build ran from 22:20 to 23:59 UTC on 2026-11-24. The new build ran from 00:02 to
01:44 UTC on 2026-11-25.

## The result

The first compare reads 61 differences. A diagnosis of each one finds no change of
behaviour.

| What | First compare | Diagnosis |
|---|---|---|
| 20 DuckDB tables | the same | — |
| `derived.pitcher_season_metrics` | 635 rows differ | Only `gmm_model.fit_date` differs: "2026-11-24" against "2026-11-25". With the date removed, all 635 rows are equal. |
| `derived.catcher_season_metrics` | 1 row differs | `blocks_aa_bounced` differs by 1.5e-11 (relative 1.1e-7): float rounding. |
| The pitch, batted-ball and advancement pools, the manager roles (34 files) | bytes differ | The same rows in another order. |
| Five embedding files | the key order differs | The same keys. The vectors are equal once aligned by key. The mean and the standard deviation differ by at most 3.1e-5 (float32 sums in another order). |
| `pitcher_sim.npz` | the index and the neighbour lists differ | The matrix is equal once aligned by the index. Every query has the same neighbour set; the worst score difference is 7.8e-16. |
| `park_geometry.json` | the six carry coefficients differ | The fit reads its rows in another order; the coefficients differ at the 13th digit. |

## Why the order differs

The profile build rewrites the 2026 season only. A plain scan of each file shows the
cause:

- `sim.pitch_pool`, `sim.outcome_pool` and `sim.advancement_opportunity_pool` hold their
  2023, 2024 and 2025 rows in the same stored order in both files. Their 2026 rows are in
  a different stored order.
- `derived.batter_season_metrics` and `derived.pitcher_season_metrics` show the same
  pattern: 2025 in the same order, 2026 not.

The builds insert the same rows, and DuckDB stores them in a different order. DuckDB
keeps no row order for a parallel insert without ORDER BY. The bundle exports read the
pools and the profiles with no ORDER BY, so each export follows the stored order. Step A
leaves every one of these queries unchanged (the AST proof of 477 definitions). I did not
run master's build twice to show the same order change between two master runs.

## The harness

`results/sim647_bytecheck/` (not committed): `run.sh`, `compare.py`, and the four
diagnosis passes `diagnose.py` to `diagnose4.py`. The DuckDB copies and
the two bundles are at `/data/sim647_bytecheck/` in the app's data volume.

## The second check: the function review (2026-11-25)

The function review (branch commit 4ca3a99) merges helpers and deletes dead code in the same
builds, so it gets its own check. The check copies `old.duckdb` (the file master's build left)
to `new2.duckdb`. The function-review code, exported with `git archive` to a frozen folder,
reruns the same profile build and bundle build on that copy. `compare2.py` then compares
`old.duckdb` and `bundle_old` with `new2.duckdb` and `bundle_new2` by content:

- the tables by `EXCEPT ALL` both ways; a table that differs is compared row by row on its
  key columns, floats equal within 1e-9 relative or absolute, the arsenal fit's date aside;
- the pool files as joined rows (the metadata columns plus every array column, the pitch
  pool's batted-ball row number mapped to its pitch id), sorted;
- the embeddings aligned by key, their mean and standard deviation within 1e-4 relative;
- the pitcher score matrix aligned by its index; the JSON files with floats within 1e-9.

The comparer first read the first check's two builds as the same by content.

| What | Result |
|---|---|
| 20 DuckDB tables | equal row for row |
| `derived.catcher_season_metrics`, `derived.pitcher_season_metrics` | equal by content (the float rounding and the fit date of the first check) |
| The 74 bundle files | equal by content |
| **Result** | **SAME BY CONTENT** |

The harness adds `run2.sh`, `compare2.py` and the frozen code folder `code_4ca3a99/`.
