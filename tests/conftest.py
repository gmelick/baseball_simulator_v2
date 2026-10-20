"""
Root test configuration: shared fixtures and pytest hooks.

Fixtures defined here are available to ALL tests (unit, integration, backtesting).
Heavy fixtures that require live infrastructure (PostgreSQL, Redis) live in
tests/integration/conftest.py and are only instantiated when integration tests run.

Shared mock fixtures
--------------------
The live-ingestion-pipeline tests need the same handful of mocks: an
AsyncMock asyncpg pool, an AsyncMock Redis client, a mock aiohttp session, a
minimal feed/live payload, and a built game_state. The fixtures below provide
them once. They are opt-in: a test gets one only by naming it in its
signature. Some test modules define their own local helpers instead.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# The flag pins. Production sets the SIM_* flags in the docker-compose `app`
# env, and `docker compose run app pytest` inherits that env. Most pins below
# set a flag to its OFF value, which skips that feature's code, so the unit
# lane's output stays byte-identical (the same, bit for bit) to its baseline.
# A test that wants a feature on sets it explicitly: with monkeypatch, or on
# the sampler or the machine.

# The manager (the intentional walk and the pitching change). Off = no
# manager on the machine. The manager tests opt in with monkeypatch.
os.environ["SIM_MANAGER"] = "0"

# The batted-ball platoon: the fielding draw keys its pool on the batter's
# hand against the pitcher's.
os.environ["SIM_BB_PLATOON"] = "0"

# The home-field draw weight on a row of the other batting side. 1.0 skips the
# weight multiplication exactly, so the unit suite keeps a symmetric run
# environment. The home-field tests pass home_off_weight to the sampler
# directly, not via the env.
os.environ["SIM_HOME_OFF_WEIGHT"] = "1.0"

# The park kernel's bandwidth. 0 disables the kernel exactly; the tests set
# the sampler's park_sigma directly.
os.environ["SIM_PARK_KERNEL_SIGMA"] = "0"

# The catcher receiving ratio, and the drawn pitch row's got-away fact (a wild
# pitch or a passed ball). The tests opt in explicitly.
os.environ["SIM_CATCHER_RECEIVING"] = "0"
os.environ["SIM_GOT_AWAY"] = "0"

# The draw-conditioning weights: fatigue and the batting side on the pitch
# draw, and the drawn pitch's similarity on the fielding draw. The tests set
# the sampler attributes directly.
os.environ["SIM_FATIGUE_PC_SIGMA"] = "0"
os.environ["SIM_FATIGUE_TTO_SIGMA"] = "0"
os.environ["SIM_PITCH_HOME_OFF_WEIGHT"] = "1.0"
os.environ["SIM_BB_PITCH_SIGMA"] = "0"

# The pitch-draw cell index. Off = the whole-pool weight assembly; the tests
# set ``pitch_cell_index`` directly.
os.environ["SIM_PITCH_CELL_INDEX"] = "0"

# The actor score matrices are the only actor factors, and no flag gates them.
# A synthetic bundle without a matrix leaves that actor neutral; the tests set
# ``actor_power`` directly.

# The pitch / pitch-result split and the born-batted-ball kernel (a weight on
# the batted ball the result row was born with). Both off = one draw per
# pitch; the tests set the sampler attributes directly.
os.environ["SIM_PITCH_RESULT_SPLIT"] = "0"
os.environ["SIM_PITCH_PITCHER_POWER"] = "1.0"  # 1.0 = the raw score, no power step
os.environ["SIM_BB_BORN_SIGMA"] = "0"

# The fielding draw's class filter, the park's wall-zone rule and the fence
# stage. The tests set the sampler attributes directly.
os.environ["SIM_BB_CLASS_FILTER"] = "0"
os.environ["SIM_PARK_WALL_ZONE_ONLY"] = "0"
os.environ["SIM_FENCE_STAGE"] = "0"
# The fence amendments: the carry offset, the born kernel's exponent per
# feature and the wall-margin band. The tests set the sampler attributes
# directly.
os.environ["SIM_CARRY_OFFSET"] = "0"
os.environ["SIM_BB_BORN_PER_FEATURE"] = "0"
os.environ["SIM_BB_MARGIN_BAND"] = "0"
# The bundle watch: a test never restarts PID 1 and never reads a bundle
# version that a lifespan pinned. A lock error in a test never waits 30 minutes.
os.environ["SIM_RESTART_ON_NEW_BUNDLE"] = "0"
os.environ.pop("BASEBALL_ENGINE_ARTIFACT_DIR", None)
os.environ["SIM_BOOT_LOCK_WAIT_S"] = "0"

# The pitching change as a draw from the change opportunity pool. Off = no
# pitching change. A test that wants the draw sets ``machine.manager_draw``.
os.environ["SIM_MANAGER_DRAW"] = "0"
# The live manager's usage weight on the change draw and the six reliever-draw
# weights, every one at its OFF value. A unit game then keeps the pool's own
# change rate and the positional reliever pick.
os.environ["SIM_ACTOR_POWER_MANAGER_USAGE"] = "0"
# The pen source: 'synthetic' = the no-DB seam's pen; 'box' = the MLB box's
# per-game listing (production).
os.environ["SIM_BULLPEN_SOURCE"] = "synthetic"
os.environ["SIM_RELIEF_ROLE_SIGMA"] = "0"
os.environ["SIM_RELIEF_PITCHER_POWER"] = "0"
os.environ["SIM_RELIEF_REST_SIGMA"] = "0"
os.environ["SIM_RELIEF_PITCHED2D_OFF_WEIGHT"] = "1.0"
os.environ["SIM_RELIEF_PITCHES3D_SIGMA"] = "0"
os.environ["SIM_RELIEF_HAND_OFF_WEIGHT"] = "1.0"

# The running game on the pitch (SIM_STEAL_PITCH_CLASS) is NOT pinned, on
# purpose. The flag defaults ON, in the sampler and in the factory. The unit
# lane builds its samplers directly, so the environment never reaches them, and
# a pin here would change nothing. The no-database tests run the production
# order on purpose (plan §4.5): the pickoff draw before the pitch, the steal
# draw after it in the pitch's class. A test that wants the single pre-pitch
# draw sets ``sampler.steal_pitch_class = False``, or builds a steal pool with
# no class.

# The API requires a login in every environment, and the one opt-out is
# AUTH_DISABLED=1. The suite opts out here so the route tests that build a
# bare app or router need no credentials. setdefault (not a pin) keeps an
# explicit value from the shell. The two files that test the enforcement
# delete the variable with monkeypatch: tests/unit/test_api_auth_session.py
# and tests/unit/test_route_auth_coverage.py.
os.environ.setdefault("AUTH_DISABLED", "1")

# The test lane's odds provider. The app container reads the stack's .env
# through env_file, and that file names the vendor: ODDS_PROVIDER=bettingpros.
# Twelve tests build the live pipeline with __new__ (no constructor) and set
# no provider, so the pipeline's lazy fallback reads the environment. Without
# the pin, those tests build the vendor's provider and its reads go to the
# network. The pin is a hard one, because setdefault cannot beat the explicit
# .env value. A test that wants the vendor name sets it with monkeypatch
# (tests/unit/test_odds_writers.py). The clean_env fixture of
# tests/unit/test_odds_provider_seam.py deletes it. Monkeypatch restores the
# pinned value on teardown. The pin removes the API key too. A test that
# still builds the vendor's provider then sends no BettingPros request. The
# provider's _bp_get raises "needs an API key" first. The public methods
# catch that error and return empty rows. The MLB Stats API reads (_mlb_get)
# need no key, so they still try the network.
os.environ["ODDS_PROVIDER"] = "mock"
os.environ.pop("ODDS_API_KEY", None)

# ---------------------------------------------------------------------------
# Shared lightweight fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_game_pk() -> int:
    """A stable game_pk used across unit tests to avoid magic numbers."""
    return 745000


@pytest.fixture()
def sample_player_ids() -> dict[str, list[int]]:
    """A small set of fake player IDs for unit test data construction."""
    return {
        "pitchers": [100001, 100002, 100003],
        "batters": [200001, 200002, 200003, 200004, 200005, 200006, 200007, 200008, 200009],
        "fielders": [200001, 200002, 200003],
    }


# ---------------------------------------------------------------------------
# Mock infrastructure fixtures (no live DB / Redis / HTTP)
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_db_pool() -> AsyncMock:
    """An AsyncMock standing in for an asyncpg connection pool.

    ``execute`` / ``fetch`` / ``fetchrow`` / ``fetchval`` are awaitable mocks.
    ``fetch`` defaults to returning an empty list so a builder that issues a
    batch query (e.g. ``_batch_days_rest``) gets a benign empty result rather
    than a coroutine it can't iterate.
    """
    pool = AsyncMock()
    pool.fetch.return_value = []
    pool.fetchrow.return_value = None
    pool.fetchval.return_value = None
    # ``execute`` returns an asyncpg command tag string in production
    # (e.g. "UPDATE 1"); default to "UPDATE 0" so row-count parsers don't trip.
    pool.execute.return_value = "UPDATE 0"
    return pool


@pytest.fixture()
def mock_redis() -> AsyncMock:
    """An AsyncMock standing in for an async Redis client.

    ``get`` / ``ttl`` default to None (cache miss / no cooldown); ``setex`` and
    ``set`` are awaitable no-ops.  Tests override ``get.return_value`` /
    ``ttl.return_value`` when they need a hit.
    """
    redis = AsyncMock()
    redis.get.return_value = None
    redis.ttl.return_value = None
    return redis


@pytest.fixture()
def mock_http_session() -> MagicMock:
    """A mock aiohttp.ClientSession whose ``.get()`` returns an async-context
    response.  Configure ``mock_http_session._response.status`` and
    ``mock_http_session._response.json.return_value`` per test.

    The session itself is a MagicMock (``.get`` is sync and returns the
    async-context-manager response, matching aiohttp's API).
    """
    response = AsyncMock()
    response.status = 200
    response.json.return_value = {}
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get.return_value = response
    # Expose the response so tests can tweak status / payload.
    session._response = response
    return session


@pytest.fixture()
def sample_feed(sample_game_pk: int) -> dict[str, Any]:
    """A minimal but structurally-complete MLB feed/live payload.

    Sufficient to drive ``GameStateBuilder.build()`` and
    ``LiveIngestionPipeline._should_resimulate()`` without a network call.
    A single completed plate appearance is present so play-history parsing and
    the re-sim trigger both have something to chew on.
    """
    return {
        "gamePk": sample_game_pk,
        "gameData": {
            "status": {"abstractGameState": "Live"},
            "teams": {"home": {"id": 147}, "away": {"id": 111}},
            "datetime": {"officialDate": "2024-08-15"},
        },
        "liveData": {
            "linescore": {
                "currentInning": 3,
                "inningHalf": "Top",
                "outs": 2,
                "teams": {
                    "home": {"runs": 3, "hits": 6, "errors": 0},
                    "away": {"runs": 1, "hits": 4, "errors": 1},
                },
                "innings": [
                    {"home": {"runs": 1}, "away": {"runs": 0}},
                    {"home": {"runs": 2}, "away": {"runs": 0}},
                    {"home": {"runs": 0}, "away": {"runs": 1}},
                ],
                "offense": {
                    "first": {"id": 301},
                    "second": {},
                    "third": {},
                },
                "defense": {"pitcher": {"id": 999}},
            },
            "boxscore": {
                "teams": {
                    "home": {"players": {}, "battingOrder": []},
                    "away": {"players": {}, "battingOrder": []},
                }
            },
            "plays": {
                "currentPlay": {
                    "matchup": {
                        "batter": {"id": 101, "fullName": "Test Batter"},
                        "pitcher": {"id": 999, "fullName": "Test Pitcher"},
                    },
                    "count": {"balls": 2, "strikes": 1},
                    "about": {
                        "isComplete": True,
                        "atBatIndex": 41,
                        "inning": 3,
                        "halfInning": "top",
                    },
                    "result": {"eventType": "single", "description": "Single to left", "rbi": 1},
                },
                "allPlays": [
                    {
                        "about": {
                            "atBatIndex": 41,
                            "inning": 3,
                            "halfInning": "top",
                            "isComplete": True,
                        },
                        "matchup": {
                            "batter": {"id": 101, "fullName": "Test Batter"},
                            "pitcher": {"id": 999, "fullName": "Test Pitcher"},
                        },
                        "result": {
                            "eventType": "single",
                            "description": "Single to left",
                            "rbi": 1,
                        },
                        "playEvents": [
                            {
                                "isPitch": True,
                                "pitchNumber": 1,
                                "details": {
                                    "type": {"code": "FF", "description": "Four-Seam Fastball"},
                                    "description": "Ball",
                                },
                                "pitchData": {"startSpeed": 95.1},
                                "count": {"balls": 1, "strikes": 0, "outs": 2},
                            }
                        ],
                    }
                ],
            },
        },
    }


@pytest.fixture()
def sample_game_state(sample_game_pk: int) -> dict[str, Any]:
    """A built game_state dict (the shape GameStateBuilder.build() returns).

    Used by tests that exercise consumers of game_state (prop collection,
    broadcast payloads, resim signalling) without re-running the builder.
    """
    return {
        "game_pk": sample_game_pk,
        "game_status": "Live",
        "inning": 3,
        "half": "Top",
        "outs": 2,
        "balls": 2,
        "strikes": 1,
        "home_score": 3,
        "away_score": 1,
        "batting_team_id": 111,
        "fielding_team_id": 147,
        "on_1b": 301,
        "on_2b": None,
        "on_3b": None,
        "current_batter_id": 101,
        "current_pitcher_id": 999,
        "home_lineup": [{"player_id": 401}, {"player_id": 402}],
        "away_lineup": [{"player_id": 501}, {"player_id": 502}],
        "home_bullpen": [],
        "away_bullpen": [],
        "home_bench": [],
        "away_bench": [],
        "play_history": [{"at_bat_number": 42, "inning": 3}],
        "inning_scores": {},
        "last_updated_at": "2024-08-15T19:05:00+00:00",
    }


# ---------------------------------------------------------------------------
# The event-loop guard (Python 3.11+ / pytest-asyncio interaction)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _ensure_current_event_loop():
    """Guarantee a usable current event loop for EVERY test.

    pytest-asyncio (auto mode) sets the main thread's event loop to ``None`` after
    each async test. On Python 3.11+, ``asyncio.get_event_loop()`` then RAISES
    ``RuntimeError: There is no current event loop`` instead of lazily creating one
    (the 3.10 behaviour). Any *sync* test that touches the loop -- directly or via a
    library -- therefore fails *order-dependently* (only when an async test ran
    earlier in the same process, e.g. the full CI suite, but not when the file runs
    alone). Restoring a valid loop before every test removes that whole failure
    class regardless of collection order.
    """
    import asyncio

    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    yield
