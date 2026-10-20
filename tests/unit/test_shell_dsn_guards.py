"""The nightly shell scripts carry no default DSN.

Each script must stop at once, naming ``BASEBALL_DB_DSN``, when the variable is
not set.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

GUARDED = (
    "nightly_finals.sh",
    "nightly_rebuild.sh",
    "nightly_ingest.sh",
    "weekly_refresh.sh",
    "nightly_closing_lines.sh",
)

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None or os.name == "nt",
    reason="POSIX shell scripts (CI and the container run them)",
)


@pytest.mark.parametrize("script", GUARDED)
def test_the_script_names_the_guard(script: str) -> None:
    text = (SCRIPTS / script).read_text(encoding="utf-8")
    assert "${BASEBALL_DB_DSN:?" in text
    assert "baseball" + "_pass" not in text
    assert "${BASEBALL_DB_DSN:-" not in text


@pytest.mark.parametrize("script", GUARDED)
def test_the_script_stops_without_the_dsn(script: str, tmp_path: Path) -> None:
    # A stand-in python records any call: the guard must stop the script first.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    record = tmp_path / "called"
    for name in ("python", "python3"):
        fake = fake_bin / name
        fake.write_text(f'#!/bin/sh\ntouch "{record.as_posix()}"\n', encoding="utf-8")
        fake.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "BASEBALL_DB_DSN"}
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["INGEST_MARK_DIR"] = str(tmp_path / "marks")
    started = time.monotonic()
    proc = subprocess.run(
        ["sh", str(SCRIPTS / script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    elapsed = time.monotonic() - started
    assert proc.returncode != 0
    assert "BASEBALL_DB_DSN" in proc.stderr
    assert elapsed < 5.0
    assert not record.exists()
