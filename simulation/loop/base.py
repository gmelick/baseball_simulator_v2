"""The type declarations the loop's mixins share.

Each mixin reads attributes that :meth:`StateMachine.__init__` sets and calls
methods that the core or another mixin defines. ``LoopMixin`` declares them for
the type checker only: the block runs under ``TYPE_CHECKING``, so at run time
the class is empty and every name resolves on the machine itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

    from simulation.game_state import Bases, GameState, PlayResult
    from simulation.loop.boxscore import BoxScore, PlayerStatLine
    from simulation.loop.values import RunningGameTally, StealResolution


class LoopMixin:
    """The attributes and methods every loop mixin may use."""

    if TYPE_CHECKING:
        rng: np.random.Generator
        ibb_rates: dict[tuple[int, int, bool, bool], float] | None
        full_pool_sampler: Any
        manager: Any
        manager_draw: bool
        manager_decisions: list[dict]
        bullpen: dict | None
        boxscore: BoxScore | None
        running_game_tally: RunningGameTally
        _bb_platoon: bool
        _got_away: bool
        _last_pitch_got_away: bool
        _pending_steal: StealResolution | None
        _half_inning_error_outs_lost: int

        # --- the core (simulation/sim_loop.py) ---
        @staticmethod
        def _snapshot_bases(state: GameState) -> Bases: ...

        def _check_bases(self, bases: Bases, *, batter_id: int | None = None) -> None: ...

        def _commit_run_delta(
            self,
            state: GameState,
            result: PlayResult,
            *,
            event: str | None,
            result_hits: int,
            result_outs: int,
            result_runs: int,
            pre_outs: int,
            pre_bases: Bases,
            batter_reached: bool,
            runners_scored: int,
            runners_retired: int,
        ) -> None: ...

        def _end_of_pa(self, state: GameState, result: PlayResult) -> None: ...

        # --- the other mixins ---
        def _box_line(self, player_id: int) -> PlayerStatLine: ...

        def _resolve_got_away_advance(self, state: GameState, result: PlayResult) -> None: ...

        def _steal_order_active(self) -> bool: ...

        def _steal_opportunity_draw(
            self, state: GameState, *, pitch_class: str | None = None
        ) -> None: ...
