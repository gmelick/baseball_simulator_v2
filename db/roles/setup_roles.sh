#!/bin/sh
# =============================================================================
# SIM-443b: create the two least-privilege Postgres roles and grant them.
#
#   baseball_app  the app, the live service and the nightly jobs: read and
#                 write rows in raw and sim; no DDL, no TRUNCATE.
#   baseball_ro   the SQL console: read raw only; every transaction read-only.
#
# The script is idempotent. Every run creates a missing role, resets both
# passwords from the environment and re-applies the grants. Two callers:
#
#   1. A fresh Postgres volume: the image's entrypoint runs it once from
#      /docker-entrypoint-initdb.d/10-setup-roles.sh. A bind mount carries no
#      exec bit on Windows, so the entrypoint SOURCES the file. The body runs
#      in a subshell, so its `set -u` and its variables never leak into the
#      entrypoint, and a failure still fails the init.
#   2. An existing volume: `make db-roles`, which runs
#      `sh /opt/baseball/roles/setup_roles.sh` inside the db container.
#      `make db-roles ROTATE_ADMIN_PASSWORD=1` also sets the owner's
#      (POSTGRES_USER) password from POSTGRES_PASSWORD.
#
# It connects over the container's unix socket as POSTGRES_USER, so it needs
# no password to log in. It passes the passwords to psql as variables, and the
# SQL quotes them as literals (:'name'). It never echoes a command: no `set -x`.
# =============================================================================
(
    set -u

    roles_sql="${ROLES_SQL:-/opt/baseball/roles/setup_roles.sql}"
    admin_role="${POSTGRES_USER:-baseball_user}"
    db_name="${POSTGRES_DB:-baseball_sim}"
    rotate_admin="off"
    if [ "${ROTATE_ADMIN_PASSWORD:-0}" = "1" ]; then
        rotate_admin="on"
    fi

    missing=""
    [ -n "${BASEBALL_APP_DB_PASSWORD:-}" ] || missing="$missing BASEBALL_APP_DB_PASSWORD"
    [ -n "${BASEBALL_RO_DB_PASSWORD:-}" ] || missing="$missing BASEBALL_RO_DB_PASSWORD"
    if [ "$rotate_admin" = "on" ] && [ -z "${POSTGRES_PASSWORD:-}" ]; then
        missing="$missing POSTGRES_PASSWORD"
    fi
    if [ -n "$missing" ]; then
        echo "setup_roles: not set:$missing - run: python scripts/gen_secrets.py --append .env" >&2
        exit 1
    fi
    if [ ! -r "$roles_sql" ]; then
        echo "setup_roles: cannot read $roles_sql" >&2
        exit 1
    fi

    if [ "$rotate_admin" = "on" ]; then
        psql -q -X --no-password -v ON_ERROR_STOP=1 \
            -U "$admin_role" -d "$db_name" \
            -v admin_role="$admin_role" \
            -v db_name="$db_name" \
            -v app_password="$BASEBALL_APP_DB_PASSWORD" \
            -v ro_password="$BASEBALL_RO_DB_PASSWORD" \
            -v rotate_admin="$rotate_admin" \
            -v admin_password="$POSTGRES_PASSWORD" \
            -f "$roles_sql"
    else
        psql -q -X --no-password -v ON_ERROR_STOP=1 \
            -U "$admin_role" -d "$db_name" \
            -v admin_role="$admin_role" \
            -v db_name="$db_name" \
            -v app_password="$BASEBALL_APP_DB_PASSWORD" \
            -v ro_password="$BASEBALL_RO_DB_PASSWORD" \
            -v rotate_admin="$rotate_admin" \
            -v admin_password="" \
            -f "$roles_sql"
    fi
    status=$?
    if [ "$status" -ne 0 ]; then
        echo "setup_roles: psql failed (exit $status)" >&2
        exit "$status"
    fi
    echo "setup_roles: baseball_app and baseball_ro are set up"
)
