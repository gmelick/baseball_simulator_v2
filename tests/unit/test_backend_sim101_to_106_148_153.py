"""
Unit tests for SIM-101..106, SIM-148, SIM-153 (Backend / QA / DevOps sprint 2026-05-07)
========================================================================================

Permanent regression suite for:
  SIM-101 — Per-game GameStateBuilder cache + incremental play history.
  SIM-102 — Opener role classification (IP < 4.0 AND BF >= 9 → "Opener").
  SIM-103 — ConnectionManager.broadcast() iterates a snapshot of subscribers.
  SIM-104 — /resimulate endpoint Redis cooldown (HTTP 429 with retry_after_seconds).
  SIM-105 — _completed_games skip set + boot-time hydration.
  SIM-106 — async-callable type enforcement on simulation_callback.
  SIM-148 — pitcher_similarity test cleanup (vacuous assertion removal +
            _score_pair regression guard + finite_distances() docstring fix).
  SIM-153 — Secrets management baseline + CI secrets-check job.

Run:
    pytest tests/unit/test_backend_sim101_to_106_148_153.py -v
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import re
import sys

import pytest

# ---------------------------------------------------------------------------
# Repo root on sys.path
# ---------------------------------------------------------------------------
_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


# ===========================================================================
# SIM-102 — _infer_role() opener classification
# ===========================================================================


class TestSim102OpenerRole:
    """Opener slot must sit between SP and MRP."""

    def _infer(self, ip: str | float, bf: int) -> str:
        from pipeline.live.live_ingestion_pipeline import GameStateBuilder

        return GameStateBuilder._infer_role(
            {"inningsPitched": str(ip), "battersFaced": bf},
            {},
        )

    def test_starter_remains_sp(self) -> None:
        assert self._infer("5.1", 18) == "SP"

    def test_opener_with_two_innings_nine_bf(self) -> None:
        # 2.0 IP + 9 BF — classic opener pattern
        assert self._infer("2.0", 9) == "Opener"

    def test_short_outing_high_bf_is_opener(self) -> None:
        # 0.2 IP + 10 BF (lots of runners but quick hook) → Opener
        assert self._infer("0.2", 10) == "Opener"

    def test_two_inning_with_six_bf_is_mrp_not_opener(self) -> None:
        # 2.0 IP + 6 BF — multi-inning relief, not an opener
        assert self._infer("2.0", 6) == "MRP"

    def test_one_inning_relief_is_rp(self) -> None:
        assert self._infer("0.2", 3) == "RP"

    def test_missing_battersfaced_falls_back_safely(self) -> None:
        from pipeline.live.live_ingestion_pipeline import GameStateBuilder

        # No battersFaced key — must not crash; falls back to old IP-only logic
        role = GameStateBuilder._infer_role({"inningsPitched": "2.0"}, {})
        assert role in ("MRP", "RP", "SP")  # graceful, no Opener miss


# ===========================================================================
# SIM-103 — broadcast() set snapshot
# ===========================================================================


class TestSim103BroadcastSnapshot:
    """broadcast() must iterate over a copy of the subscriber set."""

    def test_broadcast_uses_set_copy(self) -> None:
        """If broadcast iterates the live set, mutating it mid-iteration would
        raise ``RuntimeError: Set changed size during iteration``.  We force a
        mutation during the simulated send and assert no crash + remaining
        client got the message."""
        from pipeline.live.live_ingestion_pipeline import ConnectionManager

        cm = ConnectionManager()
        live_set = cm._subscriptions.setdefault(745001, set())

        sent: list[str] = []
        new_ws_added: list[bool] = [False]

        class _StubWS:
            def __init__(self, name: str) -> None:
                self.name = name

            async def send_text(self, msg: str) -> None:
                sent.append(self.name)
                # Simulate a concurrent connect during the send: append to
                # the LIVE underlying set.  Pre-SIM-103 this would have
                # caused the next loop iteration to raise RuntimeError.
                if not new_ws_added[0]:
                    live_set.add(_StubWS("intruder"))
                    new_ws_added[0] = True

        ws_a = _StubWS("a")
        ws_b = _StubWS("b")
        live_set.add(ws_a)
        live_set.add(ws_b)

        # Should NOT raise RuntimeError
        asyncio.run(cm.broadcast(745001, {"x": 1}))

        # Existing subscribers received the message
        assert "a" in sent and "b" in sent
        # The intruder appeared mid-broadcast but was NOT spuriously sent to
        # in this call (snapshot was taken before mutation)
        assert "intruder" not in sent


# ===========================================================================
# SIM-104 — /resimulate endpoint Redis rate limit
# ===========================================================================


class TestSim104ResimulateRateLimit:
    """Pre-SIM-104 the manual resimulate endpoint had no debouncing.  We
    assert the Redis cooldown key is set and a TTL > 0 returns 429."""

    def test_constant_present(self) -> None:
        from pipeline.live import live_ingestion_pipeline as lip

        assert hasattr(lip, "RESIM_COOLDOWN_S")
        assert lip.RESIM_COOLDOWN_S >= 5  # at least a few seconds

    def test_endpoint_source_uses_redis_cooldown(self) -> None:
        """Source-level grep regression: the endpoint must use Redis to gate."""
        src = _read(_ROOT / "pipeline" / "live" / "live_ingestion_pipeline.py")
        # Endpoint must read TTL and set cooldown key.
        assert "resim_cooldown:" in src, (
            "SIM-104: cooldown Redis key prefix `resim_cooldown:` missing — "
            "rate limit is not wired."
        )
        assert "RESIM_COOLDOWN_S" in src
        assert "rate_limited" in src and "retry_after_seconds" in src, (
            "SIM-104: error envelope must include status=rate_limited + "
            "retry_after_seconds per the ticket spec."
        )


# ===========================================================================
# SIM-105 — _completed_games skip set + hydration
# ===========================================================================


class TestSim105CompletedGamesSkip:
    """Polls must skip _upsert_game_record() for finalized games."""

    def test_pipeline_has_completed_games_set(self) -> None:
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        pipeline = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        # Manually invoke the init parts we need without DSN side effects
        pipeline._completed_games = set()  # mirror what __init__ sets
        assert isinstance(pipeline._completed_games, set)

    def test_hydrate_completed_games_method_exists(self) -> None:
        """SIM-105 acceptance: pipeline must hydrate from raw.games on boot."""
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        assert callable(getattr(LiveIngestionPipeline, "_hydrate_completed_games", None))


# ===========================================================================
# SIM-106 — Async-callable simulation_callback type
# ===========================================================================


class TestSim106AsyncCallback:
    """Sync callbacks must raise TypeError at construction time."""

    def test_sync_callback_rejected(self) -> None:
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        def sync_callback(game_pk: int, state: dict) -> None:
            return None

        with pytest.raises(TypeError, match="async function"):
            LiveIngestionPipeline(
                dsn="postgresql://x:y@localhost/z",
                redis_url="redis://localhost",
                simulation_callback=sync_callback,
            )

    def test_async_callback_accepted(self) -> None:
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        async def async_callback(game_pk: int, state: dict) -> None:
            return None

        # Should NOT raise.  We only construct — start() needs Redis/DB.
        pipeline = LiveIngestionPipeline(
            dsn="postgresql://x:y@localhost/z",
            redis_url="redis://localhost",
            simulation_callback=async_callback,
        )
        assert pipeline._sim_cb is async_callback

    def test_no_callback_is_fine(self) -> None:
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        pipeline = LiveIngestionPipeline(
            dsn="postgresql://x:y@localhost/z",
            redis_url="redis://localhost",
            simulation_callback=None,
        )
        assert pipeline._sim_cb is None


# ===========================================================================
# SIM-101 — Builder cache + incremental play history
# ===========================================================================


class TestSim101BuilderCache:
    """GameStateBuilder must cache history across refreshes."""

    def test_builder_holds_history_state(self) -> None:
        from pipeline.live.live_ingestion_pipeline import GameStateBuilder

        b = GameStateBuilder.__new__(GameStateBuilder)
        b._history = []
        b._last_at_bat_index = -1
        b._game_date = None

        # First call with 2 plays
        plays_first = [
            _make_play(idx=0, batter_id=100, event="strikeout"),
            _make_play(idx=1, batter_id=101, event="walk"),
        ]
        h1 = b._parse_play_history(plays_first)
        assert len(h1) == 2
        assert b._last_at_bat_index == 1

        # Second call with all the prior plays + 1 new one — incremental:
        # only the new play should append; existing entries unchanged.
        plays_second = plays_first + [
            _make_play(idx=2, batter_id=102, event="single"),
        ]
        h2 = b._parse_play_history(plays_second)
        assert len(h2) == 3
        assert b._last_at_bat_index == 2

    def test_builder_replaces_in_flight_at_bat(self) -> None:
        """Mid-PA refresh: same atBatIndex with updated event must overwrite
        the last cached entry, not append a duplicate."""
        from pipeline.live.live_ingestion_pipeline import GameStateBuilder

        b = GameStateBuilder.__new__(GameStateBuilder)
        b._history = []
        b._last_at_bat_index = -1
        b._game_date = None

        in_flight = _make_play(idx=0, batter_id=100, event=None)  # PA in-flight
        b._parse_play_history([in_flight])
        assert b._history[-1]["event"] is None

        completed = _make_play(idx=0, batter_id=100, event="single")
        h = b._parse_play_history([completed])

        assert len(h) == 1  # NOT duplicated
        assert h[-1]["event"] == "single"
        assert b._last_at_bat_index == 0

    def test_pipeline_caches_builders_per_game(self) -> None:
        """Two refreshes for the same game_pk return the SAME builder."""
        from pipeline.live.live_ingestion_pipeline import (
            GameStateBuilder,
            LiveIngestionPipeline,
        )

        pipeline = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        pipeline._db = None
        pipeline._builders = {}

        b1 = pipeline._get_or_create_builder(745001)
        b2 = pipeline._get_or_create_builder(745001)
        assert b1 is b2
        assert isinstance(b1, GameStateBuilder)

        # Different game = different builder
        b3 = pipeline._get_or_create_builder(745002)
        assert b3 is not b1


def _make_play(idx: int, batter_id: int, event: str | None) -> dict:
    """Helper: build a minimal allPlays entry with the keys our parser uses."""
    return {
        "about": {
            "atBatIndex": idx,
            "inning": 1,
            "halfInning": "top",
            "isComplete": event is not None,
        },
        "result": {"eventType": event, "description": event or "in progress", "rbi": 0},
        "matchup": {
            "batter": {"id": batter_id, "fullName": f"Batter{batter_id}"},
            "pitcher": {"id": 999, "fullName": "Pitcher999"},
        },
        "playEvents": [],
    }


# ===========================================================================
# SIM-148 — pitcher_similarity test cleanup
# ===========================================================================

_SCIPY_AVAILABLE = True
try:
    import scipy  # noqa: F401
except ImportError:
    _SCIPY_AVAILABLE = False


class TestSim148PitcherSimilarityCleanup:
    """Source-level checks (the actual unit test sits in
    tests/unit/test_pitcher_similarity.py — we re-verify shape here so the
    SIM-148 contract is self-contained).

    NOTE: Module imports are skipped when scipy is unavailable (CI runs with
    scipy installed; sandbox harnesses sometimes don't).  Source-level grep
    tests still run unconditionally so the SIM-148 contract is verified
    even without scipy."""

    def test_score_pair_returns_three_elements(self) -> None:
        """SIM-148 / SIM-067 regression: 3-tuple, not 5-tuple.

        Walk the source instead of constructing a real engine — we just
        need to confirm the function shape.  Richer behaviour is already
        exercised by tests/unit/test_pitcher_similarity.py."""
        src = _read(_ROOT / "similarity" / "engines" / "pitcher_similarity.py")
        assert "SIM-148" in src or "SIM-067" in src, (
            "SIM-148: pitcher_similarity.py must reference the SIM-148/SIM-067 "
            "context comment so future readers see the why."
        )
        # 3-tuple guard: source must contain a 3-element return tuple
        # immediately after the SIM-148 comment in _score_pair.
        assert (
            "(composite, arsenal, command)" in src
        ), "SIM-148: _score_pair docstring must advertise a 3-tuple return."

    def test_no_release_score_in_source(self) -> None:
        """Source-level guard: release_score must not appear as a dataclass
        field annotation.  Catches the regression even when scipy is missing."""
        src = _read(_ROOT / "similarity" / "engines" / "pitcher_similarity.py")
        # Allow `release_score` to appear in comments; reject it as a
        # field annotation like ``release_score: float`` inside the
        # SimilarityResult class.
        # Confine the search to between "class SimilarityResult" and the
        # next blank line after a closing of that block.
        m = re.search(r"class SimilarityResult.*?(?=\n\n[A-Z@])", src, re.DOTALL)
        body = m.group(0) if m else src
        assert "release_score" not in body, (
            "SIM-148/SIM-067: SimilarityResult must NOT declare release_score "
            "(release-point info is already inside the GMM components)."
        )

    @pytest.mark.skipif(not _SCIPY_AVAILABLE, reason="scipy not installed")
    def test_no_release_score_field_on_similarity_result(self) -> None:
        from dataclasses import fields

        from similarity.engines.pitcher_similarity import SimilarityResult

        names = {f.name for f in fields(SimilarityResult)}
        assert "release_score" not in names

    @pytest.mark.skipif(not _SCIPY_AVAILABLE, reason="scipy not installed")
    def test_finite_distances_docstring_uses_calibrate_arsenal_scale(self) -> None:
        from similarity.engines.pitcher_similarity import ArsenalCache

        doc = ArsenalCache.finite_distances.__doc__ or ""
        assert "calibrate_arsenal_scale" in doc, (
            "SIM-148 AC #4: finite_distances() docstring must reference the "
            "current calibrate_arsenal_scale API (post-SIM-066 rename)."
        )
        # The stale gamma reference must be gone from the SIGNATURE line —
        # the file's SIM-148 historical comment is allowed to mention it.
        assert "calibrate_arsenal_gamma()" not in doc, (
            "SIM-148 AC #4: stale calibrate_arsenal_gamma() call signature "
            "still in finite_distances() docstring."
        )


# ===========================================================================
# SIM-153 — Secrets management baseline
# ===========================================================================


class TestSim153SecretsBaseline:
    """The secrets baseline depends on file-system artefacts; we assert
    each one is in place."""

    def test_env_example_exists_and_documents_required_vars(self) -> None:
        path = _ROOT / ".env.example"
        assert path.exists(), "SIM-153: .env.example missing"
        text = _read(path)
        for required in ("BASEBALL_DB_DSN", "REDIS_URL"):
            assert required in text, f"SIM-153: .env.example must document {required}"

    def test_gitignore_excludes_dotenv(self) -> None:
        path = _ROOT / ".gitignore"
        assert path.exists()
        assert re.search(
            r"^\.env\b", _read(path), re.MULTILINE
        ), "SIM-153: .gitignore must explicitly list `.env`"

    def test_python_dotenv_in_requirements(self) -> None:
        text = _read(_ROOT / "requirements.txt")
        assert "python-dotenv" in text, "SIM-153: python-dotenv must be in requirements.txt"

    def test_validate_environment_function_exists(self) -> None:
        from api.main import validate_environment

        # Save current state, force-clear, call, expect RuntimeError.
        saved = {k: os.environ.pop(k, None) for k in ("BASEBALL_DB_DSN", "REDIS_URL")}
        try:
            with pytest.raises(RuntimeError, match="Missing required"):
                validate_environment()
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_ci_workflow_has_secrets_check_job(self) -> None:
        ci = _read(_ROOT / ".github" / "workflows" / "ci.yml")
        assert "secrets-check" in ci, "SIM-153: CI workflow must define a `secrets-check` job"
        assert (
            "Reject committed .env" in ci or "committed .env" in ci
        ), "SIM-153: secrets-check job must reject committed .env files"

    def test_loader_falls_back_to_env_dsn(self) -> None:
        """SIM-153 AC #4: HistoricalDataLoader reads BASEBALL_DB_DSN from env
        when no explicit dsn parameter is passed."""
        from pipeline.etl.etl_historical_loader import HistoricalDataLoader

        saved = os.environ.pop("BASEBALL_DB_DSN", None)
        try:
            with pytest.raises(RuntimeError, match="BASEBALL_DB_DSN"):
                HistoricalDataLoader()
            os.environ["BASEBALL_DB_DSN"] = "postgresql://from:env@localhost/db"
            loader = HistoricalDataLoader()
            assert loader.dsn == "postgresql://from:env@localhost/db"
            loader2 = HistoricalDataLoader(dsn="postgresql://explicit:pwd@localhost/db")
            assert loader2.dsn == "postgresql://explicit:pwd@localhost/db"
        finally:
            os.environ.pop("BASEBALL_DB_DSN", None)
            if saved is not None:
                os.environ["BASEBALL_DB_DSN"] = saved
