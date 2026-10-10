"""
pipeline/batch/bundle_versions.py — the engine-artifact bundle as published versions (SIM-563).

Why: the export used to rewrite ``/data/play_pool/engine_artifacts`` in place.
The running app's workers read that folder whenever one of them (re)starts, so
a worker that respawned during a rebuild could read a half-written file, or
pair the parent's old shared arrays with new files.

What this module does:

* ``engine_artifacts`` (the LINK) is a symbolic link to one folder under
  ``engine_artifacts.versions/<build id>/`` (a VERSION). Readers keep the old
  path; the link resolves it.
* A build STAGES a new version: a copy of the current one, so a partial build
  and the hand-written files (``park_geometry_overrides.json``) carry over.
* A finished build PUBLISHES the version: a temporary link moved onto the link
  with ``os.replace``, which is atomic. A reader sees the old version or the
  new one, never a mix.
* PRUNE keeps the newest versions and every version a running app marks as in
  use (``.in_use.<tag>``), so a pinned app never loses its files.

The first publish on a host converts a legacy real folder: it moves the folder
under ``engine_artifacts.versions/legacy-<ts>`` and links to it, so the old
path keeps resolving to the same files.

Owner: Data Engineer (SIM-563).
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import UTC, datetime

log = logging.getLogger("bundle_versions")

#: The suffix of the folder that holds the versions, next to the link.
VERSIONS_SUFFIX = ".versions"

#: The prefix of an in-use marker inside a version folder.
IN_USE_PREFIX = ".in_use."

#: Versions kept by :func:`prune_bundle_versions` (marked versions are kept too).
KEEP_VERSIONS = 3

#: The prefix of a converted legacy folder. It sorts as the oldest version.
LEGACY_PREFIX = "legacy-"

#: The file :func:`publish_bundle_version` writes into a version it publishes.
#: A folder without it is a build that never published: one in progress, or one
#: whose process died (a segfault skips the clean-up).
PUBLISHED_MARKER = ".published"


def versions_root(link: str) -> str:
    """The folder that holds the versions of ``link``."""
    return os.path.normpath(link) + VERSIONS_SUFFIX


def resolve_bundle_dir(link: str) -> str:
    """The real folder ``link`` points to (``link`` itself for a legacy folder)."""
    return os.path.realpath(link)


def _build_id(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


def _unique_dir(root: str, name: str) -> str:
    path = os.path.join(root, name)
    n = 1
    while os.path.lexists(path):
        n += 1
        path = os.path.join(root, f"{name}-{n}")
    return path


def _ignore_markers(_dir: str, names: list[str]) -> set[str]:
    return {
        n
        for n in names
        if n.startswith(IN_USE_PREFIX) or n.startswith("tmp_") or n == PUBLISHED_MARKER
    }


def stage_bundle_version(link: str, *, now: datetime | None = None) -> str:
    """Create a new version folder, seeded with a copy of the current version.

    Returns the new folder. Nothing a reader sees changes until
    :func:`publish_bundle_version`. With no current bundle, the folder starts empty.
    """
    root = versions_root(link)
    os.makedirs(root, exist_ok=True)
    dest = _unique_dir(root, _build_id(now))
    if os.path.isdir(link):
        shutil.copytree(resolve_bundle_dir(link), dest, ignore=_ignore_markers)
    else:
        os.makedirs(dest)
    log.info("SIM-563: staged bundle version %s", dest)
    return dest


def convert_legacy_bundle(link: str, *, now: datetime | None = None) -> None:
    """Move a legacy real folder under the versions folder and link to it.

    The app calls this at boot, before it pins a version, so the pin always names
    a version folder; :func:`publish_bundle_version` calls it too.
    """
    if os.path.islink(link) or not os.path.isdir(link):
        return
    root = versions_root(link)
    os.makedirs(root, exist_ok=True)
    dest = _unique_dir(root, LEGACY_PREFIX + _build_id(now))
    os.rename(link, dest)
    os.symlink(os.path.relpath(dest, os.path.dirname(os.path.abspath(link))), link)
    log.info("SIM-563: converted the legacy bundle folder to version %s", dest)


def publish_bundle_version(link: str, version_dir: str) -> None:
    """Point ``link`` at ``version_dir`` in one atomic step."""
    convert_legacy_bundle(link)
    parent = os.path.dirname(os.path.abspath(link))
    tmp = os.path.abspath(link) + ".tmp-link"
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(os.path.relpath(os.path.abspath(version_dir), parent), tmp)
    with open(os.path.join(version_dir, PUBLISHED_MARKER), "w", encoding="utf-8") as fh:
        fh.write(datetime.now(UTC).isoformat())
    os.replace(tmp, os.path.abspath(link))
    log.info("SIM-563: published bundle version %s", version_dir)


def discard_bundle_version(version_dir: str) -> None:
    """Delete a staged version that failed to build. The link never pointed at it."""
    shutil.rmtree(version_dir, ignore_errors=True)
    log.warning("SIM-563: discarded the failed bundle version %s", version_dir)


def _version_order(name: str) -> tuple[int, str]:
    return (0 if name.startswith(LEGACY_PREFIX) else 1, name)


def _in_use(version_dir: str) -> bool:
    try:
        return any(n.startswith(IN_USE_PREFIX) for n in os.listdir(version_dir))
    except OSError:
        return False


def _published(name: str, path: str) -> bool:
    return name.startswith(LEGACY_PREFIX) or os.path.exists(os.path.join(path, PUBLISHED_MARKER))


def prune_bundle_versions(link: str, keep: int = KEEP_VERSIONS) -> list[str]:
    """Delete old versions and dead builds. Returns the deleted folders.

    * A PUBLISHED version is kept when it is among the newest ``keep``, is the
      current one, or carries an in-use marker.
    * An UNPUBLISHED folder older than the current version is a build whose
      process died before it published (a segfault skips the clean-up): it is
      deleted. One newer than the current version may be a build in progress, so
      it stays.
    """
    root = versions_root(link)
    if not os.path.isdir(root):
        return []
    current = resolve_bundle_dir(link)
    current_key = _version_order(os.path.basename(current))
    names = sorted(
        (n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n))),
        key=_version_order,
    )
    published = [n for n in names if _published(n, os.path.join(root, n))]
    keep_names = set(published[-keep:]) if keep > 0 else set()
    removed: list[str] = []
    for name in names:
        path = os.path.join(root, name)
        if os.path.realpath(path) == current or _in_use(path) or name in keep_names:
            continue
        if name not in published and _version_order(name) > current_key:
            continue  # maybe a build in progress
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path)
        log.info("SIM-563: pruned bundle version %s", path)
    return removed


def mark_in_use(version_dir: str, tag: str) -> str | None:
    """Write the in-use marker ``tag`` into a version. Returns its path, or None."""
    path = os.path.join(version_dir, IN_USE_PREFIX + tag)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(datetime.now(UTC).isoformat())
    except OSError as exc:
        log.warning("SIM-563: cannot mark %s in use (%s)", version_dir, exc)
        return None
    return path


def unmark_in_use(marker: str | None) -> None:
    """Remove an in-use marker :func:`mark_in_use` wrote."""
    if marker:
        try:
            os.remove(marker)
        except OSError:
            pass
