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
``GameSpec.machine_factory`` seam) and are concrete ``StateMachine`` SUBCLASSES
(e.g. ``simulation.batch_runner._RngOutcomeStateMachine``, which overrides
``step_pitch`` to draw its own pitch outcome).  A plain ``RecordingStateMachine``
subclass of the *base* ``StateMachine`` would throw that subclass behaviour away.
So the primary tool is :class:`RecordingMachine` -- a thin delegating wrapper that
keeps the factory's real machine intact and only intercepts ``step_pitch``.

``simulate_game`` reads a handful of attributes off the machine it is handed
(``.rng`` -- which it re-seeds; ``.sampler`` / ``._pa`` -- which it re-seeds;
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

from typing import Any, Optional

from simulation.batch_runner import (
    GameSpec,
    _resolve_dotted,
    rng_driven_machine_factory,
)
from simulation.game_state import PlayResult
from simulation.sim_loop import GameSimResult, StateMachine, simulate_game

#: The default machine factory dotted-ref: the picklable, no-DB rng-driven
#: factory from the batch runner, so :func:`record_game_plays` runs a whole game
#: with no live sampler / DuckDB / Postgres.  A caller may pass the production
#: factory ref instead once it exists.
DEFAULT_FACTORY_REF = "simulation.batch_runner:rng_driven_machine_factory"


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
    _OWN_ATTRS = frozenset({"_inner", "recorded_plays"})

    def __init__(self, inner: StateMachine) -> None:
        # Bypass __setattr__'s delegation for the two wrapper-owned attributes.
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "recorded_plays", [])

    def step_pitch(self, state: Any, **kwargs: Any) -> PlayResult:
        """Delegate one pitch to the wrapped machine, recording its result.

        Forwards ``state`` + any kwargs verbatim to the inner machine's
        ``step_pitch`` (so a subclass that draws its own outcome keeps doing so),
        captures the returned ``PlayResult`` in order, and returns it unchanged
        -- ``simulate_game`` cannot tell it was wrapped.
        """
        result = self._inner.step_pitch(state, **kwargs)
        self.recorded_plays.append(result)
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

    def step_pitch(self, state: Any, **kwargs: Any) -> PlayResult:  # type: ignore[override]
        result = super().step_pitch(state, **kwargs)
        self.recorded_plays.append(result)
        return result


def record_game_plays(
    *,
    factory_ref: str = DEFAULT_FACTORY_REF,
    seed: Optional[int] = None,
    sim_kwargs: Optional[dict[str, Any]] = None,
) -> "tuple[GameSimResult, list[PlayResult]]":
    """Simulate ONE game and return ``(GameSimResult, ordered list[PlayResult])``.

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
    return result, list(recorder.recorded_plays)


__all__ = [
    "DEFAULT_FACTORY_REF",
    "RecordingMachine",
    "RecordingStateMachine",
    "record_game_plays",
]
