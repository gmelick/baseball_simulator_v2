"""
test_baseball_analyst_sim324.py
===============================
SIM-324 -- the **Phase-4 baseball sniff-test suite**.

WHAT THIS IS (and is NOT)
-------------------------
This suite validates that the SIM-320 ``simulate_game()`` LOOP, when fed a
*calibrated league-average outcome model*, produces **baseball-realistic emergent
aggregate statistics**:

  * a run environment of ~4.4 runs / team / game,
  * pitches-per-PA in the ~3.8-3.9 band,
  * platoon splits that move in the expected direction when a L/R skew is wired
    into the injected model,
  * an RE24 base-out run-expectancy matrix that is monotonic in the sensible
    directions (more runners on / fewer outs => higher run expectancy).

These are "does the loop produce sane baseball?" checks.  They test the LOOP's
arithmetic / aggregation -- the count machine, the half-inning roll, the
``resolve_runs`` RE24 wiring, baserunner advancement -- NOT the quality of the
similarity engines (validated elsewhere).  So the loop is driven by an *injected*
realistic outcome distribution, NOT live DB/FAISS, exactly as the
SIM-316/317/318/319/320 unit tests inject their signals
(see ``tests/unit/test_backend_sim320.py``).

HOW THE MODEL IS INJECTED (the two seams)
-----------------------------------------
Two independent seams shape the emergent statistics, mirroring the spec's two
sampling steps (§2 steps 3 and 5):

  1. **The per-pitch outcome model** -- a calibrated distribution over
     ``{ball, called_strike, swinging_strike, foul, in_play}``.  A
     ``StateMachine`` subclass draws each pitch from it with the loop's seeded
     rng (no sampler), driving the §5.1 count machine (and therefore the walk /
     strikeout rates AND the pitches-per-PA).  The two-strike-foul absorbing rule
     in the real loop lengthens PAs -- so P/PA is an *emergent* property of the
     loop, not a number we set.
  2. **The in-play PA-event model** -- a calibrated distribution over batted-ball
     events ``{single, double, triple, home_run, field_out, ...}``.  An injected
     :class:`PlayResolver` maps each ``in_play`` pitch to a sampled event; the
     loop's own baserunning (``_advance_runners``) + ``resolve_runs`` then turn
     that into runs given the live base-out state.  We deliberately do NOT inject
     ``result_runs`` -- runs EMERGE from the loop's baserunning so the run
     environment is a real test of the loop, not an echo of an injected number.

NOISE-ROBUSTNESS
----------------
Every assertion uses (a) a fixed seed, (b) a tolerance band justified by
Monte-Carlo sampling error over the chosen game count, and (c) enough games to
make the band tight without blowing the sandbox time budget.  The always-on
subset runs a few hundred games (seconds); a ``@pytest.mark.slow`` test runs a
larger sample for a tighter run-environment check.
"""

from __future__ import annotations

import numpy as np
import pytest

from simulation.game_state import GameState, Half, Team
from simulation.run_resolution import RE24_MATRIX, OUTS_PER_INNING, re24_value
from simulation.sim_loop import (
    REGULATION_INNINGS,
    FieldingSignal,
    GameSimResult,
    PlayResolver,
    StateMachine,
    simulate_game,
)

SEASON = 2024
PITCHER = 477132
AWAY_LINEUP = list(range(101, 110))   # 9 batters
HOME_LINEUP = list(range(201, 210))   # 9 batters


# ===========================================================================
# The calibrated league-average outcome model
# ===========================================================================
#
# (1) PER-PITCH OUTCOME MODEL (drives the count machine -> walk/K rate + P/PA).
#
# These per-pitch probabilities are tuned so that, run through the real §5.1
# count machine (incl. the two-strike-foul absorbing rule), the EMERGENT PA
# statistics land at league average: ~8-9% walks, ~22% strikeouts, ~3.85 P/PA.
# A 2024-ish per-pitch mix is roughly: ~36% balls, ~26% called+swinging strikes,
# ~16% fouls, ~22% balls-in-play; foul share is high because two-strike fouls are
# absorbed (they prolong the PA), which is exactly the P/PA driver the spec calls
# out (§5.1 / foul-ball doc §3.3).
LEAGUE_PITCH_MODEL: "dict[str, float]" = {
    "ball": 0.332,
    "called_strike": 0.138,
    "swinging_strike": 0.087,
    "foul": 0.273,
    "in_play": 0.170,
}

# (2) IN-PLAY PA-EVENT MODEL (conditional on a ball put in play -> BABIP / HR).
#
# Conditional on contact (``in_play``), the realized event mix.  2024 league:
# of balls in play ~ 7% are home runs, BABIP ~ .300 on the non-HR balls, so the
# non-HR hit split is single-heavy with a smaller double / rare triple share, the
# remainder outs (incl. a slice of GIDP).  These are the frequencies the play
# pool would emit; we inject them directly so the loop's baserunning + resolve_runs
# convert them into the run environment.
LEAGUE_INPLAY_MODEL: "dict[str, float]" = {
    "home_run": 0.050,
    "single": 0.262,
    "double": 0.084,
    "triple": 0.008,
    "field_out": 0.546,
    "ground_into_double_play": 0.050,
}

# The hit value (result_hits) + outs recorded (result_outs) per injected event,
# in the play-pool vocabulary resolve_runs / advance_state consume.
_EVENT_HITS = {"single": 1, "double": 2, "triple": 3, "home_run": 4,
               "field_out": 0, "ground_into_double_play": 0}
_EVENT_OUTS = {"single": 0, "double": 0, "triple": 0, "home_run": 0,
               "field_out": 1, "ground_into_double_play": 2}


def _normalize(model: "dict[str, float]") -> "tuple[list, np.ndarray]":
    keys = list(model.keys())
    probs = np.asarray([model[k] for k in keys], dtype=np.float64)
    return keys, probs / probs.sum()


# ===========================================================================
# Test doubles — a no-DB league outcome machine + in-play resolver
# ===========================================================================


class _LeagueOutcomeMachine(StateMachine):
    """A StateMachine that draws each pitch outcome from the calibrated
    per-pitch model with its own seeded loop rng (NO sampler).

    A ``hand_skew`` (>0) tilts the per-pitch mix by the batter's hand to model a
    platoon advantage: a positive skew makes the configured ``platoon_adv_hand``
    batters put more balls in play / strike out less (the offensive-advantage
    side).  Used by the platoon-split test; 0.0 (the default) is the neutral
    league model used everywhere else.
    """

    def __init__(self, *a, hand_skew: float = 0.0, platoon_adv_hand: str = "L", **kw):
        super().__init__(*a, **kw)
        self._base_keys, self._base_probs = _normalize(LEAGUE_PITCH_MODEL)
        self._hand_skew = float(hand_skew)
        self._platoon_adv_hand = platoon_adv_hand

    def _draw_for(self, state) -> str:
        if self._hand_skew == 0.0:
            idx = int(self.rng.choice(len(self._base_keys), p=self._base_probs))
            return self._base_keys[idx]
        probs = dict(zip(self._base_keys, self._base_probs))
        # The advantaged hand: shift mass from whiffs (swinging_strike) into
        # balls-in-play (more / better contact).  The disadvantaged hand gets the
        # opposite tilt.  Direction only -- the test asserts the loop PROPAGATES
        # handedness, not a precise magnitude.
        sign = 1.0 if state.bat_hand == self._platoon_adv_hand else -1.0
        delta = sign * self._hand_skew
        probs["in_play"] = probs["in_play"] + delta
        probs["swinging_strike"] = probs["swinging_strike"] - delta
        keys = list(probs.keys())
        arr = np.asarray([max(1e-9, probs[k]) for k in keys], dtype=np.float64)
        arr = arr / arr.sum()
        idx = int(self.rng.choice(len(keys), p=arr))
        return keys[idx]

    def step_pitch(self, state, **_kw):  # type: ignore[override]
        return super().step_pitch(state, pitch_outcome=self._draw_for(state))


class _LeagueInPlayResolver(PlayResolver):
    """Resolve an ``in_play`` pitch to a sampled league-average batted-ball event.

    The event is drawn from the calibrated in-play model with a shared seeded rng.
    We return only the event + its (hits, outs) deltas; we deliberately leave
    ``result_runs`` to the loop -- its ``_advance_runners`` scores the runs given
    the live base-out state, so the run environment EMERGES from the loop rather
    than being injected.  ``_injected_battedball`` carries NO ``result_runs`` for
    exactly that reason (so ``_resolve_in_play`` falls back to the loop's own
    ``runners_scored``).
    """

    def __init__(self, rng: "np.random.Generator"):
        self.rng = rng
        self._keys, self._probs = _normalize(LEAGUE_INPLAY_MODEL)
        # Present (with NO result_runs) so the no-sampler path reaches step 6/7.
        self._injected_battedball = {"event": "field_out"}

    def resolve_fielding(self, state, battedball_sample) -> FieldingSignal:
        idx = int(self.rng.choice(len(self._keys), p=self._probs))
        event = self._keys[idx]
        outs = _EVENT_OUTS[event]
        # Context-filter the sampled event the way a real play pool would: a
        # double-play out can only be a *double* play when there are <2 outs AND
        # a runner is on first to be forced.  Otherwise it degrades to a single
        # ground-ball out (one out).  This keeps the committed base-out state
        # legal (outs never exceed 3) -- the production sampler is likewise
        # base-out-state conditioned, so this is calibration, not a loop change.
        if event == "ground_into_double_play" and (
            state.outs >= OUTS_PER_INNING - 1 or state.bases.first is None
        ):
            event = "field_out"
            outs = 1
        return FieldingSignal(
            event=event,
            result_hits=_EVENT_HITS[event],
            result_outs=outs,
            result_runs=0,   # runs emerge from the loop's baserunning
        )


class _HandedMachine(_LeagueOutcomeMachine):
    """A league machine that sets ``bat_hand`` to the batting side's hand before
    each pitch (so the per-pitch skew sees the right handedness)."""

    def __init__(self, *a, away_hand: str = "R", home_hand: str = "R", **kw):
        super().__init__(*a, **kw)
        self._away_hand = away_hand
        self._home_hand = home_hand

    def step_pitch(self, state, **_kw):  # type: ignore[override]
        state.bat_hand = self._home_hand if state.half == Half.BOTTOM else self._away_hand
        return super().step_pitch(state)


class _PACountingMachine(_LeagueOutcomeMachine):
    """A league machine that counts terminal plate appearances (for P/PA)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.pa_count = 0

    def step_pitch(self, state, **_kw):  # type: ignore[override]
        result = super().step_pitch(state)
        if result.pa_terminal:
            self.pa_count += 1
        return result


class _EventTallyMachine(_LeagueOutcomeMachine):
    """A league machine that tallies terminal PA events across many games."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.pa_count = 0
        self.events = {"walk": 0, "strikeout": 0, "single": 0, "double": 0,
                       "triple": 0, "home_run": 0, "field_out": 0,
                       "ground_into_double_play": 0}

    def step_pitch(self, state, **_kw):  # type: ignore[override]
        result = super().step_pitch(state)
        if result.pa_terminal:
            self.pa_count += 1
            if result.event in self.events:
                self.events[result.event] += 1
        return result


# ===========================================================================
# Aggregation harness — run N seeded games, collect emergent statistics
# ===========================================================================


class _Aggregate:
    """Accumulates per-game emergent statistics across a batch of games."""

    __slots__ = ("games", "total_runs", "total_pitches", "team_games")

    def __init__(self):
        self.games = 0
        self.total_runs = 0
        self.total_pitches = 0
        self.team_games = 0  # team-games == 2 * games (two teams bat per game)

    def add(self, r: GameSimResult):
        self.games += 1
        self.total_runs += r.home_score + r.away_score
        self.total_pitches += r.total_pitches
        self.team_games += 2

    @property
    def runs_per_team_game(self) -> float:
        return self.total_runs / self.team_games if self.team_games else 0.0


def _fresh_state(seed: int, away_hand: str = "R") -> GameState:
    return GameState(
        pitcher_id=PITCHER, bat_hand=away_hand, season=SEASON,
        away_lineup=AWAY_LINEUP, home_lineup=HOME_LINEUP,
        batter_id=AWAY_LINEUP[0], seed=seed,
    )


def _run_one(seed: int, *, hand_skew: float = 0.0, platoon_adv_hand: str = "L",
             away_hand: str = "R", home_hand: str = "R") -> GameSimResult:
    """Simulate one full game under the calibrated model with a fixed seed."""
    rng = np.random.default_rng(seed)
    resolver = _LeagueInPlayResolver(np.random.default_rng(seed + 1_000_003))
    if away_hand != home_hand or hand_skew != 0.0:
        sm = _HandedMachine(
            resolver=resolver, rng=rng, hand_skew=hand_skew,
            platoon_adv_hand=platoon_adv_hand,
            away_hand=away_hand, home_hand=home_hand,
        )
    else:
        sm = _LeagueOutcomeMachine(resolver=resolver, rng=rng)
    state = _fresh_state(seed, away_hand=away_hand)
    return simulate_game(sm, initial_state=state, seed=seed)


def _aggregate(seeds, **kw) -> _Aggregate:
    agg = _Aggregate()
    for s in seeds:
        agg.add(_run_one(s, **kw))
    return agg


# ===========================================================================
# 1. Run environment ~ 4.4 R / team / game
# ===========================================================================


class TestRunEnvironment:
    def test_run_environment_is_baseball_realistic(self):
        # A few hundred seeded games gives a stable mean within the sandbox budget.
        agg = _aggregate(range(220))
        rpg = agg.runs_per_team_game
        # The spec's Phase-4 sniff target is ~4.4 R/team/G.  The band 3.8-5.0 is
        # justified: with ~220 games (440 team-games) and a per-team-game run SD
        # of ~3, the standard error of the mean is ~3/sqrt(440) ~ 0.14, so a
        # +/-0.6 band is > 4 standard errors -- it absorbs Monte-Carlo noise AND
        # the modest calibration slack of an injected (not measured) model while
        # still failing loudly if the loop mis-aggregates runs (e.g. the SIM-312
        # silent-zero-out regression would push this far above 5.0).
        assert 3.8 <= rpg <= 5.0, (
            f"run environment {rpg:.2f} R/team/G outside the realistic 3.8-5.0 band"
        )

    def test_run_environment_is_deterministic_for_a_fixed_seed_set(self):
        # The whole aggregate is reproducible from the seed set (noise-robust by
        # construction: re-running gives the identical number).
        a = _aggregate(range(40))
        b = _aggregate(range(40))
        assert a.total_runs == b.total_runs
        assert a.runs_per_team_game == b.runs_per_team_game

    @pytest.mark.slow
    def test_run_environment_tighter_band_large_sample(self):
        # A larger sample tightens the estimate; assert the mean sits near 4.4
        # with a tighter band (slow-marked so the always-on suite stays fast).
        agg = _aggregate(range(600))
        rpg = agg.runs_per_team_game
        assert 4.0 <= rpg <= 4.9, (
            f"large-sample run environment {rpg:.2f} outside the tight 4.0-4.9 band"
        )


# ===========================================================================
# 2. Pitches per PA ~ 3.7-4.0 (spec says 3.8-3.9)
# ===========================================================================


class TestPitchesPerPA:
    @staticmethod
    def _pitches_per_pa(seeds):
        total_pitches = 0
        total_pa = 0
        for s in seeds:
            rng = np.random.default_rng(s)
            res = _LeagueInPlayResolver(np.random.default_rng(s + 1_000_003))
            sm = _PACountingMachine(resolver=res, rng=rng)
            r = simulate_game(sm, initial_state=_fresh_state(s), seed=s)
            total_pitches += r.total_pitches
            total_pa += sm.pa_count
        return total_pitches / total_pa, total_pa

    def test_pitches_per_pa_in_realistic_band(self):
        ppa, n_pa = self._pitches_per_pa(range(220))
        # Spec target 3.8-3.9; we assert the slightly wider 3.7-4.0 to allow
        # Monte-Carlo noise + injected-model slack.  With tens of thousands of PAs
        # across 220 games the SE of P/PA is < 0.01, so the band is many SEs wide
        # -- it cannot flake, yet a broken count machine (e.g. the two-strike-foul
        # absorbing rule regressing) would move P/PA outside it immediately.
        assert n_pa > 5000, f"too few PAs ({n_pa}) for a stable P/PA estimate"
        assert 3.7 <= ppa <= 4.0, (
            f"pitches-per-PA {ppa:.3f} outside the realistic 3.7-4.0 band"
        )


# ===========================================================================
# 3. Platoon split emerges (wire a L/R skew -> assert direction)
# ===========================================================================


class TestPlatoonSplit:
    @staticmethod
    def _runs_by_side(seeds, *, hand_skew, platoon_adv_hand):
        # AWAY bats with the advantaged hand, HOME with the disadvantaged hand;
        # the advantaged side should out-score the other across the batch
        # (direction only, not magnitude).
        adv = dis = 0
        other = "R" if platoon_adv_hand == "L" else "L"
        for s in seeds:
            r = _run_one(
                s, hand_skew=hand_skew, platoon_adv_hand=platoon_adv_hand,
                away_hand=platoon_adv_hand, home_hand=other,
            )
            adv += r.away_score   # away bats with the advantaged hand
            dis += r.home_score   # home bats with the disadvantaged hand
        return adv, dis

    def test_platoon_advantage_side_scores_more(self):
        # A meaningful per-pitch skew (more contact, fewer whiffs for the
        # advantaged hand) must propagate through the loop to MORE runs for that
        # side, aggregated over enough games to drown the per-game noise.
        adv, dis = self._runs_by_side(range(180), hand_skew=0.06,
                                      platoon_adv_hand="L")
        assert adv > dis, (
            f"platoon split did not emerge: advantaged-hand runs {adv} "
            f"<= disadvantaged-hand runs {dis}"
        )
        # The gap should be appreciable (not a one-run fluke) given a 6pp skew
        # over 180 games; require at least a 5% edge to be robust to noise.
        assert adv >= dis * 1.05, (
            f"platoon edge too small to be a real propagated split: "
            f"adv={adv} dis={dis} (ratio {adv / max(1, dis):.3f})"
        )

    def test_zero_skew_has_no_systematic_platoon_split(self):
        # Sanity / control: with NO skew the two sides are statistically even
        # (the harness is not biased toward one side).  Wide band -- this only
        # guards against a structural bias, not Monte-Carlo wobble.
        adv, dis = self._runs_by_side(range(120), hand_skew=0.0,
                                      platoon_adv_hand="L")
        ratio = adv / max(1, dis)
        assert 0.85 <= ratio <= 1.15, (
            f"zero-skew sides are systematically uneven (ratio {ratio:.3f}) -- "
            "the harness has a structural bias"
        )


# ===========================================================================
# 4. RE24 base-out run-expectancy monotonicity (read the loop's actual matrix)
# ===========================================================================


class TestRE24Monotonicity:
    """Read the RE24 matrix the loop actually uses (run_resolution.RE24_MATRIX)
    and assert it is monotonic in the sensible directions.  This validates the
    base-out run-expectancy table that drives every ``resolve_runs`` call in the
    loop -- a non-monotonic table would corrupt the run environment."""

    def test_re24_decreases_with_more_outs_holding_bases_fixed(self):
        # For each base state, RE strictly decreases as outs go 0 -> 1 -> 2.
        for runners_state in range(8):
            re0 = RE24_MATRIX[(0, runners_state)]
            re1 = RE24_MATRIX[(1, runners_state)]
            re2 = RE24_MATRIX[(2, runners_state)]
            assert re0 > re1 > re2, (
                f"RE24 not decreasing in outs for runners_state={runners_state}: "
                f"{re0} -> {re1} -> {re2}"
            )

    def test_re24_increases_when_a_runner_is_added_holding_outs_fixed(self):
        # Adding a runner (turning a 0-bit on) never lowers RE at a fixed out
        # count.  Compare every base state to each superset reachable by adding
        # exactly one runner -- the superset must have >= RE.
        for outs in range(OUTS_PER_INNING):
            for rs in range(8):
                for bit in (0b001, 0b010, 0b100):
                    if rs & bit:
                        continue  # already occupied
                    sup = rs | bit
                    assert RE24_MATRIX[(outs, sup)] >= RE24_MATRIX[(outs, rs)], (
                        f"adding a runner lowered RE24 at outs={outs}: "
                        f"{rs:03b} -> {sup:03b}"
                    )

    def test_re24_more_total_baserunners_dominates_empty(self):
        # The loaded bases always beat empty bases at the same out count, and any
        # occupied state beats empty -- the gross monotonicity the run
        # environment depends on.
        for outs in range(OUTS_PER_INNING):
            empty = RE24_MATRIX[(outs, 0)]
            loaded = RE24_MATRIX[(outs, 7)]
            assert loaded > empty
            for rs in range(1, 8):
                assert RE24_MATRIX[(outs, rs)] > empty

    def test_re24_value_helper_zeroes_a_completed_inning(self):
        # The loop relies on RE == 0 once the inning is over (3 outs) -- the
        # run-value delta on the third out must net out the carry-over RE.
        assert re24_value(OUTS_PER_INNING, 7) == 0.0
        assert re24_value(0, 0) == RE24_MATRIX[(0, 0)]


# ===========================================================================
# 5. The injected model itself produces league-average PA outcomes
# ===========================================================================


class TestModelCalibrationSanity:
    """A direct check that the injected per-pitch + in-play models yield
    league-average WALK / STRIKEOUT / hit rates, so the run-environment and P/PA
    bands above are anchored to a genuinely calibrated model (not an arbitrary
    one that happens to hit a number)."""

    def test_walk_and_strikeout_rates_are_league_average(self):
        events = {"walk": 0, "strikeout": 0, "single": 0, "double": 0,
                  "triple": 0, "home_run": 0, "field_out": 0,
                  "ground_into_double_play": 0}
        total = 0
        for s in range(80):
            rng = np.random.default_rng(s)
            res = _LeagueInPlayResolver(np.random.default_rng(s + 7))
            sm = _EventTallyMachine(resolver=res, rng=rng)
            simulate_game(sm, initial_state=_fresh_state(s), seed=s)
            total += sm.pa_count
            for k in events:
                events[k] += sm.events[k]
        assert total > 2500, f"too few PAs ({total}) to judge rates"
        bb_rate = events["walk"] / total
        k_rate = events["strikeout"] / total
        hr_rate = events["home_run"] / total
        # League 2024: BB ~ 8.2%, K ~ 22.6%, HR ~ 3% of PA.  Bands are wide enough
        # for sampling noise (this 80-game subset has ~2.5-3k PAs, so each rate has
        # an SE of ~0.008) but tight enough to certify the model is CALIBRATED to a
        # league-average run-scoring environment, not an arbitrary one.
        assert 0.06 <= bb_rate <= 0.11, f"walk rate {bb_rate:.3f} not league-average"
        assert 0.18 <= k_rate <= 0.28, f"strikeout rate {k_rate:.3f} not league-average"
        assert 0.015 <= hr_rate <= 0.045, f"HR rate {hr_rate:.3f} not league-average"
