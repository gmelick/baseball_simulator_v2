"""
scripts/probes/arsenal_reproducibility_check.py — two pitcher profile builds of one season
from the same data, compared by content: do they write the same arsenals (SIM-580)?

What it does, in order:

  1. prints which ``pitcher.py`` the process loaded, and whether its pitch-vector query
     carries the key order (``ORDER BY game_pk, at_bat_number, pitch_number``), so the
     record shows which code ran;
  2. builds the pitcher profiles of ``--season`` twice (``_compute_pitcher_profiles``, the
     arsenal mixture fit included), each into a NEW scratch DuckDB file under ``/tmp``,
     with Postgres attached read-only. It never opens the live file;
  3. checks that each build is valid: no crashed worker chunk (an ERROR on the computor's
     logger), the same fallback set in both builds, and hundreds of fitted arsenals;
  4. compares every pitcher-season: the ``gmm_model`` JSON without
     ``fit_diagnostics.fit_date`` (the day the fit ran), and the component rows by
     ``(pitcher_id, season, component_id)``.

Exit 0: the two builds wrote the same arsenals. Exit 1: some arsenals differ. Exit 2: a
build is not valid (a crashed chunk, a different fallback set, or fewer than MIN_FITTED
fitted arsenals): run it again. The probe also exits 2 when BASEBALL_DB_DSN is not set.

From the repository root (Git Bash):

    MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps -T -v "$PWD:/app" \\
        app python scripts/probes/arsenal_reproducibility_check.py \\
        --season 2025 --asof 2026-10-01 --workers 6 --out results/sim580/fix.json

A worktree has no .env, so use the main checkout's compose file and mount the worktree's
code. Set M to the main checkout and WT to the worktree, both as C:/... paths:

    MSYS_NO_PATHCONV=1 docker compose -f "$M/docker-compose.yml" --project-directory "$M" \\
        run --rm --no-deps -T -v "$WT:/app" -v "$WT/api:/app/api" \\
        -v "$WT/pipeline:/app/pipeline" -v "$WT/similarity:/app/similarity" \\
        -v "$WT/simulation:/app/simulation" -v "$WT/db:/app/db" \\
        -v "$M/results:/app/results" app python scripts/probes/arsenal_reproducibility_check.py \\
        --season 2025 --asof 2026-10-01 --workers 6 --out results/sim580/fix.json

Without the five code mounts, compose mounts the main checkout's code over /app, and the
probe runs that checkout's pitcher.py. The ``code:`` line shows which query ran.

The scratch files hold one season of pitcher rows (tens of MB). Each build took 9 to 10
minutes on 6 workers (docs/audit/records/sim580_arsenal_reproducibility.txt). Run the probe
outside the nightly window (07:00 to 11:00 UTC).
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.batch.player_profile_computor import PlayerProfileComputor  # noqa: E402
from pipeline.batch.profiles import pitcher as pitcher_module  # noqa: E402
from pipeline.batch.profiles.constants import MIN_PITCHER_PITCHES  # noqa: E402

log = logging.getLogger("arsenal_reproducibility_check")

KEY_ORDER = "ORDER BY game_pk, at_bat_number, pitch_number"
# A fit can fail on its own data; a season of real pitchers fits hundreds.
MIN_FITTED = 100


class _ErrorCounter(logging.Handler):
    """Counts the ERROR records of one logger (the crashed-chunk line)."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _code_check() -> dict:
    source = inspect.getsource(pitcher_module.PitcherProfilesMixin._compute_pitcher_profiles)
    return {"pitcher_py": pitcher_module.__file__, "key_order_in_query": KEY_ORDER in source}


def _build(pg_dsn: str, season: int, asof: date, scratch: Path) -> dict:
    """Build one season's pitcher profiles into a new file and read the arsenals back."""
    path = scratch / "profiles.duckdb"
    if path.exists() or str(path.resolve()).startswith("/data"):
        raise SystemExit(f"refusing to build into {path}: not a new scratch file")
    counter = _ErrorCounter()
    computor_log = logging.getLogger("player_profile_computor")
    computor_log.addHandler(counter)
    t0 = time.perf_counter()
    computor = PlayerProfileComputor(pg_dsn=pg_dsn, duckdb_path=str(path))
    computor._connect()
    try:
        computor._compute_pitcher_profiles([season], asof=asof)
        conn = computor._conn
        assert conn is not None
        models = conn.execute(
            "SELECT pitcher_id, season, sample_pitches, below_minimum_sample, gmm_model "
            "FROM derived.pitcher_season_metrics WHERE season = ? ORDER BY 1, 2",
            [season],
        ).fetchall()
        components = conn.execute(
            "SELECT * FROM derived.pitcher_gmm_components WHERE season = ? ORDER BY 1, 2, 3",
            [season],
        ).fetchall()
    finally:
        computor._close()
        computor_log.removeHandler(counter)
    arsenals: dict[str, dict] = {}
    fallbacks: list[str] = []
    for pitcher_id, ssn, sample, below, gmm_model in models:
        key = f"{pitcher_id}:{ssn}"
        model = json.loads(gmm_model) if isinstance(gmm_model, str) else gmm_model
        model.get("fit_diagnostics", {}).pop("fit_date", None)
        pending = model.get("note") == "pending_gmm_fit"
        if sample >= MIN_PITCHER_PITCHES and (below or pending):
            fallbacks.append(key)
        arsenals[key] = {"model": model, "components": []}
    for row in components:
        arsenals[f"{row[0]}:{row[1]}"]["components"].append(list(row))
    fitted = sum(1 for a in arsenals.values() if (a["model"].get("n_components") or 0) > 0)
    return {
        "seconds": round(time.perf_counter() - t0),
        "pitcher_seasons": len(arsenals),
        "fitted": fitted,
        "fallbacks": sorted(fallbacks),
        "errors": counter.messages,
        "arsenals": arsenals,
    }


def _compare(a: dict, b: dict) -> dict:
    keys = sorted(set(a["arsenals"]) | set(b["arsenals"]))
    differing: list[str] = []
    k_differs = 0
    for key in keys:
        left, right = a["arsenals"].get(key), b["arsenals"].get(key)
        if left == right:
            continue
        differing.append(key)
        k_left = ((left or {}).get("model") or {}).get("n_components")
        k_right = ((right or {}).get("model") or {}).get("n_components")
        k_differs += int(k_left != k_right)
    return {
        "pitcher_seasons": len(keys),
        "identical": len(keys) - len(differing),
        "differing": len(differing),
        "different_k": k_differs,
        "first_differing": differing[:10],
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--asof", type=date.fromisoformat, required=True)
    ap.add_argument("--out", required=True, help="the JSON summary, e.g. results/sim580/fix.json")
    ap.add_argument("--workers", type=int, default=None, help="cap the fit's worker processes")
    args = ap.parse_args(argv)
    pg_dsn = os.environ.get("BASEBALL_DB_DSN", "")
    if not pg_dsn:
        log.error("no Postgres DSN (BASEBALL_DB_DSN)")
        return 2
    if args.workers:
        # The worker count does not change what a fit writes; it caps the memory.
        workers = args.workers
        pitcher_module._resolve_gmm_workers = lambda n_tasks: max(1, min(workers, n_tasks))

    code = _code_check()
    print(f"code: {code['pitcher_py']}; key order in the query: {code['key_order_in_query']}")
    builds = []
    for label in ("A", "B"):
        scratch = Path(tempfile.mkdtemp(prefix=f"sim580_{label}_", dir="/tmp"))
        try:
            builds.append(_build(pg_dsn, args.season, args.asof, scratch))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        b = builds[-1]
        print(
            f"build {label}: {b['pitcher_seasons']} pitcher-seasons, {b['fitted']} fitted, "
            f"{len(b['fallbacks'])} fallbacks, {len(b['errors'])} errors, {b['seconds']} s"
        )

    a, b = builds
    valid = (
        not a["errors"]
        and not b["errors"]
        and a["fallbacks"] == b["fallbacks"]
        and min(a["fitted"], b["fitted"]) >= MIN_FITTED
    )
    result = _compare(a, b)
    summary = {
        "season": args.season,
        "asof": args.asof.isoformat(),
        **code,
        "valid": valid,
        "builds": [{k: v for k, v in x.items() if k != "arsenals"} for x in builds],
        **result,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(
        f"season {args.season}: {result['pitcher_seasons']} pitcher-seasons; identical "
        f"{result['identical']}; differing {result['differing']}; different K "
        f"{result['different_k']}; valid {valid}"
    )
    if result["first_differing"]:
        print(f"first differing: {', '.join(result['first_differing'])}")
    if not valid:
        print("NOT VALID: a crashed chunk, a different fallback set or too few fits; rerun")
        return 2
    return 1 if result["differing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
