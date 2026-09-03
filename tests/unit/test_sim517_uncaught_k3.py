"""
tests/unit/test_sim517_uncaught_k3.py
=====================================
SIM-517 part A — the uncaught-strike-3 slice of the catcher receiving
profile (``PlayerProfileComputor._compute_catcher_uncaught_k3``).

The label: a strikeout-final pitch whose PA description names a wild pitch
or a passed ball. The output: raw counts in ``sample_``-prefixed columns
(the artifact exporter keeps ``sample_*`` OUT of the similarity embedding)
plus ONE embedding-visible EB-shrunk rate toward the season league rate.

Runs against real DuckDB over stub ``pg.raw.pitches`` — the SIM-515 harness
pattern. The ``derived.catcher_season_metrics`` DDL is the REAL one from
``db/schemas/02_duckdb_schema.sql``, so a schema drift breaks this test, not
production.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

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
        "season SMALLINT, fielder_2 INTEGER, events VARCHAR, des VARCHAR)"
    )
    c.execute("CREATE SCHEMA derived")
    c.execute(_real_ddl("derived.catcher_season_metrics"))
    return c


def _k3(c, pk, ab, *, catcher, events="strikeout", des="Somebody strikes out swinging."):
    c.execute(
        "INSERT INTO pg.raw.pitches VALUES (?,?,3,2025,?,?,?)",
        [pk, ab, catcher, events, des],
    )


def _catcher_row(c, catcher):
    c.execute(
        "INSERT INTO derived.catcher_season_metrics (player_id, season) VALUES (?, 2025)",
        [catcher],
    )


def _compute(c) -> None:
    comp = PlayerProfileComputor.__new__(PlayerProfileComputor)
    comp._conn = c
    comp._compute_catcher_uncaught_k3([2025])


def _row(c, catcher):
    return c.execute(
        "SELECT sample_k3_received, sample_uncaught_k3, uncaught_k3_rate_eb "
        "FROM derived.catcher_season_metrics WHERE player_id = ? AND season = 2025",
        [catcher],
    ).fetchone()


_UNCAUGHT_DES = "Somebody strikes out swinging. Somebody to 1st. Wild pitch by pitcher X."
_UNCAUGHT_PB = "Somebody strikes out swinging. Somebody to 1st. Passed ball by catcher Y."


class TestTheLabel:
    def test_wild_pitch_and_passed_ball_both_count(self):
        c = _conn()
        _catcher_row(c, 900)
        _k3(c, 1, 1, catcher=900, des=_UNCAUGHT_DES)
        _k3(c, 1, 2, catcher=900, des=_UNCAUGHT_PB)
        _k3(c, 1, 3, catcher=900)  # a clean strikeout
        _compute(c)
        assert _row(c, 900)[:2] == (3, 2)

    def test_a_clean_strikeout_is_not_uncaught(self):
        c = _conn()
        _catcher_row(c, 900)
        _k3(c, 1, 1, catcher=900)
        _compute(c)
        assert _row(c, 900)[:2] == (1, 0)

    def test_non_strikeout_pitches_never_enter_the_denominator(self):
        c = _conn()
        _catcher_row(c, 900)
        _k3(c, 1, 1, catcher=900)
        # A walk PA whose description mentions a wild pitch: not a K3 row.
        _k3(c, 1, 2, catcher=900, events="walk", des="Somebody walks. Wild pitch by X.")
        # A mid-PA pitch (no terminal event): never counted.
        _k3(c, 1, 3, catcher=900, events=None, des="Somebody walks.")
        _compute(c)
        assert _row(c, 900)[:2] == (1, 0)

    def test_strikeout_double_play_counts_as_received(self):
        c = _conn()
        _catcher_row(c, 900)
        _k3(c, 1, 1, catcher=900, events="strikeout_double_play")
        _compute(c)
        assert _row(c, 900)[:2] == (1, 0)


class TestTheShrinkage:
    def test_the_rate_shrinks_toward_the_season_league_rate(self):
        # Catcher A: 3 K3s, 1 uncaught (raw 0.333). Catcher B: 7 K3s, 0
        # uncaught. League rate = 1/10. With prior n=2000 both land within a
        # whisker of 0.1, A a hair above B.
        c = _conn()
        _catcher_row(c, 900)
        _catcher_row(c, 901)
        _k3(c, 1, 1, catcher=900, des=_UNCAUGHT_DES)
        _k3(c, 1, 2, catcher=900)
        _k3(c, 1, 3, catcher=900)
        for ab in range(4, 11):
            _k3(c, 1, ab, catcher=901)
        _compute(c)
        league = 1.0 / 10.0
        n_prior = PlayerProfileComputor._UNCAUGHT_K3_EB_PRIOR_N
        a = _row(c, 900)[2]
        b = _row(c, 901)[2]
        assert a == pytest.approx((1 + n_prior * league) / (3 + n_prior))
        assert b == pytest.approx((0 + n_prior * league) / (7 + n_prior))
        assert a > b

    def test_a_catcher_with_no_k3s_keeps_null_columns(self):
        c = _conn()
        _catcher_row(c, 900)
        _catcher_row(c, 999)  # never received a strike-3
        _k3(c, 1, 1, catcher=900)
        _compute(c)
        assert _row(c, 999) == (None, None, None)


class TestEmbeddingExclusion:
    def test_sample_columns_carry_the_excluded_prefix(self):
        """The artifact exporter drops columns starting with ``sample_`` —
        the raw counts must carry that prefix so Poisson noise never enters
        the similarity embedding, and the EB rate must NOT carry it."""
        cols = duckdb.connect(":memory:")
        cols.execute("CREATE SCHEMA derived")
        cols.execute(_real_ddl("derived.catcher_season_metrics"))
        names = [
            r[0]
            for r in cols.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='catcher_season_metrics'"
            ).fetchall()
        ]
        assert "sample_k3_received" in names
        assert "sample_uncaught_k3" in names
        assert "uncaught_k3_rate_eb" in names
        assert not any(n == "uncaught_k3_rate_eb" and n.startswith("sample_") for n in names)
