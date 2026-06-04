"""
test_sim433_bullpen_availability.py
===================================
SIM-433 — per-game bullpen availability + IL ingestion.

Three units under test, all without a live DB / network / docker:

  1. The Alembic migration 0015 chains off 0014 and creates
     raw.game_bullpen_availability with the documented shape.
  2. PlayerProfileComputor._compute_bullpen_workload — the raw.pitches-derived
     rest/recent-workload half, against a mocked DuckDB connection (the SQL is
     issued + the returned DataFrame is surfaced; the run() call site caches it).
  3. BullpenAvailabilityIngest — the MLB-API roster/IL PARSE + the availability
     decision + the workload↔roster merge, with the network seam (_mlb_get) wired
     to a captured roster fixture and the DB seam (_persist) captured in-memory.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from pipeline.batch import player_profile_computor as ppc
from pipeline.live.bullpen_availability_ingest import (
    REST_PITCHES_3D,
    AvailabilityRow,
    BullpenAvailabilityIngest,
    PitcherStatus,
    _as_date,
    _opt_int,
    _rest_as_of,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
_FIX = REPO_ROOT / "tests" / "fixtures" / "bullpen"


def _load_roster(name: str) -> dict:
    import json

    return json.loads((_FIX / name).read_text(encoding="utf-8"))


# ===========================================================================
# 1. Alembic migration 0015
# ===========================================================================


class TestMigration0015:
    PATH = REPO_ROOT / "db" / "migrations" / "versions" / "0015_sim433_game_bullpen_availability.py"

    def test_migration_file_exists(self):
        assert self.PATH.exists()

    def test_revises_0014(self):
        text = self.PATH.read_text(encoding="utf-8")
        assert 'revision = "0015"' in text
        assert 'down_revision = "0014"' in text

    def test_upgrade_creates_table_and_indexes(self):
        text = self.PATH.read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS raw.game_bullpen_availability" in text
        # PK + the two read-path indexes are present and IF NOT EXISTS (idempotent).
        assert "PRIMARY KEY (game_pk, team_id, pitcher_id)" in text
        assert "idx_gba_game_team" in text
        assert "idx_gba_pitcher" in text
        assert "CREATE INDEX IF NOT EXISTS" in text

    def test_reason_check_constraint_values(self):
        text = self.PATH.read_text(encoding="utf-8")
        for reason in ("active", "IL", "rest", "recent_use"):
            assert f"'{reason}'" in text

    def test_downgrade_drops_table_and_indexes(self):
        text = self.PATH.read_text(encoding="utf-8")
        assert "DROP TABLE IF EXISTS raw.game_bullpen_availability" in text
        assert "DROP INDEX IF EXISTS raw.idx_gba_game_team" in text
        assert "DROP INDEX IF EXISTS raw.idx_gba_pitcher" in text


# ===========================================================================
# 2. PlayerProfileComputor._compute_bullpen_workload
# ===========================================================================


def _workload_df() -> pd.DataFrame:
    """A representative workload result (the DataFrame DuckDB would return)."""
    return pd.DataFrame(
        [
            # An arm pitching today after 1 day off → back_to_back True.
            {
                "game_pk": 746437,
                "team_id": 116,
                "pitcher_id": 622663,
                "season": 2024,
                "game_date": "2024-08-15",
                "pitches_in_game": 18,
                "days_rest": 1,
                "back_to_back": True,
                "pitches_last_3d": 30,
            },
            # A well-rested arm.
            {
                "game_pk": 746437,
                "team_id": 116,
                "pitcher_id": 643410,
                "season": 2024,
                "game_date": "2024-08-15",
                "pitches_in_game": 15,
                "days_rest": 4,
                "back_to_back": False,
                "pitches_last_3d": 0,
            },
        ]
    )


def _computor_with_df(df: pd.DataFrame) -> ppc.PlayerProfileComputor:
    c = ppc.PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")
    conn = MagicMock()
    result = MagicMock()
    result.fetchdf.return_value = df
    conn.execute.return_value = result
    c._conn = conn
    return c


class TestComputeBullpenWorkload:
    def test_returns_dataframe_from_duckdb(self):
        c = _computor_with_df(_workload_df())
        out = c._compute_bullpen_workload([2024])
        assert isinstance(out, pd.DataFrame)
        assert len(out) == 2
        assert set(out["pitcher_id"]) == {622663, 643410}

    def test_sql_reads_raw_pitches_with_quality_filter_and_season(self):
        c = _computor_with_df(_workload_df())
        c._compute_bullpen_workload([2024, 2025])
        sql = c._conn.execute.call_args_list[-1].args[0]
        assert "pg.raw.pitches" in sql
        assert "data_quality_flag = FALSE" in sql
        assert "2024, 2025" in sql

    def test_sql_derives_fielding_team_from_half_inning(self):
        # Home fields the Top, away the Bottom — the team_id derivation.
        c = _computor_with_df(_workload_df())
        c._compute_bullpen_workload([2024])
        sql = c._conn.execute.call_args_list[-1].args[0]
        assert "inning_topbot = 'Top'" in sql
        assert "home_id" in sql and "away_id" in sql

    def test_sql_computes_rest_and_3day_workload(self):
        c = _computor_with_df(_workload_df())
        c._compute_bullpen_workload([2024])
        sql = c._conn.execute.call_args_list[-1].args[0]
        assert "days_rest" in sql
        assert "back_to_back" in sql
        assert "pitches_last_3d" in sql
        assert "LAG(" in sql  # previous-appearance lookup
        assert "INTERVAL 3 DAY" in sql  # 3-day window

    def test_empty_result_returns_empty_frame(self):
        c = _computor_with_df(pd.DataFrame())
        out = c._compute_bullpen_workload([2024])
        assert out.empty

    def test_does_not_write_to_postgres(self):
        # Postgres is attached READ_ONLY — the method must only SELECT, never INSERT.
        c = _computor_with_df(_workload_df())
        c._compute_bullpen_workload([2024])
        sql = c._conn.execute.call_args_list[-1].args[0]
        assert "INSERT" not in sql.upper()


# ===========================================================================
# 3. BullpenAvailabilityIngest — parse / decision / merge / ingest
# ===========================================================================


class _FixtureIngest(BullpenAvailabilityIngest):
    """Ingest with the network + DB seams wired for tests."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.persisted: list[AvailabilityRow] = []
        self.roster_calls: list[tuple[int, str]] = []

    def _mlb_get(self, path, params):  # type: ignore[override]
        if path == "schedule":
            return _load_roster_schedule()
        if path.endswith("/roster"):
            team_id = int(path.split("/")[1])
            self.roster_calls.append((team_id, params.get("date", "")))
            if team_id == 116 and params.get("rosterType") == "active":
                return _load_roster("mlb_roster_active_116.json")
            # away team / full-season fetch: empty roster
            return {"roster": []}
        raise AssertionError(f"unexpected MLB path {path}")

    def _persist(self, rows):  # type: ignore[override]
        self.persisted.extend(rows)


def _load_roster_schedule() -> dict:
    # Reuse the bettingpros schedule fixture shape: game_pk 746437, home 116, away 136.
    return {
        "dates": [
            {
                "games": [
                    {
                        "gamePk": 746437,
                        "gameDate": "2024-08-15T17:10:00Z",
                        "teams": {
                            "home": {"team": {"id": 116, "name": "Detroit Tigers"}},
                            "away": {"team": {"id": 136, "name": "Seattle Mariners"}},
                        },
                    }
                ]
            }
        ]
    }


class TestParseRoster:
    def test_keeps_only_pitchers(self):
        statuses = BullpenAvailabilityIngest.parse_roster(
            _load_roster("mlb_roster_active_116.json")
        )
        # 4 pitchers in the fixture; the 1B (596059) is excluded.
        assert 596059 not in statuses
        assert set(statuses) == {669373, 622663, 643410, 656298}

    def test_active_status_flags(self):
        statuses = BullpenAvailabilityIngest.parse_roster(
            _load_roster("mlb_roster_active_116.json")
        )
        assert statuses[669373].on_active_roster is True
        assert statuses[669373].on_il is False

    def test_il_status_detected(self):
        statuses = BullpenAvailabilityIngest.parse_roster(
            _load_roster("mlb_roster_active_116.json")
        )
        # 656298 has status code 'D60' → injured list, not on the active roster.
        assert statuses[656298].on_il is True
        assert statuses[656298].on_active_roster is False

    def test_empty_payload_is_empty(self):
        assert BullpenAvailabilityIngest.parse_roster({}) == {}
        assert BullpenAvailabilityIngest.parse_roster({"roster": []}) == {}

    def test_missing_status_block_treated_as_unavailable(self):
        payload = {
            "roster": [
                {
                    "person": {"id": 1},
                    "position": {"code": "1", "type": "Pitcher"},
                    # no "status" block
                }
            ]
        }
        statuses = BullpenAvailabilityIngest.parse_roster(payload)
        assert statuses[1].on_active_roster is False
        assert statuses[1].on_il is False

    def test_il_prefix_variants(self):
        for code in ("D10", "D15", "D60", "IL10", "IL60"):
            assert BullpenAvailabilityIngest._is_il_status(code) is True
        for code in ("A", "RM", "DES", ""):
            assert BullpenAvailabilityIngest._is_il_status(code) is False


class TestDecide:
    def setup_method(self):
        self.ingest = BullpenAvailabilityIngest()
        self.active = PitcherStatus(pitcher_id=1, on_active_roster=True, on_il=False)

    def test_il_takes_precedence(self):
        il = PitcherStatus(pitcher_id=1, on_active_roster=False, on_il=True)
        avail, reason = self.ingest.decide(
            pitcher_id=1, status=il, days_rest=5, pitches_last_3d=0, back_to_back=False
        )
        assert avail is False
        assert reason == "IL"

    def test_off_roster_is_il_reason(self):
        off = PitcherStatus(pitcher_id=1, on_active_roster=False, on_il=False)
        avail, reason = self.ingest.decide(
            pitcher_id=1, status=off, days_rest=5, pitches_last_3d=0, back_to_back=False
        )
        assert avail is False
        assert reason == "IL"

    def test_none_status_unavailable(self):
        avail, reason = self.ingest.decide(
            pitcher_id=1, status=None, days_rest=5, pitches_last_3d=0, back_to_back=False
        )
        assert avail is False
        assert reason == "IL"

    def test_back_to_back_blocks_rest(self):
        avail, reason = self.ingest.decide(
            pitcher_id=1, status=self.active, days_rest=1, pitches_last_3d=10, back_to_back=True
        )
        assert avail is False
        assert reason == "rest"

    def test_heavy_recent_workload_blocks(self):
        avail, reason = self.ingest.decide(
            pitcher_id=1,
            status=self.active,
            days_rest=2,
            pitches_last_3d=REST_PITCHES_3D + 5,
            back_to_back=False,
        )
        assert avail is False
        assert reason == "recent_use"

    def test_rested_arm_available(self):
        avail, reason = self.ingest.decide(
            pitcher_id=1, status=self.active, days_rest=4, pitches_last_3d=0, back_to_back=False
        )
        assert avail is True
        assert reason == "active"

    def test_threshold_is_inclusive(self):
        avail, reason = self.ingest.decide(
            pitcher_id=1,
            status=self.active,
            days_rest=2,
            pitches_last_3d=REST_PITCHES_3D,
            back_to_back=False,
        )
        assert avail is False
        assert reason == "recent_use"

    def test_custom_threshold(self):
        ing = BullpenAvailabilityIngest(rest_pitches_3d=20)
        avail, reason = ing.decide(
            pitcher_id=1, status=self.active, days_rest=2, pitches_last_3d=25, back_to_back=False
        )
        assert avail is False
        assert reason == "recent_use"


class TestBuildRows:
    def test_merges_workload_and_roster(self):
        ingest = BullpenAvailabilityIngest()
        roster = {
            116: BullpenAvailabilityIngest.parse_roster(_load_roster("mlb_roster_active_116.json"))
        }
        workload = _workload_df().to_dict("records")
        rows = ingest.build_rows(746437, workload, roster)
        by_pid = {r.pitcher_id: r for r in rows}
        # 622663 pitched back-to-back → rest-blocked.
        assert by_pid[622663].available is False
        assert by_pid[622663].reason == "rest"
        # 643410 well-rested + active → available.
        assert by_pid[643410].available is True
        assert by_pid[643410].reason == "active"
        # SIM-433-v2: the FULL roster is emitted, including arms that did NOT pitch.
        # 669373 is active + absent from the workload → available-but-UNUSED (the
        # signal v1 missed entirely).
        assert by_pid[669373].available is True
        assert by_pid[669373].reason == "active"
        # 656298 is on the IL (D60) and did not pitch → unavailable.
        assert by_pid[656298].available is False
        assert by_pid[656298].reason == "IL"
        # All 4 roster pitchers are emitted (v1 emitted only the 2 that pitched).
        assert len(rows) == 4

    def test_arm_absent_from_roster_but_pitched_is_available(self):
        # An arm that pitched in the game (in workload) but missing from the
        # roster fetch is still emitted available (it demonstrably pitched).
        ingest = BullpenAvailabilityIngest()
        workload = [
            {
                "game_pk": 1,
                "team_id": 999,
                "pitcher_id": 555,
                "days_rest": 3,
                "pitches_last_3d": 0,
                "back_to_back": False,
            }
        ]
        rows = ingest.build_rows(1, workload, roster_by_team={})
        assert rows[0].available is True
        assert rows[0].reason == "active"

    def test_il_arm_in_workload_still_marked_il(self):
        # Edge: an arm flagged IL by the roster but present in workload → IL wins.
        ingest = BullpenAvailabilityIngest()
        roster = {
            116: BullpenAvailabilityIngest.parse_roster(_load_roster("mlb_roster_active_116.json"))
        }
        workload = [
            {
                "game_pk": 746437,
                "team_id": 116,
                "pitcher_id": 656298,  # the D60 arm
                "days_rest": 5,
                "pitches_last_3d": 0,
                "back_to_back": False,
            }
        ]
        rows = ingest.build_rows(746437, workload, roster)
        # v2 emits the full roster; find the IL arm by id (IL wins over the workload).
        by_pid = {r.pitcher_id: r for r in rows}
        assert by_pid[656298].available is False
        assert by_pid[656298].reason == "IL"

    def test_nan_days_rest_becomes_none(self):
        ingest = BullpenAvailabilityIngest()
        workload = [
            {
                "game_pk": 1,
                "team_id": 1,
                "pitcher_id": 1,
                "days_rest": float("nan"),  # first appearance — DuckDB NULL → NaN
                "pitches_last_3d": 0,
                "back_to_back": False,
            }
        ]
        rows = ingest.build_rows(1, workload, roster_by_team={})
        assert rows[0].days_rest is None


class TestIngestGame:
    def test_ingest_game_resolves_persists_and_returns_rows(self):
        ingest = _FixtureIngest()
        workload = _workload_df().to_dict("records")
        rows = ingest.ingest_game(746437, workload)
        by_pid = {r.pitcher_id: r for r in rows}
        # v2: the FULL team-116 roster (4 pitchers) — incl. the unused arm 669373 —
        # not just the 2 that pitched.
        assert len(rows) == 4
        assert by_pid[669373].available is True and by_pid[669373].reason == "active"
        # Persisted exactly the rows it returned.
        assert ingest.persisted == rows
        # Resolved the roster for team 116 on the game's date.
        assert (116, "2024-08-15") in ingest.roster_calls

    def test_ingest_empty_workload_noops(self):
        ingest = _FixtureIngest()
        assert ingest.ingest_game(746437, []) == []
        assert ingest.persisted == []

    def test_ingest_unresolvable_game_skipped(self):
        ingest = _FixtureIngest()

        def _no_game(path, params):
            if path == "schedule":
                return {"dates": []}
            raise AssertionError("should not fetch roster for an unresolved game")

        ingest._mlb_get = _no_game  # type: ignore[assignment]
        workload = _workload_df().to_dict("records")
        assert ingest.ingest_game(746437, workload) == []

    def test_ingest_groups_by_game(self):
        ingest = _FixtureIngest()
        records = _workload_df().to_dict("records")
        total = ingest.ingest(records)
        # v2: the full team-116 roster (4 pitchers) for the single game, not just
        # the 2 that pitched.
        assert total == 4
        assert len(ingest.persisted) == 4

    def test_ingest_per_game_failure_isolated(self):
        ingest = _FixtureIngest()

        # Make ingest_game raise for one game; the run must continue.
        def _boom(game_pk, workload):
            raise RuntimeError("boom")

        ingest.ingest_game = _boom  # type: ignore[assignment]
        records = _workload_df().to_dict("records")
        # Should swallow the per-game error and return 0 (no rows persisted).
        assert ingest.ingest(records) == 0


class TestAvailabilityRow:
    def test_as_tuple_matches_insert_column_order(self):
        row = AvailabilityRow(
            game_pk=1,
            team_id=2,
            pitcher_id=3,
            available=True,
            reason="active",
            days_rest=4,
            pitches_last_3d=5,
            back_to_back=False,
            is_starter=False,
            source="mlb_api",
        )
        assert row.as_tuple() == (1, 2, 3, True, "active", 4, 5, False, False, "mlb_api")

    def test_persist_without_dsn_raises(self):
        ingest = BullpenAvailabilityIngest(pg_dsn=None)
        row = AvailabilityRow(
            game_pk=1,
            team_id=2,
            pitcher_id=3,
            available=True,
            reason="active",
            days_rest=4,
            pitches_last_3d=5,
            back_to_back=False,
        )
        with pytest.raises(RuntimeError, match="pg_dsn"):
            ingest._persist([row])

    def test_persist_empty_rows_noop(self):
        # Empty list must not require a DSN or touch the DB.
        ingest = BullpenAvailabilityIngest(pg_dsn=None)
        ingest._persist([])  # no raise


class TestOptInt:
    def test_none(self):
        assert _opt_int(None) is None

    def test_nan(self):
        assert _opt_int(float("nan")) is None

    def test_float_to_int(self):
        assert _opt_int(3.0) == 3

    def test_garbage(self):
        assert _opt_int("not-a-number") is None


# ===========================================================================
# 4. SIM-433-v2 — full-roster capture + timeline rest for unused arms
# ===========================================================================


class TestRestAsOf:
    def test_no_prior_appearance_is_fully_rested(self):
        from datetime import date

        assert _rest_as_of([], date(2024, 8, 15)) == (None, 0, False)

    def test_back_to_back_from_timeline(self):
        from datetime import date

        # threw yesterday (35 pitches) → days_rest 1, back_to_back, 35 in last 3d.
        tl = [(date(2024, 8, 14), 35)]
        days_rest, p3, b2b = _rest_as_of(tl, date(2024, 8, 15))
        assert (days_rest, p3, b2b) == (1, 35, True)

    def test_three_day_window_excludes_current_and_older(self):
        from datetime import date

        tl = [
            (date(2024, 8, 11), 20),  # 4 days before → excluded
            (date(2024, 8, 13), 15),  # 2 days before → included
            (date(2024, 8, 15), 99),  # the game day → excluded (< game_date only)
        ]
        days_rest, p3, b2b = _rest_as_of(tl, date(2024, 8, 15))
        assert days_rest == 2  # since 08-13
        assert p3 == 15  # only the 08-13 appearance is in the 1..3-day window
        assert b2b is False

    def test_as_date_coerces_str_and_datetime(self):
        from datetime import date, datetime

        assert _as_date("2024-08-15") == date(2024, 8, 15)
        assert _as_date(datetime(2024, 8, 15, 17, 10)) == date(2024, 8, 15)
        assert _as_date(date(2024, 8, 15)) == date(2024, 8, 15)
        assert _as_date(None) is None


class TestV2UnusedArmRest:
    def test_unused_arm_with_recent_workload_is_rest_blocked(self):
        # SIM-433-v2: an arm on the roster that did NOT pitch in this game but threw
        # yesterday must be 'rest'-unavailable (from the timeline), not falsely
        # 'active'. Without the timeline it would look available.
        from datetime import date

        ingest = BullpenAvailabilityIngest()
        roster = {
            116: BullpenAvailabilityIngest.parse_roster(_load_roster("mlb_roster_active_116.json"))
        }
        # 669373 is active + absent from the workload; give it a back-to-back timeline.
        timeline = {669373: [(date(2024, 8, 14), 30)]}
        rows = ingest.build_rows(
            746437,
            workload=[],
            roster_by_team=roster,
            timeline=timeline,
            game_date=date(2024, 8, 15),
        )
        by_pid = {r.pitcher_id: r for r in rows}
        assert by_pid[669373].available is False
        assert by_pid[669373].reason == "rest"
        assert by_pid[669373].back_to_back is True
        # A different active arm with no timeline → fully rested / available.
        assert by_pid[643410].available is True
        assert by_pid[643410].reason == "active"
