"""SIM-506 — the steal labels read BOTH homes of a steal outcome.

A steal outcome lives in two disjoint places in ``raw.pitches``: the
``sb_attempt_*``/``sb_success_*`` columns (a mid-PA steal) and the ``events``
column (a steal that ENDS the plate appearance — ``caught_stealing_2b`` etc.,
with the columns FALSE). Measured overlap: exactly zero. A caught stealing
ends a PA routinely (2024, 2B: 330 column CS + 249 event-only CS) while a
successful steal almost never does (3 event SB against 2,773 column SB), so a
columns-only consumer inflates every steal SUCCESS rate ~5-7 points. That
defect shipped in the opportunity pool and every steal-feature builder.

Two layers here:

  * the ``sql_steal_attempt``/``sql_steal_success`` helpers — the single
    canonical definition every builder must use;
  * ``_build_pitcher_steal_metrics`` over an in-memory DuckDB emulating
    ``pg.raw.pitches`` + ``pg.raw.play_events`` — the event-CS attempt count
    (SIM-506) and the SIM-504 item 3 disengagement rates.

The opportunity-pool builder's event-label rows are covered beside its other
seams in ``test_sim474_steal_draw.py``.
"""

from __future__ import annotations

import duckdb
import pytest

from pipeline.batch.player_profile_computor import PlayerProfileComputor
from pipeline.statcast_events import sql_steal_attempt, sql_steal_success

# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


class TestTheLabelHelpers:
    def test_attempt_reads_both_homes(self):
        sql = sql_steal_attempt("2b")
        assert "sb_attempt_2b" in sql
        assert "caught_stealing_2b" in sql
        assert "stolen_base_2b" in sql

    def test_success_reads_both_homes(self):
        sql = sql_steal_success("3b")
        assert "sb_success_3b" in sql
        assert "stolen_base_3b" in sql
        # A caught stealing is never a success.
        assert "caught_stealing_3b" not in sql

    def test_the_prefix_reaches_both_columns(self):
        sql = sql_steal_attempt("home", "rp.")
        assert "rp.sb_attempt_home" in sql
        assert "rp.events" in sql

    def test_an_unknown_base_raises(self):
        with pytest.raises(ValueError):
            sql_steal_attempt("1b")
        with pytest.raises(ValueError):
            sql_steal_success("second")


# ---------------------------------------------------------------------------
# The pitcher-steal metrics builder
# ---------------------------------------------------------------------------


def _conn(with_play_events: bool = True) -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    c.execute("ATTACH ':memory:' AS pg")
    c.execute("CREATE SCHEMA pg.raw")
    c.execute(
        "CREATE TABLE pg.raw.pitches ("
        "game_pk INTEGER, at_bat_number INTEGER, pitch_number INTEGER, "
        "season SMALLINT, pitcher INTEGER, p_throws VARCHAR, "
        "on_1b INTEGER, on_2b INTEGER, on_3b INTEGER, "
        "events VARCHAR, type VARCHAR, "
        "sb_attempt_2b BOOLEAN, sb_attempt_3b BOOLEAN, sb_attempt_home BOOLEAN, "
        "sb_success_2b BOOLEAN, sb_success_3b BOOLEAN, sb_success_home BOOLEAN, "
        "data_quality_flag BOOLEAN)"
    )
    if with_play_events:
        c.execute(
            "CREATE TABLE pg.raw.play_events ("
            "pitcher_id INTEGER, season SMALLINT, event_type VARCHAR, is_out BOOLEAN)"
        )
    c.execute("CREATE SCHEMA derived")
    c.execute(
        "CREATE TABLE derived.pitcher_steal_metrics ("
        "pitcher_id INTEGER, season SMALLINT, throws VARCHAR, "
        "sample_baserunner_events INTEGER, sample_steal_attempts_against INTEGER, "
        "sb_against_per_9 DOUBLE, cs_rate_forced DOUBLE, "
        "steal_attempt_rate_allowed DOUBLE, "
        "pickoff_rate DOUBLE, stepoff_rate DOUBLE, "
        "below_minimum_sample BOOLEAN, "
        "PRIMARY KEY (pitcher_id, season))"
    )
    return c


def _pitch(c, ab, pn, *, on_1b=None, on_2b=None, ev=None, att2=False, suc2=False):
    c.execute(
        "INSERT INTO pg.raw.pitches VALUES "
        "(1, ?, ?, 2024, 901, 'R', ?, ?, NULL, ?, NULL, ?, FALSE, FALSE, ?, FALSE, FALSE, FALSE)",
        [ab, pn, on_1b, on_2b, ev, att2, suc2],
    )


def _build(c) -> None:
    comp = PlayerProfileComputor.__new__(PlayerProfileComputor)
    comp._conn = c
    comp._build_pitcher_steal_metrics([2024])


def _row(c):
    return c.execute(
        "SELECT sample_steal_attempts_against, cs_rate_forced, pickoff_rate, stepoff_rate "
        "FROM derived.pitcher_steal_metrics WHERE pitcher_id = 901 AND season = 2024"
    ).fetchone()


class TestThePitcherStealMetrics:
    def test_an_event_caught_stealing_counts_as_a_forced_cs(self):
        """One column SB + one PA-ending event CS = 2 attempts, cs_rate 0.5.
        The columns-only build read 1 attempt and cs_rate_forced 0.0."""
        c = _conn()
        _pitch(c, 1, 1, on_1b=11, att2=True, suc2=True)
        _pitch(c, 2, 1, on_1b=12, ev="caught_stealing_2b")
        _build(c)
        attempts, cs_rate, _, _ = _row(c)
        assert attempts == 2
        assert cs_rate == pytest.approx(0.5)

    def test_disengagement_rates_come_from_play_events(self):
        """3 pickoffs + 1 pickoff_error + 2 stepoffs over 2 runner-on pitches:
        pickoff_rate counts every throw over (an errant one is still a throw)."""
        c = _conn()
        _pitch(c, 1, 1, on_1b=11)
        _pitch(c, 2, 1, on_2b=22)
        for et in ("pickoff", "pickoff", "pickoff", "pickoff_error", "stepoff", "stepoff"):
            c.execute("INSERT INTO pg.raw.play_events VALUES (901, 2024, ?, FALSE)", [et])
        _build(c)
        _, _, pickoff_rate, stepoff_rate = _row(c)
        assert pickoff_rate == pytest.approx(4 / 2)
        assert stepoff_rate == pytest.approx(2 / 2)

    def test_a_pre_0018_database_degrades_to_zero_rates(self):
        """No raw.play_events table: the probe falls back to an empty relation
        and both rates read 0.0 — never NULL, which would poison the steal
        draw's weight vector as NaN."""
        c = _conn(with_play_events=False)
        _pitch(c, 1, 1, on_1b=11)
        _build(c)
        _, _, pickoff_rate, stepoff_rate = _row(c)
        assert pickoff_rate == 0.0
        assert stepoff_rate == 0.0
