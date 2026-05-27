"""
sim_loop.py
===========
SIM-316 -- GameState-driven plate-appearance STATE MACHINE skeleton.

WHAT THIS IS (and is NOT) -- the SIM-316 deliverable
----------------------------------------------------
SIM-303 left a *single-pitch scaffold* here: ``simulate_pitch`` sampled exactly
one pitch (and, on contact, one batted ball) through the SIM-302
``PlayPoolSampler`` and stopped -- no count machine, no terminal-PA logic, no
half-inning control.  SIM-316 turns that scaffold into the **count / out / base /
inning state machine** the SIM-310 spec
(``docs/architecture/2026-06-17-phase4-sim-loop-spec.md`` §5.1 / §6.1) requires:

  * the **count machine** (§5.1): ball 4 -> walk (terminal), strike 3 ->
    strikeout (terminal), and the SIM-056 **two-strike-foul absorbing rule** (a
    foul with two strikes keeps the PA alive -- the count does NOT advance past
    strike 2 on a foul);
  * the **half-inning logic** (§6.1): 3 outs -> clear bases, reset count + outs,
    flip ``Half`` (TOP<->BOTTOM), advance the inning on BOTTOM->TOP, and carry
    the per-team batting-order pointer forward;
  * **invalid-state guards** via the SIM-311 ``GameState.assert_*`` helpers so
    outs stay 0-2 during play, balls 0-3 / strikes 0-2 mid-count, scores
    non-negative, and base occupancy stays consistent;
  * a state machine that **reads/commits the ``GameState`` and emits a
    ``PlayResult``**, with the per-pitch sampler call still wired (the query
    "fingerprint" is still the SIM-303 stub -- ``# TODO(SIM-317)``).

WHAT THIS IS STILL NOT (downstream tickets own these; explicit hooks below)
---------------------------------------------------------------------------
  * **SIM-317** -- real fingerprint derivation from game state (replacing the
    stub hash) is now wired in via an optional
    :class:`simulation.fingerprints.FingerprintDeriver` injected into
    :class:`PlateAppearanceSimulator`; see
    :meth:`PlateAppearanceSimulator._pitch_fingerprint`.  When no deriver is
    supplied (the SIM-316 count-machine-only / no-DB test mode) the legacy stub
    hash is used so that path stays DB/FAISS-free.
  * **SIM-318** -- DONE: the step-4 outcome-determination *detail*: the SIM-056
    count-conditional foul **re-weight** applied in the loop BEFORE the count
    advance (the sampler stays count-blind), plus the full terminal
    classification.  See :func:`apply_count_foul_weighting` /
    :func:`strikes_bucket_foul_factor` and
    :meth:`StateMachine._draw_reweighted_outcome` /
    :meth:`StateMachine._accept_or_resample_foul` wired into
    :meth:`StateMachine.step_pitch` step 4.
  * **SIM-319** -- fielding + baserunning resolution of a batted ball into outs
    and base/score deltas, the steal decision/outcome, and the dropped-third-
    strike edge; see :meth:`StateMachine._resolve_in_play` and the out/baserunner
    hooks in :meth:`StateMachine.step_pitch`.
  * **SIM-320** -- the full-game ``simulate_game()`` driver + walk-off / extra-
    innings control; see :func:`simulate_game` (a guarded stub) and
    :meth:`StateMachine.advance_half_inning`.
  * **SIM-321** -- cross-engine score fusion (referenced by SIM-317).
  * **SIM-323** -- the manager substitution/IBB/steal module behind the §3/§5.3
    hooks; see :meth:`StateMachine._pre_pitch_hook` / :meth:`StateMachine._end_of_pa_hook`.

The legacy SIM-303 surface (``PitchState``, ``pitch_outcome_to_event``,
``PlateAppearanceSimulator.simulate_pitch`` returning the scaffold dict) is kept
verbatim so the SIM-303 tests keep passing; the new machine is additive.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from simulation.constants import RUN_VALUES, resolve_event_to_canonical
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
from simulation.play_pool_sampler import PlayPoolSampler
from simulation.run_resolution import resolve_runs

# Dimensions of the two FAISS fingerprints (must match the tiles the sampler
# reads: 10-dim pitch fingerprint from SIM-041, 3-dim batted-ball from SIM-042;
# see docs/architecture/2026-05-20-play-pool.md §2).
_PITCH_FINGERPRINT_DIM = 10
_BATTEDBALL_FINGERPRINT_DIM = 3

# SIM-421 (L2): when the real FingerprintDeriver is wired the query is the
# matchup CENTROID; sampling its k-NN alone over-draws the dense centre
# (well-located called strikes) and under-draws off-centre pitches (balls), so a
# game collapses to ~all strikeouts.  Per pitch we add zero-mean Gaussian jitter
# in the normalized tile space (scaled per-dim by the engine FEATURE_SCALE) so
# the draw SCATTERS like a real pitch sequence and reproduces the pitcher's
# marginal outcome mix.  The stub-fingerprint path is already stochastic and is
# left untouched.  Sigma ~ the within-tile normalized spread.
try:  # numpy-only engine constants (faiss is guarded inside the engine modules)
    from similarity.engines.batted_ball_similarity import FEATURE_SCALE as _BB_FEATURE_SCALE
    from similarity.engines.pitch_pitch_similarity import FEATURE_SCALE as _PITCH_FEATURE_SCALE
except Exception:  # pragma: no cover - defensive; jitter simply disables
    _PITCH_FEATURE_SCALE = None
    _BB_FEATURE_SCALE = None
_QUERY_JITTER_SIGMA = float(os.environ.get("SIM_QUERY_JITTER_SIGMA", "1.25"))

#: The single pitch outcome that means the ball was put in play -> resolve a
#: batted ball.  Mirrors the ``outcome_type`` vocabulary the SIM-301 builder
#: writes ("ball", "called_strike", "swinging_strike", "foul", "in_play") and
#: is re-exported from the SIM-311 GameState contract for one source of truth.
CONTACT_PITCH_OUTCOME = _GS_CONTACT_PITCH_OUTCOME

#: PA-event strings the §5.1 count machine produces for non-contact terminals.
EVENT_WALK = "walk"
EVENT_STRIKEOUT = "strikeout"

#: Marker the count machine emits while a PA is still live (no terminal yet).
#: Distinct from a real PA event so callers / run-resolution treat it as 0 runs.
EVENT_IN_PROGRESS = "in_progress"

#: The single pitch outcome SIM-056 re-weights (the count-conditional one).
_FOUL_OUTCOME = "foul"

#: SIM-056 count-conditional foul multiplier, keyed by ``count_strikes`` bucket
#: (0 / 1 / 2 strikes).  These are the illustrative ``strikes_bucket_factor``
#: values from ``docs/architecture/2026-05-21-foul-ball-weighting.md`` §2.1 /
#: ``docs/data/foul_rate_by_count.csv``: a foul is ~1.55x as likely per swing at
#: two strikes (the protect-the-plate / foul-off step-up), and a no-op (1.0 /
#: 1.05) outside two strikes.  The table is keyed by the strike bucket because
#: that is the dominant, behaviourally-grounded signal (foul-ball doc §2.1
#: "granularity decision"); a future calibration can swap in measured league
#: rates (and refine to all 12 counts) as a *data* change with no logic change.
STRIKES_BUCKET_FOUL_FACTOR: dict[int, float] = {
    0: 1.00,  # 0 strikes: a foul can only become strike 1 (bounded) -> no tilt
    1: 1.05,  # 1 strike : a foul can only become strike 2 (bounded) -> tiny tilt
    2: 1.55,  # 2 strikes: absorbing, count-neutral foul-off -> the big step-up
}


def strikes_bucket_foul_factor(strikes: int) -> float:
    """The SIM-056 count-conditional foul multiplier for ``count_strikes``.

    Returns ``STRIKES_BUCKET_FOUL_FACTOR[min(strikes, 2)]`` — the strike count is
    clamped into the 0/1/2 bucket so a transient ``strikes`` value never raises
    (the two-strike bucket is the ceiling; the absorbing rule means strikes never
    truly exceeds 2 mid-PA).  This is the *only* count-conditioning SIM-056
    injects; everything else stays empirical (the pool's own foul tendency is
    preserved — foul-ball doc §2.2).
    """
    if strikes < 0:
        raise ValueError(f"count_strikes cannot be negative: {strikes}")
    return STRIKES_BUCKET_FOUL_FACTOR[min(int(strikes), 2)]


def apply_count_foul_weighting(
    distribution: dict[str, float], balls: int, strikes: int
) -> dict[str, float]:
    """SIM-056 Option-A re-weight: tilt a sampled outcome ``distribution`` toward
    ``foul`` by the count-conditional factor, then renormalize the closed
    pitch-outcome vocabulary to sum to 1.0 (foul-ball doc §3.2 Option A).

    ``distribution`` is a ``{pitch_outcome: probability}`` map over (a subset of)
    :data:`PITCH_OUTCOMES` — the neighbourhood mix the sampler drew from.  This
    multiplies the ``foul`` bucket's mass by
    :func:`strikes_bucket_foul_factor` for the live count and renormalizes; with
    the factor > 1 at two strikes that shifts probability **into** ``foul`` and
    (via renormalization) **out of** the terminal ``swinging_strike`` /
    ``in_play`` buckets — exactly the real-world more-foul-offs ⇒ fewer
    strike-threes / balls-in-play effect.

    Pure / side-effect-free: returns a NEW dict and never touches the sampler.
    The factor is ``1.0`` (a no-op) outside two strikes under the §2.1
    placeholders, so this only reshapes the mix where the empirical pool is
    missing the count tilt.  ``balls`` is accepted for the future per-(balls,
    strikes) calibration (foul-ball doc §2.1) but does not affect the bucketed
    factor today.
    """
    if not distribution:
        return {}
    factor = strikes_bucket_foul_factor(strikes)
    reweighted: dict[str, float] = {}
    for outcome, mass in distribution.items():
        m = float(mass)
        if m < 0.0:
            raise ValueError(f"outcome mass for {outcome!r} cannot be negative: {mass!r}")
        reweighted[outcome] = m * factor if outcome == _FOUL_OUTCOME else m
    total = sum(reweighted.values())
    if total <= 0.0:
        # Degenerate (all-zero) distribution: nothing to renormalize; return the
        # zeroed map unchanged rather than dividing by zero.
        return reweighted
    return {outcome: mass / total for outcome, mass in reweighted.items()}


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
#     by module path nor turns a distance into a weight -- it consumes a bounded
#     *signal* (out/error/safe probabilities, an advance count) supplied by an
#     injected :class:`PlayResolver`.  In production the resolver wraps the
#     fielder/baserunner/steal/catcher RBF engines (+ the sampler for the
#     batted-ball / stolen-base draws); in tests it is injected directly so the
#     loop unit-tests with NO live DuckDB / FAISS (the SIM-302/303/316/317/318
#     dependency-injection pattern).
#   * The stolen-base *pool* is reached through ``sim.stolen_base_pool`` -- an
#     injectable accessor on the StateMachine named to match the spec's access
#     path (§3 item 4) -- so the steal outcome is a sampled historical result
#     (safe / caught), not a hardcoded coin flip.


#: Hit-value of the batter's result, in the ``result_hits`` vocabulary the play
#: pool stores and ``run_resolution.advance_state`` consumes: 0=out, 1=1B, 2=2B,
#: 3=3B, 4=HR (``db/schemas/02_duckdb_schema.sql`` sim.outcome_pool).
_HIT_VALUE_BY_EVENT: dict[str, int] = {
    "single": 1,
    "double": 2,
    "triple": 3,
    "home_run": 4,
}

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

    Fields mirror the play pool's ``result_*`` columns + the fielder RBF outputs
    so a production :class:`PlayResolver` can fill it from the sampler + engines,
    and a test can inject it verbatim.
    """

    event: str  # Statcast/canonical batted-ball event
    result_hits: int  # 0=out 1=1B 2=2B 3=3B 4=HR (pool vocab)
    result_outs: int  # outs recorded on the play (0/1/2)
    result_runs: int  # runs that physically scored on the play
    fielder_id: int | None = None  # the fielder who handled the ball (RBF)
    is_error: bool = False  # error flag (fielder/catcher RBF)
    exit_velo: float | None = None
    launch_angle: float | None = None
    spray_angle: float | None = None


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


class PlayResolver:
    """Injectable provider of the engine-derived signals SIM-319 consumes.

    This is the seam that keeps the loop engine-/DB-free in tests and keeps the
    engines distance-pure in production.  The default implementation here wires
    the **sampler** for the batted-ball draw (step 5) and applies a conservative,
    engine-signal-shaped resolution; a production subclass overrides the hooks to
    consult the fielder / baserunner / steal / catcher RBF engines (resolved via
    the registry) and the ``sim.stolen_base_pool``.  Tests inject a fake.

    The resolver returns *signals/value objects only* -- it NEVER mutates the
    GameState and NEVER computes a run value (that is the loop's job, via
    ``resolve_runs``).  Every method is overridable in isolation.
    """

    def resolve_fielding(self, state: GameState, battedball_sample: dict | None) -> FieldingSignal:
        """Map a sampled batted-ball event to a :class:`FieldingSignal` (step 6).

        The default reads the play pool's ``result_hits/result_outs/result_runs``
        if the sampler carried them (the context-aware path :func:`resolve_runs`
        wants); otherwise it infers a conservative delta from the event string
        (a hit reaches the batter and scores no runner without runner context; an
        out records one -- or two for a DP).  A production resolver replaces the
        inference with the fielder RBF out/error signal.
        """
        sample = battedball_sample or {}
        event = str(sample.get("event", "field_out"))
        hits = sample.get("result_hits")
        outs = sample.get("result_outs")
        runs = sample.get("result_runs")
        if hits is None:
            hits = _HIT_VALUE_BY_EVENT.get(event, 0)
        if outs is None:
            outs = 2 if event in _DOUBLE_PLAY_EVENTS else (0 if int(hits) > 0 else 1)
        if runs is None:
            runs = 0
        return FieldingSignal(
            event=event,
            result_hits=int(hits),
            result_outs=int(outs),
            result_runs=int(runs),
            fielder_id=sample.get("fielder_id"),
            is_error=bool(sample.get("is_error", False)),
            exit_velo=sample.get("exit_velo"),
            launch_angle=sample.get("launch_angle"),
            spray_angle=sample.get("spray_angle"),
        )

    def resolve_steal(self, state: GameState) -> StealResolution:
        """Resolve a steal attempt (the pre-pitch decision is already taken).

        Samples the safe/caught outcome from ``sim.stolen_base_pool`` when an
        accessor is wired (a runner/catcher/pitcher-matched historical draw);
        with no pool wired this returns ``attempted=False`` so the loop is a
        no-op (the count-machine / no-DB path).  A production resolver scores the
        baserunner-steal / catcher / pitcher-steal RBFs to bias the draw.
        """
        return StealResolution(attempted=False)


@dataclass(slots=True)
class _SampledStealPool:
    """A tiny in-memory ``sim.stolen_base_pool`` stand-in (the injectable seam).

    Holds a list of ``(success: bool, weight: float)`` historical rows for a
    runner; ``sample(rng)`` draws one weighted by ``recency_weight``.  Production
    wires the real DuckDB-backed pool; tests inject this (or a fake with a fixed
    rng) so the steal outcome is a *sampled historical result*, not a hardcoded
    flip.
    """

    rows: list[tuple[bool, float]]

    def sample(self, rng) -> bool:
        if not self.rows:
            return False
        weights = np.asarray([max(0.0, float(w)) for _, w in self.rows], dtype=np.float64)
        total = float(weights.sum())
        if total <= 0.0:
            idx = int(rng.integers(len(self.rows)))
        else:
            idx = int(rng.choice(len(self.rows), p=weights / total))
        return bool(self.rows[idx][0])


# ---------------------------------------------------------------------------
# Minimal throwaway input (SIM-303 scaffold — kept for back-compat)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PitchState:
    """A deliberately tiny stand-in for the SIM-311 GameState.

    Retained verbatim from the SIM-303 scaffold so the existing scaffold tests
    keep passing.  New SIM-316 code drives the real
    :class:`simulation.game_state.GameState` through :class:`StateMachine`; this
    type only feeds the legacy :meth:`PlateAppearanceSimulator.simulate_pitch`
    single-pitch path.
    """

    pitcher_id: int
    bat_hand: str
    season: int
    balls: int = 0
    strikes: int = 0
    outs: int = 0
    # bases occupancy as a 3-tuple of bools (1B, 2B, 3B).
    bases: tuple[bool, bool, bool] = (False, False, False)


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
# Placeholder pitch-outcome -> PA-event mapping (SIM-303 scaffold — kept)
# ---------------------------------------------------------------------------


def pitch_outcome_to_event(pitch_outcome: str) -> str | None:
    """Map a *non-contact* terminal pitch outcome to a PA-event string, or
    ``None`` when the pitch is in-play (contact) and must be resolved by the
    batted-ball pool instead.

    Retained from the SIM-303 scaffold (the single-pitch path uses it).  The
    real count-driven terminal logic now lives in :func:`advance_count` /
    :meth:`StateMachine.step_pitch`; this stays the back-compat marker mapping
    so the SIM-303 tests keep passing.
    """
    if pitch_outcome == CONTACT_PITCH_OUTCOME:
        return None  # contact -> caller resolves via sample_batted_ball
    return {
        "ball": EVENT_IN_PROGRESS,
        "called_strike": EVENT_IN_PROGRESS,
        "swinging_strike": EVENT_IN_PROGRESS,
        "foul": EVENT_IN_PROGRESS,
    }.get(pitch_outcome, EVENT_IN_PROGRESS)


# ---------------------------------------------------------------------------
# The plate-appearance simulator (SIM-303 single-pitch scaffold — kept)
# ---------------------------------------------------------------------------


class PlateAppearanceSimulator:
    """SIM-303 scaffold: a sim-loop-shaped single caller of the PlayPoolSampler.

    Kept intact so the SIM-303 tests pass unchanged.  :meth:`simulate_pitch`
    samples ONE pitch (and, on contact, one batted ball) and returns the legacy
    ``PlayResult``-shaped dict.  The SIM-316 state machine
    (:class:`StateMachine`) is the GameState-driven replacement; it reuses this
    class's stub fingerprints to keep the sampler wiring in one place.
    """

    def __init__(
        self,
        sampler: PlayPoolSampler,
        *,
        k: int = 25,
        rng: np.random.Generator | None = None,
        fingerprint_deriver=None,
    ) -> None:
        if sampler is None:
            raise ValueError("PlateAppearanceSimulator requires a PlayPoolSampler.")
        self.sampler = sampler
        self.k = int(k)
        # Used ONLY for the synthetic stub fingerprints; the sampler holds its
        # own rng for the actual k-NN draws.
        self.rng = rng if rng is not None else np.random.default_rng()
        # SIM-317: an optional FingerprintDeriver replaces the stub hashes with
        # the real game-state-derived 10-dim / 3-dim feature vectors.  When None,
        # the legacy stub hash is used (keeps any caller without the engine-backed
        # provider working; the count-machine-only no-DB path never reaches here).
        self.fingerprint_deriver = fingerprint_deriver

    # ---- fingerprints (SIM-317 real derivation, with a stub fall-back) -----

    def _pitch_fingerprint(self, state: PitchState | GameState) -> NDArray[np.float32]:
        """Build the 10-dim pitch query vector (spec §4.1).

        SIM-317: when a :class:`simulation.fingerprints.FingerprintDeriver` is
        injected, derive the real vector in ``PITCH_FEATURES`` order from the
        matchup geometry + SIM-321 fusion (z-score + sqrt-weight normalized into
        the SIM-041 tile space; pre-filter keys are NOT vector dims, spec §4.3).
        Otherwise fall back to the legacy deterministic-hash stub so the FAISS
        search still runs (this only happens on the real sampling path).
        """
        if self.fingerprint_deriver is not None:
            return self.fingerprint_deriver.pitch_fingerprint(state)
        seed = abs(
            hash((state.pitcher_id, state.bat_hand, state.balls, state.strikes, state.outs))
        ) % (2**32)
        local = np.random.default_rng(seed)
        return local.standard_normal(_PITCH_FINGERPRINT_DIM).astype(np.float32)

    def _battedball_fingerprint(self, state: PitchState | GameState) -> NDArray[np.float32]:
        """Build the 3-dim batted-ball query vector (EV / LA / spray; spec §4.2).

        SIM-317: when a deriver is injected, derive the real contact-quality
        vector in ``BATTED_BALL_FEATURES`` order (z-score + sqrt-weight into the
        SIM-042 tile space).  Otherwise fall back to the legacy hash stub.
        """
        if self.fingerprint_deriver is not None:
            return self.fingerprint_deriver.battedball_fingerprint(state)
        seed = abs(hash((state.bat_hand, state.pitcher_id, state.outs))) % (2**32)
        local = np.random.default_rng(seed)
        return local.standard_normal(_BATTEDBALL_FINGERPRINT_DIM).astype(np.float32)

    # ---- the SIM-303 single-pitch step (kept verbatim) --------------------

    def simulate_pitch(self, state: PitchState) -> dict:
        """Simulate ONE pitch via the play-pool sampler and return the legacy
        ``PlayResult``-shaped dict (the SIM-303 scaffold deliverable).

        SIM-316 leaves this single-pitch path untouched; the GameState-driven
        loop is :meth:`StateMachine.step_pitch`.
        """
        # --- pitch fingerprint -> sampler.sample_pitch ---------------------
        pitch_fp = self._pitch_fingerprint(state)
        pitch_sample = self.sampler.sample_pitch(
            state.pitcher_id, state.bat_hand, state.season, pitch_fp, k=self.k
        )
        pitch_outcome = pitch_sample["pitch_outcome"]
        fellback = bool(pitch_sample.get("fellback", False))

        is_contact = pitch_outcome == CONTACT_PITCH_OUTCOME
        battedball_sample: dict | None = None
        event: str | None

        if is_contact:
            bb_fp = self._battedball_fingerprint(state)
            battedball_sample = self.sampler.sample_batted_ball(
                state.bat_hand, state.season, bb_fp, k=self.k
            )
            event = battedball_sample["event"]
            fellback = fellback or bool(battedball_sample.get("fellback", False))
        else:
            event = pitch_outcome_to_event(pitch_outcome)

        runs = float(RUN_VALUES.get(event, 0.0)) if event is not None else 0.0

        return {
            "pitch_outcome": pitch_outcome,
            "is_contact": is_contact,
            "event": event,
            "runs": runs,
            "fellback": fellback,
            "pitch_sample": pitch_sample,
            "battedball_sample": battedball_sample,
        }


# ---------------------------------------------------------------------------
# StateMachine — the SIM-316 GameState-driven §5/§6 skeleton
# ---------------------------------------------------------------------------


class StateMachine:
    """The SIM-316 plate-appearance / half-inning state machine.

    Reads a mutable SIM-311 :class:`~simulation.game_state.GameState`, drives the
    §5.1 count machine and §6.1 half-inning logic, commits the result, and emits
    a :class:`~simulation.game_state.PlayResult`.  The per-pitch sampler call is
    wired through a :class:`PlateAppearanceSimulator` (so the stub fingerprints +
    sampler plumbing live in one place); a ``sampler`` may be omitted entirely to
    drive the *count/out/base* machine in isolation (the SIM-316 acceptance tests
    do this — they classify a supplied pitch outcome without sampling).

    Ownership boundaries this ticket respects (see module docstring):

      * §5.2 foul **re-weight** + the step-4 outcome-determination detail -> SIM-318;
      * fielding/baserunning resolution, steal, dropped-third-strike -> SIM-319;
      * walk-off / extra-innings / ``simulate_game()`` -> SIM-320;
      * fingerprint derivation -> SIM-317; manager hooks -> SIM-323.
    """

    def __init__(
        self,
        sampler: PlayPoolSampler | None = None,
        *,
        k: int = 25,
        rng: np.random.Generator | None = None,
        fingerprint_deriver=None,
        resolver: PlayResolver | None = None,
        sim=None,
        manager=None,
        bench=None,
        full_pool_sampler=None,
    ) -> None:
        self.rng = rng if rng is not None else np.random.default_rng()
        self.k = int(k)
        # SIM-424: opt-in full-pool similarity-weighted pitch sampler. When wired
        # (the engine-artifacts bundle is present + the flag is set) step_pitch
        # draws the pitch outcome from it instead of the per-tile sample_pitch;
        # None keeps the validated default path untouched.
        self.full_pool_sampler = full_pool_sampler
        self._fp_matchup: tuple | None = None
        # The single-pitch helper owns the (SIM-317 real / stub) fingerprint +
        # sampler call.  When no sampler is supplied the machine is "count-machine
        # only": the caller passes a pitch_outcome directly into step_pitch (the
        # loop body exercised by the SIM-316 unit tests, no live DB/FAISS
        # required) -- the deriver is NEVER touched on that path.
        self.sampler = sampler
        self.fingerprint_deriver = fingerprint_deriver
        # SIM-319: the fielding/baserunning/steal resolver (the engine-signal
        # seam).  Defaults to the in-loop :class:`PlayResolver` (sampler-driven,
        # engine-signal-shaped); a production caller injects an engine-backed
        # subclass, a test injects a fake.
        self.resolver = resolver if resolver is not None else PlayResolver()
        # SIM-319: the simulation context that carries ``stolen_base_pool`` (the
        # spec's ``sim.stolen_base_pool`` access path, §3 item 4).  Injectable so
        # the steal outcome is a sampled historical safe/caught result; ``None``
        # in the no-DB / count-machine path (steals are then a no-op).
        self.sim = sim
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
        self._pa: PlateAppearanceSimulator | None = (
            PlateAppearanceSimulator(
                sampler,
                k=k,
                rng=self.rng,
                fingerprint_deriver=fingerprint_deriver,
            )
            if sampler is not None
            else None
        )

    # ===================================================================
    # Pitch step: read GameState -> sample/classify -> commit -> PlayResult
    # ===================================================================

    def step_pitch(
        self,
        state: GameState,
        *,
        pitch_outcome: str | None = None,
        outcome_distribution: dict[str, float] | None = None,
    ) -> PlayResult:
        """Advance the game by exactly ONE pitch (spec §2 step 1 -> step 8).

        Flow:
          1. **Read** the GameState and validate the live (in-play) invariants.
          2. **Pre-pitch hook** (§3): manager decisions — a no-op stub here,
             SIM-323 owns the logic.
          3. **Sample** the pitch outcome (via the wired sampler) unless the
             caller supplied ``pitch_outcome`` directly (count-machine-only mode).
          4. **Outcome determination (SIM-318, §5.1/§5.2)** — apply the SIM-056
             count-conditional **foul re-weight** to the committed outcome
             *before* the count advances, then **classify** the count advance /
             PA-terminal via :func:`advance_count` (§5.1 — incl. the SIM-056
             two-strike-foul absorbing rule).
          5. **Commit** the new count to the GameState; on terminal, resolve the
             PA (walk / strikeout / in-play) and roll the PA over, recording outs
             and flipping the half-inning at 3 outs (§6.1).
          6. **Emit** a typed ``PlayResult`` whose ``next_state`` is the committed
             GameState.

        ``pitch_outcome`` (when given) MUST be one of
        :data:`simulation.game_state.PITCH_OUTCOMES`; it lets the SIM-316/318 tests
        drive deterministic count sequences without a live sampler.

        ``outcome_distribution`` (when given) is a ``{pitch_outcome: prob}`` map
        over the sampled neighbourhood; the SIM-056 Option-A re-weight
        (:func:`apply_count_foul_weighting`) is applied to it for the live count
        and the committed outcome is drawn from the re-weighted distribution with
        the machine's ``rng``.  This is the count-blind-sampler-preserving path
        the SIM-318 tests use to assert the re-weight without a live DB/FAISS.
        Supplying both ``pitch_outcome`` and ``outcome_distribution`` is an error.
        """
        # --- Step 1: read + validate the incoming (live) state -------------
        state.assert_invariants(in_play=True)

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

        if pitch_outcome is not None and outcome_distribution is not None:
            raise ValueError(
                "pass at most one of pitch_outcome / outcome_distribution to "
                "step_pitch (they are two ways to supply the sampled outcome)."
            )

        # --- Steps 2/3: pitch selection + sampling -------------------------
        pitch_sample: dict | None = None
        fellback = False
        if outcome_distribution is not None:
            # SIM-318 Option-A path: the caller supplies the sampled neighbourhood
            # distribution; the sampler stays count-blind.  The SIM-056 foul
            # re-weight (step 4) reshapes it for the live count, then we draw.
            pitch_outcome = self._draw_reweighted_outcome(
                outcome_distribution, state.balls, state.strikes
            )
        elif pitch_outcome is None:
            if self._pa is None:
                raise ValueError(
                    "step_pitch needs either an injected sampler or an explicit "
                    "pitch_outcome (count-machine-only mode)."
                )
            if self.full_pool_sampler is not None:
                # SIM-424: full-pool similarity-weighted draw (Situation+Pitcher+
                # Batter); count still enters via the §5.1 machine downstream.
                pitch_outcome = self._full_pool_outcome(state)
                fellback = False
            else:
                pitch_fp = self._pa._pitch_fingerprint(state)  # TODO(SIM-317)
                pitch_fp = self._jitter_query(pitch_fp, _PITCH_FEATURE_SCALE)
                pitch_sample = self._pa.sampler.sample_pitch(
                    state.pitcher_id, state.bat_hand, state.season, pitch_fp, k=self.k
                )
                pitch_outcome = pitch_sample["pitch_outcome"]
                fellback = bool(pitch_sample.get("fellback", False))
                # SIM-318 Option-B path: the count-blind sampler returned a single
                # draw; bias *whether* a foul is accepted by the count-conditional
                # factor (re-sample on rejection from the same count-blind sampler).
                pitch_outcome = self._accept_or_resample_foul(
                    pitch_outcome,
                    state,
                    pitch_fp,
                )
        elif pitch_outcome not in PITCH_OUTCOMES:
            raise ValueError(f"pitch_outcome {pitch_outcome!r} is not one of {PITCH_OUTCOMES}.")

        # --- Step 4: outcome determination (§5.1 count machine) ------------
        # The SIM-056 count-conditional foul re-weight has already been applied
        # (above, BEFORE the count advance) on the path that sampled the outcome;
        # advance_count then applies the §5.1 terminal mechanics incl. the
        # two-strike-foul absorbing rule.  When the caller injected a fixed
        # pitch_outcome (count-machine-only mode) no re-weight applies — the
        # outcome is taken verbatim so deterministic count sequences are exact.
        adv = advance_count(state.balls, state.strikes, pitch_outcome)

        result = PlayResult(
            pitch_outcome=pitch_outcome,
            is_contact=adv.is_contact,
            pa_terminal=adv.terminal,
            event=adv.event if adv.event != EVENT_IN_PROGRESS else None,
            pitch_sample=pitch_sample,
            fellback=fellback,
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

    def _draw_reweighted_outcome(
        self, distribution: dict[str, float], balls: int, strikes: int
    ) -> str:
        """SIM-318 Option-A: re-weight a sampled outcome ``distribution`` by the
        count-conditional foul factor (:func:`apply_count_foul_weighting`) and
        draw one committed ``pitch_outcome`` from it with the machine's ``rng``.

        The re-weight (the foul-mass tilt + renormalization) lives entirely here
        in the loop's step 4; the sampler that produced ``distribution`` stays
        count-blind and distance-pure (foul-ball doc §3.1).  Every key in
        ``distribution`` must be a valid :data:`PITCH_OUTCOMES` member.
        """
        for outcome in distribution:
            if outcome not in PITCH_OUTCOMES:
                raise ValueError(
                    f"outcome_distribution key {outcome!r} is not one of {PITCH_OUTCOMES}."
                )
        reweighted = apply_count_foul_weighting(distribution, balls, strikes)
        total = sum(reweighted.values())
        if total <= 0.0:
            raise ValueError(
                "outcome_distribution has zero total mass after the foul "
                "re-weight; cannot draw an outcome."
            )
        outcomes = list(reweighted.keys())
        probs = np.asarray([reweighted[o] for o in outcomes], dtype=np.float64)
        probs = probs / probs.sum()  # absorb any residual float drift
        idx = int(self.rng.choice(len(outcomes), p=probs))
        return outcomes[idx]

    def _accept_or_resample_foul(self, pitch_outcome: str, state: GameState, pitch_fp) -> str:
        """SIM-318 Option-B: bias a single count-blind ``sample_pitch`` draw by
        the SIM-056 count-conditional foul factor (foul-ball doc §3.2 Option B).

        With the factor ``f`` for the live ``count_strikes``:

          * ``f >= 1`` (the placeholder regime: 1.0 / 1.05 / 1.55) — a non-foul
            draw is accepted as-is; a ``foul`` draw is *always* accepted (a
            tilt-toward-foul never rejects a foul), and additionally a non-foul
            draw is *converted* to a foul with probability so that the realized
            foul rate matches ``f x`` the count-blind foul rate.  Because the
            count-blind foul rate is unknown to the loop, we approximate the
            tilt by drawing one extra count-blind pitch and, if it is a foul,
            accepting that foul with probability ``(f - 1)`` — this injects
            exactly the *additional* foul mass ``(f - 1)`` * p(foul) the factor
            calls for while leaving the sampler count-blind.
          * ``f == 1`` — a strict no-op (the non-two-strike placeholder regime).

        The sampler is queried again only when ``f > 1`` AND the first draw was a
        non-foul, so the common case (and every non-two-strike count under the
        placeholders) costs no extra FAISS work.  The sampler itself never sees
        the count.
        """
        factor = strikes_bucket_foul_factor(state.strikes)
        if factor <= 1.0 or pitch_outcome == _FOUL_OUTCOME or self._pa is None:
            return pitch_outcome
        # f > 1 and the first draw was a non-foul: inject the additional foul
        # mass (f - 1) * p(foul) by drawing one more count-blind pitch and, if it
        # is a foul, converting to a foul with probability (f - 1).
        extra = float(factor - 1.0)
        if extra > 0.0 and float(self.rng.random()) < min(1.0, extra):
            second = self._pa.sampler.sample_pitch(
                state.pitcher_id, state.bat_hand, state.season, pitch_fp, k=self.k
            )
            if second.get("pitch_outcome") == _FOUL_OUTCOME:
                return _FOUL_OUTCOME
        return pitch_outcome

    def _jitter_query(self, fp, scale):
        """Scatter a deriver CENTROID query per pitch (SIM-421 L2).

        Adds zero-mean Gaussian noise (per-dim ``scale * sigma``, drawn from the
        loop rng so it stays reproducible from the per-game seed) so the k-NN
        samples the pre-filtered tile representatively instead of collapsing onto
        the dense centre.  A no-op on the stub-fingerprint path (already
        stochastic) and when the engine scale is unavailable.
        """
        if self.fingerprint_deriver is None or scale is None or fp is None:
            return fp
        noise = self.rng.standard_normal(np.shape(fp)).astype(np.float32)
        return (np.asarray(fp, dtype=np.float32) + noise * (scale * _QUERY_JITTER_SIGMA)).astype(
            np.float32
        )

    def _full_pool_outcome(self, state: GameState) -> str:
        """SIM-424: draw a pitch outcome from the full-pool sampler.

        Refreshes the matchup when (pitcher, hand, batter) changes — the pitcher
        factor is half-inning-constant, the batter + situation factors are
        PA-constant — and conditions the situation at the PA start; the count then
        enters downstream via the §5.1 count machine (as the legacy path did)."""
        fp = self.full_pool_sampler
        hand = state.bat_hand if state.bat_hand in fp.a.pools else "R"
        key = (state.pitcher_id, hand, state.batter_id)
        if key != self._fp_matchup:
            self._fp_matchup = key
            season = int(getattr(state, "season", 2024) or 2024)
            fp.new_half_inning(hand, f"{state.pitcher_id}:{season}")
            bat = state.home_score if state.offense == Team.HOME else state.away_score
            fld = state.away_score if state.offense == Team.HOME else state.home_score
            score_diff = max(-5, min(5, int(bat) - int(fld)))
            # base-out only; the count is conditioned per pitch via the draw bucket.
            base_out = np.array(
                [state.outs, state.runners_state, state.inning, score_diff], dtype=np.float32
            )
            fp.new_plate_appearance(f"{state.batter_id}:{season}", base_out)
        return self._apply_framing(state, fp.draw(state.balls, state.strikes))

    def _apply_framing(self, state: GameState, outcome: str) -> str:
        """SIM-428: nudge a TAKEN pitch (ball<->called_strike) by the fielding
        catcher's centred framing delta — a good framer steals a strike, a poor one
        loses one.  Swings (foul / swinging_strike / in_play) are never frameable.
        Aggregate-neutral across the league (the delta is ~centred); the effect is
        per-catcher differentiation.  No-op when no catcher / framing signal."""
        if outcome not in ("ball", "called_strike"):
            return outcome
        fp = self.full_pool_sampler
        if fp is None:
            return outcome
        catcher = state.away_catcher_id if state.offense == Team.HOME else state.home_catcher_id
        if catcher is None:
            return outcome
        season = int(getattr(state, "season", 2024) or 2024)
        d = fp.catcher_framing(f"{int(catcher)}:{season}")
        if d == 0.0:
            return outcome
        r = float(self.rng.random())
        if outcome == "ball" and d > 0.0 and r < d:
            return "called_strike"
        if outcome == "called_strike" and d < 0.0 and r < -d:
            return "ball"
        return outcome

    def _full_pool_fielding(self, state: GameState) -> FieldingSignal | None:
        """SIM-425: draw a batted ball from the full bat_hand batted-ball pool
        (Batter + Situation weighting) -> a FieldingSignal.  None when no full-pool
        sampler / batted-ball pool is wired (the per-tile path then runs)."""
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
        fp.battedball_new_pa(hand, f"{state.batter_id}:{season}", sit)
        ev, rh, _ro, la = fp.battedball_draw()
        # SIM-425/429: outs_on_pitch is unreliable in the pool (~0 for most field
        # outs), so infer outs from the event (as the default resolver does) —
        # otherwise only strikeouts end the half-inning and games bloat.
        outs = 2 if ev in _DOUBLE_PLAY_EVENTS else (0 if int(rh) > 0 else 1)
        return FieldingSignal(
            event=ev, result_hits=int(rh), result_outs=int(outs), result_runs=0,
            launch_angle=float(la),
        )

    def _tag_rate(self, runner_id: int | None, season: int, event: str) -> float:
        """SIM-425: probability the runner on 3rd tags & scores on a fly out.  An
        explicit ``sac_fly`` draw means the ball was deep enough, so score near-
        certainly; a generic deep fly uses the runner's own ``tag_up_attempt_rate``
        from the baserunner embedding (league ~0.45 fallback)."""
        if event in ("sac_fly", "sac_fly_double_play"):
            return 0.92
        fp = self.full_pool_sampler
        if fp is not None and runner_id is not None:
            r = fp.runner_rate(f"{int(runner_id)}:{int(season)}", "tag_up_attempt_rate")
            if r is not None:
                return float(min(max(r, 0.0), 0.95))
        return 0.45

    def _full_pool_out_advancement(
        self, state: GameState, result: PlayResult, sig: FieldingSignal
    ) -> int:
        """SIM-425: advance runners on a full-pool OUT and return the runs scored.

        Fly outs tag the runner home from 3rd (sac fly) and push a runner from 2nd
        to an open 3rd on a deep fly; ground outs push the lead runner one base on a
        productive grounder.  Double plays erase the trail runner (no productive
        run), and with 2 outs the inning-ending out scores no one.  Mutates
        ``state.bases`` consistently — the run VALUE is still committed by the
        caller via :meth:`_commit_run_delta` -> ``resolve_runs``."""
        event = sig.event or ""
        if event in _DOUBLE_PLAY_EVENTS or state.outs >= 2:
            return 0
        la = float(sig.launch_angle) if sig.launch_angle is not None else 0.0
        season = int(getattr(state, "season", 2024) or 2024)
        old = state.bases
        new_first, new_second, new_third = old.first, old.second, old.third
        runs = 0
        advances: dict[int, int] = {}
        is_fly = (la >= 24.0) or event in ("sac_fly", "sac_fly_double_play")
        if is_fly:
            if old.third is not None and float(self.rng.random()) < self._tag_rate(
                old.third, season, event
            ):
                runs += 1
                new_third = None
                advances[old.third] = 0
            if old.second is not None and new_third is None and la >= 28.0 and (
                float(self.rng.random()) < 0.30
            ):
                new_third, new_second = old.second, None
                advances[old.second] = 3
        else:
            # Ground out: lead runner advances one base on a productive grounder.
            if old.third is not None and float(self.rng.random()) < 0.28:
                runs += 1
                new_third = None
                advances[old.third] = 0
            elif old.second is not None and old.third is None and (
                float(self.rng.random()) < 0.35
            ):
                new_third, new_second = old.second, None
                advances[old.second] = 3
        state.bases = Bases(first=new_first, second=new_second, third=new_third)
        state.bases.assert_consistent()
        if advances:
            result.baserunner_advances.update({k: v for k, v in advances.items() if k != -1})
        return runs

    # ===================================================================
    # SIM-319 — run/base-out delta via resolve_runs (the ONE place, §8)
    # ===================================================================

    def _commit_run_delta(
        self,
        state: GameState,
        result: PlayResult,
        *,
        event: str | None,
        result_hits: int,
        result_outs: int,
        result_runs: int,
    ) -> None:
        """Resolve + commit ONE play's run value and base-out delta (spec §8).

        This is the SINGLE place the loop turns a sampled
        ``result_hits/result_outs/result_runs`` delta into runs and a base-out
        state: it calls :func:`simulation.run_resolution.resolve_runs` with the
        CURRENT base-out state and the sampled deltas, records the RE24 (or
        linear-weight fallback) provenance on the ``PlayResult``, commits the
        integer runs that physically scored to the score, and records the outs.
        No run value or base-out arithmetic is ever done inline (SIM-312 §8).

        The *placement* of runners on bases (which runner is on which bag) is the
        baserunning step's job (:meth:`_advance_runners`); ``resolve_runs`` owns
        the RUN VALUE + the out/occupancy COUNT.  ``runs_scored`` /
        ``outs_recorded`` here are the authoritative integer deltas the score and
        out machine commit.
        """
        rr = resolve_runs(
            event=event,
            outs=state.outs,
            runners_state=state.runners_state,
            result_hits=int(result_hits),
            result_outs=int(result_outs),
            result_runs=int(result_runs),
        )
        result.runs = rr.runs
        result.run_resolution_method = rr.method
        result.canonical_event = rr.canonical_event
        result.re_start = rr.re_start
        result.re_end = rr.re_end
        result.runs_scored = int(result_runs)
        if result_runs:
            state.add_runs(int(result_runs))
            state.assert_score_valid()
        if result_outs:
            # SIM-421: a sampled multi-out event (e.g. a GIDP carrying 2 outs)
            # comes from a DIFFERENT base-out context; clamp it to the outs that
            # actually remain in the half-inning so a double play with 2 already
            # out ends the inning at 3 rather than overflowing to 4 (the ceiling
            # assertion in _record_outs).
            outs_to_record = min(int(result_outs), max(0, OUTS_PER_INNING - state.outs))
            if outs_to_record:
                result.outs_recorded += outs_to_record
                self._record_outs(state, outs_to_record)

    # ===================================================================
    # SIM-319 — Step 7 baserunning (per-runner advancement on the bases)
    # ===================================================================

    #: Probability a runner takes ONE extra base beyond the batter's hit value,
    #: keyed by (bases_advanced, from_base) — the Retrosheet-style extra-advance
    #: rates (e.g. 2nd->home on a single ~55%, 1st->3rd ~28%, 1st->home on a
    #: double ~45%).  Absent keys = no extra base (SIM-421).
    _EXTRA_ADVANCE_P: dict[tuple[int, int], float] = {
        (1, 2): 0.55,  # single, runner on 2nd -> scores
        (1, 1): 0.28,  # single, runner on 1st -> 3rd
        (2, 1): 0.45,  # double, runner on 1st -> scores
    }

    #: SIM-425: which baserunner-embedding rate governs each extra-base advance.
    _ADVANCE_RATE_FEATURE: dict[tuple[int, int], str] = {
        (1, 2): "second_to_home_attempt_rate",  # single, runner 2nd -> scores
        (1, 1): "first_to_third_attempt_rate",  # single, runner 1st -> 3rd
        (2, 1): "first_to_home_attempt_rate",   # double, runner 1st -> scores
    }

    def _advance_rate(
        self, from_base: int, bases_advanced: int, runner_id: int | None, season: int
    ) -> float:
        """The probability a runner takes one extra base.  SIM-425: when the
        full-pool sampler + this runner's baserunner embedding are present, use the
        runner's OWN attempt-rate (engine-backed); otherwise the Retrosheet league
        constant.  Keeps the per-tile path (no full_pool_sampler) on the constant."""
        constant = self._EXTRA_ADVANCE_P.get((int(bases_advanced), int(from_base)), 0.0)
        fp = self.full_pool_sampler
        feat = self._ADVANCE_RATE_FEATURE.get((int(bases_advanced), int(from_base)))
        if fp is not None and feat is not None and runner_id is not None:
            rate = fp.runner_rate(f"{int(runner_id)}:{int(season)}", feat)
            if rate is not None:
                return float(min(max(rate, 0.0), 0.97))
        return constant

    def _extra_advance(
        self, from_base: int, bases_advanced: int, runner_id: int | None = None, season: int = 2024
    ) -> int:
        """Return 1 if the runner takes an extra base beyond the hit value, else 0
        (SIM-421/425).  Only hits (bases_advanced 1/2) grant extra bases; the draw
        comes from the loop rng so a fixed seed reproduces the advance."""
        p = self._advance_rate(from_base, bases_advanced, runner_id, season)
        return 1 if p and float(self.rng.random()) < p else 0

    def _advance_runners(
        self, state: GameState, result: PlayResult, *, batter_reached: int, bases_advanced: int
    ) -> int:
        """Advance the runners currently on base + place the batter (step 7).

        ``batter_reached`` is the base the batter ends on (0 == out / not on base,
        1/2/3 = the bag reached, 4 == home/HR scores).  ``bases_advanced`` is how
        many bases each existing runner is pushed (1 for a single/walk-force-ish,
        2 for a double, etc.).  Returns the integer number of runners who SCORED
        (crossed home) so the caller can hand that to :meth:`_commit_run_delta`
        as the ``result_runs`` delta — the run VALUE is still resolved there via
        ``resolve_runs``, never here.

        Mutates ``state.bases`` consistently (no two runners on one bag, no
        impossible advance) and records each surviving runner's end base in
        ``result.baserunner_advances``.  This is the only place runner identities
        move; it owns OCCUPANCY, not run value.
        """
        old = state.bases
        # Collect existing runners as (from_base, runner_id), lead runner first.
        existing = [
            (3, old.third),
            (2, old.second),
            (1, old.first),
        ]
        new_first = new_second = new_third = None
        runs_scored = 0
        advances: dict[int, int] = {}
        for from_base, rid in existing:
            if rid is None:
                continue
            # SIM-421: a baserunner advances the batter's hit value PLUS a
            # probabilistic extra base (the loop rng keeps it deterministic).  The
            # old rigid ``from_base + bases_advanced`` stranded the runner from 2nd
            # on every single (he only reached 3rd), suppressing run-scoring ~26%.
            # Probabilities track the Retrosheet extra-base-advance rates.
            dest = from_base + int(bases_advanced) + self._extra_advance(
                from_base, bases_advanced, runner_id=rid,
                season=int(getattr(state, "season", 2024) or 2024),
            )
            if dest >= 4:
                runs_scored += 1
                advances[rid] = 0  # 0 == scored
                continue
            # Place on the destination bag.  Under a uniform push runners never
            # pass each other, so the lead-most bag is always free first; if a
            # bag is somehow contested, fall the runner to the nearest open
            # trailing bag so the base state stays consistent (no two on a bag).
            placed = dest
            if dest == 3 and new_third is None:
                new_third = rid
            elif dest >= 2 and new_second is None:
                new_second = rid
                placed = 2
            elif new_first is None:
                new_first = rid
                placed = 1
            elif new_second is None:
                new_second = rid
                placed = 2
            elif new_third is None:
                new_third = rid
                placed = 3
            advances[rid] = placed
        # Place the batter.
        if batter_reached >= 4:
            runs_scored += 1
            if result.event == "home_run" or state.batter_id is not None:
                advances[state.batter_id if state.batter_id is not None else -1] = 0
        elif batter_reached >= 1:
            bid = state.batter_id if state.batter_id is not None else -1
            if batter_reached == 3 and new_third is None:
                new_third = bid
            elif batter_reached >= 2 and new_second is None:
                new_second = bid
            elif batter_reached >= 1 and new_first is None:
                new_first = bid
            else:
                # Trailing bag fallback so the batter never displaces a runner.
                if new_first is None:
                    new_first = bid
                elif new_second is None:
                    new_second = bid
                elif new_third is None:
                    new_third = bid
            advances[bid] = batter_reached
        state.bases = Bases(first=new_first, second=new_second, third=new_third)
        state.bases.assert_consistent()
        if advances:
            result.baserunner_advances.update({k: v for k, v in advances.items() if k != -1})
        return runs_scored

    # ===================================================================
    # SIM-319 — Step 7 steal outcome (decision in pre-pitch hook, §3)
    # ===================================================================

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
        result.steal_attempted = True
        from_base = steal.from_base
        to_base = (
            steal.to_base
            if steal.to_base is not None
            else (_NEXT_BASE.get(from_base, 4) if from_base else 4)
        )
        rid = steal.runner_id
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
                self._commit_run_delta(
                    state,
                    result,
                    event="stolen_base_home",
                    result_hits=0,
                    result_outs=0,
                    result_runs=1,
                )
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
            self._commit_run_delta(
                state,
                result,
                event="caught_stealing",
                result_hits=0,
                result_outs=1,
                result_runs=0,
            )
            if rid is not None:
                result.baserunner_advances[rid] = 0  # out (off the bases)

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
        b.assert_consistent()

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

    def _resolve_walk(self, state: GameState, result: PlayResult) -> None:
        """Resolve a ball-4 walk (§5.1 / §7): force the runners + score any
        forced run, routing the run/base-out delta through ``resolve_runs``.

        A walk forces the batter to 1B and pushes each runner ahead only when the
        bag behind him is occupied (a true force).  The number of forced runs is
        computed from the base state, then handed to :meth:`_commit_run_delta`.
        """
        result.event = EVENT_WALK
        result.pa_terminal = True
        b = state.bases
        # A walk forces runners only on consecutive occupancy from 1B.
        forced_run = 0
        if b.first is not None:
            if b.second is not None:
                if b.third is not None:
                    forced_run = 1  # bases loaded -> run forced home
                # push 2B -> 3B
                b.third = b.second
            # push 1B -> 2B
            b.second = b.first
        # batter to 1B
        b.first = state.batter_id if state.batter_id is not None else b.first
        b.assert_consistent()
        if state.batter_id is not None:
            result.baserunner_advances[state.batter_id] = 1
        # Route the (possibly forced) run through resolve_runs — no inline runs.
        self._commit_run_delta(
            state,
            result,
            event=EVENT_WALK,
            result_hits=0,
            result_outs=0,
            result_runs=forced_run,
        )

    def _issue_intentional_walk(self, state: GameState) -> PlayResult:
        """Issue an intentional walk signalled by the manager (SIM-323 §3 item 2).

        No pitch is thrown: the batter is awarded first base via the same
        forced-runner mechanics as a ball-4 walk, the run-resolution provenance is
        attached, and the PA is rolled over (batting order advances, count resets
        / half-inning rolls).  Returns the :class:`PlayResult` (``pitch_outcome``
        carries the sentinel ``"ball"`` since an IBB is recorded as four balls; the
        canonical event resolves to a walk via :meth:`_resolve_walk`).
        """
        result = PlayResult(
            pitch_outcome="ball",
            is_contact=False,
            pa_terminal=True,
            event=EVENT_WALK,
        )
        # Consume the signal so it never carries to the next pitch / PA.
        state.manager.intentional_walk_signalled = False
        self._resolve_walk(state, result)
        self._end_of_pa(state, result)
        result.next_state = state
        return result

    def _resolve_strikeout(self, state: GameState, result: PlayResult) -> None:
        """Resolve a strike-3 strikeout (§5.1) incl. the dropped-third-strike
        edge (§5.4).

        Ordinary K: route a one-out delta through ``resolve_runs``.  Dropped
        third strike (uncaught swinging strike-3 with **1B open OR two outs**):
        the batter may reach first — modelled here as a reach (no out, batter to
        1B) when the resolver/edge fires; the run/base-out delta still goes
        through ``resolve_runs``.
        """
        result.event = EVENT_STRIKEOUT
        result.pa_terminal = True
        if self._dropped_third_strike(state, result):
            # Uncaught K3: batter reaches 1B (no out recorded), pushing forced
            # runners exactly like a walk does.  resolve_runs scores any force.
            result.event = "strikeout"  # still a K event; batter reached on D3K
            forced_run = self._force_on_reach(state, result)
            self._commit_run_delta(
                state,
                result,
                event="field_error",  # batter safe at 1B (no out)
                result_hits=1,
                result_outs=0,
                result_runs=forced_run,
            )
            return
        # Ordinary strikeout: one out, no base/score change beyond the out.
        self._commit_run_delta(
            state,
            result,
            event=EVENT_STRIKEOUT,
            result_hits=0,
            result_outs=1,
            result_runs=0,
        )

    def _dropped_third_strike(self, state: GameState, result: PlayResult) -> bool:
        """The §5.4 dropped-third-strike predicate + (optional) resolver roll.

        The edge is *eligible* only on a swinging strike-3 when first base is
        OPEN or there are two outs (the official rule).  Whether the catcher
        actually drops it is a catcher-RBF signal: the injected resolver may
        expose ``dropped_third_strike(state, result)`` to decide; with no such
        signal the edge does NOT fire (the conservative default — an ordinary K),
        keeping the no-DB path deterministic.
        """
        if result.pitch_outcome != "swinging_strike":
            return False
        first_base_open = state.bases.first is None
        two_outs = state.outs >= OUTS_PER_INNING - 1
        if not (first_base_open or two_outs):
            return False
        hook = getattr(self.resolver, "dropped_third_strike", None)
        if hook is None:
            return False
        return bool(hook(state, result))

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
        b.assert_consistent()
        if state.batter_id is not None:
            result.baserunner_advances[state.batter_id] = 1
        return forced_run

    #: Out events that a sac-fly intent may convert into a ``sacrifice_fly``: a
    #: plain fly/air out (NOT a hit, NOT a strikeout, NOT a grounder/DP, NOT an
    #: already-productive out).  Kept conservative so the bias only nudges the
    #: genuinely-ambiguous deep-fly-out into the productive-out it already is.
    _SAC_FLY_ELIGIBLE_OUTS: frozenset[str] = frozenset(
        {
            "field_out",
            "fly_out",
            "flyout",
            "out",
            "air_out",
            "sac_fly",
            "sacrifice_fly",
        }
    )

    def _apply_sac_fly_bias(self, state: GameState, sig: FieldingSignal) -> FieldingSignal:
        """Bias a sampled fly-ball OUT toward a ``sacrifice_fly`` (SIM-349).

        A no-op unless ALL hold: the manager flagged ``sac_fly_intent`` for this
        PA; a runner is on 3rd; fewer than 2 outs; and the sampled fielding signal
        is a single-out fly/air ball (a member of
        :data:`_SAC_FLY_ELIGIBLE_OUTS`, ``result_hits == 0``,
        ``result_outs == 1``).  In that case it returns a NEW
        :class:`FieldingSignal` with the canonical ``sacrifice_fly`` event and
        ``result_runs == 1`` (the runner from 3rd scores) and removes that runner
        from 3B so the base state stays legal; the single out is unchanged.  The
        downstream ``resolve_runs`` then credits the productive out exactly as the
        SIM-312 sac-fly policy does.

        Returns ``sig`` unchanged on the (default) no-intent path or when the
        sampled ball is anything other than a plain fly-out, so the run
        environment is untouched outside the genuine sac-fly opportunity.
        """
        if not state.manager.sac_fly_intent:
            return sig
        if state.bases.third is None or int(state.outs) >= 2:
            return sig
        if int(sig.result_hits) != 0 or int(sig.result_outs) != 1:
            return sig
        if str(sig.event) not in self._SAC_FLY_ELIGIBLE_OUTS:
            return sig
        # The runner on 3rd scores on the sac fly -> clear 3B so occupancy stays
        # consistent (the run itself is committed via resolve_runs by the caller).
        state.bases.third = None
        state.bases.assert_consistent()
        # Consume the per-PA intent so it never re-fires on a later pitch/PA.
        state.manager.sac_fly_intent = False
        # ``result_runs`` is at least 1 (the runner from 3rd); honour a richer
        # sample if it already scored more.
        return FieldingSignal(
            event="sacrifice_fly",
            result_hits=0,
            result_outs=1,
            result_runs=max(1, int(sig.result_runs)),
            fielder_id=sig.fielder_id,
            is_error=sig.is_error,
            exit_velo=sig.exit_velo,
            launch_angle=sig.launch_angle,
            spray_angle=sig.spray_angle,
        )

    def _resolve_in_play(self, state: GameState, result: PlayResult) -> None:
        """Resolve an in-play PA: batted-ball sample (step 5) -> fielding (step 6)
        -> baserunning (step 7), routing every run/base-out delta through
        ``resolve_runs`` (§8).

        Flow (when a sampler/resolver is wired):
          1. **Step 5** — sample the batted-ball event via the sampler.
          2. **Step 6** — the resolver maps it to a :class:`FieldingSignal`
             (event + sampled ``result_hits/outs/runs`` + fielder/error from the
             fielder RBF).  HR is the natural ``result_hits == 4`` case.
          3. **Step 7** — advance the runners on the bases per the hit value
             (:meth:`_advance_runners`), then commit the run/base-out delta via
             :meth:`_commit_run_delta` -> ``resolve_runs``.

        In count-machine-only mode (no sampler AND no resolver-supplied sample)
        the in-play outcome is left as an unresolved terminal (event=None) so the
        count machine can still be tested without FAISS.
        """
        result.pa_terminal = True
        # SIM-425: full-pool batted-ball draw (Batter+Situation over the whole
        # bat_hand batted-ball pool) when wired; else the per-tile sampler path.
        bb_sample: dict | None = None
        sig = self._full_pool_fielding(state)
        # SIM-425: did the batted ball come from the full-pool sampler?  Only then
        # do we model productive-out advancement here (the per-tile path keeps its
        # validated pool-supplied ``result_runs``).
        full_pool_sig = sig is not None
        if sig is None:
            if self._pa is not None:
                # --- Step 5: batted-ball sampling --------------------------
                bb_fp = self._pa._battedball_fingerprint(state)  # SIM-317
                bb_fp = self._jitter_query(bb_fp, _BB_FEATURE_SCALE)
                bb_sample = self._pa.sampler.sample_batted_ball(
                    state.bat_hand, state.season, bb_fp, k=self.k
                )
                result.battedball_sample = bb_sample
                result.fellback = result.fellback or bool(bb_sample.get("fellback", False))
            elif getattr(self.resolver, "_injected_battedball", None) is not None:
                # A resolver may carry an injected batted-ball sample (no-DB tests).
                bb_sample = getattr(self.resolver, "_injected_battedball", None)

            if bb_sample is None:
                # Count-machine-only mode: terminal in-play, resolution handed off.
                result.event = None
                return

            # --- Step 6: fielding resolution (fielder/catcher RBF signal) --
            sig = self.resolver.resolve_fielding(state, bb_sample)
        # --- SIM-349 sac-fly intent bias (productive-out nudge) ------------
        # When the manager flagged sac-fly intent for this PA and the sampled
        # batted ball is a fly-ball OUT with the runner still on 3rd and <2 outs,
        # nudge the resolved event to the canonical ``sacrifice_fly`` so the
        # SIM-312 run-resolution credits the runner from 3rd.  A bias on an
        # already-out fly ball only — it never converts a hit/grounder/K, never
        # adds an out, and is a no-op when intent is off (the default path).
        sig = self._apply_sac_fly_bias(state, sig)
        result.event = sig.event
        result.fielder_id = sig.fielder_id
        result.is_error = sig.is_error
        result.exit_velo = sig.exit_velo
        result.launch_angle = sig.launch_angle
        result.spray_angle = sig.spray_angle

        # --- Step 7: baserunner advancement on the bases -------------------
        # The batter's hit value sets how far runners advance; a HR scores
        # everyone, a triple pushes 3, etc.  An out advances no one by default
        # (sac/productive-out advancement is folded into the sampled result_runs).
        hit = int(sig.result_hits)
        if hit >= 1:
            runners_scored = self._advance_runners(
                state,
                result,
                batter_reached=hit,
                bases_advanced=hit,
            )
        elif full_pool_sig:
            # SIM-425: productive-out advancement on the full-pool path — a fly out
            # tags the runner home from 3rd (sac fly), a ground out advances forced
            # runners.  This is the bulk of the hits-are-right / runs-low gap: outs
            # previously moved no one.  Scored per the runner's own tag-up/advance
            # rate from the baserunner embedding.
            runners_scored = self._full_pool_out_advancement(state, result, sig)
        else:
            runners_scored = 0
            # An out may still record outs (incl. the batter) without moving the
            # surviving runners; leave the bases as-is for the conservative case.

        # Use the sampled result_runs when the pool carried it (context-aware);
        # otherwise fall back to the runners our advancement scored.  The SIM-349
        # sac-fly bias sets ``sig.result_runs`` on the converted productive out
        # (the runner from 3rd scores) when the sample itself carried none, so the
        # credited run is honoured without overriding a richer pool sample.
        sampled_runs = bb_sample.get("result_runs") if bb_sample is not None else None
        runs = int(sampled_runs) if sampled_runs is not None else int(runners_scored)
        runs = max(runs, int(sig.result_runs))

        # --- Step 8 hand-off: ONE resolve_runs call for runs + base-out ----
        self._commit_run_delta(
            state,
            result,
            event=sig.event,
            result_hits=hit,
            result_outs=int(sig.result_outs),
            result_runs=runs,
        )

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

        # Advance the batting-order pointer for the team that just batted.
        self._advance_batting_order(state)

        # SIM-317: the matchup is fixed WITHIN a PA but changes at the PA
        # boundary (new batter / pitcher), so drop the deriver's per-PA matchup
        # cache here.  No-op when no deriver is wired (count-machine-only mode).
        if self.fingerprint_deriver is not None:
            self.fingerprint_deriver.new_plate_appearance()

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
        count-machine-only in-play terminal with no resolver) we fall back to
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

        EARNED/UNEARNED SIMPLIFICATION (documented per the AC): full ER scoring
        requires reconstructing the inning "as if" errors never happened (which
        runners would have scored absent the error / extra outs).  We deliberately
        do NOT do that here.  Instead we use the per-play ``is_error`` flag as the
        split: a run that scores on a play flagged as an error is unearned; every
        other scored run is earned and charged to the current pitcher.  This is a
        conservative, deterministic, per-play approximation — it under-counts the
        rare "error earlier in the inning lets a later run score unearned" case,
        which a future ticket can refine with the inning-reconstruction rule.
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
            # RBI: runs the batter drove in (not credited on an error-driven run).
            if runs and not is_error:
                bat.rbi += runs

        # ---- runs scored (SIM-365): credit each runner who crossed home on this
        # play.  ``baserunner_advances`` records ``end_base == 0`` for a runner who
        # SCORED (set in :meth:`_advance_runners` when dest >= 4, and for the HR
        # batter).  Steals are credited entirely in :meth:`_resolve_steal_outcome`
        # (so a steal on a NON-terminal pitch — which never reaches this method —
        # is still counted, and a caught-stealing ``0`` is never mistaken for a
        # scored run); we therefore skip run attribution on a steal PA here to
        # avoid double-counting a steal-of-home.  Forced runs on a walk are not
        # recorded per-runner in ``baserunner_advances`` (only ``runs_scored``),
        # so a walk-forced run is a documented under-count for the per-runner R.
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
            # ER: scored runs charged to the pitcher unless flagged as error.
            if runs and not is_error:
                pit.er += runs
            # SIM-365: runs allowed (R) include UNEARNED runs too, so unlike ER
            # there is no ``is_error`` exclusion — R >= ER by construction.  A
            # steal-of-home on a NON-terminal pitch is charged in
            # :meth:`_resolve_steal_outcome` (it never reaches this method);
            # a terminal-pitch steal-of-home's run is already in ``runs`` here.
            if runs:
                pit.r_allowed += runs

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
            state.pitcher_id = pitcher
            state.throw_hand = state.throw_hands.get(pitcher, state.throw_hand)
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
             ``ManagerContext.green_light_rate``.  The resolver only stages a
             steal when the green-light is on.

        The steal *decision* still lives here (the resolver's
        :meth:`PlayResolver.resolve_steal` stages the :class:`StealResolution`);
        the *outcome* is committed in step 7 (:meth:`_resolve_steal_outcome`).  A
        test may instead call :meth:`stage_steal` directly.

        No-op-safe: with no manager profile wired (``self.manager is None``) every
        tendency reads 0.0, so no IBB / pitch-out fires and the green-light stays
        off — the resolver default (``attempted=False``) keeps the loop a no-op.
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

        # --- steal initiate wiring (SIM-319) -----------------------------------
        # Only auto-stage from the resolver if a test has not already staged one,
        # and only when the green-light is on (the manager allowed a steal).
        if self._pending_steal is not None:
            return
        if green <= 0.0:
            # SIM-426: no manager green-light (the production case — manager is
            # None).  Fall back to the runner-embedding-driven steal decision so
            # the full-pool path still attempts steals at ~MLB volume.
            self._full_pool_steal_decision(state)
            return
        # Gate the resolver consult on the green-light draw so the manager
        # tendency actually governs whether an attempt is even considered.
        if self._manager_rng() >= green:
            return
        steal = self.resolver.resolve_steal(state)
        if steal is not None and steal.attempted:
            self._pending_steal = steal

    def _should_issue_ibb(self, state: GameState, li: float) -> bool:
        """Decide an intentional walk (§3 item 2).

        Fires only in the canonical IBB spot — first base OPEN, a runner in
        scoring position, fewer than two outs preferred, a close & late game — and
        only when the manager is the type to manage matchups aggressively (proxy:
        ``platoon_advantage_exploitation_rate``) AND the leverage is high.  Never
        loads the bases by IBB unless it sets up a force in a tie/lead spot late
        (kept conservative: requires first base empty).
        """
        b = state.bases
        # First base must be OPEN (the defining IBB precondition).
        if b.first is not None:
            return False
        # A runner in scoring position to make the IBB worthwhile.
        if b.second is None and b.third is None:
            return False
        # Close game (within 1 run) and late (7th+) — the textbook IBB window.
        if abs(int(state.score_diff)) > 1 or int(state.inning) < _LATE_INNING:
            return False
        # High leverage required.
        if li < _HIGH_LEVERAGE:
            return False
        # Manager tendency: how much they manage matchups (0..1-ish).  Combine the
        # platoon-exploitation tendency with leverage into a fire probability.
        tend = self._tendency("platoon_advantage_exploitation_rate", 0.0)
        if tend <= 0.0:
            return False
        fire_p = min(1.0, tend * min(li / _HIGH_LEVERAGE, 2.0))
        return self._manager_rng() < fire_p

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
        safe: bool | None = None,
    ) -> None:
        """Stage a steal attempt for the NEXT pitch (the §3 decision).

        Resolves the safe/caught outcome from ``sim.stolen_base_pool`` when wired
        (a sampled historical result) unless ``safe`` is given explicitly (the
        deterministic test path).  The attempt is consumed + resolved in step 7
        of the next :meth:`step_pitch`.
        """
        if to_base is None:
            to_base = _NEXT_BASE.get(from_base, 4)
        if safe is None:
            safe = self._sample_steal_success(state=None)
        self._pending_steal = StealResolution(
            attempted=True,
            runner_id=runner_id,
            from_base=from_base,
            to_base=to_base,
            safe=bool(safe),
        )

    def _sample_steal_success(self, state) -> bool:
        """Draw a safe/caught outcome from ``sim.stolen_base_pool`` (§3 item 4).

        Uses the injected ``self.sim.stolen_base_pool`` accessor (a
        :class:`_SampledStealPool` or any object exposing ``sample(rng) -> bool``);
        with no pool wired the conservative default is ``False`` (caught), so a
        steal never silently succeeds without a sampled historical basis.
        """
        pool = getattr(self.sim, "stolen_base_pool", None) if self.sim is not None else None
        if pool is None:
            return False
        return bool(pool.sample(self.rng))

    #: SIM-426: scales a runner's raw ``sb_attempt_rate`` to a per-opportunity
    #: (first-pitch-of-PA) attempt probability.  Calibrated so the full-pool path
    #: produces ~MLB attempt volume (~0.7 attempts/game/team).
    _STEAL_ATTEMPT_K: float = 0.38

    def _full_pool_steal_decision(self, state: GameState) -> None:
        """SIM-426: stage a steal on the full-pool path from the RUNNER's own
        ``sb_attempt_rate`` / ``sb_success_rate`` (baserunner embedding), with NO
        dependence on a manager profile (which is absent in production).

        Fires at most once per PA (gated to the first pitch) for the lead stealable
        runner — a runner on 1B with 2B open (steal of 2nd) or on 2B with 3B open
        (steal of 3rd, at a lower base rate).  Attempts fall off in blowouts and
        with two outs.  The safe/caught outcome is the runner's ``sb_success_rate``;
        the existing step-7 :meth:`_resolve_steal_outcome` commits it."""
        fp = self.full_pool_sampler
        if fp is None or self._pending_steal is not None:
            return
        k = self._STEAL_ATTEMPT_K
        env_k = os.environ.get("SIM_STEAL_K")
        if env_k is not None:
            try:
                k = float(env_k)
            except ValueError:
                pass
        if k <= 0.0:
            return
        # One decision per PA: only at the fresh count (the hook fires every pitch).
        if int(state.balls) != 0 or int(state.strikes) != 0:
            return
        b = state.bases
        if b.first is not None and b.second is None:
            runner_id, from_base, base_scale = b.first, 1, 1.0
        elif b.second is not None and b.third is None:
            runner_id, from_base, base_scale = b.second, 2, 0.35  # steals of 3rd are rarer
        else:
            return
        if runner_id is None:
            return
        season = int(getattr(state, "season", 2024) or 2024)
        attempt = fp.runner_rate(f"{int(runner_id)}:{season}", "sb_attempt_rate")
        if attempt is None or attempt <= 0.0:
            return
        # Damp aggression in blowouts (running into outs is pointless when lopsided)
        # and with two outs (a CS ends the inning).
        blowout = 0.4 if abs(int(state.score_diff)) >= 5 else 1.0
        two_out = 0.6 if int(state.outs) >= 2 else 1.0
        attempt_p = min(0.9, attempt * k * base_scale * blowout * two_out)
        if float(self.rng.random()) >= attempt_p:
            return
        raw = fp.runner_rate(f"{int(runner_id)}:{season}", "sb_success_rate")
        # The embedding success-rate reflects historically chosen (favorable) spots;
        # haircut to the realized rate against an average-defense in-sim catcher so
        # the SB/CS split matches MLB (~0.78 SB%).  (A catcher-framing/arm factor is
        # the SIM-428 follow-on once catcher_id is threaded to the resolver.)
        success = 0.75 if raw is None else float(min(max(raw * 0.90, 0.0), 0.95))
        safe = float(self.rng.random()) < success
        self.stage_steal(runner_id=runner_id, from_base=from_base, safe=safe)

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
        self._maybe_sac_fly_intent(state, li)
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
        forced = pc >= _PULL_PITCH_CEILING
        if not forced and self._manager_rng() >= fire_p:
            return
        new_arm = self._pick_reliever(state, li)
        if new_arm is None:
            return  # bullpen empty -> degrade gracefully (stay with the starter).
        old = state.pitcher_id
        state.pitcher_id = int(new_arm)
        state.pitcher_pitch_count = 0  # fresh arm
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
        """Choose a reliever from the defending team's bullpen BY LEVERAGE.

        High-LI + late -> the **closer** (the first arm in the list, by
        convention the highest-leverage role); otherwise a middle reliever from
        the back of the list.  Pops the chosen arm so it cannot be reused.  Reads
        ``ManagerContext.bullpen_available`` keyed by the defending Team (or its
        int value).  Returns ``None`` when no arm is available.
        """
        pen_map = state.manager.bullpen_available or {}
        team = state.defense
        arms = pen_map.get(team)
        if arms is None:
            arms = pen_map.get(int(team))
        if not arms:
            return None
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

    def _maybe_sac_fly_intent(self, state: GameState, li: float) -> None:
        """Flag sac-fly intent for the NEXT PA (SIM-349 productive-out bias).

        With a **runner on 3rd** and **fewer than 2 outs** in a **run-needed
        spot** (tied or trailing — a run materially helps), the manager biases the
        batter toward the productive-out (a fly ball deep enough to score the
        runner from 3rd).  Gated by the manager's
        ``squeeze_play_rate_per_3b_opp`` tendency — the closest available
        manager-similarity AGGRESSION feature (idx 4, defined as "squeeze attempts
        / runner-on-3rd-<2-out", i.e. the same runner-on-3rd-<2-out opportunity a
        sac fly exploits; mapping documented here) — scaled by leverage vs a
        loop-rng draw.

        Sets ``ManagerContext.sac_fly_intent`` (a PA-level flag): when an in-play
        fly-ball PA resolves with the runner on 3rd, :meth:`_apply_sac_fly_bias`
        nudges a would-be deep fly-out toward the canonical ``sacrifice_fly``
        event so the SIM-312 run-resolution credits the run.  This is a
        **bias/flag**, not a forced outcome — the count machine and the batted-ball
        sample still play out; if the batter does something else (a hit, a K, a
        grounder), the intent simply has no effect.  Never mutates the base-out
        state here, so it cannot create an illegal state.

        No-op-safe: with no manager profile the tendency reads 0.0 and the flag
        stays off.
        """
        # Reset the prior PA's intent so a stale flag never carries forward.
        state.manager.sac_fly_intent = False
        b = state.bases
        # Runner on 3rd + fewer than 2 outs is the defining sac-fly opportunity.
        if b.third is None or int(state.outs) >= 2:
            return
        # Run-needed spot: tied or trailing (score_diff <= 0 for the offense).  A
        # team comfortably ahead does not give up an out for one run here.
        if int(state.score_diff) > 0:
            return
        tend = self._tendency("squeeze_play_rate_per_3b_opp", 0.0)
        if tend <= 0.0:
            return
        li_scale = 0.5 + 0.5 * min(li / _HIGH_LEVERAGE, 2.0)
        fire_p = min(1.0, tend * li_scale)
        if self._manager_rng() >= fire_p:
            return
        state.manager.sac_fly_intent = True
        self.manager_decisions.append(
            {
                "kind": "sac_fly_intent",
                "inning": int(state.inning),
                "leverage": li,
                "batter_id": state.batter_id,
                "runner_id": b.third,
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
    sampler: PlayPoolSampler | None = None,
    resolver: PlayResolver | None = None,
    sim=None,
    fingerprint_deriver=None,
    k: int = 25,
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
        machine's loop rng (steal / foul / draw decisions) AND the sampler's k-NN
        rng, so a fixed ``(game, seed)`` reproduces an identical game.

    Wiring: pass a fully-built ``state_machine`` (the production path), or let the
    driver build one from an injected ``sampler`` / ``resolver`` / ``sim`` (the
    no-DB unit-test path mirrors the SIM-319 injection pattern).  Likewise pass an
    ``initial_state`` or let the driver build a fresh "top of the 1st" GameState
    from ``pitcher_id`` / ``bat_hand`` / ``season`` / the two lineups.

    Returns a :class:`GameSimResult`.  ``state_machine`` is driven with a single
    deterministically-supplied pitch outcome ONLY when the machine has no sampler
    AND a caller supplies ``_outcome_for`` via a subclass; the normal no-DB test
    path injects a sampler-or-resolver so the machine samples outcomes itself.
    """
    # --- Build the machine (thread the seed through its loop rng) -------------
    if state_machine is None:
        rng = np.random.default_rng(seed)
        state_machine = StateMachine(
            sampler,
            k=k,
            rng=rng,
            resolver=resolver,
            sim=sim,
            fingerprint_deriver=fingerprint_deriver,
        )
    elif seed is not None:
        # Re-seed the supplied machine's loop rng so the per-game seed governs the
        # loop-level draws (steal / foul re-weight / outcome choice).
        state_machine.rng = np.random.default_rng(seed)

    # --- Thread the seed through the sampler's k-NN rng (§6.3) ---------------
    # The sampler holds its OWN injected Generator; re-seed it so the FAISS draws
    # are reproducible from the same per-game seed.  Guarded so a sampler-less
    # (resolver-driven) machine is untouched.
    if seed is not None:
        smp = getattr(state_machine, "sampler", None)
        if smp is not None and hasattr(smp, "rng"):
            smp.rng = np.random.default_rng(seed)
        pa = getattr(state_machine, "_pa", None)
        if (
            pa is not None
            and getattr(pa, "sampler", None) is not None
            and hasattr(pa.sampler, "rng")
        ):
            pa.sampler.rng = np.random.default_rng(seed)

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
        initial_state.bat_hand = initial_state.bat_hand_for(initial_state.batter_id)
    state = initial_state
    if seed is not None and state.seed is None:
        state.seed = seed

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
    # SIM-303 scaffold surface (kept for back-compat)
    "PitchState",
    "PlateAppearanceSimulator",
    "pitch_outcome_to_event",
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
    # SIM-318 count-conditional foul re-weight (step 4 / SIM-056)
    "STRIKES_BUCKET_FOUL_FACTOR",
    "strikes_bucket_foul_factor",
    "apply_count_foul_weighting",
    # SIM-319 fielding / baserunning / steal resolution (steps 6/7 + §5.4)
    "FieldingSignal",
    "StealResolution",
    "PlayResolver",
    "STEAL_SAFE",
    "STEAL_CAUGHT",
    # PA-event markers
    "EVENT_WALK",
    "EVENT_STRIKEOUT",
    "EVENT_IN_PROGRESS",
]
