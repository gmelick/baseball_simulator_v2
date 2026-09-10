# SIM-510→513 — the fielding transition draw: the owner's design (2026-08-18)

**Read this first if you are picking up the loop-finalization epic in a fresh context.**
It captures the owner's design decisions, the holes we closed during review, the measured
anchors, and the build order. `BACKLOG.md`'s 2026-08-18 SIM-510 banner holds the ticket rows.
Next free ticket id after this filing: **SIM-514**.

---

## Why this epic exists

The owner ruled: finalize the simulation loop BEFORE fine-tuning individual statistics.
The loop's ball-in-play resolution is the last place that violates the standing
architecture rule (2026-08-10: *every decision is a similarity-weighted draw from a
hard-filtered pool, never a hand-tuned formula*):

* Outs are re-derived from event labels; the pool's own (now-correct) `result_outs` is
  deliberately discarded (`simulation/sim_loop.py` ~1504, the SIM-473 note).
* Runner destinations come from hand-tuned constants: tag-up 0.92, deep-fly advance 0.30,
  ground-out advances 0.28/0.35, `_extra_advance` Retrosheet-tuned extra bases.
* A double play charges two outs but REMOVES NO RUNNER (`runners_retired=0` on every
  in-play path — the comment at ~2694 admits it), and a phantom-DP guard relabels ~55% of
  drawn DPs. Measured: DP 0.16-0.20/team-game vs the 2025 centre 0.7459 (SIM-494,
  strict-xfail on the DP band).
* A drawn reach-on-error becomes a one-out `field_out` (outs inferred as
  `0 if hits > 0 else 1`) AND `simulation/constants.py:177` aliases `field_error` to a
  single, so the batter is retired on the bases and credited a hit at the plate. Nothing
  reaches base on an error today (SIM-496; `ROE_reached` reads 0.0, strict-xfail).
* The loop's `_OUT_EVENTS` lists `fielders_choice` as a batter out — wrong for that
  question (the batter reaches on 90.5% of those rows; pinned by
  `tests/unit/test_sim501a_out_label.py`).

## The owner's design (stated 2026-08-18, verbatim in substance)

**The fielding draw IS the play for the majority of ball-in-play events.** One
similarity-weighted draw over REAL transition rows, hard-filtered so the drawn row is
legal in the live base-out state. The drawn row determines the event, the out count,
WHO is out, and all forced/automatic runner movement. No formula tree decides who is out.

**The advancement draw fires ONLY in five discretionary scenarios** — the plays where the
specific runner's speed and the specific defender's arm matter:

1. first → third on a single
2. second → home on a single
3. first → home on a double
4. tagging up on a fly ball (any runner; 3B → home is the sac fly)
5. the batter stretching a hit to an extra base

Everything else — forces, productive-out advancement, double plays, fielder's choices,
infield hits, doubled-off runners on liners, compound error plays — comes from the
fielding row at real data frequencies. Station-to-station is automatic on hits (a double
scores the runner from second without a draw).

## Decisions closed during design review — do not relitigate

1. **The double-count guard (critical).** Real transition rows carry discretionary
   advancement baked in ("single, runner scored from second"). The fielding draw must
   NORMALIZE the five enumerated movements to station-to-station and hand them to the
   advancement draw as the sole authority. The rows' baked-in sends are not wasted: they
   are the DATA for the advancement opportunity pools (attempted / outcome per
   opportunity, non-attempts included — the SIM-468 denominator lesson).
2. **The fielding draw's hard filter includes the base-out state** (runner configuration
   × outs, alongside the batted-ball conditioning). This is what makes "the drawn row is
   the play" safe and is what deletes the phantom-DP guard. This is the SIM-467 cell
   framework applied to the outcome pool. **AMENDED 2026-08-19 (owner): NO widening.** The
   base-out filter is essential, so it never relaxes. There are only 24 base-out cells and
   all are common. The count is soft conditioning, never a hard filter. An empty cell is a
   data defect — raise, do not widen. The SIM-475 machinery stays out of this epic. *(This
   decision said "Thin cells use the SIM-475 widening order" until 2026-08-19.)*
3. **Third-out timing (Rule 5.08).** Every advancement-draw out is a TAG play by
   construction, so lead-first resolution gives run timing almost free: a run that
   crossed before a trailing runner's tag-out counts. Batter-runner force cases live in
   the fielding row.
4. **Advance-on-the-throw** (runner on 3B holds, batter takes 2B behind the throw) is
   FOLDED into the stretch scenario. Accepted approximation; measure later.
5. **Lead-runner short-circuit:** if the lead runner does not attempt, no trailing draws
   — EXCEPT the batter-stretch draw, which stands alone (decision 4).
6. **The batter's final base:** the fielding row's hit class is normalized conservative
   (single = batter at 1B, double = 2B); the stretch draw owns extra bases. Never both.
7. **Orphaned knobs:** the SIM-349 sac-fly-intent nudge is SUPERSEDED (a tag draw from
   3B produces sac flies naturally — label the play `sacrifice_fly` post-hoc for
   AB/RBI). The SIM-412 home-field, SIM-411 park, and SIM-425b fielder-RBF nudges act
   upstream (out↔single flips) and MUST be re-validated against the new draws — do not
   leave them fighting the transition draw. **AMENDED 2026-08-19 (owner): the re-validation
   lives in SIM-491, AFTER SIM-513 lands. SIM-513 contains no validation.** *(This decision
   said "in the certification ticket" until 2026-08-19.)*
8. **SIM-496's alias half** (`constants.py:177` field_error → single) is fixed inside
   SIM-511 — the drawn row carries batter-reached truth, so the alias goes.

## Data support (verified present after the 2026-08 rebuild)

* `raw.pitches` carries pre-pitch `on_1b/2b/3b` AND post-play `post_on_1b/2b/3b`,
  `runner_*_scored`, `runner_*_out_advancing` — the transition truth per PA-ending row
  (the baserunner profiles already read them).
* The batted-ball pool rows carry `fielder_pos`/`fielder_id` (SIM-425b), spray angle,
  EV, LA, distance — the geometry + the specific defender for the advancement features.
* OF arm metrics (`_compute_outfield_arm_metrics`) and sprint speed exist in the
  embeddings. `sim.outcome_pool.result_outs` is events-derived and correct (sim501.1+).
* No re-sweep is needed for any of this epic — pool/artifact rebuilds only (minutes,
  not hours). Remember `--seasons 2017 … 2026 --full-rebuild` semantics and the
  `POOL_BUILDER_VERSION` bump on ANY formula change (currently `sim509.1`).

## Measured anchors the acceptance run must hit (2025 references, SIM-508)

* DP 0.7459/team-game (sim ~0.17 → the strict xfail on `test_double_play_band_sim450`
  comes OUT when it lands; same for H and ROE_reached).
* ROE_reached 0.2078 (sim 0.0 today). ROE (drawn) already ~in band.
* The 2025-band lane = 12 games × 471 iterations (`SIM_ACCEPTANCE_ITERS=471`,
  n=11,304); current scoreboard: K/H green; BB +12.8%, R +6.0%, HR +6.5%, 2B +5.2%,
  SB +5.7% red; CS/3B/ROE unresolved (sim spread > sd_ref — re-check after this epic:
  correct DP/ROE mechanics may reduce the overdispersion).
* Fixing DP/ROE moves R in BOTH directions (DPs shorten innings, ROE adds runners) —
  re-read the whole scoreboard after, per the validation-caveat rule.

## Build order

SIM-510 (data columns + pools) → SIM-511 (the transition fielding draw) → SIM-512 (the
five-scenario advancement draw) → SIM-513 (retire the legacy paths + certify). 511 and
512 are each roughly a SIM-507-sized build. **AMENDED 2026-08-19 (owner): SIM-511 and
SIM-512 land as ONE combined change — no feature flag. The station-to-station interim
state never reaches `master`.** The old SIM-462/472/473 rows in the
2026-08-10 BACKLOG table are SUPERSEDED by these tickets (SIM-472 — pitch-similarity
into the batted-ball draw — stays open separately; it is not part of this epic).

## Operational reminders for the fresh context

* Detached long jobs: `docker compose run -d --name <x>` (never PowerShell
  Start-Process); the lane env is `SIM_ACCEPTANCE=1 SIM_ACCEPTANCE_ITERS=471
  SIM_ACCEPTANCE_TIMEOUT=16200 SIM_RUNNER_WORKERS=6` + production flags, with `tests/`
  and `scripts/` volume-mounted (`MSYS_NO_PATHCONV=1` from Git Bash).
* The positional-INSERT trap: `sim.*` pool INSERTs have no column list — DDL order in
  `db/schemas/02_duckdb_schema.sql`, the ALTER migration, and the builder SELECT must
  match, new columns LAST (`test_the_positional_insert_matches_the_ddl` guards the
  steal pool; clone the pattern).
* DuckDB migration numbering: next is 0018 (schema v17 → v18) + bump
  `db/schemas/duckdb_schema_version.txt` + the `test_sim_store.py` version test.
* Validate ETL/pool work on HUNDREDS of games, never dozens.
