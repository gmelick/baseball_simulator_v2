# Postgres roles (SIM-443b)

The database has three login roles. Each process logs in as the weakest role
that can do its job.

| Role | Who logs in as it | What it may do | Password variable |
|---|---|---|---|
| `baseball_user` | the `migrate` service and admin scripts over the db container's socket | everything: it owns the cluster (the superuser) and every table | `POSTGRES_PASSWORD` |
| `baseball_app` | the app, the `live` service, the `jobs` container, host-side loaders | read, insert, update and delete rows in `raw` and `sim`; use their sequences | `BASEBALL_APP_DB_PASSWORD` |
| `baseball_ro` | the SQL console and the data assistant (`BASEBALL_DB_RO_DSN`) | read `raw` only; every transaction is read-only | `BASEBALL_RO_DB_PASSWORD` |

`baseball_app` and `baseball_ro` are `NOSUPERUSER NOCREATEDB NOCREATEROLE
NOINHERIT`. Neither may `TRUNCATE`, create a table or alter one. A schema
change runs only through the `migrate` service, which logs in as the owner.

## The files

- `setup_roles.sh` checks the passwords are set and runs `setup_roles.sql`
  through `psql` over the container's unix socket. It passes the passwords as
  psql variables; it never prints them.
- `setup_roles.sql` creates a missing role, sets both passwords, and applies
  the grants and the default privileges (the grants a later migration's new
  tables receive). It ends with a summary of the roles' flags.

Both are idempotent: run them as often as you like.

## How they run

- **A fresh volume.** The db service mounts `setup_roles.sh` as
  `/docker-entrypoint-initdb.d/10-setup-roles.sh`, so Postgres runs it once on
  first start.
- **An existing volume.** Run `make db-roles`. It resets both passwords from
  `.env` and re-applies the grants. `make db-roles ROTATE_ADMIN_PASSWORD=1`
  also sets `baseball_user`'s password from `POSTGRES_PASSWORD`.

The passwords come from `.env`. Create them with
`python scripts/gen_secrets.py --append .env`.

## The rule for a new schema

The grants name the schemas `raw` and `sim`. A migration that adds a schema
must add its lines to `setup_roles.sql` (the `GRANT USAGE`, the table and
sequence grants and the `ALTER DEFAULT PRIVILEGES`), and the deploy must run
`make db-roles` after `make migrate`. Without them the app cannot read the new
schema's tables.
