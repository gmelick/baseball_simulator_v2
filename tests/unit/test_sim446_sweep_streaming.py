"""SIM-446: ``scripts/resumable_sweep.py`` must STREAM its child's output.

Why this file exists: the wrapper runs each sweep attempt as a subprocess and has
to capture stdout (it reads the stream to spot the completion marker and the fatal
-error signature). An earlier version treated "capture" as licence to *swallow* —
it printed only ``CHILD_SUMMARY`` and ``Fatal Python error`` lines. A season takes
hours, so the operator saw nothing at all and could not tell a working run from a
hung one. Silence is a real defect in a long-running operational tool.

These tests drive ``_run_attempt`` with a fake subprocess, so they assert on
observable behaviour rather than on source text.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "resumable_sweep.py"


def _load_module():
    """Load the operational script by path — scripts/ is not an importable package."""
    spec = importlib.util.spec_from_file_location("resumable_sweep", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["resumable_sweep"] = mod
    spec.loader.exec_module(mod)
    return mod


sweep = _load_module()


class _FakeStdout:
    """A pipe-like object yielding canned lines, then EOF (empty string)."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self.closed = False

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""

    def close(self) -> None:
        self.closed = True


class _FakeProc:
    def __init__(self, lines: list[str], returncode: int = 0) -> None:
        self.stdout = _FakeStdout(lines)
        self.returncode = returncode
        self.waited = False

    def wait(self) -> int:
        self.waited = True
        return self.returncode


CHILD_LINES = [
    "INFO  === Season 2018 ===\n",
    "INFO    Reloading game 529401\n",
    "INFO    game 529401: 291 inserted, 0 skipped (hard errors), 2 flagged\n",
    "INFO    Reloading game 529402\n",
    "CHILD_SUMMARY: {'attempted': 2, 'loaded': 2}\n",
    "CHILD_COMPLETE\n",
]


@pytest.fixture
def run_attempt(monkeypatch, tmp_path):
    """Call _run_attempt against a fake child; return (stdout_text, rc, complete)."""

    def _run(lines=CHILD_LINES, returncode=0, quiet=False, capsys=None):
        proc = _FakeProc(lines, returncode)
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: proc)
        rc, complete = sweep._run_attempt(2018, tmp_path / "2018.txt", True, quiet)
        return proc, rc, complete

    return _run


class TestChildOutputIsStreamed:
    def test_per_game_lines_reach_stdout_by_default(self, run_attempt, capsys):
        """THE REGRESSION GUARD. These are the lines the operator watches to know
        the sweep is alive; swallowing them makes a working run look like a hang."""
        run_attempt()
        out = capsys.readouterr().out
        assert "Reloading game 529401" in out
        assert "game 529401: 291 inserted" in out
        assert "Reloading game 529402" in out

    def test_summary_and_season_banner_also_reach_stdout(self, run_attempt, capsys):
        run_attempt()
        out = capsys.readouterr().out
        assert "=== Season 2018 ===" in out
        assert "CHILD_SUMMARY" in out

    def test_completion_marker_is_still_detected_while_streaming(self, run_attempt):
        """Streaming must not cost us the marker — without it the driver would
        treat a finished season as a crash and retry it forever."""
        _, rc, complete = run_attempt()
        assert complete is True
        assert rc == 0

    def test_crash_is_reported_with_its_exit_code(self, run_attempt):
        lines = ["INFO  Reloading game 1\n", "Fatal Python error: Executing a cache.\n"]
        _, rc, complete = run_attempt(lines=lines, returncode=139)
        assert complete is False, "no CHILD_COMPLETE marker => the attempt crashed"
        assert rc == 139

    def test_the_pipe_is_closed_and_the_child_reaped(self, run_attempt):
        """An unclosed pipe plus an unreaped child leaks a handle and a zombie per
        attempt, and a season can take hundreds of attempts."""
        proc, _, _ = run_attempt()
        assert proc.stdout.closed is True
        assert proc.waited is True


class TestQuietMode:
    def test_quiet_suppresses_per_game_noise(self, run_attempt, capsys):
        run_attempt(quiet=True)
        out = capsys.readouterr().out
        assert "game 529401: 291 inserted" not in out

    def test_quiet_still_shows_summaries_and_fatal_errors(self, run_attempt, capsys):
        run_attempt(lines=[*CHILD_LINES, "Fatal Python error: Executing a cache.\n"], quiet=True)
        out = capsys.readouterr().out
        assert "CHILD_SUMMARY" in out
        assert "Fatal Python error" in out

    def test_quiet_emits_a_heartbeat_so_it_is_never_fully_silent(self, run_attempt, capsys):
        """Even suppressed, a multi-hour attempt must prove it is alive."""
        n = sweep._HEARTBEAT_EVERY
        lines = [f"INFO    Reloading game {i}\n" for i in range(n + 1)]
        run_attempt(lines=lines, quiet=True)
        out = capsys.readouterr().out
        assert f"… {n} games this attempt" in out
