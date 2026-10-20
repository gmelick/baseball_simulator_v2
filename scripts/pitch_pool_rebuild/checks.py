"""Step 3, the checks a to e inside the rebuild's transaction, the rollback and
the restore."""

from __future__ import annotations

from typing import Any

from pipeline.batch.player_profile_computor import POOL_BUILDER_VERSION, STRIKE_THREE_FOUL_TYPES
from pipeline.batch.pool_chain import (
    LABEL_CHECK_TOLERANCE,
    OUTCOMES,
    chain_rates,
    format_label_check,
    format_pa_count,
    label_check,
    pa_group_count,
    real_pa_rates,
)

from .common import (
    ALL_SEASONS_EXPECTED_MOVES,
    MOVE_CODES,
    WINDOW_EXPECTED_MOVES,
    _builder_versions,
    _log,
    _pool_counts,
    _raw_code_rows_sql,
    _season_list,
    _sql_in,
)

# ---------------------------------------------------------------------------
# The checks (inside the rebuild's transaction)
# ---------------------------------------------------------------------------


def _check_rows(con: Any, seasons: list[int], before: dict[int, int]) -> bool:
    """Check a: the rows per season equal the snapshot's; every row keeps its
    pitch_id; no class is NULL or outside the chain's six."""
    sl = _season_list(seasons)
    after = _pool_counts(con, seasons)
    ok = True
    for s in seasons:
        same = before.get(s) == after.get(s)
        ok = ok and same
        _log(
            f"  a. {s}: rows before {before.get(s, 0):,}  after {after.get(s, 0):,}  "
            f"{'OK' if same else 'MISMATCH'}"
        )
    matched = con.execute(
        "SELECT COUNT(*) FROM _sim553_before b "
        "JOIN sim.pitch_pool n ON n.pitch_id = b.pitch_id AND n.season = b.season"
    ).fetchone()[0]
    total = sum(before.values())
    good = int(matched) == total
    ok = ok and good
    _log(
        f"  a. rows matched to the snapshot by pitch_id: {matched:,} of {total:,} {'OK' if good else 'MISMATCH'}"
    )
    nulls = con.execute(
        f"SELECT COUNT(*) FROM sim.pitch_pool WHERE season IN ({sl}) AND outcome_type IS NULL"
    ).fetchone()[0]
    odd = con.execute(
        f"SELECT outcome_type, COUNT(*) FROM sim.pitch_pool WHERE season IN ({sl}) "
        f"AND outcome_type NOT IN {_sql_in(OUTCOMES)} GROUP BY 1"
    ).fetchall()
    good = nulls == 0 and not odd
    ok = ok and good
    _log(
        f"  a. NULL classes {nulls}; classes outside {OUTCOMES}: {odd or 'none'} "
        f"{'OK' if good else 'MISMATCH'}"
    )
    return ok


def _move_verdict(expected: str | None, actual: str | None) -> str:
    if expected is None:
        return "unchanged" if actual is None else "UNEXPECTED"
    if actual is None:
        return "MISSED"
    return "moved" if actual == expected else "WRONG"


def _check_moves(con: Any, seasons: list[int], window: list[int], rerun: list[int]) -> bool:
    """Check b: the rows that changed class are exactly the expected rows.

    Joins the snapshot to the new pool on ``pitch_id`` (every row), and the
    MOVE_CODES rows of ``raw.pitches`` to the new pool on the pitch's key. A
    season the snapshot already held at the running builder (a re-run)
    expects no move (``expected_move`` is blanked). ``raw_move`` keeps each
    pitch's move from its raw code, un-blanked: the round-trip grades the
    copy-to-export changes on it (:func:`_window_moves`), so a re-run grades
    the same as a first run.
    """
    sl = _season_list(seasons)
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE _sim553_changed AS
        SELECT b.pitch_id, n.season, n.stand, b.outcome_type AS old_class,
               n.outcome_type AS new_class, n.game_pk, n.at_bat_number, n.pitch_number, n.got_away
        FROM _sim553_before b
        JOIN sim.pitch_pool n ON n.pitch_id = b.pitch_id AND n.season = b.season
        WHERE b.outcome_type IS DISTINCT FROM n.outcome_type
        """
    )
    rerun_list = _season_list(rerun) if rerun else "NULL"
    # Every MOVE_CODES pool row with its source code, count and expected move
    # (check c reads the code from here too).
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _sim553_expected AS
        WITH r AS ({_raw_code_rows_sql(seasons)})
        SELECT p.pitch_id, p.season, p.stand, r.code, r.strikes, r.events,
               CASE WHEN p.season IN ({rerun_list}) THEN NULL ELSE r.expected_move END
                   AS expected_move,
               r.expected_move AS raw_move
        FROM r JOIN sim.pitch_pool p
          ON p.game_pk = r.game_pk AND p.at_bat_number = r.at_bat_number
         AND p.pitch_number = r.pitch_number AND p.season IN ({sl})
        """
    )
    rows = con.execute(
        """
        SELECT COALESCE(c.season, e.season) AS season, e.code, e.strikes, e.expected_move,
               CASE WHEN c.pitch_id IS NULL THEN NULL
                    ELSE c.old_class || '>' || c.new_class END AS actual_move,
               COUNT(*) AS n
        FROM _sim553_changed c FULL OUTER JOIN _sim553_expected e ON e.pitch_id = c.pitch_id
        GROUP BY ALL
        """
    ).fetchall()
    # A changed row outside the MOVE_CODES has no code yet: look it up.
    outside = con.execute(
        f"""
        SELECT c.season, TRIM(r.type) AS code, r.strikes,
               c.old_class || '>' || c.new_class AS actual_move, COUNT(*)
        FROM _sim553_changed c
        JOIN pg.raw.pitches r
          ON r.game_pk = c.game_pk AND r.at_bat_number = c.at_bat_number
         AND r.pitch_number = c.pitch_number
        WHERE r.data_quality_flag = FALSE AND r.season IN ({sl})
          AND (r.type IS NULL OR TRIM(r.type) NOT IN {_sql_in(MOVE_CODES)})
        GROUP BY ALL
        """
    ).fetchall()
    table: dict[tuple[str, str, str, str], list[int]] = {}
    unexplained = 0
    for season, code, strikes, expected, actual, n in rows:
        if code is None:
            unexplained += int(n)  # re-counted with its code from ``outside``
            continue
        key = (str(code), str(strikes), expected or "-", actual or "-")
        cell = table.setdefault(key, [0, 0])
        cell[1] += int(n)
        if int(season) in window:
            cell[0] += int(n)
    looked_up = 0
    for season, code, strikes, actual, n in outside:
        looked_up += int(n)
        key = (str(code), str(strikes), "-", actual)
        cell = table.setdefault(key, [0, 0])
        cell[1] += int(n)
        if int(season) in window:
            cell[0] += int(n)
    ok = looked_up == unexplained
    if not ok:
        _log(
            f"  b. {unexplained:,} changed rows have no MOVE_CODES source row, and the code "
            f"lookup found {looked_up:,}: a changed row has no raw.pitches row  MISMATCH"
        )
    _log("  b. the class moves, by code and strike count (window 2023-2026 | all seasons):")
    _log(
        f"     {'code':<5}{'strikes':<9}{'expected move':<24}{'actual move':<24}{'window':>9}{'all':>9}  verdict"
    )
    moved = [0, 0]
    for key in sorted(table):
        code, strikes, expected, actual = key
        win_n, all_n = table[key]
        verdict = _move_verdict(
            None if expected == "-" else expected, None if actual == "-" else actual
        )
        if verdict in ("UNEXPECTED", "MISSED", "WRONG"):
            ok = False
        if actual != "-":
            moved[0] += win_n
            moved[1] += all_n
        _log(
            f"     {code:<5}{strikes:<9}{expected:<24}{actual:<24}{win_n:>9,}{all_n:>9,}  {verdict}"
        )
    if rerun:
        _log(
            f"  b. seasons already at {POOL_BUILDER_VERSION} before the rebuild expect no move: {rerun}"
        )
    _log(
        f"  b. rows that changed class: window {moved[0]:,} (measured 2026-09-23: "
        f"{WINDOW_EXPECTED_MOVES:,}), all seasons {moved[1]:,} (about "
        f"{ALL_SEASONS_EXPECTED_MOVES:,}) {'OK' if ok else 'MISMATCH'}"
    )
    return ok


def _window_moves(con: Any, window: list[int]) -> tuple[list[int], list[str]]:
    """Every window pitch the export writes (the batting sides 'L' and 'R')
    whose raw code moves under the new coding: its ``pitch_id`` and its move
    ``'old>new'`` (check b's ``raw_move``, never blanked on a re-run). The
    round-trip reads these after the writable connection closes."""
    rows = con.execute(
        "SELECT pitch_id, raw_move FROM _sim553_expected "
        f"WHERE raw_move IS NOT NULL AND season IN ({_season_list(window)}) "
        "AND stand IN ('L', 'R') ORDER BY pitch_id"
    ).fetchall()
    return [int(r[0]) for r in rows], [str(r[1]) for r in rows]


def _check_got_away(con: Any, window: list[int]) -> bool:
    """Check c: no moved foul tip or foul bunt carries the got-away flag.

    A moved T / L / O row is a strike three on a swing under the new coding, and a got-away
    flag would let the dropped-third-strike rule (SIM-484) put the batter on
    base. By rule a foul tip is caught, so the flag must be absent (the
    design's §2.5). The gate covers those rows only. A moved 'Q' is a real
    swing and miss (at a pitchout), so a passed ball on it is a real got-away,
    exactly as on an 'S' or 'W' row: the check reports such rows by name and
    does not fail on them. (For example: game 824606, at-bat 14,
    pitch 8, a strikeout on a swinging pitchout with a passed ball.)
    """
    wl = _season_list(window)
    foul_tips = _sql_in(STRIKE_THREE_FOUL_TYPES)
    gated, gated_win, other, null_n, n_all = con.execute(
        f"""
        SELECT COUNT(*) FILTER (WHERE c.got_away AND e.code IN {foul_tips}),
               COUNT(*) FILTER (WHERE c.got_away AND e.code IN {foul_tips}
                                AND c.season IN ({wl})),
               COUNT(*) FILTER (WHERE c.got_away AND e.code IS DISTINCT FROM NULL
                                AND e.code NOT IN {foul_tips}),
               COUNT(*) FILTER (WHERE c.got_away IS NULL),
               COUNT(*)
        FROM _sim553_changed c LEFT JOIN _sim553_expected e ON e.pitch_id = c.pitch_id
        """
    ).fetchone()
    ok = int(gated) == 0
    _log(
        f"  c. moved {'/'.join(STRIKE_THREE_FOUL_TYPES)} rows with the got-away flag: {gated:,} "
        f"(window {gated_win:,}) of {n_all:,} moved rows; flag NULL on {null_n:,} "
        f"{'OK' if ok else 'MISMATCH'}"
    )
    if int(other):
        _log(
            f"  c. reported, not gated: {other:,} other moved rows carry the flag (a real swing "
            "and miss with a passed ball or a wild pitch):"
        )
        for season, pid, code, strikes, events, move in con.execute(
            """
            SELECT c.season, c.pitch_id, e.code, e.strikes, e.events,
                   c.old_class || '>' || c.new_class
            FROM _sim553_changed c JOIN _sim553_expected e ON e.pitch_id = c.pitch_id
            WHERE c.got_away AND e.code NOT IN """
            + foul_tips
            + " ORDER BY c.pitch_id LIMIT 20"
        ).fetchall():
            _log(
                f"     {season} pitch_id {pid}: code {code} at {strikes} strikes, "
                f"events {events}, {move}"
            )
    return ok


def _check_label(con: Any, window: list[int], prefix: str = "d.") -> bool:
    """Check d: the count chain of the window pool's plate appearances against
    real plate appearances, every channel within LABEL_CHECK_TOLERANCE.

    Both sides cover the same groups. The chain reads only the pool
    rows of the at-bats that end in a batting event (``pa_only``), the rule
    ``real_pa_rates`` applies. A chain over every row also plays on the
    at-bats that never reached an end (an inning-ending pickoff, a game ended
    mid-at-bat), about half of them balls, which pushes walks up about 0.6%.
    The full-pool chain is the band centre (the simulator draws every row),
    so it prints for the record.

    The rule holds only while both sides read the same plate appearances.
    So the check also compares the counts: the groups the ``pa_only`` chain
    reads (``pa_group_count``, from the chain's own SQL) must equal
    ``real_pa_rates``' ``n_pa``, or the check FAILS (for example, when
    ``raw.pitches`` gained games the pool has not been rebuilt for).
    """
    pred = f"season IN ({_season_list(window)})"
    chain = chain_rates(con, pred, pa_only=True)
    real = real_pa_rates(con, pred)
    n_real = int(real["n_pa"])
    n_pool = pa_group_count(con, pred)
    rows = label_check(chain, real)
    _log(
        f"  {prefix} the count chain of the pool's plate appearances (the at-bats that end in a "
        f"batting event) against {n_real:,} real plate appearances ({pred}), "
        f"tolerance {LABEL_CHECK_TOLERANCE:.1%}:"
    )
    _log(f"     {format_pa_count(n_pool, n_real)}")
    for line in format_label_check(rows):
        _log(f"     {line}")
    full = chain_rates(con, pred)
    weighted = chain_rates(con, pred, weighted=True)
    _log(
        f"  {prefix} for the record — the band centres (the chain over every pool row, which the "
        f"simulator draws): unweighted K/PA {full['k']:.4f}  BB/PA {full['walk']:.4f}  "
        f"HBP/PA {full['hbp']:.4f}  in play {full['in_play']:.4f}  "
        f"pitches/PA {full['pitches']:.3f}; recency-weighted K/PA {weighted['k']:.4f}  "
        f"BB/PA {weighted['walk']:.4f}  HBP/PA {weighted['hbp']:.4f}  "
        f"in play {weighted['in_play']:.4f}  pitches/PA {weighted['pitches']:.3f}"
    )
    ok = n_pool == n_real and all(row[4] for row in rows)
    _log(f"  {prefix} label check {'PASS' if ok else 'FAIL'}")
    return ok


def _check_metadata(con: Any, seasons: list[int]) -> bool:
    """Check e: the build metadata names the running builder for every season,
    and its row count is the table's."""
    after = _pool_counts(con, seasons)
    meta = {
        int(s): (v, int(n))
        for s, v, n in con.execute(
            "SELECT season, builder_version, row_count FROM sim.pool_build_metadata "
            f"WHERE pool_name = 'pitch_pool' AND season IN ({_season_list(seasons)})"
        ).fetchall()
    }
    ok = True
    for s in seasons:
        version, n = meta.get(s, (None, -1))
        good = version == POOL_BUILDER_VERSION and n == after.get(s)
        ok = ok and good
        _log(f"  e. {s}: builder {version}  row_count {n:,}  {'OK' if good else 'MISMATCH'}")
    return ok


def _matches_snapshot(
    con: Any, seasons: list[int], before: dict[int, int], versions: dict[int, str | None]
) -> bool:
    """TRUE when the pool holds the snapshot's rows, classes and builders."""
    if _pool_counts(con, seasons) != before or _builder_versions(con, seasons) != versions:
        return False
    differ = con.execute(
        "SELECT COUNT(*) FROM _sim553_before b "
        "LEFT JOIN sim.pitch_pool n ON n.pitch_id = b.pitch_id AND n.season = b.season "
        "WHERE n.outcome_type IS DISTINCT FROM b.outcome_type"
    ).fetchone()[0]
    return int(differ) == 0


def _rollback(
    con: Any, seasons: list[int], before: dict[int, int], versions: dict[int, str | None]
) -> None:
    """Roll the rebuild's transaction back and show the table is as it was."""
    try:
        con.execute("ROLLBACK")
    except Exception as exc:  # noqa: BLE001 - report, then verify what the table holds
        _log(f"  ROLLBACK raised {type(exc).__name__}: {exc}")
    restored = _matches_snapshot(con, seasons, before, versions)
    _log(
        "  the transaction is rolled back: sim.pitch_pool "
        + (
            "holds its old rows, classes and builders"
            if restored
            else "DOES NOT match the snapshot — read it before the next export"
        )
    )


def _restore(
    con: Any, seasons: list[int], before: dict[int, int], versions: dict[int, str | None]
) -> None:
    """--no-transaction: put the snapshot's classes and builders back after a
    failed check. The autocommit rebuild commits row by row, so there is no
    transaction to roll back. The other columns stay rebuilt from the source;
    only the class and the builder carry the old coding."""
    try:
        matched = con.execute(
            "SELECT COUNT(*) FROM _sim553_before b "
            "JOIN sim.pitch_pool n ON n.pitch_id = b.pitch_id AND n.season = b.season"
        ).fetchone()[0]
        if _pool_counts(con, seasons) != before or int(matched) != sum(before.values()):
            _log(
                "  RESTORE NOT POSSIBLE: sim.pitch_pool's rows differ from the snapshot. The "
                "bundle is untouched and the app is safe, but do not run the nightly "
                "engine-artifact export until the pool is rebuilt and checked"
            )
            return
        con.execute(
            "UPDATE sim.pitch_pool AS n SET outcome_type = b.outcome_type "
            "FROM _sim553_before AS b "
            "WHERE n.pitch_id = b.pitch_id AND n.outcome_type IS DISTINCT FROM b.outcome_type"
        )
        for s, v in versions.items():
            con.execute(
                "UPDATE sim.pool_build_metadata SET builder_version = ? "
                "WHERE pool_name = 'pitch_pool' AND season = ?",
                [v, s],
            )
    except Exception as exc:  # noqa: BLE001 - report, then verify what the table holds
        _log(f"  the restore raised {type(exc).__name__}: {exc}")
    restored = _matches_snapshot(con, seasons, before, versions)
    _log(
        "  restored from the snapshot: sim.pitch_pool "
        + (
            "holds its old classes and builders"
            if restored
            else "DOES NOT match the snapshot — read it before the next export"
        )
    )
