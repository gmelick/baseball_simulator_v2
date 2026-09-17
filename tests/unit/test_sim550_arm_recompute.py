"""
SIM-550 — the outfield arm block's recompute script and the fit probe's fielder read.

Plan: docs/audit/2026-09-16-sim550-outfield-arm-block-plan.md (§5.4 the script,
§5.5 the probe).

What these tests hold, on an in-memory DuckDB with the canonical schema:

* the script's direct pool aggregation (the verify block's own arithmetic) gives
  the hand-computed chances, attempts, runners thrown out and expected attempts,
  with the expectation per season x decision x outs x position cell (the
  fill's own cell; the named check reads value for value), a chance whose
  fielder is unknown ignored, and an infielder's chance outside the block;
* each verify check passes on a block that matches the pool and names the
  defect when the block drifts: a value off the pool, a block on a catcher's
  row, a block where the pool holds no chance, a row the fill skipped, a
  league row without its arm keys, a stale ``of_arm_runs``, a mixed cutoff;
* the cross-check builds the fielder-view URL (``type=Fld``, ``n=1``) from the
  runner entry without touching the registry, reads the headers, correlates
  the chances and the thrown-out rate, and fails loudly on a header it cannot
  place or a row from the wrong season;
* the script's own fill clears the block over the requested seasons before
  the matched UPDATE (a stale runner-view block on a row the pool never
  touches goes NULL), and ``main`` probes the writer lock on every path, so
  ``--skip-fill`` against a running app gives the run-book message;
* the probe's tier read: ``rate_report`` tiers the outfielders by their
  prevention, drops an actor without one, reports each tier's mean value and
  the spread ratio; the prevention map reads the fielder table and falls back
  to the embedding, and both center each outfielder on his position-season's
  chances-weighted mean, so a position-blind block still tiers by arm.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

duckdb = pytest.importorskip("duckdb")

REPO = Path(__file__).resolve().parents[2]
SCHEMA_SQL = REPO / "db" / "schemas" / "02_duckdb_schema.sql"
_SCRIPTS = REPO / "scripts"


def _load(name: str):
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


recompute = _load("sim550_arm_recompute")

TODAY = date(2026, 9, 17)

# ---------------------------------------------------------------------------
# The fixture: three outfielders (two center fielders share a cell), a catcher,
# a shortstop, a chance with no fielder, and one 2023 chance the 2024 run ignores
# ---------------------------------------------------------------------------

_POOL_COLS = (
    "pitch_id, game_pk, at_bat_number, pitch_number, game_date, season, scenario, "
    "from_base, target_base, runner_id, fielder_id, fielder_pos, outs, exit_velo, "
    "launch_angle, spray_angle, hit_distance, attempted, safe"
)

#: (fielder_id, fielder_pos, outs, chances, attempts, thrown_out) per cell. The
#: expectation cell is season x decision x outs x POSITION (the fill's rule).
#: CF, outs 0: 60 chances (101: 40 / 12; 103: 20 / 4) -> rate 16/60. CF, outs 1:
#: 20 chances, 1 attempt -> 0.05 (101 alone). LF, outs 0: 20 / 3 -> 0.15; LF,
#: outs 1: 20 / 4 -> 0.20 (102 alone in both, so his prevention is 0 by
#: construction). League: 120 chances, 96 holds (0.80), 24 attempts, one runner
#: thrown out (1/24 = 0.0417).
_CELLS = (
    (101, 8, 0, 40, 12, 1),
    (102, 7, 0, 20, 3, 0),
    (101, 8, 1, 20, 1, 0),
    (102, 7, 1, 20, 4, 0),
    (103, 8, 0, 20, 4, 0),
)
#: What the block must read for the three outfielders (hand-computed).
_EXPECTED_BLOCK = {
    (101, "CF"): {
        "arm_opportunities": 60,
        "arm_holds": 47,
        "arm_hold_rate": 47 / 60,
        "arm_assists": 1,
        "arm_thrown_out_rate": 1 / 13,
        "arm_advancement_prevention": (40 * 16 / 60 + 20 * 0.05 - 13) / 60,
    },
    (102, "LF"): {
        "arm_opportunities": 40,
        "arm_holds": 33,
        "arm_hold_rate": 33 / 40,
        "arm_assists": 0,
        "arm_thrown_out_rate": 0.0,
        "arm_advancement_prevention": (20 * 0.15 + 20 * 0.20 - 7) / 40,
    },
    (103, "CF"): {
        "arm_opportunities": 20,
        "arm_holds": 16,
        "arm_hold_rate": 16 / 20,
        "arm_assists": 0,
        "arm_thrown_out_rate": 0.0,
        "arm_advancement_prevention": (20 * 16 / 60 - 4) / 20,
    },
}


def _pool_rows() -> list[tuple]:
    rows: list[tuple] = []
    pitch_id = 0
    for fielder, pos, outs, chances, attempts, thrown_out in _CELLS:
        for i in range(chances):
            pitch_id += 1
            attempted = i < attempts
            safe = attempted and i >= thrown_out
            rows.append(
                (
                    pitch_id,
                    1,
                    pitch_id,
                    1,
                    date(2024, 6, 1),
                    2024,
                    1,
                    1,
                    3,
                    900,
                    fielder,
                    pos,
                    outs,
                    95.0,
                    12.0,
                    0.0,
                    250.0,
                    attempted,
                    safe,
                )
            )
    # A chance whose fielder is unknown, and a shortstop's chance: neither is
    # an outfield chance, so neither reaches a block.
    rows.append(
        (
            9001,
            1,
            9001,
            1,
            date(2024, 6, 1),
            2024,
            1,
            1,
            3,
            900,
            None,
            None,
            0,
            95.0,
            12.0,
            0.0,
            250.0,
            True,
            True,
        )
    )
    rows.append(
        (
            9002,
            1,
            9002,
            1,
            date(2024, 6, 1),
            2024,
            1,
            1,
            3,
            900,
            400,
            6,
            0,
            95.0,
            12.0,
            0.0,
            250.0,
            False,
            False,
        )
    )
    # One 2023 chance for the center fielder: a 2024-only run leaves it alone.
    rows.append(
        (
            9003,
            2,
            9003,
            1,
            date(2023, 6, 1),
            2023,
            1,
            1,
            3,
            900,
            101,
            8,
            0,
            95.0,
            12.0,
            0.0,
            250.0,
            False,
            False,
        )
    )
    return rows


def _fixture() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
    con.executemany(
        f"INSERT INTO sim.advancement_opportunity_pool ({_POOL_COLS}) "
        f"VALUES ({', '.join(['?'] * 19)})",
        _pool_rows(),
    )
    con.executemany(
        "INSERT INTO derived.fielder_season_metrics (player_id, position, season, "
        "arm_strength, below_minimum_sample, asof_date) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (101, "CF", 2024, 92.0, False, date(2026, 9, 16)),
            (102, "LF", 2024, 90.0, False, date(2026, 9, 16)),
            (103, "CF", 2024, 89.0, False, date(2026, 9, 16)),
            (300, "C", 2024, None, False, date(2026, 9, 16)),
            (400, "SS", 2024, None, False, date(2026, 9, 16)),
            (101, "CF", 2023, 91.0, False, date(2026, 9, 16)),
        ],
    )
    return con


def _fill_like_the_step(con: duckdb.DuckDBPyConnection, seasons: list[int]) -> None:
    """Write the block from the script's own pool aggregation — the same
    arithmetic the computor's fill step uses (that step has its own tests)."""
    con.execute(
        f"""
        UPDATE derived.fielder_season_metrics f SET
            arm_opportunities          = a.chances,
            arm_holds                  = a.chances - a.attempts,
            arm_hold_rate              = 1.0 - a.attempts * 1.0 / a.chances,
            arm_assists                = a.thrown_out,
            arm_thrown_out_rate        = a.thrown_out * 1.0 / NULLIF(a.attempts, 0),
            arm_advancement_prevention = (a.expected_attempts - a.attempts) / a.chances,
            of_arm_runs                = NULL
        FROM ({recompute._pool_block_sql(seasons)}) a
        WHERE f.player_id = a.player_id AND f.position = a.position AND f.season = a.season
        """
    )


def _seed_league_rows(con: duckdb.DuckDBPyConnection, overrides: dict | None = None) -> None:
    overrides = overrides or {}
    con.execute(recompute.LeagueAverageProfiles.LEAGUE_AVG_DDL)
    for pos in ("LF", "CF", "RF"):
        for season in (2023, 2024):
            profile = {
                "outs_above_average": 0.5,
                "error_rate": 0.01,
                "arm_hold_rate": 0.8,
                "dp_run_value": None,
                "arm_advancement_prevention": 0.0,
                "arm_thrown_out_rate": 0.05,
                "arm_strength": 90.0,
            }
            profile.update(overrides.get((pos, season), {}))
            for k in overrides.get(("drop", pos, season), ()):
                profile.pop(k, None)
            con.execute(
                "INSERT OR REPLACE INTO derived.league_averages VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                [f"fielder_{pos}", season, json.dumps(profile)],
            )


@pytest.fixture
def small_floors(monkeypatch):
    """The coverage floors are sized for a real season; the fixture has two
    outfield rows."""
    monkeypatch.setattr(recompute, "BLOCK_FLOOR_FULL", 2)
    monkeypatch.setattr(recompute, "BLOCK_FLOOR_2020", 1)
    monkeypatch.setattr(recompute, "BLOCK_FLOOR_CURRENT", 1)


# ---------------------------------------------------------------------------
# The pool aggregation and the floors
# ---------------------------------------------------------------------------


class TestPoolAggregation:
    def test_the_direct_pool_query_is_the_hand_computed_block(self) -> None:
        con = _fixture()
        rows = {
            (int(pid), pos): (int(ch), int(att), int(out), float(exp))
            for pid, pos, _season, ch, att, out, exp in con.execute(
                recompute._pool_block_sql([2024])
            ).fetchall()
        }
        # No shortstop, no unknown fielder.
        assert set(rows) == {(101, "CF"), (102, "LF"), (103, "CF")}
        # The expectation is per position: 101's 40 chances at 0 outs sit in the
        # CF cell (16/60), not in a cell shared with the left fielder.
        assert rows[(101, "CF")] == (60, 13, 1, pytest.approx(40 * 16 / 60 + 1.0))
        assert rows[(102, "LF")] == (40, 7, 0, pytest.approx(7.0))
        assert rows[(103, "CF")] == (20, 4, 0, pytest.approx(20 * 16 / 60))

    def test_the_position_filter_keeps_one_position(self) -> None:
        con = _fixture()
        rows = con.execute(recompute._pool_block_sql([2024], position_num=8)).fetchall()
        assert sorted((int(r[0]), r[1]) for r in rows) == [(101, "CF"), (103, "CF")]

    def test_the_block_floor_by_season(self) -> None:
        assert recompute.block_floor(2024, TODAY) == recompute.BLOCK_FLOOR_FULL
        assert recompute.block_floor(2020, TODAY) == recompute.BLOCK_FLOOR_2020
        assert recompute.block_floor(2026, TODAY) == recompute.BLOCK_FLOOR_CURRENT

    def test_pearson(self) -> None:
        assert recompute.pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
        assert recompute.pearson([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
        assert np.isnan(recompute.pearson([1, 1, 1], [1, 2, 3]))
        assert np.isnan(recompute.pearson([1], [2]))


# ---------------------------------------------------------------------------
# The verify checks
# ---------------------------------------------------------------------------


class TestVerifyChecks:
    def test_a_block_that_matches_the_pool_passes_every_check(self, small_floors) -> None:
        con = _fixture()
        _fill_like_the_step(con, [2024])
        _seed_league_rows(con)
        assert recompute.check_block_coverage(con, [2024], TODAY) == []
        assert recompute.check_league_figures(con, [2024]) == []
        assert recompute.check_block_only_where_fielded(con, [2024]) == []
        assert recompute.check_named_center_fielders(con, 2024) == []
        assert recompute.check_of_arm_runs_null(con, [2024]) == []
        assert recompute.check_arm_strength_coverage(con, [2024], TODAY) == []
        assert recompute.check_league_rows(con, [2024]) == []
        assert recompute.check_one_asof(con) == []

    def test_the_block_reads_the_hand_computed_values(self) -> None:
        con = _fixture()
        _fill_like_the_step(con, [2024])
        for (pid, pos), want in _EXPECTED_BLOCK.items():
            row = con.execute(
                f"SELECT {', '.join(want)} FROM derived.fielder_season_metrics "
                "WHERE player_id = ? AND position = ? AND season = 2024",
                [pid, pos],
            ).fetchone()
            got = dict(zip(want, row, strict=True))
            for k, v in want.items():
                assert got[k] == pytest.approx(v), (pid, pos, k)
        # The catcher, the shortstop and the 2023 row stay NULL.
        assert (
            con.execute(
                "SELECT COUNT(*) FROM derived.fielder_season_metrics WHERE arm_opportunities IS NOT NULL"
            ).fetchone()[0]
            == 3
        )

    def test_the_real_fill_step_agrees_with_the_script_value_for_value(self, small_floors) -> None:
        """The computor's own ``_fill_outfield_arm_block`` on this fixture reads the
        hand-computed block and passes the script's checks — the two SQLs (the fill
        and ``_pool_block_sql``) share one cell key and one arithmetic."""
        from pipeline.batch.player_profile_computor import PlayerProfileComputor

        con = _fixture()
        computor = PlayerProfileComputor(pg_dsn="", duckdb_path=":memory:")
        computor._conn = con
        computor._fill_outfield_arm_block([2024], asof=None)
        for (pid, pos), want in _EXPECTED_BLOCK.items():
            row = con.execute(
                f"SELECT {', '.join(want)} FROM derived.fielder_season_metrics "
                "WHERE player_id = ? AND position = ? AND season = 2024",
                [pid, pos],
            ).fetchone()
            got = dict(zip(want, row, strict=True))
            for k, v in want.items():
                assert got[k] == pytest.approx(v), (pid, pos, k)
        _seed_league_rows(con)
        assert recompute.check_named_center_fielders(con, 2024) == []
        assert recompute.check_block_only_where_fielded(con, [2024]) == []
        assert recompute.check_league_figures(con, [2024]) == []
        assert recompute.check_of_arm_runs_null(con, [2024]) == []

    def test_the_named_check_catches_a_value_off_the_pool(self) -> None:
        con = _fixture()
        _fill_like_the_step(con, [2024])
        con.execute(
            "UPDATE derived.fielder_season_metrics SET arm_assists = 0 "
            "WHERE player_id = 101 AND position = 'CF' AND season = 2024"
        )
        problems = recompute.check_named_center_fielders(con, 2024)
        assert len(problems) == 1 and "arm_assists" in problems[0] and "CF 101" in problems[0]

    def test_the_named_check_reports_a_center_fielder_without_a_row(self) -> None:
        con = _fixture()
        _fill_like_the_step(con, [2024])
        con.execute("DELETE FROM derived.fielder_season_metrics WHERE player_id = 101")
        problems = recompute.check_named_center_fielders(con, 2024)
        assert problems == [
            "season 2024: center fielder 101 (60 chances in the pool) has no CF row in "
            "derived.fielder_season_metrics"
        ]

    def test_a_block_on_a_catchers_row_is_a_problem(self) -> None:
        con = _fixture()
        _fill_like_the_step(con, [2024])
        con.execute(
            "UPDATE derived.fielder_season_metrics SET arm_opportunities = 12, arm_holds = 10 "
            "WHERE player_id = 300"
        )
        problems = recompute.check_block_only_where_fielded(con, [2024])
        assert any("non-outfield" in p for p in problems)
        assert any("catcher-only" in p for p in problems)

    def test_a_block_where_the_pool_holds_no_chance_is_a_problem(self) -> None:
        con = _fixture()
        _fill_like_the_step(con, [2024])
        con.execute(
            "INSERT INTO derived.fielder_season_metrics (player_id, position, season, "
            "arm_opportunities, arm_holds) VALUES (500, 'RF', 2024, 30, 25)"
        )
        problems = recompute.check_block_only_where_fielded(con, [2024])
        assert problems == [
            "1 outfield rows carry a block although the pool holds no chance fielded by "
            "that player at that position — the block came from somewhere else"
        ]

    def test_a_row_the_fill_skipped_is_a_problem(self) -> None:
        con = _fixture()
        _fill_like_the_step(con, [2024])
        # The 2023 chance has a row and no block: a 2023-2024 run must flag it,
        # a 2024-only run must not.
        assert recompute.check_block_only_where_fielded(con, [2024]) == []
        problems = recompute.check_block_only_where_fielded(con, [2023, 2024])
        assert problems == [
            "1 outfield rows have chances in the pool but no block — the fill skipped them"
        ]

    def test_the_league_figures_name_an_out_of_band_hold(self) -> None:
        con = _fixture()
        _fill_like_the_step(con, [2024])
        con.execute(
            "UPDATE derived.fielder_season_metrics SET arm_holds = 20 "
            "WHERE player_id = 101 AND position = 'CF' AND season = 2024"
        )
        problems = recompute.check_league_figures(con, [2024])
        # holds 20 + 33 + 16 = 69 of 120 chances
        assert any("hold rate is 0.5750" in p for p in problems)

    def test_the_coverage_check_names_a_missing_season_and_a_thin_one(
        self, small_floors, monkeypatch
    ) -> None:
        con = _fixture()
        _fill_like_the_step(con, [2024])
        problems = recompute.check_block_coverage(con, [2024, 2025], TODAY)
        assert problems == ["season 2025: no outfield fielder rows at all"]
        monkeypatch.setattr(recompute, "BLOCK_FLOOR_FULL", 4)
        problems = recompute.check_block_coverage(con, [2024], TODAY)
        assert len(problems) == 1 and "only 3 outfield rows carry a block (floor 4)" in problems[0]

    def test_a_stale_of_arm_runs_is_a_problem_inside_the_run_only(self) -> None:
        con = _fixture()
        _fill_like_the_step(con, [2024])
        con.execute(
            "UPDATE derived.fielder_season_metrics SET of_arm_runs = 1.5 WHERE season = 2023"
        )
        assert recompute.check_of_arm_runs_null(con, [2024]) == []
        problems = recompute.check_of_arm_runs_null(con, [2023, 2024])
        assert len(problems) == 1 and "1 rows" in problems[0]

    def test_the_arm_strength_coverage_floor(self, monkeypatch) -> None:
        con = _fixture()
        assert recompute.check_arm_strength_coverage(con, [2024], TODAY) == []
        con.execute(
            "UPDATE derived.fielder_season_metrics SET arm_strength = NULL "
            "WHERE player_id IN (102, 103)"
        )
        # 2024: one of three outfield rows carries a velocity (0.333) — below 0.6.
        problems = recompute.check_arm_strength_coverage(con, [2024], TODAY)
        assert len(problems) == 1 and "covers 0.333" in problems[0]
        monkeypatch.setattr(recompute, "ARM_STRENGTH_COVERAGE_FLOOR", 0.3)
        assert recompute.check_arm_strength_coverage(con, [2024], TODAY) == []
        # The season in progress is logged, never gated.
        monkeypatch.setattr(recompute, "ARM_STRENGTH_COVERAGE_FLOOR", 0.9)
        assert recompute.check_arm_strength_coverage(con, [2024], date(2024, 8, 1)) == []

    def test_the_league_rows_need_the_three_arm_keys(self) -> None:
        con = _fixture()
        _seed_league_rows(con, {("drop", "CF", 2024): ("arm_strength",)})
        problems = recompute.check_league_rows(con, [2024])
        assert problems == [
            "'fielder_CF' league row 2024 lacks the keys ['arm_strength'] — the shrinkage has "
            "no target"
        ]

    def test_a_null_velocity_is_fine_before_2023_and_a_problem_after(self) -> None:
        con = _fixture()
        con.execute(
            "INSERT INTO derived.fielder_season_metrics (player_id, position, season, "
            "below_minimum_sample) VALUES (103, 'RF', 2022, FALSE), (104, 'RF', 2024, FALSE)"
        )
        _seed_league_rows(con, {("RF", 2024): {"arm_strength": None}})
        con.execute(
            "INSERT OR REPLACE INTO derived.league_averages VALUES ('fielder_RF', 2022, ?, "
            "CURRENT_TIMESTAMP)",
            [
                json.dumps(
                    {
                        "arm_advancement_prevention": 0.0,
                        "arm_thrown_out_rate": 0.05,
                        "arm_strength": None,
                    }
                )
            ],
        )
        assert recompute.check_league_rows(con, [2022]) == []
        problems = recompute.check_league_rows(con, [2024])
        assert problems == ["'fielder_RF' league row 2024: arm_strength is null"]

    def test_a_missing_league_row_for_a_season_with_data(self) -> None:
        con = _fixture()
        _seed_league_rows(con)
        con.execute("DELETE FROM derived.league_averages WHERE entity_type = 'fielder_LF'")
        problems = recompute.check_league_rows(con, [2024])
        assert problems == ["no 'fielder_LF' league-average row for season 2024"]

    def test_a_mixed_cutoff_is_a_problem(self) -> None:
        con = _fixture()
        assert recompute.check_one_asof(con) == []
        con.execute(
            "UPDATE derived.fielder_season_metrics SET asof_date = DATE '2026-09-01' "
            "WHERE player_id = 300"
        )
        problems = recompute.check_one_asof(con)
        assert len(problems) == 1 and "2 distinct asof_date" in problems[0]

    def test_verify_runs_every_check_and_raises_on_a_problem(
        self, tmp_path, small_floors, monkeypatch
    ) -> None:
        path = str(tmp_path / "sim550.duckdb")
        con = duckdb.connect(path)
        con.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        con.executemany(
            f"INSERT INTO sim.advancement_opportunity_pool ({_POOL_COLS}) "
            f"VALUES ({', '.join(['?'] * 19)})",
            _pool_rows(),
        )
        con.executemany(
            "INSERT INTO derived.fielder_season_metrics (player_id, position, season, "
            "arm_strength, below_minimum_sample, asof_date) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (101, "CF", 2024, 92.0, False, date(2026, 9, 16)),
                (102, "LF", 2024, 90.0, False, date(2026, 9, 16)),
                (103, "CF", 2024, 89.0, False, date(2026, 9, 16)),
            ],
        )
        _fill_like_the_step(con, [2024])
        _seed_league_rows(con)
        con.close()
        recompute.verify(path, [2024], cross_check=False)
        con = duckdb.connect(path)
        con.execute("UPDATE derived.fielder_season_metrics SET of_arm_runs = 2.0")
        con.close()
        with pytest.raises(SystemExit, match="1 verification problem"):
            recompute.verify(path, [2024], cross_check=False)


# ---------------------------------------------------------------------------
# The script's own fill: the clear before the matched UPDATE, the lock probe
# ---------------------------------------------------------------------------


def _file_fixture(path: str) -> None:
    con = duckdb.connect(path)
    con.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
    con.executemany(
        f"INSERT INTO sim.advancement_opportunity_pool ({_POOL_COLS}) "
        f"VALUES ({', '.join(['?'] * 19)})",
        _pool_rows(),
    )
    con.executemany(
        "INSERT INTO derived.fielder_season_metrics (player_id, position, season, "
        "arm_strength, below_minimum_sample, asof_date) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (101, "CF", 2024, 92.0, False, date(2026, 9, 16)),
            (102, "LF", 2024, 90.0, False, date(2026, 9, 16)),
            (103, "CF", 2024, 89.0, False, date(2026, 9, 16)),
            (101, "CF", 2023, 91.0, False, date(2026, 9, 16)),
        ],
    )
    con.close()


class TestFillArmBlock:
    def test_the_fill_clears_a_stale_block_on_a_row_the_pool_never_touches(
        self, tmp_path, small_floors
    ) -> None:
        """The live table holds 23 outfield rows the old runner-view join
        filled and the pool never touches (thin rows, 1 to 5 batted balls).
        The computor's fill is a matched UPDATE, so alone it leaves them; the
        script clears the block over the requested seasons first, as the
        nightly aggregator does, and the verify checks come back green. A
        row of a season outside the run keeps its block."""
        path = str(tmp_path / "sim550_fill.duckdb")
        _file_fixture(path)
        con = duckdb.connect(path)
        stale = (
            "INSERT INTO derived.fielder_season_metrics (player_id, position, season, "
            "arm_opportunities, arm_holds, arm_hold_rate, arm_assists, arm_thrown_out_rate, "
            "arm_advancement_prevention, of_arm_runs, below_minimum_sample, asof_date) "
            "VALUES (?, ?, ?, 92, 61, 0.66, 2, 0.06, -0.01, -0.09, TRUE, DATE '2026-09-16')"
        )
        con.execute(stale, [641355, "LF", 2024])  # no 2024 pool chance at LF
        con.execute(stale, [543807, "CF", 2022])  # a season outside the run
        con.close()

        recompute.fill_arm_block(path, [2024])

        con = duckdb.connect(path, read_only=True)
        try:
            row = con.execute(
                f"SELECT {', '.join(recompute.ARM_BLOCK_COLUMNS)} "
                "FROM derived.fielder_season_metrics WHERE player_id = 641355"
            ).fetchone()
            assert row == (None,) * len(recompute.ARM_BLOCK_COLUMNS)
            outside = con.execute(
                "SELECT arm_opportunities, of_arm_runs FROM derived.fielder_season_metrics "
                "WHERE player_id = 543807"
            ).fetchone()
            assert outside == (92, pytest.approx(-0.09))
            # The matched rows still read the hand-computed block after the clear.
            for (pid, pos), want in _EXPECTED_BLOCK.items():
                got = con.execute(
                    f"SELECT {', '.join(want)} FROM derived.fielder_season_metrics "
                    "WHERE player_id = ? AND position = ? AND season = 2024",
                    [pid, pos],
                ).fetchone()
                for k, v in zip(want, got, strict=True):
                    assert v == pytest.approx(want[k]), (pid, pos, k)
            assert recompute.check_block_only_where_fielded(con, [2024]) == []
            assert recompute.check_of_arm_runs_null(con, [2024]) == []
        finally:
            con.close()

    def test_clear_arm_block_counts_the_rows_it_cleared(self) -> None:
        con = _fixture()
        _fill_like_the_step(con, [2024])
        con.execute(
            "UPDATE derived.fielder_season_metrics SET of_arm_runs = 1.0 WHERE player_id = 300"
        )
        assert recompute.clear_arm_block(con, [2024]) == 4  # three blocks + the catcher's run value
        assert recompute.clear_arm_block(con, [2024]) == 0
        assert (
            con.execute(
                "SELECT COUNT(*) FROM derived.fielder_season_metrics WHERE season = 2024 AND ("
                + " OR ".join(f"{c} IS NOT NULL" for c in recompute.ARM_BLOCK_COLUMNS)
                + ")"
            ).fetchone()[0]
            == 0
        )

    def test_skip_fill_against_a_locked_database_gives_the_run_book_message(
        self, tmp_path, monkeypatch
    ) -> None:
        """With ``--skip-fill`` the fill's guarded connection never opens, and
        the league-row recompute's own ``duckdb.connect`` would raise the raw
        writer-lock error. The probe at the top of ``main`` turns it into the
        run-book instruction on every path."""
        path = str(tmp_path / "sim550_locked.duckdb")
        _file_fixture(path)
        real_connect = duckdb.connect

        def locked_connect(*args, **kwargs):
            if args and args[0] == path and not kwargs.get("read_only", False):
                raise duckdb.IOException(
                    f"IO Error: Could not set lock on file {path}: File is already open"
                )
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(duckdb, "connect", locked_connect)
        with pytest.raises(SystemExit, match="docker compose stop app") as info:
            recompute.main(["--skip-fill", "--no-cross-check", "--duckdb-path", path])
        assert "sim550_locked.duckdb" in str(info.value)
        # The same run without the flag reads the same message: one probe, one text.
        with pytest.raises(SystemExit, match="docker compose stop app"):
            recompute.main(["--no-cross-check", "--duckdb-path", path])


# ---------------------------------------------------------------------------
# The cross-check against Savant's fielder view
# ---------------------------------------------------------------------------


def _fielder_view_csv(rows: list[tuple[int, int, int, int, int]]) -> str:
    head = "entity_id,entity_name,year,n_opp_xb,n_att_xb,n_out,fielder_runs\n"
    body = "".join(
        f"{pid},Player {pid},{yr},{opp},{att},{out},0.5\n" for pid, yr, opp, att, out in rows
    )
    return head + body


class TestCrossCheck:
    def test_the_url_is_the_fielder_view_of_the_runner_entry(self) -> None:
        from pipeline.etl.savant_boards import BOARDS

        seen: list[str] = []

        def fetcher(url: str) -> str:
            seen.append(url)
            return _fielder_view_csv([(101, 2024, 55, 15, 1)])

        headers, rows = recompute.savant_fielder_view(2024, fetcher)
        assert len(seen) == 1
        assert "type=Fld" in seen[0] and "n=1" in seen[0] and "season_start=2024" in seen[0]
        assert headers[:3] == ["entity_id", "entity_name", "year"]
        assert rows[0]["n_opp_xb"] == "55"
        # The registry's runner entry is untouched.
        assert BOARDS["baserunning"].extra == {"n": "1", "type": "Run"}

    def test_the_column_resolver_names_what_it_cannot_place(self) -> None:
        cols = recompute.resolve_fielder_view_columns(
            ["entity_id", "year", "n_opp_xb", "n_att_xb", "n_out"]
        )
        assert cols == {
            "player": "entity_id",
            "chances": "n_opp_xb",
            "attempts": "n_att_xb",
            "thrown_out": "n_out",
        }
        with pytest.raises(KeyError, match="thrown_out"):
            recompute.resolve_fielder_view_columns(["entity_id", "n_opp_xb", "n_att_xb"])

    def test_a_matching_view_passes_and_a_shuffled_one_fails(self, monkeypatch) -> None:
        con = _fixture()
        _fill_like_the_step(con, [2024])
        monkeypatch.setattr(recompute, "CROSS_CHECK_MIN_PLAYERS", 2)
        monkeypatch.setattr(recompute, "CROSS_CHECK_MIN_CHANCES", 30)
        # Savant counts fewer chances (no batter stretch) but the order and the
        # thrown-out rates agree: 101 has 60 / 13 / 1 here, 102 has 40 / 7 / 0.
        good = _fielder_view_csv(
            [(101, 2024, 50, 12, 1), (102, 2024, 33, 6, 0), (999, 2024, 80, 20, 2)]
        )
        assert recompute.cross_check_savant(con, 2024, fetcher=lambda _u: good) == []
        bad = _fielder_view_csv([(101, 2024, 33, 6, 0), (102, 2024, 50, 12, 1)])
        problems = recompute.cross_check_savant(con, 2024, fetcher=lambda _u: bad)
        assert any("r(chances) = -1.000" in p for p in problems)
        assert any("r(thrown-out rate) = -1.000" in p for p in problems)

    def test_a_wrong_season_row_and_a_strange_header_fail_loudly(self) -> None:
        con = _fixture()
        _fill_like_the_step(con, [2024])
        stale = _fielder_view_csv([(101, 2026, 50, 12, 1)])
        problems = recompute.cross_check_savant(con, 2024, fetcher=lambda _u: stale)
        assert len(problems) == 1 and "could not be read" in problems[0] and "2026" in problems[0]
        strange = "foo,bar\n1,2\n"
        problems = recompute.cross_check_savant(con, 2024, fetcher=lambda _u: strange)
        assert len(problems) == 1 and "headers: ['foo', 'bar']" in problems[0]

    def test_too_few_matched_players_is_a_problem(self) -> None:
        con = _fixture()
        _fill_like_the_step(con, [2024])
        csv = _fielder_view_csv([(101, 2024, 50, 12, 1), (102, 2024, 33, 6, 0)])
        problems = recompute.cross_check_savant(con, 2024, fetcher=lambda _u: csv)
        assert any("only 1 player(s)" in p for p in problems)


# ---------------------------------------------------------------------------
# The fit probe's fielder tier read
# ---------------------------------------------------------------------------


probe = _load("sim523_fit_probe")


class TestProbeFielderRead:
    @staticmethod
    def _synthetic():
        # Six outfielders, each 100 sim draws and 200 own rows. The sim attempt
        # rate tracks the own rate, which falls as prevention rises.
        prevention = {
            "1:LF:2024": -0.10,
            "2:CF:2024": -0.05,
            "3:RF:2024": 0.00,
            "4:LF:2024": 0.03,
            "5:CF:2024": 0.08,
        }
        sim = {}
        ref = {}
        for i, key in enumerate(list(prevention) + ["6:RF:2024"]):
            att = 40 - 5 * i
            sim[key] = [100, att, att - 2]
            ref[key] = np.array([200.0, 2.0 * att, 2.0 * att - 4.0])
        return sim, ref, prevention

    def test_the_tiers_follow_the_prevention_and_drop_an_actor_without_one(self) -> None:
        sim, ref, prevention = self._synthetic()
        rep = probe.rate_report(sim, ref, "fielders", tier_by=prevention)
        assert rep["n_actors"] == 5  # the sixth has no prevention
        assert len(rep["tiers"]) == 3
        values = [t["tier_value"] for t in rep["tiers"]]
        assert values == sorted(values)
        # Five actors of equal weight split 1 / 2 / 2 by cumulative weight.
        assert rep["tiers"][0]["tier_value"] == pytest.approx(-0.10)
        assert rep["tiers"][2]["tier_value"] == pytest.approx(0.055)
        # A sim that tracks the pool reads a spread ratio of one.
        assert rep["spread_ratio"] == pytest.approx(1.0)
        assert rep["spread_own"] < 0  # the strong arms concede fewer attempts

    def test_without_a_tier_key_the_report_is_the_runner_read(self) -> None:
        sim, ref, _ = self._synthetic()
        rep = probe.rate_report(sim, ref, "runners")
        assert rep["n_actors"] == 6
        assert "tier_value" not in rep["tiers"][0] and "spread_ratio" not in rep

    def test_the_prevention_map_reads_the_fielder_table_centered_per_position(self) -> None:
        """The tier key is the outfielder's prevention minus his
        position-season's chances-weighted mean. On a block the fill wrote
        (its expectation cell is per position) that mean is 0 by construction,
        so the key equals the stored value; a block from a position-blind cell
        loses its position offset instead (the next test)."""
        con = _fixture()
        _fill_like_the_step(con, [2024])
        got = probe._fielder_prevention_map(con)
        assert set(got) == {"101:CF:2024", "102:LF:2024", "103:CF:2024"}
        for (pid, pos), want in _EXPECTED_BLOCK.items():
            assert got[f"{pid}:{pos}:2024"] == pytest.approx(
                want["arm_advancement_prevention"], abs=1e-6
            )
        # A position-blind block: the left fielder's stored +0.04 is the
        # position offset, and his key reads 0 once centered.
        con.execute(
            "UPDATE derived.fielder_season_metrics SET arm_advancement_prevention = 0.04 "
            "WHERE player_id = 102"
        )
        assert probe._fielder_prevention_map(con)["102:LF:2024"] == pytest.approx(0.0)
        assert probe._fielder_prevention_map(None) == {}
        assert probe._fielder_prevention_map(duckdb.connect(":memory:")) == {}

    def test_the_embedding_fallback_keeps_outfielders_with_chances(self) -> None:
        feats = ["outs_above_average", "arm_opportunities", "arm_advancement_prevention"]
        keys = ["101:CF:2024", "102:LF:2024", "300:C:2024", "103:RF:2024", "104:CF:2024"]
        vecs = np.array(
            [
                [1.0, 60.0, -0.02],
                [0.5, 0.0, 0.0],  # no chances: the 0.0 is a NULL, not a value
                [0.0, 0.0, 0.0],
                [2.0, 25.0, 0.04],
                [1.5, 20.0, 0.06],
            ],
            dtype=np.float32,
        )
        fp = SimpleNamespace(
            a=SimpleNamespace(
                actor_emb={"fielder": {"keys": keys, "features": feats, "vecs": vecs}}
            )
        )
        got = probe._embedding_prevention_map(fp)
        cf_mean = (-0.02 * 60 + 0.06 * 20) / 80
        assert set(got) == {"101:CF:2024", "103:RF:2024", "104:CF:2024"}
        assert got["101:CF:2024"] == pytest.approx(-0.02 - cf_mean, abs=1e-6)
        assert got["104:CF:2024"] == pytest.approx(0.06 - cf_mean, abs=1e-6)
        assert got["103:RF:2024"] == pytest.approx(0.0, abs=1e-6)  # alone at his position
        assert (
            probe._embedding_prevention_map(SimpleNamespace(a=SimpleNamespace(actor_emb={}))) == {}
        )

    def test_the_tier_key_is_the_deviation_from_the_position_mean(self) -> None:
        """The defect the review found: on a block whose expectation cell is
        position-blind, every left fielder's raw prevention sits above every
        right fielder's. A tercile over the raw value puts only left fielders
        in the top tier, whatever their arms (the strongest right-field arm
        lands in the middle). Centered per position-season, the strongest
        arm of each position lands in the top tier and the weakest right
        fielder alone in the bottom; each position's keys average zero.
        (``_tiers`` splits six equal weights 1 / 2 / 3.)"""
        raw = {
            "1:LF:2024": (0.020, 100.0),  # the weakest LF arm
            "2:LF:2024": (0.040, 100.0),
            "3:LF:2024": (0.060, 100.0),  # the strongest LF arm
            "4:RF:2024": (-0.050, 100.0),  # the weakest RF arm
            "5:RF:2024": (-0.020, 100.0),
            "6:RF:2024": (0.010, 100.0),  # the strongest RF arm
        }
        by_raw = probe._tiers([(k, 1.0, v) for k, (v, _) in raw.items()], 1, lambda t: t[2], 3)
        assert {t[0].split(":")[1] for t in by_raw[2]} == {"LF"}
        assert "6:RF:2024" in [t[0] for t in by_raw[1]]
        centered = probe._center_within_position(raw)
        assert sum(v for k, v in centered.items() if ":LF:" in k) == pytest.approx(0.0)
        assert sum(v for k, v in centered.items() if ":RF:" in k) == pytest.approx(0.0)
        by_centered = probe._tiers([(k, 1.0, v) for k, v in centered.items()], 1, lambda t: t[2], 3)
        assert [t[0] for t in by_centered[0]] == ["4:RF:2024"]
        top = [t[0] for t in by_centered[2]]
        assert "3:LF:2024" in top and "6:RF:2024" in top
        # The mean is chances-weighted: the regular's 300 chances set it, not
        # the call-up's 10; and a season is its own group.
        weighted = probe._center_within_position(
            {"7:CF:2024": (0.010, 300.0), "8:CF:2024": (0.090, 10.0), "9:CF:2025": (0.5, 50.0)}
        )
        mean = (0.010 * 300 + 0.090 * 10) / 310
        assert weighted["7:CF:2024"] == pytest.approx(0.010 - mean)
        assert weighted["8:CF:2024"] == pytest.approx(0.090 - mean)
        assert weighted["9:CF:2025"] == pytest.approx(0.0)
