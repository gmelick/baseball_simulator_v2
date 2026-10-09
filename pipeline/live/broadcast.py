"""The bridge between the live service and the browsers (SIM-519 Part C).

The live service runs in its own container (decision D2), but the browsers'
WebSockets end in the app. A Redis channel carries every browser-bound message
across:

* the live service publishes each message on ``live:game:{game_pk}``
  (:class:`RedisBroadcaster`);
* the app subscribes to a game's channel while at least one browser watches
  that game, and forwards each message to the game's local sockets
  (:class:`RedisBridge`). The last browser to leave stops the subscription.

The live service also keeps two keys fresh on every schedule poll:
``live:heartbeat`` (the poll time; 90-second TTL) and ``live:watching`` (the
live game_pks). The app's ``/ready`` and the freshness gauge read the
heartbeat's age (:func:`heartbeat_status`).

Nothing is stored on the channel: a browser that connects late gets the next
message, and the game page reads ``/live`` for the current state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)

CHANNEL = "live:game:{game_pk}"
HEARTBEAT_KEY = "live:heartbeat"
WATCHING_KEY = "live:watching"
HEARTBEAT_TTL_S = 90
#: A heartbeat older than this reads "stale" (three missed 30-second polls).
HEARTBEAT_STALE_S = 90


def channel(game_pk: int) -> str:
    return CHANNEL.format(game_pk=int(game_pk))


class Broadcaster(Protocol):
    async def broadcast(self, game_pk: int, payload: dict) -> None: ...


class RedisBroadcaster:
    """Publishes browser-bound messages on the game's Redis channel."""

    def __init__(self, redis: Any, *, default: Callable[[Any], Any] | None = None) -> None:
        self._redis = redis
        self._default = default

    async def broadcast(self, game_pk: int, payload: dict) -> None:
        message = json.dumps(payload, default=self._default)
        try:
            await self._redis.publish(channel(game_pk), message)
        except Exception as exc:  # noqa: BLE001 -- a lost message is the next poll's
            log.warning("live: publish failed for game %s: %s", game_pk, exc)


async def write_heartbeat(
    redis: Any, live_game_pks: list[int], *, now: float | None = None
) -> None:
    """The live service's heartbeat and its list of live games (90-second TTL)."""
    stamp = time.time() if now is None else now
    await redis.set(HEARTBEAT_KEY, str(stamp), ex=HEARTBEAT_TTL_S)
    await redis.set(
        WATCHING_KEY, json.dumps(sorted(int(p) for p in live_game_pks)), ex=HEARTBEAT_TTL_S
    )


async def heartbeat_age_s(redis: Any, *, now: float | None = None) -> float | None:
    """Seconds since the live service's last poll; None when it never wrote one (or it expired)."""
    raw = await redis.get(HEARTBEAT_KEY)
    if raw is None:
        return None
    try:
        return max(0.0, (time.time() if now is None else now) - float(raw))
    except (TypeError, ValueError):
        return None


async def heartbeat_status(redis: Any) -> str:
    """``ok``, ``stale`` or ``absent`` for ``/ready``."""
    try:
        age = await heartbeat_age_s(redis)
    except Exception:  # noqa: BLE001
        return "absent"
    if age is None:
        return "absent"
    return "ok" if age <= HEARTBEAT_STALE_S else "stale"


Forward = Callable[[int, dict], Awaitable[None]]


class RedisBridge:
    """The app side: one Redis subscription per watched game, reference-counted."""

    def __init__(self, redis: Any, forward: Forward) -> None:
        self._redis = redis
        self._forward = forward
        self._watchers: dict[int, int] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}

    async def retain(self, game_pk: int) -> None:
        """A browser started watching ``game_pk``; the first one subscribes."""
        n = self._watchers.get(game_pk, 0) + 1
        self._watchers[game_pk] = n
        if n == 1:
            self._tasks[game_pk] = asyncio.create_task(self._pump(game_pk))

    async def release(self, game_pk: int) -> None:
        """A browser left; the last one stops the subscription."""
        n = self._watchers.get(game_pk, 0) - 1
        if n > 0:
            self._watchers[game_pk] = n
            return
        self._watchers.pop(game_pk, None)
        task = self._tasks.pop(game_pk, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def watching(self) -> dict[int, int]:
        return dict(self._watchers)

    async def close(self) -> None:
        for pk in list(self._tasks):
            self._watchers[pk] = 1
            await self.release(pk)

    async def _pump(self, game_pk: int) -> None:
        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(channel(game_pk))
            while True:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg is None:
                    continue
                data = msg.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                try:
                    payload = json.loads(data)
                except (TypeError, ValueError):
                    continue
                await self._forward(game_pk, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("live bridge: game %s subscription ended: %s", game_pk, exc)
        finally:
            try:
                await pubsub.unsubscribe(channel(game_pk))
                await pubsub.aclose()
            except Exception:  # noqa: BLE001
                pass
