"""Run the corrective reload sweep to its end, and restart it after an interpreter crash.

The sweep's process dies now and then at a random point with a CPython memory
fault: ``Fatal Python error`` on Windows, exit 139 in a Linux container. The root
cause is unknown. The loader reads through stdlib ``urllib``, not ``requests``,
which made the fault rare; nothing proves it gone.

WHY A WRAPPER IS SAFE
---------------------
The sweep resumes by design. ``reload_game`` deletes and re-inserts one game in
a single transaction, and the shrink guard refuses to replace a game with fewer
rows than it removed. So a crash cannot corrupt a game; it can only stop the run.
This script records every game that loads and skips it on the next attempt, so a
crash costs no progress. It does NOT fix the fault.

The child process records a game only when the game loads: ``_dispatch_game``
contains each game's failure, so "the dispatcher returned" does not mean "the
game loaded". A season that ends with failed games runs again, and the new
attempt retries just those games. An attempt with zero progress stops the run,
because a game that fails every time would make it spin forever.

USAGE
-----
    python ingest/games/resumable_sweep.py --season 2017
    python ingest/games/resumable_sweep.py --season 2017 --end-season 2025
    python ingest/games/resumable_sweep.py --season 2017 --max-attempts 200

The progress lives in ``.sweep_progress/<season>.txt`` (one game_pk per line), so
a run can stop and resume at any time, across reboots too.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROGRESS_DIR = REPO / ".sweep_progress"

# How often --quiet emits a "still alive" line, in games.
_HEARTBEAT_EVERY = 25

# The child does the work. It runs as a SUBPROCESS because the fault kills the
# interpreter, which no in-process handler can catch.
_CHILD = r"""
import logging, os, sys, pathlib
sys.path.insert(0, r"{repo}")
season = int(sys.argv[1])
progress = pathlib.Path(sys.argv[2])

# The per-game log lines are the progress the driver streams and counts.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from ingest.games.reload_games import _host_dsn, _load_env

env = _load_env()
os.environ["BASEBALL_DB_DSN"] = _host_dsn(env) if {native} else env["BASEBALL_DB_DSN"]

done = set()
if progress.exists():
    done = {{int(x) for x in progress.read_text().split() if x.strip()}}

from ingest.games.load_games import HistoricalDataLoader

loader = HistoricalDataLoader()
original = loader._dispatch_game

def dispatch(game_pk, season_, cache, *, reload, summary):
    if game_pk in done:
        summary["skipped"] = summary.get("skipped", 0) + 1
        return
    # SIM-446: record a game ONLY when it actually loaded.
    #
    # _dispatch_game deliberately SWALLOWS per-game exceptions (it bumps
    # summary["failed"] and returns) so one bad feed cannot kill a multi-hour
    # run. So "the dispatcher returned" does NOT mean "the game loaded". An
    # earlier version recorded progress on return, which permanently marked
    # FAILED games as done -- every later attempt skipped them, the sweep
    # reported success, and those games silently kept their pre-SIM-440 rows.
    # That cost 4 games across the 2018-2025 sweep, found only by diffing the
    # ledger against raw.pitches. Gate on the loaded counter instead.
    before = summary.get("loaded", 0)
    original(game_pk, season_, cache, reload=reload, summary=summary)
    if summary.get("loaded", 0) <= before:
        return  # failed or skipped -- leave it out so the next attempt retries it
    with progress.open("a") as fh:
        fh.write(str(game_pk) + "\n")
        fh.flush()
        os.fsync(fh.fileno())

loader._dispatch_game = dispatch
try:
    s = loader.refresh_seasons(season, season, reload=True)
    print("CHILD_SUMMARY:", s, flush=True)
    # Machine-readable, so the driver never has to parse a dict repr. A season
    # can finish with per-game failures -- those games are NOT in the progress
    # file, so the driver must run another attempt to pick them up.
    print("CHILD_FAILED:", int(s.get("failed", 0)), flush=True)
    print("CHILD_COMPLETE", flush=True)
finally:
    loader.close()
"""


def _done_count(progress: Path) -> int:
    """The number of games the progress file records as done."""
    return len(progress.read_text().split()) if progress.exists() else 0


def _run_attempt(season: int, progress: Path, native: bool, quiet: bool) -> tuple[int, bool, int]:
    """Run one child attempt; return ``(exit_code, saw_complete_marker, failed_count)``.

    The child's output streams to stdout as it arrives. The driver captures it to
    find the completion marker and the crash text, but never swallows it: the
    per-game log lines are the progress, and a multi-hour season with no output
    looks the same as a hang.

    ``--quiet`` prints only the summaries and the crash text, plus a heartbeat
    every :data:`_HEARTBEAT_EVERY` games, so the run is never silent.
    """
    code = _CHILD.format(repo=str(REPO), native=str(native))
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", code, str(season), str(progress)],
        cwd=str(REPO),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,  # line-buffered: each line arrives at once, not in blocks
    )
    complete = False
    games = 0
    failed = 0
    assert proc.stdout is not None
    # readline(), not `for line in proc.stdout`: the iterator's read-ahead can
    # hold lines back.
    for line in iter(proc.stdout.readline, ""):
        if "CHILD_COMPLETE" in line:
            complete = True
        if "CHILD_FAILED:" in line:
            try:
                failed = int(line.split("CHILD_FAILED:", 1)[1].strip())
            except ValueError:
                failed = 0
        if "Reloading game" in line:
            games += 1

        if not quiet:
            print(line.rstrip(), flush=True)
        elif "CHILD_SUMMARY" in line or "Fatal Python error" in line:
            print("   " + line.rstrip(), flush=True)
        elif games and games % _HEARTBEAT_EVERY == 0 and "Reloading game" in line:
            print(f"   … {games} games this attempt", flush=True)

    proc.stdout.close()
    proc.wait()
    return proc.returncode, complete, failed


def main() -> int:
    """Sweep each season until every game loads; return the exit code."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--end-season", type=int, default=None, help="inclusive; defaults to --season")
    ap.add_argument("--max-attempts", type=int, default=500, help="per season")
    ap.add_argument(
        "--in-docker",
        action="store_true",
        help="child connects via the compose DSN instead of 127.0.0.1:DB_HOST_PORT",
    )
    ap.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the child's per-game log; keep summaries, fatal errors and a "
        f"heartbeat every {_HEARTBEAT_EVERY} games (default: stream everything)",
    )
    args = ap.parse_args()

    PROGRESS_DIR.mkdir(exist_ok=True)
    end = args.end_season or args.season
    overall_start = time.time()

    for season in range(args.season, end + 1):
        progress = PROGRESS_DIR / f"{season}.txt"
        started = _done_count(progress)
        print(f"\n=== season {season} — {started} game(s) already done ===", flush=True)

        for attempt in range(1, args.max_attempts + 1):
            before = _done_count(progress)
            t0 = time.time()
            rc, complete, failed = _run_attempt(season, progress, not args.in_docker, args.quiet)
            after = _done_count(progress)
            gained = after - before
            mins = (time.time() - t0) / 60

            if complete and not failed:
                print(
                    f"  attempt {attempt}: COMPLETE — {after} games total "
                    f"(+{gained} this attempt, {mins:.1f} min)",
                    flush=True,
                )
                break

            if complete:
                # The season reached its end, but some games failed:
                # _dispatch_game contains them, and the progress file leaves them
                # out, so ANOTHER attempt retries exactly those games. A break
                # here reports the season COMPLETE and leaves each failed game on
                # its old rows. A transient fault succeeds on a retry; a fault that
                # repeats gains nothing and trips the zero-progress check below.
                print(
                    f"  attempt {attempt}: reached the end of the season but "
                    f"{failed} game(s) FAILED (+{gained} loaded, {mins:.1f} min) — "
                    f"retrying just those",
                    flush=True,
                )
            else:
                print(
                    f"  attempt {attempt}: crashed rc={rc} after +{gained} games "
                    f"({after} total, {mins:.1f} min) — resuming",
                    flush=True,
                )

            if gained == 0:
                # No progress: a game fails every time, which is not the random
                # crash. A retry would spin forever.
                print(
                    "  STOPPING: an attempt made zero progress, so a game is failing "
                    "deterministically rather than transiently — retrying would spin "
                    "forever. Run `python ingest/games/reload_games.py --dry-run` to see "
                    "exactly which games are outstanding, then investigate one.",
                    flush=True,
                )
                return 1
        else:
            print(f"  exhausted --max-attempts for season {season}", flush=True)
            return 1

    total = sum(_done_count(PROGRESS_DIR / f"{s}.txt") for s in range(args.season, end + 1))
    print(
        f"\nALL SEASONS COMPLETE — {total} games in {(time.time() - overall_start) / 60:.1f} min",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
