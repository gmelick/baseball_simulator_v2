"""
tests/unit/test_sim427_relief_draw.py
=====================================
SIM-427 — each team's real manager for the pitching change (plan:
docs/audit/2026-09-13-sim427-build-plan.md, parts 4c-4e).

What these tests pin:

  * the manager weight on the change draw (``_change_manager_factor``): power
    0 leaves the draw byte-identical; a quick-hook manager's usage row moves
    the draw toward the rows his look-alikes changed; an unscored row is
    draw-neutral at the scored rows' mean; no matrix, no column or an unknown
    live manager leaves the factor off;
  * the reliever draw from the live pen (``relief_arm_draw``): every weight
    off -> None (the positional pick stands); the role, rest, pitched-in-the-
    last-two-days, recent-pitches, hand and stuff weights each prefer the
    candidate that resembles the arm the drawn row brought in; a candidate
    without the fact is neutral; an empty pen or no drawn row -> None; the
    loop pops the drawn arm;
  * the steal weight (``_steal_aggression``): the batting side's measured
    rate over the league mean, clamped, neutral without a profile or a league
    row or a manager;
  * the no-DB change pool (``synthetic_bundle.change_pool``): the rates per
    cell, the SIM-427 columns, and the league bundle's manager switch;
  * the resolver's pen attachment (``_attach_bullpens``): off by default
    (``SIM_BULLPEN_SOURCE=synthetic``, production today); with ``box`` both
    sides come from the listing with rest, usage and the pen arms' hands; a
    missing listing or a failed read leaves the synthetic marker;
  * the profile resolver (``resolve_manager_profiles_onto_state``): the
    season, the season-before fallback, the league row, and an empty dict on
    every failure.
"""

from __future__ import annotations

import asyncio
from datetime import date

import numpy as np
import pytest

from pipeline.batch.engine_artifacts import ChangePool, EngineArtifacts
from simulation.full_pool_sampler import FullPoolSampler
from simulation.game_state import GameState, Half, Team
from simulation.lineup_resolver import (
    LineupSlot,
    ResolvedLineup,
    TeamLineup,
    _attach_bullpens,
    bullpen_source_enabled,
)
from simulation.sim_kwargs import (
    MANAGER_TENDENCY_COLUMNS,
    resolve_manager_profiles_onto_state,
    sim_kwargs_from_state,
)
from simulation.sim_loop import StateMachine
from simulation.synthetic_bundle import DEFAULT_CHANGE_RATES, change_pool, league_artifacts

_SEASON = 2024

# ===========================================================================
# Fixtures: a change pool with two managers, an incoming arm per row
# ===========================================================================


def _pool(rows: list[tuple]) -> ChangePool:
    """rows: (changed, manager_id, incoming_id, in_days_rest, in_pitched_2d,
    in_pitches_3d, in_throws). Every row sits in ONE hard cell (a starter, a
    half-inning boundary, 90 pitches, third time through) with the same soft
    columns, so only the manager factor tells the rows apart."""
    n = len(rows)
    return ChangePool(
        sit=np.tile(np.asarray([90, 20, 7, 0, 0, 1], dtype=np.float32), (n, 1)),
        pitcher_id=np.full(n, 100, dtype=np.int64),
        incoming_id=np.asarray([r[2] for r in rows], dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        is_starter=np.ones(n, dtype=np.int8),
        new_half=np.ones(n, dtype=np.int8),
        changed=np.asarray([r[0] for r in rows], dtype=np.int8),
        recency=np.ones(n, dtype=np.float32),
        manager_id=np.asarray([r[1] for r in rows], dtype=np.int64),
        in_days_rest=np.asarray([r[3] for r in rows], dtype=np.int8),
        in_pitched_2d=np.asarray([r[4] for r in rows], dtype=np.int8),
        in_pitches_3d=np.asarray([r[5] for r in rows], dtype=np.int16),
        in_throws=np.asarray([r[6] for r in rows], dtype=np.int8),
    )


#: Manager 1 (quick hook) changed at every one of his four boundaries; manager 2
#: (long leash) changed at none of his four; manager -1 = unknown (two rows, one
#: changed). The incoming arm is 501 on every changed row.
_ROWS = (
    [(1, 1, 501, 5, 0, 0, 2)] * 4
    + [(0, 2, 0, -1, -1, -1, 0)] * 4
    + [(1, -1, 501, 5, 0, 0, 2), (0, -1, 0, -1, -1, -1, 0)]
)

#: The manager-usage score matrix: the live manager 10 resembles the quick
#: hook (0.9) and not the long leash (0.1); manager 11 the reverse.
_MGR_INDEX = {"1:2024": 0, "2:2024": 1, "10:2024": 2, "11:2024": 3}
_MGR_MATRIX = np.asarray(
    [
        [1.0, 0.0, 0.9, 0.1],
        [0.0, 1.0, 0.1, 0.9],
        [0.9, 0.1, 1.0, 0.0],
        [0.1, 0.9, 0.0, 1.0],
    ],
    dtype=np.float32,
)


def _sampler(
    pool: ChangePool | None = None,
    *,
    seed: int = 0,
    manager_power: float | None = None,
    matrix: bool = True,
    roles=None,
    pitcher_sim=None,
    index=None,
) -> FullPoolSampler:
    art = EngineArtifacts(
        pools={},
        pitcher_sim=pitcher_sim or {},
        pitcher_sim_index=index or {},
        change_pool=_pool(_ROWS) if pool is None else pool,
        actor_sim={"manager_usage": {"index": _MGR_INDEX, "matrix": _MGR_MATRIX}} if matrix else {},
        change_roles=roles,
    )
    fp = FullPoolSampler(art, np.random.default_rng(seed))
    fp.change_min_cell = 1
    if manager_power is not None:
        fp.actor_power["manager_usage"] = float(manager_power)
    return fp


def _draw_share(fp: FullPoolSampler, manager_key: str | None, n: int = 400) -> float:
    hits = 0
    for _ in range(n):
        out = fp.pitching_change_draw(
            "100:2024",
            is_starter=True,
            new_half=True,
            pitch_count=90,
            batters_faced=20,
            inning=7,
            outs=0,
            runners_state=0,
            score_diff=1,
            manager_key=manager_key,
        )
        hits += 1 if out else 0
    return hits / n


# ===========================================================================
# The manager weight on the change draw
# ===========================================================================


class TestManagerFactor:
    def test_power_zero_is_off_and_byte_identical(self):
        # The default power is 0: the factor returns None and the draw sequence
        # equals the one with no manager key at all.
        fp = _sampler(seed=3)
        meta = fp._change_meta()
        assert fp._change_manager_factor(meta, np.arange(10), "10:2024") is None
        a = [_draw_share(_sampler(seed=s), "10:2024", n=40) for s in range(3)]
        b = [_draw_share(_sampler(seed=s), None, n=40) for s in range(3)]
        assert a == b

    def test_the_factor_gathers_the_live_managers_row(self):
        fp = _sampler(manager_power=1.0)
        meta = fp._change_meta()
        f = fp._change_manager_factor(meta, np.arange(10), "10:2024")
        assert f is not None and f.shape == (10,)
        # rows 0-3 belong to manager 1 (score 0.9), rows 4-7 to manager 2 (0.1)
        assert np.allclose(f[:4], 0.9) and np.allclose(f[4:8], 0.1)
        # the two unknown-manager rows sit at the scored rows' mean (draw-neutral)
        assert np.allclose(f[8:], 0.5)

    def test_the_power_sharpens_the_row(self):
        fp = _sampler(manager_power=2.0)
        f = fp._change_manager_factor(fp._change_meta(), np.arange(8), "10:2024")
        assert np.allclose(f[:4], 0.81) and np.allclose(f[4:], 0.01)

    def test_a_quick_hook_manager_changes_more_often_than_a_long_leash(self):
        # Without the weight the pool says 50% (5 changed rows of 10). With it,
        # the live manager who resembles the quick hook draws his rows.
        flat = _draw_share(_sampler(seed=1), None)
        quick = _draw_share(_sampler(seed=1, manager_power=1.0), "10:2024")
        leash = _draw_share(_sampler(seed=1, manager_power=1.0), "11:2024")
        assert 0.40 < flat < 0.60
        assert quick > 0.75 and leash < 0.25

    def test_no_matrix_no_column_or_an_unknown_manager_leaves_the_factor_off(self):
        fp = _sampler(manager_power=1.0, matrix=False)
        assert fp._change_manager_factor(fp._change_meta(), np.arange(10), "10:2024") is None
        # a pool built before the manager column
        old = _pool(_ROWS)
        old.manager_id = None
        fp2 = _sampler(old, manager_power=1.0)
        assert fp2._change_manager_factor(fp2._change_meta(), np.arange(10), "10:2024") is None
        # a live manager the matrix does not know
        fp3 = _sampler(manager_power=1.0)
        assert fp3._change_manager_factor(fp3._change_meta(), np.arange(10), "99:2024") is None
        # and the draw itself still works (neutral) in each case
        assert 0.35 < _draw_share(fp3, "99:2024") < 0.65

    def test_the_effective_sample_share_falls_as_the_power_concentrates_the_draw(self):
        # The starvation guard: ten alike rows draw at share 1.0 with the
        # weight off; at power 8 the quick hook's four rows carry almost all
        # of the weight, so the share falls toward 4/10.
        fp = _sampler(seed=5)
        _draw_share(fp, None, n=5)
        assert fp.change_ess_stats[1] == 5 and fp.change_ess_stats[0] / 5 == pytest.approx(1.0)
        fp8 = _sampler(seed=5, manager_power=8.0)
        _draw_share(fp8, "10:2024", n=5)
        share = fp8.change_ess_stats[0] / fp8.change_ess_stats[1]
        assert 0.4 < share < 0.6

    def test_the_last_row_carries_the_sim427_facts(self):
        fp = _sampler(manager_power=1.0, seed=2)
        assert _draw_share(fp, "10:2024", n=1) == 1.0  # the quick hook's rows: changed
        row = fp.last_change_row()
        assert row["changed"] is True and row["incoming_id"] == 501
        assert row["manager_id"] in (1, -1)
        assert (row["in_days_rest"], row["in_pitched_2d"], row["in_pitches_3d"]) == (5, 0, 0)
        assert row["in_throws"] == 2


# ===========================================================================
# The reliever draw from the live pen
# ===========================================================================


def _drawn(fp: FullPoolSampler, row: int = 0) -> FullPoolSampler:
    """Pretend the last change draw landed on ``row``."""
    fp._change_last_row = row
    return fp


def _shares(fp: FullPoolSampler, candidates, n: int = 300, **kw) -> dict[int, float]:
    counts = dict.fromkeys(candidates, 0)
    for _ in range(n):
        i = fp.relief_arm_draw(list(candidates), live_season=_SEASON, **kw)
        assert i is not None
        counts[candidates[i]] += 1
    return {k: v / n for k, v in counts.items()}


class TestRelieverDraw:
    def test_every_weight_off_is_none(self):
        fp = _drawn(_sampler())
        assert fp.relief_weights_on() is False
        assert fp.relief_arm_draw([1, 2, 3], live_season=_SEASON) is None

    def test_an_empty_pen_or_no_drawn_row_is_none(self):
        fp = _sampler()
        fp.relief_rest_sigma = 1.0
        assert fp.relief_arm_draw([], live_season=_SEASON) is None
        assert fp._change_last_row is None
        assert fp.relief_arm_draw([1, 2], live_season=_SEASON) is None

    def test_rest_prefers_the_arm_rested_like_the_drawn_one(self):
        fp = _drawn(_sampler(seed=4))
        fp.relief_rest_sigma = 1.0  # the drawn arm had 5 days of rest
        usage = {1: (5, 0, 0), 2: (0, 1, 40)}
        s = _shares(fp, [1, 2], recent_usage=usage)
        assert s[1] > 0.95

    def test_pitched_two_days_ago_is_a_mismatch_weight(self):
        fp = _drawn(_sampler(seed=4))
        fp.relief_pitched2d_off_weight = 0.1  # the drawn arm had NOT pitched
        usage = {1: (3, 0, 0), 2: (1, 1, 20), 3: (2, 1, 15)}
        s = _shares(fp, [1, 2, 3], recent_usage=usage)
        assert s[1] > 0.75 and abs(s[2] - s[3]) < 0.15

    def test_recent_pitches_prefers_the_nearer_count(self):
        fp = _drawn(_sampler(seed=4))
        fp.relief_pitches3d_sigma = 10.0  # the drawn arm threw 0 in three days
        usage = {1: (5, 0, 0), 2: (5, 0, 45)}
        s = _shares(fp, [1, 2], recent_usage=usage)
        assert s[1] > 0.95

    def test_a_candidate_without_the_fact_is_neutral(self):
        fp = _drawn(_sampler(seed=4))
        fp.relief_rest_sigma = 1.0
        # 2 has no usage row: he keeps the weight 1.0, the same as the perfect match.
        s = _shares(fp, [1, 2], recent_usage={1: (5, 0, 0)})
        assert abs(s[1] - 0.5) < 0.12

    def test_hand_prefers_the_drawn_arms_hand(self):
        fp = _drawn(_sampler(seed=4))
        fp.relief_hand_off_weight = 0.1  # the drawn arm throws right
        s = _shares(fp, [1, 2, 3], throw_hands={1: "R", 2: "L", 3: "l"})
        assert s[1] > 0.75

    def test_role_prefers_the_same_high_leverage_share(self):
        roles = {
            "501:2024": (40, 0.80, 8.0),  # the drawn arm: a closer
            "1:2024": (35, 0.75, 8.2),
            "2:2023": (50, 0.10, 5.1),  # last season's row is the fallback
        }
        fp = _drawn(_sampler(seed=4, roles=roles))
        fp.relief_role_sigma = 0.15
        s = _shares(fp, [1, 2])
        assert s[1] > 0.95

    def test_stuff_prefers_the_arm_the_pitcher_engine_scores_alike(self):
        index = {"501:2024": 0, "1:2024": 1, "2:2024": 2}
        sims = {"501:2024": {"1:2024": 0.9, "2:2024": 0.1}}
        fp = _drawn(_sampler(seed=4, pitcher_sim=sims, index=index))
        fp.relief_pitcher_power = 1.0
        s = _shares(fp, [1, 2])
        assert 0.80 < s[1] < 0.98  # 0.9 : 0.1

    def test_weights_multiply(self):
        fp = _drawn(_sampler(seed=4))
        fp.relief_rest_sigma = 1.0
        fp.relief_hand_off_weight = 0.2
        usage = {1: (5, 0, 0), 2: (5, 0, 0), 3: (0, 1, 30)}
        hands = {1: "R", 2: "L", 3: "R"}
        s = _shares(fp, [1, 2, 3], recent_usage=usage, throw_hands=hands)
        assert s[1] > s[2] > s[3]

    def test_the_counters_tell_a_uniform_draw_from_a_weighted_one(self):
        fp = _drawn(_sampler(seed=4))
        fp.relief_rest_sigma = 1.0
        fp.relief_arm_draw([1, 2], live_season=_SEASON, recent_usage={})  # no facts: uniform
        fp.relief_arm_draw([1, 2], live_season=_SEASON, recent_usage={1: (5, 0, 0), 2: (0, 0, 0)})
        assert list(fp.relief_draw_counts) == [2, 1]


class TestTheLoopPopsTheDrawnArm:
    def test_the_draw_source_uses_the_reliever_draw_and_pops_the_arm(self):
        class _FP:
            def __init__(self):
                self.calls = []

            def pitching_change_draw(self, key, **kw):
                return True

            def relief_arm_draw(self, candidates, **kw):
                self.calls.append((list(candidates), kw))
                return 1  # the second arm

        fp = _FP()
        m = StateMachine(fp, rng=np.random.default_rng(0), manager={})
        m.manager_draw = True
        s = GameState(pitcher_id=100, bat_hand="R", season=_SEASON)
        s.inning, s.half = 6, Half.TOP
        s.throw_hands = {901: "L", 902: "R"}
        s.pitcher_recent_usage = {901: (1, 1, 30), 902: (5, 0, 0)}
        s.manager.bullpen_available = {Team.HOME: [901, 902, 903]}
        m._maybe_pull_starter(s, 1.0)
        assert s.pitcher_id == 902
        assert s.manager.bullpen_available[Team.HOME] == [901, 903]
        cands, kw = fp.calls[0]
        assert cands == [901, 902, 903] and kw["live_season"] == _SEASON
        assert kw["throw_hands"] == s.throw_hands
        assert kw["recent_usage"] == s.pitcher_recent_usage

    def test_a_none_from_the_reliever_draw_keeps_the_positional_pick(self):
        class _FP:
            def pitching_change_draw(self, key, **kw):
                return True

            def relief_arm_draw(self, candidates, **kw):
                return None

        m = StateMachine(_FP(), rng=np.random.default_rng(0), manager={})
        m.manager_draw = True
        s = GameState(pitcher_id=100, bat_hand="R", season=_SEASON)
        s.inning, s.half = 6, Half.TOP
        s.manager.bullpen_available = {Team.HOME: [901, 902, 903]}
        m._maybe_pull_starter(s, 1.0)  # low leverage: from the back of the pen
        assert s.pitcher_id == 903

    def test_a_change_with_the_weights_off_is_the_positional_pick(self):
        # The real pen carries hands and rest. With every reliever weight off
        # the pick is POSITIONAL — never a platoon ranking (before the flip the
        # SIM-434 ranking picked a same-hand arm 93% of the time; the pool's
        # own entering arms are 29% left). The ranking is deleted.
        class _FP:
            def pitching_change_draw(self, key, **kw):
                return True

            def relief_arm_draw(self, candidates, **kw):
                return None  # every weight off

        m = StateMachine(_FP(), rng=np.random.default_rng(0), manager={})
        m.manager_draw = True
        s = GameState(pitcher_id=100, bat_hand="R", season=_SEASON)
        s.inning, s.half = 6, Half.TOP
        s.batter_id = 1
        s.bat_hands = {1: "R"}
        s.throw_hands = {901: "R", 902: "L", 903: "L"}  # the ranking would take 901
        s.pitcher_rest_days = {901: 5.0, 902: 5.0, 903: 5.0}
        s.manager.bullpen_available = {Team.HOME: [901, 902, 903]}
        m._maybe_pull_starter(s, 1.0)
        assert s.pitcher_id == 903  # positional: the back of the pen at low leverage
        # no sampler at all: the same positional pick, whatever the hands say
        m2 = StateMachine(None, rng=np.random.default_rng(0), manager={})
        s2 = GameState(pitcher_id=100, bat_hand="R", season=_SEASON)
        s2.inning, s2.half = 6, Half.TOP
        s2.batter_id = 1
        s2.bat_hands = {1: "R"}
        s2.throw_hands = {901: "R", 902: "L", 903: "L"}
        s2.manager.bullpen_available = {Team.HOME: [901, 902, 903]}
        assert m2._pick_reliever(s2, 1.0) == 903


# ===========================================================================
# The steal weight
# ===========================================================================


class TestStealAggression:
    def _state(self, half=Half.TOP):
        s = GameState(pitcher_id=100, bat_hand="R", season=_SEASON)
        s.half = half
        return s

    def test_neutral_without_a_manager(self):
        m = StateMachine(rng=np.random.default_rng(0))
        s = self._state()
        s.away_manager_profile = {"steal_order_rate_per_1b_opp": 0.10}
        s.manager_league_profile = {"steal_order_rate_per_1b_opp": 0.05}
        assert m._steal_aggression(s) == 1.0

    def test_the_batting_sides_rate_over_the_league_mean(self):
        m = StateMachine(rng=np.random.default_rng(0), manager={})
        s = self._state(Half.TOP)  # the away side bats
        s.away_manager_profile = {"steal_order_rate_per_1b_opp": 0.10}
        s.home_manager_profile = {"steal_order_rate_per_1b_opp": 0.01}
        s.manager_league_profile = {"steal_order_rate_per_1b_opp": 0.05}
        assert m._steal_aggression(s) == pytest.approx(2.0)
        s.half = Half.BOTTOM  # now the home side bats
        assert m._steal_aggression(s) == pytest.approx(0.2)

    def test_clamped_and_neutral_on_a_missing_fact(self):
        m = StateMachine(rng=np.random.default_rng(0), manager={})
        s = self._state()
        s.away_manager_profile = {"steal_order_rate_per_1b_opp": 1.0}
        s.manager_league_profile = {"steal_order_rate_per_1b_opp": 0.05}
        assert m._steal_aggression(s) == 4.0
        s.away_manager_profile = {"steal_order_rate_per_1b_opp": 0.0}
        assert m._steal_aggression(s) == 0.05
        s.away_manager_profile = {}
        assert m._steal_aggression(s) == 1.0
        s.away_manager_profile = {"steal_order_rate_per_1b_opp": 0.1}
        s.manager_league_profile = {"steal_order_rate_per_1b_opp": 0.0}
        assert m._steal_aggression(s) == 1.0


# ===========================================================================
# The no-DB change pool
# ===========================================================================


class TestSyntheticChangePool:
    def test_every_hard_cell_has_three_rows_and_the_sim427_columns(self):
        pool = change_pool()
        assert pool.n == 2 * 2 * 16 * 4 * 3
        assert pool.manager_id.shape == (pool.n,) and int(pool.manager_id[0]) == 1
        assert set(np.unique(pool.in_days_rest).tolist()) == {5}
        assert set(np.unique(pool.in_pitched_2d).tolist()) == {0}
        assert set(np.unique(pool.in_pitches_3d).tolist()) == {0}
        assert set(np.unique(pool.in_throws).tolist()) == {2}

    def test_the_rates_land_per_cell(self):
        pool = change_pool({"starter_half_high": 0.6, "reliever_mid": 0.2})
        fp = FullPoolSampler(EngineArtifacts(pools={}, change_pool=pool), np.random.default_rng(0))
        fp.change_min_cell = 1
        meta = fp._change_meta()
        for cell, rate in (((1, 1, 12, 3), 0.6), ((0, 0, 3, 1), 0.2), ((1, 1, 2, 1), 0.02)):
            rows = meta["cells"][cell]
            assert rows.size == 3
            changed = float(pool.recency[rows][pool.changed[rows] == 1].sum())
            assert changed == pytest.approx(rate)
            assert float(pool.recency[rows].sum()) == pytest.approx(1.0)

    def test_the_league_bundle_carries_the_pool_unless_told_not_to(self):
        assert league_artifacts().change_pool is not None
        assert league_artifacts(manager=False).change_pool is None
        assert DEFAULT_CHANGE_RATES["starter_half_high"] > DEFAULT_CHANGE_RATES["starter_half_low"]


# ===========================================================================
# The resolver's pen attachment
# ===========================================================================


def _slots(ids):
    return tuple(
        LineupSlot(
            batting_order=i + 1, player_id=pid, position_code="DH", is_starter=True, sequence=1
        )
        for i, pid in enumerate(ids)
    )


def _resolved(game_date=date(2024, 6, 1)) -> ResolvedLineup:
    return ResolvedLineup(
        game_pk=777,
        season=_SEASON,
        home=TeamLineup(team_id=10, slots=_slots(range(201, 210)), pitcher_id=222),
        away=TeamLineup(team_id=20, slots=_slots(range(101, 110)), pitcher_id=333),
        throw_hands={222: "R", 333: "L"},
        game_date=game_date,
    )


class _Conn:
    """Answers the resolver's three queries: the listing, the rotation, the
    candidates' usage, and the pen arms' hands."""

    def __init__(self, listing_by_team, *, boom=False):
        self.listing = listing_by_team
        self.boom = boom
        self.calls = []

    async def fetch(self, sql, *params):
        self.calls.append((sql, params))
        if self.boom:
            raise RuntimeError("relation raw.game_bullpen does not exist")
        if "raw.game_bullpen" in sql:
            gp, team = params
            return [{"pitcher_id": p, "listed": lst} for p, lst in self.listing.get(team, [])]
        if "p_started" in sql:
            return [{"player_id": 9001}]  # the rotation: 9001 is an off-day starter
        if "unnest(" in sql:
            ids, _d = params
            return [
                {
                    "player_id": pid,
                    "days_rest": min(pid % 7, 5),
                    "pitched_2d": bool(pid % 2),
                    "pitches_3d": pid % 50,
                }
                for pid in ids
            ]
        if "raw.players" in sql:
            (ids,) = params
            return [
                {"player_id": pid, "bats": "R", "throws": "L" if pid % 2 else "R"} for pid in ids
            ]
        raise AssertionError(sql)


class TestAttachBullpens:
    @pytest.fixture(autouse=True)
    def _box_source(self, monkeypatch):
        monkeypatch.setenv("SIM_BULLPEN_SOURCE", "box")

    def test_the_switch_off_asks_nothing_and_keeps_the_synthetic_pen(self, monkeypatch):
        # Production today: the unit suite pins 'synthetic'; the flip sets 'box'.
        monkeypatch.setenv("SIM_BULLPEN_SOURCE", "synthetic")
        assert bullpen_source_enabled() is False
        conn = _Conn({10: [(601, "bullpen")], 20: [(701, "bullpen")]})
        r = _resolved()
        asyncio.run(_attach_bullpens(conn, r))
        assert conn.calls == [] and r.bullpen_source == "synthetic" and r.bullpen == {}
        monkeypatch.setenv("SIM_BULLPEN_SOURCE", " Box ")
        assert bullpen_source_enabled() is True

    def test_both_sides_from_the_box_with_rest_usage_and_hands(self):
        listing = {
            10: [(9001, "bullpen"), (601, "pitched"), (602, "bullpen"), (222, "started")],
            20: [(701, "bullpen"), (702, "pitched"), (333, "started")],
        }
        conn = _Conn(listing)
        r = _resolved()
        asyncio.run(_attach_bullpens(conn, r))
        assert r.bullpen_source == "box"
        # the rotation arm (9001) and today's starters (222 / 333) are out;
        # the arms the manager used ('pitched') lead the order
        assert r.bullpen == {1: [601, 602], 0: [702, 701]}
        assert r.pitcher_rest_days[601] == 5.0 and r.pitcher_rest_days[702] == 2.0
        assert r.pitcher_recent_usage[601] == (5, 1, 1) and r.pitcher_recent_usage[602] == (0, 0, 2)
        # the pen arms' hands merged in without touching the starters' own
        assert r.throw_hands[601] == "L" and r.throw_hands[602] == "R"
        assert r.throw_hands[222] == "R" and r.throw_hands[333] == "L"

    def test_a_missing_listing_on_either_side_keeps_the_synthetic_marker(self):
        r = _resolved()
        asyncio.run(_attach_bullpens(_Conn({20: [(701, "bullpen")]}), r))
        assert r.bullpen_source == "synthetic" and r.bullpen == {}
        assert r.pitcher_rest_days == {} and r.pitcher_recent_usage == {}

    def test_a_failed_read_keeps_the_synthetic_marker(self):
        r = _resolved()
        asyncio.run(_attach_bullpens(_Conn({}, boom=True), r))
        assert r.bullpen_source == "synthetic" and r.bullpen == {}

    def test_no_game_date_asks_nothing(self):
        conn = _Conn({})
        r = _resolved(game_date=None)
        asyncio.run(_attach_bullpens(conn, r))
        assert conn.calls == [] and r.bullpen_source == "synthetic"

    def test_a_text_date_is_accepted(self):
        conn = _Conn({10: [(601, "bullpen")], 20: [(701, "bullpen")]})
        r = _resolved(game_date="2024-06-01T19:05:00")
        asyncio.run(_attach_bullpens(conn, r))
        assert r.bullpen_source == "box" and r.bullpen == {1: [601], 0: [701]}


# ===========================================================================
# The kwargs contract carries the new facts
# ===========================================================================


class TestKwargsCarryTheFacts:
    def test_the_state_facts_reach_the_kwargs_picklable(self):
        s = GameState(pitcher_id=100, bat_hand="R", season=_SEASON)
        s.home_manager_id, s.away_manager_id = 11, 22
        s.manager.bullpen_available = {Team.HOME: [1, 2], 0: [3]}
        s.pitcher_recent_usage = {1: (5, 0, 0)}
        s.bullpen_source = "box"
        s.home_manager_profile = {"steal_order_rate_per_1b_opp": 0.06}
        s.away_manager_profile = {}
        s.manager_league_profile = {"steal_order_rate_per_1b_opp": 0.05}
        kw = sim_kwargs_from_state(s)
        assert (kw["home_manager_id"], kw["away_manager_id"]) == (11, 22)
        assert kw["bullpen"] == {1: [1, 2], 0: [3]}
        assert kw["pitcher_recent_usage"] == {1: (5, 0, 0)} and kw["bullpen_source"] == "box"
        assert kw["manager_profiles"] == {0: {}, 1: {"steal_order_rate_per_1b_opp": 0.06}}
        assert kw["manager_league_profile"] == {"steal_order_rate_per_1b_opp": 0.05}


# ===========================================================================
# The profile resolver
# ===========================================================================


class _Duck:
    """A DuckDB stand-in: the manager rows by (manager, season), one league row."""

    def __init__(self, rows, league=None, *, boom=False):
        self.rows = rows
        self.league = league
        self.boom = boom
        self._pending = None

    def execute(self, sql, params=None):
        if self.boom:
            raise RuntimeError("Catalog Error: Table does not exist")
        if "league_averages" in sql:
            self._pending = None if self.league is None else (self.league,)
        else:
            mid, season = params
            self._pending = self.rows.get((int(mid), int(season)))
        return self

    def fetchone(self):
        return self._pending


class TestResolveManagerProfiles:
    def _state(self):
        s = GameState(pitcher_id=100, bat_hand="R", season=_SEASON)
        s.home_manager_id, s.away_manager_id = 11, 22
        return s

    def test_the_season_then_the_season_before_then_empty(self):
        n = len(MANAGER_TENDENCY_COLUMNS)
        row_2024 = tuple(float(i) for i in range(n))
        row_2023 = tuple(float(i) + 100 for i in range(n))
        rows = {(11, 2024): row_2024, (22, 2023): row_2023}
        s = self._state()
        resolve_manager_profiles_onto_state(s, _Duck(rows, league='{"a": 1, "b": null}'))
        assert s.home_manager_profile[MANAGER_TENDENCY_COLUMNS[1]] == 1.0
        assert s.away_manager_profile[MANAGER_TENDENCY_COLUMNS[1]] == 101.0
        assert s.manager_league_profile == {"a": 1.0}
        s2 = self._state()
        s2.away_manager_id = 33
        resolve_manager_profiles_onto_state(s2, _Duck(rows))
        assert s2.away_manager_profile == {} and s2.manager_league_profile == {}

    def test_a_null_column_is_left_out_and_a_short_row_ignored(self):
        n = len(MANAGER_TENDENCY_COLUMNS)
        row = tuple([None] + [1.0] * (n - 1))
        s = self._state()
        resolve_manager_profiles_onto_state(s, _Duck({(11, 2024): row, (22, 2024): (1.0, 2.0)}))
        assert MANAGER_TENDENCY_COLUMNS[0] not in s.home_manager_profile
        assert len(s.home_manager_profile) == n - 1
        assert s.away_manager_profile == {}

    def test_no_connection_or_a_missing_table_never_raises(self):
        s = self._state()
        resolve_manager_profiles_onto_state(s, None)
        assert s.home_manager_profile == {} and s.manager_league_profile == {}
        resolve_manager_profiles_onto_state(s, _Duck({}, boom=True))
        assert s.home_manager_profile == {} and s.manager_league_profile == {}


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
