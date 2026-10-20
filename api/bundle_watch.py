"""
api/bundle_watch.py
===================
The app picks up a nightly rebuild by restarting itself.

The app loads the player profiles (the similarity engines, from DuckDB) and the
engine-artifact bundle once, at boot. The nightly rebuild ends by PUBLISHING a
new bundle version: the link ``engine_artifacts`` moves to a new folder
(``pipeline/batch/bundle_versions.py``). This module:

* PINS the version the app loaded. :func:`pin_bundle_version` resolves the link
  and sets ``BASEBALL_ENGINE_ARTIFACT_DIR`` before the worker pool starts, so a
  worker that respawns later reads the same version as the parent's shared
  arrays. It also marks that version in use, so pruning keeps it.
* WATCHES the link (:func:`watch_for_new_bundle`). When the link points to a
  new version, the watch waits until no run job is running (at most
  ``drain_cap`` seconds), then until no DuckDB writer holds the file, and then
  restarts the app: SIGTERM to PID 1. In the container, PID 1 is uvicorn; it
  shuts down cleanly and Docker's ``restart: unless-stopped`` starts it again,
  and the boot loads the new profiles and the new bundle.

The restart is OFF unless ``SIM_RESTART_ON_NEW_BUNDLE`` is set (the compose app
service sets it), so a test or a host-side run never signals PID 1.

A restart, not an in-place swap, is an owner ruling (the nightly rebuild that
reaches users, SIM-563): about two to three minutes of downtime near 5 AM
Eastern, against twice the memory and much harder code for a swap.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import time
from collections.abc import Awaitable, Callable

from pipeline.batch.bundle_versions import (
    convert_legacy_bundle,
    mark_in_use,
    resolve_bundle_dir,
)
from simulation.flags import read_bool

log = logging.getLogger(__name__)

#: The variable that turns the self-restart on.
RESTART_ENV = "SIM_RESTART_ON_NEW_BUNDLE"

#: The variable the workers read the pinned version from
#: (``simulation.production_factory.PINNED_ARTIFACT_DIR_ENV``).
PINNED_ENV = "BASEBALL_ENGINE_ARTIFACT_DIR"

#: Seconds between two looks at the link.
CHECK_SECONDS = 60.0

#: The longest the restart waits for the run jobs to finish.
DRAIN_CAP_SECONDS = 600.0


def bundle_link() -> str:
    """The published bundle path, ``$BASEBALL_PLAY_POOL_DIR/engine_artifacts``."""
    return os.path.join(
        os.environ.get("BASEBALL_PLAY_POOL_DIR", "/data/play_pool"), "engine_artifacts"
    )


def restart_enabled() -> bool:
    return read_bool(RESTART_ENV)


def pin_bundle_version(link: str | None = None) -> tuple[str, str | None]:
    """Resolve the link, pin it for the workers and mark it in use.

    Returns ``(version_dir, marker)``. Call it before the worker pool starts:
    the forkserver's workers inherit the environment it sets.
    """
    link = link or bundle_link()
    try:
        convert_legacy_bundle(link)
    except OSError as exc:  # the pin still works on the real folder
        log.warning("SIM-563: cannot convert the legacy bundle folder %s (%s)", link, exc)
    version = resolve_bundle_dir(link)
    os.environ[PINNED_ENV] = version
    marker = mark_in_use(version, socket.gethostname()) if os.path.isdir(version) else None
    log.info("SIM-563: bundle version pinned for this boot: %s", version)
    return version, marker


def duckdb_writer_free(path: str | None = None) -> bool:
    """True when a read-only open of the statistics DuckDB succeeds (or it is absent)."""
    from simulation.sim_kwargs import DEFAULT_DUCKDB_PATH, open_sim_duckdb

    path = path or os.environ.get("BASEBALL_DUCKDB_PATH") or DEFAULT_DUCKDB_PATH
    if not os.path.exists(path):
        return True
    con = open_sim_duckdb(path)
    if con is None:
        return False
    try:
        con.close()
    except Exception:  # noqa: BLE001
        pass
    return True


def restart_container() -> None:
    """Ask PID 1 to stop. Docker's restart policy starts the container again."""
    os.kill(1, signal.SIGTERM)


async def watch_for_new_bundle(
    *,
    link: str,
    loaded: str,
    active_jobs: Callable[[], int],
    writer_free: Callable[[], bool] = duckdb_writer_free,
    restart: Callable[[], None] = restart_container,
    interval: float = CHECK_SECONDS,
    drain_cap: float = DRAIN_CAP_SECONDS,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Wait for a new published version, then restart the app. Returns that version."""
    while True:
        await sleep(interval)
        current = resolve_bundle_dir(link)
        if current == loaded or not os.path.isdir(current):
            continue
        log.warning(
            "SIM-563: a new bundle version is published (%s; this app loaded %s). "
            "The app restarts once its run jobs finish.",
            current,
            loaded,
        )
        started = clock()
        while active_jobs() > 0 and clock() - started < drain_cap:
            await sleep(5.0)
        if active_jobs() > 0:
            log.warning(
                "SIM-563: %d run job(s) still running after %.0f s; restarting anyway "
                "(the next boot marks them failed('restart')).",
                active_jobs(),
                drain_cap,
            )
        while not await asyncio.to_thread(writer_free):
            log.info("SIM-563: a DuckDB writer still holds the file; the restart waits.")
            await sleep(interval)
        log.warning("SIM-563: restarting the app to load bundle version %s.", current)
        restart()
        return current
