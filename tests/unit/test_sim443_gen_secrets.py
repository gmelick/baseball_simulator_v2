"""SIM-443b — the secrets generator writes the eight secrets and never prints one."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gen_secrets.py"
_spec = importlib.util.spec_from_file_location("gen_secrets", _SCRIPT)
assert _spec is not None and _spec.loader is not None
gen = importlib.util.module_from_spec(_spec)
sys.modules["gen_secrets"] = gen
_spec.loader.exec_module(gen)

EXPECTED_KEYS = (
    "POSTGRES_PASSWORD",
    "BASEBALL_APP_DB_PASSWORD",
    "BASEBALL_RO_DB_PASSWORD",
    "REDIS_PASSWORD",
    "GRAFANA_ADMIN_PASSWORD",
    "SECRET_KEY",
    "AUTH_PASSWORD",
    "API_KEYS",
)
_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _values(text: str) -> dict[str, str]:
    return gen.read_env(text)


def test_the_eight_keys() -> None:
    assert gen.SECRET_KEYS == EXPECTED_KEYS


def test_stdout_mode_prints_eight_distinct_url_safe_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert gen.main([]) == 0
    out = capsys.readouterr().out
    values = _values(out)
    assert tuple(values) == EXPECTED_KEYS
    for value in values.values():
        assert _VALUE_RE.match(value), value
    assert len(set(values.values())) == len(EXPECTED_KEYS)


def test_append_creates_a_missing_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    assert gen.main(["--append", str(env)]) == 0
    values = _values(env.read_text(encoding="utf-8"))
    assert set(values) == set(EXPECTED_KEYS)
    assert all(_VALUE_RE.match(v) for v in values.values())


def test_append_keeps_an_existing_key_and_adds_only_the_missing(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("ENVIRONMENT=development\nREDIS_PASSWORD=mine\n", encoding="utf-8")
    assert gen.main(["--append", str(env)]) == 0
    text = env.read_text(encoding="utf-8")
    values = _values(text)
    assert values["REDIS_PASSWORD"] == "mine"
    assert values["ENVIRONMENT"] == "development"
    assert text.count("REDIS_PASSWORD=") == 1
    assert set(EXPECTED_KEYS) <= set(values)


def test_append_fills_a_blank_line_in_place(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# the db\nPOSTGRES_PASSWORD=\nOTHER=1\n", encoding="utf-8")
    gen.main(["--append", str(env)])
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# the db"
    assert lines[1].startswith("POSTGRES_PASSWORD=")
    assert _VALUE_RE.match(lines[1].split("=", 1)[1])
    assert lines[2] == "OTHER=1"
    assert sum(1 for line in lines if line.startswith("POSTGRES_PASSWORD=")) == 1


def test_a_second_append_is_a_no_op(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    gen.main(["--append", str(env)])
    first = env.read_bytes()
    gen.main(["--append", str(env)])
    assert env.read_bytes() == first


def test_append_keeps_crlf_line_endings(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_bytes(b"ENVIRONMENT=development\r\n")
    gen.main(["--append", str(env)])
    data = env.read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n")


def test_check_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = tmp_path / ".env"
    assert gen.main(["--check", str(env)]) == 1  # absent file
    env.write_text("POSTGRES_PASSWORD=x\nREDIS_PASSWORD=\n", encoding="utf-8")
    assert gen.main(["--check", str(env)]) == 1
    err = capsys.readouterr().err
    assert "REDIS_PASSWORD" in err and "API_KEYS" in err
    gen.main(["--append", str(env)])
    capsys.readouterr()
    assert gen.main(["--check", str(env)]) == 0


def test_append_and_check_print_key_names_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = tmp_path / ".env"
    env.write_text("SECRET_KEY=keepme_secret_value\n", encoding="utf-8")
    gen.main(["--append", str(env)])
    captured = capsys.readouterr()
    assert captured.out == ""
    for value in _values(env.read_text(encoding="utf-8")).values():
        assert value not in captured.err
    assert "keepme_secret_value" not in captured.err
    assert "SECRET_KEY" in captured.err
    gen.main(["--check", str(env)])
    captured = capsys.readouterr()
    assert captured.out == ""
    for value in _values(env.read_text(encoding="utf-8")).values():
        assert value not in captured.err
