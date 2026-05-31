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
  2. replay N iterations via ``record_game_plays`` (the SAME factory the API /
     batch runner use), collecting one :class:`GameSimResult` per iteration,
  3. take the simulator's home win probability (SIM-330) + per-player prop PMFs
     (SIM-329, built from the per-iteration boxscores), and
  4. pair each against what ACTUALLY happened — the real home/away score from
     ``raw.games`` (win prob), and the real per-player prop totals aggregated
     from ``raw.pitches.events`` (props: batter H/HR/TB, pitcher K/BB).

It then scores those (prediction, actual) pairs with
``simulation.prop_validation`` — win-prob ECE/Brier/log-loss + the fitted
reliability curve + per-prop over/under calibration + PMF coverage — writes a
``PropValidationReport`` JSON, and, when ``--write-calibration`` is set, writes the
fitted reliability curve into the CalibrationReport at ``CALIBRATION_REPORT_PATH``
so the next API boot applies the empirical win-prob correction.

WHAT PROPS ARE VALIDATED
------------------------
The prop ground truth comes from ``raw.pitches.events`` — the SAME pitch-by-pitch
table the similarity engines are built from, so NO extra data source is needed.
Only the props EXACTLY recoverable from the per-PA event label are scored:

  * batter:  H, HR, TB
  * pitcher: K, BB

RBI / ER / OUTS are intentionally NOT derived — RBI needs the per-PA
runs-driven-in count, ER needs earned/unearned attribution, and OUTS needs a
per-event out count, none of which the event label carries; deriving them from
``events`` would produce wrong actuals that corrupt the calibration. They are left
for a richer box-score source. ``--no-props`` runs the win-prob fit alone (a fast
smoke run) — the win-prob reliability curve does not depend on the props.

USAGE
-----
    # In the app container (Postgres at db:5432, DuckDB at /data/...):
    python scripts/validate_props.py --seasons 2023 2024 --iterations 100
    python scripts/validate_props.py --seasons 2024 --max-games 200 --write-calibration
    python scripts/validate_props.py --seasons 2024 --no-props      # win-prob only

    # Via the Makefile wrapper:
    make validate-props FLAGS="--seasons 2024 --write-calibration"

This is an OFFLINE validation/fitting job (it replays real sims, so it is slow —
use ``--max-games`` to cap a smoke run). It never serves a request; the API only
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

from simulation.prop_distributions import PropDistributionSet  # noqa: E402
from simulation.prop_validation import (  # noqa: E402
    PropValidationReport,
    build_validation_report,
    pair_props_for_validation,
    real_props_from_pa_events,
    write_reliability_curve_to_calibration_report,
)

log = logging.getLogger("validate_props")

DEFAULT_DSN = os.environ.get(
    "BASEBALL_DB_DSN",
    "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim",
)
DEFAULT_CALIBRATION_PATH = os.environ.get("CALIBRATION_REPORT_PATH", "/data/calibration.json")
DEFAULT_OUTPUT = os.environ.get("PROP_VALIDATION_PATH", "/data/prop_validation.json")

#: The production machine factory (dotted ref) — the SAME one the API serves with.
_FACTORY_REF = "simulation.production_factory:production_machine_factory"


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


async def _fetch_pa_events(pool, game_pk: int) -> list[tuple]:
    """Return one ``(batter, pitcher, events)`` tuple per COMPLETED plate
    appearance for ``game_pk``, read from ``raw.pitches``.

    ``raw.pitches.events`` is populated only on the terminal pitch of each PA, so
    filtering non-empty ``events`` yields exactly one row per PA — the real
    per-player outcomes :func:`real_props_from_pa_events` aggregates into the prop
    ground truth (the SAME table the similarity engines are built from).
    """
    rows = await pool.fetch(
        "SELECT batter, pitcher, events FROM raw.pitches "
        "WHERE game_pk = $1 AND events IS NOT NULL AND events <> ''",
        int(game_pk),
    )
    return [(r["batter"], r["pitcher"], r["events"]) for r in rows]


def _collect_game_results(state, n_iter: int, base_seed: int | None) -> list:
    """Replay one already-resolved game N times; return the per-iteration results.

    Uses the SIM-356 ``record_game_plays`` seam (the SAME one ``/api/.../boxscore``
    uses to materialise per-game boxscores, which ``BatchRunner`` does not retain):
    builds the live machine from the production ``factory_ref`` + the resolved
    GameState's sim_kwargs, runs ``simulate_game`` at each derived seed, and
    collects the :class:`GameSimResult` (carrying ``.boxscore`` + the score) per
    iteration. The list feeds BOTH the win-probability aggregation AND the prop-PMF
    set. Sync + CPU-bound, so the async caller offloads it via ``asyncio.to_thread``.
    """
    from api.routes.games import _sim_kwargs_from_state
    from simulation.batch_runner import derive_seed
    from simulation.play_recorder import record_game_plays

    sim_kwargs = _sim_kwargs_from_state(state)
    results = []
    for i in range(int(n_iter)):
        seed = derive_seed(base_seed, i)
        result, _plays = record_game_plays(
            factory_ref=_FACTORY_REF, seed=seed, sim_kwargs=sim_kwargs
        )
        results.append(result)
    return results


async def run(args: argparse.Namespace) -> int:
    from api.routes.games import _resolve_state_or_error
    from simulation.win_probability import win_probability

    seasons = sorted({int(s) for s in args.seasons}, reverse=True)
    log.info("SIM-407 validation over seasons %s (iterations=%d).", seasons, args.iterations)

    games = await _fetch_final_games(args.dsn, seasons, args.max_games)
    log.info("Found %d completed games to validate.", len(games))
    if not games:
        log.warning("No completed games found — nothing to validate.")
        return 0

    import asyncpg

    winprob_pairs: list[tuple[float, int]] = []
    prop_pairs_by_line: dict[tuple[str, float], list] = {}
    n_done = 0
    n_props = 0
    # Pool created INSIDE the try so a construction failure still hits the finally.
    pool = None
    try:
        pool = await asyncpg.create_pool(args.dsn, min_size=1, max_size=4)
        for g in games:
            game_pk = g["game_pk"]
            try:
                state = await _resolve_state_or_error(pool, game_pk)
            except Exception as exc:  # noqa: BLE001 - skip un-resolvable games
                log.info("skip game %s (state unresolved: %s)", game_pk, type(exc).__name__)
                continue

            results = await asyncio.to_thread(
                _collect_game_results, state, args.iterations, args.base_seed
            )
            if not results:
                continue

            # Win prob (identity map here — we are FITTING the map, so we validate
            # the RAW smoothed probability). win_probability accepts the per-
            # iteration results list and aggregates it (SIM-330).
            wp = win_probability(results)
            home_won = 1 if g["home_score"] > g["away_score"] else 0
            winprob_pairs.append((float(wp.home_win_prob), home_won))

            # Prop PMFs (from the per-iteration boxscores) paired against the REAL
            # per-player outcomes derived from raw.pitches.events for this game.
            if not args.no_props:
                pset = PropDistributionSet.from_results(results)
                pa_events = await _fetch_pa_events(pool, game_pk)
                batter_actuals, pitcher_actuals = real_props_from_pa_events(pa_events)
                n_props += pair_props_for_validation(
                    pset, batter_actuals, pitcher_actuals, prop_pairs_by_line
                )

            n_done += 1
            if n_done % 25 == 0:
                log.info("  validated %d/%d games (%d prop pairs) ...", n_done, len(games), n_props)
    finally:
        if pool is not None:
            await pool.close()

    log.info("Building validation report from %d games (%d prop pairs).", n_done, n_props)
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
        help="Win-prob validation only (skip the per-game prop pairing; faster smoke run).",
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
