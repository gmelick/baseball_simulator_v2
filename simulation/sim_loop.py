"""
sim_loop.py
===========
The pitch-by-pitch game simulator: the count machine, the half-inning
control, the manager hooks and the full-game driver.

Every decision in this loop is a similarity-weighted draw from a hard-filtered
pool, never a hand-tuned formula, and the drawn row IS the play (the owner
rules of 2026-08-10 and 2026-08-29). The pitch outcome comes from
:meth:`StateMachine._full_pool_outcome`; the batted ball from
:meth:`StateMachine._full_pool_fielding`, whose drawn row carries its whole
base-out transition (SIM-511); the discretionary extra bases from the SIM-512
advancement draws; the steal from the SIM-474 opportunity draw. All of them
read one :class:`~simulation.full_pool_sampler.FullPoolSampler` over an
engine-artifact bundle.

There is ONE in-play path. SIM-486 deleted the per-tile FAISS fallback, the
injected ``PlayResolver`` and the legacy advancement code that resolved a
signal without a transition. A test that needs a play hands the machine a
:mod:`simulation.synthetic_bundle` bundle instead, so the suite runs the code
users get.

The count machine can still run alone: a caller that passes
``pitch_outcome=`` to :meth:`StateMachine.step_pitch` drives deterministic
count sequences with no sampler at all (walks, strikeouts and the base-out
bookkeeping resolve; an in-play pitch with no sampler is left unresolved).

Section owners, for the history: the count machine + half-inning logic
(SIM-316), fielding / baserunning / steals (SIM-319), the full-game driver
(SIM-320), the manager hooks (SIM-323 / SIM-434), the run ledger (SIM-499 /
SIM-500), the transition draw (SIM-511 / SIM-512).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np

from simulation.constants import resolve_event_to_canonical
from simulation.game_state import (
    BALLS_FOR_WALK,
    OUTS_PER_INNING,
    PITCH_OUTCOMES,
    STRIKES_FOR_STRIKEOUT,
    Bases,
    GameState,
    Half,
    PlayResult,
    Team,
)
from simulation.game_state import (
    CONTACT_PITCH_OUTCOME as _GS_CONTACT_PITCH_OUTCOME,
)
from simulation.run_resolution import resolve_runs

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

# ---------------------------------------------------------------------------
# SIM-323 — manager-tendency access (the §3/§5.3 decision inputs)
# ---------------------------------------------------------------------------

#: Maps a manager-tendency *name* to its position in the SIM-2.8
#: :class:`similarity.engines.manager_similarity.ManagerProfile` feature vectors
#: (``usage_vec`` / ``aggression_vec`` / ``platoon_vec``), so a profile-shaped
#: object can be read by name without importing the engine (which would drag in
#: DuckDB).  The order mirrors ``USAGE_FEATURES`` / ``AGGRESSION_FEATURES`` /
#: ``PLATOON_FEATURES`` exactly.  Used by :meth:`StateMachine._tendency`.
_MANAGER_TENDENCY_INDEX = {
    # usage_vec
    "starter_avg_pitch_count": ("usage_vec", 0),
    "starter_pull_pct_before_100": ("usage_vec", 1),
    "closer_entry_leverage_index": ("usage_vec", 2),
    "high_leverage_reliever_rate": ("usage_vec", 3),
    "opener_usage_rate": ("usage_vec", 4),
    "bulk_innings_rate": ("usage_vec", 5),
    "available_reliever_usage_rate": ("usage_vec", 6),  # SIM-427 capstone
    # aggression_vec
    "steal_order_rate_per_1b_opp": ("aggression_vec", 0),
    "hit_and_run_rate_per_opportunity": ("aggression_vec", 1),
    "sac_bunt_rate_high_leverage": ("aggression_vec", 2),
    "sac_bunt_rate_low_leverage": ("aggression_vec", 3),
    "squeeze_play_rate_per_3b_opp": ("aggression_vec", 4),
    # platoon_vec
    "pinch_hit_rate_vs_same_hand": ("platoon_vec", 0),
    "pinch_hit_rate_high_leverage": ("platoon_vec", 1),
    "defensive_sub_rate_late_innings": ("platoon_vec", 2),
    "double_switch_rate_per_reliever_change": ("platoon_vec", 3),
    "platoon_advantage_exploitation_rate": ("platoon_vec", 4),
}

#: Late-inning threshold (7th+) at which leverage-sensitive bullpen / pinch
#: decisions engage (spec §5.3; the manager-similarity ``*_late_innings`` cut).
_LATE_INNING = 7
#: A starter pull is *considered* once the pitch count reaches this (the
#: manager's ``starter_avg_pitch_count`` tendency biases the actual hook around
#: it); a hard ceiling forces a pull regardless of tendency.
_PULL_PITCH_FLOOR = 75
_PULL_PITCH_CEILING = 110
#: The leverage index above which a spot is "high leverage" (the LI > 1.5 cut the
#: manager-similarity features use for sac-bunt / pinch-hit gating).
_HIGH_LEVERAGE = 1.5

# ---------------------------------------------------------------------------
# SIM-434 — manager pull + reliever-selection model (fatigue / TTO / rest)
# ---------------------------------------------------------------------------
#
# A per-pitcher fatigue / times-through-the-order (TTO) effectiveness model + a
# reliever-selection scoring function, all PURE helpers (no GameState mutation,
# no rng) so they unit-test in isolation.  They are CONSULTED by the SIM-323
# manager hooks ONLY when a manager + bullpen are wired (i.e. when SIM_MANAGER
# enables the wiring); with the flag off, ``manager is None`` and none of this
# runs — production output is byte-identical (see SIM-434 integration notes).

#: A starter's "typical" pitch budget; fatigue ramps as the count approaches and
#: exceeds it.  Used as the denominator of the in-game pitch-count fatigue term
#: when real per-team rest data is not yet available (SIM-433 not ingested).
_FATIGUE_PITCH_BUDGET = 95.0
#: Each time through the order beyond the first adds this much to the pitcher's
#: effective fatigue (the documented "times-through-the-order penalty": a
#: starter loses effectiveness the 2nd and especially the 3rd time facing a
#: lineup).  Linear, capped at the 3rd time through (TTO >= 3 is treated as 3).
_TTO_FATIGUE_PER_TIME = 0.12
#: A full season's "fully rested" starter rest, in days; rest at/above this gives
#: the maximum rest bonus, rest at 0 (back-to-back) the minimum.  Used only when
#: a per-pitcher rest map is wired; the in-game fatigue fallback ignores it.
_FULL_REST_DAYS = 5.0

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


def _env_flag(name: str, *, default: bool = False) -> bool:
    """Parse an on/off env flag the same way :mod:`simulation.production_factory`
    parses ``SIM_MANAGER`` (SIM-434).

    Unset -> ``default``; ``"0"``/``"false"``/``"no"``/``"off"`` (any case) ->
    ``False``; anything else -> ``True``.  Centralised so the SIM_MANAGER gate is
    read identically in the loop and the factory.
    """
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


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


def pitcher_fatigue(
    pitch_count: int,
    *,
    tto: int = 1,
    rest_days: float | None = None,
) -> float:
    """A bounded [0, 1] fatigue index for the current pitcher (SIM-434).

    0.0 == fully fresh, 1.0 == maximally gassed.  Three additive drivers, each a
    documented, monotone proxy (so the gating is transparent + DB-free):

      * **in-game pitch count** — ramps from 0 toward 1 as the count approaches
        and exceeds :data:`_FATIGUE_PITCH_BUDGET` (the dominant signal, always
        available);
      * **times through the order** — each time through beyond the first adds
        :data:`_TTO_FATIGUE_PER_TIME` (the TTO penalty), capped at the 3rd time;
      * **rest** — when a per-pitcher ``rest_days`` is wired (SIM-433 follow-on),
        short rest ADDS fatigue (back-to-back work tires an arm); when ``None``
        (the data-not-ingested fallback) this term is simply 0 and the index is
        driven by pitch count + TTO alone.

    Monotone in the obvious directions (more pitches / more times through / less
    rest -> higher) and clamped to ``[0.0, 1.0]``.
    """
    pc = max(0, int(pitch_count))
    pc_term = pc / _FATIGUE_PITCH_BUDGET if _FATIGUE_PITCH_BUDGET > 0 else 0.0
    times = max(1, min(int(tto), 3))
    tto_term = (times - 1) * _TTO_FATIGUE_PER_TIME
    rest_term = 0.0
    if rest_days is not None:
        # Short rest tires the arm; full (>=_FULL_REST_DAYS) rest contributes 0.
        deficit = max(0.0, _FULL_REST_DAYS - max(0.0, float(rest_days)))
        rest_term = 0.06 * (deficit / _FULL_REST_DAYS if _FULL_REST_DAYS > 0 else 0.0)
    return float(max(0.0, min(1.0, pc_term + tto_term + rest_term)))


def tto_effectiveness(tto: int) -> float:
    """A bounded (0, 1] effectiveness multiplier from the times-through-the-order
    penalty (SIM-434).

    A starter is most effective the 1st time through a lineup and decays the 2nd
    and (especially) the 3rd time; a reliever, called for one trip, sits at the
    top.  Returns 1.0 for the 1st time through and decays by
    :data:`_TTO_FATIGUE_PER_TIME` per subsequent time, floored so it never reaches
    0 (a tired pitcher is still *some* use).
    """
    times = max(1, int(tto))
    decay = (times - 1) * _TTO_FATIGUE_PER_TIME
    return float(max(0.25, 1.0 - decay))


def platoon_factor(bat_hand: str | None, throw_hand: str | None) -> float:
    """The platoon multiplier for a reliever vs the current batter (SIM-434).

    Same-handed matchup (R-vs-R / L-vs-L) favours the pitcher; opposite-handed
    favours the batter.  Returns ``> 1`` when the matchup is in the *pitcher's*
    favour (same hand), ``< 1`` when it is not, ``1.0`` when either hand is
    unknown (a switch hitter resolved to 'S', or an unwired hand) so the score is
    platoon-neutral rather than guessing.
    """
    if not bat_hand or not throw_hand:
        return 1.0
    bh = str(bat_hand).upper()
    th = str(throw_hand).upper()
    if bh not in ("L", "R") or th not in ("L", "R"):
        return 1.0
    return 1.15 if bh == th else 0.87


def score_reliever(
    *,
    leverage: float,
    platoon: float,
    effectiveness: float,
    rest_days: float | None = None,
) -> float:
    """Score a candidate reliever for the current spot (SIM-434).

    The multiplicative product of the four levers the manager weighs:
    ``leverage × platoon × effectiveness × rest_bonus``.  A higher score is a
    better fit for THIS spot:

      * **leverage** — a high-LI spot wants the best available arm (the score
        scales with the live Leverage Index);
      * **platoon** — :func:`platoon_factor` (same-hand advantage);
      * **effectiveness** — the arm's fresh-arm effectiveness (a closer's
        :func:`tto_effectiveness` is 1.0; a tired bulk arm scores lower);
      * **rest** — a rested arm (``rest_days`` high, or ``None`` == treated as
        rested) scores at full; a short-rest arm is discounted.

    Pure + monotone (higher leverage / platoon / effectiveness / rest -> higher
    score); never negative.  Used by :meth:`StateMachine._pick_reliever` to RANK
    a wired bullpen when SIM_MANAGER is on.
    """
    lev = max(0.0, float(leverage))
    plt = max(0.0, float(platoon))
    eff = max(0.0, float(effectiveness))
    if rest_days is None:
        rest_bonus = 1.0
    else:
        rd = max(0.0, float(rest_days))
        # 0 days -> 0.7 (tired), >=_FULL_REST_DAYS -> 1.0 (rested), linear between.
        frac = min(1.0, rd / _FULL_REST_DAYS) if _FULL_REST_DAYS > 0 else 1.0
        rest_bonus = 0.7 + 0.3 * frac
    return float(max(0.0, lev * plt * eff * rest_bonus))


def _safe_float(val, default: float = 0.0) -> float:
    """Coerce ``val`` to a finite float, falling back to ``default`` on a None /
    NaN / non-numeric (the SIM-323 tendency reads are defensive — a missing or
    junk profile value must degrade to the no-op default)."""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return float(default)
    if f != f:  # NaN
        return float(default)
    return f


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


# ---------------------------------------------------------------------------
# StateMachine — the SIM-316 GameState-driven §5/§6 skeleton
# ---------------------------------------------------------------------------


class StateMachine:
    """The SIM-316 plate-appearance / half-inning state machine.

    Reads a mutable SIM-311 :class:`~simulation.game_state.GameState`, drives the
    §5.1 count machine and §6.1 half-inning logic, commits the result, and emits
    a :class:`~simulation.game_state.PlayResult`.  Every draw comes from the
    ``full_pool_sampler`` (a :class:`~simulation.full_pool_sampler.FullPoolSampler`
    over an engine-artifact bundle — the production bundle on disk, or a
    :mod:`simulation.synthetic_bundle` bundle in a test).  The sampler may be
    omitted to drive the *count/out/base* machine in isolation: the caller then
    supplies each ``pitch_outcome`` and an in-play pitch stays unresolved.
    """

    def __init__(
        self,
        full_pool_sampler=None,
        *,
        rng: np.random.Generator | None = None,
        manager=None,
        bench=None,
        ibb_rates: dict[tuple[int, int, bool, bool], float] | None = None,
    ) -> None:
        self.rng = rng if rng is not None else np.random.default_rng()
        # SIM-515: the measured IBB rate per (runners_state, outs, is_late,
        # is_close) cell — sim.ibb_rates, loaded by the factory. None (the
        # no-DB default) means no IBB ever fires; the hand-tuned formula that
        # issued 2.64x MLB's volume is retired.
        self.ibb_rates = ibb_rates
        # The full-pool similarity-weighted sampler: the pitch draw, the batted-
        # ball transition draw, the advancement draws and the steal draw all
        # read it. None = the count-machine-only mode (a caller supplies each
        # pitch outcome; an in-play pitch stays unresolved).
        self.full_pool_sampler = full_pool_sampler
        # SIM-455: the full-pool weight cache has THREE lifetimes, so it needs THREE
        # keys. One key (`_fp_matchup`, `(pitcher, hand, batter)`) governed both
        # refreshes and got both wrong:
        #   * it carried `batter_id`, so a new batter re-ran `new_half_inning`, which
        #     recomputes `f_pitcher` over the WHOLE pool. A game has ~83 plate
        #     appearances and ~5 pitchers, so ~78 of those full-pool passes did work
        #     that produced the identical vector.
        #   * it OMITTED the base-out state, which `new_plate_appearance` reads to
        #     build the situation factor. Base-out changes INSIDE a plate appearance
        #     (a steal, a runner advancing on a wild pitch, a caught stealing adding
        #     an out) and nothing refreshed, so the situation factor went STALE for
        #     the rest of the plate appearance.
        # `_fp_pitcher_key` gates `new_half_inning` (f_pitcher, per pitcher+hand);
        # `_fp_pa_key` gates `new_plate_appearance` (f_batter, per batter — memoized
        # downstream in `FullPoolSampler._batter_affinity` — times the situation
        # factor, per base-out).
        self._fp_pitcher_key: tuple | None = None
        self._fp_pa_key: tuple | None = None
        # SIM-411/413/425b realism nudges, each GATED OFF by default so a game is
        # byte-identical to before unless the operator opts in (mirrors
        # SIM_MANAGER). All three are ALSO graceful-optional: they no-op when the
        # data they read (the migration-0012 batted-ball columns / a venue park
        # factor / a per-position defense map) is absent, so turning a flag on
        # without the rebuilt artifact is still a no-op. Read once per machine.
        self._bb_platoon = _env_flag("SIM_BB_PLATOON")  # SIM-413
        # SIM-476 (2026-08-30): the SIM-425b fielder nudge and the SIM-411 park
        # flip are DELETED — both post-draw flips are superseded by the fitted
        # SIM-491 draw-weight kernels (SIM_FIELDER_KERNEL_SIGMA=0.5 /
        # SIM_PARK_KERNEL_SIGMA=0.02, fitted against the pool's own
        # conditional frequencies; see docs/audit/2026-08-28-sim476-fit-plan.md).
        # SIM-517 (2026-09-04): the SIM-428 framing flip (`_apply_framing`,
        # `SIM_FRAMING`) is DELETED — the catcher's receiving effect is now a
        # WEIGHT in the pitch draw (the fitted anisotropic receiving kernel:
        # SIM_CATCHER_FRAMING_SIGMA=0.25 / SIM_CATCHER_BLOCK_SIGMA=0.05), so
        # the drawn row is the play with no post-draw adjustment anywhere.
        # SIM-517 part D: honor the drawn pitch row's got-away fact (a passed
        # ball / wild pitch on THAT pitch, incl. an uncaught third strike).
        # Default OFF until the part-E fit + certifying lane land it — the
        # flag-off game is byte-identical (no rng, no reads).
        self._got_away = _env_flag("SIM_GOT_AWAY")
        #: The last drawn pitch's got-away fact (False on every other path).
        self._last_pitch_got_away = False
        # SIM-323: the manager tendency source the §3/§5.3 hooks read.  Duck-typed:
        # any object/mapping exposing the manager-similarity tendency rates
        # (``starter_pull_pct_before_100`` / ``closer_entry_leverage_index`` /
        # ``steal_order_rate_per_1b_opp`` / ``pinch_hit_rate_high_leverage`` /
        # ``sac_bunt_rate_high_leverage`` / ``sac_bunt_rate_low_leverage`` /
        # ``starter_avg_pitch_count``), OR a :class:`ManagerProfile`-shaped object
        # exposing ``usage_vec`` / ``aggression_vec`` / ``platoon_vec`` (read via
        # :data:`_MANAGER_TENDENCY_INDEX`).  ``None`` == no profile wired -> every
        # decision is a no-op (the SIM-320/324/326 no-DB test path stays green).
        self.manager = manager
        # SIM-323: available pinch-hitters per team, ``{Team: [batter_id, ...]}``
        # (Team value or int key both accepted).  The end-of-PA pinch-hit decision
        # pops from here so a bench player can only be used once; ``None``/empty ->
        # pinch-hit degrades to a no-op (the no-DB test path).  Production wires the
        # real bench off the roster.
        self.bench = dict(bench) if bench else {}
        # SIM-434: per-defending-team available-arms bullpen map, staged on the machine
        # by the production factory (when SIM_MANAGER is on) and read by ``simulate_game``
        # as the fallback when no explicit ``bullpen=`` is passed. ``None`` -> no bullpen
        # wired, so the pull→bullpen hook degrades to a no-op (flag-off byte-identical).
        self.bullpen: dict | None = None
        # SIM-323: a log of the manager decisions actually taken this game (the
        # observable side of the §3/§5.3 hooks the SIM-323 tests assert against).
        # Each entry is a small dict: {"kind": ..., "inning": ..., "leverage": ...,
        # plus decision-specific ids}.  Lightweight + additive.
        self.manager_decisions: list[dict] = []
        # Steal decision made in the pre-pitch hook, resolved in step 7.  Reset
        # each pitch in step_pitch.
        self._pending_steal: StealResolution | None = None
        # SIM-328: per-game boxscore accumulator.  Populated INSIDE the PA loop
        # (:meth:`_accumulate_pa`, called from :meth:`_end_of_pa`) on every
        # terminal PA — the batter on offense, the pitcher on defense.  Lazily
        # created on the first terminal PA so a machine that is never driven (or a
        # caller that does not care) carries no boxscore.  The full-game driver
        # exposes it on :class:`GameSimResult.boxscore`.
        self.boxscore: BoxScore | None = None
        # SIM-414: per-half-inning counter of outs that errors prevented (a
        # reach-on-error that would have been an out adds 1).  Used by
        # :meth:`_accumulate_pa` to flag runs that scored AFTER the inning
        # "should have ended" as UNEARNED per MLB Rule 9.16(b) — the prior
        # implementation only excluded the per-play ``is_error`` case and
        # under-counted unearned runs.  Reset to 0 in :meth:`advance_half_inning`.
        self._half_inning_error_outs_lost: int = 0

    # ===================================================================
    # Pitch step: read GameState -> sample/classify -> commit -> PlayResult
    # ===================================================================

    def step_pitch(
        self,
        state: GameState,
        *,
        pitch_outcome: str | None = None,
    ) -> PlayResult:
        """Advance the game by exactly ONE pitch (spec §2 step 1 -> step 8).

        Flow:
          1. **Read** the GameState and validate the live (in-play) invariants.
          2. **Pre-pitch hook** (§3): manager decisions — a no-op stub here,
             SIM-323 owns the logic.
          3. **Draw** the pitch outcome from the full-pool sampler (the live
             count is the draw's bucket) unless the caller supplied
             ``pitch_outcome`` directly (count-machine-only mode).
          4. **Outcome determination (§5.1)** — classify the count advance /
             PA-terminal via :func:`advance_count` (incl. the SIM-056
             two-strike-foul absorbing rule).
          5. **Commit** the new count to the GameState; on terminal, resolve the
             PA (walk / strikeout / in-play) and roll the PA over, recording outs
             and flipping the half-inning at 3 outs (§6.1).
          6. **Emit** a typed ``PlayResult`` whose ``next_state`` is the committed
             GameState.

        ``pitch_outcome`` (when given) MUST be one of
        :data:`simulation.game_state.PITCH_OUTCOMES`; it lets the count-machine
        tests drive deterministic count sequences without a sampler.
        """
        # --- Step 1: read + validate the incoming (live) state -------------
        state.assert_invariants(in_play=True)
        # SIM-517: the got-away fact belongs to ONE drawn pitch; clear any
        # stale carry before this pitch samples (an injected outcome never
        # sets it).
        self._last_pitch_got_away = False

        # --- Pre-pitch manager hook (§3) — steal initiate + SIM-323 logic --
        # The hook may stage a steal attempt (decision); its outcome (safe /
        # caught) is resolved in step 7 below.  A steal pre-staged by a caller
        # (``stage_steal``) is honoured; otherwise the hook may auto-stage one.
        # ``_pending_steal`` is consumed + cleared in
        # :meth:`_resolve_steal_outcome` so an attempt never carries across
        # pitches.
        self._pre_pitch_hook(state)

        # --- IBB short-circuit (SIM-323 §3 item 2) -------------------------
        # An intentional walk signalled by the pre-pitch hook ends the PA without
        # running the count machine: the batter is awarded first base.  Resolved
        # through the same forced-runner walk path (and its run-resolution) so the
        # base-out + score delta stays canonical, then the PA is rolled over.
        if state.manager.intentional_walk_signalled:
            return self._issue_intentional_walk(state)

        # --- SIM-434: count this pitch ------------------------------------
        # The current pitcher's pitch count was NEVER incremented (the
        # ``state.pitcher_pitch_count = 0`` reset on a pull existed, but nothing
        # ever advanced it), so the SIM-323 starter-pull floor/ceiling gate could
        # never fire even when a manager + bullpen were wired.  A real pitch is
        # thrown on every path below (the IBB short-circuit above threw none and
        # returned already), so increment exactly here, once per pitch.  This is a
        # pure counter bump: it consumes no rng and is read ONLY by
        # ``_maybe_pull_starter`` (no-op while ``manager is None``), so with
        # SIM_MANAGER off the simulated game is byte-identical to before.
        state.pitcher_pitch_count += 1

        # --- Steps 2/3: the pitch draw -------------------------------------
        if pitch_outcome is None:
            if self.full_pool_sampler is None:
                raise ValueError(
                    "step_pitch needs either a full_pool_sampler or an explicit "
                    "pitch_outcome (count-machine-only mode)."
                )
            # SIM-424: full-pool similarity-weighted draw (Situation+Pitcher+
            # Batter); the live count is the draw's bucket (SIM-429).
            pitch_outcome = self._full_pool_outcome(state)
        elif pitch_outcome not in PITCH_OUTCOMES:
            raise ValueError(f"pitch_outcome {pitch_outcome!r} is not one of {PITCH_OUTCOMES}.")

        # --- Step 4: outcome determination (§5.1 count machine) ------------
        # advance_count applies the §5.1 terminal mechanics incl. the
        # two-strike-foul absorbing rule; the drawn (or injected) outcome is
        # taken verbatim.
        adv = advance_count(state.balls, state.strikes, pitch_outcome)

        result = PlayResult(
            pitch_outcome=pitch_outcome,
            is_contact=adv.is_contact,
            pa_terminal=adv.terminal,
            event=adv.event if adv.event != EVENT_IN_PROGRESS else None,
        )

        # --- Step 7 (steal outcome) on a NON-terminal pitch ----------------
        # A steal resolves on the pitch regardless of whether the PA ended.  On
        # a non-terminal pitch (ball / strike-1 / non-terminal foul) the steal is
        # the only baserunning that happens; on a terminal pitch the steal is
        # resolved BEFORE the batted-ball / walk resolution so a caught stealing
        # that is the 3rd out short-circuits the PA result (§3 / §7).
        if not adv.terminal:
            # Live PA: resolve any steal (it can be the 3rd out -> half-inning),
            # then commit the advanced count.
            self._resolve_steal_outcome(state, result)
            if state.is_half_inning_over():
                # A caught stealing ended the half-inning on a non-terminal pitch:
                # roll it (clears bases, resets the count) and emit.
                self.advance_half_inning(state)
                result.next_state = state
                return result
            # SIM-517 part D: the drawn pitch got away (a passed ball / wild
            # pitch) — the runners advance one base, the drawn row is the
            # play. Skipped when a steal or pickoff already resolved this
            # pitch's baserunning (the pool rows mix the two channels; one
            # mover per pitch keeps the accounting canonical).
            if (
                self._last_pitch_got_away
                and not result.steal_attempted
                and not result.pickoff_out
                and not result.pickoff_error
            ):
                self._resolve_got_away_advance(state, result)
            state.balls = adv.balls
            state.strikes = adv.strikes
            state.assert_count_valid(mid_count=True)
            result.next_state = state
            return result

        # Terminal PA. Commit the transient terminal count for inspection, then
        # resolve the steal (§7) before the batted-ball / walk / K resolution so
        # a caught-stealing 3rd out pre-empts the PA result, then roll over.
        state.balls = adv.balls
        state.strikes = adv.strikes

        self._resolve_steal_outcome(state, result)
        if not state.is_half_inning_over():
            if adv.is_contact:
                self._resolve_in_play(state, result)
            elif adv.event == EVENT_WALK:
                self._resolve_walk(state, result)
            elif adv.event == EVENT_HIT_BY_PITCH:
                # SIM-509: identical force mechanics, its own canonical event.
                self._resolve_walk(state, result, event=EVENT_HIT_BY_PITCH)
            elif adv.event == EVENT_STRIKEOUT:
                self._resolve_strikeout(state, result)

        # End-of-PA: advance the batting order + run the manager sub hook, then
        # reset the count for the next batter (or roll the half-inning).
        self._end_of_pa(state, result)
        result.next_state = state
        return result

    # ===================================================================
    # Step 4 — SIM-056 count-conditional foul re-weight (before count advance)
    # ===================================================================

    def _full_pool_outcome(self, state: GameState) -> str:
        """SIM-424: draw a pitch outcome from the full-pool sampler.

        SIM-455 — the matchup weight has THREE parts with THREE lifetimes, and this
        method refreshes each one on its own trigger:

          * ``f_pitcher`` (``new_half_inning``) scores the WHOLE pool against the
            pitcher. It changes only when the pitcher or the batter hand changes, so
            it recomputes once per pitcher, not once per batter.
          * ``f_batter`` and the situation factor (``new_plate_appearance``) recompute
            when the batter changes OR the base-out state changes. Base-out moves
            INSIDE a plate appearance on a steal, a wild pitch or a caught stealing,
            and the situation factor must follow it.

        A pitcher change invalidates the plate-appearance weight too, because
        ``new_plate_appearance`` multiplies the base that ``new_half_inning`` builds.

        The count is NOT part of either key: the draw conditions on the live count per
        pitch via the bucket CDFs, and the §5.1 count machine consumes the result."""
        fp = self.full_pool_sampler
        hand = state.bat_hand if state.bat_hand in fp.a.pools else "R"
        season = int(getattr(state, "season", 2024) or 2024)

        # --- lifetime 1: f_pitcher, per (pitcher, hand, season, catcher) ------
        # SIM-517: the fielding catcher joins the key so the receiving factor
        # (staged in new_half_inning) follows a catcher change; at
        # catcher_sigma 0 the extra key member changes nothing (the fielding
        # side, and so the catcher, only changes with the pitcher).
        catcher = state.away_catcher_id if state.offense == Team.HOME else state.home_catcher_id
        pitcher_key = (state.pitcher_id, hand, season, catcher)
        if pitcher_key != self._fp_pitcher_key:
            self._fp_pitcher_key = pitcher_key
            # The new base invalidates the PA weight built on top of the old one.
            self._fp_pa_key = None
            fp.new_half_inning(
                hand,
                f"{state.pitcher_id}:{season}",
                catcher_key=(f"{int(catcher)}:{season}" if catcher is not None else None),
            )

        # --- lifetimes 2 + 3: f_batter (per batter) x f_situation (per base-out)
        bat = state.home_score if state.offense == Team.HOME else state.away_score
        fld = state.away_score if state.offense == Team.HOME else state.home_score
        score_diff = max(-5, min(5, int(bat) - int(fld)))
        # base-out only; the count is conditioned per pitch via the draw bucket.
        base_out = (int(state.outs), int(state.runners_state), int(state.inning), score_diff)
        pa_key = (state.batter_id, season, base_out)
        if pa_key != self._fp_pa_key:
            self._fp_pa_key = pa_key
            fp.new_plate_appearance(
                f"{state.batter_id}:{season}",
                np.array(base_out, dtype=np.float32),
            )
        # SIM-517: the framing flip that wrapped this draw is deleted — the
        # receiving kernel conditions the draw itself; the drawn row stands.
        outcome = fp.draw(state.balls, state.strikes)
        # SIM-517 part D: carry the drawn row's got-away fact to the resolvers
        # (read only when the flag is on — flag-off touches nothing).
        if self._got_away:
            self._last_pitch_got_away = fp.last_pitch_got_away()
        return outcome

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
        # SIM-491 part 3 (SIM-425b): pass the FIELDING team's defense map for
        # the fielder-quality kernel. A no-op unless the sampler's
        # fielder_sigma is set above 0 (SIM_FIELDER_KERNEL_SIGMA).
        defense = state.home_defense if state.defense == Team.HOME else state.away_defense
        fp.battedball_new_pa(
            hand,
            f"{state.batter_id}:{season}",
            sit,
            pitcher_throws=pthrows,
            bat_home=(state.offense == Team.HOME),
            park_run_factor=float(getattr(state, "park_run_factor", 1.0) or 1.0),
            defense_map=defense or None,
            live_season=season,
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

    # ===================================================================
    # SIM-511/512 — the transition fielding draw + the advancement draws
    # ===================================================================

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

    # ===================================================================
    # SIM-319 — run/base-out delta via resolve_runs (the ONE place, §8)
    # ===================================================================

    @staticmethod
    def _snapshot_bases(state: GameState) -> Bases:
        """Copy the current base state (SIM-499).

        Four resolvers mutate ``state.bases`` **in place** before they commit, so
        a caller that keeps a reference keeps the mutated object.  A run-value
        ledger fed that reference reads the state AFTER the play and calls it the
        state before.  Every caller of :meth:`_commit_run_delta` takes this copy
        first, at the top of the resolver, above every mutation.
        """
        b = state.bases
        return Bases(first=b.first, second=b.second, third=b.third)

    def _check_bases(self, bases: Bases, *, batter_id: int | None = None) -> None:
        """Assert the base state is legal (SIM-500).  Passing ``batter_id`` also
        asserts the batter is not already standing on a bag."""
        bases.assert_consistent(batter_id=batter_id)

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
    ) -> None:
        """Resolve + commit ONE play's run value and base-out delta (spec §8).

        This is the SINGLE place the loop turns a resolved play into a run value,
        a score change and an out count.

        **The caller supplies both base-out states (SIM-499).**  ``pre_outs`` and
        ``pre_bases`` are the state the caller measured BEFORE it touched
        anything.  The state AFTER the play is read from the live ``state`` here,
        which is ground truth at this point because every mutation has already
        happened.  Nothing derives either state.  The old code read
        ``state.outs`` / ``state.runners_state`` at commit time and called that
        the "before" state, but four callers mutate the bases first, so "before"
        was the after-state; and it derived the after-state by a conservation
        formula that has no term for a runner retired on the play.  Measured
        errors: a walk with a runner on first **+0.50 against a true +0.60**, a
        caught stealing **-0.24 against -0.62**, a sac fly **+0.76 against
        -0.13**, a steal of home **+1.00 against +0.11**.

        **The three transition facts** describe what happened to the bodies on
        the field, and go to :meth:`Bases.assert_transition` (SIM-500):

          * ``batter_reached`` — the batter BECAME a baserunner.  A batter
            retired at the plate is ``False`` and is NOT counted in
            ``runners_retired``; a home run is ``True`` and the batter is also
            one of ``runners_scored``.
          * ``runners_scored`` — bodies that crossed home, INCLUDING the batter
            on a home run.  This is a body count.  It is not always equal to
            ``result_runs``, which is the integer the pool supplied and the score
            commits; where they disagree, the pool row and the base state
            disagree, which is a defect elsewhere (see the class docstring of
            :meth:`_resolve_in_play`).
          * ``runners_retired`` — baserunners the fielders removed from a base.
            Outs on the batter are not in this count.

        The assertion CHECKS the conservation identity that SIM-499 deleted as a
        way to DERIVE.  Wrong as a derivation, right as a check: a checker is
        told the retired count that a derivation had to guess.

        ``result_hits`` does not feed the run value.  It stays on the signature
        because it names the shape of the play, and the SIM-496 acceptance probe
        reads it here to count batters who actually reached on an error.
        """
        if int(state.outs) != int(pre_outs):
            raise AssertionError(
                f"_commit_run_delta: the caller passed pre_outs={pre_outs} but the "
                f"live state already holds outs={state.outs}. Outs must be recorded "
                "HERE, after the run value is resolved, never before the commit."
            )
        # SIM-421: a sampled multi-out event (e.g. a GIDP carrying 2 outs) comes
        # from a DIFFERENT base-out context; clamp it to the outs that actually
        # remain in the half-inning so a double play with 2 already out ends the
        # inning at 3 rather than overflowing to 4 (the ceiling assertion in
        # _record_outs).
        outs_to_record = min(int(result_outs), max(0, OUTS_PER_INNING - int(pre_outs)))
        post_outs = int(pre_outs) + outs_to_record
        post_bases = state.bases

        # SIM-500: check the runner-conservation identity.  It is no longer used
        # to derive the after-state, so a failure here is a real defect in the
        # resolver that just ran, not an artefact of the ledger.
        # SIM-500: pass ``batter_id``. Without it, TWO of the guard's four checks
        # are dead at every ledger call site — the invented-runner/identity-swap
        # check and the batter-is-not-a-runner check both need to know who batted.
        pre_bases.assert_transition(
            post_bases,
            batter_reached=batter_reached,
            runners_scored=int(runners_scored),
            runners_retired=int(runners_retired),
            batter_id=state.batter_id,
        )

        rr = resolve_runs(
            event=event,
            pre_outs=int(pre_outs),
            pre_runners_state=int(pre_bases.runners_state),
            post_outs=post_outs,
            post_runners_state=int(post_bases.runners_state),
            result_runs=int(result_runs),
        )
        if rr.method != "re24_delta":
            raise AssertionError(
                f"_commit_run_delta: the ledger resolved by {rr.method!r}, not "
                "'re24_delta'. The context-free linear weight must never reach a "
                "committed run value (SIM-499)."
            )
        result.runs = rr.runs
        result.run_resolution_method = rr.method
        result.canonical_event = rr.canonical_event
        result.re_start = rr.re_start
        result.re_end = rr.re_end
        result.runs_scored += int(result_runs)
        if result_runs:
            state.add_runs(int(result_runs))
            state.assert_score_valid()
        if outs_to_record:
            result.outs_recorded += outs_to_record
            self._record_outs(state, outs_to_record)

    # ===================================================================
    # SIM-319 — Step 7 steal outcome (decision in pre-pitch hook, §3)
    # ===================================================================

    def _resolve_got_away_advance(self, state: GameState, result: PlayResult) -> None:
        """SIM-517 part D: the drawn pitch got away — every runner advances one
        base (scoring from third). The drawn row IS the play: its got-away
        fact came from a real passed ball / wild pitch, and those are scored
        precisely because a runner advanced. No rng, no outs, no batter
        involvement; a no-op with the bases empty (a got-away row drawn into
        an empty-bases live state moves nobody — nothing is scored there in
        real baseball either).

        Run accounting mirrors the steal-of-home commit (the one other
        mid-pitch scoring path): the delta routes through
        :meth:`_commit_run_delta`, the run carries no RBI (Rule 9.04 — the
        ``steal_runs_scored`` field is the no-RBI-on-this-pitch marker), and
        on a NON-terminal pitch the pitcher is charged here because
        ``_accumulate_pa`` never runs. The run counts EARNED: ~90% of real
        got-aways are wild pitches (earned, Rule 9.16); the pool flag merges
        the passed-ball minority, a box-stat nuance accepted knowingly.
        """
        b = state.bases
        if b.first is None and b.second is None and b.third is None:
            return
        pre_outs = int(state.outs)
        pre_bases = self._snapshot_bases(state)
        runs = 0
        scorer = pre_bases.third
        # Back to front so no move overwrites an occupied bag.
        if pre_bases.third is not None:
            self._move_runner(state, 3, 4)
            runs = 1
        if pre_bases.second is not None:
            self._move_runner(state, 2, 3)
            result.baserunner_advances[int(pre_bases.second)] = 3
        if pre_bases.first is not None:
            self._move_runner(state, 1, 2)
            result.baserunner_advances[int(pre_bases.first)] = 2
        self._check_bases(b)
        self._commit_run_delta(
            state,
            result,
            event="wild_pitch",
            result_hits=0,
            result_outs=0,
            result_runs=runs,
            pre_outs=pre_outs,
            pre_bases=pre_bases,
            batter_reached=False,
            runners_scored=runs,
            runners_retired=0,
        )
        if runs and scorer is not None:
            result.baserunner_advances[int(scorer)] = 0
            # No RBI on a got-away run (the same withholding a steal of home
            # uses — the field is the no-RBI marker, not steal-specific here).
            result.steal_runs_scored += runs
            self._box_line(int(scorer)).r += 1
            if not result.pa_terminal and state.pitcher_id is not None:
                self._box_line(int(state.pitcher_id)).r_allowed += 1
                outs_lost = self._half_inning_error_outs_lost
                if int(state.outs) + outs_lost < 3:
                    self._box_line(int(state.pitcher_id)).er += 1

    def _resolve_steal_outcome(self, state: GameState, result: PlayResult) -> None:
        """Resolve a steal staged by the pre-pitch hook (§3 item 4 / step 7).

        On a SAFE steal the runner advances one base (scoring on a steal of home);
        on a CAUGHT stealing the runner is removed and ONE out is recorded — and
        the out + any base-out delta is routed through
        :meth:`_commit_run_delta` -> ``resolve_runs`` (never inline).  Records
        ``steal_attempted`` / ``steal_outcome`` on the ``PlayResult``.  A no-op
        when no steal was staged.
        """
        steal = self._pending_steal
        # Consume + clear the staged attempt so it never carries to the next pitch.
        self._pending_steal = None
        if steal is None or not steal.attempted:
            return
        if steal.pickoff:
            # SIM-507: a pickoff outcome is not a steal attempt — it never
            # sets steal_attempted / steal_outcome or the SB/CS-band-visible
            # box credits except the advancing-out CS (Rule 9.07(h)).
            self._resolve_pickoff(state, result, steal)
            return
        result.steal_attempted = True
        from_base = steal.from_base
        to_base = (
            steal.to_base
            if steal.to_base is not None
            else (_NEXT_BASE.get(from_base, 4) if from_base else 4)
        )
        rid = steal.runner_id
        # SIM-499: measure the base-out state before _move_runner / _clear_base
        # mutate it in place.  ``moving`` is the runner actually standing on the
        # from-base: a steal staged from an empty bag moves no body, so the
        # transition facts below must say so rather than assume one.
        pre_outs = int(state.outs)
        pre_bases = self._snapshot_bases(state)
        moving = {1: pre_bases.first, 2: pre_bases.second, 3: pre_bases.third}.get(from_base)
        if steal.safe:
            result.steal_outcome = STEAL_SAFE
            # Move the runner off the from-base onto the to-base (or home).
            self._move_runner(state, from_base, to_base)
            # SIM-365: credit the stolen base to the runner.  Steals are
            # accumulated HERE (the single steal-commit site) rather than in
            # :meth:`_accumulate_pa` so a steal on a NON-terminal pitch — which
            # never reaches end-of-PA — is still counted.
            if rid is not None:
                self._box_line(int(rid)).sb += 1
            if to_base >= 4:
                # Steal of home: one run scores -> route through resolve_runs.
                # The runner leaves 3B, so he is a body that scored.  The old
                # ledger read the already-cleared bases as the pre-state and
                # recorded +1.00 against a true +0.11.
                self._commit_run_delta(
                    state,
                    result,
                    event="stolen_base_home",
                    result_hits=0,
                    result_outs=0,
                    result_runs=1,
                    pre_outs=pre_outs,
                    pre_bases=pre_bases,
                    batter_reached=False,
                    runners_scored=1 if moving is not None else 0,
                    runners_retired=0,
                )
                # SIM-483: mark the run as a STEAL run so the terminal-pitch
                # accumulator can withhold the batter's RBI credit (Rule
                # 9.04(b) awards no RBI on a stolen base).
                result.steal_runs_scored += 1
                if rid is not None:
                    result.baserunner_advances[rid] = 0
                    # SIM-365: the runner scored on the steal of home.
                    self._box_line(int(rid)).r += 1
                # SIM-365: charge the pitcher the run.  Only on a NON-terminal
                # pitch — on a terminal pitch ``_accumulate_pa`` will add this
                # play's ``runs_scored`` (which includes this run) to r_allowed,
                # so crediting here too would double-count.
                if not result.pa_terminal and state.pitcher_id is not None:
                    self._box_line(int(state.pitcher_id)).r_allowed += 1
                    # SIM-483: a stolen-base run is EARNED (Rule 9.16(a)), and
                    # the non-terminal path never reaches ``_accumulate_pa``'s
                    # ER credit — without this line every non-terminal steal of
                    # home under-counted the pitcher's ER. Unearned only when
                    # an earlier error means the inning should already be over
                    # (the same rule ``_accumulate_pa`` applies).
                    outs_lost = self._half_inning_error_outs_lost
                    if int(state.outs) + outs_lost < 3:
                        self._box_line(int(state.pitcher_id)).er += 1
            elif rid is not None:
                result.baserunner_advances[rid] = to_base
        else:
            result.steal_outcome = STEAL_CAUGHT
            # SIM-426: charge the caught stealing to the runner.
            if rid is not None:
                self._box_line(int(rid)).cs += 1
            # Remove the caught runner from his base, then route the OUT delta
            # through resolve_runs (no inline out arithmetic).
            self._clear_base(state, from_base)
            # A caught stealing RETIRES a baserunner.  The deleted conservation
            # formula had no term for that, so it kept the runner on base and
            # recorded -0.24 against a true -0.62.  The caller states the retired
            # count, so the ledger no longer has to guess it.
            self._commit_run_delta(
                state,
                result,
                event="caught_stealing",
                result_hits=0,
                result_outs=1,
                result_runs=0,
                pre_outs=pre_outs,
                pre_bases=pre_bases,
                batter_reached=False,
                runners_scored=0,
                runners_retired=1 if moving is not None else 0,
            )
            if rid is not None:
                result.baserunner_advances[rid] = 0  # out (off the bases)

    def _resolve_pickoff(
        self, state: GameState, result: PlayResult, steal: StealResolution
    ) -> None:
        """SIM-507: resolve a pickoff outcome staged by the pre-pitch draw.

        Three outcomes, mirroring the labeled pool rows:
          * out, advancing (``safe=False, pickoff_advancing=True``) — a
            picked-off CAUGHT STEALING: the runner broke for the next base and
            was tagged. One out, the runner is charged a CS (Rule 9.07(h)) —
            the CS band's MLB reference counts this class.
          * out, plain (``safe=False``) — tagged at his own base. One out,
            NO caught-stealing credit.
          * error (``safe=True``) — an errant throw; the runner advances one
            base. No steal credit and no ledger delta, the same shape as a
            safe steal to 2B/3B (occupancy moves; no run, no out).
        """
        pre_outs = int(state.outs)
        pre_bases = self._snapshot_bases(state)
        from_base = steal.from_base
        to_base = (
            steal.to_base
            if steal.to_base is not None
            else (_NEXT_BASE.get(from_base, 4) if from_base else 4)
        )
        rid = steal.runner_id
        moving = {1: pre_bases.first, 2: pre_bases.second, 3: pre_bases.third}.get(from_base)
        if steal.safe:
            result.pickoff_error = True
            self._move_runner(state, from_base, to_base)
            if rid is not None:
                result.baserunner_advances[rid] = to_base
            return
        result.pickoff_out = True
        if steal.pickoff_advancing and rid is not None:
            self._box_line(int(rid)).cs += 1
        self._clear_base(state, from_base)
        self._commit_run_delta(
            state,
            result,
            event="caught_stealing" if steal.pickoff_advancing else "pickoff",
            result_hits=0,
            result_outs=1,
            result_runs=0,
            pre_outs=pre_outs,
            pre_bases=pre_bases,
            batter_reached=False,
            runners_scored=0,
            runners_retired=1 if moving is not None else 0,
        )
        if rid is not None:
            result.baserunner_advances[rid] = 0

    @staticmethod
    def _move_runner(state: GameState, from_base: int | None, to_base: int) -> None:
        """Move a runner from ``from_base`` to ``to_base`` (4 == home/off bases)."""
        b = state.bases
        rid = {1: b.first, 2: b.second, 3: b.third}.get(from_base)
        StateMachine._clear_base(state, from_base)
        if to_base == 1:
            b.first = rid
        elif to_base == 2:
            b.second = rid
        elif to_base == 3:
            b.third = rid
        # to_base >= 4: scored — runner leaves the bases entirely.
        # No base-state check here: this is a low-level primitive with no ``self``,
        # and every caller validates once its whole move sequence is finished. A
        # check mid-sequence would fire on a legal intermediate state.

    @staticmethod
    def _clear_base(state: GameState, base: int | None) -> None:
        b = state.bases
        if base == 1:
            b.first = None
        elif base == 2:
            b.second = None
        elif base == 3:
            b.third = None

    # ===================================================================
    # Terminal-PA resolution — fielding (step 6) + baserunning (step 7)
    # ===================================================================

    def _resolve_walk(self, state: GameState, result: PlayResult, event: str = EVENT_WALK) -> None:
        """Resolve a ball-4 walk (§5.1 / §7): force the runners + score any
        forced run, routing the run/base-out delta through ``resolve_runs``.

        SIM-509: a HIT BY PITCH awards 1B with the same force mechanics, so it
        resolves here too — pass ``event=EVENT_HIT_BY_PITCH`` and the play
        carries its own canonical event (never counted as a BB).

        A walk forces the batter to 1B and pushes each runner ahead only when the
        bag behind him is occupied (a true force).  The number of forced runs is
        computed from the base state, then handed to :meth:`_commit_run_delta`.

        SIM-414: each forced advance is also recorded in
        ``result.baserunner_advances`` so the per-runner R credit in
        :meth:`_accumulate_pa` fires for a walk-forced run (previously a
        documented under-count that made the boxscore disagree with the
        linescore once shown together).
        """
        result.event = event
        result.pa_terminal = True
        b = state.bases
        # SIM-499: measure the base-out state BEFORE the force pushes anyone.
        # ``b`` is mutated in place below, so the ledger needs a copy, not a
        # reference — the reference is what made the old ledger read the
        # post-walk state and call it the pre-walk state.
        pre_outs = int(state.outs)
        pre_bases = self._snapshot_bases(state)
        # Capture pre-walk runner identities before the base mutation overwrites
        # them — the advances dict is keyed on the runner who moved, not the bag.
        rid_1 = b.first
        rid_2 = b.second
        rid_3 = b.third
        # A walk forces runners only on consecutive occupancy from 1B.
        forced_run = 0
        if rid_1 is not None:
            if rid_2 is not None:
                if rid_3 is not None:
                    forced_run = 1  # bases loaded -> run forced home
                    # Runner on 3B scored (end_base == 0 triggers the R credit).
                    result.baserunner_advances[int(rid_3)] = 0
                # push 2B -> 3B
                b.third = rid_2
                result.baserunner_advances[int(rid_2)] = 3
            # push 1B -> 2B
            b.second = rid_1
            result.baserunner_advances[int(rid_1)] = 2
        # Batter to 1B.
        #
        # SIM-500: assign UNCONDITIONALLY. The old form was
        #     b.first = state.batter_id if state.batter_id is not None else b.first
        # which kept the OLD occupant of 1B whenever no batter was set — the
        # count-machine path, which runs with no lineups. But the push above has
        # already moved that same runner to 2B, so the fallback left ONE runner id
        # standing on BOTH bags: a physically impossible state that then fed the
        # steal, situation and run-value logic. The SIM-500 guard caught it on its
        # first run ("runner 105 stands on both 1B and 2B").
        #
        # With no batter, 1B is simply vacant: the runner advanced and nobody
        # replaced him. That under-counts the walk by one baserunner on a path that
        # has no batter to count, which is the only self-consistent reading. Every
        # path that HAS a batter is unchanged.
        b.first = state.batter_id
        self._check_bases(b)
        if state.batter_id is not None:
            result.baserunner_advances[state.batter_id] = 1
        # Route the (possibly forced) run through resolve_runs — no inline runs.
        # The walk's after-state is deterministic: the batter reaches 1B (unless
        # no batter is set, the count-machine path), and any forced run is a body
        # that left the bases.  No runner is retired on a walk.
        self._commit_run_delta(
            state,
            result,
            event=event,
            result_hits=0,
            result_outs=0,
            result_runs=forced_run,
            pre_outs=pre_outs,
            pre_bases=pre_bases,
            batter_reached=state.batter_id is not None,
            runners_scored=forced_run,
            runners_retired=0,
        )

    def _issue_intentional_walk(self, state: GameState) -> PlayResult:
        """Issue an intentional walk signalled by the manager (SIM-323 §3 item 2).

        No pitch is thrown: the batter is awarded first base via the same
        forced-runner mechanics as a ball-4 walk, the run-resolution provenance is
        attached, and the PA is rolled over (batting order advances, count resets
        / half-inning rolls).  Returns the :class:`PlayResult` (``pitch_outcome``
        carries the sentinel ``"ball"`` since an IBB is recorded as four balls).

        SIM-515/516: the play carries its OWN canonical event
        (``intentional_walk`` — the SIM-509 HBP pattern), so the box still
        credits a BB/non-AB while the lane's IBB_PA pool band can see it. The
        first certifying lane proved the old ``walk`` label unreadable: the
        IBB numerator read zero and the surplus polluted BB_PA.
        """
        result = PlayResult(
            pitch_outcome="ball",
            is_contact=False,
            pa_terminal=True,
            event=EVENT_INTENTIONAL_WALK,
        )
        # Consume the signal so it never carries to the next pitch / PA.
        state.manager.intentional_walk_signalled = False
        self._resolve_walk(state, result, event=EVENT_INTENTIONAL_WALK)
        self._end_of_pa(state, result)
        result.next_state = state
        return result

    def _resolve_strikeout(self, state: GameState, result: PlayResult) -> None:
        """Resolve a strike-3 strikeout (§5.1) incl. the dropped-third-strike
        edge (§5.4).

        Ordinary K: route a one-out delta through ``resolve_runs``.  Dropped
        third strike (uncaught swinging strike-3 with **1B open OR two outs**):
        the batter may reach first — modelled here as a reach (no out, batter to
        1B) when the drawn pitch got away; the run/base-out delta still goes
        through ``resolve_runs``.
        """
        result.event = EVENT_STRIKEOUT
        result.pa_terminal = True
        # SIM-517: read the D3K predicate BEFORE any base movement (the
        # official rule reads the pre-pitch state), then let a got-away
        # strike-3 that CANNOT award first (1B occupied, under two outs)
        # still advance the runners — the ball still got away. The advance
        # commits its own delta; the K's snapshots below then measure the
        # post-advance state, so the two commits chain like a steal + K.
        d3k = self._dropped_third_strike(state, result)
        if not d3k and self._last_pitch_got_away:
            self._resolve_got_away_advance(state, result)
        # SIM-499: measure the base-out state before _force_on_reach can push
        # anyone.  ``_force_on_reach`` mutates ``state.bases`` in place.
        pre_outs = int(state.outs)
        pre_bases = self._snapshot_bases(state)
        if d3k:
            # Uncaught K3: batter reaches 1B (no out recorded), pushing forced
            # runners exactly like a walk does.  resolve_runs scores any force.
            result.event = "strikeout"  # still a K event; batter reached on D3K
            forced_run = self._force_on_reach(state, result)
            # This is the ONE path in the loop that puts a batter on first on an
            # error, so it is the one place the ledger's reach-on-error value is
            # observable today.  It recorded +1.10 against a true +0.38 before
            # SIM-499, because it read the post-reach bases as the pre-state.
            self._commit_run_delta(
                state,
                result,
                event="field_error",  # batter safe at 1B (no out)
                result_hits=1,
                result_outs=0,
                result_runs=forced_run,
                pre_outs=pre_outs,
                pre_bases=pre_bases,
                batter_reached=state.batter_id is not None,
                runners_scored=forced_run,
                runners_retired=0,
            )
            return
        # Ordinary strikeout: one out, no base/score change beyond the out.  The
        # batter is retired at the plate, so he never becomes a runner and he is
        # NOT a retired baserunner: the bases do not move at all.
        self._commit_run_delta(
            state,
            result,
            event=EVENT_STRIKEOUT,
            result_hits=0,
            result_outs=1,
            result_runs=0,
            pre_outs=pre_outs,
            pre_bases=pre_bases,
            batter_reached=False,
            runners_scored=0,
            runners_retired=0,
        )

    def _dropped_third_strike(self, state: GameState, result: PlayResult) -> bool:
        """The §5.4 dropped-third-strike predicate.

        The edge is *eligible* only on a swinging strike-3 when first base is
        OPEN or there are two outs (the official rule).  Whether the ball got
        away is the DRAWN PITCH ROW's own fact (SIM-517: the row was a real
        uncaught third strike — no roll, no formula).  Without that fact the
        edge does NOT fire (an ordinary K).
        """
        if result.pitch_outcome != "swinging_strike":
            return False
        first_base_open = state.bases.first is None
        two_outs = state.outs >= OUTS_PER_INNING - 1
        if not (first_base_open or two_outs):
            return False
        return bool(self._last_pitch_got_away)

    def _force_on_reach(self, state: GameState, result: PlayResult) -> int:
        """Place the batter on 1B and force runners behind him (shared by the
        walk and the dropped-third-strike reach).  Returns forced runs."""
        b = state.bases
        forced_run = 0
        if b.first is not None:
            if b.second is not None:
                if b.third is not None:
                    forced_run = 1
                b.third = b.second
            b.second = b.first
        b.first = state.batter_id if state.batter_id is not None else b.first
        self._check_bases(b)
        if state.batter_id is not None:
            result.baserunner_advances[state.batter_id] = 1
        return forced_run

    # The SIM-349 sac-fly-intent nudge (_SAC_FLY_ELIGIBLE_OUTS +
    # _apply_sac_fly_bias + _maybe_sac_fly_intent) lived here from 2026-05 to
    # 2026-08-19. SIM-513 retired it: the SIM-512 tag draw from 3B produces
    # sacrifice flies naturally from real data, at the runner's and the
    # fielder's own rates, and relabels the play post-hoc.

    # SIM-476 (owner ruling 2026-08-30): the SIM-412 home-field flip
    # (_HOME_FIELD_BIAS_DEFAULT / _home_field_bias / _apply_home_field_bias)
    # is DELETED. Home advantage is now the SIM-491 home kernel at
    # SIM_HOME_OFF_WEIGHT=0.0 — the batted-ball draw hard-conditions on the
    # batting side (bat_home), delivering the pool's own home/away
    # differential (+0.107 R/g measured, the MLB size). See
    # docs/audit/2026-08-28-sim476-fit-plan.md part 2.

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

    # ===================================================================
    # End-of-PA + half-inning control (§5.3 / §6.1)
    # ===================================================================

    def _end_of_pa(self, state: GameState, result: PlayResult) -> None:
        """Run the end-of-PA bookkeeping (§5.3): advance the batting order,
        run the manager substitution hook, then either reset the count for the
        next batter or roll the half-inning if the third out just landed.
        """
        # SIM-328: attribute the completed PA's stats to the batter (offense) and
        # the pitcher (defense) BEFORE the batting order advances — at this point
        # ``state.batter_id`` / ``state.pitcher_id`` still identify the players who
        # just finished the PA.
        self._accumulate_pa(state, result)

        # SIM-434: count the batter the CURRENT pitcher just faced (a terminal PA)
        # so the times-through-the-order effectiveness decay has an input.  A pure
        # counter bump keyed by pitcher id, read only by the manager model -> inert
        # (byte-identical) while ``manager is None``.
        pid = state.pitcher_id
        if pid is not None:
            state.pitcher_bf[pid] = state.pitcher_bf.get(pid, 0) + 1

        # Advance the batting-order pointer for the team that just batted.
        self._advance_batting_order(state)

        # End-of-PA manager hook (§5.3) — SIM-323 owns substitution/IBB/bunt.
        self._end_of_pa_hook(state)

        if state.is_half_inning_over():
            # The third out ended the half-inning (§6.1): the half-inning roll
            # clears bases + resets count/outs + flips the half.
            self.advance_half_inning(state)
        else:
            # Same half-inning, next batter: just reset the count.
            state.reset_count()
            state.batter_pa_count = 0
            # Re-validate the committed, ready-for-next-pitch state.
            state.assert_invariants(in_play=True)

    # ===================================================================
    # SIM-328 — per-player sim-average accumulation (inside the PA loop)
    # ===================================================================

    #: Canonical PA outcomes that are NOT at-bats (PA but not AB): walks, HBP,
    #: and the two productive outs (sac fly / sac bunt).  Everything else that is
    #: a terminal PA counts as an AB.
    _NON_AB_CANONICAL: frozenset[str] = frozenset(
        {
            "walk",
            "intentional_walk",
            "hit_by_pitch",
            "sacrifice_fly",
            "sacrifice_hit",
        }
    )
    #: Canonical PA outcomes that are base hits.
    _HIT_CANONICAL: frozenset[str] = frozenset(
        {
            "single",
            "double",
            "triple",
            "home_run",
        }
    )
    #: Canonical PA outcomes that count as a walk against the pitcher.
    _BB_CANONICAL: frozenset[str] = frozenset({"walk", "intentional_walk"})

    @staticmethod
    def _current_batter_id(state: GameState) -> int | None:
        """The id of the batter who just completed THIS PA (SIM-328 attribution).

        Reads the OFFENSE's lineup at its current slot pointer — the authoritative
        "batter currently up" — when a lineup is wired.  This is preferred over
        the loose ``state.batter_id`` because the loop leaves ``batter_id`` stale
        at the prior (now-fielding) team's hitter for the FIRST PA of each new
        half (``_advance_batting_order`` only repoints it at end-of-PA); reading
        the offense's slot here credits the correct hitter from the half's first
        PA without changing the locked ``batter_id`` / half-flip contract
        (SIM-316).  Falls back to ``state.batter_id`` when no lineup is wired (the
        count-machine-only tests).
        """
        if state.offense == Team.HOME:
            lineup, slot = state.home_lineup, state.home_lineup_slot
        else:
            lineup, slot = state.away_lineup, state.away_lineup_slot
        if lineup:
            return lineup[slot % len(lineup)]
        return state.batter_id

    def _box_line(self, player_id: int) -> PlayerStatLine:
        """Lazily create the boxscore and return a player's line (SIM-365).

        Mirrors the ``if self.boxscore is None`` guard inside
        :meth:`_accumulate_pa`, so steal accumulation (which happens in
        :meth:`_resolve_steal_outcome`, possibly on a non-terminal pitch that
        never reaches end-of-PA) shares the same per-game store.
        """
        if self.boxscore is None:
            self.boxscore = BoxScore()
        return self.boxscore.line(int(player_id))

    def _accumulate_pa(self, state: GameState, result: PlayResult) -> None:
        """Attribute one completed terminal PA to the batter + pitcher (SIM-328).

        Called from :meth:`_end_of_pa` BEFORE the batting order advances, so
        ``state.batter_id`` (offense) and ``state.pitcher_id`` (defense) still
        name the two players who just resolved the PA — which is exactly the
        attribution AC-2 requires (and the half-inning flip switches them
        automatically, since the ids are read off the live ``GameState``).

        Classification uses the resolved CANONICAL event
        (``result.canonical_event``, set by :meth:`_commit_run_delta` via
        :func:`run_resolution.resolve_runs`); when that is absent (e.g. a
        count-machine-only in-play terminal with no sampler) we fall back to
        :func:`resolve_event_to_canonical` on the raw ``result.event``.  A PA with
        no resolvable event (pure count-machine in-play hand-off, event is None)
        is NOT credited — there is no scored outcome to attribute.

        BATTER:
          * AB  — every terminal PA EXCEPT a walk / IBB / HBP / sac fly / sac
            bunt (the standard PA-minus-(BB+HBP+SF+SH) at-bat definition).
          * H   — single / double / triple / home_run.
          * HR  — home_run.
          * RBI — the integer runs that physically scored on this play
            (``result.runs_scored``), MINUS runs we treat as unearned-by-error
            (see the earned/unearned split below) — RBI is not credited on an
            error-driven run, mirroring the ER simplification.  A solo HR drives
            in 1 (the batter himself scores), a 3-run HR drives in 3.

        PITCHER:
          * IP  — ``result.outs_recorded`` (thirds of an inning; accumulated as
            outs on the line, rendered x.0/x.1/x.2 via ``PlayerStatLine.ip``).
          * K   — a strikeout PA (canonical 'strikeout'); credited even on a
            dropped-third-strike reach (no out) per the scoring rule.
          * BB  — a walk / IBB.
          * ER  — earned runs charged: the runs that scored on this play, EXCEPT
            when the play carried an error (``result.is_error``), in which case
            we treat the run(s) as UNEARNED and do not charge the pitcher.

        EARNED/UNEARNED — INNING-RECONSTRUCTION (SIM-414): full ER scoring per
        MLB Rule 9.16(b) treats as UNEARNED any run that would not have scored
        had the defense played errorlessly — including runs that score AFTER the
        inning "should have ended".  We approximate this by tracking
        ``_half_inning_error_outs_lost`` (incremented when an error play
        recorded no outs — the canonical reach-on-error) and flagging the run as
        unearned if ``outs_before_this_play + error_outs_lost >= 3``.  Combined
        with the per-play ``is_error`` flag, this captures the two dominant
        unearned-run patterns: (1) runs that score on an error play, (2) runs
        that score in an extended inning.  The rarer "runner reached on an
        error and later scored" case requires per-runner provenance and is
        deferred (a future ticket can add it).  RBI tracks ``is_error`` only
        (per Rule 9.04: a clean RBI in an extended inning still counts).
        """
        if self.boxscore is None:
            self.boxscore = BoxScore()
        box = self.boxscore

        canonical = result.canonical_event
        if canonical is None:
            canonical = resolve_event_to_canonical(result.event)

        runs = int(result.runs_scored or 0)
        outs = int(result.outs_recorded or 0)
        is_error = bool(result.is_error)

        # SIM-414: inning-reconstruction unearned-run flag.  ``state.outs`` has
        # already been incremented by this play, so the count BEFORE the play
        # is ``state.outs - outs``.  If errors earlier in the half-inning
        # prevented an out, the effective out total may already be >= 3 at the
        # start of this play — any run that scores here is unearned even when
        # this play itself is clean.
        outs_before_play = max(0, int(state.outs) - outs)
        effective_outs_before_play = outs_before_play + self._half_inning_error_outs_lost
        inning_should_have_ended = effective_outs_before_play >= 3
        run_is_unearned = is_error or inning_should_have_ended

        batter_id = self._current_batter_id(state)

        # ---- batter (offense) ----
        if batter_id is not None:
            bat = box.line(int(batter_id))
            if canonical is not None and canonical not in self._NON_AB_CANONICAL:
                bat.ab += 1
            if canonical in self._HIT_CANONICAL:
                bat.h += 1
                if canonical == "home_run":
                    bat.hr += 1
                # SIM-365: doubles / triples are a subset of ``h`` (so TB can be
                # computed exactly downstream); singles need no field (h - b2 - b3
                # - hr).  ADDITIVE on top of the existing ``h`` credit above.
                elif canonical == "double":
                    bat.b2 += 1
                elif canonical == "triple":
                    bat.b3 += 1
            # RBI: runs the batter drove in (not credited on an error-driven
            # run). SIM-483: a run that scored ON A STEAL (a terminal-pitch
            # steal of home folds its run into ``runs``) earns the batter NO
            # RBI — MLB Rule 9.04(b) awards none on a stolen base.
            rbi_runs = max(0, int(runs) - int(result.steal_runs_scored))
            if rbi_runs and not is_error:
                bat.rbi += rbi_runs

        # ---- runs scored (SIM-365): credit each runner who crossed home on this
        # play.  ``baserunner_advances`` records ``end_base == 0`` for a runner who
        # SCORED (set in :meth:`_resolve_in_play_transition` for a runner or the
        # batter who crossed home, and SIM-414 for the runner on 3B forced home
        # by a bases-loaded walk).  Steals are credited entirely in :meth:`_resolve_steal_outcome`
        # (so a steal on a NON-terminal pitch — which never reaches this method —
        # is still counted, and a caught-stealing ``0`` is never mistaken for a
        # scored run); we therefore skip run attribution on a steal PA here to
        # avoid double-counting a steal-of-home.
        if not result.steal_attempted:
            for rid, end_base in result.baserunner_advances.items():
                if int(end_base) == 0:
                    box.line(int(rid)).r += 1

        # ---- pitcher (defense) ----
        if state.pitcher_id is not None:
            pit = box.line(int(state.pitcher_id))
            pit.outs_recorded += outs
            if canonical == "strikeout":
                pit.k += 1
            if canonical in self._BB_CANONICAL:
                pit.bb += 1
            # SIM-365: a base hit charged to the pitcher.
            if canonical in self._HIT_CANONICAL:
                pit.h_allowed += 1
            # ER: scored runs charged to the pitcher unless the run is unearned —
            # SIM-414: now also excludes runs that score after the inning "should
            # have ended" (an earlier error prevented an out).
            if runs and not run_is_unearned:
                pit.er += runs
            # SIM-365: runs allowed (R) include UNEARNED runs too, so unlike ER
            # there is no ``is_error`` exclusion — R >= ER by construction.  A
            # steal-of-home on a NON-terminal pitch is charged in
            # :meth:`_resolve_steal_outcome` (it never reaches this method);
            # a terminal-pitch steal-of-home's run is already in ``runs`` here.
            if runs:
                pit.r_allowed += runs

        # SIM-414: a reach-on-error (the canonical error play — outs_recorded == 0
        # with is_error == True) counts as one missed out; subsequent runs in the
        # same half-inning will be unearned once effective_outs reaches 3.
        if is_error and outs == 0:
            self._half_inning_error_outs_lost += 1

    def advance_half_inning(self, state: GameState) -> None:
        """Roll the half-inning at 3 outs (spec §6.1).

        * clear the bases,
        * reset the count and outs to 0,
        * flip the half (TOP -> BOTTOM, or BOTTOM -> next inning's TOP),
        * advance the inning number on the BOTTOM -> TOP transition,
        * carry each side's batting-order pointer forward (it already lives on
          the GameState per-team, so the next half-inning for *that* team resumes
          at the right batter — nothing to copy, just don't touch the other
          team's pointer),
        * run the half-inning-boundary manager hook (§5.3).

        # TODO(SIM-320): walk-off / extra-innings / game-over predicates live in
        # the full ``simulate_game()`` loop control; this method only performs
        # the unconditional half-inning roll.  The ghost-runner-on-2B for extra
        # innings (§6.2) is also a SIM-320 concern (it would seed bases here for
        # inning >= 10).
        """
        # Inspect the transient terminal state before clearing it.
        state.assert_outs_valid(in_play=False)  # tolerate outs == 3 here

        state.bases.clear()
        state.reset_outs()
        state.reset_count()
        # SIM-414: errors don't carry across half-innings.
        self._half_inning_error_outs_lost = 0

        if state.half == Half.TOP:
            state.half = Half.BOTTOM
        else:
            state.half = Half.TOP
            state.inning += 1

        # The per-team batting-order pointers already persist on the GameState,
        # so the side coming to bat resumes where it left off automatically.
        # SIM-421: re-point the matchup at the new half's offense/defense (the
        # due-up batter + the fielding team's pitcher) so the sampler pre-filter
        # follows the game instead of freezing at the opening matchup.
        self._set_half_matchup(state)

        # Half-inning-boundary manager hook (§5.3) — SIM-323.
        self._end_of_pa_hook(state)

        # The freshly-rolled state must satisfy the live invariants.
        state.assert_invariants(in_play=True)

    # ===================================================================
    # Out / batting-order primitives (guarded)
    # ===================================================================

    def _record_outs(self, state: GameState, n: int) -> None:
        """Record ``n`` outs, guarding against an impossible (>3) total.

        Uses the SIM-311 mutator + the lightweight ``assert_outs_valid`` guard:
        during live play outs may transiently reach 3 (the third out, which the
        half-inning roll then clears), so this validates with ``in_play=False``
        to allow the terminal 3 but reject 4+.
        """
        if n < 0:
            raise ValueError("cannot record a negative number of outs.")
        state.record_out(n)
        # Allow the transient third out; the half-inning roll resets it.
        state.assert_outs_valid(in_play=False)

    def _advance_batting_order(self, state: GameState) -> None:
        """Advance the batting team's lineup-slot pointer by one, wrapping at the
        lineup length, and point ``batter_id`` at the new slot (spec §6.1).

        No-op when the relevant lineup is empty (the count-machine tests run
        without a populated lineup).
        """
        if state.offense == Team.HOME:
            lineup = state.home_lineup
            if lineup:
                state.home_lineup_slot = (state.home_lineup_slot + 1) % len(lineup)
                state.batter_id = lineup[state.home_lineup_slot]
        else:
            lineup = state.away_lineup
            if lineup:
                state.away_lineup_slot = (state.away_lineup_slot + 1) % len(lineup)
                state.batter_id = lineup[state.away_lineup_slot]
        # SIM-421: the matchup pre-filter must follow the new batter's hand (was
        # frozen at the leadoff hand for the whole game -> always one tile half).
        state.bat_hand = state.bat_hand_for(state.batter_id)

    def _set_half_matchup(self, state: GameState) -> None:
        """Re-point the matchup at the new half's offense + defense (SIM-421).

        Called from :meth:`advance_half_inning` AFTER the half flips: the side now
        batting resumes at its persisted slot pointer (its due-up batter), and the
        pitcher swaps to the fielding team's starter (away pitches when home bats,
        and vice versa), refreshing the ``throw_hand`` + ``bat_hand`` pre-filters.
        Each step is guarded so the count-machine test path (no lineups / no
        pitcher map) is a no-op and keeps its fixed matchup.
        """
        if state.offense == Team.HOME:
            lineup, slot = state.home_lineup, state.home_lineup_slot
            pitcher = state.away_pitcher_id  # away team pitches when home bats
        else:
            lineup, slot = state.away_lineup, state.away_lineup_slot
            pitcher = state.home_pitcher_id
        if lineup:
            state.batter_id = lineup[slot % len(lineup)]
        if pitcher is not None:
            # SIM-434: when the manager model is active, save the OUTGOING pitcher's
            # accrued count and restore the INCOMING pitcher's own count so the two
            # starters don't share one ledger across half-innings (the pull gate
            # reads the live ``pitcher_pitch_count``).  Manager-gated + manager-only-
            # read -> byte-identical with the flag off.  Once a starter is pulled
            # mid-game, the half-swap correctly resumes the reliever-in-place / the
            # opposing starter without resurrecting the pulled arm's count.
            if self.manager is not None and state.pitcher_id is not None:
                state.pitcher_pc[state.pitcher_id] = int(state.pitcher_pitch_count)
            state.pitcher_id = pitcher
            state.throw_hand = state.throw_hands.get(pitcher, state.throw_hand)
            if self.manager is not None:
                state.pitcher_pitch_count = int(state.pitcher_pc.get(pitcher, 0))
        state.bat_hand = state.bat_hand_for(state.batter_id)

    # ===================================================================
    # Manager hooks (§3 / §5.3) — SIM-323 owns the logic
    # ===================================================================

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

    def _tendency(self, name: str, default: float = 0.0) -> float:
        """Read a single manager-tendency rate by name (SIM-323).

        Reads from the injected ``self.manager`` source, which may be EITHER:

          * a mapping / attribute-bearing object exposing the tendency *directly*
            by name (e.g. a dict ``{"steal_order_rate_per_1b_opp": 0.4}`` or a
            ``ManagerContext`` subtype), OR
          * a :class:`ManagerProfile`-shaped object exposing the SIM-2.8
            ``usage_vec`` / ``aggression_vec`` / ``platoon_vec`` arrays (read by
            position via :data:`_MANAGER_TENDENCY_INDEX`).

        Returns ``default`` (0.0) when no profile is wired or the name is absent —
        so an unwired machine makes EVERY manager decision a no-op (the SIM-320/
        324/326 no-DB test path stays green).  Values are coerced to float and
        NaN-guarded.
        """
        src = self.manager
        if src is None:
            return float(default)
        # 1) direct mapping access.
        if isinstance(src, dict):
            val = src.get(name)
            if val is not None:
                return _safe_float(val, default)
        # 2) direct attribute access (object exposing the rate by name).
        if hasattr(src, name):
            return _safe_float(getattr(src, name), default)
        # 3) ManagerProfile-shaped vectors, read by position.
        slot = _MANAGER_TENDENCY_INDEX.get(name)
        if slot is not None:
            vec = getattr(src, slot[0], None)
            if vec is not None:
                try:
                    return _safe_float(vec[slot[1]], default)
                except (IndexError, TypeError):
                    return float(default)
        return float(default)

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
          2. **Pitch-out** (§3 item 3) — with a steal threat (runner on 1B/2B,
             1B/2B not the lead) and a high-leverage spot, a manager who runs the
             bases a lot (proxy: their own ``steal_order_rate``) anticipates the
             opponent and signals a pitch-out for THIS pitch.
          3. **Steal green-light** (§3 item 4) — sets whether the SIM-319 steal
             path *may* attempt this pitch: the manager's
             ``steal_order_rate_per_1b_opp`` tendency, scaled DOWN in low leverage
             and UP late-and-close, vs a loop-rng draw, written to
             ``ManagerContext.green_light_rate``.

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
        # Reset the per-pitch signals (they apply to THIS pitch only).
        mgr.intentional_walk_signalled = False
        mgr.pitch_out_signalled = False
        mgr.hit_and_run_signalled = False

        # --- (1) Intentional walk: first base open + RISP + close & late -------
        if self._should_issue_ibb(state, li):
            mgr.intentional_walk_signalled = True
            # An IBB pre-empts the steal/pitch-out: the loop will issue the walk.
            mgr.green_light_rate = 0.0
            self.manager_decisions.append(
                {
                    "kind": "intentional_walk",
                    "inning": int(state.inning),
                    "leverage": li,
                    "batter_id": state.batter_id,
                }
            )
            return

        # --- (3) Steal green-light: tendency scaled by leverage ----------------
        steal_rate = self._tendency("steal_order_rate_per_1b_opp", 0.0)
        # Scale the green-light by leverage: aggression rises in close & late
        # spots, falls when way ahead/behind (running into outs is costly there).
        li_scale = 0.5 + 0.5 * min(li / _HIGH_LEVERAGE, 2.0)
        green = float(max(0.0, min(steal_rate * li_scale, 1.0)))
        mgr.green_light_rate = green

        # --- (4) Hit-and-run (SIM-349): runner-go + contact-oriented PA --------
        # Evaluated before the pitch-out / steal-initiate so a hit-and-run (a
        # *contact* play) and a straight steal are not both staged on the same
        # pitch.  When the hit-and-run fires it pre-empts the steal-initiate (the
        # runner goes WITH the swing, not on a pure steal), keeping the situational
        # set coherent (no double-fire of two runner-go decisions).
        self._maybe_hit_and_run(state, li)
        if mgr.hit_and_run_signalled:
            return

        # --- (2) Pitch-out: anticipate a steal threat in a high-leverage spot --
        b = state.bases
        steal_threat = (b.first is not None and b.second is None) or (
            b.second is not None and b.third is None
        )
        if steal_threat and li >= _HIGH_LEVERAGE and green > 0.0:
            # Pitch out a fraction of the time the green-light would be on against
            # this manager (a defensive mirror of the running tendency).
            if self._manager_rng() < min(0.5 * green, 0.5):
                mgr.pitch_out_signalled = True
                self.manager_decisions.append(
                    {
                        "kind": "pitch_out",
                        "inning": int(state.inning),
                        "leverage": li,
                    }
                )

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

    def _maybe_hit_and_run(self, state: GameState, li: float) -> None:
        """Signal a hit-and-run for THIS pitch (SIM-349 §3 / aggression).

        The canonical hit-and-run spot: a **runner on 1B** (the lead runner
        breaks with the pitch), **fewer than 2 outs**, and a **favorable count**
        (the batter is not behind in the count, so a contact-oriented swing is
        sensible — we use a non-two-strike, non-3-ball count where putting the
        ball in play protects the runner who is going).  Gated by the manager's
        ``hit_and_run_rate_per_opportunity`` tendency (manager-similarity
        AGGRESSION_FEATURES idx 1 — the directly-mapped H&R tendency) scaled by
        leverage, vs a loop-rng draw.

        When it fires, sets ``ManagerContext.hit_and_run_signalled`` for this
        pitch: the runner on 1B breaks (a forced runner-go) and the batter's PA
        is biased toward contact (so the contact path in step-4 / the in-play
        resolution sees the runner already advancing).  Records a decision.  This
        is a *bias/flag*, not a forced outcome — the count machine and resolution
        still play out; it never mutates the base-out state here, so it cannot
        create an illegal state.

        No-op-safe: with no manager profile the tendency reads 0.0 and nothing
        fires.
        """
        b = state.bases
        # Runner on 1B is the defining precondition (the lead runner goes).
        if b.first is None:
            return
        # Fewer than 2 outs (running into the 3rd out on contact is the bad case).
        if int(state.outs) >= 2:
            return
        # Favorable count: not behind (a hit-and-run on a 2-strike or 3-ball count
        # is too risky / pointless).  The hook fires pre-pitch at the START of a
        # PA / between pitches; a fresh count (0-0) or a hitter's-but-not-deep
        # count qualifies.
        if int(state.strikes) >= 2 or int(state.balls) >= 3:
            return
        tend = self._tendency("hit_and_run_rate_per_opportunity", 0.0)
        if tend <= 0.0:
            return
        # Leverage scales the call: managers run more aggressively close & late,
        # less when the margin is lopsided (a contact play into outs is costly).
        li_scale = 0.5 + 0.5 * min(li / _HIGH_LEVERAGE, 2.0)
        fire_p = min(1.0, tend * li_scale)
        if self._manager_rng() >= fire_p:
            return
        state.manager.hit_and_run_signalled = True
        self.manager_decisions.append(
            {
                "kind": "hit_and_run",
                "inning": int(state.inning),
                "leverage": li,
                "batter_id": state.batter_id,
                "runner_id": b.first,
            }
        )

    def stage_steal(
        self,
        *,
        runner_id: int | None = None,
        from_base: int = 1,
        to_base: int | None = None,
        safe: bool,
    ) -> None:
        """Stage a steal attempt for the NEXT pitch (the §3 decision).

        ``safe`` is the drawn row's own outcome (:meth:`_steal_opportunity_draw`
        passes it) or a test's explicit choice.  The attempt is consumed +
        resolved in step 7 of the next :meth:`step_pitch`.
        """
        if to_base is None:
            to_base = _NEXT_BASE.get(from_base, 4)
        self._pending_steal = StealResolution(
            attempted=True,
            runner_id=runner_id,
            from_base=from_base,
            to_base=to_base,
            safe=bool(safe),
        )

    #: SIM-474: the league-mean ``steal_order_rate_per_1b_opp`` — the SIM-434
    #: default manager profile's value. The live manager's leverage-scaled
    #: tendency is divided by this to form the aggression WEIGHT on the pool
    #: draw's attempted rows (1.0 = league-average running game).
    _LEAGUE_STEAL_ORDER_RATE: float = 0.08

    def _steal_opportunity_draw(self, state: GameState) -> None:
        """SIM-474: stage a steal by drawing ONE row from the SIM-468 steal
        OPPORTUNITY pool — a similarity-weighted draw, never a formula.

        Fires every pitch with a stealable lead runner: on 1B with 2B open
        (target 2), or on 2B with 3B open (target 3, including 1B+2B — the
        lead runner drives). The pool is per-pitch and its cell is the exact
        (outs, balls, strikes), so per-pitch and per-count attempt volume is
        the pool's own — no tuned constant. The drawn row's ``attempted`` flag
        decides whether the runner goes; its ``success`` flag decides safe or
        caught; :meth:`_resolve_steal_outcome` commits it (step 7). Manager
        aggression enters as a WEIGHT on attempted rows (never a gate); with
        no manager wired the weight is neutral. No-op when the sampler or the
        pool is absent.
        """
        fp = self.full_pool_sampler
        if fp is None or self._pending_steal is not None:
            return
        if not fp.has_steal_pool():
            return
        b = state.bases
        if b.first is not None and b.second is None:
            runner_id, from_base, target = b.first, 1, 2
        elif b.second is not None and b.third is None:
            runner_id, from_base, target = b.second, 2, 3
        else:
            return
        if runner_id is None:
            return
        season = int(getattr(state, "season", 2024) or 2024)
        pitcher = state.pitcher_id
        catcher = state.away_catcher_id if state.offense == Team.HOME else state.home_catcher_id
        # Manager aggression: the manager's tendency over the league mean,
        # clamped so even a never-runs manager only DAMPS the draw (a weight,
        # not a gate — SIM-474). No manager -> neutral.
        #
        # SIM-476 step 0 (2026-08-30): the LEVERAGE factor
        # (0.5 + 0.5*min(LI/1.5, 2)) is DELETED. It double-counted leverage —
        # the draw's hard cell (outs, balls, strikes) and soft score kernel
        # already embody how often real runners went at that leverage — and
        # because it reads 1.0 only at LI >= 1.5, it suppressed ordinary-pitch
        # attempts ~0.73-0.87x, the measured 2B attempts-per-opportunity -15%
        # (arm A/B numbers in docs/audit/2026-08-28-sim476-fit-plan.md). With
        # the league-flat SIM-427 default profile the ratio below is exactly
        # 1.0, so production aggression is NEUTRAL until real per-manager
        # rates land — and those must be measured attempt-rate ratios, never
        # a leverage formula. If localization ever shows a late-game residual,
        # the fix is an inning/late soft KERNEL on the steal draw (data
        # conditioning), not a reinstated multiplier.
        if self.manager is None:
            aggression = 1.0
        else:
            rate = self._tendency("steal_order_rate_per_1b_opp", self._LEAGUE_STEAL_ORDER_RATE)
            aggression = float(min(max(rate / self._LEAGUE_STEAL_ORDER_RATE, 0.05), 4.0))
        drawn = fp.steal_draw(
            target,
            f"{int(runner_id)}:{season}",
            f"{int(pitcher)}:{season}" if pitcher is not None else "",
            f"{int(catcher)}:{season}" if catcher is not None else None,
            outs=int(state.outs),
            balls=int(state.balls),
            strikes=int(state.strikes),
            score_diff=int(state.score_diff),
            aggression=aggression,
        )
        if drawn is None:
            return
        if len(drawn) >= 5:
            attempted, success, po_out, po_adv, po_err = drawn[:5]
        else:
            # A duck-typed test sampler may still return the pre-SIM-507 pair.
            (attempted, success), po_out, po_adv, po_err = drawn, False, False, False
        if po_out or po_err:
            # SIM-507: the drawn row carries a pickoff outcome — it pre-empts
            # the steal question for this pitch. Staged like a steal (the
            # pre-pitch decision resolves in step 7) and resolved by
            # _resolve_pickoff: an out retires the runner (a CS only when he
            # was advancing), an errant throw advances him.
            self._pending_steal = StealResolution(
                attempted=True,
                runner_id=runner_id,
                from_base=from_base,
                to_base=_NEXT_BASE.get(from_base, 4),
                safe=bool(po_err and not po_out),
                pickoff=True,
                pickoff_advancing=bool(po_adv),
            )
            return
        if not attempted:
            return
        self.stage_steal(runner_id=runner_id, from_base=from_base, safe=bool(success))

    def _end_of_pa_hook(self, state: GameState) -> None:
        """End-of-PA / half-inning-boundary manager hook (§5.3): substitution +
        small-ball setup, evaluated ONLY at PA / half-inning boundaries (never
        mid-PA — the loop calls this from :meth:`_end_of_pa` and
        :meth:`advance_half_inning`).  SIM-323.

        Three decisions, each gated by the live Leverage Index + a manager
        tendency drawn from ``self.manager``:

          1. **Starter pull + bullpen-by-leverage** (§5.3 / §3 item 1) — once the
             current pitcher's pitch count clears a floor, an above-average
             ``starter_pull_pct_before_100`` manager pulls in a high-leverage
             spot; the pitch count ceiling forces a pull regardless.  The
             replacement is chosen BY LEVERAGE from the defending team's
             ``ManagerContext.bullpen_available``: the **closer** (the first /
             highest-leverage arm) enters a high-LI late spot, a middle reliever
             otherwise.  Mutates ``state.pitcher_id`` + resets the new arm's pitch
             count; degrades to a no-op when no bullpen is wired.
          2. **Pinch-hit** (§5.3 platoon) — in a high-leverage spot, an
             above-average ``pinch_hit_rate_high_leverage`` manager swaps the
             batter now due up for a bench player from ``self.bench`` (one-time
             use per bench player); degrades to a no-op with no bench.
          3. **Sac-bunt setup** (§3 / aggression) — with a runner on and fewer
             than two outs, the manager may signal a sacrifice bunt for the next
             PA, gated by ``sac_bunt_rate_high_leverage`` (in a high-LI spot) or
             ``sac_bunt_rate_low_leverage`` (small-ball otherwise).  Recorded as a
             decision (the bunt's batted-ball resolution is SIM-319's); never
             produces an illegal state.

        No-op-safe: with ``self.manager is None`` all tendencies read 0.0 and the
        bullpen / bench are empty, so nothing fires (the SIM-320/324/326 no-DB
        full-game tests stay green).
        """
        if self.manager is None:
            return None
        # Only evaluate at a real boundary with a half-inning still to play.
        li = self.compute_leverage(state)
        self._maybe_pull_starter(state, li)
        self._maybe_pinch_hit(state, li)
        self._maybe_sac_bunt(state, li)
        # (SIM-349's _maybe_sac_fly_intent sat here until 2026-08-19 —
        # retired by SIM-513; the SIM-512 tag draw owns sacrifice flies.)
        return None

    def _maybe_pull_starter(self, state: GameState, li: float) -> None:
        """Pull the current pitcher for a leverage-appropriate reliever (SIM-323).

        Considers a pull once the pitch count clears :data:`_PULL_PITCH_FLOOR`;
        fires when the manager's ``starter_pull_pct_before_100`` tendency (scaled
        by leverage) wins a loop-rng draw, OR unconditionally at
        :data:`_PULL_PITCH_CEILING`.  Picks the reliever BY LEVERAGE from the
        defending team's bullpen.  No-op when no bullpen is available.
        """
        pc = int(state.pitcher_pitch_count)
        if pc < _PULL_PITCH_FLOOR:
            return
        pull_tend = self._tendency("starter_pull_pct_before_100", 0.0)
        # Leverage scales the pull aggression; a high-LI jam hastens the hook.
        fire_p = min(1.0, pull_tend * (0.5 + 0.5 * min(li / _HIGH_LEVERAGE, 2.0)))
        # SIM-434: a fatigued / deep-into-the-order starter hastens the hook.  The
        # fatigue index (pitch count + times-through-the-order + optional rest) is
        # a >= 0 BOOST applied multiplicatively, so it can only RAISE fire_p (a
        # fresh, 1st-time-through starter has fatigue ~ pc/budget and TTO term 0 ->
        # boost ~1.0).  fire_p stays clamped to [0, 1], so a pull that fired before
        # still fires (the SIM-323 gate is monotone under this term).
        tto = times_through_order(state.pitcher_bf.get(state.pitcher_id, 0))
        fatigue = pitcher_fatigue(
            pc, tto=tto, rest_days=state.pitcher_rest_days.get(state.pitcher_id)
        )
        fire_p = min(1.0, fire_p * (1.0 + fatigue))
        forced = pc >= _PULL_PITCH_CEILING
        if not forced and self._manager_rng() >= fire_p:
            return
        new_arm = self._pick_reliever(state, li)
        if new_arm is None:
            return  # bullpen empty -> degrade gracefully (stay with the starter).
        old = state.pitcher_id
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
            }
        )

    def _pick_reliever(self, state: GameState, li: float) -> int | None:
        """Choose a reliever from the defending team's bullpen (SIM-323/SIM-434).

        Two selection modes, both popping the chosen arm so it cannot be reused
        and reading ``ManagerContext.bullpen_available`` keyed by the defending
        Team (or its int value):

          * **SIM-434 scored mode** — when per-arm metadata is wired (a hand in
            ``state.throw_hands`` and/or a rest in ``state.pitcher_rest_days`` for
            ANY candidate), rank the arms by :func:`score_reliever`
            (leverage × platoon × fresh-arm effectiveness × rest) and pop the
            best fit for THIS spot.  Ties break by list position (the closer / top
            arm first), so a no-metadata bullpen behaves exactly like the legacy
            positional mode below.
          * **legacy positional mode (SIM-323)** — High-LI + late -> the
            **closer** (the first arm in the list); otherwise a middle reliever
            from the back of the list.

        Returns ``None`` when no arm is available.
        """
        pen_map = state.manager.bullpen_available or {}
        team = state.defense
        arms = pen_map.get(team)
        if arms is None:
            arms = pen_map.get(int(team))
        if not arms:
            return None
        # SIM-434 scored mode: only when at least one arm carries hand/rest meta,
        # so a metadata-less bullpen (the SIM-323 tests) keeps the positional path
        # byte-identical.
        has_meta = any((a in state.throw_hands) or (a in state.pitcher_rest_days) for a in arms)
        if has_meta:
            bat_hand = state.bat_hand_for(state.batter_id)
            best_i = 0
            best_score = float("-inf")
            for i, arm in enumerate(arms):
                throw = state.throw_hands.get(arm)
                score = score_reliever(
                    leverage=li,
                    platoon=platoon_factor(bat_hand, throw),
                    # A fresh reliever enters on his 1st time through -> top
                    # effectiveness; a tired bulk arm could carry a TTO via
                    # ``pitcher_bf`` if it has already pitched this game.
                    effectiveness=tto_effectiveness(
                        times_through_order(state.pitcher_bf.get(arm, 0))
                    ),
                    rest_days=state.pitcher_rest_days.get(arm),
                )
                # Strictly-greater keeps the first (top / closer) arm on a tie.
                if score > best_score:
                    best_score = score
                    best_i = i
            return int(arms.pop(best_i))
        high_lev_late = li >= _HIGH_LEVERAGE and int(state.inning) >= _LATE_INNING
        if high_lev_late:
            return int(arms.pop(0))  # closer / highest-leverage arm.
        return int(arms.pop())  # middle reliever from the back.

    def _maybe_pinch_hit(self, state: GameState, li: float) -> None:
        """Pinch-hit a bench player for the batter now due up (SIM-323).

        Fires only in a high-leverage spot, gated by the manager's
        ``pinch_hit_rate_high_leverage`` tendency vs a loop-rng draw.  Swaps the
        OFFENSE's current lineup slot (the batter about to hit) for a bench player
        popped from ``self.bench`` for that team, keeping the lineup valid.  No-op
        when no bench / no lineup is wired.
        """
        if li < _HIGH_LEVERAGE:
            return
        ph_tend = self._tendency("pinch_hit_rate_high_leverage", 0.0)
        if ph_tend <= 0.0:
            return
        if self._manager_rng() >= min(1.0, ph_tend):
            return
        team = state.offense
        bench = self.bench.get(team)
        if bench is None:
            bench = self.bench.get(int(team))
        if not bench:
            return  # no bench -> degrade gracefully.
        if team == Team.HOME:
            lineup, slot = state.home_lineup, state.home_lineup_slot
        else:
            lineup, slot = state.away_lineup, state.away_lineup_slot
        if not lineup:
            return
        sub = int(bench.pop(0))
        out_batter = lineup[slot % len(lineup)]
        lineup[slot % len(lineup)] = sub
        state.batter_id = sub
        self.manager_decisions.append(
            {
                "kind": "pinch_hit",
                "inning": int(state.inning),
                "leverage": li,
                "out_batter_id": out_batter,
                "in_batter_id": sub,
            }
        )

    def _maybe_sac_bunt(self, state: GameState, li: float) -> None:
        """Signal a sacrifice bunt for the next PA (SIM-323 small-ball setup).

        With a runner on and fewer than two outs, the manager may call for a
        sac bunt, gated by ``sac_bunt_rate_high_leverage`` (high-LI spot) or
        ``sac_bunt_rate_low_leverage`` (otherwise).  Recorded as a decision (the
        bunt's batted-ball outcome is SIM-319's job); never mutates the base-out
        state, so it cannot create an illegal state.
        """
        b = state.bases
        if b.count_on_base == 0 or int(state.outs) >= 2:
            return
        if li >= _HIGH_LEVERAGE:
            tend = self._tendency("sac_bunt_rate_high_leverage", 0.0)
        else:
            tend = self._tendency("sac_bunt_rate_low_leverage", 0.0)
        if tend <= 0.0:
            return
        if self._manager_rng() >= min(1.0, tend):
            return
        self.manager_decisions.append(
            {
                "kind": "sac_bunt",
                "inning": int(state.inning),
                "leverage": li,
                "batter_id": state.batter_id,
            }
        )


# ---------------------------------------------------------------------------
# Full-game driver (SIM-320 owns the real implementation)
# ---------------------------------------------------------------------------


#: Regulation length (spec §6.2): a game is at least this many full innings.
REGULATION_INNINGS = 9

#: Hard ceiling on innings so a pathological loop (e.g. a degenerate injected
#: outcome that never records an out) cannot spin forever.  Real games settle in
#: a handful of extra innings under the ghost-runner rule; this is purely a
#: guard, far above any plausible game length.
_MAX_INNINGS = 50


# ---------------------------------------------------------------------------
# SIM-328 — per-player sim-average accumulators (the per-game boxscore)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PlayerStatLine:
    """One player's accumulated stat line for a single simulated game (SIM-328).

    Carries BOTH a batting and a pitching slot so a single id (e.g. a two-way
    player, or just to keep the keyspace uniform) has one home; a pure batter
    leaves the pitching fields at 0 and vice-versa.

    Batting:  ``ab`` / ``h`` / ``hr`` / ``rbi``.
    Pitching: ``outs_recorded`` (the raw thirds-of-an-inning the pitcher retired)
              plus ``k`` / ``bb`` / ``er``.  Innings are represented internally as
              an integer count of OUTS (thirds) so accumulation is exact; the
              human-readable x.0 / x.1 / x.2 form and the decimal thirds are
              derived on demand (:attr:`ip` / :attr:`ip_outs`).
    """

    player_id: int

    # --- batting ---
    ab: int = 0
    h: int = 0
    hr: int = 0
    rbi: int = 0
    # SIM-365 additive batting extras (all default 0 so existing constructions
    # and SIM-328 tests are unaffected):
    b2: int = 0  # doubles (a subset of ``h``)
    b3: int = 0  # triples (a subset of ``h``)
    r: int = 0  # runs scored by this player (offense)
    sb: int = 0  # stolen bases
    cs: int = 0  # caught stealing (SIM-426)

    # --- pitching (IP stored as outs == thirds of an inning) ---
    outs_recorded: int = 0
    k: int = 0
    bb: int = 0
    er: int = 0
    # SIM-365 additive pitching extras (default 0):
    h_allowed: int = 0  # hits allowed
    r_allowed: int = 0  # runs allowed (earned + unearned; >= ``er``)

    @property
    def ip_outs(self) -> int:
        """Innings pitched as a raw count of outs (thirds of an inning)."""
        return int(self.outs_recorded)

    @property
    def ip(self) -> float:
        """Innings pitched in the standard ``x.1``/``x.2`` baseball notation,
        as a float: full innings + (1/3-out remainder rendered as .1 / .2).

        ``7`` outs -> ``2.1`` (2 innings and 1 out); ``8`` -> ``2.2``; ``9`` ->
        ``3.0``.  This is the canonical box-score IP string read as a number, NOT
        a true decimal — use :attr:`ip_thirds` for arithmetic.
        """
        full, rem = divmod(int(self.outs_recorded), 3)
        return float(full) + rem / 10.0

    @property
    def ip_thirds(self) -> float:
        """Innings pitched as a TRUE decimal (outs / 3) — for rate stats / ERA."""
        return self.outs_recorded / 3.0


@dataclass(slots=True)
class BoxScore:
    """Per-game accumulator of :class:`PlayerStatLine`s, keyed by ``player_id``.

    Populated INSIDE the PA loop (:meth:`StateMachine._accumulate_pa`) on every
    terminal plate appearance: the batting team's current batter is credited on
    offense, the fielding team's current pitcher is charged on defense.  A
    downstream Monte-Carlo aggregator (SIM-329 props) sums these across N games.

    Two views over the SAME line store so the keyspace stays simple:
      * :attr:`batters` / :attr:`pitchers` are convenience filters;
      * :meth:`line` returns (creating if needed) the single line for an id.
    """

    lines: dict[int, PlayerStatLine] = field(default_factory=dict)

    def line(self, player_id: int) -> PlayerStatLine:
        """Return the stat line for ``player_id``, creating an empty one if absent."""
        line = self.lines.get(player_id)
        if line is None:
            line = PlayerStatLine(player_id=int(player_id))
            self.lines[player_id] = line
        return line

    @property
    def batters(self) -> dict[int, PlayerStatLine]:
        """Lines with any batting activity (an AB or a PA that reached base)."""
        return {pid: ln for pid, ln in self.lines.items() if ln.ab or ln.h or ln.rbi}

    @property
    def pitchers(self) -> dict[int, PlayerStatLine]:
        """Lines with any pitching activity (outs recorded / K / BB / ER)."""
        return {
            pid: ln for pid, ln in self.lines.items() if ln.outs_recorded or ln.k or ln.bb or ln.er
        }


@dataclass(slots=True)
class GameSimResult:
    """The per-game result :func:`simulate_game` returns (SIM-320).

    Minimal-but-complete (the richer multi-iteration aggregation contract is
    SIM-327): the final score, how many innings were played, the terminal
    :class:`~simulation.game_state.GameState`, the flags a caller needs to tell a
    regulation finish from a walk-off / extra-innings finish, and the per-game
    ``seed`` so a result is self-describing for the deterministic-replay contract
    (spec §6.3).
    """

    home_score: int
    away_score: int
    innings_played: int
    final_state: GameState
    walk_off: bool = False
    extra_innings: bool = False
    seed: int | None = None
    #: Total pitches thrown across the game (a cheap sanity / perf signal).
    total_pitches: int = 0
    #: SIM-328 per-game boxscore: per-player AB/H/HR/RBI + IP/K/BB/ER, keyed by
    #: player_id.  ADDITIVE/optional so SIM-320/327 returns + tests are
    #: unaffected; ``None`` when no boxscore was accumulated (the loop attaches a
    #: populated one via :func:`simulate_game`).
    boxscore: BoxScore | None = None

    @property
    def winner(self) -> Team | None:
        """The winning :class:`~simulation.game_state.Team` (None on a tie — only
        possible at the ``_MAX_INNINGS`` guard, never in a normal finish)."""
        if self.home_score > self.away_score:
            return Team.HOME
        if self.away_score > self.home_score:
            return Team.AWAY
        return None


def _place_ghost_runner(state: GameState) -> None:
    """Seed the extra-innings automatic (ghost) runner on second base (§6.2).

    The automatic runner is the player who made the last out of the prior inning
    (the configured MLB rule); the loop approximates that with the batting team's
    *previous* lineup slot — the batter who would have been "due up minus one" —
    so a real player id occupies 2B when a lineup is wired, and the bag is simply
    marked occupied (a synthetic id) when it is not (the no-lineup test path).
    Only ever called on the first pitch of an extra half-inning when the bases
    are empty.
    """
    if state.bases.runners_state != 0:
        return  # already seeded / runners present — never double-place.
    lineup = state.home_lineup if state.offense == Team.HOME else state.away_lineup
    slot = state.home_lineup_slot if state.offense == Team.HOME else state.away_lineup_slot
    ghost_id: int | None = None
    if lineup:
        ghost_id = lineup[(slot - 1) % len(lineup)]
    # A non-None id so the bag reads as occupied even without a lineup; a
    # negative-id guard in Bases.assert_consistent rejects bad ids, so use a
    # large positive synthetic id for the no-lineup case.
    state.bases.second = ghost_id if ghost_id is not None else 999_000_000
    state.bases.assert_consistent()


def _game_over_after_half(state: GameState) -> bool:
    """The spec §6.2 game-over predicate, evaluated when a half-inning has just
    *completed* (the half / inning pointer rolled past it).

    ``state`` is the state AFTER the roll, so its ``inning`` / ``half`` describe
    the half-inning about to start.  Because the roll already advanced the
    pointer, we reason about the half that just finished:

      * Just finished the **top** of an inning  -> ``state.half == BOTTOM`` (same
        inning).  The game ends only if ``inning >= 9`` AND the **home team
        already leads** (the bottom is not played, spec §6.2).
      * Just finished the **bottom** of an inning -> ``state.half == TOP`` and
        ``inning`` was incremented.  The completed inning is ``inning - 1``.  The
        game ends if that inning ``>= 9`` AND the score is **not tied** (a tie
        after a completed bottom -> extra innings).
    """
    if state.half == Half.BOTTOM:
        # Top of `state.inning` just finished; bottom is pending.
        completed_inning = state.inning
        if completed_inning >= REGULATION_INNINGS and state.home_score > state.away_score:
            return True  # home leads after the top of the 9th+ -> no bottom.
        return False
    # state.half == TOP: the bottom of (state.inning - 1) just finished.
    completed_inning = state.inning - 1
    return bool(completed_inning >= REGULATION_INNINGS and state.home_score != state.away_score)


def _is_walkoff_live(state: GameState) -> bool:
    """True when a run that just scored is a walk-off (spec §6.2): the **bottom**
    of the 9th (or any later inning) with the **home team leading**.  Evaluated
    after every pitch in a walk-off-eligible half so the game ends on the run that
    takes the lead, mid-inning, with no further batters."""
    return (
        state.half == Half.BOTTOM
        and state.inning >= REGULATION_INNINGS
        and state.home_score > state.away_score
    )


def simulate_game(
    state_machine: StateMachine | None = None,
    *,
    initial_state: GameState | None = None,
    seed: int | None = None,
    pitcher_id: int = 0,
    bat_hand: str = "R",
    season: int = 2024,
    away_lineup: list[int] | None = None,
    home_lineup: list[int] | None = None,
    bat_hands: dict[int, str] | None = None,
    throw_hands: dict[int, str] | None = None,
    home_pitcher_id: int | None = None,
    away_pitcher_id: int | None = None,
    home_catcher_id: int | None = None,
    away_catcher_id: int | None = None,
    home_defense: dict[str, int] | None = None,
    away_defense: dict[str, int] | None = None,
    park_run_factor: float = 1.0,
    manager=None,
    bench=None,
    bullpen=None,
    pitcher_rest_days: dict[int, float] | None = None,
    max_innings: int = _MAX_INNINGS,
) -> GameSimResult:
    """Drive the SIM-316 :class:`StateMachine` to a completed game (SIM-320).

    This is the step-8 loop-control entry point the spec §6.2/§6.3 defines.  It
    does NOT reimplement the 8 per-pitch steps — it orchestrates them: it calls
    :meth:`StateMachine.step_pitch` repeatedly, letting the machine own the count
    machine / fielding / baserunning / half-inning roll, and layers the
    *game-level* control on top:

      * **Regulation (§6.2):** play through 9 full innings; the bottom of the 9th
        is skipped when the home team already leads after the top of the 9th.
      * **Walk-off (§6.2):** end immediately, mid-inning, on the run that gives
        the home team the lead in the bottom of the 9th or any later inning.
      * **Extra innings (§6.2):** when tied after 9, play additional full innings,
        each half seeded with the **ghost runner on 2B** before its first pitch.
      * **Determinism (§6.3):** the per-game ``seed`` is threaded through BOTH the
        machine's loop rng (steal / advancement / manager decisions) AND the
        full-pool sampler's rng (the pitch + batted-ball draws), so a fixed
        ``(game, seed)`` reproduces an identical game.

    Wiring: pass a fully-built ``state_machine`` (the production factory's, or a
    test's over a :mod:`simulation.synthetic_bundle` bundle), or let the driver
    build a bare count-machine-only machine.  Likewise pass an ``initial_state``
    or let the driver build a fresh "top of the 1st" GameState from
    ``pitcher_id`` / ``bat_hand`` / ``season`` / the two lineups.

    SIM-434 manager passthrough (GATED by ``SIM_MANAGER``): ``manager`` /
    ``bench`` / ``bullpen`` / ``pitcher_rest_days`` are wired ONLY when supplied
    (the production factory passes them only when ``SIM_MANAGER`` is on).  With
    none supplied this is a total no-op: the machine keeps ``manager is None`` so
    every §3/§5.3 hook early-returns and the simulated game is byte-identical to
    the manager-less default.  When supplied, ``manager`` (+ optional ``bench``)
    are attached to the machine (only if it does not already carry one — a
    pre-built machine wins), ``bullpen`` (a ``{Team|int: [pitcher_id, ...]}`` map)
    seeds ``initial_state.manager.bullpen_available`` so the pull hook has arms,
    and ``pitcher_rest_days`` (a ``{pitcher_id: days}`` map, SIM-433 follow-on)
    feeds the fatigue / reliever-scoring model.

    Returns a :class:`GameSimResult`.  A machine with no sampler must be a
    subclass that supplies each pitch outcome itself (the count-machine-only
    drivers); otherwise ``step_pitch`` raises.
    """
    # --- SIM-498: two INDEPENDENT random streams from the one per-game seed ---
    # Stream 0 drives the loop rng (advancement, steal outcomes, manager
    # decisions); stream 1 drives the full-pool rng (the pitch draw and the
    # batted-ball draw). Both used to be ``np.random.default_rng(seed)`` from the
    # SAME integer, so they emitted identical sequences. Spawning keeps the per-game
    # seed reproducible while making the two streams independent.
    _streams = spawn_rng_streams(seed)
    loop_rng = _streams[LOOP_STREAM]
    full_pool_rng = _streams[FULL_POOL_STREAM]

    # --- Build the machine (thread the seed through its loop rng) -------------
    if state_machine is None:
        state_machine = StateMachine(rng=loop_rng)
    elif seed is not None:
        # Re-seed the supplied machine's loop rng so the per-game seed governs the
        # loop-level draws (steal / foul re-weight / outcome choice).
        state_machine.rng = loop_rng

    # --- SIM-434: manager / bench passthrough (GATED by the caller) ----------
    # Attach a supplied manager (+ bench) to the machine ONLY when one is given
    # AND the machine does not already carry one (a pre-built machine wins — the
    # production factory wires the manager there when SIM_MANAGER is on).  With no
    # manager supplied anywhere, ``state_machine.manager`` stays None and every
    # SIM-323 hook is a no-op -> byte-identical to the manager-less game.
    if manager is not None and getattr(state_machine, "manager", None) is None:
        state_machine.manager = manager
        if bench:
            state_machine.bench = dict(bench)

    # --- Thread the seed through the full-pool sampler's rng (§6.3) ----------
    # SIM-402: the full-pool sampler is CACHED per worker process
    # (`production_factory._build_full_pool_sampler` reuses one instance
    # across seeds for the SLA win); the per-game rng must be re-seeded here
    # so the cached sampler still produces reproducible per-seed draws.
    # SIM-498: it gets stream 1, independent of the loop rng's stream 0.
    if seed is not None:
        fp = getattr(state_machine, "full_pool_sampler", None)
        if fp is not None and hasattr(fp, "rng"):
            fp.rng = full_pool_rng

    # --- Build the initial GameState (fresh top of the 1st) ------------------
    if initial_state is None:
        initial_state = GameState(
            pitcher_id=pitcher_id,
            bat_hand=bat_hand,
            season=season,
            away_lineup=list(away_lineup or []),
            home_lineup=list(home_lineup or []),
            seed=seed,
        )
        # Point the batter at the leadoff slot when a lineup is wired.
        if initial_state.away_lineup:
            initial_state.batter_id = initial_state.away_lineup[0]
        # SIM-421: carry the per-batter hand map + both starters (picklable, so
        # they survive the BatchRunner's sim_kwargs path) so the matchup
        # pre-filter follows the lineup instead of freezing at the leadoff hand.
        if bat_hands:
            initial_state.bat_hands = dict(bat_hands)
        if throw_hands:
            initial_state.throw_hands = dict(throw_hands)
        initial_state.home_pitcher_id = home_pitcher_id
        initial_state.away_pitcher_id = away_pitcher_id
        initial_state.home_catcher_id = home_catcher_id
        initial_state.away_catcher_id = away_catcher_id
        # SIM-425b/411: per-team defense maps (position 1-9 -> player_id) for the
        # fielder-RBF nudge + the venue run factor for the park nudge. All picklable
        # so they survive the BatchRunner sim_kwargs path; empty/1.0 -> the
        # consumers stay neutral (the flags also gate them off by default).
        if home_defense:
            initial_state.home_defense = dict(home_defense)
        if away_defense:
            initial_state.away_defense = dict(away_defense)
        initial_state.park_run_factor = float(park_run_factor)
        initial_state.bat_hand = initial_state.bat_hand_for(initial_state.batter_id)
    state = initial_state
    if seed is not None and state.seed is None:
        state.seed = seed

    # --- SIM-434: seed the bullpen + per-pitcher rest onto the state ---------
    # Applied whether the state was built here or passed in.  ``bullpen`` is the
    # per-defending-team available-arms map the pull hook pops from; copy the
    # arm lists so popping a reliever never mutates the caller's bullpen.  When no
    # explicit bullpen is passed, fall back to one staged on the machine (the
    # production factory does this when SIM_MANAGER is on).  Both are no-ops when
    # neither is present -> no behaviour change with the flag off.
    eff_bullpen = bullpen if bullpen else getattr(state_machine, "bullpen", None)
    if eff_bullpen:
        state.manager.bullpen_available = {team: list(arms) for team, arms in eff_bullpen.items()}
    eff_rest = (
        pitcher_rest_days
        if pitcher_rest_days
        else getattr(state_machine, "pitcher_rest_days", None)
    )
    if eff_rest:
        state.pitcher_rest_days = dict(eff_rest)

    # --- The game loop -------------------------------------------------------
    total_pitches = 0
    walk_off = False
    # A pitch-count ceiling so a pathological machine that never records an out
    # (e.g. a degenerate injected outcome) cannot spin forever: a real inning is
    # ~15-40 pitches, so 200/inning is far above any plausible game yet bounds
    # the loop independently of the inning pointer (which only advances on a
    # completed half-inning).
    max_pitches = max(int(max_innings), REGULATION_INNINGS) * 200
    # The inning whose play actually decided / completed the game.  Tracked
    # explicitly because the half-inning roll advances ``state.inning`` PAST the
    # completed inning, so the post-loop pointer over-counts by one after a
    # completed regulation bottom half.
    last_played_inning = state.inning
    # Track whether the current half-inning has had its first pitch yet so the
    # ghost runner is seeded exactly once, at the start of each extra half.
    half_inning_open = False
    cur_inning, cur_half = state.inning, state.half

    while True:
        # Detect a brand-new half-inning (the pointer changed, or the very first
        # pitch of the game): re-arm the per-half ghost-runner seeding.
        if (state.inning, state.half) != (cur_inning, cur_half) or not half_inning_open:
            cur_inning, cur_half = state.inning, state.half
            half_inning_open = True
            # §6.2 ghost runner: every EXTRA half-inning (inning > regulation)
            # starts with the automatic runner on 2B.
            if state.inning > REGULATION_INNINGS:
                _place_ghost_runner(state)

        # --- one pitch (the machine owns steps 1-8) ----------------------
        prev_inning, prev_half = state.inning, state.half
        last_played_inning = prev_inning
        state_machine.step_pitch(state)
        total_pitches += 1

        # The committed state is always invariant-valid (guards held in step 8).
        state.assert_invariants(in_play=True)

        rolled = (state.inning, state.half) != (prev_inning, prev_half)
        if rolled:
            # A half-inning just completed (3 outs).  Re-arm for the next half.
            half_inning_open = False
            if _game_over_after_half(state):
                break
            continue

        # Same half-inning still in progress: check for a walk-off (the home team
        # took the lead in the bottom of the 9th+ -> end mid-inning, no further
        # batters, the half-inning does NOT complete, spec §6.2).
        if _is_walkoff_live(state):
            walk_off = True
            break

        # Safety guard: never spin past the inning OR pitch ceiling (the latter
        # bounds a pathological never-an-out machine that never rolls an inning).
        if state.inning > max_innings or total_pitches >= max_pitches:
            break

    # ``last_played_inning`` is the inning the deciding play happened in, so it is
    # the true count of innings played (a completed regulation bottom-9th leaves
    # the pointer at the top of the 10th, but only 9 innings were actually
    # played).
    innings_played = last_played_inning
    extra_innings = last_played_inning > REGULATION_INNINGS

    return GameSimResult(
        home_score=state.home_score,
        away_score=state.away_score,
        innings_played=innings_played,
        final_state=state,
        walk_off=walk_off,
        extra_innings=extra_innings,
        seed=seed,
        total_pitches=total_pitches,
        # SIM-328: the per-game boxscore the machine accumulated across the loop
        # (per-player AB/H/HR/RBI + IP/K/BB/ER).  Additive/optional — None if no
        # terminal PA ever resolved a scored outcome (the machine never created
        # one).  Downstream SIM-329 props aggregates this across iterations.
        boxscore=getattr(state_machine, "boxscore", None),
    )


__all__ = [
    "CONTACT_PITCH_OUTCOME",
    # SIM-316 count machine + state machine
    "CountAdvance",
    "advance_count",
    "StateMachine",
    "simulate_game",
    # SIM-320 full-game driver + per-game result
    "GameSimResult",
    "REGULATION_INNINGS",
    # SIM-328 per-player sim-average accumulators (the per-game boxscore)
    "PlayerStatLine",
    "BoxScore",
    # SIM-319 fielding / baserunning / steal resolution (steps 6/7 + §5.4)
    "FieldingSignal",
    "StealResolution",
    "STEAL_SAFE",
    "STEAL_CAUGHT",
    # PA-event markers
    "EVENT_WALK",
    "EVENT_STRIKEOUT",
    "EVENT_HIT_BY_PITCH",
    "EVENT_IN_PROGRESS",
]
