# Database migrations

This folder holds the schema changes for both databases. The
[Database](../../docs/technical/database.md) page lists every migration and every table.

## Postgres: Alembic (`versions/`)

Each Postgres schema change is one Alembic file, `versions/NNNN_<ticket>_<subject>.py`,
with `revision = "NNNN"`. [env.py](env.py) reads the database address from
`BASEBALL_DB_DSN`; [`alembic.ini`](../../alembic.ini) at the repository root points here.
[script.py.mako](script.py.mako) is the template Alembic copies for a new file.

Apply the migrations with `make migrate`. It runs the `migrate` service, the only
service that logs in as the database owner. Run any other Alembic command through the
same service:

```bat
docker compose run --rm migrate alembic current
```

## DuckDB: numbered SQL (`duckdb/`)

Each DuckDB schema change is one file, `duckdb/NNNN_<ticket>_<subject>.sql`, and it
raises [`db/schemas/duckdb_schema_version.txt`](../schemas/duckdb_schema_version.txt) to
the same number. The same change also goes into
[`db/schemas/02_duckdb_schema.sql`](../schemas/02_duckdb_schema.sql), which the profile
computor applies to a file that has no schema yet, so a new file and a migrated file
match. The
statements are idempotent (`IF NOT EXISTS`). An added column goes last in its table,
because the pool builders insert by position.

The app applies the replay files (0008 to 0010 and 0032) to `/data/replay.duckdb` itself
(`db/sim_store.ensure_replay_schema`). A file that changes the analytics file runs by hand
or through the rebuild script its ticket names. Apply migration 0031 only through
`scripts/rebuild_steal_pool.py --apply-migration`.

## The rules

- Every Postgres schema change ships an Alembic file. Every DuckDB schema change ships a
  numbered file and bumps the version file.
- The living docs state the current versions as `Alembic head **NNNN**` and
  `DuckDB schema **vNN**`. Two unit tests (`test_docs_alembic_head.py`,
  `test_docs_duckdb_version.py`) fail until the docs match the newest files.
