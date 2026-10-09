"""The game page's "what if": the real plays, the state at a plate appearance,
managerial changes, and simulating the rest of the game from that point.

The feed fixture is game 824381 (TEX at CLE, 2026-09-20), read with
``hydrate=alignment`` and trimmed by ``record_fixtures.py --plays``. It has a
mid-game pitching change, a pinch hitter, a pinch runner and an intentional walk.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline.mlb_game_feed import PlateAppearanceNotFound, parse_plays, state_at_pa
from simulation.batch_runner import GameSpec, rng_driven_machine_factory
from simulation.game_state import GameState
from simulation.sim_kwargs import sim_kwargs_from_state
from simulation.sim_loop import simulate_game
from simulation.whatif import IllegalChange, apply_changes, describe_changes

FEED = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "mlb_game_feed"
        / "full_plays_2026-09-20.json"
    ).read_text(encoding="utf-8")
)
PLAYS = parse_plays(FEED)
NAMES = PLAYS["names"]


def _name_id(full: str) -> int:
    return next(pid for pid, n in NAMES.items() if n == full)


def _play(at_bat: int) -> dict:
    return next(p for p in PLAYS["plays"] if p["at_bat"] == at_bat)


# ---------------------------------------------------------------------------
# The plays
# ---------------------------------------------------------------------------


def test_every_plate_appearance_is_a_play() -> None:
    plays = PLAYS["plays"]
    assert len(plays) == 64
    assert [p["at_bat"] for p in plays] == list(range(64))
    last = plays[-1]
    assert (last["away_score_after"], last["home_score_after"]) == (0, 1)
    # The intentional walk is a play like any other.
    assert any(p["event_type"] == "intent_walk" for p in plays)
    # The scores chain: each play's "before" is the previous play's "after".
    for a, b in zip(plays, plays[1:], strict=False):
        assert (b["away_score_before"], b["home_score_before"]) == (
            a["away_score_after"],
            a["home_score_after"],
        )


def test_outs_before_reset_each_half() -> None:
    for p in PLAYS["plays"]:
        if p["at_bat"] > 0:
            prev = _play(p["at_bat"] - 1)
            if (prev["inning"], prev["half"]) != (p["inning"], p["half"]):
                assert p["outs_before"] == 0


# ---------------------------------------------------------------------------
# The state at a plate appearance
# ---------------------------------------------------------------------------


def test_the_due_up_batter_is_the_real_batter_at_every_plate_appearance() -> None:
    for p in PLAYS["plays"]:
        st = state_at_pa(FEED, p["at_bat"])
        side = st["batting_side"]
        assert st[f"{side}_lineup"][st[f"{side}_slot"]] == p["batter_id"], p["at_bat"]
        assert (st["inning"], st["half"]) == (p["inning"], p["half"])
        assert (st["away_score"], st["home_score"]) == (
            p["away_score_before"],
            p["home_score_before"],
        )


def test_the_pitcher_on_the_mound_is_the_real_one() -> None:
    for p in PLAYS["plays"]:
        st = state_at_pa(FEED, p["at_bat"])
        fielding = "home" if st["batting_side"] == "away" else "away"
        assert st[f"{fielding}_pitcher"] == p["pitcher_id"], p["at_bat"]
        assert st[f"{fielding}_defense"]["P"] == p["pitcher_id"]
        assert len(st[f"{fielding}_defense"]) == 9


def test_the_pinch_hitter_and_runner_are_in_the_lineup() -> None:
    st = state_at_pa(FEED, 52)
    mcneil, stefanic = _name_id("Jeff McNeil"), _name_id("Michael Stefanic")
    assert mcneil in st["away_lineup"] and stefanic in st["away_lineup"]
    assert st["runners"]["second"] == stefanic and st["runners"]["third"] == mcneil
    # The men they replaced are used and cannot come back.
    assert _name_id("Jonah Heim") in st["used"]["away"]
    assert _name_id("Jonah Heim") not in st["eligible"]["away"]["hitters"]


def test_pitch_counts_and_batters_faced_so_far() -> None:
    st = state_at_pa(FEED, 30)
    williams = _name_id("Gavin Williams")
    assert st["batters_faced"][str(williams)] == sum(
        1 for p in PLAYS["plays"][:30] if p["pitcher_id"] == williams
    )
    assert st["pitch_counts"][str(williams)] == sum(
        len(p["pitches"]) for p in PLAYS["plays"][:30] if p["pitcher_id"] == williams
    )
    assert st["half_pa_count"] == sum(
        1 for p in PLAYS["plays"][:30] if (p["inning"], p["half"]) == (5, "top")
    )


def test_the_grid_so_far_sums_to_the_score() -> None:
    st = state_at_pa(FEED, 58)
    assert sum(st["grid_away"]) == st["half_start_away"]
    assert sum(st["grid_home"]) == st["half_start_home"]


def test_used_arms_leave_the_pen() -> None:
    st = state_at_pa(FEED, 53)
    gaddis = _name_id("Hunter Gaddis")
    assert gaddis not in st["bullpen"]["home"]
    assert _name_id("Cade Smith") not in st["bullpen"]["home"]  # on the mound


def test_a_reliever_is_never_a_bench_bat() -> None:
    # The box lists every player who appeared under `batters`, relievers included.
    for at_bat in (None, 44):
        st = state_at_pa(FEED, at_bat)
        for side in ("away", "home"):
            hitters = set(st["eligible"][side]["hitters"])
            assert not hitters & set(st["eligible"][side]["pitchers"])
    assert _name_id("Hunter Gaddis") not in state_at_pa(FEED, 44)["eligible"]["home"]["hitters"]


def test_first_pitch() -> None:
    st = state_at_pa(FEED, None)
    assert (st["inning"], st["half"], st["outs"]) == (1, "top", 0)
    assert (st["away_slot"], st["home_slot"]) == (0, 0)
    assert st["runners"] == {"first": None, "second": None, "third": None}
    assert len(st["eligible"]["home"]["pitchers"]) > 5


def test_an_unknown_plate_appearance() -> None:
    with pytest.raises(PlateAppearanceNotFound):
        state_at_pa(FEED, 999)


# ---------------------------------------------------------------------------
# Managerial changes
# ---------------------------------------------------------------------------


def test_reliever() -> None:
    st = state_at_pa(FEED, 40)
    fielding = "home" if st["batting_side"] == "away" else "away"
    arm = st["eligible"][fielding]["pitchers"][0]
    out = apply_changes(st, {"pitcher": {"player_id": arm}})
    assert out[f"{fielding}_pitcher"] == arm
    assert out[f"{fielding}_defense"]["P"] == arm
    assert out["pitch_counts"][str(arm)] == 0
    assert arm not in out["bullpen"][fielding]
    assert st[f"{fielding}_pitcher"] != arm  # the input is not changed


def test_pinch_hitter_takes_the_slot_and_the_position() -> None:
    st = state_at_pa(FEED, 40)
    side = st["batting_side"]
    slot = st[f"{side}_slot"]
    old = st[f"{side}_lineup"][slot]
    ph = st["eligible"][side]["hitters"][0]
    out = apply_changes(st, {"pinch_hit": [{"slot": slot, "player_id": ph}]})
    assert out[f"{side}_lineup"][slot] == ph
    assert old not in out[f"{side}_defense"].values()


def test_pinch_runner() -> None:
    st = state_at_pa(FEED, 58)
    runner = st["runners"]["first"]
    pr = st["eligible"]["home"]["hitters"][0]
    out = apply_changes(st, {"pinch_run": [{"base": "first", "player_id": pr}]})
    assert out["runners"]["first"] == pr
    assert runner not in out["home_lineup"] and pr in out["home_lineup"]


def test_defensive_switch_swaps_positions() -> None:
    st = state_at_pa(FEED, 40)
    fielding = "home" if st["batting_side"] == "away" else "away"
    d = st[f"{fielding}_defense"]
    lf, cf = d["LF"], d["CF"]
    out = apply_changes(st, {"defense": [{"side": fielding, "position": "LF", "player_id": cf}]})
    assert out[f"{fielding}_defense"]["LF"] == cf and out[f"{fielding}_defense"]["CF"] == lf


@pytest.mark.parametrize(
    "changes, words",
    [
        ({"pitcher": {"player_id": 1}}, "available reliever"),
        ({"pinch_run": [{"base": "third", "player_id": 1}]}, "nobody is on third"),
        ({"pinch_hit": [{"slot": 11, "player_id": 1}]}, "not 0-8"),
        ({"defense": [{"position": "ZZ", "player_id": 1}]}, "position"),
    ],
)
def test_illegal_changes(changes: dict, words: str) -> None:
    st = state_at_pa(FEED, 40)
    with pytest.raises(IllegalChange, match=words):
        apply_changes(st, changes)


def test_a_used_player_cannot_return() -> None:
    st = state_at_pa(FEED, 53)
    heim = _name_id("Jonah Heim")
    with pytest.raises(IllegalChange):
        apply_changes(st, {"pinch_hit": [{"slot": 0, "player_id": heim}]})


def test_describe_changes() -> None:
    lines = describe_changes(
        {"pitcher": {"player_id": _name_id("Cade Smith")}}, {str(k): v for k, v in NAMES.items()}
    )
    assert lines == ["Reliever: Cade Smith"]


# ---------------------------------------------------------------------------
# Simulating from a start state (the synthetic bundle)
# ---------------------------------------------------------------------------


def _base_state() -> GameState:
    s = GameState(pitcher_id=401, bat_hand="R", season=2024)
    s.away_lineup = [101 + i for i in range(9)]
    s.home_lineup = [201 + i for i in range(9)]
    s.batter_id = 101
    s.throw_hand = "R"
    s.away_pitcher_id = 301
    s.home_pitcher_id = 401
    s.manager.bullpen_available = {0: [302, 303], 1: [402, 403]}
    return s


def _start(**over: object) -> dict:
    ss = {
        "inning": 7,
        "half": "bottom",
        "outs": 1,
        "away_score": 3,
        "home_score": 2,
        "half_start_away": 3,
        "half_start_home": 1,
        "grid_away": [0, 1, 0, 2, 0, 0, 0],
        "grid_home": [0, 0, 1, 0, 0, 0],
        "runners": {"first": 203, "second": None, "third": None},
        "away_lineup": [101 + i for i in range(9)],
        "home_lineup": [201 + i for i in range(9)],
        "away_slot": 4,
        "home_slot": 3,
        "away_pitcher": 302,
        "home_pitcher": 402,
        "away_starter": 301,
        "home_starter": 401,
        "pitch_counts": {"302": 14},
        "batters_faced": {"302": 4},
        "half_pa_count": 2,
        "bullpen": {"away": [303], "home": [403]},
    }
    ss.update(over)
    return ss


def _run(ss: dict, seed: int) -> object:
    kw = sim_kwargs_from_state(_base_state(), allow_unavailable_park_factor=True)
    machine = rng_driven_machine_factory(seed, GameSpec(sim_kwargs=kw))
    return simulate_game(machine, seed=seed, **kw, start_state=ss)


@pytest.mark.parametrize("seed", range(12))
def test_a_game_started_mid_way_keeps_the_real_runs(seed: int) -> None:
    res = _run(_start(), seed)
    # The game was 3-2 in the bottom of the 7th: the final can only add runs.
    assert res.away_score >= 3 and res.home_score >= 2
    away, home = res.away_by_inning, res.home_by_inning
    assert away[:7] == [0, 1, 0, 2, 0, 0, 0]
    assert home[:6] == [0, 0, 1, 0, 0, 0]
    assert sum(v for v in away if v) == res.away_score
    assert sum(v for v in home if v) == res.home_score
    # The 7th's home cell counts the run already scored in that half.
    assert home[6] >= 1


@pytest.mark.parametrize("seed", range(8))
def test_no_ghost_runner_is_added_in_the_half_the_game_starts_in(
    seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    import simulation.sim_loop as loop

    placed: list[tuple[int, int]] = []
    real = loop._place_ghost_runner

    def spy(state: GameState) -> None:
        placed.append((state.inning, int(state.half)))
        real(state)

    monkeypatch.setattr(loop, "_place_ghost_runner", spy)
    ss = _start(
        inning=10,
        half="top",
        outs=1,
        away_score=4,
        home_score=4,
        half_start_away=4,
        half_start_home=4,
        grid_away=[4] + [0] * 8,
        grid_home=[4] + [0] * 8,
        runners={"first": None, "second": None, "third": None},
        half_pa_count=1,
    )
    res = _run(ss, seed)
    assert (10, 0) not in placed  # the top of the 10th was already under way
    assert res.away_by_inning[:9] == ss["grid_away"]


def test_the_start_state_is_not_mutated() -> None:
    ss = _start()
    before = copy.deepcopy(ss)
    _run(ss, 3)
    assert ss == before


# ---------------------------------------------------------------------------
# The routes: /feed/plays, /feed/state, /what-if
# ---------------------------------------------------------------------------


class _Feed:
    def __init__(self) -> None:
        self.calls = 0

    async def game_feed_payload(self, game_pk: int) -> dict:
        self.calls += 1
        return copy.deepcopy(FEED)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    from contextlib import asynccontextmanager

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tests.unit.test_sim519_sim_jobs import _Pool as _RunTable

    import api.routes.games as games_mod
    import api.sim_jobs as sj
    from api.auth import require_auth

    async def fake_resolve(conn, game_pk, **kw):
        return _base_state()

    monkeypatch.setattr(games_mod, "resolve_game_state", fake_resolve)

    class _PgPool:
        async def fetchval(self, *a):
            return None

        async def fetch(self, *a):
            return []

        async def fetchrow(self, *a):
            return None

        @asynccontextmanager
        async def acquire(self):
            yield self

    app = FastAPI()
    app.include_router(games_mod.router)
    app.dependency_overrides[require_auth] = lambda: None
    app.state.league_feed = _Feed()
    app.state.pg_pool = _PgPool()
    app.state.sim_cache = None
    app.state.sim_factory_ref = "simulation.batch_runner:rng_driven_machine_factory"
    app.state.sim_jobs = sj.SimJobRegistry(_RunTable(), concurrency=1)
    with TestClient(app) as c:
        yield c


def test_the_plays_route(client) -> None:
    body = client.get("/api/games/824381/feed/plays").json()
    assert body["status"] == "final"
    assert len(body["plays"]) == 64
    assert body["names"][str(_name_id("Cade Smith"))] == "Cade Smith"


def test_the_state_route(client) -> None:
    body = client.get("/api/games/824381/feed/state?at_bat=52").json()
    assert (body["inning"], body["half"], body["outs"]) == (8, "top", 1)
    assert body["batting_side"] == "away"
    assert body["live"]["runners"]["second"]["name"] == "Michael Stefanic"
    assert body["live"]["batter"]["name"] == "Carlos Cortes"
    assert set(body["live"]["fielders"]) == {"P", "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF"}
    assert body["home"]["bullpen"] and body["away"]["bench"]
    assert [s["slot"] for s in body["away"]["lineup"]] == list(range(9))
    assert client.get("/api/games/824381/feed/state?at_bat=999").status_code == 404


def test_first_pitch_state(client) -> None:
    body = client.get("/api/games/824381/feed/state").json()
    assert (body["inning"], body["half"], body["outs"], body["at_bat"]) == (1, "top", 0, None)


def test_an_illegal_change_is_422(client) -> None:
    resp = client.post(
        "/api/games/824381/what-if", json={"at_bat": 52, "changes": {"pitcher": {"player_id": 1}}}
    )
    assert resp.status_code == 422
    assert "available reliever" in resp.text


def test_a_what_if_runs_both_versions(client) -> None:
    import time

    st = client.get("/api/games/824381/feed/state?at_bat=52").json()
    arm = st["home"]["bullpen"][0]["id"]
    resp = client.post(
        "/api/games/824381/what-if",
        json={"at_bat": 52, "changes": {"pitcher": {"player_id": arm}}, "n_iterations": 10},
    )
    assert resp.status_code == 202, resp.text
    started = resp.json()
    assert started["base_run_id"] != started["change_run_id"]
    assert started["changes"] == [f"Reliever: {st['home']['bullpen'][0]['name']}"]
    url = f"/api/games/824381/what-if/{started['base_run_id']}/{started['change_run_id']}"
    for _ in range(600):
        body = client.get(url).json()
        if body["base"]["status"] in ("done", "failed") and body["change"]["status"] in (
            "done",
            "failed",
        ):
            break
        time.sleep(0.05)
    assert body["base"]["status"] == "done", body["base"].get("error")
    assert body["change"]["status"] == "done", body["change"].get("error")
    assert body["real_final"] == {"away": 0, "home": 1}
    assert body["start_score"] == {"away": 0, "home": 1}
    # The game was 0-1 in the top of the 8th: every simulated final keeps those runs.
    assert body["base"]["summary"]["home_score_mean"] >= 1.0


def test_before_first_pitch_either_side_changes_its_lineup_and_starter() -> None:
    st = state_at_pa(FEED, None)
    arm = st["eligible"]["away"]["pitchers"][0]
    hitter = st["eligible"]["home"]["hitters"][0]
    out = apply_changes(
        st,
        {
            "pitcher": {"side": "away", "player_id": arm},
            "pinch_hit": [{"side": "home", "slot": 2, "player_id": hitter}],
        },
    )
    assert out["away_pitcher"] == arm and out["home_lineup"][2] == hitter
    mid = state_at_pa(FEED, 40)
    with pytest.raises(IllegalChange):
        apply_changes(
            mid,
            {
                "pinch_hit": [
                    {
                        "side": "home" if mid["batting_side"] == "away" else "away",
                        "slot": 0,
                        "player_id": 1,
                    }
                ]
            },
        )
