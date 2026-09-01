"""
tests/unit/test_realism_consumers_sim411_413_425b.py
====================================================
SIM-411 / SIM-413 / SIM-425b — the realism CONSUMERS (flag-on mechanism tests).

The artifact-column plumbing is covered by
``test_engine_artifacts_realism_sim411_413_425b``; this module pins the consumer
behaviour that reads those columns, each behind its own env gate:

  * **SIM-413** — ``FullPoolSampler.battedball_new_pa(pitcher_throws=...)`` softly
    reweights the batted-ball draw toward the live pitcher-hand matchup.
  * **SIM-491/476** — the home / park / fielder DRAW-WEIGHT kernels
    (``bat_home`` match, run-factor Gaussian, live-defender OAA Gaussian) plus
    the ``last_battedball_fielder`` / ``fielder_quality`` accessors.

All are graceful-optional: with the knob at its off value / the data absent
they are a no-op. The SIM-425b post-draw fielder nudge and the SIM-411 park
flip were DELETED by SIM-476 (2026-08-30) — the fitted kernels replace them,
so their flip tests are gone with the code.
"""

from __future__ import annotations

import numpy as np
import pytest

from pipeline.batch.engine_artifacts import BattedBallPool, EngineArtifacts
from simulation.full_pool_sampler import FullPoolSampler
from simulation.game_state import GameState, Half
from simulation.sim_loop import StateMachine

_SEASON = 2024


class _FixedRNG:
    """A stand-in rng whose ``random()`` always returns ``v`` — lets a test force a
    flip (v small) or suppress it (v large) deterministically."""

    def __init__(self, v: float) -> None:
        self.v = float(v)

    def random(self) -> float:
        return self.v


# ===========================================================================
# SIM-413 — platoon reweight in the batted-ball draw (sampler level)
# ===========================================================================


def _platoon_bb_pool(with_throws: bool = True) -> BattedBallPool:
    """8 rows: the first 4 pitched by a RHP (event 'single'), the last 4 by a LHP
    (event 'double'), so the drawn EVENT reveals which hand the row came from."""
    n = 8
    p_throws = (
        np.asarray(["R", "R", "R", "R", "L", "L", "L", "L"], dtype=object) if with_throws else None
    )
    return BattedBallPool(
        geom=np.zeros((n, 3), dtype=np.float32),
        sit=np.zeros((n, 6), dtype=np.float32),
        batter_id=np.full(n, 700, dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        event=np.asarray(["single"] * 4 + ["double"] * 4, dtype=object),
        result_hits=np.array([1, 1, 1, 1, 2, 2, 2, 2], dtype=np.int8),
        result_outs=np.zeros(n, dtype=np.int8),
        recency=np.ones(n, dtype=np.float32),
        p_throws=p_throws,
    )


def _sampler(bb_pool: BattedBallPool, **kw) -> FullPoolSampler:
    art = EngineArtifacts(pools={}, bb_pools={"R": bb_pool})
    return FullPoolSampler(art, np.random.default_rng(0), **kw)


class TestPlatoonReweight:
    def test_off_weight_zero_draws_only_same_hand(self):
        # platoon_off_weight=0 => opposite-hand (LHP) rows get zero weight, so a
        # RHP matchup draws ONLY the 'single' (RHP) rows.
        fp = _sampler(_platoon_bb_pool(), platoon_off_weight=0.0)
        events = set()
        for _ in range(40):
            fp.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32), pitcher_throws="R")
            events.add(fp.battedball_draw()[0])
        assert events == {"single"}

    def test_neutral_when_no_pitcher_hand(self):
        # pitcher_throws=None => no reweight => both event types appear.
        fp = _sampler(_platoon_bb_pool(), platoon_off_weight=0.0)
        events = set()
        for _ in range(60):
            fp.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32), pitcher_throws=None)
            events.add(fp.battedball_draw()[0])
        assert events == {"single", "double"}

    def test_neutral_when_pool_has_no_p_throws(self):
        # A legacy pool (p_throws None) ignores the reweight even with a hand given.
        fp = _sampler(_platoon_bb_pool(with_throws=False), platoon_off_weight=0.0)
        events = set()
        for _ in range(60):
            fp.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32), pitcher_throws="R")
            events.add(fp.battedball_draw()[0])
        assert events == {"single", "double"}


# ===========================================================================
# SIM-491 — home-field reweight in the batted-ball draw (the SIM-412 rebuild)
# ===========================================================================


def _home_bb_pool(with_bat_home: bool = True) -> BattedBallPool:
    """8 rows: the first 4 hit in the HOME half (event 'single'), the last 4 in
    the away half (event 'double'), so the drawn EVENT reveals the batting side."""
    n = 8
    bat_home = np.array([1, 1, 1, 1, 0, 0, 0, 0], dtype=np.int8) if with_bat_home else None
    return BattedBallPool(
        geom=np.zeros((n, 3), dtype=np.float32),
        sit=np.zeros((n, 6), dtype=np.float32),
        batter_id=np.full(n, 700, dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        event=np.asarray(["single"] * 4 + ["double"] * 4, dtype=object),
        result_hits=np.array([1, 1, 1, 1, 2, 2, 2, 2], dtype=np.int8),
        result_outs=np.zeros(n, dtype=np.int8),
        recency=np.ones(n, dtype=np.float32),
        bat_home=bat_home,
    )


class TestHomeFieldReweight:
    def test_off_weight_zero_draws_only_matching_side(self):
        # home_off_weight=0 => mismatched-side rows get zero weight, so the home
        # offense draws ONLY the 'single' (home-half) rows.
        fp = _sampler(_home_bb_pool(), home_off_weight=0.0)
        events = set()
        for _ in range(40):
            fp.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32), bat_home=True)
            events.add(fp.battedball_draw()[0])
        assert events == {"single"}

    def test_away_offense_draws_only_away_rows(self):
        fp = _sampler(_home_bb_pool(), home_off_weight=0.0)
        events = set()
        for _ in range(40):
            fp.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32), bat_home=False)
            events.add(fp.battedball_draw()[0])
        assert events == {"double"}

    def test_neutral_when_bat_home_not_passed(self):
        fp = _sampler(_home_bb_pool(), home_off_weight=0.0)
        events = set()
        for _ in range(60):
            fp.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32))
            events.add(fp.battedball_draw()[0])
        assert events == {"single", "double"}

    def test_neutral_when_pool_has_no_bat_home(self):
        # A pre-0019 pool (bat_home None) ignores the reweight even when asked.
        fp = _sampler(_home_bb_pool(with_bat_home=False), home_off_weight=0.0)
        events = set()
        for _ in range(60):
            fp.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32), bat_home=True)
            events.add(fp.battedball_draw()[0])
        assert events == {"single", "double"}

    def test_default_weight_is_byte_identical(self):
        # home_off_weight=1.0 (the default) must not touch the weights at all:
        # the CDF with bat_home passed equals the CDF without it, bit for bit.
        fp_on = _sampler(_home_bb_pool())
        fp_off = _sampler(_home_bb_pool())
        fp_on.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32), bat_home=True)
        fp_off.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32))
        assert fp_on.home_off_weight == 1.0
        np.testing.assert_array_equal(fp_on._bb_cdf, fp_off._bb_cdf)


# ===========================================================================
# SIM-491 part 2 — the park KERNEL in the batted-ball draw (the SIM-411 rebuild)
# ===========================================================================


def _park_bb_pool(with_venues: bool = True) -> BattedBallPool:
    """8 rows: the first 4 in venue 15 (event 'single'), the last 4 in venue 16
    (event 'double'), so the drawn EVENT reveals the row's park."""
    n = 8
    venue_id = np.array([15, 15, 15, 15, 16, 16, 16, 16], dtype=np.int64) if with_venues else None
    return BattedBallPool(
        geom=np.zeros((n, 3), dtype=np.float32),
        sit=np.zeros((n, 6), dtype=np.float32),
        batter_id=np.full(n, 700, dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        event=np.asarray(["single"] * 4 + ["double"] * 4, dtype=object),
        result_hits=np.array([1, 1, 1, 1, 2, 2, 2, 2], dtype=np.int8),
        result_outs=np.zeros(n, dtype=np.int8),
        recency=np.ones(n, dtype=np.float32),
        venue_id=venue_id,
    )


_PARK_FACTORS = {(15, _SEASON): 1.15, (16, _SEASON): 0.85}


class TestParkKernel:
    def test_a_tight_kernel_draws_run_similar_parks(self):
        # A tight bandwidth with a live factor at venue 15's makes venue-16 rows
        # (0.30 away) vanish under the Gaussian.
        fp = _sampler(_park_bb_pool())
        fp.venue_run_factors = dict(_PARK_FACTORS)
        fp.park_sigma = 0.01
        events = set()
        for _ in range(40):
            fp.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32), park_run_factor=1.15)
            events.add(fp.battedball_draw()[0])
        assert events == {"single"}

    def test_neutral_when_no_factor_map(self):
        fp = _sampler(_park_bb_pool())
        fp.park_sigma = 0.01  # a map was never loaded -> neutral
        events = set()
        for _ in range(60):
            fp.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32), park_run_factor=1.15)
            events.add(fp.battedball_draw()[0])
        assert events == {"single", "double"}

    def test_neutral_when_pool_has_no_venue_id(self):
        fp = _sampler(_park_bb_pool(with_venues=False))
        fp.venue_run_factors = dict(_PARK_FACTORS)
        fp.park_sigma = 0.01
        events = set()
        for _ in range(60):
            fp.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32), park_run_factor=1.15)
            events.add(fp.battedball_draw()[0])
        assert events == {"single", "double"}

    def test_sigma_zero_is_byte_identical(self):
        # park_sigma=0.0 (the default) must not touch the weights at all: the
        # CDF with a park factor passed equals the CDF without it, bit for bit.
        fp_on = _sampler(_park_bb_pool())
        fp_on.venue_run_factors = dict(_PARK_FACTORS)
        fp_off = _sampler(_park_bb_pool())
        fp_on.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32), park_run_factor=1.15)
        fp_off.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32))
        assert fp_on.park_sigma == 0.0
        np.testing.assert_array_equal(fp_on._bb_cdf, fp_off._bb_cdf)

    def test_an_unmapped_season_falls_back_to_the_venue_mean(self):
        # The map holds only season 2023 for venue 15; a 2024 row reads the
        # venue mean instead of a neutral 1.0.
        fp = _sampler(_park_bb_pool())
        fp.venue_run_factors = {(15, 2023): 1.15, (16, _SEASON): 0.85}
        fp.park_sigma = 0.01
        events = set()
        for _ in range(40):
            fp.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32), park_run_factor=1.15)
            events.add(fp.battedball_draw()[0])
        assert events == {"single"}


# ===========================================================================
# SIM-491 part 3 — the fielder-quality KERNEL (the SIM-425b rebuild)
# ===========================================================================


def _fk_bb_pool(with_fielders: bool = True) -> BattedBallPool:
    """8 rows, all fielded at SS: the first 4 by the GOOD shortstop 555 (event
    'single'), the last 4 by the BAD shortstop 556 (event 'double'), so the
    drawn EVENT reveals which fielder's rows the kernel kept."""
    n = 8
    kw: dict = {}
    if with_fielders:
        kw = {
            "fielder_pos": np.full(n, 6, dtype=np.int8),
            "fielder_id": np.array([555, 555, 555, 555, 556, 556, 556, 556], dtype=np.int64),
        }
    return BattedBallPool(
        geom=np.zeros((n, 3), dtype=np.float32),
        sit=np.zeros((n, 6), dtype=np.float32),
        batter_id=np.full(n, 700, dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        event=np.asarray(["single"] * 4 + ["double"] * 4, dtype=object),
        result_hits=np.array([1, 1, 1, 1, 2, 2, 2, 2], dtype=np.int8),
        result_outs=np.zeros(n, dtype=np.int8),
        recency=np.ones(n, dtype=np.float32),
        **kw,
    )


def _fk_sampler(with_fielders: bool = True) -> FullPoolSampler:
    """A sampler whose fielder embedding holds the two pool shortstops (555
    OAA +10, 556 OAA −10) and the live shortstop 666 (OAA +10 — a twin of
    555)."""
    femb = {
        "key_index": {"555:SS:2024": 0, "556:SS:2024": 1, "666:SS:2024": 2},
        "vecs": np.array([[10.0], [-10.0], [10.0]], dtype=np.float32),
        "mean": np.zeros(1, dtype=np.float32),
        "std": np.ones(1, dtype=np.float32),
        "features": ["outs_above_average"],
    }
    art = EngineArtifacts(
        pools={}, bb_pools={"R": _fk_bb_pool(with_fielders)}, actor_emb={"fielder": femb}
    )
    return FullPoolSampler(art, np.random.default_rng(0))


_LIVE_DEFENSE = {"SS": 666}


class TestFielderQualityKernel:
    def test_a_tight_kernel_draws_similar_fielders(self):
        # The live SS (OAA +10) is a twin of 555 and far from 556, so a tight
        # bandwidth draws ONLY the 'single' (555) rows.
        fp = _fk_sampler()
        fp.fielder_sigma = 0.05
        events = set()
        for _ in range(40):
            fp.battedball_new_pa(
                "R",
                "700:2024",
                np.zeros(6, np.float32),
                defense_map=_LIVE_DEFENSE,
                live_season=_SEASON,
            )
            events.add(fp.battedball_draw()[0])
        assert events == {"single"}

    def test_neutral_when_no_defense_map(self):
        fp = _fk_sampler()
        fp.fielder_sigma = 0.05
        events = set()
        for _ in range(60):
            fp.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32))
            events.add(fp.battedball_draw()[0])
        assert events == {"single", "double"}

    def test_neutral_when_pool_has_no_fielder_columns(self):
        fp = _fk_sampler(with_fielders=False)
        fp.fielder_sigma = 0.05
        events = set()
        for _ in range(60):
            fp.battedball_new_pa(
                "R",
                "700:2024",
                np.zeros(6, np.float32),
                defense_map=_LIVE_DEFENSE,
                live_season=_SEASON,
            )
            events.add(fp.battedball_draw()[0])
        assert events == {"single", "double"}

    def test_sigma_zero_is_byte_identical(self):
        fp_on = _fk_sampler()
        fp_off = _fk_sampler()
        fp_on.battedball_new_pa(
            "R",
            "700:2024",
            np.zeros(6, np.float32),
            defense_map=_LIVE_DEFENSE,
            live_season=_SEASON,
        )
        fp_off.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32))
        assert fp_on.fielder_sigma == 0.0
        np.testing.assert_array_equal(fp_on._bb_cdf, fp_off._bb_cdf)

    def test_factor_does_not_move_balls_between_positions(self):
        """SIM-476: the fielder factor's mean is 1.0 WITHIN every position.

        Raw Gaussian weights let a well-matched position outweigh a position
        with an extreme live defender, so the draw shifted balls toward
        well-matched positions (the 2026-09-01 lane red: OF share of drawn
        balls 52.7% -> 57.4%, hits +6%). Per-position normalization pins the
        cross-position mass while keeping within-position discrimination."""
        n = 4
        pool = BattedBallPool(
            geom=np.zeros((n, 3), dtype=np.float32),
            sit=np.zeros((n, 6), dtype=np.float32),
            batter_id=np.full(n, 700, dtype=np.int64),
            season=np.full(n, _SEASON, dtype=np.int64),
            event=np.asarray(["single", "double", "triple", "field_out"], dtype=object),
            result_hits=np.array([1, 2, 3, 0], dtype=np.int8),
            result_outs=np.array([0, 0, 0, 1], dtype=np.int8),
            recency=np.ones(n, dtype=np.float32),
            # Two SS rows (a twin + a far fielder vs the live SS) and two CF
            # rows (both FAR from the live CF — the raw-weight failure case:
            # every CF weight is tiny, so the SS rows would swallow the draw).
            fielder_pos=np.array([6, 6, 8, 8], dtype=np.int8),
            fielder_id=np.array([555, 556, 777, 778], dtype=np.int64),
        )
        femb = {
            "key_index": {
                "555:SS:2024": 0,
                "556:SS:2024": 1,
                "666:SS:2024": 2,
                "777:CF:2024": 3,
                "778:CF:2024": 4,
                "888:CF:2024": 5,
                "999:CF:2024": 6,
            },
            "vecs": np.array(
                [[10.0], [-10.0], [10.0], [8.0], [9.0], [5.0], [-8.0]], dtype=np.float32
            ),
            "mean": np.zeros(1, dtype=np.float32),
            "std": np.ones(1, dtype=np.float32),
            "features": ["outs_above_average"],
        }
        art = EngineArtifacts(pools={}, bb_pools={"R": pool}, actor_emb={"fielder": femb})
        fp = FullPoolSampler(art, np.random.default_rng(0))
        fp.fielder_sigma = 0.5
        out = fp._f_live_fielder("R", np.arange(n), {"SS": 666, "CF": 888}, _SEASON)
        assert out is not None
        # Cross-position neutrality: mean factor 1.0 at BOTH positions, even
        # though every raw CF weight is astronomically smaller than the SS twin's.
        assert float(out[[0, 1]].mean()) == pytest.approx(1.0, rel=1e-5)
        assert float(out[[2, 3]].mean()) == pytest.approx(1.0, rel=1e-5)
        # Within-position discrimination survives: the twin outweighs the far
        # SS, and the closer CF (777 at 8.0 is nearer 5.0 than 778 at 9.0).
        assert out[0] > out[1]
        assert out[2] > out[3]
        # Full-underflow degenerate case: a live CF so extreme (-8 vs 8/9)
        # that every CF weight underflows to 0 in float32. The factor must go
        # NEUTRAL there — never 0, which would starve the position of balls.
        out2 = fp._f_live_fielder("R", np.arange(n), {"SS": 666, "CF": 999}, _SEASON)
        assert out2 is not None
        assert float(out2[2]) == 1.0 and float(out2[3]) == 1.0
        assert float(out2[[0, 1]].mean()) == pytest.approx(1.0, rel=1e-5)


# ===========================================================================
# SIM-425b — fielder accessors + the OAA nudge
# ===========================================================================


def _fielder_bb_pool() -> BattedBallPool:
    return BattedBallPool(
        geom=np.zeros((1, 3), dtype=np.float32),
        sit=np.zeros((1, 6), dtype=np.float32),
        batter_id=np.array([700], dtype=np.int64),
        season=np.array([_SEASON], dtype=np.int64),
        event=np.asarray(["single"], dtype=object),
        result_hits=np.array([1], dtype=np.int8),
        result_outs=np.array([0], dtype=np.int8),
        recency=np.ones(1, dtype=np.float32),
        fielder_pos=np.array([6], dtype=np.int8),  # SS
        fielder_id=np.array([555], dtype=np.int64),
    )


def _fielder_artifacts() -> EngineArtifacts:
    femb = {
        "key_index": {"555:SS:2024": 0, "666:SS:2024": 1},
        "vecs": np.array([[-5.0], [10.0]], dtype=np.float32),
        "mean": np.zeros(1, dtype=np.float32),
        "std": np.ones(1, dtype=np.float32),
        "features": ["outs_above_average"],
    }
    return EngineArtifacts(
        pools={}, bb_pools={"R": _fielder_bb_pool()}, actor_emb={"fielder": femb}
    )


class TestFielderAccessors:
    def test_last_battedball_fielder(self):
        fp = FullPoolSampler(_fielder_artifacts(), np.random.default_rng(0))
        fp.battedball_new_pa("R", "700:2024", np.zeros(6, np.float32))
        fp.battedball_draw()
        assert fp.last_battedball_fielder() == (6, 555, _SEASON)  # (pos, fid, pool season)

    def test_fielder_quality_reads_oaa_by_position_key(self):
        fp = FullPoolSampler(_fielder_artifacts(), np.random.default_rng(0))
        assert fp.fielder_quality(555, "SS", _SEASON) == -5.0
        assert fp.fielder_quality(666, "SS", _SEASON) == 10.0
        assert fp.fielder_quality(999, "SS", _SEASON) is None  # unknown
        assert fp.fielder_quality(555, "2B", _SEASON) is None  # wrong position


# ===========================================================================
# SIM-428 — the framing gate (SIM_FRAMING). Framing is ON by default but the
# defense-map fix activates it in production (catcher now resolves), so it gains
# an explicit, auditable off switch for byte-identical / reproducibility mode.
# ===========================================================================


class _FakeFramingFP:
    """A sampler whose catcher framing strongly steals strikes (delta 1.0)."""

    def catcher_framing(self, catcher_key: str) -> float:
        return 1.0


class TestFramingGate:
    def _state(self):
        # half=TOP => offense AWAY => _apply_framing reads home_catcher_id.
        return GameState(
            pitcher_id=1, bat_hand="R", season=_SEASON, half=Half.TOP, home_catcher_id=900
        )

    def test_framing_off_by_default(self):
        # SIM-517 (owner ruling 2026-08-29): the drawn row IS the play — no
        # post-draw adjustments. The flip defaults OFF; the catcher effect
        # returns as a pitch-draw weight (SIM-517).
        assert StateMachine()._framing is False

    def test_framing_on_flips_ball_to_called_strike(self):
        sm = StateMachine(full_pool_sampler=_FakeFramingFP())
        sm._framing = True
        sm.rng = _FixedRNG(0.0)  # type: ignore[assignment]
        assert sm._apply_framing(self._state(), "ball") == "called_strike"

    def test_framing_gate_off_is_noop(self):
        # SIM_FRAMING=0 short-circuits before any framing/rng draw.
        sm = StateMachine(full_pool_sampler=_FakeFramingFP())
        sm._framing = False
        sm.rng = _FixedRNG(0.0)  # type: ignore[assignment]
        assert sm._apply_framing(self._state(), "ball") == "ball"


if __name__ == "__main__":  # pragma: no cover
    import pytest

    pytest.main([__file__, "-v"])
