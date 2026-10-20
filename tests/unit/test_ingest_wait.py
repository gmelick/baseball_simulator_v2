"""The nightly rebuild waits for the finals load.

The scheduler can fire the finals load and the rebuild in the same second.
``scripts/with_mark.sh`` marks an ingest job while it runs and records
its last exit; ``scripts/wait_for_ingest.sh`` holds the rebuild until the finals
load has run today, and runs it when it has not.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None or os.name == "nt",
    reason="POSIX shell scripts (CI and the container run them)",
)


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _env(marks: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "INGEST_MARK_DIR": str(marks),
            "INGEST_GRACE_S": "0",
            "INGEST_POLL_S": "1",
            "INGEST_WAIT_S": "30",
        }
    )
    env.update(extra)
    return env


def _run(script: str, *args: str, env: dict[str, str], timeout: float = 60):
    return subprocess.run(
        ["sh", str(SCRIPTS / script), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# --- with_mark.sh -------------------------------------------------------------


def test_a_marked_job_records_its_exit_and_clears_its_mark(tmp_path):
    marks = tmp_path / "marks"
    ok = _run("with_mark.sh", "nightly-finals", "true", env=_env(marks))
    assert ok.returncode == 0
    assert not (marks / "nightly-finals.running").exists()
    date, code, _stamp = (marks / "nightly-finals.last").read_text().split()
    assert date == _today() and code == "0"

    bad = _run("with_mark.sh", "nightly-finals", "sh", "-c", "exit 3", env=_env(marks))
    assert bad.returncode == 3  # the job's own exit code passes through
    assert (marks / "nightly-finals.last").read_text().split()[1] == "3"


def test_the_mark_exists_while_the_job_runs(tmp_path):
    marks = tmp_path / "marks"
    probe = tmp_path / "seen"
    cmd = f"test -f {marks}/nightly-finals.running && touch {probe}"
    _run("with_mark.sh", "nightly-finals", "sh", "-c", cmd, env=_env(marks))
    assert probe.exists()


def test_an_unwritable_mark_folder_still_runs_the_job(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")  # a FILE where the folder should be
    probe = tmp_path / "ran"
    res = _run("with_mark.sh", "x", "touch", str(probe), env=_env(blocker / "marks"))
    assert res.returncode == 0
    assert probe.exists()


# --- wait_for_ingest.sh ----------------------------------------------------------


def test_a_finals_load_done_today_lets_the_rebuild_go(tmp_path):
    marks = tmp_path / "marks"
    marks.mkdir()
    (marks / "nightly-finals.last").write_text(f"{_today()} 0 x\n")
    ran = tmp_path / "ran"
    res = _run("wait_for_ingest.sh", env=_env(marks, INGEST_FINALS_CMD=f"touch {ran}"))
    assert res.returncode == 0
    assert not ran.exists()  # nothing re-ran
    assert "ran today" in res.stdout


def test_no_finals_load_today_runs_it_first(tmp_path):
    marks = tmp_path / "marks"
    marks.mkdir()
    (marks / "nightly-finals.last").write_text("2020-01-01 0 x\n")
    ran = tmp_path / "ran"
    res = _run("wait_for_ingest.sh", env=_env(marks, INGEST_FINALS_CMD=f"touch {ran}"))
    assert res.returncode == 0
    assert ran.exists()


def test_the_wait_holds_while_the_finals_load_runs(tmp_path):
    marks = tmp_path / "marks"
    marks.mkdir()
    running = marks / "nightly-finals.running"
    running.write_text(str(int(time.time())))
    proc = subprocess.Popen(
        ["sh", str(SCRIPTS / "wait_for_ingest.sh")],
        env=_env(marks, INGEST_FINALS_CMD="false"),
        stdout=subprocess.PIPE,
        text=True,
    )
    time.sleep(2.5)
    assert proc.poll() is None  # still waiting
    running.unlink()
    (marks / "nightly-finals.last").write_text(f"{_today()} 0 x\n")
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0
    assert "ran today" in out


def test_a_stale_mark_does_not_hold_the_rebuild(tmp_path):
    marks = tmp_path / "marks"
    marks.mkdir()
    (marks / "weekly-refresh.running").write_text(str(int(time.time()) - 7 * 3600))
    (marks / "nightly-finals.last").write_text(f"{_today()} 0 x\n")
    res = _run("wait_for_ingest.sh", env=_env(marks), timeout=20)
    assert res.returncode == 0
    assert "an ingest job is running" not in res.stdout
    assert "ran today" in res.stdout


def test_the_wait_gives_up_and_never_fails_the_rebuild(tmp_path):
    marks = tmp_path / "marks"
    marks.mkdir()
    (marks / "nightly-finals.running").write_text(str(int(time.time())))
    res = _run("wait_for_ingest.sh", env=_env(marks, INGEST_WAIT_S="2"), timeout=30)
    assert res.returncode == 0
    assert "the rebuild goes on" in res.stdout


def test_a_failing_finals_load_does_not_fail_the_rebuild(tmp_path):
    marks = tmp_path / "marks"
    marks.mkdir()
    res = _run("wait_for_ingest.sh", env=_env(marks, INGEST_FINALS_CMD="false"))
    assert res.returncode == 0
    assert "exited non-zero" in res.stdout


def test_the_rebuild_script_waits_before_its_first_step():
    text = (SCRIPTS / "nightly_rebuild.sh").read_text(encoding="utf-8")
    assert text.index("wait_for_ingest.sh") < text.index("player_profile_computor")
