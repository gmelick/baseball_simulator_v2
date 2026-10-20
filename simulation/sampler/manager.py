"""The pitching-change draw and the reliever draw.

The change draw takes one row of the change opportunity pool's hard cell; its
``changed`` flag is the decision. The reliever draw then picks the entering arm
from the live pen by its resemblance to the arm the drawn row brought in.
"""

from __future__ import annotations

import numpy as np

from simulation.sampler.base import BaseMixin


class ManagerMixin(BaseMixin):
    """The sampler's pitching-change methods."""

    def _init_manager(self) -> None:
        """The change and reliever draws' weights, counters and caches."""
        #: The change draw: a Gaussian on the z-scored soft columns
        #: (``change_sit_sigma``), the live pitcher's similarity to each row's
        #: pitcher (``change_pitcher_power``; 0 = off) and recency, inside the
        #: hard cell (role, half-inning boundary, pitch-count bucket, times
        #: through the order). A cell under ``change_min_cell`` rows widens.
        self.change_sit_sigma = 1.0
        self.change_pitcher_power = 1.0
        self.change_min_cell = 20
        #: The reliever draw's weights, by the candidate's resemblance to the
        #: arm the drawn row brought in: role (a Gaussian on the difference in
        #: high-leverage entry share), stuff (the pitcher engine's score at a
        #: power), rest (a Gaussian on days of rest), pitched in the last two
        #: days (a mismatch weight), pitches over the last three days (a
        #: Gaussian) and hand (a mismatch weight). Every one off (0 or 1.0):
        #: ``relief_arm_draw`` returns None and the loop keeps its positional
        #: pick.
        self.relief_role_sigma = 0.0
        self.relief_pitcher_power = 0.0
        self.relief_rest_sigma = 0.0
        self.relief_pitched2d_off_weight = 1.0
        self.relief_pitches3d_sigma = 0.0
        self.relief_hand_off_weight = 1.0
        #: Reliever draws, and the ones whose every weight was neutral.
        self.relief_draw_counts = np.zeros(2, dtype=np.int64)
        #: Change draws per widening level 0..3.
        self.change_widen_counts = np.zeros(4, dtype=np.int64)
        #: The manager power's starvation guard: the sum over change draws of
        #: the cell's effective-sample share (the Kish effective sample size of
        #: the final weights over the cell's row count), and the draw count.
        self.change_ess_stats = np.zeros(2, dtype=np.float64)
        self._change_meta_cache: dict | None = None
        self._change_last_row: int | None = None

    # ---- the pitching-change draw ------------------------------------------
    #: The pitch-count bucket width (pitches) of the hard cell.
    _CHANGE_PC_BUCKET = 10

    def _change_meta(self) -> dict | None:
        """One-time precompute: the hard-cell index — (is_starter, new_half,
        pitch-count bucket, times through the order) -> rows — the z-stats of
        the soft columns, and each row's pitcher profile index."""
        if self._change_meta_cache is not None:
            return self._change_meta_cache or None
        pool = getattr(self.a, "change_pool", None)
        if pool is None or pool.n == 0:
            self._change_meta_cache = {}
            return None
        sit = pool.sit
        pcb = np.clip(sit[:, 0].astype(np.int64) // self._CHANGE_PC_BUCKET, 0, 15)
        tto = np.clip(sit[:, 1].astype(np.int64) // 9 + 1, 1, 4)
        key = (
            (pool.is_starter.astype(np.int64) * 2 + pool.new_half.astype(np.int64)) * 100 + pcb
        ) * 10 + tto
        order = np.argsort(key, kind="stable")
        sk = key[order]
        uniq, starts = np.unique(sk, return_index=True)
        ends = np.append(starts[1:], len(order))
        cells: dict[tuple[int, int, int, int], np.ndarray] = {}
        for k, lo, hi in zip(uniq, starts, ends, strict=True):
            k = int(k)
            t = k % 10
            k //= 10
            b = k % 100
            k //= 100
            cells[(k // 2, k % 2, b, t)] = order[lo:hi]
        mean = sit.mean(axis=0).astype(np.float32)
        std = sit.std(axis=0).astype(np.float32)
        std = np.where(std > 1e-6, std, np.float32(1.0)).astype(np.float32)
        pidx = self.a.pitcher_sim_index
        prof = np.fromiter(
            (
                pidx.get(f"{int(p)}:{int(s)}", -1)
                for p, s in zip(pool.pitcher_id, pool.season, strict=False)
            ),
            dtype=np.int64,
            count=pool.n,
        )
        meta = {"cells": cells, "mean": mean, "std": std, "prof": prof, "widened": {}}
        # Each row's column in the manager-usage score matrix (-1 =
        # unscored, no manager on the row, or no matrix in the bundle).
        entry = self._actor_matrix("manager_usage")
        mid = getattr(pool, "manager_id", None)
        if entry is not None and mid is not None:
            index = entry["index"]
            meta["mgr_col"] = np.fromiter(
                (
                    index.get(f"{int(m)}:{int(s)}", -1) if int(m) >= 0 else -1
                    for m, s in zip(mid, pool.season, strict=False)
                ),
                dtype=np.int64,
                count=pool.n,
            )
        else:
            meta["mgr_col"] = None
        self._change_meta_cache = meta
        return meta

    def _change_rows(self, meta: dict, cell: tuple[int, int, int, int]) -> tuple[np.ndarray, int]:
        """The cell's rows, widened below ``change_min_cell`` in a fixed order:
        level 1 drops the times-through-the-order dimension, level 2 the
        pitch-count bucket too, level 3 the half-inning boundary too (the role
        alone). Cached per cell."""
        hit = meta["widened"].get(cell)
        if hit is not None:
            return hit
        cells = meta["cells"]
        need = max(int(self.change_min_cell), 1)
        starter, new_half, pcb, tto = cell
        rows = cells.get(cell, np.zeros(0, dtype=np.int64))
        level = 0
        if rows.size < need:
            rows = np.concatenate(
                [v for k, v in cells.items() if k[:3] == (starter, new_half, pcb)] or [rows]
            )
            level = 1
            if rows.size < need:
                rows = np.concatenate(
                    [v for k, v in cells.items() if k[:2] == (starter, new_half)] or [rows]
                )
                level = 2
                if rows.size < need:
                    rows = np.concatenate(
                        [v for k, v in cells.items() if k[0] == starter] or [rows]
                    )
                    level = 3
        out = (rows, level)
        meta["widened"][cell] = out
        return out

    def pitching_change_draw(
        self,
        pitcher_key: str,
        *,
        is_starter: bool,
        new_half: bool,
        pitch_count: int,
        batters_faced: int,
        inning: int,
        outs: int,
        runners_state: int,
        score_diff: int,
        manager_key: str | None = None,
    ) -> bool | None:
        """Step 1 of the loop: does the manager change pitchers at this
        plate-appearance boundary? ONE draw from the opportunity pool's hard
        cell — (starter or reliever, a half-inning boundary or mid-inning, the
        pitch-count bucket, times through the order), widened below
        ``change_min_cell`` — weighted by recency, a Gaussian on the z-scored
        soft columns (pitch count, batters faced, inning, outs, runners, the
        fielding side's score margin; bandwidth ``change_sit_sigma``), the
        current pitcher's similarity to each row's pitcher (the pitcher-sim
        row, raised to ``change_pitcher_power``; a row whose pitcher has no
        profile is neutral) and the live manager's usage similarity to each
        row's manager (``manager_key``, :meth:`_change_manager_factor`). The
        drawn row's ``changed`` flag IS the decision. None when the bundle
        carries no pool: the loop then makes no change."""
        pool = getattr(self.a, "change_pool", None)
        meta = self._change_meta()
        if pool is None or meta is None:
            return None
        pcb = int(min(max(int(pitch_count) // self._CHANGE_PC_BUCKET, 0), 15))
        tto = int(min(max(int(batters_faced) // 9 + 1, 1), 4))
        cell = (1 if is_starter else 0, 1 if new_half else 0, pcb, tto)
        rows, level = self._change_rows(meta, cell)
        self.change_widen_counts[level] += 1
        if rows.size == 0:
            return None
        live = (
            np.array(
                [pitch_count, batters_faced, inning, outs, runners_state, score_diff],
                dtype=np.float32,
            )
            - meta["mean"]
        ) / meta["std"]
        z = (pool.sit[rows] - meta["mean"]) / meta["std"]
        diff = z - live
        d2 = np.einsum("ij,ij->i", diff, diff)
        w = pool.recency[rows].astype(np.float32) * np.exp(
            -d2 / (2.0 * self.change_sit_sigma**2 * diff.shape[1])
        ).astype(np.float32)
        if self.change_pitcher_power != 0.0:
            f = self._change_pitcher_factor(pitcher_key, meta["prof"][rows])
            if f is not None:
                w = w * f
        # The live manager's USAGE similarity to each row's manager, from the
        # nightly matrix, at its power (``SIM_ACTOR_POWER_MANAGER_USAGE``;
        # 0 = off, the code default; the owner's decision of 2026-09-13). An
        # unscored row is draw-neutral.
        if manager_key:
            f = self._change_manager_factor(meta, rows, manager_key)
            if f is not None:
                w = w * f
        w = self._apply_asof(w, pool, rows)
        total = float(w.sum())
        if not np.isfinite(total) or total <= 0.0:
            return None
        sq = float(np.square(w.astype(np.float64)).sum())
        if sq > 0.0:
            self.change_ess_stats[0] += (total * total / sq) / rows.size
            self.change_ess_stats[1] += 1.0
        cdf = np.cumsum(w, dtype=np.float64)
        i = int(np.searchsorted(cdf, self.rng.random() * cdf[-1]))
        r = int(rows[min(i, rows.size - 1)])
        self._change_last_row = r
        return bool(pool.changed[r])

    def _change_manager_factor(
        self, meta: dict, rows: np.ndarray, manager_key: str
    ) -> np.ndarray | None:
        """The live manager's usage-similarity row gathered onto the
        cell's rows, raised to ``actor_power['manager_usage']`` (0 = off ->
        None), an unscored row draw-neutral at the mean scored weight. None
        when the bundle has no manager matrix, the pool no manager column, or
        the live manager no matrix row (then the factor is neutral)."""
        power = float(self.actor_power.get("manager_usage", 0.0))
        if power <= 0.0:
            return None
        entry = self._actor_matrix("manager_usage")
        cols_all = meta.get("mgr_col")
        if entry is None or cols_all is None:
            return None
        live = entry["index"].get(manager_key)
        if live is None:
            return None
        cols = cols_all[rows]
        valid = cols >= 0
        out = np.ones(len(rows), dtype=np.float32)
        if not valid.any():
            return out
        scores = entry["matrix"][live, cols[valid]].astype(np.float32)
        scores = np.where(np.isfinite(scores), scores, np.float32(1.0))
        if power != 1.0:
            scores = np.power(np.clip(scores, 0.0, None), np.float32(power)).astype(np.float32)
        out[valid] = scores
        if not valid.all():
            out[~valid] = np.float32(scores.mean())
        return out

    # ---- the reliever draw from the live pen ------------------------------
    def relief_weights_on(self) -> bool:
        """True when any reliever-draw weight is on."""
        return (
            self.relief_role_sigma > 0.0
            or self.relief_pitcher_power > 0.0
            or self.relief_rest_sigma > 0.0
            or self.relief_pitched2d_off_weight != 1.0
            or self.relief_pitches3d_sigma > 0.0
            or self.relief_hand_off_weight != 1.0
        )

    def relief_arm_draw(
        self,
        candidates: list[int],
        *,
        live_season: int,
        throw_hands: dict[int, str] | None = None,
        recent_usage: dict[int, tuple[int, int, int]] | None = None,
    ) -> int | None:
        """Draw the entering arm's INDEX in ``candidates`` (the live
        pen's available, unused arms), weighted by each candidate's resemblance
        to the arm the last drawn change-pool row brought in — role, stuff,
        recent usage and hand (the weights on the sampler; every one OFF ->
        None, and the caller keeps its positional pick). ``recent_usage`` maps
        a candidate to (days of rest, pitched in the last two days, pitches
        over the last three), the resolver's facts; ``throw_hands`` his hand.
        A fact missing on either side leaves that weight neutral for him."""
        if not candidates or not self.relief_weights_on():
            return None
        pool = getattr(self.a, "change_pool", None)
        r = self._change_last_row
        if pool is None or r is None:
            return None
        n = len(candidates)
        w = np.ones(n, dtype=np.float64)
        season = int(pool.season[r])
        drawn_id = int(pool.incoming_id[r])
        roles = getattr(self.a, "change_roles", None) or {}
        # role: the high-leverage entry share
        if self.relief_role_sigma > 0.0:
            drawn_role = roles.get(f"{drawn_id}:{season}")
            if drawn_role is not None:
                f = np.ones(n)
                for i, pid in enumerate(candidates):
                    cand = roles.get(f"{int(pid)}:{int(live_season)}") or roles.get(
                        f"{int(pid)}:{int(live_season) - 1}"
                    )
                    if cand is not None:
                        d = float(cand[1]) - float(drawn_role[1])
                        f[i] = np.exp(-(d * d) / (2.0 * self.relief_role_sigma**2))
                w *= f / max(float(f.mean()), 1e-12)
        # stuff: the pitcher engine's score between the drawn arm and the candidate
        if self.relief_pitcher_power > 0.0:
            scores = self._pitcher_prof_scores(f"{drawn_id}:{season}")
            if scores is not None:
                idx = self.a.pitcher_sim_index
                f = np.ones(n)
                valid = np.zeros(n, dtype=bool)
                for i, pid in enumerate(candidates):
                    j = idx.get(f"{int(pid)}:{int(live_season)}")
                    if j is None:
                        j = idx.get(f"{int(pid)}:{int(live_season) - 1}")
                    if j is not None and np.isfinite(scores[j]):
                        f[i] = max(float(scores[j]), 0.0) ** float(self.relief_pitcher_power)
                        valid[i] = True
                if valid.any():
                    f[~valid] = float(f[valid].mean())  # draw-neutral for an unscored arm
                    w *= f / max(float(f.mean()), 1e-12)
        # recent usage: rest, pitched in the last two days, pitches in three
        usage = recent_usage or {}
        in_rest = getattr(pool, "in_days_rest", None)
        in_p2 = getattr(pool, "in_pitched_2d", None)
        in_p3 = getattr(pool, "in_pitches_3d", None)
        if in_rest is not None and int(in_rest[r]) >= 0:
            d_rest, d_p2, d_p3 = int(in_rest[r]), int(in_p2[r]), int(in_p3[r])
            if self.relief_rest_sigma > 0.0:
                f = np.ones(n)
                for i, pid in enumerate(candidates):
                    u = usage.get(int(pid))
                    if u is not None:
                        d = float(u[0]) - float(d_rest)
                        f[i] = np.exp(-(d * d) / (2.0 * self.relief_rest_sigma**2))
                w *= f / max(float(f.mean()), 1e-12)
            if self.relief_pitched2d_off_weight != 1.0 and d_p2 >= 0:
                f = np.ones(n)
                for i, pid in enumerate(candidates):
                    u = usage.get(int(pid))
                    if u is not None and int(u[1]) != d_p2:
                        f[i] = float(self.relief_pitched2d_off_weight)
                w *= f / max(float(f.mean()), 1e-12)
            if self.relief_pitches3d_sigma > 0.0 and d_p3 >= 0:
                f = np.ones(n)
                for i, pid in enumerate(candidates):
                    u = usage.get(int(pid))
                    if u is not None:
                        d = float(u[2]) - float(d_p3)
                        f[i] = np.exp(-(d * d) / (2.0 * self.relief_pitches3d_sigma**2))
                w *= f / max(float(f.mean()), 1e-12)
        # hand: a mismatch with the drawn arm's throwing hand
        in_throws = getattr(pool, "in_throws", None)
        if self.relief_hand_off_weight != 1.0 and in_throws is not None and int(in_throws[r]) > 0:
            drawn_hand = "L" if int(in_throws[r]) == 1 else "R"
            hands = throw_hands or {}
            f = np.ones(n)
            for i, pid in enumerate(candidates):
                h = hands.get(int(pid))
                if h is not None and str(h)[:1].upper() != drawn_hand:
                    f[i] = float(self.relief_hand_off_weight)
            w *= f / max(float(f.mean()), 1e-12)
        total = float(w.sum())
        self.relief_draw_counts[0] += 1
        if not np.isfinite(total) or total <= 0.0:
            return None
        if np.allclose(w, w[0]):
            self.relief_draw_counts[1] += 1  # every weight neutral: a uniform draw
        cdf = np.cumsum(w)
        i = int(np.searchsorted(cdf, self.rng.random() * cdf[-1]))
        return min(i, n - 1)

    def _change_pitcher_factor(self, pitcher_key: str, prof_rows: np.ndarray) -> np.ndarray | None:
        """The live pitcher's similarity to each candidate row's pitcher, from
        the pitcher-sim row, raised to ``change_pitcher_power`` and normalized
        to a mean of 1 over the rows with a profile (a row without one is
        neutral). None without a matrix or a live profile."""
        scores = self._pitcher_prof_scores(pitcher_key)
        if scores is None:
            return None
        valid = prof_rows >= 0
        out = np.ones(len(prof_rows), dtype=np.float32)
        if not valid.any():
            return out
        s = np.asarray(scores[prof_rows[valid]], dtype=np.float32)
        s = np.where(np.isfinite(s), s, np.float32(0.0))
        p = float(self.change_pitcher_power)
        if p != 1.0:
            s = np.power(np.clip(s, 0.0, None), np.float32(p)).astype(np.float32)
        mean_w = float(s.mean())
        out[valid] = s / np.float32(mean_w) if mean_w > 0.0 else np.float32(1.0)
        return out

    def _pitcher_prof_scores(self, pitcher_key: str) -> np.ndarray | None:
        """The live pitcher's similarity to every profile in the pitcher-sim
        index (the dense matrix row, or the dictionary on a bundle without
        the matrix); None when the pitcher has no profile."""
        idx = self.a.pitcher_sim_index
        n_prof = len(idx)
        if n_prof == 0:
            return None
        matrix = getattr(self.a, "pitcher_sim_matrix", None)
        if matrix is not None:
            i = idx.get(pitcher_key)
            if i is None:
                return None
            return np.asarray(matrix[i], dtype=np.float32)
        sims = (self.a.pitcher_sim or {}).get(pitcher_key)
        if not sims:
            return None
        scores = np.zeros(n_prof, dtype=np.float32)
        for k, v in sims.items():
            j = idx.get(k)
            if j is not None:
                scores[j] = float(v)
        return scores

    def last_change_row(self) -> dict | None:
        """The last drawn change row: its index, ``changed``, the incoming and
        outgoing pitchers' ids, the row's manager and the incoming arm's rest
        and hand (None for a column the bundle lacks). None before any draw."""
        r = self._change_last_row
        pool = getattr(self.a, "change_pool", None)
        if r is None or pool is None:
            return None
        out = {
            "row": r,
            "changed": bool(pool.changed[r]),
            "incoming_id": int(pool.incoming_id[r]),
            "pitcher_id": int(pool.pitcher_id[r]),
        }
        for attr in ("manager_id", "in_days_rest", "in_pitched_2d", "in_pitches_3d", "in_throws"):
            col = getattr(pool, attr, None)
            out[attr] = int(col[r]) if col is not None else None
        return out
