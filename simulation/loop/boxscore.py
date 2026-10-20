"""The per-game box score and the result of one simulated game.

:class:`BoxScore` holds one :class:`PlayerStatLine` per player. The state
machine writes it while it plays (:mod:`simulation.loop.credit` and the run
ledger's ``_record_outs``). :class:`GameSimResult` is what
:func:`simulation.sim_loop.simulate_game` returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from simulation.game_state import GameState, Team


@dataclass(slots=True)
class PlayerStatLine:
    """One player's accumulated stat line for a single simulated game (SIM-328).

    Carries BOTH a batting and a pitching slot so a single id (e.g. a two-way
    player, or just to keep the keyspace uniform) has one home; a pure batter
    leaves the pitching fields at 0 and vice-versa.

    Batting:  ``ab`` / ``h`` / ``hr`` / ``rbi``, plus ``so`` (SIM-484: the
              batter's own strikeouts — the official box's batting ``k``).
    Pitching: ``outs_recorded`` (the raw thirds-of-an-inning the pitcher retired)
              plus ``bf`` / ``k`` / ``bb`` / ``er``.  Innings are represented
              internally as an integer count of OUTS (thirds) so accumulation is
              exact; the human-readable x.0 / x.1 / x.2 form and the decimal
              thirds are derived on demand (:attr:`ip` / :attr:`ip_outs`).
              SIM-557: ``StateMachine._record_outs`` writes ``outs_recorded``
              when each out is recorded; ``StateMachine._accumulate_pa`` writes
              ``bf`` and the other pitching fields at the end of a plate
              appearance.

    ``k`` is the PITCHER's strikeouts (the official box's ``p_k``). Readers
    treat it that way. :attr:`BoxScore.pitchers` and the prop builder
    (:meth:`PropDistributionSet.from_boxscores`, which feeds the /boxscore card
    and the prop routes) tell a pitcher by it; ``scripts/sim_stats.py`` sums it
    for the game's strikeouts. So a batter's strikeout goes to ``so``, never
    to ``k``. On ``k`` it would hand every batter who struck out the pitcher
    props, double the smoke's strikeout count, and fold a two-way player's
    own strikeouts into his pitching K prop.
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
    so: int = 0  # SIM-484: struck out as a batter (``k`` below is the pitcher's)

    # --- pitching (IP stored as outs == thirds of an inning) ---
    outs_recorded: int = 0
    # SIM-557: batters faced as a pitcher (one per completed plate appearance,
    # the intentional walk included; a no-pitch result adds none).
    bf: int = 0
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
    offense, the fielding team's current pitcher is charged on defense.  The one
    exception is a pitcher's outs: :meth:`StateMachine._record_outs` credits
    each out when it is recorded, on any pitch (SIM-557).  A downstream
    Monte-Carlo aggregator (SIM-329 props) sums these across N games.

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
        """Lines with any pitching activity (outs recorded / batters faced / K /
        BB / ER).

        SIM-557: an out or a batter faced is the direct test that a pitcher
        pitched. K / BB / ER stay in the test for lines built by hand.
        """
        return {
            pid: ln
            for pid, ln in self.lines.items()
            if ln.outs_recorded or ln.bf or ln.k or ln.bb or ln.er
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
    #: SIM-557: the outs the play stream recorded (the sum of every step's
    #: ``outs_recorded``). The outs on the pitchers' box lines must equal it.
    outs_played: int = 0
    #: SIM-328 per-game boxscore: per-player AB/H/HR/RBI + IP/K/BB/ER, keyed by
    #: player_id.  ADDITIVE/optional so SIM-320/327 returns + tests are
    #: unaffected; ``None`` when no boxscore was accumulated (the loop attaches a
    #: populated one via :func:`simulate_game`).
    boxscore: BoxScore | None = None
    #: SIM-546, the inning grid: each team's runs per inning, one cell per
    #: inning. ``None`` in a cell marks a half the game never played (the
    #: scoreboard "x"); ``0`` marks a half played without a run. The two lists
    #: have the same length. The segment and team markets (the first inning,
    #: the first five innings, a team's runs) are priced from these cells.
    #: ``None`` for the whole field when the result was built by hand.
    home_by_inning: list[int | None] | None = None
    away_by_inning: list[int | None] | None = None

    @property
    def winner(self) -> Team | None:
        """The winning :class:`~simulation.game_state.Team` (None on a tie — only
        possible at the ``_MAX_INNINGS`` guard, never in a normal finish)."""
        if self.home_score > self.away_score:
            return Team.HOME
        if self.away_score > self.home_score:
            return Team.AWAY
        return None
