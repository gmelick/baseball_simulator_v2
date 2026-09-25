"""SIM-501: the profile SQL's pitch-code sets must agree with the pool build.

WHY THIS FILE EXISTS
====================
``player_profile_computor.py`` carried TWO definitions of a swing and a miss, in
one file, and used the wrong one.  ``whiff_rate`` counted ``type = 'C'`` — the
CALLED-strike rate, where the batter never swung — while the pool build 3,000
lines away classified swinging strikes correctly.

A first attempt at the fix (commit ``c11c919``, reverted) unified the definitions
by inventing a THIRD set that included foul tips, contradicting the pool build
again.  It shipped with a docstring claiming durability and no test behind it.

So the rule these tests enforce is narrow and blunt: **the constants the profile
SQL uses must equal the codes the pool build uses.**  A pitcher's whiff rate has
to describe the same event as the pool the simulator samples from, or the metric
and the model disagree about what happened.

SIM-553 changed how the tests check it. The pool build now renders its class
expression from the named code sets (``SQL_OUTCOME_TYPE``), and one of its
branches is conditional: a foul tip or foul bunt is a ``foul`` below two strikes
and strike three (``swinging_strike``) at two. The old helper read the source
with a regular expression. A text pattern cannot see a conditional branch, and
it cannot see a branch-order bug (a CASE takes the first branch that matches).
So the helper below EXECUTES the expression in DuckDB and reads the class each
code gets at a given count.

Every test below fails if someone edits one definition and not the other.
"""

from __future__ import annotations

import re
import string
from pathlib import Path

import duckdb
import pytest

from pipeline.batch.player_profile_computor import (
    BALL_TYPES,
    CALLED_STRIKE_TYPES,
    FOUL_TYPES,
    NON_SWING_TYPES,
    SQL_OUTCOME_TYPE,
    SQL_SWING,
    SQL_WHIFF,
    STRIKE_THREE_FOUL_TYPES,
    WHIFF_TYPES,
    sql_in,
)
from pipeline.statcast_events import IN_PLAY_TYPES

_SOURCE = Path(__file__).resolve().parents[2] / "pipeline" / "batch" / "player_profile_computor.py"

#: Every code ``raw.pitches`` holds in ten seasons (17 codes; re-read 2026-09-25).
_FEED_CODES: tuple[str, ...] = (
    ("B", "*B", "P")  # ball
    + ("C",)  # called strike
    + ("S", "W", "M", "Q")  # swing and miss
    + ("F", "T", "L", "O", "R")  # the bat touched it, not in play
    + ("X", "D", "E")  # in play
    + ("H",)  # hit by pitch
)

#: The codes the helper runs: the feed's codes, every code a set names, and
#: every capital letter, so a code spelled inline in the expression runs too.
_CODES: tuple[str, ...] = tuple(
    sorted(
        set(_FEED_CODES)
        | set(BALL_TYPES)
        | set(CALLED_STRIKE_TYPES)
        | set(WHIFF_TYPES)
        | set(FOUL_TYPES)
        | set(IN_PLAY_TYPES)
        | set(NON_SWING_TYPES)
        | set(string.ascii_uppercase)
    )
)


def _codes_by_class(strikes: int) -> dict[str | None, set[str]]:
    """Run the pool build's class expression over every code at ``strikes``.

    Returns ``{class: codes}``. A code the expression does not know lands under
    ``None``. The codes go in space-padded, as the feed's char(2) column holds
    them, and come back trimmed.
    """
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE p (type VARCHAR, strikes SMALLINT, events VARCHAR)")
        con.executemany(
            "INSERT INTO p VALUES (?, ?, NULL)", [(c.ljust(2), strikes) for c in _CODES]
        )
        rows = con.execute(f"SELECT TRIM(type), {SQL_OUTCOME_TYPE} FROM p").fetchall()
    finally:
        con.close()
    out: dict[str | None, set[str]] = {}
    for code, cls in rows:
        out.setdefault(cls, set()).add(code)
    return out


class TestWhiffAgreesWithThePoolBuild:
    @pytest.mark.parametrize("strikes", [0, 1])
    def test_whiff_types_equal_the_pool_builds_swinging_strikes(self, strikes):
        """The headline invariant. Edit one, this fails.

        Below two strikes the pool's ``swinging_strike`` codes are exactly the
        profile's whiffs. At two strikes the class also takes the foul tips and
        foul bunts (SIM-553), which are strikes, not whiffs.
        """
        assert _codes_by_class(strikes).get("swinging_strike", set()) == set(WHIFF_TYPES)

    def test_at_two_strikes_the_class_adds_only_the_strike_three_fouls(self):
        got = _codes_by_class(2).get("swinging_strike", set())
        assert got == set(WHIFF_TYPES) | set(STRIKE_THREE_FOUL_TYPES)

    def test_foul_tips_are_not_whiffs(self):
        """A foul tip is CONTACT — the bat touches the ball.

        The reverted commit counted 'T' as a swing and a miss while this same file
        classified it as a foul, so the metric and the pool disagreed by 7,464
        pitches per season. Below two strikes the pool still codes 'T' a foul.
        """
        assert "T" not in WHIFF_TYPES
        assert "T" in _codes_by_class(0)["foul"]
        assert "T" in _codes_by_class(1)["foul"]

    def test_a_strike_three_foul_is_not_a_whiff(self):
        """SIM-553: a two-strike foul tip or foul bunt is strike three, but the
        bat touched the ball. Contact is not a whiff."""
        assert not set(STRIKE_THREE_FOUL_TYPES) & set(WHIFF_TYPES)

    def test_a_swinging_strike_the_catcher_dropped_is_still_a_whiff(self):
        """'W' is a swing and a miss the catcher did not hold. 4,027 in 2024."""
        assert "W" in WHIFF_TYPES


class TestSwingIsTheComplementOfTheNonSwings:
    def test_no_code_is_both_a_whiff_and_a_non_swing(self):
        """A whiff REQUIRES a swing, so the two sets cannot overlap."""
        assert not set(WHIFF_TYPES) & set(NON_SWING_TYPES)

    def test_a_called_strike_is_not_a_swing(self):
        """The exact confusion that produced the original defect: 'C' is a TAKE."""
        assert "C" in NON_SWING_TYPES
        assert "C" not in WHIFF_TYPES

    def test_the_non_swing_set_matches_the_pool_builds_take_codes(self):
        by_class = _codes_by_class(0)
        ball_and_called = by_class.get("ball", set()) | by_class.get("called_strike", set())
        assert ball_and_called, "the pool build maps no code to a take"
        assert ball_and_called <= set(NON_SWING_TYPES)


class TestTheRenderedSqlIsUsable:
    def test_sql_in_renders_a_quoted_list(self):
        assert sql_in(("S", "W")) == "('S', 'W')"

    def test_the_fragments_are_valid_sql_predicates(self):
        assert f"type IN {sql_in(WHIFF_TYPES)}" == SQL_WHIFF
        assert f"type NOT IN {sql_in(NON_SWING_TYPES)}" == SQL_SWING

    @pytest.mark.parametrize("fragment", [SQL_WHIFF, SQL_SWING])
    def test_a_fragment_carries_no_unrendered_placeholder(self, fragment):
        """A ``{SQL_WHIFF}`` that lands in a NON-f-string ships this text into the
        query verbatim. It then matches nothing, silently."""
        assert "{" not in fragment and "}" not in fragment


class TestNoDefinitionSurvivesInline:
    """The point of the constants is that the sets are spelled ONCE."""

    def test_the_old_broken_whiff_is_gone(self):
        src = _SOURCE.read_text(encoding="utf-8")
        assert "type = 'C' THEN 1.0 ELSE 0 END) / COUNT(*) AS whiff_rate" not in src

    def test_no_stray_inline_whiff_set_remains(self):
        src = _SOURCE.read_text(encoding="utf-8")
        assert "type IN ('M', 'O', 'S', 'T')" not in src


class TestZSwingLegsCountSwings:
    """SIM-522: the platoon z-swing legs kept the inverted predicate.

    SIM-501 fixed ``z_swing_rate`` at the season level. ``z_swing_rate_vs_l`` and
    ``z_swing_rate_vs_r`` still counted ``type IN (NON_SWING)`` — the codes where
    the bat never moved — so both held the z-TAKE rate (live 2024: 0.32 against
    the season column's 0.68). A swing is the complement of the non-swing set,
    so every z-swing column must use ``NOT IN``.
    """

    _INVERTED = re.compile(r"zone BETWEEN 1 AND 9 AND type IN \('B', 'C', 'H', 'P', '\*B'\)")

    def test_no_z_swing_column_counts_the_non_swing_set(self):
        src = _SOURCE.read_text(encoding="utf-8")
        assert not self._INVERTED.search(src), (
            "a zone-swing predicate counts the NON-swing codes — that column holds a "
            "TAKE rate under a swing name (the SIM-501 / SIM-522 defect)"
        )

    @pytest.mark.parametrize("col", ["z_swing_rate", "z_swing_rate_vs_l", "z_swing_rate_vs_r"])
    def test_each_z_swing_column_is_defined_with_not_in(self, col):
        src = _SOURCE.read_text(encoding="utf-8")
        # The text between the previous column's alias and this alias is this
        # column's whole expression (numerator AND denominator). It must carry
        # the swing predicate, not the take one.
        idx = src.index(f"AS {col},")
        expr = src[src.rfind(" AS ", 0, idx) : idx]
        assert "type NOT IN ('B', 'C', 'H', 'P', '*B')" in expr, col
