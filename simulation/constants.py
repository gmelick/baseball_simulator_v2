"""simulation.constants — the canonical outcome vocabulary + defensive run
values (SIM-202, SIM-312, SIM-511).

This module is the single home for:

  * ``CANONICAL_OUTCOME_KEYS`` — the canonical plate-appearance outcome
                              vocabulary every consumer keys on (the ledger's
                              ``canonical_event``, the linescore, the sim
                              store, the acceptance bands).
  * ``DEFENSIVE_RUN_VALUES`` — defensive run conversions used by the
                              fielder / catcher engines (runs saved per out,
                              per block, per framing strike).
  * ``STATCAST_EVENT_ALIASES`` + ``resolve_event_to_canonical`` — SIM-312:
    map Statcast's raw ``events`` vocabulary onto the canonical keys so the
    play pool's sampled events always resolve.

THE LINEAR-WEIGHT NUMBERS WERE REMOVED (owner ruling 2026-08-19, the
SIM-511+512 landing). The old ``RUN_VALUES`` table carried a hand-set average
run value per outcome. Production never read them: the ledger accepts only
the RE24-delta method (real pre/post base-out states plus the runs that
scored — ``simulation.run_resolution``), and the context-free fallback was
consumed by tests alone (verified 2026-08-19). Only the KEYS were load-
bearing, so the keys are what remains. Do not reintroduce a run-value table;
a play's value comes from the RE24 matrix and real states, never a constant.

-------------------------------------------------------------------------------
CANONICAL OUTCOME VOCABULARY  (``CANONICAL_OUTCOME_KEYS``)
-------------------------------------------------------------------------------
The simulator models a plate appearance as resolving to exactly one of these
mutually-exclusive, collectively-exhaustive outcomes (the standard
Retrosheet/Statcast PA-outcome partition, plus ``field_error``):

    single, double, triple, home_run,
    walk, intentional_walk, hit_by_pitch,
    strikeout, field_out, ground_into_double_play,
    sacrifice_fly, sacrifice_hit,
    field_error

``field_error`` became its own canonical outcome with the SIM-511 landing
(the SIM-496 fix). It aliased to ``single`` before — "treat as a single's
run value" — which credited a drawn reach-on-error as a HIT in the boxscore
while the loop retired the batter on the bases. The batter reaches on a
``field_error`` and it is NOT a hit; the linescore keys on this vocabulary.

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
# The canonical plate-appearance outcome vocabulary.
# The 12 standard outcomes plus ``field_error`` (SIM-496/511 — the batter
# reaches on an error and it is NOT a hit). Every consumer keys on this set;
# the play's VALUE comes from simulation.run_resolution (RE24 over real
# states), never from a per-outcome constant (owner ruling 2026-08-19).
# ---------------------------------------------------------------------------
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
        "field_error",  # SIM-511: its own outcome — a reach, never a hit
        # --- outs ---
        "strikeout",
        "field_out",
        "ground_into_double_play",
        "sacrifice_fly",
        "sacrifice_hit",
    }
)

# ---------------------------------------------------------------------------
# SIM-312 — Statcast raw ``events`` vocabulary  ->  canonical outcome key.
#
# The play pool stores Statcast's *raw* ``events`` strings
# (``sim.outcome_pool.events`` / ``sim.pitch_pool.events``) sampled verbatim.
# Statcast's vocabulary is far larger than the canonical partition:
# ``field_out``, ``force_out``, ``fielders_choice``, ``sac_fly``,
# ``grounded_into_double_play``, ``double_play``, ``intent_walk`` ...
#
# SIM-312 history: the loop once scored runs by ``RUN_VALUES.get(event, 0.0)``
# with mismatched spellings, so the most common outs silently scored 0.0 and
# inflated the run environment. The alias map + ``resolve_event_to_canonical``
# fixed the vocabulary half; the value half is gone entirely — the ledger
# resolves every play by RE24 over real states (``simulation.run_resolution``)
# and rejects anything else.
#
# Statcast ``events`` reference vocabulary: pybaseball / Baseball Savant
# ``events`` field (the ``des`` / ``events`` column of Statcast play-by-play).
# ---------------------------------------------------------------------------

#: Statcast raw ``events`` string  ->  canonical outcome key.
#: Covers the full set of *terminal plate-appearance* events Statcast emits.
#: Alias targets all exist in ``CANONICAL_OUTCOME_KEYS`` (asserted at import).
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
    # --- generic ball-in-play outs (the silently-zeroed bug class) ---
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
    # --- reach-on-error: its own canonical outcome (SIM-496/511) ---
    # This mapped to "single" until 2026-08-19, which credited a drawn
    # reach-on-error as a HIT. The batter reaches; it is not a hit.
    "field_error": "field_error",
}

# Import-time invariant: every alias target must be a canonical outcome key,
# so a resolved event can never leave the vocabulary.
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
    "DEFENSIVE_RUN_VALUES",
    "STATCAST_EVENT_ALIASES",
    "CANONICAL_OUTCOME_KEYS",
    "resolve_event_to_canonical",
]
