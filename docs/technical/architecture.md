# Architecture

This page describes the system one level above the code: the parts, where data lives,
how the processes run, and the three paths that move data through them. Each area page
holds the module detail: [API](api.md), [Simulation](simulation.md),
[Similarity](similarity.md), [Pipeline: ingest](pipeline-ingest.md),
[Pipeline: nightly builds](pipeline-batch.md), [Betting](betting.md),
[Database](database.md), [Frontend](frontend.md), [Scripts](scripts.md) and
[Tests](tests.md). The [loop cheat sheet](sim-loop-cheat-sheet.md) lists every draw and
its weights.

## The system on one page

```
  MLB Stats API (schedule, feed, box)      BettingPros (odds)      Baseball Savant
        |                 |                      |                        |
        | nightly         | game day             |                        |
        v                 v                      v                        v
  +-----------+     +-----------+          +-----------------------------------+
  |   jobs    |     |   live    |--------->| Postgres  raw.*  (facts)          |
  | container |---->|  service  |  writes  |           sim.*  (runs, live)     |
  +-----------+     +-----------+          +-----------------------------------+
        |                 |                          |  read through DuckDB's
        |                 | Redis channel            |  postgres extension
        |                 v                          v
        |           +-----------+          +-----------------------------------+
        |           |   Redis   |          | DuckDB  /data/baseball_sim.duckdb |
        |           +-----------+          |   derived.* profiles, sim.* pools |
        |                 ^                +-----------------------------------+
        |                 |                          | nightly bundle build
        |                 |                          v
        |                 |                +-----------------------------------+
        |                 |                | engine-artifact bundle (versions) |
        |                 |                | /data/play_pool/engine_artifacts  |
        |                 |                +-----------------------------------+
        |                 |                          | loaded at boot, shared memory
        |                 |                          v
        |           +--------------------------------------------------------+
        |           | app: FastAPI (uvicorn)  +  forkserver worker pool      |
        |           |   routes, run jobs, engines, calibration, replay file  |
        |           +--------------------------------------------------------+
        |                                     ^
        |                                     | /api, /ws, /auth
        |                              +-------------+
        |                              |    nginx    |  <- the browser (React app)
        |                              +-------------+
        +-- scheduler (Ofelia) runs the nightly jobs inside jobs, db and app
```

The browser talks only to nginx. nginx serves the built React app and forwards `/api/`,
`/ws/`, `/auth/` and the health routes to the app. The app reads Postgres for facts and
runs, the DuckDB file for profiles, and the bundle for every simulator draw.

## The architecture rule

Three owner rulings govern the simulator. The code follows each one today.

1. **Every decision is a similarity-weighted draw from a hard-filtered pool (owner
   ruling 2026-08-10).** A hard filter is an exact match that removes rows, such as the
   count or the base-out state. The draw then weights every remaining row by similarity
   and takes one. No hand-tuned formula decides a play. Every similarity draw lives in
   [`simulation/sampler/`](../../simulation/sampler/) (one mixin per draw). The
   intentional walk is the one draw with no similarity weight: it rolls once against the
   real rate of its cell (`_should_issue_ibb` in
   [`simulation/loop/manager.py`](../../simulation/loop/manager.py)).
2. **The drawn row IS the play (owner ruling 2026-08-29).** No code adjusts a play after
   the draw. Every factor is a draw weight, or it is off. The batted-ball draw carries the
   real row's whole base-out transition onto the live runners
   (`_resolve_in_play_transition` in
   [`simulation/loop/in_play.py`](../../simulation/loop/in_play.py)). The fence stage is a
   filter BEFORE the draw, not a correction after it
   ([`simulation/sampler/fence.py`](../../simulation/sampler/fence.py)). Only the rules
   of baseball act outside a draw: forced advances, the ghost runner in extra innings,
   the dropped third strike and the scoring of a play.
3. **Every change lands ON at its best-known default (owner ruling 2026-09-16).** No change
   waits on its own accuracy run. The weights are fitted together, once, by one designed
   experiment ([`scripts/designed_experiment.py`](../../scripts/designed_experiment.py))
   when the owner calls the model final. Until that fit, every similarity power is 1 (the
   `SIM_ACTOR_POWER_*` and pitch-draw powers in `docker-compose.yml`). Three factors stay
   off in production because no default exists for them yet: the catcher receiving ratio
   (`SIM_CATCHER_RECEIVING=0`, SIM-526), the pitch draw's pitch-count width
   (`SIM_FATIGUE_PC_SIGMA=0`) and the fielding draw's drawn-pitch width
   (`SIM_BB_PITCH_SIGMA=0`).

The similarity engines never run inside a worker. The nightly bundle build runs eight of
them and writes their scores as matrices: the pitcher-by-pitcher matrix and the actor
score matrices (batter, fielder per position, catcher, catcher throwing, steal runner,
advancement runner, pitcher against the run, manager usage). A worker looks a score up
at draw time and raises it to its power. The situation and batted-ball engines feed no
draw; the draws use their own hard cells and Gaussian kernels instead.

## Services

`docker-compose.yml` defines ten services. Every port except nginx's binds to
`127.0.0.1` only, so another machine on the network reaches only the site.

| Service | Runs | Port | Reads and writes |
|---|---|---|---|
| `db` | PostgreSQL 15 | 127.0.0.1:5432 | The `postgres_data` volume. Creates the app roles on a fresh volume (`db/roles/`). The scheduler runs the Postgres backup here as the `postgres` user. |
| `redis` | Redis 7, password required, 256 MB, least-recently-used eviction | 127.0.0.1:6379 | The sim cache, the similarity cache, the live channels. |
| `app` | `uvicorn api.main:app --reload` (the image's `dev` target) | 127.0.0.1:8000 | Postgres as `baseball_app`, and the read-only pool as `baseball_ro`; the `duckdb_data` volume at `/data`; Redis; `/backups` and `/offsite` read-only. Mounts `api/`, `pipeline/`, `similarity/`, `simulation/`, `db/` from the checkout. |
| `live` | `python -m pipeline.live.run_live` | none (8001 inside) | Postgres as `baseball_app`; publishes on Redis. |
| `jobs` | `sleep infinity`: a container the scheduler execs the nightly jobs into, as `appuser` | none | Postgres as `baseball_app`; `/data` read-write; `/backups` and `/offsite` read-write. |
| `nginx` | The built React app and the reverse proxy (`frontend/Dockerfile`, `deploy/nginx/nginx.conf`) | 80, every interface | Forwards to the app. |
| `scheduler` | Ofelia, the cron (`deploy/ofelia/config.ini`) | none | The Docker socket, to exec jobs into `jobs`, `db` and `app`. |
| `prometheus` | Prometheus 2.53 | 127.0.0.1:9090 | Scrapes the app's `/metrics` with an API key (`deploy/monitoring/`). |
| `grafana` | Grafana 11.1 | 127.0.0.1:3000 | The dashboards over Prometheus. |
| `migrate` | `alembic upgrade head` (profile `tools`, so only `make migrate` starts it) | none | Postgres as the owner role `baseball_user`, the only service that may change the schema. |

The app, live and jobs services blank the owner's password, the read-only password and
the Grafana password in their environment. `scripts/` is baked into the image, not
mounted: a script change reaches a container only after an image build.

## Data stores

| Store | Where | Holds | Written by |
|---|---|---|---|
| Postgres `raw.*` | the `db` service | The facts: games, pitches, play events, lineups, the official box, the bullpens, players, venues, the Savant boards, every book's odds. Alembic head **0030**. | The historical loader (nightly finals), the live service, the odds loaders. |
| Postgres `sim.*` | the `db` service | The run jobs (`sim.sim_runs`) and the live game state (`sim.lineup_state`). | The app (runs), the live service (state). |
| DuckDB file | `/data/baseball_sim.duckdb` | `derived.*` profiles (pitchers, batters, fielders, runners, catchers, managers, park factors, run expectancy, league averages) and `sim.*` play pools (pitch, outcome, steal, advancement, intentional-walk rates). DuckDB schema **v32**. | The nightly profile build only. |
| Engine-artifact bundle | `/data/play_pool/engine_artifacts` | Everything a worker draws from: the per-hand pitch and batted-ball pools, the steal, advancement and pitching-change pools, the park geometry, the pitcher-by-pitcher matrix, the actor score matrices, the reference tables (intentional-walk rates, venue run factors), the catcher receiving document. | The nightly bundle build. |
| Replay file | `/data/replay.duckdb` | One recorded game per run: the play stream, the state after each pitch, the linescore and the pitcher decisions. It keeps 5 runs per game. | The app only; it holds the file open writable. |
| Calibration | `/data/calibration.json`, `/data/prop_validation.json` | The engines' fitted calibration and the win-probability reliability curve. | `make calibrate`, `make validate-props`. The app loads it at boot. |
| Job marks | `/data/ingest_marks/` | `<job>.running` while a job runs, `<job>.last` after it ends. | `scripts/with_mark.sh`. |
| Redis | the `redis` service | The sim cache (typed JSON, keys `sim443:`; a summary lives 60 s, a seeded prop set 15 minutes, a pool query 5 minutes), the similarity cache (24 hours), the live channels `live:game:{game_pk}` and the `live:heartbeat` key. | The app and the live service. |
| Backups | `BACKUP_DIR` at `/backups`, `OFFSITE_BACKUP_DIR` at `/offsite` | Three sets per store, and their off-site copy. | The nightly backup jobs. |

**The DuckDB file has one writer at a time.** DuckDB refuses every other open, read-only
ones included, while a process writes. So the app holds no handle on the file. At boot it
builds the engines, reads the park factors into memory and closes the file
([`simulation/sim_kwargs.py`](../../simulation/sim_kwargs.py)). A lookup re-reads the park
factors when the file's modification time changes, at most once a minute. The bundle
build opens the file read-only.

**The bundle is a link to a version.** `engine_artifacts` is a symbolic link to one folder
under `engine_artifacts.versions/<build id>/`
([`pipeline/batch/bundle_versions.py`](../../pipeline/batch/bundle_versions.py)). A build
copies the current version, builds into the copy and then moves the link in one atomic
step. A reader sees the old version or the new one, never a mix. A failed build moves
nothing. Pruning keeps the newest three versions plus every version a running app marks
in use.

See [Database](database.md) for every table.

## The process model

**The app process.** One uvicorn process serves every route. Its lifespan
([`api/main.py`](../../api/main.py), the steps in [`api/startup.py`](../../api/startup.py))
runs in this order at boot:

1. Check the environment, open the Postgres pool and the run registry, and mark every
   run a previous process left unfinished as `failed('restart')`.
2. Open Redis and attach the league reader (the schedule and the per-game feed).
3. Build the eleven similarity engines and apply `/data/calibration.json`. A failure to
   build the pitcher engine stops the boot.
4. Open the assistant's client and the read-only pool, start the live bridge, open the
   replay file, and read the park factors.
5. Pin the bundle version, load the bundle, publish its large arrays to shared memory,
   and build the one long-lived `BatchRunner`.
6. Warm the workers in the background, and start the bundle watch.

**The boot waits for a DuckDB writer.** When the nightly profile build holds the file,
each engine build retries every 15 seconds for up to `SIM_BOOT_LOCK_WAIT_S` (default
1,800 seconds), then fails (`retry_on_duckdb_lock` in
[`api/state.py`](../../api/state.py)).

**The worker pool.** [`simulation/batch_runner.py`](../../simulation/batch_runner.py)
runs the games on a `ProcessPoolExecutor`. The pool starts each worker with
`forkserver` (`SIM_MP_START_METHOD`), from a lean server process, so no worker inherits
the app's engines. The worker count is `SIM_RUNNER_WORKERS`, or `min(CPU count − 1, 10)`
when unset. A worker attaches the parent's shared-memory arrays once. It then builds one
`FullPoolSampler` over the bundle and caches it for every later game
([`simulation/production_factory.py`](../../simulation/production_factory.py)). A
missing bundle fails the worker's first game: there is no fallback simulator. A worker
receives only a small picklable `GameSpec` (the game's kwargs and a dotted factory
name), never a live object.

**The pinned version.** Before the pool starts, the app resolves the bundle link and sets
`BASEBALL_ENGINE_ARTIFACT_DIR` to that version folder
([`api/bundle_watch.py`](../../api/bundle_watch.py)). The forkserver's workers inherit
the variable, so a worker that respawns after a rebuild still reads the version that
matches the parent's shared arrays. The app also marks the version in use, so pruning
keeps it.

**The restart on a new bundle.** With `SIM_RESTART_ON_NEW_BUNDLE=1` (production), a task
looks at the link every 60 seconds. When the link points to a new version, the task waits
for the running run jobs (at most 600 seconds), then for any DuckDB writer, and then sends
SIGTERM to PID 1. Docker's `restart: unless-stopped` starts the container again, and the
boot loads the new profiles and the new bundle. A restart, not a swap in place, is an
owner ruling (the nightly rebuild that reaches users, SIM-563).

## The path of one simulation request

The game page's "Load projections" queues a run job. The path, from the browser to the
answer:

1. The browser calls `POST /api/games/{game_pk}/simulate`
   ([`frontend/src/api/games.ts`](../../frontend/src/api/games.ts)). nginx forwards it to
   the app, and `require_auth` checks the login.
2. The route resolves the game into a `GameState`: the lineups, each side's starter from
   the official box (for a game with no box yet, the lineup's pitcher row), the defense,
   the pen and the hands
   ([`simulation/lineup_resolver.py`](../../simulation/lineup_resolver.py)). A game with
   no usable lineup answers 503 with Retry-After.
3. The route builds the kwargs with the one builder
   ([`simulation/sim_kwargs.py`](../../simulation/sim_kwargs.py)): the venue's park
   factor, the managers' profiles and the point-in-time cutoff (the day before the game,
   so the sampler never copies the game's own plays). The kwargs and the production
   factory's dotted name form a `GameSpec`.
4. The run registry ([`api/sim_jobs.py`](../../api/sim_jobs.py)) inserts a
   `sim.sim_runs` row as `queued`, or returns the queued or running row with the same
   spec, seed and N. `SIM_JOB_MAX_CONCURRENT` (default 1) jobs run at once.
5. The job calls `BatchRunner.run_job` in a thread. The runner derives one seed per game
   from the base seed and sends the games to the workers in chunks, writing the progress
   after each chunk.
6. Each worker runs `_run_one`: the production factory returns the cached sampler, and
   `simulate_game` plays one game pitch by pitch
   ([`simulation/sim_loop.py`](../../simulation/sim_loop.py)). Every pitch calls the
   sampler's draws. The worker returns a `GameSimResult` with the box score and the
   inning grid.
7. The runner aggregates the N results into a `GameSimSummary` (the scores, the win
   shares, the inning grids;
   [`simulation/results.py`](../../simulation/results.py)) and a
   `PropDistributionSet`, every player's distribution for every prop
   ([`simulation/prop_distributions.py`](../../simulation/prop_distributions.py)).
8. The job stores both on the run row, records one representative game on a worker into
   the replay file, and marks the row `done`.
9. The browser polls `GET /simulate/runs/{run_id}`. The panels then read the stored run:
   `/boxscore?run_id=` for the projections, `/props/{player_id}/{prop}?run_id=` for one
   distribution, and `/card` and `/plays` for the recorded game.

The betting card follows a second route, `GET /api/betting/games/{game_pk}/edges`
([`api/routes/betting.py`](../../api/routes/betting.py)). It runs a batch through the
same seam (`BatchRunner.run`, cached by spec, seed and N), maps the win share through the
calibration's reliability curve
([`simulation/win_probability.py`](../../simulation/win_probability.py)), and prices
every game market from the inning grids
([`simulation/game_market_distributions.py`](../../simulation/game_market_distributions.py)).
It then compares each price with the book's stored line
([`betting/clv_engine.py`](../../betting/clv_engine.py)).

`GET /simulate` and `POST /what-if` use the same runner. The what-if queues two runs with
one seed from a real plate appearance: the game as it stood, and the game with the
user's changes ([`simulation/whatif.py`](../../simulation/whatif.py),
[`simulation/start_state.py`](../../simulation/start_state.py)).

## The live game-day path

1. The `live` service polls the league schedule every 30 seconds and refreshes the
   `live:heartbeat` key ([`pipeline/live/run_live.py`](../../pipeline/live/run_live.py),
   [`pipeline/live/live_ingestion_pipeline.py`](../../pipeline/live/live_ingestion_pipeline.py)).
2. For a game in Preview, the published-lineup writer stores the posted lineups as
   `raw.game_lineups` rows with `source = 'published'`, so the game can be simulated
   before first pitch ([`pipeline/live/lineup_writer.py`](../../pipeline/live/lineup_writer.py)).
3. For a live game, the league's WebSocket is only a change signal. On each message the
   service re-fetches the game's feed, rebuilds the game state, and writes `raw.games`
   and `sim.lineup_state`.
4. The service stores the pre-game odds of the configured provider (`ODDS_PROVIDER`) in
   `raw.game_odds` and `raw.prop_odds`, and promotes each game's closing rows at first
   pitch.
5. The service publishes each browser-bound message on the Redis channel
   `live:game:{game_pk}`. The app's bridge subscribes while a browser watches the game
   and forwards each message to that browser's socket on `/ws/games/{game_pk}`
   ([`pipeline/live/broadcast.py`](../../pipeline/live/broadcast.py)).

The live path runs no simulation. A user starts every run.

The slate reads the league itself. The app's league reader fetches the schedule of a date
and one game's feed, and caches both
([`api/league_feed.py`](../../api/league_feed.py),
[`api/routes/games_slate.py`](../../api/routes/games_slate.py)). The database adds the
lineup flag and each game's newest run. On a feed error the slate serves the stored
listing.

## The nightly path

The scheduler runs these jobs (UTC; `deploy/ofelia/config.ini`). Each data job runs
inside `scripts/with_mark.sh` and the crash wrapper `scripts/with_retry.sh`.

| Time (UTC) | Job | Container | What it does |
|---|---|---|---|
| 07:00 daily | `nightly-finals` | jobs | Loads every game that became Final in the last three days into Postgres (`scripts/nightly_finals.sh`). |
| 08:00 Sunday | `weekly-refresh` | jobs | Sweeps the current season for any final game the nightly window missed. |
| 08:00 daily | `nightly-rebuild` | jobs | Waits for the finals load, rebuilds the current season's profiles and pools into DuckDB, then builds and publishes a new bundle version (`scripts/nightly_rebuild.sh`). |
| 10:00 daily | `nightly-backup-postgres` | db | Dumps Postgres into `/backups` (`db/backup/pg_backup.sh`). |
| 10:00 daily | `nightly-backup-data` | jobs | Copies the DuckDB file, the small `/data` files and the bundle version into `/backups` (`pipeline.backup.data_stores`). It waits while a writer's mark is present. |
| 10:30 daily | `nightly-backup-offsite` | jobs | Copies the newest complete sets into the OneDrive folder (`pipeline.backup.offsite`). |
| 10:30 daily | `nightly-closing-lines` | app | Loads the closing lines of the last two days' games, as a fallback to the live promotion (`scripts/nightly_closing_lines.sh`). |

The rebuild is the step that reaches users. The profile build
([`pipeline/batch/player_profile_computor.py`](../../pipeline/batch/player_profile_computor.py))
reads Postgres `raw.*` through DuckDB's postgres extension and writes `derived.*` and
`sim.*`. The bundle build
([`pipeline/batch/engine_artifacts.py`](../../pipeline/batch/engine_artifacts.py)
`--what all`) reads that file, runs the engines for the score matrices and publishes the
new version. The app's bundle watch then restarts the app, which loads both. See
[Pipeline: nightly builds](pipeline-batch.md) and the operator manual
([`WORKFLOW.md`](../../WORKFLOW.md)).

## Security at a glance

- Only nginx's port 80 listens on the network; every other port binds to the loopback
  address.
- Three Postgres roles: the owner `baseball_user` (the `migrate` service and the admin
  scripts), `baseball_app` (rows in `raw` and `sim`, no schema change) and `baseball_ro` (SELECT on `raw`, for the SQL console, the assistant and the Data Lab's read routes). See
  [`db/roles/README.md`](../../db/roles/README.md).
- Every API route and the WebSocket require a login: the session cookie, `X-API-Key` or a
  Bearer key ([`api/auth.py`](../../api/auth.py)). `AUTH_DISABLED=1` is the one opt-out.
- Every password comes from `.env` with no default (`scripts/gen_secrets.py`).
