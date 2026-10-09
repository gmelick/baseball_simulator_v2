"""SIM-519 Part I — the book's pregame lines on a slate card, and how they settled."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.betting as betting_mod
from api.auth import require_auth
from api.routes.betting import router as betting_router

GAME = 745001


def _row(
    book: str,
    market: str,
    line_type: str = "closing",
    fetched: str = "2024-08-15T22:00:00+00:00",
    **cols,
):
    base = {
        "market_type": market,
        "book": book,
        "line_type": line_type,
        "fetched_at": fetched,
        "home_ml": None,
        "away_ml": None,
        "draw_ml": None,
        "home_spread": None,
        "home_spread_ml": None,
        "away_spread": None,
        "away_spread_ml": None,
        "total_line": None,
        "over_ml": None,
        "under_ml": None,
    }
    base.update(cols)
    return base


def _ml(book: str, home: float, away: float, **kw):
    return _row(book, "moneyline", home_ml=home, away_ml=away, **kw)


def _rl(book: str, home_line: float, home_price: float, away_price: float, **kw):
    return _row(
        book,
        "runline",
        home_spread=home_line,
        home_spread_ml=home_price,
        away_spread=-home_line,
        away_spread_ml=away_price,
        **kw,
    )


def _tot(book: str, line: float, over: float, under: float, **kw):
    return _row(book, "total", total_line=line, over_ml=over, under_ml=under, **kw)


class _Pool:
    """Answers the three reads: the game status, the stored odds, the final score."""

    def __init__(self, odds_rows, *, status="Final", away=None, home=None):
        self.odds_rows, self.status, self.away, self.home = odds_rows, status, away, home
        self.calls: list[str] = []

    async def fetch(self, sql, *args):
        self.calls.append(sql)
        if "raw.game_odds" in sql:
            allowed = set(args[3])
            return [r for r in self.odds_rows if r["line_type"] in allowed]
        if "home_score_final" in sql:
            return [
                {
                    "status": self.status,
                    "home_score_final": self.home,
                    "away_score_final": self.away,
                }
            ]
        return [{"status": self.status}]


def _client(pool) -> TestClient:
    app = FastAPI()
    app.include_router(betting_router)
    app.dependency_overrides[require_auth] = lambda: None
    app.state.pg_pool = pool
    return TestClient(app)


FULL = [
    # FanDuel (bp:10) is third on the graded preference; DraftKings (bp:12) first.
    _ml("bp:10", -150, 130),
    _ml("bp:12", -145, 125),
    _rl("bp:12", -1.5, 135, -160),
    _tot("bp:12", 8.5, -110, -110),
]


def test_the_graded_book_wins() -> None:
    body = _client(_Pool(FULL, status="Preview")).get(f"/api/betting/games/{GAME}/card-odds").json()
    assert body["moneyline"] == {
        "book": "DraftKings",
        "line_type": "closing",
        "away": 125.0,
        "home": -145.0,
    }
    assert body["runline"]["home"] == {"line": -1.5, "price": 135.0}
    assert body["runline"]["away"] == {"line": 1.5, "price": -160.0}
    assert body["total"]["line"] == 8.5
    assert body["settled"] is None


def test_closing_beats_current_for_one_book() -> None:
    rows = [
        _ml("bp:12", -120, 100, line_type="current", fetched="2024-08-15T23:00:00+00:00"),
        _ml("bp:12", -145, 125, line_type="closing", fetched="2024-08-15T22:00:00+00:00"),
    ]
    body = _client(_Pool(rows, status="Preview")).get(f"/api/betting/games/{GAME}/card-odds").json()
    assert body["moneyline"]["home"] == -145.0
    assert body["moneyline"]["line_type"] == "closing"


def test_a_preview_game_reads_its_current_line() -> None:
    rows = [_ml("bp:12", -120, 100, line_type="current")]
    body = _client(_Pool(rows, status="Preview")).get(f"/api/betting/games/{GAME}/card-odds").json()
    assert body["moneyline"]["line_type"] == "current"
    assert body["runline"] is None and body["total"] is None


def test_a_final_game_ignores_current_rows() -> None:
    rows = [_ml("bp:12", -120, 100, line_type="current")]
    resp = _client(_Pool(rows, status="Final", away=3, home=5)).get(
        f"/api/betting/games/{GAME}/card-odds"
    )
    assert resp.status_code == 404


def test_no_rows_is_404_never_the_mock() -> None:
    resp = _client(_Pool([], status="Preview")).get(f"/api/betting/games/{GAME}/card-odds")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    ("away", "home", "ml", "rl", "total"),
    [
        (3, 5, "home", "home", "under"),  # home wins by 2: covers -1.5; 8 < 8.5
        (4, 5, "home", "away", "over"),  # home by 1: away +1.5 covers; 9 > 8.5
        (6, 2, "away", "away", "under"),
        (7, 6, "away", "away", "over"),
    ],
)
def test_settlement(away: int, home: int, ml: str, rl: str, total: str) -> None:
    body = (
        _client(_Pool(FULL, status="Final", away=away, home=home))
        .get(f"/api/betting/games/{GAME}/card-odds")
        .json()
    )
    assert body["settled"] == {
        "away_score": away,
        "home_score": home,
        "moneyline": ml,
        "runline": rl,
        "total": total,
    }


def test_total_push() -> None:
    odds = betting_mod.CardOddsResponse(
        game_pk=1,
        total=betting_mod.CardTotal(book="x", line_type="closing", line=8.0, over=-110, under=-110),
    )
    s = betting_mod._card_settlement(odds, 3, 5)  # noqa: SLF001
    assert s.total == "push"
    assert s.moneyline is None and s.runline is None


def test_unbettable_books_are_not_shown() -> None:
    rows = [_ml("consensus", -150, 130)]
    resp = _client(_Pool(rows, status="Preview")).get(f"/api/betting/games/{GAME}/card-odds")
    assert resp.status_code == 404
