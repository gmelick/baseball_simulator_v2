"""The steal and pickoff draws over the steal opportunity pool.

The pool holds one row per pitch with a stealable runner, plus the pickoff
rows. The pickoff draw reads the count group before the pitch; the steal draw
reads the real pitches of the same count and pitch class after it. Without the
pitch class, one draw before the pitch answers both questions.
"""

from __future__ import annotations

import logging

import numpy as np

from pipeline.batch.engine_artifacts import (
    STEAL_PITCH_CLASS_CODE,
    StealPool,
)
from simulation.sampler.base import BaseMixin
from simulation.sampler.kernels import (
    _count_cell,
    _group_rows,
)

#: The steal pool's warning keeps the sampler's logger name.
log = logging.getLogger("simulation.full_pool_sampler")


class StealMixin(BaseMixin):
    """The sampler's steal and pickoff methods."""

    def _init_steal(self) -> None:
        """The steal draws' bandwidth, switch and caches."""
        #: The steal draw's score-margin bandwidth (the situation, not an actor).
        self.steal_score_sigma = 2.0
        #: The running game on the pitch (``SIM_STEAL_PITCH_CLASS``, default
        #: on). On a bundle whose steal pool carries the pitch class, the loop
        #: draws the pickoff before the pitch and the steal after it, among
        #: real pitches of the same class. False, or a bundle without classes:
        #: the single pre-pitch draw.
        self.steal_pitch_class = True
        self._steal_classes: bool | None = None
        #: Per-target precompute: the group indexes and the actor row gathers.
        self._steal_meta_cache: dict[str, dict] = {}

    # ---- SIM-474: the steal draw over the opportunity pool -----------------

    #: The steal-relevant feature subsets. Each actor's similarity is a gaussian
    #: kernel over THESE z-scored columns only, so a runner is "similar" by how
    #: he runs, not by how he tags up.
    _RUNNER_STEAL_FEATURES = ("sprint_speed", "sb_attempt_rate", "sb_success_rate", "cs_rate")
    _CATCHER_STEAL_FEATURES = (
        "pop_time_mean",
        "arm_strength_mean",
        "cs_rate",
        "steal_attempt_rate_against",
    )
    #: SIM-504 item 3 added pickoff_rate/stepoff_rate (raw.play_events
    #: disengagements). `_steal_feat_cols` skips names a legacy artifact lacks,
    #: so an old bundle degrades to the 3-feature kernel instead of failing.
    _PITCHER_STEAL_FEATURES = (
        "sb_against_per_9",
        "cs_rate_forced",
        "steal_attempt_rate_allowed",
        "pickoff_rate",
        "stepoff_rate",
    )

    def has_steal_pool(self) -> bool:
        return bool(self.a.steal_pools)

    def _steal_meta(self, target: str) -> dict | None:
        """Per-target one-time precompute: the group indexes plus per-row
        embedding-row gathers for the three actors.

        SIM-554: up to three indexes over the pool's rows.

          * ``cells``: (outs, balls, strikes) -> the PITCH rows. The single
            pre-pitch draw reads it (the flag off, or a bundle without
            classes). On a bundle with classes the pickoff rows are left out,
            so that draw reads the rows it read before migration 0031.
          * ``pickoff_cells``: (outs, balls, strikes) -> EVERY row, the pitch
            rows and the pickoff rows. The pickoff draw's count group.
          * ``class_cells``: (outs, balls, strikes, class code) -> the pitch
            rows of one class. The steal draw's group.

        The last two exist only when EVERY pitch row of the pool carries a
        class. A pickoff row is told apart by ``is_pickoff_row`` (by class code
        0 on a pool built without that column). A pool where some pitch rows
        have no class (a season no builder rebuilt since migration 0031) keeps
        the single pre-pitch draw over all its pitch rows, and says so once.
        """
        meta = self._steal_meta_cache.get(target)
        if meta is not None:
            return meta
        pool = self.a.steal_pools.get(target)
        if pool is None or pool.n == 0:
            return None
        sit = pool.sit  # cols: count_balls, count_strikes, outs, score_diff
        key = (sit[:, 2].astype(np.int64) * 100 + sit[:, 0].astype(np.int64) * 10) + sit[
            :, 1
        ].astype(np.int64)
        every = np.arange(pool.n, dtype=np.int64)
        pc = getattr(pool, "pitch_class", None)
        code = np.asarray(pc, dtype=np.int64) if pc is not None else None
        mark = getattr(pool, "is_pickoff_row", None)
        if mark is not None:
            pickoff = np.asarray(mark).astype(bool)
        elif code is not None:
            pickoff = code == 0  # a pool built without the mark: code 0 is a pickoff row
        else:
            pickoff = np.zeros(pool.n, dtype=bool)
        # A pickoff row is never a pitch: it leaves the pitch groups.
        pitch_rows = every[~pickoff]
        classes = code is not None and pitch_rows.size > 0 and bool(np.all(code[pitch_rows] > 0))
        if code is not None and not classes and pitch_rows.size > 0:
            log.warning(
                "steal_pool[%s]: %d of %d pitch rows carry no pitch class; this bundle "
                "keeps the single pre-pitch steal draw (rebuild those seasons with "
                "builder sim554.1, SIM-554)",
                target,
                int(np.count_nonzero(code[pitch_rows] <= 0)),
                int(pitch_rows.size),
            )
        cells = {_count_cell(k): r for k, r in _group_rows(key, pitch_rows).items()}

        def _rows(actor: str, ids: np.ndarray) -> np.ndarray | None:
            emb = self.a.actor_emb.get(actor)
            if emb is None:
                return None
            kidx = emb["key_index"]
            return np.fromiter(
                (
                    kidx.get(f"{int(a)}:{int(s)}", -1)
                    for a, s in zip(ids, pool.season, strict=False)
                ),
                dtype=np.int64,
                count=pool.n,
            )

        meta = {
            "cells": cells,
            "runner_rows": _rows("baserunner", pool.runner_id),
            "pitcher_rows": _rows("pitcher_steal", pool.pitcher_id),
            "catcher_rows": _rows("catcher", pool.catcher_id),
        }
        if classes and code is not None:
            pickoff_cells = {_count_cell(k): r for k, r in _group_rows(key, every).items()}
            meta["pickoff_cells"] = pickoff_cells
            # The count groups that hold at least one pickoff outcome: in any
            # other group the pickoff draw cannot answer "picked off", so it
            # skips the weights (and the random number).
            po = np.asarray(pool.pickoff_out, dtype=bool) | np.asarray(
                pool.pickoff_error, dtype=bool
            )
            meta["pickoff_live"] = {c for c, r in pickoff_cells.items() if bool(po[r].any())}
            meta["class_cells"] = {
                (*_count_cell(k // 10), k % 10): r
                for k, r in _group_rows(key * 10 + code, pitch_rows).items()
            }
        self._steal_meta_cache[target] = meta
        return meta

    def has_steal_classes(self) -> bool:
        """SIM-554: True when every steal pool carries the pitch class on every
        pitch row (a bundle exported after migration 0031 from rebuilt
        seasons). The loop's new order needs it: the pickoff draw before the
        pitch, the steal draw in the class group. One unclassed target keeps
        the single pre-pitch draw for both, so the two targets never run
        different orders."""
        if self._steal_classes is None:
            metas = [self._steal_meta(t) for t in self.a.steal_pools]
            live = [m for m in metas if m is not None]
            self._steal_classes = bool(live) and all("class_cells" in m for m in live)
        return self._steal_classes

    def _steal_feat_cols(self, actor: str, names: tuple[str, ...]) -> np.ndarray | None:
        emb = self.a.actor_emb.get(actor)
        if emb is None:
            return None
        feats = emb.get("features")
        if feats is None:
            return None
        fmap = {f: i for i, f in enumerate(feats)}
        cols = [fmap[n] for n in names if n in fmap]
        return np.asarray(cols, dtype=np.int64) if cols else None

    def _steal_pick(self, w: np.ndarray, rows: np.ndarray) -> int | None:
        """One random number -> one row of ``rows`` drawn by its weight ``w``;
        None (no random number used) when every weight is zero."""
        total = float(w.sum())
        if not np.isfinite(total) or total <= 0.0:
            return None
        cdf = np.cumsum(w, dtype=np.float64)
        i = int(np.searchsorted(cdf, self.rng.random() * cdf[-1]))
        return int(rows[min(i, len(rows) - 1)])

    def steal_draw(
        self,
        target_base: int,
        runner_key: str,
        pitcher_key: str,
        catcher_key: str | None,
        *,
        outs: int,
        balls: int,
        strikes: int,
        score_diff: int,
        aggression: float = 1.0,
        pitch_class: str | None = None,
    ) -> tuple[bool, bool, bool, bool, bool] | None:
        """Draw ONE steal-opportunity row -> (attempted, success, pickoff_out,
        pickoff_advancing, pickoff_error), or None when the pool/cell is
        absent (the caller then stages nothing).

        The owner's rule (2026-08-10): every sim decision is a similarity-
        weighted draw from a hard-filtered pool, never a hand-tuned formula.
        Hard filter: target base + the exact (outs, balls, strikes) cell.
        Weights: runner similarity (how he runs), pitcher hold similarity,
        catcher-arm similarity — so a strong arm DETERS the attempt, because
        rows against similar catchers carry fewer attempts — a soft score-diff
        kernel (blowout damping emerges from the pool, not a constant), manager
        aggression as a multiplier on ATTEMPTED rows (a weight, never a gate —
        SIM-474), and recency. The drawn row answers the whole pre-pitch
        running-game question at once: `attempted` says whether the runner
        goes, `success` says safe or caught, and the SIM-507 pickoff labels
        say whether a pickoff retired the runner (`pickoff_advancing` marks a
        picked-off caught stealing) or an errant throw advanced him. On a
        pre-0017 bundle the pickoff labels are all-zero and nothing changes.

        SIM-554: with ``pitch_class`` (the class of the pitch that came out)
        on a bundle with classes and ``steal_pitch_class`` on, the hard filter
        adds the class: the rows are the real pitches of that count AND that
        class, and the group answers from its own rows however few. A group
        with no row stages no steal; nothing falls back to the count group.
        The pickoff was drawn before the pitch (:meth:`pickoff_draw`), so the
        three pickoff fields read False.
        """
        if pitch_class is not None and self.steal_pitch_class:
            meta = self._steal_meta(str(int(target_base)))
            if meta is not None and "class_cells" in meta:
                group = meta["class_cells"].get(
                    (
                        int(outs),
                        int(balls),
                        int(strikes),
                        STEAL_PITCH_CLASS_CODE.get(pitch_class, 0),
                    )
                )
                if group is None or len(group) == 0:
                    return None  # no real pitch like it: no steal
                got = self.steal_weights(
                    target_base,
                    runner_key,
                    pitcher_key,
                    catcher_key,
                    outs=outs,
                    balls=balls,
                    strikes=strikes,
                    score_diff=score_diff,
                    aggression=aggression,
                    rows=group,
                )
                if got is None:
                    return None
                pool, rows, w = got
                r = self._steal_pick(w, rows)
                if r is None:
                    return None
                return (bool(pool.attempted[r]), bool(pool.success[r]), False, False, False)
        got = self.steal_weights(
            target_base,
            runner_key,
            pitcher_key,
            catcher_key,
            outs=outs,
            balls=balls,
            strikes=strikes,
            score_diff=score_diff,
            aggression=aggression,
        )
        if got is None:
            return None
        pool, rows, w = got
        r = self._steal_pick(w, rows)
        if r is None:
            return None
        return (
            bool(pool.attempted[r]),
            bool(pool.success[r]),
            bool(pool.pickoff_out[r]),
            bool(pool.pickoff_advancing[r]),
            bool(pool.pickoff_error[r]),
        )

    def pickoff_draw(
        self,
        target_base: int,
        runner_key: str,
        pitcher_key: str,
        catcher_key: str | None,
        *,
        outs: int,
        balls: int,
        strikes: int,
        score_diff: int,
    ) -> tuple[bool, bool, bool] | None:
        """SIM-554: the pickoff draw BEFORE the pitch -> (out, advancing,
        error) of ONE row of the count group, or None when the group is absent
        or holds no pickoff outcome at all (no random number is then used).

        Every row of the (target, outs, balls, strikes) group is a candidate:
        the pitch rows (a pickoff the builder tagged to the plate appearance's
        first pitch of the pair) and the pickoff rows (a pickoff thrown before
        any pitch of the pair, migration 0031). The weights are
        :meth:`steal_weights`' own — recency, the score-margin curve, the
        runner, pitcher-hold and catcher look-alike weights — with no manager
        weight: the manager's weight acts on the steal's attempted rows.
        ``out`` retires the runner (a caught stealing when ``advancing``);
        ``error`` is a throw that got away and moves him up a base.
        """
        meta = self._steal_meta(str(int(target_base)))
        if meta is None or "pickoff_cells" not in meta:
            return None
        cell = (int(outs), int(balls), int(strikes))
        if cell not in meta["pickoff_live"]:
            return None
        got = self.steal_weights(
            target_base,
            runner_key,
            pitcher_key,
            catcher_key,
            outs=outs,
            balls=balls,
            strikes=strikes,
            score_diff=score_diff,
            rows=meta["pickoff_cells"][cell],
        )
        if got is None:
            return None
        pool, rows, w = got
        r = self._steal_pick(w, rows)
        if r is None:
            return None
        return (
            bool(pool.pickoff_out[r]),
            bool(pool.pickoff_advancing[r]),
            bool(pool.pickoff_error[r]),
        )

    def steal_weights(
        self,
        target_base: int,
        runner_key: str,
        pitcher_key: str,
        catcher_key: str | None,
        *,
        outs: int,
        balls: int,
        strikes: int,
        score_diff: int,
        aggression: float = 1.0,
        rows: np.ndarray | None = None,
    ) -> tuple[StealPool, np.ndarray, np.ndarray] | None:
        """SIM-554: the steal draw's candidate rows and their weights —
        ``(pool, rows, w)`` — or None when the pool or the cell is absent.

        The one code path :meth:`steal_draw` and :meth:`pickoff_draw` sample
        from and the running-game replay (``scripts/running_game_replay.py``)
        reads whole, so they cannot disagree. It consumes no random number.
        ``rows`` names the candidate rows (the pickoff draw's count group, the
        steal draw's class group); None = the (outs, balls, strikes) group of
        pitch rows.
        """
        pool = self.a.steal_pools.get(str(int(target_base)))
        meta = self._steal_meta(str(int(target_base)))
        if pool is None or meta is None:
            return None
        if rows is None:
            rows = meta["cells"].get((int(outs), int(balls), int(strikes)))
        if rows is None or len(rows) == 0:
            return None
        w = pool.recency[rows].astype(np.float32).copy()
        sd = pool.sit[rows, 3] - np.float32(score_diff)
        w *= np.exp(-(sd * sd) / (2.0 * self.steal_score_sigma**2)).astype(np.float32)
        if True:
            f = self._steal_actor_factor(
                "baserunner",
                runner_key,
                meta["runner_rows"],
                rows,
                self._RUNNER_STEAL_FEATURES,
                matrix="runner_steal",
            )
            if f is not None:
                w *= f
        if True:
            f = self._steal_actor_factor(
                "pitcher_steal",
                pitcher_key,
                meta["pitcher_rows"],
                rows,
                self._PITCHER_STEAL_FEATURES,
                matrix="pitcher_steal",
            )
            if f is not None:
                w *= f
        if catcher_key:
            f = self._steal_actor_factor(
                "catcher",
                catcher_key,
                meta["catcher_rows"],
                rows,
                self._CATCHER_STEAL_FEATURES,
                matrix="catcher_throwing",
            )
            if f is not None:
                w *= f
        if aggression != 1.0:
            att = pool.attempted[rows].astype(bool)
            w = np.where(att, w * np.float32(aggression), w)
        w = self._apply_asof(w, pool, rows)
        return pool, rows, w
