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


# ---------------------------------------------------------------------------
# The real game's plays and the state at each plate appearance (the "what if")
# ---------------------------------------------------------------------------
#
# The feed must be read with ``hydrate=alignment``: every pitch then carries
# the fielding side's nine (``defense``) and the batter and runners
# (``offense``). Substitutions are ``action`` events with the player, his
# position and (for a lineup change) his batting-order code ("701" = the 7th
# slot's first substitute).

SUB_EVENTS = frozenset(
    {
        "offensive_substitution",
        "defensive_substitution",
        "defensive_switch",
        "pitching_substitution",
    }
)
_DEFENSE_KEYS = (
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
_BASES = ("first", "second", "third")

#: A play that ends on a runner out (the third out on the bases) does not end
#: the batter's plate appearance: he leads off the next inning.
_RUNNER_OUT_PREFIXES = ("caught_stealing", "pickoff")


def batter_finished(play: Mapping[str, Any]) -> bool:
    """Whether the batter's plate appearance ended on this play, by its event
    (the fallback of :func:`finished_flags` for a side's latest play)."""
    event = str((play.get("result") or {}).get("eventType") or "")
    return not event.startswith(_RUNNER_OUT_PREFIXES)


def finished_flags(plays: list[Mapping[str, Any]]) -> list[bool]:
    """Per play, whether the batter's plate appearance ended on it.

    A half that ends on an out on the bases (a caught stealing, a pickoff, an
    "other out") leaves the batter's turn open: he leads off his side's next
    inning. So a play is unfinished when the same side's next play has the same
    batter. A side's latest play (a live game) falls back to the event.
    """
    flags = [True] * len(plays)
    # Per side: the next play's batter, and the men he pinch-hit for before
    # his first pitch (a pinch hitter continues the same turn).
    nxt: dict[bool, tuple[int | None, set[int]] | None] = {True: None, False: None}
    for i in range(len(plays) - 1, -1, -1):
        play = plays[i]
        top = bool((play.get("about") or {}).get("isTopInning"))
        batter = _pid((play.get("matchup") or {}).get("batter"))
        following = nxt[top]
        if following is None:
            flags[i] = batter_finished(play)
        else:
            flags[i] = batter != following[0] and batter not in following[1]
        replaced: set[int] = set()
        for e in play.get("playEvents") or []:
            if e.get("isPitch"):
                break
            if (e.get("details") or {}).get("eventType") == "offensive_substitution" and (
                (e.get("position") or {}).get("abbreviation") == "PH"
            ):
                rp = _pid(e.get("replacedPlayer"))
                if rp is not None:
                    replaced.add(rp)
        nxt[top] = (batter, replaced)
    return flags


def _pid(v: Any) -> int | None:
    return _int(v.get("id")) if isinstance(v, Mapping) else None


def _side_of(play: Mapping[str, Any], fielding: bool) -> str:
    top = bool((play.get("about") or {}).get("isTopInning"))
    batting = "away" if top else "home"
    return ("home" if top else "away") if fielding else batting


def _names(payload: Mapping[str, Any]) -> dict[int, str]:
    players = (payload.get("gameData") or {}).get("players") or {}
    return {
        int(p["id"]): str(p.get("fullName") or "")
        for p in players.values()
        if p.get("id") is not None
    }


def parse_plays(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The real game's plate appearances, in order, and a name map.

    Each play: ``at_bat`` (the feed's ``atBatIndex``), inning, half, the batter
    and pitcher ids, the event and its description, RBI, the outs before and
    after, the score before and after, the runners at its start, and its pitches
    (the call, the pitch type, the speed). An intentional walk with no pitch is a
    play like any other.
    """
    plays = ((payload.get("liveData") or {}).get("plays") or {}).get("allPlays") or []
    out: list[dict[str, Any]] = []
    flags = finished_flags(plays)
    prev_score = (0, 0)
    prev_key: tuple[int, str] | None = None
    prev_outs_after = 0
    prev_post: dict[str, int | None] = dict.fromkeys(_BASES)
    for i, p in enumerate(plays):
        about = p.get("about") or {}
        if not about.get("isComplete", True) and not p.get("playEvents"):
            continue
        key = (int(about.get("inning") or 0), "top" if about.get("isTopInning") else "bottom")
        same_half = key == prev_key
        first_pitch = next((e for e in p.get("playEvents") or [] if e.get("isPitch")), None)
        if first_pitch is not None:
            off = first_pitch.get("offense") or {}
            runners = {b: _pid(off.get(b)) for b in _BASES}
        else:
            runners = dict(prev_post) if same_half else dict.fromkeys(_BASES)
        res = p.get("result") or {}
        matchup = p.get("matchup") or {}
        after = (_int(res.get("awayScore")) or 0, _int(res.get("homeScore")) or 0)
        pitches = [
            {
                "call": ((e.get("details") or {}).get("call") or {}).get("description")
                or (e.get("details") or {}).get("description"),
                "type": ((e.get("details") or {}).get("type") or {}).get("description"),
                "speed": (e.get("pitchData") or {}).get("startSpeed"),
            }
            for e in p.get("playEvents") or []
            if e.get("isPitch")
        ]
        out.append(
            {
                "at_bat": int(p.get("atBatIndex", len(out))),
                "inning": key[0],
                "half": key[1],
                "batter_id": _pid(matchup.get("batter")),
                "pitcher_id": _pid(matchup.get("pitcher")),
                "event": res.get("event"),
                "event_type": res.get("eventType"),
                "description": res.get("description"),
                "rbi": _int(res.get("rbi")) or 0,
                "batter_finished": flags[i],
                "outs_before": prev_outs_after if same_half else 0,
                "outs_after": _int((p.get("count") or {}).get("outs")) or 0,
                "away_score_before": prev_score[0],
                "home_score_before": prev_score[1],
                "away_score_after": after[0],
                "home_score_after": after[1],
                "runners": runners,
                "pitches": pitches,
                "is_complete": bool(about.get("isComplete", True)),
            }
        )
        prev_score, prev_key = after, key
        prev_outs_after = out[-1]["outs_after"]
        prev_post = {b: _pid(matchup.get(f"postOn{b.capitalize()}")) for b in _BASES}
    return {"plays": out, "names": _names(payload)}


def _roster(payload: Mapping[str, Any], side: str) -> dict[str, Any]:
    box = (((payload.get("liveData") or {}).get("boxscore") or {}).get("teams") or {}).get(
        side
    ) or {}
    players = box.get("players") or {}
    starters: list[int | None] = [None] * 9
    positions: dict[int, str] = {}
    for p in players.values():
        order = _int(p.get("battingOrder"))
        pid = _pid(p.get("person"))
        if order is not None and pid is not None and order % 100 == 0 and 1 <= order // 100 <= 9:
            starters[order // 100 - 1] = pid
            positions[pid] = _position(p) or "DH"
    pitchers = [int(x) for x in box.get("pitchers") or []]
    hitters = [int(x) for x in (box.get("batters") or []) + (box.get("bench") or [])]
    arms = [int(x) for x in (box.get("pitchers") or []) + (box.get("bullpen") or [])]
    return {
        "starters": starters,
        "positions": positions,
        "starting_pitcher": pitchers[0] if pitchers else None,
        "hitters": list(dict.fromkeys(hitters)),
        "arms": list(dict.fromkeys(arms)),
    }


def _hands(payload: Mapping[str, Any]) -> tuple[dict[int, str], dict[int, str]]:
    bats: dict[int, str] = {}
    throws: dict[int, str] = {}
    for p in ((payload.get("gameData") or {}).get("players") or {}).values():
        pid = _int(p.get("id"))
        if pid is None:
            continue
        b = (p.get("batSide") or {}).get("code")
        t = (p.get("pitchHand") or {}).get("code")
        if b:
            bats[pid] = str(b)
        if t:
            throws[pid] = str(t)
    return bats, throws


def _defense_of(event: Mapping[str, Any]) -> dict[str, int]:
    d = event.get("defense") or {}
    return {pos: pid for key, pos in _DEFENSE_KEYS if (pid := _pid(d.get(key))) is not None}


class PlateAppearanceNotFound(LookupError):
    """The game has no plate appearance with that index (or it has not begun)."""


def state_at_pa(payload: Mapping[str, Any], at_bat: int | None) -> dict[str, Any]:
    """The start state at the start of plate appearance ``at_bat`` (the feed's
    ``atBatIndex``); ``None`` is first pitch.

    Plain data for ``simulation.start_state`` plus what the page shows: the
    eligible bench and bullpen of each side and the players already used, who
    cannot re-enter. See ``simulation/start_state.py`` for the keys.
    """
    plays = ((payload.get("liveData") or {}).get("plays") or {}).get("allPlays") or []
    rosters = {s: _roster(payload, s) for s in ("away", "home")}
    bats, throws = _hands(payload)
    lineup = {s: list(rosters[s]["starters"]) for s in ("away", "home")}
    if any(p is None for s in ("away", "home") for p in lineup[s]):
        raise PlateAppearanceNotFound("the lineups are not posted yet")
    pitcher = {s: rosters[s]["starting_pitcher"] for s in ("away", "home")}
    if any(v is None for v in pitcher.values()):
        raise PlateAppearanceNotFound("a starting pitcher is not known yet")
    positions = {s: dict(rosters[s]["positions"]) for s in ("away", "home")}
    defense: dict[str, dict[str, int]] = {s: {} for s in ("away", "home")}
    for s in ("away", "home"):
        defense[s] = {
            pos: pid for pid, pos in positions[s].items() if pos not in ("DH", "PH", "PR")
        }
        defense[s]["P"] = int(pitcher[s])
    used: dict[str, set[int]] = {s: set(lineup[s]) | {int(pitcher[s])} for s in ("away", "home")}
    pitch_counts: dict[int, int] = {}
    batters_faced: dict[int, int] = {}
    last_slot: dict[str, int | None] = {"away": None, "home": None}

    grid: dict[str, list[int | None]] = {"away": [], "home": []}
    score = (0, 0)
    half_start = (0, 0)
    cur_key: tuple[int, str] = (1, "top")
    outs = 0
    half_pa = 0
    runners: dict[str, int | None] = dict.fromkeys(_BASES)
    post: dict[str, int | None] = dict.fromkeys(_BASES)

    def apply_sub(event: Mapping[str, Any], play: Mapping[str, Any]) -> None:
        det = event.get("details") or {}
        kind = det.get("eventType")
        pid = _pid(event.get("player"))
        if kind not in SUB_EVENTS or pid is None:
            return
        side = _side_of(play, fielding=kind != "offensive_substitution")
        pos = (event.get("position") or {}).get("abbreviation")
        order = _int(event.get("battingOrder"))
        replaced = _pid(event.get("replacedPlayer"))
        if order is not None and 1 <= order // 100 <= 9:
            slot = order // 100 - 1
            old = lineup[side][slot]
            lineup[side][slot] = pid
            # A pinch hitter or runner takes the field position of the man he replaced.
            if kind == "offensive_substitution" and old is not None:
                for p_, pl in list(defense[side].items()):
                    if pl == old:
                        defense[side][p_] = pid
        if kind == "pitching_substitution":
            pitcher[side] = pid
            defense[side]["P"] = pid
        elif (
            kind in ("defensive_substitution", "defensive_switch")
            and pos
            and pos not in ("PH", "PR", "DH")
        ):
            for p_, pl in list(defense[side].items()):
                if pl == pid:
                    del defense[side][p_]
            if replaced is not None:
                for p_, pl in list(defense[side].items()):
                    if pl == replaced:
                        del defense[side][p_]
            defense[side][pos] = pid
        used[side].add(pid)

    target_seen = at_bat is None
    flags = finished_flags(plays)
    for n_play, play in enumerate(plays if at_bat is not None else []):
        about = play.get("about") or {}
        idx = int(play.get("atBatIndex", -1))
        key = (int(about.get("inning") or 0), "top" if about.get("isTopInning") else "bottom")
        if key != cur_key:
            # The previous half is complete: its runs into the grid.
            prev_side = "away" if cur_key[1] == "top" else "home"
            runs = (score[0] - half_start[0]) if prev_side == "away" else (score[1] - half_start[1])
            grid[prev_side].append(runs)
            half_start = score
            cur_key = key
            outs = 0
            half_pa = 0
            post = dict.fromkeys(_BASES)
        events = play.get("playEvents") or []
        batting = "away" if key[1] == "top" else "home"
        fielding = "home" if batting == "away" else "away"
        if at_bat is not None and idx == at_bat:
            # The events before this plate appearance's first pitch belong to it.
            first_pitch = None
            for e in events:
                if e.get("isPitch"):
                    first_pitch = e
                    break
                apply_sub(e, play)
            if first_pitch is not None:
                off = first_pitch.get("offense") or {}
                runners = {b: _pid(off.get(b)) for b in _BASES}
                d = _defense_of(first_pitch)
                if len(d) >= 9:
                    defense[fielding] = d
                    pitcher[fielding] = d["P"]
            else:
                runners = dict(post)
            target_seen = True
            break
        # A completed plate appearance before the target.
        for e in events:
            if e.get("isPitch"):
                d = _defense_of(e)
                if len(d) >= 9:
                    defense[fielding] = d
                    pitcher[fielding] = d["P"]
                p_ = d.get("P") or pitcher[fielding]
                if p_ is not None:
                    pitch_counts[int(p_)] = pitch_counts.get(int(p_), 0) + 1
            else:
                apply_sub(e, play)
        finished = flags[n_play]
        mp = _pid((play.get("matchup") or {}).get("pitcher"))
        if mp is not None:
            if finished:
                batters_faced[mp] = batters_faced.get(mp, 0) + 1
            used[fielding].add(mp)
        batter = _pid((play.get("matchup") or {}).get("batter"))
        if batter is not None and batter in lineup[batting]:
            # A batter whose turn ended on a runner out bats again: the slot
            # before his is the last one used.
            last_slot[batting] = (lineup[batting].index(batter) - (0 if finished else 1)) % 9
        res = play.get("result") or {}
        score = (_int(res.get("awayScore")) or score[0], _int(res.get("homeScore")) or score[1])
        outs = _int((play.get("count") or {}).get("outs")) or 0
        half_pa += 1
        mu = play.get("matchup") or {}
        post = {b: _pid(mu.get(f"postOn{b.capitalize()}")) for b in _BASES}
        for b in post.values():
            if b is not None:
                used[batting].add(b)
    if not target_seen:
        raise PlateAppearanceNotFound(f"no plate appearance {at_bat} in this game")

    batting = "away" if cur_key[1] == "top" else "home"
    if at_bat is None:
        cur_key, outs, half_pa = (1, "top"), 0, 0
        batting = "away"
        runners = dict.fromkeys(_BASES)
    slots = {
        s: 0 if last_slot[s] is None else (int(last_slot[s]) + 1) % 9 for s in ("away", "home")
    }
    if outs >= 3:
        outs = 0
    in_game = {s: set(lineup[s]) | set(defense[s].values()) for s in ("away", "home")}
    eligible = {
        s: {
            "hitters": [
                p for p in rosters[s]["hitters"] if p not in used[s] and p not in in_game[s]
            ],
            "pitchers": [p for p in rosters[s]["arms"] if p not in used[s] and p not in in_game[s]],
        }
        for s in ("away", "home")
    }
    names = _names(payload)
    return {
        "at_bat": at_bat,
        "inning": cur_key[0],
        "half": cur_key[1],
        "outs": int(outs),
        "away_score": int(score[0]),
        "home_score": int(score[1]),
        "half_start_away": int(half_start[0]),
        "half_start_home": int(half_start[1]),
        "grid_away": grid["away"],
        "grid_home": grid["home"],
        "runners": runners,
        "away_lineup": [int(p) for p in lineup["away"]],
        "home_lineup": [int(p) for p in lineup["home"]],
        "away_slot": slots["away"],
        "home_slot": slots["home"],
        "away_pitcher": int(pitcher["away"]),
        "home_pitcher": int(pitcher["home"]),
        "away_starter": rosters["away"]["starting_pitcher"],
        "home_starter": rosters["home"]["starting_pitcher"],
        "away_defense": defense["away"],
        "home_defense": defense["home"],
        "pitch_counts": {str(k): v for k, v in pitch_counts.items()},
        "batters_faced": {str(k): v for k, v in batters_faced.items()},
        "half_pa_count": int(half_pa),
        "bat_hands": {str(k): v for k, v in bats.items()},
        "throw_hands": {str(k): v for k, v in throws.items()},
        "bullpen": {s: eligible[s]["pitchers"] for s in ("away", "home")},
        "eligible": eligible,
        "used": {s: sorted(used[s] - in_game[s]) for s in ("away", "home")},
        "batting_side": batting,
        "names": {str(k): v for k, v in names.items()},
    }
