# Baseball Savant leaderboard audit — what we can use, and what it buys us

**Date:** 2026-09-10
**Author:** Claude Code (Data Engineer + ML/Modeling Engineer + Baseball Analyst pass)
**Scope:** every Baseball Savant leaderboard that offers a CSV download, checked against what
this platform already reads.
**Method:** I fetched all 46 leaderboard CSV endpoints directly and read the real column
headers. I did not work from memory or documentation. I then measured two things for the
batter data you asked about: how stable each new metric is year to year, and how much of it
our current features already explain. Raw pulls and scripts are in the session scratchpad.

---

## 1. The headline

**Yes — and the batter data you want is the single best opportunity on the board.**

Three Savant leaderboards carry the physical swing data: **Bat Tracking**, **Swing Path
(attack angle)** and **Batting Stance**. Together they add ten columns that pass both tests
I ran. Five of them are *more stable year to year than any feature the batter engine reads
today*, and four of them are *almost completely new information* — our existing 17 features
explain almost none of their variance.

Two facts make this unusually clean to adopt:

- **Coverage is total.** For 2024 I matched **456 of 456** batter-seasons the engine scores.
  Pulled at full roster depth the boards return ~650 batters a season, against ~651
  batter-seasons in our own table.
- **The data window is an exact fit.** Savant publishes this data for **2023 through 2026**
  and nothing earlier. Our simulation draws from a **four-season pool window, 2023-2026**
  (`RECENCY_FLOOR_SEASONS = 4`). Every pool row and every live player sits inside the window
  where the data exists.

There is a second, separate finding I did not go looking for. **Three feature blocks the
engines already weight are 100% empty in our database**, and Savant fills all three. The
outfield arm block is the worst: it is 30% of an outfielder's similarity score, and every
column in it is NULL. That is a correctness bug, not an enhancement.

---

## 2. The rule that decides what is usable

Every Savant leaderboard is a **season-level summary per player**. That single fact settles
where each one can go.

Our simulator makes every decision by drawing a real historical play from a filtered pool,
weighted by similarity. A weight can come from two places:

1. **A per-row attribute of the pool** — the pitch's velocity, the batted ball's launch
   angle, the park. This needs the value attached to each individual pitch or batted ball.
2. **An actor's similarity score** — how similar the live batter is to the batter on the pool
   row. This needs only a season-level profile per player.

Season-level leaderboards can only serve route 2. **No Savant leaderboard can add a per-pitch
pool column.** What they can do is make the actor similarity scores sharper, and every actor
score is already wired as a draw weight:

| Actor score | Which draw it weights | Power in production today |
|---|---|---|
| Batter | the pitch draw | 1.0 |
| Batter | the fielding draw | 4 |
| Fielder (per position) | the fielding draw | 1.2 |
| Runner, stealing | the steal draw | 12 |
| Runner, advancing | the advancement draw | 20 |
| Catcher, throwing | the steal draw | 2 |
| Pitcher, holding runners | the steal draw | 12 |

So the question for each leaderboard is simply: **does it make one of these scores better?**
That is the question I answer below.

---

## 3. The batter physical-swing upgrade (what you asked for)

### 3.1 What the batter engine reads today

The engine scores 26 features in four groups: plate discipline (7), batted ball (8), platoon
splits (7) and power/results (4). You are right about the character of them. **Only three are
physical measurements** — average exit velocity, maximum exit velocity and average launch
angle. Every other feature is the *rate at which an outcome happened*: whiff rate, pull rate,
walk rate, ground-ball rate and so on.

The engine's own design notes say it wants to measure "HOW a batter approaches an at-bat, not
whether he got lucky". The physical swing data is that measurement, taken directly.

### 3.2 The three boards and their columns

**Bat Tracking** — `/leaderboard/bat-tracking`
`avg_bat_speed`, `swing_length`, `hard_swing_rate`, `squared_up_per_swing`,
`squared_up_per_bat_contact`, `blast_per_swing`, `blast_per_bat_contact`, `whiff_per_swing`,
`percent_swings_competitive`, `swords`, `batter_run_value`, plus raw counts.

**Swing Path** — `/leaderboard/bat-tracking/swing-path-attack-angle`
`attack_angle`, `attack_direction`, `swing_tilt`, `ideal_attack_angle_rate`,
`avg_intercept_y_vs_plate`, `avg_intercept_y_vs_batter`, `avg_batter_x_position`,
`avg_batter_y_position`, `avg_bat_speed`.
*Terms:* **attack angle** is the bat's upward or downward angle at contact. **Attack
direction** is whether the bat points to pull or opposite field at contact. **Swing tilt** is
how steeply the swing plane is inclined. **Intercept** is how far in front of the plate the
batter meets the ball.

**Batting Stance** — `/visuals/batting-stance` *(note: under `/visuals/`, not
`/leaderboard/`)*
`avg_foot_sep`, `avg_stance_angle`, `avg_batter_x_position`, `avg_batter_y_position`,
`avg_intercept_y_vs_batter`, `avg_intercept_y_vs_plate`.
*Terms:* **foot separation** is the distance between the feet in the stance. **Stance angle**
is how open or closed the batter stands. **Batter x/y position** is where he stands in the
box — how far off the plate, and how deep toward the catcher.

This board emits **one row per batter per batting side**. A switch hitter gets two rows, one
left and one right. That maps directly onto the engine's platoon design.

### 3.3 Test one — is it real signal?

I measured how well each metric predicts the same player's next season. This is exactly the
evidence the engine already uses to set its per-feature weights. A high correlation means the
number describes the player, not the sample.

**Our current features** (our database, 2023→2024 and 2024→2025, 721 batter-season pairs):

| Feature | Year-to-year correlation |
|---|---|
| whiff rate | **0.825** ← our most stable feature today |
| contact rate | 0.822 |
| chase rate (o-swing) | 0.820 |
| first-pitch take rate | 0.809 |
| average exit velocity | 0.789 |
| strikeout rate | 0.751 |
| hard-hit rate | 0.751 |
| average launch angle | 0.743 |
| maximum exit velocity | 0.734 |
| barrel rate | 0.728 |
| walk rate | 0.646 |
| fly-ball rate | 0.616 |
| opposite-field rate | 0.581 |
| home-run rate | 0.551 |

**The new physical metrics** (Savant, 2023→2024 and 2024→2025, 480-594 pairs):

| New metric | 2023→2024 | 2024→2025 |
|---|---|---|
| swing tilt | **0.903** | 0.888 |
| average bat speed | **0.901** | 0.892 |
| position in the box, depth | **0.889** | 0.863 |
| hard-swing rate | 0.888 | 0.881 |
| swing length | 0.882 | 0.887 |
| foot separation | 0.855 | 0.830 |
| position in the box, off the plate | 0.854 | 0.849 |
| stance angle | 0.833 | 0.785 |
| contact depth vs the plate | 0.792 | 0.812 |
| contact depth vs the batter | 0.731 | 0.767 |
| attack angle | 0.718 | 0.756 |
| attack direction | 0.603 | 0.682 |
| ideal attack angle rate | 0.567 | 0.571 |
| blast rate per swing | 0.478 | 0.435 |
| squared-up rate per swing | 0.397 | 0.393 |
| percent of swings competitive | **0.003** | 0.083 |

**Read this carefully.** Five new metrics — swing tilt, bat speed, box depth, hard-swing rate
and swing length — are more stable than the best feature the engine owns. That is a large
gap, not a marginal one. At the other end, "percent of swings competitive" has a correlation
of essentially zero. It is noise. It must not be added.

### 3.4 Test two — is it new information?

A stable metric is still worthless if it restates something we already have. For each new
column I computed its correlation against all 17 current features and kept the largest.

| New metric | Largest correlation with anything we already read | Closest existing feature |
|---|---|---|
| foot separation | **0.087** | fly-ball rate |
| stance angle | **0.109** | strikeout rate |
| position in the box, depth | **0.123** | chase rate |
| swing tilt | **0.240** | fly-ball rate |
| position in the box, off the plate | 0.246 | hard-hit rate |
| contact depth vs the plate | 0.271 | opposite-field rate |
| contact depth vs the batter | 0.318 | opposite-field rate |
| swing length | 0.421 | barrel rate |
| ideal attack angle rate | 0.431 | opposite-field rate |
| attack direction | 0.526 | pull rate |
| attack angle | 0.597 | fly-ball rate |
| average bat speed | 0.765 | maximum exit velocity |
| hard-swing rate | 0.766 | maximum exit velocity |
| squared-up rate per swing | 0.779 | strikeout rate |
| blast rate per swing | 0.843 | hard-hit rate |
| blast rate per contact | 0.887 | hard-hit rate |
| whiff rate per swing | **0.983** | contact rate |

Stance geometry is the prize. Foot separation, stance angle and box depth are close to
independent of everything we measure. They describe a batter our current features cannot
tell apart at all.

At the other end, Savant's whiff-per-swing is our contact rate with the sign flipped. Adding
it would double-count a feature the engine already weights.

### 3.5 What I recommend adding

Ten columns pass both tests. I would group them as a new fifth sub-score for physical swing
identity, and refit the four existing sub-score weights around it.

**Tier 1 — stance geometry. New information, very stable. Add these first.**

1. `avg_foot_sep` — foot separation
2. `avg_stance_angle` — open or closed stance
3. `avg_batter_y_position` — depth in the box
4. `avg_batter_x_position` — distance off the plate

**Tier 2 — swing shape. The mechanical description of the swing.**

5. `swing_tilt`
6. `attack_angle`
7. `avg_intercept_y_vs_plate` — contact depth
8. `attack_direction`

**Tier 3 — bat speed. Very stable, but partly restates maximum exit velocity.**

9. `avg_bat_speed`
10. `swing_length`

**Optional, low weight:** `ideal_attack_angle_rate` (stability 0.57, redundancy 0.43). It is
a judgement call for the modelling pass.

**Do not add:**

- `whiff_per_swing` — a duplicate of contact rate (0.983).
- `blast_per_swing`, `blast_per_bat_contact`, `batted_ball_event_per_swing`,
  `squared_up_per_swing` — largely restate hard-hit rate and strikeout rate.
- `hard_swing_rate` — Savant computes it from bat speed. Adding both double-counts.
- `percent_swings_competitive` — no year-to-year signal.
- `swords`, `batter_run_value` — outcome summaries, and the engine already avoids those.

### 3.6 The platoon-split option

The bat-tracking family accepts a `pitchHand` filter, so we can pull every physical metric
split by the pitcher's throwing hand. That would feed the engine's platoon sub-score with
physical features instead of only rates.

I measured whether the split is worth the work. For bat speed, the spread of "versus lefties
minus versus righties" is about **half** the spread between players. That is a real
difference, not nothing. But the versus-lefty sample is roughly a quarter of a batter's
swings, so part of that spread is small-sample noise, and I did not separate the two.

**Recommendation:** land the overall features first. Treat the platoon split as a second
step, and fit it the way every other weight in this platform gets fitted.

### 3.7 What it costs to run

This is the cheap path, and that is worth saying plainly. Both pools already carry
`batter_id` and `season` on every row. The batter similarity score is looked up from a
nightly matrix keyed on batter and season. **Nothing about the pools has to change.**

The chain is:

1. A migration adds the new columns to `derived.batter_season_metrics` (schema v24 → v25).
2. A new loader pulls the three boards into `raw.*` tables, copying the existing sprint-speed
   loader.
3. The nightly profile builder joins them into the batter profile.
4. `make engine-artifacts` with `--what actors_sim` rebuilds the batter score matrix.
5. The sub-score weights and the new bandwidth get fitted, then certified on the balanced
   45-game lane.

**No pool rebuild. No re-sweep of the raw pitch data.** The one operational obstacle is
unchanged: the running application holds the DuckDB writer lock, so the stack must stop for
the write (the DuckDB lock item, SIM-524).

### 3.8 The one risk

Savant publishes none of this data before 2023. Our window is exactly 2023-2026 today. **If
the pool window ever widens past four seasons, these features go empty for the oldest
seasons.** The engine must shrink a missing value toward the league average rather than treat
it as zero, and the fitting pass must confirm that a missing row stays draw-neutral.

---

## 4. Three feature blocks that are empty today, and Savant fills them

I checked every column in our derived profile tables for the 2023-2026 window. Three blocks
that the engines actively weight contain **no data at all**.

### 4.1 The outfield arm — 30% of an outfielder's score is inert

The fielder engine gives an outfielder's arm a 30% weight through four features:
`arm_hold_rate`, `arm_thrown_out_rate`, `arm_advancement_prevention` and `of_arm_runs`.

**All four are NULL on all 4,799 fielder-seasons in the window.** So are `arm_strength`,
`arm_opportunities`, `arm_holds` and `arm_assists`. The outfielder factor that weights the
fielding draw is running on range and errors alone.

**The fix, from two boards:**

- **Arm Strength** (`/leaderboard/arm-strength`) — throw velocity in miles per hour, overall
  and per position (`arm_of`, `arm_lf`, `arm_cf`, `arm_rf`, `arm_inf`, `max_arm_strength`).
  387 players with 50 or more throws in 2024.
- **Arm Value** (`/leaderboard/baserunning`, bare `csv=true`) — `fielder_runs_hold`,
  `fielder_runs_advances`, `fielder_runs_thrown_out`, plus the opportunity and attempt counts
  (`n_opp_xb`, `n_att_xb`, `rate_att_xb`) and the expected rate against a generic fielder.

These map one to one onto the four dead features. The open fielder arm features item
(SIM-521) was filed as a tuning problem when it is really a missing-data problem, so it was
retired into this work on 2026-09-10.

### 4.2 The catcher's arm — a feature weighted 0.800 with no data

The catcher engine's throwing sub-score weights `arm_strength_mph` at 0.800. It reads
`derived.catcher_season_metrics.arm_strength_mean`, which is **NULL on all 420
catcher-seasons** in the window.

Separately, the 2026-05-29 schema reconciliation *removed* the exchange-time sub-score
because the column did not exist. It exists on Savant.

**The fix, from two boards:**

- **Catcher Pop Time** (`/leaderboard/poptime`) — `maxeff_arm_2b_3b_sba` is arm strength on
  steal attempts, `exchange_2b_3b_sba` is exchange time, `pop_2b_sba` is measured pop time,
  split by caught-stealing and successful steal. 99 catchers at no minimum, against 100
  catcher-seasons in our table for 2024.
- **Catcher Throwing** (`/leaderboard/catcher-throwing`) — `arm_strength`, `exchange_time`,
  `pop_time`, `est_cs_pct`, `cs_aa_per_throw`, and the runner context each throw faced
  (`seasonal_runner_speed`, `runner_distance_from_second`).

The catcher throwing score weights the steal draw at power 2. Filling it makes that draw
respond to a catcher's actual arm.

### 4.3 First-base receiving — 87% empty

`scoop_success_rate` and `scoop_opportunities` are NULL on 87% of fielder-seasons.
**First Base Receiving** (`/leaderboard/first-base-scoops-receiving`) supplies
`oaa_scoop`, `oaa_bounce`, `oaa_on_target`, the counts behind each, and the first baseman's
height and throwing hand.

---

## 5. Every other engine, ranked by what it would buy

### 5.1 The steal draw — the strongest remaining opportunity

The steal draw is weighted by three actor scores at high powers: the runner at 12, the
pitcher at 12, the catcher's arm at 2. The engines behind them are thin. The runner's
stealing engine reads **four** features. The pitcher's engine reads **three**, all outcomes.

**Basestealing Run Value** (`/leaderboard/basestealing-run-value`) adds the physical input
those engines lack: **`r_primary_lead` and `r_secondary_lead`** — how far off the bag the
runner takes his lead, in feet, before the pitch and after it. It also gives initiation
counts, pickoffs, balks and the same lead figures restricted to steal attempts.

**Pitcher Running Game** (`/leaderboard/pitcher-running-game`) gives the mirror image: the
lead a pitcher *allows*, his caught-stealing runs above average, and his pickoff counts.

Lead distance is a measured behaviour, not an outcome rate. It is exactly the kind of feature
this platform's design prefers.

### 5.2 The advancement draw — the strongest actor factor in the sim

The advancing runner's score weights that draw at power **20**, the highest power in
production. Its engine's speed sub-score is a single feature: sprint speed.

**Baserunning** (`/leaderboard/baserunning`) gives extra-base attempt and success rates with
the correct denominator (`n_opp_xb`, `n_att_xb`, `rate_att_xb`) and, importantly, the
expected rate for a generic runner in the same situations — the baseline our own rates lack.

**Running Splits** (`/leaderboard/running_splits`) gives the runner's distance at every
0.05 seconds after contact. That is the full acceleration curve, not one top-speed number.
A slow starter with a high top speed and a fast starter with a low one currently look
identical to us.

### 5.3 The fielding draw

**Outs Above Average** (`/leaderboard/outs_above_average`) adds two things we do not compute:
outs above average **split by batter handedness** (`outs_above_average_rhh` /
`_lhh`), and a directional breakdown (in front, behind, toward each line).

**Outfield Jump** (`/leaderboard/outfield_jump`) decomposes an outfielder's first three
seconds into reaction, burst and route quality. Those separate the skill from the speed.

**Fielding Run Value** (`/leaderboard/fielding-run-value`, bare `csv=true`) gives the full
run-value split — range, arm, double plays, framing, throwing, blocking — plus outs recorded
at each position, which tells us where a player actually played.

### 5.4 The pitch draw

**Arm Angle** (`/leaderboard/pitcher-arm-angles`) gives `ball_angle`, the pitcher's arm angle
in degrees, plus release and shoulder coordinates. This is one clean physical number. Note
that the pitcher engine deliberately excludes its release-point sub-score, so this is a
design question for the modelling pass, not an automatic add.

**Active Spin** and **Spin Direction** describe how much of a pitch's spin actually moves the
ball, and where the spin axis points. Our pitch model uses spin rate and spin axis but not
the efficiency of that spin.

**Pitch Arsenal Stats, batter view** (`/leaderboard/pitch-arsenal-stats?type=batter`) gives
each batter's results against each pitch type — whiff rate, put-away rate and expected
weighted on-base against four-seamers, sliders, changeups and so on. This is a natural
addition to the batter profile, and it is the closest thing on Savant to a matchup feature.

### 5.5 The catcher receiving ratio (currently switched off)

**Catcher Stance** (`/leaderboard/catcher-stance`) gives the share of pitches a catcher
receives on one knee versus a traditional crouch, and the framing, blocking and throwing run
value **for each setup separately**. When the receiving ratio work is enabled (SIM-526), this
is the physical explanation for why a catcher frames well. 329 catcher rows.

### 5.6 The manager

Savant publishes nothing that helps the manager engine. The real per-team manager profiles
work (SIM-427) stays blocked on our own data.

---

## 6. Boards with no current use

| Board | Why not |
|---|---|
| Expected Statistics | We can already fill the engine's optional `xba` / `xslg` columns from it, but the engine guards them and weights them low. Small win. |
| Statcast (exit velocity summary) | We compute all of it from raw pitch data. |
| Batted Ball | Same. Our own pull, straight and opposite-field rates come from raw data. |
| Percentile Rankings | Percentile ranks, not raw values. The raw values live on other boards. |
| Sprint Speed | Already ingested (`raw.sprint_speed`). |
| Statcast Park Factors | **No CSV export** — the table is drawn by scripts. We compute our own park factors. |
| Pitch Tempo, Pitch Timer Infractions | No decision in the sim depends on pace. |
| Home Runs | Not a similarity input. It is useful as an *independent check* on the fence stage: `xhr`, and the count of parks each home run would have left. |
| Swing/Take | Run value by zone bucket (heart, shadow, chase, waste). Partly restates chase and zone-swing rates. Worth a look during the modelling pass, not before. |
| Swing Timing + Miss Distance | Physically interesting — how early or late a batter is, and by how far he misses. **I did not measure its stability or redundancy.** Treat as an unranked candidate. |
| Automated ball-strike challenges | Not a similarity input. Worth knowing that it exists, because it changes called strikes in 2026 pool rows and compresses framing value going forward. |

---

## 7. How to pull the data — the practical notes

We already have the pattern. `pipeline/etl/etl_sprint_speed_loader.py` fetches a Savant CSV,
upserts it into a `raw.*` table, and the nightly profile builder joins it. Copy that file.

Traps I hit, all confirmed by testing:

- **Savant blocks the default Python user agent.** Send a browser-like one. The existing
  loader already does.
- **Three different season parameter styles exist.** Older boards use `year=`. The
  bat-tracking family uses `seasonStart=` and `seasonEnd=`. The run-value boards use
  `season_start=` and `season_end=`. Sending the wrong one is silent — the board returns the
  current season and looks fine. **Always check the season on a returned row.**
- **Some boards reject any parameter.** Fielding Run Value and Baserunning only answer to a
  bare `csv=true` plus the `season_start` / `season_end` pair. Adding `year=` returns zero
  rows or HTTP 500.
- **The default is qualified players only.** Set `minSwings=0` on the bat-tracking family and
  `min=0` elsewhere to get the full roster. This is the difference between 215 batters and
  650.
- **Batting Stance is at `/visuals/batting-stance`, not under `/leaderboard/`.** It is not
  linked as a leaderboard.
- **Savant is slow.** A full-roster season pull took one to three minutes per board during
  this audit. Build the loader to fetch one season at a time and retry.

---

## 8. Suggested sequence and tickets

**These six are filed in `backlog.xlsx` as of 2026-09-10.** That spreadsheet is now the live
open-ticket board — `BACKLOG.md` was retired the same day, and the closed history is frozen at
`docs/archive/BACKLOG-history.md`. One risk worth naming: the spreadsheet is untracked in git,
so it has no version history and no backup.

The priorities below are my proposal, not a ruling. I put the loader, the batter features and
the empty-column fix at P1, which adds three rows to a band that already holds seven. Move
them if that is too many.

| Ticket | Work | Why this order |
|---|---|---|
| **SIM-528** · P1 | The Savant ingest layer: one shared loader, one `raw.*` table per board we adopt, the season-parameter handling above. | Everything else depends on it. |
| **SIM-529** · P1 | The batter engine's physical swing features — the ten columns in §3.5, a new sub-score, the refit, and the balanced 45-game lane. | What you asked for. Largest measured gain. |
| **SIM-530** · P1 | Fill the empty arm columns — outfielder arm and catcher arm, exchange time, first-base receiving. | A bug fix, not an enhancement. It absorbs the retired fielder arm item (SIM-521). |
| **SIM-531** · P2 | Lead distance and opportunity features for the stealing and advancing runner engines, and the pitcher's hold engine. | Highest actor powers in the sim (12 and 20). |
| **SIM-532** · P3 | The fielder's platoon split and jump features. | Improves the fielding draw. |
| **SIM-533** · P3 | Pitcher arm angle and spin-shape features. | Lowest confidence. Needs a design decision on release point. |

**Two standing rules apply to all of it.** Every new factor is a draw weight, fitted, or it
stays off — no post-draw adjustment. And nothing gets a betting-value read until every band
the lanes grade is inside its range, which today still waits on the strikeout shortfall in
the pitch and pitch-result split (SIM-527).

---

## 9. What I did not check

I want to be clear about the limits of this pass.

- I measured stability and redundancy **for the batter data only**, and only for 2023-2025.
  I did not run the same two tests on the fielding, baserunning or catcher boards. Their case
  rests on filling columns that are empty, which needs no test, and on adding measurements we
  plainly do not have.
- I did not measure whether the new batter features **improve the simulation's output**. That
  can only come from the certifying lane after the features land and the weights are fitted.
  A better similarity score is not automatically a better sim.
- I did not check season coverage for every board across all four window seasons. I confirmed
  it for the three batter boards and for catcher pop time.
- Savant may rename or drop columns without notice. Every figure here comes from a live pull
  on 2026-09-10.
