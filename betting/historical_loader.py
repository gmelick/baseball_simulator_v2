#!/usr/bin/env python
"""Load the odds of finished games: the nightly pass and the backfills.

The loader walks the Final games in ``raw.games`` and loads each game's odds into
``raw.game_odds`` and ``raw.prop_odds`` through :func:`betting.load_odds_to_db.load_odds`. It
loads the opening and the closing line by default (``--line-types``). BettingPros
returns each market's opener with every offer, so the loader recovers an opening
line at any time.

What it writes. Each book's prices go to their own row, labelled ``book = 'bp:<id>'``
(``bp:0`` is the vendor's blended line, stored and never graded). A closing row is
written for every book that prices every side of the market. An opening row is
written only for the opener, the one book that opened every side. The loader asks a
pitcher for the pitcher markets and every other player for the batter markets
(:func:`betting.markets.prop_markets_for`); the roles come from ``raw.game_lineups``.
The load guard (:mod:`betting.validate_odds`) checks each row before the write, and the
odds hash makes a rerun write nothing new. The run ends with the guard's summary,
the rows written per book, and a warning when the guard refused more than 5% of
the rows.

Retries. ``--retries`` (default 3) and ``--retry-wait`` (default 5 s, doubled per
retry, at most 60 s) set the provider's retry policy for each vendor read. The live
service keeps the provider's default of one attempt.

Which games. By default the loader picks only the Final games of ``--seasons`` that
are missing odds (:func:`_missing_odds_clause`): a game with no stored row at one of
the run's line types. A rerun therefore skips every loaded game, and the nightly
pass needs no date window. ``--reload`` loads every Final game of the seasons
instead (the odds hash keeps the rows single).

Incomplete games. A game is incomplete when a vendor read failed after its retries,
or when the loader caught a failed fetch or write. Its rows so far stay written.
``--done-file`` lists each complete game, forced to disk, and a later run skips the
games it lists, so a ``--reload`` rerun loads only the incomplete ones.

Exit codes: 0 when every game is complete; 1 (``EXIT_INCOMPLETE``) when at least
one game is incomplete, or on an uncaught error; 2 on a usage error.

The offers cache. The job sets ``ODDS_OFFERS_CACHE_TTL_S`` to 600 s when it is
unset (the live service uses 30 s). A finished game's lines do not move, so one
fetch per market serves every player of the game.

Usage (``ODDS_API_KEY`` and ``BASEBALL_DB_DSN`` come from ``.env``; without the
key every vendor read fails and every game is incomplete)::

    make load-historical-odds FLAGS="--seasons 2024 --max-games 200"
    python -m betting.historical_loader --seasons 2024
    python -m betting.historical_loader --seasons 2024 --no-props
    python -m betting.historical_loader --seasons 2024 --no-game-odds --prop-stats singles doubles
    python -m betting.historical_loader --seasons 2024 --max-games 20 --book draftkings
    python -m betting.historical_loader --seasons 2024 \
        --done-file results/odds_load/odds_load_2024.done --retries 3 --retry-wait 5
    python -m betting.historical_loader --seasons 2024 --reload
    # The nightly pass (operations/jobs/nightly_closing_lines.sh):
    python -m betting.historical_loader --seasons 2025 2026
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
from collections import Counter
from collections.abc import Mapping
from typing import Any

from betting.load_odds_to_db import load_odds, odds_source

# The market and book vocabulary is imported, never copied.
from betting.markets import (
    BOOK_IDS_BY_NAME,
    GAME_MARKET_TYPES,
    PROP_ROLE_BATTER,
    PROP_ROLE_BOTH,
    PROP_ROLE_PITCHER,
    PROP_STATS,
    STORED_BOOK_FILTER_SQL,
    book_display_name,
    book_label,
    resolve_book,
)

# The load guard (pure; no I/O).
from betting.validate_odds import RefusalTally
from db.dsn import require_dsn
from db.introspect import pg_column_exists_async

log = logging.getLogger("betting.historical_loader")

#: No default password. main() resolves a blank value through
#: db.dsn.require_dsn, which names the missing variable.
DEFAULT_DSN = os.environ.get("BASEBALL_DB_DSN", "")

#: The line types a run loads by default: the opening and the closing line.
#: ``--line-types`` overrides it with any subset of KNOWN_LINE_TYPES.
LINE_TYPES: tuple[str, ...] = ("opening", "closing")
KNOWN_LINE_TYPES: tuple[str, ...] = ("opening", "current", "closing")

#: Every game market the book posts is persisted per line_type (one row each;
#: owner ruling 2026-09-12, SIM-421): the three full-game markets plus the
#: twelve segment and team markets. The vocabulary is imported from
#: ``betting/markets.py`` (``GAME_MARKET_TYPES``); ``--game-markets``
#: narrows it for a follow-up pass.

#: The offers-cache time-to-live this OFFLINE job applies when
#: ODDS_OFFERS_CACHE_TTL_S is unset (see the module docstring for why a long
#: value is safe here and not in the live pipeline).
OFFLINE_OFFERS_CACHE_TTL_S = 600
_OFFERS_CACHE_TTL_ENV = "ODDS_OFFERS_CACHE_TTL_S"

#: position_codes counted as pitchers in raw.game_lineups (P=pitcher; some feeds
#: use SP/RP). Everyone else is treated as a position player for prop routing.
_PITCHER_POSITIONS = frozenset({"P", "SP", "RP", "1"})

#: The loader warns when the guard refuses more than this share of the
#: rows it saw.
REFUSAL_WARN_SHARE = 0.05

#: The vendor-read retry policy this job hands the provider
#: (``--retries`` / ``--retry-wait``). The provider's own default is one
#: attempt, so the live pipeline keeps it; this offline job can afford to wait.
DEFAULT_RETRIES = 3
DEFAULT_RETRY_WAIT_S = 5.0

#: The exit code of a run that left a game incomplete (a vendor read
#: failed after its retries). An uncaught exception also exits 1 (Python's
#: exit status after a traceback). The crash-safe shell loop runs the loader
#: again a minute later on both. 0 = every game loaded;
#: 2 = a usage error (the parser's), never run again.
EXIT_INCOMPLETE = 1

#: The end-of-run summary names at most this many incomplete games.
INCOMPLETE_LIST_MAX = 20


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
    """The book id ``--book`` names, or ``None`` for every book.

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


def _select_retry_policy(retries: int, retry_wait_s: float) -> tuple[int, float]:
    """The ``(retries, first wait)`` pair this run hands the provider.

    ``retries`` must be 0 or more (0 = one attempt per read). The wait must be
    a positive, finite number of seconds. A bad value raises ``ValueError``,
    so the run stops before any fetch.
    """
    if int(retries) < 0:
        raise ValueError(f"--retries must be 0 or more, not {retries}.")
    wait = float(retry_wait_s)
    if not (math.isfinite(wait) and wait > 0):
        raise ValueError(f"--retry-wait must be a positive number of seconds, not {retry_wait_s}.")
    return int(retries), wait


def _hand_over_retry_policy(provider: Any, retries: int, retry_wait_s: float) -> None:
    """Give the provider this run's retry policy, when it takes one.

    The BettingPros provider takes it through ``set_retry_policy``. A test
    fake has no such method; the loader logs that once and goes on.
    """
    set_policy = getattr(provider, "set_retry_policy", None)
    if not callable(set_policy):
        log.info(
            "provider %s does not retry a failed read (it has no set_retry_policy)",
            type(provider).__name__,
        )
        return
    set_policy(retries, retry_wait_s)
    log.info(
        "vendor reads: up to %d retries each, the first after %.1f s, each later wait doubled",
        retries,
        retry_wait_s,
    )


def _read_failures(provider: Any) -> int:
    """The provider's count of the failed reads it caught (0 if it keeps none).

    The BettingPros provider adds 1 to ``read_failures`` each time it catches a
    failed read and goes on with an empty or partial result. A test fake may
    keep no count. A value that is not a whole number (a ``MagicMock``
    attribute) counts as 0.
    """
    value = getattr(provider, "read_failures", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _forget_failed_lookups(provider: Any) -> None:
    """Drop the provider's stored failed lookups before a game.

    The BettingPros provider stores a failed player lookup for one cache
    time-to-live (600 s here) and counts it once. A later game that names the
    same player inside that window would get the stored failure with no new
    count, and go on the done-list without the player's props. A test fake
    may have no such method.
    """
    forget = getattr(provider, "forget_failed_lookups", None)
    if callable(forget):
        forget()


def _incomplete_reasons(failed_reads: int, caught: Mapping[str, int]) -> str:
    """Why a game is incomplete, or ``""`` when it is complete.

    ``failed_reads`` is the growth of the provider's ``read_failures`` over
    the game. ``caught`` holds the fetches and the writes the loader itself
    caught over the game (keys ``fetch`` and ``write``).
    """
    parts = []
    if failed_reads > 0:
        parts.append(f"failed vendor reads: {failed_reads}")
    if caught.get("fetch", 0) > 0:
        parts.append(f"failed fetches: {caught['fetch']}")
    if caught.get("write", 0) > 0:
        parts.append(f"failed writes: {caught['write']}")
    return "; ".join(parts)


def _incomplete_summary(incomplete: list[int], *, done_file: str | None) -> str:
    """One line that counts the incomplete games and names up to 20 of them."""
    if not incomplete:
        return "incomplete games: none (no vendor read, fetch or write failed)"
    shown = ", ".join(str(pk) for pk in incomplete[:INCOMPLETE_LIST_MAX])
    more = len(incomplete) - INCOMPLETE_LIST_MAX
    if more > 0:
        shown = f"{shown} and {more} more"
    if done_file:
        then = "they stay off the done-list, so the next run loads them again"
    else:
        then = (
            "a game with some rows counts as loaded, so re-run with --reload "
            "and --done-file to fill them"
        )
    return (
        f"incomplete games: {len(incomplete)} (a vendor read failed after its retries, "
        f"or a fetch or a write failed): {shown}; {then}"
    )


def _configure_offline_cache() -> float:
    """Set the offers-cache time-to-live for this offline job; return the value in force.

    Only sets ``ODDS_OFFERS_CACHE_TTL_S`` when the operator left it unset, so an
    explicit value always wins. The provider reads the variable when it is
    built (:func:`betting.load_odds_to_db.odds_source`), so this runs before that call.
    """
    os.environ.setdefault(_OFFERS_CACHE_TTL_ENV, str(OFFLINE_OFFERS_CACHE_TTL_S))
    return float(os.environ[_OFFERS_CACHE_TTL_ENV])


def _missing_odds_clause(
    *,
    line_types: tuple[str, ...],
    game_odds: bool,
    props: bool,
    book_id: int | None = None,
    game_markets: tuple[str, ...] | None = None,
    prop_stats: tuple[str, ...] | None = None,
) -> tuple[str, list[object]]:
    """The SQL that keeps only the games missing odds, and its bound values.

    A game is missing odds when, for one of the run's line types, it has no
    stored row in a table the run writes: ``raw.game_odds`` for the game
    markets, ``raw.prop_odds`` for the props of a game with lineup rows (the
    loader loads no props without them). For the opening line a row in either
    table counts: only the opener book writes one, so a game whose game markets
    had no opener gains nothing from a reload. Only one-row-per-book rows count
    (``bp:<id>``); a ``--book`` run counts that book's rows only. A market
    subset (``--game-markets``, ``--prop-stats``) counts only its markets.

    Returns ``("AND (...)", params)``, bound from ``$1``; ``("", [])`` when the
    run writes no table.
    """
    params: list[object] = []

    def bind(value: object) -> str:
        params.append(value)
        return f"${len(params)}"

    if book_id is None:
        book_sql = f"t.{STORED_BOOK_FILTER_SQL}"
    else:
        book_sql = f"t.book = {bind(book_label(book_id))}"
    market_sql = {"raw.game_odds": "", "raw.prop_odds": ""}
    if game_markets and set(game_markets) != set(GAME_MARKET_TYPES):
        market_sql["raw.game_odds"] = f" AND t.market_type = ANY({bind(list(game_markets))})"
    if prop_stats and set(prop_stats) != set(PROP_STATS):
        market_sql["raw.prop_odds"] = f" AND t.prop_stat = ANY({bind(list(prop_stats))})"

    def stored(table: str, line_type: str) -> str:
        return (
            f"EXISTS (SELECT 1 FROM {table} t WHERE t.game_pk = raw.games.game_pk "
            f"AND t.line_type = {bind(line_type)} AND {book_sql}{market_sql[table]})"
        )

    has_lineup = "EXISTS (SELECT 1 FROM raw.game_lineups l WHERE l.game_pk = raw.games.game_pk)"
    missing: list[str] = []
    for line_type in line_types:
        if line_type == "opening" and game_odds and props:
            game_row = stored("raw.game_odds", line_type)
            prop_row = stored("raw.prop_odds", line_type)
            missing.append(f"NOT ({game_row} OR {prop_row})")
            continue
        if game_odds:
            missing.append(f"NOT {stored('raw.game_odds', line_type)}")
        if props:
            missing.append(f"({has_lineup} AND NOT {stored('raw.prop_odds', line_type)})")
    if not missing:
        return "", []
    return "AND (" + " OR ".join(missing) + ")", params


def read_done_file(path: str | None) -> set[int]:
    """The game_pks a crash-safe run has finished (``--done-file``).

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
    """Record one finished game in the ``--done-file``, forced to disk.

    The loader calls this only after the game's last row is written, so a
    game that a crash cuts off is never recorded and the next run loads it
    again in full (the odds_hash dedup keeps its rows single). It does not
    call it for an incomplete game either: a vendor read failed after its
    retries, or the loader caught a failed fetch or write (see the module
    docstring).
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
    missing_sql: str = "",
    missing_params: list[object] | None = None,
) -> list[dict]:
    """Return ``{game_pk}`` for each Final game of the seasons, in game_pk order.

    A Final game has both final scores. ``missing_sql`` (from
    :func:`_missing_odds_clause`) keeps only the games missing odds. The
    provider finds each game's teams and date itself.
    """
    import asyncpg

    sl = ", ".join(str(int(s)) for s in seasons)
    limit = f"LIMIT {int(max_games)}" if max_games else ""
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            f"""
            SELECT game_pk
            FROM raw.games
            WHERE status = 'Final'
              AND season IN ({sl})
              AND home_score_final IS NOT NULL AND away_score_final IS NOT NULL
              {missing_sql}
            ORDER BY game_pk
            {limit}
            """,
            *(missing_params or []),
        )
    finally:
        await conn.close()
    return [{"game_pk": int(r["game_pk"])} for r in rows]


async def _fetch_lineup_roles(pool, game_pk: int) -> dict[int, str]:
    """Return ``player_id -> PROP_ROLE_*`` for every player in ``game_pk``'s lineup.

    Reads ``raw.game_lineups``. A player listed at a pitcher position is a pitcher, any
    other a batter; a player listed at both (a two-way player) is both.
    """
    rows = await pool.fetch(
        """
        SELECT DISTINCT player_id, position_code
        FROM raw.game_lineups
        WHERE game_pk = $1
        """,
        int(game_pk),
    )
    roles: dict[int, str] = {}
    for r in rows:
        pos = str(r["position_code"] or "").upper()
        role = PROP_ROLE_PITCHER if pos in _PITCHER_POSITIONS else PROP_ROLE_BATTER
        pid = int(r["player_id"])
        roles[pid] = PROP_ROLE_BOTH if roles.get(pid, role) != role else role
    return roles


def _written_by_book_summary(written_by_book: Mapping[str, int]) -> str:
    """The rows written per book, most first, with each book's name."""
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

    seasons = sorted({int(s) for s in args.seasons})
    # The market subset and line types are validated here so a bad
    # value stops the run before any provider request.
    prop_stats = _select_prop_stats(getattr(args, "prop_stats", None))
    game_markets = _select_game_markets(getattr(args, "game_markets", None))
    line_types = _select_line_types(getattr(args, "line_types", None))
    no_game_odds = bool(getattr(args, "no_game_odds", False))
    # --book restricts the run to one book's rows.
    book_id = _select_book(getattr(args, "book", None))
    # The vendor-read retry policy (--retries / --retry-wait).
    retries, retry_wait_s = _select_retry_policy(
        getattr(args, "retries", DEFAULT_RETRIES),
        getattr(args, "retry_wait", DEFAULT_RETRY_WAIT_S),
    )
    # The long offline time-to-live must be in the environment BEFORE
    # the provider is built — it reads the variable in its constructor.
    cache_ttl = _configure_offline_cache()
    provider = odds_source()
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
    _hand_over_retry_policy(provider, retries, retry_wait_s)

    reload_all = bool(getattr(args, "reload", False))
    missing_sql: str = ""
    missing_params: list[object] = []
    if not reload_all:
        missing_sql, missing_params = _missing_odds_clause(
            line_types=line_types,
            game_odds=not no_game_odds,
            props=not args.no_props,
            book_id=book_id,
            game_markets=game_markets,
            prop_stats=prop_stats,
        )
    games = await _fetch_final_games(
        args.dsn,
        seasons,
        args.max_games,
        missing_sql=missing_sql,
        missing_params=missing_params,
    )
    log.info(
        "games: %s",
        "every Final game (--reload)" if reload_all else "only the Final games missing odds",
    )
    # A crash-safe run skips the games its done-file lists.
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
    # The guard's refusals and the rows written, per book, for the run.
    tally = RefusalTally()
    written_by_book: Counter[str] = Counter()
    # The games a failed vendor read, fetch or write left incomplete,
    # in load order, and the fetches and writes the loader itself caught.
    incomplete: list[int] = []
    load_failures: Counter[str] = Counter()
    try:
        pool = await asyncpg.create_pool(args.dsn, min_size=1, max_size=4)
        # Re-stamp a stored price the book still posts (Alembic 0029), as the live
        # writers do. A failed probe means the plain insert, as in the live service.
        try:
            restamp = await pg_column_exists_async(pool, "raw", "game_odds", "last_seen_at")
        except Exception:  # noqa: BLE001 -- unknown means the plain insert
            restamp = False

        for g in games:
            game_pk = g["game_pk"]
            # A lookup that failed in an earlier game is read again in
            # this one, so it counts here too; then the counts before this game.
            _forget_failed_lookups(provider)
            failures_before = _read_failures(provider)
            load_failures_before = load_failures.copy()
            if not no_game_odds:
                n_game_rows += await load_odds(
                    pool,
                    provider,
                    game_pk,
                    tally=tally,
                    restamp=restamp,
                    line_types=line_types,
                    game_markets=game_markets,
                    book_id=book_id,
                    failures=load_failures,
                    written_by_book=written_by_book,
                )

            if not args.no_props:
                roles = await _fetch_lineup_roles(pool, game_pk)
                if not roles:
                    log.info("skip props for game %s (no lineup rows)", game_pk)
                else:
                    n_prop_rows += await load_odds(
                        pool,
                        provider,
                        game_pk,
                        tally=tally,
                        restamp=restamp,
                        line_types=line_types,
                        game_markets=None,
                        roles=roles,
                        prop_stats=prop_stats,
                        book_id=book_id,
                        failures=load_failures,
                        written_by_book=written_by_book,
                    )

            n_done += 1
            # A read the provider gave up on, or a fetch or a write the
            # loader caught, leaves the game incomplete. Its rows so far stay
            # written; the done-file does not list it, so the next run loads it
            # again in full.
            reasons = _incomplete_reasons(
                _read_failures(provider) - failures_before,
                load_failures - load_failures_before,
            )
            if reasons:
                incomplete.append(int(game_pk))
                if done_file:
                    log.warning(
                        "game %s is incomplete (%s); "
                        "it stays off the done-list, so the next run loads it again",
                        game_pk,
                        reasons,
                    )
                else:
                    log.warning(
                        "game %s is incomplete (%s); the run lists it at the end",
                        game_pk,
                        reasons,
                    )
            elif done_file:
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
        "SIM-435 backfill complete: %d games (%d incomplete), %d game-odds rows, "
        "%d prop-odds rows written (re-runs are idempotent via odds_hash ON CONFLICT).",
        n_done,
        len(incomplete),
        n_game_rows,
        n_prop_rows,
    )
    # The guard's summary, the rows per book, and a warning past 5%.
    log.info("%s", tally.summary())
    log.info("%s", _written_by_book_summary(written_by_book))
    tally.warn_if_share_above(REFUSAL_WARN_SHARE, log)
    # The incomplete games, and the exit code the wrapper reads.
    summary = _incomplete_summary(incomplete, done_file=done_file)
    if incomplete:
        log.warning("%s", summary)
        return EXIT_INCOMPLETE
    log.info("%s", summary)
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
        "--reload",
        action="store_true",
        help="Load every Final game of the seasons. Default: only the games missing odds.",
    )
    p.add_argument(
        "--done-file",
        default=None,
        metavar="PATH",
        help="SIM-555 crash-safe resume: skip the games this file lists and append each "
        "game once its last row is written. A game a crash cuts off, or a game a failed "
        "vendor read, fetch or write left incomplete, is not listed, so the next run "
        "loads it again in full.",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        metavar="N",
        help="SIM-555: retry a vendor read that fails for a passing reason (a time-out, "
        "HTTP 429 or 5xx) up to N times; 0 = one attempt. Default: %(default)s.",
    )
    p.add_argument(
        "--retry-wait",
        type=float,
        default=DEFAULT_RETRY_WAIT_S,
        metavar="SECONDS",
        help="SIM-555: the wait before the first retry; each later wait is twice as long "
        "(the provider caps one wait at 60 s). Default: %(default)s.",
    )
    p.add_argument(
        "--book",
        default=None,
        metavar="NAME",
        help="SIM-555: load only this book's rows (a short name such as draftkings, "
        "or a label such as bp:12). Default: every book.",
    )
    args = p.parse_args(argv)
    # Fail loudly on an unknown market, line type or book,
    # or a bad retry policy, before any work.
    try:
        _select_prop_stats(args.prop_stats)
        _select_game_markets(args.game_markets)
        _select_line_types(args.line_types)
        _select_book(args.book)
        _select_retry_policy(args.retries, args.retry_wait)
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
    args.dsn = args.dsn or require_dsn()  # Fail loudly with no DSN
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
