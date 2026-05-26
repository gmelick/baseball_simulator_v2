"""
db/sim_store.py
===============
SIM-356 -- durable sim-result + pitch-snapshot persistence (Phase 5, Sprint 2).

WHAT THIS IS
------------
The single, mockable persistence layer behind the Phase-5 sim-history + replay
surface. It replaces the ephemeral Redis / ``sim.lineup_state.simulation_results``
JSONB blob (24h TTL, overwritten per matchup) with two durable stores, split per
the project's hybrid Postgres/DuckDB convention:

  1. **Postgres -- sim-run result history** (``sim.sim_runs``, Alembic 0014).
     One row per Monte-Carlo run holding the full
     :class:`simulation.results.GameSimSummary` as a ``to_jsonable`` JSONB dict.
     Backs the /simulate result lookup and "last N sims of game X" history.
     Accessed async via an asyncpg connection.

  2. **DuckDB -- pitch-level play-stream** (``sim.play_stream``, DuckDB 0008).
     One row per pitch, keyed by ``(run_id, sequence)``, carrying exactly the
     fields needed to rebuild a :class:`simulation.snapshots.PlayByPlayEntry`
     (and thus a whole ``PlayByPlay``) without replaying the sim. Backs GET
     .../plays and GET .../state/{at_bat}/{pitch} (SIM-357). Accessed sync via a
     duckdb connection.

The two stores join on ``run_id``: ``store_sim_run`` returns the Postgres
``run_id`` (BIGSERIAL), and the caller passes that same ``run_id`` to
``store_play_stream`` so the pitch stream is attributable to its summary.

DESIGN
------
  * **DB access only -- no domain logic.** Functions take a live connection
    (asyncpg for Postgres, duckdb for DuckDB) and do exactly one round-trip of
    work. They never open connections, manage transactions beyond a single
    statement batch, or import the FastAPI app. This keeps every function unit-
    testable against a stub connection (Postgres) or an in-memory ``:memory:``
    DuckDB (the real engine).
  * **Serialize at the boundary, not here.** ``store_sim_run`` accepts an
    already-jsonable ``summary`` dict (build it with
    ``api.serialization.to_jsonable(game_sim_summary)``); this module does not
    import numpy or the simulation dataclasses, so it stays dependency-light.
  * **Stable, documented row shapes.** The play-row schema below is the contract
    the SIM-357 endpoints write against and read back -- it is pinned here so
    both writer and reader agree without coupling to the table DDL.

------------------------------------------------------------------------------
PLAY-ROW SCHEMA (the dict shape store_play_stream writes / load_play_stream returns)
------------------------------------------------------------------------------
Each play entry is a plain dict. The required keys are the
:class:`~simulation.snapshots.PlayByPlayEntry` constructor inputs; build one
straight from a ``PlayByPlayEntry`` (e.g. ``dataclasses.asdict(entry)``) or a
``PlayResult`` + indices. Keys (column <- dict key):

    sequence        int            REQUIRED  global 0-based pitch index (order key)
    at_bat          int            REQUIRED  0-based plate-appearance index
    pitch           int            REQUIRED  1-based pitch number within the PA
    pitch_outcome   str            REQUIRED  ball/called_strike/.../in_play
    is_contact      bool           default False
    is_pa_end       bool           default False
    event           str | None     default None  resolved PA event (terminal pitch)
    runs_scored     int            default 0     integer runs that scored
    outs_recorded   int            default 0     outs recorded by the play
    exit_velo       float | None   default None   batted-ball stats (None if no contact)
    launch_angle    float | None   default None
    spray_angle     float | None   default None
    runs            float          default 0.0   RE24 / linear-weight run value
    canonical_event str | None     default None   canonical RUN_VALUES key

``load_play_stream`` returns dicts with ALL of these keys (defaults filled),
ordered by ``sequence`` ascending. ``run_id`` / ``game_pk`` are NOT in the entry
dict -- they are the lookup keys, supplied as call arguments. A returned row maps
directly onto ``PlayByPlayEntry(**row)``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# ---------------------------------------------------------------------------
# Play-row schema (the single source of truth shared by writer + reader)
# ---------------------------------------------------------------------------

#: Ordered tuple of play-entry fields == sim.play_stream non-key columns, in DDL
#: order. SIM-357 builds rows with these keys; load_play_stream returns them.
PLAY_ROW_FIELDS: tuple[str, ...] = (
    "sequence",
    "at_bat",
    "pitch",
    "pitch_outcome",
    "is_contact",
    "is_pa_end",
    "event",
    "runs_scored",
    "outs_recorded",
    "exit_velo",
    "launch_angle",
    "spray_angle",
    "runs",
    "canonical_event",
)

#: Per-field default applied when a play-entry dict omits an optional key. The
#: three positional-required fields (sequence/at_bat/pitch) + pitch_outcome have
#: NO default and raise KeyError if missing -- they identify and classify the
#: pitch and must always be present.
_PLAY_ROW_DEFAULTS: dict[str, Any] = {
    "is_contact": False,
    "is_pa_end": False,
    "event": None,
    "runs_scored": 0,
    "outs_recorded": 0,
    "exit_velo": None,
    "launch_angle": None,
    "spray_angle": None,
    "runs": 0.0,
    "canonical_event": None,
}

#: Fields with no default -- a play entry MUST carry these.
_PLAY_ROW_REQUIRED: tuple[str, ...] = (
    "sequence",
    "at_bat",
    "pitch",
    "pitch_outcome",
)


def _normalize_play_row(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Coerce one play-entry mapping into a complete, typed row dict.

    Missing optional keys are filled from ``_PLAY_ROW_DEFAULTS``; missing
    required keys raise ``KeyError``. Numeric/bool fields are cast so a numpy
    scalar or str leaking in from a JSON layer is normalized before it hits the
    DB. Unknown keys are ignored (forward-compatible with extra entry metadata).
    """
    row: dict[str, Any] = {}
    for f in _PLAY_ROW_REQUIRED:
        if f not in entry:
            raise KeyError(f"play entry missing required field {f!r}")
    row["sequence"] = int(entry["sequence"])
    row["at_bat"] = int(entry["at_bat"])
    row["pitch"] = int(entry["pitch"])
    row["pitch_outcome"] = str(entry["pitch_outcome"])
    row["is_contact"] = bool(entry.get("is_contact", _PLAY_ROW_DEFAULTS["is_contact"]))
    row["is_pa_end"] = bool(entry.get("is_pa_end", _PLAY_ROW_DEFAULTS["is_pa_end"]))
    _event = entry.get("event", _PLAY_ROW_DEFAULTS["event"])
    row["event"] = None if _event is None else str(_event)
    row["runs_scored"] = int(entry.get("runs_scored", _PLAY_ROW_DEFAULTS["runs_scored"]))
    row["outs_recorded"] = int(entry.get("outs_recorded", _PLAY_ROW_DEFAULTS["outs_recorded"]))
    row["exit_velo"] = _opt_float(entry.get("exit_velo", _PLAY_ROW_DEFAULTS["exit_velo"]))
    row["launch_angle"] = _opt_float(entry.get("launch_angle", _PLAY_ROW_DEFAULTS["launch_angle"]))
    row["spray_angle"] = _opt_float(entry.get("spray_angle", _PLAY_ROW_DEFAULTS["spray_angle"]))
    row["runs"] = float(entry.get("runs", _PLAY_ROW_DEFAULTS["runs"]))
    _canon = entry.get("canonical_event", _PLAY_ROW_DEFAULTS["canonical_event"])
    row["canonical_event"] = None if _canon is None else str(_canon)
    return row


def _opt_float(value: Any) -> float | None:
    """Cast to float, preserving None (the 'no batted-ball stat' sentinel)."""
    return None if value is None else float(value)


# ===========================================================================
# Postgres -- sim.sim_runs (async asyncpg)
# ===========================================================================

#: INSERT one run; RETURNING run_id so the caller can key the play-stream on it.
_SQL_INSERT_SIM_RUN = """
    INSERT INTO sim.sim_runs (game_pk, n_iterations, base_seed, summary)
    VALUES ($1, $2, $3, $4::jsonb)
    RETURNING run_id
"""

#: The latest run for a game_pk (idx_sim_runs_game_created serves the ORDER BY).
_SQL_LOAD_LATEST_SIM_RUN = """
    SELECT run_id, game_pk, n_iterations, base_seed, summary, created_at
    FROM sim.sim_runs
    WHERE game_pk = $1
    ORDER BY created_at DESC
    LIMIT 1
"""

#: A specific run by id.
_SQL_LOAD_SIM_RUN = """
    SELECT run_id, game_pk, n_iterations, base_seed, summary, created_at
    FROM sim.sim_runs
    WHERE run_id = $1
"""

#: The last N runs for a game_pk, newest first.
_SQL_LIST_SIM_RUNS = """
    SELECT run_id, game_pk, n_iterations, base_seed, summary, created_at
    FROM sim.sim_runs
    WHERE game_pk = $1
    ORDER BY created_at DESC
    LIMIT $2
"""


async def store_sim_run(
    conn: Any,
    *,
    game_pk: int,
    summary: Mapping[str, Any],
    n_iterations: int,
    base_seed: int | None = None,
) -> int:
    """Persist one Monte-Carlo run's summary; return its new ``run_id``.

    ``summary`` must already be JSON-serializable -- build it with
    ``api.serialization.to_jsonable(game_sim_summary)``. ``base_seed`` is the
    run's base RNG seed (None when the run was not seeded). The returned
    ``run_id`` is the handle to pass to ``store_play_stream``.
    """
    import json

    run_id = await conn.fetchval(
        _SQL_INSERT_SIM_RUN,
        int(game_pk),
        int(n_iterations),
        None if base_seed is None else int(base_seed),
        json.dumps(summary),
    )
    return int(run_id)


async def load_latest_sim_run(conn: Any, game_pk: int) -> dict | None:
    """Load the most-recent run for ``game_pk`` (None if the game has none)."""
    row = await conn.fetchrow(_SQL_LOAD_LATEST_SIM_RUN, int(game_pk))
    return None if row is None else _sim_run_row_to_dict(row)


async def load_sim_run(conn: Any, run_id: int) -> dict | None:
    """Load a specific run by ``run_id`` (None if absent)."""
    row = await conn.fetchrow(_SQL_LOAD_SIM_RUN, int(run_id))
    return None if row is None else _sim_run_row_to_dict(row)


async def list_sim_runs(conn: Any, game_pk: int, *, limit: int = 10) -> list[dict]:
    """The last ``limit`` runs for ``game_pk``, newest first (sim history)."""
    rows = await conn.fetch(_SQL_LIST_SIM_RUNS, int(game_pk), int(limit))
    return [_sim_run_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# SIM-386: live in-progress game state (sim.lineup_state)
# ---------------------------------------------------------------------------

#: Read the most-recently-updated live row for a game_pk (the partial index
#: idx_lineup_state_live on (is_live_game, updated_at) makes this fast).
_SQL_LOAD_LIVE_GAME_STATE = """
    SELECT session_id, game_pk, game_state, updated_at
      FROM sim.lineup_state
     WHERE game_pk = $1
       AND is_live_game = TRUE
     ORDER BY updated_at DESC
     LIMIT 1
"""


async def load_live_game_state(conn: Any, game_pk: int) -> dict | None:
    """Load the live game state for ``game_pk`` (``None`` if not live or absent).

    Returns ``{session_id, game_pk, game_state (parsed dict), updated_at (ISO str)}``.
    The ``game_state`` dict is decoded from the JSONB blob written by
    :class:`~pipeline.live.live_ingestion_pipeline.GameStateBuilder`; its keys
    mirror the required fields documented in that class's docstring (inning, half,
    outs, balls, strikes, home_score, away_score, home_lineup, away_lineup, etc.).
    """
    row = await conn.fetchrow(_SQL_LOAD_LIVE_GAME_STATE, int(game_pk))
    return None if row is None else _live_state_row_to_dict(row)


def _live_state_row_to_dict(row: Mapping[str, Any]) -> dict:
    """Map a ``sim.lineup_state`` row to the public dict, parsing the JSONB blob."""
    import json

    game_state = row["game_state"]
    if isinstance(game_state, (str, bytes, bytearray)):
        game_state = json.loads(game_state)
    updated_at = row["updated_at"]
    return {
        "session_id": str(row["session_id"]),
        "game_pk": int(row["game_pk"]),
        "game_state": game_state,
        "updated_at": (
            updated_at.isoformat()
            if hasattr(updated_at, "isoformat")
            else str(updated_at)
            if updated_at is not None
            else None
        ),
    }


def _sim_run_row_to_dict(row: Mapping[str, Any]) -> dict:
    """Map a ``sim.sim_runs`` row to the public dict, parsing the JSONB summary.

    asyncpg returns a ``jsonb`` column as a ``str`` unless a codec is set; we
    decode it here so callers always get a real dict for ``summary``. ``run_id``
    / ``base_seed`` are ``int|None``; ``created_at`` is passed through (a
    ``datetime`` from asyncpg, a str/None from a stub).
    """
    summary = row["summary"]
    if isinstance(summary, (str, bytes, bytearray)):
        import json

        summary = json.loads(summary)
    base_seed = row["base_seed"]
    return {
        "run_id": int(row["run_id"]),
        "game_pk": int(row["game_pk"]),
        "n_iterations": int(row["n_iterations"]),
        "base_seed": None if base_seed is None else int(base_seed),
        "summary": summary,
        "created_at": row["created_at"],
    }


# ===========================================================================
# DuckDB -- sim.play_stream (sync duckdb)
# ===========================================================================

#: Bulk INSERT one pitch row. Column order == PLAY_ROW_FIELDS preceded by the
#: two lookup keys (run_id, game_pk).
_DUCK_INSERT_PLAY = """
    INSERT INTO sim.play_stream (
        run_id, game_pk,
        sequence, at_bat, pitch, pitch_outcome, is_contact, is_pa_end, event,
        runs_scored, outs_recorded, exit_velo, launch_angle, spray_angle,
        runs, canonical_event
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

#: Load a whole stream for a (game_pk[, run_id]), ordered by sequence. The
#: SELECT list == PLAY_ROW_FIELDS so a positional fetch maps back by index.
_DUCK_SELECT_PLAY_COLS = ", ".join(PLAY_ROW_FIELDS)


def store_play_stream(
    con: Any,
    *,
    game_pk: int,
    run_id: int,
    play_entries: Iterable[Mapping[str, Any]],
) -> None:
    """Persist an ordered pitch stream for ``(game_pk, run_id)``.

    ``play_entries`` is an iterable of play-row dicts (see the PLAY-ROW SCHEMA in
    the module docstring) -- typically ``dataclasses.asdict(entry)`` for each
    :class:`~simulation.snapshots.PlayByPlayEntry`, or rows built straight from
    ``PlayResult`` + indices. Each is normalized (defaults filled, types cast)
    before insert. A no-op for an empty iterable. Idempotency / replacement is
    the caller's concern (the PK is ``(run_id, sequence)``); a fresh ``run_id``
    per run avoids collisions.
    """
    rows = [_normalize_play_row(e) for e in play_entries]
    if not rows:
        return
    params = [
        (
            int(run_id),
            int(game_pk),
            *(r[f] for f in PLAY_ROW_FIELDS),
        )
        for r in rows
    ]
    con.executemany(_DUCK_INSERT_PLAY, params)


def load_play_stream(
    con: Any,
    *,
    game_pk: int,
    run_id: int | None = None,
) -> list[dict]:
    """Load a pitch stream for ``game_pk``, ordered by ``sequence`` ascending.

    With ``run_id`` given, only that run's stream is returned; without it, every
    row for ``game_pk`` is returned (ordered by run_id then sequence so distinct
    runs stay grouped). Each returned dict carries the full PLAY-ROW SCHEMA key
    set and maps directly onto ``PlayByPlayEntry(**row)``.
    """
    if run_id is None:
        sql = (
            f"SELECT {_DUCK_SELECT_PLAY_COLS} FROM sim.play_stream "
            "WHERE game_pk = ? ORDER BY run_id, sequence"
        )
        cur = con.execute(sql, [int(game_pk)])
    else:
        sql = (
            f"SELECT {_DUCK_SELECT_PLAY_COLS} FROM sim.play_stream "
            "WHERE game_pk = ? AND run_id = ? ORDER BY sequence"
        )
        cur = con.execute(sql, [int(game_pk), int(run_id)])
    out: list[dict] = []
    for rec in cur.fetchall():
        row = dict(zip(PLAY_ROW_FIELDS, rec, strict=False))
        # Normalize duckdb's returned types back to plain python (DuckDB already
        # yields python scalars; the casts make the contract explicit + guard
        # against a fake cursor returning numpy-ish values).
        row["sequence"] = int(row["sequence"])
        row["at_bat"] = int(row["at_bat"])
        row["pitch"] = int(row["pitch"])
        row["pitch_outcome"] = str(row["pitch_outcome"])
        row["is_contact"] = bool(row["is_contact"])
        row["is_pa_end"] = bool(row["is_pa_end"])
        row["event"] = None if row["event"] is None else str(row["event"])
        row["runs_scored"] = int(row["runs_scored"])
        row["outs_recorded"] = int(row["outs_recorded"])
        row["exit_velo"] = _opt_float(row["exit_velo"])
        row["launch_angle"] = _opt_float(row["launch_angle"])
        row["spray_angle"] = _opt_float(row["spray_angle"])
        row["runs"] = float(row["runs"])
        row["canonical_event"] = (
            None if row["canonical_event"] is None else str(row["canonical_event"])
        )
        out.append(row)
    return out


# ===========================================================================
# DuckDB -- sim.state_snapshots (sync duckdb)  [SIM-357]
# ===========================================================================
#
# The /state/{at_bat}/{pitch} endpoint needs the field/baserunner/count state AS
# OF a pitch, which the play_stream rows do NOT carry. This store persists one
# serialized ``simulation.snapshots.StateAtPitch`` per pitch (a numpy-free JSON
# object produced by ``api.serialization.to_jsonable`` + ``json.dumps``), keyed
# by (run_id, sequence) and looked up by (game_pk[, run_id], at_bat, pitch).
#
# A snapshot dict handed to ``store_state_snapshots`` carries:
#     at_bat    int     REQUIRED  0-based plate-appearance index this is as-of
#     pitch     int     REQUIRED  1-based pitch number within the PA
#     sequence  int     REQUIRED  global 0-based pitch index (order key / PK part)
#     field     dict    REQUIRED  the serialized FieldSnapshot (the StateAtPitch's
#                                 ``field`` -- the BaseballFieldGraphic state)
# i.e. exactly ``to_jsonable(state_at_pitch)``: ``{at_bat, pitch, field, sequence}``.
# The WHOLE jsonable StateAtPitch dict is stored verbatim as JSON text in the
# ``snapshot`` column; ``load_state_at`` returns it parsed back to a dict so the
# endpoint can rebuild a ``StateAtPitchModel`` directly from it.

#: INSERT one snapshot row. Column order: the two lookup keys (run_id, game_pk),
#: the index columns (at_bat, pitch, sequence), then the JSON snapshot text.
_DUCK_INSERT_STATE = """
    INSERT INTO sim.state_snapshots (
        run_id, game_pk, at_bat, pitch, sequence, snapshot
    ) VALUES (?, ?, ?, ?, ?, ?)
"""


def store_state_snapshots(
    con: Any,
    *,
    game_pk: int,
    run_id: int,
    snapshots: Iterable[Mapping[str, Any]],
) -> None:
    """Persist one per-pitch state-snapshot stream for ``(game_pk, run_id)``.

    ``snapshots`` is an iterable of jsonable ``StateAtPitch`` dicts -- typically
    ``api.serialization.to_jsonable(state_at_pitch)`` for each pitch, i.e. a dict
    with keys ``at_bat`` / ``pitch`` / ``sequence`` / ``field``. Each snapshot is
    indexed by its ``at_bat`` / ``pitch`` / ``sequence`` and the whole dict is
    stored as JSON text in the ``snapshot`` column. ``sequence`` is required (it
    is the PK component); a snapshot missing ``at_bat`` / ``pitch`` / ``sequence``
    raises ``KeyError``. A no-op for an empty iterable. Idempotency / replacement
    is the caller's concern (PK is ``(run_id, sequence)``); a fresh ``run_id`` per
    run avoids collisions.
    """
    import json

    params: list[tuple] = []
    for snap in snapshots:
        if "sequence" not in snap or snap["sequence"] is None:
            raise KeyError("state snapshot missing required field 'sequence'")
        if "at_bat" not in snap:
            raise KeyError("state snapshot missing required field 'at_bat'")
        if "pitch" not in snap:
            raise KeyError("state snapshot missing required field 'pitch'")
        params.append(
            (
                int(run_id),
                int(game_pk),
                int(snap["at_bat"]),
                int(snap["pitch"]),
                int(snap["sequence"]),
                json.dumps(snap),
            )
        )
    if not params:
        return
    con.executemany(_DUCK_INSERT_STATE, params)


def _state_row_to_dict(rec: Any) -> dict:
    """Map a ``sim.state_snapshots`` SELECT row to the parsed snapshot dict.

    The SELECT yields (at_bat, pitch, sequence, snapshot); the JSON ``snapshot``
    text is parsed back to a dict so callers always get the real jsonable
    ``StateAtPitch`` structure ({at_bat, pitch, field, sequence}).
    """
    import json

    at_bat, pitch, sequence, snapshot = rec
    parsed = json.loads(snapshot) if isinstance(snapshot, (str, bytes, bytearray)) else snapshot
    return {
        "at_bat": int(at_bat),
        "pitch": int(pitch),
        "sequence": int(sequence),
        "snapshot": parsed,
    }


def load_state_at(
    con: Any,
    *,
    game_pk: int,
    at_bat: int,
    pitch: int,
    run_id: int | None = None,
) -> dict | None:
    """Load the state snapshot for ``(game_pk, at_bat, pitch)`` (None if absent).

    With ``run_id`` given, only that run's snapshot is consulted; without it the
    most-recent run's matching pitch is returned (ORDER BY run_id DESC so the
    latest run wins). The returned dict is
    ``{at_bat, pitch, sequence, snapshot}`` where ``snapshot`` is the parsed,
    numpy-free jsonable ``StateAtPitch`` ({at_bat, pitch, field, sequence}) the
    /state endpoint rebuilds a ``StateAtPitchModel`` from. ``None`` means no
    snapshot was persisted for that pitch -> the endpoint returns 404.
    """
    if run_id is None:
        sql = (
            "SELECT at_bat, pitch, sequence, snapshot FROM sim.state_snapshots "
            "WHERE game_pk = ? AND at_bat = ? AND pitch = ? "
            "ORDER BY run_id DESC LIMIT 1"
        )
        cur = con.execute(sql, [int(game_pk), int(at_bat), int(pitch)])
    else:
        sql = (
            "SELECT at_bat, pitch, sequence, snapshot FROM sim.state_snapshots "
            "WHERE game_pk = ? AND run_id = ? AND at_bat = ? AND pitch = ? "
            "ORDER BY sequence LIMIT 1"
        )
        cur = con.execute(sql, [int(game_pk), int(run_id), int(at_bat), int(pitch)])
    rec = cur.fetchone()
    return None if rec is None else _state_row_to_dict(rec)


def load_state_snapshots(
    con: Any,
    *,
    game_pk: int,
    run_id: int | None = None,
) -> list[dict]:
    """Load the whole ordered state-snapshot stream for ``game_pk``.

    With ``run_id`` given, only that run's snapshots are returned (ordered by
    ``sequence``); without it, every row for ``game_pk`` is returned (ordered by
    run_id then sequence so distinct runs stay grouped). Each dict is
    ``{at_bat, pitch, sequence, snapshot}`` (see :func:`load_state_at`).
    """
    if run_id is None:
        sql = (
            "SELECT at_bat, pitch, sequence, snapshot FROM sim.state_snapshots "
            "WHERE game_pk = ? ORDER BY run_id, sequence"
        )
        cur = con.execute(sql, [int(game_pk)])
    else:
        sql = (
            "SELECT at_bat, pitch, sequence, snapshot FROM sim.state_snapshots "
            "WHERE game_pk = ? AND run_id = ? ORDER BY sequence"
        )
        cur = con.execute(sql, [int(game_pk), int(run_id)])
    return [_state_row_to_dict(rec) for rec in cur.fetchall()]


# ===========================================================================
# DuckDB -- sim.game_cards (sync duckdb)  [SIM-362/364]
# ===========================================================================
#
# The /linescore + /decisions endpoints need the game-level DERIVED loop outputs
# -- the per-inning linescore (R/H/E grid) and the W/L/Save pitcher decisions --
# which the play_stream / state_snapshots rows do NOT carry. Those derivations
# (simulation.linescore.linescore_from_plays +
# simulation.pitcher_decisions.decisions_from_plays) read PlayResult.next_state,
# which the persisted PlayByPlayEntry rows drop, so they MUST be computed at
# RECORD time (from the recorded PlayResult list) and persisted here -- NOT
# re-derived at read time. This store persists one "game card" per run: two
# numpy-free JSON blobs (the to_jsonable Linescore + PitcherDecisions), keyed by
# run_id and looked up by (game_pk[, run_id]).
#
# A card handed to ``store_game_card`` carries:
#     linescore   dict   REQUIRED  to_jsonable(Linescore): {innings:[{inning,away,
#                                   home}], away_runs, home_runs, away_hits,
#                                   home_hits, away_errors, home_errors, ...}
#     decisions   dict   REQUIRED  to_jsonable(PitcherDecisions): {winning_pitcher_id,
#                                   losing_pitcher_id, save_pitcher_id, home_score,
#                                   away_score}
# Both whole dicts are stored verbatim as JSON text; ``load_game_card`` returns
# them parsed back so the endpoint rebuilds the response models directly.

#: INSERT one game-card row. Column order: the two lookup keys (run_id, game_pk),
#: then the two JSON blobs (linescore, decisions).
_DUCK_INSERT_GAME_CARD = """
    INSERT INTO sim.game_cards (run_id, game_pk, linescore, decisions)
    VALUES (?, ?, ?, ?)
"""


def store_game_card(
    con: Any,
    *,
    game_pk: int,
    run_id: int,
    linescore: Mapping[str, Any],
    decisions: Mapping[str, Any],
) -> None:
    """Persist one game's derived loop card (linescore + decisions) for a run.

    ``linescore`` / ``decisions`` are the already-jsonable dicts
    (``api.serialization.to_jsonable(linescore)`` /
    ``to_jsonable(pitcher_decisions)``); each whole dict is stored verbatim as
    JSON text. Keyed by ``run_id`` (the PK); ``game_pk`` is denormalized for the
    by-game lookup. Idempotency / replacement is the caller's concern (a fresh
    ``run_id`` per run avoids collisions); a duplicate ``run_id`` raises the
    engine's PK-violation error.
    """
    import json

    con.execute(
        _DUCK_INSERT_GAME_CARD,
        [
            int(run_id),
            int(game_pk),
            json.dumps(linescore),
            json.dumps(decisions),
        ],
    )


def _game_card_row_to_dict(rec: Any) -> dict:
    """Map a ``sim.game_cards`` SELECT row to the parsed card dict.

    The SELECT yields (run_id, game_pk, linescore, decisions); the two JSON text
    blobs are parsed back to dicts so callers always get the real jsonable
    structures the /linescore + /decisions endpoints rebuild their models from.
    """
    import json

    run_id, game_pk, linescore, decisions = rec
    if isinstance(linescore, (str, bytes, bytearray)):
        linescore = json.loads(linescore)
    if isinstance(decisions, (str, bytes, bytearray)):
        decisions = json.loads(decisions)
    return {
        "run_id": int(run_id),
        "game_pk": int(game_pk),
        "linescore": linescore,
        "decisions": decisions,
    }


def load_game_card(
    con: Any,
    *,
    game_pk: int,
    run_id: int | None = None,
) -> dict | None:
    """Load the game card for ``game_pk`` (None if none has been persisted).

    With ``run_id`` given, only that run's card is consulted; without it the
    most-recent run's card is returned (ORDER BY run_id DESC so the latest run
    wins). The returned dict is ``{run_id, game_pk, linescore, decisions}`` where
    ``linescore`` / ``decisions`` are the parsed, numpy-free jsonable structures
    the /linescore + /decisions (+ /card) endpoints rebuild their models from.
    ``None`` means no card was persisted -> the endpoint returns 404.
    """
    if run_id is None:
        sql = (
            "SELECT run_id, game_pk, linescore, decisions FROM sim.game_cards "
            "WHERE game_pk = ? ORDER BY run_id DESC LIMIT 1"
        )
        cur = con.execute(sql, [int(game_pk)])
    else:
        sql = (
            "SELECT run_id, game_pk, linescore, decisions FROM sim.game_cards "
            "WHERE game_pk = ? AND run_id = ? LIMIT 1"
        )
        cur = con.execute(sql, [int(game_pk), int(run_id)])
    rec = cur.fetchone()
    return None if rec is None else _game_card_row_to_dict(rec)


__all__ = [
    "PLAY_ROW_FIELDS",
    # Postgres
    "store_sim_run",
    "load_latest_sim_run",
    "load_sim_run",
    "list_sim_runs",
    "load_live_game_state",
    # DuckDB -- play stream
    "store_play_stream",
    "load_play_stream",
    # DuckDB -- state snapshots (SIM-357)
    "store_state_snapshots",
    "load_state_at",
    "load_state_snapshots",
    # DuckDB -- game cards (SIM-362/364)
    "store_game_card",
    "load_game_card",
]
