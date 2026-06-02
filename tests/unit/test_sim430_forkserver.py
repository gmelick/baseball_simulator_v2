"""tests/unit/test_sim430_forkserver.py — SIM-430 worker start-method fix.

The ProcessPool workers used the platform-default **fork** start method, which
COW-forks each worker FROM the ~6 GB engine-loaded parent. CPython's refcounting +
cyclic GC write to every inherited object header, defeating copy-on-write, so each
worker's RSS ballooned toward the full ~6 GB it inherited but does NOT need (a
full-pool worker needs only the ~470 MB bundle). The pool now uses **forkserver**
(`mp_context`), so workers fork from a lean server process (~370 MB each, measured).

These tests pin the contract: the pool context defaults to forkserver, `_pool_kwargs`
wires it, it's overridable via `SIM_MP_START_METHOD`, and an unavailable method
falls back to the platform default rather than raising.
"""

from __future__ import annotations

import multiprocessing

import pytest

import simulation.batch_runner as br


def test_default_context_is_forkserver_when_available():
    if "forkserver" not in multiprocessing.get_all_start_methods():
        pytest.skip("forkserver unavailable on this platform")
    assert br._MP_START_METHOD == "forkserver"  # the env default
    assert br._pool_mp_context().get_start_method() == "forkserver"


def test_pool_kwargs_wires_mp_context_and_initializer():
    runner = br.BatchRunner(max_workers=1)
    kw = runner._pool_kwargs()
    assert kw["initializer"] is br._worker_init
    assert "mp_context" in kw
    # The context's start method is real + available on this platform.
    assert kw["mp_context"].get_start_method() in multiprocessing.get_all_start_methods()


def test_env_override_is_honored(monkeypatch):
    # _pool_mp_context reads the module-level _MP_START_METHOD (set from
    # SIM_MP_START_METHOD at import); an operator can pin a different method.
    monkeypatch.setattr(br, "_MP_START_METHOD", "spawn")
    assert br._pool_mp_context().get_start_method() == "spawn"


def test_unavailable_method_falls_back_without_raising(monkeypatch):
    monkeypatch.setattr(br, "_MP_START_METHOD", "not-a-real-method")
    ctx = br._pool_mp_context()  # must NOT raise
    assert ctx.get_start_method() in multiprocessing.get_all_start_methods()
