# The security hardening cluster — SIM-443a to SIM-443f (2026-10-13)

Status: BUILT on branch `claude/sim-443-tickets-42c030`, reviewed and verified. NOT YET
DEPLOYED: the live cutover (the run book at the end) needs the owner's go, because it restarts
the serving stack and changes the database passwords. The record is `CHANGES.md` 2026-10-13.

## The six defects and the fixes

The six tickets were filed on 2026-07-27 and deferred until the per-file bug work finished.
This session re-checked each one against the current code; all six still held.

| Ticket | The defect | The fix |
|---|---|---|
| SIM-443a | Postgres :5432, Redis :6379, the app :8000, Prometheus :9090 and Grafana :3000 listened on every host interface | each binds `127.0.0.1`; nginx :80 stays on the LAN (owner decision) |
| SIM-443b | `baseball_pass` sat in 43 tracked files; a literal DSN in compose won over `.env`; `baseball_user` was the cluster superuser and the only login role | secrets only in `.env`; two new roles; compose builds every DSN from required variables; the nightly jobs run in a compose service, so the scheduler INI holds no credential |
| SIM-443c | `require_auth` returned before it read a credential in the deployed `development` setting or when nothing was configured; 30+ data routes and the WebSocket had no gate | the check always runs; a gate on every data router; the WebSocket checks the login itself; `AUTH_DISABLED=1` is the one opt-out (owner decision) |
| SIM-443d | the SQL console checked only the shape of a statement; `pg_authid`, `pg_stat_activity`, `information_schema` and `sim.sim_runs` all read | a parser-backed rule: every table is `raw.<name>` or a CTE of the query (sqlglot, owner decision) |
| SIM-443e | `BASEBALL_DB_RO_DSN` was unset, so the console ran on the superuser pool | the `baseball_ro` role, the variable in compose, and no fallback: the console answers 503 without a safe read-only pool |
| SIM-443f | Redis had no password, and the API unpickled whatever it read from Redis | `requirepass` and a JSON codec with a typed envelope; pickle is gone from the cache |

Owner decisions: (1) `AUTH_DISABLED=1` is the one opt-out, logged at boot and shown on
`/health`; (2) nginx :80 stays on the LAN, Prometheus and Grafana move to loopback, and
Grafana's admin password becomes a required secret; (3) add sqlglot; (4) file three follow-up
rows, not build them (SIM-564, SIM-565, SIM-566).

Two facts shaped the design. CI never runs `docker compose`, so the required-variable syntax
cannot break CI. Ofelia's INI does not expand variables, so a fresh `job-run` container can get
a credential only as a literal; that is why the jobs moved into a compose service.

## The privilege inventory

No Postgres DDL or maintenance statement runs in any runtime path. Every `CREATE` / `DROP` in
`pipeline/batch/*` and `db/sim_store.py` targets DuckDB, and the rebuild attaches Postgres
`READ_ONLY`. These writers need row access (select, insert, update, delete):

- the API: `sim.sim_runs`;
- the live service: `raw.game_lineups`, `raw.players`, `raw.games`, `raw.game_odds`,
  `raw.prop_odds`, `raw.game_bullpen_availability`, `sim.lineup_state`;
- the ETL loaders and `scripts/load_historical_odds.py`: `raw.*`, the archive tables,
  `raw.etl_*` and `raw.pipeline_run_log` included.

Serial ids need USAGE on the sequences. Triggers need no grant. Two consequences:
`public.alembic_version` stays owned by the owner role, so `alembic current` runs through the
`migrate` service; and migration 0001 uses `CREATE SCHEMA IF NOT EXISTS`, so the role script
may create the two schemas and run before the first `make migrate`.

The three roles:

| Role | Who logs in as it | What it may do |
|---|---|---|
| `baseball_user` | the `migrate` service and admin scripts over the db container's socket | the cluster owner and superuser; password `POSTGRES_PASSWORD` (required) |
| `baseball_app` | the app, live and jobs services | CONNECT; USAGE on `raw` and `sim`; SELECT, INSERT, UPDATE, DELETE on every table and USAGE, SELECT on every sequence in both, with default privileges for new ones. No TRUNCATE, REFERENCES, TRIGGER or CREATE. Password `BASEBALL_APP_DB_PASSWORD` |
| `baseball_ro` | the SQL console and the assistant (the read-only pool) | CONNECT; USAGE on `raw`; SELECT on every `raw` table, with default privileges; `sim` revoked; `default_transaction_read_only = on`. Password `BASEBALL_RO_DB_PASSWORD` |

Both new roles are LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION
NOBYPASSRLS. A new schema needs a line in `db/roles/setup_roles.sql`.

## The design, by track

### Track A — loopback ports (SIM-443a)

`docker-compose.yml` prefixes the db, redis, app, prometheus and grafana host ports with
`127.0.0.1:`. nginx is unchanged. No container-to-container path uses a host port: nginx calls
`app:8000`, Prometheus scrapes `app:8000`, Grafana reads `prometheus:9090`. The Vite dev proxy
and the host scripts that point the DSN at `127.0.0.1` keep working.

### Track B — no default password, a least-privilege app role (SIM-443b)

- **The roles.** `db/roles/setup_roles.sh` runs `psql` over the socket with the passwords as
  `-v` variables, inside a subshell, so `set -u` never leaks into the postgres entrypoint that
  sources it on first init. It never uses `set -x`, `-e` or `-a`. `setup_roles.sql` creates
  the two schemas if missing, creates each role if missing, sets each password on every run,
  applies the grants and prints a summary with no secrets. The db service mounts the folder at
  `/opt/baseball/roles` (for `make db-roles`) and the shell script into
  `/docker-entrypoint-initdb.d/` (for a fresh volume). `ROTATE_ADMIN_PASSWORD=1` also sets
  the owner's password from `POSTGRES_PASSWORD`.
- **Compose builds every DSN.** Each secret uses `${VAR:?is not set - run: python
  scripts/gen_secrets.py --append .env}`, so a missing one fails any compose command and names
  it. The app, live and jobs services log in as `baseball_app`; the app's read-only DSN logs in
  as `baseball_ro`; the migrate service logs in as the owner. The app, live and jobs services
  blank `POSTGRES_PASSWORD`, `BASEBALL_RO_DB_PASSWORD` and `GRAFANA_ADMIN_PASSWORD` over their
  `env_file`, so the owner's password never reaches a serving container. The migrate service
  mounts `./db` and `alembic.ini`, so a new migration applies without an image rebuild.
- **The `jobs` service.** The app image (`target: dev`), `command: sleep infinity`,
  `user: appuser`, `mem_limit ${JOBS_MEM_LIMIT:-10g}`, the `/data` volume and the hot-reload
  mounts, health check `test -w /data`. The scheduler waits for it to be healthy.
  `deploy/ofelia/config.ini` holds the three data jobs as `job-exec` into
  `baseball_simulator_v2-jobs-1` with the same schedules and commands, each with
  `user = appuser` (an exec with no user line runs as root, which would bring back the
  root-owned bundle defect of SIM-563). No image, network, volume, environment line or DSN is
  left. `nightly-closing-lines` is unchanged. Behaviour differences: the Sunday 08:00 pair
  shares one memory cap; a stopped `jobs` container fails all three jobs.
- **The DSN sweep.** `pipeline/db_dsn.py`: `require_dsn(var="BASEBALL_DB_DSN")` returns the
  value or raises a `RuntimeError` that names the variable, `.env.example` and
  `scripts/gen_secrets.py`; `dsn_label()` drops the password from a URL, keyword or
  query-string DSN. Every script default, `pool_chain.py`'s `DEFAULT_PG_DSN`, the ETL
  loader's `__main__` and the five nightly shell scripts (`: "${BASEBALL_DB_DSN:?...}"`) now
  fail loudly. An argparse default reads `os.environ.get(..., "")` and `main()` resolves it
  after parsing, so `--help` works with no variable set.
- **Secrets and docs.** `scripts/gen_secrets.py` (standard library, Python 3.11+) writes eight
  keys, each `secrets.token_urlsafe(32)`. `--append` adds missing keys, fills a blank line in
  place and never changes a key that has a value; it reads a file with a byte-order mark.
  `--check` exits 1 and lists every missing or blank key. Both print key names only. The env
  templates, `WORKFLOW.md`, `README.md` and `deploy/README.md` follow.
- **The CI guard.** The credential grep is case-insensitive and skips a line marked `nosec`.
  One new step rejects the retired password outside `docs/` and `CHANGES.md`. Another rejects
  a Postgres DSN whose password is not a placeholder (`CHANGE_ME*`, a bare `${VAR}` or
  `${VAR:?msg}`, `<text>`, `***`); a `${VAR:-default}` counts as a literal.

### Track C — a login on every API route (SIM-443c)

- **`api/auth.py`.** The dev sentinels and the bypasses for `development` and "nothing
  configured" are deleted. `_auth_disabled()` (`AUTH_DISABLED=1`) is the one opt-out helper.
  `_authenticate` reads the session cookie first, then an `X-API-Key` or `Authorization:
  Bearer` key, compared with `hmac.compare_digest` against every configured key.
  `require_auth` answers 401 with `WWW-Authenticate: Bearer realm="sim", ApiKey
  realm="X-API-Key", Cookie realm="sim_session"`. `authenticate_websocket` closes a refused
  handshake with code 1008 before it accepts. `ENVIRONMENT` no longer touches auth; it still
  drives the cookie `secure` flag and the CORS defaults.
- **`api/main.py`.** `validate_environment()` requires `SECRET_KEY` and `AUTH_PASSWORD` in
  every environment (a whitespace-only value counts as missing), unless `AUTH_DISABLED=1`,
  which logs one WARNING. `/health` adds `auth_enforced`.
- **The router gate.** `dependencies=[Depends(require_auth)]` on games, betting, similarity,
  similarity_explorer, players, schema_introspect, data_health, sql_runner, ai_assistant,
  metrics and the live service's `odds_router`. `auth_router` and `ws_router` carry no gate:
  `require_auth` takes a Request, which FastAPI does not supply in a WebSocket scope, so on
  `ws_router` it would fail every handshake with a TypeError. The WebSocket handler calls
  `authenticate_websocket` first. Open routes: `/health`, `/ready`, `/`, `/auth/*`, `/docs`,
  `/redoc`, `/openapi.json`, and the SPA catch-all (mounted only when `frontend/dist` exists).
- **`/auth/me` and `/auth/login`.** `mode` is `cookie`, `api_key`, `disabled` or `none`, and
  `authenticated` is true exactly when `require_auth` would pass. A missing password or signing
  key answers 503 "Login is not configured on this server."
- **The frontend.** `frontend/src/api/unauthorized.ts` raises an event on any 401 from a data
  client (not from `auth.ts`, where a 401 is a wrong password). `AuthContext.tsx` hears it and
  shows the login page, so an expired session no longer leaves error cards on every page.
- **Prometheus.** The `baseball-sim-app` job sends a Bearer token from
  `/etc/prometheus/secrets/app_api_key`; compose mounts `deploy/monitoring/secrets` read-only.
  The token file is gitignored. With no file, the target shows DOWN ("unable to read
  authorization credentials") and no request reaches the app; with a wrong key the app answers
  401.
- **Tests.** `tests/conftest.py` sets `AUTH_DISABLED=1` by default; the two enforcement test
  files remove it.

### Track D — the SQL console reads `raw.*` only (SIM-443d)

`validate_relation_allowlist(sql)` in `api/routes/sql_safety.py` parses with sqlglot
(`read="postgres"`) and fails closed. It rejects a parse failure, more than one statement, a
statement that is not a query, `SELECT ... INTO`, the `TABLE name` shorthand (sqlglot drops its
table inside a CTE), three-part names and a quoted `"RAW"` schema. Every table must be
`raw.<name>` or a CTE in scope: a non-recursive CTE sees only the CTEs declared before it, so
`WITH pg_authid AS (SELECT * FROM pg_authid) ...` is rejected. A function used as a table in
FROM or LATERAL must be on the allowlist (generate_series, unnest, the json/jsonb set
functions, regexp_split_to_table, regexp_matches, string_to_table). A bare table name is
rejected with "write it as raw.<table>". `validate_read_only_sql` calls the check last, so the
console and the assistant both get it. The blocklist gains `_to_xml`, `pg_stat_get_`,
`pg_show_all_`, `pg_hba_file_rules`, `pg_ident_file_mappings`, `pg_config`, `pg_get_`,
`pg_current_logfile`, `pg_backend_pid` and `pg_ls_`. A test passes every starter query of the
console and a list of Postgres syntaxes, so a parser change that breaks a real query reds.

### Track E — the console runs only on the read-only pool (SIM-443e)

`sql_runner._ro_pool` and `ai_assistant._pool` return only `app.state.pg_pool_ro` or answer 503.
The lifespan opens that pool with `search_path=raw` and runs `check_read_only_role`, one query
that reads the role name, the superuser flag, USAGE on `sim`, INSERT on `raw.pitches` and SELECT
on `raw.pitches`. A superuser, a role that can use `sim` or a role that can insert is refused:
the pool is closed and left unset, and the log names the exception type only. `/health` adds
`sql_console: {ro_pool, role}`. `api/state.redact_dsn` masks the password in the one boot line
that shows the DSN.

### Track F — a Redis password and a JSON cache (SIM-443f)

Redis runs with `--requirepass`; `REDISCLI_AUTH` lets its health check pass; the app and live
services build `REDIS_URL` with the password. `simulation/cache_codec.py` writes
`{"t": tag, "v": payload}` (tags `summary`, `prop_set`, `json`) with no `default=` hook, so a
value JSON cannot hold raises and is not cached. `decode` never raises: an old pickle, a
tampered or truncated envelope, a wrong tag or the wrong shape reads as a miss. `RedisCache`
moved its key prefix from `sim332:` to `sim443:`. `GameSimSummary.to_json` / `from_json` round
trip the summary exactly (int64 arrays, confidence intervals, a time-zone-aware time, the
inning grids). Summaries and prop sets never cross the worker pool, so the Redis cache was the
only place that pickled them.

## Follow-ups (filed P3)

- SIM-564: the rate limiter keys its buckets on the raw `X-API-Key` header, which it does not
  verify (it is off by default).
- SIM-565: the engine bundle loads with `np.load(..., allow_pickle=True)`; check the live bundle
  for object arrays first.
- SIM-566: `/api/schema`, `/api/analytics/*` and `/api/players/*` run fixed queries on the main
  pool; move them to the read-only pool.

## Risks to carry into the cutover

- The cookie `secure` flag follows `ENVIRONMENT`. The stack is plain HTTP: keep
  `ENVIRONMENT=development`, or the login loops.
- `${VAR:?}` fails every compose command until `.env` holds the secrets, so step 2 comes first.
- sqlglot is not in the live image. Rebuild the image before the app restarts, or the app does
  not boot.
- The console answers 503 between the app restart and the role creation if the order below is
  not kept; `/health` shows the state.
- Grafana and Postgres both ignore the env password on an existing volume (steps 7 and 8).
- `JOBS_MEM_LIMIT` is a guess (a 10g ceiling). An OOM kill is not retried, so a cap that is too
  low costs one morning's bundle. Measure on the first night.
- The live service's own app on 8001 keeps `/health`, `/api/pipeline/status` and
  `POST .../resimulate` open on the internal network (an accepted risk).

The run book below is copied verbatim from the build plan. The owner runs it.

## Live cutover run book (the owner's main checkout `C:\Users\grego\Documents\baseball_simulator_v2`, cmd.exe; after the merge to `master`; outside game hours; needs the owner's go because it restarts the serving stack)

Ordering principle: the app moves to `baseball_app` BEFORE `baseball_user`'s password changes, so nothing is ever locked out.

1. Safety net: `docker compose exec -T db pg_dumpall -U baseball_user --roles-only > backup_roles_pre_sim443.sql` (holds password hashes; local only; delete afterwards).
2. Secrets: `py scripts\gen_secrets.py --append .env` then `py scripts\gen_secrets.py --check .env` (exit 0). In `.env`, retire the old `BASEBALL_DB_DSN=...baseball_pass...` and `REDIS_URL=` lines (containers ignore them); keep `ENVIRONMENT=development` (the cookie `secure` flag; plain HTTP). `findstr baseball_pass .env` prints nothing. Copy `deploy\monitoring\secrets\app_api_key.example` to `app_api_key` and paste one `API_KEYS` value.
3. `docker compose config --quiet` proves every required variable is set. `docker compose build app live jobs`.
4. `docker compose up -d db` (recreated with the new env + mounts; the old app reconnects with the still-valid old password). Wait for healthy.
5. `make db-roles` (no rotation). Verify over the socket: `\du` shows `baseball_app` / `baseball_ro` with no superuser flag; `has_table_privilege('baseball_app','raw.pitches','INSERT')`, `('baseball_app','sim.sim_runs','UPDATE')`, `('baseball_ro','raw.pitches','SELECT')` → `t t t`; `('baseball_ro','raw.pitches','INSERT')`, `has_schema_privilege('baseball_ro','sim','USAGE')` → `f f`; every `raw`/`sim` sequence `has_sequence_privilege('baseball_app', ..., 'USAGE')` → `t`; a TCP login as each new role via `PGPASSWORD="$VAR" psql -h 127.0.0.1 -U <role>` inside the db container (no echo).
6. `docker compose up -d` (redis with `requirepass`, app, live, jobs, prometheus, grafana; nginx waits on app health), then `docker compose up -d --force-recreate scheduler` (Ofelia reads the INI at start). `docker compose ps` all healthy; `docker compose logs --tail 30 scheduler` lists the four jobs.
7. Rotate the owner: `make db-roles ROTATE_ADMIN_PASSWORD=1`. Verify `docker compose run --rm migrate alembic current` → `0030 (head)`.
8. Grafana ignores the env password on an existing volume: `docker compose exec grafana sh -c "grafana cli admin reset-admin-password \"$GF_SECURITY_ADMIN_PASSWORD\""`.
9. Redis: `docker compose exec redis sh -c "redis-cli ping"` → PONG; `docker compose exec redis sh -c "unset REDISCLI_AUTH; redis-cli ping"` → NOAUTH. After a slate load + one simulation, `redis-cli --scan --pattern 'sim*'` shows only `sim443:` keys whose values start with `{`.
10. Ports: `netstat -ano | findstr /R ":5432 :6379 :8000 :9090 :3000"` → every LISTENING line is `127.0.0.1:`/`[::1]:`, none `0.0.0.0:`; `:80` still `0.0.0.0`. Optional from another LAN machine: `Test-NetConnection <host-ip> -Port 5432` fails.
11. Auth: `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/api/games/2026-10-13` → 401; `curl -H "Authorization: Bearer <key>" http://127.0.0.1:8000/metrics` → 200; `curl http://127.0.0.1:8000/health` → `"auth_enforced": true`, `"sql_console": {"ro_pool": true, "role": "baseball_ro"}`; the browser at `http://localhost/` asks for `AUTH_PASSWORD` once; a live game page shows the socket open; `http://127.0.0.1:9090/targets` shows `baseball-sim-app` UP.
12. Console: `POST /api/sql/run` with `select * from pg_authid` → 400 "only raw.* tables may be read; found pg_authid"; `select count(*) from raw.pitches` → 200; `docker compose logs app | findstr SIM-443e` shows the pool open as `baseball_ro`.
13. Jobs: `docker compose exec jobs python -c "..."` prints `('baseball_app',)`; a dry chain with `INGEST_MARK_DIR=/tmp/marks` and `DAYS=1` exits 0; next morning `docker compose logs scheduler` shows the 07:00/08:00 execs, `docker inspect ...-jobs-1 --format "{{.State.OOMKilled}}"` → false, `docker stats` during the rebuild sizes `JOBS_MEM_LIMIT`.
14. Host-side tools: `setx BASEBALL_DB_DSN "postgresql://baseball_app:<pw>@localhost:5432/baseball_sim"` in a fresh window. Delete the roles backup. Append the DEPLOYED entry to `CHANGES.md`.

Rollback: before step 7, `git checkout <previous-sha> -- docker-compose.yml deploy/ofelia/config.ini Makefile scripts` + `docker compose up -d --force-recreate` (the old files still log in with `baseball_pass`); after step 7, also restore the owner's hash from the roles backup (`type backup_roles_pre_sim443.sql | docker compose exec -T db psql -U baseball_user -d postgres`).
