"""SIM-501a/c — the events-based out label, and the guard on `outs_on_pitch`.

Two DIFFERENT questions, two different sets:

1. "How many outs did the play record?" — PLAY_OUTS_BY_EVENT / FIELDING_OUT_EVENTS.
2. "Was the batter retired?" — BATTER_RETIRED_EVENTS.

The fielders-choice events answer them differently, which is the trap that
sank the first SIM-457 attempt. Every assertion here pins a fact MEASURED on
the live DB (2024 season) — the measurements are recorded in
pipeline/statcast_events.py and in the SIM-501 banner in BACKLOG.md.

The guard test proves the instrument can fail: re-adding a code read of
`outs_on_pitch` to the profile computor turns it red.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.statcast_events import (
    BATTER_RETIRED_EVENTS,
    FIELDING_OUT_EVENTS,
    IN_PLAY_TYPES,
    PLAY_OUTS_BY_EVENT,
    batter_retired,
    play_outs,
    sql_batter_retired,
    sql_fielding_out,
    sql_in_play,
    sql_outs_recorded,
    sql_play_outs,
)

_COMPUTOR = (
    Path(__file__).resolve().parents[2] / "pipeline" / "batch" / "player_profile_computor.py"
)

#: The events vocabulary measured over the swept seasons (32 values), plus
#: intent_walk / truncated_pa from the feed vocabulary. If a new season adds
#: an event, add it to PLAY_OUTS_BY_EVENT with a measured out count AND here.
MEASURED_VOCABULARY = {
    "field_out",
    "strikeout",
    "single",
    "walk",
    "double",
    "home_run",
    "force_out",
    "grounded_into_double_play",
    "hit_by_pitch",
    "sac_fly",
    "field_error",
    "triple",
    "sac_bunt",
    "double_play",
    "fielders_choice",
    "fielders_choice_out",
    "caught_stealing_2b",
    "strikeout_double_play",
    "catcher_interf",
    "other_out",
    "sac_fly_double_play",
    "caught_stealing_3b",
    "caught_stealing_home",
    "wild_pitch",
    "triple_play",
    "stolen_base_2b",
    "game_advisory",
    "sac_bunt_double_play",
    "passed_ball",
    "pickoff_caught_stealing_home",
    "stolen_base_3b",
    "stolen_base_home",
}


class TestVocabularyComplete:
    def test_every_measured_event_is_mapped(self):
        missing = MEASURED_VOCABULARY - PLAY_OUTS_BY_EVENT.keys()
        assert not missing, f"events with no out count: {sorted(missing)}"

    def test_no_alias_forms(self):
        """The vocabulary is the RAW Statcast form. The canonical aliases
        (sacrifice_fly, ground_into_double_play, ...) belong to
        simulation/constants.py, not here."""
        for alias in ("sacrifice_fly", "sacrifice_hit", "ground_into_double_play"):
            assert alias not in PLAY_OUTS_BY_EVENT


class TestTheFieldersChoiceTrap:
    """Measured 2024: fielders_choice rows are typed D/E only — never X —
    so NO out is recorded; fielders_choice_out records one (a runner), and
    the batter stands on 1B afterwards on 90.5% of rows."""

    def test_fielders_choice_records_no_out(self):
        assert PLAY_OUTS_BY_EVENT["fielders_choice"] == 0
        assert "fielders_choice" not in FIELDING_OUT_EVENTS

    def test_fielders_choice_out_records_one_out(self):
        assert PLAY_OUTS_BY_EVENT["fielders_choice_out"] == 1
        assert "fielders_choice_out" in FIELDING_OUT_EVENTS

    def test_batter_reaches_on_both(self):
        assert not batter_retired("fielders_choice")
        assert not batter_retired("fielders_choice_out")

    def test_force_out_retires_a_runner_not_the_batter(self):
        # Measured: batter on 1B after a force_out on 98.8% of rows.
        assert PLAY_OUTS_BY_EVENT["force_out"] == 1
        assert not batter_retired("force_out")


class TestOutCounts:
    def test_double_plays_record_two(self):
        for e in (
            "grounded_into_double_play",
            "double_play",
            "strikeout_double_play",
            "sac_fly_double_play",
            "sac_bunt_double_play",
        ):
            assert PLAY_OUTS_BY_EVENT[e] == 2, e

    def test_triple_play_records_three(self):
        assert PLAY_OUTS_BY_EVENT["triple_play"] == 3

    def test_strikeout_double_play_needs_no_steal_term(self):
        """Measured: sb_attempt_* is FALSE on every strikeout_double_play and
        caught_stealing_* row, so the events term carries BOTH outs and the
        caught-stealing term never double-counts."""
        assert PLAY_OUTS_BY_EVENT["strikeout_double_play"] == 2
        assert PLAY_OUTS_BY_EVENT["caught_stealing_2b"] == 1

    def test_hidden_runner_out_on_x_typed_reach(self):
        # Measured 2024: 290 singles and 66 doubles typed X — a runner was
        # thrown out advancing on the hit.
        assert play_outs("single", "X") == 1
        assert play_outs("single", "D") == 0
        assert play_outs("home_run") == 0

    def test_hidden_runner_out_is_closed_under_new_vocabulary(self):
        """type='X' MEANS "in play, out(s)" — so the hidden-out term is the
        COMPLEMENT of the fielding-out set, not a hand-kept whitelist. An
        event unknown to the vocabulary, typed X, still scores its out.
        (Verified equal to the old whitelist on 2024: 361 = 361 rows.)"""
        assert play_outs("some_future_event", "X") == 1
        assert play_outs("some_future_event", "D") == 0
        # An event that already accounts for its outs gains nothing from X.
        assert play_outs("field_out", "X") == 1
        assert play_outs("grounded_into_double_play", "X") == 2

    def test_null_safe(self):
        assert play_outs(None) == 0
        assert play_outs(float("nan")) == 0
        # X guarantees at least one out even when `events` is missing.
        assert play_outs(None, "X") == 1
        assert not batter_retired(None)


class TestQuestionSeparation:
    def test_batter_retired_is_a_strict_subset_of_fielding_out(self):
        assert BATTER_RETIRED_EVENTS < FIELDING_OUT_EVENTS
        # The difference is exactly the runner-out events.
        runner_outs = FIELDING_OUT_EVENTS - BATTER_RETIRED_EVENTS
        assert "force_out" in runner_outs
        assert "fielders_choice_out" in runner_outs
        assert "other_out" in runner_outs

    def test_every_batter_retired_event_records_an_out(self):
        for e in BATTER_RETIRED_EVENTS:
            assert PLAY_OUTS_BY_EVENT[e] >= 1, e


class TestSqlRendering:
    def test_qualifier_prefixes_every_column(self):
        sql = sql_outs_recorded("rp.")
        assert "rp.events" in sql and "rp.type" in sql and "rp.sb_attempt_2b" in sql
        # No unqualified bare references sneak in.
        assert " events " not in sql.replace("rp.events", "")

    def test_in_play_is_the_three_codes(self):
        assert IN_PLAY_TYPES == ("X", "D", "E")
        assert sql_in_play() == "type IN ('X', 'D', 'E')"

    def test_boolean_sets_render_their_members(self):
        assert "'fielders_choice_out'" in sql_fielding_out()
        assert "'fielders_choice'" not in sql_fielding_out().replace("'fielders_choice_out'", "")
        assert "'force_out'" not in sql_batter_retired()

    def test_play_outs_has_no_steal_term(self):
        assert "sb_attempt" not in sql_play_outs()
        assert "sb_attempt" in sql_outs_recorded()

    def test_cs_term_is_structurally_disjoint_from_the_events_term(self):
        """The caught-stealing term excludes rows whose `events` already
        counts a steal or pickoff out, so a feed change that started setting
        the sb columns on those rows could not double-count. (Measured
        anyway: zero overlap on all 3,211 such rows, 2017-2026.)"""
        from pipeline.statcast_events import sql_cs_out, sql_event_outs

        for e in ("caught_stealing_2b", "pickoff_caught_stealing_home", "strikeout_double_play"):
            # Counted once by the events term ...
            assert f"'{e}'" in sql_event_outs(), e
            # ... and excluded by the CS guard's NOT IN list.
            assert f"'{e}'" in sql_cs_out(), e
        assert "NOT IN" in sql_cs_out()

    def test_in_play_set_matches_the_pool_build(self):
        """The pool build's outcome_type CASE is the classification the
        simulator draws from; the in-play row filter must be the same set
        (the SIM-456 lesson, applied to the third code set)."""
        from tests.unit.test_sim501_profile_code_sets import _pool_build_codes

        assert set(IN_PLAY_TYPES) == _pool_build_codes("in_play")


def _offending_reads(source: str) -> list[str]:
    """Lines that READ `outs_on_pitch` — comment mentions excluded."""
    offending = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        if "outs_on_pitch" not in line:
            continue
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("--"):
            continue
        offending.append(f"{lineno}: {stripped}")
    return offending


class TestComputorReadsNoOutsOnPitch:
    """The SIM-501c instrument. `outs_on_pitch` is broken in the swept data
    (zero on 92.6% of batted-ball outs); no profile-computor site may read
    it. Comment mentions are allowed; code is not."""

    def test_no_code_reads_outs_on_pitch(self):
        offending = _offending_reads(_COMPUTOR.read_text(encoding="utf-8"))
        assert not offending, (
            "player_profile_computor.py reads outs_on_pitch — derive the out "
            "label from `events` instead (pipeline/statcast_events.py):\n" + "\n".join(offending)
        )

    def test_guard_can_fail(self):
        """The REAL detector, run over a source with one offending line
        between two allowed comment mentions — it must flag exactly the
        read. This is the same function the guard above runs, so a broken
        detector fails here, not silently there."""
        source = (
            "# outs_on_pitch is broken\n"
            "    SUM(outs_on_pitch) AS outs_recorded,\n"
            "    -- outs_on_pitch comment mention\n"
        )
        offending = _offending_reads(source)
        assert len(offending) == 1
        assert offending[0].startswith("2:")
        assert "SUM(outs_on_pitch)" in offending[0]


class TestSimLoopDivergencePinned:
    """simulation/sim_loop.py `_OUT_EVENTS` is the loop's "did the batter
    reach" test and lists BOTH fielders-choice events as outs. Measured
    2024: the batter REACHES on both, and fielders_choice records no out at
    all. Reconciling the loop is sim-scoped work (the SIM-473/SIM-499
    redesign) with regression-fixture impact, so it is pinned here rather
    than changed. When the loop is fixed, this test fails on purpose —
    update it to pin the reconciled state."""

    def test_known_divergence(self):
        from simulation.sim_loop import _OUT_EVENTS

        assert "fielders_choice" in _OUT_EVENTS
        assert "fielders_choice_out" in _OUT_EVENTS
        assert not batter_retired("fielders_choice")
        assert PLAY_OUTS_BY_EVENT["fielders_choice"] == 0
