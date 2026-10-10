"""SIM-563 — the engine-artifact bundle is published as a version behind a link.

A worker that respawns during a rebuild used to read a half-written bundle. The
build now writes a new version folder and moves the link onto it in one step.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import pytest

from pipeline.batch import bundle_versions as bv


@pytest.fixture
def link(tmp_path):
    if os.name == "nt":
        # Windows cannot rename a link onto a directory link in one step. The
        # bundle lives on the Linux /data volume; CI and the container run these.
        pytest.skip("the atomic link swap is POSIX-only")
    probe = tmp_path / "probe"
    try:
        os.symlink(tmp_path, probe)
    except (OSError, NotImplementedError):
        pytest.skip("this OS refuses symbolic links (CI on Linux runs these tests)")
    probe.unlink()
    path = tmp_path / "engine_artifacts"
    path.mkdir()
    (path / "pitch_pool").mkdir()
    (path / "pitch_pool" / "manifest.json").write_text(json.dumps({"v": "legacy"}))
    (path / "park_geometry_overrides.json").write_text("{}")
    return str(path)


def _read(link: str) -> str:
    with open(os.path.join(link, "pitch_pool", "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)["v"]


def _write(version_dir: str, v: str) -> None:
    with open(
        os.path.join(version_dir, "pitch_pool", "manifest.json"), "w", encoding="utf-8"
    ) as fh:
        json.dump({"v": v}, fh)


def _at(minute: int) -> datetime:
    return datetime(2026, 10, 10, 8, minute, tzinfo=UTC)


def test_a_staged_build_changes_nothing_until_it_publishes(link):
    staged = bv.stage_bundle_version(link, now=_at(0))
    _write(staged, "new")

    assert _read(link) == "legacy"  # the readers still see the old bundle
    # The copy carried the hand-written file.
    assert os.path.exists(os.path.join(staged, "park_geometry_overrides.json"))

    bv.publish_bundle_version(link, staged)
    assert os.path.islink(link)
    assert _read(link) == "new"
    assert bv.resolve_bundle_dir(link) == os.path.realpath(staged)


def test_the_legacy_folder_becomes_a_version_with_the_same_files(link):
    staged = bv.stage_bundle_version(link, now=_at(0))
    bv.publish_bundle_version(link, staged)

    root = bv.versions_root(link)
    legacy = [n for n in os.listdir(root) if n.startswith(bv.LEGACY_PREFIX)]
    assert len(legacy) == 1
    with open(os.path.join(root, legacy[0], "pitch_pool", "manifest.json"), encoding="utf-8") as fh:
        assert json.load(fh)["v"] == "legacy"


def test_the_link_is_relative_so_it_survives_another_mount_point(link):
    staged = bv.stage_bundle_version(link, now=_at(0))
    bv.publish_bundle_version(link, staged)
    assert not os.path.isabs(os.readlink(link))


def test_a_failed_build_leaves_the_published_bundle_alone(link):
    first = bv.stage_bundle_version(link, now=_at(0))
    _write(first, "v1")
    bv.publish_bundle_version(link, first)

    second = bv.stage_bundle_version(link, now=_at(1))
    _write(second, "half")
    bv.discard_bundle_version(second)

    assert not os.path.exists(second)
    assert _read(link) == "v1"


def test_the_new_version_is_seeded_from_the_CURRENT_one(link):
    first = bv.stage_bundle_version(link, now=_at(0))
    _write(first, "v1")
    bv.publish_bundle_version(link, first)
    second = bv.stage_bundle_version(link, now=_at(1))
    with open(os.path.join(second, "pitch_pool", "manifest.json"), encoding="utf-8") as fh:
        assert json.load(fh)["v"] == "v1"


def test_a_marker_is_not_copied_into_the_next_version(link):
    first = bv.stage_bundle_version(link, now=_at(0))
    bv.publish_bundle_version(link, first)
    bv.mark_in_use(first, "app-1")
    second = bv.stage_bundle_version(link, now=_at(1))
    assert not any(n.startswith(bv.IN_USE_PREFIX) for n in os.listdir(second))


def test_two_builds_in_one_second_get_two_folders(link):
    a = bv.stage_bundle_version(link, now=_at(0))
    b = bv.stage_bundle_version(link, now=_at(0))
    assert a != b


def test_prune_keeps_the_newest_the_current_and_the_marked(link):
    versions = []
    for m in range(6):
        d = bv.stage_bundle_version(link, now=_at(m))
        bv.publish_bundle_version(link, d)
        versions.append(d)
    marked = versions[1]
    bv.mark_in_use(marked, "app-1")
    rollback = os.path.join(os.path.dirname(link), "engine_artifacts.pre_sim553")
    os.makedirs(rollback)

    removed = bv.prune_bundle_versions(link, keep=3)

    kept = set(os.listdir(bv.versions_root(link)))
    assert {os.path.basename(v) for v in versions[-3:]} <= kept
    assert os.path.basename(marked) in kept
    assert os.path.exists(rollback)  # never touched
    assert os.path.realpath(versions[0]) not in {os.path.realpath(r) for r in kept}
    assert len(removed) == 3  # the legacy folder, versions[0] and versions[2]
    assert _read(link) == "legacy"  # the bundle still reads


def test_unmark_removes_the_marker(link):
    d = bv.stage_bundle_version(link, now=_at(0))
    marker = bv.mark_in_use(d, "app-1")
    assert marker and os.path.exists(marker)
    bv.unmark_in_use(marker)
    assert not os.path.exists(marker)
    bv.unmark_in_use(marker)  # a second call is harmless


def test_the_app_pin_converts_the_legacy_folder_first(link, monkeypatch):
    """The pin must name a version folder, never the link itself."""
    from api import bundle_watch

    monkeypatch.delenv(bundle_watch.PINNED_ENV, raising=False)
    version, marker = bundle_watch.pin_bundle_version(link)
    try:
        assert os.path.islink(link)
        assert version != os.path.abspath(link)
        assert version.startswith(bv.versions_root(link))
        assert marker and os.path.dirname(marker) == version
        assert _read(link) == "legacy"
    finally:
        os.environ.pop(bundle_watch.PINNED_ENV, None)
