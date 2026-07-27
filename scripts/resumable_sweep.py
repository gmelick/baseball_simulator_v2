"""
resumable_sweep.py — drive the corrective reload sweep to completion despite a
stochastic interpreter crash.

THE PROBLEM
-----------
The sweep used to die with::

    Fatal Python error: _PyEval_EvalFrameDefault: Executing a cache.

CPython's evaluation loop dispatched into an inline-cache entry instead of a real
instruction — i.e. the bytecode / inline cache of ``_build_row_dict`` was being
corrupted. It presented as SIGSEGV (exit 139) in Linux containers and as this
fatal error natively on Windows.

**It is STOCHASTIC.** Observed failure points across runs: 3, 5, 66, 191, 193,
318 and 405 games. Any single short A/B of this is worthless; only season-scale
runs or repeated trials can distinguish configurations.

Ruled out by direct measurement (not inference):

  * memory exhaustion .......... flat 67 MiB, OOMKilled=false
  * Docker / WSL2 .............. reproduces natively on Windows, no container
  * Riot Vanguard (host BSODs) . vgk stopped, zero bugchecks, still crashes
  * psycopg2-binary dual SSL ... source-built psycopg2, 1 OpenSSL, still crashes
  * numpy / OpenBLAS ........... import removed entirely, still crashes
  * charset_normalizer calls ... measured ZERO (the API sends charset=UTF-8)
  * simplejson C speedups ...... forced stdlib json, crashed at 193
  * psycopg2 connection churn .. 20,000 borrow/return cycles clean
  * TLS handshake churn ........ pg_stat_ssl shows ssl=False

**SIM-446 addressed it** by moving the HTTP transport to stdlib
``urllib.request`` (the loader's default since), which completed a full 2,468-game
season cleanly. This wrapper is therefore no longer the primary defence — but it
is kept, because that evidence is statistical rather than a proven root cause, and
one transient in-flight corruption still surfaced during the clean run.

WHY A WRAPPER IS A LEGITIMATE ANSWER HERE
-----------------------------------------
The sweep is *idempotent and resumable by construction*: ``reload_game`` deletes
and re-inserts one game inside a single transaction, and the shrink guard refuses
to replace a game with fewer rows than it removed. A crash therefore cannot
corrupt a game — it can only stop the run. So retrying is safe, and the only cost
of a crash is lost progress, which this script eliminates by recording every
completed ``game_pk`` and skipping it on the next attempt.

It does NOT fix the underlying fault and should not be mistaken for one — it is
the belt to SIM-446's braces. See docs/audit for the investigation record.

USAGE
-----
    python scripts/resumable_sweep.py --season 2017
    python scripts/resumable_sweep.py --season 2017 --end-season 2025
    python scripts/resumable_sweep.py --season 2017 --max-attempts 200

Progress lives in ``.sweep_progress/<season>.txt`` (one game_pk per line) so a
run can be interrupted and resumed at any time, including across reboots.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROGRESS_DIR = REPO / ".sweep_progress"

# How often --quiet emits a "still alive" line, in games.
_HEARTBEAT_EVERY = 25

# The child does the real work. It is run as a SUBPROCESS on purpose: the fault
# kills the interpreter outright, so it cannot be caught in-process.
_CHILD = r"""
import os, sys, pathlib, re
sys.path.insert(0, r"{repo}")
season = int(sys.argv[1])
progress = pathlib.Path(sys.argv[2])

env = {{}}
for line in pathlib.Path(r"{repo}", ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
dsn = env["BASEBALL_DB_DSN"]
if {native}:
    dsn = re.sub(r"@[^/:]+:\d+/", "@127.0.0.1:" + env.get("DB_HOST_PORT", "5432") + "/", dsn)
os.environ["BASEBALL_DB_DSN"] = dsn

done = set()
if progress.exists():
    done = {{int(x) for x in progress.read_text().split() if x.strip()}}

from pipeline.etl.etl_historical_loader import HistoricalDataLoader

loader = HistoricalDataLoader()
original = loader._dispatch_game

def dispatch(game_pk, season_, cache, *, reload, summary):
    if game_pk in done:
        summary["skipped"] = summary.get("skipped", 0) + 1
        return
    original(game_pk, season_, cache, reload=reload, summary=summary)
    # Record only AFTER the game's transaction has committed.
    with progress.open("a") as fh:
        fh.write(str(game_pk) + "\n")
        fh.flush()
        os.fsync(fh.fileno())

loader._dispatch_game = dispatch
try:
    s = loader.refresh_seasons(season, season, reload=True)
    print("CHILD_SUMMARY:", s, flush=True)
    print("CHILD_COMPLETE", flush=True)
finally:
    loader.close()
"""


def _run_attempt(season: int, progress: Path, native: bool, quiet: bool) -> tuple[int, bool]:
    """Run one child attempt. Returns (exit_code, saw_complete_marker).

    The child's output is STREAMED through to our stdout as it arrives. It has to
    be captured (we read it to detect the completion marker and the fatal-error
    signature), but capturing is not a licence to swallow it: an earlier version
    printed only ``CHILD_SUMMARY``/``Fatal Python error`` lines, which meant a
    multi-hour season looked completely dead from the outside. The per-game log
    lines ARE the progress indicator — the sweep is long enough that silence is
    indistinguishable from a hang.

    ``--quiet`` keeps the filtered behaviour, but even then emits a heartbeat
    every :data:`_HEARTBEAT_EVERY` games so the run is never fully silent.
    """
    code = _CHILD.format(repo=str(REPO), native=str(native))
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", code, str(season), str(progress)],
        cwd=str(REPO),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,  # line-buffered: emit each line as it arrives, not in blocks
    )
    complete = False
    games = 0
    assert proc.stdout is not None
    # readline() rather than `for line in proc.stdout` so streaming is explicit
    # and cannot be defeated by iterator read-ahead.
    for line in iter(proc.stdout.readline, ""):
        if "CHILD_COMPLETE" in line:
            complete = True
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
    return proc.returncode, complete


def main() -> int:
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
        started = len(progress.read_text().split()) if progress.exists() else 0
        print(f"\n=== season {season} — {started} game(s) already done ===", flush=True)

        for attempt in range(1, args.max_attempts + 1):
            before = len(progress.read_text().split()) if progress.exists() else 0
            t0 = time.time()
            rc, complete = _run_attempt(season, progress, not args.in_docker, args.quiet)
            after = len(progress.read_text().split()) if progress.exists() else 0
            gained = after - before
            mins = (time.time() - t0) / 60

            if complete:
                print(
                    f"  attempt {attempt}: COMPLETE — {after} games total "
                    f"(+{gained} this attempt, {mins:.1f} min)",
                    flush=True,
                )
                break

            print(
                f"  attempt {attempt}: crashed rc={rc} after +{gained} games "
                f"({after} total, {mins:.1f} min) — resuming",
                flush=True,
            )

            if gained == 0:
                # No forward progress: a genuine per-game defect, not the
                # stochastic fault. Retrying forever would spin.
                print(
                    "  STOPPING: an attempt made zero progress. The next game is failing "
                    "deterministically — investigate it rather than retrying.",
                    flush=True,
                )
                return 1
        else:
            print(f"  exhausted --max-attempts for season {season}", flush=True)
            return 1

    total = sum(
        len((PROGRESS_DIR / f"{s}.txt").read_text().split())
        for s in range(args.season, end + 1)
        if (PROGRESS_DIR / f"{s}.txt").exists()
    )
    print(
        f"\nALL SEASONS COMPLETE — {total} games in {(time.time() - overall_start) / 60:.1f} min",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
