"""
pipeline/batch/pool_chain.py
============================
SIM-553 — the count chain of the pitch pool, and the label check against real
plate appearances.

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
The pool-totals grade (the ruling of 2026-08-20) certifies the simulator's
frequencies against this chain. The chain and the simulator read the SAME
labels, so a labelling defect in the pool build reads green: a faithful
sampler of a mislabelled pool matches the mislabelled centre. Two defects in
the pool build's class expression hid that way:

  * SIM-509 (2026-08-18): hit-by-pitch pitches coded as balls, so an HBP could
    become ball four.
  * SIM-553 (2026-09-23): a two-strike foul tip or foul bunt coded as a foul,
    so the count machine pitched on after a real strikeout. The count chain
    lost 4.4% of its strikeouts (0.2163 per plate appearance against 0.2262
    on the corrected coding).

:func:`real_pa_rates` counts the same four rates from real plate appearances
in ``raw.pitches`` (Postgres). :func:`label_check` compares the chain with
them. The two sources share no label, so a coding defect opens a gap. The
tolerance is 0.5% (:data:`LABEL_CHECK_TOLERANCE`, owner decision 5 of
docs/audit/2026-09-23-foul-tip-strike-three-pool-coding-plan.md).

The check cannot see two kinds of mislabel. The count machine treats a called
and a swinging strike alike, and every in-play sub-type alike, so a swap
inside either group moves no chain rate. The unit truth tables
(``tests/unit/test_sim553_foul_tip_strike_three.py``) guard those.

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
``real_pa_rates(...)['n_pa']``. On the window 2023-2026 both read 708,439
(2026-09-25). A difference is a FAIL: the two sides no longer read the same
plate appearances, for example because ``raw.pitches`` gained games the pool
has not been rebuilt for.

Measured 2026-09-25 on the window 2023-2026 (strikeouts / walks / hit by
pitch / pitches per plate appearance, the chain's relative gap to real play):

  * the corrected coding, ``pa_only``:  +0.09% / -0.25% / +0.08% / +0.07%;
  * the corrected coding, all rows:     +0.30% / +0.36% / -0.13% / +0.27%;
  * the SIM-553 coding, ``pa_only``:    -4.29% / +2.31% / +1.12% / +0.90%
    (FAIL; all rows read -4.08% / +2.94% / +0.91% / +1.11%).

Two known residuals are not label errors:

  1. The groups cut short (above). In the window they are 4,249 groups and
     11,923 rows. On all rows they add about +0.6 points to the walk gap, +0.2
     to strikeouts and pitches, and -0.2 to hit by pitch. 2017-2019 hold about
     10,000 such groups per season, so an all-rows chain over 2017-2026 FAILS
     (walks +1.69%) where the ``pa_only`` chain passes. ``pa_only`` removes
     this residual.
  2. The pitch clock's automatic balls. A violation adds a ball with no pitch
     row, so the chain cannot see that ball, and its walks read low. On
     ``pa_only`` the walk gap is -0.41% in 2023, -0.22% in 2024, -0.20% in
     2025 and -0.16% in 2026. It fades as violations fade, and it stays in the
     ``pa_only`` read.

WHO USES IT
===========
  * ``scripts/pool_window_census.py`` — the census that sets the band centres
    in ``tests/acceptance/bands.py`` prints the label check per window.
  * ``scripts/sim553_rebuild_pitch_pool.py`` — the pool rebuild stops before
    the export when the check fails.
  * ``tests/acceptance/test_sim553_pool_label_check.py`` — the standing test.

The chain solver is the one ``scripts/sim429_chain_analysis.py`` carried
(deleted by commit 6ab341c on 2026-09-06); its logic is unchanged.
"""

from __future__ import annotations

import os
from typing import Any

#: The six pitch-pool classes, in the column order of a count matrix row.
OUTCOMES: tuple[str, ...] = (
    "ball",
    "called_strike",
    "swinging_strike",
    "foul",
    "in_play",
    "hit_by_pitch",
)

#: The largest relative gap the label check accepts between the pool's chain
#: (read with ``pa_only=True``) and real plate appearances; owner decision 5 of
#: the SIM-553 plan. On the window 2023-2026 the corrected pool reads +0.09% /
#: -0.25% / +0.08% / +0.07% (strikeouts / walks / hit by pitch / pitches); the
#: SIM-553 defect reads -4.29% / +2.31% / +1.12% / +0.90%. A FAIL is a coding
#: defect to find, never a reason to widen this.
LABEL_CHECK_TOLERANCE = 0.005

#: The channels the label check compares, in print order.
LABEL_CHANNELS: tuple[str, ...] = ("k", "walk", "hbp", "pitches")

#: The DSN the stack's containers use when ``BASEBALL_DB_DSN`` is not set.
DEFAULT_PG_DSN = "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"

#: The ``events`` values that are not a plate appearance's end: a runner event
#: (steal, pickoff, wild pitch, balk) or a feed note. An intentional walk is a
#: plate appearance, but the pool holds no pitch for it (0 IBB pitch rows since
#: 2017), so the real count leaves it out too.
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


def _sql_list(values: tuple[str, ...]) -> str:
    """Render ``values`` as a SQL ``IN`` list of string literals."""
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _sql_is_batting_event(col: str = "events") -> str:
    """The SQL test that ``col`` is a non-NULL batting event (a plate appearance's end)."""
    prefixes = " OR ".join(f"{col} LIKE '{p}%'" for p in _NON_PA_EVENT_PREFIXES)
    return f"({col} IS NOT NULL AND {col} NOT IN {_sql_list(_NON_PA_EVENTS)} AND NOT ({prefixes}))"


def _sql_pa_flag(col: str = "events") -> str:
    """The group aggregate that marks a plate appearance: 1 when a row's ``col`` is a batting event.

    SIM-553: :func:`real_pa_rates` (on ``raw.pitches``) and the ``pa_only``
    chain (on ``sim.pitch_pool``) both judge a ``(game_pk, at_bat_number)``
    group with this one expression, so the two sides cannot disagree on what a
    plate appearance is.
    """
    return f"MAX(CASE WHEN {_sql_is_batting_event(col)} THEN 1 ELSE 0 END)"


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
    """The number of plate appearances the ``pa_only`` chain reads from ``sim.pitch_pool WHERE ({pred})``.

    SIM-553: a plate appearance is a ``(game_pk, at_bat_number)`` group whose
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
    """Absorption probabilities + expected pitches from (0,0).

    ``rates[b*3+s]`` is the outcome-share row for count (b, s). A foul at two
    strikes self-loops; the closed form divides the state's other terms by
    (1 - p_foul).

    The row is in :data:`OUTCOMES` order and need not be normalised (counts or
    weights work). Returns the keys ``walk``, ``k``, ``in_play``, ``hbp`` and
    ``pitches``. The logic is the deleted ``scripts/sim429_chain_analysis.py``
    solver, unchanged; ``tests/unit/test_sim553_pool_chain.py`` holds the
    original as its oracle.

    Two edges the original did not guard. A two-strike row that is all fouls
    raises ``ValueError`` here (the original divided by zero). An all-zero row
    still reads as all-zero shares, as in the original, and loses that count's
    mass; :func:`pool_count_matrix` refuses such a row before it gets here.
    """
    p: dict[tuple[int, int], dict[str, float]] = {}

    def shares(b: int, s: int) -> dict[str, float]:
        row = rates[b * 3 + s]
        tot = sum(row[:6]) or 1.0
        return {o: row[i] / tot for i, o in enumerate(OUTCOMES)}

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
                # SIM-553 guard: a two-strike count where every pitch is a foul
                # never ends, and the closed form would divide by zero.
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
    :data:`OUTCOMES` order. ``pred`` is a SQL predicate on the pool's columns
    (``season``, ``game_date``, ...).

    ``pa_only`` (SIM-553) keeps only the rows of real plate appearances: the
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

    Raises ``ValueError`` when a row carries a class outside :data:`OUTCOMES`
    (or a NULL class) or a count outside 0-3 balls / 0-2 strikes. A new class
    must not vanish from the chain silently; the old census skipped it. It
    also raises when a count holds no volume: :func:`solve_chain` would read
    that count as all-zero shares and lose its mass without an error. The
    window's thinnest count holds 28,056 rows, so only a broken pool or a
    narrow predicate trips this.
    """
    volume = "SUM(recency_weight)" if weighted else "COUNT(*)"
    if pa_only:
        # SIM-553: one CTE of the plate-appearance keys (_sql_pa_keys, the SQL
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
    mat = [[0.0] * len(OUTCOMES) for _ in range(12)]
    for balls, strikes, outcome, n in rows:
        if outcome not in OUTCOMES:
            raise ValueError(
                f"sim.pitch_pool holds class {outcome!r} at count {balls}-{strikes} "
                f"({n} rows or weight) outside the chain's classes {OUTCOMES}; "
                "the count chain cannot place it"
            )
        if balls is None or strikes is None or not (0 <= balls <= 3 and 0 <= strikes <= 2):
            raise ValueError(
                f"sim.pitch_pool holds count {balls}-{strikes} ({n} rows or weight); "
                "the count chain has no such state"
            )
        mat[int(balls) * 3 + int(strikes)][OUTCOMES.index(outcome)] += float(n or 0.0)
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
    """The count chain of ``sim.pitch_pool WHERE {pred}``: :func:`solve_chain` of the pool's matrix.

    ``pa_only`` reads only the rows of real plate appearances (see
    :func:`pool_count_matrix`); the label check passes ``pa_only=True``.
    """
    return solve_chain(pool_count_matrix(con, pred, weighted=weighted, pa_only=pa_only))


# ---------------------------------------------------------------------------
# Real plate appearances
# ---------------------------------------------------------------------------


def _redact(text: str, dsn: str) -> str:
    """Replace the DSN (and its password) in ``text``; DuckDB quotes the DSN in an ATTACH error."""
    body = str(text)
    if dsn:
        body = body.replace(dsn, "<dsn>")
        # postgresql://user:password@host/...: drop the password on its own too.
        head = dsn.split("@", 1)[0]
        if ":" in head.split("//", 1)[-1]:
            password = head.split("//", 1)[-1].split(":", 1)[1]
            if password:
                body = body.replace(password, "<password>")
    return body


def attach_pg(con: Any, dsn: str | None = None) -> None:
    """Attach Postgres read-only as the catalog ``pg``; a no-op when ``pg`` is attached.

    ``dsn`` defaults to ``BASEBALL_DB_DSN``, then :data:`DEFAULT_PG_DSN`. The
    attach works on a read-only DuckDB connection. An attach failure raises
    ``RuntimeError`` with the DSN's password redacted.
    """
    attached = con.execute(
        "SELECT COUNT(*) FROM duckdb_databases() WHERE database_name = 'pg'"
    ).fetchone()[0]
    if attached:
        return
    dsn = dsn or os.environ.get("BASEBALL_DB_DSN") or DEFAULT_PG_DSN
    try:
        con.execute("INSTALL postgres; LOAD postgres;")
        con.execute(f"ATTACH '{dsn}' AS pg (TYPE postgres, READ_ONLY);")
    except Exception as exc:  # noqa: BLE001 - any attach failure is reported the same way
        raise RuntimeError(f"Postgres attach failed: {_redact(str(exc), dsn)}") from None


def real_pa_rates(con: Any, pred: str) -> dict[str, float]:
    """Per-plate-appearance rates of real play from ``pg.raw.pitches``.

    Needs :func:`attach_pg` first. ``pred`` is a SQL predicate on
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
    plate appearance. Across 2017-2026 every non-batting value in the feed is
    on the list (checked 2026-09-25). A new runner-event value would count as
    a plate appearance with no strikeout, walk or hit by pitch on both sides.

    Returns ``k`` (``strikeout`` / ``strikeout_double_play``), ``walk``
    (``walk``), ``hbp`` (``hit_by_pitch``) per plate appearance, ``pitches``
    (the mean pitch rows per plate appearance) and ``n_pa``. The module
    docstring (THE POPULATION RULE) records the two residuals that are not
    label errors: the groups cut short, and the pitch clock's automatic balls.
    """
    is_k = f"events IN {_sql_list(_STRIKEOUT_EVENTS)}"
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
    """Render the plate-appearance count check on one line, aligned with :func:`format_label_check`.

    SIM-553: ``n_pool`` is :func:`pa_group_count`, ``n_real`` is
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

    SIM-553: the census and the standing test print the all-rows chain's gaps
    this way beside the ``pa_only`` verdict.
    """
    return "  ".join(
        f"{_CHANNEL_NAMES.get(channel, channel)} {gap * 100.0:+.2f}%"
        for channel, _c, _r, gap, _ok in rows
    )
