"""The closing rows: at first pitch, each book's last pre-game offer is stamped
as the closing line.
"""

from __future__ import annotations

from collections.abc import (
    Mapping,
    Sequence,
)
from datetime import datetime
from typing import Any

from pipeline.live.ingestion.odds_rows import (
    _odds_hash,
    _prop_odds_hash,
)
from pipeline.odds_row_guard import (
    CLOSING_STAMP_GRACE,
    check_row,
)

# ---------------------------------------------------------------------------
# SIM-546: every live game's closing rows
# ---------------------------------------------------------------------------
# A closing row is the last price a book posted before first pitch. At first
# pitch the live pipeline promotes, per (market, book) for game odds and per
# (player, prop stat, book) for props, the latest pre-pitch row from 'current'
# to 'closing'. The rule (the design's D1):
#
#   * Read one row per key among the rows with line_type 'current' OR
#     'closing' fetched at or before the first-pitch instant. A 'closing' row
#     sorts first: a key that holds one is done, whatever was fetched after
#     it. So a second call promotes nothing, even after a restart mid-game
#     reads up to a later instant and finds in-play 'current' rows.
#   * A key with no closing row reads its latest 'current' row whose stamp
#     the load guard keeps as a closing row: the stamp is no later than the
#     scheduled start plus 15 minutes (CLOSING_STAMP_GRACE; a row with no stamp
#     passes). The read applies the rule, so in a delayed game a book that
#     moved its line during the delay keeps its last line inside the grace
#     (the design's D4). Promote that row when the full guard keeps it.
#   * Rewrite the row's odds_hash to the hash of the same row with line_type
#     'closing'. The nightly loader's identical closing row then deduplicates
#     against it. When a row with that hash already exists under the key of the
#     dedup index, the loader got there first, and the row stays 'current'.

#: SIM-546: one pre-pitch game row per (market, book): its closing row when it
#: has one, else its latest current row stamped no later than ``$3`` (the
#: scheduled start plus the grace; NULL = no stamp check).
_CLOSING_GAME_READ_SQL = """
            SELECT DISTINCT ON (market_type, book)
                   id, source, line_type, book, market_type, is_sharp_book,
                   book_line_at, odds_hash,
                   home_ml, away_ml, draw_ml, home_spread, home_spread_ml,
                   away_spread, away_spread_ml, total_line, over_ml, under_ml
            FROM raw.game_odds
            WHERE game_pk = $1
              AND line_type IN ('current', 'closing')
              AND fetched_at <= $2
              AND (line_type = 'closing' OR $3::timestamptz IS NULL
                   OR book_line_at IS NULL OR book_line_at <= $3::timestamptz)
            ORDER BY market_type, book, (line_type = 'closing') DESC,
                     fetched_at DESC, id DESC
            """


#: SIM-546: the prop analogue: one pre-pitch row per (player, prop stat, book),
#: by the same order and the same stamp bound ``$3``.
_CLOSING_PROP_READ_SQL = """
            SELECT DISTINCT ON (player_id, prop_stat, book)
                   id, source, line_type, player_id, prop_stat, book,
                   is_sharp_book, book_line_at, odds_hash,
                   line, over_ml, under_ml
            FROM raw.prop_odds
            WHERE game_pk = $1
              AND line_type IN ('current', 'closing')
              AND fetched_at <= $2
              AND (line_type = 'closing' OR $3::timestamptz IS NULL
                   OR book_line_at IS NULL OR book_line_at <= $3::timestamptz)
            ORDER BY player_id, prop_stat, book, (line_type = 'closing') DESC,
                     fetched_at DESC, id DESC
            """


#: SIM-546: the new hashes already stored for one (game, source). The dedup
#: index of raw.game_odds is (game_pk, source, odds_hash).
_CLOSING_GAME_EXISTING_SQL = """
            SELECT source, odds_hash
            FROM raw.game_odds
            WHERE game_pk = $1 AND source = $2 AND odds_hash = ANY($3::text[])
            """


#: SIM-546: the prop analogue. The dedup index of raw.prop_odds is
#: (game_pk, player_id, source, odds_hash); the caller matches the player.
_CLOSING_PROP_EXISTING_SQL = """
            SELECT player_id, source, odds_hash
            FROM raw.prop_odds
            WHERE game_pk = $1 AND source = $2 AND odds_hash = ANY($3::text[])
            """


#: SIM-546: promote the picked rows in one statement. The line_type guard makes
#: a concurrent second call harmless: it skips a row already promoted.
_CLOSING_GAME_UPDATE_SQL = """
            UPDATE raw.game_odds AS g
            SET    line_type = 'closing', odds_hash = v.odds_hash
            FROM   unnest($1::bigint[], $2::text[]) AS v(id, odds_hash)
            WHERE  g.id = v.id AND g.line_type = 'current'
            """


#: SIM-546: the prop analogue of the update.
_CLOSING_PROP_UPDATE_SQL = """
            UPDATE raw.prop_odds AS p
            SET    line_type = 'closing', odds_hash = v.odds_hash
            FROM   unnest($1::bigint[], $2::text[]) AS v(id, odds_hash)
            WHERE  p.id = v.id AND p.line_type = 'current'
            """


#: SIM-546: the price columns of a game row and of a prop row. Postgres stores a
#: price as INTEGER. A writer may have hashed it as a float: the BettingPros
#: provider reads every price as a float (-110.0, not -110).
_GAME_PRICE_KEYS: tuple[str, ...] = (
    "home_ml",
    "away_ml",
    "draw_ml",
    "home_spread_ml",
    "away_spread_ml",
    "over_ml",
    "under_ml",
)


_PROP_PRICE_KEYS: tuple[str, ...] = ("over_ml", "under_ml")


#: SIM-546: the line columns. Postgres stores a line as FLOAT. A writer may have
#: hashed a whole line as an int (5, not 5.0).
_GAME_LINE_KEYS: tuple[str, ...] = ("home_spread", "away_spread", "total_line")


_PROP_LINE_KEYS: tuple[str, ...] = ("line",)


def _as_float(value: Any) -> Any:
    """An int price as a float; any other value unchanged."""
    if isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


def _as_int(value: Any) -> Any:
    """A whole-number float line as an int; any other value unchanged."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _hash_typings(row: Mapping[str, Any], is_prop: bool) -> list[dict[str, Any]]:
    """The four ways a writer may have typed the row's numbers before it hashed them.

    The hash writes an int as ``-110`` and a float as ``-110.000000``, so one
    price hashes two ways. The first typing is the row as read. The last is the
    BettingPros provider's typing (float prices, float lines): the typing the
    nightly loader writes.
    """
    prices = _PROP_PRICE_KEYS if is_prop else _GAME_PRICE_KEYS
    lines = _PROP_LINE_KEYS if is_prop else _GAME_LINE_KEYS
    as_read = dict(row)
    int_lines = {**as_read, **{k: _as_int(as_read[k]) for k in lines if k in as_read}}
    both = {**int_lines, **{k: _as_float(as_read[k]) for k in prices if k in as_read}}
    float_prices = {**as_read, **{k: _as_float(as_read[k]) for k in prices if k in as_read}}
    return [as_read, int_lines, both, float_prices]


def closing_hash(row: Mapping[str, Any]) -> str:
    """SIM-546: the odds_hash of ``row`` as a closing row.

    The hash is ``LiveIngestionPipeline._odds_hash`` (for a prop row, one with
    ``prop_stat``: ``_prop_odds_hash``) of the row with ``line_type`` set to
    ``'closing'``. The row's numbers are typed the way its writer typed them:
    the typing whose hash at the row's own line type equals the stored
    ``odds_hash``. A row whose stored hash matches no typing (or has no hash)
    takes the BettingPros provider's typing, the one the nightly loader writes.
    """
    is_prop = row.get("prop_stat") is not None
    hasher = _prop_odds_hash if is_prop else _odds_hash
    typings = _hash_typings(row, is_prop)
    stored = row.get("odds_hash")
    chosen = typings[-1]
    if stored is not None:
        own_type = row.get("line_type", "current")
        for typing in typings:
            if hasher({**typing, "line_type": own_type}) == stored:
                chosen = typing
                break
    return hasher({**chosen, "line_type": "closing"})


def closing_candidates(
    rows: Sequence[Mapping[str, Any]], *, scheduled_start: datetime | None
) -> list[tuple[int, str]]:
    """PURE (SIM-546): the (row id, new odds_hash) of every row to promote.

    ``rows`` are the promotion's read: one pre-pitch row per key, 'current' or
    'closing' (per (market_type, book) for game odds, per (player_id,
    prop_stat, book) for props). The read returns a key's closing row when it
    has one, else its latest current row stamped inside the grace. The
    function picks a row when it is 'current' and the load guard keeps it as a
    closing row with ``scheduled_start`` attached (``check_row``: the prices,
    and the stamp no later than the start plus 15 minutes). A 'closing' row is
    already promoted, so its key is done. The new hash is :func:`closing_hash`.
    """
    picks: list[tuple[int, str]] = []
    for row in rows:
        if row.get("line_type") != "current":
            continue
        probe = {**row, "line_type": "closing", "scheduled_start": scheduled_start}
        if check_row(probe) is not None:
            continue
        picks.append((int(row["id"]), closing_hash(row)))
    return picks


def _updated_count(result: Any) -> int:
    """The row count of an asyncpg status string such as ``'UPDATE 12'`` (else 0)."""
    try:
        return int(str(result).split()[-1])
    except (IndexError, ValueError):
        return 0


async def _promote_closing_rows(
    conn: Any,
    game_pk: int,
    first_pitch_at: datetime,
    *,
    scheduled_start: datetime | None,
    is_prop: bool,
) -> int:
    """SIM-546: one read, one existence check per source, then one update."""
    read_sql = _CLOSING_PROP_READ_SQL if is_prop else _CLOSING_GAME_READ_SQL
    stamp_bound = None if scheduled_start is None else scheduled_start + CLOSING_STAMP_GRACE
    rows = [dict(r) for r in await conn.fetch(read_sql, game_pk, first_pitch_at, stamp_bound)]
    picks = closing_candidates(rows, scheduled_start=scheduled_start)
    if not picks:
        return 0
    by_id = {int(r["id"]): r for r in rows}

    def _key(player_id: Any, source: Any, odds_hash: Any) -> tuple[Any, ...]:
        key = (str(source), str(odds_hash))
        return (int(player_id), *key) if is_prop else key

    hashes_by_source: dict[str, list[str]] = {}
    for row_id, new_hash in picks:
        hashes_by_source.setdefault(str(by_id[row_id].get("source")), []).append(new_hash)
    existing_sql = _CLOSING_PROP_EXISTING_SQL if is_prop else _CLOSING_GAME_EXISTING_SQL
    taken: set[tuple[Any, ...]] = set()
    for source, hashes in hashes_by_source.items():
        for hit in await conn.fetch(existing_sql, game_pk, source, hashes):
            taken.add(_key(hit["player_id"] if is_prop else None, hit["source"], hit["odds_hash"]))
    keep = [
        (row_id, new_hash)
        for row_id, new_hash in picks
        if _key(by_id[row_id].get("player_id"), by_id[row_id].get("source"), new_hash) not in taken
    ]
    if not keep:
        return 0
    update_sql = _CLOSING_PROP_UPDATE_SQL if is_prop else _CLOSING_GAME_UPDATE_SQL
    result = await conn.execute(update_sql, [i for i, _ in keep], [h for _, h in keep])
    return _updated_count(result)


async def promote_closing_game_rows(
    conn: Any,
    game_pk: int,
    first_pitch_at: datetime,
    *,
    scheduled_start: datetime | None,
) -> int:
    """SIM-546: promote one game's closing rows in raw.game_odds, one per (market, book).

    ``conn`` is an asyncpg pool or connection. ``first_pitch_at`` bounds the rows
    read (``fetched_at`` at or before it). ``scheduled_start`` feeds the load
    guard's closing-stamp rule (``None`` = no stamp check). Returns the rows
    promoted. A second call promotes nothing, even one with a later
    ``first_pitch_at`` (a restart mid-game): a key that holds a closing row is
    done (the rule above).
    """
    return await _promote_closing_rows(
        conn, game_pk, first_pitch_at, scheduled_start=scheduled_start, is_prop=False
    )


async def promote_closing_prop_rows(
    conn: Any,
    game_pk: int,
    first_pitch_at: datetime,
    *,
    scheduled_start: datetime | None,
) -> int:
    """SIM-546: the prop analogue: one closing row per (player, prop stat, book)."""
    return await _promote_closing_rows(
        conn, game_pk, first_pitch_at, scheduled_start=scheduled_start, is_prop=True
    )
