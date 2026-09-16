# SIM-548 — the joint-fit instruments' adversarial review (2026-09-15)

> Produced by a four-reviewer / three-refuter workflow (137 agents) on the instruments built
> 2026-09-14/15: `scripts/sim548_offline_fit.py`, `sim548_design.py`, `sim548_calibrate_markets.py`,
> `sim548_market_skill.py`, the `FullPoolSampler.result_weights` refactor and the backtest's
> `sim_prob_raw`. 44 findings raised, 40 survived the refuters. What was done with it: the
> design runner's three defects, the calibration fitter's Newton and isotonic defects, the
> skill table's composite range and the missing `SIM_SIT_SIGMA` were fixed the same day (a
> second workflow, three fixers + three verifiers, then two follow-ups from the verifiers: the
> repeats' seed stride and the convergence test's order); the offline fit was rewritten as v2
> (whole starts as the test set; the plate-appearance count chain and the per-start strikeout
> read; admissible-row effective sizes; the unbiased chain; the season-share yardstick; a
> valid checkpoint; the profile-season leak check; the 2025 hold-out step). The design verdict
> (§4) sets the designed experiment's factors. Record: `CHANGES.md` 2026-09-15.

# Review: the joint-fit instruments (SIM-548)

Verdict: the code mirrors the loop faithfully where it matters (cutoff, keys, `result_weights`), but the design read has three defects that would produce wrong numbers on the first real run, and the offline objective ranks candidates in the wrong direction. Fix section 1 before any arm is read; adopt section 4 before the next offline run.

## 1. Defects to fix now

**High — `scripts/sim548_design.py:211`. One sparse market shrinks every market's game set to a few dozen games, or to zero.** `analyze` intersects the game sets of ALL markets, then across arms. The first-inning total has ~25 records on 250 games; on the repo's own 250-game reports the all-market intersection is 0 games. Every market's surface then reads an effect of exactly 0.0 with a bootstrap range of 0.0, while `n_records` still prints 4,400 — a silent, all-zero read that looks like "no effect". Fix: compute the common games PER MARKET (intersect that market's games across arms only), run `arm_game_means` and the bootstrap on those, and apply `min_n` to records on those games. For the composite, resample one game index over the union and let an absent market contribute 0/0, as `sim548_market_skill.composite` already does.

**High — `scripts/sim548_design.py:105, 276`. The "centre" arms sit at the production corner, so the "curvature" is not curvature.** `build_arms` gives every centre repeat the low (production) level of every factor; `analyze` then reports `mean(corners) − mean(repeats)`. Under a purely additive model that equals half the sum of the main effects. On the script's own planted-effect fixture (factor A shifts K by 0.2, no curvature) it prints curvature +0.0139, range [+0.0075, +0.0225], "clear of zero". Also `centre1` reuses `run01`'s seed, so the noise floor rests on two seeds plus a copy. Fix: keep the repeats as the noise floor and rename them `baseline`; offset their seeds; compute curvature only from true midpoint arms (power 8 between 4 and 16, batter 4 between 2 and 8; an ON/OFF factor at both levels) or drop the field.

**High — `scripts/sim548_design.py:222-251`. At six factors the 22-column matrix on 16 rows is rank 14; aliased interactions are printed at half or a third of their contrast.** With E=ABC, F=BCD the two-way products fall into six identical pairs and one triple (AE=BC=DF). `lstsq` splits the coefficient equally, so each printed effect and range is 1/2 or 1/3 of the estimable sum, and the composite prints the same Z two or three times. `--analyze-only` on a partial design fits the same matrix on fewer rows with no warning. Fix: build one column per alias group named `AB=CE`; refuse the read when a factorial arm is missing. (Five factors with E=ABCD is resolution V and clean; the docstring calls it IV.)

**Medium — `scripts/sim548_offline_fit.py:611`. The checkpoint resumes across seasons and sampling schemes.** The check compares only `n_pitches` and `seed`. `--season 2025` with the same count and seed loads every 2024 score and labels it the 2025 hold-out. Fix: store season, `per_pitcher`, `min_pitcher_rows`, chain settings and a hash of the sampled (hand, i) ids; refuse on mismatch. Also normalise the path to `.npz` (line 624): `np.savez('fit.ckpt')` writes `fit.ckpt.npz`, which the resume never finds.

**Medium — `scripts/sim548_offline_fit.py:71`. `sit_sigma` is laddered but nothing can set it.** No `SIM_SIT_SIGMA` exists in `production_factory.py`, so the design cannot run that arm and a chosen value cannot land. Add the env var (and provenance) or drop the rung.

**Medium — `scripts/sim548_calibrate_markets.py:100, 130, 225.** (a) The Newton fit has no step control or convergence flag; on over-spread data (slope near 0.3 — the strikeout market's own regime) it diverges on 15-80% of simulated fits, half of which then collapse to the base rate as "no information". Add step-halving on the log-likelihood and a `converged` flag; never write an unconverged map. (b) The isotonic fit does not group tied probabilities; on a 0.01 grid (100 iterations) the map depends on record order and p=0.5 can map to 1.0. Group by unique p and run weighted PAV. (c) The moneyline "before" and "map gain" are scored against the raw win share, not the mapped probability production shows; fit on `sim_prob_raw` but score "before" on `sim_prob`.

**Low.** `sim548_market_skill.py:348`: Z's range is ±1.96·sd while every other range is a percentile; a zero-sd market is dropped from the sum but kept in the normaliser. `sim548_offline_fit.py:160`: an unknown side (−1) maps to home (nil today; add the guard). `:246`: no catcher key, so an enabled receiving ratio is silently absent — assert it is off until SIM-526.

## 2. Statistical corrections

- **ESS share and row count include cutoff-zeroed rows (`:218, :273`).** A 2024 pitch admits ~30-60% of a 2023-2026 sub-cell; the printed 2% / 1% shares and ~2,400 rows are understated by ~2.5×. Effective ROWS (~45 / ~18) are right. Divide by `(w > 0).sum()` and report both counts. State the starvation rule in effective rows (10th percentile ≥ 20), not share — production already sits at 0.02 against the plan's 0.1.
- **The chain truncates to the top-40 anchors and renormalises (`:288`).** At power 16 the 40 rows hold most of the mass; at power 1-4 they hold a few percent and are the pitcher's closest look-alikes. The truncation bias moves along the very ladder the chain exists to read. Draw K anchors i.i.d. from `p_a` with a pitch-seeded stream (unbiased at every power), report the covered mass, and add an anchor's own outcome when `result_weights` is None (the loop's fallback, `:302`).
- **The discrimination read (`:389-417`) uses a 40-pitch real share as the yardstick.** Its binomial sd (~0.07) exceeds the true between-pitcher spread (~0.03-0.05), so a perfect model reads spread_ratio ~0.4 and the predicted-on-real slope is diluted ~4×. Use the pitcher's season share from rows outside the sampled pitches, regress real on predicted, and de-attenuate the correlation.
- **The bootstrap clusters by (pitcher, day) — 1.25 pitches per group (`:380`).** Cluster by pitcher too and report the wider range.

## 3. Fidelity divergences

Verified faithful: the cutoff (day before, inclusive), hand pools, `pid:season` keys, `result_weights` as the one function the draw samples, the chain's anchor rows, and the fatigue inputs. The unit test at `test_sim548_instruments.py:277` holds only at batter power 1 on the whole-pool path, but the fit's fidelity is structural (it reads `diff(_bucket_cdf)` and `result_weights` directly), so this is a test-scope note, not a defect.

Divergences that do not bias the ranking: the missing catcher (ratio OFF today); the −1 side (no such rows). One that does: **both the fit and the backtest key the pitcher and batter to full-season profiles**, which include the scored pitch. `set_asof` cuts rows, not profiles; the batter engine carries whiff/contact/K rates. The leak favours sharper powers and the 2025 hold-out inherits it. Run one setting with `{id}:2023` keys and stamp the size in §8 (mind the confound: rookies fall to a flat factor, and the pitcher matrix's diagonal is 0, not 1).

## 4. Design verdict

**The per-pitch six-outcome Brier is not sufficient.** It decomposes as uncertainty − resolution + reliability. The estimator-noise term is ≈ 0.766/ESS: 0.017 at 45 rows, 0.043 at 18. The resolution a pitcher's identity can add is bounded by the between-pitcher variance, ≈ 0.004-0.01. Every ladder in the partial run prefers its flattest value with no interior optimum — the signature of variance domination — while the market read at power 16 shows AUC 0.55 against 0.51. The two reads do not conflict; they measure different aggregation levels. The market grade carries the same bias one level up: raw Brier charges the fixable 9.5-point bias and 3× over-spread in full, and the K map recovers 0.020 — ten times the market's resolvable skill.

Changes: (1) score per plate appearance — run the 12 count buckets as an absorbing chain to K/BB/HBP/in-play and score a four-way Brier; (2) sum P(K) per starter-game and read the calibration slope and correlation against real K; (3) report the Murphy decomposition at every level; (4) add a two-strike-only Brier and a per-bucket effective-rows table; (5) in the market composite, rank on the check-set calibrated Brier with a guard that raw Brier must not worsen by more than the map recovers, and report AUC beside it; (6) restrict the test set to starters in games with a strikeout line (the plan's own definition) and report relievers separately.

**The designed experiment: four factors, full 2^4, not six at resolution IV.** Levels: pitch_pitcher_power 16 vs 4; result_pitcher_power 16 vs 4; result_batter_power 8 vs 2; fatigue_tto_sigma 0.5 vs OFF. Reasons: these carry offline swings of 0.02-0.07; sit_sigma, result_pitch_sigma, density and pitch_batter_power swing 0.001-0.003 against a market that resolves ~0.010 and cannot be seen. The split switch must stay out of the factorial: with the split OFF the two result powers are inert, so their main effect equals their switch interaction and the alias structure breaks. Instead run two supplementary arms (split OFF at pitch power 16 and 4). Separating the pitch and result pitcher powers, which moved 1→16 together on 2026-09-14, is the design's first job. Add three baseline repeats and two true centre arms at (8, 8, 4) with fatigue at each level. If a fifth factor is wanted, take `SIM_BB_BATTER_POWER` 4 vs 2 with E=ABCD (resolution V).

## 5. Before the offline fit is trusted

1. Fix the three design-script defects and the checkpoint; add tests with sparse markets and a k=6 planted interaction.
2. Re-run with the PA-level and starter-game scores, the corrected ESS denominator, the unbiased chain, and the starter filter.
3. Build the `--holdout-season 2025 --top 3` step: score production plus the top three on fresh 2025 pitches and print both deltas side by side; a candidate proceeds only if its 2025 sign holds.
4. Write the candidate rule into §6.2: a weight enters when its starter-game read moves beyond its pitcher-clustered range, holds on 2025, and keeps effective rows above the floor.
5. Run the prior-season-key check and record the leak's size.
6. Add `SIM_SIT_SIGMA` or drop it from the ladder.