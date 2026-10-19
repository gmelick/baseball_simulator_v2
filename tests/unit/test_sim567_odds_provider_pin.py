"""
test_sim567_odds_provider_pin.py
================================
The test lane's odds provider pin (SIM-567).

The unit lane runs inside the app container. That container reads the stack's
.env file. The file names the vendor: ODDS_PROVIDER=bettingpros. Twelve
unit tests build the live pipeline with ``__new__`` (a build that skips the
constructor) and set no provider. The pipeline's lazy fallback (the provider
lookup on first use) then reads the environment. Before the pin, those tests
built the vendor's provider and called the vendor.

``tests/conftest.py`` now pins ``ODDS_PROVIDER`` to "mock" and removes
``ODDS_API_KEY``. These tests prove four things:

  1. The pin reaches the test process, and the process holds no vendor key.
  2. The provider factory and a bare pipeline both answer the mock.
  3. A test that still names the vendor fails before any request leaves.
  4. The conftest keeps both pinned lines.

No test here opens a database or a socket.

Run:
    pytest tests/unit/test_sim567_odds_provider_pin.py -v
"""

from __future__ import annotations

import os
import pathlib
import sys
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so pipeline imports work
# ---------------------------------------------------------------------------
_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.bettingpros_odds_provider import BettingProsOddsProvider  # noqa: E402
from pipeline.live.live_ingestion_pipeline import (  # noqa: E402
    LiveIngestionPipeline,
    MockOddsAPI,
)
from pipeline.odds_provider import ODDS_PROVIDER_ENV, get_odds_provider  # noqa: E402

# The two environment names come from their readers. A rename in a reader
# then fails the conftest check below. A stale pin never passes unseen.
_KEY_ENV = BettingProsOddsProvider.API_KEY_ENV
_CONFTEST = _ROOT / "tests" / "conftest.py"
_PIN_LINE = f'os.environ["{ODDS_PROVIDER_ENV}"] = "mock"'
_POP_LINE = f'os.environ.pop("{_KEY_ENV}", None)'


def _make_pipeline_via_new() -> LiveIngestionPipeline:
    """Build a pipeline without ``__init__`` (no DSN, no Redis).

    The helper copies the twelve tests' exact path: the pipeline holds no
    ``_odds`` attribute, so ``_odds_provider()`` runs its lazy fallback.
    """
    p = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
    p._db = AsyncMock()
    p._last_prop_fetch = {}
    return p


def test_the_lane_pins_the_mock() -> None:
    """The conftest pin reaches this process: ``ODDS_PROVIDER`` reads "mock".

    The test lane's odds provider pin (SIM-567) beats the vendor name that
    the app container reads from the stack's .env file.
    """
    value = os.environ.get(ODDS_PROVIDER_ENV)
    assert value == "mock", (
        f"{ODDS_PROVIDER_ENV} reads {value!r}; tests/conftest.py must pin it to 'mock'"
    )


def test_the_lane_holds_no_vendor_key() -> None:
    """The test process holds no ``ODDS_API_KEY``.

    The test lane's odds provider pin (SIM-567) removes the key. A test that
    builds the vendor's provider then has no key to send.
    """
    assert _KEY_ENV not in os.environ, f"tests/conftest.py must remove {_KEY_ENV}"


def test_the_factory_answers_the_mock() -> None:
    """``get_odds_provider()`` with no name returns the mock under the pin."""
    provider = get_odds_provider()
    assert isinstance(provider, MockOddsAPI), type(provider).__name__


def test_a_bare_pipeline_draws_the_mock() -> None:
    """A pipeline built with ``__new__`` draws the mock on its first lookup.

    The twelve tests behind the test lane's odds provider pin (SIM-567) take
    this path. The pipeline also keeps the provider for its next lookup.
    """
    p = _make_pipeline_via_new()
    assert getattr(p, "_odds", None) is None
    provider = p._odds_provider()
    assert isinstance(provider, MockOddsAPI), type(provider).__name__
    assert p._odds is provider


def test_the_vendor_provider_fails_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """The vendor's provider with no key raises before any request leaves.

    The test names the vendor the way the stack's .env file does. The
    provider's key check raises a ``RuntimeError`` that names ``ODDS_API_KEY``.
    The stub on the network seam fails the test if a request ever goes first.
    """
    monkeypatch.setenv(ODDS_PROVIDER_ENV, "bettingpros")
    monkeypatch.delenv(_KEY_ENV, raising=False)
    provider = get_odds_provider()
    assert isinstance(provider, BettingProsOddsProvider), type(provider).__name__

    def _no_request(url: str, headers: dict[str, str] | None = None) -> dict:
        raise AssertionError("a request left the process")

    monkeypatch.setattr(provider, "_http_get_json", _no_request)
    with pytest.raises(RuntimeError, match=_KEY_ENV):
        provider._bp_get("x", {})


def test_the_conftest_keeps_both_lines() -> None:
    """``tests/conftest.py`` holds both pinned lines as live code.

    The check compares whole lines with the indent removed. A commented-out
    line does not count. The test lane's odds provider pin (SIM-567) needs
    both lines: the pin of the mock and the removal of the key.
    """
    lines = {ln.strip() for ln in _CONFTEST.read_text(encoding="utf-8").splitlines()}
    for expected in (_PIN_LINE, _POP_LINE):
        assert expected in lines, f"tests/conftest.py lacks the line: {expected}"
