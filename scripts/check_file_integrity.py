#!/usr/bin/env python3
"""The file-integrity guard: it catches a ``.py`` file that a file bridge truncated
or filled with NUL bytes.

The Cowork/OneDrive file bridge can truncate a large source file mid-statement.
It can also inject NUL bytes when it flushes an edit through the mount (the
truncation finding, SIM-315). Either corruption breaks a ``.py`` file with no
warning. A truncated file can look plausible in a diff and still fail to
import. A NUL byte makes the file un-parseable. The guard catches both before
a commit.

For every candidate ``.py`` file the guard:

  1. reads the raw bytes and flags any file that holds a NUL byte (``\\x00``);
  2. runs ``ast.parse`` on the decoded source and flags any file that fails to
     parse (this catches a truncated, mid-statement file).

It prints one line per offending file. It exits 1 when it finds an offender,
else 0.

Usage::

    python scripts/check_file_integrity.py                 # scan whole repo
    python scripts/check_file_integrity.py path/to/file.py # scan given paths

With path arguments (pre-commit passes the list of changed files), the guard
checks only those paths and walks any directory among them. With no argument
it walks the whole repository tree, minus the VCS, cache, virtualenv and build
directories.

The guard uses only the standard library, so it runs as a fast pre-commit hook
and as a light CI job.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from collections.abc import Iterable, Iterator

# Directories that hold no hand-written source to guard. Every walk skips them:
# the full-tree walk and the walk of a directory argument.
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
        # A missing path or a file that is not .py is skipped with no message.
        # Pre-commit already filters by ``types: [python]``, so this is a guard only.


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
