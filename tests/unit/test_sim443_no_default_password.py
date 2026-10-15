"""SIM-443a/b/f — text guards on the deployment configuration.

The stack used to ship one default Postgres password in dozens of files, a
literal DSN in the compose file and the scheduler INI, and every service port
on every host interface. These tests read the committed files and fail when
any of that comes back:

- no tracked file outside ``docs/`` and ``CHANGES.md`` holds the retired
  default password;
- compose publishes Postgres, Redis, the app, Prometheus and Grafana on
  127.0.0.1 only, and nginx on every interface;
- compose reads every secret with ``${VAR:?...}`` (no default) and builds
  each in-container DSN from the role passwords;
- the scheduler INI runs the data jobs as job-exec jobs in the ``jobs``
  container and holds no DSN;
- the five nightly shell scripts refuse to start without ``BASEBALL_DB_DSN``;
- the role script never echoes a command, and every psql variable the role
  SQL reads is one the role script passes.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
OFELIA = ROOT / "deploy" / "ofelia" / "config.ini"
ROLES_SH = ROOT / "db" / "roles" / "setup_roles.sh"
ROLES_SQL = ROOT / "db" / "roles" / "setup_roles.sql"

# Built from two halves so this file never matches its own search.
RETIRED_PASSWORD = "baseball" + "_pass"  # nosec - test-only literal

LOOPBACK_SERVICES = ("db", "redis", "app", "prometheus", "grafana")
SECRETS = (
    "POSTGRES_PASSWORD",
    "BASEBALL_APP_DB_PASSWORD",
    "BASEBALL_RO_DB_PASSWORD",
    "REDIS_PASSWORD",
    "GRAFANA_ADMIN_PASSWORD",
)
GUARDED_SCRIPTS = (
    "nightly_finals.sh",
    "nightly_rebuild.sh",
    "nightly_ingest.sh",
    "weekly_refresh.sh",
    "nightly_closing_lines.sh",
)
JOBS_CONTAINER = "baseball_simulator_v2-jobs-1"


def _compose() -> dict[str, Any]:
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _services() -> dict[str, Any]:
    services = _compose()["services"]
    assert isinstance(services, dict)
    return services


# ---------------------------------------------------------------------------
# The retired default password
# ---------------------------------------------------------------------------


def test_no_tracked_file_holds_the_retired_password() -> None:
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("needs a git checkout")
    # --untracked also reads new files git does not ignore, so a file that is
    # about to be committed is checked too.
    proc = subprocess.run(
        [
            git,
            "grep",
            "-nI",
            "--untracked",
            RETIRED_PASSWORD,
            "--",
            ":!docs/",
            ":!CHANGES.md",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # git grep exits 1 when nothing matches, 0 on a match, >1 on an error.
    if proc.returncode > 1:
        pytest.skip(f"git grep failed: {proc.stderr.strip()}")
    assert proc.returncode == 1, "the retired default password is back:\n" + proc.stdout


def test_compose_has_no_default_for_any_secret() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert RETIRED_PASSWORD not in text
    for name in SECRETS:
        # Every read of the secret is a required read: ${NAME:?message}.
        reads = re.findall(r"\$\{" + name + r"([^A-Za-z0-9_])", text)
        assert reads, f"{name} is never read"
        assert set(reads) == {":"}, f"{name} has a read that is not ${{{name}:?...}}"
        assert f"${{{name}:-" not in text
        assert f"${{{name}-" not in text
        assert f"${{{name}}}" not in text


# ---------------------------------------------------------------------------
# Published ports (SIM-443a)
# ---------------------------------------------------------------------------


def test_compose_publishes_only_nginx_on_every_interface() -> None:
    services = _services()
    for name in LOOPBACK_SERVICES:
        ports = services[name].get("ports") or []
        assert ports, f"{name} publishes no port"
        for port in ports:
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port!r} beyond loopback"
    nginx_ports = services["nginx"]["ports"]
    assert nginx_ports
    for port in nginx_ports:
        assert not str(port).startswith("127.0.0.1:"), "nginx :80 must stay on the LAN"
    # No other service publishes a port at all.
    for name, service in services.items():
        if name in (*LOOPBACK_SERVICES, "nginx"):
            continue
        assert not service.get("ports"), f"{name} publishes a port"


# ---------------------------------------------------------------------------
# The composed DSNs and the Redis password (SIM-443b, SIM-443f)
# ---------------------------------------------------------------------------


def test_compose_builds_each_dsn_from_its_role_password() -> None:
    services = _services()
    app_dsn = "postgresql://baseball_app:${BASEBALL_APP_DB_PASSWORD:?"
    for name in ("app", "live", "jobs"):
        env = services[name]["environment"]
        assert str(env["BASEBALL_DB_DSN"]).startswith(app_dsn), name
        # The owner's password never reaches a serving container.
        assert env.get("POSTGRES_PASSWORD") == "", name
    assert str(services["app"]["environment"]["BASEBALL_DB_RO_DSN"]).startswith(
        "postgresql://baseball_ro:${BASEBALL_RO_DB_PASSWORD:?"
    )
    for name in ("app", "live"):
        assert str(services[name]["environment"]["REDIS_URL"]).startswith(
            "redis://:${REDIS_PASSWORD:?"
        ), name
    migrate_dsn = str(services["migrate"]["environment"]["BASEBALL_DB_DSN"])
    assert migrate_dsn.startswith(
        "postgresql://${POSTGRES_USER:-baseball_user}:${POSTGRES_PASSWORD:?"
    )


def test_the_migrate_service_reads_the_current_migrations() -> None:
    # Without the mount, alembic runs the tree baked into the image at its
    # first build, and a newer migration never applies.
    volumes = [str(v) for v in _services()["migrate"].get("volumes", [])]
    assert "./db:/app/db:ro" in volumes
    assert "./alembic.ini:/app/alembic.ini:ro" in volumes


def test_redis_asks_for_its_password() -> None:
    redis = _services()["redis"]
    assert "--requirepass ${REDIS_PASSWORD:?" in str(redis["command"])
    assert str(redis["environment"]["REDISCLI_AUTH"]).startswith("${REDIS_PASSWORD:?")
    # A wrong password answers NOAUTH with exit 0, so the check must read PONG.
    assert "PONG" in " ".join(redis["healthcheck"]["test"])


def test_the_db_service_mounts_the_role_script() -> None:
    db = _services()["db"]
    volumes = [str(v) for v in db["volumes"]]
    assert "./db/roles:/opt/baseball/roles:ro" in volumes
    assert "./db/roles/setup_roles.sh:/docker-entrypoint-initdb.d/10-setup-roles.sh:ro" in volumes
    for name in ("BASEBALL_APP_DB_PASSWORD", "BASEBALL_RO_DB_PASSWORD"):
        assert str(db["environment"][name]).startswith("${" + name + ":?")


# ---------------------------------------------------------------------------
# The jobs container and the scheduler INI (SIM-443b)
# ---------------------------------------------------------------------------


def test_the_jobs_service_backs_the_scheduler() -> None:
    services = _services()
    jobs = services["jobs"]
    assert jobs["user"] == "appuser"
    assert jobs["command"] == "sleep infinity"
    assert jobs["restart"] == "unless-stopped"
    assert "duckdb_data:/data" in jobs["volumes"]
    assert jobs["environment"]["OPENBLAS_NUM_THREADS"] == "1"
    assert jobs["environment"]["BASEBALL_DUCKDB_PATH"] == "/data/baseball_sim.duckdb"
    assert "JOBS_MEM_LIMIT" in str(jobs["mem_limit"])
    assert "test -w /data" in " ".join(jobs["healthcheck"]["test"])
    depends = services["scheduler"]["depends_on"]
    assert depends["jobs"]["condition"] == "service_healthy"


def test_the_ofelia_ini_holds_no_credential() -> None:
    text = OFELIA.read_text(encoding="utf-8")
    assert "job-run" not in text
    assert "environment =" not in text
    assert "postgresql://" not in text
    assert RETIRED_PASSWORD not in text
    # The data backup (SIM-525) is the fourth job in the jobs container.
    assert text.count(f"container = {JOBS_CONTAINER}") == 4
    for job in ("nightly-finals", "weekly-refresh", "nightly-rebuild", "nightly-backup-data"):
        section = text.split(f'[job-exec "{job}"]', 1)[1].split("\n[", 1)[0]
        assert f"container = {JOBS_CONTAINER}" in section, job
        # Ofelia execs as root unless the job names a user (SIM-563).
        assert "user = appuser" in section, job
    # The Postgres backup runs in the db container as the postgres OS user.
    # It logs in over the socket, so it needs no password.
    section = text.split('[job-exec "nightly-backup-postgres"]', 1)[1].split("\n[", 1)[0]
    assert "container = baseball_simulator_v2-db-1" in section
    assert "user = postgres" in section


# ---------------------------------------------------------------------------
# The shell scripts (SIM-443b)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", GUARDED_SCRIPTS)
def test_the_nightly_script_requires_the_dsn(script: str) -> None:
    text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
    assert "${BASEBALL_DB_DSN:?" in text
    assert "${BASEBALL_DB_DSN:-" not in text
    assert RETIRED_PASSWORD not in text


def test_the_role_script_never_echoes_or_exits_the_caller() -> None:
    text = ROLES_SH.read_text(encoding="utf-8")
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    for line in code:
        # set -x prints every command (the passwords with them); -e and -a would
        # leak into the postgres entrypoint, which SOURCES this file.
        assert not re.match(r"\s*set\s+-[a-z]*[xea]", line), line
    # The body runs in a subshell, so `exit` never ends the entrypoint.
    body = "\n".join(code).strip()
    assert body.startswith("(") and body.endswith(")")


def test_every_role_sql_variable_is_passed_by_the_script() -> None:
    shell = ROLES_SH.read_text(encoding="utf-8")
    sql_lines = [
        line
        for line in ROLES_SQL.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("--")
    ]
    sql = "\n".join(sql_lines)
    names = set(re.findall(r""":'(\w+)'|:"(\w+)"|\\if\s+:(\w+)""", sql))
    used = {n for group in names for n in group if n}
    assert used >= {"admin_role", "db_name", "app_password", "ro_password", "rotate_admin"}
    for name in used:
        assert f"-v {name}=" in shell, f"the SQL reads :{name}, the script never passes it"
