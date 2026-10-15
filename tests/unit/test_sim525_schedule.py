"""The scheduled backups of the three data stores: the wiring (SIM-525).

The scheduler (deploy/ofelia/config.ini) runs two backup jobs at 10:00 UTC,
after the nightly rebuild. The Postgres job runs inside the db container as
the postgres user; the data job backs up the DuckDB file and the bundle
inside the jobs container. The compose file mounts the backup folder into
the db, jobs and app containers. These tests read the files only; they start
no container.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
OFELIA = ROOT / "deploy" / "ofelia" / "config.ini"
MAKEFILE = ROOT / "Makefile"

BACKUP_SOURCE = "${BACKUP_DIR:-./backups}"
DB_CONTAINER = "baseball_simulator_v2-db-1"
JOBS_CONTAINER = "baseball_simulator_v2-jobs-1"
DATA_COMMAND = (
    "sh /app/scripts/with_mark.sh nightly-backup sh /app/scripts/with_retry.sh 2 "
    "python -m pipeline.backup.data_stores"
)


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


def test_the_rebuild_leaves_a_mark_the_backup_waits_on() -> None:
    lines = _lines(_section("nightly-rebuild"))
    commands = [line for line in lines if line.startswith("command = ")]
    assert commands == [
        "command = sh /app/scripts/with_mark.sh nightly-rebuild "
        "sh /app/scripts/with_retry.sh 3 sh /app/scripts/nightly_rebuild.sh"
    ]


def test_the_backup_jobs_hold_no_credential() -> None:
    for job in ("nightly-backup-postgres", "nightly-backup-data"):
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


# ---------------------------------------------------------------------------
# The ignore files and the Makefile
# ---------------------------------------------------------------------------


def test_git_and_docker_ignore_the_default_backup_folder() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "/backups/" in [line.strip() for line in gitignore]
    assert "backups/" in [line.strip() for line in dockerignore]


def test_the_env_example_documents_the_backup_folder() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "# BACKUP_DIR=" in text
    assert "# BACKUP_KEEP=3" in text


def test_the_makefile_has_the_four_backup_targets() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    phony = text.split(".PHONY:", 1)[1].split("\n\n", 1)[0].replace("\\\n", " ").split()
    for target in ("backup", "backup-status", "backup-drill", "restore-postgres"):
        assert f"\n{target}:" in text, target
        assert target in phony, target
    assert "sh /opt/baseball/backup/pg_backup.sh" in text
    assert "python -m pipeline.backup.data_stores --status" in text
    assert "py scripts/backup_drill.py --set $(or $(SET),latest)" in text
    assert "sh /opt/baseball/backup/pg_restore.sh /backups/postgres/$(SET)" in text
    assert "Take a backup first: make backup" in text
