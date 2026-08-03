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
    "CHILD_SUMMARY: {'attempted': 2, 'loaded': 2, 'failed': 0}\n",
    "CHILD_FAILED: 0\n",
    "CHILD_COMPLETE\n",
]

# A season that reaches the end of its schedule with one game failed. This is the
# shape that lost game 824014 in the 2026 sweep: the driver saw CHILD_COMPLETE and
# stopped, but the failed game is deliberately NOT in the progress file, so nothing
# ever went back for it.
CHILD_LINES_WITH_FAILURE = [
    "INFO  === Season 2026 ===\n",
    "INFO    Reloading game 824014\n",
    "ERROR   reload failed for game 824014 (1 consecutive): integer out of range\n",
    "CHILD_SUMMARY: {'attempted': 1626, 'loaded': 1625, 'failed': 1}\n",
    "CHILD_FAILED: 1\n",
    "CHILD_COMPLETE\n",
]


@pytest.fixture
def run_attempt(monkeypatch, tmp_path):
    """Call _run_attempt against a fake child; return (stdout_text, rc, complete)."""

    def _run(lines=CHILD_LINES, returncode=0, quiet=False, capsys=None):
        proc = _FakeProc(lines, returncode)
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: proc)
        rc, complete, failed = sweep._run_attempt(2018, tmp_path / "2018.txt", True, quiet)
        return proc, rc, complete, failed

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
        _, rc, complete, _failed = run_attempt()
        assert complete is True
        assert rc == 0

    def test_crash_is_reported_with_its_exit_code(self, run_attempt):
        lines = ["INFO  Reloading game 1\n", "Fatal Python error: Executing a cache.\n"]
        _, rc, complete, _failed = run_attempt(lines=lines, returncode=139)
        assert complete is False, "no CHILD_COMPLETE marker => the attempt crashed"
        assert rc == 139

    def test_the_pipe_is_closed_and_the_child_reaped(self, run_attempt):
        """An unclosed pipe plus an unreaped child leaks a handle and a zombie per
        attempt, and a season can take hundreds of attempts."""
        proc, _, _, _ = run_attempt()
        assert proc.stdout.closed is True
        assert proc.waited is True


class TestFailuresWithinACompletedSeason:
    """SIM-447: reaching the end of the schedule is NOT the same as loading every
    game. ``_dispatch_game`` contains per-game failures by design, so a season can
    report COMPLETE with games still outstanding — and since the fix, those games
    are deliberately absent from the progress file. The driver has to notice."""

    def test_failed_count_is_reported_to_the_driver(self, run_attempt):
        _, _, complete, failed = run_attempt(lines=CHILD_LINES_WITH_FAILURE)
        assert complete is True, "the season did reach the end of its schedule"
        assert failed == 1, "but one game failed and is not in the progress file"

    def test_a_clean_season_reports_zero_failures(self, run_attempt):
        _, _, complete, failed = run_attempt()
        assert (complete, failed) == (True, 0)

    def test_a_missing_marker_is_not_mistaken_for_failures(self, run_attempt):
        """Older children, or a crash before the marker prints, must default to 0
        rather than to a truthy value that would retry a finished season forever."""
        lines = [ln for ln in CHILD_LINES if "CHILD_FAILED" not in ln]
        _, _, complete, failed = run_attempt(lines=lines)
        assert complete is True
        assert failed == 0

    def test_a_malformed_marker_does_not_crash_the_driver(self, run_attempt):
        lines = [ln.replace("CHILD_FAILED: 0", "CHILD_FAILED: ???") for ln in CHILD_LINES]
        _, _, complete, failed = run_attempt(lines=lines)
        assert complete is True
        assert failed == 0

    def test_the_child_emits_the_marker_the_driver_parses(self):
        """The child runs as an embedded SOURCE STRING, so the producer and the
        consumer of ``CHILD_FAILED`` live in different halves of this file and can
        drift apart with nothing failing. If the child stops emitting it, the
        driver silently reads 0 failures forever and we are back to the 824014 bug
        — every canned-output test would still pass. Bind the two halves."""
        import inspect

        assert "CHILD_FAILED:" in sweep._CHILD, "the child must EMIT the marker"
        assert 'int(s.get("failed", 0))' in sweep._CHILD, (
            "the marker must carry the real failed count from the summary"
        )
        assert "CHILD_FAILED:" in inspect.getsource(sweep._run_attempt), (
            "the driver must PARSE the same marker the child emits"
        )

    def test_the_driver_retries_rather_than_breaking_on_a_failed_game(self):
        """Guards the actual control flow: `if complete and not failed` must gate
        the break. `if complete` alone is what lost game 824014."""
        import inspect

        src = inspect.getsource(sweep.main)
        assert "if complete and not failed:" in src, (
            "the break must require BOTH completion and zero failures — otherwise a "
            "season that ends with a failed game is never retried"
        )


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
