"""SIM-443b — the connection-string reader has no default password."""

from __future__ import annotations

import pytest

from db.dsn import _redact, attach_pg, dsn_label, require_dsn


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
    import db.dsn as mod

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
        ("host=db port=5432 password='SE CRET' dbname=x", "db:5432/x"),  # nosec - test-only literal
        ("password=SECRET", "<redacted>"),
    ],
)
def test_dsn_label_never_holds_the_password(dsn: str, label: str) -> None:
    assert dsn_label(dsn) == label
    assert "SECRET" not in dsn_label(dsn)


def test_redact_drops_a_url_dsn_and_its_password() -> None:
    dsn = "postgresql://user:hunter2@db:5432/baseball_sim"
    msg = f"IO Error: Unable to connect to Postgres at {dsn}: password hunter2 rejected"
    out = _redact(msg, dsn)
    assert "hunter2" not in out
    assert dsn not in out


def test_redact_drops_a_keyword_dsn() -> None:
    """A DuckDB ATTACH failure quotes the connection string back."""
    dsn = "host=db port=5432 user=x password=hunter2SECRET dbname=y"
    out = _redact(f'Failed to connect using "{dsn}": connection refused', dsn)
    assert "hunter2SECRET" not in out
    assert "connection refused" in out


def test_attach_pg_is_a_no_op_when_pg_is_attached() -> None:
    """A second attach must not reach for Postgres (the in-memory ``pg`` stays)."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(":memory:")
    con.execute("ATTACH ':memory:' AS pg")
    con.execute("CREATE TABLE pg.t AS SELECT 1 AS x")
    attach_pg(con, dsn="postgresql://nobody:secret@nowhere:1/none")
    assert con.execute("SELECT x FROM pg.t").fetchone()[0] == 1


class _FailingAttach:
    """A connection whose ATTACH fails with the DSN quoted back, as DuckDB's does."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, sql: str) -> _FailingAttach:
        self.executed.append(sql)
        if sql.startswith("ATTACH"):
            raise RuntimeError(f"IO Error: Unable to connect at {sql}: password hunter2 rejected")
        return self

    def fetchall(self) -> list:
        return []


def test_attach_pg_redacts_the_password_from_a_failed_attach() -> None:
    con = _FailingAttach()
    with pytest.raises(RuntimeError) as info:
        attach_pg(con, dsn="postgresql://user:hunter2@db:5432/x")
    assert str(info.value).startswith("Postgres attach failed: ")
    assert "hunter2" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert con.executed[1:] == [
        "INSTALL postgres; LOAD postgres;",
        "ATTACH 'postgresql://user:hunter2@db:5432/x' AS pg (TYPE postgres, READ_ONLY);",
    ]
