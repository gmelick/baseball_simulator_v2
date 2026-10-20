"""Capture the ordered PlayResult stream of ONE game.

:func:`simulation.sim_loop.simulate_game` drives a
:class:`~simulation.sim_loop.StateMachine` to a finished game but returns only
a :class:`~simulation.sim_loop.GameSimResult`: the score, the innings, the
final state and the box score. It does NOT carry the ordered per-pitch
:class:`~simulation.game_state.PlayResult` list, because the summary path
never needs it. The replay routes (/plays and /state) DO need that stream.

This module captures it without changing the loop. A :class:`RecordingMachine`
wrapper sits between :func:`simulate_game` and the real StateMachine. It calls
``step_pitch`` on the wrapped machine, appends the returned ``PlayResult`` to
``recorded_plays`` and returns it unchanged. The captured list feeds
``PlayByPlay.from_play_results(...)``.

WHY A DELEGATING WRAPPER, NOT A SUBCLASS
========================================
A *factory* builds the production and no-DB machines (the
``GameSpec.machine_factory`` seam), and a factory may return a ``StateMachine``
SUBCLASS that overrides ``step_pitch``. A recording subclass of the *base*
``StateMachine`` would throw that override away. So the main tool is
:class:`RecordingMachine`: a thin wrapper that keeps the factory's machine
intact and intercepts only ``step_pitch``.

``simulate_game`` reads and writes a few attributes of the machine it gets:
``.rng`` (it re-seeds it), ``.full_pool_sampler`` (it re-seeds the sampler's
rng), ``.manager``, ``.bullpen``, ``.pitcher_rest_days`` and ``.boxscore``
(it copies the box score onto the result). The wrapper forwards EVERY
attribute read and write to the inner machine (``__getattr__`` /
``__setattr__``), so each of those reaches the real machine: the re-seed lands
on the real rng, and the box score is the real one.

:class:`RecordingStateMachine`, a true ``StateMachine`` subclass, serves a
caller that builds the base machine directly (no factory) and wants recording
built in. It overrides ``step_pitch`` to record.

Both classes are module-level, so they pickle by reference.
:func:`record_game` resolves the machine by the batch runner's dotted-factory
convention. Its default is the no-DB factory, so a full game records with no
DuckDB and no Postgres.
"""

from __future__ import annotations

import copy
from typing import Any, NamedTuple

from simulation.batch_runner import (
    GameSpec,
    _resolve_dotted,
)
from simulation.game_state import PlayResult
from simulation.loop.boxscore import GameSimResult
from simulation.sim_loop import StateMachine, simulate_game
from simulation.snapshots import PitchContext

#: The default machine factory's dotted reference: the batch runner's picklable
#: no-DB factory. :func:`record_game` then runs a whole game over the synthetic
#: bundle, with no DuckDB and no Postgres. A caller passes the production
#: factory's reference for a real game.
DEFAULT_FACTORY_REF = "simulation.batch_runner:rng_driven_machine_factory"


def _snapshot_next_state(result: PlayResult) -> None:
    """Freeze the play's committed state with a deep copy.

    ``step_pitch`` hands back the LIVE ``GameState`` as ``result.next_state``:
    the one object the loop keeps changing. Without a copy, every recorded
    play points at the final state by the end of the game. The linescore, the
    W/L/S decisions and the per-pitch ``/state`` snapshots all read
    ``next_state`` after the game, so each play needs its own frozen state.
    The cost is one deep copy per recorded pitch (a few hundred per game).
    """
    state = getattr(result, "next_state", None)
    if state is not None:
        result.next_state = copy.deepcopy(state)


class RecordingMachine:
    """A non-invasive recording wrapper around any built ``StateMachine``.

    Wraps the machine a factory built (which may be a ``StateMachine`` subclass
    with its own ``step_pitch`` override) and intercepts only ``step_pitch``:
    it delegates to the wrapped machine, appends the returned
    :class:`~simulation.game_state.PlayResult` to :attr:`recorded_plays` and
    the state before the step to :attr:`recorded_contexts`, and returns the
    result unchanged. EVERY other attribute read or write goes to the inner
    machine, so the attributes ``simulate_game`` touches (the ``.rng`` and
    ``.full_pool_sampler`` re-seeds, the ``.boxscore`` copy) reach the real
    machine.

    Module-level, so it pickles by reference. The captured list is a plain
    ``list[PlayResult]``, ready for ``PlayByPlay.from_play_results(...)``.
    """

    #: The names stored on the wrapper itself (everything else delegates).
    _OWN_ATTRS = frozenset({"_inner", "recorded_plays", "recorded_contexts"})

    def __init__(self, inner: StateMachine) -> None:
        # Bypass __setattr__'s delegation for the wrapper-owned attributes.
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "recorded_plays", [])
        # The state before each step, one per recorded play.
        object.__setattr__(self, "recorded_contexts", [])

    def step_pitch(self, state: Any, **kwargs: Any) -> PlayResult:
        """Delegate one pitch to the wrapped machine, recording its result.

        Passes ``state`` and any kwargs unchanged to the inner machine's
        ``step_pitch`` (so a subclass that draws its own outcome keeps doing
        so), captures the returned ``PlayResult`` in order and returns it
        unchanged: ``simulate_game`` cannot tell the machine was wrapped.
        """
        context = PitchContext.from_state(state)
        result = self._inner.step_pitch(state, **kwargs)
        _snapshot_next_state(result)
        self.recorded_plays.append(result)
        self.recorded_contexts.append(context)
        return result

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires for names NOT found normally, so the wrapper's
        # own attributes (set with object.__setattr__) never reach here.
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        # Keep the wrapper's own attributes local; forward everything else
        # (e.g. the ``state_machine.rng = ...`` re-seed in simulate_game) to
        # the real machine, so the loop's writes land where the loop expects.
        if name in RecordingMachine._OWN_ATTRS:
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)


class RecordingStateMachine(StateMachine):
    """A ``StateMachine`` subclass that records every pitch it steps.

    For a caller that builds the *base* ``StateMachine`` directly (no factory)
    and wants recording built in. It overrides ``step_pitch`` to call
    ``super().step_pitch(...)``, append the returned ``PlayResult`` to
    :attr:`recorded_plays` and return it. When the machine comes from a
    factory (the common path), use :class:`RecordingMachine`, which keeps the
    factory's own ``step_pitch`` override intact.

    Module-level, so it pickles by reference.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.recorded_plays: list[PlayResult] = []
        self.recorded_contexts: list[PitchContext] = []

    def step_pitch(self, state: Any, **kwargs: Any) -> PlayResult:  # type: ignore[override]
        context = PitchContext.from_state(state)
        result = super().step_pitch(state, **kwargs)
        _snapshot_next_state(result)
        self.recorded_plays.append(result)
        self.recorded_contexts.append(context)
        return result


class RecordedGame(NamedTuple):
    """One recorded game: the result, its plays, and the state before each play.

    ``contexts`` pairs one-to-one with ``plays`` (a no-pitch result included).
    """

    result: GameSimResult
    plays: list[PlayResult]
    contexts: list[PitchContext]


def record_game_plays(
    *,
    factory_ref: str = DEFAULT_FACTORY_REF,
    seed: int | None = None,
    sim_kwargs: dict[str, Any] | None = None,
) -> tuple[GameSimResult, list[PlayResult]]:
    """Simulate ONE game and return ``(GameSimResult, ordered list[PlayResult])``.

    The two-value form of :func:`record_game`; see it for the details.
    """
    recorded = record_game(factory_ref=factory_ref, seed=seed, sim_kwargs=sim_kwargs)
    return recorded.result, recorded.plays


def record_game(
    *,
    factory_ref: str = DEFAULT_FACTORY_REF,
    seed: int | None = None,
    sim_kwargs: dict[str, Any] | None = None,
) -> RecordedGame:
    """Simulate ONE game and return it as a :class:`RecordedGame`.

    The result, the ordered plays and the state before each play. The function
    builds the per-game :class:`~simulation.sim_loop.StateMachine` through the
    dotted ``factory_ref`` (``"module.path:callable"``; the default is the no-DB
    factory, so it runs with no DuckDB and no Postgres), wraps it in a
    :class:`RecordingMachine` and drives the wrapped machine through
    :func:`simulate_game` at ``seed``.

    ``sim_kwargs`` is the dict the batch runner forwards to
    :func:`simulate_game` (the two lineups, ``season``, ``pitcher_id``,
    ``bat_hand``, ``max_innings`` and the rest). A ``_``-prefixed key (e.g.
    ``_hit_rate``) is factory-only and stays out of the ``simulate_game`` call,
    as in ``batch_runner._run_one``. The captured list is ready for
    ``PlayByPlay.from_play_results(...)``.

    A fixed ``seed``, with the same ``factory_ref`` and ``sim_kwargs``,
    reproduces the exact ordered play stream.
    """
    kwargs = dict(sim_kwargs or {})

    # The factory sees the WHOLE spec (so it can read its own ``_``-prefixed
    # settings); only the other keys go into simulate_game's fixed signature,
    # the same convention as batch_runner._run_one.
    spec = GameSpec(machine_factory=factory_ref, sim_kwargs=kwargs)
    factory = _resolve_dotted(factory_ref)
    inner = factory(seed, spec)
    if inner is None:
        # A factory may return None to ask for simulate_game's default
        # machine. The wrapper cannot record that path, so the function builds
        # the base machine itself and records it. That machine has no sampler.
        recorder: Any = RecordingStateMachine()
    else:
        recorder = RecordingMachine(inner)

    passthrough = {k: v for k, v in kwargs.items() if not k.startswith("_")}
    result = simulate_game(recorder, seed=seed, **passthrough)
    return RecordedGame(
        result=result,
        plays=list(recorder.recorded_plays),
        contexts=list(recorder.recorded_contexts),
    )


def record_spec(spec: GameSpec, seed: int | None) -> RecordedGame:
    """:func:`record_game` for a :class:`GameSpec`: the worker pool's unit of work.

    The API records its replay game on the warm worker pool
    (:meth:`simulation.batch_runner.BatchRunner.record_game`), so the API process
    never loads the sim bundle. Module-level, so it pickles by reference.
    """
    return record_game(
        factory_ref=spec.machine_factory or DEFAULT_FACTORY_REF,
        seed=seed,
        sim_kwargs=spec.sim_kwargs,
    )


__all__ = [
    "DEFAULT_FACTORY_REF",
    "RecordingMachine",
    "RecordingStateMachine",
    "RecordedGame",
    "record_game",
    "record_game_plays",
    "record_spec",
]
