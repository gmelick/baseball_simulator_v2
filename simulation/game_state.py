"""The game state and the play result: the data the simulation loop reads and writes.

:class:`GameState` is the mutable state of one simulated game: the count, the
outs, the runners on base, the inning and half, the score, both batting orders
and their slot pointers, the live matchup, both defenses, the managers and
pens, and the per-pitcher counters. :class:`PlayResult` is the result of one
pitch: the pitch outcome, the plate-appearance event when the pitch ends one,
the run value, the batted ball, the fielding, the runners' movements and the
running-game outcomes.

This module holds the data shapes, their invariant guards and a few small
mutators. The loop that drives them lives in ``simulation.sim_loop`` and
``simulation.loop``; the run value comes from ``simulation.run_resolution``.
``docs/technical/sim-loop-cheat-sheet.md`` describes the loop on one page.

The count, outs and score guards are light. The base-state guards are not:
:meth:`Bases.assert_consistent` and :meth:`Bases.assert_transition` check real
invariants, because the loop relies on them as correctness checks.

The base bitmask (``Bases.runners_state``) uses the encoding of
``simulation.run_resolution`` and ``derived.run_expectancy_matrix``::

    bit0 = 1B, bit1 = 2B, bit2 = 3B   (0=empty 1=1B 2=2B 3=1B+2B 4=3B 5=1B+3B
                                        6=2B+3B 7=loaded)

so a ``GameState`` feeds ``run_resolution.re24_value(outs, runners_state)``
directly. RE24 is the run expectancy of each of the 24 base-out states: the
average runs scored from that state to the end of the half-inning.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import IntEnum

# ---------------------------------------------------------------------------
# Value types / enums
# ---------------------------------------------------------------------------

#: Outs that end a half-inning (equal to run_resolution.OUTS_PER_INNING).
OUTS_PER_INNING = 3
#: Bases a runner can stand on.  A legal base state holds at most this many
#: runners, and each of them is a different player.
MAX_RUNNERS_ON_BASE = 3
#: Balls that end a plate appearance as a walk.
BALLS_FOR_WALK = 4
#: Strikes that end a plate appearance as a strikeout.
STRIKES_FOR_STRIKEOUT = 3

#: The closed set of pitch outcomes the sampler emits: the pool's
#: ``outcome_type`` classes. ``hit_by_pitch`` is a class of its own. Folded into
#: ``ball``, every hit by pitch became ball four and a walk: the whole 2025 rate
#: of 0.397 a team-game, about 82% of the measured walk surplus (the
#: hit-by-pitch class, SIM-509).
PITCH_OUTCOMES = ("ball", "called_strike", "swinging_strike", "foul", "in_play", "hit_by_pitch")
#: The one pitch outcome that means the ball was put in play.
CONTACT_PITCH_OUTCOME = "in_play"
#: The ``pitch_outcome`` of a result on which no pitch was thrown: a pickoff
#: before the pitch made the third out and ended the half-inning. It is NOT one
#: of :data:`PITCH_OUTCOMES`: every reader that counts pitches, plate
#: appearances or count transitions must skip it. Read :attr:`PlayResult.no_pitch`.
NO_PITCH = "no_pitch"

# Base bitmask helpers (shared encoding with run_resolution).
_BIT_1B = 0b001
_BIT_2B = 0b010
_BIT_3B = 0b100


class Half(IntEnum):
    """Which half of the inning is in play."""

    TOP = 0  # away team bats
    BOTTOM = 1  # home team bats


class Team(IntEnum):
    """The two sides.  ``AWAY`` bats in the top, ``HOME`` in the bottom."""

    AWAY = 0
    HOME = 1


@dataclass
class Bases:
    """The runners on base, by player id.

    Each slot holds the **runner's player id** (an int), or ``None`` when the
    base is empty, so the advancement draws can move each runner by name.
    :attr:`runners_state` derives the 3-bit occupancy mask that the run
    expectancy lookup (``simulation.run_resolution``) reads.

    The encoding of :attr:`runners_state` matches run_resolution and the
    database: ``bit0=1B, bit1=2B, bit2=3B``.
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
        """``(on_1B, on_2B, on_3B)``: whether each base holds a runner."""
        return (self.first is not None, self.second is not None, self.third is not None)

    @property
    def count_on_base(self) -> int:
        """How many runners are currently on base (0-3)."""
        return sum(self.occupancy)

    def clear(self) -> None:
        """Empty all bases (at the end of a half-inning)."""
        self.first = self.second = self.third = None

    def runner_ids(self) -> tuple[int, ...]:
        """The ids of the runners on base, in base order.  Empty bags drop out."""
        return tuple(int(r) for r in (self.first, self.second, self.third) if r is not None)

    # ---- the invariant guards ------------------------------------------------

    @staticmethod
    def assert_runner_set_legal(
        runner_ids: Sequence[int | None],
        *,
        labels: Sequence[str] | None = None,
        where: str = "Bases",
    ) -> None:
        """Assert that ``runner_ids`` is a legal set of runners on base.

        :meth:`assert_consistent` runs this check over the three base slots.  A
        caller that holds a *list* of runner ids can call it directly, before it
        builds a :class:`Bases`.

        Three rules, all of them real:

          * **Every id is non-negative.**  A negative id is a corrupt id.
          * **No id repeats.**  One player cannot stand on two bases at once.
            This is the rule that catches a runner who is still on a base he was
            doubled off, because the fielding code re-places him while the pool
            transition also places the batter on that bag.
          * **At most** :data:`MAX_RUNNERS_ON_BASE` **runners.**  Three bases
            hold three runners.

        ``labels`` names each position for the error message (``("1B", "2B",
        "3B")`` from :meth:`assert_consistent`).  ``None`` falls back to the
        index.  ``where`` names the caller in the message.

        Raises ``ValueError`` on the first rule a caller breaks.
        """
        ids = list(runner_ids)
        names = list(labels) if labels is not None else []
        # PAD the labels, never truncate the ids. A zip that stopped at the
        # shorter list would drop every id past the last label before any rule
        # ran, so the guard would check nothing on those runners. Labels serve
        # the error message only; they never decide which runners get checked.
        if len(names) < len(ids):
            names += [f"[{i}]" for i in range(len(names), len(ids))]
        occupied = [(name, rid) for name, rid in zip(names, ids, strict=True) if rid is not None]
        if len(occupied) > MAX_RUNNERS_ON_BASE:
            raise ValueError(
                f"{where} holds {len(occupied)} runners "
                f"({[rid for _, rid in occupied]!r}); at most "
                f"{MAX_RUNNERS_ON_BASE} runners can stand on {MAX_RUNNERS_ON_BASE} bases."
            )
        seen: dict[int, str] = {}
        for name, raw in occupied:
            rid = int(raw)
            if rid < 0:
                raise ValueError(f"{where}.{name} has a negative runner id: {raw!r}")
            first_seen = seen.get(rid)
            if first_seen is not None:
                raise ValueError(
                    f"{where}: runner {rid} stands on both {first_seen} and {name}; "
                    "one player cannot occupy two bases."
                )
            seen[rid] = name

    def assert_consistent(self, *, batter_id: int | None = None) -> None:
        """Assert the base state itself is legal.

        The guard checks three invariants over the three bags — non-negative
        ids, no duplicate id, at most :data:`MAX_RUNNERS_ON_BASE` runners — and
        one optional invariant about the batter.

        Pass ``batter_id`` to also assert **the batter is not already on base**.
        The batter stands at the plate, so he is not a runner during his own
        plate appearance.  A caller that finds him on a bag has placed the same
        player twice.  The argument is optional because the short lineups the
        count-machine tests use repeat a player id across slots; only a caller
        with a real lineup passes it.

        Raises ``ValueError`` naming the base and the runner.

        This guard answers "is this base state possible", not "did the runners
        get there legally".  :meth:`assert_transition` answers the second
        question.
        """
        self.assert_runner_set_legal(
            (self.first, self.second, self.third),
            labels=("1B", "2B", "3B"),
            where="Bases",
        )
        if batter_id is not None:
            bid = int(batter_id)
            for base, rid in (("1B", self.first), ("2B", self.second), ("3B", self.third)):
                if rid is not None and int(rid) == bid:
                    raise ValueError(
                        f"Bases.{base} holds runner {bid}, who is also the batter; "
                        "the batter is not a runner during his own plate appearance."
                    )

    def assert_transition(
        self,
        post: Bases,
        *,
        batter_reached: bool | int,
        runners_scored: int,
        runners_retired: int,
        batter_id: int | None = None,
    ) -> None:
        """Assert one play conserves the bodies on the field.

        ``self`` is the base state **before** the play.  ``post`` is the base
        state **after** it.  The identity::

            runners_before + batter_reached
                == runners_after + runners_scored + runners_retired

        cannot *derive* the after-state: a derivation has no term for a runner
        retired on the play, so a double play breaks it.  It can *check* one,
        because the caller supplies the retired count instead of guessing it.

        **How the batter enters the identity.**  The batter is not on base
        before the play, so he never counts in ``runners_before``.  He enters on
        the left side as ``batter_reached``, which is 1 exactly when the batter
        **becomes a baserunner** on this play, and 0 otherwise.  Three
        consequences, and they are the whole design:

          * A batter retired at first base — a strikeout, a fly out, the batter
            half of a ground-ball double play — never becomes a runner.  He is
            ``batter_reached=0`` and he is **not** counted in
            ``runners_retired``.  ``runners_retired`` counts *baserunners*
            removed by the fielders, not outs.  An out on the batter is an out;
            it moves no body off a base.
          * A home run is ``batter_reached=1`` and the batter is also one of
            ``runners_scored``.  He reaches and scores on the same play, so he
            appears once on each side and the identity balances.
          * A batter who reaches on an error or a fielder's choice and is then
            retired later in the same play is ``batter_reached=1`` **and** one
            of ``runners_retired``.

        So ``runners_retired`` and ``runners_scored`` both count bodies that
        left the bases, and ``batter_reached`` counts the one body that can join
        them.  Outs recorded on the play are deliberately not an argument: the
        out ledger and the body ledger are different books, and mixing them
        breaks the identity.

        Pass ``batter_id`` to switch on two further checks: the batter is not on
        base in the pre-state, and every runner in the post-state is either a
        pre-state runner or the batter.  The second one catches an
        identity-swap — a runner retired and a different runner invented — which
        the count identity alone cannot see.

        Raises ``ValueError`` reporting every operand, so the failure names the
        play that broke it.
        """
        reached = int(batter_reached)
        scored = int(runners_scored)
        retired = int(runners_retired)
        if reached not in (0, 1):
            raise ValueError(f"batter_reached must be 0 or 1, got {batter_reached!r}.")
        if scored < 0 or retired < 0:
            raise ValueError(
                f"runners_scored and runners_retired must be >= 0, got "
                f"scored={runners_scored!r} retired={runners_retired!r}."
            )

        self.assert_consistent(batter_id=batter_id)
        post.assert_consistent()

        before = self.count_on_base
        after = post.count_on_base
        if before + reached != after + scored + retired:
            raise ValueError(
                "base-state transition does not conserve runners: "
                f"before={before} + batter_reached={reached} "
                f"!= after={after} + scored={scored} + retired={retired} "
                f"(pre={self.runner_ids()!r} post={post.runner_ids()!r})."
            )

        if batter_id is None:
            return
        bid = int(batter_id)
        allowed = set(self.runner_ids()) | {bid}
        invented = [rid for rid in post.runner_ids() if rid not in allowed]
        if invented:
            raise ValueError(
                f"base-state transition invents runner(s) {invented!r} who were "
                f"neither on base before the play nor the batter "
                f"(pre={self.runner_ids()!r} post={post.runner_ids()!r} batter={bid})."
            )
        if not reached and bid in post.runner_ids():
            raise ValueError(
                f"batter {bid} stands on a base but batter_reached=0 (post={post.runner_ids()!r})."
            )


@dataclass
class ManagerContext:
    """The manager's context on the state: the leverage, the pens and the walk signal.

    The manager hooks of the loop (``simulation.loop.manager``) write these
    fields; the pitching change and the intentional walk read them.
    """

    #: The leverage index (LI) of the current spot: how much the spot can swing
    #: the win probability, 1.0 = average. The pre-pitch hook writes it.
    leverage: float = 1.0
    #: The available relievers per side, ``{Team int: [pitcher ids]}``. The
    #: pitching change pops the entering arm. The real pen is the arms the MLB
    #: box lists for the game, minus the rotation.
    bullpen_available: dict[int, list[int]] = field(default_factory=dict)
    #: True when the pre-pitch hook issues an intentional walk on this pitch.
    intentional_walk_signalled: bool = False


# ---------------------------------------------------------------------------
# GameState — the mutable per-game state
# ---------------------------------------------------------------------------


@dataclass
class GameState:
    """The mutable state of one simulated game; the loop reads it and commits each play.

    Three fields are required, the matchup keys of the pitch draw:
      * ``pitcher_id`` — the current pitcher.
      * ``bat_hand``   — the batter's hand *for this plate appearance* (a switch
        hitter's hand against the current pitcher, NOT the roster ``bats``).
      * ``season``     — the pool season key.

    Every other field has a default, so a caller can build a fresh "top of the
    1st, nobody on, 0-0 count, 0 outs" state and mutate it from there.
    """

    # ---- the matchup keys ----------------------------------------------------
    pitcher_id: int
    bat_hand: str  # 'L' / 'R' — batter's hand vs the current pitcher
    season: int

    # ---- the count -----------------------------------------------------------
    balls: int = 0
    strikes: int = 0

    # ---- outs ----------------------------------------------------------------
    outs: int = 0

    # ---- the bases and their runners -----------------------------------------
    bases: Bases = field(default_factory=Bases)

    # ---- inning and half -----------------------------------------------------
    inning: int = 1
    half: Half = Half.TOP

    # ---- score ---------------------------------------------------------------
    home_score: int = 0
    away_score: int = 0

    # ---- the batting orders --------------------------------------------------
    # Per-team batting orders (lists of batter ids) and the current slot pointer
    # for each team; the pointer carries across half-innings.
    away_lineup: list[int] = field(default_factory=list)
    home_lineup: list[int] = field(default_factory=list)
    away_lineup_slot: int = 0
    home_lineup_slot: int = 0

    # Each player's hands and each side's current pitcher, so the matchup
    # follows the lineup.  ``bat_hands`` maps a batter id to 'L', 'R' or 'S' (a
    # switch hitter, resolved against the pitcher's throwing hand).  At each
    # half-inning the loop puts the fielding side's pitcher on the mound from
    # ``home_pitcher_id`` / ``away_pitcher_id``; a pitching change overwrites
    # them.  Empty by default: the count-machine tests keep their fixed
    # ``bat_hand``.
    bat_hands: dict[int, str] = field(default_factory=dict)
    throw_hands: dict[int, str] = field(default_factory=dict)
    home_pitcher_id: int | None = None
    away_pitcher_id: int | None = None
    # Each team's catcher (defense slot 'C'). The steal and pickoff draws read
    # the fielding side's catcher, and the pitch draw keys the receiving factor
    # on him.
    home_catcher_id: int | None = None
    away_catcher_id: int | None = None
    # Each team's defense, ``{position name: player_id}``, keyed by the
    # canonical slot name ('P', 'C', '1B' .. 'RF'; the vocabulary of
    # ``build_team_defense_map`` and ``snapshots.DEFENSE_POSITIONS``). The
    # fielding draw's fielder factor reads the CURRENT defender at each
    # position. Empty by default: the fielder factor is then neutral.
    home_defense: dict[str, int] = field(default_factory=dict)
    away_defense: dict[str, int] = field(default_factory=dict)

    # ---- the current matchup -------------------------------------------------
    batter_id: int | None = None  # current batter; None until lineup set
    throw_hand: str | None = None  # current pitcher's throwing hand 'L'/'R'

    # ---- per-game counters and context ---------------------------------------
    pitcher_pitch_count: int = 0  # the current pitcher's pitch count
    batter_pa_count: int = 0  # reset to 0 at each new plate appearance; never incremented
    park: str | None = None  # the venue id as a digit string; the fence stage reads it
    # The venue's regressed run park factor for this game (``derived.park_factors``,
    # ``factor_type='R'``), centred near 1.0: above 1 a hitter's park, below 1 a
    # pitcher's park. ``simulation.sim_kwargs`` resolves it once per game and
    # writes it here; the worker has no database, so the state carries it as a
    # number. The fielding draw's park kernel reads it (``SIM_PARK_KERNEL_SIGMA``;
    # 0 = off). 1.0 is neutral. The API's state factory stamps
    # ``sim_kwargs.UNRESOLVED_PARK_FACTOR`` here until the lookup runs.
    park_run_factor: float = 1.0
    # The point-in-time cutoff for a backtest replay of this game, as YYYYMMDD,
    # or None for a live game. ``sim_kwargs.resolve_asof_ymd`` computes it: the
    # day BEFORE the game, so the simulator never copies the plays it predicts.
    # ``sim_kwargs_from_state`` passes it on as ``"_asof_ymd"``, a key only the
    # factory reads (not a ``simulate_game`` parameter).
    asof_ymd: int | None = None
    # The two STARTING pitchers (a pitching change never overwrites them,
    # unlike ``home_pitcher_id`` / ``away_pitcher_id``) and the plate
    # appearances completed in the current half-inning: the pitching-change
    # draw's ``is_starter`` and ``new_half`` inputs.
    home_starter_id: int | None = None
    away_starter_id: int | None = None
    half_pa_count: int = 0

    # ``pitcher_bf`` counts the batters faced per pitcher id; the loop adds one
    # at each completed plate appearance. The pitching-change draw and the pitch
    # draw's times-through-the-order input read it. ``pitcher_rest_days`` maps a
    # pitcher id to his days of rest. The kwargs builder carries it, but no draw
    # reads it: the reliever draw reads the rest from ``pitcher_recent_usage``.
    pitcher_bf: dict[int, int] = field(default_factory=dict)
    pitcher_rest_days: dict[int, float] = field(default_factory=dict)
    # The managers and the real pen:
    #   * each side's manager (``raw.games``): the pitching-change draw weights
    #     its pool rows by the live manager's usage similarity;
    #   * the pen arms' recent usage, ``pitcher_id -> (days of rest capped at
    #     five, pitched on either of the two days before, pitches over the three
    #     days before)``: the reliever draw's usage weights;
    #   * the pen's source: 'box' (the MLB API's per-game listing) or
    #     'synthetic' (the no-DB pen);
    #   * each side's manager tendency profile and the season's league means:
    #     the steal weight reads the batting side's rate over the league's.
    # All empty by default, so every consumer is neutral.
    home_manager_id: int | None = None
    away_manager_id: int | None = None
    pitcher_recent_usage: dict[int, tuple[int, int, int]] = field(default_factory=dict)
    bullpen_source: str | None = None
    home_manager_profile: dict[str, float] = field(default_factory=dict)
    away_manager_profile: dict[str, float] = field(default_factory=dict)
    manager_league_profile: dict[str, float] = field(default_factory=dict)
    # The pitch count per pitcher id. At each half-inning the loop
    # (``_set_half_matchup``) saves the outgoing pitcher's count here and
    # restores the incoming pitcher's own, so the two starters never share one
    # count. ``pitcher_pitch_count`` stays the current pitcher's live count.
    # The loop uses this map only with a manager wired.
    pitcher_pc: dict[int, int] = field(default_factory=dict)

    # ---- the manager's context -----------------------------------------------
    manager: ManagerContext = field(default_factory=ManagerContext)

    # ---- determinism ---------------------------------------------------------
    #: The per-game seed, recorded on the state by ``simulate_game``.
    seed: int | None = None

    # ====================================================================
    # Derived reads
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
        """Offense score minus defense score.

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
        """The ``(pitcher_id, bat_hand, season)`` matchup tuple."""
        return (self.pitcher_id, self.bat_hand, self.season)

    def bat_hand_for(self, batter_id: int | None) -> str:
        """The batter's hand for this plate appearance: the draws' hand filter.

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
    # Small mutators the loop uses. Their validation is light.
    # ====================================================================

    def reset_count(self) -> None:
        """Zero the ball/strike count (a new plate appearance)."""
        self.balls = 0
        self.strikes = 0

    def reset_outs(self) -> None:
        """Zero outs (a new half-inning)."""
        self.outs = 0

    def add_ball(self) -> None:
        """Increment the ball count."""
        self.balls += 1

    def add_strike(self) -> None:
        """Increment the strike count."""
        self.strikes += 1

    def record_out(self, n: int = 1) -> None:
        """Record ``n`` outs."""
        if n < 0:
            raise ValueError("record_out(n) requires n >= 0.")
        self.outs += int(n)

    def add_runs(self, runs: int, *, to: Team | None = None) -> None:
        """Credit ``runs`` to a team (the current offense by default).

        This commits the whole runs that scored.  The run *value* (the change
        in run expectancy) comes from ``simulation.run_resolution`` and lives
        on ``PlayResult.runs``."""
        if runs < 0:
            raise ValueError("add_runs(runs) requires runs >= 0.")
        team = to if to is not None else self.offense
        if team == Team.HOME:
            self.home_score += int(runs)
        else:
            self.away_score += int(runs)

    def is_half_inning_over(self) -> bool:
        """True once ``outs`` reaches 3."""
        return self.outs >= OUTS_PER_INNING

    # ---- invariant guards.  The count / outs / score checks below are
    # ---- light; the base-state guard they aggregate is not.

    def assert_count_valid(self, *, mid_count: bool = True) -> None:
        """Lightweight count invariants.

        ``mid_count=True`` (default) asserts the *live* count is mid-PA, i.e.
        ``0 <= balls <= 3`` and ``0 <= strikes <= 2`` — the count never sits at
        a terminal value, because the loop ends the PA the instant ball 4 or
        strike 3 lands.  ``mid_count=False`` allows the transient terminal
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
        the third out ends the half-inning immediately, so a committed in-play
        state never sits at 3.  ``in_play=False`` allows the
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
        """Assert both scores are non-negative."""
        if self.home_score < 0 or self.away_score < 0:
            raise ValueError(
                f"scores must be non-negative: home={self.home_score} away={self.away_score}"
            )

    def assert_invariants(self, *, in_play: bool = True) -> None:
        """Run every committed-state invariant together.

        ``in_play`` is threaded to the count/outs guards: a *committed*,
        ready-for-the-next-pitch state is in-play (count mid-PA, outs <= 2);
        pass ``in_play=False`` to inspect a transient terminal snapshot.

        The base guard runs without a ``batter_id``.  It therefore checks that
        the base state is possible, and it does not check that the batter is off
        the bases.  The reason is a real ordering constraint: the loop
        advances the batting order **before** it re-validates the state, so the
        due-up batter is already the next player.  A caller that holds the
        batter for THIS plate appearance calls ``bases.assert_consistent(
        batter_id=...)`` itself.
        """
        self.assert_count_valid(mid_count=in_play)
        self.assert_outs_valid(in_play=in_play)
        self.assert_score_valid()
        self.bases.assert_consistent()


# ---------------------------------------------------------------------------
# PlayResult — the result of one pitch
# ---------------------------------------------------------------------------


@dataclass
class PlayResult:
    """The result of one pitch, with the plate-appearance event when the pitch ends one.

    It carries the pitch outcome, the event, the run value and its provenance,
    the batted ball, the fielding, the runners' movements, the running-game
    outcomes and the committed next state.  The run value comes from
    ``simulation.run_resolution``, so no reader re-derives it.
    """

    # ---- the pitch outcome ---------------------------------------------------
    pitch_outcome: str  # one of PITCH_OUTCOMES, or NO_PITCH
    is_contact: bool = False  # True == ball put in play
    #: True when this pitch ends the plate appearance (a walk, a strikeout, a
    #: ball in play ...).
    pa_terminal: bool = False

    # ---- the plate-appearance event ------------------------------------------
    #: e.g. None (non-terminal pitch), "walk", "strikeout", "single",
    #: "field_out", "home_run", "ground_into_double_play" ...
    event: str | None = None

    # ---- the run value and its provenance ------------------------------------
    #: The run *value* of the play: the run expectancy (RE24) after, minus the
    #: run expectancy before, plus the runs that scored. Computed by
    #: ``simulation.run_resolution.resolve_runs``; the loop never re-derives it.
    runs: float = 0.0
    #: How ``runs`` was resolved: always "re24_delta"
    #: (``run_resolution.RunResolution.method``).
    run_resolution_method: str | None = None
    #: The canonical outcome key the event resolved to, if any.
    canonical_event: str | None = None
    #: Base-out run expectancy before/after the play (the RE24 provenance).
    re_start: float | None = None
    re_end: float | None = None
    #: Integer runs that physically scored on the play (committed to the score).
    runs_scored: int = 0

    # ---- the batted ball -----------------------------------------------------
    exit_velo: float | None = None
    launch_angle: float | None = None
    spray_angle: float | None = None

    # ---- the fielding --------------------------------------------------------
    fielder_id: int | None = None  # the fielder who handled the ball
    is_error: bool = False  # True when the drawn play is a fielding error
    #: Outs recorded by this play.
    outs_recorded: int = 0

    # ---- the runners and the running game ------------------------------------
    #: Per-runner advancement as ``{runner_id: end_base}`` where end_base is
    #: 0 = scored, 1/2/3 = the base reached.  A retired runner has NO entry,
    #: because the box-score accumulator credits a run to every runner whose
    #: entry reads 0.
    baserunner_advances: dict[int, int] = field(default_factory=dict)
    #: The runner ids whose box-score run (``PlayerStatLine.r``) a mid-pitch
    #: path already credited on THIS pitch.  The steal of home credits its
    #: runner in ``_resolve_steal_outcome`` on every pitch, so
    #: ``_accumulate_pa`` skips these ids on a terminal pitch and credits
    #: every other runner who scored.
    box_run_credited: set[int] = field(default_factory=set)
    #: Whether a steal was attempted on this pitch.
    steal_attempted: bool = False
    #: Steal outcome: None / "safe" / "caught". A pickoff sets ``pickoff_out``.
    steal_outcome: str | None = None
    #: Runs on this pitch that pay no run batted in. The first case is a run
    #: that scored ON THE STEAL itself (a steal of home): MLB Rule 9.04(a) pays
    #: NO run batted in on a stolen base. The got-away advance and the run a
    #: dropped-third-strike reach forces home use it too, so the name is
    #: narrower than its use. The box-score accumulator subtracts it from the
    #: batter's RBI credit on a terminal pitch (the run is folded into the
    #: play's ``runs``).
    steal_runs_scored: int = 0
    #: A pickoff retired a runner on this pitch. NOT a steal attempt
    #: (``steal_attempted`` stays False); the runner is charged a CS only when
    #: he was tagged advancing (Rule 9.07(h)).
    pickoff_out: bool = False
    #: An errant pickoff throw advanced a runner on this pitch.
    pickoff_error: bool = False
    #: Why this result holds no plate appearance, or None. The one reason is
    #: ``"pickoff_third_out"``: a pickoff before the pitch made the third out,
    #: so no pitch was thrown, no event is credited and the same batter leads
    #: off the next inning (``pitch_outcome`` is NO_PITCH).
    pa_voided: str | None = None
    #: Why a steal staged for this pitch did not happen, or None. The one
    #: reason is ``"third_out_first"``: with two outs a third strike the
    #: catcher held is the third out before any throw, so the steal is void.
    steal_voided: str | None = None

    # ---- who was on the mound for this result --------------------------------
    #: The pitcher on the mound for this result, or None. ``step_pitch`` sets
    #: it after the manager's hooks, so a reliever who enters on this pitch is
    #: the one named. A reader of the play stream reads it here. The next
    #: state names the NEXT half's pitcher on the play that ends a half-inning.
    pitcher_id: int | None = None
    #: The pitcher's side (the fielding team) for this result, or None. A
    #: result built by hand without it leaves None, and the reader falls back
    #: to the next state.
    fielding_team: Team | None = None

    # ---- the committed next state --------------------------------------------
    #: The ``GameState`` after this play is committed.
    #: Optional so a PlayResult can be constructed before the commit.
    next_state: GameState | None = None

    @property
    def no_pitch(self) -> bool:
        """True when no pitch was thrown on this result (see NO_PITCH)."""
        return self.pitch_outcome == NO_PITCH


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
    "MAX_RUNNERS_ON_BASE",
    "BALLS_FOR_WALK",
    "STRIKES_FOR_STRIKEOUT",
    "PITCH_OUTCOMES",
    "CONTACT_PITCH_OUTCOME",
    "NO_PITCH",
]
