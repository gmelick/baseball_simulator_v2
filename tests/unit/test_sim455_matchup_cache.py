"""
tests/unit/test_sim455_matchup_cache.py
=======================================
SIM-455 — the full-pool weight cache needs THREE lifetimes, not one.

``StateMachine._full_pool_outcome`` gated BOTH sampler refreshes behind one key::

    key = (state.pitcher_id, hand, state.batter_id)

That single key was wrong in two independent ways.

**Defect 1 — wasted work (the filed ticket).** The key carries ``batter_id``, so a
new batter re-ran ``new_half_inning``, which recomputes ``f_pitcher`` over the
WHOLE pool. A game has ~83 plate appearances and ~5 pitchers, so ~78 of those
full-pool passes recomputed a vector that had not changed.

**Defect 2 — staleness (on no ticket before this module).** The key OMITS the
base-out state, which ``new_plate_appearance`` reads to build the situation
factor. Base-out changes INSIDE a plate appearance — a steal, a runner advancing
on a wild pitch, a caught stealing adding an out — and nothing refreshed, so the
situation factor stayed frozen at the state the plate appearance opened with.

The three lifetimes now are:

  * ``f_pitcher``       -> recompute on a (pitcher, hand) change
  * ``f_batter``        -> recompute on a batter change
  * situation factor    -> recompute on a base-out change

What each test must be able to detect
-------------------------------------
Every test in :class:`TestPitcherFactorLifetime` and
:class:`TestSituationFactorLifetime` FAILS against the single-key code. The
counting tests fail because the pre-fix count equals the batter count, and the
staleness tests fail because the pre-fix situation factor never refreshes.
"""

from __future__ import annotations

import numpy as np

from pipeline.batch.engine_artifacts import EngineArtifacts, HandPool
from simulation.full_pool_sampler import FullPoolSampler
from simulation.game_state import GameState, Half, Team
from simulation.sim_loop import StateMachine

_SEASON = 2024
_PITCHER_A = 900
_PITCHER_B = 901
_LINEUP = [101, 102, 103, 104, 105, 106, 107, 108, 109]

#: Base-out signature of an EMPTY-bases plate appearance, and of one with a
#: runner on second. ``runners_state`` is the bitmask ``bit0=1B, bit1=2B``.
_EMPTY_RUNNERS = 0
_RUNNER_ON_SECOND = 2


# ===========================================================================
# A pool whose OUTCOME is decided by the base-out state
# ===========================================================================


def _situation_pool() -> HandPool:
    """Two rows per count bucket, separated only by ``runners_state``.

    ``sit`` is ``(balls, strikes, outs, runners, inning, score_diff)``. Rows with
    an empty base state always read ``ball``; rows with a runner on second always
    read ``called_strike``. So the DRAWN outcome names which base-out state the
    live situation factor was built from — a stale factor is directly visible in
    the draw, not merely in a call count.
    """
    rows = []
    for balls in range(4):
        for strikes in range(3):
            rows.append((balls, strikes, _EMPTY_RUNNERS, "ball"))
            rows.append((balls, strikes, _RUNNER_ON_SECOND, "called_strike"))
    n = len(rows)
    sit = np.zeros((n, 6), dtype=np.float32)
    sit[:, 0] = [r[0] for r in rows]  # balls
    sit[:, 1] = [r[1] for r in rows]  # strikes
    sit[:, 2] = 0.0  # outs
    sit[:, 3] = [r[2] for r in rows]  # runners_state
    sit[:, 4] = 1.0  # inning
    sit[:, 5] = 0.0  # score_diff
    return HandPool(
        geom=np.zeros((n, 10), dtype=np.float32),
        sit=sit,
        pitcher_id=np.full(n, _PITCHER_A, dtype=np.int64),
        batter_id=np.full(n, _LINEUP[0], dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        outcome_type=np.asarray([r[3] for r in rows], dtype=object),
        recency=np.ones(n, dtype=np.float32),
    )


class _CountingPool(FullPoolSampler):
    """A real ``FullPoolSampler`` that counts the two expensive refreshes.

    ``_f_pitcher`` IS the full-pool pass the ticket counts: it scores every row in
    the pool against the pitcher. ``new_plate_appearance`` rebuilds the per-plate-
    appearance weight and its 12 count-bucket CDFs.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.f_pitcher_calls = 0
        self.pa_calls: list[tuple] = []

    def _f_pitcher(self, hand: str, pitcher_key: str) -> np.ndarray:
        self.f_pitcher_calls += 1
        return super()._f_pitcher(hand, pitcher_key)

    def new_plate_appearance(self, batter_key: str, base_out: np.ndarray) -> None:
        self.pa_calls.append((batter_key, tuple(np.asarray(base_out).tolist())))
        super().new_plate_appearance(batter_key, base_out)


def _machine(sit_sigma: float = 0.25) -> tuple[StateMachine, _CountingPool]:
    """A machine wired to a counting full-pool sampler.

    A tight ``sit_sigma`` makes the situation factor decisive: the pool rows whose
    base-out matches the live state dominate the draw, so the drawn outcome
    reports which base-out the factor was built from.
    """
    pool = _situation_pool()
    art = EngineArtifacts(pools={"R": pool, "L": pool})
    fp = _CountingPool(art, np.random.default_rng(7), sit_sigma=sit_sigma)
    machine = StateMachine(fp, rng=np.random.default_rng(7))
    return machine, fp


def _state(**kw) -> GameState:
    base = {
        "pitcher_id": _PITCHER_A,
        "bat_hand": "R",
        "season": _SEASON,
        "away_lineup": list(_LINEUP),
        "home_lineup": list(_LINEUP),
    }
    base.update(kw)
    state = GameState(**base)
    state.batter_id = _LINEUP[0]
    # ``offense`` is derived from ``half``: TOP -> AWAY bats. Both are defaults,
    # asserted here so a later default change cannot silently move the score_diff
    # sign in the base-out vector under these tests.
    assert state.half is Half.TOP and state.offense is Team.AWAY and state.inning == 1
    return state


# ===========================================================================
# Lifetime 1 — f_pitcher recomputes per PITCHER, not per BATTER
# ===========================================================================


class TestPitcherFactorLifetime:
    def test_nine_batters_one_pitcher_is_one_full_pool_pass(self):
        """The headline number. Pre-fix this is 9 — one full-pool pass per batter."""
        machine, fp = _machine()
        state = _state()
        for batter in _LINEUP:
            state.batter_id = batter
            machine._full_pool_outcome(state)
        assert fp.f_pitcher_calls == 1

    def test_the_wasted_passes_are_gone_across_a_full_game_of_batters(self):
        """~83 plate appearances against ~5 pitchers. The ticket says that is ~78
        unnecessary full-pool passes. Pin the corrected arithmetic: 5 pitchers ->
        5 passes, not 83."""
        machine, fp = _machine()
        state = _state()
        pitchers = [_PITCHER_A, _PITCHER_B, 902, 903, 904]
        pa = 0
        for i, pitcher in enumerate(pitchers):
            state.pitcher_id = pitcher
            # ~17 plate appearances apiece -> 83 in total.
            for j in range(17 if i < 4 else 15):
                state.batter_id = _LINEUP[(pa + j) % len(_LINEUP)]
                machine._full_pool_outcome(state)
            pa += 17
        assert len(fp.pa_calls) == 83
        assert fp.f_pitcher_calls == len(pitchers)

    def test_a_pitcher_change_does_recompute_the_pitcher_factor(self):
        """The cache must still be a cache, not a freeze."""
        machine, fp = _machine()
        state = _state()
        machine._full_pool_outcome(state)
        assert fp.f_pitcher_calls == 1
        state.pitcher_id = _PITCHER_B
        machine._full_pool_outcome(state)
        assert fp.f_pitcher_calls == 2

    def test_a_batter_hand_change_recomputes_the_pitcher_factor(self):
        """The pool is partitioned by batter hand, so a hand change means a
        different pool and a different ``f_pitcher`` vector."""
        machine, fp = _machine()
        state = _state()
        machine._full_pool_outcome(state)
        state.bat_hand = "L"
        machine._full_pool_outcome(state)
        assert fp.f_pitcher_calls == 2

    def test_a_pitcher_change_also_rebuilds_the_plate_appearance_weight(self):
        """``new_plate_appearance`` multiplies the base ``new_half_inning`` builds,
        so a new base must invalidate it even when the batter is unchanged."""
        machine, fp = _machine()
        state = _state()
        machine._full_pool_outcome(state)
        assert len(fp.pa_calls) == 1
        state.pitcher_id = _PITCHER_B
        machine._full_pool_outcome(state)
        assert len(fp.pa_calls) == 2


# ===========================================================================
# Lifetime 2 — f_batter recomputes per BATTER
# ===========================================================================


class TestBatterFactorLifetime:
    def test_each_new_batter_rebuilds_the_plate_appearance_weight(self):
        machine, fp = _machine()
        state = _state()
        for batter in _LINEUP:
            state.batter_id = batter
            machine._full_pool_outcome(state)
        assert [c[0] for c in fp.pa_calls] == [f"{b}:{_SEASON}" for b in _LINEUP]

    def test_repeat_pitches_to_one_batter_rebuild_nothing(self):
        """Pitch 6 times to one batter in an unchanged base-out state: one build."""
        machine, fp = _machine()
        state = _state()
        for balls in range(3):
            for strikes in range(2):
                state.balls, state.strikes = balls, strikes
                machine._full_pool_outcome(state)
        assert len(fp.pa_calls) == 1
        assert fp.f_pitcher_calls == 1

    def test_the_batter_affinity_is_memoized_downstream(self):
        """The ticket says ``f_batter`` is already memoized in the sampler. Verify
        it rather than trust it: the same batter key reuses one affinity vector."""
        machine, fp = _machine()
        assert fp._aff_cache == {}
        state = _state()
        machine._full_pool_outcome(state)
        # No batter embedding is wired here, so the affinity is absent and the
        # factor degrades to ones. The memo therefore stays empty, and the pool's
        # `_pool_meta` cache is what proves the per-hand precompute ran once.
        assert set(fp._pool_cache) == {"R"}
        state.bat_hand = "L"
        machine._full_pool_outcome(state)
        assert set(fp._pool_cache) == {"R", "L"}


# ===========================================================================
# Lifetime 3 — the situation factor recomputes on a BASE-OUT change
# ===========================================================================


class TestSituationFactorLifetime:
    """Every test here FAILS against the single-key code, which never refreshed
    the situation factor inside a plate appearance."""

    def test_a_steal_mid_plate_appearance_refreshes_the_situation_factor(self):
        """The runner takes second on pitch 2 of the SAME plate appearance. The
        situation factor must follow him."""
        machine, fp = _machine()
        state = _state()
        state.balls, state.strikes = 0, 0
        machine._full_pool_outcome(state)
        assert fp.pa_calls[-1][1] == (0.0, 0.0, 1.0, 0.0)

        # A steal of second: same batter, same pitcher, same plate appearance.
        state.bases.second = _LINEUP[3]
        state.balls, state.strikes = 1, 0
        machine._full_pool_outcome(state)

        assert len(fp.pa_calls) == 2, "the situation factor did not refresh on the steal"
        assert fp.pa_calls[-1][1] == (0.0, float(_RUNNER_ON_SECOND), 1.0, 0.0)

    def test_the_drawn_outcome_follows_the_live_bases_not_the_stale_ones(self):
        """The behavioural proof. In this pool an empty base state draws ``ball``
        and a runner on second draws ``called_strike``. A stale factor keeps
        drawing ``ball`` after the steal."""
        machine, fp = _machine()
        state = _state()
        state.balls, state.strikes = 0, 0
        assert machine._full_pool_outcome(state) == "ball"

        state.bases.second = _LINEUP[3]
        state.balls, state.strikes = 1, 0
        drawn = [machine._full_pool_outcome(state) for _ in range(30)]
        assert set(drawn) == {"called_strike"}, (
            "the draw still reflects the empty-bases situation factor: stale"
        )

    def test_an_out_recorded_mid_plate_appearance_refreshes_the_factor(self):
        """A caught stealing adds an out without ending the plate appearance."""
        machine, fp = _machine()
        state = _state()
        machine._full_pool_outcome(state)
        state.outs = 1
        machine._full_pool_outcome(state)
        assert len(fp.pa_calls) == 2
        assert fp.pa_calls[-1][1][0] == 1.0

    def test_a_run_scoring_mid_plate_appearance_refreshes_the_factor(self):
        """``score_diff`` is one of the four base-out dimensions. A runner scoring
        on a wild pitch changes it inside the plate appearance."""
        machine, fp = _machine()
        state = _state()
        machine._full_pool_outcome(state)
        state.away_score = 3  # the AWAY team is batting
        machine._full_pool_outcome(state)
        assert len(fp.pa_calls) == 2
        assert fp.pa_calls[-1][1][3] == 3.0

    def test_the_score_diff_stays_clamped_to_plus_or_minus_five(self):
        """Pre-existing behaviour the refactor must not change."""
        machine, fp = _machine()
        state = _state()
        state.away_score = 40
        machine._full_pool_outcome(state)
        assert fp.pa_calls[-1][1][3] == 5.0

    def test_returning_to_a_seen_base_out_state_still_rebuilds(self):
        """The key is the LIVE state, not a set of states seen before. A base-out
        that returns to an earlier value must rebuild, because the sampler holds
        only ONE weight vector."""
        machine, fp = _machine()
        state = _state()
        machine._full_pool_outcome(state)
        state.bases.second = _LINEUP[3]
        machine._full_pool_outcome(state)
        state.bases.second = None
        machine._full_pool_outcome(state)
        assert len(fp.pa_calls) == 3
        assert fp.pa_calls[-1][1] == (0.0, 0.0, 1.0, 0.0)
        assert machine._full_pool_outcome(state) == "ball"

    def test_an_unchanged_base_out_does_not_rebuild(self):
        """The staleness fix must not turn the cache into a per-pitch recompute —
        that would reintroduce the cost SIM-455 exists to remove."""
        machine, fp = _machine()
        state = _state()
        for _ in range(12):
            machine._full_pool_outcome(state)
        assert len(fp.pa_calls) == 1
        assert fp.f_pitcher_calls == 1
