#!/usr/bin/env python
"""
scripts/genexpr_crash_probe.py
==============================
The SIM-445 crash class — a probe that re-runs the workload that segfaulted,
with and without the database.

WHAT HAPPENED (2026-09-16)
--------------------------
The steal-runner score-matrix build segfaulted three times out of three inside
``pipeline/batch/engine_artifacts._key_of``. That function joined a GENERATOR
EXPRESSION (the lazy ``(... for a in key_attrs)`` that a C builtin pulls one
item at a time) with ``str.join``, once per query result — 6.7 million times
for 2,585 profiles. A standalone run of the same loop against the live engine
crashed after about 1.3 million results. The same work as a LIST COMPREHENSION
(the eager ``[... for a in key_attrs]``, which the compiler inlines so no
generator object exists) completed the build. The interpreter is CPython
3.13.15 in the app image, GIL on, no JIT.

WHAT THIS PROBE FOUND THE SAME EVENING
--------------------------------------
The fault is STOCHASTIC, not a property of the shape:

* every synthetic shape below ran clean, 6,679,640 evaluations each, on the
  container's 3.13.15 (numpy 2.5.3, DuckDB 1.5.5 loaded for the engine mode);
* the ORIGINAL reproduction — the live engine, the live DuckDB, the generator
  form of ``_key_of`` — ran clean seven times out of seven, four on the
  committed engine and three on the SIM-531 engine that had crashed.

So the four crashes in four minutes came from a condition present then and
absent later. A sweep written the same day rewrote every hot-path generator
pulled by a C builtin, as a PRECAUTION, not a proven fix. The owner did not
land it (2026-10-07): it fixes nothing proven. On ``master`` only ``_key_of``
runs the list form (the SIM-531 build fix). Below, OLD names the generator
form and NEW the rewrite; this probe builds both forms itself. Run this probe
on the day the fault recurs: if the old shapes crash and the new ones pass,
the shape matters; if both crash, it does not.

THE SHAPES
----------
Short generators, one per result — the per-pitch / per-play / per-profile-pair
shapes the sweep would have rewritten:

    join-genexpr     ":".join(str(getattr(r, a)) for a in KEY_ATTRS)   the OLD _key_of
    join-listcomp    ":".join([str(getattr(r, a)) for a in KEY_ATTRS]) the NEW _key_of
    concat           str(r.player_id) + ":" + str(r.season)             the control
    sum-genexpr      sum(1 for edge in EDGES if sd > edge)              the OLD score_band
    sum-loop         an explicit loop                                    the NEW score_band
    tuple-genexpr    tuple(int(x) for x in (a, b, c) if x is not None)  the OLD runner_ids
    tuple-listcomp   tuple([...])                                        the NEW runner_ids
    any-genexpr      any(v is None for v in vals)                        the OLD fielding-draw check
    any-loop         an explicit loop                                    the NEW fielding-draw check

One long generator, pulled millions of times by one C call — the pool-column
shape (``np.fromiter((... for a, b in zip(...)), ...)``), which the sweep left
in place:

    long-sum         sum(<one generator over every result>)
    long-fromiter    np.fromiter(<one generator over every result>, ...)   skipped without numpy

HOW TO RUN
----------
Synthetic (no database; the six-field result object is rebuilt here)::

    python scripts/genexpr_crash_probe.py                        # every shape, one subprocess each
    python scripts/genexpr_crash_probe.py --shape join-genexpr   # one shape, in-process
    python scripts/genexpr_crash_probe.py --profiles 800         # a smaller workload

The original reproduction (the live steal engine over the live DuckDB; the
three join shapes only)::

    MSYS_NO_PATHCONV=1 docker compose run --rm -T --no-deps \\
        -v "$PWD/scripts:/app/scripts" app \\
        python scripts/genexpr_crash_probe.py --engine /data/baseball_sim.duckdb

The exit code is 0 when every shape ran to a verdict and no NEW shape crashed;
it is 1 when a NEW shape crashed (the rewrite's premise is broken) or a shape
ended without a verdict. An OLD shape that runs clean is reported, not failed.
"""

from __future__ import annotations

import argparse
import faulthandler
import itertools
import os
import subprocess
import sys
import time
from dataclasses import dataclass

faulthandler.enable()

# The steal-runner build that crashed: 2,585 profiles, each scored against the
# other 2,584 — 6.7 million query results. The standalone reproduction crashed
# after about 1.3 million, so the default keeps the full count.
DEFAULT_PROFILES = 2585
DEFAULT_RESULTS = 2584
PROGRESS_EVERY = 250
ENGINE_SEASONS = (2023, 2024, 2025, 2026)

KEY_ATTRS: tuple[str, ...] = ("player_id", "season")
SCORE_BAND_EDGES: tuple[int, ...] = (-3, -1, 0, 2)
SCORE_CLAMP = 5

SHORT_SHAPES = (
    "join-genexpr",
    "join-listcomp",
    "concat",
    "sum-genexpr",
    "sum-loop",
    "tuple-genexpr",
    "tuple-listcomp",
    "any-genexpr",
    "any-loop",
)
LONG_SHAPES = ("long-sum", "long-fromiter")
ALL_SHAPES = SHORT_SHAPES + LONG_SHAPES
#: The shapes the engine mode can drive (the original reproduction's three).
ENGINE_SHAPES = ("join-genexpr", "join-listcomp", "concat")

#: The rewritten forms (NEW). A crash here fails the run.
NEW_SHAPES = frozenset({"join-listcomp", "sum-loop", "tuple-listcomp", "any-loop", "concat"})
#: The generator forms (OLD). A crash here is the expected reproduction.
OLD_SHAPES = frozenset({"join-genexpr", "sum-genexpr", "tuple-genexpr", "any-genexpr"})


@dataclass(frozen=True, slots=True)
class SimilarityResult:
    """The steal engine's query result, field for field (plain ints and floats,
    as DuckDB hands them to the engine)."""

    player_id: int
    season: int
    score: float
    tendency_score: float
    success_score: float
    sample_steal_attempts: int


def make_results(i: int, n_results: int) -> list[SimilarityResult]:
    """Fresh result objects for profile ``i`` — the engine builds a new list per
    query, so the probe does too."""
    season = 2023 + (i % 4)
    return [
        SimilarityResult(
            player_id=600000 + j,
            season=season,
            score=(j % 97) / 97.0,
            tendency_score=(j % 89) / 89.0,
            success_score=(j % 83) / 83.0,
            sample_steal_attempts=j % 40,
        )
        for j in range(n_results)
    ]


# --- the shapes, each written exactly as the production site had it ----------


def key_of_genexpr(result, key_attrs: tuple[str, ...]) -> str:
    # engine_artifacts._key_of before the sweep: str.join pulls a generator.
    return ":".join(str(getattr(result, a)) for a in key_attrs)


def key_of_listcomp(result, key_attrs: tuple[str, ...]) -> str:
    # engine_artifacts._key_of after the sweep: the comprehension is inlined.
    return ":".join([str(getattr(result, a)) for a in key_attrs])


def key_of_concat(result) -> str:
    return str(result.player_id) + ":" + str(result.season)


def score_band_genexpr(score_diff: int) -> int:
    # filter_cells.score_band before the sweep.
    sd = max(-SCORE_CLAMP, min(SCORE_CLAMP, int(score_diff)))
    return sum(1 for edge in SCORE_BAND_EDGES if sd > edge)


def score_band_loop(score_diff: int) -> int:
    # filter_cells.score_band after the sweep.
    sd = max(-SCORE_CLAMP, min(SCORE_CLAMP, int(score_diff)))
    band = 0
    for edge in SCORE_BAND_EDGES:
        if sd > edge:
            band += 1
    return band


def runner_ids_genexpr(first, second, third) -> tuple[int, ...]:
    # game_state.Bases.runner_ids before the sweep.
    return tuple(int(r) for r in (first, second, third) if r is not None)


def runner_ids_listcomp(first, second, third) -> tuple[int, ...]:
    # game_state.Bases.runner_ids after the sweep.
    return tuple([int(r) for r in (first, second, third) if r is not None])


def any_none_genexpr(vals: list) -> bool:
    # full_pool_sampler._f_born_similarity before the sweep.
    return any(v is None for v in vals)


def any_none_loop(vals: list) -> bool:
    # full_pool_sampler._f_born_similarity after the sweep. The loop IS the
    # point here, so the "use any()" lint is silenced on purpose.
    for v in vals:  # noqa: SIM110
        if v is None:
            return True
    return False


def _bases_of(r: SimilarityResult):
    """Three optional runner ids derived from the result, so every shape reads
    the same objects."""
    pid = r.player_id
    return (pid if pid % 2 else None, pid + 1 if pid % 3 else None, None if pid % 5 else pid + 2)


def _apply_short(shape: str, res: list, n: int) -> tuple[int, int]:
    """Run ``shape`` over one query's results. Returns (results seen, a sink)."""
    sink = 0
    if shape == "join-genexpr":
        for r in res:
            sink += len(key_of_genexpr(r, KEY_ATTRS))
            n += 1
    elif shape == "join-listcomp":
        for r in res:
            sink += len(key_of_listcomp(r, KEY_ATTRS))
            n += 1
    elif shape == "concat":
        for r in res:
            sink += len(key_of_concat(r))
            n += 1
    elif shape == "sum-genexpr":
        for r in res:
            sink += score_band_genexpr(r.sample_steal_attempts - 20)
            n += 1
    elif shape == "sum-loop":
        for r in res:
            sink += score_band_loop(r.sample_steal_attempts - 20)
            n += 1
    elif shape == "tuple-genexpr":
        for r in res:
            sink += len(runner_ids_genexpr(*_bases_of(r)))
            n += 1
    elif shape == "tuple-listcomp":
        for r in res:
            sink += len(runner_ids_listcomp(*_bases_of(r)))
            n += 1
    elif shape == "any-genexpr":
        for r in res:
            sink += any_none_genexpr([r.score, r.tendency_score, None if r.season == 2026 else 1])
            n += 1
    elif shape == "any-loop":
        for r in res:
            sink += any_none_loop([r.score, r.tendency_score, None if r.season == 2026 else 1])
            n += 1
    else:
        raise SystemExit(f"unknown short shape {shape!r}")
    return n, sink


def run_short(shape: str, n_profiles: int, n_results: int) -> int:
    """One generator (or its replacement) per synthetic result. Returns the count."""
    t0 = time.time()
    n = 0
    sink = 0
    for i in range(n_profiles):
        n, s = _apply_short(shape, make_results(i, n_results), n)
        sink += s
        if i % PROGRESS_EVERY == 0:
            print(f"{shape}: profile {i} results {n} {time.time() - t0:.1f}s", flush=True)
    print(f"{shape}: done results {n} sink {sink} {time.time() - t0:.1f}s", flush=True)
    return n


def run_engine(shape: str, duckdb_path: str) -> int:
    """The original reproduction: the live steal engine's query results, every
    profile against every other, through ``shape``."""
    from similarity.engines.baserunner_steal_similarity import BaserunnerStealSimilarityEngine

    if shape not in ENGINE_SHAPES:
        raise SystemExit(f"the engine mode drives {ENGINE_SHAPES}, not {shape!r}")
    eng = BaserunnerStealSimilarityEngine(duckdb_path=duckdb_path)
    eng.build(seasons=list(ENGINE_SEASONS), include_below_minimum=True)
    keys = sorted(eng._profiles)
    print(f"{shape}: {len(keys)} live profiles, first {keys[0]}", flush=True)
    t0 = time.time()
    n = 0
    sink = 0
    for i, k in enumerate(keys):
        n, s = _apply_short(shape, eng.query(*k), n)
        sink += s
        if i % PROGRESS_EVERY == 0:
            print(f"{shape}: profile {i} results {n} {time.time() - t0:.1f}s", flush=True)
    print(f"{shape}: done results {n} sink {sink} {time.time() - t0:.1f}s", flush=True)
    return n


def run_long(shape: str, n_profiles: int, n_results: int) -> int:
    """ONE generator over every result, pulled by one C call — the pool-column
    shape (``np.fromiter((... for a, b in zip(...)), ...)``). The inner
    iterable is a C iterator (``chain.from_iterable(map(...))``), as ``zip`` is
    at the production sites."""
    t0 = time.time()

    def _progress(i: int) -> list[SimilarityResult]:
        if i % PROGRESS_EVERY == 0:
            print(
                f"{shape}: profile {i} results {i * n_results} {time.time() - t0:.1f}s", flush=True
            )
        return make_results(i, n_results)

    every = itertools.chain.from_iterable(map(_progress, range(n_profiles)))
    total = n_profiles * n_results
    attr = "score"  # read through getattr, as _key_of does
    if shape == "long-sum":
        acc = sum(float(getattr(r, attr)) for r in every)
    elif shape == "long-fromiter":
        try:
            import numpy as np
        except ImportError:
            print(f"{shape}: SKIPPED (numpy not installed)", flush=True)
            return -1
        arr = np.fromiter((float(getattr(r, attr)) for r in every), dtype=np.float64, count=total)
        acc = float(arr.sum())
    else:
        raise SystemExit(f"unknown long shape {shape!r}")
    print(f"{shape}: done results {total} sink {acc:.3f} {time.time() - t0:.1f}s", flush=True)
    return total


def run_shape(shape: str, n_profiles: int, n_results: int, engine: str | None) -> int:
    if engine:
        return run_engine(shape, engine)
    if shape in SHORT_SHAPES:
        return run_short(shape, n_profiles, n_results)
    return run_long(shape, n_profiles, n_results)


# --- the driver: one subprocess per shape --------------------------------------


def _last_count(text: str) -> str:
    """The furthest 'results N' the shape reported before it stopped."""
    last = "?"
    for line in text.splitlines():
        if " results " in line:
            last = line.split(" results ")[1].split()[0]
    return last


def drive(shapes: list[str], n_profiles: int, n_results: int, engine: str | None) -> int:
    where = (
        f"the live engine over {engine}"
        if engine
        else f"{n_profiles} profiles x {n_results} synthetic results = {n_profiles * n_results:,} per shape"
    )
    print(f"interpreter {sys.version.split()[0]} on {sys.platform}; {where}")
    rows = []
    failed = False
    for shape in shapes:
        cmd = [sys.executable, os.path.abspath(__file__), "--shape", shape]
        cmd += ["--profiles", str(n_profiles), "--results", str(n_results)]
        if engine:
            cmd += ["--engine", engine]
        t0 = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        out = proc.stdout + proc.stderr
        reached = _last_count(out)
        if "SKIPPED" in out:
            verdict = "skipped"
        elif proc.returncode == 0:
            verdict = "completed"
        elif proc.returncode in (139, -11) or "Segmentation fault" in out:
            verdict = "SEGFAULT"
        else:
            verdict = f"exit {proc.returncode}"
        kind = "old" if shape in OLD_SHAPES else "new" if shape in NEW_SHAPES else "long"
        rows.append((shape, kind, verdict, reached, time.time() - t0))
        print(
            f"  {shape:<16} {kind:<5} {verdict:<10} reached {reached:>10}  {time.time() - t0:6.1f}s",
            flush=True,
        )
        if verdict == "SEGFAULT" and kind == "new":
            failed = True
        if verdict.startswith("exit "):
            failed = True
            print(out[-2000:])
    old_crashed = [s for s, k, v, _, _ in rows if k == "old" and v == "SEGFAULT"]
    old_clean = [s for s, k, v, _, _ in rows if k == "old" and v == "completed"]
    print()
    if old_crashed:
        print(f"the old shape crashed on this run: {', '.join(old_crashed)}")
    if old_clean:
        print(
            f"the old shape ran clean on this run: {', '.join(old_clean)}: the fault did not show"
        )
    new_bad = [s for s, k, v, _, _ in rows if k == "new" and v == "SEGFAULT"]
    if new_bad:
        print(f"a NEW shape crashed, so the shape is not what decides it: {', '.join(new_bad)}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--shape", choices=ALL_SHAPES, help="run ONE shape in this process (the driver uses this)"
    )
    ap.add_argument(
        "--shapes", nargs="+", choices=ALL_SHAPES, help="the shapes to drive (default: all)"
    )
    ap.add_argument("--profiles", type=int, default=DEFAULT_PROFILES)
    ap.add_argument("--results", type=int, default=DEFAULT_RESULTS)
    ap.add_argument(
        "--engine",
        metavar="DUCKDB_PATH",
        help="the original reproduction: the live steal engine over this DuckDB (the join shapes only)",
    )
    args = ap.parse_args(argv)
    if args.shape:
        run_shape(args.shape, args.profiles, args.results, args.engine)
        return 0
    shapes = list(args.shapes or (ENGINE_SHAPES if args.engine else ALL_SHAPES))
    return drive(shapes, args.profiles, args.results, args.engine)


if __name__ == "__main__":
    sys.exit(main())
