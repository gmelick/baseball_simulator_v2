"""The live service's manual resimulate endpoint is rate limited: a Redis
cooldown key per game, and an HTTP 429 envelope with ``retry_after_seconds``
while the key lives."""

from __future__ import annotations

import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


class TestResimulateCooldown:
    """The cooldown constant exists and the endpoint gates on the Redis key."""

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
