"""
api/routes/games_runs.py
========================
The reads of a game's simulation runs (the durable ``sim.sim_runs`` rows).

    GET    /api/games/{game_pk}/simulate/runs/latest     the newest run, any state
    GET    /api/games/{game_pk}/simulate/runs/{run_id}   one run's state, progress and summary
    DELETE /api/games/{game_pk}/simulate/runs/{run_id}   cancel a run
    GET    /api/games/{game_pk}/what-if/{base}/{change}  a what-if's two runs and the real final

The routes that start a run (POST /simulate and POST /what-if) live in
``api/routes/games.py`` with every other route that runs a simulation. The
``runs/latest`` route stays before ``runs/{run_id}``: the router matches routes
in order, and ``latest`` would otherwise read as a run id. The aggregate router
in ``api/routes/games.py`` includes this one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from api.auth import require_auth
from api.routes._common import _iso
from api.routes.games_feed import _league_game_payload
from api.routes.games_models import SimRunModel, WhatIfResult, _sim_summary_lite_from_stored
from simulation.prop_distributions import PropDistributionSet

router = APIRouter()


def _get_sim_jobs(request: Request) -> Any:
    jobs = getattr(request.app.state, "sim_jobs", None)
    if jobs is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the run registry is not attached (no database pool)",
        )
    return jobs


def _run_model(run: Mapping[str, Any], *, existing: bool = False) -> SimRunModel:
    summary = run.get("summary")
    return SimRunModel(
        run_id=int(run["run_id"]),
        game_pk=int(run["game_pk"]),
        status=run.get("status") or "done",
        n_iterations=int(run["n_iterations"]),
        progress_done=int(run.get("progress_done") or 0),
        base_seed=run.get("base_seed"),
        position_in_queue=run.get("position_in_queue"),
        existing=existing,
        requested_at=_iso(run.get("requested_at")),
        started_at=_iso(run.get("started_at")),
        finished_at=_iso(run.get("finished_at")),
        error=run.get("error"),
        lineup_source=run.get("lineup_source"),
        bullpen_source=run.get("bullpen_source"),
        replay_run_id=run.get("replay_run_id"),
        summary=_sim_summary_lite_from_stored(summary) if isinstance(summary, dict) else None,
    )


@router.get(
    "/{game_pk}/simulate/runs/latest",
    response_model=SimRunModel,
    summary="The game's newest run, any state (SIM-519 Part E)",
)
async def get_latest_simulation_run(game_pk: int, request: Request) -> SimRunModel:
    run = await _get_sim_jobs(request).latest(int(game_pk))
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": "no simulation run for this game", "hint": "POST /simulate"},
        )
    return _run_model(run)


@router.get(
    "/{game_pk}/simulate/runs/{run_id}",
    response_model=SimRunModel,
    summary="One run's state, progress and summary (SIM-519 Part E)",
)
async def get_simulation_run(game_pk: int, run_id: int, request: Request) -> SimRunModel:
    run = await _get_sim_jobs(request).get(int(game_pk), int(run_id))
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"run {run_id} not found")
    return _run_model(run)


@router.delete(
    "/{game_pk}/simulate/runs/{run_id}",
    response_model=SimRunModel,
    summary="Cancel a run (SIM-519 Part E)",
    dependencies=[Depends(require_auth)],
    description=(
        "A queued run is cancelled at once; a running run stops after its current chunk "
        "of games (it keeps the games it played). Idempotent. 409 when the run already "
        "finished; 404 when unknown."
    ),
)
async def cancel_simulation_run(game_pk: int, run_id: int, request: Request) -> SimRunModel:
    run = await _get_sim_jobs(request).cancel(int(game_pk), int(run_id))
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"run {run_id} not found")
    if run["status"] in ("done", "failed"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"run {run_id} is {run['status']}"
        )
    return _run_model(run)


async def _stored_prop_set(
    request: Request, game_pk: int, run_id: int | None
) -> tuple[PropDistributionSet, dict[str, Any]]:
    """A run's prop set (``run_id``, else the newest done run); 404 with a hint when none."""
    jobs = _get_sim_jobs(request)
    row = await jobs.prop_set_row(int(game_pk), run_id)
    if row is None or not row.get("prop_set"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": "no simulation run for this game", "hint": "POST /simulate"},
        )
    return PropDistributionSet.from_json(row["prop_set"]), row


@router.get(
    "/{game_pk}/what-if/{base_run_id}/{change_run_id}",
    response_model=WhatIfResult,
    summary="A what-if's two runs and the real final",
)
async def get_what_if(
    game_pk: int, base_run_id: int, change_run_id: int, request: Request
) -> WhatIfResult:
    jobs = _get_sim_jobs(request)
    base = await jobs.get(int(game_pk), int(base_run_id))
    change = await jobs.get(int(game_pk), int(change_run_id))
    if base is None or change is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="what-if run not found")
    real_final = None
    start_score = None
    payload, _source, _error = await _league_game_payload(request, game_pk)
    if payload is not None:
        from pipeline.mlb_game_feed import parse_game_feed

        card = parse_game_feed(payload)
        ls = card.get("linescore") or {}
        if card.get("status") == "final" and ls:
            real_final = {
                "away": int(ls["away"]["runs"] or 0),
                "home": int(ls["home"]["runs"] or 0),
            }
        at_bat = base.get("start_at_bat")
        if at_bat is not None or base.get("kind") == "whatif_base":
            from pipeline.mlb_game_feed import PlateAppearanceNotFound, state_at_pa

            try:
                ss = state_at_pa(payload, at_bat)
                start_score = {"away": int(ss["away_score"]), "home": int(ss["home_score"])}
            except PlateAppearanceNotFound:
                start_score = None
    return WhatIfResult(
        game_pk=int(game_pk),
        base=_run_model(base),
        change=_run_model(change),
        real_final=real_final,
        start_score=start_score,
    )
