# SIM-432 — Calibrator + validate_props ↔ live-schema reconciliation

*Filed 2026-05-31, executed + closed 2026-06-01. The unlock for SIM-406 engine
calibration + the SIM-407 win-probability reliability curve to go LIVE.*

## Why

SIM-406 (fit + apply a `CalibrationReport`) and SIM-407 (validate prop PMFs +
win-prob, fit the reliability curve) shipped code-complete with passing **unit**
tests, but their fit/validate **scripts were never run against the live
SIM-408-trimmed schema**. Running them surfaced a cascade of script↔schema
mismatches, so the app ran **identity calibration** (`/data/calibration.json`
absent; boot logged `win-prob map: identity`). SIM-432 reconciles the scripts to
the live schema — the SIM-408 "trim/guard" pattern — so the fit + validate jobs
actually complete and the app applies a real calibration.

## Ground truth — the LIVE schema (not the .sql files)

The canonical `db/schemas/02_duckdb_schema.sql` **diverges** from the running DB —
the live DuckDB is the SIM-408-reconciled + all-seasons-rebuilt one. The authoritative
column set was read directly from the running containers
(`baseball_simulator_v2-app-1` DuckDB `/data/baseball_sim.duckdb`, and
`baseball_simulator_v2-db-1` Postgres). Key live facts (2026-06-01):

| table | rows | seasons | note |
|---|---|---|---|
| `derived.batter_season_metrics` | 7773 (4243 above-min) | 2017–2026 | has `first_pitch_take_rate`, `max_exit_velo`, full `*_vs_l`/`*_vs_r` blocks; **lacks `xba`/`xslg`** |
| `derived.pitcher_season_metrics` | 8102 (5927) | 2017–2026 | has all 7 `COMMAND_FEATURES` cols (`bb_rate,k_rate,csw_rate,zone_take_rate,chase_rate,zone_rate,whiff_rate`) |
| `derived.fielder_season_metrics` | 11365 (3600) | 2017–2026 | all IF/OF calibrator cols present |
| `derived.baserunner_season_metrics` | 6392 (1344) | 2017–2026 | `sprint_speed` + success rates **unpopulated/constant** → degenerate |
| `derived.catcher_season_metrics` | 1083 (715) | 2017–2026 | incl. `shadow_/heart_zone_strike_rate` |
| `derived.baserunner_steal_metrics` | 3951 (808) | 2017–2026 | trimmed (no JUMP) |
| `derived.pitcher_steal_metrics` | 8102 (5647) | 2017–2026 | outcome-only |
| `derived.manager_season_metrics` | 327 (305) | 2017–2026 | USAGE cols gated NULL (SIM-427) |
| `raw.games` (Postgres) | 21577 Final | — | final score is **`home_score_final`/`away_score_final`** (NOT `home_score`/`away_score`) |

**Correction to the filed cascade:** items (b)/(c) of the SIM-432 filing said
`first_pitch_take_rate` / `max_exit_velo` / the `*_vs_r` platoon block were absent
from `batter_season_metrics`. They are in fact **present** on the live DB — those
notes predate the all-seasons rebuild. The batter `_opt` guard for `xba`/`xslg`
(commit `ee1188f`) already covers the only genuinely-missing batter columns, so the
batter calibrator needed no further change. The real remaining divergences were
only (a) the pitcher import and (d) the `raw.games` score columns.

## Divergences fixed

1. **Pitcher calibrator import (`ImportError`).** `_calibrate_pitcher_params`
   imported `RESULT_FEATURES` from `pitcher_similarity`, but SIM-067 removed the
   `results` sub-score — the engine exports only `COMMAND_FEATURES` and has just two
   sub-scores (arsenal + command). The old 10-column SELECT (`...ground_ball_rate,
   fly_ball_rate, line_drive_rate, whip, hr_per_9`) no longer matched the engine's
   command vocabulary either. **Fix:** import only `COMMAND_FEATURES`; SELECT exactly
   those 7 columns (by canonical name, behind an `information_schema` guard like the
   batter `_opt`); fit `sigma_command` over them; leave `sigma_results` at the 0.0
   keep-default sentinel (no consumer — the pitcher engine's `apply_calibration` only
   consumes `arsenal_gamma`/`arsenal_median_w2`; the command RBF reads the module
   `RBF_SIGMA_COMMAND`).

2. **`validate_props._fetch_final_games` ↔ `raw.games`.** Selected
   `home_score`/`away_score` (absent) → `UndefinedColumnError`. **Fix:** select
   `home_score_final AS home_score` / `away_score_final AS away_score` and filter on
   the real columns.

3. **Degenerate-sigma safety (regression-proof apply) — beyond the literal cascade.**
   Running the fit revealed 7 sub-scores returning `calibrate_sigma`'s degenerate
   **1.0** fallback (no usable pairwise spread): fielder IF-errors / IF-specialty /
   OF-arm / OF-errors, baserunner speed / success, manager usage. Because every
   engine's `apply_calibration` uses `v if v > 0 else current`, a `1.0` is applied as
   a **real override**. For most it coincides with a `1.000` module default (no-op),
   but **baserunner `RBF_SIGMA_SPEED = 0.8171`** would be silently clobbered to 1.0 —
   a real regression (sprint_speed is unpopulated on the live DB). The 4 SIM-408
   calibrators already guarded this via `_fit_sigma` (returns the **0.0**
   keep-default sentinel on a degenerate matrix); SIM-432 extends the same guarantee
   to the older fielder / baserunner / manager calibrators and hardens it: a new
   `calibrate_sigma(degenerate_value=…)` parameter lets `_fit_sigma` return 0.0 for
   **any** uncalibratable matrix — fully constant OR merely mostly-constant (>half the
   sampled pairs identical), which the previous zero-std check missed. Net effect: the
   persisted report can be applied with **zero silent regressions** — every sigma is
   either a real population fit or the keep-default sentinel.

## Fit results (live, all seasons 2017–2026)

`python scripts/fit_calibration.py --arsenal-sample 30000 → /data/calibration.json`:

- batter: σ_disc 1.049, σ_bb 1.073, σ_plat 1.085, σ_pow 0.659; eb 5.0
- pitcher: σ_command 1.078 (σ_results 0.0 = vestigial keep-default); eb 5.0
- arsenal: median W₂ **2.818** (canonical ~2.84), γ 0.0873 → ARSENAL_SCALE ≈ 4.07
- catcher: framing 0.928 / blocking 0.879 / throwing 0.639 / deterrence 0.756
- steal: BR-tendency 0.711 / BR-success 0.975 / P-outcome 0.829
- manager: aggression 0.846 / platoon 0.969 (usage 0.0 keep-default, SIM-427)
- keep-default (0.0) sentinels: IF-err, IF-spec, OF-arm, OF-err, BR-speed, BR-success,
  Mgr-usage
- batter `--validate` median similarity ≈ **0.461** (target 0.50)

## Win-prob reliability curve (live, SIM-407)

`python scripts/validate_props.py --seasons 2024 --max-games 60 --iterations 20
--write-calibration` (60 games, 3480 prop pairs, ~30 min):

- **win-prob: ECE 0.171, Brier 0.281, log-loss 0.759; 7 reliability anchors fitted**
  and written into `/data/calibration.json` (`reliability_curve`). On the next boot
  `CalibrationMap.from_report` turns it into a monotone p→p win-prob map (it enforces
  monotonicity via a running max, so the noisy dips in the raw 60-game anchors flatten).
- prop over/under calibration (informational, feeds SIM-429 — NOT SIM-432 scope):
  batter **H/HR/TB well-calibrated** (ECE 0.05–0.08, PMF coverage 0.89–0.93 vs 0.80
  nominal); **pitcher K/BB over-predicted** (K ECE 0.52, BB 0.39 — sim says "over" far
  more than reality; PMF coverage 0.41/0.60, i.e. the K/BB PMFs are too narrow). The
  pitcher-prop gap is the same hits→runs/sequencing family tracked in §11, not a
  calibration-pipeline defect.

**Throughput caveat:** the full-pool replay is ~2 s/iteration (the open SIM-430
gap), so the curve is fit over a **bounded** game sample (60 games of 2024). The
anchors are therefore noisy; a fuller multi-season fit is a follow-up batch gated on
SIM-430 throughput. The map it produces is a real (non-identity) monotone correction
either way — the SIM-406→407 loop is now closed on disk and applied at boot.

## Files

- `similarity/similarity_calibration.py` — pitcher calibrator (import + query +
  `sigma_results` keep-default); `calibrate_sigma(degenerate_value=…)`; `_fit_sigma`
  delegates with the 0.0 sentinel; fielder/baserunner/manager routed through it.
- `scripts/validate_props.py` — `_fetch_final_games` final-score columns.
- `tests/unit/test_sim432_calibration_reconciliation.py` — 13 tests.
