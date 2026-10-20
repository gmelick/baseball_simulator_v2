"""The parts of the pitch-by-pitch simulator, one module per responsibility.

:class:`simulation.sim_loop.StateMachine` is the simulator. It keeps its core
in ``simulation/sim_loop.py`` (the pitch step, the run ledger, the half-inning
control and the full-game driver) and takes the rest of its methods from the
mixins here:

* :mod:`~simulation.loop.values` -- the constants, the value objects and the
  count machine.
* :mod:`~simulation.loop.boxscore` -- the per-game box score and the game result.
* :mod:`~simulation.loop.in_play` -- the fielding draw, the drawn row's
  transition and the advancement draws.
* :mod:`~simulation.loop.running_game` -- the steal, the pickoff and the
  got-away advance: the draws, the staging and the resolution.
* :mod:`~simulation.loop.terminals` -- the walk, the intentional walk, the
  strikeout and the dropped third strike.
* :mod:`~simulation.loop.manager` -- the leverage index, the intentional-walk
  draw, the plate-appearance hooks and the pitching change.
* :mod:`~simulation.loop.credit` -- the box-score credit of a plate appearance.

A mixin holds methods only. Every draw shares the machine's ``self.rng`` and
the machine's sampler, so the random number order is the one the core sets.
"""
