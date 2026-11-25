"""The pitch pool's count chain, and the label check against real plate appearances.

THE COUNT CHAIN
===============
The simulator's count machine is a chain over the twelve counts (0-0 to 3-2).
At each count the loop draws one pitch-pool row, and the row's class
(``outcome_type``) moves the count or ends the plate appearance. A ball at
three balls is a walk. A called or swinging strike at two strikes is a
strikeout. A foul at two strikes leaves the count where it is. An in-play or
hit-by-pitch row ends the plate appearance.

:func:`solve_chain` takes the class shares at each count and returns the
per-plate-appearance rates a faithful sampler of those shares produces: the
walk, strikeout, in-play and hit-by-pitch shares, and the expected pitches.
:func:`pool_count_matrix` reads those shares from ``sim.pitch_pool``, and
:func:`chain_rates` joins the two.

WHY THE LABEL CHECK EXISTS
==========================
The pool-totals grade certifies the simulator's frequencies against this
chain. The chain and the simulator read the SAME labels, so a labelling defect
in the pool build reads green: a faithful sampler of a mislabelled pool matches
the mislabelled centre. A hit-by-pitch pitch coded as a ball, or a two-strike
foul tip coded as a foul, hides this way.

:func:`real_pa_rates` counts the same four rates from real plate appearances
in ``raw.pitches`` (Postgres). :func:`label_check` compares the chain with
them. The two sources share no label, so a coding defect opens a gap. The
tolerance is 0.5% (:data:`LABEL_CHECK_TOLERANCE`).

The check cannot see two kinds of mislabel. The count machine treats a called
and a swinging strike alike, and every in-play sub-type alike, so a swap
inside either group moves no chain rate. The unit truth tables
(``tests/unit/test_foul_tip_strike_three.py``) guard those.

THE POPULATION RULE
===================
The two sides must count the same plate appearances. :func:`real_pa_rates`
counts the ``(game_pk, at_bat_number)`` groups that end in a batting event.
The pool also holds the pitches of groups that never reach one: an inning
ended by a pickoff or a caught stealing mid-count, an intentional walk called
after some pitches, a game ended mid-plate-appearance. The chain cannot end
such a group early, so it plays each one on to an end. About half of their
rows are balls, so they push the chain's walks up.

So the label check reads the chain with ``pa_only=True``: only the pool rows
whose group holds a batting event, judged from the pool's own ``events``
column by the one expression :func:`real_pa_rates` uses (:func:`_sql_pa_flag`).
The group test reads ``events``, never the class, so it cannot hide a
mislabel. The band centres keep the default ``pa_only=False``, because the
simulator draws every pool row.

The rule holds only while both stores hold the same pitches. So every caller
of the label check also compares the counts: :func:`pa_group_count` (the
groups the ``pa_only`` chain reads, from the same SQL) must equal
``real_pa_rates(...)['n_pa']``. A difference is a FAIL: the two sides no
longer read the same plate appearances, for example because ``raw.pitches``
gained games the pool has not been rebuilt for.

Two known residuals are not label errors:

  1. The groups cut short (above). ``pa_only`` removes this residual. An
     all-rows chain over 2017-2026 fails on walks, because 2017-2019 hold
     many more such groups.
  2. The pitch clock's automatic balls. A violation adds a ball with no pitch
     row, so the chain cannot see that ball, and its walks read low by a few
     tenths of a percent. The gap fades as violations fade, and it stays in
     the ``pa_only`` read.

WHO USES IT
===========
  * ``scripts/pool_window_census.py`` — the census that sets the band centres
    in ``tests/acceptance/bands.py`` prints the label check per window.
  * ``scripts/rebuild_pitch_pool.py`` — the pool rebuild stops before
    the export when the check fails.
  * ``tests/acceptance/test_pool_label_check.py`` — the standing test.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from ingest.statcast_events import sql_list
from simulation.game_state import PITCH_OUTCOMES

#: The largest relative gap the label check accepts between the pool's chain
#: (read with ``pa_only=True``) and real plate appearances. A correct pool reads
#: within a few tenths of a percent; a coding defect opens a gap of whole
#: percents. A FAIL is a coding defect to find, never a reason to widen this.
LABEL_CHECK_TOLERANCE = 0.005

#: The channels the label check compares, in print order.
LABEL_CHANNELS: tuple[str, ...] = ("k", "walk", "hbp", "pitches")

#: The ``events`` values that are not a plate appearance's end: a runner event
#: (steal, pickoff, wild pitch, balk) or a feed note. An intentional walk is a
#: plate appearance, but the pool holds no pitch row for it, so the real count
#: leaves it out too.
_NON_PA_EVENTS: tuple[str, ...] = (
    "wild_pitch",
    "passed_ball",
    "other_out",
    "game_advisory",
    "balk",
    "other_advance",
    "runner_double_play",
    "intent_walk",
)
_NON_PA_EVENT_PREFIXES: tuple[str, ...] = ("caught_stealing", "pickoff", "stolen_base")

#: The events that count as a strikeout in the real plate appearances.
_STRIKEOUT_EVENTS: tuple[str, ...] = ("strikeout", "strikeout_double_play")


def _sql_pa_flag(col: str = "events") -> str:
    """The SQL group test of a plate appearance: 1 when a row's ``col`` is a batting event.

    A batting event is a non-NULL ``col`` that is not a runner event or a feed
    note. :func:`real_pa_rates` (on ``raw.pitches``) and the ``pa_only``
    chain (on ``sim.pitch_pool``) both judge a ``(game_pk, at_bat_number)``
    group with this one expression, so the two sides cannot disagree on what a
    plate appearance is.
    """
    prefixes = " OR ".join(f"{col} LIKE '{p}%'" for p in _NON_PA_EVENT_PREFIXES)
    batting = (
        f"({col} IS NOT NULL AND {col} NOT IN {sql_list(_NON_PA_EVENTS)} AND NOT ({prefixes}))"
    )
    return f"MAX(CASE WHEN {batting} THEN 1 ELSE 0 END)"


def _sql_pa_keys(pred: str) -> str:
    """The SQL of the plate-appearance keys of ``sim.pitch_pool WHERE ({pred})``.

    One ``(game_pk, at_bat_number)`` row per group whose rows INSIDE ``pred``
    hold a batting event (:func:`_sql_pa_flag`). The ``pa_only`` read of
    :func:`pool_count_matrix` and :func:`pa_group_count` both use this one
    string, so the rows the chain reads and the groups the count reports
    cannot drift apart.
    """
    return (
        f"SELECT game_pk, at_bat_number FROM sim.pitch_pool WHERE ({pred}) "
        f"GROUP BY game_pk, at_bat_number HAVING {_sql_pa_flag('events')} = 1"
    )


def pa_group_count(con: Any, pred: str) -> int:
    """Count the plate appearances the ``pa_only`` chain reads under ``pred``.

    A plate appearance is a ``(game_pk, at_bat_number)`` group whose
    rows inside ``pred`` hold a batting event, judged from the pool's own
    ``events`` column (:func:`_sql_pa_keys`, the SQL the ``pa_only`` read
    uses). The label check compares this with ``real_pa_rates(...)['n_pa']``:
    when the two differ, the two sides no longer read the same plate
    appearances (for example, ``raw.pitches`` gained games the pool has not
    been rebuilt for), and the channel gaps compare different populations.
    """
    row = con.execute(f"SELECT COUNT(*) FROM ({_sql_pa_keys(pred)}) AS pa").fetchone()
    return int(row[0] or 0)


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


def solve_chain(rates: list[list[float]]) -> dict[str, float]:
    """Solve the count chain from 0-0: how the plate appearance ends, and its pitches.

    ``rates[b*3+s]`` is the class row of count (b, s), in :data:`PITCH_OUTCOMES`
    order. The row need not be normalised: counts or weights work. A foul at
    two strikes loops back to the same count, so the closed form divides the
    count's other terms by (1 - p_foul). Returns the share of plate
    appearances that end in each way (``walk``, ``k``, ``in_play``, ``hbp``)
    and the expected ``pitches``.

    ``tests/unit/pools/test_pool_chain.py`` holds an earlier copy of this
    solver as its oracle. A two-strike row of fouls only raises
    ``ValueError``, because that count never ends. An all-zero row reads as
    all-zero shares and loses that count's mass; :func:`pool_count_matrix`
    refuses such a row before it gets here.
    """
    p: dict[tuple[int, int], dict[str, float]] = {}

    def shares(b: int, s: int) -> dict[str, float]:
        row = rates[b * 3 + s]
        tot = sum(row[:6]) or 1.0
        return {o: row[i] / tot for i, o in enumerate(PITCH_OUTCOMES)}

    def state(b: int, s: int) -> dict[str, float]:
        if (b, s) in p:
            return p[(b, s)]
        sh = shares(b, s)
        acc = {"walk": 0.0, "k": 0.0, "in_play": 0.0, "hbp": 0.0, "pitches": 1.0}

        def add(dest: dict[str, float], w: float) -> None:
            for key in ("walk", "k", "in_play", "hbp", "pitches"):
                acc[key] += w * dest[key]

        if b == 3:
            acc["walk"] += sh["ball"]
        else:
            add(state(b + 1, s), sh["ball"])
        strike = sh["called_strike"] + sh["swinging_strike"]
        if s == 2:
            acc["k"] += strike
        else:
            add(state(b, s + 1), strike)
        if s < 2:
            add(state(b, s + 1), sh["foul"])
            denom = 1.0
        else:
            denom = 1.0 - sh["foul"]  # the two-strike foul self-loop
            if denom <= 0.0:
                # A two-strike count of fouls only never ends, and the closed
                # form divides by zero.
                raise ValueError(
                    f"every pitch at count {b}-{s} is a foul: the count chain "
                    "never leaves that count"
                )
        acc["in_play"] += sh["in_play"]
        acc["hbp"] += sh["hit_by_pitch"]
        out = {k: v / denom for k, v in acc.items()}
        p[(b, s)] = out
        return out

    return state(0, 0)


def pool_count_matrix(
    con: Any, pred: str, weighted: bool = False, pa_only: bool = False
) -> list[list[float]]:
    """Read the pitch pool's class volume at each count: 12 rows x 6 classes.

    Row ``balls*3 + strikes`` holds the rows (or, with ``weighted``, the summed
    ``recency_weight``) of ``sim.pitch_pool WHERE {pred}`` per class, in
    :data:`PITCH_OUTCOMES` order. ``pred`` is a SQL predicate on the pool's columns
    (``season``, ``game_date``, ...).

    ``pa_only`` keeps only the rows of real plate appearances: the
    rows whose ``(game_pk, at_bat_number)`` group holds a batting event in the
    pool's own ``events`` column. The group test is :func:`_sql_pa_flag`, the
    one :func:`real_pa_rates` applies to ``raw.pitches``, and it reads
    ``events``, never the class. It needs no Postgres. The label check uses
    it; the band centres keep the default, because the simulator draws every
    pool row (the module docstring, THE POPULATION RULE).
    :func:`pa_group_count` counts the groups this read keeps, from the same SQL.

    The split rule. A predicate on the pitch (``pitch_number <= 2``,
    ``count_strikes < 2``) can put part of a group inside ``pred`` and part
    outside. ``pa_only`` then reads the group on its rows INSIDE ``pred`` only:
    the group test sees only those rows, and only those rows are kept. A group
    whose batting event falls outside ``pred`` is dropped whole. That is what
    :func:`real_pa_rates` does, because it filters the rows by ``pred`` before
    it groups them. So for any predicate on columns both tables carry
    (``season``, ``game_date``, ``pitch_number``), the ``pa_only`` groups are
    the ``real_pa_rates`` groups. The label check uses season predicates,
    where no group splits.

    Raises ``ValueError`` when a row carries a class outside :data:`PITCH_OUTCOMES`
    (or a NULL class) or a count outside 0-3 balls / 0-2 strikes. A new class
    must not vanish from the chain silently. It
    also raises when a count holds no volume: :func:`solve_chain` would read
    that count as all-zero shares and lose its mass without an error. A
    season holds thousands of rows at every count, so only a broken pool or a
    narrow predicate trips this.
    """
    volume = "SUM(recency_weight)" if weighted else "COUNT(*)"
    if pa_only:
        # One CTE of the plate-appearance keys (_sql_pa_keys, the SQL
        # pa_group_count counts), joined back. Each key appears once, so the
        # join adds no row. The CTE holds only the two key columns, so a bare
        # column in ``pred`` stays unambiguous. ``pred`` applies twice: in the
        # group test and to the rows kept (the docstring's split rule).
        sql = (
            f"WITH pa AS ({_sql_pa_keys(pred)}) "
            f"SELECT count_balls, count_strikes, outcome_type, {volume} "
            f"FROM sim.pitch_pool JOIN pa USING (game_pk, at_bat_number) "
            f"WHERE ({pred}) GROUP BY 1, 2, 3"
        )
    else:
        sql = (
            f"SELECT count_balls, count_strikes, outcome_type, {volume} "
            f"FROM sim.pitch_pool WHERE ({pred}) GROUP BY 1, 2, 3"
        )
    rows = con.execute(sql).fetchall()
    mat = [[0.0] * len(PITCH_OUTCOMES) for _ in range(12)]
    for balls, strikes, outcome, n in rows:
        if outcome not in PITCH_OUTCOMES:
            raise ValueError(
                f"sim.pitch_pool holds class {outcome!r} at count {balls}-{strikes} "
                f"({n} rows or weight) outside the chain's classes {PITCH_OUTCOMES}; "
                "the count chain cannot place it"
            )
        if balls is None or strikes is None or not (0 <= balls <= 3 and 0 <= strikes <= 2):
            raise ValueError(
                f"sim.pitch_pool holds count {balls}-{strikes} ({n} rows or weight); "
                "the count chain has no such state"
            )
        mat[int(balls) * 3 + int(strikes)][PITCH_OUTCOMES.index(outcome)] += float(n or 0.0)
    empty = [f"{i // 3}-{i % 3}" for i, row in enumerate(mat) if not sum(row) > 0.0]
    if empty:
        scope = " (pa_only)" if pa_only else ""
        raise ValueError(
            f"sim.pitch_pool WHERE {pred}{scope} holds no volume at count(s) "
            f"{', '.join(empty)}; the count chain would lose those counts' mass"
        )
    return mat


def chain_rates(
    con: Any, pred: str, weighted: bool = False, pa_only: bool = False
) -> dict[str, float]:
    """Solve the count chain of ``sim.pitch_pool WHERE {pred}`` (:func:`solve_chain`).

    ``pa_only`` reads only the rows of real plate appearances (see
    :func:`pool_count_matrix`); the label check passes ``pa_only=True``.
    """
    return solve_chain(pool_count_matrix(con, pred, weighted=weighted, pa_only=pa_only))


# ---------------------------------------------------------------------------
# Real plate appearances
# ---------------------------------------------------------------------------


def real_pa_rates(con: Any, pred: str) -> dict[str, float]:
    """Per-plate-appearance rates of real play from ``pg.raw.pitches``.

    Needs :func:`db.dsn.attach_pg` first. ``pred`` is a SQL predicate on
    ``raw.pitches`` columns (``season``, ``game_date``); the rows carry
    ``data_quality_flag = FALSE``, the pool build's own filter.

    A plate appearance is a ``(game_pk, at_bat_number)`` group with a row whose
    ``events`` is a batting event: not NULL, not a runner event (a steal, a
    pickoff, a wild pitch, a passed ball, a balk, ``other_advance``,
    ``other_out``, ``runner_double_play``), not a feed note
    (``game_advisory``), not an intentional walk. A steal or a pickoff that
    ends an inning is therefore not a plate appearance. The group test is
    :func:`_sql_pa_flag`, the same expression the ``pa_only`` chain applies to
    the pool, so compare this with ``chain_rates(..., pa_only=True)``.

    The definition is an exclusion list: any other non-NULL event counts as a
    plate appearance. Every non-batting value the feed held across 2017-2026
    is on the list. A new runner-event value counts as a plate appearance with
    no strikeout, walk or hit by pitch on both sides, so add it to the list.

    Returns ``k`` (``strikeout`` / ``strikeout_double_play``), ``walk``
    (``walk``), ``hbp`` (``hit_by_pitch``) per plate appearance, ``pitches``
    (the mean pitch rows per plate appearance) and ``n_pa``. The module
    docstring (THE POPULATION RULE) records the two residuals that are not
    label errors: the groups cut short, and the pitch clock's automatic balls.
    """
    is_k = f"events IN {sql_list(_STRIKEOUT_EVENTS)}"
    row = con.execute(
        f"""
        WITH pa AS (
            SELECT game_pk, at_bat_number,
                   COUNT(*) AS n_rows,
                   {_sql_pa_flag("events")} AS is_pa,
                   MAX(CASE WHEN {is_k} THEN 1 ELSE 0 END) AS is_k,
                   MAX(CASE WHEN events = 'walk' THEN 1 ELSE 0 END) AS is_bb,
                   MAX(CASE WHEN events = 'hit_by_pitch' THEN 1 ELSE 0 END) AS is_hbp
            FROM pg.raw.pitches
            WHERE data_quality_flag = FALSE AND ({pred})
            GROUP BY game_pk, at_bat_number
        )
        SELECT COUNT(*), SUM(is_k), SUM(is_bb), SUM(is_hbp), SUM(n_rows)
        FROM pa WHERE is_pa = 1
        """
    ).fetchone()
    n_pa = int(row[0] or 0)
    if n_pa == 0:
        raise ValueError(f"raw.pitches holds no plate appearance for {pred!r}")
    return {
        "k": float(row[1]) / n_pa,
        "walk": float(row[2]) / n_pa,
        "hbp": float(row[3]) / n_pa,
        "pitches": float(row[4]) / n_pa,
        "n_pa": float(n_pa),
    }


# ---------------------------------------------------------------------------
# The label check
# ---------------------------------------------------------------------------


def label_check(
    chain: dict[str, float],
    real: dict[str, float],
    tolerance: float = LABEL_CHECK_TOLERANCE,
) -> list[tuple[str, float, float, float, bool]]:
    """Compare the pool's chain with real plate appearances, channel by channel.

    Returns one row ``(channel, chain_value, real_value, relative_gap, ok)`` per
    channel in :data:`LABEL_CHANNELS`. ``relative_gap`` is
    ``(chain - real) / real``; ``ok`` is ``abs(relative_gap) <= tolerance``. A
    zero real rate gives a zero gap when the chain is zero too, else an
    infinite gap.
    """
    out: list[tuple[str, float, float, float, bool]] = []
    for channel in LABEL_CHANNELS:
        c = float(chain[channel])
        r = float(real[channel])
        if r == 0.0:
            gap = 0.0 if c == 0.0 else float("inf")
        else:
            gap = (c - r) / r
        out.append((channel, c, r, gap, abs(gap) <= tolerance))
    return out


class LabelCheckRun(NamedTuple):
    """One run of the label check (:func:`run_label_check`)."""

    rows: list[tuple[str, float, float, float, bool]]
    n_pool: int
    n_real: int
    real: dict[str, float]
    ok: bool


def run_label_check(con: Any, pred: str) -> LabelCheckRun:
    """The label check over ``pred``: the ``pa_only`` chain against real plate appearances.

    Needs :func:`db.dsn.attach_pg` first. ``rows`` are :func:`label_check`'s,
    ``n_pool`` is :func:`pa_group_count`, ``n_real`` and ``real`` come from
    :func:`real_pa_rates`. ``ok`` holds when the two counts are equal and every
    channel is inside the tolerance. Each caller prints the run its own way.
    """
    chain = chain_rates(con, pred, pa_only=True)
    real = real_pa_rates(con, pred)
    n_real = int(real["n_pa"])
    n_pool = pa_group_count(con, pred)
    rows = label_check(chain, real)
    ok = n_pool == n_real and all(row[4] for row in rows)
    return LabelCheckRun(rows=rows, n_pool=n_pool, n_real=n_real, real=real, ok=ok)


#: The printed name of each label-check channel.
_CHANNEL_NAMES = {"k": "K/PA", "walk": "BB/PA", "hbp": "HBP/PA", "pitches": "pitches/PA"}


def format_label_check(rows: list[tuple[str, float, float, float, bool]]) -> list[str]:
    """Render :func:`label_check` rows as aligned text lines, one per channel."""
    lines = []
    for channel, c, r, gap, ok in rows:
        lines.append(
            f"{_CHANNEL_NAMES.get(channel, channel):>11}: chain {c:.4f}  real {r:.4f}  "
            f"gap {gap * 100.0:+.2f}%  {'PASS' if ok else 'FAIL'}"
        )
    return lines


def format_pa_count(n_pool: int, n_real: int) -> str:
    """Render the plate-appearance count check as one line, aligned with the channel lines.

    ``n_pool`` is :func:`pa_group_count`, ``n_real`` is
    ``real_pa_rates(...)['n_pa']``. Equal counts PASS. Unequal counts FAIL,
    with the plain reason: the two sides no longer read the same plate
    appearances.
    """
    if n_pool == n_real:
        return f"{'PAs':>11}: pool {n_pool:,}  real {n_real:,}  equal  PASS"
    return (
        f"{'PAs':>11}: pool {n_pool:,}  real {n_real:,}  ({n_pool - n_real:+,})  FAIL: "
        "the two sides no longer read the same plate appearances (for example, "
        "raw.pitches gained games the pool has not been rebuilt for)"
    )


def format_gaps(rows: list[tuple[str, float, float, float, bool]]) -> str:
    """Render :func:`label_check` rows' gaps on one line, for a for-the-record read.

    The census and the standing test print the all-rows chain's gaps
    this way beside the ``pa_only`` verdict.
    """
    return "  ".join(
        f"{_CHANNEL_NAMES.get(channel, channel)} {gap * 100.0:+.2f}%"
        for channel, _c, _r, gap, _ok in rows
    )
