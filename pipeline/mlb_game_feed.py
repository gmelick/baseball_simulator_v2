"""The league's per-game live feed → the slate card's detail (SIM-519 Part I).

The Daily Diamond card, expanded, shows the real game: the linescore, both
teams' box scores, the posted lineups before first pitch, and for a live game
the count, the runners, the fielders, the pitcher's pitch count and the last
play. One league endpoint carries all of it
(``/api/v1.1/game/{pk}/feed/live``). :func:`parse_game_feed` turns that
response into a plain, JSON-ready dict; the API validates it against
``GameFeedCardModel``.

The parser is pure and is tested on trimmed recorded feeds in
``tests/fixtures/mlb_game_feed/``. A field the feed does not carry reads
``None`` (or an empty list), and the card hides that section.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pipeline.mlb_schedule import FINAL, LIVE, card_state_of

#: The defense keys of the feed's linescore → the design's position labels.
_FIELD_POSITIONS = (
    ("pitcher", "P"),
    ("catcher", "C"),
    ("first", "1B"),
    ("second", "2B"),
    ("third", "3B"),
    ("shortstop", "SS"),
    ("left", "LF"),
    ("center", "CF"),
    ("right", "RF"),
)


def feed_state(status: Mapping[str, Any]) -> str:
    """The card state of a feed's ``gameData.status`` (the schedule's mapper)."""
    return card_state_of(
        str(status.get("abstractGameState") or ""),
        str(status.get("codedGameState") or ""),
        str(status.get("detailedState") or ""),
    )


def _int(v: Any) -> int | None:
    try:
        return None if v is None or v == "" else int(v)
    except (TypeError, ValueError):
        return None


def _person(v: Any) -> dict[str, Any] | None:
    if not isinstance(v, Mapping) or _int(v.get("id")) is None:
        return None
    return {"id": int(v["id"]), "name": str(v.get("fullName") or "")}


def _position(p: Mapping[str, Any]) -> str | None:
    """The starting position (``allPositions[0]``, the SIM-559 rule), else the last."""
    allp = p.get("allPositions") or []
    first = allp[0] if allp else p.get("position") or {}
    return first.get("abbreviation") if isinstance(first, Mapping) else None


def _linescore(ls: Mapping[str, Any], state: str) -> dict[str, Any] | None:
    innings = ls.get("innings") or []
    if not innings:
        return None
    rows = []
    for inn in innings:
        away, home = inn.get("away") or {}, inn.get("home") or {}
        rows.append(
            {
                "num": _int(inn.get("num")),
                "away": _int(away.get("runs")),
                "home": _int(home.get("runs")),
            }
        )
    teams = ls.get("teams") or {}

    def totals(side: str) -> dict[str, int | None]:
        t = teams.get(side) or {}
        return {
            "runs": _int(t.get("runs")),
            "hits": _int(t.get("hits")),
            "errors": _int(t.get("errors")),
        }

    return {
        "innings": rows,
        "away": totals("away"),
        "home": totals("home"),
        # On a final game a missing home half is the "X" of a home win.
        "home_did_not_bat_last": state == FINAL and rows[-1]["home"] is None,
        "current_inning": _int(ls.get("currentInning")) if state == LIVE else None,
        "inning_half": ls.get("inningHalf") if state == LIVE else None,
    }


def _batter_row(p: Mapping[str, Any]) -> dict[str, Any]:
    s = (p.get("stats") or {}).get("batting") or {}
    season = (p.get("seasonStats") or {}).get("batting") or {}
    order = _int(p.get("battingOrder"))
    person = p.get("person") or {}
    return {
        "id": _int(person.get("id")),
        "name": str(person.get("fullName") or ""),
        "pos": _position(p),
        "batting_order": order,
        # The feed numbers a substitute 101, 102 ... after the starter's 100.
        "is_sub": order is not None and order % 100 != 0,
        "ab": _int(s.get("atBats")),
        "r": _int(s.get("runs")),
        "h": _int(s.get("hits")),
        "rbi": _int(s.get("rbi")),
        "bb": _int(s.get("baseOnBalls")),
        "k": _int(s.get("strikeOuts")),
        "hr": _int(s.get("homeRuns")),
        "avg": season.get("avg"),
    }


def _pitcher_row(p: Mapping[str, Any]) -> dict[str, Any]:
    s = (p.get("stats") or {}).get("pitching") or {}
    season = (p.get("seasonStats") or {}).get("pitching") or {}
    person = p.get("person") or {}
    return {
        "id": _int(person.get("id")),
        "name": str(person.get("fullName") or ""),
        "outs": _int(s.get("outs")),
        "ip": s.get("inningsPitched"),
        "h": _int(s.get("hits")),
        "r": _int(s.get("runs")),
        "er": _int(s.get("earnedRuns")),
        "bb": _int(s.get("baseOnBalls")),
        "k": _int(s.get("strikeOuts")),
        "np": _int(s.get("numberOfPitches")),
        "era": season.get("era"),
    }


def _box_side(t: Mapping[str, Any]) -> dict[str, Any]:
    players = t.get("players") or {}

    def get(pid: Any) -> Mapping[str, Any] | None:
        return players.get(f"ID{pid}")

    batters = [_batter_row(p) for p in (get(x) for x in t.get("batters") or []) if p is not None]
    # Only the players who bat (a pitcher in a DH game is listed with no order).
    batters = [b for b in batters if b["batting_order"] is not None]
    batters.sort(key=lambda b: b["batting_order"])
    pitchers = [_pitcher_row(p) for p in (get(x) for x in t.get("pitchers") or []) if p is not None]
    return {"batters": batters, "pitchers": pitchers}


def _lineup_side(t: Mapping[str, Any]) -> list[dict[str, Any]]:
    players = t.get("players") or {}
    out = []
    for i, pid in enumerate(t.get("battingOrder") or []):
        p = players.get(f"ID{pid}")
        if p is None:
            continue
        person = p.get("person") or {}
        out.append(
            {
                "order": i + 1,
                "id": _int(person.get("id")),
                "name": str(person.get("fullName") or ""),
                "pos": _position(p),
            }
        )
    return out


def _last_play(plays: Mapping[str, Any]) -> str | None:
    """The description of the newest completed play."""
    for play in reversed(plays.get("allPlays") or []):
        if (play.get("about") or {}).get("isComplete"):
            desc = (play.get("result") or {}).get("description")
            if desc:
                return str(desc)
    return None


def _live(
    ls: Mapping[str, Any], box: Mapping[str, Any], plays: Mapping[str, Any]
) -> dict[str, Any]:
    offense, defense = ls.get("offense") or {}, ls.get("defense") or {}
    # The batting side: the offense team's id against the two box teams.
    off_id = _int((offense.get("team") or {}).get("id"))
    side = None
    for s in ("away", "home"):
        if off_id is not None and off_id == _int(((box.get(s) or {}).get("team") or {}).get("id")):
            side = s
    if side is None:
        side = "away" if ls.get("isTopInning", ls.get("inningHalf") == "Top") else "home"
    current = plays.get("currentPlay") or {}
    count = current.get("count") or {}
    pitcher = _person(defense.get("pitcher"))
    np = None
    if pitcher is not None:
        field_side = "home" if side == "away" else "away"
        p = ((box.get(field_side) or {}).get("players") or {}).get(f"ID{pitcher['id']}") or {}
        np = _int(((p.get("stats") or {}).get("pitching") or {}).get("numberOfPitches"))
    return {
        "balls": _int(count.get("balls", ls.get("balls"))),
        "strikes": _int(count.get("strikes", ls.get("strikes"))),
        "outs": _int(count.get("outs", ls.get("outs"))),
        "offense": side,
        "runners": {base: _person(offense.get(base)) for base in ("first", "second", "third")},
        "batter": _person(offense.get("batter")),
        "pitcher": None if pitcher is None else {**pitcher, "np": np},
        "fielders": {
            label: (_person(defense.get(key)) or {}).get("name") for key, label in _FIELD_POSITIONS
        },
        "last_play": _last_play(plays),
    }


def parse_game_feed(payload: Mapping[str, Any]) -> dict[str, Any]:
    """One league feed → the card detail, a JSON-ready dict."""
    gd = payload.get("gameData") or {}
    ld = payload.get("liveData") or {}
    status = gd.get("status") or {}
    state = feed_state(status)
    ls = ld.get("linescore") or {}
    box_teams = (ld.get("boxscore") or {}).get("teams") or {}
    plays = ld.get("plays") or {}
    probables = gd.get("probablePitchers") or {}

    lineups = {s: _lineup_side(box_teams.get(s) or {}) for s in ("away", "home")}
    played = state in (LIVE, FINAL)
    return {
        "game_pk": _int(payload.get("gamePk")),
        "status": state,
        "detailed_state": status.get("detailedState"),
        "linescore": _linescore(ls, state) if played else None,
        "lineups": {
            "away": lineups["away"],
            "home": lineups["home"],
            "away_probable_pitcher": _person(probables.get("away")),
            "home_probable_pitcher": _person(probables.get("home")),
        },
        "box": {s: _box_side(box_teams.get(s) or {}) for s in ("away", "home")} if played else None,
        "live": _live(ls, box_teams, plays) if state == LIVE else None,
    }
