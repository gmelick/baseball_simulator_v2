# Deployment — the edge, the monitoring and the scheduler

This folder holds the configuration of the stack's edge and support services:

| Path | What it configures |
|---|---|
| [`nginx/nginx.conf`](nginx/nginx.conf) | nginx, the one service the LAN can reach: it serves the built frontend and proxies the API and the live WebSocket to the app. |
| [`monitoring/prometheus.yml`](monitoring/prometheus.yml) | Prometheus: it scrapes the app's `/metrics` every 15 s. |
| [`monitoring/secrets/`](monitoring/secrets/README.md) | The API key Prometheus sends to `/metrics` (git ignores the real file). |
| [`monitoring/grafana/`](monitoring/grafana/) | Grafana's Prometheus data source and the platform dashboard, loaded at Grafana's start. |
| [`ofelia/config.ini`](ofelia/config.ini) | The scheduler: the nightly data jobs, the backups and the closing-lines pass. |

[`WORKFLOW.md`](../WORKFLOW.md) is the operator's manual: bring-up, the daily
schedule, the health checks and the troubleshooting table. This page explains
how the edge works and how to change it.

## Topology

```
                         ┌───────────── nginx (:80, every interface) ─────────────┐
 browser / LAN ────────▶ │ /            the built frontend (index.html, /assets/) │
                         │ /api/  /auth/  /health  /ready  /metrics               │
                         │ /docs  /redoc  /openapi.json      ──▶ app:8000 (REST)  │
                         │ /ws/          ──▶ app:8000 (WebSocket upgrade)         │
                         └────────────────────────────────────────────────────────┘
 this machine only ───▶ 127.0.0.1: app 8000 · Postgres 5432 · Redis 6379 · Prometheus 9090 · Grafana 3000
```

The containers reach each other by service name on the compose network
`baseball_net` (`app:8000`, `db:5432`, `redis:6379`, `prometheus:9090`). No
container uses a host port.

## nginx

The `nginx` service is not a stock image. Compose builds it from
[`frontend/Dockerfile`](../frontend/Dockerfile): a Node 20 stage runs
`npm run build`, and an `nginx:1.27-alpine` stage copies the built frontend to
`/var/www/baseball-sim` and `nginx.conf` to `/etc/nginx/nginx.conf`. Both are
baked into the image. A change to `nginx.conf` or to the frontend therefore
needs a rebuild:

```bat
docker compose build nginx
docker compose up -d nginx
```

Check a config change before you deploy it:

```bat
docker compose run --rm --no-deps nginx nginx -t
```

### The routes

| Location | What nginx does |
|---|---|
| `/ws/` | Proxies the live-game WebSocket (`/ws/games/{game_pk}`). It speaks HTTP/1.1 and passes the `Upgrade` and `Connection` headers, turns buffering off and keeps an idle socket open for an hour. |
| `/auth/` | Proxies the login routes with a 10 s timeout. A separate block lets the browser's `POST /auth/login` receive its cookie. |
| `/api/` | Proxies the REST API with upstream keep-alive and a 120 s read timeout, the headroom a 100-game simulation needs. |
| `= /health`, `= /ready` | Proxies the probes with the access log off. |
| `= /metrics`, `/docs`, `/redoc`, `= /openapi.json` | Proxies to the app, so these never fall through to the frontend. |
| `/assets/` | Serves the frontend's hashed files with a one-year immutable cache. |
| `*.ico`, `*.svg`, `*.png` and similar | Serves the file with a 30-day cache, or 404. |
| `/` | Serves `index.html` for every other path, so the browser's router handles it. `index.html` itself is never cached, so a deploy takes effect at once. |

The `/ws/`, `/auth/` and `/api/` blocks send `Host`, `X-Real-IP`,
`X-Forwarded-For` and `X-Forwarded-Proto`; the app's rate limiter reads the
client address from `X-Real-IP`. The probe and docs blocks set `Host` alone, and
nginx passes a block's own header lines only, so those requests carry no
`X-Real-IP`. nginx also compresses JSON and text, accepts request bodies up to
10 MB and hides its version.

### When the app container's address changes

A recreate of the app (`docker compose up -d app` after a change) gives the
container a new address. An in-place restart (`docker compose restart app`, or
the app's own restart after a nightly rebuild) keeps the address. nginx follows
a new address by itself: the `baseball_app` upstream block has a `zone`, a
`resolver` on Docker's own DNS (`127.0.0.11`, `valid=10s`) and `resolve` on its
server line. nginx looks the name up again at most every 10 s, so a recreate
costs at most about 10 s of 502 answers. The test
[`tests/unit/test_nginx_resolve.py`](../tests/unit/test_nginx_resolve.py) keeps
those directives in place.

## Ports

| Service | Host address | Who reaches it |
|---|---|---|
| nginx | `0.0.0.0:${NGINX_HTTP_PORT:-80}` | every interface: the LAN uses the site here |
| app | `127.0.0.1:${APP_HOST_PORT:-8000}` | this machine only (host scripts, debugging) |
| Postgres | `127.0.0.1:${DB_HOST_PORT:-5432}` | this machine only |
| Redis | `127.0.0.1:${REDIS_HOST_PORT:-6379}` | this machine only |
| Prometheus | `127.0.0.1:${PROMETHEUS_HOST_PORT:-9090}` | this machine only |
| Grafana | `127.0.0.1:${GRAFANA_HOST_PORT:-3000}` | this machine only |

Set a port variable in `.env` to move a service off a busy port. To check the
bindings, `netstat -ano | findstr /R ":5432 :6379 :8000 :9090 :3000"` must list
only `127.0.0.1:` or `[::1]:` addresses, and `:80` lists `0.0.0.0`.

## Secrets

`docker-compose.yml` has no default for any password. It reads each one as
`${VAR:?...}`, so a missing value fails every compose command and names the
variable. `python scripts/gen_secrets.py --append .env` writes every missing or
blank secret and prints key names only.

| Variable | What it protects |
|---|---|
| `POSTGRES_PASSWORD` | `baseball_user`, the cluster owner. Only the `migrate` service logs in as it. |
| `BASEBALL_APP_DB_PASSWORD` | `baseball_app`: the app, `live` and `jobs` services. |
| `BASEBALL_RO_DB_PASSWORD` | `baseball_ro`: the app's read-only pool. |
| `REDIS_PASSWORD` | Redis's `requirepass`. |
| `GRAFANA_ADMIN_PASSWORD` | Grafana's `admin` login. Grafana reads it only when its volume is new. |
| `SECRET_KEY` | The key that signs the login cookie. The app refuses to start without it. |
| `AUTH_PASSWORD` | The browser's login password. The app refuses to start without it. |
| `API_KEYS` | A comma-separated list of keys for scripts and Prometheus. |

Compose builds every in-container connection string from these passwords. The
`app`, `live` and `jobs` services blank `POSTGRES_PASSWORD`,
`BASEBALL_RO_DB_PASSWORD` and `GRAFANA_ADMIN_PASSWORD` in their environment, so
the owner's password never reaches a serving container.
[`db/roles/README.md`](../db/roles/README.md) describes the three database roles.

## Login

Every API route and the live WebSocket need a credential, in every tier
([`api/auth.py`](../api/auth.py)):

| Path | What it needs |
|---|---|
| `/health`, `/ready`, `/`, `/docs`, `/redoc`, `/openapi.json`, `/auth/*` | nothing |
| every other `/api/` route, and `/metrics` | the login cookie, or an API key as `X-API-Key: <key>` or `Authorization: Bearer <key>` |
| `/ws/games/{game_pk}` | the login cookie or an API key; without one the socket closes with code 1008 |

The browser logs in once with `AUTH_PASSWORD` (`POST /auth/login`). The one
opt-out is `AUTH_DISABLED=1`: the app logs a warning at boot and `/health`
reports `"auth_enforced": false`. Never set it on a stack the LAN can reach.

### The Prometheus token

Prometheus scrapes `/metrics` with a Bearer key that it reads from
`deploy/monitoring/secrets/app_api_key`.
[`monitoring/secrets/README.md`](monitoring/secrets/README.md) gives the steps.
Without the file the `baseball-sim-app` target shows DOWN; Prometheus still
starts.

## Monitoring

Prometheus keeps its series in the `prometheus_data` volume and scrapes two
targets: the app and itself. It has no alert rules and no Alertmanager.

Grafana provisions the data source (`http://prometheus:9090`) and loads every
dashboard JSON in `monitoring/grafana/` into the folder "Baseball Sim". The one
dashboard, `baseball_sim_dashboard.json`, shows six panels: the newest
simulation's latency, the API's p95 response time, the live pipeline's
freshness, the requests served, the build info and the age of the newest final
game. The app's `/metrics` also exposes the backup ages
(`baseball_sim_backup_age_hours`, `baseball_sim_offsite_backup_age_hours`),
which no panel shows yet.

## The scheduler

The `scheduler` service runs Ofelia, a cron for Docker. It reads
[`ofelia/config.ini`](ofelia/config.ini) and runs each job with `docker exec`
inside a running container, through the read-only Docker socket. The INI holds
no credential: a job inherits the target container's environment.
[`WORKFLOW.md`](../WORKFLOW.md) lists the jobs, their times and what each waits
for.

Two rules for editing the INI:

- A job with no `user =` line runs as root inside its container. Every job in
  the `jobs` container names `user = appuser`, so the files it writes stay
  readable by the app.
- The `container =` lines name the compose default container names
  (`baseball_simulator_v2-jobs-1` and so on). A stack under another project
  name needs its own names there.

Ofelia reads the INI once, at its start. After an edit, recreate it:
`docker compose up -d --force-recreate scheduler`.

## Environment tiers

The repository ships three templates: `.env.example` (this laptop),
`.env.staging.example` and `.env.production.example`. Two compose rules decide
which value a container sees:

1. A key in a service's `environment:` block beats the same key in its
   `env_file`. The `app` service fixes its database and Redis connection
   strings, the replay store, the calibration path, the DuckDB path and every
   `SIM_*` flag there, so a template value for those keys never reaches the
   app. `ENVIRONMENT` and `TRUSTED_PROXY_HOSTS` sit in the same block, but as
   `${VAR}` substitutions: compose takes their values from `.env`.
2. Every service's `env_file` names `.env`. `docker compose --env-file X` changes
   only the `${VAR}` substitution inside the compose file; the containers still
   read `.env`. So on a staging or production host, put the tier's values in
   that host's `.env`.

The keys the code reads that differ by tier:

| Key | What it does | `.env.example` | staging template | production template |
|---|---|---|---|---|
| `ENVIRONMENT` | `development` allows the localhost CORS origins and an insecure cookie. Any other value marks the login cookie `secure` (HTTPS only). | `development` | `staging` | `production` |
| `RATE_LIMIT_PER_MINUTE` | Requests a minute per API key, else per client address; `0` or unset is off. | unset | `120` | `300` |
| `TRUSTED_PROXY_HOSTS` | The proxies whose `X-Real-IP` the rate limiter believes. Compose sets `nginx` when unset; an empty value turns the trust off. | unset | commented | commented |
| `CORS_ORIGINS` / `FRONTEND_URL` | The browser origins the API accepts. With neither set, development allows three localhost origins and other tiers one. | unset | the staging origin | the production origin |
| `ODDS_PROVIDER` / `ODDS_API_KEY` | The odds source: `mock` (the default), or `bettingpros` / `real` (both the BettingPros provider, which needs the key). | commented | `mock` | see the gaps below |

Keep `ENVIRONMENT=development` on a stack that serves plain HTTP. Any other
value marks the cookie `secure`, the browser drops it over HTTP, and the login
loops.

### Known gaps in the two tier templates

The staging and production templates predate the compose stack's current
shape. Fix these before you use one:

- `ODDS_PROVIDER=theoddsapi` (production) names no registered provider. The
  live service fails at its start, and the app's odds routes fail, with
  "Unknown ODDS_PROVIDER". Use `bettingpros`.
- `LIVE_PIPELINE_ENABLED=true` starts a second live pipeline inside the app. On
  this compose file the `live` service is the one publisher; leave the key unset
  or `false`.
- `WORKERS`, `SIM_MAX_WORKERS`, `ODDS_PROVIDER_API_KEY`, `ODDS_API_BASE`,
  `ASYNC_DB_DSN`, `MLB_API_BASE` and `PROMETHEUS_PUSHGATEWAY` are read by no
  code. The app's worker count is `SIM_RUNNER_WORKERS`.
- `CALIBRATION_REPORT_PATH=/data/calibration_report.json` differs from the path
  compose sets (`/data/calibration.json`); compose wins.

## Enabling TLS

The `:443` server block in `nginx.conf` is a commented stub with placeholder
location blocks. To serve HTTPS:

1. Write the `:443` server block in `nginx/nginx.conf`: copy the `:80` server's
   location blocks into it and keep its `ssl_*` lines.
2. Put the certificate files in `deploy/nginx/certs/` (`fullchain.pem`,
   `privkey.pem`).
3. In `docker-compose.yml`, uncomment the `NGINX_HTTPS_PORT` port line and the
   certificate volume of the `nginx` service.
4. Rebuild and restart nginx (`docker compose build nginx`, then
   `docker compose up -d nginx`).
5. Set `ENVIRONMENT` to `staging` or `production` in `.env`, so the cookie is
   marked `secure`, and recreate the app.
