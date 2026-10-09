"""Record trimmed league game feeds for the SIM-519 Part I tests.

Run by hand from the repo root: ``python tests/fixtures/mlb_game_feed/record_fixtures.py``.
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


def main(pairs: list[tuple[str, int]]) -> None:
    for name, pk in pairs:
        with urllib.request.urlopen(URL.format(pk=pk), timeout=60) as resp:
            feed = json.loads(resp.read().decode("utf-8"))
        (OUT / f"{name}.json").write_text(
            json.dumps(trim(feed), indent=1, sort_keys=True), encoding="utf-8"
        )
        print(name, pk)


if __name__ == "__main__":
    args = sys.argv[1:]
    main([(args[i], int(args[i + 1])) for i in range(0, len(args), 2)])
