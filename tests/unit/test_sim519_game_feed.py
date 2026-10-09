"""SIM-519 Part I — the card detail from the league's per-game feed.

The fixtures in ``tests/fixtures/mlb_game_feed/`` are real feeds, trimmed by
``record_fixtures.py`` to the subtrees the parser reads (recorded 2026-10-09).
A live game is not on the calendar today, so the live tests flip a recorded
final feed's status to Live; its linescore still carries the last offense and
defense, which is the live shape.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.games as games_mod
from api.routes.games import router as games_router
from pipeline.mlb_game_feed import parse_game_feed
from simulation.batch_runner import InMemoryCache

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "mlb_game_feed"


def _load(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def _as_live(name: str) -> dict:
    feed = copy.deepcopy(_load(name))
    feed["gameData"]["status"].update(
        abstractGameState="Live", codedGameState="I", detailedState="In Progress"
    )
    return feed


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


def test_final_extra_innings_linescore_and_box() -> None:
    card = parse_game_feed(_load("final_extra_2024-04-13"))
    assert card["status"] == "final"
    ls = card["linescore"]
    assert len(ls["innings"]) == 12
    assert ls["innings"][0]["num"] == 1
    assert sum(i["away"] or 0 for i in ls["innings"]) == ls["away"]["runs"]
    assert sum(i["home"] or 0 for i in ls["innings"]) == ls["home"]["runs"]
    assert ls["current_inning"] is None
    box = card["box"]
    for side in ("away", "home"):
        bats = box[side]["batters"]
        assert len([b for b in bats if not b["is_sub"]]) == 9
        orders = [b["batting_order"] for b in bats]
        assert orders == sorted(orders)
        assert all(b["name"] for b in bats)
        assert box[side]["pitchers"], "every side used a pitcher"
        assert all(p["np"] is not None and p["np"] > 0 for p in box[side]["pitchers"])
    # The box's hits equal the linescore's.
    assert sum(b["h"] for b in box["away"]["batters"]) == ls["away"]["hits"]
    assert card["live"] is None


def test_home_win_marks_the_unplayed_half() -> None:
    card = parse_game_feed(_load("final_home_win_2024-08-15"))
    ls = card["linescore"]
    assert ls["home_did_not_bat_last"] is True
    assert ls["innings"][-1]["home"] is None
    assert ls["home"]["runs"] > ls["away"]["runs"]


def test_extra_innings_final_did_bat_last() -> None:
    assert (
        parse_game_feed(_load("final_extra_2024-04-13"))["linescore"]["home_did_not_bat_last"]
        is False
    )


def test_box_positions_are_starting_positions() -> None:
    card = parse_game_feed(_load("final_home_win_2024-08-15"))
    starters = [b for b in card["box"]["home"]["batters"] if not b["is_sub"]]
    positions = {b["pos"] for b in starters}
    assert {"C", "SS", "CF"} <= positions
    assert all(b["avg"] for b in starters)


def test_preview_has_no_linescore_or_box() -> None:
    card = parse_game_feed(_load("preview_2026-10-10"))
    assert card["status"] == "scheduled"
    assert card["linescore"] is None and card["box"] is None and card["live"] is None
    assert card["lineups"]["away"] == [] and card["lineups"]["home"] == []


def test_preview_with_a_posted_lineup() -> None:
    feed = copy.deepcopy(_load("final_home_win_2024-08-15"))
    feed["gameData"]["status"].update(
        abstractGameState="Preview", codedGameState="P", detailedState="Pre-Game"
    )
    card = parse_game_feed(feed)
    assert card["status"] == "scheduled"
    lineup = card["lineups"]["home"]
    assert [s["order"] for s in lineup] == list(range(1, 10))
    assert all(s["name"] and s["pos"] for s in lineup)


def test_live_game_state() -> None:
    card = parse_game_feed(_as_live("final_home_win_2024-08-15"))
    assert card["status"] == "live"
    live = card["live"]
    assert live["offense"] in ("away", "home")
    assert live["batter"]["name"]
    assert live["pitcher"]["name"] and live["pitcher"]["np"] > 0
    assert set(live["fielders"]) == {"P", "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF"}
    assert all(live["fielders"].values())
    assert live["last_play"]
    assert card["linescore"]["current_inning"] == 9
    assert set(live["runners"]) == {"first", "second", "third"}


def test_live_runner_on_base() -> None:
    feed = _as_live("final_home_win_2024-08-15")
    feed["liveData"]["linescore"]["offense"]["second"] = {"id": 1, "fullName": "A Runner"}
    live = parse_game_feed(feed)["live"]
    assert live["runners"]["second"] == {"id": 1, "name": "A Runner"}
    assert live["runners"]["first"] is None


def test_live_batting_side_from_the_offense_team() -> None:
    feed = _as_live("final_home_win_2024-08-15")
    home_id = feed["liveData"]["boxscore"]["teams"]["home"]["team"]["id"]
    feed["liveData"]["linescore"]["offense"]["team"] = {"id": home_id}
    assert parse_game_feed(feed)["live"]["offense"] == "home"


def test_postponed_feed() -> None:
    feed = copy.deepcopy(_load("preview_2026-10-10"))
    feed["gameData"]["status"].update(
        abstractGameState="Final", codedGameState="D", detailedState="Postponed"
    )
    card = parse_game_feed(feed)
    assert card["status"] == "postponed"
    assert card["linescore"] is None


def test_every_fixture_validates_against_the_model() -> None:
    for name in ("final_extra_2024-04-13", "final_home_win_2024-08-15", "preview_2026-10-10"):
        games_mod.GameFeedCardModel(**parse_game_feed(_load(name)))
    games_mod.GameFeedCardModel(**parse_game_feed(_as_live("final_home_win_2024-08-15")))


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


class _Feed:
    def __init__(self, payload: dict | None = None, error: Exception | None = None):
        self.payload, self.error, self.calls = payload, error, 0

    async def game_feed_payload(self, game_pk: int) -> dict:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload or {}


def _client(feed, cache=None) -> TestClient:
    app = FastAPI()
    app.include_router(games_router)
    app.state.league_feed = feed
    app.state.sim_cache = cache
    return TestClient(app)


def test_endpoint_serves_and_caches() -> None:
    feed = _Feed(_load("final_extra_2024-04-13"))
    client = _client(feed, InMemoryCache())
    a = client.get("/api/games/746489/feed")
    b = client.get("/api/games/746489/feed")
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
    assert a.json()["source"] == "feed"
    assert feed.calls == 1


def test_ttl_by_state() -> None:
    assert games_mod.FEED_TTL_BY_STATE_S["live"] == 10
    assert games_mod.FEED_TTL_BY_STATE_S["scheduled"] == 600
    assert games_mod.FEED_TTL_BY_STATE_S["final"] == 24 * 3600


def test_error_serves_the_last_good_copy() -> None:
    cache = InMemoryCache()
    _client(_Feed(_as_live("final_home_win_2024-08-15")), cache).get("/api/games/746437/feed")
    cache._store.pop("feed:raw:v1:746437")  # noqa: SLF001 -- the 10-second TTL ran out
    body = _client(_Feed(error=TimeoutError("slow")), cache).get("/api/games/746437/feed").json()
    assert body["source"] == "feed_cached"
    assert "TimeoutError" in body["feed_error"]
    assert body["live"]["last_play"]


def test_error_with_no_copy_is_503() -> None:
    resp = _client(_Feed(error=OSError("down"))).get("/api/games/1/feed")
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "30"


def test_no_feed_attached_is_503() -> None:
    assert _client(None).get("/api/games/1/feed").status_code == 503
