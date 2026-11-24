"""The one MLB Stats API client: the sync GET, its retry policy and the async reader.

The sync GET is tested with ``urllib.request.urlopen`` faked, so the tests cover
the real request building, the retry loop and the decoding together. One test
runs a real local server to pin what CPython sends.
"""

from __future__ import annotations

import http.client
import inspect
import logging
import urllib.error
from email.message import Message
from unittest.mock import MagicMock, patch

import pytest

from ingest.games import load_games
from ingest.mlb import client
from ingest.mlb.client import RetryPolicy


def _ok(body: bytes = b"{}", headers: dict[str, str] | None = None) -> MagicMock:
    """A stand-in for urlopen's context-manager response."""
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = body
    resp.headers = headers if headers is not None else {"Content-Type": "application/json"}
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, *a: False
    return resp


def _http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    hdrs = Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    err = urllib.error.HTTPError("http://example.com", code, "status", hdrs, None)
    err.close = lambda: None  # type: ignore[method-assign]
    return err


class TestRetryPolicy:
    def test_the_wait_doubles_and_stops_at_the_cap(self) -> None:
        policy = RetryPolicy(retries=8, first_wait_s=2.0, cap_s=60.0)
        exc = ConnectionError("down")
        assert [policy.wait_s(n, exc) for n in range(1, 8)] == [2, 4, 8, 16, 32, 60, 60]

    def test_a_numeric_retry_after_sets_a_floor_on_a_429_or_503(self) -> None:
        policy = RetryPolicy(retries=3, first_wait_s=1.0)
        assert policy.wait_s(1, _http_error(429, "7")) == 7.0
        assert policy.wait_s(1, _http_error(503, "7")) == 7.0
        # The floor never lowers the doubled wait, and a 500 ignores the header.
        assert policy.wait_s(4, _http_error(429, "3")) == 8.0
        assert policy.wait_s(1, _http_error(500, "7")) == 1.0

    def test_retry_after_is_capped(self) -> None:
        """A server that asks for an hour must not stall a backfill."""
        assert RetryPolicy(retries=1).wait_s(1, _http_error(503, "3600")) == 60.0

    def test_a_date_or_a_bad_retry_after_is_ignored(self) -> None:
        policy = RetryPolicy(retries=1, first_wait_s=1.0)
        for raw in ("Wed, 21 Oct 2026 07:28:00 GMT", "-5", "nan", ""):
            assert policy.wait_s(1, _http_error(429, raw)) == 1.0

    @pytest.mark.parametrize("code", [408, 425, 429, 500, 502, 503, 504, 599])
    def test_these_statuses_are_transient(self, code: int) -> None:
        assert client.is_transient(_http_error(code))

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 410])
    def test_any_other_4xx_is_final(self, code: int) -> None:
        assert not client.is_transient(_http_error(code))


class TestGet:
    def test_params_are_urlencoded_with_doseq(self) -> None:
        """``gameTypes`` is a list. Without doseq it reads ``['R', 'F']`` and the
        schedule answers with the wrong set of games."""
        seen = {}

        def fake(req, timeout=None):
            seen["url"] = req.full_url
            seen["ua"] = req.get_header("User-agent")
            seen["timeout"] = timeout
            return _ok()

        with patch("urllib.request.urlopen", side_effect=fake):
            client.get(
                client.SCHEDULE_URL,
                {"sportId": 1, "gameTypes": ["R", "F"]},
                headers={"User-Agent": "ua/1"},
                timeout_s=7,
            )
        assert seen["url"] == f"{client.SCHEDULE_URL}?sportId=1&gameTypes=R&gameTypes=F"
        assert seen["ua"] == "ua/1" and seen["timeout"] == 7

    def test_a_transient_failure_is_retried_then_the_body_returns(self) -> None:
        with (
            patch(
                "urllib.request.urlopen",
                side_effect=[ConnectionError("down"), _http_error(502), _ok(b'{"ok": 1}')],
            ) as urlopen,
            patch("ingest.mlb.client.time.sleep") as sleep,
        ):
            got = client.get_json("http://example.com", policy=RetryPolicy(2, 1.0))
        assert got == {"ok": 1}
        assert urlopen.call_count == 3
        assert [c.args[0] for c in sleep.call_args_list] == [1.0, 2.0]

    def test_a_malformed_reply_is_retried(self) -> None:
        """``BadStatusLine`` sits outside the ``OSError`` tree, so the retry set
        needs ``HTTPException`` too."""
        assert not issubclass(http.client.BadStatusLine, OSError)
        with (
            patch(
                "urllib.request.urlopen",
                side_effect=[http.client.BadStatusLine("garbage"), _ok(b'{"ok": 1}')],
            ),
            patch("ingest.mlb.client.time.sleep"),
        ):
            assert client.get_json("http://example.com", policy=RetryPolicy(1)) == {"ok": 1}

    def test_a_final_4xx_raises_at_once_unchanged(self) -> None:
        err = _http_error(404)
        with (
            patch("urllib.request.urlopen", side_effect=err) as urlopen,
            patch("ingest.mlb.client.time.sleep") as sleep,
            pytest.raises(urllib.error.HTTPError) as excinfo,
        ):
            client.get("http://example.com", policy=RetryPolicy(5))
        assert excinfo.value is err
        assert urlopen.call_count == 1 and not sleep.called

    def test_after_the_last_retry_the_original_error_raises(self) -> None:
        with (
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")),
            patch("ingest.mlb.client.time.sleep"),
            pytest.raises(urllib.error.URLError),
        ):
            client.get("http://example.com", policy=RetryPolicy(2))

    def test_the_default_policy_is_one_attempt(self) -> None:
        with (
            patch("urllib.request.urlopen", side_effect=TimeoutError("slow")) as urlopen,
            pytest.raises(TimeoutError),
        ):
            client.get("http://example.com")
        assert urlopen.call_count == 1

    def test_a_given_sleep_replaces_time_sleep(self) -> None:
        slept: list[float] = []
        with patch("urllib.request.urlopen", side_effect=[_http_error(503), _ok()]):
            client.get("http://example.com", policy=RetryPolicy(1, 3.0), sleep=slept.append)
        assert slept == [3.0]

    def test_a_body_that_is_not_json_raises_at_once(self) -> None:
        with (
            patch("urllib.request.urlopen", return_value=_ok(b"<html>")) as urlopen,
            pytest.raises(ValueError),
        ):
            client.get_json("http://example.com", policy=RetryPolicy(3))
        assert urlopen.call_count == 1

    def test_the_retry_log_line_names_the_label_the_attempt_and_the_path(self, caplog) -> None:
        with (
            patch("urllib.request.urlopen", side_effect=[_http_error(503), _ok()]),
            patch("ingest.mlb.client.time.sleep"),
            caplog.at_level(logging.INFO, logger="ingest.mlb.client"),
        ):
            client.get("https://statsapi.mlb.com/api/v1/x", {"k": "secret"}, policy=RetryPolicy(1))
        (record,) = [r for r in caplog.records if r.name == "ingest.mlb.client"]
        text = record.getMessage()
        assert text.startswith("MLB Stats API: statsapi.mlb.com/api/v1/x failed on attempt 1 of 2")
        assert "secret" not in text

    def test_the_body_is_decoded_with_the_declared_charset(self) -> None:
        body = "Muñoz".encode("latin-1")
        with patch(
            "urllib.request.urlopen",
            return_value=_ok(body, {"Content-Type": "text/html; charset=latin-1"}),
        ):
            assert client.get("http://example.com").text == "Muñoz"

    def test_an_unknown_charset_falls_back_to_utf8(self) -> None:
        with patch(
            "urllib.request.urlopen",
            return_value=_ok(b"ok", {"Content-Type": "text/html; charset=x-not-a-codec"}),
        ):
            assert client.get("http://example.com").text == "ok"

    def test_a_header_is_found_without_regard_to_case(self) -> None:
        resp = client.Response(200, {"retry-after": "9"}, "", "u")
        assert resp.header("Retry-After") == "9" and resp.header("X") is None


class TestNoCompression:
    def test_compression_is_not_requested_without_decompression_support(self) -> None:
        """Asking for compression and decompressing it must change together.

        ``http.client`` sends ``Accept-Encoding: identity``, which forbids the
        server to compress. That is the only reason ``_decode_body`` has no
        gunzip branch.
        """
        asks = "accept-encoding" in inspect.getsource(client.get).lower()
        can = any(
            token in inspect.getsource(client._decode_body).lower()
            for token in ("content-encoding", "gzip", "zlib", "decompress")
        )
        assert asks == can

    def test_urllib_sends_identity_encoding_over_a_real_socket(self) -> None:
        """Pin the claim above against a real server: if CPython stops sending
        ``identity``, the no-gunzip assumption stops being safe."""
        import http.server
        import threading

        seen: dict[str, str] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 — the stdlib's callback name
                seen.update({k.lower(): v for k, v in self.headers.items()})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.handle_request, daemon=True).start()
        try:
            got = client.get_json(
                f"http://127.0.0.1:{server.server_address[1]}/", headers={"User-Agent": "ua/1"}
            )
        finally:
            server.server_close()
        assert got == {"ok": True}
        assert seen.get("accept-encoding") == "identity"
        assert seen.get("user-agent") == "ua/1"


class TestCallersPassTheirPolicy:
    """Each caller keeps its own count, first wait, timeout and headers."""

    def test_the_game_loader(self) -> None:
        with patch("ingest.mlb.client.get") as get:
            load_games._http_get("http://example.com", {"a": 1})
        kw = get.call_args.kwargs
        assert kw["policy"] == RetryPolicy(retries=9, first_wait_s=2.0)
        assert kw["timeout_s"] == 30.0
        assert kw["headers"] == {"User-Agent": load_games._USER_AGENT}

    def test_the_box_score_load(self) -> None:
        from ingest.load_boxscores import BoxscoreIngest

        with patch("ingest.mlb.client.get_json", return_value={"teams": {}}) as get_json:
            BoxscoreIngest(max_attempts=4, backoff=1.5, timeout=9)._mlb_get("game/1/boxscore")
        assert get_json.call_args.args[0] == f"{client.API_V1}/game/1/boxscore"
        kw = get_json.call_args.kwargs
        assert kw["policy"] == RetryPolicy(retries=3, first_wait_s=1.5)
        assert kw["timeout_s"] == 9

    def test_the_bullpen_load(self) -> None:
        from ingest.load_bullpen_availability import BullpenAvailabilityIngest

        with patch("ingest.mlb.client.get_json", return_value={}) as get_json:
            BullpenAvailabilityIngest(timeout=4)._mlb_get("schedule", {"gamePk": 1})
        assert get_json.call_args.args[:2] == (f"{client.API_V1}/schedule", {"gamePk": 1})
        assert get_json.call_args.kwargs["policy"] == client.DEFAULT_POLICY


class TestLeagueFeed:
    @pytest.mark.asyncio
    async def test_the_schedule_and_the_feed_read_their_urls(self) -> None:
        feed = client.LeagueFeed(timeout_s=1)
        calls: list[tuple[str, object]] = []

        async def fake(url, params=None):
            calls.append((url, params))
            return {}

        feed.get_json = fake  # type: ignore[method-assign]
        from datetime import date

        await feed.game_feed_payload(745000)
        await feed.schedule_payload(date(2026, 10, 9), date(2026, 10, 9))
        assert calls[0] == (client.GAME_FEED_URL.format(game_pk=745000), {"hydrate": "alignment"})
        assert calls[1][0] == client.SCHEDULE_URL

    @pytest.mark.asyncio
    async def test_close_with_no_session_is_safe(self) -> None:
        await client.LeagueFeed(timeout_s=1).close()
