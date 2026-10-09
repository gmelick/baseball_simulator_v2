"""simulation.play_recorder -- capture the ordered PlayResult stream of ONE game.

WHAT THIS IS (SIM-355 deliverable; consumed by the SIM-357 agent)
=================================================================
:func:`simulation.sim_loop.simulate_game` (SIM-320) drives a
:class:`~simulation.sim_loop.StateMachine` to a completed game but returns only a
:class:`~simulation.sim_loop.GameSimResult` -- score / innings / final state /
boxscore.  It does NOT carry the ordered per-pitch
:class:`~simulation.game_state.PlayResult` list (a deliberate SIM-320 contract
decision -- the summary path never needs it).  The Phase-5 /plays + /state replay
surface (SIM-357 / SIM-331) DOES need that ordered stream.

This module captures it **without touching sim_loop.py**: a non-invasive
:class:`RecordingMachine` wrapper sits between :func:`simulate_game` and the real
StateMachine, calls ``step_pitch`` on the wrapped machine, appends the returned
``PlayResult`` to ``recorded_plays``, and returns it verbatim.  The loop is none
the wiser; the captured list feeds straight into
``PlayByPlay.from_play_results(...)`` (SIM-331).

DESIGN -- WHY A DELEGATING WRAPPER (not just a subclass)
========================================================
The production / no-DB machines are built by a *factory* (the SIM-332
``GameSpec.machine_factory`` seam) and may be ``StateMachine`` SUBCLASSES that
override ``step_pitch``.  A plain ``RecordingStateMachine`` subclass of the
*base* ``StateMachine`` would throw that subclass behaviour away.  So the
primary tool is :class:`RecordingMachine` -- a thin delegating wrapper that
keeps the factory's real machine intact and only intercepts ``step_pitch``.

``simulate_game`` reads a handful of attributes off the machine it is handed
(``.rng`` -- which it re-seeds; ``.full_pool_sampler`` -- whose rng it re-seeds;
``.boxscore`` -- which it harvests onto the result).  The wrapper forwards ALL
attribute reads/writes to the inner machine via ``__getattr__`` / ``__setattr__``
so every one of those touchpoints transparently reaches the real machine -- the
re-seed lands on the real rng, the harvested boxscore is the real one.

A :class:`RecordingStateMachine` (a true ``StateMachine`` subclass) is ALSO
provided for the case where a caller builds the base machine directly (no
factory) and wants recording baked in -- it overrides ``step_pitch`` to record.

EVERYTHING HERE IS MODULE-LEVEL + PICKLABLE-FRIENDLY
====================================================
Both the wrapper and the subclass are module-level classes (they pickle by
reference), and :func:`record_game_plays` resolves the machine via the same
dotted-factory convention the batch runner uses -- defaulting to the no-DB rng
factory so a full game records with NO DuckDB / FAISS / Postgres.
"""

from __future__ import annotations

import copy
from typing import Any, NamedTuple

from simulation.batch_runner import (
    GameSpec,
    _resolve_dotted,
)
from simulation.game_state import PlayResult
from simulation.sim_loop import GameSimResult, StateMachine, simulate_game
from simulation.snapshots import PitchContext

#: The default machine factory dotted-ref: the picklable, no-DB rng-driven
#: factory from the batch runner, so :func:`record_game_plays` runs a whole game
#: with no live sampler / DuckDB / Postgres.  A caller may pass the production
#: factory ref instead once it exists.
DEFAULT_FACTORY_REF = "simulation.batch_runner:rng_driven_machine_factory"


def _snapshot_next_state(result: PlayResult) -> None:
    """Freeze the play's committed state (SIM-486).

    ``step_pitch`` hands back the LIVE ``GameState`` as ``result.next_state`` —
    the one object the loop keeps mutating — so by the end of the game every
    recorded play pointed at the final state. The linescore, the W/L/S
    decisions and the per-pitch ``/state`` snapshots all read ``next_state``
    after the game and were therefore built from the final state alone. The
    old no-DB harness (single-or-out games) hid it; the synthetic-bundle games
    exposed it through the replay-card coherence lane. One deep copy per
    recorded pitch (a few hundred per replayed game) makes the stream honest.
    """
    state = getattr(result, "next_state", None)
    if state is not None:
        result.next_state = copy.deepcopy(state)


class RecordingMachine:
    """A non-invasive recording wrapper around any built ``StateMachine``.

    Wraps the machine a factory produced (which may be a ``StateMachine``
    subclass with its own ``step_pitch`` override) and intercepts only
    ``step_pitch``: it delegates to the wrapped machine, appends the returned
    :class:`~simulation.game_state.PlayResult` to :attr:`recorded_plays`, and
    returns it unchanged.  EVERY other attribute access (read or write) is
    forwarded to the inner machine, so the attributes ``simulate_game`` touches
    on the machine (``.rng`` re-seed, ``.sampler`` / ``._pa`` re-seed,
    ``.boxscore`` harvest) all transparently reach the real machine.

    Module-level so it pickles by reference; the captured list is a plain
    ``list[PlayResult]`` ready for ``PlayByPlay.from_play_results(...)``.
    """

    #: The names stored on the wrapper itself (everything else delegates).
    _OWN_ATTRS = frozenset({"_inner", "recorded_plays", "recorded_contexts"})

    def __init__(self, inner: StateMachine) -> None:
        # Bypass __setattr__'s delegation for the wrapper-owned attributes.
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "recorded_plays", [])
        # SIM-561: the state before each step, one per recorded play.
        object.__setattr__(self, "recorded_contexts", [])

    def step_pitch(self, state: Any, **kwargs: Any) -> PlayResult:
        """Delegate one pitch to the wrapped machine, recording its result.

        Forwards ``state`` + any kwargs verbatim to the inner machine's
        ``step_pitch`` (so a subclass that draws its own outcome keeps doing so),
        captures the returned ``PlayResult`` in order, and returns it unchanged
        -- ``simulate_game`` cannot tell it was wrapped.
        """
        context = PitchContext.from_state(state)
        result = self._inner.step_pitch(state, **kwargs)
        _snapshot_next_state(result)
        self.recorded_plays.append(result)
        self.recorded_contexts.append(context)
        return result

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires for names NOT found normally, so _inner /
        # recorded_plays (set via object.__setattr__) never reach here.
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        # Keep wrapper-owned attrs local; forward everything else (e.g. the
        # ``state_machine.rng = ...`` re-seed simulate_game performs) to the
        # real machine so the loop's mutations land where the loop expects.
        if name in RecordingMachine._OWN_ATTRS:
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)


class RecordingStateMachine(StateMachine):
    """A ``StateMachine`` subclass that records every pitch it steps.

    For the case where a caller builds the *base* ``StateMachine`` directly (no
    factory) and wants recording baked in.  Overrides ``step_pitch`` to call
    ``super().step_pitch(...)``, append the returned ``PlayResult`` to
    :attr:`recorded_plays`, and return it.  When the machine you need comes from
    a factory (the common path), prefer :class:`RecordingMachine`, which keeps
    the factory's own ``step_pitch`` override intact.

    Module-level so it pickles by reference.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.recorded_plays: list[PlayResult] = []
        self.recorded_contexts: list[PitchContext] = []  # SIM-561

    def step_pitch(self, state: Any, **kwargs: Any) -> PlayResult:  # type: ignore[override]
        context = PitchContext.from_state(state)
        result = super().step_pitch(state, **kwargs)
        _snapshot_next_state(result)
        self.recorded_plays.append(result)
        self.recorded_contexts.append(context)
        return result


class RecordedGame(NamedTuple):
    """One recorded game (SIM-561): the result, its plays, and the state before each.

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

    The result, the ordered plays and the state before each play (SIM-561).
    Builds the per-game :class:`~simulation.sim_loop.StateMachine` via the dotted
    ``factory_ref`` (the SIM-332 ``"module.path:callable"`` convention; defaults
    to the no-DB rng factory so this runs with no DuckDB / Postgres), wraps it in
    a :class:`RecordingMachine`, drives the wrapped machine through
    :func:`simulate_game` at ``seed``, and returns the game result plus the
    captured ordered pitch stream.

    ``sim_kwargs`` is the same dict the batch runner forwards to
    :func:`simulate_game` (keys: ``away_lineup`` / ``home_lineup`` / ``season`` /
    ``pitcher_id`` / ``bat_hand`` / ``k`` / ``max_innings``); ``_``-prefixed keys
    (e.g. ``_hit_rate``) are factory-only and are filtered out of the
    ``simulate_game`` splat, mirroring ``batch_runner._run_one``.  The captured
    list is ready for ``PlayByPlay.from_play_results(...)`` (SIM-331).

    Determinism: a fixed ``seed`` (+ the same ``factory_ref`` / ``sim_kwargs``)
    reproduces the exact ordered play stream.
    """
    kwargs = dict(sim_kwargs or {})

    # The factory sees the WHOLE spec (so it can read its own ``_``-prefixed
    # knobs); only NON-underscore keys are splatted into simulate_game's fixed
    # signature -- the same convention as batch_runner._run_one.
    spec = GameSpec(machine_factory=factory_ref, sim_kwargs=kwargs)
    factory = _resolve_dotted(factory_ref)
    inner = factory(seed, spec)
    if inner is None:
        # A factory may return None to mean "let simulate_game build a default
        # machine"; we cannot record that path non-invasively, so build the
        # default base machine ourselves and record it.
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
    """:func:`record_game` for a :class:`GameSpec` -- the worker pool's unit of work.

    SIM-561: the API records its replay game on the warm worker pool
    (:meth:`simulation.batch_runner.BatchRunner.record_game`), so the API process
    never loads the sim bundle.  Module-level, so it pickles by reference.
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
