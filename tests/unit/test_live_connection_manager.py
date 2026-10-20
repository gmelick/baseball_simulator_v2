"""The live service's WebSocket connection manager: ``broadcast`` iterates a
snapshot of a game's subscribers, so a client that connects during a send
cannot break the loop."""

from __future__ import annotations

import asyncio


class TestBroadcastSnapshot:
    """broadcast() iterates over a copy of the subscriber set."""

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
                # Simulate a concurrent connect during the send: add to the
                # LIVE underlying set. Iterating that set would raise on the
                # next loop step.
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
