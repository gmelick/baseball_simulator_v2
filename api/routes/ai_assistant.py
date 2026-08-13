"""
api/routes/ai_assistant.py
==========================
SIM-439 — the Data Lab AI Assistant (optional, feature-gated).

    GET  /api/assistant/status   -> {configured, model}
    POST /api/assistant/ask      -> text/event-stream of typed SSE frames

The assistant answers baseball-data questions in natural language by writing a
read-only SQL query, running it through the **exact same** safe path as the SQL
console (``api.routes.sql_safety.run_read_only_sql``), and narrating the result.
It is physically incapable of doing anything the console can't — every query it
runs is validated, read-only, time-limited, row-capped, and shown to the user.

Feature-gating: the Anthropic client is created in ``api/main.py``'s lifespan
only when ``ANTHROPIC_API_KEY`` is set; otherwise ``app.state.anthropic_client``
is ``None``. ``/status`` reports ``configured:false`` so the UI shows a friendly
setup card instead of failing a call, and ``/ask`` returns 503. Nothing here
imports ``anthropic`` at module load, so the app + test suite boot without the
package or the key.

SSE frame types (newline-delimited ``data: {json}`` events):
    {type:'text', text, step}           — the assistant's prose (plan / answer)
    {type:'query', sql, step}           — a SQL query it is about to run
    {type:'result', columns, rows,      — the read-only result (shown verbatim)
                    row_count, truncated, elapsed_ms, step}
    {type:'error', detail, step}        — a rejected/failed query or model error
    {type:'done'}                       — end of turn
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from api.auth import require_auth
from api.routes.sql_safety import (
    DEFAULT_TIMEOUT_MS,
    SqlValidationError,
    run_read_only_sql,
)

log = logging.getLogger("api.routes.ai_assistant")

router = APIRouter(prefix="/api/assistant", tags=["data-lab"])

DEFAULT_MODEL = os.environ.get("ASSISTANT_MODEL", "claude-sonnet-5")
MAX_STEPS = int(os.environ.get("MAX_ASSISTANT_STEPS", "4"))
# Rows fed back to the model (small, to bound tokens) vs shown to the user.
_MODEL_ROW_CAP = 60
_UI_ROW_CAP = 200

RUN_SQL_TOOL = {
    "name": "run_sql",
    "description": (
        "Run ONE read-only PostgreSQL SELECT (or WITH … SELECT) against the raw.* "
        "schema and get the rows back. Only raw.* is reachable. Always bound the "
        "scan with an indexed predicate (season / pitcher / batter / game_date / "
        "venue_id) and add WHERE data_quality_flag = FALSE unless auditing flagged "
        "rows. No writes, no DDL, no multiple statements."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "A single read-only SELECT/WITH query."}
        },
        "required": ["sql"],
        "additionalProperties": False,
    },
}

# Compact column reference embedded in the system prompt so the model writes
# correct SQL without a schema round-trip.
_SCHEMA_CATALOG = """\
TABLE raw.pitches — one row per pitch (~700K/season, 2015-2026, ~10^7 rows total).
  Keys/context: game_pk, at_bat_number, pitch_number, game_date DATE, season INT,
    venue_id, home_id, away_id, inning, inning_topbot('Top'/'Bot').
  Pitcher: pitcher (player_id), p_throws('L'/'R').
  Batter: batter (player_id),
    stand('L'/'R') — the side he ACTUALLY BATTED FROM this PA; never 'S'. Use this
      for handedness, platoon splits and spray-angle sign.
    bat_hand('L'/'R'/'S') — the ROSTER-DECLARED side, constant per player and 'S'
      for every switch hitter (~10-13% of rows). Do NOT use it as per-PA handedness.
  Count/outs/score: balls(0-3), strikes(0-2), outs(0-2), bat_score, fld_score, home_score, away_score.
  Result: type — single-char pitch result code. Balls in play split THREE ways:
    'X' (in play, out(s), no run), 'D' (in play, no out, no run), 'E' (in play, run(s)).
    A ball-in-play filter is type IN ('X','D','E') — filtering on 'X' alone keeps only
    the outs (65%) and drops the hits. Other codes: 'B' ball, 'C' called strike,
    'S'/'W' swinging strike, 'F' foul, 'T' foul tip, 'H' HBP.
    description(e.g. 'called_strike','swinging_strike','foul','hit_into_play'),
    events (PA outcome, non-NULL only on the last pitch: 'single','double','triple','home_run',
    'strikeout','strikeout_double_play','walk','field_out','hit_by_pitch',...), zone(1-9 in-zone, 11-14 out).
  Pitch physics: release_speed(mph), release_spin_rate(rpm), break_vertical_induced(IVB, in),
    break_horizontal(HB, in), pfx_x, pfx_z, plate_x, plate_z, release_extension, spin_axis.
  Batted ball: launch_speed(EV mph), launch_angle(deg), hit_distance_sc, spray_angle, bb_type
    ('ground_ball'/'fly_ball'/'line_drive'/'popup'), hit_location, fielded_by.
  Baserunning: on_1b/on_2b/on_3b (runner ids or NULL), sb_attempt_2b/3b/home (bool),
    sb_success_2b/3b/home (bool), fielder_2 (catcher id).
  Runs: runs_on_pitch, rbis_on_pitch, earned_runs_on_pitch.
    ⚠ outs_on_pitch is BROKEN in the swept data (zero on 92.6% of batted-ball outs and
    100% of strikeouts — SIM-501). Never use it for outs or innings pitched; derive outs
    from `events` instead (e.g. strikeout/field_out/force_out=1, *double_play=2, triple_play=3).
  data_quality_flag BOOL — physically implausible rows; standard filter is = FALSE.
TABLE raw.players — player_id (PK), full_name, first_name, last_name, bats('L'/'R'/'S'),
  throws('L'/'R'), primary_position. JOIN raw.players pl ON pl.player_id = p.pitcher (or p.batter).
TABLE raw.games — game_pk (PK), game_date, season, venue_id, home_team_id, away_team_id,
  home_score_final, away_score_final, status.
TABLE raw.venues — (venue_id, season), venue_name, city. JOIN ON (venue_id, season).
TABLE raw.teams — (team_id, season), team_name, team_abbrev, league, division.
"""

_SYSTEM_PROMPT = f"""You are a rigorous MLB baseball-data analyst assistant embedded in an internal \
research tool. You answer questions about pitch-level Statcast data by writing ONE read-only SQL query \
at a time with the run_sql tool, then explaining the result plainly.

SCHEMA (PostgreSQL, schema `raw`):
{_SCHEMA_CATALOG}

HARD RULES:
- Only the `raw.*` schema is reachable. `derived.*` and `sim.*` live in a separate DuckDB database and \
CANNOT be joined here — never write a query referencing them.
- Emit read-only SELECT / WITH…SELECT only. No INSERT/UPDATE/DELETE/DDL, no multiple statements.
- ALWAYS bound the scan with an indexed predicate (season and/or pitcher/batter/game_date/venue_id). \
raw.pitches has ~10^7 rows — an unbounded query is unacceptable.
- Add `WHERE data_quality_flag = FALSE` unless the user explicitly wants flagged rows.
- Resolve player NAMES to ids by joining/filtering raw.players (e.g. full_name ILIKE '%cole%'); show names.
- `events` is non-NULL only on the terminal pitch of a plate appearance — use it for PA-level outcomes.
- A swing = description LIKE '%swinging_strike%' OR description IN ('foul','hit_into_play','foul_tip'); \
a whiff = description LIKE '%swinging_strike%'; chase = out-of-zone (zone IN (11,12,13,14)) swings.

ANALYST GUARDRAILS (state these when relevant, do not bury them):
- Attach a minimum-sample floor to every rate stat (pitchers ~hundreds of batters faced; batters ~100-150 \
PA / batted balls) with HAVING, and warn when a result is thin. Never rank on a tiny sample.
- Cross-venue HR/hit/run comparisons off raw.pitches are RAW, not park-adjusted (team-confounded); say so.
- Offer the L/R platoon split when handedness matters; flag pre/post-2023-rules (steals) and juiced-ball \
eras as non-like-for-like.
- Model honesty: batter H/HR/TB projections are bet-grade; pitcher K/BB are over-predicted / not yet \
bet-grade; the CLV backtest shows ~49% beat-close (no demonstrable edge yet). Do not oversell numbers.

STYLE: think briefly, run the query, then interpret the ACTUAL returned numbers. Keep prose tight. If a \
query is rejected or errors, read the message and rewrite. You have at most {MAX_STEPS} query steps."""


class ChatTurn(BaseModel):
    role: str
    content: str


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    history: list[ChatTurn] = Field(default_factory=list)


class AssistantStatus(BaseModel):
    configured: bool
    model: str


def _client(request: Request) -> Any:
    return getattr(request.app.state, "anthropic_client", None)


def _pool(request: Request) -> Any:
    ro = getattr(request.app.state, "pg_pool_ro", None)
    if ro is not None:
        return ro
    return getattr(request.app.state, "pg_pool", None)


@router.get(
    "/status", response_model=AssistantStatus, summary="Whether the AI assistant is configured"
)
async def status_probe(request: Request) -> AssistantStatus:
    return AssistantStatus(configured=_client(request) is not None, model=DEFAULT_MODEL)


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _block_to_dict(block: Any) -> dict[str, Any] | None:
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": block.text}
    if btype == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return None


async def _run_conversation(request: Request, body: AskRequest) -> AsyncIterator[str]:
    client = _client(request)
    pool = _pool(request)
    if pool is None:
        yield _sse({"type": "error", "detail": "database pool unavailable", "step": 0})
        yield _sse({"type": "done"})
        return

    # Seed the conversation with a bounded slice of prior turns.
    messages: list[dict[str, Any]] = []
    for turn in body.history[-6:]:
        role = "assistant" if turn.role == "assistant" else "user"
        messages.append({"role": role, "content": turn.content})
    messages.append({"role": "user", "content": body.question})

    step = 0
    try:
        for _ in range(MAX_STEPS):
            resp = await client.messages.create(
                model=DEFAULT_MODEL,
                max_tokens=2048,
                system=_SYSTEM_PROMPT,
                tools=[RUN_SQL_TOOL],
                messages=messages,
            )

            if getattr(resp, "stop_reason", None) == "refusal":
                yield _sse(
                    {
                        "type": "error",
                        "detail": "The assistant declined to answer this request.",
                        "step": step,
                    }
                )
                break

            assistant_content: list[dict[str, Any]] = []
            tool_uses: list[Any] = []
            for block in resp.content:
                d = _block_to_dict(block)
                if d is None:
                    continue
                assistant_content.append(d)
                if d["type"] == "text" and d["text"].strip():
                    yield _sse({"type": "text", "text": d["text"], "step": step})
                elif d["type"] == "tool_use":
                    tool_uses.append(d)

            messages.append({"role": "assistant", "content": assistant_content})

            if not tool_uses or getattr(resp, "stop_reason", None) != "tool_use":
                break

            tool_results: list[dict[str, Any]] = []
            for tu in tool_uses:
                step += 1
                sql = (tu.get("input") or {}).get("sql", "")
                if tu["name"] != "run_sql" or not sql:
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu["id"],
                            "content": "ERROR: no sql provided",
                            "is_error": True,
                        }
                    )
                    continue
                yield _sse({"type": "query", "sql": sql, "step": step})
                try:
                    result = await run_read_only_sql(
                        pool, sql, max_rows=_UI_ROW_CAP, timeout_ms=DEFAULT_TIMEOUT_MS
                    )
                except SqlValidationError as exc:
                    yield _sse({"type": "error", "detail": str(exc), "step": step})
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu["id"],
                            "content": f"REJECTED: {exc}",
                            "is_error": True,
                        }
                    )
                    continue
                except Exception as exc:  # noqa: BLE001 — DB error → feed back for a rewrite
                    detail = str(exc).strip() or type(exc).__name__
                    yield _sse({"type": "error", "detail": detail, "step": step})
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu["id"],
                            "content": f"ERROR: {detail}",
                            "is_error": True,
                        }
                    )
                    continue

                yield _sse(
                    {
                        "type": "result",
                        "columns": result["columns"],
                        "rows": result["rows"],
                        "row_count": result["row_count"],
                        "truncated": result["truncated"],
                        "elapsed_ms": result["elapsed_ms"],
                        "step": step,
                    }
                )
                model_view = {
                    "columns": result["columns"],
                    "rows": result["rows"][:_MODEL_ROW_CAP],
                    "row_count": result["row_count"],
                    "truncated": result["truncated"] or result["row_count"] > _MODEL_ROW_CAP,
                }
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": json.dumps(model_view, default=str),
                    }
                )

            messages.append({"role": "user", "content": tool_results})
        else:
            yield _sse(
                {
                    "type": "text",
                    "text": f"(Reached the {MAX_STEPS}-query limit for this question.)",
                    "step": step,
                }
            )
    except Exception as exc:  # noqa: BLE001 — never leak a 500 mid-stream
        log.warning("assistant error (%s): %s", type(exc).__name__, exc)
        yield _sse(
            {"type": "error", "detail": f"assistant error: {type(exc).__name__}", "step": step}
        )

    yield _sse({"type": "done"})


@router.post(
    "/ask", summary="Ask the AI assistant (SSE stream)", dependencies=[Depends(require_auth)]
)
async def ask(request: Request, body: AskRequest) -> Any:
    if _client(request) is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "assistant not configured — set ANTHROPIC_API_KEY to enable it"},
        )
    return StreamingResponse(
        _run_conversation(request, body),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


__all__ = ["router", "AskRequest", "AssistantStatus"]
