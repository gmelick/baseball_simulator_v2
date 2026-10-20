"""The sampler's shared machinery.

:class:`BaseMixin` holds what more than one draw reads: the per-hand pool
precompute, the as-of cutoff, the actor score-matrix lookups, the z-scored
actor embeddings and the draw-neutral means. Its ``TYPE_CHECKING`` block
declares, for the type checker only, the attributes and methods one mixin
reads from another.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from pipeline.batch.engine_artifacts import EngineArtifacts, HandPool


class BaseMixin:
    """The sampler methods every draw shares."""

    if TYPE_CHECKING:
        # --- set by FullPoolSampler.__init__ ---
        a: EngineArtifacts
        rng: np.random.Generator
        sit_sigma: float

        # --- the pitch draw (simulation/sampler/pitch.py) ---
        _hand: str | None
        _pa_keys: list[tuple] | None
        _bucket_w: list[np.ndarray | None] | None
        _bucket_fp: list[np.ndarray | None] | None
        _bucket_fb: list[np.ndarray | None] | None
        _pitcher_neutral_adj: float
        _bat_neutral_rb: float
        _pp_last_i: int | None
        _pp_pitch_i: int | None
        pitch_pitcher_power: float

        def _cell_meta(self, hand: str) -> dict: ...

        # --- the pitch-result draw (simulation/sampler/pitch_result.py) ---
        pitch_result_split: bool
        result_pitcher_power: float
        result_batter_power: float

        def _result_draw(self, b: int, rows: np.ndarray, pitch_gi: int) -> int: ...

        def _receiving_on(self) -> bool: ...

        def _recv_factor(self, hand: str, catcher_key: str) -> np.ndarray | None: ...

        @staticmethod
        def _apply_receiving(w: np.ndarray, taken: np.ndarray, f: np.ndarray) -> np.ndarray: ...

        # --- the fence stage (simulation/sampler/fence.py) ---
        fence_stage: bool
        bb_margin_band: float
        _fence_last_dec: int | None

        def in_wall_zone(self, born: dict) -> bool: ...

        def _born_in_live_air(
            self, born_bb: dict | None, venue_live: int | None
        ) -> dict | None: ...

        def _fence_rows(
            self,
            hand: str,
            meta: dict,
            cell: tuple[int, int],
            rows: np.ndarray,
            born: dict,
            venue_id: int | None,
            season: int | None = None,
        ) -> np.ndarray: ...

        def _bb_margin_stage(
            self,
            hand: str,
            rows: np.ndarray,
            born: dict,
            venue_id: int | None,
            season: int | None,
        ) -> np.ndarray: ...

    def _init_base(self) -> None:
        """The shared caches and the as-of cutoff."""
        #: Per-hand one-time precompute of the pitch pool (``_pool_meta``).
        self._pool_cache: dict[str, dict] = {}
        #: The point-in-time cutoff, a YYYYMMDD integer (SIM-535). None draws
        #: from the whole pool, as a live game does; set, every draw refuses
        #: plays from after that date.
        self.asof_ymd: int | None = None
        self._asof_masks: dict[int, np.ndarray] = {}
        #: The power each actor's score matrix is raised to, by matrix name
        #: (``SIM_ACTOR_POWER_<NAME>``). 1.0 is the raw 0-to-1 score; 0 turns
        #: the actor's factor off. A matrix the bundle lacks, or a live actor
        #: or pool row the matrix lacks, is neutral (1.0).
        self.actor_power: dict[str, float] = {}
        #: Matrix name -> each actor-embedding row's matrix column (-1 = unscored).
        self._emb_to_mat_cache: dict[str, np.ndarray] = {}
        #: The draw-neutral rule's caches: the live pitcher's score vector, and
        #: the mean scored weight per (hand, actor, key, power).
        self._prof_score_cache: dict[str, np.ndarray] = {}
        self._neutral_cache: dict[tuple, float] = {}
        #: The z-scored actor embeddings (``_emb_z``).
        self._steal_emb_z: dict[str, np.ndarray] = {}

    # ---- per-pool one-time precompute ------------------------------------
    def _pool_meta(self, hand: str) -> dict:
        if hand in self._pool_cache:
            return self._pool_cache[hand]
        pool: HandPool = self.a.pools[hand]
        n = pool.n
        # candidate (pitcher_id:season) -> dense profile index in the pitcher-sim list
        pidx = self.a.pitcher_sim_index
        pool_prof = np.fromiter(
            (
                pidx.get(f"{int(p)}:{int(s)}", -1)
                for p, s in zip(pool.pitcher_id, pool.season, strict=False)
            ),
            dtype=np.int64,
            count=n,
        )
        # candidate (batter_id:season) -> dense batter-embedding row
        bemb = self.a.actor_emb.get("batter")
        if bemb is not None:
            bkidx = bemb["key_index"]
            pool_bat = np.fromiter(
                (
                    bkidx.get(f"{int(b)}:{int(s)}", -1)
                    for b, s in zip(pool.batter_id, pool.season, strict=False)
                ),
                dtype=np.int64,
                count=n,
            )
        else:
            pool_bat = np.full(n, -1, dtype=np.int64)
        # SIM-429: count bucket per row (balls*3 + strikes, 12 buckets) + the row
        # indices per bucket, so the pitch draw conditions on the LIVE count
        # (ball-rate swings 37%@0-0 -> 23%@3-2; a count-blind draw 2.8x-inflates).
        balls = np.clip(pool.sit[:, 0].astype(np.int64), 0, 3)
        strikes = np.clip(pool.sit[:, 1].astype(np.int64), 0, 2)
        cbucket = balls * 3 + strikes
        bucket_rows = [np.nonzero(cbucket == b)[0] for b in range(12)]
        outcome_arr = np.asarray(pool.outcome_type, dtype=object)
        meta = {
            "pool": pool,
            "pool_prof": pool_prof,
            "pool_bat": pool_bat,
            "outcome": np.asarray(pool.outcome_type, dtype=object),
            # SIM-523 part E: the TAKEN rows (a called strike or a ball).
            "taken": (outcome_arr == "called_strike") | (outcome_arr == "ball"),
            "bucket_rows": bucket_rows,
            # SIM-430: the base-out columns as a CONTIGUOUS copy. _f_situation_baseout
            # ran ``pool.sit[:, 2:6]`` (a non-contiguous 4-col slice + copy over the
            # whole ~935K-row pool) on EVERY PA — the profiler's single biggest cost
            # (~0.67 s/game). Materialising it once here makes the per-PA RBF a plain
            # contiguous subtract.
            "sit_baseout": np.ascontiguousarray(pool.sit[:, 2:6]),
            # SIM-523 part F: the rows whose pitcher has no profile (the
            # draw-neutral rule at a power) and the row counts per pitcher
            # profile (the neutral mean; the batter's counts are per matrix
            # column, ``bat_col_counts``).
            "prof_unscored": pool_prof < 0,
            "prof_counts": np.bincount(pool_prof[pool_prof >= 0], minlength=max(1, len(pidx))),
        }
        self._pool_cache[hand] = meta
        return meta

    # ---- SIM-535: the point-in-time cutoff --------------------------------
    def set_asof(self, ymd: int | None) -> None:
        """Refuse pool rows from after ``ymd`` (a YYYYMMDD integer).

        The pools hold four seasons and the sampler draws from all of them.
        Simulating a game played in April 2025 while the pool still offers plays
        from that September is look-ahead — and a larger one than the batter
        profiles carried, because it is the plays themselves. The cutoff is set
        per game, so a backtest walking a season needs no rebuild.

        Sparsity is not the worry it first looks like: the pool window keeps the
        three completed seasons before the current one, so a cutoff anywhere in
        the newest season still leaves roughly three quarters of the pool.
        """
        if ymd != self.asof_ymd:
            self._asof_masks.clear()
        self.asof_ymd = None if ymd is None else int(ymd)

    def _asof_mask(self, pool) -> np.ndarray | None:
        """A 0/1 float over ``pool``'s rows — 1 where the play is admissible.

        None when no cutoff is set, which keeps the live path free of any extra
        multiply. Raises when a cutoff IS set and the pool carries no dates: a
        bundle exported before SIM-535 cannot be filtered, and drawing from it
        anyway would be exactly the silent leak this guards against.
        """
        if self.asof_ymd is None:
            return None
        key = id(pool)
        cached = self._asof_masks.get(key)
        if cached is not None:
            return cached
        ymd = getattr(pool, "game_ymd", None)
        if ymd is None:
            raise RuntimeError(
                f"{type(pool).__name__} carries no game dates, so the "
                f"{self.asof_ymd} cutoff cannot be applied. Rebuild the "
                "engine-artifact bundle (SIM-535)."
            )
        mask = (np.asarray(ymd) <= self.asof_ymd).astype(np.float32)
        self._asof_masks[key] = mask
        return mask

    def _apply_asof(self, w: np.ndarray, pool, rows: np.ndarray | None = None) -> np.ndarray:
        """Zero the weight of every play that had not happened yet."""
        m = self._asof_mask(pool)
        if m is None:
            return w
        return w * (m if rows is None else m[rows])

    # ---- SIM-523 part F: the draw-neutral rule for unscored rows ----------
    # A pool row whose pitcher / batter has no score reads 1.0 in the raw
    # factor — the MAXIMUM — which is harmless at power 1 (the certified
    # weight, kept byte for byte) and fatal at a fitted power: at power 12 a
    # 3% unscored share outweighs the whole scored pool. At any other power
    # such a row weighs the mean scored weight instead. The means come from
    # the live actor's matrix row and the row counts per column — no pool pass.
    def _pitcher_prof_score(self, pitcher_key: str) -> np.ndarray | None:
        """The live pitcher's score over the pitcher-sim profiles — the vector
        :meth:`_f_pitcher` gathers onto the rows; None when he has no profile
        or an empty query (the factor is then flat). Cached per key."""
        cached = self._prof_score_cache.get(pitcher_key)
        if cached is not None:
            return cached
        n_prof = len(self.a.pitcher_sim_index)
        if n_prof == 0:
            return None
        out: np.ndarray | None = None
        matrix = getattr(self.a, "pitcher_sim_matrix", None)
        if matrix is not None:
            # SIM-430: dense fast path — one contiguous (shared) row instead of
            # scattering the ~2 GB pitcher_sim dict. Byte-identical to the dict
            # path: a key absent from the index, or an unscored/empty query (an
            # all-zero row — a populated query always has >0 same-hand scores),
            # both fall back to the flat-ones weighting exactly as ``if not sims``.
            i = self.a.pitcher_sim_index.get(pitcher_key)
            if i is not None and matrix[i].any():
                out = matrix[i]
        else:
            sims = self.a.pitcher_sim.get(pitcher_key)
            if sims:
                out = np.full(n_prof, 0.0, dtype=np.float32)
                idx = self.a.pitcher_sim_index
                for k, v in sims.items():
                    j = idx.get(k)
                    if j is not None:
                        out[j] = v
        if out is not None:
            self._prof_score_cache[pitcher_key] = out
        return out

    def _neutral_mean_pitcher(self, hand: str, pitcher_key: str, power: float) -> float:
        """The mean of f_pitcher^power over the hand pool's SCORED rows — what an
        unscored row weighs to stay draw-neutral at that power (1.0 at power 1)."""
        if power == 1.0:
            return 1.0
        key = (hand, "pitcher", pitcher_key, float(power))
        cached = self._neutral_cache.get(key)
        if cached is not None:
            return cached
        prof_score = self._pitcher_prof_score(pitcher_key)
        counts = self._pool_meta(hand)["prof_counts"]
        mean = 1.0
        if prof_score is not None and counts.sum() > 0:
            n = min(len(prof_score), len(counts))
            sc = np.power(np.clip(np.asarray(prof_score[:n], dtype=np.float64), 0.0, None), power)
            mean = float(counts[:n] @ sc / counts[:n].sum())
        self._neutral_cache[key] = mean
        return mean

    def _batter_matrix_on(self) -> bool:
        """The batter matrix reaches the pitch draws: a power above 0 (0 turns
        the factor off) and the bundle carrying the matrix."""
        return (
            float(self.actor_power.get("batter", 1.0)) > 0.0
            and self._actor_matrix("batter") is not None
            and self._emb_to_mat("batter") is not None
        )

    def _batter_cols(self, hand: str) -> np.ndarray | None:
        """The batter matrix column per pool row (-1 = unscored), with the
        row counts per column; None when the bundle has no batter matrix."""
        meta = self._pool_meta(hand)
        if "bat_cols" not in meta:
            e2m = self._emb_to_mat("batter")
            pb = meta["pool_bat"]
            if e2m is None or len(e2m) == 0:
                meta["bat_cols"] = None
                meta["bat_col_counts"] = None
            else:
                cols = np.where(pb >= 0, e2m[np.clip(pb, 0, len(e2m) - 1)], -1)
                meta["bat_cols"] = cols
                n_cols = int(cols.max()) + 1 if cols.size else 0
                meta["bat_col_counts"] = np.bincount(cols[cols >= 0], minlength=max(1, n_cols))
        return meta["bat_cols"]

    def _batter_unscored(self, hand: str) -> np.ndarray:
        """The rows whose batter has no score on the live path (the matrix
        column, or the embedding row on the kernel path)."""
        meta = self._pool_meta(hand)
        if self._batter_matrix_on():
            cols = self._batter_cols(hand)
            if cols is not None:
                return cols < 0
        return meta["pool_bat"] < 0

    def _batter_live_scores(
        self, hand: str, batter_key: str
    ) -> tuple[np.ndarray, np.ndarray] | None:
        """(the live batter's score per matrix column / embedding row, the
        pool-row counts per column / row) — the neutral mean's ingredients;
        None when he has no scores (the factor is flat)."""
        meta = self._pool_meta(hand)
        if self._batter_matrix_on():
            entry = self._actor_matrix("batter")
            assert entry is not None
            live = entry["index"].get(batter_key)
            self._batter_cols(hand)
            counts = meta["bat_col_counts"]
            if live is None or counts is None:
                return None
            row = np.asarray(entry["matrix"][live], dtype=np.float64)
            row = np.where(np.isfinite(row), row, 1.0)
            a = float(self.actor_power.get("batter", 1.0))
            if a != 1.0:
                row = np.power(np.clip(row, 0.0, None), a)
            return row, counts
        return None

    def _neutral_mean_batter(self, hand: str, batter_key: str, power: float) -> float:
        """The mean of f_batter^power over the hand pool's SCORED rows (1.0 at
        power 1, or when the batter has no scores)."""
        if power == 1.0:
            return 1.0
        key = (hand, "batter", batter_key, float(power), bool(self._batter_matrix_on()))
        cached = self._neutral_cache.get(key)
        if cached is not None:
            return cached
        ls = self._batter_live_scores(hand, batter_key)
        mean = 1.0
        if ls is not None:
            row, counts = ls
            n = min(len(row), len(counts))
            if n and counts[:n].sum() > 0:
                sc = np.power(np.clip(row[:n], 0.0, None), power)
                mean = float(counts[:n] @ sc / counts[:n].sum())
        self._neutral_cache[key] = mean
        return mean

    def _batter_factor_rows(self, hand: str, batter_key: str, rows: np.ndarray) -> np.ndarray:
        """The RAW batter factor on ``rows`` (the matrix score to the actor
        power when the matrices are on — SIM-523 part F: the matrix reaches
        the cell path too — else the kernel affinity; 1.0 on an unscored row)."""
        if self._batter_matrix_on():
            entry = self._actor_matrix("batter")
            assert entry is not None
            live = entry["index"].get(batter_key)
            out = np.ones(rows.size, dtype=np.float32)
            cols_all = self._batter_cols(hand)
            if live is None or cols_all is None:
                return out
            cols = cols_all[rows]
            valid = cols >= 0
            if valid.any():
                sc = entry["matrix"][live, cols[valid]].astype(np.float32)
                sc = np.where(np.isfinite(sc), sc, np.float32(1.0))
                a = float(self.actor_power.get("batter", 1.0))
                if a != 1.0:
                    sc = np.power(np.clip(sc, 0.0, None), np.float32(a)).astype(np.float32)
                out[valid] = sc
            return out
        return np.ones(rows.size, dtype=np.float32)

    # ---- SIM-523 part A: the actor score-matrix lookups --------------------
    #: Which actor embedding each matrix's keys index (fielder matrices are
    #: per position and share the fielder embedding).
    _MATRIX_EMBEDDING = {
        "batter": "batter",
        "catcher": "catcher",
        "catcher_throwing": "catcher",
        "runner_steal": "baserunner",
        "runner_adv": "baserunner",
        "pitcher_steal": "pitcher_steal",
    }

    def _actor_matrix(self, name: str) -> dict | None:
        """The named score matrix entry ({"index", "matrix"}) or None."""
        return getattr(self.a, "actor_sim", {}).get(name)

    def _emb_to_mat(self, name: str) -> np.ndarray | None:
        """Embedding row -> matrix column for ``name`` (-1 = unscored)."""
        cached = self._emb_to_mat_cache.get(name)
        if cached is not None:
            return cached
        entry = self._actor_matrix(name)
        actor = self._MATRIX_EMBEDDING.get(name, "fielder" if name.startswith("fielder_") else None)
        emb = self.a.actor_emb.get(actor) if actor else None
        if entry is None or emb is None:
            return None
        index = entry["index"]
        keys = emb.get("keys")
        if keys is None:
            keys = [k for k, _ in sorted(emb["key_index"].items(), key=lambda kv: kv[1])]
        out = np.fromiter((index.get(str(k), -1) for k in keys), dtype=np.int64, count=len(keys))
        self._emb_to_mat_cache[name] = out
        return out

    def _matrix_gather(self, name: str, live_key: str, emb_rows: np.ndarray) -> np.ndarray | None:
        """The live actor's matrix row gathered onto ``emb_rows`` (embedding
        indices per pool row, -1 = absent), raised to the factor's power.
        Neutral (1.0) where either side is unscored. None when the bundle has
        no such matrix or the power is 0 (the caller leaves the actor neutral)."""
        power = float(self.actor_power.get(name, 1.0))
        if power <= 0.0:
            return None  # SIM-523: power 0 turns this actor's factor off
        entry = self._actor_matrix(name)
        e2m = self._emb_to_mat(name)
        if entry is None or e2m is None:
            return None
        live = entry["index"].get(live_key)
        out = np.ones(len(emb_rows), dtype=np.float32)
        if live is None:
            return out
        cols = np.where(emb_rows >= 0, e2m[np.clip(emb_rows, 0, len(e2m) - 1)], -1)
        valid = cols >= 0
        if not valid.any():
            return out
        scores = entry["matrix"][live, cols[valid]].astype(np.float32)
        scores = np.where(np.isfinite(scores), scores, np.float32(1.0))
        if power != 1.0:
            scores = np.power(np.clip(scores, 0.0, None), np.float32(power)).astype(np.float32)
        out[valid] = scores
        if power != 1.0 and not valid.all():
            # SIM-523 part F: an unscored row is draw-neutral at a power.
            out[~valid] = np.float32(scores.mean())
        return out

    def _emb_z(self, actor: str) -> np.ndarray | None:
        """The z-scored embedding matrix for ``actor`` (memoized)."""
        z = self._steal_emb_z.get(actor)
        if z is not None:
            return z
        emb = self.a.actor_emb.get(actor)
        if emb is None:
            return None
        vecs, mean, std = emb.get("vecs"), emb.get("mean"), emb.get("std")
        if vecs is None or mean is None or std is None:
            return None
        z = ((vecs - mean) / std).astype(np.float32)
        self._steal_emb_z[actor] = z
        return z

    def _steal_actor_factor(
        self,
        actor: str,
        live_key: str,
        emb_rows_all: np.ndarray | None,
        rows: np.ndarray,
        feat_names: tuple[str, ...],
        matrix: str | None = None,
    ) -> np.ndarray | None:
        """The LIVE actor's engine score against each pool row's actor, read
        from the ``matrix`` the bundle carries and raised to its power; 1.0
        (neutral) for rows whose actor is unscored, None when the bundle has no
        such matrix (the actor stays neutral). ``feat_names`` named the retired
        kernel's features; the call sites still pass it (SIM-523)."""
        if emb_rows_all is None or matrix is None:
            return None
        return self._matrix_gather(matrix, live_key, emb_rows_all[rows])
