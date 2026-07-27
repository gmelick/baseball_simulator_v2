"""
native_sweep_probe.py — run the ETL sweep OUTSIDE Docker, on the Windows host.

WHY THIS EXISTS
---------------
The corrective reload sweep used to die with::

    Fatal Python error: _PyEval_EvalFrameDefault: Executing a cache.

CPython's evaluation loop dispatched into an inline-cache entry instead of a real
instruction — i.e. the bytecode / inline cache of ``_build_row_dict`` was being
corrupted underneath it. It presented as SIGSEGV (exit 139) in the container and
as the fatal error above natively, at a DIFFERENT game every run (observed at 3,
5, 66, 191, 193, 318 and 405 games).

Ruled out by direct measurement, not inference:

  * memory exhaustion .......... flat 67 MiB, OOMKilled=false
  * Docker / WSL2 .............. reproduced NATIVELY on Windows, no container
  * Riot Vanguard (host BSODs) . vgk stopped, zero bugchecks, still crashed
  * psycopg2-binary dual SSL ... source-built psycopg2, 1 OpenSSL, still crashed
  * numpy / OpenBLAS ........... import removed entirely, still crashed
  * charset_normalizer calls ... instrumented: ZERO (the API sends charset=UTF-8)
  * simplejson C speedups ...... forced stdlib json, still crashed
  * psycopg2 connection churn .. 20,000 borrow/return cycles clean
  * TLS handshake churn ........ pg_stat_ssl shows ssl=False

WHAT ACTUALLY FIXED IT (SIM-446)
--------------------------------
Swapping the HTTP transport from ``requests`` to stdlib ``urllib.request``. That
run completed the FULL 2017 season — 2,468 games, 732,475 pitch rows, no crash.
Against a fault that had been firing roughly every 200 games, surviving 2,468 is
about a 1-in-10^5 coincidence.

urllib is now the loader's DEFAULT (``ETL_HTTP_TRANSPORT``), so this script no
longer patches anything — it runs the production code path, just outside Docker.

HONEST LIMITS OF THAT CONCLUSION
--------------------------------
The fault is STOCHASTIC, so this is strong evidence, not proof. One anomaly
remains from the clean run: game 492011 failed once with ``integer out of
range``, then reloaded perfectly on demand — a transient bad value, which is a
corruption fingerprint rather than a data defect. It was contained (per-game
transaction rolled back; one retry fixed it), but it means the rate was reduced,
not provably driven to zero. Re-read this file before declaring the bug closed.

USAGE
-----
    python scripts/native_sweep_probe.py --check              # connectivity only
    python scripts/native_sweep_probe.py --season 2017        # run the sweep
    python scripts/native_sweep_probe.py --season 2017 --limit 500
    python scripts/native_sweep_probe.py --transport requests # A/B the old path

Reads the DSN from ``.env`` (``BASEBALL_DB_DSN``) and rewrites the host/port to
``127.0.0.1:<DB_HOST_PORT>``. Nothing printed contains the password.

The sweep is idempotent — ``reload_game`` deletes and re-inserts each game in one
transaction, and the shrink guard refuses to replace a game with fewer rows — so
running this is safe and resumable regardless of how it ends.
"""

from __future__ import annotations

import argparse
import faulthandler
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    envfile = REPO / ".env"
    if not envfile.exists():
        raise SystemExit(f"no .env at {envfile}")
    for line in envfile.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _host_dsn(env: dict[str, str]) -> str:
    """Rewrite the in-container DSN to reach the published port from the host."""
    dsn = env.get("BASEBALL_DB_DSN")
    if not dsn:
        raise SystemExit("BASEBALL_DB_DSN not found in .env")
    port = env.get("DB_HOST_PORT", "5432")
    return re.sub(r"@[^/:]+:\d+/", f"@127.0.0.1:{port}/", dsn)


def _redact(dsn: str) -> str:
    return re.sub(r"//[^@]+@", "//***:***@", dsn)


def check(dsn: str) -> int:
    import psycopg2

    print(f"DSN: {_redact(dsn)}")
    try:
        conn = psycopg2.connect(dsn, connect_timeout=8)
    except Exception as exc:  # noqa: BLE001 — this IS the diagnostic
        print(f"CONNECT FAILED: {type(exc).__name__}: {str(exc).splitlines()[0]}")
        print(
            "\nIf this says 'server closed the connection unexpectedly', Docker\n"
            "Desktop's port proxy is not forwarding. Restart Docker Desktop (or\n"
            "`wsl --shutdown`, then bring the stack back up) and retry."
        )
        return 1
    with conn, conn.cursor() as cur:
        cur.execute("SELECT current_user, current_database(), version()")
        user, db, ver = cur.fetchone()
        cur.execute("SELECT count(*) FROM raw.etl_game_ingest")
        (done,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM raw.pitches")
        (pitches,) = cur.fetchone()
    conn.close()
    print(f"connected as {user} to {db}")
    print(f"server: {ver.split(',')[0]}")
    print(f"games already recorded: {done}   raw.pitches rows: {pitches:,}")
    print("\nOK — the host can reach the database. Run without --check to sweep.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--check", action="store_true", help="test connectivity and exit")
    ap.add_argument("--season", type=int, default=2017)
    ap.add_argument("--limit", type=int, default=0, help="stop after N games (0 = whole season)")
    ap.add_argument(
        "--transport",
        choices=("urllib", "requests"),
        default=None,
        help="override ETL_HTTP_TRANSPORT (default: the loader's own default, urllib)",
    )
    args = ap.parse_args()

    faulthandler.enable()
    env = _load_env()
    dsn = _host_dsn(env)

    if args.check:
        return check(dsn)

    os.environ["BASEBALL_DB_DSN"] = dsn
    if args.transport:
        # Must precede the import — the loader reads this at module load.
        os.environ["ETL_HTTP_TRANSPORT"] = args.transport

    print(f"platform : {sys.platform}  python {sys.version.split()[0]}  (NOT in Docker)")
    print(f"DSN      : {_redact(dsn)}")

    from pipeline.etl.etl_historical_loader import _HTTP_TRANSPORT, HistoricalDataLoader

    print(f"transport: {_HTTP_TRANSPORT}")
    for mod in ("requests", "charset_normalizer", "numpy"):
        print(f"  {mod} loaded: {mod in sys.modules}")

    loader = HistoricalDataLoader()
    if args.limit:
        # Bound the run by wrapping the dispatcher — cheaper than a partial season.
        original = loader._dispatch_game
        state = {"n": 0}

        def bounded(*a, **kw):
            if state["n"] >= args.limit:
                raise KeyboardInterrupt(f"--limit {args.limit} reached")
            state["n"] += 1
            return original(*a, **kw)

        loader._dispatch_game = bounded  # type: ignore[method-assign]

    try:
        summary = loader.refresh_seasons(args.season, args.season, reload=True)
        print("SUMMARY:", summary)
        print(
            "\nCompleted without an interpreter crash. Note the fault is STOCHASTIC —\n"
            "a single clean run is only meaningful at season scale (~2,400 games)."
        )
        return 0
    except KeyboardInterrupt as exc:
        print(f"\nstopped early: {exc}")
        return 0
    finally:
        loader.close()


if __name__ == "__main__":
    raise SystemExit(main())
