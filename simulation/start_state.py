"""Start a simulated game at a plate appearance of the real game (the "what if").

The game page lets a user pick any plate appearance of a real game, make
managerial changes, and simulate the rest of the game from that point. A
**start state** is the plain-data picture of the game at the start of that
plate appearance:

* the situation: inning, half, outs, the runners by base (player ids), the score;
* both nine-man batting orders as they stood, and each side's next-batter slot;
* each side's current pitcher, its starter, and its defense by position;
* the pitches thrown and batters faced by each pitcher so far, and the plate
  appearances already made in this half;
* the batting and throwing hand of every player who can appear;
* each side's available bullpen (the arms not yet used);
* the real runs of every half already completed (the inning grid so far) and
  the score when the current half began.

``pipeline.mlb_game_feed.state_at_pa`` builds it from the league's feed; a
``changes`` set (``simulation.whatif.apply_changes``) edits it. It travels to
the workers as the ``start_state`` sim-kwarg: plain data, so every iteration
builds its own fresh :class:`GameState` (a state object mutates as it plays).

:func:`apply_start_state` writes it onto a freshly built state inside
``simulate_game``. The rest of the game's facts (the managers, the pen's recent
usage, the park) come from the game's ordinary sim-kwargs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from simulation.game_state import GameState, Half, Team

SIDES = ("away", "home")


def _int_map(m: Mapping[Any, Any] | None) -> dict[int, Any]:
    return {int(k): v for k, v in (m or {}).items()}


def _defense(m: Mapping[str, Any] | None) -> dict[str, int]:
    return {str(k): int(v) for k, v in (m or {}).items() if v is not None}


def apply_start_state(state: GameState, ss: Mapping[str, Any]) -> None:
    """Write a start state onto ``state`` (a fresh top-of-the-1st state)."""
    state.inning = int(ss["inning"])
    state.half = Half.TOP if str(ss["half"]).lower().startswith("top") else Half.BOTTOM
    state.outs = int(ss.get("outs", 0))
    state.balls = 0
    state.strikes = 0
    state.away_score = int(ss.get("away_score", 0))
    state.home_score = int(ss.get("home_score", 0))

    runners = ss.get("runners") or {}
    state.bases.first = _opt_int(runners.get("first"))
    state.bases.second = _opt_int(runners.get("second"))
    state.bases.third = _opt_int(runners.get("third"))

    state.away_lineup = [int(p) for p in ss["away_lineup"]]
    state.home_lineup = [int(p) for p in ss["home_lineup"]]
    state.away_lineup_slot = int(ss.get("away_slot", 0)) % max(1, len(state.away_lineup))
    state.home_lineup_slot = int(ss.get("home_slot", 0)) % max(1, len(state.home_lineup))

    state.bat_hands.update({k: str(v) for k, v in _int_map(ss.get("bat_hands")).items()})
    state.throw_hands.update({k: str(v) for k, v in _int_map(ss.get("throw_hands")).items()})

    state.away_pitcher_id = int(ss["away_pitcher"])
    state.home_pitcher_id = int(ss["home_pitcher"])
    state.away_starter_id = _opt_int(ss.get("away_starter")) or state.away_pitcher_id
    state.home_starter_id = _opt_int(ss.get("home_starter")) or state.home_pitcher_id

    for side in SIDES:
        defense = _defense(ss.get(f"{side}_defense"))
        if defense:
            setattr(state, f"{side}_defense", defense)
            if "C" in defense:
                setattr(state, f"{side}_catcher_id", defense["C"])

    pitch_counts = {k: int(v) for k, v in _int_map(ss.get("pitch_counts")).items()}
    state.pitcher_pc = dict(pitch_counts)
    state.pitcher_bf = {k: int(v) for k, v in _int_map(ss.get("batters_faced")).items()}
    state.half_pa_count = int(ss.get("half_pa_count", 0))

    # The matchup: the batting side's due-up batter against the fielding
    # side's current pitcher.
    if state.offense == Team.HOME:
        state.batter_id = state.home_lineup[state.home_lineup_slot]
        state.pitcher_id = state.away_pitcher_id
    else:
        state.batter_id = state.away_lineup[state.away_lineup_slot]
        state.pitcher_id = state.home_pitcher_id
    state.pitcher_pitch_count = int(pitch_counts.get(state.pitcher_id, 0))
    state.throw_hand = state.throw_hands.get(state.pitcher_id, state.throw_hand)
    state.bat_hand = state.bat_hand_for(state.batter_id)


def apply_start_bullpen(state: GameState, ss: Mapping[str, Any]) -> None:
    """Each side's available arms (the pen not yet used). Applied after the
    game's own pen is seeded, so it wins."""
    pen = ss.get("bullpen")
    if pen is None:
        return
    state.manager.bullpen_available = {
        int(Team.AWAY): [int(p) for p in pen.get("away") or []],
        int(Team.HOME): [int(p) for p in pen.get("home") or []],
    }


def start_grid(ss: Mapping[str, Any]) -> tuple[list[int | None], list[int | None], int, int]:
    """The inning grid so far and the score when the current half began.

    Returns ``(away_by_inning, home_by_inning, half_start_away, half_start_home)``
    for ``simulate_game``'s grid: every completed half's real runs, so the grid
    of a game started mid-way still sums to its final score.
    """
    away = [None if v is None else int(v) for v in ss.get("grid_away") or []]
    home = [None if v is None else int(v) for v in ss.get("grid_home") or []]
    return (
        away,
        home,
        int(ss.get("half_start_away", ss.get("away_score", 0))),
        int(ss.get("half_start_home", ss.get("home_score", 0))),
    )


def _opt_int(v: Any) -> int | None:
    return None if v is None else int(v)
