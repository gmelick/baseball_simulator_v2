"""
tests/unit/test_sim_store.py
============================
SIM-356 -- unit tests for db/sim_store.py (durable sim-result + pitch-snapshot
persistence) and its two migrations (Alembic 0014 / DuckDB 0008).

Coverage
--------
  * DuckDB play-stream path against a REAL in-memory ``:memory:`` duckdb (the
    sandbox runs DuckDB in-process): apply 0008_*.sql, store_play_stream then
    load_play_stream, assert round-trip + ordering + that the round-tripped rows
    carry everything PlayByPlayEntry needs.
  * Postgres sim-run path against a stub asyncpg connection (mirrors
    test_api_state.py's stub style): assert store_sim_run issues the expected
    INSERT and returns the run_id; load_latest_sim_run / load_sim_run parse a
    row into the documented dict incl. JSONB summary round-trip.
  * Migration sanity: Alembic 0014 revision/down_revision/upgrade/downgrade;
    duckdb_schema_version.txt reads 14 and matches the latest migration.

Owned by Data Engineer.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from db import sim_store
from simulation.snapshots import PlayByPlay, PlayByPlayEntry

REPO_ROOT = Path(__file__).resolve().parents[2]
DUCKDB_MIGRATION = REPO_ROOT / "db" / "migrations" / "duckdb" / "0008_sim356_play_stream.sql"
ALEMBIC_0014 = REPO_ROOT / "db" / "migrations" / "versions" / "0014_sim356_sim_run_history.py"
DUCKDB_VERSION_FILE = REPO_ROOT / "db" / "schemas" / "duckdb_schema_version.txt"


# ---------------------------------------------------------------------------
# DuckDB fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def duck_con():
    """A real in-memory DuckDB with migration 0008 applied."""
    import duckdb

    con = duckdb.connect(":memory:")
    # The migration's final statement does INSERT ... INTO migration_history,
    # which 0001 creates. Pre-create it so the 0008 file applies stand-alone.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS migration_history (
            migration_id VARCHAR PRIMARY KEY,
            applied_at   TIMESTAMP NOT NULL DEFAULT now(),
            description  VARCHAR NOT NULL
        )
        """
    )
    con.execute(DUCKDB_MIGRATION.read_text())
    yield con
    con.close()


def _sample_play_entries() -> list[dict]:
    """A small ordered pitch stream built through the real PlayByPlay contract.

    Two PAs: a 3-pitch strikeout then a 1-pitch home run. We build PlayResults,
    run them through PlayByPlay.from_play_results, and asdict the entries -- so
    the test exercises exactly the dict shape SIM-357 will hand store_play_stream.
    """
    from simulation.game_state import PlayResult

    results = [
        PlayResult(pitch_outcome="called_strike"),
        PlayResult(pitch_outcome="swinging_strike"),
        PlayResult(
            pitch_outcome="swinging_strike",
            pa_terminal=True,
            event="strikeout",
            outs_recorded=1,
            runs=-0.25,
            canonical_event="strikeout",
        ),
        PlayResult(
            pitch_outcome="in_play",
            is_contact=True,
            pa_terminal=True,
            event="home_run",
            runs_scored=1,
            runs=1.4,
            exit_velo=104.2,
            launch_angle=28.0,
            spray_angle=-12.0,
            canonical_event="home_run",
        ),
    ]
    pbp = PlayByPlay.from_play_results(results)
    return [dataclasses.asdict(e) for e in pbp.entries]


# ---------------------------------------------------------------------------
# DuckDB play-stream round-trip
# ---------------------------------------------------------------------------


class TestPlayStreamRoundTrip:
    def test_store_then_load_roundtrip_and_ordering(self, duck_con):
        entries = _sample_play_entries()
        sim_store.store_play_stream(duck_con, game_pk=777001, run_id=42, play_entries=entries)
        loaded = sim_store.load_play_stream(duck_con, game_pk=777001, run_id=42)

        assert len(loaded) == len(entries) == 4
        # Ordering: load_play_stream orders by sequence ascending.
        assert [r["sequence"] for r in loaded] == [0, 1, 2, 3]
        # at_bat grouping survived: first PA = seq 0-2 (at_bat 0), then at_bat 1.
        assert [r["at_bat"] for r in loaded] == [0, 0, 0, 1]
        assert [r["pitch"] for r in loaded] == [1, 2, 3, 1]

    def test_loaded_rows_rebuild_playbyplayentry(self, duck_con):
        """A loaded row carries every PlayByPlayEntry constructor field, so it
        maps straight onto PlayByPlayEntry(**row) -- the SIM-357 read path."""
        entries = _sample_play_entries()
        sim_store.store_play_stream(duck_con, game_pk=1, run_id=1, play_entries=entries)
        loaded = sim_store.load_play_stream(duck_con, game_pk=1, run_id=1)

        rebuilt = [PlayByPlayEntry(**row) for row in loaded]
        original = [PlayByPlayEntry(**e) for e in entries]
        assert rebuilt == original

    def test_terminal_pitch_fields_persist(self, duck_con):
        """The PA event + batted-ball stats on the terminal pitch survive."""
        entries = _sample_play_entries()
        sim_store.store_play_stream(duck_con, game_pk=5, run_id=9, play_entries=entries)
        loaded = sim_store.load_play_stream(duck_con, game_pk=5, run_id=9)

        hr = loaded[-1]
        assert hr["is_pa_end"] is True
        assert hr["is_contact"] is True
        assert hr["event"] == "home_run"
        assert hr["canonical_event"] == "home_run"
        assert hr["runs_scored"] == 1
        assert hr["exit_velo"] == pytest.approx(104.2)
        assert hr["launch_angle"] == pytest.approx(28.0)
        assert hr["spray_angle"] == pytest.approx(-12.0)
        assert hr["runs"] == pytest.approx(1.4)

        # A non-contact, non-terminal pitch keeps its NULL batted-ball stats.
        first = loaded[0]
        assert first["is_pa_end"] is False
        assert first["event"] is None
        assert first["exit_velo"] is None
        assert first["launch_angle"] is None
        assert first["spray_angle"] is None

    def test_load_without_run_id_returns_all_for_game(self, duck_con):
        entries = _sample_play_entries()
        sim_store.store_play_stream(duck_con, game_pk=100, run_id=1, play_entries=entries)
        sim_store.store_play_stream(duck_con, game_pk=100, run_id=2, play_entries=entries)
        all_rows = sim_store.load_play_stream(duck_con, game_pk=100)
        assert len(all_rows) == 2 * len(entries)
        # Distinct runs stay grouped (ORDER BY run_id, sequence).
        only_run2 = sim_store.load_play_stream(duck_con, game_pk=100, run_id=2)
        assert len(only_run2) == len(entries)

    def test_store_empty_is_noop(self, duck_con):
        sim_store.store_play_stream(duck_con, game_pk=1, run_id=1, play_entries=[])
        assert sim_store.load_play_stream(duck_con, game_pk=1, run_id=1) == []

    def test_missing_required_field_raises(self, duck_con):
        with pytest.raises(KeyError):
            sim_store.store_play_stream(
                duck_con,
                game_pk=1,
                run_id=1,
                play_entries=[{"at_bat": 0, "pitch": 1, "pitch_outcome": "ball"}],
            )

    def test_optional_defaults_filled_on_sparse_row(self, duck_con):
        """A sparse row (only the 4 required fields) loads back with defaults."""
        sim_store.store_play_stream(
            duck_con,
            game_pk=2,
            run_id=3,
            play_entries=[{"sequence": 0, "at_bat": 0, "pitch": 1, "pitch_outcome": "ball"}],
        )
        row = sim_store.load_play_stream(duck_con, game_pk=2, run_id=3)[0]
        assert row["is_contact"] is False
        assert row["is_pa_end"] is False
        assert row["event"] is None
        assert row["runs_scored"] == 0
        assert row["outs_recorded"] == 0
        assert row["runs"] == 0.0
        assert row["canonical_event"] is None


# ---------------------------------------------------------------------------
# Postgres sim-run path (stub asyncpg conn)
# ---------------------------------------------------------------------------


class _StubConn:
    """asyncpg connection stand-in. Records SQL/args, serves canned results."""

    def __init__(self, *, fetchval=None, fetchrow=None, fetch=None):
        self._fetchval = fetchval
        self._fetchrow = fetchrow
        self._fetch = fetch
        self.calls: list[tuple[str, tuple]] = []

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return self._fetchval

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self._fetchrow

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self._fetch or []


_SUMMARY = {
    "n_iterations": 1000,
    "home_win_pct": 0.55,
    "away_win_pct": 0.43,
    "tie_pct": 0.02,
    "home_scores": [3, 4, 5],
    "home_win_ci": {"point": 0.55, "low": 0.52, "high": 0.58},
    "simulated_at": "2026-05-24T00:00:00+00:00",
}


@pytest.mark.asyncio
class TestSimRunPostgres:
    async def test_store_sim_run_issues_insert_and_returns_run_id(self):
        conn = _StubConn(fetchval=4242)
        run_id = await sim_store.store_sim_run(
            conn,
            game_pk=777001,
            summary=_SUMMARY,
            n_iterations=1000,
            base_seed=12345,
        )
        assert run_id == 4242
        sql, args = conn.calls[-1]
        assert "INSERT INTO sim.sim_runs" in sql
        assert "RETURNING run_id" in sql
        # Positional args: game_pk, n_iterations, base_seed, summary(json str).
        assert args[0] == 777001
        assert args[1] == 1000
        assert args[2] == 12345
        # summary is JSON-encoded so asyncpg can cast it to jsonb.
        assert json.loads(args[3]) == _SUMMARY

    async def test_store_sim_run_null_base_seed(self):
        conn = _StubConn(fetchval=1)
        await sim_store.store_sim_run(
            conn, game_pk=1, summary=_SUMMARY, n_iterations=10, base_seed=None
        )
        _, args = conn.calls[-1]
        assert args[2] is None

    async def test_load_latest_sim_run_parses_row(self):
        row = {
            "run_id": 4242,
            "game_pk": 777001,
            "n_iterations": 1000,
            "base_seed": 12345,
            "summary": _SUMMARY,  # asyncpg with a jsonb codec -> already a dict
            "created_at": "2026-05-24T00:00:00+00:00",
        }
        conn = _StubConn(fetchrow=row)
        result = await sim_store.load_latest_sim_run(conn, 777001)
        assert result is not None
        assert result["run_id"] == 4242
        assert result["game_pk"] == 777001
        assert result["n_iterations"] == 1000
        assert result["base_seed"] == 12345
        assert result["summary"] == _SUMMARY
        sql, args = conn.calls[-1]
        assert "ORDER BY created_at DESC" in sql
        assert args == (777001,)

    async def test_load_latest_sim_run_decodes_jsonb_string(self):
        """asyncpg returns jsonb as a str unless a codec is registered; the
        loader must decode it so callers always get a dict."""
        row = {
            "run_id": 1,
            "game_pk": 2,
            "n_iterations": 5,
            "base_seed": None,
            "summary": json.dumps(_SUMMARY),  # raw jsonb str
            "created_at": "2026-05-24T00:00:00+00:00",
        }
        conn = _StubConn(fetchrow=row)
        result = await sim_store.load_latest_sim_run(conn, 2)
        assert result["summary"] == _SUMMARY  # decoded back to a dict
        assert result["base_seed"] is None

    async def test_load_latest_sim_run_missing_returns_none(self):
        conn = _StubConn(fetchrow=None)
        assert await sim_store.load_latest_sim_run(conn, 999) is None

    async def test_load_sim_run_by_id(self):
        row = {
            "run_id": 7,
            "game_pk": 3,
            "n_iterations": 50,
            "base_seed": 99,
            "summary": _SUMMARY,
            "created_at": "2026-05-24T00:00:00+00:00",
        }
        conn = _StubConn(fetchrow=row)
        result = await sim_store.load_sim_run(conn, 7)
        assert result["run_id"] == 7
        sql, args = conn.calls[-1]
        assert "WHERE run_id = $1" in sql
        assert args == (7,)

    async def test_load_sim_run_missing_returns_none(self):
        conn = _StubConn(fetchrow=None)
        assert await sim_store.load_sim_run(conn, 123) is None

    async def test_list_sim_runs(self):
        rows = [
            {
                "run_id": i,
                "game_pk": 5,
                "n_iterations": 10,
                "base_seed": None,
                "summary": _SUMMARY,
                "created_at": "2026-05-24T00:00:00+00:00",
            }
            for i in (3, 2, 1)
        ]
        conn = _StubConn(fetch=rows)
        result = await sim_store.list_sim_runs(conn, 5, limit=3)
        assert [r["run_id"] for r in result] == [3, 2, 1]
        sql, args = conn.calls[-1]
        assert "ORDER BY created_at DESC" in sql
        assert args == (5, 3)


# ---------------------------------------------------------------------------
# Migration sanity
# ---------------------------------------------------------------------------


class TestMigrationSanity:
    def test_alembic_0014_chains_off_0013(self):
        text = ALEMBIC_0014.read_text()
        assert 'revision = "0014"' in text
        assert 'down_revision = "0013"' in text
        assert "def upgrade()" in text
        assert "def downgrade()" in text
        assert "sim.sim_runs" in text

    def test_duckdb_schema_version_is_17(self):
        # SIM-357 bumped 8 -> 9 (0009); SIM-362/364 -> 10 (0010);
        # SIM-408 -> 11 (0011 engine ↔ schema reconciliation);
        # SIM-411/413/425b -> 12 (0012 batted-ball realism columns);
        # SIM-427 -> 13 (0013 manager available_reliever_usage_rate);
        # SIM-440 -> 14 (0014 corrected bat_hand/stand column comments);
        # SIM-468 -> 15 (0015 sim.steal_opportunity_pool);
        # SIM-504 -> 16 (0016 pitcher disengagement rates);
        # SIM-507 -> 17 (0017 pickoff outcome columns on the steal pool).
        assert DUCKDB_VERSION_FILE.read_text().strip() == "17"

    def test_duckdb_version_matches_latest_migration(self):
        """The version file must equal the highest-numbered DuckDB migration.

        CLAUDE.md §7 records this exact drift as a past incident: a sprint bumped
        a migration and forgot the version file.  Derive it rather than restate
        it, so the next migration cannot silently skip the bump.
        """
        mig_dir = REPO_ROOT / "db" / "migrations" / "duckdb"
        numbers = sorted(int(p.name[:4]) for p in mig_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"))
        assert numbers, "no DuckDB migrations found"
        assert int(DUCKDB_VERSION_FILE.read_text().strip()) == numbers[-1], (
            f"duckdb_schema_version.txt says {DUCKDB_VERSION_FILE.read_text().strip()} "
            f"but the latest migration is {numbers[-1]:04d}"
        )

    def test_duckdb_0013_adds_manager_available_reliever_usage(self):
        path = (
            REPO_ROOT
            / "db"
            / "migrations"
            / "duckdb"
            / "0013_sim427_manager_available_reliever_usage.sql"
        )
        text = path.read_text(encoding="utf-8")
        assert "manager_season_metrics" in text
        assert "ADD COLUMN IF NOT EXISTS available_reliever_usage_rate" in text
        assert "'0013'" in text
        schema = (REPO_ROOT / "db" / "schemas" / "02_duckdb_schema.sql").read_text(encoding="utf-8")
        assert "available_reliever_usage_rate" in schema

    def test_duckdb_0012_adds_battedball_realism_cols(self):
        # SIM-411/413/425b: venue_id (park) + fielder_player_id (fielder RBF) on
        # sim.outcome_pool; p_throws (platoon) was already present.
        path = (
            REPO_ROOT
            / "db"
            / "migrations"
            / "duckdb"
            / "0012_sim411_413_425b_battedball_realism_cols.sql"
        )
        text = path.read_text(encoding="utf-8")
        assert "sim.outcome_pool" in text
        assert "ADD COLUMN IF NOT EXISTS venue_id" in text
        assert "ADD COLUMN IF NOT EXISTS fielder_player_id" in text
        assert "'0012'" in text  # migration_history entry
        # The canonical schema must carry the same two columns (fresh-build parity).
        schema = (REPO_ROOT / "db" / "schemas" / "02_duckdb_schema.sql").read_text(encoding="utf-8")
        assert "fielder_player_id" in schema

    def test_duckdb_0008_creates_play_stream(self):
        text = DUCKDB_MIGRATION.read_text()
        assert "sim.play_stream" in text
        assert "IF NOT EXISTS" in text
        assert "'0008'" in text  # migration_history entry

    def test_duckdb_0009_creates_state_snapshots(self):
        # SIM-357: the DuckDB backing for GET /state/{at_bat}/{pitch}.
        path = REPO_ROOT / "db" / "migrations" / "duckdb" / "0009_sim357_state_snapshots.sql"
        text = path.read_text()
        assert "sim.state_snapshots" in text
        assert "IF NOT EXISTS" in text
        assert "'0009'" in text  # migration_history entry

    def test_play_row_fields_match_playbyplayentry(self):
        """PLAY_ROW_FIELDS must cover every PlayByPlayEntry field (so a loaded
        row maps onto PlayByPlayEntry(**row))."""
        entry_fields = {f.name for f in dataclasses.fields(PlayByPlayEntry)}
        assert set(sim_store.PLAY_ROW_FIELDS) == entry_fields
