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
    """One player's stat line for one simulated game.

    A line carries both a batting and a pitching part, so a two-way player has
    one home; a pure batter leaves the pitching fields at 0 and the reverse.

    Batting:  ``ab`` / ``h`` / ``hr`` / ``rbi``, the extras ``b2`` / ``b3`` /
              ``r`` / ``sb`` / ``cs``, and ``so``: the batter's own strikeouts
              (the official box's batting ``k``).
    Pitching: ``outs_recorded`` (innings as a count of outs, so the sum is
              exact; :attr:`ip` and :attr:`ip_thirds` derive the two readable
              forms) and ``bf`` / ``k`` / ``bb`` / ``er`` / ``h_allowed`` /
              ``r_allowed``. ``StateMachine._record_outs`` writes
              ``outs_recorded`` when each out is recorded;
              ``StateMachine._accumulate_pa`` writes the other pitching fields
              at the end of a plate appearance.

    ``k`` is the PITCHER's strikeouts (the official box's ``p_k``), and the
    readers treat it that way: :attr:`BoxScore.pitchers` and the prop builder
    (:meth:`PropDistributionSet.from_boxscores`, which feeds the /boxscore card
    and the prop routes) tell a pitcher by it, and ``scripts/sim_stats.py``
    sums it for the game's strikeouts. So a batter's strikeout goes to ``so``,
    never to ``k``: on ``k`` it would hand every batter who struck out the
    pitcher props, double the smoke's strikeout count, and fold a two-way
    player's own strikeouts into his pitching strikeout prop.
    """

    player_id: int

    # --- batting ---
    ab: int = 0
    h: int = 0
    hr: int = 0
    rbi: int = 0
    b2: int = 0  # doubles (a subset of ``h``)
    b3: int = 0  # triples (a subset of ``h``)
    r: int = 0  # runs scored by this player (offense)
    sb: int = 0  # stolen bases
    cs: int = 0  # caught stealing
    so: int = 0  # struck out as a batter (``k`` below is the pitcher's)

    # --- pitching (IP stored as outs == thirds of an inning) ---
    outs_recorded: int = 0
    # Batters faced as a pitcher: one per completed plate appearance, the
    # intentional walk included; a no-pitch result adds none.
    bf: int = 0
    k: int = 0
    bb: int = 0
    er: int = 0
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
    """The per-game store of :class:`PlayerStatLine`s, keyed by ``player_id``.

    The state machine credits it at the end of every plate appearance
    (:meth:`StateMachine._accumulate_pa`): the batting team's batter on
    offense, the fielding team's pitcher on defense. A pitcher's outs are
    credited when each out is recorded (:meth:`StateMachine._record_outs`),
    on any pitch. The prop distributions sum the lines across games.

    :attr:`batters` and :attr:`pitchers` filter the one line store;
    :meth:`line` returns (creating if needed) one player's line.
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
        """Lines with any pitching activity: an out recorded, a batter faced,
        a strikeout, a walk or an earned run. An out or a batter faced is the
        direct test; the other three cover lines built by hand."""
        return {
            pid: ln
            for pid, ln in self.lines.items()
            if ln.outs_recorded or ln.bf or ln.k or ln.bb or ln.er
        }


@dataclass(slots=True)
class GameSimResult:
    """The result of one simulated game, as :func:`simulate_game` returns it.

    It carries the final score, the innings played, the final
    :class:`~simulation.game_state.GameState`, the flags that tell a
    regulation finish from a walk-off or an extra-innings finish, and the
    game's ``seed``, so the result names how to replay it.
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
    #: The outs the play stream recorded (the sum of every step's
    #: ``outs_recorded``). The outs on the pitchers' box lines must equal it.
    outs_played: int = 0
    #: The game's box score; ``None`` when no plate appearance was credited.
    boxscore: BoxScore | None = None
    #: The inning grid: each team's runs per inning, one cell per inning.
    #: ``None`` in a cell marks a half the game never played (the scoreboard
    #: "x"); ``0`` marks a half played without a run. The two lists have the
    #: same length. The segment and team markets (the first inning, the first
    #: five innings, a team's runs) are priced from these cells. ``None`` for
    #: the whole field when the result was built by hand.
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
