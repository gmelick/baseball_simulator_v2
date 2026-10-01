#!/usr/bin/env python
"""
scripts/sim555_rescore_reports.py — SIM-555: re-price the fixed-line records of
stored accuracy reports against the graded book, beside the originals.

WHY THIS EXISTS
---------------
The accuracy comparison (``scripts/clv_backtest.py``) used to read one
``consensus`` closing row per market, and that row could mix books: each side's
price was the newest-stamped line of any book. Since SIM-555 the store holds one
row per book (``book = 'bp:<id>'``) and the comparison grades against ONE book:
the first book on ``GRADED_BOOK_PREFERENCE`` with a closing row for the market,
the same list on every game (the blend, the daily-fantasy apps, the exchanges
and the prediction markets are never graded). This script brings the reports
written before SIM-555 to the new graded row without re-running the simulator,
where that is possible:

  * A FIXED-LINE record (moneyline, first-inning and first-five moneyline,
    first team to score, a run in the first inning) keeps its simulator
    probability and its outcome: neither depends on the price. Its market
    probability is re-computed from the graded book's closing row with the
    backtest's own de-vig (``betting.clv_engine``: two-way, or three-way on the
    first-inning and first-five moneylines), and its two stored prices become
    the graded book's. A market with no graded row, or a three-way row with no
    tie price, gives the record up: a fresh run gives no record either.
  * A LINE-CARRYING record (a total, a team total, a run line, a prop) needs
    the simulator when the graded line differs from the old one, because the
    simulator's probability belongs to the line. The script only COUNTS those
    records per market (same line, line moved, no graded row), and leaves them
    as they were, still priced from the old consensus row.

The old line comes from the store's old ``consensus`` closing row, read by the
backtest's old rule (the latest by fetch time). The script reads those rows
from the live tables and from their archives (``raw.game_odds_archive`` /
``raw.prop_odds_archive``, where ``scripts/sim555_retire_consensus_rows.py``
moves them), so a re-run after the retirement sees the same old lines. A
re-loaded game with line-carrying records and no old row in either place is a
problem (the line counts cannot be read for it).

WHAT IT WRITES
--------------
``<name>.sim555.json`` beside each report: the re-priced records, the
re-aggregated accuracy comparison (and hypothetical return, when present), the
market-shape counters, and in ``params``: ``odds_row_version``,
``graded_book_preference``, ``benchmark_book`` (``None``), ``rescored_from``
and ``rescored_sim555`` (the counts, the graded books, the line counts, the
problems, the date). A report that already carries ``odds_row_version`` is
refused, and so is a file with no ``accuracy_records`` (a paired read:
regenerate it with ``scripts/sim518_pair_accuracy.py`` over the re-scored
reports). A game whose odds are not re-loaded yet (no ``bp:`` closing row) is
a problem: its records stay unchanged. A re-loaded game with line-carrying
records and no old consensus row is a problem too. A report with a problem is
written only with ``--accept-problems``. An existing output is kept unless
``--overwrite``.

    docker compose run --rm -v "$PWD/scripts:/app/scripts" app \\
        python scripts/sim555_rescore_reports.py scripts/sim548_accuracy_split.json ...

The plan: docs/audit/2026-09-25-sim555-one-book-per-odds-row-plan.md §3 item 10,
§4 and §8 step 7.
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
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from betting.clv_engine import (  # noqa: E402
    devig_multiway,
    devig_two_way,
    implied_prob_from_american,
)
from pipeline.odds_provider import (  # noqa: E402
    GAME_MARKET_KIND,
    GAME_MARKET_TYPES,
    GRADED_BOOK_PREFERENCE,
    ODDS_ROW_VERSION,
    PROP_STAT_TO_MODEL_PROP,
    STORED_BOOK_FILTER_SQL,
    bettable_labels,
    graded_book_labels,
    graded_row_order_sql,
)

#: The suffix of a re-scored report, beside its original.
OUTPUT_SUFFIX = ".sim555.json"

#: The markets whose records carry no line: re-priced here from the graded row.
FIXED_LINE_MARKETS: tuple[str, ...] = (
    "moneyline",
    "f1_moneyline",
    "f5_moneyline",
    "first_to_score",
    "first_inning_run",
)

#: The model prop name a record carries → the ``raw.prop_odds.prop_stat`` it prices.
MODEL_PROP_TO_PROP_STAT: dict[str, str] = {m: s for s, m in PROP_STAT_TO_MODEL_PROP.items()}

#: Two prices this close are the same price (the store keeps whole numbers).
_PRICE_TOLERANCE = 1e-9

_GAME_COLUMNS = (
    "home_ml, away_ml, draw_ml, home_spread, home_spread_ml, away_spread, away_spread_ml, "
    "total_line, over_ml, under_ml"
)

#: The first ORDER BY term after the keys of a graded game-market read: a
#: three-way row whose book lists no tie sorts after every row that lists one,
#: so a tie-less row is graded only when no book lists the tie. The same term,
#: built the same way, as ``INCOMPLETE_THREE_WAY_LAST_SQL`` in
#: ``scripts/clv_backtest.py`` (the backtest module is loaded below this SQL,
#: so the term is built here; a unit test keeps the two equal). The re-price of
#: the first-inning and first-five moneylines needs the tie.
INCOMPLETE_THREE_WAY_LAST_SQL = "(draw_ml IS NULL AND market_type IN ({}))".format(
    ", ".join(f"'{m}'" for m in GAME_MARKET_TYPES if GAME_MARKET_KIND[m] == "three_way")
)

#: The graded closing row per (game, market): the readers' rule (spec §6),
#: with the backtest's tie-last term right after the keys.
GRADED_GAME_SQL = f"""
SELECT DISTINCT ON (game_pk, market_type)
       game_pk, market_type, book, {_GAME_COLUMNS}
FROM raw.game_odds
WHERE game_pk = ANY($1::int[]) AND line_type = 'closing'
  AND {STORED_BOOK_FILTER_SQL} AND book = ANY($2::varchar[])
ORDER BY game_pk, market_type, {INCOMPLETE_THREE_WAY_LAST_SQL},
         {graded_row_order_sql("$3")}
"""

#: The old consensus closing row per (game, market): the backtest's old rule. The
#: rows are read from the live table AND its archive (migration 0028): the
#: retirement script moves them to the archive, and a re-run of this script
#: after the retirement must still see them.
OLD_GAME_SQL = f"""
SELECT DISTINCT ON (game_pk, market_type)
       game_pk, market_type, book, {_GAME_COLUMNS}
FROM (
    SELECT game_pk, market_type, book, fetched_at, {_GAME_COLUMNS}
    FROM raw.game_odds
    WHERE game_pk = ANY($1::int[]) AND line_type = 'closing' AND book = 'consensus'
    UNION ALL
    SELECT game_pk, market_type, book, fetched_at, {_GAME_COLUMNS}
    FROM raw.game_odds_archive
    WHERE game_pk = ANY($1::int[]) AND line_type = 'closing' AND book = 'consensus'
) o
ORDER BY game_pk, market_type, fetched_at DESC
"""

GRADED_PROP_SQL = f"""
SELECT DISTINCT ON (game_pk, player_id, prop_stat)
       game_pk, player_id, prop_stat, book, line, over_ml, under_ml
FROM raw.prop_odds
WHERE game_pk = ANY($1::int[]) AND prop_stat = ANY($4::varchar[]) AND line_type = 'closing'
  AND {STORED_BOOK_FILTER_SQL} AND book = ANY($2::varchar[])
ORDER BY game_pk, player_id, prop_stat, {graded_row_order_sql("$3")}
"""

OLD_PROP_SQL = """
SELECT DISTINCT ON (game_pk, player_id, prop_stat)
       game_pk, player_id, prop_stat, book, line, over_ml, under_ml
FROM (
    SELECT game_pk, player_id, prop_stat, book, fetched_at, line, over_ml, under_ml
    FROM raw.prop_odds
    WHERE game_pk = ANY($1::int[]) AND prop_stat = ANY($2::varchar[]) AND line_type = 'closing'
      AND book = 'consensus'
    UNION ALL
    SELECT game_pk, player_id, prop_stat, book, fetched_at, line, over_ml, under_ml
    FROM raw.prop_odds_archive
    WHERE game_pk = ANY($1::int[]) AND prop_stat = ANY($2::varchar[]) AND line_type = 'closing'
      AND book = 'consensus'
) p
ORDER BY game_pk, player_id, prop_stat, fetched_at DESC
"""

#: The report's games that the re-load has reached (any bp: closing row).
LOADED_SQL = f"""
SELECT DISTINCT game_pk
FROM raw.game_odds
WHERE game_pk = ANY($1::int[]) AND line_type = 'closing' AND {STORED_BOOK_FILTER_SQL}
"""


def _load_backtest_module() -> Any:
    """The backtest, imported by path (``scripts/`` is not a package)."""
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


class Unpriceable(ValueError):
    """The graded row cannot price the record; ``reason`` names why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Repriced:
    """One fixed-line record's new market probability and prices."""

    market_prob: float
    side_price: float
    other_price: float | None


@dataclass
class OddsStore:
    """The rows the re-score reads, keyed the way the records name them.

    ``graded_game`` / ``old_game``: ``{game_pk: {market_type: row}}``;
    ``graded_prop`` / ``old_prop``: ``{(game_pk, player_id, prop_stat): row}``;
    ``loaded_games``: the games with any ``bp:`` closing row.
    """

    graded_game: dict[int, dict[str, dict[str, Any]]] = field(default_factory=dict)
    old_game: dict[int, dict[str, dict[str, Any]]] = field(default_factory=dict)
    graded_prop: dict[tuple[int, int, str], dict[str, Any]] = field(default_factory=dict)
    old_prop: dict[tuple[int, int, str], dict[str, Any]] = field(default_factory=dict)
    loaded_games: set[int] = field(default_factory=set)


def output_path(path: str) -> str:
    """``scripts/x.json`` → ``scripts/x.sim555.json``."""
    p = Path(path)
    return str(p.with_name(p.stem + OUTPUT_SUFFIX))


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def reprice_fixed_line(market_type: str, row: Mapping[str, Any]) -> Repriced:
    """PURE: a fixed-line record's market probability from one book's closing row.

    The reference side is the backtest's: HOME on a moneyline, a three-way
    moneyline and the first team to score; YES (stored as the over) on a run in
    the first inning. A two-way market de-vigs its two prices; a three-way
    market de-vigs home, away and the tie together and stores no fade price.
    Raises :class:`Unpriceable` (``missing_side``, ``no_tie_price`` or
    ``degenerate``).
    """
    kind = GAME_MARKET_KIND[market_type]
    if kind == "yes_no":
        side, other = _float(row.get("over_ml")), _float(row.get("under_ml"))
    else:
        side, other = _float(row.get("home_ml")), _float(row.get("away_ml"))
    if side is None or other is None:
        raise Unpriceable("missing_side")
    try:
        if kind == "three_way":
            draw = _float(row.get("draw_ml"))
            if draw is None:
                raise Unpriceable("no_tie_price")
            fair = devig_multiway(
                [
                    implied_prob_from_american(side),
                    implied_prob_from_american(other),
                    implied_prob_from_american(draw),
                ]
            )
            return Repriced(market_prob=float(fair[0]), side_price=side, other_price=None)
        return Repriced(
            market_prob=float(devig_two_way(side, other)[0]), side_price=side, other_price=other
        )
    except Unpriceable:
        raise
    except ValueError as exc:
        raise Unpriceable("degenerate") from exc


def line_key(market_type: str, row: Mapping[str, Any]) -> tuple[float | None, ...]:
    """PURE: the line a line-carrying row prices — what a record's simulator probability belongs to.

    A prop: its line. A run line: both spreads (they also decide the row's
    shape: a pair or two separate bets). A total-kind market: the total line.
    """
    if market_type == "prop":
        return (_float(row.get("line")),)
    if GAME_MARKET_KIND.get(market_type) == "runline":
        return (_float(row.get("home_spread")), _float(row.get("away_spread")))
    return (_float(row.get("total_line")),)


def _same_price(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= _PRICE_TOLERANCE


def record_prices_match(record: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    """PURE: do the record's stored prices equal a row's (does the old row explain the record)?

    A run line's away bet (``<market>_away``) stored the away price only; any
    record whose fade price is ``None`` is checked on its own price.
    """
    market, market_type = str(record.get("market")), str(record.get("market_type"))
    if market_type == "prop" or GAME_MARKET_KIND.get(market_type) in ("total", "yes_no"):
        side, other = row.get("over_ml"), row.get("under_ml")
    elif GAME_MARKET_KIND.get(market_type) == "runline":
        if market.endswith("_away"):
            side, other = row.get("away_spread_ml"), row.get("home_spread_ml")
        else:
            side, other = row.get("home_spread_ml"), row.get("away_spread_ml")
    else:
        side, other = row.get("home_ml"), row.get("away_ml")
    if not _same_price(record.get("market_side_price"), side):
        return False
    if record.get("market_other_price") is None:
        return True
    return _same_price(record.get("market_other_price"), other)


def report_needs(report: Mapping[str, Any]) -> tuple[set[int], set[str]]:
    """The report's games, and the ``prop_stat`` values its prop records price."""
    games: set[int] = set()
    stats: set[str] = set()
    for r in report.get("accuracy_records", []):
        games.add(int(r["game_pk"]))
        if r.get("market_type") == "prop":
            stat = MODEL_PROP_TO_PROP_STAT.get(str(r.get("market")))
            if stat is not None:
                stats.add(stat)
    return games, stats


def rescore_report(
    report: Mapping[str, Any],
    store: OddsStore,
    *,
    source: str,
    date: str,
    drop_not_reloaded: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """PURE: the re-scored report and a summary of what changed.

    Raises :class:`RescoreRefused` on a report already stamped with
    ``odds_row_version`` or with no ``accuracy_records``. The summary's
    ``problems`` lists every game the re-load has not reached, and every
    re-loaded game with line-carrying records but no old consensus row (live
    or archived); the caller refuses to write such a report unless forced.

    ``drop_not_reloaded`` (use it only when the report's seasons are fully
    re-loaded): a game with no ``bp:`` closing row is then a game the event
    matcher declines (its start sits hours or days from the vendor's nearest
    event, SIM-536), so its old consensus prices most likely belong to another
    game. Every record of such a game is dropped, as a fresh run drops the game
    (no graded row), and the games are listed in the params, not as problems.
    """
    params = dict(report.get("params") or {})
    if params.get("odds_row_version"):
        raise RescoreRefused(
            f"already graded against one book ({params['odds_row_version']}); nothing to re-score"
        )
    if "accuracy_records" not in report:
        raise RescoreRefused(
            "no accuracy_records — not a clv_backtest report (a paired read? regenerate it "
            "with scripts/sim518_pair_accuracy.py over the re-scored reports)"
        )
    tally: Counter[str] = Counter()
    graded_books: dict[str, Counter[str]] = {}
    lines: dict[str, Counter[str]] = {}
    unloaded: set[int] = set()
    # The games with any old consensus row (live or archived).
    old_games = set(store.old_game) | {key[0] for key in store.old_prop}
    no_old: set[int] = set()
    kept: list[dict[str, Any]] = []
    dropped_games: set[int] = set()
    for original in report["accuracy_records"]:
        r = dict(original)
        game_pk = int(r["game_pk"])
        market = str(r.get("market"))
        market_type = str(r.get("market_type"))
        loaded = game_pk in store.loaded_games
        if drop_not_reloaded and not loaded:
            dropped_games.add(game_pk)
            tally[f"{market}:dropped_not_reloaded"] += 1
            continue
        if market in FIXED_LINE_MARKETS:
            if not loaded:
                unloaded.add(game_pk)
                tally[f"{market}:kept_not_reloaded"] += 1
                kept.append(r)
                continue
            row = store.graded_game.get(game_pk, {}).get(market)
            if row is None:
                tally[f"{market}:dropped_no_graded_row"] += 1
                continue
            try:
                new = reprice_fixed_line(market, row)
            except Unpriceable as exc:
                tally[f"{market}:dropped_{exc.reason}"] += 1
                continue
            r["market_prob"] = new.market_prob
            r["market_side_price"] = new.side_price
            r["market_other_price"] = new.other_price
            graded_books.setdefault(market, Counter())[str(row.get("book"))] += 1
            tally[f"{market}:repriced"] += 1
            kept.append(r)
            continue
        # A line-carrying record: counted, never changed.
        counts = lines.setdefault(market, Counter())
        counts["records"] += 1
        kept.append(r)
        if not loaded:
            counts["game_not_reloaded"] += 1
            continue
        if game_pk not in old_games:
            no_old.add(game_pk)
        if market_type == "prop":
            stat = MODEL_PROP_TO_PROP_STAT.get(market)
            pid = r.get("player_id")
            key = (game_pk, int(pid), stat) if (stat is not None and pid is not None) else None
            old = store.old_prop.get(key) if key else None
            graded = store.graded_prop.get(key) if key else None
        else:
            old = store.old_game.get(game_pk, {}).get(market_type)
            graded = store.graded_game.get(game_pk, {}).get(market_type)
        if graded is None:
            counts["no_graded_row"] += 1
            continue
        if old is None:
            counts["no_old_row"] += 1
            continue
        if not record_prices_match(r, old):
            # The old row does not explain the record, so its line is not the record's.
            counts["old_row_unconfirmed"] += 1
            continue
        if line_key(market_type, graded) == line_key(market_type, old):
            counts["same_line"] += 1
        else:
            counts["line_moved"] += 1

    objs = [bt.AccuracyRecord.from_jsonable(r) for r in kept]
    n_boot = int(params.get("bootstrap_samples", bt.DEFAULT_BOOTSTRAP_SAMPLES))
    seed = int(params.get("bootstrap_seed", 538))
    out = dict(report)
    # The records as they came: an untouched record stays byte-identical to the
    # original (a round trip through AccuracyRecord would add fields).
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

    problems = [
        f"game {g}: not re-loaded (no bp: closing row); its fixed-line records keep the consensus prices"
        for g in sorted(unloaded)
    ] + [
        f"game {g}: no old consensus closing row, live or archived; its line-carrying "
        "records cannot be checked for a moved line"
        for g in sorted(no_old)
    ]
    dropped = {k: v for k, v in sorted(tally.items()) if ":dropped_" in k}
    params["odds_row_version"] = ODDS_ROW_VERSION
    params["graded_book_preference"] = list(GRADED_BOOK_PREFERENCE)
    params["benchmark_book"] = None
    params["rescored_from"] = source
    params["rescored_sim555"] = {
        "markets": list(FIXED_LINE_MARKETS),
        "records": sum(v for k, v in tally.items() if k.endswith(":repriced")),
        "dropped": dropped,
        "graded_books": {m: dict(c) for m, c in sorted(graded_books.items())},
        "line_markets": {m: dict(c) for m, c in sorted(lines.items())},
        "line_records_note": (
            "the line-carrying records (totals, run lines, props) keep their old consensus "
            "prices; a record whose graded line moved needs the simulator again"
        ),
        "problems": problems,
        "dropped_not_reloaded_games": sorted(dropped_games),
        "date": date,
    }
    out["params"] = params
    summary = {
        "tally": dict(tally),
        "graded_books": {m: dict(c) for m, c in graded_books.items()},
        "lines": {m: dict(c) for m, c in lines.items()},
        "problems": problems,
        "dropped_not_reloaded_games": sorted(dropped_games),
        "before": {m: _row_read(report["accuracy_records"], m) for m in FIXED_LINE_MARKETS},
        "after": {m: _row_read(kept, m) for m in FIXED_LINE_MARKETS},
    }
    return out, summary


def _row_read(records: list[dict[str, Any]], market: str) -> dict[str, Any] | None:
    """n, the two Brier scores and the gap of one market."""
    rs = [r for r in records if r.get("market") == market]
    if not rs:
        return None
    n = len(rs)
    b_sim = sum((float(r["sim_prob"]) - float(r["outcome"])) ** 2 for r in rs) / n
    b_mkt = sum((float(r["market_prob"]) - float(r["outcome"])) ** 2 for r in rs) / n
    return {"n": n, "brier_sim": b_sim, "brier_mkt": b_mkt, "gap": b_sim - b_mkt}


# ---------------------------------------------------------------------------
# Database reads (asyncpg; SELECT only)
# ---------------------------------------------------------------------------


def _row_dict(record: Mapping[str, Any], skip: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in dict(record).items():
        if k in skip:
            continue
        out[k] = v if (v is None or k == "book") else float(v)
    return out


async def fetch_store(dsn: str, game_pks: set[int], prop_stats: set[str]) -> OddsStore:
    """Every row the re-score reads for these games, in one read-only transaction."""
    import asyncpg

    store = OddsStore()
    games = sorted(game_pks)
    stats = sorted(prop_stats)
    conn = await asyncpg.connect(dsn, timeout=60)
    try:
        async with conn.transaction(readonly=True):
            skip_game = ("game_pk", "market_type")
            args = (games, bettable_labels(), graded_book_labels())
            for r in await conn.fetch(GRADED_GAME_SQL, *args):
                store.graded_game.setdefault(int(r["game_pk"]), {})[str(r["market_type"])] = (
                    _row_dict(r, skip_game)
                )
            for r in await conn.fetch(OLD_GAME_SQL, games):
                store.old_game.setdefault(int(r["game_pk"]), {})[str(r["market_type"])] = _row_dict(
                    r, skip_game
                )
            store.loaded_games = {int(r["game_pk"]) for r in await conn.fetch(LOADED_SQL, games)}
            if stats:
                skip_prop = ("game_pk", "player_id", "prop_stat")
                for r in await conn.fetch(GRADED_PROP_SQL, *args, stats):
                    key = (int(r["game_pk"]), int(r["player_id"]), str(r["prop_stat"]))
                    store.graded_prop[key] = _row_dict(r, skip_prop)
                for r in await conn.fetch(OLD_PROP_SQL, games, stats):
                    key = (int(r["game_pk"]), int(r["player_id"]), str(r["prop_stat"]))
                    store.old_prop[key] = _row_dict(r, skip_prop)
    finally:
        await conn.close()
    return store


def _print_summary(path: str, summary: dict[str, Any]) -> None:
    print(f"=== {path}")
    for k, v in sorted(summary["tally"].items()):
        print(f"  {k}: {v}")
    for m, books in sorted(summary["graded_books"].items()):
        print(f"  graded book, {m}: {books}")
    for m in FIXED_LINE_MARKETS:
        b, a = summary["before"].get(m), summary["after"].get(m)
        for label, row in (("before", b), ("after ", a)):
            if row is None:
                continue
            print(
                f"  {m:16s} {label}: n {row['n']:5d}  Brier sim {row['brier_sim']:.4f} "
                f"mkt {row['brier_mkt']:.4f}  gap {row['gap']:+.4f}"
            )
    if summary["lines"]:
        print("  line-carrying records (not re-priced; a moved line needs the simulator):")
        for m, c in sorted(summary["lines"].items()):
            print(f"    {m:18s} {dict(c)}")
    if summary.get("dropped_not_reloaded_games"):
        games = summary["dropped_not_reloaded_games"]
        print(
            f"  dropped {len(games)} games the event matcher declines (no bp: row; "
            f"--drop-not-reloaded): {', '.join(map(str, games))}"
        )
    if summary["problems"]:
        print(f"  PROBLEMS ({len(summary['problems'])}):")
        for p in summary["problems"][:20]:
            print(f"    {p}")


async def _main_async(args: argparse.Namespace) -> int:
    reports: list[tuple[str, dict[str, Any]]] = []
    games: set[int] = set()
    stats: set[str] = set()
    for path in args.reports:
        with open(path, encoding="utf-8") as fh:
            rep = json.load(fh)
        reports.append((path, rep))
        g, s = report_needs(rep)
        games |= g
        stats |= s
    store = await fetch_store(args.dsn, games, stats) if games else OddsStore()
    date = dt.date.today().isoformat()
    status = 0
    for path, rep in reports:
        try:
            out, summary = rescore_report(
                rep,
                store,
                source=path,
                date=date,
                drop_not_reloaded=bool(getattr(args, "drop_not_reloaded", False)),
            )
        except RescoreRefused as exc:
            print(f"=== {path}\n  REFUSED — {exc}")
            status = 1
            continue
        _print_summary(path, summary)
        dest = output_path(path)
        if summary["problems"] and not args.accept_problems:
            print(
                f"  NOT WRITTEN — {len(summary['problems'])} problems: games not re-loaded, or "
                "with no old consensus row (pass --accept-problems to write them into "
                "params.rescored_sim555.problems)"
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
        help="write a report with problems (see above); they are listed in the params",
    )
    ap.add_argument(
        "--drop-not-reloaded",
        action="store_true",
        help="drop every record of a game with no bp: closing row, as a fresh run does; "
        "use it only when the report's seasons are fully re-loaded (such a game is one the "
        "event matcher declines, and its old consensus prices likely belong to another game)",
    )
    ap.add_argument("--overwrite", action="store_true", help="replace an existing output")
    args = ap.parse_args(argv)
    if not args.dsn:
        ap.error("no DSN: pass --dsn or set BASEBALL_DB_DSN")
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
