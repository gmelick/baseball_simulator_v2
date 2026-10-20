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
        """Compute a simple Leverage Index (LI) for the current base-out-score-
        inning spot (SIM-323; spec step 1 'leverage').

        LI is the standard sabermetric concept (Tango/Inside-The-Book): how much
        the *swing in win probability* of the average event in this spot exceeds
        that of a neutral spot (LI == 1.0 average).  Computing the real WP-derived
        LI needs a win-expectancy table; we deliberately use a documented,
        monotone proxy built from the three drivers that dominate it, so the
        decision gating is transparent and DB-free:

          * ``inning``     — late innings raise LI (a run swings WP more in the 9th
            than the 1st): a factor rising from ~0.6 (1st) toward ~2.0 (9th+).
          * ``score_diff`` — LI peaks in a *tie / one-run* game and decays as the
            margin grows (a blowout is low leverage): ``1 / (1 + |diff| * k)``.
          * base/out state — runners on + fewer outs raise LI (more ways the spot
            turns into runs): scaled by base occupancy and (3 - outs).

        The product is clamped to ``[0.05, 6.0]``.  It is *monotone* in the obvious
        directions (later inning -> higher; bigger lead -> lower; more runners /
        fewer outs -> higher), which is the only property the gating relies on.
        Result is also written to ``state.manager.leverage`` so downstream reads
        (spec step 1) see a populated LI.
        """
        # Inning factor: ramps from ~0.6 in the 1st to ~2.0 by the 9th, flat after.
        inning = max(1, int(state.inning))
        inning_factor = 0.6 + 0.18 * min(inning - 1, 8)
        # Score factor: peaks at a tie (1.0) and decays with the margin.  Caps the
        # margin at 6 so a blowout floors the factor instead of vanishing.
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
        """A single [0, 1) draw from the machine's loop rng for a manager decision
        (so a fixed seed makes every manager decision deterministic, spec §6.3)."""
        return float(self.rng.random())

    def _pre_pitch_hook(self, state: GameState) -> None:
        """Pre-pitch manager decisions (§3): IBB, pitch-out, the steal
        **green-light**, then the **steal initiate** wiring (SIM-319).

        Order (all gated by the live Leverage Index + a manager tendency):

          1. **IBB** (§3 item 2) — with first base OPEN and a runner in scoring
             position, a close & late high-leverage spot, an above-average
             ``platoon_advantage_exploitation_rate`` manager will signal an
             intentional walk (``ManagerContext.intentional_walk_signalled``); the
             count machine never runs on that pitch — the loop issues the walk.
          2. (The pitch-out and the leverage-scaled steal green-light that sat
             here were deleted 2026-09-13 — SIM-427; the steal draw's manager
             weight is the batting manager's measured rate over the league's.)

        The steal *decision* is the SIM-474 opportunity draw
        (:meth:`_steal_opportunity_draw`, which stages the
        :class:`StealResolution`); the *outcome* is committed in step 7
        (:meth:`_resolve_steal_outcome`).  A test may instead call
        :meth:`stage_steal` directly.

        No-op-safe: with no manager profile wired (``self.manager is None``) every
        tendency reads 0.0, so no IBB / pitch-out fires; with no steal pool the
        draw stages nothing.
        """
        li = self.compute_leverage(state)
        mgr = state.manager
        # Reset the per-pitch signal (it applies to THIS pitch only). SIM-427
        # (owner decision 2026-09-13) deleted the pitch-out and hit-and-run
        # signals with their hooks: neither had a consumer that changed a play,
        # and the hit-and-run's only effect was to skip the steal draw.
        mgr.intentional_walk_signalled = False

        # --- (1) Intentional walk: the SIM-515 cell draw --------------------------
        if self._should_issue_ibb(state, li):
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

        # --- steal initiate wiring (SIM-474) -----------------------------------
        # Only auto-stage if a test has not already staged one. The old chain
        # GATED the decision on a green-light RNG draw and then consulted a
        # resolver production never wired — so production attempted ZERO steals
        # from 2026-06-04 to 2026-08-16 (SIM-495 measured SB 0.0000 vs 0.59).
        # Now the decision is a similarity-weighted draw from the SIM-468
        # opportunity pool, where manager aggression is a WEIGHT on attempted
        # rows — never a gate in front of the draw.
        if self._pending_steal is not None:
            return
        # SIM-554: in the running game on the pitch the pickoff draw runs in
        # step_pitch before the pitch and the steal draw after it, in the
        # pitch's class group. The single draw here runs only without it.
        if self._steal_order_active():
            return
        self._steal_opportunity_draw(state)

    def _should_issue_ibb(self, state: GameState, li: float) -> bool:
        """Decide an intentional walk (§3 item 2) — SIM-515.

        The decision is a draw at the REAL rate of the PA's hard-filtered cell:
        ``sim.ibb_rates`` holds, for every (runners_state, outs, late, close)
        cell, how many real PAs entered it and how many were intentionally
        walked (built over the pool's season window). One roll of the loop rng
        against ``issued / opportunities`` — never a hand-tuned formula (the
        owner's 2026-08-10 architecture rule).

        The draw fires ONCE per plate appearance, on its first pitch (the old
        formula re-rolled per pitch and compounded to 2.64x MLB's IBB volume —
        the SIM-429 diagnosis). A cell absent from the table has a measured
        rate of ~0 and never fires. With no manager wired, or no rate table
        loaded (a no-DB test machine), no IBB fires — the no-op-safe contract.

        ``li`` is unused since SIM-515: the cell (late x close x base-out) IS
        the leverage context, measured instead of modeled. Per-manager
        modulation returns as a similarity WEIGHT when the SIM-427 real
        profiles land (a weight, never a gate — the SIM-474 pattern).
        """
        del li  # SIM-515: the cell replaces the leverage heuristic.
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
        """SIM-523 part D: the manager decisions at the START of a plate
        appearance (the loop's step 1) — the pitching change, the pinch hit
        and the bunt setup — evaluated once, on the first pitch, before the
        intentional-walk and steal decisions. The body is the former
        end-of-plate-appearance hook; ``_end_of_pa_hook`` stays as its alias."""
        return self._end_of_pa_hook(state)

    def _end_of_pa_hook(self, state: GameState) -> None:
        """The manager decision at a plate-appearance boundary: the pitching
        change (the loop's step 1), evaluated once per plate appearance on its
        first pitch (SIM-523 part D). Since 2026-09-13 (SIM-427) it is a draw
        from the change opportunity pool with the live manager's usage
        similarity as a weight, and the entering arm is a draw from the live
        pen; the pull formula, the pinch hit and the sac-bunt setup are
        deleted. No-op-safe: with ``self.manager is None`` nothing fires (the
        SIM-320/324/326 no-DB full-game tests stay green).
        """
        if self.manager is None:
            return None
        # SIM-427 (owner decision 2026-09-13): the pitching change is the one
        # decision here — the pinch hit and the bunt setup are deleted (no
        # bench was ever wired; the bunt changed no play). (SIM-349's
        # _maybe_sac_fly_intent sat here until 2026-08-19 — retired by
        # SIM-513; the SIM-512 tag draw owns sacrifice flies.)
        li = self.compute_leverage(state)
        self._maybe_pull_starter(state, li)
        return None

    def _maybe_pull_starter(self, state: GameState, li: float) -> None:
        """The pitching change (the loop's step 1) — a DRAW from the change
        opportunity pool at the pool's own rate for this boundary's cell, the
        live manager's usage similarity a weight (SIM-427). No floor, no
        ceiling, no formula: the SIM-434 pull formula was deleted at the flip
        (2026-09-13; it pulled every starter at ~81 pitches with a spread of
        1.6). With the draw switch off, or no pool in the bundle, no change is
        ever drawn — the no-DB synthetic bundle carries its own change pool.
        """
        if not self.manager_draw:
            return
        if self._pitching_change_by_draw(state):
            self._change_pitcher(state, li, forced=False, source="draw")

    def _pitching_change_by_draw(self, state: GameState) -> bool | None:
        """SIM-523 part D: the boundary's inputs for the sampler's draw —
        the current pitcher's key, whether he is the side's starter, whether
        this is the first plate appearance of the half, his pitches and
        batters faced so far, and the situation from the fielding side's
        view. None when the sampler has no pool."""
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
        # SIM-427: the FIELDING side's manager keys his usage-similarity row.
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
        change (``source`` is "draw" since the flip). No arm: stay with the pitcher."""
        new_arm = self._pick_reliever(state, li, source=source)
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
                # SIM-523 part D: the boundary's facts for the probe and the lane.
                "pitch_count": out_pitches,
                "batters_faced": out_bf,
                "new_half": new_half,
            }
        )

    def _pick_reliever(self, state: GameState, li: float, *, source: str = "draw") -> int | None:
        """Choose the entering arm from the defending team's pen.

        SIM-427 (owner decision 2026-09-13): the arm is a second draw from the
        live pen — the candidate's resemblance to the arm the drawn pool row
        brought in, in role, stuff, recent usage and hand
        (:meth:`FullPoolSampler.relief_arm_draw`). Every weight off, or no
        sampler, keeps the positional pick: high leverage late -> the first arm
        (the closer / the arm the box listed as used first), otherwise from the
        back. The SIM-434 ranking was deleted at the flip. Pops the chosen arm
        so it cannot enter twice. None when no arm is available.
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
