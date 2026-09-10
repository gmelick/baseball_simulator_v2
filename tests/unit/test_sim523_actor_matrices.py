"""
tests/unit/test_sim523_actor_matrices.py
========================================
SIM-523 part A — the nightly ACTOR SCORE MATRICES (plan:
docs/audit/2026-09-08-sim523-play-picker-redesign-plan.md, part A).

Each actor factor in the draw becomes its engine's composite 0-to-1 score,
read from a dense matrix the nightly build writes, and raised to a fitted
power. What these tests pin:

  * the builder scores every profile against every other through the
    engine's own ``query`` and packs a dense matrix (diagonal 1.0, the
    unscored NaN), one file per matrix and one per fielder position, with a
    manifest and the concentration report;
  * a strict build raises when a matrix's own-staff ratio is too high; a
    normal build only warns;
  * the loader reads the matrices back and the shared-view seam publishes
    them (the SIM-403b zero-copy path);
  * the batter, runner, catcher-throwing and fielder factors are the live
    actor's matrix row gathered onto the pool rows; a missing live actor or
    pool actor is neutral; a bundle without a matrix leaves the actor neutral
    (the bell-curve kernels are retired, 2026-09-09); the power reshapes the
    score;
  * the factory reads the per-name powers from the env;
  * the concentration arithmetic on a toy pool.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import duckdb
import numpy as np
import pytest

import pipeline.batch.engine_artifacts as ea
from pipeline.batch.engine_artifacts import (
    BattedBallPool,
    EngineArtifacts,
    HandPool,
    StealPool,
    build_actor_sim_matrices,
    concentration_share,
)
from simulation.full_pool_sampler import FullPoolSampler
from simulation.production_factory import apply_actor_matrix_env

_SEASON = 2024
_PITCHER = "100:2024"
_LIVE_BAT = "200:2024"
_OTHER_BAT = "201:2024"

# ===========================================================================
# Stub engines for the builder
# ===========================================================================


class _StubEngine:
    """An engine whose ``query`` returns a fixed score for every other
    profile: 0.5 between two profiles of the same season, 0.2 otherwise."""

    key_attrs: tuple[str, ...] = ("batter_id", "season")
    profiles: list[tuple] = [(1, 2024), (2, 2024), (3, 2023)]
    score_attrs: tuple[str, ...] = ("score",)

    def __init__(self, duckdb_path: str):
        self.duckdb_path = duckdb_path
        self.built: list[int] = []
        self._profiles: dict = {}

    def build(self, seasons):
        self.built = list(seasons)
        self._profiles = {k: object() for k in self.profiles}

    def query(self, *key):
        out = []
        for other in self.profiles:
            if other == key:
                continue
            s = 0.5 if other[-1] == key[-1] else 0.2
            r = SimpleNamespace(**dict(zip(self.key_attrs, other, strict=True)))
            for a in self.score_attrs:
                setattr(r, a, s if a == "score" else 0.75)
            out.append(r)
        return out


class _BatterStub(_StubEngine):
    pass


class _CatcherStub(_StubEngine):
    key_attrs = ("catcher_id", "season")
    score_attrs = ("score", "throwing_score")


class _RunnerStealStub(_StubEngine):
    key_attrs = ("player_id", "season")


class _RunnerStub(_StubEngine):
    key_attrs = ("player_id", "season")


class _PitcherStealStub(_StubEngine):
    key_attrs = ("pitcher_id", "season")


class _FielderStub(_StubEngine):
    key_attrs = ("player_id", "position", "season")
    profiles = [(11, "SS", 2024), (12, "SS", 2024), (13, "CF", 2024), (14, "SS", 2023)]


_STUBS = {
    "BatterSimilarityEngine": _BatterStub,
    "CatcherSimilarityEngine": _CatcherStub,
    "BaserunnerStealSimilarityEngine": _RunnerStealStub,
    "BaserunnerSimilarityEngine": _RunnerStub,
    "PitcherStealSimilarityEngine": _PitcherStealStub,
    "FielderSimilarityEngine": _FielderStub,
}


@pytest.fixture()
def stub_engines(monkeypatch):
    monkeypatch.setattr(ea, "_ENGINE_LOADER", lambda module, cls: _STUBS[cls])
    monkeypatch.setattr(ea, "concentration_report", lambda *a, **k: {})


def _load_npz(path: str) -> tuple[dict, np.ndarray]:
    z = np.load(path, allow_pickle=True)
    return json.loads(str(z["index"])), z["matrix"]


class TestTheBuilder:
    def test_writes_every_matrix_with_a_manifest(self, stub_engines, tmp_path):
        sizes = build_actor_sim_matrices("x.duckdb", str(tmp_path), [2024])
        sim_dir = tmp_path / "actor_sim"
        assert sizes == {
            "batter": 2,
            "catcher": 2,
            "catcher_throwing": 2,
            "runner_steal": 2,
            "runner_adv": 2,
            "pitcher_steal": 2,
            "fielder_1B": 0,
            "fielder_2B": 0,
            "fielder_3B": 0,
            "fielder_SS": 2,
            "fielder_LF": 0,
            "fielder_CF": 1,
            "fielder_RF": 0,
        }
        with open(sim_dir / "manifest.json", encoding="utf-8") as fh:
            manifest = json.load(fh)
        assert manifest == {"seasons": [2024], "sizes": sizes}
        assert (sim_dir / "concentration.json").exists()

    def test_the_matrix_is_dense_with_a_unit_diagonal(self, stub_engines, tmp_path):
        build_actor_sim_matrices("x.duckdb", str(tmp_path), [2024])
        index, mat = _load_npz(str(tmp_path / "actor_sim" / "batter.npz"))
        assert index == {"1:2024": 0, "2:2024": 1}
        assert mat.dtype == np.float32
        assert mat.tolist() == [[1.0, 0.5], [0.5, 1.0]]

    def test_the_throwing_matrix_reads_the_sub_score(self, stub_engines, tmp_path):
        build_actor_sim_matrices("x.duckdb", str(tmp_path), [2024])
        _, comp = _load_npz(str(tmp_path / "actor_sim" / "catcher.npz"))
        _, thr = _load_npz(str(tmp_path / "actor_sim" / "catcher_throwing.npz"))
        assert comp[0, 1] == pytest.approx(0.5)
        assert thr[0, 1] == pytest.approx(0.75)

    def test_fielder_matrices_are_per_position(self, stub_engines, tmp_path):
        build_actor_sim_matrices("x.duckdb", str(tmp_path), [2024])
        index, mat = _load_npz(str(tmp_path / "actor_sim" / "fielder_SS.npz"))
        assert index == {"11:SS:2024": 0, "12:SS:2024": 1}
        assert mat.shape == (2, 2)
        cf_index, _ = _load_npz(str(tmp_path / "actor_sim" / "fielder_CF.npz"))
        assert cf_index == {"13:CF:2024": 0}

    def test_a_profile_the_engine_does_not_score_is_nan(self, stub_engines, tmp_path):
        # Two seasons in the window: the stub scores 2023 vs 2024 at 0.2, so
        # every cell is filled. Drop the cross-season score and the cell is NaN.
        def _query(self, *key):
            return [r for r in _StubEngine.query(self, *key) if r.season == key[-1]]

        _BatterStub.query = _query  # type: ignore[method-assign]
        try:
            build_actor_sim_matrices("x.duckdb", str(tmp_path), [2023, 2024])
        finally:
            del _BatterStub.query
        index, mat = _load_npz(str(tmp_path / "actor_sim" / "batter.npz"))
        assert index == {"1:2024": 0, "2:2024": 1, "3:2023": 2}
        assert np.isnan(mat[0, 2]) and np.isnan(mat[2, 0])
        assert mat[0, 1] == pytest.approx(0.5)

    def test_limit_scores_the_first_profiles_only(self, stub_engines, tmp_path):
        sizes = build_actor_sim_matrices("x.duckdb", str(tmp_path), [2024], limit=1)
        assert sizes["batter"] == 1 and sizes["fielder_SS"] == 1

    def test_strict_raises_on_a_concentrated_matrix(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ea, "_ENGINE_LOADER", lambda module, cls: _STUBS[cls])
        monkeypatch.setattr(
            ea, "concentration_report", lambda *a, **k: {"catcher": {"ratio_p90": 4.5}}
        )
        with pytest.raises(RuntimeError, match="own-staff ratio is 4.50"):
            build_actor_sim_matrices("x.duckdb", str(tmp_path), [2024], strict=True)
        # A normal build warns and still writes.
        build_actor_sim_matrices("x.duckdb", str(tmp_path), [2024])
        assert (tmp_path / "actor_sim" / "manifest.json").exists()


# ===========================================================================
# The loader and the shared-view seam
# ===========================================================================


def _minimal_pitch_pool_manifest(tmp_path) -> None:
    d = tmp_path / "pitch_pool"
    os.makedirs(d, exist_ok=True)
    with open(d / "manifest.json", "w", encoding="utf-8") as fh:
        json.dump({"seasons": [2024], "counts": {"L": 0, "R": 0}}, fh)
    con = duckdb.connect(":memory:")
    for hand in ("L", "R"):
        np.save(d / f"{hand}.geom.npy", np.zeros((0, 3), dtype=np.float32))
        np.save(d / f"{hand}.sit.npy", np.zeros((0, 6), dtype=np.float32))
        con.execute(
            "COPY (SELECT 0::BIGINT AS pitch_id, 0::BIGINT AS pitcher_id, "
            "0::BIGINT AS batter_id, 0::BIGINT AS season, ''::VARCHAR AS outcome_type, "
            "1.0::FLOAT AS recency_weight WHERE 1=0) "
            f"TO '{(d / f'{hand}.meta.parquet').as_posix()}' (FORMAT parquet)"
        )
    con.close()


class TestTheLoader:
    def test_load_reads_the_matrices_back(self, stub_engines, tmp_path):
        build_actor_sim_matrices("x.duckdb", str(tmp_path), [2024])
        _minimal_pitch_pool_manifest(tmp_path)
        art = EngineArtifacts.load(str(tmp_path))
        assert set(art.actor_sim) >= {"batter", "catcher_throwing", "fielder_SS", "fielder_CF"}
        assert art.actor_sim["batter"]["index"] == {"1:2024": 0, "2:2024": 1}
        assert art.actor_sim["batter"]["matrix"].tolist() == [[1.0, 0.5], [0.5, 1.0]]

    def test_a_legacy_bundle_has_no_matrices(self, tmp_path):
        _minimal_pitch_pool_manifest(tmp_path)
        assert EngineArtifacts.load(str(tmp_path)).actor_sim == {}

    def test_the_matrices_are_shareable(self, stub_engines, tmp_path):
        build_actor_sim_matrices("x.duckdb", str(tmp_path), [2024])
        _minimal_pitch_pool_manifest(tmp_path)
        art = EngineArtifacts.load(str(tmp_path))
        shared = art.extract_shared_arrays()
        assert "actor_sim.batter.matrix" in shared
        assert shared["actor_sim.batter.matrix"] is art.actor_sim["batter"]["matrix"]
        new = np.full((2, 2), 0.9, dtype=np.float32)
        art.attach_shared_views({"actor_sim.batter.matrix": new})
        assert art.actor_sim["batter"]["matrix"] is new
        # A load with the view supplied takes it in place of the disk read.
        art2 = EngineArtifacts.load(str(tmp_path), shared_views={"actor_sim.batter.matrix": new})
        assert art2.actor_sim["batter"]["matrix"] is new


# ===========================================================================
# The sampler: the batter factor on the pitch draw
# ===========================================================================


def _pitch_pool(n: int = 40) -> HandPool:
    """Every row in the empty-bases / no-outs / 0-0 cell; even rows belong to
    batter 200, odd rows to batter 201; the outcome names the batter."""
    idx = np.arange(n)
    bat = np.where(idx % 2 == 0, 200, 201).astype(np.int64)
    sit = np.zeros((n, 6), dtype=np.float32)
    sit[:, 4] = 5.0
    geom = np.zeros((n, 10), dtype=np.float32)
    geom[:, 0] = 90.0 + (idx % 7)
    return HandPool(
        geom=geom,
        sit=sit,
        pitcher_id=np.full(n, 100, dtype=np.int64),
        batter_id=bat,
        season=np.full(n, _SEASON, dtype=np.int64),
        outcome_type=np.asarray([f"bat{b}" for b in bat], dtype=object),
        recency=np.ones(n, dtype=np.float32),
    )


def _batter_emb(keys=(_LIVE_BAT, _OTHER_BAT)) -> dict:
    vecs = np.array([[0.0, 0.0, 0.0], [1.0, -1.0, 0.5]], dtype=np.float32)[: len(keys)]
    return {
        "key_index": {k: i for i, k in enumerate(keys)},
        "vecs": vecs,
        "mean": np.zeros(3, dtype=np.float32),
        "std": np.ones(3, dtype=np.float32),
        "features": ["a", "b", "c"],
    }


def _matrix(index: dict[str, int], rows: list[list[float]]) -> dict:
    return {"index": index, "matrix": np.asarray(rows, dtype=np.float32)}


def _pitch_sampler(*, actor_sim=None, emb_keys=(_LIVE_BAT, _OTHER_BAT), seed=0):
    art = EngineArtifacts(
        pools={"R": _pitch_pool()},
        pitcher_sim={_PITCHER: {_PITCHER: 1.0}},
        pitcher_sim_index={_PITCHER: 0},
        actor_emb={"batter": _batter_emb(emb_keys)},
        actor_sim=actor_sim,
    )
    fp = FullPoolSampler(art, np.random.default_rng(seed))
    return fp


_BASE_OUT = np.array([0, 0, 5, 0], dtype=np.float32)
#: Live batter 200 scores 1.0 vs itself, 0.0 vs 201.
_BAT_MATRIX = _matrix({_LIVE_BAT: 0, _OTHER_BAT: 1}, [[1.0, 0.0], [0.0, 1.0]])


def _pitch_draws(fp: FullPoolSampler, n: int = 80) -> set[str]:
    fp.new_half_inning("R", _PITCHER)
    out = set()
    for _ in range(n):
        fp.new_plate_appearance(_LIVE_BAT, _BASE_OUT)
        out.add(fp.draw(0, 0))
    return out


class TestTheBatterFactor:
    def test_on_gathers_the_live_row(self):
        fp = _pitch_sampler(actor_sim={"batter": _BAT_MATRIX})
        f = fp._f_batter("R", _LIVE_BAT)
        assert f.dtype == np.float32
        assert f[::2].tolist() == [1.0] * 20 and f[1::2].tolist() == [0.0] * 20
        assert _pitch_draws(fp) == {"bat200"}

    def test_a_missing_live_batter_is_neutral(self):
        fp = _pitch_sampler(actor_sim={"batter": _BAT_MATRIX})
        assert fp._f_batter("R", "999:2024").tolist() == [1.0] * 40

    def test_a_pool_batter_the_matrix_lacks_is_neutral(self):
        m = _matrix({_LIVE_BAT: 0}, [[1.0]])
        fp = _pitch_sampler(actor_sim={"batter": m})
        f = fp._f_batter("R", _LIVE_BAT)
        assert f.tolist() == [1.0] * 40

    def test_a_nan_score_is_neutral(self):
        m = _matrix({_LIVE_BAT: 0, _OTHER_BAT: 1}, [[1.0, np.nan], [np.nan, 1.0]])
        fp = _pitch_sampler(actor_sim={"batter": m})
        assert fp._f_batter("R", _LIVE_BAT)[1::2].tolist() == [1.0] * 20

    def test_the_power_reshapes_the_score(self):
        m = _matrix({_LIVE_BAT: 0, _OTHER_BAT: 1}, [[1.0, 0.5], [0.5, 1.0]])
        fp = _pitch_sampler(actor_sim={"batter": m})
        fp.actor_power = {"batter": 2.0}
        assert fp._f_batter("R", _LIVE_BAT)[1] == pytest.approx(0.25)
        fp.actor_power = {"batter": 0.5}
        fp._emb_to_mat_cache.clear()
        assert fp._f_batter("R", _LIVE_BAT)[1] == pytest.approx(0.5**0.5)

    def test_no_matrix_in_the_bundle_leaves_the_batter_neutral(self):
        fp = _pitch_sampler()
        assert fp._f_batter("R", _LIVE_BAT).tolist() == [1.0] * 40
        assert _pitch_draws(fp) == {"bat200", "bat201"}

    def test_the_embedding_to_matrix_map_is_cached(self):
        fp = _pitch_sampler(actor_sim={"batter": _BAT_MATRIX})
        fp._f_batter("R", _LIVE_BAT)
        assert fp._emb_to_mat_cache["batter"].tolist() == [0, 1]
        assert fp._emb_to_mat("batter") is fp._emb_to_mat_cache["batter"]


# ===========================================================================
# The sampler: the runner factor on the steal draw
# ===========================================================================


def _steal_pool(n, attempted, *, runner_ids) -> StealPool:
    att = np.asarray(attempted, dtype=np.int8)
    return StealPool(
        sit=np.zeros((n, 4), dtype=np.float32),
        runner_id=np.asarray(runner_ids, dtype=np.int64),
        pitcher_id=np.full(n, 901, dtype=np.int64),
        catcher_id=np.full(n, 902, dtype=np.int64),
        season=np.full(n, 2024, dtype=np.int64),
        attempted=att,
        success=att.copy(),
        recency=np.ones(n, dtype=np.float32),
    )


def _runner_emb() -> dict:
    feats = ["sprint_speed", "sb_attempt_rate", "sb_success_rate", "cs_rate"]
    vecs = np.array([[30.0, 0.4, 0.85, 0.1], [24.0, 0.01, 0.4, 0.5]], dtype=np.float32)
    return {
        "key_index": {"11:2024": 0, "12:2024": 1},
        "vecs": vecs,
        "mean": vecs.mean(axis=0),
        "std": vecs.std(axis=0) + 1e-6,
        "features": feats,
    }


def _steal_sampler(*, actor_sim=None) -> FullPoolSampler:
    # Burner 11: all attempts; plodder 12: none.
    pool = _steal_pool(400, [1] * 200 + [0] * 200, runner_ids=[11] * 200 + [12] * 200)
    art = EngineArtifacts(
        {}, steal_pools={"2": pool}, actor_emb={"baserunner": _runner_emb()}, actor_sim=actor_sim
    )
    fp = FullPoolSampler(art, np.random.default_rng(7))
    return fp


def _attempt_rate(fp, runner: str, n: int = 300) -> float:
    hits = 0
    for _ in range(n):
        d = fp.steal_draw(2, runner, "", None, outs=0, balls=0, strikes=0, score_diff=0)
        hits += int(d[0])
    return hits / n


class TestTheRunnerFactor:
    def test_the_matrix_conditions_the_steal_draw(self):
        # The live runner 11 scores 1.0 vs his own rows and 0.0 vs 12's, so
        # every draw is an attempt; the plodder draws none.
        m = _matrix({"11:2024": 0, "12:2024": 1}, [[1.0, 0.0], [0.0, 1.0]])
        fp = _steal_sampler(actor_sim={"runner_steal": m})
        assert _attempt_rate(fp, "11:2024") == 1.0
        assert _attempt_rate(fp, "12:2024") == 0.0

    def test_the_wrong_matrix_name_leaves_the_runner_neutral(self):
        # The advancement matrix does not serve the steal draw: with no steal
        # matrix the runner factor is off and the burner draws the pool's half.
        m = _matrix({"11:2024": 0, "12:2024": 1}, [[1.0, 0.0], [0.0, 1.0]])
        fp = _steal_sampler(actor_sim={"runner_adv": m})
        r = _attempt_rate(fp, "11:2024")
        assert 0.35 < r < 0.65


# ===========================================================================
# The sampler: the fielder factor on the batted-ball draw
# ===========================================================================


def _with_transition(pool: BattedBallPool) -> BattedBallPool:
    n = pool.n
    pool.r1_dest = np.full(n, -1, dtype=np.int8)
    pool.r2_dest = np.full(n, -1, dtype=np.int8)
    pool.r3_dest = np.full(n, -1, dtype=np.int8)
    pool.batter_dest = pool.result_hits.astype(np.int8)
    pool.dest_ok = np.ones(n, dtype=np.int8)
    pool.r1_adv_out = np.zeros(n, dtype=np.int8)
    pool.r2_adv_out = np.zeros(n, dtype=np.int8)
    pool.r3_adv_out = np.zeros(n, dtype=np.int8)
    pool.is_air = np.zeros(n, dtype=np.int8)
    pool.spray_raw = np.zeros(n, dtype=np.float32)
    pool.hit_dist = np.zeros(n, dtype=np.float32)
    return pool


def _bb_pool() -> BattedBallPool:
    """Eight shortstop plays: 555 fields four singles, 556 four doubles."""
    n = 8
    return _with_transition(
        BattedBallPool(
            geom=np.zeros((n, 3), dtype=np.float32),
            sit=np.zeros((n, 6), dtype=np.float32),
            batter_id=np.full(n, 700, dtype=np.int64),
            season=np.full(n, _SEASON, dtype=np.int64),
            event=np.asarray(["single"] * 4 + ["double"] * 4, dtype=object),
            result_hits=np.array([1, 1, 1, 1, 2, 2, 2, 2], dtype=np.int8),
            result_outs=np.zeros(n, dtype=np.int8),
            recency=np.ones(n, dtype=np.float32),
            fielder_pos=np.full(n, 6, dtype=np.int8),
            fielder_id=np.array([555, 555, 555, 555, 556, 556, 556, 556], dtype=np.int64),
        )
    )


def _fielder_emb() -> dict:
    return {
        "key_index": {"555:SS:2024": 0, "556:SS:2024": 1, "666:SS:2024": 2},
        "vecs": np.array([[10.0], [-10.0], [0.0]], dtype=np.float32),
        "mean": np.zeros(1, dtype=np.float32),
        "std": np.ones(1, dtype=np.float32),
        "features": ["outs_above_average"],
    }


def _bb_sampler(*, actor_sim=None) -> FullPoolSampler:
    art = EngineArtifacts(
        pools={},
        bb_pools={"R": _bb_pool()},
        actor_emb={"fielder": _fielder_emb()},
        actor_sim=actor_sim,
    )
    fp = FullPoolSampler(art, np.random.default_rng(0))
    return fp


def _bb_events(fp: FullPoolSampler, n: int = 40) -> set[str]:
    out = set()
    for _ in range(n):
        fp.battedball_new_pa(
            "R", "700:2024", np.zeros(6, np.float32), defense_map={"SS": 666}, live_season=_SEASON
        )
        out.add(fp.battedball_draw()[0])
    return out


#: The live shortstop 666 scores 1.0 vs 555 and 0.0 vs 556.
_SS_MATRIX = _matrix(
    {"555:SS:2024": 0, "556:SS:2024": 1, "666:SS:2024": 2},
    [[1.0, 0.0, 1.0], [0.0, 1.0, 0.0], [1.0, 0.0, 1.0]],
)


class TestTheFielderFactor:
    def test_the_position_matrix_picks_the_play(self):
        fp = _bb_sampler(actor_sim={"fielder_SS": _SS_MATRIX})
        assert _bb_events(fp) == {"single"}

    def test_no_matrix_in_the_bundle_leaves_the_fielder_neutral(self):
        fp = _bb_sampler()
        assert _bb_events(fp) == {"single", "double"}

    def test_the_factor_is_mean_one_within_the_position(self):
        fp = _bb_sampler(actor_sim={"fielder_SS": _SS_MATRIX})
        f = fp._f_live_fielder("R", np.arange(8), {"SS": 666}, _SEASON)
        assert f is not None
        assert float(f.mean()) == pytest.approx(1.0)
        assert f[:4].tolist() == [2.0] * 4 and f[4:].tolist() == [0.0] * 4

    def test_another_positions_matrix_leaves_shortstop_neutral(self):
        fp = _bb_sampler(actor_sim={"fielder_CF": _SS_MATRIX})
        f = fp._f_live_fielder("R", np.arange(8), {"SS": 666}, _SEASON)
        assert f is not None and f.tolist() == [1.0] * 8
        assert _bb_events(fp) == {"single", "double"}

    def test_the_fielder_power_applies_per_position(self):
        m = _matrix(
            {"555:SS:2024": 0, "556:SS:2024": 1, "666:SS:2024": 2},
            [[1.0, 0.5, 1.0], [0.5, 1.0, 0.25], [1.0, 0.25, 1.0]],
        )
        fp = _bb_sampler(actor_sim={"fielder_SS": m})
        fp.actor_power = {"fielder_SS": 2.0}
        f = fp._f_live_fielder("R", np.arange(8), {"SS": 666}, _SEASON)
        # Raw 1.0 vs 0.0625, rescaled to mean 1 within the position.
        raw = np.array([1.0] * 4 + [0.0625] * 4)
        np.testing.assert_allclose(f, raw / raw.mean(), rtol=1e-5)


# ===========================================================================
# The factory env reads
# ===========================================================================


class TestTheFactoryEnv:
    def test_no_powers_reads_empty(self):
        s = SimpleNamespace()
        apply_actor_matrix_env(s, env={})
        assert s.actor_power == {}
        assert not hasattr(s, "actor_matrices")  # the switch is retired

    def test_the_powers(self):
        s = SimpleNamespace()
        apply_actor_matrix_env(
            s,
            env={
                "SIM_ACTOR_POWER_BATTER": "0.5",
                "SIM_ACTOR_POWER_FIELDER": "2",
                "SIM_ACTOR_POWER_FIELDER_SS": "3",
                "SIM_ACTOR_POWER_RUNNER_STEAL": "junk",
            },
        )
        assert s.actor_power["batter"] == 0.5
        assert s.actor_power["fielder_SS"] == 3.0  # the explicit one wins
        assert s.actor_power["fielder_CF"] == 2.0  # the fielder default fans out
        assert "runner_steal" not in s.actor_power


# ===========================================================================
# The concentration arithmetic
# ===========================================================================


class TestConcentrationShare:
    def test_toy_pool(self):
        # Four pool rows: two thrown by the live staff (pitchers 1, 2), two by
        # others (3, 4). The live row weights his own staff's catchers at 1.0
        # and the others at 0.25.
        matrix = np.array([[1.0, 1.0, 0.25, 0.25]], dtype=np.float32)
        cols = np.array([0, 1, 2, 3])
        pitchers = np.array([1, 2, 3, 4])
        own, unweighted, ess = concentration_share(matrix, 0, cols, pitchers, {1, 2})
        assert unweighted == pytest.approx(0.5)
        assert own == pytest.approx(2.0 / 2.5)
        # ESS = (sum w)^2 / sum w^2 / n = 6.25 / 2.125 / 4
        assert ess == pytest.approx(6.25 / 2.125 / 4)

    def test_unscored_rows_are_skipped_and_nan_is_neutral(self):
        matrix = np.array([[np.nan, 0.5]], dtype=np.float32)
        cols = np.array([-1, 0, 1])
        pitchers = np.array([9, 1, 2])
        own, unweighted, _ = concentration_share(matrix, 0, cols, pitchers, {1})
        assert unweighted == pytest.approx(0.5)
        assert own == pytest.approx(1.0 / 1.5)

    def test_no_scored_rows_is_nan(self):
        matrix = np.ones((1, 1), dtype=np.float32)
        own, unweighted, ess = concentration_share(
            matrix, 0, np.array([-1, -1]), np.array([1, 2]), {1}
        )
        assert np.isnan(own) and np.isnan(unweighted) and np.isnan(ess)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
