"""The Statcast code sets and their SQL: the outs, the pitch results, the runners and the steals.

``raw.pitches.events`` holds the plate-appearance result. It is set on the
final pitch of the plate appearance and NULL on every other row. Every out
label derives from ``events``: ``outs_on_pitch`` is zero on most batted-ball
outs and on every strikeout. Do not read ``outs_on_pitch`` in the profile
computor; ``tests/unit/ingest/test_out_label.py`` fails if any site does.

Two DIFFERENT questions, and the sets that answer them. Do not mix them up:

1. **"How many outs did the play record?"** — :data:`PLAY_OUTS_BY_EVENT` and
   :data:`FIELDING_OUT_EVENTS`. ``fielders_choice_out`` records one out (a
   runner is retired). ``fielders_choice`` records none.
2. **"Was the batter retired?"** — :data:`BATTER_RETIRED_EVENTS`. The batter
   reaches base on BOTH fielders-choice events, and on ``force_out``.

The facts behind the sets, read from the stored data (do not re-derive them):

* A ``fielders_choice`` row has type D or E, never X: no out is recorded.
* After ``fielders_choice_out`` and ``force_out``, the batter stands on first
  on nearly every row: the batter reached.
* After ``field_out``, the batter is on no base.
* The steal columns (``sb_attempt_*``, ``sb_success_*``) are FALSE on every
  ``caught_stealing_*``, ``pickoff_caught_stealing_*`` and
  ``strikeout_double_play`` row. :func:`sql_cs_out` also excludes those rows,
  so the no-double-count property does not rest on the feed keeping that promise.
* No in-play row (type X, D or E) carries a steal attempt, so an out count over
  batted balls needs no caught-stealing term.
* :func:`sql_outs_recorded` reads exactly 3 outs on about 98% of completed
  half-innings; its docstring names the outs it misses. It over-counts the
  uncaught third strike, which Statcast scores ``strikeout`` with no out.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Question 1 — how many outs did the play record?
# ---------------------------------------------------------------------------

#: Outs recorded on the play, by plate-appearance result. The keys are every
#: value ``raw.pitches.events`` holds, plus ``intent_walk`` and ``truncated_pa``
#: from the MLB feed's vocabulary. A new event needs an entry here AND in
#: ``MEASURED_VOCABULARY`` of ``tests/unit/ingest/test_out_label.py``.
#: ``strikeout_double_play`` carries BOTH outs (the strikeout and the runner):
#: the steal columns are false on those rows, so no other term counts the runner.
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
    "fielders_choice": 0,  # type D or E only, never X: no out
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
#: ``fielders_choice`` and ``fielders_choice_out`` are absent on purpose: on
#: all three the batter reaches, and at most a runner is retired.
#: ``other_out`` is absent because it is a runner out that cuts the plate
#: appearance short.
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
# Pitch `type` codes — the one pitch-result vocabulary
# ---------------------------------------------------------------------------
# `raw.pitches.type` holds the feed's pitch-result code, `details.code`.
# `raw.pitches.description` holds the display text of the same code ("Called
# Strike", "In play, out(s)"). It never holds Statcast's snake_case names
# ("called_strike"), so a filter on `description` matches no row. Filter on
# `type`.
#
# The pool build (SQL_OUTCOME_TYPE) and the Data Lab routes render their
# filters from these sets; the pitcher profile reads SQL_WHIFF and SQL_SWING.
# Two profile sites still spell a code list inline: the pitcher's csw_rate and
# the batter profile's swing list. A test pins that text. The sets live here,
# not in the profile computor, so the API reads them without the computor's
# duckdb and sklearn imports. `tests/unit/test_pitch_result_vocabulary.py`
# holds the 17 (description, code) pairs that the loader stores.

#: A ball: 'B', '*B' a ball in the dirt, 'P' a pitchout the batter took.
BALL_TYPES: tuple[str, ...] = ("B", "*B", "P")

#: A called strike: the umpire called it; the batter did not swing.
CALLED_STRIKE_TYPES: tuple[str, ...] = ("C",)

#: A swing and a miss: 'S' swinging strike, 'W' swinging strike the catcher did
#: not hold, 'M' missed bunt, 'Q' a swing and a miss at a pitchout. A foul tip
#: ('T', 'O') is CONTACT, because the bat touches the ball, so the set excludes
#: it: the pool build codes it a foul below two strikes and strike three at two
#: (STRIKE_THREE_FOUL_TYPES).
WHIFF_TYPES: tuple[str, ...] = ("S", "W", "M", "Q")

#: The bat touched the ball and the ball was not put in play: 'F' foul, 'T' foul
#: tip, 'L' foul bunt, 'O' a foul tip on a bunt, 'R' foul pitchout. Each adds a
#: strike below two strikes.
FOUL_TYPES: tuple[str, ...] = ("F", "T", "L", "O", "R")

#: The fouls that are STRIKE THREE with two strikes. The rulebook makes a foul
#: tip (a foul the catcher holds) a strike at any count. A bunt fouled off on
#: the third strike is a strikeout (Rule 5.09(a)(4)). A plain foul ('F', 'R')
#: with two strikes is not. A pool that codes these as a foul or a ball pitches
#: on after a real strikeout, and the simulator loses strikeouts.
STRIKE_THREE_FOUL_TYPES: tuple[str, ...] = ("T", "O", "L")

#: The codes where the bat never left the shoulder: ball, called strike, hit by
#: pitch, pitchout, blocked ball. A swing is the complement of this set, so the
#: set gives the denominator of every swing-conditioned rate.
NON_SWING_TYPES: tuple[str, ...] = ("B", "C", "H", "P", "*B")

#: The three in-play `type` codes. X = in play with out(s) and no run;
#: D = in play, no out, no run; E = in play with run(s). Nearly every hit is
#: typed D or E, so a filter on X alone loses the hits.
IN_PLAY_TYPES: tuple[str, ...] = ("X", "D", "E")

#: The foul tips: the bat touches the ball and the catcher holds it. 'T' is
#: the foul tip; 'O' is the feed's foul tip on a bunt. The model counts a foul
#: tip as contact, so WHIFF_TYPES excludes them. Baseball Savant (the public
#: Statcast site) counts a foul tip as a whiff, and the Data Lab shows Savant's
#: whiff.
FOUL_TIP_TYPES: tuple[str, ...] = ("T", "O")

#: Savant's whiff: the model's whiff plus the foul tips. The Data Lab reads
#: this set; the profiles and the pool build never do. It renders as
#: ('S', 'W', 'M', 'Q', 'T', 'O').
SAVANT_WHIFF_TYPES: tuple[str, ...] = WHIFF_TYPES + FOUL_TIP_TYPES


#: The events whose out count already holds an out the steal columns could
#: also describe. :func:`sql_cs_out` excludes these rows, so the two never
#: count one out twice, whatever the feed does.
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


def sql_swing(q: str = "") -> str:
    """Swing row filter: the bat left the shoulder. ``q`` prefixes the column."""
    return f"{q}type NOT IN {sql_list(NON_SWING_TYPES)}"


def sql_savant_whiff(q: str = "") -> str:
    """Savant's whiff row filter: a swing and a miss, or a foul tip.

    Only the Data Lab reads it. The model's whiff is ``WHIFF_TYPES``.
    """
    return f"{q}type IN {sql_list(SAVANT_WHIFF_TYPES)}"


def sql_called_strike(q: str = "") -> str:
    """Called-strike row filter: the umpire called a strike on a taken pitch."""
    return f"{q}type IN {sql_list(CALLED_STRIKE_TYPES)}"


def sql_csw(q: str = "") -> str:
    """CSW row filter: a called strike or a Savant whiff.

    CSW means "called strikes plus whiffs". Its denominator is every pitch.
    """
    return f"({sql_called_strike(q)} OR {sql_savant_whiff(q)})"


def _outs_groups() -> list[tuple[int, list[str]]]:
    """The events that record an out, grouped by out count, in ascending order."""
    groups: dict[int, list[str]] = {}
    for e, n in sorted(PLAY_OUTS_BY_EVENT.items()):
        if n > 0:
            groups.setdefault(n, []).append(e)
    return sorted(groups.items())


def sql_event_outs(q: str = "") -> str:
    """CASE expression: the outs recorded by ``events`` alone (0 on NULL).

    One ``IN`` list per out count (1, 2, 3), so the common one-out case
    resolves on the first branch, not after one compare per event, on every
    row of a full-table scan.
    """
    whens = " ".join(
        f"WHEN {q}events IN {sql_list(members)} THEN {n}" for n, members in _outs_groups()
    )
    return f"CASE {whens} ELSE 0 END"


def sql_hidden_runner_out(q: str = "") -> str:
    """The hidden runner out (0 or 1).

    ``type = 'X'`` MEANS "in play, out(s) recorded": every X row carries at
    least one out. When ``events`` counts none (a runner thrown out advancing
    on a hit, an error, or a fielders choice with every runner safe), the out
    lives only in the type code. The test is "not an out event", so a new
    reach event typed X still scores its out.
    """
    return (
        f"CASE WHEN {q}type = 'X' AND ({q}events IS NULL "
        f"OR {q}events NOT IN {sql_list(sorted(FIELDING_OUT_EVENTS))}) "
        f"THEN 1 ELSE 0 END"
    )


def sql_play_outs(q: str = "") -> str:
    """Outs recorded on the play: the events term + the hidden runner out.

    This is the per-play answer to question 1. It has no caught-stealing
    term, so use it on in-play rows only: no in-play pitch carries a steal
    attempt.
    """
    return f"(({sql_event_outs(q)}) + ({sql_hidden_runner_out(q)}))"


def sql_cs_out(q: str = "") -> str:
    """A mid-plate-appearance caught stealing on this pitch (0 or 1).

    The row guard keeps an out from counting twice: a row whose ``events``
    already counts a steal or pickoff out (``_STEAL_OUT_EVENTS``) adds
    nothing here, whatever the steal columns say. On the stored data the
    guard never fires, but the formula does not depend on the feed keeping
    that promise.
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
    """The total outs recorded on this row: the play outs + the caught-stealing outs.

    This is the innings-pitched building block: SUM it per pitcher. About
    0.5% of real outs are not here: the pickoff outs and the runner outs the
    feed anchors off a pitch live in no ``raw.pitches`` row. The uncaught
    third strike over-counts by about 0.06%.
    """
    return f"({sql_play_outs(q)} + ({sql_cs_out(q)}))"


# Where the outs live that a pitch row does not show:
# - A pickoff out lives in `raw.play_events`. The two profile `outs_recorded`
#   readers add it from there (the computor's `_play_events_outs_cte`).
# - A caught stealing in mid plate appearance lives in the `sb_*` columns;
#   the caught-stealing term above counts it.
# - A batter strikeout the feed displaces lives in `events`; the terms above
#   count it.
# - A runner out anchored to an `other_out` or `wild_pitch` action that does
#   not end the plate appearance reaches no table. These are about 0.04% of
#   all outs, accepted.


# ---------------------------------------------------------------------------
# Transition destinations
# ---------------------------------------------------------------------------
# The fielding draw applies a drawn pool row's whole change of base state, so
# the pool must encode where each runner and the batter ended. `raw.pitches`
# carries the truth: the loader starts the post-play bases from the pre-play
# bases, moves every runner to his ``movement.end``, and clears a runner who
# scored or was retired. A stranded runner therefore keeps his base, and an
# inning-ending third out never mislabels the survivors.
#
# The destination code (one SMALLINT per runner or batter):
#   NULL  = no runner on that base before the pitch (runner columns only)
#   4     = scored
#   3/2/1 = the base he ended on after the play
#   0     = retired on the play

TRANSITION_BASES = ("1b", "2b", "3b")


def sql_runner_dest(base: str, q: str = "") -> str:
    """CASE expression: the destination of the pre-pitch runner on ``base``.

    The scored flag comes FIRST: it is the official scoring, so it already
    applies the rule on when a run counts (Rule 5.08). A runner on no base
    after the play who did not score was retired: the loader clears exactly
    those.
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
    when the batter id lives on a joined table). The scoring branch for a
    play that is not a home run counts runs: when the play's runs exceed the
    runners' scored flags, the extra run is the batter's.
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
# Steal-attempt labels
# ---------------------------------------------------------------------------
# A steal outcome lives in one of TWO places in raw.pitches, never both:
#
#   * the ``sb_attempt_*`` / ``sb_success_*`` columns: a steal in mid plate
#     appearance (the plate appearance goes on after the play);
#   * the ``events`` column: a steal that ENDS the plate appearance
#     (``caught_stealing_2b`` and the like; the columns stay FALSE there).
#
# A caught stealing often ends the plate appearance, so a large share of the
# caught stealings live only in ``events``. A successful steal almost never
# does. A reader of the columns alone therefore inflates every steal SUCCESS
# rate. Use these two helpers at EVERY site that labels a steal attempt or
# outcome; do not write a third definition.
#
# An accepted gap: a caught stealing inside a ``strikeout_double_play`` names
# no base, so these labels leave it out (up to about 100 a season).

STEAL_BASES = ("2b", "3b", "home")


def sql_steal_attempt(base: str, q: str = "") -> str:
    """Boolean: a steal of ``base`` was attempted on this pitch row.

    NULL-safe: a NULL ``events`` (any pitch in mid plate appearance) must read
    FALSE, not NULL. ``FALSE OR NULL`` is NULL, and it breaks a NOT NULL label
    column.
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

    Pass the row's ``type`` code too, to count the hidden runner out
    (``type='X'`` means at least one out). Omit it where ``type`` is not
    selected.
    """
    outs = PLAY_OUTS_BY_EVENT.get(event, 0) if isinstance(event, str) else 0
    if type_code == "X" and outs == 0:
        outs = 1
    return outs


def batter_retired(event: object) -> bool:
    """True when the batter did not reach base (question 2). NULL-safe."""
    return isinstance(event, str) and event in BATTER_RETIRED_EVENTS
