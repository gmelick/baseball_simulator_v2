"""Record trimmed league game feeds for the SIM-519 Part I tests.

Run by hand from the repo root: ``python tests/fixtures/mlb_game_feed/record_fixtures.py NAME PK ...``
(add ``--plays`` to keep every play, read with ``hydrate=alignment``, for the what-if tests).
Each feed is about 700 KB; the trim keeps only the subtrees
``pipeline.mlb_game_feed.parse_game_feed`` reads, with their real field names.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent
URL = "https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live"

BAT_KEYS = ("atBats", "runs", "hits", "rbi", "baseOnBalls", "strikeOuts", "homeRuns")
PIT_KEYS = (
    "outs",
    "hits",
    "runs",
    "earnedRuns",
    "baseOnBalls",
    "strikeOuts",
    "numberOfPitches",
    "inningsPitched",
)


def _player(p: dict) -> dict:
    stats = p.get("stats") or {}
    season = p.get("seasonStats") or {}
    return {
        "person": {k: p["person"].get(k) for k in ("id", "fullName")},
        "position": p.get("position"),
        "allPositions": p.get("allPositions"),
        "battingOrder": p.get("battingOrder"),
        "stats": {
            "batting": {
                k: stats.get("batting", {}).get(k)
                for k in BAT_KEYS
                if k in stats.get("batting", {})
            },
            "pitching": {
                k: stats.get("pitching", {}).get(k)
                for k in PIT_KEYS
                if k in stats.get("pitching", {})
            },
        },
        "seasonStats": {
            "batting": {"avg": season.get("batting", {}).get("avg")},
            "pitching": {"era": season.get("pitching", {}).get("era")},
        },
    }


def _play(play: dict) -> dict:
    return {
        "result": {"description": (play.get("result") or {}).get("description")},
        "about": play.get("about"),
        "count": play.get("count"),
        "matchup": {k: (play.get("matchup") or {}).get(k) for k in ("batter", "pitcher")},
    }


def trim(feed: dict) -> dict:
    gd, ld = feed["gameData"], feed["liveData"]
    box = {}
    for side in ("away", "home"):
        t = ld["boxscore"]["teams"][side]
        keep = (
            set(t.get("batters", [])) | set(t.get("pitchers", [])) | set(t.get("battingOrder", []))
        )
        box[side] = {
            "team": {"id": (t.get("team") or {}).get("id")},
            "batters": t.get("batters", []),
            "pitchers": t.get("pitchers", []),
            "battingOrder": t.get("battingOrder", []),
            "players": {
                k: _player(v) for k, v in t["players"].items() if v["person"]["id"] in keep
            },
        }
    plays = ld.get("plays") or {}
    return {
        "gamePk": feed.get("gamePk"),
        "gameData": {
            "status": gd["status"],
            "teams": {
                s: {k: gd["teams"][s].get(k) for k in ("id", "abbreviation", "teamName")}
                for s in ("away", "home")
            },
            "probablePitchers": gd.get("probablePitchers"),
        },
        "liveData": {
            "linescore": ld["linescore"],
            "boxscore": {"teams": box},
            "plays": {
                "currentPlay": _play(plays["currentPlay"]) if plays.get("currentPlay") else None,
                "allPlays": [_play(p) for p in (plays.get("allPlays") or [])[-3:]],
            },
        },
    }


def _ids(block: dict | None) -> dict:
    return {
        k: ({"id": v.get("id")} if isinstance(v, dict) else v) for k, v in (block or {}).items()
    }


def _event(e: dict) -> dict:
    det = e.get("details") or {}
    return {
        "type": e.get("type"),
        "isPitch": e.get("isPitch"),
        "isSubstitution": e.get("isSubstitution"),
        "details": {
            k: det.get(k) for k in ("eventType", "description", "call", "type", "event") if k in det
        },
        "pitchData": {"startSpeed": (e.get("pitchData") or {}).get("startSpeed")},
        "count": e.get("count"),
        "offense": _ids(e.get("offense")),
        "defense": _ids(e.get("defense")),
        "player": {"id": (e.get("player") or {}).get("id")} if e.get("player") else None,
        "position": {"abbreviation": (e.get("position") or {}).get("abbreviation")}
        if e.get("position")
        else None,
        "battingOrder": e.get("battingOrder"),
        "replacedPlayer": {"id": (e.get("replacedPlayer") or {}).get("id")}
        if e.get("replacedPlayer")
        else None,
    }


def _full_play(p: dict) -> dict:
    res = p.get("result") or {}
    mu = p.get("matchup") or {}
    return {
        "atBatIndex": p.get("atBatIndex"),
        "about": p.get("about"),
        "count": p.get("count"),
        "result": {
            k: res.get(k)
            for k in ("event", "eventType", "description", "rbi", "awayScore", "homeScore")
        },
        "matchup": {
            k: (
                {"id": mu[k].get("id"), "fullName": mu[k].get("fullName")}
                if isinstance(mu.get(k), dict)
                else None
            )
            for k in ("batter", "pitcher", "postOnFirst", "postOnSecond", "postOnThird")
        },
        "playEvents": [_event(e) for e in p.get("playEvents") or []],
    }


def trim_with_plays(feed: dict) -> dict:
    """``trim`` plus every play (for the what-if state), the players' hands and
    positions, and the box's bench and bullpen lists."""
    out = trim(feed)
    gd, ld = feed["gameData"], feed["liveData"]
    out["gameData"]["players"] = {
        k: {
            "id": v.get("id"),
            "fullName": v.get("fullName"),
            "batSide": {"code": (v.get("batSide") or {}).get("code")},
            "pitchHand": {"code": (v.get("pitchHand") or {}).get("code")},
            "primaryPosition": {
                "abbreviation": (v.get("primaryPosition") or {}).get("abbreviation")
            },
        }
        for k, v in gd.get("players", {}).items()
    }
    out["liveData"]["plays"]["allPlays"] = [
        _full_play(p) for p in ld["plays"].get("allPlays") or []
    ]
    for side in ("away", "home"):
        t = ld["boxscore"]["teams"][side]
        out["liveData"]["boxscore"]["teams"][side]["bench"] = t.get("bench", [])
        out["liveData"]["boxscore"]["teams"][side]["bullpen"] = t.get("bullpen", [])
    return out


def main(pairs: list[tuple[str, int]], *, plays: bool = False) -> None:
    for name, pk in pairs:
        url = URL.format(pk=pk) + ("?hydrate=alignment" if plays else "")
        with urllib.request.urlopen(url, timeout=60) as resp:
            feed = json.loads(resp.read().decode("utf-8"))
        data = trim_with_plays(feed) if plays else trim(feed)
        (OUT / f"{name}.json").write_text(
            json.dumps(data, indent=None if plays else 1, sort_keys=True), encoding="utf-8"
        )
        print(name, pk)


if __name__ == "__main__":
    args = sys.argv[1:]
    with_plays = "--plays" in args
    args = [a for a in args if a != "--plays"]
    main([(args[i], int(args[i + 1])) for i in range(0, len(args), 2)], plays=with_plays)
