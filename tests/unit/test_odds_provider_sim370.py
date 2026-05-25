"""
test_odds_provider_sim370.py
============================
SIM-370 — Real odds/prop provider swap behind MockOddsAPI.

Verifies the provider *seam* added in ``pipeline/odds_provider.py``:

  1. ``get_odds_provider()`` returns a ``MockOddsAPI`` by default and respects
     the ``ODDS_PROVIDER`` env var (explicit name + a registered fake select
     correctly; an unknown name raises a clear error).
  2. ``MockOddsAPI`` satisfies the ``OddsProvider`` interface (runtime Protocol
     isinstance check + return-shape parity smoke).
  3. The ``RealOddsAPIProvider`` stub raises the documented not-configured error.
  4. A fake provider can be injected into ``LiveIngestionPipeline`` so its
     provider lookup (``_fetch_odds`` / ``_fetch_prop_odds``) is exercised
     without the mock — and a ``__new__``-built pipeline falls back to the mock.

There is NO real odds API in this environment; SIM-370 delivers the seam, not a
live integration.

Owned by Data Engineer (Agent 4) + Betting Analyst (Agent 8).

Run:
    pytest tests/unit/test_odds_provider_sim370.py -v
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so pipeline imports work
# ---------------------------------------------------------------------------
_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.live.live_ingestion_pipeline import (  # noqa: E402
    PROP_BOOKS,
    PROP_STATS,
    LiveIngestionPipeline,
    MockOddsAPI,
)
from pipeline.odds_provider import (  # noqa: E402
    DEFAULT_PROVIDER,
    ODDS_PROVIDER_ENV,
    OddsProvider,
    RealOddsAPIProvider,
    available_providers,
    get_odds_provider,
    register_odds_provider,
)

# ===========================================================================
# Test fakes
# ===========================================================================


class _FakeProvider:
    """A minimal OddsProvider that tags every quote ``source='fake'``.

    Structurally conforms to ``OddsProvider`` so it is a drop-in source; used to
    prove env selection and pipeline-lookup routing without the mock.
    """

    def get_odds(self, game_pk, **kwargs):  # type: ignore[no-untyped-def]
        return {"game_pk": game_pk, "source": "fake", "is_mock": False}

    def get_prop_odds(self, game_pk, player_id, prop_stat, **kwargs):  # type: ignore[no-untyped-def]
        return {
            "game_pk": game_pk,
            "player_id": player_id,
            "prop_stat": prop_stat,
            "book": kwargs.get("book"),
            "is_sharp_book": kwargs.get("is_sharp_book", False),
            "source": "fake",
            "is_mock": False,
        }


@pytest.fixture
def clean_env(monkeypatch):
    """Ensure ODDS_PROVIDER is unset so the default path is exercised."""
    monkeypatch.delenv(ODDS_PROVIDER_ENV, raising=False)
    return monkeypatch


def _make_pipeline_via_new() -> LiveIngestionPipeline:
    """Construct a pipeline without running __init__ (no DSN/Redis needed).

    Mirrors tests/unit/test_data_engineer_sim340.py — exercises the lazy
    provider fallback in ``_odds_provider()``.
    """
    from unittest.mock import AsyncMock

    p = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
    p._db = AsyncMock()
    p._last_prop_fetch = {}
    return p


# ===========================================================================
# 1. Factory: default + env selection + unknown
# ===========================================================================


class TestFactory:
    def test_default_returns_mock(self, clean_env) -> None:
        provider = get_odds_provider()
        assert isinstance(provider, MockOddsAPI)

    def test_default_provider_name_is_mock(self) -> None:
        assert DEFAULT_PROVIDER == "mock"

    def test_explicit_mock_name(self, clean_env) -> None:
        assert isinstance(get_odds_provider("mock"), MockOddsAPI)

    def test_env_var_selects_provider(self, clean_env) -> None:
        register_odds_provider("envfake", lambda: _FakeProvider())
        clean_env.setenv(ODDS_PROVIDER_ENV, "envfake")
        provider = get_odds_provider()
        assert isinstance(provider, _FakeProvider)
        # env value is case-insensitive
        clean_env.setenv(ODDS_PROVIDER_ENV, "ENVFAKE")
        assert isinstance(get_odds_provider(), _FakeProvider)

    def test_explicit_arg_overrides_env(self, clean_env) -> None:
        clean_env.setenv(ODDS_PROVIDER_ENV, "real")
        # explicit "mock" wins over env "real"
        assert isinstance(get_odds_provider("mock"), MockOddsAPI)

    def test_registered_fake_selected_by_name(self) -> None:
        register_odds_provider("fake_named", lambda: _FakeProvider())
        assert "fake_named" in available_providers()
        assert isinstance(get_odds_provider("fake_named"), _FakeProvider)

    def test_unknown_name_raises_clear_error(self, clean_env) -> None:
        with pytest.raises(RuntimeError) as exc:
            get_odds_provider("definitely_not_registered")
        msg = str(exc.value)
        assert "Unknown ODDS_PROVIDER" in msg
        assert "definitely_not_registered" in msg
        # error lists the known providers so the operator can self-correct
        assert "mock" in msg


# ===========================================================================
# 2. MockOddsAPI conforms to the OddsProvider interface
# ===========================================================================


class TestMockConforms:
    def test_isinstance_protocol(self) -> None:
        # runtime_checkable Protocol — duck-typed presence check
        assert isinstance(MockOddsAPI(), OddsProvider)

    def test_has_required_methods(self) -> None:
        inst = MockOddsAPI()
        assert callable(inst.get_odds)
        assert callable(inst.get_prop_odds)

    def test_get_odds_return_shape(self) -> None:
        odds = MockOddsAPI().get_odds(745000)
        required = {
            "game_pk",
            "source",
            "is_mock",
            "book",
            "line_type",
            "market_type",
            "is_sharp_book",
            "home_ml",
            "away_ml",
            "home_spread",
            "home_spread_ml",
            "away_spread",
            "away_spread_ml",
            "total_line",
            "over_ml",
            "under_ml",
        }
        assert required.issubset(odds.keys())

    def test_get_prop_odds_return_shape(self) -> None:
        prop = MockOddsAPI().get_prop_odds(745000, 101, "strikeouts")
        required = {
            "game_pk",
            "player_id",
            "prop_stat",
            "line",
            "over_ml",
            "under_ml",
            "book",
            "line_type",
            "is_sharp_book",
            "source",
            "is_mock",
        }
        assert required.issubset(prop.keys())

    def test_instance_matches_static(self) -> None:
        # Provider obtained from the factory must behave identically to the
        # static call sites the rest of the codebase still uses.
        prov = get_odds_provider("mock")
        assert prov.get_odds(745000) == MockOddsAPI.get_odds(745000)
        assert prov.get_prop_odds(745000, 101, "hits") == MockOddsAPI.get_prop_odds(
            745000, 101, "hits"
        )

    def test_prop_unknown_stat_still_raises(self) -> None:
        # The provider contract (mirrored by a real provider) raises ValueError
        # on an unknown prop_stat.
        with pytest.raises(ValueError):
            get_odds_provider("mock").get_prop_odds(745000, 101, "nope")


# ===========================================================================
# 3. RealOddsAPIProvider stub
# ===========================================================================


class TestRealStub:
    def test_conforms_to_protocol(self) -> None:
        assert isinstance(RealOddsAPIProvider(), OddsProvider)

    def test_get_odds_raises_not_configured(self) -> None:
        with pytest.raises(RuntimeError) as exc:
            RealOddsAPIProvider().get_odds(745000)
        msg = str(exc.value)
        assert "not yet implemented" in msg
        assert RealOddsAPIProvider.API_KEY_ENV in msg

    def test_get_prop_odds_raises_not_configured(self) -> None:
        with pytest.raises(RuntimeError) as exc:
            RealOddsAPIProvider().get_prop_odds(745000, 101, "strikeouts")
        assert "not yet implemented" in str(exc.value)

    def test_selectable_via_factory(self, clean_env) -> None:
        clean_env.setenv(ODDS_PROVIDER_ENV, "real")
        assert isinstance(get_odds_provider(), RealOddsAPIProvider)


# ===========================================================================
# 4. Pipeline provider lookup (injection + lazy fallback)
# ===========================================================================


class TestPipelineLookup:
    def test_injected_fake_routes_prop_odds(self) -> None:
        pipeline = _make_pipeline_via_new()
        pipeline._odds = _FakeProvider()
        quotes = pipeline._fetch_prop_odds(745000, [101], line_type="current")
        assert quotes, "expected at least one quote"
        assert all(q["source"] == "fake" for q in quotes)
        # books still iterate through PROP_BOOKS even with a fake provider
        assert {q["book"] for q in quotes} == {b for b, _ in PROP_BOOKS}

    def test_injected_fake_routes_game_odds(self) -> None:
        pipeline = _make_pipeline_via_new()
        pipeline._odds = _FakeProvider()
        odds = asyncio.run(pipeline._fetch_odds(745000))
        assert odds["source"] == "fake"

    def test_new_pipeline_falls_back_to_mock(self, clean_env) -> None:
        # __new__ skips __init__, so self._odds is unset; _odds_provider() must
        # lazily resolve to the default mock (keeps SIM-340 tests passing).
        pipeline = _make_pipeline_via_new()
        assert not hasattr(pipeline, "_odds")
        quotes = pipeline._fetch_prop_odds(745000, [101], line_type="current")
        # 1 player * len(PROP_STATS) props * len(PROP_BOOKS) books
        assert len(quotes) == len(PROP_STATS) * len(PROP_BOOKS)
        assert all(q["source"] == "mock" for q in quotes)
        # provider was memoised onto the instance after first lookup
        assert isinstance(pipeline._odds, MockOddsAPI)
