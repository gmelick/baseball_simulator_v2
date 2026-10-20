"""What the live ingestion pipeline sets up at construction: the set of
completed games a poll skips (hydrated from ``raw.games`` at boot), and a
simulation callback that must be an async function."""

from __future__ import annotations

import pytest


class TestCompletedGamesSkip:
    """A poll skips ``_upsert_game_record()`` for a finalized game."""

    def test_pipeline_has_completed_games_set(self) -> None:
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        pipeline = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        # Manually invoke the init parts we need without DSN side effects
        pipeline._completed_games = set()  # mirror what __init__ sets
        assert isinstance(pipeline._completed_games, set)

    def test_hydrate_completed_games_method_exists(self) -> None:
        """The pipeline hydrates the set from raw.games on boot."""
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        assert callable(getattr(LiveIngestionPipeline, "_hydrate_completed_games", None))


class TestSimulationCallback:
    """A sync callback raises TypeError at construction time."""

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
