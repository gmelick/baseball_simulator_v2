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

RESOLUTION POLICY — EXPLICIT STATES ONLY (SIM-499; the ONLY path since
2026-08-19)
==================================================
``resolve_runs(...)`` is told **both** base-out states.  The caller measures the
state before the play and the state after it, and hands over both: supply
``pre_outs`` / ``pre_runners_state``, ``post_outs`` / ``post_runners_state``
and ``result_runs``.  The run value is ``RE(post) - RE(pre) + result_runs``.

The old CONTEXT-FREE fallback (a per-event linear-weight constant from the
``RUN_VALUES`` table) was REMOVED with the SIM-511+512 landing (owner ruling
2026-08-19): the ledger always rejected it, and only tests consumed it. A
call without the full state now raises — there is no path that guesses a
missing state or substitutes a constant, so a wrong run value cannot be
produced silently.

WHY THE OLD DERIVATION WAS DELETED (SIM-499)
============================================
This module used to *derive* the after-state from the before-state by
conservation: ``new_on_base = old_on_base + reached - runs``, then place that
many runners on the lead-most bags.  Two defects, both measured:

  * **The formula has no term for a runner RETIRED on the play.**  A caught
    stealing removed a body the formula could not account for, so it kept the
    runner on base and undercharged the play: **-0.24 recorded against a true
    -0.62**.  A double play desynced the same way.
  * **The caller read the state at commit time, after it had already mutated
    the bases.**  The "before" state was therefore the *after* state.  A walk
    with a runner on first recorded **+0.50 against a true +0.60**; with the
    bases loaded, **+0.69 against +1.00**.  A sac fly recorded **+0.76 against a
    true -0.13**, and a steal of home **+1.00 against +0.11**.

Both defects dissolve when the caller supplies the two states it already knows.
The conservation identity survives as a **check** — ``Bases.assert_transition``
(SIM-500), wired at the commit — because a caller can state the retired count
that a derivation had to guess.

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

from simulation.constants import resolve_event_to_canonical

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
# The public resolution API.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RunResolution:
    """Result of resolving a play's run value.

    ``runs``      — the RE24 run value of the play.
    ``method``    — always ``"re24_delta"`` (the linear-weight method was
                    removed 2026-08-19; the field stays so the ledger's
                    method assertion keeps meaning).
    ``re_start`` / ``re_end`` — base-out RE before/after.
    ``post_outs`` / ``post_runners_state`` — the after-state the CALLER supplied,
                    echoed back for the record.  Nothing here derives it
                    (SIM-499).
    ``canonical_event`` — the resolved canonical key (None if unresolved).
    """

    runs: float
    method: str
    re_start: float | None = None
    re_end: float | None = None
    post_outs: int | None = None
    post_runners_state: int | None = None
    canonical_event: str | None = None


#: The five values the RE24 path needs.  All of them, or none of them.
_STATE_ARG_NAMES = (
    "pre_outs",
    "pre_runners_state",
    "post_outs",
    "post_runners_state",
    "result_runs",
)


def resolve_runs(
    *,
    event: str | None = None,
    pre_outs: int | None = None,
    pre_runners_state: int | None = None,
    post_outs: int | None = None,
    post_runners_state: int | None = None,
    result_runs: int | None = None,
    matrix: dict | None = None,
) -> RunResolution:
    """Resolve the run value of one resolved play from its two base-out states.

    Supply all five of ``pre_outs``, ``pre_runners_state``, ``post_outs``,
    ``post_runners_state`` and ``result_runs``::

        runs = RE(post_state) - RE(pre_state) + result_runs

    The caller measures both states.  This function derives neither of them.
    That is the whole point of SIM-499: the old code derived the after-state by
    conservation, which has no term for a runner retired on the play, and the
    caller read the before-state after it had already mutated the bases.

    Raises ``ValueError`` when ANY of the five is missing, naming the ones
    that are.  The context-free linear-weight fallback that once caught a
    state-free call was removed 2026-08-19 (owner ruling): a hand-set
    per-event constant must never stand in for real states.

    Also raises ``ValueError`` on an impossible transition: a negative out count,
    a play that *removes* outs, or a negative ``result_runs``.
    """
    supplied = (pre_outs, pre_runners_state, post_outs, post_runners_state, result_runs)
    missing = [
        name for name, value in zip(_STATE_ARG_NAMES, supplied, strict=True) if value is None
    ]
    if missing:
        given = [n for n in _STATE_ARG_NAMES if n not in missing]
        raise ValueError(
            "resolve_runs: an incomplete base-out state cannot resolve a run "
            f"value (SIM-499). You gave {given!r} and left {missing!r} unset. "
            "Give the state BEFORE the play, the state AFTER it, and the runs "
            "that scored. The context-free linear-weight fallback was removed "
            "2026-08-19 — no constant stands in for real states."
        )

    pre_o, post_o = int(pre_outs), int(post_outs)  # type: ignore[arg-type]
    runs_in = int(result_runs)  # type: ignore[arg-type]
    if pre_o < 0 or post_o < 0:
        raise ValueError(f"resolve_runs: outs cannot be negative (pre={pre_o} post={post_o}).")
    if post_o < pre_o:
        raise ValueError(
            f"resolve_runs: a play cannot remove outs (pre_outs={pre_o} "
            f"post_outs={post_o}). The two states are in the wrong order, or "
            "the caller measured the after-state before the play."
        )
    if runs_in < 0:
        raise ValueError(f"resolve_runs: result_runs cannot be negative ({runs_in}).")
    re_start = re24_value(pre_o, int(pre_runners_state), matrix)  # type: ignore[arg-type]
    re_end = re24_value(post_o, int(post_runners_state), matrix)  # type: ignore[arg-type]
    return RunResolution(
        runs=float(re_end - re_start + float(runs_in)),
        method="re24_delta",
        re_start=re_start,
        re_end=re_end,
        post_outs=post_o,
        post_runners_state=int(post_runners_state) & 0b111,  # type: ignore[operator]
        canonical_event=resolve_event_to_canonical(event),
    )


__all__ = [
    "RE24_MATRIX",
    "OUTS_PER_INNING",
    "RunResolution",
    "re24_value",
    "re24_from_rows",
    "resolve_runs",
]
