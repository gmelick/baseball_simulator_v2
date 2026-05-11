"""
api/routes/similarity.py
========================
Similarity Score Explorer endpoints.

v1 scope: Pitcher → Pitcher engine (the other 8 built engines reuse the
same response shape and frontend component in v2).

Design contract (full doc: ``docs/similarity_visualization_spec.md``):

    GET /api/similarity/pitcher/{pitcher_id}/{season}
        ?bins=20&top_n=20
        → {
            query: {pitcher_id, season, p_throws, full_name},
            engine, engine_version, population_size,
            score_summary: {min, p25, median, p75, max, mean, std},
            bins:   [{bin_index, lo, hi, count, preview, members}, ...],
            top_n:  [member, ...],
            diagnostic: {status, median_target, median_observed},
        }

The route handler is intentionally thin: it asks the engine for the
SimilarityResult list, resolves names, and delegates everything else to
pure helpers in this module. Those helpers are unit-tested in
``tests/unit/test_similarity_endpoint.py`` without an HTTP layer.

Agent ownership for this slice (per docs/similarity_visualization_spec.md
§2): Backend Developer owns the route handler + caching; ML Engineer
owns the diagnostic thresholds and bin semantics; Performance Engineer
signed off on the cache TTL.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from api.state import (
    CACHE_TTL_SECONDS,
    NameResolver,
    SimilarityCache,
    make_cache_key,
)

# Type-only import keeps this module importable in environments where
# the heavy similarity engine deps (numpy, scipy, duckdb) are not present
# (e.g. lightweight CI lint jobs).
try:  # pragma: no cover — import path varies in test contexts
    from similarity.engines.pitcher_similarity import (
        PitcherSimilarityEngine,
        SimilarityResult,
    )
except Exception:  # pragma: no cover — fallback for partial test setups
    PitcherSimilarityEngine = object  # type: ignore[assignment,misc]
    SimilarityResult = object  # type: ignore[assignment,misc]


log = logging.getLogger("api.routes.similarity")

router = APIRouter(prefix="/api/similarity", tags=["similarity"])


# ---------------------------------------------------------------------------
# Diagnostic thresholds — owned by ML Engineer (Agent 3).
# Must match the values documented in
# docs/similarity_visualization_spec.md §3 "Diagnostic logic".
# ---------------------------------------------------------------------------

DIAGNOSTIC_MEDIAN_TARGET = 0.50  # project-wide engine target
COLLAPSED_MAX_BIN_FRACTION = 0.80  # ≥80% of scores in one bin = COLLAPSED
NO_SPREAD_STD_THRESHOLD = 0.05  # std < 0.05 = NO_SPREAD


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class _Member(BaseModel):
    """One pitcher's appearance in a bin / preview / top-N list."""

    pitcher_id: int
    season: int
    full_name: str
    p_throws: str
    score: float = Field(..., ge=0.0, le=1.0)
    arsenal_score: float
    command_score: float
    sample_pitches: int


class _Bin(BaseModel):
    bin_index: int
    lo: float
    hi: float
    count: int
    preview: list[_Member]
    members: list[_Member]


class _ScoreSummary(BaseModel):
    min: float
    p25: float
    median: float
    p75: float
    max: float
    mean: float
    std: float


class _Diagnostic(BaseModel):
    status: str  # HEALTHY | COLLAPSED | NO_SPREAD
    median_target: float
    median_observed: float


class _Query(BaseModel):
    pitcher_id: int
    season: int
    p_throws: str
    full_name: str


class SimilarityDistributionResponse(BaseModel):
    """Top-level response for the histogram endpoint."""

    query: _Query
    engine: str
    engine_version: str
    population_size: int
    score_summary: _ScoreSummary
    bins: list[_Bin]
    top_n: list[_Member]
    diagnostic: _Diagnostic


# ---------------------------------------------------------------------------
# Dependency contracts
# ---------------------------------------------------------------------------
#
# The Protocol types (NameResolver, SimilarityCache) are owned by
# api/state.py — re-imported above so the route file remains the single
# place where the HTTP-level contract lives.


def get_pitcher_engine(request: Request):
    """
    FastAPI dependency — returns the singleton PitcherSimilarityEngine
    from app state. The lifespan in ``api.main`` is responsible for
    ``engine.build()`` having been called before the app accepts traffic.

    Returns 503 if the engine is not yet attached to app.state.
    """
    engine = getattr(request.app.state, "pitcher_engine", None)
    if engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="engine warming",
        )
    return engine


def get_name_resolver(request: Request) -> NameResolver:
    """
    FastAPI dependency — returns an async callable that resolves
    pitcher_ids to full_names. The lifespan attaches a Postgres-backed
    resolver to ``app.state.player_name_resolver``.

    Falls back to an async stub returning ``"Pitcher #<id>"`` so the
    endpoint still serves something useful in degraded environments
    (e.g. tests, dev with no DB). The fallback is logged at WARNING so
    nobody ships to staging without noticing.
    """
    resolver = getattr(request.app.state, "player_name_resolver", None)
    if resolver is None:
        log.warning(
            "player_name_resolver not attached to app.state; "
            "returning placeholder names. Wire up in lifespan."
        )

        async def _placeholder(ids: Iterable[int]) -> dict[int, str]:
            return {pid: f"Pitcher #{pid}" for pid in ids}

        return _placeholder
    return resolver


def get_similarity_cache(request: Request) -> SimilarityCache | None:
    """
    FastAPI dependency — returns the Redis-backed SimilarityCache if
    the lifespan attached one, else None. A None cache means "skip the
    cache entirely" — the route handles that path explicitly so dev
    setups without Redis still work.
    """
    return getattr(request.app.state, "similarity_cache", None)


# ---------------------------------------------------------------------------
# Pure helpers — unit-tested without HTTP
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _BinSpec:
    """Bin edges; rightmost bin is inclusive of 1.0."""

    n_bins: int
    width: float

    @classmethod
    def linear(cls, n_bins: int) -> _BinSpec:
        if n_bins <= 0:
            raise ValueError(f"n_bins must be positive, got {n_bins}")
        return cls(n_bins=n_bins, width=1.0 / n_bins)

    def index_for(self, score: float) -> int:
        # Clamp to [0, 1] in case of float drift from engine.
        s = max(0.0, min(1.0, score))
        # Right edge: score=1.0 belongs in the last bin (inclusive top).
        if s >= 1.0:
            return self.n_bins - 1
        return int(s / self.width)

    def edges(self, idx: int) -> tuple[float, float]:
        lo = idx * self.width
        hi = lo + self.width
        return (round(lo, 6), round(hi, 6))


def compute_score_summary(scores: list[float]) -> dict[str, float]:
    """
    Min/p25/median/p75/max/mean/std over the full population. Empty
    population returns all zeros — caller should already have raised 404
    in that case, this is just defensive.

    Implementation note: avoids numpy so this helper stays importable in
    a numpy-free environment (matches the optional import at top of the
    module).
    """
    if not scores:
        return {
            "min": 0.0,
            "p25": 0.0,
            "median": 0.0,
            "p75": 0.0,
            "max": 0.0,
            "mean": 0.0,
            "std": 0.0,
        }

    s = sorted(scores)
    n = len(s)

    def _percentile(p: float) -> float:
        # Linear interpolation between order statistics — matches numpy default.
        if n == 1:
            return s[0]
        rank = p * (n - 1)
        lo = int(math.floor(rank))
        hi = int(math.ceil(rank))
        if lo == hi:
            return s[lo]
        frac = rank - lo
        return s[lo] + frac * (s[hi] - s[lo])

    mean = sum(s) / n
    var = sum((x - mean) ** 2 for x in s) / n  # population std, matches numpy.std default
    std = math.sqrt(var)

    return {
        "min": s[0],
        "p25": _percentile(0.25),
        "median": _percentile(0.50),
        "p75": _percentile(0.75),
        "max": s[-1],
        "mean": mean,
        "std": std,
    }


def classify_diagnostic(
    scores: list[float],
    n_bins: int,
) -> dict[str, float | str]:
    """
    Classify the engine's output health for this query:
      - COLLAPSED  if any single bin contains ≥80% of mass
      - NO_SPREAD  if population std < 0.05
      - HEALTHY    otherwise

    Thresholds mirror similarity_diagnostics.py conceptually and are
    pinned in this module's constants for the Backend / ML Engineer
    contract.
    """
    if not scores:
        return {
            "status": "HEALTHY",
            "median_target": DIAGNOSTIC_MEDIAN_TARGET,
            "median_observed": 0.0,
        }

    summary = compute_score_summary(scores)

    if summary["std"] < NO_SPREAD_STD_THRESHOLD:
        status_label = "NO_SPREAD"
    else:
        spec = _BinSpec.linear(n_bins)
        counts = [0] * n_bins
        for s in scores:
            counts[spec.index_for(s)] += 1
        max_frac = max(counts) / len(scores)
        status_label = "COLLAPSED" if max_frac >= COLLAPSED_MAX_BIN_FRACTION else "HEALTHY"

    return {
        "status": status_label,
        "median_target": DIAGNOSTIC_MEDIAN_TARGET,
        "median_observed": summary["median"],
    }


def _result_to_member(
    result,  # SimilarityResult duck-typed (pitcher_id, season, p_throws,
    # score, arsenal_score, command_score, sample_pitches)
    names: dict[int, str],
) -> dict:
    return {
        "pitcher_id": result.pitcher_id,
        "season": result.season,
        "full_name": names.get(result.pitcher_id, f"Pitcher #{result.pitcher_id}"),
        "p_throws": result.p_throws,
        "score": float(result.score),
        "arsenal_score": float(result.arsenal_score),
        "command_score": float(result.command_score),
        "sample_pitches": int(result.sample_pitches),
    }


def build_bins(
    results: list,  # list[SimilarityResult]
    names: dict[int, str],
    n_bins: int = 20,
    preview_size: int = 5,
) -> list[dict]:
    """
    Construct the histogram bin payload from the engine's full scored
    list. Each bin's ``members`` is sorted by score descending; the
    ``preview`` is the top ``preview_size`` of that list. Bins with
    count==0 are still returned (so the frontend doesn't have to fill
    gaps) — but their members and preview lists are empty.
    """
    spec = _BinSpec.linear(n_bins)

    buckets: list[list] = [[] for _ in range(n_bins)]
    for r in results:
        idx = spec.index_for(float(r.score))
        buckets[idx].append(r)

    bins_payload: list[dict] = []
    for i, bucket in enumerate(buckets):
        bucket.sort(key=lambda r: float(r.score), reverse=True)
        members = [_result_to_member(r, names) for r in bucket]
        lo, hi = spec.edges(i)
        bins_payload.append(
            {
                "bin_index": i,
                "lo": lo,
                "hi": hi,
                "count": len(members),
                "preview": members[:preview_size],
                "members": members,
            }
        )

    return bins_payload


def build_top_n(
    results: list,  # already sorted desc by engine
    names: dict[int, str],
    n: int,
) -> list[dict]:
    return [_result_to_member(r, names) for r in results[:n]]


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


@router.get(
    "/pitcher/{pitcher_id}/{season}",
    response_model=SimilarityDistributionResponse,
    summary="Pitcher similarity score distribution",
    description=(
        "Returns the full similarity-score distribution between the query "
        "pitcher-season and every same-handedness pitcher-season in the "
        "engine, binned for the histogram view. See "
        "``docs/similarity_visualization_spec.md`` for the full design."
    ),
)
async def get_pitcher_similarity_distribution(
    pitcher_id: int,
    season: int,
    bins: int = Query(20, ge=4, le=100, description="Histogram bin count"),
    top_n: int = Query(20, ge=1, le=200, description="Top-N highlight band"),
    engine=Depends(get_pitcher_engine),
    name_resolver: NameResolver = Depends(get_name_resolver),
    cache: SimilarityCache | None = Depends(get_similarity_cache),
) -> SimilarityDistributionResponse:
    """
    Per the design spec §3:
      404 — pitcher not in engine
      503 — engine warming (raised in get_pitcher_engine dependency)

    Caching:
      The full response payload is cached in Redis under
      ``simviz:pitcher:{id}:{season}:bins={b}:top_n={n}`` with a 24h
      TTL. The engine output is deterministic for a fixed
      (pitcher_id, season) until the next nightly profile rebuild —
      long TTL is safe (Performance Engineer-approved).

      Cache invalidation on profile rebuild is a Data Engineer
      follow-on; see docs/similarity_visualization_spec.md §6.
    """
    # ---- Cache check (fast path) ----
    cache_key = make_cache_key(pitcher_id, season, bins, top_n)
    if cache is not None:
        cached = await cache.get_json(cache_key)
        if cached is not None:
            return SimilarityDistributionResponse(**cached)

    # ---- Cold path: query the engine ----
    # engine.query is CPU-bound and may pay a ~1.2s arsenal-cache
    # warmup on the first call for a pitcher. Run it in a worker thread
    # so the event loop stays responsive.
    results = await asyncio.to_thread(engine.query, pitcher_id, season, None)
    if not results:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="pitcher not in engine",
        )

    # Resolve names. Include the query pitcher so the response header
    # carries the human-readable name too.
    ids_needed = {r.pitcher_id for r in results}
    ids_needed.add(pitcher_id)
    names = await name_resolver(ids_needed)

    # Pull query metadata directly off the engine's stored profile so we
    # can echo p_throws and full_name back to the frontend.
    query_profile = engine._profiles.get((pitcher_id, season))  # noqa: SLF001
    if query_profile is None:
        # Defensive: engine.query() returned non-empty above; if the
        # profile dict is somehow inconsistent we surface as 404 not 500.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="pitcher not in engine",
        )

    scores = [float(r.score) for r in results]

    payload = {
        "query": {
            "pitcher_id": pitcher_id,
            "season": season,
            "p_throws": query_profile.p_throws,
            "full_name": names.get(pitcher_id, f"Pitcher #{pitcher_id}"),
        },
        "engine": "pitcher_similarity",
        "engine_version": "0.1.0-phase2",
        "population_size": len(results),
        "score_summary": compute_score_summary(scores),
        "bins": build_bins(results, names, n_bins=bins),
        "top_n": build_top_n(results, names, n=top_n),
        "diagnostic": classify_diagnostic(scores, n_bins=bins),
    }

    # ---- Cache write-through ----
    if cache is not None:
        try:
            await cache.set_json(cache_key, payload, ttl_s=CACHE_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            # A cache write failure should never break the response.
            log.warning("Cache write failed for %s: %s", cache_key, exc)

    return SimilarityDistributionResponse(**payload)
