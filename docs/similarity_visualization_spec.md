# The Similarity Explorer

The Similarity Explorer shows how the similarity engines see a player. You pick a
player-season, and the page shows every other player-season the engine scored against it:
the ranked comps, the spread of the scores, and the sub-scores behind any one comparison.
It reads the engines the app builds at boot. It never touches the simulator, and the
simulator never reads it. The engines themselves are in [Similarity Engines](technical/similarity.md).

The section numbers below are stable: code comments cite §3 (the API contract, its
diagnostic logic and its caching) and §6 (what is out of scope), and a test cites §5.

## 1. What the explorer does

- It ranks the comps of one player-season under one engine and bins every score into a
  histogram. A healthy engine gives a spread of scores around 0.5 with a thin tail of close
  comps. A spike in one bin, or no spread at all, warns that the engine is not separating
  players.
- It breaks one comparison into its sub-scores (for the pitcher: the arsenal and the
  command), so you can see why two players match.
- It finds the historical game states nearest a state you describe (the Situation Finder).

Eight engines give a 0–1 score with sub-scores: pitcher, batter, fielder, catcher,
advancement runner (`baserunner`), steal runner (`baserunner_steal`), pitcher against the
run (`pitcher_steal`) and manager. The other three (situation, pitch-to-pitch, batted ball)
return a distance and are not keyed by a player; only the situation engine has a page.

## 2. Where the code lives

| Part | Code |
|---|---|
| The pitcher distribution route | [api/routes/similarity.py](../api/routes/similarity.py) |
| The explorer routes for every engine | [api/routes/similarity_explorer.py](../api/routes/similarity_explorer.py) |
| The engine builds, the payload cache, the name resolver | [api/state.py](../api/state.py), called from `build_engines` in [api/startup.py](../api/startup.py) |
| The browser client | [frontend/src/api/similarity.ts](../frontend/src/api/similarity.ts) |
| The pages | [EngineIndexPage.tsx](../frontend/src/pages/EngineIndexPage.tsx), [EngineExplorerPage.tsx](../frontend/src/pages/EngineExplorerPage.tsx), [SituationFinderPage.tsx](../frontend/src/pages/SituationFinderPage.tsx), [PlayerDetailPage.tsx](../frontend/src/pages/PlayerDetailPage.tsx) |
| The components | [CompTable.tsx](../frontend/src/components/similarity/CompTable.tsx), [ComponentScoreBars.tsx](../frontend/src/components/similarity/ComponentScoreBars.tsx), [PlayerSearchInput.tsx](../frontend/src/components/similarity/PlayerSearchInput.tsx) |

At boot the lifespan builds the pitcher engine as `app.state.pitcher_engine` and all eleven
engines as `app.state.engines`. A failure of the pitcher build stops the boot; another
engine that fails is logged and left out. `SIMILARITY_ENGINE_ENABLED=false` skips both
builds: the pitcher, query, meta, pair and situation routes then answer 503, and the
catalog lists every engine as not built. The app applies `/data/calibration.json`
to `app.state.engines`. The two pitcher instances score alike, because the pitcher's
calibration rewrites one module-wide value, the arsenal scale.

Every route below requires a login (the session cookie, `X-API-Key` or a Bearer key).

## 3. The API contract

### 3.1 The pitcher distribution route

`GET /api/similarity/pitcher/{pitcher_id}/{season}`

| Query parameter | Range | Default | Meaning |
|---|---|---|---|
| `bins` | 4–100 | 20 | The number of histogram bins. |
| `top_n` | 1–200 | 20 | The length of the ranked list. |

The route asks the pitcher engine for every same-hand pitcher-season (in a worker thread)
and resolves the names from `raw.players`. Without a name resolver it writes
`Pitcher #<id>` and logs a warning. The 200 response:

```json
{
  "query": {"pitcher_id": 694973, "season": 2025, "p_throws": "R", "full_name": "..."},
  "engine": "pitcher_similarity",
  "engine_version": "0.1.0-phase2",
  "population_size": 2412,
  "score_summary": {"min": 0.04, "p25": 0.41, "median": 0.53, "p75": 0.66,
                    "max": 0.94, "mean": 0.52, "std": 0.18},
  "bins": [{"bin_index": 0, "lo": 0.0, "hi": 0.05, "count": 7,
            "preview": ["member", "..."], "members": ["member", "..."]}],
  "top_n": ["member", "..."],
  "diagnostic": {"status": "HEALTHY", "median_target": 0.5, "median_observed": 0.53}
}
```

A member is `{pitcher_id, season, full_name, p_throws, score, arsenal_score,
command_score, sample_pitches}`.

- `population_size` counts the comps: every loaded pitcher-season of the same hand except
  the query's own (the pitcher's other seasons are in).
- `engine_version` is a fixed label.
- `score_summary` is computed over every comp; `std` is the population standard deviation.
- The bins are linear from 0 to 1. Each bin is [lo, hi), except the last, which includes 1.0.
  The route returns every bin, empty ones too. A bin's `members` run highest score first,
  and its `preview` is the first five of them.
- `top_n` is the first `top_n` comps, highest score first.

**Diagnostic logic.** The route classifies the scores in this order:

1. `NO_SPREAD` when the standard deviation is under 0.05.
2. `COLLAPSED` when one bin (at the request's bin count) holds 80% or more of the scores.
3. `HEALTHY` otherwise.

`median_target` is the project-wide target, 0.50; `median_observed` is the comps' median.
The thresholds are the constants `NO_SPREAD_STD_THRESHOLD`, `COLLAPSED_MAX_BIN_FRACTION`
and `DIAGNOSTIC_MEDIAN_TARGET` in `api/routes/similarity.py`.

**Errors.**

- 404 `{"detail": "pitcher not in engine"}`: the engine does not hold that pitcher-season.
- 503 `{"detail": "engine warming"}`: `app.state.pitcher_engine` is not attached.

**Caching.** The route caches the whole payload in Redis:

- The key is `simviz:pitcher:{pitcher_id}:{season}:bins={bins}:top_n={top_n}`
  (`make_cache_key` in `api/state.py`).
- The time to live is 24 hours (`CACHE_TTL_SECONDS`). A pitcher-season's scores change only
  when the nightly rebuild changes the profiles and the app rebuilds its engines. Redis
  outlives an app restart, so a cached payload can lag that rebuild by up to 24 hours (§6).
- The route reads the cache first and writes through on a miss. A value that does not
  decode reads as a miss. A failed write logs a warning and the route still answers.
- With no cache attached, the route skips the cache.

This route is the only cached one. The explorer routes of §3.2 query the engine on every
call.

### 3.2 The explorer routes for every engine

| Route | What it returns |
|---|---|
| `GET /api/similarity/engines` | The catalog: one entry per engine with its kind (`score` or `distance`), method, partition, sub-score count, build status and profile count. A score engine counts as built when it holds profiles. |
| `GET /api/similarity/{engine}/meta` | The engine's sub-score fields, labels and weights, its sample field and minimum sample, and whether it takes `position` or `vs_hand`. |
| `GET /api/similarity/{engine}/query` | The ranked comps and the histogram of one subject: `entity_id`, `season` (2015–2027), `position` (required for the fielder), `vs_hand` (`L` or `R`; batter only), `bins` (4–100, default 20), `top_n` (1–200, default 25). |
| `GET /api/similarity/{engine}/pair` | One comparison, `a_id`/`a_season` against `b_id`/`b_season`, with every sub-score. |
| `POST /api/similarity/situation/query` | The `k` (default 25) historical plate appearances nearest a game state, with their distance, game, inning, outs, runners, leverage and score difference. |

The query and pair routes reuse the pitcher route's helpers (`_BinSpec`,
`compute_score_summary`, `classify_diagnostic`), so the bins and the diagnostic follow §3.1.
A member is `{entity_id, season, name, score, sub_scores, sample, below_min_sample, extra}`.
`below_min_sample` compares the comp's sample with the engine's minimum. A sub-score the pair
could not score (a steal runner's lead or a pitcher's hold missing on one side) is left out
of `sub_scores`.

The composite is not the weighted sum of the sub-scores. Every score engine multiplies by
the pair confidence (a discount for a thin sample on either side), and the batter engine
also applies its bats penalty. The response carries the weights and each comp's sample, so
the page can explain the gap.

Each engine's fields come from an `EngineAdapter` in `SCORE_ADAPTERS`:

| Engine | Id field | Sample field (minimum) | Sub-scores (weight) |
|---|---|---|---|
| `pitcher` | `pitcher_id` | `sample_pitches` (200) | arsenal 0.65, command 0.35 |
| `batter` | `batter_id` | `sample_pa` (100) | discipline 0.40, batted ball 0.35, platoon 0.15, power 0.10; with `vs_hand` 0.30 / 0.25 / 0.35 / 0.10 |
| `fielder` | `player_id` | `sample_batted_balls` (50) | range, then double play / errors / specialty (infield, 0.45 / 0.30 / 0.15 / 0.10) or arm / star plays / errors (outfield, 0.40 / 0.30 / 0.15 / 0.15) |
| `catcher` | `catcher_id` | `sample_pitches_received` (300) | framing 0.53, blocking 0.24, throwing 0.14, deterrence 0.09 |
| `baserunner` | `player_id` | `sample_advancement_opps` (20) | speed 0.35, aggression 0.40, success 0.25 |
| `baserunner_steal` | `player_id` | `sample_steal_attempts` (10) | tendency 0.45, lead 0.45, success 0.10 |
| `pitcher_steal` | `pitcher_id` | `sample_baserunner_events` (30) | outcome 0.35, hold 0.65 |
| `manager` | `manager_id` | `sample_games` (50) | bullpen usage 0.40, aggression 0.35, platoon 0.25 |

The batter row lags the engine; see §6.

**Errors.** A distance engine on a score route answers 404 with a pointer to the Situation
Finder or the Data Lab. An unknown engine answers 404. An engine that is not built answers
503. A fielder query without `position` answers 400. A subject with no profile answers 404;
a subject with a profile but no comparable population answers 200 with `population_size` 0.
The situation route answers 503 when the situation engine is not built or its index is not
ready.

## 4. The pages

The pages are React components under `frontend/src/pages/`. They call only the §3.2 routes;
no page calls the pitcher distribution route of §3.1.

- **`/similarity`, the engine chooser** (`EngineIndexPage`). It lists the eight score engines
  with a build badge (the profile count, or "warming") and the three distance engines as
  "pattern finder". A score engine links to its explorer, the situation engine to the
  Situation Finder, the other two to the Data Lab summary.
- **`/similarity/:engine`, the explorer** (`EngineExplorerPage`). A name type-ahead
  (`PlayerSearchInput`, over `/api/players/search`), a season picker (2015–2027), a position
  picker for the fielder (default 2B) and a platoon picker for the batter. The choices live
  in the URL (`subject=<id>-<season>`, `pos`, `vs`, `compare=<id>-<season>`), so a link can
  be shared. The page asks for 20 bins and the top 50. It shows:
  - the score histogram, with the median, the 75th percentile, the maximum and the
    diagnostic status;
  - the engine's method, partition and note;
  - the comps table (`CompTable`): rank, player (a link to `/player/:id`), season, the
    composite bar, a small bar per sub-score and the sample size.
  A row click opens a drawer with the pair route's answer: a radar chart when the engine has
  three or more sub-scores, and `ComponentScoreBars` (one bar per sub-score, a tick at its
  weight, and a note that the composite carries the confidence discount). A 503 shows the
  "engine warming" state; a 404 shows "No profile for this subject".
- **`/similarity/situation`, the Situation Finder** (`SituationFinderPage`). A form for the
  eleven state features and a table of the nearest plate appearances, labelled as a
  distance (lower means more similar), not a score.
- **`/player/:playerId`, the player hub** (`PlayerDetailPage`). The player's identity and
  seasons, and a card per engine that applies to him, each linking to the explorer seeded
  with his latest season.

## 5. What the tests check

- [test_similarity_endpoint.py](../tests/unit/test_similarity_endpoint.py): the bin edges
  (the last bin includes 1.0), the score summary, the bins and their previews, the top-N
  cut, the three diagnostic classes, the route's payload shape, the 404 for an unknown
  pitcher and the 503 when no engine is attached. A stub engine stands in; no database runs.
- [test_similarity_explorer_adapters.py](../tests/unit/test_similarity_explorer_adapters.py):
  each adapter's id, sample, sub-score and extra fields exist on its engine's result
  dataclass; the fielder labels flip between infield and outfield; `vs_hand` re-weights the
  batter.
- [test_api_state.py](../tests/unit/test_api_state.py): the cache key format and the cache's
  round trip, time to live and treatment of a bad value.

## 6. Out of scope and known gaps

- **No cache invalidation.** The nightly rebuild does not delete the `simviz:*` keys. A
  cached payload lives out its 24 hours.
- **The batter adapter lags the engine.** The engine scores five sub-scores (the physical
  swing and stance group at 0.20; weights 0.32 / 0.28 / 0.12 / 0.08 / 0.20). The explorer's
  adapter still lists four sub-scores with the older weights and shows no physical bar.
- **No page for the pitch-to-pitch and batted-ball engines.** They are keyed by a pitch or a
  batted ball, not a player.
- **No cross-engine view** (a comp under two engines at once), no career view of a comp, and
  no backtest overlay of how a comp's real results matched.
