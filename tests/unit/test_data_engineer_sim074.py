"""
SIM-074 — Statcast sliding-scale barrel definition
==================================================

Validates :func:`pipeline.batch.player_profile_computor._barrel_case_sql`,
the DuckDB boolean expression that classifies a batted ball as a *barrel*.

Real Statcast definition (what we implement here):

  * Below 98 mph exit velocity it is NEVER a barrel.
  * At exactly 98 mph the launch-angle band is 26-30 deg.
  * For each +1 mph above 98 the lower bound DROPS 1 deg and the upper bound
    RISES 2 deg, so the band widens with exit velocity.
  * The band is clamped to [8, 50] deg, reached around >= 116 mph.

Closed-form (deg), where EV = launch_speed:

    lower_la = GREATEST(8,  26 - (EV - 98) * 1)
    upper_la = LEAST   (50, 30 + (EV - 98) * 2)
    barrel   = (EV >= 98) AND (lower_la <= LA <= upper_la)

Exact band edges at the documented anchor velocities:

    EV (mph) | lower LA | upper LA
    ---------+----------+---------
       98    |    26    |    30
       99    |    25    |    32
      100    |    24    |    34
      105    |    19    |    44
      110    |    14    |    50  (upper clamped: 30+24=54 -> 50)
      116    |     8    |    50  (lower clamped: 26-18=8 -> 8)
     >=116   |     8    |    50

A NULL exit velocity or launch angle yields NULL (never counted as a barrel).
"""

from __future__ import annotations

import duckdb
import pytest

from pipeline.batch import player_profile_computor as ppc


@pytest.fixture()
def con():
    """In-memory DuckDB with a synthetic batted-ball table."""
    c = duckdb.connect(":memory:")
    c.execute(
        """
        CREATE TABLE bbe (
            id          INTEGER,
            launch_speed DOUBLE,
            launch_angle DOUBLE,
            p_throws    VARCHAR
        )
        """
    )
    yield c
    c.close()


def _is_barrel(con, ev, la, prefix: str = "") -> bool | None:
    """Insert one row and return whether the barrel CASE expression flags it.

    Returns None when the SQL boolean evaluates to NULL (NULL inputs).
    """
    con.execute("DELETE FROM bbe")
    con.execute(
        "INSERT INTO bbe VALUES (1, ?, ?, 'R')",
        [ev, la],
    )
    expr = ppc._barrel_case_sql(prefix)
    row = con.execute(f"SELECT ({expr}) AS is_barrel FROM bbe").fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Boundary classification (required by the ticket)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ev, la, expected",
    [
        # exactly at the 98 mph floor, band 26-30
        (98.0, 28.0, True),    # inside band
        (98.0, 26.0, True),    # lower edge inclusive
        (98.0, 30.0, True),    # upper edge inclusive
        (98.0, 25.0, False),   # just below lower edge -> not a barrel
        (98.0, 31.0, False),   # just above upper edge -> not a barrel
        # just below the EV floor -> never a barrel even at an ideal angle
        (97.0, 28.0, False),
        (97.9, 28.0, False),
        # 100 mph: band widens to 24-34
        (100.0, 24.0, True),   # new lower edge
        (100.0, 34.0, True),   # new upper edge
        (100.0, 23.0, False),  # below widened lower edge
        (100.0, 35.0, False),  # above widened upper edge
        # 116 mph: band fully clamped to 8-50
        (116.0, 8.0, True),    # clamped lower edge
        (116.0, 50.0, True),   # clamped upper edge
        (116.0, 7.0, False),   # below clamp
        (116.0, 51.0, False),  # above clamp
        # well past 116 mph -> still clamped 8-50
        (120.0, 8.0, True),
        (120.0, 50.0, True),
        (120.0, 7.0, False),
    ],
)
def test_barrel_boundary_points(con, ev, la, expected):
    assert _is_barrel(con, ev, la) is expected


# ---------------------------------------------------------------------------
# NULL handling — NULL inputs must NOT be counted as barrels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ev, la",
    [
        (None, 28.0),    # NULL exit velocity
        (105.0, None),   # NULL launch angle
        (None, None),    # both NULL
    ],
)
def test_null_inputs_not_barrels(con, ev, la):
    result = _is_barrel(con, ev, la)
    # NULL boolean (or explicit False) — in either case the enclosing
    # SUM(CASE WHEN <expr> THEN 1.0 ELSE 0 END) treats it as a non-barrel.
    assert not result


def test_null_not_summed_as_barrel(con):
    """End-to-end: NULL EV/LA rows do not increment the barrel numerator."""
    con.execute("DELETE FROM bbe")
    con.executemany(
        "INSERT INTO bbe VALUES (?, ?, ?, ?)",
        [
            (1, None, None, "R"),     # NULL -> not a barrel
            (2, 97.0, 28.0, "R"),     # below EV floor -> not a barrel
            (3, 98.0, 28.0, "R"),     # barrel
            (4, 100.0, 34.0, "R"),    # barrel
        ],
    )
    expr = ppc._barrel_case_sql()
    n = con.execute(
        f"SELECT SUM(CASE WHEN {expr} THEN 1.0 ELSE 0 END) FROM bbe"
    ).fetchone()[0]
    assert n == 2.0


# ---------------------------------------------------------------------------
# Sliding-scale monotonicity: the band must widen as EV rises
# ---------------------------------------------------------------------------


def test_band_widens_with_velocity(con):
    # LA = 24 is a barrel at 100 mph but NOT at 99 (lower bound is 25 there)
    assert _is_barrel(con, 100.0, 24.0) is True
    assert _is_barrel(con, 99.0, 24.0) is False
    # LA = 33 is a barrel at 100 mph (upper 34) but NOT at 98 (upper 30)
    assert _is_barrel(con, 100.0, 33.0) is True
    assert _is_barrel(con, 98.0, 33.0) is False


# ---------------------------------------------------------------------------
# Platoon prefix gating works (p_throws filter)
# ---------------------------------------------------------------------------


def test_platoon_prefix_gates_handedness(con):
    con.execute("DELETE FROM bbe")
    con.execute("INSERT INTO bbe VALUES (1, 100.0, 30.0, 'L')")
    # An ideal barrel hit off a LHP: counts for vs_l, not for vs_r.
    expr_l = ppc._barrel_case_sql("p_throws='L' AND ")
    expr_r = ppc._barrel_case_sql("p_throws='R' AND ")
    assert con.execute(f"SELECT ({expr_l}) FROM bbe").fetchone()[0] is True
    assert con.execute(f"SELECT ({expr_r}) FROM bbe").fetchone()[0] is False


# ---------------------------------------------------------------------------
# Anchor band edges match the documented EV -> LA mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ev, lower, upper",
    [
        (98.0, 26.0, 30.0),
        (99.0, 25.0, 32.0),
        (100.0, 24.0, 34.0),
        (105.0, 19.0, 44.0),
        (110.0, 14.0, 50.0),   # upper clamped
        (116.0, 8.0, 50.0),    # lower clamped
        (120.0, 8.0, 50.0),    # both clamped
    ],
)
def test_documented_band_edges(con, ev, lower, upper):
    # Both edges inclusive -> barrels; just outside -> not.
    assert _is_barrel(con, ev, lower) is True
    assert _is_barrel(con, ev, upper) is True
    assert _is_barrel(con, ev, lower - 0.5) is False
    assert _is_barrel(con, ev, upper + 0.5) is False
