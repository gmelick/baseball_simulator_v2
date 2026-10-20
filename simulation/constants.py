"""The simulator's shared vocabularies and defensive run values.

* ``CANONICAL_OUTCOME_KEYS``: the thirteen plate-appearance outcomes every
  consumer keys on (the ledger's ``canonical_event``, the linescore, the sim
  store, the acceptance bands).
* ``STATCAST_EVENT_ALIASES`` and :func:`resolve_event_to_canonical`: Statcast's
  raw ``events`` strings mapped onto those outcomes, so every sampled event
  resolves.
* ``FIELD_POSITION_NAMES``: the scorekeeping position number -> the position
  name that the fielder embedding and the defense maps key by.
* ``DEFENSIVE_RUN_VALUES``: the runs saved per defensive event above average,
  read by the profile computor.

There is no run value per outcome here, and there must not be one: a play's
value comes from the RE24 matrix over the real base-out states before and after
it (``simulation.run_resolution``; the owner's ruling of 2026-08-19).

The canonical outcomes::

    single, double, triple, home_run,
    walk, intentional_walk, hit_by_pitch,
    strikeout, field_out, ground_into_double_play,
    sacrifice_fly, sacrifice_hit,
    field_error

``field_error`` is a reach on an error. The batter reaches, and it is not a hit.

The defensive run values come from the Statcast research on outs above average
and Tango's framing and blocking conversions::

    runs_per_oaa_infield        0.75   runs per infield out above average
    runs_per_oaa_outfield       0.90   runs per outfield out above average
    runs_per_block_saved        0.25   runs per blocked pitch above average
    runs_per_strike_above_avg   0.125  runs per framing strike above average
"""

from __future__ import annotations

#: The canonical plate-appearance outcomes: mutually exclusive and exhaustive.
CANONICAL_OUTCOME_KEYS: frozenset[str] = frozenset(
    {
        # --- reaching base via a hit ---
        "single",
        "double",
        "triple",
        "home_run",
        # --- reaching base without a hit ---
        "walk",
        "intentional_walk",
        "hit_by_pitch",
        "field_error",  # a reach on an error, never a hit
        # --- outs ---
        "strikeout",
        "field_out",
        "ground_into_double_play",
        "sacrifice_fly",
        "sacrifice_hit",
    }
)

#: Statcast raw ``events`` string -> canonical outcome key, for every terminal
#: plate-appearance event Statcast emits. The play pools store the raw strings
#: (``sim.outcome_pool.events``, ``sim.pitch_pool.events``). Every target is a
#: canonical key (checked at import below).
STATCAST_EVENT_ALIASES: dict[str, str] = {
    # --- hits ---
    "single": "single",
    "double": "double",
    "triple": "triple",
    "home_run": "home_run",
    # --- reaching base without a hit ---
    "walk": "walk",
    "intent_walk": "intentional_walk",  # IBB (Statcast spelling)
    "intentional_walk": "intentional_walk",  # tolerate canonical spelling too
    "hit_by_pitch": "hit_by_pitch",
    "catcher_interf": "hit_by_pitch",  # reach-on-interference ~ HBP value
    # --- strikeouts ---
    "strikeout": "strikeout",
    "strikeout_double_play": "ground_into_double_play",  # K + runner out
    # --- ball-in-play outs ---
    "field_out": "field_out",
    "force_out": "field_out",
    "fielders_choice": "field_out",
    "fielders_choice_out": "field_out",
    "other_out": "field_out",
    # --- double plays / lost runners ---
    "grounded_into_double_play": "ground_into_double_play",  # Statcast spelling
    "ground_into_double_play": "ground_into_double_play",  # canonical spelling
    "double_play": "ground_into_double_play",
    "triple_play": "ground_into_double_play",  # rarer; >= GIDP severity
    # --- productive / sacrifice outs ---
    "sac_fly": "sacrifice_fly",
    "sac_fly_double_play": "sacrifice_fly",
    "sac_bunt": "sacrifice_hit",
    "sac_bunt_double_play": "sacrifice_hit",
    "sacrifice_fly": "sacrifice_fly",  # tolerate canonical spelling
    "sacrifice_hit": "sacrifice_hit",  # tolerate canonical spelling
    # --- a reach on an error: its own outcome, never a hit ---
    "field_error": "field_error",
}

# Every alias target must be a canonical key, so a resolved event never leaves
# the vocabulary.
assert set(STATCAST_EVENT_ALIASES.values()) <= CANONICAL_OUTCOME_KEYS, (
    "STATCAST_EVENT_ALIASES maps to a key absent from CANONICAL_OUTCOME_KEYS: "
    f"{set(STATCAST_EVENT_ALIASES.values()) - CANONICAL_OUTCOME_KEYS}"
)


def resolve_event_to_canonical(event: str | None) -> str | None:
    """Map any Statcast ``events`` string (or a canonical key) to its canonical
    outcome key.

    Returns ``None`` for ``None``, the empty string, and unknown / non-terminal
    tokens (e.g. the sim-loop's ``"in_progress"`` marker or the sampler's
    ``"unknown"`` payload).  ``None`` signals that the token is outside the
    vocabulary — a detectable condition, never a silent default.
    """
    if event is None:
        return None
    key = str(event).strip()
    if not key:
        return None
    if key in CANONICAL_OUTCOME_KEYS:  # already canonical
        return key
    return STATCAST_EVENT_ALIASES.get(key)  # raw Statcast -> canonical, else None


#: The scorekeeping position number (``fielded_by_position`` in the pools) ->
#: the position name. The fielder embedding's keys are
#: ``"{player_id}:{position}:{season}"`` with these names, and a game's
#: defense map is keyed by them. The loop and the sampler share this one table.
FIELD_POSITION_NAMES: dict[int, str] = {
    1: "P",
    2: "C",
    3: "1B",
    4: "2B",
    5: "3B",
    6: "SS",
    7: "LF",
    8: "CF",
    9: "RF",
}

#: Runs saved per defensive event above average (see the module docstring).
DEFENSIVE_RUN_VALUES: dict[str, float] = {
    "runs_per_oaa_infield": 0.75,
    "runs_per_oaa_outfield": 0.90,
    "runs_per_block_saved": 0.25,
    "runs_per_strike_above_avg": 0.125,
}

__all__ = [
    "CANONICAL_OUTCOME_KEYS",
    "DEFENSIVE_RUN_VALUES",
    "FIELD_POSITION_NAMES",
    "STATCAST_EVENT_ALIASES",
    "resolve_event_to_canonical",
]
