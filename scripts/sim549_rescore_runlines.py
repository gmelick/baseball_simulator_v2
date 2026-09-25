"""
scripts/sim549_rescore_runlines.py — SIM-549: re-price the run-line records of
stored accuracy reports, beside the originals.

WHY THIS EXISTS
---------------
The accuracy comparison (``scripts/clv_backtest.py``) priced every closing run
line as the two sides of ONE bet. The book often lists the two teams' run lines
as two SEPARATE bets instead — home -1.5 and away -1.5, both lost on a tie —
and pairing their prices inflated the line's probability (by a fifth on the
2024 first-five run line). Since SIM-549 the comparison scores such a row as
two bets, each priced from its own price over the margin of the same game's
two-way markets. This script brings the reports written BEFORE the fix to the
same scoring without re-running the simulator:

  * a PAIR (the away spread is the negative of the home spread) keeps its
    record exactly;
  * a row listed as TWO SEPARATE BETS re-prices its home record: the market
    probability becomes the home price's implied probability over the same
    game's reference margin, and the record loses its fade price;
  * the AWAY bet's record cannot be built here — its simulator probability
    needs the per-iteration margins, which a report does not store — so it
    joins from the next backtest run.

The spreads are not in a report's records, so the script reads the closing
rows from ``raw.game_odds`` (the latest ``fetched_at`` per game and market,
the backtest's own rule). It checks every stored price against the store. It
refuses to write a report with a missing closing row or a price the store does
not hold, unless ``--accept-problems``; then ``params.rescored.problems`` lists
them, because those records keep their old pair-priced number. It uses the backtest's own
functions (:func:`run_line_is_pair`, :func:`reference_margin`), so a re-scored
home record carries the number a fresh run would give it.

The output sits beside the input as ``<name>.sim549.json`` and is stamped
``params.run_line_scoring = "sim549.1"`` with ``params.rescored`` (the source,
the counts, the date). A report already stamped is refused; so is a file with
no ``accuracy_records`` key (a paired read — regenerate it with
``scripts/sim518_pair_accuracy.py`` over the re-scored reports instead). An
existing output is kept unless ``--overwrite``.

    docker compose run --rm -v "$PWD/scripts:/app/scripts" app \\
        python scripts/sim549_rescore_runlines.py scripts/sim548_accuracy_split.json ...

The plan: docs/audit/2026-09-23-sim549-first-five-run-line-two-bets-plan.md §5.4.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import importlib.util
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.odds_provider import GAME_MARKET_SEGMENT  # noqa: E402

#: The suffix of a re-scored report, beside its original.
OUTPUT_SUFFIX = ".sim549.json"
#: The two-way markets :func:`reference_margin` reads, besides the run lines.
_REFERENCE_MARKETS: tuple[str, ...] = ("total", "f5_total", "f1_total", "moneyline")
#: Two prices this close are the same price (they are stored as numerics).
_PRICE_TOLERANCE = 1e-9


def _load_backtest_module() -> Any:
    """The backtest, imported by path (scripts/ is not a package)."""
    if "clv_backtest" in sys.modules:
        return sys.modules["clv_backtest"]
    spec = importlib.util.spec_from_file_location(
        "clv_backtest", str(_ROOT / "scripts" / "clv_backtest.py")
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["clv_backtest"] = mod
    spec.loader.exec_module(mod)
    return mod


bt = _load_backtest_module()


class RescoreRefused(ValueError):
    """The report cannot be re-scored (already stamped, or not a backtest report)."""


def output_path(path: str) -> str:
    """``scripts/x.json`` -> ``scripts/x.sim549.json``."""
    p = Path(path)
    return str(p.with_name(p.stem + OUTPUT_SUFFIX))


def run_line_games(report: dict[str, Any]) -> set[int]:
    """The games whose run-line records need their closing rows."""
    return {
        int(r["game_pk"])
        for r in report.get("accuracy_records", [])
        if r.get("market") in bt.RUN_LINE_MARKET_TYPES
    }


async def fetch_closing_rows(dsn: str, game_pks: set[int]) -> dict[int, dict[str, Any]]:
    """``{game_pk: {market_type: {"closing": row}}}`` — the backtest's own odds
    shape — for the run lines and the reference markets of ``game_pks``."""
    import asyncpg

    markets = list(bt.RUN_LINE_MARKET_TYPES) + list(_REFERENCE_MARKETS)
    conn = await asyncpg.connect(dsn, timeout=60)
    try:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (game_pk, market_type)
                   game_pk, market_type,
                   home_ml, away_ml, draw_ml,
                   home_spread, home_spread_ml, away_spread, away_spread_ml,
                   total_line, over_ml, under_ml
            FROM raw.game_odds
            WHERE line_type = 'closing'
              AND game_pk = ANY($1::bigint[])
              AND market_type = ANY($2::text[])
            ORDER BY game_pk, market_type, fetched_at DESC
            """,
            sorted(game_pks),
            markets,
        )
    finally:
        await conn.close()
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        row = {
            k: (None if v is None else float(v))
            for k, v in dict(r).items()
            if k not in ("game_pk", "market_type")
        }
        out.setdefault(int(r["game_pk"]), {})[str(r["market_type"])] = {"closing": row}
    return out


def _same_price(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= _PRICE_TOLERANCE


def _report_margin(
    records_by_key: dict[tuple[int, str], dict[str, Any]], game_pk: int, market: str
) -> float | None:
    """The report's own record of a two-way market: its two prices' margin."""
    r = records_by_key.get((game_pk, market))
    if r is None or r.get("market_side_price") is None or r.get("market_other_price") is None:
        return None
    return bt._implied(float(r["market_side_price"])) + bt._implied(float(r["market_other_price"]))


def _row_read(records: list[dict[str, Any]], market: str) -> dict[str, Any] | None:
    """n, the two Brier scores, the gap and the line's bias of one market."""
    rs = [r for r in records if r.get("market") == market]
    if not rs:
        return None
    y = np.array([float(r["outcome"]) for r in rs])
    ps = np.array([float(r["sim_prob"]) for r in rs])
    pm = np.array([float(r["market_prob"]) for r in rs])
    return {
        "n": len(rs),
        "brier_sim": float(np.mean((ps - y) ** 2)),
        "brier_mkt": float(np.mean((pm - y) ** 2)),
        "gap": float(np.mean((ps - y) ** 2) - np.mean((pm - y) ** 2)),
        "bias_mkt": float(pm.mean() - y.mean()),
    }


def rescore_report(
    report: dict[str, Any],
    closing: dict[int, dict[str, Any]],
    *,
    source: str,
    date: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """PURE: the re-scored report and a summary of what changed.

    ``closing`` is :func:`fetch_closing_rows`'s output. Raises
    :class:`RescoreRefused` on a report already stamped or with no records.
    The summary's ``problems`` lists every record whose closing row is missing
    or whose stored price differs from the store; the caller refuses to write
    such a report unless forced.
    """
    params = dict(report.get("params") or {})
    if params.get("run_line_scoring"):
        raise RescoreRefused(
            f"already scored by run-line shape ({params['run_line_scoring']}); nothing to re-score"
        )
    if "accuracy_records" not in report:
        raise RescoreRefused(
            "no accuracy_records — not a clv_backtest report (a paired read? regenerate it "
            "with scripts/sim518_pair_accuracy.py over the re-scored reports)"
        )
    records = [dict(r) for r in report["accuracy_records"]]
    by_key = {
        (int(r["game_pk"]), str(r["market"])): r for r in records if r.get("player_id") is None
    }
    tally: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    problems: list[str] = []
    before = {m: _row_read(records, m) for m in bt.RUN_LINE_MARKET_TYPES}
    kept: list[dict[str, Any]] = []
    for r in records:
        market = r.get("market")
        if market not in bt.RUN_LINE_MARKET_TYPES:
            kept.append(r)
            continue
        game_pk = int(r["game_pk"])
        odds = closing.get(game_pk, {})
        row = (odds.get(market) or {}).get("closing")
        if row is None:
            problems.append(f"game {game_pk} {market}: no closing row in the store")
            kept.append(r)
            continue
        if not (
            _same_price(row.get("home_spread_ml"), r.get("market_side_price"))
            and _same_price(row.get("away_spread_ml"), r.get("market_other_price"))
        ):
            problems.append(
                f"game {game_pk} {market}: stored prices {r.get('market_side_price')}/"
                f"{r.get('market_other_price')} vs the store's {row.get('home_spread_ml')}/"
                f"{row.get('away_spread_ml')}"
            )
            kept.append(r)
            continue
        cp = bt._closing_prices(
            odds, market, "home_spread_ml", "away_spread_ml", "home_spread", "away_spread"
        )
        if cp is None or cp.line is None or cp.other_line is None:
            # the scorer gives such a row no record: its shape is unknown
            tally[f"{market}:dropped_no_away_spread"] += 1
            continue
        if bt.run_line_is_pair(cp.line, cp.other_line):
            tally[f"{market}:pairs"] += 1
            kept.append(r)
            continue
        segment = GAME_MARKET_SEGMENT[market]
        margin, margin_source = bt.reference_margin(odds, segment)
        sources[margin_source] += 1
        own = _report_margin(by_key, game_pk, margin_source)
        if own is not None:
            tally["margin_checked_against_the_report"] += 1
            if abs(own - margin) > 1e-9:
                tally["margin_differs_from_the_report"] += 1
        r["market_prob"] = float(min(bt._implied(float(cp.side)) / margin, 1.0))
        r["market_other_price"] = None
        tally[f"{market}:repriced"] += 1
        kept.append(r)
    objs = [bt.AccuracyRecord.from_jsonable(r) for r in kept]
    n_boot = int(params.get("bootstrap_samples", bt.DEFAULT_BOOTSTRAP_SAMPLES))
    seed = int(params.get("bootstrap_seed", 538))
    out = dict(report)
    # the records as they came: an untouched record stays byte-identical to
    # the original (a round trip through AccuracyRecord would add fields)
    out["accuracy_records"] = kept
    out["accuracy_comparison"] = bt.aggregate_accuracy_comparison(
        objs, n_bootstrap=n_boot, seed=seed
    )
    if "hypothetical_return" in report:
        out["hypothetical_return"] = bt.aggregate_hypothetical_return(
            objs,
            edge_threshold=float(params.get("edge_threshold", bt.DEFAULT_RETURN_EDGE_THRESHOLD)),
            n_bootstrap=n_boot,
            seed=seed,
        )
    counters = dict(report.get("counters") or {})
    counters["n_accuracy_records"] = len(objs)
    counters["market_shapes"] = bt.market_shape_tally(objs)
    out["counters"] = counters
    repriced = sum(v for k, v in tally.items() if k.endswith(":repriced"))
    params["run_line_scoring"] = bt.RUN_LINE_SCORING_VERSION
    params["rescored"] = {
        "from": source,
        "markets": list(bt.RUN_LINE_MARKET_TYPES),
        "records": repriced,
        # records kept unchanged because their closing row was missing or their
        # price differed from the store's (written only with --accept-problems)
        "problems": list(problems),
        "dropped": sum(v for k, v in tally.items() if k.endswith(":dropped_no_away_spread")),
        "reference_margin_sources": dict(sources),
        "away_records": (
            "absent: the away bet's simulator probability needs the per-iteration "
            "margins, which a report does not store; the next backtest run carries them"
        ),
        "date": date,
    }
    out["params"] = params
    after = {m: _row_read(out["accuracy_records"], m) for m in bt.RUN_LINE_MARKET_TYPES}
    summary = {
        "tally": dict(tally),
        "reference_margin_sources": dict(sources),
        "problems": problems,
        "before": before,
        "after": after,
    }
    return out, summary


def _print_summary(path: str, summary: dict[str, Any]) -> None:
    print(f"=== {path}")
    for k, v in sorted(summary["tally"].items()):
        print(f"  {k}: {v}")
    print(f"  reference margin from: {summary['reference_margin_sources']}")
    for m in bt.RUN_LINE_MARKET_TYPES:
        b, a = summary["before"].get(m), summary["after"].get(m)
        if b is None and a is None:
            continue
        for label, row in (("before", b), ("after ", a)):
            if row is None:
                print(f"  {m:12s} {label}: no records")
                continue
            print(
                f"  {m:12s} {label}: n {row['n']:5d}  Brier sim {row['brier_sim']:.4f} "
                f"mkt {row['brier_mkt']:.4f}  gap {row['gap']:+.4f}  line bias {row['bias_mkt']:+.3f}"
            )
    if summary["problems"]:
        print(f"  PROBLEMS ({len(summary['problems'])}):")
        for p in summary["problems"][:20]:
            print(f"    {p}")


async def _main_async(args: argparse.Namespace) -> int:
    reports: list[tuple[str, dict[str, Any]]] = []
    games: set[int] = set()
    status = 0
    for path in args.reports:
        rep = json.load(open(path, encoding="utf-8"))
        reports.append((path, rep))
        games |= run_line_games(rep)
    closing = await fetch_closing_rows(args.dsn, games) if games else {}
    date = dt.date.today().isoformat()
    for path, rep in reports:
        try:
            out, summary = rescore_report(rep, closing, source=path, date=date)
        except RescoreRefused as exc:
            print(f"=== {path}\n  REFUSED — {exc}")
            status = 1
            continue
        _print_summary(path, summary)
        dest = output_path(path)
        if summary["problems"] and not args.accept_problems:
            print(
                f"  NOT WRITTEN — {len(summary['problems'])} problems "
                "(pass --accept-problems to write them into params.rescored.problems)"
            )
            status = 1
            continue
        if os.path.exists(dest) and not args.overwrite:
            print(f"  NOT WRITTEN — {dest} exists (pass --overwrite to replace it)")
            status = 1
            continue
        with open(dest, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2)
        print(f"  wrote {dest}")
    return status


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("reports", nargs="+", help="clv_backtest JSON reports")
    ap.add_argument("--dsn", default=os.environ.get("BASEBALL_DB_DSN", ""))
    ap.add_argument(
        "--accept-problems",
        action="store_true",
        help="write a report with price problems; they are listed in params.rescored.problems",
    )
    ap.add_argument("--overwrite", action="store_true", help="replace an existing output")
    args = ap.parse_args(argv)
    if not args.dsn:
        ap.error("no DSN: pass --dsn or set BASEBALL_DB_DSN")
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
