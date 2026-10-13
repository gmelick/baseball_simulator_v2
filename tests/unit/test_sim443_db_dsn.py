"""SIM-443b — the connection-string reader has no default password."""

from __future__ import annotations

import pytest

from pipeline.db_dsn import dsn_label, require_dsn


def test_returns_the_stripped_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASEBALL_DB_DSN", "  postgresql://u:p@h:5432/d \n")
    assert require_dsn() == "postgresql://u:p@h:5432/d"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_or_blank_raises_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("BASEBALL_DB_DSN", raising=False)
    else:
        monkeypatch.setenv("BASEBALL_DB_DSN", value)
    with pytest.raises(RuntimeError) as info:
        require_dsn()
    message = str(info.value)
    assert "BASEBALL_DB_DSN" in message
    assert ".env.example" in message


def test_custom_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTHER_DSN", "postgresql://x:y@z/w")
    monkeypatch.delenv("BASEBALL_DB_DSN", raising=False)
    assert require_dsn("OTHER_DSN") == "postgresql://x:y@z/w"
    monkeypatch.delenv("OTHER_DSN")
    with pytest.raises(RuntimeError, match="OTHER_DSN"):
        require_dsn("OTHER_DSN")


def test_no_default_password_in_the_module() -> None:
    import pipeline.db_dsn as mod

    assert mod.__file__ is not None
    with open(mod.__file__, encoding="utf-8") as fh:
        source = fh.read()
    assert "baseball" + "_pass" not in source
    assert "postgresql://" not in source.replace("a postgresql:// connection", "")


@pytest.mark.parametrize(
    ("dsn", "label"),
    [
        ("postgresql://baseball_app:SECRET@db:5432/baseball_sim", "db:5432/baseball_sim"),
        ("postgresql://u@db/x?password=SECRET", "db/x"),
        ("host=db dbname=baseball_sim user=baseball_app password=SECRET", "db/baseball_sim"),
        ("host=db port=5432 password='SE CRET' dbname=x", "db:5432/x"),
        ("password=SECRET", "<redacted>"),
    ],
)
def test_dsn_label_never_holds_the_password(dsn: str, label: str) -> None:
    assert dsn_label(dsn) == label
    assert "SECRET" not in dsn_label(dsn)
