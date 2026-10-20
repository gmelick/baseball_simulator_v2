"""Step 3, the checks a to h inside the rebuild's transaction, and the rollback."""

from __future__ import annotations

from typing import Any

from pipeline.batch.artifacts.pools import STEAL_PITCH_CLASSES

from .common import (
    DEAD_BALL_CLASSES,
    EXPECT,
    LABELS,
    MIGRATION,
    NEW_COLUMNS,
    POOL,
    SNAP,
    _builder,
    _log,
    _ok,
    _season_list,
)
from .migration import missing_columns
from .pickoffs import PickoffCount
from .preflight import _steal_counts, _steal_versions

# ---------------------------------------------------------------------------
# The checks (inside the rebuild's transaction)
# ---------------------------------------------------------------------------


def _examples(con: Any, sql: str) -> str:
    rows = con.execute(sql + " LIMIT 5").fetchall()
    return f" e.g. {rows}" if rows else ""


def check_snapshot_back(con: Any) -> bool:
    """Check a: every snapshot PITCH row is back with the same six values.

    A pickoff row of an earlier run is keyed by ``-raw.play_events.id``, and a
    game reload gives its play records new ids. So the earlier run's pickoff
    rows are reported for information only; check c proves every pickoff row
    of this build against today's play records, and check b proves every new
    row is a pickoff row."""
    diffs = " OR ".join(f"COALESCE(n.{c}, FALSE) IS DISTINCT FROM b.{c}" for c in LABELS)
    # The brackets hold the OR chain together: the example query below adds
    # "AND b.pitch_id > 0", and AND binds before OR.
    base = (
        f"FROM {SNAP} b LEFT JOIN {POOL} n ON n.pitch_id = b.pitch_id "
        f"WHERE (n.pitch_id IS NULL OR {diffs})"
    )
    bad_pitch, bad_pickoff = con.execute(
        "SELECT COUNT(*) FILTER (WHERE b.pitch_id > 0), COUNT(*) FILTER (WHERE b.pitch_id < 0) "
        + base
    ).fetchone()
    total = int(con.execute(f"SELECT COUNT(*) FROM {SNAP} WHERE pitch_id > 0").fetchone()[0])
    good = int(bad_pitch) == 0
    _log(
        f"  a. snapshot pitch rows back with the same six values: {total - bad_pitch:,} "
        f"of {total:,} (missing or changed: {bad_pitch:,})  {_ok(good)}"
        + ("" if good else _examples(con, "SELECT b.pitch_id " + base + " AND b.pitch_id > 0"))
    )
    if int(bad_pickoff):
        _log(
            f"     information: {int(bad_pickoff):,} pickoff rows of an earlier run are not back "
            "under the same id (a reloaded game renumbers its play records); check c proves "
            "this build's pickoff rows"
        )
    return good


def check_new_rows(con: Any, seasons: list[int]) -> bool:
    """Check b: every row the snapshot does not hold is a pickoff row."""
    base = (
        f"FROM {POOL} n LEFT JOIN {SNAP} b ON b.pitch_id = n.pitch_id "
        f"WHERE n.season IN ({_season_list(seasons)}) AND b.pitch_id IS NULL"
    )
    new, bad = con.execute(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE NOT (n.pitch_id < 0 "
        "AND COALESCE(n.is_pickoff_row, FALSE))) " + base
    ).fetchone()
    good = int(bad) == 0
    _log(
        f"  b. new rows {int(new):,}; of those not a pickoff row: {int(bad):,}  {_ok(good)}"
        + (
            ""
            if good
            else _examples(
                con,
                "SELECT n.pitch_id, n.season, n.is_pickoff_row "
                + base
                + " AND NOT (n.pitch_id < 0 AND COALESCE(n.is_pickoff_row, FALSE))",
            )
        )
    )
    return good


#: Check c's comparison of one pickoff row with its outcome (``s`` the row,
#: ``e`` the outcome).
_PICKOFF_ROW_DIFFS = " OR ".join(
    (
        "s.game_pk IS DISTINCT FROM e.game_pk",
        "s.at_bat_number IS DISTINCT FROM e.at_bat_number",
        "s.season IS DISTINCT FROM e.season",
        "s.game_date IS DISTINCT FROM e.game_date",
        "s.runner_id IS DISTINCT FROM e.runner_id",
        "s.pitcher_id IS DISTINCT FROM e.pitcher_id",
        "s.target_base IS DISTINCT FROM e.pair",
        "s.outs IS DISTINCT FROM e.outs_before",
        "s.count_balls IS DISTINCT FROM 0",
        "s.count_strikes IS DISTINCT FROM 0",
        "s.score_diff IS DISTINCT FROM GREATEST(-5, LEAST(5, e.bat_score - e.fld_score))",
        "COALESCE(s.attempted, FALSE)",
        "COALESCE(s.success, FALSE)",
        "COALESCE(s.pickoff_out, FALSE) IS DISTINCT FROM e.is_out",
        "COALESCE(s.pickoff_advancing, FALSE) IS DISTINCT FROM e.is_adv",
        "COALESCE(s.pickoff_error, FALSE) IS DISTINCT FROM (e.is_err AND NOT e.is_out)",
        "s.pitch_class IS NOT NULL",
        "NOT COALESCE(s.is_pickoff_row, FALSE)",
    )
)


def check_pickoff_rows(con: Any, seasons: list[int], counts: dict[int, PickoffCount]) -> bool:
    """Check c: the pickoff rows are exactly the counted outcomes, one row
    each, and each row carries its outcome's facts."""
    sl = _season_list(seasons)
    joined = (
        f"FROM (SELECT * FROM {EXPECT} WHERE NOT has_pitch AND has_ids) e "
        f"FULL OUTER JOIN (SELECT * FROM {POOL} WHERE season IN ({sl}) AND pitch_id < 0) s "
        "ON s.pitch_id = -e.id"
    )
    missing, extra, differ = con.execute(
        "SELECT COUNT(*) FILTER (WHERE s.pitch_id IS NULL), "
        "COUNT(*) FILTER (WHERE e.id IS NULL), "
        f"COUNT(*) FILTER (WHERE s.pitch_id IS NOT NULL AND e.id IS NOT NULL "
        f"AND ({_PICKOFF_ROW_DIFFS})) " + joined
    ).fetchone()
    built = {
        int(s): int(n)
        for s, n in con.execute(
            f"SELECT season, COUNT(*) FROM {POOL} WHERE season IN ({sl}) AND pitch_id < 0 "
            "GROUP BY 1"
        ).fetchall()
    }
    ok = int(missing) == 0 and int(extra) == 0 and int(differ) == 0
    for s in seasons:
        want = counts.get(int(s), PickoffCount()).rows
        good = built.get(int(s), 0) == want
        ok = ok and good
        _log(f"  c. {s}: pickoff rows {built.get(int(s), 0):,}; counted {want:,}  {_ok(good)}")
    _log(
        f"  c. counted outcomes with no row {int(missing):,}; rows with no counted outcome "
        f"{int(extra):,}; rows whose runner, thrower, pair, outs, count or labels differ "
        f"{int(differ):,}  {_ok(ok)}"
    )
    if int(missing):
        _log(
            "     "
            + _examples(
                con,
                "SELECT e.id, e.game_pk, e.at_bat_number " + joined + " WHERE s.pitch_id IS NULL",
            )
        )
    if int(extra):
        _log(
            "     "
            + _examples(
                con,
                "SELECT s.pitch_id, s.game_pk, s.at_bat_number " + joined + " WHERE e.id IS NULL",
            )
        )
    if int(differ):
        _log(
            "     "
            + _examples(
                con,
                "SELECT e.id " + joined + f" WHERE s.pitch_id IS NOT NULL AND e.id IS NOT NULL "
                f"AND ({_PICKOFF_ROW_DIFFS})",
            )
        )
    return ok


def check_identity(con: Any, seasons: list[int], counts: dict[int, PickoffCount]) -> bool:
    """Check d: tagged rows + pickoff rows equal the outcomes that fit a
    pair, less the listed ones (no runner or pitcher id; a second outcome
    folded into one tag)."""
    sl = _season_list(seasons)
    tagged = {
        int(s): int(n)
        for s, n in con.execute(
            f"SELECT season, COUNT(*) FROM {POOL} WHERE season IN ({sl}) AND pitch_id > 0 "
            "AND (COALESCE(pickoff_out, FALSE) OR COALESCE(pickoff_error, FALSE)) GROUP BY 1"
        ).fetchall()
    }
    built = {
        int(s): int(n)
        for s, n in con.execute(
            f"SELECT season, COUNT(*) FROM {POOL} WHERE season IN ({sl}) AND pitch_id < 0 "
            "GROUP BY 1"
        ).fetchall()
    }
    ok = True
    for s in seasons:
        c = counts.get(int(s), PickoffCount())
        t, p = tagged.get(int(s), 0), built.get(int(s), 0)
        good = t == c.tag_groups and t + p + c.folded + len(c.no_id) == c.fit
        ok = ok and good
        _log(
            f"  d. {s}: tagged rows {t:,} + pickoff rows {p:,} = {t + p:,}; outcomes that fit a "
            f"pair {c.fit:,}, less {c.folded:,} folded and {len(c.no_id):,} listed = "
            f"{c.fit - c.folded - len(c.no_id):,}  {_ok(good)}"
        )
    return ok


def check_classes(con: Any, seasons: list[int]) -> bool:
    """Check e: the class is set on every pitch row and NULL on every pickoff
    row; it equals the pitch pool's class for the same pitch, so the shares
    per (target, outs, balls, strikes) equal the pitch pool's."""
    sl = _season_list(seasons)
    classes = "(" + ", ".join(f"'{c}'" for c in STEAL_PITCH_CLASSES) + ")"
    pitch_null, pitch_marked, odd, po_class, po_unmarked = con.execute(
        f"""
        SELECT COUNT(*) FILTER (WHERE pitch_id > 0 AND pitch_class IS NULL),
               COUNT(*) FILTER (WHERE pitch_id > 0 AND COALESCE(is_pickoff_row, FALSE)),
               COUNT(*) FILTER (WHERE pitch_class IS NOT NULL AND pitch_class NOT IN {classes}),
               COUNT(*) FILTER (WHERE pitch_id < 0 AND pitch_class IS NOT NULL),
               COUNT(*) FILTER (WHERE pitch_id < 0 AND NOT COALESCE(is_pickoff_row, FALSE))
        FROM {POOL} WHERE season IN ({sl})
        """
    ).fetchone()
    joined = (
        f"FROM {POOL} s LEFT JOIN sim.pitch_pool pp ON pp.pitch_id = s.pitch_id "
        f"WHERE s.season IN ({sl}) AND s.pitch_id > 0"
    )
    no_pitch, differ = con.execute(
        "SELECT COUNT(*) FILTER (WHERE pp.pitch_id IS NULL), "
        "COUNT(*) FILTER (WHERE s.pitch_class IS DISTINCT FROM pp.outcome_type) " + joined
    ).fetchone()
    worst = con.execute(
        f"""
        WITH j AS (
            SELECT s.target_base AS t, s.outs AS o, s.count_balls AS b, s.count_strikes AS k,
                   COALESCE(s.pitch_class, '<none>') AS ours,
                   COALESCE(pp.outcome_type, '<none>') AS theirs
            {joined}
        ),
        cell AS (SELECT t, o, b, k, COUNT(*) AS n FROM j GROUP BY ALL),
        a AS (SELECT t, o, b, k, ours AS cls, COUNT(*) AS n FROM j GROUP BY ALL),
        p AS (SELECT t, o, b, k, theirs AS cls, COUNT(*) AS n FROM j GROUP BY ALL)
        SELECT COALESCE(MAX(ABS(COALESCE(a.n, 0) - COALESCE(p.n, 0)) / cell.n), 0)
        FROM a FULL OUTER JOIN p USING (t, o, b, k, cls) JOIN cell USING (t, o, b, k)
        """
    ).fetchone()[0]
    good = (
        int(pitch_null) == 0
        and int(pitch_marked) == 0
        and int(odd) == 0
        and int(po_class) == 0
        and int(po_unmarked) == 0
        and int(no_pitch) == 0
        and int(differ) == 0
        and float(worst) == 0.0
    )
    _log(
        f"  e. pitch rows with no class {int(pitch_null):,}, marked as pickoff rows "
        f"{int(pitch_marked):,}; classes outside the six {int(odd):,}; pickoff rows with a class "
        f"{int(po_class):,}, unmarked {int(po_unmarked):,}  {_ok(good)}"
    )
    _log(
        f"  e. pitch rows with no pitch-pool row {int(no_pitch):,}; with a class other than the "
        f"pitch pool's {int(differ):,}; the largest class-share gap in a (target, outs, balls, "
        f"strikes) cell {float(worst):.6f}  {_ok(good)}"
    )
    for t, cls, n_att in con.execute(
        f"SELECT target_base, pitch_class, COUNT(*) FROM {POOL} WHERE season IN ({sl}) "
        "AND pitch_id > 0 AND attempted GROUP BY 1, 2 ORDER BY 1, 3 DESC"
    ).fetchall():
        _log(f"     attempts on target {t}: {cls} {int(n_att):,}")
    return good


def check_dead_ball_attempts(con: Any, seasons: list[int]) -> bool:
    """Check f: no attempted row rides a ball in play or a hit by pitch."""
    dead = "(" + ", ".join(f"'{c}'" for c in DEAD_BALL_CLASSES) + ")"
    n = int(
        con.execute(
            f"SELECT COUNT(*) FROM {POOL} WHERE season IN ({_season_list(seasons)}) "
            f"AND COALESCE(attempted, FALSE) AND pitch_class IN {dead}"
        ).fetchone()[0]
    )
    good = n == 0
    _log(f"  f. attempted rows on a ball in play or a hit by pitch: {n:,}  {_ok(good)}")
    return good


def check_pickoff_row_ids(con: Any, seasons: list[int]) -> bool:
    """Check g: every pickoff row has a game date, a runner and a pitcher."""
    n = int(
        con.execute(
            f"SELECT COUNT(*) FROM {POOL} WHERE season IN ({_season_list(seasons)}) "
            "AND pitch_id < 0 AND (game_date IS NULL OR runner_id IS NULL OR pitcher_id IS NULL)"
        ).fetchone()[0]
    )
    no_catcher = int(
        con.execute(
            f"SELECT COUNT(*) FROM {POOL} WHERE season IN ({_season_list(seasons)}) "
            "AND pitch_id < 0 AND catcher_id IS NULL"
        ).fetchone()[0]
    )
    good = n == 0
    _log(
        f"  g. pickoff rows with no game date, runner or pitcher: {n:,}  {_ok(good)} "
        f"(with no catcher, a neutral weight: {no_catcher:,})"
    )
    return good


def check_metadata(con: Any, seasons: list[int]) -> bool:
    """Check h: the build metadata names the builder for every season, with
    the table's row count."""
    after = _steal_counts(con, seasons)
    meta = {
        int(s): (v, int(n))
        for s, v, n in con.execute(
            "SELECT season, builder_version, row_count FROM sim.pool_build_metadata "
            f"WHERE pool_name = 'steal_opportunity_pool' AND season IN ({_season_list(seasons)})"
        ).fetchall()
    }
    ok = True
    for s in seasons:
        version, n = meta.get(int(s), (None, -1))
        good = version == _builder() and n == after.get(int(s), 0)
        ok = ok and good
        _log(f"  h. {s}: builder {version}  row_count {n:,}  {_ok(good)}")
    return ok


def matches_snapshot(
    con: Any, seasons: list[int], before: dict[int, int], versions: dict[int, str | None]
) -> bool:
    """TRUE when the pool holds exactly the snapshot's rows, labels and builders."""
    if _steal_counts(con, seasons) != before or _steal_versions(con, seasons) != versions:
        return False
    diffs = " OR ".join(f"COALESCE(n.{c}, FALSE) IS DISTINCT FROM b.{c}" for c in LABELS)
    differ = con.execute(
        f"SELECT COUNT(*) FROM {SNAP} b LEFT JOIN {POOL} n ON n.pitch_id = b.pitch_id "
        f"WHERE (n.pitch_id IS NULL OR {diffs})"
    ).fetchone()[0]
    return int(differ) == 0


def _log_migration_rolled_back(con: Any, migrated_here: bool) -> None:
    """After a rollback (exit 1 or 2), say what became of migration 0031's columns.

    When this run applied 0031 inside the transaction, the columns go with the
    rows. When they were on the table before this run (an earlier run
    committed them, or someone applied 0031 by hand), the rollback cannot
    remove them, and a pool builder older than sim554.1 fails on them.
    """
    left = [c for c in NEW_COLUMNS if c not in missing_columns(con)]
    if not migrated_here:
        if left:
            _log(
                f"  WARNING: the 0031 columns {left} were on the table before this run, so the "
                "rollback keeps them. Master's pool builder fails on this table after it "
                "deletes the season: merge the SIM-554 code, or hold the nightly and every "
                "`make profile-computor`, or drop the two columns"
            )
        return
    if left:
        _log(
            f"  WARNING: the 0031 columns {left} stayed after the rollback. Master's pool "
            "builder fails on this table after it deletes the season: merge the SIM-554 code, "
            "or hold the nightly and every `make profile-computor`, or drop the two columns"
        )
    else:
        _log(f"  {MIGRATION.name} rolled back with the rows: the table has its old shape")


def rollback(
    con: Any, seasons: list[int], before: dict[int, int], versions: dict[int, str | None]
) -> bool:
    """Roll the rebuild's transaction back and prove the table is as it was."""
    try:
        con.execute("ROLLBACK")
    except Exception as exc:  # noqa: BLE001 - report, then verify what the table holds
        _log(f"  ROLLBACK raised {type(exc).__name__}: {exc}")
    restored = matches_snapshot(con, seasons, before, versions)
    _log(
        "  the transaction is rolled back: sim.steal_opportunity_pool "
        + (
            "holds the snapshot's rows, labels and builders"
            if restored
            else "DOES NOT match the snapshot. Read it before the next pool export"
        )
    )
    return restored
