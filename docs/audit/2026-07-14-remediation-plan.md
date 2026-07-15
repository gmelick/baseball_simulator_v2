# 2026-07-14 — Remediation Plan

**Scope.** A single, dependency-ordered plan to fix (a) the eight net-new defects found in the 2026-07-14
code-walkthrough review and (b) the 129 findings in
[`2026-07-13-analytics-firm-comprehensive-audit.md`](2026-07-13-analytics-firm-comprehensive-audit.md).
Every finding below was re-verified against the current working tree (HEAD `18c6aac`, 2026-07-14) before
being planned — nothing here is planned off a stale citation.

**How the two sources relate.** The audit and the review agree on the four production simulator bugs
(pitcher resurrection, zeroed steals, phantom-DP runner, reach-on-error→outs) and on the CLV instrument
being broken. The review additionally found **eight defects the audit does not contain** — six of them in
the nightly profile SQL that feeds *every* engine, plus a similarity-scoring bug and two run-attribution
bugs. Those are folded into clusters D, E, and C below and flagged `source: review`.

**The one thing to understand first.** The platform's headline result — *"~49% beat-close = the model has
no betting edge"* — is **not evidence about the model. It is an artifact of a broken measuring instrument.**
The CLV backtest reads the wrong game's odds ~25–35% of the time, compares prices across different betting
lines, prices bets with an uncalibrated win probability, counts unmoved lines as losses, and reports no
error bars on a sample far too small to tell 49% from 53%. Meanwhile the simulator it measures has a
bullpen that doesn't work and scores zero stolen bases. **Fix the instrument and the simulator first, then
re-run the season backtest, then — and only then — decide where model work is needed.** Do not act on the
~49% number, in either direction, until the terminal re-run (the last ticket in this plan) produces a
trustworthy one.

---

## 1. The dependency spine

Everything hangs off one prerequisite and converges on one terminal gate.

```
                          ┌─────────────────────────────────────────────────────────┐
  WAVE 0  ── A (git) ──▶  │  WAVE 1 (four parallel tracks)                            │
  commit SIM-437,         │   C  simulator bug batch (C1–C7)                          │
  gitignore .claude,      │   D  derived-metrics SQL (D-1..D-5, D-M1..3)  ┐           │
  track docs, DoD         │   E  model/calibration + leakage (E-LEAK…)    ├─ one      │
                          │   B  CLV instrument fixes (1.1,1.4,1.5,1.7…)  ┘  recompute│
                          └───────────────┬─────────────────────────────────────────┘
                                          │  calibrate+validate runs the sim → runs ONCE, after C+D+E land
  WAVE 2 (parallel, mostly after A)       ▼
   F testing/self-verification    ┌──────────────────────────┐
   G service-layer/API            │  TERMINAL GATE            │
   I operations/DR/security       │  B.RERUN — season CLV     │  ← blocked on C + D + E-LEAK + all of B
                                  │  backtest on the repaired │
  WAVE 3 (parallel, after A)      │  instrument. Strike ~49%. │
   H frontend                     └──────────────────────────┘
   J performance
```

**Cluster A (version control) blocks every commit in the plan.** Right now `git commit -am` produces a
broken `master` — the two modified ETL loaders hard-import `pipeline/etl/coercion.py`, which is untracked,
and `git clean -fd` (a habitual command on this Docker-disk-constrained host) would permanently delete it.
Nothing else can be CI-adjudicated until A lands. **Do A first, alone.**

**The terminal gate (B.RERUN) is the whole point.** The season CLV re-read is only meaningful once the
simulator bugs (C), the profile SQL (D), the temporal leakage (E-LEAK), and every CLV-instrument fix (B)
are in and CI-green. Until then, the ~49% figure must be struck from every doc as an instrument artifact.

---

## 2. The critical path (longest chain to a trustworthy number)

```
A  ──▶  C (sim bugs)          ─┐
A  ──▶  D (profile SQL) ─ recompute ─┐
A  ──▶  E-LEAK (as-of bundles)      ─┤──▶  make calibrate + validate-props (ONE run, post-C/D/E)  ──▶  B.RERUN
A  ──▶  B (instrument math + book-pinned re-backfill) ─────────────────────────────────────────────┘
```

Everything on this path is either a small diff or a batched recompute; the wall-clock cost is dominated by
(1) the one ~5.7-hour profile recompute after D lands and (2) the season backtest network/compute at the
end. **The measurement fixes (B) and the four sim bug fixes (C) are the highest-value, lowest-effort work
in the entire plan and should start the moment A lands.**

Everything in Waves 2 and 3 (testing, API, ops, frontend, perf) is off the critical path and can proceed
in parallel — none of it gates the CLV re-read, and the CLV re-read does not gate it.

---

## 3. Wave 0 — Unblock the repo (Cluster A) · do first, alone

| Ticket | Ref | Sev | Fix | Effort |
|---|---|---|---|---|
| **SIM-438** | A3 | low | Append a `.claude/` block to `.gitignore` **before any staging** (it currently holds a local Bash-permission allowlist that `git add .` would publish). | S |
| SIM-438 | A1/A2 | high | Branch off `master`; stage the seven SIM-437 files **by explicit path** (`coercion.py` + the 2 loaders + the test + BACKLOG/CHANGES/CLAUDE doc-syncs); commit referencing SIM-437; push; open a PR so all 8 CI jobs adjudicate the never-CI'd code production already runs via bind mounts. Committing `coercion.py` dissolves the `clean -fd`/`commit -am`/`checkout .` data-loss traps. **Never `git add .`/`-A`.** | S |
| SIM-438 | A4 | low | Track `docs/CODE_REVIEW_CHECKLIST.md` (a cited SIM-437 deliverable) and the audit doc by explicit path. | S |
| SIM-438 | A5 | med | Amend the CLAUDE.md definition-of-done: *a ticket is not CLOSED until its commit hash is on `master`, CI is green on it, and the hash is recorded in the BACKLOG row.* Backfill SIM-437's hash. Optional: a `check_file_integrity.py` guard failing any `DONE` BACKLOG row lacking a hex hash. | S |

**Operational guardrail until A1 pushes:** do **not** run `git clean -fd`, `git stash`, `git checkout .`,
`git restore .`, or `git commit -am` on this tree — any of them destroys `coercion.py` or the uncommitted
NaN-fix and breaks the bind-mounted container's next ETL run.

**Revalidation:** CI green on the pushed SIM-437 commit *is* the revalidation. No fixtures/embeddings regenerate here.

---

## 4. Wave 1 — Restore trust + fix the model substrate (four parallel tracks)

All four tracks start together once A lands. They touch different files/owners and converge only at the
single `calibrate + validate-props` step and the terminal CLV re-run.

### Track C — Simulator bug batch (deterministic fixes C1–C7) · one ticket, one fixture regen

These are the bugs that contaminate the traded pitcher/batter prop markets. **C1–C7 land as one ticket**
(suggest **SIM-439**) so the RNG change (C7) forces exactly one golden regeneration.

| Ref | Sev | Src | Defect (current loc) | Fix | Effect |
|---|---|---|---|---|---|
| **C1** | crit | both | Pulled pitchers resurrected every half-inning — `_maybe_pull_starter` sets only `state.pitcher_id` (sim_loop.py:3216); `_set_half_matchup` (2734/2737/2750) re-reads the never-updated `home/away_pitcher_id`. | On a pull, write the reliever into the **defending team's** slot (`home/away_pitcher_id`), repurposing those fields from "starter" to "current pitcher" (grep-confirmed they're read only by `_set_half_matchup`). | Pitchers/game **~9 → ~4–5**; no illegal re-entry; pitcher K/BB/ER/OUTS PMFs stop averaging a phantom carousel. **Re-measures the SIM-434 "2→9.25" claim, which is an artifact of this bug.** |
| **C2** | high | both | `SIM_MANAGER` on → the engine steal path is only reached when green-light ≤ 0; a real profile's green is 0.04–0.12, routing to a resolver stub that returns `attempted=False` → **zero steals since 2026-06-04**. | Fall back to `_full_pool_steal_decision` whenever the resolver stages nothing (keep a real injected resolver winning). | Restores ~0.7 attempts/game; fixes SB/CS/R props; corrects the misread "no distortion" enablement (it was losing ~0.6 SB/game). |
| **C3** | high | both | GIDP records 2 outs but never removes the doubled-off runner (`_full_pool_out_advancement` DP early-return 1519; per-tile 2363) → phantom runner. | Add a shared `_resolve_double_play` that clears the retired forced runner in **both** paths before commit; `assert_consistent()` after. | Removes ~+0.03–0.05 R/team-game of inflation that *masks* the real deficit; fixes per-runner R/ER. |
| **C4** | high | both | Reach-on-error → out: pool `field_error` rows carry `hits=0`; `_full_pool_fielding` infers `outs = 0 if hits>0 else 1` → every error becomes a 1-out field-out. | Special-case error events (batter reaches, `outs=0`, `is_error=True`) through the existing `_force_on_reach` + SIM-414 unearned-run machinery; guard the hit credit `and not is_error`; mirror in the per-tile path. | **+0.25–0.35 R/team-game** — the concrete *"rate stats right, runs low"* (SIM-429) signature, invisible to rate-stat validation. |
| **C5** | med | review | `runs_scored = int(result_runs)` **overwrites** (=) while outs use `+=` (sim_loop.py:1606) → a steal-of-home then a scoring PA on one pitch loses the steal run from linescore + pitcher R/ER. | `result.runs_scored = int(result.runs_scored or 0) + int(result_runs)`. | Corrects pitcher R/ER on terminal steal-of-home pitches (team score already correct). |
| **C6** | low | review | RE24 start-state read **after** base mutation — `_resolve_walk`/`_full_pool_out_advancement` mutate `state.bases` before `_commit_run_delta` reads `state.runners_state` as the RE24 "start". | Add `runners_state_override`/`outs_override`; callers snapshot pre-mutation state and pass it. **Land with C3/C4** (their new pre-commit mutations need the same snapshot). | `re_start`/`re_end`/`run_resolution_method` become truthful for any run-conversion calibration. |
| **C7** | low | review | `simulate_game` seeds four RNGs with the *same* integer → correlated pitch/advancement/steal streams. | `ss = SeedSequence(seed); c = ss.spawn(4)`; seed each generator from a distinct child. Preserves per-`(game,seed)` determinism. | Decorrelates the Monte-Carlo draws; corrects prop-PMF variance/joint structure. |

**Realism-mechanic sub-track (C8–C12) is model work, not the deterministic batch** — do it *after* C1–C7
land so the run-gap baseline is clean, and gate C9/C10/C11 on the C4 re-measure. Suggest **SIM-439a/b…** or
a fresh block:

| Ref | Sev | Fix summary | Blocked by |
|---|---|---|---|
| C8 | med | Add HBP / WP / PB / balk / pickoff channels + wire the production dropped-3rd-strike hook (~0.15–0.25 R/team-game). | C1–C4 |
| C9 | med | Advance R1 on productive ground outs; model the fielder's-choice correctly (retire the lead forced runner); recalibrate the 0.28/0.35 advancement constants vs Retrosheet. | C4 |
| C10 | med | Apply TTO/fatigue as a monotone tilt on the pitch-outcome distribution (today they only time the pull) → restores hit-clustering that per-channel rate calibration structurally cannot recover. | C4 |
| C11 | med | Condition the batted-ball draw on the pitcher's GB/FB profile via `_f_pitcher`. **Consumes the pitcher GB/FB column that D-2 fixes — D must land first.** | C4, D-2 |
| C12 | low | Make manager small-ball real (sac bunt / pitch-out / hit-and-run) *or* stop emitting the decorative decisions the frontend narrates. | — |

**Revalidation (Track C):** regenerate any seed-pinned sim fixture (C7 changes every seeded game) — and
create the **first `simulate_game` golden** (there is none today) as part of this batch. Re-run
`scripts/sim_stats.py` at ≥400 iters × ≥20 games after each of C1/C2/C3/C4 with `SIM_MANAGER` +
`SIM_FULL_POOL` on and confirm the predicted per-channel moves. **Do not** re-run the CLV backtest for an
edge read until the whole plan's terminal gate.

### Track D — Derived-metrics SQL correctness · code edits in one branch, recompute ONCE

**All six of D-1..D-4 + D-M1 are net-new from the review (the audit does not contain them).** They are
wrong-code / wrong-denominator / handedness-blind / inverted SQL in the nightly profile computor — the
substrate for every engine, the league-average fallbacks, and the actor embeddings that weight the
production sampler. Each is a small diff; the honest cost is the shared ~5.7-hour recompute, which must run
**once**.

| Ref | Sev | Src | Defect (loc in `player_profile_computor.py`, unless noted) | Fix |
|---|---|---|---|---|
| **D-1** | high | review | `whiff_rate = SUM(type='C')/COUNT(*)` (1625) — `'C'` is a **called** strike, so this is called-strike rate, not whiffs. | Numerator → swinging codes `IN ('M','O','S','T','W')` (a per-pitch SwStr% consistent with `csw_rate`); decide per-pitch vs per-swing with the ML owner. |
| **D-2** | med | review | GB/FB/LD rates divide by `SUM(type='X')` (outs-only) (1646–1651); the numerator includes hits → can exceed 1.0. | Denominator → `type IN ('D','E','X')` (the batter version 20 lines down is already correct). |
| **D-3** | med | review | `first_pitch_take_rate` numerator is the **swing** predicate (1840) → it stores first-pitch *swing* rate (inverted). | Invert to the no-swing complement `type IN ('B','C','H','P','*B')`. |
| **D-4** | med | review | Pull/oppo handedness-blind (fixed `spray_angle` sign, 1876/1879); `pull_rate_vs_l ≡ pull_rate_vs_r` (no `p_throws` filter, 1928–1961) with the wrong `type='X'` denom; barrel splits share the wrong denom. | Use the handedness-signed angle already proven in `_build_outcome_pool` (4742–4746); add `p_throws` to both split legs; denom → `type IN ('D','E','X')`. |
| **D-5** | med | review | **RBI always 0** — `runner.get("rbi")` reads the wrong dict level (`etl_historical_loader.py:769`); the adjacent `earned` correctly reads `runner["details"]`. | `runner["details"].get("rbi", False)`. **Companion:** fix the test fixture at `test_etl_historical_loader.py:1633` (it encodes the bug) + add the RBI assertion + a negative test. Separate raw-side track; re-run the ETL/backfill. |
| D-M1 | med | review | RE-matrix undercounts — `MAX(pre-PA score)` misses runs on the inning's last PA. | Anchor on the inning's final (post-event) score. |
| D-M2 | med | review | Park factors use venue-vs-league, not a home/road differential → the home team's offense confounds the "park". | Rebuild on the home/road-differential construction; needs analyst sign-off; coordinate with the audit's park-consumer fix (no HR channel, C-adjacent). |
| D-M3 | med | review | `spin_axis` (circular 0–360°) treated as linear in the GMM + FAISS. | sin/cos encode (or drop); triggers a pitch-pitch FAISS rebuild + arsenal re-check. |

**Ticket split:** **SIM-440** = D-1..D-4 + D-M1 (Wave 1, one recompute); **SIM-441** = D-5 (RBI, decoupled
raw-side); **SIM-442** = D-M2 + D-M3 (fold into the same recompute if the FAISS rebuild is acceptable in the
pass, else a clean second wave). PM confirms the next free ID at commit time (siblings also claim from 438).

**Revalidation (Track D) — the expensive shared step, run EXACTLY once:** `make profile-computor` (all
seasons) → `LeagueAverageProfiles.compute` → rebuild engine artifacts (actor embeddings + pitcher-sim
matrix + hand pools; D-M3 also rebuilds the pitch-pitch FAISS + `make play-pool-cache`) → **`make
calibrate` + `make validate-props --write-calibration` run *after* Track C lands** so calibration is fitted
once against a corrected simulator → `generate_fixtures.py --force` for covered goldens (honest caveat:
today's gate covers 0 of pitcher/batter/fielder — F-02 closes that). Add the `:memory:` schema-contract
tests (F-03) so this class of bug can't silently recur.

### Track E — Model / calibration + look-ahead leakage · batched bundle rebuild

| Ref | Sev | Src | Defect | Fix |
|---|---|---|---|---|
| **E-LEAK** | crit | audit | 2024 backtests sample 2025/26 plays and up-weight them 2.0; actor profiles are within-season aggregates. An expanding-window splitter exists (`recency_walk_forward.py`) but no live/validation caller. | As-of-date artifact bundles (`last_n_seasons(as_of)` excludes seasons > as_of); anchor recency to the simulated season; prior-season / season-to-date actor profiles for replays; route the validation/CLV path through as-of bundles. **This is the CLV-rerun gate on the model side.** |
| **E-1** | high | review | No-GMM pitcher redistribution factor is `1/0.35 ≈ 2.857` (pitcher_similarity.py:1441) → every low-usage pitcher's composite saturates to ~1.0 (near-perfect match to everyone). Also a module-global mutable `ARSENAL_SCALE`. | Set the convex-average factor to **1.0** (composite = command). Thread `self._arsenal_scale` into the score paths; stop mutating the module global. **Re-litigates SIM-346 — needs ML + Baseball Analyst sign-off; file a new SIM-NNN, don't silently revert a tested ticket.** |
| E-CAL-ARSENAL | med | audit | Pitcher-sim artifact built without `apply_calibration` → bakes default `ARSENAL_SCALE 4.10` vs fitted 4.0655. | `apply_calibration(report)` in `build_pitcher_sim_matrix`; stamp the calibration id into the npz. |
| E-CAL-SIGMA | med | audit | Full-pool sampler uses hard-coded `sit_sigma=2.0`, `batter_sigma=3.0` — never fitted. | Fit them to the 0.50-median target; thread from `production_factory`; record in provenance. |
| E-CAL-BATTER | med | audit | Batter factor is a uniform-weight RBF over **all** numeric embedding columns, discarding the engine's weighted 4 sub-scores. | Export a reliability-weighted batter embedding restricted to the engine's features; `_batter_affinity` unchanged mathematically. (Blocked by E-ZFILL.) |
| E-MISSING-1.0 | med | audit | Unprofiled pool rows get factor weight **1.0** (self-similarity max) → call-ups ~2× over-sampled. | Substitute the profiled-rows mean, not 1.0, for unprofiled candidates. |
| E-ZFILL | med | audit | Embeddings 0-fill missing values **before** z-scoring → a missing `max_exit_velo` becomes z ≈ −21. | Mean-impute before persisting (or persist NaN + impute-to-mean at load). |
| E-RELCURVE | med | audit | Reliability curve: `min_bin_count=1` (1-game anchors); running-max monotonization ratchets only up; fit ⊂ eval. | Isotonic/PAVA on raw `(pred,outcome)` pairs; raise the bin floor; fit on a held-out split and persist out-of-sample ECE. |
| E-EB | med | audit | Batter EB shrinkage inert (`N_PRIOR=5` under a 100-PA floor → α≥0.95); calibrated `eb_n_prior_*` stored but consumed by nothing. | Thread the calibrated prior into the engine at `build()` **or** delete the dead field — ML owner decides. |
| E-ESS | low | audit | Product-of-kernels weight has no ESS diagnostic/tempering (correlated factors double-count). | Emit per-PA ESS `(Σw)²/Σw²` (byte-neutral); optional fitted tempering β behind a flag. |

**Ticket split:** **SIM-443** = E-1 (+ global-scale hardening; sign-off gate); **SIM-444** = the
bundle-touching calibration fixes (E-CAL-ARSENAL, E-CAL-SIGMA, E-CAL-BATTER, E-ZFILL, E-MISSING-1.0) as
**one bundle rebuild**; **SIM-445** = E-RELCURVE + E-EB + E-ESS; **SIM-446** = E-LEAK. Batch E-1 / ARSENAL /
ZFILL / BATTER into the single bundle rebuild to avoid a half-updated bundle.

### Track B — The CLV measurement instrument · the gold-standard metric

Group the pure-scoreboard math (testable on the persisted 120-game records, **no re-sim**) from the
structural odds-provenance work.

**B-math (SIM-447) — land first, days of effort:**

| Ref | Sev | Defect | Fix |
|---|---|---|---|
| **1.5** | high | Unmoved lines (`clv_prob==0`) counted as **losses** (clv_backtest.py:379) — 203/4626 (4.4%) of the real report. | Three-way beat/**push**/lose; rate over decisive bets only; report `mean_clv_prob` ± SE. **Re-aggregating the existing JSON alone moves the headline 48.81% → 51.05% and flips HR 48.9% → 53.7%.** |
| **1.4** | high | Backtest prices the moneyline with the **identity** (uncalibrated) win-prob (clv_backtest.py:876) while production uses the fitted curve. | Load the `CalibrationReport` in worker init; pass `calibration_map`; stamp the map name. |
| **1.7** | high | No CIs / minimum-n anywhere; the docstring hard-codes 52–55% thresholds inside a ±8.9pp band. | Wilson CI per row; **game-level clustering** for pooled rows (within-game bets are correlated); flag rows below a power floor (~1,225 bets/market) `UNDERPOWERED`. |
| **1.6** | high | `n=100` + `min_edge=0` bets on Monte-Carlo noise (±5pp); two-way edges are exact complements so every market places a bet; winner's curse. | Place only when `\|edge\| ≥ max(min_edge, 2·SE)`; default `--min-edge 0.02` for the strategy read; report the `\|edge\|>2SE` share. |
| 1.EX.degenerate | med | Degenerate 0/1 sim probs silently skipped — the model's biggest claimed edges never enter the scoreboard. | Root-fix via B.PROP-TAIL; belt-and-suspenders clamp to `[eps,1-eps]`; count `n_degenerate`. |
| 1.EX.mockfilter | med | Backtest reads odds with **no** `is_mock`/source filter. | `AND is_mock = FALSE` (+ pinned source) on both odds reads. |
| 1.EX.push-loss | med | Push mass charged as a full loss in EV/Kelly. | `EV = p_win·b − p_lose + p_push·0`; feed push-aware EV into Kelly. |
| 1.EX.slate-bias | med | `--max-games` takes a pk-ordered prefix (WSH in 81/120). | Deterministic hashed random sample or stratify by team/month; report the slate composition. |
| 1.EX.devig-method | med | Proportional de-vig only; sensitivity unmeasured. | Add Shin/power behind a param; report the CLV sensitivity band. |

**B-odds (SIM-448 → SIM-449 → SIM-450 → SIM-451) — structural, gates the re-run:**

| Ref | Sev | Defect | Fix | Effort |
|---|---|---|---|---|
| **1.1** | crit | BettingPros resolves on the **UTC** `gameDate` (bettingpros_odds_provider.py:138), not `officialDate` → wrong-game odds in ~26% CT / ~43% MT/PT pairs; DHs always take game 1's odds. Same bug in `bullpen_availability_ingest.py:196`. | Use `officialDate`; disambiguate DHs by nearest scheduled first pitch; assert matched event within ~2h of first pitch else skip; **re-run the 2024 backfill and re-audit** (the wrong-game signature must fall to ~ET baseline ~0.1%). | M |
| **1.8** | high | "Closing line" = max-`updated` scanned across **all** books per selection, `updated` discarded, no `≤ first-pitch` guard, persisted as `book='consensus'`. | Pin one sharp book for both endpoints (kills the cross-book de-vig, 1.EX.devig-books too); Alembic migration adds `updated TIMESTAMPTZ` + `book_id` to `raw.game_odds`/`raw.prop_odds`; thread the `updated` stamp; add the first-pitch guard; re-backfill; spot-check ~50 games. | L |
| **1.2** | crit | CLV compared across **different lines** on 9/10 markets (clv_backtest.py:633/748) — a total 8.5→9.0 scores ~0. Same in `line_movement.py:417-428`. | Phase 1: store open+close line, flag `line_moved`, exclude + report the moved rate. Phase 2: credit the move through the model PMF. Mirror into the live `/line-movement`. Add the open-8.5/close-9.0 test. | M |
| **1.FWD** | high | Forward capture is dead wiring — `opening_line_job` scheduled by nothing; `mark_closing_*` uncalled → every 2026 slate day loses reference data. | Ofelia jobs for open-line capture (~08:00 ET) + a post-game `mark_closing_*` step; at minimum a daily current-slate backfill so no day is permanently lost. | M |
| **B.PROP-TAIL** | high | Prop PMFs have **no tail smoothing** (review + audit) — a possible-but-unsampled prop (e.g. `P(K≥12)`) prices to hard 0.0, fabricating a maximal edge or getting silently dropped. Shared with G13. | Laplace/Beta pseudo-counts over a dense support, or a light parametric tail; keep observed mass dominant; regenerate prop goldens. | M |

**B.RERUN (SIM-481) — the terminal gate.** After **all** of B + Track C + Track D + E-LEAK are in and
CI-green: re-run the full-season backtest at `n≥400` with the significance gate, three-way tie handling,
calibrated win-prob, line-move handling, and the book-pinned re-backfilled table. Publish with Wilson CIs,
per-market power flags, a de-vig sensitivity band, and full provenance (flags, git SHA, calibration hash,
artifact build id, pinned book). **Strike the pre-repair ~49% from every doc.** Only then decide model work.

---

## 5. Wave 2 — Make production self-verifying & safe (parallel; mostly off the critical path)

### Cluster F — Testing & self-verification (Theme 4)
CI currently certifies a configuration production never runs (every realism flag + the full-pool sampler
pinned OFF suite-wide; the ~250 lines of core production sim methods, the 5 draw-driving engines, and every
engine's DuckDB SQL contract have zero coverage).

| Ticket | Ref | Sev | Fix | Gated by |
|---|---|---|---|---|
| SIM-452 | F-01 | high | Production-config regression lane: a `simulate_game` golden with `SIM_FULL_POOL=1` + all flags ON over a **committed toy artifact bundle**. | Goldens **regenerate as the closing step of Track C** (else they freeze the bugs); F-05 |
| SIM-452 | F-02 | high | Extend the engine golden gate to pitcher/batter/fielder/pitch-pitch/batted-ball (the 5 that weight the draw); fix the false ci.yml "all engines" header. | — |
| SIM-453 | F-03 | high | `:memory:` DuckDB **schema-contract** test running each engine's real `build()` — replaces the tautological empty-mock smoke that let SIM-408 ship 4 dead engines under green CI. | — |
| SIM-454 | F-04 | high | ERROR-log both silent `except: return None` factory blocks; **fail boot in prod** when the full-pool bundle can't load; provenance stamp (flags, calibration hash, artifact build id, sampler path) on `/ready`, `/metrics`, `SimulateResponse`, and the backtest JSON. | — |
| SIM-455 | F-05 | high | Version + **atomic** (tmp+rename) engine-artifact publish; loader cross-file consistency/version check; add the artifact build to the nightly chain (nothing schedules "the nightly builder" today). | — |
| SIM-456 | F-06 | high | Domain-anchored `_validate_outputs()` at the end of the 5,220-line `run()` that **raises** (the first `raise` in the file) on out-of-band DP-rate/K-rate/BABIP/row-count/park-factor — catches the DP-rate-0.0 class the same night. | — |
| SIM-457 | F-09/10/11/12 | high | ~50-line DuckDB migration runner (diff `migration_history` vs the dir, apply in a transaction, wire into make + nightly step 0 + a boot check); explicit column list on the `sim.outcome_pool` INSERT (kills the silent venue↔fielder transposition); add the 4 missing tables (incl. `migration_history`) to the base schema; fix the wrong-db-file docs. | — |
| SIM-458 | F-07/08 | med | Bench the FullPoolSampler over the toy bundle (not the rng stub) under PERF_STRICT; add determinism + flag-off byte-identity gates; correct the overstated parallel-CLV "byte-identical" claim to "aggregate-identical". | F-01 |
| SIM-459 | F-13/14/15/16/17 | high | Fix the validation harness so it can actually exercise `SIM_PARK_FACTOR`/`SIM_FIELDER_RBF` (pass the defense maps + park factor it currently drops), implement the advertised per-channel breakouts (RISP/advancement/DP/per-pitcher ERA-K9-BB9-WHIP that don't exist in code), report **seed-paired OFF/ON deltas with between-game CIs**, record full provenance — **then re-run the flag-enablement validation at the pre-registered ≥400×≥20 bar, one flag at a time.** Until then mark the five "validated" labels PROVISIONAL. | F-14/15/16/17 + Track C |

### Cluster G — Service layer / API (Theme 5)

| Ticket | Ref | Sev | Fix | Gated by |
|---|---|---|---|---|
| SIM-460 | G1/G2 | high | Fixed worker count decoupled from per-request `n` (n=1 must **not** run a full-pool game in the API parent; 1<n<workers must not rebuild the shared pool); result timeout + `BrokenProcessPool` recovery so one dead worker no longer bricks `/simulate` until restart. | — |
| SIM-461 | G3 | high | Add `require_auth` to `/boxscore` + `/props` (unauthenticated compute-DoS today); build prop sets from **retained pooled boxscores** instead of the ~150–220s serial parent re-sim. | J4/J8 |
| SIM-462 | G4 | high | Wire `/edges` + `/signals` to `raw.game_odds`; gate the mock behind an explicit dev flag (Kelly-sized "+EV" signals price off fabricated lines today); fix `odds_source` per-side labeling. | SIM-448 (real odds must be right-game first) |
| SIM-463 | G5 | high | Fail auth **CLOSED** in non-dev (reject empty/whitespace/placeholder creds); loud boot warning + a `/health` auth-state field. | Cluster I prod overlay |
| SIM-464 | G6–G12 | med | `/plays` latest-run only; move DuckDB writes off the event loop + lock the shared connection; rate-limiter bucket on trusted identity + bound the dict; WS auth/caps + leak fix; `/ready` provenance; cache single-flight + **non-pickle** codec; skip replay persist on cache hits. | G11↔I6 |

### Cluster I — Operations / DR / security (Theme 7)

| Ticket | Ref | Sev | Fix | Gated by |
|---|---|---|---|---|
| SIM-465 | I1 | high | **Backups/DR** — nightly `pg_dump` + DuckDB/calibration copy to a host path **outside** the WSL2 vhdx via Ofelia; retention; `restore.sh` + a runbook + **one restore drill**. (Only recovery today is `down -v`/`make nuke` — both destroy data; forward-captured odds are uniquely unrecoverable.) | I4 |
| SIM-466 | I2 | high | `docker-compose.prod.yml` overlay: `target: runtime`, no bind mounts, no `--reload`, real `.env.production` (the documented `--env-file` tier does nothing today → silently auth-less prod). | I5 |
| SIM-467 | I3/I3b | high | cadvisor/node-exporter + Alertmanager + rules (container RSS vs the 10 GB cap is unmeasurable today — memory is the worst incident class); pool-exercising `/ready` so an OOM-deadlocked pool flips unhealthy and `restart` fires. | — |
| SIM-468 | I4 | high | Dedicated `REPLAY_DUCKDB_PATH` so the live replay RW handle stops contending with the nightly rebuild on one file. | — |
| SIM-469 | I5/I6 | high | Real credentials (remove the hardcoded compose DSN that shadows `.env`); bind datastore ports to loopback; Redis `requirepass` + non-pickle cache codec (poisoning→RCE path today); no Grafana admin/admin. | — |
| SIM-470 | I7–I12 | med | Release branch filter (`main`→`master`; ghcr has never run); per-step freshness watermark (nightly failures are silent); log rotation; pin `ofelia`/actions to digests + a Python lock file; reboot runbook; Makefile drift. | — |

---

## 6. Wave 3 — Frontend & performance (independent; parallel after A)

### Cluster H — Frontend (Theme 6)

| Ticket | Ref | Sev | Fix |
|---|---|---|---|
| SIM-471 | H7 | med | **Land first** — add Vitest + RTL + jsdom (+ a CI job) and Playwright renders of the money surfaces *with data*, so H1/H2/H3/H8 ship test-guarded. |
| SIM-472 | H1 | high | Unify the run-line market key on **`runline`** (`clv_engine.py:670` label + `LineMovementPanel`/`BettingCard`) so the tab stops 422-ing and the mock/live badge renders. Do **not** touch the distinct numeric `run_line` query param. |
| SIM-473 | H2 | high | Fix `BaseballFieldGraphic` occupancy predicate — `false != null` reads as occupied, so every base shows occupied in every state except bases-loaded. |
| SIM-474 | H3 | high | BettingCard fires two unseeded 200-iter sims per click and cross-pairs badges/edges — fetch sequentially with a **shared seed** (the second call then cache-hits; ~2.5 min → one sim). |
| SIM-475 | H4 | med | Decide the OpenAPI pipeline: wire `typed.ts` + a CI drift gate (`openapi.json` already dropped `lineup_ready`), **or** delete the dead pipeline. |
| SIM-476 | H5/H6/H8/H9 | med | Render sub-resource errors + loading + the SIM-409 Retry-After; WS-driven revalidation (scheduled→live flip, re-sim results without reload); slate-card scores + winner highlight (columns already exist, no migration); centralized 401→login. |

### Cluster J — Performance (Theme 8; most byte-identical)

| Ticket | Ref | Sev | Fix | Output change? |
|---|---|---|---|---|
| SIM-477 | J1/J2/J7/J10 | med | Early-return the half-inning-constant pitcher factor (batter_id wrongly in the key); lazy count-bucket CDFs (~600 MB/game churn) + drop redundant `.astype`; cross-iteration `(hand,pitcher,batter)` LRU (3+ O(N) passes on 99/100 iters); delete dead `_f_situation`. | none (byte-identical) |
| SIM-478 | J3/J5/J6/J8/J9 | med | Drop the parent's private duplicate of shared arrays + pinned bundle; int8-encode the categorical pool columns that defeat the shared-memory seam; bake per-worker derived indices into the artifact; return compact per-game records not full `GameSimResult`; stop serializing the legacy ~2 GB pitcher-sim dict. | none |
| SIM-479 | J4 | high | Retain pooled boxscores so `/props`/`/boxscore` reuse the warm batch (the biggest user-facing win). | none (pairs with G3/G-461) |
| SIM-480 | J11 | med | Make the PERF_STRICT SLA gate exercise the production full-pool path, not the rng stub. | none (pairs with F-07) |

---

## 7. Sequencing at a glance

| Wave | Tickets | Starts after | Off critical path? |
|---|---|---|---|
| **0** Version control | SIM-438 | — (do first, alone) | it *is* the gate |
| **1** Sim bugs (C) | SIM-439 | SIM-438 | on critical path |
| **1** Profile SQL (D) | SIM-440/441/442 | SIM-438 | on critical path (recompute) |
| **1** Model/calibration (E) | SIM-443/444/445/446 | SIM-438 (E-1 needs sign-off) | E-LEAK on critical path |
| **1** CLV instrument (B) | SIM-447/448/449/450/451 | SIM-438 | on critical path |
| **2** Testing (F) | SIM-452…459 | SIM-438 (F-01/F-13 gate on C) | off (self-verification) |
| **2** API (G) | SIM-460…464 | SIM-438 (G4→448, G3→J) | off |
| **2** Ops (I) | SIM-465…470 | SIM-438 (I2/I7→A) | off |
| **3** Frontend (H) | SIM-471…476 | SIM-438 | off |
| **3** Perf (J) | SIM-477…480 | SIM-438 | off |
| **Terminal** CLV re-run | **SIM-481** | C + D + E-LEAK + all of B, CI-green | the answer |

**One recompute, one calibration, one re-run.** The single most important scheduling rule: do the code
edits for C, D, and E in their branches, then run `profile-computor` → engine-artifact rebuild → **one**
`make calibrate` + `validate-props` (after C lands) → **one** season CLV re-run (SIM-481). Running the
~5.7-hour recompute or the season backtest per-finding is the trap to avoid.

---

## 8. What "done" looks like

1. `master` is CI-green with SIM-437 committed and the definition-of-done updated (Wave 0).
2. The simulator produces legal baseball: ~4–5 pitchers/game, ~0.7 steal attempts/game, no phantom
   runners, reach-on-error as baserunners — and `sim_stats` (at the ≥400×≥20 bar, with the harness fixed)
   shows the run gap closing by the predicted ~+0.25–0.35 R/team-game from ROE alone.
3. The profile columns mean what their names say (whiff = swings-and-misses, take = takes, pull = pull),
   verified by the `:memory:` schema-contract tests and a healthy `sim_stats` rate-stat pass.
4. The CLV backtest reads the right game's book-pinned lines, compares like-for-like lines, prices with the
   calibrated win-prob, reports beat/push/lose with Wilson CIs, and carries full provenance.
5. Production is self-describing (provenance on every response, a data-quality gate, a schema-contract
   test, a migration runner) and recoverable (backups + a tested restore), with auth failing closed.
6. **SIM-481** produces a season beat-close read that is evidence about the *model* — with error bars — and
   the ~49% instrument artifact is struck from every doc. **Only then** is model work (C8–C12 realism
   mechanics, further calibration) prioritized against a trustworthy signal.

*Source findings and `file:line` evidence: the 2026-07-14 code-review (net-new items, `source: review`)
and [`2026-07-13-analytics-firm-comprehensive-audit.md`](2026-07-13-analytics-firm-comprehensive-audit.md).
Ticket numbers are a proposed contiguous allocation from the current next-free ID (SIM-438); the PM
deconflicts at commit time.*
