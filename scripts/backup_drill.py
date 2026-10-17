"""The restore drill for the scheduled backups (SIM-525).

The drill proves that a backup set restores. It runs on the host with the
Windows ``py`` launcher and uses only the standard library. It never touches
the live stack's database:

1. It refuses to start while any ``sim525_*`` container or volume exists.
2. It starts a scratch Postgres cluster (``sim525_drill_pg``) with no network,
   on a named volume, with the backup set mounted read-only at ``/restore``.
3. It runs the real ``db/backup/pg_restore.sh`` against the scratch cluster.
4. It asks the jobs container to verify the DuckDB and bundle copies
   (``python -m pipeline.backup.data_stores --verify-set``).
5. It removes the scratch container and its volume, whatever happened.
6. It writes ``<backup dir>/drills/<start time>.json`` and prints a report.

The drill also restores from the off-site copy (SIM-568). The off-site copy
is the OneDrive folder that ``OFFSITE_BACKUP_DIR`` names (default
``./backups_offsite``); the jobs container reads it at ``/offsite``. Pass
``--offsite``, or pass that folder as ``--backup-dir``. The data verify then
runs with ``--backups-root /offsite``. The report names its source
(``"source": "offsite"``) and lands in the off-site folder's ``drills/``.
OneDrive can turn synced files into online-only placeholders. A drill from
such a dehydrated copy downloads the whole set first, so it takes longer and
needs the OneDrive client online.

The drill reads ``BACKUP_DIR`` and ``OFFSITE_BACKUP_DIR`` in the order docker
compose reads them (SIM-568): the shell environment first, then the repo's
``.env``. So the drill and the jobs container's mounts name the same folders.

Exit 0 when every step passes, 1 when a step fails, 2 when the drill refuses
to start: a leftover scratch container or volume, no complete set,
``--offsite`` with a ``--backup-dir`` that is not the off-site folder, a
backup folder that is neither the local folder nor the off-site folder the
jobs container reads, or no ``db/backup`` folder in the repo.

Usage::

    py scripts/backup_drill.py [--set TS|latest] [--data-set TS|latest]
                               [--backup-dir PATH] [--offsite]
                               [--keep-cluster] [--skip-data]
                               [--timeout-s 7200]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any

REPO = Path(__file__).resolve().parents[1]
# timezone.utc, not datetime.UTC, so the drill also runs on Python 3.10.
UTC = timezone.utc  # noqa: UP017

NAME = "sim525_drill_pg"
IMAGE = "postgres:15-alpine"
PG_USER = "baseball_user"
PG_DB = "baseball_sim"
RESTORE_SCRIPT = "/opt/baseball/backup/pg_restore.sh"
TAIL_LINES = 40
READY_TIMEOUT_S = 120.0
READY_POLL_S = 2.0
# The roots where the jobs container mounts the two backup folders (SIM-568).
LOCAL_ROOT = "/backups"
OFFSITE_ROOT = "/offsite"


class DrillError(Exception):
    """A reason the drill refuses to start."""


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _run(
    argv: Sequence[str],
    cwd: Path | None = None,
    capture: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one command with a list argv and no shell.

    With ``capture`` the output comes back quietly. Without it the output also
    streams to the console line by line, and comes back in ``stdout``.
    """
    args = list(argv)
    if capture:
        return subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
        )
    lines: list[str] = []
    with subprocess.Popen(
        args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    ) as proc:
        stream = proc.stdout
        assert stream is not None

        def pump() -> None:
            # A daemon thread reads the output, so a silent hang cannot block the timeout.
            for line in stream:
                sys.stdout.write(line)
                sys.stdout.flush()
                lines.append(line)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
            reader.join(timeout=5)
            lines.append(f"[drill] the command passed its {timeout:.0f} s timeout\n")
            code = rc if rc != 0 else -9
            return subprocess.CompletedProcess(args, code, "".join(lines), "")
        reader.join(timeout=5)
    return subprocess.CompletedProcess(args, rc, "".join(lines), "")


def log(message: str) -> None:
    """Print one stamped line."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[backup-drill {stamp}] {message}", flush=True)


def read_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines; skip blanks and comments; strip one pair of quotes."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def compose_value(key: str, repo: Path) -> str:
    """Return the value docker compose reads for one variable (SIM-568).

    Compose takes a variable from the shell environment first, and from the
    repo's .env only when the shell does not set it. The drill reads the same
    order, so its folders match the jobs container's mounts. An empty string
    means the variable is unset or empty: the caller then takes the compose
    file's default, as ``${VAR:-default}`` does.
    """
    if key in os.environ:
        return os.environ[key]
    return read_env_file(repo / ".env").get(key, "")


def resolve_backup_dir(flag: str | None, repo: Path) -> Path:
    """Pick the backup folder: the flag, else BACKUP_DIR, else ./backups.

    BACKUP_DIR comes from the shell environment, then .env (``compose_value``).
    """
    if flag:
        return Path(flag).resolve()
    configured = compose_value("BACKUP_DIR", repo)
    path = Path(configured) if configured else Path("backups")
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def resolve_offsite_dir(flag: str | None, repo: Path) -> Path:
    """Pick the off-site folder (SIM-568).

    The flag wins; else OFFSITE_BACKUP_DIR from the shell environment, then
    .env (``compose_value``); else ./backups_offsite, the same default that the
    compose file mounts at /offsite.
    """
    if flag:
        return Path(flag).resolve()
    configured = compose_value("OFFSITE_BACKUP_DIR", repo)
    path = Path(configured) if configured else Path("backups_offsite")
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def complete_sets(store_dir: Path) -> list[str]:
    """Return the names of the complete sets in a store folder, oldest first."""
    if not store_dir.is_dir():
        return []
    return sorted(
        p.name
        for p in store_dir.iterdir()
        if p.is_dir() and not p.name.endswith(".partial") and (p / "manifest.json").is_file()
    )


def pick_set(store_dir: Path, which: str) -> str:
    """Return the named complete set, or the newest one for 'latest'."""
    names = complete_sets(store_dir)
    if which == "latest":
        if not names:
            raise DrillError(f"no complete set in {store_dir}")
        return names[-1]
    if which not in names:
        raise DrillError(f"set {which} is missing or has no manifest.json in {store_dir}")
    return which


def pick_data_set(backup_dir: Path, which: str) -> str:
    """Return one set id that is complete in BOTH the duckdb and the bundle store.

    One data run writes its duckdb and bundle sets under one id. For 'latest'
    the drill takes the newest id complete in both stores; a named id must be
    complete in both.
    """
    duckdb_sets = complete_sets(backup_dir / "duckdb")
    bundle_sets = complete_sets(backup_dir / "bundle")
    if which == "latest":
        if not duckdb_sets:
            raise DrillError(f"no complete set in {backup_dir / 'duckdb'}")
        if not bundle_sets:
            raise DrillError(f"no complete set in {backup_dir / 'bundle'}")
        shared = sorted(set(duckdb_sets) & set(bundle_sets))
        if not shared:
            raise DrillError("no set id is complete in both the duckdb and the bundle store")
        return shared[-1]
    for store, names in (("duckdb", duckdb_sets), ("bundle", bundle_sets)):
        if which not in names:
            raise DrillError(
                f"set {which} is missing or has no manifest.json in {backup_dir / store}"
            )
    return which


def docker_path(path: PurePath) -> str:
    """Write a host path the way docker on Windows reads it: 'C:/...'."""
    return path.as_posix()


def docker_run_argv(set_dir: Path, backup_scripts: Path) -> list[str]:
    """Build the argv that starts the scratch cluster."""
    return [
        "docker",
        "run",
        "-d",
        "--name",
        NAME,
        "--network",
        "none",
        "-e",
        "POSTGRES_HOST_AUTH_METHOD=trust",
        "-e",
        f"POSTGRES_USER={PG_USER}",
        "-e",
        f"POSTGRES_DB={PG_DB}",
        "-v",
        f"{NAME}:/var/lib/postgresql/data",
        "-v",
        f"{docker_path(set_dir.resolve())}:/restore:ro",
        "-v",
        f"{docker_path(backup_scripts.resolve())}:/opt/baseball/backup:ro",
        IMAGE,
    ]


def tail_lines(text: str, count: int = TAIL_LINES) -> list[str]:
    """Return the last lines of an output, for the report."""
    return text.splitlines()[-count:]


def restore_ok(exit_code: int, output: str) -> bool:
    """Pass the restore on exit 0, no ignored errors and a final 'RESTORE OK' line."""
    if exit_code != 0 or "errors ignored" in output:
        return False
    lines = [line for line in output.splitlines() if line.strip()]
    return bool(lines) and lines[-1].startswith("RESTORE OK")


def leftovers() -> list[str]:
    """List every sim525_* container and volume that already exists."""
    found: list[str] = []
    for argv in (
        ["docker", "ps", "-a", "--filter", "name=sim525_", "--format", "{{.Names}}"],
        ["docker", "volume", "ls", "--filter", "name=sim525_", "--format", "{{.Name}}"],
    ):
        result = _run(argv)
        if result.returncode != 0:
            raise DrillError(f"'{' '.join(argv[:3])}' failed: {result.stderr.strip()}")
        found.extend(n.strip() for n in result.stdout.splitlines() if n.strip())
    return found


def ready_argv() -> list[str]:
    """Build the pg_isready probe of the FINAL server.

    The image's entrypoint first runs a temporary server on the unix socket
    only (listen_addresses=''), and pg_isready over the socket answers 0 in
    that phase. The final server listens on loopback, even with no network, so
    a probe over 127.0.0.1 answers only once the real server is up.
    """
    return ["docker", "exec", NAME, "pg_isready", "-h", "127.0.0.1", "-U", PG_USER, "-d", PG_DB]


def wait_ready(
    timeout_s: float = READY_TIMEOUT_S,
    poll_s: float = READY_POLL_S,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Poll pg_isready in the scratch cluster until it answers or the time runs out."""
    pause = sleep if sleep is not None else _sleep
    deadline = clock() + timeout_s
    while True:
        result = _run(ready_argv())
        if result.returncode == 0:
            return True
        if clock() >= deadline:
            return False
        pause(poll_s)


def _step(set_id: str, started: float, code: int, ok: bool, output: str) -> dict[str, Any]:
    return {
        "set": set_id,
        "seconds": round(time.monotonic() - started, 1),
        "ok": ok,
        "exit_code": code,
        "output_tail": tail_lines(output),
    }


def _restore_postgres(set_id: str, set_dir: Path, timeout_s: float) -> dict[str, Any]:
    started = time.monotonic()
    log(f"starting the scratch cluster {NAME} for postgres set {set_id}")
    run = _run(docker_run_argv(set_dir, REPO / "db" / "backup"))
    if run.returncode != 0:
        output = run.stdout + run.stderr
        log(f"docker run failed (exit {run.returncode})")
        return _step(set_id, started, run.returncode, False, output)
    if not wait_ready(timeout_s=READY_TIMEOUT_S, poll_s=READY_POLL_S):
        log(f"the scratch cluster did not answer pg_isready in {READY_TIMEOUT_S:.0f} s")
        return _step(set_id, started, -1, False, "pg_isready timed out\n")
    log("restoring the dump with pg_restore.sh")
    result = _run(
        ["docker", "exec", "-u", "postgres", NAME, "sh", RESTORE_SCRIPT, "/restore"],
        capture=False,
        timeout=timeout_s,
    )
    output = result.stdout + result.stderr
    return _step(set_id, started, result.returncode, restore_ok(result.returncode, output), output)


def verify_argv(data_set: str, container_root: str = LOCAL_ROOT) -> list[str]:
    """Build the argv of the data verify in the jobs container.

    The local root keeps the argv of the scheduled backups (SIM-525). Any
    other root, such as the off-site root (SIM-568), adds
    ``--backups-root <root>``.
    """
    argv = ["docker", "compose", "run", "--rm", "-T", "jobs", "python", "-m"]
    argv += ["pipeline.backup.data_stores", "--verify-set", data_set]
    if container_root != LOCAL_ROOT:
        argv += ["--backups-root", container_root]
    return argv


def _verify_data(
    data_set: str, timeout_s: float, container_root: str = LOCAL_ROOT
) -> dict[str, Any]:
    started = time.monotonic()
    log(
        f"verifying the duckdb and bundle copies (set {data_set}) "
        f"under {container_root} in the jobs container"
    )
    argv = verify_argv(data_set, container_root)
    result = _run(argv, cwd=REPO, capture=False, timeout=timeout_s)
    output = result.stdout + result.stderr
    return _step(data_set, started, result.returncode, result.returncode == 0, output)


def _cleanup() -> None:
    log(f"removing the scratch container and volume {NAME}")
    for argv in (["docker", "rm", "-f", NAME], ["docker", "volume", "rm", NAME]):
        result = _run(argv)
        if result.returncode != 0:
            log(f"'{' '.join(argv)}' failed: {result.stderr.strip()}")


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restore a backup set into a scratch cluster and verify the data copies."
    )
    parser.add_argument("--set", default="latest", help="postgres set TS or 'latest'")
    parser.add_argument("--data-set", default="latest", help="duckdb/bundle set TS or 'latest'")
    parser.add_argument(
        "--backup-dir",
        default=None,
        help="the backup folder (default: BACKUP_DIR from the shell, then .env)",
    )
    parser.add_argument(
        "--offsite",
        action="store_true",
        help="restore from the off-site copy (OFFSITE_BACKUP_DIR from the shell, then .env)",
    )
    parser.add_argument(
        "--keep-cluster", action="store_true", help="leave the scratch cluster for inspection"
    )
    parser.add_argument("--skip-data", action="store_true", help="skip the duckdb/bundle verify")
    parser.add_argument("--timeout-s", type=float, default=7200.0, help="per-step timeout")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the drill and return its exit code."""
    args = _parse(argv)
    compose_dir = resolve_backup_dir(None, REPO)
    offsite_dir = resolve_offsite_dir(None, REPO)
    backup_dir = offsite_dir if args.offsite else resolve_backup_dir(args.backup_dir, REPO)
    # The off-site copy (SIM-568): --offsite, or a --backup-dir that is the
    # off-site folder. The local folder wins when the two are one folder.
    offsite = args.offsite or (backup_dir == offsite_dir and backup_dir != compose_dir)
    source = "offsite" if offsite else "local"
    container_root = OFFSITE_ROOT if offsite else LOCAL_ROOT
    try:
        if args.offsite and args.backup_dir:
            flagged = resolve_backup_dir(args.backup_dir, REPO)
            if flagged != offsite_dir:
                raise DrillError(
                    f"--offsite reads {offsite_dir} (OFFSITE_BACKUP_DIR), "
                    f"not --backup-dir {flagged}; drop one of the two"
                )
        found = leftovers()
        if found:
            raise DrillError(
                "a scratch container or volume already exists: "
                + ", ".join(found)
                + f" (remove it: docker rm -f {NAME}; docker volume rm {NAME})"
            )
        set_id = pick_set(backup_dir / "postgres", args.set)
        data_set_id: str | None = None
        if not args.skip_data:
            # The jobs container reads /backups through compose's BACKUP_DIR
            # and /offsite through OFFSITE_BACKUP_DIR (SIM-568), so the verify
            # only means something when this folder is one of the two.
            if not offsite and compose_dir != backup_dir:
                raise DrillError(
                    f"--backup-dir {backup_dir} is not a folder the jobs container "
                    f"reads ({compose_dir}, BACKUP_DIR; {offsite_dir}, "
                    "OFFSITE_BACKUP_DIR); pass --skip-data or drop --backup-dir"
                )
            data_set_id = pick_data_set(backup_dir, args.data_set)
        if not (REPO / "db" / "backup").is_dir():
            raise DrillError(f"{REPO / 'db' / 'backup'} is missing")
    except DrillError as exc:
        print(f"backup_drill: refused: {exc}", file=sys.stderr)
        return 2

    started_at = datetime.now(UTC)
    drill_id = started_at.strftime("%Y%m%dT%H%M%SZ")
    log(f"drill {drill_id}: backup folder {backup_dir} (source {source})")
    postgres: dict[str, Any] | None = None
    data: dict[str, Any] | None = None
    try:
        postgres = _restore_postgres(set_id, backup_dir / "postgres" / set_id, args.timeout_s)
        if data_set_id is not None:
            data = _verify_data(data_set_id, args.timeout_s, container_root)
    finally:
        if args.keep_cluster:
            log(f"keeping {NAME}; remove it with: docker rm -f {NAME}; docker volume rm {NAME}")
        else:
            _cleanup()

    ok = bool(postgres and postgres["ok"]) and (args.skip_data or bool(data and data["ok"]))
    report = {
        "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "postgres": postgres,
        "data": data,
        "ok": ok,
    }
    drills = backup_dir / "drills"
    drills.mkdir(parents=True, exist_ok=True)
    report_path = drills / f"{drill_id}.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print()
    print(f"Restore drill {drill_id}: {'PASS' if ok else 'FAIL'}")
    for label, part in (("postgres", postgres), ("data", data)):
        if part is None:
            print(f"  {label:<9} skipped")
            continue
        verdict = "ok" if part["ok"] else "FAILED"
        print(
            f"  {label:<9} set {part['set']}: {verdict} "
            f"(exit {part['exit_code']}, {part['seconds']} s)"
        )
    print(f"  source    {source} ({backup_dir})")
    print(f"  report    {report_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
