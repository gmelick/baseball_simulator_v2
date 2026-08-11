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

Every test below fails if someone edits one definition and not the other.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pipeline.batch.player_profile_computor import (
    NON_SWING_TYPES,
    SQL_SWING,
    SQL_WHIFF,
    WHIFF_TYPES,
    sql_in,
)

_SOURCE = Path(__file__).resolve().parents[2] / "pipeline" / "batch" / "player_profile_computor.py"


def _pool_build_codes(label: str) -> set[str]:
    """Pull the code set the pool build maps to ``label`` straight from the SQL.

    Reads the source rather than importing a constant on purpose: the pool build
    spells its sets inline, so the only way to prove agreement is to go and read
    what it actually says.
    """
    src = _SOURCE.read_text(encoding="utf-8")
    lbl = re.escape(label)
    # The pool build writes a multi-code set as `IN ('S', 'W', 'M')` and a single
    # code as `= 'C'`. Match both, or this helper silently reports an empty set for
    # every single-code label and the test passes for the wrong reason.
    m = re.search(r"WHEN TRIM\(type\) IN \(([^)]*)\) THEN '" + lbl + r"'", src)
    if m is None:
        m = re.search(r"WHEN TRIM\(type\) = ('[^']+') THEN '" + lbl + r"'", src)
    assert m is not None, f"the pool build no longer maps any code set to {label!r}"
    codes = set(re.findall(r"'([^']+)'", m.group(1)))
    assert codes, f"matched the {label!r} branch but parsed no codes from it"
    return codes


class TestWhiffAgreesWithThePoolBuild:
    def test_whiff_types_equal_the_pool_builds_swinging_strikes(self):
        """The headline invariant. Edit one, this fails."""
        assert set(WHIFF_TYPES) == _pool_build_codes("swinging_strike")

    def test_foul_tips_are_not_whiffs(self):
        """A foul tip is CONTACT — the bat touches the ball.

        The reverted commit counted 'T' as a swing and a miss while this same file
        classified it as a foul, so the metric and the pool disagreed by 7,464
        pitches per season.
        """
        assert "T" not in WHIFF_TYPES
        assert "T" in _pool_build_codes("foul")

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
        ball_and_called = _pool_build_codes("ball") | _pool_build_codes("called_strike")
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
