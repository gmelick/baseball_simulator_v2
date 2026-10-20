"""The scheduled Postgres backup and its restore (db/backup).

``db/backup/pg_backup.sh`` dumps the database in directory format, counts every
table before and after the dump, and writes a manifest last. The tests run the
real script under ``sh`` with fake ``pg_dump``, ``psql``, ``pg_restore`` and
``df`` binaries placed first on PATH, so no database is needed.
``db/backup/pg_restore.sh`` refuses a populated target and checks each restored
table's count against the bracket the backup took.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

BACKUP_DIR = Path(__file__).resolve().parents[2] / "db" / "backup"
BACKUP_SH = BACKUP_DIR / "pg_backup.sh"
RESTORE_SH = BACKUP_DIR / "pg_restore.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None or os.name == "nt",
    reason="POSIX shell scripts (CI and the container run them)",
)

FAKE_PG_DUMP = r"""#!/bin/sh
if [ "${1:-}" = "--version" ]; then
    echo "pg_dump (PostgreSQL) 15.18"
    exit 0
fi
printf '%s\n' "$@" > "$FAKE_LOG_DIR/pg_dump.argv"
out=""
while [ $# -gt 0 ]; do
    case "$1" in
        -f) out="$2"; shift 2 ;;
        *) shift ;;
    esac
done
mkdir -p "$out"
chmod 700 "$out"  # the real pg_dump creates the dump folder 0700
printf 'toc' > "$out/toc.dat"
printf 'aaaa' > "$out/3001.dat.gz"
printf 'bbbbbb' > "$out/3002.dat.gz"
exit "${FAKE_PG_DUMP_EXIT:-0}"
"""

FAKE_PSQL = r"""#!/bin/sh
sql=""
while [ $# -gt 0 ]; do
    case "$1" in
        -c) sql="$2"; shift 2 ;;
        *) shift ;;
    esac
done
printf '%s\n' "$sql" >> "$FAKE_LOG_DIR/psql.log"
if [ "${FAKE_PSQL_MODE:-}" = "down" ]; then
    echo 'psql: error: FATAL:  database "fresh1" does not exist' >&2
    exit 2
fi
case "$sql" in
    *"DROP SCHEMA"*)
        if [ "${FAKE_PSQL_MODE:-}" = "populated" ]; then
            echo "ERROR:  cannot drop schema raw because other objects depend on it" >&2
            exit 1
        fi
        if [ "${FAKE_PSQL_MODE:-}" = "denied" ]; then
            echo "ERROR:  must be owner of schema raw" >&2
            exit 1
        fi
        ;;
    *relkind*) printf 'public.alembic_version\nraw.pitches\nsim.sim_runs\n' ;;
    *"count(*)"*) echo "${FAKE_COUNT:-10}" ;;
    *server_version*) echo "15.18 (Alpine)" ;;
    *alembic_version*) echo "0030" ;;
esac
exit 0
"""

FAKE_PG_RESTORE = r"""#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_LOG_DIR/pg_restore.argv"
if [ "${FAKE_PG_RESTORE_MODE:-}" = "fail" ]; then
    echo "pg_restore: warning: errors ignored on restore: 2" >&2
    exit 1
fi
case "$*" in
    *--list*) echo "; Archive created at 2026-10-15" ;;
esac
exit 0
"""

# The free column (KiB) is FAKE_DF_FREE_KB; once fewer than FAKE_DF_SETS_BELOW
# complete postgres sets remain, it reads FAKE_DF_FREE_AFTER_KB instead.
FAKE_DF = r"""#!/bin/sh
for last; do :; done
free="${FAKE_DF_FREE_KB:-104857600}"
if [ -n "${FAKE_DF_SETS_BELOW:-}" ]; then
    n=$(ls "$last"/postgres/*/manifest.json 2>/dev/null | wc -l)
    if [ "$n" -lt "$FAKE_DF_SETS_BELOW" ]; then
        free="$FAKE_DF_FREE_AFTER_KB"
    fi
fi
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "fake 999999999 1 $free 1% $last"
"""

TS = ("20261001T100000Z", "20261002T100000Z", "20261003T100000Z", "20261004T100000Z")


@pytest.fixture
def fake(tmp_path):
    """Write the fake binaries and return (env, root, log dir)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("pg_dump", FAKE_PG_DUMP),
        ("psql", FAKE_PSQL),
        ("pg_restore", FAKE_PG_RESTORE),
        ("df", FAKE_DF),
    ):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    logs = tmp_path / "logs"
    logs.mkdir()
    root = tmp_path / "backups"
    root.mkdir()
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            "FAKE_LOG_DIR": str(logs),
            "BACKUP_ROOT": str(root),
            "POSTGRES_USER": "baseball_user",
            "POSTGRES_DB": "baseball_sim",
        }
    )
    return env, root, logs


def _run(script: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(script), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _make_set(root: Path, ts: str, nbytes: int = 1000) -> Path:
    """Write a complete fake postgres set whose manifest claims ``nbytes``."""
    d = root / "postgres" / ts
    (d / "dump").mkdir(parents=True)
    (d / "dump" / "toc.dat").write_text("toc")
    manifest = {"store": "postgres", "set": ts, "bytes": nbytes}
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return d


def _complete_sets(root: Path) -> list[str]:
    pg = root / "postgres"
    return sorted(p.name for p in pg.iterdir() if (p / "manifest.json").exists())


# --- static checks ------------------------------------------------------------


@pytest.mark.parametrize("script", [BACKUP_SH, RESTORE_SH], ids=lambda p: p.name)
def test_the_script_parses_and_never_echoes_a_command(script):
    assert subprocess.run(["sh", "-n", str(script)], capture_output=True).returncode == 0
    text = script.read_text()
    assert "set -x" not in text
    assert "postgresql://" not in text
    assert text.startswith("#!/usr/bin/env sh")


# --- pg_backup.sh -------------------------------------------------------------


def test_a_backup_writes_a_complete_set(fake):
    env, root, logs = fake
    res = _run(BACKUP_SH, env=env)
    assert res.returncode == 0, res.stdout + res.stderr

    sets = list((root / "postgres").iterdir())
    assert len(sets) == 1
    s = sets[0]
    assert not s.name.endswith(".partial")
    assert len(s.name) == 16 and s.name.endswith("Z")
    assert (s / "dump" / "toc.dat").is_file()
    before = (s / "counts_before.tsv").read_text().splitlines()
    after = (s / "counts_after.tsv").read_text().splitlines()
    assert before == ["public.alembic_version\t10", "raw.pitches\t10", "sim.sim_runs\t10"]
    assert after == before

    m = json.loads((s / "manifest.json").read_text(encoding="utf-8"))
    for key in (
        "store", "set", "created_at", "seconds", "bytes", "host", "tool", "files",
        "database", "server_version", "client_version", "format", "jobs",
        "compress", "alembic_head", "pg_restore_list_ok", "counts_files",
    ):  # fmt: skip
        assert key in m, key
    assert m["store"] == "postgres"
    assert m["set"] == s.name
    assert m["created_at"].endswith("Z")
    assert isinstance(m["seconds"], float)
    assert m["tool"] == "db/backup/pg_backup.sh"
    assert m["database"] == "baseball_sim"
    assert m["server_version"] == "15.18 (Alpine)"
    assert m["client_version"] == "pg_dump (PostgreSQL) 15.18"
    assert m["format"] == "directory"
    assert m["jobs"] == 4 and m["compress"] == 3
    assert m["alembic_head"] == "0030"
    assert m["pg_restore_list_ok"] is True
    assert m["counts_files"] == ["counts_before.tsv", "counts_after.tsv"]

    paths = {f["path"]: f for f in m["files"]}
    assert "dump/toc.dat" in paths and "dump/3001.dat.gz" in paths
    assert "counts_before.tsv" in paths and "manifest.json" not in paths
    toc = paths["dump/toc.dat"]
    assert toc["bytes"] == 3
    assert toc["sha256"] == hashlib.sha256(b"toc").hexdigest()
    assert m["bytes"] == sum(f["bytes"] for f in m["files"])

    argv = (logs / "pg_dump.argv").read_text().splitlines()
    assert "-Fd" in argv
    assert argv[argv.index("-j") + 1] == "4"
    assert argv[argv.index("-Z") + 1] == "3"
    assert argv[argv.index("-f") + 1] == str(root / "postgres" / f"{s.name}.partial" / "dump")
    assert "--list" in (logs / "pg_restore.argv").read_text()

    last = res.stdout.strip().splitlines()[-1]
    assert last.startswith(f"BACKUP OK postgres {s.name} ")
    assert " bytes " in last and last.endswith(" s")


def test_the_set_is_readable_by_another_user(fake):
    # pg_dump creates the dump folder 0700. The off-site copy reads the set
    # from the jobs container as appuser, another uid, so the script opens
    # every folder and file of the set before the manifest lands.
    env, root, _logs = fake
    res = _run(BACKUP_SH, env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    s = next((root / "postgres").iterdir())
    for path in [s, *s.rglob("*")]:
        mode = stat.S_IMODE(path.stat().st_mode)
        want = 0o055 if path.is_dir() else 0o044
        assert mode & want == want, f"{path.relative_to(s)} has mode {oct(mode)}"


def test_a_leftover_partial_set_is_deleted(fake):
    env, root, _logs = fake
    dead = root / "postgres" / "x.partial"
    dead.mkdir(parents=True)
    (dead / "junk").write_text("x")
    assert _run(BACKUP_SH, env=env).returncode == 0
    assert not dead.exists()


def test_low_free_space_refuses_before_the_dump(fake):
    env, root, logs = fake
    env["FAKE_DF_FREE_KB"] = "1024"
    res = _run(BACKUP_SH, env=env)
    assert res.returncode == 2
    assert not (logs / "pg_dump.argv").exists()
    assert _complete_sets(root) == []


def test_low_free_space_deletes_the_oldest_sets_while_more_than_two_remain(fake):
    env, root, logs = fake
    for ts in TS:
        _make_set(root, ts)
    env["FAKE_DF_FREE_KB"] = "1024"
    res = _run(BACKUP_SH, env=env)
    assert res.returncode == 2
    assert not (logs / "pg_dump.argv").exists()
    assert _complete_sets(root) == list(TS[2:])


def test_the_guard_recovers_when_pruning_frees_enough(fake):
    env, root, logs = fake
    for ts in TS:
        _make_set(root, ts)
    env.update(
        {
            "FAKE_DF_FREE_KB": "1024",
            "FAKE_DF_SETS_BELOW": "4",
            "FAKE_DF_FREE_AFTER_KB": "104857600",
        }
    )
    res = _run(BACKUP_SH, env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert (logs / "pg_dump.argv").exists()
    sets = _complete_sets(root)
    assert TS[0] not in sets
    assert len(sets) == 3  # retention keeps three after the success


def test_the_guard_counts_the_newest_set_size(fake):
    env, root, logs = fake
    # 100 GiB free; the newest set is 80 GiB, so 1.5 x 80 = 120 GiB is short.
    _make_set(root, TS[0], nbytes=80 * 1024**3)
    env["FAKE_DF_FREE_KB"] = str(100 * 1024**2)
    res = _run(BACKUP_SH, env=env)
    assert res.returncode == 2
    assert not (logs / "pg_dump.argv").exists()


def test_retention_keeps_three_complete_sets(fake):
    env, root, _logs = fake
    for ts in TS:
        _make_set(root, ts)
    res = _run(BACKUP_SH, env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    sets = _complete_sets(root)
    assert len(sets) == 3
    assert sets[:2] == list(TS[2:])


def test_a_failed_dump_exits_1_and_prunes_nothing(fake):
    env, root, _logs = fake
    for ts in TS:
        _make_set(root, ts)
    env["FAKE_PG_DUMP_EXIT"] = "1"
    res = _run(BACKUP_SH, env=env)
    assert res.returncode == 1
    assert len(_complete_sets(root)) == 4
    assert "BACKUP OK" not in res.stdout


def _dead_pid() -> int:
    proc = subprocess.Popen(["sh", "-c", "exit 0"])
    proc.wait()
    return proc.pid


def test_a_held_lock_refuses_and_deletes_nothing(fake):
    env, root, logs = fake
    # A run in progress: its lock (a live pid, just taken) and its partial set.
    lock = root / "postgres" / ".lock"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(f"{os.getpid()} {int(time.time())}\n")
    busy = root / "postgres" / "20261015T100000Z.partial"
    (busy / "dump").mkdir(parents=True)
    (busy / "dump" / "toc.dat").write_text("toc")
    res = _run(BACKUP_SH, env=env)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "holds" in res.stderr
    assert (busy / "dump" / "toc.dat").is_file()
    assert lock.is_dir()  # the refused run leaves the other run's lock alone
    assert not (logs / "pg_dump.argv").exists()


def test_a_stale_lock_is_removed_and_the_run_proceeds(fake):
    env, root, logs = fake
    lock = root / "postgres" / ".lock"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(f"{_dead_pid()} {int(time.time())}\n")
    res = _run(BACKUP_SH, env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "stale lock" in res.stdout
    assert not lock.exists()
    assert len(_complete_sets(root)) == 1


def test_an_old_lock_is_stale_even_with_a_live_pid(fake):
    env, root, _logs = fake
    lock = root / "postgres" / ".lock"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(f"{os.getpid()} 1000\n")  # epoch 1000: years old
    res = _run(BACKUP_SH, env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert not lock.exists()


def test_the_lock_is_released_after_a_failure(fake):
    env, root, _logs = fake
    env["FAKE_PG_DUMP_EXIT"] = "1"
    assert _run(BACKUP_SH, env=env).returncode == 1
    assert not (root / "postgres" / ".lock").exists()


def test_a_missing_root_refuses(fake, tmp_path):
    env, _root, logs = fake
    env["BACKUP_ROOT"] = str(tmp_path / "nowhere")
    assert _run(BACKUP_SH, env=env).returncode == 2
    assert not (logs / "pg_dump.argv").exists()


# --- pg_restore.sh ------------------------------------------------------------


def _restore_set(root: Path, before: int = 8, after: int = 12) -> Path:
    s = _make_set(root, "20261005T100000Z")
    tables = ("public.alembic_version", "raw.pitches", "sim.sim_runs")
    (s / "counts_before.tsv").write_text("".join(f"{t}\t{before}\n" for t in tables))
    (s / "counts_after.tsv").write_text("".join(f"{t}\t{after}\n" for t in tables))
    return s


def test_restore_refuses_a_populated_target(fake):
    env, root, logs = fake
    s = _restore_set(root)
    env["FAKE_PSQL_MODE"] = "populated"
    res = _run(RESTORE_SH, str(s), env=env)
    assert res.returncode == 3
    assert "target database is not empty" in res.stdout + res.stderr
    assert not (logs / "pg_restore.argv").exists()


def test_restore_reports_a_connection_failure_not_a_populated_target(fake):
    env, root, logs = fake
    s = _restore_set(root)
    env.update({"FAKE_PSQL_MODE": "down", "PG_CONNECT_WAIT_S": "0"})
    res = _run(RESTORE_SH, str(s), env=env)
    assert res.returncode == 1, res.stdout + res.stderr
    out = res.stdout + res.stderr
    assert "cannot connect" in out
    assert "does not exist" in out
    assert "not empty" not in out
    assert "DROP SCHEMA" not in (logs / "psql.log").read_text()
    assert not (logs / "pg_restore.argv").exists()


def test_restore_reports_another_drop_failure_as_exit_1(fake):
    env, root, logs = fake
    s = _restore_set(root)
    env["FAKE_PSQL_MODE"] = "denied"
    res = _run(RESTORE_SH, str(s), env=env)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "not empty" not in res.stdout + res.stderr
    assert "must be owner" in res.stderr
    assert not (logs / "pg_restore.argv").exists()


def test_restore_passes_when_every_count_lies_in_the_bracket(fake):
    env, root, logs = fake
    s = _restore_set(root)
    env["FAKE_COUNT"] = "10"
    res = _run(RESTORE_SH, str(s), env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.strip().splitlines()[-1] == f"RESTORE OK {s}"
    assert "errors ignored" not in res.stdout + res.stderr
    argv = (logs / "pg_restore.argv").read_text()
    for flag in ("-j 4", "--no-owner", "--no-acl", "--exit-on-error", "-d baseball_sim"):
        assert flag in argv
    psql_log = (logs / "psql.log").read_text()
    assert "DROP SCHEMA IF EXISTS raw" in psql_log
    assert "DROP SCHEMA IF EXISTS sim" in psql_log
    assert "CASCADE" not in psql_log


def test_restore_accepts_a_count_on_the_bracket_edge(fake):
    env, root, _logs = fake
    s = _restore_set(root, before=12, after=8)  # a table that shrank in the dump
    env["FAKE_COUNT"] = "8"
    assert _run(RESTORE_SH, str(s), env=env).returncode == 0


def test_restore_fails_when_a_count_lies_outside_the_bracket(fake):
    env, root, _logs = fake
    s = _restore_set(root)
    env["FAKE_COUNT"] = "13"
    res = _run(RESTORE_SH, str(s), env=env)
    assert res.returncode == 1
    assert "RESTORE OK" not in res.stdout


def test_restore_skips_the_count_check_when_asked(fake):
    env, root, _logs = fake
    s = _restore_set(root)
    env.update({"FAKE_COUNT": "13", "COUNT_CHECK": "0"})
    res = _run(RESTORE_SH, str(s), env=env)
    assert res.returncode == 0
    assert res.stdout.strip().splitlines()[-1] == f"RESTORE OK {s}"


def test_restore_fails_when_pg_restore_fails(fake):
    env, root, _logs = fake
    s = _restore_set(root)
    env["FAKE_PG_RESTORE_MODE"] = "fail"
    res = _run(RESTORE_SH, str(s), env=env)
    assert res.returncode == 1
    assert "RESTORE OK" not in res.stdout


def test_restore_requires_a_complete_set(fake, tmp_path):
    env, _root, logs = fake
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _run(RESTORE_SH, str(empty), env=env).returncode == 2
    assert _run(RESTORE_SH, env=env).returncode == 2
    assert not (logs / "pg_restore.argv").exists()
