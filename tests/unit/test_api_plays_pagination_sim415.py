"""
test_api_plays_pagination_sim415.py
===================================
Unit tests for SIM-415 pagination on GET /api/games/{game_pk}/plays.

The endpoint reads ``app.state.sim_duckdb`` + ``sim_store.load_play_stream``;
both are stubbed (a sentinel connection + a monkeypatched loader returning
canned play rows) so no live DuckDB is needed.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.games as games_mod
from api.routes.games import router as games_router


def _canned_rows(n: int) -> list[dict]:
    """n play rows across 3-pitch plate appearances (every 3rd pitch ends a PA)."""
    rows = []
    for seq in range(n):
        pa = seq // 3
        pitch = (seq % 3) + 1
        is_end = pitch == 3
        rows.append(
            {
                "sequence": seq,
                "at_bat": pa,
                "pitch": pitch,
                "pitch_outcome": "ball" if not is_end else "in_play",
                "is_contact": is_end,
                "is_pa_end": is_end,
                "event": "single" if is_end else None,
                "runs_scored": 0,
                "outs_recorded": 0,
                "exit_velo": 95.0 if is_end else None,
                "launch_angle": 12.0 if is_end else None,
                "spray_angle": None,
                "runs": 0.0,
                "canonical_event": "1B" if is_end else None,
            }
        )
    return rows


def _build_app(monkeypatch, *, n_rows: int = 30) -> FastAPI:
    rows = _canned_rows(n_rows)

    def _fake_load(con, *, game_pk):  # matches sim_store.load_play_stream signature
        return rows

    monkeypatch.setattr(
        games_mod,
        "sim_store",
        type("_FakeStore", (), {"load_play_stream": staticmethod(_fake_load)})(),
    )
    app = FastAPI()
    app.include_router(games_router)
    app.state.pg_pool = object()
    app.state.sim_cache = None
    app.state.sim_duckdb = object()  # non-None sentinel → endpoint proceeds
    return app


def test_no_limit_returns_full_collection_unchanged(monkeypatch):
    app = _build_app(monkeypatch, n_rows=30)
    client = TestClient(app)
    body = client.get("/api/games/745001/plays").json()
    assert len(body["entries"]) == 30
    assert body["n_pitches"] == 30
    assert body["n_plate_appearances"] == 10
    # Pagination fields stay null on the full response (backward-compatible).
    assert body["total_entries"] is None
    assert body["page_offset"] is None
    assert body["page_limit"] is None


def test_limit_offset_slices_entries(monkeypatch):
    app = _build_app(monkeypatch, n_rows=30)
    client = TestClient(app)
    body = client.get("/api/games/745001/plays?limit=10&offset=5").json()
    assert len(body["entries"]) == 10
    assert body["entries"][0]["sequence"] == 5
    assert body["entries"][-1]["sequence"] == 14
    # Pagination metadata describes the slice.
    assert body["total_entries"] == 30
    assert body["page_offset"] == 5
    assert body["page_limit"] == 10
    assert body["returned_entries"] == 10
    # Full-game totals are PRESERVED (not trimmed to the page).
    assert body["n_pitches"] == 30
    assert body["n_plate_appearances"] == 10


def test_offset_past_end_returns_empty_page_with_totals(monkeypatch):
    app = _build_app(monkeypatch, n_rows=30)
    client = TestClient(app)
    body = client.get("/api/games/745001/plays?limit=10&offset=100").json()
    assert body["entries"] == []
    assert body["returned_entries"] == 0
    assert body["total_entries"] == 30
    assert body["n_pitches"] == 30  # totals still correct


def test_limit_validation_rejects_zero(monkeypatch):
    app = _build_app(monkeypatch, n_rows=30)
    client = TestClient(app)
    assert client.get("/api/games/745001/plays?limit=0").status_code == 422
    assert client.get("/api/games/745001/plays?offset=-1").status_code == 422
