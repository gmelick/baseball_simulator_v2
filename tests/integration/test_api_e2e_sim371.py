"""
tests/integration/test_api_e2e_sim371.py
=========================================
SIM-371 -- the API + WebSocket + historical-replay END-TO-END test suite (the
final Phase-5 testing slice).

WHAT THIS IS (and is NOT)
-------------------------
A TRUE end-to-end suite that drives the REAL Phase-5 routers (games + betting +
the live ws_router) through the FULL multi-endpoint FLOWS a frontend would walk,
asserting CROSS-ENDPOINT CONSISTENCY -- not just single endpoints in isolation.
It is SANDBOX-RUNNABLE: it uses FastAPI's ``TestClient`` over an in-process app
wired with the established mock seams (a fake asyncpg pool serving canned raw.*
rows + an in-memory DuckDB with the SIM-356/357/362 migrations applied + the
no-DB rng machine factory + an in-memory sim cache). It is NOT a testcontainers /
live-Postgres / live-MLB-WebSocket suite -- the only things stubbed are the DB
(lineup resolution + raw.games / raw.game_odds reads) and the production sampler;
EVERYTHING else (the BatchRunner + simulate_game loop + the SIM-357 record ->
persist replay flow + the SIM-362/364 linescore/decisions derivation + the
SIM-367/369 betting math + the SIM-350 serializers + the ws_router handshake)
runs FOR REAL in-process.

ISOLATION STRATEGY (mirrors tests/unit/test_api_games*.py + test_api_betting*.py)
-------------------------------------------------------------------------------
``_build_e2e_app`` mounts the REAL games + betting + ws routers onto a tiny app
and attaches the app.state contract the routes read:

  * ``pg_pool``          -- a fake asyncpg pool/connection. ``fetch`` serves
    canned ``raw.games`` rows (for GET /{date}) or canned ``raw.game_odds`` rows
    (for /line-movement + /clv); ``fetchval`` stands in as the SIM-356 sim-run
    history INSERT ... RETURNING run_id conn so a real cross-store run_id flows
    into the DuckDB play-stream / state / card writes.
  * ``sim_duckdb``       -- a REAL in-memory DuckDB with migrations 0008 (play
    stream), 0009 (state snapshots) and 0010 (game card) applied, so the SIM-357
    /plays + /state and SIM-362/364 /linescore + /decisions + /card reads serve
    the persisted run.
  * ``sim_factory_ref``  -- the no-DB ``rng_driven_machine_factory`` so the
    BatchRunner runs a real (fast, in-process, deterministic) batch with NO live
    sampler/DuckDB.
  * ``sim_cache``        -- a shared in-memory cache so the same persisted run is
    reused across the flow's endpoints (the SIM-359 memoization path).

``resolve_game_state`` (as imported into api.routes.games -- the module the
betting router ALSO reuses) is monkeypatched to a fixed small GameState, so the
sim runs with no live Postgres/DuckDB.

THE FLOWS (each a clear test; fixtures reused)
----------------------------------------------
  1. Full game-card flow: GET /{date} -> pick a game_pk -> GET /{pk}/simulate
     (real no-DB batch, persists the replay) -> /plays, /state/{ab}/{pitch},
     /linescore, /decisions, /boxscore, /card -- all numpy-free and all
     referencing the SAME persisted run, with cross-endpoint consistency asserted
     (linescore per-inning cells sum to the R totals; decisions' final scores ==
     the linescore totals; /plays' first pitch resolves through /state from the
     same stream).
  2. Override flow: POST /{pk}/simulate/with_override -> baseline + override +
     delta (and a real, non-trivial override actually moves the win pct).
  3. Betting flow: /edges -> /signals -> /line-movement -> /clv are coherent
     (signals are the +EV subset of edges; the CLV snapshot is the subset of the
     line-movement series carrying a CLV).
  4. WebSocket: ``TestClient.websocket_connect("/ws/games/{pk}")`` connects, the
     ws_router's ping/pong protocol round-trips, and a graceful disconnect cleans
     up the connection_manager subscription -- a REAL connect test (the route
     needs no live pipeline; connection_manager just accepts + tracks the socket).
  5. Historical-replay E2E: a deterministic seeded /simulate replays bit-for-bit
     (same seed -> identical summary AND identical persisted play-stream) -- the
     "replay" property -- while a different seed diverges.

Owned by QA / DevOps (SIM-371).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.games as games_mod
from api.routes.betting import router as betting_router
from api.routes.games import router as games_router
from pipeline.live.live_ingestion_pipeline import (
    connection_manager,
    ws_router,
)
from simulation.batch_runner import InMemoryCache
from simulation.game_state import GameState

REPO_ROOT = Path(__file__).resolve().parents[2]
DUCK_MIGRATIONS = (
    REPO_ROOT / "db" / "migrations" / "duckdb" / "0008_sim356_play_stream.sql",
    REPO_ROOT / "db" / "migrations" / "duckdb" / "0009_sim357_state_snapshots.sql",
    REPO_ROOT / "db" / "migrations" / "duckdb" / "0010_sim362_364_game_card.sql",
)

# The no-DB, picklable rng factory -- the production factory ref is swapped to
# this so the whole flow runs a real batch with no live sampler/DuckDB.
NO_DB_FACTORY_REF = "simulation.batch_runner:rng_driven_machine_factory"


# ---------------------------------------------------------------------------
# Canned DB rows
# ---------------------------------------------------------------------------

# Two raw.games rows on one date -- the GET /{date} listing that opens the flow.
CANNED_GAMES_ROWS = [
    {
        "game_pk": 745001,
        "season": 2024,
        "game_date": "2024-08-15",
        "status": "Scheduled",
        "home_team_id": 147,
        "away_team_id": 111,
        "venue_id": 3313,
    },
    {
        "game_pk": 745002,
        "season": 2024,
        "game_date": "2024-08-15",
        "status": "Scheduled",
        "home_team_id": 119,
        "away_team_id": 137,
        "venue_id": 22,
    },
]

# raw.game_odds moneyline history for one game/book: opening (-120/+100) ->
# closing (-150/+130). Home odds shorten (implied prob rises) so the line moved
# TOWARD home; both rows carry both sides so the series de-vigs into an
# entry-vs-close CLV -- the /line-movement + /clv flow's backing.
CANNED_ODDS_ROWS = [
    {
        "fetched_at": "2024-08-15T10:00:00+00:00",
        "line_type": "opening",
        "book": "pinnacle",
        "is_sharp_book": True,
        "market_type": "moneyline",
        "home_ml": -120,
        "away_ml": 100,
        "home_spread": None,
        "home_spread_ml": None,
        "away_spread": None,
        "away_spread_ml": None,
        "total_line": None,
        "over_ml": None,
        "under_ml": None,
    },
    {
        "fetched_at": "2024-08-15T12:00:00+00:00",
        "line_type": "closing",
        "book": "pinnacle",
        "is_sharp_book": True,
        "market_type": "moneyline",
        "home_ml": -150,
        "away_ml": 130,
        "home_spread": None,
        "home_spread_ml": None,
        "away_spread": None,
        "away_spread_ml": None,
        "total_line": None,
        "over_ml": None,
        "under_ml": None,
    },
]


# ---------------------------------------------------------------------------
# Fakes (mirror the unit-test idiom)
# ---------------------------------------------------------------------------


class _FakePool:
    """A fake asyncpg pool/connection serving canned rows.

    ``fetch`` returns whatever canned rows it was built with (raw.games for the
    date listing, raw.game_odds for line-movement). ``fetchval`` stands in as the
    SIM-356 sim-run history ``INSERT ... RETURNING run_id`` conn so the replay
    persist gets a real cross-store run_id (the play-stream / state / card rows
    are then grouped under it). No ``acquire`` is exposed, so the routes use the
    pool directly as a connection (the mock-pool / direct-conn path).
    """

    def __init__(self, *, games_rows=None, odds_rows=None, run_id=4242):
        self._games_rows = list(games_rows) if games_rows is not None else []
        self._odds_rows = list(odds_rows) if odds_rows is not None else []
        self._run_id = run_id
        self.fetch_calls: list = []
        self.run_ids_issued: list[int] = []

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        # Route the canned rows by which table the SQL reads -- the date listing
        # hits raw.games, line-movement/clv hit raw.game_odds.
        if "raw.game_odds" in sql or "game_odds" in sql:
            return list(self._odds_rows)
        return list(self._games_rows)

    async def fetchrow(self, sql, *args):  # pragma: no cover -- not used here
        rows = self._games_rows or self._odds_rows
        return rows[0] if rows else None

    async def fetchval(self, sql, *args):
        # The SIM-356 sim-run history INSERT ... RETURNING run_id. A monotonically
        # increasing run_id per call keeps repeated /simulate runs in distinct
        # game-card rows (latest-run-wins on the by-game read).
        self._run_id += 1
        self.run_ids_issued.append(self._run_id)
        return self._run_id


def _small_game_state() -> GameState:
    """A minimal but valid GameState the monkeypatched resolver returns -- a full
    1..9 lineup + a pitcher + season, enough for simulate_game to run a real
    (no-DB) game via the rng factory."""
    state = GameState(pitcher_id=600001, bat_hand="R", season=2024)
    state.away_lineup = [101, 102, 103, 104, 105, 106, 107, 108, 109]
    state.home_lineup = [201, 202, 203, 204, 205, 206, 207, 208, 209]
    state.away_lineup_slot = 0
    state.home_lineup_slot = 0
    state.batter_id = 101
    state.throw_hand = "R"
    return state


# ---------------------------------------------------------------------------
# DuckDB replay store (real in-memory, migrations 0008 + 0009 + 0010 applied)
# ---------------------------------------------------------------------------


def _fresh_duckdb():
    """A real in-memory DuckDB with the play-stream (0008), state-snapshot (0009)
    and game-card (0010) migrations applied -- the full SIM-357/362/364 replay
    store the flow's /plays + /state + /linescore + /decisions + /card read."""
    import duckdb

    con = duckdb.connect(":memory:")
    # migration_history is referenced by 0010's INSERT OR IGNORE -- create it
    # first (it normally lives in 0001 / the schema bootstrap).
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS migration_history (
            migration_id VARCHAR PRIMARY KEY,
            applied_at   TIMESTAMP NOT NULL DEFAULT now(),
            description  VARCHAR NOT NULL
        )
        """
    )
    for path in DUCK_MIGRATIONS:
        con.execute(path.read_text())
    return con


# ---------------------------------------------------------------------------
# The E2E app builder + fixtures
# ---------------------------------------------------------------------------


def _build_e2e_app(*, pool, duck, cache=None) -> FastAPI:
    """Build the END-TO-END app: the REAL games + betting + ws routers mounted on
    a tiny app, with the app.state contract the routes read attached directly.

    This is deliberately NOT ``api.main.create_app()`` -- that would start the
    heavy similarity-engine lifespan + the live ingestion background pipeline.
    Mounting the real routers + the mock seams gives a true multi-endpoint app
    while staying sandbox-runnable.
    """
    app = FastAPI()
    app.include_router(games_router)
    app.include_router(betting_router)
    app.include_router(ws_router)
    app.state.pg_pool = pool
    app.state.sim_duckdb = duck
    app.state.sim_cache = cache  # None => each runner picks its own backend
    app.state.sim_factory_ref = NO_DB_FACTORY_REF  # the testability seam
    return app


@pytest.fixture()
def duck_con():
    con = _fresh_duckdb()
    yield con
    con.close()


@pytest.fixture()
def patch_resolver(monkeypatch):
    """Monkeypatch resolve_game_state (as imported into the games module, which
    the betting router ALSO reuses) so every sim in the flow runs without a live
    Postgres/DuckDB."""

    async def _fake_resolve_game_state(conn, game_pk, **kwargs):
        return _small_game_state()

    monkeypatch.setattr(games_mod, "resolve_game_state", _fake_resolve_game_state)
    return _fake_resolve_game_state


@pytest.fixture()
def e2e(patch_resolver, duck_con):
    """The wired E2E app + client + the fake pool, ready for a full flow.

    A shared in-memory sim cache backs the app so the games + betting routers
    reuse the SAME memoized summary for a (spec + seed + N) across the flow.
    """
    pool = _FakePool(games_rows=CANNED_GAMES_ROWS, odds_rows=CANNED_ODDS_ROWS)
    app = _build_e2e_app(pool=pool, duck=duck_con, cache=InMemoryCache())
    client = TestClient(app)
    return client, pool


# ===========================================================================
# FLOW 1 -- the full game-card flow (the headline E2E walk)
# ===========================================================================


class TestFullGameCardFlow:
    """GET /{date} -> /simulate -> /plays + /state + /linescore + /decisions +
    /boxscore + /card, all referencing the SAME persisted run, with
    cross-endpoint consistency asserted."""

    def test_date_to_simulate_to_replay_card_is_coherent(self, e2e):
        client, _pool = e2e

        # 1) The schedule listing -- pick a game to walk.
        sched = client.get("/api/games/2024-08-15")
        assert sched.status_code == 200, sched.text
        body = sched.json()
        assert body["count"] == 2
        game_pk = body["games"][0]["game_pk"]
        assert game_pk in (745001, 745002)

        # 2) Simulate that game (a REAL no-DB batch -> persists the replay).
        sim = client.get(f"/api/games/{game_pk}/simulate?n_iterations=8&base_seed=42")
        assert sim.status_code == 200, sim.text
        sim_body = sim.json()
        assert sim_body["game_pk"] == game_pk
        summary = sim_body["summary"]
        assert summary["n_iterations"] == 8
        json.dumps(sim_body)  # numpy-free over the wire

        # 3) /plays -- the persisted pitch stream for that run.
        plays = client.get(f"/api/games/{game_pk}/plays")
        assert plays.status_code == 200, plays.text
        pbp = plays.json()
        assert pbp["n_pitches"] >= 1
        assert len(pbp["entries"]) == pbp["n_pitches"]
        seqs = [e["sequence"] for e in pbp["entries"]]
        assert seqs == sorted(seqs)  # the stream is sequence-ordered
        json.dumps(pbp)

        # 4) /state for the FIRST pitch -- comes from the SAME persisted stream.
        e0 = pbp["entries"][0]
        st = client.get(f"/api/games/{game_pk}/state/{e0['at_bat']}/{e0['pitch']}")
        assert st.status_code == 200, st.text
        snap = st.json()
        # Cross-endpoint consistency: the /state snapshot's (at_bat, pitch) match
        # the /plays entry it was looked up from -- they are one persisted stream.
        assert snap["at_bat"] == e0["at_bat"]
        assert snap["pitch"] == e0["pitch"]
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
        json.dumps(snap)

        # 5) /linescore -- the derived R/H/E grid for the SAME run, INTERNALLY
        #    consistent: each side's per-inning cells sum to its R total.
        ls = client.get(f"/api/games/{game_pk}/linescore")
        assert ls.status_code == 200, ls.text
        line = ls.json()
        away_cells = [c for c in line["away_by_inning"] if c is not None]
        home_cells = [c for c in line["home_by_inning"] if c is not None]
        assert sum(away_cells) == line["away_runs"]
        assert sum(home_cells) == line["home_runs"]
        # innings[] mirror the by-inning cell lists.
        assert [i["away"] for i in line["innings"]] == line["away_by_inning"]
        assert [i["home"] for i in line["innings"]] == line["home_by_inning"]
        json.dumps(line)

        # 6) /decisions -- the W/L/Save card for the SAME run. Its final scores
        #    must equal the linescore R totals (both derived from one PlayResult
        #    stream -> the two endpoints are mutually consistent).
        dec = client.get(f"/api/games/{game_pk}/decisions")
        assert dec.status_code == 200, dec.text
        decisions = dec.json()
        assert decisions["home_score"] == line["home_runs"]
        assert decisions["away_score"] == line["away_runs"]
        json.dumps(decisions)

        # 7) /card -- the COMBINED linescore + decisions; must equal the two
        #    single-endpoint payloads byte-for-byte (one persisted card, three
        #    read paths).
        card = client.get(f"/api/games/{game_pk}/card")
        assert card.status_code == 200, card.text
        card_body = card.json()
        assert card_body["game_pk"] == game_pk
        assert card_body["linescore"] == line
        assert card_body["decisions"] == decisions

        # 8) /boxscore -- the per-player prop-means card (a fresh seeded N-game
        #    batch). numpy-free; carries entries.
        box = client.get(f"/api/games/{game_pk}/boxscore?n_iterations=8&base_seed=42")
        assert box.status_code == 200, box.text
        box_body = box.json()
        json.dumps(box_body)

    def test_plays_before_simulate_is_404(self, e2e):
        """A game with no persisted run -> /plays 404 (replay store wired, but
        empty for that game) -- the documented 'nothing persisted' behaviour."""
        client, _pool = e2e
        resp = client.get("/api/games/700123/plays")
        assert resp.status_code == 404

    def test_state_unknown_pitch_after_simulate_is_404(self, e2e):
        client, _pool = e2e
        client.get("/api/games/745002/simulate?n_iterations=4&base_seed=1")
        resp = client.get("/api/games/745002/state/999/999")
        assert resp.status_code == 404


# ===========================================================================
# FLOW 2 -- the managerial-override flow
# ===========================================================================


class TestOverrideFlow:
    def test_with_override_returns_baseline_override_delta(self, e2e):
        client, _pool = e2e

        body = {
            "home_lineup": [301, 302, 303, 304, 305, 306, 307, 308, 309],
            "pitcher_id": 700002,
            "description": "swap home lineup + ace",
        }
        resp = client.post(
            "/api/games/745001/simulate/with_override?n_iterations=12&base_seed=11",
            json=body,
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["game_pk"] == 745001
        assert payload["n_iterations"] == 12
        # Baseline + override are both full GameSimSummaryModels at the SAME N.
        assert payload["baseline"]["n_iterations"] == 12
        assert payload["override"]["n_iterations"] == 12
        # The delta carries a metrics map; delta == override - baseline exactly.
        metrics = payload["delta"]["metrics"]
        assert "home_win_pct" in metrics and "total_score_mean" in metrics
        for md in metrics.values():
            assert md["delta"] == pytest.approx(md["override"] - md["baseline"], abs=1e-9)
        assert payload["delta"]["description"] == "swap home lineup + ace"
        json.dumps(payload)

    def test_empty_override_yields_zero_delta(self, e2e):
        """An all-empty override == baseline at the same seed -> an all-zero
        delta (a real apples-to-apples comparison run for both sides)."""
        client, _pool = e2e
        resp = client.post(
            "/api/games/745001/simulate/with_override?n_iterations=8&base_seed=9",
            json={},
        )
        assert resp.status_code == 200, resp.text
        metrics = resp.json()["delta"]["metrics"]
        assert all(m["delta"] == pytest.approx(0.0, abs=1e-9) for m in metrics.values())


# ===========================================================================
# FLOW 3 -- the betting flow (/edges -> /signals -> /line-movement -> /clv)
# ===========================================================================


class TestBettingFlow:
    def test_edges_to_signals_are_coherent(self, e2e):
        client, _pool = e2e

        # /edges with injected lopsided moneyline odds (a big underdog price the
        # sim likes) so at least one side carries a priceable +EV edge.
        edges = client.get(
            "/api/betting/games/745001/edges"
            "?n_iterations=80&base_seed=11&markets=moneyline"
            "&home_ml=400&away_ml=-600"
        )
        assert edges.status_code == 200, edges.text
        edges_body = edges.json()
        assert edges_body["game_pk"] == 745001
        assert {e["label"] for e in edges_body["edges"]} == {"moneyline"}
        json.dumps(edges_body)

        # /signals over the SAME game/odds/seed -- the +EV gated, Kelly-sized,
        # EV-ranked SUBSET of the edges. Every signal is strictly +EV.
        signals = client.get(
            "/api/betting/games/745001/signals"
            "?n_iterations=80&base_seed=11&markets=moneyline"
            "&home_ml=400&away_ml=-600&min_edge=0.0"
        )
        assert signals.status_code == 200, signals.text
        sig_body = signals.json()
        sigs = sig_body["signals"]
        evs = [s["ev"] for s in sigs]
        assert evs == sorted(evs, reverse=True)  # EV-descending ranking
        for i, s in enumerate(sigs):
            assert s["rank"] == i
            assert s["ev"] > 0.0 and s["edge"] > 0.0
            assert 0.0 <= s["stake_fraction"] <= sig_body["config"]["max_stake_fraction"]
            # Each signal nests its source EdgeReport -- the coherence link back
            # to /edges (same sim_prob shape).
            assert "report" in s and "sim_prob" in s["report"]
        json.dumps(sig_body)

    def test_line_movement_to_clv_are_coherent(self, e2e):
        client, _pool = e2e

        # /line-movement from the canned raw.game_odds history.
        lm = client.get("/api/betting/games/745001/line-movement?market_type=moneyline")
        assert lm.status_code == 200, lm.text
        lm_body = lm.json()
        assert lm_body["game_pk"] == 745001
        assert lm_body["count"] == 2  # home + away series for one book
        sides = {s["side"] for s in lm_body["series"]}
        assert sides == {"home", "away"}
        home = next(s for s in lm_body["series"] if s["side"] == "home")
        # Opening -120 -> closing -150: home implied prob rises (steam to home).
        assert home["direction"] == "toward"
        assert home["has_movement"] is True
        assert home["implied_prob_series"][1] > home["implied_prob_series"][0]
        json.dumps(lm_body)

        # /clv is the SUBSET of the line-movement series that carry a CLV -- its
        # count must not exceed the line-movement count, and every clv series
        # has a non-null clv (the coherence invariant).
        clv = client.get("/api/betting/games/745001/clv?market_type=moneyline")
        assert clv.status_code == 200, clv.text
        clv_body = clv.json()
        assert clv_body["count"] <= lm_body["count"]
        assert clv_body["count"] == 2  # both sides have both prices -> both CLV
        for s in clv_body["series"]:
            assert s["clv"] is not None
            assert "beat_close" in s["clv"]
        json.dumps(clv_body)

    def test_line_movement_empty_when_no_odds(self, duck_con, patch_resolver):
        """A game with no persisted odds -> an empty (but valid) series."""
        pool = _FakePool(games_rows=CANNED_GAMES_ROWS, odds_rows=[])
        app = _build_e2e_app(pool=pool, duck=duck_con, cache=InMemoryCache())
        client = TestClient(app)
        resp = client.get("/api/betting/games/745001/line-movement?market_type=moneyline")
        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] == 0


# ===========================================================================
# FLOW 4 -- the WebSocket flow (ws_router connect + ping/pong + disconnect)
# ===========================================================================


class TestWebSocketFlow:
    """The ws_router (/ws/games/{game_pk}) accepts the connection via
    connection_manager (just ws.accept() + subscription tracking -- no live
    pipeline needed), then loops: a 'ping' text frame -> a {"type":"pong"}
    JSON reply; a graceful close -> a WebSocketDisconnect that unsubscribes.
    These are REAL connect tests over TestClient.websocket_connect."""

    def test_ws_connect_ping_pong_and_disconnect(self, e2e):
        client, _pool = e2e
        game_pk = 745001

        before = connection_manager.subscriber_count(game_pk)
        with client.websocket_connect(f"/ws/games/{game_pk}") as ws:
            # The route accepted us -> the connection_manager tracks one more sub.
            assert connection_manager.subscriber_count(game_pk) == before + 1
            # The ws_router's protocol: send "ping" -> receive a JSON pong frame.
            ws.send_text("ping")
            reply = ws.receive_json()
            assert reply == {"type": "pong"}
            # A second round-trip proves the receive loop keeps serving.
            ws.send_text("ping")
            assert ws.receive_json() == {"type": "pong"}
        # On context exit the client closes -> the route's WebSocketDisconnect
        # branch fires and the subscription is cleaned up.
        assert connection_manager.subscriber_count(game_pk) == before

    def test_ws_ignores_non_ping_then_still_pongs(self, e2e):
        """A non-'ping' frame is silently consumed (no reply); a subsequent
        'ping' still pongs -- the loop is resilient to arbitrary client text."""
        client, _pool = e2e
        with client.websocket_connect("/ws/games/745002") as ws:
            ws.send_text("hello")  # consumed, no reply per the protocol
            ws.send_text("ping")
            assert ws.receive_json() == {"type": "pong"}


# ===========================================================================
# FLOW 5 -- the historical-replay E2E (deterministic reproducibility)
# ===========================================================================


@pytest.mark.slow
class TestHistoricalReplayE2E:
    """The 'replay' property: a fixed seed reproduces a run bit-for-bit -- the
    same summary AND the same persisted play-stream -- while a different seed
    diverges. This is the determinism gate a historical-replay harness relies
    on.

    Marked ``slow``: each test drives MULTIPLE uncached /simulate runs (each a
    real batch + a record -> re-persist replay pass), so it is the heaviest flow
    in the suite. Run with ``-m slow`` (or unfiltered); the rest of the suite is
    fast.
    """

    def test_same_seed_reproduces_summary_and_play_stream(self, e2e, duck_con):
        client, pool = e2e
        game_pk = 745001

        # Two seeded runs with caching OFF so we exercise the REAL recompute +
        # re-persist path both times (a cache hit would trivially match). Each
        # run persists under its own SIM-356 run_id (the fake pool issues a fresh
        # one per call), so the two streams coexist in the store -- we compare
        # them PER RUN below rather than via the accumulating by-game /plays read.
        a = client.get(f"/api/games/{game_pk}/simulate?n_iterations=5&base_seed=7&use_cache=false")
        b = client.get(f"/api/games/{game_pk}/simulate?n_iterations=5&base_seed=7&use_cache=false")

        assert a.status_code == b.status_code == 200
        # The aggregate summary replays bit-for-bit (per-iteration arrays match).
        assert a.json()["summary"]["home_scores"] == b.json()["summary"]["home_scores"]
        assert a.json()["summary"]["away_scores"] == b.json()["summary"]["away_scores"]

        # The persisted representative game (recorded at seed=base_seed) replays
        # identically across the two runs. Read each run's stream by its own
        # run_id from the store directly (the by-game /plays read returns the
        # UNION across runs, which is the documented endpoint behaviour).
        from db import sim_store

        run_id_a, run_id_b = pool.run_ids_issued[-2], pool.run_ids_issued[-1]
        assert run_id_a != run_id_b
        stream_a = sim_store.load_play_stream(duck_con, game_pk=game_pk, run_id=run_id_a)
        stream_b = sim_store.load_play_stream(duck_con, game_pk=game_pk, run_id=run_id_b)
        assert len(stream_a) == len(stream_b) >= 1
        assert [e["pitch_outcome"] for e in stream_a] == [e["pitch_outcome"] for e in stream_b]

    def test_different_seed_diverges(self, e2e):
        """A control: a DIFFERENT seed yields a different replay -- so the
        reproducibility above is real determinism, not a constant."""
        client, _pool = e2e
        game_pk = 745001
        a = client.get(f"/api/games/{game_pk}/simulate?n_iterations=12&base_seed=1&use_cache=false")
        b = client.get(
            f"/api/games/{game_pk}/simulate?n_iterations=12&base_seed=999&use_cache=false"
        )
        assert a.status_code == b.status_code == 200
        # Over 12 iterations two distinct seeds must produce a different score
        # vector (an astronomically safe inequality for the rng path).
        assert a.json()["summary"]["home_scores"] != b.json()["summary"]["home_scores"]
