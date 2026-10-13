#!/usr/bin/env python3
"""
scripts/gen_secrets.py
======================
SIM-443b — generate the stack's secrets. The code and the compose file carry no
default password, so every secret must sit in ``.env``.

The script writes eight secrets: the three Postgres role passwords, the Redis
password, the Grafana admin password, the session-signing key, the login
password and one API key. Each value is ``secrets.token_urlsafe(32)``: 43
characters of ``A-Z a-z 0-9 - _``, so a value needs no escaping inside a DSN.

Usage (stdlib only; runs on the host with any Python 3.11+):

    python scripts/gen_secrets.py                  # print KEY=value lines to stdout
    python scripts/gen_secrets.py --append .env    # add the missing secrets to .env
    python scripts/gen_secrets.py --check .env     # exit 1 when a secret is missing

``--append`` never changes a secret that has a value. A key that is missing gets
a new line at the end of the file. A key that is present but blank (the shape
``.env.example`` ships) gets its value filled in on its own line. The file is
created when it does not exist. ``--append`` and ``--check`` print only key
names, on stderr, never a value.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

#: The secrets the stack needs, in the order they are written.
SECRET_KEYS: tuple[str, ...] = (
    "POSTGRES_PASSWORD",
    "BASEBALL_APP_DB_PASSWORD",
    "BASEBALL_RO_DB_PASSWORD",
    "REDIS_PASSWORD",
    "GRAFANA_ADMIN_PASSWORD",
    "SECRET_KEY",
    "AUTH_PASSWORD",
    "API_KEYS",
)

#: The number of random bytes behind each value (43 URL-safe characters).
TOKEN_BYTES = 32

#: One ``KEY=value`` line of a dotenv file, with an optional ``export`` prefix.
_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")

#: A quoted value, with an optional trailing comment after the closing quote.
_QUOTED_RE = re.compile(r"""^(["'])(.*?)\1\s*(?:#.*)?$""")

#: The UTF-8 byte-order mark. PowerShell 5.1 writes it; compose strips it.
_BOM = "\ufeff"


def new_secret() -> str:
    """Return one new secret value."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def _clean_value(raw: str) -> str:
    """Return a dotenv value without its quotes and its trailing comment."""
    value = raw.strip()
    quoted = _QUOTED_RE.match(value)
    if quoted:
        return quoted.group(2).strip()
    if " #" in value:
        value = value.split(" #", 1)[0]
    return value.strip()


def read_env(text: str) -> dict[str, str]:
    """Return ``{key: value}`` for every assignment line of a dotenv text.

    A comment line is skipped. When a key appears twice, the last line wins,
    the way docker compose reads the file.
    """
    found: dict[str, str] = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if match:
            found[match.group(1)] = _clean_value(match.group(2))
    return found


def missing_keys(text: str) -> list[str]:
    """Return the secret keys that are absent or blank in a dotenv text."""
    present = read_env(text)
    return [key for key in SECRET_KEYS if not present.get(key)]


def append_secrets(path: Path) -> tuple[list[str], list[str], list[str]]:
    """Add the missing secrets to the dotenv file at ``path``.

    Returns ``(added, filled, kept)``: the keys appended at the end, the blank
    keys filled in place, and the keys left alone because they hold a value.
    The file keeps its line endings. A new file is created with mode 0600 on a
    POSIX host.
    """
    existed = path.exists()
    # Raw bytes keep a CRLF file's line endings; read_text would translate them.
    # utf-8-sig drops a byte-order mark, which would hide the first line's key.
    raw = path.read_bytes() if existed else b""
    has_bom = raw.startswith(_BOM.encode("utf-8"))
    text = raw.decode("utf-8-sig")
    eol = "\r\n" if "\r\n" in text else "\n"
    present = read_env(text)

    kept = [key for key in SECRET_KEYS if present.get(key)]
    blank = {key for key in SECRET_KEYS if key in present and not present[key]}
    added = [key for key in SECRET_KEYS if key not in present]

    filled: list[str] = []
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        match = _LINE_RE.match(line.rstrip("\r\n"))
        if not match or match.group(1) not in blank or _clean_value(match.group(2)):
            continue
        # Every blank copy of a key is filled: compose reads the last line.
        # A copy that holds a value is never changed.
        key = match.group(1)
        ending = line[len(line.rstrip("\r\n")) :]
        lines[index] = f"{key}={new_secret()}{ending}"
        if key not in filled:
            filled.append(key)

    out = "".join(lines)
    if added:
        if out and not out.endswith(("\n", "\r")):
            out += eol
        stamp = datetime.now(UTC).strftime("%Y-%m-%d")
        out += f"# Secrets generated by scripts/gen_secrets.py on {stamp} (SIM-443b).{eol}"
        for key in added:
            out += f"{key}={new_secret()}{eol}"

    if added or filled:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write((_BOM if has_bom else "") + out)
        if not existed and os.name == "posix":
            os.chmod(path, 0o600)
    return added, filled, kept


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Generate the stack's secrets (SIM-443b). With no option, print "
            "KEY=value lines to stdout."
        )
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--append",
        metavar="PATH",
        type=Path,
        help="add the missing or blank secrets to this dotenv file (created when absent)",
    )
    mode.add_argument(
        "--check",
        metavar="PATH",
        type=Path,
        help="exit 1 when a secret is missing or blank in this dotenv file",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.check is not None:
        text = args.check.read_text(encoding="utf-8-sig") if args.check.exists() else ""
        missing = missing_keys(text)
        if missing:
            sys.stderr.write(
                f"{args.check}: {len(missing)} secret(s) missing or blank: "
                + ", ".join(missing)
                + f"\nRun: python scripts/gen_secrets.py --append {args.check}\n"
            )
            return 1
        sys.stderr.write(f"{args.check}: all {len(SECRET_KEYS)} secrets are set.\n")
        return 0

    if args.append is not None:
        added, filled, kept = append_secrets(args.append)
        for key in added:
            sys.stderr.write(f"added   {key}\n")
        for key in filled:
            sys.stderr.write(f"filled  {key} (the line was blank)\n")
        for key in kept:
            sys.stderr.write(f"kept    {key} (already set; not changed)\n")
        return 0

    for key in SECRET_KEYS:
        sys.stdout.write(f"{key}={new_secret()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
