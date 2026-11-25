# Play pools

The `pools/` folder builds the pools of real plays that the simulator draws from. A pool
is a DuckDB table in the `sim` schema. Each row is one real event, such as a pitch or a
ball in play. A draw keeps the rows that fit the live situation and picks one row by
weight. The folder also builds the intentional-walk rates and the plate-appearance
situations. It checks the pitch pool's labels against real plate appearances.

The six builders are mixins of `PlayerProfileComputor`. A mixin is a class that adds
methods to another class. The computor runs them after the profiles, in the nightly build
([Player profiles](profiles.md)). The bundle build then exports the pools into the files
the sampler reads ([The bundle](bundle.md)). The table layouts are on the
[database page](database.md#play-pools-sim). The two pool-rebuild scripts call these
builders by hand ([Scripts](scripts.md#the-pool-rebuilds)).

| Part | Folder or file |
|---|---|
| The recency weights, the incremental gate and the build records | [`pools/pool_metadata.py`](../../pools/pool_metadata.py) |
| The pitch pool, `sim.pitch_pool` | [`pools/build_pitch_pool.py`](../../pools/build_pitch_pool.py) |
| The batted-ball pool, `sim.outcome_pool` | [`pools/build_batted_ball_pool.py`](../../pools/build_batted_ball_pool.py) |
| The steal-opportunity pool and the stolen-base pool | [`pools/build_steal_pool.py`](../../pools/build_steal_pool.py) |
| The advancement pool, `sim.advancement_opportunity_pool` | [`pools/build_advancement_pool.py`](../../pools/build_advancement_pool.py) |
| The intentional-walk rates, `sim.ibb_rates` | [`pools/build_ibb_rates.py`](../../pools/build_ibb_rates.py) |
| The plate-appearance situations, `derived.at_bat_situations` | [`pools/build_at_bat_situations.py`](../../pools/build_at_bat_situations.py) |
| The count chain and the label check | [`pools/check_pool_labels.py`](../../pools/check_pool_labels.py) |

## The terms

| Term | Meaning |
|---|---|
| **Pool** | A table of real plays that one draw samples. The drawn row is the play: the simulator never adjusts it. |
| **Opportunity pool** | A pool with one row per chance to act, taken or not. The rows with no attempt give the draw its denominator. One draw then answers "does he go" and "safe or out" together. |
| **Pool window** | The seasons the bundle exports: the four newest seasons in `sim.pitch_pool` (2023–2026 today). The tables hold every season a run built. |
| **Clean pitch** | A `raw.pitches` row with `data_quality_flag = FALSE`. Every pool reads clean pitches only. |
| **Pitch class** | The pitch pool's `outcome_type`: `ball`, `called_strike`, `swinging_strike`, `foul`, `in_play` or `hit_by_pitch`. The simulator's count machine reads it. |
| **Cell** | One combination of the values a draw filters on exactly. The intentional-walk cell is the base state, the outs, a late inning and a close score. |
| **runners_state** | A number from 0 to 7 for the occupied bases: 1 for first, 2 for second, 4 for third, added together. |
| **Score margin** | `score_diff`: the batting side's lead in runs, clamped to −5 to +5. |
| **Got-away** | A pitch the catcher did not hold: a wild pitch, a passed ball or an uncaught third strike. |
| **Pickoff row** | A steal-opportunity row for a pickoff out or error with no pitch of its pair in the plate appearance. It sits at a 0-0 count. |
| **Recency weight** | The draw weight a row carries for the age of its season. Each row stores it in `recency_weight`. |
| **Reference season** | The season a recency weight counts age from: the newest season of the run, or of any earlier build record. |
| **Build record** | One row of `sim.pool_build_metadata` per pool and season. It holds the pool's row count, the source watermark, the source row count, the reference season and the builder version. |
| **Source watermark** | The newest `game_date` among a season's clean pitches, stored in the build record. |
| **Builder version** | `POOL_BUILDER_VERSION`, `sim554.1` today. Raise it with any formula change to a pool's SELECT. The gate then rebuilds every season it checks. |
| **Incremental rebuild** | A pool build that writes only the stale seasons. A season is stale when its watermark moved, its source row count changed or its builder version differs. |
| **Positional insert** | An `INSERT` with no column list. The SELECT's column order must match the table's order in `db/schemas/02_duckdb_schema.sql`. |
| **Count chain** | The walk through the twelve ball-strike counts, 0-0 to 3-2. At each count the pitch class moves the count or ends the plate appearance. Its solution is the rates a sampler produces when it draws each class at the pool's own share. |
| **Label check** | The comparison of the count chain with real plate appearances. The real side reads `events`, not the pool's classes, so a wrong class opens a gap. |

## How it fits together

**Which pool feeds which draw.** The numbers are the draw numbers of the
[loop cheat sheet](sim-loop-cheat-sheet.md).

| Table | Bundle files | The draw that samples it | Other readers |
|---|---|---|---|
| `sim.pitch_pool` | `pitch_pool/{L,R}.*`, one set per batter hand | ⑤a the pitch thrown (`draw`, `simulation/sampler/pitch.py`) and ⑤b its result (`_result_draw`, `simulation/sampler/pitch_result.py`) | the batted-ball and steal pool builds; the change opportunity pool (①); the catcher-receiving export (off); the pitch-to-pitch engine; the concentration check; the label check |
| `sim.outcome_pool` | `battedball_pool/{L,R}.*` | ⑥ the fielding draw (`battedball_draw`, `simulation/sampler/fielding.py`). The born ball of ⑤b is a row of this pool, found through `pitch_pool/{L,R}.bb_row.npy`. | the advancement pool build; the park geometry; the batted-ball engine; the concentration check |
| `sim.steal_opportunity_pool` | `steal_pool/{2,3}.*`, one set per target base | ④a the pickoff (`pickoff_draw`) and ④b the steal (`steal_draw`), in `simulation/sampler/steal.py` | `scripts/pool_window_census.py`, `scripts/running_game_replay.py`, the steal-pool rebuild's checks |
| `sim.advancement_opportunity_pool` | `advancement_pool/{key}.*`, one set per decision | ⑦ the extra bases (`advancement_draw`, `simulation/sampler/advancement.py`) | the outfield arm block of the fielder profile (`_fill_outfield_arm_block`) |
| `sim.ibb_rates` | `ibb_rates.json` | ③ the intentional walk (`_should_issue_ibb`, `simulation/loop/manager.py`) | none |
| `derived.at_bat_situations` | `manager_pool/change.*`, joined to the pitch pool | ① the pitching change (`pitching_change_draw`, `simulation/sampler/manager.py`) | the situation engine, which feeds no draw; `scripts/measure_filter_cells.py` |
| `sim.stolen_base_pool` | none | none | none |

**The build order.** `PlayerProfileComputor.run` builds the pools after the profiles. The
stolen-base pool copies runner and catcher metrics from the profile tables. The arm block
reads the advancement pool. So the order matters.

1. The `nightly-rebuild` job runs [`operations/jobs/nightly_rebuild.sh`](../../operations/jobs/nightly_rebuild.sh)
   at 08:00 UTC in the `jobs` container.
2. The script calls `python -m profiles.build_profiles --seasons ${YEAR}`: the current
   year only.
3. `PlayerProfileComputor.run` builds the profiles and the defensive metrics
   ([Player profiles](profiles.md)).
4. `_build_pitch_pool` writes the pitch pool from `pg.raw.pitches`.
5. `_build_outcome_pool` writes the batted-ball pool from the pitch pool's in-play rows.
6. `_build_stolen_base_pool` and `_build_steal_opportunity_pool` write the two steal
   pools. Both join the pitch pool.
7. `_build_advancement_opportunity_pool` writes the advancement pool from the batted-ball pool.
8. `_fill_outfield_arm_block` fills the fielders' arm columns from the advancement pool.
9. `_build_ibb_rates` rewrites the intentional-walk rates.
10. `_build_at_bat_situations` writes the plate-appearance situations.
11. The script then runs `python -m bundle.build_bundle --what all`, which exports the pools.

Steps 4 to 7 pass `incremental=True`, except on `--full-rebuild`. Steps 9 and 10 have no
gate. A full rebuild deletes the `derived.*` rows first (`_delete_seasons`). It leaves
the `sim.*` pools alone: each pool build deletes its own seasons.

No pool build takes a cutoff date, so a pool holds every play of its seasons. A backtest
drops the later plays at draw time, through each row's game date
(`FullPoolSampler.set_asof`).

**The incremental gate.** Each of the five pool builders starts with `_seasons_to_build`.

1. `_canonical_ref_season` picks the reference season from the full season list.
2. With `incremental`, `_seasons_needing_rebuild` reads the pool's build records.
3. For each season, `_source_state` scans the clean pitches of `pg.raw.pitches`. It
   returns the newest `game_date` and the row count.
4. A season is stale when it has no record or its date moved forward. A changed row
   count or builder version also makes it stale.
5. When no season is stale, the build logs "all seasons fresh" and skips.
6. Otherwise the build deletes the stale seasons' rows and inserts them again.
7. `_record_pool_build` writes one build record per season it built.

The row count catches a late game or a double-header on a date already seen. Such a row
does not move the newest date.

The build runs the DELETE and the INSERT as two statements, with no transaction around
them. A failed INSERT leaves the season with no rows until the next build. The nightly
script then stops before the bundle step, so the published bundle keeps its pools.

**The recency weights.** The weight is 2.0 for the reference season and the season
before. Each older season takes ×0.75, with a floor of 0.25. With 2026 as the reference
season, the pool window reads:

| Season | 2026 | 2025 | 2024 | 2023 |
|---|---|---|---|---|
| Weight | 2.0 | 2.0 | 1.5 | 1.125 |

The older seasons read 0.84, 0.63, 0.47, 0.36 and 0.27, then 0.25.

**The export into the bundle.** The bundle build opens the DuckDB file read-only. It takes
the pool window from `last_n_seasons` and writes one file set per pool. A worker reads
the pools from the bundle, never from the DuckDB file. The export copies each row's stored recency
weight. The pitch-pool and batted-ball exports keep DuckDB's stored row order, and each
pool build changes that order ([The bundle](bundle.md)).

**The two rebuild scripts.** `scripts/rebuild_pitch_pool.py` and
`scripts/rebuild_steal_pool.py` rebuild one pool over named seasons. Each script makes a
bare computor (`PlayerProfileComputor.__new__`) and gives it the connection. It calls the
builder with `incremental=False`. By default the build and the script's checks run in one
transaction, so a failed check rolls the build back. The pitch-pool script runs the label
check before it exports.

**Docker.** [docker-compose.yml](../../docker-compose.yml) mounts `./pools` into the
`app`, `live` and `jobs` services, and the image copies it too. The nightly job runs the
mounted copy at its next start, with no image rebuild.

---

## The package

### `pools/__init__.py`

[Source](../../pools/__init__.py) · 11 lines · Tests: none

The package docstring: one line per part, the step before (`profiles/`) and the steps
after (`similarity/`, `bundle/`). Import every name from its own module.

---

## The shared parts

### `pools/pool_metadata.py`

[Source](../../pools/pool_metadata.py) · 227 lines · Tests: [test_pool_recency_and_incremental_rebuild.py](../../tests/unit/pools/test_pool_recency_and_incremental_rebuild.py), [test_recency_walk_forward.py](../../tests/unit/pools/test_recency_walk_forward.py), [test_steal_pool_data.py](../../tests/unit/test_steal_pool_data.py), [test_steal_pool_rebuild.py](../../tests/unit/test_steal_pool_rebuild.py), [test_draw_conditioning.py](../../tests/unit/test_draw_conditioning.py), [test_hit_by_pitch.py](../../tests/unit/test_hit_by_pitch.py), [test_foul_tip_strike_three.py](../../tests/unit/test_foul_tip_strike_three.py), [test_park_factors.py](../../tests/unit/profiles/test_park_factors.py), [test_profile_data_additions.py](../../tests/unit/profiles/test_profile_data_additions.py)

The parts every pool builder shares: the builder version, the recency weights, the
incremental gate, the build records and two situation columns.

| Function / class | What it does | Called from |
|---|---|---|
| `POOL_BUILDER_VERSION` | The builder version, `sim554.1`. The gate treats a season built by another version as stale. | `_seasons_needing_rebuild`, `_record_pool_build`; `scripts/rebuild_pitch_pool.py`, `scripts/pitch_pool_rebuild/checks.py` and `preflight.py`, `scripts/steal_pool_rebuild/common.py` |
| `RECENCY_RECENT_SEASONS`, `RECENCY_DECAY`, `RECENCY_FLOOR`, `RECENCY_PEAK` | The recency constants: 2 seasons at the peak, ×0.75 a season, a floor of 0.25, a peak of 2.0. | `recency_weight`, `_recency_weight_sql` |
| `recency_weight` | The recency weight of a season against a reference season, in Python. | the tests: the oracle of `_recency_weight_sql` |
| `_recency_weight_sql` | The same formula as a SQL `CASE` over a season column. | the five pool builders |
| `_canonical_ref_season` | The reference season: the newest season asked for, or a newer `recency_ref_season` from any build record. With no build-record table, the newest season asked for. | `_seasons_to_build` |
| `_seasons_to_build` | Returns the seasons to write and the reference season, or None when every season is fresh. The reference reads the full list, before the gate narrows it. | the five pool builders |
| `_source_state` | `(MAX(game_date), COUNT(*))` of a season's clean pitches in `pg.raw.pitches`. | `_seasons_needing_rebuild`, `_record_pool_build`; `scripts/steal_pool_rebuild/preflight.py` |
| `_seasons_needing_rebuild` | The stale seasons of one pool. A missing build-record table makes every season stale. | `_seasons_to_build` |
| `_record_pool_build` | Inserts or replaces the build record of each season built. | the five pool builders |
| `read_pool_builds` / `pool_builder_versions` | One pool's build records per season; the builder version per season. | `scripts/rebuild_pitch_pool.py`, `scripts/rebuild_steal_pool.py`, `scripts/pitch_pool_rebuild/checks.py` and `preflight.py`, `scripts/steal_pool_rebuild/checks.py` and `preflight.py` |
| `sql_runners_state` / `sql_score_diff` | The SQL of `runners_state` and of the score margin. `q` prefixes the column names. | `sql_runners_state`: the pitch pool, the stolen-base pool and the intentional-walk rates. `sql_score_diff`: the pitch pool and the two steal pools. |

**Watch out:** each row stores its recency weight, and a run rewrites only the seasons it
builds. At the year turn, the nightly takes the new year as the reference season. It
rewrites only that season's rows. The older seasons keep the weights of the old
reference. 2025 keeps 2.0 where it should read 1.5. Each older season stays one step too
heavy. The bundle exports the stored weights (re-weight the older seasons' rows when
the year turns, SIM-650).

The gate checks only the seasons a run passes, and the nightly passes the current season.
A raised `POOL_BUILDER_VERSION` therefore rebuilds only the current season at night.
Rebuild the older seasons with `--seasons 2017 … 2026 --full-rebuild`. The watermark reads
`pg.raw.pitches` for every pool, never the pitch pool. So the gates of the pools built on
the pitch pool cannot see a rebuild of the pitch pool alone.

---

## The pool builders

Each builder is a method of a mixin that subclasses `ComputorBase` (`profiles/base.py`).
`self._conn` is the computor's DuckDB connection, with Postgres attached read-only as `pg`.

### `pools/build_pitch_pool.py`

[Source](../../pools/build_pitch_pool.py) · 173 lines · Tests: [test_uncaught_strike_three.py](../../tests/unit/test_uncaught_strike_three.py); through `PlayerProfileComputor`: [test_foul_tip_strike_three.py](../../tests/unit/test_foul_tip_strike_three.py), [test_hit_by_pitch.py](../../tests/unit/test_hit_by_pitch.py), [test_draw_conditioning.py](../../tests/unit/test_draw_conditioning.py), [test_park_factors.py](../../tests/unit/profiles/test_park_factors.py)

`PitchPoolMixin` writes `sim.pitch_pool`: one row per clean pitch. The pitch draw and
the pitch-result draw sample it. The batted-ball pool and the two steal pools read it, so
it builds first.

| Function / class | What it does | Called from |
|---|---|---|
| `PitchPoolMixin._build_pitch_pool` | Deletes the seasons' rows, then inserts one row per clean pitch. Each row adds the `pitch_id` key: the game, the at-bat and the pitch number. It adds `runners_state`, the score margin and the pitch class. It adds the previous pitch's speed, movement and result code. It adds the recency weight, the catcher (`fielder_2`) and the got-away label. Last come the batting side (`bat_home`) and the two fatigue columns. | `PlayerProfileComputor.run`; `scripts/rebuild_pitch_pool.py` |
| `SQL_GOT_AWAY` | The got-away label: the parser's wild-pitch or passed-ball flag, or a strikeout whose description names a wild pitch or a passed ball. | `_build_pitch_pool`; `scripts/pitch_pool_rebuild/common.py` |

The build takes three expressions from `profiles/sql.py`. `SQL_OUTCOME_TYPE` gives the
pitch class: a hit by pitch first, then the `type` code. A two-strike foul tip or foul
bunt reads `swinging_strike`, strike three. `SQL_PITCHER_PITCH_COUNT` and
`SQL_TIMES_THROUGH_ORDER` give the fatigue columns: the pitcher's pitches before the plate
appearance, and his times through the order.

**Flags:** none in the build. The loop reads the got-away label under `SIM_GOT_AWAY`
(default 0, production 1). It reads the times through the order under
`SIM_FATIGUE_TTO_SIGMA` (default 0, production 0.5).

**Watch out:** the INSERT is positional. A new column goes last, in the DDL and in the
SELECT together. `SQL_OUTCOME_TYPE` has no `ELSE`, so an unknown `type` code yields NULL.
The NOT NULL `outcome_type` column then refuses the insert. The `stand` column is the
side the batter hit from in this plate appearance, never `S`. Never key on `bat_hand`:
it is the declared side, `S` for a switch hitter. The fatigue columns count clean pitches
only, because the WHERE runs before the window.

The insert reads `raw.pitches` with no ORDER BY, so each build stores the rows in a new
order. The pitch-result draw lowers the weight of a pitch in a crowded region. It
measures the crowding against reference rows that it picks by position. So its weights
change between pool builds ([The bundle](bundle.md); export the pitch pool in pitch
order, SIM-635).

### `pools/build_batted_ball_pool.py`

[Source](../../pools/build_batted_ball_pool.py) · 248 lines · Tests: through `PlayerProfileComputor`: [test_transition_pool.py](../../tests/unit/test_transition_pool.py), [test_pool_pull_relative_spray.py](../../tests/unit/pools/test_pool_pull_relative_spray.py), [test_etl_reload_game.py](../../tests/unit/ingest/test_etl_reload_game.py), [test_park_factors.py](../../tests/unit/profiles/test_park_factors.py)

`BattedBallPoolMixin` writes `sim.outcome_pool`: one row per ball in play, with the whole
base-out transition the play made. The fielding draw samples it.

| Function / class | What it does | Called from |
|---|---|---|
| `BattedBallPoolMixin._build_outcome_pool` | Reads the in-play rows of `sim.pitch_pool` and joins each to its `pg.raw.pitches` row. It adds the batted ball and the positions that fielded it and made an error. It adds the hits, the outs, the runs and the recency weight. It adds the venue, the fielder and the runners before and after. It adds each runner's and the batter's destination, and the outs check. Last come the batting side, the eight fielders and the putout and assist position masks. | `PlayerProfileComputor.run` |

The batted ball holds the exit velocity, the launch angle and two spray angles: the raw
one and the pull-relative one. It also holds the batted-ball type, the distance and the
landing point. The outs come from `events` through `sql_play_outs`
(`ingest/statcast_events.py`). The destinations come from `sql_runner_dest` and
`sql_batter_dest`. NULL is no runner, 0 is retired, 1 to 3 is the base after the play, and
4 is scored. `dest_outs_consistent` is TRUE when the retired bodies equal the play's outs.
The masks come from `sql_credit_mask` (`profiles/sql.py`).

**Watch out:** the INSERT is positional. The outer SELECT moves `bat_home` and the
fielding chain to the end with `EXCLUDE`, to match the DDL's order. A new column goes
after `assist_pos_mask`. The pull-relative spray angle keys on `stand`, and a negative
value is the pull side for both hands. The build must run after `_build_pitch_pool` in
the same run. The export keeps the rows whose `dest_outs_consistent` is FALSE; the
fielding draw's cell index drops them.

The insert has no ORDER BY, so each build stores the rows in a new order. The fielding
draw lowers the weight of a ball in a crowded region. It measures the crowding against
reference rows that it picks by position. So its weights change between pool builds
([The bundle](bundle.md); export the batted-ball pool in pitch order, SIM-636).

### `pools/build_steal_pool.py`

[Source](../../pools/build_steal_pool.py) · 498 lines · Tests: through `PlayerProfileComputor`: [test_steal_pool_data.py](../../tests/unit/test_steal_pool_data.py), [test_steal_pool_rebuild.py](../../tests/unit/test_steal_pool_rebuild.py), [test_steal_draw.py](../../tests/unit/test_steal_draw.py)

`StealPoolMixin` writes two tables. `sim.steal_opportunity_pool` holds one row per pitch
where a steal was possible, attempted or not, plus the pickoff rows. The pickoff draw and
the steal draw sample it. `sim.stolen_base_pool` holds the steal attempts alone.

An opportunity is the lead runner and the open base ahead of him. A runner on first with
second open targets base 2. A runner on second with third open targets base 3, whether or
not first is taken. The outs are 0 to 2. Steals of home are out of scope.

| Function / class | What it does | Called from |
|---|---|---|
| `StealPoolMixin._build_steal_opportunity_pool` | One row per opportunity pitch. The row holds the runner, the pitcher, the catcher, the target base and the situation. It holds `attempted`, `success`, the recency weight, three pickoff flags and the pitch class (`pitch_class`). The build tags each pickoff outcome on one pitch of its pair, or makes a pickoff row. | `PlayerProfileComputor.run`; `scripts/rebuild_steal_pool.py` |
| `StealPoolMixin._pickoff_outcomes_cte` | The SQL of each pickoff throw in `raw.play_events`, mapped to the pair of its held runner. An out at the next base is a picked-off caught stealing (`is_adv_i`). The relation is empty when `pg.raw.play_events` is absent. | `_build_steal_opportunity_pool` |
| `StealPoolMixin._build_stolen_base_pool` | One row per steal attempt of second, third or home. It copies the runner's sprint speed and steal success rate, and the catcher's pop time and arm strength. The pitcher's metric is NULL. | `PlayerProfileComputor.run` |
| `_require_steal_pool_0031` | Raises `RuntimeError` before the delete when the table lacks the migration-0031 columns, so no row is lost. | `_build_steal_opportunity_pool` |
| `STEAL_POOL_0031_COLUMNS` / `MIGRATION_0031` | The two columns migration 0031 adds (`pitch_class`, `is_pickoff_row`), and its file. | `scripts/steal_pool_rebuild/checks.py`, `migration.py`, `common.py` |

The attempt and success labels are `sql_steal_attempt` and `sql_steal_success`
(`ingest/statcast_events.py`). They read the steal columns or `events`, because a caught
stealing that ends the plate appearance lives only in `events`. A strikeout double play
counts as attempted and never as a success.

A pickoff outcome is an out or an error. When its plate appearance holds a pitch of its
pair, the outcome tags the first pitch with no attempt. An out beats an error. Otherwise
the outcome becomes a pickoff row, with `is_pickoff_row` TRUE. The row's `pitch_id` is
the negative of the play record's id. Its `pitch_number` is 0, its count 0-0 and its
`pitch_class` NULL. A pickoff row is never attempted. Its catcher comes from the nearest
pitch of the same half-inning. On a tie, the earlier plate appearance wins. With no such
pitch, the catcher is NULL.

**Flags:** none in the build. `SIM_STEAL_PITCH_CLASS` (default 1) makes the loop draw the
steal after the pitch, among the rows of the drawn pitch's class. At 0, or on a bundle
with an unclassed pitch row, the loop runs one steal draw before the pitch.

**Watch out:** the nightly job applies no DuckDB migration. Apply migration 0031 only
through `scripts/rebuild_steal_pool.py --apply-migration`. The INSERT is positional, and
the builder refuses an un-migrated table before it deletes anything. The pickoff rows
sit at 0-0, so the simulator's pickoffs happen mostly at 0-0. The build drops the throws
that fit no pair, about 0.006 a team-game. They are a runner held at third, an out at
home, and a pickoff at first with second base taken.

The computor builds `sim.stolen_base_pool` every night, and nothing reads it. No draw,
export or script uses it (delete or wire up the data nobody reads, SIM-631).

### `pools/build_advancement_pool.py`

[Source](../../pools/build_advancement_pool.py) · 190 lines · Tests: through `PlayerProfileComputor`: [test_transition_pool.py](../../tests/unit/test_transition_pool.py); the run order: [test_outfield_arm_block.py](../../tests/unit/profiles/test_outfield_arm_block.py), [test_point_in_time_runner_catcher_fielder.py](../../tests/unit/profiles/test_point_in_time_runner_catcher_fielder.py)

`AdvancementPoolMixin` writes `sim.advancement_opportunity_pool`: one row per runner
decision on a ball in play, attempted or not. A decision is a runner's choice to try for
an extra base. The advancement draw samples the pool, one set per decision.

| Function / class | What it does | Called from |
|---|---|---|
| `AdvancementPoolMixin._build_advancement_opportunity_pool` | Builds one SELECT per decision over `sim.outcome_pool` and joins them with `UNION ALL`. Each row holds the runner, the fielder and his position, the outs and the batted ball. It holds `attempted`, `safe`, `error_extra` and the recency weight. | `PlayerProfileComputor.run` |

| Scenario | Runner, from and to | The play |
|---|---|---|
| 1 | first to third | a single |
| 2 | second to home | a single |
| 3 | first to home | a double |
| 4 | third to home; second to third; first to second | a tag-up: the batter out on a fly ball, popup or line drive with fewer than two outs |
| 5 | the batter to second; the batter to third | a single; a double |

`attempted` is true when the runner reached the target or the defence threw him out advancing.
`safe` is true when he reached it. `error_extra` is true when he went past the target,
an extra base on a bad throw. A runner doubled off on a tag-up had no decision, so his
row leaves the pool.

**Watch out:** the INSERT is positional. The build reads `sim.outcome_pool`, so it runs
after `_build_outcome_pool`. `_fill_outfield_arm_block` reads this pool, so it runs after
this build; the two run-order tests pin both. The build drops the rows with no batter
destination and the rows whose `dest_outs_consistent` is not TRUE. The loop limits these
five movements in the drawn fielding row to station to station. Each runner then moves
only as many bases as the hit, so no extra base counts twice.

### `pools/build_ibb_rates.py`

[Source](../../pools/build_ibb_rates.py) · 98 lines · Tests: through `PlayerProfileComputor`: [test_ibb_rates.py](../../tests/unit/pools/test_ibb_rates.py)

`IbbRatesMixin` writes `sim.ibb_rates`: the real intentional-walk rate of each cell. The
cell is `runners_state`, the outs, a late inning and a close score. A late inning is the
seventh or later. A close score is a margin of one run or less. The table holds at most
96 cells. The loop rolls once per
plate appearance, on its first pitch, against `issued / opportunities`. No similarity
weight applies.

| Function / class | What it does | Called from |
|---|---|---|
| `IbbRatesMixin._build_ibb_rates` | Deletes the whole table and rebuilds it from the run's seasons. The numerator counts the `intent_walk` rows of `raw.play_events`. The denominator counts the pitched plate appearances, at their first pitch, and the intentional walks with no pitch. `GREATEST` keeps each rate at 1 or less. | `PlayerProfileComputor.run` |

The table has no season column, no build record and no recency weight.

**Flags:** `SIM_MANAGER` (default 0, production 1). The loop reads the rates only with a
manager wired.

**Watch out:** the build reads only the run's seasons, and the nightly passes the current
season. So the table holds one season, not the pool window. The first nightly of a new
year finds no games, and the table empties. An empty table loads as None. Then no
intentional walk fires in any game (build the intentional-walk rates over the pool window
every night, SIM-649). The docstring of `_should_issue_ibb` says the table covers the
pool window. It does not. The table has no
game date, so the backtest cutoff does not apply to it.

### `pools/build_at_bat_situations.py`

[Source](../../pools/build_at_bat_situations.py) · 90 lines · Tests: none

`AtBatSituationsMixin` writes `derived.at_bat_situations`: one row per plate appearance,
with the state at its first pitch. The table lives in `derived`, but the computor builds
it with the pools. The change opportunity pool joins it to the pitch pool for the
pitching-change draw.

| Function / class | What it does | Called from |
|---|---|---|
| `AtBatSituationsMixin._build_at_bat_situations` | Writes the inning, the half, the outs, the three bases, the score and a leverage index. It also writes the pitcher's pitches before the plate appearance and the batter's plate-appearance number in the game. `INSERT OR REPLACE` on `(game_pk, at_bat_number)` makes a rerun safe. | `PlayerProfileComputor.run` |

The leverage index is a product of four factors. The inning gives 0.6, 0.9, 1.3 or 1.5.
The margin gives 1.5 − 0.24 per run, capped at five runs, with a floor of 0.3. Each runner
adds 0.15 to 1. Two outs give 1.1, else 1.0.

**Watch out:** the leverage index is a fixed product, not a fit. Only the situation
engine reads it, and that engine feeds no draw. The build has no incremental gate, so
every run rewrites the run's seasons. A full rebuild clears the table first
(`_delete_seasons`). The situation engine refuses to build on a missing or empty table.

---

## The label check

### `pools/check_pool_labels.py`

[Source](../../pools/check_pool_labels.py) · 481 lines · Tests: [test_pool_chain.py](../../tests/unit/pools/test_pool_chain.py), [test_pool_label_check.py](../../tests/acceptance/test_pool_label_check.py)

The count chain of the pitch pool, and the label check against real plate appearances.
The acceptance bands grade the simulator against the chain of the pool's own labels. A
wrong label moves that centre with the simulator, so the band reads green. The label
check counts the same rates from real plate appearances in `raw.pitches`, from `events`.
The two sides share no label, so a coding defect opens a gap. The tolerance is 0.5%.

The two sides count the same plate appearances. A plate appearance is a
`(game_pk, at_bat_number)` group with a batting event in `events`. The label check reads
the chain with `pa_only=True`: only the pool rows of such groups. The band centres keep
`pa_only=False`, because the simulator draws every pool row.

| Function / class | What it does | Called from |
|---|---|---|
| `solve_chain` | Solves the count chain from 0-0 in closed form. It returns how the plate appearances end: the walk, strikeout, in-play and hit-by-pitch shares. It also returns the expected pitches. A two-strike count of fouls only raises `ValueError`. | `chain_rates`; `scripts/pool_window_census.py` |
| `pool_count_matrix` | The pool's class volume at each count: 12 rows by 6 classes, in rows or in summed recency weight. `pa_only` keeps the rows of real plate appearances. It raises on an unknown class, a count outside the twelve, or a count with no volume. | `chain_rates`; `scripts/pool_window_census.py` |
| `chain_rates` | `solve_chain` over `pool_count_matrix`. | `run_label_check`; `scripts/pitch_pool_rebuild/checks.py`; the acceptance test |
| `real_pa_rates` | The strikeouts, walks, hit by pitch and pitches per plate appearance in `pg.raw.pitches`, and `n_pa`. It needs `db.dsn.attach_pg` first. | `run_label_check` |
| `pa_group_count` | The plate appearances the `pa_only` chain reads. It must equal `n_pa`. | `run_label_check`; the acceptance test |
| `label_check` | One row per channel: the chain value, the real value, the relative gap and whether the gap is inside the tolerance. | `run_label_check`; `scripts/pool_window_census.py`; the acceptance test |
| `run_label_check` / `LabelCheckRun` | One run of the check. `ok` holds when the two counts are equal and every channel is inside the tolerance. | `scripts/pitch_pool_rebuild/checks.py` (`_check_label`), `scripts/pool_window_census.py`, the acceptance test |
| `format_label_check` / `format_pa_count` / `format_gaps` | The printed lines: one per channel, the count line, and the gaps on one line. | the same three callers; `format_gaps` only the census and the acceptance test |
| `LABEL_CHECK_TOLERANCE` / `LABEL_CHANNELS` | The tolerance, 0.005; the channels `k`, `walk`, `hbp` and `pitches`. | `label_check`; the tolerance also `scripts/pitch_pool_rebuild/checks.py`, `scripts/pool_window_census.py` and the acceptance test |
| `_sql_pa_flag` / `_sql_pa_keys` | The one group test of a plate appearance, and the SQL of the groups it keeps. Both sides use it, so they cannot disagree on what a plate appearance is. | `real_pa_rates`, `pool_count_matrix`, `pa_group_count` |

**Watch out:** the nightly build does not run the check. The pitch-pool rebuild script,
the census and the acceptance test run it. The check cannot see a swap between a called
and a swinging strike. Nor can it see a swap between two in-play kinds: the count machine
treats each pair alike. The truth tables in
[test_foul_tip_strike_three.py](../../tests/unit/test_foul_tip_strike_three.py) guard
those. The pitch clock's automatic balls have no pitch row, so the chain's walks read a
few tenths of a percent low. That gap is not a label error. A new runner event in
`events` counts as a plate appearance until you add it to `_NON_PA_EVENTS`. A FAIL is a
coding defect to find, never a reason to widen the tolerance.
