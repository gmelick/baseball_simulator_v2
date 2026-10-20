"""The loop's constants, value objects and count machine.

Nothing here touches a :class:`~simulation.game_state.GameState`. The value
objects carry a drawn play (:class:`FieldingSignal`), a staged steal or pickoff
(:class:`StealResolution`), the running game's counters
(:class:`RunningGameTally`) and one count step (:class:`CountAdvance`).
:func:`advance_count` is the count machine: a pure function the state machine
calls once per pitch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from simulation.game_state import (
    BALLS_FOR_WALK,
    PITCH_OUTCOMES,
    STRIKES_FOR_STRIKEOUT,
)
from simulation.game_state import (
    CONTACT_PITCH_OUTCOME as _GS_CONTACT_PITCH_OUTCOME,
)

#: The single pitch outcome that means the ball was put in play -> resolve a
#: batted ball.  Mirrors the ``outcome_type`` vocabulary the SIM-301 builder
#: writes ("ball", "called_strike", "swinging_strike", "foul", "in_play") and
#: is re-exported from the SIM-311 GameState contract for one source of truth.
CONTACT_PITCH_OUTCOME = _GS_CONTACT_PITCH_OUTCOME

#: PA-event strings the §5.1 count machine produces for non-contact terminals.
EVENT_WALK = "walk"
EVENT_STRIKEOUT = "strikeout"
#: SIM-509: a hit-by-pitch is its own terminal — the same force mechanics as a
#: walk, but canonically ``hit_by_pitch`` so the box and the BB band never
#: count it as a walk.
EVENT_HIT_BY_PITCH = "hit_by_pitch"
#: SIM-515/516: the IBB's OWN canonical event. The box counts it as a BB and a
#: non-AB exactly like a ball-4 walk (``_BB_CANONICAL`` / ``_NON_AB_CANONICAL``
#: both hold it), but the lane's pool bands grade the two classes against
#: DIFFERENT references (the count-machine chain vs ``sim.ibb_rates``), so the
#: label must carry the truth. Emitting ``walk`` here zeroed the IBB_PA band's
#: numerator and pushed the surplus into BB_PA — the first certifying lane
#: caught exactly that.
EVENT_INTENTIONAL_WALK = "intentional_walk"

#: Marker the count machine emits while a PA is still live (no terminal yet).
#: Distinct from a real PA event so callers / run-resolution treat it as 0 runs.
EVENT_IN_PROGRESS = "in_progress"

# ---------------------------------------------------------------------------
# SIM-319 — fielding / baserunning / steal resolution (steps 6/7 + §5.4)
# ---------------------------------------------------------------------------
#
# WHAT SIM-319 OWNS (spec §2 steps 6/7, §3 steal, §5.4 dropped-3rd-strike):
#
#   * Step 6 (fielding): on an in-play batted ball, turn the sampled event +
#     the fielder/catcher RBF signal into outs / errors / hits.
#   * Step 7 (baserunning): advance the runners on the bases per the resolved
#     event + the baserunner RBF signal, scoring runs.
#   * Steals: the *decision* is made in the pre-pitch hook (§3); the *outcome*
#     (safe / caught) is resolved here in step 7 against the stolen-base pool.
#   * Dropped third strike (§5.4): a swinging strike-3 the catcher does not
#     hold lets the batter try for first base when 1B is open OR there are 2 outs.
#
# THE LOCKED BOUNDARIES (carried from SIM-312 / the spec):
#   * EVERY run value AND base-out delta is produced by
#     ``simulation.run_resolution.resolve_runs`` -- never inline arithmetic.
#     The loop hands ``resolve_runs`` the *sampled* ``result_hits/outs/runs``
#     deltas + the current base-out state and commits exactly what it returns.
#   * Engines stay distance-/similarity-pure.  The loop NEVER imports an engine
#     by module path nor turns a distance into a weight -- it consumes the
#     drawn pool row as a bounded :class:`FieldingSignal` from the full-pool
#     sampler (SIM-486: the one and only in-play source; a test hands the
#     machine a synthetic bundle instead of a resolver).
#   * The steal is a draw from the SIM-468 opportunity pool
#     (:meth:`StateMachine._steal_opportunity_draw`); a test stages one
#     explicitly through :meth:`StateMachine.stage_steal`.

#: Statcast batted-ball event strings that are pure outs (no base reached). The
#: canonical resolver (constants.resolve_event_to_canonical) maps the long
#: Statcast vocabulary; this set is only the loop's quick "did the batter reach"
#: test for the result_hits delta.
_OUT_EVENTS: frozenset[str] = frozenset(
    {
        "field_out",
        "force_out",
        "fielders_choice",
        "fielders_choice_out",
        "other_out",
        "strikeout",
        "sac_fly",
        "sacrifice_fly",
        "sac_bunt",
        "sacrifice_hit",
        "grounded_into_double_play",
        "ground_into_double_play",
        "double_play",
        "triple_play",
        "strikeout_double_play",
    }
)

#: Events that record TWO outs on the play (the batter + a runner).
_DOUBLE_PLAY_EVENTS: frozenset[str] = frozenset(
    {
        "grounded_into_double_play",
        "ground_into_double_play",
        "double_play",
        "strikeout_double_play",
        "sac_fly_double_play",
        "sac_bunt_double_play",
    }
)

#: Steal-outcome strings recorded on ``PlayResult.steal_outcome`` (spec §3/§7).
STEAL_SAFE = "safe"
STEAL_CAUGHT = "caught"

#: Base order for the "next base" mapping used by steal / forced advances.
_NEXT_BASE = {1: 2, 2: 3, 3: 4}  # 4 == home (scores)

#: Late-inning threshold (7th+) at which leverage-sensitive bullpen / pinch
#: decisions engage (spec §5.3; the manager-similarity ``*_late_innings`` cut).
_LATE_INNING = 7
#: The leverage index above which a spot is "high leverage" (the LI > 1.5 cut the
#: manager-similarity features use for sac-bunt / pinch-hit gating).
_HIGH_LEVERAGE = 1.5

#: SIM-425b: scorekeeping position number (sim.outcome_pool.fielded_by_position) ->
#: the position string the fielder embedding is keyed by (player_id:position:season).
_POS_NUM_TO_STR: dict[int, str] = {
    1: "P",
    2: "C",
    3: "1B",
    4: "2B",
    5: "3B",
    6: "SS",
    7: "LF",
    8: "CF",
    9: "RF",
}
#: SIM-498: the index of each independent random stream inside the per-game spawn.
#: Stream 0 drives the LOOP rng, stream 1 drives the FULL-POOL rng. Named so the
#: two consumers can never silently swap places.
LOOP_STREAM = 0
FULL_POOL_STREAM = 1
_N_RNG_STREAMS = 2


def spawn_rng_streams(seed: int | None, n: int = _N_RNG_STREAMS) -> list[np.random.Generator]:
    """SIM-498: build ``n`` INDEPENDENT generators from one per-game seed.

    ``simulate_game`` used to build every generator with ``np.random.default_rng(seed)``
    from the SAME integer, so the loop rng and the full-pool rng emitted IDENTICAL
    sequences. The loop rng draws advancement, steal outcomes, manager decisions and
    the framing nudge; the full-pool rng draws the pitch and the batted ball. Two
    draws that the model treats as independent events came off one sequence.

    ``np.random.SeedSequence(seed).spawn(n)`` derives ``n`` child sequences that are
    independent by construction, so each consumer gets its own stream.

    Reproducibility is preserved exactly: a given ``seed`` always spawns the same
    children in the same order, so a fixed (game, seed) pair replays byte-identically.
    ``seed=None`` draws fresh OS entropy, as ``default_rng(None)`` did.

    Honest scope note: the two streams emitted identical SEQUENCES, but the loop and
    the sampler CONSUME them at different rates, so the alignment drifts across a
    game. Nobody specified or measured that dependence. This function removes it; it
    does not tell you how large it was.
    """
    return [np.random.default_rng(child) for child in np.random.SeedSequence(seed).spawn(n)]


def times_through_order(batters_faced: int, lineup_size: int = 9) -> int:
    """How many times through the batting order a pitcher is on (SIM-434).

    ``batters_faced`` is the number of plate appearances the pitcher has worked
    (0-based count of batters retired/reached); on his Nth batter he is in the
    ``(N // lineup_size) + 1``-th time through.  Returns >= 1 once he has faced a
    batter, 1 before any.  Defensive: a non-positive lineup size collapses to a
    9-man order so a bad lineup never divides by zero.
    """
    size = lineup_size if lineup_size and lineup_size > 0 else 9
    bf = max(0, int(batters_faced))
    return (bf // size) + 1


def _venue_of(state: object) -> int | None:
    """SIM-523 part C4: the live venue id ``GameState.park`` carries (a
    digit string), or None."""
    park = getattr(state, "park", None)
    if park is None:
        return None
    text = str(park).strip()
    return int(text) if text.isdigit() else None


@dataclass(frozen=True, slots=True)
class FieldingSignal:
    """The engine-derived fielding signal for one in-play batted ball (step 6).

    A bounded, distance-/similarity-PURE value object: it carries the *resolved*
    event the loop will score (its ``result_hits/outs/runs`` deltas come straight
    from the play-pool sample), which fielder handled the ball, and whether an
    error occurred.  The loop turns this into a base-out + run delta ONLY through
    :func:`simulation.run_resolution.resolve_runs`; it never re-derives runs.

    Fields mirror the play pool's ``result_*`` columns; the full-pool sampler's
    drawn row fills it (:meth:`StateMachine._full_pool_fielding`).
    """

    event: str  # Statcast/canonical batted-ball event
    result_hits: int  # 0=out 1=1B 2=2B 3=3B 4=HR (pool vocab)
    # Outs recorded on the play. A raw pool row can carry 3 (triple play) and
    # can carry result_hits>=1 WITH result_outs>=1 (a runner thrown out on a
    # hit); the transition path reads the outs from the row's destinations.
    result_outs: int
    result_runs: int  # runs that physically scored on the play
    fielder_id: int | None = None  # the fielder who handled the ball (RBF)
    is_error: bool = False  # error flag (fielder/catcher RBF)
    exit_velo: float | None = None
    launch_angle: float | None = None
    spray_angle: float | None = None
    # SIM-511: the drawn pool row's whole base-state transition (the
    # ``FullPoolSampler.last_transition`` dict). The drawn row IS the play:
    # the loop applies these destinations instead of inferring outs or
    # advancing runners by formula. See ``_resolve_in_play_transition``.
    transition: dict | None = None


@dataclass(frozen=True, slots=True)
class StealResolution:
    """The resolved outcome of one steal attempt (pre-pitch decision -> step 7).

    Sampled from ``sim.stolen_base_pool`` (the historical safe/caught result for
    a runner/catcher/pitcher matchup).  ``runner_id`` is the lead runner who
    went; ``from_base`` / ``to_base`` are 1/2/3 (4 == home).  ``safe`` is the
    pool's ``success`` boolean.  The loop mutates the bases + (on a caught
    stealing) records the out via :func:`resolve_runs`.
    """

    attempted: bool
    runner_id: int | None = None
    from_base: int | None = None
    to_base: int | None = None
    safe: bool = False
    #: SIM-507: this resolution is a PICKOFF outcome, not a steal attempt.
    #: ``safe=False`` retires the runner (an out); ``safe=True`` is an errant
    #: throw — the runner advances one base, no steal credit either way.
    pickoff: bool = False
    #: The pickoff out was a picked-off CAUGHT STEALING (the runner was tagged
    #: at the NEXT base) — MLB Rule 9.07(h) scores it as a CS.
    pickoff_advancing: bool = False


@dataclass(slots=True)
class RunningGameTally:
    """SIM-554: what the running game's paths did in one machine's games.

    It counts and changes no play. The running-game census
    (``scripts/probes/running_game_census.py``) reads it. A pickoff counts
    where it is drawn: before the pitch in the new order, at the pre-pitch
    draw in the old one, with the count it was drawn at.
    """

    #: Pickoff draws made before a pitch (the new order only).
    pickoff_draws: int = 0
    #: Pickoff outs, a picked-off caught stealing included.
    pickoff_outs: int = 0
    #: Pickoff throws that got away and moved the runner up.
    pickoff_errors: int = 0
    #: Pickoff outcomes by the (balls, strikes) count they were drawn at.
    pickoffs_by_count: dict[tuple[int, int], int] = field(default_factory=dict)
    #: Pickoffs that made the third out before the pitch: no pitch thrown.
    no_pitch_third_outs: int = 0
    #: Staged steals voided, by reason ("third_out_first": decision 3).
    voided: dict[str, int] = field(default_factory=dict)
    #: Dropped-third-strike reaches (the batter took first).
    d3k_reaches: int = 0
    #: Runners the got-away advance moved on those reaches (decision 4).
    d3k_runners_moved: int = 0


# ---------------------------------------------------------------------------
# Count machine — the §5.1 terminal classification (pure, side-effect-free)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CountAdvance:
    """The result of advancing the count on one sampled pitch outcome (§5.1).

    Pure value object: it carries the *new* count and whether (and how) the PA
    terminated, so the caller can decide what to commit to the GameState.  The
    count machine itself NEVER mutates a GameState — that is the StateMachine's
    job (read -> classify -> commit), keeping the §5.1 logic unit-testable in
    isolation.
    """

    balls: int
    strikes: int
    #: True once the PA is terminal (walk / strikeout / in-play resolution).
    terminal: bool
    #: The terminal PA event, or ``EVENT_IN_PROGRESS`` while the PA is live.
    #: For ``in_play`` this is None (the event is resolved by the batted ball).
    event: str | None
    #: True when this outcome put the ball in play (the batted-ball path).
    is_contact: bool


def advance_count(balls: int, strikes: int, pitch_outcome: str) -> CountAdvance:
    """Advance a ``(balls, strikes)`` count by one sampled ``pitch_outcome`` and
    classify PA-terminal per the SIM-310 spec §5.1.

    The machine:

      * ``ball``            -> balls += 1; ball 4 terminates as a **walk**.
      * ``called_strike`` /
        ``swinging_strike`` -> strikes += 1; strike 3 terminates as a
                               **strikeout**.
      * ``foul``            -> strikes += 1 ONLY while strikes < 2; with two
                               strikes the foul is **absorbed** (count unchanged,
                               PA stays alive) — the SIM-056 absorbing rule.
      * ``in_play``         -> terminal as a pitch outcome; the PA's real event
                               is resolved by the batted-ball steps (None here).

    This is a pure function: it returns a :class:`CountAdvance` and never mutates
    anything.  The count-conditional foul **re-weight** that biases *how often*
    a foul is drawn at two strikes is SIM-318 (it lives upstream of this call,
    in outcome determination); this function implements only the absorbing /
    terminal mechanics.

    Raises ``ValueError`` on an unknown ``pitch_outcome`` or a count that is
    already terminal (a guard against driving a dead PA).
    """
    if pitch_outcome not in PITCH_OUTCOMES:
        raise ValueError(
            f"unknown pitch_outcome {pitch_outcome!r}; expected one of {PITCH_OUTCOMES}."
        )
    if balls < 0 or strikes < 0:
        raise ValueError(f"count cannot be negative: {balls}-{strikes}.")
    if balls >= BALLS_FOR_WALK or strikes >= STRIKES_FOR_STRIKEOUT:
        raise ValueError(
            f"count {balls}-{strikes} is already terminal; the PA should have "
            "ended before another pitch was classified (spec §5.1)."
        )

    if pitch_outcome == "hit_by_pitch":
        # SIM-509: terminal at any count — the batter is awarded 1B. Resolved
        # by the walk force mechanics with its own canonical event.
        return CountAdvance(
            balls, strikes, terminal=True, event=EVENT_HIT_BY_PITCH, is_contact=False
        )

    if pitch_outcome == "ball":
        balls += 1
        if balls >= BALLS_FOR_WALK:
            return CountAdvance(balls, strikes, terminal=True, event=EVENT_WALK, is_contact=False)
        return CountAdvance(
            balls, strikes, terminal=False, event=EVENT_IN_PROGRESS, is_contact=False
        )

    if pitch_outcome in ("called_strike", "swinging_strike"):
        strikes += 1
        if strikes >= STRIKES_FOR_STRIKEOUT:
            return CountAdvance(
                balls, strikes, terminal=True, event=EVENT_STRIKEOUT, is_contact=False
            )
        return CountAdvance(
            balls, strikes, terminal=False, event=EVENT_IN_PROGRESS, is_contact=False
        )

    if pitch_outcome == "foul":
        # SIM-056 absorbing rule: a two-strike foul keeps the PA alive and does
        # NOT advance the count past strike 2.
        if strikes < STRIKES_FOR_STRIKEOUT - 1:
            strikes += 1
        return CountAdvance(
            balls, strikes, terminal=False, event=EVENT_IN_PROGRESS, is_contact=False
        )

    # in_play: terminal as a pitch outcome; event resolved by batted-ball steps.
    return CountAdvance(balls, strikes, terminal=True, event=None, is_contact=True)


#: Regulation length (spec §6.2): a game is at least this many full innings.
REGULATION_INNINGS = 9

#: Hard ceiling on innings so a pathological loop (e.g. a degenerate injected
#: outcome that never records an out) cannot spin forever.  Real games settle in
#: a handful of extra innings under the ghost-runner rule; this is purely a
#: guard, far above any plausible game length.
_MAX_INNINGS = 50
