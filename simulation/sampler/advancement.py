"""The advancement draws: the five discretionary extra-base decisions.

Each (scenario, from base, target base) is its own pool. A draw weighs the
rows by the throw geometry, the runner and the live fielder's arm, and its row
answers whether the runner goes, whether he is safe and whether a bad throw
gives him an extra base.
"""

from __future__ import annotations

import numpy as np

from simulation.constants import FIELD_POSITION_NAMES
from simulation.sampler.base import BaseMixin


class AdvancementMixin(BaseMixin):
    """The sampler's advancement methods."""

    def _init_advancement(self) -> None:
        """The advancement draws' bandwidth and cache."""
        #: Per-decision precompute (``_adv_meta``).
        self._adv_meta_cache: dict[str, dict] = {}
        #: The throw-geometry kernel's bandwidth (exit velocity, launch angle,
        #: spray, distance, outs: the ball, not an actor).
        self.adv_feat_sigma = 1.0

    def has_advancement(self) -> bool:
        """True when the bundle carries the advancement pools."""
        return bool(self.a.adv_pools)

    def _adv_meta(self, key: str) -> dict | None:
        """One decision's precompute: the z-scored throw-geometry matrix and
        each row's runner and fielder embedding rows. Cached per decision."""
        meta = self._adv_meta_cache.get(key)
        if meta is not None:
            return meta
        pool = self.a.adv_pools.get(key)
        if pool is None or pool.n == 0:
            return None
        feat = pool.feat.astype(np.float32)
        mean = feat.mean(axis=0)
        std = feat.std(axis=0)
        std[std < 1e-6] = 1.0
        zfeat = (feat - mean) / std

        runner_rows = None
        bemb = self.a.actor_emb.get("baserunner")
        if bemb is not None:
            kidx = bemb["key_index"]
            runner_rows = np.fromiter(
                (
                    kidx.get(f"{int(r)}:{int(s)}", -1)
                    for r, s in zip(pool.runner_id, pool.season, strict=False)
                ),
                dtype=np.int64,
                count=pool.n,
            )
        fielder_rows = None
        femb = self.a.actor_emb.get("fielder")
        if femb is not None:
            kidx = femb["key_index"]
            fielder_rows = np.fromiter(
                (
                    kidx.get(f"{int(f)}:{FIELD_POSITION_NAMES.get(int(p), '?')}:{int(s)}", -1)
                    for f, p, s in zip(pool.fielder_id, pool.fielder_pos, pool.season, strict=False)
                ),
                dtype=np.int64,
                count=pool.n,
            )
        meta = {
            "zfeat": zfeat,
            "mean": mean,
            "std": std,
            "rows": np.arange(pool.n, dtype=np.int64),
            "runner_rows": runner_rows,
            "fielder_rows": fielder_rows,
        }
        self._adv_meta_cache[key] = meta
        return meta

    def advancement_draw(
        self,
        scenario: int,
        from_base: int,
        target_base: int,
        runner_key: str,
        fielder_key: str | None,
        *,
        outs: int,
        exit_velo: float,
        launch_angle: float,
        spray_angle: float,
        hit_distance: float,
    ) -> tuple[bool, bool, bool] | None:
        """Draw one advancement-opportunity row -> (attempted, safe,
        error_extra), or None when the bundle has no pool for the decision
        (the caller then stages no extra base).

        The hard filter is the decision itself: (scenario, from base, target
        base) is its own pool. The weights are recency, a Gaussian over the
        z-scored throw geometry (exit velocity, launch angle, spray, distance,
        outs), the runner's score (his legs and his decisions) and the live
        fielder's score against each row's fielder. A strong arm deters the
        send, because rows against similar arms carry fewer attempts. The
        drawn row answers the whole question: ``attempted`` whether he goes,
        ``safe`` the throw's outcome, ``error_extra`` the extra base on a bad
        throw.
        """
        key = f"{int(scenario)}_{int(from_base)}_{int(target_base)}"
        pool = self.a.adv_pools.get(key)
        meta = self._adv_meta(key)
        if pool is None or meta is None:
            return None
        w = pool.recency.astype(np.float32).copy()
        live = (
            np.asarray([exit_velo, launch_angle, spray_angle, hit_distance, outs], dtype=np.float32)
            - meta["mean"]
        ) / meta["std"]
        diff = meta["zfeat"] - live
        d2 = np.einsum("ij,ij->i", diff, diff)
        w *= np.exp(-d2 / (2.0 * self.adv_feat_sigma**2 * diff.shape[1])).astype(np.float32)
        f = self._actor_factor("runner_adv", runner_key, meta["runner_rows"], meta["rows"])
        if f is not None:
            w *= f
        if fielder_key:
            # The live fielder key is "id:POS:season"; the fielder matrices are
            # per position, so the matrix name follows the key's position.
            parts = fielder_key.split(":")
            f = self._actor_factor(
                f"fielder_{parts[1]}" if len(parts) == 3 else None,
                fielder_key,
                meta["fielder_rows"],
                meta["rows"],
            )
            if f is not None:
                w *= f
        w = self._apply_asof(w, pool)
        total = float(w.sum())
        if not np.isfinite(total) or total <= 0.0:
            return None
        cdf = np.cumsum(w, dtype=np.float64)
        i = min(int(np.searchsorted(cdf, self.rng.random() * cdf[-1])), pool.n - 1)
        return (bool(pool.attempted[i]), bool(pool.safe[i]), bool(pool.error_extra[i]))
