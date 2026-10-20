#!/usr/bin/env python
"""
scripts/accuracy_comparison.py
==============================
The sim-vs-closing-line accuracy comparison (SIM-538): the platform's validation
of the simulator against the market.

For every completed game with a closing betting price, the comparison takes the
simulator's own probability of an outcome and the closing line's probability of
the same outcome with the book's margin removed (de-vigged), and scores both
against what happened with a PROPER SCORING RULE (Brier score, log loss): a rule
under which a forecaster cannot improve the score by hedging toward a safe
prediction, only by being more accurate. Both scores come from the same game and
the same outcome, so the comparison is PAIRED (the simulator's score minus the
market's, per observation), which needs a smaller sample to detect a real
difference than two unrelated averages do. A model cannot beat the market here by
being bland: mirroring the pool average scores no better than the market's own
price.

The closing line is the benchmark (owner decision 2026-09-10) because the
simulator has a probability only once both lineups post, after the lines open,
and because nearly every game has a closing line while a matched opening line is
often missing. The plan: ``docs/audit/2026-09-10-sim536-541-market-accuracy-plan.md``.

The stages live in the package ``scripts/accuracy/`` (its ``__init__`` lists them);
this file is the command line.

WHAT THIS DOES NOT CLAIM
------------------------
* That an edge was CAPTURABLE. Beating the closing line's accuracy is necessary
  for a real edge, but proving it was bankable needs a price snapshot at the
  moment the model can act (right after lineups post), which the platform does
  not collect.
* That the point-in-time cutoff makes a backtest game untouched by later data.
  The cutoff bounds the PLAY-POOL ROWS the sampler can draw. The player skill
  profiles that WEIGHT those draws come from one bundle built at one date for the
  whole run, so a January game can still be weighted by a player's later form. A
  bundle per game date is not feasible at backtest scale; date-bucketed profile
  snapshots, or accepting the residual as documented look-ahead, would close it.
* That a bucket mixing several markets or players of one game (the "overall"
  row; a prop bucket such as K or H) holds as many independent trials as rows.
  Those rows share one game's park, bullpen and script, so the interval resamples
  GAMES (``accuracy/confidence.py``). It is honest about the number of games
  behind it, but a bucket built from a few busy games still carries less
  information than the same row count spread over as many games.

THE MARKETS
-----------
* Game: the moneyline (home), the total (over at ``total_line``) and the run line
  (home at ``home_spread``; a run line listed as two separate bets also scores the
  away bet at ``away_spread``, key ``runline_away``), and the twelve segment and
  team markets (the first-five and first-inning run lines scored the same way).
* Props: the fifteen markets of ``accuracy.records.PROP_VOCAB_MAP``, every one on a
  game graded by the official box score, and K/BB/H/HR/TB alone on a game graded
  by the event-label fallback (``accuracy/grading.py``).

Each record scores a FIXED reference side (home, or over), never the model's own
pick. Each market is graded against one book (``accuracy/pricing.py``).

HOW IT REUSES THE PLATFORM'S SEAMS
----------------------------------
* The replay: ``_resolve_state_or_error`` resolves a game's lineup into a
  GameState, and ``record_game_plays`` replays N iterations under the production
  factory ref (the path of ``api/routes/betting.py`` and
  ``scripts/validate_props.py``). The results build a ``GameSimSummary``, a
  ``WinProbability`` and a ``PropDistributionSet``.
* The de-vig and the edge math: ``betting.clv_engine`` (``moneyline_edge_report``,
  ``total_over_under_edge_report``, ``prop_edge_report``,
  ``run_line_bet_cover_prob``, ``devig_two_way``, ``devig_one_sided``), fed the
  CLOSING quote.
* The scoring rules: ``simulation.prop_validation.binary_brier`` and
  ``binary_log_loss``, once with the simulator's probabilities and once with the
  market's, over the same outcomes.
* The park factor: ``resolve_park_factor_onto_state`` resolves the venue's run
  factor before the replay. The run REFUSES to start when the sim DuckDB will not
  open (exit 2): a neutral 1.0 cannot be told apart from a real neutral park
  (SIM-452).
* The point-in-time cutoff: ``resolve_asof_ymd`` sets ``state.asof_ymd``, which the
  sim kwargs carry as ``_asof_ymd``, so a replay never draws a pool row or a profile
  dated after its own game.
* The calibration: the win probability is scored through the fitted reliability
  curve the live API applies (``api.state.load_calibration_report`` and
  ``CalibrationMap.from_report``; the identity map without a report), so the
  moneyline scores the probability production serves.

The record builders (``accuracy/grading.py``) and the tables
(``accuracy/report.py``, ``accuracy/hypothetical_return.py``) are pure; the readers
and the replay touch the database and the simulator.

MINIMUM SAMPLE SIZE
-------------------
Every row states the fewest observations it needs before its result means
anything. ``accuracy/confidence.py`` holds the method and what it does not claim.

HYPOTHETICAL DOLLAR RETURN
--------------------------
``--report-hypothetical-return`` adds a companion table: a 1-unit bet at the
closing price on every disagreement of at least ``--edge-threshold``. It is a
diagnostic, NOT a certified betting edge; ``accuracy/hypothetical_return.py`` holds
its scope and the reasons.

EXIT CODES
----------
Every abandoned run leaves its own code (the ``EXIT_*`` constants). A run that
measured nothing never exits 0: a silent no-op must not read like a healthy run.

USAGE
-----
    # In the app container (Postgres at db:5432, DuckDB at /data/...):
    python scripts/accuracy_comparison.py --seasons 2024 --max-games 50 --iterations 100
    python scripts/accuracy_comparison.py --seasons 2023 2024 --markets game
    python scripts/accuracy_comparison.py --seasons 2024 --markets props
    # full season, 6 games at once (the across-games parallel mode):
    python scripts/accuracy_comparison.py --seasons 2024 --workers 6 --iterations 100
    # the serial path (no pool; the reference of a byte-identical check):
    python scripts/accuracy_comparison.py --seasons 2024 --max-games 2 --workers 1
    # ALSO print the hypothetical dollar return (opt-in):
    python scripts/accuracy_comparison.py --seasons 2024 --report-hypothetical-return
    python scripts/accuracy_comparison.py --seasons 2024 --report-hypothetical-return --edge-threshold 0.05
    # grade against the vendor's blend instead (a second benchmark):
    python scripts/accuracy_comparison.py --seasons 2024 --benchmark-book bp:0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
# The stages live in scripts/accuracy/. A caller that loads this file by path
# (a probe in scripts/probes/, a test) may not have scripts/ on sys.path.
if _SCRIPTS_DIR not in sys.path:
    sys.path.append(_SCRIPTS_DIR)

from accuracy.confidence import (  # noqa: E402
    DEFAULT_ALPHA,
    DEFAULT_BOOTSTRAP_SAMPLES,
    DETECTION_MARGIN,
    MIN_DETECTABLE_EDGE,
)
from accuracy.grading import (  # noqa: E402
    RUN_LINE_SCORING_VERSION,
    market_shape_tally,
    warn_market_shapes,
)
from accuracy.hypothetical_return import (  # noqa: E402
    DEFAULT_RETURN_EDGE_THRESHOLD,
    aggregate_hypothetical_return,
    format_hypothetical_return,
)
from accuracy.pricing import odds_row_provenance, resolve_benchmark_book  # noqa: E402
from accuracy.replay import (  # noqa: E402
    _FACTORY_REF,
    DEFAULT_CALIBRATION_PATH,
    DEFAULT_DSN,
    DEFAULT_DUCKDB_PATH,
    DEFAULT_WORKERS,
    WorkerInitError,
    _run_parallel,
    _run_serial,
)
from accuracy.report import aggregate_accuracy_comparison, format_accuracy_comparison  # noqa: E402
from accuracy.selection import (  # noqa: E402
    _fetch_final_games,
    _read_game_pks_file,
    bundle_provenance,
)

from pipeline.db_dsn import dsn_label, require_dsn  # noqa: E402
from simulation.win_probability import IDENTITY_CALIBRATION, CalibrationMap  # noqa: E402

log = logging.getLogger("accuracy_comparison")

DEFAULT_OUTPUT = os.environ.get("CLV_BACKTEST_PATH", "/data/accuracy_comparison.json")

# ---------------------------------------------------------------------------
# The exit codes (SIM-454). Every ABANDONED run leaves its own code. A backtest
# that measured nothing never exits 0: a silent no-op reads exactly like a
# healthy run.
# ---------------------------------------------------------------------------
#: Everything ran and at least one game was priced.
EXIT_OK = 0
#: The sim DuckDB would not open and the operator did not pass
#: ``--allow-neutral-parks``.
EXIT_NO_PARK_SOURCE = 2
#: A worker could not build its event loop, Postgres pool or park-factor source.
EXIT_WORKER_INIT = 3
#: A game replayed park-blind: a defect in this script.
EXIT_UNRESOLVED_PARK = 4
#: The run finished but priced NOTHING: no games found, or every game landed in
#: ``no_odds`` / ``unresolved`` / ``empty``. The table is empty, so the run
#: measured nothing and must not look successful.
EXIT_NOTHING_SCORED = 5
#: The parent process could not reach Postgres at all.
EXIT_INFRASTRUCTURE = 6


def _open_parent_duckdb(args: argparse.Namespace) -> Any:
    """The sim DuckDB of the parent process, or ``None``.

    The park factor lives in ``derived.park_factors`` in this file. Without it
    every game runs at a neutral 1.0 and ``SIM_PARK_FACTOR`` does nothing, so the
    backtest measures a simulator other than the one production serves.
    :func:`run` turns a ``None`` into a non-zero exit unless the operator passes
    ``--allow-neutral-parks``.
    """
    from simulation.sim_kwargs import open_sim_duckdb

    return open_sim_duckdb(args.duckdb)


async def run(args: argparse.Namespace) -> int:
    """Run the comparison, print its tables, write the JSON report and return the
    exit code."""
    from api.state import load_calibration_report
    from simulation.sim_kwargs import UnresolvedParkFactorError as _UnresolvedParkFactorError

    seasons = sorted({int(s) for s in args.seasons})
    do_game = args.markets in ("game", "all")
    do_props = args.markets in ("props", "all")
    workers = max(1, int(args.workers))
    log.info(
        "SIM-538 sim-vs-closing-line accuracy comparison — seasons=%s iterations=%d "
        "markets=%s workers=%d",
        seasons,
        args.iterations,
        args.markets,
        workers,
    )

    # The fitted calibration the live API applies at boot, loaded once in the
    # parent. The serial path passes the map; the parallel path passes only the
    # PATH (the worker parameters must stay picklable) and each worker loads its
    # own copy. A missing or corrupt report falls back to IDENTITY_CALIBRATION
    # instead of aborting the run.
    calibration_report = await asyncio.to_thread(load_calibration_report, args.calibration_path)
    calibration_map = (
        CalibrationMap.from_report(calibration_report)
        if calibration_report is not None
        else IDENTITY_CALIBRATION
    )
    log.info(
        "SIM-538: win probability scored through %s.",
        calibration_map.name
        if calibration_report is not None
        else "IDENTITY_CALIBRATION (no report found)",
    )

    # The park-factor source is checked BEFORE any sim runs (SIM-452). A backtest
    # that cannot read it measures a simulator nobody ships, so it refuses to start.
    duck = _open_parent_duckdb(args)
    if duck is None:
        if not args.allow_neutral_parks:
            log.error(
                "SIM-452: the sim DuckDB at %r would not open, so every park factor "
                "falls back to a neutral 1.0 and SIM_PARK_FACTOR is a no-op. This run "
                "would measure a simulator production does not serve. Fix the path "
                "(--duckdb) or pass --allow-neutral-parks to accept it.",
                args.duckdb,
            )
            return EXIT_NO_PARK_SOURCE
        log.warning(
            "SIM-452: running WITHOUT park factors (--allow-neutral-parks). Every game "
            "uses a neutral 1.0 and SIM_PARK_FACTOR does nothing this run."
        )

    try:
        game_pks = await _fetch_final_games(args.dsn, seasons, args.max_games)
        if args.game_pks_file:
            wanted = _read_game_pks_file(args.game_pks_file)
            game_pks = [g for g in game_pks if g in wanted]
            log.info(
                "SIM-518: --game-pks-file kept %d of the seasons' Final games (%d listed).",
                len(game_pks),
                len(wanted),
            )
    except Exception:
        # The parent cannot reach Postgres: name it and exit on a documented code.
        log.exception(
            "Could not read the game list from Postgres at %r.",
            dsn_label(args.dsn),  # the host only, never the password
        )
        if duck is not None:
            try:
                duck.close()
            except Exception:  # noqa: BLE001 — a close failure never masks the cause
                pass
        return EXIT_INFRASTRUCTURE
    log.info("Found %d completed games to backtest.", len(game_pks))
    if not game_pks:
        log.warning("No completed games found — nothing to backtest.")

    try:
        if workers <= 1:
            # The serial in-process path (the reference of a byte-identical check).
            counters = await _run_serial(
                game_pks,
                duck=duck,
                do_game=do_game,
                do_props=do_props,
                calibration_map=calibration_map,
                args=args,
            )
        else:
            # The across-games pool is sync, so it runs off the event loop.
            counters = await asyncio.to_thread(
                _run_parallel,
                game_pks,
                workers=workers,
                do_game=do_game,
                do_props=do_props,
                calibration_path=args.calibration_path,
                args=args,
            )
    except WorkerInitError:
        # A worker could not build its loop, pool or park-factor source. Every
        # remaining game would fail the same way, so the run stops with a named
        # cause instead of writing a season of "unresolved" rows and exiting 0.
        log.exception("SIM-454: worker initialisation failed — the run is ABANDONED.")
        return EXIT_WORKER_INIT
    except _UnresolvedParkFactorError:
        log.exception("SIM-452: a game replayed park-blind — the run is ABANDONED.")
        return EXIT_UNRESOLVED_PARK
    finally:
        if duck is not None:
            try:
                duck.close()
            except Exception:  # noqa: BLE001 — a close failure never sinks the run
                pass

    # Say how many scored games got a non-neutral park factor: all-neutral over a
    # real slate means the park nudge did nothing.
    if counters.games_scored and not counters.games_park_nonneutral:
        log.warning(
            "SIM-452: %d games scored and NOT ONE used a non-neutral park factor. "
            "SIM_PARK_FACTOR had no effect on this backtest.",
            counters.games_scored,
        )
    else:
        log.info(
            "SIM-452: %d/%d scored games ran with a non-neutral park factor.",
            counters.games_park_nonneutral,
            counters.games_scored,
        )
    # Say which record graded the props: with zero games on the official box score
    # the run graded five props per game, not every market.
    if do_props:
        log.info(
            "SIM-545: props graded on the official box score for %d games, on the "
            "event-label fallback for %d games.",
            counters.games_official_boxscore,
            counters.games_event_label,
        )

    params = {
        "seasons": seasons,
        "iterations": int(args.iterations),
        "markets": args.markets,
        "base_seed": int(args.base_seed),
        "workers": workers,
        # The host and the database, never the password.
        "dsn": dsn_label(args.dsn),
        "duckdb": args.duckdb,
        "park_factors_available": duck is not None,
        "factory_ref": _FACTORY_REF,
        # "calibration_applied" says whether the win-probability map in use is
        # calibrated, not merely whether a report loaded. A report with no fitted
        # reliability curve (make calibrate ran; make validate-props
        # --write-calibration has not) makes CalibrationMap.from_report fall back
        # to the IDENTITY_CALIBRATION singleton, and every game is scored
        # uncalibrated though the file exists. Comparing the resolved map with the
        # singleton is the honest check.
        "calibration_path": args.calibration_path,
        "calibration_applied": calibration_map is not IDENTITY_CALIBRATION,
        "bootstrap_samples": int(args.bootstrap_samples),
        "bootstrap_seed": int(args.bootstrap_seed),
        # The minimum-sample-size settings, so a reader of the report knows what
        # "underpowered" was measured against (accuracy/confidence.py).
        "min_detectable_edge": MIN_DETECTABLE_EDGE,
        "detection_margin": DETECTION_MARGIN,
        "base_alpha": DEFAULT_ALPHA,
        # The hypothetical return.
        "report_hypothetical_return": bool(args.report_hypothetical_return),
        "edge_threshold": float(args.edge_threshold),
        # The frozen inputs, for the paired-arm read.
        "game_pks_file": args.game_pks_file,
        "provenance": bundle_provenance(args.calibration_path),
        # The run lines are scored by their shape (a pair, or two separate bets).
        # The skill table refuses to merge a stamped report with an unstamped one.
        "run_line_scoring": RUN_LINE_SCORING_VERSION,
        # The odds rows the run graded against (the one-book rows, the preference
        # list, the benchmark book). The skill table and the paired read refuse to
        # merge or pair reports whose stamps differ.
        **odds_row_provenance(getattr(args, "benchmark_book", None)),
    }

    # The shape of every run line and the margin of every pair.
    market_shapes = market_shape_tally(counters.accuracy_records)
    warn_market_shapes(market_shapes)

    run_counters = {
        "games_attempted": counters.games_attempted,
        "games_scored": counters.games_scored,
        "games_no_odds": counters.games_no_odds,
        "games_unresolved": counters.games_unresolved,
        "games_park_nonneutral": counters.games_park_nonneutral,
        # The games per prop ground-truth source.
        "games_official_boxscore": counters.games_official_boxscore,
        "games_event_label": counters.games_event_label,
        "n_accuracy_records": len(counters.accuracy_records),
        "market_shapes": market_shapes,
    }

    accuracy_comparison = aggregate_accuracy_comparison(
        counters.accuracy_records,
        n_bootstrap=int(args.bootstrap_samples),
        seed=int(args.bootstrap_seed),
    )
    print(format_accuracy_comparison(accuracy_comparison, params=params, counters=run_counters))
    print()

    # The hypothetical return is opt-in; it reads the accuracy records above.
    hypothetical_return: dict[str, Any] | None = None
    if args.report_hypothetical_return:
        hypothetical_return = aggregate_hypothetical_return(
            counters.accuracy_records,
            edge_threshold=float(args.edge_threshold),
            n_bootstrap=int(args.bootstrap_samples),
            seed=int(args.bootstrap_seed),
        )
        print(format_hypothetical_return(hypothetical_return, params=params))
        print()

    report: dict[str, Any] = {
        "params": params,
        "counters": run_counters,
        "accuracy_comparison": accuracy_comparison,
        "accuracy_records": [a.to_jsonable() for a in counters.accuracy_records],
    }
    if hypothetical_return is not None:
        report["hypothetical_return"] = hypothetical_return
    out_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(out_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    log.info("Wrote CLV backtest report -> %s", args.output)

    # A run that priced nothing is a failure, not a result. The report is written
    # (the operator has the file to diagnose from), but the exit code says the run
    # measured nothing.
    if counters.games_scored == 0:
        log.error(
            "SIM-454: the backtest scored 0 games (attempted=%d, no_odds=%d, "
            "unresolved=%d) and produced %d accuracy records. This run "
            "measured NOTHING. Exiting %d.",
            counters.games_attempted,
            counters.games_no_odds,
            counters.games_unresolved,
            len(counters.accuracy_records),
            EXIT_NOTHING_SCORED,
        )
        return EXIT_NOTHING_SCORED

    # A run that scored SOME games but lost most of them is still suspect. Say so
    # out loud; do not fail it, because a season legitimately holds bad games.
    if counters.games_unresolved > counters.games_scored:
        log.warning(
            "SIM-454: %d games came back UNRESOLVED against %d scored. Check the "
            "worker logs before you read the accuracy comparison as a measurement.",
            counters.games_unresolved,
            counters.games_scored,
        )
    return EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Sim-vs-closing-line accuracy comparison (SIM-538): compares the "
            "simulator's own probability against the closing line's, scored "
            "with a proper scoring rule (Brier score, log loss)."
        )
    )
    p.add_argument("--seasons", type=int, nargs="+", required=True, help="Seasons to backtest.")
    p.add_argument("--max-games", type=int, default=None, help="Cap games (smoke run).")
    p.add_argument(
        "--game-pks-file",
        default=None,
        help=(
            "SIM-518: score ONLY the game_pks listed in this file (one per line, or a JSON "
            "list), intersected with the seasons' Final games — the paired-arm subset run. "
            "The same file on both arms keeps the pairing exact."
        ),
    )
    p.add_argument("--iterations", type=int, default=100, help="Monte-Carlo iters per game.")
    p.add_argument(
        "--markets",
        choices=("game", "props", "all"),
        default="all",
        help="Which markets to score (default all).",
    )
    p.add_argument("--base-seed", type=int, default=0, help="Reproducibility seed.")
    p.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=(
            "ACROSS-GAMES parallel workers (default %(default)s). Each forkserver "
            "worker runs a WHOLE game serially (~373 MB sampler cache each). "
            "--workers 1 is the SERIAL in-process fallback."
        ),
    )
    p.add_argument("--output", default=DEFAULT_OUTPUT, help="CLV backtest JSON report path.")
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (completed games + odds).")
    p.add_argument("--duckdb", default=DEFAULT_DUCKDB_PATH, help="Sim DuckDB path.")
    p.add_argument(
        "--allow-neutral-parks",
        action="store_true",
        help=(
            "SIM-452 escape hatch. Run even when the sim DuckDB will not open, with "
            "every park factor at a neutral 1.0. The run then measures a simulator "
            "production does not serve, so use it only for a plumbing smoke test."
        ),
    )
    p.add_argument(
        "--calibration-path",
        default=DEFAULT_CALIBRATION_PATH,
        help=(
            f"SIM-538: CalibrationReport JSON to score win probability through — "
            f"the SAME one the live API applies at boot (default: "
            f"{DEFAULT_CALIBRATION_PATH}). Missing/corrupt degrades to the identity "
            "map, same as the API's own boot fallback."
        ),
    )
    p.add_argument(
        "--bootstrap-samples",
        type=int,
        default=DEFAULT_BOOTSTRAP_SAMPLES,
        help=(
            "SIM-538: resamples for the paired-difference confidence interval "
            f"(default {DEFAULT_BOOTSTRAP_SAMPLES})."
        ),
    )
    p.add_argument(
        "--bootstrap-seed",
        type=int,
        default=538,
        help="SIM-538: seed for the paired bootstrap (deterministic re-runs).",
    )
    p.add_argument(
        "--benchmark-book",
        type=resolve_benchmark_book,
        default=None,
        help=(
            "SIM-555: grade against this ONE book instead of the preference list — a "
            "second benchmark, never the default. A stored label (bp:0 = the vendor's "
            "blend) or a short name (draftkings). The report records it; the skill table "
            "and the paired read never merge it with a preference-list run."
        ),
    )
    p.add_argument(
        "--report-hypothetical-return",
        action="store_true",
        help=(
            "SIM-540: also print the hypothetical dollar return — a diagnostic, "
            "NOT a certified betting edge (see the module docstring). Opt-in."
        ),
    )
    p.add_argument(
        "--edge-threshold",
        type=float,
        default=DEFAULT_RETURN_EDGE_THRESHOLD,
        help=(
            "SIM-540: minimum |sim_prob - market_prob| disagreement a bet must "
            f"clear to enter the hypothetical-return report (default "
            f"{DEFAULT_RETURN_EDGE_THRESHOLD})."
        ),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = parse_args(argv)
    args.dsn = args.dsn or require_dsn()  # fail loudly with no DSN (SIM-443b)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
