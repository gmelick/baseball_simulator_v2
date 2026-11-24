"""Extract the non-pitch play events that ``raw.pitches`` cannot hold.

``raw.pitches`` holds one row per pitch, so a play with no pitch leaves no row
there. An intentional walk is signalled, not thrown, so its play has no pitch
events; a pickoff out can end a play with no pitch too. These events, with the
pickoff throws, the stepoffs and the balks, go to ``raw.play_events``.

Where each signal lives in the feed (three of the five are not where you would
guess):

  pickoff throw   playEvent ``type == "pickoff"``. Its ``details.eventType`` is
                  EMPTY, so an eventType filter never sees it.
  stepoff         playEvent ``type == "stepoff"``; its eventType is empty too.
  balk            playEvent ``type == "action"`` with ``details.eventType`` of
                  ``balk`` or ``forced_balk``.
  intent walk     the PLAY's ``result.eventType == "intent_walk"``; no pitches.
  pickoff OUT     NOT on the playEvent but on the RUNNER entry, where
                  ``details.eventType`` reads ``pickoff_1b`` /
                  ``pickoff_caught_stealing_2b`` and ``movementReason`` reads
                  ``r_pickoff_*``.

The module is pure: it takes a fetched play dict and returns row dicts, with no
network and no database, so the tests can pin the payload shapes above.
"""

from __future__ import annotations

from typing import Any

#: The playEvent ``type`` values that are the event itself. Their
#: ``details.eventType`` is empty, so only the ``type`` finds them.
_TYPE_EVENTS: dict[str, str] = {
    "pickoff": "pickoff",
    "stepoff": "stepoff",
}

#: The ``details.eventType`` values of a balk, on a ``type == "action"`` playEvent.
#: A forced balk still moves the runners up, so both map to one ``event_type``.
_ACTION_EVENTS: dict[str, str] = {
    "balk": "balk",
    "forced_balk": "balk",
}

#: The ``details.eventType`` PREFIXES of a pickoff outcome, on a
#: ``type == "action"`` playEvent.
#:
#: MLB WRITES A PICKOFF TWICE, AND THE TWO EVENTS SAY DIFFERENT THINGS. The throw
#: is a ``type == "pickoff"`` event. When the throw resolves (the runner is out,
#: or the ball gets away), the feed adds a ``type == "action"`` event carrying
#: ``pickoff_1b`` / ``pickoff_caught_stealing_2b`` / ``pickoff_error_3b``. The
#: runner's movement is keyed to THAT index, not to the throw's. Game 744843,
#: atBatIndex 56::
#:
#:     idx 2  type=pickoff  eventType=None                        <- the throw
#:     idx 3  type=action   eventType=pickoff_caught_stealing_2b  <- the OUT
#:     runner r_pickoff_caught_stealing_2b playIndex=3 isOut=True
#:
#: A match on the throw alone records every harmless throw and loses every
#: outcome. A prefix match covers every base suffix (``_1b`` / ``_2b`` / ``_3b`` /
#: ``_home``), where a list would drop a suffix nobody thought of. Order matters:
#: ``pickoff_error`` comes before the bare ``pickoff``, or an errant throw reads as
#: a clean one.
_ACTION_PICKOFF_PREFIXES: tuple[tuple[str, str], ...] = (
    ("pickoff_error", "pickoff_error"),
    ("pickoff", "pickoff"),
)


def _action_pickoff_kind(etype: str) -> str | None:
    """Return the ``event_type`` of a pickoff outcome on an action event, else ``None``."""
    for prefix, kind in _ACTION_PICKOFF_PREFIXES:
        if etype.startswith(prefix):
            return kind
    return None


#: The play-level ``result.eventType`` of an intentional walk. The match is on
#: the play, because the play has no events to match.
_INTENT_WALK_RESULT = "intent_walk"

#: The runner ``movementReason`` prefixes of a pickoff. ``r_pickoff_error_*`` is
#: an errant throw: the runner ADVANCES, so it is its own ``event_type`` and never
#: an out.
_PICKOFF_ERROR_PREFIX = "r_pickoff_error"
_PICKOFF_PREFIX = "r_pickoff"

#: The ``outBase`` tokens. HOME IS ``"4B"``, not ``"home"``: a runner picked off
#: at the plate reads ``outBase: "4B"`` (game 744812, atBatIndex 42).
_BASE_FROM_TEXT: dict[str, int] = {"1B": 1, "2B": 2, "3B": 3, "4B": 4}


def _base_of(text: str | None) -> int | None:
    """Return the base a text names (``'2B'``, ``'pickoff_2b'`` → 2), else ``None``."""
    if not text:
        return None
    upper = str(text).upper()
    for token, num in _BASE_FROM_TEXT.items():
        if token in upper:
            return num
    return None


def _runners_state(offense: dict[str, Any] | None) -> int:
    """Return the base occupancy as three bits: bit 0 first, bit 1 second, bit 2 third.

    ``sim.pitch_pool.runners_state`` uses the same encoding, so a pool built from
    this table filters on the simulator's number.
    """
    o = offense or {}
    return (
        (1 if o.get("first") else 0) + (2 if o.get("second") else 0) + (4 if o.get("third") else 0)
    )


def _credits(runner: dict[str, Any]) -> tuple[int | None, int | None]:
    """Return ``(putout_player_id, assist_player_id)``, the first of each in a runner's credits."""
    putout = assist = None
    for c in runner.get("credits") or []:
        kind = c.get("credit")
        pid = (c.get("player") or {}).get("id")
        if kind == "f_putout" and putout is None:
            putout = pid
        elif kind == "f_assist" and assist is None:
            assist = pid
    return putout, assist


def _pickoff_runners(play: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Return each pickoff runner movement, keyed by its ``playIndex``.

    A pickoff OUT is on the runner, not on the playEvent, so the index joins the
    event to its outcome.
    """
    out: dict[int, dict[str, Any]] = {}
    for runner in play.get("runners") or []:
        details = runner.get("details") or {}
        reason = str(details.get("movementReason") or "")
        if not reason.startswith(_PICKOFF_PREFIX):
            continue
        idx = details.get("playIndex")
        if idx is None:
            continue
        out[int(idx)] = runner
    return out


def extract_play_events(
    play: dict[str, Any],
    *,
    game_pk: int,
    game_date: str,
    season: int,
    outs_before_play: int,
    runners_state_before: int,
    home_score_before: int,
    away_score_before: int,
) -> list[dict[str, Any]]:
    """Return one row dict per non-pitch event in ``play``.

    ``outs_before_play`` and ``runners_state_before`` are the state ENTERING the
    plate appearance. The caller tracks both, so this function stays pure.

    ``runners_state_before`` cannot come from the play. An intentional walk has
    no ``playEvents``, so it has no ``offense`` block to read the bases from, and
    a default of empty bases is wrong for an extra-inning walk with the automatic
    runner on second.

    ``home_score_before`` / ``away_score_before`` are the score ENTERING the play,
    as the pitch rows record it. The play's ``result.homeScore`` /
    ``result.awayScore`` are the score AFTER the play: read from there, a pickoff
    carries the runs of a home run hit later in the same plate appearance.
    """
    rows: list[dict[str, Any]] = []
    consumed_indices: set[int] = set()
    events = play.get("playEvents") or []
    about = play.get("about") or {}
    result = play.get("result") or {}
    matchup = play.get("matchup") or {}

    at_bat_number = int(play.get("atBatIndex", -1)) + 1
    inning = int(about.get("inning") or 0)
    topbot = "Top" if str(about.get("halfInning", "")).lower().startswith("top") else "Bot"
    pitcher_id = (matchup.get("pitcher") or {}).get("id")
    batter_id = (matchup.get("batter") or {}).get("id")

    # The thrower of each non-pitch event. `matchup.pitcher` is the LAST pitcher
    # of the plate appearance, so it is wrong for a throw made before a pitching
    # change in the middle of the plate appearance (an injury). The forward walk
    # tracks who is on the mound: a pitch names its pitcher (`defense.pitcher`,
    # present under the loader's `hydrate=alignment`), and a
    # `pitching_substitution` action names the INCOMING pitcher in `player.id`.
    # An event before any signal takes the pitcher of the nearest FOLLOWING
    # pitch. `matchup.pitcher` is the last resort.
    thrower_at: dict[int, int] = {}
    current: int | None = None
    for event in events:
        idx0 = event.get("index")
        if str(((event.get("details") or {}).get("eventType")) or "") == "pitching_substitution":
            pid = (event.get("player") or {}).get("id")
            if pid:
                current = int(pid)
        if idx0 is not None and current is not None:
            thrower_at[int(idx0)] = current
        if event.get("isPitch"):
            pid = ((event.get("defense") or {}).get("pitcher") or {}).get("id")
            if pid:
                current = int(pid)
                if idx0 is not None:
                    thrower_at[int(idx0)] = current
    following_pitcher: dict[int, int] = {}
    carry: int | None = None
    for event in reversed(events):
        if event.get("isPitch"):
            pid = ((event.get("defense") or {}).get("pitcher") or {}).get("id")
            if pid:
                carry = int(pid)
        idx0 = event.get("index")
        if idx0 is not None and carry is not None:
            following_pitcher[int(idx0)] = carry

    def _thrower(play_index: int) -> int | None:
        return thrower_at.get(play_index) or following_pitcher.get(play_index) or pitcher_id

    # The score entering the play, seen from the batting side. The play's own
    # `result` scores are post-play.
    if topbot == "Top":
        bat_score, fld_score = int(away_score_before), int(home_score_before)
    else:
        bat_score, fld_score = int(home_score_before), int(away_score_before)

    pickoff_by_index = _pickoff_runners(play)

    def _row(event_type: str, play_index: int, **kw: Any) -> dict[str, Any]:
        base = {
            "game_pk": game_pk,
            "at_bat_number": at_bat_number,
            "play_index": play_index,
            "game_date": game_date,
            "season": season,
            "event_type": event_type,
            "inning": inning,
            "inning_topbot": topbot,
            "outs_before": outs_before_play,
            "runners_state": 0,
            "bat_score": bat_score,
            "fld_score": fld_score,
            "pitcher_id": _thrower(play_index),
            "batter_id": batter_id,
            "runner_id": None,
            "base": None,
            "is_out": False,
            "out_number": None,
            "fielder_putout": None,
            "fielder_assist": None,
        }
        base.update(kw)
        return base

    for event in events:
        event_details = event.get("details") or {}
        etype = str(event_details.get("eventType") or "")
        raw_type = str(event.get("type") or "")
        index = event.get("index")
        if index is None:
            continue
        index = int(index)
        offense = event.get("offense")
        state = _runners_state(offense) if offense else runners_state_before

        kind = _TYPE_EVENTS.get(raw_type)
        if kind is None and raw_type == "action":
            # A balk, or a pickoff OUTCOME (see _ACTION_PICKOFF_PREFIXES).
            kind = _ACTION_EVENTS.get(etype) or _action_pickoff_kind(etype)
        if kind is None:
            continue

        # A balk or a stepoff has no runner movement to join; a pickoff may.
        if not kind.startswith("pickoff"):
            rows.append(_row(kind, index, runners_state=state))
            continue

        # The base the throw targeted. A bare throw names it ONLY in its
        # description text ("Pickoff Attempt 1B"); an outcome names it in its
        # eventType (`pickoff_1b` / `pickoff_error_3b`).
        event_base = _base_of(etype) or _base_of(event_details.get("description"))

        # The runner movement at the same index says whether a runner was out,
        # and at which base. A bare throw normally has none; an outcome always
        # has one. Without a movement, the event's own kind stands, so an
        # action-carried `pickoff_error` stays an error.
        runner = pickoff_by_index.get(index)
        consumed_indices.add(index)
        if runner is None:
            rows.append(_row(kind, index, runners_state=state, base=event_base))
            continue

        details = runner.get("details") or {}
        movement = runner.get("movement") or {}
        reason = str(details.get("movementReason") or "")
        is_error = reason.startswith(_PICKOFF_ERROR_PREFIX)
        putout, assist = _credits(runner)
        rows.append(
            _row(
                "pickoff_error" if is_error else "pickoff",
                index,
                runners_state=state,
                runner_id=(details.get("runner") or {}).get("id"),
                # The movement names the base of the out; the event's text is
                # the fallback.
                base=_base_of(movement.get("outBase") or reason) or event_base,
                # An errant throw advances the runner: it is never an out.
                is_out=bool(movement.get("isOut")) and not is_error,
                out_number=movement.get("outNumber") if not is_error else None,
                fielder_putout=putout,
                fielder_assist=assist,
            )
        )

    # A pickoff movement whose index matched no recorded event. A runner picked
    # off at HOME has his movement keyed to a PITCH index (game 744812,
    # atBatIndex 42: the throw at index 0, the movement at playIndex 2, a pitch).
    # The loop above emits no row for a pitch, so the out is recorded here, at
    # the index the feed names. This table is apart from `raw.pitches`, so the
    # shared index collides with nothing.
    for index, runner in sorted(pickoff_by_index.items()):
        if index in consumed_indices:
            continue
        details = runner.get("details") or {}
        movement = runner.get("movement") or {}
        reason = str(details.get("movementReason") or "")
        is_error = reason.startswith(_PICKOFF_ERROR_PREFIX)
        putout, assist = _credits(runner)
        rows.append(
            _row(
                "pickoff_error" if is_error else "pickoff",
                index,
                runners_state=runners_state_before,
                runner_id=(details.get("runner") or {}).get("id"),
                base=_base_of(movement.get("outBase")) or _base_of(reason),
                is_out=bool(movement.get("isOut")) and not is_error,
                out_number=movement.get("outNumber") if not is_error else None,
                fielder_putout=putout,
                fielder_assist=assist,
            )
        )

    # An intentional walk has no event of its own: the PLAY is the record. It
    # goes last, so the row order stays stable.
    if str(result.get("eventType") or "").lower() == _INTENT_WALK_RESULT:
        rows.append(
            _row(
                "intent_walk",
                # No playEvent to key on. The sentinel -1 collides with no real
                # index and sorts before every event.
                -1,
                runners_state=runners_state_before,
                runner_id=batter_id,
                base=1,
            )
        )

    return rows


__all__ = ["extract_play_events"]
