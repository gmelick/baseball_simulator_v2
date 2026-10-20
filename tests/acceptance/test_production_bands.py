"""The acceptance lane (SIM-450): the PRODUCTION simulator against reference rates.

WHAT THIS LANE IS
=================
It runs the simulator with every production flag ON, measures the box-score
channels and the pool-referenced frequency channels, and asserts each against a
band. It is the only test in this repository that drives the simulator users get:
``tests/conftest.py`` pins several production flags OFF for the unit lane, and
``conftest.py`` in this package turns them back on (its docstring says how).

The lane also asserts that the production methods it measures really run
(``_full_pool_outcome``, ``_full_pool_fielding``, ``_resolve_in_play_transition``,
``_commit_run_delta`` and ``_steal_opportunity_draw``): a method that never runs
measures nothing, and its channels would read as quiet rather than broken.

THE TWO KINDS OF BAND
=====================
* **The pool bands** (``bands.POOL_REFERENCES``, owner ruling 2026-08-20) are the
  certification. The simulator is a similarity-weighted sampler of the play pool,
  so its frequencies are graded against the pool's own totals: a faithful sampler
  is green by construction, and a red is a mechanism defect, never an era gap.
* **The game bands** (``bands.REFERENCES``) grade per-team-game box channels
  against MLB rates. ``R`` and ``home_win_pct`` stay game-graded; the other box
  channels are superseded by their pool bands (``bands.SUPERSEDED_BY_POOL``) and
  print their verdict for the reader only.

THE GAME-BAND RULE
==================
``half_width = max(Z * sd / sqrt(n), floor)`` with ``Z = 4.0``, ``sd`` measured
from the run itself, and a per-channel floor. A channel earns a PASS only when the
run is long enough, its mean sits inside the half-width, AND the floor is the
binding term. Four verdicts::

    UNDERPOWERED  the run is shorter than this channel needs. No verdict.
    FAIL          long enough, and the model sits outside the band.
    UNRESOLVED    inside the band, but this run is noisier than MLB.
    PASS          long enough, inside, and the floor binds.

``tests/acceptance/bands.py`` owns the rule, the reference rates, their provenance
and the arithmetic. Read it before you read a result.

THE COST
========
The box channels resolve together at ``bands.BOX_LANE_SIMS`` game-sims (5,648);
``home_win_pct`` needs 13,365 decisive games. ``bands.binding_requirement``
computes both; do not copy the numbers. A run shorter than that reports
UNDERPOWERED, which is a missing measurement, not a pass. A floor loose enough to
certify home-field advantage in a shorter run could not reject the 0.515 baseline
``CLAUDE.md §11`` documents.

WHICH CHANNELS CARRY AN XFAIL
=============================
A lane that is red on delivery gets muted, and widening a band to force green is
the anti-pattern this file forbids. A channel whose defect is DOCUMENTED WITH A
TICKET ships as ``@pytest.mark.xfail(strict=True)``:

  * the defect is still open  -> XFAIL -> the lane is green and the report still
    names the channel;
  * the defect is fixed       -> XPASS(strict) -> the lane goes RED and the
    engineer must delete the marker.

No channel carries a marker today. If a certifying run reds a channel:

    **File a ticket. Do not widen the floor.** An un-ticketed red is a finding.
    ``bands.py`` "MOVING A BAND" is the procedure, and it requires a model change
    with a measurement behind it before any floor moves.

WHY THERE ARE TWO REACH-ON-ERROR CHANNELS
=========================================
``ROE`` counts the reaches on error the pool DREW, at the ``_full_pool_fielding``
boundary, before the loop resolves the play. ``ROE_reached`` counts the batters who
actually reached, at the single commit point ``_commit_run_delta``. A green ROE
beside a red ROE_reached says the pool is right and the loop is wrong; neither
channel says that on its own.

WHY home_win_pct HAS NO XFAIL
=============================
``CLAUDE.md §11`` documents the structural-only baseline at ~.510-.515, and the
band is sized to reject it. Below 13,365 decisive games the channel reports
UNDERPOWERED and prints the run it needs: nobody has measured home-field advantage
in this simulator at a size that resolves it, so an xfail would claim a
measurement nobody has taken. A green home_win_pct says "not the bias-off
baseline"; it does not say the home-field magnitude is right.

RUNNING IT
==========
The default lane is the balanced set at ``DEFAULT_GAMES`` x ``DEFAULT_ITERS``
(45 x 130 = 5,850 game-sims, ``conftest.py``)::

    MSYS_NO_PATHCONV=1 docker compose run --rm \
        -v "$PWD:/app" \
        -e SIM_ACCEPTANCE=1 -e SIM_ACCEPTANCE_TIMEOUT=14400 \
        app pytest tests/acceptance -v -rxX

    # SMOKE: 2 games x 5 iterations. Certifies nothing, and
    # test_sample_is_large_enough_to_mean_anything fails on purpose.
    ... -e SIM_ACCEPTANCE=1 -e SIM_ACCEPTANCE_GAMES=2 -e SIM_ACCEPTANCE_ITERS=5 ...

The ``-v "$PWD:/app"`` mount is load-bearing: ``scripts/`` is NOT bind-mounted
(CLAUDE.md §2a), and the lane parses ``scripts/sim_stats.py`` to check its own MLB
constants, so without the mount it reads the image's copy. The default 7,200 s
timeout is too short for a certifying run: raise ``SIM_ACCEPTANCE_TIMEOUT`` with
the iteration count.

Use the whole balanced set for any certifying run. A shortened prefix is
park-balanced (``bands.MAX_PREFIX_PARK_BIAS``), which keeps a verdict from
inverting but is no longer negligible against the R floor; see
``test_a_shortened_run_cannot_hide_a_park_shift_in_the_R_band``.

Owner: QA / DevOps.
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
    cell_index_line,
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


def test_production_flags_are_on() -> None:
    """The override beat ``tests/conftest.py``. Everything else depends on this.

    If this fails, every band below measured another simulator and labelled the
    result "production", so it is asserted first and on its own.
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


def test_sample_is_large_enough_to_mean_anything(acceptance_run: AcceptanceRun) -> None:
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


def test_the_game_set_does_not_bias_the_run_environment(
    acceptance_run: AcceptanceRun,
) -> None:
    """The games actually simulated must represent the whole set's park factors.

    ``conftest.py`` slices ``ACCEPTANCE_GAME_PKS[:n_games]``. That tuple IS
    ``bands.BALANCED_GAME_ORDER``, so every prefix of four or more games is
    park-balanced. An order sorted by park factor would hand a short run the most
    pitcher-friendly parks, and every offensive band would judge the model on a
    run environment it was never asked to reproduce.

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
            f"game {game_pk} resolved park_run_factor {factor:.4f}, but "
            f"bands.ACCEPTANCE_PARK_FACTORS pins {expected:.4f}. Either the park-factor "
            "resolution changed, or the season in progress moved on (its factors are "
            "recomputed as games accumulate): re-read that season's pins from "
            "derived.park_factors."
        )

    run_mean = bands.mean_park_factor(list(resolved))
    set_mean = bands.mean_park_factor(bands.BALANCED_GAME_ORDER)
    assert abs(run_mean - set_mean) <= bands.MAX_PREFIX_PARK_BIAS, (
        f"the {len(resolved)} games simulated have a mean park run factor of {run_mean:.5f} "
        f"against {set_mean:.5f} for the full set — a {run_mean - set_mean:+.5f} shift, over "
        f"the {bands.MAX_PREFIX_PARK_BIAS} tolerance. Every offensive band is reading a "
        "different run environment from the one it is centred on. Run the whole game set."
    )


def test_the_realism_inputs_reach_the_simulator(acceptance_run: AcceptanceRun) -> None:
    """The park factor and the two defense maps really reach the simulator
    (the sim-kwargs guard, SIM-449).

    Without them the fielder score matrices (``SIM_ACTOR_POWER_FIELDER``) and the
    park kernel (``SIM_PARK_KERNEL_SIGMA``) are inert, and an A/B test of either
    compares two identical no-ops and reports "no effect". This lane fails instead.
    """
    assert acceptance_run.defense_sizes, "no game was resolved"
    for game_pk, (home_n, away_n) in acceptance_run.defense_sizes.items():
        assert home_n and away_n, (
            f"game {game_pk} passed an EMPTY defense map "
            f"(home={home_n}, away={away_n}); the fielder factor cannot act."
        )
    non_neutral = [pk for pk, pf in acceptance_run.park_factors.items() if pf != 1.0]
    assert non_neutral, (
        "every park_run_factor is a neutral 1.0, so the park kernel is a no-op across "
        f"the whole game set: {acceptance_run.park_factors}"
    )


def test_every_band_channel_was_measured(acceptance_run: AcceptanceRun) -> None:
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


def test_steal_totals_reconcile(acceptance_run: AcceptanceRun) -> None:
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


def test_full_pool_outcome_runs(acceptance_run: AcceptanceRun) -> None:
    """``_full_pool_outcome`` drives every production pitch."""
    _assert_called(acceptance_run, "_full_pool_outcome")


def test_full_pool_fielding_runs(acceptance_run: AcceptanceRun) -> None:
    """``_full_pool_fielding`` resolves every batted ball."""
    _assert_called(acceptance_run, "_full_pool_fielding")


def test_transition_resolution_runs(acceptance_run: AcceptanceRun) -> None:
    """``_resolve_in_play_transition`` resolves every ball in play: it applies the
    drawn row's destinations (the transition draw, SIM-511)."""
    _assert_called(acceptance_run, "_resolve_in_play_transition")


def test_commit_run_delta_runs(acceptance_run: AcceptanceRun) -> None:
    """``_commit_run_delta`` is the ROE_reached measuring point.

    If this is zero the ``ROE_reached`` channel is reading a probe that never
    fired, and a zero there would mean "not measured" rather than "nothing
    reached". The two must not be confusable.
    """
    _assert_called(acceptance_run, "_commit_run_delta")


def test_steal_opportunity_draw_runs(acceptance_run: AcceptanceRun) -> None:
    """``_steal_opportunity_draw`` (the opportunity-pool steal draw) is reached in
    the production configuration, on every pitch with a stealable runner."""
    _assert_called(acceptance_run, "_steal_opportunity_draw")


# ---------------------------------------------------------------------------
# The per-channel bands
# ---------------------------------------------------------------------------


def _assert_band(run: AcceptanceRun, channel: str) -> None:
    values = run.observations.get(channel) or []
    assert values, f"the run produced no observations for {channel}"
    result = bands.evaluate(channel, values)
    assert result.passed, result.explain()


def _demoted_band(run: AcceptanceRun, channel: str) -> None:
    """A box channel superseded as certification by its pool-referenced
    per-opportunity band (the pool-totals ruling of 2026-08-20, SIM-516).

    The simulator's frequencies are graded against the play pool's own totals,
    so an era gap between the pool and an external season cannot red the lane.
    The game-level verdict is still measured and printed for the reader; the
    assert is the pool band's."""
    assert channel in bands.SUPERSEDED_BY_POOL, (
        f"{channel} is not in bands.SUPERSEDED_BY_POOL — either demote it there "
        "or assert its game band here; a channel must be gated by exactly one."
    )
    values = run.observations.get(channel) or []
    assert values, f"the run produced no observations for {channel}"
    result = bands.evaluate(channel, values)
    print(f"[superseded by the pool band — informational] {result.explain()}")


def _assert_pool_band(channel: str, numerator: float, denominator: float) -> None:
    result = bands.evaluate_pool(channel, numerator, denominator)
    print(result.explain())
    assert result.passed, result.explain()


def test_runs_band(acceptance_run: AcceptanceRun) -> None:
    """Runs per team per game. The channel the whole betting surface rests on.

    The floor (2.75%) is sized to reject the run-conversion gap ``CLAUDE.md §11``
    records, a 7% shortfall, at 2.54x, so it catches a regression to that gap.
    ``test_the_R_band_reds_at_exactly_seven_percent`` in
    ``test_band_arithmetic.py`` holds that property.
    """
    _assert_band(acceptance_run, "R")


def test_hits_band(acceptance_run: AcceptanceRun) -> None:
    """Hits per team per game. A reach on error is not a hit, so it is not counted."""
    _demoted_band(acceptance_run, "H")


def test_home_runs_band(acceptance_run: AcceptanceRun) -> None:
    """Home runs per team per game.

    No ticket sizes a home-run defect, so there is no xfail. If a certifying run
    reds this, file a ticket; do not widen the floor.
    """
    _demoted_band(acceptance_run, "HR")


def test_doubles_band(acceptance_run: AcceptanceRun) -> None:
    """Doubles per team per game. No documented defect; Rule-B floor."""
    _demoted_band(acceptance_run, "2B")


def test_triples_band(acceptance_run: AcceptanceRun) -> None:
    """Triples per team per game.

    The loosest floor in the lane at 11.1%, because triples are rare: the
    channel's spread is 2.8x its centre, so it costs the most to measure.
    """
    _demoted_band(acceptance_run, "3B")


def test_walks_band(acceptance_run: AcceptanceRun) -> None:
    """Walks per team per game.

    No ticket sizes a walk defect, so there is no xfail. A red here is worth
    chasing: over-walking feeds the pitcher BB prop. Read ``bands.py`` first: the
    3.30 centre is itself 5.0% above this project's own 2023 data (3.1434), twice
    this floor, so a red BB may be the centre rather than the model.
    """
    _demoted_band(acceptance_run, "BB")


def test_strikeouts_band(acceptance_run: AcceptanceRun) -> None:
    """Strikeouts per team per game, counted for the batting side.

    The tightest floor in the lane (1.3%). No ticket sizes a strikeout defect, so
    there is no xfail.
    """
    _demoted_band(acceptance_run, "K")


def test_stolen_bases_band(acceptance_run: AcceptanceRun) -> None:
    """Stolen bases per team per game."""
    _demoted_band(acceptance_run, "SB")


def test_caught_stealing_band(acceptance_run: AcceptanceRun) -> None:
    """Caught stealing per team per game."""
    _demoted_band(acceptance_run, "CS")


def test_double_play_band(acceptance_run: AcceptanceRun) -> None:
    """Double plays turned against the batting team, per team per game.

    Counted from the ``_full_pool_fielding`` signal: the drawn event is in
    ``_DOUBLE_PLAY_EVENTS`` AND the play recorded at least two outs. The second
    condition keeps a relabelled double-play event from counting.
    """
    _demoted_band(acceptance_run, "DP")


def test_reach_on_error_drawn_band(acceptance_run: AcceptanceRun) -> None:
    """Reaches on error the POOL DREW, per team per game.

    Measured at the ``_full_pool_fielding`` boundary, before the loop resolves the
    play, so a green result proves only that the pool supplies errors at about the
    right rate. ``test_reach_on_error_reached_band`` below sees whether the batter
    reached. No ticket sizes a drawn-ROE defect: a red here is a new finding.
    """
    _demoted_band(acceptance_run, "ROE")


def test_reach_on_error_reached_band(acceptance_run: AcceptanceRun) -> None:
    """Batters who ACTUALLY reached on an error, per team per game.

    Counted at ``_commit_run_delta``, the single point the loop turns a play
    into a base-out delta: the commit carries the ``field_error`` event and the
    batter became a runner (``batter_reached``). The in-play commit is the one
    reach-on-error site: a dropped-third-strike reach commits as a strikeout,
    because official scoring charges no error on it (SIM-484).

    ``test_commit_run_delta_runs`` asserts the probe fired, so a zero here
    means "nothing reached", never "nothing was measured".
    """
    _demoted_band(acceptance_run, "ROE_reached")


def test_home_win_pct_band(acceptance_run: AcceptanceRun) -> None:
    """Home-team win percentage over decisive games.

    Below 13,365 decisive games this channel reports UNDERPOWERED: no shorter run
    separates the structural-only baseline ``CLAUDE.md §11`` documents
    (~.510-.515) from MLB's rate at Z = 4. The failure message carries the run
    length it needs. There is no xfail on purpose (see "WHY home_win_pct HAS NO
    XFAIL" in the module docstring).

    A green result says "not the bias-off baseline". It does not say the
    home-field magnitude is right: AUD-HFA in
    ``docs/audit/2026-07-23-MASTER-BUG-REGISTER.md`` argues it overshoots.
    """
    _assert_band(acceptance_run, "home_win_pct")


# ---------------------------------------------------------------------------
# SIM-516 — the POOL-REFERENCED frequency bands (owner ruling 2026-08-20)
#
# The sim is a similarity-weighted sampler of the play pool; its FREQUENCIES
# are graded against the pool's OWN totals (bands.POOL_REFERENCES — centres
# from scripts/pool_window_census.py on the 2023-2026 window). A faithful
# sampler greens by construction; a red is a mechanism defect, never an era
# gap. These asserts replace the superseded per-team-game box asserts above.
# ---------------------------------------------------------------------------


def _pool_hits(run: AcceptanceRun) -> dict[str, float]:
    """Whole-lane hit-class totals from the per-team-game observations."""
    return {
        name: float(sum(run.observations.get(name) or []))
        for name in ("H", "HR", "2B", "3B", "ROE")
    }


def test_pool_band_walks_per_pa(acceptance_run: AcceptanceRun) -> None:
    """PITCHED walks per plate appearance vs the pool's count-machine chain.

    Intentional walks are excluded here (they throw no pitch) and graded by
    their own band below; mixing the two would hide a defect in either."""
    pc = acceptance_run.pool_counts
    _assert_pool_band("BB_PA", pc["BB_pitched"], pc["PA"])


def test_pool_band_intentional_walks_per_pa(acceptance_run: AcceptanceRun) -> None:
    """Intentional walks per PA vs the sim.ibb_rates league rate (SIM-515).

    A formula-issued intentional walk read 0.0085/PA against a real 0.00288,
    9.7x this band's floor; the band guards against that class of defect."""
    pc = acceptance_run.pool_counts
    _assert_pool_band("IBB_PA", pc["IBB"], pc["PA"])


def test_pool_band_strikeouts_per_pa(acceptance_run: AcceptanceRun) -> None:
    pc = acceptance_run.pool_counts
    _assert_pool_band("K_PA", pc["K_pa"], pc["PA"])


def test_pool_band_hbp_per_pa(acceptance_run: AcceptanceRun) -> None:
    pc = acceptance_run.pool_counts
    _assert_pool_band("HBP_PA", pc["HBP"], pc["PA"])


def test_pool_band_pitches_per_pa(acceptance_run: AcceptanceRun) -> None:
    pc = acceptance_run.pool_counts
    _assert_pool_band("PITCHES_PA", acceptance_run.calls["_full_pool_outcome"], pc["PA"])


def test_pool_band_singles_per_bip(acceptance_run: AcceptanceRun) -> None:
    pc = acceptance_run.pool_counts
    h = _pool_hits(acceptance_run)
    singles = h["H"] - h["HR"] - h["2B"] - h["3B"]
    _assert_pool_band("SINGLES_BIP", singles, pc["BIP"])


def test_pool_band_doubles_per_bip(acceptance_run: AcceptanceRun) -> None:
    pc = acceptance_run.pool_counts
    _assert_pool_band("DOUBLES_BIP", _pool_hits(acceptance_run)["2B"], pc["BIP"])


def test_pool_band_triples_per_bip(acceptance_run: AcceptanceRun) -> None:
    pc = acceptance_run.pool_counts
    _assert_pool_band("TRIPLES_BIP", _pool_hits(acceptance_run)["3B"], pc["BIP"])


def test_pool_band_home_runs_per_bip(acceptance_run: AcceptanceRun) -> None:
    pc = acceptance_run.pool_counts
    _assert_pool_band("HR_BIP", _pool_hits(acceptance_run)["HR"], pc["BIP"])


def test_pool_band_roe_per_bip(acceptance_run: AcceptanceRun) -> None:
    pc = acceptance_run.pool_counts
    _assert_pool_band("ROE_BIP", _pool_hits(acceptance_run)["ROE"], pc["BIP"])


def test_pool_band_steal_attempts_2b(acceptance_run: AcceptanceRun) -> None:
    """Steal attempts per opportunity at 2B vs the pool's own rate.

    Counted at the sampler seam from the drawn row's own flags. The band guards
    the attempt volume (the steal-weight volume finding, SIM-556)."""
    pc = acceptance_run.pool_counts
    _assert_pool_band("STEAL_ATT_OPP_2B", pc["STEAL_ATT_2"], pc["STEAL_OPP_2"])


def test_pool_band_steal_attempts_3b(acceptance_run: AcceptanceRun) -> None:
    pc = acceptance_run.pool_counts
    _assert_pool_band("STEAL_ATT_OPP_3B", pc["STEAL_ATT_3"], pc["STEAL_OPP_3"])


def test_pool_band_steal_safe_share_2b(acceptance_run: AcceptanceRun) -> None:
    """The safe share of 2B attempts vs the pool's own; the band keeps it from
    regressing."""
    pc = acceptance_run.pool_counts
    _assert_pool_band("STEAL_SAFE_2B", pc["STEAL_SAFE_2"], pc["STEAL_ATT_2"])


def test_pool_band_dp_per_opportunity(acceptance_run: AcceptanceRun) -> None:
    """DP per opportunity (a BIP with a runner on 1B and <2 outs), the strict
    transition-row definition (r1 AND the batter retired) — the same
    definition the census measured the pool centre with. Traffic (how OFTEN
    the cell occurs) is the walk machinery's business, not this band's."""
    pc = acceptance_run.pool_counts
    _assert_pool_band("DP_OPP", pc["DP_ROW"], pc["DP_OPP_DEN"])


def test_pool_band_called_strike_taken(acceptance_run: AcceptanceRun) -> None:
    """Called strikes per TAKEN pitch. The catcher receiving ratio preserves the
    marginal per count bucket (only the per-catcher conditional may move), so this
    band holding is that property measured live."""
    pc = acceptance_run.pool_counts
    _assert_pool_band("CALLED_STRIKE_TAKEN", pc["CALLED"], pc["TAKEN"])


def test_pool_band_got_away_pitch(acceptance_run: AcceptanceRun) -> None:
    """Got-away rows drawn per pitch (a passed ball or a wild pitch, the uncaught
    third strike included), pinned to the pool's own rate (~0.34 per team-game,
    MLB 0.33-0.35)."""
    pc = acceptance_run.pool_counts
    _assert_pool_band("GOT_AWAY_PITCH", pc["GOT_AWAY"], acceptance_run.calls["_full_pool_outcome"])


def test_report_pool_bands(acceptance_run: AcceptanceRun) -> None:
    """Print the whole pool-band table, pass or fail (SIM-516). Always passes;
    its job is the log."""
    pc = acceptance_run.pool_counts
    h = _pool_hits(acceptance_run)
    pairs: dict[str, tuple[float, float]] = {
        "BB_PA": (pc["BB_pitched"], pc["PA"]),
        "IBB_PA": (pc["IBB"], pc["PA"]),
        "K_PA": (pc["K_pa"], pc["PA"]),
        "HBP_PA": (pc["HBP"], pc["PA"]),
        "PITCHES_PA": (acceptance_run.calls["_full_pool_outcome"], pc["PA"]),
        "SINGLES_BIP": (h["H"] - h["HR"] - h["2B"] - h["3B"], pc["BIP"]),
        "DOUBLES_BIP": (h["2B"], pc["BIP"]),
        "TRIPLES_BIP": (h["3B"], pc["BIP"]),
        "HR_BIP": (h["HR"], pc["BIP"]),
        "ROE_BIP": (h["ROE"], pc["BIP"]),
        "STEAL_ATT_OPP_2B": (pc["STEAL_ATT_2"], pc["STEAL_OPP_2"]),
        "STEAL_ATT_OPP_3B": (pc["STEAL_ATT_3"], pc["STEAL_OPP_3"]),
        "STEAL_SAFE_2B": (pc["STEAL_SAFE_2"], pc["STEAL_ATT_2"]),
        "DP_OPP": (pc["DP_ROW"], pc["DP_OPP_DEN"]),
        "CALLED_STRIKE_TAKEN": (pc["CALLED"], pc["TAKEN"]),
        "GOT_AWAY_PITCH": (pc["GOT_AWAY"], acceptance_run.calls["_full_pool_outcome"]),
    }
    lines = [f"SIM-516 pool-referenced bands — window: {bands.POOL_WINDOW}"]
    for channel in bands.POOL_CHANNELS:
        numer, denom = pairs[channel]
        if denom <= 0:
            lines.append(f"{channel}: NO OPPORTUNITIES")
            continue
        lines.append(bands.evaluate_pool(channel, numer, denom).explain())
    print("\n".join(lines))


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def test_report_every_channel(acceptance_run: AcceptanceRun) -> None:
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
        f"flags: {' '.join(f'{k}={v}' for k, v in PRODUCTION_FLAGS.items())}",
        f"calls: {acceptance_run.calls}",
        f"ties (excluded from home_win_pct): {acceptance_run.ties}",
        f"run environment: mean park run factor {park_mean:.5f} "
        f"(full set {bands.mean_park_factor(bands.BALANCED_GAME_ORDER):.5f})",
        cell_index_line(acceptance_run.cell_index),
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
