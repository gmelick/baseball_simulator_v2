"""SIM-553 — the standing label check: the pitch pool's count chain against real play.

WHAT THIS TEST GUARDS
=====================
The pool bands (``tests/acceptance/bands.py`` ``POOL_REFERENCES``) grade the
simulator against the count chain of the pool's OWN labels: the per-plate-
appearance rates a faithful sampler of the pool's per-count class shares
produces. A labelling defect in the pool build moves that centre with the
simulator, so the band reads green on it. Two defects in the pool build's class
expression hid that way:

  * SIM-509 (2026-08-18): hit-by-pitch pitches coded as balls.
  * SIM-553 (2026-09-23): a two-strike foul tip or foul bunt coded as a foul.
    The count machine pitched on after a real strikeout. On 2023-2026 the
    label check's chain (the pool rows of real plate appearances) read
    strikeouts 4.29% below real play; the chain over every pool row read
    4.08% below.

This test counts the same four rates (strikeouts, walks, hit by pitch, pitches
per plate appearance) from REAL plate appearances in ``raw.pitches`` and fails
when the chain drifts more than 0.5% from any of them
(``pipeline.batch.pool_chain.LABEL_CHECK_TOLERANCE``, owner decision 5 of the
SIM-553 plan). The real count shares no label with the pool.

The two sides count the same plate appearances. The chain reads only the pool
rows of groups that end in a batting event (``chain_rates(..., pa_only=True)``),
judged from the pool's own ``events`` column by the expression
``real_pa_rates`` uses. A separate test proves the two populations match: the
pool's plate appearances (``pool_chain.pa_group_count``) must equal the real
count (708,439 on 2023-2026). A difference means the two sides no longer read
the same plate appearances, for example because ``raw.pitches`` gained games
the pool has not been rebuilt for. The all-rows chain also plays on the groups
cut short by a runner out, which adds about +0.6 points to its walk gap; the
report prints that gap for the record only (``pipeline/batch/pool_chain.py``,
THE POPULATION RULE). Measured 2026-09-25 on 2023-2026 (strikeouts / walks /
hit by pitch / pitches): the corrected coding reads +0.09% / -0.25% / +0.08% /
+0.07%; the SIM-553 coding reads -4.29% / +2.31% / +1.12% / +0.90% and fails.
The walk gap's -0.25% is the pitch clock's automatic balls, which have no pitch
row (-0.41% in 2023, fading to -0.16% in 2026). It is not a label error.

The check cannot see a swap between a called and a swinging strike, or between
in-play sub-types: the count machine treats each group alike. The unit truth
tables (``tests/unit/test_sim553_foul_tip_strike_three.py``) guard those.

THE WINDOW
==========
The test reads its window from the live pool: the seasons the engine-artifact
bundle exports (``pipeline.batch.engine_artifacts.last_n_seasons``, the latest
``RECENCY_FLOOR_SEASONS`` seasons of ``sim.pitch_pool``). When a new season's
rows arrive, the check grades the new window at once, so a coding change that
reaches only the new season's rows is graded too.

A guard test ties that window to the one the band centres were measured on
(``bands.POOL_WINDOW``, today "full seasons 2023-2026"). When the window rolls
and nobody restates the centres, the guard FAILS and names both windows. The
fix is to re-run the census on the new window and restate the centres and
``POOL_WINDOW`` in ``tests/acceptance/bands.py``, never to pin this test's
window.

WHEN IT RUNS
============
It needs the live sim DuckDB (opened READ-ONLY, which works while the app runs)
and Postgres, and it takes a few seconds. Nothing runs it on a schedule. It
carries the ``acceptance`` marker, so two hand-started lanes collect it:

  * the acceptance lane, ``pytest tests/acceptance`` in the app container with
    ``-v "$PWD/tests:/app/tests"`` (the SIM-450 lane command);
  * the manual acceptance workflow (``.github/workflows/acceptance-nightly.yml``,
    dispatch only), which selects ``-m acceptance``.

CI never collects it: the unit lane runs ``tests/unit`` and the
acceptance-arithmetic job names one other file. ``make test`` sees this file
only after ``docker compose build app``, because the dev image bakes ``tests/``
in (the Dockerfile's dev stage) and the app service does not mount it.

To run it alone, from the repo root:

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -e SIM_ACCEPTANCE=1 -v "$PWD/tests:/app/tests" -v "$PWD/pipeline:/app/pipeline" app python -m pytest tests/acceptance/test_sim553_pool_label_check.py -p no:cacheprovider -rs

Where either store is unreachable (the host, CI) the live tests SKIP. Under
``SIM_ACCEPTANCE=1`` they FAIL instead, the package's rule: an explicit request
for the lane never turns into a silent skip. The same comparison is check d of
the pool rebuild (``scripts/sim553_rebuild_pitch_pool.py``) and the ``--strict``
exit of ``scripts/pool_window_census.py --windows W1 --strict``.

A FAIL MEANS one of three things. Read which test failed:

  * the channels: the pool build codes a pitch into the wrong class, or the
    pool predates the fix. A strikeout shortfall with a walk and pitch surplus
    is the SIM-553 shape. The fix is in ``SQL_OUTCOME_TYPE``
    (``pipeline/batch/player_profile_computor.py``) and a pool rebuild
    (``scripts/sim553_rebuild_pitch_pool.py``), never in this tolerance;
  * the plate-appearance counts: the pool and ``raw.pitches`` hold different
    games. Rebuild the pool for the window before you read the channels;
  * the window guard: the window rolled and the band centres did not.
"""

from __future__ import annotations

import os
import re
from typing import Any, NoReturn

import pytest

from pipeline.batch import pool_chain
from pipeline.batch.engine_artifacts import RECENCY_FLOOR_SEASONS, last_n_seasons
from tests.acceptance import bands
from tests.acceptance.conftest import opted_in

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(300)]


def _duckdb_path() -> str:
    return os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")


def _unavailable(reason: str) -> NoReturn:
    """Skip, or fail under ``SIM_ACCEPTANCE=1`` (the package's loud-precondition rule)."""
    if opted_in():
        pytest.fail(f"SIM-553 label check cannot run: {reason}")
    pytest.skip(f"SIM-553 label check needs the live stores: {reason}")


def _bands_seasons() -> list[int]:
    """The seasons ``bands.POOL_WINDOW`` states: its first two years, as a closed range."""
    years = [int(y) for y in re.findall(r"\b(20\d\d)\b", bands.POOL_WINDOW)[:2]]
    assert len(years) == 2, (
        f"bands.POOL_WINDOW reads {bands.POOL_WINDOW!r}; this test expects "
        "'full seasons <first>-<last>'"
    )
    return list(range(years[0], years[1] + 1))


def _season_pred(seasons: list[int]) -> str:
    """A predicate on ``season``, a column both ``sim.pitch_pool`` and ``raw.pitches`` carry."""
    return f"season IN ({', '.join(str(int(s)) for s in seasons)})"


@pytest.fixture(scope="module")
def live_con() -> Any:
    """The sim DuckDB, read-only, with Postgres attached as ``pg``; else skip."""
    path = _duckdb_path()
    if not os.path.isfile(path):
        _unavailable(f"no sim DuckDB at {path}")
    try:
        import duckdb

        con = duckdb.connect(path, read_only=True)
    except Exception as exc:  # noqa: BLE001 - a missing module or a writer's lock
        _unavailable(f"cannot open {path} read-only ({type(exc).__name__}: {exc})")
    try:
        has_pool = con.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'sim' AND table_name = 'pitch_pool'"
        ).fetchone()[0]
        if not has_pool:
            _unavailable(f"{path} holds no sim.pitch_pool")
        try:
            pool_chain.attach_pg(con)
        except RuntimeError as exc:
            _unavailable(str(exc))
        yield con
    finally:
        con.close()


@pytest.fixture(scope="module")
def window(live_con: Any) -> list[int]:
    """The seasons the bundle exports from the live pool (``last_n_seasons``)."""
    seasons = last_n_seasons(live_con)
    if not seasons:
        _unavailable("sim.pitch_pool holds no season")
    return seasons


@pytest.fixture(scope="module")
def real(live_con: Any, window: list[int]) -> dict[str, float]:
    """The real plate-appearance rates over the live window."""
    return pool_chain.real_pa_rates(live_con, _season_pred(window))


def test_the_bands_window_spans_the_export_floor() -> None:
    """``bands.POOL_WINDOW`` names as many seasons as the bundle exports."""
    seasons = _bands_seasons()
    assert len(seasons) == RECENCY_FLOOR_SEASONS, (
        f"bands.POOL_WINDOW reads {bands.POOL_WINDOW!r} ({len(seasons)} seasons), but the "
        f"bundle exports RECENCY_FLOOR_SEASONS = {RECENCY_FLOOR_SEASONS} "
        "(pipeline/batch/engine_artifacts.py); restate one of them"
    )


def test_the_live_window_is_the_bands_window(window: list[int]) -> None:
    """The window the label check grades is the window the band centres were measured on."""
    stated = _bands_seasons()
    assert window == stated, (
        f"the live pool's window is {window[0]}-{window[-1]} {window} "
        f"(engine_artifacts.last_n_seasons, RECENCY_FLOOR_SEASONS = {RECENCY_FLOOR_SEASONS}), "
        f"but bands.POOL_WINDOW reads {bands.POOL_WINDOW!r} {stated}. The window rolled "
        "and the band centres did not: re-run scripts/pool_window_census.py on the new "
        "window and restate the centres and POOL_WINDOW in tests/acceptance/bands.py."
    )


def test_both_sides_read_the_same_plate_appearances(
    live_con: Any, window: list[int], real: dict[str, float]
) -> None:
    """The pool's plate appearances (the ``pa_only`` groups) equal the real count."""
    n_pool = pool_chain.pa_group_count(live_con, _season_pred(window))
    n_real = int(real["n_pa"])
    line = pool_chain.format_pa_count(n_pool, n_real)
    print(f"window {_season_pred(window)}:\n{line}")
    assert n_pool == n_real, (
        f"window {_season_pred(window)}:\n{line}\n"
        "The label check would compare different plate appearances. Rebuild the pool "
        "for the window (scripts/sim553_rebuild_pitch_pool.py or the nightly "
        "player_profile_computor run) before you read the channel gaps."
    )


def test_the_pool_chain_matches_real_plate_appearances(
    live_con: Any, window: list[int], real: dict[str, float]
) -> None:
    pred = _season_pred(window)
    # SIM-553: the chain over the pool rows of real plate appearances only, so
    # both sides count the same groups (pool_chain, THE POPULATION RULE).
    chain = pool_chain.chain_rates(live_con, pred, pa_only=True)
    rows = pool_chain.label_check(chain, real)
    failed = [row[0] for row in rows if not row[4]]
    n_pool = pool_chain.pa_group_count(live_con, pred)

    # For the record only: the all-rows chain (the band centres) against the
    # same plate appearances. Its gap includes the groups cut short.
    all_rows = pool_chain.label_check(pool_chain.chain_rates(live_con, pred), real)
    versions = live_con.execute(
        "SELECT DISTINCT builder_version FROM sim.pool_build_metadata "
        f"WHERE pool_name = 'pitch_pool' AND {pred} ORDER BY 1"
    ).fetchall()
    report = "\n".join(
        [
            f"window {pred}; {int(real['n_pa']):,} real plate appearances; "
            f"pitch pool built by {[v[0] for v in versions]}; tolerance "
            f"{pool_chain.LABEL_CHECK_TOLERANCE * 100.0:.1f}%; the chain over the pool "
            "rows of those plate appearances:",
            pool_chain.format_pa_count(n_pool, int(real["n_pa"])),
            *pool_chain.format_label_check(rows),
            "for the record, the all-rows chain against the same plate appearances: "
            + pool_chain.format_gaps(all_rows),
        ]
    )
    print(report)
    assert not failed, (
        f"the pitch pool's count chain drifts from real plate appearances on {failed}:\n"
        f"{report}\n"
        "The pool build codes a pitch into the wrong class (see SQL_OUTCOME_TYPE in "
        "pipeline/batch/player_profile_computor.py), or the pool predates the fix "
        "(rebuild: scripts/sim553_rebuild_pitch_pool.py)."
    )
