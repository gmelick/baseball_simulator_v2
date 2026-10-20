"""simulation.run_resolution — the run value of one play from its two base-out states.

WHAT THIS MODULE DOES
=====================
The run ledger values every committed play by run expectancy. Run expectancy
(RE24) is the expected runs from a base-out state to the end of the inning,
over the 24 states (8 base states × 3 out counts). The run value of a play is
the textbook RE24 identity:

    run_value = RE_end - RE_start + runs_scored

(See Tom Tango's "RE24" / "The Book". ``derived.dp_play_detail`` uses the same
identity as ``re24_start - re24_end`` for the defense's sign.)

A flat per-event weight ignores the base-out state. A single with the bases
loaded and two outs is worth far more than a single with nobody on. A GIDP
(a ground ball into a double play) with a runner on first and no outs costs far
more than a GIDP with the bases empty. The RE24 delta reads the state.

THE CALLER SUPPLIES BOTH STATES
===============================
``resolve_runs(...)`` takes both base-out states. The caller measures the
state before the play and the state after it, and supplies ``pre_outs`` /
``pre_runners_state``, ``post_outs`` / ``post_runners_state`` and
``result_runs``. The run value is ``RE(post) - RE(pre) + result_runs``.

A call without all five raises. No path guesses a missing state or substitutes
a per-event constant (the owner's ruling of 2026-08-19), so a wrong run value
cannot pass silently.

A derived after-state is wrong. The measurements (SIM-499):

  * **Conservation has no term for a runner RETIRED on the play.** The formula
    ``new_on_base = old_on_base + reached - runs`` keeps a caught-stealing
    runner on base: **-0.24 against a true -0.62**. A double play goes wrong
    the same way.
  * **A state read at commit time is already the after-state.** A walk with a
    runner on first reads **+0.50 against a true +0.60**; with the bases
    loaded, **+0.69 against +1.00**. A sac fly reads **+0.76 against a true
    -0.13**, and a steal of home **+1.00 against +0.11**.

The conservation identity runs as a **check** at the commit
(``Bases.assert_transition``): the caller states the retired count, so the
check needs no guess.

``canonical_event`` maps the pool's raw Statcast event (``field_out``,
``force_out``, ``sac_fly``, ``grounded_into_double_play`` ...) to its canonical
key through ``simulation.constants.resolve_event_to_canonical``.

RE24 MATRIX SOURCE
==================
The bundled 24-state matrix (8 base states × 3 out states) is the standard
base-out run-expectancy table for a recent (~2024) MLB run environment
(~4.4 R/team/G). Its values agree with the public Tango / Baseball
Prospectus / Retrosheet RE24 tables for the early 2020s. More baserunners raise
RE; more outs lower it. The simulator uses this bundled matrix.
:func:`re24_from_rows` builds a matrix of the same shape from the
``derived.run_expectancy_matrix`` table (the same ``outs`` × ``runners_state``
bitmask keying), and ``resolve_runs(matrix=...)`` accepts it; no caller
passes one.

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
    ``method``    — always ``"re24_delta"``; the run ledger asserts it.
    ``re_start`` / ``re_end`` — base-out RE before/after.
    ``post_outs`` / ``post_runners_state`` — the after-state the CALLER supplied,
                    echoed back for the record. Nothing here derives it.
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

    The caller measures both states, and this function derives neither of
    them. A derivation by conservation has no term for a runner retired on
    the play (see the module docstring).

    Raises ``ValueError`` when ANY of the five is missing, naming the ones
    that are. A hand-set per-event constant never stands in for real states
    (the owner's ruling of 2026-08-19).

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
