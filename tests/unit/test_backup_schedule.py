"""The scheduled backups of the three data stores: the wiring.

The scheduler (deploy/ofelia/config.ini) runs two backup jobs at 10:00 UTC,
after the nightly rebuild. The Postgres job runs inside the db container as
the postgres user; the data job backs up the DuckDB file and the bundle
inside the jobs container. The compose file mounts the backup folder into
the db, jobs and app containers. These tests read the files and start no
container. Two of them run a Makefile piece through sh or make, when the
test host has them (the app image does).

The off-site copy is a third job at 10:30 UTC in the jobs container.
It copies the newest sets from /backups to /offsite, the host folder
OFFSITE_BACKUP_DIR (a OneDrive folder). The jobs container mounts that folder
writable; the app mounts it read-only for the off-site age gauge.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
OFELIA = ROOT / "deploy" / "ofelia" / "config.ini"
MAKEFILE = ROOT / "Makefile"

BACKUP_SOURCE = "${BACKUP_DIR:-./backups}"
OFFSITE_SOURCE = "${OFFSITE_BACKUP_DIR:-./backups_offsite}"
DB_CONTAINER = "baseball_simulator_v2-db-1"
JOBS_CONTAINER = "baseball_simulator_v2-jobs-1"
DATA_COMMAND = (
    "sh /app/scripts/with_mark.sh nightly-backup sh /app/scripts/with_retry.sh 2 "
    "python -m pipeline.backup.data_stores"
)
OFFSITE_COMMAND = (
    "sh /app/scripts/with_mark.sh nightly-backup-offsite sh /app/scripts/with_retry.sh 2 "
    "python -m pipeline.backup.offsite"
)
MARK_DIR = "/data/ingest_marks"
GUARD_PREFIX = 'docker compose exec -T jobs sh -c "'


def _section(job: str) -> str:
    """Return the body of one job-exec block of the scheduler INI."""
    text = OFELIA.read_text(encoding="utf-8")
    header = f'[job-exec "{job}"]'
    assert header in text, job
    return text.split(header, 1)[1].split("\n[", 1)[0]


def _lines(section: str) -> list[str]:
    """Return the setting lines of a block, without comments or blanks."""
    return [
        line.strip()
        for line in section.splitlines()
        if line.strip() and not line.lstrip().startswith(";")
    ]


def _services() -> dict[str, Any]:
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = data["services"]
    assert isinstance(services, dict)
    return services


def _volumes(service: str) -> list[str]:
    return [str(v) for v in _services()[service]["volumes"]]


# ---------------------------------------------------------------------------
# The scheduler INI
# ---------------------------------------------------------------------------


def test_the_postgres_backup_runs_in_the_db_container_as_postgres() -> None:
    lines = _lines(_section("nightly-backup-postgres"))
    assert "schedule = 0 0 10 * * *" in lines
    assert f"container = {DB_CONTAINER}" in lines
    assert "user = postgres" in lines
    assert "no-overlap = true" in lines
    assert "command = sh /opt/baseball/backup/pg_backup.sh" in lines


def test_the_data_backup_runs_in_the_jobs_container_marked() -> None:
    lines = _lines(_section("nightly-backup-data"))
    assert "schedule = 0 0 10 * * *" in lines
    assert f"container = {JOBS_CONTAINER}" in lines
    assert "user = appuser" in lines
    assert "no-overlap = true" in lines
    assert f"command = {DATA_COMMAND}" in lines


def test_the_offsite_copy_runs_in_the_jobs_container_marked_at_10_30() -> None:
    # The copy runs after the two 10:00 backups; the module itself waits on them.
    lines = _lines(_section("nightly-backup-offsite"))
    assert "schedule = 0 30 10 * * *" in lines
    assert f"container = {JOBS_CONTAINER}" in lines
    assert "user = appuser" in lines
    assert "no-overlap = true" in lines
    assert f"command = {OFFSITE_COMMAND}" in lines


def test_the_offsite_copy_follows_the_data_backup_in_the_ini() -> None:
    text = OFELIA.read_text(encoding="utf-8")
    data = text.index('[job-exec "nightly-backup-data"]')
    offsite = text.index('[job-exec "nightly-backup-offsite"]')
    assert data < offsite


def _comment_paragraph(text: str, first: str) -> list[str]:
    """Return the comment lines from the one starting ``first`` to the paragraph's end."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(first))
    paragraph: list[str] = []
    for line in lines[start:]:
        if not line.startswith(";") or line.strip() == ";" or line.startswith("; ===="):
            break
        paragraph.append(line)
    return paragraph


def _prose(lines: list[str]) -> str:
    """Join comment lines into one string, without the leading semicolons."""
    return " ".join(line.lstrip(";").strip() for line in lines)


def test_the_offsite_comments_name_the_keep_setting_and_the_wait_limit() -> None:
    # The count of the kept sets comes from OFFSITE_KEEP, not a fixed three.
    text = OFELIA.read_text(encoding="utf-8")
    job = _prose(_comment_paragraph(text, "; SIM-568: the off-site copy of the backup sets"))
    assert "OFFSITE_KEEP" in job
    assert "BACKUP_KEEP" in job
    assert "newest three" not in job
    header = _prose(_comment_paragraph(text, "; The off-site copy (SIM-568)"))
    assert "After two hours it logs the wait and copies" in header
    assert "nightly-backup-offsite.running" in header


def test_the_offsite_comment_lines_keep_the_80_column_wrap() -> None:
    text = OFELIA.read_text(encoding="utf-8")
    for first in (
        "; The marks:",
        "; The off-site copy (SIM-568)",
        "; SIM-568: the off-site copy of the backup sets",
    ):
        paragraph = _comment_paragraph(text, first)
        assert paragraph, first
        for line in paragraph:
            assert len(line) <= 80, line


def test_the_rebuild_leaves_a_mark_the_backup_waits_on() -> None:
    lines = _lines(_section("nightly-rebuild"))
    commands = [line for line in lines if line.startswith("command = ")]
    assert commands == [
        "command = sh /app/scripts/with_mark.sh nightly-rebuild "
        "sh /app/scripts/with_retry.sh 3 sh /app/scripts/nightly_rebuild.sh"
    ]


def test_the_backup_jobs_hold_no_credential() -> None:
    for job in ("nightly-backup-postgres", "nightly-backup-data", "nightly-backup-offsite"):
        section = _section(job)
        assert "environment =" not in section, job
        assert "postgresql://" not in section, job
        assert "PASSWORD" not in section, job


# ---------------------------------------------------------------------------
# The compose mounts
# ---------------------------------------------------------------------------


def test_the_db_service_mounts_the_backup_folder_and_scripts() -> None:
    volumes = _volumes("db")
    assert f"{BACKUP_SOURCE}:/backups" in volumes
    assert "./db/backup:/opt/baseball/backup:ro" in volumes


def test_the_db_service_passes_the_backup_settings_to_the_postgres_backup() -> None:
    # The db service has no env_file and an Ofelia job-exec passes no
    # environment, so a BACKUP_KEEP in .env reaches pg_backup.sh only here.
    environment = _services()["db"]["environment"]
    assert environment["BACKUP_KEEP"] == "${BACKUP_KEEP:-3}"
    assert environment["BACKUP_MIN_FREE_GB"] == "${BACKUP_MIN_FREE_GB:-10}"


def test_the_jobs_service_mounts_the_backup_folder_writable() -> None:
    assert f"{BACKUP_SOURCE}:/backups" in _volumes("jobs")


def test_the_app_reads_the_backup_folder_read_only() -> None:
    assert f"{BACKUP_SOURCE}:/backups:ro" in _volumes("app")
    assert _services()["app"]["environment"]["BACKUPS_ROOT"] == "/backups"


def test_a_missing_backup_dir_never_stops_compose() -> None:
    # A `:?` would fail every compose command in a worktree without the value.
    text = COMPOSE.read_text(encoding="utf-8")
    assert "${BACKUP_DIR:?" not in text
    assert "${OFFSITE_BACKUP_DIR:?" not in text


def test_the_jobs_service_mounts_the_offsite_folder_writable() -> None:
    # The copy writes the sets into /offsite.
    volumes = _volumes("jobs")
    assert f"{OFFSITE_SOURCE}:/offsite" in volumes
    assert f"{OFFSITE_SOURCE}:/offsite:ro" not in volumes


def test_the_app_reads_the_offsite_folder_read_only() -> None:
    # The off-site age gauge reads the copies; the app never writes one.
    assert f"{OFFSITE_SOURCE}:/offsite:ro" in _volumes("app")
    assert _services()["app"]["environment"]["OFFSITE_ROOT"] == "/offsite"


def test_the_db_service_never_mounts_the_offsite_folder() -> None:
    # The copy reads postgres/ through the jobs container's /backups, so the
    # db service (and its restart) stays out of it.
    assert not [v for v in _volumes("db") if "/offsite" in v]


# ---------------------------------------------------------------------------
# The ignore files and the Makefile
# ---------------------------------------------------------------------------


def test_git_and_docker_ignore_the_default_backup_folder() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "/backups/" in [line.strip() for line in gitignore]
    assert "backups/" in [line.strip() for line in dockerignore]
    # The default folder of the off-site copy.
    assert "/backups_offsite/" in [line.strip() for line in gitignore]
    assert "backups_offsite/" in [line.strip() for line in dockerignore]


def test_the_env_example_documents_the_backup_folder() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "# BACKUP_DIR=" in text
    assert "# BACKUP_KEEP=3" in text
    # The folder of the off-site copy and its retention.
    assert "# OFFSITE_BACKUP_DIR=" in text
    assert "# OFFSITE_KEEP=3" in text


def test_the_makefile_has_the_five_backup_targets() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    phony = text.split(".PHONY:", 1)[1].split("\n\n", 1)[0].replace("\\\n", " ").split()
    for target in ("backup", "backup-status", "backup-drill", "restore-postgres", "backup-offsite"):
        assert f"\n{target}:" in text, target
        assert target in phony, target
    assert "sh /opt/baseball/backup/pg_backup.sh" in text
    assert "python -m pipeline.backup.data_stores --status" in text
    assert "py scripts/backup_drill.py --set $(or $(SET),latest)" in text
    assert "sh /opt/baseball/backup/pg_restore.sh /backups/postgres/$(SET)" in text
    assert "Take a backup first: make backup" in text


def _recipe(text: str, target: str) -> list[str]:
    """Return the TAB-indented recipe lines of one Makefile target."""
    body = text.split(f"\n{target}:", 1)[1].split("\n", 1)[1]
    lines: list[str] = []
    for line in body.splitlines():
        if not line.startswith("\t"):
            break
        lines.append(line[1:])
    return lines


def _offsite_guard() -> str:
    """Return the shell script of the guard line of `make backup-offsite`."""
    line = _recipe(MAKEFILE.read_text(encoding="utf-8"), "backup-offsite")[0]
    assert line.startswith(GUARD_PREFIX), line
    assert line.endswith('"'), line
    return line[len(GUARD_PREFIX) : -1]


def test_the_makefile_runs_the_offsite_copy_marked() -> None:
    # The manual copy leaves the same mark as the scheduled job. A guard
    # line runs first and refuses while another copy holds a fresh mark.
    text = MAKEFILE.read_text(encoding="utf-8")
    recipe = _recipe(text, "backup-offsite")
    assert len(recipe) == 2, recipe
    assert recipe[0].startswith(GUARD_PREFIX)
    assert recipe[1] == (
        "docker compose exec -T jobs sh /app/scripts/with_mark.sh nightly-backup-offsite "
        "python -m pipeline.backup.offsite"
    )


def test_the_offsite_guard_survives_make_cmd_and_git_bash() -> None:
    # make expands $, cmd.exe expands %, and a host sh expands $ and a backtick
    # inside double quotes. The guard holds none of them and no inner double quote.
    script = _offsite_guard()
    for char in ("$", "%", "`", '"'):
        assert char not in script, char
    assert f"find {MARK_DIR} " in script
    assert "-name nightly-backup-offsite.running" in script
    # 360 minutes = the copy's own limit for a stale mark (offsite.py stale_s 21600).
    assert "-mmin -360" in script
    assert "exit 2" in script


def _gnu_find() -> bool:
    if sys.platform == "win32" or shutil.which("sh") is None or shutil.which("find") is None:
        return False
    probe = subprocess.run(["find", "--version"], capture_output=True, text=True, check=False)
    return probe.returncode == 0 and "GNU" in probe.stdout


@pytest.mark.skipif(not _gnu_find(), reason="needs sh and GNU find (the app image has both)")
def test_the_offsite_guard_refuses_only_while_a_fresh_mark_exists(tmp_path: Path) -> None:
    # A manual copy during another copy rewrites the same files and removes
    # the other run's mark. The guard refuses while the mark is fresh.
    script = _offsite_guard().replace(MARK_DIR, str(tmp_path))

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)

    assert run().returncode == 0  # no mark: the copy goes ahead

    mark = tmp_path / "nightly-backup-offsite.running"
    mark.write_text(f"{int(time.time())}\n", encoding="utf-8")
    refused = run()
    assert refused.returncode == 2
    assert "another off-site copy runs" in refused.stderr

    seven_hours_ago = time.time() - 7 * 3600
    os.utime(mark, (seven_hours_ago, seven_hours_ago))
    assert run().returncode == 0  # a dead run's mark does not block the copy

    mark.unlink()
    (tmp_path / "nightly-backup.running").write_text("0\n", encoding="utf-8")
    assert run().returncode == 0  # the data backup's mark is the module's wait, not this guard


def test_the_backup_status_shows_the_offsite_copy() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    assert _recipe(text, "backup-status") == [
        "docker compose exec -T jobs python -m pipeline.backup.data_stores --status",
        "docker compose exec -T jobs python -m pipeline.backup.offsite --status",
    ]


def test_the_backup_drill_restores_from_the_copy_on_offsite() -> None:
    # `make backup-drill OFFSITE=1` passes --offsite to the drill. Only the
    # value 1 does: OFFSITE=0 must not run the off-site drill.
    text = MAKEFILE.read_text(encoding="utf-8")
    assert _recipe(text, "backup-drill") == [
        "py scripts/backup_drill.py --set $(or $(SET),latest) "
        "$(if $(filter 1,$(OFFSITE)),--offsite,)"
    ]


@pytest.mark.skipif(shutil.which("make") is None, reason="needs GNU make (the app image has it)")
@pytest.mark.parametrize(
    ("value", "offsite"),
    [(None, False), ("1", True), ("0", False), ("", False), ("yes", False)],
)
def test_only_offsite_1_turns_on_the_offsite_drill(value: str | None, offsite: bool) -> None:
    # `make -n` prints the recipe and runs nothing.
    env = {k: v for k, v in os.environ.items() if k not in ("OFFSITE", "MAKEFLAGS", "SET")}
    argv = ["make", "-n", "-f", str(MAKEFILE), "backup-drill"]
    if value is not None:
        argv.append(f"OFFSITE={value}")
    out = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, check=True).stdout
    drill = [line for line in out.splitlines() if line.startswith("py scripts/backup_drill.py")]
    assert len(drill) == 1, out
    expected = ["py", "scripts/backup_drill.py", "--set", "latest"]
    if offsite:
        expected.append("--offsite")
    assert drill[0].split() == expected
