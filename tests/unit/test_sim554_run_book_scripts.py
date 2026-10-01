"""SIM-554 — the run book's two scripts, on in-memory fixtures (no live DB, no bundle).

``scripts/sim554_rebuild_steal_pool.py`` rebuilds the steal opportunity pool
with the pitch class and the pickoff rows, then exports the steal pool alone.
``scripts/sim554_running_game_census.py`` reads where the simulator's steals
and pickoffs land, two arms side by side. These tests pin the scripts' own
logic:

  * the migration's statement splitter, and the migration applied twice;
    what the log says of its two columns after a rollback, also when they
    were on the table before the run;
  * the pre-flight: a clean state goes; a missing migration, a stale build
    watermark, a torn bundle and the wrong builder each stop it; a killed
    export's staging files do not stop the re-run;
  * the checks inside the rebuild's transaction catch a changed label (and
    name the changed pitch row), an extra pitch row, a pickoff row with a
    class, one wrong fact on a pickoff row and an attempted ball in play,
    and a rollback restores the snapshot;
  * the pickoff rows counted from the play records, independently of the
    builder: which outcomes fit a pair, which are tagged, which become rows,
    which are listed;
  * the band centres over pitch rows only, and the export's round-trip;
  * the census's reading of one step (opportunity, attempt, class, no-pitch
    third out, intentional walk, dropped-third-strike reach) and the
    report's stop rules.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

duckdb = pytest.importorskip("duckdb")

REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = REPO / "scripts"


def _load(name: str):
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rb = _load("sim554_rebuild_steal_pool")

WINDOW = [2023, 2024, 2025, 2026]

_STEAL_DDL = """
CREATE TABLE sim.steal_opportunity_pool (
    pitch_id BIGINT NOT NULL, game_pk INTEGER NOT NULL,
    at_bat_number INTEGER NOT NULL, pitch_number INTEGER NOT NULL,
    game_date DATE NOT NULL, season SMALLINT NOT NULL,
    runner_id INTEGER NOT NULL, pitcher_id INTEGER NOT NULL, catcher_id INTEGER,
    target_base SMALLINT NOT NULL CHECK (target_base IN (2, 3)),
    inning SMALLINT NOT NULL, outs SMALLINT NOT NULL CHECK (outs BETWEEN 0 AND 2),
    count_balls SMALLINT NOT NULL, count_strikes SMALLINT NOT NULL,
    score_diff SMALLINT NOT NULL,
    attempted BOOLEAN NOT NULL, success BOOLEAN NOT NULL DEFAULT FALSE,
    recency_weight FLOAT NOT NULL DEFAULT 1.0,
    pickoff_out BOOLEAN DEFAULT FALSE,
    pickoff_advancing BOOLEAN DEFAULT FALSE,
    pickoff_error BOOLEAN DEFAULT FALSE{extra},
    PRIMARY KEY (pitch_id)
)
"""
_NEW_COLS = ",\n    pitch_class VARCHAR(20),\n    is_pickoff_row BOOLEAN DEFAULT FALSE"


def _db(*, migrated: bool = True):
    """An in-memory DuckDB with ``pg`` attached in memory (``attach_pg`` is then a no-op)."""
    c = duckdb.connect(":memory:")
    c.execute("ATTACH ':memory:' AS pg")
    c.execute("CREATE SCHEMA pg.raw")
    c.execute(  # the columns the steal-pool builder reads
        "CREATE TABLE pg.raw.pitches (game_pk INTEGER, at_bat_number INTEGER, "
        "pitch_number INTEGER, game_date DATE, season INTEGER, pitcher INTEGER, "
        "fielder_2 INTEGER, inning SMALLINT, inning_topbot VARCHAR, outs SMALLINT, "
        "balls SMALLINT, strikes SMALLINT, bat_score SMALLINT, fld_score SMALLINT, "
        "on_1b INTEGER, on_2b INTEGER, on_3b INTEGER, sb_attempt_2b BOOLEAN, "
        "sb_attempt_3b BOOLEAN, sb_success_2b BOOLEAN, sb_success_3b BOOLEAN, "
        "data_quality_flag BOOLEAN, events VARCHAR)"
    )
    c.execute(
        "CREATE TABLE pg.raw.play_events (id BIGINT, game_pk INTEGER, at_bat_number INTEGER, "
        "game_date DATE, season SMALLINT, event_type VARCHAR, inning SMALLINT, "
        "inning_topbot VARCHAR, outs_before SMALLINT, runners_state SMALLINT, "
        "bat_score SMALLINT, fld_score SMALLINT, pitcher_id INTEGER, runner_id INTEGER, "
        "base SMALLINT, is_out BOOLEAN)"
    )
    c.execute("CREATE SCHEMA sim")
    c.execute(
        "CREATE TABLE sim.pitch_pool (pitch_id BIGINT, game_pk INTEGER, at_bat_number INTEGER, "
        "pitch_number INTEGER, season SMALLINT, outcome_type VARCHAR)"
    )
    c.execute(_STEAL_DDL.format(extra=_NEW_COLS if migrated else ""))
    c.execute(
        "CREATE TABLE sim.pool_build_metadata (pool_name VARCHAR, season SMALLINT, "
        "row_count BIGINT, source_max_game_date DATE, source_row_count BIGINT, "
        "recency_ref_season SMALLINT, builder_version VARCHAR, "
        "built_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (pool_name, season))"
    )
    return c


def _pitch_id(pk: int, ab: int, pn: int) -> int:
    return int(f"{pk}{ab:03d}{pn:03d}")


def _steal_row(
    c,
    pk,
    ab,
    pn,
    *,
    season=2024,
    target=2,
    attempted=False,
    success=False,
    po_out=False,
    po_adv=False,
    po_err=False,
    pitch_class="ball",
    outs=0,
    balls=0,
    strikes=0,
    with_pitch_pool=True,
):
    """One PITCH row of the steal pool (and its pitch-pool row)."""
    pid = _pitch_id(pk, ab, pn)
    c.execute(
        "INSERT INTO sim.steal_opportunity_pool VALUES "
        "(?, ?, ?, ?, DATE '2024-06-01', ?, 11, 901, 902, ?, 1, ?, ?, ?, 0, ?, ?, 1.0, "
        "?, ?, ?, ?, FALSE)",
        [
            pid,
            pk,
            ab,
            pn,
            season,
            target,
            outs,
            balls,
            strikes,
            attempted,
            success,
            po_out,
            po_adv,
            po_err,
            pitch_class,
        ],
    )
    if with_pitch_pool:
        c.execute(
            "INSERT INTO sim.pitch_pool VALUES (?, ?, ?, ?, ?, ?)",
            [pid, pk, ab, pn, season, pitch_class],
        )
    return pid


def _event(
    c,
    eid,
    pk,
    ab,
    *,
    season=2024,
    event_type="pickoff",
    is_out=True,
    base=1,
    runners_state=1,
    runner_id=11,
    pitcher_id=901,
    outs_before=1,
):
    c.execute(
        "INSERT INTO pg.raw.play_events VALUES "
        "(?, ?, ?, DATE '2024-06-01', ?, ?, 1, 'Top', ?, ?, 3, 1, ?, ?, ?, ?)",
        [
            eid,
            pk,
            ab,
            season,
            event_type,
            outs_before,
            runners_state,
            pitcher_id,
            runner_id,
            base,
            is_out,
        ],
    )


def _pickoff_row(
    c,
    eid,
    pk,
    ab,
    *,
    season=2024,
    target=2,
    outs=1,
    out=True,
    adv=False,
    err=False,
    pitch_class=None,
    runner=11,
    pitcher=901,
    score_diff=2,
):
    """A pickoff row the way the builder writes one (the design's §4.2)."""
    c.execute(
        "INSERT INTO sim.steal_opportunity_pool VALUES "
        "(?, ?, ?, 0, DATE '2024-06-01', ?, ?, ?, 902, ?, 1, ?, 0, 0, ?, FALSE, FALSE, 1.0, "
        "?, ?, ?, ?, TRUE)",
        [
            -eid,
            pk,
            ab,
            season,
            runner,
            pitcher,
            target,
            outs,
            score_diff,
            out,
            adv,
            err,
            pitch_class,
        ],
    )


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


class TestTheMigration:
    def test_the_splitter_drops_comment_lines_and_tails(self):
        text = (
            "-- 0031 header; a semicolon in a comment\n"
            "ALTER TABLE sim.t ADD COLUMN IF NOT EXISTS a VARCHAR(20); -- tail; more\n"
            "\n"
            "-- a whole comment line\n"
            "ALTER TABLE sim.t ADD COLUMN IF NOT EXISTS b BOOLEAN DEFAULT FALSE;\n"
        )
        assert rb.migration_statements(text) == [
            "ALTER TABLE sim.t ADD COLUMN IF NOT EXISTS a VARCHAR(20)",
            "ALTER TABLE sim.t ADD COLUMN IF NOT EXISTS b BOOLEAN DEFAULT FALSE",
        ]

    def test_the_real_file_is_two_add_column_statements(self):
        stmts = rb.migration_statements(rb.MIGRATION.read_text(encoding="utf-8"))
        assert len(stmts) == 2
        assert all("ADD COLUMN IF NOT EXISTS" in s for s in stmts)
        assert "pitch_class" in stmts[0] and "is_pickoff_row" in stmts[1]

    def test_applied_twice_it_adds_the_columns_once(self):
        c = _db(migrated=False)
        c.execute(  # an existing row, written before the migration
            "INSERT INTO sim.steal_opportunity_pool VALUES (1, 1, 1, 1, DATE '2024-06-01', 2024, "
            "11, 901, 902, 2, 1, 0, 0, 0, 0, FALSE, FALSE, 1.0, FALSE, FALSE, FALSE)"
        )
        assert rb.missing_columns(c) == ["pitch_class", "is_pickoff_row"]
        assert rb.apply_migration(c) == 2
        assert rb.apply_migration(c) == 2  # idempotent
        assert rb.missing_columns(c) == []
        # The existing row reads no class and is not a pickoff row.
        assert c.execute(
            "SELECT pitch_class, is_pickoff_row FROM sim.steal_opportunity_pool"
        ).fetchall() == [(None, False)]


class TestTheColumnsAfterARollback:
    """What the log says about migration 0031's two columns after a failed rebuild."""

    def test_columns_this_run_added_are_gone_with_the_rows(self, capsys):
        rb._log_migration_rolled_back(_db(migrated=False), migrated_here=True)
        out = capsys.readouterr().out
        assert "rolled back with the rows: the table has its old shape" in out
        assert "WARNING" not in out

    def test_columns_this_run_added_that_stayed_are_a_warning(self, capsys):
        rb._log_migration_rolled_back(_db(migrated=True), migrated_here=True)
        assert "WARNING: the 0031 columns ['pitch_class', 'is_pickoff_row'] stayed" in (
            capsys.readouterr().out
        )

    def test_columns_on_the_table_before_the_run_stay_with_a_warning(self, capsys):
        rb._log_migration_rolled_back(_db(migrated=True), migrated_here=False)
        out = capsys.readouterr().out
        assert (
            "WARNING: the 0031 columns ['pitch_class', 'is_pickoff_row'] were on the table "
            "before this run, so the rollback keeps them. Master's pool builder fails" in out
        )

    def test_a_table_that_never_had_the_columns_needs_no_word(self, capsys):
        rb._log_migration_rolled_back(_db(migrated=False), migrated_here=False)
        assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# The pre-flight
# ---------------------------------------------------------------------------


def _write_steal_pool(
    pool_dir: Path,
    counts: dict[str, int],
    seasons=WINDOW,
    *,
    with_class=False,
    sit_rows: dict[str, int] | None = None,
) -> None:
    """A steal pool directory in the export's shape."""
    pool_dir.mkdir(parents=True, exist_ok=True)
    mem = duckdb.connect()
    for t, n in counts.items():
        rows = (sit_rows or {}).get(t, n)
        np.save(pool_dir / f"{t}.sit.npy", np.zeros((rows, 4), dtype=np.float32))
        cls = ", CASE WHEN i % 2 = 0 THEN 'ball' ELSE '' END AS pitch_class" if with_class else ""
        mem.execute(
            f"COPY (SELECT i AS pitch_id, 11 AS runner_id, 901 AS pitcher_id, 902 AS catcher_id, "
            f"2024 AS season, FALSE AS attempted, FALSE AS success, 1.0 AS recency_weight, "
            f"FALSE AS pickoff_out, FALSE AS pickoff_advancing, FALSE AS pickoff_error{cls} "
            f"FROM range({n}) t(i)) TO '{(pool_dir / f'{t}.meta.parquet').as_posix()}' "
            "(FORMAT parquet)"
        )
    mem.close()
    (pool_dir / "manifest.json").write_text(
        json.dumps(
            {
                "seasons": list(seasons),
                "counts": counts,
                "sit_cols": ["count_balls", "count_strikes", "outs", "score_diff"],
            }
        )
    )


@pytest.fixture
def builder_554(monkeypatch):
    monkeypatch.setattr(rb.ppc, "POOL_BUILDER_VERSION", "sim554.1")


def _clean_state(tmp_path: Path):
    """A state the pre-flight clears: the window's pitch pool built by
    sim553.1, the steal pool's watermark on raw.pitches today, a whole bundle."""
    c = _db()
    for i, s in enumerate(WINDOW):
        c.execute(
            "INSERT INTO pg.raw.pitches (game_pk, at_bat_number, pitch_number, game_date, "
            "season, data_quality_flag) VALUES (?, 1, 1, DATE '2024-06-01', ?, FALSE)",
            [100 + i, s],
        )
        c.execute(
            "INSERT INTO sim.pitch_pool VALUES (?, ?, 1, 1, ?, 'ball')",
            [_pitch_id(100 + i, 1, 1), 100 + i, s],
        )
        c.execute(
            "INSERT INTO sim.pool_build_metadata (pool_name, season, row_count, "
            "source_max_game_date, source_row_count, recency_ref_season, builder_version) "
            "VALUES ('pitch_pool', ?, 1, DATE '2024-06-01', 1, 2026, 'sim553.1'), "
            "('steal_opportunity_pool', ?, 1, DATE '2024-06-01', 1, 2026, 'sim553.1')",
            [s, s],
        )
    art = tmp_path / "engine_artifacts"
    _write_steal_pool(art / "steal_pool", {"2": 3, "3": 2})
    return c, str(art), str(art) + rb.BACKUP_SUFFIX


def _preflight(c, art, backup, *, apply=False, force=False, seasons=WINDOW):
    return rb.preflight(
        c, list(seasons), rb.last_n_seasons(c), art, backup, force=force, apply_migration_flag=apply
    )


class TestThePreflight:
    def test_a_clean_state_goes(self, tmp_path, builder_554):
        c, art, backup = _clean_state(tmp_path)
        assert rb.last_n_seasons(c) == WINDOW
        assert _preflight(c, art, backup) == []

    def test_the_wrong_builder_stops(self, tmp_path, monkeypatch):
        c, art, backup = _clean_state(tmp_path)
        monkeypatch.setattr(rb.ppc, "POOL_BUILDER_VERSION", "sim553.1")
        problems = _preflight(c, art, backup)
        assert len(problems) == 1 and "POOL_BUILDER_VERSION is 'sim553.1'" in problems[0]

    def test_a_missing_migration_stops_unless_it_is_applied(self, tmp_path, builder_554):
        c, art, backup = _clean_state(tmp_path)
        c.execute("ALTER TABLE sim.steal_opportunity_pool DROP COLUMN is_pickoff_row")
        problems = _preflight(c, art, backup)
        assert any("migration 0031 is not applied" in p for p in problems)
        assert _preflight(c, art, backup, apply=True) == []

    def test_a_stale_watermark_stops(self, tmp_path, builder_554):
        c, art, backup = _clean_state(tmp_path)
        c.execute(
            "INSERT INTO pg.raw.pitches (game_pk, at_bat_number, pitch_number, game_date, "
            "season, data_quality_flag) VALUES (999, 1, 1, DATE '2024-06-02', 2026, FALSE)"
        )
        problems = _preflight(c, art, backup)
        assert len(problems) == 1 and problems[0].startswith("2026: raw.pitches changed")

    def test_a_pitch_pool_on_an_old_builder_stops(self, tmp_path, builder_554):
        c, art, backup = _clean_state(tmp_path)
        c.execute(
            "UPDATE sim.pool_build_metadata SET builder_version = 'sim523g.1' "
            "WHERE pool_name = 'pitch_pool' AND season = 2025"
        )
        problems = _preflight(c, art, backup)
        assert len(problems) == 1 and problems[0].startswith("2025: the pitch pool")

    def test_a_torn_bundle_stops(self, tmp_path, builder_554):
        c, art, backup = _clean_state(tmp_path)
        _write_steal_pool(Path(art) / "steal_pool", {"2": 3, "3": 2}, sit_rows={"2": 2})
        problems = _preflight(c, art, backup)
        assert any("not whole" in p and "target 2" in p for p in problems)

    def test_a_stray_file_stops(self, tmp_path, builder_554):
        c, art, backup = _clean_state(tmp_path)
        (Path(art) / "steal_pool" / "notes.txt").write_text("x")
        assert any("notes.txt" in p for p in _preflight(c, art, backup))

    def test_a_season_list_without_the_window_stops(self, tmp_path, builder_554):
        c, art, backup = _clean_state(tmp_path)
        problems = _preflight(c, art, backup, seasons=[2024, 2025, 2026])
        assert any("leave out the window seasons [2023]" in p for p in problems)


class TestTheRollbackPoint:
    def test_no_copy_means_copy_the_bundle(self, tmp_path):
        _c, art, backup = _clean_state(tmp_path)
        plan = rb.backup_plan(art, backup, force=False)
        assert plan.action == "copy" and not plan.problems

    def test_a_whole_copy_equal_to_the_bundle_is_kept(self, tmp_path):
        _c, art, backup = _clean_state(tmp_path)
        assert rb.take_backup(art, backup, force=False) == backup
        assert rb.backup_plan(art, backup, force=False).action == "keep"

    def test_a_copy_the_bundle_moved_on_from_stops_unless_forced(self, tmp_path):
        _c, art, backup = _clean_state(tmp_path)
        rb.take_backup(art, backup, force=False)
        _write_steal_pool(Path(art) / "steal_pool", {"2": 4, "3": 2})  # a later old-shape export
        plan = rb.backup_plan(art, backup, force=False)
        assert plan.problems and "changed after the copy" in plan.problems[0]
        forced = rb.backup_plan(art, backup, force=True)
        assert forced.action == "replace" and not forced.problems

    def test_a_started_export_keeps_the_copy_over_a_half_written_bundle(self, tmp_path):
        _c, art, backup = _clean_state(tmp_path)
        rb.take_backup(art, backup, force=False)
        Path(rb.export_marker(backup)).write_text("started")
        _write_steal_pool(Path(art) / "steal_pool", {"2": 4, "3": 2}, sit_rows={"2": 1})
        plan = rb.backup_plan(art, backup, force=False)
        assert plan.action == "keep" and not plan.problems
        problems = rb.bundle_problems(art, backup, WINDOW, force=False)
        assert problems == []  # the export rewrites the half-written pool

    @pytest.mark.parametrize(
        "strays",
        [
            ["tmp_2.sit.npy"],
            ["tmp_2.sit.npy", "tmp_2.meta.parquet"],
            ["tmp_3.sit.npy", "tmp_tmp_3.meta.parquet"],
        ],
        ids=["the-array-only", "both-files-of-target-2", "duckdbs-own-copy-file"],
    )
    def test_a_killed_exports_staging_files_do_not_stop_the_rerun(
        self, tmp_path, builder_554, strays
    ):
        """A killed export (a blue screen, ``docker stop``) skips the export's
        own clean-up, so the old pool keeps the export's staging files. The
        re-run keeps the copy, and the next export overwrites those files and
        moves them into place."""
        c, art, backup = _clean_state(tmp_path)
        rb.take_backup(art, backup, force=False)
        Path(rb.export_marker(backup)).write_text("started")
        pool_dir = Path(art) / "steal_pool"
        for name in strays:
            (pool_dir / name).write_bytes(b"half-written")
        assert all(rb._export_replaces(name) for name in strays)
        assert rb.steal_pool_problem(str(pool_dir)) is None  # the old five files are whole
        assert rb.backup_plan(art, backup, force=False).action == "keep"
        assert _preflight(c, art, backup) == []

    def test_every_staging_file_of_the_export_is_one_the_rerun_accepts(self, tmp_path, monkeypatch):
        """The export's own staging names, read from a real export that fails
        before it moves its files: the run book knows every one of them."""
        c = _db()
        _steal_row(c, 10, 1, 1)
        _pickoff_row(c, 5, 10, 2)
        _steal_row(c, 10, 3, 1, target=3)
        left: list[str] = []

        def killed(tmp: str, _final: str) -> None:
            left.extend(sorted(set(os.listdir(os.path.dirname(tmp))) - rb.STEAL_POOL_FILES))
            raise RuntimeError("killed before the move")

        with monkeypatch.context() as m:
            m.setattr(os, "replace", killed)
            with pytest.raises(RuntimeError, match="killed before the move"):
                rb.build_steal_pool_artifact(c, str(tmp_path), [2024])
        assert left == [
            "tmp_2.meta.parquet",
            "tmp_2.sit.npy",
            "tmp_3.meta.parquet",
            "tmp_3.sit.npy",
        ]
        assert all(rb._export_replaces(name) for name in left)


# ---------------------------------------------------------------------------
# The pickoff rows counted from the play records
# ---------------------------------------------------------------------------


def _planted_outcomes(c):
    """The play records of one season against the snapshot's pitch rows.

    Event 1 and event 8: a pickoff out and an error in plate appearance (10, 1),
    which holds a target-2 pitch row: one tag, one outcome folded into it.
    Event 2: an out at second (the runner broke), no pitch of the pair: a row.
    Event 3: an error on the runner on second, no pitch: a target-3 row.
    Event 4: a throw to first with first and second occupied: no pair.
    Event 5: a pickoff throw with no outcome: not an outcome.
    Event 6: a throw to third: no pair.
    Event 7: an out with no runner id and no pitch: listed, no row.
    Event 9: another season.
    """
    _steal_row(c, 10, 1, 1)  # the target-2 pitch of plate appearance (10, 1)
    _event(c, 1, 10, 1, base=1, runners_state=1)
    _event(c, 8, 10, 1, event_type="pickoff_error", is_out=False, base=1, runners_state=1)
    _event(c, 2, 10, 2, base=2, runners_state=1, outs_before=2)
    _event(c, 3, 10, 3, event_type="pickoff_error", is_out=False, base=2, runners_state=2)
    _event(c, 4, 10, 4, base=1, runners_state=3)
    _event(c, 5, 10, 5, is_out=False, base=1, runners_state=1)
    _event(c, 6, 10, 6, base=3, runners_state=4)
    _event(c, 7, 10, 7, base=1, runners_state=1, runner_id=None)
    _event(c, 9, 10, 8, season=2022, base=1, runners_state=1)


class TestThePickoffCount:
    def test_the_count_sorts_every_outcome(self):
        c = _db()
        _planted_outcomes(c)
        rb.take_snapshot(c, [2024])
        rb.count_pickoff_rows(c, [2024])
        got = rb.pickoff_summary(c, [2024])[2024]
        assert (got.fit, got.with_pitch, got.tag_groups, got.folded, got.rows) == (5, 2, 1, 1, 2)
        assert [item[0] for item in got.no_id] == [7]
        rows = c.execute(
            f"SELECT id, pair, is_out, is_adv, is_err FROM {rb.EXPECT} "
            "WHERE NOT has_pitch AND has_ids ORDER BY id"
        ).fetchall()
        assert rows == [(2, 2, True, True, False), (3, 3, False, False, True)]

    def test_a_pitch_of_another_pair_does_not_count(self):
        c = _db()
        _steal_row(c, 10, 2, 1, target=3)  # the PA's only pitch row is the target-3 pair
        _event(c, 2, 10, 2, base=2, runners_state=1)  # a target-2 outcome
        rb.take_snapshot(c, [2024])
        rb.count_pickoff_rows(c, [2024])
        assert rb.pickoff_summary(c, [2024])[2024].rows == 1


def _rebuilt(c):
    """What the builder should write for :func:`_planted_outcomes`: the two rows."""
    _pickoff_row(c, 2, 10, 2, outs=2, out=True, adv=True)
    _pickoff_row(c, 3, 10, 3, target=3, out=False, err=True)


class TestTheChecks:
    def _setup(self):
        c = _db()
        _planted_outcomes(c)
        c.execute("UPDATE sim.steal_opportunity_pool SET pickoff_out = TRUE")  # the tag
        _steal_row(c, 11, 1, 1, attempted=True, success=True)
        _steal_row(c, 11, 1, 2, pitch_class="in_play")
        rb.take_snapshot(c, [2024])
        rb.count_pickoff_rows(c, [2024])
        return c, rb.pickoff_summary(c, [2024])

    def test_a_faithful_rebuild_passes_every_check(self):
        c, counts = self._setup()
        _rebuilt(c)
        assert rb.check_snapshot_back(c)
        assert rb.check_new_rows(c, [2024])
        assert rb.check_pickoff_rows(c, [2024], counts)
        assert rb.check_identity(c, [2024], counts)
        assert rb.check_classes(c, [2024])
        assert rb.check_dead_ball_attempts(c, [2024])
        assert rb.check_pickoff_row_ids(c, [2024])

    def test_a_changed_label_is_caught(self):
        c, _counts = self._setup()
        _rebuilt(c)
        c.execute(
            "UPDATE sim.steal_opportunity_pool SET success = FALSE WHERE pitch_id = ?",
            [_pitch_id(11, 1, 1)],
        )
        assert not rb.check_snapshot_back(c)

    def test_the_example_names_the_changed_pitch_row_not_the_renumbered_pickoff_rows(self, capsys):
        """Check a fails on a changed pitch row. A reloaded game also renumbered
        an earlier run's pickoff rows, which check a reports for information
        only, so its example line must list the pitch row alone."""
        c = _db()
        pid = _steal_row(c, 10, 1, 1, attempted=True, success=True)
        for eid in range(1, 7):  # an earlier run's pickoff rows
            _pickoff_row(c, eid, 10, 1 + eid)
        rb.take_snapshot(c, [2024])
        c.execute("DELETE FROM sim.steal_opportunity_pool WHERE pitch_id < 0")
        for eid in range(1, 7):  # the same outcomes under the reloaded game's new ids
            _pickoff_row(c, 1000 + eid, 10, 1 + eid)
        c.execute("UPDATE sim.steal_opportunity_pool SET success = FALSE WHERE pitch_id = ?", [pid])
        capsys.readouterr()
        assert not rb.check_snapshot_back(c)
        out = capsys.readouterr().out
        line = next(row for row in out.splitlines() if "a. snapshot pitch rows" in row)
        assert "(missing or changed: 1)" in line and line.endswith(f"e.g. [({pid},)]")
        assert "6 pickoff rows of an earlier run are not back" in out

    def test_an_extra_pitch_row_is_caught(self):
        c, _counts = self._setup()
        _rebuilt(c)
        _steal_row(c, 12, 1, 1)
        assert not rb.check_new_rows(c, [2024])

    def test_a_pickoff_row_with_a_class_is_caught(self):
        c, counts = self._setup()
        _pickoff_row(c, 2, 10, 2, outs=2, out=True, adv=True, pitch_class="ball")
        _pickoff_row(c, 3, 10, 3, target=3, out=False, err=True)
        assert not rb.check_classes(c, [2024])
        assert not rb.check_pickoff_rows(c, [2024], counts)

    def test_a_pickoff_row_with_the_wrong_label_or_none_at_all_is_caught(self):
        c, counts = self._setup()
        _pickoff_row(c, 2, 10, 2, outs=2, out=True, adv=False)  # the caught stealing lost
        assert not rb.check_pickoff_rows(c, [2024], counts)  # and event 3 has no row
        assert not rb.check_identity(c, [2024], counts)

    @pytest.mark.parametrize(
        "bad",
        [
            {"adv": False},
            {"outs": 1},
            {"runner": 99},
            {"pitcher": 999},
            {"score_diff": 0},
            {"target": 3},
        ],
        ids=["advancing", "outs", "runner", "pitcher", "score_diff", "target"],
    )
    def test_one_wrong_fact_on_a_counted_pickoff_row_is_caught(self, bad, capsys):
        """Both counted rows are present, so no row is missing or extra and the
        per-season count holds. Only the comparison of each row's facts with
        its outcome can fail check c here. Event 2's right row is a picked-off
        caught stealing (advancing) on the target-2 pair, with two outs,
        runner 11, pitcher 901 and a margin of +2."""
        c, counts = self._setup()
        _pickoff_row(c, 2, 10, 2, **{"outs": 2, "out": True, "adv": True, **bad})
        _pickoff_row(c, 3, 10, 3, target=3, out=False, err=True)
        capsys.readouterr()
        assert not rb.check_pickoff_rows(c, [2024], counts)
        out = capsys.readouterr().out
        assert "c. 2024: pickoff rows 2; counted 2  OK" in out
        assert "with no row 0; rows with no counted outcome 0;" in out
        assert "count or labels differ 1  MISMATCH" in out

    def test_an_attempted_ball_in_play_is_caught(self):
        c, _counts = self._setup()
        _rebuilt(c)
        c.execute(
            "UPDATE sim.steal_opportunity_pool SET attempted = TRUE WHERE pitch_id = ?",
            [_pitch_id(11, 1, 2)],
        )
        assert not rb.check_dead_ball_attempts(c, [2024])

    def test_a_class_unlike_the_pitch_pools_is_caught(self):
        c, _counts = self._setup()
        _rebuilt(c)
        c.execute(
            "UPDATE sim.pitch_pool SET outcome_type = 'foul' WHERE pitch_id = ?",
            [_pitch_id(11, 1, 1)],
        )
        assert not rb.check_classes(c, [2024])

    def test_a_rollback_restores_the_snapshot(self):
        c, _counts = self._setup()
        c.execute(
            "INSERT INTO sim.pool_build_metadata (pool_name, season, row_count, builder_version) "
            "VALUES ('steal_opportunity_pool', 2024, 3, 'sim553.1')"
        )
        before = rb._steal_counts(c, [2024])
        versions = rb._steal_versions(c, [2024])
        c.execute("BEGIN TRANSACTION")
        c.execute("DELETE FROM sim.steal_opportunity_pool")
        _rebuilt(c)
        assert not rb.matches_snapshot(c, [2024], before, versions)
        assert rb.rollback(c, [2024], before, versions)


def _raw_pitch(
    c,
    pk,
    ab,
    pn,
    *,
    on_1b=None,
    on_2b=None,
    balls=0,
    strikes=0,
    att2=False,
    suc2=False,
    pitch_class="ball",
):
    c.execute(
        "INSERT INTO pg.raw.pitches VALUES (?, ?, ?, DATE '2024-06-01', 2024, 901, 902, 1, "
        "'Top', 0, ?, ?, 3, 1, ?, ?, NULL, ?, FALSE, ?, FALSE, FALSE, NULL)",
        [pk, ab, pn, balls, strikes, on_1b, on_2b, att2, suc2],
    )
    c.execute(
        "INSERT INTO sim.pitch_pool VALUES (?, ?, ?, ?, 2024, ?)",
        [_pitch_id(pk, ab, pn), pk, ab, pn, pitch_class],
    )


def _builder_game(c) -> None:
    """One game the real builder reads. Plate appearance 1: a runner on first,
    two balls (the second a stolen base) and an errant pickoff throw (a tag).
    Plate appearance 2: a runner on second, one foul, and before the first
    pitch a runner on first put out at second: he broke, so a picked-off
    caught stealing (a target-2 pickoff row). Plate appearance 3: no runner on
    the pitch, and a pickoff out at second before it (a target-3 pickoff row)."""
    _raw_pitch(c, 10, 1, 1, on_1b=11)
    _raw_pitch(c, 10, 1, 2, on_1b=11, balls=1, att2=True, suc2=True)
    _raw_pitch(c, 10, 2, 1, on_2b=11, pitch_class="foul")
    _raw_pitch(c, 10, 3, 1, pitch_class="in_play")
    _event(c, 1, 10, 1, event_type="pickoff_error", is_out=False, base=1, runners_state=1)
    _event(c, 2, 10, 2, base=2, runners_state=1, outs_before=0)
    _event(c, 3, 10, 3, base=2, runners_state=2, outs_before=1, runner_id=12)


def _build(c) -> None:
    comp = rb.ppc.PlayerProfileComputor.__new__(rb.ppc.PlayerProfileComputor)
    comp._conn = c
    comp._build_steal_opportunity_pool([2024], incremental=False)


def _all_checks(c, counts) -> dict[str, bool]:
    return {
        "a": rb.check_snapshot_back(c),
        "b": rb.check_new_rows(c, [2024]),
        "c": rb.check_pickoff_rows(c, [2024], counts),
        "d": rb.check_identity(c, [2024], counts),
        "e": rb.check_classes(c, [2024]),
        "f": rb.check_dead_ball_attempts(c, [2024]),
        "g": rb.check_pickoff_row_ids(c, [2024]),
        "h": rb.check_metadata(c, [2024]),
    }


class TestAgainstTheBuilder:
    """The checks against the computor's own rebuild (``__new__`` bypass)."""

    def test_every_check_passes_on_the_first_rebuild(self):
        c = _db()
        _builder_game(c)
        _build(c)
        # Turn the pool back into what an old builder wrote: no pickoff rows,
        # no class.
        c.execute("DELETE FROM sim.steal_opportunity_pool WHERE is_pickoff_row")
        c.execute("UPDATE sim.steal_opportunity_pool SET pitch_class = NULL")
        rb.take_snapshot(c, [2024])
        rb.count_pickoff_rows(c, [2024])
        counts = rb.pickoff_summary(c, [2024])
        assert (counts[2024].fit, counts[2024].tag_groups, counts[2024].rows) == (3, 1, 2)
        # Autocommit here: DuckDB 1.1 (this host) refuses to delete and
        # re-insert a primary key in one transaction; the container's does not.
        _build(c)
        assert _all_checks(c, counts) == dict.fromkeys("abcdefgh", True)
        rows = c.execute(
            "SELECT pitch_id, target_base, outs, pickoff_out, pickoff_advancing, catcher_id "
            "FROM sim.steal_opportunity_pool WHERE is_pickoff_row ORDER BY pitch_id"
        ).fetchall()
        assert rows == [(-3, 3, 1, True, False, 902), (-2, 2, 0, True, True, 902)]

    def test_every_check_passes_on_a_rerun(self):
        c = _db()
        _builder_game(c)
        _build(c)
        rb.take_snapshot(c, [2024])  # the snapshot already holds the pickoff rows
        rb.count_pickoff_rows(c, [2024])
        counts = rb.pickoff_summary(c, [2024])
        _build(c)
        assert _all_checks(c, counts) == dict.fromkeys("abcdefgh", True)

    def test_a_rerun_after_a_reloaded_game_passes(self, capsys):
        """A game reload deletes and re-inserts its play records, so their
        BIGSERIAL ids change and the pickoff rows of the first build come back
        under new ids. Check a reads pitch rows only; check c proves the new
        pickoff rows against today's play records."""
        c = _db()
        _builder_game(c)
        _build(c)
        rb.take_snapshot(c, [2024])
        c.execute("UPDATE pg.raw.play_events SET id = id + 1000")  # the reload
        rb.count_pickoff_rows(c, [2024])
        counts = rb.pickoff_summary(c, [2024])
        _build(c)
        assert _all_checks(c, counts) == dict.fromkeys("abcdefgh", True)
        ids = c.execute(
            "SELECT pitch_id FROM sim.steal_opportunity_pool WHERE is_pickoff_row ORDER BY 1"
        ).fetchall()
        assert ids == [(-1003,), (-1002,)]
        assert "2 pickoff rows of an earlier run are not back" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The centres and the export's round-trip
# ---------------------------------------------------------------------------


class TestTheCentres:
    def test_the_centres_read_pitch_rows_only(self):
        # Four pitch rows (two attempts, one safe; one tagged pickoff) and two
        # pickoff rows. Recency 2, 2, 1, 1 on the pitch rows.
        got = rb.steal_centres(
            attempted=np.array([1, 1, 0, 0, 0, 0]),
            success=np.array([1, 0, 0, 0, 0, 0]),
            recency=np.array([2.0, 2.0, 1.0, 1.0, 9.0, 9.0]),
            pickoff_out=np.array([0, 0, 1, 0, 1, 0]),
            pickoff_error=np.array([0, 0, 0, 0, 0, 1]),
            pitch_class=np.array([1, 2, 1, 3, 0, 0]),
        )
        assert got["pitch_rows"] == 4 and got["pickoff_rows"] == 2
        assert got["att_rate"] == 0.5
        assert got["att_rate_rcy"] == pytest.approx(4.0 / 6.0)
        assert got["safe_share"] == 0.5
        assert got["pickoff_per_pitch_row"] == pytest.approx(3 / 4)

    def test_a_pool_without_classes_is_all_pitch_rows(self):
        got = rb.steal_centres(
            np.array([1, 0]), np.array([1, 0]), np.ones(2), np.zeros(2), np.zeros(2), None
        )
        assert got["pitch_rows"] == 2 and got["pickoff_rows"] == 0 and got["att_rate"] == 0.5

    def test_the_class_strings_map_to_codes(self):
        codes = rb.class_codes(["ball", "", "hit_by_pitch", "in_play", "nonsense", None])
        assert codes.tolist() == [1, 0, 6, 5, -1, 0]

    def test_the_band_centres_load(self):
        """The centres come from ``tests/acceptance/bands.py`` itself, not from
        the script's fallback copy (which holds the same 2026-09 values, so
        only the source and a fresh read of the file tell the two apart)."""
        path = REPO / "tests" / "acceptance" / "bands.py"
        centres, source = rb.band_centres()
        assert Path(source) == path
        spec = importlib.util.spec_from_file_location("_sim554_test_bands", path)
        assert spec is not None and spec.loader is not None
        bands = importlib.util.module_from_spec(spec)
        sys.modules["_sim554_test_bands"] = bands  # the dataclasses resolve their module
        spec.loader.exec_module(bands)
        assert set(centres) == set(rb.FALLBACK_CENTRES)
        assert centres == {k: float(bands.POOL_REFERENCES[k].centre) for k in rb.FALLBACK_CENTRES}


def _export_like(c, pool_dir: Path, target: str) -> None:
    """Write one target's files the way the SIM-554 export does (pitch-id order)."""
    pool_dir.mkdir(parents=True, exist_ok=True)
    w = f"target_base = {int(target)}"
    d = c.execute(
        "SELECT count_balls, count_strikes, outs, score_diff FROM sim.steal_opportunity_pool "
        f"WHERE {w} ORDER BY pitch_id"
    ).fetchnumpy()
    np.save(pool_dir / f"{target}.sit.npy", np.stack([d[k].astype(np.float32) for k in d], axis=1))
    c.execute(
        "COPY (SELECT pitch_id, runner_id, pitcher_id, catcher_id, season, attempted, success, "
        "recency_weight, pickoff_out, pickoff_advancing, pickoff_error, "
        "COALESCE(pitch_class, '') AS pitch_class, COALESCE(is_pickoff_row, FALSE) "
        f"AS is_pickoff_row FROM sim.steal_opportunity_pool WHERE {w} ORDER BY pitch_id) "
        f"TO '{(pool_dir / f'{target}.meta.parquet').as_posix()}' (FORMAT parquet)"
    )


class TestTheRoundTrip:
    def _pool(self, tmp_path):
        c = _db()
        _steal_row(c, 10, 1, 1, balls=1)
        _steal_row(c, 10, 1, 2, balls=2, attempted=True, success=True)
        _pickoff_row(c, 5, 10, 2)
        d = tmp_path / "steal_pool"
        _export_like(c, d, "2")
        return c, d

    def test_a_faithful_export_passes(self, tmp_path):
        c, d = self._pool(tmp_path)
        assert rb.roundtrip_target(c, str(d), "2", [2024], 3, rb.POOL)

    def test_a_label_unlike_duckdbs_fails(self, tmp_path):
        c, d = self._pool(tmp_path)
        c.execute("UPDATE sim.steal_opportunity_pool SET success = FALSE")
        assert not rb.roundtrip_target(c, str(d), "2", [2024], 3, rb.POOL)

    def test_a_count_unlike_duckdbs_fails(self, tmp_path):
        c, d = self._pool(tmp_path)
        c.execute("UPDATE sim.steal_opportunity_pool SET count_strikes = 2 WHERE pitch_id > 0")
        assert not rb.roundtrip_target(c, str(d), "2", [2024], 3, rb.POOL)

    def test_an_export_without_pitch_ids_fails(self, tmp_path):
        c, d = self._pool(tmp_path)
        c.execute(
            "COPY (SELECT runner_id, season, attempted FROM sim.steal_opportunity_pool) "
            f"TO '{(d / '2.meta.parquet').as_posix()}' (FORMAT parquet)"
        )
        assert not rb.roundtrip_target(c, str(d), "2", [2024], 3, rb.POOL)

    def test_the_rest_of_the_bundle_is_hashed_and_compared(self, tmp_path):
        art = tmp_path / "bundle"
        (art / "pitch_pool").mkdir(parents=True)
        (art / "steal_pool").mkdir()
        (art / "pitch_pool" / "L.sit.npy").write_bytes(b"abc")
        (art / "steal_pool" / "2.sit.npy").write_bytes(b"abc")
        before = rb.bundle_hashes(str(art))
        assert list(before) == ["pitch_pool/L.sit.npy"]
        (art / "steal_pool" / "2.sit.npy").write_bytes(b"new")  # the export's own write
        assert rb.compare_hashes(before, rb.bundle_hashes(str(art))) == []
        (art / "pitch_pool" / "L.sit.npy").write_bytes(b"abd")
        assert rb.compare_hashes(before, rb.bundle_hashes(str(art))) == [
            "changed: pitch_pool/L.sit.npy"
        ]


@pytest.fixture
def quiet_logging(monkeypatch):
    """``main`` points the root logger at stdout (force=True); a test keeps pytest's."""
    monkeypatch.setattr(rb.logging, "basicConfig", lambda **_kw: None)


def test_the_rebuild_refuses_a_missing_database(tmp_path, builder_554, quiet_logging):
    missing = tmp_path / "nope.duckdb"
    assert rb.main(["--duckdb-path", str(missing), "--art-dir", str(tmp_path)]) == rb.EXIT_STOPPED
    assert not os.path.exists(missing)  # never created


def _file_databases(tmp_path: Path) -> tuple[str, str]:
    """A DuckDB file holding the ``raw`` tables (attached as ``pg`` in place of
    Postgres) and the main DuckDB file, its steal pool as an old builder left
    it: no pickoff rows, no class, and no migration-0031 columns."""
    pg_path, main_path = str(tmp_path / "pg.duckdb"), str(tmp_path / "main.duckdb")
    c = _db()
    _builder_game(c)
    for s in (2023, 2025, 2026):  # the window's other seasons: one pitch, no runner
        c.execute(
            "INSERT INTO pg.raw.pitches (game_pk, at_bat_number, pitch_number, game_date, "
            "season, pitcher, fielder_2, inning, inning_topbot, outs, balls, strikes, bat_score, "
            "fld_score, data_quality_flag) VALUES (?, 1, 1, DATE '2024-06-01', ?, 901, 902, 1, "
            "'Top', 0, 0, 0, 0, 0, FALSE)",
            [s, s],
        )
        c.execute(
            "INSERT INTO sim.pitch_pool VALUES (?, ?, 1, 1, ?, 'ball')", [_pitch_id(s, 1, 1), s, s]
        )
    for s in WINDOW:
        c.execute(
            "INSERT INTO sim.pool_build_metadata (pool_name, season, row_count, builder_version) "
            "VALUES ('pitch_pool', ?, 1, 'sim553.1')",
            [s],
        )
    comp = rb.ppc.PlayerProfileComputor.__new__(rb.ppc.PlayerProfileComputor)
    comp._conn = c
    comp._build_steal_opportunity_pool(WINDOW, incremental=False)
    c.execute("UPDATE sim.pool_build_metadata SET builder_version = 'sim553.1'")
    c.execute("DELETE FROM sim.steal_opportunity_pool WHERE is_pickoff_row")
    c.execute(f"ATTACH '{Path(pg_path).as_posix()}' AS pgfile")
    c.execute(f"ATTACH '{Path(main_path).as_posix()}' AS mainfile")
    c.execute("CREATE SCHEMA pgfile.raw")
    for t in ("pitches", "play_events"):
        c.execute(f"CREATE TABLE pgfile.raw.{t} AS SELECT * FROM pg.raw.{t}")
    c.execute("CREATE SCHEMA mainfile.sim")
    c.execute("CREATE TABLE mainfile.sim.pitch_pool AS SELECT * FROM sim.pitch_pool")
    c.execute(  # the key the builder's INSERT OR REPLACE needs
        "CREATE TABLE mainfile.sim.pool_build_metadata (pool_name VARCHAR, season SMALLINT, "
        "row_count BIGINT, source_max_game_date DATE, source_row_count BIGINT, "
        "recency_ref_season SMALLINT, builder_version VARCHAR, "
        "built_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (pool_name, season))"
    )
    c.execute("INSERT INTO mainfile.sim.pool_build_metadata SELECT * FROM sim.pool_build_metadata")
    # The live table's primary key is left out: DuckDB 1.1 (this host) refuses
    # to delete and re-insert a key in one transaction.
    c.execute(
        _STEAL_DDL.format(extra="")
        .replace("CREATE TABLE sim.", "CREATE TABLE mainfile.sim.")
        .replace(",\n    PRIMARY KEY (pitch_id)", "")
    )
    c.execute(
        "INSERT INTO mainfile.sim.steal_opportunity_pool SELECT * EXCLUDE (pitch_class, "
        "is_pickoff_row) FROM sim.steal_opportunity_pool"
    )
    c.close()
    return pg_path, main_path


class TestTheWholeRun:
    """``main`` end to end on files: the pre-flight, the migration, the
    rebuild and its checks, the copy, the export, the round-trip."""

    def test_the_run_completes_and_leaves_the_rest_of_the_bundle_alone(
        self, tmp_path, builder_554, quiet_logging, monkeypatch, capsys
    ):
        pg_path, main_path = _file_databases(tmp_path)
        monkeypatch.setattr(
            rb, "attach_pg", lambda con: con.execute(f"ATTACH '{pg_path}' AS pg (READ_ONLY)")
        )
        art = tmp_path / "engine_artifacts"
        _write_steal_pool(art / "steal_pool", {"2": 2, "3": 1})  # the old export's counts
        (art / "pitch_pool").mkdir()
        (art / "pitch_pool" / "L.sit.npy").write_bytes(b"untouched")
        argv = ["--duckdb-path", main_path, "--art-dir", str(art)]
        assert rb.main(argv) == rb.EXIT_STOPPED  # the migration is not applied
        assert rb.main([*argv, "--apply-migration"]) == rb.EXIT_OK
        out = capsys.readouterr().out
        assert "every check passed: COMMIT" in out and "REBUILD COMPLETE" in out
        backup = str(art) + rb.BACKUP_SUFFIX
        assert os.path.isfile(os.path.join(backup, "manifest.json"))
        assert not os.path.exists(rb.export_marker(backup))
        assert (art / "pitch_pool" / "L.sit.npy").read_bytes() == b"untouched"
        with open(art / "steal_pool" / "manifest.json", encoding="utf-8") as fh:
            assert json.load(fh)["counts"] == {"2": 3, "3": 2}  # one pickoff row each
        # A re-run keeps the copy and repeats the rebuild with nothing new.
        assert rb.main(argv) == rb.EXIT_OK
        assert "kept " + backup in capsys.readouterr().out

    def test_a_failed_check_rolls_the_migration_back_with_the_rows(
        self, tmp_path, builder_554, quiet_logging, monkeypatch, capsys
    ):
        """--apply-migration adds the two columns INSIDE the rebuild's
        transaction, so a failed check leaves the table in its old shape: the
        old code's positional INSERT keeps working until the merge."""
        pg_path, main_path = _file_databases(tmp_path)
        monkeypatch.setattr(
            rb, "attach_pg", lambda con: con.execute(f"ATTACH '{pg_path}' AS pg (READ_ONLY)")
        )
        monkeypatch.setattr(rb, "check_dead_ball_attempts", lambda con, seasons: False)
        art = tmp_path / "engine_artifacts"
        _write_steal_pool(art / "steal_pool", {"2": 2, "3": 1})
        argv = ["--duckdb-path", main_path, "--art-dir", str(art), "--apply-migration"]
        assert rb.main(argv) == rb.EXIT_STOPPED
        out = capsys.readouterr().out
        assert "inside the transaction" in out
        assert "rolled back with the rows: the table has its old shape" in out
        assert "REBUILD FAILED at check f" in out
        con = duckdb.connect(main_path, read_only=True)
        try:
            cols = {
                r[0]
                for r in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'sim' AND table_name = 'steal_opportunity_pool'"
                ).fetchall()
            }
        finally:
            con.close()
        assert "pitch_class" not in cols and "is_pickoff_row" not in cols

    @pytest.mark.parametrize("failure", ["a failed check", "a raise"])
    def test_a_migration_applied_before_the_run_stays_with_a_warning(
        self, tmp_path, builder_554, quiet_logging, monkeypatch, capsys, failure
    ):
        """0031 applied by hand before the run is not the run's to roll back.
        On a failed check (exit 2) and on a raise (exit 1) the rollback keeps
        the two columns, and the log says so."""
        pg_path, main_path = _file_databases(tmp_path)
        monkeypatch.setattr(
            rb, "attach_pg", lambda con: con.execute(f"ATTACH '{pg_path}' AS pg (READ_ONLY)")
        )
        con = duckdb.connect(main_path)
        try:
            rb.apply_migration(con)  # the separate step the run book forbids
        finally:
            con.close()

        def raises(_con, _seasons):
            raise RuntimeError("check f raised")

        check_f = raises if failure == "a raise" else (lambda _con, _seasons: False)
        monkeypatch.setattr(rb, "check_dead_ball_attempts", check_f)
        art = tmp_path / "engine_artifacts"
        _write_steal_pool(art / "steal_pool", {"2": 2, "3": 1})
        argv = ["--duckdb-path", main_path, "--art-dir", str(art), "--apply-migration"]
        want = rb.EXIT_ERROR if failure == "a raise" else rb.EXIT_STOPPED
        assert rb.main(argv) == want
        out = capsys.readouterr().out
        assert "migration 0031: applied before this run" in out
        assert "inside the transaction" not in out  # the run applied nothing
        assert "were on the table before this run, so the rollback keeps them" in out
        con = duckdb.connect(main_path, read_only=True)
        try:
            cols = {
                r[0]
                for r in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'sim' AND table_name = 'steal_opportunity_pool'"
                ).fetchall()
            }
        finally:
            con.close()
        assert {"pitch_class", "is_pickoff_row"} <= cols


# ===========================================================================
# The census
# ===========================================================================

census = _load("sim554_running_game_census")

from simulation.game_state import NO_PITCH, PlayResult  # noqa: E402


def _snap(
    *,
    first=None,
    second=None,
    third=None,
    outs=0,
    balls=0,
    strikes=0,
    inning=1,
    half="TOP",
    batter=7,
):
    return census.Snap(
        first=first,
        second=second,
        third=third,
        outs=outs,
        balls=balls,
        strikes=strikes,
        inning=inning,
        half=half,
        batter=batter,
    )


class TestTheCensusStep:
    def test_a_thrown_pitch_with_a_stealable_runner_is_an_opportunity(self):
        rec = census.Recorder()
        rec.start_game_sim()
        pre = _snap(first=11)
        res = PlayResult(pitch_outcome="ball", steal_attempted=True, steal_outcome="safe")
        rec.observe(pre, res, pre)
        assert rec.opp["2"]["ball"] == 1 and rec.att["2"]["ball"] == 1 and rec.safe["2"] == 1
        rec.observe(_snap(first=11, second=12), PlayResult(pitch_outcome="foul"), pre)
        assert rec.opp["3"]["foul"] == 1 and rec.att["3"]["foul"] == 0  # the lead runner
        rec.observe(_snap(first=11, second=12, third=13), PlayResult(pitch_outcome="ball"), pre)
        rec.observe(_snap(), PlayResult(pitch_outcome="in_play"), pre)
        assert sum(rec.opp["2"].values()) + sum(rec.opp["3"].values()) == 2

    def test_a_caught_steal_counts_an_attempt_and_no_safe(self):
        rec = census.Recorder()
        rec.start_game_sim()
        res = PlayResult(
            pitch_outcome="called_strike", steal_attempted=True, steal_outcome="caught"
        )
        rec.observe(_snap(second=12), res, _snap(second=None, outs=1))
        assert rec.att["3"]["called_strike"] == 1 and rec.safe["3"] == 0

    def test_an_intentional_walk_is_skipped(self):
        rec = census.Recorder()
        rec.start_game_sim()
        res = PlayResult(pitch_outcome="ball", event="intentional_walk", pa_terminal=True)
        rec.observe(_snap(first=11), res, _snap(first=7, second=11))
        assert rec.ibb == 1 and sum(rec.opp["2"].values()) == 0

    def test_a_no_pitch_third_out_and_the_same_batter_next_time(self):
        rec = census.Recorder()
        rec.start_game_sim()
        pre = _snap(first=11, outs=2, balls=3, strikes=2, batter=7)
        res = PlayResult(pitch_outcome=NO_PITCH, pickoff_out=True, pa_voided="pickoff_third_out")
        rec.observe(pre, res, _snap(inning=1, half="BOTTOM", batter=20))
        assert rec.no_pitch == {"n": 1, "half_rolled": 1, "labelled": 1}
        assert rec.pickoff_third_outs == 1 and rec.pickoff_third_outs_pa_credited == 0
        assert rec.pickoffs_by_count == {"3-2": 1}
        assert sum(rec.opp["2"].values()) == 0  # no pitch, no opportunity
        # The other side bats, then the same batter leads off at 0-0.
        rec.observe(_snap(half="BOTTOM", batter=20), PlayResult(pitch_outcome="ball"), pre)
        rec.observe(_snap(inning=2, batter=7), PlayResult(pitch_outcome="ball"), pre)
        assert rec.leadoff == {"same": 1, "other": 0, "unchecked": 0}

    def test_a_different_leadoff_batter_is_counted_and_an_unread_one_is_unchecked(self):
        rec = census.Recorder()
        rec.start_game_sim()
        res = PlayResult(pitch_outcome=NO_PITCH, pickoff_out=True, pa_voided="pickoff_third_out")
        rec.observe(_snap(first=11, outs=2, batter=7), res, _snap(half="BOTTOM"))
        rec.observe(_snap(inning=2, batter=8), PlayResult(pitch_outcome="ball"), _snap())
        assert rec.leadoff["other"] == 1
        rec.observe(_snap(first=11, outs=2, batter=8), res, _snap(half="BOTTOM"))
        rec.end_game_sim([], 1.0)  # the game ended before that side batted again
        assert rec.leadoff["unchecked"] == 1

    def test_the_old_order_credits_a_pickoff_third_out_as_a_plate_appearance(self):
        rec = census.Recorder()
        rec.start_game_sim()
        res = PlayResult(
            pitch_outcome="called_strike", pa_terminal=True, event="strikeout", pickoff_out=True
        )
        rec.observe(_snap(first=11, outs=2, strikes=2), res, _snap(half="BOTTOM"))
        assert rec.pickoff_third_outs == 1 and rec.pickoff_third_outs_pa_credited == 1
        assert rec.opp["2"]["called_strike"] == 1  # a pitch was thrown

    def test_a_voided_steal_is_counted_by_reason(self):
        rec = census.Recorder()
        rec.start_game_sim()
        res = PlayResult(
            pitch_outcome="called_strike",
            pa_terminal=True,
            event="strikeout",
            steal_voided="third_out_first",
        )
        rec.observe(_snap(first=11, outs=2, strikes=2), res, _snap(half="BOTTOM"))
        assert rec.voided == {"third_out_first": 1}

    def test_a_dropped_third_strike_reach_and_the_runners_it_moved(self):
        rec = census.Recorder()
        rec.start_game_sim()
        # A runner on second, first open: unforced. He goes to third.
        res = PlayResult(
            pitch_outcome="swinging_strike",
            pa_terminal=True,
            event="strikeout",
            baserunner_advances={7: 1, 12: 3},
        )
        rec.observe(_snap(second=12, outs=1, strikes=2), res, _snap(first=7, third=12, outs=1))
        # Bases loaded, two outs: every runner is forced; the one on third scores.
        res2 = PlayResult(
            pitch_outcome="swinging_strike",
            pa_terminal=True,
            event="strikeout",
            baserunner_advances={8: 1, 11: 2, 12: 3, 13: 0},
        )
        rec.observe(
            _snap(first=11, second=12, third=13, outs=2, strikes=2, batter=8),
            res2,
            _snap(first=8, second=11, third=12, outs=2),
        )
        # A plain strikeout reaches nobody.
        rec.observe(
            _snap(first=11, strikes=2),
            PlayResult(pitch_outcome="swinging_strike", pa_terminal=True, event="strikeout"),
            _snap(first=11, outs=1),
        )
        assert rec.d3k == {
            "reaches": 2,
            "runners_on": 4,
            "runners_moved": 4,
            "unforced_chances": 1,
            "unforced_moved": 1,
            "with_running_play": 0,
        }

    def test_a_reach_with_a_steal_on_the_pitch_is_set_aside(self):
        rec = census.Recorder()
        rec.start_game_sim()
        res = PlayResult(
            pitch_outcome="swinging_strike",
            pa_terminal=True,
            event="strikeout",
            steal_attempted=True,
            steal_outcome="safe",
            baserunner_advances={7: 1, 11: 2},
        )
        rec.observe(_snap(first=11, strikes=2), res, _snap(first=7, second=11))
        assert rec.d3k["reaches"] == 1 and rec.d3k["with_running_play"] == 1
        assert rec.d3k["runners_on"] == 0

    def test_an_expected_attempt_lands_on_the_class_of_the_pitch(self):
        rec = census.Recorder()
        rec.start_game_sim()
        rec.stage_expected_attempt(2, 0.25)
        rec.observe(_snap(first=11), PlayResult(pitch_outcome="foul"), _snap(first=11))
        rec.stage_expected_attempt(2, 0.5)  # a no-pitch step drops it
        rec.observe(
            _snap(first=11, outs=2),
            PlayResult(pitch_outcome=NO_PITCH, pickoff_out=True),
            _snap(half="BOTTOM"),
        )
        assert rec.exp_attempt["2"]["foul"] == 0.25
        assert sum(rec.exp_attempt["2"].values()) == 0.25

    def test_the_box_and_the_seconds_close_a_game_sim(self):
        rec = census.Recorder()
        rec.start_game_sim()
        res = PlayResult(pitch_outcome="ball", steal_attempted=True, steal_outcome="safe")
        rec.observe(_snap(first=11), res, _snap(second=11))

        class Line:
            def __init__(self, sb, cs):
                self.sb, self.cs = sb, cs

        rec.end_game_sim([Line(1, 0), Line(0, 1), Line(2, 0)], 0.9)
        assert rec.per_sim == [
            {
                "sb": 3,
                "cs": 1,
                "pickoffs": 0,
                "opp2": 1,
                "att2": 1,
                "opp3": 0,
                "att3": 0,
                "seconds": 0.9,
            }
        ]


class _Pool:
    def __init__(self):
        self.pickoff_out = np.array([1, 0, 0, 0], dtype=np.int8)
        self.pickoff_error = np.array([0, 0, 1, 0], dtype=np.int8)
        self.attempted = np.array([0, 1, 0, 1], dtype=np.int8)


class _FakeSampler:
    """The sampler seams the census wraps, with fixed weights."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.pool = _Pool()

    def steal_weights(self, *a, rows=None, aggression=1.0, **kw):
        rows = np.arange(4) if rows is None else np.asarray(rows)
        w = np.array([1.0, 1.0, 2.0, 0.0])[rows] * np.where(
            self.pool.attempted[rows] > 0, aggression, 1.0
        )
        return self.pool, rows, w

    def _steal_meta(self, target):
        cells = {(0, 0, 0): np.arange(4)}
        return {
            "cells": cells,
            "pickoff_cells": cells,
            "pickoff_live": {(0, 0, 0)},
            "class_cells": {(0, 0, 0, 1): np.array([1, 2])},
        }

    def steal_draw(self, *a, **kw):
        self.calls.append(("steal_draw", kw))
        return (False, False, True, True, False)

    def pickoff_draw(self, *a, **kw):
        self.calls.append(("pickoff_draw", kw))
        return (True, False, False)


class TestTheCensusSeams:
    def test_the_expected_shares_read_the_weights(self):
        fp = _FakeSampler()
        kw = {"outs": 0, "balls": 0, "strikes": 0, "score_diff": 0}
        got = census.expected_shares(fp, (2, "r", "p", None), kw, rows=None, aggression=1.0)
        assert got == (0.75, 0.25)

    def test_the_wrappers_go_on_once_and_feed_the_recorder(self):
        fp = _FakeSampler()
        rec = census.Recorder()
        holder = {"rec": rec}
        census.install_seams(fp, holder)
        census.install_seams(fp, holder)  # the SIM-514 stacking trap
        kw = {"outs": 0, "balls": 0, "strikes": 0, "score_diff": 0}
        fp.steal_draw(2, "r", "p", None, aggression=1.0, **kw)  # the old pre-pitch draw
        fp.pickoff_draw(2, "r", "p", None, **kw)  # the new order's pickoff draw
        fp.steal_draw(2, "r", "p", None, aggression=1.0, pitch_class="ball", **kw)
        assert [name for name, _ in fp.calls] == ["steal_draw", "pickoff_draw", "steal_draw"]
        assert "pitch_class" not in fp.calls[0][1] and fp.calls[2][1]["pitch_class"] == "ball"
        assert rec.exp_pickoff["2"] == [1.5, 2]  # 0.75 at each of the two pickoff reads
        assert rec.seam_kind == {"out": 1, "out_advancing": 1, "error": 0}
        # The class group holds rows 1 and 2: one attempted, of weights 1 and 2.
        assert rec._staged_attempt == (2, pytest.approx(1.0 / 3.0))


def _summary(
    *,
    arm,
    opp=(1000, 300),
    att=(21.0, 1.5),
    misplaced=0.0,
    exp_po=0.0010,
    third_out_first=0,
    se=0.002,
):
    """A planted arm summary in the shape ``census.summarize`` returns."""
    return {
        "arm": arm,
        "att_per_opp": {"2": (att[0] / opp[0], se), "3": (att[1] / opp[1], se / 2)},
        "n_att": int(att[0] + att[1]),
        "misplaced_share": misplaced,
        "exp_pickoff_per_draw": exp_po,
        "third_out_first": third_out_first,
    }


class TestTheStopRules:
    def _stops(self, off, on):
        return [r.name for r in census.stop_rules(off, on) if not r.ok]

    def test_a_clean_pair_passes_every_rule(self):
        off = _summary(arm=0, misplaced=0.36, exp_po=0.0010)
        on = _summary(arm=1, misplaced=0.01, exp_po=0.00128)
        assert self._stops(off, on) == []

    def test_a_misplaced_share_above_five_percent_stops(self):
        off = _summary(arm=0, exp_po=0.0010)
        on = _summary(arm=1, misplaced=0.06, exp_po=0.00128)
        assert self._stops(off, on) == ["misplaced"]

    def test_a_pickoff_ratio_outside_the_window_stops(self):
        off = _summary(arm=0, exp_po=0.0010)
        assert self._stops(off, _summary(arm=1, exp_po=0.0011)) == ["pickoff_ratio"]
        assert self._stops(off, _summary(arm=1, exp_po=0.0015)) == ["pickoff_ratio"]

    def test_an_attempt_volume_moved_beyond_two_standard_errors_stops(self):
        off = _summary(arm=0, exp_po=0.0010, se=0.002)
        # 0.021 -> 0.027: 0.006 against an unpaired SE of 0.0028.
        on = _summary(arm=1, exp_po=0.00128, att=(27.0, 1.5), se=0.002)
        assert self._stops(off, on) == ["volume_2"]

    def test_a_third_out_first_on_the_new_arm_stops(self):
        off = _summary(arm=0, exp_po=0.0010, third_out_first=3)
        on = _summary(arm=1, exp_po=0.00128, third_out_first=1)
        assert self._stops(off, on) == ["third_out_first"]

    def test_no_attempt_on_the_new_arm_stops(self):
        off = _summary(arm=0, exp_po=0.0010)
        on = _summary(arm=1, exp_po=0.00128, att=(0.0, 0.0))
        assert "misplaced" in self._stops(off, on)

    def test_the_ratio_estimate_and_its_standard_error(self):
        rate, se = census.ratio_se([(1, 10), (3, 10), (2, 10)])
        assert rate == pytest.approx(0.2)
        # The residuals a - r*o are -1, 1, 0: sqrt(2 * 3/2) / 30.
        assert se == pytest.approx(np.sqrt(3.0) / 30.0)


class TestThePoolReference:
    def test_the_pool_reference_reads_pitch_rows_by_recency(self):
        class P:  # one target's pool: five pitch rows and one pickoff row
            attempted = np.array([1, 1, 0, 0, 1, 0])
            success = np.array([1, 0, 0, 0, 1, 0])
            recency = np.array([2.0, 1.0, 1.0, 1.0, 1.0, 5.0])
            pickoff_out = np.array([0, 0, 1, 0, 0, 1])
            pickoff_error = np.zeros(6)
            pitch_class = np.array([1, 1, 2, 4, 5, 0])

        ref = census.pool_reference({"2": P()})
        t = ref["targets"]["2"]
        assert (t["pitch_rows"], t["pickoff_rows"], t["tags"], t["pickoff_row_outcomes"]) == (
            5,
            1,
            1,
            1,
        )
        assert t["att_rate_rcy"] == pytest.approx(4.0 / 6.0)  # (2 + 1 + 1) / 6
        assert t["safe_share"] == pytest.approx(3.0 / 4.0)  # (2 + 1) / (2 + 1 + 1)
        assert ref["att_share"]["ball"] == pytest.approx(0.75)  # weights 2 + 1 of 4
        assert ref["att_share"]["in_play"] == pytest.approx(0.25)
        assert ref["att_share_raw"]["ball"] == pytest.approx(2.0 / 3.0)
        assert ref["pickoff_ratio"] == pytest.approx(2.0)  # (1 tag + 1 row) / 1 tag


class TestTheCensusSummary:
    def test_the_summary_reads_shares_rates_and_the_misplaced_attempts(self):
        rec = census.Recorder()
        for _ in range(2):
            rec.start_game_sim()
            for cls, att in (
                ("ball", True),
                ("ball", False),
                ("foul", True),
                ("called_strike", False),
            ):
                res = PlayResult(
                    pitch_outcome=cls, steal_attempted=att, steal_outcome="safe" if att else None
                )
                rec.observe(_snap(first=11), res, _snap(first=11))
            rec.add_expected_pickoff(2, 0.002)
            rec.end_game_sim([], 1.0)
        s = census.summarize(rec.to_jsonable() | {"arm": 1, "game_pks": [1], "iters": 2})
        assert s["game_sims"] == 2
        assert s["att_per_opp"]["2"][0] == pytest.approx(0.5)
        assert s["misplaced_share"] == pytest.approx(0.5)
        assert s["att_share"]["ball"] == pytest.approx(0.5)
        assert s["exp_pickoff_per_draw"] == pytest.approx(0.002)
        assert s["safe_share"]["2"] == pytest.approx(1.0)

    def test_the_report_names_every_rule_and_a_verdict(self):
        rec = census.Recorder()
        rec.start_game_sim()
        rec.end_game_sim([], 1.0)
        arm = rec.to_jsonable() | {"game_pks": [1], "iters": 1, "pool": {}}
        lines, stops = census.report_lines(arm | {"arm": 0}, arm | {"arm": 1})
        text = "\n".join(lines)
        for name in ("misplaced", "pickoff_ratio", "volume_2", "volume_3", "third_out_first"):
            assert name in text
        assert "STOP" in text and stops  # an empty census stops: no attempt on either arm
