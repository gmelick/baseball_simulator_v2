"""SIM-554 — the data under the running game on the pitch.

The steal draw moves after the pitch and reads the class of the pitch that
came out; the pickoff draw stays before the pitch. Both read the steal
opportunity pool, which gains two columns in migration 0031:

  * ``pitch_class`` — the pitch pool's ``outcome_type`` for the pitch the row
    rode (NULL on a pickoff row);
  * ``is_pickoff_row`` — TRUE on a row that is a pickoff outcome thrown before
    any pitch of its pair in the plate appearance. Measured 2026-09-30, 22% of
    the pickoff outcomes that fit a pair had no such pitch, so the pool
    dropped them. The builder now writes each one as a row of its own.

The layers, in the order the run book meets them (design §4, tests 22-24):

  * the migration and the version file;
  * the builder (``_build_steal_opportunity_pool``) over an in-memory DuckDB
    that uses the REAL table DDL from ``db/schemas/02_duckdb_schema.sql``;
  * the guard that stops the builder on a table without the two columns;
  * the export (``build_steal_pool_artifact``) and the loader
    (``EngineArtifacts.load``), including the shared views.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
from pathlib import Path

import duckdb
import numpy as np
import pytest

from pipeline.batch.engine_artifacts import (
    STEAL_PITCH_CLASS_CODE,
    STEAL_PITCH_CLASSES,
    EngineArtifacts,
    build_steal_pool_artifact,
    steal_pitch_class_codes,
)
from pipeline.batch.player_profile_computor import PlayerProfileComputor, recency_weight
from simulation.game_state import PITCH_OUTCOMES

REPO = Path(__file__).resolve().parents[2]
SCHEMA_SQL = REPO / "db" / "schemas" / "02_duckdb_schema.sql"
VERSION_FILE = REPO / "db" / "schemas" / "duckdb_schema_version.txt"
MIGRATIONS = REPO / "db" / "migrations" / "duckdb"
MIGRATION_0031 = MIGRATIONS / "0031_sim554_steal_pool_pitch_class.sql"
TABLE = "sim.steal_opportunity_pool"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _real_ddl() -> str:
    text = SCHEMA_SQL.read_text(encoding="utf-8")
    start = text.index(f"CREATE TABLE IF NOT EXISTS {TABLE} (")
    end = text.index("\n);", start)
    return text[start : end + 3]


def _v30_ddl() -> str:
    """The table as it stood before migration 0031: the real DDL less the
    SIM-554 block."""
    ddl = _real_ddl()
    start = ddl.index("    -- SIM-554 (migration 0031)")
    end = ddl.index("\n    PRIMARY KEY")
    return ddl[:start] + ddl[end + 1 :]


def _ddl_columns(ddl: str) -> list[str]:
    c = duckdb.connect(":memory:")
    c.execute("CREATE SCHEMA sim")
    c.execute(ddl)
    return [r[0] for r in c.execute(f"DESCRIBE {TABLE}").fetchall()]


def _migration_statements() -> list[str]:
    """Split the migration the way the rebuild scripts do: drop every line's
    double-dash tail, then split on semicolons."""
    body = "\n".join(
        line.split("--", 1)[0] for line in MIGRATION_0031.read_text(encoding="utf-8").splitlines()
    )
    return [s.strip() for s in body.split(";") if s.strip()]


class _Recorder:
    """A connection that records every statement the builder sends."""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self.con = con
        self.sql: list[str] = []

    def execute(self, sql: str, *args):
        self.sql.append(sql)
        return self.con.execute(sql, *args)


class _Db:
    """An in-memory DuckDB with the tables the steal-pool builder reads.

    ``pg.raw.pitches`` and ``pg.raw.play_events`` carry the columns the
    builder selects; the pool itself is the real DDL (or the pre-0031 shape).
    """

    def __init__(self, *, ddl: str | None = None, play_events: bool = True) -> None:
        c = duckdb.connect(":memory:")
        c.execute("ATTACH ':memory:' AS pg")
        c.execute("CREATE SCHEMA pg.raw")
        c.execute(
            "CREATE TABLE pg.raw.pitches ("
            "game_pk INTEGER, at_bat_number INTEGER, pitch_number INTEGER, "
            "game_date DATE, season SMALLINT, pitcher INTEGER, fielder_2 INTEGER, "
            "inning SMALLINT, inning_topbot VARCHAR, outs SMALLINT, balls SMALLINT, "
            "strikes SMALLINT, bat_score SMALLINT, fld_score SMALLINT, "
            "on_1b INTEGER, on_2b INTEGER, on_3b INTEGER, "
            "sb_attempt_2b BOOLEAN, sb_attempt_3b BOOLEAN, "
            "sb_success_2b BOOLEAN, sb_success_3b BOOLEAN, "
            "data_quality_flag BOOLEAN, events VARCHAR)"
        )
        if play_events:
            c.execute(
                "CREATE TABLE pg.raw.play_events ("
                "id BIGINT, game_pk INTEGER, at_bat_number INTEGER, game_date DATE, "
                "season SMALLINT, event_type VARCHAR, inning SMALLINT, "
                "inning_topbot VARCHAR, outs_before SMALLINT, runners_state SMALLINT, "
                "bat_score SMALLINT, fld_score SMALLINT, pitcher_id INTEGER, "
                "runner_id INTEGER, base SMALLINT, is_out BOOLEAN)"
            )
        c.execute("CREATE SCHEMA sim")
        c.execute(
            "CREATE TABLE sim.pitch_pool ("
            "pitch_id BIGINT, game_pk INTEGER, at_bat_number INTEGER, "
            "pitch_number INTEGER, outcome_type VARCHAR)"
        )
        c.execute(ddl if ddl is not None else _real_ddl())
        c.execute(
            "CREATE TABLE sim.pool_build_metadata ("
            "pool_name VARCHAR, season SMALLINT, row_count BIGINT, "
            "source_max_game_date DATE, source_row_count BIGINT, "
            "recency_ref_season SMALLINT, builder_version VARCHAR, "
            "built_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY (pool_name, season))"
        )
        self.c = c
        self._next_event_id = 100

    def pitch(
        self,
        pk: int,
        ab: int,
        pn: int,
        *,
        season: int = 2024,
        inning: int = 3,
        half: str = "Top",
        pitcher: int | None = 901,
        catcher: int = 902,
        on_1b: int | None = None,
        on_2b: int | None = None,
        on_3b: int | None = None,
        outs: int = 0,
        balls: int = 0,
        strikes: int = 0,
        bat: int = 3,
        fld: int = 1,
        att2: bool = False,
        suc2: bool = False,
        pitch_class: str = "ball",
    ) -> int:
        pid = pk * 10000 + ab * 100 + pn
        self.c.execute(
            "INSERT INTO pg.raw.pitches VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,FALSE,?,FALSE,FALSE,NULL)",
            [
                pk,
                ab,
                pn,
                f"{season}-06-01",
                season,
                pitcher,
                catcher,
                inning,
                half,
                outs,
                balls,
                strikes,
                bat,
                fld,
                on_1b,
                on_2b,
                on_3b,
                att2,
                suc2,
            ],
        )
        self.c.execute(
            "INSERT INTO sim.pitch_pool VALUES (?,?,?,?,?)", [pid, pk, ab, pn, pitch_class]
        )
        return pid

    def event(
        self,
        pk: int,
        ab: int,
        *,
        season: int = 2024,
        event_type: str = "pickoff",
        is_out: bool = True,
        base: int = 1,
        runners_state: int = 1,
        runner_id: int | None = 11,
        pitcher_id: int | None = 977,
        inning: int = 3,
        half: str = "Top",
        outs_before: int = 1,
        bat: int = 4,
        fld: int = 2,
    ) -> int:
        self._next_event_id += 1
        eid = self._next_event_id
        self.c.execute(
            "INSERT INTO pg.raw.play_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                eid,
                pk,
                ab,
                f"{season}-06-01",
                season,
                event_type,
                inning,
                half,
                outs_before,
                runners_state,
                bat,
                fld,
                pitcher_id,
                runner_id,
                base,
                is_out,
            ],
        )
        return eid

    def build(self, seasons: list[int] | None = None, *, recorder: _Recorder | None = None):
        comp = PlayerProfileComputor.__new__(PlayerProfileComputor)
        comp._conn = recorder if recorder is not None else self.c
        comp._build_steal_opportunity_pool(seasons or [2024], incremental=False)

    def pickoff_rows(self) -> list[dict]:
        cur = self.c.execute(f"SELECT * FROM {TABLE} WHERE is_pickoff_row ORDER BY pitch_id DESC")
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]

    def count(self, where: str = "TRUE") -> int:
        return self.c.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE {where}").fetchone()[0]


# ---------------------------------------------------------------------------
# 1. The migration and the version file
# ---------------------------------------------------------------------------


class TestMigration0031:
    def test_it_adds_exactly_the_two_columns_in_order(self):
        sql = MIGRATION_0031.read_text(encoding="utf-8")
        added = re.findall(r"^ALTER TABLE (\S+) ADD COLUMN IF NOT EXISTS (\S+)", sql, re.M)
        assert added == [(TABLE, "pitch_class"), (TABLE, "is_pickoff_row")]
        statements = _migration_statements()
        assert len(statements) == 2
        assert all(s.startswith("ALTER TABLE") and "DROP" not in s.upper() for s in statements)
        assert sql.startswith("-- 0031 — SIM-554:")
        assert "(schema v30 -> v31)" in sql.splitlines()[0]

    def test_no_statement_carries_a_double_dash(self):
        """The rebuild scripts drop every line's double-dash tail before they
        split on semicolons, so a double dash inside a statement would cut it."""
        for line in MIGRATION_0031.read_text(encoding="utf-8").splitlines():
            code = line.strip()
            if code and not code.startswith("--"):
                assert "--" not in code, line

    def test_the_old_shape_gains_the_columns_last_with_neutral_values(self):
        c = duckdb.connect(":memory:")
        c.execute("CREATE SCHEMA sim")
        c.execute(_v30_ddl())
        c.execute(
            f"INSERT INTO {TABLE} (pitch_id, game_pk, at_bat_number, pitch_number, game_date, "
            "season, runner_id, pitcher_id, target_base, inning, outs, count_balls, "
            "count_strikes, score_diff, attempted) "
            "VALUES (1, 1, 1, 1, DATE '2024-06-01', 2024, 11, 901, 2, 1, 0, 0, 0, 0, FALSE)"
        )
        for _ in range(2):  # the second pass proves the migration idempotent
            for stmt in _migration_statements():
                c.execute(stmt)
        cols = [r[0] for r in c.execute(f"DESCRIBE {TABLE}").fetchall()]
        assert cols == _ddl_columns(_real_ddl())
        assert cols[-3:] == ["pickoff_error", "pitch_class", "is_pickoff_row"]
        assert c.execute(f"SELECT pitch_class, is_pickoff_row FROM {TABLE}").fetchall() == [
            (None, False)
        ]

    def test_the_version_file_equals_the_newest_migration(self):
        newest = max(int(p.name[:4]) for p in MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))
        assert newest == 31
        assert int(VERSION_FILE.read_text(encoding="utf-8").strip()) == newest


# ---------------------------------------------------------------------------
# 2. The builder's SELECT matches the DDL (design test 22)
# ---------------------------------------------------------------------------


class TestTheSelectMatchesTheDdl:
    def test_the_ddl_ends_with_the_two_columns(self):
        cols = _ddl_columns(_real_ddl())
        assert cols[-2:] == ["pitch_class", "is_pickoff_row"]
        assert len(cols) == 23

    def test_both_branches_append_the_two_columns_last(self):
        """The INSERT is positional. Each branch of the SELECT names the two
        new columns after the pickoff labels and before its FROM."""
        import inspect

        src = inspect.getsource(PlayerProfileComputor._build_steal_opportunity_pool)
        union = src.rindex("UNION ALL")

        def order(text: str, *patterns: str) -> list[int]:
            return [re.search(p, text).start() for p in patterns]

        pitch_branch = src[src.rindex("AS pickoff_error", 0, union) : union]
        got = order(pitch_branch, r"f\.pitch_class,", r"FALSE\s+AS is_pickoff_row", r"FROM f\s")
        assert got == sorted(got)
        pickoff_branch = src[src.index("AS pickoff_error", union) :]
        got = order(
            pickoff_branch,
            r"\s+AS pitch_class,",
            r"TRUE\s+AS is_pickoff_row",
            r"FROM pickoff_events",
        )
        assert got == sorted(got)

    def test_the_executed_select_names_every_ddl_column_in_order(self):
        db = _Db()
        db.pitch(1, 1, 1, on_1b=11)
        rec = _Recorder(db.c)
        db.build(recorder=rec)
        insert = next(s for s in rec.sql if s.strip().startswith(f"INSERT INTO {TABLE}"))
        body = insert.strip()[len(f"INSERT INTO {TABLE}") :]
        names = [d[0] for d in db.c.execute(body).description]
        assert names == _ddl_columns(_real_ddl())


# ---------------------------------------------------------------------------
# 3. The builder: the class and the pickoff rows (design test 23)
# ---------------------------------------------------------------------------


class TestThePitchClass:
    def test_pitch_rows_carry_their_pitch_pool_class(self):
        db = _Db()
        db.pitch(1, 1, 1, on_1b=11, pitch_class="ball")
        db.pitch(1, 1, 2, on_1b=11, pitch_class="foul", strikes=0, balls=1)
        db.pitch(1, 1, 3, on_1b=11, pitch_class="in_play", strikes=1, balls=1)
        db.build()
        rows = db.c.execute(
            f"SELECT pitch_number, pitch_class, is_pickoff_row FROM {TABLE} ORDER BY pitch_number"
        ).fetchall()
        assert rows == [(1, "ball", False), (2, "foul", False), (3, "in_play", False)]


class TestThePickoffRows:
    def test_a_pickoff_before_any_pitch_of_its_pair_is_one_pickoff_row(self):
        """Runner 11 on first, one out: the pitcher throws over and picks him
        off before the first pitch. The rest of the plate appearance has the
        bases empty, so no pitch of the pair exists and the pool used to drop
        the outcome."""
        db = _Db()
        db.pitch(7, 4, 1, catcher=904, outs=1)  # the previous batter, bases empty
        db.pitch(7, 5, 1, catcher=905, outs=2)  # after the pickoff: bases empty
        db.pitch(7, 5, 2, catcher=995, outs=2)
        eid = db.event(7, 5, outs_before=1, bat=4, fld=2)
        db.build()
        rows = db.pickoff_rows()
        assert len(rows) == 1
        row = rows[0]
        assert row == {
            "pitch_id": -eid,
            "game_pk": 7,
            "at_bat_number": 5,
            "pitch_number": 0,
            "game_date": datetime.date(2024, 6, 1),
            "season": 2024,
            "runner_id": 11,
            "pitcher_id": 977,  # the thrower, from the play record
            "catcher_id": 905,  # the nearest pitch: this PA's first
            "target_base": 2,
            "inning": 3,
            "outs": 1,  # the outs before the throw
            "count_balls": 0,
            "count_strikes": 0,
            "score_diff": 2,
            "attempted": False,
            "success": False,
            "recency_weight": recency_weight(2024, 2024),
            "pickoff_out": True,
            "pickoff_advancing": False,
            "pickoff_error": False,
            "pitch_class": None,
            "is_pickoff_row": True,
        }
        assert db.count("NOT is_pickoff_row") == 0  # no pitch had a runner on

    def test_an_outcome_with_a_pitch_of_its_pair_keeps_its_tag_and_makes_no_row(self):
        db = _Db()
        db.pitch(1, 1, 1, on_1b=11)
        db.pitch(1, 1, 2, on_1b=11)
        db.event(1, 1, outs_before=0)
        db.build()
        assert db.count("is_pickoff_row") == 0
        tagged = db.c.execute(
            f"SELECT pitch_number FROM {TABLE} WHERE pickoff_out ORDER BY pitch_number"
        ).fetchall()
        assert tagged == [(1,)]

    def test_a_throw_with_no_outcome_makes_no_row(self):
        """The play record names the runner on a throw that moved no one; it is
        neither an out nor an error, so it is not a pickoff outcome."""
        db = _Db()
        db.pitch(1, 2, 1, outs=1)
        db.event(1, 2, is_out=False)
        db.build()
        assert db.count() == 0

    @pytest.mark.parametrize(
        ("base", "runners_state"),
        [
            (3, 4),  # a runner held at third: no pair
            (1, 3),  # a throw to first with first and second occupied
        ],
    )
    def test_an_outcome_with_no_pair_makes_no_row(self, base, runners_state):
        db = _Db()
        db.pitch(1, 2, 1, outs=1)
        db.event(1, 2, base=base, runners_state=runners_state)
        db.build()
        assert db.count("is_pickoff_row") == 0

    @pytest.mark.parametrize("missing", ["runner_id", "pitcher_id"])
    def test_an_outcome_without_a_runner_or_a_thrower_makes_no_row(self, missing):
        db = _Db()
        db.pitch(1, 2, 1, outs=1)
        db.event(1, 2, **{missing: None})
        db.build()
        assert db.count("is_pickoff_row") == 0

    def test_a_pitch_the_pool_leaves_out_does_not_hide_the_outcome(self):
        """The pair's only pitch has no pitcher id, so the pool keeps no row for
        it. The outcome then has no row to ride and becomes a pickoff row."""
        db = _Db()
        db.pitch(1, 2, 1, on_1b=11, pitcher=None)
        db.event(1, 2, outs_before=0)
        db.build()
        assert db.count("NOT is_pickoff_row") == 0
        assert db.count("is_pickoff_row") == 1

    def test_the_labels_of_an_error_and_a_caught_stealing(self):
        db = _Db()
        db.pitch(1, 3, 1, outs=1)
        db.pitch(1, 4, 1, outs=2)
        db.event(1, 3, event_type="pickoff_error", is_out=False)
        db.event(1, 4, base=2, runners_state=1, outs_before=1)  # tagged at second
        db.build()
        got = db.c.execute(
            f"SELECT at_bat_number, pickoff_out, pickoff_advancing, pickoff_error "
            f"FROM {TABLE} WHERE is_pickoff_row ORDER BY at_bat_number"
        ).fetchall()
        assert got == [(3, False, False, True), (4, True, True, False)]

    def test_a_runner_on_second_maps_to_the_third_base_pair(self):
        db = _Db()
        db.pitch(1, 3, 1, outs=2)
        db.event(1, 3, base=2, runners_state=2, runner_id=22)
        db.build()
        (row,) = db.pickoff_rows()
        assert (row["target_base"], row["runner_id"], row["pickoff_advancing"]) == (3, 22, False)

    def test_the_score_margin_is_clamped_like_the_pitch_rows(self):
        db = _Db()
        db.pitch(1, 3, 1, outs=2)
        db.event(1, 3, bat=0, fld=9)
        db.build()
        assert db.pickoff_rows()[0]["score_diff"] == -5

    def test_the_recency_weight_follows_the_season(self):
        db = _Db()
        db.pitch(1, 3, 1, season=2022, outs=2)
        db.pitch(2, 3, 1, season=2024, outs=2)
        db.event(1, 3, season=2022)
        db.event(2, 3, season=2024)
        db.build([2022, 2024])
        got = db.c.execute(
            f"SELECT season, recency_weight FROM {TABLE} WHERE is_pickoff_row ORDER BY season"
        ).fetchall()
        assert got == [(2022, recency_weight(2022, 2024)), (2024, recency_weight(2024, 2024))]
        assert got[0][1] < got[1][1]

    def test_a_rebuild_replaces_the_pickoff_rows(self):
        db = _Db()
        db.pitch(1, 3, 1, outs=2)
        db.event(1, 3)
        db.build()
        db.build()
        assert db.count("is_pickoff_row") == 1

    def test_the_build_metadata_counts_the_pickoff_rows(self):
        db = _Db()
        db.pitch(1, 1, 1, on_1b=11)
        db.pitch(1, 3, 1, outs=2)
        db.event(1, 3)
        db.build()
        got = db.c.execute("SELECT row_count FROM sim.pool_build_metadata").fetchone()
        assert got == (2,)

    def test_no_play_records_means_no_pickoff_rows(self):
        db = _Db(play_events=False)
        db.pitch(1, 1, 1, on_1b=11)
        db.build()
        assert db.count() == 1 and db.count("is_pickoff_row") == 0


class TestThePickoffRowCatcher:
    """The play record names no catcher. The row takes the catcher of the
    nearest pitch of the same half-inning; with none, the catcher is unknown
    and the sampler's catcher weight is neutral."""

    def test_the_nearest_plate_appearance_of_the_half_inning(self):
        db = _Db()
        db.pitch(7, 3, 1, catcher=903, outs=0)
        db.pitch(7, 6, 1, catcher=906, outs=2)
        db.pitch(7, 5, 1, catcher=999, half="Bot")  # the other half
        db.pitch(7, 5, 2, catcher=998, inning=4)  # another inning
        db.event(7, 5, outs_before=2)  # a third out: PA 5 has no pitch at all
        db.build()
        assert db.pickoff_rows()[0]["catcher_id"] == 906

    def test_a_tie_goes_to_the_earlier_plate_appearance_and_its_first_pitch(self):
        db = _Db()
        db.pitch(7, 4, 2, catcher=942, outs=1)
        db.pitch(7, 4, 1, catcher=941, outs=1)
        db.pitch(7, 6, 1, catcher=906, outs=2)
        db.event(7, 5, outs_before=1)
        db.build()
        assert db.pickoff_rows()[0]["catcher_id"] == 941

    def test_no_pitch_in_the_half_inning_leaves_the_catcher_null(self):
        db = _Db()
        db.pitch(7, 5, 1, catcher=999, half="Bot")
        db.event(7, 5, outs_before=2)
        db.build()
        (row,) = db.pickoff_rows()
        assert row["catcher_id"] is None


# ---------------------------------------------------------------------------
# 4. The guard: no rebuild on an un-migrated table
# ---------------------------------------------------------------------------


class TestTheGuard:
    def test_an_unmigrated_table_stops_the_build_before_the_delete(self):
        db = _Db(ddl=_v30_ddl())
        db.c.execute(
            f"INSERT INTO {TABLE} (pitch_id, game_pk, at_bat_number, pitch_number, game_date, "
            "season, runner_id, pitcher_id, target_base, inning, outs, count_balls, "
            "count_strikes, score_diff, attempted) "
            "VALUES (1, 1, 1, 1, DATE '2024-06-01', 2024, 11, 901, 2, 1, 0, 0, 0, 0, FALSE)"
        )
        db.pitch(1, 1, 1, on_1b=11)
        with pytest.raises(RuntimeError, match="0031_sim554_steal_pool_pitch_class.sql"):
            db.build()
        assert db.count() == 1

    def test_a_half_migrated_table_also_stops(self):
        db = _Db(ddl=_v30_ddl())
        db.c.execute(f"ALTER TABLE {TABLE} ADD COLUMN pitch_class VARCHAR(20)")
        with pytest.raises(RuntimeError, match="is_pickoff_row"):
            db.build()

    def test_no_table_has_no_rows_to_guard(self):
        """With no table there is nothing to lose. The probe passes and the
        DELETE fails on the missing table by itself."""
        db = _Db()
        db.c.execute(f"DROP TABLE {TABLE}")
        with pytest.raises(duckdb.CatalogException):
            db.build()


# ---------------------------------------------------------------------------
# 5. The export and the loader (design test 24)
# ---------------------------------------------------------------------------


def _minimal_pitch_pool(art_dir: Path) -> None:
    """load() requires the PITCH pool's files for both hands; zero rows do."""
    d = art_dir / "pitch_pool"
    os.makedirs(d, exist_ok=True)
    with open(d / "manifest.json", "w", encoding="utf-8") as fh:
        json.dump({"seasons": [2024], "counts": {"L": 0, "R": 0}}, fh)
    con = duckdb.connect(":memory:")
    for hand in ("L", "R"):
        np.save(d / f"{hand}.geom.npy", np.zeros((0, 3), dtype=np.float32))
        np.save(d / f"{hand}.sit.npy", np.zeros((0, 6), dtype=np.float32))
        con.execute(
            "COPY (SELECT 0::BIGINT AS pitch_id, 0::BIGINT AS pitcher_id, "
            "0::BIGINT AS batter_id, 0::BIGINT AS season, ''::VARCHAR AS outcome_type, "
            "1.0::FLOAT AS recency_weight WHERE 1=0) "
            f"TO '{(d / f'{hand}.meta.parquet').as_posix()}' (FORMAT parquet)"
        )
    con.close()


def _steal_bundle(art_dir: Path, classes: list[str] | None) -> None:
    """A hand-written steal_pool/ directory: one row per entry of ``classes``
    in the "2" pool (None = a parquet without the class column), none in "3"."""
    d = art_dir / "steal_pool"
    os.makedirs(d, exist_ok=True)
    con = duckdb.connect(":memory:")
    for target, names in (("2", classes or ["ball"]), ("3", [])):
        n = len(names)
        np.save(d / f"{target}.sit.npy", np.zeros((n, 4), dtype=np.float32))
        rows = ", ".join(f"({i}, '{name}')" for i, name in enumerate(names)) or "(0, '')"
        cls_col = ", v.c AS pitch_class" if classes is not None else ""
        con.execute(
            "COPY (SELECT 11::INTEGER AS runner_id, 901::INTEGER AS pitcher_id, "
            "902::INTEGER AS catcher_id, 2024::SMALLINT AS season, FALSE AS attempted, "
            "FALSE AS success, 1.0::DOUBLE AS recency_weight, FALSE AS pickoff_out, "
            "FALSE AS pickoff_advancing, FALSE AS pickoff_error"
            f"{cls_col} FROM (VALUES {rows}) v(i, c) "
            f"WHERE {'TRUE' if n else 'FALSE'} ORDER BY v.i) "
            f"TO '{(d / f'{target}.meta.parquet').as_posix()}' (FORMAT parquet)"
        )
    with open(d / "manifest.json", "w", encoding="utf-8") as fh:
        json.dump({"seasons": [2024], "counts": {"2": len(classes or ["ball"]), "3": 0}}, fh)
    con.close()


def _export_db() -> _Db:
    """Three pitch rows at distinct counts and one pickoff row, all target 2."""
    db = _Db()
    db.pitch(3, 1, 1, on_1b=11, balls=0, strikes=0, pitch_class="called_strike")
    db.pitch(3, 1, 2, on_1b=11, balls=0, strikes=1, pitch_class="ball")
    db.pitch(1, 1, 1, on_1b=12, balls=2, strikes=2, outs=1, pitch_class="swinging_strike")
    db.pitch(5, 3, 1, outs=2)  # bases empty: the pickoff below has no pitch of its pair
    db.event(5, 3, outs_before=1)
    db.build()
    return db


class TestTheExport:
    def test_the_meta_carries_the_class_and_the_flag_in_pitch_id_order(self, tmp_path):
        db = _export_db()
        counts = build_steal_pool_artifact(db.c, str(tmp_path), [2024])
        assert counts == {"2": 4, "3": 0}
        meta = (tmp_path / "steal_pool" / "2.meta.parquet").as_posix()
        got = db.c.execute(
            f"SELECT pitch_id, pitch_class, is_pickoff_row FROM read_parquet('{meta}')"
        ).fetchall()
        ids = [r[0] for r in got]
        assert ids == sorted(ids) and ids[0] < 0  # the pickoff row first
        assert [r[1:] for r in got] == [
            ("", True),
            ("swinging_strike", False),
            ("called_strike", False),
            ("ball", False),
        ]

    def test_the_sit_rows_align_with_the_meta_rows(self, tmp_path):
        db = _export_db()
        build_steal_pool_artifact(db.c, str(tmp_path), [2024])
        sit = np.load(tmp_path / "steal_pool" / "2.sit.npy")
        meta = (tmp_path / "steal_pool" / "2.meta.parquet").as_posix()
        ids = [
            r[0] for r in db.c.execute(f"SELECT pitch_id FROM read_parquet('{meta}')").fetchall()
        ]
        assert sit.shape == (len(ids), 4)
        for i, pid in enumerate(ids):
            want = db.c.execute(
                f"SELECT count_balls, count_strikes, outs, score_diff FROM {TABLE} "
                "WHERE pitch_id = ?",
                [pid],
            ).fetchone()
            assert tuple(sit[i].tolist()) == tuple(float(v) for v in want), pid

    def test_a_pre_0031_table_fails_the_export(self, tmp_path):
        """The migration precedes the export in the run book. An export from
        an un-migrated table must fail loudly, not ship a bundle without
        classes that the loop would read as the old order."""
        db = _Db(ddl=_v30_ddl())
        with pytest.raises(RuntimeError, match="0031_sim554_steal_pool_pitch_class"):
            build_steal_pool_artifact(db.c, str(tmp_path), [2024])
        assert not (tmp_path / "steal_pool").exists()  # refused before any write

    def test_a_refused_export_leaves_the_old_pool_byte_identical(self, tmp_path):
        """The half-write case the review found: the sit query works on an old
        table, so a refusal after it would leave a new sit.npy beside an old
        parquet."""
        build_steal_pool_artifact(_export_db().c, str(tmp_path), [2024])
        pool = tmp_path / "steal_pool"
        before = {f.name: f.read_bytes() for f in pool.iterdir()}
        with pytest.raises(RuntimeError):
            build_steal_pool_artifact(_Db(ddl=_v30_ddl()).c, str(tmp_path), [2024])
        assert {f.name: f.read_bytes() for f in pool.iterdir()} == before

    def test_a_failure_mid_export_leaves_the_old_pool_and_no_stray(self, tmp_path, monkeypatch):
        build_steal_pool_artifact(_export_db().c, str(tmp_path), [2024])
        pool = tmp_path / "steal_pool"
        before = {f.name: f.read_bytes() for f in pool.iterdir()}
        db = _export_db()
        db.c.execute(f"UPDATE {TABLE} SET outs = 2 WHERE pitch_id > 0")  # a different pool
        calls = {"n": 0}
        real = np.save

        def save_then_fail(path, arr, *a, **k):
            calls["n"] += 1
            real(path, arr, *a, **k)
            if calls["n"] == 2:  # target 3's sit file: target 2 is staged already
                raise OSError("disk full")

        monkeypatch.setattr(np, "save", save_then_fail)
        with pytest.raises(OSError, match="disk full"):
            build_steal_pool_artifact(db.c, str(tmp_path), [2024])
        assert {f.name: f.read_bytes() for f in pool.iterdir()} == before

    def test_a_season_no_builder_reclassed_is_logged(self, tmp_path, caplog):
        """A migrated table whose season an old builder wrote: the pitch rows
        hold no class, the loader reads them as code 0, and the steal draw
        never picks them. The export finishes and names the count."""
        db = _export_db()
        db.c.execute(f"UPDATE {TABLE} SET pitch_class = NULL WHERE pitch_number = 2")
        with caplog.at_level(logging.WARNING, logger="engine_artifacts"):
            build_steal_pool_artifact(db.c, str(tmp_path), [2024])
        assert "steal_pool[2]: 1 pitch rows have no pitch class" in caplog.text
        assert "steal_pool[3]" not in caplog.text

    def test_a_fully_classed_pool_logs_no_warning(self, tmp_path, caplog):
        db = _export_db()
        with caplog.at_level(logging.WARNING, logger="engine_artifacts"):
            build_steal_pool_artifact(db.c, str(tmp_path), [2024])
        assert "no pitch class" not in caplog.text


class TestTheLoader:
    def test_the_vocabulary_is_the_loops(self):
        assert STEAL_PITCH_CLASSES == PITCH_OUTCOMES
        assert [STEAL_PITCH_CLASS_CODE[c] for c in PITCH_OUTCOMES] == [1, 2, 3, 4, 5, 6]

    def test_a_null_class_reads_zero(self):
        """The export writes '' for a NULL class; a parquet written another way
        may still hold a NULL, plain or masked."""
        plain = np.array(["foul", None, "ball"], dtype=object)
        assert steal_pitch_class_codes(plain).tolist() == [4, 0, 1]
        masked = np.ma.masked_array(np.array(["foul", "x", "ball"], dtype=object), [0, 1, 0])
        assert steal_pitch_class_codes(masked).tolist() == [4, 0, 1]
        assert steal_pitch_class_codes(np.array([], dtype=object)).dtype == np.int8

    def test_the_six_classes_map_to_codes_and_a_pickoff_row_to_zero(self, tmp_path):
        _steal_bundle(tmp_path, [*PITCH_OUTCOMES, ""])
        _minimal_pitch_pool(tmp_path)
        art = EngineArtifacts.load(str(tmp_path))
        pc = art.steal_pools["2"].pitch_class
        assert pc is not None and pc.dtype == np.int8
        assert pc.tolist() == [1, 2, 3, 4, 5, 6, 0]
        assert art.steal_pools["3"].pitch_class is not None
        assert art.steal_pools["3"].pitch_class.size == 0

    def test_an_unknown_class_reads_zero_and_is_logged(self, tmp_path, caplog):
        _steal_bundle(tmp_path, ["ball", "pitchout", "foul"])
        _minimal_pitch_pool(tmp_path)
        with caplog.at_level(logging.WARNING, logger="engine_artifacts"):
            art = EngineArtifacts.load(str(tmp_path))
        assert art.steal_pools["2"].pitch_class.tolist() == [1, 0, 4]
        assert "pitchout" in caplog.text

    def test_a_torn_pool_refuses_to_load(self, tmp_path):
        """The sit rows and the meta rows come from two writes; a pair whose
        row counts differ is refused, never paired row by row."""
        _steal_bundle(tmp_path, ["ball", "foul"])
        _minimal_pitch_pool(tmp_path)
        np.save(tmp_path / "steal_pool" / "2.sit.npy", np.zeros((3, 4), dtype=np.float32))
        with pytest.raises(ValueError, match="re-export the steal pool"):
            EngineArtifacts.load(str(tmp_path))

    def test_a_pre_0031_bundle_has_no_classes(self, tmp_path):
        _steal_bundle(tmp_path, None)
        _minimal_pitch_pool(tmp_path)
        art = EngineArtifacts.load(str(tmp_path))
        assert art.steal_pools["2"].pitch_class is None
        assert art.steal_pools["2"].is_pickoff_row is None
        assert "steal_pool.2.pitch_class" not in art.extract_shared_arrays()

    def test_the_class_survives_the_shared_views(self, tmp_path):
        _steal_bundle(tmp_path, ["ball", "foul"])
        _minimal_pitch_pool(tmp_path)
        art = EngineArtifacts.load(str(tmp_path))
        shared = art.extract_shared_arrays()
        assert shared["steal_pool.2.pitch_class"].tolist() == [1, 4]
        view = np.array([3, 3], dtype=np.int8)
        art.attach_shared_views({"steal_pool.2.pitch_class": view})
        assert art.steal_pools["2"].pitch_class is view
        # A worker that loads with the shared views takes the view, not the disk.
        again = EngineArtifacts.load(str(tmp_path), shared_views={"steal_pool.2.pitch_class": view})
        assert again.steal_pools["2"].pitch_class is view

    def test_the_round_trip_from_the_builder(self, tmp_path):
        db = _export_db()
        build_steal_pool_artifact(db.c, str(tmp_path), [2024])
        _minimal_pitch_pool(tmp_path)
        art = EngineArtifacts.load(str(tmp_path))
        p2 = art.steal_pools["2"]
        assert p2.pitch_class.tolist() == [0, 3, 2, 1]
        assert p2.is_pickoff_row is not None and p2.is_pickoff_row.tolist() == [1, 0, 0, 0]
        assert "steal_pool.2.is_pickoff_row" in art.extract_shared_arrays()
        assert p2.pickoff_out.tolist() == [1, 0, 0, 0]
        assert p2.runner_id.tolist() == [11, 12, 11, 11]
