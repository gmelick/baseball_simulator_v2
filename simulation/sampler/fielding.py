"""The fielding (batted-ball) draw.

The draw hard-filters the batted-ball pool to the live base-out cell, then to
the born ball's class and the fence stage's decision, and weighs the rows by
the batter, the situation, recency and the gated extras: the platoon and
batting-side weights, the park kernel, the live defenders, the drawn pitch and
the born ball. The drawn row carries its whole base-out transition.
"""

from __future__ import annotations

import numpy as np

from simulation.sampler.base import BaseMixin
from simulation.sampler.kernels import (
    _POS_NUM_TO_NAME,
)


class FieldingMixin(BaseMixin):
    """The sampler's batted-ball methods."""

    def _init_fielding(self, platoon_off_weight: float, home_off_weight: float) -> None:
        """The fielding draw's weights, filters, counters and caches."""
        #: The weight of an opposite-hand row when the platoon weight is on
        #: (same-hand rows keep 1.0; 1.0 turns the weight off).
        self.platoon_off_weight = float(platoon_off_weight)
        #: The weight of a row whose batting side mismatches the live one
        #: (``SIM_HOME_OFF_WEIGHT``; 1.0 runs no multiplication). 0.0, the
        #: production value, is a hard match: home advantage comes from the
        #: pool's own home and away rates.
        self.home_off_weight = float(home_off_weight)
        #: The park kernel: a Gaussian on |park run factor of the live venue -
        #: that of the row's venue| (``SIM_PARK_KERNEL_SIGMA``; 0.0 = off).
        #: ``venue_run_factors`` maps (venue_id, season) to the regressed run
        #: factor; the factory loads it when the kernel is on.
        self.park_sigma = 0.0
        self.venue_run_factors: dict[tuple[int, int], float] | None = None
        #: Per-hand per-row park factor (1.0 for an unknown venue).
        self._bb_park: dict[str, np.ndarray] = {}
        #: Per-hand per-row fielder-embedding index (-1 = absent).
        self._bb_fielder_emb: dict[str, np.ndarray] = {}
        #: The pitch-similarity kernel (SIM-472): a Gaussian on the z-scored
        #: distance between the drawn pitch and each row's own pitch
        #: (0.0 = off).
        self.bb_pitch_sigma = 0.0
        #: The born-ball kernel: a Gaussian on the z-scored (exit velocity,
        #: launch angle, spray, distance) distance between the ball born in the
        #: pitch-result draw and each row's (0.0 = off), divided by each row's
        #: local density under the same kernel to the power
        #: ``bb_born_density_power`` (1.0 = the full correction, 0 = off). The
        #: kernel alone leans toward the dense, weakly hit balls and under-draws
        #: hits; the correction removes that lean.
        self.bb_born_sigma = 0.0
        self.bb_born_density_power = 1.0
        #: The born kernel's exponent per feature (``SIM_BB_BORN_PER_FEATURE``):
        #: on, the divisor is 2 sigma^2; off, 2 sigma^2 times the feature count.
        #: The kernel always measures on the born ball's class (fly balls
        #: against fly balls) when the ball has one.
        self.bb_born_per_feature = False
        #: The born ball's class (ground, line, fly, popup, bunt) as a hard
        #: filter after the cell (``SIM_BB_CLASS_FILTER``). An empty
        #: (cell, class) keeps the cell and is counted: [0] filtered,
        #: [1] fell back.
        self.bb_class_filter = False
        self.bb_class_counts = np.zeros(2, dtype=np.int64)
        #: The power on the fielding draw's batter factor (1.0 = the raw score).
        self.bb_batter_power = 1.0
        #: The park kernel applies only to a ball in the wall zone
        #: (``SIM_PARK_WALL_ZONE_ONLY``).
        self.park_wall_zone_only = False
        #: Draws whose every candidate weight underflowed to zero. The draw then
        #: runs uniform over the filtered rows: the filters are the facts, the
        #: weights only a preference.
        self.bb_zero_weight_count = 0
        #: Per-hand caches: the batter matrix column per row, the born kernel's
        #: statistics per (hand, class) and its feature matrix, and the
        #: pitch-geometry statistics.
        self._bb_bat_cols: dict[str, np.ndarray | None] = {}
        self._bb_born_stats: dict[
            tuple[str, int | None], tuple[np.ndarray, np.ndarray, np.ndarray]
        ] = {}
        self._bb_born_x: dict[str, np.ndarray] = {}
        self._bb_pgeom_stats: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        #: The current PA's hand, candidate rows and CDF, and the last drawn row.
        self._bb_hand: str | None = None
        self._bb_rows: np.ndarray | None = None
        self._bb_cdf: np.ndarray | None = None
        self._bb_last_i: int | None = None
        #: Per-hand: each row's batter-embedding index, the right-handed
        #: pitcher mask and the transition precompute (the base-out cell index).
        self._bb_pool_bat: dict[str, np.ndarray] = {}
        self._bb_throws_r: dict[str, np.ndarray] = {}
        self._bb_meta_cache: dict[str, dict] = {}
        self._fld_idx: dict[str, int] | None = None

    # ---- SIM-425: batted-ball draw (step 5) -------------------------------
    def _bb_batter_cols(self, hand: str) -> np.ndarray | None:
        """The batter matrix column per batted-ball row (-1 = unscored); None
        without the matrix. Cached per hand."""
        if hand in self._bb_bat_cols:
            return self._bb_bat_cols[hand]
        e2m = self._emb_to_mat("batter")
        pb = self._bb_pool_bat_idx(hand)
        cols = None if e2m is None else np.where(pb >= 0, e2m[np.clip(pb, 0, len(e2m) - 1)], -1)
        self._bb_bat_cols[hand] = cols
        return cols

    def _bb_batter_factor(self, hand: str, batter_key: str, rows: np.ndarray) -> np.ndarray:
        """SIM-523 (the kernel retirement): the fielding draw's batter factor on
        ``rows`` — the batter engine's score raised to ``bb_batter_power`` (at a
        power other than 1 an unscored row weighs the rows' mean scored weight,
        draw-neutral); ones on a bundle without the batter matrix."""
        if self._batter_matrix_on():
            entry = self._actor_matrix("batter")
            live = entry["index"].get(batter_key) if entry is not None else None
            cols_all = self._bb_batter_cols(hand)
            out = np.ones(rows.size, dtype=np.float32)
            if entry is None or live is None or cols_all is None:
                return out
            cols = cols_all[rows]
            valid = cols >= 0
            if not valid.any():
                return out
            sc = entry["matrix"][live, cols[valid]].astype(np.float32)
            sc = np.where(np.isfinite(sc), sc, np.float32(1.0))
            p = float(self.bb_batter_power)
            if p != 1.0:
                sc = np.power(np.clip(sc, 0.0, None), np.float32(p)).astype(np.float32)
                out[~valid] = np.float32(sc.mean())
            out[valid] = sc
            return out
        return np.ones(rows.size, dtype=np.float32)

    def _bb_pool_bat_idx(self, hand: str) -> np.ndarray:
        if hand in self._bb_pool_bat:
            return self._bb_pool_bat[hand]
        pool = self.a.bb_pools[hand]
        bemb = self.a.actor_emb.get("batter")
        if bemb is None:
            pb = np.full(pool.n, -1, dtype=np.int64)
        else:
            ki = bemb["key_index"]
            pb = np.fromiter(
                (
                    ki.get(f"{int(b)}:{int(s)}", -1)
                    for b, s in zip(pool.batter_id, pool.season, strict=False)
                ),
                dtype=np.int64,
                count=pool.n,
            )
        self._bb_pool_bat[hand] = pb
        return pb

    def _bb_park_factors(self, hand: str) -> np.ndarray | None:
        """SIM-491 part 2 (SIM-411): the per-row park run factor for the hand's
        batted-ball pool, from ``venue_run_factors`` keyed (venue_id, row season)
        with a venue-only mean fallback; 1.0 for unknown venues. None when the
        pool carries no per-row ``venue_id`` (a pre-0012 bundle) or no factor
        map is loaded — the caller then leaves the draw unweighted."""
        cached = self._bb_park.get(hand)
        if cached is not None:
            return cached
        vf = self.venue_run_factors
        pool = self.a.bb_pools[hand]
        vid = getattr(pool, "venue_id", None)
        if not vf or vid is None:
            return None
        # Venue-only mean fallback for (venue, season) pairs the map lacks.
        by_venue: dict[int, list[float]] = {}
        for (v, _s), f in vf.items():
            by_venue.setdefault(int(v), []).append(float(f))
        venue_mean = {v: sum(fs) / len(fs) for v, fs in by_venue.items()}
        out = np.fromiter(
            (
                vf.get((int(v), int(s)), venue_mean.get(int(v), 1.0))
                for v, s in zip(vid, pool.season, strict=False)
            ),
            dtype=np.float32,
            count=pool.n,
        )
        self._bb_park[hand] = out
        return out

    #: SIM-491 part 3: the OAA-centred feature subset of the fielder embedding
    #: (derived.fielder_season_metrics). v1 is the range-quality core; the
    #: SIM-476 fit may widen it toward the arm features the advancement draws
    #: already use.
    _FIELDER_BB_FEATURES = ("outs_above_average",)

    def _bb_fielder_emb_rows(self, hand: str) -> np.ndarray | None:
        """SIM-491 part 3: each batted-ball row's fielder-embedding index, keyed
        ``fielder_id:POS:row_season`` (the row's CONTEMPORANEOUS season — the
        SIM-425b survivorship lesson); -1 when absent. None when the pool has no
        fielder columns (a pre-0012 bundle) or no fielder embedding is loaded."""
        cached = self._bb_fielder_emb.get(hand)
        if cached is not None:
            return cached
        pool = self.a.bb_pools[hand]
        pos = getattr(pool, "fielder_pos", None)
        fid = getattr(pool, "fielder_id", None)
        emb = self.a.actor_emb.get("fielder")
        if pos is None or fid is None or emb is None:
            return None
        ki = emb["key_index"]
        out = np.fromiter(
            (
                ki.get(f"{int(f)}:{_POS_NUM_TO_NAME.get(int(p), '?')}:{int(s)}", -1)
                for f, p, s in zip(fid, pos, pool.season, strict=False)
            ),
            dtype=np.int64,
            count=pool.n,
        )
        self._bb_fielder_emb[hand] = out
        return out

    def _fielder_matrices_on(self) -> bool:
        """SIM-523 part A: the fielder factor applies whenever the fielder power
        is above 0 and the bundle carries at least one per-position matrix."""
        return float(self.actor_power.get("fielder", 1.0)) > 0.0 and any(
            self._actor_matrix(f"fielder_{n}") is not None for n in _POS_NUM_TO_NAME.values()
        )

    def _f_live_fielder(
        self, hand: str, rows: np.ndarray, defense_map: dict[str, int], season: int
    ) -> np.ndarray | None:
        """SIM-523 (the kernel retirement): per row, the LIVE defender at the
        row's position scored against the row's own fielder by the fielder
        engine's per-position matrix, raised to the fielder power and rescaled
        to a mean of 1 within each position. 1.0 (neutral) for rows where
        either side is unscored; None when the bundle has no fielder
        embedding (the pool rows cannot be keyed)."""
        emb = self.a.actor_emb.get("fielder")
        row_emb = self._bb_fielder_emb_rows(hand)
        if emb is None or row_emb is None:
            return None
        ki = emb["key_index"]
        # The live defender's embedding index per position NUMBER (the row's
        # fielder_pos vocabulary), -1 when the defense map / embedding lacks it.
        live_by_pos = np.full(10, -1, dtype=np.int64)
        for p, name in _POS_NUM_TO_NAME.items():
            pid = defense_map.get(name)
            if pid:
                live_by_pos[p] = ki.get(f"{int(pid)}:{name}:{int(season)}", -1)
        pool = self.a.bb_pools[hand]
        pos = np.clip(np.asarray(pool.fielder_pos)[rows].astype(np.int64), 0, 9)
        live_idx = live_by_pos[pos]
        row_idx = row_emb[rows]
        out = np.ones(len(rows), dtype=np.float32)
        if not ((live_idx >= 0) & (row_idx >= 0)).any():
            return out
        # The fielder engine's per-position score matrix, gathered per
        # position; rows at a position without a matrix or a live key stay
        # neutral. The per-position mean-1 rescale below still applies (a
        # factor must never move balls between positions).
        valid = np.zeros(len(rows), dtype=bool)
        for p, name in _POS_NUM_TO_NAME.items():
            at = pos == p
            if not at.any():
                continue
            pid = defense_map.get(name)
            if not pid:
                continue
            f = self._matrix_gather(
                f"fielder_{name}", f"{int(pid)}:{name}:{int(season)}", row_idx[at]
            )
            if f is None:
                continue
            out[at] = f
            valid |= at & (row_idx >= 0)
        # SIM-476: the factor must not move batted balls BETWEEN positions.
        # Where a ball goes is batted-ball physics (the batter/situation
        # kernels); the fielder factor's job is to pick WHICH play at that
        # position, given the live defender. Raw weights break that: a
        # position whose live defender scores well against the pool outweighs
        # a position with a poorly scored defender, so the draw redistributes
        # balls toward well-matched positions (measured on the retired kernel:
        # the OF share of drawn balls rose 52.7% -> 57.4% at sigma=0.5, which
        # alone inflates hits ~+6% — the 2026-09-01 lane red). Normalizing to
        # a MEAN of 1 within each position keeps the within-position
        # discrimination and kills the cross-position shift; it also makes
        # missing-identity rows (weight 1.0) exactly draw-neutral.
        for p in np.unique(pos[valid]):
            m = valid & (pos == p)
            mean_w = float(out[m].mean())
            if mean_w > 0.0:
                out[m] = out[m] / np.float32(mean_w)
            else:
                # Every weight at this position is zero (a live defender scored
                # 0 against every row): the factor has no usable discrimination
                # here, so it goes NEUTRAL — never zero, which would starve the
                # position of batted balls entirely.
                out[m] = np.float32(1.0)
        return out

    def _bb_same_hand_mask(self, hand: str, pitcher_throws: str) -> np.ndarray | None:
        """SIM-413: boolean mask of batted-ball pool rows whose pitcher threw the
        SAME hand as ``pitcher_throws``. None when the pool carries no per-row
        ``p_throws`` (a legacy bundle) so the caller leaves the draw unweighted."""
        pool = self.a.bb_pools[hand]
        pt = getattr(pool, "p_throws", None)
        if pt is None:
            return None
        tr = self._bb_throws_r.get(hand)
        if tr is None:
            tr = np.asarray(pt, dtype=object) == "R"
            self._bb_throws_r[hand] = tr
        return tr if pitcher_throws == "R" else ~tr

    def has_transition(self, hand: str) -> bool:
        """SIM-511: True when this hand's batted-ball pool carries the SIM-510
        transition columns (a sim510.1+ bundle). False = a data defect: SIM-486
        deleted the legacy soft draw, so the batted-ball draw raises."""
        pool = self.a.bb_pools.get(hand)
        return (
            pool is not None
            and getattr(pool, "r1_dest", None) is not None
            and getattr(pool, "dest_ok", None) is not None
            and getattr(pool, "is_air", None) is not None
        )

    def _transition_meta(self, hand: str) -> dict | None:
        """SIM-511 per-hand one-time precompute: the base-out cell index over
        CONSISTENT transition rows (``dest_ok`` — SIM-510's outs-accounting
        guard) plus the soft-kernel situation columns.

        The hard filter is the base-out cell ALONE — 8 runner configurations
        × 3 out states = 24 cells, all common (owner ruling 2026-08-19). The
        filter is essential, so it never relaxes; the count stays SOFT
        conditioning; an EMPTY cell raises as a data defect. Never widen.
        """
        meta = self._bb_meta_cache.get(hand)
        if meta is not None:
            return meta
        if not self.has_transition(hand):
            return None
        pool = self.a.bb_pools[hand]
        ok = pool.dest_ok.astype(bool)
        outs = pool.sit[:, 2].astype(np.int64)
        rs = pool.sit[:, 3].astype(np.int64)
        cells: dict[tuple[int, int], np.ndarray] = {}
        for rstate in range(8):
            for o in range(3):
                rows = np.nonzero(ok & (rs == rstate) & (outs == o))[0]
                if len(rows):
                    cells[(rstate, o)] = rows
        # An empty cell raises AT DRAW TIME (battedball_new_pa), where the
        # live state names the hole — the defect surfaces on first contact.
        # The soft situation kernel runs over the NON-exact dims only:
        # balls, strikes, inning, score_diff (sit cols 0, 1, 4, 5).
        meta = {"cells": cells, "soft": np.ascontiguousarray(pool.sit[:, [0, 1, 4, 5]])}
        self._bb_meta_cache[hand] = meta
        return meta

    def battedball_new_pa(
        self,
        hand: str,
        batter_key: str,
        state: np.ndarray,
        pitcher_throws: str | None = None,
        bat_home: bool | None = None,
        park_run_factor: float | None = None,
        defense_map: dict[str, int] | None = None,
        live_season: int | None = None,
        pitch_geom: np.ndarray | None = None,
        born_bb: dict | None = None,
        venue_id: int | None = None,
    ) -> None:
        """Assemble the batted-ball weight CDF for the PA (f_batter · f_situation · recency).

        SIM-523 part B: when ``born_bb`` (the result row's own batted ball,
        :meth:`last_born_batted_ball`) is supplied AND :attr:`bb_born_sigma`
        > 0, a Gaussian on the z-scored (exit velocity, launch angle, spray,
        distance) distance between that ball and each cell row's pulls the
        draw toward plays on similar batted balls — the ball born in the
        pitch-result draw is what gets fielded. Omitted / sigma 0 -> unchanged.

        SIM-523 part C: with :attr:`bb_class_filter` on and a born ball whose
        class is known, the cell is hard-filtered to that class (an empty
        class falls back to the cell); with
        :attr:`park_wall_zone_only` on the park kernel applies only when the
        born ball is in the wall zone. With :attr:`fence_stage` on and
        ``venue_id`` (the live park) given, the born ball's carry against
        that park's fence decides home run / no home run / the band before
        the draw (:meth:`fence_decision`); ``live_season`` picks the park's
        fence group (SIM-478).

        SIM-478 §11 / §12 / §12.7: with :attr:`carry_offset` on and a
        document that carries ``carry_offset_ft``, the born ball's distance
        is first re-expressed in the live park's air (:meth:`carry_of`) — one
        number the fence check, the kernel and the wall margin all read. The
        born kernel measures on the born ball's CLASS ruler (per-class mean
        and spread), with the exponent per feature when
        :attr:`bb_born_per_feature` is on. With :attr:`bb_margin_band` > 0,
        a wall-zone air ball the fence did not call over draws only rows whose
        margin to their OWN wall sits within the band of the born ball's margin
        to the live wall (:meth:`_bb_margin_stage`).

        SIM-518 (SIM-472): when ``pitch_geom`` (the DRAWN pitch's ten geometry
        values, :meth:`last_pitch_geom`) is supplied AND :attr:`bb_pitch_sigma`
        > 0 AND the pool carries ``pgeom``, a Gaussian on the z-scored distance
        between that pitch and each row's own pitch pulls the draw toward
        batted balls hit off similar pitches — the pitch and the contact
        agree. Omitted / sigma 0 / a pre-sim518 export -> the draw is unchanged.

        SIM-511: the draw HARD-filters the exact base-out cell (the drawn row
        must be legal in the live state — that is what makes "the drawn row is
        the play" safe), and the situation kernel runs over the remaining soft
        dims (balls, strikes, inning, score_diff). A bundle without the
        transition columns raises (SIM-486 deleted the legacy soft draw).

        SIM-413: when ``pitcher_throws`` ('L'/'R') is supplied AND the pool carries
        per-row ``p_throws``, softly reweight toward same-hand-matchup rows (opposite
        hand rows ×:attr:`platoon_off_weight`) so the drawn batted ball reflects the
        live platoon matchup. Omitted / legacy pool -> the draw is unchanged.

        SIM-491: when ``bat_home`` is supplied AND the pool carries per-row
        ``bat_home`` (migration 0019), softly reweight toward rows whose batting
        side matches the live one (mismatched rows ×:attr:`home_off_weight`) —
        the SIM-412 home-field advantage as a draw weight. Omitted / weight 1.0 /
        legacy pool -> the draw is unchanged.

        SIM-491 part 2 (SIM-411): when ``park_run_factor`` is supplied,
        :attr:`park_sigma` > 0 AND a venue-factor map is loaded, a Gaussian on
        the |live − row| park-factor delta pulls the draw toward
        run-environment-similar parks. Omitted / sigma 0 / no map -> the draw
        is unchanged.

        SIM-523 (the kernel retirement): when ``defense_map`` (position name
        -> live defender id) is supplied and the bundle carries the per-position
        fielder matrices, each row is weighted by the LIVE defender's engine
        score against the row's own fielder, raised to the fielder power.
        Omitted / no matrices / a pre-0012 bundle -> the draw is unchanged."""
        self._bb_hand = hand
        pool = self.a.bb_pools[hand]
        sv = np.asarray(state, dtype=np.float32)
        meta = self._transition_meta(hand)
        if meta is None:
            raise RuntimeError(
                f"SIM-486: the {hand}-hand batted-ball pool carries no transition "
                "columns (r1_dest / dest_ok / is_air) — a pre-sim510.1 bundle. Rebuild "
                "the engine artifacts; there is no legacy soft-draw path."
            )
        # --- SIM-511: the hard base-out cell ---------------------------
        rstate = int(sv[3]) & 0b111
        o = min(max(int(sv[2]), 0), 2)
        rows = meta["cells"].get((rstate, o))
        if rows is None:
            raise RuntimeError(
                f"SIM-511: base-out cell (runners_state={rstate}, outs={o}) is "
                f"EMPTY in the {hand}-hand batted-ball pool — a data defect. "
                "The base-out filter is essential and never widens (owner "
                "ruling 2026-08-19); rebuild the pool and investigate."
            )
        # --- SIM-478 §11: the born ball's carry in the LIVE park's air -----
        # One working copy carries carry_live as its distance. Its venue is
        # the live park: the ball now sits in that park's air, so a second
        # ``carry_of`` on it shifts by exactly 0. The class filter, the fence
        # stage, the margin band and the kernel all read the copy.
        born = self._born_in_live_air(born_bb, venue_id)
        self._fence_last_dec = None
        # --- SIM-523 part C: the batted-ball CLASS, the third hard filter --
        if born is not None and self.bb_class_filter:
            rows = self._bb_class_rows(hand, meta, (rstate, o), rows, born.get("cls"))
        # --- SIM-523 part C4: the fence stage ------------------------------
        if born is not None and self.fence_stage:
            rows = self._fence_rows(hand, meta, (rstate, o), rows, born, venue_id, live_season)
        # --- SIM-478 §12.7: the wall-margin band ---------------------------
        # A banded set is one subset per born margin. The density cache keys
        # a set by its ends and size, which names only the nested sets of the
        # cell, the class and the fence; a banded set skips the cache.
        banded = False
        if born is not None and self.bb_margin_band > 0.0:
            unbanded = rows
            rows = self._bb_margin_stage(hand, rows, born, venue_id, live_season)
            banded = rows is not unbanded
        # SIM-523 (the kernel retirement): the batter engine's score when the
        # matrices are on; the retired kernel's affinity off (byte-identical).
        f_bat = self._bb_batter_factor(hand, batter_key, rows)
        diff = meta["soft"][rows] - sv[[0, 1, 4, 5]]
        d2 = np.einsum("ij,ij->i", diff, diff)
        f_sit = np.exp(-d2 / (2.0 * self.sit_sigma**2 * diff.shape[1])).astype(np.float32)
        w = self._apply_asof(f_bat * f_sit * pool.recency[rows], pool, rows)
        if pitcher_throws and self.platoon_off_weight != 1.0:
            same = self._bb_same_hand_mask(hand, pitcher_throws)
            if same is not None:
                w = w * np.where(same[rows], np.float32(1.0), np.float32(self.platoon_off_weight))
        if bat_home is not None and self.home_off_weight != 1.0:
            bh = getattr(pool, "bat_home", None)
            if bh is not None:
                match = (bh[rows] > 0) == bool(bat_home)
                w = w * np.where(match, np.float32(1.0), np.float32(self.home_off_weight))
        if (
            park_run_factor is not None
            and self.park_sigma > 0.0
            # SIM-523 part C: the park matters only to a ball that can reach
            # its wall — when the zone rule is on and the ball is known.
            and (born is None or not self.park_wall_zone_only or self.in_wall_zone(born))
        ):
            pf = self._bb_park_factors(hand)
            if pf is not None:
                d = pf[rows] - np.float32(park_run_factor)
                w = w * np.exp(-(d * d) / (2.0 * self.park_sigma**2)).astype(np.float32)
        if defense_map and self._fielder_matrices_on():
            ff = self._f_live_fielder(hand, rows, defense_map, int(live_season or 0))
            if ff is not None:
                w = w * ff
        if pitch_geom is not None and self.bb_pitch_sigma > 0.0:
            fpg = self._f_pitch_similarity(hand, rows, pitch_geom)
            if fpg is not None:
                w = w * fpg
        if born is not None and self.bb_born_sigma > 0.0:
            fbb = self._f_born_similarity(hand, rows, born)
            if fbb is not None:
                w = w * fbb
                if self.bb_born_density_power != 0.0:
                    w = w * self._born_inv_density(
                        hand, meta, rows, born.get("cls"), cache=not banded
                    )
        cdf = np.cumsum(w, dtype=np.float64)
        if not (cdf[-1] > 0.0) or not np.isfinite(cdf[-1]):
            # Every weight underflowed (a narrow candidate set under tight
            # kernels): keep the filtered rows, drop the preference.
            self.bb_zero_weight_count += 1
            cdf = np.cumsum(np.ones(len(rows), dtype=np.float64))
        self._bb_rows = rows
        self._bb_cdf = cdf

    # ---- SIM-523 part C: the fielding draw's class filter, speed, wall zone
    def _bb_class_rows(
        self, hand: str, meta: dict, cell: tuple[int, int], rows: np.ndarray, cls: int | None
    ) -> np.ndarray:
        """The cell's rows of the born ball's class (cached per cell and
        class); the whole cell when the class is unknown, the pool carries
        no class, or the class is empty in this cell (counted)."""
        bc = getattr(self.a.bb_pools[hand], "bb_class", None)
        if bc is None or cls is None or int(cls) <= 0:
            return rows
        cache = meta.setdefault("class_cells", {})
        key = (cell, int(cls))
        sub = cache.get(key)
        if sub is None:
            sub = rows[bc[rows] == int(cls)]
            cache[key] = sub
        if sub.size == 0:
            self.bb_class_counts[1] += 1
            return rows
        self.bb_class_counts[0] += 1
        return sub

    # ---- SIM-523 part B: the born batted ball on the fielding draw ---------
    def _bb_born_features(self, hand: str) -> np.ndarray:
        """The batted-ball pool's (exit velocity, launch angle, spray[,
        distance]) matrix — distance only when the pool carries it."""
        x = self._bb_born_x.get(hand)
        if x is None:
            pool = self.a.bb_pools[hand]
            if pool.hit_dist is not None:
                x = np.column_stack([pool.geom[:, :3], pool.hit_dist]).astype(np.float32)
            else:
                x = np.ascontiguousarray(pool.geom[:, :3], dtype=np.float32)
            self._bb_born_x[hand] = x
        return x

    def _bb_born_z_stats(
        self, hand: str, cls: int | None = None
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """The born kernel's ruler: (mean, std, complete-row mask) of the
        pool's (exit velocity, launch angle, spray[, distance]). SIM-478 §12
        — the CLASS RULER: with ``cls`` given and the pool carrying the
        class, the mean and spread come from the pool's complete rows of that
        class (fly balls measured against fly balls); the pool-wide
        statistics when the ball has no class, the pool no class column, or
        the class fewer than two complete rows. The mask is completeness
        alone (finite, exit velocity above 0, a distance above 0 when the
        pool carries one), shared by every class. Cached per (hand, class)."""
        key = (hand, int(cls) if cls is not None and int(cls) > 0 else None)
        cached = self._bb_born_stats.get(key)
        if cached is not None:
            return cached
        x = self._bb_born_features(hand)
        valid = np.isfinite(x).all(axis=1) & (x[:, 0] > 0.0)
        if x.shape[1] == 4:
            valid &= x[:, 3] > 0.0
        sel = valid
        if key[1] is not None:
            bc = getattr(self.a.bb_pools[hand], "bb_class", None)
            if bc is None:
                stats = self._bb_born_z_stats(hand, None)
                self._bb_born_stats[key] = stats
                return stats
            sel = valid & (np.asarray(bc) == key[1])
            if int(np.count_nonzero(sel)) < 2:
                stats = self._bb_born_z_stats(hand, None)
                self._bb_born_stats[key] = stats
                return stats
        if sel.any():
            mean = x[sel].mean(axis=0).astype(np.float32)
            std = x[sel].std(axis=0).astype(np.float32)
        else:
            mean = np.zeros(x.shape[1], dtype=np.float32)
            std = np.ones(x.shape[1], dtype=np.float32)
        std = np.where(std > 1e-6, std, np.float32(1.0)).astype(np.float32)
        stats = (mean, std, valid)
        self._bb_born_stats[key] = stats
        return stats

    def _born_scale(self, n_features: int) -> float:
        """The born kernel's exponent divisor: 2 sigma^2 x the feature count
        (today's formula), or 2 sigma^2 with the exponent per feature on
        (SIM-478 §12)."""
        n = 1 if self.bb_born_per_feature else int(n_features)
        return 2.0 * float(self.bb_born_sigma) ** 2 * n

    #: Reference rows per candidate set for the born kernel's density estimate.
    _BORN_DENSITY_REFS = 256

    def _born_inv_density(
        self,
        hand: str,
        meta: dict,
        rows: np.ndarray,
        cls: int | None = None,
        cache: bool = True,
    ) -> np.ndarray:
        """The inverse local density of every candidate row under the born
        kernel — each row's mean kernel value against a fixed random subset of
        the candidate rows (cached per candidate set, bandwidth and power; the
        subset comes from a generator seeded by the set, so the sampler's own
        stream is untouched), floored at 5% of the median, raised to
        ``bb_born_density_power`` and normalized to a mean of 1; a row with
        incomplete features is exactly neutral. The candidate set is keyed by
        its first and last row and its size — the nested sets of the cell,
        the class filter and the fence stage. A margin-banded set (SIM-478
        §12.7) is one subset per born margin. Two banded sets can share that
        key and differ inside. The caller passes ``cache=False`` for a banded
        set; the estimate then neither reads nor writes the cache. SIM-478
        §12 — the key also carries the born ball's class (the ruler) and the
        per-feature flag, and the estimate runs under the same ruler and
        exponent as :meth:`_f_born_similarity`."""
        store = meta.setdefault("born_density", {})
        cls_key = int(cls) if cls is not None and int(cls) > 0 else None
        key = (
            int(rows[0]) if rows.size else -1,
            int(rows[-1]) if rows.size else -1,
            int(rows.size),
            float(self.bb_born_sigma),
            float(self.bb_born_density_power),
            cls_key,
            bool(self.bb_born_per_feature),
        )
        hit = store.get(key) if cache else None
        if hit is not None:
            return hit
        mean, std, valid_all = self._bb_born_z_stats(hand, cls_key)
        x = self._bb_born_features(hand)
        valid = valid_all[rows]
        out = np.ones(len(rows), dtype=np.float32)
        vr = rows[valid]
        if vr.size:
            z = (x[vr] - mean) / std
            m = int(min(self._BORN_DENSITY_REFS, vr.size))
            if m < vr.size:
                seed = abs(hash(key[:3])) % (2**32)
                ref = z[np.random.default_rng(seed).choice(vr.size, size=m, replace=False)]
            else:
                ref = z
            scale = self._born_scale(z.shape[1])
            ref_sq = np.einsum("ij,ij->i", ref, ref)
            dens = np.empty(vr.size, dtype=np.float64)
            for start in range(0, vr.size, 8192):
                zb = z[start : start + 8192]
                d2 = np.einsum("ij,ij->i", zb, zb)[:, None] + ref_sq[None, :] - 2.0 * (zb @ ref.T)
                dens[start : start + 8192] = np.exp(-np.maximum(d2, 0.0) / scale).mean(axis=1)
            floor = 0.05 * float(np.median(dens))
            inv = np.power(np.maximum(dens, max(floor, 1e-30)), -float(self.bb_born_density_power))
            inv = inv / inv.mean()
            out[valid] = inv.astype(np.float32)
        if cache:
            store[key] = out
        return out

    def _f_born_similarity(self, hand: str, rows: np.ndarray, born: dict) -> np.ndarray | None:
        """The batted-ball similarity factor over the cell ``rows`` against the
        born ball: a Gaussian on the z-scored feature distance, normalized to
        a mean of 1 over the rows with complete data (an incomplete row is
        exactly neutral). None when the born ball is incomplete. SIM-478 §12:
        the ruler is the born ball's class's (``born["cls"]``), the distance
        the born ball's ``dist`` (carry_live when the offset is on)."""
        mean, std, valid_all = self._bb_born_z_stats(hand, born.get("cls"))
        x = self._bb_born_features(hand)
        live_vals = [born.get("ev"), born.get("la"), born.get("spray")]
        if x.shape[1] == 4:
            live_vals.append(born.get("dist"))
        if any(v is None for v in live_vals):
            return None
        live_arr = np.asarray(live_vals, dtype=np.float32)
        if not bool(np.isfinite(live_arr).all()) or not (float(live_arr[0]) > 0.0):
            return None
        live = (live_arr - mean) / std
        valid = valid_all[rows]
        out = np.ones(len(rows), dtype=np.float32)
        if valid.any():
            diff = (x[rows[valid]] - mean) / std - live
            d2 = np.einsum("ij,ij->i", diff, diff)
            f = np.exp(-d2 / self._born_scale(diff.shape[1])).astype(np.float32)
            mean_w = float(f.mean())
            out[valid] = f / np.float32(mean_w) if mean_w > 0.0 else np.float32(1.0)
        return out

    def _bb_pgeom_z_stats(self, hand: str) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
        """SIM-518 (SIM-472): per-column mean/std of the batted-ball pool's
        producing-pitch geometry over its all-finite rows, plus that row mask.
        Cached per hand; None when the pool carries no ``pgeom``."""
        cached = self._bb_pgeom_stats.get(hand)
        if cached is not None:
            return cached
        pg = getattr(self.a.bb_pools[hand], "pgeom", None)
        if pg is None:
            return None
        valid = np.isfinite(pg).all(axis=1)
        if valid.any():
            mean = pg[valid].mean(axis=0).astype(np.float32)
            std = pg[valid].std(axis=0).astype(np.float32)
        else:
            mean = np.zeros(pg.shape[1], dtype=np.float32)
            std = np.ones(pg.shape[1], dtype=np.float32)
        std = np.where(std > 1e-6, std, np.float32(1.0)).astype(np.float32)
        stats = (mean, std, valid)
        self._bb_pgeom_stats[hand] = stats
        return stats

    def _f_pitch_similarity(
        self, hand: str, rows: np.ndarray, pitch_geom: np.ndarray
    ) -> np.ndarray | None:
        """SIM-518 (SIM-472): the per-row pitch-similarity factor over the
        cell ``rows`` — a Gaussian on the z-scored 10-dim distance between
        the drawn pitch and each row's producing pitch, NORMALIZED to a mean
        of 1 over the rows with complete geometry; a row missing any value is
        exactly neutral. None when the pool carries no ``pgeom``."""
        stats = self._bb_pgeom_z_stats(hand)
        if stats is None:
            return None
        mean, std, valid_all = stats
        pg = self.a.bb_pools[hand].pgeom
        assert pg is not None
        live = (np.asarray(pitch_geom, dtype=np.float32) - mean) / std
        valid = valid_all[rows]
        out = np.ones(len(rows), dtype=np.float32)
        if valid.any():
            z = (pg[rows[valid]] - mean) / std
            diff = z - live
            d2 = np.einsum("ij,ij->i", diff, diff)
            f = np.exp(-d2 / (2.0 * self.bb_pitch_sigma**2 * diff.shape[1])).astype(np.float32)
            mean_w = float(f.mean())
            out[valid] = f / np.float32(mean_w) if mean_w > 0.0 else np.float32(1.0)
        return out

    def battedball_draw(self) -> tuple[str, int, int, float]:
        """Draw one batted ball -> (event, result_hits, result_outs, launch_angle).

        SIM-425: launch_angle (geom col 1) is returned so the resolver can tell a
        fly out (tag-up eligible) from a ground out for productive-out advancement.
        SIM-511: on a transition bundle the drawn index maps through the PA's
        base-out cell; read the row's transition via :meth:`last_transition`.
        """
        if self._bb_hand is None or self._bb_cdf is None or self._bb_cdf[-1] <= 0:
            self._bb_last_i = None
            return ("field_out", 0, 1, 0.0)
        pool = self.a.bb_pools[self._bb_hand]
        i = int(np.searchsorted(self._bb_cdf, self.rng.random() * self._bb_cdf[-1]))
        if self._bb_rows is not None:
            i = int(self._bb_rows[min(i, len(self._bb_rows) - 1)])
        else:
            i = min(i, pool.n - 1)
        self._bb_last_i = i  # SIM-425b: remember the row for the fielder lookup
        return (
            str(pool.event[i]),
            int(pool.result_hits[i]),
            int(pool.result_outs[i]),
            float(pool.geom[i, 1]),
        )

    def last_transition(self) -> dict | None:
        """SIM-511: the drawn row's full transition, or None when no draw has
        happened yet.

        Keys: ``r1``/``r2``/``r3`` — the destination of the pre-pitch runner
        on that base (-1 = no runner; 4 = scored; 3/2/1 = the post base;
        0 = retired); ``batter`` — the batter-runner's destination (0 = out);
        ``adv1``/``adv2``/``adv3`` — thrown-out-advancing flags (a
        discretionary out, never a force or a doubled-off runner);
        ``is_air`` — a caught-ball row (the tag-up shape); ``ev``/``spray``/
        ``dist`` — the throw geometry for the SIM-512 advancement kernel.
        """
        i = self._bb_last_i
        if i is None or self._bb_hand is None or not self.has_transition(self._bb_hand):
            return None
        pool = self.a.bb_pools[self._bb_hand]
        if pool.r1_adv_out is None or pool.spray_raw is None or pool.hit_dist is None:
            return None
        return {
            "r1": int(pool.r1_dest[i]),
            "r2": int(pool.r2_dest[i]),
            "r3": int(pool.r3_dest[i]),
            "batter": int(pool.batter_dest[i]),
            "adv1": bool(pool.r1_adv_out[i]),
            "adv2": bool(pool.r2_adv_out[i]),
            "adv3": bool(pool.r3_adv_out[i]),
            "is_air": bool(pool.is_air[i]),
            "ev": float(pool.geom[i, 0]),
            "spray": float(pool.spray_raw[i]),
            "dist": float(pool.hit_dist[i]),
        }

    def last_battedball_fielder(self) -> tuple[int, int, int] | None:
        """SIM-425b: ``(fielded_by_position, fielder_player_id, season)`` of the row
        the last :meth:`battedball_draw` returned, or None when the pool lacks the
        fielder columns (a legacy bundle) or no draw has happened.

        The ``season`` is the pool ROW's own season, so the caller scores that
        fielder by their CONTEMPORANEOUS OAA — not the game season, which would drop
        any pool fielder lacking a game-season row (a survivorship filter that biases
        the live-vs-pool comparison)."""
        i = self._bb_last_i
        if i is None or self._bb_hand is None:
            return None
        pool = self.a.bb_pools[self._bb_hand]
        pos, fid = getattr(pool, "fielder_pos", None), getattr(pool, "fielder_id", None)
        if pos is None or fid is None:
            return None
        return (int(pos[i]), int(fid[i]), int(pool.season[i]))

    def fielder_quality(self, fielder_id: int, position: str, season: int) -> float | None:
        """SIM-425b: the fielder's outs-above-average (per the fielder embedding,
        keyed ``player_id:position:season``), or None when the fielder / feature is
        absent. A higher value == a better defender at that position; the resolver
        nudges out↔hit by the delta between the live defender and the pool play's
        fielder. Returns None (-> neutral) on a legacy bundle or an unknown fielder."""
        femb = self.a.actor_emb.get("fielder")
        if femb is None or not fielder_id:
            return None
        feats = femb.get("features")
        idx = femb["key_index"].get(f"{int(fielder_id)}:{position}:{int(season)}")
        if feats is None or idx is None:
            return None
        if self._fld_idx is None:
            self._fld_idx = {f: i for i, f in enumerate(feats)}
        oaa_i = self._fld_idx.get("outs_above_average")
        if oaa_i is None:
            return None
        v = float(femb["vecs"][idx][oaa_i])
        return v if np.isfinite(v) else None

    def has_battedball(self) -> bool:
        return bool(self.a.bb_pools)
