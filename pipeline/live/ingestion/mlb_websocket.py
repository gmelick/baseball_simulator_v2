"""The WebSocket parts: the client of MLB's live game socket and the manager of
the app's own subscribers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import (
    Callable,
    Coroutine,
)
from typing import Any

import websockets
import websockets.exceptions
from fastapi import WebSocket

from pipeline.live.ingestion.constants import (
    MLB_WS_TEMPLATE,
    WS_RECONNECT_BASE,
    WS_RECONNECT_MAX,
)
from pipeline.live.ingestion.odds_rows import _json_default

log = logging.getLogger("live_ingestion")


# ---------------------------------------------------------------------------
# MLB Game WebSocket Client
# ---------------------------------------------------------------------------


class MLBGameWebSocket:
    """
    Subscribes to the MLB gameday push feed for a single game_pk.

    The MLB WebSocket sends a message whenever the game state changes
    (new pitch, substitution, scoring play, etc.). The message itself
    is treated as a pure signal — the actual state is always fetched
    from the REST API on receipt, ensuring we never depend on the WS
    payload format being stable.

    Reconnect strategy: exponential backoff starting at WS_RECONNECT_BASE
    seconds, capped at WS_RECONNECT_MAX seconds.
    """

    def __init__(
        self,
        game_pk: int,
        on_update: Callable[
            [int], Coroutine[Any, Any, None]
        ],  # async callback(game_pk: int) -> None
    ) -> None:
        self.game_pk = game_pk
        self.on_update = on_update
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"ws-game-{self.game_pk}")

    def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self) -> None:
        url = MLB_WS_TEMPLATE.format(game_pk=self.game_pk)
        backoff = WS_RECONNECT_BASE

        while not self._stop.is_set():
            try:
                log.info("WS connecting: game %s", self.game_pk)
                async with websockets.connect(url, ping_interval=30, ping_timeout=10) as ws:
                    log.info("WS connected:  game %s", self.game_pk)
                    backoff = WS_RECONNECT_BASE  # reset on successful connect
                    async for message in ws:
                        if self._stop.is_set():
                            break
                        log.debug("WS message: game %s — %s", self.game_pk, message[:120])
                        # Any message is a change signal — fire the update callback
                        asyncio.create_task(self.on_update(self.game_pk))

            except asyncio.CancelledError:
                log.info("WS cancelled: game %s", self.game_pk)
                return
            except websockets.exceptions.ConnectionClosedOK:
                log.info("WS closed normally: game %s", self.game_pk)
                return  # game probably ended
            except Exception as exc:
                if self._stop.is_set():
                    return
                log.warning(
                    "WS error game %s (%s) — reconnecting in %.1fs", self.game_pk, exc, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, WS_RECONNECT_MAX)


# ---------------------------------------------------------------------------
# Frontend WebSocket Connection Manager
# ---------------------------------------------------------------------------


class ConnectionManager:
    """
    Manages frontend WebSocket clients subscribed to individual game channels.
    Extends the existing dashboard's WS infrastructure.

    Clients connect to:  ws://host/ws/games/{game_pk}
    """

    def __init__(self) -> None:
        # game_pk → set of connected WebSocket objects
        self._subscriptions: dict[int, set[WebSocket]] = {}

    async def connect(self, game_pk: int, ws: WebSocket) -> None:
        await ws.accept()
        self._subscriptions.setdefault(game_pk, set()).add(ws)
        log.info(
            "Frontend WS connected: game %s (%d subscribers)",
            game_pk,
            len(self._subscriptions[game_pk]),
        )

    def disconnect(self, game_pk: int, ws: WebSocket) -> None:
        subs = self._subscriptions.get(game_pk, set())
        subs.discard(ws)
        log.info("Frontend WS disconnected: game %s (%d remaining)", game_pk, len(subs))

    async def broadcast(self, game_pk: int, payload: dict) -> None:
        """Send a JSON payload to all clients watching this game.

        SIM-103: Iterate over a SHALLOW COPY of the subscription set.
        ``await ws.send_text()`` yields control, so a concurrent connect()
        or disconnect() on the same game_pk would mutate the original set
        mid-loop and raise ``RuntimeError: Set changed size during iteration``.
        Copy is O(N); the receive set rarely exceeds a few dozen connections.
        Dead-connection cleanup still applies to the live underlying set.

        SIM-555: a date or a datetime in the payload goes out as ISO-8601 text
        (the by-book odds rows carry ``book_line_at`` as a datetime); the
        refresh loop also strips the odds row before it gets here.
        """
        live_subs = self._subscriptions.get(game_pk, set())
        if not live_subs:
            return
        subs_snapshot = set(live_subs)  # SIM-103: iteration-safe snapshot
        dead: set[WebSocket] = set()
        message = json.dumps(payload, default=_json_default)
        for ws in subs_snapshot:
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)
        # Apply cleanup to the live set (which may have changed during the
        # broadcast).  discard() is idempotent and safe even if a disconnect
        # already removed the ws.
        for ws in dead:
            live_subs.discard(ws)

    def subscriber_count(self, game_pk: int) -> int:
        return len(self._subscriptions.get(game_pk, set()))
