"""
scripts/sim532_by_position.py — the fielder's batter-hand split and Savant's outs above
average, measured WITHIN one position (SIM-532, plan §2.2b).

What it measures and why. The build plan first scored the hand split (a fielder's
outs above average against left-handed batters minus his figure against right-handed
batters) on the outfield and the infield POOLED. The arm rebuild (SIM-550) found the
same morning that a repeat measured across pooled positions can be the position
label rather than the player: the position means differ by 20 to 40 outs, so a
pooled year-to-year correlation partly reads which position a man plays, and the
engine scores within a position. This probe measures every candidate within one
position, on players at that position in consecutive seasons with 100 or more of our
chances in both, two season pairs pooled per era: the shift era (2018->19 and
2021->22) and the shift-ban era (2023->24 and 2024->25, the pool window). Four
features: the hand gap as a count, the hand gap per 100 of our chances, Savant's outs
above average per 100 of our chances, and our own outs above average per 100. Then
the gap's mean size for regulars (250 or more chances).

The numbers it printed on 2026-09-17 (the plan's §2.2b table; the CSV pulls of the
board, seven per season, ``oaa_{year}_pos{3..9}.csv``):

    Year-to-year r within the position         1B     2B     3B     SS     LF     CF     RF
    hand gap (count), shift era (n 43-70)     0.04  -0.19   0.41   0.38  -0.04   0.14  -0.08
    hand gap (count), ban era (n 41-68)       0.21   0.15   0.06   0.36   0.12   0.18  -0.05
    hand gap per 100 chances, ban era         0.18   0.17  -0.13   0.38   0.12   0.06  -0.12
    Savant OAA per 100, ban era               0.34   0.63   0.51   0.44   0.38   0.54   0.53
    Savant OAA per 100, shift era             0.09   0.42   0.42   0.48   0.26   0.52   0.40
    our OAA per 100, ban era                  0.16   0.47   0.19   0.21   0.17   0.19   0.27
    mean |left - right| per 100, regulars     1.2    1.5    1.7    1.1    0.7    0.6    0.7

The read: the hand gap repeats only at shortstop (0.36 to 0.38 in both eras) and at
third base in the shift era alone; Savant's per-position figure beats our own at
every position but first base. So the split is loaded and read by nothing, and
Savant's figure joins both range groups (decisions 1 and 4).

Sources, after the loads (run-book step 3): Savant's per-position rows from Postgres
``raw.savant_outs_above_average`` (``--dsn`` or ``BASEBALL_DB_DSN``) and our chances
and outs above average from DuckDB ``derived.fielder_season_metrics`` (``--duckdb-path``).
The DuckDB read opens the file read-only, and that open FAILS while the app holds
the file read-write (the forkserver's writer lock, SIM-524): stop the app first
(``docker compose stop app``), or pass ``--csv-dir`` and leave the app up.
``--csv-dir DIR`` reads the design session's CSV layout instead:
``DIR/fielder_range.csv`` (player_id, position, season, opportunities,
outs_above_average) and ``DIR/sim532/oaa_{year}_pos{3..9}.csv`` (or ``DIR/oaa_...`` when
the subdirectory is absent), the board's own columns.

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim532_by_position.py
    python scripts/sim532_by_position.py --csv-dir path/to/scratch
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections.abc import Callable
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np  # noqa: E402

DEFAULT_DUCKDB = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
DEFAULT_DSN = os.environ.get("BASEBALL_DB_DSN", "")

#: The board's position number and the fielder table's label.
POS: dict[int, str] = {3: "1B", 4: "2B", 5: "3B", 6: "SS", 7: "LF", 8: "CF", 9: "RF"}
#: The first season of each pair, per era (the pair is season -> season + 1).
ERAS: dict[str, tuple[int, ...]] = {
    "SHIFT ERA (2018->19 + 2021->22)": (2018, 2021),
    "BAN ERA (2023->24 + 2024->25)": (2023, 2024),
}
SEASONS: tuple[int, ...] = (2018, 2019, 2021, 2022, 2023, 2024, 2025)
MIN_CHANCES = 100
REGULAR_CHANCES = 250
MIN_PAIRS = 6

#: ours: (player_id, position, season) -> (our chances, our outs above average)
Ours = dict[tuple[int, str, int], tuple[float | None, float | None]]
#: sav: (player_id, position, season) -> {oaa, rhh, lhh}
Savant = dict[tuple[int, str, int], dict[str, float | None]]


def _f(x: object) -> float | None:
    try:
        return float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The two sources: the databases after the loads, or the design session's CSVs
# ---------------------------------------------------------------------------


def _connect_read_only(duckdb_path: str):
    """A read-only DuckDB connection, or the run-book instruction when the app
    holds the file read-write (the forkserver's writer lock, SIM-524)."""
    import duckdb

    try:
        return duckdb.connect(duckdb_path, read_only=True)
    except duckdb.IOException as exc:
        raise SystemExit(
            f"cannot open {duckdb_path} read-only ({exc}). The app holds the DuckDB "
            "writer lock (SIM-524): stop it first — docker compose stop app — or pass "
            "--csv-dir to read the design session's CSV layout instead."
        ) from exc


def read_from_databases(dsn: str, duckdb_path: str) -> tuple[Ours, Savant]:
    import psycopg2

    con = _connect_read_only(duckdb_path)
    try:
        ours: Ours = {
            (int(pid), str(pos), int(season)): (_f(opp), _f(oaa))
            for pid, pos, season, opp, oaa in con.execute(
                "SELECT player_id, position, season, opportunities, outs_above_average "
                "FROM derived.fielder_season_metrics "
                f"WHERE season IN ({', '.join(str(s) for s in SEASONS)})"
            ).fetchall()
        }
    finally:
        con.close()
    pg = psycopg2.connect(dsn)
    try:
        cur = pg.cursor()
        cur.execute(
            "SELECT player_id, position, season, outs_above_average, oaa_vs_rhh, oaa_vs_lhh "
            "FROM raw.savant_outs_above_average WHERE season = ANY(%s)",
            (list(SEASONS),),
        )
        sav: Savant = {
            (int(pid), str(pos), int(season)): {"oaa": _f(oaa), "rhh": _f(rhh), "lhh": _f(lhh)}
            for pid, pos, season, oaa, rhh, lhh in cur.fetchall()
        }
    finally:
        pg.close()
    return ours, sav


def read_from_csv(csv_dir: Path) -> tuple[Ours, Savant]:
    ours: Ours = {}
    with (csv_dir / "fielder_range.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            ours[(int(r["player_id"]), r["position"], int(r["season"]))] = (
                _f(r["opportunities"]),
                _f(r["outs_above_average"]),
            )
    sav: Savant = {}
    for y in SEASONS:
        for p, name in POS.items():
            path = csv_dir / "sim532" / f"oaa_{y}_pos{p}.csv"
            if not path.exists():
                path = csv_dir / f"oaa_{y}_pos{p}.csv"
            with path.open(encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    sav[(int(r["player_id"]), name, y)] = {
                        "oaa": _f(r["outs_above_average"]),
                        "rhh": _f(r["outs_above_average_rhh"]),
                        "lhh": _f(r["outs_above_average_lhh"]),
                    }
    return ours, sav


# ---------------------------------------------------------------------------
# The analysis (unchanged from the design session's probe)
# ---------------------------------------------------------------------------

Key = tuple[int, str, int]
Getter = Callable[[dict[str, float | None], Key], float | None]


def make_features(ours: Ours) -> dict[str, Getter]:
    def chances(k: Key) -> float | None:
        o = ours.get(k)
        return o[0] if o else None

    def per100(v: float | None, k: Key) -> float | None:
        c = chances(k)
        return (v * 100.0 / c) if v is not None and c else None

    def gap(s: dict[str, float | None]) -> float | None:
        if s["lhh"] is None or s["rhh"] is None:
            return None
        return s["lhh"] - s["rhh"]

    def our_oaa(k: Key) -> float | None:
        o = ours.get(k)
        return o[1] if o else None

    return {
        "hand gap (LHH-RHH) count": lambda s, k: gap(s),
        "hand gap per 100 chances": lambda s, k: per100(gap(s), k),
        "Savant OAA per 100": lambda s, k: per100(s["oaa"], k),
        "our OAA per 100": lambda s, k: per100(our_oaa(k), k),
    }


def pairs(
    ours: Ours, sav: Savant, pos: str, mn: float, get: Getter, years: tuple[int, ...]
) -> tuple[int, float | None]:
    """The year-to-year correlation of ``get`` at ``pos`` over players present
    in ``year`` and ``year + 1`` with ``mn`` or more of our chances in both, for
    every ``year`` in ``years``; (n, r) — r None below ``MIN_PAIRS`` pairs."""

    def chances(k: Key) -> float | None:
        o = ours.get(k)
        return o[0] if o else None

    xs: list[float] = []
    ys: list[float] = []
    for (pid, p, y), s in sav.items():
        if p != pos or y not in years:
            continue
        k1, k2 = (pid, p, y), (pid, p, y + 1)
        if k2 not in sav:
            continue
        c1, c2 = chances(k1), chances(k2)
        if not c1 or not c2 or c1 < mn or c2 < mn:
            continue
        va, vb = get(s, k1), get(sav[k2], k2)
        if va is None or vb is None:
            continue
        xs.append(va)
        ys.append(vb)
    if len(xs) < MIN_PAIRS:
        return len(xs), None
    return len(xs), round(float(np.corrcoef(xs, ys)[0, 1]), 2)


def report(ours: Ours, sav: Savant) -> None:
    feats = make_features(ours)
    for era, years in ERAS.items():
        print(f"=== {era}: year-to-year r within each position, min {MIN_CHANCES} chances (n) ===")
        print(f"{'feature':26s} " + " ".join(f"{p:>12s}" for p in POS.values()))
        for label, get in feats.items():
            cells = []
            for p in POS.values():
                n, r = pairs(ours, sav, p, MIN_CHANCES, get, years)
                cells.append(f"{str(r):>6s} n{n:<4d}")
            print(f"{label:26s} " + " ".join(cells))
        print()

    def chances(k: Key) -> float | None:
        o = ours.get(k)
        return o[0] if o else None

    print(
        "=== the hand gap's size: mean |LHH-RHH| per 100 chances, regulars "
        f"(>= {REGULAR_CHANCES} chances), by position and era ==="
    )
    for era, years in ERAS.items():
        row = []
        for p in POS.values():
            g = [
                abs((s["lhh"] - s["rhh"]) * 100.0 / chances(k))  # type: ignore[operator]
                for k, s in sav.items()
                if k[1] == p
                and k[2] in years
                and s["lhh"] is not None
                and s["rhh"] is not None
                and chances(k)
                and chances(k) >= REGULAR_CHANCES  # type: ignore[operator]
            ]
            row.append(f"{np.mean(g):5.2f} n{len(g):<3d}" if g else f"{'-':>5s} n0  ")
        print(f"{era:34s} " + " ".join(f"{c:>12s}" for c in row))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres (raw.savant_outs_above_average)")
    ap.add_argument(
        "--duckdb-path", default=DEFAULT_DUCKDB, help="DuckDB (derived.fielder_season_metrics)"
    )
    ap.add_argument(
        "--csv-dir",
        default=None,
        help="read the design session's CSV layout from this directory instead of the databases",
    )
    args = ap.parse_args(argv)
    if args.csv_dir:
        ours, sav = read_from_csv(Path(args.csv_dir))
    else:
        if not args.dsn:
            print("no Postgres DSN (BASEBALL_DB_DSN or --dsn), and no --csv-dir", file=sys.stderr)
            return 2
        ours, sav = read_from_databases(args.dsn, args.duckdb_path)
    print(f"{len(ours)} fielder rows of ours; {len(sav)} Savant position-season rows\n")
    report(ours, sav)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
