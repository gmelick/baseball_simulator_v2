"""tests/unit/test_cache_codec.py -- the Redis cache stores JSON, never a pickle.

A pickle load runs code, so the Redis cache stores typed JSON (the stack
hardening, SIM-443f). These tests pin the JSON codec
(``simulation/cache_codec.py``) and the ``RedisCache`` that uses it, over a
dict-backed fake Redis client:

* a summary and a prop set come back equal to what went in;
* the stored text is a JSON envelope ``{"t": <tag>, "v": <payload>}``;
* any value the codec cannot read (an old pickle, a tampered or truncated
  envelope, an old key prefix) is a cache miss, never an error;
* a value JSON cannot hold is skipped with a warning, never stored;
* neither module imports ``pickle``.

The real games run on the no-DB synthetic bundle through ``record_game_plays``.
"""

from __future__ import annotations

import ast
import json
import logging
import pickle
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pytest

from api.serialization import to_jsonable
from simulation import cache_codec
from simulation.batch_runner import RedisCache, make_cache
from simulation.loop.boxscore import GameSimResult
from simulation.play_recorder import record_game_plays
from simulation.prop_distributions import PropDistributionSet
from simulation.results import ConfidenceInterval, GameSimSummary

REPO_ROOT = Path(__file__).resolve().parents[2]

SIM_KWARGS = {
    "season": 2024,
    "pitcher_id": 600001,
    "bat_hand": "R",
    "away_lineup": [101, 102, 103, 104, 105, 106, 107, 108, 109],
    "home_lineup": [201, 202, 203, 204, 205, 206, 207, 208, 209],
    "max_innings": 12,
}


class _FakeRedis:
    """A dict-backed stand-in for ``redis.Redis``: stores ``(value, ex)``."""

    def __init__(self) -> None:
        self.store: dict[str, tuple[str | bytes, int | None]] = {}

    def ping(self) -> bool:
        return True

    def get(self, name: str) -> str | bytes | None:
        item = self.store.get(name)
        return None if item is None else item[0]

    def set(self, name: str, value: str | bytes, ex: int | None = None) -> None:
        self.store[name] = (value, ex)

    def scan_iter(self, match: str):
        prefix = match.rstrip("*")
        return [k for k in list(self.store) if k.startswith(prefix)]

    def delete(self, *names: str) -> int:
        for n in names:
            self.store.pop(n, None)
        return len(names)


class _BrokenRedis(_FakeRedis):
    """A client whose every operation raises, as a dropped connection does."""

    def get(self, name: str):
        raise ConnectionError("redis down")

    def set(self, name: str, value, ex=None):
        raise ConnectionError("redis down")

    def scan_iter(self, match: str):
        raise ConnectionError("redis down")


def _result(home, away, *, score=(1, 0)) -> GameSimResult:
    """A hand-built result; the summary reads only the scores and the grid."""
    return GameSimResult(
        home_score=score[0],
        away_score=score[1],
        innings_played=9,
        final_state=None,  # type: ignore[arg-type]
        home_by_inning=home,
        away_by_inning=away,
    )


@pytest.fixture(scope="module")
def real_results() -> list[GameSimResult]:
    """Six real games on the synthetic bundle."""
    return [record_game_plays(seed=s, sim_kwargs=dict(SIM_KWARGS))[0] for s in range(6)]


def _assert_summary_equal(a: GameSimSummary, b: GameSimSummary) -> None:
    for name in ("home_scores", "away_scores", "total_scores"):
        left, right = getattr(a, name), getattr(b, name)
        assert np.array_equal(left, right), name
        assert right.dtype == np.int64, name
    for name in (
        "n_iterations",
        "home_win_pct",
        "away_win_pct",
        "tie_pct",
        "home_score_mean",
        "away_score_mean",
        "total_score_mean",
        "home_score_median",
        "away_score_median",
        "total_score_median",
        "home_win_ci",
        "away_win_ci",
        "home_score_ci",
        "away_score_ci",
        "total_score_ci",
        "confidence_level",
        "ci_method",
        "inning_grids",
    ):
        assert getattr(a, name) == getattr(b, name), name
    assert b.simulated_at == a.simulated_at
    assert b.simulated_at.tzinfo is not None


# ---------------------------------------------------------------------------
# 1: the summary round trip
# ---------------------------------------------------------------------------


def test_hand_built_summary_round_trips_exactly():
    summary = GameSimSummary.from_results(
        [_result([1, None], [0, 0], score=(1, 0)), _result([0, 2], [1, 0], score=(2, 1))],
        simulated_at=datetime(2026, 10, 9, 12, 30, tzinfo=UTC),
    )
    cache = RedisCache(_FakeRedis())
    cache.set("k", summary, 60)
    back = cache.get("k")
    assert isinstance(back, GameSimSummary)
    _assert_summary_equal(summary, back)
    assert back.inning_grids == [([1, None], [0, 0]), ([0, 2], [1, 0])]
    assert isinstance(back.inning_grids[0], tuple)
    assert isinstance(back.home_win_ci, ConfidenceInterval)


def test_real_summary_round_trips_exactly(real_results):
    summary = GameSimSummary.from_results(real_results)
    assert summary.inning_grids is not None
    back = cache_codec.decode(cache_codec.encode(summary))
    assert isinstance(back, GameSimSummary)
    _assert_summary_equal(summary, back)


def test_to_json_equals_to_jsonable(real_results):
    summary = GameSimSummary.from_results(real_results)
    assert summary.to_json() == to_jsonable(summary)
    no_grid = GameSimSummary.from_results([_result(None, None)])
    assert no_grid.to_json() == to_jsonable(no_grid)
    assert GameSimSummary.from_json(no_grid.to_json()).inning_grids is None


def test_naive_time_reads_as_utc():
    summary = GameSimSummary.from_results([_result(None, None)])
    data = summary.to_json()
    data["simulated_at"] = "2026-10-09T12:00:00"
    back = GameSimSummary.from_json(data)
    assert back.simulated_at == datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 2: the prop set and plain values
# ---------------------------------------------------------------------------


def test_prop_set_round_trips(real_results):
    props = PropDistributionSet.from_results(real_results)
    assert props.by_player
    cache = RedisCache(_FakeRedis())
    cache.set("p", props, 900)
    back = cache.get("p")
    assert isinstance(back, PropDistributionSet)
    assert back.to_json() == props.to_json()
    pid = next(iter(props.by_player))
    prop = next(iter(props.by_player[pid]))
    assert back.by_player[pid][prop].support.dtype == np.int64
    assert np.array_equal(back.by_player[pid][prop].support, props.by_player[pid][prop].support)


@pytest.mark.parametrize(
    "value",
    [
        {"date": "2026-10-12", "games": [{"pk": 1, "x": None}], "count": 1},
        [1, 2.5, "three", None, True],
        "text",
        7,
    ],
)
def test_plain_json_values_round_trip(value):
    cache = RedisCache(_FakeRedis())
    cache.set("j", value, 30)
    assert cache.get("j") == value


def test_ttl_is_passed_as_ex():
    client = _FakeRedis()
    RedisCache(client).set("k", {"a": 1}, 123)
    assert client.store["sim443:k"][1] == 123


def test_stored_text_is_a_json_envelope():
    client = _FakeRedis()
    cache = RedisCache(client)
    cache.set("s", GameSimSummary.from_results([_result(None, None)]), 60)
    cache.set("d", {"a": 1}, 60)
    summary_doc = json.loads(client.store["sim443:s"][0])
    assert set(summary_doc) == {"t", "v"}
    assert summary_doc["t"] == "summary"
    assert json.loads(client.store["sim443:d"][0]) == {"t": "json", "v": {"a": 1}}


# ---------------------------------------------------------------------------
# 3: anything the codec cannot read is a miss
# ---------------------------------------------------------------------------


def test_legacy_pickle_is_a_miss():
    client = _FakeRedis()
    summary = GameSimSummary.from_results([_result(None, None)])
    client.store["sim443:k"] = (pickle.dumps(summary), 60)
    assert RedisCache(client).get("k") is None


def test_old_prefix_is_never_read():
    client = _FakeRedis()
    client.store["sim332:k"] = (cache_codec.encode({"a": 1}), 60)
    assert RedisCache(client).get("k") is None


@pytest.mark.parametrize(
    "raw",
    [
        b"\x80\x05",
        '{"t":"json","v":{"a":1}',  # truncated
        '{"t":"pickle","v":"gASV"}',  # unknown tag
        '{"t":"json"}',  # no payload
        '{"t":"json","v":1,"x":2}',  # an extra key
        '["json", 1]',  # not a dict
        '{"t":"summary","v":{"n_iterations":3}}',  # a summary with missing keys
        '{"t":"summary","v":[1,2]}',  # a summary that is not a dict
        '{"t":"prop_set","v":"nope"}',
        "",
        "not json at all",
    ],
)
def test_unreadable_values_are_a_miss(raw):
    client = _FakeRedis()
    client.store["sim443:k"] = (raw, 60)
    assert RedisCache(client).get("k") is None
    assert cache_codec.decode(raw) is None


def test_missing_key_is_a_miss():
    assert RedisCache(_FakeRedis()).get("absent") is None
    assert cache_codec.decode(None) is None


# ---------------------------------------------------------------------------
# 4: errors degrade, never raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [object(), {"day": date(2026, 10, 12)}, {1, 2}])
def test_uncacheable_value_is_skipped_with_a_warning(value, caplog):
    client = _FakeRedis()
    with caplog.at_level(logging.WARNING, logger="simulation.batch_runner"):
        RedisCache(client).set("bad", value, 60)
    assert client.store == {}
    assert any("sim cache: skipped bad" in r.getMessage() for r in caplog.records)


def test_client_errors_degrade_to_a_miss():
    cache = RedisCache(_BrokenRedis())
    cache.set("k", {"a": 1}, 60)
    assert cache.get("k") is None
    cache.clear()


def test_clear_deletes_only_the_prefix():
    client = _FakeRedis()
    client.store["other:k"] = ("x", None)
    cache = RedisCache(client)
    cache.set("a", 1, 60)
    cache.set("b", 2, 60)
    cache.clear()
    assert set(client.store) == {"other:k"}


# ---------------------------------------------------------------------------
# 5: no pickle anywhere on the cache path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["simulation/cache_codec.py", "simulation/batch_runner.py"])
def test_no_pickle_import(path):
    tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert "pickle" not in names
    assert "dill" not in names
    assert "cloudpickle" not in names


def test_make_cache_returns_the_json_redis_cache():
    cache = make_cache(client=_FakeRedis())
    assert isinstance(cache, RedisCache)
    assert cache._prefix == "sim443:"
