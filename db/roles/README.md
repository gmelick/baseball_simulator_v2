# Postgres roles

The statistics database has three login roles. Each process logs in as the
weakest role that can do its job. This folder holds the script that creates the
two weaker roles and grants their privileges (the security hardening, SIM-443).

| Role | Who logs in as it | What it may do | Password in `.env` |
|---|---|---|---|
| `baseball_user` | the `migrate` service; the backup and restore scripts and this folder's script, over the `db` container's socket | everything: it is the superuser and owns every table | `POSTGRES_PASSWORD` |
| `baseball_app` | the `app`, `live` and `jobs` services, and host tools such as the loaders | read, insert, update and delete rows in the `raw` and `sim` schemas; use their sequences | `BASEBALL_APP_DB_PASSWORD` |
| `baseball_ro` | the app's read-only pool: the SQL console, the AI assistant and the Data Lab read routes (schema, analytics, players, data freshness) | read the `raw` schema only; every transaction is read-only | `BASEBALL_RO_DB_PASSWORD` |

Both weaker roles are `NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
NOREPLICATION NOBYPASSRLS`. Neither may truncate, create or alter a table. A
schema change runs only through the `migrate` service (`make migrate`), which
logs in as the owner.

## The files

- [`setup_roles.sh`](setup_roles.sh) checks that the two passwords are set. Then
  it runs `setup_roles.sql` through `psql` over the container's unix socket, as
  `POSTGRES_USER`, so it needs no password to log in. It passes each password as
  a psql variable and never prints it.
- [`setup_roles.sql`](setup_roles.sql) does the work:
  1. It creates the `raw` and `sim` schemas when they are missing, owned by the
     owner. Migration 0001 uses `CREATE SCHEMA IF NOT EXISTS`, so this is safe
     before the first `make migrate`.
  2. It creates a missing role and sets both roles' passwords and flags.
  3. It sets `default_transaction_read_only = on` for `baseball_ro`.
  4. It grants `baseball_app` row access and sequence use in `raw` and `sim`,
     and `baseball_ro` `SELECT` in `raw`. It revokes every privilege
     `baseball_ro` could hold in `sim`.
  5. It sets the default privileges, so a table that a later migration creates
     as the owner gets the same grants.
  6. It prints the three roles' flags. It prints no secret.

Both files are idempotent: a rerun changes nothing but the passwords.

## How they run

- **A new Postgres volume.** The `db` service mounts `setup_roles.sh` as
  `/docker-entrypoint-initdb.d/10-setup-roles.sh`. Postgres runs it once, on the
  volume's first start.
- **An existing volume.** Run `make db-roles`. It runs the script inside the
  running `db` container, resets both passwords from `.env` and re-applies the
  grants.
- **The owner's password.** `make db-roles ROTATE_ADMIN_PASSWORD=1` also sets
  `baseball_user`'s password from `POSTGRES_PASSWORD`. Use it when the volume
  is older than the password in `.env`: Postgres reads `POSTGRES_PASSWORD` only
  when it creates a volume.

Create the passwords with `python scripts/gen_secrets.py --append .env`.

## How the app checks the read-only role

At boot the app opens the read-only pool from `BASEBALL_DB_RO_DSN` with the
search path `raw` ([`api/startup.py`](../../api/startup.py)). It refuses the
pool when the role is a superuser or can write. Then the read-only routes answer
503, and `/health` shows `"sql_console": {"ro_pool": false, ...}`. The app
never falls back to its main pool for those routes.

The search path is `raw` alone, so a query on that pool must qualify any name
outside `raw`. The player search, for one, calls `public.similarity()` (the
trigram match function), not `similarity()`.

## The rule for a new schema

The grants name the schemas `raw` and `sim`. A migration that adds a schema must
also add its lines to `setup_roles.sql`: the `GRANT USAGE`, the table and
sequence grants, and the `ALTER DEFAULT PRIVILEGES`. The deploy then runs
`make db-roles` after `make migrate`. Without both steps the app gets
`permission denied` on the new schema's tables.
