"""
simulation/sim_kwargs.py
========================
The ONE builder that turns a resolved GameState into ``simulate_game`` kwargs,
plus the venue park-factor resolver it depends on (SIM-449).

WHY THIS EXISTS
---------------
Two callers built these kwargs. ``api/routes/games.py`` built the full set. The
validation harness ``scripts/sim_stats.py`` built a second copy by hand, and that
copy dropped three keys: ``home_defense``, ``away_defense`` and
``park_run_factor``.

Those three keys are the ONLY inputs two production features read.
The fielder consumer (today the SIM-491 fielder kernel) reads the two defense
maps. The park consumer (today the SIM-491 park kernel) reads the park
factor. The harness therefore passed an empty defense map and a neutral 1.0 park
factor on every run. An operator who toggled either flag under the harness
compared two identical no-ops, and the harness reported "no effect". The team read
that as a measurement. It was not one.

This module holds the single definition. ``api/routes/games.py`` imports it, and
so does every harness script, so the kwargs the API sends and the kwargs the
harness sends cannot differ again.

WHAT SIM-449 DID NOT FIX (SIM-452)
----------------------------------
SIM-449 unified the BUILDER. It did not unify the park-factor RESOLUTION. Only
three sites looked the venue factor up and put it on the GameState. The other
five called the builder on a GameState whose ``park_run_factor`` still held the
dataclass default 1.0, so ``SIM_PARK_FACTOR`` was inert for them. One of the five
was ``scripts/clv_backtest.py`` -- the Closing Line Value backtest, the fund's
gold-standard metric.

``SIM_KWARG_KEYS`` could never catch this. The key ``park_run_factor`` is always
PRESENT. It is just always 1.0. And 1.0 is ALSO the correct answer for a genuinely
neutral park, so a reader cannot tell "the lookup returned neutral" from "nobody
looked".

SIM-452 removes the ambiguity with :data:`UNRESOLVED_PARK_FACTOR`. It is a float
subclass that equals 1.0, so every reader that only wants the number keeps
working, but ``isinstance`` tells it apart from a resolved 1.0.
:func:`api.routes.games._resolve_state_or_error` stamps it on EVERY GameState it
returns, and :func:`sim_kwargs_from_state` REFUSES to build kwargs from a state
that still carries it. A caller that skips the lookup now raises
:class:`UnresolvedParkFactorError` instead of shipping a park-blind simulation.

The sentinel lives in the field the lookup writes, so a real lookup clears it by
simple assignment. A separate "resolved" boolean would have to be cleared by hand,
and the hand that forgets is the hand this ticket exists for.

WHAT SIM-452 STILL GOT WRONG (SIM-453)
--------------------------------------
SIM-452 stamped the sentinel, then threw it away again. ``resolve_park_run_factor``
returned a PLAIN ``1.0`` on every failure branch -- no DuckDB, unknown venue, no
park-factor row, a query error. ``resolve_park_factor_onto_state`` wrote that plain
float onto the state, which CLEARED the sentinel. So the state read as "resolved"
after a lookup that resolved nothing.

Reproduced on the shipped code (Python 3.13, in the app container)::

    mark_park_factor_unresolved(state)   -> park_factor_is_resolved(state) is False
    await resolve_park_run_factor(None, None, 12345, 2024) -> 1.0 (plain float)
    state.park_run_factor = <that>       -> park_factor_is_resolved(state) is True

The sentinel therefore proved the resolver was CALLED. It never proved anything
was RESOLVED. And the stack where the resolver answers nothing is the DEFAULT
one: ``api/main.py`` only opens ``app.state.sim_duckdb`` when
``REPLAY_PERSISTENCE_ENABLED`` is true, and that variable is set in neither
``.env`` nor ``docker-compose.yml``. Every API route therefore shipped a neutral
1.0 while the instrument reported "resolved".

SIM-453 makes the two states stay apart end to end:

* Every unavailable branch returns :data:`UNRESOLVED_PARK_FACTOR` carrying a
  ``reason``, not a plain 1.0. The state stays UNRESOLVED.
* Every unavailable branch logs at WARNING, deduplicated by reason, so an
  operator sees it once per cause instead of never or 2,400 times.
* :func:`sim_kwargs_from_state` still REFUSES an unresolved state. Proceeding is
  possible but never implicit: the caller passes
  ``allow_unavailable_park_factor=True`` and gets another WARNING for it.
* :func:`build_sim_kwargs` proceeds loudly by DEFAULT, because ``/simulate`` must
  keep answering on a stack with no sim DuckDB. It does NOT clear the sentinel,
  so the caller can still ask the state what happened.

HOW THE DEFAULT STACK GETS ITS PARK FACTORS BACK (SIM-453)
----------------------------------------------------------
Reading ``derived.park_factors`` is a READ. Replay persistence is a WRITE. The two
were gated by one variable, and that is why the park factor went dark by default.
:func:`prime_park_factor_source` opens the sim DuckDB READ-ONLY as its own
concern. ``api/main.py`` calls it in the lifespan whether or not replay is on, and
:func:`resolve_park_run_factor` falls back to it when its caller passes
``con=None``.

The fallback is live only after an explicit prime. Nothing primes it in the unit
lane, so a unit test that passes ``con=None`` still gets the unavailable sentinel
and never touches a file.

WHY IT LIVES UNDER simulation/
------------------------------
``docker-compose.yml`` bind-mounts ``api/``, ``pipeline/``, ``similarity/``,
``simulation/`` and ``db/`` into the app service. It does NOT mount ``scripts/``.
A shared module under ``scripts/`` would need an image rebuild to reach the
container, so the shared module lives under a mounted package.

Owner: Backend Developer (SIM-449, SIM-452, SIM-453).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: The default sim DuckDB path. The API lifespan (``api/main.py``) and the
#: play-pool sampler read the same env var, so a harness opens the same file.
DEFAULT_DUCKDB_PATH = "/data/baseball_sim.duckdb"


# ---------------------------------------------------------------------------
# SIM-452 -- the unresolved-park-factor sentinel
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
    behaves exactly as before. ``isinstance`` separates it from a plain ``1.0``
    that a park-factor lookup really produced, which is the distinction the whole
    defect turns on.

    SIM-453: the instance carries the ``reason`` it is unresolved, so the warning
    an operator reads names the cause ("no park-factor source", "no row for venue
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
        # back a plain 1.0 on the far side -- the exact defect, restored by pickle.
        return (_UnresolvedParkFactor, (self.reason,))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"UNRESOLVED_PARK_FACTOR({self.reason!r})"


#: The park factor a GameState carries before anybody looks the venue up.
#: :func:`api.routes.games._resolve_state_or_error` stamps it on every state it
#: returns; :func:`sim_kwargs_from_state` refuses to build kwargs while it is there.
UNRESOLVED_PARK_FACTOR: float = _UnresolvedParkFactor("nobody looked the venue up")


def park_factor_unavailable(reason: str) -> float:
    """Build an UNRESOLVED park factor that records why the source could not answer.

    SIM-453. ``resolve_park_run_factor`` returns this instead of a plain ``1.0`` on
    every branch where the lookup fails. "I could not look it up" and "I looked it
    up and this park is neutral" are different answers, and only the second one is
    a plain ``1.0``.
    """
    return _UnresolvedParkFactor(reason)


def park_factor_reason(value: Any) -> str | None:
    """Why ``value`` is unresolved, or ``None`` when it is a resolved factor (SIM-453)."""
    if isinstance(value, _UnresolvedParkFactor):
        return value.reason
    return None


def mark_park_factor_unresolved(state: Any) -> None:
    """Stamp the unresolved sentinel on ``state`` (SIM-452).

    The state factory calls this. From this point the state cannot produce sim
    kwargs until somebody resolves the venue factor onto it.
    """
    state.park_run_factor = UNRESOLVED_PARK_FACTOR


def _warn_once(reason: str) -> None:
    """Log ``reason`` at WARNING the first time this process sees it (SIM-453).

    A season backtest asks 2,400 times. One line per CAUSE is loud. 2,400 lines per
    cause is noise an operator learns to scroll past, which is how a silent neutral
    survives in a log that technically mentions it.
    """
    if reason in _WARNED_REASONS:
        return
    _WARNED_REASONS.add(reason)
    log.warning("SIM-453: park factor UNRESOLVED — %s. The run proceeds park-blind.", reason)


def reset_park_factor_warnings() -> None:
    """Clear the warn-once memo (SIM-453). Tests call this; production does not."""
    _WARNED_REASONS.clear()


#: Reasons already logged in this process. See :func:`_warn_once`.
_WARNED_REASONS: set[str] = set()


def park_factor_is_resolved(state: Any) -> bool:
    """Report whether somebody resolved ``state``'s park factor (SIM-452).

    A state that never carried the sentinel counts as resolved. The harnesses that
    build a GameState by hand (``scripts/sim_stats.py``, the acceptance lane) set the
    factor themselves, and a bare unit-test GameState keeps the dataclass default.
    """
    return not isinstance(getattr(state, "park_run_factor", None), _UnresolvedParkFactor)


#: Every key :func:`sim_kwargs_from_state` returns. A unit test asserts the built
#: dict against this set, so a dropped key fails CI instead of turning a
#: production feature into a silent no-op -- the SIM-449 defect.
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
        "max_innings",
    }
)


def sim_kwargs_from_state(
    state: Any, *, allow_unavailable_park_factor: bool = False
) -> dict[str, Any]:
    """Build the ``simulate_game`` kwargs from a resolved GameState (SIM-353; extracted SIM-449).

    Pulls the lineup contract off the GameState's public fields (the same fields
    ``lineup_resolver.build_game_state`` populated) into the ``sim_kwargs`` dict
    the BatchRunner / ``simulate_game`` consume.

    This is the ONLY definition. ``api/routes/games.py`` and every harness script
    import it, so the kwargs the API sends and the kwargs the validation harness
    sends cannot differ.

    Raises :class:`UnresolvedParkFactorError` when ``state`` still carries
    :data:`UNRESOLVED_PARK_FACTOR` (SIM-452). Prefer :func:`build_sim_kwargs`,
    which resolves first and then calls this.

    ``allow_unavailable_park_factor=True`` is the EXPLICIT park-blind path
    (SIM-453). It builds anyway, logs at WARNING, and sends a plain neutral 1.0.
    It does not touch ``state``, so the state still reports the truth afterwards.
    Nothing takes this path by accident: the caller has to name it.
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
    return {
        "away_lineup": list(getattr(state, "away_lineup", []) or []),
        "home_lineup": list(getattr(state, "home_lineup", []) or []),
        "season": int(getattr(state, "season", 2024)),
        "pitcher_id": int(getattr(state, "pitcher_id", 0) or 0),
        "bat_hand": str(getattr(state, "bat_hand", "R") or "R"),
        # SIM-421: carry the per-batter hand map + both starters so the matchup
        # pre-filter follows the lineup across PAs/half-innings (picklable, so
        # they survive the ProcessPool sim_kwargs path).
        "bat_hands": dict(getattr(state, "bat_hands", {}) or {}),
        "throw_hands": dict(getattr(state, "throw_hands", {}) or {}),
        "home_pitcher_id": getattr(state, "home_pitcher_id", None),
        "away_pitcher_id": getattr(state, "away_pitcher_id", None),
        # SIM-428: catchers for the framing nudge.
        "home_catcher_id": getattr(state, "home_catcher_id", None),
        "away_catcher_id": getattr(state, "away_catcher_id", None),
        # SIM-425b/476: per-position defense maps for the fielder kernel
        # (SIM_FIELDER_KERNEL_SIGMA). SIM-411/476: the venue run park-factor for
        # the park kernel (SIM_PARK_KERNEL_SIGMA) — resolved onto the state by the caller when a sim
        # DuckDB is available; defaults to 1.0 (neutral) otherwise. Both are no-ops
        # with their gate off, so passing them is always safe.
        "home_defense": dict(getattr(state, "home_defense", {}) or {}),
        "away_defense": dict(getattr(state, "away_defense", {}) or {}),
        "park_run_factor": float(getattr(state, "park_run_factor", 1.0) or 1.0),
        "max_innings": 12,
    }


async def resolve_park_run_factor(pool: Any, con: Any, game_pk: int, season: int) -> float:
    """SIM-411: resolve the venue run park-factor for a game (~1.0 = neutral). Extracted SIM-449.

    The venue lives in Postgres (``raw.games.venue_id``) and the factor in DuckDB
    (``derived.park_factors``), so this is a two-source lookup: fetch the game's
    venue from ``pool``, then its regressed run factor (``factor_type='R'``) for the
    season from ``con`` (``app.state.sim_duckdb``).

    ``con=None`` falls back to the primed read-only park-factor source when
    ``api/main.py`` opened one (SIM-453). That is how the default local stack --
    which never opens ``app.state.sim_duckdb`` -- reads the park factors that are
    sitting in the file.

    RETURN CONTRACT (SIM-453). A real row inside the sane 0.5-2.0 range comes back
    a PLAIN ``float``. Every other outcome -- no source, unknown venue, no row, a
    degenerate value, a query error -- comes back :data:`UNRESOLVED_PARK_FACTOR`
    carrying its reason, and logs at WARNING. The sentinel still ``== 1.0``, so a
    caller that only reads the number is unaffected; a caller that asks
    :func:`park_factor_is_resolved` now gets the truth. Before SIM-453 every one of
    those branches returned a plain ``1.0`` and the failure became invisible.

    ``pool`` may be a connection pool (it has ``acquire``) or a single connection
    (it does not). Both the API pool and a script's asyncpg connection work."""
    if con is None:
        con = _primed_park_factor_connection()
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
        res = con.execute(
            "SELECT regressed_factor FROM derived.park_factors "
            "WHERE venue_id = ? AND season = ? AND factor_type = 'R'",
            [int(venue_id), int(season)],
        ).fetchone()
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


async def resolve_park_factor_onto_state(
    state: Any,
    pool: Any,
    con: Any,
    game_pk: int,
    season: int | None = None,
) -> float:
    """Resolve the venue park factor and WRITE it onto ``state`` (SIM-452).

    A real lookup writes a plain ``float`` and so clears
    :data:`UNRESOLVED_PARK_FACTOR`. An unavailable source writes the sentinel back,
    so the state stays UNRESOLVED (SIM-453). A resolved neutral park is therefore a
    plain ``1.0``, an unavailable one is the sentinel, and the two stay apart all
    the way to the builder.

    The write deliberately does NOT call ``float()``. ``float(sentinel)`` returns a
    plain ``1.0`` and that single call was the SIM-452 defect: it erased the
    sentinel one line after the resolver produced it.

    ``season`` defaults to the state's own season. Returns the factor it wrote.
    """
    if season is None:
        season = int(getattr(state, "season", 2024) or 2024)
    factor = await resolve_park_run_factor(pool, con, int(game_pk), int(season))
    state.park_run_factor = factor
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

    THE one entry point (SIM-452). Every production caller uses it: the four
    ``api/routes/games.py`` routes, ``api/routes/betting.py``,
    ``scripts/clv_backtest.py`` and ``scripts/validate_props.py``.

    ``on_unavailable_park_factor`` picks what happens when the source cannot answer
    (SIM-453):

    * ``"proceed"`` (the default) builds park-blind kwargs and says so at WARNING.
      ``/simulate`` must keep answering on a stack with no sim DuckDB, so refusing
      here would turn a data gap into an HTTP 500. The state KEEPS the sentinel, so
      the caller can still read the truth off it and report it.
    * ``"raise"`` refuses, for a caller whose whole output is invalid park-blind.

    Either way the failure is LOUD. The defect SIM-453 fixes is not "the run
    continued"; it is "the run continued and the instrument said it was fine".

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
    if not park_factor_is_resolved(state) and on_unavailable_park_factor == "proceed":
        log.warning(
            "SIM-453: game_pk=%s simulates PARK-BLIND — %s",
            int(game_pk),
            park_factor_reason(getattr(state, "park_run_factor", None)),
        )
    return sim_kwargs_from_state(
        state,
        allow_unavailable_park_factor=(on_unavailable_park_factor == "proceed"),
    )


def open_sim_duckdb(path: str | None = None) -> Any:
    """Open the sim DuckDB read-only, or return ``None`` (SIM-449).

    :func:`resolve_park_run_factor` needs a DuckDB connection. The API takes one
    from ``app.state.sim_duckdb``. A script has no app state, so it opens its own.
    The connection is READ-ONLY, so a harness never takes the write lock the
    nightly profile computor needs.

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
# SIM-453 -- the park-factor source, separated from replay persistence
#
# THE DECISION, AND WHY.
#
# ``api/main.py`` opened the sim DuckDB only under REPLAY_PERSISTENCE_ENABLED,
# which is set in neither ``.env`` nor ``docker-compose.yml``. So the default
# stack ran park-blind. There were two ways out.
#
#   (a) Set REPLAY_PERSISTENCE_ENABLED=true in docker-compose.yml.
#   (b) Open the park-factor source as its own read-only concern.
#
# We took (b). Three measured reasons, all from this repository:
#
#   1. The variable opens a WRITABLE connection. Reading ``derived.park_factors``
#      needs no write. A writable connection takes the DuckDB write lock, which is
#      exactly the clash ``api/main.py`` names as the reason the flag is off by
#      default.
#   2. The replay tables are NOT in the file. Queried in the app container:
#      ``information_schema.tables WHERE table_schema='sim'`` returns
#      outcome_pool, pitch_pool, pool_build_metadata, stolen_base_pool -- no
#      ``play_stream``, no ``state_snapshots``. Turning replay on would move
#      /plays, /state, /linescore and /card from a clean 503 "replay store
#      unavailable" to a 500 on a missing table.
#   3. The two concerns are unrelated. One reads reference data; the other writes
#      a replay stream. One variable for both is why the park factor went dark,
#      and setting that variable keeps the conflation and adds side effects.
#
# The connection is process-wide because the thing it opens is process-wide: a
# read-only DuckDB handle. It is live only after an explicit prime, so the unit
# lane never touches a file and ``con=None`` there still means "unavailable".
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

    def header_value(self) -> str:
        """The ``X-Park-Factor-Source`` response header value (SIM-453)."""
        return f"duckdb:{self.n_rows}" if self.available else "unavailable"


#: The primed read-only connection, or ``None``. Process-wide by design.
_PARK_FACTOR_CON: Any = None

#: What the last prime found. ``None`` means nobody primed in this process.
_PARK_FACTOR_SOURCE: ParkFactorSource | None = None


def _primed_park_factor_connection() -> Any:
    """The primed read-only connection, or ``None`` (SIM-453)."""
    return _PARK_FACTOR_CON


def prime_park_factor_source(path: str | None = None) -> ParkFactorSource:
    """Open the read-only park-factor source for this process (SIM-453).

    ``api/main.py`` calls this in the lifespan, whether or not replay persistence
    is on, because reading park factors is not replay persistence. After it
    succeeds, :func:`resolve_park_run_factor` uses it whenever its caller passes
    ``con=None`` -- which every ``api/routes/games.py`` route does on the default
    stack.

    The prime also COUNTS ``derived.park_factors``. An empty table is an
    unavailable source, not a neutral one, so the count is part of the verdict.

    Idempotent: a second call while a connection is open returns the same verdict.
    Never raises; a failure returns an unavailable :class:`ParkFactorSource`.
    """
    global _PARK_FACTOR_CON, _PARK_FACTOR_SOURCE

    duckdb_path = path or os.environ.get("BASEBALL_DUCKDB_PATH") or DEFAULT_DUCKDB_PATH
    if _PARK_FACTOR_CON is not None and _PARK_FACTOR_SOURCE is not None:
        return _PARK_FACTOR_SOURCE

    con = open_sim_duckdb(duckdb_path)
    if con is None:
        _PARK_FACTOR_SOURCE = ParkFactorSource(
            available=False,
            path=duckdb_path,
            n_rows=0,
            detail="the read-only DuckDB open failed (missing file, no duckdb module, "
            "or another process holds the write lock)",
        )
        return _PARK_FACTOR_SOURCE

    try:
        row = con.execute("SELECT count(*) FROM derived.park_factors").fetchone()
        n_rows = int(row[0]) if row else 0
    except Exception as exc:  # noqa: BLE001
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
        _PARK_FACTOR_SOURCE = ParkFactorSource(
            available=False,
            path=duckdb_path,
            n_rows=0,
            detail=f"derived.park_factors is unreadable ({type(exc).__name__}: {exc})",
        )
        return _PARK_FACTOR_SOURCE

    if n_rows <= 0:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
        _PARK_FACTOR_SOURCE = ParkFactorSource(
            available=False,
            path=duckdb_path,
            n_rows=0,
            detail="derived.park_factors opened but holds 0 rows",
        )
        return _PARK_FACTOR_SOURCE

    _PARK_FACTOR_CON = con
    _PARK_FACTOR_SOURCE = ParkFactorSource(
        available=True,
        path=duckdb_path,
        n_rows=n_rows,
        detail="read-only; separate from REPLAY_PERSISTENCE_ENABLED",
    )
    return _PARK_FACTOR_SOURCE


def park_factor_source_status() -> ParkFactorSource | None:
    """What the last :func:`prime_park_factor_source` found, or ``None`` (SIM-453)."""
    return _PARK_FACTOR_SOURCE


def close_park_factor_source() -> None:
    """Close the primed read-only source (SIM-453). The API lifespan calls this."""
    global _PARK_FACTOR_CON, _PARK_FACTOR_SOURCE

    if _PARK_FACTOR_CON is not None:
        try:
            _PARK_FACTOR_CON.close()
        except Exception:  # noqa: BLE001
            pass
    _PARK_FACTOR_CON = None
    _PARK_FACTOR_SOURCE = None
