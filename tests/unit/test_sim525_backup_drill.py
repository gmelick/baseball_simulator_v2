"""The restore drill for the scheduled backups (SIM-525).

``scripts/backup_drill.py`` runs on the host. It restores the newest Postgres
backup set into a scratch cluster, verifies the DuckDB and bundle copies, and
removes the scratch cluster. These tests replace every docker call with a fake
seam, so they run on any OS and never touch Docker.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "backup_drill.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("backup_drill_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


drill = _load()


def _make_set(root: Path, store: str, ts: str, complete: bool = True) -> Path:
    path = root / store / ts
    path.mkdir(parents=True)
    if complete:
        (path / "manifest.json").write_text("{}", encoding="utf-8")
    return path


class FakeDocker:
    """A stand-in for the ``_run`` seam that records every call."""

    def __init__(
        self,
        leftovers: str = "",
        volumes: str = "",
        restore_rc: int = 0,
        restore_out: str = "raw.games 10 OK\nRESTORE OK /restore\n",
        verify_rc: int = 0,
        run_rc: int = 0,
    ) -> None:
        self.calls: list[list[str]] = []
        self.leftovers = leftovers
        self.volumes = volumes
        self.restore_rc = restore_rc
        self.restore_out = restore_out
        self.verify_rc = verify_rc
        self.run_rc = run_rc

    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        args = list(argv)
        self.calls.append(args)
        out, rc = "", 0
        if args[:3] == ["docker", "ps", "-a"]:
            out = self.leftovers
        elif args[:3] == ["docker", "volume", "ls"]:
            out = self.volumes
        elif args[:2] == ["docker", "run"]:
            rc = self.run_rc
            out = "abc123\n"
        elif "pg_isready" in args:
            rc = 0
        elif "/opt/baseball/backup/pg_restore.sh" in args:
            rc, out = self.restore_rc, self.restore_out
        elif args[:3] == ["docker", "compose", "run"]:
            rc, out = self.verify_rc, "VERIFY OK\n"
        return subprocess.CompletedProcess(args, rc, out, "")

    def find(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


# ---------------------------------------------------------------- .env parser


def test_the_env_parser_reads_keys_strips_quotes_and_skips_comments(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        'BACKUP_DIR="C:/Users/grego/Documents/baseball_backups"\n'
        "OTHER='single'\n"
        "  SPACED = value  \n"
        "# BACKUP_KEEP=3\n"
        "NOEQUALS\n",
        encoding="utf-8",
    )
    values = drill.read_env_file(env)
    assert values["BACKUP_DIR"] == "C:/Users/grego/Documents/baseball_backups"
    assert values["OTHER"] == "single"
    assert values["SPACED"] == "value"
    assert "BACKUP_KEEP" not in values
    assert "NOEQUALS" not in values


def test_a_missing_env_file_reads_empty(tmp_path: Path) -> None:
    assert drill.read_env_file(tmp_path / "absent.env") == {}


def test_the_backup_dir_comes_from_the_flag_then_env_then_default(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("BACKUP_DIR=elsewhere/sets\n", encoding="utf-8")
    flag = tmp_path / "flagged"
    assert drill.resolve_backup_dir(str(flag), tmp_path) == flag.resolve()
    assert drill.resolve_backup_dir(None, tmp_path) == (tmp_path / "elsewhere/sets").resolve()
    (tmp_path / ".env").write_text("# nothing\n", encoding="utf-8")
    assert drill.resolve_backup_dir(None, tmp_path) == (tmp_path / "backups").resolve()


# ---------------------------------------------------------------- set picker


def test_the_picker_takes_the_newest_complete_set(tmp_path: Path) -> None:
    _make_set(tmp_path, "postgres", "20261013T100000Z")
    _make_set(tmp_path, "postgres", "20261014T100000Z")
    _make_set(tmp_path, "postgres", "20261015T100000Z", complete=False)
    _make_set(tmp_path, "postgres", "20261016T100000Z.partial", complete=False)
    assert drill.pick_set(tmp_path / "postgres", "latest") == "20261014T100000Z"


def test_the_picker_takes_an_explicit_complete_set(tmp_path: Path) -> None:
    _make_set(tmp_path, "postgres", "20261013T100000Z")
    _make_set(tmp_path, "postgres", "20261014T100000Z")
    assert drill.pick_set(tmp_path / "postgres", "20261013T100000Z") == "20261013T100000Z"


def test_the_picker_refuses_a_missing_or_incomplete_set(tmp_path: Path) -> None:
    _make_set(tmp_path, "postgres", "20261015T100000Z", complete=False)
    with pytest.raises(drill.DrillError):
        drill.pick_set(tmp_path / "postgres", "latest")
    with pytest.raises(drill.DrillError):
        drill.pick_set(tmp_path / "postgres", "20261015T100000Z")
    with pytest.raises(drill.DrillError):
        drill.pick_set(tmp_path / "postgres", "20991231T000000Z")
    with pytest.raises(drill.DrillError):
        drill.pick_set(tmp_path / "nothing", "latest")


# ---------------------------------------------------------------- docker run argv


def test_the_docker_run_argv_carries_every_flag_in_order(tmp_path: Path) -> None:
    set_dir = tmp_path / "postgres" / "20261014T100000Z"
    scripts = tmp_path / "db" / "backup"
    argv = drill.docker_run_argv(set_dir, scripts)
    assert argv == [
        "docker",
        "run",
        "-d",
        "--name",
        "sim525_drill_pg",
        "--network",
        "none",
        "-e",
        "POSTGRES_HOST_AUTH_METHOD=trust",
        "-e",
        "POSTGRES_USER=baseball_user",
        "-e",
        "POSTGRES_DB=baseball_sim",
        "-v",
        "sim525_drill_pg:/var/lib/postgresql/data",
        "-v",
        f"{set_dir.resolve().as_posix()}:/restore:ro",
        "-v",
        f"{scripts.resolve().as_posix()}:/opt/baseball/backup:ro",
        "postgres:15-alpine",
    ]
    assert "-p" not in argv
    assert all(not a.startswith("/c/") for a in argv)


def test_a_windows_path_reads_as_a_drive_letter_path() -> None:
    from pathlib import PureWindowsPath

    assert drill.docker_path(PureWindowsPath("C:\\Users\\grego\\b")) == "C:/Users/grego/b"


# ---------------------------------------------------------------- success criteria


def test_the_restore_passes_on_exit_0_and_a_final_restore_ok_line() -> None:
    assert drill.restore_ok(0, "raw.games 10 OK\nRESTORE OK /restore\n")


@pytest.mark.parametrize(
    ("code", "output"),
    [
        (1, "RESTORE OK /restore\n"),
        (0, "pg_restore: warning: errors ignored on restore: 3\nRESTORE OK /restore\n"),
        (0, "RESTORE OK /restore\nsomething after\n"),
        (0, ""),
        (3, "target database is not empty\n"),
    ],
)
def test_the_restore_fails_on_each_violation(code: int, output: str) -> None:
    assert not drill.restore_ok(code, output)


def test_the_tail_keeps_the_last_40_lines() -> None:
    text = "\n".join(f"line {i}" for i in range(100))
    tail = drill.tail_lines(text)
    assert len(tail) == 40
    assert tail[-1] == "line 99"


# ---------------------------------------------------------------- the whole drill


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeDocker) -> Path:
    backups = tmp_path / "backups"
    for store in ("postgres", "duckdb", "bundle"):
        _make_set(backups, store, "20261014T100000Z")
    (tmp_path / "db" / "backup").mkdir(parents=True)
    monkeypatch.setattr(drill, "REPO", tmp_path)
    monkeypatch.setattr(drill, "_run", fake)
    monkeypatch.setattr(drill, "_sleep", lambda s: None)
    return backups


def _report(backups: Path) -> dict[str, Any]:
    reports = sorted((backups / "drills").glob("*.json"))
    assert len(reports) == 1
    data: dict[str, Any] = json.loads(reports[0].read_text(encoding="utf-8"))
    return data


def test_a_good_drill_exits_0_and_writes_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker()
    backups = _setup(tmp_path, monkeypatch, fake)
    code = drill.main(["--backup-dir", str(backups)])
    assert code == 0
    report = _report(backups)
    assert set(report) == {"started_at", "finished_at", "postgres", "data", "ok"}
    assert report["ok"] is True
    for part in ("postgres", "data"):
        assert set(report[part]) == {"set", "seconds", "ok", "exit_code", "output_tail"}
        assert report[part]["ok"] is True
        assert report[part]["exit_code"] == 0
        assert isinstance(report[part]["output_tail"], list)
    assert report["postgres"]["set"] == "20261014T100000Z"
    assert report["data"]["set"] == "20261014T100000Z"
    restore = [c for c in fake.calls if "/opt/baseball/backup/pg_restore.sh" in c]
    assert restore == [
        [
            "docker",
            "exec",
            "-u",
            "postgres",
            "sim525_drill_pg",
            "sh",
            "/opt/baseball/backup/pg_restore.sh",
            "/restore",
        ]
    ]
    verify = fake.find("docker", "compose", "run")
    assert verify == [
        [
            "docker",
            "compose",
            "run",
            "--rm",
            "-T",
            "jobs",
            "python",
            "-m",
            "pipeline.backup.data_stores",
            "--verify-set",
            "20261014T100000Z",
        ]
    ]
    assert fake.find("docker", "rm", "-f", "sim525_drill_pg")
    assert fake.find("docker", "volume", "rm", "sim525_drill_pg")


def test_the_cleanup_runs_after_a_failed_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker(restore_rc=1, restore_out="pg_restore: error: boom\n")
    backups = _setup(tmp_path, monkeypatch, fake)
    code = drill.main(["--backup-dir", str(backups), "--skip-data"])
    assert code == 1
    report = _report(backups)
    assert report["ok"] is False
    assert report["postgres"]["ok"] is False
    assert report["postgres"]["exit_code"] == 1
    assert report["data"] is None
    assert not fake.find("docker", "compose", "run")
    rm_index = fake.calls.index(["docker", "rm", "-f", "sim525_drill_pg"])
    vol_index = fake.calls.index(["docker", "volume", "rm", "sim525_drill_pg"])
    assert rm_index < vol_index


def test_the_cleanup_runs_when_a_step_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker()
    backups = _setup(tmp_path, monkeypatch, fake)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the docker daemon went away")

    monkeypatch.setattr(drill, "wait_ready", boom)
    with pytest.raises(RuntimeError):
        drill.main(["--backup-dir", str(backups)])
    assert fake.find("docker", "rm", "-f", "sim525_drill_pg")
    assert fake.find("docker", "volume", "rm", "sim525_drill_pg")


def test_a_failed_data_verify_fails_the_drill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker(verify_rc=1)
    backups = _setup(tmp_path, monkeypatch, fake)
    assert drill.main(["--backup-dir", str(backups), "--data-set", "20261014T100000Z"]) == 1
    report = _report(backups)
    assert report["postgres"]["ok"] is True
    assert report["data"]["ok"] is False
    assert report["data"]["set"] == "20261014T100000Z"
    assert report["ok"] is False


def test_keep_cluster_skips_the_rm_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDocker()
    backups = _setup(tmp_path, monkeypatch, fake)
    assert drill.main(["--backup-dir", str(backups), "--keep-cluster", "--skip-data"]) == 0
    assert not fake.find("docker", "rm")
    assert not fake.find("docker", "volume", "rm")


@pytest.mark.parametrize(
    ("containers", "volumes"),
    [("sim525_drill_pg\n", ""), ("", "sim525_drill_pg\n")],
)
def test_a_leftover_container_or_volume_refuses_with_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, containers: str, volumes: str
) -> None:
    fake = FakeDocker(leftovers=containers, volumes=volumes)
    backups = _setup(tmp_path, monkeypatch, fake)
    assert drill.main(["--backup-dir", str(backups)]) == 2
    assert not fake.find("docker", "run")
    assert not fake.find("docker", "rm")
    assert not (backups / "drills").exists()
    listing = fake.find("docker", "ps", "-a")[0]
    assert listing == [
        "docker",
        "ps",
        "-a",
        "--filter",
        "name=sim525_",
        "--format",
        "{{.Names}}",
    ]
    assert fake.find("docker", "volume", "ls")[0] == [
        "docker",
        "volume",
        "ls",
        "--filter",
        "name=sim525_",
        "--format",
        "{{.Name}}",
    ]


def test_no_complete_postgres_set_refuses_with_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker()
    backups = _setup(tmp_path, monkeypatch, fake)
    assert drill.main(["--backup-dir", str(backups), "--set", "20991231T000000Z"]) == 2
    assert not fake.find("docker", "run")


def test_the_wait_polls_pg_isready_until_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([1, 1, 0])
    calls: list[list[str]] = []

    def fake_run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), next(answers), "", "")

    clock = iter([0.0, 0.0, 2.0, 4.0, 6.0])
    monkeypatch.setattr(drill, "_run", fake_run)
    slept: list[float] = []
    assert drill.wait_ready(timeout_s=120, poll_s=2, sleep=slept.append, clock=lambda: next(clock))
    assert calls[0] == [
        "docker",
        "exec",
        "sim525_drill_pg",
        "pg_isready",
        "-h",
        "127.0.0.1",
        "-U",
        "baseball_user",
        "-d",
        "baseball_sim",
    ]
    assert len(calls) == 3
    assert slept == [2, 2]


def test_the_wait_gives_up_after_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 2, "", "")

    now = [0.0]

    def sleep(s: float) -> None:
        now[0] += s

    monkeypatch.setattr(drill, "_run", fake_run)
    assert not drill.wait_ready(timeout_s=10, poll_s=2, sleep=sleep, clock=lambda: now[0])


def test_a_cluster_that_never_starts_fails_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker()
    backups = _setup(tmp_path, monkeypatch, fake)
    monkeypatch.setattr(drill, "wait_ready", lambda **kwargs: False)
    assert drill.main(["--backup-dir", str(backups)]) == 1
    report = _report(backups)
    assert report["postgres"]["ok"] is False
    assert fake.find("docker", "volume", "rm", "sim525_drill_pg")


def test_the_probe_asks_the_final_server_over_loopback() -> None:
    argv = drill.ready_argv()
    assert argv[argv.index("-h") + 1] == "127.0.0.1"
    assert argv[argv.index("-d") + 1] == "baseball_sim"


# ---------------------------------------------------------------- the data set


@pytest.mark.parametrize("missing", ["duckdb", "bundle"])
def test_no_complete_data_set_refuses_with_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    fake = FakeDocker()
    backups = _setup(tmp_path, monkeypatch, fake)
    (backups / missing / "20261014T100000Z" / "manifest.json").unlink()
    assert drill.main(["--backup-dir", str(backups)]) == 2
    assert not fake.find("docker", "run")
    assert not fake.find("docker", "compose", "run")


def test_skip_data_needs_no_data_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDocker()
    backups = _setup(tmp_path, monkeypatch, fake)
    (backups / "duckdb" / "20261014T100000Z" / "manifest.json").unlink()
    assert drill.main(["--backup-dir", str(backups), "--skip-data"]) == 0


def test_latest_resolves_to_the_newest_id_complete_in_both_stores(tmp_path: Path) -> None:
    _make_set(tmp_path, "duckdb", "20261013T100000Z")
    _make_set(tmp_path, "bundle", "20261013T100000Z")
    _make_set(tmp_path, "bundle", "20261014T100000Z")
    _make_set(tmp_path, "duckdb", "20261014T100000Z", complete=False)
    assert drill.pick_data_set(tmp_path, "latest") == "20261013T100000Z"
    with pytest.raises(drill.DrillError):
        drill.pick_data_set(tmp_path, "20261014T100000Z")
    assert drill.pick_data_set(tmp_path, "20261013T100000Z") == "20261013T100000Z"


def test_the_resolved_data_set_is_passed_to_the_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker()
    backups = _setup(tmp_path, monkeypatch, fake)
    for store in ("duckdb", "bundle"):
        _make_set(backups, store, "20261015T100000Z")
    assert drill.main(["--backup-dir", str(backups)]) == 0
    verify = fake.find("docker", "compose", "run")[0]
    assert verify[-2:] == ["--verify-set", "20261015T100000Z"]
    assert "latest" not in verify
    assert _report(backups)["data"]["set"] == "20261015T100000Z"


def test_a_backup_dir_unlike_the_compose_folder_refuses_the_data_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker()
    backups = _setup(tmp_path, monkeypatch, fake)
    other = tmp_path / "other"
    for store in ("postgres", "duckdb", "bundle"):
        _make_set(other, store, "20261014T100000Z")
    assert drill.main(["--backup-dir", str(other)]) == 2
    assert not fake.find("docker", "run")
    assert drill.main(["--backup-dir", str(other), "--skip-data"]) == 0
    assert backups.is_dir()


# ---------------------------------------------------------------- the streaming runner


def test_the_streaming_runner_times_out_a_silent_command() -> None:
    import time as _time

    started = _time.monotonic()
    result = drill._run(
        [sys.executable, "-c", "import time; time.sleep(30)"], capture=False, timeout=1.0
    )
    assert _time.monotonic() - started < 15
    assert result.returncode != 0
    assert "timeout" in result.stdout


def test_the_streaming_runner_returns_the_output_and_exit_code() -> None:
    result = drill._run(
        [sys.executable, "-c", "print('one'); print('two'); raise SystemExit(3)"],
        capture=False,
        timeout=30.0,
    )
    assert result.returncode == 3
    assert result.stdout.splitlines() == ["one", "two"]
