"""The Statcast ``events`` out-label vocabulary (SIM-501a).

``raw.pitches.events`` holds the plate-appearance result. It is set on the
final pitch of the plate appearance and NULL on every other row. SIM-501
measured it clean over the full sweep (32 distinct values, all interpretable),
while ``outs_on_pitch`` is zero on 92.6% of batted-ball outs and 100% of
strikeouts in the swept data. Every out label therefore derives from
``events``. Do not read ``outs_on_pitch`` in the profile computor —
``tests/unit/test_sim501a_out_label.py`` fails if any site does.

Two DIFFERENT questions, and the sets that answer them. Do not conflate them:

1. **"How many outs did the play record?"** — :data:`PLAY_OUTS_BY_EVENT` and
   :data:`FIELDING_OUT_EVENTS`. ``fielders_choice_out`` records one out (a
   runner is retired). ``fielders_choice`` records none.
2. **"Was the batter retired?"** — :data:`BATTER_RETIRED_EVENTS`. The batter
   reaches base on BOTH fielders-choice events, and on ``force_out``.

Measured on the live DB, 2024 season (do not re-derive these):

* ``fielders_choice`` rows carry type D/E only — never X. No out is recorded.
* ``fielders_choice_out``: the batter stands on 1B after the play on 90.5%
  of rows. ``force_out``: 98.8%. Both events mean the batter REACHED.
* ``field_out``: the batter is on no base after the play on 100.0% of rows.
* Completed-half-inning identity: SUM(play outs + caught-stealing outs) over
  a completed half equals exactly 3 on 40,780 of 41,542 halves (98.2%).
  The undercount tail (1.6% of halves, ~0.5% of outs) is pickoff outs and
  runner outs the feed anchors off the pitch record — the SIM-502 domain.
  The overcount tail (0.2%) is the uncaught third strike, which Statcast
  scores ``strikeout`` with no out recorded.
* The steal columns (``sb_attempt_*`` / ``sb_success_*``) are FALSE on every
  ``caught_stealing_*`` / ``pickoff_caught_stealing_*`` /
  ``strikeout_double_play`` row — all 3,211 of them, every season 2017-2026.
  :func:`sql_cs_out` also excludes those rows structurally, so the
  no-double-count property does not rest on the feed keeping that promise.
* No in-play row (type X/D/E) carries a steal attempt, so a per-play out
  count over batted balls needs no caught-stealing term.

``simulation/sim_loop.py`` carries ``_OUT_EVENTS`` as the loop's own
"did the batter reach" test. It lists ``fielders_choice`` as an out, which
the measurement above disproves for that question. Reconciling the loop is
sim-scoped work (the SIM-473/SIM-499 redesign), not this module's job;
``test_sim501a_out_label.py`` pins the divergence so it cannot drift
unnoticed.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Question 1 — how many outs did the play record?
# ---------------------------------------------------------------------------

#: Outs recorded on the play, by plate-appearance result. The keys are the
#: full vocabulary measured over the 2017-2026 sweep, plus ``intent_walk``
#: and ``truncated_pa`` (present in the MLB feed vocabulary, absent from the
#: swept rows). ``strikeout_double_play`` carries BOTH outs (the strikeout
#: and the runner), because the steal columns are false on those rows.
PLAY_OUTS_BY_EVENT: dict[str, int] = {
    # one out
    "strikeout": 1,
    "field_out": 1,
    "force_out": 1,
    "fielders_choice_out": 1,
    "other_out": 1,
    "sac_fly": 1,
    "sac_bunt": 1,
    "caught_stealing_2b": 1,
    "caught_stealing_3b": 1,
    "caught_stealing_home": 1,
    "pickoff_caught_stealing_2b": 1,
    "pickoff_caught_stealing_3b": 1,
    "pickoff_caught_stealing_home": 1,
    # two outs
    "grounded_into_double_play": 2,
    "double_play": 2,
    "strikeout_double_play": 2,
    "sac_fly_double_play": 2,
    "sac_bunt_double_play": 2,
    # three outs
    "triple_play": 3,
    # no outs — the batter or a runner reached, or nothing was retired
    "single": 0,
    "double": 0,
    "triple": 0,
    "home_run": 0,
    "walk": 0,
    "intent_walk": 0,
    "hit_by_pitch": 0,
    "field_error": 0,
    "fielders_choice": 0,  # measured: type D/E only, never X — no out
    "catcher_interf": 0,
    "wild_pitch": 0,
    "passed_ball": 0,
    "stolen_base_2b": 0,
    "stolen_base_3b": 0,
    "stolen_base_home": 0,
    "game_advisory": 0,
    "truncated_pa": 0,
}

#: Events that record at least one out on the play. Answers question 1 as a
#: boolean. On in-play rows the caught-stealing members can never appear.
FIELDING_OUT_EVENTS: frozenset[str] = frozenset(e for e, n in PLAY_OUTS_BY_EVENT.items() if n > 0)

# ---------------------------------------------------------------------------
# Question 2 — was the batter retired?
# ---------------------------------------------------------------------------

#: Events where the batter did NOT reach base. ``force_out``,
#: ``fielders_choice`` and ``fielders_choice_out`` are absent on purpose:
#: on all three the batter reaches while (at most) a runner is retired.
#: ``other_out`` is absent because it is a runner out that truncates the
#: plate appearance.
BATTER_RETIRED_EVENTS: frozenset[str] = frozenset(
    {
        "strikeout",
        "strikeout_double_play",
        "field_out",
        "sac_fly",
        "sac_bunt",
        "grounded_into_double_play",
        "double_play",
        "sac_fly_double_play",
        "sac_bunt_double_play",
        "triple_play",
    }
)

# ---------------------------------------------------------------------------
# Pitch `type` codes — ball-in-play row filter
# ---------------------------------------------------------------------------

#: The three in-play `type` codes. X = in play with out(s) and no run;
#: D = in play, no out, no run; E = in play with run(s). X alone is 65% of
#: balls in play, and the missing 35% ARE the hits — filtering on X was the
#: SIM-457 defect.
IN_PLAY_TYPES: tuple[str, ...] = ("X", "D", "E")


#: Events whose out-count on the row already includes an out that the steal
#: columns could in principle also describe. :func:`sql_cs_out` excludes
#: these rows STRUCTURALLY, so the disjointness does not rest on the feed's
#: behavior. (Measured anyway: zero overlap on all 3,211 such rows across
#: every swept season 2017-2026.)
_STEAL_OUT_EVENTS: tuple[str, ...] = (
    "caught_stealing_2b",
    "caught_stealing_3b",
    "caught_stealing_home",
    "pickoff_caught_stealing_2b",
    "pickoff_caught_stealing_3b",
    "pickoff_caught_stealing_home",
    "strikeout_double_play",
)


def sql_list(values: tuple[str, ...] | list[str]) -> str:
    """Render strings as a SQL ``IN`` list: ``('a', 'b')``."""
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def sql_in_play(q: str = "") -> str:
    """Ball-in-play row filter. ``q`` prefixes the column (e.g. ``"rp."``)."""
    return f"{q}type IN {sql_list(IN_PLAY_TYPES)}"


def _outs_groups() -> list[tuple[int, list[str]]]:
    """The out-recording events grouped by out count, descending."""
    groups: dict[int, list[str]] = {}
    for e, n in sorted(PLAY_OUTS_BY_EVENT.items()):
        if n > 0:
            groups.setdefault(n, []).append(e)
    return sorted(groups.items())


def sql_event_outs(q: str = "") -> str:
    """CASE expression: outs recorded by ``events`` alone (0 on NULL).

    One ``IN``-list branch per out count (1, 2, 3), so the common case
    resolves on the first branch instead of walking a 19-way compare chain
    on every row of a full-table scan.
    """
    whens = " ".join(
        f"WHEN {q}events IN {sql_list(members)} THEN {n}" for n, members in _outs_groups()
    )
    return f"CASE {whens} ELSE 0 END"


def sql_hidden_runner_out(q: str = "") -> str:
    """The hidden runner out (0 or 1).

    ``type = 'X'`` MEANS "in play, out(s) recorded" — every X-typed row
    carries at least one out. When ``events`` accounts for none (a runner
    was thrown out advancing on a hit, an error, or an all-safe fielders
    choice), the out lives only in the type code. Measured 2024: 290
    singles, 66 doubles, 3 field errors and 2 catcher interferences typed
    X. This complement form is closed under new vocabulary: an unmapped
    future reach event typed X still scores its out.
    """
    return (
        f"CASE WHEN {q}type = 'X' AND ({q}events IS NULL "
        f"OR {q}events NOT IN {sql_list(sorted(FIELDING_OUT_EVENTS))}) "
        f"THEN 1 ELSE 0 END"
    )


def sql_play_outs(q: str = "") -> str:
    """Outs recorded on the play: the events term + the hidden runner out.

    This is the per-play answer to question 1. It has no caught-stealing
    term, so use it where the row set is in-play only (steal attempts never
    occur on in-play pitches — measured, 2024: zero rows).
    """
    return f"(({sql_event_outs(q)}) + ({sql_hidden_runner_out(q)}))"


def sql_cs_out(q: str = "") -> str:
    """A mid-plate-appearance caught stealing on this pitch (0 or 1).

    The row guard makes the no-double-count property STRUCTURAL: rows whose
    ``events`` already counts a steal or pickoff out (see
    ``_STEAL_OUT_EVENTS``) contribute nothing here, whatever the steal
    columns say. On the swept data the guard never fires — the steal
    columns are FALSE on all 3,211 such rows across 2017-2026 — but the
    formula no longer depends on the feed keeping that promise.
    """
    return (
        f"CASE WHEN (COALESCE({q}sb_attempt_2b, FALSE) "
        f"OR COALESCE({q}sb_attempt_3b, FALSE) "
        f"OR COALESCE({q}sb_attempt_home, FALSE)) "
        f"AND NOT COALESCE({q}sb_success_2b, FALSE) "
        f"AND NOT COALESCE({q}sb_success_3b, FALSE) "
        f"AND NOT COALESCE({q}sb_success_home, FALSE) "
        f"AND ({q}events IS NULL OR {q}events NOT IN {sql_list(_STEAL_OUT_EVENTS)}) "
        f"THEN 1 ELSE 0 END"
    )


def sql_outs_recorded(q: str = "") -> str:
    """Total outs recorded on this row: play outs + caught-stealing outs.

    This is the innings-pitched building block (SIM-501c): SUM it per
    pitcher. Known residual, measured on 2024: ~0.5% of real outs are
    invisible here — pickoff outs and feed-displaced runner outs live in
    no ``raw.pitches`` row (SIM-502) — and the uncaught third strike
    over-counts by ~0.06%. The column this replaces missed ~36%.
    """
    return f"({sql_play_outs(q)} + ({sql_cs_out(q)}))"


#: SIM-502c — the measured residual of the IP formula, so nobody re-derives it.
#: Over 357 games / 19,072 outs, 131 out-movements were keyed to a non-pitch
#: index. 53 pickoffs live in `raw.play_events` — and since SIM-504 the two
#: profile `outs_recorded` consumers ADD them from that table (the computor's
#: `_play_events_outs_cte`), closing most of what was a ~0.5% residual at the
#: next recompute; 63 mid-PA caught stealings live in the `sb_*` columns (the
#: CS term above counts them); 7 displaced batter strikeouts live in `events`
#: (the terms above count them). The ~8 runner outs anchored to
#: `other_out`/`wild_pitch` actions that do not end the PA reach no table:
#: ~0.04% of all outs, accepted. With the ~0.06% uncaught-third-strike
#: over-count, the formula reads 3 outs on 98.2% of completed half-innings.


# ---------------------------------------------------------------------------
# Transition destinations (SIM-510)
# ---------------------------------------------------------------------------
# The fielding-transition draw (SIM-511) applies a drawn pool row's whole
# base-state transition, so the pool must encode where each body ended.
# `raw.pitches` carries the truth: the ETL parser initializes the post-play
# seats from the pre-play seats, re-seats every runner ``movement.end``,
# and clears a scored or retired runner. A stranded runner therefore keeps
# his base — an inning-ending third out never mislabels the survivors.
#
# Destination encoding (one SMALLINT per body):
#   NULL = no runner on that base pre-pitch (runner columns only)
#   4    = scored
#   3/2/1 = the post-play base the body ended on
#   0    = retired on the play

TRANSITION_BASES = ("1b", "2b", "3b")


def sql_runner_dest(base: str, q: str = "") -> str:
    """CASE expression: the destination of the pre-pitch runner on ``base``.

    The scored flag is checked FIRST — it is the official scoring, so run
    timing (Rule 5.08) rides along for free. A runner found on no post-play
    base who did not score was retired (the parser clears exactly those).
    """
    if base not in TRANSITION_BASES:
        raise ValueError(f"base must be one of {TRANSITION_BASES}, got {base!r}")
    r = f"{q}on_{base}"
    return (
        f"CASE WHEN {r} IS NULL THEN NULL "
        f"WHEN COALESCE({q}runner_{base}_scored, FALSE) THEN 4 "
        f"WHEN {q}post_on_3b = {r} THEN 3 "
        f"WHEN {q}post_on_2b = {r} THEN 2 "
        f"WHEN {q}post_on_1b = {r} THEN 1 "
        f"ELSE 0 END"
    )


def sql_batter_dest(q: str = "", batter_expr: str | None = None) -> str:
    """CASE expression: the batter-runner's destination (0 = out).

    ``batter_expr`` overrides the batter-id column (e.g. ``"pp.batter_id"``
    when the batter id lives on a joined table). The non-HR scoring branch
    is runs accounting: when the play's runs exceed the named runner-scored
    flags, the extra run is the batter's own trip around the bases.
    """
    b = batter_expr if batter_expr is not None else f"{q}batter"
    return (
        f"CASE WHEN {q}post_on_1b = {b} THEN 1 "
        f"WHEN {q}post_on_2b = {b} THEN 2 "
        f"WHEN {q}post_on_3b = {b} THEN 3 "
        f"WHEN {q}events = 'home_run' THEN 4 "
        f"WHEN COALESCE({q}runs_on_pitch, 0) > "
        f"(COALESCE({q}runner_1b_scored, FALSE)::INT "
        f"+ COALESCE({q}runner_2b_scored, FALSE)::INT "
        f"+ COALESCE({q}runner_3b_scored, FALSE)::INT) THEN 4 "
        f"ELSE 0 END"
    )


def sql_fielding_out(q: str = "") -> str:
    """Boolean: the play recorded at least one out (question 1)."""
    return f"{q}events IN {sql_list(sorted(FIELDING_OUT_EVENTS))}"


def sql_batter_retired(q: str = "") -> str:
    """Boolean: the batter was retired (question 2)."""
    return f"{q}events IN {sql_list(sorted(BATTER_RETIRED_EVENTS))}"


# ---------------------------------------------------------------------------
# Steal-attempt labels (SIM-506)
# ---------------------------------------------------------------------------
# A steal outcome lives in TWO disjoint places in raw.pitches:
#
#   * the ``sb_attempt_*`` / ``sb_success_*`` columns — a MID-plate-appearance
#     steal (the PA continues after the play);
#   * the ``events`` column — a steal that ENDS the plate appearance
#     (``caught_stealing_2b`` etc.; the columns stay FALSE on those rows).
#
# Measured on the full swept data (2026-08-17): the overlap is exactly ZERO.
# The asymmetry is total: a caught stealing ends a PA routinely (2024, 2B:
# 330 column CS + 249 event-only CS — 43% of caught stealings live only in
# ``events``), but a successful steal almost never does (2024: 3 event SB
# against 2,773 column SB). A consumer that reads the columns alone therefore
# inflates every steal SUCCESS rate by ~5-7 points — that defect shipped in
# the SIM-468 opportunity pool and every steal-feature builder (SIM-506).
# Use these two helpers at EVERY site that labels a steal attempt or outcome;
# do not write a third definition.
#
# Known, accepted residual: a caught stealing folded into a
# ``strikeout_double_play`` names no base, so it is not attributable to a
# target and stays outside these labels (≤ ~98 events/season, ~2.5% of DPs).

STEAL_BASES = ("2b", "3b", "home")


def sql_steal_attempt(base: str, q: str = "") -> str:
    """Boolean: a steal of ``base`` was attempted on this pitch row.

    NULL-safe: a NULL ``events`` (any mid-PA pitch) must read FALSE, not
    NULL — ``FALSE OR NULL`` is NULL and poisons NOT NULL label columns.
    """
    if base not in STEAL_BASES:
        raise ValueError(f"base must be one of {STEAL_BASES}, got {base!r}")
    return (
        f"(COALESCE({q}sb_attempt_{base}, FALSE) OR COALESCE("
        f"{q}events IN ('caught_stealing_{base}', 'stolen_base_{base}'), FALSE))"
    )


def sql_steal_success(base: str, q: str = "") -> str:
    """Boolean: an attempted steal of ``base`` succeeded on this pitch row.

    NULL-safe like :func:`sql_steal_attempt`.
    """
    if base not in STEAL_BASES:
        raise ValueError(f"base must be one of {STEAL_BASES}, got {base!r}")
    return (
        f"(COALESCE({q}sb_success_{base}, FALSE) OR "
        f"COALESCE({q}events = 'stolen_base_{base}', FALSE))"
    )


# ---------------------------------------------------------------------------
# Python-side helpers for the pandas paths
# ---------------------------------------------------------------------------


def play_outs(event: object, type_code: object = None) -> int:
    """Outs recorded on the play (question 1). NULL-safe.

    Pass the row's ``type`` code as well to count the hidden runner out
    (``type='X'`` guarantees at least one out); omit it where `type` is not
    selected.
    """
    outs = PLAY_OUTS_BY_EVENT.get(event, 0) if isinstance(event, str) else 0
    if type_code == "X" and outs == 0:
        outs = 1
    return outs


def batter_retired(event: object) -> bool:
    """True when the batter did not reach base (question 2). NULL-safe."""
    return isinstance(event, str) and event in BATTER_RETIRED_EVENTS
