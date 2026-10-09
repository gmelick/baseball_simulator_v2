"""SIM-519 Part B — the published-lineup writer.

The schedule entry is a recorded 2024 game flipped to Preview; the feed is the
same game's trimmed recorded feed (its box carries each starter's batting
order and starting position, as a posted pre-game box does). The pool is a
fake that records every statement.
"""

from __future__ import annotations

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from pipeline.live.lineup_writer import (
    PublishedLineupWriter,
    lineup_signature,
    with_probable_pitchers,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures"
PK = 746437


def _entry() -> dict:
    sched = json.loads(
        (FIX / "mlb_schedule" / "normal_2024-08-15.json").read_text(encoding="utf-8")
    )
    entry = copy.deepcopy(next(g for g in sched["dates"][0]["games"] if g["gamePk"] == PK))
    entry["status"].update(
        abstractGameState="Preview", codedGameState="P", detailedState="Pre-Game"
    )
    return entry


def _feed() -> dict:
    return json.loads(
        (FIX / "mlb_game_feed" / "final_home_win_2024-08-15.json").read_text(encoding="utf-8")
    )


class _Conn:
    def __init__(self, pool: _Pool) -> None:
        self.pool = pool

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.pool.log.append(("fetchval", sql, args))
        if "source = 'box'" in sql:
            return 1 if self.pool.has_box else None
        if "SELECT throws" in sql:
            return "R"
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        self.pool.log.append(("fetch", sql, args))
        if "FROM raw.players WHERE player_id = ANY" in sql:
            return [{"player_id": p} for p in args[0] if p not in self.pool.unknown_players]
        if "LIMIT 10" in sql:
            return self.pool.recent
        if "batting_order IS NOT NULL" in sql:
            return self.pool.batters
        return []

    async def execute(self, sql: str, *args: Any) -> None:
        self.pool.log.append(("execute", sql, args))

    async def executemany(self, sql: str, rows: list) -> None:
        self.pool.log.append(("executemany", sql, rows))
        self.pool.inserted = rows

    @asynccontextmanager
    async def transaction(self):
        self.pool.log.append(("begin", "", ()))
        yield
        self.pool.log.append(("commit", "", ()))


class _Pool:
    def __init__(self, *, has_box: bool = False, unknown_players: set[int] | None = None) -> None:
        self.has_box = has_box
        self.unknown_players = unknown_players or set()
        self.log: list[tuple[str, str, Any]] = []
        self.inserted: list = []
        self.recent: list[dict] = []
        self.batters: list[dict] = []

    @asynccontextmanager
    async def acquire(self):
        yield _Conn(self)


class _Http:
    def __init__(self, people: dict[int, dict] | None = None) -> None:
        self.urls: list[str] = []
        self.people = people or {}

    async def __call__(self, url: str) -> Any:
        self.urls.append(url)
        if "/feed/live" in url:
            return _feed()
        pid = int(url.rsplit("/", 1)[1])
        return {"people": [self.people[pid]]} if pid in self.people else {"people": []}


def _run(coro):
    return asyncio.run(coro)


def test_writes_the_published_lineup_with_the_probables_as_p() -> None:
    pool, http = _Pool(), _Http()
    writer = PublishedLineupWriter(pool, http, projected=False)
    assert _run(writer.on_preview(_entry())) == "published"

    rows = pool.inserted
    assert all(r[-1] == "published" for r in rows)
    by_team: dict[int, list] = {}
    for r in rows:
        by_team.setdefault(r[2], []).append(r)
    assert len(by_team) == 2
    entry = _entry()
    for side in ("away", "home"):
        team_id = entry["teams"][side]["team"]["id"]
        team_rows = by_team[team_id]
        assert sorted(r[4] for r in team_rows if r[4] is not None) == list(range(1, 10))
        p_rows = [r for r in team_rows if r[5] == "P"]
        assert len(p_rows) == 1
        assert p_rows[0][3] == entry["teams"][side]["probablePitcher"]["id"]
        assert p_rows[0][4] is None
    # One transaction: delete the old published/projected rows, then insert.
    kinds = [k for k, *_ in pool.log]
    assert (
        kinds.index("begin")
        < kinds.index("execute")
        < kinds.index("executemany")
        < kinds.index("commit")
    )
    delete_sql = next(sql for k, sql, _ in pool.log if k == "execute")
    assert "source IN ('published', 'projected')" in delete_sql
    assert sum("/feed/live" in u for u in http.urls) == 1


def test_the_same_entry_twice_writes_once() -> None:
    pool, http = _Pool(), _Http()
    writer = PublishedLineupWriter(pool, http, projected=False)
    assert _run(writer.on_preview(_entry())) == "published"
    assert _run(writer.on_preview(_entry())) is None
    assert sum("/feed/live" in u for u in http.urls) == 1


def test_a_scratch_rewrites() -> None:
    pool, http = _Pool(), _Http()
    writer = PublishedLineupWriter(pool, http, projected=False)
    _run(writer.on_preview(_entry()))
    changed = _entry()
    changed["lineups"]["homePlayers"][0]["id"] = 999999  # the leadoff man scratched
    assert _run(writer.on_preview(changed)) == "published"
    assert sum("/feed/live" in u for u in http.urls) == 2


def test_a_game_with_box_rows_is_left_alone() -> None:
    pool, http = _Pool(has_box=True), _Http()
    assert _run(PublishedLineupWriter(pool, http, projected=False).on_preview(_entry())) is None
    assert pool.inserted == []
    assert http.urls == []


def test_a_live_or_final_entry_is_ignored() -> None:
    entry = _entry()
    entry["status"].update(
        abstractGameState="Live", codedGameState="I", detailedState="In Progress"
    )
    pool, http = _Pool(), _Http()
    assert _run(PublishedLineupWriter(pool, http, projected=False).on_preview(entry)) is None
    assert pool.log == []


def test_no_lineup_and_projection_off_writes_nothing() -> None:
    entry = _entry()
    entry.pop("lineups")
    pool, http = _Pool(), _Http()
    assert _run(PublishedLineupWriter(pool, http, projected=False).on_preview(entry)) is None
    assert pool.inserted == []


def test_an_unknown_player_is_added_first() -> None:
    feed_starter = next(
        p["person"]["id"]
        for p in _feed()["liveData"]["boxscore"]["teams"]["home"]["players"].values()
        if p.get("battingOrder") == "100"
    )
    person = {
        "id": feed_starter,
        "fullName": "New Callup",
        "firstName": "New",
        "lastName": "Callup",
        "batSide": {"code": "L"},
        "pitchHand": {"code": "R"},
        "primaryPosition": {"abbreviation": "CF"},
        "height": "6' 1\"",
        "weight": 200,
    }
    pool, http = _Pool(unknown_players={feed_starter}), _Http(people={feed_starter: person})
    assert (
        _run(PublishedLineupWriter(pool, http, projected=False).on_preview(_entry())) == "published"
    )
    inserts = [
        args for k, sql, args in pool.log if k == "execute" and "INSERT INTO raw.players" in sql
    ]
    assert len(inserts) == 1
    assert inserts[0][0] == feed_starter and inserts[0][5] == "L"


def test_an_unknown_player_without_a_hand_stops_the_write() -> None:
    pool, http = _Pool(unknown_players={1}), _Http()
    writer = PublishedLineupWriter(pool, http, projected=False)
    # Player 1 is not in the lineup, so the write goes through.
    assert _run(writer.on_preview(_entry())) == "published"
    starter = pool.inserted[0][3]
    pool2, http2 = _Pool(unknown_players={starter}), _Http()
    assert _run(PublishedLineupWriter(pool2, http2, projected=False).on_preview(_entry())) is None
    assert pool2.inserted == []


def test_projected_lineup_when_enabled() -> None:
    entry = _entry()
    entry.pop("lineups")
    pool, http = _Pool(), _Http()
    pool.recent = [
        {"game_pk": 1, "game_date": None, "opp_throws": "L"},
        {"game_pk": 2, "game_date": None, "opp_throws": "R"},
    ]
    pool.batters = [
        {"player_id": 100 + i, "batting_order": i + 1, "position_code": "CF"} for i in range(9)
    ]
    assert _run(PublishedLineupWriter(pool, http, projected=True).on_preview(entry)) == "projected"
    assert all(r[-1] == "projected" for r in pool.inserted)
    # Same opposing hand (R, from the fake) picks game 2.
    picked = [
        args for k, sql, args in pool.log if k == "fetch" and "batting_order IS NOT NULL" in sql
    ]
    assert all(a[0] == 2 for a in picked)
    assert http.urls == []  # no feed read for a projection


def test_signature_changes_with_a_probable() -> None:
    a = _entry()
    b = _entry()
    b["teams"]["home"]["probablePitcher"]["id"] = 1
    assert lineup_signature(a) != lineup_signature(b)


@pytest.mark.parametrize("two_way", [False, True])
def test_with_probable_pitchers(two_way: bool) -> None:
    rows = [(1, 2024, 10, 7, 1, "DH" if two_way else "CF", True, 1)]
    out = with_probable_pitchers(rows, 1, 2024, {10: 7 if two_way else 8})
    assert len(out) == (1 if two_way else 2)


# ---------------------------------------------------------------------------
# The game row: the official date and the schedule fields (Alembic 0029)
# ---------------------------------------------------------------------------


def _pipeline_with_db(columns_exist: bool):
    from unittest.mock import AsyncMock, MagicMock

    from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

    p = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
    p._db = MagicMock()
    p._db.execute = AsyncMock()
    p._db.fetchval = AsyncMock(return_value=1 if columns_exist else None)
    return p


def test_the_game_row_takes_the_official_date_and_the_schedule_fields() -> None:
    p = _pipeline_with_db(True)
    entry = _entry()
    entry["gameDate"] = "2024-08-16T02:10:00Z"  # a night game: next UTC day
    _run(p._upsert_game_record(entry))
    insert, update = p._db.execute.await_args_list
    assert str(insert.args[3]) == "2024-08-15"  # game_date = the official date
    assert "UPDATE raw.games SET" in update.args[0]
    assert update.args[1] == PK
    assert update.args[7] == entry["teams"]["home"]["probablePitcher"]["id"]


def test_no_0029_columns_means_no_update() -> None:
    p = _pipeline_with_db(False)
    _run(p._upsert_game_record(_entry()))
    assert p._db.execute.await_count == 1
