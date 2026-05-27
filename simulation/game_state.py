"""simulation.game_state — the GameState / PlayResult contract (SIM-311).

WHAT THIS IS
============
This module owns the **dataclass contract** that the Phase-4 simulation loop is
built against.  It is the artifact the SIM-310 spec
(``docs/architecture/2026-06-17-phase4-sim-loop-spec.md`` §9) explicitly defers
to SIM-311:

    "SIM-311 owns the ``GameState`` (mutable count/out/base/score/inning/half/
     lineup/manager context) and ``PlayResult`` (pitch details, outcome type,
     batted-ball stats, fielding resolution, baserunner movements, runs, outs,
     next state) contract.  Every step's I/O in §2 is expressed against those
     types."

This module is the **contract only** — the mutable data shapes, their invariants,
and a handful of ergonomic mutators the SIM-316 state machine will lean on.  It
deliberately does NOT implement:

  * the count/out/base/inning **state machine** (SIM-316 — §5/§6 of the spec);
  * the **fingerprint derivation** that reads this state (SIM-317 — §4);
  * the **outcome determination** / foul re-weight (SIM-318 — §5.1/§5.2);
  * **fielding / baserunning** resolution (SIM-319 — steps 6/7);
  * **run resolution** (SIM-312 — already landed in
    ``simulation.run_resolution``; ``PlayResult`` carries its provenance);
  * the **manager** substitution/steal/IBB module (SIM-323 — the §3/§5.3 hooks);
  * the **invalid-state harness** (SIM-326 enforces invalid-state detection;
    validation here is intentionally lightweight, see ``GameState.assert_*``).

ALIGNMENT WITH THE SIM-310 SPEC (§2 step I/O)
=============================================
The 8 canonical steps read a ``GameState`` and emit a ``PlayResult``.  The field
sets below are chosen so each step's listed input/output has a home:

  * Step 1 (game-state read) — ``GameState`` exposes count, outs, inning/half,
    base state, ``score_diff`` (offense − defense), ``leverage`` (manager
    context), ``pitcher_pitch_count``, ``batter_pa_count``, ``park`` (venue).
  * Step 2/3 (pitch selection + sampling) — the sampler **pre-filter keys**
    ``(pitcher_id, bat_hand, season)`` are first-class on ``GameState`` (spec
    §4.3: these are tile pre-filter args, NOT fingerprint dims) via
    :meth:`GameState.sampler_prefilter`.
  * Step 4 (outcome determination) — ``balls`` / ``strikes`` are the live count
    the §5.1 machine advances.
  * Step 7/8 (baserunning + state update) — base occupancy + runner identities,
    home/away score, lineup pointers + per-team batting order, current batter /
    pitcher ids; the resolved run value + run-resolution provenance live on
    ``PlayResult`` (so the loop never re-derives runs inline — spec §8).

The bitmask base encoding (``Bases.runners_state``) is byte-compatible with
``simulation.run_resolution`` / ``derived.run_expectancy_matrix``::

    bit0 = 1B, bit1 = 2B, bit2 = 3B   (0=empty 1=1B 2=2B 3=1B+2B 4=3B 5=1B+3B
                                        6=2B+3B 7=loaded)

so a ``GameState`` can feed ``run_resolution.re24_value(outs, runners_state)``
directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

# ---------------------------------------------------------------------------
# Value types / enums
# ---------------------------------------------------------------------------

#: Outs that end a half-inning (mirrors run_resolution.OUTS_PER_INNING).
OUTS_PER_INNING = 3
#: Balls that terminate a PA as a walk (spec §5.1).
BALLS_FOR_WALK = 4
#: Strikes that terminate a PA as a strikeout (spec §5.1).
STRIKES_FOR_STRIKEOUT = 3

#: The closed pitch-outcome vocabulary the sampler emits (mirrors the SIM-301
#: ``outcome_type`` set and ``sim_loop.CONTACT_PITCH_OUTCOME``).
PITCH_OUTCOMES = ("ball", "called_strike", "swinging_strike", "foul", "in_play")
#: The single pitch outcome that means the ball was put in play (spec §5.1).
CONTACT_PITCH_OUTCOME = "in_play"

# Base bitmask helpers (shared encoding with run_resolution).
_BIT_1B = 0b001
_BIT_2B = 0b010
_BIT_3B = 0b100


class Half(IntEnum):
    """Which half of the inning is being played (spec §2 'inning/half')."""

    TOP = 0  # away team bats
    BOTTOM = 1  # home team bats


class Team(IntEnum):
    """The two sides.  ``AWAY`` bats in the top, ``HOME`` in the bottom."""

    AWAY = 0
    HOME = 1


@dataclass
class Bases:
    """Base-occupancy state with optional runner identities (SIM-311).

    The three slots carry the **runner's player id** (an int) or ``None`` when
    the base is empty.  This is the richer representation the spec §7 baserunner
    step needs (per-runner advancement); the cheap bitmask summary used by RE24
    (``simulation.run_resolution``) is derived on demand via
    :attr:`runners_state` so a ``GameState`` plugs straight into the SIM-312
    run-resolution policy.

    Encoding of :attr:`runners_state` matches run_resolution / the DB:
    ``bit0=1B, bit1=2B, bit2=3B``.
    """

    first: int | None = None  # runner id on 1B, or None
    second: int | None = None  # runner id on 2B, or None
    third: int | None = None  # runner id on 3B, or None

    @property
    def runners_state(self) -> int:
        """The 3-bit base-occupancy bitmask (run_resolution / RE24 encoding)."""
        rs = 0
        if self.first is not None:
            rs |= _BIT_1B
        if self.second is not None:
            rs |= _BIT_2B
        if self.third is not None:
            rs |= _BIT_3B
        return rs

    @property
    def occupancy(self) -> tuple[bool, bool, bool]:
        """``(on_1B, on_2B, on_3B)`` bools — the scaffold ``PitchState.bases``
        shape, generalized.  Convenience for callers that only need occupancy."""
        return (self.first is not None, self.second is not None, self.third is not None)

    @property
    def count_on_base(self) -> int:
        """How many runners are currently on base (0-3)."""
        return sum(self.occupancy)

    def clear(self) -> None:
        """Empty all bases (half-inning transition, spec §6.1)."""
        self.first = self.second = self.third = None

    def assert_consistent(self) -> None:
        """Lightweight invariant: each occupied base holds a non-negative id.

        (Per-runner advancement legality is SIM-319; invalid-state *detection*
        is SIM-326's harness — this only guards against obviously-corrupt ids.)
        """
        for base, rid in (("1B", self.first), ("2B", self.second), ("3B", self.third)):
            if rid is not None and int(rid) < 0:
                raise ValueError(f"Bases.{base} has a negative runner id: {rid!r}")


@dataclass
class ManagerContext:
    """Manager / leverage context hook (spec §3 pre-pitch + §5.3 end-of-PA hooks).

    This is the typed home for the manager-decision inputs the SIM-310 spec
    references but does NOT design — steal green-light, bullpen-by-leverage,
    pinch decisions.  The substitution/steal/IBB *logic* is **SIM-323**; this
    struct only fixes WHERE that context lives on the state so downstream
    tickets compile against stable field names.

    # TODO (SIM-323): the manager RBF engine populates green_light_rate,
    # bullpen availability, and the sub triggers; the fields here are the
    # contract surface those decisions read/write.
    """

    #: High-leverage index (spec step 1 reads 'leverage'); 1.0 == average LI.
    leverage: float = 1.0
    #: Manager steal green-light tendency in [0, 1] (spec §3 item 4).  SIM-323.
    green_light_rate: float = 0.0
    #: Available bullpen pitcher ids per defending team (spec §3 item 1). SIM-323.
    bullpen_available: dict[int, list[int]] = field(default_factory=dict)
    #: Whether an intentional walk is signalled for THIS PA (spec §3 item 2).
    intentional_walk_signalled: bool = False
    #: Whether a pitch-out is signalled for THIS pitch (spec §3 item 3).
    pitch_out_signalled: bool = False
    #: SIM-349 hit-and-run: a pre-pitch signal that the runner(s) break with the
    #: pitch and the batter is in a contact-oriented PA (runner-go + contact bias
    #: for THIS pitch).  Reset each pitch in the pre-pitch hook.
    hit_and_run_signalled: bool = False
    #: SIM-349 sac-fly intent: a PA-level flag (set at the PA boundary) that, with
    #: a runner on 3rd and <2 outs in a run-needed spot, biases the in-play
    #: resolution toward the productive-out (sacrifice-fly) behaviour so the
    #: SIM-312 run-resolution credits the run.  A bias/flag only — never a forced
    #: outcome.  Consumed (cleared) when the PA resolves.
    sac_fly_intent: bool = False


# ---------------------------------------------------------------------------
# GameState — the mutable per-game state (spec §9 / step 1 read · step 8 write)
# ---------------------------------------------------------------------------


@dataclass
class GameState:
    """The mutable game state the simulation loop reads (step 1) and commits to
    (step 8).  Owned by SIM-311; built against the SIM-310 spec §2 step I/O.

    Required identity fields (the sampler pre-filter, spec §4.3):
      * ``pitcher_id`` — current pitcher (tile pre-filter key).
      * ``bat_hand``   — the batter's hand *for this PA* (switch-hitter's hand vs
        the current pitcher — spec §4.3 / HANDOFF §4 — NOT the roster ``bats``).
      * ``season``     — tile pre-filter key.

    Everything else defaults so the SIM-316 state machine can construct a fresh
    "top of the 1st, nobody on, 0-0 count, 0 outs" state and mutate from there.
    """

    # ---- sampler pre-filter (spec §4.3 — tile keys, NOT fingerprint dims) ----
    pitcher_id: int
    bat_hand: str  # 'L' / 'R' — batter's hand vs the current pitcher
    season: int

    # ---- count (spec step 4 / §5.1 count machine) ----------------------------
    balls: int = 0
    strikes: int = 0

    # ---- outs (spec §6.1 half-inning) ---------------------------------------
    outs: int = 0

    # ---- base state + runner identities (spec step 7) -----------------------
    bases: Bases = field(default_factory=Bases)

    # ---- inning + half (spec step 1 'inning/half') --------------------------
    inning: int = 1
    half: Half = Half.TOP

    # ---- score (spec step 1 'score diff' / step 8 commit) -------------------
    home_score: int = 0
    away_score: int = 0

    # ---- lineup / batting order (spec §6.1 'batting-order pointer per side') -
    # Per-team batting orders (lists of batter ids) and the current slot pointer
    # for each team; the pointer carries across half-innings (spec §6.1).
    away_lineup: list[int] = field(default_factory=list)
    home_lineup: list[int] = field(default_factory=list)
    away_lineup_slot: int = 0
    home_lineup_slot: int = 0

    # SIM-421: per-batter handedness + per-team starters so the matchup
    # pre-filter follows the lineup.  ``bat_hands`` maps batter_id -> 'L'/'R'/'S'
    # ('S' resolves vs the pitcher's throwing hand); the two pitcher ids let the
    # half-inning roll swap the pitcher to the fielding team's starter.  Empty by
    # default -> the count-machine test path keeps its fixed ``bat_hand`` (no-op).
    bat_hands: dict[int, str] = field(default_factory=dict)
    throw_hands: dict[int, str] = field(default_factory=dict)
    home_pitcher_id: int | None = None
    away_pitcher_id: int | None = None

    # ---- current matchup ids (spec step 2 matchup) --------------------------
    batter_id: int | None = None  # current batter; None until lineup set
    throw_hand: str | None = None  # current pitcher's throwing hand 'L'/'R'

    # ---- per-game counters the spec step-1 read surfaces --------------------
    pitcher_pitch_count: int = 0  # current pitcher's pitch count (fatigue/sub)
    batter_pa_count: int = 0  # PAs the current batter has had (3rd-time-thru)
    park: str | None = None  # venue id for park/venue context (spec step 1/6)

    # ---- manager / leverage context hook (spec §3 / §5.3; SIM-323) ----------
    manager: ManagerContext = field(default_factory=ManagerContext)

    # ---- determinism (spec §6.3) --------------------------------------------
    #: Per-game RNG seed threaded through sampler + loop draws (spec §6.3).
    seed: int | None = None

    # ====================================================================
    # Derived reads (spec step 1 'situation context')
    # ====================================================================

    @property
    def offense(self) -> Team:
        """The batting team (AWAY in the top, HOME in the bottom)."""
        return Team.AWAY if self.half == Half.TOP else Team.HOME

    @property
    def defense(self) -> Team:
        """The fielding team."""
        return Team.HOME if self.half == Half.TOP else Team.AWAY

    @property
    def score_diff(self) -> int:
        """Offense score minus defense score (spec step 1 'score diff').

        Positive == the batting team leads.
        """
        if self.offense == Team.HOME:
            return self.home_score - self.away_score
        return self.away_score - self.home_score

    @property
    def runners_state(self) -> int:
        """The base-occupancy bitmask (RE24 / run_resolution encoding)."""
        return self.bases.runners_state

    def sampler_prefilter(self) -> tuple[int, str, int]:
        """The ``(pitcher_id, bat_hand, season)`` tuple the sampler pre-filters
        on (spec §4.3 / step 3).  These are tile keys, NOT fingerprint dims."""
        return (self.pitcher_id, self.bat_hand, self.season)

    def bat_hand_for(self, batter_id: int | None) -> str:
        """The batter's hand for the sampler pre-filter (SIM-421).

        Resolves a switch hitter ('S') against the current pitcher's throwing
        hand (the platoon side: a switch hitter bats opposite the pitcher).  When
        the batter's hand is unknown (the count-machine test path, which sets no
        ``bat_hands`` map) the current ``bat_hand`` is kept unchanged."""
        hand = self.bat_hands.get(batter_id) if batter_id is not None else None
        if hand is None:
            return self.bat_hand
        if hand == "S":
            return "L" if (self.throw_hand or "R") == "R" else "R"
        return hand

    # ====================================================================
    # Ergonomic mutators for the SIM-316 state machine.
    # (Validation is intentionally lightweight; SIM-326 owns the harness.)
    # ====================================================================

    def reset_count(self) -> None:
        """Zero the ball/strike count (new PA, spec §5.3 / §6.1)."""
        self.balls = 0
        self.strikes = 0

    def reset_outs(self) -> None:
        """Zero outs (half-inning transition, spec §6.1)."""
        self.outs = 0

    def add_ball(self) -> None:
        """Increment the ball count (spec §5.1)."""
        self.balls += 1

    def add_strike(self) -> None:
        """Increment the strike count (spec §5.1)."""
        self.strikes += 1

    def record_out(self, n: int = 1) -> None:
        """Record ``n`` outs (spec step 7/8)."""
        if n < 0:
            raise ValueError("record_out(n) requires n >= 0.")
        self.outs += int(n)

    def add_runs(self, runs: int, *, to: Team | None = None) -> None:
        """Credit ``runs`` to a team (defaults to the current offense, spec
        step 8).  Run *value* (RE24/linear weight) is computed by SIM-312's
        ``run_resolution`` and lives on ``PlayResult.runs``; this commits the
        integer runs that physically scored."""
        if runs < 0:
            raise ValueError("add_runs(runs) requires runs >= 0.")
        team = to if to is not None else self.offense
        if team == Team.HOME:
            self.home_score += int(runs)
        else:
            self.away_score += int(runs)

    def is_half_inning_over(self) -> bool:
        """True once ``outs`` reaches 3 (spec §6.1)."""
        return self.outs >= OUTS_PER_INNING

    # ---- lightweight invariant guards (SIM-326 owns the real harness) -------

    def assert_count_valid(self, *, mid_count: bool = True) -> None:
        """Lightweight count invariants.

        ``mid_count=True`` (default) asserts the *live* count is mid-PA, i.e.
        ``0 <= balls <= 3`` and ``0 <= strikes <= 2`` — the count never sits at
        a terminal value because step 4 ends the PA the instant ball 4 / strike
        3 lands (spec §5.1).  ``mid_count=False`` allows the transient terminal
        value a caller may inspect *before* the PA is rolled over.
        """
        if self.balls < 0 or self.strikes < 0:
            raise ValueError(f"count cannot be negative: {self.balls}-{self.strikes}")
        if mid_count:
            if self.balls > BALLS_FOR_WALK - 1:
                raise ValueError(
                    f"balls={self.balls} is terminal (>=4 == walk); the PA should "
                    "have ended at step 4 (spec §5.1)."
                )
            if self.strikes > STRIKES_FOR_STRIKEOUT - 1:
                raise ValueError(
                    f"strikes={self.strikes} is terminal (>=3 == strikeout); the "
                    "PA should have ended at step 4 (spec §5.1)."
                )

    def assert_outs_valid(self, *, in_play: bool = True) -> None:
        """Lightweight outs invariant.

        ``in_play=True`` (default) asserts ``0 <= outs <= 2`` — during live play
        the third out ends the half-inning immediately (spec §6.1), so a
        committed in-play state never sits at 3.  ``in_play=False`` allows the
        transient ``outs == 3`` a caller inspects before the half-inning rolls.
        """
        if self.outs < 0:
            raise ValueError(f"outs cannot be negative: {self.outs}")
        ceiling = OUTS_PER_INNING - 1 if in_play else OUTS_PER_INNING
        if self.outs > ceiling:
            raise ValueError(
                f"outs={self.outs} exceeds the in-play ceiling {ceiling} "
                "(half-inning should have ended, spec §6.1)."
            )

    def assert_score_valid(self) -> None:
        """Scores are non-negative (spec step 8 commit invariant)."""
        if self.home_score < 0 or self.away_score < 0:
            raise ValueError(
                f"scores must be non-negative: home={self.home_score} away={self.away_score}"
            )

    def assert_invariants(self, *, in_play: bool = True) -> None:
        """Run all the lightweight committed-state invariants together.

        ``in_play`` is threaded to the count/outs guards: a *committed*,
        ready-for-the-next-pitch state is in-play (count mid-PA, outs <= 2);
        pass ``in_play=False`` to inspect a transient terminal snapshot.
        """
        self.assert_count_valid(mid_count=in_play)
        self.assert_outs_valid(in_play=in_play)
        self.assert_score_valid()
        self.bases.assert_consistent()


# ---------------------------------------------------------------------------
# PlayResult — the structured result of one pitch / one PA event (spec §9)
# ---------------------------------------------------------------------------


@dataclass
class PlayResult:
    """The structured result of one pitch (and, on contact, the resolved PA
    event).  Owned by SIM-311; generalizes the scaffold's ``simulate_pitch``
    return dict (``simulation/sim_loop.py``) into a typed contract.

    The scaffold dict had::

        {"pitch_outcome", "is_contact", "event", "runs", "fellback",
         "pitch_sample", "battedball_sample"}

    Every one of those has a typed home below, plus the spec §2 step-5/6/7
    deltas (batted-ball stats, fielding resolution, baserunner movements,
    runs/outs deltas, next-state pointer) and the SIM-312 run-resolution
    provenance so the loop never re-derives run values inline (spec §8).
    """

    # ---- step 3/4: the pitch outcome + count classification -----------------
    pitch_outcome: str  # ball/called_strike/.../in_play
    is_contact: bool = False  # True == ball put in play (step 5)
    #: True once step 4 declares the PA terminal (walk/K/in-play resolution).
    pa_terminal: bool = False

    # ---- the resolved PA event (step 4 terminal classification / step 5-7) --
    #: e.g. None (non-terminal pitch), "walk", "strikeout", "single",
    #: "field_out", "home_run", "ground_into_double_play" ... (spec §5.1).
    event: str | None = None

    # ---- step 8: run value + SIM-312 run-resolution provenance --------------
    #: The resolved run *value* of the play (RE24 or linear-weight). Computed by
    #: ``simulation.run_resolution.resolve_runs`` (SIM-312) — the loop must NOT
    #: re-derive this inline (spec §8).
    runs: float = 0.0
    #: How ``runs`` was resolved: "re24_delta" (primary) / "linear_weight"
    #: (fallback) — mirrors ``run_resolution.RunResolution.method``.
    run_resolution_method: str | None = None
    #: The canonical RUN_VALUES key the event resolved to (SIM-312), if any.
    canonical_event: str | None = None
    #: Base-out run expectancy before/after the play (SIM-312 RE24 provenance).
    re_start: float | None = None
    re_end: float | None = None
    #: Integer runs that physically scored on the play (committed to the score).
    runs_scored: int = 0

    # ---- step 5: batted-ball stats (placeholders, typed; SIM-319 fills) ------
    exit_velo: float | None = None
    launch_angle: float | None = None
    spray_angle: float | None = None

    # ---- step 6: fielding resolution (placeholders, typed; SIM-319) ---------
    fielder_id: int | None = None  # the fielder who handled the ball
    is_error: bool = False  # error flag (fielder/catcher RBF)
    #: Outs recorded by this play (the step-7 baserunning/fielding delta).
    outs_recorded: int = 0

    # ---- step 7: baserunner movement (placeholders, typed; SIM-319) ---------
    #: Per-runner advancement as ``{runner_id: end_base}`` where end_base is
    #: 0=scored/out-resolved-elsewhere, 1/2/3 = the base reached.  Empty until
    #: SIM-319 resolves advancement.
    baserunner_advances: dict[int, int] = field(default_factory=dict)
    #: Whether a steal was attempted on this pitch (spec §3 item 4 / step 7).
    steal_attempted: bool = False
    #: Steal outcome: None / "safe" / "caught" / "pickoff" (SIM-319).
    steal_outcome: str | None = None

    # ---- raw sampler payloads (carried verbatim from the sampler) -----------
    #: The raw ``PlayPoolSampler.sample_pitch(...)`` dict (row_id/distance/...).
    pitch_sample: dict | None = None
    #: The raw ``sample_batted_ball(...)`` dict, or None on a non-contact pitch.
    battedball_sample: dict | None = None
    #: True when any served tile fell back to the pitcher_id=0 league-average
    #: tile (mirrors the scaffold's ``fellback`` flag).
    fellback: bool = False

    # ---- step 8: next-state pointer (the committed GameState) ----------------
    #: The ``GameState`` after this play is committed (spec step 8 'next state').
    #: Optional so a PlayResult can be constructed before the commit.
    next_state: GameState | None = None

    def as_scaffold_dict(self) -> dict[str, Any]:
        """Return the legacy scaffold ``simulate_pitch`` dict shape.

        Lets SIM-316 keep the scaffold's dict-shaped callers working while the
        loop migrates to the typed ``PlayResult`` (the scaffold itself is left
        untouched per SIM-311 scope).
        """
        return {
            "pitch_outcome": self.pitch_outcome,
            "is_contact": self.is_contact,
            "event": self.event,
            "runs": self.runs,
            "fellback": self.fellback,
            "pitch_sample": self.pitch_sample,
            "battedball_sample": self.battedball_sample,
        }


__all__ = [
    # enums / value types
    "Half",
    "Team",
    "Bases",
    "ManagerContext",
    # the contract
    "GameState",
    "PlayResult",
    # constants
    "OUTS_PER_INNING",
    "BALLS_FOR_WALK",
    "STRIKES_FOR_STRIKEOUT",
    "PITCH_OUTCOMES",
    "CONTACT_PITCH_OUTCOME",
]
