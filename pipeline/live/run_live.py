"""The live service's entry point (SIM-519 Part C; decision D2).

``python -m pipeline.live.run_live`` runs the live ingestion pipeline in its own
container (the compose ``live`` service): the schedule poll, the per-game push
subscriptions, the published-lineup writer and the pre-game odds cycles. It
publishes every browser-bound message on Redis; the app's bridge forwards it.

Before it starts, it waits up to ten minutes for Postgres to accept a
connection: after a host reboot the database recovers after the containers
start. It serves ``/health`` (the last poll's age) and ``/api/pipeline/status``
on port 8001 (``LIVE_PORT``) and stops cleanly on SIGTERM (uvicorn's handler).

Never set ``LIVE_PIPELINE_ENABLED`` in the app when this service runs: two
publishers would double-write every table.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

log = logging.getLogger("pipeline.live.run_live")

DB_WAIT_S = 600


async def wait_for_postgres(
    dsn: str, *, timeout_s: float = DB_WAIT_S, interval_s: float = 5.0
) -> None:
    """Return once Postgres accepts a connection; raise after ``timeout_s``."""
    import asyncpg

    deadline = time.monotonic() + timeout_s
    attempt = 0
    while True:
        attempt += 1
        try:
            conn = await asyncpg.connect(dsn, timeout=10)
            await conn.close()
            return
        except Exception as exc:  # noqa: BLE001
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Postgres did not accept a connection in {timeout_s:.0f}s"
                ) from exc
            log.info("waiting for Postgres (attempt %d): %s", attempt, exc)
            await asyncio.sleep(interval_s)


def main() -> None:
    import uvicorn

    from pipeline.live.live_ingestion_pipeline import create_app

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    dsn = os.environ["BASEBALL_DB_DSN"]
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    asyncio.run(wait_for_postgres(dsn))
    app = create_app(dsn=dsn, redis_url=redis_url, publish_to_redis=True)
    uvicorn.run(
        app, host="0.0.0.0", port=int(os.environ.get("LIVE_PORT", "8001")), log_level="info"
    )


if __name__ == "__main__":
    main()
