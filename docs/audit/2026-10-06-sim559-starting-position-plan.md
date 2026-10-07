# The starting position of each starter (SIM-559): the finding, its cost, the fix — 2026-10-06

**Status:** FOUND + MEASURED + CODE BUILT 2026-10-06. The backfill is NOT RUN. The ticket is
SIM-559 (P2) in `BACKLOG.xlsx`. Found during the starting-pitcher fix (SIM-558) on 2026-10-05.

## 1. Why it matters

The simulator puts each team's real fielders in the field. It reads who plays where from the
lineup table, `raw.game_lineups`. That table records, for each starter, the LAST position he
held in the game, not the one he started at. One team-game in four therefore runs with a hole
in the defense: a fielder who moved mid-game holds his final slot, his starting slot is empty,
and the player who really started at his final slot has no glove. The fielder factor of the
fielding draw, the catcher factor of the steal and pickoff draws, the fielder named on the play
record and the arm factor of the advancement draw all read that map. Where a slot is empty the
factor goes flat; where it holds the wrong man, the factor scores the wrong man.

## 2. What is wrong

The loader's `_build_starting_lineup_rows` (`pipeline/etl/etl_historical_loader.py`) writes
`position_code` from the box feed's `player["position"]["abbreviation"]`. In the MLB Stats API
that field is the player's last position of the game. The feed's `player["allPositions"]`
lists every position he held, in order; its first entry is where he started. Every feed from
2017 on carries `allPositions` (checked on games of 2017-06-15 and 2020-08-15). The box-score
endpoint (`/api/v1/game/{pk}/boxscore`) carries the same two fields.

Game 823372 (Pittsburgh at home, 2026-10-05) shows the shape. The home lineup's codes, as the
table holds them and as the feed says they started:

| Slot | Player | `position` (stored) | `allPositions` | Started at |
|---|---|---|---|---|
| 3 | Bryan Reynolds | RF | LF, RF | LF |
| 7 | Tyler Callihan | RF | RF | RF |
| 8 | Jake Mangum | LF | CF, LF | CF |

The defense-map builder (`simulation/lineup_resolver.py`, `build_team_defense_map`) keeps one
player per slot name. Reynolds and Callihan both read RF; Reynolds bats first, so he holds RF
and Callihan has no glove. Mangum holds LF. Nobody holds CF. Three of eight slots are wrong or
empty on one side of one ordinary game.

The box-score table (`raw.game_player_stats.position_code`, parsed from the same feed in
`pipeline/etl/boxscore_ingest.py`) carries the same last-position value: on all 444,877 joined
starter rows the two codes are identical (zero differences).

## 3. How much

Read-only SQL over all 22,742 Final games of 2017-2026 (45,484 team-games), counting the eight
fielding codes (C, 1B, 2B, 3B, SS, LF, CF, RF) among each team-game's nine batting rows:

| Team-games | Count | Share |
|---|---|---|
| all eight codes present | 33,591 | 73.9% |
| one code missing | 10,802 | 23.7% |
| two or more missing | 1,091 | 2.4% |
| no catcher | 301 | 0.7% |

The hole rate is 20.5% in 2017 and 25-30% in every season since 2019 (29.9% in 2026 to date).
CF is the code most often missing (3,278 team-games), then LF (2,113), 2B (1,964) and RF
(1,768); LF (2,712) and RF (2,450) are the codes most often held twice. 195 team-games have a
'P'-coded batting row beside a designated hitter (a position player who finished the game
pitching; 55 have two 'P' rows in the order).

The hole census cannot see a swap: two starters who traded positions mid-game leave all eight
codes present and two slots wrong. A comparison against the box feed catches it. For the 45
balanced certifying games (`scripts/sim523_game_set.json`) and 300 random Final games (seed
0.42), the stored map was rebuilt from the database exactly as production builds it and compared
slot by slot with the starting map read from `allPositions[0]`:

| Sample | Team-games | With a defect | Slots missing | Slots wrong | Of slots | No catcher | Catcher wrong | Hidden swaps |
|---|---|---|---|---|---|---|---|---|
| the balanced 45 games | 90 | 29 (32.2%) | 37 | 21 | 720 | 2 | 0 | 0 |
| 300 random games | 600 | 175 (29.2%) | 188 | 141 | 4,800 | 3 | 4 | 4 |

47 of the balanced set's 810 starters and 250 of the random sample's 5,400 moved during the
game (5.8% and 4.6%). The random sample held 7 position players who pitched and 3 two-way
starters (P then DH). The instrument is `scripts/sim559_defense_map_compare.py`.

## 4. What it costs the simulator

Where the map is read:

- **The fielding draw.** `FullPoolSampler._f_live_fielder` weights every pool row at a position
  by the live defender's fielder-engine score against the row's own fielder. A slot the map
  lacks leaves that position's rows at weight 1.0 (flat); a wrong player scores the wrong man.
  The weights are rescaled to a mean of 1 within each position, so the defect never moves balls
  between positions. Every power is 1 today (owner ruling 2026-09-16), so the factor reads
  nearly flat either way.
- **The play record and the advancement draw.** `StateMachine._live_fielder_at_drawn_position`
  names the live defender at the drawn row's position; None when the slot is empty. The
  advancement draw then skips its arm factor, and the play record names no fielder.
- **The steal and pickoff draws.** `_running_game_keys` reads the fielding side's catcher; a
  side without a 'C' row has `catcher_id` None, and the catcher-throwing factor is skipped. The
  catcher also joins the pitch-draw staging key (the receiving ratio is OFF today).

The ten-game smoke (`scripts/sim559_smoke_arm.py`; the first ten games of the balanced set,
40 iterations each, the production flags, seeds 0-39 in both arms; the record is
`scripts/sim559_smoke.txt`). Arm `stored` runs the maps production builds today; arm `true`
runs the same games with the eight fielding slots and the catcher rebuilt from `allPositions[0]`
(the pitcher and the lineups unchanged). Seven of the ten games have a defect on one side.

| Read | Count (stored arm) | Share |
|---|---|---|
| balls in play drawn | 36,507 | |
| live defender missing at the drawn position | 1,876 | 5.1% |
| live defender a different player than the true starter | 821 | 2.2% |
| either | 2,697 | 7.4% |
| running-game key reads (pickoff + steal draws) | 88,326 | |
| no catcher | 4,318 | 4.9% (all in game 776142) |
| wrong catcher | 0 | |

The paired channel read (true minus stored, per game, mean over the ten games, the standard
error across games):

| Channel per game | Stored | Difference |
|---|---|---|
| runs | 9.03 | +0.05 ± 0.21 |
| hits | 16.82 | −0.10 ± 0.25 |
| doubles | 3.37 | −0.04 ± 0.13 |
| home runs | 2.36 | +0.06 ± 0.05 |
| walks | 3.46 | +0.12 ± 0.05 |
| strikeouts | 9.68 | −0.15 ± 0.09 |
| stolen bases | 1.17 | −0.06 ± 0.07 |
| caught stealing | 0.37 | +0.02 ± 0.03 |

No channel moves beyond what ten games resolve. The walk and strikeout differences come from
one game (776142, the side with no catcher): the catcher joins the pitch-draw staging key, so
adding him re-stages the draw and the two arms consume their random streams differently from
that point. That is stream divergence, not an effect of the catcher's receiving (it is OFF).
Two of the seven defective games (776151, 822970) came back byte-identical across the arms: the
true starter at the missing slot has no fielder-engine profile at that position and season, so
the factor stays neutral with or without him. The fix can only act where the fielder engine
scores the man.

**Acceptance bands.** None is expected to move. The fielder factor is mean-1 within a position
and at power 1, so the hit, double and reach-on-error bands cannot shift measurably; the steal
bands (attempts at second and third, the safe share) are the only ones the catcher factor
touches, through 2 of the 90 team-games, by a fraction of a percent at most. The first 45 × 130
lane after the backfill is the record; a move there is a finding, not an expectation.

## 5. The fix

**The loader (built).** `starting_position(pdata)` in `pipeline/etl/boxscore_ingest.py` returns
`allPositions[0]`, falls back to `position` for a payload without `allPositions` or with a
blank first entry, and None when neither names a position. `_build_starting_lineup_rows` uses
it ('UT' when None, as before). The loader's docstring records the change.

**The two-way starter.** A pitcher who also bats as the designated hitter lists `allPositions`
P then DH and bats as DH. His lineup row now reads 'P' with his batting slot. The resolver
(`_pick_team_slots`) treats a 'P' row with a batting order as both the pitcher candidate and
the batting-slot occupant, so he pitches and bats first, as he does. The box's `p_started`
(SIM-558) names him too. The old reading coded him 'DH' and left his side without a pitcher row
(the SIM-558 finding: 80 team-games).

**The position player who pitched.** `allPositions` SS then P reads 'SS'. The real starter keeps
the only 'P' row. The old reading gave such a side two 'P' rows (308 team-games in SIM-558's
census), and the balanced-set script scored the wrong man as a starter.

**The unique key.** `(game_pk, team_id, player_id, sequence)` allows one row per player per
sequence. The repair is an UPDATE of `position_code` on the existing sequence-1 row; no row is
added or deleted, so the key and the foreign keys are untouched.

**The backfill (built, NOT RUN).** `scripts/sim559_backfill_start_positions.py`: for every Final
game with lineup rows, one box fetch (`BoxscoreIngest.fetch_boxscore`, the SIM-545 client with
its bounded retry), `parse_starting_positions` (one row per batting-order starter, both codes),
a read of the game's stored starter codes, and ONE statement:

```sql
UPDATE raw.game_lineups AS l
SET    position_code = v.position_code
FROM   unnest($1::int[], $2::int[], $3::int[], $4::text[])
       AS v(game_pk, team_id, player_id, position_code)
WHERE  l.game_pk = v.game_pk AND l.team_id = v.team_id AND l.player_id = v.player_id
  AND  l.sequence = 1 AND l.is_starter
  AND  l.position_code IS DISTINCT FROM v.position_code
```

Only a row whose code differs is written, so the table's `updated_at` trigger marks exactly the
repaired rows (`updated_at > created_at`). The lineup insert is `ON CONFLICT DO NOTHING`, so a
re-load of a game never repairs its rows: this UPDATE is the only repair path. `--dry-run`
fetches and compares and writes nothing; `--done-file` makes the run crash-safe (the host
reboots often); five failures in a row stop it. Postgres only: no DuckDB lock, the app may keep
serving.

**The box-score table keeps the last position (decision).** The official box shows the last
position, and nothing grades a prop by position. One table, one meaning: the lineup row carries
the start, the box row carries the finish. No schema change, no migration. After the backfill
the two columns differ exactly where a starter moved during the game, which is the census of
mid-game position changes for free.

**The bundle does not change.** The defense map is resolved from Postgres per game at
simulation time; the engine-artifact bundle holds no lineups. No rebuild follows the backfill.
Cached simulation results (the DuckDB game cards, the Redis cache) hold the old maps until the
game is simulated again.

**Merge order.** Merge the loader change before the next nightly load, or the nightly writes
the new games with the old coding and the backfill must run again over them (`--seasons 2026`).

## 6. The run book

1. Merge the loader change; the unit lane green (`tests/unit/test_sim559_starting_position.py`,
   the SIM-409 lineup tests, the SIM-545 box tests, the resolver tests).
2. Dry run: `python scripts/sim559_backfill_start_positions.py --dry-run --max-games 200`. Read
   the move table (stored → starting pairs); expect LF↔RF, CF→LF/RF, DH→P and P→glove pairs,
   about 5% of starters.
3. The full run, detached, crash-safe:
   `docker compose run -d app python scripts/sim559_backfill_start_positions.py --done-file /data/sim559_done.txt`
   (scripts/ is baked into the image: rebuild the image first, or mount the worktree's scripts).
   About 3.5 hours at the default 0.25 s between MLB calls; `--sleep 0.1` brings it near 2.5.
4. Verify. The hole census (the SQL below) must read zero team-games with fewer than eight
   codes and zero without a catcher, except the genuine cases the box itself shows. The
   comparison `python scripts/sim559_defense_map_compare.py --balanced --random 300 --expect-clean`
   must read zero missing and zero wrong slots. The smoke arm `stored` re-run must tally zero
   missing and zero wrong reads.
5. The next 45 × 130 lane records whether any band moved.

The hole census:

```sql
WITH tg AS (
  SELECT l.game_pk, l.team_id,
         count(DISTINCT l.position_code) FILTER (WHERE l.batting_order IS NOT NULL
           AND l.position_code IN ('C','1B','2B','3B','SS','LF','CF','RF')) AS n_codes,
         bool_or(l.position_code = 'C' AND l.batting_order IS NOT NULL) AS has_c
  FROM raw.game_lineups l JOIN raw.games g ON g.game_pk = l.game_pk
  WHERE g.status = 'Final' AND l.is_starter AND l.sequence = 1
  GROUP BY 1, 2)
SELECT count(*) AS team_games,
       count(*) FILTER (WHERE n_codes = 8) AS all_eight,
       count(*) FILTER (WHERE n_codes = 7) AS one_missing,
       count(*) FILTER (WHERE n_codes <= 6) AS two_plus_missing,
       count(*) FILTER (WHERE NOT has_c) AS no_catcher
FROM tg;
```

## 7. Cost in time

| Route | Time | What it rewrites |
|---|---|---|
| the full ETL re-sweep | about 6 hours | every pitch, event and lineup row; cannot repair a lineup row (`ON CONFLICT DO NOTHING`) |
| the box-only backfill (this script) | about 3.5 hours (2.5 at `--sleep 0.1`) | the starters' `position_code` only; one request per game |

The box-score backfill of 20,270 games (SIM-545) ran in one afternoon at the same pacing.

## 8. Tests

`tests/unit/test_sim559_starting_position.py` (24 tests, pure, no DB): the helper on a moved
fielder, the two-way starter (→ 'P'), the position player who pitched (→ his glove), the
fallbacks and the column width; the loader on the real shape of game 823372 (all eight gloves
once, the sub skipped, the row shape kept), the two-way starter not double-added, an older
payload without `allPositions`; the defense map the resolver builds from the fixed rows (eight
gloves plus the pitcher) and, pinned as a baseline, the hole the old reading leaves; the
backfill parser (one row per batting-order starter, the team-id fallback, rows without a slot
or a position skipped) and the writer (one statement with the four arrays, nothing for no rows,
the command tag read). The SIM-409 loader tests, the SIM-545 box tests and the resolver tests
pass unchanged (103 in the four files).

## 9. The two SIM-558 leftovers

- **The balanced-set script's starters (built here).** `scripts/sim523_game_set.py` found the
  starters by `position_code == 'P'` and scored 89 of the set's 90 starters. It now reads the
  official box's `p_started` with the lineup's 'P' rows as the fallback, and counts every row
  with a batting slot as a batter. The set itself is an owner ruling and is not regenerated.
- **The API's missing-starter refusal (not built; in the ticket).** `_resolve_state_or_error`
  maps every `LineupResolutionError` to 404. A known game whose lineup names no pitcher is a
  data gap, not a missing game; it should answer 503 with `Retry-After`, like the unpublished
  lineup (SIM-409), because the box backfill can fill it. That needs an error subclass in
  `simulation/lineup_resolver.py`, which the SIM-558 worktree is editing: do it after that merge.

## 10. Decisions for the owner

1. Priority P2 as filed, or P1 before the next lane: the fix is cheap and the lane's defense
   maps are wrong on 29 of 90 team-games today.
2. When to run the backfill: it needs no stop of the app. It can run tonight.
3. The box table keeps the last position (recommended, §5); say so if you want the starting
   position on the box row too (a migration and a second column).
