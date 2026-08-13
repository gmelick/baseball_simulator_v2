"""SIM-502: extract the non-pitch play events ``raw.pitches`` cannot represent.

WHY THIS MODULE EXISTS
======================
``raw.pitches`` holds one row per PITCH. Some plays contain no pitch at all, so
they produce no row and are invisible. Measured over 150 real 2024 games
(11,247 plays): 24 plays had zero pitch events — 21 intentional walks and 3
pickoff caught-stealings.

The intentional walk is the worse half. It is signalled, not thrown, so it
generates no pitch events. Confirmed against the live database: **zero** rows
carry ``events = 'intent_walk'`` against 14,683 ordinary walks, in a season that
really contained roughly 340-450 of them. ``walk_rate`` in the profile SQL
filters ``events IN ('walk','intent_walk')`` and that second branch has never
matched anything.

WHERE EACH SIGNAL LIVES — verified against real payloads, and NOT where you would
guess for three of the five:

  pickoff throw   playEvent ``type == "pickoff"``.  Its ``details.eventType`` is
                  EMPTY, so an eventType filter never sees it.
  stepoff         playEvent ``type == "stepoff"``, eventType also empty.
  balk            playEvent ``type == "action"`` with ``details.eventType`` of
                  ``balk`` or ``forced_balk``.
  intent walk     the PLAY's ``result.event == "Intent Walk"``, no pitches.
  pickoff OUT     NOT on the playEvent — on the RUNNER entry, where
                  ``details.eventType`` reads ``pickoff_1b`` /
                  ``pickoff_caught_stealing_2b`` and ``movementReason`` reads
                  ``r_pickoff_*``.

This module is PURE: it takes an already-fetched play dict and returns row
dicts. No network, no database. That is what lets the tests pin the payload
shapes above without a live stack.
"""

from __future__ import annotations

from typing import Any

#: playEvent ``type`` values that are themselves the event we want to record.
#: Their ``details.eventType`` is empty, so they can only be found by ``type``.
_TYPE_EVENTS: dict[str, str] = {
    "pickoff": "pickoff",
    "stepoff": "stepoff",
}

#: ``details.eventType`` values on a ``type == "action"`` playEvent.  A forced
#: balk is still a balk — the runner still takes the base — so both map to one
#: ``event_type`` and the distinction is not modelled.
#:
#: ⚠ MLB WRITES A PICKOFF TWICE, AND THE TWO EVENTS SAY DIFFERENT THINGS.
#: A throw over is ``type == "pickoff"``.  When that throw RESOLVES — the runner
#: is retired, or the ball gets away — the feed emits a SECOND playEvent, a
#: ``type == "action"`` carrying ``pickoff_1b`` / ``pickoff_caught_stealing_2b`` /
#: ``pickoff_error_3b``, and the runner movement is keyed to THAT index, not to
#: the throw.  Game 744843 atBatIndex 56, verbatim::
#:
#:     idx 2  type=pickoff  eventType=None                        <- the throw
#:     idx 3  type=action   eventType=pickoff_caught_stealing_2b  <- the OUT
#:     runner r_pickoff_caught_stealing_2b playIndex=3 isOut=True
#:
#: Matching only ``type == "pickoff"`` therefore records every harmless throw and
#: loses every outcome.  Measured over 1,066 real 2024 games: **0 of 72 pickoff
#: errors and only 70 of 159 pickoff outs** survived that version.
#:
#: These are matched by PREFIX rather than by an exhaustive list, because the base
#: suffix varies (``_1b`` / ``_2b`` / ``_3b`` / ``_home``) and a list would silently
#: drop whichever suffix nobody thought of.
_ACTION_EVENTS: dict[str, str] = {
    "balk": "balk",
    "forced_balk": "balk",
}

#: ``details.eventType`` PREFIXES on a ``type == "action"`` playEvent that carry a
#: pickoff outcome.  Order matters: ``pickoff_error`` must be tested before the
#: bare ``pickoff`` prefix, or an errant throw is misread as a clean one.
_ACTION_PICKOFF_PREFIXES: tuple[tuple[str, str], ...] = (
    ("pickoff_error", "pickoff_error"),
    ("pickoff", "pickoff"),
)


def _action_pickoff_kind(etype: str) -> str | None:
    """The ``event_type`` for an action-carried pickoff outcome, else ``None``."""
    for prefix, kind in _ACTION_PICKOFF_PREFIXES:
        if etype.startswith(prefix):
            return kind
    return None


#: The play-level result that marks an intentional walk.  Matched on the play,
#: not on any event, because the play carries no events to match.
_INTENT_WALK_RESULT = "intent_walk"

#: A runner ``movementReason`` naming a pickoff.  ``r_pickoff_error_*`` is the
#: errant throw — the runner ADVANCES rather than being retired — so it is a
#: separate ``event_type`` and never an out.
_PICKOFF_ERROR_PREFIX = "r_pickoff_error"
_PICKOFF_PREFIX = "r_pickoff"

#: ``outBase`` tokens. HOME IS ``"4B"``, not ``"home"`` — a runner picked off at
#: the plate reads ``outBase: "4B"``, so a map of 1B/2B/3B silently returns None
#: and the row loses which base the out happened at. Verified on game 744812
#: atBatIndex 42: ``r_pickoff_caught_stealing_home ... outBase '4B'``.
_BASE_FROM_TEXT: dict[str, int] = {"1B": 1, "2B": 2, "3B": 3, "4B": 4}


def _base_of(text: str | None) -> int | None:
    """Map ``'1B'`` / ``'2b'`` / ``'pickoff_2b'`` to ``2``. ``None`` when absent."""
    if not text:
        return None
    upper = str(text).upper()
    for token, num in _BASE_FROM_TEXT.items():
        if token in upper:
            return num
    return None


def _runners_state(offense: dict[str, Any] | None) -> int:
    """Three-bit occupancy mask: bit0=1B, bit1=2B, bit2=3B.

    Matches the encoding ``sim.pitch_pool.runners_state`` uses, so a pool built
    from this table filters on the same number the simulator does.
    """
    o = offense or {}
    return (
        (1 if o.get("first") else 0) + (2 if o.get("second") else 0) + (4 if o.get("third") else 0)
    )


def _credits(runner: dict[str, Any]) -> tuple[int | None, int | None]:
    """``(putout_player_id, assist_player_id)`` from a runner's credits."""
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
    """Index every pickoff-related runner movement by its ``playIndex``.

    A pickoff OUT is recorded on the runner, not on the playEvent, so the throw
    and its outcome have to be joined back together by index.
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

    ``outs_before_play`` and ``runners_state_before`` describe the state ENTERING
    the plate appearance. The caller tracks both; this module does not, because a
    pure function that also owned running state would be two things.

    ``runners_state_before`` is not optional and cannot be derived here. An
    INTENT WALK play carries no ``playEvents`` at all, so there is no per-event
    ``offense`` block to read the base state from — the first version of this
    function fell back to an empty dict and recorded every intentional walk with
    the bases empty. A tenth-inning intentional walk under the ghost-runner rule
    proved it: real state was a runner on second, recorded state was 0.

    ``home_score_before`` / ``away_score_before`` are the score ENTERING the
    play, exactly as the pitch rows record it. They cannot be read from the
    play itself: ``result.homeScore`` / ``result.awayScore`` are the score
    AFTER the play, and the first version of this function read them — so a
    pickoff at playEvent 4 was stamped with the runs from a home run four
    pitches LATER, look-ahead leakage on 12.3% of rows (SIM-502b, measured:
    676 of 5,479). The loader already proves the pre-play semantics: it uses
    ``result.homeScore`` to seed the NEXT play, never the current one.
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

    # Score entering the play, from the batting side's perspective. Supplied by
    # the caller — the play's own `result` scores are POST-play (SIM-502b).
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
            "pitcher_id": pitcher_id,
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
            # A balk first, then the pickoff OUTCOME events. See the note on
            # _ACTION_EVENTS: the outcome rides on an action, not on the throw.
            kind = _ACTION_EVENTS.get(etype) or _action_pickoff_kind(etype)
        if kind is None:
            continue

        # Both pickoff kinds carry a runner movement worth joining. A bare throw
        # (type == "pickoff") usually has none; an action-carried outcome always
        # does, and that movement is what says whether the runner was retired.
        if not kind.startswith("pickoff"):
            rows.append(_row(kind, index, runners_state=state))
            continue

        # SIM-502d: the base the throw targeted. A bare throw carries it ONLY
        # in the description text ("Pickoff Attempt 1B" — no structured field
        # exists on the event, verified); an action-carried outcome names it in
        # the eventType itself (`pickoff_1b` / `pickoff_error_3b`). Without
        # this, 96.3% of pickoff rows had no base.
        event_base = _base_of(etype) or _base_of(event_details.get("description"))

        # Join the runner movement recorded against this same index to learn
        # whether anyone was retired, and at which base. A bare throw normally has
        # no movement; an action-carried outcome always does. When there is none,
        # fall back to the kind the event itself declared — that keeps an
        # action-carried `pickoff_error` an error even if its movement is absent.
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
                # The runner movement names where the OUT happened; the throw's
                # own text is the fallback when the movement is silent.
                base=_base_of(movement.get("outBase") or reason) or event_base,
                # An errant throw ADVANCES the runner. It is never an out, even
                # though it is reported on the same pickoff play.
                is_out=bool(movement.get("isOut")) and not is_error,
                out_number=movement.get("outNumber") if not is_error else None,
                fielder_putout=putout,
                fielder_assist=assist,
            )
        )

    # A pickoff movement whose index matched NO event we recorded.
    #
    # A runner picked off at HOME has the movement keyed to a PITCH index, not to
    # the throw and not to an action. Game 744812 atBatIndex 42: the throw is at
    # index 0, the movement reads `playIndex: 2`, and index 2 is a pitch. Nothing
    # above emits a row for a pitch, so that out was dropped — the last one
    # missing after the action-event fix, 26 of 27.
    #
    # The out is real, so record it at the index the feed named. This table is
    # separate from `raw.pitches`, so sharing an index with a pitch collides with
    # nothing.
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

    # The intentional walk carries no event of its own — the PLAY is the record.
    # Emitted last so a play that somehow held both keeps a stable order.
    if str(result.get("eventType") or "").lower() == _INTENT_WALK_RESULT:
        rows.append(
            _row(
                "intent_walk",
                # No playEvent to key on. -1 is a deliberate sentinel: it cannot
                # collide with a real index and it sorts before every event.
                -1,
                runners_state=runners_state_before,
                runner_id=batter_id,
                base=1,
            )
        )

    return rows


__all__ = ["extract_play_events"]
