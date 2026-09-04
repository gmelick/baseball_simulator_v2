"""
Root test configuration — shared fixtures and pytest hooks.

Fixtures defined here are available to ALL tests (unit, integration, backtesting).
Heavy fixtures that require live infrastructure (PostgreSQL, Redis) live in
tests/integration/conftest.py and are only instantiated when integration tests run.

SIM-152 — Shared unit-test fixtures
-----------------------------------
The live-ingestion-pipeline tests (test_live_pipeline_bugs.py,
test_data_engineer_sim340.py, test_live_pipeline_sim348.py) all stand up the
same handful of mocks: an AsyncMock asyncpg pool, an AsyncMock Redis client, a
mock aiohttp session, a minimal feed/live payload, and a built game_state.
SIM-152 hoists those into shared fixtures here so each module reuses them
instead of re-rolling its own.  Existing modules that already define local
helpers keep working — these fixtures are *additive* and opt-in (a test only
gets one by naming it in its signature).
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# SIM-429: production enables the full-pool sampler via SIM_FULL_POOL=1 (set in
# the docker-compose `app` env, which `docker compose run app pytest` inherits).
# The unit suite is pinned to the validated per-tile path — tests that exercise
# the full-pool path opt in explicitly via the `_full_pool` sim-kwarg, not the
# env — so force the env off here for deterministic, env-independent runs.
os.environ["SIM_FULL_POOL"] = "0"

# (SIM-476, owner ruling 2026-08-30: the SIM-412 home-field flip is DELETED —
# home advantage is the SIM-491 home kernel, pinned off below via
# SIM_HOME_OFF_WEIGHT=1.0, so the unit suite keeps its symmetric run
# environment.)

# SIM-434: production enables the manager decision model via SIM_MANAGER=1 (set
# in the docker-compose `app` env, which `docker compose run app pytest` inherits).
# The unit suite asserts the flag-OFF (manager-is-None) byte-identical baseline,
# so force it off here; the SIM-434 tests opt in explicitly via monkeypatch.
os.environ["SIM_MANAGER"] = "0"

# SIM-413: production enables the L-R platoon batted-ball reweight in the
# docker-compose `app` env; the unit suite asserts the flag-off baseline, so
# pin it off here so the env never leaks into `docker compose run app pytest`.
# (SIM-476 deleted the SIM_PARK_FACTOR / SIM_FIELDER_RBF post-draw flips; the
# fitted kernel sigmas pinned below replaced them.)
os.environ["SIM_BB_PLATOON"] = "0"

# SIM-491: the home-field DRAW weight (the SIM-412 rebuild). 1.0 disables the
# reweight exactly (no weight multiplication runs), so pin it here so a dev
# shell's env never leaks into `docker compose run app pytest`. The SIM-491
# tests pass home_off_weight to the sampler directly, not via the env.
os.environ["SIM_HOME_OFF_WEIGHT"] = "1.0"

# SIM-491 part 2: the park KERNEL bandwidth (the SIM-411 rebuild). 0 disables
# the kernel exactly; the tests set the sampler's park_sigma directly.
os.environ["SIM_PARK_KERNEL_SIGMA"] = "0"

# SIM-491 part 3: the fielder-quality KERNEL bandwidth (the SIM-425b rebuild).
# 0 disables it exactly; the tests set the sampler's fielder_sigma directly.
os.environ["SIM_FIELDER_KERNEL_SIGMA"] = "0"

# SIM-517: the catcher RECEIVING kernel bandwidth + the got-away resolution.
# Both off = byte-identical; the SIM-517 tests opt in explicitly.
os.environ["SIM_CATCHER_FRAMING_SIGMA"] = "0"
os.environ["SIM_CATCHER_BLOCK_SIGMA"] = "0"
os.environ["SIM_GOT_AWAY"] = "0"

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
# SIM-152: Mock infrastructure fixtures (no live DB / Redis / HTTP)
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
# SIM-378 — event-loop guard (Python 3.11+ / pytest-asyncio interaction)
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
