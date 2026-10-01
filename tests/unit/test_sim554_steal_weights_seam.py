"""SIM-554 — the steal draw's weights as a seam.

``FullPoolSampler.steal_weights`` returns the steal draw's candidate rows and
their weights. ``steal_draw`` samples from exactly that, so the running-game
replay (``scripts/sim554_running_game_replay.py``) reads the odds the simulator
draws from, and the two cannot disagree. The seam consumes no random number.
"""

from __future__ import annotations

import numpy as np

from pipeline.batch.engine_artifacts import EngineArtifacts, StealPool
from simulation.full_pool_sampler import FullPoolSampler

KW = {"outs": 0, "balls": 0, "strikes": 0, "score_diff": 0}


def _pool(n: int, attempted: list[int], *, score_diff: list[float] | None = None) -> StealPool:
    att = np.asarray(attempted, dtype=np.int8)
    sit = np.zeros((n, 4), dtype=np.float32)
    if score_diff is not None:
        sit[:, 3] = np.asarray(score_diff, dtype=np.float32)
    return StealPool(
        sit=sit,
        runner_id=np.full(n, 11, dtype=np.int64),
        pitcher_id=np.full(n, 901, dtype=np.int64),
        catcher_id=np.full(n, 902, dtype=np.int64),
        season=np.full(n, 2024, dtype=np.int64),
        attempted=att,
        success=att.copy(),
        recency=np.linspace(0.5, 2.0, n).astype(np.float32),
    )


def _sampler(pool: StealPool, seed: int = 7) -> FullPoolSampler:
    art = EngineArtifacts({}, steal_pools={"2": pool}, actor_emb={})
    return FullPoolSampler(art, np.random.default_rng(seed))


class TestTheSeam:
    def test_it_returns_the_cell_rows_and_one_weight_per_row(self):
        fp = _sampler(_pool(100, [1] * 10 + [0] * 90))
        got = fp.steal_weights(2, "11:2024", "901:2024", "902:2024", **KW)
        assert got is not None
        pool, rows, w = got
        assert pool is fp.a.steal_pools["2"]
        assert len(rows) == 100 and w.shape == (100,)
        assert np.all(w > 0)

    def test_an_absent_pool_or_cell_returns_none(self):
        fp = _sampler(_pool(10, [0] * 10))
        assert fp.steal_weights(3, "11:2024", "", None, **KW) is None
        assert (
            fp.steal_weights(2, "11:2024", "", None, outs=2, balls=3, strikes=2, score_diff=0)
            is None
        )

    def test_it_consumes_no_random_number(self):
        fp = _sampler(_pool(50, [1] * 5 + [0] * 45))
        before = fp.rng.bit_generator.state
        fp.steal_weights(2, "11:2024", "901:2024", "902:2024", **KW)
        assert fp.rng.bit_generator.state == before

    def test_the_weights_carry_the_score_nearness_and_the_managers_weight(self):
        n = 40
        fp = _sampler(_pool(n, [1] * 20 + [0] * 20, score_diff=[0.0] * 30 + [5.0] * 10))
        _pool_, _rows, w = fp.steal_weights(2, "11:2024", "", None, **KW)
        rec = np.linspace(0.5, 2.0, n).astype(np.float32)
        # a row five runs away weighs less than its recency alone
        assert np.all(w[30:] < rec[30:])
        assert np.allclose(w[:30], rec[:30])
        _p, _r, w4 = fp.steal_weights(2, "11:2024", "", None, aggression=4.0, **KW)
        assert np.allclose(w4[:20], 4.0 * w[:20])  # the attempted rows only
        assert np.allclose(w4[20:], w[20:])


class TestTheDrawSamplesFromTheSeam:
    def test_the_draw_is_the_seams_weights_and_one_random_number(self):
        pool = _pool(200, [1] * 40 + [0] * 160)
        drawn = _sampler(pool, seed=3)
        shadow = _sampler(pool, seed=3)
        for _ in range(300):
            got = drawn.steal_draw(2, "11:2024", "901:2024", "902:2024", **KW)
            _p, rows, w = shadow.steal_weights(2, "11:2024", "901:2024", "902:2024", **KW)
            cdf = np.cumsum(w, dtype=np.float64)
            i = min(int(np.searchsorted(cdf, shadow.rng.random() * cdf[-1])), len(rows) - 1)
            r = rows[i]
            assert got is not None
            assert got[:2] == (bool(pool.attempted[r]), bool(pool.success[r]))

    def test_the_drawn_attempt_rate_is_the_weighted_share_of_attempted_rows(self):
        pool = _pool(400, [1] * 80 + [0] * 320)
        fp = _sampler(pool, seed=11)
        _p, rows, w = fp.steal_weights(2, "11:2024", "", None, **KW)
        share = float(w[pool.attempted[rows] > 0].sum() / w.sum())
        hits = sum(int(fp.steal_draw(2, "11:2024", "", None, **KW)[0]) for _ in range(4000))
        assert abs(hits / 4000 - share) < 0.03
