# 2026-07-13 — Analytics-Firm Comprehensive Audit (10 specialist roles + adversarial verification)

**Scope:** the entire platform — architecture, modeling/ML, baseball realism, data engineering, backend/API,
performance, betting/CLV methodology, QA/testing, DevOps/SRE, frontend — audited as of working tree
2026-07-13 (HEAD `18c6aac`, 2026-06-06).

**Method:** ten specialist auditors (mirroring a top sports-analytics firm's staffing) each read the actual
code, produced findings + genuine strengths with `file:line` evidence. Every finding of severity ≥ medium
was then handed to an **independent adversarial verifier** whose only job was to *refute* it against the
code (and, where possible, against the live databases). A completeness critic then identified three areas
the whole panel missed and dispatched gap auditors, whose findings were verified the same way.

**Tally:** 129 findings — **97 confirmed, 6 partial, 0 refuted**, 26 unverified (16 low-severity by
design; 10 gap-audit verifications lost to an API rate limit). 68 strengths documented. Several severities
were *corrected by the verifier* (both up and down); this report uses the post-verification severity.

**Severity scale:** critical = wrong numbers can reach betting decisions / data corruption / security
hole · high = materially wrong behavior or rework-forcing design flaw · medium = meaningful
quality/maintainability/perf improvement · low = polish.

---

## Executive summary

The platform's engineering foundations are genuinely strong — deterministic seed discipline, a clean
layered architecture, sophisticated calibration vocabulary, idempotent data loaders, and unusual
epistemic honesty (the team reported its own "no edge yet" result). **But the audit's central conclusion
is that the fund's measuring instruments are less trustworthy than the docs believe, in ways that made
the headline "~49% beat-close = no edge" read unreliable in *both* directions:**

1. **The CLV backtest — the gold-standard go/no-go instrument — has at least six independent, confirmed
   defects** (wrong-game odds, cross-line comparison, look-ahead leakage, uncalibrated win prob, ties
   scored as losses, no uncertainty reporting). Two were *empirically demonstrated against the live DB*.
2. **The production simulator has confirmed game-logic bugs** that specifically contaminate the traded
   prop markets (pulled pitchers resurrected every half-inning; stolen bases silently zeroed since the
   SIM_MANAGER enablement; phantom runners after double plays; reach-on-error converted into outs).
3. **CI certifies a configuration production never runs.** Every realism flag is pinned OFF in tests and
   ON in production; the golden-file gate covers 5 of 11 engines and none that drive the production
   sampler; degradations at four layers are silent.
4. **Operational risk is concentrated and unmitigated:** no backups of any datastore, the production
   stack is the dev compose config with auth off and default credentials, zero alerting, and ~5 weeks of
   work (SIM-437) exists only as uncommitted local changes that production executes via bind mounts.

The single most valuable strategic insight: **stop treating the ~49% result as evidence about the model.
It is currently evidence about the instrument.** Fix the measurement layer first (items 1–8 below, mostly
small), re-run the season backtest, and only then decide where model work is needed.

---

## What the platform does well (verified strengths, selected)

- **Determinism as a spine.** Per-game seeds are threaded through every RNG holder (sim_loop.py:3736-3753,
  batch_runner.py:183); flag-off behavior is designed byte-identical; the CLV backtest records base seeds
  and its parallel mode was verified byte-identical to serial. For a Monte-Carlo trading shop this is the
  right foundation, done right.
- **Clean layering with disciplined dependency direction.** `betting/` is pure math with no DB/env/framework;
  no production module in simulation/similarity/pipeline/betting imports `api`; one pricing implementation
  (clv_engine) is shared by the live surface and the offline backtest so they cannot drift algorithmically.
- **Statistically literate prop/win-prob machinery.** Full integer-support PMFs built from the raw
  per-iteration substrate with exact sportsbook push conventions (prop_distributions.py); Jeffreys-smoothed
  win probabilities with explicit tie policy and honest CIs; the validation toolkit correctly distinguishes
  binary from multiclass calibration and uses mid-P PIT for discrete PMFs — textbook-correct choices.
- **Idempotent, auditable raw-side ETL.** Natural-key/content-hash dedup on every write path; two-tier
  validation (hard error vs quality-flag) mirrored by a DB trigger; skipped rows persisted to
  `raw.etl_errors` with a first-class reprocess operation.
- **Hard-won multiprocessing engineering, documented where the next engineer will read it.** The
  forkserver/COW diagnosis, shared-memory ownership contract, bounded-concurrency prewarm with deadline —
  each carries its incident history inline.
- **MLB-rule fidelity where it was attempted.** W/L/S per Rule 9.17(b)/9.19 including sub-5-IP starter
  reassignment; walk-offs, skipped bottom-9ths, extra-innings ghost runner; count-conditioned pitch draw
  with a documented empirical basis. The lineup resolver handles DH/defensive-sub semantics correctly.
- **Frontend hygiene.** Strict TypeScript with zero `any`-escapes, real accessibility (semantic tables,
  aria on graphics, focus management), fetch-cancellation discipline everywhere, a genuine design-token
  system, and cost-aware UX gating of expensive sim endpoints.
- **Epistemic honesty as culture.** MARKET_TRUST labels, the unflattering ~49% reported rather than
  cherry-picked, survivorship-bias guards written into docstrings, an adversarial validation audit
  (2026-06-03) that refuted the team's own claims and found a real bug. The instinct is right — the audit
  below shows where the *instrumentation* hasn't kept up with the instinct.

---

## Theme 1 — The measurement layer is broken (fix FIRST, mostly small effort)

These defects all sit in the CLV backtest / odds pipeline. Until they are fixed and the season backtest
re-run, no beat-close number should drive strategy.

### 1.1 [CRITICAL · confirmed · empirically demonstrated] Wrong-game odds: BettingPros resolution keys on the UTC date
`pipeline/bettingpros_odds_provider.py:138` uses `str(game["gameDate"])[:10]` — a **UTC** timestamp — instead
of `officialDate` (which the historical loader already uses correctly at etl_historical_loader.py:510). Every
CT/MT/PT night game resolves to the *next* calendar day; the nickname suffix-match then pairs it with the
following day's game between the same teams. **The verifier queried the live 2024 backfill: the
identical-open-AND-close signature appears in 26.1% of CT, 43.3% of MT, and 43.2% of PT same-matchup pairs
vs 0.1% for ET (baseline)** — plausibly 25-35% of the 2,378-game backfill carries the wrong game's reference
lines. Double-headers always take game 1's odds for game 2 (3 of 5 DH pairs confirmed identical). Same bug in
`bullpen_availability_ingest.py:196`.
**Fix (small):** use `officialDate`; disambiguate DHs by scheduled time; assert matched-event time within
~2h of first pitch; re-audit the backfill before trusting any season-scale read.

### 1.2 [CRITICAL · confirmed] CLV is computed across *different lines* on 9 of 10 markets
`scripts/clv_backtest.py:633,748` take the line from the opening row only and never compare the closing
row's line. Books express steam by *moving the line* — a total 8.5→9.0 at unchanged juice is a maximal CLV
win that scores as ~zero. Affects totals, run lines, and all 7 props (only the moneyline is line-less);
`TwoWayPrices` cannot even represent a moved line, and `BetRecord` doesn't store the closing line so the
artifact can't be filtered post-hoc. Same defect serves the live `/line-movement` CLV endpoint
(betting/line_movement.py:417-428). No moved-line unit test exists.
**Fix (small):** skip-or-bucket line-moved markets and report the rate; better, credit line moves through
the model PMF. Add the open-8.5/close-9.0 test.

### 1.3 [CRITICAL · confirmed] Look-ahead leakage: 2024 backtests sample 2025-2026 plays, future data up-weighted
The artifact bundle is scoped to the newest 3 DB seasons (engine_artifacts.py:65-72 → 2024/25/26), recency
weights anchor to the newest-ever season so a 2024 replay weights 2025/26 plays at 2.0 vs 1.5 for its own
season, and actor profiles are full-season aggregates keyed `{player}:{game_season}` (within-season
lookahead on every profile). For pre-2024 replay games (the reliability-curve fit spans 2017-2026) **every
sampled play is future data**. All headline validation numbers — win-prob ECE 0.047, "bettable" H/HR/TB
ECE 0.02-0.05, and the CLV read — were produced under this leakage; a correct expanding-window splitter
exists (recency_walk_forward.py) but the production validation path never uses it.
**Fix (medium):** as-of-date artifact bundles for backtests; anchor recency to the simulated season;
prior-season (or season-to-date) actor profiles for replays. Treat current ECE/trust labels as unvalidated
until re-run.

### 1.4 [HIGH · confirmed] The backtest prices the moneyline with the *identity* (uncalibrated) win probability
`clv_backtest.py:876` calls `win_probability(summary)` bare → IDENTITY_CALIBRATION, while production threads
the fitted reliability curve (api/routes/betting.py:332-333, the SIM-387 fix). The script's own docstrings
claim the opposite ("a calibrated WinProbability", lines 37/842). The raw win prob has ECE 0.171 — the curve
was fitted precisely because it is biased. This is the SIM-387 bug class re-introduced in the new scoreboard.
**Fix (small):** load the CalibrationReport in `_score_one_game` / worker init; stamp the map name into the
report params.

### 1.5 [HIGH · confirmed · empirically demonstrated] Unmoved lines count as losses — this alone drags the headline across the 50% bar
`beat_close = clv_prob > 0` counts exact-zero (unmoved) markets as failures. **Verifier, from the real
120-game report JSON: 203 of 4,626 bets (4.4%) have clv_prob == 0.0; headline 48.81% as-computed vs 51.05%
excluding ties; the HR prop market flips 48.9% → 53.7%.** (Tie-immune mean CLV is -0.0086 ± 0.0010, so the
strategic "no edge yet" conclusion happens to survive — but the headline rate and per-market comparison are
biased by construction.)
**Fix (small):** three-way beat/push/lose split; report mean_clv_prob with SE.

### 1.6 [HIGH · partial] n=100 iterations + min_edge=0 places bets on Monte-Carlo noise
Model prices are raw frequencies over 100 sims (SE ≈ ±5pp at p≈0.5); edges on a two-way market are exact
complements, so *every* priced market places a bet whose side near the market price is chosen by sampling
error, and max-of-two-noisy-edges inflates recorded edges (winner's curse). Live bet_signal uses a 2% floor
the backtest doesn't mirror. (Verifier: a uniform 2-3pp true edge would still surface at ~55-60% aggregate,
so "no *uniform* edge" retains some evidentiary value — but per-bet edges and any concentrated-edge readout
are unreliable.)
**Fix (small/medium):** gate at ≥2× the analytic per-market MC SE (or raise backtest iterations); report the
share of picks whose |edge| > 2 SE.

### 1.7 [HIGH · confirmed] No confidence intervals or minimum-n anywhere on the scoreboard
The moneyline row of the 120-game read is n≈120 → 95% CI half-width ±8.9pp; distinguishing 49% from 53%
needs ~1,225 bets *per market*. The docstring hard-codes decision thresholds inside the noise band. Raw
per-bet records are persisted, so CIs are computable — just never computed.
**Fix (small):** Wilson CI per row, game-level clustering for pooled rows, suppress rows below a power floor.

### 1.8 [HIGH · confirmed] The historical "closing line" is an unverifiable proxy with erased provenance
Closing = max-`updated` line scanned across ALL books (per selection independently — over/under legs can
come from different books at different lines, then get de-vigged as one market: confirmed also as a
separate finding), pulled ~15 months post-game, with no `updated ≤ first-pitch` guard; opening comes from a
different provenance; rows persist as `book='consensus'`. The 49% headline rides on this proxy.
**Fix (small/medium):** pin one sharp book for both endpoints; persist book id + `updated`; spot-check ~50
games against archived closes before trusting the metric.

**Also confirmed in this theme (medium):** degenerate 0/1 sim probabilities silently skipped (the model's
biggest claimed edges never enter the scoreboard); backtest reads odds with no source/is_mock filter (a
default-provider run would silently poison the table — latent, the real backfill used bettingpros);
proportional devig only (no power/Shin; sensitivity unmeasured); pk-ordered `--max-games` slate is heavily
non-random (**verifier: Washington appears in 81 of the 120 games**); push mass charged as full loss in
EV/Kelly (understates both — conservative, but wrong); the deployable strategy (bet_signal gates + Kelly
weights) is never what the scoreboard measures.

**And the forward capture is dead wiring [HIGH · confirmed]:** `opening_line_job` is scheduled by nothing;
`mark_closing_lines`/`mark_closing_prop_lines` have no production caller; the live pipeline itself is off
by default. Every 2026 slate day is losing forward CLV reference data (recoverable retroactively via the
BettingPros backfill path, but the wiring the docstrings describe does not exist).

---

## Theme 2 — Confirmed simulator bugs that contaminate traded props

### 2.1 [CRITICAL · confirmed] Pulled pitchers are resurrected every half-inning (SIM_MANAGER=1, ON in production)
`_maybe_pull_starter` mutates only `state.pitcher_id`; every half-inning `_set_half_matchup`
(sim_loop.py:2732-2753) re-points the mound at `home/away_pitcher_id` — set once at build, never updated on
a pull. The pulled starter returns next half with his saved pitch count, usually re-triggers the pull gate,
burns a fresh reliever ~every half-inning; when the 6-arm pen empties the *removed* pitcher throws again
(illegal re-entry). The docstring claims the opposite of what the code does. The celebrated SIM-434
validation stat — "pitchers/game 2.0→9.25, realistic" — is arithmetically an artifact of this cycle (a
~290-pitch game cannot legitimately produce 9 pitchers with a 75-pitch pull floor). Pitcher K/BB/ER/OUTS
PMFs — traded markets — are built from these contaminated sims.
**Fix (small):** track current pitcher per team; `_set_half_matchup` reads that ledger. Regression test:
pulled pitcher never reappears; pitchers/game lands ~4-5.

### 2.2 [HIGH · confirmed] Enabling SIM_MANAGER silently zeroed ALL stolen bases
The validated engine-backed steal path is reachable only when the manager green-light is ≤ 0; with the
default manager profile green is always 0.04-0.12, routing to `resolver.resolve_steal` — and production
wires no resolver, so the base-class stub returns `attempted=False` unconditionally. **Since the 2026-06-04
enablement, production games contain zero steal attempts.** The "runs −0.10/team, no distortion" validation
was consistent with losing ~0.6 SB/game — real distortion misread as noise (the validation table omitted
SB/CS even though sim_stats reports them).
**Fix (small):** fall back to `_full_pool_steal_decision` when the resolver declines; flag-on regression
asserting ~0.7 attempts/game.

### 2.3 [HIGH · confirmed] Double plays never remove the doubled-off runner — phantom runners
The DP branch records 2 outs but no code path touches `state.bases` (the docstring claiming "double plays
erase the trail runner" is implemented nowhere). A 0-out GIDP with R1 leaves the runner standing on first
with two outs — a physically impossible state (a real player id) that feeds later steal/situation logic and
can score. ~+0.03-0.05 R/team-game artificial inflation that *masks* the real conversion deficits and
corrupts per-runner R / ER attribution. Same defect in the per-tile fallback resolver.
**Fix (small):** clear the retired trail runner in the DP branch (both paths); unit test.

### 2.4 [HIGH · confirmed] Reach-on-error is converted into outs — plausibly the largest single piece of the run gap
Pool `field_error` rows carry result_hits=0; `_full_pool_fielding` deliberately ignores pool outs and infers
`outs = 0 if hits > 0 else 1` — every drawn error becomes a one-out field_out. MLB ROE ≈ 0.5-0.6/team-game;
flipping each from baserunner to inning-shortening out ≈ 0.25-0.35 R/team-game suppressed. **Invisible to
rate-stat validation because ROE is not a hit in MLB accounting either — the signature is exactly "rate
stats right, runs low", the tracked SIM-429 symptom.** The dropped-third-strike path already shows the
intended reach semantics working downstream.
**Fix (small):** special-case error-family events (batter reaches, no out, `is_error=True` into the SIM-414
unearned-run machinery); re-run sim_stats and expect ~+0.25-0.35 R/team-game before touching any other knob.

### 2.5 Other confirmed realism findings (ordered by likely run-gap contribution)
- **HBP, WP, PB, balks, pickoffs absent entirely** (medium): ~0.15-0.25 R/team-game of free-baserunner /
  hit-free-advancement channels; D3K can never fire in production (unwired resolver hook).
- **Runner on 1st never advances on non-DP ground outs; FC/force-outs retire the wrong player** (medium):
  systematic under-advancement into scoring position; advancement constants (0.28 3rd→home on ground out)
  read low vs Retrosheet.
- **TTO/fatigue never degrade pitch outcomes** (medium): they only time the pull. i.i.d. season-aggregate
  sampling removes hit clustering; runs are convex in baserunners, so the same hit rate yields fewer runs —
  a sequencing mechanism per-channel rate calibration cannot fix.
- **Situation kernel treats the runners bitmask + raw inning as unstandardized Euclidean dims** (high):
  with sit_sigma=2.0, runner-on-2nd vs bases-empty retains weight 0.92 — RISP conditioning is an ~8%
  down-weight, i.e. essentially noise; loaded(7) is *closer* to 3rd-only(4) than to 1st+2nd(3). This is a
  concrete causal candidate for the "batted-ball-with-RISP" gap; the count-bucket fix (exact stratification)
  is the proven in-repo pattern to copy for (outs, runners_state).
- **Park factor models no HR/XBH channel** (high): out↔single flips only; HR PMFs are perfectly
  park-invariant while HR props are the most park-elastic traded market. `derived.park_factors` ALREADY
  computes per-event HR/1B/2B/3B factors with L/R splits — the sim consumes only factor_type='R'.
- **Home-field advantage is 100% home-batter singles at a knowingly overshot magnitude** (medium): the
  measured retune (0.025→~0.017) was never applied; the entire HFA loads one-sidedly onto home-batter
  H/TB PMFs — exactly the "trustworthy" CLV markets; stacks additively with the park factor on the same
  channel.
- **Batted-ball draw is pitcher-blind given contact** (medium): a GB sinkerballer and a flyball pitcher
  produce identical contact distributions vs the same batter; `_f_pitcher` machinery already exists to fix it.
- **Manager small-ball is decorative** (low): sac-bunt calls do nothing, pitch-outs are never read, and a
  hit-and-run only *suppresses* running; frontend play-by-play narrates strategy that never happens.

---

## Theme 3 — Model/calibration design gaps

- **The production full-pool sampler largely bypasses the similarity-calibration layer** (medium, partial):
  hard-coded sit/batter sigmas never fitted; the pitcher-sim artifact is built without `apply_calibration`
  (bakes default ARSENAL_SCALE 4.10 vs fitted 4.0655); batter factor RBFs over all numeric columns with
  uniform weights, discarding the engine's sub-score structure/reliability weights. "Calibration is LIVE"
  is true for the boot engines serving /similarity — not for the path that prices props.
- **Missing profiles get factor weight 1.0 = self-similarity max** (medium, confirmed): unprofiled
  call-ups are ~2× over-sampled vs the 0.50-median profiled row. Substitute the pool-mean instead.
- **Embeddings 0-fill missing values BEFORE z-scoring** (medium, partial): a missing `max_exit_velo`
  becomes z ≈ −21, crushing that player's affinity everywhere. Impute at the column mean.
- **Win-prob reliability curve fragility** (medium, partial): min_bin_count=1 accepts 1-game anchors;
  running-max "monotonization" can only ratchet the curve UP (use PAVA); the fitted map's quality is never
  evaluated anywhere (the backtest doesn't use it — see 1.4).
- **EB shrinkage inert for batters** (medium): EB_N_PRIOR=5 under a 100-PA inclusion floor → alpha ≥ 0.95;
  the calibrated `eb_n_prior_*` values are computed and stored but consumed by no engine (the exact
  computed-but-never-applied gap SIM-346 closed for ARSENAL_SCALE).
- **Product-of-kernels weight has no ESS diagnostic or tempering** (low): correlated factors double-count
  evidence; per-PA effective sample size is two reductions on an existing array.

---

## Theme 4 — The production configuration has no safety net

- **[HIGH · confirmed] CI certifies only the fallback path.** `tests/conftest.py:33-53` pins SIM_FULL_POOL
  and every realism flag OFF suite-wide; no test anywhere enables SIM_FULL_POOL; production runs the exact
  inverse. The four core production methods (`_full_pool_outcome/fielding/out_advancement/steal_decision`,
  ~250 lines shaping every production pitch) have zero test references. The conftest comment claiming tests
  "opt in explicitly" is aspirational — none does.
- **[HIGH · confirmed] The regression gate pins 5 of 11 engines — none that weight the production draw**
  (pitcher/batter/fielder/pitch-pitch/batted-ball uncovered), imports nothing from simulation/, and pins
  module-default sigmas while production rebuilds scorers from calibration.json at boot. The ci.yml header
  ("all 9 engines") is wrong on both counts. No simulate_game-level golden exists for ANY configuration.
- **[HIGH · confirmed] The `__new__` bypass leaves every engine's DuckDB SQL contract untested** — the
  build-smoke suite mocks connections to return empty rows and asserts `profile_count == 0` *passes*; this
  is the exact gap that produced SIM-408 (4 engines dead in production under green CI). DuckDB is
  in-process; a :memory: schema-contract test closes this in the unit lane.
- **[HIGH · confirmed] Silent model-degradation ladder with zero provenance.** Four stacked best-effort
  fallbacks (bare `except: return None` in the sampler builder and deriver builder — no log; engines
  log-and-skip; calibration degrades to identity at INFO) mean a corrupt artifact volume serves HTTP-200
  prices from a different model, undetectable live or forensically. /ready treats engines as
  "informational"; no response carries sampler-path/calibration/artifact identity.
- **[HIGH · confirmed] Engine artifacts — the production sim's actual data source — are unversioned,
  non-atomically published, and absent from the nightly chain** (the module calls itself "the NIGHTLY
  BUILDER"; nothing schedules it). Loader performs no cross-file consistency checks; a worker cold-loading
  mid-rebuild can pair new geometry with old metadata.
- **[HIGH · confirmed] No data-quality gate on the derived/DuckDB side** — `run()` executes ~22 steps with
  zero post-step assertions and zero `raise` statements in the 5,220-line file; the DP-rate bug class
  (0.0 rates shipped for months, 5.7-hour recompute) can ship again. A domain-anchored range-check pass at
  the end of run() is cheap.
- **Perf gates measure a stub** (medium): PERF_STRICT hard-gates the rng-driven no-DB factory weekly; no
  benchmark exercises FullPoolSampler at all; the "authoritative DB-backed perf job" the bench file promises
  was never built.
- **"Byte-identical" claims are verified manually, not by any gate** (medium): the regression lane CLAUDE.md
  credits runs no simulation; the parallel-CLV byte-identity claim is strictly false at report level
  (as_completed ordering) — only count aggregates are order-insensitive, and exactly that invariant is untested.

### DuckDB schema deployment (gap audit — the SIM-408 recurrence vector)
- **[CRITICAL · unverified¹] Schema lag silently reverts validated realism while the flags stay ON:** the
  loader/sampler deliberately degrade on pre-0012 bundles (platoon mask → None, fielder nudge → no-op) with
  no log or metric, so a restored/rebuilt file produces sims that no longer match the calibration they were
  fitted with. The builder fails loudly on the same condition — the loud-builder/silent-loader asymmetry is
  where drift hides.
- **[HIGH · unverified¹] No DuckDB migration runner, no boot verification:** all 13 migrations are applied
  by a human piping SQL; nothing reads `duckdb_schema_version.txt` (only a unit test reading the repo text
  file) or the live `migration_history` ledger (write-only — every migration inserts, no code reads:
  confirmed). The one automated DDL hook skips ALL schema work if a single sentinel table exists and cannot
  ALTER anything.
- **[HIGH · unverified¹] Positional `INSERT ... SELECT * FROM bip`** into sim.outcome_pool depends on
  hand-applied column order; a divergence silently writes venue_id values into fielder_player_id (both
  nullable INTEGER — type-checks fine).
- **[HIGH · unverified¹] `02_duckdb_schema.sql` is missing four migration-created tables** including
  `migration_history` itself — a fresh-built DB breaks replay persistence AND crashes the next hand-applied
  migration at its ledger insert (mid-file, no transactions → half-applied state).
- **[MEDIUM · confirmed] The documented procedure points at the wrong database file** (`baseball_simulator.duckdb`
  relative path in all 13 headers + the WORKFLOW health check vs the real `/data/baseball_sim.duckdb` in the
  volume); actual applications went through a third, undocumented container route.

¹ *"unverified" here means the independent re-check was lost to a rate limit; the gap auditor itself verified
each claim with line-level citations before filing.*

### Validation evidence (gap audit — how the flags got turned on)
- **[CRITICAL · unverified¹] Every production realism flag was enabled on validation an order of magnitude
  below the project's own pre-registered bar** (≥400 sims × ≥20 games, set 2026-06-03 because "the
  2-game/200-iter sweep is noise-dominated") — then enabled the NEXT DAY at 3-4 games, all effects combined
  (the backlog required each measured alone), and relabeled "Validated — no run distortion" in five summary
  docs. At that power, the manager's "runs unchanged −0.10/team" is consistent with −0.26..+0.06 — a 5% run
  suppression would have been invisible. (And per 2.2, a real distortion — zero steals — did in fact slip
  through it.) Home win% moved 0.567→0.523 in the same enablement and was narrated as a "bonus".
- **[HIGH · unverified¹] `scripts/sim_stats.py` structurally cannot exercise SIM_PARK_FACTOR or
  SIM_FIELDER_RBF:** `_sim_kwargs` drops `home_defense`/`away_defense` (already resolved!) and never passes
  `park_run_factor`, so both consumers are provably inert under the designated harness — any A/B toggling
  them compares two identical no-ops and tautologically reports "no distortion". This rebuilds, into the
  measurement tool itself, the exact defense-map-inertness failure the 2026-06-03 audit caught in production.
- **[HIGH · confirmed] The harness's promised per-channel breakouts (RISP, advancement, DP rate, per-pitcher
  ERA/K9/BB9/WHIP) do not exist in the code** — docstring and CLAUDE.md both advertise them; grep matches
  only the docstring. No committed script can reproduce the SIM-434 enablement metrics (pitchers/game,
  starter IP): all enablement numbers came from unversioned ad-hoc tooling.
- **[HIGH · unverified¹] The single precision metric is wrong for every decision it gates:** pooled
  per-iteration SE with zero between-game variance (3 games + more iters ⇒ "TIGHT — calibration-grade"),
  not a delta SE (decisions read OFF→ON deltas; no paired-difference mode despite deterministic seeds
  making it trivial), and a league-average home-win% target applied to 3-4 specific matchups.
- **[MEDIUM · confirmed] Validation artifacts record almost none of the configuration that produced them:**
  the flags under test (SIM_MANAGER/PARK_FACTOR/BB_PLATOON/FIELDER_RBF/FRAMING/STEAL_K), git SHA, artifact
  and calibration identity are absent from stdout and --json-out; the evidence behind five production flags
  is unauditable after the fact.

---

## Theme 5 — Service layer (backend/API)

- **[HIGH · confirmed] One dead worker bricks /simulate until container restart:** no timeout on
  `fut.result()`, no BrokenProcessPool handling anywhere, healthcheck hits /health which never touches the
  pool — the documented OOM-deadlock failure mode has no detection or recovery.
- **[HIGH · confirmed] Per-request `n_iterations` resizes the SHARED pool:** n=1 runs a full-pool production
  game in the API parent (violating the code's own "parent never holds this object" invariant); 1<n<6
  tears down and rebuilds the prewarmed pool while blocking all concurrent requests.
- **[HIGH · confirmed] /boxscore and /props re-simulate N games serially in the API parent, bypassing the
  pool, the cache, AND auth** (~150-220 s at n=100 vs the pool's ~38 s; an unauthenticated compute-DoS
  surface at n≤2000). The workers already pickle back full boxscores; `GameSimSummary.from_results` simply
  drops them — the fix removes the entire path.
- **[HIGH · confirmed] /edges and /signals price off a deterministic MOCK odds provider by default** —
  fabricated lines feed Kelly-sized "+EV" signals to the UI (2,378 games of real SIM-435 odds sit unread
  one table away); `/api/odds/{pk}` ALWAYS returns mock. The only guard is a string flag the frontend
  renders as a cosmetic badge (and for run_line, not even that — see Theme 6). Odds_source mislabels
  half-injected markets as "injected".
- **[HIGH · partial] The deployed stack runs auth-disabled** (ENVIRONMENT=development in the live .env,
  placeholder SECRET_KEY, limiter off, everything reachable on host :80/:8000); non-dev *fails open* when
  API_KEYS is empty and AUTH_PASSWORD is whitespace. No boot warning or /health field distinguishes
  auth-bypassed from auth-enforced.
- **Medium (all confirmed):** /plays concatenates ALL persisted runs (route promises most-recent); one
  shared DuckDB connection used cross-thread without cursors; rate limiter bypassable via arbitrary
  X-API-Key + unbounded bucket dict; WS endpoint has no auth/caps and leak-prone cleanup, with the SIM-385
  typed schemas wired into nothing; degraded boot invisible to /ready; no cache stampede protection on 38-s
  computes + pickled Redis values (an RCE vector given unauthenticated Redis); /simulate persists replay
  rows even on cache hits.

---

## Theme 6 — Frontend

- **[HIGH · confirmed] Run-line market broken end-to-end by a `run_line` vs `runline` key mismatch:** the
  Line-movement "Run line" tab always 422s, and the mock-odds warning badge never renders for run-line
  edges — on a betting dashboard where every market is mock-priced by default. A backend unit test asserts
  the mismatch (both spellings in one response).
- **[HIGH · confirmed] BaseballFieldGraphic shows every base occupied on all live games** (`false != null`
  passes the occupancy check; the only call site passes booleans). The live base-state display is wrong in
  every state except bases-loaded.
- **[HIGH · confirmed] One "Load betting" click fires two concurrent 200-iteration sims with no shared
  seed** (~2.5 min of duplicated pool compute), then pairs +EV badges from sim A with edge numbers from
  sim B on the same card. Sequential fetch (or one endpoint — BetSignal already carries its EdgeReport)
  fixes both.
- **[MEDIUM] (confirmed):** the generated OpenAPI type pipeline is dead weight (typed.ts imported by
  nothing, hand-mirrored shapes, checked-in openapi.json already drifted — `lineup_ready` missing — no CI
  diff); GamePage computes but never renders sub-resource errors and has no loading state (failures render
  as "run a simulation" empty states; the SIM-409 Retry-After contract has no consumer); no revalidation
  strategy (a scheduled game never flips live; re-sim results unreachable without reload; the WS advertises
  exactly the events needed); zero component/unit tests — the whole test surface is 4 mocked smoke tests
  with the money surfaces never rendered with data; slate cards show no scores (winner-highlight is dead
  code; the backend already returns lineup_ready and stores final scores).

---

## Theme 7 — Operations

- **[HIGH · confirmed] No backup or disaster recovery for ANY datastore.** All five volumes live in one
  WSL2 vhdx on a C: drive with a documented 100%-full incident. Uniquely unrecoverable: forward-captured
  live odds snapshots. Everything else is days-of-rebuild. The only documented recovery is `down -v` /
  `make nuke` — both destroy the data. Fix: nightly pg_dump + DuckDB/calibration file copy outside the
  vhdx via the existing Ofelia chain, plus one restore drill.
- **[HIGH · confirmed] Production runs the dev compose config:** `target: dev` image (pytest/ruff baked in),
  `uvicorn --reload` over bind mounts (any file save drops the warmed pool mid-slate), and the documented
  `--env-file .env.production` bring-up does nothing (the service hardcodes `env_file: .env`) — following
  the documented prod procedure yields ENVIRONMENT=production with no API keys = silently auth-less (the
  fail-open corner of Theme 5).
- **[HIGH · confirmed] Zero alerting; monitoring cannot see the worst failure mode.** No Alertmanager/rules,
  no cadvisor/node-exporter (container RSS vs the 10 GB cap is unmeasurable — the platform's worst incident
  class is memory), /health is a static dict and /ready never touches the pool, so an OOM'd deadlocked pool
  stays green and `restart: unless-stopped` can never fire.
- **[HIGH · confirmed] Wired-in DuckDB single-writer conflict:** the replay RW handle (ON in the prod/staging
  env tiers) vs the nightly rebuild container on the same file — either the nightly fails or replay silently
  degrades; the in-code comment admits the missing dedicated-file design.
- **[HIGH · confirmed] Default credentials everywhere + published ports:** `baseball_pass` in 16 files —
  including a hardcoded no-substitution compose DSN override that wins over env_file, so setting real
  credentials in .env doesn't even take effect; unauthenticated Redis published to the LAN *while serving
  pickled cache values the API deserializes* (a poisoning → RCE → wrong-numbers path); Grafana admin/admin.
- **[MEDIUM · confirmed/partial]:** the ghcr release path has never run (workflows target `main`; default
  branch is `master`; zero tags ever); nightly ingest failures are silent (the one freshness watermark
  advances on ETL success even when profiles/tiles fail, and isn't in /metrics); no log rotation on the
  disk-full-prone host; `ofelia:latest` unpinned while holding the Docker socket (root-equivalent), actions
  tag-pinned not SHA-pinned, no Python lock file; no host-reboot runbook (the scheduler profile never comes
  back with a plain `up`).

### Version-control process (gap audit)
- **[HIGH] SIM-437 — declared CLOSED 2026-06-22 — exists only as uncommitted working-tree changes** (HEAD
  is 2026-06-06; nothing since exists on any branch or the remote). The production stack executes this
  never-CI'd code via bind mounts. The refactor itself was verified line-by-line as high quality (a strict
  superset that fixes a real NaN latent bug) — the failure is purely process.
- **[HIGH · confirmed] The tracked, modified loaders hard-import the UNTRACKED `coercion.py`** — `git
  commit -am` produces a broken master (CI fails at collection); `git clean -fd` (routine here per the
  Docker-disk habits) permanently deletes it and crashes the container's next ETL run; `git checkout .`
  silently reverts the NaN fix. Each is one habitual command away.
- **[MEDIUM · confirmed] The documented definition-of-done omits version control entirely** — CLAUDE.md's
  workflow ends at "document"; commit/push/CI-green appear nowhere, so "CLOSED with zero commits" followed
  the written process to the letter. Fix: a ticket is not CLOSED until its closing commit hash exists and
  CI is green on it; record the hash in the BACKLOG row.
- Also: the 192KB CODE_REVIEW_CHECKLIST.md (referenced as a SIM-437 deliverable) is untracked; `.claude/`
  is untracked AND unignored (`git add .` would publish local tool permissions).

---

## Theme 8 — Performance (beyond the tracked SIM-436)

All confirmed; the headline is that meaningful wins remain that do NOT require new hardware:
- **Half-inning-constant pitcher factor recomputed every PA** (the refresh key includes batter_id — ~6-7
  redundant full-pool passes per PA; a one-line early-return, byte-identical).
- **No cross-iteration memoization** although 100 iterations replay the same game through one warm sampler
  (a bounded LRU on (hand,pitcher,batter)→product eliminates 3+ O(N) passes per hit; exact).
- **Per-PA allocation churn:** all 12 count-bucket CDFs built eagerly in float64 (~600 MB churn/game) though
  a PA visits 3-5; redundant `.astype` copies (4 sites). Lazy CDFs are byte-identical (no RNG in construction).
- **/boxscore //props path gets none of the parallelism work** (see Theme 5) — the biggest user-facing win.
- **Memory:** parent holds a permanent private duplicate of every published shared array + the whole bundle
  pinned in the lifespan frame (hundreds of MB of OOM headroom; note the verifier's caveat — clear the
  registry-guard order first); object-dtype categorical pool columns defeat the shared-memory seam
  (int8-encode outcome_type/event/p_throws); derived structures rebuilt per worker with Python-level loops
  instead of baked into the artifact.
- **The plateau's parent-side share** (result unpickling/aggregation) would also shrink if workers returned
  compact arrays rather than full result objects.

---

## Prioritized action plan

**Now (days, mostly small diffs — restore trust in the instrument):**
1. Commit and push SIM-437 (one commit; unblocks everything else being CI-adjudicated).
2. Fix the odds UTC-date bug (1.1) and re-audit the 2024 backfill; fix line-move handling (1.2), calibrated
   win-prob (1.4), tie handling (1.5), MC-noise gating + Wilson CIs (1.6/1.7).
3. Fix the four confirmed sim bugs: pitcher resurrection (2.1), zeroed steals (2.2), phantom DP runner (2.3),
   ROE→outs (2.4). Each is a small, testable diff with a predicted, measurable effect on the run gap.
4. Backups: nightly pg_dump + DuckDB file copy outside the vhdx; one restore drill.
5. Re-run the season CLV backtest on the repaired instrument + leakage-free bundles (1.3) before drawing
   any further edge conclusions.

**Next (1-2 sprints — make production self-verifying):**
6. Production-configuration regression lane: simulate_game golden with SIM_FULL_POOL=1 + flags ON over a
   committed toy artifact bundle; extend the engine golden gate to pitcher/batter/fielder; :memory:
   DuckDB schema-contract test running every engine's real build().
7. Kill the silent-degradation ladder: ERROR logs in both factory except blocks, engines/calibration/sampler
   state on /ready + /metrics, provenance stamp (flags, calibration hash, artifact build id) on
   SimulateResponse and the backtest JSON; fail boot in production when the full-pool bundle can't load.
8. DuckDB migration runner (~50 lines: diff migration_history vs the directory, apply in order in a
   transaction) wired into make, nightly step 0, and a boot check; explicit column list on the outcome-pool
   INSERT; add the four missing tables to 02_duckdb_schema.sql.
9. Re-run the flag-enablement validation at the pre-registered ≥400×≥20 bar, one flag at a time, with the
   harness first fixed to (a) actually pass defense maps + park factor, (b) implement the promised
   per-channel breakouts, (c) report seed-paired deltas with CIs, (d) record full flag/SHA provenance.
10. Wire /edges//signals to raw.game_odds (mock only behind an explicit dev gate); fix run_line/runline,
    the field-graphic predicate, and the BettingCard double-sim.

**Then (the standing program):**
11. Prod compose overlay (runtime image, no reload, real env tiers), credentials + localhost port bindings,
    Alertmanager + cadvisor with a pool-exercising readiness probe, log rotation, branch-filter fix so
    releases actually publish.
12. Model-level design work, informed by the repaired instrument: stratify the batted-ball draw on
    (outs, runners_state) like the count buckets; per-event park factors (the HR channel — the data already
    exists in derived.park_factors); split HFA across channels and apply the measured retune; add
    HBP/WP/PB; pitcher-conditioned contact; TTO outcome tilt; wire calibration into the sampler
    (sigmas + pitcher-sim artifact + engine-weighted batter embedding).
13. Perf: the four exact hot-loop fixes, pooled boxscore retention for /props, int8 categorical columns.

---

## Complete finding index

Verdicts: ✅ confirmed · ◐ partial · ⏳ unverified (rate-limited verifier) · ▫ unverified (low-sev, by design).
Severity shown is post-verification.

### Chief Software Architect (11)
| Sev | V | Finding |
|---|---|---|
| high | ✅ | CLV backtest prices moneyline off uncalibrated win prob (production bets calibrated) |
| high | ✅ | Silent model-degradation ladder; zero provenance on results |
| high | ✅ | CI/golden gate certify only the fallback path; no production-config drift gate |
| medium | ✅ | ~23 scattered env vars, three incompatible boolean grammars ('off' enables the live pipeline) |
| medium | ◐ | Domain logic trapped in api/routes/games.py, imported privately by calibration + CLV scripts |
| medium | ✅ | sim_loop.py: 7 concerns in one ~2,500-line class (extraction pattern already proven) |
| medium | ✅ | FastAPI routers defined inside pipeline layer |
| medium | ✅ | 480 lines of score-fusion code dead in production by its own documentation |
| medium | ◐ | games.py (1,876 lines) mixes wire schema, SQL, orchestration, persistence |
| low | ▫ | Duplicated mp-context plumbing (batch_runner ↔ clv_backtest) |
| low | ▫ | to_jsonable str() fallback contradicts its no-silent-loss contract |

### Quantitative ML / Modeling (12)
| Sev | V | Finding |
|---|---|---|
| critical | ✅ | Look-ahead leakage: 2024 backtests sample 2025-26, future up-weighted |
| critical | ✅ | CLV computed across line moves (no line-equality check) |
| high | ✅ | Backtest moneyline uses identity win-prob map |
| medium | ◐ | Full-pool sampler bypasses the similarity-calibration layer |
| high | ✅ | Situation kernel: bitmask+raw-inning Euclidean geometry → RISP conditioning ≈ noise |
| high | ✅ | min_edge=0 at n=100 measures the noise floor; no uncertainty reported |
| medium | ◐ | Reliability curve: 1-game bins, running-max monotonization, fit⊂eval overlap |
| medium | ✅ | Missing profiles get weight 1.0 = self-similarity max |
| medium | ◐ | Embeddings 0-fill before z-scoring → phantom multi-sigma distances |
| medium | ✅ | HFA routed 100% through home-batting BABIP; known overshoot never retuned |
| medium | ✅ | EB shrinkage inert for batters; calibrated priors never consumed |
| low | ▫ | No ESS diagnostic/tempering on the product-of-kernels weight |

### Baseball Research Analyst (12)
| Sev | V | Finding |
|---|---|---|
| critical | ✅ | Pulled pitchers resurrected every half-inning (SIM_MANAGER on) |
| high | ✅ | SIM_MANAGER silently zeroed all stolen bases |
| high | ✅ | Double plays never remove the doubled-off runner (phantom runners) |
| high | ✅ | Reach-on-error converted into outs (~0.25-0.35 R/team-game suppressed) |
| high | ✅ | Park factor has no HR/XBH channel |
| medium | ✅ | HBP/WP/PB/balk/pickoff absent; D3K can never fire in production |
| medium | ✅ | HFA = home-batter singles only, at overshot magnitude |
| medium | ✅ | TTO/fatigue never degrade outcomes (sequencing mechanism for the run gap) |
| medium | ✅ | Situation RBF geometry (bitmask/unscaled dims) |
| medium | ✅ | Batted-ball draw pitcher-blind given contact |
| medium | ✅ | R1 never advances on non-DP ground outs; FC retires wrong player |
| low | ▫ | Manager small-ball is decorative (sac bunt no-op; hit-and-run suppresses running) |

### Data Engineer (10)
| Sev | V | Finding |
|---|---|---|
| critical | ✅ | BettingPros keyed on UTC date → wrong-game odds (empirically ~26-43% CT/MT/PT) |
| high | ✅ | Forward CLV capture is dead wiring (opening job unscheduled; closing markers uncalled) |
| high | ✅ | Engine artifacts unversioned, non-atomic, absent from nightly |
| high | ✅ | No derived-side data-quality gate (DP-rate bug class can recur) |
| medium | ✅ | Nightly computor: no transactions/checkpoints; DELETE→INSERT windows |
| medium | ✅ | Live slate uses server-local date.today() (West-Coast games fall off) |
| medium | ✅ | No retry/backoff/failure-ledger on BettingPros+bullpen HTTP (plus negative-caching of blips) |
| medium | ✅ | _ensure_venue retry loops lack break-on-success; unguarded Savant scrape |
| low | ▫ | wind_speed/wind_direction read from keys the feed doesn't provide (always NULL) |
| low | ▫ | Fire-and-forget create_task without held references |

### Backend / API (12)
| Sev | V | Finding |
|---|---|---|
| high | ✅ | No pool timeout / BrokenProcessPool recovery — one dead worker bricks /simulate |
| high | ✅ | Per-request n_iterations resizes the shared pool; n=1 runs in the API parent |
| high | ✅ | /boxscore //props: serial parent-process sims bypassing pool, cache, and auth |
| high | ✅ | /edges //signals price off mock odds by default; real stored odds never read |
| high | ◐ | Deployed stack auth-disabled (dev env); non-dev fails open on empty config |
| medium | ✅ | /plays returns ALL runs concatenated |
| medium | ✅ | Shared DuckDB connection cross-thread without cursors |
| medium | ✅ | Rate limiter bypassable via arbitrary X-API-Key; unbounded buckets |
| medium | ✅ | WS: no auth/caps, leak-prone cleanup, typed schemas unwired |
| medium | ✅ | Degraded boot invisible to /ready and consumers |
| medium | ✅ | No stampede protection; pickled Redis values (RCE via open Redis) |
| low | ▫ | No request IDs/structured logs; global p95 near-meaningless; naming debt |

### Performance (10)
| Sev | V | Finding |
|---|---|---|
| medium | ✅ | Half-inning-constant pitcher factor recomputed every PA |
| high | ✅ | Prop endpoints re-simulate serially in parent while pooled boxscores are discarded |
| medium | ✅ | Parent keeps private duplicates of all shared arrays + pinned bundle |
| medium | ✅ | Object-dtype categorical columns defeat the shared-memory seam |
| medium | ✅ | Derived structures rebuilt per worker in Python loops |
| medium | ✅ | Eager float64 CDFs ×12 per PA + redundant astype copies |
| medium | ✅ | No cross-iteration matchup-weight memoization |
| medium | ◐ | Perf harness/PERF_STRICT never execute the production path |
| low | ▫ | Nightly still serializes the legacy 2 GB dict to JSON in the npz |
| low | ▫ | Dead hot-path method _f_situation |

### Betting Markets Quant (13)
| Sev | V | Finding |
|---|---|---|
| critical | ✅ | CLV across different lines on 9 of 10 markets |
| high | ✅ | Moneyline scored with identity win prob |
| high | ✅ | Unmoved lines count as losses (48.81%→51.05% excl-ties; HR 48.9→53.7) |
| high | ◐ | n=100 noise + min_edge=0: bets on sampling noise; winner's curse |
| high | ✅ | No CIs / sample-size gating; 120 games can't distinguish 49% from 53% |
| high | ✅ | Closing line = unverifiable max-updated cross-book proxy, provenance erased |
| high | ✅ | Push mass charged as full loss in EV and Kelly |
| medium | ✅ | Degenerate 0/1 probs silently dropped (largest claimed edges excluded) |
| medium | ✅ | No source/is_mock filter on backtest odds reads |
| medium | ✅ | Over/under legs from different books/lines de-vigged as one market |
| medium | ✅ | Proportional devig only; method sensitivity unmeasured |
| medium | ◐ | Slate selection bias (pk-ordered cap: WSH in 81/120 games; uncounted drops) |
| medium | ▫ | Scoreboard measures raw picks, not the gated/Kelly-weighted strategy |

### QA / Test (10)
| Sev | V | Finding |
|---|---|---|
| critical | ✅ | CLV scoreboard scores across moved lines — untested and unguarded |
| high | ✅ | Production sim path has zero automated behavioral coverage |
| high | ✅ | Regression gate = 5/11 engines, none driving the production sampler |
| high | ✅ | __new__ bypass leaves DuckDB SQL contracts untested (SIM-408 class) |
| medium | ⏳ | Coverage gate measures a curated denominator excluding highest-risk modules |
| medium | ✅ | PERF_STRICT hard-gates an rng stub, weekly |
| medium | ✅ | Byte-identity claims verified manually, not by any gate (and false at report level for parallel CLV) |
| medium | ✅ | Frontend: 4 smoke tests total; money surfaces never rendered with data |
| low | ▫ | Golden test mutates module-scoped fixture without try/finally |
| low | ▫ | Tracked .py.tmp; local --tb=short (the renderer CI abandoned) |

### DevOps / SRE (11)
| Sev | V | Finding |
|---|---|---|
| high | ✅ | No backup/DR for any datastore (severity high after verifier's recoverability analysis) |
| medium | ✅ | Release workflows target 'main'; default branch is 'master' — ghcr never ran |
| high | ✅ | Production runs the dev compose config; --env-file tier story broken (→ silent auth-less prod) |
| high | ✅ | Zero alerting; no memory metrics; probes green through the worst failure |
| high | ✅ | Wired-in DuckDB single-writer conflict (replay RW + nightly rebuild) |
| high | ✅ | Default credentials in 16 files + hardcoded compose DSN + published ports + open Redis |
| medium | ◐ | Nightly failures silent (existing freshness watermark false-greens) |
| medium | ✅ | No log rotation/aggregation on a disk-full-prone host |
| medium | ✅ | Supply chain: ofelia:latest w/ Docker socket; tag-pinned actions; no lock file |
| medium | ✅ | Single Windows/WSL2 host; no reboot-recovery runbook |
| low | ▫ | Makefile drift (migrate bypasses the migrate service; coverage scope diverges) |

### Frontend (11)
| Sev | V | Finding |
|---|---|---|
| high | ✅ | run_line vs runline mismatch: tab always 422s; mock-odds badge never renders |
| high | ✅ | Field graphic renders every base occupied on live games |
| medium | ✅ | Dead OpenAPI type pipeline; openapi.json drifted; no CI diff |
| high | ✅ | BettingCard: two concurrent unseeded 200-iter sims; cross-sim signal/edge pairing |
| medium | ✅ | Sub-resource errors never rendered; no loading state; Retry-After unconsumed |
| medium | ✅ | No revalidation: scheduled games never flip live; re-sims unreachable |
| medium | ✅ | Zero component/unit tests |
| medium | ✅ | Slate cards score-less; lineup_ready returned by API but unconsumed |
| medium | ◐ | No centralized 401 handling (blast radius: the 3 auth-gated panels) |
| low | ▫ | Dead OverridePanelV1; empty root dirs; stray Phase-2 HTML |
| low | ▫ | App shell inline styles bypass the token system |

### Gap audits (17)
| Sev | V | Finding |
|---|---|---|
| high | ⏳ | SIM-437 "CLOSED" exists only as uncommitted working-tree changes |
| high | ⏳ | Production executes this never-CI'd code via bind mounts |
| high | ✅ | Tracked loaders hard-import untracked coercion.py (commit -am / clean -fd / checkout traps) |
| medium | ✅ | Definition-of-done omits version control entirely |
| low | ▫ | Untracked 192KB checklist referenced by docs; .claude/ unignored |
| critical | ⏳ | Schema lag silently reverts validated realism while flags stay ON |
| high | ⏳ | No DuckDB migration runner or boot-time schema verification |
| high | ⏳ | Positional INSERT…SELECT * into outcome_pool (silent column transposition) |
| high | ⏳ | 02_duckdb_schema.sql missing 4 migration-created tables incl. migration_history |
| medium | ⏳ | _run_schema_ddl sentinel skip: cannot evolve an existing DB |
| medium | ✅ | Migration docs point at the wrong database file |
| medium | ✅ | migration_history ledger is write-only (no reader anywhere) |
| critical | ⏳ | Flags enabled at 3-4 games vs the pre-registered ≥400×≥20 gate, relabeled "validated" |
| high | ⏳ | sim_stats.py structurally cannot exercise SIM_PARK_FACTOR / SIM_FIELDER_RBF |
| high | ✅ | Promised per-channel breakouts don't exist; enablement metrics irreproducible |
| high | ⏳ | Harness SE metric ignores between-game variance; no paired-delta mode |
| medium | ✅ | Validation artifacts record almost no configuration provenance |

---

*Produced by a 127-agent audit workflow (10 role auditors + 96 adversarial verifiers + completeness critic +
3 gap auditors + gap verification), 2026-07-13. Verifier evidence, including the live-DB empirical checks
(wrong-game odds signature; tie-rate analysis of /data/clv_backtest_2024.json), is preserved in the
session transcript.*
