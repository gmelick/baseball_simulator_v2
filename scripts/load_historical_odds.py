#!/usr/bin/env python
"""
scripts/load_historical_odds.py
===============================
SIM-435 — historical odds loader: backfill OPENING + CLOSING betting lines for
completed games into ``raw.game_odds`` / ``raw.prop_odds`` so the CLV backtest
(SIM-429 sub-4) has entry+closing lines to score against.

WHY
---
``raw.game_odds`` and ``raw.prop_odds`` are EMPTY (0 rows). The CLV engine
(SIM-339) needs, per completed game, an entry (opening) line and a closing line.
The live pipeline captures these forward (SIM-340 marking) but there is no
history. This script walks the Final games already in ``raw.games`` and pulls
their opening + closing lines from the configured odds provider (BettingPros by
default — set ``ODDS_PROVIDER=bettingpros`` + ``ODDS_API_KEY``) and persists them
through the SAME write path the live pipeline uses, so the odds_hash ON CONFLICT
dedup keeps re-runs idempotent.

WHAT IT WRITES
--------------
SIM-555 (2026-09-28): ONE ROW PER BOOK. The provider's payload lists every
book's line on every side; the loader stores each book's prices as its own row,
labelled ``book = 'bp:<id>'`` (the BettingPros id; ``bp:0`` is the vendor's
blended line, stored and never graded) and stamped ``book_line_at`` (the
vendor's stamp on the line; migration 0028). A row never mixes two books.

Per Final game (ordered by game_pk for reproducibility), for each line type
(``opening`` and ``closing`` by default):
  * game odds: every game market the book posts (the three full-game markets
    plus the twelve segment and team markets — SIM-421). ``closing``: one
    ``raw.game_odds`` row per book that quotes every side of the market.
    ``opening``: at most one row, the opener's (none when the sides name two
    openers). Each market fills only its own columns.
  * prop odds: the same per (player, prop_stat) for every player in the game's
    lineup. The pitcher markets (``PITCHER_PROP_STATS``: strikeouts /
    earned_runs / walks / outs_recorded / hits_allowed) are fetched only for
    pitchers; the batter markets (``BATTER_PROP_STATS``: hits / home_runs /
    total_bases / rbis / singles / doubles / triples / runs / stolen_bases /
    hits_runs_rbis) only for non-pitchers. Both tuples come from
    ``pipeline/odds_provider.py`` (the single source — SIM-421).

Before a row is written, the load guard (``pipeline/odds_row_guard.py``)
checks it. A row with no price at all is skipped silently. A row that cannot
be one bet (a missing side, spreads of different size, equal spreads priced
like a pair, a first-five row with a first-inning shape, an over and an under
at two lines, a closing line stamped more than 15 minutes after the scheduled
start) is refused: it is logged at INFO and counted by rule, market and book.
The kept rows of one offer (one market, or one player's market, at one line
type) go to the database in ONE ``executemany``. The run ends with the guard's
summary, the rows written per book, and a WARNING when the guard refused more
than 5% of the rows it saw.

``--book NAME`` (``draftkings``, ``DraftKings``, ``bp:12``) restricts a run to
that one book's rows (a smoke run, or a top-up of one book). An unknown name
stops the run before any fetch.

Persistence reuses ``LiveIngestionPipeline._persist_odds_many`` /
``_persist_prop_odds_many`` (so the SIM-092/SIM-340 ``odds_hash`` dedup + the
``raw.prop_odds`` CHECK constraint apply unchanged). The pipeline is constructed
WITHOUT starting it (no Redis / WS / HTTP loop) — we attach our own asyncpg pool
to ``pipeline._db`` and call the two persist coroutines directly.

USAGE
-----
    # SIM-555: from the host, with this checkout's scripts/ mounted over the
    # image's copy (scripts/ is not bind-mounted) and the provider named:
    MSYS_NO_PATHCONV=1 docker compose run -d --name sim555_load_2024 \
        -v "$PWD/scripts:/app/scripts" app \
        python scripts/load_historical_odds.py --seasons 2024 --provider bettingpros

    # In the app container, with a real provider configured:
    ODDS_PROVIDER=bettingpros ODDS_API_KEY=… \
        python scripts/load_historical_odds.py --seasons 2024 --max-games 200
    python scripts/load_historical_odds.py --seasons 2023 2024            # game + prop
    python scripts/load_historical_odds.py --seasons 2024 --no-props      # game odds only

    # SIM-421 follow-up pass: fetch ONLY the eight new markets for a season whose
    # seven original markets are already loaded. The game lines are skipped
    # (--no-game-odds); a re-run is idempotent through the odds_hash dedup.
    ODDS_PROVIDER=bettingpros ODDS_API_KEY=... python scripts/load_historical_odds.py
        --seasons 2024 --no-game-odds
        --prop-stats singles doubles triples runs stolen_bases hits_runs_rbis
                     outs_recorded hits_allowed

    # --prop-stats takes any subset of the 15-market vocabulary (an unknown value
    # stops the run before any fetch); --line-types defaults to "opening closing".

    # SIM-555: one book only (a smoke run, or a top-up of one book's rows):
    ODDS_PROVIDER=bettingpros ODDS_API_KEY=... python scripts/load_historical_odds.py
        --seasons 2024 --max-games 20 --book draftkings

    # SIM-421 (owner ruling 2026-09-12): every game market the book posts is
    # loaded by default — the three full-game markets plus the twelve segment
    # and team markets (first-inning / first-five moneyline, total, run line;
    # each side's full-game and first-five team total; first team to score;
    # a run in the first inning). --game-markets narrows the set the same way
    # --prop-stats does, e.g. a follow-up pass for the twelve new markets only:
    ODDS_PROVIDER=bettingpros ODDS_API_KEY=... python scripts/load_historical_odds.py
        --seasons 2024 --no-props
        --game-markets f1_moneyline f5_moneyline f1_total f5_total f1_runline
                       f5_runline team_total_home team_total_away
                       f5_team_total_home f5_team_total_away first_to_score
                       first_inning_run

    # Via a Makefile wrapper (mirror make validate-props):
    make load-historical-odds FLAGS="--seasons 2024 --max-games 200"

This is an OFFLINE backfill job. It is network-bound; cap a smoke run with
``--max-games``. SIM-555 (review fix 2026-09-28): the job refuses the
deterministic MockOddsAPI unless ``--provider mock`` is on the command line.
The app container leaves ``ODDS_PROVIDER`` unset and the registry's default
is the mock, so a run with no ``--provider`` used to write made-up
``consensus`` prices into a real season. ``--provider mock`` still gives the
no-network synthetic-line smoke / wiring check.

The BettingPros provider caches one ``/offers`` response per (event, market)
for a time-to-live (SIM-421). The live pipeline needs a short one (30 s) so a
60-second cycle never re-uses a stale line. This job does not: its games are
over, their lines are final, and every player x market x line type of one game
is requested back-to-back. The job therefore sets ``ODDS_OFFERS_CACHE_TTL_S``
to ``OFFLINE_OFFERS_CACHE_TTL_S`` (600 s) when the variable is unset, which
collapses a game's several hundred prop requests into one fetch per market.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# SIM-421: the prop-market vocabulary is imported, never copied — the pitcher /
# batter split routes which markets a player is asked for.
# SIM-555: the book vocabulary and the by-book seam come from the same source.
from pipeline.odds_provider import (  # noqa: E402
    BATTER_PROP_STATS,
    BOOK_IDS_BY_NAME,
    DEFAULT_PROVIDER,
    GAME_MARKET_TYPES,
    GAME_ODDS_FIELDS,
    ODDS_PROVIDER_ENV,
    PITCHER_PROP_STATS,
    PROP_STATS,
    STORED_BOOK_FILTER_SQL,
    book_display_name,
    book_id_from_label,
    book_label,
    odds_rows_by_book,
    prop_rows_by_book,
    resolve_book,
)

# SIM-555: the load guard (pure; no I/O).
from pipeline.odds_row_guard import RefusalTally, check_row  # noqa: E402

log = logging.getLogger("load_historical_odds")

DEFAULT_DSN = os.environ.get(
    "BASEBALL_DB_DSN",
    "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim",
)

#: line_types to backfill by default (entry + closing — the two CLV reference
#: points). ``--line-types`` overrides it with any subset of KNOWN_LINE_TYPES.
LINE_TYPES: tuple[str, ...] = ("opening", "closing")
KNOWN_LINE_TYPES: tuple[str, ...] = ("opening", "current", "closing")

#: SIM-421 (owner ruling 2026-09-12): every game market the book posts is
#: persisted per line_type (one row each) — the three full-game markets plus
#: the twelve segment and team markets. The vocabulary is imported from
#: ``pipeline/odds_provider.py`` (``GAME_MARKET_TYPES``); ``--game-markets``
#: narrows it for a follow-up pass.

#: SIM-421: the offers-cache time-to-live this OFFLINE job applies when
#: ODDS_OFFERS_CACHE_TTL_S is unset (see the module docstring for why a long
#: value is safe here and not in the live pipeline).
OFFLINE_OFFERS_CACHE_TTL_S = 600
_OFFERS_CACHE_TTL_ENV = "ODDS_OFFERS_CACHE_TTL_S"

#: position_codes counted as pitchers in raw.game_lineups (P=pitcher; some feeds
#: use SP/RP). Everyone else is treated as a position player for prop routing.
_PITCHER_POSITIONS = frozenset({"P", "SP", "RP", "1"})

#: SIM-555: the loader warns when the guard refuses more than this share of the
#: rows it saw.
REFUSAL_WARN_SHARE = 0.05

#: SIM-555: the odds fields of a prop row; a row with all three empty is not a quote.
_PROP_ODDS_FIELDS: tuple[str, ...] = ("line", "over_ml", "under_ml")

#: SIM-555: the writers the load functions take. A game writer gets
#: ``(game_pk, rows)``, a prop writer ``(rows)``; both persist one offer's rows
#: in one batch and return the number of rows sent.
GameWriter = Callable[[int, list[dict[str, Any]]], Awaitable[Any]]
PropWriter = Callable[[list[dict[str, Any]]], Awaitable[Any]]


def _select_prop_stats(requested: list[str] | None) -> tuple[str, ...]:
    """The prop markets this run fetches, in canonical PROP_STATS order.

    ``None`` (no ``--prop-stats``) means every market. An unknown value raises
    ``ValueError`` naming it and the vocabulary — the run stops before any
    fetch, so a typo never silently loads a partial season.
    """
    if not requested:
        return PROP_STATS
    unknown = sorted({s for s in requested if s not in PROP_STATS})
    if unknown:
        raise ValueError(
            f"Unknown prop_stat value(s) {unknown}. Known values: {', '.join(PROP_STATS)}"
        )
    wanted = set(requested)
    return tuple(s for s in PROP_STATS if s in wanted)


def _select_game_markets(requested: list[str] | None) -> tuple[str, ...]:
    """The game markets this run fetches, in canonical GAME_MARKET_TYPES order.

    ``None`` (no ``--game-markets``) means every market. An unknown value
    raises ``ValueError`` naming it and the vocabulary, so the run stops before
    any fetch.
    """
    if not requested:
        return GAME_MARKET_TYPES
    unknown = sorted({m for m in requested if m not in GAME_MARKET_TYPES})
    if unknown:
        raise ValueError(
            f"Unknown market_type value(s) {unknown}. Known values: {', '.join(GAME_MARKET_TYPES)}"
        )
    wanted = set(requested)
    return tuple(m for m in GAME_MARKET_TYPES if m in wanted)


def _select_line_types(requested: list[str] | None) -> tuple[str, ...]:
    """The line types this run fetches (default: opening + closing).

    An unknown value raises ``ValueError`` (the raw.prop_odds CHECK constraint
    would reject it anyway; failing here is earlier and clearer).
    """
    if not requested:
        return LINE_TYPES
    unknown = sorted({lt for lt in requested if lt not in KNOWN_LINE_TYPES})
    if unknown:
        raise ValueError(
            f"Unknown line_type value(s) {unknown}. Known values: {', '.join(KNOWN_LINE_TYPES)}"
        )
    wanted = set(requested)
    return tuple(lt for lt in KNOWN_LINE_TYPES if lt in wanted)


def _select_book(requested: str | None) -> int | None:
    """SIM-555: the book id ``--book`` names, or ``None`` for every book.

    ``resolve_book`` reads a short name (``draftkings``, ``DraftKings``) or a
    label (``bp:12``). A name it does not know raises ``ValueError`` naming the
    known names, so a typo stops the run before any fetch.
    """
    if requested is None or not str(requested).strip():
        return None
    book_id = resolve_book(str(requested).strip())
    if book_id is None:
        raise ValueError(
            f"Unknown book {requested!r}. Known names: {', '.join(sorted(BOOK_IDS_BY_NAME))}"
            " (or a label such as bp:12)."
        )
    return book_id


#: SIM-555: the registry name of the deterministic mock provider.
MOCK_PROVIDER = "mock"


def _select_provider(requested: str | None) -> str:
    """SIM-555: the odds provider this run reads; never the mock unless asked.

    ``--provider`` wins, then the ``ODDS_PROVIDER`` environment variable, then
    the registry's default (the mock). The mock writes made-up prices, so a
    run whose provider resolves to it without ``--provider mock`` on the
    command line raises ``ValueError``: the app container leaves
    ``ODDS_PROVIDER`` unset, and a real season must never get mock rows by
    default.
    """
    asked = (requested or "").strip().lower()
    name = (asked or os.environ.get(ODDS_PROVIDER_ENV) or DEFAULT_PROVIDER).strip().lower()
    if name == MOCK_PROVIDER and asked != MOCK_PROVIDER:
        raise ValueError(
            "the odds provider resolves to the mock (ODDS_PROVIDER is unset or 'mock'), "
            "which writes made-up prices: pass --provider bettingpros for a real load, "
            "or --provider mock for a no-network test run."
        )
    return name


def _configure_offline_cache() -> float:
    """Set the offers-cache time-to-live for this offline job; return the value in force.

    Only sets ``ODDS_OFFERS_CACHE_TTL_S`` when the operator left it unset, so an
    explicit value always wins. The provider reads the variable when it is
    built (``get_odds_provider``), so this runs before that call.
    """
    os.environ.setdefault(_OFFERS_CACHE_TTL_ENV, str(OFFLINE_OFFERS_CACHE_TTL_S))
    return float(os.environ[_OFFERS_CACHE_TTL_ENV])


def parse_skip_loaded_since(value: str | None) -> datetime | None:
    """SIM-421 resume: parse ``--skip-loaded-since`` into an aware datetime.

    ISO-8601 (``2026-09-12T23:40:00`` or with an offset). A value with no offset
    is read in the machine's local time zone — the same clock the loader logs
    in — so an operator can copy a timestamp straight from a log line.
    """
    if value is None or not str(value).strip():
        return None
    dt = datetime.fromisoformat(str(value).strip())
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive == local wall-clock time
    return dt


#: SIM-555: the resume query's table, by the kind of rows the run writes last.
_RESUME_TABLE_PROPS = "raw.prop_odds"
_RESUME_TABLE_GAME_ODDS = "raw.game_odds"

#: SIM-555: on an every-book run, a game counts as loaded when its rows since
#: the resume instant name at least this many books. One book's rows alone are
#: a ``--book`` smoke's, not a full load's (a full load stores the blend,
#: ``bp:0``, beside every sportsbook).
RESUME_MIN_BOOKS = 2


def _resume_clause(*, table: str, book_id: int | None) -> str:
    """SIM-555: the resume filter's SQL (an ``AND NOT EXISTS (...)`` clause).

    The clause binds ``$1`` = the resume instant, ``$2`` = the run's line
    types, and on a ``--book`` run ``$3`` = the book's label. It drops a game
    that holds, in ``table``, rows fetched at or after ``$1`` at one of the
    run's line types (so the live cycle's ``current`` rows never count), and
    labelled ``bp:<id>`` (so an old ``consensus`` row never counts). On an
    every-book run those rows must name at least ``RESUME_MIN_BOOKS`` books,
    so a game a ``--book`` smoke touched still loads; on a ``--book`` run the
    book's own rows are enough.
    """
    if table not in (_RESUME_TABLE_PROPS, _RESUME_TABLE_GAME_ODDS):
        raise ValueError(f"resume table must be raw.prop_odds or raw.game_odds, not {table!r}")
    where = (
        f"SELECT 1 FROM {table} p "
        "WHERE p.game_pk = raw.games.game_pk AND p.fetched_at >= $1 "
        "AND p.line_type = ANY($2::varchar[]) "
    )
    if book_id is None:
        clause = (
            f"AND NOT EXISTS ({where}AND p.{STORED_BOOK_FILTER_SQL} "  # p.book LIKE 'bp:%'
            f"HAVING COUNT(DISTINCT p.book) >= {int(RESUME_MIN_BOOKS)})"
        )
        return clause
    return f"AND NOT EXISTS ({where}AND p.book = $3)"


def read_done_file(path: str | None) -> set[int]:
    """SIM-555: the game_pks a crash-safe run has finished (``--done-file``).

    One game_pk per line. A missing file is an empty set; a line that is not
    a whole number is ignored (a line cut short by a crash is harmless: its
    game is loaded again).
    """
    if not path or not os.path.exists(path):
        return set()
    done: set[int] = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            text = line.strip()
            if text.isdigit():
                done.add(int(text))
    return done


def append_done(path: str, game_pk: int) -> None:
    """SIM-555: record one finished game in the ``--done-file``, forced to disk.

    The loader calls this only after the game's last row is written, so a
    game that a crash cuts off is never recorded and the next run loads it
    again in full (the odds_hash dedup keeps its rows single).
    """
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{int(game_pk)}\n")
        fh.flush()
        os.fsync(fh.fileno())


async def _fetch_final_games(
    dsn: str,
    seasons: list[int],
    max_games: int | None,
    *,
    skip_loaded_since: datetime | None = None,
    book_id: int | None = None,
    line_types: tuple[str, ...] = LINE_TYPES,
    resume_on_game_odds: bool = False,
) -> list[dict]:
    """Return completed-game rows ``{game_pk}`` for the requested seasons.

    Reads ``raw.games`` (status='Final'); we only need the game_pk — the odds
    provider resolves teams/date itself from the MLB schedule. SIM-432: the live
    schema stores the final score in ``home_score_final`` / ``away_score_final``
    (not ``home_score``), so we gate on those being present (a real Final game).

    SIM-421 resume: ``skip_loaded_since`` drops every game that already holds a
    ``raw.prop_odds`` row fetched at or after that instant — the games a run
    that died mid-season had finished. A game the book listed no event for has
    no rows and is retried (cheap: no event, no offers calls). The one game in
    flight when the run died may hold a partial set of rows and is skipped too;
    a later full pass fills it through the dedup.

    SIM-555: the rows that count are the run's own kind (see
    :func:`_resume_clause`): one-row-per-book rows (``p.book LIKE 'bp:%'``) at
    the run's ``line_types``, from at least two books; with ``book_id`` (a
    ``--book`` run) that book's rows. ``resume_on_game_odds`` reads
    ``raw.game_odds`` instead of ``raw.prop_odds``: a ``--no-props`` run writes
    no prop rows, so its resume has to look at the game rows.
    """
    import asyncpg

    sl = ", ".join(str(int(s)) for s in seasons)
    limit = f"LIMIT {int(max_games)}" if max_games else ""
    resume_clause = ""
    params: list[object] = []
    if skip_loaded_since is not None:
        table = _RESUME_TABLE_GAME_ODDS if resume_on_game_odds else _RESUME_TABLE_PROPS
        resume_clause = _resume_clause(table=table, book_id=book_id)
        params.extend([skip_loaded_since, list(line_types)])
        if book_id is not None:
            params.append(book_label(book_id))
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            f"""
            SELECT game_pk
            FROM raw.games
            WHERE status = 'Final'
              AND season IN ({sl})
              AND home_score_final IS NOT NULL AND away_score_final IS NOT NULL
              {resume_clause}
            ORDER BY game_pk
            {limit}
            """,
            *params,
        )
    finally:
        await conn.close()
    return [{"game_pk": int(r["game_pk"])} for r in rows]


async def _fetch_lineup_players(pool, game_pk: int) -> list[tuple[int, bool]]:
    """Return ``(player_id, is_pitcher)`` for every player in ``game_pk``'s lineup.

    Reads ``raw.game_lineups`` (the SIM-353 lineup table). ``is_pitcher`` routes
    which prop markets are requested for the player. DISTINCT so a player who
    changed slots (sequence > 1) is fetched once.
    """
    rows = await pool.fetch(
        """
        SELECT DISTINCT player_id, position_code
        FROM raw.game_lineups
        WHERE game_pk = $1
        """,
        int(game_pk),
    )
    out: list[tuple[int, bool]] = []
    for r in rows:
        pos = str(r["position_code"] or "").upper()
        out.append((int(r["player_id"]), pos in _PITCHER_POSITIONS))
    return out


def _has_line(odds: Mapping[str, Any]) -> bool:
    """True if a game-odds dict carries at least one resolved price/line."""
    return any(odds.get(k) is not None for k in GAME_ODDS_FIELDS)


def _prop_has_line(quote: Mapping[str, Any]) -> bool:
    """SIM-555: True if a prop dict carries a line or a price."""
    return any(quote.get(k) is not None for k in _PROP_ODDS_FIELDS)


def _keep_rows(
    rows: list[dict[str, Any]],
    *,
    game_pk: int,
    tally: RefusalTally | None,
    book_id: int | None = None,
) -> list[dict[str, Any]]:
    """SIM-555: the rows of one offer that go to the database.

    In order: a ``--book`` run drops every other book's row; a row with no
    price or line at all is skipped silently (it is not a quote); the load
    guard checks every other row. A refused row is logged at INFO and counted
    in ``tally`` by rule, market and book.
    """
    kept: list[dict[str, Any]] = []
    for row in rows:
        if book_id is not None and book_id_from_label(row.get("book")) != book_id:
            continue
        prop_stat = row.get("prop_stat")
        has_odds = _has_line(row) if prop_stat is None else _prop_has_line(row)
        if not has_odds:
            continue
        if tally is not None:
            tally.offered()
        refusal = check_row(row)
        if refusal is None:
            kept.append(row)
            continue
        market = str(prop_stat if prop_stat is not None else row.get("market_type"))
        book = str(row.get("book"))
        if tally is not None:
            tally.add(refusal, market, book)
        shown = market if prop_stat is None else f"{market} (player {row.get('player_id')})"
        log.info(
            "refused game %s %s/%s %s: %s",
            game_pk,
            row.get("line_type"),
            shown,
            book,
            refusal.message,
        )
    return kept


def _count_by_book(rows: list[dict[str, Any]], written_by_book: Counter[str] | None) -> None:
    """SIM-555: add each written row to the per-book count (``None`` counts nothing)."""
    if written_by_book is None:
        return
    for row in rows:
        written_by_book[str(row.get("book"))] += 1


async def _load_game_odds(
    provider,
    persist_many: GameWriter,
    game_pk: int,
    *,
    line_types: tuple[str, ...] = LINE_TYPES,
    game_markets: tuple[str, ...] = GAME_MARKET_TYPES,
    tally: RefusalTally | None = None,
    written_by_book: Counter[str] | None = None,
    book_id: int | None = None,
) -> int:
    """Fetch + persist the game lines (opening & closing by default) for one game.

    SIM-555: each (line type, market) is one offer. The provider gives every
    book's row (``odds_rows_by_book``); ``_keep_rows`` applies ``--book``, skips
    the empty rows and runs the load guard; the kept rows go to
    ``persist_many(game_pk, rows)`` in ONE call. ``tally`` counts the guard's
    refusals and ``written_by_book`` the rows written per book (both optional).

    ``game_markets`` is the market subset (``--game-markets``); the default is
    every market the book posts. Returns the rows written (sent to the
    database; the dedup may insert fewer).
    """
    written = 0
    for line_type in line_types:
        for market_type in game_markets:
            try:
                rows = odds_rows_by_book(
                    provider, game_pk, line_type=line_type, market_type=market_type
                )
            except Exception as exc:  # noqa: BLE001 — skip a market we can't fetch
                log.warning(
                    "game odds fetch failed game %s %s/%s: %s",
                    game_pk,
                    line_type,
                    market_type,
                    exc,
                )
                continue
            kept = _keep_rows(rows, game_pk=game_pk, tally=tally, book_id=book_id)
            if not kept:
                continue
            try:
                await persist_many(game_pk, kept)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "game odds persist failed game %s %s/%s (%d rows): %s",
                    game_pk,
                    line_type,
                    market_type,
                    len(kept),
                    exc,
                )
                continue
            written += len(kept)
            _count_by_book(kept, written_by_book)
    return written


def _prop_stats_for_player(is_pitcher: bool, prop_stats: tuple[str, ...]) -> tuple[str, ...]:
    """SIM-421: the markets of ``prop_stats`` a pitcher (or a hitter) is asked for.

    A pitcher gets the pitcher markets only, a hitter the batter markets only,
    so ``hits`` (batter) and ``hits_allowed`` (pitcher) never cross.
    """
    role_stats = PITCHER_PROP_STATS if is_pitcher else BATTER_PROP_STATS
    return tuple(s for s in prop_stats if s in role_stats)


async def _load_prop_odds(
    provider,
    persist_many: PropWriter,
    game_pk: int,
    players: list[tuple[int, bool]],
    *,
    prop_stats: tuple[str, ...] = PROP_STATS,
    line_types: tuple[str, ...] = LINE_TYPES,
    tally: RefusalTally | None = None,
    written_by_book: Counter[str] | None = None,
    book_id: int | None = None,
) -> int:
    """Fetch + persist the prop lines (opening & closing by default) for one game.

    SIM-555: each (player, prop_stat, line type) is one offer: every book's row
    (``prop_rows_by_book``), filtered and guarded as in :func:`_load_game_odds`,
    persisted in ONE ``persist_many(rows)`` call.

    ``prop_stats`` narrows the markets (``--prop-stats``); each player is asked
    only for the markets of his role. An unknown prop_stat raises
    ``ValueError`` (a programming error). Returns the rows written.
    """
    written = 0
    for player_id, is_pitcher in players:
        for prop_stat in _prop_stats_for_player(is_pitcher, prop_stats):
            for line_type in line_types:
                try:
                    rows = prop_rows_by_book(
                        provider, game_pk, player_id, prop_stat, line_type=line_type
                    )
                except ValueError:
                    raise  # an unknown prop_stat is a programming error — surface it
                except Exception as exc:  # noqa: BLE001 — skip a market we can't fetch
                    log.warning(
                        "prop fetch failed game %s player %s %s/%s: %s",
                        game_pk,
                        player_id,
                        prop_stat,
                        line_type,
                        exc,
                    )
                    continue
                kept = _keep_rows(rows, game_pk=game_pk, tally=tally, book_id=book_id)
                if not kept:
                    continue
                try:
                    await persist_many(kept)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "prop persist failed game %s player %s %s/%s (%d rows): %s",
                        game_pk,
                        player_id,
                        prop_stat,
                        line_type,
                        len(kept),
                        exc,
                    )
                    continue
                written += len(kept)
                _count_by_book(kept, written_by_book)
    return written


def _build_persisters(dsn: str, pool):
    """Return ``(persist_game_many, persist_prop_many)`` bound to the live pipeline write path.

    SIM-555: the batch writers ``LiveIngestionPipeline._persist_odds_many`` /
    ``_persist_prop_odds_many`` (one ``executemany`` per offer; the
    SIM-092/SIM-340 odds_hash dedup + INSERT … ON CONFLICT DO NOTHING
    unchanged). The pipeline is built without starting it: a dummy redis_url
    satisfies the __init__ guard — start() is never called so Redis is
    untouched — and our own asyncpg pool is attached to ``_db``.
    """
    from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

    pipeline = LiveIngestionPipeline(
        dsn=dsn,
        redis_url=os.environ.get("REDIS_URL", "redis://unused:6379"),
    )
    pipeline._db = pool  # attach our pool; start() (which would build one) is never called
    return pipeline._persist_odds_many, pipeline._persist_prop_odds_many


def _written_by_book_summary(written_by_book: Mapping[str, int]) -> str:
    """SIM-555: the rows written per book, most first, with each book's name."""
    if not written_by_book:
        return "rows written by book: none"
    lines = ["rows written by book:"]
    for book in sorted(written_by_book, key=lambda b: (-written_by_book[b], b)):
        name = book_display_name(book)
        shown = book if name == book else f"{book} ({name})"
        lines.append(f"  {shown}: {written_by_book[book]}")
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    import asyncpg

    from pipeline.odds_provider import get_odds_provider

    seasons = sorted({int(s) for s in args.seasons})
    # SIM-421: the market subset and line types are validated here so a bad
    # value stops the run before any provider request.
    prop_stats = _select_prop_stats(getattr(args, "prop_stats", None))
    game_markets = _select_game_markets(getattr(args, "game_markets", None))
    line_types = _select_line_types(getattr(args, "line_types", None))
    no_game_odds = bool(getattr(args, "no_game_odds", False))
    # SIM-555: --book restricts the run to one book's rows.
    book_id = _select_book(getattr(args, "book", None))
    # SIM-421: the long offline time-to-live must be in the environment BEFORE
    # the provider is built — it reads the variable in its constructor.
    try:
        provider_name = _select_provider(args.provider)
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    cache_ttl = _configure_offline_cache()
    provider = get_odds_provider(provider_name)
    log.info(
        "SIM-435 historical odds backfill — seasons=%s provider=%s game_odds=%s props=%s "
        "game_markets=%s prop_stats=%s line_types=%s book=%s offers_cache_ttl_s=%s",
        seasons,
        type(provider).__name__,
        not no_game_odds,
        not args.no_props,
        list(game_markets),
        list(prop_stats),
        list(line_types),
        "every book" if book_id is None else book_display_name(book_label(book_id)),
        cache_ttl,
    )

    skip_since = parse_skip_loaded_since(getattr(args, "skip_loaded_since", None))
    # SIM-555: a --no-props run writes no prop rows; its resume reads the game rows.
    resume_on_game_odds = bool(args.no_props)
    games = await _fetch_final_games(
        args.dsn,
        seasons,
        args.max_games,
        skip_loaded_since=skip_since,
        book_id=book_id,
        line_types=line_types,
        resume_on_game_odds=resume_on_game_odds,
    )
    if skip_since is not None:
        log.info(
            "resume: skipping games with %s rows (%s) fetched since %s",
            "game-odds" if resume_on_game_odds else "prop",
            ", ".join(line_types),
            skip_since.isoformat(),
        )
    # SIM-555: a crash-safe run skips the games its done-file lists.
    done_file = getattr(args, "done_file", None)
    if done_file:
        done = read_done_file(done_file)
        before = len(games)
        games = [g for g in games if int(g["game_pk"]) not in done]
        log.info(
            "done-file %s: %d games already finished, %d skipped here",
            done_file,
            len(done),
            before - len(games),
        )
    log.info("Found %d completed games to backfill.", len(games))
    if not games:
        log.warning("No completed games found — nothing to backfill.")
        return 0

    pool = None
    n_game_rows = 0
    n_prop_rows = 0
    n_done = 0
    # SIM-555: the guard's refusals and the rows written, per book, for the run.
    tally = RefusalTally()
    written_by_book: Counter[str] = Counter()
    try:
        pool = await asyncpg.create_pool(args.dsn, min_size=1, max_size=4)
        persist_game_many, persist_prop_many = _build_persisters(args.dsn, pool)

        for g in games:
            game_pk = g["game_pk"]
            if not no_game_odds:
                n_game_rows += await _load_game_odds(
                    provider,
                    persist_game_many,
                    game_pk,
                    line_types=line_types,
                    game_markets=game_markets,
                    tally=tally,
                    written_by_book=written_by_book,
                    book_id=book_id,
                )

            if not args.no_props:
                players = await _fetch_lineup_players(pool, game_pk)
                if not players:
                    log.info("skip props for game %s (no lineup rows)", game_pk)
                else:
                    n_prop_rows += await _load_prop_odds(
                        provider,
                        persist_prop_many,
                        game_pk,
                        players,
                        prop_stats=prop_stats,
                        line_types=line_types,
                        tally=tally,
                        written_by_book=written_by_book,
                        book_id=book_id,
                    )

            n_done += 1
            if done_file:
                append_done(done_file, game_pk)
            if n_done % 25 == 0:
                log.info(
                    "  backfilled %d/%d games (%d game rows, %d prop rows, %d refused) ...",
                    n_done,
                    len(games),
                    n_game_rows,
                    n_prop_rows,
                    tally.refused,
                )
    finally:
        if pool is not None:
            await pool.close()

    log.info(
        "SIM-435 backfill complete: %d games, %d game-odds rows, %d prop-odds rows written "
        "(re-runs are idempotent via odds_hash ON CONFLICT).",
        n_done,
        n_game_rows,
        n_prop_rows,
    )
    # SIM-555: the guard's summary, the rows per book, and a warning past 5%.
    log.info("%s", tally.summary())
    log.info("%s", _written_by_book_summary(written_by_book))
    tally.warn_if_share_above(REFUSAL_WARN_SHARE, log)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backfill opening+closing odds for completed games (SIM-435)."
    )
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (completed games + writes).")
    p.add_argument("--seasons", type=int, nargs="+", required=True, help="Seasons to backfill.")
    p.add_argument("--max-games", type=int, default=None, help="Cap games (smoke run).")
    p.add_argument(
        "--no-props",
        action="store_true",
        help="Backfill game odds only (skip the per-player prop lines).",
    )
    p.add_argument(
        "--no-game-odds",
        action="store_true",
        help="Backfill prop lines only (skip the game-level lines) — the SIM-421 "
        "follow-up pass for a season whose game lines are already loaded.",
    )
    p.add_argument(
        "--prop-stats",
        nargs="+",
        default=None,
        metavar="PROP_STAT",
        help="Fetch only these prop markets (any subset of: "
        f"{', '.join(PROP_STATS)}). Default: every market.",
    )
    p.add_argument(
        "--game-markets",
        nargs="+",
        default=None,
        metavar="MARKET_TYPE",
        help="Fetch only these game markets (any subset of: "
        f"{', '.join(GAME_MARKET_TYPES)}). Default: every market.",
    )
    p.add_argument(
        "--line-types",
        nargs="+",
        default=None,
        metavar="LINE_TYPE",
        help=f"Line types to fetch (any subset of: {', '.join(KNOWN_LINE_TYPES)}). "
        f"Default: {' '.join(LINE_TYPES)}.",
    )
    p.add_argument(
        "--skip-loaded-since",
        default=None,
        metavar="ISO_TIMESTAMP",
        help="Resume a run that died: skip every game that already has this run's rows "
        "(prop rows; game rows with --no-props) at its line types, fetched at or after "
        "this instant (ISO-8601; no offset = local time).",
    )
    p.add_argument(
        "--done-file",
        default=None,
        metavar="PATH",
        help="SIM-555 crash-safe resume: skip the games this file lists and append each "
        "game once its last row is written. A game a crash cuts off is not listed, so "
        "the next run loads it again in full.",
    )
    p.add_argument(
        "--provider",
        default=None,
        help="Odds provider name (defaults to the ODDS_PROVIDER env). The mock "
        "(the registry default) runs only when named here: --provider mock.",
    )
    p.add_argument(
        "--book",
        default=None,
        metavar="NAME",
        help="SIM-555: load only this book's rows (a short name such as draftkings, "
        "or a label such as bp:12). Default: every book.",
    )
    args = p.parse_args(argv)
    # SIM-421 / SIM-555: fail loudly on an unknown market, line type or book,
    # before any work.
    try:
        _select_prop_stats(args.prop_stats)
        _select_game_markets(args.game_markets)
        _select_line_types(args.line_types)
        _select_book(args.book)
    except ValueError as exc:
        p.error(str(exc))
    return args


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
