"""
native_sweep_probe.py — run the ETL sweep OUTSIDE Docker, on the Windows host.

WHY THIS EXISTS
---------------
The corrective reload sweep segfaults inside the container: SIGSEGV (exit 139),
``OOMKilled=false``, memory flat at ~67 MiB, crashing inside CPython's own
allocator at a DIFFERENT game every run (observed at 3, 5, 66, 318 and 405
games). Every component has been isolated and none reproduces alone:

  * pure-Python allocation loop ....... 32M ops, clean (both images)
  * numpy scalar coercion ............. 3M iterations, clean
  * psycopg2 connection churn ......... 20,000 borrow/return cycles, clean
  * HTTP path (real MLB API) .......... 480 requests, clean
  * psycopg2-binary dual OpenSSL ...... removed; still crashes
  * numpy / OpenBLAS entirely ......... removed; still crashes
  * Riot Vanguard (host BSOD driver) .. stopped, no bugchecks; still crashes

It only reproduces under the full interleaved loop. The open question is
whether the fault is in the application stack or in the Docker/WSL2 layer.

This script answers that. It runs the IDENTICAL workload against the SAME
database using the Windows host's own CPython — no container, no WSL2, no
Docker networking.

  * Survives here, crashes in the container -> the Docker/WSL2 layer.
  * Crashes here too                        -> the application/library stack,
                                               and this becomes a clean minimal
                                               reproducer to report upstream.

PREREQUISITE
------------
The host must be able to reach Postgres. As of this writing it CANNOT: Docker
Desktop's published port (``DB_HOST_PORT``, currently 5434) accepts the TCP
connection and then drops it during the Postgres startup handshake. Restarting
Docker Desktop / ``wsl --shutdown`` normally clears that. Verify with::

    python scripts/native_sweep_probe.py --check

USAGE
-----
    python scripts/native_sweep_probe.py --check          # connectivity only
    python scripts/native_sweep_probe.py --season 2017    # run the sweep
    python scripts/native_sweep_probe.py --season 2017 --limit 500

Reads the DSN from ``.env`` (``BASEBALL_DB_DSN``) and rewrites the host/port to
``127.0.0.1:<DB_HOST_PORT>``. Nothing is printed that contains the password.

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


def _install_urllib_transport() -> None:
    """Replace the loader's `requests`-based `_connect` with a stdlib one.

    DIAGNOSTIC ONLY. The native crash reports::

        Fatal Python error: _PyEval_EvalFrameDefault: Executing a cache.

    which means CPython's evaluation loop dispatched into an inline-cache entry
    instead of a real instruction — the bytecode or inline cache of
    ``_build_row_dict`` is being corrupted. The surviving C extensions in that
    process are ``psycopg2._psycopg``, ``_brotli``, ``simplejson._speedups`` and
    FOUR copies of the mypyc-compiled ``charset_normalizer`` (two standalone, two
    vendored inside ``requests``).

    ``urllib.request`` reaches the same endpoints using only stdlib code, so this
    removes ``requests``, ``charset_normalizer``, ``_brotli`` and ``simplejson``
    from the loop in one step. If the sweep then survives, the fault is in that
    stack; if it still crashes, the whole HTTP layer is exonerated.
    """
    import json as _json
    import urllib.parse
    import urllib.request

    from pipeline.etl import etl_historical_loader as m

    def _urllib_connect(url: str, params: dict | None = None) -> dict:
        if params:
            # doseq=True matches requests' handling of list values (gameTypes).
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        last: Exception | None = None
        for attempt in range(1, m.MAX_API_RETRIES + 1):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "baseball-sim-etl/1.0 (urllib probe)"}
                )
                with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                    return _json.loads(resp.read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001 — mirrors _http_get's retry shape
                last = exc
                if attempt == m.MAX_API_RETRIES:
                    raise
                time_mod = __import__("time")
                time_mod.sleep(m.RETRY_BACKOFF_S * attempt)
        raise last or RuntimeError("unreachable")

    m._connect = _urllib_connect  # type: ignore[assignment]
    print("HTTP transport: urllib.request (stdlib) — requests/charset_normalizer bypassed")


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
        "--urllib",
        action="store_true",
        help="use stdlib urllib.request instead of requests (bypasses charset_normalizer)",
    )
    args = ap.parse_args()

    faulthandler.enable()
    env = _load_env()
    dsn = _host_dsn(env)

    if args.check:
        return check(dsn)

    os.environ["BASEBALL_DB_DSN"] = dsn
    print(f"platform : {sys.platform}  python {sys.version.split()[0]}  (NOT in Docker)")
    print(f"DSN      : {_redact(dsn)}")

    from pipeline.etl.etl_historical_loader import HistoricalDataLoader

    if args.urllib:
        _install_urllib_transport()

    print("numpy loaded:", "numpy" in sys.modules, "(should be False)")

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
        print("\nSURVIVED — no segfault on the host. That points at the Docker/WSL2 layer.")
        return 0
    except KeyboardInterrupt as exc:
        print(f"\nstopped early: {exc}")
        return 0
    finally:
        loader.close()


if __name__ == "__main__":
    raise SystemExit(main())
