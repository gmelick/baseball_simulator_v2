"""simulation.constants — Centralized run-value constants (SIM-202, SIM-312).

This module is the single, sourced home for the run-value constants used
throughout the simulator. It exposes:

  * ``RUN_VALUES``          — offensive *linear weights* (Tango / "The Book"
                              style) for the 12 standard plate-appearance
                              outcomes, anchored to a 2024 run environment.
  * ``DEFENSIVE_RUN_VALUES`` — defensive run conversions used by the
                              fielder / catcher engines (runs saved per out,
                              per block, per framing strike).
  * ``STATCAST_EVENT_ALIASES`` + ``resolve_event_to_canonical`` /
    ``run_value_for_event`` — SIM-312: map Statcast's raw ``events`` vocabulary
    onto the canonical ``RUN_VALUES`` keys so the play pool's sampled events
    never silently score 0.0.

Why centralize?
---------------
Before this module these magic numbers lived inline in pipeline and engine
code. Centralizing them gives one auditable, citeable source so the Baseball
Analyst methodology stays consistent across the codebase, and so a future
season re-baseline is a one-file change.

-------------------------------------------------------------------------------
OFFENSIVE LINEAR WEIGHTS  (``RUN_VALUES``)
-------------------------------------------------------------------------------
"Linear weights" express the average run value of each plate-appearance
outcome relative to a generic batting out, in the spirit of Tom Tango,
Mitchel Lichtman & Andrew Dolphin, *The Book: Playing the Percentages in
Baseball* (2007), and the FanGraphs wOBA / linear-weights run-value tables.

Anchoring (run environment)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
These values are anchored to the **2024 MLB run environment** (~4.39 R/team/G,
league wOBA ~ .310, wOBA scale ~ 1.24 per the FanGraphs 2024 guts/constants
table). The event weights below are the *absolute* run values (runs above a
zero baseline, with the out carrying its true negative value) consistent with
that environment, i.e. the canonical Tango linear-weight set lightly tuned to
2024:

    walk (uBB)            +0.55
    hit-by-pitch (HBP)    +0.58   (slightly > BB: cannot be intentional)
    single (1B)           +0.70
    double (2B)           +1.00
    triple (3B)           +1.27
    home run (HR)         +1.65
    batting out / K       -0.27   (the cost of making an out in 2024)

Derived / situational outcomes (also 2024-anchored, from FanGraphs/Tango):
    intentional walk      +0.18   (IBB is worth far less than uBB)
    strikeout             -0.27   (~ the generic batting out)
    field out (BIP out)   -0.27   (generic out value)
    GIDP                  -0.79   (the out PLUS the second out / lost runner)
    sacrifice fly         +0.22   (productive out: scores a runner from 3rd)
    sacrifice hit (bunt)  -0.10   (gives up an out to advance a runner)

Canonical 12-outcome key set
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The simulator models a plate appearance as resolving to exactly one of these
12 mutually-exclusive, collectively-exhaustive outcomes (the standard
Retrosheet/Statcast PA-outcome partition used for linear-weight accounting):

    single, double, triple, home_run,
    walk, intentional_walk, hit_by_pitch,
    strikeout, field_out, ground_into_double_play,
    sacrifice_fly, sacrifice_hit

-------------------------------------------------------------------------------
DEFENSIVE RUN VALUES  (``DEFENSIVE_RUN_VALUES``)
-------------------------------------------------------------------------------
Conversion factors from defensive events to runs, as used by the fielder and
catcher engines. Grounded in Statcast OAA->runs research and Tango
framing/blocking run conversions:

    runs_per_oaa_infield        0.75   runs per infield out above average
    runs_per_oaa_outfield       0.90   runs per outfield out above average
    runs_per_block_saved        0.25   runs per blocked pitch above average
    runs_per_strike_above_avg   0.125  runs per framing strike above average

These four values are CENTRALIZED here unchanged from their prior inline
definitions in ``pipeline/batch/player_profile_computor.py``.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Offensive linear weights — 12 standard plate-appearance outcomes.
# 2024 run environment (FanGraphs 2024 guts; Tango/Lichtman/Dolphin "The Book").
# Units: runs, relative to a zero baseline (outs carry their true negative
# value). All values are floats.
# ---------------------------------------------------------------------------
RUN_VALUES: dict[str, float] = {
    # --- reaching base via a hit ---
    "single": 0.70,
    "double": 1.00,
    "triple": 1.27,
    "home_run": 1.65,
    # --- reaching base without a hit ---
    "walk": 0.55,  # unintentional walk (uBB)
    "intentional_walk": 0.18,  # IBB — worth far less than an uBB
    "hit_by_pitch": 0.58,  # slightly above uBB
    # --- outs ---
    "strikeout": -0.27,
    "field_out": -0.27,  # generic ball-in-play out
    "ground_into_double_play": -0.79,  # out + lost runner/extra out
    "sacrifice_fly": 0.22,  # productive out (scores runner from 3B)
    "sacrifice_hit": -0.10,  # sac bunt — gives up an out to advance
}

# ---------------------------------------------------------------------------
# SIM-312 — Statcast raw ``events`` vocabulary  ->  canonical RUN_VALUES key.
#
# THE BUG THIS FIXES
# ~~~~~~~~~~~~~~~~~~~
# ``RUN_VALUES`` above is keyed by the *canonical 12-outcome* partition
# (``intentional_walk``, ``ground_into_double_play``, ``sacrifice_fly`` ...).
# The play pool, however, stores Statcast's *raw* ``events`` strings
# (``sim.outcome_pool.events`` / ``sim.pitch_pool.events``) sampled verbatim by
# the ``PlayPoolSampler``.  Statcast's vocabulary is different and far larger:
# ``field_out``, ``force_out``, ``fielders_choice``, ``sac_fly``,
# ``grounded_into_double_play``, ``double_play``, ``intent_walk`` ...
#
# ``sim_loop.py`` does ``RUN_VALUES.get(event, 0.0)``.  Because the most common
# Statcast out strings (``field_out``, ``force_out``, ...) are NOT canonical
# RUN_VALUES keys, they silently resolved to **0.0 runs** -- so the simulated
# run environment would be badly inflated (outs would cost nothing).
#
# THE FIX
# ~~~~~~~
# An explicit alias map from EVERY Statcast ``events`` value the pool can emit
# to its canonical RUN_VALUES key, plus ``resolve_event_to_canonical`` /
# ``run_value_for_event``, which never silently return 0.0 for a *known* out:
# an unknown event is detectable (``resolve_event_to_canonical`` returns
# ``None``; ``run_value_for_event(..., strict=True)`` raises).
#
# Statcast ``events`` reference vocabulary: pybaseball / Baseball Savant
# ``events`` field (the ``des`` / ``events`` column of Statcast play-by-play).
# ---------------------------------------------------------------------------

#: Statcast raw ``events`` string  ->  canonical 12-outcome ``RUN_VALUES`` key.
#: Covers the full set of *terminal plate-appearance* events Statcast emits.
#: Canonical keys all exist in ``RUN_VALUES`` above (asserted at import below).
STATCAST_EVENT_ALIASES: dict[str, str] = {
    # --- hits ---
    "single": "single",
    "double": "double",
    "triple": "triple",
    "home_run": "home_run",
    # --- reaching base without a hit ---
    "walk": "walk",
    "intent_walk": "intentional_walk",        # IBB (Statcast spelling)
    "intentional_walk": "intentional_walk",   # tolerate canonical spelling too
    "hit_by_pitch": "hit_by_pitch",
    "catcher_interf": "hit_by_pitch",          # reach-on-interference ~ HBP value
    # --- strikeouts ---
    "strikeout": "strikeout",
    "strikeout_double_play": "ground_into_double_play",  # K + runner out
    # --- generic ball-in-play outs (the silently-zeroed bug class) ---
    "field_out": "field_out",
    "force_out": "field_out",
    "fielders_choice": "field_out",
    "fielders_choice_out": "field_out",
    "other_out": "field_out",
    # --- double plays / lost runners ---
    "grounded_into_double_play": "ground_into_double_play",  # Statcast spelling
    "ground_into_double_play": "ground_into_double_play",     # canonical spelling
    "double_play": "ground_into_double_play",
    "triple_play": "ground_into_double_play",  # rarer; >= GIDP severity
    # --- productive / sacrifice outs ---
    "sac_fly": "sacrifice_fly",
    "sac_fly_double_play": "sacrifice_fly",
    "sac_bunt": "sacrifice_hit",
    "sac_bunt_double_play": "sacrifice_hit",
    "sacrifice_fly": "sacrifice_fly",   # tolerate canonical spelling
    "sacrifice_hit": "sacrifice_hit",   # tolerate canonical spelling
    # --- reach-on-error: treat as a single's run value (batter reaches 1B) ---
    "field_error": "single",
}

#: Canonical keys ``resolve_event_to_canonical`` may legitimately return — i.e.
#: the codomain of ``STATCAST_EVENT_ALIASES`` (a subset of ``RUN_VALUES``).
CANONICAL_OUTCOME_KEYS: frozenset[str] = frozenset(RUN_VALUES.keys())

# Import-time invariant: every alias target must be a real RUN_VALUES key, so a
# resolved event can never miss the linear-weight table.
assert set(STATCAST_EVENT_ALIASES.values()) <= CANONICAL_OUTCOME_KEYS, (
    "STATCAST_EVENT_ALIASES maps to a key absent from RUN_VALUES: "
    f"{set(STATCAST_EVENT_ALIASES.values()) - CANONICAL_OUTCOME_KEYS}"
)


class UnknownEventError(KeyError):
    """Raised by ``run_value_for_event`` (strict mode) for an event string that
    is neither a canonical RUN_VALUES key nor a known Statcast alias.

    This is the explicit *detectable* failure mode that replaces the old silent
    ``RUN_VALUES.get(event, 0.0)`` zero -- a known out can never quietly score
    0.0, and a genuinely-unknown token surfaces loudly instead of corrupting
    the run environment.
    """


def resolve_event_to_canonical(event: "str | None") -> "str | None":
    """Map any Statcast ``events`` string (or a canonical key) to its canonical
    ``RUN_VALUES`` key.

    Returns ``None`` for ``None``, the empty string, and unknown / non-terminal
    tokens (e.g. the sim-loop's ``"in_progress"`` marker or the sampler's
    ``"unknown"`` payload).  ``None`` signals that no run value applies (NOT that
    the value is 0.0) -- this is what lets the resolver avoid the silent-zero bug.
    """
    if event is None:
        return None
    key = str(event).strip()
    if not key:
        return None
    if key in CANONICAL_OUTCOME_KEYS:   # already canonical
        return key
    return STATCAST_EVENT_ALIASES.get(key)  # raw Statcast -> canonical, else None


def run_value_for_event(
    event: "str | None",
    *,
    default: "float | None" = None,
    strict: bool = False,
) -> "float | None":
    """Linear-weight run value for a Statcast ``events`` string (or canonical key).

    Resolution order:
      1. Resolve ``event`` to its canonical key via ``resolve_event_to_canonical``.
      2. If it resolves, return ``RUN_VALUES[canonical]`` (a known out can NEVER
         silently return 0.0 -- ``field_out`` etc. now resolve to their true
         negative value).
      3. If it does NOT resolve (unknown / non-terminal token):
           * ``strict=True``  -> raise ``UnknownEventError``;
           * otherwise        -> return ``default`` (``None`` by default, so the
                                  caller can tell "no value" from a real 0.0).

    This is the *context-free* linear-weight fallback.  The preferred,
    context-aware path is ``simulation.run_resolution``, which resolves runs from
    sampled ``result_*`` deltas + the RE24 matrix and only falls back here.
    """
    canonical = resolve_event_to_canonical(event)
    if canonical is not None:
        return float(RUN_VALUES[canonical])
    if strict:
        raise UnknownEventError(
            f"Unknown / non-terminal event {event!r}: not a canonical RUN_VALUES "
            f"key and not in STATCAST_EVENT_ALIASES. Add an alias or pass a "
            f"terminal PA event."
        )
    return default


# ---------------------------------------------------------------------------
# Defensive run values — runs saved per defensive event above average.
# Centralized unchanged from pipeline/batch/player_profile_computor.py.
# ---------------------------------------------------------------------------
DEFENSIVE_RUN_VALUES: dict[str, float] = {
    "runs_per_oaa_infield": 0.75,
    "runs_per_oaa_outfield": 0.90,
    "runs_per_block_saved": 0.25,
    "runs_per_strike_above_avg": 0.125,
}

__all__ = [
    "RUN_VALUES",
    "DEFENSIVE_RUN_VALUES",
    "STATCAST_EVENT_ALIASES",
    "CANONICAL_OUTCOME_KEYS",
    "UnknownEventError",
    "resolve_event_to_canonical",
    "run_value_for_event",
]
