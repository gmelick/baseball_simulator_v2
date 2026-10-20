"""The /plays and /state replay routes of api/routes/games.py, and the replay store.

WHAT'S COVERED
--------------
  1. The state-snapshot store of db/sim_store.py, on a real in-memory DuckDB with
     the replay schema. store_state_snapshots then load_state_at returns the right
     snapshot for an (at_bat, pitch), and None for a missing one.
     load_state_snapshots returns the whole stream in order. An empty list writes
     nothing. A snapshot without a required key raises.
  2. The two routes end to end, on a small FastAPI app with only the games router.
     The app has a fake pg pool, an in-memory DuckDB on ``app.state.replay_duckdb``
     and the no-DB factory. GET /simulate records the game. Then GET /plays returns
     a numpy-free PlayByPlay, and GET /state/{at_bat}/{pitch} returns the snapshot.
     Both routes answer 404 when nothing is stored or the pitch is unknown, and 503
     with no replay store.
  3. The recording is best effort. With no replay store, /simulate still answers
     200. A failed run-history write does not break /simulate either.

ISOLATION
---------
The tests follow test_api_games.py: a mock app, a fake pool and the patch_resolver
fixture. So the similarity-engine lifespan and the production sampler never run.
The one addition is an in-memory DuckDB with the replay schema, attached as
``app.state.replay_duckdb``.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.games as games_mod
from api.routes.games import router as games_router
from db import sim_store
from simulation.game_state import GameState

# The no-DB, picklable factory. The app's factory ref points here, so /simulate
# (and the recorded representative game) run over the in-memory synthetic bundle.
NO_DB_FACTORY_REF = "simulation.batch_runner:rng_driven_machine_factory"


# ---------------------------------------------------------------------------
# DuckDB fixtures + helpers
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
# Fakes (as in test_api_games.py)
# ---------------------------------------------------------------------------


class _FakePool:
    """A fake asyncpg pool and connection.

    The tests monkeypatch resolve_game_state, but a pool must be present, or
    _get_pool answers 503. fetchval, fetchrow and fetch also let the fake stand
    in for the run-history connection."""

    def __init__(self, rows=None):
        self._rows = rows if rows is not None else []
        self.store_calls: list = []

    async def fetch(self, sql, *args):
        return self._rows

    async def fetchrow(self, sql, *args):  # pragma: no cover
        return self._rows[0] if self._rows else None

    async def fetchval(self, sql, *args):
        # Stands in for the run-history INSERT ... RETURNING run_id connection.
        self.store_calls.append((sql, args))
        return 4242


def _small_game_state() -> GameState:
    """A minimal but valid GameState the monkeypatched resolver returns."""
    state = GameState(pitcher_id=600001, bat_hand="R", season=2024)
    state.away_lineup = [101, 102, 103, 104, 105, 106, 107, 108, 109]
    state.home_lineup = [201, 202, 203, 204, 205, 206, 207, 208, 209]
    state.away_lineup_slot = 0
    state.home_lineup_slot = 0
    state.batter_id = 101
    state.throw_hand = "R"
    return state


def _build_app(*, pool, duck=None, factory_ref=NO_DB_FACTORY_REF) -> FastAPI:
    """A small app with only the games router and the app.state the routes read."""
    app = FastAPI()
    app.include_router(games_router)
    app.state.pg_pool = pool
    app.state.sim_cache = None
    app.state.sim_factory_ref = factory_ref
    app.state.replay_duckdb = duck  # the replay store (None => skip the recording)
    return app


@pytest.fixture()
def patch_resolver(monkeypatch):
    async def _fake_resolve_game_state(conn, game_pk, **kwargs):
        return _small_game_state()

    monkeypatch.setattr(games_mod, "resolve_game_state", _fake_resolve_game_state)
    return _fake_resolve_game_state


# ===========================================================================
# 1) DuckDB state-snapshot store round-trip (real :memory: duckdb)
# ===========================================================================


def _sample_snapshots() -> list[dict]:
    """A small ordered per-pitch StateAtPitch stream, built through the real contracts.

    Each item has the jsonable dict shape that the route hands store_state_snapshots."""
    from api.serialization import to_jsonable
    from simulation.snapshots import StateAtPitch

    snaps = []
    seq = 0
    for at_bat, pitches in ((0, 3), (1, 1)):
        for pitch in range(1, pitches + 1):
            st = GameState(pitcher_id=1, bat_hand="R", season=2024)
            st.batter_id = 100 + at_bat
            st.balls = pitch - 1
            sap = StateAtPitch.from_game_state(st, at_bat=at_bat, pitch=pitch, sequence=seq)
            snaps.append(to_jsonable(sap))
            seq += 1
    return snaps


class TestStateSnapshotStore:
    def test_store_then_load_at_roundtrip(self, duck_con):
        snaps = _sample_snapshots()
        sim_store.store_state_snapshots(duck_con, game_pk=900, run_id=7, snapshots=snaps)
        # The right snapshot comes back for a given (at_bat, pitch).
        hit = sim_store.load_state_at(duck_con, game_pk=900, at_bat=0, pitch=2, run_id=7)
        assert hit is not None
        assert hit["at_bat"] == 0
        assert hit["pitch"] == 2
        assert hit["sequence"] == 1
        # The full jsonable StateAtPitch is preserved under "snapshot".
        snap = hit["snapshot"]
        assert snap["at_bat"] == 0 and snap["pitch"] == 2
        assert snap["field"]["balls"] == 1  # pitch 2 => 1 ball in the fixture

    def test_load_at_missing_returns_none(self, duck_con):
        sim_store.store_state_snapshots(
            duck_con, game_pk=900, run_id=7, snapshots=_sample_snapshots()
        )
        assert sim_store.load_state_at(duck_con, game_pk=900, at_bat=9, pitch=9, run_id=7) is None

    def test_load_at_without_run_id_uses_latest_run(self, duck_con):
        # Two runs for the same game; the no-run_id lookup returns the latest.
        sim_store.store_state_snapshots(
            duck_con, game_pk=901, run_id=1, snapshots=_sample_snapshots()
        )
        sim_store.store_state_snapshots(
            duck_con, game_pk=901, run_id=2, snapshots=_sample_snapshots()
        )
        hit = sim_store.load_state_at(duck_con, game_pk=901, at_bat=1, pitch=1)
        assert hit is not None
        assert hit["at_bat"] == 1 and hit["pitch"] == 1

    def test_load_snapshots_whole_stream_ordered(self, duck_con):
        snaps = _sample_snapshots()
        sim_store.store_state_snapshots(duck_con, game_pk=902, run_id=3, snapshots=snaps)
        loaded = sim_store.load_state_snapshots(duck_con, game_pk=902, run_id=3)
        assert len(loaded) == len(snaps) == 4
        assert [r["sequence"] for r in loaded] == [0, 1, 2, 3]

    def test_store_empty_is_noop(self, duck_con):
        sim_store.store_state_snapshots(duck_con, game_pk=1, run_id=1, snapshots=[])
        assert sim_store.load_state_snapshots(duck_con, game_pk=1, run_id=1) == []

    def test_missing_required_field_raises(self, duck_con):
        with pytest.raises(KeyError):
            sim_store.store_state_snapshots(
                duck_con,
                game_pk=1,
                run_id=1,
                snapshots=[{"at_bat": 0, "pitch": 1, "field": {}}],  # no sequence
            )


# ===========================================================================
# 2) Endpoints end-to-end: /simulate persists, then /plays + /state read
# ===========================================================================


class TestPlaysAndStateEndpoints:
    def test_simulate_then_plays_and_state(self, patch_resolver, duck_con):
        app = _build_app(pool=_FakePool(), duck=duck_con)
        client = TestClient(app)

        # Drive /simulate at a fixed seed -> persists play-stream + snapshots.
        sim = client.get("/api/games/745001/simulate?n_iterations=3&base_seed=42")
        assert sim.status_code == 200, sim.text

        # /plays returns a numpy-free PlayByPlay.
        plays = client.get("/api/games/745001/plays")
        assert plays.status_code == 200, plays.text
        body = plays.json()
        assert body["n_pitches"] >= 1
        assert len(body["entries"]) == body["n_pitches"]
        # Entry shape (PlayByPlayEntryModel) + ordering by sequence.
        seqs = [e["sequence"] for e in body["entries"]]
        assert seqs == sorted(seqs)
        first = body["entries"][0]
        for key in ("sequence", "at_bat", "pitch", "pitch_outcome", "is_pa_end"):
            assert key in first
        json.dumps(body)  # numpy-free

        # /state for the first pitch returns its snapshot.
        e0 = body["entries"][0]
        st = client.get(f"/api/games/745001/state/{e0['at_bat']}/{e0['pitch']}")
        assert st.status_code == 200, st.text
        snap = st.json()
        assert snap["at_bat"] == e0["at_bat"]
        assert snap["pitch"] == e0["pitch"]
        # The field snapshot carries the BaseballFieldGraphic shape incl. the
        # derived occupied_bases / runners_on the response model requires.
        field = snap["field"]
        for key in (
            "positions",
            "baserunners",
            "balls",
            "strikes",
            "outs",
            "inning",
            "half",
            "occupied_bases",
            "runners_on",
        ):
            assert key in field
        assert isinstance(field["occupied_bases"], list)
        json.dumps(snap)  # numpy-free

    def test_state_unknown_pitch_is_404(self, patch_resolver, duck_con):
        app = _build_app(pool=_FakePool(), duck=duck_con)
        client = TestClient(app)
        client.get("/api/games/745002/simulate?n_iterations=2&base_seed=1")
        resp = client.get("/api/games/745002/state/999/999")
        assert resp.status_code == 404

    def test_plays_nothing_persisted_is_404(self, duck_con):
        """A game with no persisted stream -> /plays 404 (no /simulate run)."""
        app = _build_app(pool=_FakePool(), duck=duck_con)
        client = TestClient(app)
        resp = client.get("/api/games/123456/plays")
        assert resp.status_code == 404

    def test_state_nothing_persisted_is_404(self, duck_con):
        app = _build_app(pool=_FakePool(), duck=duck_con)
        client = TestClient(app)
        resp = client.get("/api/games/123456/state/0/1")
        assert resp.status_code == 404

    def test_plays_no_store_is_503(self):
        """No DuckDB replay store wired -> /plays 503."""
        app = _build_app(pool=_FakePool(), duck=None)
        client = TestClient(app)
        resp = client.get("/api/games/745001/plays")
        assert resp.status_code == 503

    def test_state_no_store_is_503(self):
        app = _build_app(pool=_FakePool(), duck=None)
        client = TestClient(app)
        resp = client.get("/api/games/745001/state/0/1")
        assert resp.status_code == 503


# ===========================================================================
# 3) Best-effort persistence: a missing store never breaks /simulate
# ===========================================================================


class TestPersistenceIsBestEffort:
    def test_simulate_ok_without_duckdb_then_plays_404(self, patch_resolver):
        """With no replay store on app.state, /simulate still answers 200.

        The route skips the recording. A recording failure never breaks the
        /simulate response."""
        app = _build_app(pool=_FakePool(), duck=None)
        client = TestClient(app)

        sim = client.get("/api/games/745001/simulate?n_iterations=3&base_seed=42")
        assert sim.status_code == 200, sim.text
        # The summary still came through fine.
        assert sim.json()["summary"]["n_iterations"] == 3

    def test_simulate_ok_when_history_pool_store_raises(self, patch_resolver, duck_con):
        """A failed run-history write (the pool's fetchval raises) keeps /simulate up.

        The replay file numbers its own runs, so the play stream still lands and
        /plays works."""

        class _AngryPool(_FakePool):
            async def fetchval(self, sql, *args):
                raise RuntimeError("postgres down")

        app = _build_app(pool=_AngryPool(), duck=duck_con)
        client = TestClient(app)

        sim = client.get("/api/games/745099/simulate?n_iterations=2&base_seed=5")
        assert sim.status_code == 200, sim.text
        # The replay stream still persisted, under the replay file's own run id.
        plays = client.get("/api/games/745099/plays")
        assert plays.status_code == 200, plays.text
        assert plays.json()["n_pitches"] >= 1
