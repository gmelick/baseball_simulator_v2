"""simulation.run_resolution — context-aware run resolution for the sim loop (SIM-312).

WHY THIS MODULE EXISTS
======================
The Phase-3 scaffold (``simulation/sim_loop.py``) scored a plate appearance with
a context-free linear-weight lookup: ``RUN_VALUES.get(event, 0.0)``.  Two
problems (the SIM-312 P0 live bug):

  1. **Vocabulary mismatch** — the play pool stores Statcast's *raw* ``events``
     strings (``field_out``, ``force_out``, ``sac_fly``,
     ``grounded_into_double_play`` ...), which are NOT canonical ``RUN_VALUES``
     keys, so the most common outs silently scored **0.0 runs** and inflated the
     run environment.  (Fixed in ``simulation/constants.py`` via an alias map +
     resolver.)
  2. **Context-blindness** — even with the right key, a flat linear weight ignores
     the base/out state.  A single with the bases loaded and two outs is worth
     far more than a single with nobody on; a GIDP with a runner on first and no
     outs costs far more than one in an empty-bases context.

This module makes run resolution **context-aware** by using the data the play
pool already samples — the per-play result deltas ``result_hits``,
``result_outs``, ``result_runs`` (see ``sim.outcome_pool`` in
``db/schemas/02_duckdb_schema.sql``) — together with an **RE24** (base-out
run-expectancy) matrix.  The change in run expectancy over a play, plus runs
that physically scored, is the textbook RE24 run value:

    run_value = RE_end - RE_start + runs_scored

(See Tom Tango's "RE24" / "The Book"; the same identity is already used in this
codebase's ``derived.dp_play_detail`` as ``re24_start - re24_end`` for the
defense's sign, and the ``derived.run_expectancy_matrix`` table stores exactly
this 24-state matrix.)

RESOLUTION POLICY (in priority order)
=====================================
``resolve_runs(...)`` resolves a play's run value as follows:

  1. **PRIMARY — sampled deltas + RE24.**  If the sampled play carries
     ``result_hits`` / ``result_outs`` / ``result_runs`` (the columns the play
     pool stores), advance the base-out state by those deltas and return the
     RE24 run value above plus the runs that scored.  This is fully
     context-aware (uses the *actual* outs/bases at the start of the PA).
  2. **FALLBACK — linear weights.**  When the deltas are unavailable (e.g. a
     no-contact PA that the count machine resolved to ``walk``/``strikeout``, or
     a pool row missing result columns), fall back to the context-free linear
     weight via ``simulation.constants.run_value_for_event``.

A "known out" therefore can never silently score 0.0: the delta path scores the
out via RE24, and the fallback path resolves the Statcast alias to its true
(negative) linear weight.  An unknown event surfaces (``strict=True``) rather
than corrupting the run total.

RE24 MATRIX SOURCE
==================
The bundled 24-state matrix (8 base states × 3 out states) is the standard
base-out run-expectancy table for a recent (~2024) MLB run environment
(~4.4 R/team/G).  Values are consistent with the public Tango / Baseball
Prospectus / Retrosheet RE24 tables for the early-2020s run environment and are
monotonic in the expected ways (more baserunners ⇒ higher RE; more outs ⇒ lower
RE).  In production this can be swapped for the live
``derived.run_expectancy_matrix`` table (same ``outs`` × ``runners_state``
bitmask keying) via :func:`re24_from_rows`; the bundled table is the offline /
unit-test default and the safety net when the DB table is empty.

Base-out state encoding (matches the rest of the codebase)
----------------------------------------------------------
``runners_state`` is the 3-bit bitmask used by ``sim.pitch_pool`` /
``derived.run_expectancy_matrix``:

    bit0 = runner on 1B, bit1 = runner on 2B, bit2 = runner on 3B
    0=empty 1=1B 2=2B 3=1B+2B 4=3B 5=1B+3B 6=2B+3B 7=loaded
"""

from __future__ import annotations

from dataclasses import dataclass

from simulation.constants import resolve_event_to_canonical, run_value_for_event

# ---------------------------------------------------------------------------
# RE24 — base-out run-expectancy matrix.
# Keyed (outs, runners_state) -> expected runs to end of inning.
# ~2024 MLB run environment (~4.4 R/team/G).  Standard Tango/Retrosheet-style
# RE24; monotonic in baserunners (↑) and outs (↓).  Mirrors the 24-state
# structure of derived.run_expectancy_matrix (outs 0-2 × runners_state 0-7).
# ---------------------------------------------------------------------------
RE24_MATRIX: dict[tuple[int, int], float] = {
    # outs = 0
    (0, 0): 0.51,  # empty
    (0, 1): 0.89,  # 1B
    (0, 2): 1.16,  # 2B
    (0, 3): 1.49,  # 1B+2B
    (0, 4): 1.40,  # 3B
    (0, 5): 1.78,  # 1B+3B
    (0, 6): 1.99,  # 2B+3B
    (0, 7): 2.30,  # loaded
    # outs = 1
    (1, 0): 0.27,
    (1, 1): 0.53,
    (1, 2): 0.69,
    (1, 3): 0.91,
    (1, 4): 0.96,
    (1, 5): 1.16,
    (1, 6): 1.39,
    (1, 7): 1.55,
    # outs = 2
    (2, 0): 0.11,
    (2, 1): 0.23,
    (2, 2): 0.32,
    (2, 3): 0.44,
    (2, 4): 0.37,
    (2, 5): 0.50,
    (2, 6): 0.58,
    (2, 7): 0.75,
}

#: Number of outs that ends a half-inning (and zeroes the carry-over RE).
OUTS_PER_INNING = 3

# Bitmask helpers for the 3-base occupancy state.
_BIT_1B = 0b001
_BIT_2B = 0b010
_BIT_3B = 0b100


def re24_value(outs: int, runners_state: int, matrix: dict | None = None) -> float:
    """RE for a base-out state.  ≥3 outs ⇒ 0.0 (inning over, no carry-over RE)."""
    if outs >= OUTS_PER_INNING:
        return 0.0
    m = matrix if matrix is not None else RE24_MATRIX
    o = int(outs)
    rs = int(runners_state) & 0b111
    if (o, rs) not in m:
        raise KeyError(f"RE24 matrix has no entry for (outs={o}, runners_state={rs}).")
    return float(m[(o, rs)])


def re24_from_rows(rows: list[tuple[int, int, float]]) -> dict[tuple[int, int], float]:
    """Build an RE24 matrix dict from ``(outs, runners_state, expected_runs)`` rows.

    Convenience for loading ``derived.run_expectancy_matrix`` (its three keying
    columns) into the shape :func:`re24_value` expects.  Validates that all 24
    base-out states are present.
    """
    matrix = {(int(o), int(rs) & 0b111): float(er) for o, rs, er in rows}
    missing = {(o, rs) for o in range(OUTS_PER_INNING) for rs in range(8)} - set(matrix.keys())
    if missing:
        raise ValueError(f"RE24 rows are missing base-out states: {sorted(missing)}")
    return matrix


# ---------------------------------------------------------------------------
# Base-out state advancement from sampled result_* deltas.
# ---------------------------------------------------------------------------


def _popcount(runners_state: int) -> int:
    return bin(int(runners_state) & 0b111).count("1")


def advance_state(
    outs: int,
    runners_state: int,
    *,
    result_hits: int,
    result_outs: int,
    result_runs: int,
) -> tuple[int, int]:
    """Advance a base-out state by the sampled per-play deltas.

    Returns ``(new_outs, new_runners_state)``.

    The play pool stores ``result_hits`` (0-4: 0=out, 1=1B, 2=2B, 3=3B, 4=HR),
    ``result_outs`` (outs recorded on the play), and ``result_runs`` (runs that
    scored).  We don't have per-runner identities here, so we apply a standard,
    deterministic base-advancement convention that is consistent with the
    ``result_runs`` the pool already recorded:

      * The number of runners physically on base after the play is fixed by
        conservation:  ``new_on_base = old_on_base + reached - runs_scored``
        where ``reached`` is 1 when the batter reached (hit value 1-4 → on a HR
        the batter also scores so does not stay on base), else 0.
      * We then *place* those ``new_on_base`` runners on the lead-most bases
        (3B, then 2B, then 1B) — the closest base-occupancy summary available
        without tracking runner identity.  This keeps the RE24 lookup well-defined
        and conservative; full per-runner advancement is SIM-319's job.

    If the half-inning ends (outs reach 3), the returned runners_state is 0.
    """
    outs = int(outs)
    rs = int(runners_state) & 0b111
    hits = int(result_hits)
    d_outs = int(result_outs)
    runs = int(result_runs)

    new_outs = outs + d_outs
    if new_outs >= OUTS_PER_INNING:
        return OUTS_PER_INNING, 0  # inning over: bases cleared, no carry RE

    reached = 1 if (1 <= hits <= 3) else 0  # batter stays on base only for 1B/2B/3B
    old_on_base = _popcount(rs)
    new_on_base = old_on_base + reached - runs
    # Clamp into the physically-possible [0, 3] (defensive against odd pool rows).
    new_on_base = max(0, min(3, new_on_base))

    # Place runners on the lead-most bases (3B first), a base-occupancy summary.
    new_rs = 0
    if new_on_base >= 1:
        new_rs |= _BIT_3B
    if new_on_base >= 2:
        new_rs |= _BIT_2B
    if new_on_base >= 3:
        new_rs |= _BIT_1B
    return new_outs, new_rs


# ---------------------------------------------------------------------------
# The public resolution API.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RunResolution:
    """Result of resolving a play's run value.

    ``runs``      — the RE24 (or linear-weight) run value of the play.
    ``method``    — ``"re24_delta"`` (primary) or ``"linear_weight"`` (fallback).
    ``re_start`` / ``re_end`` — base-out RE before/after (None for the fallback).
    ``new_outs`` / ``new_runners_state`` — resolved post-play state (None for fallback).
    ``canonical_event`` — the resolved canonical key (None if unresolved).
    """

    runs: float
    method: str
    re_start: float | None = None
    re_end: float | None = None
    new_outs: int | None = None
    new_runners_state: int | None = None
    canonical_event: str | None = None


def resolve_runs(
    *,
    event: str | None = None,
    outs: int | None = None,
    runners_state: int | None = None,
    result_hits: int | None = None,
    result_outs: int | None = None,
    result_runs: int | None = None,
    matrix: dict | None = None,
    strict: bool = False,
) -> RunResolution:
    """Resolve the run value of one resolved play.

    PRIMARY (context-aware): when the base-out state (``outs``, ``runners_state``)
    AND the sampled deltas (``result_hits``/``result_outs``/``result_runs``) are
    all provided, compute the RE24 run value:

        runs = RE(end_state) - RE(start_state) + result_runs

    FALLBACK (context-free): otherwise use the linear-weight value of ``event``
    via ``simulation.constants.run_value_for_event`` (which itself resolves the
    Statcast alias → canonical key, so a known out is never silently 0.0).

    ``strict=True`` makes the fallback raise on an unknown event instead of
    returning 0.0.

    Raises ``ValueError`` if neither path can produce a value (no deltas AND no
    resolvable event) — there is no silent-zero outcome.
    """
    have_deltas = (
        outs is not None
        and runners_state is not None
        and result_hits is not None
        and result_outs is not None
        and result_runs is not None
    )

    if have_deltas:
        re_start = re24_value(outs, runners_state, matrix)
        new_outs, new_rs = advance_state(
            outs,
            runners_state,
            result_hits=int(result_hits),
            result_outs=int(result_outs),
            result_runs=int(result_runs),
        )
        re_end = re24_value(new_outs, new_rs, matrix)
        runs = re_end - re_start + float(int(result_runs))
        return RunResolution(
            runs=float(runs),
            method="re24_delta",
            re_start=re_start,
            re_end=re_end,
            new_outs=new_outs,
            new_runners_state=new_rs,
            canonical_event=resolve_event_to_canonical(event),
        )

    # ---- fallback: context-free linear weight ----
    lw = run_value_for_event(event, default=None, strict=strict)
    if lw is None:
        raise ValueError(
            "resolve_runs: cannot resolve runs — no sampled result_* deltas were "
            f"provided AND event {event!r} is not a known PA outcome. Provide "
            "deltas + base-out state, or a resolvable terminal event."
        )
    return RunResolution(
        runs=float(lw),
        method="linear_weight",
        canonical_event=resolve_event_to_canonical(event),
    )


__all__ = [
    "RE24_MATRIX",
    "OUTS_PER_INNING",
    "RunResolution",
    "re24_value",
    "re24_from_rows",
    "advance_state",
    "resolve_runs",
]
