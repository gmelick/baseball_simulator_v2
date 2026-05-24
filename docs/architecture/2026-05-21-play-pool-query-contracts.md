# Play-Pool Query Column Contracts (Phase 3 / Phase 4)

*Ticket: SIM-111 · Owner: Backend Developer + Data Engineer · Date: 2026-05-21*
*Status: ACTIVE — authoritative column contract for the play-pool query API
(the `PlayPoolSampler`, SIM-302, and the Phase 4 sim loop, SIM-303). This is the
reference SIM-115 (index pruning) acts on and the reference any future change to
`sim.pitch_pool` / `sim.outcome_pool` / `sim.stolen_base_pool` must update.*

---

## 0. Purpose

SIM-300 (`docs/architecture/2026-05-20-play-pool.md`) defined the **build/read
architecture** of the play pool — tiling, the FAISS sidecars, the recency
lifecycle, the sampler API surface. What it did *not* pin down is the
**column-level contract**: exactly which columns of the three `sim.*` pool tables
the query path reads, in what role, and with what guarantees.

This document formalizes that contract so:

1. Downstream code (the SIM-302 sampler, the SIM-303 sim loop, the SIM-220
   backtester) has an authoritative list of the columns it may rely on and the
   shapes it queries them with.
2. The index-pruning work (**SIM-115**) has a concrete, query-traced input for
   deciding which of the indexes declared in `db/schemas/02_duckdb_schema.sql`
   are *served* (keep) vs *dead weight* (drop) — see §6.
3. Schema changes to a load-bearing column are caught: any change requires a
   migration **and** a bump to this doc (§7).

It is deliberately scoped to the **query path**. How the pools are *populated*
(the nightly ETL out of PostgreSQL `raw.pitches` / `derived.*`) is owned by the
ingest tickets and is not re-specified here.

The three pools:

| Pool | DDL (`02_duckdb_schema.sql`) | Query consumers | FAISS-tiled in Phase 3? |
|---|---|---|---|
| `sim.pitch_pool` | ~L632 | SIM-301 builder (fingerprint read), SIM-302 sampler (outcome fetch), sim loop Step 2 | **Yes** — pitch tiles |
| `sim.outcome_pool` | ~L723 | SIM-301 builder (launch read), SIM-302 sampler (event fetch), sim loop Steps 3b/4 | **Yes** — batted-ball tiles |
| `sim.stolen_base_pool` | ~L804 | sim loop Step 1 / Step 3a (steal decision + outcome) | **No** — RBF/direct, not disk-tiled (SIM-300 §9) |

`sim.pool_build_metadata` (~L867) is build-side bookkeeping, not a query-path
table; it is summarized in §5 for completeness only.

---

## 1. Column-role vocabulary

Every column below is tagged with exactly one **role**, the lens SIM-115 uses to
reason about indexing:

- **PK** — primary key. The point-lookup key. `pitch_id` for all three pools.
- **pre-filter key** — a column the query path filters on *before* any FAISS
  search, to scope a tile / candidate set. These are the indexes SIM-115 must
  keep (§6).
- **feature / fingerprint** — a column that becomes a dimension of the FAISS
  query vector (read in bulk at build time; never filtered on).
- **situation** — game-state context (count / outs / runners / inning / score).
  Read into the situation engine (KDTree, SIM-070) and into the Phase 4 state
  machine; **the situation *is* the query**, so these are not pre-filter keys for
  the FAISS pools (SIM-300 §3).
- **outcome / result** — the historical result a sampled row maps back to (the
  payload the sampler fetches by `pitch_id`).
- **metadata** — provenance / sampling weights (`recency_weight`, `game_date`,
  source-ref columns).

A column can be load-bearing for the query path in more than one consumer; the
role tag reflects its **primary** function in the query API.

---

## 2. `sim.pitch_pool` — column contract

Pre-filter keys: **`pitcher_id` + `stand`** (pitcher identity × batter
handedness — SIM-300 §3). PK: `pitch_id`.

| Column | Type | Role | Semantics / query use |
|---|---|---|---|
| **`pitch_id`** | BIGINT | **PK** | Surrogate id. The value stored in `.rowids.npy`; the **point-lookup key** the sampler fetches the outcome by (`WHERE pitch_id IN (...)`). |
| `game_pk` | INTEGER | metadata | Source game ref. Not read by the query path. |
| `at_bat_number` | INTEGER | metadata | Source PA ref. Not read by the query path. |
| `pitch_number` | INTEGER | metadata | Source pitch ref. Not read by the query path. |
| `game_date` | DATE | metadata | Freshness **watermark** source: the builder uses `strftime(MAX(game_date))` as the per-tile staleness watermark when no `updated_at` column exists (SIM-301 deviation note; `WATERMARK_COLUMN_CANDIDATES`). |
| `season` | SMALLINT | pre-filter key (tiling) + metadata | Tiling dimension (one tile set per season) **and** the recency-boost key (`_apply_recency_boost` reads `season` per row). Read in every build query (`SELECT pitch_id, season, ...`). |
| **`pitcher_id`** | INTEGER | **pre-filter key** | Pitch geometry is a property of who threw it. First half of the pitch tile key; filtered on in `_fetch_pitch_group` (`AND pitcher_id IN (...)`). |
| `p_throws` | VARCHAR(1) | situation | Pitcher hand. Available for matchup features; not a pre-filter key for the pitch tile. |
| `batter_id` | INTEGER | situation | Batter identity. Not a pitch-pool pre-filter key. |
| **`stand`** | VARCHAR(1) | **pre-filter key** | Batter handedness (`L`/`R`; switch hitters resolved per-PA at ETL). Second half of the pitch tile key. Builder selects `bat_hand` if present else `stand` (`BAT_HAND_COLUMN_CANDIDATES`); production DDL spells it `stand`. No `S` tile. |
| `velo` | FLOAT | **fingerprint** | release_speed. Fingerprint dim 1. |
| `ivb` | FLOAT | **fingerprint** | Induced vertical break (gravity removed). Fingerprint dim 2. |
| `hb` | FLOAT | **fingerprint** | Horizontal break. Fingerprint dim 3. |
| `spin_rate` | FLOAT | **fingerprint** | Fingerprint dim 4. |
| `spin_axis` | FLOAT | **fingerprint** | Fingerprint dim 5. |
| `release_x` | FLOAT | **fingerprint** | release_pos_x. Fingerprint dim 6. |
| `release_z` | FLOAT | **fingerprint** | release_pos_z. Fingerprint dim 7. |
| `release_ext` | FLOAT | **fingerprint** | release_extension. Fingerprint dim 8. |
| `plate_x` | FLOAT | **fingerprint** | Fingerprint dim 9. |
| `plate_z` | FLOAT | **fingerprint** | Fingerprint dim 10. |
| `zone` | SMALLINT | feature | Statcast zone. Available; not in the 10-dim fingerprint. |
| `count_balls` | SMALLINT | situation | Ball count. State-machine / situation-engine input. |
| `count_strikes` | SMALLINT | situation | Strike count. State-machine / situation-engine input. |
| `outs` | SMALLINT | situation | Outs (0–2). |
| `runners_state` | SMALLINT | situation | Bitmask bit0=1B, bit1=2B, bit2=3B (0–7). Baserunner configuration. |
| `inning` | SMALLINT | situation | |
| `score_diff` | SMALLINT | situation | bat_score − fld_score. |
| `prev_pitch_velo` | FLOAT | feature | Sequence-model context. Not in the 10-dim fingerprint. |
| `prev_pitch_ivb` | FLOAT | feature | Sequence-model context. |
| `prev_pitch_hb` | FLOAT | feature | Sequence-model context. |
| `prev_pitch_outcome` | VARCHAR(5) | feature | Prior pitch result (B/S/X/C/F). Sequence context. |
| **`outcome_type`** | VARCHAR(20) | **outcome** | Terminal pitch classification (`ball`, `called_strike`, `swinging_strike`, `foul`, `in_play`). The payload `sample_pitch` returns as `pitch_outcome` (`PITCH_OUTCOME_COLUMN`). |
| `events` | VARCHAR(50) | outcome | PA event; non-NULL when `outcome_type = in_play`. (Primary event read is in the outcome pool; mirrored here.) |
| **`recency_weight`** | FLOAT | **metadata** | SIM-076 sampling weight (§4). NOT NULL DEFAULT 1.0. |

**Build read shape** (`_fetch_pitch_group`):
`SELECT pitch_id, season, velo, ivb, hb, spin_rate, spin_axis, release_x,
release_z, release_ext, plate_x, plate_z FROM sim.pitch_pool WHERE season = ?
AND stand = ? AND pitcher_id IN (...)`.

**Sampler outcome-fetch shape** (`_duckdb_fetch`):
`SELECT pitch_id, outcome_type FROM sim.pitch_pool WHERE pitch_id IN (...)`.

---

## 3. `sim.outcome_pool` — column contract

In-play subset of `pitch_pool` with full batted-ball detail. Pre-filter key:
**`stand` only** (batter-side launch geometry — SIM-300 §3). PK: `pitch_id`.
The columns duplicated from `pitch_pool` carry the same semantics and roles as
§2; only the role-relevant and batted-ball-specific columns are detailed here.

| Column | Type | Role | Semantics / query use |
|---|---|---|---|
| **`pitch_id`** | BIGINT | **PK** | Point-lookup key for the event payload (`.rowids.npy` value). |
| `game_date` | DATE | metadata | Watermark source (same as §2). |
| `season` | SMALLINT | pre-filter key (tiling) + metadata | Tiling + recency-boost key; read in the build query. |
| `pitcher_id` | INTEGER | feature | Carried for self-contained queries; **not** a batted-ball pre-filter key. |
| **`stand`** | VARCHAR(1) | **pre-filter key** | The *sole* pre-filter for the batted-ball tile. `bat_hand`/`stand` candidate selection as in §2. |
| `velo` … `plate_z`, `zone` | FLOAT/SMALLINT | feature | Pitch features carried for joins/analysis; not in the 3-dim batted-ball fingerprint. |
| `count_balls`, `count_strikes`, `outs`, `runners_state`, `inning`, `score_diff` | SMALLINT | situation | State context (same as §2). |
| `events` | VARCHAR(50) | **outcome** | The PA event (`single`, `double`, `field_out`, …). The payload `sample_batted_ball` returns as `event` (`BATTEDBALL_OUTCOME_COLUMN`); also the group-by key for `return_distribution=True`. |
| **`exit_velo`** | FLOAT | **fingerprint** | launch_speed. Batted-ball dim 1. Build query requires `IS NOT NULL`. |
| **`launch_angle`** | FLOAT | **fingerprint** | Batted-ball dim 2. Build query requires `IS NOT NULL`. |
| **`pull_relative_spray_angle`** | FLOAT | **fingerprint** (preferred) | SIM-051 handedness-corrected spray. Batted-ball dim 3 when present. NULL for unresolved switch hitters (`bat_hand = 'S'`). |
| `spray_angle` | FLOAT | **fingerprint** (fall-back) | Raw Statcast spray. Used as dim 3 only when `pull_relative_spray_angle` is absent (`_select_spray_column()`, SIM-042 / SIM-300 §3.1). |
| `bb_type` | VARCHAR(20) | outcome | ground_ball/fly_ball/line_drive/popup. Step 3b/4 input; not in the fingerprint. |
| `hit_distance` | FLOAT | outcome | Step 4 input. |
| `hc_x`, `hc_y` | FLOAT | outcome | Hit-coordinate detail; Step 4 fielding input. |
| `fielded_by_position` | SMALLINT | outcome | 1–9 fielder. Step 4. |
| `fielding_error_position` | SMALLINT | outcome | Error position; NULL = clean. Step 4. |
| `result_hits` | SMALLINT | outcome | 0–4 (0=out,1=1B,2=2B,3=3B,4=HR). NOT NULL DEFAULT 0. |
| `result_outs` | SMALLINT | outcome | NOT NULL DEFAULT 0. |
| `result_runs` | SMALLINT | outcome | NOT NULL DEFAULT 0. |
| **`recency_weight`** | FLOAT | **metadata** | SIM-076 (§4). NOT NULL DEFAULT 1.0. |

**Build read shape** (`_build_battedball_payload`):
`SELECT pitch_id, season, exit_velo, launch_angle, <spray_col> AS spray FROM
sim.outcome_pool WHERE season = ? AND stand = ? AND exit_velo IS NOT NULL AND
launch_angle IS NOT NULL AND <spray_col> IS NOT NULL`.

**Sampler outcome-fetch shape** (`_duckdb_fetch`):
`SELECT pitch_id, events FROM sim.outcome_pool WHERE pitch_id IN (...)`.

---

## 4. `sim.stolen_base_pool` — column contract

All pitches where a steal was attempted. **Not FAISS-tiled in Phase 3**
(SIM-300 §9): it feeds the sim loop's Step 1 (steal decision) and Step 3a (steal
outcome) directly. Its query contract is documented here so SIM-115 can index it
for the *situational + participant* lookups the sim loop will issue, not for FAISS
tiles. Pre-filter keys (query path): **`runner_id`, `catcher_id`, `pitcher_id`**
(the participant identities a steal is scored on); situation is the query.
PK: `pitch_id`.

| Column | Type | Role | Semantics / query use |
|---|---|---|---|
| **`pitch_id`** | BIGINT | **PK** | Point-lookup key. |
| `game_pk`, `at_bat_number`, `pitch_number` | INTEGER | metadata | Source refs. |
| `game_date` | DATE | metadata | Watermark / recency source. |
| `season` | SMALLINT | pre-filter key + metadata | Season filter + recency key. |
| **`runner_id`** | INTEGER | **pre-filter key** | The base-stealer. Primary participant filter for the steal-decision lookup. |
| **`pitcher_id`** | INTEGER | **pre-filter key** | Pitcher (hold-runner ability). |
| **`catcher_id`** | INTEGER | **pre-filter key** | Catcher (fielder_2; pop time / arm). |
| `base_attempted` | VARCHAR(5) | situation | `'2B'`/`'3B'`/`'home'` (CHECK-constrained). Filter for which base. |
| `inning`, `outs`, `runners_state`, `score_diff`, `count_balls`, `count_strikes` | SMALLINT | situation | Steal-context state. |
| `velo`, `ivb` | FLOAT | feature | Pitch characteristics at the steal. |
| `runner_sprint_speed` | FLOAT | feature | Denormalized from `derived.baserunner_season_metrics` (avoids a join). |
| `runner_sb_success_rate` | FLOAT | feature | Denormalized runner metric. |
| `catcher_pop_time_mean` | FLOAT | feature | Denormalized catcher metric. |
| `catcher_arm_strength` | FLOAT | feature | Denormalized catcher metric. |
| `pitcher_sb_allowed_rate` | FLOAT | feature | Denormalized pitcher metric. |
| **`success`** | BOOLEAN | **outcome** | Whether the steal succeeded. The result the sim loop samples. NOT NULL. |
| **`recency_weight`** | FLOAT | **metadata** | SIM-076 (§4). NOT NULL DEFAULT 1.0. |

**Query shapes (sim loop, Phase 4):** point lookup by `pitch_id`; pre-filtered
candidate scan by `runner_id` (and/or `catcher_id` / `pitcher_id`) optionally
narrowed by `base_attempted` and `season`.

---

## 5. `sim.pool_build_metadata` — build-side, not a query-path table

One row per `(pool_name, season)` recording the last build (row count, source
watermark `source_max_game_date`, `recency_ref_season`, `builder_version`,
`built_at`). It drives the incremental rebuild (SIM-095) and records the recency
reference season used (§4). PK `(pool_name, season)`. The **query API does not
read it** — it is consumed by the build job and by ops. Listed here only so a
reader of this contract knows it exists and is out of scope for SIM-115 query
indexing.

---

## 6. Access patterns ↔ index map (the SIM-115 input)

This is the section SIM-115 acts on. Each row ties a real query the sampler /
sim loop issues to the index that should serve it. Indexes named are the ones
declared in `db/schemas/02_duckdb_schema.sql`.

### 6.1 The actual query shapes

| # | Consumer | Pool | Shape (WHERE / SELECT) | Serving access path |
|---|---|---|---|---|
| A | SIM-302 `_duckdb_fetch` (pitch payload) | `pitch_pool` | `SELECT pitch_id, outcome_type WHERE pitch_id IN (...)` | **PK** point lookup |
| B | SIM-302 `_duckdb_fetch` (event payload) | `outcome_pool` | `SELECT pitch_id, events WHERE pitch_id IN (...)` | **PK** point lookup |
| C | SIM-301 `_fetch_pitch_group` | `pitch_pool` | `WHERE season = ? AND stand = ? AND pitcher_id IN (...)` | composite on `(pitcher_id, season)` + `stand` |
| D | SIM-301 `_plan_pitch_tiles` | `pitch_pool` | `GROUP BY season, pitcher_id, stand` | scan + group (no point index) |
| E | SIM-301 `_build_battedball_payload` | `outcome_pool` | `WHERE season = ? AND stand = ?` (+ NOT NULL launch cols) | `season` + `stand` |
| F | SIM-301 `_plan_battedball_tiles` | `outcome_pool` | `GROUP BY season, stand` | scan + group |
| G | Phase 4 sim loop (steal decision) | `stolen_base_pool` | `WHERE runner_id = ?` (± `base_attempted`, `season`) | `runner_id` |
| H | Phase 4 situation engine (SIM-070) | `pitch_pool` | situation columns read in bulk into the in-memory KDTree | **bulk scan — no index**; situation is the query |

### 6.2 Recommended keep / drop, per pool

SIM-115 should **keep** an index iff it serves a pre-filter key or PK shape above,
and **drop** indexes that exist only to serve single-feature predicates the query
path never issues (the fingerprint/feature columns are read in *bulk* inside an
already-`stand`/`pitcher_id`-scoped scan — never filtered on individually).

**`sim.pitch_pool`**

| Index | Decision | Reason |
|---|---|---|
| PK `(pitch_id)` | **KEEP** | Serves A (point lookup), the hot per-pitch payload fetch. |
| `idx_pp_pitcher_season (pitcher_id, season)` | **KEEP** | Serves C (the pitch-tile build pre-filter). The load-bearing one. |
| `idx_pp_pitcher (pitcher_id)` | **KEEP** | Serves the steal-style / single-pitcher scans and is the prefix of the composite; cheap, broadly useful. |
| `idx_pp_season (season)` | KEEP (advisory) | Serves season-scoped rebuilds (`--seasons`); low cost. |
| `idx_pp_game_date` | KEEP (advisory) | Watermark `MAX(game_date)` aggregation; small. |
| `idx_pp_batter`, `idx_pp_batter_season` | **DROP** | Pitch pool is never pre-filtered by `batter_id` in the query path (batter-side filtering lives in the outcome pool on `stand`). |
| `idx_pp_outcome (outcome_type)` | **DROP** | `outcome_type` is *projected* in A, never filtered on. |
| `idx_pp_runners (runners_state)` | **DROP** | Situation is consumed in bulk by the KDTree (H), never via a `runners_state` predicate. |
| `idx_pp_count (count_balls, count_strikes, outs)` | **DROP** | Same — situation columns are bulk-read, not filtered. |
| `idx_pp_velo`, `idx_pp_ivb` | **DROP** | Fingerprint dims are read in bulk inside the `(pitcher_id, season, stand)`-scoped scan (C); no query filters on a single feature value. These are the clearest dead indexes. |

> Note: there is **no index on `stand`** today, yet `stand` is half the pitch
> pre-filter (C). SIM-115 should evaluate adding `idx_pp_pitcher_stand_season
> (pitcher_id, stand, season)` (or extending the existing composite with `stand`)
> so C is fully index-served rather than relying on the `(pitcher_id, season)`
> prefix + a residual `stand` filter.

**`sim.outcome_pool`**

| Index | Decision | Reason |
|---|---|---|
| PK `(pitch_id)` | **KEEP** | Serves B (event payload point lookup). |
| `idx_op_season (season)` | **KEEP** | Serves E/F (the only batted-ball pre-filters are `season` + `stand`). |
| `idx_op_pitcher`, `idx_op_batter` | **DROP** | Batted-ball tiles pre-filter on `stand` only; pitcher/batter id are carried but never filtered. |
| `idx_op_bb_type`, `idx_op_exit_velo`, `idx_op_launch_angle`, `idx_op_spray_angle` | **DROP** | Fingerprint/outcome detail read in bulk inside the `season`+`stand` scan; never single-column predicates. |
| `idx_op_runners` | **DROP** | Situation, bulk-read. |
| `idx_op_result_hits`, `idx_op_fielded_by` | **DROP** | Step 3b/4 read these per fetched row by PK, not via a value predicate. |

> Note: as with the pitch pool there is **no index on `stand`**. SIM-115 should
> evaluate `idx_op_stand_season (stand, season)` so E is fully index-served.

**`sim.stolen_base_pool`**

| Index | Decision | Reason |
|---|---|---|
| PK `(pitch_id)` | **KEEP** | Point lookup. |
| `idx_sbp_runner (runner_id)` | **KEEP** | Serves G (the primary steal-decision pre-filter). |
| `idx_sbp_catcher`, `idx_sbp_pitcher` | KEEP (advisory) | Participant pre-filters the sim loop will issue (catcher pop-time / pitcher hold lookups). |
| `idx_sbp_season` | KEEP (advisory) | Season-scoped rebuild/recency. |
| `idx_sbp_base (base_attempted)` | KEEP (advisory) | Narrows G by base; low-cardinality but used as a secondary filter. |
| `idx_sbp_success (success)` | **DROP** | Boolean outcome, projected not filtered. |
| `idx_sbp_runner_speed (runner_sprint_speed)` | **DROP** | Denormalized feature read by PK/participant scan; never a single-column predicate. |

**Summary for SIM-115.** Across all three pools the rule is the same: **keep the
PK and the pre-filter-key indexes; drop every single-feature / outcome-column
index** (those columns are projected or bulk-scanned, never used as a WHERE
predicate). The two gaps to evaluate are explicit `stand`-bearing composites on
the pitch and outcome pools.

---

## 7. Stability contract

This is the guarantee surface for consumers of the query API.

### 7.1 Guaranteed columns (consumers MAY rely on these)

These columns are part of the contract; their **name, type, and semantics are
stable** and may not change without a migration + a bump to this doc:

- **All PK / pre-filter-key columns:** `pitch_id`, `pitcher_id`, `stand`,
  `season` (all pools); `runner_id`, `catcher_id`, `base_attempted`, `success`
  (stolen-base pool).
- **The fingerprint columns**, *in order and dimension count*:
  - pitch (10-dim): `velo, ivb, hb, spin_rate, spin_axis, release_x, release_z,
    release_ext, plate_x, plate_z` — order MUST match `PITCH_FEATURES` in
    `play_pool_cache.py` and the sampler's query-vector contract.
  - batted-ball (3-dim): `exit_velo, launch_angle`, and the spray column
    (`pull_relative_spray_angle` preferred, `spray_angle` fall-back).
- **The outcome payload columns:** `outcome_type` (pitch), `events`
  (batted-ball), `success` (steal). The sampler keys on the *exact* string
  vocabularies (`outcome_type ∈ {ball, called_strike, swinging_strike, foul,
  in_play}`; `events` is the PA-event vocabulary).
- **`recency_weight`** (all three pools): present, NOT NULL, FLOAT, DEFAULT 1.0
  (§4).

### 7.2 Advisory columns (present, but not a hard contract)

Read opportunistically; consumers must tolerate their absence/NULL and must not
hard-fail on them: `game_pk`, `at_bat_number`, `pitch_number`, `zone`, the
`prev_pitch_*` sequence columns, `bb_type`, `hit_distance`, `hc_x`, `hc_y`,
`fielding_error_position`, and the denormalized steal metrics
(`runner_sprint_speed`, `runner_sb_success_rate`, `catcher_pop_time_mean`,
`catcher_arm_strength`, `pitcher_sb_allowed_rate`). The builder already codes for
schema flexibility (e.g. `bat_hand`/`stand` candidate selection, the
`updated_at`→`game_date` watermark fall-back) — that resilience is the model.

### 7.3 The change rule

> **Any change to a §7.1 guaranteed column** — rename, retype, drop, a change to
> a fingerprint column's *order*, or a change to an outcome string vocabulary —
> **requires a schema migration AND a version bump to this document** (and, for
> fingerprint/builder-logic changes, a `BUILDER_VERSION` bump in
> `play_pool_cache.py` so tiles rebuild, SIM-300 §4.4 rule 3).

Adding a new **advisory** column requires neither. Promoting an advisory column
to guaranteed (because a consumer started relying on it) requires updating §7.1
here.

---

## 8. `recency_weight` semantics (SIM-076)

`recency_weight` is a per-row sampling weight present on all three pools (FLOAT,
NOT NULL, DEFAULT 1.0):

- **Value rule.** `2.0` for rows in the most-recent two seasons; geometric decay
  (×0.75 per season, floored at `0.25`) for older rows, **relative to the build's
  reference season** (`sim.pool_build_metadata.recency_ref_season`). So recent
  form is preferred without discarding history.
- **How the sampler uses it.** The `PlayPoolSampler` **multiplies a row's
  FAISS-distance weight by its `recency_weight`** before normalizing the k-NN
  sampling distribution. Distance gives "how similar"; `recency_weight` gives
  "how recent" — the product is the draw probability.
- **Relationship to the tile-level recency boost.** SIM-300's build-side recency
  *boost* (duplicating last-two-season rows once in the FAISS index, §5) and this
  per-row `recency_weight` are complementary: the boost biases which vectors are
  *retrievable*, `recency_weight` biases the *probability among the retrieved k*.
  A change to the recency policy that alters `recency_weight` values is a
  guaranteed-column change (§7.3) **and** changes the recency-boost flags, so it
  forces a tile rebuild (SIM-300 §4.4 rule 4).

---

## 9. Acceptance trace

| SIM-111 requirement | Where satisfied |
|---|---|
| Per-pool column tables (name/type/semantics/role, PK + pre-filter keys marked) | §2 (pitch), §3 (outcome), §4 (stolen-base) |
| Access patterns (point lookup, pre-filtered scan, situational) tied to indexes | §6.1 |
| Index keep/drop split as the SIM-115 input | §6.2 |
| Stability contract (guaranteed vs advisory, migration rule) | §7 |
| `recency_weight` semantics + sampler multiplies distance-weight by it | §8, and noted in each §2–§4 table |

---

*End of SIM-111 contract.*
