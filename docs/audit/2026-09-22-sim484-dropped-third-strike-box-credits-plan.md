# Build plan — the box-score credits on the dropped-third-strike play (SIM-484)

> **STATUS 2026-09-23 — BUILT, REVIEWED AND CLOSED.** The owner's instruction
> "implement the SIM-484 tech design" took all four decisions (§10) as recommended. The
> build departs from this text in two places, both for a reason found while building: the
> batter's strikeout goes to a new batting field `so`, not to `k`; and the at-the-pitch
> snapshot applies after a steal only, not after a pickoff. §11 is the build record: the
> two departures, the review's corrections to this plan, and the adjacent defects it found.
> The readable page https://claude.ai/artifact/8x4G6hQ48CYh39q1HTLm4N shows the design as
> PROPOSED (2026-09-22); this file is the record.
>
> *(2026-09-22, as proposed: four small edits to the simulation loop, no data change, no
> rebuild, one app restart.)*
>
> **The play is modelled; its bookkeeping is wrong in three places, and the code reads a
> fourth.** On a swinging third strike that gets away with first base open or two outs, the
> batter reaches first and the loop already does that. But the play commits to the run
> ledger as a reach on an error, so the box credits the pitcher no strikeout; a run forced
> home pays the batter an RBI the rules withhold; and the "first base open" test reads the
> bases after a steal on the same pitch has moved them. On the way I found that the box
> never credits the batter's own strikeout on any strikeout, and that a runner who stole on
> a got-away third strike is moved twice. The official box, checked on every one of 2025's
> fifty such plays, credits the strikeout to both the pitcher and the batter.

**Date:** 2026-09-22
**Ticket:** SIM-484 (P2) in `BACKLOG.xlsx`. Next free ID: SIM-553.
**Evidence:** the loop as it stands at commit 06e20af (`simulation/sim_loop.py`,
`simulation/constants.py`, `simulation/run_resolution.py`, `simulation/game_state.py`); the
tests that pin today's behaviour (`tests/unit/test_sim421_runner_run_credit.py`,
`tests/unit/test_backend_sim319.py`); the acceptance lane's reach-on-error counter
(`tests/acceptance/conftest.py`); `raw.pitches` and the official box `raw.game_player_stats`
for 2023 to 2026.
**Builds on:** the prop-market review of 2026-09-12 that found the three items and the owner
decision that made them this ticket's scope (`CHANGES.md` 2026-09-12; the row text in
`docs/audit/2026-09-12-sim421-backlog-rows.md` §4); the catcher receiving profile (SIM-517),
which made the got-away fact the drawn pitch row's own.

---

## 0. The short version

**What the ticket asks for.** The box score's credits on a dropped third strike must match
official scoring: the pitcher gets the strikeout, the batter's line records it, a run forced
home by the reach pays no RBI, and the "first base open" test reads the bases as they were
at the pitch. The team score, the outs and the runs allowed must not change.

**What the code and the data say.**

- The play is real and rare. In the regular seasons 2023 to 2026, 69 to 99 strikeouts a
  season end on a wild pitch or a passed ball (about 0.2% of strikeouts), 49 to 69 of them
  with the batter reaching first, three to eight with a run scoring. Seven in ten are wild
  pitches. That is about one play in fifty games, which is why no band moves.
- The official box credits the strikeout. On all 50 of 2025's reaches, the pitcher's
  box strikeouts equal every strikeout plate appearance he recorded, and the batter's box
  strikeouts equal his. Official scoring (Rule 9.15) credits a strikeout when the batter
  becomes a runner on an uncaught third strike; Rule 9.04(b) credits no RBI for a run that
  scores on a wild pitch or a passed ball.
- The loop gets the bases and the score right and the labels wrong. The reach commits with
  the event `field_error`, so the box reads a reach on an error: no strikeout to the
  pitcher, an at-bat and no hit to the batter (right), the RBI paid because nothing marks
  the run as a got-away run (wrong), the run earned (right seven times in ten), and the
  lane's reach-on-error counter counts it (wrong: official scoring calls it a strikeout
  and a wild pitch, not an error).
- The batter's strikeout is never credited, on any strikeout. The box line has a `k`
  field; only the pitcher's side of the credit exists. The definition of done's "the
  batter's line records the strikeout too" is a new credit, not a repair, and it belongs
  on every strikeout.
- The "first base open" test reads the bases after the steal. On a terminal pitch the loop
  resolves a staged steal before the strikeout so a caught-stealing third out can end the
  inning first. The eligibility test then sees first base empty because the runner just
  stole second. The rule reads first base and the outs at the moment of the pitch.
- Found on the way: a runner who stole on a got-away third strike moves twice. The
  non-terminal path advances runners on a got-away pitch only when no steal or pickoff
  resolved that pitch (one mover per pitch); the terminal strikeout path has no such guard,
  so a runner who stole second is advanced again to third by the got-away.

**The plan.** Four edits in `simulation/sim_loop.py`: commit the reach as a strikeout (the
canonical event the play is), mark the forced run as a got-away run so no RBI is paid,
credit the batter's strikeout beside the pitcher's, and snapshot first base and the outs
before the steal resolves and hand them to the eligibility test. A fifth, one-line guard
closes the double move if the owner takes it. The pinned test flips to the correct
assertions and eight new tests cover the cases. No data change, no rebuild, no
calibration; the loop is bind-mounted, so one app restart.

**What it will show.** Nothing at the lane: about one play in fifty games. The strikeout
prop's actual and projection now agree on it, and the reach-on-error counter stops counting
a play that is not one.

**Four decisions** in §10, with my recommendation on each.

---

## 1. The mechanism

```
step_pitch (terminal pitch)                                        simulation/sim_loop.py
   the drawn pitch row carries got_away (a real passed ball / wild pitch on that pitch — SIM-517)
   ├─ _resolve_steal_outcome          ← a steal staged before the pitch resolves FIRST (a caught-stealing third out ends the inning)
   └─ _resolve_strikeout
        d3k = _dropped_third_strike:  swinging strike three AND (first base open OR two outs) AND got_away
                                      ⚠ reads state.bases / state.outs AFTER the steal moved them (item 3)
        not d3k and got_away → _resolve_got_away_advance (runners move up one base)   ⚠ no "one mover per pitch" guard (found on the way)
        d3k → _force_on_reach (batter to first, forced runners up, a forced run from third)
              _commit_run_delta(event="field_error", result_hits=1, result_outs=0, result_runs=forced_run, …)   ⚠ the label (items 1, 2)
   _end_of_pa → _accumulate_pa
        canonical "field_error": AB yes, no hit, pitcher K NO (item 1), RBI = runs − steal_runs_scored = 1 (item 2), ER charged, runs allowed charged
        the batter's own K: never credited, on any strikeout (found on the way)
```

The ledger's run value comes from the two base-out states, never from the label; the label
becomes `canonical_event`, which the box, the play-by-play and the lane's reach-on-error
counter read. That is why relabelling the commit fixes the box without touching a run
value, an out or a score.

---

## 2. The evidence

### 2.1 The play in real data (regular seasons, `raw.pitches`)

A strikeout whose play description names a wild pitch or a passed ball is the uncaught third
strike (the SIM-517 label: exact, no mid-plate-appearance leakage).

| Season | Strikeout PAs | Uncaught third strikes | on a wild pitch | on a passed ball | batter to first | a run scored |
|---|---|---|---|---|---|---|
| 2023 | 41,748 | 99 | 74 | 25 | 69 | 8 |
| 2024 | 41,126 | 69 | 52 | 17 | 49 | 5 |
| 2025 | 40,589 | 78 | 56 | 22 | 50 | 5 |
| 2026 (to date) | 33,977 | 50 | 32 | 18 | 32 | 3 |

About 0.2% of strikeouts; two thirds put the batter on first (the rest advance runners only,
first base occupied); about one play in fifty games. The ticket's "a few hundred per
season" was high by three.

**The official box on these plays (2025, `raw.game_player_stats`):** 50 reaches; on all 50
the pitcher's box strikeouts equal his strikeout plate appearances in that game (the
strikeout is credited), and on all 50 the batter's box strikeouts equal his. Zero cases of
the box excluding the play.

### 2.2 What the loop credits today, item by item

| Credit | Official scoring | The loop today | Where |
|---|---|---|---|
| Pitcher's strikeout | credited (Rule 9.15) | **not credited**: the commit's event is `field_error`, the box credits `k` only on canonical `strikeout` | `_resolve_strikeout` commit; `_accumulate_pa` |
| Batter's strikeout | charged | **never credited on any strikeout**: no `bat.k` line exists | `_accumulate_pa` |
| Batter's at-bat | charged | charged (`field_error` is not a non-AB event) | `_accumulate_pa` |
| Hit | none | none (`field_error` is not a hit) | `_accumulate_pa` |
| RBI on a run forced home | none (Rule 9.04(b): the run scores on the wild pitch / passed ball) | **paid**: `rbi = runs − steal_runs_scored`, and the reach sets no marker; `is_error` is false on this path | `_force_on_reach`, `_accumulate_pa` |
| Runner's run, team score | the run | right (fixed 2026-09-12) | `_force_on_reach` |
| Outs | none | none | the commit's `result_outs=0` |
| Runs allowed / earned run | run allowed; earned on a wild pitch, unearned on a passed ball | run allowed; earned always | `_accumulate_pa` |
| The lane's reach-on-error counter | not a reach on error | **counted**: the counter keys on a `field_error` commit with the batter reaching | `tests/acceptance/conftest.py` |
| Play-by-play | strikeout, batter to first | event `strikeout`, canonical `field_error` | `snapshots.py` |

The pinned test (`TestDroppedThirdStrikeForcesARunHome`) asserts `pit.k == 0` and
`bat.rbi == 1` with the note that this is the ticket's scope, and `pit.er == 1`,
`pit.r_allowed == 1`, `outs == 2`, the runner's `r == 1`.

### 2.3 The steal-order defect (item 3)

On a terminal pitch `step_pitch` resolves the staged steal, then the strikeout. The
eligibility test reads `state.bases.first is None` and `state.outs >= 2` at that moment.
Two cases go wrong:

- One out, a runner on first steals second on a swinging strike three that got away. At
  the pitch first base was occupied under two outs: the batter is out. The loop sees first
  base empty and lets him reach.
- One out, the same runner is caught stealing (the second out). At the pitch: not eligible,
  the batter is the third out. The loop sees two outs and first base empty and lets him
  reach.

With two outs at the pitch the batter is eligible either way; a caught stealing for the
third out ends the half-inning before the strikeout resolves, which the loop already handles.

### 2.4 Found on the way: the double move

On a non-terminal pitch the got-away advance runs only when `not result.steal_attempted and
not result.pickoff_out and not result.pickoff_error` (one mover per pitch; the pool rows mix
the channels). On a terminal strikeout that is not a dropped-third-strike reach, the same
advance runs with no guard: a runner who just stole second is moved again to third. Two
rare events on one pitch, but the guard is one line and the non-terminal path already
states the rule.

### 2.5 The model, as it stands

- The got-away fact is the drawn pitch row's own (a real passed ball or wild pitch on that
  pitch); no roll, no formula. The got-away rate is a graded pool band (`GOT_AWAY_PITCH`,
  PASS at +4.1% on the certified lane).
- The run ledger resolves every play from its two base-out states (RE24); the event label
  is provenance, not value.
- `PlayResult.steal_runs_scored` is the loop's "no RBI on this run" marker: the steal of
  home and the got-away advance both use it. The name is narrower than its use.
- The pitch pool's got-away flag carries no kind (wild pitch or passed ball), so the loop
  cannot tell an earned run from an unearned one on this play; it charges the run earned,
  right seven times in ten by the real split.

---

## 3. Findings that shape the plan

**Finding 1 — the label is the defect.** The run value, the outs, the bases and the score
are right; the commit's event is what the box and the lane read, and `field_error` is the
wrong word for a strikeout on which the batter reached. The play IS a strikeout: the
canonical vocabulary already has the word, the batter's reach lives in the base states the
ledger measured, and the box's strikeout credit keys on that word. No new vocabulary, no
flag.

**Finding 2 — the RBI marker exists.** The got-away advance and the steal of home withhold
the RBI through `steal_runs_scored`; the dropped-third-strike reach forgot to. One line.

**Finding 3 — the batter's strikeout is a missing credit, not a broken one.** It is missing
on every strikeout. The definition of done names it on this play; crediting it on every
strikeout is the same line and makes the box line whole (the official box carries it as
`k`; the API's `PlayerStatLineModel.k` already exists).

**Finding 4 — the eligibility test needs the bases at the pitch.** The loop already
snapshots bases in every resolver for the ledger; the same snapshot taken before the steal
resolves, plus the out count, is what the rule reads.

**Finding 5 — the ticket's DoD holds the score, the outs and the runs allowed fixed.** Every
edit here changes a credit or a predicate; none changes a run value, an out or a score, and
the tests assert that on every case.

---

## 4. Data changes

None. No table, no pool, no profile, no bundle, no calibration.

---

## 5. Code changes, file by file

| File | Change | What |
|---|---|---|
| `simulation/sim_loop.py` | modified | four edits: the commit's label, the RBI marker, the batter's strikeout credit, the at-pitch snapshot (and the one-line guard if decision 3 is taken) |
| `tests/unit/test_sim421_runner_run_credit.py` | modified | the pinned assertions flip; eight new tests (§7) |
| `tests/unit/test_backend_sim319.py` | modified | the three dropped-third-strike tests gain the box assertions |
| `docs/technical/sim-loop-cheat-sheet.md` | modified | one line under the strikeout step: the credits on the play |
| `BACKLOG.xlsx`, `CHANGES.md` | modified | the row deleted; the record |

### 5.1 The label and the RBI (items 1 and 2)

```python
# simulation/sim_loop.py — _resolve_strikeout, the dropped-third-strike branch
if d3k:
    result.event = EVENT_STRIKEOUT                       # unchanged: the play is a strikeout
    forced_run = self._force_on_reach(state, result)
    # SIM-484: the forced run scores on the wild pitch / passed ball, not on the
    # batter — no RBI (Rule 9.04(b)); the same marker the got-away advance and
    # the steal of home use.
    result.steal_runs_scored += forced_run
    self._commit_run_delta(
        state, result,
        event=EVENT_STRIKEOUT,        # was "field_error": the box credits the K on canonical "strikeout"
        result_hits=0,                # was 1: a reach is not a hit (the SIM-511 convention for every reach)
        result_outs=0, result_runs=forced_run,
        pre_outs=pre_outs, pre_bases=pre_bases,
        batter_reached=state.batter_id is not None,
        runners_scored=forced_run, runners_retired=0,
    )
    return
```

`resolve_runs` maps `"strikeout"` to canonical `strikeout` and takes its value from the two
states, so the run value is what it was. `_accumulate_pa` then credits the pitcher's `k`,
the batter's at-bat and no hit, `rbi = runs − steal_runs_scored = 0`, the runs allowed and
the earned run as before, and `_half_inning_error_outs_lost` stays untouched (it was
already: `is_error` is false on this path).

### 5.2 The batter's strikeout

> **Built differently (§11.1):** the credit is `bat.so += 1`, a new batting field. `k` is
> the pitcher's field, and three readers tell a pitcher by it.

```python
# simulation/sim_loop.py — _accumulate_pa, the batter block
if canonical == "strikeout":
    bat.k += 1            # SIM-484: the batter's own strikeout, on every strikeout (the box line's k; the official box's k)
```

### 5.3 The bases at the pitch (item 3)

> **Built differently (§11.1):** `step_pitch` passes the snapshot only when a STEAL
> resolved (`result.steal_attempted`). A pickoff is thrown before the pitch, so after one
> the live state is the state at the pitch; the unconditional snapshot below would retire
> batters the rule lets run.

```python
# simulation/sim_loop.py — step_pitch, the terminal branch
state.balls, state.strikes = adv.balls, adv.strikes
# SIM-484: the dropped-third-strike rule reads first base and the outs AT THE PITCH;
# the steal below moves them first (a caught-stealing third out must end the inning first).
first_open_at_pitch = state.bases.first is None
outs_at_pitch = int(state.outs)
self._resolve_steal_outcome(state, result)
if not state.is_half_inning_over():
    …
    elif adv.event == EVENT_STRIKEOUT:
        self._resolve_strikeout(state, result, first_open_at_pitch=first_open_at_pitch, outs_at_pitch=outs_at_pitch)

def _dropped_third_strike(self, state, result, *, first_open_at_pitch=None, outs_at_pitch=None) -> bool:
    if result.pitch_outcome != "swinging_strike":
        return False
    first_open = state.bases.first is None if first_open_at_pitch is None else first_open_at_pitch
    outs = int(state.outs) if outs_at_pitch is None else int(outs_at_pitch)
    if not (first_open or outs >= OUTS_PER_INNING - 1):
        return False
    return bool(self._last_pitch_got_away)
```

The defaults keep every existing caller (the unit harnesses call `_resolve_strikeout`
without a steal) byte-identical. When the steal was caught for the third out, the
half-inning ends before the strikeout resolves, as today.

### 5.4 The one-line guard (decision 3)

```python
# simulation/sim_loop.py — _resolve_strikeout
if (
    not d3k
    and self._last_pitch_got_away
    and not result.steal_attempted          # SIM-484: one mover per pitch, the non-terminal path's rule
    and not result.pickoff_out
    and not result.pickoff_error
):
    self._resolve_got_away_advance(state, result)
```

---

## 6. Weights, bandwidth, power

Nothing. No weight, no kernel, no power, no band centre. The got-away rate band is
untouched (the drawn fact does not change); the reach-on-error band's numerator loses the
dropped third strikes it should never have held (about 0.01 per team-game against a centre
of 0.21 and a floor of 8%).

---

## 7. Tests (`tests/unit/test_sim421_runner_run_credit.py` unless named)

The pinned test flips: `pit.k == 1`, `bat.k == 1`, `bat.rbi == 0`, and still `pit.er == 1`,
`pit.r_allowed == 1`, `state.outs == 2`, the runner's `r == 1`, the team score 1. New:

- **First base open, nobody forced:** the batter reaches; `pit.k == 1`, `bat.k == 1`,
  `bat.ab == 1`, `bat.h == 0`, no RBI, no run, no out; canonical event `strikeout`.
- **An ordinary strikeout:** `bat.k == 1` beside `pit.k == 1`; one out.
- **Steal order, one out, the runner steals second on a got-away strike three:** the batter
  is out (`pit.k == 1`, `bat.k == 1`, `state.outs == 2`), the runner stands on second (the
  steal stands), the batter is not on first.
- **Steal order, one out, caught stealing on the same pitch:** two outs from the steal,
  the batter is the third out, the half-inning rolls.
- **Steal order, two outs at the pitch, the runner steals second:** eligible; the batter
  reaches, the runner stays on second, no run.
- **The terminal got-away with a steal (decision 3):** one out, runner on first steals
  second, first base occupied at the pitch, the pitch got away: the batter is out, the
  runner ends on second, not third.
- **The reach-on-error counter** (`tests/acceptance` conftest, a unit-level check of the
  wrapper): a strikeout commit with the batter reaching does not count as a reach on error.
- **`test_backend_sim319.py`:** the three dropped-third-strike tests assert the box's `k`
  on both lines.

Every new test asserts the team score, the outs and `r_allowed` alongside the credit, per
the definition of done.

---

## 8. Run book

```
# 1. the code lands; ruff, mypy, the unit lane (the two test files above + tests/unit/test_sim517*); the regression lane is untouched (no engine change)
# 2. docker compose restart app    (simulation/ is bind-mounted; no rebuild, no data, no calibration, no matrices)
# 3. the ten-game smoke (scripts/sim_stats.py) — nothing to read at this rate; the run confirms the loop boots and the box serialises
# 4. close: CHANGES.md; delete the SIM-484 row; the cheat sheet's one line
```

No lane: at one play in fifty games no band can move, and the acceptance lane's
reach-on-error channel is the only reader that changes, by less than a tenth of its floor.

---

## 9. What could go wrong (ranked)

1. **A reader that assumed a strikeout always records an out.** `_accumulate_pa` reads the
   outs from `result.outs_recorded` (0 here) and the bases from the advances, so the box is
   safe; the play-by-play carries the reach in `baserunner_advances`. A search for
   `canonical == "strikeout"` finds the two credit lines and nothing that infers an out.
2. **The batter's strikeout credit reaches the API.** `PlayerStatLineModel.k` already
   serialises; a batter's line now shows a non-zero `k`. The box display reads the pitcher's
   `k` on pitcher lines; nothing in `frontend/src` reads a batter's `k` today, so the field
   is data with no consumer until the box page shows it.
3. **The snapshot's defaults hide a caller.** Every caller of `_resolve_strikeout` that
   does not pass the snapshot behaves as today; the terminal path in `step_pitch` is the
   only caller that resolves a steal first, and it passes both values.
4. **The passed-ball earned run stays wrong.** Seven in ten of these plays are wild pitches
   (earned); the loop charges all ten earned. Fixing the three needs a kind on the pitch
   pool's got-away flag, a pool rebuild for about two runs a season. Recorded, not built.
5. **The lane's reach-on-error counter drops by the plays it should not have counted.**
   About 0.01 per team-game against a centre of 0.208 and a floor of 0.017; the band cannot
   red on it, and the change is in the right direction.

---

## 10. Decisions for the owner

**All four TAKEN as recommended, 2026-09-23** (the owner's instruction "implement the
SIM-484 tech design"). Decision 2 is built on `so`, not `k` (§11.1).

1. **The label.** Recommended: commit the reach with the event `strikeout` (the canonical
   word the box credits on, the play the states describe) and `result_hits = 0`, as §5.1.
   The ticket's other option, a "strikeout on this play" flag on `PlayResult` read by
   `_accumulate_pa`, would leave the ledger's label a reach on an error and the lane's
   counter counting it. Alternative: a new canonical key `strikeout_reach`, which touches
   the vocabulary, the alias map, the box's sets, the play-by-play and the lane's counter
   for the same result.
2. **The batter's strikeout on every strikeout.** Recommended: yes, one line, the box line
   becomes whole and the definition of done's sentence is met on this play as a
   consequence. Alternative: credit it on the dropped third strike only, which leaves the
   batter's `k` at zero on the other 99.8% of strikeouts.
3. **The double move found on the way.** Recommended: close it in the same change with the
   non-terminal path's guard (§5.4), one line and one test. Alternative: file it as its own
   row.
4. **The earned run on a passed-ball dropped third strike.** Recommended: leave it earned
   and record the seven-in-ten split beside the code; the pool's got-away flag has no kind,
   and the fix is a pitch-pool column and a rebuild for about two runs a season.
   Alternative: add `got_away_kind` to the pitch pool in the next rebuild and split the
   charge then.

Recorded, not asked: no run value, out, score or run allowed changes on any case; the
`steal_runs_scored` marker keeps its name (three sites use it as "no RBI on this run"; a
rename is hygiene for the sim-loop decomposition, SIM-493); every similarity power is 1
and nothing here is a weight.

> **Corrected 2026-09-23 (§11.2, item 2):** that sentence holds for the relabel, the RBI
> marker and the batter's credit. The rule at the pitch and the one-mover guard change the
> play itself, on purpose, in the cases they cover.

---

## 11. Build record (2026-09-23)

### 11.1 What was built

All in `simulation/sim_loop.py` (plus one comment in `simulation/game_state.py`):

1. **The label.** The reach commits with the event `strikeout` and `result_hits = 0`
   (§5.1). The run value comes from the two base-out states, so it does not move.
2. **The RBI.** `result.steal_runs_scored += forced_run` on the reach (§5.1).
3. **The batter's strikeout, on every strikeout**, on a new batting field
   `PlayerStatLine.so`, NOT on `k` (a departure from §5.2). `k` is the pitcher's field:
   the line's own contract says a pure batter leaves the pitching fields at 0.
   `BoxScore.pitchers` and the prop builder (which feeds the /boxscore card and the prop
   routes) tell a pitcher by `k`, and `scripts/sim_stats.py` sums `k` for the game's
   strikeouts. The review emulated `bat.k`: 18 batters moved into the pitcher view and got
   the five pitcher props, and the smoke's strikeout count doubled (2,874 against 1,437).
   A two-way player's pitching K prop would also have absorbed his own batting strikeouts.
   `so` reaches no API response and no prop today (`PlayerStatLineModel` mirrors only the
   original batting and pitching fields). The box line records it, which is what the
   definition of done asks.
4. **The bases at the pitch.** `step_pitch` snapshots first base and the outs before the
   steal resolves, and hands them to the rule only when a STEAL resolved (a departure from
   §5.3). A pickoff is thrown before the pitch, so after a pickoff the live state is the
   state at the pitch. The unconditional snapshot would retire batters the rule lets run.
5. **One mover per pitch** on the terminal got-away advance (§5.4, decision 3), as designed.

The earned-run note of decision 4 sits beside the code. The called-third-strike gap
(§11.2, item 6) sits beside the predicate. The rule citations in the loop now read Rule
9.04(a) (§11.2, item 1), the older steal-of-home sites included.

**Tests.** `tests/unit/test_sim484_dropped_third_strike_credits.py` (new, 16 tests; the
plan named the run-credit file, and the cases grew past it); the pinned test in
`tests/unit/test_sim421_runner_run_credit.py` flipped; the three dropped-third-strike tests
in `tests/unit/test_backend_sim319.py` assert the credits; a real-machine test in
`tests/acceptance/test_band_arithmetic_sim450.py` drives the loop through the lane's probes
(the reach-on-error channel stays empty, the strikeout channels count the play).

**The review** (five lenses, a skeptic per finding, a completeness critic; 46 agents). The
rules lens ran 432 terminal strike-three cases (outs × bases × swinging or called × got
away or held × every steal and pickoff the draw can stage) against a rules oracle: the 78
cases the old code got wrong now match. The readers lens ran the old and the new loop game
by game with the same seeds (80 games): the score, the innings, the pitch count and every
line's outs, runs allowed, earned runs, hits allowed, walks, at-bats, hits, runs, steals
and caught stealing are identical; only the strikeout credits change. The mutation lens
ran 19 mutants. After four tests added from its report, every logical edit has a test that
fails without it; `result_hits` is an equivalent mutant (nothing reads it). The critic
checked the official box on all four seasons: the K is credited to both players on all 200
reaches of 2023–2026, and none of the 21 runs forced home pays an RBI.

### 11.2 Corrections to this plan

1. **The rule citation.** The no-RBI rule is 9.04(a): it pays an RBI only on a hit, a
   sacrifice, an infield out, a fielder's choice or a bases-full award. Rule 9.04(b) is the
   force double play. The conclusion (no RBI) stands.
2. **"No run value, out, score or run allowed changes on any case" (§10)** holds for items
   1–3 only. The rule at the pitch turns a reach into an out when a steal emptied first base
   (§2.3). The guard holds a runner the steal did not move: runners on first and third, the
   runner on first steals second on a got-away called third strike. The runner on third
   used to score; now he holds. Both are the designed behaviour.
3. **The lane's reach-on-error channel (§8, §9 item 5)** moves by about half its floor, not
   a tenth: about 0.009 per team-game against a floor of 0.0171, toward the centre. The
   certified lane (`scripts/sim523_lane_h.txt`) read the reached channel at 0.2150 (+3.5%)
   and the drawn channel at 0.2057 (−1.0%) against a centre of 0.2078. The 0.0093 gap
   between them was the dropped third strikes, which the build takes out of the reached
   channel.
4. **§9 item 2:** the batter's strikeout does not reach the API (§11.1, item 3).
5. **§2.1:** checked on all four seasons, not only 2025 (§11.1, the review).
6. **The swinging-only predicate.** Rule 5.05(a)(2) also lets the batter run on a CALLED
   third strike that is not caught. Two such plays sit in the window (both 2026, of 296
   uncaught third strikes in 2023–2026); the loop makes those batters out. Recorded beside
   the predicate, not built.

### 11.3 What the review found next to the change (not built here)

None of these is this ticket's; each predates it. Ranked by size. Item 1 is SIM-553 (built on
its own branch, 2026-09-25); items 2–5 are filed together as SIM-554 (P2, 2026-09-25).

1. **Foul tips.** The pitch-pool build codes a foul tip (`T`) and a foul bunt (`L`) as
   `foul` (`pipeline/batch/player_profile_computor.py`, the `outcome_type` CASE). At two
   strikes the count machine absorbs a foul, so a real strikeout on a two-strike foul tip
   becomes one more pitch. In 2025, 2,886 of about 40,600 strikeouts ended on a two-strike
   foul tip or foul bunt (7.1%). Chained through the count machine, the pool's coding gives
   0.2163 strikeouts per plate appearance against 0.2262 under official scoring: the loop
   loses about 4.4% of strikeouts. The lane's strikeout reference chains the same coding
   (0.2165), so no pool band can see it. The game-graded strikeout band read −2.5%, and
   the accuracy comparison's strikeout probabilities sit low (the sweep's plan). The
   Phase 4 loop spec required the ETL to code this case. About 35 times this ticket's size.
2. **The steal draw ignores the pitch.** The pre-pitch steal draw knows only the count;
   the pitch draw does not know a steal is staged. Real attempts never ride on a ball in
   play or on a two-out strike three. In the loop about 17% land on a ball in play and 18%
   on a foul (a 2025 estimate from the pool's rates). The steal volume stays on its bands
   (they count at the draw); where the attempts land changes outs, bases and runs. A
   two-out steal on a caught third strike is credited, although the strikeout ended the
   inning first (about 0.014 stolen bases per team-game).
3. **A two-out pickoff on a plate-appearance-ending pitch** uses up the batter's plate
   appearance and credits the drawn pitch's event (about one plate appearance in 190 games).
4. **A two-out caught stealing on a caught third strike** charges a caught stealing the
   rules do not (about one in 235 games).
5. **A dropped-third-strike reach freezes runners who are not forced.** Real runners left
   their base on 28 of 32 such chances (2023–2026), about seven plays a season.

The review also worried about the running app: a new slot on the stat line, workers on the
new code and a parent on the old. That does not apply to this stack. The app runs with hot
reload, so the server reloaded the new loop on every edit, and the run book's restart came
next.

### 11.4 The run book, as run

1. The code landed. Ruff, ruff format and mypy are clean. The targeted tests and the
   container unit, regression and band-arithmetic lanes pass: 4,422 tests, 4,400 passed and
   2 skipped, with 20 failures on files the container does not mount (`CLAUDE.md`,
   `WORKFLOW.md`, `deploy/`); a second run with those files mounted reports no failure.
2. `docker compose restart app`: healthy, `build_all_engines: 11/11`, calibration applied.
3. The ten-game smoke: the loop boots and the box records (10 games × 50: runs 9.34 a game for both teams; strikeouts −1.7% and walks +8.6% against MLB — no grade at this size). Beside it, a credit check on the production bundle (the same games × 20, `sim484_smoke_check.py` in the session scratch): 3,222 strikeouts, and the pitchers' `k` equals the batters' `so` equals the strikeout plate appearances in every one of the 200 games; 8 dropped-third-strike reaches, all committed as strikeouts; no batter in the pitcher view and none with a K prop.
4. Closed: `CHANGES.md`; the SIM-484 row deleted from `BACKLOG.xlsx`; the cheat sheet's
   step ⑧; the loop reference's `step_pitch` row.
