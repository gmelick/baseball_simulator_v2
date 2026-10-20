"""The loop-output routes of api/routes/games.py: the game card, the fielders, the box.

WHAT'S COVERED
--------------
  1. The game-card store of db/sim_store.py, on a real in-memory DuckDB with the
     replay schema. store_game_card then load_game_card returns the jsonable
     linescore and decisions. The latest run wins when run_id is omitted. A missing
     card returns None. A given run_id is honoured.
  2. The schema converters: LinescoreModel.from_dataclass and .from_jsonable agree;
     PitcherDecisionsModel.from_dataclass and .from_jsonable agree;
     BoxscoreCardModel.from_prop_set exposes the per-player prop means (numpy-free).
  3. The routes end to end, on a small FastAPI app with only the games router. The
     app has a fake pg pool, an in-memory DuckDB on app.state.replay_duckdb and the
     no-DB factory, as in test_api_games_plays.py:
       * GET /simulate stores the game card and the state snapshots with fielders;
       * GET /linescore returns a numpy-free Linescore;
       * GET /decisions returns the W/L/S (win, loss, save) payload;
       * GET /card returns both;
       * GET /state/{ab}/{pitch} returns a FieldSnapshot whose nine positions all
         hold a player;
       * GET /boxscore returns the per-player prop means;
       * the 404 path (nothing stored) and the 503 path (no store);
       * /simulate still answers 200 with no replay store (the recording is best
         effort).

ISOLATION
---------
The tests follow test_api_games_plays.py: a mock app, a fake pool and the
patch_resolver fixture. The route builds the defense map from raw.game_lineups, so
the fake pool returns canned lineup rows for that query (matched on the SQL text).
The defense map is then full in the end-to-end test.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.games as games_mod
from api.routes.games import router as games_router
from api.schemas import (
    BoxscoreCardModel,
    LinescoreModel,
    PitcherDecisionsModel,
)
from api.serialization import to_jsonable
from db import sim_store
from simulation.game_state import GameState, Half, PlayResult
from simulation.linescore import linescore_from_plays
from simulation.pitcher_decisions import decisions_from_plays

NO_DB_FACTORY_REF = "simulation.batch_runner:rng_driven_machine_factory"


# ---------------------------------------------------------------------------
# DuckDB fixtures
# ---------------------------------------------------------------------------


def _fresh_duckdb():
    """A real in-memory DuckDB with the replay schema applied."""
    import duckdb

    con = duckdb.connect(":memory:")
    # The app's own schema setup for the replay file (0008, 0009, 0010 and
    # 0032), so the test and the app read one source.
    from db.sim_store import ensure_replay_schema

    ensure_replay_schema(con)
    return con


@pytest.fixture()
def duck_con():
    con = _fresh_duckdb()
    yield con
    con.close()


# ---------------------------------------------------------------------------
# Hand-built PlayResult streams with known scores, so the R/H/E and W/L/S
# checks have exact answers.
# ---------------------------------------------------------------------------


def _state(*, inning, half, home_score, away_score, pitcher_id):
    st = GameState(pitcher_id=pitcher_id, bat_hand="R", season=2024, half=half)
    st.inning = inning
    st.home_score = home_score
    st.away_score = away_score
    return st


def _play(*, event, runs_scored, is_error, next_state, canonical_event=None):
    return PlayResult(
        pitch_outcome="in_play",
        is_contact=True,
        pa_terminal=True,
        event=event,
        canonical_event=canonical_event if canonical_event is not None else event,
        runs_scored=runs_scored,
        is_error=is_error,
        outs_recorded=0,
        next_state=next_state,
    )


def _scoring_stream() -> list[PlayResult]:
    """A small finished game with known R/H/E and W/L/S answers.

    The away team scores 1 in the top 1st off the home pitcher (id 700). The home
    team scores 2 in the bottom 1st off the away pitcher (id 800) and keeps the
    lead. So the home team wins: 800 takes the loss, and 700 takes the win. The
    stream has one away hit, one home hit and one error. The error comes in the
    bottom half, so the away defense is charged with it."""
    plays: list[PlayResult] = []
    # Top 1st: away single, away scores 1 (home pitcher 700 on the mound).
    plays.append(
        _play(
            event="single",
            runs_scored=1,
            is_error=False,
            next_state=_state(inning=1, half=Half.TOP, home_score=0, away_score=1, pitcher_id=700),
        )
    )
    # Bottom 1st: home double (a hit) + an error by the away defense; home scores 2
    # to take a lead it never gives back (away pitcher 800 on the mound).
    plays.append(
        _play(
            event="double",
            runs_scored=2,
            is_error=False,
            next_state=_state(
                inning=1, half=Half.BOTTOM, home_score=2, away_score=1, pitcher_id=800
            ),
        )
    )
    plays.append(
        _play(
            event="field_out",
            runs_scored=0,
            is_error=True,  # error in the bottom half -> charged to the AWAY defense
            next_state=_state(
                inning=1, half=Half.BOTTOM, home_score=2, away_score=1, pitcher_id=800
            ),
        )
    )
    return plays


# ===========================================================================
# 1) DuckDB game-card store round-trip (real :memory: duckdb)
# ===========================================================================


class TestGameCardStore:
    def test_store_then_load_roundtrips(self, duck_con):
        plays = _scoring_stream()
        ls = to_jsonable(linescore_from_plays(plays))
        dec = to_jsonable(decisions_from_plays(plays))
        sim_store.store_game_card(duck_con, game_pk=900, run_id=7, linescore=ls, decisions=dec)
        got = sim_store.load_game_card(duck_con, game_pk=900)
        assert got is not None
        assert got["run_id"] == 7
        assert got["linescore"] == ls
        assert got["decisions"] == dec

    def test_load_missing_returns_none(self, duck_con):
        assert sim_store.load_game_card(duck_con, game_pk=12345) is None

    def test_latest_run_wins_without_run_id(self, duck_con):
        ls1 = {
            "innings": [],
            "away_runs": 1,
            "home_runs": 0,
            "away_hits": 0,
            "home_hits": 0,
            "away_errors": 0,
            "home_errors": 0,
        }
        ls2 = {**ls1, "away_runs": 9}
        dec = {
            "winning_pitcher_id": None,
            "losing_pitcher_id": None,
            "save_pitcher_id": None,
            "home_score": 0,
            "away_score": 0,
        }
        sim_store.store_game_card(duck_con, game_pk=901, run_id=1, linescore=ls1, decisions=dec)
        sim_store.store_game_card(duck_con, game_pk=901, run_id=2, linescore=ls2, decisions=dec)
        latest = sim_store.load_game_card(duck_con, game_pk=901)
        assert latest["run_id"] == 2
        assert latest["linescore"]["away_runs"] == 9
        # A specific run_id is honoured.
        by_id = sim_store.load_game_card(duck_con, game_pk=901, run_id=1)
        assert by_id["linescore"]["away_runs"] == 1


# ===========================================================================
# 2) Schema converters (from_dataclass / from_jsonable agree; numpy-free)
# ===========================================================================


class TestSchemaConverters:
    def test_linescore_from_dataclass_and_from_jsonable_agree(self):
        ls = linescore_from_plays(_scoring_stream())
        from_dc = LinescoreModel.from_dataclass(ls)
        from_js = LinescoreModel.from_jsonable(to_jsonable(ls))
        assert from_dc.model_dump() == from_js.model_dump()
        # R/H/E reflect the hand-built stream: away 1R/1H, home 2R/1H, away 1E.
        assert from_dc.away_runs == 1 and from_dc.home_runs == 2
        assert from_dc.away_hits == 1 and from_dc.home_hits == 1
        assert from_dc.away_errors == 1 and from_dc.home_errors == 0
        assert from_dc.n_innings == 1
        json.dumps(from_dc.model_dump())  # numpy-free

    def test_decisions_from_dataclass_and_from_jsonable_agree(self):
        dec = decisions_from_plays(_scoring_stream())
        from_dc = PitcherDecisionsModel.from_dataclass(dec)
        from_js = PitcherDecisionsModel.from_jsonable(to_jsonable(dec))
        assert from_dc.model_dump() == from_js.model_dump()
        # HOME won 2-1; the decisive lead came in the bottom 1st (away pitcher 800
        # on the mound) so 800 loses and the home pitcher 700 wins.
        assert from_dc.home_score == 2 and from_dc.away_score == 1
        assert from_dc.winning_pitcher_id == 700
        assert from_dc.losing_pitcher_id == 800
        json.dumps(from_dc.model_dump())  # numpy-free

    def test_boxscore_card_from_prop_set_exposes_means(self):
        from simulation.prop_distributions import PropDistributionSet
        from simulation.results import BoxScore

        # Two games: a pitcher (id 1) with 5 then 7 K, a batter (id 2) with 1 then 2 H.
        boxes = []
        for k, h in ((5, 1), (7, 2)):
            box = BoxScore()
            pl = box.line(1)
            pl.outs_recorded, pl.k = 18, k
            bl = box.line(2)
            bl.ab, bl.h = 4, h
            boxes.append(box)
        pset = PropDistributionSet.from_boxscores(boxes)
        card = BoxscoreCardModel.from_prop_set(pset)
        assert card.n_iterations == 2
        # Pitcher mean K = (5+7)/2 = 6.0; batter mean H = (1+2)/2 = 1.5.
        assert card.players["1"].means["K"] == pytest.approx(6.0)
        assert card.players["2"].means["H"] == pytest.approx(1.5)
        json.dumps(card.model_dump())  # numpy-free


# ---------------------------------------------------------------------------
# Fakes (as in test_api_games_plays.py) and canned lineup rows for the defense
# ---------------------------------------------------------------------------

# Canned raw.game_lineups rows, so resolve_lineup builds a full defense map.
# Home team 10, away team 20. Each side has a 9-slot order with fielding
# position codes 1..9, so build_team_defense_map yields all 9 fielders.
_HOME_TEAM_ID, _AWAY_TEAM_ID = 10, 20


def _lineup_rows():
    rows = []
    for tid, base in ((_HOME_TEAM_ID, 200), (_AWAY_TEAM_ID, 100)):
        for slot in range(1, 10):
            rows.append(
                {
                    "team_id": tid,
                    "player_id": base + slot,
                    "batting_order": slot,
                    "position_code": str(slot),  # 1..9 -> P,C,1B,...RF
                    "is_starter": True,
                    "sequence": 1,
                    "entered_inning": 1,
                    "entered_at_bat": None,
                    "pinch_role": None,
                }
            )
    return rows


class _LineupPool:
    """A fake asyncpg pool and connection, matched on the SQL text.

    The fake answers resolve_lineup's three queries and the run-history
    INSERT ... RETURNING (fetchval). The tests monkeypatch resolve_game_state, so
    the pool serves only resolve_lineup (the defense map) and the history write."""

    def __init__(self):
        self.store_calls: list = []

    async def fetch(self, sql, *args):
        if "FROM   raw.game_lineups" in sql or "FROM raw.game_lineups" in sql:
            return _lineup_rows()
        if "FROM   raw.players" in sql or "FROM raw.players" in sql:
            # bats/throws hands for every player id we use.
            ids = args[0] if args else []
            return [{"player_id": pid, "bats": "R", "throws": "R"} for pid in ids]
        return []

    async def fetchrow(self, sql, *args):
        if "FROM   raw.games" in sql or "FROM raw.games" in sql:
            return {
                "game_pk": int(args[0]),
                "season": 2024,
                "home_team_id": _HOME_TEAM_ID,
                "away_team_id": _AWAY_TEAM_ID,
            }
        return None

    async def fetchval(self, sql, *args):
        self.store_calls.append((sql, args))
        return 4242


def _small_game_state() -> GameState:
    state = GameState(pitcher_id=201, bat_hand="R", season=2024)
    state.away_lineup = [101, 102, 103, 104, 105, 106, 107, 108, 109]
    state.home_lineup = [201, 202, 203, 204, 205, 206, 207, 208, 209]
    state.away_lineup_slot = 0
    state.home_lineup_slot = 0
    state.batter_id = 101
    state.throw_hand = "R"
    return state


def _build_app(*, pool, duck=None, factory_ref=NO_DB_FACTORY_REF) -> FastAPI:
    app = FastAPI()
    app.include_router(games_router)
    app.state.pg_pool = pool
    app.state.sim_cache = None
    app.state.sim_factory_ref = factory_ref
    app.state.replay_duckdb = duck
    return app


@pytest.fixture()
def patch_resolver(monkeypatch):
    async def _fake_resolve_game_state(conn, game_pk, **kwargs):
        return _small_game_state()

    monkeypatch.setattr(games_mod, "resolve_game_state", _fake_resolve_game_state)
    return _fake_resolve_game_state


# ===========================================================================
# 3) Endpoints end-to-end: /simulate persists, then /linescore + /decisions +
#    /card + /state (fielders) + /boxscore read.
# ===========================================================================


class TestCardAndBoxscoreEndpoints:
    def test_simulate_then_linescore_decisions_card(self, patch_resolver, duck_con):
        app = _build_app(pool=_LineupPool(), duck=duck_con)
        client = TestClient(app)

        sim = client.get("/api/games/745001/simulate?n_iterations=3&base_seed=42")
        assert sim.status_code == 200, sim.text

        # /linescore -- a numpy-free Linescore with the inning grid and its shape.
        ls = client.get("/api/games/745001/linescore")
        assert ls.status_code == 200, ls.text
        lbody = ls.json()
        for key in (
            "innings",
            "away_runs",
            "home_runs",
            "away_hits",
            "home_hits",
            "away_errors",
            "home_errors",
            "n_innings",
            "away_by_inning",
            "home_by_inning",
        ):
            assert key in lbody
        assert lbody["n_innings"] == len(lbody["innings"]) >= 1
        json.dumps(lbody)

        # /decisions -- the W/L/S payload.
        dec = client.get("/api/games/745001/decisions")
        assert dec.status_code == 200, dec.text
        dbody = dec.json()
        for key in (
            "winning_pitcher_id",
            "losing_pitcher_id",
            "save_pitcher_id",
            "home_score",
            "away_score",
        ):
            assert key in dbody
        json.dumps(dbody)

        # /card -- both at once.
        card = client.get("/api/games/745001/card")
        assert card.status_code == 200, card.text
        cbody = card.json()
        assert cbody["game_pk"] == 745001
        assert cbody["linescore"]["n_innings"] == lbody["n_innings"]
        assert cbody["decisions"]["home_score"] == dbody["home_score"]

    def test_simulate_populates_fielders_in_state(self, patch_resolver, duck_con):
        """The stored per-pitch StateAtPitch carries the nine fielders, so /state returns them.

        The fielders come from the defense map that resolve_lineup builds."""
        app = _build_app(pool=_LineupPool(), duck=duck_con)
        client = TestClient(app)
        client.get("/api/games/745050/simulate?n_iterations=2&base_seed=1")

        plays = client.get("/api/games/745050/plays").json()
        e0 = plays["entries"][0]
        st = client.get(f"/api/games/745050/state/{e0['at_bat']}/{e0['pitch']}")
        assert st.status_code == 200, st.text
        positions = st.json()["field"]["positions"]
        # All nine slots are present, and each one holds a player.
        assert set(positions.keys()) == {"P", "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF"}
        populated = [p for p in positions.values() if p is not None]
        assert len(populated) == 9, positions
        # Each populated slot is a PlayerRef shape.
        assert all("player_id" in p for p in populated)

    def test_boxscore_returns_per_player_prop_means(self, patch_resolver):
        """/boxscore returns each player's prop means (numpy-free)."""
        app = _build_app(pool=_LineupPool(), duck=None)
        client = TestClient(app)
        resp = client.get("/api/games/745001/boxscore?n_iterations=4&base_seed=7")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["n_iterations"] == 4
        assert body["players"], "expected at least one player row"
        # The pitcher (201) gets K/BB/ER/OUTS means; a row exposes a means map.
        any_row = next(iter(body["players"].values()))
        assert "player_id" in any_row and "means" in any_row
        assert all(isinstance(v, int | float) for v in any_row["means"].values())
        # The pitcher of the resolved state (201) should be present with K mean.
        assert "201" in body["players"]
        assert "K" in body["players"]["201"]["means"]
        json.dumps(body)

    # ---- 404 / 503 paths --------------------------------------------------

    def test_linescore_nothing_persisted_is_404(self, duck_con):
        app = _build_app(pool=_LineupPool(), duck=duck_con)
        client = TestClient(app)
        assert client.get("/api/games/999/linescore").status_code == 404
        assert client.get("/api/games/999/decisions").status_code == 404
        assert client.get("/api/games/999/card").status_code == 404

    def test_card_no_store_is_503(self):
        app = _build_app(pool=_LineupPool(), duck=None)
        client = TestClient(app)
        assert client.get("/api/games/745001/linescore").status_code == 503
        assert client.get("/api/games/745001/decisions").status_code == 503
        assert client.get("/api/games/745001/card").status_code == 503


# ===========================================================================
# 4) Best-effort: a missing DuckDB store never breaks /simulate; the card persist
#    failure is swallowed.
# ===========================================================================


class TestPersistenceBestEffort:
    def test_simulate_ok_without_duckdb(self, patch_resolver):
        app = _build_app(pool=_LineupPool(), duck=None)
        client = TestClient(app)
        sim = client.get("/api/games/745001/simulate?n_iterations=3&base_seed=42")
        assert sim.status_code == 200, sim.text
        assert sim.json()["summary"]["n_iterations"] == 3
        # No store -> /card 503 (documented "no replay store wired").
        assert client.get("/api/games/745001/card").status_code == 503
