"""
replay_chi_squared.py
=====================
The historical-replay harness: a chi-squared goodness-of-fit (GOF) test of the
simulator's runs per team-game.

WHAT THE HARNESS DOES
---------------------
  (a) It takes a set of historical games with KNOWN runs per team-game (the
      :class:`HistoricalGame` rows, one per team-game).
  (b) It replays each matchup through ``simulate_game()`` to get a SIMULATED
      distribution of runs per team-game.
  (c) It compares the two distributions with a chi-squared GOF test. The bins
      are 0, 1, 2, ..., N+ runs. The harness pools the sparse high-run tail
      until every expected count is large enough for the test. The result
      carries the statistic, the degrees of freedom and the p-value. The gate
      is ``p > 0.05``.

WITH NO DATABASE
----------------
The tests run with no Statcast database. The default machine is the production
:class:`StateMachine` over the synthetic league bundle
(:func:`simulation.synthetic_bundle.league_artifacts`). The tests compare its
distribution with a reference drawn from the same league model (a
self-consistency check) or with a published league distribution.

WITH REAL DATA
--------------
Build :class:`HistoricalGame` rows from the real box scores: each row carries
the matchup keys and the actual runs. Pass them to :func:`replay_and_test` with
a production ``state_machine_factory``. The same chi-squared code then tests
the loop against real history.

NOISE
-----
Each replayed game has a fixed seed, so the distribution is reproducible. Tail
pooling keeps every expected count away from zero. The gate is a p-value, not
a point tolerance. A negative control (a deliberately WRONG reference) reads
p < 0.05, so the test has power.

The harness drives the loop and changes nothing in it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import chisquare

from simulation.game_state import GameState
from simulation.loop.boxscore import GameSimResult
from simulation.sim_loop import StateMachine, simulate_game
from simulation.synthetic_bundle import league_artifacts, synthetic_sampler

# ---------------------------------------------------------------------------
# The league-average outcome model
# ---------------------------------------------------------------------------
#
# The per-pitch and in-play mixes of the baseball sniff tests (SIM-324).
# Through the count machine they give about 8-9% walks, 22% strikeouts and
# 3.85 pitches per plate appearance, and about 4.4 runs a team-game. The
# harness keeps its own copy, so a caller can build a reference from the same
# model without importing a test module.

LEAGUE_PITCH_MODEL: dict[str, float] = {
    "ball": 0.332,
    "called_strike": 0.138,
    "swinging_strike": 0.087,
    "foul": 0.273,
    "in_play": 0.170,
}

# The in-play mix follows the production outcome pool's proportions: hits on
# about 33% of the balls in play that stay in the park. With the advancement
# draws it lands the run environment near the 4.4 runs a team-game target.
LEAGUE_INPLAY_MODEL: dict[str, float] = {
    "home_run": 0.049,
    "single": 0.239,
    "double": 0.071,
    "triple": 0.006,
    "field_out": 0.609,
    "grounded_into_double_play": 0.026,
}

#: The default matchup keys of the no-DB reference replay (the sniff tests' matchup).
_DEFAULT_SEASON = 2024
_DEFAULT_PITCHER = 477132
_DEFAULT_AWAY_LINEUP = list(range(101, 110))
_DEFAULT_HOME_LINEUP = list(range(201, 210))

# ---------------------------------------------------------------------------
# The real-data input: one row per team-game with a KNOWN actual run total
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoricalGame:
    """One historical team-game with its KNOWN run total: the real-data input.

    The unit is a team-game, not a game, because the test compares runs per
    team-game. ``runs`` is the runs that team scored. The other fields are the
    matchup keys the loop needs to replay the game: the pitcher faced, the
    season, the batting hand and the lineups. With real data they come from
    the box score, and a production factory replays the game over the real
    play pools. With no database they default to the sniff tests' matchup,
    and the synthetic league bundle drives the loop.

    To test real data, build ``[HistoricalGame(runs=actual, pitcher_id=...,
    ...), ...]`` from the box scores. Pass the list to :func:`replay_and_test`
    with a production ``state_machine_factory``.
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
    """The result of one chi-squared goodness-of-fit test.

    ``statistic`` is the chi-squared statistic, ``dof`` the degrees of freedom
    (``#bins - 1`` after pooling), ``p_value`` the GOF p-value, and ``bins``
    the bin labels (e.g. ``["0", "1", "2", "3+"]``) of the counts.
    ``observed`` / ``expected`` are the pooled count vectors the test read, so
    a caller can inspect exactly what it compared. ``passed`` is
    ``p_value > alpha``.
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
# The no-DB league machine: the production StateMachine over the synthetic
# league bundle
# ---------------------------------------------------------------------------

_LEAGUE_ARTIFACTS = None


def _league_artifacts():
    """The league bundle, built once; every replay draws from it."""
    global _LEAGUE_ARTIFACTS
    if _LEAGUE_ARTIFACTS is None:
        _LEAGUE_ARTIFACTS = league_artifacts(
            pitch_model=LEAGUE_PITCH_MODEL, inplay_model=LEAGUE_INPLAY_MODEL
        )
    return _LEAGUE_ARTIFACTS


def _default_state_machine(seed: int) -> StateMachine:
    """The no-DB league machine for one seeded replay game.

    It is the production machine over the synthetic league bundle, so every
    pitch and every batted ball is a full-pool draw."""
    return StateMachine(
        synthetic_sampler(_league_artifacts(), seed + 1_000_003),
        rng=np.random.default_rng(seed),
    )


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

    Each game uses its own fixed seed (``base_seed + i``), so the whole
    distribution is reproducible. ``state_machine_factory`` is
    ``factory(seed) -> StateMachine``; it defaults to the no-DB league machine.
    A production caller passes a factory that builds the machine over the real
    bundle.

    The matchup keys (pitcher, season, hand, lineups) go straight to
    :func:`simulate_game`. With the synthetic league bundle they are
    bookkeeping only: the league mix decides the outcome.
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

    The replay gives one simulated team-game per :class:`HistoricalGame`, so
    the simulated and actual samples have the same size. Each game's seed is
    its ``seed`` field when set, else ``base_seed + i``. The matchup keys come
    from the :class:`HistoricalGame`. With a production
    ``state_machine_factory`` this is the real historical replay; otherwise the
    synthetic league bundle drives it.
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
        # One team-game per historical row: the home side's runs (an arbitrary,
        # consistent choice), so |simulated| == |actual| for the GOF.
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
    """The histogram of runs per team-game: an alias of :func:`bin_run_totals`."""
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
    """Test the SIMULATED run distribution against a reference with a
    chi-squared GOF test.

    Supply the reference EITHER as ``reference_totals`` (a sequence of actual
    or reference runs per team-game -- the real-data path) OR as
    ``reference_distribution`` (a probability vector over the
    ``0,1,...,max_bin+`` bins -- e.g. a published league distribution of runs
    per game). Give exactly one.

    The function bins both sides with :func:`bin_run_totals` and scales the
    expected counts to the observed total. It pools the low-expected tail bins
    (:func:`pool_low_expected_bins`), and ``scipy.stats.chisquare`` gives the
    statistic and the p-value. The degrees of freedom are
    ``#pooled_bins - 1``: the caller supplies the expected distribution, so
    the test fits no parameter.

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
# Replay and test in one call
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
    """Replay ``historical_games`` through the loop and test the simulated run
    distribution against the games' ACTUAL run totals.

    With real data this is the historical validation: build the
    :class:`HistoricalGame` list from the real box scores and pass a production
    ``state_machine_factory``. With no database the tests run a
    self-consistency or reference check instead
    (``tests/unit/test_replay_chi_squared.py``).
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
