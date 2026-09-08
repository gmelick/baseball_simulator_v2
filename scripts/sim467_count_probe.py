"""
scripts/sim467_count_probe.py — SIM-467: the per-count pitch-outcome mix, whole pool vs cell path.

The 12×500 lane read PITCHES_PA +5.5% and BB_PA −4.6% with the cell index on.
Both are functions of the per-count outcome mix the pitch draw produces, so
this probe records EVERY pitch draw (count → outcome) for N games per arm in
one warm process and prints, per count, the outcome shares of both arms and
the shift. It also reports the sim's PA cell-visit shares (runners × outs ×
band) so a "the sim visits longer cells" story can be told from the same run.

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim467_count_probe.py --iters 20 744795 661032
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter, defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for p in (str(_ROOT), str(_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from sim_stats import _FACTORY, _resolve, open_sim_duckdb, sim_kwargs_from_state  # noqa: E402

from simulation.batch_runner import GameSpec  # noqa: E402
from simulation.filter_cells import score_band  # noqa: E402
from simulation.production_factory import production_machine_factory  # noqa: E402
from simulation.sim_loop import BoxScore, simulate_game  # noqa: E402

_OUTCOMES = ("ball", "called_strike", "swinging_strike", "foul", "in_play", "hit_by_pitch")


class _Tap:
    """Wrap the sampler's draw / new_plate_appearance to record counts and cells."""

    def __init__(self, fp) -> None:
        self.fp = fp
        self.by_count: dict[tuple[int, int], Counter] = defaultdict(Counter)
        self.cells: Counter = Counter()
        self.pa = 0
        self.empty_fallbacks = 0
        self._draw = fp.draw
        self._npa = fp.new_plate_appearance

    def install(self) -> None:
        tap = self

        def draw(balls: int = 0, strikes: int = 0) -> str:
            out = tap._draw(balls, strikes)
            tap.by_count[(int(balls), int(strikes))][out] += 1
            if tap.fp._pp_last_i is None:
                tap.empty_fallbacks += 1
            return out

        def npa(batter_key, base_out, **kw):
            bo = [int(x) for x in base_out]
            tap.cells[(bo[1] & 7, bo[0], score_band(bo[3]))] += 1
            tap.pa += 1
            return tap._npa(batter_key, base_out, **kw)

        self.fp.draw = draw
        self.fp.new_plate_appearance = npa

    def remove(self) -> None:
        self.fp.draw = self._draw
        self.fp.new_plate_appearance = self._npa


def _run(machine, kw, seeds) -> None:
    machine._fp_pitcher_key = None
    machine._fp_pa_key = None
    for seed in seeds:
        machine.boxscore = BoxScore()
        simulate_game(state_machine=machine, seed=seed, **kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("game_pks", type=int, nargs="+")
    ap.add_argument("--iters", type=int, default=20)
    ap.add_argument("--min-cell", type=int, default=20)
    args = ap.parse_args()

    duck = open_sim_duckdb()
    try:
        states = [asyncio.run(_resolve(gp, duck)) for gp in args.game_pks]
    finally:
        if duck is not None:
            duck.close()

    taps: dict[str, list[_Tap]] = {"off": [], "on": []}
    for state in states:
        kw = sim_kwargs_from_state(state)
        machine = production_machine_factory(
            0, GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
        )
        fp = machine.full_pool_sampler
        for arm in ("off", "on"):
            fp.pitch_cell_index = arm == "on"
            fp.pitch_min_cell = args.min_cell
            tap = _Tap(fp)
            tap.install()
            _run(machine, kw, list(range(args.iters)))
            tap.remove()
            taps[arm].append(tap)

    def merged(arm: str):
        bc: dict[tuple[int, int], Counter] = defaultdict(Counter)
        cells: Counter = Counter()
        pa = 0
        fb = 0
        for t in taps[arm]:
            for k, c in t.by_count.items():
                bc[k].update(c)
            cells.update(t.cells)
            pa += t.pa
            fb += t.empty_fallbacks
        return bc, cells, pa, fb

    off_bc, off_cells, off_pa, off_fb = merged("off")
    on_bc, on_cells, on_pa, on_fb = merged("on")
    off_pitches = sum(sum(c.values()) for c in off_bc.values())
    on_pitches = sum(sum(c.values()) for c in on_bc.values())
    print(
        f"games/arm={len(states) * args.iters}  PAs off={off_pa} on={on_pa}  "
        f"pitches/PA off={off_pitches / max(1, off_pa):.3f} on={on_pitches / max(1, on_pa):.3f}  "
        f"empty-bucket fallbacks off={off_fb} on={on_fb}"
    )
    if fp.pitch_cell_index:
        print(f"widening draws by level (on): {fp.cell_index_stats()['draws_by_level']}")
    print("\nper-count outcome shares — off | on | shift (pp); n = draws at that count")
    hdr = "count   n_off   n_on  " + "  ".join(f"{o[:6]:>17}" for o in _OUTCOMES)
    print(hdr)
    for b in range(4):
        for s in range(3):
            co, cn = off_bc.get((b, s), Counter()), on_bc.get((b, s), Counter())
            no, nn = sum(co.values()), sum(cn.values())
            cols = []
            for o in _OUTCOMES:
                po = co[o] / no if no else 0.0
                pn = cn[o] / nn if nn else 0.0
                cols.append(f"{po:6.3f}|{pn:6.3f}|{(pn - po) * 100:+4.1f}")
            print(f"{b}-{s}   {no:6d} {nn:6d}  " + "  ".join(cols))
    print("\nPA cell-visit shares (runners, outs, band): off | on — the 12 most visited")
    tot_o, tot_n = sum(off_cells.values()), sum(on_cells.values())
    for key, n in off_cells.most_common(12):
        print(f"  {key}: {n / tot_o:6.3f} | {on_cells[key] / max(1, tot_n):6.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
