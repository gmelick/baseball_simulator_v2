"""The fence stage, the carry and the wall-margin band.

Before the fielding draw, the born ball's carry meets the live park's fence at
its direction. Over the fence, the draw runs among home-run rows; short of it,
among the rest; inside the band, every row stays. The carry offset re-reads the
born ball's distance in the live park's air. The wall-margin band keeps a
wall-zone air ball to rows that landed as near their own wall.
"""

from __future__ import annotations

import numpy as np

from pipeline.batch.artifacts.park_geometry import carry_predict, parse_group_key
from simulation.sampler.base import BaseMixin


class FenceMixin(BaseMixin):
    """The sampler's fence-stage methods."""

    def _init_fence(self) -> None:
        """The fence stage's switches, counters and caches."""
        #: The wall zone: an air ball (a line drive or a fly ball) carrying at
        #: least this many feet.
        self.wall_zone_distance = 300.0
        #: The fence stage (``SIM_FENCE_STAGE``) and its band in feet. The band
        #: is 0, a decisive fence: a 10-foot band lost about 30% of home runs,
        #: because a born home run inside the band drew a home run only 21% of
        #: the time under the born kernel's ~50 ft distance bandwidth.
        self.fence_stage = False
        self.fence_margin = 0.0
        #: Fence decisions: [0] over, [1] short, [2] the band, [3] passed (no
        #: geometry, venue or direction), [4] a decision with no matching rows
        #: (the rows stay), [5] not an air ball (also counted in [1]).
        self.fence_counts = np.zeros(6, dtype=np.int64)
        #: The last decision of ``_fence_rows`` (None before the stage runs).
        self._fence_last_dec: int | None = None
        #: The parsed season groups of each venue, keyed to the document.
        self._fence_groups: dict[str, list[tuple[int, int, list]]] = {}
        self._fence_groups_doc: dict | None = None
        #: Per-hand home-run mask over the batted-ball pool.
        self._bb_hr_cache: dict[str, np.ndarray] = {}
        #: The carry offset (``SIM_CARRY_OFFSET``): the born ball's carry in
        #: the live park's air, its own distance plus offset[live park] minus
        #: offset[its park] from the document's ``carry_offset_ft``. The fence
        #: check, the born kernel and the wall margin all read that one number;
        #: the candidate rows keep their own distances. Off, or a document
        #: without the table: no shift.
        self.carry_offset = False
        self._carry_offsets_cache: dict[int, float] | None = None
        self._carry_offsets_doc: dict | None = None
        #: The wall-margin band (``SIM_BB_MARGIN_BAND``, feet; 0 = off). A
        #: row's margin is its distance minus its own park's fence at its spray
        #: and season. A wall-zone air ball the fence did not call over draws
        #: only rows within the band of its own margin to the live fence;
        #: fewer than ``bb_margin_min_rows`` survivors leave the rows as they
        #: were. Counts: [0] applied, [1] fell back, [2] skipped.
        self.bb_margin_band = 0.0
        self.bb_margin_min_rows = 20
        self.bb_margin_counts = np.zeros(3, dtype=np.int64)
        #: Per-hand row margins (NaN = unknown), keyed to the document.
        self._bb_margin_cache: dict[str, np.ndarray] = {}
        self._bb_margin_doc: dict | None = None

    def in_wall_zone(self, born: dict) -> bool:
        """Is the born ball a wall-zone ball — an air ball (line drive or
        fly ball) carrying at least ``wall_zone_distance`` feet?"""
        cls = born.get("cls")
        dist = born.get("dist")
        if cls is None or dist is None:
            return False
        return int(cls) in (2, 3) and float(dist) >= float(self.wall_zone_distance)

    # ---- the fence stage -------------------------------------------------
    def _fence_line(self, pg: dict, venue_id: int | None, season: int | None) -> list | None:
        """The venue's fence line for a season. A venue's entry is
        either a plain list (an old document — the one group) or a dict of
        ``"FIRST-LAST"`` group keys. The group whose range holds the season
        wins; with no such group, or no season, the latest group (the
        largest last season). None for a venue the document lacks."""
        if venue_id is None:
            return None
        v = str(int(venue_id))
        entry = (pg.get("venues") or {}).get(v)
        if entry is None:
            return None
        if not isinstance(entry, dict):
            return list(entry)
        if self._fence_groups_doc is not pg:
            self._fence_groups = {}
            self._fence_groups_doc = pg
        groups = self._fence_groups.get(v)
        if groups is None:
            groups = [(*parse_group_key(str(k)), list(line)) for k, line in entry.items()]
            self._fence_groups[v] = groups
        if not groups:
            return None
        if season is not None:
            for first, last, line in groups:
                if first <= int(season) <= last:
                    return line
        return max(groups, key=lambda g: g[1])[2]

    def fence_at(
        self, venue_id: int | None, spray: float | None, season: int | None = None
    ) -> float | None:
        """The live park's effective fence (feet of carry) at a field-side
        spray angle in a season: the venue's line for that season's group,
        the league line for a venue or sector the geometry lacks, None
        without geometry or direction. The grid (sector width, its first
        angle, the sector count) is read from the document."""
        pg = getattr(self.a, "park_geometry", None)
        if not pg or spray is None or not np.isfinite(float(spray)):
            return None
        n = int(pg.get("n_sectors", 0))
        if n <= 0:
            return None
        s = (float(spray) - float(pg.get("spray_min", -45.0))) / float(pg.get("sector_deg", 10))
        sec = int(min(max(int(np.floor(s)), 0), n - 1))
        league = pg.get("league") or []
        line = self._fence_line(pg, venue_id, season)
        f = line[sec] if line is not None and sec < len(line) else None
        if f is None and sec < len(league):
            f = league[sec]
        return float(f) if f is not None else None

    def _carry_offsets(self) -> dict[int, float] | None:
        """The document's ``carry_offset_ft`` table as {venue:
        feet}, parsed once per document object; None without the table (or
        an empty one). A null entry (a park under 100 home runs) reads 0."""
        pg = getattr(self.a, "park_geometry", None)
        if not pg:
            return None
        if self._carry_offsets_doc is not pg:
            table = pg.get("carry_offset_ft") or {}
            offs: dict[int, float] = {}
            for k, v in table.items():
                try:
                    offs[int(k)] = float(v) if v is not None else 0.0
                except (TypeError, ValueError):
                    continue
            self._carry_offsets_cache = {k: v for k, v in offs.items() if np.isfinite(v)} or None
            self._carry_offsets_doc = pg
        return self._carry_offsets_cache

    def carry_shift(self, born: dict, venue_live: int | None) -> float:
        """offset[live park] - offset[the born ball's park], in
        feet; 0 when :attr:`carry_offset` is off, the document has no table,
        the live park or the ball's park is unknown (a park absent from the
        table reads 0)."""
        if not self.carry_offset or venue_live is None:
            return 0.0
        vr = born.get("venue")
        if vr is None:
            return 0.0
        offs = self._carry_offsets()
        if not offs:
            return 0.0
        return offs.get(int(venue_live), 0.0) - offs.get(int(vr), 0.0)

    def carry_of(self, born: dict, venue_live: int | None = None) -> float | None:
        """The born ball's carry: its own reported distance, else the bundle's
        carry model on its exit velocity and launch angle, else None. With
        ``venue_live`` given, the carry in that park's air — the offset
        difference of :meth:`carry_shift` added; a ball whose
        ``venue`` IS the live park shifts by exactly 0."""
        d = born.get("dist")
        carry: float | None = None
        if d is not None and np.isfinite(float(d)) and float(d) > 0.0:
            carry = float(d)
        else:
            pg = getattr(self.a, "park_geometry", None) or {}
            coef = (pg.get("carry") or {}).get("coef")
            ev, la = born.get("ev"), born.get("la")
            if coef and ev is not None and la is not None and float(ev) > 0.0:
                carry = carry_predict(coef, float(ev), float(la))
        if carry is None:
            return None
        return carry + self.carry_shift(born, venue_live)

    def _born_in_live_air(self, born_bb: dict | None, venue_live: int | None) -> dict | None:
        """The born ball re-expressed in the live park's air: a
        working copy whose ``dist`` is :meth:`carry_of` with the shift and
        whose ``venue`` is the live park. The ball itself (no copy) when the
        shift cannot apply: the flag off, no live park, no ``venue`` on the
        ball, a document without the offset table, or no carry at all."""
        if born_bb is None or not self.carry_offset or venue_live is None:
            return born_bb
        if born_bb.get("venue") is None or not self._carry_offsets():
            return born_bb
        carry = self.carry_of(born_bb, venue_live)
        if carry is None:
            return born_bb
        born = dict(born_bb)
        born["dist"] = carry
        born["venue"] = int(venue_live)
        return born

    def fence_decision(self, born: dict, venue_id: int | None, season: int | None = None) -> int:
        """0 = over the fence (a home run), 1 = short of it (no home run),
        2 = inside the band (the draw decides), 3 = the stage passes. The
        season picks the park's fence group. The carry is read in the live
        park's air; a copy from :meth:`_born_in_live_air` already is, and
        shifts by 0."""
        cls = born.get("cls")
        if cls is None or int(cls) <= 0:
            return 3
        if int(cls) not in (2, 3):
            return 1  # a ground ball, popup or bunt never leaves the park
        if venue_id is None:
            return 3
        fence = self.fence_at(venue_id, born.get("spray_raw"), season)
        carry = self.carry_of(born, venue_id)
        if fence is None or carry is None:
            return 3
        if carry >= fence + float(self.fence_margin):
            return 0
        if carry <= fence - float(self.fence_margin):
            return 1
        return 2

    def _bb_hr_mask(self, hand: str) -> np.ndarray:
        m = self._bb_hr_cache.get(hand)
        if m is None:
            m = np.asarray(self.a.bb_pools[hand].result_hits) == 4
            self._bb_hr_cache[hand] = m
        return m

    def _fence_rows(
        self,
        hand: str,
        meta: dict,
        cell: tuple[int, int],
        rows: np.ndarray,
        born: dict,
        venue_id: int | None,
        season: int | None = None,
    ) -> np.ndarray:
        """Apply the fence decision to the candidate rows: home-run rows only
        (the whole cell's when the class-filtered rows hold none), the rows
        without a home run, or the rows as they are. A ball the class rule
        called short (not an air ball) counts in [5] as well as [1]."""
        dec = self.fence_decision(born, venue_id, season)
        self._fence_last_dec = dec
        self.fence_counts[dec] += 1
        if dec == 1 and int(born.get("cls")) not in (2, 3):
            self.fence_counts[5] += 1
        if dec not in (0, 1):
            return rows
        hr = self._bb_hr_mask(hand)
        if dec == 0:
            sub = rows[hr[rows]]
            if sub.size == 0:
                cell_rows = meta["cells"][cell]
                sub = cell_rows[hr[cell_rows]]
        else:
            sub = rows[~hr[rows]]
        if sub.size == 0:
            self.fence_counts[4] += 1
            return rows
        return sub

    # ---- the wall-margin band --------------------------------------------
    def _bb_margins(self, hand: str) -> np.ndarray:
        """Every pool row's margin to its OWN wall, float32 per row. The
        margin is the row's distance minus its park's fence at its spray and
        season, read from the geometry document. The row's season picks the
        park's fence group. A park or sector the document lacks reads the
        league line, as :meth:`fence_at` does. NaN where the venue, the
        spray, the distance or the fence is missing. The array is computed
        once per hand at first use and cached against the document object.
        A rebuilt document recomputes it, so the margins always follow the
        lines. The bundle never stores them."""
        pg = getattr(self.a, "park_geometry", None)
        if self._bb_margin_doc is not pg:
            self._bb_margin_cache = {}
            self._bb_margin_doc = pg
        m = self._bb_margin_cache.get(hand)
        if m is not None:
            return m
        pool = self.a.bb_pools[hand]
        out = np.full(pool.n, np.nan, dtype=np.float32)
        n_sec = int(pg.get("n_sectors", 0)) if pg else 0
        if (
            pg
            and n_sec > 0
            and pool.venue_id is not None
            and pool.spray_raw is not None
            and pool.hit_dist is not None
        ):
            venue = np.asarray(pool.venue_id, dtype=np.int64)
            spray = np.asarray(pool.spray_raw, dtype=np.float64)
            dist = np.asarray(pool.hit_dist, dtype=np.float64)
            season = np.asarray(pool.season, dtype=np.int64)
            ok = (venue > 0) & np.isfinite(spray) & np.isfinite(dist) & (dist > 0.0)
            s_idx = (spray - float(pg.get("spray_min", -45.0))) / float(pg.get("sector_deg", 10))
            sec = np.clip(np.floor(np.where(ok, s_idx, 0.0)).astype(np.int64), 0, n_sec - 1)
            league = np.full(n_sec, np.nan, dtype=np.float64)
            for i, f in enumerate(list(pg.get("league") or [])[:n_sec]):
                league[i] = float(f) if f is not None else np.nan
            fence = np.full(pool.n, np.nan, dtype=np.float64)
            keys = np.stack([venue, season], axis=1)[ok]
            pairs = np.unique(keys, axis=0) if keys.size else np.zeros((0, 2), dtype=np.int64)
            for v, y in pairs:
                sel = ok & (venue == v) & (season == y)
                line = self._fence_line(pg, int(v), int(y))
                arr = league.copy()
                if line is not None:
                    for i, f in enumerate(list(line)[:n_sec]):
                        if f is not None:
                            arr[i] = float(f)
                fence[sel] = arr[sec[sel]]
            good = ok & np.isfinite(fence)
            out[good] = (dist[good] - fence[good]).astype(np.float32)
        self._bb_margin_cache[hand] = out
        return out

    def _bb_margin_rows(self, hand: str, rows: np.ndarray, born_margin: float) -> np.ndarray:
        """The candidate rows whose margin to their own wall sits within
        :attr:`bb_margin_band` feet of the born ball's; the rows as they were
        (counted as the fallback) when fewer than :attr:`bb_margin_min_rows`
        survive. A row with no margin (NaN) never matches."""
        m = self._bb_margins(hand)[rows]
        with np.errstate(invalid="ignore"):
            keep = np.abs(m - np.float32(born_margin)) <= np.float32(self.bb_margin_band)
        n_keep = int(np.count_nonzero(keep))
        if n_keep < int(self.bb_margin_min_rows):
            self.bb_margin_counts[1] += 1
            return rows
        self.bb_margin_counts[0] += 1
        return rows[keep]

    def _bb_margin_stage(
        self,
        hand: str,
        rows: np.ndarray,
        born: dict,
        venue_id: int | None,
        season: int | None,
    ) -> np.ndarray:
        """Apply the band after the class filter and the fence stage: only for
        a born AIR ball IN THE WALL ZONE (a line drive or a fly ball carrying
        :attr:`wall_zone_distance` feet or more in the live air: the band is
        for the wall play, not every fly ball) that the fence did NOT
        call over (short, the band or passed; the stage's own decision when it
        ran, else a fresh read without a counter) whose margin to the live
        wall (its carry in the live air minus ``fence_at``) is finite.
        Everything else is counted as skipped and the rows stay. A ball short
        of the wall zone keeps the whole neighbourhood: for it the band would
        only select parks by fence depth, which is not the wall play."""
        cls = born.get("cls")
        if cls is None or int(cls) not in (2, 3) or venue_id is None:
            self.bb_margin_counts[2] += 1
            return rows
        if not self.in_wall_zone(born):
            self.bb_margin_counts[2] += 1
            return rows
        dec = self._fence_last_dec
        if dec is None:
            dec = self.fence_decision(born, venue_id, season)
        if dec == 0:
            self.bb_margin_counts[2] += 1
            return rows
        fence = self.fence_at(venue_id, born.get("spray_raw"), season)
        carry = self.carry_of(born, venue_id)
        if fence is None or carry is None or not np.isfinite(carry - fence):
            self.bb_margin_counts[2] += 1
            return rows
        return self._bb_margin_rows(hand, rows, float(carry - fence))
