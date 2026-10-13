# The Prometheus scrape token (SIM-443c)

Every API route requires a login, `/metrics` included. Prometheus logs in
with one of the app's API keys, sent as `Authorization: Bearer <key>`.
`deploy/monitoring/prometheus.yml` reads the key from
`/etc/prometheus/secrets/app_api_key`. Compose mounts this directory there,
read-only.

Git tracks only this README and `app_api_key.example`. The real
`app_api_key` file is in `.gitignore`. Never commit it.

## Set the token

1. Copy `app_api_key.example` to `app_api_key` in this directory.
2. Open `.env` and copy ONE value from `API_KEYS` (a comma-separated list).
3. Replace the placeholder line in `app_api_key` with that value. Keep one
   line and nothing else.
4. Recreate Prometheus: `docker compose up -d --force-recreate prometheus`.
5. Open `http://127.0.0.1:9090/targets`. The `baseball-sim-app` target
   shows UP.

## When the target shows DOWN

- No `app_api_key` file: Prometheus cannot read the token, so it sends no
  request. The target shows DOWN with "unable to read authorization
  credentials", and the app's log shows nothing. Prometheus itself still
  starts (`promtool check config` exits 1).
- The placeholder is still in the file, or the key is wrong: the app answers
  401 and the target shows DOWN.
- You rotated `API_KEYS` in `.env`: copy the new value into `app_api_key`
  and recreate Prometheus.
