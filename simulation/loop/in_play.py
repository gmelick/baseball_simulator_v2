"""The ball in play: the fielding draw, the drawn row's transition and the
advancement draws.

The fielding draw takes one row of the batted-ball pool, hard-filtered to the
live base-out cell. The row IS the play: its event, its outs and every
runner's destination. Five extra-base movements are the runners' own
decisions, so the advancement draws decide them instead of the row.
"""

from __future__ import annotations

import numpy as np

from simulation.game_state import (
    OUTS_PER_INNING,
    Bases,
    GameState,
    PlayResult,
    Team,
)
from simulation.loop.base import LoopMixin
from simulation.loop.values import _POS_NUM_TO_STR, FieldingSignal, _venue_of


class InPlayMixin(LoopMixin):
    """The state machine's in-play methods."""

    def _full_pool_fielding(self, state: GameState) -> FieldingSignal | None:
        """SIM-425/511: draw a batted ball from the full bat_hand batted-ball
        pool (Batter + Situation weighting, hard-filtered to the live base-out
        cell) -> a FieldingSignal carrying the drawn row's whole transition.
        None when no full-pool sampler / batted-ball pool is wired (the
        count-machine-only mode then leaves the in-play pitch unresolved)."""
        fp = self.full_pool_sampler
        if fp is None or not fp.has_battedball():
            return None
        hand = state.bat_hand if state.bat_hand in fp.a.bb_pools else "R"
        season = int(getattr(state, "season", 2024) or 2024)
        bat = state.home_score if state.offense == Team.HOME else state.away_score
        fld = state.away_score if state.offense == Team.HOME else state.home_score
        sd = max(-5, min(5, int(bat) - int(fld)))
        sit = np.array(
            [state.balls, state.strikes, state.outs, state.runners_state, state.inning, sd],
            dtype=np.float32,
        )
        # SIM-413: pass the live pitcher hand so the batted-ball draw is softly
        # conditioned on the platoon matchup (no-op when the flag is off or the pool
        # / pitcher hand is unknown).
        pthrows = state.throw_hand if self._bb_platoon else None
        # SIM-491: pass the live batting side for the home-field draw weight.
        # A no-op unless the sampler's home_off_weight is set below 1.0
        # (SIM_HOME_OFF_WEIGHT) AND the pool carries bat_home (migration 0019).
        # SIM-491 part 2 (SIM-411): pass the live park run factor for the park
        # kernel. A no-op unless the sampler's park_sigma is set above 0
        # (SIM_PARK_KERNEL_SIGMA) AND the factory loaded a venue-factor map.
        # SIM-523: pass the FIELDING team's defense map for the fielder factor
        # (the per-position score matrices). A no-op on a bundle without them.
        defense = state.home_defense if state.defense == Team.HOME else state.away_defense
        # SIM-518 (SIM-472): the DRAWN pitch's geometry, so the batted ball can
        # agree with the pitch that produced it. Passed only when the kernel is
        # on (SIM_BB_PITCH_SIGMA > 0) — off, the call is unchanged.
        bb_extra: dict[str, object] = {}
        if float(getattr(fp, "bb_pitch_sigma", 0.0)) > 0.0:
            pg = fp.last_pitch_geom()
            if pg is not None:
                bb_extra["pitch_geom"] = pg
        # SIM-523 part B: the batted ball BORN in the pitch-result draw (the
        # result row's own, through the artifact's pitch-id join). Passed only
        # when its kernel is on (SIM_BB_BORN_SIGMA > 0) — off, the call is
        # unchanged.
        if (
            float(getattr(fp, "bb_born_sigma", 0.0)) > 0.0
            # SIM-523 part C: the class filter and the wall-zone rule read it too.
            or bool(getattr(fp, "bb_class_filter", False))
            or bool(getattr(fp, "park_wall_zone_only", False))
            or bool(getattr(fp, "fence_stage", False))
        ):
            reader = getattr(fp, "last_born_batted_ball", None)
            born = reader() if reader is not None else None
            if born is not None:
                bb_extra["born_bb"] = born
        # SIM-523 part C4: the live park for the fence stage (``state.park``
        # holds the venue id; passed only when the stage is on).
        if bool(getattr(fp, "fence_stage", False)):
            bb_extra["venue_id"] = _venue_of(state)
        fp.battedball_new_pa(
            hand,
            f"{state.batter_id}:{season}",
            sit,
            pitcher_throws=pthrows,
            bat_home=(state.offense == Team.HOME),
            park_run_factor=float(getattr(state, "park_run_factor", 1.0) or 1.0),
            defense_map=defense or None,
            live_season=season,
            **bb_extra,
        )
        ev, rh, _ro, la = fp.battedball_draw()
        # --- SIM-511: the transition path -----------------------------------
        # The draw was HARD-filtered to the live base-out cell and the drawn
        # row carries its whole transition: the row IS the play (event, outs,
        # WHO is out, all movement). SIM-486 deleted the legacy soft-draw
        # resolution, so a bundle without transition columns is a data
        # defect, not a fallback.
        tr = fp.last_transition()
        if tr is None:
            raise RuntimeError(
                "SIM-486: the batted-ball draw returned no transition — the bundle "
                "predates sim510.1 (no r1_dest / dest_ok / is_air columns). Rebuild "
                "the engine artifacts; there is no legacy resolution path."
            )
        live_fid, _pos = self._live_fielder_at_drawn_position(state, fp)
        return FieldingSignal(
            event=ev,
            result_hits=int(rh),
            result_outs=int(_ro),
            result_runs=0,
            launch_angle=float(la),
            is_error=(ev == "field_error"),
            fielder_id=live_fid,
            exit_velo=float(tr.get("ev", 0.0)),
            spray_angle=float(tr.get("spray", 0.0)),
            transition=tr,
        )

    def _live_fielder_at_drawn_position(
        self, state: GameState, fp
    ) -> tuple[int | None, str | None]:
        """SIM-511/512: the LIVE defender at the drawn pool row's position.

        The pool row says WHERE the ball went; the live defense map says WHO
        fields it in this game. Returns ``(fielder_id, position_name)`` —
        the attribution for the play record and the arm the SIM-512
        advancement draw conditions on. ``(None, None)`` when the bundle or
        the defense map lacks the data (the draws then skip the arm factor).
        """
        f = fp.last_battedball_fielder()
        if f is None:
            return None, None
        pos_str = _POS_NUM_TO_STR.get(int(f[0]))
        if pos_str is None:
            return None, None
        defense = state.home_defense if state.defense == Team.HOME else state.away_defense
        cur = defense.get(pos_str) if defense else None
        return (int(cur) if cur else None), pos_str

    @staticmethod
    def _normalized_dests(tr: dict, hit: int) -> dict[int, int]:
        """The drawn row's destinations with the five DISCRETIONARY movements
        clamped to station-to-station (design decision 1 — the double-count
        guard). The SIM-512 advancement draws are the sole authority on those
        extra bases; the row's own sends are the DATA for the opportunity
        pools, never applied twice.

        Keys 1/2/3 = the pre-pitch runner on that base; key 0 = the batter.
        Everything OUTSIDE the five scenarios is row truth at real data
        frequencies: forces, double plays, fielders' choices, doubled-off
        runners (dest 0 WITHOUT the advancing flag — no tag decision existed),
        productive ground-out advancement, a runner held on an infield hit,
        and a runner cut down at the plate from 3B on a single (not one of
        the five — station-to-station never converts a row out into a safe
        outside the enumerated movements).
        """
        d1, d2, d3, bd = int(tr["r1"]), int(tr["r2"]), int(tr["r3"]), int(tr["batter"])
        if hit == 1:  # a single
            if d1 >= 3 or (d1 == 0 and tr["adv1"]):
                d1 = 2  # scenario 1 — 1st -> 3rd on a single
            if d2 == 4 or (d2 == 0 and tr["adv2"]):
                d2 = 3  # scenario 2 — 2nd -> home on a single
            if bd >= 2 or bd == 0:
                bd = 1  # scenario 5 — the batter stretch
        elif hit == 2:  # a double
            if d1 == 4 or (d1 == 0 and tr["adv1"]):
                d1 = 3  # scenario 3 — 1st -> home on a double
            if bd >= 3 or bd == 0:
                bd = 2  # scenario 5 — the batter stretch
        elif hit == 0 and tr.get("is_air") and int(tr["batter"]) == 0:
            # scenario 4 — tag-ups on a caught ball, any runner.
            if d3 == 4 or (d3 == 0 and tr["adv3"]):
                d3 = 3
            if d2 >= 3 or (d2 == 0 and tr["adv2"]):
                d2 = 2
            if d1 >= 2 or (d1 == 0 and tr["adv1"]):
                d1 = 1
        return {1: d1, 2: d2, 3: d3, 0: bd}

    @staticmethod
    def _seat(seats: dict[int, int | None], want: int, rid: int) -> int:
        """Seat a body on ``want``, else the nearest open bag — downward first
        (can't-pass), then upward — else return 4: the body is forced home.

        A body is NEVER overwritten or dropped. The first lane run of the
        transition draw crashed the SIM-500 conservation guard here: a rare
        row whose normalized destinations collide cascaded a trailing runner
        down to first base, and the old fallback let the batter OVERWRITE
        him — one body vanished (before=2 + batter_reached=1, after=2 + 0 +
        0). An impossible normalized row now degrades to a forced-advance
        chain instead: every body keeps a bag or scores, so the conservation
        identity always balances. The shape is ~1-in-100k plate appearances;
        the upward/score branches are that tail, never the common path.
        """
        for b in (want, want - 1, want - 2, want + 1, want + 2):
            if 1 <= b <= 3 and seats[b] is None:
                seats[b] = rid
                return b
        return 4  # every bag taken: the chain forces the body home

    def _resolve_in_play_transition(
        self,
        state: GameState,
        result: PlayResult,
        sig: FieldingSignal,
        pre_outs: int,
        pre_bases: Bases,
    ) -> None:
        """SIM-511/512: the drawn transition row IS the play.

        The row was hard-filtered to the live base-out cell, so its per-base
        destinations apply 1:1 to the live runners: the event, the outs, WHO
        is out, and all forced/automatic movement come from the row. The five
        discretionary movements were normalized to station-to-station
        (:meth:`_normalized_dests`); the SIM-512 advancement draws below are
        the sole authority on those extra bases.

        This is the ONLY in-play path (SIM-486 deleted the phantom-DP guard,
        the ``outs = 0 if rh else 1`` inference, the uniform runner push, the
        hand-set extra-advance and tag-up constants, and the SIM-349 sac-fly
        nudge — a tag draw from 3B produces sacrifice flies naturally).
        ``runners_retired`` is REAL here — the SIM-494 out-count-versus-bodies
        identity is checked at the commit by ``Bases.assert_transition``.
        """
        tr = sig.transition or {}
        hit = int(sig.result_hits)
        event_label = sig.event
        # A drawn sac-fly row's score was normalized away; the S4 tag draw
        # re-decides it and relabels below.
        if event_label in ("sac_fly", "sacrifice_fly"):
            event_label = "field_out"
        ndest = self._normalized_dests(tr, hit)
        # --- 1. apply the row to the LIVE runners ---------------------------
        # The batter seats FIRST: his bag on a hit is law (a single puts him
        # on first). Runners then seat lead-first; a rare colliding row falls
        # through _seat's no-body-lost ladder (down, up, home).
        seats: dict[int, int | None] = {1: None, 2: None, 3: None}
        advances: dict[int, int] = {}
        runs = 0
        runners_retired = 0  # bodies removed from BASES (the batter counts
        # here only when he reached first — assert_transition's contract)
        bid = state.batter_id if state.batter_id is not None else -1
        bd = ndest[0]
        batter_reached = bd >= 1
        plate_out = 1 if bd == 0 else 0
        if bd == 4:
            runs += 1
            advances[bid] = 0
        elif bd >= 1:
            placed = self._seat(seats, bd, bid)
            if placed >= 4:
                runs += 1
                advances[bid] = 0
            else:
                advances[bid] = placed
        for frm, rid in ((3, pre_bases.third), (2, pre_bases.second), (1, pre_bases.first)):
            if rid is None:
                continue
            d = ndest[frm]
            if d < 0:
                d = frm  # no pool runner on this base — impossible under the
                # hard filter; hold the live runner (defensive)
            if d == 4:
                runs += 1
                advances[rid] = 0
            elif d == 0:
                runners_retired += 1
            else:
                placed = self._seat(seats, d, rid)
                if placed >= 4:
                    runs += 1
                    advances[rid] = 0
                else:
                    advances[rid] = placed
        state.bases = Bases(first=seats[1], second=seats[2], third=seats[3])
        self._check_bases(state.bases)
        # --- 2. the five-scenario advancement draws (SIM-512) --------------
        outs_now = int(pre_outs) + runners_retired + plate_out
        adv_runs = adv_retired = 0
        if outs_now < OUTS_PER_INNING:
            adv_runs, adv_retired, event_label = self._run_advancement_draws(
                state,
                result,
                sig,
                tr,
                hit,
                pre_bases,
                pre_outs,
                outs_now,
                advances,
                event_label,
            )
            runs += adv_runs
            runners_retired += adv_retired
        # --- 3. record + commit --------------------------------------------
        result.event = event_label
        result.fielder_id = sig.fielder_id
        result.is_error = sig.is_error
        result.exit_velo = sig.exit_velo
        result.launch_angle = sig.launch_angle
        result.spray_angle = sig.spray_angle
        if advances:
            result.baserunner_advances.update({k: v for k, v in advances.items() if k != -1})
        self._commit_run_delta(
            state,
            result,
            event=event_label,
            result_hits=hit,
            result_outs=int(runners_retired + plate_out),
            result_runs=int(runs),
            pre_outs=pre_outs,
            pre_bases=pre_bases,
            batter_reached=batter_reached,
            runners_scored=int(runs),
            runners_retired=int(runners_retired),
        )

    def _run_advancement_draws(
        self,
        state: GameState,
        result: PlayResult,
        sig: FieldingSignal,
        tr: dict,
        hit: int,
        pre_bases: Bases,
        pre_outs: int,
        outs_now: int,
        advances: dict[int, int],
        event_label: str,
    ) -> tuple[int, int, str]:
        """SIM-512: the per-runner attempt→outcome draws for the five
        discretionary scenarios. Returns ``(runs, retired, event_label)`` and
        mutates ``state.bases`` + ``advances`` in place.

        Lead-first with can't-pass occupancy; if the lead runner does not
        attempt, no trailing draws — EXCEPT the batter-stretch draw, which
        stands alone (advance-on-the-throw is folded into it, an accepted
        approximation). Every advancement out is a TAG play, so Rule 5.08
        timing is free: runs banked by earlier (lead) resolutions stand when
        a trailing tag-out ends the inning, and no draw fires once the third
        out is recorded. A tag from 3B that scores on a caught ball relabels
        the play ``sacrifice_fly`` post-hoc (AB/RBI per SIM-312).
        """
        fp = self.full_pool_sampler
        if fp is None or not fp.has_advancement():
            return 0, 0, event_label
        season = int(getattr(state, "season", 2024) or 2024)
        fid, pos_str = self._live_fielder_at_drawn_position(state, fp)
        fielder_key = f"{fid}:{pos_str}:{season}" if fid and pos_str else None
        ev = float(tr.get("ev", 0.0))
        la = float(sig.launch_angle or 0.0)
        spray = float(tr.get("spray", 0.0))
        dist = float(tr.get("dist", 0.0))
        seats: dict[int, int | None] = {
            1: state.bases.first,
            2: state.bases.second,
            3: state.bases.third,
        }
        runs = retired = 0
        outs = int(outs_now)
        bid = state.batter_id if state.batter_id is not None else -1

        def draw(scen: int, frm: int, tgt: int, rid: int):
            return fp.advancement_draw(
                scen,
                frm,
                tgt,
                f"{int(rid)}:{season}",
                fielder_key,
                outs=int(pre_outs),
                exit_velo=ev,
                launch_angle=la,
                spray_angle=spray,
                hit_distance=dist,
            )

        def resolve(rid: int, cur: int, safe_to: int, extra_to: int | None, res) -> bool:
            """Apply one draw. Returns True when the runner ATTEMPTED."""
            nonlocal runs, retired, outs
            if res is None:
                return False
            attempted, safe, extra = res
            if not attempted:
                return False
            seats[cur] = None
            if extra and extra_to is not None:
                place = extra_to
            elif safe or extra:
                place = safe_to
            else:
                retired += 1
                outs += 1
                advances.pop(rid, None)
                return True
            if place >= 4:
                runs += 1
                advances[rid] = 0
            else:
                placed = self._seat(seats, place, rid)
                if placed >= 4:
                    runs += 1
                    advances[rid] = 0
                else:
                    advances[rid] = placed
            return True

        if hit == 1:  # a single
            lead_declined = False
            r2 = pre_bases.second
            if r2 is not None and seats[3] == r2 and outs < OUTS_PER_INNING:
                lead_declined = not resolve(r2, 3, 4, None, draw(2, 2, 4, r2))
            r1 = pre_bases.first
            if (
                r1 is not None
                and seats[2] == r1
                and seats[3] is None
                and not lead_declined
                and outs < OUTS_PER_INNING
            ):
                lead_declined = not resolve(r1, 2, 3, 4, draw(1, 1, 3, r1))
            if bid != -1 and seats[1] == bid and seats[2] is None and outs < OUTS_PER_INNING:
                resolve(bid, 1, 2, 3, draw(5, 0, 2, bid))
        elif hit == 2:  # a double
            lead_declined = False
            r1 = pre_bases.first
            if r1 is not None and seats[3] == r1 and outs < OUTS_PER_INNING:
                lead_declined = not resolve(r1, 3, 4, None, draw(3, 1, 4, r1))
            if bid != -1 and seats[2] == bid and seats[3] is None and outs < OUTS_PER_INNING:
                resolve(bid, 2, 3, 4, draw(5, 0, 3, bid))
        elif hit == 0 and tr.get("is_air") and int(tr.get("batter", -1)) == 0:
            lead_declined = False
            r3 = pre_bases.third
            if r3 is not None and seats[3] == r3 and outs < OUTS_PER_INNING:
                went = resolve(r3, 3, 4, None, draw(4, 3, 4, r3))
                if went and advances.get(r3) == 0:
                    # The tag from 3B scored on a caught ball: the play IS a
                    # sacrifice fly (no AB, RBI credited — SIM-312 vocab).
                    event_label = "sacrifice_fly"
                lead_declined = not went
            r2 = pre_bases.second
            if (
                r2 is not None
                and seats[2] == r2
                and seats[3] is None
                and not lead_declined
                and outs < OUTS_PER_INNING
            ):
                lead_declined = not resolve(r2, 2, 3, 4, draw(4, 2, 3, r2))
            r1 = pre_bases.first
            if (
                r1 is not None
                and seats[1] == r1
                and seats[2] is None
                and not lead_declined
                and outs < OUTS_PER_INNING
            ):
                resolve(r1, 1, 2, 3, draw(4, 1, 2, r1))
        state.bases = Bases(first=seats[1], second=seats[2], third=seats[3])
        self._check_bases(state.bases)
        return runs, retired, event_label

    def _resolve_in_play(self, state: GameState, result: PlayResult) -> None:
        """Resolve an in-play PA: the batted-ball transition draw (step 5/6) ->
        the advancement draws (step 7) -> ONE ``resolve_runs`` commit (§8).

        The drawn row IS the play (SIM-511): :meth:`_full_pool_fielding` draws
        it from the live base-out cell and :meth:`_resolve_in_play_transition`
        applies its destinations to the live runners, runs the SIM-512
        advancement draws for the discretionary extra bases, and commits.

        In count-machine-only mode (no sampler, or a bundle with no batted-ball
        pool) the in-play outcome is left as an unresolved terminal
        (``event=None``) so the count machine can still be driven alone.
        """
        result.pa_terminal = True
        # SIM-499: measure the base-out state HERE, at the top, above everything.
        # _resolve_in_play_transition rebuilds the whole base state, and the
        # ledger needs the state as it was before it. Placing this snapshot
        # below the mutator is a trap that has been walked into once already.
        pre_outs = int(state.outs)
        pre_bases = self._snapshot_bases(state)
        sig = self._full_pool_fielding(state)
        if sig is None:
            # Count-machine-only mode: terminal in-play, resolution handed off.
            result.event = None
            return
        self._resolve_in_play_transition(state, result, sig, pre_outs, pre_bases)
