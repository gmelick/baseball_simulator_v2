# Betting

The `betting/` folder holds everything about the market's prices. It gets the odds from
BettingPros (the odds vendor), checks each row, stores it, and compares the simulator's
probabilities with the stored prices. Each step of the work has one file.

| Step | File |
|---|---|
| Our market names and book labels | [`betting/markets.py`](../../betting/markets.py) |
| 1. Match an MLB game to its BettingPros event | [`betting/bettingpros/match_games.py`](../../betting/bettingpros/match_games.py) |
| 2. Fetch the odds and build one row per book | [`betting/bettingpros/fetch_odds.py`](../../betting/bettingpros/fetch_odds.py) |
| 3. Validate each row (the load guard) | [`betting/validate_odds.py`](../../betting/validate_odds.py) |
| 4. Load the kept rows, and stamp the closing line | [`betting/load_odds_to_db.py`](../../betting/load_odds_to_db.py) |
| 5. Load the finished games (the nightly pass and the backfills) | [`betting/historical_loader.py`](../../betting/historical_loader.py) |
| 6. Price the markets: no-vig probability, edge, expected value, bet signal | [`betting/pricing.py`](../../betting/pricing.py) |

The odds tables are on the [database page](database.md#odds). The betting routes are on
the [API page](api.md#the-betting-routes).

## The terms

| Term | Meaning |
|---|---|
| **Book** | A sportsbook, a fantasy app, an exchange or a prediction market that posts prices. A stored row names its book as `bp:<id>`, the BettingPros id. `bp:0` is the vendor's blend of every book: it is stored and never graded. |
| **Line type** | `opening` (the first price a book posted), `current` (the price now) or `closing` (the last price before first pitch). |
| **American odds** | The book's price. `-110` means stake 110 to win 100. `+150` means stake 100 to win 150. |
| **Decimal odds** | The total return per unit staked. `+150` is 2.50. |
| **Implied probability** | The probability a price stands for, with the book's margin still in it. `-110` implies 0.5238. |
| **Vig** | The book's margin. A market's implied probabilities add up to more than 1; the excess is the vig. |
| **No-vig probability** | The implied probabilities scaled so the sides add up to exactly 1. Removing the vig is the "de-vig". |
| **Edge** | The simulator's probability minus the no-vig probability. Positive: the simulator rates the side higher than the market. |
| **Expected value (EV)** | The profit per unit staked if the simulator is right: `EV = p × b − (1 − p)`, where `b` is the profit per unit on a win at the OFFERED price. |
| **Fair price** | The American price of a probability with no vig. The report shows the simulator's fair price next to the offered price. |
| **Graded book** | The first book on `GRADED_BOOK_PREFERENCE` that has a row. Its row gives the no-vig probability. |

The edge reads the no-vig probability. The EV reads the offered price, because a bet
takes that price. Mixing the two up counts the vig twice or not at all.

The folder computes no closing line value (CLV: how a bet's price compares with the
closing price). The model is not judged by it (owner ruling 2026-09-28).

## How it fits together

**Getting the odds in.**

1. `betting.load_odds_to_db.odds_source()` builds the one odds source, `BettingProsOddsProvider`. It
   reads the vendor key from `ODDS_API_KEY`. Without the key, every BettingPros read fails
   with one plain message; the provider logs it, counts it in `read_failures` and returns
   no rows.
2. The provider matches the MLB game to its BettingPros event (`match_games.py`). It reads
   the MLB schedule for the official date, the teams and the start, then picks the event.
3. For one market and line type, the provider reads the event's offers and builds one row
   per book (`fetch_odds.py`). A closing or current row is written for each book that prices
   every side. An opening row is written only for the opener: the one book that opened
   every side.
4. `betting.load_odds_to_db` drops a row with no price (`has_price`), runs the load guard on the rest
   (`validate_odds.check_row`), logs and counts each refusal, and writes the kept rows of one
   offer in one `executemany`. The odds hash skips a row already stored.
5. Two callers run steps 3 and 4.
   * The live service (`pipeline/live/ingestion/`) loads the current lines of every
     watched game once a minute. It fetches on its vendor thread (`game_offers`,
     `prop_offers`) and writes on its event loop (`write_offers`). At first pitch it stamps
     each book's last pre-pitch row as `closing` (`promote_closing_game_rows`,
     `promote_closing_prop_rows`).
   * The historical loader (`python -m betting.historical_loader`) calls `load_odds` for each Final
     game. The scheduler's `nightly-closing-lines` job
     ([`operations/jobs/nightly_closing_lines.sh`](../../operations/jobs/nightly_closing_lines.sh)) runs it
     at 10:30 UTC over the games of the last two days, for the opening and the closing
     lines. Nothing else stores an opening line.

**Pricing.**

1. The game page's Betting card calls `GET /api/betting/games/{game_pk}/edges` and
   `.../signals` ([betting.ts](../../frontend/src/api/betting.ts)).
2. The route (`api/routes/betting.py`) runs or reuses a simulation of the game.
3. `api/routes/betting_pricing.py` finds each market's prices: the injected query
   parameters, else the stored lines (`api/routes/betting_stored_odds.py`). A market with
   neither gets no report, and its `odds_source` reads `none`.
4. For each side, the pricing code calls one report builder. The builder turns the
   simulator output into a probability, de-vigs the prices and returns an `EdgeReport`.
5. `/edges` returns the reports. `/signals` passes them to `bet_signals_from_edges`.

The accuracy comparison (`scripts/accuracy/`) uses the same pricing functions offline,
against the closing lines of past games.

**Docker.** `docker-compose.yml` mounts `./betting` into the `app`, `live` and `jobs`
services, so an edit reaches them without an image rebuild.

## The modules

### `betting/__init__.py`

[Source](../../betting/__init__.py) · 10 lines

The package docstring only: one line per module. Import every name from its own module.

### `betting/markets.py`

[Source](../../betting/markets.py) · 453 lines · Tests: [test_odds_vocabulary.py](../../tests/unit/test_odds_vocabulary.py), [test_odds_game_markets.py](../../tests/unit/test_odds_game_markets.py), [test_odds_book_vocabulary.py](../../tests/unit/test_odds_book_vocabulary.py), [test_odds_readers.py](../../tests/unit/test_odds_readers.py)

The names every odds reader and writer shares. The module is constants and small pure
functions.

| Name | What it does | Called from |
|---|---|---|
| `PROP_STATS` / `PITCHER_PROP_STATS` / `BATTER_PROP_STATS` | The fifteen prop markets: five pitcher markets and ten batter markets. `hits` is a batter market; `hits_allowed` is a pitcher market. | `fetch_odds.py`, `load_odds_to_db.py`, `historical_loader.py`, migration 0022 |
| `PROP_STAT_TO_MODEL_PROP` | Each market's name in the simulator's prop output (`strikeouts` → `K`). | `scripts/probes/rescore_graded_book.py` |
| `PROP_ROLE_PITCHER` / `PROP_ROLE_BATTER` / `PROP_ROLE_BOTH` | A player's role. `both` is the safe default for an unknown role or a two-way player. | `historical_loader.py`, `pipeline/live/ingestion/prop_odds.py` |
| `prop_markets_for(role, prop_stats)` | The markets a player in that role is asked for. | `load_odds_to_db.prop_offers` |
| `GAME_MARKET_TYPES` / `FULL_GAME_MARKET_TYPES` | The fifteen game markets; the three full-game ones (`moneyline`, `runline`, `total`). | the routes, the loaders, `scripts/accuracy/`, `simulation/game_market_distributions.py` |
| `GAME_MARKET_KIND` / `GAME_MARKET_SEGMENT` / `GAME_MARKET_SIDE` / `GAME_MARKET_NAMES` | Per market: the kind (`moneyline`, `three_way`, `runline`, `total`, `yes_no`), which says the columns it fills; the segment (full game, first inning, first five); the team of a team total; the display name. | the same, `validate_odds.py` |
| `GAME_ODDS_FIELDS` | The price and line columns of a game row. | `fetch_odds.py`, `load_odds_to_db.has_price`, `validate_odds.py` |
| `BOOK_NAMES` / `BOOK_KIND` / `BOOK_IDS_BY_NAME` / `BETTABLE_KINDS` | The books by id, their kind (sportsbook, fantasy app, exchange, prediction market, blend) and their short names. Only a sportsbook is bettable. | the `book_*` helpers, `historical_loader.py` |
| `book_label` / `book_id_from_label` / `book_kind` / `is_bettable` / `book_display_name` / `resolve_book` | Move between a book id, its stored label `bp:<id>`, its kind and its display name. `resolve_book` reads an id, a label or a short name (`draftkings`). | `fetch_odds.py`, `load_odds_to_db.py`, `historical_loader.py`, the betting routes, `scripts/accuracy/`, the probes |
| `GRADED_BOOK_PREFERENCE` / `graded_book_labels` / `graded_row_order_sql` | The graded book order (DraftKings first), as ids, as labels and as an SQL `ORDER BY` tail. | `fetch_odds.get_odds`, `api/routes/betting_stored_odds.py`, `betting_card.py`, `scripts/accuracy/pricing.py`, the probes |
| `bettable_labels` | The bettable books' labels. | `betting_stored_odds.py`, `scripts/accuracy/pricing.py`, the probes |
| `STORED_BOOK_FILTER_SQL` | `book LIKE 'bp:%'`: a reader keeps only one-book rows, because an old mixed `consensus` row may still exist. | `betting_stored_odds.py`, `historical_loader.py`, `scripts/accuracy/pricing.py`, the probes |
| `ODDS_ROW_VERSION` | The stamp of the one-book-per-row format (`sim555.2`) that an accuracy report records. | `scripts/accuracy/pricing.py`, the probes |

**Watch out:** a new prop market goes in three places together: the tuples here, a
migration that widens the `raw.prop_odds` CHECK, and the BettingPros market id in
`fetch_odds.py`. An import-time guard in `fetch_odds.py` and the vocabulary test fail when
one is missing.

### `betting/bettingpros/match_games.py`

[Source](../../betting/bettingpros/match_games.py) · 754 lines · Tests: [test_bettingpros_provider.py](../../tests/unit/test_bettingpros_provider.py), [test_odds_matcher.py](../../tests/unit/test_odds_matcher.py)

Finds BettingPros' event for an MLB game. The module also holds the HTTP client every
BettingPros read uses. Each class is a mixin (a class that adds methods to the provider
class): `EventMatchMixin` extends `TransportMixin`, which extends `ProviderBase`. Every part
logs as `betting.bettingpros`.

| Name | What it does | Called from |
|---|---|---|
| `ProviderBase` | Type declarations of the provider's settings and caches, so every mixin sees them. | `fetch_odds.BettingProsOddsProvider` |
| `TransportMixin._http_get_json` | One GET with the retry policy. It retries a time-out, an HTTP 429 or a 5xx, waits twice as long each time (at most 60 s, a `Retry-After` included), and removes the key from an error message. | `_bp_get`, `_mlb_get`; `scripts/probes/odds_row_compare.py` records and replays it |
| `TransportMixin._bp_get` / `_mlb_get` | The two network seams the tests stub: a BettingPros v3 read (it raises at once with no key) and an MLB Stats API read. | every read |
| `TransportMixin.set_retry_policy` | Sets the retries and the first wait. | the provider's constructor, `historical_loader.py` |
| `_read_schedule` / `_ScheduleInfo` / `_is_postponed` | The schedule facts of one game from `schedule?gamePk=`: the official date, the teams, the start, the double-header number, the postponed start. The played entry wins over a postponed one. | `_resolve_game_meta` |
| `EventMatchMixin._resolve_game_meta` | Game → (official date, home name, away name, UTC start). The date is `officialDate`, never the UTC `gameDate`. | `_resolve_event`, the row builders |
| `EventMatchMixin._resolve_event` / `_match_by_start` / `_match_game_two` | Game → its BettingPros event. With a known start, the nearest event of the same teams on that date wins. Game 2 of a straight double-header has a placeholder start, so it takes the later event. | `fetch_odds` row builders |
| `forget_game` / `forget_failed_lookups` | Drop one game's cached facts; drop every stored failed lookup. | `pipeline/live/ingestion/game_odds.py`; `historical_loader.py` before each game |
| `_postponed_start` | A made-up game's original start, so the guard can refuse a row that priced the game not played. | `fetch_odds` row builders |
| `_parse_utc` / `_normalize_name` | Parse a timestamp from either source; the name key (lower case, no accents or punctuation). | this module, `fetch_odds.py` |

A found game or event stays cached for the process lifetime. A failed lookup stays for
one cache lifetime (`ODDS_OFFERS_CACHE_TTL_S`). A read that raised adds 1 to
`read_failures`; a normal absence (no event, no offer) does not.

### `betting/bettingpros/fetch_odds.py`

[Source](../../betting/bettingpros/fetch_odds.py) · 1,091 lines · Tests: [test_bettingpros_provider.py](../../tests/unit/test_bettingpros_provider.py), [test_odds_one_book_per_row.py](../../tests/unit/test_odds_one_book_per_row.py), [test_odds_game_markets.py](../../tests/unit/test_odds_game_markets.py), [test_odds_provider_retry.py](../../tests/unit/test_odds_provider_retry.py), [test_odds_provider_pin.py](../../tests/unit/test_odds_provider_pin.py), [test_odds_census_sql.py](../../tests/unit/test_odds_census_sql.py)

Gets BettingPros' odds for a game and builds one row per book. The provider class is
`BettingProsOddsProvider(OfferRowsMixin, ProviderBase)`; `OfferRowsMixin` extends
`EventMatchMixin`.

| Name | What it does | Called from |
|---|---|---|
| `_GAME_MARKET_IDS` / `_PROP_MARKET_IDS` | Our market name → the vendor's market id. Two import-time guards fail when the keys differ from `GAME_MARKET_TYPES` or `PROP_STATS`. | the row builders |
| `BettingProsOddsProvider.get_odds_by_book` | One row per book for one game market and line type. An unknown market raises `ValueError`; a game with no match gives `[]`. | `load_odds_to_db.game_offers` |
| `BettingProsOddsProvider.get_prop_odds_by_book` | One row per book for one player's prop market. | `load_odds_to_db.prop_offers` |
| `BettingProsOddsProvider.get_odds` | The graded book's row of one market, else the first other sportsbook's, else a row of `None` prices. The live service broadcasts it and never stores it. | `pipeline/live/ingestion/game_odds.py` |
| `OfferRowsMixin._rows` | One row per book for a market's sides. `opening`: at most one row, the opener's; a tie joins only when the same book opened it. Another line type: a row for each book with a usable line on every required side. `book_line_at` is the newest stamp of the row's sides. | `_game_market_rows`, `_prop_market_rows` |
| `OfferRowsMixin._game_market_rows` / `_prop_market_rows` | The rows of one game market or one player's prop market, with `source = 'bettingpros'`, the scheduled start, the official date and the postponed start for the guard. | the three reads |
| `OfferRowsMixin._offers` / `_fetch_offers_all_pages` | Every offer of one (event, market), every page; only a complete market is cached. | the row builders |
| `OfferRowsMixin._resolve_player_name` | MLB player id → the name key the vendor's offers carry. | `_find_player_offer` |
| `F5_TWIN_MARKETS` / `twin_price_tolerance` / `_f5_opener_is_twin` / `_f5_exclusions` | The twin rule: a book's first-five line that copies its own first-inning line is a first-inning bet, so its row is dropped. The gap is 0.03 implied, 0 for the moneyline. A failed first-inning read drops the first-five rows. | `_game_market_rows`; `scripts/probes/odds_book_census.py` |
| `F5_EXCLUDED_BOOKS` / `F5_EXCLUDED_MARKETS` / `is_f5_dated_excluded` | The dated rule: DraftKings' first-five run line and total from 2025-03-01 are first-inning bets. | the same |

**Flags:** `ODDS_API_KEY` (the vendor key). `ODDS_OFFERS_CACHE_TTL_S` (default 30 seconds,
half the live cadence; 0 turns the caches off). `BETTINGPROS_MAX_RETRIES` (default 0) and
`BETTINGPROS_RETRY_WAIT_S` (default 2.0).

**Watch out:**

* Leave `BETTINGPROS_MAX_RETRIES` unset in the app and the live service. A retry waits with
  `time.sleep`, which stalls the live service's vendor thread. Only the historical loader
  turns retries on (`--retries`).
* The provider reads every price as a float (`-110.0`). The closing stamp's hash
  (`load_odds_to_db.closing_hash`) depends on that typing.

### `betting/validate_odds.py`

[Source](../../betting/validate_odds.py) · 426 lines · Tests: [test_odds_one_book_per_row.py](../../tests/unit/test_odds_one_book_per_row.py), [test_odds_writers.py](../../tests/unit/test_odds_writers.py), [test_odds_closing_rows.py](../../tests/unit/test_odds_closing_rows.py), [test_odds_book_census.py](../../tests/unit/test_odds_book_census.py)

The load guard: it refuses a row that no single book could post. The module does no I/O.

| Name | What it does | Called from |
|---|---|---|
| `check_row(row)` | Returns a `Refusal` (a rule and a plain reason) or `None`. The checks run in the order of `RULES`. | `load_odds_to_db.keep_rows`, `api/routes/betting_stored_odds.py`, `scripts/probes/odds_book_census.py` |
| `RULES` | The 14 rules: `zero_price` (BettingPros sends 0 for a price a book does not offer); a missing side; run-line spreads of two sizes; equal spreads priced like a pair; a first-five "+0.5 / +0.5" pair priced like a first-inning tie; an over and an under at two lines; a first-five total at a first-inning line; a placeholder team-total line; a first-five tie at a first-inning price; three three-way rules (a sum below 0.98, team prices that refund a tie, a sum above 1.18); a closing row stamped more than 15 minutes after the start; a price stamped before a postponed game's original start. | `check_row`, `RefusalTally.summary` |
| `RefusalTally` | Counts the rows offered and the refusals by rule, market and book; `summary()` prints them; `warn_if_share_above` warns past a share. | `load_odds_to_db.keep_rows`, `historical_loader.py`, `pipeline/live/ingestion/base.py` |
| `CLOSING_STAMP_GRACE` | 15 minutes: how late a closing row may be stamped. | `check_row`, `load_odds_to_db.py` (the closing stamp) |

The guard reads prices through `betting.pricing.implied_prob_from_american`, the one price
formula. `zero_price` runs first, so the formula never sees a price of 0.

### `betting/load_odds_to_db.py`

[Source](../../betting/load_odds_to_db.py) · 796 lines · Tests: [test_odds_writers.py](../../tests/unit/test_odds_writers.py), [test_odds_closing_rows.py](../../tests/unit/test_odds_closing_rows.py), [test_odds_last_seen.py](../../tests/unit/test_odds_last_seen.py), [test_odds_loader_retry.py](../../tests/unit/test_odds_loader_retry.py), [test_odds_one_book_per_row.py](../../tests/unit/test_odds_one_book_per_row.py)

The one load path, the insert statements, the odds hash and the closing stamp.

| Name | What it does | Called from |
|---|---|---|
| `odds_source()` | A new `BettingProsOddsProvider`, the one odds source. | `historical_loader.py`, `pipeline/live/ingestion/base.py`, `live_ingestion_pipeline.py` |
| `has_price(row)` | True when a game or prop row has at least one price or line. A row with none is skipped with no log line. | `game_offers`, `prop_offers` |
| `game_offers` / `prop_offers` | Every book's priced rows, one offer per (line type, market) or per (player, market, line type). A fetch that raises is logged, counted under `fetch`, and the next market runs. These block on the network. | `load_odds`; the live cycles on their vendor thread |
| `keep_rows` | The rows of one offer the guard keeps; logs each refusal at INFO and counts it. `book_id` keeps one book. | `write_offers` |
| `write_offers` | Checks and writes each offer, one `executemany` per offer. A write that raises is logged, counted under `write`, and the next offer runs. Returns the rows sent. | `load_odds`; the live cycles |
| `load_odds(conn, provider, game_pk, ...)` | Fetch, keep, check and write one game's odds. `game_markets=None` skips the game markets; `roles=None` skips the props. | `historical_loader.py` |
| `insert_game_odds_rows` / `insert_prop_odds_rows` / `game_odds_insert_args` / `prop_odds_insert_args` | The two INSERTs, `ON CONFLICT ... DO NOTHING` on the odds hash. With `restamp`, a row the book still posts gets `last_seen_at = NOW()` instead (Alembic 0029). | `write_offers` |
| `_odds_hash` / `_prop_odds_hash` | The SHA-256 of the row's book, line type, market and prices. The vendor stamp is not in it, so an unchanged price fetched again deduplicates. | the insert arguments; `scripts/probes/backfill_odds_hash.py` |
| `_stamp_param` | A row's stamp as an aware UTC datetime. | the insert arguments, `pipeline/live/ingestion/odds_cycle.py` |
| `promote_closing_game_rows` / `promote_closing_prop_rows` | At first pitch: per (market, book), or per (player, prop, book), the latest pre-pitch `current` row stamped no later than the start plus 15 minutes becomes `closing`. A key that holds a closing row is done, so a second call promotes nothing. | `pipeline/live/ingestion/game_odds.py`, `prop_odds.py` |
| `closing_hash` / `closing_candidates` | The promoted row's new hash is the hash of the same row as `closing`, so the nightly loader's identical row deduplicates against it. | `_promote_closing_rows` |

**Watch out:** when a row with the new closing hash already exists, the loader got there
first, and the live row stays `current`. A key with no closing row at first pitch (a book
that first posts in play) can take an in-play row on a later promotion.

### `betting/historical_loader.py`

[Source](../../betting/historical_loader.py) · 871 lines · Tests: [test_historical_odds_load.py](../../tests/unit/test_historical_odds_load.py), [test_odds_writers.py](../../tests/unit/test_odds_writers.py), [test_odds_loader_retry.py](../../tests/unit/test_odds_loader_retry.py), [test_odds_game_markets.py](../../tests/unit/test_odds_game_markets.py), [test_odds_vocabulary.py](../../tests/unit/test_odds_vocabulary.py)

Loads the odds of finished games. By default it picks only the Final games of the seasons
that are missing odds: a game with no stored row at one of the run's line types. An
opening row in either odds table counts, because only the opener book writes one. Props
count only for a game with lineup rows. `--reload` picks every Final game. For each game
it calls `load_odds`: the game markets, then the props of every player in
`raw.game_lineups` (a pitcher gets the pitcher markets, every other player the batter
markets).

| Option | What it does |
|---|---|
| `--seasons`, `--max-games` | Select the games. |
| `--reload` | Load every Final game of the seasons, not only the games missing odds. |
| `--line-types` | Any of `opening`, `current`, `closing`; default `opening closing`. |
| `--prop-stats`, `--game-markets`, `--no-props`, `--no-game-odds` | Narrow the markets. An unknown value stops the run before any fetch. |
| `--book NAME` | Load one book's rows (a smoke run or a top-up). |
| `--done-file PATH` | Skip the games the file lists; add each complete game to it. |
| `--retries N`, `--retry-wait S` | The vendor-read retry policy (default 3 retries, the first after 5 s). |

| Name | What it does | Called from |
|---|---|---|
| `run(args)` | The load: build the source, select the games, load each, then log the guard's summary, the rows per book and the incomplete games. Returns the exit code. | `main` |
| `_missing_odds_clause` | The SQL that keeps only the games missing odds, for the run's line types, tables, book and markets. | `run` |
| `_fetch_final_games` | The Final games of the seasons, with the missing-odds filter. | `run` |
| `_fetch_lineup_roles` | Each lineup player's role from `raw.game_lineups`. | `run` |
| `_incomplete_reasons` / `_incomplete_summary` | A game is incomplete when the provider's `read_failures` grew or the loader caught a fetch or write failure; one line names up to 20 such games. | `run` |
| `read_done_file` / `append_done` | Read and append the done-file, forced to disk. | `run`; `scripts/probes/backfill_start_positions.py` |
| `_configure_offline_cache` | Sets `ODDS_OFFERS_CACHE_TTL_S` to 600 s when unset, before the provider is built. | `run` |

**Exit codes:** 0 when every game is complete; 1 when a game is incomplete (its rows so
far stay written, and the done-file leaves it out); 2 on a usage error.

Run: `make load-historical-odds FLAGS="--seasons 2024"`, or
`python -m betting.historical_loader --seasons 2024` in the app container. Without
`ODDS_API_KEY`, every read fails and every game is incomplete.

### `betting/pricing.py`

[Source](../../betting/pricing.py) · 611 lines · Tests: [test_bet_signal_pricing.py](../../tests/unit/test_bet_signal_pricing.py), [test_bet_signal_run_line.py](../../tests/unit/test_bet_signal_run_line.py), [test_bet_signal.py](../../tests/unit/test_bet_signal.py), [test_segment_edges.py](../../tests/unit/test_segment_edges.py), [test_live_run_lines.py](../../tests/unit/test_live_run_lines.py)

The pricing math. It is plain arithmetic: no database, no random numbers and no odds
fetch. The caller passes the prices in. The module has seven sections, in this order.

**1. The conversions.**

| Name | What it does | Called from |
|---|---|---|
| `american_to_decimal` | American odds to decimal odds. | `expected_value`, `stake_fraction`; `betting_stored_odds.py`; `scripts/accuracy/pricing.py`, `hypothetical_return.py` |
| `implied_prob_from_american` | A price's implied probability, vig included. The one copy of the formula; a price of 0 raises. | `devig`, `devig_one_sided`, `reference_margin_from_prices`; `validate_odds.py`; the twin rule in `fetch_odds.py`; `scripts/accuracy/pricing.py`, `scripts/market_skill.py`, the census probe |
| `prob_to_american` | A probability's fair price. Raises on 0 or 1. | `edge_report` (the report's `sim_fair_american`); `scripts/accuracy/grading.py` (to skip a 0 or 1 probability) |

**2. The de-vig.**

| Name | What it does | Called from |
|---|---|---|
| `devig(american_prices)` | The no-vig probabilities of two or more prices: `q_i / Σ q`. Two prices for a two-way market, three for a market with a tie. | every two-way builder, `three_way_edge_report`; `betting_pricing.py`; `scripts/accuracy/grading.py`; the probes |
| `devig_one_sided(american, margin)` | The no-vig probability of a bet with no opposite side: its implied probability over another market's margin, capped at 1. | `betting_pricing.py`, `scripts/accuracy/grading.py` |

**3. The expected value.** `expected_value(p_sim, offered_american)` is the EV per unit
staked. It is used by `edge_report`, `stake_fraction`, `betting_pricing.py` and
`scripts/accuracy/hypothetical_return.py`.

**4. The records.**

| Name | What it does |
|---|---|
| `MarketSide` | HOME, AWAY, OVER, UNDER, DRAW (the tie). |
| `OddsQuote` | A two-way market's prices: `side` (the side being priced), `other` and `line`. |
| `EdgeReport` | One side of one market: the simulator's probability, the no-vig probability, the edge, the EV, the offered price and the simulator's fair price. |
| `edge_report(...)` | Builds an `EdgeReport` from the simulator's probability, the no-vig probability and the offered price. Every builder ends here; `betting_pricing.py` calls it for a separate run-line bet and the first-to-score moneyline. |

**5. The report builders.** Each one reads a different simulator output.

| Name | Simulator output | Called from |
|---|---|---|
| `moneyline_edge_report` | the win probability | `betting_pricing.py`, `scripts/accuracy/grading.py` |
| `prop_edge_report` | a player's prop distribution | `api/routes/games.py`, `scripts/accuracy/grading.py` |
| `samples_over_under_edge_report` | one number per simulated game: the game total (label `total`), the first-five runs, a team's runs, the first-inning runs | `betting_pricing.py`, `scripts/accuracy/grading.py` |
| `three_way_edge_report` | the home, away and tie probabilities of the first-inning and first-five moneylines | `betting_pricing.py` |

**The push.** Scores are whole numbers. On a half-integer line no game lands on the line,
so the two sides add up to 1. On a whole-number line a game on the line is a push and
counts for neither side.

**6. The run line.** The book lists a run line in one of two ways.

* **A pair:** one bet with two sides (home −1.5 / away +1.5). The away spread is the
  negative of the home spread. The two prices de-vig against each other
  (`run_line_edge_report`).
* **Two separate bets:** the spreads share a sign (home −1.5 / away −1.5). Both can lose
  on a one-run game, so they must never de-vig against each other. Each bet is priced
  from its own price over the margin of another two-way market of the same game
  (`devig_one_sided`). The caller lists the reference markets in order: the segment's
  total, the full-game total, the moneyline. A margin outside `REFERENCE_MARGIN_BAND`
  (1.00 to 1.15) is skipped. With none usable, `DEFAULT_ONE_SIDED_MARGIN` (1.05) applies.
  A three-way market is never a reference, because its margin also covers the tie.

| Name | What it does | Called from |
|---|---|---|
| `spread_cover_prob(margins, line, side)` | The cover probability of a side. `line` is always the HOME line. | `run_line_edge_report`, `run_line_bet_cover_prob`; `simulation/game_market_distributions.py`; `scripts/accuracy/` |
| `run_line_bet_cover_prob(margins, side, own_line)` | The cover probability of one bet at its OWN spread. Kept apart from `spread_cover_prob` because the two line conventions are easy to mix up. | `betting_pricing.py`, `scripts/accuracy/grading.py` |
| `run_line_edge_report` | The report of one side of a pair. | `betting_pricing.py` |
| `run_line_is_pair` | True when away spread = −home spread. | `betting_pricing.py`, `scripts/accuracy/`, `scripts/probes/rescore_runlines.py` |
| `reference_margin_from_prices` | The margin of the first usable reference market. | `betting_pricing.py`, `betting_stored_odds.py`, `scripts/accuracy/pricing.py` |

**7. The signals.** `bet_signals_from_edges` has three steps.

1. **The gate.** A report passes when its edge is above zero and at least `min_edge`
   (0.02), and its EV is above `min_ev` (0.0).
2. **The stake.** The Kelly criterion gives the stake that grows a bankroll fastest:
   `f* = EV / b`. The stake is `min(cap, kelly_fraction × max(0, f*))`, with quarter
   Kelly (0.25) and a 5% cap by default.
3. **The rank.** Signals sort by EV, then edge, then stake, highest first.

| Name | What it does | Called from |
|---|---|---|
| `DEFAULT_MIN_EDGE` | The 0.02 edge floor. | `BetSignalConfig`; `scripts/accuracy/confidence.py`, `hypothetical_return.py` |
| `BetSignalConfig`, `DEFAULT_CONFIG` | The gate and stake settings, and their default instance. A negative Kelly fraction or cap raises `ValueError`. | `api/routes/betting.py` builds a `BetSignalConfig`; `DEFAULT_CONFIG` is the default argument inside the module |
| `stake_fraction(report, config)` | The recommended stake as a fraction of the bankroll. | `bet_signals_from_edges` |
| `BetSignal` | One signal: the report's label, side, line, price, edge and EV, plus the stake and the rank. It keeps the source report. | `api/schemas/markets.py` (`BetSignalModel`) |
| `bet_signals_from_edges(reports, config)` | Gate, stake and rank. | `api/routes/betting.py` |

A signal says the simulator prices a side above the market. It is not proof that the bet
makes money. A signal holds at the price its report captured; the caller rebuilds the
report when the price moves. `/signals` fires on all fifteen game markets, the
uncalibrated segment and team markets included (the segment markets, SIM-546).

**Watch out:** every builder raises `ValueError` on a simulator probability of exactly 0
or 1, because such a probability has no fair price. The pricing route and the accuracy
comparison skip that side.

## Who calls the folder

| Caller | What it uses |
|---|---|
| [`pipeline/live/ingestion/`](../../pipeline/live/ingestion/) | `odds_source`, `game_offers`, `prop_offers`, `write_offers`, the two closing stamps, `RefusalTally`, the prop roles ([pipeline-ingest.md](pipeline-ingest.md#the-live-service)) |
| [`operations/jobs/nightly_closing_lines.sh`](../../operations/jobs/nightly_closing_lines.sh) | `python -m betting.historical_loader` for the last two days' games |
| [`api/routes/betting.py`](../../api/routes/betting.py) | `/edges` and `/signals`; `BetSignalConfig`, `bet_signals_from_edges` |
| [`api/routes/betting_pricing.py`](../../api/routes/betting_pricing.py) | Every report builder, the de-vigs and the margin rule |
| [`api/routes/betting_stored_odds.py`](../../api/routes/betting_stored_odds.py) | `check_row`, the book helpers, `reference_margin_from_prices`, `american_to_decimal` |
| [`api/routes/games.py`](../../api/routes/games.py) | `GET /api/games/{pk}/props/{player_id}/{prop}`; `prop_edge_report` |
| [`scripts/accuracy/`](../../scripts/accuracy/) | The accuracy comparison: grading, pricing, the confidence floor and the hypothetical return |
| `scripts/probes/odds_book_census.py`, `odds_row_compare.py`, `book_sharpness.py`, `rescore_graded_book.py`, `rescore_runlines.py` | One-off studies of the vendor's rows and the stored odds |

No betting-value measurement runs until every band the acceptance lanes grade is green
(owner ruling 2026-09-08).
