# Deployment — reverse proxy & environment tiers (SIM-373)

This directory holds the production-edge infrastructure: an **nginx reverse
proxy** in front of the FastAPI `app` service, plus the three environment
tiers (development / staging / production).

## Topology

```
            :80 / :443                 app:8000 (in-network)
client ───────────────▶  nginx  ───────────────▶  FastAPI (uvicorn)
                          │                         ├── REST   /api/*
                          │  REST + ops + docs ─────┤   ops    /health /ready
                          │                         │   docs   /docs /openapi.json
                          └─ WebSocket /ws/ ────────┴── WS     /ws/games/{game_pk}
                             (HTTP/1.1 Upgrade)
```

- `nginx` (`nginx:1.27-alpine`) terminates client connections on the host
  (`:80`, with a `:443` TLS stub) and proxies to the app container at
  `app:8000` over the compose `baseball_net` bridge network.
- The app's internal port is **8000** (Dockerfile `EXPOSE 8000`, uvicorn
  `--port 8000`). nginx reaches it via the `baseball_app` upstream
  (`server app:8000`).

## Routing (`deploy/nginx/nginx.conf`)

| Path prefix                                  | Handling |
|----------------------------------------------|----------|
| `/ws/` (`/ws/games/{game_pk}`)               | **WebSocket** — `proxy_http_version 1.1` + `Upgrade`/`Connection: upgrade` headers, buffering off, ~1h read timeout for idle live-game sockets. |
| `/` (catches `/api/*`, `/ready`, `/docs`, `/openapi.json`, root) | REST proxy with upstream keep-alive and a 120s read timeout (headroom for multi-second `/simulate`). |
| `= /health`                                  | Fast health proxy, access log off (for LB / uptime probes). |

Cross-cutting: `gzip` on JSON/text, `client_max_body_size 10m`, standard
`Host` / `X-Real-IP` / `X-Forwarded-For` / `X-Forwarded-Proto` headers,
`server_tokens off`.

### WebSocket-upgrade note

Browsers open `/ws/games/{game_pk}` with an HTTP `Upgrade: websocket` header.
nginx defaults to HTTP/1.0 to the upstream and strips hop-by-hop headers, which
breaks the handshake. The `/ws/` block fixes this with:

```nginx
proxy_http_version 1.1;
proxy_set_header   Upgrade    $http_upgrade;
proxy_set_header   Connection $connection_upgrade;   # via map directive
```

The `map $http_upgrade $connection_upgrade` block ensures normal requests get
`Connection: ""` (keep-alive friendly) while WS requests get
`Connection: upgrade`.

## Published ports (SIM-443a)

| Service | Host address | Who reaches it |
|---|---|---|
| nginx | `0.0.0.0:${NGINX_HTTP_PORT:-80}` | every interface: the LAN uses the site here |
| app | `127.0.0.1:${APP_HOST_PORT:-8000}` | this machine only (debugging, host scripts) |
| Postgres | `127.0.0.1:${DB_HOST_PORT:-5432}` | this machine only |
| Redis | `127.0.0.1:${REDIS_HOST_PORT:-6379}` | this machine only |
| Prometheus | `127.0.0.1:${PROMETHEUS_HOST_PORT:-9090}` | this machine only |
| Grafana | `127.0.0.1:${GRAFANA_HOST_PORT:-3000}` | this machine only |

The containers reach each other over `baseball_net` (`app:8000`, `db:5432`,
`redis:6379`, `prometheus:9090`); no container uses a host port. To check:
`netstat -ano | findstr /R ":5432 :6379 :8000 :9090 :3000"` lists only
`127.0.0.1:` or `[::1]:` addresses, and `:80` lists `0.0.0.0`.

## Secrets (SIM-443b, SIM-443f)

`docker-compose.yml` has no default for any password. It reads each with
`${VAR:?...}`, so a missing one fails every compose command and names the
variable. Create the missing ones with `python scripts/gen_secrets.py --append .env`.

| Variable | What it protects |
|---|---|
| `POSTGRES_PASSWORD` | `baseball_user`, the cluster owner. Only the `migrate` service logs in as it. |
| `BASEBALL_APP_DB_PASSWORD` | `baseball_app`: the app, `live` and `jobs` (rows in `raw` and `sim`, no DDL). |
| `BASEBALL_RO_DB_PASSWORD` | `baseball_ro`: the SQL console (reads `raw` only, read-only transactions). |
| `REDIS_PASSWORD` | Redis `requirepass`; compose builds `REDIS_URL` from it. |
| `GRAFANA_ADMIN_PASSWORD` | Grafana's `admin` login (read on a new volume only). |

Compose builds every in-container DSN from these passwords; `db/roles/README.md`
describes the three roles and `make db-roles` creates or refreshes them.

## Login (SIM-443c)

Every API route and the live WebSocket require a credential in EVERY tier.
`ENVIRONMENT` no longer turns auth on or off.

| Route group | What it needs |
|---|---|
| `/health`, `/ready`, `/`, `/docs`, `/redoc`, `/openapi.json`, `/auth/*` | nothing (open) |
| every other `/api/*` route, `/metrics` | the session cookie, or an API key (`X-API-Key: <key>` or `Authorization: Bearer <key>`) |
| `/ws/games/{game_pk}` | the session cookie (the browser sends it on the handshake) or an API key; otherwise the socket closes with code 1008 |

The browser logs in once with `AUTH_PASSWORD` (`POST /auth/login`); the app
signs the cookie with `SECRET_KEY`. The app refuses to start without both.
`API_KEYS` is a comma-separated list for scripts and Prometheus. The one
opt-out is `AUTH_DISABLED=1`: the app logs a WARNING at boot and `/health`
reports `"auth_enforced": false`. Never set it on a stack the LAN can reach.

### The Prometheus token

Prometheus scrapes `/metrics` with a Bearer key read from a file:

1. Copy `deploy/monitoring/secrets/app_api_key.example` to
   `deploy/monitoring/secrets/app_api_key` (git ignores it).
2. Paste one of the `API_KEYS` values into it, on one line.
3. `docker compose up -d --force-recreate prometheus`.
4. `http://127.0.0.1:9090/targets` shows `baseball-sim-app` UP. A missing
   file shows the target DOWN; Prometheus still starts.

`deploy/monitoring/secrets/README.md` repeats these steps next to the file.
When you rotate `API_KEYS`, copy the new value into the file.

## Running the stack behind nginx

```bash
# Dev — app is also published on the loopback address :8000 (handy for
# debugging) and nginx fronts it on :80.
docker compose up -d
curl http://localhost/health          # via nginx
curl http://127.0.0.1:8000/health     # direct to app (this machine only)
curl -H "X-API-Key: $API_KEY" http://127.0.0.1:8000/metrics   # a protected route

# Validate the nginx config (where the nginx binary/image is available):
docker compose run --rm --no-deps nginx nginx -t
```

Override host ports via env (`.env`): `NGINX_HTTP_PORT`, `NGINX_HTTPS_PORT`,
`APP_HOST_PORT`, `DB_HOST_PORT`, `REDIS_HOST_PORT`, `PROMETHEUS_HOST_PORT`,
`GRAFANA_HOST_PORT`.

## Environment tiers

| Setting                       | development (`.env.example`) | staging (`.env.staging.example`) | production (`.env.production.example`) |
|-------------------------------|------------------------------|----------------------------------|----------------------------------------|
| `ENVIRONMENT`                 | `development`                | `staging`                        | `production` |
| Login (cookie or `API_KEYS`)  | **enforced** (SIM-443c)      | **enforced**                     | **enforced** |
| `RATE_LIMIT_PER_MINUTE`       | unset / 0 (off)              | `120`                            | `300` |
| `CORS_ORIGINS`                | `*` (dev fallback)           | staging frontend origin          | prod frontend origin (**never `*`**) |
| `WORKERS`                     | `1`                          | `3`                              | `9` (≈ 2·vCPU+1) |
| `LIVE_PIPELINE_ENABLED`       | `false`                      | `true`                           | `true` |
| `REPLAY_PERSISTENCE_ENABLED`  | `false`                      | `true`                           | `true` |
| `ODDS_PROVIDER`               | `mock`                       | `mock` (or real)                 | real (`theoddsapi`) |
| DB / Redis hosts              | compose `db` / `redis`       | staging hosts                    | prod hosts |
| TLS (`:443`)                  | off                          | optional                         | on (enable cert mount) |
| Session cookie `secure` flag  | off                          | on                               | on |

Auth/CORS behavior is driven by `api/auth.py`. In every tier a missing or
invalid credential returns `401`. Outside `development` the wildcard CORS
fallback is disabled and the session cookie is marked `secure`, so the
browser sends it over HTTPS only: keep `ENVIRONMENT=development` on a stack
that serves plain HTTP, or the login loops.

### Usage

```bash
cp .env.staging.example    .env.staging      # then fill in CHANGE_ME values
cp .env.production.example  .env.production   # inject secrets from your manager
```

Point compose at a tier with `--env-file` (and override the app build target to
the lean `runtime` stage for non-dev):

```bash
docker compose --env-file .env.production up -d
```

> Secrets (`SECRET_KEY`, `AUTH_PASSWORD`, `API_KEYS`, the three Postgres
> passwords, `REDIS_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`, `ODDS_PROVIDER_API_KEY`)
> must come from your secrets manager — the `*.example` files only show the shape.

## Enabling TLS (staging/prod)

1. Uncomment the `:443` server block in `deploy/nginx/nginx.conf` and the
   cert volume mount + `NGINX_HTTPS_PORT` mapping in `docker-compose.yml`.
2. Mount real certs at `deploy/nginx/certs/` (`fullchain.pem`, `privkey.pem`)
   — e.g. via certbot or your platform's certificate secret.
