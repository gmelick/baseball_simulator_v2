# The betting folder: one place for the odds and the pricing (SIM-645) — plan

Status: PLANNED, 2026-11-21. Owner decisions taken in the session of 2026-11-21 are marked
"decided"; §7 lists the last two. No question is open.

## 1. Why

The odds code lives in five places today: `pipeline/bettingpros/` (8 files),
`pipeline/odds_provider.py`, `pipeline/odds_row_guard.py`, six files in
`pipeline/live/ingestion/`, `pipeline/etl/opening_line_job.py`,
`scripts/load_historical_odds.py`, and `betting/`. The owner wants one folder that follows the
steps of the work, so a reader finds each step in one file.

The review behind this plan found three more problems:

- **The mock odds source makes up prices.** `MockOddsAPI` is the default source when
  `ODDS_PROVIDER` is not set. The API's pricing route fills every market that has no stored line
  with mock prices, and computes edges, expected values and bet signals from them. The Betting
  card hides those markets, but any other reader of the API gets the made-up numbers. The tests
  that use the mock never run the code that reads BettingPros' responses.
- **The three odds callers repeat one another.** The live cycle, the opening-line job and the
  historical loader each fetch every book's rows, drop the rows with no price, run the load
  guard, log the refusals and write the rows. The keep-and-check step is written three times,
  the "does this row have a price" check five times, and the choice of prop markets for a
  pitcher or a batter twice. The historical loader builds a whole live-service object only to
  borrow its two writers.

The plan keeps the folder at two BettingPros files; the HTTP client sits in `fetch_odds.py`
(§7).
- **The opening-line job never runs.** Nothing schedules it (`deploy/ofelia/config.ini` has no
  entry; `schedule_opening_line_job` has no caller). The live service's
  `capture_opening_prop_lines` has no caller either. The 165,395 opening rows in
  `raw.game_odds` came from the historical loader; the newest is from 2026-10-01. BettingPros
  returns the opener with every offer, so the historical loader recovers an opening line at any
  time.

## 2. The target layout (decided)

```
betting/
  markets.py            our market names: the 15 game markets, the 15 prop markets, the
                        pitcher and batter prop sets (today in pipeline/odds_provider.py)
  bettingpros/
    match_games.py      get BettingPros' games and match each to an MLB schedule game
                        (today events.py + schedule.py)
    fetch_odds.py       the HTTP client, BettingPros' market ids, the first-five rules, the
                        offer reading and the row building
                        (today transport.py + markets.py + parsing.py + rows.py + provider.py
                        + base.py)
  validate.py           the load guard (today pipeline/odds_row_guard.py)
  load.py               load_odds: the one core that fetches, keeps, checks and writes the
                        rows; the insert statements and the odds hash; the closing-line stamp
                        (today odds_rows.py + closing_rows.py + the three keep steps)
  history.py            finished games: the nightly two-day pass and the manual backfills
                        (today scripts/load_historical_odds.py)
  pricing.py            de-vig, edge, expected value, the bet signal, and the one
                        price-to-probability formula (today betting/bet_signal.py)
```

What stays outside `betting/`:

- **A small live hook in `pipeline/live/`.** The odds refresh runs inside the game-day loop
  beside the lineups and the feed. The hook keeps the cadence clock and the first-pitch trigger,
  and calls `load_odds` and the closing stamp. Today's `game_odds.py` and `prop_odds.py`
  mixins shrink to that hook.
- **The API routes in `api/routes/`.** The betting routes and the stored-odds reader stay with
  the other routes; they import from `betting/`.

What is deleted:

- `pipeline/etl/opening_line_job.py`, `capture_opening_prop_lines`, and their tests (decided).
- `pipeline/live/ingestion/mock_odds.py`, the mock provider, and the provider registry in
  `pipeline/odds_provider.py` (decided: §4, step B3).
- `pipeline/live/ingestion/odds_routes.py`: `GET /api/odds/{game_pk}` and
  `GET /api/odds/today/all` always serve mock lines, and no screen calls them. This absorbs the
  retire-the-mock-routes ticket (SIM-627).
- `scripts/load_historical_odds.py` becomes `python -m betting.history`.
- The `raw.pipeline_run_log` table stays: the venue backfill also writes it.

## 3. Docker (decided)

`docker-compose.yml` mounts `pipeline/` into `app`, `live` and `jobs`, but not `betting/`. Each
of the three services gets `./betting:/app/betting`, so an edit reaches the containers without
an image rebuild. The nightly closing pass (`scripts/nightly_closing_lines.sh`) calls
`python -m betting.history`, so it reads the mounted copy too.

## 4. The steps

The work lands in two steps on one branch. Step A moves code and changes no behaviour. Step B
changes behaviour. A reviewer can then read the move and the behaviour change apart.

### Step A — the move (no behaviour change)

1. Create the layout of §2. Move each function and constant unchanged into its new file. Merge
   the files that §2 merges; drop the module headers the merge makes redundant.
2. Update every import: about 80 files (35 tests, 21 in `pipeline/`, 14 in `scripts/`, 8 in
   `api/`, 1 in `simulation/`, plus the docs). Import each name from its new home; no shim
   re-exports the old paths (CLAUDE.md §5).
3. Add the three `betting/` mounts. Point `nightly_closing_lines.sh` and the Makefile's
   `load-historical-odds` target at `python -m betting.history`.
4. Proof: the AST comparison (each function's body, docstrings and imports removed) equals the
   old body; the unit lane passes with only import lines changed in the tests.

### Step B — the behaviour changes

**B1. One load core.** `betting/load.py` gets `load_odds(game_pk, line_types, players)`. It
fetches every book's rows, drops the rows with no price, runs `validate.check_row`, logs and
counts each refusal, and writes the rows through the one insert path with the odds hash. One
`has_price(row)` replaces the five checks. One `prop_markets_for(role)` replaces the two role
filters. The live hook and `betting/history.py` both call `load_odds`. `history.py` uses the
writers directly and stops building a `LiveIngestionPipeline`.

**B2. The opening lines come from the nightly pass.** Delete the opening-line job and
`capture_opening_prop_lines`. The nightly pass loads `--line-types opening closing` for the
last two days' games. This takes the opening half of the capture-the-opening-lines ticket
(SIM-625); its venue-backfill half stays on that row.

**B3. No mock source.**
- Delete `MockOddsAPI` and the provider registry. The odds source is BettingPros; a missing
  `ODDS_API_KEY` raises at the first fetch with one plain message. The `ODDS_PROVIDER`
  setting goes (§7).
- The API's pricing route: a market with no injected price and no usable stored row returns
  its simulated probability and fair price with no offered price, no edge, no expected value
  and no bet signal. `odds_source` reads `"none"` for it, in place of `"mock"`.
- The Betting card reads `"none"` where it read `"mock"`; its note is unchanged.
- Regenerate `frontend/openapi.json` in the app image, then run `npm run gen:api`
  (CLAUDE.md §15).
- `tests/conftest.py` stops pinning `ODDS_PROVIDER=mock`. It keeps removing `ODDS_API_KEY`, so
  no test reaches the network.

**B4. Tests on recorded real responses.** `tests/fixtures/bettingpros/` already holds 20
recorded responses from two games. A capture script (`scripts/probes/record_bp_fixtures.py`,
run once in the app container with the key) records more, so the set covers:
- all 30 markets;
- a postponed game and a doubleheader;
- an offer whose two sides name two openers;
- a first-five line that copies the first-inning line, and one that does not;
- a prop market that pages.

A fixture-backed client serves the recorded file for each request URL. The 11 test files that
use the mock move to it, so every odds test runs the real reading and row-building code.

**B5. One price formula.** `pricing.implied_prob_from_american` is the one copy. The guard,
`scripts/accuracy/pricing.py` and `scripts/market_skill.py` import it. Today the guard's copy
returns 1.0 on a price of 0 and the betting copy raises. The guard checks for a price of 0
first and refuses the row, so no stored row changes; the step checks that no stored row has a
price of 0 before it relies on that.

## 5. The checks

- ruff, the format check and mypy over the CI scope (with `betting/` already in it).
- The container unit lane in two halves, then the file index (`python scripts/docs_file_index.py`).
- **A row comparison at scale (CLAUDE.md §2b: hundreds of games, never dozens).** Before Step
  B, run the old historical loader over 300 finished games of 2026 into a scratch copy of the
  two odds tables. After Step B, run `python -m betting.history` over the same games into a
  second scratch copy. The two copies must hold the same rows, odds hashes included.
- A live check on the next game day: the live hook writes current rows and stamps the closing
  rows at first pitch, as before.
- An independent QA pass over the diff.

## 6. Docs and records

- Rewrite from the code: `docs/technical/betting.md` (it becomes the page for the whole
  folder), the Odds section of `pipeline-ingest.md`, the odds rows of `api.md`, `scripts.md`
  and `database.md`, and `code-review-path.md`.
- Regenerate the file index.
- CLAUDE.md §2a's "`betting/` is NOT mounted" caveat changes; §5's repository map gets the
  folder.
- `CHANGES.md` gets the entry. In `BACKLOG.xlsx`: delete the SIM-627 row; narrow the SIM-625 row
  to the venue backfill.
- Rewrite the `ODDS_PROVIDER` lines of the three env templates. The rest of the templates stays
  with the env-template ticket (SIM-632).

## 7. Owner decisions (2026-11-21)

1. **The `ODDS_PROVIDER` setting is removed (decided).** With one source, the setting chooses
   nothing. The key's presence decides whether odds load. Step B3 removes the setting from the
   code, the compose comments and the env templates. The nightly script and the scheduler entry
   drop their `ODDS_PROVIDER=bettingpros` guards; the script still exits 1 when the key is
   missing.
2. **The HTTP client lives in `fetch_odds.py` (decided).** Both BettingPros files call the
   vendor over HTTP with the same retries and cache. `match_games.py` imports the client from
   `fetch_odds.py`, so the folder keeps its two files.

## 8. Out of scope, and stale rows found

- Storing a price that returns to an earlier value (SIM-608) changes the dedup rule; it stays
  its own ticket and lands more easily after B1.
- Two backlog rows describe removed code: the line-movement raise (SIM-609) names
  `line_movement_from_quotes`, and item 7 of the seven small defects (SIM-610) names
  `sharp_consensus`. The betting clean-up (SIM-642) deleted both. Those rows need the owner's
  word to close or trim.
