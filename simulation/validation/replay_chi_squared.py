"""
replay_chi_squared.py
=====================
SIM-325 -- the **E2E historical-replay + chi-squared goodness-of-fit** harness
(the validation spine's end-to-end check).

WHAT THIS IS
------------
A reusable harness that:

  (a) takes a set of *historical* games with KNOWN actual per-team-game run
      totals (the :class:`HistoricalGame` seam -- one row per team-game),
  (b) **replays** the matching games through the SIM-320
      ``simulate_game()`` loop to produce a SIMULATED per-team-game run
      distribution, and
  (c) runs a **chi-squared goodness-of-fit** test comparing the simulated vs
      actual run-total distributions -- binned 0,1,2,...,N+ runs/team/game with
      low-expected-count **tail pooling** so the chi-squared assumptions hold --
      reporting the chi-squared statistic, degrees of freedom, and p-value, and
      asserting ``p > 0.05`` (the spec's validation gate).

THE NO-DB REALITY + THE REAL-DATA SEAM
--------------------------------------
The full historical Statcast DB is NOT loadable in the sandbox, so the harness
is validated on a **reference run distribution** the same way SIM-324 validates
the loop's emergent statistics: drive ``simulate_game()`` with a *calibrated
league-average outcome model* (the SIM-324 idiom, re-exported here) and compare
the simulated distribution against a reference distribution drawn from the SAME
calibrated model (self-consistency) OR a published league run-per-game
distribution.  Either way the test is meaningful and passes (p > 0.05) without
the real DB.

The REAL-DATA SEAM is :class:`HistoricalGame` + :func:`run_total_distribution`
+ :func:`chi_squared_gof`: when the real Statcast DB is available, build a list
of :class:`HistoricalGame` from the actual box scores (each carries the matchup
keys + the actual run total), pass it to :func:`replay_and_test`, and the SAME
chi-squared machinery validates the loop against real history -- no code change,
only a different ``historical_games`` argument and a production resolver/sampler.

NOISE-ROBUSTNESS
----------------
The simulated distribution is built from MANY seeded games (fixed seeds), the
binning pools low-expected tail bins (so no expected count is tiny), and the
gate is a p-value (not a brittle point tolerance).  A NEGATIVE CONTROL
(:func:`chi_squared_gof` against a deliberately WRONG reference) is rejected
(p < 0.05), proving the test has power.

This module does NOT modify ``simulation/sim_loop.py``; it only drives it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import chisquare

from simulation.game_state import GameState
from simulation.sim_loop import (
    FieldingSignal,
    GameSimResult,
    PlayResolver,
    StateMachine,
    simulate_game,
)

# ---------------------------------------------------------------------------
# The calibrated league-average outcome model (the SIM-324 idiom, re-used)
# ---------------------------------------------------------------------------
#
# These are the SIM-324 per-pitch + in-play models: run through the real §5.1
# count machine they produce league-average emergent PA statistics (~8-9% BB,
# ~22% K, ~3.85 P/PA) and a ~4.4 R/team/G run environment.  We re-use them here
# (rather than importing the test module) so the harness is a self-contained,
# importable library: production callers can build a reference distribution from
# the SAME calibrated model the SIM-324 sniff suite is anchored to.

LEAGUE_PITCH_MODEL: dict[str, float] = {
    "ball": 0.332,
    "called_strike": 0.138,
    "swinging_strike": 0.087,
    "foul": 0.273,
    "in_play": 0.170,
}

LEAGUE_INPLAY_MODEL: dict[str, float] = {
    "home_run": 0.050,
    "single": 0.262,
    "double": 0.084,
    "triple": 0.008,
    "field_out": 0.546,
    "ground_into_double_play": 0.050,
}

_EVENT_HITS = {
    "single": 1,
    "double": 2,
    "triple": 3,
    "home_run": 4,
    "field_out": 0,
    "ground_into_double_play": 0,
}
_EVENT_OUTS = {
    "single": 0,
    "double": 0,
    "triple": 0,
    "home_run": 0,
    "field_out": 1,
    "ground_into_double_play": 2,
}

#: Default matchup keys for the no-DB reference replay (mirror SIM-324).
_DEFAULT_SEASON = 2024
_DEFAULT_PITCHER = 477132
_DEFAULT_AWAY_LINEUP = list(range(101, 110))
_DEFAULT_HOME_LINEUP = list(range(201, 210))

#: Outs-per-inning constant for the in-play double-play context filter.
_OUTS_PER_INNING = 3


def _normalize(model: dict[str, float]) -> tuple[list, np.ndarray]:
    keys = list(model.keys())
    probs = np.asarray([model[k] for k in keys], dtype=np.float64)
    return keys, probs / probs.sum()


# ---------------------------------------------------------------------------
# The real-data seam: one row per team-game with a KNOWN actual run total
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoricalGame:
    """One historical TEAM-game with its KNOWN actual run total -- the real-data
    seam (SIM-325 acceptance #2).

    A team-game (not a game) is the unit because the chi-squared compares the
    per-team-game run distribution.  ``runs`` is the actual runs that team
    scored.  The remaining fields are the matchup keys the loop needs to *replay*
    the game (the pitcher faced, season, batting-side hand, and the lineups);
    when the real Statcast DB is available these are filled from the box score
    and a production sampler/resolver is wired so the replay draws from real
    play pools.  In the no-DB sandbox the keys default to the SIM-324 reference
    matchup and the loop is driven by the calibrated league model.

    To plug REAL data: build ``[HistoricalGame(runs=actual, pitcher_id=..., ...),
    ...]`` from the box scores and pass it to :func:`replay_and_test` with a
    production ``state_machine_factory`` -- the SAME chi-squared machinery then
    validates the loop against real history.
    """

    runs: int
    pitcher_id: int = _DEFAULT_PITCHER
    season: int = _DEFAULT_SEASON
    bat_hand: str = "R"
    away_lineup: tuple[int, ...] = tuple(_DEFAULT_AWAY_LINEUP)
    home_lineup: tuple[int, ...] = tuple(_DEFAULT_HOME_LINEUP)
    seed: int | None = None


# ---------------------------------------------------------------------------
# The chi-squared result value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChiSquaredResult:
    """The reported chi-squared goodness-of-fit outcome (SIM-325 acceptance #1).

    ``statistic`` is the chi-squared test statistic, ``dof`` the degrees of
    freedom (``#bins - 1`` after pooling), ``p_value`` the GOF p-value, and
    ``bins`` the human-readable bin labels (e.g. ``["0", "1", "2", "3+"]``) the
    observed/expected counts correspond to.  ``observed`` / ``expected`` are the
    post-pooling count vectors actually fed to the chi-squared test, so a caller
    can inspect exactly what was compared.  ``passed`` is ``p_value > alpha``.
    """

    statistic: float
    dof: int
    p_value: float
    bins: list[str]
    observed: list[float]
    expected: list[float]
    alpha: float = 0.05

    @property
    def passed(self) -> bool:
        return self.p_value > self.alpha


# ---------------------------------------------------------------------------
# Test doubles — the calibrated no-DB league machine + in-play resolver
# (the SIM-324 idiom, re-used so the reference replay is realistic)
# ---------------------------------------------------------------------------


class _LeagueOutcomeMachine(StateMachine):
    """A StateMachine that draws each pitch outcome from the calibrated per-pitch
    league model with its own seeded loop rng (NO sampler) -- the SIM-324 no-DB
    driver path, re-used so the reference replay is baseball-realistic."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._keys, self._probs = _normalize(LEAGUE_PITCH_MODEL)

    def step_pitch(self, state, **_kw):  # type: ignore[override]
        idx = int(self.rng.choice(len(self._keys), p=self._probs))
        return super().step_pitch(state, pitch_outcome=self._keys[idx])


class _LeagueInPlayResolver(PlayResolver):
    """Resolve an ``in_play`` pitch to a sampled league-average batted-ball event
    (the SIM-324 idiom): runs EMERGE from the loop's baserunning, not injected."""

    def __init__(self, rng: np.random.Generator):
        self.rng = rng
        self._keys, self._probs = _normalize(LEAGUE_INPLAY_MODEL)
        self._injected_battedball = {"event": "field_out"}

    def resolve_fielding(self, state, battedball_sample) -> FieldingSignal:
        idx = int(self.rng.choice(len(self._keys), p=self._probs))
        event = self._keys[idx]
        outs = _EVENT_OUTS[event]
        # Same context filter as SIM-324: a GIDP is only a double play with <2
        # outs AND a runner on first to force; else it degrades to a 1-out
        # ground out (keeps the committed base-out state legal).
        if event == "ground_into_double_play" and (
            state.outs >= _OUTS_PER_INNING - 1 or state.bases.first is None
        ):
            event = "field_out"
            outs = 1
        return FieldingSignal(
            event=event,
            result_hits=_EVENT_HITS[event],
            result_outs=outs,
            result_runs=0,
        )


def _default_state_machine(seed: int) -> StateMachine:
    """Build the calibrated no-DB league machine for one seeded replay game."""
    rng = np.random.default_rng(seed)
    resolver = _LeagueInPlayResolver(np.random.default_rng(seed + 1_000_003))
    return _LeagueOutcomeMachine(resolver=resolver, rng=rng)


# ---------------------------------------------------------------------------
# Replay: drive simulate_game() to a per-team-game run distribution
# ---------------------------------------------------------------------------


def simulate_run_distribution(
    n_games: int,
    *,
    base_seed: int = 0,
    state_machine_factory=None,
    pitcher_id: int = _DEFAULT_PITCHER,
    season: int = _DEFAULT_SEASON,
    bat_hand: str = "R",
    away_lineup: list[int] | None = None,
    home_lineup: list[int] | None = None,
) -> list[int]:
    """Replay ``n_games`` games through ``simulate_game()`` and return the SIMULATED
    per-TEAM-game run totals (a flat list of ``2 * n_games`` integers -- home +
    away of each game).

    Each game uses a distinct fixed seed (``base_seed + i``) so the whole
    distribution is reproducible.  ``state_machine_factory`` is the injection
    seam: ``factory(seed) -> StateMachine``; it defaults to the calibrated no-DB
    league machine (the SIM-324 idiom).  A production caller passes a factory that
    wires a real sampler/resolver to replay against real play pools.

    The matchup keys (pitcher / season / hand / lineups) are passed straight to
    :func:`simulate_game`; in the no-DB path they are bookkeeping only (the
    outcome comes from the injected model).
    """
    if n_games <= 0:
        raise ValueError(f"n_games must be positive, got {n_games}.")
    factory = state_machine_factory or _default_state_machine
    away = list(away_lineup) if away_lineup is not None else list(_DEFAULT_AWAY_LINEUP)
    home = list(home_lineup) if home_lineup is not None else list(_DEFAULT_HOME_LINEUP)
    totals: list[int] = []
    for i in range(n_games):
        seed = base_seed + i
        sm = factory(seed)
        state = GameState(
            pitcher_id=pitcher_id,
            bat_hand=bat_hand,
            season=season,
            away_lineup=away,
            home_lineup=home,
            batter_id=away[0],
            seed=seed,
        )
        result: GameSimResult = simulate_game(sm, initial_state=state, seed=seed)
        totals.append(int(result.home_score))
        totals.append(int(result.away_score))
    return totals


def replay_historical_games(
    historical_games: list[HistoricalGame],
    *,
    base_seed: int = 0,
    state_machine_factory=None,
) -> list[int]:
    """Replay each :class:`HistoricalGame`'s matchup through ``simulate_game()`` and
    return the SIMULATED per-team-game run totals (the real-data replay path).

    One simulated team-game is produced per :class:`HistoricalGame` (so the
    simulated and actual sample sizes match for a fair chi-squared).  Each game's
    seed is its ``seed`` field when set, else ``base_seed + i`` (reproducible).
    The matchup keys come from the :class:`HistoricalGame`; with a production
    ``state_machine_factory`` (wiring a real sampler/resolver) this is the real
    historical replay -- otherwise the calibrated no-DB model drives it.
    """
    factory = state_machine_factory or _default_state_machine
    totals: list[int] = []
    for i, hg in enumerate(historical_games):
        seed = hg.seed if hg.seed is not None else base_seed + i
        sm = factory(seed)
        state = GameState(
            pitcher_id=hg.pitcher_id,
            bat_hand=hg.bat_hand,
            season=hg.season,
            away_lineup=list(hg.away_lineup),
            home_lineup=list(hg.home_lineup),
            batter_id=hg.away_lineup[0],
            seed=seed,
        )
        result: GameSimResult = simulate_game(sm, initial_state=state, seed=seed)
        # One team-game per historical row: take the home half (arbitrary but
        # consistent) so |simulated| == |actual| for the GOF.
        totals.append(int(result.home_score))
    return totals


# ---------------------------------------------------------------------------
# Binning + low-expected-count pooling (the chi-squared assumptions)
# ---------------------------------------------------------------------------


def bin_run_totals(run_totals, max_bin: int = 10) -> np.ndarray:
    """Histogram a sequence of integer per-team-game run totals into bins
    ``0, 1, 2, ..., (max_bin)+`` -- the last bin is a ``max_bin+`` tail that
    absorbs every total >= ``max_bin``.

    Returns a length ``max_bin + 1`` count vector (index ``j`` == games with
    exactly ``j`` runs, except the final index which is ``>= max_bin``).  This is
    the raw histogram BEFORE low-expected-count pooling
    (:func:`pool_low_expected_bins`).
    """
    if max_bin < 1:
        raise ValueError(f"max_bin must be >= 1, got {max_bin}.")
    counts = np.zeros(max_bin + 1, dtype=np.float64)
    for r in run_totals:
        r = int(r)
        if r < 0:
            raise ValueError(f"run total cannot be negative: {r}.")
        counts[min(r, max_bin)] += 1.0
    return counts


def pool_low_expected_bins(
    observed: np.ndarray,
    expected: np.ndarray,
    *,
    min_expected: float = 5.0,
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Pool adjacent TAIL bins until every EXPECTED count is >= ``min_expected``,
    so the chi-squared approximation is valid (the classic Cochran rule:
    expected counts should not be tiny).

    Both ``observed`` and ``expected`` are aligned count vectors over the same
    bins (``bin_run_totals`` output, possibly already scaled to the same total).
    Working from the HIGH-run tail inward, any bin whose expected count is below
    ``min_expected`` is merged into the bin below it (observed AND expected
    summed), collapsing the sparse high-run tail into a single ``k+`` bin.  If
    the lowest bin still ends up below ``min_expected`` it is merged upward into
    its neighbour so no bin is left sparse.

    Returns ``(pooled_observed, pooled_expected, labels)`` where ``labels`` are
    human-readable bin names (``"0", "1", ..., "k+"``).  Pure / side-effect-free.
    """
    obs = list(np.asarray(observed, dtype=np.float64))
    exp = list(np.asarray(expected, dtype=np.float64))
    if len(obs) != len(exp):
        raise ValueError("observed and expected must be the same length.")
    if len(obs) == 0:
        raise ValueError("cannot pool empty count vectors.")
    # Track which original bin indices each pooled bin spans, so we can label.
    spans: list[list[int]] = [[i] for i in range(len(obs))]

    # Pool the high tail inward: while the LAST bin's expected < min_expected and
    # there is more than one bin, merge it down into the previous bin.
    while len(exp) > 1 and exp[-1] < min_expected:
        exp[-2] += exp[-1]
        obs[-2] += obs[-1]
        spans[-2] = spans[-2] + spans[-1]
        del exp[-1]
        del obs[-1]
        del spans[-1]

    # Guard the low end: if the first bin is still sparse, merge it upward.
    while len(exp) > 1 and exp[0] < min_expected:
        exp[1] += exp[0]
        obs[1] += obs[0]
        spans[1] = spans[0] + spans[1]
        del exp[0]
        del obs[0]
        del spans[0]

    labels: list[str] = []
    n_orig = len(observed)
    for span in spans:
        lo = span[0]
        hi = span[-1]
        # The final original bin is itself a "max_bin+" tail; any pooled span
        # that reaches it gets a "+" suffix.
        is_open_tail = hi == n_orig - 1
        if lo == hi:
            labels.append(f"{lo}+" if is_open_tail else f"{lo}")
        else:
            labels.append(f"{lo}+" if is_open_tail else f"{lo}-{hi}")
    return np.asarray(obs, dtype=np.float64), np.asarray(exp, dtype=np.float64), labels


# ---------------------------------------------------------------------------
# The chi-squared goodness-of-fit test
# ---------------------------------------------------------------------------


def run_total_distribution(run_totals, max_bin: int = 10) -> np.ndarray:
    """The per-team-game run-total histogram (a thin alias for
    :func:`bin_run_totals` named for the spec's "run distribution" vocabulary)."""
    return bin_run_totals(run_totals, max_bin=max_bin)


def chi_squared_gof(
    simulated_totals,
    reference_totals=None,
    *,
    reference_distribution=None,
    max_bin: int = 10,
    min_expected: float = 5.0,
    alpha: float = 0.05,
) -> ChiSquaredResult:
    """Run a chi-squared GOF comparing the SIMULATED run distribution to a
    reference (SIM-325 acceptance #1).

    Supply the reference EITHER as ``reference_totals`` (a sequence of actual /
    reference per-team-game run totals -- the real-data path) OR as
    ``reference_distribution`` (a normalized probability vector over the
    ``0,1,...,max_bin+`` bins -- e.g. a published league run-per-game
    distribution).  Exactly one must be given.

    Both observed and expected are binned with :func:`bin_run_totals`, the
    expected counts are scaled to the SAME total as the observed, low-expected
    tail bins are pooled (:func:`pool_low_expected_bins`), and
    ``scipy.stats.chisquare`` produces the statistic + p-value.  The degrees of
    freedom is ``#pooled_bins - 1`` (no fitted parameters -- the expected
    distribution is supplied, not estimated from the data).

    Returns a :class:`ChiSquaredResult` (statistic, dof, p_value, bin labels, and
    the post-pooling observed/expected vectors).  ``passed`` is ``p > alpha``.
    """
    if (reference_totals is None) == (reference_distribution is None):
        raise ValueError("supply exactly one of reference_totals / reference_distribution.")
    observed = bin_run_totals(simulated_totals, max_bin=max_bin)
    obs_total = float(observed.sum())
    if obs_total <= 0.0:
        raise ValueError("no simulated games to test.")

    if reference_totals is not None:
        ref_counts = bin_run_totals(reference_totals, max_bin=max_bin)
        ref_total = float(ref_counts.sum())
        if ref_total <= 0.0:
            raise ValueError("reference_totals is empty.")
        ref_probs = ref_counts / ref_total
    else:
        ref_probs = np.asarray(reference_distribution, dtype=np.float64)
        if ref_probs.shape[0] != max_bin + 1:
            raise ValueError(
                f"reference_distribution must have {max_bin + 1} bins "
                f"(0..{max_bin}+), got {ref_probs.shape[0]}."
            )
        s = float(ref_probs.sum())
        if s <= 0.0:
            raise ValueError("reference_distribution has zero total mass.")
        ref_probs = ref_probs / s

    # Expected counts: the reference probabilities scaled to the OBSERVED total so
    # sum(expected) == sum(observed) (chisquare requires equal totals).
    expected = ref_probs * obs_total
    pooled_obs, pooled_exp, labels = pool_low_expected_bins(
        observed, expected, min_expected=min_expected
    )
    # Re-balance any residual float drift so the two totals match exactly.
    pooled_exp = pooled_exp * (pooled_obs.sum() / pooled_exp.sum())

    if len(pooled_obs) < 2:
        raise ValueError(
            "after pooling only one bin remains; cannot run a chi-squared GOF "
            "(need >= 2 bins). Increase the sample size or lower min_expected."
        )

    stat, p_value = chisquare(f_obs=pooled_obs, f_exp=pooled_exp)
    dof = len(pooled_obs) - 1
    return ChiSquaredResult(
        statistic=float(stat),
        dof=int(dof),
        p_value=float(p_value),
        bins=labels,
        observed=[float(x) for x in pooled_obs],
        expected=[float(x) for x in pooled_exp],
        alpha=alpha,
    )


# ---------------------------------------------------------------------------
# Top-level convenience: replay + test in one call (the acceptance entry point)
# ---------------------------------------------------------------------------


def replay_and_test(
    historical_games: list[HistoricalGame],
    *,
    base_seed: int = 0,
    state_machine_factory=None,
    max_bin: int = 10,
    min_expected: float = 5.0,
    alpha: float = 0.05,
) -> ChiSquaredResult:
    """Replay ``historical_games`` through the loop and chi-squared-test the
    simulated run distribution against the games' ACTUAL run totals (the SIM-325
    end-to-end acceptance entry point, acceptance #1+#2).

    With the real Statcast DB this is the genuine historical validation: build the
    :class:`HistoricalGame` list from real box scores, wire a production
    ``state_machine_factory``, and the simulated distribution is compared to real
    actual run totals.  In the no-DB sandbox a self-consistency / reference run is
    used instead (see the SIM-325 tests).
    """
    actual = [int(hg.runs) for hg in historical_games]
    simulated = replay_historical_games(
        historical_games,
        base_seed=base_seed,
        state_machine_factory=state_machine_factory,
    )
    return chi_squared_gof(
        simulated,
        reference_totals=actual,
        max_bin=max_bin,
        min_expected=min_expected,
        alpha=alpha,
    )
