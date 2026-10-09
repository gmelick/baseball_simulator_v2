"""SIM-546: the inning grid on the per-game result and the cached summary.

The loop writes each team's runs per inning onto the per-game result
(``GameSimResult.home_by_inning`` / ``away_by_inning``). The summary keeps the
cells of every iteration (``GameSimSummary.inning_grids``), so the cache can
price the segment and team markets (the first inning, the first five innings,
a team's runs). These tests pin four things:

* the loop's grid equals the linescore that ``simulation.linescore`` derives
  from the recorded plays of the same game, cell for cell;
* the grid reads the state only, so every game plays exactly as before;
* the summary carries the grids, and a summary without them reads None;
* the stored summary's lite projection drops the grids.

The games run on the no-DB synthetic bundle through ``record_game_plays``.
"""

from __future__ import annotations

import pickle
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from api.routes.games import _sim_summary_lite_from_stored
from api.schemas import GameSimSummaryLite
from api.serialization import to_jsonable
from simulation.game_market_distributions import SegmentRuns, segment_runs_from_summary
from simulation.linescore import linescore_from_plays
from simulation.play_recorder import record_game_plays
from simulation.results import GameSimSummary
from simulation.sim_loop import GameSimResult

SIM_KWARGS = {
    "season": 2024,
    "pitcher_id": 600001,
    "bat_hand": "R",
    "away_lineup": [101, 102, 103, 104, 105, 106, 107, 108, 109],
    "home_lineup": [201, 202, 203, 204, 205, 206, 207, 208, 209],
    "max_innings": 12,
}

SEEDS = tuple(range(10))

#: (home_score, away_score, total_pitches) per seed, read at commit 80e3f46
#: before the grid existed. The grid draws no random number, so these hold.
OUTCOMES_AT_80E3F46 = {
    0: (8, 6, 326),
    1: (3, 4, 312),
    2: (8, 2, 259),
    3: (1, 5, 304),
    4: (2, 6, 285),
    5: (6, 7, 357),
    6: (10, 8, 290),
    7: (3, 5, 314),
    8: (5, 4, 269),
    9: (4, 7, 310),
}

#: Seed 8 ends on a walk-off in the bottom of the 9th.
WALK_OFF_SEED = 8
#: Seed 0: the home team leads after the top of the 9th, so the bottom is
#: never played.
UNPLAYED_BOTTOM_SEED = 0


@pytest.fixture(scope="module")
def recorded_games():
    """The ten games, recorded once: ``{seed: (result, plays)}``."""
    return {seed: record_game_plays(seed=seed, sim_kwargs=dict(SIM_KWARGS)) for seed in SEEDS}


def _result(
    home: list[int | None] | None, away: list[int | None] | None, *, score=(1, 0)
) -> GameSimResult:
    """A hand-built result; the summary reads only the scores and the grid."""
    return GameSimResult(
        home_score=score[0],
        away_score=score[1],
        innings_played=9,
        final_state=None,  # type: ignore[arg-type]
        home_by_inning=home,
        away_by_inning=away,
    )


# ---------------------------------------------------------------------------
# 1-4: the loop's grid
# ---------------------------------------------------------------------------


def test_grid_matches_the_linescore_on_recorded_games(recorded_games):
    for seed, (result, plays) in recorded_games.items():
        linescore = linescore_from_plays(plays)
        assert result.home_by_inning == linescore.home_by_inning, seed
        assert result.away_by_inning == linescore.away_by_inning, seed


def test_grid_sums_to_the_final_score(recorded_games):
    for seed, (result, _plays) in recorded_games.items():
        assert result.home_by_inning is not None
        assert result.away_by_inning is not None
        assert len(result.home_by_inning) == len(result.away_by_inning), seed
        assert len(result.away_by_inning) == result.innings_played, seed
        assert sum(c for c in result.home_by_inning if c is not None) == result.home_score
        assert sum(c for c in result.away_by_inning if c is not None) == result.away_score
        # Only the home team's last cell can be unplayed; the away team bats
        # every inning.
        assert None not in result.away_by_inning, seed
        assert None not in result.home_by_inning[:-1], seed


def test_walk_off_and_unplayed_bottom(recorded_games):
    walk_off, _ = recorded_games[WALK_OFF_SEED]
    assert walk_off.walk_off is True
    assert walk_off.home_by_inning is not None
    # The walk-off half was played and scored the winning run.
    assert walk_off.home_by_inning[-1] is not None
    assert walk_off.home_by_inning[-1] > 0

    unplayed, _ = recorded_games[UNPLAYED_BOTTOM_SEED]
    assert unplayed.walk_off is False
    assert unplayed.home_score > unplayed.away_score
    assert unplayed.innings_played == 9
    assert unplayed.home_by_inning is not None
    assert unplayed.away_by_inning is not None
    assert unplayed.home_by_inning[-1] is None
    assert unplayed.away_by_inning[-1] is not None


def test_ceiling_break_closes_the_half_in_progress():
    """A game cut at the inning ceiling keeps the cell of the half it was in.

    Seed 3 is tied after ten innings. With a ceiling of ten, the loop stops
    after the first pitch of the 11th; the top of the 11th was played, so it
    has a cell, and the bottom has None.
    """
    result, plays = record_game_plays(seed=3, sim_kwargs={**SIM_KWARGS, "max_innings": 10})
    linescore = linescore_from_plays(plays)
    assert result.home_by_inning == linescore.home_by_inning
    assert result.away_by_inning == linescore.away_by_inning
    assert result.away_by_inning is not None
    assert result.home_by_inning is not None
    assert len(result.away_by_inning) == 11
    assert result.away_by_inning[-1] is not None
    assert result.home_by_inning[-1] is None


def test_outcomes_unchanged(recorded_games):
    observed = {
        seed: (result.home_score, result.away_score, result.total_pitches)
        for seed, (result, _plays) in recorded_games.items()
    }
    assert observed == OUTCOMES_AT_80E3F46


# ---------------------------------------------------------------------------
# 5: the summary
# ---------------------------------------------------------------------------


def test_summary_carries_grids_and_tolerates_their_absence(recorded_games):
    results = [recorded_games[seed][0] for seed in SEEDS]
    summary = GameSimSummary.from_results(results)
    assert summary.inning_grids is not None
    assert len(summary.inning_grids) == len(SEEDS)
    for (home_cells, away_cells), result in zip(summary.inning_grids, results, strict=True):
        assert home_cells == result.home_by_inning
        assert away_cells == result.away_by_inning

    # The segment runs read from the summary give each game's final score.
    runs = segment_runs_from_summary(summary)
    assert isinstance(runs, SegmentRuns)
    assert runs.n == len(SEEDS)
    assert runs.home_game.tolist() == [r.home_score for r in results]
    assert runs.away_game.tolist() == [r.away_score for r in results]

    # One result without a grid: the summary has no grid at all.
    hand_built = [_result([1, 0], [0, 0]), _result(None, None)]
    no_grid = GameSimSummary.from_results(hand_built)
    assert no_grid.inning_grids is None
    assert segment_runs_from_summary(no_grid) is None

    # A stub summary with no such attribute at all.
    class _Stub:
        home_scores = None

    assert segment_runs_from_summary(_Stub()) is None


def test_a_summary_pickled_before_the_grid_reads_none():
    """A summary cached by the old code restores with the grid slot unset.

    Deleting the slot before the round trip gives the same state the old
    pickle restores to. The summary then reads None, and ``to_jsonable``
    (the stored summary) does not raise.
    """
    summary = GameSimSummary.from_results(
        [_result([1, 0], [0, 0])], simulated_at=datetime(2026, 10, 9, tzinfo=UTC)
    )
    del summary.inning_grids
    restored = pickle.loads(pickle.dumps(summary))
    assert restored.inning_grids is None
    assert getattr(restored, "inning_grids", "missing") is None
    assert segment_runs_from_summary(restored) is None
    assert to_jsonable(restored)["inning_grids"] is None
    # Every other missing name still raises.
    with pytest.raises(AttributeError):
        _ = restored.no_such_field  # type: ignore[attr-defined]


def test_the_stored_summary_serialises_the_grid():
    summary = GameSimSummary.from_results(
        [_result([1, None], [0, 0], score=(1, 0)), _result([0, 2], [1, 0], score=(2, 1))]
    )
    stored = to_jsonable(summary)
    assert stored["inning_grids"] == [[[1, None], [0, 0]], [[0, 2], [1, 0]]]
    # The cache pickles the summary; the grid survives the round trip.
    assert pickle.loads(pickle.dumps(summary)).inning_grids == summary.inning_grids


# ---------------------------------------------------------------------------
# 6: the lite projection
# ---------------------------------------------------------------------------


def test_lite_projection_drops_the_grids(recorded_games):
    results = [recorded_games[seed][0] for seed in SEEDS]
    stored = to_jsonable(GameSimSummary.from_results(results))
    assert "inning_grids" in stored
    # ``to_jsonable`` writes no ``half_width`` on an interval, and the lite
    # model requires one (a gap older than the grid, outside this test).
    # Add it, as the stored summaries of the existing card tests carry it.
    for key, value in stored.items():
        if key.endswith("_ci"):
            value["half_width"] = (value["high"] - value["low"]) / 2.0
    # The grid is an unknown field to the lite model, which forbids extras.
    with pytest.raises(ValidationError):
        GameSimSummaryLite.model_validate(
            {k: v for k, v in stored.items() if not k.endswith("_scores")}
        )
    lite = _sim_summary_lite_from_stored(stored)
    assert lite is not None
    assert lite.n_iterations == len(SEEDS)
    assert "inning_grids" not in lite.model_dump()
