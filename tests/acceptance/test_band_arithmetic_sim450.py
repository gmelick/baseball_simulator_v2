"""SIM-450 — self-test of the band rule on synthetic inputs.

The heavy lane needs a 3.2 GB DuckDB, an engine-artifact bundle and Postgres, so
it runs on a schedule and nowhere else. The band ARITHMETIC needs none of that.
These tests run everywhere, including in ``make test``, so the rule that decides
red-or-green is itself covered.

A band that cannot fail is worthless. A band that fails on noise is worse than
worthless. These tests pin both edges of that statement.

WHY THIS FILE WAS REWRITTEN ON 2026-08-10 (ROUND 3)
===================================================
Round 2 repaired two floors. A third review then found that it had sized both
against numbers that were not the ones on record, and that a third hole was open
underneath.

Reproduced against the round-2 tree before anything changed:

  * ``R`` at ``rel_floor=0.075`` PASSED at 7.0%, 7.2%, 7.4% and 7.5% low, and
    first failed at 7.6%. ``CLAUDE.md:85`` records the live gap as "Runs run
    ~7-8% low", so the band passed on most of the documented range. Round 2 had
    sized the floor on ``CLAUDE.md:465``'s "10-12%", which the owner ruled STALE
    on 2026-08-10.
  * ``home_win_pct`` at ``abs_floor=0.030`` failed ONLY at exactly 0.5000 and
    PASSED at 0.5100, 0.5125 and 0.5150. ``CLAUDE.md:400`` records the defect as
    "stuck at the structural-only ~.510-.515", and this lane's own first
    production run measured 0.5125.
  * ``evaluate("H", [8.0, 8.0])`` returned ``passed=True``: two observations, 7%
    off the MLB centre, reported as a certification.

All three are fixed in ``bands.py`` and pinned below. Search for CAN-IT-FAIL
PROOF. Deleting one of those tests puts the platform back where it was: an
instrument that reports success on the defect it was built to find.

Owner: QA / DevOps (SIM-450).
"""

from __future__ import annotations

import ast
import itertools
import math
from pathlib import Path

import pytest

from tests.acceptance import bands
from tests.acceptance.conftest import ACCEPTANCE_GAME_PKS

#: The size at which the twelve box channels resolve under the round-3 floors:
#: 5,100 game-sims x 2 team-games. ``bands.binding_requirement`` computes it; the
#: literal is here only so a reader sees the number the fixtures use.
CERTIFYING_TEAM_GAMES = 10200

#: The decisive games ``home_win_pct`` needs. About 16.3 hours serial.
CERTIFYING_DECISIVE_GAMES = 26015

#: The size round 2 called "the nightly". Kept as a fixture size so the tests can
#: show what that run length now reports: UNDERPOWERED on every channel.
ROUND2_NIGHTLY_TEAM_GAMES = 2400


def _spread_sample(mean: float, sd: float, n: int) -> list[float]:
    """``n`` observations whose sample mean and sample sd are exactly as asked.

    Half sit below the mean and half above. A real sample has spread, so a
    synthetic one must too: a constant list has sd 0, which zeroes the
    standard-error term and tests nothing about it.

    The offset is shrunk by ``sqrt((n-1)/n)`` because ``statistics.stdev``
    divides by ``n-1``. Without that correction a sample built at exactly the
    required size lands a hair above its floor and the resolution rule reports
    UNRESOLVED for a reason that is pure arithmetic on the fixture.
    """
    if n < 2 or n % 2:
        raise ValueError("use an even n of at least 2 so the sample balances exactly")
    offset = sd * math.sqrt((n - 1) / n)
    half = n // 2
    return [mean - offset] * half + [mean + offset] * half


def _coin_flip(n: int) -> list[float]:
    """``n`` decisive games split exactly 50/50. The null model for home field."""
    half = n // 2
    return [1.0] * half + [0.0] * (n - half)


def _home_win_sample(rate: float, n: int) -> list[float]:
    """``n`` decisive games in which the home team wins ``rate`` of the time."""
    wins = round(rate * n)
    return [1.0] * wins + [0.0] * (n - wins)


def _even(n: int) -> int:
    """The next even number at or above ``n``, so ``_spread_sample`` accepts it."""
    return n if n % 2 == 0 else n + 1


def _certifying_sample(channel: str, mean: float) -> list[float]:
    """A sample at ``channel``'s own resolving size, centred on ``mean``.

    Uses the channel's REFERENCE spread, so the fixture is the run the lane would
    have to produce to certify that channel, not a convenient one.
    """
    ref = bands.REFERENCES[channel]
    return _spread_sample(mean, ref.sd_ref, _even(ref.required_observations()))


# ---------------------------------------------------------------------------
# The reference table
# ---------------------------------------------------------------------------


def test_every_channel_has_a_reference_sim450() -> None:
    """Each asserted channel carries a centre, a floor, a spread and a source."""
    assert set(bands.CHANNELS) == set(bands.REFERENCES)
    for name in bands.CHANNELS:
        ref = bands.REFERENCES[name]
        assert ref.centre > 0.0, name
        assert ref.floor() > 0.0, f"{name} has no floor, so its band collapses to noise"
        assert ref.source.strip(), f"{name} has no provenance"
        assert ref.sd_ref > 0.0, f"{name} has no reference spread, so its run length is a guess"
        assert ref.obs_per_sim in (1, 2), name
        # Exactly one floor form, so a reader never has to guess which applies.
        assert (ref.rel_floor is None) != (ref.abs_floor is None), name


def test_every_floor_states_where_it_came_from_sim450() -> None:
    """No floor is a bare number. Each cites a defect or states a rationale.

    This is the round-3 rule that stops a floor being chosen to make a channel
    green. A channel either names the documented magnitude it must register, with
    a file and a line, or it says in words why it carries the floor it carries.
    """
    for name in bands.CHANNELS:
        ref = bands.REFERENCES[name]
        if ref.must_detect is not None:
            assert ref.detect_source.strip(), f"{name} declares must_detect with no source"
            # A source has to point at a file, not wave at one.
            assert any(
                token in ref.detect_source
                for token in ("CLAUDE.md:", "BACKLOG.md:", "sim_loop.py:", "constants.py:")
            ), f"{name} detect_source names no file and line: {ref.detect_source!r}"
        else:
            assert ref.floor_rationale.strip(), (
                f"{name} has no documented defect AND no stated rationale for its floor. "
                "Every floor in this table is either sized on a magnitude on record or "
                "justified statistically. A bare number is how a band gets widened quietly."
            )
            assert "Rule B" in ref.floor_rationale, name


def test_box_channels_and_home_win_pct_partition_the_lane_sim450() -> None:
    """``home_win_pct`` is the one channel measured per game, not per team-game."""
    assert set(bands.BOX_CHANNELS) | {"home_win_pct"} == set(bands.CHANNELS)
    assert "home_win_pct" not in bands.BOX_CHANNELS
    assert bands.REFERENCES["home_win_pct"].obs_per_sim == 1
    for name in bands.BOX_CHANNELS:
        assert bands.REFERENCES[name].obs_per_sim == 2, name


# ---------------------------------------------------------------------------
# Requirement 8 — the restated constants, against the file they came from
# ---------------------------------------------------------------------------


#: Printed on every drift failure. ``scripts/`` is baked into the app image and
#: NOT bind-mounted (CLAUDE.md section 2a), so a container run can read a
#: months-old copy and blame the wrong file. Measured 2026-08-10: the image held
#: ``_MLB_2023`` at :69 while the repo held it at :80.
_STALE_IMAGE_HINT = (
    "  If you are in the app container: scripts/ is baked into the image and is NOT\n"
    '  bind-mounted, so this may be a stale image copy. Re-run with -v "$PWD/scripts:\n'
    '  /app/scripts" before you change anything in bands.py.'
)


def _sim_stats_path() -> Path:
    """``scripts/sim_stats.py``, found relative to this package, not to the CWD."""
    return Path(bands.__file__).resolve().parents[2] / "scripts" / "sim_stats.py"


def _top_level_assignments(path: Path) -> dict[str, tuple[int, object]]:
    """Every top-level ``NAME = <literal>`` in ``path``, as ``{name: (line, value)}``.

    Parses rather than imports. ``scripts/`` has no ``__init__.py``, and
    ``sim_stats.py`` pulls in asyncpg and the whole simulator at module level, so
    importing it here would drag the entire platform into a pure-arithmetic test.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, tuple[int, object]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            value = ast.literal_eval(node.value)
        except ValueError:
            continue
        found[target.id] = (target.lineno, value)
    return found


def test_restated_mlb_constants_match_sim_stats_sim450() -> None:
    """The restated MLB rates equal ``scripts/sim_stats.py``, at the cited lines.

    ``bands.py`` restates ``_MLB_2023`` and ``_MLB_HOME_WIN_PCT`` instead of
    importing them, for the three reasons its docstring gives. A restatement rots
    in two ways and this test fails on both:

      * VALUE drift — somebody edits one copy;
      * LINE drift — the citation still says :69 and :83 while the constants have
        moved to :80 and :94, which is exactly what had happened by 2026-08-10.

    A stale file:line citation is not cosmetic here. Every floor in this lane is
    justified by a citation, so a citation nobody can follow is a floor nobody
    can check.

    MEASURED 2026-08-10 — THE CONTAINER AND THE REPO DISAGREE. ``scripts/`` is
    baked into the app image and is NOT bind-mounted, so a container run without
    ``-v "$PWD/scripts:/app/scripts"`` reads whatever ``scripts/sim_stats.py``
    looked like when the image was last built. The image in use that day held the
    constants at :69 and :83; the repo holds them at :80 and :94. This test found
    that, which is the point — but it means a red here has TWO possible causes and
    ``_STALE_IMAGE_HINT`` names both.
    """
    path = _sim_stats_path()
    assert path.is_file(), (
        f"{path} is missing, so the restated MLB rates in bands.py cannot be checked "
        "against their source. This test fails rather than skips: an unverifiable "
        "provenance block is the failure this lane exists to end."
    )
    found = _top_level_assignments(path)

    assert "_MLB_2023" in found, f"_MLB_2023 is no longer a top-level assignment in {path}"
    line, value = found["_MLB_2023"]
    assert value == bands.RESTATED_MLB_2023, (
        "bands.RESTATED_MLB_2023 has drifted from scripts/sim_stats.py::_MLB_2023.\n"
        f"  read from:  {path}\n"
        f"  sim_stats:  {value}\n  bands:      {bands.RESTATED_MLB_2023}\n"
        f"{_STALE_IMAGE_HINT}"
    )
    assert line == bands.SIM_STATS_MLB_2023_LINE, (
        f"_MLB_2023 sits at line {line} of {path}, but bands.py cites "
        f":{bands.SIM_STATS_MLB_2023_LINE}.\n"
        "  If the repo moved it: update SIM_STATS_MLB_2023_LINE and every citation in "
        "the bands.py docstring.\n"
        f"{_STALE_IMAGE_HINT}"
    )

    assert "_MLB_HOME_WIN_PCT" in found, f"_MLB_HOME_WIN_PCT is no longer top-level in {path}"
    line, value = found["_MLB_HOME_WIN_PCT"]
    assert value == bands.RESTATED_MLB_HOME_WIN_PCT, (
        f"sim_stats says {value}, bands.py restates {bands.RESTATED_MLB_HOME_WIN_PCT}\n"
        f"{_STALE_IMAGE_HINT}"
    )
    assert line == bands.SIM_STATS_HOME_WIN_PCT_LINE, (
        f"_MLB_HOME_WIN_PCT sits at line {line} of {path}, but bands.py cites "
        f":{bands.SIM_STATS_HOME_WIN_PCT_LINE}.\n{_STALE_IMAGE_HINT}"
    )


def test_the_reference_table_uses_the_restated_constants_sim450() -> None:
    """Every ``_MLB_2023`` channel's centre IS the restated value, not a copy.

    Without this the drift test above would guard a dictionary nothing reads.
    """
    for name, centre in bands.RESTATED_MLB_2023.items():
        assert bands.REFERENCES[name].centre == centre, name
        assert f":{bands.SIM_STATS_MLB_2023_LINE}" in bands.REFERENCES[name].source, name
    assert bands.REFERENCES["home_win_pct"].centre == bands.RESTATED_MLB_HOME_WIN_PCT
    assert f":{bands.SIM_STATS_HOME_WIN_PCT_LINE}" in bands.REFERENCES["home_win_pct"].source


# ---------------------------------------------------------------------------
# The guard that stops a floor being widened back over a known defect
# ---------------------------------------------------------------------------


def test_a_floor_never_rises_to_meet_a_documented_defect_sim450() -> None:
    """A band must reject the defect it exists to reject, with margin.

    This is the machine-checked half of the "MOVING A BAND" policy in
    ``bands.py``. A floor equal to the defect detects it half the time, which is
    a coin flip dressed as an instrument. ``DETECTION_MARGIN`` of 1.6 buys 99.2%
    detection power at the channel's own resolving sample size.
    """
    declared = [name for name in bands.CHANNELS if bands.REFERENCES[name].must_detect is not None]
    assert declared, "no channel declares a defect it must detect; the guard is inert"
    for name in declared:
        ref = bands.REFERENCES[name]
        margin = ref.detection_margin() or 0.0
        assert margin >= bands.DETECTION_MARGIN, (
            f"{name}: floor {ref.floor():.5f} against a documented defect of "
            f"{ref.must_detect:.5f} is a margin of only {margin:.3f}, under the required "
            f"{bands.DETECTION_MARGIN}. Detection power would be "
            f"{bands.detection_power(margin):.1%}. Source: {ref.detect_source}"
        )
        assert bands.detection_power(margin) >= 0.99, name


def test_the_round3_floors_hold_their_derived_values_sim450() -> None:
    """Pin every floor the 2026-08-10 round-3 review derived, with its arithmetic.

    A floor is not a taste. Each one below is either ``documented magnitude /
    1.6`` or the tightest deviation 10,200 team-games resolves, and this test
    fails if one drifts.
    """
    expected_rel = {
        "R": 0.0276,
        "H": 0.0158,
        "HR": 0.0382,
        "2B": 0.0333,
        "3B": 0.1109,
        "BB": 0.0243,
        "K": 0.0137,
        "SB": 0.0611,
        "CS": 0.0648,
        "DP": 0.0421,
        "ROE": 0.0857,
        "ROE_reached": 0.0857,
    }
    actual = {n: bands.REFERENCES[n].rel_floor for n in bands.BOX_CHANNELS}
    assert actual == expected_rel
    assert bands.REFERENCES["home_win_pct"].abs_floor == 0.0124
    assert bands.DETECTION_MARGIN == 1.6
    assert bands.Z == 4.0


def test_every_rule_b_floor_is_the_tightest_the_run_resolves_sim450() -> None:
    """A channel with no documented defect is as sensitive as the run allows.

    Rule B in ``bands.py``: the floor is ``Z * sd_ref / sqrt(10200)`` rounded up
    to four decimals. Rounding up is what keeps the channel resolvable at the
    design point; rounding down would make it permanently UNDERPOWERED. This test
    pins both edges — the floor is at or above the minimum, and within one
    rounding step of it.
    """
    for name in bands.BOX_CHANNELS:
        ref = bands.REFERENCES[name]
        if ref.must_detect is not None and ref.floor_rationale == "":
            continue
        minimum = bands.Z * ref.sd_ref / math.sqrt(CERTIFYING_TEAM_GAMES)
        assert ref.floor() >= minimum, (
            f"{name}: floor {ref.floor():.6f} is below the {minimum:.6f} that "
            f"{CERTIFYING_TEAM_GAMES} team-games can resolve, so it can never certify."
        )
        # One rounding step at four decimals of the RELATIVE floor.
        assert ref.floor() - minimum < 1e-4 * ref.centre, (
            f"{name}: floor {ref.floor():.6f} sits more than one rounding step above "
            f"the {minimum:.6f} minimum. Rule B says take the tightest resolvable floor."
        )


# ---------------------------------------------------------------------------
# CAN-IT-FAIL PROOF 1 — R reds on the run-conversion gap ON RECORD
# ---------------------------------------------------------------------------


def test_the_R_band_reds_on_the_documented_run_conversion_gap_sim450() -> None:
    """R must go RED on a 7% run shortfall.

    THE DOCUMENTED MAGNITUDE IS ``CLAUDE.md:85``: "Runs run ~7-8% low (down from
    ~12% pre-fix)". The owner ruled on 2026-08-10 that this line is authoritative
    and that ``CLAUDE.md:465``'s "runs sit ~10-12% low" is stale.

    Round 2 sized the floor on the stale line and shipped ``rel_floor=0.075``,
    which passed at 7.0%, 7.2%, 7.4% and 7.5% low. The band was green across most
    of the platform's own live gap, on the channel the whole betting surface
    rests on.

    The sample is R's own resolving size with the real MLB per-team-game spread,
    so this is the verdict a certifying run would actually print.
    """
    for shortfall in (0.070, 0.072, 0.075, 0.078, 0.080, 0.10, 0.12):
        result = bands.evaluate("R", _certifying_sample("R", 4.62 * (1.0 - shortfall)))
        assert result.verdict == "FAIL", (
            f"a {shortfall:.1%} run shortfall must red the R band. CLAUDE.md:85 records "
            f"the live gap as 7-8% low." + result.explain()
        )
        assert result.failed and not result.passed
        assert result.driver == "FLOOR", "the verdict must be about the model, not the noise"


def test_the_R_band_reds_at_exactly_seven_percent_sim450() -> None:
    """The mandated boundary case, on its own so it cannot be lost in a loop.

    ``CLAUDE.md:85`` — "Runs run ~7-8% low". 7% is the EASIEST end of that range
    for the band to miss, so it is the one that decides whether the instrument
    works. Delta 0.3234 against a floor of 0.1275 is a margin of 2.54, which is
    a detection power above 0.9999 at this sample size.
    """
    ref = bands.REFERENCES["R"]
    documented = 0.07 * ref.centre
    assert ref.must_detect == pytest.approx(documented, abs=1e-9), (
        "R.must_detect must be the CLAUDE.md:85 magnitude, 0.07 * 4.62 = 0.3234"
    )
    assert ref.floor() < documented / bands.DETECTION_MARGIN, (
        f"R floor {ref.floor():.5f} is not below the CLAUDE.md:85 gap {documented:.5f} "
        f"divided by the {bands.DETECTION_MARGIN} detection margin. Detection power at "
        f"the resolving sample size would be "
        f"{bands.detection_power(documented / ref.floor()):.1%}."
    )

    result = bands.evaluate("R", _certifying_sample("R", ref.centre - documented))
    assert result.verdict == "FAIL", (
        "the R band must red on the run-conversion gap CLAUDE.md:85 records" + result.explain()
    )
    assert result.delta == pytest.approx(-documented, abs=1e-9)
    assert abs(result.delta) / result.floor > 2.5


def test_the_R_band_does_not_red_a_healthy_model_sim450() -> None:
    """The R band is not trivially red. Small deviations still pass.

    Without this, "make it fail" could be satisfied by a floor of zero, and the
    lane would cry wolf every night until someone muted it.
    """
    for shortfall in (0.0, 0.005, 0.01, 0.02):
        result = bands.evaluate("R", _certifying_sample("R", 4.62 * (1.0 - shortfall)))
        assert result.passed, f"a {shortfall:.1%} deviation must stay green" + result.explain()


def test_the_R_band_boundary_sits_where_the_floor_says_sim450() -> None:
    """R flips from PASS to FAIL at its floor, 2.76% — under the documented 7%."""
    inside = bands.evaluate("R", _certifying_sample("R", 4.62 * (1.0 - 0.027)))
    outside = bands.evaluate("R", _certifying_sample("R", 4.62 * (1.0 - 0.029)))
    assert inside.passed and inside.rel_delta > -0.0276
    assert outside.failed and outside.rel_delta < -0.0276


# ---------------------------------------------------------------------------
# CAN-IT-FAIL PROOF 2 — home_win_pct reds on the baseline ON RECORD
# ---------------------------------------------------------------------------


def test_home_win_pct_reds_on_the_documented_structural_baseline_sim450() -> None:
    """home_win_pct must go RED at 0.5125, and across the whole documented range.

    THE DOCUMENTED MAGNITUDE IS ``CLAUDE.md:400``: home_win_pct "stuck at the
    structural-only ~.510-.515". That is the value the channel reads when the
    SIM-412 home-field bias does not act. This lane's own first production run
    measured 0.5125 — the middle of that range — at 400 game-sims.

    Round 2 shipped ``abs_floor=0.030``, which failed ONLY at exactly 0.5000 and
    PASSED at 0.5100, 0.5125 and 0.5150. It rejected a coin flip and nothing
    else, so it could not tell "the home-field model works" from "the home-field
    model does nothing".

    The floor is now 0.0124, sized on the HARDEST point of the documented range
    (0.535 - 0.515 = 0.020) with the 1.6 detection margin.
    """
    ref = bands.REFERENCES["home_win_pct"]
    n = ref.required_observations()
    for rate in (0.5000, 0.5100, 0.5125, 0.5150):
        result = bands.evaluate("home_win_pct", _home_win_sample(rate, n))
        assert result.verdict == "FAIL", (
            f"a home win rate of {rate:.4f} must red the band. CLAUDE.md:400 records the "
            f"structural-only baseline as ~.510-.515." + result.explain()
        )
        assert result.failed and not result.passed


def test_home_win_pct_reds_at_exactly_0_5125_sim450() -> None:
    """The mandated boundary case, on its own.

    ``CLAUDE.md:400`` — home_win_pct "stuck at the structural-only ~.510-.515".
    0.5125 is that baseline's midpoint and the value
    ``test_production_config_bands_sim450.py`` recorded from the first production
    run of this lane. Delta 0.0225 against a floor of 0.0124 is a margin of 1.81.
    """
    ref = bands.REFERENCES["home_win_pct"]
    assert ref.must_detect == pytest.approx(0.020, abs=1e-9), (
        "home_win_pct.must_detect must be the CLAUDE.md:400 magnitude, 0.535 - 0.515"
    )
    assert ref.floor() <= ref.must_detect / bands.DETECTION_MARGIN, (
        f"home_win_pct floor {ref.floor():.5f} is not below the CLAUDE.md:400 baseline gap "
        f"{ref.must_detect:.5f} divided by the {bands.DETECTION_MARGIN} detection margin. "
        f"Detection power would be "
        f"{bands.detection_power(ref.must_detect / ref.floor()):.1%}."
    )

    result = bands.evaluate("home_win_pct", _home_win_sample(0.5125, ref.required_observations()))
    assert result.verdict == "FAIL", (
        "the home_win_pct band must red on 0.5125 — the midpoint of the CLAUDE.md:400 "
        "structural-only baseline, and this lane's own first production reading" + result.explain()
    )
    assert result.mean == pytest.approx(0.5125, abs=1e-4)
    assert abs(result.delta) / result.floor > 1.8


def test_home_win_pct_passes_a_correct_model_at_the_required_sample_sim450() -> None:
    """The channel is not trivially red: MLB's own rate passes, once the run is long."""
    n = bands.REFERENCES["home_win_pct"].required_observations()
    good = bands.evaluate("home_win_pct", _home_win_sample(0.535, n))
    assert good.mean == pytest.approx(0.535, abs=2e-4)
    assert good.verdict == "PASS"


def test_home_win_pct_reports_underpowered_at_every_shorter_run_sim450() -> None:
    """Below its own size the channel says so, and never reports success.

    A coin flip and MLB's own rate both return UNDERPOWERED at the sizes this
    lane used to call a nightly. Neither is a pass, and the message carries the
    run length the channel needs.
    """
    ref = bands.REFERENCES["home_win_pct"]
    for n in (1200, 4445, ref.required_observations() - 2):
        for rate in (0.500, 0.5125, 0.535):
            result = bands.evaluate("home_win_pct", _home_win_sample(rate, n))
            assert result.verdict == "UNDERPOWERED", (n, rate)
            assert not result.passed and not result.failed
            assert not result.conclusive
    short = bands.evaluate("home_win_pct", _coin_flip(1200))
    assert "UNDERPOWERED" in short.explain()
    assert str(short.required_sims) in short.explain()


# ---------------------------------------------------------------------------
# CAN-IT-FAIL PROOF 3 — the minimum-sample gate
# ---------------------------------------------------------------------------


def test_a_two_observation_sample_is_never_a_pass_sim450() -> None:
    """``evaluate("H", [8.0] * 2)`` returned ``passed=True`` before 2026-08-10.

    Two observations, a mean of 8.0 against an MLB centre of 8.60 — 7% off — and
    the instrument called it a certification. The resolution rule could not catch
    it: that rule asks the RUN's measured spread, and a constant sample reports
    no spread, so the standard-error term was 0.0 and the floor "bound" on
    nothing.

    The minimum-sample gate asks the REFERENCE spread instead. A run cannot
    influence that number, so no sample can talk its way past it.
    """
    result = bands.evaluate("H", [8.0] * 2)
    assert result.verdict == "UNDERPOWERED", (
        "two observations cannot certify anything. This returned passed=True before "
        "2026-08-10 because a constant sample reports no spread." + result.explain()
    )
    assert not result.passed
    assert not result.failed
    assert not result.conclusive
    assert result.underpowered
    assert result.n == 2
    assert result.sd == 0.0
    assert result.se_term == 0.0
    assert result.required_obs > 10_000
    assert "too short" in result.explain()


def test_no_channel_can_pass_on_a_degenerate_sample_sim450() -> None:
    """The general form: a zero-spread sample is UNDERPOWERED on every channel.

    A constant list is the worst case for the resolution rule and the best case
    for a floor, so it is the sample most likely to manufacture a false pass.
    Every channel refuses it at every size below its own requirement.
    """
    for name in bands.CHANNELS:
        ref = bands.REFERENCES[name]
        for n in (2, 10, 800, ref.required_observations() - 1):
            if n < 2:
                continue
            result = bands.evaluate(name, [ref.centre] * n)
            assert result.verdict == "UNDERPOWERED", (name, n)
            assert not result.passed and not result.failed


def test_the_gate_opens_at_exactly_the_required_observations_sim450() -> None:
    """One observation short is UNDERPOWERED; exactly enough is a verdict.

    The boundary is asserted so nobody weakens the gate to "roughly enough".
    """
    for name in bands.CHANNELS:
        ref = bands.REFERENCES[name]
        need = ref.required_observations()
        short = bands.evaluate(name, [ref.centre] * (need - 1))
        exact = bands.evaluate(name, [ref.centre] * need)
        assert short.verdict == "UNDERPOWERED", name
        assert exact.verdict == "PASS", name
        assert exact.n == ref.required_observations()


def test_a_short_run_reports_underpowered_rather_than_failing_sim450() -> None:
    """A smoke run is not evidence in EITHER direction.

    Before round 3 a 20-team-game smoke with a 1.5-run shortfall reported
    UNRESOLVED, and a 1-observation sample reported FAIL. Both claimed more than
    the sample supports. A run below the requirement now says only that.
    """
    smoke = bands.evaluate("R", _spread_sample(4.62 - 1.5, 3.2188, 20))
    assert smoke.verdict == "UNDERPOWERED"
    assert not smoke.passed and not smoke.failed

    single = bands.evaluate("R", [4.0])
    assert single.verdict == "UNDERPOWERED"
    assert not single.failed, "one observation cannot convict the model either"

    full = bands.evaluate("R", _certifying_sample("R", 4.62 - 1.5))
    assert full.verdict == "FAIL"
    assert full.mean == pytest.approx(3.12, abs=1e-9)


# ---------------------------------------------------------------------------
# Requirement 3 — the tension, resolved with arithmetic rather than a guess
# ---------------------------------------------------------------------------


def test_a_tighter_floor_does_not_raise_the_false_positive_rate_sim450() -> None:
    """The floor and the false-alarm rate are DECOUPLED by the resolution rule.

    The obvious objection to tightening a floor is that the lane starts crying
    wolf on sampling noise. For this rule that objection is wrong.

    The resolution rule requires ``Z * sd / sqrt(n) <= floor``, so at any
    resolving sample size the true standard error obeys ``sigma_m <= floor / Z``.
    A CORRECT model reds only when its sample mean strays past ``floor``, which
    is at least ``Z * sigma_m`` away. So::

        P(false red) <= 2 * (1 - Phi(Z)) = 6.33e-05     for ANY floor

    A tighter floor costs RUN LENGTH, not quiet. This test measures that across
    floors spanning a factor of 14 and shows the false-alarm rate is flat.
    """
    from statistics import NormalDist

    nd = NormalDist()
    ceiling = 2.0 * (1.0 - nd.cdf(bands.Z))
    sd = bands.REFERENCES["R"].sd_ref
    rates = []
    for floor in (0.05, 0.10, 0.20, 0.35, 0.6930):
        n = math.ceil((bands.Z * sd / floor) ** 2)
        sigma_m = sd / math.sqrt(n)
        rates.append(2.0 * (1.0 - nd.cdf(floor / sigma_m)))
    for rate in rates:
        assert rate <= ceiling + 1e-12
        assert rate == pytest.approx(ceiling, rel=0.03)
    # Tightening by 14x changes the false-alarm rate by under 3%.
    assert max(rates) / min(rates) < 1.03


def test_the_round2_nightly_now_certifies_nothing_sim450() -> None:
    """The run round 2 called "the nightly" is a smoke test under round-3 floors.

    12 games x 100 iterations = 1,200 game-sims = 2,400 team-games. Round 2 said
    that certified eleven of twelve channels. It certifies NONE of the thirteen
    now, because every floor is 3x to 7x tighter.

    This is asserted rather than left as prose so that an operator who kept the
    old command finds out from a red test instead of from a green tick over an
    empty measurement.
    """
    for name in bands.BOX_CHANNELS:
        ref = bands.REFERENCES[name]
        result = bands.evaluate(
            name, _spread_sample(ref.centre, ref.sd_ref, ROUND2_NIGHTLY_TEAM_GAMES)
        )
        assert result.verdict == "UNDERPOWERED", name
        assert not result.conclusive, name
    decisive = bands.evaluate("home_win_pct", _home_win_sample(0.535, 1200))
    assert decisive.verdict == "UNDERPOWERED"


def test_the_detection_margin_buys_the_power_it_claims_sim450() -> None:
    """``DETECTION_MARGIN`` is a power decision, and the power is computable.

    At a channel's own resolving sample size ``sigma_m = floor / Z``, so the
    probability the band reds on a defect ``m`` times its floor is
    ``Phi(Z * (m - 1))``. The shipped margin of 1.6 buys 99.2%.
    """
    assert bands.detection_power(1.0) == pytest.approx(0.5, abs=1e-9)
    assert bands.detection_power(1.25) == pytest.approx(0.8413, abs=5e-4)
    assert bands.detection_power(1.5) == pytest.approx(0.9772, abs=5e-4)
    assert bands.detection_power(bands.DETECTION_MARGIN) == pytest.approx(0.9918, abs=5e-4)
    assert bands.detection_power(bands.DETECTION_MARGIN) >= 0.99
    # Monotone, so a bigger margin is never worse.
    powers = [bands.detection_power(m) for m in (1.0, 1.2, 1.4, 1.6, 2.0)]
    for lo, hi in itertools.pairwise(powers):
        assert hi >= lo


def test_the_certifying_run_cost_is_stated_not_hidden_sim450() -> None:
    """Pin the run lengths the round-3 floors demand, in game-sims and in hours.

    Requirement: if a floor cannot be both sensitive and quiet at a feasible
    sample size, say so. It can — the cost is time, and this test writes the time
    down so nobody discovers it at 3 a.m.
    """
    box_channel, box_sims = bands.binding_requirement(bands.BOX_CHANNELS)
    lane_channel, lane_sims = bands.binding_requirement()

    assert box_sims == bands.BOX_LANE_SIMS
    assert box_channel in bands.BOX_CHANNELS
    assert lane_channel == "home_win_pct"
    assert lane_sims == CERTIFYING_DECISIVE_GAMES

    seconds_per_sim = 2.25  # measured 2026-08-10: 901.6 s for 400 sims
    assert box_sims * seconds_per_sim / 3600.0 == pytest.approx(3.19, abs=0.05)
    assert lane_sims * seconds_per_sim / 3600.0 == pytest.approx(16.26, abs=0.05)

    # The box lane grew 7.3x against the round-2 floors' 696 game-sims. That is
    # the price of seeing a 7% run gap instead of a 15% one.
    assert box_sims / 696 == pytest.approx(7.33, abs=0.05)


def test_required_sample_sizes_match_the_published_table_sim450() -> None:
    """Pin the run lengths the ``bands.py`` docstring publishes.

    A reader plans a run from that table. If a floor moves and the table does
    not, this test fails and points at the row to update.
    """
    expected = {
        "R": 5098,
        "H": 5096,
        "HR": 5077,
        "2B": 5089,
        "3B": 5092,
        "BB": 5098,
        "K": 5082,
        "SB": 5091,
        "CS": 5092,
        "DP": 5083,
        "ROE": 5100,
        "ROE_reached": 5100,
        "home_win_pct": 26015,
    }
    actual = {name: bands.required_sims(name) for name in bands.CHANNELS}
    assert actual == expected


def test_every_box_channel_resolves_inside_one_run_sim450() -> None:
    """The twelve box channels are sized to land together at 5,100 game-sims.

    They are deliberately within 0.5% of each other, so one run certifies all
    twelve and no single channel silently drags the lane longer.
    """
    sims = [bands.required_sims(name) for name in bands.BOX_CHANNELS]
    assert max(sims) <= bands.BOX_LANE_SIMS
    assert min(sims) / max(sims) > 0.995
    for name in bands.BOX_CHANNELS:
        ref = bands.REFERENCES[name]
        se = bands.Z * ref.sd_ref / math.sqrt(CERTIFYING_TEAM_GAMES)
        assert se <= ref.floor(), (
            f"{name}: the standard error {se:.5f} at {CERTIFYING_TEAM_GAMES} team-games "
            f"exceeds the floor {ref.floor():.5f}, so the box run cannot certify it."
        )


def test_home_win_pct_costs_a_weekend_and_the_lane_says_so_sim450() -> None:
    """Record the one channel a nightly cannot certify at any honest floor.

    This is a limit, not a defect, and it is asserted so nobody rediscovers it by
    accident or quietly widens the floor to make it fit. A floor loose enough for
    a nightly cannot reject the 0.515 baseline ``CLAUDE.md:400`` documents.
    """
    ref = bands.REFERENCES["home_win_pct"]
    assert ref.required_sims() == CERTIFYING_DECISIVE_GAMES
    assert ref.required_sims() > 5 * bands.BOX_LANE_SIMS

    # The floor a 5,100-sim run could resolve, and what it would cost in power.
    nightly_floor = bands.Z * ref.sd_ref / math.sqrt(bands.BOX_LANE_SIMS)
    assert nightly_floor > (ref.must_detect or 0.0), (
        "if a box-lane-sized run could resolve a floor under the documented 0.020 "
        "baseline, this channel would not need its own run and this test should go."
    )


# ---------------------------------------------------------------------------
# The resolution rule still works on top of the gate
# ---------------------------------------------------------------------------


def test_a_noisier_than_MLB_run_reports_unresolved_sim450() -> None:
    """Long enough on paper, but the run's OWN spread is wider than the reference.

    The minimum-sample gate uses ``sd_ref``, a fixed MLB number. The resolution
    rule uses the run's measured ``sd``. Both are needed: a run can reach the
    required count and still be too noisy to certify, and only the second rule
    sees that.
    """
    for name in bands.CHANNELS:
        ref = bands.REFERENCES[name]
        n = _even(ref.required_observations())
        noisy = bands.evaluate(name, _spread_sample(ref.centre, ref.sd_ref * 2.0, n))
        assert not noisy.underpowered, name
        assert noisy.within_band, name
        assert not noisy.resolved, name
        assert noisy.verdict == "UNRESOLVED", name
        assert not noisy.passed and not noisy.failed, name
        assert "noisier" in noisy.explain()

        calm = bands.evaluate(name, _spread_sample(ref.centre, ref.sd_ref, n))
        assert calm.verdict == "PASS", name


def test_verdict_covers_every_case_exactly_once_sim450() -> None:
    """The four verdicts are exhaustive, and only PASS and FAIL are conclusive."""
    ref = bands.REFERENCES["R"]
    n = _even(ref.required_observations())
    seen = set()
    for result in (
        bands.evaluate("R", _spread_sample(ref.centre, ref.sd_ref, n)),
        bands.evaluate("R", _spread_sample(ref.centre * 0.8, ref.sd_ref, n)),
        bands.evaluate("R", _spread_sample(ref.centre, ref.sd_ref * 2.0, n)),
        bands.evaluate("R", _spread_sample(ref.centre, ref.sd_ref, 20)),
    ):
        seen.add(result.verdict)
        assert result.verdict in bands.VERDICTS
        assert result.passed == (result.verdict == "PASS")
        assert result.failed == (result.verdict == "FAIL")
        assert result.conclusive == (result.verdict in ("PASS", "FAIL"))
    assert seen == set(bands.VERDICTS)


# ---------------------------------------------------------------------------
# The band rule itself
# ---------------------------------------------------------------------------


def test_half_width_is_the_larger_of_the_two_terms_sim450() -> None:
    """The rule is ``max(Z * sd / sqrt(n), floor)`` — both terms, larger wins."""
    ref = bands.REFERENCES["R"]
    half, se, floor = bands.half_width(ref, sd=3.2188, n=20)
    assert se == pytest.approx(4.0 * 3.2188 / math.sqrt(20), rel=1e-9)
    assert half == se > floor
    half, se, floor = bands.half_width(ref, sd=3.2188, n=CERTIFYING_TEAM_GAMES)
    assert floor == pytest.approx(0.0276 * 4.62, rel=1e-9)
    assert half == floor > se


def test_band_tightens_as_the_run_grows_sim450() -> None:
    """More games must never widen a band. That is the point of the SE term."""
    ref = bands.REFERENCES["home_win_pct"]
    widths = [bands.half_width(ref, sd=0.4988, n=n)[0] for n in (50, 200, 1200, 26015, 100000)]
    for wide, narrow in itertools.pairwise(widths):
        assert narrow <= wide
    assert widths[-1] == pytest.approx(ref.floor(), rel=1e-9)


def test_a_band_can_actually_fail_sim450() -> None:
    """A band that cannot fail is worthless. Feed it a wrong model and it reds."""
    # Stolen bases pinned at zero — the production behaviour BACKLOG.md:19 records.
    n = bands.REFERENCES["SB"].required_observations()
    zeroed = bands.evaluate("SB", [0.0] * n)
    assert zeroed.sd == 0.0
    assert zeroed.se_term == 0.0
    assert zeroed.half_width == zeroed.floor
    assert zeroed.verdict == "FAIL"
    assert zeroed.failed and not zeroed.passed
    assert "the model is wrong" in zeroed.explain()


def test_a_band_does_not_fail_on_a_correct_model_sim450() -> None:
    """A model sitting on the MLB centre passes, once the run resolves it."""
    need = bands.REFERENCES["R"].required_observations()
    for n in (need, need * 2, need * 4):
        result = bands.evaluate("R", [4.62] * n)
        assert result.passed
        assert result.delta == pytest.approx(0.0, abs=1e-9)


def test_sample_sd_handles_a_degenerate_sample_sim450() -> None:
    """Fewer than two observations gives sd 0.0 rather than an exception."""
    assert bands.sample_sd([]) == 0.0
    assert bands.sample_sd([1.0]) == 0.0
    assert bands.sample_sd([1.0, 3.0]) == pytest.approx(math.sqrt(2.0))


def test_z_pins_the_false_alarm_rate_sim450() -> None:
    """Z is a false-alarm-rate decision. Lowering it makes the lane flaky.

    At Z=4.0 and 13 two-sided channels the whole-run false-red probability is
    8.2e-04 — about one false red every 1,215 runs. At Z=3.0 it is 3.5e-02, about
    one every 29. This test fails if someone quietly lowers Z.
    """
    from statistics import NormalDist

    assert bands.Z == 4.0
    per_channel = 2.0 * (1.0 - NormalDist().cdf(bands.Z))
    per_run = 1.0 - (1.0 - per_channel) ** len(bands.CHANNELS)
    assert per_run < 1e-3


# ---------------------------------------------------------------------------
# The two reach-on-error channels
# ---------------------------------------------------------------------------


def test_the_lane_carries_both_reach_on_error_channels_sim450() -> None:
    """SIM-496 needs two ROE channels, and only one of them can see the defect.

    ``BACKLOG.md:20`` records it in as many words: the ``ROE`` probe counts the
    DRAWN event at the ``_full_pool_fielding`` boundary, so it is taken before
    the loop turns the reach on error into an out. No floor on that channel can
    ever red on the defect — the measurement happens too early.

    ``ROE_reached`` counts batters who actually reached, at ``_commit_run_delta``.
    It is the channel with the ``must_detect``. Keep both: a green ROE beside a
    red ROE_reached says the pool is right and the loop is wrong, and neither
    channel says that on its own.
    """
    drawn = bands.REFERENCES["ROE"]
    reached = bands.REFERENCES["ROE_reached"]

    assert drawn.must_detect is None, "the drawn channel cannot detect SIM-496; do not claim it"
    assert "no floor on it can ever red" in drawn.floor_rationale
    assert reached.must_detect == pytest.approx(0.2193, abs=1e-9)
    assert "SIM-496" in reached.detect_source
    assert drawn.centre == reached.centre, "the same MLB rate underlies both"
    assert drawn.floor() == reached.floor()

    # The defect today: nothing reaches base on an error at all.
    zeroed = bands.evaluate("ROE_reached", [0.0] * reached.required_observations())
    assert zeroed.verdict == "FAIL"
    assert zeroed.rel_delta == pytest.approx(-1.0, abs=1e-9)

    # ... while the drawn channel sits happily on its centre in the same run.
    fine = bands.evaluate("ROE", [drawn.centre] * drawn.required_observations())
    assert fine.verdict == "PASS"


# ---------------------------------------------------------------------------
# The ROE_reached probe — the counter that has to see SIM-496
# ---------------------------------------------------------------------------


class _StubMachine:
    """The six methods ``_install_probes`` wraps, and nothing else.

    A real ``StateMachine`` needs an artifact bundle, so the probe would
    otherwise be exercised only by the heavy lane — which has never produced a
    CI signal. This stub gives the counter a test that runs everywhere.
    """

    def __init__(self) -> None:
        self.commits: list[tuple[str | None, int, int, int]] = []

    def _full_pool_outcome(self, state: object) -> str:
        return "outcome"

    def _full_pool_fielding(self, state: object) -> None:
        return None

    def _full_pool_out_advancement(self, state: object, result: object, sig: object) -> None:
        return None

    def _full_pool_steal_decision(self, state: object) -> None:
        return None

    def _accumulate_pa(self, state: object, result: object) -> None:
        return None

    def _commit_run_delta(
        self,
        state: object,
        result: object,
        *,
        event: str | None,
        result_hits: int,
        result_outs: int,
        result_runs: int,
    ) -> None:
        self.commits.append((event, result_hits, result_outs, result_runs))


class _StubState:
    def __init__(self, offense: int) -> None:
        self.offense = offense


def test_the_roe_reached_probe_counts_only_a_batter_who_reached_sim450() -> None:
    """SIM-496's counter: a reach on error is a commit with NO out and a base.

    ``BACKLOG.md:20`` records the production shape today —
    ``_full_pool_fielding`` infers ``outs = 0 if int(rh) > 0 else 1``
    (``sim_loop.py:1432``) and a pool ``field_error`` row carries
    ``result_hits = 0``, so the commit is ``(field_error, hits=0, outs=1)``: the
    batter is retired. The probe must count ZERO for that shape and ONE for the
    corrected shape, or the new channel would report a pass on the defect it was
    added to catch.

    The wrapper must also forward every argument unchanged. It sits on the single
    run-commit path of the whole simulator, so a probe that altered a commit
    would corrupt every number the lane reports.
    """
    from tests.acceptance.conftest import _blank_tally, _install_probes

    machine = _StubMachine()
    tally = _blank_tally()
    calls = {
        "_full_pool_outcome": 0,
        "_full_pool_fielding": 0,
        "_full_pool_out_advancement": 0,
        "_full_pool_steal_decision": 0,
        "_commit_run_delta": 0,
    }
    _install_probes(machine, tally, calls)

    away, home = _StubState(0), _StubState(1)

    # The production shape TODAY (SIM-496): retired on the bases.
    machine._commit_run_delta(
        away, object(), event="field_error", result_hits=0, result_outs=1, result_runs=0
    )
    assert tally["ROE_reached"] == [0, 0], (
        "a field_error commit that records an OUT is the SIM-496 defect, not a reach. "
        "Counting it would make the new channel green on the defect it exists to catch."
    )

    # The CORRECTED shape: batter safe at first, no out. Matches the
    # dropped-third-strike commit at sim_loop.py:1989.
    machine._commit_run_delta(
        away, object(), event="field_error", result_hits=1, result_outs=0, result_runs=0
    )
    machine._commit_run_delta(
        home, object(), event="field_error", result_hits=1, result_outs=0, result_runs=1
    )
    assert tally["ROE_reached"] == [1, 1]

    # A non-error commit never counts, whatever its shape.
    for event in ("single", "walk", "strikeout", "field_out", None):
        machine._commit_run_delta(
            home, object(), event=event, result_hits=1, result_outs=0, result_runs=0
        )
    assert tally["ROE_reached"] == [1, 1]

    # Every commit reached the real method, unaltered and in order.
    assert machine.commits == [
        ("field_error", 0, 1, 0),
        ("field_error", 1, 0, 0),
        ("field_error", 1, 0, 1),
        ("single", 1, 0, 0),
        ("walk", 1, 0, 0),
        ("strikeout", 1, 0, 0),
        ("field_out", 1, 0, 0),
        (None, 1, 0, 0),
    ]
    assert calls["_commit_run_delta"] == 8


def test_the_probe_tally_covers_every_channel_it_is_asked_for_sim450() -> None:
    """``TALLY_CHANNELS`` and the band channel list cannot drift apart.

    Every band channel is produced either by the per-game tally or by the result
    object (R, SB, CS, home_win_pct). A channel in ``bands.CHANNELS`` with no
    producer would reach the lane as "no observations", which reads as a broken
    test rather than as a missing measurement.
    """
    from tests.acceptance.conftest import TALLY_CHANNELS, _blank_tally

    from_result = {"R", "SB", "CS", "home_win_pct"}
    produced = set(TALLY_CHANNELS) | from_result
    assert produced == set(bands.CHANNELS), (
        f"channels with no producer: {sorted(set(bands.CHANNELS) - produced)}; "
        f"producers with no channel: {sorted(produced - set(bands.CHANNELS))}"
    )
    assert tuple(_blank_tally()) == TALLY_CHANNELS
    assert not (set(TALLY_CHANNELS) & from_result), "a channel must have exactly one producer"


# ---------------------------------------------------------------------------
# The game set — a shortened run must measure the same run environment
# ---------------------------------------------------------------------------


def test_balanced_game_order_covers_the_whole_set_sim450() -> None:
    """The balanced order is a permutation of the twelve games, not a subset."""
    assert sorted(bands.BALANCED_GAME_ORDER) == sorted(bands.ACCEPTANCE_PARK_FACTORS)
    assert len(bands.BALANCED_GAME_ORDER) == 12
    assert sorted(bands.BALANCED_GAME_ORDER) == sorted(ACCEPTANCE_GAME_PKS)


def test_balanced_game_order_keeps_every_prefix_near_the_set_mean_sim450() -> None:
    """Any run of four or more games measures the whole set's run environment."""
    k, bias = bands.worst_prefix_park_bias(bands.BALANCED_GAME_ORDER)
    assert abs(bias) <= bands.MAX_PREFIX_PARK_BIAS, (
        f"the {k}-game prefix sits {bias:+.4f} from the full-set park factor, over the "
        f"{bands.MAX_PREFIX_PARK_BIAS} tolerance"
    )
    for length in range(bands.MIN_BALANCED_PREFIX, 13):
        assert abs(bands.prefix_park_bias(bands.BALANCED_GAME_ORDER, length)) <= (
            bands.MAX_PREFIX_PARK_BIAS
        ), length


def test_the_prefix_check_rejects_an_ascending_order_sim450() -> None:
    """CAN-IT-FAIL PROOF 4. The balance check reds on the order it replaced.

    ``conftest.py`` listed the twelve games in ASCENDING park-factor order on
    delivery, so an 8-game run took the eight most pitcher-friendly parks: mean
    factor 0.96844 against 0.99952 for the full set. A check that could not
    reject that order would protect nothing.
    """
    ascending = tuple(sorted(bands.ACCEPTANCE_PARK_FACTORS, key=bands.ACCEPTANCE_PARK_FACTORS.get))
    k, bias = bands.worst_prefix_park_bias(ascending)
    assert abs(bias) > bands.MAX_PREFIX_PARK_BIAS, "the check must reject the ascending order"
    assert bands.mean_park_factor(ascending[:8]) == pytest.approx(0.96844, abs=5e-5)
    assert bands.prefix_park_bias(ascending, 8) == pytest.approx(-0.03108, abs=5e-5)
    assert bands.mean_park_factor(bands.BALANCED_GAME_ORDER[:8]) == pytest.approx(0.99640, abs=5e-5)


def test_conftest_slices_the_balanced_game_order_sim450() -> None:
    """The acceptance game set IS the balanced order (round 3).

    Round 2 shipped ``BALANCED_GAME_ORDER`` in ``bands.py`` and left this test
    ``xfail(strict=True)``, because ``conftest.py`` belonged to another agent that
    round. The marker's reason text asked a future agent to make the fix — which
    would have turned this into an XPASS and reddened the push lane the moment
    anyone did what it asked. Round 3 owns both files, so the reorder landed and
    the marker is gone.
    """
    assert tuple(ACCEPTANCE_GAME_PKS) == bands.BALANCED_GAME_ORDER, (
        "conftest.ACCEPTANCE_GAME_PKS must BE bands.BALANCED_GAME_ORDER. The fixture "
        "slices [:n_games], so any other order hands a shortened run a biased set of "
        f"parks.\n  conftest: {tuple(ACCEPTANCE_GAME_PKS)}\n  balanced: "
        f"{bands.BALANCED_GAME_ORDER}"
    )
    k, bias = bands.worst_prefix_park_bias(tuple(ACCEPTANCE_GAME_PKS))
    assert abs(bias) <= bands.MAX_PREFIX_PARK_BIAS, (
        f"the {k}-game prefix of ACCEPTANCE_GAME_PKS sits {bias:+.4f} from the full-set "
        f"park factor. Reorder it to bands.BALANCED_GAME_ORDER."
    )


def test_a_shortened_run_cannot_hide_a_park_shift_in_the_R_band_sim450() -> None:
    """HONEST LIMIT: the round-3 R floor is no longer safely above park drift.

    Under the round-2 floor of 0.3465 the 0.02 prefix tolerance was worth 0.0924
    runs, 27% of the floor, and the balanced order's real worst prefix was 17% of
    it. Both were comfortable.

    The round-3 floor is 0.1275. The same tolerance is now 72% of it, and the
    balanced order's real worst prefix bias of 0.0126 is worth 0.058 runs, or 46%
    of the floor. Park selection alone still cannot push a correct model across
    the threshold, but the headroom is gone.

    This test asserts the true state rather than a comfortable one. The operating
    rule that follows is in ``bands.py``: run all twelve games for any certifying
    run, and treat a shortened prefix as a smoke test.
    """
    floor = bands.REFERENCES["R"].floor()
    centre = bands.REFERENCES["R"].centre

    tolerance_in_runs = bands.MAX_PREFIX_PARK_BIAS * centre
    assert tolerance_in_runs / floor == pytest.approx(0.72, abs=0.02)
    assert tolerance_in_runs < floor, (
        "if the allowed park drift ever exceeds the R floor, a compliant prefix can red "
        "a correct model and the tolerance must be tightened."
    )

    _, worst = bands.worst_prefix_park_bias(bands.BALANCED_GAME_ORDER)
    actual_in_runs = abs(worst) * centre
    assert actual_in_runs / floor == pytest.approx(0.46, abs=0.02)
    assert actual_in_runs < floor / 2.0

    # The full twelve-game set has no prefix bias at all, which is why a
    # certifying run uses it.
    assert bands.prefix_park_bias(bands.BALANCED_GAME_ORDER, 12) == pytest.approx(0.0, abs=1e-12)
