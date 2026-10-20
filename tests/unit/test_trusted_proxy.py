"""
tests/unit/test_trusted_proxy.py
=======================================
SIM-569: behind nginx, the rate limiter counts a request against the real
client's address, not the proxy's.

nginx sets ``X-Real-IP`` to the connection's own address on every request,
so a browser cannot forge it. The app believes that header only when the
connection comes from a proxy named in ``TRUSTED_PROXY_HOSTS``; the names
resolve to addresses at most once a minute. With the variable unset, the
app reads the connection's address and nothing else (the behaviour before
SIM-569).

The tests patch the resolver, so no test touches DNS except the one that
resolves ``localhost``. Starlette's TestClient connects from the host
``testclient``, which the tests name as the trusted proxy.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import auth
from api.auth import RateLimitMiddleware

_PROXY = "testclient"  # the host Starlette's TestClient connects from
_CLIENT_A = "192.168.1.20"
_CLIENT_B = "192.168.1.21"


def _app(limit: int = 2) -> FastAPI:
    app = FastAPI()

    @app.get("/data")
    def data():
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware, limit=limit, window_s=60, enabled=True)
    return app


@pytest.fixture(autouse=True)
def _no_trusted_proxy(monkeypatch):
    """Every test starts with no trusted proxy and an empty resolver cache."""
    monkeypatch.delenv("TRUSTED_PROXY_HOSTS", raising=False)
    auth._reset_trusted_proxy_cache()
    yield
    auth._reset_trusted_proxy_cache()


def _trust(monkeypatch, addresses_by_name: dict[str, set[str]], hosts: str = "nginx") -> None:
    """Name the trusted proxies and fix what each name resolves to."""
    monkeypatch.setenv("TRUSTED_PROXY_HOSTS", hosts)
    monkeypatch.setattr(
        auth, "_resolve_proxy_host", lambda name: set(addresses_by_name.get(name, ()))
    )


def _get(client: TestClient, real_ip: str | None):
    headers = {"X-Real-IP": real_ip} if real_ip is not None else {}
    return client.get("/data", headers=headers)


def test_no_trusted_proxy_ignores_the_real_ip_header():
    """SIM-569: with TRUSTED_PROXY_HOSTS unset, every request counts against the connection."""
    client = TestClient(_app(limit=2))
    assert _get(client, _CLIENT_A).status_code == 200
    assert _get(client, _CLIENT_B).status_code == 200
    assert _get(client, "10.0.0.3").status_code == 429


def test_trusted_proxy_keeps_a_bucket_per_real_client(monkeypatch):
    """SIM-569: behind a trusted proxy, two real clients get separate buckets."""
    _trust(monkeypatch, {"nginx": {_PROXY}})
    client = TestClient(_app(limit=2))
    assert _get(client, _CLIENT_A).status_code == 200
    assert _get(client, _CLIENT_A).status_code == 200
    assert _get(client, _CLIENT_A).status_code == 429
    assert _get(client, _CLIENT_B).status_code == 200
    assert _get(client, _CLIENT_B).status_code == 200
    assert _get(client, _CLIENT_B).status_code == 429


def test_untrusted_connection_cannot_pick_its_address(monkeypatch):
    """SIM-569: a connection that is not the proxy keeps its own address.

    The proxy name resolves to another address, so the TestClient's
    connection is untrusted and its X-Real-IP header changes nothing.
    """
    _trust(monkeypatch, {"nginx": {"172.30.0.9"}})
    client = TestClient(_app(limit=2))
    assert _get(client, _CLIENT_A).status_code == 200
    assert _get(client, _CLIENT_B).status_code == 200
    assert _get(client, "10.0.0.3").status_code == 429


def test_trusted_proxy_without_the_header_counts_the_connection(monkeypatch):
    """SIM-569: a trusted proxy that sends no X-Real-IP counts against its own address."""
    _trust(monkeypatch, {"nginx": {_PROXY}})
    client = TestClient(_app(limit=2))
    assert _get(client, None).status_code == 200
    assert _get(client, "").status_code == 200
    assert _get(client, None).status_code == 429


def test_client_address_direct_cases(monkeypatch):
    """SIM-569: the address helper on its own: no client, a trusted proxy, an untrusted one."""
    _trust(monkeypatch, {"nginx": {"172.30.0.5"}})
    no_client = SimpleNamespace(client=None, headers={"X-Real-IP": _CLIENT_A})
    assert auth._client_address(no_client) == "unknown"
    via_proxy = SimpleNamespace(
        client=SimpleNamespace(host="172.30.0.5"), headers={"X-Real-IP": f" {_CLIENT_A} "}
    )
    assert auth._client_address(via_proxy) == _CLIENT_A
    direct = SimpleNamespace(
        client=SimpleNamespace(host="172.30.0.1"), headers={"X-Real-IP": _CLIENT_A}
    )
    assert auth._client_address(direct) == "172.30.0.1"


def test_several_names_and_a_literal_address(monkeypatch):
    """SIM-569: the variable takes a comma-separated list; every entry resolves."""
    _trust(
        monkeypatch, {"nginx": {"172.30.0.5"}, "10.1.1.1": {"10.1.1.1"}}, hosts="nginx, 10.1.1.1 ,"
    )
    assert auth._trusted_proxy_addresses() == frozenset({"172.30.0.5", "10.1.1.1"})


def test_resolution_is_cached_for_the_ttl(monkeypatch):
    """SIM-569: the names resolve once per TTL, not once per request."""
    calls: list[str] = []

    def resolver(name: str) -> set[str]:
        calls.append(name)
        return {_PROXY}

    monkeypatch.setenv("TRUSTED_PROXY_HOSTS", "nginx")
    monkeypatch.setattr(auth, "_resolve_proxy_host", resolver)
    client = TestClient(_app(limit=50))
    for _ in range(5):
        assert _get(client, _CLIENT_A).status_code == 200
    assert calls == ["nginx"]

    # Past the TTL the names resolve again.
    clock = {"now": 10_000.0}
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock["now"])
    auth._reset_trusted_proxy_cache()
    assert _get(client, _CLIENT_A).status_code == 200
    clock["now"] += auth._PROXY_RESOLVE_TTL_S + 1
    assert _get(client, _CLIENT_A).status_code == 200
    assert calls == ["nginx", "nginx", "nginx"]


def test_a_name_that_does_not_resolve_trusts_nothing(monkeypatch):
    """SIM-569: an unresolved name adds no address and raises nothing; the request still runs."""
    monkeypatch.setenv("TRUSTED_PROXY_HOSTS", "no-such-proxy")
    monkeypatch.setattr(auth, "_resolve_proxy_host", lambda name: set())
    assert auth._trusted_proxy_addresses() == frozenset()
    client = TestClient(_app(limit=2))
    assert _get(client, _CLIENT_A).status_code == 200
    assert _get(client, _CLIENT_B).status_code == 200
    assert _get(client, _CLIENT_A).status_code == 429


def test_the_real_resolver_resolves_localhost_and_swallows_a_bad_name():
    """SIM-569: the resolver returns the addresses of a name and an empty set for a bad one."""
    assert "127.0.0.1" in auth._resolve_proxy_host("localhost")
    assert auth._resolve_proxy_host("no-such-host.invalid") == set()
