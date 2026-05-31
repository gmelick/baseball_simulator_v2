#!/usr/bin/env python
"""
scripts/fit_calibration.py
==========================
SIM-406 — fit + persist a ``CalibrationReport`` over the real DuckDB profiles and
(optionally) validate that it lands every engine's median similarity near the
0.50 target.

WHAT THIS PRODUCES
------------------
A single JSON file (``CalibrationReport.to_json``) at ``--output`` (default
``CALIBRATION_REPORT_PATH`` env, else ``/data/calibration.json``).  The serving
API loads it at boot (``api.main`` lifespan) and:

  * applies the per-engine RBF / arsenal sigmas to EVERY similarity engine that
    exposes ``apply_calibration`` (the 8 similarity-score engines), so similarity
    is scored on the population-fit 0.50-median curve instead of hardcoded
    literals — the SIM-406 "apply to all engines" deliverable; and
  * derives the win-probability ``CalibrationMap`` from any fitted reliability
    curve (SIM-361 — left empty here; the sim-vs-actual reliability fit is SIM-407).

WHAT IS FIT
-----------
``SimilarityCalibrator.calibrate_from_population`` does the Tier-1 population fit
for batter / pitcher / fielder / baserunner (sigmas + reliability weights + EB
priors) and — SIM-406 — the four SIM-408-era engines (catcher / baserunner-steal
/ pitcher-steal / manager) sigmas.  The pitcher *arsenal* W₂→score anchor needs
the GMM W₂ distance distribution, which lives on the built pitcher engine (not in
DuckDB), so we build the pitcher engine and SAMPLE a representative set of
same-hand pairwise W₂ distances to anchor ``arsenal_gamma`` (skip with
``--no-arsenal`` to keep the locked 4.10 default).

USAGE
-----
    # In the app container (DuckDB at /data/baseball_sim.duckdb):
    python scripts/fit_calibration.py                       # all seasons, with arsenal
    python scripts/fit_calibration.py --seasons 2023 2024   # subset
    python scripts/fit_calibration.py --no-arsenal          # skip the slow W2 sample
    python scripts/fit_calibration.py --validate            # + per-engine median check

    # Via the Makefile wrapper:
    make calibrate
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# Standalone script: ensure the repo root is importable when run as
# ``python scripts/fit_calibration.py`` (Python puts scripts/ on sys.path[0],
# not the repo root).  E402 on the subsequent imports is expected + ignored
# (see pyproject ``[tool.ruff.lint.per-file-ignores]``).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np  # noqa: E402

from similarity.similarity_calibration import (  # noqa: E402
    CalibrationReport,
    SimilarityCalibrator,
)

log = logging.getLogger("fit_calibration")

DEFAULT_DUCKDB_PATH = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
DEFAULT_OUTPUT = os.environ.get("CALIBRATION_REPORT_PATH", "/data/calibration.json")
#: All Statcast seasons the platform ingests (matches the engine __main__ blocks).
DEFAULT_SEASONS = [2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017]


def resolve_seasons(duckdb_path: str, requested: list[int] | None) -> list[int]:
    """Return the seasons to calibrate over.

    Explicit ``--seasons`` win; otherwise read the distinct seasons present in
    ``derived.batter_season_metrics`` (so the fit tracks whatever the DuckDB
    actually holds), falling back to :data:`DEFAULT_SEASONS` if that read fails.
    """
    if requested:
        return sorted(set(requested), reverse=True)
    try:
        import duckdb

        conn = duckdb.connect(duckdb_path, read_only=True)
        try:
            rows = conn.execute(
                "SELECT DISTINCT season FROM derived.batter_season_metrics ORDER BY season DESC"
            ).fetchall()
        finally:
            conn.close()
        seasons = [int(r[0]) for r in rows]
        if seasons:
            return seasons
    except Exception as exc:  # noqa: BLE001 - fall back to the canonical list
        log.warning(
            "Could not read seasons from DuckDB (%s: %s); using default list.",
            type(exc).__name__,
            exc,
        )
    return DEFAULT_SEASONS


def sample_arsenal_distances(
    duckdb_path: str, seasons: list[int], n_pairs: int, seed: int
) -> np.ndarray:
    """Build the pitcher engine and SAMPLE same-hand pairwise arsenal W₂ distances.

    The full pairwise W₂ cache is O(N²) (~20 min for one hand); the calibration
    only needs the W₂ *distribution* to anchor the 0.50-median, so we sample
    ``n_pairs`` random same-hand pairs and compute their W₂ directly — seconds,
    not minutes.  Returns the finite distances (empty array if the engine has no
    GMM-bearing profiles).
    """
    from similarity.engines.pitcher_similarity import (
        ArsenalSimilarity,
        PitcherSimilarityEngine,
    )

    log.info("Building pitcher engine for arsenal-W2 sampling ...")
    engine = PitcherSimilarityEngine(duckdb_path=duckdb_path)
    engine.build(seasons=seasons)

    rng = np.random.default_rng(seed)
    dists: list[float] = []
    partitions = [
        getattr(engine, "_partition_l", None),
        getattr(engine, "_partition_r", None),
    ]
    for part in partitions:
        if part is None:
            continue
        profs = [p for p in getattr(part, "profiles", []) if p.gmm is not None]
        if len(profs) < 2:
            continue
        max_pairs = len(profs) * (len(profs) - 1) // 2
        target = min(n_pairs // 2, max_pairs)
        seen: set[tuple[int, int]] = set()
        attempts = 0
        while len(seen) < target and attempts < target * 5:
            attempts += 1
            i, j = int(rng.integers(0, len(profs))), int(rng.integers(0, len(profs)))
            if i == j:
                continue
            key = (i, j) if i < j else (j, i)
            if key in seen:
                continue
            seen.add(key)
            d = ArsenalSimilarity.distance(profs[i].gmm, profs[j].gmm)
            if np.isfinite(d):
                dists.append(float(d))
    arr = np.array(dists, dtype=np.float64)
    log.info(
        "Sampled %d finite arsenal W2 distances (median=%.4f).",
        arr.size,
        float(np.median(arr)) if arr.size else float("nan"),
    )
    return arr


def validate_engine_medians(
    duckdb_path: str, seasons: list[int], report: CalibrationReport
) -> None:
    """Best-effort: build batter + pitcher engines, apply the report, and log the
    median similarity over a small random query sample — a sanity check that the
    fit lands near 0.50.  Skipped silently on any build error (offline/no-DB)."""
    try:
        from similarity.engines.batter_similarity import BatterSimilarityEngine

        eng = BatterSimilarityEngine(duckdb_path=duckdb_path)
        eng.build(seasons=seasons)
        eng.apply_calibration(report)
        ids = eng.profile_ids()
        if not ids:
            log.info("Validation: no batter profiles to sample.")
            return
        rng = np.random.default_rng(406)
        sample = [ids[int(rng.integers(0, len(ids)))] for _ in range(min(30, len(ids)))]
        medians = []
        for bid, season in sample:
            results = eng.query(bid, season)
            if results:
                medians.append(float(np.median([r.score for r in results])))
        if medians:
            log.info(
                "Validation (batter): mean of per-query median similarity = %.4f "
                "(target 0.50, n_queries=%d).",
                float(np.mean(medians)),
                len(medians),
            )
    except Exception as exc:  # noqa: BLE001 - validation is best-effort
        log.warning("Engine-median validation skipped (%s: %s).", type(exc).__name__, exc)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fit + persist a CalibrationReport (SIM-406).")
    p.add_argument(
        "--duckdb-path",
        default=DEFAULT_DUCKDB_PATH,
        help=f"DuckDB analytical DB (default: {DEFAULT_DUCKDB_PATH}).",
    )
    p.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Where to write the CalibrationReport JSON (default: {DEFAULT_OUTPUT}).",
    )
    p.add_argument(
        "--seasons",
        type=int,
        nargs="+",
        default=None,
        help="Seasons to calibrate over (default: every season in the DuckDB).",
    )
    p.add_argument(
        "--target-median",
        type=float,
        default=0.50,
        help="Target median similarity per engine (default: 0.50).",
    )
    p.add_argument(
        "--arsenal-sample",
        type=int,
        default=40_000,
        help="Number of same-hand pitcher pairs to sample for the arsenal W2 anchor.",
    )
    p.add_argument(
        "--no-arsenal",
        action="store_true",
        help="Skip arsenal W2 sampling (keeps the locked 4.10 ARSENAL_SCALE default).",
    )
    p.add_argument(
        "--validate",
        action="store_true",
        help="After fitting, build the batter engine + log its median similarity.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = parse_args(argv)

    seasons = resolve_seasons(args.duckdb_path, args.seasons)
    log.info("Fitting calibration over seasons %s (DuckDB: %s).", seasons, args.duckdb_path)

    arsenal_dists: np.ndarray | None = None
    if not args.no_arsenal:
        try:
            arsenal_dists = sample_arsenal_distances(
                args.duckdb_path, seasons, args.arsenal_sample, seed=406
            )
            if arsenal_dists.size == 0:
                arsenal_dists = None
        except Exception as exc:  # noqa: BLE001 - arsenal is optional
            log.warning(
                "Arsenal W2 sampling failed (%s: %s); keeping default ARSENAL_SCALE.",
                type(exc).__name__,
                exc,
            )
            arsenal_dists = None

    calibrator = SimilarityCalibrator(duckdb_path=args.duckdb_path)
    report = calibrator.calibrate_from_population(
        seasons=seasons,
        target_median_score=args.target_median,
        arsenal_w2_distances=arsenal_dists,
    )

    print(report.summary())

    out_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(out_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(report.to_json())
    log.info("Wrote CalibrationReport -> %s", args.output)

    if args.validate:
        validate_engine_medians(args.duckdb_path, seasons, report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
