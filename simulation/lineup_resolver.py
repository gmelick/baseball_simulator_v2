"""simulation.lineup_resolver — runtime lineup / substitution resolver (SIM-353).

WHAT THIS IS
============
The SIM-310 simulation loop is built against the :class:`GameState` contract
(``simulation.game_state``) whose lineup fields are *ordered batting orders plus
a per-side slot pointer*::

    away_lineup: list[int]       # away batting order (slot 1..9 -> player ids)
    home_lineup: list[int]       # home batting order
    away_lineup_slot: int = 0    # 0-based pointer into away_lineup
    home_lineup_slot: int = 0    # 0-based pointer into home_lineup
    batter_id: Optional[int]     # the batter due up (lineup[slot] for the offense)

Nothing in the loop READS the lineup stores to BUILD one of those (the SIM-338
gap, confirmed by the Phase-5 audit).  This module closes that gap.

The lineup data lives in **Postgres** (the rest of the loop reads DuckDB):

  * ``raw.game_lineups`` — one row per (game_pk, team_id, player_id, sequence).
    ``sequence`` is 1 for the original entry and increments each time a batting
    slot changes occupant (a substitution); ``entered_inning`` /
    ``entered_at_bat`` record WHEN a sub entered; ``is_starter`` flags the
    opening-day occupant; ``pinch_role`` is 'PH'/'PR'/'DEF' for replacements;
    ``batting_order`` is the 1..9 slot (NULL for a pitcher in a DH lineup who
    never bats, e.g. an AL pitcher).
  * ``raw.games`` — maps ``team_id`` to the home/away side and carries
    ``season`` (a sampler pre-filter key on ``GameState``).
  * ``raw.players`` — ``bats`` / ``throws`` give the batter hand / pitcher
    throwing hand the sampler pre-filter needs.

DESIGN — DB ACCESS IS SEPARATED FROM GAMESTATE BUILDING
=======================================================
So the GameState-building logic is unit-testable without a live DB, the module
is layered into three pieces:

  1. **Pure value types** (:class:`LineupSlot`, :class:`TeamLineup`,
     :class:`ResolvedLineup`) — no I/O.
  2. **Pure assembly** — :func:`resolve_lineup_from_rows` takes *canned rows*
     (the same dict shape asyncpg ``Record`` exposes) plus the team→side map and
     the hand map, applies the substitution rules, and returns a
     :class:`ResolvedLineup`.  :func:`build_game_state` maps a
     :class:`ResolvedLineup` into a :class:`GameState` via GameState's existing
     public constructor / fields only.  Neither touches a DB.
  3. **DB access** — :func:`fetch_game_sides`, :func:`fetch_lineup_rows`,
     :func:`fetch_player_hands` issue the asyncpg queries; the async orchestrator
     :func:`resolve_lineup` wires (3) -> (2).

A unit test drives layer (2) with a fake connection / canned rows; only an
integration test needs a live Postgres.

SUBSTITUTIONS
=============
For each batting-order slot the *current occupant* is the row with the highest
``sequence`` among the rows for that slot whose substitution had taken effect by
the requested point in time:

  * ``as_of_at_bat=None`` (default) -> the latest known occupant of every slot
    (whatever the most recent substitution left in place).
  * ``as_of_at_bat=N`` -> only substitutions with ``entered_at_bat <= N`` (or a
    NULL ``entered_at_bat``, treated as "in from the start") count, so a what-if
    simulation can rewind to the lineup as it stood at at-bat N.

The starter (``is_starter=TRUE``, ``sequence=1``) is the slot's occupant until a
later-sequence sub displaces it.  A pinch *runner* ('PR') does not change who is
*due up to bat* in that slot for the rest of the game — only PH / DEF / a new
position player does — but in ``raw.game_lineups`` every slot-occupant change is
a new row, so "highest applicable sequence wins" is the correct rule for the
*batting order* this resolver produces.  The current pitcher is resolved
separately (the highest-sequence row whose ``position_code`` is the pitcher slot)
so a mid-game pitching change is reflected in ``GameState.pitcher_id``.

This module is owned by the Data Engineer (SIM-353).  It does NOT mutate
``GameState`` after construction (the SIM-316 state machine owns mutation) and it
does NOT edit ``simulation.game_state`` — it only constructs a GameState through
that module's public surface.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from simulation.game_state import GameState, Half, Team

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: position_code values that denote the pitcher slot in raw.game_lineups.
#: '1' is the MLB scoring/position number for pitcher; 'P'/'SP'/'RP' cover the
#: abbreviation styles the live ingestion path may write.
PITCHER_POSITION_CODES = frozenset({"1", "P", "SP", "RP"})

#: MLB scoring position code (raw.game_lineups.position_code) -> the canonical
#: defensive-slot name FieldSnapshot keys on (snapshots.DEFENSE_POSITIONS).  The
#: nine fielding positions are numbered 1..9 in scorebook order; codes outside
#: this set (e.g. '10'/'DH' for a designated hitter, or the pinch roles
#: 'PH'/'PR'/'DEF') are NOT fielding positions and are skipped by the builder.
#: Mirrors snapshots.POSITION_NUMBER (name -> number); this is its inverse keyed
#: by the *string* code raw.game_lineups stores.  Kept here (not imported from
#: snapshots) so the resolver has no dependency on the API/contract layer.
POSITION_CODE_TO_NAME: dict[str, str] = {
    "1": "P",
    "2": "C",
    "3": "1B",
    "4": "2B",
    "5": "3B",
    "6": "SS",
    "7": "LF",
    "8": "CF",
    "9": "RF",
}

#: The canonical defensive-slot names, in scorebook order (1=P .. 9=RF).  Equal
#: to snapshots.DEFENSE_POSITIONS; duplicated here to keep this module free of an
#: import from the contract layer.
DEFENSE_POSITION_NAMES: tuple[str, ...] = tuple(POSITION_CODE_TO_NAME.values())

#: The number of batting-order slots in a standard lineup.
LINEUP_SLOTS = 9

#: Default batter hand when raw.players has no hand for a player (defensive).
#: 'R' is the league-modal hand; the sampler treats it as a pre-filter key only.
DEFAULT_BAT_HAND = "R"


class LineupResolutionError(ValueError):
    """Raised when a lineup cannot be resolved into a usable GameState.

    Subclasses :class:`ValueError` so callers that already guard the SIM-326
    invalid-state path catch it without a new except clause.
    """


class LineupNotIngestedError(LineupResolutionError):
    """Raised when the game exists but ``raw.game_lineups`` has no rows yet.

    Distinct from the parent :class:`LineupResolutionError` (which covers
    "game not found in raw.games") so callers can distinguish a permanent
    failure (game unknown → 404) from a transient one (lineup not yet
    published by MLB → 503 Retry-After, typically 15 min before first pitch).
    """


# ---------------------------------------------------------------------------
# Pure value types (no I/O)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LineupSlot:
    """One resolved occupant of one batting-order slot for one team.

    ``batting_order`` is the 1..9 slot.  ``player_id`` is the *current* occupant
    after substitutions have been applied for the requested point in time.
    ``is_substitution`` is True when a later-sequence row (not the starter) is
    the occupant; ``sequence`` / ``pinch_role`` carry the provenance.
    """

    batting_order: int
    player_id: int
    position_code: str
    is_starter: bool
    sequence: int
    pinch_role: str | None = None

    @property
    def is_substitution(self) -> bool:
        """True when this occupant arrived via a substitution (not the starter)."""
        return not self.is_starter or self.sequence > 1


@dataclass(frozen=True)
class TeamLineup:
    """A resolved batting order for one side, plus the current pitcher.

    ``slots`` is sorted by ``batting_order`` (1..9).  ``batting_order_ids`` is the
    ordered list of player ids the GameState ``*_lineup`` field expects.
    """

    team_id: int
    slots: tuple[LineupSlot, ...]
    pitcher_id: int | None

    @property
    def batting_order_ids(self) -> list[int]:
        """The ordered list of batter ids (slot 1..9) for ``GameState.*_lineup``."""
        return [s.player_id for s in self.slots]


@dataclass
class ResolvedLineup:
    """Both sides' resolved lineups for a game, ready to build a GameState.

    This is the DB-free intermediate the builder consumes; an integration test
    that *does* hit Postgres asserts against this, and a unit test constructs it
    directly to exercise :func:`build_game_state` in isolation.
    """

    game_pk: int
    season: int
    home: TeamLineup
    away: TeamLineup
    #: player_id -> 'L'/'R'/'S' batting hand (from raw.players.bats).
    bat_hands: dict[int, str] = field(default_factory=dict)
    #: player_id -> 'L'/'R'/'S' throwing hand (from raw.players.throws).
    throw_hands: dict[int, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure assembly — canned rows -> ResolvedLineup (no DB)
# ---------------------------------------------------------------------------


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from an asyncpg ``Record`` or a plain dict uniformly.

    asyncpg Records support ``row[key]`` and ``.get`` is dict-only, so we go
    through ``__getitem__`` with a KeyError guard to accept both.
    """
    try:
        val = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if val is None else val


def _occupant_takes_effect(row: Any, as_of_at_bat: int | None) -> bool:
    """Whether a lineup row's occupant has taken effect by ``as_of_at_bat``.

    ``as_of_at_bat=None`` -> always (use the latest occupant of every slot).
    Otherwise a row counts only if it entered at or before ``as_of_at_bat``; a
    NULL ``entered_at_bat`` is treated as "in from the start" (the starter, or a
    sub whose at-bat the source didn't record) so it always counts.
    """
    if as_of_at_bat is None:
        return True
    entered = _row_get(row, "entered_at_bat")
    if entered is None:
        return True
    return int(entered) <= int(as_of_at_bat)


def _pick_team_slots(
    rows: Iterable[Mapping[str, Any]],
    *,
    as_of_at_bat: int | None,
) -> tuple[list[LineupSlot], int | None]:
    """Reduce one team's raw rows to (ordered batting slots, current pitcher id).

    For each ``batting_order`` slot, the occupant is the row with the highest
    ``sequence`` whose substitution has taken effect by ``as_of_at_bat``.  The
    pitcher is resolved independently as the highest-sequence pitcher-position
    row (so a mid-game pitching change wins) regardless of batting_order — an AL
    pitcher has no batting slot.
    """
    # batting_order -> the winning row so far (highest applicable sequence).
    best_by_slot: dict[int, Any] = {}
    # the winning pitcher row so far (highest applicable sequence).
    best_pitcher: Any | None = None

    for row in rows:
        if not _occupant_takes_effect(row, as_of_at_bat):
            continue
        seq = int(_row_get(row, "sequence", 1) or 1)

        # Pitcher resolution (independent of batting_order).
        pos = str(_row_get(row, "position_code", "") or "")
        if pos in PITCHER_POSITION_CODES:
            if best_pitcher is None or seq > int(_row_get(best_pitcher, "sequence", 1) or 1):
                best_pitcher = row

        # Batting-slot resolution (skip rows with no batting_order — e.g. an AL
        # pitcher who never bats, or a defensive replacement recorded w/o a slot).
        bo = _row_get(row, "batting_order")
        if bo is None:
            continue
        bo = int(bo)
        cur = best_by_slot.get(bo)
        if cur is None or seq > int(_row_get(cur, "sequence", 1) or 1):
            best_by_slot[bo] = row

    slots = [
        LineupSlot(
            batting_order=bo,
            player_id=int(_row_get(best_by_slot[bo], "player_id")),
            position_code=str(_row_get(best_by_slot[bo], "position_code", "") or ""),
            is_starter=bool(_row_get(best_by_slot[bo], "is_starter", False)),
            sequence=int(_row_get(best_by_slot[bo], "sequence", 1) or 1),
            pinch_role=_row_get(best_by_slot[bo], "pinch_role"),
        )
        for bo in sorted(best_by_slot)
    ]

    pitcher_id = int(_row_get(best_pitcher, "player_id")) if best_pitcher is not None else None
    return slots, pitcher_id


def resolve_lineup_from_rows(
    *,
    game_pk: int,
    season: int,
    home_team_id: int,
    away_team_id: int,
    lineup_rows: Iterable[Mapping[str, Any]],
    bat_hands: Mapping[int, str] | None = None,
    throw_hands: Mapping[int, str] | None = None,
    as_of_at_bat: int | None = None,
) -> ResolvedLineup:
    """Assemble a :class:`ResolvedLineup` from raw ``raw.game_lineups`` rows.

    Pure (no I/O): the same logic an integration test exercises against live
    Postgres and a unit test exercises with canned rows.  ``lineup_rows`` is an
    iterable of mappings with at least:
    ``team_id, player_id, batting_order, position_code, is_starter, sequence,
    entered_at_bat, pinch_role`` (the ``raw.game_lineups`` columns).

    Rows whose ``team_id`` is neither side are ignored.  Substitution semantics
    are documented on :func:`_occupant_takes_effect` / :func:`_pick_team_slots`.

    Raises
    ------
    LineupResolutionError
        If neither side has any batting-order slot after resolution (an empty /
        missing lineup the loop cannot run against).
    """
    home_rows: list[Mapping[str, Any]] = []
    away_rows: list[Mapping[str, Any]] = []
    for row in lineup_rows:
        tid = _row_get(row, "team_id")
        if tid is None:
            continue
        tid = int(tid)
        if tid == home_team_id:
            home_rows.append(row)
        elif tid == away_team_id:
            away_rows.append(row)
        # rows for other teams are silently ignored.

    home_slots, home_pitcher = _pick_team_slots(home_rows, as_of_at_bat=as_of_at_bat)
    away_slots, away_pitcher = _pick_team_slots(away_rows, as_of_at_bat=as_of_at_bat)

    if not home_slots and not away_slots:
        raise LineupResolutionError(
            f"no batting-order rows resolved for game_pk={game_pk} "
            f"(home_team_id={home_team_id}, away_team_id={away_team_id}); "
            "raw.game_lineups has no usable lineup for this game."
        )

    home = TeamLineup(team_id=home_team_id, slots=tuple(home_slots), pitcher_id=home_pitcher)
    away = TeamLineup(team_id=away_team_id, slots=tuple(away_slots), pitcher_id=away_pitcher)

    return ResolvedLineup(
        game_pk=game_pk,
        season=season,
        home=home,
        away=away,
        bat_hands=dict(bat_hands or {}),
        throw_hands=dict(throw_hands or {}),
    )


# ---------------------------------------------------------------------------
# Pure GameState building — ResolvedLineup -> GameState (no DB)
# ---------------------------------------------------------------------------


def build_game_state(
    resolved: ResolvedLineup,
    *,
    half: Half = Half.TOP,
    seed: int | None = None,
) -> GameState:
    """Build a fresh, top-of-the-1st :class:`GameState` from a resolved lineup.

    Populates the GameState lineup contract via GameState's *public fields only*
    (this module never mutates the GameState dataclass definition):

      * ``home_lineup`` / ``away_lineup`` — ordered batting orders (slot 1..9).
      * ``home_lineup_slot`` / ``away_lineup_slot`` — both 0 (top of the 1st;
        the SIM-316 state machine advances the pointers from here).
      * ``pitcher_id`` — the *defending* team's current pitcher for the opening
        half (home pitches in the top of the 1st).
      * ``batter_id`` — the leadoff hitter of the *offense* (away in the top).
      * ``bat_hand`` / ``throw_hand`` — sampler pre-filter keys from the hand
        maps (default 'R' when a hand is unknown).
      * ``season`` — the sampler pre-filter season from the resolved lineup.

    The required GameState identity fields (``pitcher_id``, ``bat_hand``,
    ``season``) are passed to the constructor; everything else defaults to a
    fresh game and is then filled with the lineup ids.

    Raises
    ------
    LineupResolutionError
        If the offense (the side batting in ``half``) has no batting order, or
        the defense has no resolvable pitcher — the loop cannot start a PA.
    """
    offense_team = Team.AWAY if half == Half.TOP else Team.HOME
    defense = resolved.home if offense_team == Team.AWAY else resolved.away

    away_ids = resolved.away.batting_order_ids
    home_ids = resolved.home.batting_order_ids
    offense_ids = away_ids if offense_team == Team.AWAY else home_ids

    if not offense_ids:
        raise LineupResolutionError(
            f"offense ({offense_team.name}) has an empty batting order for "
            f"game_pk={resolved.game_pk}; cannot put a leadoff batter up."
        )

    pitcher_id = defense.pitcher_id
    if pitcher_id is None:
        raise LineupResolutionError(
            f"defense ({defense.team_id}) has no resolvable pitcher for "
            f"game_pk={resolved.game_pk}; cannot start a plate appearance."
        )

    leadoff_batter = offense_ids[0]
    bat_hand = resolved.bat_hands.get(leadoff_batter, DEFAULT_BAT_HAND)
    throw_hand = resolved.throw_hands.get(pitcher_id)

    state = GameState(
        pitcher_id=pitcher_id,
        bat_hand=bat_hand,
        season=resolved.season,
        half=half,
        seed=seed,
    )
    # Populate the lineup contract through the public fields.
    state.home_lineup = list(home_ids)
    state.away_lineup = list(away_ids)
    state.home_lineup_slot = 0
    state.away_lineup_slot = 0
    state.batter_id = leadoff_batter
    state.throw_hand = throw_hand
    # SIM-421: retain the per-batter hand map + both starters so the matchup
    # pre-filter follows the lineup (batter hand per PA, pitcher per half).
    state.bat_hands = dict(resolved.bat_hands)
    state.throw_hands = dict(resolved.throw_hands)
    state.home_pitcher_id = resolved.home.pitcher_id
    state.away_pitcher_id = resolved.away.pitcher_id
    # SIM-428/425b: each team's per-position defense map ('P','C','1B'..'RF' ->
    # player_id). The catcher feeds the framing gate; the full map feeds the
    # SIM-491 fielder kernel (SIM_FIELDER_KERNEL_SIGMA). Computed once per side.
    home_def = build_team_defense_map(resolved.home)
    away_def = build_team_defense_map(resolved.away)
    state.home_catcher_id = home_def.get("C")
    state.away_catcher_id = away_def.get("C")
    state.home_defense = home_def
    state.away_defense = away_def
    return state


# ---------------------------------------------------------------------------
# Per-position fielder map — ResolvedLineup -> {position name: player_id} (SIM-363)
# ---------------------------------------------------------------------------


def build_team_defense_map(team: TeamLineup) -> dict[str, int]:
    """Map ONE team's resolved lineup to ``{defensive-slot name: player_id}``.

    Produces the ``defense_positions`` map ``FieldSnapshot.from_game_state`` takes
    (snapshots.DEFENSE_POSITIONS -> player id) for the fielding side, keyed by the
    canonical slot name ('P','C','1B'..'RF') derived from each occupant's MLB
    position code (``POSITION_CODE_TO_NAME``).

    Rules
    -----
      * Each batting-order slot's *current occupant* (the resolver already applied
        the highest-sequence-wins substitution rule) contributes one fielding
        slot, by its ``position_code``.
      * The pitcher is added from ``team.pitcher_id`` (an AL pitcher has no batting
        slot, so the pitcher would otherwise be missing); an explicit pitcher
        ALWAYS wins the 'P' slot over any batting-slot row that also maps to 'P'.
      * Non-fielding occupants are skipped: a DH (code '10'/'DH'), or a pinch
        role ('PH'/'PR'/'DEF') that is not yet in the field (a non-numeric /
        unmapped ``position_code``).  Their batting slot stays in the order but
        they hold no glove, so they get no FieldSnapshot slot.
      * If two batting slots map to the same fielding position (a defensive sub
        that the source recorded without retiring the prior occupant), the one
        with the higher ``sequence`` wins — consistent with the resolver's
        "current occupant = highest applicable sequence" substitution logic.

    The result has at most 9 entries (one per DEFENSE_POSITIONS name); a partial
    lineup yields a partial map (the FieldSnapshot leaves the rest unassigned).
    """
    # position name -> (winning sequence so far, player_id), so a same-position
    # collision resolves to the higher-sequence (current) occupant.
    best: dict[str, tuple[int, int]] = {}
    for slot in team.slots:
        code = str(slot.position_code)
        # raw.game_lineups stores position_code as the canonical NAME ('SS','C',
        # '1B'..) — accept it directly. Also map a numeric scorebook code ('1'..'9')
        # for any legacy/alternate source. 'DH' / pinch role / anything else has no
        # fielding slot -> skipped. (Before this, the name-format real data fell
        # through POSITION_CODE_TO_NAME — which is keyed by NUMBER — so only the
        # explicitly-added pitcher survived: the catcher + the 7 other fielders were
        # silently dropped, leaving SIM-428 framing + SIM-425b fielder-RBF inert.)
        name = code if code in DEFENSE_POSITION_NAMES else POSITION_CODE_TO_NAME.get(code)
        if name is None:
            continue
        cur = best.get(name)
        if cur is None or slot.sequence > cur[0]:
            best[name] = (slot.sequence, slot.player_id)

    out: dict[str, int] = {name: pid for name, (_seq, pid) in best.items()}

    # The current pitcher always owns 'P' (a mid-game pitching change is resolved
    # on TeamLineup.pitcher_id independently of the batting order, so prefer it).
    if team.pitcher_id is not None:
        out["P"] = int(team.pitcher_id)

    return out


def build_defense_map(resolved: ResolvedLineup, *, fielding_side: str) -> dict[str, int]:
    """Map the FIELDING team's lineup to ``{defensive-slot name: player_id}``.

    ``fielding_side`` selects which team is in the field: ``"home"`` or ``"away"``
    (case-insensitive; ``Team.HOME`` / ``Team.AWAY`` are also accepted).  Returns
    the ``defense_positions`` map ``FieldSnapshot.from_game_state`` consumes.

    See :func:`build_team_defense_map` for the position-code -> slot-name mapping
    and the substitution / DH / pinch-role rules.

    Raises
    ------
    LineupResolutionError
        If ``fielding_side`` is not a recognizable home/away selector.
    """
    side = _normalize_side(fielding_side)
    team = resolved.home if side == Team.HOME else resolved.away
    return build_team_defense_map(team)


def fielding_side_for_half(half: Half) -> Team:
    """Which team is in the FIELD for ``half`` (the defense).

    TOP   -> the AWAY team is batting, so the **HOME** team fields.
    BOTTOM-> the HOME team is batting, so the **AWAY** team fields.

    (Mirrors ``GameState.defense``: HOME defends in the top, AWAY in the bottom.)
    """
    return Team.HOME if half == Half.TOP else Team.AWAY


def build_defense_map_for_state(resolved: ResolvedLineup, state: GameState) -> dict[str, int]:
    """Convenience: pick the fielding side from ``state.half`` and build its map.

    Wires :func:`fielding_side_for_half` (the home/away <-> fielding convention)
    into :func:`build_defense_map`, so callers holding a live :class:`GameState`
    (e.g. the /state endpoint building a ``FieldSnapshot``) get the correct
    defensive map for the current half without restating the convention.
    """
    side = fielding_side_for_half(state.half)
    team = resolved.home if side == Team.HOME else resolved.away
    return build_team_defense_map(team)


def _normalize_side(side: Any) -> Team:
    """Coerce a side selector (``Team``, ``"home"``/``"away"``, 0/1) to a ``Team``."""
    if isinstance(side, Team):
        return side
    if isinstance(side, str):
        s = side.strip().lower()
        if s in ("home", "h"):
            return Team.HOME
        if s in ("away", "a"):
            return Team.AWAY
    if isinstance(side, int) and not isinstance(side, bool):
        try:
            return Team(side)
        except ValueError:
            pass
    raise LineupResolutionError(
        f"fielding_side={side!r} is not a recognizable side; expected 'home'/'away' or a Team."
    )


# ---------------------------------------------------------------------------
# DB access (asyncpg) — separated from the pure layers above
# ---------------------------------------------------------------------------

# A "connection" here is anything exposing async ``fetch`` / ``fetchrow``
# (asyncpg's Pool, Connection, and the project's mock_db_pool all satisfy this),
# so these helpers work against a pool or a single connection.

_GAME_SIDES_SQL = """
    SELECT game_pk, season, home_team_id, away_team_id
    FROM   raw.games
    WHERE  game_pk = $1
"""

_LINEUP_ROWS_SQL = """
    SELECT team_id, player_id, batting_order, position_code,
           is_starter, sequence, entered_inning, entered_at_bat, pinch_role
    FROM   raw.game_lineups
    WHERE  game_pk = $1
    ORDER  BY team_id, batting_order, sequence
"""

_PLAYER_HANDS_SQL = """
    SELECT player_id, bats, throws
    FROM   raw.players
    WHERE  player_id = ANY($1::int[])
"""


async def fetch_game_sides(conn: Any, game_pk: int) -> Mapping[str, Any] | None:
    """Read the ``raw.games`` row that maps team ids to the home/away sides.

    Returns the row (``game_pk, season, home_team_id, away_team_id``) or None
    when the game is unknown.
    """
    return await conn.fetchrow(_GAME_SIDES_SQL, int(game_pk))


async def fetch_lineup_rows(conn: Any, game_pk: int) -> Sequence[Mapping[str, Any]]:
    """Read every ``raw.game_lineups`` row for ``game_pk`` (all sequences).

    All sequences are returned; the substitution reduction happens in the pure
    :func:`resolve_lineup_from_rows` so it is unit-testable without a DB.
    """
    return await conn.fetch(_LINEUP_ROWS_SQL, int(game_pk))


async def fetch_player_hands(
    conn: Any, player_ids: Iterable[int]
) -> tuple[dict[int, str], dict[int, str]]:
    """Read ``bats`` / ``throws`` for the given players in one round trip.

    Returns ``(bat_hands, throw_hands)`` dicts keyed by player_id.  Missing ids
    are simply absent (the builder falls back to a default hand).
    """
    ids = sorted({int(p) for p in player_ids})
    if not ids:
        return {}, {}
    rows = await conn.fetch(_PLAYER_HANDS_SQL, ids)
    bats: dict[int, str] = {}
    throws: dict[int, str] = {}
    for r in rows:
        pid = int(_row_get(r, "player_id"))
        b = _row_get(r, "bats")
        t = _row_get(r, "throws")
        if b is not None:
            bats[pid] = str(b)
        if t is not None:
            throws[pid] = str(t)
    return bats, throws


# ---------------------------------------------------------------------------
# Async orchestrator — DB access -> pure assembly -> GameState
# ---------------------------------------------------------------------------


async def resolve_lineup(
    conn: Any,
    game_pk: int,
    *,
    as_of_at_bat: int | None = None,
) -> ResolvedLineup:
    """Read Postgres and return the resolved (substitution-applied) lineup.

    Thin orchestration only: it reads ``raw.games`` (to map sides + season),
    ``raw.game_lineups`` (every sequence), and ``raw.players`` (hands), then
    delegates ALL logic to the pure :func:`resolve_lineup_from_rows`.

    Raises
    ------
    LineupResolutionError
        When the game is unknown, or no usable lineup resolves.
    """
    game = await fetch_game_sides(conn, game_pk)
    if game is None:
        raise LineupResolutionError(
            f"game_pk={game_pk} not found in raw.games; cannot resolve a lineup."
        )

    season = int(_row_get(game, "season"))
    home_team_id = int(_row_get(game, "home_team_id"))
    away_team_id = int(_row_get(game, "away_team_id"))

    lineup_rows = await fetch_lineup_rows(conn, game_pk)
    if not lineup_rows:
        raise LineupNotIngestedError(
            f"no raw.game_lineups rows for game_pk={game_pk}; "
            "lineup not yet published — try again closer to game time."
        )

    player_ids = {
        int(_row_get(r, "player_id")) for r in lineup_rows if _row_get(r, "player_id") is not None
    }
    bat_hands, throw_hands = await fetch_player_hands(conn, player_ids)

    return resolve_lineup_from_rows(
        game_pk=int(game_pk),
        season=season,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        lineup_rows=lineup_rows,
        bat_hands=bat_hands,
        throw_hands=throw_hands,
        as_of_at_bat=as_of_at_bat,
    )


async def resolve_game_state(
    conn: Any,
    game_pk: int,
    *,
    half: Half = Half.TOP,
    as_of_at_bat: int | None = None,
    seed: int | None = None,
) -> GameState:
    """One-call convenience: Postgres ``game_pk`` -> a fresh :class:`GameState`.

    Wires :func:`resolve_lineup` (DB) into :func:`build_game_state` (pure).
    """
    resolved = await resolve_lineup(conn, game_pk, as_of_at_bat=as_of_at_bat)
    return build_game_state(resolved, half=half, seed=seed)


__all__ = [
    # value types
    "LineupSlot",
    "TeamLineup",
    "ResolvedLineup",
    "LineupResolutionError",
    "LineupNotIngestedError",
    # pure assembly + building
    "resolve_lineup_from_rows",
    "build_game_state",
    # per-position fielder map (SIM-363)
    "build_team_defense_map",
    "build_defense_map",
    "build_defense_map_for_state",
    "fielding_side_for_half",
    # DB access
    "fetch_game_sides",
    "fetch_lineup_rows",
    "fetch_player_hands",
    # orchestrators
    "resolve_lineup",
    "resolve_game_state",
    # constants
    "PITCHER_POSITION_CODES",
    "POSITION_CODE_TO_NAME",
    "DEFENSE_POSITION_NAMES",
    "LINEUP_SLOTS",
    "DEFAULT_BAT_HAND",
]
