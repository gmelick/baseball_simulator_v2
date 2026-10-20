"""
api/routes/games_feed.py
========================
The real game: the league's per-game feed and the live service's stored state.

    GET /api/games/{game_pk}/feed         the slate card's detail
    GET /api/games/{game_pk}/feed/plays   the real play-by-play
    GET /api/games/{game_pk}/feed/state   the game at the start of a plate appearance
    GET /api/games/{game_pk}/live         the live service's stored game state

One cached copy of the league feed (:func:`_league_game_payload`) serves the
first three routes, the what-if's start state (:func:`_start_state`) and the
betting card's grading. The aggregate router in ``api/routes/games.py``
includes this one. Its routes carry no prefix, tag or dependency of their own.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from api.routes._common import _get_pool, _get_sim_cache
from api.routes.games_models import (
    FeedLive,
    FeedPerson,
    FeedPitcher,
    FeedRunners,
    GameFeedCardModel,
    LineupSpot,
    PaStateModel,
    PlayerOption,
    RealPlay,
    RealPlaysModel,
    SideState,
)
from api.schemas import LiveGameStateResponse
from db import sim_store

log = logging.getLogger("api.routes.games")

router = APIRouter()


#: The feed cache's time to live by the game's state, in seconds.
FEED_TTL_BY_STATE_S = {"live": 10, "scheduled": 600, "final": 24 * 3600, "postponed": 24 * 3600}
#: The last good feed of a game, served when the league read fails.
FEED_LAST_GOOD_TTL_S = 24 * 3600


async def _league_game_payload(
    request: Request, game_pk: int, *, use_cache: bool = True
) -> tuple[Any, str, str | None]:
    """The league's raw feed of one game, cached; ``(payload, source, error)``.

    One cached copy serves the slate card (``/feed``), the real play-by-play
    (``/feed/plays``), the what-if state (``/feed/state``), the what-if result
    and the betting card's grading. The TTL follows
    the game's state (10 s live, 10 min before the game, 24 h final). On a league
    error the last good copy is served (``source = feed_cached``); with none the
    payload is None.
    """
    from pipeline.mlb_game_feed import feed_state

    feed = getattr(request.app.state, "league_feed", None)
    cache = _get_sim_cache(request)
    key, last_key = f"feed:raw:v1:{game_pk}", f"feed:rawlast:v1:{game_pk}"
    if use_cache and cache is not None:
        try:
            hit = cache.get(key)
        except Exception:  # noqa: BLE001 -- a cache hiccup must not break the read
            hit = None
        if hit is not None:
            return hit, "feed", None
    error: str | None = None
    if feed is None:
        error = "no league feed attached"
    else:
        try:
            payload = await feed.game_feed_payload(int(game_pk))
        except Exception as exc:  # noqa: BLE001 -- degrade to the last good copy
            error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("feed: the league read failed for %s: %s", game_pk, error)
        else:
            if cache is not None:
                try:
                    state = feed_state((payload.get("gameData") or {}).get("status") or {})
                    cache.set(key, payload, FEED_TTL_BY_STATE_S.get(state, 60))
                    cache.set(last_key, payload, FEED_LAST_GOOD_TTL_S)
                except Exception as exc:  # noqa: BLE001
                    log.warning("feed cache write failed for %s: %s", key, exc)
            return payload, "feed", None
    last = None
    if cache is not None:
        try:
            last = cache.get(last_key)
        except Exception:  # noqa: BLE001
            last = None
    if last is not None:
        return last, "feed_cached", error
    return None, "none", error


def _feed_unavailable(game_pk: int, error: str | None) -> Any:
    """The 503 a feed route answers when no copy of the league feed is at hand."""
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=503,
        content={
            "detail": f"the league feed for game {game_pk} is unavailable",
            "feed_error": error,
        },
        headers={"Retry-After": "30"},
    )


@router.get(
    "/{game_pk}/feed",
    response_model=GameFeedCardModel,
    summary="The real game behind a slate card (SIM-519 Part I)",
    description=(
        "The league's live feed for one game, reduced to what the Daily Diamond card "
        "shows: the linescore, both box scores, the posted lineups and probable "
        "pitchers, and for a live game the count, runners, fielders, the pitcher's "
        "pitch count and the last play. Cached 10 s while live, 10 min before the "
        "game and 24 h once final. On a league error the last good copy is served "
        "(`source = feed_cached`); with none, 503 with Retry-After."
    ),
)
async def get_game_feed(game_pk: int, request: Request, use_cache: bool = Query(True)) -> Any:
    from pipeline.mlb_game_feed import parse_game_feed

    payload, source, error = await _league_game_payload(request, game_pk, use_cache=use_cache)
    if payload is None:
        return _feed_unavailable(game_pk, error)
    try:
        card = GameFeedCardModel(**parse_game_feed(payload))
    except Exception as exc:  # noqa: BLE001 -- a malformed feed is a league error
        log.warning("feed: the league feed for %s did not parse: %s", game_pk, exc)
        return _feed_unavailable(game_pk, f"{type(exc).__name__}: {exc}"[:300])
    if source == "feed_cached":
        card.source = "feed_cached"
        card.feed_error = error
    return card


@router.get(
    "/{game_pk}/feed/plays",
    response_model=RealPlaysModel,
    summary="The real game's play-by-play",
    description=(
        "Every plate appearance of the real game from the league feed (the same cached "
        "copy as /feed): inning, half, batter, pitcher, the event and its description, "
        "outs and score before and after, the runners at its start, and its pitches. "
        "503 with Retry-After when the feed is unavailable."
    ),
)
async def get_real_plays(game_pk: int, request: Request) -> Any:
    from pipeline.mlb_game_feed import feed_state, parse_plays

    payload, source, error = await _league_game_payload(request, game_pk)
    if payload is None:
        return _feed_unavailable(game_pk, error)
    parsed = parse_plays(payload)
    return RealPlaysModel(
        game_pk=int(game_pk),
        status=feed_state((payload.get("gameData") or {}).get("status") or {}),  # type: ignore[arg-type]
        plays=[RealPlay(**p) for p in parsed["plays"]],
        names={str(k): v for k, v in parsed["names"].items()},
        source="feed_cached" if source == "feed_cached" else "feed",
    )


def _option(ss: Mapping[str, Any], pid: int, position: str | None = None) -> PlayerOption:
    """One player of a start state as a PlayerOption (name, hands, position)."""
    return PlayerOption(
        id=int(pid),
        name=str((ss.get("names") or {}).get(str(pid), f"Player {pid}")),
        position=position,
        bats=(ss.get("bat_hands") or {}).get(str(pid)),
        throws=(ss.get("throw_hands") or {}).get(str(pid)),
    )


def _pa_state_model(game_pk: int, ss: Mapping[str, Any]) -> PaStateModel:
    """A start state (``pipeline.mlb_game_feed.state_at_pa``) as the /feed/state model.

    ``live`` has the shape of the slate card's field, at a 0-0 count with the
    batter due up.
    """
    sides: dict[str, SideState] = {}
    for side in ("away", "home"):
        defense = ss[f"{side}_defense"]
        pos_of = {int(v): k for k, v in defense.items()}
        sides[side] = SideState(
            lineup=[
                LineupSpot(
                    slot=i,
                    id=int(pid),
                    name=_option(ss, pid).name,
                    position=pos_of.get(int(pid), "DH"),
                )
                for i, pid in enumerate(ss[f"{side}_lineup"])
            ],
            pitcher=_option(ss, ss[f"{side}_pitcher"], "P"),
            defense={pos: _option(ss, pid, pos) for pos, pid in defense.items()},
            bench=[_option(ss, p) for p in ss["eligible"][side]["hitters"]],
            bullpen=[_option(ss, p, "P") for p in ss["eligible"][side]["pitchers"]],
            used=[_option(ss, p) for p in ss["used"][side]],
        )
    batting = ss["batting_side"]
    fielding = "home" if batting == "away" else "away"
    due = ss[f"{batting}_lineup"][ss[f"{batting}_slot"]]
    pitcher = int(ss[f"{fielding}_pitcher"])

    def person(pid: Any) -> FeedPerson | None:
        return None if pid is None else FeedPerson(id=int(pid), name=_option(ss, pid).name)

    live = FeedLive(
        balls=0,
        strikes=0,
        outs=int(ss["outs"]),
        offense=batting,  # type: ignore[arg-type]
        runners=FeedRunners(
            **{b: person(ss["runners"].get(b)) for b in ("first", "second", "third")}
        ),
        batter=person(due),
        pitcher=FeedPitcher(
            id=pitcher,
            name=_option(ss, pitcher).name,
            np=int((ss.get("pitch_counts") or {}).get(str(pitcher), 0)),
        ),
        fielders={pos: _option(ss, pid).name for pos, pid in ss[f"{fielding}_defense"].items()},
        last_play=None,
    )
    return PaStateModel(
        game_pk=int(game_pk),
        at_bat=ss.get("at_bat"),
        inning=int(ss["inning"]),
        half=ss["half"],
        outs=int(ss["outs"]),
        away_score=int(ss["away_score"]),
        home_score=int(ss["home_score"]),
        batting_side=batting,
        live=live,
        away=sides["away"],
        home=sides["home"],
    )


def _feed_lineup_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Both starting lineups from a league feed, as ``raw.game_lineups`` rows.

    The loader's own builder reads the box: each starter at his starting
    position and the starting pitcher. A game whose box lists no starter yet (a
    preview game without posted lineups) gives no rows.
    """
    from pipeline.etl.etl_historical_loader import _build_starting_lineup_rows

    game_pk = int(((payload.get("gameData") or {}).get("game") or {}).get("pk") or 0)
    season = int(((payload.get("gameData") or {}).get("game") or {}).get("season") or 0)
    return [
        {
            "team_id": r[2],
            "player_id": r[3],
            "batting_order": r[4],
            "position_code": r[5],
            "is_starter": r[6],
            "sequence": r[7],
            "entered_inning": None,
            "entered_at_bat": None,
            "pinch_role": None,
        }
        for r in _build_starting_lineup_rows(game_pk, season, dict(payload))
    ]


async def _start_state(request: Request, game_pk: int, at_bat: int | None) -> dict[str, Any]:
    """The start state at a plate appearance, or an HTTP error."""
    from pipeline.mlb_game_feed import PlateAppearanceNotFound, state_at_pa

    payload, _source, error = await _league_game_payload(request, game_pk)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"the league feed for game {game_pk} is unavailable: {error}",
            headers={"Retry-After": "30"},
        )
    try:
        return state_at_pa(payload, at_bat)
    except PlateAppearanceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get(
    "/{game_pk}/feed/state",
    response_model=PaStateModel,
    summary="The real game at the start of a plate appearance (the what-if)",
    description=(
        "The game as it stood at the start of plate appearance `at_bat` (the feed's "
        "atBatIndex; omitted = first pitch): inning, half, outs, score, runners, both "
        "lineups with positions, each side's pitcher and defense, and the bench and "
        "bullpen still available. 404 for an unknown plate appearance."
    ),
)
async def get_pa_state(
    game_pk: int, request: Request, at_bat: int | None = Query(None, ge=0)
) -> PaStateModel:
    return _pa_state_model(game_pk, await _start_state(request, game_pk, at_bat))


@router.get(
    "/{game_pk}/live",
    response_model=LiveGameStateResponse,
    summary="Live in-progress game state (SIM-386)",
    description=(
        "Read the live game state from ``sim.lineup_state`` for an in-progress game.  "
        "The live ingestion pipeline (port :8001) writes the game state JSONB on every "
        "MLB WebSocket signal; this endpoint surfaces it on the main API so the frontend "
        "does not need to reach the pipeline app directly.  Fields mirror the "
        "``game_state`` JSONB blob: inning/half/outs/score/baserunners/lineup/roster.  "
        "``updated_at`` (ISO-8601) marks when the pipeline last wrote; the frontend "
        "may treat a gap > 30s as a potential pipeline staleness signal.  "
        "Returns 404 when no live row exists for ``game_pk`` (game not yet live, "
        "already finished, or pipeline has not ingested it yet). "
        "Returns 503 when no DB pool is attached."
    ),
)
async def get_live_game_state(
    game_pk: int,
    request: Request,
) -> LiveGameStateResponse:
    pool = _get_pool(request)
    acquire = getattr(pool, "acquire", None)
    if acquire is not None:
        async with pool.acquire() as conn:
            live = await sim_store.load_live_game_state(conn, int(game_pk))
    else:
        live = await sim_store.load_live_game_state(pool, int(game_pk))

    if live is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No live game state found for game_pk {game_pk}. "
                "The game may not be in-progress or the pipeline has not "
                "ingested it yet."
            ),
        )

    gs: dict[str, Any] = live.get("game_state") or {}

    def _i(key: str, default: int = 0) -> int:
        v = gs.get(key, default)
        return default if v is None else int(v)

    def _opt_i(key: str) -> int | None:
        v = gs.get(key)
        return None if v is None else int(v)

    def _ids(key: str) -> list[int]:
        # The live service stores each roster entry as an object with a
        # ``player_id`` (GameStateBuilder._parse_roster); an older row holds bare ids.
        v = gs.get(key)
        if not v:
            return []
        ids = (x.get("player_id") if isinstance(x, Mapping) else x for x in v)
        return [int(x) for x in ids if x is not None]

    return LiveGameStateResponse(
        game_pk=int(live["game_pk"]),
        session_id=str(live["session_id"]),
        inning=_i("inning", 1),
        half=str(gs.get("half", "Top")),
        outs=_i("outs"),
        balls=_i("balls"),
        strikes=_i("strikes"),
        home_score=_i("home_score"),
        away_score=_i("away_score"),
        batting_team_id=_opt_i("batting_team_id"),
        fielding_team_id=_opt_i("fielding_team_id"),
        on_1b=_opt_i("on_1b"),
        on_2b=_opt_i("on_2b"),
        on_3b=_opt_i("on_3b"),
        current_batter_id=_opt_i("current_batter_id"),
        current_pitcher_id=_opt_i("current_pitcher_id"),
        home_lineup=_ids("home_lineup"),
        away_lineup=_ids("away_lineup"),
        home_bullpen=_ids("home_bullpen"),
        away_bullpen=_ids("away_bullpen"),
        home_bench=_ids("home_bench"),
        away_bench=_ids("away_bench"),
        updated_at=live.get("updated_at"),
    )
