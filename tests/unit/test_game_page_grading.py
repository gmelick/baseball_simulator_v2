"""The game page's betting box: each side graded on the real final."""

from __future__ import annotations

import pytest

from api.routes.betting import _grade
from simulation.game_market_distributions import SegmentRuns

# Away 0 1 0 | 2 0 0 0 0 0 = 3; home 1 0 0 | 0 2 0 0 1 x = 4. Home scored first.
GRID = {"away": [0, 1, 0, 2, 0, 0, 0, 0, 0], "home": [1, 0, 0, 0, 2, 0, 0, 1, None]}
ACTUAL = SegmentRuns.from_official_grid(GRID)


@pytest.mark.parametrize(
    ("label", "side", "line", "expected"),
    [
        ("moneyline", "home", None, "won"),
        ("moneyline", "away", None, "lost"),
        ("run_line", "home", -1.5, "lost"),  # won by 1
        ("run_line", "away", 1.5, "won"),
        ("total", "over", 6.5, "won"),  # 7 runs
        ("total", "under", 6.5, "lost"),
        ("total", "over", 7.0, "push"),
        ("total", "under", 7.0, "push"),
        ("f5_moneyline", "home", None, "lost"),  # 3-3 after five
        ("f5_moneyline", "draw", None, "won"),
        ("f1_moneyline", "draw", None, "lost"),  # 0-1 after one
        ("f1_moneyline", "home", None, "won"),
        ("f5_total", "over", 5.5, "won"),  # 6 runs
        ("team_total_away", "under", 3.5, "won"),
        ("team_total_home", "over", 3.5, "won"),
        ("first_to_score", "home", None, "won"),
        ("first_to_score", "away", None, "lost"),
        ("first_inning_run", "over", 0.5, "won"),
        ("first_inning_run", "under", 0.5, "lost"),
    ],
)
def test_grades(label: str, side: str, line: float | None, expected: str) -> None:
    assert _grade(ACTUAL, label, side, line) == expected


def test_a_tied_segment_wins_the_draw() -> None:
    tied = SegmentRuns.from_official_grid({"away": [1, 0, 0, 0, 0, 2], "home": [0, 1, 0, 0, 0, 0]})
    assert _grade(tied, "f5_moneyline", "draw", None) == "won"
    assert _grade(tied, "f5_moneyline", "home", None) == "lost"
    assert _grade(tied, "f5_moneyline", "away", None) == "lost"


def test_an_unknown_market_is_not_graded() -> None:
    assert _grade(ACTUAL, "no_such_market", "home", None) is None
