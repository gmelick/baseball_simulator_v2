"""SIM-515 — the measured IBB rate table.

Three layers under test, mirroring the seams:

  * the computor build (``_build_ibb_rates``) runs against the REAL DDL text
    from ``db/schemas/02_duckdb_schema.sql`` over stub ``pg.raw`` tables — the
    numerator (play_events intent_walk rows, which carry the pre-play cell),
    the denominator (pitched PAs by their FIRST pitch's state, plus no-pitch
    IBB PAs, which have no raw.pitches rows at all), and the rate<=1 clamp;
  * the factory loader (``_load_ibb_rates``) turns the table into the
    {cell: rate} dict and caches per worker;
  * the loop consumer tests live in ``test_baseball_analyst_sim323.py``
    (``TestIntentionalWalk`` — the once-per-PA draw at the cell rate).
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from pipeline.batch.player_profile_computor import PlayerProfileComputor

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_SQL = (REPO_ROOT / "db" / "schemas" / "02_duckdb_schema.sql").read_text(encoding="utf-8")


def _real_ddl(table: str) -> str:
    start = SCHEMA_SQL.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    end = SCHEMA_SQL.index("\n);", start)
    return SCHEMA_SQL[start : end + 3]


def _conn() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    c.execute("ATTACH ':memory:' AS pg")
    c.execute("CREATE SCHEMA pg.raw")
    c.execute(
        "CREATE TABLE pg.raw.pitches ("
        "game_pk INTEGER, at_bat_number INTEGER, pitch_number INTEGER, "
        "season SMALLINT, data_quality_flag BOOLEAN, "
        "on_1b INTEGER, on_2b INTEGER, on_3b INTEGER, "
        "outs SMALLINT, inning SMALLINT, bat_score SMALLINT, fld_score SMALLINT)"
    )
    c.execute(
        "CREATE TABLE pg.raw.play_events ("
        "game_pk INTEGER, at_bat_number INTEGER, season SMALLINT, "
        "event_type VARCHAR, runners_state SMALLINT, outs_before SMALLINT, "
        "inning SMALLINT, bat_score SMALLINT, fld_score SMALLINT)"
    )
    c.execute("CREATE SCHEMA sim")
    c.execute(_real_ddl("sim.ibb_rates"))
    return c


def _pitch(c, pk, ab, n, *, on_2b=None, outs=0, inning=9, bat=3, fld=3):
    c.execute(
        "INSERT INTO pg.raw.pitches VALUES (?,?,?,2025,FALSE,NULL,?,NULL,?,?,?,?)",
        [pk, ab, n, on_2b, outs, inning, bat, fld],
    )


def _ibb(c, pk, ab, *, rs=2, outs=0, inning=9, bat=3, fld=3):
    c.execute(
        "INSERT INTO pg.raw.play_events VALUES (?,?,2025,'intent_walk',?,?,?,?,?)",
        [pk, ab, rs, outs, inning, bat, fld],
    )


def _build(c) -> None:
    comp = PlayerProfileComputor.__new__(PlayerProfileComputor)
    comp._conn = c
    comp._build_ibb_rates([2025])


def _rows(c):
    return c.execute(
        "SELECT runners_state, outs, is_late, is_close, opportunities, issued "
        "FROM sim.ibb_rates ORDER BY 1, 2, 3, 4"
    ).fetchall()


class TestTheBuilder:
    def test_a_pitched_pa_and_its_cell(self):
        # Two pitched PAs enter the (rs=2, 0 out, late, close) cell; one of
        # them is intentionally walked mid-PA (it also has pitch rows).
        c = _conn()
        _pitch(c, 1, 1, 1, on_2b=22)
        _pitch(c, 1, 1, 2, on_2b=22)  # a 2nd pitch — the PA still counts ONCE
        _pitch(c, 1, 2, 1, on_2b=22)
        _ibb(c, 1, 2)
        _build(c)
        assert _rows(c) == [(2, 0, True, True, 2, 1)]

    def test_a_no_pitch_ibb_joins_the_denominator(self):
        # The IBB PA threw no pitches (the modern no-pitch IBB): it must count
        # in BOTH the numerator and the denominator.
        c = _conn()
        _pitch(c, 1, 1, 1, on_2b=22)
        _ibb(c, 1, 2)  # no pitch rows for at-bat 2
        _build(c)
        assert _rows(c) == [(2, 0, True, True, 2, 1)]

    def test_the_first_pitch_names_the_entering_cell(self):
        # The PA's SECOND pitch has a different base state (a steal happened);
        # the cell must come from the first pitch.
        c = _conn()
        _pitch(c, 1, 1, 1, on_2b=None, outs=1, inning=3, bat=0, fld=4)
        _pitch(c, 1, 1, 2, on_2b=22, outs=1, inning=3, bat=0, fld=4)
        _build(c)
        assert _rows(c) == [(0, 1, False, False, 1, 0)]

    def test_the_late_and_close_classes(self):
        c = _conn()
        _pitch(c, 1, 1, 1, on_2b=22, inning=6, bat=0, fld=2)  # early, not close
        _pitch(c, 1, 2, 1, on_2b=22, inning=7, bat=1, fld=2)  # late, close
        _build(c)
        assert _rows(c) == [
            (2, 0, False, False, 1, 0),
            (2, 0, True, True, 1, 0),
        ]

    def test_a_rate_never_exceeds_one(self):
        # An IBB whose pre-play cell mismatches its PA's entering cell (a
        # mid-PA steal) could give issued > opportunities in the play cell;
        # GREATEST clamps opportunities up.
        c = _conn()
        _pitch(c, 1, 1, 1, on_2b=None)  # the pitched PA entered rs=0
        _ibb(c, 1, 1, rs=2)  # the play row says rs=2 at the walk
        _build(c)
        rows = {(r[0], r[1], r[2], r[3]): (r[4], r[5]) for r in _rows(c)}
        opp, issued = rows[(2, 0, True, True)]
        assert issued <= opp

    def test_quality_flagged_pitches_are_excluded(self):
        c = _conn()
        c.execute("INSERT INTO pg.raw.pitches VALUES (1,1,1,2025,TRUE,NULL,22,NULL,0,9,3,3)")
        _build(c)
        assert _rows(c) == []


class TestTheLoader:
    def test_the_loader_builds_the_rate_dict(self, monkeypatch, tmp_path):
        import simulation.production_factory as pf

        c = duckdb.connect(str(tmp_path / "t.duckdb"))
        c.execute("CREATE SCHEMA sim")
        c.execute(_real_ddl("sim.ibb_rates"))
        c.execute("INSERT INTO sim.ibb_rates VALUES (2, 0, TRUE, TRUE, 200, 30)")
        c.execute("INSERT INTO sim.ibb_rates VALUES (4, 2, TRUE, TRUE, 100, 0)")
        c.close()
        monkeypatch.setenv("BASEBALL_DUCKDB_PATH", str(tmp_path / "t.duckdb"))
        pf.reset_caches()
        try:
            rates = pf._load_ibb_rates()
            assert rates == {(2, 0, True, True): 0.15, (4, 2, True, True): 0.0}
        finally:
            pf.reset_caches()

    def test_a_missing_table_loads_none(self, monkeypatch, tmp_path):
        import simulation.production_factory as pf

        c = duckdb.connect(str(tmp_path / "empty.duckdb"))
        c.close()
        monkeypatch.setenv("BASEBALL_DUCKDB_PATH", str(tmp_path / "empty.duckdb"))
        pf.reset_caches()
        try:
            assert pf._load_ibb_rates() is None
        finally:
            pf.reset_caches()
