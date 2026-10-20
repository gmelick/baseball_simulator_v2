"""The odds rows the live cycle writes: the JSON form of an offer, the
staleness test, the insert statements and their arguments.
"""

from __future__ import annotations

from collections.abc import (
    Mapping,
    Sequence,
)
from datetime import (
    UTC,
    date,
    datetime,
)
from typing import Any

from pipeline.live.ingestion.constants import (
    PREGAME_NEAR_START_CADENCE_S,
    PREGAME_NEAR_START_WINDOW_S,
    PREGAME_ODDS_CADENCE_S,
)
from pipeline.odds_provider import GAME_ODDS_FIELDS

# ---------------------------------------------------------------------------
# SIM-555: odds rows, JSON and the database
# ---------------------------------------------------------------------------

#: The by-book row keys that serve the load guard and the logs only. They are
#: never stored and never sent to a browser: the scheduled start (a datetime),
#: the official date and, SIM-555, a made-up game's postponed original start.
_GUARD_ONLY_KEYS = frozenset({"scheduled_start", "game_date", "postponed_start"})

#: The odds fields of a prop row; a row with all three empty is not a quote.
_PROP_ODDS_FIELDS: tuple[str, ...] = ("line", "over_ml", "under_ml")


def _json_default(value: Any) -> str:
    """The ``json.dumps`` fallback: a date or a datetime as ISO-8601 text (SIM-555).

    The BettingPros rows carry ``book_line_at`` as a datetime; a bare
    ``json.dumps`` raises ``TypeError`` on it. Any other unknown type still
    raises, so a real bug stays loud.
    """
    if isinstance(value, date):  # a datetime is a date too
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _jsonable_odds(odds: Mapping[str, Any] | None) -> dict[str, Any]:
    """An odds row made safe for ``json.dumps`` (SIM-555).

    The row loses the guard-only keys (``scheduled_start``, ``game_date``,
    ``postponed_start``) and sends a date or a datetime (``book_line_at``) as
    ISO-8601 text. Every other key passes through unchanged. ``None`` gives an
    empty dict.
    """
    out: dict[str, Any] = {}
    for key, value in (odds or {}).items():
        if key in _GUARD_ONLY_KEYS:
            continue
        out[key] = value.isoformat() if isinstance(value, date) else value
    return out


def _odds_facts_may_be_stale(game: Mapping[str, Any]) -> bool:
    """True when the odds provider's cached facts of a schedule entry's game may be old (SIM-555).

    The provider keeps a found game's schedule facts and vendor event for the
    process lifetime. A game looked up before a postponement keeps its
    original start and event. Three kinds of entry say the facts changed:

      * the original date's entry of a postponed or suspended game
        (``rescheduleGameDate`` / ``resumeGameDate``);
      * a postponed entry not yet rescheduled (``detailedState`` starting
        "Postponed", or ``codedGameState`` "D", as the provider reads it);
      * a made-up or resumed game before its first pitch (``rescheduledFrom`` /
        ``resumedFrom`` while ``Preview``). The poll reads one calendar date's
        schedule (``date.today()``, UTC in the container), so it misses the
        original date's entry of a game postponed after midnight UTC; this
        entry catches that game on its new date.

    The poll drops the cached facts on each such entry. A live game keeps
    them, so the per-pitch refresh reads nothing again.
    """
    if "rescheduleGameDate" in game or "resumeGameDate" in game:
        return True
    status = game.get("status") or {}
    if str(status.get("detailedState") or "").startswith("Postponed"):
        return True
    if status.get("codedGameState") == "D":
        return True
    made_up = "rescheduledFrom" in game or "resumedFrom" in game
    return made_up and status.get("abstractGameState") == "Preview"


def _stamp_param(value: Any) -> datetime | None:
    """A row's ``book_line_at`` as an aware datetime for the TIMESTAMPTZ column (SIM-555).

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


def _scheduled_start(game: Mapping[str, Any]) -> datetime | None:
    """SIM-546: a schedule entry's scheduled start as an aware UTC datetime.

    The entry's ``gameDate`` is ISO 8601 UTC text ("2026-10-09T23:05:00Z").
    A missing or unreadable value gives ``None``.
    """
    return _stamp_param(game.get("gameDate"))


def pregame_odds_cadence_s(game: Mapping[str, Any], now: datetime) -> int:
    """PURE (SIM-546): the pre-game cycle's cadence in seconds for one schedule entry.

    Inside PREGAME_NEAR_START_WINDOW_S of the scheduled start, or past the
    start while the game still waits in ``Preview`` (a delay), the cadence is
    PREGAME_NEAR_START_CADENCE_S (60 s). Further out, or with no readable
    start, it is PREGAME_ODDS_CADENCE_S (600 s). ``now`` is an aware datetime.
    """
    start = _scheduled_start(game)
    if start is None:
        return PREGAME_ODDS_CADENCE_S
    if (start - now).total_seconds() <= PREGAME_NEAR_START_WINDOW_S:
        return PREGAME_NEAR_START_CADENCE_S
    return PREGAME_ODDS_CADENCE_S


def _game_row_has_odds(row: Mapping[str, Any]) -> bool:
    """True when a game-odds row carries at least one price or line."""
    return any(row.get(f) is not None for f in GAME_ODDS_FIELDS)


def _prop_row_has_odds(row: Mapping[str, Any]) -> bool:
    """True when a prop-odds row carries a line or a price."""
    return any(row.get(f) is not None for f in _PROP_ODDS_FIELDS)


#: The raw.game_odds INSERT. SIM-555 adds ``book_line_at``; ``odds_hash`` stays
#: the LAST parameter (two pinned tests read the hash as the final argument).
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

#: The raw.prop_odds INSERT. SIM-555 adds ``book_line_at``; ``odds_hash`` stays last.
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


#: SIM-519 Part G: the live writers' form. A row the book still posts (the same
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
    SIM-092: Deterministic SHA-256 fingerprint of an odds payload.

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
    # SIM-421 (2026-09-12): the tie price of a three-way segment moneyline
    # joins the hash ONLY when it is set. A two-way market's payload is
    # therefore byte-identical to what it was before the column existed,
    # so every hash stored before this change still deduplicates.
    if odds.get("draw_ml") is not None:
        payload += f"|draw_ml={_fmt(odds.get('draw_ml'))}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _prop_odds_hash(prop: dict) -> str:
    """
    SIM-340: Deterministic SHA-256 fingerprint of a prop-odds payload —
    the prop analogue of _odds_hash() (SIM-092).

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

    SIM-555: ``book_line_at`` (the vendor's stamp on the row's line) sits just
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
        odds.get("draw_ml"),  # SIM-421: the tie price of a three-way segment moneyline
        _stamp_param(odds.get("book_line_at")),  # SIM-555: the vendor's stamp
        _odds_hash(dict(odds)),  # stays LAST: two pinned tests read it last
    )


def prop_odds_insert_args(prop: Mapping[str, Any]) -> tuple[Any, ...]:
    """The bind values of one raw.prop_odds INSERT, in ``_PROP_ODDS_INSERT_SQL`` order.

    SIM-555: ``book_line_at`` sits just before ``odds_hash``, which stays last;
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
        _stamp_param(prop.get("book_line_at")),  # SIM-555: the vendor's stamp
        _prop_odds_hash(dict(prop)),  # stays LAST
    )


async def insert_game_odds_rows(
    conn: Any, game_pk: int, rows: Sequence[Mapping[str, Any]], *, restamp: bool = False
) -> int:
    """SIM-555: insert game-odds rows in one ``executemany``, with the dedup.

    ``conn`` is an asyncpg pool or connection. The SQL is the live writer's
    (``odds_hash`` last, ``ON CONFLICT ... DO NOTHING``). Returns the number of
    rows sent (the dedup may insert fewer). An empty list sends nothing.
    SIM-519 Part G: ``restamp`` re-stamps ``last_seen_at`` on a row already
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
    """SIM-555: insert prop-odds rows in one ``executemany``, with the dedup.

    The prop analogue of :func:`insert_game_odds_rows`. Returns the rows sent.
    """
    if not rows:
        return 0
    sql = _PROP_ODDS_RESTAMP_SQL if restamp else _PROP_ODDS_INSERT_SQL
    await conn.executemany(sql, [prop_odds_insert_args(row) for row in rows])
    return len(rows)
