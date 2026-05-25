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

## Running the stack behind nginx

```bash
# Dev — app is also published directly on :8000 (handy for debugging) and
# nginx fronts it on :80.
docker compose up -d
curl http://localhost/health          # via nginx
curl http://localhost:8000/health     # direct to app

# Validate the nginx config (where the nginx binary/image is available):
docker compose run --rm --no-deps nginx nginx -t
```

Override host ports via env (`.env`): `NGINX_HTTP_PORT`, `NGINX_HTTPS_PORT`,
`APP_HOST_PORT`, `DB_HOST_PORT`, `REDIS_HOST_PORT`.

## Environment tiers

| Setting                       | development (`.env.example`) | staging (`.env.staging.example`) | production (`.env.production.example`) |
|-------------------------------|------------------------------|----------------------------------|----------------------------------------|
| `ENVIRONMENT`                 | `development`                | `staging`                        | `production` |
| API-key auth (`API_KEYS`)     | off (pass-through)           | **enforced**                     | **enforced** |
| `RATE_LIMIT_PER_MINUTE`       | unset / 0 (off)              | `120`                            | `300` |
| `CORS_ORIGINS`                | `*` (dev fallback)           | staging frontend origin          | prod frontend origin (**never `*`**) |
| `WORKERS`                     | `1`                          | `3`                              | `9` (≈ 2·vCPU+1) |
| `LIVE_PIPELINE_ENABLED`       | `false`                      | `true`                           | `true` |
| `REPLAY_PERSISTENCE_ENABLED`  | `false`                      | `true`                           | `true` |
| `ODDS_PROVIDER`               | `mock`                       | `mock` (or real)                 | real (`theoddsapi`) |
| DB / Redis hosts              | compose `db` / `redis`       | staging hosts                    | prod hosts |
| TLS (`:443`)                  | off                          | optional                         | on (enable cert mount) |

Auth/CORS behavior is driven by `api/auth.py`: outside `development`, the
wildcard CORS fallback is disabled and (when `API_KEYS` is set) missing/invalid
`X-API-Key` returns `401`.

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

> Secrets (`SECRET_KEY`, `API_KEYS`, DB password, `ODDS_PROVIDER_API_KEY`) must
> come from your secrets manager — the `*.example` files only show the shape.

## Enabling TLS (staging/prod)

1. Uncomment the `:443` server block in `deploy/nginx/nginx.conf` and the
   cert volume mount + `NGINX_HTTPS_PORT` mapping in `docker-compose.yml`.
2. Mount real certs at `deploy/nginx/certs/` (`fullchain.pem`, `privkey.pem`)
   — e.g. via certbot or your platform's certificate secret.
