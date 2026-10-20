"""SIM-553 — the count chain of the pitch pool and the label check.

``pipeline/batch/pool_chain.py`` solves the count chain (the per-plate-
appearance rates a faithful sampler of the pool's per-count class shares
produces) and compares it with real plate appearances. These tests pin:

  * the solver on matrices with closed-form answers, and against the deleted
    original (``scripts/sim429_chain_analysis.py``, commit 6ab341c^) as an
    oracle;
  * the solver against the simulator's own count machine (``advance_count``),
    so a rule change in the loop cannot leave the chain behind;
  * the solver's guard on an all-foul two-strike count (the original divided
    by zero);
  * the pool reader, which must refuse a class the chain cannot place and a
    count with no rows;
  * the real plate-appearance definition, on an in-memory ``pg.raw.pitches``,
    with one lone-event group per exclusion;
  * ``pa_only``: the chain reads only the pool rows of real plate appearances,
    judged by the same expression ``real_pa_rates`` uses, so the two sides of
    the label check count the same groups;
  * the label check's arithmetic and its tolerance edge.
"""

from __future__ import annotations

import random

import duckdb
import numpy as np
import pytest

from pipeline.batch import pool_chain as pc
from simulation.game_state import PITCH_OUTCOMES
from simulation.sim_loop import (
    EVENT_HIT_BY_PITCH,
    EVENT_STRIKEOUT,
    EVENT_WALK,
    advance_count,
)

# ---------------------------------------------------------------------------
# The oracle: the deleted solver, verbatim
# (git show 6ab341c^:scripts/sim429_chain_analysis.py)
# ---------------------------------------------------------------------------

_ORIGINAL_OUTCOMES = ("ball", "called_strike", "swinging_strike", "foul", "in_play", "hit_by_pitch")


def _original_solve_chain(rates: list[list[float]]) -> dict[str, float]:
    """Absorption probabilities + expected pitches from (0,0).

    ``rates[b*3+s]`` is the outcome-share row for count (b, s). A foul at two
    strikes self-loops; the closed form divides the state's other terms by
    (1 - p_foul).
    """
    p: dict[tuple[int, int], dict[str, float]] = {}

    def shares(b: int, s: int) -> dict[str, float]:
        row = rates[b * 3 + s]
        tot = sum(row[:6]) or 1.0
        return {o: row[i] / tot for i, o in enumerate(_ORIGINAL_OUTCOMES)}

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
        acc["in_play"] += sh["in_play"]
        acc["hbp"] += sh["hit_by_pitch"]
        out = {k: v / denom for k, v in acc.items()}
        p[(b, s)] = out
        return out

    return state(0, 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uniform(outcome: str) -> list[list[float]]:
    """A matrix where every pitch at every count is ``outcome``."""
    row = [0.0] * 6
    row[pc.OUTCOMES.index(outcome)] = 1.0
    return [list(row) for _ in range(12)]


def _random_matrix(seed: int) -> list[list[float]]:
    """A dense matrix of raw counts (not shares), every class present at every count."""
    rng = random.Random(seed)
    return [[float(rng.randint(1, 5000)) for _ in range(6)] for _ in range(12)]


def _machine_chain(rates: list[list[float]]) -> dict[str, float]:
    """The chain solved as a linear system over the loop's own ``advance_count``.

    Twelve live counts; each class's transition comes from the count machine.
    ``N = (I - Q)^-1`` gives the absorption shares ``N R`` and the expected
    pitches ``N 1`` from 0-0.
    """
    counts = [(b, s) for b in range(4) for s in range(3)]
    index = {c: i for i, c in enumerate(counts)}
    ends = ("walk", "k", "in_play", "hbp")
    q = np.zeros((12, 12))
    r = np.zeros((12, 4))
    for (b, s), i in index.items():
        row = rates[b * 3 + s]
        tot = sum(row)
        for o, n in zip(pc.OUTCOMES, row, strict=True):
            share = n / tot
            adv = advance_count(b, s, o)
            if not adv.terminal:
                q[i, index[(adv.balls, adv.strikes)]] += share
            elif adv.is_contact:
                r[i, ends.index("in_play")] += share
            elif adv.event == EVENT_WALK:
                r[i, ends.index("walk")] += share
            elif adv.event == EVENT_STRIKEOUT:
                r[i, ends.index("k")] += share
            elif adv.event == EVENT_HIT_BY_PITCH:
                r[i, ends.index("hbp")] += share
            else:  # pragma: no cover - a new terminal event the chain does not know
                raise AssertionError(f"unknown terminal event {adv.event!r} for {o}")
    n_mat = np.linalg.inv(np.eye(12) - q)
    absorb = n_mat @ r
    pitches = n_mat @ np.ones(12)
    out = {e: float(absorb[0, j]) for j, e in enumerate(ends)}
    out["pitches"] = float(pitches[0])
    return out


# ---------------------------------------------------------------------------
# The solver
# ---------------------------------------------------------------------------


class TestTheSolverClosedForms:
    def test_every_pitch_a_ball_is_a_walk_in_four(self):
        r = pc.solve_chain(_uniform("ball"))
        assert r["walk"] == pytest.approx(1.0)
        assert r["k"] == r["in_play"] == r["hbp"] == 0.0
        assert r["pitches"] == pytest.approx(4.0)

    def test_every_pitch_a_called_strike_is_a_strikeout_in_three(self):
        r = pc.solve_chain(_uniform("called_strike"))
        assert r["k"] == pytest.approx(1.0)
        assert r["walk"] == r["in_play"] == r["hbp"] == 0.0
        assert r["pitches"] == pytest.approx(3.0)

    def test_a_swinging_strike_counts_as_a_strike(self):
        r = pc.solve_chain(_uniform("swinging_strike"))
        assert r["k"] == pytest.approx(1.0)
        assert r["pitches"] == pytest.approx(3.0)

    def test_every_pitch_in_play_ends_on_the_first_pitch(self):
        r = pc.solve_chain(_uniform("in_play"))
        assert r["in_play"] == pytest.approx(1.0)
        assert r["pitches"] == pytest.approx(1.0)

    def test_every_pitch_a_hit_by_pitch_ends_on_the_first_pitch(self):
        r = pc.solve_chain(_uniform("hit_by_pitch"))
        assert r["hbp"] == pytest.approx(1.0)
        assert r["pitches"] == pytest.approx(1.0)

    @pytest.mark.parametrize("f", [0.0, 0.25, 0.5, 0.9])
    def test_the_two_strike_foul_loop_costs_one_over_one_minus_f(self, f):
        """Two called strikes reach 0-2; there a foul share f self-loops.

        The two-strike state's expected pitches are 1 / (1 - f), so the plate
        appearance takes 2 + 1 / (1 - f) pitches and still ends in a strikeout.
        """
        mat = _uniform("called_strike")
        for b in range(4):
            mat[b * 3 + 2] = [0.0, 1.0 - f, 0.0, f, 0.0, 0.0]
        r = pc.solve_chain(mat)
        assert r["k"] == pytest.approx(1.0)
        assert r["pitches"] - 2.0 == pytest.approx(1.0 / (1.0 - f))

    def test_a_foul_below_two_strikes_adds_a_strike(self):
        """Every pitch a foul below two strikes, a called strike at two: three pitches."""
        mat = _uniform("foul")
        for b in range(4):
            mat[b * 3 + 2] = [0.0, 1.0, 0.0, 0.0, 0.0, 0.0]
        r = pc.solve_chain(mat)
        assert r["k"] == pytest.approx(1.0)
        assert r["pitches"] == pytest.approx(3.0)

    @pytest.mark.parametrize("seed", [1, 553, 2026])
    def test_the_absorption_shares_sum_to_one(self, seed):
        r = pc.solve_chain(_random_matrix(seed))
        assert r["walk"] + r["k"] + r["in_play"] + r["hbp"] == pytest.approx(1.0, abs=1e-12)
        assert 1.0 <= r["pitches"] <= 12.0

    def test_the_rows_need_not_be_normalised(self):
        mat = _random_matrix(7)
        scaled = [[v * 37.5 for v in row] for row in mat]
        a, b = pc.solve_chain(mat), pc.solve_chain(scaled)
        for key in ("walk", "k", "in_play", "hbp", "pitches"):
            assert a[key] == pytest.approx(b[key], rel=1e-12)


class TestTheSolverIsTheDeletedOriginal:
    def test_the_classes_are_the_original_classes(self):
        assert pc.OUTCOMES == _ORIGINAL_OUTCOMES

    @pytest.mark.parametrize("seed", [0, 11, 553])
    def test_equal_to_the_original_on_a_fixed_matrix(self, seed):
        mat = _random_matrix(seed)
        assert pc.solve_chain(mat) == _original_solve_chain(mat)

    def test_equal_to_the_original_on_a_pool_shaped_matrix(self):
        """A matrix with the pool's shape: a zero cell and a heavy two-strike foul share."""
        mat = _random_matrix(99)
        mat[0][5] = 0.0  # no hit-by-pitch at 0-0
        mat[11][3] = 40_000.0  # 3-2: fouls dominate
        assert pc.solve_chain(mat) == _original_solve_chain(mat)

    def test_an_all_foul_two_strike_count_raises_a_clear_error(self):
        """The original divided by zero there; the guard names the count instead."""
        mat = _uniform("called_strike")
        mat[1 * 3 + 2] = [0.0, 0.0, 0.0, 5.0, 0.0, 0.0]
        with pytest.raises(ZeroDivisionError):
            _original_solve_chain(mat)
        with pytest.raises(ValueError, match="1-2 is a foul"):
            pc.solve_chain(mat)


class TestTheSolverMatchesTheCountMachine:
    """The chain's rules are the loop's rules (``simulation.sim_loop.advance_count``)."""

    def test_the_chain_classes_are_the_simulator_vocabulary(self):
        assert set(pc.OUTCOMES) == set(PITCH_OUTCOMES)

    @pytest.mark.parametrize("seed", [3, 42, 553])
    def test_the_closed_form_equals_the_linear_system_over_advance_count(self, seed):
        mat = _random_matrix(seed)
        closed = pc.solve_chain(mat)
        machine = _machine_chain(mat)
        for key in ("walk", "k", "in_play", "hbp", "pitches"):
            assert closed[key] == pytest.approx(machine[key], rel=1e-10), key


# ---------------------------------------------------------------------------
# The pool reader
# ---------------------------------------------------------------------------


def _mini_pool(rows: list[tuple[int, int, str, float, int]]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA sim")
    con.execute(
        "CREATE TABLE sim.pitch_pool (count_balls INTEGER, count_strikes INTEGER, "
        "outcome_type VARCHAR, recency_weight DOUBLE, season INTEGER)"
    )
    con.executemany("INSERT INTO sim.pitch_pool VALUES (?, ?, ?, ?, ?)", rows)
    return con


def _grid_rows(season: int = 2024) -> list[tuple[int, int, str, float, int]]:
    """One in-play row at every count: the pool reader refuses an empty count."""
    return [(b, s, "in_play", 1.0, season) for b in range(4) for s in range(3)]


class TestThePoolReader:
    def test_the_matrix_counts_rows_per_count_and_class(self):
        con = _mini_pool(
            _grid_rows()
            + [
                (0, 0, "ball", 1.0, 2024),
                (0, 0, "ball", 0.5, 2024),
                (1, 2, "foul", 0.25, 2025),
                (3, 2, "swinging_strike", 1.0, 2025),
                (3, 2, "ball", 1.0, 2019),  # outside the window
            ]
        )
        pred = "season BETWEEN 2023 AND 2026"
        mat = pc.pool_count_matrix(con, pred)
        assert len(mat) == 12 and all(len(row) == 6 for row in mat)
        # The grid puts one in-play row at every count.
        assert mat[0] == [2.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        assert mat[1 * 3 + 2][pc.OUTCOMES.index("foul")] == 1.0
        assert mat[3 * 3 + 2] == [0.0, 0.0, 1.0, 0.0, 1.0, 0.0]
        assert sum(sum(row) for row in mat) == 12.0 + 4.0

    def test_the_weighted_matrix_sums_the_recency_weight(self):
        con = _mini_pool(_grid_rows() + [(0, 0, "ball", 1.0, 2024), (0, 0, "ball", 0.5, 2024)])
        mat = pc.pool_count_matrix(con, "TRUE", weighted=True)
        assert mat[0][0] == pytest.approx(1.5)

    def test_chain_rates_solves_the_pool_matrix(self):
        rows = [(b, s, "ball", 1.0, 2024) for b in range(4) for s in range(3)]
        con = _mini_pool(rows)
        r = pc.chain_rates(con, "TRUE")
        assert r["walk"] == pytest.approx(1.0)
        assert r["pitches"] == pytest.approx(4.0)

    def test_an_unknown_class_raises(self):
        """A seventh class must not vanish from the chain silently."""
        con = _mini_pool(_grid_rows() + [(2, 2, "foul_strike_three", 1.0, 2024)])
        with pytest.raises(ValueError, match="foul_strike_three"):
            pc.pool_count_matrix(con, "TRUE")

    def test_an_empty_count_raises(self):
        """A count with no rows must not lose its mass silently (solve_chain reads it as zeros)."""
        con = _mini_pool([row for row in _grid_rows() if (row[0], row[1]) != (2, 1)])
        with pytest.raises(ValueError, match="2-1"):
            pc.pool_count_matrix(con, "TRUE")

    def test_a_count_emptied_by_the_predicate_raises(self):
        con = _mini_pool(_grid_rows(season=2019) + [(0, 0, "ball", 1.0, 2024)])
        with pytest.raises(ValueError, match="no volume"):
            pc.pool_count_matrix(con, "season >= 2023")

    def test_a_null_class_raises(self):
        con = _mini_pool([(0, 0, None, 1.0, 2024)])  # type: ignore[list-item]
        with pytest.raises(ValueError, match="None"):
            pc.pool_count_matrix(con, "TRUE")

    def test_a_count_outside_the_machine_raises(self):
        con = _mini_pool([(4, 0, "ball", 1.0, 2024)])
        with pytest.raises(ValueError, match="4-0"):
            pc.pool_count_matrix(con, "TRUE")

    def test_an_unknown_class_outside_the_predicate_is_ignored(self):
        con = _mini_pool(_grid_rows() + [(0, 0, "ball", 1.0, 2024), (0, 0, "mystery", 1.0, 2018)])
        mat = pc.pool_count_matrix(con, "season >= 2023")
        assert mat[0] == [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]


# ---------------------------------------------------------------------------
# Real plate appearances (an in-memory ``pg.raw.pitches``)
# ---------------------------------------------------------------------------


def _mini_raw(rows: list[tuple[int, int, str | None, bool, int]]) -> duckdb.DuckDBPyConnection:
    """``rows``: (game_pk, at_bat_number, events, data_quality_flag, season), one per pitch."""
    con = duckdb.connect(":memory:")
    con.execute("ATTACH ':memory:' AS pg")
    con.execute("CREATE SCHEMA pg.raw")
    con.execute(
        "CREATE TABLE pg.raw.pitches (game_pk INTEGER, at_bat_number INTEGER, "
        "events VARCHAR, data_quality_flag BOOLEAN, season INTEGER, game_date DATE)"
    )
    con.executemany("INSERT INTO pg.raw.pitches VALUES (?, ?, ?, ?, ?, DATE '2024-06-01')", rows)
    return con


class TestRealPlateAppearances:
    def _rows(self) -> list[tuple[int, int, str | None, bool, int]]:
        return [
            # PA 1: three pitches, a strikeout.
            (1, 1, None, False, 2024),
            (1, 1, None, False, 2024),
            (1, 1, "strikeout", False, 2024),
            # PA 2: a stolen base mid-PA, then a walk on the fifth pitch.
            (1, 2, None, False, 2024),
            (1, 2, "stolen_base_2b", False, 2024),
            (1, 2, None, False, 2024),
            (1, 2, None, False, 2024),
            (1, 2, "walk", False, 2024),
            # Not a PA: a caught stealing ends the inning mid-count.
            (1, 3, None, False, 2024),
            (1, 3, "caught_stealing_2b", False, 2024),
            # PA 3: one pitch, hit by pitch.
            (1, 4, "hit_by_pitch", False, 2024),
            # PA 4: a strikeout double play on the second pitch.
            (1, 5, None, False, 2024),
            (1, 5, "strikeout_double_play", False, 2024),
            # Not a PA: no event at all (an incomplete feed group).
            (1, 6, None, False, 2024),
            # Not a PA: a runner event or a feed note alone. Each exclusion in
            # pool_chain._NON_PA_EVENTS / _NON_PA_EVENT_PREFIXES has its own
            # lone-event group, so dropping any one of them fails the n_pa check.
            (1, 7, "pickoff_1b", False, 2024),
            (1, 8, "wild_pitch", False, 2024),
            (1, 9, "game_advisory", False, 2024),
            (1, 10, "intent_walk", False, 2024),
            (1, 13, "stolen_base_home", False, 2024),
            (1, 14, "runner_double_play", False, 2024),
            (1, 15, "other_advance", False, 2024),
            (1, 16, "balk", False, 2024),
            (1, 17, "passed_ball", False, 2024),
            (1, 18, "other_out", False, 2024),
            # Flagged rows never count.
            (1, 11, "strikeout", True, 2024),
            # Outside the window.
            (1, 12, "strikeout", False, 2019),
        ]

    def test_the_plate_appearance_definition(self):
        con = _mini_raw(self._rows())
        r = pc.real_pa_rates(con, "season BETWEEN 2023 AND 2026")
        assert r["n_pa"] == 4.0
        assert r["k"] == pytest.approx(2 / 4)
        assert r["walk"] == pytest.approx(1 / 4)
        assert r["hbp"] == pytest.approx(1 / 4)
        # 3 + 5 + 1 + 2 pitch rows over four plate appearances.
        assert r["pitches"] == pytest.approx(11 / 4)

    def test_no_plate_appearance_raises(self):
        con = _mini_raw([(1, 1, None, False, 2024)])
        with pytest.raises(ValueError, match="no plate appearance"):
            pc.real_pa_rates(con, "TRUE")

    def test_attach_pg_is_a_no_op_when_pg_is_attached(self):
        """A second attach must not reach for Postgres (the in-memory ``pg`` stays)."""
        con = _mini_raw(self._rows())
        pc.attach_pg(con, dsn="postgresql://nobody:secret@nowhere:1/none")
        assert pc.real_pa_rates(con, "season = 2024")["n_pa"] == 4.0


# ---------------------------------------------------------------------------
# pa_only: the pool rows of real plate appearances (the label check's chain)
# ---------------------------------------------------------------------------

#: A pitch: (game_pk, at_bat_number, events, balls, strikes, outcome_type,
#: data_quality_flag, season).
_Pitch = tuple[int, int, str | None, int, int, str, bool, int]

_WINDOW = "season BETWEEN 2023 AND 2026"
_GRID_AB = 100


def _grid_pa(season: int = 2024) -> list[_Pitch]:
    """One plate appearance with a pitch at every count; only its last row carries an event.

    The pool reader refuses an empty count, so every pool below carries this
    group. It adds twelve rows and one plate appearance.
    """
    return [
        (1, _GRID_AB, "field_out" if i == 11 else None, i // 3, i % 3, "in_play", False, season)
        for i in range(12)
    ]


def _twin_stores(rows: list[_Pitch]) -> duckdb.DuckDBPyConnection:
    """A ``pg.raw.pitches`` and a ``sim.pitch_pool`` built from the same pitches.

    The pool takes the unflagged rows (the pool build's own filter) at a
    recency weight of 0.5. Both tables carry ``pitch_number`` (1, 2, ... in
    each group's row order), a column the two real tables share, so a
    predicate on it can split a group on both sides alike.
    """
    con = duckdb.connect(":memory:")
    con.execute("ATTACH ':memory:' AS pg")
    con.execute("CREATE SCHEMA pg.raw")
    con.execute(
        "CREATE TABLE pg.raw.pitches (game_pk INTEGER, at_bat_number INTEGER, "
        "pitch_number INTEGER, events VARCHAR, data_quality_flag BOOLEAN, season INTEGER, "
        "game_date DATE)"
    )
    con.execute("CREATE SCHEMA sim")
    con.execute(
        "CREATE TABLE sim.pitch_pool (game_pk INTEGER, at_bat_number INTEGER, "
        "pitch_number INTEGER, events VARCHAR, count_balls INTEGER, count_strikes INTEGER, "
        "outcome_type VARCHAR, recency_weight DOUBLE, season INTEGER)"
    )
    seen: dict[tuple[int, int], int] = {}
    numbered = []
    for row in rows:
        key = (row[0], row[1])
        seen[key] = seen.get(key, 0) + 1
        numbered.append((seen[key], row))
    con.executemany(
        "INSERT INTO pg.raw.pitches VALUES (?, ?, ?, ?, ?, ?, DATE '2024-06-01')",
        [(g, ab, n, ev, flag, season) for n, (g, ab, ev, _b, _s, _o, flag, season) in numbered],
    )
    con.executemany(
        "INSERT INTO sim.pitch_pool VALUES (?, ?, ?, ?, ?, ?, ?, 0.5, ?)",
        [
            (g, ab, n, ev, b, s, o, season)
            for n, (g, ab, ev, b, s, o, flag, season) in numbered
            if not flag
        ],
    )
    return con


def _volume(mat: list[list[float]]) -> float:
    return sum(sum(row) for row in mat)


#: Batting events: each ends a plate appearance.
_BATTING_EVENTS = (
    "strikeout",
    "strikeout_double_play",
    "walk",
    "hit_by_pitch",
    "field_out",
    "single",
    "home_run",
    "sac_fly",
    "catcher_interf",
)

#: Every exclusion (each exact name, and a value under each prefix), NULL, and
#: the batting events.
_EVENT_CASES: tuple[str | None, ...] = (
    None,
    *pc._NON_PA_EVENTS,
    "caught_stealing_3b",
    "pickoff_2b",
    "pickoff_caught_stealing_home",
    "stolen_base_home",
    *_BATTING_EVENTS,
)


class TestThePlateAppearanceRows:
    """``pa_only`` reads only the pool rows of real plate appearances (SIM-553).

    The label check compares the chain with real plate appearances, so both
    sides must count the same ``(game_pk, at_bat_number)`` groups.
    """

    def test_a_group_cut_short_is_dropped(self):
        rows = _grid_pa() + [
            # An inning ends on a caught stealing mid-count: no batting event.
            (1, 1, None, 0, 0, "ball", False, 2024),
            (1, 1, "caught_stealing_2b", 1, 0, "ball", False, 2024),
            # A group with no event at all (a game ended mid-plate-appearance).
            (1, 2, None, 0, 0, "ball", False, 2024),
            (1, 2, None, 1, 0, "ball", False, 2024),
            (1, 2, None, 2, 0, "ball", False, 2024),
        ]
        con = _twin_stores(rows)
        every = pc.pool_count_matrix(con, _WINDOW)
        pa = pc.pool_count_matrix(con, _WINDOW, pa_only=True)
        assert _volume(every) == 17.0
        assert every[0] == [2.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        assert _volume(pa) == 12.0
        assert pa[0] == [0.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        assert pc.pa_group_count(con, _WINDOW) == 1

    def test_a_batting_event_group_is_kept_whole(self):
        """A mid-PA stolen base does not end the group; the walk does, so all four rows stay."""
        rows = _grid_pa() + [
            (1, 1, None, 0, 0, "ball", False, 2024),
            (1, 1, "stolen_base_2b", 1, 0, "ball", False, 2024),
            (1, 1, None, 2, 0, "ball", False, 2024),
            (1, 1, "walk", 3, 0, "ball", False, 2024),
        ]
        con = _twin_stores(rows)
        pa = pc.pool_count_matrix(con, _WINDOW, pa_only=True)
        assert _volume(pa) == 16.0
        assert pa == pc.pool_count_matrix(con, _WINDOW)
        assert pc.pa_group_count(con, _WINDOW) == 2

    def test_the_group_test_reads_events_never_the_class(self):
        """A group of in-play rows with no event is dropped; a group of balls with a walk is kept."""
        rows = _grid_pa() + [
            (1, 1, None, 0, 0, "in_play", False, 2024),
            (1, 2, "walk", 0, 0, "ball", False, 2024),
        ]
        con = _twin_stores(rows)
        pa = pc.pool_count_matrix(con, _WINDOW, pa_only=True)
        assert pa[0] == [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        assert pc.pa_group_count(con, _WINDOW) == 2

    def test_a_group_outside_the_predicate_stays_out(self):
        rows = _grid_pa() + [(1, 1, "strikeout", 0, 0, "called_strike", False, 2019)]
        con = _twin_stores(rows)
        assert _volume(pc.pool_count_matrix(con, _WINDOW, pa_only=True)) == 12.0
        assert pc.pa_group_count(con, _WINDOW) == 1

    def test_the_weighted_read_sums_the_kept_rows_weight(self):
        rows = _grid_pa() + [(1, 1, None, 0, 0, "ball", False, 2024)]
        con = _twin_stores(rows)
        mat = pc.pool_count_matrix(con, _WINDOW, weighted=True, pa_only=True)
        assert _volume(mat) == pytest.approx(12 * 0.5)

    def test_an_empty_count_among_the_kept_rows_raises(self):
        """Only a cut-short group fills 2-1, so the pa_only read has no row there."""
        rows = [row for row in _grid_pa() if (row[3], row[4]) != (2, 1)] + [
            (1, 1, None, 2, 1, "ball", False, 2024),
        ]
        con = _twin_stores(rows)
        assert _volume(pc.pool_count_matrix(con, _WINDOW)) == 12.0
        with pytest.raises(ValueError, match=r"\(pa_only\) holds no volume at count\(s\) 2-1"):
            pc.pool_count_matrix(con, _WINDOW, pa_only=True)

    def test_chain_rates_passes_pa_only_through(self):
        """A cut-short hit-by-pitch row moves the all-rows chain, not the pa_only chain."""
        rows = _grid_pa() + [(1, 1, None, 0, 0, "hit_by_pitch", False, 2024)]
        con = _twin_stores(rows)
        assert pc.chain_rates(con, _WINDOW)["hbp"] == pytest.approx(0.5)
        assert pc.chain_rates(con, _WINDOW, pa_only=True)["hbp"] == 0.0
        assert pc.chain_rates(con, _WINDOW, pa_only=True)["in_play"] == pytest.approx(1.0)

    @pytest.mark.parametrize("event", _EVENT_CASES)
    def test_both_sides_judge_a_group_alike(self, event):
        """The pool keeps a group's rows exactly when ``real_pa_rates`` counts the group."""
        rows = _grid_pa() + [
            (1, 1, None, 0, 0, "ball", False, 2024),
            (1, 1, event, 1, 0, "ball", False, 2024),
        ]
        con = _twin_stores(rows)
        real = pc.real_pa_rates(con, _WINDOW)
        pa = pc.pool_count_matrix(con, _WINDOW, pa_only=True)
        # The pa_only rows are the rows of the real plate appearances, no more.
        assert _volume(pa) == pytest.approx(real["pitches"] * real["n_pa"])
        assert real["n_pa"] == (2.0 if event in _BATTING_EVENTS else 1.0)
        # The pool's count of those groups is the real count.
        assert pc.pa_group_count(con, _WINDOW) == real["n_pa"]

    def test_both_sides_move_together_when_the_list_changes(self, monkeypatch):
        """One expression judges both sides: drop 'balk' from the list and both count its group."""
        rows = _grid_pa() + [(1, 1, "balk", 0, 0, "ball", False, 2024)]
        con = _twin_stores(rows)
        assert pc.real_pa_rates(con, _WINDOW)["n_pa"] == 1.0
        assert _volume(pc.pool_count_matrix(con, _WINDOW, pa_only=True)) == 12.0
        assert pc.pa_group_count(con, _WINDOW) == 1
        monkeypatch.setattr(
            pc, "_NON_PA_EVENTS", tuple(e for e in pc._NON_PA_EVENTS if e != "balk")
        )
        assert pc.real_pa_rates(con, _WINDOW)["n_pa"] == 2.0
        assert _volume(pc.pool_count_matrix(con, _WINDOW, pa_only=True)) == 13.0
        assert pc.pa_group_count(con, _WINDOW) == 2

    def test_a_flagged_row_is_in_neither_store(self):
        """The pool build drops flagged rows, and the real count skips them: the sides agree."""
        rows = _grid_pa() + [(1, 1, "strikeout", 0, 0, "called_strike", True, 2024)]
        con = _twin_stores(rows)
        assert pc.real_pa_rates(con, _WINDOW)["n_pa"] == 1.0
        assert _volume(pc.pool_count_matrix(con, _WINDOW, pa_only=True)) == 12.0
        assert pc.pa_group_count(con, _WINDOW) == 1

    def test_no_plate_appearance_counts_zero(self):
        """``pa_group_count`` reports an empty window as 0; it does not raise."""
        con = _twin_stores(_grid_pa())
        assert pc.pa_group_count(con, "season = 1999") == 0


#: The split tests' two extra groups beside the grid (which a predicate keeps
#: whole through ``at_bat_number = 100 OR ...``, so no count empties):
#: at-bat 1, a strikeout on three strikes (the event on pitch 3, at 0-2);
#: at-bat 2, a single on the second pitch (the event on pitch 2, at 1-0).
_SPLIT_ROWS: list[_Pitch] = [
    (1, 1, None, 0, 0, "called_strike", False, 2024),
    (1, 1, None, 0, 1, "swinging_strike", False, 2024),
    (1, 1, "strikeout", 0, 2, "swinging_strike", False, 2024),
    (1, 2, None, 0, 0, "ball", False, 2024),
    (1, 2, "single", 1, 0, "in_play", False, 2024),
]


def _keep_grid(pred: str) -> str:
    return f"at_bat_number = {_GRID_AB} OR {pred}"


class TestAPredicateThatSplitsAGroup:
    """``pa_only`` reads a split group on its rows inside the predicate only.

    A predicate on the pitch puts part of a group inside it and part outside.
    The rule (``pool_count_matrix``'s docstring): the group test sees only the
    rows inside the predicate, and only those rows are kept. That is what
    ``real_pa_rates`` does, so the two sides keep the same groups.
    """

    def test_a_group_whose_event_falls_outside_the_predicate_is_dropped(self):
        """``count_strikes < 2`` holds the strikeout's first two pitches but not its event."""
        con = _twin_stores(_grid_pa() + _SPLIT_ROWS)
        pred = _keep_grid("count_strikes < 2")
        pa = pc.pool_count_matrix(con, pred, pa_only=True)
        # The single's two rows join the grid; the strikeout's 0-0 and 0-1 do not.
        assert pa[0] == [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        assert pa[1] == [0.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        assert pa[3] == [0.0, 0.0, 0.0, 0.0, 2.0, 0.0]
        assert _volume(pa) == 14.0
        assert pc.pa_group_count(con, pred) == 2

    def test_only_the_rows_inside_the_predicate_are_kept(self):
        """``count_strikes >= 1`` holds the strikeout's event, so it stays, without its 0-0 pitch."""
        con = _twin_stores(_grid_pa() + _SPLIT_ROWS)
        pred = _keep_grid("count_strikes >= 1")
        pa = pc.pool_count_matrix(con, pred, pa_only=True)
        assert pa[0] == [0.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        assert pa[1] == [0.0, 0.0, 1.0, 0.0, 1.0, 0.0]
        assert pa[2] == [0.0, 0.0, 1.0, 0.0, 1.0, 0.0]
        assert _volume(pa) == 14.0
        assert pc.pa_group_count(con, pred) == 2

    @pytest.mark.parametrize(
        ("pred", "n_pa", "n_rows"),
        [
            # Pitches 1-2: the strikeout's event (pitch 3) is out, so it drops.
            ("pitch_number <= 2", 2, 14),
            # Pitches 2 on: both events are in; the first pitches are not.
            ("pitch_number >= 2", 3, 15),
        ],
    )
    def test_the_split_groups_are_the_real_groups(self, pred, n_pa, n_rows):
        """On a column both tables carry, ``pa_only`` keeps exactly ``real_pa_rates``' groups and rows."""
        con = _twin_stores(_grid_pa() + _SPLIT_ROWS)
        pred = _keep_grid(pred)
        real = pc.real_pa_rates(con, pred)
        pa = pc.pool_count_matrix(con, pred, pa_only=True)
        assert real["n_pa"] == n_pa
        assert pc.pa_group_count(con, pred) == n_pa
        assert real["pitches"] * real["n_pa"] == pytest.approx(n_rows)
        assert _volume(pa) == n_rows


class TestThePlateAppearanceCountLine:
    def test_equal_counts_pass(self):
        line = pc.format_pa_count(708_439, 708_439)
        assert "708,439" in line and line.endswith("PASS")

    def test_unequal_counts_fail_with_the_reason(self):
        line = pc.format_pa_count(708_000, 708_439)
        assert "FAIL" in line and "(-439)" in line
        assert "no longer read the same plate appearances" in line


class TestTheRedaction:
    def test_the_dsn_and_its_password_leave_the_message(self):
        dsn = "postgresql://user:hunter2@db:5432/baseball_sim"
        msg = f"IO Error: Unable to connect to Postgres at {dsn}: password hunter2 rejected"
        out = pc._redact(msg, dsn)
        assert "hunter2" not in out
        assert dsn not in out


# ---------------------------------------------------------------------------
# The label check
# ---------------------------------------------------------------------------


def _rates(k: float, walk: float, hbp: float, pitches: float) -> dict[str, float]:
    return {"k": k, "walk": walk, "hbp": hbp, "pitches": pitches}


class TestTheLabelCheck:
    def test_the_tolerance_is_half_a_percent(self):
        assert pc.LABEL_CHECK_TOLERANCE == 0.005

    def test_the_rows_carry_channel_values_gap_and_verdict(self):
        chain = _rates(0.2163, 0.0850, 0.0113, 3.938)
        real = _rates(0.2255, 0.0826, 0.0112, 3.895)
        rows = pc.label_check(chain, real)
        assert [r[0] for r in rows] == ["k", "walk", "hbp", "pitches"]
        for channel, c, r, gap, _ok in rows:
            assert c == chain[channel] and r == real[channel]
            assert gap == pytest.approx((c - r) / r)
        verdict = {r[0]: r[4] for r in rows}
        # The SIM-553 defect on the all-rows chain: K -4.1%, BB +2.9%, HBP
        # +0.9%, pitches +1.1% (the pa_only read: -4.29 / +2.31 / +1.12 / +0.90%).
        assert verdict == {"k": False, "walk": False, "hbp": False, "pitches": False}

    def test_the_corrected_pool_passes(self):
        """The corrected pool's measured reads on 2023-2026 (2026-09-25).

        The verdict (``pa_only``): +0.09 / -0.25 / +0.08 / +0.07%. The all-rows
        chain passes too (+0.30 / +0.36 / -0.13 / +0.27%), with less margin.
        """
        real = _rates(0.22554, 0.08259, 0.01118, 3.8949)
        pa_only = {
            k: v * (1.0 + g)
            for (k, v), g in zip(real.items(), (0.0009, -0.0025, 0.0008, 0.0007), strict=True)
        }
        assert all(r[4] for r in pc.label_check(pa_only, real))
        all_rows = _rates(0.22622, 0.08289, 0.01117, 3.90555)
        assert all(r[4] for r in pc.label_check(all_rows, real))

    def test_the_sim553_coding_fails_on_the_pa_only_read(self):
        """The old coding, pa_only (2026-09-25): -4.29 / +2.31 / +1.12 / +0.90% — every channel fails."""
        rows = pc.label_check(
            _rates(0.2159, 0.0845, 0.0113, 3.9301), _rates(0.22554, 0.08259, 0.01118, 3.8949)
        )
        assert not any(r[4] for r in rows)

    def test_the_default_tolerance_either_side(self):
        real = _rates(0.2, 0.08, 0.01, 4.0)
        inside = _rates(0.2 * 1.004, 0.08 * 0.996, 0.01, 4.0)
        outside = _rates(0.2 * 1.006, 0.08 * 0.994, 0.01, 4.0)
        assert all(r[4] for r in pc.label_check(inside, real))
        verdict = {r[0]: r[4] for r in pc.label_check(outside, real)}
        assert verdict == {"k": False, "walk": False, "hbp": True, "pitches": True}

    def test_the_edge_is_inclusive(self):
        """Binary-exact values: a gap of exactly the tolerance passes, a hair more fails."""
        real = _rates(2.0, 2.0, 2.0, 2.0)
        at_edge = _rates(2.5, 1.5, 2.0, 2.0)
        rows = pc.label_check(at_edge, real, tolerance=0.25)
        assert [r[3] for r in rows] == [0.25, -0.25, 0.0, 0.0]
        assert all(r[4] for r in rows)
        beyond = _rates(np.nextafter(2.5, 3.0), 2.0, 2.0, 2.0)
        assert not pc.label_check(beyond, real, tolerance=0.25)[0][4]

    def test_a_zero_real_rate(self):
        real = _rates(0.2, 0.08, 0.0, 4.0)
        both_zero = pc.label_check(_rates(0.2, 0.08, 0.0, 4.0), real)
        assert both_zero[2][3] == 0.0 and both_zero[2][4]
        chain_only = pc.label_check(_rates(0.2, 0.08, 0.001, 4.0), real)
        assert chain_only[2][3] == float("inf") and not chain_only[2][4]

    def test_the_real_count_carries_extra_keys(self):
        """``real_pa_rates`` returns ``n_pa`` beside the four channels; the check ignores it."""
        real = {**_rates(0.2, 0.08, 0.01, 4.0), "n_pa": 708_439.0}
        assert len(pc.label_check(_rates(0.2, 0.08, 0.01, 4.0), real)) == 4

    def test_the_text_lines_say_pass_and_fail(self):
        rows = pc.label_check(
            _rates(0.2163, 0.0826, 0.0112, 3.895), _rates(0.2255, 0.0826, 0.0112, 3.895)
        )
        lines = pc.format_label_check(rows)
        assert len(lines) == 4
        assert "K/PA" in lines[0] and "FAIL" in lines[0] and "-4.08%" in lines[0]
        assert "BB/PA" in lines[1] and "PASS" in lines[1]

    def test_the_gap_line_names_every_channel(self):
        rows = pc.label_check(
            _rates(0.2163, 0.0850, 0.0113, 3.9382), _rates(0.2255, 0.0826, 0.0112, 3.8949)
        )
        assert pc.format_gaps(rows) == (
            "K/PA -4.08%  BB/PA +2.91%  HBP/PA +0.89%  pitches/PA +1.11%"
        )
