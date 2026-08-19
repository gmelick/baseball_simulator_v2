"""SIM-450 — the acceptance lane: the PRODUCTION simulator against real MLB rates.

WHAT THIS LANE IS
=================
It runs the simulator with every production flag ON, measures thirteen box-score
channels, and asserts each one against a band around the real MLB rate. It is the
only test in this repository that drives the simulator users get.

WHY IT EXISTS
=============
``tests/conftest.py`` pins ``SIM_FULL_POOL``, ``SIM_MANAGER``, ``SIM_PARK_FACTOR``,
``SIM_BB_PLATOON``, ``SIM_FIELDER_RBF`` and ``SIM_HOME_FIELD_BIAS`` OFF at import
time (lines 33, 39, 45, 51, 52, 53). Production sets the exact inverse. The four
methods that shape every production pitch had ZERO test references anywhere in
the repo:

  * ``_full_pool_outcome``          (``simulation/sim_loop.py:1332``)
  * ``_full_pool_fielding``         (``simulation/sim_loop.py:1386``)
  * ``_full_pool_out_advancement``  (``simulation/sim_loop.py:1507``)
  * the steal decision — ``_steal_opportunity_draw`` since SIM-474
    (previously the unreachable ``_full_pool_steal_decision``)

That is how four confirmed production defects survived eight weeks. This lane
calls all four and asserts each one runs.

THE BAND RULE
=============
``half_width = max(Z * sd / sqrt(n), floor)`` with ``Z = 4.0``, ``sd`` measured
from the run itself, and a per-channel floor. A channel earns a PASS only when
the run is long enough, its mean sits inside the half-width, AND the floor is the
binding term. Four verdicts::

    UNDERPOWERED  the run is shorter than this channel needs. No verdict.
    FAIL          long enough, and the model sits outside the band.
    UNRESOLVED    inside the band, but this run is noisier than MLB.
    PASS          long enough, inside, and the floor binds.

``tests/acceptance/bands.py`` owns the rule, the reference rates, their
provenance, and the full arithmetic. Read it before you read a result.

WHAT THE 2026-08-10 ROUND-3 REVIEW CHANGED
==========================================
Round 2 repaired two floors. A third review found it had sized both against
numbers that were not the ones on record, and that a third hole sat underneath.

  * **R was sized on a stale line.** Round 2 used ``CLAUDE.md:465``'s "runs sit
    ~10-12% low" and shipped a 7.5% floor. The owner ruled that line STALE on
    2026-08-10: ``CLAUDE.md:85`` records the live gap as "Runs run ~7-8% low".
    The 7.5% floor PASSED at 7.0%, 7.2%, 7.4% and 7.5% low — most of the live
    range. The floor is now **2.76%**.
  * **home_win_pct rejected only a coin flip.** The 0.030 floor failed at exactly
    0.5000 and PASSED at 0.5100, 0.5125 and 0.5150. ``CLAUDE.md:400`` records the
    defect as "stuck at the structural-only ~.510-.515", and this lane's own
    first production run measured **0.5125**. So the band passed on the whole
    documented defect AND on the lane's own reading. The floor is now **0.0124**.
  * **A two-observation sample could pass.** ``evaluate("H", [8.0, 8.0])``
    returned ``passed=True``. A minimum-sample gate now returns UNDERPOWERED
    below each channel's own requirement.
  * **Every other floor was re-derived**, either from a documented magnitude
    (SIM-494 / SIM-495 / SIM-496) or, where none exists, from the tightest
    deviation the certifying run resolves. The table with each file and line is
    in ``bands.py``.
  * **A thirteenth channel, ``ROE_reached``.** ``BACKLOG.md:20`` requires it.
  * **The game order is fixed.** ``conftest.ACCEPTANCE_GAME_PKS`` IS
    ``bands.BALANCED_GAME_ORDER`` now, so a shortened run measures the same run
    environment as the full set.

THE COST, AND WHY IT IS NOT NEGOTIABLE
======================================
The old floors certified the box channels in 696 game-sims. The new ones need
**5,100** — 12 games x 425 iterations, about 3.2 hours at the measured 2.25 s per
sim. ``home_win_pct`` needs **26,015 decisive games**, about 16.3 hours.

That is the honest price. A floor loose enough to certify home-field advantage
overnight cannot reject the 0.515 baseline ``CLAUDE.md:400`` documents, and a
band that cannot reject the documented defect is the failure this round exists to
end. So:

    **A nightly-length run certifies nothing. The box lane is a 3.2-hour job and
    home_win_pct is a weekend job.** Both report UNDERPOWERED below that, which
    is a missing measurement, not a pass.

SIM-467 (the 2,880-cell filter index, roughly 1000x less per-draw work) would
make both routine. Until it lands the hours above are real.

THE DAY-ONE READING — AND WHY NONE OF IT IS A VERDICT ANY MORE
==============================================================
I ran this lane once, on 2026-08-10, at 8 games x 50 iterations = 400 game-sims
(n = 800 team-games, 901.6 s, 2.25 s per sim) inside the app container. Every
flag was confirmed ON at run time and the SIM-449 kwargs were confirmed complete.

**Three caveats now sit over the whole table, and together they mean the lane has
never produced a single conclusive channel verdict.**

  1. It ran on the biased ASCENDING 8-game prefix, so it measured a run
     environment with a mean park run factor of 0.96844 rather than the set's
     0.99952. Every offensive channel reads about 3% low for that reason alone.
  2. Under the round-3 floors, 800 team-games is **UNDERPOWERED on every box
     channel** — they need about 10,200 — and about 790 decisive games is
     UNDERPOWERED on ``home_win_pct``, which needs 26,015.
  3. The verdict column predates both the resolution rule and the minimum-sample
     gate.

Read the table as a smoke reading. It is kept because it is what produced
SIM-494, SIM-495 and SIM-496, not because it certifies anything::

    channel          sim      MLB    delta   round-3 floor   status TODAY
    R             4.4413   4.6200    -3.9%      0.1275 ( 2.8%)  UNDERPOWERED
    H             9.2650   8.6000    +7.7%      0.1359 ( 1.6%)  UNDERPOWERED
    HR            1.3113   1.2100    +8.4%      0.0462 ( 3.8%)  UNDERPOWERED
    2B            1.6425   1.6000    +2.7%      0.0533 ( 3.3%)  UNDERPOWERED
    3B            0.1400   0.1400    +0.0%      0.0155 (11.1%)  UNDERPOWERED
    BB            3.6800   3.3000   +11.5%      0.0802 ( 2.4%)  UNDERPOWERED
    K             8.6700   8.6000    +0.8%      0.1178 ( 1.4%)  UNDERPOWERED
    SB            0.0000   0.5900  -100.0%      0.0361 ( 6.1%)  UNDERPOWERED
    CS            0.0000   0.1700  -100.0%      0.0110 ( 6.5%)  UNDERPOWERED
    DP            0.1600   0.8160   -80.4%      0.0344 ( 4.2%)  UNDERPOWERED
    ROE           0.2437   0.2193   +11.1%      0.0188 ( 8.6%)  UNDERPOWERED
    ROE_reached      n/a   0.2193       —       0.0188 ( 8.6%)  never measured
    home_win_pct  0.5125   0.5350    -4.2%      0.0124 ( 2.3%)  UNDERPOWERED

    calls: _full_pool_outcome 123,205  _full_pool_fielding 21,443
           _full_pool_out_advancement 14,226  _full_pool_steal_decision 0

This SUPERSEDES an earlier 32-sim probe run through ``scripts/sim_stats.py``,
which reported HR at -25% and BB at +24%. That probe was too small and predated
the SIM-449 fix, so it passed an empty defense map and a neutral park factor. Do
not cite it.

WHICH CHANNELS CARRY AN XFAIL, AND WHICH DELIBERATELY DO NOT
============================================================
A lane that is red on delivery gets muted, and widening a band to force green is
the anti-pattern this file forbids. A channel whose defect is DOCUMENTED WITH A
TICKET ships as ``@pytest.mark.xfail(strict=True)``:

  * the defect is still open  -> XFAIL -> the lane is green and the report still
    names the channel;
  * the defect is fixed       -> XPASS(strict) -> the lane goes RED and the
    engineer must delete the marker.

Band assertions whose defect is open carry the marker, each naming its
ticket. The 2026-08-19 state: NO channel carries a marker. The last three
(H / DP / ROE_reached, all SIM-494/496) were deleted with the SIM-511+512
transition-draw landing — the drawn row IS the play, so a double play removes
its runner, a reach-on-error puts the batter on base, and field_error is its
own canonical outcome (never a hit). Earlier deletions: R after the SIM-459
rebuild measured it IN band; SB / CS / the steal call-count when SIM-474
landed. The 2026-08-16 table, kept for history::

    H             SIM-496   BACKLOG.md:20  a retired batter is credited a hit
    DP            SIM-494   BACKLOG.md:18  measured 0.1600 against 0.8160
    ROE_reached   SIM-496   BACKLOG.md:20  nothing reaches base on an error

**HR, 2B, 3B, BB, K, ROE and home_win_pct carry NO xfail, on purpose.** Their
day-one numbers put HR, BB and ROE outside the round-3 floors, and BB at +11.5%
is the largest unexplained deviation in the lane. But that reading was
UNDERPOWERED, so claiming it as a known failure would be inventing a measurement
I do not have. If a certifying run reds one of them:

    **File a ticket. Do not widen the floor.** An un-ticketed red is a finding.
    ``bands.py`` "MOVING A BAND" is the procedure, and it requires a model change
    with a measurement behind it before any floor moves.

``home_win_pct`` has its own reason, below.

THE ZEROED-STEALS ROOT CAUSE — FIXED BY SIM-474 (2026-08-16), kept for history
-------------------------------------------------------------------------------
From 2026-06-04 to 2026-08-16 ``SIM_MANAGER=1`` disabled every steal: the
default profile set ``steal_order_rate_per_1b_opp = 0.08``, so the green-light
gate was "on", so control skipped the SIM-426 fallback and reached the base
``PlayResolver.resolve_steal`` stub, which always answered ``attempted=False``
(SIM-495 measured SB 0.0000 against 0.59 across 400 game-sims). SIM-474
deleted the gate, the fallback and its tuned ``_STEAL_ATTEMPT_K``: the steal
decision is now a similarity-weighted draw from the SIM-468 opportunity pool
(``_steal_opportunity_draw`` -> ``FullPoolSampler.steal_draw``), where manager
aggression is a WEIGHT on attempted rows — never a gate — and the drawn row's
``attempted``/``success`` flags answer both questions at once.

WHY THERE ARE TWO REACH-ON-ERROR CHANNELS
=========================================
``BACKLOG.md:20`` (SIM-496) records that ``_full_pool_fielding`` infers
``outs = 0 if int(rh) > 0 else 1`` (``sim_loop.py:1432``) and a pool
``field_error`` row carries ``result_hits = 0``. So every drawn reach on error
becomes a one-out ``field_out`` and the batter never reaches base, while
``simulation/constants.py:177`` credits him a hit anyway — retired on the bases,
credited at the plate.

The day-one ROE band PASSED at +11.1%, and **that pass is evidence FOR the
defect, not against it**. The probe counts the DRAWN event, which happens before
the loop discards the play. No floor on that channel can ever red on SIM-496.

``ROE_reached`` counts batters who actually reached, at the single commit point
``_commit_run_delta``. It reads zero today. Keep both channels: a green ROE
beside a red ROE_reached localises the fault to the loop rather than the pool,
and neither channel says that alone.

WHY home_win_pct HAS NO XFAIL
=============================
``CLAUDE.md:400`` documents the structural-only baseline at ~.510-.515, and the
band is sized to reject it. The day-one run read 0.5125 — squarely in that range
— which would suggest the SIM-412 home-field bias is not acting.

I am NOT claiming that. Roughly 790 decisive games carry a standard error of
about 0.018 on this channel, so 0.5125 is consistent with anything from about
0.48 to 0.55. The reading proves nothing, and ``docs/audit/2026-07-23-MASTER-BUG-
REGISTER.md:232`` (AUD-HFA) argues the opposite direction — that the bias is
knowingly OVERSHOT at 0.025 against a measured retune of 0.017.

So the channel ships with no marker. Below 26,015 decisive games it reports
UNDERPOWERED and prints the run it needs. That is the truthful state: nobody has
measured home-field advantage in this simulator at a size that can resolve it.

A green home_win_pct will say "not the bias-off baseline". It will not say the
SIM-412 magnitude is right.

RUNNING IT
==========
Three sizes, and they certify different things. ``bands.binding_requirement``
computes the two thresholds; do not copy the numbers::

    # FULL: certifies all thirteen channels. 12 x 2168 = 26,016 game-sims,
    # about 16.3 hours. A weekend job.
    MSYS_NO_PATHCONV=1 docker compose run --rm \
        -v "$PWD/tests:/app/tests" -v "$PWD/scripts:/app/scripts" \
        -e SIM_ACCEPTANCE=1 -e SIM_ACCEPTANCE_ITERS=2168 \
        -e SIM_ACCEPTANCE_TIMEOUT=64800 app pytest tests/acceptance -v -rxX

    # BOX ONLY: certifies the twelve per-team-game channels, about 3.2 hours.
    # home_win_pct reports UNDERPOWERED. This is the 12 x 425 default.
    ... -e SIM_ACCEPTANCE=1 -e SIM_ACCEPTANCE_TIMEOUT=14400 app pytest ...

    # SMOKE: 2 games x 5 iterations, measured 26 s. Certifies nothing, and
    # test_sample_is_large_enough_to_mean_anything_sim450 fails on purpose.
    ... -e SIM_ACCEPTANCE=1 -e SIM_ACCEPTANCE_GAMES=2 -e SIM_ACCEPTANCE_ITERS=5 ...

Both mounts are load-bearing. ``tests/`` is obvious. ``scripts/`` is NOT
bind-mounted by default (CLAUDE.md section 2a) and the lane parses
``scripts/sim_stats.py`` to check its own MLB constants, so without that mount it
reads a months-old image-baked copy. Measured 2026-08-10: the image held
``_MLB_2023`` at line 69 and the repo held it at line 80, and the drift test
failed on the difference.

The default 7,200 s timeout is too short for either certifying size, so raise
``SIM_ACCEPTANCE_TIMEOUT`` with the iteration count.

Use all twelve games for any certifying run. A shortened prefix is park-balanced
to within 0.0126, which is 46% of the round-3 R floor — small enough not to
invert a verdict, large enough that it is no longer negligible. See
``test_a_shortened_run_cannot_hide_a_park_shift_in_the_R_band_sim450``.

Owner: QA / DevOps (SIM-450).
"""

from __future__ import annotations

import os

import pytest

from tests.acceptance import bands
from tests.acceptance.conftest import (
    DELETED_FLAGS,
    MIN_MEANINGFUL_SIMS,
    PRODUCTION_FLAGS,
    AcceptanceRun,
    opted_in,
)

# The opt-in guard. ``make test`` runs ``pytest tests/``, so this module has to
# refuse to collect without the data. The marker alone would not do it.
if not opted_in():
    pytest.skip(
        "SIM-450 acceptance lane needs the engine-artifact bundle, a read-only DuckDB "
        "and Postgres. Opt in with SIM_ACCEPTANCE=1.",
        allow_module_level=True,
    )


def _timeout_s() -> int:
    raw = os.environ.get("SIM_ACCEPTANCE_TIMEOUT", "").strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return 7200


# The 12 x 425 box run takes about 3.2 hours and the 12 x 2168 run that also
# certifies home_win_pct takes about 16.3 hours. The default 7,200 s ceiling
# covers NEITHER, so raise SIM_ACCEPTANCE_TIMEOUT whenever you raise
# SIM_ACCEPTANCE_ITERS. The unit lane's ``--timeout=30`` would kill either, and a
# per-test marker overrides the command line.
pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(_timeout_s())]


# ---------------------------------------------------------------------------
# The configuration the lane actually ran under
# ---------------------------------------------------------------------------


def test_production_flags_are_on_sim450() -> None:
    """The override beat ``tests/conftest.py``. Everything else depends on this.

    If this fails, every band below measured the per-tile simulator and labelled
    the result "production". That is the failure this ticket exists to end, so it
    is asserted first and on its own.
    """
    for name, expected in PRODUCTION_FLAGS.items():
        assert os.environ.get(name) == expected, (
            f"{name}={os.environ.get(name)!r}, expected {expected!r}. "
            "tests/conftest.py pinned it off and the package fixture did not win."
        )
    for name in DELETED_FLAGS:
        assert name not in os.environ, (
            f"{name} is set to {os.environ[name]!r}; production leaves it unset so the "
            "SIM-412 class default of 0.025 applies."
        )


def test_sample_is_large_enough_to_mean_anything_sim450(acceptance_run: AcceptanceRun) -> None:
    """Refuse to certify a run too small for the box channels' floors to bind.

    Below the binding requirement every channel returns UNDERPOWERED: the run
    cannot fail on model accuracy and it cannot pass either. A run that small is
    a smoke test. It must not be read, or reported, as a certification.

    This gate covers the twelve per-team-game channels. ``home_win_pct`` needs
    about 5x the run — one observation per decisive game instead of two per
    game-sim — and enforces that itself through its own UNDERPOWERED verdict, so
    a 12 x 425 box run still certifies twelve of thirteen channels.
    """
    box_channel, box_required = bands.binding_requirement(bands.BOX_CHANNELS)
    lane_channel, lane_required = bands.binding_requirement()
    assert acceptance_run.total_sims >= box_required, (
        f"{acceptance_run.total_sims} game-sims is a smoke run, not an acceptance run. "
        f"The box channels need {box_required} game-sims before their floors bind "
        f"({box_channel} is the binding one), so nothing here can pass OR fail on model "
        f"accuracy. Raise SIM_ACCEPTANCE_GAMES / SIM_ACCEPTANCE_ITERS and "
        f"SIM_ACCEPTANCE_TIMEOUT with them. "
        f"(conftest.MIN_MEANINGFUL_SIMS is {MIN_MEANINGFUL_SIMS}; the whole lane "
        f"including {lane_channel} needs {lane_required}.)"
    )


def test_the_game_set_does_not_bias_the_run_environment_sim450(
    acceptance_run: AcceptanceRun,
) -> None:
    """The games actually simulated must represent the whole set's park factors.

    ``conftest.py`` slices ``ACCEPTANCE_GAME_PKS[:n_games]``. That tuple IS
    ``bands.BALANCED_GAME_ORDER`` since 2026-08-10, so every prefix of four or
    more games is park-balanced. Before that it was ASCENDING by park run factor
    and an 8-game run took the eight most pitcher-friendly parks — mean 0.96844
    against 0.99952 — so every offensive band judged the model on a run
    environment it was never asked to reproduce.

    This asserts the park factors the run RESOLVED, not a hard-coded table, so it
    also catches a game set that drifts away from ``bands.ACCEPTANCE_PARK_FACTORS``.
    """
    resolved = acceptance_run.park_factors
    assert resolved, "no game was resolved"

    unknown = sorted(set(resolved) - set(bands.ACCEPTANCE_PARK_FACTORS))
    assert not unknown, (
        f"games {unknown} are not in bands.ACCEPTANCE_PARK_FACTORS, so the reference table "
        "is stale. Re-read derived.park_factors (season 2024, factor_type='R') and update it."
    )
    for game_pk, factor in resolved.items():
        expected = bands.ACCEPTANCE_PARK_FACTORS[int(game_pk)]
        assert factor == pytest.approx(expected, abs=5e-4), (
            f"game {game_pk} resolved park_run_factor {factor:.4f}, but DuckDB held "
            f"{expected:.4f} on 2026-08-10. The park-factor resolution changed."
        )

    run_mean = bands.mean_park_factor(list(resolved))
    set_mean = bands.mean_park_factor(bands.BALANCED_GAME_ORDER)
    assert abs(run_mean - set_mean) <= bands.MAX_PREFIX_PARK_BIAS, (
        f"the {len(resolved)} games simulated have a mean park run factor of {run_mean:.5f} "
        f"against {set_mean:.5f} for the full set — a {run_mean - set_mean:+.5f} shift, over "
        f"the {bands.MAX_PREFIX_PARK_BIAS} tolerance. Every offensive band is reading a "
        "different run environment from the one it is centred on. Run all 12 games."
    )


def test_sim449_inputs_reach_the_simulator_sim450(acceptance_run: AcceptanceRun) -> None:
    """The park factor and the two defense maps are really wired (SIM-449 guard).

    The old harness dropped ``home_defense``, ``away_defense`` and
    ``park_run_factor``, so ``SIM_FIELDER_RBF`` and ``SIM_PARK_FACTOR`` were
    structurally inert. An A/B test of either flag then compared two identical
    no-ops and reported "no effect". This lane fails rather than repeat that.
    """
    assert acceptance_run.defense_sizes, "no game was resolved"
    for game_pk, (home_n, away_n) in acceptance_run.defense_sizes.items():
        assert home_n and away_n, (
            f"game {game_pk} passed an EMPTY defense map "
            f"(home={home_n}, away={away_n}); SIM_FIELDER_RBF cannot act."
        )
    non_neutral = [pk for pk, pf in acceptance_run.park_factors.items() if pf != 1.0]
    assert non_neutral, (
        "every park_run_factor is a neutral 1.0, so SIM_PARK_FACTOR is a no-op across "
        f"the whole game set: {acceptance_run.park_factors}"
    )


def test_every_band_channel_was_measured_sim450(acceptance_run: AcceptanceRun) -> None:
    """The run produced observations for every channel ``bands.py`` declares.

    A channel added to ``bands.CHANNELS`` and never wired into the fixture would
    otherwise vanish: ``_assert_band`` would fail with "no observations" on one
    test and the report would print "no data", both easy to skim past. This says
    it once, plainly.
    """
    missing = [c for c in bands.CHANNELS if not acceptance_run.observations.get(c)]
    assert not missing, (
        f"bands.py declares {len(bands.CHANNELS)} channels and the run measured none for "
        f"{missing}. Wire them into tests/acceptance/conftest.py::acceptance_run."
    )


def test_steal_totals_reconcile_sim450(acceptance_run: AcceptanceRun) -> None:
    """Every stolen base the boxscore reported was attributed to a team.

    SB and CS are credited to the RUNNER, so they partition by lineup membership
    rather than by the batting side. A pinch runner from outside both lineups
    would be dropped from both teams and quietly shrink the SB band's numerator.
    """
    assert acceptance_run.unattributed_sb == 0, (
        f"{acceptance_run.unattributed_sb} stolen bases belong to no lineup; the SB band "
        "is measuring an undercount."
    )
    assert acceptance_run.unattributed_cs == 0, (
        f"{acceptance_run.unattributed_cs} caught-stealings belong to no lineup; the CS "
        "band is measuring an undercount."
    )


# ---------------------------------------------------------------------------
# The production methods that had zero test references
# ---------------------------------------------------------------------------


def _assert_called(run: AcceptanceRun, method: str) -> None:
    count = run.calls.get(method, 0)
    assert count > 0, (
        f"{method} was never called across {run.total_sims} production game-sims. "
        "Either the full-pool path is not wired or a gate above it is closed."
    )


def test_full_pool_outcome_runs_sim450(acceptance_run: AcceptanceRun) -> None:
    """``_full_pool_outcome`` (sim_loop.py:1332) drives every production pitch."""
    _assert_called(acceptance_run, "_full_pool_outcome")


def test_full_pool_fielding_runs_sim450(acceptance_run: AcceptanceRun) -> None:
    """``_full_pool_fielding`` (sim_loop.py:1386) resolves every batted ball."""
    _assert_called(acceptance_run, "_full_pool_fielding")


def test_full_pool_out_advancement_runs_sim450(acceptance_run: AcceptanceRun) -> None:
    """``_full_pool_out_advancement`` (sim_loop.py:1507) moves runners on outs."""
    _assert_called(acceptance_run, "_full_pool_out_advancement")


def test_commit_run_delta_runs_sim450(acceptance_run: AcceptanceRun) -> None:
    """``_commit_run_delta`` (sim_loop.py:1567) is the ROE_reached measuring point.

    If this is zero the ``ROE_reached`` channel is reading a probe that never
    fired, and a zero there would mean "not measured" rather than "nothing
    reached". The two must not be confusable.
    """
    _assert_called(acceptance_run, "_commit_run_delta")


# The SIM-495 expected-red xfail lived here from 2026-08-10 to 2026-08-16:
# _full_pool_steal_decision was measured at 0 calls across 400 production
# game-sims because the green-light gate routed control to a resolver stub.
# SIM-474 deleted the gate and the fallback; the steal decision is now the
# SIM-468 opportunity-pool draw, reached on every stealable-runner pitch.
def test_steal_opportunity_draw_runs_sim450(acceptance_run: AcceptanceRun) -> None:
    """``_steal_opportunity_draw`` is reached in the production configuration."""
    _assert_called(acceptance_run, "_steal_opportunity_draw")


# ---------------------------------------------------------------------------
# The per-channel bands
# ---------------------------------------------------------------------------


def _assert_band(run: AcceptanceRun, channel: str) -> None:
    values = run.observations.get(channel) or []
    assert values, f"the run produced no observations for {channel}"
    result = bands.evaluate(channel, values)
    assert result.passed, result.explain()


# The SIM-429 expected-red xfail marker lived here from 2026-08-10 to
# 2026-08-16. The 2026-08-15 lane (12 x 425 on the SIM-459-recomputed data)
# measured R INSIDE the band and the strict marker XPASSed, which is the
# marker's own deletion condition. The floor stays sized to reject a 7%
# shortfall at 2.54x — it must catch a REGRESSION to the old gap.
def test_runs_band_sim450(acceptance_run: AcceptanceRun) -> None:
    """Runs per team per game. The channel the whole betting surface rests on.

    The floor was 15% on delivery and 7.5% after round 2. Both returned PASS on
    the platform's own documented run-conversion gap, on the channel the betting
    product rests on. It is 2.76% since round 3, sized on ``CLAUDE.md:85``.
    ``test_the_R_band_reds_at_exactly_seven_percent_sim450`` in
    ``test_band_arithmetic_sim450.py`` holds that property from now on.

    The likely fixes are SIM-473 (replace ``_full_pool_out_advancement``, under
    which a runner on first cannot advance on any out) and SIM-458 (correct the
    run-expectancy matrix that feeds it).
    """
    _assert_band(acceptance_run, "R")


# The SIM-496 expected-red xfail lived here from 2026-08-10 to 2026-08-19:
# constants.py aliased field_error to the canonical single, crediting a
# retired batter a hit. The SIM-511 landing made field_error its own
# canonical outcome — a reach, never a hit — so the alias and the marker
# went together.
def test_hits_band_sim450(acceptance_run: AcceptanceRun) -> None:
    """Hits per team per game. A reach on error is NOT counted (SIM-496/511)."""
    _assert_band(acceptance_run, "H")


def test_home_runs_band_sim450(acceptance_run: AcceptanceRun) -> None:
    """Home runs per team per game.

    No xfail: no ticket sizes a HR defect. The day-one reading was +8.4% against
    a round-3 floor of 3.8%, but that reading was UNDERPOWERED, so it is not a
    known failure. If a certifying run reds this, file a ticket — do not widen
    the floor.
    """
    _assert_band(acceptance_run, "HR")


def test_doubles_band_sim450(acceptance_run: AcceptanceRun) -> None:
    """Doubles per team per game. No documented defect; Rule-B floor."""
    _assert_band(acceptance_run, "2B")


def test_triples_band_sim450(acceptance_run: AcceptanceRun) -> None:
    """Triples per team per game.

    The loosest floor in the lane at 11.1%, because triples are rare: the
    channel's spread is 2.8x its centre, so it costs the most to measure.
    """
    _assert_band(acceptance_run, "3B")


def test_walks_band_sim450(acceptance_run: AcceptanceRun) -> None:
    """Walks per team per game. The largest unexplained deviation in the lane.

    The day-one run measured +11.5% against a round-3 floor of 2.4%. No ticket
    sizes it, so there is no xfail. Two things make a red here worth chasing
    rather than dismissing: over-walking feeds the pitcher BB prop that
    ``CLAUDE.md`` already records as over-predicted (ECE 0.21), and SIM-456
    (``whiff_rate`` measures called strikes) is an unsized candidate cause.

    Read ``bands.py`` first: the 3.30 centre is itself 5.0% above this project's
    own 2023 data (3.1434), which is twice this floor. A red BB may be the centre
    rather than the model.
    """
    _assert_band(acceptance_run, "BB")


def test_strikeouts_band_sim450(acceptance_run: AcceptanceRun) -> None:
    """Strikeouts per team per game, counted for the batting side.

    The tightest floor in the lane at 1.4%. SIM-456 plausibly moves this channel;
    nobody has sized it, so there is no xfail.
    """
    _assert_band(acceptance_run, "K")


# The SIM-495 expected-red xfail (SB measured 0.0000 against 0.59, -100%)
# lived here from 2026-08-10 to 2026-08-16. SIM-474 replaced the gated stub
# with the opportunity-pool draw; the band now measures a live channel.
def test_stolen_bases_band_sim450(acceptance_run: AcceptanceRun) -> None:
    """Stolen bases per team per game."""
    _assert_band(acceptance_run, "SB")


# The SIM-495 expected-red xfail (CS measured 0.0000 against 0.17) lived here
# from 2026-08-10 to 2026-08-16; same root cause and same SIM-474 fix as SB.
def test_caught_stealing_band_sim450(acceptance_run: AcceptanceRun) -> None:
    """Caught stealing per team per game."""
    _assert_band(acceptance_run, "CS")


# The SIM-494 expected-red xfail lived here from 2026-08-10 to 2026-08-19:
# the phantom-DP guard relabeled ~55% of drawn double plays and no runner was
# ever removed (0.1600 measured against 0.8160). The SIM-511 landing draws a
# transition row hard-filtered to the live base-out cell, so every drawn DP
# arrives with a real runner to retire and the guard is bypassed.
def test_double_play_band_sim450(acceptance_run: AcceptanceRun) -> None:
    """Double plays turned against the batting team, per team per game.

    Counted from the ``_full_pool_fielding`` signal: the drawn event is in
    ``_DOUBLE_PLAY_EVENTS`` AND the play recorded at least two outs. The
    second condition kept the phantom-DP-guard era honest and stays as
    defense in depth on the legacy path.
    """
    _assert_band(acceptance_run, "DP")


def test_reach_on_error_drawn_band_sim450(acceptance_run: AcceptanceRun) -> None:
    """Reaches on error the POOL DREW, per team per game.

    NO XFAIL, AND THIS CHANNEL CANNOT SEE SIM-496. It is measured at the
    ``_full_pool_fielding`` boundary, which is before the loop turns the reach on
    error into an out. A green result here proves only that the pool supplies
    errors at about the right rate. ``test_reach_on_error_reached_band_sim450``
    below is the channel that can register the defect.

    The day-one run measured +11.1% against a round-3 floor of 8.6%. No ticket
    sizes a drawn-ROE defect, so a red here is a new finding: file a ticket.
    """
    _assert_band(acceptance_run, "ROE")


# The SIM-496 expected-red xfail lived here from 2026-08-10 to 2026-08-19:
# the out-inference turned every drawn reach-on-error into a one-out
# field_out (ROE_reached 0.0000 against 0.2193). The SIM-511 landing applies
# the drawn row's batter destination, so the batter actually reaches.
def test_reach_on_error_reached_band_sim450(acceptance_run: AcceptanceRun) -> None:
    """Batters who ACTUALLY reached on an error, per team per game (SIM-496).

    Counted at ``_commit_run_delta`` (``sim_loop.py:1567``), the single point the
    loop turns a play into a base-out delta: the commit carries the
    ``field_error`` event, records no out, and credits at least one base. Both
    reach-on-error sites pass through it — the in-play commit at ``:2378`` and the
    dropped-third-strike commit at ``:1989``.

    ``test_commit_run_delta_runs_sim450`` asserts the probe fired, so a zero here
    means "nothing reached", never "nothing was measured".
    """
    _assert_band(acceptance_run, "ROE_reached")


def test_home_win_pct_band_sim450(acceptance_run: AcceptanceRun) -> None:
    """Home-team win percentage over decisive games.

    THIS CHANNEL IS RED BELOW 26,015 DECISIVE GAMES, AND THAT IS THE FIX WORKING.
    It reports UNDERPOWERED because no shorter run can separate the
    structural-only baseline ``CLAUDE.md:400`` documents (~.510-.515) from MLB's
    0.535 at Z=4. Until round 3 this channel reported PASS at 0.5000, 0.5100,
    0.5125 and 0.5150 — the whole documented defect. The failure message carries
    the run length it needs.

    There is NO xfail here on purpose. The day-one run read 0.5125, which sits in
    the bias-off range, but roughly 790 decisive games carry a standard error of
    about 0.018 on this channel, so that reading is consistent with anything from
    0.48 to 0.55. An xfail would claim a measurement nobody has taken. See "WHY
    home_win_pct HAS NO XFAIL" in the module docstring.

    A green result will say "not the bias-off baseline". It will NOT say the
    SIM-412 home-field magnitude is correctly sized — AUD-HFA
    (docs/audit/2026-07-23-MASTER-BUG-REGISTER.md:232) argues it overshoots.
    """
    _assert_band(acceptance_run, "home_win_pct")


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def test_report_every_channel_sim450(acceptance_run: AcceptanceRun) -> None:
    """Print the whole band table, pass or fail (SIM-450).

    A green tick that teaches nothing is the failure mode this lane exists to
    end. This test always passes; its job is to put every channel's arithmetic in
    the log and, on a GitHub runner, in the job summary. Read it, do not trust the
    tick.
    """
    park_mean = bands.mean_park_factor(list(acceptance_run.park_factors)) or 0.0
    lines = [
        f"SIM-450 acceptance bands — {acceptance_run.n_games} games x "
        f"{acceptance_run.n_iters} iters = {acceptance_run.total_sims} game-sims "
        f"in {acceptance_run.elapsed_s:.1f}s",
        f"flags: {' '.join(f'{k}={v}' for k, v in PRODUCTION_FLAGS.items())} "
        f"SIM_HOME_FIELD_BIAS=<unset>",
        f"calls: {acceptance_run.calls}",
        f"ties (excluded from home_win_pct): {acceptance_run.ties}",
        f"run environment: mean park run factor {park_mean:.5f} "
        f"(full set {bands.mean_park_factor(bands.BALANCED_GAME_ORDER):.5f})",
        "",
        f"{'channel':<14}{'sim':>9}{'MLB':>9}{'delta':>10}{'half':>9}"
        f"{'driver':>8}{'n':>8}{'needs':>8}  verdict",
    ]
    inconclusive: list[str] = []
    for channel in bands.CHANNELS:
        values = acceptance_run.observations.get(channel) or []
        if not values:
            lines.append(f"{channel:<14}{'no data':>9}")
            inconclusive.append(f"{channel} (no data)")
            continue
        r = bands.evaluate(channel, values)
        lines.append(
            f"{channel:<14}{r.mean:>9.4f}{r.centre:>9.4f}{r.rel_delta:>+9.1%}"
            f"{r.half_width:>10.4f}{r.driver:>8}{r.n:>8}{r.required_sims:>8}  "
            f"{r.verdict}"
        )
        if not r.conclusive:
            inconclusive.append(f"{channel} ({r.verdict}, needs {r.required_sims} sims)")
    lines.append("")
    if inconclusive:
        lines.append(
            "NOT A RESULT — these channels said nothing about the model. UNDERPOWERED "
            "means the run was too short for the channel's floor to bind; UNRESOLVED "
            "means this run was noisier than MLB. Neither is a pass and neither is a "
            "failure: " + ", ".join(inconclusive)
        )
    lines.append(
        "'needs' is the game-sim count at which the channel's floor binds. "
        "Band rule: half = max(Z*sd/sqrt(n), floor), Z=4.0. PASS also requires n to reach "
        "'needs' AND the floor to bind. See tests/acceptance/bands.py."
    )
    report = "\n".join(lines)
    print("\n" + report)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("## SIM-450 acceptance bands\n\n```\n" + report + "\n```\n\n")
