# Scripts

The `scripts/` folder holds the command-line tools around the platform: the nightly shell jobs, the data loaders and backfills, the pool rebuilds, the measurement tools that grade the simulator, and one-off probes. No script serves a user request; the API and the simulator never import one. Operators, the scheduler and the test lanes run them.

## How it fits together

Every script runs in one of three places:

1. **The `jobs` container**, through the Ofelia scheduler (`deploy/ofelia/config.ini`). The nightly shell jobs run there as `appuser`.
2. **The `app` container**, through a `make` target or `docker compose run --rm app python scripts/<name>.py`. Most Python tools need the container's Postgres network and the `/data` volume.
3. **The host**, with the Windows `py` launcher, for the standard-library tools (`gen_secrets.py`, `backup_drill.py`, `check_file_integrity.py`).

Four rules apply to every script:

- **`scripts/` is baked into the app image.** Compose does not bind-mount it ([docker-compose.yml](../../docker-compose.yml) mounts only `api/`, `pipeline/`, `similarity/`, `simulation/` and `db/`). A script edit reaches the `app` and `jobs` containers only after `docker compose build app` (or `jobs`) and a recreate. To run the checkout's copy now, mount it: `docker compose run --rm -v "$PWD:/app" app python scripts/<name>.py`. From Git Bash, prefix `MSYS_NO_PATHCONV=1`, or Git Bash rewrites `/app/...` into a Windows path.
- **No database password has a default.** A script reads `BASEBALL_DB_DSN` through `pipeline.db_dsn.require_dsn` and stops, naming the variable, when it is missing. The containers compose the DSN from `.env`; on the host you set it yourself.
- **Run outputs go to `results/`**, which git and Docker ignore. A small record that a document cites goes to `docs/audit/records/`.
- **A test loads a script through [tests/script_loader.py](../../tests/script_loader.py).** `load_script(name)` looks in `scripts/` and `scripts/probes/`, and `scripts_on_path()` lets a test import a stage package (`accuracy`, `pitch_pool_rebuild`, `steal_pool_rebuild`) by name. [test_script_flag_names.py](../../tests/unit/test_script_flag_names.py) fails when a script outside `scripts/probes/` names a `SIM_*` flag that `simulation/flags.py` does not register.

### The nightly path

The scheduler fires these jobs every day (UTC). Each runs inside `with_mark.sh`, so the jobs can wait for each other.

1. **07:00** — `nightly_finals.sh` loads the last three days' Final games into Postgres, through `with_retry.sh 6`.
2. **08:00** — `nightly_rebuild.sh` waits for the finals load (`wait_for_ingest.sh`), rebuilds the current season's DuckDB profiles and pools (`pipeline.batch.player_profile_computor`), then builds and publishes a new engine-artifact bundle (`pipeline.batch.engine_artifacts --what all`). The app sees the new bundle and restarts itself.
3. **08:00 on Sunday** — `weekly_refresh.sh` sweeps the whole current season for any Final game the three-day window missed.
4. **10:00** — the backups of the three data stores (`db/backup/pg_backup.sh` in the `db` container; `python -m pipeline.backup.data_stores` in `jobs`). The data backup waits for the rebuild's mark.
5. **10:30** — the off-site copy (`python -m pipeline.backup.offsite`, in `jobs`) and `nightly_closing_lines.sh`, which runs in the `app` container because it needs the vendor key from `.env`.

The backup modules live in `pipeline/backup/`; see [Pipeline: the nightly builds](pipeline-batch.md).

### The measurement path

1. `sim_stats.py` — a short smoke of the production simulator: box-score channels against the 2025 season, and one gate (the pitchers' outs equal the outs played).
2. `accuracy_comparison.py` — the simulator's probability against the closing line's, per market, on completed games, scored with a proper scoring rule (a score that rewards only accuracy, never hedging: the Brier score and log loss).
3. `pair_accuracy.py` — two accuracy reports of one game set, read as a paired difference: did a change help?
4. `market_skill.py`, `designed_experiment.py`, `calibrate_markets.py`, `offline_draw_fit.py` — the accuracy fit's instruments (SIM-548): the skill table, the multi-factor experiment, the per-market calibration map and the offline weight fit.

The acceptance lane (`tests/acceptance/`) also reads `sim_stats.py`, `balanced_game_set.py`, `pool_window_census.py` and `fence_check.py`. See [Tests](tests.md).

---

## The nightly shell jobs and their wrappers

All of these are POSIX `sh` scripts that run in the Debian app image. Each one stops at once without `BASEBALL_DB_DSN`. [test_shell_dsn_guards.py](../../tests/unit/test_shell_dsn_guards.py) and [test_no_default_password.py](../../tests/unit/test_no_default_password.py) hold that rule.

### `scripts/nightly_finals.sh`

[Source](../../scripts/nightly_finals.sh) · 51 lines · Tests: [test_shell_dsn_guards.py](../../tests/unit/test_shell_dsn_guards.py), [test_no_default_password.py](../../tests/unit/test_no_default_password.py)

Loads every game that became Final from today minus `DAYS` (default 3) to today, with "today" in Eastern time, the league's day. It calls `HistoricalDataLoader.load_date_range`, which skips games already loaded. Postgres only, so it runs while the app serves. It exits 1 when any game failed.

Run: the scheduler at 07:00 UTC, as `with_mark.sh nightly-finals with_retry.sh 6 nightly_finals.sh`. By hand: `make ingest-catch-up DAYS=14`.

### `scripts/weekly_refresh.sh`

[Source](../../scripts/weekly_refresh.sh) · 29 lines · Tests: [test_shell_dsn_guards.py](../../tests/unit/test_shell_dsn_guards.py), [test_no_default_password.py](../../tests/unit/test_no_default_password.py)

Calls `HistoricalDataLoader.refresh_seasons(year, year)` on the current season, to catch a Final game that the three-day window missed. Postgres only. It exits 1 when any game failed.

Run: the scheduler on Sunday at 08:00 UTC, through `with_mark.sh weekly-refresh` and `with_retry.sh 6`.

### `scripts/nightly_rebuild.sh`

[Source](../../scripts/nightly_rebuild.sh) · 35 lines · Tests: [test_ingest_wait.py](../../tests/unit/test_ingest_wait.py), [test_backup_schedule.py](../../tests/unit/test_backup_schedule.py), [test_shell_dsn_guards.py](../../tests/unit/test_shell_dsn_guards.py)

Rebuilds the current season's profiles and the engine-artifact bundle, in three steps: `wait_for_ingest.sh`, `python -m pipeline.batch.player_profile_computor --seasons <year>`, `python -m pipeline.batch.engine_artifacts --what all`. The profile step writes DuckDB and needs its write lock; the bundle step opens it read-only. The app holds no handle on that file, so the job runs beside the app. The bundle step publishes a new bundle version, and the app restarts itself on the new version (`api/bundle_watch.py`).

Run: the scheduler at 08:00 UTC, through `with_mark.sh nightly-rebuild` and `with_retry.sh 3`. By hand: `docker compose run --rm app sh /app/scripts/nightly_rebuild.sh`.

### `scripts/nightly_ingest.sh`

[Source](../../scripts/nightly_ingest.sh) · 75 lines · Tests: [test_etl_hardening.py](../../tests/unit/test_etl_hardening.py), [test_shell_dsn_guards.py](../../tests/unit/test_shell_dsn_guards.py)

The whole chain in one by-hand run: `refresh_seasons` for the current season, then the profile computor, then the bundle build. It stops before the rebuild when any game failed, so it never rebuilds on a season it knows is incomplete. The scheduler does not run this script; it runs the three jobs above instead.

Run: `docker compose run --rm app sh /app/scripts/nightly_ingest.sh`.

**Watch out:** the Python heredoc's delimiter is quoted (`'PY'`), so the shell does not expand `${YEAR}` inside it. The year crosses into Python through the environment (`os.environ["YEAR"]`).

### `scripts/nightly_closing_lines.sh`

[Source](../../scripts/nightly_closing_lines.sh) · 69 lines · Tests: [test_odds_closing_rows.py](../../tests/unit/test_odds_closing_rows.py), [test_shell_dsn_guards.py](../../tests/unit/test_shell_dsn_guards.py)

The nightly closing pass. A closing row is the last price a book posted before first pitch. The live pipeline marks each game's closing rows at first pitch; this job is the fallback. It runs `load_historical_odds.py --provider bettingpros --line-types closing` over the games of yesterday and the day before (UTC dates, so a late West Coast game is not missed). It writes nothing and exits 0 unless `ODDS_PROVIDER` is `bettingpros`: the mock provider must never write closing rows into the real store. With the real provider and no `ODDS_API_KEY`, it exits 1.

Run: the scheduler at 10:30 UTC, inside the running `app` container (`env ODDS_PROVIDER=bettingpros sh /app/scripts/nightly_closing_lines.sh`). The `app` container reads `ODDS_API_KEY` from `.env`; the `jobs` container does not.

### `scripts/with_retry.sh`

[Source](../../scripts/with_retry.sh) · 57 lines · Tests: [test_retry_wrapper.py](../../tests/unit/test_retry_wrapper.py), [test_backup_schedule.py](../../tests/unit/test_backup_schedule.py)

`with_retry.sh <max_attempts> <command...>` runs the command again after a crash, up to `max_attempts` times, 30 seconds apart (`RETRY_SLEEP_S`). A crash is exit 139 (a segmentation fault) or output that holds "Fatal Python error": the stochastic interpreter crash of the app image (the SIM-445 crash class, CLAUDE.md §2a). Any other non-zero exit stops the wrapper at once with that code. The loaders are idempotent per game, so a rerun resumes where the crash stopped.

### `scripts/with_mark.sh`

[Source](../../scripts/with_mark.sh) · 45 lines · Tests: [test_ingest_wait.py](../../tests/unit/test_ingest_wait.py), [test_backup_schedule.py](../../tests/unit/test_backup_schedule.py)

`with_mark.sh <name> <command...>` writes `$INGEST_MARK_DIR/<name>.running` (default `/data/ingest_marks`) with the start time, runs the command, then removes the mark and writes `<name>.last` as one line: the UTC date, the exit code and the UTC timestamp. The mark spans every attempt of a `with_retry.sh` inside it. When the folder cannot be written, the command still runs, unmarked.

### `scripts/wait_for_ingest.sh`

[Source](../../scripts/wait_for_ingest.sh) · 62 lines · Tests: [test_ingest_wait.py](../../tests/unit/test_ingest_wait.py)

The rebuild's first step. It makes sure the finals load ran today before the rebuild reads Postgres. It waits `INGEST_GRACE_S` (60 s), so a job fired in the same second can write its mark. Then it waits while a fresh `nightly-finals` or `weekly-refresh` mark exists (at most `INGEST_WAIT_S`, 2 hours; a mark older than 6 hours is stale). When `nightly-finals.last` shows today, the rebuild goes on. When it does not, the script runs the finals load first. It never fails the rebuild.

---

## Data loaders and backfills

### `scripts/load_historical_odds.py`

[Source](../../scripts/load_historical_odds.py) · 1,268 lines · Tests: [test_historical_odds_load.py](../../tests/unit/test_historical_odds_load.py), [test_odds_writers.py](../../tests/unit/test_odds_writers.py), [test_odds_loader_retry.py](../../tests/unit/test_odds_loader_retry.py), [test_odds_game_markets.py](../../tests/unit/test_odds_game_markets.py)

Backfills the opening and closing lines of completed games into `raw.game_odds` and `raw.prop_odds`. It walks the Final games in `raw.games` and asks the odds provider for every book's row (one row per book, labelled `bp:<id>`; `bp:0` is the vendor's blended line, stored and never graded). It fetches every game market the book posts and the fifteen prop markets, pitcher markets for pitchers and batter markets for the rest (the vocabulary lives in `pipeline/odds_provider.py`). The load guard (`pipeline/odds_row_guard.py`) checks every row before the write, and the `odds_hash` dedup keeps a rerun idempotent.

| Option | What it does |
|---|---|
| `--provider` | `bettingpros` for real lines, `mock` for a wiring smoke. The loader refuses the mock unless it is named. |
| `--line-types`, `--game-dates`, `--seasons`, `--max-games` | Select what to load. |
| `--prop-stats`, `--game-markets`, `--no-props`, `--no-game-odds` | Narrow the markets. |
| `--book NAME` | Load one book's rows (a smoke or a top-up). |
| `--done-file PATH`, `--skip-loaded-since TS` | Resume a run that died. |
| `--retries N`, `--retry-wait S` | The vendor-read retry policy handed to the provider. |

Run: `make load-historical-odds FLAGS="--seasons 2024 --provider bettingpros"`. The nightly closing pass calls it too.

### `scripts/load_official_boxscores.py`

[Source](../../scripts/load_official_boxscores.py) · 344 lines · Tests: [test_boxscore_ingest.py](../../tests/unit/test_boxscore_ingest.py), [test_game_bullpen_listing.py](../../tests/unit/test_game_bullpen_listing.py)

Backfills the official box score: one `raw.game_player_stats` row per player who appeared, from the MLB Stats API `/game/{pk}/boxscore` endpoint. The same fetch writes the per-game bullpen listing (`raw.game_bullpen`); `--bullpen-only` writes the listing alone. Props grade on this box (owner decision 2026-09-12, the official box-score ground truth, SIM-545). `--only-missing` (the default) skips a game that has rows. Five failures in a row stop the run (`--max-consecutive-failures`). The historical loader writes the box for each game it loads, so this script is for games already in `raw.games`.

Run: `python scripts/load_official_boxscores.py --seasons 2024` in the app container.

### `scripts/reload_games.py`

[Source](../../scripts/reload_games.py) · 374 lines · Tests: none

Reloads specific games, or every gap it finds. It sorts the gaps into three classes: STALE (pitch rows with no `raw.etl_game_ingest` ledger row: the game looks loaded and is not), FAILED (a ledger row that is not `loaded` for a Final game) and NOT PLAYED (an `empty` outcome for a cancelled or postponed game; nothing to fix). It reloads the first two and reports the third. `reload_game` deletes and re-inserts one game in one transaction, and the shrink guard refuses a reload that loses rows unless `--allow-shrink`. The run ends with a per-season reconciliation of the ledger against the pitch rows.

Run: `python scripts/reload_games.py --dry-run`, then without `--dry-run`. `--game-pk`, `--season` and `--refresh-venues` narrow or widen it.

### `scripts/resumable_sweep.py`

[Source](../../scripts/resumable_sweep.py) · 287 lines · Tests: [test_sweep_streaming.py](../../tests/unit/test_sweep_streaming.py)

Drives a full corrective reload of one or more seasons to completion despite the stochastic interpreter crash. It runs the loader in a child process, records every finished `game_pk` in `.sweep_progress/<season>.txt` (ignored by git), and restarts the child after a crash, skipping the finished games. A crash cannot corrupt a game, because each reload is one transaction. A full ten-season sweep takes about six hours.

Run on the host: `python scripts/resumable_sweep.py --season 2017 --end-season 2026`. `--in-docker` uses the compose DSN instead of `127.0.0.1:DB_HOST_PORT`.

**Watch out:** the child reads `BASEBALL_DB_DSN` from the repo's `.env` file, not from the environment. The stack's `.env` carries no DSN line by default (compose builds the DSN), so add a host DSN line before a run.

---

## The pool rebuilds

Both rebuilds rewrite one pool in DuckDB inside one transaction, prove the rebuild moved only the rows the design says it moves, export that pool alone into the bundle, and prove the export changed nothing else. Both share one exit-code set: 0 complete; 1 an unexpected error before the export (undone); 2 a pre-flight or pool check failed (nothing exported); 3 the DuckDB file is locked; 4 the export or its round-trip failed (the bundle needs the printed rollback).

**Watch out:** stop the app first. The export rewrites, in place, the bundle version the app's workers read. Run from the main checkout with the code mounted (`-v "$PWD:/app"`).

### `scripts/rebuild_pitch_pool.py` and `scripts/pitch_pool_rebuild/`

[Source](../../scripts/rebuild_pitch_pool.py) · 494 lines · Tests: [test_pool_label_check.py](../../tests/acceptance/test_pool_label_check.py)

Rebuilds `sim.pitch_pool` on the foul-tip coding: a two-strike foul tip or foul bunt is strike three (the foul-tip strike three, SIM-553; builder `sim553.1`). Steps: 0 the pre-flight, 1 a snapshot of every pitch's class, 2 the rebuild, 3 the checks inside the transaction, 4–6 the bundle copy, the export and the round-trip. `--check-only` runs the pre-flight alone.

| Module | What it holds |
|---|---|
| [`pitch_pool_rebuild/preflight.py`](../../scripts/pitch_pool_rebuild/preflight.py) (645 lines) | Step 0: every reason the write must not start, and an information-only drift line for the other pools. |
| [`pitch_pool_rebuild/checks.py`](../../scripts/pitch_pool_rebuild/checks.py) (409 lines) | Step 3: checks a–e inside the transaction (row counts, exactly the expected class moves, no moved foul tip with the got-away flag), the rollback and the restore. |
| [`pitch_pool_rebuild/export.py`](../../scripts/pitch_pool_rebuild/export.py) (633 lines) | Steps 4–6: the bundle copy, the pitch-pool export, the round-trip and the rollback text. |
| [`pitch_pool_rebuild/common.py`](../../scripts/pitch_pool_rebuild/common.py) (224 lines) | The paths, the files of a whole pitch pool, the expected class moves and the exit codes. |
| [`pitch_pool_rebuild/__init__.py`](../../scripts/pitch_pool_rebuild/__init__.py) (8 lines) | The step map. |

### `scripts/rebuild_steal_pool.py` and `scripts/steal_pool_rebuild/`

[Source](../../scripts/rebuild_steal_pool.py) · 458 lines · Tests: [test_steal_pool_rebuild.py](../../tests/unit/test_steal_pool_rebuild.py)

Rebuilds `sim.steal_opportunity_pool` for the running game on the pitch (SIM-554; builder `sim554.1`). The pool gains the class of the pitch each row rode (`pitch_class`) and the pickoff rows thrown before any pitch (`is_pickoff_row`). `--apply-migration` applies DuckDB migration 0031 inside the rebuild's own transaction, after the snapshot, so a failed check rolls the columns back with the rows. Never apply 0031 as a separate step.

| Module | What it holds |
|---|---|
| [`steal_pool_rebuild/preflight.py`](../../scripts/steal_pool_rebuild/preflight.py) (225 lines) | Step 0: the builder version, the migration, the window's pitch pool, the build watermark, a whole bundle pool. |
| [`steal_pool_rebuild/pickoffs.py`](../../scripts/steal_pool_rebuild/pickoffs.py) (142 lines) | Step 1: the snapshot, and the pickoff rows counted from the play records, never from the builder's SQL. |
| [`steal_pool_rebuild/migration.py`](../../scripts/steal_pool_rebuild/migration.py) (35 lines) | Migration 0031, applied inside step 2. |
| [`steal_pool_rebuild/checks.py`](../../scripts/steal_pool_rebuild/checks.py) (400 lines) | Step 3: checks a–h inside the transaction, and the rollback. |
| [`steal_pool_rebuild/bundle.py`](../../scripts/steal_pool_rebuild/bundle.py) (230 lines) | Step 4: whether the bundle's steal pool is whole, the rollback copy and the hashes that prove the export touched nothing else. |
| [`steal_pool_rebuild/export.py`](../../scripts/steal_pool_rebuild/export.py) (351 lines) | Steps 5–7: the export, the round-trip, the steal band centres and the rollback text. |
| [`steal_pool_rebuild/common.py`](../../scripts/steal_pool_rebuild/common.py) (158 lines) | The paths, the files of a whole steal pool, the labels and the exit codes. |
| [`steal_pool_rebuild/__init__.py`](../../scripts/steal_pool_rebuild/__init__.py) (9 lines) | The step map. |

---

## Measurement tools

### `scripts/sim_stats.py`

[Source](../../scripts/sim_stats.py) · 666 lines · Tests: [test_smoke_outs_gate.py](../../tests/unit/test_smoke_outs_gate.py), [test_fence_counters.py](../../tests/unit/test_fence_counters.py), [test_no_pitch_readers.py](../../tests/unit/test_no_pitch_readers.py)

The Monte-Carlo box-score harness. It runs N iterations per game (default 200) through the production factory over the real bundle, in one process, and prints per-team R, H, HR, 2B, 3B, BB, K, SB and CS against this project's own 2025 season (`_MLB_2025`), the home and away split, the fence stage's counters and the wall-margin band's counters. It prints the realism flags it ran with, so a no-op never reads as "no effect". It reads the DuckDB file read-only, so it runs during a nightly build.

| Function | What it does | Called from |
|---|---|---|
| `outs_credit_check` | Compares the pitchers' credited outs with the outs each game played (`GameSimResult.outs_played`). A difference makes the script exit 1: its one gate. | `main`; [test_smoke_outs_gate.py](../../tests/unit/test_smoke_outs_gate.py) |
| `_aggregate`, `_print_report` | The per-channel means and the report. | `main` |
| `_fence_counter_summary`, `_fence_d_read`, `_margin_counter_summary` | The fence stage's counters with the fence certification's D thresholds, and the wall-margin band's counters. | `main`; [test_fence_counters.py](../../tests/unit/test_fence_counters.py) |

Run: `python scripts/sim_stats.py --iters 200 744795 661032 --json-out results/x.json` in the app container. The per-change smoke of the architecture rule's third clause (owner ruling 2026-09-16) is ten games of this script.

### `scripts/accuracy_comparison.py` and `scripts/accuracy/`

[Source](../../scripts/accuracy_comparison.py) · 601 lines · Tests: [test_accuracy_comparison.py](../../tests/unit/test_accuracy_comparison.py), [test_accuracy_comparison_props.py](../../tests/unit/test_accuracy_comparison_props.py), [test_accuracy_minimum_sample.py](../../tests/unit/test_accuracy_minimum_sample.py), [test_segment_markets.py](../../tests/unit/test_segment_markets.py)

The platform's validation of the simulator against the market. For every completed game with a closing line, it scores the simulator's probability and the closing line's de-vigged probability (the book's margin removed) against what happened, with the Brier score and log loss. Both scores come from the same game and outcome, so the comparison is paired: per observation, the simulator's score minus the market's. The closing line is the benchmark by owner decision 2026-09-10. Props grade on the official box score, with the event labels as a per-game fallback. The comparison grades each market against one book: the first book on `GRADED_BOOK_PREFERENCE` with a closing row (one graded book, SIM-555).

The file is the command line; the stages live in the package:

| Module | What it holds |
|---|---|
| [`accuracy/selection.py`](../../scripts/accuracy/selection.py) (201 lines) | The game list (Final games, optionally cut to `--game-pks-file`) and `bundle_provenance`: the bundle, calibration and flags behind a run, so a paired read refuses two reports built from different inputs. |
| [`accuracy/replay.py`](../../scripts/accuracy/replay.py) (787 lines) | `_score_one_game`, the unit of work: the closing odds, the lineup, the park factor and the point-in-time cutoff, N replays through the production factory, the win probability through the calibration map, the prop distributions, the scoring. `_run_parallel` fans whole games out to `forkserver` workers (`--workers`, default 6); `--workers 1` is the serial path. A broken process stops the run with a named cause (`WorkerInitError`); a bad game is skipped and counted. |
| [`accuracy/pricing.py`](../../scripts/accuracy/pricing.py) (573 lines) | The graded row per market, `attach_best_prices` (the best price a bettor could take at the graded line), `reference_margin` (the margin that prices a one-sided bet), `--benchmark-book`. |
| [`accuracy/grading.py`](../../scripts/accuracy/grading.py) (779 lines) | What really happened, and one record per priced market: `score_game_accuracy`, `score_segment_market_accuracy`, `score_prop_accuracy`, `score_run_line_market` (a run line is a pair only when the away spread is the negative of the home spread; else it is two separate bets, SIM-549). |
| [`accuracy/records.py`](../../scripts/accuracy/records.py) (262 lines) | `AccuracyRecord` (one paired observation) and the market vocabulary. |
| [`accuracy/report.py`](../../scripts/accuracy/report.py) (368 lines) | `aggregate_accuracy_comparison` (one row per market, a seeded bootstrap interval) and the text table. |
| [`accuracy/confidence.py`](../../scripts/accuracy/confidence.py) (249 lines) | The game-clustered bootstrap and the stated minimum sample size per row (the edge floor 0.02 shrunk by the detection margin 1.6, Bonferroni-corrected). |
| [`accuracy/hypothetical_return.py`](../../scripts/accuracy/hypothetical_return.py) (370 lines) | The opt-in dollar-return table (`--report-hypothetical-return`). Its `certified` flag is always False: no betting-value read counts until every band is green (owner ruling 2026-09-08). |
| [`accuracy/__init__.py`](../../scripts/accuracy/__init__.py) (21 lines) | The stage map. |

Run: `python scripts/accuracy_comparison.py --seasons 2024 --workers 6 --iterations 100` in the app container. The report goes to `/data/accuracy_comparison.json` (`CLV_BACKTEST_PATH`) unless `--output` names a file. It refuses to run park-blind unless `--allow-neutral-parks`.

**Watch out:** the point-in-time cutoff bounds the pool rows a draw can use, but the player profiles that weight the draws come from one bundle built on one date. A backtest game can still be weighted by a player's later form.

### `scripts/pair_accuracy.py`

[Source](../../scripts/pair_accuracy.py) · 411 lines · Tests: [test_pair_accuracy.py](../../tests/unit/test_pair_accuracy.py), [test_accuracy_fit_instruments.py](../../tests/unit/test_accuracy_fit_instruments.py)

Pairs two accuracy reports run on the same games, seeds and bundle: the OFF arm and the ON arm. Per market it reports the mean paired Brier and log-loss difference (ON minus OFF; negative means ON was more accurate), the game-clustered range and the minimum sample. It refuses to pair reports whose provenance differs (the bundle, the calibration hash, the seed, the iterations, the game list, the run-line scoring stamp, the graded odds rows) unless `--force`. With a DSN it splits the props by the starter's real pitch count and can write a deep-start game list.

Run: `python scripts/pair_accuracy.py off.json on.json --json-out results/pair.json`.

### `scripts/market_skill.py`

[Source](../../scripts/market_skill.py) · 628 lines · Tests: [test_market_calibration.py](../../tests/unit/test_market_calibration.py), [test_accuracy_fit_instruments.py](../../tests/unit/test_accuracy_fit_instruments.py)

The per-market skill table of one read: the simulator's and the market's Brier score, a base forecast that knows only the outcome rate, the skill (base minus model), the gap to the line with a game-clustered range, the bias, the spread, the AUC (the chance that a real "yes" drew the higher probability) and the reliability by bin. It merges several reports of one configuration on disjoint game sets. `--compare` adds the composite objective Z between two reads on the same games (the accuracy fit, SIM-548). It refuses reports graded against different odds rows.

### `scripts/designed_experiment.py` and `scripts/designed_experiment_run.sh`

[Source](../../scripts/designed_experiment.py) · 544 lines · [Run script](../../scripts/designed_experiment_run.sh) · 37 lines · Tests: [test_designed_experiment.py](../../tests/unit/test_designed_experiment.py)

The designed experiment on the full simulator (the accuracy fit, SIM-548). `--factors` names up to six weights, each with a production level, a candidate level and an optional mid level. Up to four factors run the full factorial; five run a half fraction; six run a quarter fraction. Each arm is one `accuracy_comparison.py` run with the arm's settings exported; an arm whose report exists is skipped, so the run resumes. The read gives each factor's main effect, the two-way interactions by alias group, the curvature from centre arms, the noise floor from baseline repeats and the composite Z. The architecture rule fits every weight together in one such experiment when the owner calls the model final. `designed_experiment_run.sh` is the four-factor run of the accuracy fit, written for the app container with the app stopped.

### `scripts/calibrate_markets.py`

[Source](../../scripts/calibrate_markets.py) · 565 lines · Tests: [test_market_calibration.py](../../tests/unit/test_market_calibration.py)

Fits a per-market calibration map on one set of accuracy reports and checks it on another set it never saw: by default a logistic map, `logit(p') = a + b·logit(p)`, or `--method isotonic` (a monotone step map). The map corrects probabilities, never plays. A map earns its place only when it improves the check set. `--write-calibration` merges the maps into the calibration file under `market_calibration`; no app code reads that key. It refuses reports graded against different odds rows.

### `scripts/offline_draw_fit.py`

[Source](../../scripts/offline_draw_fit.py) · 1,059 lines · Tests: [test_accuracy_fit_instruments.py](../../tests/unit/test_accuracy_fit_instruments.py)

The offline joint fit of the pitch draws' weights (the accuracy fit, SIM-548). It replays real pitches through the simulator's own sampler, without playing games. For each pitch it sets the cutoff to the day before the game, reads the whole candidate set's weights as a distribution over the six pitch outcomes, and scores the real outcome. It scores per pitch, per plate appearance (the count buckets run as a chain to the plate appearance's end) and per start (the predicted strikeouts against the real ones). `--holdout-season` scores the top settings on another season.

### `scripts/fence_check.py`

[Source](../../scripts/fence_check.py) · 1,444 lines · Tests: [test_fence_check.py](../../tests/unit/test_fence_check.py)

The certification of the outfield fence lines in `park_geometry.json` (the fence certification, SIM-478/479/480). It replays the fence stage's rule on every real air ball of the pool window and grades seven checks: A1 the whole window, A2 every venue with 200 or more balls, A3 held-out seasons, A4 the down-the-line balls, A5 moved walls, A6 thin parks, A7 the transfer of every ball into each park against that park's home-run rate. `--lane` adds B2, the per-park read of a lane record. It opens DuckDB read-only. It exits 1 when a check fails. It runs in the run book of every geometry rebuild.

### `scripts/balanced_game_set.py`

[Source](../../scripts/balanced_game_set.py) · 383 lines · Tests: read by [tests/acceptance/bands.py](../../tests/acceptance/bands.py)

Builds the balanced certifying game set (owner ruling 2026-09-09): three full-day slates from distinct seasons, every team on the field each day, chosen so each graded channel's actor-matched expectation (the starters' and batters' own pool rates, weighted by their plate appearances) sits near the pool's totals. It prints the game keys in a park-balanced order, ready for `bands.py`, and `--json-out` writes [scripts/data/balanced_game_set.json](../../scripts/data/balanced_game_set.json).

### `scripts/pool_window_census.py`

[Source](../../scripts/pool_window_census.py) · 306 lines · Tests: [test_pool_label_check.py](../../tests/acceptance/test_pool_label_check.py)

Measures three pool windows (W1 full seasons 2023–2026, the production window; W2 2024–2026; W3 a rolling three years): the pool volumes, the per-count outcome shares run through the count chain to per-plate-appearance centres, the thinnest hard-filter cells. It also runs the label check: the pool's chain against real plate appearances counted from `raw.pitches`, PASS within 0.5% (`pipeline.batch.pool_chain.LABEL_CHECK_TOLERANCE`). The pool-totals grade cannot see a labelling defect, because it reads the same labels; this check can. `--strict` exits 1 when a window's check fails or cannot run.

### `scripts/measure_filter_cells.py`

[Source](../../scripts/measure_filter_cells.py) · 1,378 lines · Tests: [test_filter_cells.py](../../tests/unit/test_filter_cells.py), [test_pitch_cell_index.py](../../tests/unit/test_pitch_cell_index.py)

Measures how full the pitch draw's 2,880 hard-filter cells are (base occupancy × outs × count × score band × home or away; the filter-cell occupancy measurement, SIM-451). It reports the share of all draws that land in an under-full cell for each candidate minimum (`DEFAULT_MIN_CELL` is 20 in `simulation/filter_cells.py`), and what each step of the widening ladder recovers.

**Watch out:** its default `--output` is `docs/audit/2026-08-10-sim451-filter-cell-occupancy.json`, inside the docs tree rather than `results/`. Pass `--output`.

### `scripts/running_game_replay.py`

[Source](../../scripts/running_game_replay.py) · 1,473 lines · Tests: [test_running_game_replay.py](../../tests/unit/test_running_game_replay.py), [test_steal_weights_seam.py](../../tests/unit/test_steal_weights_seam.py)

The running-game replay (the running game on the pitch, SIM-554). It replays real pitches on which a steal was possible through the sampler, hides the pitch's game and every later game, reads the whole candidate set as a table over the pitch's result and the steal outcome, and scores eleven ways of sampling the steal against what happened. `pass` scores one season; `report` compares a tune season with a test season. `steal_weight_volume_probe.py` imports it.

### `scripts/trace_game.py`

[Source](../../scripts/trace_game.py) · 183 lines · Tests: [test_no_pitch_readers.py](../../tests/unit/test_no_pitch_readers.py)

Runs one game through the production loop and writes a CSV with one row per pitch: the state before, the box totals before, the pitch and its resolution, the state after and the totals after. A no-pitch pickoff for the third out is a row with `pitch_outcome = no_pitch`. Use it to find where a hit goes unrecorded or a run is over-credited.

Run: `python scripts/trace_game.py 744980 --seed 42 > trace.csv` in the app container (the summary goes to stderr).

---

## Calibration and prop validation

### `scripts/fit_calibration.py`

[Source](../../scripts/fit_calibration.py) · 296 lines · Tests: none

Fits the `CalibrationReport` over the DuckDB profiles and writes it to `/data/calibration.json` (`CALIBRATION_REPORT_PATH`). The app loads that file at boot. It applies the per-engine bandwidths to every similarity engine that exposes `apply_calibration`, and derives the win-probability map from any reliability curve the file holds. `SimilarityCalibrator.calibrate_from_population` fits the batter, pitcher, fielder and baserunner engines and the bandwidths of the catcher, steal-runner, pitcher-steal and manager engines. The pitcher arsenal anchor samples same-hand W₂ distances (the Wasserstein-2 distance between two pitchers' pitch-mix models) from the built pitcher engine; `--no-arsenal` keeps the 4.10 default. `--validate` checks each engine's median similarity near 0.50.

Run: `make calibrate`, after the profile computor.

**Watch out:** the script writes the whole file, so it drops the win-probability reliability curve. Write the curve back with `validate_props.py --write-calibration`, or with `write_reliability_curve_to_calibration_report` from `/data/prop_validation.json`, or the app boots uncalibrated.

### `scripts/validate_props.py`

[Source](../../scripts/validate_props.py) · 611 lines · Tests: [test_validate_props.py](../../tests/unit/test_validate_props.py), [test_calibration_reconciliation.py](../../tests/unit/test_calibration_reconciliation.py)

Validates the win probabilities and the prop distributions (the probability of each count of a stat) against completed games. Per Final game it resolves the lineup, replays N iterations with `record_game_plays` through the production factory, and pairs the home win probability with the real score and each prop distribution with the official box score (`raw.game_player_stats`, every priced market) or, for a game with no box rows, the event labels (H, HR, TB, K, BB only). `simulation.prop_validation` scores the pairs: the expected calibration error (the mean gap between predicted and observed rates), the Brier score, log loss, the reliability curve and the per-prop calibration. It writes `/data/prop_validation.json` (`PROP_VALIDATION_PATH`). `--write-calibration` writes the fitted reliability curve into the calibration file. It exits 2 when the park-factor source will not open, unless `--allow-neutral-parks`.

Run: `make validate-props FLAGS="--seasons 2024 --iterations 100 --write-calibration"`.

### `scripts/boxscore_audit.py`

[Source](../../scripts/boxscore_audit.py) · 475 lines · Tests: [test_boxscore_reader.py](../../tests/unit/test_boxscore_reader.py)

The study behind the box-score ground truth (SIM-545). It derives every per-player prop total the platform can build from `raw.pitches` and `raw.play_events`, compares each with the official box (`raw.game_player_stats`) for the same games, and prints per stat the rows compared, the exact-match rate, the mean absolute difference and the ten worst players. Its read is the evidence for the owner decision to grade props on the box, never the derivation. Read-only. Sample hundreds of games, never dozens.

Run: `python scripts/boxscore_audit.py --seasons 2024 --max-games 500` in the app container.

---

## Operations

### `scripts/gen_secrets.py`

[Source](../../scripts/gen_secrets.py) · 208 lines · Tests: [test_gen_secrets.py](../../tests/unit/test_gen_secrets.py)

Generates the stack's eight secrets: the three Postgres role passwords, the Redis password, the Grafana admin password, the session-signing key, the login password and one API key. Each value is `secrets.token_urlsafe(32)`. `--append .env` adds only the missing or blank keys and never changes a value; `--check .env` exits 1 when a secret is missing. It prints key names only, never a value. Standard library only.

Run on the host: `make gen-secrets` (`py scripts/gen_secrets.py --append .env`).

### `scripts/backup_drill.py`

[Source](../../scripts/backup_drill.py) · 535 lines · Tests: [test_backup_drill.py](../../tests/unit/test_backup_drill.py), [test_backup_schedule.py](../../tests/unit/test_backup_schedule.py)

The restore drill. It proves a backup set restores, without touching the live database: a scratch Postgres cluster with no network, the real `db/backup/pg_restore.sh` against it, a verify of the DuckDB and bundle copies in the `jobs` container (`python -m pipeline.backup.data_stores --verify-set`), then the scratch cluster's removal, whatever happened. It writes `<backup dir>/drills/<start>.json`. `--offsite` restores from the OneDrive copy instead. It exits 0 on a pass, 1 when a step fails, 2 when it refuses to start. Standard library only; it needs about 20 GB free in Docker's disk image while it runs.

Run on the host: `make backup-drill` or `make backup-drill OFFSITE=1`.

### `scripts/export_openapi.py`

[Source](../../scripts/export_openapi.py) · 45 lines · Tests: [test_openapi_snapshot.py](../../tests/unit/test_openapi_snapshot.py)

Writes the FastAPI schema to `frontend/openapi.json`, indented with sorted keys. `npm run gen:api` then generates the frontend's TypeScript types from it. Run it whenever the API contract changes, in the app image with the checkout mounted, never on the host: the host's Pydantic drops `additionalProperties: true`.

Run: `docker compose run --rm -v "$PWD:/app" app python scripts/export_openapi.py`, then `cd frontend && npm run gen:api`, and commit both files.

### `scripts/check_file_integrity.py`

[Source](../../scripts/check_file_integrity.py) · 176 lines · Tests: [test_check_file_integrity.py](../../tests/unit/test_check_file_integrity.py)

Catches a `.py` file that a file bridge truncated or filled with NUL bytes: it flags any file with a NUL byte or one that `ast.parse` rejects, and exits 1 on an offender. With paths it checks those; with none it walks the repository. Standard library only. The pre-commit hook (`.pre-commit-config.yaml`) and the CI `file-integrity` job run it.

### `scripts/behaviour_fingerprint.py`

[Source](../../scripts/behaviour_fingerprint.py) · 546 lines · Tests: none

Proves a refactor leaves the simulator's output unchanged. It runs seeded games and writes one SHA-256 hash per game (the play stream, the box score, the linescore) to a JSON record; equal records before and after a change mean the change is behaviour-neutral. Modes: `nodb` (the no-database factory over the in-memory bundle), `synthetic` (the production factory with the code defaults and with the compose flags), `production` (a real bundle and the 45 balanced games; pass a copy of one bundle version and `--kwargs-cache`), `bundle` (hash every array of a built bundle) and `compare` (exit 1 on any difference). The record compares two commits on one day; it is not a golden file.

### `scripts/docs_file_index.py`

[Source](../../scripts/docs_file_index.py) · Tests: [test_docs_file_index.py](../../tests/unit/test_docs_file_index.py)

Writes [file-index.md](file-index.md): every tracked source and config file, grouped by the stage of the [code review path](code-review-path.md) that reads it, with its size, the first sentence of its docstring or header comment, and the test files that import it. `--check` exits 1 when the page is stale; the unit test does the same check. It needs `git` and the standard library only.

Run on the host: `python scripts/docs_file_index.py` after you add, move or remove a file, or change a file's first docstring sentence.

### `scripts/genexpr_crash_probe.py`

[Source](../../scripts/genexpr_crash_probe.py) · 434 lines · Tests: none

The probe for the stochastic interpreter crash (the SIM-445 crash class). It reruns the workload that crashed, in generator-expression and list-comprehension forms, with and without the live engine. Run it on the day the crash recurs: when the old shapes crash and the new ones pass, the shape matters; when both crash, it does not.

### Accuracy-fit run scripts

| Script | What it runs |
|---|---|
| [`accuracy_baseline_1000.sh`](../../scripts/accuracy_baseline_1000.sh) (28 lines) | The 1,000-game accuracy baseline of production: games 251–1,000 of 2024 as three 250-game chunks (`scripts/data/games_2024_chunk{2,3,4}.txt`), each its own report under `results/sim548/`. |
| [`accuracy_split_arm.sh`](../../scripts/accuracy_split_arm.sh) (24 lines) | The pitch / pitch-result split as one paired arm (pitcher power 16 in both draws, batter power 8) on the baseline's 250 games. |

Both run in the app container with the checkout mounted (`docker compose run -d --rm -v "$PWD:/app" app sh scripts/<name>.sh`).

---

## Probes

`scripts/probes/` holds one-off tools: an instrument for one decision, a one-time backfill or a run-book step. Each still runs. Each prints its own usage at the top of its file. Most run in the app container with the checkout mounted: `MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD:/app" app python scripts/probes/<name>.py`.

| Probe | What it does | Tests |
|---|---|---|
| [`actor_concentration_check.py`](../../scripts/probes/actor_concentration_check.py) (86) | Reruns the bundle's concentration report at given powers: no factor may put more than its natural share of a draw on the live player's own team (the play-picker redesign, SIM-523). | none |
| [`arm_angle_probe.py`](../../scripts/probes/arm_angle_probe.py) (1,223) | The evidence for the arm-angle decision: Savant's arm angle, active spin and spin-axis deviation add under one R² point, so none joins the pitcher model (owner decision 2026-09-19, SIM-533). | [test_arm_angle_decision.py](../../tests/unit/test_arm_angle_decision.py) |
| [`backfill_odds_hash.py`](../../scripts/probes/backfill_odds_hash.py) (279) | One-time backfill of `raw.game_odds.odds_hash` on old rows, then a collapse of duplicates to the earliest row (SIM-157). | [test_backfill_odds_hash.py](../../tests/unit/test_backfill_odds_hash.py) |
| [`backfill_start_positions.py`](../../scripts/probes/backfill_start_positions.py) (352) | Repairs each starter's position in `raw.game_lineups` to the one he started at (`allPositions[0]`), one UPDATE per game; `--done-file` makes it crash-safe. Postgres only (the starting position, SIM-559). | none |
| [`book_sharpness.py`](../../scripts/probes/book_sharpness.py) (921) | Which book's closing line forecasts the outcome best, on common games, by Brier score with a game-clustered range; the owner sets the graded-book order from it (SIM-555). | [test_book_sharpness.py](../../tests/unit/test_book_sharpness.py) |
| [`bullpen_check.py`](../../scripts/probes/bullpen_check.py) (227) | The real pen against reality: coverage, arms per side, the relievers who pitched that the pen holds, and the rest mix (the manager draw, SIM-427). | none |
| [`catcher_receiving_probe.py`](../../scripts/probes/catcher_receiving_probe.py) (198) | The catcher receiving ratio on and off, with the real catchers and with the best and worst framer: the called-strike share of taken pitches must move while the taken share does not. | none |
| [`check_bat_side_coverage.py`](../../scripts/probes/check_bat_side_coverage.py) (188) | Audits `raw.pitches.bat_hand` coverage per season and writes the report `docs/data_quality/2026-05-20-bat-side-coverage.md` (SIM-160). | none |
| [`conditional_rate_probe.py`](../../scripts/probes/conditional_rate_probe.py) (1,100) | The conditional-rate fit instrument: the simulator's rates conditioned on the live actor (pitcher, batter, runner, catcher, outfielder) against the pool's own rates for that actor's rows, by tier. | [test_outfield_arm_recompute.py](../../tests/unit/test_outfield_arm_recompute.py) |
| [`fatigue_kernel_scan.py`](../../scripts/probes/fatigue_kernel_scan.py) (749) | The offline scan of the pitcher-fatigue kernel at a ladder of bandwidths, from the pool alone (the fatigue fit, SIM-518). | [test_fatigue_kernel_scan.py](../../tests/unit/test_fatigue_kernel_scan.py) |
| [`fatigue_probe.py`](../../scripts/probes/fatigue_probe.py) (582) | The in-loop fatigue probe: one bandwidth arm per process on the balanced set; `report` pairs the arms. | via [test_odds_readers.py](../../tests/unit/test_odds_readers.py) |
| [`fielder_profile_recompute.py`](../../scripts/probes/fielder_profile_recompute.py) (728) | Run-book step: DuckDB migration 0030, the fielder chain, the league rows and the verify block for Savant's outs above average and the outfield jump (SIM-532). | [test_fielder_profile_recompute.py](../../tests/unit/test_fielder_profile_recompute.py) |
| [`manager_probe.py`](../../scripts/probes/manager_probe.py) (915) | The manager probe: one pitching-change arm per process; reads pitchers per game, the starter's pitches at the pull, the change rate by manager tier and the entering arm's rest (SIM-427). | via [test_odds_readers.py](../../tests/unit/test_odds_readers.py) |
| [`manager_profile_recompute.py`](../../scripts/probes/manager_profile_recompute.py) (142) | Recomputes the manager profiles and the manager league-average row on their own (SIM-427). | none |
| [`odds_book_census.py`](../../scripts/probes/odds_book_census.py) (1,872) | The read-only census of the vendor's books before a re-load: graded-book coverage, first-five rows that look like first-inning bets, the dated exclusion (SIM-555). | [test_odds_book_census.py](../../tests/unit/test_odds_book_census.py) |
| [`odds_census.sql`](../../scripts/probes/odds_census.sql) (459) | The read-only SQL census of one season's one-row-per-book odds rows. Run: `docker compose exec -T db psql -U baseball_user -d baseball_sim -v season=2024 -f - < scripts/probes/odds_census.sql`. | [test_odds_census_sql.py](../../tests/unit/test_odds_census_sql.py) |
| [`outfield_arm_recompute.py`](../../scripts/probes/outfield_arm_recompute.py) (837) | Run-book step: the outfield arm block rebuilt from the advancement pool, the league rows, the verify block and a cross-check against Savant's fielder view (SIM-550). | [test_outfield_arm_recompute.py](../../tests/unit/test_outfield_arm_recompute.py) |
| [`rescore_graded_book.py`](../../scripts/probes/rescore_graded_book.py) (682) | Re-prices the fixed-line records of stored accuracy reports against the graded book, beside the originals (`<name>.sim555.json`). | [test_rescore_graded_book.py](../../tests/unit/test_rescore_graded_book.py) |
| [`rescore_runlines.py`](../../scripts/probes/rescore_runlines.py) (460) | Re-prices the run-line records of stored accuracy reports under the pair-or-two-bets rule (`<name>.sim549.json`). | [test_rescore_runlines.py](../../tests/unit/test_rescore_runlines.py) |
| [`retire_consensus_odds.py`](../../scripts/probes/retire_consensus_odds.py) (419) | Archives, then deletes, the old mixed `consensus` odds rows, one season per transaction, into `raw.game_odds_archive` / `raw.prop_odds_archive`. | [test_retire_consensus_odds.py](../../tests/unit/test_retire_consensus_odds.py) |
| [`run_index_acceptance.py`](../../scripts/probes/run_index_acceptance.py) (380) | The `EXPLAIN ANALYZE` gates for two Postgres indexes; writes the report `docs/perf/2026-05-13-index-acceptance.md` (SIM-158). | [test_index_acceptance_harness.py](../../tests/unit/test_index_acceptance_harness.py) |
| [`runner_profile_recompute.py`](../../scripts/probes/runner_profile_recompute.py) (368) | Run-book step: DuckDB migrations 0028 and 0029, the three runner-side profile tables and the league rows for the lead distance (SIM-531). | none |
| [`running_game_census.py`](../../scripts/probes/running_game_census.py) (1,067) | Where the simulator's steals and pickoffs land, by pitch class and count, per arm of `SIM_STEAL_PITCH_CLASS`, with a near-noiseless expected-rate read (SIM-554). | [test_running_game_census.py](../../tests/unit/test_running_game_census.py) |
| [`steal_weight_volume_probe.py`](../../scripts/probes/steal_weight_volume_probe.py) (1,161) | Why the steal draw's look-alike weights lower the steal attempts, and which balance weight restores them (the steal-weight volume finding, SIM-556). | [test_steal_weight_volume_probe.py](../../tests/unit/test_steal_weight_volume_probe.py) |
| [`wall_zone_probe.py`](../../scripts/probes/wall_zone_probe.py) (418) | The wall-play check: the drawn plays of born air balls near the live park's fence, short side and over side, against what the fence stage guarantees. | [test_fence_counters.py](../../tests/unit/test_fence_counters.py) |

**Watch out:** the profile recomputes write DuckDB, so they need the write lock: they cannot run while the nightly rebuild or another writer holds the file. The app holds no handle on the analytics file, so it does not block them.

---

## Input data files

| File | What it holds | Read by |
|---|---|---|
| [`data/balanced_game_set.json`](../../scripts/data/balanced_game_set.json) (355 lines) | The balanced certifying set: three dates, the games, the expectation table and the pool totals. | `conditional_rate_probe.py`, `fatigue_probe.py`, `manager_probe.py`, `running_game_census.py`, `wall_zone_probe.py`; written by `balanced_game_set.py` |
| [`data/games_2024_chunk2.txt`](../../scripts/data/games_2024_chunk2.txt), [`chunk3`](../../scripts/data/games_2024_chunk3.txt), [`chunk4`](../../scripts/data/games_2024_chunk4.txt) (250 lines each) | Games 251–1,000 of 2024 by game id, one `game_pk` per line. | `accuracy_baseline_1000.sh` |
| [`data/games_2024_design.txt`](../../scripts/data/games_2024_design.txt) (250 lines) | Games 1,001–1,250 of 2024 by game id. | `designed_experiment_run.sh` |
