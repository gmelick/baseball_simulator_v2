"""
tests/unit/test_sim523_result_split.py
======================================
SIM-523 part B — the PITCH / PITCH-RESULT split (plan:
docs/audit/2026-09-08-sim523-play-picker-redesign-plan.md, part B; the loop's
steps 3 and 4).

One draw used to pick the pitch and its result together. With the split on,
the sampler first draws the PITCH thrown from the per-plate-appearance weight,
then draws the RESULT among the same rows, weighted again by that weight times
the pitch-to-pitch score to the drawn pitch. The result row is the play: its
outcome, its got-away fact and, when in play, its own batted ball through the
artifact's pitch-id join. What these tests pin:

  * switch OFF: one draw, byte-identical, nothing kept;
  * switch ON with a tight bandwidth: the result comes from the drawn pitch's
    own cluster; with a loose bandwidth the result mixes across clusters;
  * the result row (not the pitch row) answers the got-away and born-ball
    reads; the pitch row answers the geometry read;
  * a pitch with no geometry, or bandwidth 0, makes the result the pitch row;
  * the pitcher / batter powers re-raise their factors on the right draw;
  * the cell-index path splits exactly like the whole-pool path;
  * the sampler's copy of the pitch engine's feature weights matches the engine;
  * the artifact export writes the pitch-id join and the loader reads it back
    (shared-view included); an export without the pitch pool writes no join;
  * the born batted ball's kernel on the fielding draw, and the loop passing
    the born ball only when that kernel is on;
  * the factory env reads.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import duckdb
import numpy as np
import pytest

from pipeline.batch.engine_artifacts import (
    _GEOM_COLS,
    BattedBallPool,
    EngineArtifacts,
    HandPool,
    build_battedball_pool_artifact,
    build_pitch_pool_artifact,
    join_rows,
)
from simulation.full_pool_sampler import _PITCH_FEATURE_WEIGHTS, FullPoolSampler, _repower
from simulation.game_state import GameState, Half, Team
from simulation.production_factory import apply_result_split_env
from simulation.sim_loop import StateMachine

_SEASON = 2024
_PITCHER = "100:2024"
_BATTER = "200:2024"
_BASE_OUT = np.array([0, 0, 5, 0], dtype=np.float32)

#: Two pitches far apart in every geometry column that varies.
_FASTBALL = np.array([95.0, 15.0, 5.0, 2300.0, 200.0, -1.0, 6.0, 6.5, 0.0, 2.5], dtype=np.float32)
_CURVE = np.array([78.0, -8.0, 8.0, 2600.0, 40.0, -1.0, 6.0, 6.0, 0.3, 1.5], dtype=np.float32)


def _pool(
    geoms: list[np.ndarray],
    outcomes: list[str],
    *,
    pitchers: list[int] | None = None,
    batters: list[int] | None = None,
    got_away: list[int] | None = None,
    bb_row: list[int] | None = None,
) -> HandPool:
    """Every row in the 0-0 count of the empty / no-outs cell, so the drawn
    outcome and geometry reveal which rows a draw favored."""
    n = len(geoms)
    sit = np.zeros((n, 6), dtype=np.float32)
    sit[:, 4] = 5.0
    return HandPool(
        geom=np.stack(geoms).astype(np.float32),
        sit=sit,
        pitcher_id=np.asarray(pitchers if pitchers is not None else [100] * n, dtype=np.int64),
        batter_id=np.asarray(batters if batters is not None else [200] * n, dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        outcome_type=np.asarray(outcomes, dtype=object),
        recency=np.ones(n, dtype=np.float32),
        got_away=(np.asarray(got_away, dtype=np.int8) if got_away is not None else None),
        bb_row=(np.asarray(bb_row, dtype=np.int32) if bb_row is not None else None),
    )


def _two_clusters(**kw) -> HandPool:
    """Four fastballs that are swung at and missed, four curves taken for balls."""
    return _pool([_FASTBALL] * 4 + [_CURVE] * 4, ["swinging_strike"] * 4 + ["ball"] * 4, **kw)


def _bb_pool(evs: list[float], events: list[str]) -> BattedBallPool:
    n = len(evs)
    rh = np.asarray([1 if e == "single" else 0 for e in events], dtype=np.int8)
    return BattedBallPool(
        geom=np.column_stack(
            [
                np.asarray(evs, dtype=np.float32),
                np.asarray([15.0 if e == "single" else 50.0 for e in events], dtype=np.float32),
                np.zeros(n, dtype=np.float32),
            ]
        ).astype(np.float32),
        sit=np.zeros((n, 6), dtype=np.float32),
        batter_id=np.full(n, 200, dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        event=np.asarray(events, dtype=object),
        result_hits=rh,
        result_outs=(1 - rh).astype(np.int8),
        recency=np.ones(n, dtype=np.float32),
        r1_dest=np.full(n, -1, dtype=np.int8),
        r2_dest=np.full(n, -1, dtype=np.int8),
        r3_dest=np.full(n, -1, dtype=np.int8),
        batter_dest=rh.copy(),
        dest_ok=np.ones(n, dtype=np.int8),
        r1_adv_out=np.zeros(n, dtype=np.int8),
        r2_adv_out=np.zeros(n, dtype=np.int8),
        r3_adv_out=np.zeros(n, dtype=np.int8),
        is_air=np.asarray([0 if e == "single" else 1 for e in events], dtype=np.int8),
        spray_raw=np.zeros(n, dtype=np.float32),
        hit_dist=np.asarray([300.0 if e == "single" else 200.0 for e in events], dtype=np.float32),
    )


def _sampler(
    pool: HandPool,
    *,
    bb: BattedBallPool | None = None,
    on: bool = True,
    sigma: float = 0.05,
    seed: int = 0,
    pitcher_sim: dict | None = None,
    pitcher_sim_index: dict | None = None,
    actor_emb: dict | None = None,
    actor_sim: dict | None = None,
    cell: bool = False,
) -> FullPoolSampler:
    art = EngineArtifacts(
        pools={"R": pool},
        pitcher_sim=pitcher_sim if pitcher_sim is not None else {_PITCHER: {_PITCHER: 1.0}},
        pitcher_sim_index=(pitcher_sim_index if pitcher_sim_index is not None else {_PITCHER: 0}),
        bb_pools=({"R": bb} if bb is not None else {}),
        actor_emb=actor_emb or {},
        actor_sim=actor_sim,
    )
    fp = FullPoolSampler(art, np.random.default_rng(seed))
    fp.pitch_result_split = on
    fp.result_pitch_sigma = sigma
    if cell:
        fp.pitch_cell_index = True
        fp.pitch_min_cell = 0
    return fp


def _draws(fp: FullPoolSampler, n: int = 60, batter: str = _BATTER) -> list[tuple[str, str]]:
    """(the drawn pitch's cluster, the outcome) per draw; the cluster reads
    "incomplete" when the drawn pitch has no complete geometry."""
    fp.new_half_inning("R", _PITCHER)
    out = []
    for _ in range(n):
        fp.new_plate_appearance(batter, _BASE_OUT)
        o = fp.draw(0, 0)
        g = fp.last_pitch_geom()
        if g is None:
            out.append(("incomplete", o))
        else:
            out.append(("fastball" if float(g[0]) > 90.0 else "curve", o))
    return out


# ===========================================================================
# The split itself
# ===========================================================================


class TestTheSplit:
    def test_off_is_one_draw_and_keeps_nothing(self):
        fp = _sampler(_two_clusters(), on=False)
        seq = [o for _, o in _draws(fp)]
        assert fp._bucket_w is None and fp._bucket_fb is None and fp._bucket_fp is None
        assert set(seq) == {"swinging_strike", "ball"}
        # The pitch row IS the result row while the split is off.
        assert fp.last_result_row() == fp._pp_pitch_i
        # And the sequence is exactly a fresh sampler's (nothing else moved).
        assert seq == [o for _, o in _draws(_sampler(_two_clusters(), on=False))]

    def test_a_tight_bandwidth_keeps_the_result_in_the_pitchs_cluster(self):
        fp = _sampler(_two_clusters(), sigma=0.05)
        pairs = _draws(fp, n=120)
        assert {p for p, _ in pairs} == {"fastball", "curve"}  # both pitches get thrown
        assert all(o == ("swinging_strike" if p == "fastball" else "ball") for p, o in pairs)

    def test_a_loose_bandwidth_mixes_the_result_across_clusters(self):
        fp = _sampler(_two_clusters(), sigma=1e6)
        pairs = _draws(fp, n=200)
        crossed = sum(1 for p, o in pairs if (p == "fastball") != (o == "swinging_strike"))
        assert crossed > 40  # ~half the draws cross at a flat pitch factor

    def test_the_result_row_is_the_play(self):
        # Curve rows got away; fastball rows did not. With a loose bandwidth
        # the result often comes from the other cluster than the pitch, and
        # the got-away read must follow the RESULT row.
        pool = _two_clusters(got_away=[0] * 4 + [1] * 4)
        fp = _sampler(pool, sigma=1e6)
        fp.new_half_inning("R", _PITCHER)
        seen_cross = False
        for _ in range(200):
            fp.new_plate_appearance(_BATTER, _BASE_OUT)
            o = fp.draw(0, 0)
            r = fp.last_result_row()
            assert r is not None and str(pool.outcome_type[r]) == o
            assert fp.last_pitch_got_away() == bool(pool.got_away[r])
            g = fp.last_pitch_geom()
            assert g is not None and np.allclose(g, pool.geom[fp._pp_pitch_i])
            seen_cross |= r != fp._pp_pitch_i
        assert seen_cross

    def test_bandwidth_zero_makes_the_result_the_pitch_row(self):
        fp = _sampler(_two_clusters(), sigma=0.0)
        fp.new_half_inning("R", _PITCHER)
        for _ in range(40):
            fp.new_plate_appearance(_BATTER, _BASE_OUT)
            fp.draw(0, 0)
            assert fp.last_result_row() == fp._pp_pitch_i

    def test_a_pitch_without_geometry_makes_the_result_the_pitch_row(self):
        # A velocity of 0 is the export's stand-in for a missing pitch.
        pool = _pool([np.zeros(10, dtype=np.float32)] * 8, ["ball"] * 4 + ["foul"] * 4)
        fp = _sampler(pool, sigma=1e6)
        fp.new_half_inning("R", _PITCHER)
        for _ in range(40):
            fp.new_plate_appearance(_BATTER, _BASE_OUT)
            fp.draw(0, 0)
            assert fp.last_result_row() == fp._pp_pitch_i
            assert fp.last_pitch_geom() is None

    def test_incomplete_candidate_rows_draw_at_the_average_weight(self):
        # Rows 6-7 are curves missing a spin rate. Against a drawn fastball a
        # complete curve row (4-5) scores ~0 and never comes back; an
        # incomplete row is exactly neutral — it draws like an average row,
        # neither an exact match nor a miss. An incomplete DRAWN pitch
        # conditions nothing: its result is itself.
        geoms = [_FASTBALL] * 4 + [_CURVE] * 2
        bad = _CURVE.copy()
        bad[3] = np.nan
        geoms += [bad] * 2
        pool = _pool(geoms, ["swinging_strike"] * 4 + ["ball"] * 4)
        fp = _sampler(pool, sigma=0.05)
        fp.new_half_inning("R", _PITCHER)
        results_after_fastball: set[int] = set()
        seen = set()
        for _ in range(200):
            fp.new_plate_appearance(_BATTER, _BASE_OUT)
            fp.draw(0, 0)
            r = fp.last_result_row()
            assert r is not None
            g = fp.last_pitch_geom()
            if g is None:
                seen.add("incomplete")
                assert r == fp._pp_pitch_i
            elif float(g[0]) > 90.0:
                seen.add("fastball")
                results_after_fastball.add(r)
            else:
                seen.add("curve")
        assert seen == {"fastball", "curve", "incomplete"}
        assert results_after_fastball <= {0, 1, 2, 3, 6, 7}
        assert results_after_fastball & {6, 7}  # the neutral rows do come back
        assert not (results_after_fastball & {4, 5})  # the complete curves never do

    def test_the_cell_path_splits_the_same_way(self):
        fp = _sampler(_two_clusters(), sigma=0.05, cell=True)
        pairs = _draws(fp, n=120)
        assert fp._pa_rows is not None  # the cell path ran
        assert {p for p, _ in pairs} == {"fastball", "curve"}
        assert all(o == ("swinging_strike" if p == "fastball" else "ball") for p, o in pairs)

    def test_an_empty_bucket_clears_both_rows(self):
        fp = _sampler(_two_clusters())
        fp.new_half_inning("R", _PITCHER)
        fp.new_plate_appearance(_BATTER, _BASE_OUT)
        assert fp.draw(3, 2) == "ball"  # no 3-2 rows in the pool
        assert fp.last_result_row() is None and fp._pp_pitch_i is None
        assert fp.last_pitch_geom() is None and fp.last_born_batted_ball() is None


# ===========================================================================
# The density correction
# ===========================================================================


def _gradient_pool(n: int = 4000) -> HandPool:
    """A pool whose only varying geometry is the horizontal plate location,
    half-normal — dense over the middle, sparse outside — with a pitch beyond
    0.83 ft a ball (~40% of the rows)."""
    rng = np.random.default_rng(1)
    x = np.abs(rng.normal(size=n)).astype(np.float32)
    geom = np.zeros((n, 10), dtype=np.float32)
    geom[:, 0] = 92.0
    geom[:, 8] = x
    return _pool(list(geom), list(np.where(x > 0.83, "ball", "called_strike")))


def _ball_share(fp: FullPoolSampler, n: int = 8000) -> float:
    fp.new_half_inning("R", _PITCHER)
    fp.new_plate_appearance(_BATTER, _BASE_OUT)
    return sum(fp.draw(0, 0) == "ball" for _ in range(n)) / n


class TestTheDensityCorrection:
    def test_the_raw_kernel_leans_toward_the_dense_side_and_the_correction_undoes_it(self):
        # Standard error of a share over 8,000 draws is ~0.0055. The raw split
        # under-draws balls (the sparse side) by ~0.018; with the correction the
        # split reproduces the single draw within 0.015 at the default power.
        pool = _gradient_pool()
        single = _ball_share(_sampler(pool, on=False))
        raw = _sampler(pool, sigma=0.2)
        raw.result_density_power = 0.0
        corrected = _sampler(pool, sigma=0.2)  # the default power
        assert corrected.result_density_power == 1.0
        raw_share, corrected_share = _ball_share(raw), _ball_share(corrected)
        assert single - raw_share > 0.010
        assert abs(corrected_share - single) < 0.015

    def test_the_density_is_a_cached_pool_fact(self):
        fp = _sampler(_two_clusters(), sigma=0.5)
        fp.new_half_inning("R", _PITCHER)
        fp.new_plate_appearance(_BATTER, _BASE_OUT)
        fp.draw(0, 0)
        cache = fp._pool_meta("R")["density"]
        assert len(cache) == 1
        (((key, sigma, power), inv),) = cache.items()
        assert key == (-1, 0) and sigma == 0.5 and power == 1.0
        # Two equal clusters of four: every row sees the same density.
        np.testing.assert_allclose(inv, np.ones(8), rtol=1e-5)
        # The same draw again reuses the entry; a new bandwidth makes a new one.
        fp.draw(0, 0)
        assert len(cache) == 1
        fp.result_pitch_sigma = 0.25
        fp.draw(0, 0)
        assert len(cache) == 2

    def test_a_lone_outlier_gains_at_most_twenty_times(self):
        # A hundred fastballs and one pitch nobody else throws: its density
        # (its own kernel value alone, ~1/101) floors at 5% of the median, so
        # its inverse density is 20x a fastball's — never more.
        odd = _CURVE.copy()
        odd[0] = 60.0
        pool = _pool([_FASTBALL] * 100 + [odd], ["called_strike"] * 100 + ["ball"])
        fp = _sampler(pool, sigma=0.05)
        inv = fp._result_inv_density("R", (-1, 0), np.arange(101))
        assert inv[100] / inv[0] == pytest.approx(20.0, rel=1e-3)
        # At half power the gain is the square root of that.
        fp.result_density_power = 0.5
        inv = fp._result_inv_density("R", (-1, 0), np.arange(101))
        assert inv[100] / inv[0] == pytest.approx(20.0**0.5, rel=1e-3)

    def test_incomplete_rows_are_neutral_in_the_density(self):
        bad = _FASTBALL.copy()
        bad[0] = 0.0
        fp = _sampler(_pool([_FASTBALL] * 4 + [bad] * 2, ["ball"] * 6))
        inv = fp._result_inv_density("R", (-1, 0), np.arange(6))
        assert inv[4:].tolist() == [1.0, 1.0]


# ===========================================================================
# The powers
# ===========================================================================


def _batter_emb() -> dict:
    keys = ["200:2024", "201:2024"]
    return {
        "key_index": {k: i for i, k in enumerate(keys)},
        "vecs": np.zeros((2, 2), dtype=np.float32),
        "mean": np.zeros(2, dtype=np.float32),
        "std": np.ones(2, dtype=np.float32),
        "features": ["a", "b"],
    }


#: The live batter 200 scores 1.0 vs his own rows and 0.1 vs batter 201's
#: (at power 8 the other batter's rows keep 1e-8 of their weight).
_BAT_MATRIX = {
    "batter": {
        "index": {"200:2024": 0, "201:2024": 1},
        "matrix": np.array([[1.0, 0.1], [0.1, 1.0]], dtype=np.float32),
    }
}


class TestThePowers:
    def test_repower(self):
        w = np.array([1.0, 0.5, 0.0], dtype=np.float32)
        f = np.array([1.0, 0.5, 0.0], dtype=np.float32)
        assert _repower(w, f, 1.0) is w
        np.testing.assert_allclose(_repower(w, f, 2.0), [1.0, 0.25, 0.0])
        np.testing.assert_allclose(_repower(w, f, 0.5), [1.0, 0.5**0.5, 0.0])

    def test_the_result_batter_power_re_raises_the_batter_factor(self):
        # Same pitch everywhere; batter 200's rows foul it off, 201's put it
        # in play. At power 1 both results appear (weights 1 : 0.1); at power
        # 8 the other batter's rows fall to 0.1^8 and only fouls remain.
        pool = _pool([_FASTBALL] * 8, ["foul"] * 4 + ["in_play"] * 4, batters=[200] * 4 + [201] * 4)
        fp = _sampler(pool, sigma=1e6, actor_emb={"batter": _batter_emb()}, actor_sim=_BAT_MATRIX)
        assert {o for _, o in _draws(fp, n=120)} == {"foul", "in_play"}
        fp.result_batter_power = 8.0
        assert {o for _, o in _draws(fp, n=120)} == {"foul"}

    def test_the_pitch_batter_power_re_raises_the_batter_factor_on_the_pitch_draw(self):
        # Batter 200's rows are fastballs, 201's curves. At power 8 the pitch
        # draw itself picks only fastballs.
        pool = _pool(
            [_FASTBALL] * 4 + [_CURVE] * 4,
            ["swinging_strike"] * 4 + ["ball"] * 4,
            batters=[200] * 4 + [201] * 4,
        )
        fp = _sampler(pool, sigma=0.05, actor_emb={"batter": _batter_emb()}, actor_sim=_BAT_MATRIX)
        assert {p for p, _ in _draws(fp, n=120)} == {"fastball", "curve"}
        fp.pitch_batter_power = 8.0
        assert {p for p, _ in _draws(fp, n=120)} == {"fastball"}

    def test_the_result_pitcher_power_re_raises_the_pitcher_factor(self):
        # Pitcher 100 (the live arm, similarity 1.0) throws fouls; pitcher 101
        # (similarity 0.1) gets balls in play. Power 8 leaves only pitcher 100.
        pool = _pool(
            [_FASTBALL] * 8, ["foul"] * 4 + ["in_play"] * 4, pitchers=[100] * 4 + [101] * 4
        )
        fp = _sampler(
            pool,
            sigma=1e6,
            pitcher_sim={_PITCHER: {_PITCHER: 1.0, "101:2024": 0.1}},
            pitcher_sim_index={_PITCHER: 0, "101:2024": 1},
        )
        assert {o for _, o in _draws(fp, n=120)} == {"foul", "in_play"}
        fp.result_pitcher_power = 8.0
        assert {o for _, o in _draws(fp, n=120)} == {"foul"}

    def test_the_feature_weights_match_the_pitch_engine(self):
        eng = pytest.importorskip("similarity.engines.pitch_pitch_similarity")
        assert list(eng.FEATURE_NAMES) == list(_GEOM_COLS)
        np.testing.assert_allclose(_PITCH_FEATURE_WEIGHTS, eng.FEATURE_WEIGHTS, rtol=1e-6)


# ===========================================================================
# The born batted ball
# ===========================================================================


class TestTheBornBattedBall:
    def test_the_in_play_result_reads_its_own_batted_ball(self):
        # Rows 0-3 in play, joined to batted-ball rows 0..3; rows 4-7 balls.
        bb = _bb_pool([101.0, 102.0, 103.0, 104.0], ["single"] * 4)
        pool = _pool(
            [_FASTBALL] * 4 + [_CURVE] * 4,
            ["in_play"] * 4 + ["ball"] * 4,
            bb_row=[0, 1, 2, 3, -1, -1, -1, -1],
        )
        fp = _sampler(pool, bb=bb, sigma=0.05)
        fp.new_half_inning("R", _PITCHER)
        seen = set()
        for _ in range(80):
            fp.new_plate_appearance(_BATTER, _BASE_OUT)
            o = fp.draw(0, 0)
            born = fp.last_born_batted_ball()
            r = fp.last_result_row()
            if o == "in_play":
                assert born is not None and born["row"] == r
                assert born["ev"] == pytest.approx(101.0 + r)
                assert born["la"] == pytest.approx(15.0) and born["dist"] == pytest.approx(300.0)
                assert born["is_air"] is False
                seen.add(r)
            else:
                assert born is None
        assert seen == {0, 1, 2, 3}

    def test_no_join_or_no_batted_ball_pool_reads_none(self):
        pool = _pool([_FASTBALL] * 4, ["in_play"] * 4)  # no bb_row column
        fp = _sampler(pool, bb=_bb_pool([100.0] * 4, ["single"] * 4))
        fp.new_half_inning("R", _PITCHER)
        fp.new_plate_appearance(_BATTER, _BASE_OUT)
        fp.draw(0, 0)
        assert fp.last_born_batted_ball() is None
        pool2 = _pool([_FASTBALL] * 4, ["in_play"] * 4, bb_row=[0, 1, 2, 3])
        fp2 = _sampler(pool2)  # no batted-ball pool at all
        fp2.new_half_inning("R", _PITCHER)
        fp2.new_plate_appearance(_BATTER, _BASE_OUT)
        fp2.draw(0, 0)
        assert fp2.last_born_batted_ball() is None

    def test_the_born_kernel_draws_plays_on_similar_batted_balls(self):
        bb = _bb_pool([100.0] * 4 + [70.0] * 4, ["single"] * 4 + ["field_out"] * 4)
        fp = _sampler(_pool([_FASTBALL] * 4, ["in_play"] * 4), bb=bb)
        born = {"ev": 100.0, "la": 15.0, "spray": 0.0, "dist": 300.0}

        def draws(**kw) -> set[str]:
            out = set()
            for _ in range(40):
                fp.battedball_new_pa("R", _BATTER, np.zeros(6, np.float32), **kw)
                out.add(fp.battedball_draw()[0])
            return out

        assert draws() == {"single", "field_out"}
        fp.bb_born_sigma = 0.05
        assert draws(born_bb=born) == {"single"}
        assert draws(born_bb={"ev": 70.0, "la": 50.0, "spray": 0.0, "dist": 200.0}) == {"field_out"}
        # An incomplete born ball is neutral; sigma 0 is off.
        assert draws(born_bb={"ev": None, "la": 15.0, "spray": 0.0}) == {"single", "field_out"}
        fp.bb_born_sigma = 0.0
        assert draws(born_bb=born) == {"single", "field_out"}

    def test_sigma_zero_is_byte_identical_with_a_born_ball(self):
        bb = _bb_pool([100.0] * 4 + [70.0] * 4, ["single"] * 4 + ["field_out"] * 4)
        born = {"ev": 100.0, "la": 15.0, "spray": 0.0, "dist": 300.0}

        def seq(with_born: bool) -> list[str]:
            fp = _sampler(_pool([_FASTBALL] * 4, ["in_play"] * 4), bb=bb, seed=3)
            out = []
            for _ in range(30):
                kw = {"born_bb": born} if with_born else {}
                fp.battedball_new_pa("R", _BATTER, np.zeros(6, np.float32), **kw)
                out.append(fp.battedball_draw()[0])
            return out

        assert seq(True) == seq(False)


# ===========================================================================
# The loop wiring
# ===========================================================================


class _Recorder(FullPoolSampler):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.bb_kwargs: list[dict] = []

    def battedball_new_pa(self, *a, **kw) -> None:
        self.bb_kwargs.append({k: v for k, v in kw.items() if k == "born_bb"})
        super().battedball_new_pa(*a, **kw)


def _state() -> GameState:
    state = GameState(
        pitcher_id=100,
        bat_hand="R",
        season=_SEASON,
        away_lineup=[200, 201, 202],
        home_lineup=[200, 201, 202],
    )
    state.batter_id = 200
    assert state.half is Half.TOP and state.offense is Team.AWAY
    return state


class TestLoopWiring:
    def test_the_born_ball_reaches_the_fielding_draw_only_when_its_kernel_is_on(self):
        bb = _bb_pool([100.0] * 4 + [70.0] * 4, ["single"] * 4 + ["field_out"] * 4)
        pool = _pool([_FASTBALL] * 4, ["in_play"] * 4, bb_row=[0, 1, 2, 3])
        art = EngineArtifacts(
            pools={"R": pool, "L": pool},
            pitcher_sim={_PITCHER: {_PITCHER: 1.0}},
            pitcher_sim_index={_PITCHER: 0},
            bb_pools={"R": bb, "L": bb},
        )
        fp = _Recorder(art, np.random.default_rng(7))
        fp.pitch_result_split = True
        machine = StateMachine(fp, rng=np.random.default_rng(7))
        state = _state()
        assert machine._full_pool_outcome(state) == "in_play"
        machine._full_pool_fielding(state)
        assert fp.bb_kwargs[-1] == {}
        fp.bb_born_sigma = 0.05
        machine._full_pool_fielding(state)
        born = fp.bb_kwargs[-1]["born_bb"]
        assert born["row"] == fp.last_result_row() and born["ev"] == pytest.approx(100.0)


# ===========================================================================
# The artifact export + loader
# ===========================================================================

_OUTCOME_POOL_DDL = """
CREATE SCHEMA IF NOT EXISTS sim;
CREATE TABLE sim.outcome_pool (
    pitch_id BIGINT, batter_id INTEGER, season SMALLINT, stand VARCHAR,
    exit_velo FLOAT, launch_angle FLOAT, pull_relative_spray_angle FLOAT,
    count_balls SMALLINT, count_strikes SMALLINT, outs SMALLINT,
    runners_state SMALLINT, inning SMALLINT, score_diff SMALLINT,
    events VARCHAR, result_hits SMALLINT, result_outs SMALLINT,
    result_runs SMALLINT, recency_weight FLOAT,
    p_throws VARCHAR, venue_id INTEGER, fielded_by_position SMALLINT,
    fielder_player_id INTEGER
);
"""

_PITCH_POOL_DDL = """
CREATE SCHEMA IF NOT EXISTS sim;
CREATE TABLE sim.pitch_pool (
    pitch_id BIGINT, pitcher_id INTEGER, batter_id INTEGER, season SMALLINT,
    stand VARCHAR, outcome_type VARCHAR, recency_weight FLOAT,
    velo FLOAT, ivb FLOAT, hb FLOAT, spin_rate FLOAT, spin_axis FLOAT,
    release_x FLOAT, release_z FLOAT, release_ext FLOAT, plate_x FLOAT, plate_z FLOAT,
    count_balls SMALLINT, count_strikes SMALLINT, outs SMALLINT,
    runners_state SMALLINT, inning SMALLINT, score_diff SMALLINT
);
"""


def _seed(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(_OUTCOME_POOL_DDL)
    con.execute(_PITCH_POOL_DDL)
    # Right-handed: pitches 11 and 12 in play (both with a batted ball), 13 a
    # ball, 14 in play WITHOUT a batted-ball row. Left-handed: pitch 21 in play.
    for pid, stand, outcome in (
        (11, "R", "in_play"),
        (12, "R", "in_play"),
        (13, "R", "ball"),
        (14, "R", "in_play"),
        (21, "L", "in_play"),
    ):
        con.execute(
            "INSERT INTO sim.pitch_pool VALUES (?,?,?,?, ?,?,1.0, "
            "93.0,12.0,5.0,2200.0,180.0, 1.0,6.0,6.5,0.0,2.5, 0,0,0, 0,1,0)",
            [pid, 600000 + pid, 700000 + pid, _SEASON, stand, outcome],
        )
    for pid, stand, ev in ((12, "R", 102.0), (11, "R", 98.0), (21, "L", 88.0)):
        con.execute(
            "INSERT INTO sim.outcome_pool VALUES (?,?,?,?, ?,12.0,10.0, 0,0,0, 0,1,0, "
            "'single',1,0, 0, 1.0, 'R',15,6,500006)",
            [pid, 700000 + pid, _SEASON, stand, ev],
        )


class TestTheArtifactJoin:
    def test_join_rows(self):
        out = join_rows(np.array([5, 3, 9, -1, 3]), np.array([3, 9, 7]))
        assert out.tolist() == [-1, 0, 1, -1, 0]
        assert out.dtype == np.int32
        assert join_rows(np.array([1, 2]), np.array([], dtype=np.int64)).tolist() == [-1, -1]

    def test_export_writes_the_join_and_load_reads_it(self, tmp_path):
        con = duckdb.connect(":memory:")
        try:
            _seed(con)
            build_pitch_pool_artifact(con, str(tmp_path), [_SEASON])
            build_battedball_pool_artifact(con, str(tmp_path), [_SEASON])
        finally:
            con.close()
        assert os.path.exists(tmp_path / "pitch_pool" / "R.bb_row.npy")
        art = EngineArtifacts.load(str(tmp_path))
        pool, bb = art.pools["R"], art.bb_pools["R"]
        assert pool.bb_row is not None and pool.bb_row.dtype == np.int32
        # Every in-play pitch with a batted ball maps to the batted-ball row
        # of the SAME pitch (the batter ids were made to match per pitch).
        outcomes = [str(o) for o in pool.outcome_type]
        for i, o in enumerate(outcomes):
            j = int(pool.bb_row[i])
            if j >= 0:
                assert o == "in_play"
                assert int(bb.batter_id[j]) == int(pool.batter_id[i])
        joined = {int(pool.batter_id[i]) for i in range(pool.n) if pool.bb_row[i] >= 0}
        assert joined == {700011, 700012}  # 13 is a ball, 14 has no batted ball
        assert int(art.pools["L"].bb_row[0]) == 0
        # The join is shareable and the loader takes a published view.
        shared = art.extract_shared_arrays()
        assert shared["pool.R.bb_row"] is pool.bb_row
        view = np.full(pool.n, -1, dtype=np.int32)
        art2 = EngineArtifacts.load(str(tmp_path), shared_views={"pool.R.bb_row": view})
        assert art2.pools["R"].bb_row is view

    def test_the_batted_ball_export_alone_writes_no_join(self, tmp_path):
        con = duckdb.connect(":memory:")
        try:
            _seed(con)
            build_battedball_pool_artifact(con, str(tmp_path), [_SEASON])
            assert not os.path.exists(tmp_path / "pitch_pool" / "R.bb_row.npy")
            # And a later pitch-pool export still loads with bb_row None.
            build_pitch_pool_artifact(con, str(tmp_path), [_SEASON])
        finally:
            con.close()
        art = EngineArtifacts.load(str(tmp_path))
        assert art.pools["R"].bb_row is None
        # The batted-ball pool's own pitch ids are on disk for the next join.
        assert os.path.exists(tmp_path / "battedball_pool" / "R.pitch_id.npy")


# ===========================================================================
# The factory env reads
# ===========================================================================


class TestTheFactoryEnv:
    def test_defaults(self):
        s = SimpleNamespace()
        apply_result_split_env(s, env={})
        assert s.pitch_result_split is False
        assert s.result_pitch_sigma == 1.0 and s.result_pitcher_power == 1.0
        assert s.result_density_power == 1.0
        assert s.result_batter_power == 1.0 and s.pitch_batter_power == 1.0
        assert s.bb_born_sigma == 0.0

    def test_values_and_junk(self):
        s = SimpleNamespace()
        apply_result_split_env(
            s,
            env={
                "SIM_PITCH_RESULT_SPLIT": "1",
                "SIM_RESULT_PITCH_SIGMA": "0.5",
                "SIM_RESULT_DENSITY_POWER": "0.75",
                "SIM_RESULT_PITCHER_POWER": "0.5",
                "SIM_RESULT_BATTER_POWER": "junk",
                "SIM_PITCH_BATTER_POWER": "0.25",
                "SIM_BB_BORN_SIGMA": "0.3",
            },
        )
        assert s.pitch_result_split is True
        assert s.result_pitch_sigma == 0.5 and s.result_pitcher_power == 0.5
        assert s.result_density_power == 0.75
        assert s.result_batter_power == 1.0  # junk -> the default
        assert s.pitch_batter_power == 0.25 and s.bb_born_sigma == 0.3

    def test_the_unit_suite_pins_the_split_off(self):
        assert os.environ.get("SIM_PITCH_RESULT_SPLIT") == "0"
        assert os.environ.get("SIM_BB_BORN_SIGMA") == "0"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
