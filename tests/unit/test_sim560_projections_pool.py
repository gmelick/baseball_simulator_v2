"""SIM-560: the game page's projections run on the worker pool and name each player.

The projections panel reads ``GET /api/games/{pk}/boxscore``, and each clicked
prop reads ``GET /api/games/{pk}/props/{player}/{prop}``. Both routes built their
prop set by replaying N games one after another inside the API process
(``record_game_plays`` in a loop): about 2.5 minutes for the panel's 100 games,
and the same again for every click. They now ask the batch runner for the set
(:meth:`BatchRunner.run_prop_set`). The runner fans the games out to its worker
pool and caches a seeded set, so a click reads the run the panel shows.

The card also names each player and tags the player's side, batting-order slot
and starting-pitcher role, so the panel can group the players by team.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.games as games_mod
import simulation.batch_runner as batch_runner_mod
from api.routes.games import router as games_router
from simulation.batch_runner import (
    BatchRunner,
    GameSpec,
    InMemoryCache,
    NullCache,
    derive_seed,
)
from simulation.game_state import GameState
from simulation.play_recorder import record_game_plays
from simulation.prop_distributions import PropDistributionSet
from simulation.results import BoxScore
from simulation.sim_kwargs import sim_kwargs_from_state

NO_DB_FACTORY_REF = "simulation.batch_runner:rng_driven_machine_factory"

_AWAY_STARTER = 301
_HOME_STARTER = 401


def _state() -> GameState:
    """A small two-team game: batters 101-109 (away) and 201-209 (home)."""
    state = GameState(pitcher_id=_HOME_STARTER, bat_hand="R", season=2024)
    state.away_lineup = [101 + i for i in range(9)]
    state.home_lineup = [201 + i for i in range(9)]
    state.away_lineup_slot = 0
    state.home_lineup_slot = 0
    state.batter_id = 101
    state.throw_hand = "R"
    state.away_pitcher_id = _AWAY_STARTER
    state.home_pitcher_id = _HOME_STARTER
    state.manager.bullpen_available = {0: [302, 303], 1: [402, 403]}
    return state


def _kwargs() -> dict:
    return sim_kwargs_from_state(_state())


def _spec() -> GameSpec:
    return GameSpec(machine_factory=NO_DB_FACTORY_REF, sim_kwargs=_kwargs())


def _serial_recorded_set(n: int, base_seed: int) -> PropDistributionSet:
    """The pre-SIM-560 route path: record N games one after another."""
    boxscores = []
    for i in range(n):
        result, _plays = record_game_plays(
            factory_ref=NO_DB_FACTORY_REF,
            seed=derive_seed(base_seed, i),
            sim_kwargs=_kwargs(),
        )
        boxscores.append(result.boxscore if result.boxscore is not None else BoxScore())
    return PropDistributionSet.from_boxscores(boxscores)


def _fingerprint(pset: PropDistributionSet) -> dict:
    """Every number a prop set carries, as plain Python values."""
    return {
        pid: {
            prop: (dist.n, dist.mean, dist.support.tolist(), dist.probabilities.tolist())
            for prop, dist in props.items()
        }
        for pid, props in pset.by_player.items()
    }


class _CountingRunner(BatchRunner):
    """A runner that counts how many times it simulates a batch."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.executions = 0

    def _execute(self, spec, seeds, max_workers):
        self.executions += 1
        return super()._execute(spec, seeds, max_workers)


# ===========================================================================
# 1) BatchRunner.run_prop_set
# ===========================================================================


class TestRunPropSet:
    def test_matches_the_recorded_serial_path(self):
        """The runner's set equals the one the old route built, number for number."""
        runner = BatchRunner(cache=NullCache(), max_workers=1)
        pooled = runner.run_prop_set(_spec(), n_iterations=6, base_seed=77)
        assert pooled.n_iterations == 6
        assert _fingerprint(pooled) == _fingerprint(_serial_recorded_set(6, 77))

    def test_pooled_matches_in_process(self):
        """Two workers give the same set as one: the games come back in seed order."""
        in_process = BatchRunner(cache=NullCache(), max_workers=1).run_prop_set(
            _spec(), n_iterations=4, base_seed=5
        )
        pooled_runner = BatchRunner(cache=NullCache(), max_workers=2)
        try:
            pooled = pooled_runner.run_prop_set(_spec(), n_iterations=4, base_seed=5)
            assert pooled_runner._pool is not None  # the games ran on the pool
        finally:
            pooled_runner.close()
        assert _fingerprint(pooled) == _fingerprint(in_process)

    def test_seeded_set_is_cached(self):
        runner = _CountingRunner(cache=InMemoryCache(), max_workers=1)
        first = runner.run_prop_set(_spec(), n_iterations=3, base_seed=9)
        again = runner.run_prop_set(_spec(), n_iterations=3, base_seed=9)
        assert runner.executions == 1
        assert _fingerprint(again) == _fingerprint(first)

        runner.run_prop_set(_spec(), n_iterations=3, base_seed=10)
        assert runner.executions == 2  # a new seed is a new run

    def test_prop_set_and_summary_keep_separate_cache_entries(self):
        runner = BatchRunner(cache=InMemoryCache(), max_workers=1)
        runner.run_prop_set(_spec(), n_iterations=3, base_seed=9)
        batch = runner.run(_spec(), n_iterations=3, base_seed=9)
        assert batch.from_cache is False
        assert not isinstance(batch.summary, PropDistributionSet)

    def test_unseeded_set_is_never_cached(self):
        """No seed asks for a fresh draw, so each call runs the games again."""
        runner = _CountingRunner(cache=InMemoryCache(), max_workers=1)
        runner.run_prop_set(_spec(), n_iterations=2, base_seed=None)
        runner.run_prop_set(_spec(), n_iterations=2, base_seed=None)
        assert runner.executions == 2

    def test_a_restarted_app_does_not_read_an_old_process_set(self):
        """Redis outlives a restart, and a restart can change the bundle or a flag.
        The key carries the process's token, so a new process runs the games again."""
        runner = _CountingRunner(cache=InMemoryCache(), max_workers=1)
        runner.run_prop_set(_spec(), n_iterations=2, base_seed=4)
        token = batch_runner_mod._PROP_SET_CACHE_EPOCH
        try:
            batch_runner_mod._PROP_SET_CACHE_EPOCH = token + "-restarted"
            runner.run_prop_set(_spec(), n_iterations=2, base_seed=4)
        finally:
            batch_runner_mod._PROP_SET_CACHE_EPOCH = token
        assert runner.executions == 2

    def test_use_cache_false_runs_again(self):
        runner = _CountingRunner(cache=InMemoryCache(), max_workers=1)
        runner.run_prop_set(_spec(), n_iterations=2, base_seed=3)
        runner.run_prop_set(_spec(), n_iterations=2, base_seed=3, use_cache=False)
        assert runner.executions == 2

    def test_rejects_zero_iterations(self):
        with pytest.raises(ValueError):
            BatchRunner(cache=NullCache(), max_workers=1).run_prop_set(
                _spec(), n_iterations=0, base_seed=1
            )


# ===========================================================================
# 2) _player_tags: side, batting-order slot, starting pitcher
# ===========================================================================


class TestPlayerTags:
    def test_batters_carry_their_side_and_slot(self):
        tags = games_mod._player_tags(_kwargs())
        assert tags[101].side == "away" and tags[101].lineup_slot == 1
        assert tags[109].side == "away" and tags[109].lineup_slot == 9
        assert tags[205].side == "home" and tags[205].lineup_slot == 5
        assert tags[205].starting_pitcher is False

    def test_starters_and_pens(self):
        tags = games_mod._player_tags(_kwargs())
        assert tags[_AWAY_STARTER].side == "away" and tags[_AWAY_STARTER].starting_pitcher
        assert tags[_HOME_STARTER].side == "home" and tags[_HOME_STARTER].starting_pitcher
        assert tags[302].side == "away" and not tags[302].starting_pitcher
        assert tags[403].side == "home" and tags[403].lineup_slot is None

    def test_a_pitcher_who_bats_keeps_both_tags(self):
        kw = _kwargs()
        kw["home_lineup"] = [_HOME_STARTER] + list(kw["home_lineup"][1:])
        tag = games_mod._player_tags(kw)[_HOME_STARTER]
        assert tag.side == "home" and tag.lineup_slot == 1 and tag.starting_pitcher

    def test_placeholder_relievers_take_their_starters_side(self):
        """A game with no pen in the box (a scheduled game) pitches the factory's
        placeholder arms, negative ids built from each starter's id."""
        from simulation.production_factory import _default_bullpen_for_spec

        kw = _kwargs()
        kw["bullpen"] = {}
        placeholder = _default_bullpen_for_spec(GameSpec(sim_kwargs=kw))
        tags = games_mod._player_tags(kw)
        for team, side in ((0, "away"), (1, "home")):
            for pid in placeholder[team]:
                assert pid < 0
                assert tags[pid].side == side
                assert tags[pid].lineup_slot is None
                assert tags[pid].starting_pitcher is False

    def test_placeholder_relievers_get_a_label(self):
        from simulation.production_factory import _default_bullpen_for_spec

        kw = _kwargs()
        placeholder = _default_bullpen_for_spec(GameSpec(sim_kwargs=kw))
        names = games_mod._placeholder_names(kw)
        assert names[placeholder[0][0]] == "Generic reliever 1"
        assert names[placeholder[1][5]] == "Generic reliever 6"
        assert all(pid < 0 for pid in names)

    def test_empty_kwargs_tag_only_the_placeholder_arms(self):
        """With no starters the placeholder ids still exist, so tag them and nobody else."""
        assert all(pid < 0 for pid in games_mod._player_tags({}))


# ===========================================================================
# 3) The routes
# ===========================================================================


class _NamesPool:
    """A fake asyncpg pool: game rows for the kwargs build, names for the card."""

    def __init__(self, *, with_names: bool = True):
        self.with_names = with_names

    async def fetch(self, sql, *args):
        if "raw.players" in sql:
            ids = args[0] if args else []
            if not self.with_names:
                return [{"player_id": pid, "bats": "R", "throws": "R"} for pid in ids]
            return [{"player_id": pid, "full_name": f"Player Name {pid}"} for pid in ids]
        return []

    async def fetchrow(self, sql, *args):
        if "raw.games" in sql:
            return {"game_pk": int(args[0]), "season": 2024, "home_team_id": 1, "away_team_id": 2}
        return None

    async def fetchval(self, sql, *args):
        return None


def _build_app(*, pool, runner=None, cache=None) -> FastAPI:
    app = FastAPI()
    app.include_router(games_router)
    app.state.pg_pool = pool
    # An in-memory cache per app: the route's transient runner must never fall
    # back to make_cache(), which reaches a live Redis when REDIS_URL is set.
    app.state.sim_cache = cache if cache is not None else InMemoryCache()
    app.state.sim_factory_ref = NO_DB_FACTORY_REF
    app.state.sim_duckdb = None
    if runner is not None:
        app.state.sim_runner = runner
    return app


@pytest.fixture()
def patch_resolver(monkeypatch):
    async def _fake_resolve_game_state(conn, game_pk, **kwargs):
        return _state()

    monkeypatch.setattr(games_mod, "resolve_game_state", _fake_resolve_game_state)


@pytest.fixture()
def no_serial_replay(monkeypatch):
    """Fail the test if a route still replays the games through the recorder."""

    def _boom(**_kwargs):
        raise AssertionError("the projections must not replay games one by one")

    monkeypatch.setattr(games_mod, "record_game_plays", _boom)


class TestBoxscoreRoute:
    def test_rows_carry_name_side_slot_and_starter(self, patch_resolver, no_serial_replay):
        client = TestClient(_build_app(pool=_NamesPool()))
        resp = client.get("/api/games/745001/boxscore?n_iterations=3&base_seed=7")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["n_iterations"] == 3
        assert body["base_seed"] == 7
        players = body["players"]

        lead_off = players["101"]
        assert lead_off["name"] == "Player Name 101"
        assert lead_off["side"] == "away"
        assert lead_off["lineup_slot"] == 1
        assert lead_off["starting_pitcher"] is False

        starter = players[str(_HOME_STARTER)]
        assert starter["side"] == "home"
        assert starter["starting_pitcher"] is True
        assert starter["lineup_slot"] is None
        assert "K" in starter["means"]
        json.dumps(body)

    def test_runs_through_the_app_runner(self, patch_resolver, no_serial_replay):
        runner = _CountingRunner(cache=InMemoryCache(), max_workers=1)
        client = TestClient(_build_app(pool=_NamesPool(), runner=runner))
        client.get("/api/games/745001/boxscore?n_iterations=2&base_seed=1")
        client.get("/api/games/745001/boxscore?n_iterations=2&base_seed=1")
        assert runner.executions == 1  # the second request read the cached set

    def test_missing_names_leave_name_empty(self, patch_resolver):
        client = TestClient(_build_app(pool=_NamesPool(with_names=False)))
        resp = client.get("/api/games/745001/boxscore?n_iterations=2&base_seed=1")
        assert resp.status_code == 200, resp.text
        assert resp.json()["players"]["101"]["name"] is None

    def test_a_failing_names_query_does_not_break_the_card(self, patch_resolver, monkeypatch):
        async def _fail(_pool, _ids):
            raise RuntimeError("database down")

        monkeypatch.setattr(games_mod, "_query_player_names", _fail)
        client = TestClient(_build_app(pool=_NamesPool()))
        resp = client.get("/api/games/745001/boxscore?n_iterations=2&base_seed=1")
        assert resp.status_code == 200, resp.text
        assert resp.json()["players"]["101"]["name"] is None


class TestPropRouteReadsTheSameRun:
    def test_a_click_after_the_card_reads_the_cached_run(self, patch_resolver, no_serial_replay):
        runner = _CountingRunner(cache=InMemoryCache(), max_workers=1)
        client = TestClient(_build_app(pool=_NamesPool(), runner=runner))
        card = client.get("/api/games/745001/boxscore?n_iterations=4&base_seed=5").json()
        prop = client.get(
            f"/api/games/745001/props/{_HOME_STARTER}/K?n_iterations=4&base_seed=5&line=4.5"
        )
        assert prop.status_code == 200, prop.text
        assert runner.executions == 1
        assert prop.json()["mean"] == pytest.approx(
            card["players"][str(_HOME_STARTER)]["means"]["K"]
        )
