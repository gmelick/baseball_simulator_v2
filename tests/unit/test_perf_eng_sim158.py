"""tests/unit/test_perf_eng_sim158.py — SIM-158
=================================================
Pure-string unit tests for the SIM-158 index-acceptance harness.  These tests
do NOT require a live PostgreSQL — they exercise the harness's plan-parsing
helpers against fixture EXPLAIN ANALYZE output captured from real plans.

Live-DB validation lives in tests/integration/ once a populated staging
database is wired into CI.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_harness():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "run_index_acceptance",
        REPO_ROOT / "scripts" / "run_index_acceptance.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Fixture EXPLAIN ANALYZE output
# ---------------------------------------------------------------------------

# Passing SIM-085 plan: Index Scan using idx_pitches_situation, 12 ms total
SITUATION_PASS_PLAN = """\
Index Scan using idx_pitches_situation on raw.pitches  (cost=0.42..14.99 rows=120 width=64) (actual time=0.041..11.220 rows=143 loops=1)
  Index Cond: ((inning = 7) AND (outs = 1))
  Filter: (data_quality_flag = false)
  Buffers: shared hit=24 read=8
Planning Time: 0.182 ms
Execution Time: 12.41 ms
"""

# Failing SIM-085 plan: Seq Scan, 312 ms total
SITUATION_FAIL_PLAN = """\
Seq Scan on raw.pitches  (cost=0.00..49500.00 rows=120 width=64) (actual time=0.022..308.770 rows=143 loops=1)
  Filter: ((inning = 7) AND (outs = 1) AND (NOT data_quality_flag))
  Rows Removed by Filter: 718320
  Buffers: shared hit=2 read=49432
Planning Time: 0.067 ms
Execution Time: 312.04 ms
"""

# Passing SIM-089 plan: Index Scan using idx_pitches_pitcher_season_clean, 28 ms total
PITCHER_PASS_PLAN = """\
Index Scan using idx_pitches_pitcher_season_clean on raw.pitches  (cost=0.42..96.78 rows=2980 width=72) (actual time=0.018..27.115 rows=3072 loops=1)
  Index Cond: ((pitcher = 605400) AND (season = 2024))
  Buffers: shared hit=42 read=11
Planning Time: 0.119 ms
Execution Time: 27.83 ms
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPlanParsing(unittest.TestCase):

    def test_extract_total_ms_pass(self):
        h = _load_harness()
        self.assertAlmostEqual(h._extract_total_ms(SITUATION_PASS_PLAN), 12.41)
        self.assertAlmostEqual(h._extract_total_ms(SITUATION_FAIL_PLAN), 312.04)

    def test_extract_total_ms_missing_raises(self):
        h = _load_harness()
        with self.assertRaises(RuntimeError):
            h._extract_total_ms("planner produced nothing useful")

    def test_situation_pass_plan_uses_expected_index(self):
        h = _load_harness()
        self.assertTrue(h._plan_uses_index(SITUATION_PASS_PLAN, h.SITUATION_INDEX_NAME))
        self.assertFalse(h._plan_is_seq_scan(SITUATION_PASS_PLAN))

    def test_situation_fail_plan_is_detected(self):
        h = _load_harness()
        self.assertFalse(h._plan_uses_index(SITUATION_FAIL_PLAN, h.SITUATION_INDEX_NAME))
        self.assertTrue(h._plan_is_seq_scan(SITUATION_FAIL_PLAN))

    def test_pitcher_pass_plan(self):
        h = _load_harness()
        self.assertTrue(h._plan_uses_index(PITCHER_PASS_PLAN, h.PITCHER_INDEX_NAME))
        self.assertFalse(h._plan_is_seq_scan(PITCHER_PASS_PLAN))
        self.assertLess(h._extract_total_ms(PITCHER_PASS_PLAN),
                        h.PITCHER_LATENCY_MS_BUDGET)


class TestAcceptanceBudgets(unittest.TestCase):

    def test_locked_in_thresholds(self):
        """Budgets must match the SIM-158 acceptance criteria text."""
        h = _load_harness()
        self.assertEqual(h.SITUATION_LATENCY_MS_BUDGET, 30.0)
        self.assertEqual(h.PITCHER_LATENCY_MS_BUDGET, 50.0)
        self.assertEqual(h.SITUATION_INDEX_NAME, "idx_pitches_situation")
        self.assertEqual(h.PITCHER_INDEX_NAME, "idx_pitches_pitcher_season_clean")


if __name__ == "__main__":
    unittest.main()
