"""Managerial changes on a start state (the game page's "what if").

The user picks a plate appearance of the real game and makes real managerial
moves before it. :func:`apply_changes` writes them onto a start state (see
``simulation.start_state``) and returns a new one; the input is not changed.

The moves:

* **pinch-hit** ``{"side", "slot", "player_id"}``: the batting side's lineup slot
  (0-8) gets a bench hitter; he takes the replaced man's field position too.
* **reliever** ``{"side", "player_id"}``: the fielding side brings in an arm from
  its bullpen; he takes the P of the defense, starts at 0 pitches, and leaves
  the pen. In a lineup without a DH he takes the pitcher's batting slot.
* **pinch-run** ``{"base", "player_id"}``: a bench player replaces the runner on
  ``first``, ``second`` or ``third`` and takes that runner's lineup slot and field
  position.
* **defense** ``{"side", "position", "player_id"}``: a player already in the
  game moves to the position (the man there takes his old one), or a bench
  player replaces the man at it (and his lineup slot). ``position`` ``P`` is a
  reliever.

Every move is checked against the start state's ``eligible`` lists (bench
hitters, unused arms) and its ``used`` lists: a player who left the game cannot
come back, and a bench player enters once. A bad move raises
:class:`IllegalChange` with the reason.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

POSITIONS = ("P", "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF")
BASES = ("first", "second", "third")


class IllegalChange(ValueError):
    """A managerial change the rules (or the roster) do not allow."""


def _other(side: str) -> str:
    return "home" if side == "away" else "away"


def apply_changes(start: Mapping[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
    """A new start state with ``changes`` applied (see the module docstring)."""
    ss: dict[str, Any] = copy.deepcopy(dict(start))
    batting = ss["batting_side"]
    fielding = _other(batting)
    # Before first pitch either side may change its lineup or its starter.
    pregame = ss.get("at_bat") is None
    eligible = {
        s: {k: list(v) for k, v in (ss.get("eligible") or {}).get(s, {}).items()}
        for s in ("away", "home")
    }
    entered: set[int] = set()

    def take(side: str, pid: int, kind: str) -> None:
        pool = eligible.get(side, {}).get(kind, [])
        if pid in entered or pid not in pool:
            label = "bench hitter" if kind == "hitters" else "available reliever"
            raise IllegalChange(f"player {pid} is not an {label} for the {side} side at this point")
        pool.remove(pid)
        entered.add(pid)

    def replace_everywhere(side: str, old: int, new: int) -> None:
        lineup = ss[f"{side}_lineup"]
        for i, p in enumerate(lineup):
            if p == old:
                lineup[i] = new
        defense = ss[f"{side}_defense"]
        for pos, p in list(defense.items()):
            if p == old:
                defense[pos] = new

    def bring_in_pitcher(side: str, pid: int) -> None:
        if side != fielding and not pregame:
            raise IllegalChange(
                "only the fielding side can change pitchers before this plate appearance"
            )
        take(side, pid, "pitchers")
        old = int(ss[f"{side}_pitcher"])
        ss[f"{side}_pitcher"] = pid
        replace_everywhere(side, old, pid)
        ss[f"{side}_defense"]["P"] = pid
        ss.setdefault("pitch_counts", {})[str(pid)] = 0
        ss["bullpen"][side] = [p for p in ss["bullpen"][side] if p != pid]

    for move in changes.get("pinch_hit") or []:
        side = str(move.get("side") or batting)
        if side != batting and not pregame:
            raise IllegalChange("a pinch hitter bats for the side at bat")
        slot = int(move["slot"])
        if not 0 <= slot < 9:
            raise IllegalChange(f"lineup slot {slot} is not 0-8")
        pid = int(move["player_id"])
        take(side, pid, "hitters")
        old = int(ss[f"{side}_lineup"][slot])
        replace_everywhere(side, old, pid)

    if changes.get("pitcher"):
        move = changes["pitcher"]
        bring_in_pitcher(str(move.get("side") or fielding), int(move["player_id"]))

    for move in changes.get("pinch_run") or []:
        base = str(move["base"])
        if base not in BASES:
            raise IllegalChange(f"base {base!r} is not first, second or third")
        runner = (ss.get("runners") or {}).get(base)
        if runner is None:
            raise IllegalChange(f"nobody is on {base}")
        pid = int(move["player_id"])
        take(batting, pid, "hitters")
        ss["runners"][base] = pid
        replace_everywhere(batting, int(runner), pid)

    for move in changes.get("defense") or []:
        side = str(move.get("side") or fielding)
        pos = str(move["position"]).upper()
        pid = int(move["player_id"])
        if pos not in POSITIONS:
            raise IllegalChange(f"position {pos!r} is not one of {', '.join(POSITIONS)}")
        if pos == "P":
            bring_in_pitcher(side, pid)
            continue
        defense = ss[f"{side}_defense"]
        current = defense.get(pos)
        in_game = pid in ss[f"{side}_lineup"] or pid in defense.values()
        if in_game:
            # A switch: the mover's old position goes to the man he displaces.
            old_pos = next((p for p, x in defense.items() if x == pid), None)
            if old_pos == "P":
                raise IllegalChange("the pitcher cannot move to the field here")
            defense[pos] = pid
            if old_pos is not None and current is not None:
                defense[old_pos] = current
            elif old_pos is not None:
                del defense[old_pos]
        else:
            take(side, pid, "hitters")
            if current is None:
                raise IllegalChange(f"nobody plays {pos} for the {side} side")
            replace_everywhere(side, int(current), pid)

    hands_b = ss.get("bat_hands") or {}
    hands_t = ss.get("throw_hands") or {}
    for pid in entered:
        if str(pid) not in hands_b and str(pid) not in hands_t:
            raise IllegalChange(f"player {pid} has no known hand")
    ss["eligible"] = eligible
    return ss


def describe_changes(changes: Mapping[str, Any], names: Mapping[str, str]) -> list[str]:
    """One plain line per move ("Pinch-hit: J. McNeil for slot 7")."""

    def n(pid: Any) -> str:
        return str(names.get(str(pid), pid))

    out = [
        f"Pinch-hit: {n(m['player_id'])} in slot {int(m['slot']) + 1}"
        for m in changes.get("pinch_hit") or []
    ]
    if changes.get("pitcher"):
        out.append(f"Reliever: {n(changes['pitcher']['player_id'])}")
    out += [
        f"Pinch-run: {n(m['player_id'])} at {m['base']}" for m in changes.get("pinch_run") or []
    ]
    out += [
        f"Defense: {n(m['player_id'])} to {m['position']}" for m in changes.get("defense") or []
    ]
    return out
