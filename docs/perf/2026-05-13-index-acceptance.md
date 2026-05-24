# SIM-158 -- Index Acceptance Gates (sprint 2026-05-13)

*Generated: 2026-05-17T16:29:02+00:00 (UTC)*

This report captures the EXPLAIN ANALYZE acceptance gates for SIM-085
(`idx_pitches_situation`) and SIM-089 (`idx_pitches_pitcher_season_clean`),
run against the staging database after 2024 data was loaded.

**Outcome:** Both gates passed

---

## SIM-085 -- `idx_pitches_situation` (composite situation index)

* **Representative situation:** {'inning': 7, 'outs': 1, 'balls': 1, 'strikes': 2, 'on_1b': None, 'on_2b': None, 'on_3b': None}
* **Acceptance gate:** Index Scan using `idx_pitches_situation` AND latency < 30 ms.
* **Measured latency:** 27.54 ms
* **Result:** PASS

```text
Bitmap Heap Scan on pitches  (cost=315.45..35581.65 rows=9979 width=12) (actual time=1.969..26.869 rows=12299 loops=1)
  Recheck Cond: ((inning = '7'::smallint) AND (outs = '1'::smallint) AND (balls = '1'::smallint) AND (strikes = '2'::smallint) AND (on_1b IS NULL) AND (on_2b IS NULL) AND (on_3b IS NULL) AND (NOT data_quality_flag))
  Heap Blocks: exact=9724
  Buffers: shared hit=9736
  ->  Bitmap Index Scan on idx_pitches_situation  (cost=0.00..312.96 rows=9979 width=0) (actual time=1.018..1.019 rows=12299 loops=1)
        Index Cond: ((inning = '7'::smallint) AND (outs = '1'::smallint) AND (balls = '1'::smallint) AND (strikes = '2'::smallint) AND (on_1b IS NULL) AND (on_2b IS NULL) AND (on_3b IS NULL))
        Buffers: shared hit=12
Planning:
  Buffers: shared hit=364
Planning Time: 2.027 ms
Execution Time: 27.538 ms
```

---

## SIM-089 -- `idx_pitches_pitcher_season_clean`

* **Representative pitcher / season:** pitcher_id=605400, season=2024
* **Acceptance gate:** Index Scan using `idx_pitches_pitcher_season_clean` AND latency < 50 ms on a 3,000-pitch fetch.
* **Measured latency:** 2.25 ms
* **Result:** PASS

```text
Index Scan using idx_pitches_pitcher_season_clean on pitches  (cost=0.43..9740.91 rows=2841 width=56) (actual time=0.024..2.106 rows=3274 loops=1)
  Index Cond: ((pitcher = 605400) AND (season = 2024))
  Buffers: shared hit=438
Planning:
  Buffers: shared hit=27
Planning Time: 0.275 ms
Execution Time: 2.255 ms
```

---

## Reproducing this report

```sh
BASEBALL_DB_DSN=postgresql://... python scripts/run_index_acceptance.py \
    --season 2024 --pitcher-id 605400 \
    --out docs/perf/2026-05-13-index-acceptance.md
```
