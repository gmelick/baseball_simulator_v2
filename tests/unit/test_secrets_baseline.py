"""The secrets baseline: ``.env.example`` documents the required variables,
``.gitignore`` keeps ``.env`` out of git, ``python-dotenv`` is a requirement, the
API refuses to boot without its variables, CI rejects a committed ``.env``, and
the historical loader reads its DSN from the environment."""

from __future__ import annotations

import os
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


class TestSecretsBaseline:
    """Each artefact of the baseline is in place."""

    def test_env_example_exists_and_documents_required_vars(self) -> None:
        path = _ROOT / ".env.example"
        assert path.exists(), "SIM-153: .env.example missing"
        text = _read(path)
        for required in ("BASEBALL_DB_DSN", "REDIS_URL"):
            assert required in text, f"SIM-153: .env.example must document {required}"

    def test_gitignore_excludes_dotenv(self) -> None:
        path = _ROOT / ".gitignore"
        assert path.exists()
        assert re.search(r"^\.env\b", _read(path), re.MULTILINE), (
            "SIM-153: .gitignore must explicitly list `.env`"
        )

    def test_python_dotenv_in_requirements(self) -> None:
        text = _read(_ROOT / "requirements.txt")
        assert "python-dotenv" in text, "SIM-153: python-dotenv must be in requirements.txt"

    def test_validate_environment_function_exists(self) -> None:
        from api.main import validate_environment

        # Save current state, force-clear, call, expect RuntimeError.
        saved = {k: os.environ.pop(k, None) for k in ("BASEBALL_DB_DSN", "REDIS_URL")}
        try:
            with pytest.raises(RuntimeError, match="Missing required"):
                validate_environment()
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_ci_workflow_has_secrets_check_job(self) -> None:
        ci = _read(_ROOT / ".github" / "workflows" / "ci.yml")
        assert "secrets-check" in ci, "SIM-153: CI workflow must define a `secrets-check` job"
        assert "Reject committed .env" in ci or "committed .env" in ci, (
            "SIM-153: secrets-check job must reject committed .env files"
        )

    def test_loader_falls_back_to_env_dsn(self) -> None:
        """HistoricalDataLoader reads BASEBALL_DB_DSN from the environment when
        no explicit dsn parameter is passed."""
        from pipeline.etl.etl_historical_loader import HistoricalDataLoader

        saved = os.environ.pop("BASEBALL_DB_DSN", None)
        try:
            with pytest.raises(RuntimeError, match="BASEBALL_DB_DSN"):
                HistoricalDataLoader()
            os.environ["BASEBALL_DB_DSN"] = "postgresql://from:env@localhost/db"
            loader = HistoricalDataLoader()
            assert loader.dsn == "postgresql://from:env@localhost/db"
            loader2 = HistoricalDataLoader(dsn="postgresql://explicit:pwd@localhost/db")
            assert loader2.dsn == "postgresql://explicit:pwd@localhost/db"
        finally:
            os.environ.pop("BASEBALL_DB_DSN", None)
            if saved is not None:
                os.environ["BASEBALL_DB_DSN"] = saved
