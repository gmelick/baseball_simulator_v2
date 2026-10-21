# Database

The platform keeps its data in three stores. Postgres holds what comes in from outside —
games, pitches, lineups, box scores, Savant leaderboards, odds — and the record of every
simulation run. A DuckDB analytics file holds what the nightly builds compute from
Postgres: the player profiles and the play pools the simulator draws from. A second,
small DuckDB file holds the replay of simulated games that the game page shows. This page
lists every table, says who writes it and who reads it, and then covers the migrations,
the database roles and the backup scripts.

Two more stores sit outside the databases. The engine-artifact bundle is a folder of
array files built from the analytics file; the workers load it (see
[the nightly builds](pipeline-batch.md)). Redis caches simulation results (see
[the API](api.md)).

The current schema versions are Alembic head **0030** for Postgres and
DuckDB schema **v32** for the analytics file.

## The stores at a glance

| Store | Where | Schemas | Written by | Read by |
|---|---|---|---|---|
| Postgres | the `db` compose service, database `baseball_sim` | `raw`, `sim` | the ETL loaders, the live service, the nightly jobs, the app (run records) | the app, the nightly builds (through DuckDB's Postgres attach), the scripts |
| DuckDB analytics file | `/data/baseball_sim.duckdb` (`BASEBALL_DUCKDB_PATH`) | `derived`, `sim` | the profile computor and the pool rebuild scripts | the similarity engines at app boot, the bundle builder, the park-factor lookup, the scripts |
| DuckDB replay file | `/data/replay.duckdb` (`REPLAY_DUCKDB_PATH`) | `sim` | the app only | the app only |

## How it fits together

1. The historical loader (`pipeline/etl/etl_historical_loader.py`) fetches each game's
   MLB Stats API feed. It writes the reference rows, then `raw.pitches` and
   `raw.play_events`. The box-score parser (`pipeline/etl/boxscore_ingest.py`) writes
   `raw.game_player_stats` and `raw.game_bullpen` for the same game.
2. The Savant loaders (`pipeline/etl/savant_loader.py`,
   `pipeline/etl/savant_pitch_tracking_loader.py`) and the sprint-speed loader write the
   `raw.savant_*` tables and `raw.sprint_speed`.
3. The live service (`pipeline/live/`) writes the schedule fields on `raw.games`, the
   posted lineups, the odds rows and `sim.lineup_state`.
4. The nightly profile computor (`pipeline/batch/player_profile_computor.py`) opens the
   analytics file, attaches Postgres read-only as `pg`, applies
   `db/schemas/02_duckdb_schema.sql`, and rebuilds the `derived.*` profiles and the
   `sim.*` play pools.
5. The bundle builder (`pipeline/batch/engine_artifacts.py`) reads the analytics file
   into the bundle the simulator loads.
6. The app reads Postgres as `baseball_app`, builds the similarity engines from the
   analytics file at boot, writes each simulation run to `sim.sim_runs`, and writes one
   replayed game per run to the replay file (`db/sim_store.py`).
7. The Data Lab (the SQL console and its read routes) reads `raw.*` as `baseball_ro`.
8. The nightly backups copy all three stores (see [Backup and restore](#backup-and-restore)).

## Postgres

Alembic (`db/migrations/versions/`) is the source of truth for the Postgres schema.
`make migrate` applies it through the `migrate` compose service, the only service that
logs in as the cluster owner. The reference file `db/schemas/01_postgres_schema.sql` is
documentation only: no code applies it, and it lags the migrations (see its module
section below).

### Reference tables

| Table | Holds | Written by | Read by |
|---|---|---|---|
| `raw.venues` | One row per ballpark and season: name, city, surface, roof, elevation, the fence distances and wall heights. | the historical loader; `pipeline/etl/venue_backfill_job.py` | the loader, the live service, the slate route, the Data Lab |
| `raw.teams` | One row per team and season: league, division, home venue. | the historical loader (`pipeline/etl/historical/reference_rows.py`) | the loader, the live service, the slate route |
| `raw.players` | One row per player: name, bat and throw hand, primary position. | the historical loader; the live lineup writer (`pipeline/live/lineup_writer.py`) | the lineup resolver, the API's player and prop routes, the Data Lab, the Savant and sprint-speed loaders, the bullpen derivation |
| `raw.managers` | One row per manager and season: the team and the tenure dates (`season_end` NULL while active). | the historical loader | the loader (each pitch carries the two managers' ids) |

### Games, lineups and box scores

| Table | Holds | Written by | Read by |
|---|---|---|---|
| `raw.games` | One row per game: date, type, status, teams, venue, final score, the inning-by-inning line (`inning_scores`), weather, and the schedule fields (start time, doubleheader, detailed state, probable pitchers, first pitch). | the historical loader; the live service (`pipeline/live/live_ingestion_pipeline.py`); the venue backfill | nearly every reader: the slate, the game page, the betting routes, the bullpen derivation, the scripts |
| `raw.game_lineups` | Each team's batting order and the position each starter STARTED at, with substitutions (`sequence`, `pinch_role`). `source` is `box` (the final box score), `published` (posted before first pitch) or `projected`. | the historical loader (`pipeline/etl/historical/reference_rows.py`); the live lineup writer; the starting-position backfill (`scripts/probes/backfill_start_positions.py`, through `boxscore_ingest.persist_starting_positions`) | the lineup resolver (`simulation/lineup_resolver.py`), the game routes, the manager profile, the scripts |
| `raw.game_player_stats` | The official per-player box score: one row per player who APPEARED (no row means the player did not play and a book voids the bet). Batting and pitching lines, `p_started`. No foreign key on `player_id`, on purpose. | the box-score parser; `scripts/load_official_boxscores.py` | the prop grading (`simulation/prop_validation.py`, `scripts/accuracy/`), the lineup resolver (each side's starter), the bullpen derivation |
| `raw.game_bullpen` | The arms the MLB box lists for a game per side: `started`, `pitched` or `bullpen`. The simulator's pen. | the box-score parser; `scripts/load_official_boxscores.py --bullpen-only` | `pipeline/bullpen_usage.py`, the lineup resolver, the game routes |
| `raw.game_bullpen_availability` | Who was AVAILABLE per game (active roster and injured list), with rest fields. | `pipeline/live/bullpen_availability_ingest.py` | the manager profile (`pipeline/batch/profiles/manager.py`) |

### Pitch and event data

| Table | Holds | Written by | Read by |
|---|---|---|---|
| `raw.pitches` | One row per pitch, parsed from the MLB Stats API game feed: the count, the pitch's physics, the batted ball, the fielders, the runners, the event, the managers. A row is never changed after it is written; a corrective reload deletes and rewrites the game. An insert trigger sets `data_quality_flag` on physically implausible values. | the historical loader (`pipeline/etl/historical/writes.py`) | the profile computor and the pool builders (through `pg.raw.pitches`), the pool label check (`pipeline/batch/pool_chain.py`), the Data Lab |
| `raw.play_events` | The events that are not pitches: pickoff throws (errant ones included), step-offs, balks, and intentional walks signalled without a pitch. | the historical loader | the profile computor (pickoff rates, runner events), the steal-pool rebuild, the prop ground truth |
| `raw.sprint_speed` | Savant sprint speed per player-season. | `pipeline/etl/etl_sprint_speed_loader.py` | the baserunner and fielder profiles, the pool builder |

### Savant leaderboards

Baseball Savant publishes season leaderboards that the MLB feed lacks. The landing tables
are one-to-one copies of the boards. `pipeline/etl/savant_boards.py` (`BOARDS`) names each
board's table and columns, and `pipeline/etl/savant_loader.py` writes them all through one
insert.

| Table | Holds | Read by |
|---|---|---|
| `raw.savant_bat_tracking`, `raw.savant_swing_path`, `raw.savant_batting_stance` | Bat speed, swing length, the swing path and the stance. A switch hitter's two sides are kept apart by the pitcher's hand (`split`) or by `bat_side`. Each row carries `asof_date`, the date its data runs through. | the batter profile |
| `raw.savant_pitch_tracking` | The same swing measurements per PITCH (keyed by game, at-bat and pitch number), plus the pitcher's arm angle, so a profile can aggregate to any cutoff date. Written by `pipeline/etl/savant_pitch_tracking_loader.py`. | the batter profile. The arm angle is stored and read by nothing (the arm-angle decision, SIM-533). |
| `raw.savant_arm_strength` | Fielders' throwing arm strength by position. | the fielder profile |
| `raw.savant_outs_above_average` | Outs above average, pulled once per position; the key carries the position pulled. | the fielder profile |
| `raw.savant_outfield_jump` | The outfielder's reaction, burst, route and jump in feet. | the fielder profile |
| `raw.savant_baserunning` | Run values on the bases: a runner's extra-base chances and attempts, and the run value of an outfielder's arm. | the baserunner profile |
| `raw.savant_basestealing`, `raw.savant_pitcher_running_game` | The runner's lead and jump, and the lead and jump a pitcher allows. | the baserunner-steal and pitcher-steal profiles |
| `raw.savant_poptime`, `raw.savant_catcher_throwing` | Catcher pop time, exchange time and throwing results. | the catcher profile |
| `raw.savant_first_base_receiving` | First-base receiving and scoops. | no reader today |

### Odds

One odds row holds ONE book's prices and names the book (`book = 'bp:<id>'`). A reader
keeps only those rows (`pipeline.odds_provider.STORED_BOOK_FILTER_SQL`), because an old
`consensus` row can mix two books. A row is new only when its price set is new: the
writer computes an odds hash and skips a duplicate.

| Table | Holds | Written by | Read by |
|---|---|---|---|
| `raw.game_odds` | Game-market prices per snapshot: the moneyline (with `draw_ml` for a three-way segment moneyline), the run line, the total, and the twelve segment and team markets (`market_type`, fifteen values). `line_type` is `opening`, `current`, `closing` or `bet_placement`. `book_line_at` is the vendor's stamp; `last_seen_at` the last time the book still posted the price. | the live service's odds writers (`pipeline/live/ingestion/odds_rows.py`, `closing_rows.py`), also used by `scripts/load_historical_odds.py` | the betting routes, `betting/line_movement.py`, the accuracy comparison, the slate card |
| `raw.prop_odds` | Player-prop prices: over / under at a line, per player and `prop_stat` (fifteen markets). | the same writers | the accuracy comparison, the opening-line job, the probes |
| `raw.game_odds_archive`, `raw.prop_odds_archive` | The retired `consensus` rows, copied out of the live tables season by season. | `scripts/probes/retire_consensus_odds.py` | two odds probes |

### ETL bookkeeping

| Table | Holds | Written by | Read by |
|---|---|---|---|
| `raw.etl_data_freshness` | The last game loaded per pitcher and batter. | the historical loader | the loader, the Data Health route |
| `raw.etl_errors` | One row per pitch the loader skipped on a hard error; unique per natural key, so a rerun adds no duplicate. | the historical loader | the loader's reprocess path |
| `raw.etl_game_ingest` | The terminal outcome per game (`loaded`, `empty`, `failed`), so a game with no pitches is not fetched forever. A reload ignores it on purpose. | the historical loader | the loader; `scripts/reload_games.py` |
| `raw.pipeline_run_log` | A run log of two jobs. | `pipeline/etl/opening_line_job.py`, `pipeline/etl/venue_backfill_job.py` | the same two jobs |

### The `sim` schema

| Object | Holds | Written by | Read by |
|---|---|---|---|
| `sim.sim_runs` | One row per simulation run. A run job moves `queued → running → done / failed / cancelled` and stores its summary, prop distributions, inning grids and the id of its replayed game (`replay_run_id`). `kind` is `pregame`, `whatif_base` or `whatif_change`; every "latest run" read takes `pregame` only. A partial unique index allows one active run per spec. | the run-job registry (`api/sim_jobs.py`); `db.sim_store.store_sim_run` | the slate card, the game page, `api/routes/games_runs.py` |
| `sim.lineup_state` | The live state of an in-progress game as JSON (`game_state`), one live row per game. | the live service (`_upsert_lineup_state`) | `GET /api/games/{pk}/live` through `db.sim_store.load_live_game_state` |
| `sim.live_games` (view) | Today's live games joined to their live row. | — | no code reads it |
| `sim.purge_expired_sessions()` (function) | Deletes expired, non-live `sim.lineup_state` rows. | — | no code calls it |

### Triggers

- `update_updated_at_column()` sets `updated_at` on every update of `raw.venues`,
  `raw.teams`, `raw.players`, `raw.managers`, `raw.games`, `raw.game_lineups` and
  `sim.lineup_state`.
- `raw.flag_pitch_quality()` runs before each insert into `raw.pitches`. It sets
  `data_quality_flag` when the release speed lies outside 50 to 110 mph, the exit
  velocity is above 125 mph, or the induced vertical break lies outside ±25 inches. The
  pool builders leave flagged rows out. The ETL validator in
  `pipeline/etl/historical/rows.py` holds the same band; change both together.

## DuckDB: the analytics file

The profile computor creates the schema on connect: it runs
`db/schemas/02_duckdb_schema.sql`, where every statement is `CREATE ... IF NOT EXISTS`.
A new, empty file therefore gets the current tables. An existing file gets a schema change
only from its numbered migration under `db/migrations/duckdb/`, which a run book or a
rebuild script applies. The nightly job applies no DuckDB migration. A builder that needs a
migrated table checks for it first and refuses before it deletes anything (for example
`_require_steal_pool_0031` in `pipeline/batch/profiles/pool_meta.py`).

The computor reads Postgres through DuckDB's Postgres extension:
`ATTACH '<dsn>' AS pg (TYPE postgres, READ_ONLY)`, then `pg.raw.pitches`.

### Profiles (`derived`)

Each profile table holds one row per player (or manager, or venue) per season. The eight
actor profile tables carry `asof_date`, the date a row's data runs through (the
point-in-time profiles, SIM-534 and SIM-537). Every engine refuses a table stamped at two
dates, so a partial rebuild must include the season in progress (the one-cutoff rule,
SIM-551).

| Table | Holds | Built by | Read by |
|---|---|---|---|
| `derived.pitcher_season_metrics` | The pitcher profile, with the arsenal as a Gaussian mixture model (a sum of bell curves over eight pitch features) in `gmm_model`. | `pipeline/batch/profiles/pitcher.py`, `gmm.py` | the pitcher engine and its calibration |
| `derived.pitcher_gmm_components` | One row per mixture component, for SQL reads. | `gmm.py` | the computor |
| `derived.batter_season_metrics` | The batter profile: plate discipline, batted-ball shape, platoon splits, the Savant swing measurements. | `profiles/batter.py` | the batter engine and its calibration |
| `derived.fielder_season_metrics` | One row per player, position and season: range, arm, errors, double plays, Savant outs above average and jump, sprint speed. | `profiles/fielder.py` | the fielder engine; `scripts/probes/outfield_arm_recompute.py` writes it too |
| `derived.baserunner_season_metrics` | Extra-base advancement and speed. | `profiles/baserunner.py` | the advancement-runner engine |
| `derived.baserunner_steal_metrics` | Steal attempts and success, with the Savant lead and jump. | `profiles/baserunner.py` | the steal-runner engine |
| `derived.pitcher_steal_metrics` | The steal results a pitcher allows, his pickoff rates, the lead he allows. | `profiles/baserunner.py` | the pitcher-steal engine |
| `derived.catcher_season_metrics` | Framing, blocking, throwing and the dropped-third-strike columns. | `profiles/catcher.py` | the catcher engine |
| `derived.manager_season_metrics` | The manager's usage, aggression and platoon tendencies. | `profiles/manager.py` | the manager engine; `simulation/sim_kwargs.py` |
| `derived.league_averages` | One JSON profile per entity type and season: the target a thin profile shrinks toward. Created by its builder, not by the DDL file. | `profiles/league_averages.py` | every actor engine; `simulation/sim_kwargs.py` (the manager steal weight) |
| `derived.park_factors` | Per venue, season and factor type, with left and right splits and a regressed factor. | `profiles/park.py` | the park-factor lookup (`simulation/sim_kwargs.py`), the bundle's reference file, the situation engine, the scripts |
| `derived.at_bat_situations` | One row per plate appearance: the game state the batter came up to. | `profiles/pools.py` | the situation engine, the change-pool builder, the leverage builder |
| `derived.run_expectancy_matrix` | The expected runs of each of the 24 base-out states. | `profiles/leverage.py` | the computor's run values; `simulation/run_resolution.py` has a row loader for it |
| `derived.outfield_play_detail`, `derived.infield_play_detail`, `derived.dp_play_detail` | Declared per-play fielding detail. No code writes them; a full rebuild clears them. | — | — |

### Play pools (`sim`)

The play pools are filtered copies of real plays that the simulator draws from. The
tables hold every loaded season. The bundle export keeps the pool window: the last three
completed seasons plus the current one (`RECENCY_FLOOR_SEASONS = 4` in
`pipeline/batch/artifacts/common.py`).

| Table | Holds | Built by | Read by |
|---|---|---|---|
| `sim.pitch_pool` | One row per clean pitch, with the pitch's features, its class (`outcome_type`), the count, the catcher, the got-away facts and the draw's conditioning columns. | `profiles/pools.py`; `scripts/rebuild_pitch_pool.py` | the bundle builder (`pipeline/batch/artifacts/pitch_pool.py`), the pool label check |
| `sim.outcome_pool` | The balls in play, with the batted ball, the venue, the batting side, the fielders on the play and the whole base-out transition the play made. | `profiles/pools.py` | the bundle's batted-ball pool, the park geometry builder, the batted-ball engine |
| `sim.steal_opportunity_pool` | One row per pitch where a steal was possible, attempted or not, plus the pickoff outcomes and their own rows (`is_pickoff_row`); `pitch_class` is the class of the pitch the row rode. | `profiles/pools.py`; `scripts/rebuild_steal_pool.py` | the bundle's steal pool |
| `sim.advancement_opportunity_pool` | One row per discretionary runner decision (five scenarios), attempted or not. Built from the outcome pool. | `profiles/pools.py` | the bundle's advancement pool, the fielder engine |
| `sim.ibb_rates` | The measured intentional-walk rate per situation cell. | `profiles/pools.py` | the bundle's reference file (the loop reads it from there) |
| `sim.stolen_base_pool` | Every steal attempt. | `profiles/pools.py` | no draw reads it |
| `sim.pool_build_metadata` | One row per pool and season: the last build, its source watermark and its builder version. A season is rebuilt only when its source moved or the builder version changed. | `profiles/pool_meta.py` | the computor, the two rebuild scripts |

`migration_history` (no schema prefix) records each numbered migration applied to the
file.

## DuckDB: the replay file

The game page shows one simulated game pitch by pitch. Those rows live in their own file,
`/data/replay.duckdb`, so the app never holds a write lock on the analytics file. The app
opens it writable at boot when `REPLAY_PERSISTENCE_ENABLED` is on (compose sets it), and
`db.sim_store.ensure_replay_schema` applies migrations 0008, 0009, 0010 and 0032 to it.
No other process can open the file while the app runs, read-only included.

| Table | Holds |
|---|---|
| `sim.play_stream` | One row per pitch of a stored game: the outcome, the event, the batted ball, the runs, and the pitch's inning, half, outs before, batter, pitcher and score after. |
| `sim.state_snapshots` | The field and count state at each pitch, as JSON. |
| `sim.game_cards` | The game's linescore and pitching decisions, as JSON, and its seed. Written last, so a run with a card is complete. |

The file numbers its own runs (`next_replay_run_id`) and keeps the five newest per game
(`REPLAY_RUNS_KEPT`).

## Migrations

### Postgres (Alembic)

The current version is Alembic head **0030**. Each file is
`NNNN_<ticket>_<subject>.py` with `revision = "NNNN"`. `db/migrations/env.py` reads `BASEBALL_DB_DSN`. Run another Alembic
command through the owner's service: `docker compose run --rm migrate alembic current`.

| Rev | What it does |
|---|---|
| 0001 | The first schema: the eight `raw` tables, `sim.lineup_state`, the triggers, the purge function and the live-games view. |
| 0002 | One live `sim.lineup_state` row per game (a unique partial index). |
| 0003 | `raw.etl_data_freshness`, `raw.game_odds`, `raw.prop_odds`, `raw.pipeline_run_log`. |
| 0004 | `raw.prop_odds.prop_stat` with its CHECK and index. |
| 0005 | A situation index on `raw.pitches`. |
| 0006 | `raw.games.venue_id` may be NULL. |
| 0007 | The quality trigger's speed floor drops to 50 mph. |
| 0008 | Drops an unused pitch-type index. |
| 0009 | A clean-rows index on `raw.pitches` (pitcher, season). |
| 0010 | The odds hash on `raw.game_odds`: a duplicate price set is not stored. |
| 0011 | `raw.etl_errors`. |
| 0012 | The odds-hash index becomes a full unique index. |
| 0013 | The odds hash on `raw.prop_odds`. |
| 0014 | `sim.sim_runs`. |
| 0015 | `raw.game_bullpen_availability`. |
| 0016 | The quality trigger's band becomes 50 to 110 mph. |
| 0017 | The ETL hardening: nullable manager ids, `field_assist_6_plus`, the `raw.players.active` drop, a unique key on `raw.etl_errors`, `raw.etl_game_ingest`. |
| 0018 | `raw.play_events`. |
| 0019 | Eight Savant landing tables. |
| 0020 | `raw.savant_pitch_tracking`; `asof_date` on `raw.savant_batting_stance`. |
| 0021 | `asof_date` on the two swing boards. |
| 0022 | `raw.prop_odds.prop_stat` takes fifteen markets. Its downgrade DELETES the eight newer markets' rows. |
| 0023 | `raw.game_player_stats`. |
| 0024 | `raw.game_odds.market_type` takes fifteen markets; `draw_ml`. Its downgrade DELETES the twelve newer markets' rows. |
| 0025 | `raw.game_bullpen`. |
| 0026 | `raw.savant_basestealing`, `raw.savant_pitcher_running_game`. |
| 0027 | `raw.savant_outs_above_average`, `raw.savant_outfield_jump`. |
| 0028 | `book_line_at` on both odds tables, two per-book indexes, the two archive tables. |
| 0029 | The live slate: the schedule columns on `raw.games`, `source` and `published_at` on `raw.game_lineups`, the run-job columns on `sim.sim_runs`, `last_seen_at` on both odds tables. |
| 0030 | The what-if runs: `kind`, `start_at_bat` and `changes` on `sim.sim_runs`. Its downgrade DELETES the what-if runs. |

### DuckDB (numbered SQL)

The current version is DuckDB schema **v32**. Each change ships a numbered file
`db/migrations/duckdb/NNNN_<ticket>_<subject>.sql` AND raises
`db/schemas/duckdb_schema_version.txt` to the same number. Most files record themselves
in `migration_history` with `INSERT OR IGNORE`, and their statements are idempotent
(`IF NOT EXISTS`). `02_duckdb_schema.sql` carries the same change, so a new file
matches a migrated one. An added column goes last in its table, because the pool builders insert by
position.

| No. | What it does |
|---|---|
| 0001 | The first schema and `migration_history`. |
| 0002 | `steal_attempt_rate_against` on the catcher profile. |
| 0003 | The pull-relative spray angle on the outcome pool (its comment on the batting side is corrected by 0014). |
| 0004 | `recency_weight` on the pools; `sim.pool_build_metadata`. |
| 0005, 0006 | Prune and reconcile the pool indexes. |
| 0007 | Left and right park factors; the source row count on the build metadata. |
| 0008, 0009, 0010 | The replay tables: the play stream, the state snapshots, the game cards. |
| 0011 | `derived.at_bat_situations`, `derived.baserunner_steal_metrics`, `derived.pitcher_steal_metrics`. |
| 0012 | The venue and the fielder on the outcome pool. |
| 0013 | The manager's available-reliever usage rate. |
| 0014 | Corrects the batting-side comment of 0003. |
| 0015 | `sim.steal_opportunity_pool`. |
| 0016 | The pitcher's pickoff and step-off rates. |
| 0017 | The pickoff outcome columns on the steal pool. |
| 0018 | The base-out transition columns on the outcome pool; `sim.advancement_opportunity_pool`. |
| 0019 | The batting side on the outcome pool. |
| 0020 | `sim.ibb_rates`. |
| 0021 | The dropped-third-strike columns on the catcher profile. |
| 0022 | The catcher and got-away columns on the pitch pool. |
| 0023 | The draw-conditioning columns on the pitch pool. |
| 0024 | Sprint speed on the fielder profile; the fielders and the putout / assist masks on the outcome pool. |
| 0025 | The batter's physical swing and stance columns. |
| 0026, 0027, 0028 | `asof_date` on the batter, then the pitcher and manager, then the last five profile tables. |
| 0029 | Lead distance on the three runner-side profiles. |
| 0030 | Savant outs above average and the outfield jump on the fielder profile. |
| 0031 | `pitch_class` and `is_pickoff_row` on the steal pool. Apply it only through `scripts/rebuild_steal_pool.py --apply-migration`, which writes the pickoff rows in the same transaction. |
| 0032 | The pitch context on the replay play stream and the seed on the game cards. On the analytics file it is a guarded no-op. |

### The doc guards

Three unit tests hold the prose to the files on disk:

- [`test_docs_alembic_head.py`](../../tests/unit/test_docs_alembic_head.py) reads the
  newest file under `db/migrations/versions/` and fails when a doc's statement of the
  current head lags it.
- [`test_docs_duckdb_version.py`](../../tests/unit/test_docs_duckdb_version.py) does the
  same for the DuckDB version.
- [`test_sim_store.py`](../../tests/unit/test_sim_store.py) holds the version file equal
  to the newest numbered DuckDB migration.

A migration ticket updates the version phrases in the same commit.

## Roles and grants

Three Postgres roles log in. No password has a default: compose builds every DSN from the
secrets in `.env`.

| Role | Used by | May do |
|---|---|---|
| the owner (`POSTGRES_USER`, `baseball_user`) | the `migrate` service; the backup and restore scripts over the socket | everything; owns the schemas and tables |
| `baseball_app` | the app, the live service and the jobs container (`BASEBALL_DB_DSN`) | SELECT, INSERT, UPDATE, DELETE on the rows of `raw` and `sim`; use the sequences. No DDL, no TRUNCATE. |
| `baseball_ro` | the Data Lab: the SQL console and the read routes (analytics, players, data health, schema), through the app's read-only pool (`BASEBALL_DB_RO_DSN`) | SELECT on `raw` only; every transaction is read-only; no access to `sim`. |

`db/roles/setup_roles.sql` holds the grants, including the default privileges, so a table
a later migration creates gets the same grants. A NEW schema needs its own lines there.
[db/roles/README.md](../../db/roles/README.md) is the operator's note.

## Backup and restore

The two Postgres scripts run inside the `db` container as the `postgres` OS user over the
unix socket, so they need no password. The DuckDB file and the bundle are backed up by
`pipeline/backup/data_stores.py` (see [the nightly builds](pipeline-batch.md)). The run
book is the operator's manual, [WORKFLOW.md](../../WORKFLOW.md).

## The modules

### `db/sim_store.py`

[Source](../../db/sim_store.py) · 925 lines · Tests: [test_sim_store.py](../../tests/unit/test_sim_store.py), [test_replay_store.py](../../tests/unit/test_replay_store.py), [test_api_games_plays.py](../../tests/unit/test_api_games_plays.py), [test_api_game_card.py](../../tests/unit/test_api_game_card.py)

The persistence layer of the simulation runs and the replay. Each function does one round
trip on a connection the caller opened: asyncpg for Postgres, duckdb for the replay file.
It holds no domain logic, so a test can run it against a stub or an in-memory DuckDB.
`PLAY_ROW_FIELDS` is the contract the replay writer and the reader share.

| Function / class | What it does | Called from |
|---|---|---|
| `store_sim_run` | Inserts a run's summary into `sim.sim_runs`; returns `run_id`. | `api/routes/games.py` (`_store_history_run`) |
| `load_latest_sim_run` | The newest finished `pregame` run of a game. | `api/routes/games_slate.py` |
| `load_sim_run`, `list_sim_runs` | One run by id; the last N runs of a game. | tests only |
| `load_live_game_state` | The live `sim.lineup_state` row of a game. | `api/routes/games_feed.py` |
| `store_play_stream`, `load_play_stream` | Write and read a stored game's pitches, defaults filled. | `api/routes/games.py` |
| `store_state_snapshots`, `load_state_at`, `load_state_snapshots` | Write the per-pitch states; read one or all. | `api/routes/games.py` |
| `store_game_card`, `load_game_card` | Write and read the linescore and decisions. The card is built at record time, because it needs the play's next state, which the stored rows drop. | `api/routes/games.py` |
| `ensure_replay_schema`, `open_replay_store` | Apply the four replay migrations; open the file writable. | `api/main.py` (the lifespan) |
| `next_replay_run_id`, `latest_replay_run_id`, `prune_replay_runs` | The file's own run numbering, its newest complete run, and the delete of all but the newest five runs of a game. | `api/routes/games.py` |

**Watch out:** a reused `run_id` raises a primary-key error; the caller draws a fresh one
under `_REPLAY_WRITE_LOCK` in `api/routes/games.py`. `load_latest_sim_run` falls back to
the newest run of any kind on a database before Alembic 0030.

### `db/introspect.py`

[Source](../../db/introspect.py) · 119 lines · Tests: [test_db_introspect.py](../../tests/unit/test_db_introspect.py)

The one "does this table or column exist" probe. Code that reads an optional table or
column (one a migration adds, or one a test fixture leaves out) asks the catalog first.
Each helper reads `information_schema` or `to_regclass`, so a missing table answers
"absent" instead of raising. Names enter the SQL as literals, each checked to be a plain
identifier.

| Function | What it does | Called from |
|---|---|---|
| `duck_columns(conn, schema, table, this_file_only=False)` | The column names of a DuckDB table; empty when absent. | the actor engines, the calibration, the bundle builders, the league averages, the pool metadata |
| `duck_table_exists` | Whether a DuckDB table exists. | the advancement-pool builder, the fielder profile, two recompute probes |
| `duck_relation_readable` | Whether `SELECT 1 FROM <relation> LIMIT 0` runs, for an attached relation such as `pg.raw.play_events`. | the profile computor and three profile mixins |
| `pg_column_exists`, `pg_relation_exists` | The same questions on a psycopg2 cursor. | the historical loader |
| `pg_column_exists_async` | The column question on an asyncpg connection or pool. | the live ingestion pipeline |

### `db/migrations/env.py`

[Source](../../db/migrations/env.py) · 114 lines · Tests: [test_schema_migrations.py](../../tests/integration/test_schema_migrations.py)

Alembic's environment. It reads `BASEBALL_DB_DSN`, adds the `+psycopg2` driver suffix to
a plain `postgresql://` DSN, and runs the migrations online (a `NullPool` engine) or
offline (SQL output). `alembic.ini` at the repository root points at this folder.
`db/migrations/script.py.mako` is Alembic's template for a new revision file, and
[`db/migrations/README.md`](../../db/migrations/README.md) explains the folder.

### `db/migrations/versions/`

[Source](../../db/migrations/versions/) · 30 files · Tests: [test_schema_migrations.py](../../tests/integration/test_schema_migrations.py), [test_docs_alembic_head.py](../../tests/unit/test_docs_alembic_head.py)

The Postgres migrations, one per schema change. The [Postgres table](#postgres-alembic)
above lists each. Several unit tests read a migration's source to check its SQL (the odds,
the Savant boards, the bullpen and the backup tests).

### `db/migrations/duckdb/`

[Source](../../db/migrations/duckdb/) · 32 files · Tests: [test_sim_store.py](../../tests/unit/test_sim_store.py), [test_docs_duckdb_version.py](../../tests/unit/test_docs_duckdb_version.py), [test_steal_pool_data.py](../../tests/unit/test_steal_pool_data.py)

The DuckDB migrations, one per schema change. The [DuckDB table](#duckdb-numbered-sql)
above lists each. `db/sim_store.py` applies four of them to the replay file; the rest are
applied to the analytics file by hand or by a rebuild script.

### `db/schemas/02_duckdb_schema.sql`

[Source](../../db/schemas/02_duckdb_schema.sql) · 1,482 lines · Tests: [test_delete_seasons_coverage.py](../../tests/unit/test_delete_seasons_coverage.py), [test_draw_conditioning.py](../../tests/unit/test_draw_conditioning.py), [test_batter_physical_features.py](../../tests/unit/test_batter_physical_features.py)

The analytics file's full schema, applied on every connect by the profile computor
(`PlayerProfileComputor._run_schema_ddl` reads it from `db/schemas/` under the working
directory). It is current: it carries every column of migrations 0002 to 0031.

**Watch out:** the column order of each pool table is load-bearing, because the pool
builders' INSERTs are positional. Its header comment lists eleven of its twenty-two
tables.

### `db/schemas/01_postgres_schema.sql`

[Source](../../db/schemas/01_postgres_schema.sql) · 1,352 lines · Tests: [test_etl_hardening.py](../../tests/unit/test_etl_hardening.py), [test_etl_error_ledger.py](../../tests/unit/test_etl_error_ledger.py), [test_odds_one_book_per_row.py](../../tests/unit/test_odds_one_book_per_row.py)

A reference copy of the Postgres DDL with comments on each table. No code applies it, and
it is incomplete: it has no `raw.savant_pitch_tracking` (0020) and no `raw.game_bullpen`
(0025), and its `raw.games`, `raw.game_lineups` and `sim.sim_runs` lack the columns of
0029 and 0030. Its header calls itself authoritative only through migration 0013. Several
unit tests read it to check a column or a CHECK constraint.

### `db/schemas/duckdb_schema_version.txt`

[Source](../../db/schemas/duckdb_schema_version.txt) · 1 line · Tests: [test_sim_store.py](../../tests/unit/test_sim_store.py), [test_docs_duckdb_version.py](../../tests/unit/test_docs_duckdb_version.py)

The DuckDB schema version as one number. It must equal the newest numbered DuckDB
migration.

### `db/roles/`

[setup_roles.sh](../../db/roles/setup_roles.sh) · 79 lines · [setup_roles.sql](../../db/roles/setup_roles.sql) · 72 lines · [README.md](../../db/roles/README.md) · Tests: [test_no_default_password.py](../../tests/unit/test_no_default_password.py)

`setup_roles.sh` creates the two least-privilege roles and grants them. It is idempotent:
each run creates a missing role, resets both passwords from the environment and re-applies
the grants. A fresh Postgres volume runs it once from the image's init directory;
`make db-roles` runs it on an existing volume, and `make db-roles ROTATE_ADMIN_PASSWORD=1`
also resets the owner's password. It refuses to run when a password is missing. The SQL
receives the passwords as psql variables and quotes them as literals; the script never
echoes a command.

### `db/backup/`

[pg_backup.sh](../../db/backup/pg_backup.sh) · 305 lines · [pg_restore.sh](../../db/backup/pg_restore.sh) · 147 lines · Tests: [test_pg_backup_script.py](../../tests/unit/test_pg_backup_script.py), [test_backup_drill.py](../../tests/unit/test_backup_drill.py), [test_backup_schedule.py](../../tests/unit/test_backup_schedule.py)

| Script | What it does | Called from |
|---|---|---|
| `pg_backup.sh` | Dumps the database in directory format (`pg_dump -Fd`, the format that restores in parallel) into `$BACKUP_ROOT/postgres/<TS>/`, with the row count of every table just before and just after the dump. It builds the set as `<TS>.partial`, opens its permissions so the jobs container can read it, writes `manifest.json` last, then renames the folder. It keeps the newest `BACKUP_KEEP` (3) complete sets. A lock folder allows one run at a time; a free-space guard needs the larger of 10 GiB and 1.5 × the newest set. Exit 2 is a refusal before any write. | the Ofelia job `nightly-backup-postgres` at 10:00 UTC; `make backup` |
| `pg_restore.sh <set>` | Restores one set into an EMPTY database. It waits for the server, drops the empty `raw` and `sim` schemas without CASCADE (a populated database fails the drop, and the script exits 3 before it restores anything), runs `pg_restore` in parallel with `--exit-on-error`, then checks each table's count against the two counts in the set. | `make restore-postgres SET=<ts>`; the restore drill (`scripts/backup_drill.py`) |

**Watch out:** after a restore, run `make db-roles` (the grants), then
`docker compose run --rm migrate alembic current` (expect the head).
