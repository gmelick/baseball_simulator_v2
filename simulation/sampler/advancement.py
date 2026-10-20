"""The advancement draws: the five discretionary extra-base decisions.

Each (scenario, from base, target base) is its own pool. A draw weighs the
rows by the throw geometry, the runner and the live fielder's arm, and its row
answers whether the runner goes, whether he is safe and whether a bad throw
gives him an extra base.
"""

from __future__ import annotations

import numpy as np

from simulation.sampler.base import BaseMixin
from simulation.sampler.kernels import (
    _POS_NUM_TO_NAME,
)


class AdvancementMixin(BaseMixin):
    """The sampler's advancement methods."""

    def _init_advancement(self) -> None:
        """The advancement draws' bandwidth and cache."""
        #: Per-decision precompute (``_adv_meta``).
        self._adv_meta_cache: dict[str, dict] = {}
        #: The throw-geometry kernel's bandwidth (exit velocity, launch angle,
        #: spray, distance, outs: the ball, not an actor).
        self.adv_feat_sigma = 1.0
        self._br_idx: dict[str, int] | None = None

    # ---- SIM-425: engine-backed baserunner advancement rates --------------
    def runner_rate(self, runner_key: str, name: str) -> float | None:
        """Return a baserunner's raw advancement rate (e.g. ``second_to_home_attempt_rate``,
        ``tag_up_attempt_rate``) from the baserunner embedding, or None when the
        runner / feature is absent so the caller can fall back to a league constant."""
        bemb = self.a.actor_emb.get("baserunner")
        if bemb is None:
            return None
        feats = bemb.get("features")
        idx = bemb["key_index"].get(runner_key)
        if feats is None or idx is None or name not in self._br_feat_idx(feats):
            return None
        v = float(bemb["vecs"][idx][self._br_feat_idx(feats)[name]])
        return v if np.isfinite(v) else None

    def _br_feat_idx(self, feats: list) -> dict[str, int]:
        if self._br_idx is None:
            self._br_idx = {f: i for i, f in enumerate(feats)}
        return self._br_idx

    # ---- SIM-512: the five-scenario advancement draw -----------------------

    #: The runner engine's features: how he runs AND how he decides. All columns of
    #: derived.baserunner_season_metrics (the baserunner embedding).
    _RUNNER_ADV_FEATURES = (
        "sprint_speed",
        "extra_base_attempt_rate",
        "extra_base_success_rate",
        "first_to_third_attempt_rate",
        "second_to_home_attempt_rate",
        "first_to_home_attempt_rate",
        "tag_up_attempt_rate",
    )
    #: The fielder-arm kernel: a strong arm DETERS the send, because rows
    #: against similar arms carry fewer attempts. Columns of
    #: derived.fielder_season_metrics (the fielder embedding).
    _FIELDER_ADV_FEATURES = (
        "arm_strength",
        "arm_hold_rate",
        "arm_thrown_out_rate",
        "arm_advancement_prevention",
    )

    def has_advancement(self) -> bool:
        """SIM-512: True when the bundle carries the advancement pools."""
        return bool(self.a.adv_pools)

    def _adv_meta(self, key: str) -> dict | None:
        """Per-decision one-time precompute: the z-scored throw-geometry
        matrix + per-row embedding-row gathers for the runner and fielder."""
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
                    kidx.get(f"{int(f)}:{_POS_NUM_TO_NAME.get(int(p), '?')}:{int(s)}", -1)
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
        """SIM-512: draw ONE advancement-opportunity row -> (attempted, safe,
        error_extra), or None when the pool/decision is absent (a legacy
        bundle — the caller then stages no discretionary advancement).

        The owner's rule (2026-08-10): every sim decision is a similarity-
        weighted draw from a hard-filtered pool, never a hand-tuned formula.
        Hard filter: the decision itself — (scenario, from_base, target_base)
        is its own sub-pool. Weights: runner similarity (his legs AND his
        decisions), the LIVE fielder's arm against each row fielder's arm
        (a strong arm deters the send because rows against similar arms carry
        fewer attempts), a z-scored kernel over the throw geometry (EV, LA,
        spray, distance, outs), and recency. The drawn row answers the whole
        question at once: ``attempted`` says whether he goes, ``safe`` says
        the throw's outcome, ``error_extra`` the extra base on a bad throw.
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
        f = self._steal_actor_factor(
            "baserunner",
            runner_key,
            meta["runner_rows"],
            meta["rows"],
            self._RUNNER_ADV_FEATURES,
            matrix="runner_adv",
        )
        if f is not None:
            w *= f
        if fielder_key:
            # The live fielder key is "id:POS:season"; the fielder matrices are
            # per position, so the matrix name follows the key's position.
            parts = fielder_key.split(":")
            f = self._steal_actor_factor(
                "fielder",
                fielder_key,
                meta["fielder_rows"],
                meta["rows"],
                self._FIELDER_ADV_FEATURES,
                matrix=(f"fielder_{parts[1]}" if len(parts) == 3 else None),
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
