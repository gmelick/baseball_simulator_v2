# Remediation Plan — MLB Simulation Platform
**Baseline:** master @ `66746df` · 2026-07-27 · Alembic head **0017** · DuckDB schema **14** · next free ticket **SIM-443**
**Source:** `docs/audit/2026-07-23-MASTER-BUG-REGISTER.md` (273 findings) + this session's four-module reconciliation

---

## Closed so far (what this session actually finished, with register IDs)

Master's three post-register commits closed **4 of the register's 273 findings** — and only **1 of its 49 `FIXED-BRANCH` rows**. Everything else in those commits is net-new adversarial-review output.

### Register-tracked closures — `pipeline/etl/etl_historical_loader.py` (commit `c551f8d`)

| ID | Was | Now | Evidence |
|---|---|---|---|
| **`D-5`** | FIXED-BRANCH | **FIXED-MASTER** | `:1052` reads `runner["details"].get("rbi", False)`; master's rewrite is independent of the branch's hunk |
| **`audit-DE-8`** | OPEN | **FIXED-MASTER** | `_ensure_venue` at `:1913` — retry loops break on success, bare re-raise removed, Savant scrape fails loudly |
| **`audit-DE-9`** | OPEN | **FIXED-MASTER** | wind parsed from the single `weather.wind` string |
| **`SIM-437`** | FIXED-MASTER | unchanged | `e59c322` |

### Net-new closures with no register ID (do not credit against any register row)

**`c551f8d`** — 17 net-new ETL defects found in fresh adversarial review and fixed: `isOut` read from the wrong dict (3 columns FALSE on 6.55M rows); `bat_hand`/`stand` semantics documented backwards repo-wide and corrected; in-play completeness gate widened `X` → `D/E/X`; per-game exception isolation + consecutive-failure circuit breaker on both paths; `reload_game()` (DELETE+re-INSERT in one transaction) replacing `ON CONFLICT DO NOTHING` + fabricated insert counts; substitution-flag PA broadcast (~4× inflation); release_speed ceiling 102 → 50–110 in validator *and* DB trigger; the Python validator's silent override of SIM-087's widened floor; running score re-synced from `play["result"]`; outs accumulator counting non-pitch events; `_ensure_players` handedness fabrication; manager fallback / ordering / `season_end`; `post_on_*` retaining put-out runners; `load_date_range` silent >365d clamp + Final gate; `_ensure_game` conflict clause 6-of-25 columns; `raw.etl_errors` unbounded growth + ledger transaction scope; and a low cluster (spray-quadrant flip behind the plate, comma stripping, `field_assist_6_plus` overflow column, `primary_position` default `'P'`→`'UT'`, `raw.players.active` dropped, `'C'` removed from GAME_TYPES, pooled `requests.Session` + retry semantics, locked pool construction).

**`e19d083`** — `n_def_sub_late` counted pitch rows, not plate appearances (`player_profile_computor.py:2619-2650`, now `COUNT(DISTINCT game_pk||'-'||at_bat_number)`). No register ID. **Everything else in that file's register section is still open.**

**`66746df`** — SIM-442: quoted-identifier bypass of the dangerous-function blocklist in `api/routes/sql_safety.py`. No register ID.

Schema shipped: **Alembic 0016 + 0017**, **DuckDB 0014** (version file 13→14).

### ⚠ Two housekeeping defects created by these commits

1. **SIM-442 is double-allocated.** The register assigns SIM-442 to the *deferred* plan ticket covering `D-M2` (park factors as venue-vs-league) and `D-M3` (spin_axis as linear) — register lines 472-473. `66746df` spent SIM-442 on the SQL-validator fix. Renumber the deferred one before it is worked.
2. **The register file carries stray agent text.** `docs/audit/2026-07-23-MASTER-BUG-REGISTER.md` ends with a line reading `agentId: a56235071ba3a78bb (use SendMessage with to: ... to continue this agent)`. That is captured tool output, not content. Strip it.

### Still open in `etl_historical_loader.py` — do not report this file as done

`_dispatch_game` never writes `outcome='failed'`/`last_error`; a player skipped for unestablishable handedness is still FK-referenced by pitch rows; `uq_etl_errors_natural_key` does not infer on NULL keys; the circuit breaker resets on the already-loaded skip path; `_log_freshness.updated_at` only advances on a watermark advance; `primary_position` still reads the boxscore slot rather than `person.primaryPosition`. Plus `D-N5` — **every parser fix above is inert until the reload sweep runs**, and `rbis_on_pitch` still has zero read consumers.

---

## ⚠ Believed-fixed but NOT on master (the wave1-remediation trap)

`git branch --merged master` returns `feat/sim-realism-and-engine-wiring`, `fix-weekly-integration`, `master`, `sim-438-live-game-season`. **`wave1-remediation` is absent.** 8 commits sit unmerged (`a21308a`, `5ec3b5c`, `52ef261`, `a8e84e2`, `a7b12c8`, `534b7a9`, `85488b7`, `a19921b`).

**48 of the register's 49 `FIXED-BRANCH` rows are live production defects today.** Every one below was verified by reading `git show master:<file>`, not inferred from the label.

### The ones the owner is most likely to believe are done

| Area | IDs still live on master | Verified at |
|---|---|---|
| **Simulator (Track C)** | `2.1/C1` pitcher resurrection (with `SIM_MANAGER=1` **ON in production**), `2.2/C2` steals zeroed, `2.3/C3` phantom DP runner, `2.4/C4` ROE→out, `C5`, `C6`, `C7`, `C1 (semantics)` | no `_resolve_double_play`, no `_ERROR_EVENTS` in sim_loop.py; `result.runs_scored = int(result_runs)` at `:1606`; steal gate at `:2945`→`:2955` |
| **Profile SQL (Track D)** | `D-1` whiff_rate is called-strike rate, `D-2` GB/FB denominator outs-only, `D-3` first_pitch_take_rate is a *swing* rate, `D-4` fixed spray sign + `pull_rate_vs_l ≡ pull_rate_vs_r` (no `p_throws` on either leg), `D-M1` RE matrix misses last-PA runs | `:1630`, `:1652`, `:1845`, `:1881-1985`, `build_run_expectancy_matrix` |
| **Similarity (Track E)** | `E-1`, `E-1-GLOBAL`, `E-EB`, `E-ZFILL`, `E-MISSING-1.0`, `E-CAL-SIGMA` (+ wiring), `E-CAL-ARSENAL`, `E-ESS`, `E-RELCURVE` ×3 | `pitcher_similarity.py:1441/:1403`; `batter_similarity.py:199/:324`; `engine_artifacts.py:284` (`np.nan_to_num` *before* z-score); `full_pool_sampler.py:41-43`; `prop_validation.py:225` |
| **Betting / CLV (Track B)** | `1.2` cross-line CLV, `1.4`/`AUD-1.4` identity win-prob, `1.5` ties-as-losses, `1.6`, `1.7`, `1.EX.push-loss`, `1.EX.degenerate`, `1.EX.mockfilter`, `1.EX.slate-bias`, `1.2-LM`, `B.PROP-TAIL` | none of `_wilson_interval`/`_clustered_se`/`line_moved`/`is_mock`/`_deterministic_sample`/`POWER_FLOOR_BETS` exist in `clv_backtest.py`; `clv_engine.py:236` has no `p_push`; `clv_backtest.py:876` is bare `win_probability(summary)` |
| **Odds / ingest** | `1.1` UTC-date wrong-game matching (25–35% of the 2,378-game backfill), `1.8`, `1.1 (b)` (21,612 bullpen games wrong-dated), `SIM-448-MIG` | `bettingpros_odds_provider.py:138`, `bullpen_availability_ingest.py:196` — both still `str(game["gameDate"])[:10]` |

**Say this plainly: the ~49% beat-close headline is still produced by the un-remediated instrument, on odds that are still ~25–35% wrong-game, with an identity win-probability, ties scored as losses, and CLV compared across different lines.** That number is not evidence about the model in either direction.

### The 8 `BLOCKER` rows are not master bugs

`C-B1`, `C-B2`, `C-N10`, `E-B1`, `E-B2`, `E-B3`, `B-B1`, `B-B2`, `B-B3`, `B-B4`, `D-B1`, `B-N3`, `B-N4`, `B-N6`, `B-N7`, `B-N8`, `B-N9`, `E-N4`, `E-N5`, `E-B1-API` describe defects **in unmerged branch code**. On master the code they break does not exist. Treat each as a **merge gate on its partner fix**, not as production-critical:

`E-B1 ↔ E-RELCURVE` · `E-B2 ↔ E-ESS/E-MISSING-1.0` · `E-B3 ↔ E-1+E-CAL-ARSENAL+E-ZFILL` · `B-B1 ↔ B.PROP-TAIL` · `B-B2 ↔ 1.6` · `B-B3 ↔ 1.2` · `B-B4 ↔ 1.7` · `C-B1/C-B2 ↔ C2` · `D-B1 ↔ D-4`

Merging wave1 as-is is **strictly worse than master in three places**: the reliability curve emits literal 1.0/0.0 for 61–64% of ordinary lopsided games (silently deleting the moneyline market from the betting card *and* the CLV scoreboard); prop tail-smoothing converts zero-information markets from SKIPPED to PLACED at edge **+0.1912** (exactly `1 − fair_under`, the maximum the market allows); and the printed CLV confidence interval becomes ~3× too narrow (0.0309 vs the honest clustered 0.0980).

### Verdict on the branch: **retire it as a merge candidate. Cherry-pick.**

Three collisions, one fatal:

**(a) Alembic — hard break, not a text conflict.** Both sides declare `revision="0016"`, `down_revision="0015"`. Different filenames, so **git merges cleanly and Alembic then reports two heads and applies nothing** — silently reverting both the SIM-440 trigger fix and the SIM-448 odds provenance columns. Master has since advanced to 0017, so the branch file must be renumbered to **0018** with `down_revision="0017"`, and the "single head" verification redone. Master's own `tests/unit/test_sim440_reload_game.py::TestAlembicHistoryIsLinear` will catch it.

**(b) `etl_historical_loader.py`** — master rewrote 1,428 lines; the branch changes 5, all of which master already landed with identical semantics. **Discard the branch's contribution to this file entirely.**

**(c) `player_profile_computor.py` — the dangerous one.** Hunks do not textually overlap, so git will auto-merge. That *is* the hazard: the branch's `D-4` keys the signed-spray CASE on **`bat_hand`** (the `D-B1` blocker — `'S'` on 10.4–13.3% of rows, zeroing `pull_rate`/`oppo_rate` for 416 batter-seasons, the two highest-weighted of the 8 `BATTED_BALL_FEATURES`), and it would land silently 2,900 lines above master's correction keying `pull_relative_spray_angle` on **`stand`**. The merged file would be self-contradictory. **The register's own fix instruction for D-B1 — "reuse the idiom at `:4645`, `CASE WHEN bat_hand IN ('L','R') THEN bat_hand ELSE stand END`" — is now obsolete; `c551f8d` deleted that idiom because it encodes the semantics backwards. Key on `stand` directly.**

**Cherry-pick order of cleanliness:** (1) `simulation/*`, `similarity/*`, `betting/*`, `scripts/clv_backtest.py`, `bettingpros_odds_provider.py`, `bullpen_availability_ingest.py`, `engine_artifacts.py` — zero overlap, but each carries its own BLOCKER, so a pick is not sign-off; (2) the SIM-448 migration, renumbered to 0018; (3) `player_profile_computor` D-1/D-2/D-3/D-M1 take as-is, **D-4 rewrite onto `stand`**; (4) discard the ETL hunk.

---

## Deferred security backlog (tracked, not scheduled)

Postponed by owner decision until the per-file bug work is done. Listed so it is not lost, **not** sequenced anywhere below.

| Register ID | Item |
|---|---|
| **G5** | API unauthenticated as deployed — `api/auth.py:248` returns before checking credentials when `ENVIRONMENT=development`; the non-dev path also fails open (empty `API_KEYS` + whitespace `AUTH_PASSWORD` allows requests, no boot warning, no `/health` field distinguishing bypassed from enforced) |
| **I5** | Postgres/Redis published on all host interfaces; default `baseball_pass` in 16 files; a hardcoded compose DSN override that *wins over* `env_file` (so setting real credentials in `.env` has no effect); Grafana admin/admin; `baseball_user` is a verified superuser |
| **I6** | Unauthenticated LAN-published Redis serving **pickled** cache values the API deserializes — a complete poisoning → deserialization-RCE → wrong-prices path |
| **G11 (codec half)** | The pickle codec itself; paired with I6/SIM-469. *The single-flight-lock half of G11 is not deferred and is scheduled in Phase 2.* |
| — | No schema allowlist in the SQL console (`pg_authid` readable); `BASEBALL_DB_RO_DSN` supported but unconfigured |

**Not deferred, do not confuse with the above:** `G8` (rate limiter buckets on the client-supplied `X-API-Key` header — rotate to bypass; unbounded bucket dict), `I10` (`ofelia:latest` unpinned with the Docker socket mounted; Actions tag-pinned not SHA-pinned; no Python lock file — precisely how CI-PIN-1 happened), `I2` (production runs the *dev* compose config), and `G3`'s auth half (add `require_auth` to two specific routes). These are scheduled.

---

## Remaining open issues by file

**Reading note:** seven files appear in two module sections of the source review (`full_pool_sampler.py`, `prop_validation.py`, `prop_distributions.py`, `engine_artifacts.py`, `player_profile_computor.py`, `sim_stats.py`, `clv_backtest.py`). Their counts below are the per-section views of the same file and are **not additive** — the union is what matters and is reflected in the phase plan.

**Gate legend:** `none` = verifiable with a unit test or a stack restart · `re-ingest` = the 21.6k-game pitch reload · `recompute` = the ~5.7h profile rebuild · `artifacts` = `engine_artifacts --what all` · `cal-refit` = `make calibrate` + `validate-props --write-calibration` · `odds` = purge + re-backfill · `clv` = a CLV/sim re-run

### Module 1 — `simulation/` (64 open + 1 deferred)

| File | Open | Worst | Gate |
|---|---|---|---|
| `simulation/sim_loop.py` | **29** (C1/H10/M11/L7) | `C1` — a pulled starter returns next half-inning under `SIM_MANAGER=1` (ON in prod); every pitcher K/BB/ER/OUTS PMF in every traded market was built on a reliever carousel ending in illegal re-entry | cal-refit |
| `simulation/full_pool_sampler.py` | **9** (H3/M4/L2) | `AUD-SITKERNEL` — runners bitmask + raw inning as unstandardized Euclidean dims; runner-on-2nd vs bases-empty retains weight **0.92**, so RISP conditioning is an ~8% down-weight (a concrete causal candidate for the run-conversion gap) | artifacts |
| `simulation/batch_runner.py` | **5** (H2/M2/L1) | `G1a` — `results[idx] = fut.result()` at `:1099` with no `timeout=` and no `BrokenProcessPool` handling anywhere; `/health` never touches the pool, so the documented OOM-deadlock has no detection and no recovery | **none** |
| `simulation/prop_validation.py` | **4** (C1/M3) | `E-RELCURVE` — shipped curve accepts `min_bin_count=1`, so a single-game bin injects a hard 0/1 into the map loaded at boot and per CLV worker; ECE is computed in-sample on the fit data | cal-refit |
| `simulation/prop_distributions.py` | **3** (C1/H2) | `B.PROP-TAIL` — no tail smoothing at all, so `P(K≥12)` prices to hard 0.0: the model's biggest claimed edges either fabricate a maximal edge or are silently dropped | clv |
| `simulation/production_factory.py` | **3** (H1/M2) | `F-04a` — two bare `except Exception: return None` with no log (`:169-171`, `:365-366`); a corrupt artifact volume serves HTTP-200 prices from a different model, undetectable live or forensically | artifacts |
| `simulation/run_resolution.py` | **2** (M2) | `C-N2`/`C-N5` — `advance_state()` has no vocabulary for a runner retired on the play and no reached-credit for reach-on-error, so it cannot record a correct RE24 end state for either Track C fix that depends on it | **none** |
| `simulation/win_probability.py` | **2** (H1/M1) | `AUD-1.4` — `clv_backtest.py:876` calls `win_probability(summary)` bare → IDENTITY calibration, while production threads the fitted curve; the SIM-387 bug class, re-introduced into the scoreboard | cal-refit |
| `simulation/game_state.py` | **2** (C1/L1) | `C1 (semantics)` — `home_pitcher_id`/`away_pitcher_id` documented as "starter" while C1 requires "current pitcher"; `assert_consistent()` is a bare non-negative-id check that cannot detect the illegal base-out state C3 relies on it to catch | **none** |
| `simulation/results.py` | **1** (H1) | `G3/J4` — `/boxscore` and `/props` re-simulate serially in the API parent (~150–220 s at n=100 vs the pool's ~38 s) purely because `from_results` drops boxscores the workers already pickle back | **none** |
| `simulation/play_pool_sampler.py` | **1** (H1) | `AUD-QA-INVERSION` — CI certifies a config production never runs; the golden gate imports nothing from `simulation/` | **none** |
| `simulation/score_fusion.py` | **1** (M1) | `AUD-ARCH-FUSION` — ~480 lines dead in production by their own documentation | **none** |
| `simulation/pitcher_decisions.py` | **1** (L1) | `C-N9` — module header states `GameState` lacks fields that `game_state.py:254-255` defines. **Register mis-classifies this as branch-new; it is a live master doc defect** | **none** |
| `simulation/snapshots.py` | **1** (L1) | `C-N1` blast radius — incomplete RE24 provenance rendered into the play-by-play (sac fly shows `re_start` 0.27 vs true 0.96). Display-only; do not let it inflate C-N1's priority | **none** |

**Stale-status corrections for this module, verified in code:** all 17 `FIXED-BRANCH` rows are OPEN. **Every line number on a `FIXED-BRANCH` row is a branch line number** — master `sim_loop.py` is 3,925 lines (register says 4,099), master `full_pool_sampler.py` is 443 (register says 573). Re-derived master anchors: `_full_pool_outcome` `:1332`, `_full_pool_fielding` `:1386`, `_full_pool_out_advancement` `:1507`, `_full_pool_steal_decision` `:3091`, `_maybe_pull_starter` `:3183`, `_set_half_matchup` `:2722`; `production_factory` bare excepts `:169-171`/`:365-366`; `steal_order_rate_per_1b_opp: 0.08` at `:461`.

### Module 2 — `pipeline/` (47 open)

| File | Open | Worst | Gate |
|---|---|---|---|
| `pipeline/batch/player_profile_computor.py` | **15** (C2/H4/M8/L1) | `E-LEAK (a)` — `recency_weight` materialized against `_canonical_ref_season`, so a 2024 replay up-weights 2025/26 plays 2.0 vs 1.5 for its own season. **Every ECE, trust label and CLV figure ever published was measured with future data in the sample** | recompute |
| `pipeline/batch/engine_artifacts.py` | **12** (C2/H2/M6/L2) | `E-LEAK (b)` — bundle scoped to the newest 3 DB seasons (`RECENCY_FLOOR_SEASONS=3`, `:62`); for every pre-2024 replay **every sampled play is future data**. This is the condition under which win-prob ECE 0.047 and the "bettable" H/HR/TB labels were produced | artifacts |
| `pipeline/bettingpros_odds_provider.py` | **8** (C1/H4/M3) | `1.1` — `str(game["gameDate"])[:10]` at `:138`; identical-open-AND-close in **26.1% CT / 43.3% MT / 43.2% PT** same-matchup pairs vs a **0.1% ET** baseline | odds |
| `pipeline/etl/etl_historical_loader.py` | **7** (M1/L6) | `D-N5` — every `c551f8d` parser fix is **inert** until the 21.6k reload sweep runs; `rbis_on_pitch` still has zero readers. Plus 6 un-ticketed post-`c551f8d` residuals — **the FK handedness hole is the one to pull forward** (it will be exercised at 21.6k scale by the very sweep this file now enables) | re-ingest |
| `pipeline/live/live_ingestion_pipeline.py` | **6** (H2/M3/L1) | `audit-B-1.FWD` — `mark_closing_lines`/`mark_closing_prop_lines` have **no production caller** and the live pipeline is off by default; every 2026 slate day permanently loses forward CLV reference data, the one dataset unrecoverable at any price | **none** |
| `pipeline/live/bullpen_availability_ingest.py` | **2** (H1/M1) | `1.1 (b)` — `:196` same UTC-date bug; the completed **21,612-game** SIM-433 ingest queried the *next day's* roster and IL. No purge, no re-ingest scheduled | re-ingest (separate 21.6k sweep, different endpoint — parallelizable) |
| `pipeline/odds_provider.py` | **2** (H2) | `audit-B-mock` — factory still defaults `DEFAULT_PROVIDER="mock"` (`:179`); anything not explicitly setting `ODDS_PROVIDER=bettingpros` Kelly-sizes "+EV" signals off fabricated lines, and the CLV backtest has no `is_mock` filter | **none** |
| `pipeline/etl/opening_line_job.py` | **1** (H1) | `audit-B-1.FWD (b)` — `schedule_opening_line_job` exists at `:487` with **no caller**; `deploy/ofelia/config.ini` holds exactly one job | **none** |
| `pipeline/batch/play_pool_cache.py` | 0 | No findings — listed because it is a mandatory participant in the single recompute and `c551f8d`'s `_build_pitch_pool` change **already invalidated the current tiles** | recompute |

### Module 3 — `similarity/` + `betting/` + `scripts/` (68 open, 0 fixed)

| File | Open | Worst | Gate |
|---|---|---|---|
| `scripts/clv_backtest.py` | **19** (C3/H6/M7/L3) | `1.2` — CLV compared **across different betting lines** on 9 of 10 markets; the single most valuable CLV win (the book moving the line) scores as ~zero | clv |
| `scripts/load_historical_odds.py` (+provider) | **8** (C1/H5/M2) | `1.1` + `B-N5` — fixing the matcher does **not** repair the poisoned table: `:172` skips rather than overwrites, so a rejected game *keeps* its wrong-game row as the latest row the backtest reads | odds |
| `similarity/engines/pitcher_similarity.py` | **6** (H1/M5) | `E-1` — a GMM-less pitcher's command sub-score is multiplied by 1/0.35 ≈ **2.857**; 66.9% clip to similarity exactly 1.0, P(GMM-less outranks full-arsenal) = **0.936**, and GMM-less occupy **100%** of the top 1% | artifacts |
| `similarity/similarity_calibration.py` (cross-engine) | **6** (H3/M2/L1) | `CAL-BYPASS` — "calibration is LIVE" is true for the boot engines serving `/similarity` and **false for the path that prices props**: the production sampler runs unfitted hard-coded sigmas, an uncalibrated pitcher-sim matrix and an unweighted batter distance | cal-refit |
| `scripts/sim_stats.py` | **5** (C1/H3/M1) | `F-13` — `_sim_kwargs` drops the already-resolved defense maps and never passes `park_run_factor`, so `SIM_PARK_FACTOR` and `SIM_FIELDER_RBF` are **provably inert under the harness**: toggling them compares two identical no-ops and tautologically reports "no distortion" | none (code) / clv (the re-run) |
| `similarity/engines/batter_similarity.py` | **4** (M4) | `E-EB` — EB shrinkage effectively off (`EB_N_PRIOR=5` under a 100-PA floor gives α ≥ 0.95); `E-N1`: the branch fix moved the gap down a level rather than closing it — `_apply_shrinkage()` runs inside `build()` and `eb_alpha` is baked at load | artifacts |
| `betting/clv_engine.py` | **4** (H2/M1/L1) | `B-N1` — the push fix was applied to `model_ev`, which decides nothing; `_pick_side` gates on `.edge`, which is not push-aware, so a perfectly calibrated sim reports **edge = −0.0450 on both sides** of a whole-number total and those totals are never bet | **none** |
| `pipeline/batch/engine_artifacts.py` (Track E view) | **4** (M4) | `E-ZFILL` — `vecs=np.nan_to_num(mat)` at `:284` zero-fills **before** z-scoring; a player missing `max_exit_velo` lands at **z ≈ −21** and his similarity to everyone everywhere is crushed | artifacts |
| `simulation/prop_distributions.py` (Track B view) | **3** (C1/H2) | see Module 1 | clv |
| `simulation/prop_validation.py` (Track E view) | **3** (C1/M2) | see Module 1 | cal-refit |
| `betting/line_movement.py` | **2** (H1/M1) | `1.2-LM` — the live `/line-movement` CLV endpoint never checks whether the line moved; a total going 8.5 → 9.0 (a maximal CLV win) scores ~zero | **none** |
| `similarity/engines/situation_similarity.py` | **1** (H1) | `SIT-GEOM` — bases-loaded (7) scores closer to third-only (4) than to first-and-second (3). The register's named causal candidate for the tracked "runs ~10–12% low / batted-ball-with-RISP" gap | cal-refit |
| `similarity/backtesting/` | **1** (C1) | `E-LEAK` — the only critical that invalidates *already-published* results. A correct expanding-window splitter exists in this very directory with **zero callers anywhere** | clv |
| `betting/bet_signal.py` | **1** (M1) | `STRAT-GAP` — the scoreboard measures raw picks; the deployable strategy is the gated Kelly-weighted subset. The number the fund reads is not the number the fund would earn | clv |
| `simulation/full_pool_sampler.py` (perf view) | **1** (H1) | `E-B2` — branch-only: +22%/PA, taking n=100 `/simulate` from ~38 s back to ~55–65 s, silently giving back most of the SIM-430/436 epic | **none** |
| `scripts/validate_props.py` | 0 | Sequencing landmine only — it is the **write path** for the curve `E-B1` breaks | cal-refit |
| `scripts/fit_calibration.py` | 0 | Sequencing landmine only — the convergence point for six findings | cal-refit |

### Module 4 — `api/` + `db/` + `frontend/` + tests/CI + ops (74 open incl. 8 merge-gate + 2 deferred)

| File | Open | Worst | Gate |
|---|---|---|---|
| `tests/unit/` | **7** (C1/H5/L1) | `ADV-SUITE-1` — 2,344 unit + 53 regression + 12 e2e + 24 integration tests, ruff and mypy clean, **green over every blocking defect on the branch**. `F-03`: the build-smoke suite mocks connections to return empty rows then asserts `profile_count == 0` **passes** — an engine that finds nothing in the database is certified green. That is exactly the gap that produced SIM-408 | **none** |
| `frontend/src/pages/` + graphics | **7** (H1/M4/L2) | `H2` — `BaseballFieldGraphic` shows **every base occupied on every live game**: the predicate is `!= null` and `false != null` is true, while the only call site passes booleans | **none** |
| `.github/workflows/` | **6** (M5/L1, +2 deferred) | `F-07` — `PERF_STRICT` hard-gates an rng stub; no benchmark touches `FullPoolSampler`, so the SLA gate stays green while the production path regresses arbitrarily. `I7`: **both** release workflows filter `branches: - main` while the default branch is `master` — the ghcr release path has never run once and there is no immutable image to roll back to | artifacts (F-07) / none (rest) |
| `db/migrations/duckdb/` + `db/schemas/` | **6** (C1/H3/M2) | `DUCK-LAG-1` — schema lag silently reverts validated realism while the flags still read ON (loader degrades silently; **builder fails loudly on the same condition** — that asymmetry is where drift hides). `F-11` re-verified: `migration_history` appears **0 times** in `02_duckdb_schema.sql`, and 0014 was not folded in either | recompute (F-10) / none (rest) |
| `api/main.py` + `api/state.py` | **5** (H3/M2) | `G2` — nothing bounds `fut.result()`, no `BrokenProcessPool` handler exists, healthcheck never touches the pool. `API-ENV-1`: ~23 env vars, three incompatible boolean grammars, one where `'off'` **enables** the live pipeline | **none** |
| `api/routes/games.py` | **5** (H1/M4) | `G3` — unauthenticated serial re-simulation compute-DoS at n ≤ 2000. `ARCH-GAMES-1`: still 1,876 lines, and the calibration + CLV scripts privately import its underscore-prefixed internals | **none** |
| `scripts/sim_stats.py` | **5** (C1/H3/M1) | `F-17` — five production flags enabled on validation an order of magnitude below the project's own pre-registered bar, all effects measured together, then relabeled "Validated — no run distortion" in five documents. At that power, the manager's "−0.10 runs/team" is consistent with **−0.26 to +0.06**, a real distortion *did* slip through (enabling `SIM_MANAGER` zeroed all stolen bases), and home win% moved 0.567 → 0.523 in the same enablement and was narrated as a bonus | clv |
| `api/routes/betting.py` | **3** (C1/H1/M1) | `G4` — `/edges` and `/signals` price off the mock provider by default and `/api/odds/{pk}` **always** returns mock, while 2,378 games of real odds sit unread one table away | odds |
| `docker-compose.yml` | **3** (H1/M1/L1) | `I2` — production runs the dev config: `target: dev`, `uvicorn --reload` over bind mounts (**any file save drops the warmed worker pool mid-slate**), and the documented `--env-file .env.production` bring-up does nothing because the service hardcodes `env_file: .env` | **none** |
| `deploy/monitoring/` | **3** (H1/M2) | `I3` — no Alertmanager, no alert rules, no cadvisor/node-exporter (container RSS against the 10 GB `mem_limit` is unmeasurable, and memory is this platform's worst incident class); an OOM-deadlocked pool stays **GREEN**, so `restart: unless-stopped` can never fire | **none** |
| credentials / network / supply chain | **3** (H2/M1) | `I10` (**not deferred**) — `ofelia:latest` unpinned with the Docker socket mounted (root-equivalent on the host); Actions tag-pinned not SHA-pinned; no Python lock file. `I5`/`I6` deferred | **none** |
| `api/auth.py` | **2** (H1/M1) | `G8` (**not deferred**) — limiter buckets on the client-supplied header; unbounded bucket dict. `G5` deferred | **none** |
| `db/sim_store.py` | **2** (H1/M1) | `I4` — DuckDB allows one writer; the live replay RW handle points at the same file as the nightly rebuild container | **none** (but the drill collides with the nightly window) |
| `api/serialization.py` + layering | **2** (M1/L1) | `ARCH-ROUTERS-1` — FastAPI routers defined inside `pipeline/`, inverting the dependency direction | **none** |
| `deploy/` — Ofelia + nightly chain | **2** (H2) | `F-05` — the engine artifacts, **the production simulator's actual data source**, are unversioned, published non-atomically, and absent from the nightly chain; the module's own docstring calls itself "the NIGHTLY BUILDER" and nothing schedules it | artifacts |
| `deploy/` — backups / DR | **1** (H1) | `I1` — **no backup and no DR for any datastore**; five volumes in one WSL2 vhdx on a C: drive with a documented 100%-full incident; both documented "recovery" procedures destroy the data; forward-captured odds snapshots cannot be re-fetched at any price | **none** |
| `frontend/e2e/smoke.spec.ts` | **1** (M1) | `H7` — the entire frontend test surface is 4 mocked smoke tests. `66746df` added ~15 pages/components including a SQL console and an AI assistant that both execute user-authored SQL, with **zero** frontend tests | **none** |
| `frontend/.../betting/` | **2** (H2) | `H1` — the run-line market is broken end-to-end by `run_line` vs `runline`; a backend unit test **asserts the mismatch**. `H3`: one click fires two concurrent 200-iteration sims and pairs +EV badges from sim A with edge numbers from sim B | **none** |
| `db/migrations/versions/` | **1** (H1) | `B-N3` — branch 0016 adds two **dead** columns (nothing writes `updated`/`book_id`, no reader selects them), so SIM-448's entire point is undelivered while looking landed | odds |
| `frontend/openapi.json` + `api/` clients | **1** (M1) | `H4` — dead type pipeline; `grep -c lineup_ready frontend/openapi.json` = **0**, and `66746df` added four more hand-mirrored clients against seven new route modules | **none** |
| `pipeline/batch/player_profile_computor.py` (QA view) | **1** (H1) | `F-06` — ~22 steps, zero post-step assertions, **zero `raise` statements in 5,238 lines**. The proposed `_validate_outputs()` would be the first | recompute |
| `api/websocket/` | **1** (M1) | `G9` — no auth, no connection caps, leaking cleanup; the SIM-385 typed schemas are wired into nothing | **none** |
| `tests/conftest.py` | **1** (H1) | `F-01` — pins `SIM_FULL_POOL` + every realism flag OFF suite-wide; the four core production methods have **zero test references** | artifacts (toy bundle) |
| `tests/regression/` | **2** (H1/L1) | `F-02` — pins 5 of 11 engines, **none** of the five that weight the production draw, imports nothing from `simulation/`, and pins module-default sigmas production does not use. CI header still claims "all 9 similarity engines" — wrong on both count and coverage | cal-refit |
| `api/routes/metrics.py` | **1** (L1) | `API-OBS-1` — no request IDs, no structured logging; one global p95 mixing a 2 ms card fetch with a 38 s simulate | **none** |
| `CLAUDE.md` | **1** (M1) | `A5` — the documented definition-of-done omits version control entirely, which is why "CLOSED with zero commits" followed the written process to the letter | **none** |

**Module 4 stale corrections:** Alembic head is **0017**, not 0015 (`ALEMBIC-HEAD` stale). `F-09`/`F-12` say "13 DuckDB migrations" — there are **14**, and the version file reads 14, so F-12's wrong-DB-path header fix is now 14 files. `SIM-448-MIG`'s recorded adversarial verification ("chain correct, single head") is **invalid** as of today.

---

## Recommended order

**Ten phases. One order. `SIM-443` onward.**

The shape: *make the instruments trustworthy → fix everything that costs nothing → land the three code tracks → fire the expensive chain exactly once → measure.* Phases 0–2 are parallelizable across people; Phases 3–5 partition cleanly by file ownership; Phases 6–9 are strictly serial.

---

### Phase 0 — Stop ongoing, irreversible loss (no gate)

**Files:** `pipeline/etl/opening_line_job.py`, `pipeline/live/live_ingestion_pipeline.py`, `deploy/ofelia/config.ini`, `deploy/` (backups), `.github/workflows/*-release.yml`, `docs/audit/...MASTER-BUG-REGISTER.md`, `CLAUDE.md`, branch renumbering.

**IDs:** `audit-B-1.FWD`, `audit-B-1.FWD (b)`, `1.FWD`, `OPS-FWD-1`, `I1`, `I7`, `A5`, plus the SIM-442 collision and the branch's 0016→0018 renumber.

**Why now.** Two things are being destroyed while you read this. **Forward odds capture is dead wiring** — `opening_line_job` is scheduled by nothing, `mark_closing_lines` has no production caller, the live pipeline is off by default. Every 2026 slate day permanently loses CLV reference data; the register calls forward-captured odds "the one uniquely unrecoverable datastore." And **there is no backup of anything** — five Docker volumes in one WSL2 vhdx on a C: drive that has already hit 100%, with both documented recovery procedures being destructive. Everything else in this 273-finding corpus is days-of-rebuild. This is not.

**What it unblocks.** Nothing technically — that is the point. It is pure loss-prevention, and it is cheap.

**Shared gate:** none.

**Exit criterion.** An Ofelia job captures opening lines daily at ~08:00 ET and marks closing lines post-game, verified by two consecutive days of rows in `raw.game_odds` with distinct open/close. `pg_dump` + a DuckDB and `calibration.json` file copy land nightly on a host path **outside the vhdx**, with retention, a `restore.sh`, and **one actual restore drill completed**. Both release workflows filter `master` and one image is tagged in ghcr. The branch's SIM-448 migration is renumbered `0018`/`down_revision="0017"`. The deferred D-M2/D-M3 ticket is renumbered off SIM-442. `CLAUDE.md`'s definition-of-done requires a commit hash on master with CI green before a ticket is CLOSED.

**Effort:** 2–3 days.

---

### Phase 1 — Build the instruments (no gate)

**Files:** `scripts/sim_stats.py`, `tests/conftest.py`, `tests/regression/`, `tests/unit/`, `.github/workflows/ci.yml`, `pipeline/batch/engine_artifacts.py` (versioning only), `simulation/production_factory.py`, `api/main.py`, `db/migrations/duckdb/` (runner only).

**IDs:** `F-13`, `F-14`, `F-15`, `F-16` · `F-01` (lane only, **not** the golden) · `F-02` · `F-03` · `QA-COV-1` · `F-05` · `F-04a` · `F-09` · `DUCK-LEDGER-1` · `F-12` · `F-11`.

**Why now.** You are about to spend ~5.7 hours of recompute, a 21.6k-game sweep, and an artifact rebuild, and then read the result. **Today you cannot read it.** `F-13` proves it: the measurement harness drops the already-resolved defense maps and never passes the park factor, so `SIM_PARK_FACTOR` and `SIM_FIELDER_RBF` A/B toggles compare two identical no-ops — it rebuilds *inside the measurement tool* the exact defense-map-inertness failure the 2026-06-03 audit caught in production. `F-14`: the per-channel RISP/advancement/DP breakouts the harness advertises **exist only in the docstring**, so no committed script can reproduce the numbers that justified enabling five production flags. `F-15`: the one precision number pools per-iteration variance and ignores between-game variance, so 3 games with more iterations prints "TIGHT — calibration-grade." `F-03`: the build-smoke suite asserts `profile_count == 0` **passes**, certifying green an engine that finds nothing in the database — the SIM-408 gap, still open.

Fixing the harness after the recompute means re-running the recompute.

**What it unblocks.** Everything. `F-14`'s RISP breakout is the only lens that can read `SIT-GEOM` and `C4`. `F-01`'s toy-bundle lane is the prerequisite for `F-07`. `F-05`'s artifact versioning is the prerequisite for `F-04`'s provenance stamp and `DUCK-LAG-1`'s version assertion.

**Shared gate:** none. `F-01` requires building and committing a small toy artifact bundle — a toy bundle, **not** a production artifact rebuild.

**Exit criterion.** `sim_stats.py` threads defense maps and park factor, prints RISP / advancement / DP-rate / per-pitcher breakouts, reports a **delta** standard error incorporating between-game variance, and stamps flags-under-test + git SHA + artifact id + calibration id into stdout and `--json-out`. A production-config CI lane exists with `SIM_FULL_POOL=1` and every realism flag ON (**no golden generated yet**). The regression gate covers all 11 engines. Coverage scope includes `db/` and `scripts/`. Artifacts carry a version + build id, publish atomically (tmp+rename), and the loader asserts version compatibility. `production_factory`'s two bare excepts ERROR-log and fail boot in production. **Immediately and independently of any fix: mark the five "Validated — no run distortion" labels PROVISIONAL in all five summary documents.**

**Effort:** 1–1.5 weeks.

---

### Phase 2 — The free fixes (no gate)

Five files with twelve findings sit behind no data run at all. Work them in parallel with Phase 1.

**Files:** `simulation/run_resolution.py`, `simulation/game_state.py`, `simulation/batch_runner.py`, `simulation/results.py`, `simulation/pitcher_decisions.py`, `simulation/score_fusion.py`, `simulation/full_pool_sampler.py` (perf only), `api/routes/games.py`, `api/main.py`, `docker-compose.yml`, `deploy/monitoring/`, `db/sim_store.py`, `api/auth.py` (`G8` only), `frontend/*`.

**Order within the phase:**

**2a. `run_resolution.py` first, standalone.** `C-N2` + `C-N5` are two small arithmetic changes in a tiny file, and they **gate the two highest-value Track C fixes**. `advance_state()`'s conservation identity `new_on_base = old + reached − runs` (`:202`) has no retired-runner term and no error-reached term. Land `C3` without `C-N2` and you trade one wrong state for another (4/8 → 2/8 consistent DP shapes). Land `C4` without `C-N5` and every reach-on-error records at RE24 value **0.0** — silently defeating the granular run-conversion calibration `C4` exists to enable (true ≈ +0.38, pre-fix −0.24, post-fix 0.0). **This is the single best-value starting point in the whole plan.**

**2b. `game_state.py` + `pitcher_decisions.py`.** Redocument `home_pitcher_id`/`away_pitcher_id` as *current pitcher*; either give `assert_consistent()` real base-out invariants or delete it — **do not ship `C3` with a guard that reads as protection and provides none** (`C-N7`). Fix the `pitcher_decisions.py` header, which is already false on master.

**2c. `batch_runner.py` + `results.py` + `api/routes/games.py` + `api/main.py`.** `G1a`/`G2`/`G3`/`G11`(lock half)/`G12`. The timeout idiom is already in `batch_runner.py` at `:958` (`timeout=deadline` on the prewarm path) and simply absent from the hot path at `:1099`. `G3`'s fix is *subtractive* — retain the boxscores the workers already pickle back and the entire serial path disappears (~150–220 s → ~38 s), plus `require_auth` on the two routes.

**2d. `full_pool_sampler.py` perf (`E-B2` merge gate) + `AUD-J2/J7` + `AUD-PERF-DEAD` + `AUD-J1`.** All byte-identical or explicitly free: lazy CDF construction (no RNG in construction), delete the zero-caller `_f_situation` at `:209`, one-line early return removing ~6–7 redundant full-pool O(N) passes per PA. `E-B2` must be closed before *any* perf claim — it silently gives back most of the 215 s → 38 s epic.

**2e. Ops/infra:** `I2` prod compose overlay (unblocks the deferred `G5` whenever it is scheduled, and stops the mid-slate pool drop), `I3`/`I8`/`I9` alerting + a `/ready` that submits real work to the pool, `I4` `REPLAY_DUCKDB_PATH`, `I10` pins, `G8`, `I11` reboot runbook.

**2f. Frontend:** `H7` **first** — Vitest + RTL + jsdom, and Playwright renders of the money surfaces *with data*. Then `H1`/`H2`/`H3`/`H8` ship test-guarded. Every one of H1/H2/H3 is a rendering bug a single render-with-data test would have caught. `H8` is the cheapest win in the module — the backend already returns `lineup_ready` and stores `home_score_final`/`away_score_final`; no migration.

**Shared gate:** none. **Exit criterion:** all twelve free simulation findings closed with mutation-checked tests; a live `/simulate` probe confirms `G1a`/`G2`/`G3`; the frontend test lane is green with at least one render-with-data test per money surface.

**Effort:** 2 weeks, parallelizable across 2–3 workstreams.

---

### Phase 3 — Track B: repair the measurement instrument (code only; gate deferred to Phase 9)

**Files:** `betting/clv_engine.py`, `betting/line_movement.py`, `betting/bet_signal.py`, `simulation/prop_distributions.py`, `simulation/win_probability.py`, `scripts/clv_backtest.py`, `pipeline/odds_provider.py`.

**IDs:** `1.EX.push-loss`/`B-N1`/`B-N2`, `H1`(label half) · `1.2-LM`+`B-N8` · `B.PROP-TAIL` (with `B-B1` fixed, and `B-N9` parity) · `AUD-1.4` + `E-RELCURVE`(monotonization→PAVA) · `1.2`/`1.4`/`1.5`/`1.6`/`1.7`/`1.EX.degenerate`/`1.EX.mockfilter`/`1.EX.slate-bias`/`1.EX.devig-method`/`1.EX.devig-books`/`B-N10`/`B-N11`/`B-N12`/`CLV-PAR`/`ARCH-DOMAIN`/`ARCH-MP` (with `B-B2`/`B-B3`/`B-B4` fixed) · `STRAT-GAP` · `audit-B-mock` + `SIM-oddsprovider-1`.

**Why now.** Criterion (c): until the instrument is honest, every expensive run buys a differently-wrong number. And this is all pure arithmetic and naming — zero data cost, unit-testable today.

**Do not cherry-pick these blind.** Three branch fixes are worse than master: `B-B1` (Poisson λ = sample mean → when the sim never observed the event λ→0, the "floor" is 2e-11, nonzero by just enough to escape the degenerate guard, so SKIPPED becomes PLACED at edge **+0.1912** — exactly `1 − fair_under`, the maximum the market allows, in the region where the simulator has zero information); `B-B2` (the new noise gate is **anti-correlated with information** — the floor is 2·SE, SE is largest near p=0.5, so a genuine 5pp moneyline edge at p=0.55 is *rejected* while the phantom 19.1pp tail-prop edge at p≈1.0 is *accepted*); `B-B4` (computes a cluster-robust SE correctly then prints a CI that ignores it — 0.0309 vs the honest 0.0980, and `POWER_FLOOR_BETS=1225` claims 95% power for a ~2pp edge when the correct n is **8,122**). `B-B3`: the beat-close *rate* correctly excludes line-moved bets but the economic `mean_clv_prob` still includes them, smuggling the cross-line artefact back into the headline (honest mean CLV 0.0 prints as +0.100).

**Correct fix for `B.PROP-TAIL`:** a genuine Laplace/Beta pseudo-count over the dense support with a **minimum rate**, keeping the degenerate-information guard as a backstop, plus a test asserting a zero-observation prop is skipped or floored. The branch's smoothing *shape* is sound (PMF sums to 1.0; p_over+p_under+p_push == 1 at every line across 5 prop shapes, 0 violations; worst central-mass shift 0.26pp) — only the rate is wrong.

**One factory, one flag, defaulted identically** for `B-N9`: today `clv_backtest.py:1246` would smooth while `api/routes/games.py:1701` and `validate_props.py:227` would not — any prop edge the backtest reports would not be achievable from the prices the API serves.

**Also required and unglamorous:** `B-N10` — the new headline is **not comparable** to the historical ~49%. Holding beat-count fixed, 49% becomes 54.4% at a 10% push share and 70.0% at a 30% share, with the null still 50%. Publish both conventions or the "improvement" will be an artefact.

**What it unblocks.** Phase 9's terminal read. `STRAT-GAP` (report raw-pick **and** strategy-mode) must be built now so it rides the same run.

**Shared gate:** none for the code; `clv` for confirmation, which is Phase 9.

**Exit criterion.** `edge` is push-aware and a calibrated sim on an integer total yields a non-negative edge on exactly one side. `line_moved` exists in both the backtest and `line_movement.py`, and moved-line markets render as an explicit **excluded** state rather than vanishing. The tail floor has a minimum rate and a zero-observation prop is provably skipped or floored. `clv_backtest.py` loads the calibration map. `is_mock = FALSE` filters both odds reads and the provider default is no longer `mock`. `prefer_book_id` is threaded through `get_odds_provider`. A test puts **more than one bet in the same `game_pk`** so `_clustered_se` is actually exercised. Both headline conventions are printed.

**Effort:** 1.5–2 weeks.

---

### Phase 4 — Track C: the simulator (code only; feeds Phase 7)

**Files:** `simulation/sim_loop.py` (+ `snapshots.py` as blast radius). **New ticket ID: `SIM-443`** — SIM-439 was consumed by the Data Lab on master and BACKLOG.md line 12 records it DONE.

**IDs:** `C1` (+`C-B1`/`C-B2` merge gates), `C2`, `C3` (+`C-N2`,`C-N3`), `C4` (+`C-N5`), `C5`, `C6`, `C7`, `C-N1`, `C-N4`, `C-N6`, `C-N8`, `C8`–`C12`, `AUD-HFA`, `AUD-PARK-HR`, `AUD-J1`, `AUD-ARCH-SIMLOOP`.

**Why now.** Criterion (a), blast radius: this is the top of the data chain that produces every traded number. `C1` is live in production right now with `SIM_MANAGER=1` ON — `_maybe_pull_starter` mutates only `state.pitcher_id` while `_set_half_matchup` re-reads `home_pitcher_id`/`away_pitcher_id`, set once at game build and never updated. **Every pitcher K/BB/ER/OUTS prop PMF in every traded market was built on a reliever carousel ending in illegal re-entry.** `C4` (ROE→out) is the highest-value single fix — "plausibly the largest single piece of the run gap," predicted +0.25–0.35 R/team-game, with the signature *exactly rate stats right, runs low*, which is the tracked SIM-429 symptom.

**Ordering inside the phase.** `run_resolution.py` is already done (Phase 2a). Then: **fix `C-B1` and `C-B2` in one edit** — both live in the same ~15 lines around `sim_loop.py:3086-3100` — **and before any `sim_stats` re-measure**, because `C-B1`'s fix changes steal volume by ~**10×** (0.0764 vs 0.0080 attempts/opp over 40,000 opportunities). Then `C1`+`C2`, then `C3`+`C-N2`+`C-N3` together, then `C4`+`C-N5` together, then the rest. `C11` (pitcher-conditioned contact) is blocked on `D-2` — hold it for Phase 5.

**Follow the proven pattern.** Multi-lens adversarial review of the whole file first (the ETL review found 13 net-new defects the register missed — expect the same here). Batch by shared gate. One mutation-checked test per fix. **Adversarial re-audit of the fixes themselves** — that step caught a regression the first time and it will again.

**What it unblocks.** The calibration refit (Track C alone moves the run environment 7.653 → 8.207 R/G, **+7.2%** over 300 games — `C-N10` — and both `/data/calibration.json` and the win-prob reliability curve were fit on the pre-fix environment). And the golden.

**Shared gate:** `cal-refit`, jointly with `run_resolution.py`, `game_state.py`, `prop_validation.py`, `win_probability.py`, `situation_similarity.py`, `tests/regression/`.

**Exit criterion.** All 29 findings closed with mutation-checked tests. `AUD-QA-PRODPATH` closed: the four production methods (`:1332`, `:1386`, `:1507`, `:3091`) have real test references in the Phase-1 production-config lane. `sim_stats.py` at ≥400×≥20 shows a run-environment move consistent with `C-N10`'s +7.2%, read on the **per-channel** breakouts, not the global R mean. **The `simulate_game` golden is generated as the CLOSING step of this phase, never the opening one** — regenerating first freezes the bugs into the fixtures.

**Effort:** 3–4 weeks. This is the largest single file of work in the plan.

---

### Phase 5 — Track D + Track E: the data layer and the leak (code only; feeds Phase 7)

**Files:** `pipeline/batch/player_profile_computor.py`, `pipeline/batch/engine_artifacts.py`, `similarity/engines/{pitcher,batter,situation}_similarity.py`, `simulation/full_pool_sampler.py`, `simulation/prop_validation.py`, `simulation/production_factory.py`, `similarity/backtesting/`, `db/migrations/duckdb/`.

**IDs — Track D:** `D-1`, `D-2`, `D-3`, `D-4` (**rewritten onto `stand`**, closing `D-B1`), `D-M1`, `D-N1`, `D-N3`, `D-N7`, `audit-DE-4`, `audit-DE-5`, `F-06`, `F-10`, `audit-GAP-outcomepool`.
**IDs — Track E:** `E-LEAK (a)` + `E-LEAK (b)`, `E-ZFILL`, `E-MISSING-1.0`, `E-1`, `E-1-GLOBAL`, `E-1-RESIDUAL`, `E-EB`+`E-N1`, `E-N2`, `E-N5`, `E-CAL-SIGMA`+wiring, `E-CAL-ARSENAL`, `E-ESS`, `E-RELCURVE`×3 (+`E-B1` **hard gate**), `E-N4`, `SIT-GEOM`, `AUD-SITKERNEL`, `audit-DE-3`, `audit-GAP-schemalag`, `DUCK-LAG-1`, `audit-PERF-J5/J6/J9`.

**Why now.** Criterion (a) again — these columns feed every embedding, every artifact, every sampled play. And criterion (c): **`E-LEAK` is the only critical that invalidates results already published.** Win-prob ECE 0.047, the "bettable" H/HR/TB ECE 0.02–0.05 labels, and the entire CLV read were all produced with future data in the sample. A correct expanding-window splitter already exists at `similarity/backtesting/recency_walk_forward.py` with zero callers anywhere in the repo — every grep hit is documentation. **Until as-of-date scoping lands, re-running validation buys a different wrong number, not a right one.**

**Two atomic units inside this phase, both non-negotiable.**
1. `full_pool_sampler.py` + `engine_artifacts.py` + `{pitcher,batter}_similarity.py` are **one atomic unit** for `E-1`/`E-CAL-ARSENAL`/`E-CAL-BATTER`/`E-ZFILL`. `E-B3` is the reason: the four weight-moving changes bite at *different* times — `E-MISSING-1.0` is live on merge while `E-ZFILL`/`E-1`/`E-CAL-ARSENAL` bite only on the next artifact rebuild — so production would pass through a third, never-validated hybrid state. **`c551f8d` made this worse:** the bat_hand/stand correction changed what `build_battedball_pool_artifact` filters on (~1 batted ball in 8 was being excluded), so the next rebuild moves the batted-ball pool for a reason unrelated to Track E. Sequence the rebuild **once, atomically, covering both changes**, or the run-environment delta is unattributable.
2. `E-N2` must land **with** any `E-CAL-BATTER` work: `EngineArtifacts.load` builds `actor_emb` from a fixed key set at `:743`/`:753` that never includes `weights`, so the calibration seam would be a **silent no-op**.

**Decide `E-N5` explicitly.** `RBFSimilarity.score` **masks** a missing dimension (`nan_to_num(diff, nan=0.0)` = "no evidence"); the mean-fill **penalizes** it. Two contradictory missing-data conventions in one codebase. Pick one, document it, and make `np.nanmean` not warn on all-NaN columns.

**`SIT-GEOM`/`AUD-SITKERNEL` belongs here, not later.** Exact-stratify on `(outs, runners_state)` the way the count buckets already do — the proven in-repo pattern. It changes RISP conditioning, so it moves the run environment and must land inside the single calibration pass. `situation` is one of the 5 engines the golden gate covers, so this is the one place the regression gate will actually bite.

**`E-B1` is an absolute gate.** Do **not** run `make validate-props --write-calibration` against branch code under any circumstances until `E-B1` is closed — that command writes the defective curve into `/data/calibration.json`, which is then loaded at `api/main.py:236` **and** in every CLV worker. Measured P(map(0.90)==1.0): 0.620 at n=60, 0.640 at the full 2,378-game slate (master: 0.02/0.00). **It does not wash out with sample size.** Root cause: anchoring at `[0.0, uy[0]]`/`[1.0, uy[-1]]` — the fitted end-block values, and terminal PAVA blocks are frequently a single observation — instead of `[0,0]`/`[1,1]`. Where it stops short of degenerate it is worse because it is silent: seed 0, n=60, p=0.95 maps to **0.4444**, and the model then bets the dog.

**`F-06` is the cheap insurance that pays for itself.** A `_validate_outputs()` that **raises** on out-of-band DP-rate / K-rate / BABIP / row-count / park-factor values would be the first `raise` in 5,238 lines. Without it, the DP-rate bug class (0.0 rates shipped for months, then a 5.7-hour recompute) can and will ship again. Add it **before** the recompute, so the recompute is self-checking.

**Shared gate:** `recompute` (D) and `artifacts` (E) — both fire in Phase 7.

**Exit criterion.** All D-track SQL corrected and unit-tested against `:memory:` DuckDB per `F-03`. `D-4` keys on `stand`, both platoon legs carry `p_throws`, and a test asserts `pull_rate_vs_l ≠ pull_rate_vs_r` on a fixture with both hands. `E-LEAK`: `last_n_seasons` takes an `as_of` parameter, profiles are as-of-scoped, and **a test asserts a backtest of season S samples zero plays from seasons > S**. `E-B1` closed with a test that does *not* clip predictions (the branch's hollow test clips, putting 12.5% of samples at exactly 0.0 and 12.1% at 1.0, making the failure mode structurally impossible — "that is why ~2400 tests are green over a live-money defect"). `_validate_outputs()` raises. `E-N4`'s bare `NaN` JSON literal is fixed.

**Effort:** 3–4 weeks, parallel with Phase 4 (clean file partition: D/E own `pipeline/batch/` + `similarity/`, C owns `sim_loop.py`).

---

### Phase 6 — The odds and bullpen corrective ingests (parallel track; start during Phase 3)

**Files:** `pipeline/bettingpros_odds_provider.py`, `scripts/load_historical_odds.py`, `pipeline/live/bullpen_availability_ingest.py`, `db/migrations/versions/0018_...`.

**IDs:** `1.1`, `1.8`, `1.1 (b)`, `B-N3`, `B-N4`, `B-N5`, `B-N6`, `B-N7`, `SIM-448-MIG`, `audit-DE-7`/`(b)`, `G4`.

**Why now.** External-API wall-clock: 2,378 games of odds plus 21,612 games of bullpen roster/IL calls. Start it early; it does not contend with the profile chain for CPU, and `G4` (the API pricing off mock by default) cannot be fixed until real odds are right-game.

**The register's required order, quoted and non-negotiable:**
1. **Verify the provider's `scheduled` timezone against ONE real BettingPros payload (`B-N6`) — if it is Eastern-local rather than UTC, the guard rejects 100% of events and the re-backfill writes zero odds.** This is a five-minute check. Do it before anything else in this phase.
2. Wire `prefer_book_id` through `get_odds_provider` (`SIM-oddsprovider-1`, done in Phase 3) or the "pin one book" half of `1.8` is not delivered.
3. Complete the DB side (`B-N3`): migration 0018 adds `updated`/`book_id`, **and `_persist_odds`/`_persist_prop_odds` must actually write them and the CLV readers must select them.** Fix `B-N4` in the same edit — the provenance stamp is written by a shared closure overwritten as the code walks moneyline → run line → total (`:430-435`), so a moneyline row can carry the total's `updated`/`book_id`, defeating the entire purpose of the new columns.
4. **Purge or quarantine the pre-fix rows, THEN re-backfill, THEN re-audit** that the wrong-game signature falls to the ET ~0.1% baseline. `B-N5` is the trap: `load_historical_odds.py:172` **skips** persisting rather than overwriting, so a game the new guard rejects simply *keeps its wrong-game row* as the latest row the backtest reads. **A re-backfill without a purge repairs nothing.**
5. `B-N7`: the ±2h guard also rejects legitimately rescheduled and suspended/resumed games, silently shrinking and biasing the slate with only a warning. Handle those explicitly.
6. **Bullpen re-ingest**, separately and in parallel: fix `:196` to `officialDate`, land `audit-DE-7 (b)` (retry/backoff — today a transient failure is negatively cached as `_meta_cache[game_pk] = None` for the life of the run) **before** the sweep, or a flaky MLB-API response silently drops games from the corrective sweep exactly as it did from the original one. Then re-ingest all 21,612 games.

**Exit criterion.** Identical-open-AND-close rate in CT/MT/PT falls from 26.1/43.3/43.2% to the ET ~0.1% baseline. Every persisted odds row carries a correct `updated` and `book_id` and a documented guarantee that the "closing" quote predates first pitch. `raw.game_bullpen_availability` re-ingested for all 21,612 games with a measured row count. `G4` closed — `/edges`, `/signals` and `/api/odds/{pk}` read `raw.game_odds` with `is_mock = FALSE`.

**Effort:** 1 week of code + 3–5 days of ingest wall-clock.

---

### Phase 7 — **THE BIG RUN.** One migrate, one sweep, one recompute, one rebuild.

**This is the phase where a sequencing mistake costs days.** Nothing in the repo enforces or even documents this chain: the Makefile has no `engine-artifacts` target and `scripts/nightly_ingest.sh` is still `refresh_seasons → player_profile_computor → play_pool_cache` with the artifact build **absent** — even after `c551f8d` edited that script. The most expensive and most order-sensitive step in the whole plan is a hand-typed command. **Fix that as step 0 of this phase.**

**Prerequisite check before firing anything:** Phases 1, 2, 4, 5 complete and CI-green on master. `F-06`'s `_validate_outputs()` in place. Backups from Phase 0 verified by an actual restore drill.

| # | Step | Prerequisite | Cost |
|---|---|---|---|
| 0 | Add `engine-artifacts` to the Makefile and to `nightly_ingest.sh`; document the whole chain in `WORKFLOW.md` | `F-05`, `E-N3` | minutes |
| 1 | `make migrate` — Alembic **0016 + 0017** | none | minutes |
| 2 | **`refresh_seasons(reload=True)` — the 21.6k-game corrective pitch sweep** | step 1 (the pitch INSERT binds `field_assist_6_plus`, which 0017 creates); the 6 ETL residuals fixed, **especially the FK handedness hole**, which will be exercised at 21.6k scale by this very sweep | hours–days |
| 3 | `make profile-computor --full-rebuild` (**~5.7 h**) + `LeagueAverageProfiles.compute` | step 2 **and all of Track D landed** | ~6 h |
| 4 | `python -m pipeline.batch.engine_artifacts --what all` | step 3 **and all of Track E landed, atomically** | ~1 h |
| 5 | `make play-pool-cache` | step 4 | ~1 h |

**Why the sweep must precede the recompute.** `c551f8d`'s own body: *"NOT LIVE: every parser-value fix only takes effect on `refresh_seasons(reload=True)`, and `make migrate` must run first."* And `e19d083`'s: *"the sweep rewrites games one at a time, so mid-sweep the corpus holds BOTH conventions at once — which is why this has to land BEFORE the reload, not after."* **Running the recompute before the sweep means paying 5.7 hours twice.**

**Exit criterion.** The sweep reports a measured (not fabricated) row count with `ReloadShrinkError` never fired and zero games left with `outcome='failed'` unexamined. `_validate_outputs()` passes on every step of the recompute. Artifacts publish atomically with a version, build id and manifest. A fresh worker cold-load logs the artifact build id.

---

### Phase 8 — Calibration, exactly once

| # | Step | Hard gate |
|---|---|---|
| 1 | `make calibrate` | Track C landed (so calibration is fitted once against a **corrected** simulator); `E-CAL-SIGMA` seam wired; Phase 7 complete |
| 2 | **Re-bake `engine_artifacts --what pitcher_sim`** | `E-CAL-ARSENAL` bakes the fitted arsenal scale, so the matrix must be rebuilt *after* calibrate — the two-pass resolution to `E-N3`'s circular ordering |
| 3 | `make validate-props --write-calibration` | **`E-B1` CLOSED** (absolute), `E-N4` fixed, `E-LEAK` landed |
| 4 | `generate_fixtures.py --force` | everything above; **closing step, never the opening one** |

**Exit criterion.** Boot logs `applied fitted calibration to N engines; win-prob map: <curve>`. No bin in the reliability curve has fewer than the new minimum count. `winprob_oos_ece` is computed **out-of-sample** and is finite (not a bare `NaN` JSON literal). `prob_to_american` never receives 0.0 or 1.0. Goldens regenerated for all 11 engines from **calibrated** sigmas, plus a `simulate_game` golden over the committed toy bundle.

---

### Phase 9 — The validation batch that was never run

**IDs:** `F-17`, `E-B3`, `VAL-BAR`, plus `C-N10`'s +7.2% confirmation.

**≥400 sims × ≥20 games, ONE FLAG AT A TIME, for all five production flags** (`SIM_MANAGER`, `SIM_PARK_FACTOR`, `SIM_BB_PLATOON`, `SIM_FIELDER_RBF`, `SIM_FRAMING`). This is the project's own pre-registered bar, set 2026-06-03, and never met for any of them.

Read the **per-channel** breakouts — RISP, advancement, DP rate, per-pitcher ERA/K9/BB9/WHIP — not the global R mean. The precision number must be a **delta** SE incorporating between-game variance. Every run stamps flags-under-test, git SHA, artifact id and calibration id.

**Exit criterion.** Each flag has an OFF→ON delta with an honest confidence interval, at a power where a 5% run suppression would be visible (it was not, last time — and a real distortion did slip through: enabling `SIM_MANAGER` zeroed all stolen bases). The five "Validated — no run distortion" labels are replaced with real numbers or the flag is turned off. Box rate stats within ~4% of MLB and the run environment reconciled.

---

### Phase 10 — The terminal CLV read (`SIM-481`) — the go/no-go

**Fire only when C + D + E-LEAK + all of B are in, CI-green, and Phases 6–9 complete.**

Publish **two** numbers per the `STRAT-GAP` design: the raw-pick scoreboard and the strategy-mode read applying the live `bet_signal` gates and Kelly weights. Publish **both push conventions** per `B-N10` — the new headline is not comparable to the historical ~49%. Print the **clustered** confidence interval, and state the honest power (n=8,122 for a ~2pp edge; 1,225 gives ~22%).

**Exit criterion.** A beat-close figure with an honest interval, computed on right-game odds, with a calibrated win probability, with ties handled explicitly, comparing like-for-like lines, over a slate not dominated by one club (Washington appeared in 81 of 120 games last time). **That number — for the first time — is evidence about the model.**

---

## The gate calendar

| Order | Gate | Fires in | Cost | Must be complete before it fires |
|---|---|---|---|---|
| **1** | Nightly backup + restore drill | Phase 0 | hours | nothing |
| **2** | Ofelia opening/closing line jobs live | Phase 0 | minutes | nothing |
| **3** | Toy artifact bundle committed | Phase 1 | hours | `F-01` lane design |
| **4** | Odds **purge**, then re-backfill (2,378 games) | Phase 6 | 2–3 days | `B-N6` payload TZ check · migration 0018 renumbered · `B-N3` columns written **and read** · `B-N4` stamp closure · `B-N7` reschedule handling · `audit-B-mock` filter · `SIM-oddsprovider-1` |
| **5** | Bullpen re-ingest (21,612 games) — *parallel with 4* | Phase 6 | 2–3 days | `officialDate` fix at `:196` · `audit-DE-7 (b)` retry/backoff |
| **6** | `make migrate` (0016 + 0017) | Phase 7 | minutes | nothing |
| **7** | **Pitch reload sweep (21.6k games)** | Phase 7 | hours–days | gate 6 · the 6 ETL residuals, **FK handedness hole first** |
| **8** | **`make profile-computor --full-rebuild` (~5.7 h)** + `LeagueAverageProfiles` | Phase 7 | ~6 h | gate 7 · **all of Track D** (`D-1`,`D-2`,`D-3`,`D-4`-on-`stand`,`D-M1`,`D-N1`,`D-N3`,`D-N7`,`audit-DE-4`,`F-10`) · `F-06` validator |
| **9** | `engine_artifacts --what all` | Phase 7 | ~1 h | gate 8 · **all of Track E atomically** (`E-ZFILL`,`E-MISSING-1.0`,`E-1`,`E-1-GLOBAL`,`E-EB`+`E-N1`,`E-N2`,`E-N5`,`E-LEAK (b)`,`F-05`) |
| **10** | `make play-pool-cache` | Phase 7 | ~1 h | gate 9 |
| **11** | `make calibrate` | Phase 8 | ~1 h | gates 8–10 · **Track C landed** · `E-CAL-SIGMA` wired |
| **12** | Re-bake `--what pitcher_sim` | Phase 8 | ~20 min | gate 11 (`E-CAL-ARSENAL` bakes the fitted scale) |
| **13** | `make validate-props --write-calibration` | Phase 8 | hours | **`E-B1` CLOSED** · `E-N4` · `E-LEAK (a)`+`(b)` |
| **14** | `generate_fixtures.py --force` | Phase 8 | minutes | gates 11–13 · **Track C complete** — closing step only |
| **15** | ≥400×≥20 validation, 5 flags one at a time | Phase 9 | days | `F-13`–`F-16` · gates 8–14 |
| **16** | **Terminal CLV re-read (`SIM-481`)** | Phase 10 | ~1 day at `--workers 6` | **everything above** |

**Total expensive wall-clock: roughly 2 weeks of runs, spread across ~3 months of code work.** Every one of those runs happens exactly once if this order holds.

---

## Traps

**1. The Alembic double-head is a silent no-op, not a merge conflict.** Both sides declare `revision="0016"`, `down_revision="0015"`, in differently-named files. Git merges cleanly. Alembic then sees two children of 0015, reports multiple heads, and **applies nothing** — silently reverting both the SIM-440 trigger fix and the SIM-448 odds columns while `make migrate` exits 0. Renumber to 0018/`down_revision="0017"` **before** any merge, and re-run `TestAlembicHistoryIsLinear`.

**2. The `player_profile_computor.py` auto-merge is the worst trap in the plan.** The hunks do not textually overlap, so git will merge them silently, producing a file where `pull_relative_spray_angle` is keyed on `stand` at `:4769` and `pull_rate` is keyed on `bat_hand` 2,900 lines above — the `D-B1` defect landing *next to* the master commit that proves it wrong, with no conflict marker. **Never auto-merge that file. Rewrite `D-4` onto `stand` by hand.**

**3. The register's own fix instruction for `D-B1` is obsolete and will re-introduce the bug.** It says to reuse `CASE WHEN bat_hand IN ('L','R') THEN bat_hand ELSE stand END` at `:4645`. `c551f8d` **deleted that idiom on purpose** — `stand` is the per-PA resolved side (`'S'` on zero rows), `bat_hand` is roster-declared (`'S'` on 10.4–13.3%). Anyone following the register verbatim re-creates exactly what `D-B1` describes.

**4. Recompute-before-sweep costs 5.7 hours twice.** Every SIM-440 parser fix is inert until `refresh_seasons(reload=True)` runs, and the sweep rewrites the very `raw.pitches` rows the computor aggregates. The register's chain starts at the recompute; **that is now wrong** — master prepended two steps.

**5. `n_def_sub_late` is the canonical partial-state hazard, and the sweep creates more of them.** Mid-sweep the corpus holds **both conventions at once**. Do not run *any* profile step, artifact build, calibration or measurement while the sweep is in flight. Treat the sweep as an exclusive lock on the whole platform.

**6. Fixing the odds matcher repairs nothing by itself.** `load_historical_odds.py:172` skips rather than overwrites, so a game the new guard rejects **keeps its wrong-game row as the latest row the backtest reads**. Budget the purge, not just the fix. Same shape for the 21,612-game bullpen table.

**7. Check the BettingPros `scheduled` timezone against one real payload before the re-backfill.** If it is Eastern-local rather than UTC, the new guard rejects 100% of events and the re-backfill **writes zero odds** — and you will not notice until the CLV read comes back empty. Five minutes now, days later.

**8. Never run `make validate-props --write-calibration` before `E-B1` is closed.** That command writes the defective curve into `/data/calibration.json`, which is then loaded at API boot **and in every CLV worker**. P(map(0.90)==1.0) = 0.64 at full-slate scale and it does not wash out with sample size. Merging the branch and running that command poisons the live calibration file on a production stack.

**9. Do not merge `C3` with a guard that reads as protection and provides none.** `C3` adds an `assert_consistent()` call after the DP removal; `Bases.assert_consistent()` is a bare non-negative-id check (`C-N7`). Either give it real base-out invariants or delete the call.

**10. Do not merge `C4` without `C-N5`.** Every reach-on-error would record at RE24 value **0.0** — silently defeating the granular run-conversion calibration `C4` exists to enable. True ≈ +0.38; pre-fix −0.24; post-fix 0.0. A wrong number that looks intentional is worse than the original wrong number.

**11. Do not merge `B.PROP-TAIL` as written.** λ = the sample mean means the "floor" is 2e-11 when the sim never observed the event — nonzero by exactly enough to escape the degenerate guard. Measured: SKIPPED → PLACED at edge **+0.1912**, the maximum the market allows, in the region where the simulator has zero information. The one live-money defect in this corpus that would *lose money faster* after the fix than before it.

**12. Do not generate the `simulate_game` golden before Track C lands.** Building the production-config lane early is correct; freezing its golden early **pins the known-bad behaviour into the regression gate**. Two different acts; only the second is harmful.

**13. The artifact rebuild must cover Track E *and* the `c551f8d` batted-ball change in one atomic run.** `c551f8d` changed what `build_battedball_pool_artifact` filters on (~1 batted ball in 8 was being excluded). Rebuild for Track E alone and the run-environment delta is unattributable between two unrelated causes.

**14. Green CI is not a safety signal in this repo.** 2,344 unit + 53 regression + 12 e2e tests, ruff and mypy clean, green over every blocking defect on the branch — including two regressions versus master. `conftest.py` pins `SIM_FULL_POOL` and every realism flag OFF while production runs the inverse; the golden gate imports nothing from `simulation/`; the perf gate benchmarks an rng stub; a build-smoke test certifies green an engine that finds nothing in the database. **Until Phase 1 lands, do not use suite-green as a merge signal for anything in this plan.**

**15. Two ticket-ID collisions will corrupt the record.** The branch's `tests/unit/test_sim439_*.py` (7 files, the sim-bug track) versus master's SIM-439 (Data Lab, BACKLOG line 12, DONE 2026-07-24) — renumber to **SIM-443** before merge. And SIM-442 is now both the SQL-validator fix (shipped) and the deferred D-M2/D-M3 plan ticket.

**16. Re-running the CLV backtest early is the most tempting mistake available.** It is fast, it produces a headline, and the headline will be meaningless. `E-LEAK` alone guarantees it. **Do not re-run for an edge read until Phase 10.** Every intermediate run should be labelled a smoke test in writing, so nobody quotes it six weeks later.