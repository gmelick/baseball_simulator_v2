"""Load the odds into the database.

The module holds the odds source seam, the insert statements and the odds hash, and the
closing-line stamp.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from betting.markets import GAME_ODDS_FIELDS
from betting.validate import CLOSING_STAMP_GRACE, check_row

# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


@runtime_checkable
class OddsProvider(Protocol):
    """An odds source: one row per book for a game market or a prop market.

    ``get_odds`` gives the one row the live service broadcasts.
    ``isinstance(obj, OddsProvider)`` checks only that the methods exist.
    An unknown ``market_type`` or ``prop_stat`` raises ``ValueError``.
    """

    def get_odds(
        self,
        game_pk: int,
        *,
        line_type: str = "current",
        market_type: str = "moneyline",
    ) -> dict[str, Any]:
        """The graded book's row of one game market."""
        ...

    def get_odds_by_book(
        self,
        game_pk: int,
        *,
        line_type: str = "current",
        market_type: str = "moneyline",
    ) -> list[dict[str, Any]]:
        """One row per book for one game market, with every price field."""
        ...

    def get_prop_odds_by_book(
        self,
        game_pk: int,
        player_id: int,
        prop_stat: str,
        *,
        line_type: str = "current",
    ) -> list[dict[str, Any]]:
        """One row per book for one player's prop market."""
        ...


def odds_rows_by_book(
    provider: Any,
    game_pk: int,
    *,
    line_type: str = "current",
    market_type: str = "moneyline",
) -> list[dict[str, Any]]:
    """Every book's row of one game market, from any provider.

    A provider without ``get_odds_by_book`` (a test fake) answers with its one
    ``get_odds`` row. A by-book result that is not a list or a tuple (an
    unconfigured mock object) also falls back to ``get_odds``.
    """
    by_book = getattr(provider, "get_odds_by_book", None)
    if callable(by_book):
        rows = by_book(game_pk, line_type=line_type, market_type=market_type)
        if isinstance(rows, (list, tuple)):
            return list(rows)
    return [provider.get_odds(game_pk, line_type=line_type, market_type=market_type)]


def prop_rows_by_book(
    provider: Any,
    game_pk: int,
    player_id: int,
    prop_stat: str,
    *,
    line_type: str = "current",
) -> list[dict[str, Any]]:
    """Every book's row of one player's prop market, from any provider.

    The fallback is the same as :func:`odds_rows_by_book`, to ``get_prop_odds``.
    """
    by_book = getattr(provider, "get_prop_odds_by_book", None)
    if callable(by_book):
        rows = by_book(game_pk, player_id, prop_stat, line_type=line_type)
        if isinstance(rows, (list, tuple)):
            return list(rows)
    return [provider.get_prop_odds(game_pk, player_id, prop_stat, line_type=line_type)]


# ---------------------------------------------------------------------------
# The provider registry and factory
# ---------------------------------------------------------------------------

#: The environment variable that names the provider.
ODDS_PROVIDER_ENV = "ODDS_PROVIDER"


#: The provider name when the environment variable is unset or empty.
DEFAULT_PROVIDER = "mock"


# Provider name → zero-argument factory. Each factory imports its provider late.
_PROVIDER_FACTORIES: dict[str, Callable[[], OddsProvider]] = {}


def _make_mock_provider() -> OddsProvider:
    """The factory of the mock provider.

    The import is inside the function. It avoids a circular import and keeps
    this module importable without the live pipeline's dependencies.
    """
    from pipeline.live.ingestion.mock_odds import MockOddsAPI

    return MockOddsAPI()


def register_odds_provider(name: str, factory: Callable[[], OddsProvider]) -> None:
    """Register a provider factory, so ``ODDS_PROVIDER=<name>`` selects it.

    ``factory`` takes no argument. A test uses this to inject a fake provider.
    Names match case-insensitively.
    """
    _PROVIDER_FACTORIES[name.strip().lower()] = factory


def _make_bettingpros_provider() -> OddsProvider:
    """The factory of the BettingPros provider."""
    from betting.bettingpros.fetch_odds import BettingProsOddsProvider

    return BettingProsOddsProvider()


def get_odds_provider(name: str | None = None) -> OddsProvider:
    """The selected odds provider.

    The name comes from ``name``, else ``ODDS_PROVIDER``, else ``"mock"``.
    An unregistered name raises ``RuntimeError``.
    """
    selected = (name or os.environ.get(ODDS_PROVIDER_ENV) or DEFAULT_PROVIDER).strip().lower()
    factory = _PROVIDER_FACTORIES.get(selected)
    if factory is None:
        raise RuntimeError(
            f"Unknown ODDS_PROVIDER '{selected}'. Registered providers: "
            f"{', '.join(sorted(_PROVIDER_FACTORIES))}. Set {ODDS_PROVIDER_ENV} to one "
            "of these or register one via register_odds_provider()."
        )
    return factory()


# The built-in providers.
register_odds_provider("mock", _make_mock_provider)
register_odds_provider("bettingpros", _make_bettingpros_provider)


# ---------------------------------------------------------------------------
# Odds rows and the database
# ---------------------------------------------------------------------------

#: The odds fields of a prop row; a row with all three empty is not a quote.
_PROP_ODDS_FIELDS: tuple[str, ...] = ("line", "over_ml", "under_ml")


def _stamp_param(value: Any) -> datetime | None:
    """A row's ``book_line_at`` as an aware datetime for the TIMESTAMPTZ column.

    A naive datetime is read as UTC (the vendor stamps in UTC); ISO-8601 text is
    parsed; anything else (``None``, a mock row without the key) gives ``None``.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _game_row_has_odds(row: Mapping[str, Any]) -> bool:
    """True when a game-odds row carries at least one price or line."""
    return any(row.get(f) is not None for f in GAME_ODDS_FIELDS)


def _prop_row_has_odds(row: Mapping[str, Any]) -> bool:
    """True when a prop-odds row carries a line or a price."""
    return any(row.get(f) is not None for f in _PROP_ODDS_FIELDS)


#: The raw.game_odds INSERT. ``odds_hash`` stays the LAST parameter (two pinned
#: tests read the hash as the final argument).
_GAME_ODDS_INSERT_SQL = """
            INSERT INTO raw.game_odds
                (game_pk, source, is_mock,
                 book, line_type, market_type, is_sharp_book,
                 home_ml, away_ml,
                 home_spread, home_spread_ml, away_spread, away_spread_ml,
                 total_line, over_ml, under_ml,
                 draw_ml, book_line_at, odds_hash)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
            ON CONFLICT (game_pk, source, odds_hash)
              WHERE odds_hash IS NOT NULL
              DO NOTHING
            """


#: The raw.prop_odds INSERT; ``odds_hash`` stays last.
_PROP_ODDS_INSERT_SQL = """
            INSERT INTO raw.prop_odds
                (game_pk, player_id, source, is_mock,
                 prop_stat, line, over_ml, under_ml,
                 book, line_type, is_sharp_book, book_line_at, odds_hash)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            ON CONFLICT (game_pk, player_id, source, odds_hash)
              WHERE odds_hash IS NOT NULL
              DO NOTHING
            """


#: The live writers' form. A row the book still posts (the same
#: hash) is re-stamped instead of skipped, so the pre-game read knows the
#: price is current. Needs Alembic 0029 (``last_seen_at``).
_GAME_ODDS_RESTAMP_SQL = _GAME_ODDS_INSERT_SQL.replace(
    "DO NOTHING", "DO UPDATE SET last_seen_at = NOW()"
)


_PROP_ODDS_RESTAMP_SQL = _PROP_ODDS_INSERT_SQL.replace(
    "DO NOTHING", "DO UPDATE SET last_seen_at = NOW()"
)


def _odds_hash(odds: dict) -> str:
    """
    Deterministic SHA-256 fingerprint of an odds payload.

    Hashing rules:
      * Stable key order regardless of dict iteration order.
      * Numeric values are formatted with full precision so 1.5 and 1.50
        collide (they should — same line).
      * book / line_type / market_type / is_sharp_book are part of the
        hash because two books at the same price are still two distinct
        quotes.  source is NOT in the hash (it lives outside the unique
        index, on the table itself, paired with odds_hash by the index).

    Identical payloads produce identical hashes; an INSERT … ON CONFLICT
    (game_pk, source, odds_hash) DO NOTHING is therefore a no-op for any
    snapshot the pipeline has already written.
    """
    import hashlib

    # Keys in fixed order — do NOT change order without bumping the hash
    # space (would invalidate every previously stored hash, but the
    # partial unique index tolerates NULL so a re-fingerprint is safe).
    ordered_keys = (
        "book",
        "line_type",
        "market_type",
        "is_sharp_book",
        "home_ml",
        "away_ml",
        "home_spread",
        "home_spread_ml",
        "away_spread",
        "away_spread_ml",
        "total_line",
        "over_ml",
        "under_ml",
    )

    def _fmt(v: object) -> str:
        # Normalize numeric types so 1.5 and 1.50 hash identically;
        # bool / None / strings stringify directly.
        if v is None:
            return "∅"
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, float):
            # 6-decimal precision is plenty for American odds and lines.
            return f"{v:.6f}"
        return str(v)

    payload = "|".join(f"{k}={_fmt(odds.get(k))}" for k in ordered_keys)
    # The tie price of a three-way segment moneyline joins the hash ONLY when
    # it is set, so a two-way market's payload (and every hash stored for one)
    # is the same as a row without the column.
    if odds.get("draw_ml") is not None:
        payload += f"|draw_ml={_fmt(odds.get('draw_ml'))}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _prop_odds_hash(prop: dict) -> str:
    """
    Deterministic SHA-256 fingerprint of a prop-odds payload —
    the prop analogue of _odds_hash().

    Identical prop snapshots produce identical hashes, so an
    INSERT … ON CONFLICT (game_pk, player_id, source, odds_hash) DO NOTHING
    is a no-op for any snapshot already written.  This is what makes the
    live cadence safe: even if the cadence gate is loosened, the dedup hash
    collapses unchanged lines instead of accumulating a row per WS signal.

    prop_stat / book / line_type / is_sharp_book are part of the hash because
    two books (or the over vs the under, or current vs opening) at the same
    line are still distinct quotes.  source lives outside the hash, paired
    with it in the unique index.
    """
    import hashlib

    ordered_keys = (
        "player_id",
        "prop_stat",
        "book",
        "line_type",
        "is_sharp_book",
        "line",
        "over_ml",
        "under_ml",
    )

    def _fmt(v: object) -> str:
        if v is None:
            return "∅"
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, float):
            return f"{v:.6f}"
        return str(v)

    payload = "|".join(f"{k}={_fmt(prop.get(k))}" for k in ordered_keys)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def game_odds_insert_args(game_pk: int, odds: Mapping[str, Any]) -> tuple[Any, ...]:
    """The bind values of one raw.game_odds INSERT, in ``_GAME_ODDS_INSERT_SQL`` order.

    ``book_line_at`` (the vendor's stamp on the row's line) sits just
    before ``odds_hash``, which stays LAST: two pinned tests read the hash as
    the final argument. The stamp is not in the hash, so an unchanged line
    re-fetched later still deduplicates. The live pipeline, the historical
    loader and the opening-line job all bind through this one function, so
    every writer sends the hash the live schema requires (``odds_hash`` is
    NOT NULL on raw.game_odds since migration 0012).
    """
    return (
        game_pk,
        odds.get("source", "mock"),
        odds.get("is_mock", True),
        odds.get("book", "consensus"),
        odds.get("line_type", "current"),
        odds.get("market_type", "moneyline"),
        odds.get("is_sharp_book", False),
        odds.get("home_ml"),
        odds.get("away_ml"),
        odds.get("home_spread"),
        odds.get("home_spread_ml"),
        odds.get("away_spread"),
        odds.get("away_spread_ml"),
        odds.get("total_line"),
        odds.get("over_ml"),
        odds.get("under_ml"),
        odds.get("draw_ml"),  # The tie price of a three-way segment moneyline
        _stamp_param(odds.get("book_line_at")),  # The vendor's stamp
        _odds_hash(dict(odds)),  # stays LAST: two pinned tests read it last
    )


def prop_odds_insert_args(prop: Mapping[str, Any]) -> tuple[Any, ...]:
    """The bind values of one raw.prop_odds INSERT, in ``_PROP_ODDS_INSERT_SQL`` order.

    ``book_line_at`` sits just before ``odds_hash``, which stays last;
    the stamp is not in the hash. Every prop writer binds through this function.
    """
    return (
        prop["game_pk"],
        prop["player_id"],
        prop.get("source", "mock"),
        prop.get("is_mock", True),
        prop["prop_stat"],
        prop["line"],
        prop.get("over_ml"),
        prop.get("under_ml"),
        prop.get("book", "consensus"),
        prop.get("line_type", "current"),
        prop.get("is_sharp_book", False),
        _stamp_param(prop.get("book_line_at")),  # The vendor's stamp
        _prop_odds_hash(dict(prop)),  # stays LAST
    )


async def insert_game_odds_rows(
    conn: Any, game_pk: int, rows: Sequence[Mapping[str, Any]], *, restamp: bool = False
) -> int:
    """Insert game-odds rows in one ``executemany``, with the dedup.

    ``conn`` is an asyncpg pool or connection. The SQL is the live writer's
    (``odds_hash`` last, ``ON CONFLICT ... DO NOTHING``). Returns the number of
    rows sent (the dedup may insert fewer). An empty list sends nothing.
    ``restamp`` re-stamps ``last_seen_at`` on a row already
    stored (the live writers, once Alembic 0029 is applied).
    """
    if not rows:
        return 0
    sql = _GAME_ODDS_RESTAMP_SQL if restamp else _GAME_ODDS_INSERT_SQL
    await conn.executemany(sql, [game_odds_insert_args(game_pk, row) for row in rows])
    return len(rows)


async def insert_prop_odds_rows(
    conn: Any, rows: Sequence[Mapping[str, Any]], *, restamp: bool = False
) -> int:
    """Insert prop-odds rows in one ``executemany``, with the dedup.

    The prop analogue of :func:`insert_game_odds_rows`. Returns the rows sent.
    """
    if not rows:
        return 0
    sql = _PROP_ODDS_RESTAMP_SQL if restamp else _PROP_ODDS_INSERT_SQL
    await conn.executemany(sql, [prop_odds_insert_args(row) for row in rows])
    return len(rows)


# ---------------------------------------------------------------------------
# Every live game's closing rows
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

#: One pre-pitch game row per (market, book): its closing row when it
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


#: The prop analogue: one pre-pitch row per (player, prop stat, book),
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


#: The new hashes already stored for one (game, source). The dedup
#: index of raw.game_odds is (game_pk, source, odds_hash).
_CLOSING_GAME_EXISTING_SQL = """
            SELECT source, odds_hash
            FROM raw.game_odds
            WHERE game_pk = $1 AND source = $2 AND odds_hash = ANY($3::text[])
            """


#: The prop analogue. The dedup index of raw.prop_odds is
#: (game_pk, player_id, source, odds_hash); the caller matches the player.
_CLOSING_PROP_EXISTING_SQL = """
            SELECT player_id, source, odds_hash
            FROM raw.prop_odds
            WHERE game_pk = $1 AND source = $2 AND odds_hash = ANY($3::text[])
            """


#: Promote the picked rows in one statement. The line_type guard makes
#: a concurrent second call harmless: it skips a row already promoted.
_CLOSING_GAME_UPDATE_SQL = """
            UPDATE raw.game_odds AS g
            SET    line_type = 'closing', odds_hash = v.odds_hash
            FROM   unnest($1::bigint[], $2::text[]) AS v(id, odds_hash)
            WHERE  g.id = v.id AND g.line_type = 'current'
            """


#: The prop analogue of the update.
_CLOSING_PROP_UPDATE_SQL = """
            UPDATE raw.prop_odds AS p
            SET    line_type = 'closing', odds_hash = v.odds_hash
            FROM   unnest($1::bigint[], $2::text[]) AS v(id, odds_hash)
            WHERE  p.id = v.id AND p.line_type = 'current'
            """


#: The price columns of a game row and of a prop row. Postgres stores a
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


#: The line columns. Postgres stores a line as FLOAT. A writer may have
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
    """The odds_hash of ``row`` as a closing row.

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
    """PURE: the (row id, new odds_hash) of every row to promote.

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
    """One read, one existence check per source, then one update."""
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
    """Promote one game's closing rows in raw.game_odds, one per (market, book).

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
    """The prop analogue: one closing row per (player, prop stat, book)."""
    return await _promote_closing_rows(
        conn, game_pk, first_pitch_at, scheduled_start=scheduled_start, is_prop=True
    )
