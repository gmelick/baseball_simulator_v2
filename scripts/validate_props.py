#!/usr/bin/env python
"""
scripts/validate_props.py
=========================
SIM-407 — validate the simulator's win probabilities + prop PMFs against REAL
completed-game outcomes, write a validation report, and (optionally) fit the
win-probability reliability curve back into the SIM-406 CalibrationReport.

WHAT IT DOES
------------
For each completed ("Final") game in the requested seasons:
  1. resolve the game's lineup into a GameState (the SIM-353 path),
  2. run an N-iteration Monte-Carlo batch (the SAME BatchRunner the API uses),
  3. take the simulator's home win probability (SIM-330) + per-player prop PMFs
     (SIM-329), and
  4. pair each against what ACTUALLY happened (the real home/away score from
     ``raw.games``; the real per-player box-score line from ``raw.*`` if a
     box-source is available — see ``--no-props`` below).

It then scores those (prediction, actual) pairs with
``simulation.prop_validation`` — win-prob ECE/Brier/log-loss + the fitted
reliability curve + per-prop over/under calibration + PMF coverage — writes a
``PropValidationReport`` JSON, and, when ``--write-calibration`` is set, writes the
fitted reliability curve into the CalibrationReport at ``CALIBRATION_REPORT_PATH``
so the next API boot applies the empirical win-prob correction.

WIN-PROB vs PROP SCOPE
----------------------
The win-prob validation needs only the real final score (always in ``raw.games``),
so it runs by default. The PROP validation needs the real per-player box-score
line (K/H/HR/...) for the game; that requires a completed-game box-score source in
the DB. If your environment doesn't expose one yet, pass ``--no-props`` to run the
win-prob fit alone (the prop scaffolding + report fields stay, just unpopulated) —
the win-prob reliability curve (the SIM-406 → 407 deliverable) does not depend on
it.

USAGE
-----
    # In the app container (Postgres at db:5432, DuckDB at /data/...):
    python scripts/validate_props.py --seasons 2023 2024 --iterations 100
    python scripts/validate_props.py --seasons 2024 --max-games 200 --write-calibration
    python scripts/validate_props.py --seasons 2024 --no-props      # win-prob only

    # Via the Makefile wrapper:
    make validate-props FLAGS="--seasons 2024 --write-calibration"

This is an OFFLINE validation/fitting job (it runs real sims, so it is slow — use
``--max-games`` to cap a smoke run). It never serves a request; the API only
*reads* the artifacts it writes.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from simulation.prop_validation import (  # noqa: E402
    PropValidationReport,
    build_validation_report,
    write_reliability_curve_to_calibration_report,
)

log = logging.getLogger("validate_props")

DEFAULT_DSN = os.environ.get(
    "BASEBALL_DB_DSN",
    "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim",
)
DEFAULT_CALIBRATION_PATH = os.environ.get("CALIBRATION_REPORT_PATH", "/data/calibration.json")
DEFAULT_OUTPUT = os.environ.get("PROP_VALIDATION_PATH", "/data/prop_validation.json")


async def _fetch_final_games(dsn: str, seasons: list[int], max_games: int | None) -> list[dict]:
    """Return completed-game rows ``{game_pk, home_score, away_score}`` for the
    requested seasons, ordered for reproducibility.

    Reads ``raw.games`` (status='Final'); the real final score is the win-prob
    validation's ground truth. ``max_games`` caps the count for a smoke run.
    """
    import asyncpg

    sl = ", ".join(str(int(s)) for s in seasons)
    limit = f"LIMIT {int(max_games)}" if max_games else ""
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            f"""
            SELECT game_pk, home_score, away_score
            FROM raw.games
            WHERE status = 'Final'
              AND season IN ({sl})
              AND home_score IS NOT NULL AND away_score IS NOT NULL
            ORDER BY game_pk
            {limit}
            """
        )
    finally:
        await conn.close()
    return [
        {
            "game_pk": int(r["game_pk"]),
            "home_score": int(r["home_score"]),
            "away_score": int(r["away_score"]),
        }
        for r in rows
    ]


def _run_one_game_sim(state, runner, factory_ref, n_iter: int, seed: int):
    """Simulate ONE already-resolved game; return its GameSimSummary.

    Reuses the API's own sim seam (``api.routes.games`` helpers) so the validation
    runs the SAME path production serves: build a GameSpec from the resolved
    GameState under the production factory, run the BatchRunner. The state is
    resolved by the async caller (``run``) because ``resolve_game_state`` is async
    and this function runs on a worker thread (``asyncio.to_thread``).
    """
    from api.routes.games import _run_batch, _sim_kwargs_from_state
    from simulation.batch_runner import GameSpec

    spec = GameSpec(machine_factory=factory_ref, sim_kwargs=_sim_kwargs_from_state(state))
    batch = _run_batch(runner, spec, n_iterations=n_iter, base_seed=seed, use_cache=False)
    return batch.summary


async def run(args: argparse.Namespace) -> int:
    from api.routes.games import _resolve_state_or_error
    from simulation.batch_runner import BatchRunner, default_max_workers, make_cache
    from simulation.win_probability import win_probability

    seasons = sorted({int(s) for s in args.seasons}, reverse=True)
    log.info("SIM-407 validation over seasons %s (iterations=%d).", seasons, args.iterations)

    games = await _fetch_final_games(args.dsn, seasons, args.max_games)
    log.info("Found %d completed games to validate.", len(games))
    if not games:
        log.warning("No completed games found — nothing to validate.")
        return 0

    import asyncpg

    # SIM_RUNNER_WORKERS is honored by the runner; default to a modest pool. The
    # per-game machine factory travels on the GameSpec (see _run_one_game_sim), NOT
    # on the BatchRunner — BatchRunner.__init__ takes (cache, max_workers, ...).
    workers_env = os.environ.get("SIM_RUNNER_WORKERS")
    workers = int(workers_env) if workers_env else default_max_workers()
    factory_ref = "simulation.production_factory:production_machine_factory"

    winprob_pairs: list[tuple[float, int]] = []
    prop_pairs_by_line: dict[tuple[str, float], list] = {}
    n_done = 0
    # Create the pool + runner INSIDE the try so a construction failure still hits
    # the finally (no leaked asyncpg pool / executor).
    pool = None
    runner = None
    try:
        pool = await asyncpg.create_pool(args.dsn, min_size=1, max_size=4)
        runner = BatchRunner(cache=make_cache(), max_workers=workers)
        for g in games:
            game_pk = g["game_pk"]
            try:
                state = await _resolve_state_or_error(pool, game_pk)
            except Exception as exc:  # noqa: BLE001 - skip un-resolvable games
                log.info("skip game %s (state unresolved: %s)", game_pk, type(exc).__name__)
                continue

            summary = await asyncio.to_thread(
                _run_one_game_sim,
                state,
                runner,
                factory_ref,
                args.iterations,
                args.base_seed,
            )
            if summary is None:
                continue

            # Win prob (calibrated transform, identity map here — we are FITTING the
            # map, so we validate the raw smoothed probability) vs the real result.
            wp = win_probability(summary)
            home_won = 1 if g["home_score"] > g["away_score"] else 0
            winprob_pairs.append((float(wp.home_win_prob), home_won))

            # Prop PMFs would be paired here against the real box line; gated on a
            # completed-game box-score source (see module docstring / --no-props).
            if not args.no_props:
                # Placeholder for the prop pairing — populated when a box-source is
                # wired. Left empty rather than fabricating actuals.
                pass

            n_done += 1
            if n_done % 25 == 0:
                log.info("  validated %d/%d games ...", n_done, len(games))
    finally:
        if runner is not None:
            runner.close()
        if pool is not None:
            await pool.close()

    log.info("Building validation report from %d games.", n_done)
    report = build_validation_report(
        winprob_pairs,
        prop_pairs_by_line,
        n_bins=args.bins,
        n_games=n_done,
        seasons_used=seasons,
    )

    print(_summary_text(report))

    out_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(out_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(report.to_json())
    log.info("Wrote PropValidationReport -> %s", args.output)

    if args.write_calibration:
        ok = write_reliability_curve_to_calibration_report(report, args.calibration_path)
        if ok:
            log.info(
                "Wrote fitted win-prob reliability curve into %s — next boot applies it.",
                args.calibration_path,
            )
        else:
            log.warning(
                "Did NOT write reliability curve (empty curve or missing %s); "
                "win-prob map stays identity.",
                args.calibration_path,
            )

    return 0


def _summary_text(report: PropValidationReport) -> str:
    lines = [
        "=" * 64,
        "SIM-407 PROP / WIN-PROBABILITY VALIDATION REPORT",
        "=" * 64,
        f"Games validated: {report.n_games}",
        f"Seasons: {report.seasons_used}",
        "",
        "--- Win probability ---",
        f"  n:        {report.winprob_n}",
        f"  ECE:      {report.winprob_ece:.4f}   (0 = perfectly calibrated)",
        f"  Brier:    {report.winprob_brier:.4f}",
        f"  log-loss: {report.winprob_log_loss:.4f}",
        f"  fitted reliability anchors: {len(report.winprob_reliability_curve)}",
    ]
    if report.prop_calibrations:
        lines += ["", "--- Prop over/under calibration ---"]
        for c in report.prop_calibrations:
            lines.append(
                f"  {c.get('prop', '?'):<5} line {c.get('line', 0):>4}: "
                f"ECE={c.get('ece', float('nan')):.3f} "
                f"Brier={c.get('brier', float('nan')):.3f} "
                f"pred_over={c.get('mean_pred_over', float('nan')):.3f} "
                f"obs_over={c.get('observed_over_rate', float('nan')):.3f} (n={c.get('n', 0)})"
            )
    if report.pmf_coverage:
        lines += ["", "--- PMF coverage ---"]
        for prop, cov in report.pmf_coverage.items():
            lines.append(
                f"  {prop:<5}: coverage={cov.get('coverage', float('nan')):.3f} "
                f"(nominal {cov.get('nominal', 0):.2f}), "
                f"PIT_mean={cov.get('pit_mean', float('nan')):.3f} (n={cov.get('n', 0)})"
            )
    lines.append("=" * 64)
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Validate prop PMFs + fit win-prob curve (SIM-407).")
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (completed games).")
    p.add_argument("--seasons", type=int, nargs="+", required=True, help="Seasons to validate.")
    p.add_argument("--iterations", type=int, default=100, help="Monte-Carlo iters per game.")
    p.add_argument("--base-seed", type=int, default=407, help="Reproducibility seed.")
    p.add_argument("--max-games", type=int, default=None, help="Cap games (smoke run).")
    p.add_argument("--bins", type=int, default=10, help="Reliability/ECE bin count.")
    p.add_argument(
        "--no-props",
        action="store_true",
        help="Win-prob validation only (skip prop pairing; needs no box-source).",
    )
    p.add_argument("--output", default=DEFAULT_OUTPUT, help="PropValidationReport JSON path.")
    p.add_argument(
        "--write-calibration",
        action="store_true",
        help="Write the fitted win-prob reliability curve into CALIBRATION_REPORT_PATH.",
    )
    p.add_argument(
        "--calibration-path",
        default=DEFAULT_CALIBRATION_PATH,
        help=f"CalibrationReport to update (default: {DEFAULT_CALIBRATION_PATH}).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
