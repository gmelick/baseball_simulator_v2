"""SIM-519 Part A — the schedule-driven slate endpoint.

A tiny app with the games router only (the ``test_api_games.py`` idiom). The
league feed is a fake on ``app.state.league_feed`` that serves a recorded
schedule response; the pool is a fake that answers the two merge queries.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.games as games_mod
from api.routes.games import router as games_router
from simulation.batch_runner import InMemoryCache

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "mlb_schedule"


def _payload(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


class _Feed:
    def __init__(self, payload: dict | None = None, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls: list[tuple[date, date]] = []

    async def schedule_payload(self, start: date, end: date) -> dict:
        self.calls.append((start, end))
        if self.error is not None:
            raise self.error
        assert self.payload is not None
        return self.payload


class _Pool:
    """Answers the enrichment and the run queries; the stored listing for the db path."""

    def __init__(
        self,
        *,
        enrich=(),
        runs=(),
        listing=(),
        fail_runs: bool = False,
        no_source_column: bool = False,
    ):
        self.no_source_column = no_source_column
        self.enrich = list(enrich)
        self.runs = list(runs)
        self.listing = list(listing)
        self.fail_runs = fail_runs
        self.calls: list[str] = []

    async def fetch(self, sql, *args):
        self.calls.append(sql)
        if "sim.sim_runs" in sql:
            if self.fail_runs:
                raise RuntimeError("relation sim.sim_runs does not exist")
            return self.runs
        if "ANY($1::int[])" in sql:
            if "lineup_source" in sql and self.no_source_column:
                raise RuntimeError('column "source" does not exist')
            return self.enrich
        return self.listing


def _app(*, feed=None, pool=None, cache=None) -> TestClient:
    app = FastAPI()
    app.include_router(games_router)
    app.state.league_feed = feed
    app.state.pg_pool = pool
    app.state.sim_cache = cache
    return TestClient(app)


_SUMMARY = {
    "n_iterations": 100,
    "home_win_pct": 0.54,
    "away_win_pct": 0.46,
    "tie_pct": 0.0,
    "home_score_mean": 4.6,
    "away_score_mean": 4.1,
    "total_score_mean": 8.7,
    "home_score_median": 4.0,
    "away_score_median": 4.0,
    "total_score_median": 8.0,
    "home_win_ci": {"point": 0.54, "low": 0.44, "high": 0.64},
    "away_win_ci": {"point": 0.46, "low": 0.36, "high": 0.56},
    "home_score_ci": {"point": 4.6, "low": 4.1, "high": 5.1},
    "away_score_ci": {"point": 4.1, "low": 3.6, "high": 4.6},
    "total_score_ci": {"point": 8.7, "low": 8.0, "high": 9.4},
    "simulated_at": "2024-08-15T12:00:00+00:00",
    "home_scores": [1, 2, 3],
}


def test_schedule_cards_merge_our_data_per_game() -> None:
    sched = _payload("normal_2024-08-15")
    pks = [g["gamePk"] for g in sched["dates"][0]["games"]]
    known, run_pk = pks[0], pks[1]
    pool = _Pool(
        enrich=[
            {"game_pk": known, "status": "Final", "venue_city": "Queens", "lineup_ready": True}
        ],
        runs=[
            {
                "game_pk": run_pk,
                "run_id": 9,
                "n_iterations": 100,
                "summary": json.dumps(_SUMMARY),
                "created_at": "2024-08-15T12:00:00+00:00",
            }
        ],
    )
    client = _app(feed=_Feed(sched), pool=pool)
    body = client.get("/api/games/2024-08-15").json()

    assert body["source"] == "schedule"
    assert body["feed_error"] is None
    assert body["count"] == 7
    cards = {g["game_pk"]: g for g in body["games"]}
    k = cards[known]
    assert k["db_known"] is True and k["lineup_ready"] is True
    assert k["venue_city"] == "Queens"
    assert k["game_status"] == "final"
    assert k["away_score"] is not None and k["home_score"] is not None
    assert k["start_utc"].endswith("+00:00")
    assert k["n_innings"] >= 9
    # A schedule game with no stored row is still a card.
    unknown = cards[pks[2]]
    assert unknown["db_known"] is False and unknown["lineup_ready"] is False
    # The newest run's summary rides on its card, without the raw arrays.
    r = cards[run_pk]
    assert r["sim_summary"]["home_win_pct"] == pytest.approx(0.54)
    assert "home_scores" not in r["sim_summary"]
    assert r["sim_run_at"].startswith("2024-08-15")
    assert cards[known]["sim_summary"] is None
    # Two merge queries, not one per game.
    assert len(pool.calls) == 2


def test_records_come_from_the_schedule() -> None:
    client = _app(feed=_Feed(_payload("normal_2024-08-15")), pool=_Pool())
    card = next(
        g
        for g in client.get("/api/games/2024-08-15").json()["games"]
        if g["home_team_abbrev"] == "NYM"
    )
    assert (card["home_wins"], card["home_losses"]) == (62, 59)
    assert card["home_probable_pitcher_name"] == "Jose Quintana"


def test_postponed_card_carries_reason_and_makeup() -> None:
    client = _app(feed=_Feed(_payload("postponed_2024-04-02")), pool=_Pool())
    games = client.get("/api/games/2024-04-02").json()["games"]
    ppd = [g for g in games if g["game_status"] == "postponed"]
    assert ppd
    assert ppd[0]["reason"] == "Rain"
    assert ppd[0]["rescheduled_to"] == "2024-04-04"
    assert ppd[0]["away_score"] is None


def test_preview_card_has_no_score_and_a_start_time() -> None:
    client = _app(feed=_Feed(_payload("preview_2026-10-10")), pool=_Pool())
    g = client.get("/api/games/2026-10-10").json()["games"][0]
    assert g["game_status"] == "scheduled"
    assert g["away_score"] is None and g["home_score"] is None
    assert g["start_utc"] == "2026-10-11T00:00:00+00:00"
    assert g["series_description"] == "AL Division Series"


def test_off_day_is_an_empty_schedule_not_the_db() -> None:
    pool = _Pool(
        listing=[{"game_pk": 1, "season": 2024, "game_date": "2024-07-17", "status": "Final"}]
    )
    body = (
        _app(feed=_Feed(_payload("off_day_2024-07-17")), pool=pool)
        .get("/api/games/2024-07-17")
        .json()
    )
    assert body["source"] == "schedule"
    assert body["count"] == 0


def test_a_db_row_the_schedule_lacks_is_dropped() -> None:
    # The stored listing is never read on the schedule path.
    pool = _Pool(
        listing=[{"game_pk": 1, "season": 2024, "game_date": "2024-08-15", "status": "Final"}]
    )
    body = (
        _app(feed=_Feed(_payload("normal_2024-08-15")), pool=pool)
        .get("/api/games/2024-08-15")
        .json()
    )
    assert 1 not in {g["game_pk"] for g in body["games"]}


def test_a_missing_run_table_still_serves_cards() -> None:
    body = (
        _app(feed=_Feed(_payload("normal_2024-08-15")), pool=_Pool(fail_runs=True))
        .get("/api/games/2024-08-15")
        .json()
    )
    assert body["count"] == 7
    assert all(g["sim_summary"] is None for g in body["games"])


def test_the_cache_serves_a_repeat_without_the_feed() -> None:
    feed = _Feed(_payload("normal_2024-08-15"))
    client = _app(feed=feed, pool=_Pool(), cache=InMemoryCache())
    client.get("/api/games/2024-08-15")
    client.get("/api/games/2024-08-15")
    assert len(feed.calls) == 1
    client.get("/api/games/2024-08-15?use_cache=false")
    assert len(feed.calls) == 2


def test_feed_error_serves_the_last_good_schedule() -> None:
    cache = InMemoryCache()
    good = _Feed(_payload("normal_2024-08-15"))
    _app(feed=good, pool=_Pool(), cache=cache).get("/api/games/2024-08-15")
    # The fresh key expires; the last-good key stays.
    cache._store.pop("slate:v2:2024-08-15")  # noqa: SLF001 -- simulate the TTL
    bad = _Feed(error=TimeoutError("read timed out"))
    body = _app(feed=bad, pool=_Pool(), cache=cache).get("/api/games/2024-08-15").json()
    assert body["source"] == "schedule_cached"
    assert "TimeoutError" in body["feed_error"]
    assert body["count"] == 7


def test_feed_error_with_no_copy_serves_the_stored_listing() -> None:
    listing = [
        {
            "game_pk": 745001,
            "season": 2024,
            "game_date": "2024-08-15",
            "status": "Final",
            "home_team_id": 147,
            "away_team_id": 111,
        },
        {"game_pk": 745002, "season": 2024, "game_date": "2024-08-15", "status": "Postponed"},
    ]
    body = (
        _app(feed=_Feed(error=OSError("down")), pool=_Pool(listing=listing))
        .get("/api/games/2024-08-15")
        .json()
    )
    assert body["source"] == "db"
    assert "OSError" in body["feed_error"]
    states = {g["game_pk"]: g["game_status"] for g in body["games"]}
    assert states == {745001: "final", 745002: "postponed"}


def test_feed_error_and_no_pool_is_503() -> None:
    resp = _app(feed=_Feed(error=OSError("down")), pool=None).get("/api/games/2024-08-15")
    assert resp.status_code == 503


def test_schedule_path_needs_no_pool() -> None:
    body = (
        _app(feed=_Feed(_payload("normal_2024-08-15")), pool=None)
        .get("/api/games/2024-08-15")
        .json()
    )
    assert body["count"] == 7
    assert all(g["db_known"] is False for g in body["games"])


def test_suspended_game_is_one_card() -> None:
    payload = _payload("suspended_2024-05-21")
    entry = next(g for g in payload["dates"][0]["games"] if g["gamePk"] == 745180)
    payload["dates"][0]["games"].append(entry)  # the league lists it twice on a resume day
    body = _app(feed=_Feed(payload), pool=_Pool()).get("/api/games/2024-05-21").json()
    assert [g["game_pk"] for g in body["games"]].count(745180) == 1


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 10, 8), games_mod.SLATE_TTL_PAST_S),
        (date(2026, 10, 9), games_mod.SLATE_TTL_TODAY_S),
        (date(2026, 10, 10), games_mod.SLATE_TTL_FUTURE_S),
    ],
)
def test_ttl_by_date_class(day: date, expected: int) -> None:
    assert games_mod._slate_ttl(day, date(2026, 10, 9)) == expected  # noqa: SLF001


def test_the_old_mock_envelope_still_validates() -> None:
    old = {
        "date": "2024-08-15",
        "count": 1,
        "games": [{"game_pk": 1, "season": 2024, "game_date": "2024-08-15"}],
    }
    resp = games_mod.GamesOnDateResponse(**old)
    assert resp.games[0].game_status is None


def test_lineup_source_rides_on_the_card() -> None:
    sched = _payload("normal_2024-08-15")
    pk = sched["dates"][0]["games"][0]["gamePk"]
    pool = _Pool(
        enrich=[
            {
                "game_pk": pk,
                "status": "Preview",
                "venue_city": "X",
                "lineup_ready": True,
                "lineup_source": "published",
            }
        ]
    )
    card = next(
        g
        for g in _app(feed=_Feed(sched), pool=pool).get("/api/games/2024-08-15").json()["games"]
        if g["game_pk"] == pk
    )
    assert card["lineup_source"] == "published"


def test_before_0029_the_plain_enrichment_read_serves() -> None:
    sched = _payload("normal_2024-08-15")
    pk = sched["dates"][0]["games"][0]["gamePk"]
    pool = _Pool(
        enrich=[{"game_pk": pk, "status": "Final", "venue_city": "X", "lineup_ready": True}],
        no_source_column=True,
    )
    card = next(
        g
        for g in _app(feed=_Feed(sched), pool=pool).get("/api/games/2024-08-15").json()["games"]
        if g["game_pk"] == pk
    )
    assert card["lineup_ready"] is True and card["lineup_source"] is None
