"""
scripts/sim478_fence_check.py — the certification of the outfield fence (SIM-478/479/480):
the fence stage's decision against every real air ball of the pool window, with the
thresholds of the plan's §7 (docs/audit/2026-09-20-sim478-480-fence-certification-plan.md).

What it measures. The fence stage removes the home-run rows from the fielding draw when
the born ball's carry falls short of the live park's fence line, and keeps only them when
the carry clears it. The line is one number per park, season group and ten-degree spray
sector (``park_geometry.json`` in the engine-artifact bundle). This script asks "is the
line right?" by replaying the production rule on the pool's own fly balls and line drives:
the document's grid with the clamped sector, the group of the ball's own season, the
ball's own reported distance as the carry. A ball with no distance counts as PASSED (the
stage would fall to the carry model there; 0.1% of the balls, so the model is not needed
for the read). Read-only: it opens the DuckDB file read-only, which works while the app
runs, and it never writes the bundle.

The checks (the plan's §7; the thresholds sit in ``THRESHOLDS``):

* A1  the whole window, every decided ball: P(home run | over) and the share of home
      runs called short ("missed"). The breakdowns — the errors by distance from the
      line (|carry - fence| bands), by sector, by venue, and the share of home runs by
      their margin over the fence — read the balls inside the grid, the balls a line
      describes; a clamped ball tests the grid and is A4's read.
* A2  every venue with 200 or more decided balls inside the grid: P(home run | over)
      and missed.
* A3  held-out seasons: the lines rebuilt from the OTHER seasons with the builder's own
      rule (``build_geometry_document``, no prior, no overrides), tested on the held-out
      season; each within three points of the whole-window reading. On an old document
      whose grid differs from the builder's, the rebuild runs the same line rule on the
      document's grid (one group per venue), so the read describes the document checked.
      The test balls are the ones the grid covers; a clamped ball is A4's question.
* A4  the down-the-line balls (|spray| >= 45 degrees): on the old nine-sector document
      they are the clamped balls the plan's defect 1 describes; on the eleven-sector
      document they have their own sectors. Their missed share.
* A5  moved walls: the change detector (``detect_moves``, the builder's own) on every
      venue; for every move, the checked document's missed share on the late seasons'
      balls at the moved sectors against the missed share of a line built on the late
      seasons alone.
* A6  thin parks: a venue with balls in the window's last season whose group for that
      season still rests on the league line at any sector; the game count from the pool.
* A7  the transfer (the plan's §11): every pool air ball "born" into each park with 200
      or more decided balls, against that park's fence. The fair target is the pool's
      ball mix scored at the park's own home-run rate per cell (4 mph of exit velocity
      × 4 degrees of launch angle × 10 degrees of |spray|; a cell with fewer than five
      of the park's balls takes the league's cell rate). The share called over with (a)
      the ball's own distance and (c) the ball's own distance plus the park's carry
      offset minus the ball's own park's (the document's ``carry_offset_ft``; 0 when a
      park has none). PASS when every park's (c) sits within 1.5 points of its target
      (the plan's 1.0 point was set from the clean-fly fit's numbers; the home-run fit
      decision 6 chose reads Yankee −1.4 and Fenway +1.4 on the live pool, 2026-09-22).
      A document without offsets prints (a) alone and is "not graded" — that does not
      fail the run.
* B2  ``--lane``: the per-park read of a lane JSON (``tests/acceptance/conftest.py``
      writes it): per venue the sim's home runs per ball in play against the games'
      actor-matched expectation (``scripts/sim523_game_set.py``), the residual against
      the pool's own park home-run factor. Only a park with ``--min-games`` (default 3)
      games in the record enters the read. The standard error per park combines the
      sim's binomial error with the expectation's own sampling error
      (``expectation_se``: the nine batters' and the starters' own sample sizes). The
      verdict: no entering park beyond 3 combined standard errors; r over the entering
      parks is reported, not graded. ``bb_margin_counts`` in the record (the wall-margin
      band's counters) is read beside the fence counters, informational.

This script's own read of the LIVE document of 2026-09-08 (nine sectors, no season
groups; the plan's §2 "today" column), run 2026-09-21: 242,957 air balls, 21,892 home
runs, 242,652 decided; P(home run | over) 0.918, missed 9.0%, over-but-kept 8.2%; the 0-5
ft band 0.635 over / 0.297 short; held-out seasons 0.899 / 7.3% (2023) to 0.920 / 9.2%
(2026); the down-the-line balls (A4, |spray| >= 45) 12,726 with 24.5% of their home runs
missed (12,722 of them clamped); Camden Yards' left field 18.4% missed on its 2025-26
balls against 7.2% on a late line (sectors 1-3 of the detector's eleven-sector grid,
n 1,140, 125 home runs); Las Vegas Ballpark 7 of 9 sectors on the league line. Verdicts:
A1-A3 PASS, A4-A6 FAIL. The plan's §2 quotes the design probe for Camden (20.1% against
7.5%, n 1,267, 134 home runs): the probe took sectors 0-2 of the nine-sector grid WITH
the clamped balls beyond -45 degrees; ``moved_walls`` keeps only the balls inside the
grid, so the two selections differ and this script does not reproduce the probe's pair.

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim478_fence_check.py --json-out /tmp/check.json
    ... --geometry /path/to/trial/park_geometry.json         # a rebuilt document
    ... --lane /path/to/lane.json                            # the per-park read (B2)

Exit code 0 when every verdict passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402

from pipeline.batch.engine_artifacts import (  # noqa: E402
    PARK_N_SECTORS,
    PARK_SECTOR_DEG,
    PARK_SPRAY_MAX,
    PARK_SPRAY_MIN,
    build_geometry_document,
    detect_moves,
    group_for_season,
    sector_line,
    sector_of,
)

DEFAULT_GEOMETRY = "/data/play_pool/engine_artifacts/park_geometry.json"
DEFAULT_DUCKDB = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
#: The |carry - fence| bands of the error table, feet.
BANDS: tuple[tuple[float, float], ...] = ((0, 5), (5, 10), (10, 20), (20, 40), (40, math.inf))
#: A home run's margin over the fence, feet: the three shares the plan reports.
MARGIN_BANDS: tuple[tuple[str, float, float], ...] = (
    ("<10", -math.inf, 10),
    ("10-30", 10, 30),
    ("30+", 30, math.inf),
)
#: A ball is "down the line" from this |spray| on.
DOWN_THE_LINE_DEG = 45.0
#: A venue joins the per-venue table from this many decided balls.
VENUE_MIN_DECIDED = 200
#: A sector is "thin" under this many home runs (informational).
THIN_HR = 20
#: A7: the transfer cell — exit velocity (mph), launch angle (degrees), |spray| (degrees).
TRANSFER_CELL: tuple[float, float, float] = (4.0, 4.0, 10.0)
#: A7: a cell with fewer of the park's balls takes the league's cell rate.
TRANSFER_MIN_CELL = 5
#: B2: a park enters the read from this many games in the lane record.
B2_MIN_GAMES = 3
#: B2: the starters' share of a game's plate appearances (the game-set
#: builder's ``_STARTER_SHARE``) and the sides' weight in the expectation.
B2_STARTER_SHARE = 0.6
B2_SIDE_WEIGHT = 0.5
VENUE_NAMES: dict[int, str] = {
    1: "Angel",
    2: "Camden",
    3: "Fenway",
    4: "Rate",
    5: "Progressive",
    7: "Kauffman",
    10: "Coliseum",
    12: "Tropicana",
    14: "Rogers",
    15: "Chase",
    17: "Wrigley",
    19: "Coors",
    22: "Dodger",
    31: "PNC",
    32: "AmFam",
    680: "T-Mobile",
    2392: "Daikin",
    2394: "Comerica",
    2395: "Oracle",
    2523: "Steinbrenner",
    2529: "Sutter",
    2602: "GreatAmerican",
    2680: "Petco",
    2681: "CitizensBank",
    2735: "Williamsport",
    2889: "Busch",
    3289: "Citi",
    3309: "Nationals",
    3312: "Target",
    3313: "Yankee",
    4169: "loanDepot",
    4705: "Truist",
    5325: "GlobeLife",
    5340: "MexicoCity",
    5355: "LasVegas",
}


@dataclass(frozen=True)
class Thresholds:
    """The plan's §7 pass lines. A share is a fraction (0.10 = ten percent)."""

    a1_p_hr_over: float = 0.90
    a1_missed: float = 0.10
    a2_p_hr_over: float = 0.80
    a2_missed: float = 0.15
    a3_delta: float = 0.03
    a4_missed: float = 0.12
    a5_excess: float = 0.05
    #: A7: every park's (c) within this of its fair target (a share). The
    #: plan's §11.3 line was 1.0 point, set from the clean-fly fit's (c)
    #: column. The home-run fit (decision 6, the fit that ships) reads
    #: Yankee −1.4 / Fenway +1.4 / Kauffman +1.1 / Coliseum +1.1 on the
    #: live pool, so the line is 1.5 points (the review of 2026-09-22).
    a7_gap: float = 0.015
    #: B2: r is reported, not graded (the redesign of the plan's §11.4);
    #: the value stays as the plan's §7 reference.
    b2_r: float = 0.5
    b2_se: float = 3.0


THRESHOLDS = Thresholds()


@dataclass(frozen=True)
class Grid:
    """The sector grid a document describes."""

    spray_min: float
    spray_max: float
    sector_deg: float
    n_sectors: int

    def lo(self, sec: int) -> float:
        return self.spray_min + sec * self.sector_deg

    def hi(self, sec: int) -> float:
        return self.spray_min + (sec + 1) * self.sector_deg


def document_grid(doc: dict) -> Grid:
    return Grid(
        spray_min=float(doc.get("spray_min", -45.0)),
        spray_max=float(doc.get("spray_max", 45.0)),
        sector_deg=float(doc.get("sector_deg", 10)),
        n_sectors=int(doc.get("n_sectors", 9)),
    )


BUILDER_GRID = Grid(PARK_SPRAY_MIN, PARK_SPRAY_MAX, float(PARK_SECTOR_DEG), PARK_N_SECTORS)


def _name(venue: int) -> str:
    return VENUE_NAMES.get(int(venue), str(venue))


def _share(num: float, den: float) -> float:
    return float(num) / float(den) if den else float("nan")


def _venue_entry(block: dict | None, venue: int, season: int | None) -> list | None:
    """One venue's per-sector list from a document block (``venues``,
    ``source``, ``support_hr``): the season's group on a version-2 document,
    the plain list on an old one, ``None`` when the venue is absent."""
    if not block:
        return None
    entry = block.get(str(int(venue)))
    if entry is None:
        return None
    if isinstance(entry, dict):
        key = group_for_season(entry, season)
        return None if key is None else entry[key]
    return entry


def line_for(doc: dict, venue: int, season: int | None) -> list | None:
    """The venue's fence lines for the season (its group), or ``None``."""
    return _venue_entry(doc.get("venues"), venue, season)


def subset(balls: dict[str, np.ndarray], mask: np.ndarray) -> dict[str, np.ndarray]:
    return {k: np.asarray(v)[mask] for k, v in balls.items()}


@dataclass
class Decision:
    """The production rule on a set of balls: the clamped sector, the fence
    line per ball (NaN without one), and the over / short / decided masks."""

    sec: np.ndarray
    fence: np.ndarray
    decided: np.ndarray
    over: np.ndarray
    short: np.ndarray
    margin: np.ndarray


def decide(balls: dict[str, np.ndarray], doc: dict) -> Decision:
    """The fence stage's decision on every ball under the document: the
    document's grid, the clamped sector, the group of the ball's season, the
    ball's own distance as the carry. Undecided = no spray, no distance or no
    line (the stage passes)."""
    grid = document_grid(doc)
    venue = np.asarray(balls["venue"], dtype=np.int64)
    season = np.asarray(balls["season"], dtype=np.int64)
    spray = np.asarray(balls["spray"], dtype=np.float64)
    dist = np.asarray(balls["dist"], dtype=np.float64)
    n = len(venue)
    has_spray = np.isfinite(spray)
    sec = sector_of(
        np.where(has_spray, spray, grid.spray_min), grid.spray_min, grid.sector_deg, grid.n_sectors
    )
    league_raw = list(doc.get("league") or [])
    league = np.full(grid.n_sectors, np.nan)
    for s, x in enumerate(league_raw[: grid.n_sectors]):
        if x is not None:
            league[s] = float(x)
    fence = np.full(n, np.nan)
    keys = np.unique(np.stack([venue, season], axis=1), axis=0) if n else np.empty((0, 2))
    for v, y in keys:
        m = (venue == v) & (season == y)
        line = line_for(doc, int(v), int(y))
        arr = league.copy()
        if line is not None:
            for s in range(min(grid.n_sectors, len(line))):
                if line[s] is not None:
                    arr[s] = float(line[s])
        fence[m] = arr[sec[m]]
    decided = has_spray & np.isfinite(dist) & (dist > 0) & np.isfinite(fence)
    over = decided & (dist >= fence)
    short = decided & (dist < fence)
    margin = np.where(decided, dist - fence, np.nan)
    return Decision(sec=sec, fence=fence, decided=decided, over=over, short=short, margin=margin)


def _rates(hr: np.ndarray, d: Decision, mask: np.ndarray | None = None) -> dict[str, Any]:
    """P(HR|over), P(HR|short), missed, over-but-kept and the counts on a mask."""
    m = np.ones(len(hr), dtype=bool) if mask is None else mask
    over, short, decided = d.over & m, d.short & m, d.decided & m
    n_hr_decided = int((decided & hr).sum())
    return {
        "n_decided": int(decided.sum()),
        "n_over": int(over.sum()),
        "n_short": int(short.sum()),
        "n_hr": n_hr_decided,
        "p_hr_over": _share((over & hr).sum(), over.sum()),
        "p_hr_short": _share((short & hr).sum(), short.sum()),
        "missed": _share((short & hr).sum(), n_hr_decided),
        "over_but_kept": _share((over & ~hr).sum(), over.sum()),
    }


def decision_check(balls: dict[str, np.ndarray], doc: dict) -> dict[str, Any]:
    """§2.2 on the document: the whole window (every decided ball under the
    clamp), then the breakdowns — the |carry - fence| bands, the home runs by
    margin, the sectors, the venues (200+ decided) — on the balls INSIDE the
    grid (the balls a line describes; a clamped ball tests the grid, which is
    the down-the-line read), and the down-the-line balls on their own."""
    grid = document_grid(doc)
    hr = np.asarray(balls["hr"], dtype=bool)
    spray = np.asarray(balls["spray"], dtype=np.float64)
    dist = np.asarray(balls["dist"], dtype=np.float64)
    venue = np.asarray(balls["venue"], dtype=np.int64)
    d = decide(balls, doc)
    n = len(hr)
    out: dict[str, Any] = {
        "grid": asdict(grid),
        "n_air": int(n),
        "n_hr_all": int(hr.sum()),
        "hr_rate": _share(hr.sum(), n),
        "n_no_carry": int((~(np.isfinite(dist) & (dist > 0))).sum()),
        "n_passed": int((~d.decided).sum()),
    }
    out.update(_rates(hr, d))
    out["hr_decided_over"] = _share((d.over & hr).sum(), out["n_hr"])
    inside = d.decided & np.isfinite(spray) & (spray >= grid.spray_min) & (spray < grid.spray_max)
    out["n_inside_grid"] = int(inside.sum())
    out["n_clamped"] = int((d.decided & ~inside).sum())
    a = np.abs(d.margin)
    bands = []
    for lo, hi in BANDS:
        m = inside & (a >= lo) & (a < hi)
        mo, ms = m & d.over, m & d.short
        bands.append(
            {
                "band": f"{lo:g}-{hi:g}" if math.isfinite(hi) else f"{lo:g}+",
                "n": int(m.sum()),
                "n_over": int(mo.sum()),
                "p_hr_over": _share((mo & hr).sum(), mo.sum()),
                "n_short": int(ms.sum()),
                "p_hr_short": _share((ms & hr).sum(), ms.sum()),
            }
        )
    out["bands"] = bands
    hm = inside & hr
    out["hr_by_margin"] = {
        label: _share((hm & (d.margin >= lo) & (d.margin < hi)).sum(), hm.sum())
        for label, lo, hi in MARGIN_BANDS
    }
    league = list(doc.get("league") or [])
    sectors = []
    for s in range(grid.n_sectors):
        m = inside & (d.sec == s)
        r = _rates(hr, d, m)
        sectors.append(
            {
                "sector": s,
                "lo": grid.lo(s),
                "hi": grid.hi(s),
                "n": r["n_decided"],
                "n_over": r["n_over"],
                "p_hr_over": r["p_hr_over"],
                "missed": r["missed"],
                "league_line": league[s] if s < len(league) else None,
            }
        )
    out["sectors"] = sectors
    venues = []
    for v in np.unique(venue):
        m = inside & (venue == v)
        if m.sum() < VENUE_MIN_DECIDED:
            continue
        r = _rates(hr, d, m)
        venues.append({"venue": int(v), "name": _name(int(v)), **r})
    venues.sort(key=lambda r: r["p_hr_over"])
    out["venues"] = venues
    dtl = np.isfinite(spray) & (np.abs(spray) >= DOWN_THE_LINE_DEG) & d.decided
    r = _rates(hr, d, dtl)
    out["down_the_line"] = {
        "n": int(dtl.sum()),
        "share_of_decided": _share(dtl.sum(), d.decided.sum()),
        "hr_rate": _share((dtl & hr).sum(), dtl.sum()),
        **r,
    }
    return out


def _whole_window_lines(balls: dict[str, np.ndarray], seasons: list[int], grid: Grid) -> dict:
    """The line rule on a grid the builder does not carry (an old document):
    one line per venue and sector over all the balls, the league line the
    median over venues; a document ``decide`` reads (plain lists)."""
    spray = np.asarray(balls["spray"], dtype=np.float64)
    inside = np.isfinite(spray) & (spray >= grid.spray_min) & (spray < grid.spray_max)
    b = subset(balls, inside)
    sec = sector_of(b["spray"], grid.spray_min, grid.sector_deg, grid.n_sectors)
    hr = np.asarray(b["hr"], dtype=bool)
    venues: dict[str, list] = {}
    for v in np.unique(b["venue"]):
        vm = b["venue"] == v
        line = []
        for s in range(grid.n_sectors):
            m = vm & (sec == s)
            line.append(sector_line(b["dist"][m & hr], b["dist"][m & ~hr])[0])
        venues[str(int(v))] = line
    league = []
    for s in range(grid.n_sectors):
        vals = [float(line[s]) for line in venues.values() if line[s] is not None]
        league.append(round(float(np.median(vals)), 1) if vals else None)
    return {
        "seasons": [int(s) for s in seasons],
        "sector_deg": grid.sector_deg,
        "spray_min": grid.spray_min,
        "spray_max": grid.spray_max,
        "n_sectors": grid.n_sectors,
        "league": league,
        "venues": venues,
    }


def rebuild_lines(balls: dict[str, np.ndarray], seasons: list[int], grid: Grid) -> dict:
    """A geometry document from these balls alone: the builder's own
    ``build_geometry_document`` (no prior, no overrides) when the grid is
    the builder's; the same line rule on the grid otherwise."""
    if grid == BUILDER_GRID:
        return build_geometry_document(balls, seasons, {}, {})
    return _whole_window_lines(balls, seasons, grid)


def with_carry(balls: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """The balls with their own distance (what a rebuild may use)."""
    dist = np.asarray(balls["dist"], dtype=np.float64)
    return subset(balls, np.isfinite(dist) & (dist > 0))


def held_out(balls: dict[str, np.ndarray], seasons: list[int], doc: dict) -> list[dict[str, Any]]:
    """§2.3: for each season, the lines rebuilt from the other seasons and
    the decision on the held-out season's balls inside the grid."""
    grid = document_grid(doc)
    carry = with_carry(balls)
    spray = np.asarray(carry["spray"], dtype=np.float64)
    inside = np.isfinite(spray) & (spray >= grid.spray_min) & (spray < grid.spray_max)
    season = np.asarray(carry["season"], dtype=np.int64)
    method = "build_geometry_document" if grid == BUILDER_GRID else "line rule on the document grid"
    out = []
    for h in seasons:
        train_seasons = [int(s) for s in seasons if int(s) != int(h)]
        train = subset(carry, season != int(h))
        rebuilt = rebuild_lines(train, train_seasons, grid)
        test = subset(carry, (season == int(h)) & inside)
        d = decide(test, rebuilt)
        r = _rates(np.asarray(test["hr"], dtype=bool), d)
        out.append({"season": int(h), "method": method, **r})
    return out


def moved_walls(
    balls: dict[str, np.ndarray], doc: dict, seasons: list[int]
) -> list[dict[str, Any]]:
    """§2.4 defect 2: the change detector on every venue (the builder's grid);
    for every move, the checked document's read on the late seasons' balls at
    the moved sectors against a line built on the late seasons alone."""
    carry = with_carry(balls)
    spray = np.asarray(carry["spray"], dtype=np.float64)
    inside = (
        np.isfinite(spray) & (spray >= BUILDER_GRID.spray_min) & (spray < BUILDER_GRID.spray_max)
    )
    b = subset(carry, inside)
    b["sec"] = sector_of(
        b["spray"], BUILDER_GRID.spray_min, BUILDER_GRID.sector_deg, BUILDER_GRID.n_sectors
    )
    out = []
    for v in np.unique(b["venue"]):
        vb = subset(b, b["venue"] == v)
        _groups, moves = detect_moves(vb, seasons, BUILDER_GRID.n_sectors)
        for mv in moves:
            to = int(mv["to"])
            late_seasons = [int(s) for s in seasons if int(s) >= to]
            late_all = subset(vb, vb["season"] >= to)
            late_doc = rebuild_lines(late_all, late_seasons, BUILDER_GRID)
            at = np.isin(late_all["sec"], np.asarray(mv["sectors"], dtype=np.int64))
            test = subset(late_all, at)
            hr = np.asarray(test["hr"], dtype=bool)
            live = _rates(hr, decide(test, doc))
            late = _rates(hr, decide(test, late_doc))
            out.append(
                {
                    "venue": int(v),
                    "name": _name(int(v)),
                    "sectors": [int(s) for s in mv["sectors"]],
                    "spray_lo": BUILDER_GRID.lo(min(mv["sectors"])),
                    "spray_hi": BUILDER_GRID.hi(max(mv["sectors"])),
                    "from": int(mv["from"]),
                    "to": to,
                    "early": [float(x) for x in mv["early"]],
                    "late": [float(x) for x in mv["late"]],
                    "n": int(len(hr)),
                    "n_hr": int(hr.sum()),
                    "live_p_hr_over": live["p_hr_over"],
                    "live_missed": live["missed"],
                    "live_over_but_kept": live["over_but_kept"],
                    "late_p_hr_over": late["p_hr_over"],
                    "late_missed": late["missed"],
                    "late_over_but_kept": late["over_but_kept"],
                    "excess_missed": live["missed"] - late["missed"],
                }
            )
    out.sort(key=lambda m: (m["venue"], m["to"]))
    return out


def thin_parks(
    doc: dict,
    balls: dict[str, np.ndarray],
    seasons: list[int],
    games_by_venue: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    """§2.4 defect 3: the venues with balls in the window's last season whose
    group for that season has a league-line sector (the verdict) or a sector
    under ``THIN_HR`` home runs (informational), with the pool's game count."""
    last = int(max(seasons))
    games_by_venue = games_by_venue or {}
    venue = np.asarray(balls["venue"], dtype=np.int64)
    season = np.asarray(balls["season"], dtype=np.int64)
    n = int(doc.get("n_sectors", 9))
    out = []
    for v in np.unique(venue[season == last]):
        v = int(v)
        src = _venue_entry(doc.get("source"), v, last)
        sup = _venue_entry(doc.get("support_hr"), v, last)
        has_geometry = line_for(doc, v, last) is not None
        if src is None:
            league = list(range(n))
        else:
            league = [i for i, s in enumerate(src) if s == "league"]
        thin = list(range(n)) if sup is None else [i for i, h in enumerate(sup) if int(h) < THIN_HR]
        if not league and not thin:
            continue
        out.append(
            {
                "venue": v,
                "name": _name(v),
                "season": last,
                "games": int(games_by_venue.get(v, 0)),
                "has_geometry": has_geometry,
                "league_sectors": league,
                "thin_sectors": thin,
                "source": list(src) if src is not None else None,
                "support_hr": [int(h) for h in sup] if sup is not None else None,
            }
        )
    out.sort(key=lambda r: (-len(r["league_sectors"]), -r["games"], r["venue"]))
    return out


def document_offsets(doc: dict) -> dict[int, float]:
    """The document's carry offsets (``carry_offset_ft``, feet per venue);
    a null value or an absent park reads as no offset."""
    raw = doc.get("carry_offset_ft") or {}
    out: dict[int, float] = {}
    for k, v in raw.items():
        if v is not None and math.isfinite(float(v)):
            out[int(k)] = float(v)
    return out


def transfer_cells(ev: np.ndarray, la: np.ndarray, spray: np.ndarray) -> np.ndarray:
    """A7: one integer cell index per ball on the ``TRANSFER_CELL`` grid
    (exit velocity, launch angle, |spray|); balls in one cell share an index
    from 0 to the number of occupied cells minus one."""
    w_ev, w_la, w_sp = TRANSFER_CELL
    keys = np.stack(
        [
            np.floor(np.asarray(ev, dtype=np.float64) / w_ev),
            np.floor(np.asarray(la, dtype=np.float64) / w_la),
            np.floor(np.abs(np.asarray(spray, dtype=np.float64)) / w_sp),
        ],
        axis=1,
    ).astype(np.int64)
    if len(keys) == 0:
        return np.zeros(0, dtype=np.int64)
    _cells, inv = np.unique(keys, axis=0, return_inverse=True)
    return np.asarray(inv, dtype=np.int64).reshape(-1)


def transfer_check(
    balls: dict[str, np.ndarray],
    doc: dict,
    min_decided: int = VENUE_MIN_DECIDED,
    min_cell: int = TRANSFER_MIN_CELL,
) -> dict[str, Any]:
    """A7 (the plan's §11): every pool air ball "born" into each park.

    The pool: the air balls with an exit velocity, a launch angle, a spray and
    a distance. For every park with ``min_decided`` decided balls of its own
    in the window: the fair target = the mean over ALL pool balls of the
    park's home-run rate in the ball's cell (``transfer_cells``; a cell with
    fewer than ``min_cell`` of the park's balls takes the league's cell
    rate); the share of pool balls called over the park's fence — the park's
    group for the ball's season, the document's grid — with (a) the ball's
    own distance and (c) the ball's own distance + the park's offset − the
    ball's own park's offset (``document_offsets``; 0 when absent). A ball
    the park's line does not decide (no fence at its sector) is left out of
    (a) and (c). ``graded`` is False when the document carries no offsets
    or the balls no exit velocity / launch angle; then ``over_c`` is NaN."""
    offsets = document_offsets(doc)
    has_offsets = bool(offsets)
    out: dict[str, Any] = {
        "graded": False,
        "has_offsets": has_offsets,
        "cell": list(TRANSFER_CELL),
        "min_cell": int(min_cell),
        "min_decided": int(min_decided),
        "n_pool": 0,
        "parks": [],
    }
    if "ev" not in balls or "la" not in balls:
        out["reason"] = "the balls carry no exit velocity / launch angle"
        return out
    ev = np.asarray(balls["ev"], dtype=np.float64)
    la = np.asarray(balls["la"], dtype=np.float64)
    spray = np.asarray(balls["spray"], dtype=np.float64)
    dist = np.asarray(balls["dist"], dtype=np.float64)
    venue = np.asarray(balls["venue"], dtype=np.int64)
    pool = np.isfinite(ev) & np.isfinite(la) & np.isfinite(spray) & np.isfinite(dist) & (dist > 0)
    p = subset(balls, pool)
    n_pool = int(pool.sum())
    out["n_pool"] = n_pool
    if n_pool == 0:
        out["reason"] = "no pool ball with the three factors and a distance"
        return out
    p_hr = np.asarray(p["hr"], dtype=bool).astype(np.float64)
    p_venue = np.asarray(p["venue"], dtype=np.int64)
    p_dist = np.asarray(p["dist"], dtype=np.float64)
    inv = transfer_cells(p["ev"], p["la"], p["spray"])
    n_cells = int(inv.max()) + 1
    league_cnt = np.bincount(inv, minlength=n_cells).astype(np.float64)
    league_hr = np.bincount(inv, weights=p_hr, minlength=n_cells)
    league_rate = np.divide(league_hr, league_cnt, out=np.zeros(n_cells), where=league_cnt > 0)
    ball_offset = np.array([offsets.get(int(v), 0.0) for v in p_venue], dtype=np.float64)
    own = decide(balls, doc)
    game_pk = balls.get("game_pk")
    parks = []
    for v in np.unique(venue):
        v = int(v)
        n_decided = int((own.decided & (venue == v)).sum())
        if n_decided < min_decided:
            continue
        m = p_venue == v
        park_cnt = np.bincount(inv[m], minlength=n_cells).astype(np.float64)
        park_hr = np.bincount(inv[m], weights=p_hr[m], minlength=n_cells)
        park_rate = np.divide(park_hr, park_cnt, out=np.zeros(n_cells), where=park_cnt > 0)
        rate = np.where(park_cnt >= min_cell, park_rate, league_rate)
        target = float(rate[inv].mean())
        born = dict(p)
        born["venue"] = np.full(n_pool, v, dtype=np.int64)
        d = decide(born, doc)
        dec = d.decided
        n_dec = int(dec.sum())
        over_a = _share((d.over & dec).sum(), n_dec)
        if has_offsets:
            shifted = p_dist + offsets.get(v, 0.0) - ball_offset
            over_c = _share((dec & (shifted >= d.fence)).sum(), n_dec)
        else:
            over_c = float("nan")
        games = 0
        if game_pk is not None:
            games = int(len(np.unique(np.asarray(game_pk)[venue == v])))
        parks.append(
            {
                "venue": v,
                "name": _name(v),
                "games": games,
                "n_decided": n_decided,
                "n_pool_decided": n_dec,
                "offset_ft": offsets.get(v),
                "own_hr_share": _share(p_hr[m].sum(), m.sum()),
                "covered_share": float((park_cnt >= min_cell)[inv].mean()),
                "target": target,
                "over_a": over_a,
                "over_c": over_c,
                "gap_a": over_a - target,
                "gap_c": over_c - target if has_offsets else float("nan"),
            }
        )
    parks.sort(key=lambda r: -abs(r["gap_a"]) if math.isfinite(r["gap_a"]) else 0.0)
    out["parks"] = parks
    out["graded"] = has_offsets and bool(parks)
    if not parks:
        out["reason"] = f"no park with {min_decided}+ decided balls"
    elif not has_offsets:
        out["reason"] = "the document carries no carry offsets"
    return out


def expectation_se(rates: Any, season: int, lineup: dict[str, list]) -> float | None:
    """B2: the standard error of one game's ``hr_bip`` expectation
    (``sim523_game_set._game_expectation``) from the actors' own sample sizes.

    The expectation is half the batters' side plus half the starters' side.
    The batters' side is the mean of the nine batters' own rates, so its
    error is the standard error of a mean of nine measured rates: each
    batter's rate p_b was measured on n_b balls in play (the pool's
    recency-weighted count), with variance p_b(1 - p_b) / n_b; the mean's
    variance is their sum over n_bat squared. The starters' side weighs the
    starters' mean rate by the starter share (0.6), so its variance is
    (0.5 * 0.6) squared times the same sum over the starters. The rest of
    each side is the pool total, a constant. Only the actors with a rate
    count (``actor_rates`` returns one from 60 plate appearances and 30
    balls in play); a side with no rated actor contributes nothing, as in
    the expectation. Returns None when the expectation itself is None
    (fewer than 12 rated batters or no rated starter)."""
    var = 0.0
    sides = (
        ("batter_id", lineup.get("batters", []), B2_SIDE_WEIGHT),
        ("pitcher_id", lineup.get("pitchers", []), B2_SIDE_WEIGHT * B2_STARTER_SHARE),
    )
    n_rated = {}
    for actor, ids, weight in sides:
        terms = []
        rated = 0
        for a in ids:
            r = rates.actor_rates(actor, (int(a), int(season)))
            if r is None:
                continue
            rated += 1
            if "hr_bip" not in r:
                continue
            bip = rates.bip[actor].get((int(a), int(season)))
            n_a = float(bip[0]) if bip is not None else 0.0
            if n_a <= 0:
                continue
            pr = float(r["hr_bip"])
            terms.append(pr * (1.0 - pr) / n_a)
        n_rated[actor] = rated
        if terms:
            var += weight * weight * sum(terms) / (len(terms) ** 2)
    if n_rated["batter_id"] < 12 or n_rated["pitcher_id"] == 0:
        return None
    return math.sqrt(var)


def park_table(
    per_game: list[dict[str, Any]],
    expected: dict[int, float],
    factor: dict[int, float],
    thresholds: Thresholds = THRESHOLDS,
    expected_se: dict[int, float] | None = None,
    min_games: int = B2_MIN_GAMES,
) -> dict[str, Any]:
    """B2, the arithmetic: per venue the sim's HR per ball in play against
    the BIP-weighted mean of its games' expected ``hr_bip``; the residual
    against the pool's park home-run factor. A venue ENTERS the read
    (``entered``) with ``min_games`` games or more; only entering venues
    join r and the flag. The standard error per venue combines the sim's
    binomial error (``se``, the old column) with the expectation's own
    (``se_exp``: the BIP-weighted MEAN of the games' ``expected_se`` — the
    home lineup repeats across a park's games, so the games' expectation
    errors are not independent and do not shrink with their count) as
    ``se_combined`` = sqrt(se² + se_exp²); a venue is flagged when
    |residual| exceeds ``b2_se`` combined errors. r is reported, not graded:
    ``passed`` = at least one venue entered and none flagged. A game without
    an expectation, or without a venue (``venue_id`` None or 0, the lane's
    "no venue" value), is left out of both sides and counted under
    ``n_games_skipped``."""
    expected_se = expected_se or {}
    by_venue: dict[int, dict[str, float]] = {}
    skipped = 0
    for g in per_game:
        e = expected.get(int(g["game_pk"]))
        if e is None or not g.get("venue_id"):
            skipped += 1
            continue
        acc = by_venue.setdefault(
            int(g["venue_id"]), {"games": 0, "bip": 0.0, "hr": 0.0, "exp": 0.0, "exp_se": 0.0}
        )
        bip = float(g.get("BIP", 0.0))
        acc["games"] += 1
        acc["bip"] += bip
        acc["hr"] += float(g.get("HR", 0.0))
        acc["exp"] += bip * float(e)
        acc["exp_se"] += bip * float(expected_se.get(int(g["game_pk"]), 0.0))
    rows = []
    for v, acc in sorted(by_venue.items()):
        if acc["bip"] <= 0:
            continue
        sim = acc["hr"] / acc["bip"]
        exp = acc["exp"] / acc["bip"]
        se = math.sqrt(max(sim * (1.0 - sim), 0.0) / acc["bip"])
        se_exp = acc["exp_se"] / acc["bip"]
        se_combined = math.sqrt(se * se + se_exp * se_exp)
        resid = sim - exp
        f = factor.get(v)
        entered = int(acc["games"]) >= int(min_games)
        rows.append(
            {
                "venue": v,
                "name": _name(v),
                "games": int(acc["games"]),
                "bip": acc["bip"],
                "sim_hr_bip": sim,
                "expected_hr_bip": exp,
                "residual": resid,
                "se": se,
                "se_exp": se_exp,
                "se_combined": se_combined,
                "factor": f,
                "entered": entered,
                "flagged": bool(
                    entered and se_combined > 0 and abs(resid) > thresholds.b2_se * se_combined
                ),
            }
        )
    xs = [r["residual"] for r in rows if r["entered"] and r["factor"] is not None]
    ys = [r["factor"] - 1.0 for r in rows if r["entered"] and r["factor"] is not None]
    r_val = (
        float(np.corrcoef(xs, ys)[0, 1])
        if len(xs) >= 3 and np.std(xs) > 0 and np.std(ys) > 0
        else float("nan")
    )
    flagged = [r["venue"] for r in rows if r["flagged"]]
    n_entered = sum(1 for r in rows if r["entered"])
    return {
        "rows": rows,
        "n_venues": len(rows),
        "n_entered": n_entered,
        "min_games": int(min_games),
        "n_games_skipped": skipped,
        "r": r_val,
        "flagged": flagged,
        "passed": bool(n_entered > 0 and not flagged),
    }


def counters_read(
    fence_counts: list[int] | None, bb_margin_counts: list[int] | None = None
) -> dict[str, Any] | None:
    """The stage's own counters from a lane JSON (informational, the plan's
    D): [over, short, band, passed, no matching rows, not an air ball]; a
    ground ball counts in both [1] and [5]. Every share is over the AIR balls,
    the denominator the harness's D read (``scripts/sim_stats.py``) grades,
    so the two readers of one lane agree. With ``bb_margin_counts`` (the
    wall-margin band's [applied, fallback (too few rows), skipped]) the read
    gains ``margin``: the three counts and the first two as shares of the
    air balls (a skipped count includes the balls that are not air balls,
    so it has no share)."""
    if not fence_counts or len(fence_counts) < 6:
        return None
    c = [int(x) for x in fence_counts[:6]]
    born = c[0] + c[1] + c[2] + c[3]
    air = born - c[5]
    out = {
        "counts": c,
        "born": born,
        "air": air,
        "over_share_of_air": _share(c[0], air),
        "short_share_of_air": _share(c[1] - c[5], air),
        "passed_share_of_air": _share(c[3], air),
        "no_rows_share_of_air": _share(c[4], air),
    }
    if bb_margin_counts is not None and len(bb_margin_counts) >= 3:
        m = [int(x) for x in bb_margin_counts[:3]]
        out["margin"] = {
            "counts": m,
            "applied_share_of_air": _share(m[0], air),
            "fallback_share_of_air": _share(m[1], air),
            "skipped": m[2],
        }
    return out


def verdicts(summary: dict[str, Any], thresholds: Thresholds = THRESHOLDS) -> list[dict[str, Any]]:
    """The §7 checks on a summary (``decision``, ``held_out``, ``moves``,
    ``thin``, and ``park`` when a lane was read). One row per check."""
    out = []
    dec = summary["decision"]
    ok = dec["p_hr_over"] >= thresholds.a1_p_hr_over and dec["missed"] <= thresholds.a1_missed
    out.append(
        {
            "check": "A1",
            "passed": bool(ok),
            "text": f"whole window: P(HR|over) {dec['p_hr_over']:.3f} (>= {thresholds.a1_p_hr_over:.2f}), "
            f"missed {dec['missed']:.1%} (<= {thresholds.a1_missed:.0%})",
        }
    )
    ven = dec.get("venues", [])
    bad = [
        v
        for v in ven
        if v["p_hr_over"] < thresholds.a2_p_hr_over or v["missed"] > thresholds.a2_missed
    ]
    worst_p = min((v["p_hr_over"] for v in ven), default=float("nan"))
    worst_m = max((v["missed"] for v in ven), default=float("nan"))
    out.append(
        {
            "check": "A2",
            "passed": bool(ven) and not bad,
            "text": f"{len(ven)} venues with {VENUE_MIN_DECIDED}+ decided: worst P(HR|over) {worst_p:.3f} "
            f"(>= {thresholds.a2_p_hr_over:.2f}), worst missed {worst_m:.1%} (<= {thresholds.a2_missed:.0%})"
            + (f"; failing: {', '.join(_name(v['venue']) for v in bad)}" if bad else ""),
        }
    )
    ho = summary.get("held_out", [])
    off = [
        h
        for h in ho
        if abs(h["p_hr_over"] - dec["p_hr_over"]) > thresholds.a3_delta
        or abs(h["missed"] - dec["missed"]) > thresholds.a3_delta
    ]
    spans = ", ".join(f"{h['season']} {h['p_hr_over']:.3f}/{h['missed']:.1%}" for h in ho)
    out.append(
        {
            "check": "A3",
            "passed": bool(ho) and not off,
            "text": f"held-out seasons within {thresholds.a3_delta:.0%} of {dec['p_hr_over']:.3f}/{dec['missed']:.1%}: {spans}"
            + (f"; off: {', '.join(str(h['season']) for h in off)}" if off else ""),
        }
    )
    dtl = dec["down_the_line"]
    out.append(
        {
            "check": "A4",
            "passed": bool(dtl["n"] > 0 and dtl["missed"] <= thresholds.a4_missed),
            "text": f"down-the-line balls (|spray| >= {DOWN_THE_LINE_DEG:g}): n {dtl['n']:,}, "
            f"P(HR|over) {dtl['p_hr_over']:.3f}, missed {dtl['missed']:.1%} (<= {thresholds.a4_missed:.0%})",
        }
    )
    moves = summary.get("moves", [])
    bad_moves = [m for m in moves if m["excess_missed"] > thresholds.a5_excess]
    out.append(
        {
            "check": "A5",
            "passed": not bad_moves,
            "text": f"{len(moves)} moves listed; the live line's late-season missed share within "
            f"{thresholds.a5_excess:.0%} of the late line's"
            + (
                "; over: "
                + ", ".join(
                    f"{m['name']} {m['to']} sectors {m['sectors']} {m['live_missed']:.1%} vs {m['late_missed']:.1%}"
                    for m in bad_moves
                )
                if bad_moves
                else ""
            ),
        }
    )
    thin = [t for t in summary.get("thin", []) if t["league_sectors"]]
    out.append(
        {
            "check": "A6",
            "passed": not thin,
            "text": "no last-season venue on the league line"
            if not thin
            else "league-line sectors: "
            + ", ".join(
                f"{t['name']} {len(t['league_sectors'])} of {len(t['source']) if t['source'] else '?'} ({t['games']} games)"
                for t in thin
            ),
        }
    )
    tr = summary.get("transfer")
    if tr is not None:
        out.append(_a7_verdict(tr, thresholds))
    park = summary.get("park")
    if park is not None:
        r_txt = f"{park['r']:.3f}" if math.isfinite(park["r"]) else "nan"
        n_entered = park.get("n_entered", park["n_venues"])
        min_games = park.get("min_games", 1)
        out.append(
            {
                "check": "B2",
                "passed": bool(park["passed"]),
                "text": f"per-park read: {n_entered} of {park['n_venues']} venues with {min_games}+ games enter; "
                f"r {r_txt} (reported, not graded); "
                f"{len(park['flagged'])} venues beyond {thresholds.b2_se:g} combined SE"
                + (f" ({', '.join(_name(v) for v in park['flagged'])})" if park["flagged"] else "")
                + ("; no venue entered" if not n_entered else ""),
            }
        )
    return out


def _a7_verdict(tr: dict[str, Any], thresholds: Thresholds) -> dict[str, Any]:
    """A7's row: PASS when every park's (c) sits within ``a7_gap`` of its
    target; "not graded" (passed, so the run does not fail) when the
    document carries no offsets or no park qualifies."""
    parks = tr.get("parks", [])
    if not tr.get("graded"):
        reason = tr.get("reason") or "no read"
        worst_a = max((abs(r["gap_a"]) for r in parks), default=float("nan"))
        return {
            "check": "A7",
            "passed": True,
            "graded": False,
            "text": f"not graded: {reason}"
            + (
                f"; {len(parks)} parks read with (a) alone, worst gap {worst_a:.1%}"
                if parks
                else ""
            ),
        }
    bad = [r for r in parks if not (abs(r["gap_c"]) <= thresholds.a7_gap)]
    worst = max(parks, key=lambda r: abs(r["gap_c"]))
    return {
        "check": "A7",
        "passed": not bad,
        "graded": True,
        "text": f"transfer over {len(parks)} parks: every (c) within {thresholds.a7_gap:.1%} of its target; "
        f"worst {worst['name']} {worst['gap_c']:+.1%}"
        + ("; off: " + ", ".join(f"{r['name']} {r['gap_c']:+.1%}" for r in bad) if bad else ""),
    }


# ---------------------------------------------------------------------------
# The loaders (DuckDB read-only; Postgres for the lane's lineups)
# ---------------------------------------------------------------------------


def load_check_balls(con: Any, seasons: list[int]) -> dict[str, np.ndarray]:
    """Every fly ball and line drive of the window with a venue — the
    builder's ``load_air_balls`` columns WITHOUT its distance and spray
    filters, so the balls the stage passes are counted. ``ev`` / ``la`` (the
    exit velocity and launch angle, NaN when absent) serve the transfer
    check (A7) and the carry-offset fit of a rebuilt document; ``game_pk``
    gives A7 its game count per park."""
    season_list = ", ".join(str(int(s)) for s in seasons)
    d = con.execute(
        f"""
        SELECT venue_id, season, spray_angle, hit_distance, events,
               exit_velo, launch_angle, game_pk
        FROM sim.outcome_pool
        WHERE season IN ({season_list})
          AND bb_type IN ('fly_ball', 'line_drive')
          AND venue_id IS NOT NULL
        """
    ).fetchnumpy()
    venue = np.asarray(np.ma.filled(d["venue_id"], -1), dtype=np.int64)
    season = np.asarray(np.ma.filled(d["season"], 0), dtype=np.int64)
    spray = np.asarray(np.ma.filled(d["spray_angle"], np.nan), dtype=np.float64)
    dist = np.asarray(np.ma.filled(d["hit_distance"], np.nan), dtype=np.float64)
    events = np.asarray(np.ma.filled(d["events"], ""), dtype=object).astype(str)
    hr = events == "home_run"
    ev = np.asarray(np.ma.filled(d["exit_velo"], np.nan), dtype=np.float64)
    la = np.asarray(np.ma.filled(d["launch_angle"], np.nan), dtype=np.float64)
    ev = np.where(ev > 0, ev, np.nan)
    game_pk = np.asarray(np.ma.filled(d["game_pk"], 0), dtype=np.int64)
    return {
        "venue": venue,
        "season": season,
        "spray": spray,
        "dist": dist,
        "hr": hr,
        "kept300": (~hr) & np.isfinite(dist) & (dist >= 300.0),
        "ev": ev,
        "la": la,
        "game_pk": game_pk,
    }


def load_games_by_venue(con: Any, season: int) -> dict[int, int]:
    rows = con.execute(
        "SELECT venue_id, count(DISTINCT game_pk) FROM sim.outcome_pool "
        "WHERE season = ? AND venue_id IS NOT NULL GROUP BY 1",
        [int(season)],
    ).fetchall()
    return {int(v): int(c) for v, c in rows}


def load_park_hr_factor(con: Any, seasons: list[int]) -> dict[int, float]:
    """The pool's park home-run factor: the venue's home runs per ball in
    play over the window divided by the pool's overall (plain counts)."""
    season_list = ", ".join(str(int(s)) for s in seasons)
    rows = con.execute(
        f"SELECT venue_id, count(*), sum(CASE WHEN events = 'home_run' THEN 1 ELSE 0 END) "
        f"FROM sim.outcome_pool WHERE season IN ({season_list}) AND venue_id IS NOT NULL GROUP BY 1"
    ).fetchall()
    tot_bip = sum(int(r[1]) for r in rows)
    tot_hr = sum(int(r[2]) for r in rows)
    overall = tot_hr / tot_bip if tot_bip else float("nan")
    return {int(v): (int(h) / int(b)) / overall for v, b, h in rows if int(b) > 0 and overall > 0}


def lane_expectations(
    con: Any, per_game: list[dict[str, Any]]
) -> tuple[dict[int, float], dict[int, float]]:
    """Each lane game's actor-matched ``hr_bip`` expectation from the game-set
    builder (the pools' own rates for the game's starters, the lineups from
    Postgres) and its standard error (``expectation_se``), as two maps by
    game_pk."""
    import asyncpg
    from sim523_game_set import PoolRates, _dsn, _game_expectation, _lineups

    pks = sorted({int(g["game_pk"]) for g in per_game})

    async def _fetch() -> dict[int, dict[str, list]]:
        conn = await asyncpg.connect(_dsn())
        try:
            return await _lineups(conn, pks)
        finally:
            await conn.close()

    lineups = asyncio.run(_fetch())
    rates = PoolRates(con)
    out: dict[int, float] = {}
    ses: dict[int, float] = {}
    for g in per_game:
        pk = int(g["game_pk"])
        lineup = lineups.get(pk)
        if not lineup:
            continue
        e = _game_expectation(rates, int(g["season"]), lineup)
        if e is not None and "hr_bip" in e:
            out[pk] = float(e["hr_bip"])
            se = expectation_se(rates, int(g["season"]), lineup)
            if se is not None:
                ses[pk] = float(se)
    return out, ses


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _fmt(x: float, nd: int = 3) -> str:
    return f"{x:.{nd}f}" if x is not None and math.isfinite(x) else "-"


def _pct(x: float) -> str:
    return f"{x:.1%}" if x is not None and math.isfinite(x) else "-"


def print_decision(dec: dict[str, Any], say: Any) -> None:
    g = dec["grid"]
    say(
        f"=== A. the decision on every real air ball (grid {g['spray_min']:g}..{g['spray_max']:g}, "
        f"{g['n_sectors']} sectors; the ball's own distance as the carry) ==="
    )
    say(
        f"air balls {dec['n_air']:,}; home runs {dec['n_hr_all']:,} ({dec['hr_rate']:.4f}); "
        f"decided {dec['n_decided']:,}; passed {dec['n_passed']:,} (no carry {dec['n_no_carry']:,})"
    )
    say(
        f"over {dec['n_over']:,} | short {dec['n_short']:,} | P(HR|over) {dec['p_hr_over']:.4f} | "
        f"P(HR|short) {dec['p_hr_short']:.5f} | missed home runs {dec['missed']:.4f} "
        f"| over-but-kept {dec['over_but_kept']:.4f}"
    )
    say(
        f"the breakdowns read the {dec['n_inside_grid']:,} decided balls inside the grid "
        f"({dec['n_clamped']:,} clamped balls are the down-the-line read)"
    )
    say("by |carry - fence| (ft): n | over n, P(HR) | short n, P(HR)")
    for b in dec["bands"]:
        say(
            f"  {b['band']:>6}: n {b['n']:7,} | over {b['n_over']:6,} P(HR) {_fmt(b['p_hr_over'])} "
            f"| short {b['n_short']:7,} P(HR) {_fmt(b['p_hr_short'], 4)}"
        )
    hm = dec["hr_by_margin"]
    say(
        "home runs by margin over the fence: "
        + ", ".join(f"{k} ft {_fmt(v)}" for k, v in hm.items())
    )
    say("by sector: n, over, P(HR|over), missed, league line")
    for s in dec["sectors"]:
        say(
            f"  sector {s['sector']:2d} [{s['lo']:+.0f}..{s['hi']:+.0f}): n {s['n']:7,} over {s['n_over']:6,} "
            f"P(HR|over) {_fmt(s['p_hr_over'])} missed {_fmt(s['missed'], 4)} league {s['league_line']}"
        )
    say(
        f"by venue ({VENUE_MIN_DECIDED}+ decided), worst first: decided, over, P(HR|over), missed, HR"
    )
    for v in dec["venues"]:
        say(
            f"  {v['name']:14s} {v['venue']:5d}: decided {v['n_decided']:6,} over {v['n_over']:5,} "
            f"P(HR|over) {_fmt(v['p_hr_over'])} missed {_fmt(v['missed'])} HR {v['n_hr']:5,}"
        )
    d = dec["down_the_line"]
    say(
        f"down the line (|spray| >= {DOWN_THE_LINE_DEG:g}): n {d['n']:,} ({d['share_of_decided']:.4f} of decided), "
        f"home-run rate {_fmt(d['hr_rate'], 4)}, over {d['n_over']:,}, P(HR|over) {_fmt(d['p_hr_over'], 4)}, "
        f"missed {_fmt(d['missed'], 4)}"
    )


def print_held_out(rows: list[dict[str, Any]], say: Any) -> None:
    say("=== A3. held-out seasons (the lines rebuilt from the other seasons) ===")
    for h in rows:
        say(
            f"  held-out {h['season']}: n {h['n_decided']:7,} | P(HR|over) {_fmt(h['p_hr_over'], 4)} | "
            f"missed {_fmt(h['missed'], 4)} | over-but-kept {_fmt(h['over_but_kept'], 4)}  [{h['method']}]"
        )


def print_moves(rows: list[dict[str, Any]], say: Any) -> None:
    say(f"=== A5. moved walls (the change detector on the builder's grid): {len(rows)} moves ===")
    for m in rows:
        say(
            f"  {m['name']:14s} {m['venue']:5d} sectors {m['sectors']} [{m['spray_lo']:+.0f}..{m['spray_hi']:+.0f}) "
            f"{m['from']} -> {m['to']}: early {m['early']} late {m['late']} | late-season balls n {m['n']:,} HR {m['n_hr']} | "
            f"live: P(HR|over) {_fmt(m['live_p_hr_over'])} missed {_fmt(m['live_missed'])} | "
            f"late line: P(HR|over) {_fmt(m['late_p_hr_over'])} missed {_fmt(m['late_missed'])} | "
            f"excess {m['excess_missed']:+.3f}"
        )


def print_thin(rows: list[dict[str, Any]], season: int, say: Any) -> None:
    say(
        f"=== A6. thin parks in {season}: league-line sectors (the verdict) and sectors under {THIN_HR} HR ==="
    )
    for t in rows:
        say(
            f"  {t['name']:14s} {t['venue']:5d}: games {t['games']:3d} | "
            f"{'NO GEOMETRY | ' if not t['has_geometry'] else ''}league {t['league_sectors']} | thin {t['thin_sectors']}"
            + (f" | source {t['source']}" if t["league_sectors"] and t["source"] else "")
        )


def print_transfer(tr: dict[str, Any], say: Any) -> None:
    cell = tr.get("cell", list(TRANSFER_CELL))
    say(
        f"=== A7. the transfer: every pool air ball born into each park ({tr.get('min_decided', VENUE_MIN_DECIDED)}+ decided); "
        f"n pool {tr.get('n_pool', 0):,}; cells {cell[0]:g} mph x {cell[1]:g} deg x {cell[2]:g} deg |spray|, "
        f"a cell under {tr.get('min_cell', TRANSFER_MIN_CELL)} park balls at the league rate ==="
    )
    if not tr.get("parks"):
        say(f"  no park read: {tr.get('reason', '-')}")
        return
    has = bool(tr.get("has_offsets"))
    say(
        "  venue                 games  offset  own HR  target  (a) own dist    gap"
        + ("  (c) + offset    gap" if has else "")
    )
    for r in tr["parks"]:
        off = f"{r['offset_ft']:+6.1f}" if r.get("offset_ft") is not None else "     -"
        line = (
            f"  {r['name']:14s} {r['venue']:5d} {r['games']:5d}  {off}  {_pct(r['own_hr_share']):>6} "
            f"{_pct(r['target']):>7}  {_pct(r['over_a']):>12} {r['gap_a']:+6.1%}"
        )
        if has:
            line += f"  {_pct(r['over_c']):>12} {r['gap_c']:+6.1%}"
        say(line)
    if not has:
        say("  (c) needs the document's carry offsets (``carry_offset_ft``): not read")


def print_park(park: dict[str, Any], say: Any) -> None:
    say(
        f"=== B2. the per-park read: {park['n_venues']} venues, {park.get('n_entered', park['n_venues'])} with "
        f"{park.get('min_games', 1)}+ games enter ({park['n_games_skipped']} games without an expectation) ==="
    )
    say(
        "  venue                 games      BIP    sim  expected residual  SE sim  SE exp SE comb  factor"
    )
    for r in park["rows"]:
        say(
            f"  {r['name']:14s} {r['venue']:5d} {r['games']:5d} {r['bip']:8.0f} {r['sim_hr_bip']:.4f}  {r['expected_hr_bip']:.4f} "
            f"{r['residual']:+.4f}  {r['se']:.4f}  {r.get('se_exp', 0.0):.4f}  {r.get('se_combined', r['se']):.4f}  "
            f"{_fmt(r['factor']) if r['factor'] is not None else '-'}"
            + ("" if r.get("entered", True) else "  (not entered)")
            + ("  <-- beyond 3 combined SE" if r["flagged"] else "")
        )
    say(
        f"  r(residual, factor - 1) over the entering venues = {_fmt(park['r'])} (reported, not graded)"
    )


def print_counters(c: dict[str, Any] | None, say: Any) -> None:
    if c is None:
        return
    say(
        f"=== D. the stage's counters (informational): over {c['counts'][0]:,} short {c['counts'][1]:,} band {c['counts'][2]:,} "
        f"passed {c['counts'][3]:,} no-rows {c['counts'][4]:,} not-air {c['counts'][5]:,} ==="
    )
    say(
        f"  air balls {c['air']:,}: over {c['over_share_of_air']:.4f} (the pool's home-run share of air balls ~0.09), "
        f"passed {c['passed_share_of_air']:.4f} (<= 0.001), no matching rows {c['no_rows_share_of_air']:.4f} (<= 0.005)"
    )
    m = c.get("margin")
    if m is not None:
        say(
            f"  wall-margin band (informational): applied {m['counts'][0]:,} ({_pct(m['applied_share_of_air'])} of air balls) "
            f"· fallback (too few rows) {m['counts'][1]:,} ({_pct(m['fallback_share_of_air'])}) · skipped {m['skipped']:,}"
        )


def run_check(
    balls: dict[str, np.ndarray],
    doc: dict,
    seasons: list[int],
    games_by_venue: dict[int, int] | None = None,
) -> dict[str, Any]:
    """The offline checks A1-A7 on a document and the window's balls."""
    return {
        "seasons": [int(s) for s in seasons],
        "decision": decision_check(balls, doc),
        "held_out": held_out(balls, seasons, doc),
        "moves": moved_walls(balls, doc, seasons),
        "thin": thin_parks(doc, balls, seasons, games_by_venue),
        "transfer": transfer_check(balls, doc),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--geometry", default=DEFAULT_GEOMETRY, help="the park_geometry.json to certify"
    )
    ap.add_argument(
        "--duckdb-path", default=DEFAULT_DUCKDB, help="the DuckDB file (opened read-only)"
    )
    ap.add_argument(
        "--seasons", type=int, nargs="*", default=None, help="the window (default: the document's)"
    )
    ap.add_argument("--json-out", default=None, help="write every number and the verdicts here")
    ap.add_argument("--lane", default=None, help="a lane JSON for the per-park read (B2)")
    ap.add_argument(
        "--min-games",
        type=int,
        default=B2_MIN_GAMES,
        help=f"B2: a park enters the read from this many lane games (default {B2_MIN_GAMES})",
    )
    ap.add_argument("--quiet", action="store_true", help="print only the verdict lines")
    args = ap.parse_args(argv)

    def say(text: str = "") -> None:
        if not args.quiet:
            print(text)

    import duckdb

    with open(args.geometry, encoding="utf-8") as fh:
        doc = json.load(fh)
    seasons = [int(s) for s in (args.seasons or doc.get("seasons") or [2023, 2024, 2025, 2026])]
    say(
        f"geometry {args.geometry}: version {doc.get('version', 1)}, {document_grid(doc).n_sectors} sectors, "
        f"{len(doc.get('venues', {}))} venues, seasons {seasons}"
    )
    con = duckdb.connect(args.duckdb_path, read_only=True)
    balls = load_check_balls(con, seasons)
    games_by_venue = load_games_by_venue(con, max(seasons))
    summary = run_check(balls, doc, seasons, games_by_venue)
    summary["geometry"] = args.geometry
    summary["thresholds"] = asdict(THRESHOLDS)
    print_decision(summary["decision"], say)
    say()
    print_held_out(summary["held_out"], say)
    say()
    print_moves(summary["moves"], say)
    say()
    print_thin(summary["thin"], max(seasons), say)
    say()
    print_transfer(summary["transfer"], say)

    if args.lane:
        with open(args.lane, encoding="utf-8") as fh:
            lane = json.load(fh)
        per_game = list(lane.get("per_game", []))
        expected, expected_se = lane_expectations(con, per_game)
        factor = load_park_hr_factor(con, seasons)
        summary["park"] = park_table(
            per_game, expected, factor, expected_se=expected_se, min_games=args.min_games
        )
        summary["park"]["lane"] = args.lane
        summary["counters"] = counters_read(lane.get("fence_counts"), lane.get("bb_margin_counts"))
        say()
        print_park(summary["park"], say)
        print_counters(summary["counters"], say)

    summary["verdicts"] = verdicts(summary)
    say()
    for v in summary["verdicts"]:
        tag = "PASS" if v["passed"] else "FAIL"
        if v.get("graded") is False:
            tag = "INFO"
        print(f"{tag} {v['check']} {v['text']}")
    all_pass = all(v["passed"] for v in summary["verdicts"])
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            # ``default`` sees only what json cannot encode; a plain float NaN
            # is encodable, so it is cleared first (a bare NaN is not JSON).
            json.dump(_without_nan(summary), fh, indent=2, default=_jsonable)
        say(f"wrote {args.json_out}")
    return 0 if all_pass else 1


def _without_nan(x: Any) -> Any:
    """``x`` with every non-finite float (a plain one or numpy's) as None,
    through dicts, lists and tuples."""
    if isinstance(x, dict):
        return {k: _without_nan(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_without_nan(v) for v in x]
    if isinstance(x, (float, np.floating)) and not math.isfinite(float(x)):
        return None
    return x


def _jsonable(x: Any) -> Any:
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        return None if not np.isfinite(x) else float(x)
    if isinstance(x, np.ndarray):
        return x.tolist()
    if isinstance(x, float) and not math.isfinite(x):
        return None
    raise TypeError(f"not JSON serialisable: {type(x)!r}")


if __name__ == "__main__":
    sys.exit(main())
