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

#: The plate-appearance events the count machine produces without a ball in play.
EVENT_WALK = "walk"
EVENT_STRIKEOUT = "strikeout"
#: A hit by pitch: the walk's force mechanics under its own canonical event, so
#: the box and the walk band never count it as a walk.
EVENT_HIT_BY_PITCH = "hit_by_pitch"
#: The intentional walk's own canonical event. The box counts it as a walk and
#: a non-at-bat, as it does a ball-four walk. The lane's pool bands grade the
#: two against different references (the count chain and ``sim.ibb_rates``),
#: so the label must tell them apart: a ``walk`` label read zero on the
#: intentional-walk band and pushed the surplus into the walk band (the first
#: certifying lane).
EVENT_INTENTIONAL_WALK = "intentional_walk"

#: The count machine's marker while a plate appearance is still live (no
#: terminal yet). It is not a plate-appearance event and carries no runs.
EVENT_IN_PROGRESS = "in_progress"

#: The Statcast events that record TWO outs on the play (the batter and a
#: runner). The acceptance lane's double-play counter reads it.
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

#: The steal outcomes on ``PlayResult.steal_outcome``.
STEAL_SAFE = "safe"
STEAL_CAUGHT = "caught"

#: The next base of a runner who moves up one (4 = home: he scores).
_NEXT_BASE = {1: 2, 2: 3, 3: 4}

#: The first late inning: the intentional-walk cell's ``late`` flag, and the
#: inning from which a high-leverage spot sends the closer.
_LATE_INNING = 7
#: The leverage index above which a spot is high leverage.
_HIGH_LEVERAGE = 1.5

#: The index of each independent random stream inside a game's spawn: stream 0
#: drives the loop's rng, stream 1 the sampler's. Named, so the two consumers
#: can never swap places.
LOOP_STREAM = 0
FULL_POOL_STREAM = 1
_N_RNG_STREAMS = 2

#: Regulation length: a game is at least this many full innings.
REGULATION_INNINGS = 9

#: The ceiling on innings, so a pathological machine (an injected outcome that
#: never records an out) cannot spin forever. Real games settle in a few extra
#: innings under the ghost-runner rule; this is a guard only.
_MAX_INNINGS = 50


def spawn_rng_streams(seed: int | None, n: int = _N_RNG_STREAMS) -> list[np.random.Generator]:
    """``n`` independent generators from one per-game seed.

    The loop's rng draws the advancement draws, the steal outcomes, the
    manager's decisions and the intentional walk; the sampler's rng draws the
    pitch and the batted ball. The two must be independent, so each comes from
    its own child of ``np.random.SeedSequence(seed).spawn(n)``: two generators
    built from the same integer emit identical sequences (SIM-498).

    A given ``seed`` always spawns the same children in the same order, so a
    (game, seed) pair replays byte for byte. ``seed=None`` draws fresh OS
    entropy.
    """
    return [np.random.default_rng(child) for child in np.random.SeedSequence(seed).spawn(n)]


def times_through_order(batters_faced: int, lineup_size: int = 9) -> int:
    """How many times through the batting order a pitcher is.

    ``batters_faced`` counts the plate appearances he has completed; on his
    Nth batter he is in the ``(N // lineup_size) + 1``-th time through, so 1
    before any. A non-positive lineup size counts as nine.
    """
    size = lineup_size if lineup_size and lineup_size > 0 else 9
    bf = max(0, int(batters_faced))
    return (bf // size) + 1


def _venue_of(state: object) -> int | None:
    """The live venue id ``GameState.park`` carries (a digit string), or None."""
    park = getattr(state, "park", None)
    if park is None:
        return None
    text = str(park).strip()
    return int(text) if text.isdigit() else None


@dataclass(frozen=True, slots=True)
class FieldingSignal:
    """One drawn batted ball: the row of the batted-ball pool that IS the play.

    It carries the drawn row's event and its ``result_*`` counts, the live
    defender at the row's position, whether the play was an error, the
    batted-ball geometry and the row's whole base-out ``transition``. The loop
    applies the transition's destinations and turns the play into a run value
    only through the run ledger (``StateMachine._commit_run_delta``).
    """

    event: str  # Statcast/canonical batted-ball event
    result_hits: int  # 0=out 1=1B 2=2B 3=3B 4=HR (pool vocab)
    # Outs recorded on the play. A raw pool row can carry 3 (triple play) and
    # can carry result_hits>=1 WITH result_outs>=1 (a runner thrown out on a
    # hit); the transition path reads the outs from the row's destinations.
    result_outs: int
    result_runs: int  # runs that physically scored on the play
    fielder_id: int | None = None  # the live defender at the drawn position
    is_error: bool = False  # the drawn row is a reach on an error
    exit_velo: float | None = None
    launch_angle: float | None = None
    spray_angle: float | None = None
    # The drawn row's whole base-state transition
    # (``FullPoolSampler.last_transition``): the loop applies these
    # destinations instead of inferring outs or advancing runners by formula.
    transition: dict | None = None


@dataclass(frozen=True, slots=True)
class StealResolution:
    """One staged steal attempt or pickoff, resolved on the pitch.

    ``runner_id`` is the lead runner; ``from_base`` and ``to_base`` are 1-3
    (4 = home). ``safe`` is the drawn row's outcome. The loop moves the bases
    and, on an out, records it through the run ledger.
    """

    attempted: bool
    runner_id: int | None = None
    from_base: int | None = None
    to_base: int | None = None
    safe: bool = False
    #: A pickoff outcome, not a steal attempt: ``safe=False`` retires the
    #: runner, ``safe=True`` is an errant throw that moves him up a base. No
    #: steal credit either way.
    pickoff: bool = False
    #: The pickoff out was a picked-off CAUGHT STEALING (the runner was tagged
    #: at the next base): Rule 9.07(h) scores it as a caught stealing.
    pickoff_advancing: bool = False


@dataclass(slots=True)
class RunningGameTally:
    """What the running game's paths did in one machine's games.

    It counts and changes no play. The running-game census
    (``scripts/probes/running_game_census.py``) reads it. A pickoff counts
    where it is drawn (before the pitch, or at the single pre-pitch draw) with
    the count it was drawn at.
    """

    #: Pickoff draws made before a pitch (the running game on the pitch only).
    pickoff_draws: int = 0
    #: Pickoff outs, a picked-off caught stealing included.
    pickoff_outs: int = 0
    #: Pickoff throws that got away and moved the runner up.
    pickoff_errors: int = 0
    #: Pickoff outcomes by the (balls, strikes) count they were drawn at.
    pickoffs_by_count: dict[tuple[int, int], int] = field(default_factory=dict)
    #: Pickoffs that made the third out before the pitch: no pitch thrown.
    no_pitch_third_outs: int = 0
    #: Staged steals voided, by reason ("third_out_first").
    voided: dict[str, int] = field(default_factory=dict)
    #: Dropped-third-strike reaches (the batter took first).
    d3k_reaches: int = 0
    #: Runners the got-away advance moved on those reaches.
    d3k_runners_moved: int = 0


# ---------------------------------------------------------------------------
# The count machine
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CountAdvance:
    """The result of advancing the count by one pitch outcome.

    It carries the new count and whether (and how) the plate appearance
    ended. The count machine never changes a GameState; the state machine
    reads this and commits it.
    """

    balls: int
    strikes: int
    #: True once the plate appearance is over (a walk, a strikeout, a ball in play).
    terminal: bool
    #: The terminal event, or ``EVENT_IN_PROGRESS`` while the plate appearance
    #: is live. None for ``in_play``: the batted ball decides the event.
    event: str | None
    #: True when this outcome put the ball in play.
    is_contact: bool


def advance_count(balls: int, strikes: int, pitch_outcome: str) -> CountAdvance:
    """Advance a ``(balls, strikes)`` count by one ``pitch_outcome``.

      * ``ball``            -> balls += 1; ball four ends it as a **walk**.
      * ``called_strike`` /
        ``swinging_strike`` -> strikes += 1; strike three ends it as a
                               **strikeout**.
      * ``foul``            -> strikes += 1 only while strikes < 2; a
                               two-strike foul leaves the count unchanged and
                               the plate appearance alive.
      * ``hit_by_pitch``    -> ends it at any count, as a **hit by pitch**.
      * ``in_play``         -> ends it; the batted ball decides the event
                               (None here).

    A pure function: it returns a :class:`CountAdvance` and changes nothing.
    Raises ``ValueError`` on an unknown ``pitch_outcome`` or a count that has
    already ended.
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
        # Terminal at any count: the batter is awarded first base, through
        # the walk's force mechanics with its own canonical event.
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
        # A two-strike foul keeps the plate appearance alive and does not
        # advance the count past strike two.
        if strikes < STRIKES_FOR_STRIKEOUT - 1:
            strikes += 1
        return CountAdvance(
            balls, strikes, terminal=False, event=EVENT_IN_PROGRESS, is_contact=False
        )

    # in_play: terminal as a pitch outcome; the batted ball decides the event.
    return CountAdvance(balls, strikes, terminal=True, event=None, is_contact=True)
