"""The manager's decisions: the leverage index, the intentional walk and the
pitching change.

Each decision is a draw. The intentional walk rolls the loop's random number
against the measured rate of the plate appearance's cell. The pitching change
is a draw from the change opportunity pool, and the entering arm a second draw
from the live pen. With no manager wired, every hook does nothing.
"""

from __future__ import annotations

from simulation.game_state import (
    GameState,
    Half,
    Team,
)
from simulation.loop.base import LoopMixin
from simulation.loop.values import _HIGH_LEVERAGE, _LATE_INNING


class ManagerMixin(LoopMixin):
    """The state machine's manager methods."""

    @staticmethod
    def compute_leverage(state: GameState) -> float:
        """A simple leverage index (LI) for the current base-out, score and
        inning spot.

        LI is the standard sabermetric idea: how much more the average event
        in this spot swings the win probability than an event in a neutral
        spot (LI 1.0). The real LI needs a win-expectancy table; this proxy
        multiplies the three drivers that dominate it, so the decisions stay
        transparent and need no database:

          * ``inning``: late innings raise LI, from ~0.6 in the 1st to ~2.0 by
            the 9th.
          * ``score_diff``: LI peaks in a tie or one-run game and decays as
            the margin grows: ``1 / (1 + |diff| * k)``.
          * the base-out state: runners on and fewer outs raise LI.

        The product is clamped to ``[0.05, 6.0]`` and is monotone in each
        driver, the one property the decisions rely on. The result is also
        written to ``state.manager.leverage``.
        """
        # Inning factor: ramps from ~0.6 in the 1st to ~2.0 by the 9th, flat after.
        inning = max(1, int(state.inning))
        inning_factor = 0.6 + 0.18 * min(inning - 1, 8)
        # Score factor: peaks at a tie (1.0) and decays with the margin.  Caps the
        # margin at 8 so a blowout floors the factor instead of vanishing.
        margin = min(abs(int(state.score_diff)), 8)
        score_factor = 1.0 / (1.0 + 0.55 * margin)
        # Base/out factor: more runners + fewer outs == more leverage.  A weighted
        # base count (runner nearer to scoring counts more) times the outs term.
        b = state.bases
        base_weight = (
            (0.9 if b.first is not None else 0.0)
            + (1.1 if b.second is not None else 0.0)
            + (1.3 if b.third is not None else 0.0)
        )
        outs = max(0, min(int(state.outs), 2))
        base_out_factor = 1.0 + 0.45 * base_weight * (3 - outs) / 3.0
        li = inning_factor * score_factor * base_out_factor
        li = float(max(0.05, min(li, 6.0)))
        state.manager.leverage = li
        return li

    def _manager_rng(self) -> float:
        """One [0, 1) draw from the machine's loop rng for a manager decision,
        so a fixed seed makes every decision deterministic."""
        return float(self.rng.random())

    def _pre_pitch_hook(self, state: GameState) -> None:
        """The decisions before every pitch: the intentional walk, then the
        single pre-pitch steal draw.

          1. **The intentional walk**: on a plate appearance's first pitch, a
             draw at the measured rate of its cell
             (:meth:`_should_issue_ibb`). A walk sets
             ``state.manager.intentional_walk_signalled``, and the count
             machine never runs on that pitch: the loop issues the walk.
          2. **The steal**: without the running game on the pitch, the steal
             opportunity draw runs here, before the pitch
             (:meth:`_steal_opportunity_draw`), unless a steal is already
             staged. With it, :meth:`step_pitch` runs the pickoff and the
             steal draws itself.

        With no manager wired no intentional walk fires; with no steal pool
        the draw stages nothing.
        """
        li = self.compute_leverage(state)
        mgr = state.manager
        # Reset the per-pitch signal: it applies to THIS pitch only.
        mgr.intentional_walk_signalled = False

        # --- (1) The intentional walk: the cell draw ---------------------------
        if self._should_issue_ibb(state):
            mgr.intentional_walk_signalled = True
            self.manager_decisions.append(
                {
                    "kind": "intentional_walk",
                    "inning": int(state.inning),
                    "leverage": li,
                    "batter_id": state.batter_id,
                }
            )
            return

        # --- (2) The single pre-pitch steal draw --------------------------------
        # A staged steal (a test's) stands. The decision is a draw from the
        # steal opportunity pool, where the manager's aggression is a WEIGHT on
        # attempted rows, never a gate in front of the draw: a gated chain once
        # attempted ZERO steals in production for ten weeks (SIM-495).
        if self._pending_steal is not None:
            return
        # In the running game on the pitch, step_pitch draws the pickoff
        # before the pitch and the steal after it, in the pitch's class group.
        if self._steal_order_active():
            return
        self._steal_opportunity_draw(state)

    def _should_issue_ibb(self, state: GameState) -> bool:
        """Decide an intentional walk: a draw at the REAL rate of the plate
        appearance's cell.

        ``sim.ibb_rates`` holds, for every (runners_state, outs, late, close)
        cell, how many real plate appearances entered it and how many were
        intentionally walked, over the pool's season window. One roll of the
        loop rng against ``issued / opportunities``, never a hand-tuned
        formula. The cell (late x close x base-out) IS the leverage context,
        measured instead of modelled.

        The draw fires ONCE per plate appearance, on its first pitch: a per-pitch
        roll compounds to 2.64x MLB's intentional-walk volume (SIM-429). A cell
        absent from the table has a measured rate of about 0 and never fires.
        With no manager wired, or no rate table loaded (a no-database test
        machine), no intentional walk fires.
        """
        if self.manager is None:
            return False
        rates = self.ibb_rates
        if not rates:
            return False
        # Once per PA: the first pitch is the only 0-0 pitch.
        if int(state.balls) != 0 or int(state.strikes) != 0:
            return False
        cell = (
            int(state.runners_state),
            int(state.outs),
            int(state.inning) >= _LATE_INNING,
            abs(int(state.home_score) - int(state.away_score)) <= 1,
        )
        rate = float(rates.get(cell, 0.0))
        if rate <= 0.0:
            return False
        return self._manager_rng() < rate

    def _start_of_pa_hook(self, state: GameState) -> None:
        """The decision at the START of a plate appearance, on its first
        pitch, before the intentional walk and the steal: the pitching change.

        The change is a draw from the change opportunity pool with the live
        manager's usage similarity as a weight, and the entering arm a draw
        from the live pen (the owner's decision of 2026-09-13). With
        ``self.manager is None`` nothing fires, so the no-database full-game
        tests run with no change.
        """
        if self.manager is None:
            return None
        li = self.compute_leverage(state)
        self._maybe_pull_starter(state, li)
        return None

    def _maybe_pull_starter(self, state: GameState, li: float) -> None:
        """The pitching change: a DRAW from the change opportunity pool at the
        pool's own rate for this boundary's cell, the live manager's usage
        similarity a weight. No floor, no ceiling, no formula. With the draw
        switch off (``manager_draw``), or no pool in the bundle, no change is
        drawn; the no-database synthetic bundle carries its own change pool.
        """
        if not self.manager_draw:
            return
        if self._pitching_change_by_draw(state):
            self._change_pitcher(state, li, forced=False, source="draw")

    def _pitching_change_by_draw(self, state: GameState) -> bool | None:
        """The boundary's inputs for the sampler's change draw: the current
        pitcher's key, whether he is the side's starter, whether this is the
        first plate appearance of the half, his pitches and batters faced so
        far, the situation from the fielding side's view and the fielding
        manager's key. None when the sampler has no change draw."""
        fp = self.full_pool_sampler
        draw = getattr(fp, "pitching_change_draw", None)
        if fp is None or draw is None or state.pitcher_id is None:
            return None
        pid = int(state.pitcher_id)
        defense = state.defense
        starter = state.home_starter_id if defense == Team.HOME else state.away_starter_id
        is_starter = starter is None or int(starter) == pid
        fld = state.home_score if defense == Team.HOME else state.away_score
        bat = state.away_score if defense == Team.HOME else state.home_score
        season = int(getattr(state, "season", 2024) or 2024)
        # The FIELDING side's manager keys his usage-similarity row.
        mid = state.home_manager_id if defense == Team.HOME else state.away_manager_id
        return draw(
            f"{pid}:{season}",
            is_starter=is_starter,
            new_half=int(state.half_pa_count) == 0,
            pitch_count=int(state.pitcher_pitch_count),
            batters_faced=int(state.pitcher_bf.get(pid, 0)),
            inning=int(state.inning),
            outs=int(state.outs),
            runners_state=int(state.runners_state),
            score_diff=int(fld) - int(bat),
            manager_key=(f"{int(mid)}:{season}" if mid is not None else None),
        )

    def _change_pitcher(self, state: GameState, li: float, *, forced: bool, source: str) -> None:
        """Bring in the reliever ``_pick_reliever`` selects and record the
        change in ``manager_decisions``. With no arm available the pitcher
        stays."""
        new_arm = self._pick_reliever(state, li)
        if new_arm is None:
            return  # bullpen empty -> degrade gracefully (stay with the starter).
        old = state.pitcher_id
        out_pitches = int(state.pitcher_pitch_count)
        out_bf = int(state.pitcher_bf.get(old, 0)) if old is not None else 0
        new_half = int(state.half_pa_count) == 0
        state.pitcher_id = int(new_arm)
        state.pitcher_pitch_count = 0  # fresh arm
        if state.half == Half.TOP:
            state.home_pitcher_id = int(new_arm)
        else:
            state.away_pitcher_id = int(new_arm)
        self.manager_decisions.append(
            {
                "kind": "pitching_change",
                "inning": int(state.inning),
                "leverage": li,
                "out_pitcher_id": old,
                "in_pitcher_id": int(new_arm),
                "forced": forced,
                "source": source,
                # The boundary's facts, for the manager probe and the lane.
                "pitch_count": out_pitches,
                "batters_faced": out_bf,
                "new_half": new_half,
            }
        )

    def _pick_reliever(self, state: GameState, li: float) -> int | None:
        """Choose the entering arm from the defending team's pen.

        The arm is a second draw from the live pen: each candidate's
        resemblance to the arm the drawn pool row brought in, in role, stuff,
        recent usage and hand (:meth:`FullPoolSampler.relief_arm_draw`). With
        every weight off, or no sampler, the pick is positional: in a
        high-leverage late spot the first arm (the closer, the arm the box
        listed first), otherwise the last. Pops the chosen arm so it cannot
        enter twice. None when no arm is available.
        """
        pen_map = state.manager.bullpen_available or {}
        team = state.defense
        arms = pen_map.get(team)
        if arms is None:
            arms = pen_map.get(int(team))
        if not arms:
            return None
        fp = self.full_pool_sampler
        draw = getattr(fp, "relief_arm_draw", None)
        if fp is not None and draw is not None:
            i = draw(
                list(arms),
                live_season=int(getattr(state, "season", 2024) or 2024),
                throw_hands=state.throw_hands,
                recent_usage=state.pitcher_recent_usage,
            )
            if i is not None:
                return int(arms.pop(int(i)))
        high_lev_late = li >= _HIGH_LEVERAGE and int(state.inning) >= _LATE_INNING
        if high_lev_late:
            return int(arms.pop(0))  # closer / highest-leverage arm.
        return int(arms.pop())  # middle reliever from the back.
