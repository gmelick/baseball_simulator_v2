"""
test_similarity_endpoint.py
============================
Unit tests for the Similarity Score Explorer endpoint.

Covers (per docs/similarity_visualization_spec.md §5 acceptance criteria):
  - Pure helpers: _BinSpec, compute_score_summary, build_bins,
    classify_diagnostic.
  - Route handler payload shape on the happy path.
  - 404 on unknown pitcher.
  - 503 when the engine has not been attached to app.state.
  - Top-N highlight band cutoff is correct.
  - Diagnostic classification (HEALTHY / NO_SPREAD / COLLAPSED).

Tests do NOT require a live PostgreSQL or DuckDB. They follow the
project's "construct via __new__" pattern from
tests/unit/test_pitcher_similarity.py: the engine is replaced with a
lightweight stub object that exposes the two attributes the route uses
(``query`` and ``_profiles``). This keeps the suite fast and CI-friendly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pytest

# pytest will collect this whole file under ``tests/unit``. The api
# package is importable because the project root is on sys.path
# (ruff/pytest src config in pyproject.toml).
from api.routes.similarity import (
    _BinSpec,
    build_bins,
    build_top_n,
    classify_diagnostic,
    compute_score_summary,
    COLLAPSED_MAX_BIN_FRACTION,
    DIAGNOSTIC_MEDIAN_TARGET,
    NO_SPREAD_STD_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Helpers — minimal stand-ins for SimilarityResult and the engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StubResult:
    """Duck-types similarity.engines.pitcher_similarity.SimilarityResult."""
    pitcher_id: int
    season: int
    p_throws: str
    score: float
    arsenal_score: float
    command_score: float
    sample_pitches: int


@dataclass(frozen=True)
class _StubProfile:
    """Duck-types PitcherProfile, only exposes what the route reads."""
    p_throws: str


class _StubEngine:
    """
    Stand-in for PitcherSimilarityEngine. Mirrors the ``query`` contract
    and exposes ``_profiles`` so the route can echo p_throws back in the
    response. Built deliberately without inheriting from the real engine
    (no numpy / duckdb deps imported into the test).
    """

    def __init__(self, query_result_map, profiles_map):
        # query_result_map: {(pitcher_id, season): list[_StubResult]}
        # profiles_map:     {(pitcher_id, season): _StubProfile}
        self._query_map = query_result_map
        self._profiles = profiles_map  # name matches the route's lookup

    def query(self, pitcher_id: int, season: int, n: int | None = None):
        results = self._query_map.get((pitcher_id, season), [])
        # Engine returns sorted desc by score already.
        results = sorted(results, key=lambda r: r.score, reverse=True)
        return results if n is None else results[:n]


def _name_resolver_factory(names: dict[int, str]):
    """Async name resolver — matches the NameResolver Protocol used by the route."""
    async def _resolve(ids: Iterable[int]) -> dict[int, str]:
        return {pid: names.get(pid, f"Pitcher #{pid}") for pid in ids}
    return _resolve


class _InMemoryCache:
    """
    Tiny in-memory stand-in for SimilarityCache. Tracks calls so tests
    can assert the route hit / wrote through correctly. TTL is ignored
    (this is a test fake, not a TTL-aware cache).
    """

    def __init__(self, preload: dict | None = None) -> None:
        self._store: dict[str, dict] = dict(preload or {})
        self.get_calls: list[str] = []
        self.set_calls: list[tuple[str, dict, int]] = []

    async def get_json(self, key: str):
        self.get_calls.append(key)
        return self._store.get(key)

    async def set_json(self, key: str, value: dict, ttl_s: int = 0) -> None:
        self.set_calls.append((key, value, ttl_s))
        self._store[key] = value


# ---------------------------------------------------------------------------
# _BinSpec
# ---------------------------------------------------------------------------


class TestBinSpec:

    def test_linear_bins_have_uniform_width(self):
        spec = _BinSpec.linear(20)
        assert spec.n_bins == 20
        assert spec.width == pytest.approx(0.05)

    def test_score_zero_falls_in_first_bin(self):
        spec = _BinSpec.linear(20)
        assert spec.index_for(0.0) == 0

    def test_score_one_falls_in_last_bin_inclusive(self):
        # Critical: score=1.0 must have a home, not fall off the end.
        spec = _BinSpec.linear(20)
        assert spec.index_for(1.0) == 19

    def test_score_at_bin_edge_falls_into_upper_bin(self):
        spec = _BinSpec.linear(10)  # width 0.1
        # 0.5 should fall into bin 5 ([0.5, 0.6)), NOT bin 4.
        assert spec.index_for(0.5) == 5

    def test_negative_score_clamped_to_zero(self):
        spec = _BinSpec.linear(20)
        assert spec.index_for(-0.01) == 0

    def test_above_one_clamped_to_last_bin(self):
        spec = _BinSpec.linear(20)
        assert spec.index_for(1.5) == 19

    def test_invalid_n_bins_raises(self):
        with pytest.raises(ValueError):
            _BinSpec.linear(0)
        with pytest.raises(ValueError):
            _BinSpec.linear(-3)

    def test_edges_are_correct(self):
        spec = _BinSpec.linear(20)
        lo, hi = spec.edges(0)
        assert (lo, hi) == (0.0, 0.05)
        lo, hi = spec.edges(19)
        assert (lo, hi) == (0.95, 1.0)


# ---------------------------------------------------------------------------
# compute_score_summary
# ---------------------------------------------------------------------------


class TestScoreSummary:

    def test_empty_returns_zeros(self):
        s = compute_score_summary([])
        assert s == dict(min=0.0, p25=0.0, median=0.0, p75=0.0,
                         max=0.0, mean=0.0, std=0.0)

    def test_singleton_population(self):
        s = compute_score_summary([0.42])
        assert s["min"] == s["median"] == s["max"] == 0.42
        assert s["mean"] == pytest.approx(0.42)
        assert s["std"] == pytest.approx(0.0)

    def test_uniform_population_quartiles(self):
        # Range 0.0..1.0 in 11 evenly spaced points → median 0.5
        scores = [i / 10 for i in range(11)]
        s = compute_score_summary(scores)
        assert s["min"] == pytest.approx(0.0)
        assert s["max"] == pytest.approx(1.0)
        assert s["median"] == pytest.approx(0.5)
        assert s["p25"] == pytest.approx(0.25)
        assert s["p75"] == pytest.approx(0.75)
        assert s["mean"] == pytest.approx(0.5)
        # Population std (numpy default), not sample std.
        # std of evenly spaced 0..1 with N=11 ≈ 0.3162
        assert s["std"] == pytest.approx(0.3162, rel=1e-3)


# ---------------------------------------------------------------------------
# build_bins / build_top_n
# ---------------------------------------------------------------------------


class TestBuildBins:

    @staticmethod
    def _result(pid: int, season: int, score: float, arsenal: float = 0.5,
                command: float = 0.5, p_throws: str = "R", n: int = 500):
        return _StubResult(pid, season, p_throws, score, arsenal, command, n)

    def test_every_bin_present_even_if_empty(self):
        results = [self._result(1, 2024, 0.04), self._result(2, 2024, 0.97)]
        bins = build_bins(results, names={1: "A", 2: "B"}, n_bins=10)
        assert len(bins) == 10
        # Bins 1..8 should be empty.
        for i in range(1, 9):
            assert bins[i]["count"] == 0
            assert bins[i]["members"] == []

    def test_member_assignment_and_sort_within_bin(self):
        # Three pitchers all in bin [0.80, 0.85), expect descending by score.
        results = [
            self._result(10, 2024, 0.81, arsenal=0.7, command=0.9),
            self._result(20, 2024, 0.84, arsenal=0.9, command=0.8),
            self._result(30, 2024, 0.82, arsenal=0.8, command=0.85),
        ]
        names = {10: "Foo", 20: "Bar", 30: "Baz"}
        bins = build_bins(results, names, n_bins=20)

        bin16 = bins[16]  # [0.80, 0.85)
        assert bin16["count"] == 3
        scores_in_order = [m["score"] for m in bin16["members"]]
        assert scores_in_order == sorted(scores_in_order, reverse=True)
        # Top of the bin should be id=20 (score 0.84).
        assert bin16["members"][0]["pitcher_id"] == 20
        assert bin16["members"][0]["full_name"] == "Bar"

    def test_preview_is_top_5_subset_of_members(self):
        results = [self._result(i, 2024, 0.51 + i * 0.0001) for i in range(20)]
        bins = build_bins(results, names={}, n_bins=20, preview_size=5)
        bin10 = bins[10]
        assert bin10["count"] == 20
        assert len(bin10["preview"]) == 5
        # Preview is exactly the prefix of members.
        assert bin10["preview"] == bin10["members"][:5]

    def test_score_one_lands_in_final_bin(self):
        results = [self._result(1, 2024, 1.0)]
        bins = build_bins(results, names={1: "Perfect"}, n_bins=10)
        assert bins[-1]["count"] == 1
        assert bins[-1]["members"][0]["score"] == pytest.approx(1.0)

    def test_unknown_id_falls_back_to_placeholder_name(self):
        results = [self._result(999, 2024, 0.5)]
        bins = build_bins(results, names={}, n_bins=20)
        member = bins[10]["members"][0]
        assert member["full_name"] == "Pitcher #999"


class TestBuildTopN:

    def test_top_n_returns_first_n_engine_results(self):
        # build_top_n trusts the engine's pre-sorted order — it does NOT
        # re-sort. This test pins that contract.
        results = [
            _StubResult(1, 2024, "R", 0.91, 0.94, 0.86, 1000),
            _StubResult(2, 2024, "R", 0.88, 0.91, 0.79, 1500),
            _StubResult(3, 2024, "R", 0.86, 0.86, 0.82,  900),
            _StubResult(4, 2024, "R", 0.10, 0.05, 0.20,  800),
        ]
        out = build_top_n(results, names={1: "A", 2: "B", 3: "C", 4: "D"}, n=3)
        assert [m["pitcher_id"] for m in out] == [1, 2, 3]
        assert all("full_name" in m for m in out)


# ---------------------------------------------------------------------------
# classify_diagnostic
# ---------------------------------------------------------------------------


class TestDiagnostic:

    def test_healthy_distribution(self):
        # Spread roughly normal around 0.5 — should be HEALTHY.
        scores = [0.1, 0.3, 0.4, 0.45, 0.5, 0.55, 0.6, 0.7, 0.9]
        d = classify_diagnostic(scores, n_bins=20)
        assert d["status"] == "HEALTHY"
        assert d["median_target"] == DIAGNOSTIC_MEDIAN_TARGET
        assert d["median_observed"] == pytest.approx(0.5)

    def test_no_spread_when_std_too_low(self):
        # All scores identical → std == 0 → NO_SPREAD.
        scores = [0.5] * 20
        d = classify_diagnostic(scores, n_bins=20)
        assert d["status"] == "NO_SPREAD"
        assert d["median_observed"] == pytest.approx(0.5)

    def test_collapsed_when_one_bin_dominates(self):
        # 95% of scores in a single bin, std non-trivial.
        # Anchor std comfortably above NO_SPREAD_STD_THRESHOLD so the
        # NO_SPREAD branch doesn't pre-empt COLLAPSED.
        scores = [0.50] * 95 + [0.05, 0.10, 0.85, 0.95, 0.99]
        d = classify_diagnostic(scores, n_bins=20)
        # Sanity: confirm we're outside the NO_SPREAD branch.
        std = compute_score_summary(scores)["std"]
        assert std > NO_SPREAD_STD_THRESHOLD
        assert d["status"] == "COLLAPSED"

    def test_collapsed_threshold_constant(self):
        assert 0.0 < COLLAPSED_MAX_BIN_FRACTION <= 1.0


# ---------------------------------------------------------------------------
# Route handler — full HTTP path via FastAPI TestClient
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_with_stub_engine():
    """Build a FastAPI app, attach a stub engine + name resolver."""
    fastapi = pytest.importorskip("fastapi")
    pytest.importorskip("starlette")  # FastAPI's test client dep

    from fastapi import FastAPI
    from api.routes.similarity import router

    app = FastAPI()
    app.include_router(router)

    # Stub engine: query for (694973, 2025) returns a healthy spread of comps.
    skenes_key = (694973, 2025)
    comps = [
        # Top-3: clearly the most similar
        _StubResult(594798, 2024, "R", 0.91, 0.94, 0.86, 1842),  # deGrom
        _StubResult(657277, 2023, "R", 0.88, 0.91, 0.79, 1505),  # Glasnow
        _StubResult(675911, 2024, "R", 0.86, 0.86, 0.82,  920),  # Strider
        # Mid-pack
        _StubResult(111, 2024, "R", 0.62, 0.65, 0.58, 1200),
        _StubResult(112, 2024, "R", 0.55, 0.50, 0.60, 1100),
        _StubResult(113, 2024, "R", 0.51, 0.49, 0.53, 1000),
        _StubResult(114, 2024, "R", 0.48, 0.45, 0.51,  900),
        _StubResult(115, 2024, "R", 0.42, 0.40, 0.44,  800),
        # Long left tail
        _StubResult(201, 2024, "R", 0.18, 0.10, 0.26,  700),
        _StubResult(202, 2024, "R", 0.05, 0.02, 0.08,  600),
    ]
    app.state.pitcher_engine = _StubEngine(
        query_result_map={skenes_key: comps},
        profiles_map={skenes_key: _StubProfile(p_throws="R")},
    )
    app.state.player_name_resolver = _name_resolver_factory({
        694973: "Paul Skenes",
        594798: "Jacob deGrom",
        657277: "Tyler Glasnow",
        675911: "Spencer Strider",
    })
    return app


def test_endpoint_returns_documented_shape(app_with_stub_engine):
    from fastapi.testclient import TestClient
    client = TestClient(app_with_stub_engine)

    resp = client.get("/api/similarity/pitcher/694973/2025?bins=20&top_n=3")
    assert resp.status_code == 200
    body = resp.json()

    # Top-level shape exactly matches the spec.
    assert set(body.keys()) == {
        "query", "engine", "engine_version", "population_size",
        "score_summary", "bins", "top_n", "diagnostic",
    }
    assert body["engine"] == "pitcher_similarity"
    assert body["query"] == {
        "pitcher_id": 694973, "season": 2025,
        "p_throws": "R", "full_name": "Paul Skenes",
    }
    assert body["population_size"] == 10

    # Bins: 20 of them, sum of counts == population size.
    assert len(body["bins"]) == 20
    assert sum(b["count"] for b in body["bins"]) == 10

    # Top-N: 3 entries, names resolved.
    assert len(body["top_n"]) == 3
    names = [m["full_name"] for m in body["top_n"]]
    assert names == ["Jacob deGrom", "Tyler Glasnow", "Spencer Strider"]


def test_endpoint_returns_404_for_unknown_pitcher(app_with_stub_engine):
    from fastapi.testclient import TestClient
    client = TestClient(app_with_stub_engine)

    resp = client.get("/api/similarity/pitcher/999999/2025")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "pitcher not in engine"


def test_endpoint_returns_503_when_engine_not_attached():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes.similarity import router

    app = FastAPI()
    app.include_router(router)
    # Deliberately do NOT attach app.state.pitcher_engine.

    client = TestClient(app)
    resp = client.get("/api/similarity/pitcher/694973/2025")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "engine warming"


def test_top_n_param_caps_results(app_with_stub_engine):
    from fastapi.testclient import TestClient
    client = TestClient(app_with_stub_engine)

    resp = client.get("/api/similarity/pitcher/694973/2025?top_n=2")
    assert resp.status_code == 200
    assert len(resp.json()["top_n"]) == 2


def test_invalid_bins_param_rejected(app_with_stub_engine):
    """bins is bounded in [4, 100] by the route validator."""
    from fastapi.testclient import TestClient
    client = TestClient(app_with_stub_engine)

    # Below floor → 422 from FastAPI's query-param validator.
    resp = client.get("/api/similarity/pitcher/694973/2025?bins=2")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Redis cache integration
# ---------------------------------------------------------------------------


def test_cache_miss_invokes_engine_and_writes_through(app_with_stub_engine):
    """
    First call for a (pitcher, season, bins, top_n) tuple is a cache
    miss — the engine is queried and the resulting payload is written
    to the cache under the spec-defined key.
    """
    from fastapi.testclient import TestClient
    from api.state import make_cache_key

    cache = _InMemoryCache()
    app_with_stub_engine.state.similarity_cache = cache

    client = TestClient(app_with_stub_engine)
    resp = client.get("/api/similarity/pitcher/694973/2025?bins=20&top_n=20")
    assert resp.status_code == 200

    expected_key = make_cache_key(694973, 2025, 20, 20)
    # Exactly one get (miss) and one set (write-through).
    assert cache.get_calls == [expected_key]
    assert len(cache.set_calls) == 1
    written_key, written_value, ttl = cache.set_calls[0]
    assert written_key == expected_key
    assert written_value["query"]["pitcher_id"] == 694973
    assert ttl == 24 * 60 * 60  # spec: 24h


def test_cache_hit_skips_engine(app_with_stub_engine):
    """
    A preloaded cache entry is returned directly. Confirm the engine
    is NOT consulted by swapping in an engine that would explode if
    called.
    """
    from fastapi.testclient import TestClient
    from api.state import make_cache_key

    class _ExplodingEngine:
        _profiles = {(1, 2025): None}

        def query(self, *_args, **_kwargs):
            raise AssertionError("engine.query should not be called on cache hit")

    canned_payload = {
        "query": {"pitcher_id": 1, "season": 2025, "p_throws": "R",
                  "full_name": "From Cache"},
        "engine": "pitcher_similarity",
        "engine_version": "0.1.0-phase2",
        "population_size": 0,
        "score_summary": {"min": 0, "p25": 0, "median": 0, "p75": 0,
                          "max": 0, "mean": 0, "std": 0},
        "bins": [],
        "top_n": [],
        "diagnostic": {"status": "HEALTHY", "median_target": 0.5,
                       "median_observed": 0.0},
    }
    cache = _InMemoryCache(preload={
        make_cache_key(1, 2025, 20, 20): canned_payload,
    })
    app_with_stub_engine.state.pitcher_engine = _ExplodingEngine()
    app_with_stub_engine.state.similarity_cache = cache

    client = TestClient(app_with_stub_engine)
    resp = client.get("/api/similarity/pitcher/1/2025?bins=20&top_n=20")
    assert resp.status_code == 200
    assert resp.json()["query"]["full_name"] == "From Cache"
    # No write-through on a hit.
    assert cache.set_calls == []


def test_cache_optional_when_state_missing(app_with_stub_engine):
    """
    The route degrades gracefully when no similarity_cache is attached.
    This is the dev-without-Redis path.
    """
    from fastapi.testclient import TestClient

    # Make sure no cache is attached (fixture doesn't attach one by default).
    if hasattr(app_with_stub_engine.state, "similarity_cache"):
        delattr(app_with_stub_engine.state, "similarity_cache")

    client = TestClient(app_with_stub_engine)
    resp = client.get("/api/similarity/pitcher/694973/2025")
    assert resp.status_code == 200


def test_different_bin_counts_use_different_cache_keys(app_with_stub_engine):
    """
    Two different bin counts must not collide in the cache — the key
    format pins this contract for the eventual nightly invalidator.
    """
    from fastapi.testclient import TestClient
    from api.state import make_cache_key

    cache = _InMemoryCache()
    app_with_stub_engine.state.similarity_cache = cache

    client = TestClient(app_with_stub_engine)
    client.get("/api/similarity/pitcher/694973/2025?bins=10&top_n=20")
    client.get("/api/similarity/pitcher/694973/2025?bins=40&top_n=20")

    keys_written = {k for k, _, _ in cache.set_calls}
    assert keys_written == {
        make_cache_key(694973, 2025, 10, 20),
        make_cache_key(694973, 2025, 40, 20),
    }
