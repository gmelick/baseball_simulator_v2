"""
tests/unit/test_nginx_resolve.py
=======================================
SIM-570: nginx re-resolves the app's name while it runs.

The reverse proxy (``deploy/nginx/nginx.conf``) proxies every API route to
the upstream group ``baseball_app``. Before SIM-570 nginx looked the name
``app`` up once, when nginx started, so a recreated app container (a new
address on the compose network) got 502 on every route until nginx
restarted. The fix is three directives in the upstream block, which
open-source nginx supports since 1.27.3: ``zone`` (the group in shared
memory), ``resolver`` (Docker's embedded DNS, with a short ``valid``) and
the ``resolve`` parameter on the server line.

These tests read the config as text. They guard the directives, the
proxy headers the rate limiter trusts (SIM-569) and the keep-alive the
upstream block gives every location. A container run needs the working
tree mounted (``deploy/`` is not bind-mounted).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_CONF = _REPO / "deploy" / "nginx" / "nginx.conf"


@pytest.fixture(scope="module")
def conf() -> str:
    if not _CONF.exists():
        pytest.skip("deploy/nginx/nginx.conf is not in this checkout (image-only run)")
    return _CONF.read_text(encoding="utf-8")


def _upstream_block(text: str) -> str:
    match = re.search(r"upstream\s+baseball_app\s*\{(.*?)\n\s*\}", text, re.DOTALL)
    assert match, "SIM-570: the upstream block baseball_app is missing"
    return match.group(1)


def test_the_server_line_re_resolves(conf):
    """SIM-570: the app's server line carries ``resolve``."""
    block = _upstream_block(conf)
    assert re.search(r"^\s*server\s+app:8000\s+resolve\s*;", block, re.MULTILINE), block


def test_the_group_sits_in_shared_memory(conf):
    """SIM-570: ``resolve`` needs the group in a ``zone``."""
    block = _upstream_block(conf)
    assert re.search(r"^\s*zone\s+baseball_app\s+\d+k\s*;", block, re.MULTILINE), block


def test_the_resolver_is_dockers_dns_with_a_short_ttl(conf):
    """SIM-570: the resolver is 127.0.0.11 with ``valid`` at most 30 s and IPv6 off."""
    block = _upstream_block(conf)
    match = re.search(r"^\s*resolver\s+127\.0\.0\.11\s+([^;]*);", block, re.MULTILINE)
    assert match, block
    options = match.group(1)
    valid = re.search(r"valid=(\d+)s", options)
    assert valid and int(valid.group(1)) <= 30, options
    assert "ipv6=off" in options, options


def test_every_proxied_location_still_uses_the_upstream_group(conf):
    """SIM-570: the locations keep ``proxy_pass http://baseball_app`` (the group's keep-alive)."""
    targets = re.findall(r"proxy_pass\s+(\S+)\s*;", conf)
    assert targets, "no proxy_pass in the config"
    assert set(targets) == {"http://baseball_app"}, targets
    assert re.search(r"^\s*keepalive\s+\d+\s*;", _upstream_block(conf), re.MULTILINE)


def test_the_real_ip_headers_stay(conf):
    """SIM-569: the rate limiter trusts X-Real-IP from nginx, so the header lines stay."""
    assert len(re.findall(r"proxy_set_header\s+X-Real-IP\s+\$remote_addr\s*;", conf)) >= 4
    assert "X-Forwarded-For   $proxy_add_x_forwarded_for" in conf
