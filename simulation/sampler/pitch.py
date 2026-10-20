"""The pitch draw.

The weight of a pool row factorizes: the pitcher factor times recency (fixed
for a half-inning), times the batter factor and the base-out situation kernel
(fixed for a plate appearance), times the gated extras (fatigue, the batting
side, the catcher receiving ratio). The weight is split into twelve
count-bucket CDFs once per plate appearance, and each pitch is one
``searchsorted`` draw in the live count's bucket. With the cell index on, the
weight is assembled over the plate appearance's hard-filter cell only.
"""

from __future__ import annotations

import numpy as np

from simulation.filter_cells import (
    DEFAULT_MIN_CELL,
    N_BAND,
    N_BASE,
    N_COUNT,
    N_OUTS,
    score_band,
    score_band_array,
)
from simulation.sampler.base import BaseMixin
from simulation.sampler.kernels import (
    _raise_neutral,
    _repower_neutral,
)


class PitchMixin(BaseMixin):
    """The sampler's pitch-draw methods."""

    def _init_pitch(self) -> None:
        """The pitch draw's live matchup, its weights and the cell index."""
        #: The live matchup: the batter hand, the half-inning base weight (the
        #: pitcher factor times recency, cutoff applied) and the per-PA
        #: count-bucket CDFs.
        self._hand: str | None = None
        self._base: np.ndarray | None = None
        self._bucket_cdf: list | None = None
        #: The half-inning's pitcher factor; the result draw re-raises it.
        self._f_pitcher_vec: np.ndarray | None = None
        #: The pitch draw's powers on the pitcher and batter factors (1.0 =
        #: the raw score). ``result_pitcher_power`` is absolute: the result
        #: draw re-raises from ``pitch_pitcher_power`` to it.
        self.pitch_pitcher_power = 1.0
        self.pitch_batter_power = 1.0
        #: The per-matchup neutral values the result draw re-raises an
        #: unscored row by.
        self._pitcher_neutral_adj = 1.0
        self._bat_neutral_rb = 1.0
        #: The last RESULT row (the play; its got-away and born-ball reads) and
        #: the last PITCH-draw row (the pitch thrown), as global pitch-pool
        #: indices. The two are equal while the split is off; None before any
        #: draw or after an empty-bucket fallback.
        self._pp_last_i: int | None = None
        self._pp_pitch_i: int | None = None
        #: The conditioning weights, each off by default (a 0.0
        #: sigma or a 1.0 weight runs no multiplication). Fatigue: Gaussians on
        #: |live - row| pitch count and times through the order, normalized to
        #: a mean of 1 within each count bucket; a row with an unknown value is
        #: neutral. The batting side: a row whose side mismatches the live one
        #: is multiplied by ``pitch_home_off_weight`` (0.0 = a hard match).
        self.fatigue_pc_sigma = 0.0
        self.fatigue_tto_sigma = 0.0
        self.pitch_home_off_weight = 1.0
        #: The pitch-draw cell index. Off, the per-PA weight covers
        #: the whole pool. On, each plate appearance selects its hard-filter
        #: cell -- (runners, outs, score band, batting side) -- and assembles
        #: the same weights over that cell's twelve count sub-cells only. A
        #: sub-cell under ``pitch_min_cell`` rows widens in a fixed order (band,
        #: then side, then count); ``widen_counts`` counts the draws per level.
        #: A pool without ``bat_home`` has no side dimension.
        self.pitch_cell_index = False
        self.pitch_min_cell = DEFAULT_MIN_CELL
        self._cell_cache: dict[str, dict] = {}
        #: The current PA's per-count sub-cell rows, widening levels and keys
        #: (the cell path only; None on the whole-pool path).
        self._pa_rows: list[np.ndarray] | None = None
        self._pa_levels: list[int] | None = None
        self._pa_keys: list[tuple] | None = None
        #: Draws per widening level 0..3.
        self.widen_counts = np.zeros(4, dtype=np.int64)
        #: Per-count raw per-PA weights and pitcher / batter factors, kept only
        #: while the split is on (the result draw re-weights them).
        self._bucket_w: list[np.ndarray | None] | None = None
        self._bucket_fp: list[np.ndarray | None] | None = None
        self._bucket_fb: list[np.ndarray | None] | None = None

    # ---- the factors -----------------------------------------------------
    def _f_pitcher(self, hand: str, pitcher_key: str) -> np.ndarray:
        """The pitcher factor per pool row: the live pitcher's score against
        each row's pitcher, from the pitcher-sim row; 1.0 for a row whose
        pitcher has no profile, and on every row when the live pitcher has
        none."""
        meta = self._pool_meta(hand)
        prof_score = self._pitcher_prof_score(pitcher_key)
        if prof_score is None:
            return np.ones(meta["pool"].n, dtype=np.float32)
        n_prof = len(prof_score)
        pp = meta["pool_prof"]
        out = np.where(pp >= 0, prof_score[np.clip(pp, 0, n_prof - 1)], np.float32(1.0))
        return out.astype(np.float32)

    def _f_batter(self, hand: str, batter_key: str) -> np.ndarray:
        """The batter factor per pool row: the batter engine's score matrix
        row, at the batter power; ones on a bundle without the matrix."""
        meta = self._pool_meta(hand)
        f = self._matrix_gather("batter", batter_key, meta["pool_bat"])
        if f is not None:
            return f
        return np.ones(meta["pool"].n, dtype=np.float32)

    def _f_situation_baseout(self, hand: str, base_out: np.ndarray) -> np.ndarray:
        """The situation kernel: a Gaussian over the base-out columns only
        (outs, runners, inning, score margin), bandwidth ``sit_sigma``. The
        count is the per-pitch bucket, not this factor."""
        s = self._pool_meta(hand)["sit_baseout"]  # contiguous, cached
        diff: np.ndarray = s - np.asarray(base_out, dtype=np.float32)
        d2 = np.einsum("ij,ij->i", diff, diff)
        return np.exp(-d2 / (2.0 * self.sit_sigma**2 * s.shape[1])).astype(np.float32)

    # ---- the matchup lifecycle -------------------------------------------
    def new_half_inning(self, hand: str, pitcher_key: str, catcher_key: str | None = None) -> None:
        """Cache the half-inning base weight (the pitcher factor at the pitch
        power, times recency, the cutoff applied) and stage the fielding
        catcher for the receiving ratio."""
        self._hand = hand
        self._catcher_key = catcher_key
        pool = self.a.pools[hand]
        f_pitcher = self._f_pitcher(hand, pitcher_key)
        self._f_pitcher_vec = f_pitcher  # the result draw re-raises it
        # The pitch draw's pitcher POWER (1.0 = the raw score). At any other
        # power an unscored row weighs the mean scored weight (draw-neutral),
        # and the result draw re-raises such a row by the ratio of the two
        # neutral means.
        p = float(self.pitch_pitcher_power)
        q = float(self.result_pitcher_power)
        self._pitcher_neutral_adj = 1.0
        if p != 1.0:
            uns = self._pool_meta(hand)["prof_unscored"]
            mean_p = self._neutral_mean_pitcher(hand, pitcher_key, p)
            f_pitcher = _raise_neutral(f_pitcher, uns, p, mean_p)
            if q != p and mean_p > 0.0:
                self._pitcher_neutral_adj = (
                    self._neutral_mean_pitcher(hand, pitcher_key, q) / mean_p
                )
        elif q != 1.0:
            self._pitcher_neutral_adj = self._neutral_mean_pitcher(hand, pitcher_key, q)
        # The cutoff rides on the half-inning base, so BOTH pitch paths (the
        # cell index and the plain bucket draw) inherit it from the one place
        # they share.
        self._base = self._apply_asof((f_pitcher * pool.recency).astype(np.float32), pool)

    def new_plate_appearance(
        self,
        batter_key: str,
        base_out: np.ndarray,
        *,
        pitch_count: int | None = None,
        tto: int | None = None,
        bat_home: bool | None = None,
    ) -> None:
        """Assemble the plate appearance's weight (the base, times the batter
        factor and the base-out situation kernel, times the gated fatigue,
        batting-side and receiving weights) and split it into twelve
        count-bucket CDFs for the per-pitch draw.

        ``pitch_count`` (pitches the live pitcher threw BEFORE this plate
        appearance) and ``tto`` (his times through the order entering it) feed
        the fatigue weight; ``bat_home`` feeds the batting-side weight. Each is
        read only when its sigma or weight is on and the pool carries the
        column; omitted or off, it changes nothing."""
        assert self._hand is not None and self._base is not None, "call new_half_inning first"
        if self.pitch_cell_index:
            self._new_plate_appearance_cell(
                batter_key, base_out, pitch_count=pitch_count, tto=tto, bat_home=bat_home
            )
            return
        self._pa_rows = None
        self._pa_levels = None
        self._pa_keys = None
        f_bat = self._f_batter(self._hand, batter_key)
        # The batter powers, with the draw-neutral rule.
        uns_b = self._batter_unscored(self._hand)
        pb_power = float(self.pitch_batter_power)
        mean_pb = self._neutral_mean_batter(self._hand, batter_key, pb_power)
        self._bat_neutral_rb = self._neutral_mean_batter(
            self._hand, batter_key, float(self.result_batter_power)
        )
        w = self._base * f_bat * self._f_situation_baseout(self._hand, base_out)
        # The receiving ratio: applied per count bucket below (the single
        # draw), or kept for the result draw (the split).
        recv = (
            self._recv_factor(self._hand, self._catcher_key)
            if self._receiving_on() and self._catcher_key is not None
            else None
        )
        # --- the conditioning weights (each absent when off) ---------------
        if (self.fatigue_pc_sigma > 0.0 or self.fatigue_tto_sigma > 0.0) and (
            pitch_count is not None or tto is not None
        ):
            f_fat = self._f_fatigue(self._hand, pitch_count, tto)
            if f_fat is not None:
                w = w * f_fat
        if bat_home is not None and self.pitch_home_off_weight != 1.0:
            bh = getattr(self.a.pools[self._hand], "bat_home", None)
            if bh is not None:
                # An unknown-side row (-1) stays neutral; a mismatch × the weight.
                mismatch = (bh >= 0) & ((bh > 0) != bool(bat_home))
                w = w * np.where(mismatch, np.float32(self.pitch_home_off_weight), np.float32(1.0))
        rows = self._pool_meta(self._hand)["bucket_rows"]
        # The split keeps the raw weight per count for the result draw; off,
        # nothing is kept.
        if self.pitch_result_split:
            fpv = self._f_pitcher_vec
            self._bucket_w = [(w[r] if r.size else None) for r in rows]
            self._bucket_fb = [(f_bat[r] if r.size else None) for r in rows]
            self._bucket_fp = [(fpv[r] if (r.size and fpv is not None) else None) for r in rows]
            w = _repower_neutral(w, f_bat, uns_b, pb_power, mean_pb)
            self._bucket_recv = (
                [(recv[r] if r.size else None) for r in rows] if recv is not None else None
            )
        else:
            self._bucket_w = self._bucket_fb = self._bucket_fp = None
            self._bucket_recv = None
            # The single draw's batter power (1.0 = as is).
            w = _repower_neutral(w, f_bat, uns_b, pb_power, mean_pb)
            if recv is not None:
                taken = self._pool_meta(self._hand)["taken"]
                for r in rows:
                    if r.size:
                        w[r] = self._apply_receiving(w[r], taken[r], recv[r])
        self._bucket_cdf = [(np.cumsum(w[r], dtype=np.float64) if r.size else None) for r in rows]

    # ---- the cell index --------------------------------------------------
    def _cell_meta(self, hand: str) -> dict:
        """Per-hand one-time precompute of the cell index: every row's sub-cell
        id — ((runners, outs, band, side) × count) — a stable row order sorted
        by it, and the offsets that make each sub-cell one contiguous slice.
        The side axis has THREE values when the pool carries ``bat_home``
        (away / home / unknown, so an unknown-side row joins no live side's
        cell but every side union) and ONE when it does not (a pre-0023
        bundle). ~N int32 + a small offsets array; ~0.2 s per hand."""
        meta = self._cell_cache.get(hand)
        if meta is not None:
            return meta
        pool = self.a.pools[hand]
        sit = pool.sit
        rs = sit[:, 3].astype(np.int64) & 0b111
        outs = np.clip(sit[:, 2].astype(np.int64), 0, N_OUTS - 1)
        cb = np.clip(sit[:, 0].astype(np.int64), 0, 3) * 3 + np.clip(
            sit[:, 1].astype(np.int64), 0, 2
        )
        band = score_band_array(sit[:, 5])
        bh = getattr(pool, "bat_home", None)
        # A column that is UNKNOWN on every row (a migration-0023 pool exported
        # before its rebuild filled it) has no side dimension either: every
        # live side's cell would be empty and every draw would widen past the
        # score band. Treat it exactly like an absent column.
        if bh is None or not bool((bh >= 0).any()):
            n_side = 1
            side = np.zeros(pool.n, dtype=np.int64)
        else:
            n_side = 3
            side = np.where(bh > 0, 1, np.where(bh == 0, 0, 2)).astype(np.int64)
        sub = (((rs * N_OUTS + outs) * N_BAND + band) * n_side + side) * N_COUNT + cb
        n_sub = N_BASE * N_OUTS * N_BAND * n_side * N_COUNT
        order = np.argsort(sub, kind="stable").astype(np.int32)
        offsets = np.zeros(n_sub + 1, dtype=np.int64)
        offsets[1:] = np.cumsum(np.bincount(sub, minlength=n_sub))
        meta = {"n_side": n_side, "order": order, "offsets": offsets, "widened": {}}
        self._cell_cache[hand] = meta
        return meta

    def _subcell_rows(
        self, hand: str, rs: int, outs: int, band: int, side: int | None, cb: int
    ) -> tuple[np.ndarray, int]:
        """The rows of one count sub-cell of the live PA cell, WIDENED by
        decision #19's fixed ladder when the sub-cell holds fewer than
        ``pitch_min_cell`` rows: level 1 unions the five score bands, level 2
        the batting sides too, level 3 the twelve counts too. ``side`` None
        means the live side is unknown — level 0 already unions the sides.
        Returns ``(rows, level)``; cached per sub-cell (occupancy is a pool
        fact, not a live-state fact). An empty level-3 cell raises: the
        base-out cell is essential and never widens away."""
        meta = self._cell_meta(hand)
        n_side = meta["n_side"]
        order, off = meta["order"], meta["offsets"]
        key = (rs, outs, band, side, cb)
        cached = meta["widened"].get(key)
        if cached is not None:
            return cached

        def sl(b: int, s: int, c: int) -> np.ndarray:
            sub = (((rs * N_OUTS + outs) * N_BAND + b) * n_side + s) * N_COUNT + c
            return order[off[sub] : off[sub + 1]]

        sides = [side] if side is not None else list(range(n_side))
        need = max(int(self.pitch_min_cell), 1)
        parts = [sl(band, s, cb) for s in sides]
        rows = parts[0] if len(parts) == 1 else np.concatenate(parts)
        level = 0
        if rows.size < need:
            rows = np.concatenate([sl(b, s, cb) for b in range(N_BAND) for s in sides])
            level = 1
            if rows.size < need:
                rows = np.concatenate([sl(b, s, cb) for b in range(N_BAND) for s in range(n_side)])
                level = 2
                if rows.size < need:
                    rows = np.concatenate(
                        [
                            sl(b, s, c)
                            for b in range(N_BAND)
                            for s in range(n_side)
                            for c in range(N_COUNT)
                        ]
                    )
                    level = 3
                    if rows.size == 0:
                        raise RuntimeError(
                            f"SIM-467: base-out cell (runners_state={rs}, outs={outs}) is "
                            f"EMPTY in the {hand}-hand pitch pool — a data defect. The "
                            "base-out cell is essential and the ladder ends there; rebuild "
                            "the pool and investigate."
                        )
        result = (rows, level)
        meta["widened"][key] = result
        return result

    def _new_plate_appearance_cell(
        self,
        batter_key: str,
        base_out: np.ndarray,
        *,
        pitch_count: int | None,
        tto: int | None,
        bat_home: bool | None,
    ) -> None:
        """The cell path of :meth:`new_plate_appearance`: the same
        factors as the whole-pool path, evaluated on the live cell's rows only
        and multiplied in the same order, so every in-cell weight is bit-
        identical to the whole-pool path's weight for that row."""
        hand = self._hand
        assert hand is not None and self._base is not None
        meta = self._pool_meta(hand)
        cmeta = self._cell_meta(hand)
        bo = np.asarray(base_out, dtype=np.float32)
        rs = int(bo[1]) & 0b111
        outs = min(max(int(bo[0]), 0), N_OUTS - 1)
        band = score_band(int(bo[3]))
        side: int | None
        if cmeta["n_side"] == 1 or bat_home is None:
            side = None if cmeta["n_side"] > 1 else 0
        else:
            side = 1 if bat_home else 0
        base = self._base
        sitb = meta["sit_baseout"]
        # The batter factor per sub-cell (the matrix when on), its powers and
        # the draw-neutral rule's ingredients.
        uns_b = self._batter_unscored(hand)
        pb_power = float(self.pitch_batter_power)
        mean_pb = self._neutral_mean_batter(hand, batter_key, pb_power)
        self._bat_neutral_rb = self._neutral_mean_batter(
            hand, batter_key, float(self.result_batter_power)
        )
        # The receiving ratio: the single draw applies it per sub-cell; the
        # split keeps it for the result draw.
        recv: np.ndarray | None = (
            self._recv_factor(hand, self._catcher_key)
            if self._receiving_on() and self._catcher_key is not None
            else None
        )
        taken_all = meta["taken"]
        fatigue_on = (self.fatigue_pc_sigma > 0.0 or self.fatigue_tto_sigma > 0.0) and (
            pitch_count is not None or tto is not None
        )
        bh = getattr(self.a.pools[hand], "bat_home", None)
        home_on = bat_home is not None and self.pitch_home_off_weight != 1.0 and bh is not None
        pa_rows: list[np.ndarray] = []
        pa_levels: list[int] = []
        pa_keys: list[tuple] = []
        cdfs: list[np.ndarray | None] = []
        split = self.pitch_result_split
        fpv = self._f_pitcher_vec
        b_w: list[np.ndarray | None] = []
        b_fb: list[np.ndarray | None] = []
        b_fp: list[np.ndarray | None] = []
        b_recv: list[np.ndarray | None] = []
        for cb in range(N_COUNT):
            rows, level = self._subcell_rows(hand, rs, outs, band, side, cb)
            pa_rows.append(rows)
            pa_levels.append(level)
            pa_keys.append((rs, outs, band, -1 if side is None else side, cb))
            if rows.size == 0:
                cdfs.append(None)
                b_w.append(None)
                b_fb.append(None)
                b_fp.append(None)
                b_recv.append(None)
                continue
            f_bat = self._batter_factor_rows(hand, batter_key, rows)
            diff: np.ndarray = sitb[rows] - bo
            d2 = np.einsum("ij,ij->i", diff, diff)
            f_sit = np.exp(-d2 / (2.0 * self.sit_sigma**2 * sitb.shape[1])).astype(np.float32)
            w = base[rows] * f_bat * f_sit
            if fatigue_on:
                f_fat = self._fatigue_rows(hand, pitch_count, tto, rows)
                if f_fat is not None:
                    w = w * f_fat
            if home_on:
                assert bh is not None and bat_home is not None
                bhr = bh[rows]
                mismatch = (bhr >= 0) & ((bhr > 0) != bool(bat_home))
                w = w * np.where(mismatch, np.float32(self.pitch_home_off_weight), np.float32(1.0))
            if split:
                # Keep the raw weight and factors for the result draw; the
                # pitch draw's batter power applies here only.
                b_w.append(w)
                b_fb.append(f_bat)
                b_fp.append(fpv[rows] if fpv is not None else None)
                b_recv.append(recv[rows] if recv is not None else None)
                w = _repower_neutral(w, f_bat, uns_b[rows], pb_power, mean_pb)
            else:
                # The single draw's batter power (1.0 = as is).
                w = _repower_neutral(w, f_bat, uns_b[rows], pb_power, mean_pb)
                if recv is not None:
                    # The receiving ratio, last, mass-preserving.
                    w = self._apply_receiving(w, taken_all[rows], recv[rows])
            cdfs.append(np.cumsum(w, dtype=np.float64))
        self._pa_rows = pa_rows
        self._pa_levels = pa_levels
        self._pa_keys = pa_keys
        self._bucket_cdf = cdfs
        if split:
            self._bucket_w, self._bucket_fb, self._bucket_fp = b_w, b_fb, b_fp
            self._bucket_recv = b_recv if recv is not None else None
        else:
            self._bucket_w = self._bucket_fb = self._bucket_fp = None
            self._bucket_recv = None

    def _fatigue_rows(
        self, hand: str, pitch_count: int | None, tto: int | None, rows: np.ndarray
    ) -> np.ndarray | None:
        """:meth:`_f_fatigue` restricted to ``rows`` (one count sub-cell of the
        live cell): the same Gaussians, normalized to a mean of 1 over the
        sub-cell's valid rows; unknown rows exactly neutral."""
        pool = self.a.pools[hand]
        pc_col = getattr(pool, "pitch_count", None)
        tto_col = getattr(pool, "tto", None)
        out = np.ones(rows.size, dtype=np.float32)
        valid = np.zeros(rows.size, dtype=bool)
        touched = False
        if self.fatigue_pc_sigma > 0.0 and pitch_count is not None and pc_col is not None:
            pcr = pc_col[rows]
            ok = pcr >= 0
            d = pcr.astype(np.float32) - np.float32(pitch_count)
            f = np.exp(-(d * d) / (2.0 * self.fatigue_pc_sigma**2)).astype(np.float32)
            out = np.where(ok, out * f, out).astype(np.float32)
            valid |= ok
            touched = True
        if self.fatigue_tto_sigma > 0.0 and tto is not None and tto_col is not None:
            ttr = tto_col[rows]
            ok = ttr > 0
            d = ttr.astype(np.float32) - np.float32(tto)
            f = np.exp(-(d * d) / (2.0 * self.fatigue_tto_sigma**2)).astype(np.float32)
            out = np.where(ok, out * f, out).astype(np.float32)
            valid |= ok
            touched = True
        if not touched:
            return None
        if valid.any():
            mean_w = float(out[valid].mean())
            out[valid] = out[valid] / np.float32(mean_w) if mean_w > 0.0 else np.float32(1.0)
        return out

    def cell_index_stats(self) -> dict:
        """Draws per widening level (0 = the exact cell) and the index shape
        per hand, for the lane's report."""
        return {
            "enabled": bool(self.pitch_cell_index),
            "min_cell": int(self.pitch_min_cell),
            "draws_by_level": [int(x) for x in self.widen_counts],
            "n_side": {h: int(m["n_side"]) for h, m in self._cell_cache.items()},
        }

    def _f_fatigue(self, hand: str, pitch_count: int | None, tto: int | None) -> np.ndarray | None:
        """The per-row fatigue factor for the live pitcher's
        pitch count and times through the order — a Gaussian on each |live −
        row| distance (a sigma of 0.0 removes that term), NORMALIZED to a mean
        of 1 within each COUNT BUCKET over the rows that carry a value. A row
        with an unknown value (pitch count -1 / times-through 0, a pre-rebuild
        row) is exactly neutral. None when the pool carries neither column."""
        pool = self.a.pools[hand]
        pc_col = getattr(pool, "pitch_count", None)
        tto_col = getattr(pool, "tto", None)
        out = np.ones(pool.n, dtype=np.float32)
        valid = np.zeros(pool.n, dtype=bool)
        touched = False
        if self.fatigue_pc_sigma > 0.0 and pitch_count is not None and pc_col is not None:
            ok = pc_col >= 0
            d = pc_col.astype(np.float32) - np.float32(pitch_count)
            f = np.exp(-(d * d) / (2.0 * self.fatigue_pc_sigma**2)).astype(np.float32)
            out = np.where(ok, out * f, out).astype(np.float32)
            valid |= ok
            touched = True
        if self.fatigue_tto_sigma > 0.0 and tto is not None and tto_col is not None:
            ok = tto_col > 0
            d = tto_col.astype(np.float32) - np.float32(tto)
            f = np.exp(-(d * d) / (2.0 * self.fatigue_tto_sigma**2)).astype(np.float32)
            out = np.where(ok, out * f, out).astype(np.float32)
            valid |= ok
            touched = True
        if not touched:
            return None
        # Per-COUNT-BUCKET normalization over the valid rows: the factor
        # shifts WHICH pitch is drawn at a count, never how often a count's
        # pitches are drawn; a fully underflowed bucket goes neutral rather
        # than starving.
        for r in self._pool_meta(hand)["bucket_rows"]:
            if r.size == 0:
                continue
            m = valid[r]
            if not m.any():
                continue
            sel = r[m]
            mean_w = float(out[sel].mean())
            if mean_w > 0.0:
                out[sel] = out[sel] / np.float32(mean_w)
            else:
                out[sel] = np.float32(1.0)
        return out

    def draw(self, balls: int = 0, strikes: int = 0) -> str:
        """Draw one pitch outcome in the live count's bucket, by the plate
        appearance's weights. With the split on, the result draw then picks
        the play among the same rows. An empty bucket returns ``"ball"``."""
        assert self._bucket_cdf is not None, "call new_plate_appearance first"
        meta = self._pool_cache[self._hand]
        b = min(max(int(balls), 0), 3) * 3 + min(max(int(strikes), 0), 2)
        cdf = self._bucket_cdf[b]
        # On the cell path the bucket's rows are the live cell's sub-cell
        # (global indices, so the got-away and geometry reads work the same);
        # count the draw's widening level.
        if self._pa_rows is not None:
            rows = self._pa_rows[b]
            if self._pa_levels is not None:
                self.widen_counts[self._pa_levels[b]] += 1
        else:
            rows = meta["bucket_rows"][b]
        if cdf is None or cdf[-1] <= 0:
            self._pp_last_i = None
            self._pp_pitch_i = None
            return "ball"
        i = int(np.searchsorted(cdf, self.rng.random() * cdf[-1]))
        gi = int(rows[min(i, rows.size - 1)])
        self._pp_pitch_i = gi  # the pitch thrown
        if self.pitch_result_split and self._bucket_w is not None:
            gi = self._result_draw(b, rows, gi)
        self._pp_last_i = gi  # the RESULT row: the got-away / born-ball reads
        return str(meta["outcome"][gi])
