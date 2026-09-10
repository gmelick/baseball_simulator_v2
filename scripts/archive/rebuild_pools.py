"""Rebuild sim.pitch_pool + sim.outcome_pool for the given seasons only.

Skips the expensive engine-profile recompute (profiles already built); used to
re-materialize the pools after the SIM-421 outcome_type fix.

Usage: python scripts/rebuild_pools.py [season ...]   (default: 2026)
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT))
from pipeline.batch.player_profile_computor import PlayerProfileComputor

seasons = [int(a) for a in sys.argv[1:]] or [2026]
c = PlayerProfileComputor(
    pg_dsn=os.environ["BASEBALL_DB_DSN"],
    duckdb_path=os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb"),
)
c._connect()
try:
    c._build_pitch_pool(seasons, incremental=False)
    c._build_outcome_pool(seasons, incremental=False)
finally:
    c._close()
print(f"pools rebuilt for seasons={seasons}")
