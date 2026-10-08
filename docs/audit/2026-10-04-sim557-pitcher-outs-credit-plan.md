# Build plan — every out goes on a pitcher's line: the caught stealing and the pickoff that do not end the plate appearance (SIM-557)

> **STATUS 2026-10-07 — BUILT, the gates and the smoke green; the build record is §12.**
> All three decisions were taken by the owner, as recommended (§10, §11). The merge, the app
> restart and the n=100 read wait. The
> readable page is https://claude.ai/artifact/CGxPvHoJH84yKdtoukBcjL; this file is the record. The evidence is
> three read-only probes on the production bundle (§2) and the official box score. Next free ID:
> SIM-560 as of 2026-10-07 (read it in `BACKLOG.xlsx`).

## 0. The short version

**What the ticket asks for.** Every out the defense records goes once on the line of the pitcher
who was on the mound when it happened: a strikeout, a ball in play, a caught stealing and a
pickoff, on a pitch that ends the plate appearance and on one that does not. A unit test pins
each path. A ten-game smoke shows the pitchers' outs equal the outs played.

**Terms.** A pitcher's **box line** is his row of the simulated box score: outs, strikeouts,
walks, runs. His outs are his innings pitched (three outs an inning) and the number the
**pitcher-outs prop** pays on (one of the fifteen prop markets). A pitch **ends the plate
appearance** when it is a walk, a strikeout, a hit by pitch or a ball in play. The **play
stream** is the ordered list of one game's results, one per pitch. The **win module** names the
winning, losing and save pitchers of a simulated game from the play stream.

**What the code and the data say.**

- **The simulator records an out in one place and credits it in two.** The record is exact. The
  credit runs only when a pitch ends the plate appearance, plus the one case the running-game
  change (SIM-554) added. A caught stealing or a pickoff out on any other pitch is recorded and
  never credited.
- **The size, on 1,800 production game-simulations: 575 of 95,813 outs, 0.32 a game, 0.6%.**
  271 are a caught stealing that is not the third out, 177 a caught stealing that is the third
  out, 127 a pickoff out before a pitch that does not end the plate appearance. Real games hold
  about 0.43 such outs a game.
- **Starters carry most of it.** A starter's box line is short 0.10 outs a start; 9.3% of
  starts miss at least one out. Starters end 63% of their outings on an inning boundary, so a
  missing out moves 15 to 14 and 18 to 17: the chance of "over 14.5 outs" reads 64.1% and
  should read 66.3%; "over 17.5" reads 31.0% and should read 32.9%.
- **No play changes.** The fix moves a number on a box line. Every seeded game plays the same
  pitches and ends with the same score.
- **The check in the ticket is not an identity.** "Three times the innings" fails on a
  walk-off, on a home side that does not bat last and on a game stopped at the 12-inning cap.
  The exact check is: the outs on the pitchers' lines equal the outs the play stream holds.
- **A second defect, found by reading every reader of the outs.** The win module counts a
  starter's outs from the play stream, and it credits the last play of each half-inning to the
  other side's pitcher. Its count is wrong for the winning starter in 55% of decided games. A
  starter needs 15 outs for the win, and in 4% of decided games the win goes to the wrong
  pitcher.
- **A third, small one.** The box score's pitcher list leaves out a reliever who retires
  nobody and allows no strikeout, walk or earned run: 25 relievers in 1,800 game-simulations.

**The plan.** Credit the out where the out is recorded, and nowhere else: one writer in place of
two, so no exit of a pitch can skip it and no out can count twice. The game result gains a count
of the outs played, a test holds the two counts equal on full games, and the ten-game smoke
fails when they differ. If you approve decisions 2 and 3: every play result names its own
pitcher and fielding side, the win module reads them, and the box line counts batters faced so
the pitcher list holds every pitcher who pitched. No data change, no weight, no flag. Thirty
tests. No lane: no play changes.

**Decisions** in §10: three, all taken on 2026-10-07.

## 1. The mechanism today

### 1.1 Where an out is recorded

One place. Every resolver (a ball in play, a strikeout, a caught stealing, a pickoff) calls the
run ledger `_commit_run_delta` (`simulation/sim_loop.py`). The ledger adds the out to the play
result (`result.outs_recorded`) and calls `_record_outs`, which adds it to the game state. No
other code changes the outs of the state.

### 1.2 Where an out is credited to a pitcher

Two places.

- `_accumulate_pa` adds `result.outs_recorded` to the line of the pitcher on the state. It runs
  from `_end_of_pa`, so it runs only when a pitch ends the plate appearance. On such a pitch the
  result also holds a caught stealing or a pickoff out of the same pitch, so those are credited.
- The no-pitch return of SIM-554: a pickoff that makes the third out before the pitch credits
  its own out, because no plate appearance ends there.

### 1.3 The five ways one step of the loop ends

| The step ends on | Outs it can hold | Credited today |
|---|---|---|
| an intentional walk | none | (nothing to credit) |
| a pickoff third out before the pitch (no pitch thrown) | 1 | yes, by SIM-554 |
| a pitch that does not end the plate appearance, and the half-inning ends on a caught stealing or a pickoff | 1 | **no** |
| a pitch that does not end the plate appearance, mid-count | 0 or 1 (a caught stealing, a pickoff out) | **no** |
| a pitch that ends the plate appearance | 0 to 3 | yes, by `_accumulate_pa` |

### 1.4 Who reads the pitcher's outs on the box line

| Reader | What it does with the outs |
|---|---|
| the pitcher-outs prop (`simulation/prop_distributions.py`, `OUTS`) | the probability of each number of outs, per pitcher |
| the box score card of the API (`api/schemas.py`) | innings pitched |
| `BoxScore.pitchers` and the prop builder's "did he pitch" test | a pitcher with outs is listed and owns pitcher props |
| the accuracy comparison and `validate_props` | grade the pitcher-outs market against the official box |
| the smoke (`scripts/sim_stats.py`) and the manager probe | rates per inning; a starter's depth |

### 1.5 Who reads the outs of the play stream

The linescore, the play-by-play and the win module read each play result. They see every out,
credited or not. The linescore knows that the play ending a half-inning carries the NEXT half's
state, and puts the play back in its own half. The win module does not.

### 1.6 The win module's count

`decisions_from_plays` (`simulation/pitcher_decisions.py`) gives each play's outs to the pitcher
named by the play's next state, when that state's fielding side is the winner. A play that ends
a half-inning has a next state in the other half, with the other side's pitcher. So the last
play of every half goes to the wrong side. A play result does not name its own pitcher, so the
module has nothing else to read.

The module's unit tests build each play by hand with a next state in the play's own half. The
real stream is not shaped that way, so the tests pass and the defect stays.

## 2. The evidence

### 2.1 The probes

Three probes, all read-only, on the production bundle with the production flags (the running
game on the pitch on, the manager draw on). Each builds the production machine for the 45 games
of the balanced certifying set and wraps three methods; it changes no play and consumes no
random number.

- **Probe 1, the credit** (45 games × 40 seeds = 1,800 game-simulations, 17 minutes in two
  containers). It logs each call of `_record_outs` with the pitcher on the mound, and after
  each game compares each pitcher's recorded outs with his box line.
- **Probe 2, the win module's count** (45 × 12 = 540). It replays the module's rule beside the
  truth for the winning starter.
- **Probe 3, the win itself** (45 × 20 = 900). It runs the module beside two patched copies of
  its own source: A corrects only the out count; B also reads the pitcher of record from the
  play's own pitcher.

One game of the 45 (823372) resolves with no away starter, so the home starter pitches for both
sides in it (§2.7). Probe 1's starter numbers leave it out (44 games, 3,520 starts). Its
reliever counts and probes 2 and 3 include it; it is one game in 45.

### 2.2 The size

| | Count | A game | Share of outs |
|---|---|---|---|
| Outs played | 95,813 | 53.23 | 100% |
| Credited on a pitch that ends the plate appearance | 95,147 | 52.86 | 99.3% |
| Credited as a pickoff third out before the pitch (SIM-554) | 91 | 0.05 | 0.1% |
| **Not credited** | **575** | **0.32** | **0.60%** |
| a caught stealing, not the third out | 271 | 0.151 | |
| a caught stealing, the third out | 177 | 0.098 | |
| a pickoff out before a pitch that does not end the plate appearance | 127 | 0.071 | |

For every pitcher of every game, his box line plus his uncredited outs equals his recorded outs
(0 exceptions). The official box holds 53.2 outs a game (2023–2026), so the simulator plays the
right number of outs; it loses 0.6% of them on the way to the box.

The backlog row's figure (0.65 a game) came from the synthetic test bundle, whose steal rates
are far above real play.

### 2.3 Starters and the pitcher-outs prop

3,520 starts (44 games). The box line holds 15.06 outs a start; the starter recorded 15.16. The
gap is 0.10 outs. 328 starts (9.3%) miss at least one out; 19 miss two or more.

| Line | P(over) on the box line today | P(over) with every out credited | Change |
|---|---|---|---|
| 13.5 | 72.0% | 72.8% | +0.8 |
| 14.5 | 64.1% | 66.3% | **+2.2** |
| 15.5 | 44.2% | 44.7% | +0.5 |
| 16.5 | 37.9% | 38.6% | +0.6 |
| 17.5 | 31.0% | 32.9% | **+2.0** |
| 18.5 | 15.6% | 15.9% | +0.2 |

The change is largest just under 15 and 18 outs. 63% of starts end on a multiple of three outs,
because the manager changes pitchers between innings. One missing out moves such a start under
the line. At each starter's own line (the half line just under his median) the chance of the
over rises 2.0 points on average, and 5 points or more for 19 of 88 starters (40 seeds each, so
one starter's figure is coarse).

### 2.4 Relievers and the pitcher list

12,264 relief appearances (6.8 a game; the official box holds 6.6). 227 have an uncredited out.
30 relievers are absent from `BoxScore.pitchers` today. Five of them recorded only uncredited
outs: the ticket's case. The other 25 retired nobody and allowed no strikeout, walk or earned
run (a hit, a hit by pitch, an unearned run); the list's test does not see them. The prop
builder's wider test misses 8, and 4 after the credit.

### 2.5 Real play

Read-only, `raw.game_player_stats` and `raw.play_events`, 2023–2026.

| | Real |
|---|---|
| Pitcher outs a game | 53.2 |
| Caught stealings a game (the official box; a pickoff of a runner who broke counts) | 0.39 |
| Pickoff outs a game | 0.15, of which about 0.09 are not a caught stealing |
| Strikeout-and-caught-stealing double plays (they end the plate appearance) | 0.04 a game |
| **Outs on the bases on a pitch that does not end the plate appearance** | **about 0.43 a game** |
| A starter's outs a start | 15.3 to 15.6 by season |
| Pitchers with an out and no batter faced (a reliever's pickoff or caught stealing) | 16 in four seasons |
| Pitchers with no out, strikeout, walk or earned run | 102 of 80,834 appearances, 1 in 93 games |

The 0.43 is built from three counts; it is an estimate, not a single query. The simulator's 0.32
is lower because it attempts fewer steals than real play (the red steal bands of the last lane;
the balance weight of the steal draw, SIM-556).

### 2.6 The win module against the truth

**Probe 2** (538 decided games). The module's count of the winning starter's outs, minus the
truth: −3 in 1 game, −2 in 6, −1 in 51, **0 in 240**, +1 in 199, +2 in 38, +3 in 3. It differs
in 298 games (55%). In 34 games (6.3%) the two counts fall on different sides of 15 outs.

The mechanism, read from the code. For a starter who pitches whole innings the error cancels:
he loses the last play of his own half and gains the last play of the other half. It does not
cancel when a half ends on a double play (two outs on one play), and it adds one for an away
starter who leaves between innings: the last play of the next top half still names him.

**Probe 3** (897 decided games of 900). With the count corrected, the win goes to another
pitcher in 33 games (3.7%). With the pitcher of record also read from the play's own pitcher, in
36 (4.0%); the save changes in 4; the loss in none.

### 2.7 Found on the way, outside this ticket

- **One certifying game has no away starter.** Game 823372 (2026-06-10) resolves with
  `away_pitcher_id = None`, and the home starter pitches for both sides in every simulation of
  it. The official box names the away starter; he is a two-way player who also batted that day.
  Flagged as a separate task on 2026-10-04. Another session found the cause (the lineup table stores each player's LAST position, so a two-way starter who later moved in the field carried no pitcher code), filed it as SIM-558 and closed it on 2026-10-07: the resolver now reads the official box's starter, and the certifying lane on the fixed code moved no band.
- **Ties at the 12-inning cap.** 3 of 900 game-simulations (6 of 1,800 in probe 1) end tied:
  the production kwargs stop a game after 12 innings, one pitch into the 13th. Recorded, not
  examined.
- **The simulator's starters are short of real ones beyond this ticket.** 15.2 outs a start
  against 15.3 to 15.6; "over 15.5" at 44.7% against a real 50.8%. The credit closes 0.10 outs
  of that. The rest belongs to the manager draw and the run level, not to the box.

## 3. Findings that shape the plan

1. **One recorder, two writers, two uncovered exits.** The out is recorded in one function. The
   credit sits at the end of a plate appearance, and two of the five exits of a step never
   reach it.
2. **The loss is small and it sits on one market.** 0.6% of outs, but the pitcher-outs prop is
   a count with its mass on multiples of three, so 2 points of probability move at the common
   lines.
3. **No play changes.** The fix writes a box number. It draws nothing and moves no runner. The
   sixteen pool bands read rates per opportunity; none reads a pitcher's outs.
4. **Crediting at the recorder is exact.** Every out passes through `_record_outs` once, with
   the fielding side's pitcher on the state. A credit there covers every exit, today's and any
   later one, and it cannot double count once `_accumulate_pa` stops writing.
5. **The ticket's warning stays true and does not apply.** The row says: do not credit inside
   the pickoff or the steal resolver, because `_accumulate_pa` counts a pickoff before a pitch
   that ends the plate appearance. That is a warning against a second writer. Moving the one
   writer to the recorder removes the double count at its root.
6. **The ticket's patch works and leaves four writers.** Crediting on the two uncovered exits
   is exact today. It adds two writers to the two that exist, and the next new exit is the next
   missing credit. SIM-554 added an exit on 2026-10-01 and had to remember the credit.
7. **"Three times the innings" is not an identity.** In probe 1 the away pitchers' outs fit
   three shapes (the home side did not bat last: 837 games; a full last half: 794; a walk-off:
   169), and 6 games stop at the inning cap. The exact identity is: the outs on the pitchers'
   lines equal the outs the play stream holds.
8. **A play result does not name its pitcher.** Every reader of the play stream infers the
   pitcher from the next state. That is wrong on the play that ends a half-inning and on a
   reliever whose first play ends the half. The win module's count is the visible damage.
9. **The pitcher list reads four fields, not "he pitched".** A batters-faced count on the line
   is the direct test; the official box carries the same field.

## 4. Data changes

None. No migration, no pool rebuild, no bundle export.

## 5. Code changes, file by file

### 5.1 The one writer — `simulation/sim_loop.py`, `_record_outs`

```python
def _record_outs(self, state: GameState, n: int) -> None:
    """Record ``n`` outs and credit them to the pitcher on the mound (SIM-557)."""
    if n < 0:
        raise ValueError("cannot record a negative number of outs.")
    state.record_out(n)
    state.assert_outs_valid(in_play=False)
    # SIM-557: the ONE writer of a pitcher's outs. Every out the defense records passes here
    # once, before the half-inning rolls, so ``state.pitcher_id`` is the pitcher who was on the
    # mound. A caught stealing or a pickoff on a pitch that does not end the plate appearance
    # is credited like any other out.
    if n and state.pitcher_id is not None:
        self._box_line(int(state.pitcher_id)).outs_recorded += int(n)
```

### 5.2 The two old writers go

- `_accumulate_pa`: delete `pit.outs_recorded += outs`. The local `outs` stays; the earned-run
  rule reads it. The docstring's "IP" line says where the outs are written now.
- The no-pitch return of `step_pitch`: delete the credit block SIM-554 added. The pickoff's out
  was credited when the ledger recorded it.

### 5.3 The outs played — `GameSimResult` and `simulate_game`

`GameSimResult.outs_played: int = 0`. The driver adds each step's `outs_recorded`. It is the
play stream's count, taken beside the pitch count the driver keeps already.

### 5.4 The play names its pitcher (decision 2)

- `PlayResult.pitcher_id: int | None = None` and `PlayResult.fielding_team: Team | None = None`:
  the pitcher on the mound for this result and his side. `step_pitch` sets both when it makes
  the result, after the manager's hooks (so a reliever who enters on this pitch is named). The
  intentional-walk result sets them too.
- `decisions_from_plays`: when a play carries the two fields, the module reads the fielding
  side and the pitcher from the play, for the out count and for the pitcher of record. A play
  without them reads as today, so a hand-built stream keeps its meaning and the module's
  existing tests stand.

```python
for r, st in paired:
    own = r.fielding_team is not None
    defending = r.fielding_team if own else _defending_team(st)
    pid = r.pitcher_id if own else st.pitcher_id
    if defending == Team.HOME:
        home_poR = pid
    else:
        away_poR = pid
    ...
    if defending == winner and pid is not None:
        ...the count, on pid
```

### 5.5 Batters faced and the pitcher list (decision 3)

- `PlayerStatLine.bf: int = 0`, the batters this pitcher faced. `_accumulate_pa` adds one for
  the pitcher on every completed plate appearance, the intentional walk included. A no-pitch
  result never reaches it.
- `BoxScore.pitchers`: a line with outs or a batter faced (the four old fields stay in the
  test for lines built by hand).
- `PropDistributionSet.from_boxscores`: the "did he pitch" test gains the same field.
- The API's line model is unchanged: `bf` is not served.

### 5.6 The smoke — `scripts/sim_stats.py`

Each game summary gains the pitchers' outs and `outs_played`. The report prints both per game
and the number of game-simulations where they differ. A difference makes the script exit 1: the
two counts are an identity, so this line is a gate, unlike the script's other reads. The JSON
record carries both.

### 5.7 Documentation

The cheat sheet (the credits list of the last step: who writes a pitcher's outs; the win
module's read); `docs/technical/simulation.md` (`_record_outs`, `GameSimResult`, `PlayResult`,
`decisions_from_plays`, `BoxScore.pitchers`); `CLAUDE.md` (the sentence of the SIM-554 bullet on
the no-pitch credit); `CHANGES.md`; the row deleted from `BACKLOG.xlsx`.

## 6. Weights, bandwidth, power, flags

None. The change corrects an account; there is no arm to compare and no value to fit, so it
carries no flag.

## 7. Tests

`tests/unit/test_sim557_pitcher_outs_credit.py`, on the harness of the SIM-484 and SIM-554
suites (a synthetic bundle, an injected pitch, a staged steal or pickoff).

**The paths.**
1. A strikeout: one out on the pitcher's line.
2. A double play on a ball in play: two.
3. A caught stealing on a ball, not the third out: one; the count goes on; a later strikeout of
   the same batter makes two.
4. A caught stealing on a ball for the third out: one, on the pitcher who was on the mound; the
   next half's pitcher has none.
5. A pickoff out before a pitch that does not end the plate appearance: one.
6. A pickoff out before a pitch that is strike three: two, not three.
7. A caught stealing on strike three with nobody out: two, each once.
8. The pickoff third out before the pitch: one (now through the recorder).
9. The single pre-pitch draw (the flag of the running game off): a pickoff out on a ball: one.
10. A caught stealing for the third out on ball four: the walk is credited, one out.
11. After a pitching change the out goes to the reliever, none to the starter.
12. No pitcher on the state: no line is made, no error.
13. `_accumulate_pa` called alone adds no out to the pitcher.
14. One writer: the source of `sim_loop.py` holds one write of a box line's `outs_recorded`,
    inside `_record_outs`.

**The list.**
15. A reliever whose only out is a pickoff is in `BoxScore.pitchers`.
16. He owns the pitcher props; his outs sample is 1.
17. (Decision 3) A reliever who faces one batter and allows a single: one batter faced, listed,
    no outs.
18. (Decision 3) An intentional walk is a batter faced; a no-pitch result is not.

**The identity.**
19. Twenty-five synthetic games with steals and pickoffs: in each, the outs on the pitchers'
    lines equal `outs_played`.
20. In each, the home side's outs are three times the innings, and the away side's fit one of
    the three shapes of finding 7.
21. `outs_played` equals the sum of the recorded stream's `outs_recorded`.

**The play's own pitcher (decision 2).**
22. Every result of a synthetic game names its pitcher and its fielding side; the no-pitch
    result and the intentional walk too.
23. The play that ends a half names the pitcher who recorded the out.
24. A starter whose innings end on double plays: the module's count is his box line's; at 15
    outs he keeps the win.
25. An away starter who leaves after 4⅔ innings: 14 outs, and the win goes to a reliever.
26. A reliever whose first pitch ends the half, and whose side then takes the lead for good,
    is the winning pitcher.
27. A hand-built stream without the two fields reads as today.
28. On recorded synthetic games: for every pitcher of the winning side, the module's count
    equals his box line.

**The smoke.**
29. Equal counts: the check line reads zero differences and the exit code is 0.
30. A box with one out removed: the line names the game-simulation and the exit code is 1.

Thirty tests: 21 for the ticket, 7 for decision 2, 2 for decision 3. The existing suites stay
green as written; none asserts a pitcher's outs after a direct call of `_accumulate_pa` (25
direct calls read).

## 8. Run book

1. **The "before" smoke.** On the code before the change: `scripts/sim_stats.py`, ten games ×
   50, with `--json-out`. Keep the record.
2. **Code and gates.** The changes of §5 on a branch. Ruff, ruff format, mypy over the CI scope;
   the unit lane (the host and the app image); the regression lane; the band-arithmetic lane.
3. **The "after" smoke**, the same ten games and seeds. Stop if any of these fails:
   - the new line reads zero game-simulations where the pitchers' outs differ from the outs
     played;
   - every batting and scoring count equals the "before" record (runs, hits, home runs, walks,
     strikeouts, steals): no play changed;
   - the pitchers' outs a game rise by about 0.3.
4. **The app.** `simulation/` is mounted: restart the app. Run one n=100 `/simulate` and read
   that the box's pitcher outs sum to the outs played.
5. **Close.** The changelog; the row deleted from the backlog; the docs of §5.7; this plan's
   build record; commit on the branch; merge.

No lane and no accuracy run: no play changes (finding 3), and the ruling of 2026-09-16 asks for
the cheap gates only.

## 9. What could go wrong (ranked)

1. **A reader took "has a pitcher line" to mean "finished a batter".** The recorder makes a
   pitcher's line at his first out, which can now come before his first completed batter. No
   such reader was found; the unit lane decides.
2. **A later change adds a second writer.** Test 14 fails on it, the identity test fails on a
   full game, and the smoke of every later change exits 1.
3. **The stored accuracy baselines move on one market.** The pitcher-outs rows read about 0.1
   outs a start higher. No calibration map is fitted on that market. Every other row is the
   same to the digit.
4. **The win changes on 4% of replayed games** (decision 2). That is the fix. A reader who
   compares an old replay card with a new one sees another winning pitcher.
5. **A test that calls the ledger by hand with a pitcher on the state** now makes a box line.
   Three test files call it (10 calls); all build a full machine.
6. **The inning-cap games.** A game stopped at the cap has one pitch in the 13th; the identity
   of test 20 leaves such a game out by its tie. Not changed here.

## 10. Decisions for the owner

1. **Where the credit is written.** *TAKEN 2026-10-07 by the owner ("go with your
   recommendations for all the open items"), as recommended:* in `_record_outs`, the one place an out is
   recorded, and the two existing writers are removed. One writer; every exit of a pitch is
   covered, a later one included; no out can count twice. *Alternative (the backlog row's
   proposal):* keep the two writers and add a credit on each of the two exits that skip the end
   of the plate appearance. It is exact today and smaller to review; it leaves four writers,
   and a new exit must remember the credit, as SIM-554 had to.
2. **The win module's count.** *TAKEN 2026-10-07 by the owner, as recommended:* fix it in
   this ticket. Every play result names
   its pitcher and fielding side, and the module reads them for the out count and for the
   pitcher of record. The win goes to another pitcher in 4% of decided games. *Alternatives:*
   file it as its own ticket and keep this ticket to the box line; or correct only
   the out count and leave the pitcher of record on the next state (3.7% of games in place of
   4.0%).
3. **The pitcher list.** *TAKEN 2026-10-07 by the owner, as recommended:* add batters faced
   to the box line and list every
   pitcher with an out or a batter faced. It adds the 25 relievers in 1,800 game-simulations
   that the credit alone leaves out. *Alternatives:* leave the list as it is (the ticket's own
   case, the reliever whose only out is a pickoff, is listed by the credit alone); or widen the
   test to hits and runs allowed with no new field, which still misses a reliever whose one
   batter was hit by a pitch.

**Recorded, not asked.** No flag and no lane (§6, §8). The smoke's new line is a gate with an
exit code, because it checks an identity. The definition of done's "three times the innings"
becomes "the pitchers' outs equal the outs played" (finding 7). The API's line model does not
gain the batters-faced field. A reliever's props still count a game he did not enter as zero;
not this ticket. The game with no away starter is flagged as its own task (§2.7). The ties at
the 12-inning cap and the level of the simulator's starters against real ones are recorded and
not changed.

## 11. The decisions taken (2026-10-07)

The owner, 2026-10-07: "Go with your recommendations for all the open items." All three
decisions of §10 are taken as recommended. The build is the whole of §5:

1. the credit in `_record_outs`, and the two old writers removed (§5.1, §5.2);
2. `GameSimResult.outs_played`, the identity tests and the smoke's gate (§5.3, §5.6);
3. `PlayResult.pitcher_id` and `PlayResult.fielding_team`, read by the win module (§5.4);
4. `PlayerStatLine.bf` and the pitcher list by outs or batters faced (§5.5);
5. the docs (§5.7).

Thirty tests (§7); the run book of §8. Nothing is built by this record.

## 12. Build record (2026-10-07)

**What was built.** The whole of §5, on branch `sim557-pitcher-outs-credit` (from master
a51ba87), uncommitted when this record was written.

1. `_record_outs` is the one writer of a pitcher's outs (§5.1). It credits each out to
   `state.pitcher_id` before the half-inning rolls.
2. The two old writers are gone (§5.2): the write in `_accumulate_pa`, and the credit block
   in the no-pitch return of `step_pitch`. The comment there says the ledger credited the out.
3. `GameSimResult.outs_played` (§5.3): `simulate_game` adds each step's `outs_recorded`.
4. `PlayResult.pitcher_id` and `PlayResult.fielding_team` (§5.4): `step_pitch` sets them after
   the manager's hooks; the intentional walk sets them too. `decisions_from_plays` reads them
   for the pitcher of record and for the starter's out count, through two helpers
   (`_play_side_and_pitcher`, `_side_pitcher_outs`).
5. `PlayerStatLine.bf` (§5.5): one batter faced on every completed plate appearance.
   `BoxScore.pitchers` and the prop builder list a pitcher with an out or a batter faced.
6. The smoke's gate (§5.6): `scripts/sim_stats.py` carries `p_outs` and `outs_played` on each
   game summary; `outs_credit_check` counts the game-sims where they differ; `main` exits 1
   on a difference and names up to five such game-sims by game and seed.
7. The docs (§5.7): the cheat sheet, `docs/technical/simulation.md`, `CLAUDE.md` (the
   running-game clause and a new bullet), `CHANGES.md`; the row deleted from `BACKLOG.xlsx`.
8. Tests: 78 collected in four new files (`test_sim557_pitcher_outs_credit.py` 20,
   `test_sim557_outs_identity.py` 18, `test_sim557_play_pitcher_decisions.py` 24,
   `test_sim557_smoke_gate.py` 16). One existing test changed: `test_sim474_steal_draw.py`
   asserted that a pickoff out made no box line; it now asserts the pitcher's line with one
   out.

**The gates.**

| Gate | Result |
|---|---|
| Unit lane, the app image (Starlette 0.41.3, the worktree mounted) | 5,502 passed, 2 skipped, 0 failed |
| The four new files and the steal-draw suite, host Python 3.13 | 116 passed, 0 failed |
| Regression lane | 33 passed |
| Band arithmetic | 58 passed |
| `ruff check` / `ruff format --check` | clean / 443 files formatted |
| `mypy similarity/ pipeline/ api/` | no issues in 62 files |

The full unit lane does not run on the host: the host has Starlette 1.6.0 against the pin
`starlette>=0.41,<0.42`, and 109 tests fail on import of two files this build does not touch.
One stress test fails on the host when 30 Windows spawn workers fail to import numpy. All of
them pass in the image.

**The review.** Eight findings, all confirmed and fixed.

- A batter faced was credited on a plate appearance that a third out on the bases cut short.
  It happens only in the single pre-pitch steal draw, which production does not run. The
  batter's at-bat and the loop's own count of batters faced share the flaw, so the fix of
  all three needs its own ticket; the code says so at the write. Not filed.
- Four findings on the win module's starter fallback (below). The fallback first changed the
  result of an old stream without the two fields. It now runs only on a stream whose plays
  name their sides.
- Three findings on the tests and the gate: test 28 did not call the win module; no test
  isolated the prop builder's batters-faced clause; no test checked the two keys
  `_game_summary` writes; the gate's failure named no game-sim (test 30 and §5.6 ask for
  that). Each has its test now, and the gate names the game-sims.

**The smoke** (ten games × 50, seeds 0–49; both runs mounted the worktree's `scripts/` and
`pipeline/`, which has no diff against master).

| metric | before (master a51ba87) | after (the worktree) |
|---|---|---|
| batting and scoring values compared (500 game-sims × 19 keys) | 9,500 | 9,500, all equal |
| aggregate means compared | 21 | 21, all equal |
| R a team-game | 4.58 | 4.58 |
| pitcher outs credited a game-sim | 52.94 | 53.25 |
| outs played a game-sim | not available (the simulator predates the outs count) | 53.25 |
| game-sims where credited and played outs differ | 125 of 500 (against the after run's played outs) | 0 |
| outs gate (exit code) | not available, exit 0 | 0 mismatches, exit 0 |
| elapsed | 520.6 s | 515.6 s |

The credited outs rose 0.304 a game-sim (+0.10 to +0.44 by game), against the 0.32 of §2.2
on the balanced set. The run book's three stop conditions (§8 step 3) all hold. No play
changed.

**What the plan got wrong.**

- **§5.4 missed a case the old defect hid.** A side has no pitcher of record until its
  pitcher fields a play. Before the fix, the play that ends the top of the 1st was credited
  to the away side, so the away starter was on record from that play on. With the play's own
  side read, he is on record only from the bottom of the 1st. So an away side that leads for
  good from the top of the 1st had no winning pitcher. The build adds a fallback: a side
  with no pitcher of record at the decisive play has its first pitcher (its starter) on
  record. The fallback runs only on a stream whose plays name their sides, so a stream
  without the two fields reads exactly as today, as §5.4 asks.
- **§5.5 assumed every completed plate appearance is a batter faced.** In the single pre-pitch
  draw a third out on the bases can come before the pitch's result, and the loop still ends
  the plate appearance. That draw is not production; see the review above.
- **§5.6 said the report prints both counts per game.** The build prints one line with the
  means and the count of differing game-sims, and on a failure it names up to five of them
  by game and seed. The JSON record carries the numbers.
- **§7 counted thirty tests.** The build has 78 collected tests (some parametrized), with the
  review's additions.

**Not done:** the merge, the app restart and the n=100 read (§8 step 4).

## Appendix — the probes' reports

```
probe 1 (1,800 game-sims; flags: steal_pitch_class on, steal order active, manager draw on)
outs played (by step): 95,813  (53.23 a game)
uncredited outs by kind:
     271  0.151 a game   caught stealing | not the third out
     177  0.098 a game   caught stealing | third out
     127  0.071 a game   pickoff out | not the third out
credited outs by path:
   95147  52.859 a game   a pitch that ends the plate appearance
      91  0.051 a game   a pickoff third out before the pitch (SIM-554)
probe sanity (box + uncredited != recorded): 0 pitcher-games
home pitchers: recorded outs != 3 x innings in 6 of 1800 game-sims
away pitchers:   837  3 x (innings - 1): the home side did not bat last
away pitchers:   794  3 x innings
away pitchers:   169  walk-off: 3 x (innings - 1) + the outs at the winning run
starters (44 games, 3,520 starts): box 15.063, recorded 15.162, gap 0.099
  starts with at least one uncredited out: 328 (9.3%); with two or more: 19
  share of starts ending on a multiple of 3 outs: 63.2%
relievers (45 games): 12,264 appearances; 227 with an uncredited out;
  30 absent from BoxScore.pitchers (5 listed once credited);
  8 absent from the prop builder's test (4 present once credited)

probe 2 (540 game-sims, 538 decided)
module count minus the truth, winning starters: {-3: 1, -2: 6, -1: 51, 0: 240, 1: 199, 2: 38, 3: 3}
on different sides of 15 outs: 34

probe 3 (900 game-sims, 897 decided, 3 ties, all stopped in the 13th inning)
A (the out count): the win goes to another pitcher: 33; the save changes: 3
B (count + pitcher of record): the win goes to another pitcher: 36; the save changes: 4; the loss: 0
A and B name different winners: 3
```

The probe scripts and their JSON records are in the session scratchpad (`sim557/`): they are
evidence for this plan, not part of the build. The build's tests 19 to 21 and the smoke's gate
replace them.
