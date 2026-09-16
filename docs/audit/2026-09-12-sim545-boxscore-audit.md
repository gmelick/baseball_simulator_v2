# The official box score vs the platform's own derivation — the 2024 season (2,472 games)

**Date:** 2026-09-12
**Ticket:** the official per-player box score as the prop ground truth (SIM-545), the
sub-ticket of the unpriced prop markets work (SIM-421).

**What this document records.** The study the ticket's definition of done asks for. The
box-score backfill ran for the whole 2024 season. The audit then compared the platform's
own per-pitch derivation with the official box score on every one of those games. This
document replaces the 40-game smoke that stood here earlier on the same day. Read the
tables as the measured distance between the two records, stat by stat.

## What ran

1. **The two migrations.** 0022 (the fifteen-value CHECK constraint on
   `raw.prop_odds.prop_stat`) and 0023 (`raw.game_player_stats`) are applied on the live
   Postgres. `alembic upgrade head` ran with `BASEBALL_DB_DSN` set to the host port. The
   head is `0023`.
2. **The full-season backfill.** `scripts/load_official_boxscores.py` ran for the 2024
   season. It fetched 2,432 games in this run; the 40-game smoke had loaded the other 40.
   The table now holds 2,472 games and 72,651 rows: 51,679 batter lines, 21,144 pitcher
   lines, and 172 two-way rows where the same player batted and pitched. Zero games
   failed. At `--sleep 0.2` the run took about 40 seconds per 100 games, about a quarter
   of an hour in all. Integrity: on every one of the 2,472 games the box-score runs per
   side equal `raw.games` `home_score_final` / `away_score_final`. Every Final 2024 game
   has rows.
3. **The audit.** `scripts/sim545_boxscore_audit.py` ran on all 2,472 games. For each
   stat it derives a per-player total from `raw.pitches` (plus the pickoff outs and the
   intentional walks in `raw.play_events`) and compares it with the official row.

## What the audit found

Three terms. The **exact-match rate** is the share of player-games where the derived
total equals the official total. The **mean absolute difference** is the average size of
the gap per player-game, with its direction ignored. The **signed bias** is the average of
derived minus official per player-game; a negative value means the derivation reads low.

### Batters (51,679 player-games)

| Stat | Exact-match rate | Mean absolute difference | Signed bias (derived − official) |
|---|---:|---:|---:|
| H | 1.0000 (one row differs) | 0.0000 | +0.0000 |
| HR | 1.0000 | 0.0000 | 0.0000 |
| TB | 1.0000 (the same row) | 0.0000 | +0.0000 |
| 1B | 1.0000 (the same row) | 0.0000 | +0.0000 |
| 2B | 1.0000 | 0.0000 | 0.0000 |
| 3B | 1.0000 | 0.0000 | 0.0000 |
| R | 0.9997 | 0.0003 | −0.0003 |
| SB | 0.9908 | 0.0093 | −0.0093 |
| RBI | 1.0000 | 0.0000 | 0.0000 |

### Pitchers (21,144 player-games)

| Stat | Exact-match rate | Mean absolute difference | Signed bias (derived − official) |
|---|---:|---:|---:|
| K | 0.9967 | 0.0033 | −0.0033 |
| BB | 0.9971 | 0.0029 | −0.0022 |
| H_ALLOWED | 1.0000 | 0.0000 | 0.0000 |
| OUTS | 0.9793 | 0.0209 | −0.0126 |
| ER | 0.8757 | 0.1785 | −0.0006 |

### The inexact stats, one at a time

**H, TB and 1B — one row in 51,679.** Game 746942, player 643376: the derivation reads 1
hit, the box reads 0. The same row moves H, TB and 1B by one. This is a scorer's change:
the official scorer re-ruled the play after the feed recorded it, and the pitch row keeps
the first label. At four decimals the rate still prints 1.0000.

**R — three batter lines in ten thousand, always low.** Every miss reads the derivation
one run LOWER than the box. The cause is not established. My hypothesis: the run scores on
an event with no pitch row (a balk, or a throw on a pickoff), so the per-pitch runner
flags never see it. Nobody opened the missed plays to confirm this.

**SB — nine batter lines in a thousand, always low.** Every miss reads the derivation one
or two steals LOWER than the box. The cause is not established. My hypothesis: a steal
credited on a play with no pitch row, or the second runner of a double steal that the
pitch-row steal columns record once. This is a hypothesis, not a verified cause.

**K — one pitcher-game in 300, low.** The cause is known. The feed anchors some
strikeouts to a trailing `no_pitch` event. The derivation reads the event label on the
last real pitch, so it does not see that strikeout. This is the residual of the
play-events parser fix (SIM-502c).

**BB — three pitcher lines in a thousand, mostly low.** The signed bias (−0.0022) is
smaller than the mean absolute difference (0.0029), so most misses read low and a few
read high. The derivation already adds the intentional walks from `raw.play_events`. The
cause is not established. My hypothesis for the low side: the same `no_pitch` anchoring
that moves the strikeouts. This is a hypothesis.

**OUTS — two pitcher-games in a hundred, mostly low.** The cause is known and was
documented when the events-based out label landed (SIM-501a/c). The derived outs come
from the innings-pitched formula over the pitch rows. That formula misses a pickoff out
and a runner out that the feed places on a different pitch from the one that retired him
(the displaced-runner out). The audit adds the `raw.play_events` pickoff outs; the
displaced-runner residual remains. The net bias is 0.0126 outs per pitcher-game, about
0.2% of the outs an average pitcher line records (about 6.2 outs, relievers included).

**ER — one pitcher-game in eight, a wash on net.** The signed bias is near zero
(−0.0006) and the mean absolute difference is large (0.1785). The cause is known. The
per-pitch `earned_runs_on_pitch` flag charges a run to the pitcher OF THE PITCH. The
official scorer charges an **inherited runner** — a runner a reliever finds on base when
he enters — to the pitcher who put him on base. So the two errors cancel inside one game:
the pitcher of the pitch reads high, the pitcher who put the runner on reads low, +2 for
one and −2 for the other. The team's earned runs are right; the per-pitcher line is not.

## What this means for the prop grading

- **The read.** The derivation is exact for every hit-type prop (H, HR, TB, 1B, 2B, 3B,
  hits allowed) and for RBI. It is NOT exact for SB, R, K, BB, OUTS and ER. That is the
  evidence for the owner decision this change implements: grade props on the official
  box score, never the derivation.
- **What the old path got wrong.** The event-label path graded five props (H, HR, TB, K,
  BB). It mis-graded the strikeout prop on about one pitcher-game in 300. It could not
  grade the other ten props at all.
- **The box is the reference.** A non-zero gap on a stat is a finding about the
  derivation or the pitch sweep, not about the box score. The accuracy comparison
  (`scripts/clv_backtest.py`) and `scripts/validate_props.py` read the box first and fall
  back to the event label only for a game with no rows. Every 2024 game has rows, so the
  fallback never fires on 2024.
- **Where the derivation still matters.** Only a lane that reads `raw.pitches` directly.
  The three known causes (K, OUTS, ER) tell such a lane what to expect. The three
  hypotheses (R, SB, BB) are open; none blocks grading.
- **BB reads the same from either source now.** The sim's BB line counts an intentional
  walk (`_BB_CANONICAL` in `simulation/sim_loop.py`). The box counts it. The event-label
  fallback in `simulation/prop_validation.py` now lists `intent_walk` in `_WALK_EVENTS`
  too. The fallback still under-counts a pitcher's walks by his intentional ones, because
  `raw.pitches` carries no `intent_walk` row; the box carries them.

## Still to do

1. **The other nine seasons' box scores.** One command:
   `python scripts/load_official_boxscores.py --seasons 2017 2018 2019 2020 2021 2022 2023 2025 2026`.
   About 2–3 hours at the polite default sleep. The 2024 season is the only season with
   odds loaded, so it is the only season the accuracy comparison can use today.
2. **The odds for the eight new markets.** This run has NOT happened. It hits the owner's
   contended BettingPros API, so it must be coordinated with the owner's own wrong-game
   odds re-load (SIM-536). The exact command is in the docstring of
   `scripts/load_historical_odds.py`.
3. **The accuracy comparison re-run.** After step 2. Read its header line first: it says
   how many games were graded on the official box score and how many on the fallback.
4. **The three open hypotheses** (R, SB, BB above). Open a ticket only if a lane ever
   needs the derivation for those stats.

## Closure — 2026-09-12 (afternoon)

The other nine seasons loaded the same day: 20,270 games, 596,480 rows, zero failures,
about 2 h 45 m at a 0.2 s pause between calls. All ten seasons: 22,742 games, 669,131
player lines. Every Final game has rows. On every game the box-score runs per side equal
the final score in `raw.games`. The ticket (SIM-545) is closed on that evidence; its one
open residue — the strikeout the box withholds on a dropped-third-strike reach — is the
scope of SIM-484.
