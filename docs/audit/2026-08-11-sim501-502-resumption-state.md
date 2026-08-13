# SIM-501 / SIM-502 — resumption state

**Written 2026-08-11 so this work can restart in a fresh context.** Read this, then
`BACKLOG.md` for the ticket rows. Next free ticket id: **SIM-503**.

---

## The one-paragraph version

`raw.pitches` holds one row per PITCH, and several things the simulator needs are not pitches.
SIM-501 fixed the out counting, which was badly wrong and is now correct and landed. SIM-502 adds a
table for non-pitch play events — pickoffs, stepoffs, balks, intentional walks. **Its code is landed
but INERT, it has four open defects, and its migration must not be applied yet.**

---

## LANDED AND CORRECT — SIM-501

### `raw.pitches.outs` was wrong on 46% of plate appearances

The loader kept a running `outs` variable that it updated AFTER building each row, so every row
carried the PREVIOUS play's entry value. It now reads the payload directly:

* per row — that pitch's own `play_event["count"]["outs"]`
* per play — `playEvents[0]["count"]["outs"]`, the state entering the play
* carried forward — `play["count"]["outs"]`, the state after it, for pitch-less plays

**Verified: 5,248 / 5,248 = 100% agreement with the payload over 70 games, up from 53.9%.**

An earlier check of this column looked at its DISTRIBUTION — 0/1/2, no 3s, sensibly declining — and
called it sane. The distribution was plausible and the values were wrong half the time. Checking that
a number looks reasonable is not checking that it is right.

### `outs_on_pitch` was zero on nearly every out

It was derived as `play_event["count"]["outs"] - outs`. `count.outs` is constant across a plate
appearance, so the subtraction could not produce a delta: **92.6% of balls in play that became an out
recorded 0, and 100% of strikeouts did.** It now COUNTS runner movements keyed to the pitch index.

**Verified: `SUM(outs_on_pitch) / real outs = 0.990.** The residual is SIM-502c, below — and the
comment in the code explaining the residual is wrong; see that ticket.

### The half-inning marker advanced in the wrong scope

`prev_half` was assigned inside the pitch branch, so a pitch-less play never reached it and the reset
fired twice, destroying carried base state. Now assigned at play level. **11 occurrences in 1,066
games.** ⚠ This fix is correct and INCOMPLETE — see SIM-502a.

---

## LANDED BUT INERT — SIM-502

### State of each piece

| Piece | State |
|---|---|
| `db/migrations/versions/0018_sim502_play_events.py` | written, **NOT APPLIED** |
| `pipeline/etl/play_events.py` | pure extractor, no network or DB |
| loader wiring | writes inside the game's transaction |
| `tests/unit/test_sim502_play_events.py` | 23 tests, all passing |
| `raw.play_events` in the database | **DOES NOT EXIST** |

**The write is inert by design.** `_write_play_events` probes `to_regclass('raw.play_events')` and
returns 0 when the table is absent, logging once per run. So the code on master cannot break a
nightly ETL run. **Do not remove that guard until the four defects are closed.**

### What is verified working

Over 300 real games: pickoff outs **42/42**, pickoff errors **22/22**, zero duplicate natural keys,
every `base` value inside the widened 1..4 CHECK.

### The four defects — three FIXED 2026-08-13, one open

Full detail in `BACKLOG.md` under the SIM-502 banner. In short:

* **502a** (P1) — ✅ FIXED. The loader reads the feed's `runner_placed` announcement (the base is
  carried directly on the event) and re-seeds the state after the half-inning reset. Tested
  THROUGH the loader this time. 31/31 correct over all 216 extras games of 2024.
* **502b** (P1) — ✅ FIXED. The extractor now requires the caller's pre-play score and ignores the
  play's own `result` scores. 0 mismatches on 2,452 rows over 344 games.
* **502c** (P2) — ✅ CLOSED, mostly by SIM-501a. Measured over 357 games: the displaced outs land
  in `raw.play_events` (pickoffs), the `sb_*` columns (mid-PA caught stealings) or `events`
  (batter strikeouts — the decision: a feed-displaced batter out belongs to `events`, and it is
  already there). Residual ~0.04% of outs accepted and documented. The false loader comment is
  replaced with the measured taxonomy.
* **502d** (P3) — ✅ FIXED. Base parsed from the throw's description / the action's eventType;
  the runner movement still wins. 0 of 1,572 pickoff rows missing base over 344 games.

---

## Payload facts, so nobody re-derives them

Verified against real `statsapi.mlb.com/api/v1.1/game/{pk}/feed/live`. Three of these are NOT where a
reasonable person would guess.

| Thing | Where it lives |
|---|---|
| pickoff THROW | playEvent `type == "pickoff"`, `details.eventType` **EMPTY** |
| pickoff OUTCOME | a SECOND playEvent, `type == "action"`, eventType `pickoff_1b` / `pickoff_caught_stealing_2b` / `pickoff_error_1b`. **The runner movement is keyed to THIS index, not the throw.** |
| pickoff out/error | the RUNNER entry — `movementReason` `r_pickoff_*` |
| stepoff | playEvent `type == "stepoff"`, eventType empty |
| balk | playEvent `type == "action"`, eventType `balk` or `forced_balk` |
| intentional walk | the PLAY's `result.eventType`. **No playEvents at all.** |
| automatic runner | playEvent `type == "action"`, eventType `runner_placed` |
| home plate | `outBase` reads **`"4B"`**, not `"home"` |
| pre-play outs | `playEvents[0]["count"]["outs"]` |
| post-play outs | `play["count"]["outs"]` |

Per-season volumes, extrapolated from 150 games: pickoff throws ~9,700, stepoffs ~4,400, pickoff outs
~340, balks ~194, intentional walks ~340-450.

---

## How to work on this

**Sample hundreds of games, never dozens.** Both review rounds found every defect from real payloads
at scale and none from reading code. A 70-game sample reported "100%" on a metric that 950 games
showed was wrong.

**Drive the real loader, not a fixture:**

```python
import pipeline.etl.etl_historical_loader as L
rows, game_dict = L._fetch_game_pitches(pk, {})
play_event_rows = game_dict["_play_events"]
```

**Test through the caller, not just the extractor.** SIM-502a exists because the unit test hands
state directly to the extractor and never drives the loader — so it documents a bug it cannot catch.

**Run a third adversarial review before applying 0018.** Two rounds, four defects each. The expensive
failure is a constraint violation partway through a re-sweep, so weigh that above everything.

---

## Corrected figures

**The re-sweep takes ~6 hours, not 55.** `.sweep_progress/` shows 2017-2025 ran 17:53 to 00:02 —
**6 hours 9 minutes for nine seasons**. The 55-hour figure came from the span between the first and
last file timestamp, but the 2026 file is the IN-PROGRESS season and is appended to over days. A span
is not a duration. Corrected in `BACKLOG.md` and the ticket list.

---

## Related open work

*(Updated 2026-08-13.)*

* **SIM-501a/c — CLOSED 2026-08-13.** The events-based out label is in
  (`pipeline/statcast_events.py`), SIM-457 is re-landed per site, and no profile-computor site
  reads `outs_on_pitch` (a unit test enforces it). SIM-501b's ETL fix is landed; the swept DATA
  stays pre-fix until the re-sweep, and nothing reads the column any more.
* **The profile recompute still must not run**, for two remaining reasons: **SIM-458** (the
  run-expectancy fix) is still reverted, and the swept `raw.pitches.outs` (pre-play outs) is
  stale-by-one-play on 46% of plate appearances until the re-sweep — the situation/RE24 features
  group by it. Sequence: close SIM-502a..d → re-sweep → re-land SIM-458 → SIM-459.
