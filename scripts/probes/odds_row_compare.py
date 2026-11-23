"""Run the historical odds loader with its vendor reads recorded or replayed, and capture its writes.

The row comparison of the betting-folder work (SIM-645) runs the loader twice over the same
games. The first run records every BettingPros and MLB response; the second replays them, so
both runs see the same input. Neither run writes to the database: the writes are captured to
a file of rows, and ``--compare`` checks that two such files hold the same rows.

Usage (in the app container, the checkout under test mounted at /app)::

    python scripts/probes/odds_row_compare.py --record DIR -- --seasons 2026 --game-dates ...
    python scripts/probes/odds_row_compare.py --replay DIR --out DIR2 -- <the same loader args>
    python scripts/probes/odds_row_compare.py --compare DIR DIR2
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import decimal
import json
import os
import sys
from collections import Counter
from typing import Any

import asyncpg

import betting.historical_loader as loader
from betting.bettingpros.match_games import TransportMixin


def _jsonable(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


class _Writes:
    """A pool stand-in: reads go to the real pool; writes are captured, never sent."""

    def __init__(self, pool: Any, out_path: str) -> None:
        self._pool = pool
        self._out = open(out_path, "w", encoding="utf8")

    async def executemany(self, sql: str, args: list[tuple[Any, ...]]) -> None:
        head = " ".join(sql.split())[:60]
        for a in args:
            self._out.write(json.dumps({"sql": head, "args": _jsonable(list(a))}) + "\n")

    async def execute(self, sql: str, *args: Any) -> str:
        head = " ".join(sql.split())[:60]
        self._out.write(json.dumps({"sql": head, "args": _jsonable(list(args))}) + "\n")
        return "INSERT 0 0"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)

    async def close(self) -> None:
        self._out.close()
        await self._pool.close()


def _install_http(mode: str, directory: str) -> None:
    """Record every vendor response to ``responses.jsonl`` as it arrives, or replay them.

    A response is kept as its JSON; a failed read as its exception's type and message,
    replayed as a ``RuntimeError`` with the same text (the loader catches any error the
    same way, and the comparison runs with no retries).
    """
    path = os.path.join(directory, "responses.jsonl")
    original = TransportMixin._http_get_json
    if mode == "record":
        out = open(path, "w", encoding="utf8")

        def recording(self: Any, url: str, headers: dict[str, str] | None = None) -> Any:
            try:
                result = original(self, url, headers)
            except Exception as exc:
                failed = f"{type(exc).__name__}: {exc}"
                out.write(json.dumps({"url": url, "raise": failed}) + "\n")
                out.flush()
                raise
            out.write(json.dumps({"url": url, "ok": result}) + "\n")
            out.flush()
            return result

        TransportMixin._http_get_json = recording  # type: ignore[method-assign]
        return
    saved: dict[str, list[dict[str, Any]]] = {}
    with open(path, encoding="utf8") as fh:
        for line in fh:
            rec = json.loads(line)
            saved.setdefault(rec["url"], []).append(rec)
    served: Counter[str] = Counter()

    def replaying(self: Any, url: str, headers: dict[str, str] | None = None) -> Any:
        calls = saved.get(url)
        if not calls:
            raise RuntimeError(f"replay: no recorded response for {url}")
        rec = calls[min(served[url], len(calls) - 1)]
        served[url] += 1
        if "raise" in rec:
            raise RuntimeError(rec["raise"])
        return rec["ok"]

    TransportMixin._http_get_json = replaying  # type: ignore[method-assign]


def _install_writes(out_dir: str) -> None:
    real_create = asyncpg.create_pool

    async def create_pool(*args: Any, **kwargs: Any) -> _Writes:
        return _Writes(await real_create(*args, **kwargs), os.path.join(out_dir, "rows.jsonl"))

    asyncpg.create_pool = create_pool  # type: ignore[assignment]


def _compare(a: str, b: str) -> int:
    def rows(d: str) -> list[str]:
        with open(os.path.join(d, "rows.jsonl"), encoding="utf8") as fh:
            return fh.read().splitlines()

    ra, rb = rows(a), rows(b)
    ca, cb = Counter(ra), Counter(rb)
    print(f"{a}: {len(ra)} rows; {b}: {len(rb)} rows")
    print(f"same order: {ra == rb}; same rows (any order): {ca == cb}")
    for label, diff in (("only in first", ca - cb), ("only in second", cb - ca)):
        for row, n in list(diff.items())[:5]:
            print(f"{label} (x{n}): {row[:300]}")
    return 0 if ca == cb else 1


def main() -> int:
    argv = sys.argv[1:]
    loader_args = argv[argv.index("--") + 1 :] if "--" in argv else []
    own = argv[: argv.index("--")] if "--" in argv else argv
    p = argparse.ArgumentParser()
    p.add_argument("--record")
    p.add_argument("--replay")
    p.add_argument("--out")
    p.add_argument("--compare", nargs=2)
    ns = p.parse_args(own)
    if ns.compare:
        return _compare(*ns.compare)
    mode, directory = ("record", ns.record) if ns.record else ("replay", ns.replay)
    out_dir = ns.out or directory
    os.makedirs(out_dir, exist_ok=True)
    _install_http(mode, directory)
    _install_writes(out_dir)
    args = loader.parse_args(loader_args)
    args.dsn = args.dsn or os.environ["BASEBALL_DB_DSN"]
    return asyncio.run(loader.run(args))


if __name__ == "__main__":
    raise SystemExit(main())
