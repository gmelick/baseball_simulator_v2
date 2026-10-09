"""SIM-519 Part D — the crash-retry wrapper (``scripts/with_retry.sh``)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "scripts" / "with_retry.sh"
SH = shutil.which("sh")

pytestmark = pytest.mark.skipif(SH is None, reason="no POSIX sh on this host")


def _fake(tmp_path: Path, codes: list[int], *, crash_text: bool = False) -> Path:
    """A command that exits with ``codes[n]`` on its n-th run (a counter file)."""
    counter = tmp_path / "n"
    counter.write_text("0")
    script = tmp_path / "fake.sh"
    cases = "\n".join(
        f"  {i}) {'echo "Fatal Python error: Segmentation fault"; ' if crash_text and c else ''}exit {c} ;;"
        for i, c in enumerate(codes)
    )
    script.write_text(
        "#!/usr/bin/env sh\n"
        f'n=$(cat "{counter.as_posix()}")\n'
        f'echo $((n + 1)) > "{counter.as_posix()}"\n'
        'echo "run $n"\n'
        'case "$n" in\n'
        f"{cases}\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        newline="\n",
    )
    return script


def _run(max_attempts: int, script: Path) -> tuple[int, str, int]:
    env = {**os.environ, "RETRY_SLEEP_S": "0"}
    proc = subprocess.run(
        [SH, WRAPPER.as_posix(), str(max_attempts), SH, script.as_posix()],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    runs = int((script.parent / "n").read_text().strip())
    return proc.returncode, proc.stdout + proc.stderr, runs


def test_two_crashes_then_success(tmp_path: Path) -> None:
    code, out, runs = _run(6, _fake(tmp_path, [139, 139, 0]))
    assert code == 0
    assert runs == 3
    assert "attempt 3/6" in out


def test_a_real_failure_stops_at_once(tmp_path: Path) -> None:
    code, out, runs = _run(6, _fake(tmp_path, [2]))
    assert code == 2
    assert runs == 1
    assert "not a crash" in out


def test_fatal_python_error_text_counts_as_a_crash(tmp_path: Path) -> None:
    code, _out, runs = _run(6, _fake(tmp_path, [1, 0], crash_text=True))
    assert code == 0
    assert runs == 2


def test_gives_up_after_max_attempts(tmp_path: Path) -> None:
    code, out, runs = _run(3, _fake(tmp_path, [139, 139, 139, 139]))
    assert code == 139
    assert runs == 3
    assert "giving up" in out


def test_usage_error() -> None:
    proc = subprocess.run([SH, WRAPPER.as_posix(), "3"], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 2
