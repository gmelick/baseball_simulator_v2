"""The registry of the ``SIM_*`` environment flags.

Every ``SIM_*`` flag the API or the simulator reads is a row of :data:`FLAGS`:
its name, its type, its code default, the work that owns it and a one-line
meaning. Code reads a flag only through the readers below, so one table says
what each flag does and what an unset flag means. The production values live in
the ``app`` service of ``docker-compose.yml``; the unit tests pin their own in
``tests/conftest.py``. ``tests/unit/test_flag_registry.py`` fails when the code
reads a ``SIM_*`` name that is not registered, or when compose sets one.

The readers keep the rules the scattered reads had:

* an unset flag reads as its default;
* an on/off flag reads ``1``, ``true``, ``yes`` and ``on`` as on, and ``""``,
  ``0``, ``false``, ``no`` and ``off`` as off, in any case and with spaces
  trimmed; any other value raises :class:`FlagValueError`;
* a number that does not parse keeps the default and logs a warning.

Each reader takes an optional ``env`` mapping (default ``os.environ``), so a
caller can read a flag set it was handed instead of the process environment.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)

Kind = Literal["bool", "int", "float", "str"]

ON_VALUES = frozenset({"1", "true", "yes", "on"})
OFF_VALUES = frozenset({"", "0", "false", "no", "off"})

#: The prefix of the actor score-matrix powers, one flag per actor:
#: ``SIM_ACTOR_POWER_BATTER``, ``..._FIELDER`` (and ``..._FIELDER_<POS>`` for one
#: position), ``..._RUNNER_STEAL``, ``..._RUNNER_ADV``, ``..._CATCHER_THROWING``,
#: ``..._PITCHER_STEAL`` and ``..._MANAGER_USAGE``. An unset power keeps the
#: sampler's default: 1.0, except the manager's usage score, which is off (0.0).
ACTOR_POWER_PREFIX = "SIM_ACTOR_POWER_"


class FlagValueError(ValueError):
    """An on/off flag holds a value that is neither on nor off."""


@dataclass(frozen=True, slots=True)
class Flag:
    """One environment flag: what it is called, what it holds, and who owns it."""

    name: str
    kind: Kind
    default: bool | int | float | str | None
    owner: str
    meaning: str


def _f(name: str, kind: Kind, default: bool | int | float | str | None, owner: str, meaning: str):
    return Flag(name, kind, default, owner, meaning)


_TABLE: tuple[Flag, ...] = (
    # --- The API process -------------------------------------------------------
    _f(
        "SIM_RUNNER_WORKERS",
        "int",
        None,
        "the worker pool (SIM-403)",
        "Worker processes in the simulation pool; unset = min(CPU count - 1, 10).",
    ),
    _f(
        "SIM_MACHINE_FACTORY_REF",
        "str",
        "",
        "the machine factory seam (SIM-352)",
        "The dotted factory the API's runs use; unset = the production factory.",
    ),
    _f(
        "SIM_JOB_MAX_CONCURRENT",
        "int",
        1,
        "the simulation run jobs (SIM-519)",
        "Run jobs that may run at once (at least 1).",
    ),
    _f(
        "SIM_RESTART_ON_NEW_BUNDLE",
        "bool",
        False,
        "the bundle watch (SIM-563)",
        "Restart the app when the nightly rebuild publishes a new bundle version.",
    ),
    _f(
        "SIM_BOOT_LOCK_WAIT_S",
        "float",
        1800.0,
        "the bundle watch (SIM-563)",
        "Seconds a boot waits for a DuckDB writer to release the file.",
    ),
    # --- The batch runner (read once, when the module loads) -------------------
    _f(
        "SIM_PREWARM_MAX_CONCURRENT",
        "int",
        2,
        "the worker prewarm (SIM-402)",
        "Workers that load the bundle at the same time while the pool warms.",
    ),
    _f(
        "SIM_MP_START_METHOD",
        "str",
        "forkserver",
        "the worker start method (SIM-430)",
        "How the pool starts a worker: forkserver, spawn or fork.",
    ),
    # --- The lineup resolver ---------------------------------------------------
    _f(
        "SIM_BULLPEN_SOURCE",
        "str",
        "synthetic",
        "the real manager and pen (SIM-427)",
        "'box' reads each side's pen from the official box; else a synthetic pen.",
    ),
    # --- The state machine -----------------------------------------------------
    _f(
        "SIM_BB_PLATOON",
        "bool",
        False,
        "the batted-ball platoon (SIM-413)",
        "The fielding draw keys its pool on the batter's hand against the pitcher's.",
    ),
    _f(
        "SIM_GOT_AWAY",
        "bool",
        False,
        "the catcher receiving profile (SIM-517)",
        "Honour the drawn pitch row's got-away fact (a wild pitch or passed ball).",
    ),
    _f(
        "SIM_MANAGER",
        "bool",
        False,
        "the manager model (SIM-434)",
        "Attach a manager to the machine: the intentional walk and the pitching change.",
    ),
    _f(
        "SIM_MANAGER_DRAW",
        "bool",
        False,
        "the real manager and pen (SIM-427)",
        "The pitching change is a draw from the change opportunity pool.",
    ),
    # --- The pitch draw --------------------------------------------------------
    _f(
        "SIM_SIT_SIGMA",
        "float",
        2.0,
        "the joint weight fit (SIM-548)",
        "The pitch draw's bandwidth on the base-out situation.",
    ),
    _f(
        "SIM_HOME_OFF_WEIGHT",
        "float",
        1.0,
        "the home-field draw (SIM-476)",
        "The fielding draw's weight on a row of the other batting side; 1 = off, 0 = hard match.",
    ),
    _f(
        "SIM_PITCH_HOME_OFF_WEIGHT",
        "float",
        1.0,
        "the draw conditioning (SIM-518)",
        "The pitch draw's weight on a row of the other batting side; 1 = off.",
    ),
    _f(
        "SIM_PARK_KERNEL_SIGMA",
        "float",
        0.0,
        "the park kernel (SIM-491)",
        "The bandwidth of the park run-factor kernel; 0 = off.",
    ),
    _f(
        "SIM_FATIGUE_PC_SIGMA",
        "float",
        0.0,
        "the draw conditioning (SIM-518)",
        "The pitch draw's bandwidth on the pitch count; 0 = off.",
    ),
    _f(
        "SIM_FATIGUE_TTO_SIGMA",
        "float",
        0.0,
        "the draw conditioning (SIM-518)",
        "The pitch draw's bandwidth on the times through the order; 0 = off.",
    ),
    _f(
        "SIM_PITCH_CELL_INDEX",
        "bool",
        False,
        "the pitch-draw cell index (SIM-467)",
        "Filter each plate appearance's pitch draw to its base-out-score cell.",
    ),
    _f(
        "SIM_PITCH_MIN_CELL",
        "int",
        20,
        "the pitch-draw cell index (SIM-467)",
        "The fewest rows a cell may hold before the draw widens it.",
    ),
    _f(
        "SIM_PITCH_RESULT_SPLIT",
        "bool",
        False,
        "the play-picker redesign (SIM-523)",
        "Draw the pitch, then its result conditioned on the pitch (the split).",
    ),
    _f(
        "SIM_PITCH_PITCHER_POWER",
        "float",
        1.0,
        "the play-picker redesign (SIM-523)",
        "The power on the pitcher's score in the pitch draw.",
    ),
    _f(
        "SIM_PITCH_BATTER_POWER",
        "float",
        1.0,
        "the play-picker redesign (SIM-523)",
        "The power on the batter's score in the pitch draw.",
    ),
    _f(
        "SIM_RESULT_PITCH_SIGMA",
        "float",
        1.0,
        "the play-picker redesign (SIM-523)",
        "The result draw's bandwidth on the drawn pitch (the split only).",
    ),
    _f(
        "SIM_RESULT_DENSITY_POWER",
        "float",
        1.0,
        "the play-picker redesign (SIM-523)",
        "The power of the result draw's density correction; 0 = off.",
    ),
    _f(
        "SIM_RESULT_PITCHER_POWER",
        "float",
        1.0,
        "the play-picker redesign (SIM-523)",
        "The power on the pitcher's score in the result draw.",
    ),
    _f(
        "SIM_RESULT_BATTER_POWER",
        "float",
        1.0,
        "the play-picker redesign (SIM-523)",
        "The power on the batter's score in the result draw.",
    ),
    _f(
        "SIM_CATCHER_RECEIVING",
        "bool",
        False,
        "the catcher receiving ratio (SIM-526)",
        "Apply the catcher's ball-strike ratio to taken pitches.",
    ),
    # --- The fielding draw -----------------------------------------------------
    _f(
        "SIM_BB_BORN_SIGMA",
        "float",
        0.0,
        "the play-picker redesign (SIM-523)",
        "The fielding draw's bandwidth on the batted ball the result row was born with; 0 = off.",
    ),
    _f(
        "SIM_BB_BORN_DENSITY_POWER",
        "float",
        1.0,
        "the play-picker redesign (SIM-523)",
        "The power of the born-ball kernel's density correction; 0 = off.",
    ),
    _f(
        "SIM_BB_BORN_PER_FEATURE",
        "bool",
        False,
        "the fence amendments (SIM-478)",
        "The born-ball kernel's exponent is per feature, not per feature count.",
    ),
    _f(
        "SIM_BB_PITCH_SIGMA",
        "float",
        0.0,
        "the draw conditioning (SIM-518)",
        "The fielding draw's bandwidth on the drawn pitch; 0 = off.",
    ),
    _f(
        "SIM_BB_CLASS_FILTER",
        "bool",
        False,
        "the play-picker redesign (SIM-523)",
        "Filter the fielding draw to the born ball's class.",
    ),
    _f(
        "SIM_BB_BATTER_POWER",
        "float",
        1.0,
        "the play-picker redesign (SIM-523)",
        "The power on the batter's score in the fielding draw.",
    ),
    _f(
        "SIM_PARK_WALL_ZONE_ONLY",
        "bool",
        False,
        "the play-picker redesign (SIM-523)",
        "Weight the park only for balls in the wall zone.",
    ),
    _f(
        "SIM_WALL_ZONE_DISTANCE",
        "float",
        300.0,
        "the play-picker redesign (SIM-523)",
        "Feet: a ball this far or farther is in the wall zone.",
    ),
    _f(
        "SIM_FENCE_STAGE",
        "bool",
        False,
        "the fence certification (SIM-478)",
        "Run the fence stage: an air ball's carry against the live park's fence.",
    ),
    _f(
        "SIM_FENCE_MARGIN",
        "float",
        0.0,
        "the fence certification (SIM-478)",
        "Feet of the fence stage's undecided band; 0 = decisive.",
    ),
    _f(
        "SIM_CARRY_OFFSET",
        "bool",
        False,
        "the fence amendments (SIM-478)",
        "Read the born ball's carry in the live park's air (the per-park offsets).",
    ),
    _f(
        "SIM_BB_MARGIN_BAND",
        "float",
        0.0,
        "the fence amendments (SIM-478)",
        "Feet: a short wall-zone ball draws only rows this close to their fence; 0 = off.",
    ),
    _f(
        "SIM_BB_MARGIN_MIN_ROWS",
        "int",
        20,
        "the fence amendments (SIM-478)",
        "The fewest rows the wall-margin band may leave before it stands down.",
    ),
    # --- The running game ------------------------------------------------------
    _f(
        "SIM_STEAL_PITCH_CLASS",
        "bool",
        True,
        "the running game on the pitch (SIM-554)",
        "Draw the pickoff before the pitch and the steal after it, in the pitch's class.",
    ),
    # --- The pitching change ---------------------------------------------------
    _f(
        "SIM_CHANGE_SIT_SIGMA",
        "float",
        1.0,
        "the play-picker redesign (SIM-523)",
        "The pitching-change draw's bandwidth on the situation.",
    ),
    _f(
        "SIM_CHANGE_PITCHER_POWER",
        "float",
        1.0,
        "the play-picker redesign (SIM-523)",
        "The power on the live pitcher's score in the pitching-change draw; 0 = off.",
    ),
    _f(
        "SIM_CHANGE_MIN_CELL",
        "int",
        20,
        "the play-picker redesign (SIM-523)",
        "The fewest rows a pitching-change cell may hold before the draw widens it.",
    ),
    _f(
        "SIM_RELIEF_ROLE_SIGMA",
        "float",
        0.0,
        "the real manager and pen (SIM-427)",
        "The reliever draw's bandwidth on the arm's role; 0 = off.",
    ),
    _f(
        "SIM_RELIEF_PITCHER_POWER",
        "float",
        0.0,
        "the real manager and pen (SIM-427)",
        "The power on the reliever's stuff score; 0 = off.",
    ),
    _f(
        "SIM_RELIEF_REST_SIGMA",
        "float",
        0.0,
        "the real manager and pen (SIM-427)",
        "The reliever draw's bandwidth on days of rest; 0 = off.",
    ),
    _f(
        "SIM_RELIEF_PITCHED2D_OFF_WEIGHT",
        "float",
        1.0,
        "the real manager and pen (SIM-427)",
        "The weight on an arm that pitched in the last two days; 1 = off.",
    ),
    _f(
        "SIM_RELIEF_PITCHES3D_SIGMA",
        "float",
        0.0,
        "the real manager and pen (SIM-427)",
        "The reliever draw's bandwidth on pitches thrown in three days; 0 = off.",
    ),
    _f(
        "SIM_RELIEF_HAND_OFF_WEIGHT",
        "float",
        1.0,
        "the real manager and pen (SIM-427)",
        "The weight on an arm of the other hand; 1 = off.",
    ),
)

#: Every registered flag, by name.
FLAGS: dict[str, Flag] = {flag.name: flag for flag in _TABLE}


def is_registered(name: str) -> bool:
    """Whether ``name`` is a registered flag or an actor power."""
    return name in FLAGS or name.startswith(ACTOR_POWER_PREFIX)


def _spec(name: str, kind: Kind) -> Flag:
    flag = FLAGS.get(name)
    if flag is None:
        raise KeyError(f"{name} is not a registered SIM_* flag (simulation/flags.py)")
    if flag.kind != kind:
        raise TypeError(f"{name} is a {flag.kind} flag, read as {kind}")
    return flag


def _raw(name: str, env: Mapping[str, str] | None) -> str | None:
    return (os.environ if env is None else env).get(name)


def read_bool(name: str, env: Mapping[str, str] | None = None) -> bool:
    """The on/off flag ``name``; unset reads as its default."""
    flag = _spec(name, "bool")
    raw = _raw(name, env)
    if raw is None:
        return bool(flag.default)
    value = raw.strip().lower()
    if value in ON_VALUES:
        return True
    if value in OFF_VALUES:
        return False
    raise FlagValueError(
        f"{name}={raw!r} is neither on ({', '.join(sorted(ON_VALUES))}) "
        f"nor off (empty, {', '.join(sorted(OFF_VALUES - {''}))})"
    )


def read_float(name: str, env: Mapping[str, str] | None = None) -> float:
    """The number flag ``name``; unset or unparsable reads as its default."""
    flag = _spec(name, "float")
    raw = _raw(name, env)
    if raw is None:
        return float(flag.default)  # type: ignore[arg-type]
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r does not parse as a number; keeping %s", name, raw, flag.default)
        return float(flag.default)  # type: ignore[arg-type]


def read_int(name: str, env: Mapping[str, str] | None = None) -> int | None:
    """The whole-number flag ``name``; unset or unparsable reads as its default.

    A value written as a decimal (``"20.0"``) reads as its whole part.
    """
    flag = _spec(name, "int")
    raw = _raw(name, env)
    if raw is None or not raw.strip():
        return flag.default  # type: ignore[return-value]
    try:
        return int(float(raw))
    except ValueError:
        log.warning("%s=%r does not parse as a whole number; keeping %s", name, raw, flag.default)
        return flag.default  # type: ignore[return-value]


def read_str(name: str, env: Mapping[str, str] | None = None) -> str:
    """The text flag ``name`` as set (not trimmed); unset reads as its default."""
    flag = _spec(name, "str")
    raw = _raw(name, env)
    return str(flag.default) if raw is None else raw


def actor_power_overrides(env: Mapping[str, str] | None = None) -> dict[str, float]:
    """The actor powers the environment sets, keyed by actor.

    ``SIM_ACTOR_POWER_BATTER=2`` gives ``{"batter": 2.0}``; a per-position
    fielder power keeps its position in capitals (``fielder_SS``). A value that
    does not parse is left out, so the sampler keeps its default for that actor.
    """
    src = os.environ if env is None else env
    powers: dict[str, float] = {}
    for key, val in src.items():
        if not key.startswith(ACTOR_POWER_PREFIX):
            continue
        name = key[len(ACTOR_POWER_PREFIX) :].lower()
        if name.startswith("fielder_"):
            name = "fielder_" + name[len("fielder_") :].upper()
        try:
            powers[name] = float(val)
        except ValueError:
            log.warning("%s=%r does not parse as a number; the sampler keeps its default", key, val)
    return powers


__all__ = [
    "ACTOR_POWER_PREFIX",
    "FLAGS",
    "Flag",
    "FlagValueError",
    "OFF_VALUES",
    "ON_VALUES",
    "actor_power_overrides",
    "is_registered",
    "read_bool",
    "read_float",
    "read_int",
    "read_str",
]
