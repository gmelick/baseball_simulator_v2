"""
api/env_check.py
================
The environment check the app runs first at startup.

:func:`validate_environment` stops a misconfigured container at boot, before it
opens any connection. It logs under ``api.main``, the logger of the lifespan
that calls it.
"""

from __future__ import annotations

import logging
import os

from api.auth import _auth_disabled

log = logging.getLogger("api.main")


_REQUIRED_ENV_VARS = [
    "BASEBALL_DB_DSN",
    "REDIS_URL",
]

# The auth variables are required in every environment (the login on every
# route, SIM-443c). The one opt-out is AUTH_DISABLED=1, which
# validate_environment() logs as a warning.
_REQUIRED_AUTH_ENV_VARS = [
    "SECRET_KEY",  # HMAC signing key for session tokens
    "AUTH_PASSWORD",  # shared browser login password
]


def validate_environment() -> None:
    """Check that every required environment variable is set; raise RuntimeError if not.

    The error lists what is missing. The lifespan calls it first, so a
    misconfigured container fails at boot instead of running with broken
    connections.

    The auth variables (SECRET_KEY, AUTH_PASSWORD) are required in every
    environment; ``ENVIRONMENT`` plays no part. A blank or whitespace-only value
    counts as missing. With ``AUTH_DISABLED=1`` the function logs one warning
    and skips the auth check; the test suite runs this way (tests/conftest.py).
    """
    missing = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            f"Copy .env.example to .env and fill in the required values."
        )

    if _auth_disabled():
        log.warning(
            "SIM-443c: AUTH_DISABLED is set - every API route and the live "
            "WebSocket accept requests with no credentials."
        )
        return

    missing_auth = [v for v in _REQUIRED_AUTH_ENV_VARS if not os.environ.get(v, "").strip()]
    if missing_auth:
        raise RuntimeError(
            f"Missing required auth variable(s): {', '.join(missing_auth)}. "
            f"Run: python scripts/gen_secrets.py --append .env "
            f"(or set AUTH_DISABLED=1 to run with no login)."
        )
    log.info("SIM-443c: login enforced on every API route and the live WebSocket.")
