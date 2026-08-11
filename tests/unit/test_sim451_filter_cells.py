"""
tests/unit/test_sim451_filter_cells.py
======================================
Unit tests for the SIM-451 filter-cell measurement (``scripts/measure_filter_cells.py``).

These run with NO database. The script imports ``duckdb`` lazily, so importing
the module touches nothing. What they exercise is the PURE cell algebra that the
whole measurement rests on:

  * :func:`score_band` — the -5..+5 clamp copied from ``sim_loop.py:1348`` plus
    the 5-band split SIM-451 originates.
  * :func:`count_bucket` — the 12-bucket count map copied from
    ``full_pool_sampler.py:248``.
  * :func:`cell_key` / :func:`decode_cell` — the 2,880-cell encode and its exact
    inverse. A collision or a gap here silently mis-attributes rows.
  * :func:`occupancy_stats` — the DRAW-WEIGHTED under-full share. That share is
    the number SIM-475 reads, so its arithmetic gets its own tests, including the
    case that proves counting cells is the wrong metric.
  * :func:`widening_recovery` — how much under-full draw share each relaxation
    step recovers. The second half of the SIM-451 deliverable.
  * :func:`emptiest_reachable` — the reachability filter on the emptiest-cell
    listing.

FOUR INSTRUMENT TESTS CARRY THEIR OWN FAILURE CASE
--------------------------------------------------
An independent review found that the first version of this measurement reported
success while measuring nothing. Four of its checks could not go red:

  * ``_redact_dsn`` claimed to strip the password and returned a libpq keyword
    DSN whole. ``test_redact_dsn_strips_a_libpq_keyword_password`` is the case
    that the old code FAILS.
  * ``coverage_ok`` was true over a report whose outcome pool was missing a whole
    season. ``test_health_report_fails_on_a_wholly_absent_season`` and
    ``test_run_returns_non_zero_when_a_season_is_missing`` are the cases that the
    old code FAILS.
  * the lazy-``duckdb`` guard asserted ``not hasattr(mfc, "duckdb")``, which is
    true whenever ``duckdb`` IS installed. ``test_duckdb_is_absent_from_a_clean_import``
    checks ``sys.modules`` in a fresh subprocess instead, which the old
    module-scope ``engine_artifacts`` import FAILS.
  * the SQL that produces every number had no behavioural test.
    ``test_cell_key_sql_matches_python_on_every_legal_situation`` runs it in
    DuckDB and compares row by row, and
    ``test_the_sql_comparison_detects_an_injected_band_fault`` proves that
    comparison can detect a wrong band expression.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import os
import subprocess
import sys
import textwrap

import numpy as np
import pytest

# Import the script module by path (scripts/ is not a package). It MUST be
# registered in sys.modules before exec_module so that @dataclass can resolve the
# module's namespace (dataclasses looks the module up by name).
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_MODULE_PATH = os.path.join(_REPO_ROOT, "scripts", "measure_filter_cells.py")
_spec = importlib.util.spec_from_file_location("measure_filter_cells", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
measure_filter_cells = importlib.util.module_from_spec(_spec)
sys.modules["measure_filter_cells"] = measure_filter_cells
_spec.loader.exec_module(measure_filter_cells)

mfc = measure_filter_cells
score_band = mfc.score_band
count_bucket = mfc.count_bucket
cell_key = mfc.cell_key
decode_cell = mfc.decode_cell
occupancy_stats = mfc.occupancy_stats
emptiest_reachable = mfc.emptiest_reachable
unreachable_cells = mfc.unreachable_cells
widening_recovery = mfc.widening_recovery
widen_group_ids = mfc.widen_group_ids
widened_group_size_per_cell = mfc.widened_group_size_per_cell
health_report = mfc.health_report
N_CELLS = mfc.N_CELLS


# ---------------------------------------------------------------------------
# score_band — the clamp plus the 5-band split
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("score_diff", "expected"),
    [
        (-9, 0),
        (-5, 0),
        (-4, 0),
        (-3, 0),
        (-2, 1),
        (-1, 1),
        (0, 2),
        (1, 3),
        (2, 3),
        (3, 4),
        (5, 4),
        (9, 4),
    ],
)
def test_score_band_edges(score_diff: int, expected: int) -> None:
    """Every band boundary lands where SCORE_BAND_EDGES says it does."""
    assert score_band(score_diff) == expected


def test_score_band_clamp_collapses_blowouts() -> None:
    """The clamp makes a 9-run deficit the same situation as a 5-run deficit.

    That is the ``sim_loop.py:1348`` behaviour: the sim compresses everything
    beyond five runs onto the boundary. A measurement that skipped the clamp
    would spread those rows across bands the sampler never separates.
    """
    assert score_band(-9) == score_band(-5) == score_band(-100)
    assert score_band(9) == score_band(5) == score_band(100)


def test_score_band_produces_exactly_five_bands() -> None:
    """No input in a wide range escapes the 0..4 range, and all five are used."""
    bands = {score_band(sd) for sd in range(-20, 21)}
    assert bands == {0, 1, 2, 3, 4}
    assert len(mfc.SCORE_BAND_LABELS) == mfc.N_BAND == 5


def test_score_band_is_monotone() -> None:
    """A larger lead never lands in a lower band. A non-monotone split would mean
    the edges are out of order."""
    seq = [score_band(sd) for sd in range(-20, 21)]
    assert seq == sorted(seq)


# ---------------------------------------------------------------------------
# count_bucket — the 12-bucket count map
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("balls", "strikes", "expected"),
    [(0, 0, 0), (0, 1, 1), (0, 2, 2), (1, 0, 3), (2, 1, 7), (3, 0, 9), (3, 2, 11)],
)
def test_count_bucket_legal_counts(balls: int, strikes: int, expected: int) -> None:
    assert count_bucket(balls, strikes) == expected


def test_count_bucket_saturates_out_of_range() -> None:
    """The saturating min/max is part of the copied definition, not a guard we added.

    A 4th ball and a 3rd strike end the plate appearance, so they never reach the
    sampler. When they do arrive the map pins them to the nearest legal count
    instead of raising.
    """
    assert count_bucket(4, 3) == 11
    assert count_bucket(99, 99) == 11
    assert count_bucket(-1, -1) == 0


def test_count_bucket_covers_twelve_buckets_without_collision() -> None:
    """The 4x3 legal grid maps onto exactly {0..11}."""
    seen = [count_bucket(b, s) for b in range(4) for s in range(3)]
    assert sorted(seen) == list(range(12))


# ---------------------------------------------------------------------------
# cell_key / decode_cell — the 2,880-cell bijection
# ---------------------------------------------------------------------------


def _legal_coordinates() -> list[tuple[int, int, int, int, int, int]]:
    """Every legal (base, outs, balls, strikes, score_diff, home) combination.

    One representative score difference per band, so the sweep visits all 2,880
    cells exactly once.
    """
    band_reps = (-5, -1, 0, 2, 5)
    return [
        (rs, outs, balls, strikes, sd, home)
        for rs in range(8)
        for outs in range(3)
        for balls in range(4)
        for strikes in range(3)
        for sd in band_reps
        for home in range(2)
    ]


def test_cell_key_is_a_bijection_onto_2880_cells() -> None:
    """Every legal situation gets its own cell, and every cell gets a situation.

    A collision would merge two situations into one pool; a gap would leave a
    cell unreachable by construction. Both would make the occupancy report lie.
    """
    coords = _legal_coordinates()
    assert len(coords) == N_CELLS == 2880
    keys = [cell_key(*c) for c in coords]
    assert sorted(keys) == list(range(N_CELLS))


def test_decode_cell_round_trips_every_cell() -> None:
    """decode_cell inverts cell_key for all 2,880 cells."""
    for rs, outs, balls, strikes, sd, home in _legal_coordinates():
        cid = cell_key(rs, outs, balls, strikes, sd, home)
        got = decode_cell(cid)
        assert got.runners_state == rs
        assert got.outs == outs
        assert got.balls == balls
        assert got.strikes == strikes
        assert got.band == score_band(sd)
        assert got.bat_is_home == home
        assert cell_key(got.runners_state, got.outs, got.balls, got.strikes, sd, home) == cid


def test_cell_key_known_value() -> None:
    """One hand-computed cell, so a refactor of the encode cannot pass silently.

    Bases loaded, two out, full count, the home team trailing by four:
    ((((7*3 + 2) * 12 + 11) * 5 + 0) * 2) + 1 == 2871.
    """
    cid = cell_key(runners_state=7, outs=2, balls=3, strikes=2, score_diff=-4, bat_is_home=1)
    assert cid == 2871
    coords = decode_cell(2871)
    assert coords.label == "bases=123 outs=2 count=3-2 band=<=-3 HOME"


def test_decode_cell_rejects_out_of_range() -> None:
    for bad in (-1, N_CELLS, N_CELLS + 10):
        with pytest.raises(ValueError):
            decode_cell(bad)


def test_cell_key_masks_the_base_state() -> None:
    """runners_state is a 3-bit mask. Bit 3 and above are not part of the cell."""
    assert cell_key(0, 0, 0, 0, 0, 0) == cell_key(8, 0, 0, 0, 0, 0)


# ---------------------------------------------------------------------------
# occupancy_stats — the draw-weighted under-full share
# ---------------------------------------------------------------------------


def test_occupancy_stats_draw_share_weights_by_reach() -> None:
    """The headline share weights each cell by how often play reaches it.

    2,000 cells hold 1 row each and 880 cells hold 1,000 rows each. Two thirds of
    the CELLS are under 100 rows, but those cells take only 0.227% of the DRAWS.
    Counting cells would report a crisis that does not exist.
    """
    occ = np.zeros(N_CELLS, dtype=np.int64)
    occ[:2000] = 1
    occ[2000:] = 1000
    stats = occupancy_stats(occ, min_cells=(100,))
    assert stats["total_rows"] == 2000 + 880 * 1000 == 882000
    under = stats["under"]["100"]
    assert under["cells"] == 2000
    assert under["rows"] == 2000
    assert under["draw_share_pct"] == pytest.approx(100.0 * 2000 / 882000)
    assert under["draw_share_pct"] == pytest.approx(0.22676, abs=1e-5)
    # 69% of cells are thin; 0.2% of draws are. That gap is the whole point.
    assert under["cells"] / N_CELLS > 0.69


def test_occupancy_stats_thin_cells_can_dominate_the_draws() -> None:
    """The mirror case: few thin cells, but they take most of the draws.

    99 rows in each of 2,879 cells and 1 huge cell. Under a 100-row minimum only
    2,879 of 2,880 cells are thin, and they still take 96.6% of the draws. Cell
    count alone cannot tell these two worlds apart.
    """
    occ = np.full(N_CELLS, 99, dtype=np.int64)
    occ[0] = 10_000
    stats = occupancy_stats(occ, min_cells=(100,))
    under = stats["under"]["100"]
    assert under["cells"] == N_CELLS - 1
    assert under["draw_share_pct"] == pytest.approx(
        100.0 * (99 * (N_CELLS - 1)) / (99 * (N_CELLS - 1) + 10_000)
    )
    assert under["draw_share_pct"] > 96.0


def test_occupancy_stats_all_zero_is_safe() -> None:
    """An empty pool reports a 0.0 share, never a division by zero."""
    stats = occupancy_stats(np.zeros(N_CELLS, dtype=np.int64), min_cells=(20, 200))
    assert stats["total_rows"] == 0
    assert stats["cells_nonempty"] == 0
    assert stats["mean_per_cell"] == 0.0
    for m in ("20", "200"):
        assert stats["under"][m]["cells"] == N_CELLS
        assert stats["under"][m]["draw_share_pct"] == 0.0


def test_occupancy_stats_every_cell_above_threshold() -> None:
    """When no cell is thin, both the cell count and the share are zero."""
    occ = np.full(N_CELLS, 500, dtype=np.int64)
    stats = occupancy_stats(occ, min_cells=(20, 200))
    for m in ("20", "200"):
        assert stats["under"][m]["cells"] == 0
        assert stats["under"][m]["draw_share_pct"] == 0.0
    assert stats["cells_nonempty"] == N_CELLS
    assert stats["mean_per_cell"] == pytest.approx(500.0)


def test_occupancy_stats_percentiles_bracket_the_data() -> None:
    """p0 is the minimum, p100 the maximum, and the percentiles never decrease."""
    rng = np.random.default_rng(451)
    occ = rng.integers(0, 5000, size=N_CELLS, dtype=np.int64)
    stats = occupancy_stats(occ)
    pct = stats["percentiles"]
    assert pct["0"] == int(occ.min())
    assert pct["100"] == int(occ.max())
    values = [pct[str(p)] for p in mfc.PERCENTILES]
    assert values == sorted(values)
    assert stats["total_rows"] == int(occ.sum())


def test_occupancy_stats_reports_every_requested_threshold() -> None:
    stats = occupancy_stats(np.ones(N_CELLS, dtype=np.int64), min_cells=(20, 50, 100, 200))
    assert set(stats["under"]) == {"20", "50", "100", "200"}


# ---------------------------------------------------------------------------
# emptiest_reachable / unreachable_cells
# ---------------------------------------------------------------------------


def test_emptiest_reachable_excludes_unreachable_cells() -> None:
    """A cell the reference never reaches is not a thin cell — it is no cell at all."""
    target = np.zeros(N_CELLS, dtype=np.int64)
    reference = np.full(N_CELLS, 100, dtype=np.int64)
    reference[[3, 7, 11]] = 0  # never reached
    got = emptiest_reachable(target, reference, k=50)
    ids = {c["cell_id"] for c in got}
    assert ids.isdisjoint({3, 7, 11})
    assert len(got) == 50


def test_emptiest_reachable_sorts_ascending_and_caps_at_k() -> None:
    target = np.arange(N_CELLS, dtype=np.int64)
    reference = np.ones(N_CELLS, dtype=np.int64)
    got = emptiest_reachable(target, reference, k=5)
    assert [c["cell_id"] for c in got] == [0, 1, 2, 3, 4]
    assert [c["count"] for c in got] == [0, 1, 2, 3, 4]
    assert all(c["reference_count"] == 1 for c in got)
    assert all(c["label"] == decode_cell(c["cell_id"]).label for c in got)


def test_emptiest_reachable_empty_reference_returns_nothing() -> None:
    got = emptiest_reachable(
        np.ones(N_CELLS, dtype=np.int64), np.zeros(N_CELLS, dtype=np.int64), k=20
    )
    assert got == []


def test_unreachable_cells_names_the_gaps() -> None:
    reference = np.ones(N_CELLS, dtype=np.int64)
    reference[[2871, 5]] = 0
    got = unreachable_cells(reference)
    assert [c["cell_id"] for c in got] == [5, 2871]
    assert got[1]["label"] == "bases=123 outs=2 count=3-2 band=<=-3 HOME"
    assert unreachable_cells(np.ones(N_CELLS, dtype=np.int64)) == []


# ---------------------------------------------------------------------------
# The SQL forms must agree with the Python forms
# ---------------------------------------------------------------------------


def test_sql_helpers_build_from_the_python_constants() -> None:
    """The SQL is generated from SCORE_BAND_EDGES, so the two forms cannot drift.

    The score-band SQL is a SUM of one indicator per edge, exactly as the Python
    is. The earlier form was a first-match CASE ladder, which reads the edges in
    tuple order while the Python does not. This asserts the generated text holds
    one ``> edge`` term per edge and the clamp.
    """
    sql = mfc._score_band_sql("p.score_diff")
    for edge in mfc.SCORE_BAND_EDGES:
        assert f"> {edge} THEN 1 ELSE 0" in sql
    assert sql.count("CASE WHEN") == len(mfc.SCORE_BAND_EDGES)
    assert f"LEAST({mfc.SCORE_CLAMP}" in sql
    assert "CASE" in sql and "ELSE 0 END) + (CASE" in sql

    bucket = mfc._count_bucket_sql("p.count_balls", "p.count_strikes")
    assert "count_balls" in bucket and "count_strikes" in bucket and "* 3 +" in bucket

    key = mfc._cell_key_sql("p", "h")
    assert "runners_state" in key and "h.bat_is_home" in key


def test_score_band_edges_must_be_strictly_increasing() -> None:
    """The guard refuses an unsorted edge tuple instead of computing a wrong band.

    ``score_band`` counts how many edges the difference exceeds, which ignores
    tuple order. A reader who expects a first-match ladder reads a different
    band from an unsorted tuple. The validator makes that impossible to load.
    """
    mfc._validate_score_band_edges((-3, -1, 0, 2))  # the shipped tuple loads
    for bad in [(0, -3, -1, 2), (-3, -3, 0, 2), (2, 0, -1, -3)]:
        with pytest.raises(ValueError, match="strictly increasing"):
            mfc._validate_score_band_edges(bad)


# ---------------------------------------------------------------------------
# The SQL that produces 100% of the numbers, run against a real DuckDB
# ---------------------------------------------------------------------------


def _legal_situation_rows() -> list[tuple[int, int, int, int, int, int]]:
    """Every legal situation, with the score difference swept past the clamp."""
    return [
        (rs, outs, balls, strikes, sd, home)
        for rs in range(8)
        for outs in range(3)
        for balls in range(4)
        for strikes in range(3)
        for sd in range(-7, 8)
        for home in range(2)
    ]


def _run_cell_key_sql(key_sql: str) -> list[tuple[int, int]]:
    """Run one cell-key SQL expression over every legal situation in DuckDB.

    Returns ``[(row_id, cell_id), ...]``. The table stands in for
    ``sim.pitch_pool p JOIN ha h``: one alias ``p`` carrying the situation
    columns and one alias ``h`` carrying ``bat_is_home``.
    """
    duckdb = pytest.importorskip("duckdb")
    rows = _legal_situation_rows()
    con = duckdb.connect(":memory:")
    try:
        con.execute(
            "CREATE TABLE p (row_id INTEGER, runners_state INTEGER, outs INTEGER, "
            "count_balls INTEGER, count_strikes INTEGER, score_diff INTEGER)"
        )
        con.execute("CREATE TABLE h (row_id INTEGER, bat_is_home INTEGER)")
        con.executemany(
            "INSERT INTO p VALUES (?, ?, ?, ?, ?, ?)",
            [(i, rs, o, b, s, sd) for i, (rs, o, b, s, sd, _) in enumerate(rows)],
        )
        con.executemany(
            "INSERT INTO h VALUES (?, ?)",
            [(i, home) for i, (_, _, _, _, _, home) in enumerate(rows)],
        )
        return [
            (int(a), int(b))
            for a, b in con.execute(
                f"SELECT p.row_id, {key_sql} FROM p JOIN h USING (row_id) ORDER BY p.row_id"
            ).fetchall()
        ]
    finally:
        con.close()


def test_cell_key_sql_matches_python_on_every_legal_situation() -> None:
    """The SQL cell key equals the Python cell key, row for row.

    The SQL computes 100% of the numbers in the report and the Python computes
    100% of the numbers in these tests. Nothing checked that the two agree. This
    runs the real generated expression in a real DuckDB over 8x3x4x3x15x2 = 8,640
    situations, including score differences outside the +/-5 clamp.
    """
    rows = _legal_situation_rows()
    got = _run_cell_key_sql(mfc._cell_key_sql("p", "h"))
    assert len(got) == len(rows)
    for (row_id, sql_cell), coords in zip(got, rows, strict=True):
        expected = cell_key(*coords)
        assert sql_cell == expected, f"row {row_id} {coords}: SQL {sql_cell} != Python {expected}"
    # The sweep must actually visit every cell, or the comparison proves little.
    assert len({c for _, c in got}) == N_CELLS


def test_the_sql_comparison_detects_an_injected_band_fault() -> None:
    """The comparison above can go red. Here is a fault that makes it.

    A first-match CASE ladder over the edges in a DIFFERENT order is exactly the
    drift the shipped sum-of-indicators form removes. Injecting it must produce a
    mismatch. If this test passes with no mismatch, the comparison is decorative.
    """
    clamped = (
        f"GREATEST(-{mfc.SCORE_CLAMP}, LEAST({mfc.SCORE_CLAMP}, CAST(p.score_diff AS INTEGER)))"
    )
    scrambled = (0, -3, -1, 2)  # same edges, wrong order, first-match semantics
    whens = " ".join(f"WHEN {clamped} <= {edge} THEN {i}" for i, edge in enumerate(scrambled))
    faulty_band = f"(CASE {whens} ELSE {len(scrambled)} END)"

    rs = "(CAST(p.runners_state AS INTEGER) % 8)"
    outs = "LEAST(GREATEST(CAST(p.outs AS INTEGER), 0), 2)"
    bucket = mfc._count_bucket_sql("p.count_balls", "p.count_strikes")
    faulty_key = (
        f"((((({rs}) * 3 + {outs}) * 12 + {bucket}) * 5 + {faulty_band})"
        f" * 2 + CAST(h.bat_is_home AS INTEGER))"
    )

    rows = _legal_situation_rows()
    got = _run_cell_key_sql(faulty_key)
    mismatches = [
        (coords, sql_cell)
        for (_, sql_cell), coords in zip(got, rows, strict=True)
        if sql_cell != cell_key(*coords)
    ]
    assert mismatches, "the SQL-vs-Python comparison did not notice a wrong band expression"


# ---------------------------------------------------------------------------
# BLOCKER 1 — the credential leak into the committed report
# ---------------------------------------------------------------------------


def test_redact_dsn_strips_a_libpq_keyword_password() -> None:
    """A libpq keyword DSN holds no '@'. The old redactor returned it WHOLE.

    ``_redact_dsn`` writes the ``dsn_host`` field of a JSON file that is
    committed as evidence, and the repository runs a CI secrets-check job. The
    old body was ``dsn.rsplit("@", 1)[-1]``: with no '@' in the string, rsplit
    returns the whole string, password included. This is the case that fails on
    that body and passes on the fixed one.
    """
    dsn = "host=db port=5432 user=baseball_user password=hunter2SECRET dbname=baseball_sim"
    got = mfc._redact_dsn(dsn)
    assert "hunter2SECRET" not in got
    assert "password" not in got
    assert got == "db:5432/baseball_sim"


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        # URL form — the shape docker-compose.yml:108 uses.
        ("postgresql://baseball_user:baseball_pass@db:5432/baseball_sim", "db:5432/baseball_sim"),
        ("postgres://u:p@localhost:5433/baseball_sim", "localhost:5433/baseball_sim"),
        ("postgresql://u:p@db/baseball_sim", "db/baseball_sim"),
        # Query options must not survive; they can carry sslpassword.
        ("postgresql://u:s3cret@db:5432/x?sslmode=require&sslpassword=q", "db:5432/x"),
        # URL-encoded '@' inside the password.
        ("postgresql://u:p%40ss%40word@db:5432/x", "db:5432/x"),
        # libpq keyword form, in several orders and with quoting.
        ("host=db port=5432 user=x password=SECRET dbname=y", "db:5432/y"),
        ("password=SECRET dbname=y host=db", "db/y"),
        ("dbname=y host=db port=5432 password='a b c'", "db:5432/y"),
        ("password = SECRET host = db", "db"),
        ("hostaddr=10.0.0.4 port=5432 dbname=y password=SECRET", "10.0.0.4:5432/y"),
        # Nothing safe to show.
        ("password=SECRET user=x", "<redacted>"),
        # A bare string is not a valid libpq DSN, so it is refused rather than
        # echoed. A password put in the wrong variable arrives looking like this.
        ("hunter2", "<redacted>"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_redact_dsn_handles_both_dsn_forms(dsn: str, expected: str) -> None:
    assert mfc._redact_dsn(dsn) == expected


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://baseball_user:LEAKME@db:5432/baseball_sim",
        "host=db port=5432 user=baseball_user password=LEAKME dbname=baseball_sim",
        "postgresql://u:LEAKME@db:5432/x?sslmode=require",
        "postgresql://u:LEAK://ME@db:5432/x",
        "dbname=y password='LEAKME' host=db",
        "sslpassword=LEAKME host=db dbname=y",
        "LEAKME",
        "postgresql://LEAKME",
        "postgresql://u:p@[::1]:5432/x",
    ],
)
def test_redact_dsn_never_echoes_the_secret(dsn: str) -> None:
    """Deny by default: no input form leaks the marker into the report."""
    got = mfc._redact_dsn(dsn)
    assert "LEAKME" not in got
    assert mfc._SAFE_DSN_SHAPE.match(got) or got in ("", mfc.REDACTED)


def test_redact_text_scrubs_a_dsn_out_of_an_attach_error() -> None:
    """A DuckDB ATTACH failure quotes the connection string back.

    ``open_connection`` logs that message. Logging it raw writes the password to
    stdout and into any captured CI log.
    """
    dsn = "host=db port=5432 user=x password=hunter2SECRET dbname=y"
    message = f'Failed to connect using "{dsn}": connection refused'
    got = mfc._redact_text(message, dsn)
    assert "hunter2SECRET" not in got
    assert "db:5432/y" in got
    assert "connection refused" in got


# ---------------------------------------------------------------------------
# BLOCKER 5 — the widening ladder
# ---------------------------------------------------------------------------


def test_widen_group_ids_merge_only_the_dropped_dimension() -> None:
    """Dropping a dimension merges exactly the cells that differ only in it."""
    gid, n_groups = widen_group_ids(("home_or_away",))
    assert n_groups == N_CELLS // 2
    # Cell 2*k and cell 2*k+1 differ only in bat_is_home, so they must merge.
    assert gid[0] == gid[1]
    assert gid[2] != gid[0]
    assert len(set(gid.tolist())) == n_groups


def test_widen_group_ids_vectorised_coordinates_match_decode_cell() -> None:
    """The vectorised decode used by the ladder equals the readable decode_cell."""
    coords = mfc._CELL_COORDS
    for cid in (0, 1, 5, 1234, 2871, N_CELLS - 1):
        got = decode_cell(cid)
        assert int(coords["base"][cid]) == got.runners_state
        assert int(coords["outs"][cid]) == got.outs
        assert int(coords["count"][cid]) == count_bucket(got.balls, got.strikes)
        assert int(coords["score_band"][cid]) == got.band
        assert int(coords["home_or_away"][cid]) == got.bat_is_home


def test_widen_group_ids_rejects_an_unknown_dimension() -> None:
    with pytest.raises(ValueError, match="unknown widening dimension"):
        widen_group_ids(("park",))


def test_widen_dropping_everything_leaves_one_group() -> None:
    gid, n_groups = widen_group_ids(mfc.CELL_DIMENSIONS)
    assert n_groups == 1
    assert set(gid.tolist()) == {0}


def test_widened_group_size_conserves_the_row_total() -> None:
    """Widening moves no rows. Every step must see the same total."""
    rng = np.random.default_rng(451)
    occ = rng.integers(0, 300, size=N_CELLS, dtype=np.int64)
    for dropped in mfc.WIDEN_LADDER:
        sizes, n_groups = widened_group_size_per_cell(occ, tuple(dropped))
        gid, _ = widen_group_ids(tuple(dropped))
        totals = np.zeros(n_groups, dtype=np.int64)
        np.add.at(totals, gid, occ)
        assert int(totals.sum()) == int(occ.sum())
        # Every cell reads the size of its own group.
        assert np.array_equal(sizes, totals[gid])


def test_widening_recovery_recovers_a_thin_cell_by_relaxing_the_band() -> None:
    """The headline: a cell thin on its own becomes full once the band relaxes.

    Give every cell 60 rows. Under MIN_CELL=100 the whole pool is under-full, so
    the exact-cell baseline share is 100%. Merging the five score bands makes
    every group 300 rows, so step 1 recovers all of it.
    """
    occ = np.full(N_CELLS, 60, dtype=np.int64)
    got = widening_recovery(occ, min_cells=(100,))
    ladder = got["ladder"]
    assert ladder[0]["drops"] == [] and ladder[0]["n_groups"] == N_CELLS
    assert ladder[0]["under"]["100"]["draw_share_pct"] == pytest.approx(100.0)
    step1 = ladder[1]
    assert step1["drops"] == ["score_band"]
    assert step1["n_groups"] == N_CELLS // 5
    assert step1["under"]["100"]["draw_share_pct"] == pytest.approx(0.0)
    assert step1["under"]["100"]["recovered_pct_points"] == pytest.approx(100.0)
    assert step1["under"]["100"]["recovered_share_of_baseline_pct"] == pytest.approx(100.0)


def test_widening_recovery_surviving_share_only_falls() -> None:
    """A widened group contains its exact cell, so the ladder is monotone.

    A step that reported MORE under-full share than the step before it would mean
    the group arithmetic is wrong.
    """
    rng = np.random.default_rng(7)
    occ = rng.integers(0, 400, size=N_CELLS, dtype=np.int64)
    got = widening_recovery(occ, min_cells=(20, 50, 100, 200))
    for m in ("20", "50", "100", "200"):
        shares = [s["under"][m]["draw_share_pct"] for s in got["ladder"]]
        assert shares == sorted(shares, reverse=True), f"MIN_CELL={m} ladder is not monotone"
        recovered = [s["under"][m]["recovered_pct_points"] for s in got["ladder"][1:]]
        assert all(r >= -1e-9 for r in recovered)


def test_widening_recovery_ranks_the_first_drop_from_the_data() -> None:
    """The ranking must follow the data, not the decided order.

    Build a pool where the score band is NOT the best first drop. Every row sits
    in score band 2, and each populated cell holds 12 rows. Under MIN_CELL=100
    the whole pool is under-full, and the five single drops merge different
    numbers of POPULATED cells:

      * count merges 12 of them -> 144 rows, above the minimum, recovers 100%;
      * base merges 8 -> 96, still short;
      * outs merges 3 -> 36; home_or_away merges 2 -> 24;
      * score_band merges 5 cells of which only ONE is populated -> 12, no gain.

    So the count wins outright and the report must say the data CONTRADICTS
    'relax the score band first'. A ranking that answered 'score_band' here would
    be restating the decision instead of measuring it.
    """
    occ = np.zeros(N_CELLS, dtype=np.int64)
    for cid in range(N_CELLS):
        if decode_cell(cid).band == 2:
            occ[cid] = 12
    got = widening_recovery(occ, min_cells=(100,))
    assert got["admissible_first_drop_ranking"]["100"] == ["count", "home_or_away", "score_band"]
    assert got["first_drop_ranking"]["100"][0] == "count"
    assert got["score_band_is_the_best_first_drop"]["100"] is False
    band = next(s for s in got["single_drop"] if s["drops"] == ["score_band"])
    assert band["under"]["100"]["recovered_pct_points"] == pytest.approx(0.0)
    count = next(s for s in got["single_drop"] if s["drops"] == ["count"])
    assert count["under"]["100"]["recovered_share_of_baseline_pct"] == pytest.approx(100.0)


def test_widening_verdict_ignores_the_dimensions_the_ladder_may_not_relax() -> None:
    """The verdict must weigh only the drops SIM-475 is allowed to make.

    Every row sits in count bucket 0, 15 rows a cell, MIN_CELL=100. Then:

      * dropping the BASE state merges 8 populated cells -> 120 rows, full
        recovery. The base state is INADMISSIBLE: it is the situation the hard
        filter exists to hold fixed.
      * dropping the count merges 12 cells of which only ONE is populated -> 15
        rows, no gain. Same for the score band (5 cells, 75 rows) and
        home-or-away (2 cells, 30 rows).

    So the five-dimension diagnostic names ``base`` first, and the verdict on
    the decided order must ignore it. A verdict computed over all five would
    read 'the data contradicts the order' and point SIM-475 at a relaxation the
    design forbids.
    """
    occ = np.zeros(N_CELLS, dtype=np.int64)
    for cid in range(N_CELLS):
        coords = decode_cell(cid)
        if count_bucket(coords.balls, coords.strikes) == 0:
            occ[cid] = 15
    got = widening_recovery(occ, min_cells=(100,))

    assert got["first_drop_ranking"]["100"][0] == "base"  # the diagnostic
    assert got["inadmissible_dimensions"] == ["base", "outs"]
    assert set(got["admissible_first_drop_ranking"]["100"]) == set(mfc.WIDEN_ADMISSIBLE)
    assert "base" not in got["admissible_first_drop_ranking"]["100"]
    assert "outs" not in got["admissible_first_drop_ranking"]["100"]
    # No admissible drop beats the score band here, so the order still stands.
    assert got["score_band_is_the_best_first_drop"]["100"] is True
    base = next(s for s in got["single_drop"] if s["drops"] == ["base"])
    assert base["admissible"] is False
    assert base["under"]["100"]["recovered_share_of_baseline_pct"] == pytest.approx(100.0)
    for entry in got["single_drop"]:
        assert entry["admissible"] == (entry["drops"][0] in mfc.WIDEN_ADMISSIBLE)


def test_widening_recovery_confirms_the_decided_order_when_the_data_supports_it() -> None:
    """The mirror case: spread across bands, so relaxing the band does recover."""
    occ = np.full(N_CELLS, 30, dtype=np.int64)
    got = widening_recovery(occ, min_cells=(100,))
    assert got["score_band_is_the_best_first_drop"]["100"] is True
    assert got["decided_order"] == ["score_band", "home_or_away", "count"]


def test_widening_recovery_empty_pool_is_safe() -> None:
    got = widening_recovery(np.zeros(N_CELLS, dtype=np.int64), min_cells=(20,))
    for step in got["ladder"]:
        assert step["under"]["20"]["draw_share_pct"] == 0.0
        assert step["under"]["20"]["rows"] == 0


# ---------------------------------------------------------------------------
# BLOCKER 6 — the false green and the missing exit code
# ---------------------------------------------------------------------------


def _healthy_inputs() -> dict[str, object]:
    """A minimal complete measurement: two seasons, both pools, no gaps."""
    counts = {
        "pitch_pool": {"2024": 700_000, "2025": 700_000},
        "outcome_pool": {"2024": 120_000, "2025": 120_000},
    }
    coverage = {
        table: [
            {"season": 2024, "pool_rows": 10, "matched": 10, "dropped": 0, "coverage_pct": 100.0},
            {"season": 2025, "pool_rows": 10, "matched": 10, "dropped": 0, "coverage_pct": 100.0},
        ]
        for table in ("pitch_pool", "outcome_pool")
    }
    configurations = {
        f"{table}|recent|ALL": {
            "table": table,
            "seasons": [2024, 2025],
            "seasons_measured": [2024, 2025],
            "seasons_missing": [],
        }
        for table in ("pitch_pool", "outcome_pool")
    }
    return {"season_counts": counts, "coverage": coverage, "configurations": configurations}


def test_health_report_passes_a_complete_measurement() -> None:
    got = health_report(**_healthy_inputs(), min_coverage=95.0)  # type: ignore[arg-type]
    assert got["coverage_ok"] is True
    assert got["problems"] == []


def test_health_report_fails_on_a_wholly_absent_season() -> None:
    """THE case the old flag could not see, taken from the real committed report.

    ``sim.outcome_pool`` held seasons 2017..2025 while ``sim.pitch_pool`` held
    2017..2026. The old ``coverage_ok`` was the join-coverage loop alone, and
    that loop iterates the seasons a pool HOLDS, so it never visited 2026 and
    never went false. This is the reproduction, shrunk to two seasons.
    """
    inputs = _healthy_inputs()
    counts = inputs["season_counts"]
    counts["pitch_pool"]["2026"] = 464_063  # type: ignore[index]
    # outcome_pool gets no 2026 key at all — exactly the real gap.
    got = health_report(**inputs, min_coverage=95.0)  # type: ignore[arg-type]
    assert got["coverage_ok"] is False
    assert got["pools_hold_every_season"] is False
    assert got["missing_seasons"] == {"outcome_pool": [2026]}
    assert any("ZERO rows for season(s) [2026]" in p for p in got["problems"])
    # The join-coverage question on its own still answers "fine". That is the
    # precise reason the old flag was green.
    assert got["join_coverage_ok"] is True


def test_health_report_fails_a_configuration_that_measured_fewer_seasons() -> None:
    """A three-season label over a two-season measurement must go red."""
    inputs = _healthy_inputs()
    inputs["configurations"]["outcome_pool|recent|ALL"] = {  # type: ignore[index]
        "table": "outcome_pool",
        "seasons": [2024, 2025, 2026],
        "seasons_measured": [2024, 2025],
        "seasons_missing": [2026],
    }
    got = health_report(**inputs, min_coverage=95.0)  # type: ignore[arg-type]
    assert got["coverage_ok"] is False
    assert got["configurations_complete"] is False
    assert got["configuration_season_gaps"]["outcome_pool|recent|ALL"]["seasons_missing"] == [2026]


def test_health_report_still_fails_on_a_thin_join() -> None:
    """The original check survives the rewrite."""
    inputs = _healthy_inputs()
    inputs["coverage"]["pitch_pool"][0]["coverage_pct"] = 41.5  # type: ignore[index]
    got = health_report(**inputs, min_coverage=95.0)  # type: ignore[arg-type]
    assert got["coverage_ok"] is False
    assert got["join_coverage_ok"] is False
    assert any("below 95.0%" in p for p in got["problems"])


# ---------------------------------------------------------------------------
# The end-to-end exit code, over a synthetic DuckDB that reproduces the gap
# ---------------------------------------------------------------------------


def _build_fake_pools(path: str, outcome_seasons: tuple[int, ...]) -> None:
    """Write a tiny DuckDB holding sim.pitch_pool, sim.outcome_pool and the map.

    The pitch pool always holds 2024, 2025 and 2026. ``outcome_seasons`` chooses
    which of those the outcome pool holds, so a caller can build the real gap or
    a complete pool with one argument.
    """
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(path)
    try:
        con.execute("CREATE SCHEMA sim; CREATE SCHEMA derived;")
        for table in ("pitch_pool", "outcome_pool"):
            con.execute(
                f"CREATE TABLE sim.{table} (game_pk INTEGER, at_bat_number INTEGER, "
                "season INTEGER, stand VARCHAR, runners_state INTEGER, outs INTEGER, "
                "count_balls INTEGER, count_strikes INTEGER, score_diff INTEGER, "
                "exit_velo DOUBLE, launch_angle DOUBLE, pull_relative_spray_angle DOUBLE)"
            )
        con.execute(
            "CREATE TABLE derived.at_bat_situations (game_pk INTEGER, at_bat_number INTEGER, "
            "top_or_bottom INTEGER)"
        )
        pitch_rows = []
        outcome_rows = []
        situ_rows = []
        pk = 0
        for season in (2024, 2025, 2026):
            for rs, outs, balls, strikes, sd, home in itertools.islice(
                (
                    (rs, o, b, s, sd, hm)
                    for rs in range(8)
                    for o in range(3)
                    for b in range(4)
                    for s in range(3)
                    for sd in (-5, -2, 0, 2, 5)
                    for hm in range(2)
                ),
                None,
            ):
                pk += 1
                stand = "L" if pk % 2 else "R"
                row = (
                    pk,
                    1,
                    season,
                    stand,
                    rs,
                    outs,
                    balls,
                    strikes,
                    sd,
                    92.0,
                    14.0,
                    3.0,
                )
                pitch_rows.append(row)
                situ_rows.append((pk, 1, home))
                if season in outcome_seasons:
                    outcome_rows.append(row)
        con.executemany("INSERT INTO sim.pitch_pool VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", pitch_rows)
        if outcome_rows:
            con.executemany(
                "INSERT INTO sim.outcome_pool VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", outcome_rows
            )
        con.executemany("INSERT INTO derived.at_bat_situations VALUES (?,?,?)", situ_rows)
    finally:
        con.close()


def _run_script(duckdb_path: str, output: str) -> subprocess.CompletedProcess[str]:
    """Run measure_filter_cells.py as a real process and return the result.

    A subprocess is the only honest way to read an exit code. It also proves the
    module is importable on its own.
    """
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            _MODULE_PATH,
            "--duckdb",
            duckdb_path,
            "--half-inning-source",
            "duckdb",
            "--recent-seasons",
            "3",
            "--min-cells",
            "20",
            "--output",
            output,
        ],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env={**os.environ, "BASEBALL_DB_DSN": ""},
        timeout=600,
    )


@pytest.mark.slow
def test_run_returns_non_zero_when_a_season_is_missing(tmp_path) -> None:
    """The reproduction of the committed false green, end to end.

    The outcome pool holds 2024 and 2025; the pitch pool also holds 2026. The
    committed 2026-08-10 report called that ``coverage_ok: true`` and exited 0.
    The script must now write the JSON, record the gap, and exit 2.
    """
    pytest.importorskip("duckdb")
    db = str(tmp_path / "gap.duckdb")
    out = str(tmp_path / "gap.json")
    _build_fake_pools(db, outcome_seasons=(2024, 2025))

    result = _run_script(db, out)
    assert result.returncode == mfc.EXIT_UNHEALTHY == 2, result.stdout + result.stderr

    report = json.loads(open(out, encoding="utf-8").read())
    assert report["coverage_ok"] is False
    assert report["health"]["missing_seasons"] == {"outcome_pool": [2026]}
    gaps = report["health"]["configuration_season_gaps"]
    assert "outcome_pool|recent|ALL" in gaps
    assert gaps["outcome_pool|recent|ALL"]["seasons_missing"] == [2026]
    # The label no longer lies: the configuration says which seasons it measured.
    cfg = report["configurations"]["outcome_pool|recent|ALL"]
    assert cfg["seasons"] == [2024, 2025, 2026]
    assert cfg["seasons_measured"] == [2024, 2025]
    # The widening evidence is present, which it was not in the first report.
    assert cfg["widening"]["decided_order"] == ["score_band", "home_or_away", "count"]
    assert [s["drops"] for s in cfg["widening"]["ladder"]] == [
        [],
        ["score_band"],
        ["score_band", "home_or_away"],
        ["score_band", "home_or_away", "count"],
    ]


@pytest.mark.slow
def test_run_returns_zero_when_every_season_is_present(tmp_path) -> None:
    """The control. Without the gap the same script exits 0.

    Without this case a script that always exited 2 would pass the test above
    while measuring nothing.
    """
    pytest.importorskip("duckdb")
    db = str(tmp_path / "full.duckdb")
    out = str(tmp_path / "full.json")
    _build_fake_pools(db, outcome_seasons=(2024, 2025, 2026))

    result = _run_script(db, out)
    assert result.returncode == 0, result.stdout + result.stderr

    report = json.loads(open(out, encoding="utf-8").read())
    assert report["coverage_ok"] is True
    assert report["health"]["problems"] == []
    assert report["configurations"]["outcome_pool|recent|ALL"]["seasons_missing"] == []


# ---------------------------------------------------------------------------
# The lazy-import claim, checked where it can actually fail
# ---------------------------------------------------------------------------


def test_duckdb_is_absent_from_a_clean_import() -> None:
    """Importing this script must not import ``duckdb``.

    The old guard asserted ``not hasattr(mfc, "duckdb")``. That is true whenever
    ``duckdb`` IS installed, because the script never binds the name itself — so
    the guard could not fail. Meanwhile the module-scope
    ``from pipeline.batch.engine_artifacts import ...`` pulled ``duckdb`` in
    transitively (``engine_artifacts.py:38`` imports it at module scope), and the
    docstring's promise was false.

    This checks ``sys.modules`` in a FRESH subprocess, which is the only place
    the answer is not polluted by whatever another test imported first.
    """
    probe = textwrap.dedent(
        f"""
        import importlib.util, sys
        assert "duckdb" not in sys.modules, "duckdb was preloaded before the probe"
        spec = importlib.util.spec_from_file_location("mfc", {_MODULE_PATH!r})
        module = importlib.util.module_from_spec(spec)
        sys.modules["mfc"] = module
        spec.loader.exec_module(module)
        print("LOADED_DUCKDB" if "duckdb" in sys.modules else "NO_DUCKDB")
        print("LOADED_ENGINE_ARTIFACTS"
              if "pipeline.batch.engine_artifacts" in sys.modules else "NO_ENGINE_ARTIFACTS")
        """
    )
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "NO_DUCKDB" in result.stdout, f"a bare import pulled duckdb in: {result.stdout}"
    assert "NO_ENGINE_ARTIFACTS" in result.stdout, result.stdout
    assert not hasattr(mfc, "duckdb")
    assert mfc.SCRIPT_VERSION.startswith("SIM-451")


def test_recency_floor_is_read_from_the_artifact_builder() -> None:
    """The lazy wrapper returns the builder's own constant, never a copy."""
    from pipeline.batch import engine_artifacts

    assert mfc.recency_floor_seasons() == engine_artifacts.RECENCY_FLOOR_SEASONS
