"""SIM-502: the non-pitch play events ``raw.pitches`` cannot represent.

Every payload shape below was COPIED FROM A REAL 2024 GAME, not invented. That
matters: three of the five signals are not where a reasonable person would guess,
and a test built on a guessed shape would pass while the loader saw nothing.

  * a pickoff throw carries ``type == "pickoff"`` and an EMPTY
    ``details.eventType`` — an eventType filter never matches it
  * a balk is ``type == "action"`` with ``details.eventType == "balk"``
  * a pickoff OUT is on the RUNNER entry, not the play event
  * an intentional walk has NO play events at all

The last one is why this table exists. Measured over 150 real games, 24 plays had
zero pitch events and 21 of them were intentional walks. Confirmed against the
live database: ``raw.pitches`` holds ZERO rows with ``events = 'intent_walk'``
against 14,683 ordinary walks.
"""

from __future__ import annotations

import pytest

from pipeline.etl.play_events import extract_play_events

_CTX = {"game_pk": 744797, "game_date": "2024-07-20", "season": 2024}


def _play(events, *, runners=None, result_type=None, at_bat=41, inning=6, half="bottom"):
    return {
        "atBatIndex": at_bat - 1,
        "about": {"inning": inning, "halfInning": half},
        "result": {"eventType": result_type, "homeScore": 3, "awayScore": 1},
        "matchup": {"pitcher": {"id": 592866}, "batter": {"id": 547180}},
        "playEvents": events,
        "runners": runners or [],
    }


def _ev(index, kind, event_type=None, offense=None):
    return {
        "index": index,
        "type": kind,
        "isPitch": kind == "pitch",
        "details": {"eventType": event_type} if event_type is not None else {},
        "offense": offense if offense is not None else {},
    }


def _run(index, reason, *, out=True, out_base="1B", out_number=1, runner_id=547180):
    """A runner movement, shaped as the real feed emits it."""
    return {
        "movement": {
            "end": None,
            "outBase": out_base,
            "isOut": out,
            "outNumber": out_number if out else None,
        },
        "details": {
            "movementReason": reason,
            "eventType": reason.removeprefix("r_"),
            "runner": {"id": runner_id},
            "playIndex": index,
        },
        "credits": [
            {"player": {"id": 592866}, "credit": "f_assist"},
            {"player": {"id": 660766}, "credit": "f_putout"},
        ],
    }


def _extract(play, outs=0, state=0):
    return extract_play_events(play, outs_before_play=outs, runners_state_before=state, **_CTX)


class TestTheSignalsAreWhereTheRealFeedPutsThem:
    def test_a_pickoff_is_found_by_type_not_by_eventtype(self):
        """Its ``details.eventType`` is empty. Filtering on eventType finds nothing."""
        rows = _extract(_play([_ev(0, "pickoff")]))
        assert [r["event_type"] for r in rows] == ["pickoff"]

    def test_a_stepoff_is_recorded(self):
        rows = _extract(_play([_ev(2, "stepoff")]))
        assert [r["event_type"] for r in rows] == ["stepoff"]

    @pytest.mark.parametrize("etype", ["balk", "forced_balk"])
    def test_a_balk_is_an_action_with_an_eventtype(self, etype):
        """Both spellings are a balk: the runner takes the base either way."""
        rows = _extract(_play([_ev(1, "action", etype)]))
        assert [r["event_type"] for r in rows] == ["balk"]

    def test_an_ordinary_pitch_produces_nothing(self):
        """This table holds only NON-pitch events. Pitches go to raw.pitches."""
        assert _extract(_play([_ev(0, "pitch")])) == []

    def test_an_unrelated_action_is_ignored(self):
        """A mound visit or substitution is not a play event we model."""
        rows = _extract(_play([_ev(0, "action", "mound_visit")]))
        assert rows == []


class TestThePickoffOutComesFromTheRunnerNotTheEvent:
    def test_a_pickoff_with_no_runner_movement_is_a_throw_not_an_out(self):
        rows = _extract(_play([_ev(0, "pickoff")]))
        assert rows[0]["is_out"] is False
        assert rows[0]["runner_id"] is None

    def test_a_pickoff_out_takes_its_outcome_from_the_runner(self):
        rows = _extract(
            _play([_ev(0, "pickoff")], runners=[_run(0, "r_pickoff_1b", out_base="1B")])
        )
        assert rows[0]["is_out"] is True
        assert rows[0]["base"] == 1
        assert rows[0]["runner_id"] == 547180
        assert rows[0]["fielder_putout"] == 660766
        assert rows[0]["fielder_assist"] == 592866

    def test_a_pickoff_caught_stealing_is_still_a_pickoff(self):
        rows = _extract(
            _play(
                [_ev(3, "pickoff")],
                runners=[_run(3, "r_pickoff_caught_stealing_2b", out_base="2B")],
            )
        )
        assert rows[0]["event_type"] == "pickoff"
        assert rows[0]["is_out"] is True
        assert rows[0]["base"] == 2

    def test_an_errant_pickoff_throw_is_never_an_out(self):
        """``r_pickoff_error_*`` means the throw got away and the runner ADVANCED.

        It is reported on the same pickoff play, so a naive reading of ``isOut``
        would book an out on a play where the runner gained a base.
        """
        rows = _extract(
            _play(
                [_ev(0, "pickoff")],
                runners=[_run(0, "r_pickoff_error_1b", out=False, out_base=None)],
            )
        )
        assert rows[0]["event_type"] == "pickoff_error"
        assert rows[0]["is_out"] is False
        assert rows[0]["out_number"] is None

    def test_the_third_out_is_recorded_as_such(self):
        """``out_number == 3`` marks the case a pitch-row design cannot hold: the
        pickoff ended the half-inning, so no pitch follows it."""
        rows = _extract(_play([_ev(0, "pickoff")], runners=[_run(0, "r_pickoff_1b", out_number=3)]))
        assert rows[0]["out_number"] == 3

    def test_a_runner_movement_on_a_different_index_is_not_joined(self):
        """Two throws in one plate appearance: the out belongs to ONE of them."""
        rows = _extract(
            _play(
                [_ev(0, "pickoff"), _ev(1, "pickoff")],
                runners=[_run(1, "r_pickoff_1b")],
            )
        )
        by_index = {r["play_index"]: r for r in rows}
        assert by_index[0]["is_out"] is False
        assert by_index[1]["is_out"] is True


class TestTheIntentionalWalk:
    def test_it_is_captured_with_no_play_events_at_all(self):
        """The whole reason this table exists. No events, so nothing to iterate."""
        rows = _extract(_play([], result_type="intent_walk"))
        assert [r["event_type"] for r in rows] == ["intent_walk"]

    def test_it_awards_first_base_to_the_batter(self):
        rows = _extract(_play([], result_type="intent_walk"))
        assert rows[0]["base"] == 1
        assert rows[0]["runner_id"] == rows[0]["batter_id"]
        assert rows[0]["is_out"] is False

    def test_it_records_the_base_state_the_caller_supplied(self):
        """THE REGRESSION THIS FILE EXISTS FOR.

        The first version of the extractor read base state from the first play
        event. An intentional walk HAS no play events, so it fell through to an
        empty dict and recorded every intentional walk with the bases empty. A
        tenth-inning walk under the ghost-runner rule exposed it: the real state
        was a runner on second, the recorded state was 0.

        Real 2024 states measured after the fix: 3B, 1B+3B, 2B+3B twice, and 2B.
        Never empty — an intentional walk with nobody on is nearly nonsensical.
        """
        rows = _extract(_play([], result_type="intent_walk"), outs=2, state=6)
        assert rows[0]["runners_state"] == 6
        assert rows[0]["outs_before"] == 2

    def test_its_play_index_cannot_collide_with_a_real_event(self):
        """There is no play event to key on, so the sentinel must be impossible."""
        rows = _extract(_play([], result_type="intent_walk"))
        assert rows[0]["play_index"] == -1


class TestTheSituationTravelsWithTheRow:
    def test_a_pitchless_play_still_gets_outs_and_base_state(self):
        rows = _extract(_play([_ev(0, "pickoff")]), outs=1, state=5)
        assert rows[0]["outs_before"] == 1
        assert rows[0]["runners_state"] == 5

    def test_an_event_with_its_own_offense_block_prefers_it(self):
        """A pickoff mid-plate-appearance knows the live base state itself."""
        rows = _extract(
            _play([_ev(0, "pickoff", offense={"first": {"id": 1}, "third": {"id": 3}})]),
            state=0,
        )
        assert rows[0]["runners_state"] == 5  # 1B + 3B

    def test_the_half_inning_is_recorded(self):
        top = _extract(_play([_ev(0, "pickoff")], half="top"))
        bot = _extract(_play([_ev(0, "pickoff")], half="bottom"))
        assert top[0]["inning_topbot"] == "Top"
        assert bot[0]["inning_topbot"] == "Bot"

    def test_the_batting_sides_score_is_the_bat_score(self):
        """Top of the inning: the AWAY team bats, so away is the batting score."""
        top = _extract(_play([_ev(0, "pickoff")], half="top"))
        assert top[0]["bat_score"] == 1
        assert top[0]["fld_score"] == 3


class TestMalformedInputIsSurvived:
    def test_an_event_with_no_index_is_skipped_not_crashed(self):
        play = _play([{"type": "pickoff", "isPitch": False, "details": {}}])
        assert _extract(play) == []

    def test_a_runner_with_no_playindex_is_ignored(self):
        runner = _run(0, "r_pickoff_1b")
        del runner["details"]["playIndex"]
        rows = _extract(_play([_ev(0, "pickoff")], runners=[runner]))
        assert rows[0]["is_out"] is False

    def test_missing_blocks_do_not_raise(self):
        assert extract_play_events({}, outs_before_play=0, runners_state_before=0, **_CTX) == []
