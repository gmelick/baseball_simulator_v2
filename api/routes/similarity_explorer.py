"""
api/routes/similarity_explorer.py
=================================
SIM-439 — generalized Similarity Explorer across all 11 engines.

The original ``api/routes/similarity.py`` serves only the pitcher engine (with
``arsenal``/``command`` hard-coded). This module generalizes the same idea to
every engine using each one's REAL component-score fields, WITHOUT touching the
existing pitcher route (both routers mount under ``/api/similarity``):

  GET  /api/similarity/engines                     — catalog + build status
  GET  /api/similarity/{engine}/meta               — sub-score fields/labels/weights
  GET  /api/similarity/{engine}/query              — ranked comps + histogram bins
  GET  /api/similarity/{engine}/pair               — one subject-vs-comp breakdown
  POST /api/similarity/situation/query             — nearest historical game states

Two honesty rules baked into the surface (per the analyst + designer specs):
  1. The **8 score engines** (pitcher, batter, fielder, catcher, baserunner,
     baserunner_steal, pitcher_steal, manager) return a 0–1 composite with
     component sub-scores. The **3 distance engines** (situation, pitch_pitch,
     batted_ball) return an unbounded *distance* with NO sub-scores and are not
     player-keyed — the score endpoints 404 them and steer callers to the
     Situation Finder / Data Lab.
  2. The composite is NOT Σ(weight·sub_score): every score engine multiplies by a
     ``sqrt(min eb_alpha)`` confidence discount (and batter adds a bats penalty).
     We ship the weights + each comp's sample size so the UI can surface the gap
     rather than pretend the bar-math reconstructs the headline.

Reuses the numpy-free pure helpers from ``api.routes.similarity``
(``compute_score_summary``, ``classify_diagnostic``, ``_BinSpec``) so the
histogram/diagnostic logic stays in one place.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from api.routes.similarity import (
    _BinSpec,
    classify_diagnostic,
    compute_score_summary,
    get_name_resolver,
)

log = logging.getLogger("api.routes.similarity_explorer")

router = APIRouter(prefix="/api/similarity", tags=["similarity"])


# ---------------------------------------------------------------------------
# Engine adapters — the per-engine truth from the real result dataclasses.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineAdapter:
    name: str
    method: str
    partition: str
    id_field: str  # result attribute holding the matched entity's id
    sample_field: str
    min_sample: int
    #: (result_field, display_label, weight) for each component sub-score.
    subscores: tuple[tuple[str, str, float], ...]
    supports_vs_hand: bool = False
    position_keyed: bool = False
    #: (result_field, label) extra context fields (p_throws/bats/position/...).
    extra_fields: tuple[tuple[str, str], ...] = ()
    note: str = ""


SCORE_ADAPTERS: dict[str, EngineAdapter] = {
    "pitcher": EngineAdapter(
        name="pitcher",
        method="GMM-Wasserstein arsenal + RBF command",
        partition="Within throwing hand — L vs R are never compared",
        id_field="pitcher_id",
        sample_field="sample_pitches",
        min_sample=200,
        subscores=(("arsenal_score", "Arsenal", 0.65), ("command_score", "Command", 0.35)),
        extra_fields=(("p_throws", "Throws"),),
        note="arsenal_score is 0.0 when a pitcher's GMM is missing (match collapses to command-only).",
    ),
    "batter": EngineAdapter(
        name="batter",
        method="Reliability-weighted RBF (4 dimensions)",
        partition="All batters — a bats-mismatch penalty is applied L vs R",
        id_field="batter_id",
        sample_field="sample_pa",
        min_sample=100,
        subscores=(
            ("discipline_score", "Discipline", 0.40),
            ("batted_ball_score", "Batted Ball", 0.35),
            ("platoon_score", "Platoon", 0.15),
            ("power_score", "Power", 0.10),
        ),
        supports_vs_hand=True,
        extra_fields=(("bats", "Bats"),),
        note="With vs_hand the weights shift to platoon-dominant (0.30/0.25/0.35/0.10).",
    ),
    "fielder": EngineAdapter(
        name="fielder",
        method="Reliability-weighted RBF (position-partitioned)",
        partition="Position-partitioned — only same-position seasons compared",
        id_field="player_id",
        sample_field="sample_batted_balls",
        min_sample=50,
        subscores=(
            ("range_score", "Range", 0.45),
            ("secondary_score", "Secondary", 0.30),
            ("tertiary_score", "Tertiary", 0.15),
            ("quaternary_score", "Quaternary", 0.10),
        ),
        position_keyed=True,
        extra_fields=(("position", "Pos"),),
        note="Secondary/tertiary/quaternary labels depend on position (IF vs OF).",
    ),
    "catcher": EngineAdapter(
        name="catcher",
        method="Reliability-weighted RBF (framing-dominant)",
        partition="All catchers",
        id_field="catcher_id",
        sample_field="sample_pitches_received",
        min_sample=300,
        subscores=(
            ("framing_score", "Framing", 0.53),
            ("blocking_score", "Blocking", 0.24),
            ("throwing_score", "Throwing", 0.14),
            ("deterrence_score", "Deterrence", 0.09),
        ),
        note="deterrence is a single feature and falls back to league-average for sparse seasons.",
    ),
    "baserunner": EngineAdapter(
        name="baserunner",
        method="Reliability-weighted RBF (extra-base advancement)",
        partition="All runners",
        id_field="player_id",
        sample_field="sample_advancement_opps",
        min_sample=20,
        subscores=(
            ("speed_score", "Speed", 0.35),
            ("aggression_score", "Aggression", 0.40),
            ("success_score", "Success", 0.25),
        ),
        note="speed_score is a single feature (sprint speed); aggression carries the most weight.",
    ),
    "baserunner_steal": EngineAdapter(
        name="baserunner_steal",
        method="Reliability-weighted RBF (stolen bases)",
        partition="All runners with steal attempts",
        id_field="player_id",
        sample_field="sample_steal_attempts",
        min_sample=10,
        subscores=(
            ("tendency_score", "Tendency", 0.62),
            ("success_score", "Success", 0.38),
        ),
        note="Two sub-scores only (the jump dimension was removed). Warn under ~30 attempts.",
    ),
    "pitcher_steal": EngineAdapter(
        name="pitcher_steal",
        method="Reliability-weighted RBF (runner suppression)",
        partition="All pitchers",
        id_field="pitcher_id",
        sample_field="sample_baserunner_events",
        min_sample=30,
        subscores=(("outcome_score", "Outcome", 1.00),),
        extra_fields=(("throws", "Throws"),),
        note="A single outcome dimension (SB/9, forced-CS, attempt rate rolled into one).",
    ),
    "manager": EngineAdapter(
        name="manager",
        method="Reliability-weighted RBF (tactics)",
        partition="All managers",
        id_field="manager_id",
        sample_field="sample_games",
        min_sample=50,
        subscores=(
            ("usage_score", "Bullpen Usage", 0.40),
            ("aggression_score", "Aggression", 0.35),
            ("platoon_score", "Platoon", 0.25),
        ),
        note="Manager names may show as an id (resolved from raw.players only).",
    ),
}

DISTANCE_ENGINES: dict[str, str] = {
    "situation": "Game-state matcher (KDTree) — use the Situation Finder, not a player search.",
    "pitch_pitch": "Individual-pitch matcher — a sim primitive, keyed by a pitch vector, not a player.",
    "batted_ball": "Batted-ball outcome model — keyed by exit-velo/launch-angle/spray, not a player.",
}

# Fielder position-dependent labels + weights (the query() code-path mapping).
_OF_POSITIONS = {"LF", "CF", "RF"}
_FIELDER_IF = (("Range", 0.45), ("Double Play", 0.30), ("Errors", 0.15), ("Specialty", 0.10))
_FIELDER_OF = (("Range", 0.40), ("Arm", 0.30), ("Star Plays", 0.15), ("Errors", 0.15))
_FIELDER_FIELDS = ("range_score", "secondary_score", "tertiary_score", "quaternary_score")
_BATTER_VSHAND_WEIGHTS = (0.30, 0.25, 0.35, 0.10)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class SubScoreMeta(BaseModel):
    field: str
    label: str
    weight: float | None = None


class EngineCatalogEntry(BaseModel):
    engine: str
    kind: str  # "score" | "distance"
    method: str
    partition: str | None = None
    sub_score_count: int = 0
    built: bool = False
    profile_count: int = 0
    note: str = ""


class EngineMetaResponse(BaseModel):
    engine: str
    method: str
    partition: str
    sub_scores: list[SubScoreMeta]
    sample_field: str
    min_sample: int
    supports_vs_hand: bool
    position_keyed: bool
    built: bool
    profile_count: int
    note: str = ""


class SimMember(BaseModel):
    entity_id: int
    season: int
    name: str
    score: float
    sub_scores: dict[str, float]
    sample: int
    below_min_sample: bool
    extra: dict[str, Any] = {}


class SimBin(BaseModel):
    bin_index: int
    lo: float
    hi: float
    count: int
    preview: list[SimMember]
    members: list[SimMember]


class SimQueryResponse(BaseModel):
    engine: str
    method: str
    subject: dict[str, Any]
    sub_scores_meta: list[SubScoreMeta]
    sample_field: str
    min_sample: int
    partition: str
    population_size: int
    score_summary: dict[str, float]
    diagnostic: dict[str, Any]
    bins: list[SimBin]
    top_n: list[SimMember]
    note: str = ""


class SimPairResponse(BaseModel):
    engine: str
    subject: dict[str, Any]
    comp: SimMember
    sub_scores_meta: list[SubScoreMeta]
    note: str = ""


class NearestSituationModel(BaseModel):
    play_id: str
    game_pk: int
    distance: float
    inning: int
    outs: int
    runners: int
    leverage_index: float
    score_differential: float


class SituationQueryRequest(BaseModel):
    inning: int = 1
    top_or_bottom: int = 0
    outs: int = 0
    runner_on_1b: int = 0
    runner_on_2b: int = 0
    runner_on_3b: int = 0
    score_differential: float = 0.0
    leverage_index: float = 1.0
    pitcher_pitch_count: int = 0
    batter_pa_count: int = 1
    park_factor_runs: float = 1.0
    k: int = 25


class SituationQueryResponse(BaseModel):
    k: int
    count: int
    results: list[NearestSituationModel]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _engines(request: Request) -> dict[str, Any]:
    return getattr(request.app.state, "engines", {}) or {}


def _require_score_engine(request: Request, engine_name: str) -> tuple[EngineAdapter, Any]:
    if engine_name in DISTANCE_ENGINES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"'{engine_name}' is a pattern-finder (distance) engine, not a player-comp "
                f"engine. {DISTANCE_ENGINES[engine_name]}"
            ),
        )
    adapter = SCORE_ADAPTERS.get(engine_name)
    if adapter is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown engine '{engine_name}'"
        )
    engine = _engines(request).get(engine_name)
    if engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"engine '{engine_name}' is warming / not built",
        )
    return adapter, engine


def _subscores_meta(
    adapter: EngineAdapter, position: str | None, vs_hand: str | None
) -> list[SubScoreMeta]:
    if adapter.name == "fielder":
        table = _FIELDER_OF if (position or "").upper() in _OF_POSITIONS else _FIELDER_IF
        return [
            SubScoreMeta(field=f, label=lbl, weight=w)
            for f, (lbl, w) in zip(_FIELDER_FIELDS, table, strict=True)
        ]
    if adapter.name == "batter" and vs_hand:
        return [
            SubScoreMeta(field=f, label=lbl, weight=w)
            for (f, lbl, _), w in zip(adapter.subscores, _BATTER_VSHAND_WEIGHTS, strict=True)
        ]
    return [SubScoreMeta(field=f, label=lbl, weight=w) for f, lbl, w in adapter.subscores]


def _call_query(
    adapter: EngineAdapter,
    engine: Any,
    entity_id: int,
    season: int,
    position: str | None,
    vs_hand: str | None,
    n: int | None,
):
    if adapter.name == "fielder":
        return engine.query(entity_id, position, season, n)
    if adapter.name == "batter":
        return engine.query(entity_id, season, n, vs_hand)
    return engine.query(entity_id, season, n)


def _call_get_profile(
    adapter: EngineAdapter, engine: Any, entity_id: int, position: str | None, season: int
):
    if adapter.name == "fielder":
        return engine.get_profile(entity_id, position, season)
    return engine.get_profile(entity_id, season)


def _call_pair(
    adapter: EngineAdapter,
    engine: Any,
    a: tuple[int, int],
    b: tuple[int, int],
    position: str | None,
    vs_hand: str | None,
):
    if adapter.name == "fielder":
        return engine.query_pair((a[0], position, a[1]), (b[0], position, b[1]))
    if adapter.name == "batter":
        return engine.query_pair((a[0], a[1]), (b[0], b[1]), vs_hand)
    return engine.query_pair((a[0], a[1]), (b[0], b[1]))


def _member(
    adapter: EngineAdapter, result: Any, names: dict[int, str], fields: list[str]
) -> dict[str, Any]:
    eid = int(getattr(result, adapter.id_field))
    sample = int(getattr(result, adapter.sample_field))
    return {
        "entity_id": eid,
        "season": int(result.season),
        "name": names.get(eid, f"#{eid}"),
        "score": float(result.score),
        "sub_scores": {f: float(getattr(result, f)) for f in fields},
        "sample": sample,
        "below_min_sample": sample < adapter.min_sample,
        "extra": {lbl: getattr(result, f, None) for f, lbl in adapter.extra_fields},
    }


def _build_bins(adapter, results, names, fields, n_bins, preview_size=5) -> list[dict[str, Any]]:
    spec = _BinSpec.linear(n_bins)
    buckets: list[list[Any]] = [[] for _ in range(n_bins)]
    for r in results:
        buckets[spec.index_for(float(r.score))].append(r)
    out: list[dict[str, Any]] = []
    for i, bucket in enumerate(buckets):
        bucket.sort(key=lambda r: float(r.score), reverse=True)
        members = [_member(adapter, r, names, fields) for r in bucket]
        lo, hi = spec.edges(i)
        out.append(
            {
                "bin_index": i,
                "lo": lo,
                "hi": hi,
                "count": len(members),
                "preview": members[:preview_size],
                "members": members,
            }
        )
    return out


async def _resolve_names(request: Request, ids: Iterable[int]) -> dict[int, str]:
    resolver = get_name_resolver(request)
    return await resolver({int(i) for i in ids})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/engines", response_model=list[EngineCatalogEntry], summary="Engine catalog + build status"
)
async def list_engines(request: Request) -> list[EngineCatalogEntry]:
    built = _engines(request)
    out: list[EngineCatalogEntry] = []
    for name, adapter in SCORE_ADAPTERS.items():
        eng = built.get(name)
        count = 0
        if eng is not None:
            try:
                count = int(eng.profile_count)
            except Exception:  # noqa: BLE001
                count = 0
        out.append(
            EngineCatalogEntry(
                engine=name,
                kind="score",
                method=adapter.method,
                partition=adapter.partition,
                sub_score_count=len(adapter.subscores),
                built=eng is not None and count > 0,
                profile_count=count,
                note=adapter.note,
            )
        )
    for name, desc in DISTANCE_ENGINES.items():
        eng = built.get(name)
        out.append(
            EngineCatalogEntry(
                engine=name,
                kind="distance",
                method=desc,
                built=eng is not None,
                note="Distance engine — no player comps / sub-scores.",
            )
        )
    return out


@router.get(
    "/{engine}/meta", response_model=EngineMetaResponse, summary="Engine sub-score metadata"
)
async def engine_meta(
    request: Request,
    engine: str,
    position: str | None = Query(
        None, description="Fielder only — resolves IF/OF sub-score labels."
    ),
    vs_hand: str | None = Query(None, pattern="^[LR]$"),
) -> EngineMetaResponse:
    adapter, eng = _require_score_engine(request, engine)
    try:
        count = int(eng.profile_count)
    except Exception:  # noqa: BLE001
        count = 0
    return EngineMetaResponse(
        engine=engine,
        method=adapter.method,
        partition=adapter.partition,
        sub_scores=_subscores_meta(adapter, position, vs_hand),
        sample_field=adapter.sample_field,
        min_sample=adapter.min_sample,
        supports_vs_hand=adapter.supports_vs_hand,
        position_keyed=adapter.position_keyed,
        built=count > 0,
        profile_count=count,
        note=adapter.note,
    )


@router.get(
    "/{engine}/query", response_model=SimQueryResponse, summary="Ranked comps + score distribution"
)
async def engine_query(
    request: Request,
    engine: str,
    entity_id: int = Query(..., description="Player / manager id."),
    season: int = Query(..., ge=2015, le=2027),
    position: str | None = Query(
        None, description="Required for the fielder engine (2B/SS/CF/...)."
    ),
    vs_hand: str | None = Query(
        None, pattern="^[LR]$", description="Batter engine — platoon-weighted mode."
    ),
    bins: int = Query(20, ge=4, le=100),
    top_n: int = Query(25, ge=1, le=200),
) -> SimQueryResponse:
    adapter, eng = _require_score_engine(request, engine)
    if adapter.position_keyed and not position:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="the fielder engine requires a 'position'",
        )

    results = await asyncio.to_thread(
        _call_query, adapter, eng, entity_id, season, position, vs_hand, None
    )
    if not results:
        # Distinguish "subject not in engine" (404) from "no comparable pop" (empty 200).
        profile = _call_get_profile(adapter, eng, entity_id, position, season)
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"{engine} subject {entity_id} ({season}) not found — below the minimum sample or not ingested",
            )

    meta = _subscores_meta(adapter, position, vs_hand)
    fields = [m.field for m in meta]

    ids = {int(getattr(r, adapter.id_field)) for r in results}
    ids.add(entity_id)
    names = await _resolve_names(request, ids)

    scores = [float(r.score) for r in results]
    bins_payload = _build_bins(adapter, results, names, fields, bins)
    top_payload = [_member(adapter, r, names, fields) for r in results[:top_n]]

    subject: dict[str, Any] = {
        "entity_id": entity_id,
        "season": season,
        "name": names.get(entity_id, f"#{entity_id}"),
    }
    if position:
        subject["position"] = position
    if vs_hand:
        subject["vs_hand"] = vs_hand
    profile = _call_get_profile(adapter, eng, entity_id, position, season)
    if profile is not None:
        for attr, lbl in (("p_throws", "Throws"), ("bats", "Bats"), ("throws", "Throws")):
            val = getattr(profile, attr, None)
            if val is not None:
                subject[lbl.lower()] = val

    return SimQueryResponse(
        engine=engine,
        method=adapter.method,
        subject=subject,
        sub_scores_meta=meta,
        sample_field=adapter.sample_field,
        min_sample=adapter.min_sample,
        partition=adapter.partition,
        population_size=len(results),
        score_summary=compute_score_summary(scores),
        diagnostic=classify_diagnostic(scores, n_bins=bins),
        bins=[SimBin(**b) for b in bins_payload],
        top_n=[SimMember(**m) for m in top_payload],
        note=adapter.note,
    )


@router.get(
    "/{engine}/pair", response_model=SimPairResponse, summary="One subject-vs-comp breakdown"
)
async def engine_pair(
    request: Request,
    engine: str,
    a_id: int = Query(...),
    a_season: int = Query(..., ge=2015, le=2027),
    b_id: int = Query(...),
    b_season: int = Query(..., ge=2015, le=2027),
    position: str | None = None,
    vs_hand: str | None = Query(None, pattern="^[LR]$"),
) -> SimPairResponse:
    adapter, eng = _require_score_engine(request, engine)
    if adapter.position_keyed and not position:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="the fielder engine requires a 'position'",
        )

    result = await asyncio.to_thread(
        _call_pair, adapter, eng, (a_id, a_season), (b_id, b_season), position, vs_hand
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="one or both entities not in the engine"
        )

    meta = _subscores_meta(adapter, position, vs_hand)
    fields = [m.field for m in meta]
    names = await _resolve_names(request, [a_id, b_id])
    comp = _member(adapter, result, names, fields)

    subject: dict[str, Any] = {
        "entity_id": a_id,
        "season": a_season,
        "name": names.get(a_id, f"#{a_id}"),
    }
    if position:
        subject["position"] = position

    return SimPairResponse(
        engine=engine,
        subject=subject,
        comp=SimMember(**comp),
        sub_scores_meta=meta,
        note=adapter.note,
    )


@router.post(
    "/situation/query",
    response_model=SituationQueryResponse,
    summary="Nearest historical game states",
)
async def situation_query(request: Request, body: SituationQueryRequest) -> SituationQueryResponse:
    engine = _engines(request).get("situation")
    if engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the situation engine is not built in this deployment (the full-pool sampler is production)",
        )
    # Lazy import so the module stays importable without the heavy engine deps.
    from similarity.engines.situation_similarity import SituationVector

    vector = SituationVector(
        inning=body.inning,
        top_or_bottom=body.top_or_bottom,
        outs=body.outs,
        runner_on_1b=body.runner_on_1b,
        runner_on_2b=body.runner_on_2b,
        runner_on_3b=body.runner_on_3b,
        score_differential=body.score_differential,
        leverage_index=body.leverage_index,
        pitcher_pitch_count=body.pitcher_pitch_count,
        batter_pa_count=body.batter_pa_count,
        park_factor_runs=body.park_factor_runs,
    )
    try:
        results = await asyncio.to_thread(engine.query, vector, body.k)
    except RuntimeError as exc:  # engine built but index empty / not ready
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    models = [
        NearestSituationModel(
            play_id=str(r.play_id),
            game_pk=int(r.game_pk),
            distance=float(r.distance),
            inning=int(r.inning),
            outs=int(r.outs),
            runners=int(r.runners),
            leverage_index=float(r.leverage_index),
            score_differential=float(r.score_differential),
        )
        for r in results
    ]
    return SituationQueryResponse(k=body.k, count=len(models), results=models)


__all__ = ["router", "SCORE_ADAPTERS", "DISTANCE_ENGINES", "EngineAdapter"]
