#!/usr/bin/env python3
"""SIM-315 — file-integrity guard (OneDrive / Cowork truncation remediation).

The Cowork/OneDrive file bridge has been observed to truncate large source
files mid-statement and to inject NUL bytes when an edit is flushed through the
mount. Either corruption silently breaks a ``.py`` file: a truncated file may
still *look* plausible in a diff yet fail to import, and an embedded NUL byte
makes the file un-parseable. This guard catches both classes of corruption
*before* they are committed.

For every candidate ``.py`` file it:

  1. reads the raw bytes and flags any file containing a NUL byte (``\\x00``);
  2. ``ast.parse``-s the decoded source and flags any file that fails to parse
     (this is what catches truncated / mid-statement files).

It prints a clear per-file report and exits non-zero if *any* offender is
found, zero otherwise.

Usage::

    python scripts/check_file_integrity.py                 # scan whole repo
    python scripts/check_file_integrity.py path/to/file.py # scan given paths

When invoked with explicit path arguments (as pre-commit does with the list of
changed files), only those paths are checked; directories among the arguments
are walked. With no arguments the entire repository tree is walked, excluding
VCS / cache / virtualenv / build directories.

Pure standard library — no third-party dependencies — so it is safe to run as a
fast pre-commit hook and as a lightweight CI job.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from collections.abc import Iterable, Iterator

# Directories that never contain hand-authored source we want to guard.
# Skipped during a full-tree walk (and when an explicit directory arg is walked).
EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        "htmlcov",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".benchmarks",
        "node_modules",
        "build",
        "dist",
        ".eggs",
        ".tox",
    }
)

NULL_BYTE = b"\x00"


class Offender:
    """A single integrity violation for one file."""

    __slots__ = ("path", "reason")

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"Offender(path={self.path!r}, reason={self.reason!r})"


def _is_excluded_dir(name: str) -> bool:
    return name in EXCLUDED_DIRS


def iter_python_files(paths: Iterable[str]) -> Iterator[str]:
    """Yield ``.py`` files for the given paths.

    A path that is a file is yielded as-is if it ends with ``.py``. A path that
    is a directory is walked recursively, pruning :data:`EXCLUDED_DIRS` and
    yielding every ``.py`` file beneath it.
    """
    for path in paths:
        if os.path.isfile(path):
            if path.endswith(".py"):
                yield path
            continue
        if os.path.isdir(path):
            for root, dirnames, filenames in os.walk(path):
                # Prune excluded directories in place so os.walk skips them.
                dirnames[:] = [d for d in dirnames if not _is_excluded_dir(d)]
                for filename in filenames:
                    if filename.endswith(".py"):
                        yield os.path.join(root, filename)
        # Silently ignore non-existent paths / non-.py file args — pre-commit
        # filters by ``types: [python]`` so this is just defensive.


def check_file(path: str) -> Offender | None:
    """Return an :class:`Offender` describing the first problem found, else None."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:  # pragma: no cover - unreadable file is itself a problem
        return Offender(path, f"could not read file: {exc}")

    # (a) NUL byte → corruption injected by the file bridge.
    if NULL_BYTE in raw:
        index = raw.index(NULL_BYTE)
        return Offender(path, f"contains a NUL byte (\\x00) at offset {index}")

    # (b) Decode + parse. A truncated / mid-statement file fails here.
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return Offender(path, f"is not valid UTF-8: {exc}")

    try:
        ast.parse(source, filename=path)
    except SyntaxError as exc:
        lineno = exc.lineno if exc.lineno is not None else "?"
        return Offender(path, f"failed to parse (truncated/corrupt?): line {lineno}: {exc.msg}")

    return None


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 if clean, 1 if any offender was found."""
    parser = argparse.ArgumentParser(
        prog="check_file_integrity.py",
        description=(
            "Flag .py files corrupted by the Cowork/OneDrive file bridge: "
            "NUL bytes or truncated/un-parseable source (SIM-315)."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Files or directories to check (default: the whole repository tree).",
    )
    args = parser.parse_args(argv)

    paths = args.paths if args.paths else ["."]

    files = sorted(set(iter_python_files(paths)))
    offenders: list[Offender] = []
    for path in files:
        offender = check_file(path)
        if offender is not None:
            offenders.append(offender)

    if offenders:
        print(f"FAIL: {len(offenders)} file(s) failed the integrity check (SIM-315):")
        for off in offenders:
            print(f"  - {off.path}: {off.reason}")
        print(
            "\nThese files are likely truncated or NUL-corrupted by the file bridge. "
            "Restore them from a clean copy / re-edit via a native write before committing."
        )
        return 1

    print(f"OK: {len(files)} Python file(s) checked, no integrity issues found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
