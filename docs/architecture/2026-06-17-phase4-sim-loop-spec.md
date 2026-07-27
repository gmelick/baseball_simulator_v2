# Phase 4 Simulation-Loop Spec — The Canonical Plate-Appearance Loop

*Ticket: SIM-310 · Owner: Backend Developer (lead) + Baseball Analyst · Date: 2026-06-17*
*Status: AUTHORITATIVE — this is the single source of truth for the Phase 4
simulation loop. It reconciles the two pre-existing, conflicting loop definitions
(`README.md` "Simulation Engine" §; `docs/perf/2026-06-03-sim-loop-time-budget.md`
§2) into ONE ordering. Where this doc and any other source disagree on loop
ordering or terminal logic, **this doc wins**. It is a SPEC — no production code.
It is the reference SIM-311/316/317/318/319/320/321 build against.*

---

## 0. Purpose and scope

`simulation/sim_loop.py` is today a clearly-marked **single-pitch scaffold**
(SIM-303): `PlateAppearanceSimulator.simulate_pitch` samples exactly one pitch
through the SIM-302 `PlayPoolSampler` and, on contact, one batted ball — then
stops. Its query "fingerprints" are synthetic hashes of the count/outs
(`_pitch_fingerprint`, `_battedball_fingerprint`), explicitly TODO-marked for
Phase 4. There is no count machine, no terminal PA logic, no half-inning or game
loop, and no real engine-derived feature vectors.

This spec defines the full loop that replaces that scaffold. It is **authoritative
about ordering, the inputs/outputs of each step, and terminal conditions**. It
does **not** redefine the artifacts other P0/P1 tickets own:

- **`GameState` / `PlayResult` dataclasses** → owned by **SIM-311** (referenced, §9).
- **Run resolution / RE24 / `RUN_VALUES`↔`events` reconciliation** → owned by
  **SIM-312** (referenced, §8).
- **Cross-engine score fusion** (combining the 11 engines into one per-pitch draw)
  → owned by **SIM-321** (referenced, §7).

The reader should treat §2 (the 8 steps) and §5 (terminal logic) as the binding
contract; everything else is the rationale and the plug-in map.

---

## 1. The reconciliation problem

Two documents define a "loop" and they disagree on **what the 8 steps are** and
**where they start**:

### 1.1 What `README.md` says (the "Simulation Engine" section)

README defines an 8-step loop that **starts with steal determination** and folds
manager substitution logic into the loop body:

1. **Steal Determination** — compute steal-attempt probability from the lead
   runner, pitcher (allowing-SB), catcher (throw-out), situation, scaled by the
   manager's green-light rate.
2. **Pitch Selection** — query the pitch pool, sample a pitch vector.
3a. **Steal Outcome Resolution** *(if steal attempted)*.
3b. **Pitch Outcome Determination** *(if no steal)* — sample called/swinging/foul/
   ball/in-play; on in-play sample batted-ball params.
4. **Fielding Resolution** — fielder, outs, error; HR check precedes fielding.
5. **Baserunner Advancement**.
6. **State Update** — produce next `GameState`, emit `PlayResult`.
7. **Manager Decision Logic** — *end of PA*: evaluate substitution triggers.
8. **Loop Control** — continue until game over; inning transitions; extra-innings
   ghost runner; deterministic RNG.

**Problems with the README ordering:** (a) it makes **steal determination step 1**,
i.e. it puts a *manager pre-pitch decision* inside the per-pitch step list and ahead
of the pitch itself; (b) it branches the loop on `3a`/`3b` (two sub-steps sharing a
number), which is not a clean count of 8; (c) it places manager *substitution*
(step 7) inside the per-pitch loop even though substitutions are a **per-PA / per-
half-inning** decision, not a per-pitch one.

### 1.2 What the time-budget says (`2026-06-03-sim-loop-time-budget.md` §2)

The performance budget reconciles a **different** 8 steps, derived from
`agent_team.md` (Backend Developer), and explicitly built so every step that
"runs inside the per-pitch loop" gets a latency allocation:

1. Game-state read · 2. Pitch selection · 3. Pitch sampling · 4. Outcome
determination · 5. Batted-ball (contact) sampling · 6. Fielding resolution ·
7. Baserunner advancement · 8. State update + loop control.

**Strengths of the budget ordering:** it starts from a **game-state read** (not a
steal), it separates **pitch *selection*** (build the query vector / engine work)
from **pitch *sampling*** (the FAISS k-NN draw via the sampler), and it correctly
treats fielding/baserunning/batted-ball as **amortized** (they fire only ~17% of
pitches). It is the ordering the perf acceptance contract (SIM-119/SIM-335) already
gates against.

**Gaps in the budget ordering:** it folds steal checks loosely into step 7
("…steal checks") and does not surface manager pre-pitch decisions or substitution
as named steps, because its job is latency allocation, not loop semantics.

### 1.3 The reconciliation decision

**This spec adopts the time-budget's 8-step ordering as the canonical per-pitch
loop, with three clarifications layered on.** Rationale:

1. **A loop step list should start where computation actually starts each pitch — a
   read of current state — not with a conditional manager decision.** The README's
   "steal first" framing conflates a *manager pre-pitch decision* (which is real and
   important) with the *per-pitch mechanical loop*. We keep both, but separate them:
   the **8 steps are the per-pitch mechanics**; **manager pre-pitch decisions
   (steal / pitch-out / IBB / substitution) are a distinct pre-pitch hook** that
   runs *before* step 2 fires (§3), not as "step 1."
2. **Separating "pitch selection" (vector construction, the engine-heavy work) from
   "pitch sampling" (the sampler k-NN draw) matches the code seam already in place**
   (SIM-317 builds the fingerprint; SIM-302 `sample_pitch` does the draw) and the
   perf budget's dominant-cost line (step 2 = pitcher `query()`, ~81% of the per-
   pitch budget). The README collapses these into one "Pitch Selection" step.
3. **The perf budget's amortization is the correct semantic, not just a timing
   trick:** fielding/baserunning/batted-ball genuinely *do not run* on a ball or a
   called strike, so they belong as conditional steps (5/6/7), exactly where the
   budget puts them. The README's flat 1–8 list hides this conditionality.

The README's two pieces that the budget under-specifies — **steal as a real
pre-pitch decision** and **substitution as a real per-PA decision** — are preserved
explicitly in §3 (pre-pitch manager hook) and §5.3 (end-of-PA manager hook), so
nothing in the README is lost; it is just placed where it belongs relative to the
8 mechanical steps.

> **One-line statement of record:** *The canonical loop is the time-budget's 8
> steps (game-state read → pitch selection → pitch sampling → outcome
> determination → batted-ball sampling → fielding → baserunning → state-update +
> loop-control), with manager pre-pitch decisions (incl. steal) as a hook before
> step 2 and manager substitution as a hook at end-of-PA. The README's "steal-
> first" 8 steps are superseded.*

---

## 2. The canonical 8 steps (per pitch)

These 8 steps execute **once per pitch**. Steps 5–7 are **conditional** (they fire
only on the outcomes noted). All step I/O is in terms of the SIM-311 `GameState`
(read) and `PlayResult` (emitted) — see §9.

| # | Step | Fires | Input | Output |
|---|---|---|---|---|
| 1 | **Game-state read** | every pitch | `GameState` | immutable per-pitch *situation context* (count, outs, inning/half, base state, score diff, leverage, pitcher pitch-count, batter PA-count, park) |
| 2 | **Pitch selection** | every pitch | situation context + matchup (pitcher, batter, catcher) | the **10-dim pitch query fingerprint** (§4) + the resolved per-matchup engine weights (SIM-321) |
| 3 | **Pitch sampling** | every pitch | fingerprint + `(pitcher_id, bat_hand, season)` | one sampled `pitch_outcome ∈ {ball, called_strike, swinging_strike, foul, in_play}` (via `PlayPoolSampler.sample_pitch`) |
| 4 | **Outcome determination** | every pitch | `pitch_outcome` + live count | count advance / PA-terminal classification (§5.1), incl. the SIM-056 count-conditional foul re-weight + two-strike-foul absorbing rule |
| 5 | **Batted-ball sampling** | on `in_play` (~17%) | the **3-dim batted-ball fingerprint** (§4) + `(bat_hand, season)` | one batted-ball event (via `PlayPoolSampler.sample_batted_ball`) → EV/LA/spray-derived event |
| 6 | **Fielding resolution** | on a batted ball (~17%) | batted-ball event + defensive alignment + venue | HR check → fielder, outs recorded, error flag (fielder/catcher RBF engines) |
| 7 | **Baserunner advancement** | on contact / walk / out-with-runners | resolved event + base state | per-runner advance (mandatory + optional), runs scored (baserunner/fielder RBF) |
| 8 | **State update + loop control** | every pitch | all deltas above | commit to `GameState`, emit `PlayResult`, snapshot for replay, evaluate PA-end / half-inning-end / game-end predicates, advance |

**Notes on the conditional steps.** A `ball`, `called_strike`, `swinging_strike`,
or non-terminal `foul` exits after step 4 (then 8): no batted ball, no fielding, no
baserunner advance from a batted ball — though step 7 still runs for a **walk** (to
force runners) and step 8 always runs. An `in_play` flows 4 → 5 → 6 → 7 → 8. A
strikeout / walk is terminal at step 4 and proceeds to 7 (force/advance on walk;
drop-third-strike edge case is a step-6/7 concern flagged in §5.4) then 8.

### 2.1 Pitch selection vs pitch sampling — the seam

Step 2 ("selection") is where **all the engine work** lives: building the real
query fingerprint from game state (§4) and, per **SIM-321**, fusing the per-matchup
similarity scores (pitcher GMM, batter RBF, situation KDTree) that shape *which*
neighbourhood the sampler should weight. Step 3 ("sampling") is the **distance-pure
draw**: `PlayPoolSampler.sample_pitch` runs the FAISS k-NN, converts L2 distance →
weight (`1/(d+ε)`, the one place that conversion happens), and draws one outcome.
Keeping these as two steps preserves the score-discipline contract (engines stay
distance-pure / similarity-pure; the sampler owns distance→weight) and the
arsenal-cache-warm optimization (the matchup fingerprint can be cached for the whole
PA — SIM-119 §5 mitigation — since the matchup does not change within a PA).

---

## 3. Manager decisions BEFORE the pitch (the pre-pitch hook)

The README correctly identifies that several **manager decisions happen before a
pitch is thrown**. They are NOT one of the 8 mechanical steps; they are a **pre-
pitch hook that runs between step 1 and step 2** (after the situation context is
read, before the pitch is selected). The four pre-pitch decisions, in evaluation
order:

1. **Substitution (pre-PA only)** — pinch-hitter / pinch-runner / defensive
   replacement / pitching change. Evaluated **at the top of a new plate appearance**
   (and at half-inning boundaries), *not* between pitches of a live PA. Triggers:
   third-time-through-order, pitch-count threshold, high-leverage platoon
   disadvantage, bullpen-by-leverage. *(Most substitution logic is SIM-323's manager
   module; this spec only fixes WHERE it sits: end-of-PA / start-of-PA, see §5.3.)*
2. **Intentional walk (IBB)** — a pre-PA decision that, if taken, **bypasses steps
   2–6 entirely**: the PA resolves directly to `intentional_walk`, runners are forced
   (step 7), state updates (step 8). No pitch is sampled.
3. **Pitch-out** — a per-pitch decision (anticipating a steal): it modifies the
   pitch that step 3 will represent (a deliberate ball, batter almost never swings)
   and pairs with a heightened catcher throw chance if a steal is in progress.
4. **Steal attempt** — a per-pitch decision for the lead runner(s). Computed from
   the baserunner-steal RBF, pitcher-allowing-SB RBF, catcher-throwing RBF, and the
   situation/leverage, scaled by the manager's green-light tendency (manager RBF).
   If a steal is initiated, its **outcome** (safe / caught / pickoff) is resolved as
   part of the contact-independent baserunning resolution (step 7) **using the
   `sim.stolen_base_pool`**, concurrent with the sampled pitch result.

**Where steal lives vs the README.** The README made steal **step 1**. This spec
demotes it to a **pre-pitch hook decision (initiate)** plus a **step-7 resolution
(outcome)**. This is cleaner because: the steal *decision* needs the situation
context (step 1's output) but precedes the pitch; the steal *outcome* shares the
pitch event (a steal resolves on the pitch, and its success interacts with whether
the batter put the ball in play). SIM-319 owns the steal decision+outcome
implementation against the `stolen_base_pool`; this spec fixes its placement.

> **Ordering within a pitch, end to end:**
> step 1 (read) → **pre-pitch hook** {substitution if new PA · IBB? · pitch-out? ·
> steal initiate?} → step 2 (select) → step 3 (sample) → step 4 (outcome) →
> [5 → 6] if in-play → 7 (baserunning incl. steal outcome) → 8 (commit + control).

---

## 4. Fingerprint derivation (replacing the stub hashes)

The scaffold builds both query vectors as **deterministic hashes of the count/outs**
(`_pitch_fingerprint`, `_battedball_fingerprint`). Phase 4 (SIM-317) replaces these
with **real feature vectors derived from game state**, in the exact feature order
the FAISS engines index, so the query vector lands in the same normalized space as
the tile vectors.

### 4.1 The 10-dim pitch fingerprint (SIM-041 pitch-to-pitch engine)

Order **must** match `similarity/engines/pitch_pitch_similarity.py::PITCH_FEATURES`
(and the `PitchVector` dataclass) exactly:

```
[ velo, ivb, hb, spin_rate, spin_axis,
  release_x, release_z, release_ext,
  plate_x, plate_z ]                              (10 dims)
```

**Derivation.** This is a *pitch the pitcher is likely to throw in this situation*,
not a hash. The fingerprint is assembled from the **pitcher's profile** (the
pitcher's arsenal centroid / expected pitch given count and batter hand: velo, ivb,
hb, spin_rate, spin_axis, release_x/z, extension) and the **intended location**
(plate_x, plate_z) implied by the count and matchup (e.g. ahead-in-count → expand
the zone; behind → attack it). The pitcher GMM engine supplies the arsenal geometry;
the situation KDTree + batter RBF tilt the expected pitch type/location for the live
count and batter. **SIM-321 owns how those engine scores fuse into the final
fingerprint** — this spec only fixes the **feature list, order, and the fact that it
is derived from game state**, not hashed. The vector is z-score normalized and
sqrt-weight scaled identically to the engine before it is passed to
`sample_pitch(query_vec=...)`. Because the matchup is fixed within a PA, the
expensive part of this derivation is cached per-PA (SIM-119 §5).

### 4.2 The 3-dim batted-ball fingerprint (SIM-042 batted-ball engine)

Order **must** match
`similarity/engines/batted_ball_similarity.py::BATTED_BALL_FEATURES`:

```
[ exit_velo, launch_angle, spray_angle ]          (3 dims)
```

(When SIM-051's `pull_relative_spray_angle` is materialized, the third feature
becomes pull-relative spray; the engine/loader fall-forward automatically and no
loop change is needed.)

**Derivation.** Built **only on `in_play`** (step 5). The contact-quality vector is
the *expected batted ball for this batter against this pitch*: the batter's
batted-ball profile (typical EV / LA / spray for the batter's hand and the pitch
type/location just sampled) supplies the centroid; the batter RBF and the just-
sampled pitch geometry tilt it. As with the pitch fingerprint, **feature list and
order are fixed here; the fusion of engine scores into the centroid is SIM-321.**
The vector is passed to `sample_batted_ball(query_vec=...)`, which draws the realized
event (single / double / … / out) from the launch-condition neighbourhood.

### 4.3 The pre-filter is NOT in the fingerprint

Both FAISS distances are **pure physics**: categorical matchup keys (`pitcher_id`,
`bat_hand`, `season`) are the **tile pre-filter** (play-pool spec §3), passed as
arguments to `sample_pitch` / `sample_batted_ball`, NOT dimensions of the query
vector. The loop must pass the **batter's hand for this PA** (the switch-hitter's
hand vs the current pitcher), not the roster-declared side.

> **Corrected 2026-07-27 (SIM-440).** The hand-for-this-PA lives in
> `raw.pitches.stand` (`'S'` on 0 rows), NOT in `raw.pitches.bat_hand`, which is
> the roster-declared side and is `'S'` for every switch hitter. The parameter is
> still spelled `bat_hand` in the sampler signature; its VALUE must come from
> `stand`. Canonical definition: `db/schemas/01_postgres_schema.sql`. Count is also NOT a fingerprint dimension — its
effect enters at step 4 via the SIM-056 foul re-weight, not the FAISS distance.

---

## 5. Terminal / PA logic

This is the count machine the scaffold lacks (its `pitch_outcome_to_event` maps
every non-contact outcome to a non-terminal `"in_progress"` marker — a stub). Step 4
owns this.

### 5.1 Count accumulation and terminal classification

On each sampled `pitch_outcome`, advance the count and test for PA-terminal:

```
ball:             count_balls += 1
                  if count_balls == 4:  PA terminates → walk

called_strike,
swinging_strike:  count_strikes += 1
                  if count_strikes == 3: PA terminates → strikeout

foul:             if count_strikes < 2: count_strikes += 1   # ordinary strike (≤2)
                  else:                  (no change)          # two-strike foul: PA STAYS ALIVE

in_play:          PA terminates → resolve via batted ball (steps 5–7)
```

- **Ball 4 → walk.** Terminal; runners forced (step 7); `PlayResult.event = "walk"`.
- **Strike 3 → strikeout.** Terminal; `event = "strikeout"` (drop-third-strike edge,
  §5.4).
- **Two-strike foul stays alive.** This is the SIM-056 **absorbing rule**: with
  `count_strikes == 2`, a `foul` does **not** increment strikes and does **not** end
  the PA — the loop draws the next pitch. This is non-negotiable for correct
  pitches-per-PA (and therefore correct pitch counts → starter fatigue → bullpen
  usage). See `docs/architecture/2026-05-21-foul-ball-weighting.md` §3.3.
- **In-play → batted-ball resolution.** Terminal *as a pitch outcome*; the PA's
  actual result is the event resolved by steps 5–7 (e.g. `single`, `field_out`,
  `home_run`, `ground_into_double_play`, `sacrifice_fly`).

### 5.2 SIM-056 foul re-weighting (step 4, before the count advance)

Before applying the count advance above, step 4 applies the **count-conditional foul
re-weight** from the foul-ball doc: multiply the sampled neighbourhood's `foul` mass
by `strikes_bucket_factor[count_strikes]` (≈1.55 at two strikes, 1.0 otherwise under
the illustrative table), renormalize the closed outcome vocabulary, and draw the
committed outcome. This lives in the loop (step 4), **not** in the sampler — the
sampler stays count-blind and distance-pure. See foul-ball doc §3.1–§3.2. The two
pieces (re-weight + absorbing rule) are both required and are independent.

### 5.3 End-of-PA hook (manager substitution)

When step 4 (or the batted-ball resolution) ends the PA, the loop:
1. emits the terminal `PlayResult`,
2. advances to the next batter in the batting order,
3. runs the **end-of-PA manager hook** (substitution evaluation, §3 item 1; owned by
   SIM-323), then
4. begins the next PA's pre-pitch hook (§3).

Substitutions are evaluated **here and at half-inning boundaries**, never mid-PA.

### 5.4 Edge cases the loop must honor (flagged, owner noted)

- **Dropped third strike** (uncaught strike 3 with 1B open or 2 outs → batter may
  reach) — resolved in step 6/7; owner SIM-319.
- **Foul tip caught with two strikes = strikeout**, and **foul bunt with two strikes
  = strike three** — these must be encoded in `outcome_type` **upstream by the ETL**
  (not inferred in the loop); foul-ball doc §3.3.
- **HBP** — a pitch outcome the current `outcome_type` vocab does not separate from
  `ball`; if HBP is not a distinct sampled outcome, it is out of the §5.1 machine and
  must be handled by SIM-312's event vocabulary reconciliation. **Flagged open.**

---

## 6. Half-inning, game, and loop control (step 8)

### 6.1 Half-inning (3 outs)

The loop accumulates outs across PAs within a half-inning. When `outs == 3`:
- clear the bases, reset the count, reset outs to 0,
- flip the half (top → bottom, or bottom → next inning's top),
- carry each side's batting-order pointer across half-innings (the lineup resumes
  where it left off),
- run the half-inning-boundary manager hook (§5.3).

### 6.2 Game (9 innings, extra innings, walk-off)

- **Regulation:** play through the top and bottom of 9 innings.
- **Walk-off:** if the **home** team leads at any point during the **bottom of the
  9th (or any later inning)**, the game ends immediately on the run that takes the
  lead — the half-inning does NOT complete. Symmetrically, the **bottom of the 9th
  is not played** if the home team already leads after the top of the 9th.
- **Extra innings:** if tied after 9, play additional full innings. Each extra half-
  inning **starts with the ghost runner on second base** (the automatic runner =
  the player who made the last out of the prior inning, or per the configured rule).
  Walk-off logic applies in every extra inning's bottom half.
- **Game over predicate (step 8):** `inning ≥ 9` AND the trailing team cannot tie or
  the home team leads after a completed/abandoned bottom half.

### 6.3 Loop control and determinism

- **Deterministic RNG seeding.** The loop is seeded once per game iteration so a
  `(game, seed)` pair is reproducible (README step 8; SIM-281/parallelism ADR). The
  sampler holds its own injected `numpy.random.Generator`; the loop must thread the
  per-iteration seed through both the sampler's rng and any loop-level draws (steal
  decisions, manager decisions, foul re-weight draw) so the whole game is
  reproducible from one seed.
- **Snapshotting for replay.** Step 8 snapshots enough state per pitch to support the
  Phase 5/6 replay endpoints (`/state/{at_bat}/{pitch}`); the snapshot contract is
  SIM-331, the per-iteration aggregation is SIM-327.
- **`simulate_game()` entry point** (SIM-320) drives this loop to completion and
  returns the per-game result; the 100-iteration parallel runner is SIM-332.

---

## 7. Where each engine plugs in (the 11 engines + registry)

Canonical engine names are exactly the registry's (`similarity/registry.py`,
`SimilarityEngineRegistry.list_engines()`). The loop never imports engines by module
path — it resolves them by canonical name through the registry.

| # | Canonical name | Family | Step(s) it serves |
|---|---|---|---|
| 1 | `pitcher` | gmm | Step 2 — arsenal geometry for the pitch fingerprint |
| 2 | `batter` | rbf | Steps 2, 5 — pitch-location tilt + batted-ball centroid |
| 3 | `fielder` | rbf | Step 6 — fielding resolution (out/hit/error) |
| 4 | `baserunner` | rbf | Step 7 — extra-base advancement |
| 5 | `baserunner_steal` | rbf | Pre-pitch hook + step 7 — steal decision/outcome |
| 6 | `catcher` | rbf | Pre-pitch hook + steps 6/7 — throw-out, framing/blocking |
| 7 | `pitcher_steal` | rbf | Pre-pitch hook — pitcher hold/pickoff (allowing SB) |
| 8 | `manager` | rbf | Pre-pitch + end-of-PA hooks — steal green-light, sub/IBB/bunt tendencies |
| 9 | `situation` | kdtree | Steps 2, 5 — situation-conditioned neighbourhood tilt |
| 10 | `pitch_to_pitch` | faiss | Step 3 — the pitch tile the sampler draws from (10-dim) |
| 11 | `batted_ball` | faiss | Step 5 — the batted-ball tile the sampler draws from (3-dim) |

**Score discipline.** RBF/GMM engines emit bounded similarity ∈ [0,1]; KDTree/FAISS
emit distance. The **sampler** (SIM-302) is the only place a FAISS distance becomes a
sampling weight. **Cross-engine fusion — how the pitcher/batter/situation similarity
scores combine to shape one per-pitch draw, and how catcher/fielder/manager scores
combine for the resolution steps — is SIM-321's job. This spec only states which
engines feed which step; it does not design the fusion.**

---

## 8. Run resolution (referenced — owned by SIM-312)

This spec does **not** define how an event becomes runs. The scaffold's
`runs = RUN_VALUES.get(event, 0.0)` is a known-buggy placeholder (SIM-312: the
`RUN_VALUES` keys do not match the pool's Statcast-raw `events` vocabulary, so common
outs silently score 0.0). **SIM-312 owns the run-resolution policy**: sampled
`result_hits/result_outs/result_runs` deltas + the RE24 run-expectancy matrix as the
primary mechanism, with the `simulation/constants.py` linear weights as a fallback.
The loop (step 7/8) calls into that policy to compute runs scored and the base/out
state delta; it must NOT re-derive run values inline. See SIM-312.

---

## 9. The GameState / PlayResult contract (referenced — owned by SIM-311)

This spec does **not** define the dataclasses. **SIM-311 owns the `GameState`
(mutable count/out/base/score/inning/half/lineup/manager context) and `PlayResult`
(pitch details, outcome type, batted-ball stats, fielding resolution, baserunner
movements, runs, outs, next state) contract.** Every step's I/O in §2 is expressed
against those types; when SIM-311 lands, the field names there are authoritative and
this spec's column-level descriptions defer to them. The scaffold's throwaway
`PitchState` (and its `simulate_pitch` return dict) are explicitly replaced by the
SIM-311 types.

---

## 10. Acceptance trace (SIM-310 requirements → where satisfied)

| SIM-310 requirement | Where |
|---|---|
| Canonical 8 steps, reconciling README vs time-budget (pick one, justify, note both) | §1 (both sources + decision), §2 (the 8 steps) |
| Manager pre-pitch decisions (steal / pitch-out / IBB / substitution) and their placement | §3 (pre-pitch hook) + §5.3 (end-of-PA sub) |
| Fingerprint derivation from game state (10-dim pitch SIM-041, 3-dim batted-ball SIM-042); feature lists per engines; replaces stub hashes | §4 (§4.1 / §4.2 feature lists in engine order; §4.3 pre-filter) |
| Terminal / PA logic (count, ball-4, strike-3, two-strike foul alive, in-play) | §5 (§5.1 machine, §5.2 foul re-weight ref) |
| Half-inning (3 outs), game (9 innings, extra, walk-off), loop control | §6 |
| Engine plug-in map (11 engines + registry); fusion deferred to SIM-321 | §7 |
| Run resolution deferred to SIM-312 | §8 |
| GameState / PlayResult contract deferred to SIM-311 | §9 |

---

## 11. Open items handed to downstream tickets

- **SIM-311** — author `GameState` / `PlayResult` dataclasses; this spec's §2 I/O is
  the requirements input.
- **SIM-312** — run-resolution policy (RE24 primary, linear-weight fallback) + fix
  the `RUN_VALUES`↔`events` vocabulary mismatch; **resolve where HBP enters the
  outcome vocabulary** (§5.4 flag).
- **SIM-316** — implement the §5/§6 count/out/base/inning state machine + invalid-
  state guards.
- **SIM-317** — implement §4 real fingerprint derivation via engines/registry,
  replacing the scaffold hashes.
- **SIM-318** — implement step 4 (outcome determination) + SIM-056 foul re-weight +
  two-strike-foul absorbing rule.
- **SIM-319** — implement steps 6/7 (fielding + baserunning) + the §3 steal
  decision/outcome against `sim.stolen_base_pool` + the §5.4 dropped-third-strike
  edge.
- **SIM-320** — implement step 8 half-inning/game/walk-off/extra-innings control +
  `simulate_game()`.
- **SIM-321** — cross-engine score fusion that this spec only references (§4, §7).
- **SIM-323** — the manager substitution/IBB/bunt module behind the §3 / §5.3 hooks.

*End of SIM-310 spec. Authoritative for loop ordering and terminal logic. Design
only — no production code.*
