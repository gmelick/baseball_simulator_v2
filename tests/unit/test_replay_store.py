"""SIM-561: the game page's linescore and play-by-play show a simulated game.

The replay store lives in a DuckDB file of its own (``REPLAY_DUCKDB_PATH``),
which only the app writes, so the analytics file is never locked. A stored
game's play-by-play carries each pitch's inning, half, outs, batter, pitcher and
the score after it (DuckDB 0032). ``POST /sample-game`` simulates and stores one
game; ``/boxscore`` stores its run's first game; the replay reads serve the
newest complete run, and the store keeps the newest few runs per game.
"""

from __future__ import annotations

import ast
import dataclasses
import itertools
from pathlib import Path

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.games as games_mod
from api.routes.games import router as games_router
from db import sim_store
from simulation.batch_runner import BatchRunner, GameSpec, InMemoryCache
from simulation.game_state import GameState
from simulation.play_recorder import record_game
from simulation.production_factory import (
    _default_bullpen_for_spec,
    placeholder_reliever_number,
)
from simulation.sim_kwargs import sim_kwargs_from_state
from simulation.snapshots import PitchContext, PlayByPlay, thrown_pitches

REPO_ROOT = Path(__file__).resolve().parents[2]
NO_DB_FACTORY_REF = "simulation.batch_runner:rng_driven_machine_factory"
_AWAY_STARTER = 301
_HOME_STARTER = 401
_CONTEXT_FIELDS = (
    "inning",
    "half",
    "outs_before",
    "batter_id",
    "pitcher_id",
    "away_score",
    "home_score",
)


def _state() -> GameState:
    state = GameState(pitcher_id=_HOME_STARTER, bat_hand="R", season=2024)
    state.away_lineup = [101 + i for i in range(9)]
    state.home_lineup = [201 + i for i in range(9)]
    state.batter_id = 101
    state.throw_hand = "R"
    state.away_pitcher_id = _AWAY_STARTER
    state.home_pitcher_id = _HOME_STARTER
    return state


def _kwargs() -> dict:
    return sim_kwargs_from_state(_state())


def _replay_con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    sim_store.ensure_replay_schema(con)
    return con


def _store_fake_run(con, *, game_pk: int, run_id: int, n: int = 3) -> None:
    rows = [{"sequence": i, "at_bat": 0, "pitch": i + 1, "pitch_outcome": "ball"} for i in range(n)]
    sim_store.store_play_stream(con, game_pk=game_pk, run_id=run_id, play_entries=rows)
    sim_store.store_state_snapshots(
        con,
        game_pk=game_pk,
        run_id=run_id,
        snapshots=[{"at_bat": 0, "pitch": 1, "sequence": 0, "field": {}}],
    )
    sim_store.store_game_card(
        con, game_pk=game_pk, run_id=run_id, linescore={"r": run_id}, decisions={}
    )


# ===========================================================================
# 1) The replay file
# ===========================================================================


class TestTheReplayFile:
    def test_the_schema_is_the_replay_migrations_and_is_idempotent(self):
        con = _replay_con()
        sim_store.ensure_replay_schema(con)  # a second call changes nothing
        tables = {
            r[0]
            for r in con.execute(
                "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'sim'"
            ).fetchall()
        }
        assert tables == {"play_stream", "state_snapshots", "game_cards"}
        cols = [
            r[0]
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'play_stream' ORDER BY ordinal_position"
            ).fetchall()
        ]
        assert tuple(cols[-len(_CONTEXT_FIELDS) :]) == _CONTEXT_FIELDS
        assert ["run_id", "game_pk", *sim_store.PLAY_ROW_FIELDS] == cols
        applied = [
            r[0] for r in con.execute("SELECT migration_id FROM migration_history").fetchall()
        ]
        assert sorted(applied) == ["0008", "0009", "0010", "0032"]

    def test_0032_is_a_no_op_on_a_file_without_the_play_stream(self):
        """The analytics file holds no sim.play_stream; 0032 must not fail there."""
        con = duckdb.connect(":memory:")
        con.execute(
            "CREATE TABLE migration_history (migration_id VARCHAR PRIMARY KEY, "
            "applied_at TIMESTAMP NOT NULL DEFAULT now(), description VARCHAR NOT NULL)"
        )
        path = REPO_ROOT / "db" / "migrations" / "duckdb" / "0032_sim561_play_stream_context.sql"
        con.execute(path.read_text(encoding="utf-8"))
        assert con.execute("SELECT count(*) FROM migration_history").fetchone() == (1,)

    def test_open_replay_store_creates_the_file_and_keeps_its_data(self, tmp_path):
        path = str(tmp_path / "replay.duckdb")
        con = sim_store.open_replay_store(path)
        _store_fake_run(con, game_pk=7, run_id=11)
        con.close()
        again = sim_store.open_replay_store(path)
        try:
            assert sim_store.latest_replay_run_id(again, game_pk=7) == 11
        finally:
            again.close()

    def test_the_context_round_trips(self):
        con = _replay_con()
        row = {
            "sequence": 0,
            "at_bat": 0,
            "pitch": 1,
            "pitch_outcome": "in_play",
            "inning": 3,
            "half": "bottom",
            "outs_before": 2,
            "batter_id": 660271,
            "pitcher_id": 543037,
            "away_score": 1,
            "home_score": 4,
        }
        sim_store.store_play_stream(con, game_pk=1, run_id=1, play_entries=[row])
        (back,) = sim_store.load_play_stream(con, game_pk=1, run_id=1)
        assert {f: back[f] for f in _CONTEXT_FIELDS} == {f: row[f] for f in _CONTEXT_FIELDS}

    def test_a_row_without_context_reads_none(self):
        con = _replay_con()
        sim_store.store_play_stream(
            con,
            game_pk=1,
            run_id=1,
            play_entries=[{"sequence": 0, "at_bat": 0, "pitch": 1, "pitch_outcome": "ball"}],
        )
        (back,) = sim_store.load_play_stream(con, game_pk=1, run_id=1)
        assert all(back[f] is None for f in _CONTEXT_FIELDS)

    def test_the_newest_run_is_the_newest_with_a_card(self):
        con = _replay_con()
        _store_fake_run(con, game_pk=5, run_id=3)
        _store_fake_run(con, game_pk=5, run_id=9)
        _store_fake_run(con, game_pk=6, run_id=12)
        # A half-stored run (plays, no card) is not complete.
        sim_store.store_play_stream(
            con,
            game_pk=5,
            run_id=10,
            play_entries=[{"sequence": 0, "at_bat": 0, "pitch": 1, "pitch_outcome": "ball"}],
        )
        assert sim_store.latest_replay_run_id(con, game_pk=5) == 9
        assert sim_store.latest_replay_run_id(con, game_pk=99) is None

    def test_pruning_keeps_the_newest_runs_of_that_game_only(self):
        con = _replay_con()
        for run_id in range(1, 9):
            _store_fake_run(con, game_pk=5, run_id=run_id)
        _store_fake_run(con, game_pk=6, run_id=100)
        dropped = sim_store.prune_replay_runs(con, game_pk=5, keep=5)
        assert dropped == 3
        for table in ("sim.play_stream", "sim.state_snapshots", "sim.game_cards"):
            kept = {
                r[0]
                for r in con.execute(
                    f"SELECT DISTINCT run_id FROM {table} WHERE game_pk = 5"
                ).fetchall()
            }
            assert kept == {4, 5, 6, 7, 8}, table
        assert sim_store.latest_replay_run_id(con, game_pk=6) == 100


# ===========================================================================
# 2) The recorder's context
# ===========================================================================


class TestThePitchContext:
    def _recorded(self, seed: int = 11):
        return record_game(factory_ref=NO_DB_FACTORY_REF, seed=seed, sim_kwargs=_kwargs())

    def test_one_context_per_recorded_play(self):
        game = self._recorded()
        assert len(game.contexts) == len(game.plays) > 0
        assert all(isinstance(c, PitchContext) for c in game.contexts)

    def test_entries_carry_who_and_when(self):
        game = self._recorded()
        entries = PlayByPlay.from_play_results(game.plays, game.contexts).entries
        first = entries[0]
        assert (first.inning, first.half, first.outs_before) == (1, "top", 0)
        assert first.batter_id == 101
        assert first.pitcher_id == _HOME_STARTER
        for e in entries:
            assert e.half in ("top", "bottom")
            if e.half == "top":
                assert 101 <= e.batter_id <= 109
            else:
                assert 201 <= e.batter_id <= 209
            assert 0 <= e.outs_before <= 2

    def test_a_new_plate_appearance_has_the_next_batter(self):
        game = self._recorded()
        entries = PlayByPlay.from_play_results(game.plays, game.contexts).entries
        firsts = [e for e in entries if e.pitch == 1 and e.half == "top" and e.inning == 1]
        assert [e.batter_id for e in firsts][:3] == [101, 102, 103]

    def test_the_last_score_is_the_final_score(self):
        game = self._recorded()
        entries = PlayByPlay.from_play_results(game.plays, game.contexts).entries
        last = entries[-1]
        assert (last.away_score, last.home_score) == (
            game.result.away_score,
            game.result.home_score,
        )
        away = [e.away_score for e in entries]
        assert away == sorted(away)  # a score never goes down

    def test_a_context_list_of_the_wrong_length_is_refused(self):
        game = self._recorded()
        with pytest.raises(ValueError):
            PlayByPlay.from_play_results(game.plays, game.contexts[:-1])

    def test_without_contexts_the_entries_still_build(self):
        game = self._recorded()
        entries = PlayByPlay.from_play_results(game.plays).entries
        assert entries[0].inning is None and entries[0].batter_id is None
        assert entries[0].pitcher_id == _HOME_STARTER  # from the play itself


class TestRecordingOnThePool:
    def test_a_pooled_record_equals_an_in_process_record(self):
        spec = GameSpec(machine_factory=NO_DB_FACTORY_REF, sim_kwargs=_kwargs())
        here = BatchRunner(cache=InMemoryCache(), max_workers=1).record_game(spec, 13)
        runner = BatchRunner(cache=InMemoryCache(), max_workers=2)
        try:
            pooled = runner.record_game(spec, 13)
            assert runner._pool is not None  # it ran on a worker
        finally:
            runner.close()
        assert pooled.contexts == here.contexts
        assert [p.event for p in pooled.plays] == [p.event for p in here.plays]
        assert (pooled.result.away_score, pooled.result.home_score) == (
            here.result.away_score,
            here.result.home_score,
        )


class TestPlaceholderRelievers:
    def test_the_number_reads_back_from_the_id(self):
        pens = _default_bullpen_for_spec(GameSpec(sim_kwargs=_kwargs()))
        for pen in pens.values():
            assert [placeholder_reliever_number(pid) for pid in pen] == [1, 2, 3, 4, 5, 6]

    def test_a_real_id_has_no_number(self):
        assert placeholder_reliever_number(660271) is None
        assert placeholder_reliever_number(0) is None
        assert placeholder_reliever_number(-1) is None  # below the placeholder range
        assert placeholder_reliever_number(-9_000_000) is None


# ===========================================================================
# 3) The routes
# ===========================================================================


class _Pool:
    """A fake asyncpg pool: the game row, players' names, the sim-run history id."""

    def __init__(self):
        self._ids = itertools.count(1)
        self.history: list[tuple] = []

    async def fetch(self, sql, *args):
        if "raw.players" in sql:
            ids = args[0] if args else []
            return [{"player_id": pid, "full_name": f"Player Name {pid}"} for pid in ids]
        return []

    async def fetchrow(self, sql, *args):
        if "raw.games" in sql:
            return {"game_pk": int(args[0]), "season": 2024, "home_team_id": 1, "away_team_id": 2}
        return None

    async def fetchval(self, sql, *args):
        self.history.append(args)
        return next(self._ids)


def _build_app(*, replay=None, pool=None) -> FastAPI:
    app = FastAPI()
    app.include_router(games_router)
    app.state.pg_pool = pool if pool is not None else _Pool()
    app.state.sim_cache = InMemoryCache()
    app.state.sim_factory_ref = NO_DB_FACTORY_REF
    app.state.sim_duckdb = None  # the analytics handle; the replay store is apart
    app.state.replay_duckdb = replay
    return app


@pytest.fixture()
def patch_resolver(monkeypatch):
    async def _fake(conn, game_pk, **kwargs):
        return _state()

    monkeypatch.setattr(games_mod, "resolve_game_state", _fake)


class TestTheSampleGameRoute:
    def test_a_sample_game_fills_the_linescore_and_the_play_by_play(self, patch_resolver):
        replay = _replay_con()
        client = TestClient(_build_app(replay=replay))
        resp = client.post("/api/games/745001/sample-game?base_seed=21")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["base_seed"] == 21 and body["run_id"] == 1

        linescore = client.get("/api/games/745001/linescore")
        assert linescore.status_code == 200, linescore.text

        plays = client.get("/api/games/745001/plays").json()
        assert plays["run_id"] == 1
        first = plays["entries"][0]
        assert (first["inning"], first["half"], first["outs_before"]) == (1, "top", 0)
        assert first["batter_id"] == 101 and first["pitcher_id"] == _HOME_STARTER
        assert plays["names"]["101"] == "Player Name 101"
        assert plays["names"][str(_HOME_STARTER)] == f"Player Name {_HOME_STARTER}"

        # The stored game is the game at that seed.
        recorded = record_game(factory_ref=NO_DB_FACTORY_REF, seed=21, sim_kwargs=_kwargs())
        assert plays["n_pitches"] == len(thrown_pitches(recorded.plays))

    def test_a_sample_game_writes_no_history_row(self, patch_resolver):
        """A one-game summary is not a Monte-Carlo run: sim.sim_runs stays clean."""
        pool = _Pool()
        client = TestClient(_build_app(replay=_replay_con(), pool=pool))
        client.post("/api/games/745001/sample-game?base_seed=3")
        client.get("/api/games/745001/boxscore?n_iterations=2&base_seed=3")
        assert pool.history == []

    def test_simulate_keeps_its_batch_summary_in_the_history(self, patch_resolver):
        pool = _Pool()
        client = TestClient(_build_app(replay=_replay_con(), pool=pool))
        client.get("/api/games/745001/simulate?n_iterations=3&base_seed=4")
        (args,) = pool.history
        assert (args[0], args[1], args[2]) == (745001, 3, 4)

    def test_the_card_and_the_plays_carry_the_run_and_its_seed(self, patch_resolver):
        client = TestClient(_build_app(replay=_replay_con()))
        client.post("/api/games/745001/sample-game?base_seed=31")
        client.post("/api/games/745001/sample-game?base_seed=32")
        card = client.get("/api/games/745001/card").json()
        assert (card["run_id"], card["base_seed"]) == (2, 32)
        newest = client.get("/api/games/745001/plays").json()
        assert (newest["run_id"], newest["base_seed"]) == (2, 32)
        older = client.get("/api/games/745001/plays?run_id=1").json()
        assert (older["run_id"], older["base_seed"]) == (1, 31)
        assert client.get("/api/games/745001/plays?run_id=99").status_code == 404

    def test_a_random_seed_is_reported(self, patch_resolver):
        client = TestClient(_build_app(replay=_replay_con()))
        body = client.post("/api/games/745001/sample-game").json()
        assert isinstance(body["base_seed"], int) and body["base_seed"] >= 0

    def test_the_reads_serve_the_newest_game_only(self, patch_resolver):
        replay = _replay_con()
        client = TestClient(_build_app(replay=replay))
        client.post("/api/games/745001/sample-game?base_seed=21")
        client.post("/api/games/745001/sample-game?base_seed=22")
        plays = client.get("/api/games/745001/plays").json()
        assert plays["run_id"] == 2
        recorded = record_game(factory_ref=NO_DB_FACTORY_REF, seed=22, sim_kwargs=_kwargs())
        assert plays["n_pitches"] == len(thrown_pitches(recorded.plays))
        assert [e["sequence"] for e in plays["entries"]] == list(range(plays["n_pitches"]))

    def test_the_store_keeps_the_newest_runs(self, patch_resolver):
        replay = _replay_con()
        client = TestClient(_build_app(replay=replay))
        for seed in range(sim_store.REPLAY_RUNS_KEPT + 2):
            client.post(f"/api/games/745001/sample-game?base_seed={seed}")
        runs = [
            r[0] for r in replay.execute("SELECT run_id FROM sim.game_cards ORDER BY 1").fetchall()
        ]
        assert runs == list(range(3, sim_store.REPLAY_RUNS_KEPT + 3))

    def test_the_game_is_recorded_on_the_runner(self, patch_resolver, monkeypatch):
        """The API process never records a game itself: it asks the runner,
        whose workers hold the sim bundle warm."""

        def _boom(**_kwargs):
            raise AssertionError("the route recorded the game in the API process")

        monkeypatch.setattr(games_mod, "record_game", _boom)
        seen: list[tuple] = []
        real = BatchRunner.record_game

        def _spy(self, spec, seed):
            seen.append((spec.machine_factory, seed))
            return real(self, spec, seed)

        monkeypatch.setattr(BatchRunner, "record_game", _spy)
        client = TestClient(_build_app(replay=_replay_con()))
        assert client.post("/api/games/745001/sample-game?base_seed=8").status_code == 200
        assert seen == [(NO_DB_FACTORY_REF, 8)]

    def test_with_the_store_off_it_answers_503(self, patch_resolver):
        client = TestClient(_build_app(replay=None))
        assert client.post("/api/games/745001/sample-game").status_code == 503
        assert client.get("/api/games/745001/plays").status_code == 503
        assert client.get("/api/games/745001/linescore").status_code == 503

    def test_a_failed_store_answers_500(self, patch_resolver, monkeypatch):
        def _boom(*_a, **_k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(games_mod, "_build_replay_artifacts", _boom)
        client = TestClient(_build_app(replay=_replay_con()))
        assert client.post("/api/games/745001/sample-game").status_code == 500


class TestTheReplayWrites:
    """The file numbers its own runs; stores run one at a time; a prune never
    fails a store."""

    def _built(self, seed: int):
        game = record_game(factory_ref=NO_DB_FACTORY_REF, seed=seed, sim_kwargs=_kwargs())
        return games_mod._build_replay_artifacts(game)

    def _store(self, con, game_pk: int, built, seed: int) -> int:
        return games_mod._replay_call(
            con, games_mod._store_replay_run, game_pk=game_pk, built=built, base_seed=seed
        )

    def test_run_ids_are_the_files_own_and_unique_across_games(self):
        con = _replay_con()
        built = self._built(1)
        assert [self._store(con, pk, built, 1) for pk in (10, 20, 10)] == [1, 2, 3]
        assert sim_store.latest_replay_run_id(con, game_pk=10) == 3
        assert sim_store.latest_replay_run_id(con, game_pk=20) == 2

    def test_concurrent_stores_of_one_game_all_succeed(self, tmp_path):
        import threading

        con = sim_store.open_replay_store(str(tmp_path / "replay.duckdb"))
        built = self._built(2)
        errors: list[BaseException] = []
        ids: list[int] = []

        def _work():
            for _ in range(6):
                try:
                    ids.append(self._store(con, 77, built, 2))
                except BaseException as exc:  # noqa: BLE001 -- collected for the assert
                    errors.append(exc)

        threads = [threading.Thread(target=_work) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        try:
            assert errors == []
            assert sorted(ids) == list(range(1, 19))
            kept = con.execute("SELECT count(*) FROM sim.game_cards WHERE game_pk = 77").fetchone()[
                0
            ]
            assert kept == sim_store.REPLAY_RUNS_KEPT
        finally:
            con.close()

    def test_a_failed_prune_keeps_the_stored_game(self, monkeypatch):
        def _boom(*_a, **_k):
            raise RuntimeError("Conflict on tuple deletion")

        monkeypatch.setattr(sim_store, "prune_replay_runs", _boom)
        con = _replay_con()
        run_id = self._store(con, 5, self._built(3), 3)
        assert sim_store.latest_replay_run_id(con, game_pk=5) == run_id

    def test_a_failed_write_leaves_nothing_half_stored(self, monkeypatch):
        def _boom(*_a, **_k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(sim_store, "store_game_card", _boom)
        con = _replay_con()
        with pytest.raises(RuntimeError):
            self._store(con, 5, self._built(4), 4)
        assert con.execute("SELECT count(*) FROM sim.play_stream").fetchone() == (0,)


class TestTheProjectionsStoreTheirFirstGame:
    def test_boxscore_stores_the_game_at_its_seed(self, patch_resolver):
        replay = _replay_con()
        client = TestClient(_build_app(replay=replay))
        resp = client.get("/api/games/745001/boxscore?n_iterations=3&base_seed=40")
        assert resp.status_code == 200, resp.text
        plays = client.get("/api/games/745001/plays").json()
        recorded = record_game(factory_ref=NO_DB_FACTORY_REF, seed=40, sim_kwargs=_kwargs())
        assert plays["n_pitches"] == len(thrown_pitches(recorded.plays))
        # Game 0 of the projections is that game: its box score is the recorded one.
        card = resp.json()["players"]
        starter_k = card[str(_HOME_STARTER)]["means"]["K"]
        assert starter_k >= 0

    def test_boxscore_works_with_the_store_off(self, patch_resolver):
        client = TestClient(_build_app(replay=None))
        assert (
            client.get("/api/games/745001/boxscore?n_iterations=2&base_seed=1").status_code == 200
        )


# ===========================================================================
# 4) The app never opens the analytics file writable
# ===========================================================================


def test_the_lifespan_attaches_the_replay_store_not_the_analytics_handle():
    """api/main.py may set app.state.replay_duckdb; it must never set sim_duckdb."""
    tree = ast.parse((REPO_ROOT / "api" / "main.py").read_text(encoding="utf-8"))
    targets = [
        ast.unparse(t)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for t in node.targets
    ]
    assert "app.state.replay_duckdb" in targets
    assert "app.state.sim_duckdb" not in targets


def test_the_replay_entry_model_round_trips_the_context():
    from api.schemas import PlayByPlayEntryModel

    game = record_game(factory_ref=NO_DB_FACTORY_REF, seed=5, sim_kwargs=_kwargs())
    entry = PlayByPlay.from_play_results(game.plays, game.contexts).entries[0]
    model = PlayByPlayEntryModel.from_dataclass(entry)
    assert {f: getattr(model, f) for f in _CONTEXT_FIELDS} == {
        f: getattr(entry, f) for f in _CONTEXT_FIELDS
    }
    assert dataclasses.asdict(entry)["batter_id"] == 101
