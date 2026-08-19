"""SIM-511/512 — the transition fielding draw + the five-scenario advancement
draws (the combined landing; owner rulings 2026-08-19).

Three layers, mirroring the seams:

  * the sampler (``FullPoolSampler``): the HARD base-out cell filter over
    consistent transition rows (24 cells, never widened — an empty cell
    raises), ``last_transition``, and the ``advancement_draw`` over the
    SIM-510 opportunity pools;
  * the normalization (``StateMachine._normalized_dests``): the five
    discretionary movements clamp to station-to-station (the double-count
    guard); everything else is row truth;
  * the loop wiring (``StateMachine._resolve_in_play_transition``) with a
    duck-typed sampler: the drawn row IS the play — outs and WHO is out come
    from the row (SIM-494/496 structural fix), the advancement draws are the
    sole authority on the five scenarios, Rule 5.08 tag timing, and the
    post-hoc ``sacrifice_fly`` relabel.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from pipeline.batch.engine_artifacts import (
    AdvancementPool,
    BattedBallPool,
    EngineArtifacts,
)
from simulation.full_pool_sampler import FullPoolSampler
from simulation.game_state import Bases, GameState, Half, PlayResult
from simulation.sim_loop import FieldingSignal, StateMachine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bb_pool(
    n: int,
    *,
    rs=0,
    outs=0,
    events="single",
    rh=1,
    ro=0,
    r1=-1,
    r2=-1,
    r3=-1,
    bd=1,
    adv1=0,
    adv2=0,
    adv3=0,
    is_air=0,
    dest_ok=1,
) -> BattedBallPool:
    """A transition-carrying batted-ball pool. Scalar args broadcast; list
    args set per-row values."""

    def arr(v, dtype):
        a = np.asarray(v if isinstance(v, list | tuple) else [v] * n, dtype=dtype)
        assert len(a) == n
        return a

    sit = np.zeros((n, 6), dtype=np.float32)
    sit[:, 2] = arr(outs, np.float32)
    sit[:, 3] = arr(rs, np.float32)
    ev_arr = np.asarray(events if isinstance(events, list | tuple) else [events] * n, dtype=object)
    return BattedBallPool(
        geom=np.zeros((n, 3), dtype=np.float32),
        sit=sit,
        batter_id=np.zeros(n, dtype=np.int64),
        season=np.full(n, 2024, dtype=np.int64),
        event=ev_arr,
        result_hits=arr(rh, np.int8),
        result_outs=arr(ro, np.int8),
        recency=np.ones(n, dtype=np.float32),
        r1_dest=arr(r1, np.int8),
        r2_dest=arr(r2, np.int8),
        r3_dest=arr(r3, np.int8),
        batter_dest=arr(bd, np.int8),
        dest_ok=arr(dest_ok, np.int8),
        r1_adv_out=arr(adv1, np.int8),
        r2_adv_out=arr(adv2, np.int8),
        r3_adv_out=arr(adv3, np.int8),
        is_air=arr(is_air, np.int8),
        spray_raw=np.zeros(n, dtype=np.float32),
        hit_dist=np.zeros(n, dtype=np.float32),
    )


def _adv_pool(n: int, attempted, safe=None, extra=None) -> AdvancementPool:
    att = np.asarray(attempted, dtype=np.int8)
    return AdvancementPool(
        feat=np.zeros((n, 5), dtype=np.float32),
        runner_id=np.full(n, 11, dtype=np.int64),
        fielder_id=np.full(n, 907, dtype=np.int64),
        fielder_pos=np.full(n, 8, dtype=np.int8),
        season=np.full(n, 2024, dtype=np.int64),
        attempted=att,
        safe=(np.asarray(safe, dtype=np.int8) if safe is not None else att.copy()),
        error_extra=(
            np.asarray(extra, dtype=np.int8) if extra is not None else np.zeros(n, dtype=np.int8)
        ),
        recency=np.ones(n, dtype=np.float32),
    )


def _fp(bb_pools=None, adv_pools=None, actor_emb=None, seed=7) -> FullPoolSampler:
    art = EngineArtifacts(
        {}, bb_pools=bb_pools or {}, adv_pools=adv_pools or {}, actor_emb=actor_emb or {}
    )
    return FullPoolSampler(art, np.random.default_rng(seed))


def _sit(balls=0, strikes=0, outs=0, rs=0, inning=1, sd=0) -> np.ndarray:
    return np.array([balls, strikes, outs, rs, inning, sd], dtype=np.float32)


# ---------------------------------------------------------------------------
# The sampler: the hard base-out cell + last_transition
# ---------------------------------------------------------------------------


class TestTheHardCellFilter:
    def test_the_draw_stays_inside_the_live_cell(self):
        # Rows split across two cells with different events; the draw must
        # never leave the live cell.
        pool = _bb_pool(
            200,
            rs=[1] * 100 + [0] * 100,
            events=["single"] * 100 + ["double"] * 100,
            r1=[2] * 100 + [-1] * 100,
        )
        fp = _fp({"R": pool})
        for _ in range(30):
            fp.battedball_new_pa("R", "9:2024", _sit(rs=1))
            ev, rh, _ro, _la = fp.battedball_draw()
            assert ev == "single"
            tr = fp.last_transition()
            assert tr is not None and tr["r1"] == 2

    def test_an_inconsistent_row_never_draws(self):
        pool = _bb_pool(
            100,
            rs=1,
            events=["single"] * 50 + ["double"] * 50,
            dest_ok=[1] * 50 + [0] * 50,
        )
        fp = _fp({"R": pool})
        for _ in range(30):
            fp.battedball_new_pa("R", "9:2024", _sit(rs=1))
            ev, *_ = fp.battedball_draw()
            assert ev == "single"

    def test_an_empty_cell_raises_never_widens(self):
        # Owner ruling 2026-08-19: the base-out filter is essential; an empty
        # cell is a data defect, not a widening opportunity.
        fp = _fp({"R": _bb_pool(10, rs=0)})
        with pytest.raises(RuntimeError, match="never widens"):
            fp.battedball_new_pa("R", "9:2024", _sit(rs=7, outs=2))

    def test_a_legacy_bundle_keeps_the_soft_draw(self):
        pool = BattedBallPool(
            geom=np.zeros((50, 3), dtype=np.float32),
            sit=np.zeros((50, 6), dtype=np.float32),
            batter_id=np.zeros(50, dtype=np.int64),
            season=np.full(50, 2024, dtype=np.int64),
            event=np.asarray(["single"] * 50, dtype=object),
            result_hits=np.ones(50, dtype=np.int8),
            result_outs=np.zeros(50, dtype=np.int8),
            recency=np.ones(50, dtype=np.float32),
        )
        fp = _fp({"R": pool})
        assert not fp.has_transition("R")
        fp.battedball_new_pa("R", "9:2024", _sit(rs=7, outs=2))  # no raise
        ev, *_ = fp.battedball_draw()
        assert ev == "single"
        assert fp.last_transition() is None


class TestTheAdvancementDraw:
    def test_the_attempt_rate_is_the_pools_own(self):
        # 30% of opportunity rows are attempts; a neutral draw must track it —
        # the non-attempt denominator working (the SIM-468 lesson).
        fp = _fp(adv_pools={"2_2_4": _adv_pool(1000, [1] * 300 + [0] * 700)})
        hits = 0
        for _ in range(2000):
            d = fp.advancement_draw(
                2,
                2,
                4,
                "11:2024",
                None,
                outs=0,
                exit_velo=0,
                launch_angle=0,
                spray_angle=0,
                hit_distance=0,
            )
            assert d is not None
            hits += int(d[0])
        assert hits / 2000 == pytest.approx(0.30, abs=0.04)

    def test_the_drawn_row_answers_the_whole_question(self):
        fp = _fp(adv_pools={"1_1_3": _adv_pool(20, [1] * 20, safe=[1] * 20, extra=[1] * 20)})
        d = fp.advancement_draw(
            1,
            1,
            3,
            "11:2024",
            None,
            outs=0,
            exit_velo=0,
            launch_angle=0,
            spray_angle=0,
            hit_distance=0,
        )
        assert d == (True, True, True)

    def test_an_absent_decision_returns_none(self):
        fp = _fp(adv_pools={"1_1_3": _adv_pool(10, [1] * 10)})
        assert (
            fp.advancement_draw(
                5,
                0,
                2,
                "9:2024",
                None,
                outs=0,
                exit_velo=0,
                launch_angle=0,
                spray_angle=0,
                hit_distance=0,
            )
            is None
        )
        assert not _fp().has_advancement()


# ---------------------------------------------------------------------------
# The normalization (the double-count guard)
# ---------------------------------------------------------------------------


def _tr(r1=-1, r2=-1, r3=-1, batter=1, adv1=False, adv2=False, adv3=False, is_air=False):
    return {
        "r1": r1,
        "r2": r2,
        "r3": r3,
        "batter": batter,
        "adv1": adv1,
        "adv2": adv2,
        "adv3": adv3,
        "is_air": is_air,
        "ev": 90.0,
        "spray": 0.0,
        "dist": 200.0,
    }


class TestNormalizedDests:
    nd = staticmethod(StateMachine._normalized_dests)

    def test_single_clamps_the_three_enumerated_movements(self):
        # 1st->3rd, 2nd->home, the stretch: all re-decided by the draws.
        d = self.nd(_tr(r1=3, r2=4, batter=2), hit=1)
        assert (d[1], d[2], d[0]) == (2, 3, 1)
        # A discretionary OUT (the advancing flag) is re-decided too.
        d = self.nd(_tr(r1=0, adv1=True, r2=0, adv2=True, batter=0), hit=1)
        assert (d[1], d[2], d[0]) == (2, 3, 1)

    def test_single_keeps_row_truth_outside_the_five(self):
        # A held runner on an infield single, a runner cut down at the plate
        # from 3B (not enumerated), and a non-advancing out all stand.
        d = self.nd(_tr(r1=1, r2=2, r3=0, adv3=True, batter=1), hit=1)
        assert (d[1], d[2], d[3]) == (1, 2, 0)

    def test_double_clamps_first_to_home_and_the_stretch(self):
        d = self.nd(_tr(r1=4, batter=3), hit=2)
        assert (d[1], d[0]) == (3, 2)
        d = self.nd(_tr(r1=0, adv1=True, batter=0), hit=2)
        assert (d[1], d[0]) == (3, 2)

    def test_air_out_strips_tags_but_keeps_doubled_off_runners(self):
        d = self.nd(_tr(r3=4, r2=3, r1=0, adv1=False, batter=0, is_air=True), hit=0)
        assert (d[3], d[2], d[1], d[0]) == (3, 2, 0, 0)

    def test_ground_out_is_row_truth(self):
        # Productive ground-out advancement is NOT enumerated: the row plays.
        d = self.nd(_tr(r2=3, r3=4, batter=0, is_air=False), hit=0)
        assert (d[2], d[3], d[0]) == (3, 4, 0)


# ---------------------------------------------------------------------------
# The loop wiring
# ---------------------------------------------------------------------------


class _FakeTransitionFP:
    """Duck-typed sampler exposing the SIM-512 seam. Records calls."""

    def __init__(self, adv: dict | None = None):
        self.adv = adv or {}
        self.calls: list[tuple] = []
        self.a = SimpleNamespace(bb_pools={"R": object()}, adv_pools=dict(self.adv))

    def has_advancement(self) -> bool:
        return bool(self.adv)

    def advancement_draw(self, scen, frm, tgt, runner_key, fielder_key, **kw):
        self.calls.append((scen, frm, tgt, runner_key))
        return self.adv.get((scen, frm, tgt))

    def last_battedball_fielder(self):
        return None


def _machine(adv=None):
    m = StateMachine(rng=np.random.default_rng(5))
    m.full_pool_sampler = _FakeTransitionFP(adv)
    return m


def _state(first=None, second=None, third=None, outs=0):
    s = GameState(pitcher_id=901, bat_hand="R", season=2024, batter_id=900)
    s.half = Half.TOP
    s.outs = outs
    s.bases = Bases(first=first, second=second, third=third)
    return s


def _resolve(m, s, *, event="single", rh=1, tr):
    result = PlayResult(pitch_outcome="in_play", is_contact=True)
    sig = FieldingSignal(
        event=event,
        result_hits=rh,
        result_outs=0,
        result_runs=0,
        launch_angle=20.0,
        is_error=(event == "field_error"),
        transition=tr,
    )
    pre_outs = int(s.outs)
    pre_bases = m._snapshot_bases(s)
    m._resolve_in_play_transition(s, result, sig, pre_outs, pre_bases)
    return result


class TestTheTransitionIsThePlay:
    def test_the_row_moves_every_body(self):
        m = _machine()
        s = _state(first=11, third=33)
        r = _resolve(m, s, tr=_tr(r1=2, r3=4, batter=1))
        assert (s.bases.first, s.bases.second, s.bases.third) == (900, 11, None)
        assert s.away_score == 1 and s.outs == 0
        assert r.baserunner_advances[33] == 0
        assert r.runs_scored == 1

    def test_a_double_play_retires_the_named_runner(self):
        # SIM-494 structurally fixed: two outs, two bodies gone — the runner
        # from 1B is REMOVED, not left standing.
        m = _machine()
        s = _state(first=11)
        r = _resolve(m, s, event="grounded_into_double_play", rh=0, tr=_tr(r1=0, batter=0))
        assert (s.bases.first, s.bases.second, s.bases.third) == (None, None, None)
        assert s.outs == 2 and r.outs_recorded == 2
        assert s.away_score == 0

    def test_a_reach_on_error_puts_the_batter_on_base(self):
        # SIM-496 structurally fixed: the batter REACHES and it is not a hit.
        m = _machine()
        s = _state()
        r = _resolve(m, s, event="field_error", rh=0, tr=_tr(batter=1))
        assert s.bases.first == 900 and s.outs == 0
        assert r.is_error and r.canonical_event == "field_error"

    def test_an_inning_ending_row_strands_the_survivors(self):
        m = _machine()
        s = _state(second=22, outs=2)
        _resolve(m, s, event="field_out", rh=0, tr=_tr(r2=2, batter=0, is_air=False))
        assert s.outs == 3 and s.away_score == 0


class TestTheAdvancementWiring:
    def test_the_lead_send_scores_and_the_trailer_advances(self):
        adv = {
            (2, 2, 4): (True, True, False),  # 2nd -> home: sent, safe
            (1, 1, 3): (True, True, False),  # 1st -> 3rd behind the throw
            (5, 0, 2): (False, False, False),  # the batter holds
        }
        m = _machine(adv)
        s = _state(first=11, second=22)
        r = _resolve(m, s, tr=_tr(r1=2, r2=3, batter=1))
        assert s.away_score == 1
        assert (s.bases.first, s.bases.second, s.bases.third) == (900, None, 11)
        assert r.baserunner_advances[22] == 0 and r.baserunner_advances[11] == 3

    def test_a_declined_lead_blocks_the_trailing_draw(self):
        adv = {
            (2, 2, 4): (False, False, False),  # the lead holds 3B
            (1, 1, 3): (True, True, False),  # must never be consulted
            (5, 0, 2): (False, False, False),
        }
        m = _machine(adv)
        s = _state(first=11, second=22)
        _resolve(m, s, tr=_tr(r1=2, r2=3, batter=1))
        scens = [c[0] for c in m.full_pool_sampler.calls]
        # No scenario-1 call after the lead declined; no stretch either —
        # the runner from 1B occupies 2B (can't-pass occupancy).
        assert scens == [2]
        assert (s.bases.first, s.bases.second, s.bases.third) == (900, 11, 22)

    def test_the_stretch_stands_alone_when_the_lead_declines(self):
        # Decision 5's exception: the batter-stretch draw fires even after
        # the lead declined — occupancy permitting (2B open here).
        adv = {
            (2, 2, 4): (False, False, False),  # the lead holds 3B
            (5, 0, 2): (True, True, False),  # the batter takes 2B anyway
        }
        m = _machine(adv)
        s = _state(second=22)
        _resolve(m, s, tr=_tr(r2=3, batter=1))
        scens = [c[0] for c in m.full_pool_sampler.calls]
        assert scens == [2, 5]
        assert (s.bases.first, s.bases.second, s.bases.third) == (None, 900, 22)

    def test_rule_508_a_run_before_the_trailing_tag_out_counts(self):
        adv = {
            (2, 2, 4): (True, True, False),  # the lead run crosses first
            (1, 1, 3): (True, False, False),  # the trailer is tagged: out 3
        }
        m = _machine(adv)
        s = _state(first=11, second=22, outs=2)
        r = _resolve(m, s, tr=_tr(r1=2, r2=3, batter=1))
        assert s.outs == 3
        assert s.away_score == 1  # the tag is not a force: the run stands
        assert r.runs_scored == 1

    def test_a_tag_from_third_relabels_the_sacrifice_fly(self):
        adv = {(4, 3, 4): (True, True, False)}
        m = _machine(adv)
        s = _state(third=33)
        # The row's sac fly was normalized away (r3 4 -> 3); the tag draw
        # re-decides it and the label follows the outcome.
        r = _resolve(m, s, event="sac_fly", rh=0, tr=_tr(r3=4, batter=0, is_air=True))
        assert r.event == "sacrifice_fly" and s.away_score == 1
        assert s.outs == 1 and s.bases.third is None

    def test_a_declined_tag_is_a_plain_fly_out(self):
        adv = {(4, 3, 4): (False, False, False)}
        m = _machine(adv)
        s = _state(third=33)
        r = _resolve(m, s, event="sac_fly", rh=0, tr=_tr(r3=4, batter=0, is_air=True))
        assert r.event == "field_out" and s.away_score == 0
        assert s.bases.third == 33 and s.outs == 1

    def test_the_batter_stretch_out_keeps_the_hit_and_records_the_out(self):
        adv = {(5, 0, 2): (True, False, False)}
        m = _machine(adv)
        s = _state()
        r = _resolve(m, s, tr=_tr(batter=1))
        assert r.event == "single"  # the hit stands in the box score
        assert s.outs == 1 and s.bases.first is None

    def test_no_advancement_pools_is_station_to_station(self):
        m = StateMachine(rng=np.random.default_rng(5))
        m.full_pool_sampler = _FakeTransitionFP()  # has_advancement() False
        s = _state(second=22)
        _resolve(m, s, tr=_tr(r2=3, batter=1))
        assert (s.bases.first, s.bases.second, s.bases.third) == (900, None, 22)
        assert s.away_score == 0
