"""
scripts/sim478_wall_zone_probe.py — the wall-play check (SIM-478/479/480, plan §5.4 and §7 C).

WHAT IT MEASURES
================
The fielding draw on the balls that reach the wall. The tap is the split probe's
(``scripts/sim523_split_probe.py``): the born batted ball against the drawn play.
Here it keeps only born AIR balls (a line drive or a fly ball) whose carry sits
within 30 ft of the LIVE park's fence line at the ball's direction, and it
splits them by the side of the fence: short (carry under the line) and over
(carry at or past it). The carry is the born ball's in the live park's air
(SIM-478 §11, the carry offset): the number the fence stage decides on, so
the probe's side is the stage's side.

For each side it reports the share of home runs, doubles, triples and outs
among the BORN rows' own outcomes (what those balls did in the pool) and among
the DRAWN plays. The grade separates the fence decision from the wall play:

* the home-run share is graded against what the stage guarantees — 1.0 on the
  over side (the stage keeps only home-run rows) and 0.0 on the short side (it
  removes them). The born rows carry the pool's own reading at that distance
  (about 0.9 over, 0.06 short), so a grade against the born share fails by
  construction; the target is a constant, with no standard error, and the
  0.01 slack absorbs the rare ball the stage passes (no matching rows).
* the double, triple and out shares are graded among the plays that were NOT
  a home run (born and drawn each on their own denominator), within two
  standard errors of the born denominator plus 0.01. The over side has no
  drawn non-home-run play by construction, so those shares are graded on the
  short side only; on the over side they are informational.

The flags are the container's — the production set. The probe never flips one.

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim478_wall_zone_probe.py --iters 30            # the balanced set, ~15 min
    ... --iters 3 --games 776151 776142 --json-out /app/sim478_scratch/wall.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
for p in (str(_ROOT), str(_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from sim_stats import (  # noqa: E402
    _FACTORY,
    _resolve,
    open_sim_duckdb,
    sim_kwargs_from_state,
)

from simulation.batch_runner import GameSpec  # noqa: E402
from simulation.production_factory import production_machine_factory  # noqa: E402
from simulation.sim_loop import BoxScore, simulate_game  # noqa: E402

#: The balanced certifying set (the lane's game order).
_GAME_SET = _ROOT / "scripts" / "sim523_game_set.json"

#: The outcomes the check grades, in report order.
OUTCOMES: tuple[str, ...] = ("home_run", "double", "triple", "out")
_LABEL = {"home_run": "home run", "double": "double", "triple": "triple", "out": "out"}
SIDES: tuple[str, ...] = ("short", "over")
#: The wall zone of this check: a born ball within this many feet of the line.
BAND_FT = 30.0
#: The verdict's slack beyond two standard errors.
SLACK = 0.01
#: The air classes of the batted-ball pool (2 = line drive, 3 = fly ball).
_AIR_CLASSES = (2, 3)


def classify(result_hits: int, result_outs: int) -> str:
    """One label for a play from its hit code and its outs: home run, triple,
    double, single, out (an out with no hit) or other."""
    rh = int(result_hits)
    if rh == 4:
        return "home_run"
    if rh == 3:
        return "triple"
    if rh == 2:
        return "double"
    if rh == 1:
        return "single"
    if int(result_outs) > 0:
        return "out"
    return "other"


def _share_se(p: float, n: int) -> float:
    return math.sqrt(max(p * (1.0 - p), 0.0) / n) if n > 0 else 0.0


#: What the fence stage guarantees for the drawn home-run share, per side.
HR_TARGET: dict[str, float] = {"over": 1.0, "short": 0.0}


def wall_zone_shares(tallies: dict[str, Any]) -> dict[str, Any]:
    """The shares, their standard errors and the checks, per side.

    ``tallies[side]`` is a counter with ``n``, ``born_<outcome>`` and
    ``drawn_<outcome>``. Two rules:

    * ``home_run``: the drawn share against the stage's own guarantee
      (``HR_TARGET``: 1.0 over, 0.0 short), a constant with no standard
      error; PASS within ``SLACK``. The born share is reported beside it.
    * ``double`` / ``triple`` / ``out``: the shares AMONG THE PLAYS THAT WERE
      NOT A HOME RUN, born over the born non-home-run plays and drawn over the
      drawn ones; the standard error is the born share's over the born
      denominator, ``sqrt(p (1 - p) / born_n)``; PASS within ``2 SE + SLACK``.
      A share is ``graded`` only when both denominators hold a play — the
      over side's drawn denominator is 0 by construction, so there the three
      shares are informational and only the home-run rule counts.

    A side with no balls fails (it measured nothing).
    """
    out: dict[str, Any] = {}
    for side in SIDES:
        t = tallies.get(side) or {}
        n = int(t.get("n", 0))
        born_hr_n = int(t.get("born_home_run", 0))
        drawn_hr_n = int(t.get("drawn_home_run", 0))
        born_not_hr = n - born_hr_n
        drawn_not_hr = n - drawn_hr_n
        target = HR_TARGET[side]
        drawn_hr = drawn_hr_n / n if n else 0.0
        shares: dict[str, Any] = {
            "home_run": {
                "born": born_hr_n / n if n else 0.0,
                "drawn": drawn_hr,
                "target": target,
                "se": 0.0,
                "delta": drawn_hr - target,
                "tolerance": SLACK,
                "graded": bool(n > 0),
                "pass": bool(n > 0 and abs(drawn_hr - target) <= SLACK),
            }
        }
        for oc in OUTCOMES:
            if oc == "home_run":
                continue
            born = int(t.get(f"born_{oc}", 0)) / born_not_hr if born_not_hr else 0.0
            drawn = int(t.get(f"drawn_{oc}", 0)) / drawn_not_hr if drawn_not_hr else 0.0
            se = _share_se(born, born_not_hr)
            graded = born_not_hr > 0 and drawn_not_hr > 0
            shares[oc] = {
                "born": born,
                "drawn": drawn,
                "se": se,
                "delta": drawn - born,
                "tolerance": 2.0 * se + SLACK,
                "graded": bool(graded),
                "pass": bool(graded and abs(drawn - born) <= 2.0 * se + SLACK),
            }
        out[side] = {
            "n": n,
            "shares": shares,
            "not_home_run": {"born_n": born_not_hr, "drawn_n": drawn_not_hr},
            "pass": bool(n > 0 and all(s["pass"] for s in shares.values() if s["graded"])),
        }
    out["all_pass"] = all(out[side]["pass"] for side in SIDES)
    return out


def verdict_lines(shares: dict[str, Any]) -> list[str]:
    """One verdict line per side and outcome, in the report's words."""
    lines: list[str] = []
    for side in SIDES:
        s = shares[side]
        if s["n"] == 0:
            lines.append(f"FAIL C {side}: no born air balls within {BAND_FT:.0f} ft of the fence")
            continue
        hr = s["shares"]["home_run"]
        lines.append(
            f"{'PASS' if hr['pass'] else 'FAIL'} C {side}: home run share drawn "
            f"{hr['drawn']:.3f} vs the stage's {hr['target']:.1f} (born {hr['born']:.3f}, "
            f"n {s['n']})"
        )
        nh = s["not_home_run"]
        for oc in OUTCOMES:
            if oc == "home_run":
                continue
            r = s["shares"][oc]
            if not r["graded"]:
                lines.append(
                    f"INFO C {side}: {_LABEL[oc]} share among the non-home-run plays not "
                    f"graded (drawn n {nh['drawn_n']}, born n {nh['born_n']})"
                )
                continue
            lines.append(
                f"{'PASS' if r['pass'] else 'FAIL'} C {side}: {_LABEL[oc]} share among the "
                f"non-home-run plays drawn {r['drawn']:.3f} vs born {r['born']:.3f} "
                f"(SE {r['se']:.3f}, drawn n {nh['drawn_n']}, born n {nh['born_n']})"
            )
    return lines


def _fence_at(fp: Any, venue_id: int | None, spray: float | None, season: int | None):
    """The live fence for the season. The sampler is gaining the ``season``
    keyword; an older sampler without it takes the two-argument call."""
    try:
        return fp.fence_at(venue_id, spray, season=season)
    except TypeError:
        # The sampler's fence_at has no season keyword yet: the season-blind line.
        return fp.fence_at(venue_id, spray)


def _carry_of(fp: Any, born: dict, venue_id: int | None):
    """The born ball's carry in the LIVE park's air (SIM-478 §11): the number
    the fence stage decides on. The tap reads the raw born ball, so it passes
    the live venue itself. An older sampler without the venue argument takes
    the one-argument call (no shift)."""
    try:
        return fp.carry_of(born, venue_id)
    except TypeError:
        return fp.carry_of(born)


class _Tap:
    """Keep the born air balls within the band and tally born vs drawn."""

    def __init__(self, fp: Any, band_ft: float = BAND_FT):
        self.fp = fp
        self.band_ft = float(band_ft)
        self.tallies: dict[str, Counter] = {side: Counter() for side in SIDES}
        self.counts: Counter = Counter()
        self.venue_id: int | None = None
        self.season: int | None = None
        self._bnpa = fp.battedball_new_pa
        self._bdraw = fp.battedball_draw
        self._kept: tuple[str, int, str] | None = None

    def install(self) -> None:
        tap = self

        def bnpa(hand: str, batter_key: str, state: Any, **kw: Any) -> Any:
            tap._kept = None
            born = kw.get("born_bb")
            if born and born.get("row") is not None:
                tap.counts["born"] += 1
                cls = born.get("cls")
                if cls is not None and int(cls) in _AIR_CLASSES:
                    tap.counts["born_air"] += 1
                    venue = kw.get("venue_id")
                    if venue is None:
                        venue = tap.venue_id
                    season = kw.get("live_season")
                    if season is None:
                        season = tap.season
                    fence = _fence_at(tap.fp, venue, born.get("spray_raw"), season)
                    carry = _carry_of(tap.fp, born, venue)
                    if fence is None or carry is None:
                        tap.counts["no_fence_or_carry"] += 1
                    elif abs(float(carry) - float(fence)) <= tap.band_ft:
                        side = "short" if float(carry) < float(fence) else "over"
                        tap._kept = (hand, int(born["row"]), side)
            return tap._bnpa(hand, batter_key, state, **kw)

        def bdraw() -> Any:
            out = tap._bdraw()
            if tap._kept is not None:
                hand, row, side = tap._kept
                pool = tap.fp.a.bb_pools[hand]
                born_oc = classify(int(pool.result_hits[row]), int(pool.result_outs[row]))
                drawn_oc = classify(int(out[1]), int(out[2]))
                t = tap.tallies[side]
                t["n"] += 1
                t[f"born_{born_oc}"] += 1
                t[f"drawn_{drawn_oc}"] += 1
                tap._kept = None
            return out

        self.fp.battedball_new_pa = bnpa
        self.fp.battedball_draw = bdraw

    def remove(self) -> None:
        self.fp.battedball_new_pa = self._bnpa
        self.fp.battedball_draw = self._bdraw


def _balanced_set() -> list[int]:
    doc = json.loads(_GAME_SET.read_text())
    return [int(g["game_pk"]) for g in doc["order"]]


def _venue_of(state: Any) -> int | None:
    park = getattr(state, "park", None)
    text = str(park).strip() if park is not None else ""
    return int(text) if text.isdigit() else None


def _print_table(shares: dict[str, Any]) -> None:
    print("\n  the fence decision: the drawn home-run share vs the stage's target (born):")
    for side in SIDES:
        hr = shares[side]["shares"]["home_run"]
        print(
            f"  {side:>6} n {shares[side]['n']:>6}: drawn {hr['drawn']:.3f} vs "
            f"{hr['target']:.1f} (born {hr['born']:.3f})"
        )
    print("\n  the wall play, among the plays that were not a home run (drawn / born (SE)):")
    for side in SIDES:
        s = shares[side]
        nh = s["not_home_run"]
        cells = " ".join(
            f"{_LABEL[oc]} {s['shares'][oc]['drawn']:.3f} / {s['shares'][oc]['born']:.3f} "
            f"({s['shares'][oc]['se']:.3f})"
            for oc in OUTCOMES
            if oc != "home_run"
        )
        print(f"  {side:>6} drawn n {nh['drawn_n']}, born n {nh['born_n']}: {cells}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--games", type=int, nargs="*", default=None)
    ap.add_argument("--iters", type=int, default=30)
    ap.add_argument("--band-ft", type=float, default=BAND_FT)
    ap.add_argument("--json-out", type=str, default=None)
    args = ap.parse_args()
    game_pks = list(args.games) if args.games else _balanced_set()

    duck = open_sim_duckdb()
    try:
        states = [asyncio.run(_resolve(gp, duck)) for gp in game_pks]
    finally:
        if duck is not None:
            duck.close()

    tallies: dict[str, Counter] = {side: Counter() for side in SIDES}
    counts: Counter = Counter()
    fp = None
    t0 = time.perf_counter()
    for game_pk, state in zip(game_pks, states, strict=True):
        kw = sim_kwargs_from_state(state)
        machine = production_machine_factory(
            0, GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
        )
        fp = machine.full_pool_sampler
        if counts["games"] == 0:
            print(
                f"  flags (the container's): fence stage {bool(getattr(fp, 'fence_stage', False))}, "
                f"margin {getattr(fp, 'fence_margin', None)}, class filter "
                f"{bool(getattr(fp, 'bb_class_filter', False))}, born sigma "
                f"{getattr(fp, 'bb_born_sigma', None)}"
            )
            if hasattr(fp, "fence_counts"):
                fp.fence_counts[:] = 0
        tap = _Tap(fp, band_ft=args.band_ft)
        tap.venue_id = _venue_of(state)
        tap.season = int(getattr(state, "season", 0) or 0) or None
        tap.install()
        try:
            machine._fp_pitcher_key = None
            machine._fp_pa_key = None
            for seed in range(args.iters):
                machine.boxscore = BoxScore()
                simulate_game(state_machine=machine, seed=seed, **kw)
        finally:
            tap.remove()
        for side in SIDES:
            tallies[side].update(tap.tallies[side])
        counts.update(tap.counts)
        counts["games"] += 1
        print(
            f"  game {game_pk} (venue {tap.venue_id}, season {tap.season}): "
            f"born air {tap.counts['born_air']}, in the band short "
            f"{tap.tallies['short']['n']} / over {tap.tallies['over']['n']}",
            flush=True,
        )
    elapsed = time.perf_counter() - t0

    shares = wall_zone_shares(tallies)
    print(
        f"\n=== the wall play (SIM-478 C): {len(states)} games x {args.iters} iterations, "
        f"born air balls within {args.band_ft:.0f} ft of the live fence ==="
    )
    print(
        f"  born balls {counts['born']}, air {counts['born_air']}, without a fence or a carry "
        f"{counts['no_fence_or_carry']}, in the band {sum(t['n'] for t in tallies.values())}; "
        f"{elapsed:.0f} s"
    )
    fence_counts = (
        [int(x) for x in fp.fence_counts] if fp is not None and hasattr(fp, "fence_counts") else []
    )
    if fence_counts:
        print(f"  fence counters over the run: {fence_counts}")
    _print_table(shares)
    print()
    for line in verdict_lines(shares):
        print("  " + line)
    print(f"\n  C the wall play: {'PASS' if shares['all_pass'] else 'FAIL'}")

    if args.json_out:
        doc = {
            "games": game_pks,
            "iters": args.iters,
            "band_ft": args.band_ft,
            "counts": dict(counts),
            "tallies": {side: dict(t) for side, t in tallies.items()},
            "shares": shares,
            "fence_counts": fence_counts,
            "elapsed_s": elapsed,
        }
        Path(args.json_out).write_text(json.dumps(doc, indent=2))
        print(f"  wrote {args.json_out}")
    return 0 if shares["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
