"""Build and read park_geometry.json: each park's fence by spray sector, and the carry model.

The fence stage reads the document's fence lines, its carry model and its carry offsets.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import duckdb
import numpy as np

from bundle.common import read_json, write_json
from profiles.sql import sql_seasons

log = logging.getLogger("engine_artifacts")


# ---------------------------------------------------------------------------
# The park geometry and the carry model
# ---------------------------------------------------------------------------
#
# The pool's own batted balls reveal each park's fence. Per venue and spray SECTOR (a band of
# the field-side spray angle, ``PARK_SECTOR_DEG`` degrees wide), the fence line is the carry a
# ball needs to leave the park. THE LINE RULE takes the midpoint of the shortest home-run
# carries (their 10th percentile) and the longest carries that stayed in (the 99th percentile
# of the other air balls). A tall wall shows up as a longer required carry, so one number per
# sector is the EFFECTIVE fence. The build merges an optional hand-curated
# ``park_geometry_overrides.json`` over the built lines.
#
# The document (version 2):
#
#   1. The grid runs to +-55 degrees (eleven sectors), so a ball down the line (about -50)
#      has a sector of its own.
#   2. A venue's lines come per SEASON GROUP. THE CHANGE DETECTOR splits a venue's seasons
#      where a sector's early and late lines, each on ``PARK_MOVE_MIN_HR`` home runs or more,
#      differ by ``PARK_MOVE_FT`` or more (for example, Camden Yards moved its left-field wall
#      in for 2025). The document keys a group "FIRST-LAST" and records each split in "moves".
#   3. A thin sector rests on a PUBLISHED-DISTANCE PRIOR, not on the league line. The prior is
#      the Stats API's five fence distances (``ingest/venues/load_venue_dimensions.py``)
#      interpolated at the sector's midpoint angle, plus the league's typical offset between
#      the effective and the published fence there. THE THIN RULE blends the prior toward the
#      park's own balls, at a weight of ``PARK_PRIOR_N`` home runs.
#
# THE CARRY MODEL: a home run's reported distance is its landing point. A quadratic in exit
# velocity and launch angle, fitted on the pool's home runs, stands in for a ball whose own
# distance is missing.
#
# THE CARRY OFFSET: one number per park, the park's mean home-run carry against the league's
# at the same exit velocity, launch angle and spray. It is the park term of the carry model,
# refitted with one dummy (a 0/1 column) per park (``carry_offsets``). The fit counts every
# home run of the window, so no ball is selected by its outcome. The offsets centre on the
# median park. A park under ``PARK_OFFSET_MIN_HR`` home runs gets no offset, and the sampler
# reads an absent park as 0. The sampler moves a born ball's distance into the LIVE park's air
# with ``offset[venue_live] - offset[venue_row]`` before the fence check and the fielding
# draw. The candidate rows keep their own distances.
#
# The pieces are pure functions over numpy arrays, so ``scripts/fence_check.py`` can rebuild a
# line from any subset of the balls: ``load_air_balls`` → ``sector_of`` → ``sector_line`` /
# ``detect_moves`` / ``published_at`` / ``prior_lines`` / ``carry_offsets`` →
# ``build_geometry_document``. ``build_park_geometry`` adds the carry model and writes the file.

PARK_GEOMETRY_VERSION = 2
PARK_SECTOR_DEG = 10
PARK_SPRAY_MIN = -55.0
PARK_SPRAY_MAX = 55.0
PARK_N_SECTORS = int(round((PARK_SPRAY_MAX - PARK_SPRAY_MIN) / PARK_SECTOR_DEG))
#: Minimum support for a venue sector's own line (home runs / balls kept in).
PARK_MIN_HR = 10
PARK_MIN_KEPT = 10
#: The change detector: a sector moved when its early and late lines, each on
#: ``PARK_MOVE_MIN_HR`` home runs or more, differ by ``PARK_MOVE_FT`` or more.
PARK_MOVE_FT = 10.0
PARK_MOVE_MIN_HR = 10
#: The published-distance prior's weight, in home runs, and the fewest venues a sector's
#: league offset needs.
PARK_PRIOR_N = 10
PARK_PRIOR_MIN_PARKS = 5
#: The five published points and the field-side angle of each.
PARK_PUBLISHED_POINTS: tuple[tuple[str, float], ...] = (
    ("leftLine", -45.0),
    ("leftCenter", -22.5),
    ("center", 0.0),
    ("rightCenter", 22.5),
    ("rightLine", 45.0),
)
#: A kept ball counts toward a sector's support from this distance.
PARK_KEPT_MIN_FT = 300.0
CARRY_FEATURES = ("1", "ev", "la", "la^2", "ev*la", "ev^2")
#: The carry offset's design: the carry model plus the spray terms. One dummy column per
#: park follows these columns.
CARRY_OFFSET_FEATURES = (
    "1",
    "ev",
    "la",
    "la^2",
    "ev*la",
    "ev^2",
    "|spray|",
    "spray^2",
    "ev*|spray|",
)
#: A park needs this many home runs with an exit velocity, a launch angle and a spray for its
#: own offset.
PARK_OFFSET_MIN_HR = 100


def park_sector(spray: float) -> int:
    """The sector index (0 .. PARK_N_SECTORS-1) of a field-side spray angle."""
    s = (float(spray) - PARK_SPRAY_MIN) / PARK_SECTOR_DEG
    return int(min(max(int(np.floor(s)), 0), PARK_N_SECTORS - 1))


def sector_of(
    spray: np.ndarray,
    spray_min: float = PARK_SPRAY_MIN,
    sector_deg: float = PARK_SECTOR_DEG,
    n_sectors: int = PARK_N_SECTORS,
) -> np.ndarray:
    """The sector index of every spray angle: ``floor((spray - spray_min) / sector_deg)``,
    clamped to the grid."""
    s = np.floor((np.asarray(spray, dtype=np.float64) - spray_min) / sector_deg)
    return np.clip(s, 0, n_sectors - 1).astype(np.int64)


def sector_midpoint(sec: int, spray_min: float, sector_deg: float) -> float:
    """The field-side angle at the middle of a sector."""
    return float(spray_min) + (int(sec) + 0.5) * float(sector_deg)


def carry_design(ev: np.ndarray, la: np.ndarray) -> np.ndarray:
    """The carry model's design matrix (``CARRY_FEATURES`` order)."""
    ev = np.asarray(ev, dtype=np.float64)
    la = np.asarray(la, dtype=np.float64)
    return np.column_stack([np.ones_like(ev), ev, la, la * la, ev * la, ev * ev])


def carry_predict(coef: list[float] | np.ndarray, ev: float, la: float) -> float:
    """The modelled carry (feet) of a ball hit at ``ev`` mph and ``la`` degrees."""
    out = carry_design(np.array([ev]), np.array([la])) @ np.asarray(coef, dtype=np.float64)
    return float(out[0])


def load_air_balls(con: duckdb.DuckDBPyConnection, seasons: list[int]) -> dict[str, np.ndarray]:
    """The window's fly balls and line drives that have a venue, a distance and a spray angle.

    Returns numpy arrays: ``venue``, ``season``, ``spray``, ``dist``, ``hr`` (a home run), and
    ``ev`` / ``la`` (exit velocity and launch angle, NaN where the pool has none). The fence
    lines keep every ball; the carry offset skips a ball with a NaN. The query applies no
    spray filter, because the builder drops a ball outside the grid itself."""
    season_list = sql_seasons(seasons)
    d = con.execute(
        f"""
        SELECT venue_id, season, spray_angle, hit_distance, events, exit_velo, launch_angle
        FROM sim.outcome_pool
        WHERE season IN ({season_list})
          AND bb_type IN ('fly_ball', 'line_drive')
          AND venue_id IS NOT NULL AND spray_angle IS NOT NULL
          AND hit_distance IS NOT NULL AND hit_distance > 0
        """
    ).fetchnumpy()
    venue = np.asarray(np.ma.filled(d["venue_id"], -1), dtype=np.int64)
    season = np.asarray(np.ma.filled(d["season"], 0), dtype=np.int64)
    spray = np.asarray(np.ma.filled(d["spray_angle"], np.nan), dtype=np.float64)
    dist = np.asarray(np.ma.filled(d["hit_distance"], np.nan), dtype=np.float64)
    events = np.asarray(np.ma.filled(d["events"], ""), dtype=object).astype(str)
    ev = np.asarray(np.ma.filled(d["exit_velo"], np.nan), dtype=np.float64)
    la = np.asarray(np.ma.filled(d["launch_angle"], np.nan), dtype=np.float64)
    hr = events == "home_run"
    return {
        "venue": venue,
        "season": season,
        "spray": spray,
        "dist": dist,
        "hr": hr,
        "ev": ev,
        "la": la,
    }


def carry_offset_design(ev: np.ndarray, la: np.ndarray, spray: np.ndarray) -> np.ndarray:
    """The carry offset's design matrix (``CARRY_OFFSET_FEATURES`` order)."""
    ev = np.asarray(ev, dtype=np.float64)
    sp = np.abs(np.asarray(spray, dtype=np.float64))
    return np.column_stack([carry_design(ev, la), sp, sp * sp, ev * sp])


def carry_offsets(
    balls: dict[str, np.ndarray], min_hr: int = PARK_OFFSET_MIN_HR
) -> tuple[dict[int, float], dict[str, Any]]:
    """THE CARRY OFFSET per park: the park term of the home-run carry model, refitted with
    one dummy column per park.

    The rows are the home runs with a finite exit velocity, launch angle, spray and distance.
    A park with fewer than ``min_hr`` such home runs stays out of the fit and gets no offset.
    The fit regresses distance on ``carry_offset_design`` plus one dummy per fitted park
    (least squares; the first park is the reference). Each park's term is then centred on
    the MEDIAN of the fitted parks' terms and rounded to 0.1 ft.

    Returns ``(offsets, fit)``. ``offsets`` maps venue id to feet. ``fit`` records ``n_hr``
    (the rows), ``n_parks``, ``min_hr``, ``reference_median_park`` (the fitted park whose
    term sits nearest the median; the smallest id on a tie), ``centre_ft`` (the median
    subtracted, against the first park) and ``residual_sd_ft``. Balls without ``ev`` /
    ``la`` give no offsets: ``({}, {"n_hr": 0, ...})``."""
    fit: dict[str, Any] = {
        "features": list(CARRY_OFFSET_FEATURES),
        "min_hr": int(min_hr),
        "n_hr": 0,
        "n_parks": 0,
        "reference_median_park": None,
        "centre_ft": None,
        "residual_sd_ft": None,
    }
    if "ev" not in balls or "la" not in balls or np.asarray(balls["hr"]).size == 0:
        return {}, fit
    ev = np.asarray(balls["ev"], dtype=np.float64)
    la = np.asarray(balls["la"], dtype=np.float64)
    spray = np.asarray(balls["spray"], dtype=np.float64)
    dist = np.asarray(balls["dist"], dtype=np.float64)
    venue = np.asarray(balls["venue"], dtype=np.int64)
    ok = (
        np.asarray(balls["hr"], dtype=bool)
        & np.isfinite(ev)
        & np.isfinite(la)
        & np.isfinite(spray)
        & np.isfinite(dist)
        & (dist > 0)
    )
    ids, counts = np.unique(venue[ok], return_counts=True)
    fitted = [int(v) for v, c in zip(ids, counts, strict=True) if int(c) >= int(min_hr)]
    rows = ok & np.isin(venue, fitted)
    fit["n_hr"] = int(rows.sum())
    fit["n_parks"] = len(fitted)
    if not fitted:
        return {}, fit
    x = carry_offset_design(ev[rows], la[rows], spray[rows])
    if len(fitted) > 1:
        col = {v: k for k, v in enumerate(fitted)}
        dummies = np.zeros((int(rows.sum()), len(fitted) - 1), dtype=np.float64)
        idx = np.array([col[int(v)] for v in venue[rows]], dtype=np.int64)
        has = idx > 0
        dummies[np.flatnonzero(has), idx[has] - 1] = 1.0
        x = np.column_stack([x, dummies])
    beta, *_ = np.linalg.lstsq(x, dist[rows], rcond=None)
    n_feat = len(CARRY_OFFSET_FEATURES)
    raw = {fitted[0]: 0.0}
    for k, v in enumerate(fitted[1:], start=1):
        raw[v] = float(beta[n_feat + k - 1])
    centre = float(np.median(list(raw.values())))
    # ``+ 0.0`` turns a rounded -0.0 into 0.0 for the document.
    offsets = {v: round(raw[v] - centre, 1) + 0.0 for v in fitted}
    fit["reference_median_park"] = min(fitted, key=lambda v: (abs(raw[v] - centre), v))
    fit["centre_ft"] = round(centre, 2)
    fit["residual_sd_ft"] = round(float(np.std(dist[rows] - x @ beta)), 2)
    return offsets, fit


def sector_line(
    dist_hr: np.ndarray, dist_kept: np.ndarray
) -> tuple[float | None, str | None, int, int]:
    """THE LINE RULE on one sector's balls; returns ``(line, source, n_hr, n_kept)``.

    ``dist_hr`` holds the home runs' carries, ``dist_kept`` every other air ball's. The home
    runs' 10th percentile needs ``PARK_MIN_HR`` home runs. The kept balls' 99th percentile
    needs ``PARK_MIN_KEPT`` kept balls of ``PARK_KEPT_MIN_FT`` or more, but the quantile runs
    over ALL kept balls. The line is the midpoint of the two, rounded to 0.1 ft ("both");
    one of the two alone when the other is thin ("hr" / "kept"); ``(None, None, …)`` when
    both are thin."""
    dist_hr = np.asarray(dist_hr, dtype=np.float64)
    dist_kept = np.asarray(dist_kept, dtype=np.float64)
    n_hr = int(dist_hr.size)
    n_kept = int((dist_kept >= PARK_KEPT_MIN_FT).sum())
    a = float(np.quantile(dist_hr, 0.10)) if n_hr >= PARK_MIN_HR else None
    b = float(np.quantile(dist_kept, 0.99)) if n_kept >= PARK_MIN_KEPT else None
    if a is not None and b is not None:
        return round((a + b) / 2.0, 1), "both", n_hr, n_kept
    if a is not None:
        return round(a, 1), "hr", n_hr, n_kept
    if b is not None:
        return round(b, 1), "kept", n_hr, n_kept
    return None, None, n_hr, n_kept


def published_at(published: dict | None, sec: int) -> float | None:
    """The published fence at a sector's midpoint angle: the five published
    points (the lines at +-45 degrees, the alleys at +-22.5, centre at 0)
    linearly interpolated at the midpoint, clamped to [-45, +45] so the outer
    sectors read the line distance. ``None`` when the venue publishes no
    point on either side of the angle."""
    if not published:
        return None
    pts = [
        (angle, float(published[key]))
        for key, angle in PARK_PUBLISHED_POINTS
        if published.get(key) is not None
    ]
    if len(pts) < 2:
        return None
    angle = min(max(sector_midpoint(sec, PARK_SPRAY_MIN, PARK_SECTOR_DEG), -45.0), 45.0)
    xs = [p[0] for p in pts]
    if angle < xs[0] or angle > xs[-1]:
        return None
    return float(np.interp(angle, xs, [p[1] for p in pts]))


def league_offsets(
    own_lines: dict[int, list[float | None]],
    own_sources: dict[int, list[str | None]],
    published_by_venue: dict[int, dict],
) -> list[float | None]:
    """Per sector, the median over venues of (own whole-window line minus the
    published fence at the sector) — the league's typical gap between the
    effective and the published fence. A venue contributes when its own line
    rests on ``PARK_MIN_HR`` home runs ("both" or "hr") and it publishes a
    distance there; ``None`` under ``PARK_PRIOR_MIN_PARKS`` venues."""
    out: list[float | None] = []
    for s in range(PARK_N_SECTORS):
        gaps = []
        for venue, line in own_lines.items():
            src = own_sources.get(venue, [None] * PARK_N_SECTORS)[s]
            if line[s] is None or src not in ("both", "hr"):
                continue
            pub = published_at(published_by_venue.get(int(venue)), s)
            if pub is None:
                continue
            gaps.append(float(line[s]) - pub)
        out.append(round(float(np.median(gaps)), 1) if len(gaps) >= PARK_PRIOR_MIN_PARKS else None)
    return out


def prior_lines(published: dict | None, league_offset: list[float | None]) -> list[float | None]:
    """One venue's PRIOR line per sector: the published fence at the sector's
    midpoint plus the league offset; ``None`` when either is missing."""
    out: list[float | None] = []
    for s in range(PARK_N_SECTORS):
        pub = published_at(published, s)
        off = league_offset[s] if s < len(league_offset) else None
        out.append(round(pub + off, 1) if (pub is not None and off is not None) else None)
    return out


def _line_of(
    balls: dict[str, np.ndarray], mask: np.ndarray
) -> tuple[float | None, str | None, int, int]:
    """The line rule on the balls a mask selects."""
    return sector_line(balls["dist"][mask & balls["hr"]], balls["dist"][mask & ~balls["hr"]])


def _thin_rule(
    own: float | None,
    src: str | None,
    n_hr: int,
    prior: float | None,
    league_line: float | None,
) -> tuple[float | None, str]:
    """THE THIN RULE for one group and sector; returns ``(line, source)``.

    The park's own estimate stands at ``PARK_PRIOR_N`` home runs or more, or when there is no
    prior. Below that, the own estimate blends with the prior. Without an own estimate, the
    prior stands, else the league line."""
    if own is not None and src is not None:
        if n_hr >= PARK_PRIOR_N or prior is None:
            return own, src
        if n_hr <= 0:
            return prior, "prior"
        blend = (n_hr * own + PARK_PRIOR_N * prior) / (n_hr + PARK_PRIOR_N)
        return round(blend, 1), "blend"
    if prior is not None:
        return prior, "prior"
    return league_line, "league"


def detect_moves(
    balls: dict[str, np.ndarray], seasons: list[int]
) -> tuple[list[tuple[int, int]], list[dict]]:
    """THE CHANGE DETECTOR on ONE venue's balls (``balls`` holds ``season``, ``sec``,
    ``dist``, ``hr``); returns the groups as ``(first, last)`` season pairs, and the moves.

    Each boundary between consecutive seasons of the venue in the window splits the current
    group into early (before it) and late (from it). A boundary qualifies when some sector's
    early and late lines each rest on ``PARK_MOVE_MIN_HR`` home runs and differ by
    ``PARK_MOVE_FT``. The qualifying boundary with the largest gap splits the group, and the
    search runs again on each side.

    A known limit: the detector sees a wall that moves in the LAST season of the window only
    once that season holds ``PARK_MOVE_MIN_HR`` home runs at the moved sector. Until then the
    sector reads the blended line, or the prior-blended line when the season's own support
    is thin."""
    window = {int(s) for s in seasons}
    present = sorted(int(y) for y in np.unique(balls["season"]) if int(y) in window)
    groups: list[tuple[int, int]] = []
    moves: list[dict] = []
    if not present:
        return groups, moves
    sec = balls["sec"]
    season = balls["season"]

    def _lines(mask: np.ndarray) -> list[tuple[float | None, int]]:
        return [
            (line, n_hr)
            for line, _src, n_hr, _n_kept in (
                _line_of(balls, mask & (sec == s)) for s in range(PARK_N_SECTORS)
            )
        ]

    def _split(members: list[int]) -> None:
        # The key is (the largest gap, the gap sum, the boundary index), so a tie goes to the
        # LATER boundary. A late block feeds the 10th percentile of every earlier boundary's
        # late side, so the ties sit at or before the move.
        best: tuple[float, float, int] | None = None
        best_detail: tuple[list[int], list[float], list[float]] | None = None
        in_group = np.isin(season, members)
        for i in range(1, len(members)):
            y = members[i]
            early = _lines(in_group & (season < y))
            late = _lines(in_group & (season >= y))
            secs, e_lines, l_lines, gaps = [], [], [], []
            for s in range(PARK_N_SECTORS):
                (le, ne), (ll, nl) = early[s], late[s]
                if le is None or ll is None or ne < PARK_MOVE_MIN_HR or nl < PARK_MOVE_MIN_HR:
                    continue
                gap = abs(ll - le)
                if gap >= PARK_MOVE_FT:
                    secs.append(s)
                    e_lines.append(le)
                    l_lines.append(ll)
                    gaps.append(gap)
            if not secs:
                continue
            key = (max(gaps), float(sum(gaps)), i)
            if best is None or key > best:
                best = key
                best_detail = (secs, e_lines, l_lines)
        if best is None or best_detail is None:
            groups.append((members[0], members[-1]))
            return
        i = best[2]
        secs, e_lines, l_lines = best_detail
        moves.append(
            {
                "venue": int(balls["venue"][0])
                if "venue" in balls and balls["venue"].size
                else None,
                "sectors": secs,
                "from": members[i - 1],
                "to": members[i],
                "early": e_lines,
                "late": l_lines,
            }
        )
        _split(members[:i])
        _split(members[i:])

    _split(present)
    groups.sort()
    moves.sort(key=lambda m: (m["to"], m["sectors"]))
    return groups, moves


def group_key(first: int, last: int) -> str:
    """A season group's document key, ``"FIRST-LAST"``."""
    return f"{int(first)}-{int(last)}"


def parse_group_key(key: str) -> tuple[int, int]:
    """The ``(first, last)`` seasons of a group key."""
    first, last = key.split("-", 1)
    return int(first), int(last)


def group_for_season(groups: dict[str, Any], season: int | None) -> str | None:
    """The group key whose range holds ``season``; the LATEST group (the
    largest last season) when none does or the season is ``None``."""
    if not groups:
        return None
    if season is not None:
        for key in groups:
            first, last = parse_group_key(key)
            if first <= int(season) <= last:
                return key
    return max(groups, key=lambda k: parse_group_key(k)[1])


def _apply_overrides(
    overrides: dict,
    venues: dict[str, dict[str, list[float | None]]],
    source: dict[str, dict[str, list[str]]],
    default_seed: Any,
    default_key: str,
    n_sectors: int,
    support_hr: dict[str, dict[str, list[int]]] | None = None,
    support_kept: dict[str, dict[str, list[int]]] | None = None,
) -> None:
    """Apply the hand-curated corrections over the built lines, in place.

    ``{venue: {sector: feet}}`` applies to every group of the venue; ``{venue: {"FIRST-LAST":
    {sector: feet}}}`` applies to that group alone. A venue the pool never saw, or a group the
    builder did not produce, starts from ``default_seed(venue)``: the seed line and its
    per-sector sources ("prior" where the venue's prior gave the value, "league" otherwise).
    A new venue's group key is ``default_key``. A seeded group gets zero support in
    ``support_hr`` / ``support_kept``, because no ball built it."""

    def _seed(v: str, g: str) -> None:
        line, srcs = default_seed(v)
        venues.setdefault(v, {})[g] = list(line)
        source.setdefault(v, {})[g] = list(srcs)
        if support_hr is not None:
            support_hr.setdefault(v, {})[g] = [0] * n_sectors
        if support_kept is not None:
            support_kept.setdefault(v, {})[g] = [0] * n_sectors

    for venue, spec in overrides.items():
        v = str(venue)
        if v not in venues:
            _seed(v, default_key)
        flat: dict[str, dict[str, float]] = {}
        for key, val in spec.items():
            if isinstance(val, dict):
                flat[str(key)] = {str(k): float(f) for k, f in val.items()}
            else:
                for g in venues[v]:
                    flat.setdefault(g, {})[str(key)] = float(val)
        for g, secs in flat.items():
            if g not in venues[v]:
                _seed(v, g)
            for sec_key, feet in secs.items():
                s = int(sec_key)
                if 0 <= s < n_sectors:
                    venues[v][g][s] = float(feet)
                    source[v][g][s] = "override"


def build_geometry_document(
    balls: dict[str, np.ndarray],
    seasons: list[int],
    published_by_venue: dict[int, dict] | None = None,
    overrides: dict | None = None,
) -> dict:
    """The version-2 geometry document WITHOUT the carry block.

    The document holds the sector grid, the league line, and every venue's lines per season
    group with their sources and support. It also holds the league offset, the priors, the
    moves, the overrides, and the carry offset per park (``carry_offset_ft``, str venue to
    feet, with its fit record ``carry_offset_fit``). ``balls`` is ``load_air_balls``'s dict;
    the function drops a ball outside the grid and counts it in ``dropped_outside_grid``."""
    published_by_venue = published_by_venue or {}
    overrides = overrides or {}
    n = PARK_N_SECTORS
    spray = np.asarray(balls["spray"], dtype=np.float64)
    inside = np.isfinite(spray) & (spray >= PARK_SPRAY_MIN) & (spray < PARK_SPRAY_MAX)
    dropped = int((~inside).sum())
    b = {k: np.asarray(v)[inside] for k, v in balls.items()}
    b["sec"] = sector_of(b["spray"], PARK_SPRAY_MIN, PARK_SECTOR_DEG, n)
    venue_ids = sorted(int(v) for v in np.unique(b["venue"]))

    # The whole-window own lines feed the league line and the league offset.
    own_lines: dict[int, list[float | None]] = {}
    own_sources: dict[int, list[str | None]] = {}
    for v in venue_ids:
        vm = b["venue"] == v
        lines, srcs = [], []
        for s in range(n):
            line, src, _n_hr, _n_kept = _line_of(b, vm & (b["sec"] == s))
            lines.append(line)
            srcs.append(src)
        own_lines[v] = lines
        own_sources[v] = srcs
    league: list[float | None] = []
    for s in range(n):
        vals = [float(lines[s]) for lines in own_lines.values() if lines[s] is not None]
        league.append(round(float(np.median(vals)), 1) if vals else None)
    league_offset = league_offsets(own_lines, own_sources, published_by_venue)
    # Every published venue gets a prior line — the ones in the pool and the
    # ones the pool has never seen (a new park before its first ball).
    priors: dict[str, dict] = {}
    for v in sorted(set(venue_ids) | {int(k) for k in published_by_venue}):
        pub = published_by_venue.get(v)
        if not pub:
            continue
        line_p = prior_lines(pub, league_offset)
        if all(p is None for p in line_p):
            continue
        priors[str(v)] = {
            "published": {key: pub.get(key) for key, _angle in PARK_PUBLISHED_POINTS},
            "line": line_p,
        }

    # The season groups per venue, and the lines inside each.
    venues: dict[str, dict[str, list[float | None]]] = {}
    source: dict[str, dict[str, list[str]]] = {}
    support_hr: dict[str, dict[str, list[int]]] = {}
    support_kept: dict[str, dict[str, list[int]]] = {}
    moves: list[dict] = []
    for v in venue_ids:
        vm = b["venue"] == v
        vb = {k: arr[vm] for k, arr in b.items()}
        groups, v_moves = detect_moves(vb, seasons)
        moves.extend(v_moves)
        prior = priors.get(str(v), {}).get("line", [None] * n)
        key_v = str(v)
        venues[key_v], source[key_v], support_hr[key_v], support_kept[key_v] = {}, {}, {}, {}
        for first, last in groups:
            gm = (vb["season"] >= first) & (vb["season"] <= last)
            g_line: list[float | None] = []
            g_src: list[str] = []
            g_hr: list[int] = []
            g_kept: list[int] = []
            for s in range(n):
                own, src, n_hr, n_kept = _line_of(vb, gm & (vb["sec"] == s))
                line, how = _thin_rule(own, src, n_hr, prior[s], league[s])
                g_line.append(line)
                g_src.append(how)
                g_hr.append(n_hr)
                g_kept.append(n_kept)
            g = group_key(first, last)
            venues[key_v][g] = g_line
            source[key_v][g] = g_src
            support_hr[key_v][g] = g_hr
            support_kept[key_v][g] = g_kept

    window_key = group_key(min(seasons), max(seasons)) if seasons else group_key(0, 0)

    def _default_seed(v: str) -> tuple[list[float | None], list[str]]:
        # The seed of an override-created venue or group: the venue's prior
        # where it exists, the league line elsewhere, each sector labelled.
        prior_line = priors.get(v, {}).get("line")
        if prior_line is None:
            return list(league), ["league"] * n
        line = [p if p is not None else lg for p, lg in zip(prior_line, league, strict=True)]
        srcs = ["prior" if p is not None else "league" for p in prior_line]
        return line, srcs

    # A published venue the pool has never seen gets the prior alone, as one group over the
    # window. Its first balls blend the prior away at the next build; until then the sampler
    # reads this line instead of the league's.
    for key_v in sorted(priors):
        if key_v in venues:
            continue
        seed_line, seed_srcs = _default_seed(key_v)
        venues[key_v] = {window_key: seed_line}
        source[key_v] = {window_key: seed_srcs}
        support_hr[key_v] = {window_key: [0] * n}
        support_kept[key_v] = {window_key: [0] * n}

    _apply_overrides(
        overrides, venues, source, _default_seed, window_key, n, support_hr, support_kept
    )
    # The carry offset per park, on the same balls (inside the grid).
    offsets, offset_fit = carry_offsets(b)
    return {
        "version": PARK_GEOMETRY_VERSION,
        "seasons": [int(s) for s in seasons],
        "sector_deg": PARK_SECTOR_DEG,
        "spray_min": PARK_SPRAY_MIN,
        "spray_max": PARK_SPRAY_MAX,
        "n_sectors": n,
        "league": league,
        "venues": venues,
        "source": source,
        "support_hr": support_hr,
        "support_kept": support_kept,
        "league_offset": league_offset,
        "prior": priors,
        "moves": moves,
        "dropped_outside_grid": dropped,
        "overrides": overrides,
        "carry_offset_ft": {str(v): float(f) for v, f in sorted(offsets.items())},
        "carry_offset_fit": offset_fit,
    }


def build_park_geometry(con: duckdb.DuckDBPyConnection, out_dir: str, seasons: list[int]) -> dict:
    """Write ``<out_dir>/park_geometry.json`` (version 2) from the pool window; return the
    document.

    The document adds the carry model and its home-run validation to
    ``build_geometry_document``'s. The function also refreshes
    ``<out_dir>/venue_dimensions.json`` from the Stats API; the last copy stands when the API
    is down."""
    from ingest.venues.load_venue_dimensions import write_venue_dimensions

    balls = load_air_balls(con, seasons)
    published = write_venue_dimensions(out_dir, seasons)
    overrides: dict = read_json(out_dir, "park_geometry_overrides.json") or {}
    doc = build_geometry_document(balls, seasons, published, overrides)

    # The carry model, fitted and validated on home runs.
    season_list = sql_seasons(seasons)
    d = con.execute(
        f"SELECT exit_velo, launch_angle, hit_distance FROM sim.outcome_pool "
        f"WHERE season IN ({season_list}) AND events = 'home_run' AND hit_distance > 0 "
        "AND exit_velo IS NOT NULL AND launch_angle IS NOT NULL"
    ).fetchnumpy()
    ev = np.asarray(np.ma.filled(d["exit_velo"], np.nan), dtype=np.float64)
    la = np.asarray(np.ma.filled(d["launch_angle"], np.nan), dtype=np.float64)
    hd = np.asarray(np.ma.filled(d["hit_distance"], np.nan), dtype=np.float64)
    ok = np.isfinite(ev) & np.isfinite(la) & np.isfinite(hd)
    carry: dict = {"features": list(CARRY_FEATURES), "coef": None, "n_hr": int(ok.sum())}
    if ok.sum() >= 50:
        x = carry_design(ev[ok], la[ok])
        coef, *_ = np.linalg.lstsq(x, hd[ok], rcond=None)
        pred = x @ coef
        carry["coef"] = [float(c) for c in coef]
        carry["hr_mae_ft"] = round(float(np.abs(pred - hd[ok]).mean()), 2)
        carry["hr_rmse_ft"] = round(float(np.sqrt(((pred - hd[ok]) ** 2).mean())), 2)
    doc["carry"] = carry

    os.makedirs(out_dir, exist_ok=True)
    write_json(os.path.join(out_dir, "park_geometry.json"), doc)
    n_groups = sum(len(g) for g in doc["venues"].values())
    offsets = doc.get("carry_offset_ft") or {}
    if offsets:
        spread = float(np.std(list(offsets.values())))
        coors = offsets.get("19")
        offset_note = (
            f"carry offsets for {len(offsets)} parks on {doc['carry_offset_fit']['n_hr']} "
            f"home runs (spread SD {spread:.1f} ft; Coors "
            f"{'absent' if coors is None else f'{coors:+.1f} ft'})"
        )
    else:
        offset_note = "no carry offsets (no park reached the home-run minimum)"
    log.info(
        "park_geometry v%d: %d venues (%d season groups, %d moves) x %d sectors; "
        "%d balls outside the grid dropped; %d venues with a published prior; "
        "carry model on %d home runs (MAE %s ft); %s",
        PARK_GEOMETRY_VERSION,
        len(doc["venues"]),
        n_groups,
        len(doc["moves"]),
        PARK_N_SECTORS,
        doc["dropped_outside_grid"],
        len(doc["prior"]),
        carry["n_hr"],
        carry.get("hr_mae_ft"),
        offset_note,
    )
    return doc
