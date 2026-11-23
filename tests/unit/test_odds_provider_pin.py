"""
test_odds_provider_pin.py
================================
The test lane holds no vendor key (SIM-567, SIM-645).

The unit lane runs inside the app container, which reads the stack's .env file.
``tests/conftest.py`` removes ``ODDS_API_KEY``. A pipeline built with ``__new__``
makes the BettingPros provider on first use; without a key, that provider refuses
every request before it leaves the process. These tests prove three things:

  1. The test process holds no vendor key.
  2. A bare pipeline makes the BettingPros provider on its first lookup.
  3. The vendor's request seam (``_bp_get``) refuses to send without the key, and
     the conftest keeps the line that removes the key.

No test here opens a database or a socket.
"""

from __future__ import annotations

import os
import pathlib
import sys
import urllib.request
from unittest.mock import AsyncMock

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from betting.bettingpros.fetch_odds import BettingProsOddsProvider  # noqa: E402
from betting.load_odds_to_db import odds_source  # noqa: E402
from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline  # noqa: E402

# The environment name comes from its reader, so a rename fails the conftest check.
_KEY_ENV = BettingProsOddsProvider.API_KEY_ENV
_CONFTEST = _ROOT / "tests" / "conftest.py"
_POP_LINE = f'os.environ.pop("{_KEY_ENV}", None)'


def _make_pipeline_via_new() -> LiveIngestionPipeline:
    """Build a pipeline without ``__init__``: it holds no ``_odds`` attribute."""
    p = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
    p._db = AsyncMock()
    p._last_prop_fetch = {}
    return p


def test_the_lane_holds_no_vendor_key() -> None:
    """The test process holds no ``ODDS_API_KEY``."""
    assert _KEY_ENV not in os.environ, f"tests/conftest.py must remove {_KEY_ENV}"


def test_a_bare_pipeline_makes_the_bettingpros_provider() -> None:
    """A pipeline built with ``__new__`` makes the provider once and keeps it."""
    p = _make_pipeline_via_new()
    assert getattr(p, "_odds", None) is None
    provider = p._odds_provider()
    assert isinstance(provider, BettingProsOddsProvider), type(provider).__name__
    assert p._odds is provider
    assert p._odds_provider() is provider


def test_the_vendor_seam_refuses_without_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The vendor's request seam with no key raises before its request leaves.

    Two stubs fail the test if a request goes first: one on the provider's
    network seam, one on ``urllib.request.urlopen``.
    """
    monkeypatch.delenv(_KEY_ENV, raising=False)
    provider = odds_source()
    assert isinstance(provider, BettingProsOddsProvider), type(provider).__name__

    def _no_request(*args: object, **kwargs: object) -> dict:
        raise AssertionError("a request left the process")

    monkeypatch.setattr(provider, "_http_get_json", _no_request)
    monkeypatch.setattr(urllib.request, "urlopen", _no_request)
    with pytest.raises(RuntimeError, match=_KEY_ENV):
        provider._bp_get("x", {})


def test_the_conftest_keeps_the_key_line() -> None:
    """``tests/conftest.py`` holds the line that removes the key, as live code."""
    lines = {ln.strip() for ln in _CONFTEST.read_text(encoding="utf-8").splitlines()}
    assert _POP_LINE in lines, f"tests/conftest.py lacks the line: {_POP_LINE}"
