"""
simulation/sim_kwargs.py
========================
The ONE builder that turns a resolved GameState into ``simulate_game`` kwargs,
plus the venue park-factor resolver it depends on.

ONE BUILDER
-----------
``api/routes/games.py`` imports :func:`sim_kwargs_from_state`, and so does every
harness script, so the kwargs the API sends and the kwargs a harness sends
cannot differ. A second copy drifts: a hand-written copy in
``scripts/sim_stats.py`` dropped the two defense maps and the park factor, and
the harness read two identical no-ops as "no effect" (the one builder, SIM-449).

RESOLVED OR NOT: THE PARK-FACTOR SENTINEL
-----------------------------------------
A neutral park's factor is 1.0, and so is the GameState default. The key
``park_run_factor`` is always PRESENT, so a reader cannot tell "the lookup
returned neutral" from "nobody looked". :data:`UNRESOLVED_PARK_FACTOR` removes
the ambiguity. It is a float subclass that equals 1.0, so every reader that only
wants the number keeps working, but ``isinstance`` tells it apart from a
resolved 1.0. The two states stay apart end to end:

* :func:`api.routes.games._resolve_state_or_error` stamps the sentinel on EVERY
  GameState it returns.
* :func:`resolve_park_run_factor` returns a plain float only for a real row
  inside the sane 0.5-2.0 range. Every unavailable branch (no source, unknown
  venue, no row, a degenerate value, a query error) returns the sentinel
  carrying a ``reason``, not a plain 1.0. A plain 1.0 would clear the
  sentinel, so a lookup that resolved nothing would read as "resolved" (the
  park-factor sentinel, SIM-453).
* Every unavailable branch logs at WARNING, deduplicated by reason, so an
  operator sees it once per cause instead of never or 2,400 times.
* :func:`sim_kwargs_from_state` REFUSES an unresolved state with
  :class:`UnresolvedParkFactorError`. Proceeding is possible but never
  implicit: the caller passes ``allow_unavailable_park_factor=True`` and gets
  another WARNING for it.
* :func:`build_sim_kwargs` proceeds loudly by DEFAULT, because ``/simulate`` must
  keep answering on a stack with no park-factor source. It does NOT clear the
  sentinel, so the caller can still ask the state what happened.

The sentinel lives in the field the lookup writes, so a real lookup clears it by
simple assignment. A separate "resolved" boolean would have to be cleared by
hand, and a hand can forget.

WHERE THE PARK FACTORS AND THE MANAGER PROFILES COME FROM
---------------------------------------------------------
:func:`prime_park_factor_source` opens the sim DuckDB READ-ONLY once. In that one
open it reads three things into memory: the run factors
(``derived.park_factors``), each manager's season profile
(``derived.manager_season_metrics``) and the season's manager league means
(the ``'manager'`` rows of ``derived.league_averages``). Then it CLOSES the
file. ``api/main.py`` calls the prime in the lifespan.

Every API route passes ``con=None``. :func:`resolve_park_run_factor` then reads
the run factors from the snapshot, and :func:`resolve_manager_profiles_onto_state`
reads the manager profiles from the :class:`ManagerProfileSnapshot`. The park
resolver re-reads the file when its mtime changes, so a rebuild reaches the app
without a restart, and the manager snapshot moves with the run factors. The
block above :class:`ParkFactorSource` gives the reasons for this design.

The fallback is live only after an explicit prime. Nothing primes it in the unit
lane, so a unit test that passes ``con=None`` still gets the unavailable sentinel
and empty manager profiles, and never touches a file.

WHY IT LIVES UNDER simulation/
------------------------------
``docker-compose.yml`` bind-mounts ``api/``, ``pipeline/``, ``similarity/``,
``simulation/`` and ``db/`` into the app service. It does NOT mount ``scripts/``.
A shared module under ``scripts/`` would need an image rebuild to reach the
container, so the shared module lives under a mounted package.

Owner: Backend Developer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

log = logging.getLogger(__name__)

#: The default sim DuckDB path. The API lifespan (``api/main.py``) and the
#: play-pool sampler read the same env var, so a harness opens the same file.
DEFAULT_DUCKDB_PATH = "/data/baseball_sim.duckdb"


# ---------------------------------------------------------------------------
# The unresolved-park-factor sentinel
# ---------------------------------------------------------------------------


class UnresolvedParkFactorError(RuntimeError):
    """A caller asked for sim kwargs from a state whose park factor is unresolved.

    The caller must first run :func:`resolve_park_factor_onto_state` (or the whole
    :func:`build_sim_kwargs` entry point). Raising here is deliberate: a park-blind
    simulation reports success and produces numbers that look fine, so the only way
    an operator learns about it is if the code refuses to run.
    """


class _UnresolvedParkFactor(float):
    """A park factor that NOBODY resolved. It equals 1.0 and is not 1.0.

    The value is a real float of 1.0, so any consumer that only reads the number
    reads a neutral park. ``isinstance`` separates it from a plain ``1.0`` that a
    park-factor lookup really produced, which is the distinction the sentinel
    exists for.

    The instance carries the ``reason`` it is unresolved, so the warning an
    operator reads names the cause ("no park-factor source", "no row for venue
    3313 season 2024") instead of saying "neutral" and stopping there.
    """

    __slots__ = ("reason",)

    #: A bare annotation, not an assignment — assigning here would collide with
    #: ``__slots__``. It tells mypy the slot exists.
    reason: str

    def __new__(cls, reason: str = "nobody looked the venue up") -> _UnresolvedParkFactor:
        obj = super().__new__(cls, 1.0)
        obj.reason = str(reason)
        return obj

    def __reduce__(self):  # pragma: no cover - pickling across the ProcessPool
        # A float subclass does not pickle its slots by default, and a GameState
        # crosses to a forkserver worker by pickle. Without this the sentinel comes
        # back a plain 1.0 on the far side, and the state reads as resolved there.
        return (_UnresolvedParkFactor, (self.reason,))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"UNRESOLVED_PARK_FACTOR({self.reason!r})"


#: The park factor a GameState carries before anybody looks the venue up.
#: :func:`api.routes.games._resolve_state_or_error` stamps it on every state it
#: returns; :func:`sim_kwargs_from_state` refuses to build kwargs while it is there.
UNRESOLVED_PARK_FACTOR: float = _UnresolvedParkFactor("nobody looked the venue up")


def park_factor_unavailable(reason: str) -> float:
    """Build an UNRESOLVED park factor that records why the source could not answer.

    ``resolve_park_run_factor`` returns this instead of a plain ``1.0`` on every
    branch where the lookup fails. "I could not look it up" and "I looked it
    up and this park is neutral" are different answers, and only the second one is
    a plain ``1.0``.
    """
    return _UnresolvedParkFactor(reason)


def park_factor_reason(value: Any) -> str | None:
    """Why ``value`` is unresolved, or ``None`` when it is a resolved factor."""
    if isinstance(value, _UnresolvedParkFactor):
        return value.reason
    return None


def mark_park_factor_unresolved(state: Any) -> None:
    """Stamp the unresolved sentinel on ``state``.

    The state factory calls this. From this point the state cannot produce sim
    kwargs until somebody resolves the venue factor onto it.
    """
    state.park_run_factor = UNRESOLVED_PARK_FACTOR


def _warn_once(reason: str) -> None:
    """Log ``reason`` at WARNING the first time this process sees it.

    A season backtest asks 2,400 times. One line per CAUSE is loud. 2,400 lines per
    cause is noise an operator learns to scroll past, which is how a silent neutral
    survives in a log that technically mentions it.
    """
    if reason in _WARNED_REASONS:
        return
    _WARNED_REASONS.add(reason)
    log.warning("SIM-453: park factor UNRESOLVED — %s. The run proceeds park-blind.", reason)


def reset_park_factor_warnings() -> None:
    """Clear the warn-once memo. Tests call this; production does not."""
    _WARNED_REASONS.clear()


#: Reasons already logged in this process. See :func:`_warn_once`.
_WARNED_REASONS: set[str] = set()


def park_factor_is_resolved(state: Any) -> bool:
    """Report whether somebody resolved ``state``'s park factor.

    A state that never carried the sentinel counts as resolved. The harnesses that
    build a GameState by hand (``scripts/sim_stats.py``, the acceptance lane) set the
    factor themselves, and a bare unit-test GameState keeps the dataclass default.
    """
    return not isinstance(getattr(state, "park_run_factor", None), _UnresolvedParkFactor)


#: Every key :func:`sim_kwargs_from_state` returns. A unit test asserts the built
#: dict against this set, so a dropped key fails CI instead of turning a
#: production feature into a silent no-op.
SIM_KWARG_KEYS: frozenset[str] = frozenset(
    {
        "away_lineup",
        "home_lineup",
        "season",
        "pitcher_id",
        "bat_hand",
        "bat_hands",
        "throw_hands",
        "home_pitcher_id",
        "away_pitcher_id",
        "home_catcher_id",
        "away_catcher_id",
        "home_defense",
        "away_defense",
        "park_run_factor",
        # The live park for the fence stage (None = unknown).
        "venue_id",
        "max_innings",
        # Each side's manager, the real pen and its usage, the pen's source,
        # each side's manager tendency profile and the league means.
        "home_manager_id",
        "away_manager_id",
        "bullpen",
        "pitcher_rest_days",
        "pitcher_recent_usage",
        "bullpen_source",
        "manager_profiles",
        "manager_league_profile",
    }
)


def sim_kwargs_from_state(
    state: Any, *, allow_unavailable_park_factor: bool = False
) -> dict[str, Any]:
    """Build the ``simulate_game`` kwargs from a resolved GameState.

    Pulls the lineup contract off the GameState's public fields (the same fields
    ``lineup_resolver.build_game_state`` populated) into the ``sim_kwargs`` dict
    the BatchRunner / ``simulate_game`` consume.

    This is the ONLY definition. ``api/routes/games.py`` and every harness script
    import it, so the kwargs the API sends and the kwargs the validation harness
    sends cannot differ.

    Raises :class:`UnresolvedParkFactorError` when ``state`` still carries
    :data:`UNRESOLVED_PARK_FACTOR`. Prefer :func:`build_sim_kwargs`, which
    resolves first and then calls this.

    ``allow_unavailable_park_factor=True`` is the EXPLICIT park-blind path. It
    builds anyway, logs at WARNING, and sends a plain neutral 1.0. It does not
    touch ``state``, so the state still reports the truth afterwards. Nothing
    takes this path by accident: the caller has to name it.

    When ``state.asof_ymd`` is set (a caller resolved and wrote it — see
    :func:`resolve_asof_ymd`), the returned dict carries it forward as
    ``"_asof_ymd"``, the SAME factory-only key :func:`build_sim_kwargs` adds.
    When it is ``None`` (a live game, the default), nothing is added.
    """
    if not park_factor_is_resolved(state):
        reason = park_factor_reason(getattr(state, "park_run_factor", None)) or "unknown cause"
        if not allow_unavailable_park_factor:
            raise UnresolvedParkFactorError(
                "SIM-452: this GameState's park_run_factor is UNRESOLVED — "
                f"{reason}. Call build_sim_kwargs(state, pool=..., con=..., "
                "game_pk=...) — or resolve_park_factor_onto_state(...) — before "
                "building sim kwargs. Building now would send a neutral 1.0 park "
                "factor and make the park kernel a silent no-op for this run. To "
                "proceed anyway, pass allow_unavailable_park_factor=True."
            )
        log.warning(
            "SIM-453: building sim kwargs with an UNAVAILABLE park factor (%s). "
            "This run is park-blind and the park kernel is a no-op for it.",
            reason,
        )
    kwargs: dict[str, Any] = {
        "away_lineup": list(getattr(state, "away_lineup", []) or []),
        "home_lineup": list(getattr(state, "home_lineup", []) or []),
        "season": int(getattr(state, "season", 2024)),
        "pitcher_id": int(getattr(state, "pitcher_id", 0) or 0),
        "bat_hand": str(getattr(state, "bat_hand", "R") or "R"),
        # The per-batter hand map and both starters, so the matchup follows the
        # lineup across PAs and half-innings (plain dicts and ints, so they
        # pickle across the ProcessPool to the worker).
        "bat_hands": dict(getattr(state, "bat_hands", {}) or {}),
        "throw_hands": dict(getattr(state, "throw_hands", {}) or {}),
        "home_pitcher_id": getattr(state, "home_pitcher_id", None),
        "away_pitcher_id": getattr(state, "away_pitcher_id", None),
        # The catchers, for the steal and pickoff draws and the receiving factor.
        "home_catcher_id": getattr(state, "home_catcher_id", None),
        "away_catcher_id": getattr(state, "away_catcher_id", None),
        # The per-position defense maps, for the fielder score matrices, and the
        # venue run park factor, for the park kernel (SIM_PARK_KERNEL_SIGMA). The
        # caller resolves the factor onto the state; it is 1.0 (neutral) when no
        # source answers. Each is a no-op with its weight off, so passing them
        # is always safe.
        "home_defense": dict(getattr(state, "home_defense", {}) or {}),
        "away_defense": dict(getattr(state, "away_defense", {}) or {}),
        "park_run_factor": float(getattr(state, "park_run_factor", 1.0) or 1.0),
        # The venue id ``state.park`` carries (a digit string), for the fence stage.
        "venue_id": venue_id_of_state(state),
        "max_innings": 12,
        # The managers and the real pen (picklable: int keys, tuples).
        "home_manager_id": getattr(state, "home_manager_id", None),
        "away_manager_id": getattr(state, "away_manager_id", None),
        "bullpen": {
            int(k): list(v)
            for k, v in (
                getattr(getattr(state, "manager", None), "bullpen_available", {}) or {}
            ).items()
        },
        "pitcher_rest_days": dict(getattr(state, "pitcher_rest_days", {}) or {}),
        "pitcher_recent_usage": {
            int(k): tuple(int(x) for x in v)
            for k, v in (getattr(state, "pitcher_recent_usage", {}) or {}).items()
        },
        "bullpen_source": getattr(state, "bullpen_source", None),
        "manager_profiles": {
            0: dict(getattr(state, "away_manager_profile", {}) or {}),
            1: dict(getattr(state, "home_manager_profile", {}) or {}),
        },
        "manager_league_profile": dict(getattr(state, "manager_league_profile", {}) or {}),
    }
    # A factory-only key, added only when a caller resolved and wrote a cutoff
    # onto the state (see the docstring above).
    asof_ymd = getattr(state, "asof_ymd", None)
    if asof_ymd is not None:
        kwargs["_asof_ymd"] = asof_ymd
    return kwargs


def venue_id_of_state(state: Any) -> int | None:
    """The venue id a GameState's ``park`` field carries, or None."""
    park = getattr(state, "park", None)
    if park is None:
        return None
    text = str(park).strip()
    return int(text) if text.isdigit() else None


async def resolve_venue_id(pool: Any, game_pk: int) -> int | None:
    """The game's venue id from ``raw.games`` (Postgres), or None when the
    pool is absent, the game is unknown or the query fails."""
    if pool is None:
        return None
    try:
        acquire = getattr(pool, "acquire", None)
        if acquire is not None:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT venue_id FROM raw.games WHERE game_pk = $1", int(game_pk)
                )
        else:
            row = await pool.fetchrow(
                "SELECT venue_id FROM raw.games WHERE game_pk = $1", int(game_pk)
            )
        venue = None if row is None else row["venue_id"]
    except Exception as exc:  # noqa: BLE001 — a venue is optional context
        log.warning("SIM-523: venue lookup failed for game_pk=%s: %s", int(game_pk), exc)
        return None
    if venue is None:
        return None
    try:
        return int(venue)
    except (TypeError, ValueError):
        return None


def _park_factor_row(con: Any, venue_id: int, season: int) -> tuple[Any, ...] | None:
    """One ``(regressed_factor,)`` row for the venue and season, or ``None``.

    ``con`` is a DuckDB connection or the in-memory snapshot, a dict keyed by
    ``(venue_id, season)``. Both answer the same question, so the
    resolver's reasons are the same on both paths.
    """
    if isinstance(con, dict):
        if (venue_id, season) not in con:
            return None
        return (con[(venue_id, season)],)
    row = con.execute(
        "SELECT regressed_factor FROM derived.park_factors "
        "WHERE venue_id = ? AND season = ? AND factor_type = 'R'",
        [venue_id, season],
    ).fetchone()
    return None if row is None else tuple(row)


async def resolve_park_run_factor(pool: Any, con: Any, game_pk: int, season: int) -> float:
    """Resolve the venue run park factor for a game (~1.0 = neutral).

    The venue lives in Postgres (``raw.games.venue_id``) and the factor in DuckDB
    (``derived.park_factors``), so this is a two-source lookup: fetch the game's
    venue from ``pool``, then its regressed run factor (``factor_type='R'``) for the
    season from ``con`` (a DuckDB connection).

    ``con=None`` falls back to the park-factor snapshot when ``api/main.py`` primed
    one. That is how the API reads the park factors: the app sets no
    ``app.state.sim_duckdb``, so every route passes ``con=None``. The snapshot is
    a dict in memory; no file stays open. On that path this call also runs the
    mtime check, which re-reads the run factors and the manager profiles together
    after a rebuild.

    RETURN CONTRACT. A real row inside the sane 0.5-2.0 range comes back a PLAIN
    ``float``. Every other outcome -- no source, unknown venue, no row, a
    degenerate value, a query error -- comes back :data:`UNRESOLVED_PARK_FACTOR`
    carrying its reason, and logs at WARNING. The sentinel still ``== 1.0``, so a
    caller that only reads the number is unaffected; a caller that asks
    :func:`park_factor_is_resolved` gets the truth.

    ``pool`` may be a connection pool (it has ``acquire``) or a single connection
    (it does not). Both the API pool and a script's asyncpg connection work."""
    if con is None:
        await _refresh_park_factor_snapshot_if_changed()
        con = _PARK_FACTORS
    if con is None:
        reason = (
            "no park-factor source — app.state.sim_duckdb is None and no read-only "
            "source was primed (see prime_park_factor_source)"
        )
        _warn_once(reason)
        return park_factor_unavailable(reason)
    try:
        acquire = getattr(pool, "acquire", None)
        if acquire is not None:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT venue_id FROM raw.games WHERE game_pk = $1", int(game_pk)
                )
        else:
            row = await pool.fetchrow(
                "SELECT venue_id FROM raw.games WHERE game_pk = $1", int(game_pk)
            )
        venue_id = None if row is None else row["venue_id"]
        if venue_id is None:
            reason = f"raw.games has no venue_id for game_pk={int(game_pk)}"
            _warn_once(reason)
            return park_factor_unavailable(reason)
        res = _park_factor_row(con, int(venue_id), int(season))
        if not res or res[0] is None:
            reason = (
                "derived.park_factors has no factor_type='R' row for "
                f"venue_id={int(venue_id)} season={int(season)}"
            )
            _warn_once(reason)
            return park_factor_unavailable(reason)
        f = float(res[0])
        if 0.5 <= f <= 2.0:
            return f
        # A degenerate/garbage factor is not a neutral park. It is a broken source.
        reason = (
            f"derived.park_factors gave {f!r} for venue_id={int(venue_id)} "
            f"season={int(season)}, outside the sane 0.5-2.0 park range"
        )
        _warn_once(reason)
        return park_factor_unavailable(reason)
    except Exception as exc:  # noqa: BLE001 -- any failure is an unavailable source
        reason = f"park-factor lookup raised {type(exc).__name__}: {exc}"
        _warn_once(reason)
        return park_factor_unavailable(reason)


async def resolve_asof_ymd(pool: Any, game_pk: int) -> int | None:
    """The last date whose plays a simulator of ``game_pk`` may copy.

    That is the day BEFORE the game, not the game's own date, and the reason is
    the game itself. The play pools hold every play of every game in the window,
    including this one. A cutoff on the game's own date would leave the
    simulator free to copy the very plays it is trying to predict — the most
    direct leak available, and one that would make a backtest look superb.

    Cutting at the previous day also drops the other games played earlier that
    day, which is a real if small loss. Taking those back needs first-pitch
    times, which we do not carry; a day is the finest cut the data supports.

    Returns None when the pool is absent, the game is unknown or the query
    fails — the caller then simulates without a cutoff, which is correct for a
    live game.
    """
    if pool is None:
        return None
    try:
        acquire = getattr(pool, "acquire", None)
        if acquire is not None:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT game_date FROM raw.games WHERE game_pk = $1", int(game_pk)
                )
        else:
            row = await pool.fetchrow(
                "SELECT game_date FROM raw.games WHERE game_pk = $1", int(game_pk)
            )
        game_date = None if row is None else row["game_date"]
    except Exception as exc:  # noqa: BLE001 — a missing cutoff is not fatal
        log.warning("SIM-535: game-date lookup failed for game_pk=%s: %s", int(game_pk), exc)
        return None
    if game_date is None:
        return None
    try:
        cutoff = game_date - timedelta(days=1)
        return int(cutoff.strftime("%Y%m%d"))
    except (AttributeError, TypeError, ValueError):
        log.warning("SIM-535: unusable game_date %r for game_pk=%s", game_date, int(game_pk))
        return None


#: The manager tendency columns the profile dicts carry (the engine's vocabulary).
MANAGER_TENDENCY_COLUMNS: tuple[str, ...] = (
    "starter_avg_pitch_count",
    "starter_pull_pct_before_100",
    "closer_entry_leverage_index",
    "high_leverage_reliever_rate",
    "opener_usage_rate",
    "bulk_innings_rate",
    "available_reliever_usage_rate",
    "steal_order_rate_per_1b_opp",
    "hit_and_run_rate_per_opportunity",
    "sac_bunt_rate_high_leverage",
    "sac_bunt_rate_low_leverage",
    "squeeze_play_rate_per_3b_opp",
    "pinch_hit_rate_vs_same_hand",
    "pinch_hit_rate_high_leverage",
    "defensive_sub_rate_late_innings",
    "double_switch_rate_per_reliever_change",
    "platoon_advantage_exploitation_rate",
)


@dataclass(frozen=True)
class ManagerProfileSnapshot:
    """The manager profiles that :func:`prime_park_factor_source` holds in memory.

    ``profiles`` maps ``(manager_id, season)`` to the manager's tendency values.
    It holds only the rows at or above the minimum sample
    (``NOT below_minimum_sample``), and each row drops its None columns.
    ``league`` maps a season to the manager league means, with None values
    dropped. Both dicts have the shape the SQL path of
    :func:`resolve_manager_profiles_onto_state` builds, so the two paths give
    the same profiles. An empty snapshot means the prime ran and found no
    manager rows.
    """

    profiles: dict[tuple[int, int], dict[str, float]]
    league: dict[int, dict[str, float]]


def _parse_league_profile(raw: Any) -> dict[str, float]:
    """One ``profile_json`` value as ``{column: mean}``, with None values dropped."""
    import json

    doc = json.loads(raw) if isinstance(raw, str) else dict(raw)
    return {str(k): float(v) for k, v in doc.items() if v is not None}


def _read_manager_profile_rows(con: Any, path: str = "the sim DuckDB") -> ManagerProfileSnapshot:
    """Read every kept manager-season and every manager league row from ``con``.

    The park-factor prime calls this inside its one open of the file, so the
    manager read adds no handle. Never raises. A missing table, a query error
    or a row of the wrong width leaves that table's dict empty, and the reader
    logs ONE warning for the whole read, once per cause in the process. Every
    steal draw's manager weight is then 1.0, the neutral value.
    """
    cols = ", ".join(MANAGER_TENDENCY_COLUMNS)
    width = 2 + len(MANAGER_TENDENCY_COLUMNS)
    problems: list[str] = []

    profiles: dict[tuple[int, int], dict[str, float]] = {}
    try:
        rows = con.execute(
            f"SELECT manager_id, season, {cols} FROM derived.manager_season_metrics "
            "WHERE NOT below_minimum_sample"
        ).fetchall()
        for row in rows:
            if len(row) != width:
                raise ValueError(f"a manager row has {len(row)} columns, not {width}")
            manager_id, season, *values = row
            profiles[(int(manager_id), int(season))] = {
                c: float(v)
                for c, v in zip(MANAGER_TENDENCY_COLUMNS, values, strict=True)
                if v is not None
            }
    except Exception as exc:  # noqa: BLE001 — an unreadable table reads as no profiles
        profiles = {}
        problems.append(f"derived.manager_season_metrics: {_first_line(exc)}")

    league: dict[int, dict[str, float]] = {}
    try:
        rows = con.execute(
            "SELECT season, profile_json FROM derived.league_averages WHERE entity_type = 'manager'"
        ).fetchall()
        for row in rows:
            if len(row) != 2:
                raise ValueError(f"a league row has {len(row)} columns, not 2")
            season, raw = row
            if raw is not None:
                league[int(season)] = _parse_league_profile(raw)
    except Exception as exc:  # noqa: BLE001 — an unreadable table reads as no league means
        league = {}
        problems.append(f"derived.league_averages: {_first_line(exc)}")

    if problems:
        _warn_manager_profiles_once(
            f"SIM-575: manager profiles unreadable in {path} ({'; '.join(problems)}); "
            "every steal draw's manager weight is 1.0"
        )
    return ManagerProfileSnapshot(profiles=profiles, league=league)


def _first_line(exc: Exception) -> str:
    """Name ``exc`` on one line: its type and the first line of its text."""
    text = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {text[0] if text else ''}"


def _warn_manager_profiles_once(message: str) -> None:
    """Log ``message`` at WARNING the first time this process sees it.

    A park read that keeps failing re-reads the file on every check, and every
    re-read reads the manager tables again. The memo keeps one line per cause.
    The memo is the park-factor memo, so :func:`reset_park_factor_warnings`
    clears both.
    """
    if message in _WARNED_REASONS:
        return
    _WARNED_REASONS.add(message)
    log.warning(message)


def resolve_manager_profiles_onto_state(state: Any, con: Any) -> None:
    """Write each side's manager tendency profile and the season's league means
    onto ``state`` (``home_manager_profile``, ``away_manager_profile``,
    ``manager_league_profile``).

    ``con`` picks the source:

    * ``None``: the snapshot that :func:`prime_park_factor_source` holds. Every
      API route takes this path. With nothing primed (the unit lane), the call
      leaves the state untouched and reads no file.
    * a :class:`ManagerProfileSnapshot`: dict lookups in that snapshot.
    * anything else: a DuckDB connection, read by SQL. The scripts and the
      lanes pass their own read-only connection.

    The chain per side: (manager, season) -> (manager, season - 1) -> empty
    (the consumer then reads the league mean, never a hand-set value). A missing
    table or an unknown manager leaves the dicts empty, and every consumer is
    neutral on an empty dict. Read-only; never raises.

    This call runs no mtime check of its own. :func:`build_sim_kwargs` resolves
    the park factor first, and that lookup re-reads the file after a rebuild,
    so the manager read here sees the fresh snapshot.
    """
    if con is None:
        con = _MANAGER_PROFILES
        if con is None:
            return
    season = int(getattr(state, "season", 0) or 0)

    if isinstance(con, ManagerProfileSnapshot):
        snap = con

        def _snap_profile(manager_id: Any) -> dict[str, float]:
            if manager_id is None:
                return {}
            try:
                mid = int(manager_id)
            except (TypeError, ValueError):  # an id that is not a number reads as no profile
                return {}
            for s in (season, season - 1):
                found = snap.profiles.get((mid, s))
                if found is not None:
                    return dict(found)
            return {}

        try:
            state.home_manager_profile = _snap_profile(getattr(state, "home_manager_id", None))
            state.away_manager_profile = _snap_profile(getattr(state, "away_manager_id", None))
            state.manager_league_profile = dict(snap.league.get(season, {}))
        except AttributeError:  # a state stand-in that takes no attributes
            return
        return

    cols = ", ".join(MANAGER_TENDENCY_COLUMNS)

    def _profile(manager_id: Any) -> dict[str, float]:
        if manager_id is None:
            return {}
        for s in (season, season - 1):
            try:
                row = con.execute(
                    f"SELECT {cols} FROM derived.manager_season_metrics "
                    "WHERE manager_id = ? AND season = ? AND NOT below_minimum_sample",
                    [int(manager_id), int(s)],
                ).fetchone()
            except Exception:  # noqa: BLE001 — a missing table reads as no profile
                return {}
            if row is not None and len(row) == len(MANAGER_TENDENCY_COLUMNS):
                return {
                    c: float(v)
                    for c, v in zip(MANAGER_TENDENCY_COLUMNS, row, strict=True)
                    if v is not None
                }
        return {}

    league: dict[str, float] = {}
    try:
        league_row = con.execute(
            "SELECT profile_json FROM derived.league_averages "
            "WHERE entity_type = 'manager' AND season = ?",
            [season],
        ).fetchone()
        if league_row is not None and league_row[0] is not None:
            league = _parse_league_profile(league_row[0])
    except Exception:  # noqa: BLE001 — a missing table, an unexpected shape: no league row
        league = {}
    try:
        state.home_manager_profile = _profile(getattr(state, "home_manager_id", None))
        state.away_manager_profile = _profile(getattr(state, "away_manager_id", None))
        state.manager_league_profile = league
    except AttributeError:  # a state stand-in that takes no attributes: nothing to carry
        return


async def resolve_park_factor_onto_state(
    state: Any,
    pool: Any,
    con: Any,
    game_pk: int,
    season: int | None = None,
) -> float:
    """Resolve the venue park factor and WRITE it onto ``state``.

    A real lookup writes a plain ``float`` and so clears
    :data:`UNRESOLVED_PARK_FACTOR`. An unavailable source writes the sentinel back,
    so the state stays UNRESOLVED. A resolved neutral park is therefore a plain
    ``1.0``, an unavailable one is the sentinel, and the two stay apart all the
    way to the builder.

    The write deliberately does NOT call ``float()``. ``float(sentinel)`` returns a
    plain ``1.0``, so that one call would erase the sentinel one line after the
    resolver produced it.

    When the state carries no venue id, this also writes the game's venue onto
    ``state.park`` for the fence stage. ``season`` defaults to the state's own
    season. Returns the factor it wrote.
    """
    if season is None:
        season = int(getattr(state, "season", 2024) or 2024)
    factor = await resolve_park_run_factor(pool, con, int(game_pk), int(season))
    state.park_run_factor = factor
    # The venue itself, for the fence stage (optional).
    if venue_id_of_state(state) is None:
        venue = await resolve_venue_id(pool, int(game_pk))
        if venue is not None:
            state.park = str(venue)
    return factor


async def build_sim_kwargs(
    state: Any,
    *,
    pool: Any,
    con: Any,
    game_pk: int,
    season: int | None = None,
    on_unavailable_park_factor: str = "proceed",
) -> dict[str, Any]:
    """Resolve the park factor onto ``state``, then build the ``simulate_game`` kwargs.

    THE one entry point for the API: every route reaches it through
    ``api.routes.games._resolved_sim_kwargs`` (the game routes and the betting
    card). It also resolves both managers' tendency profiles, the season's
    manager league means and the backtest cutoff. With ``con=None`` the park
    factor and the manager profiles both come from the snapshot that
    :func:`prime_park_factor_source` holds; the park lookup runs first, so its
    mtime check refreshes the manager snapshot too.
    The two offline jobs (``scripts/accuracy_comparison.py``,
    ``scripts/validate_props.py``) run the resolve step on their event loop and
    the bare builder in a worker thread instead.

    ``on_unavailable_park_factor`` picks what happens when the source cannot
    answer:

    * ``"proceed"`` (the default) builds park-blind kwargs and says so at WARNING.
      ``/simulate`` must keep answering on a stack with no sim DuckDB, so refusing
      here would turn a data gap into an HTTP 500. The state KEEPS the sentinel, so
      the caller can still read the truth off it and report it.
    * ``"raise"`` refuses, for a caller whose whole output is invalid park-blind.

    Either way the failure is LOUD. A park-blind run may continue; it may never
    continue while the instrument says it is fine.

    WHY THIS FUNCTION IS ASYNC AND THE BUILDER IS NOT
    -------------------------------------------------
    The park-factor lookup reads Postgres, so it must be async. The builder must
    stay sync, because two callers build kwargs inside a worker thread or a
    forkserver process that has no event loop and no database, and the unit tests
    build kwargs with no database at all.

    So the two halves cannot merge. Instead the state carries the sentinel across
    the boundary: the async half runs where the connection pool lives, and the sync
    half refuses to run until the async half has. The ordering is enforced by the
    data, not by a comment.
    """
    if on_unavailable_park_factor not in ("proceed", "raise"):
        raise ValueError(
            "on_unavailable_park_factor must be 'proceed' or 'raise', got "
            f"{on_unavailable_park_factor!r}"
        )
    await resolve_park_factor_onto_state(state, pool, con, int(game_pk), season)
    # The two managers' tendency profiles and the league means.
    resolve_manager_profiles_onto_state(state, con)
    if not park_factor_is_resolved(state) and on_unavailable_park_factor == "proceed":
        log.warning(
            "SIM-453: game_pk=%s simulates PARK-BLIND — %s",
            int(game_pk),
            park_factor_reason(getattr(state, "park_run_factor", None)),
        )
    kwargs = sim_kwargs_from_state(
        state,
        allow_unavailable_park_factor=(on_unavailable_park_factor == "proceed"),
    )
    # The point-in-time cutoff, as a FACTORY-ONLY key (the leading underscore:
    # it tunes the machine the factory builds and is not a ``simulate_game``
    # parameter). Absent or None, the sampler draws from the whole pool, which
    # is right for a live game.
    asof = await resolve_asof_ymd(pool, int(game_pk))
    if asof is not None:
        kwargs["_asof_ymd"] = asof
    return kwargs


def open_sim_duckdb(path: str | None = None) -> Any:
    """Open the sim DuckDB read-only, or return ``None``.

    :func:`resolve_park_run_factor` needs a DuckDB connection or the primed
    snapshot. A script has no app and no snapshot, so it opens its own
    connection; the snapshot prime opens one here too. The connection is
    READ-ONLY, so a harness never takes the write lock the nightly profile
    computor needs.

    Returns ``None`` on any failure -- no duckdb module, a missing file, or another
    process holding the write lock. The caller then TELLS the operator that every
    park factor is a neutral 1.0 instead of hiding it.
    """
    duckdb_path = path or os.environ.get("BASEBALL_DUCKDB_PATH") or DEFAULT_DUCKDB_PATH
    try:
        import duckdb

        return duckdb.connect(duckdb_path, read_only=True)
    except Exception:  # noqa: BLE001 -- any failure degrades to the neutral path
        return None


# ---------------------------------------------------------------------------
# The park-factor source, separate from replay persistence
#
# Reading ``derived.park_factors`` is a READ of reference data from the sim
# DuckDB. Replay persistence is a WRITE of a replay stream to its own file
# (``REPLAY_DUCKDB_PATH``). The two concerns are unrelated, so the park-factor
# source opens on its own, read-only, whatever REPLAY_PERSISTENCE_ENABLED says.
# When one variable gated both, the default stack ran park-blind (the
# park-factor source, SIM-453).
#
# The same open also reads the manager profiles. The API routes pass no
# connection, so before the prime carried the manager tables every API run had
# empty manager profiles, and the steal draw's manager weight was 1.0 for every
# team while the lanes, which open their own connection, read the real
# profiles (the manager profiles in the API's runs, SIM-575). A missing or
# unreadable manager table never changes the park verdict: the run factors
# stay available, and the manager snapshot is empty.
#
# The snapshots are process-wide, and they are live only after an explicit
# prime, so the unit lane never touches a file and ``con=None`` there still
# means "unavailable".
#
# THE FILE IS READ ONCE AND CLOSED. DuckDB lets a WRITER open the file only
# when no other process holds it, read-only handles included. One read-only
# handle kept open in the uvicorn server process blocked every rebuild (a
# profile recompute, a pool rebuild, the nightly job) until somebody stopped
# the app; ``/proc/<pid>/fd`` in the app container showed that one descriptor
# (the rebuild while the app serves, SIM-524). The run factors are a few
# thousand rows and the manager tables a few hundred, so the prime reads them
# into dicts and closes the file. The park resolver re-reads the file when its
# mtime changes, at most once a minute; a re-read that fails (a rebuild holds
# the write lock) keeps both old snapshots.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParkFactorSource:
    """What :func:`prime_park_factor_source` found. ``api/main.py`` logs this."""

    available: bool
    path: str
    #: Rows in ``derived.park_factors``. A source that opens but holds 0 rows is
    #: NOT available -- it answers nothing, so it is an unavailable source.
    n_rows: int
    detail: str
    #: Manager-season rows the prime kept (at or above the minimum sample).
    n_manager_profiles: int = 0
    #: Seasons with a manager row in ``derived.league_averages``.
    n_manager_league_seasons: int = 0

    def header_value(self) -> str:
        """The ``X-Park-Factor-Source`` response header value."""
        return f"duckdb:{self.n_rows}" if self.available else "unavailable"


#: The primed run factors, ``(venue_id, season) -> regressed_factor``, or ``None``.
#: Process-wide by design. A value may be ``None``: the row exists, the factor not.
_PARK_FACTORS: dict[tuple[int, int], Any] | None = None

#: The primed manager profiles, or ``None`` when nobody primed (the unit lane).
#: An EMPTY snapshot means the prime ran and found no manager rows.
_MANAGER_PROFILES: ManagerProfileSnapshot | None = None

#: What the last prime found. ``None`` means nobody primed in this process.
_PARK_FACTOR_SOURCE: ParkFactorSource | None = None

#: The file's mtime when the snapshot was read. ``None`` = unknown.
_PARK_FACTORS_MTIME: float | None = None

#: ``time.monotonic()`` of the last mtime check.
_PARK_FACTORS_CHECKED_AT: float = 0.0

#: True while a re-read keeps failing, so a long rebuild logs one line, not one a minute.
_PARK_FACTORS_REFRESH_FAILING: bool = False

#: Seconds between two mtime checks of the file.
PARK_FACTOR_REFRESH_SECONDS = 60.0


def _file_mtime(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _read_park_factor_snapshot(
    duckdb_path: str,
) -> tuple[
    ParkFactorSource,
    dict[tuple[int, int], Any] | None,
    ManagerProfileSnapshot | None,
]:
    """Read the run factors and the manager profiles into memory, then CLOSE the file.

    Returns ``(verdict, run factors, manager profiles)``. The run factors are
    ``None`` unless the park source is available. The manager profiles are
    ``None`` only when the open failed; any other manager problem gives an empty
    snapshot. The manager read never changes the park verdict. Never raises.
    The connection is closed on every path, so no handle outlives this call.
    """
    con = open_sim_duckdb(duckdb_path)
    if con is None:
        return (
            ParkFactorSource(
                available=False,
                path=duckdb_path,
                n_rows=0,
                detail="the read-only DuckDB open failed (missing file, no duckdb module, "
                "or another process holds the write lock)",
            ),
            None,
            None,
        )
    park_error: Exception | None = None
    n_rows = 0
    rows: list[Any] = []
    try:
        try:
            row = con.execute("SELECT count(*) FROM derived.park_factors").fetchone()
            n_rows = int(row[0]) if row else 0
            rows = (
                con.execute(
                    "SELECT venue_id, season, regressed_factor FROM derived.park_factors "
                    "WHERE factor_type = 'R'"
                ).fetchall()
                if n_rows > 0
                else []
            )
        except Exception as exc:  # noqa: BLE001
            park_error = exc
        managers = _read_manager_profile_rows(con, duckdb_path)
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass

    n_profiles = len(managers.profiles)
    n_league = len(managers.league)
    if park_error is not None:
        return (
            ParkFactorSource(
                available=False,
                path=duckdb_path,
                n_rows=0,
                detail="derived.park_factors is unreadable "
                f"({type(park_error).__name__}: {park_error})",
                n_manager_profiles=n_profiles,
                n_manager_league_seasons=n_league,
            ),
            None,
            managers,
        )
    if n_rows <= 0:
        return (
            ParkFactorSource(
                available=False,
                path=duckdb_path,
                n_rows=0,
                detail="derived.park_factors opened but holds 0 rows",
                n_manager_profiles=n_profiles,
                n_manager_league_seasons=n_league,
            ),
            None,
            managers,
        )
    factors = {(int(v), int(s)): f for v, s, f in rows}
    return (
        ParkFactorSource(
            available=True,
            path=duckdb_path,
            n_rows=n_rows,
            detail="read-only, read into memory and closed (SIM-524); separate from "
            "REPLAY_PERSISTENCE_ENABLED",
            n_manager_profiles=n_profiles,
            n_manager_league_seasons=n_league,
        ),
        factors,
        managers,
    )


def prime_park_factor_source(path: str | None = None) -> ParkFactorSource:
    """Read the park factors and the manager profiles for this process into memory.

    ``api/main.py`` calls this in the lifespan, whether or not replay persistence
    is on, because reading reference data is not replay persistence. After it
    runs, every caller that passes ``con=None`` reads the snapshots, and every
    ``api/routes/games.py`` route does. :func:`resolve_park_run_factor` reads the
    run factors. :func:`resolve_manager_profiles_onto_state` reads each manager's
    season profile and the season's manager league means.

    The prime also COUNTS ``derived.park_factors``. An empty table is an
    unavailable source, not a neutral one, so the count is part of the verdict.
    The manager counts ride on the verdict (``n_manager_profiles``,
    ``n_manager_league_seasons``) and never change it. The file is closed before
    this returns, so a rebuild can write it.

    Idempotent: a second call while a snapshot is held returns the same verdict.
    Never raises; a failure returns an unavailable :class:`ParkFactorSource`.
    """
    global _PARK_FACTORS, _PARK_FACTOR_SOURCE, _PARK_FACTORS_MTIME, _PARK_FACTORS_CHECKED_AT
    global _MANAGER_PROFILES

    duckdb_path = path or os.environ.get("BASEBALL_DUCKDB_PATH") or DEFAULT_DUCKDB_PATH
    if _PARK_FACTORS is not None and _PARK_FACTOR_SOURCE is not None:
        return _PARK_FACTOR_SOURCE

    mtime = _file_mtime(duckdb_path)
    source, factors, managers = _read_park_factor_snapshot(duckdb_path)
    _PARK_FACTOR_SOURCE = source
    _PARK_FACTORS = factors
    _MANAGER_PROFILES = managers
    _PARK_FACTORS_MTIME = mtime
    _PARK_FACTORS_CHECKED_AT = time.monotonic()
    return source


def refresh_park_factor_snapshot() -> bool:
    """Re-read the file when its mtime moved. True = a new snapshot.

    The re-read replaces the run factors and the manager profiles together. A
    re-read that fails -- a rebuild holds the write lock, or the rebuilt table
    is empty -- keeps both old snapshots and the old mtime, so the next check
    tries again. Only a source somebody primed is refreshed.

    A prime that FAILED (a boot during a rebuild) is tried again on every
    check, so the app recovers its park factors and its manager profiles
    without a restart.
    """
    global _PARK_FACTORS, _PARK_FACTOR_SOURCE, _PARK_FACTORS_MTIME
    global _PARK_FACTORS_CHECKED_AT, _PARK_FACTORS_REFRESH_FAILING
    global _MANAGER_PROFILES

    _PARK_FACTORS_CHECKED_AT = time.monotonic()
    old = _PARK_FACTOR_SOURCE
    if old is None:
        return False
    if _PARK_FACTORS is None:
        mtime = _file_mtime(old.path)
        source, factors, managers = _read_park_factor_snapshot(old.path)
        if factors is None:
            return False
        _PARK_FACTORS = factors
        _MANAGER_PROFILES = managers
        _PARK_FACTOR_SOURCE = source
        _PARK_FACTORS_MTIME = mtime
        log.info(
            "SIM-563: park factors read from %s after the boot-time read failed: %d rows, "
            "%d manager-seasons.",
            old.path,
            source.n_rows,
            source.n_manager_profiles,
        )
        return True
    mtime = _file_mtime(old.path)
    if mtime is None or mtime == _PARK_FACTORS_MTIME:
        return False
    source, factors, managers = _read_park_factor_snapshot(old.path)
    if factors is None:
        if not _PARK_FACTORS_REFRESH_FAILING:
            log.warning(
                "SIM-524: %s changed but the park-factor re-read failed (%s); "
                "the app keeps the snapshot it has (%d rows) and retries.",
                old.path,
                source.detail,
                old.n_rows,
            )
        _PARK_FACTORS_REFRESH_FAILING = True
        return False
    _PARK_FACTORS = factors
    _MANAGER_PROFILES = managers
    _PARK_FACTOR_SOURCE = source
    _PARK_FACTORS_MTIME = mtime
    _PARK_FACTORS_REFRESH_FAILING = False
    log.info(
        "SIM-524: park factors re-read from %s after the file changed: %d rows, "
        "%d manager-seasons.",
        old.path,
        source.n_rows,
        source.n_manager_profiles,
    )
    return True


async def _refresh_park_factor_snapshot_if_changed() -> None:
    """Check the file's mtime at most every :data:`PARK_FACTOR_REFRESH_SECONDS`.

    The re-read runs on a thread, so a lookup never blocks the event loop on the
    file. Nothing happens before a prime, so the unit lane never reads a file.
    """
    if _PARK_FACTOR_SOURCE is None:
        return
    if time.monotonic() - _PARK_FACTORS_CHECKED_AT < PARK_FACTOR_REFRESH_SECONDS:
        return
    await asyncio.to_thread(refresh_park_factor_snapshot)


def park_factor_source_status() -> ParkFactorSource | None:
    """What the last :func:`prime_park_factor_source` found, or ``None``."""
    return _PARK_FACTOR_SOURCE


def close_park_factor_source() -> None:
    """Drop the park-factor and manager-profile snapshots. The API lifespan calls
    this at shutdown.

    No file is open by then: the prime already closed it.
    """
    global _PARK_FACTORS, _PARK_FACTOR_SOURCE, _PARK_FACTORS_MTIME
    global _PARK_FACTORS_CHECKED_AT, _PARK_FACTORS_REFRESH_FAILING
    global _MANAGER_PROFILES

    _PARK_FACTORS = None
    _MANAGER_PROFILES = None
    _PARK_FACTOR_SOURCE = None
    _PARK_FACTORS_MTIME = None
    _PARK_FACTORS_CHECKED_AT = 0.0
    _PARK_FACTORS_REFRESH_FAILING = False
