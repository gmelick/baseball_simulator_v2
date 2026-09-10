# Where Savant's data really lives, and how to get point-in-time values

> **STATUS 2026-09-10 — BUILT for the batter data (SIM-534).** The per-pitch
> loader, the cutoff on every batter source, and the guards are in. What the
> research below recommended is what shipped, with three corrections found by
> building it:
>
> 1. **The per-pitch bat-tracking columns start in 2024, not 2023.** A 2023
>    regular-season day returns pitches with every one of them empty, even
>    though the leaderboard publishes 2023 season values. Only `arm_angle` and
>    `hyper_speed` go back further. So the leaderboard stays as the 2023 source.
> 2. **That turns out not to matter**, because look-ahead is about the
>    simulation date, not the season. A 2023 full-season aggregate holds nothing
>    recorded after a 2025 cutoff. Only the season *containing* the cutoff has to
>    be truncated, and every season from 2024 on can be.
> 3. **The contact-depth feature moved from vs-plate to vs-batter.** The
>    per-pitch export publishes only the vs-batter intercept, and it is the
>    better feature anyway: it describes the batter's own swing geometry, while
>    where he stands in the box is already carried separately.
>
> Still season-only, exactly as §3 says: sprint speed, outs above average, arm
> strength, pop time, baserunning and the catcher metrics. §5's measurement of
> what the prior-season fallback costs stands.

**Date:** 2026-09-10
**Question:** can we get Savant data "as of a date" instead of whole-season, so a backtest of
2025 games does not use numbers that only existed after the season ended?
**Short answer:** yes for the batter swing data — completely. No for the fielding and running
measurements, which Savant publishes only as season totals. The split is clean, and it lands
in a good place: the metrics we *can* date are the ones that move within a season, and the
ones we cannot are the ones that barely move at all.

Everything below was probed live on 2026-09-10, not inferred.

---

## 1. There is no general Savant API

The `/savant/api/v1/` path you spotted is not a REST surface. Only specific named endpoints
exist under it. Every other path I tried returns the site's 404 page with a 200-sized HTML
body:

| Path | Result |
|---|---|
| `/savant/api/v1/trending-players` | 200, JSON — a 40-odd row popularity list. No statistics. |
| `/savant/api/v1/` · `/players` · `/search` · `/player/{id}` · `/leaderboard` | 404, HTML |

The trending-players response carries name, id, team, position and a percentage trend. It is
a search-box convenience, not a data feed.

## 2. Savant's three real data surfaces

**(a) The per-pitch search export — this is the raw layer.**
`/statcast_search/csv` returns **119 columns per pitch**, filtered by `game_date_gt` /
`game_date_lt` to the day. This is the underlying event data every leaderboard is aggregated
from. One day of 2024 returned 4,189 rows.

**(b) The leaderboards.** Season aggregates computed on top of (a) plus tracking data that is
not in (a). Some accept a date range; most do not. See §3.

**(c) The game feed.** `/gf?game_pk={id}` returns about 2.4 MB of JSON per game — the feed
Savant's own game pages use. It carries per-pitch `batSpeed`, `play_id`, expected batting
average, barrel flags and the automated ball-strike challenge markers. It has no fielding or
running measurements, so it adds little over (a) for our purposes.

## 3. What can be dated, and what cannot

| Measurement | Point-in-time? | How |
|---|---|---|
| Bat speed, swing length | **Yes** | per pitch, in the search export (`bat_speed`, `swing_length`) |
| Attack angle, attack direction, swing tilt | **Yes** | per pitch (`attack_angle`, `attack_direction`, `swing_path_tilt`) |
| Contact depth | **Yes** | per pitch (`intercept_ball_minus_batter_pos_x/y_inches`) |
| Pitcher arm angle | **Yes** | per pitch (`arm_angle`) |
| Stance: foot separation, stance angle, box position | **Yes** | not per pitch, but the stance board accepts `dateStart` / `dateEnd` |
| Sprint speed | **No** | season only |
| Outs above average | **No** | season only |
| Arm strength (fielders and catchers) | **No** | season only |
| Pop time, exchange time | **No** | season only |
| Baserunning run value, extra-base rates | **No** | season only |
| Catcher framing, blocking, throwing | **No** | season only |

**Verified, not assumed.** Stanton's 2024 bat speed is 81.21 mph over 715 swings for the full
season and **80.76 over 421 swings** for April through June. Zach Neto's foot separation is
22.14 inches for the season and **22.65** for the same window. The date filter is real on both.
The four fielding and running boards carry no date control at all — I checked each page's form
inputs.

**The row cap matters.** The search export truncates at exactly 25,000 rows. One week of 2024
hit the cap; one day did not. Ingestion has to run day by day — roughly 185 requests a season,
740 for the four-season pool window. At the speeds I measured that is well under an hour.

## 4. The look-ahead exposure we just created

Worth being blunt about this. The physical swing features that landed today (SIM-529) were
loaded as **season aggregates**. For simulating today's games that is correct. **For
backtesting 2025 it is look-ahead**: a bat speed averaged over all of 2025 encodes how the
batter swung in September when we are simulating April.

The same applies to the arm blocks (SIM-530) and to sprint speed, which we have ingested for
years.

Nothing is broken — no backtest has run on this yet, and the closing-line-value re-measure
(SIM-429) is still gated behind the strikeout work (SIM-527). But that re-measure is exactly
where this would bite, so the fix should land before it, not after.

## 5. What I would do about it

**For the batter swing data — take the per-pitch route.** Add the nine tracking columns from
the search export to `raw.pitches`, then compute the physical features as-of-date in the
profile builder the same way every other batter feature is already computed. That removes the
Savant leaderboard from the batter path entirely: no season aggregate, no look-ahead, and one
fewer external dependency. It also brings columns we do not have and would want anyway —
infield and outfield fielding alignment, days since each player's previous game, times through
the order.

The stance columns cannot come from the per-pitch feed. Pull those with `dateStart` /
`dateEnd` instead — one request per (season, cutoff date) rather than per season.

**For the fielding and running measurements — use the previous season, and stop worrying.**
This is the option you called "safest but not best". I measured what it actually costs, using
the data now loaded:

| Measurement | How well the previous season predicts the next |
|---|---|
| Sprint speed | **0.910** |
| Catcher arm strength | **0.894** |
| Fielder arm strength | **0.857** |
| Pop time | 0.726 |
| Extra-base attempt rate against a fielder | 0.546 |
| Fielder arm run value | **0.254** |

The physical traits barely move. Using a runner's 2024 sprint speed to simulate his 2025
season loses almost nothing, because a 0.910 correlation is close to the year-to-year
stability of the measurement itself. The prior-season fallback is not a compromise here; for
speed and arm velocity it is very nearly free.

The bottom row is the interesting one. Fielder arm **run value** repeats at 0.254 — it is
mostly noise. That cuts both ways: the previous season is a poor predictor of it, *and*
using the current season would leak the very outcome we are trying to predict. For a metric
that unstable, the honest choices are a heavily shrunk prior-season value or leaving it out.
It should not be carried at full weight in either direction.

**One lead I did not finish.** The player page embeds a `rangeLine` array — one row per
fielding play with `play_id`, difficulty stars, distance, opportunity time, whether the out
was made, and **`catch_rate`**, the catch probability. That is precisely what outs above
average is computed from, so per-play OAA is reconstructable in principle, and `play_id`
resolves to a game through the game feed. But the array returned the same 116 plays whatever
season parameter I passed, which is well short of a full season of opportunities. It is a
real lead, not a solved source. Worth an hour before accepting season-only OAA.

## 6. The part that needs no Savant at all

Worth stating plainly, because it changes how big this problem is. Everything derived from
pitch and batted-ball events — all 26 of the batter model's original features, the catcher
framing and blocking numbers, the pitcher command features, every pool row — we compute
ourselves from `raw.pitches`. Those are already point-in-time capable: the builder takes a
season list today, and taking a date cutoff instead is a `WHERE game_date <= ...` change, not
a data problem.

The look-ahead question only ever applied to the five Savant-sourced measurement families in
§3. Two of them (the swing data and the stance data) can be fixed completely. Three of them
(speed, arm, fielding value) cannot, and for the two that matter most the prior-season
substitute costs almost nothing.
