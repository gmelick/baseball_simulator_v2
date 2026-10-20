"""The pitch-result draw, the catcher receiving ratio and the readers of the
last drawn pitch.

With the split on, the pitch draw picks the pitch thrown and the result draw
picks the play among the same candidate rows, weighted toward rows whose pitch
resembles the drawn one. The receiving ratio moves weight between balls and
called strikes on taken pitches and keeps the taken total unchanged.
"""

from __future__ import annotations

import numpy as np

from pipeline.batch.artifacts.receiving import recv_block_cell, recv_zone_group
from simulation.sampler.base import BaseMixin
from simulation.sampler.kernels import (
    _PITCH_FEATURE_WEIGHTS,
    _repower_neutral,
)


class PitchResultMixin(BaseMixin):
    """The sampler's pitch-result and receiving methods."""

    def _init_pitch_result(self) -> None:
        """The split's switches and bandwidths and the receiving ratio."""
        #: The pitch / pitch-result split (``SIM_PITCH_RESULT_SPLIT``). Off,
        #: one draw picks the pitch and its result. On, the result draw weighs
        #: the pitch draw's candidate rows by the pitch engine's similarity to
        #: the drawn pitch (bandwidth ``result_pitch_sigma``; 0 = the result is
        #: the pitch row), re-raises the pitcher and batter factors to their
        #: result powers, and divides by each row's local density under the
        #: same kernel (``result_density_power``; 1.0 = the full correction,
        #: 0 = off). The density correction keeps the split's marginals on the
        #: single draw's at neutral powers: without it the ball share fell 7%
        #: and walks 37% (the probe of 2026-09-08).
        self.pitch_result_split = False
        self.result_pitch_sigma = 1.0
        self.result_pitcher_power = 1.0
        self.result_batter_power = 1.0
        self.result_density_power = 1.0
        #: Per-hand (mean, std, complete-row mask) of the pitch pool's geometry.
        self._pp_geom_stats: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        #: The catcher receiving ratio (``SIM_CATCHER_RECEIVING``), the last
        #: factor of the pitch-result draw, on taken pitches only. A called
        #: strike times the live catcher's framing multiplier at its zone
        #: group, a ball times its mirror, a got-away times his blocking ratio
        #: and the other taken rows times its mirror; the taken group is then
        #: rescaled to its old total, so the ratio never moves the swing-or-take
        #: split. Off by default; fitting and enabling it is open work (SIM-526).
        self.catcher_receiving = False
        #: (hand, catcher key) -> the per-row ratio, or None.
        self._recv_cache: dict[tuple[str, str], np.ndarray | None] = {}
        #: The per-count receiving factors kept for the result draw.
        self._bucket_recv: list[np.ndarray | None] | None = None
        #: The fielding catcher's key, staged by ``new_half_inning``.
        self._catcher_key: str | None = None

    # ---- the pitch-result draw -------------------------------------------
    def _pp_geom_z_stats(self, hand: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Per-column mean / std of the pitch pool's geometry over its
        complete rows (all finite, a real velocity), plus that row mask.
        Cached per hand; one pass over the pool."""
        cached = self._pp_geom_stats.get(hand)
        if cached is not None:
            return cached
        g = self.a.pools[hand].geom
        valid = np.isfinite(g).all(axis=1) & (g[:, 0] > 0.0)
        if valid.any():
            mean = g[valid].mean(axis=0).astype(np.float32)
            std = g[valid].std(axis=0).astype(np.float32)
        else:
            mean = np.zeros(g.shape[1], dtype=np.float32)
            std = np.ones(g.shape[1], dtype=np.float32)
        std = np.where(std > 1e-6, std, np.float32(1.0)).astype(np.float32)
        stats = (mean, std, valid)
        self._pp_geom_stats[hand] = stats
        return stats

    def _f_result_pitch(self, hand: str, rows: np.ndarray, pitch_gi: int) -> np.ndarray | None:
        """The pitch-to-pitch factor of every candidate row against the drawn
        pitch: a Gaussian on the pitch engine's own metric (z-scored geometry,
        each dimension weighted by the engine's feature weight), bandwidth
        ``result_pitch_sigma`` in per-feature standard deviations, normalized
        to a mean of 1 over the rows with complete geometry (an incomplete row
        is exactly neutral). None when the drawn pitch itself has no complete
        geometry or the bandwidth is 0 — the result is then the pitch row."""
        sigma = float(self.result_pitch_sigma)
        if sigma <= 0.0:
            return None
        mean, std, valid_all = self._pp_geom_z_stats(hand)
        if not bool(valid_all[pitch_gi]):
            return None
        g = self.a.pools[hand].geom
        live = (g[pitch_gi] - mean) / std
        valid = valid_all[rows]
        out = np.ones(len(rows), dtype=np.float32)
        if valid.any():
            diff = (g[rows[valid]] - mean) / std - live
            d2 = np.einsum("ij,ij->i", diff * _PITCH_FEATURE_WEIGHTS, diff)
            scale = 2.0 * sigma * sigma * float(_PITCH_FEATURE_WEIGHTS.sum())
            f = np.exp(-d2 / scale).astype(np.float32)
            mean_w = float(f.mean())
            out[valid] = f / np.float32(mean_w) if mean_w > 0.0 else np.float32(1.0)
        return out

    #: Reference rows per candidate set for the density estimate.
    _RESULT_DENSITY_REFS = 256

    def _result_inv_density(self, hand: str, key: tuple, rows: np.ndarray) -> np.ndarray:
        """The inverse local density of every candidate row under the result
        kernel — each row's mean kernel value against a fixed random subset
        of the candidate rows (a pool fact: cached per candidate set and
        bandwidth; the subset is drawn from a generator seeded by the key, so
        the sampler's own stream is untouched), raised to
        ``result_density_power``. Floored at 5% of the median density so a
        lone outlier gains at most 20× at power 1, normalized to a mean of 1;
        a row without complete geometry is exactly neutral."""
        meta = self._cell_meta(hand) if key[0] >= 0 else self._pool_meta(hand)
        cache = meta.setdefault("density", {})
        ckey = (key, float(self.result_pitch_sigma), float(self.result_density_power))
        hit = cache.get(ckey)
        if hit is not None:
            return hit
        mean, std, valid_all = self._pp_geom_z_stats(hand)
        valid = valid_all[rows]
        out = np.ones(len(rows), dtype=np.float32)
        vr = rows[valid]
        if vr.size:
            g = self.a.pools[hand].geom
            sw = np.sqrt(_PITCH_FEATURE_WEIGHTS)
            z = ((g[vr] - mean) / std) * sw  # weighted metric: plain squared distance
            m = int(min(self._RESULT_DENSITY_REFS, vr.size))
            if m < vr.size:
                seed = abs(hash((int(key[0]), int(key[1]), *[int(k) for k in key[2:]]))) % (2**32)
                pick = np.random.default_rng(seed).choice(vr.size, size=m, replace=False)
                ref = z[pick]
            else:
                ref = z
            scale = 2.0 * float(self.result_pitch_sigma) ** 2 * float(_PITCH_FEATURE_WEIGHTS.sum())
            ref_sq = np.einsum("ij,ij->i", ref, ref)
            dens = np.empty(vr.size, dtype=np.float64)
            for start in range(0, vr.size, 8192):
                zb = z[start : start + 8192]
                d2 = np.einsum("ij,ij->i", zb, zb)[:, None] + ref_sq[None, :] - 2.0 * (zb @ ref.T)
                dens[start : start + 8192] = np.exp(-np.maximum(d2, 0.0) / scale).mean(axis=1)
            floor = 0.05 * float(np.median(dens))
            inv = np.power(np.maximum(dens, max(floor, 1e-30)), -float(self.result_density_power))
            inv = inv / inv.mean()
            out[valid] = inv.astype(np.float32)
        cache[ckey] = out
        return out

    def result_weights(self, b: int, rows: np.ndarray, pitch_gi: int) -> np.ndarray | None:
        """The RESULT draw's weight over the pitch draw's candidate
        rows (the same count sub-cell ``b``), anchored on the pitch ``pitch_gi``:
        the raw per-PA weight × the pitch-to-pitch factor × the density
        correction × the re-raised pitcher / batter factors × the receiving
        ratio. None when nothing can condition the draw (the pitch row itself
        then stands). The one code path :meth:`_result_draw` samples from and
        the offline joint fit reads whole — so the two cannot disagree."""
        assert self._bucket_w is not None and self._hand is not None
        w = self._bucket_w[b]
        if w is None or w.size == 0:
            return None
        f = self._f_result_pitch(self._hand, rows, pitch_gi)
        if f is None:
            return None
        wr = w * f
        if self.result_density_power != 0.0:
            key = self._pa_keys[b] if self._pa_keys is not None else (-1, b)
            wr = wr * self._result_inv_density(self._hand, key, rows)
        # ``result_pitcher_power`` is ABSOLUTE. The raw weight
        # carries the pitcher factor at the PITCH draw's power, so the result
        # draw re-raises by the difference (1.0 here = no change).
        q = float(self.result_pitcher_power) - float(self.pitch_pitcher_power) + 1.0
        if q != 1.0 and self._bucket_fp is not None:
            fp = self._bucket_fp[b]
            if fp is not None:
                uns = self._pool_meta(self._hand)["prof_unscored"][rows]
                wr = _repower_neutral(wr, fp, uns, q, self._pitcher_neutral_adj)
        rb = float(self.result_batter_power)
        if rb != 1.0 and self._bucket_fb is not None:
            fb = self._bucket_fb[b]
            if fb is not None:
                uns = self._batter_unscored(self._hand)[rows]
                wr = _repower_neutral(wr, fb, uns, rb, self._bat_neutral_rb)
        # The catcher receiving ratio, last, on taken rows.
        if self._bucket_recv is not None and self._bucket_recv[b] is not None:
            taken = self._pool_meta(self._hand)["taken"][rows]
            wr = self._apply_receiving(wr, taken, self._bucket_recv[b])
        return wr

    def _result_draw(self, b: int, rows: np.ndarray, pitch_gi: int) -> int:
        """Step 4: draw the RESULT row among the pitch draw's candidate rows
        under :meth:`result_weights`. Falls back to the pitch row itself when
        nothing can condition the draw."""
        wr = self.result_weights(b, rows, pitch_gi)
        if wr is None:
            return pitch_gi
        cdf = np.cumsum(wr, dtype=np.float64)
        total = float(cdf[-1])
        if not np.isfinite(total) or total <= 0.0:
            return pitch_gi
        j = int(np.searchsorted(cdf, self.rng.random() * total))
        return int(rows[min(j, rows.size - 1)])

    def last_result_row(self) -> int | None:
        """The global pitch-pool index of the last RESULT row
        (the play), or None before any draw / after an empty-bucket fallback.
        Equal to the pitch row while the split is off."""
        return self._pp_last_i

    def last_born_batted_ball(self) -> dict | None:
        """The last result row's OWN batted ball, read through
        the artifact's pitch-id join — ``row`` (the batted-ball pool index),
        ``ev``, ``la``, ``spray`` (pull-relative), ``spray_raw``, ``dist``,
        ``is_air``. None when the last result was not in play, the bundle
        carries no join, or the join has no batted ball for that pitch."""
        i = self._pp_last_i
        if i is None or self._hand is None:
            return None
        br = getattr(self.a.pools[self._hand], "bb_row", None)
        if br is None:
            return None
        j = int(br[i])
        pool = self.a.bb_pools.get(self._hand)
        if j < 0 or pool is None or j >= pool.n:
            return None
        return {
            "row": j,
            "ev": float(pool.geom[j, 0]),
            "la": float(pool.geom[j, 1]),
            "spray": float(pool.geom[j, 2]),
            "spray_raw": (float(pool.spray_raw[j]) if pool.spray_raw is not None else None),
            "dist": (float(pool.hit_dist[j]) if pool.hit_dist is not None else None),
            "is_air": (bool(pool.is_air[j]) if pool.is_air is not None else None),
            # The class (``BB_CLASS``; None on a pool without it) and the park
            # the ball was hit in.
            "cls": (int(pool.bb_class[j]) if pool.bb_class is not None else None),
            "venue": (int(pool.venue_id[j]) if pool.venue_id is not None else None),
        }

    def last_pitch_got_away(self) -> bool:
        """Did the LAST drawn pitch row get away from its catcher (a
        passed ball / wild pitch, incl. an uncaught third strike)? The drawn
        row IS the play — this flag is its got-away fact. False before any
        draw, after an empty-bucket fallback, or on a pool without the
        ``got_away`` column."""
        i = self._pp_last_i
        if i is None or self._hand is None:
            return False
        ga = self.a.pools[self._hand].got_away
        if ga is None:
            return False
        return bool(ga[i])

    def last_pitch_geom(self) -> np.ndarray | None:
        """The LAST drawn pitch row's raw geometry (the ten
        pitch-pool columns, ``_GEOM_COLS`` order), so the batted-ball draw can
        condition on the pitch that produced it. None before any draw, after
        an empty-bucket fallback, or when the row's velocity is missing (the
        export writes a NULL as 0.0, and no real pitch reads 0 mph). It is the
        PITCH-draw row, the pitch thrown, which is the result row while the
        split is off."""
        i = self._pp_pitch_i if self._pp_pitch_i is not None else self._pp_last_i
        if i is None or self._hand is None:
            return None
        g = self.a.pools[self._hand].geom[i]
        if not (float(g[0]) > 0.0) or not bool(np.isfinite(g).all()):
            return None
        return g

    # ---- the catcher receiving ratio -------------------------------------
    def _receiving_on(self) -> bool:
        """True when the receiving ratio is on and a catcher is staged."""
        return bool(self.catcher_receiving) and self._catcher_key is not None

    def _recv_factor(self, hand: str, catcher_key: str) -> np.ndarray | None:
        """The per-row receiving ratio for the LIVE catcher over the hand's
        pitch pool — 1.0 on every swung-at, hit-by-pitch or unknown-zone row;
        on a taken row the framing multiplier at its zone group (a called
        strike) or the mirror (a ball), times the blocking ratio (a got-away)
        or its mirror (a taken pitch that stayed caught). None when the bundle
        carries no receiving document, no zones, or no entry for the catcher
        (the draw is then unweighted — a neutral catcher)."""
        key = (hand, catcher_key)
        if key in self._recv_cache:
            return self._recv_cache[key]
        out: np.ndarray | None = None
        table = getattr(self.a, "receiving", None) or {}
        pool = self.a.pools[hand]
        entry = (table.get("catchers") or {}).get(catcher_key)
        zone = getattr(pool, "zone", None)
        if entry is not None and zone is not None:
            meta = self._pool_meta(hand)
            outcome = meta["outcome"]
            g = recv_zone_group(zone)
            groups = list(table.get("groups") or [])
            # The league rates of each ROW's own season (the pooled rates for
            # a season the document lacks) — a called-strike rate that moved
            # between seasons must not read as catcher skill.
            per_season = table.get("league_frame") or {}
            pooled = table.get("league_frame_all") or {}
            row_season = np.asarray(pool.season, dtype=np.int64)
            gi = np.clip(g.astype(np.int64), 0, len(groups))
            L = np.ones(pool.n, dtype=np.float64)
            for s in np.unique(row_season):
                lf = per_season.get(str(int(s)))
                if not isinstance(lf, dict):
                    lf = pooled
                lg = np.array(
                    [1.0] + [float(lf.get(name, 0.0)) for name in groups], dtype=np.float64
                )
                sel_s = row_season == s
                L[sel_s] = lg[gi[sel_s]]
            mg = np.array(
                [1.0] + [float((entry.get("frame") or {}).get(name, 1.0)) for name in groups],
                dtype=np.float64,
            )
            known = (g > 0) & (gi <= len(groups))
            m = mg[gi]
            cs = outcome == "called_strike"
            ball = outcome == "ball"
            out = np.ones(pool.n, dtype=np.float32)
            sel = cs & known
            out[sel] = m[sel].astype(np.float32)
            with np.errstate(divide="ignore", invalid="ignore"):
                mirror = np.where(L < 1.0, (1.0 - L * m) / (1.0 - L), 1.0)
            sel = ball & known
            out[sel] = np.clip(mirror[sel], 0.0, None).astype(np.float32)
            b = entry.get("block")
            gb_season = table.get("league_block") or {}
            gb_all = list(table.get("league_block_all") or [])
            if b is not None and (gb_season or gb_all) and pool.got_away is not None:
                cell = recv_block_cell(zone, pool.geom[:, 9], pool.geom[:, 0] > 0.0)
                n_cells = len(gb_all) if gb_all else len(next(iter(gb_season.values())))
                gcell = np.zeros(pool.n, dtype=np.float64)
                ok = (cs | ball) & (cell >= 0) & (cell < n_cells)
                for s in np.unique(row_season):
                    cells = gb_season.get(str(int(s))) if isinstance(gb_season, dict) else None
                    if not cells:
                        cells = gb_all
                    if not cells:
                        continue
                    sel_s = ok & (row_season == s)
                    gcell[sel_s] = np.asarray(cells, dtype=np.float64)[cell[sel_s].astype(np.int64)]
                if ok.any():
                    G = gcell[ok]
                    ga = np.asarray(pool.got_away)[ok] > 0
                    bf = float(b)
                    with np.errstate(divide="ignore", invalid="ignore"):
                        fac = np.where(ga, bf, np.where(G < 1.0, (1.0 - G * bf) / (1.0 - G), 1.0))
                    out[ok] = out[ok] * np.clip(fac, 0.0, None).astype(np.float32)
        self._recv_cache[key] = out
        return out

    @staticmethod
    def _apply_receiving(w: np.ndarray, taken: np.ndarray, f: np.ndarray) -> np.ndarray:
        """Multiply the TAKEN rows' weights by their ratio factors and rescale
        the taken group so its total weight is unchanged; swung-at rows are
        untouched. A group with no weight, or one every factor zeroes, stays."""
        if not taken.any():
            return w
        before = float(w[taken].sum())
        wf = w[taken] * f[taken]
        after = float(wf.sum())
        if before <= 0.0 or after <= 0.0 or not np.isfinite(after):
            return w
        out = w.copy()
        out[taken] = wf * np.float32(before / after)
        return out
