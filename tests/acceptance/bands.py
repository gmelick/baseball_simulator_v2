"""SIM-450 — the reference rates, the band rule, and the contract that governs both.

This module is pure arithmetic. It imports nothing from the simulator, so a
reviewer can read the numbers and a test can check the rule without a database,
an artifact bundle or a Docker container.

WHAT A BAND IS
==============
A band is a statement of the form "the simulator's mean for this channel must sit
within ``half_width`` of the real MLB rate". A channel is one box-score quantity
per team per game: R, H, HR, 2B, 3B, BB, K, SB, CS, the double-play rate, two
reach-on-error rates, plus the home-team win percentage.

WHY A BAND AND NOT A GOLDEN FIXTURE
===================================
A golden fixture asserts byte-identical output. The SIM-448..SIM-496 remediation
programme changes the run environment ON PURPOSE, so a golden fixture generated
today would fail on every intended change AND would freeze today's confirmed sim
defects into the baseline as "correct". A band survives an intended model change
and still catches a regression.

THE BAND RULE
=============
For every channel::

    half_width = max( Z * sd / sqrt(n) , floor )

    n     = the number of observations the run produced for this channel
    sd    = the sample standard deviation MEASURED FROM THE RUN ITSELF
    Z     = 4.0
    floor = rel_floor * centre   (or a stated absolute floor)

A channel earns a PASS when ALL THREE of these hold::

    n                      >= required_observations()   # the run is long enough
    abs(sim_mean - centre) <= half_width                # the model sits inside
    Z * sd / sqrt(n)       <= floor                     # the floor binds

The verdict therefore has four values::

    UNDERPOWERED  the run is shorter than this channel needs. No verdict is
                  possible. Neither a pass nor a failure — a missing measurement.
    FAIL          the mean sits outside the half-width. The model is wrong.
    UNRESOLVED    the mean sits inside the half-width, but the run's OWN measured
                  spread pushes the standard-error term above the floor. The run
                  cannot certify this channel.
    PASS          long enough, inside the band, and the floor binds.

``BandResult.passed`` is true only for PASS. ``BandResult.failed`` is true only
for FAIL. UNDERPOWERED and UNRESOLVED are neither, and the lane prints the sample
size each one needs.

WHY THE MINIMUM-SAMPLE GATE EXISTS — A WORKED HOLE
==================================================
Before 2026-08-10 (round 3) the rule had only the last two conditions, and the
standard error was computed from the run's own measured spread. A short run whose
spread happens to measure ZERO then has a zero standard-error term, so the floor
binds trivially and the channel reports PASS on two observations::

    evaluate("H", [8.0, 8.0])
      n = 2, sd = 0.0, se = 0.0, floor = 0.1359
      |8.0 - 8.60| = 0.60 > 0.1359                    -> FAIL today
      (under the pre-round-3 floor of 0.8600 it read  -> PASS)

Two observations cannot support any conclusion, whatever the floor. The
resolution rule could not catch this because it asks the RUN's spread, and a
degenerate run reports no spread. The minimum-sample gate asks the REFERENCE
spread instead — a fixed, measured MLB number the run cannot influence — so no
sample, however degenerate, can talk its way past it.

WHY Z = 4.0
===========
The lane asserts 13 channels, each two-sided. The per-channel false-failure
probability and the resulting whole-run false-red rate are::

    z=3.0   p=2.700e-03   run=3.453e-02   1 false red per     29 nights
    z=3.5   p=4.653e-04   run=6.034e-03   1 false red per    166 nights
    z=4.0   p=6.334e-05   run=8.234e-04   1 false red per  1,215 nights
    z=4.5   p=6.795e-06   run=8.834e-05   1 false red per 11,320 nights

(Reproduce with ``p = 2 * (1 - NormalDist().cdf(z))`` and
``run = 1 - (1 - p) ** 13``.)

z=3.0 gives a false red every month, and a lane that cries wolf monthly gets
muted. z=4.0 gives roughly one false red every 3.3 years. Take z=4.0.

THE TENSION EVERY FLOOR CHOICE FACES, AND WHY IT IS NOT THE TENSION IT LOOKS LIKE
================================================================================
The obvious worry about tightening a floor is that the lane starts crying wolf on
sampling noise. For THIS rule that worry is wrong, and the arithmetic says so.

The resolution rule requires ``Z * sd / sqrt(n) <= floor``. Write the true
standard error of the mean as ``sigma_m``. Resolution therefore means
``sigma_m <= floor / Z``. A CORRECT model reds only when its sample mean strays
past ``floor``, and ``floor >= Z * sigma_m``, so::

    P(false red) <= 2 * (1 - Phi(Z)) = 6.33e-05     for ANY floor

Measured across five R floors spanning 14x, at each one's own resolving sample
size::

    floor 0.0500   resolving n = 66,309   P(false red) = 6.334e-05
    floor 0.1000   resolving n = 16,578   P(false red) = 6.331e-05
    floor 0.2000   resolving n =  4,145   P(false red) = 6.325e-05
    floor 0.3500   resolving n =  1,354   P(false red) = 6.304e-05
    floor 0.6930   resolving n =    346   P(false red) = 6.208e-05

The false-alarm rate is set by Z alone. **A tighter floor costs run length, not
quiet.** That is the whole answer to "will this make the lane flaky": it will
not, and the price is paid in hours instead. Every hour is stated below.

HOW EACH FLOOR IS CHOSEN — TWO RULES, BOTH MACHINE-CHECKED
==========================================================
**Rule A — a channel with a documented defect.** The floor must be small enough
to REJECT that defect with high probability. At a channel's own resolving sample
size the detection power is::

    power = Phi( (d - floor) / sigma_m ) = Phi( Z * (d / floor - 1) )

    d = the defect magnitude documented in this repository

So the margin ``d / floor`` alone fixes the power::

    d/floor = 1.00 -> power 0.5000      d/floor = 1.50 -> power 0.9772
    d/floor = 1.25 -> power 0.8413      d/floor = 1.60 -> power 0.9918

``DETECTION_MARGIN`` is 1.6, giving **99.2% power** at the minimum resolving run
and more at any longer one. ``Reference.must_detect`` carries ``d`` and
``test_band_arithmetic_sim450.py`` fails if any floor rises above ``d / 1.6``.

**Rule B — a channel with no documented defect.** There is no magnitude to aim
at, so the only non-arbitrary choice is "as sensitive as the run allows". The
floor is the smallest deviation the lane's certifying run can resolve::

    floor = Z * sd_ref / sqrt(n_lane)      rounded UP to six decimals

A Rule-B floor is a MEASUREMENT LIMIT, not a tolerance. A red Rule-B channel
means "the model differs from MLB by more than this run can attribute to noise".
It does not mean a ticket exists.

THE DOCUMENTED-DEFECT TABLE — EVERY FLOOR TRACED TO A FILE AND A LINE
=====================================================================
"first fails" is the smallest relative deviation the band rejects once the run
resolves. It equals the floor as a fraction of the centre::

The floors below are the SIM-508 set, recomputed 2026-08-18 against the 2025
centres and spreads (the "round-3" 2023-era floors are in git history)::

  channel  documented magnitude          source             floor   first
                                                                    fails
  R        runs ~7-8% low                CLAUDE.md:85       0.0275  2.7%
  H        +0.2078/team-game: a drawn    BACKLOG.md:20      0.0157  1.6%
           field_error is aliased to a   (SIM-496) +
           single, so a retired batter   constants.py:177
           is credited a hit
  HR       none with a magnitude         Rule B             0.0381  3.8%
  2B       none with a magnitude         Rule B             0.0318  3.2%
  3B       none with a magnitude         Rule B             0.1071 10.7%
  BB       none with a magnitude         Rule B             0.0226  2.3%
  K        none with a magnitude         Rule B             0.0132  1.3%
  SB       0.0000 = -100% (re-zeroed)    BACKLOG.md:19      0.0537  5.4%
                                         (SIM-495)
  CS       0.0000 = -100% (re-zeroed)    BACKLOG.md:19      0.0772  7.7%
                                         (SIM-495)
  DP       -80.4% of centre              BACKLOG.md:18      0.0420  4.2%
                                         (SIM-494)
  ROE      none — see the warning below  Rule B             0.0822  8.2%
  ROE_     nothing reaches base on an    BACKLOG.md:20      0.0822  8.2%
  reached  error at all = -100%          (SIM-496)
  home_    stuck at the structural-only  CLAUDE.md:400      0.0173  3.2%
  win_pct  ~.510-.515 baseline                              (abs)

Two entries need their reasoning written out.

**R.** ``CLAUDE.md:85`` reads "Runs run ~7-8% low (down from ~12% pre-fix)". The
owner ruled on 2026-08-10 that this line is authoritative and that
``CLAUDE.md:465``'s "runs sit ~10-12% low" is STALE. The band is therefore sized
on the HARDER end of the live range, 7%: ``d = 0.07 * 4.4473 = 0.3113``. A floor
of 0.027478 x 4.4473 = 0.1222 clears the 1.6 margin with room (``d/floor = 2.55``).

**home_win_pct.** ``CLAUDE.md:400`` records the defect as "stuck at the
structural-only ~.510-.515". The band is sized on the HARDEST point of that
range, 0.515, so ``d = 0.5428 - 0.515 = 0.0278`` and the floor is the margin
bound 0.0278 / 1.6 rounded down to 0.0173 (``d/floor = 1.61``). It also reds
0.5125 — the value this lane's own first production run measured — at
``delta/floor = 1.75``. The 2025 centre nearly HALVED this channel's cost:
13,365 decisive games against the old 26,015.

WARNING — ONE CHANNEL CANNOT SEE ITS OWN DEFECT AT ANY FLOOR
============================================================
``ROE`` counts the reach-on-error events the pool DRAWS. ``BACKLOG.md:20``
(SIM-496) records that the loop then converts every one of them into an out:
``_full_pool_fielding`` infers ``outs = 0 if int(rh) > 0 else 1``
(``sim_loop.py:1432``) and a pool ``field_error`` row carries ``result_hits = 0``.
So the batter is retired, and ``constants.py:177`` credits him a hit anyway.

No floor on the DRAWN channel can register that. The measurement is taken before
the defect happens. ``BACKLOG.md:20`` states the fix in as many words: "The lane
cannot register this failure and must gain a second ROE channel counted after
resolution". ``ROE_reached`` is that channel. It counts batters who actually
reached, at the single commit point ``_commit_run_delta``, and it reads 0.0
today.

Keep BOTH. Together they localise the defect: a green ROE with a red ROE_reached
says the pool is right and the loop is wrong. Neither channel says that alone.

HOW LONG THE RUN MUST BE
========================
A channel resolves once its standard-error term drops to its floor::

    required observations = (Z * sd_ref / floor) ** 2

Each game-sim yields TWO team-game observations for a box channel and ONE
decisive game for ``home_win_pct``::

    channel        floor    sd_ref   required obs   required game-sims
    R             0.1222    3.2468         11,295                5,648
    H             0.1299    3.4505         11,295                5,648
    HR            0.0443    1.1772         11,295                5,648
    2B            0.0506    1.3455         11,295                5,648
    3B            0.0138    0.3677         11,295                5,648
    BB            0.0717    1.9045         11,295                5,648
    K             0.1105    2.9352         11,294                5,647
    SB            0.0336    0.8923         11,295                5,648
    CS            0.0148    0.3940         11,295                5,648
    DP            0.0314    0.8332         11,295                5,648
    ROE           0.0171    0.4541         11,295                5,648
    ROE_reached   0.0171    0.4541         11,295                5,648
    home_win_pct  0.0173    0.5000         13,365               13,365

The twelve box channels are sized to land together, so **5,648 game-sims
certify all twelve** — 12 games x 471 iterations, about 3.5 hours at the
measured 2.25 s per sim. ``H`` anchors the set: its floor is BOUND by the 1.6x
detection margin on the SIM-496 magnitude, and every other floor is the
tightest that resolves beside it. ``home_win_pct`` needs **13,365 decisive
games**, about 8.4 hours. Call ``required_sims`` / ``binding_requirement``
rather than copying these numbers.

THE COST, STATED PLAINLY
========================
The old floors certified the box channels in 696 game-sims. The new ones need
5,648 — **8.1x the run**, about 3.5 hours instead of 26 minutes. That is the
price of an instrument that can see a 7% run-conversion gap instead of a 15% one.

``home_win_pct`` is worse. Certifying it at 26,015 decisive games takes about
16.3 hours serial, which is a WEEKEND run, not a nightly. I am not proposing a
looser floor to make it fit: a floor loose enough for a nightly cannot reject the
0.515 baseline ``CLAUDE.md:400`` documents, and a band that cannot reject the
documented defect is the exact failure this round exists to end. So the honest
statement is:

    **The nightly certifies twelve channels. ``home_win_pct`` is certifiable
    only on a weekend run, and reports UNDERPOWERED every other night.**

SIM-467 (the 2,880-cell filter index, ~1000x less per-draw work) would make both
numbers routine. Until it lands, the hours above are real.

WHAT THE SE TERM DOES NOT CAPTURE
=================================
The iterations of one game share a lineup, a park and two starting pitchers. The
run therefore estimates the mean over THOSE matchups, not the mean over MLB. The
within-run standard error says nothing about that matchup-selection error. The
floor absorbs it, and ``BALANCED_GAME_ORDER`` below keeps the matchup set from
biasing the run environment in the first place.

This is a real limit on the tight Rule-B floors. A 1.4% K floor is finer than the
matchup-selection error of a twelve-game set almost certainly is. Read a red
Rule-B channel as "look here", not as "the model is broken".

REFERENCE RATES — WHERE THE NUMBERS COME FROM
=============================================
SIM-508 (owner decision, 2026-08-18): every reference is THIS PROJECT'S OWN
ingested 2025 season — 2,430 regular-season Final games, 4,860 team-games,
measured 2026-08-18 — replacing the hand-written 2023 constants. The artifact's
recency floor draws from 2024-2026, so 2025 grades the sim against the era it
actually plays. Definitional notes that keep the reference matching what the
probes count: BB includes intentional walks (595 in ``raw.play_events`` 2025 —
the probe counts both canonical walk classes); CS includes every scored class —
pitch-steal CS, K+CS double plays, and 149 advancing pickoffs (Rule 9.07(h),
the SIM-507 channel).

The nine box channels restate ``_MLB_2025`` from ``scripts/sim_stats.py:88``
verbatim, and ``home_win_pct`` restates ``_MLB_HOME_WIN_PCT`` from
``scripts/sim_stats.py:102``. (The citations were once wrong for months —
:69/:83 as the file outgrew them —
``test_restated_mlb_constants_match_sim_stats_sim450`` now parses that file and
fails on BOTH value drift and line drift, so a stale citation cannot survive
again.)

⚠ ``scripts/`` is baked into the app image and is NOT bind-mounted (CLAUDE.md
section 2a). Run the lane in a container with ``-v "$PWD/scripts:/app/scripts"``,
or the drift test reads whatever that file looked like when the image was last
built. Measured 2026-08-10: the image held ``_MLB_2023`` at :69 while the repo
held it at :80, so the test failed against the container and passed against the
repo. The REPO is the source of truth for a citation.

They are RESTATED, not imported, for three reasons. ``scripts/`` has no
``__init__.py`` and is not bind-mounted into the app container, so a test that
imports it binds to an image-baked copy. ``_MLB_2025`` is a private name in a CLI
script whose module-level imports pull in asyncpg and the whole simulator. The
lane needs four channels the dict does not carry, so restating gives one table
with one provenance block instead of two imports and four loose literals.

``DP``, ``ROE`` and ``ROE_reached`` have no entry in ``_MLB_2025``. I derived
them from the same 2025 games. Reproduce with::

    WITH g AS (SELECT game_pk FROM raw.games
               WHERE season=2025 AND game_type='R' AND status='Final'),
         p AS (SELECT * FROM raw.pitches WHERE season=2025
               AND game_pk IN (SELECT game_pk FROM g))
    SELECT
      SUM((events IN ('grounded_into_double_play','ground_into_double_play',
                      'double_play','strikeout_double_play','sac_fly_double_play',
                      'sac_bunt_double_play'))::int) AS dp,
      SUM((events='field_error')::int)               AS roe
    FROM p;

Measured 2026-08-18 against the live database: 2,430 regular-season Final games
= 4,860 team-games, dp=3,625, roe=1,010. So DP = 0.7459 and ROE = 0.2078 per team
per game. ``ROE_reached`` shares the ROE centre: in MLB every reach on error
does put the batter on base, so the two rates are the same number.

THE sd_ref COLUMN — MEASURED, NOT ASSUMED
=========================================
Every ``sd_ref`` below is the real per-team-game standard deviation, measured
against the live Postgres on 2026-08-18 over the same 4,860 team-games of 2025.
The R value came from ``raw.games`` final scores; the rest grouped
``raw.pitches`` by ``(game_pk, inning_topbot)``. Two sd_ref values are measured
on the pitch-row classes alone and documented as such at their entries: BB
(the 0.12/team-game IBB stream) and CS (the 0.03/team-game pickoff stream) —
both streams are too small and too flat to move a spread.

``sd_ref`` does two jobs. It sizes the floors and the run length, as before.
Since round 3 it ALSO drives the minimum-sample gate, so a run cannot report a
verdict on a spread it never measured. The lane still takes ``sd`` from its own
sample for the standard-error term.

THE OLD DICT-VS-DATA DISAGREEMENT — RESOLVED BY SIM-508
=======================================================
Until 2026-08-18 the nine box centres were hand-written 2023 MLB constants that
disagreed with this project's own 2023 data by 2.3-5.5% on four channels (H,
2B, BB, SB), and the file carried a standing warning not to treat a red on
those four as proof about the simulator. SIM-508 ended that: the centre and the
data are now the SAME measurement (own ingested 2025), so a red channel is
about the model, full stop. The old comparison table is in git history.

THE GAME SET AND WHY ITS ORDER MATTERS
======================================
``tests/acceptance/conftest.py`` holds twelve 2024 games, one per venue, and
slices ``[:n_games]`` when the operator shortens the run. The set as a whole is
unbiased — its mean 2024 park run factor is 0.99952 against a league mean of
1.0014 — but the ORDER decides what a shortened run measures.

The order shipped on 2026-08-10 was ASCENDING by park factor, so every prefix
was a pitcher's park set. Measured prefix means, against the full-set 0.99952::

    games   ascending order   balanced order
        4         0.93112          0.99410
        6         0.95350          0.99528
        8         0.96844          0.99640
       10         0.98081          0.99825
       12         0.99952          0.99952

An 8-game run therefore sampled a run environment 3.1% below the set it claimed
to represent, and the day-one 400-sim measurement in
``test_production_config_bands_sim450.py`` was taken on exactly that prefix.

``BALANCED_GAME_ORDER`` fixes it by pairing extremes: the widest pair first, each
pair written low-then-high. ``conftest.ACCEPTANCE_GAME_PKS`` now IS this tuple,
so every prefix of four or more games sits within ``MAX_PREFIX_PARK_BIAS`` of the
full-set mean. Prefixes of one and three games still cannot balance — one game
has no partner, and three splits the widest pair — so no certifiable run is that
short.

Every park factor in ``ACCEPTANCE_PARK_FACTORS`` was read from
``derived.park_factors`` (season 2024, ``factor_type='R'``, ``regressed_factor``)
in the live DuckDB on 2026-08-10.

MOVING A BAND
=============
THESE BANDS WILL MOVE. The remediation programme changes the run environment on
purpose. A band that no longer fits a deliberately changed model is not a bug.

Move a band only under these rules:

  1. Name the ticket that changed the model.
  2. State the measured new value and the sample size behind it.
  3. Change the FLOOR. Never change Z. Never change the MLB centre.
  4. Record the old floor, the new floor and the date in the changelog below.
  5. Never raise a floor above ``must_detect / DETECTION_MARGIN``. That field
     records a defect the band is REQUIRED to register, and
     ``test_band_arithmetic_sim450.py`` fails if a floor crosses it.

Widening a band to turn a red lane green, with no model change behind it, is a
defect in this file. If a channel is red and nobody changed the model, the model
is wrong.

FLOOR CHANGELOG
===============
2026-08-10 (SIM-450) — initial floors. Chosen so the floor binds at 12 games x
100 iterations, then checked against a real 400-sim run of the production
configuration.

2026-08-10 (SIM-450 remediation round 2, same day) — R 0.15 -> 0.075 and the
resolution rule added, after a review found two bands could not fail on the
defect they exist to catch.

2026-08-10 (SIM-450 remediation round 3, same day) — **a third review found that
round 2 sized both repaired floors against numbers that were not the ones on
record.** Every floor is re-derived here from a magnitude with a file and a line
behind it. Reproduced before changing anything:

  * ``evaluate("R", ...)`` at rel_floor 0.075 PASSED at 7.0%, 7.2%, 7.4% and
    7.5% low, and first failed at 7.6%. ``CLAUDE.md:85`` records the live gap as
    "Runs run ~7-8% low", so the band passed on most of the documented range.
    Round 2 had sized it against ``CLAUDE.md:465``'s "10-12%", which the owner
    ruled STALE on 2026-08-10.
  * ``evaluate("home_win_pct", ...)`` at floor 0.030 failed ONLY at exactly
    0.5000 and PASSED at 0.5100, 0.5125 and 0.5150. ``CLAUDE.md:400`` records the
    defect as "stuck at the structural-only ~.510-.515", and this lane's own
    first production run measured 0.5125
    (``test_production_config_bands_sim450.py``). The band passed on the whole
    documented range and on the lane's own reading.
  * ``evaluate("H", [8.0, 8.0])`` returned ``passed=True`` — two observations,
    7% off the MLB centre.

The changes:

  * **R: rel_floor 0.075 -> 0.0276.** Sized on ``CLAUDE.md:85``, harder end:
    ``d = 0.07 * 4.62 = 0.3234``; floor = 0.1275; margin 2.54; power > 0.9999.
    Cost: 5,098 game-sims, up from 691.
  * **H: rel_floor 0.10 -> 0.0158.** Sized on ``BACKLOG.md:20`` (SIM-496) with
    ``simulation/constants.py:177``: every drawn ``field_error`` is credited as a
    hit although the batter was retired, worth ``d = 0.2193`` per team-game.
    Floor 0.1359; margin 1.61; power 0.992. **H binds the box lane at 5,096
    game-sims.**
  * **HR 0.15 -> 0.0382, 2B 0.15 -> 0.0333, 3B 0.30 -> 0.1109, BB 0.12 -> 0.0243,
    K 0.10 -> 0.0137, ROE 0.35 -> 0.0857.** No documented magnitude. Rule B: the
    tightest deviation team-games resolves (11,295).
  * **SB 0.25 -> 0.0611, CS 0.35 -> 0.0648, DP 0.20 -> 0.0421.** Each has a
    documented magnitude (``BACKLOG.md:19`` SIM-495, ``BACKLOG.md:18`` SIM-494)
    that the OLD floor already rejected. Rule A did not force a change here;
    Rule B did, and it applies because these channels share the lane's run.
    Tightening them costs nothing (they resolve inside the 5,648-sim box run) and
    keeps the band meaningful after the defect is fixed.
  * **home_win_pct: abs_floor 0.030 -> 0.0124.** Sized on ``CLAUDE.md:400``,
    hardest point: ``d = 0.535 - 0.515 = 0.020``; margin 1.61; power 0.992. It
    reds 0.5125 at 1.81x the floor. Cost: **26,015 decisive games, about 16.3
    hours** — a weekend run. Stated, not hidden. No nightly-sized floor can
    reject the documented baseline.
  * **NEW CHANNEL ``ROE_reached``**, floor 0.0857, ``must_detect`` 0.2193.
    ``BACKLOG.md:20`` requires it in as many words: the DRAWN ROE channel is
    measured before the defect happens, so no floor on it can ever red.
  * **The minimum-sample gate** (``BandResult.underpowered``) closes the
    zero-variance hole. A channel with fewer than ``required_observations()``
    observations returns UNDERPOWERED, which is neither a pass nor a failure.
  * **``ACCEPTANCE_GAME_PKS`` in ``conftest.py`` now IS ``BALANCED_GAME_ORDER``.**
    Round 2 shipped the balanced tuple here and a strict xfail there, because it
    did not own that file. Round 3 owns it, so the reorder landed and the xfail
    is deleted.
  * **The ``scripts/sim_stats.py`` citations moved from :69 / :83 to :80 / :94**,
    and a test now parses that file so both value drift and line drift fail.

  Nothing was widened. Z is unchanged at 4.0. No MLB centre moved.

Owner: QA / DevOps (SIM-450).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

#: The z-multiplier on the Monte-Carlo standard error. See "WHY Z = 4.0" above.
#: Changing this is out of scope for any model ticket — it is a false-alarm-rate
#: decision, not a model decision.
Z: float = 4.0

#: How much smaller than a documented defect a floor must be. The detection power
#: at a channel's own resolving sample size is ``Phi(Z * (margin - 1))``, so 1.6
#: buys 99.2%. See "HOW EACH FLOOR IS CHOSEN" above.
DETECTION_MARGIN: float = 1.6

#: The design point behind every floor: 5,648 game-sims = 11,295 team-games
#: (SIM-508). H is the binding Rule-A channel — its floor sits at the 1.6x
#: detection margin on the SIM-496 magnitude — and every other floor is the
#: tightest that resolves beside it. Operationally 12 games x 471 iterations.
BOX_LANE_SIMS: int = 5648

#: Ordered channel names. The nightly asserts every one of them.
CHANNELS: tuple[str, ...] = (
    "R",
    "H",
    "HR",
    "2B",
    "3B",
    "BB",
    "K",
    "SB",
    "CS",
    "DP",
    "ROE",
    "ROE_reached",
    "home_win_pct",
)

#: The twelve channels measured once per team per game. ``home_win_pct`` is the
#: exception: it yields one observation per DECISIVE game, so it needs about 5x
#: the run length to resolve. The lane reports the two requirements apart.
BOX_CHANNELS: tuple[str, ...] = tuple(c for c in CHANNELS if c != "home_win_pct")


# ---------------------------------------------------------------------------
# Provenance — the constants this file restates from scripts/sim_stats.py
# ---------------------------------------------------------------------------

#: The line ``_MLB_2025`` is assigned on in ``scripts/sim_stats.py``. Asserted by
#: ``test_restated_mlb_constants_match_sim_stats_sim450``, which parses that file
#: rather than importing it. A moved constant fails the test instead of rotting
#: into a wrong citation, which is what :69 and :83 did before 2026-08-10.
SIM_STATS_MLB_2025_LINE: int = 88

#: The line ``_MLB_HOME_WIN_PCT`` is assigned on in ``scripts/sim_stats.py``.
SIM_STATS_HOME_WIN_PCT_LINE: int = 102

#: ``_MLB_2025`` restated verbatim (SIM-508, owner decision 2026-08-18: every
#: reference is this project's OWN ingested 2025 season — 2,430 regular-season
#: Final games, 4,860 team-games, measured 2026-08-18 — so the sim is graded
#: against the era its 2024-2026 artifact floor draws from). ``REFERENCES``
#: reads its centres from here, so the drift test compares the numbers the
#: bands actually use.
RESTATED_MLB_2025: dict[str, float] = {
    "R": 4.4473,
    "H": 8.2588,
    "HR": 1.1626,
    "2B": 1.5936,
    "3B": 0.1292,
    "BB": 3.1656,
    "K": 8.3525,
    "SB": 0.6251,
    "CS": 0.1922,
}

#: ``_MLB_HOME_WIN_PCT`` restated verbatim (measured 2025, same games).
RESTATED_MLB_HOME_WIN_PCT: float = 0.5428

#: Derived from this project's own ingested 2025 data — SQL in the module
#: docstring. ``ROE_reached`` shares the ROE centre because in MLB every reach on
#: error does put the batter on base.
DERIVED_2025: dict[str, float] = {
    "DP": 0.7459,
    "ROE": 0.2078,
    "ROE_reached": 0.2078,
}

_SIM_STATS = "scripts/sim_stats.py"


@dataclass(frozen=True, slots=True)
class Reference:
    """One channel's MLB centre, the floor under its band, and its sample cost.

    ``rel_floor`` is a fraction of ``centre``. ``abs_floor`` is a floor stated
    directly in the channel's own units. Exactly one of the two is set.

    ``sd_ref`` is the real MLB per-team-game standard deviation, measured against
    the live database (see "THE sd_ref COLUMN" above). It sizes the floor, it
    answers "how long must the run be", and it drives the minimum-sample gate. It
    is NOT used for the standard-error term — the lane measures that spread from
    its own sample.

    ``obs_per_sim`` is how many observations one game-sim contributes: two for a
    box channel, one for ``home_win_pct``.

    ``must_detect`` is a deviation, in the channel's own units, that this band is
    REQUIRED to register. It is set only where a defect is documented with a
    magnitude, ``detect_source`` carries the file and the line, and
    ``test_band_arithmetic_sim450.py`` fails if a floor ever rises above
    ``must_detect / DETECTION_MARGIN``. It is the machine-checked half of the
    "MOVING A BAND" policy.

    ``floor_rationale`` says in one line why a channel with NO documented defect
    carries the floor it carries. Rule B channels must fill it in.
    """

    centre: float
    source: str
    sd_ref: float
    rel_floor: float | None = None
    abs_floor: float | None = None
    obs_per_sim: int = 2
    must_detect: float | None = None
    detect_source: str = ""
    floor_rationale: str = ""

    def floor(self) -> float:
        """The absolute floor under this channel's half-width."""
        if self.abs_floor is not None:
            return float(self.abs_floor)
        return float(self.rel_floor or 0.0) * float(self.centre)

    def required_observations(self) -> int:
        """Observations needed before the floor binds and the channel resolves.

        Solves ``Z * sd_ref / sqrt(n) <= floor`` for ``n``. Below this count no
        verdict is possible, so ``evaluate`` returns UNDERPOWERED.
        """
        fl = self.floor()
        if fl <= 0.0:
            raise ValueError("a channel with no floor can never resolve")
        return int(math.ceil((Z * float(self.sd_ref) / fl) ** 2))

    def required_sims(self) -> int:
        """Game-sims needed to resolve this channel.

        Ties are excluded from ``home_win_pct``, so a real run needs a few more
        sims than this for that channel. Add margin rather than trimming it.
        """
        return int(math.ceil(self.required_observations() / float(self.obs_per_sim)))

    def detection_margin(self) -> float | None:
        """``must_detect / floor``. ``None`` when no defect is documented.

        The detection power at the resolving sample size is
        ``Phi(Z * (margin - 1))``. The policy requires at least
        ``DETECTION_MARGIN``.
        """
        if self.must_detect is None:
            return None
        return float(self.must_detect) / self.floor()


#: The reference table. ``source`` names where each centre comes from, and
#: ``detect_source`` names where each defect magnitude comes from, so a reader
#: never has to guess whether a number was measured, documented or invented.
REFERENCES: dict[str, Reference] = {
    # --- the nine channels restated from scripts/sim_stats.py:88 (_MLB_2025) ---
    # SIM-508: every sd_ref below re-measured on the same 2025 games
    # (2026-08-18); every Rule-B floor recomputed as the tightest deviation
    # team-games resolves (11,295) against the 2025 spread and centre.
    "R": Reference(
        RESTATED_MLB_2025["R"],
        f"{_SIM_STATS}:{SIM_STATS_MLB_2025_LINE} _MLB_2025",
        sd_ref=3.2468,
        rel_floor=0.027478,
        must_detect=0.311311,
        detect_source="CLAUDE.md:85 'Runs run ~7-8% low' -> harder end 0.07 * 4.4473 = 0.311311",
    ),
    "H": Reference(
        RESTATED_MLB_2025["H"],
        f"{_SIM_STATS}:{SIM_STATS_MLB_2025_LINE} _MLB_2025",
        sd_ref=3.4505,
        rel_floor=0.015725,
        must_detect=0.2078,
        detect_source=(
            "BACKLOG.md:20 (SIM-496) + simulation/constants.py:177: a drawn field_error "
            "is aliased to a single, so a retired batter is credited a hit. Worth the "
            "whole 2025 reach-on-error rate, 0.2078 per team-game. The 1.6x detection "
            "margin BINDS this floor and ANCHORS the box lane: every other floor "
            "is sized to resolve beside it at 11,295 obs (12x471)."
        ),
    ),
    "HR": Reference(
        RESTATED_MLB_2025["HR"],
        f"{_SIM_STATS}:{SIM_STATS_MLB_2025_LINE} _MLB_2025",
        sd_ref=1.1772,
        rel_floor=0.038110,
        floor_rationale=(
            "Rule B. No magnitude on record. AUD-PARK-HR "
            "(docs/audit/2026-07-23-MASTER-BUG-REGISTER.md:233) records that HR PMFs are "
            "park-invariant but sizes nothing. Floor = the tightest deviation the box lane "
            "team-games resolves."
        ),
    ),
    "2B": Reference(
        RESTATED_MLB_2025["2B"],
        f"{_SIM_STATS}:{SIM_STATS_MLB_2025_LINE} _MLB_2025",
        sd_ref=1.3455,
        rel_floor=0.031778,
        floor_rationale="Rule B. No magnitude on record. Tightest resolvable at the 11,295-obs box lane.",
    ),
    "3B": Reference(
        RESTATED_MLB_2025["3B"],
        f"{_SIM_STATS}:{SIM_STATS_MLB_2025_LINE} _MLB_2025",
        sd_ref=0.3677,
        rel_floor=0.107115,
        floor_rationale=(
            "Rule B. No magnitude on record. Tightest resolvable at the 11,295-obs box lane. This is "
            "the loosest floor in the lane because the channel's spread (0.3677) is 2.8x "
            "its centre (0.1292) — triples are rare, so they cost the most to measure."
        ),
    ),
    "BB": Reference(
        RESTATED_MLB_2025["BB"],
        f"{_SIM_STATS}:{SIM_STATS_MLB_2025_LINE} _MLB_2025",
        sd_ref=1.9045,
        rel_floor=0.022644,
        floor_rationale=(
            "Rule B. No magnitude on record. SIM-456 (whiff_rate measured called strikes) was "
            "once the unsized candidate cause; it is CLOSED 2026-09-04 (fix live since the "
            "2026-08-14 recompute) and the 2026-08-20 diagnosis decomposed the walk surplus "
            "as IBB 54% / Markov structure 33% / pool era 12% / kernel tilt 1%. Tightest "
            "resolvable at the 11,295-obs box lane. The centre includes intentional walks (595 in "
            "raw.play_events 2025); sd_ref is measured on the pitch rows alone — the "
            "0.12/team-game IBB stream adds negligible spread."
        ),
    ),
    "K": Reference(
        RESTATED_MLB_2025["K"],
        f"{_SIM_STATS}:{SIM_STATS_MLB_2025_LINE} _MLB_2025",
        sd_ref=2.9352,
        rel_floor=0.013227,
        floor_rationale=(
            "Rule B. No magnitude on record; SIM-456 closed 2026-09-04 (fix live since the "
            "2026-08-14 recompute; K read −2.5% vs the pool, do not tune). Tightest "
            "resolvable at the 11,295-obs box lane, and the tightest floor in the lane."
        ),
    ),
    "SB": Reference(
        RESTATED_MLB_2025["SB"],
        f"{_SIM_STATS}:{SIM_STATS_MLB_2025_LINE} _MLB_2025",
        sd_ref=0.8923,
        rel_floor=0.053726,
        must_detect=0.6251,
        detect_source=(
            "BACKLOG.md:19 (SIM-495): SB measured 0.0000 over 400 production game-sims "
            "before SIM-474 replaced the gated stub with the opportunity-pool draw "
            "(2026-08-16). The floor keeps rejecting a re-zeroed channel: -centre."
        ),
    ),
    "CS": Reference(
        RESTATED_MLB_2025["CS"],
        f"{_SIM_STATS}:{SIM_STATS_MLB_2025_LINE} _MLB_2025",
        sd_ref=0.3940,
        rel_floor=0.077155,
        must_detect=0.1922,
        detect_source=(
            "BACKLOG.md:19 (SIM-495): CS measured 0.0000. Same root cause as SB — no "
            "steal is attempted, so none is caught. -100% = -centre. The 2025 centre "
            "carries all scored classes (pitch-steal + K+CS + advancing pickoffs, "
            "SIM-507); sd_ref is measured on the pitch-row classes — the 0.03/team-game "
            "pickoff stream adds negligible spread."
        ),
    ),
    # --- three channels derived from raw.pitches 2025 (SQL in the docstring) ---
    "DP": Reference(
        DERIVED_2025["DP"],
        "raw.pitches 2025, 4860 team-games",
        sd_ref=0.8332,
        rel_floor=0.042043,
        must_detect=0.5997,
        detect_source=(
            "BACKLOG.md:18 (SIM-494): DP measured 0.1600 against a 0.8160-era centre at "
            "400 production game-sims — 80.4% low, on a FLOOR-driven band. 0.804 * 0.7459."
        ),
    ),
    "ROE": Reference(
        DERIVED_2025["ROE"],
        "raw.pitches 2025, 4860 team-games",
        sd_ref=0.4541,
        rel_floor=0.082248,
        floor_rationale=(
            "Rule B, and READ THE WARNING in the module docstring. This channel counts the "
            "reach-on-error events the pool DRAWS, which is BEFORE the SIM-496 defect "
            "happens, so no floor on it can ever red on that defect. ROE_reached is the "
            "channel that can. Keep both: green here plus red there localises the fault to "
            "the loop rather than the pool."
        ),
    ),
    "ROE_reached": Reference(
        DERIVED_2025["ROE_reached"],
        "raw.pitches 2025, 4860 team-games (same rate as ROE)",
        sd_ref=0.4541,
        rel_floor=0.082248,
        must_detect=0.2078,
        detect_source=(
            "BACKLOG.md:20 (SIM-496): _full_pool_fielding infers outs = 0 if rh > 0 else 1 "
            "(sim_loop.py:1432) and a pool field_error row carries result_hits = 0, so every "
            "drawn reach on error becomes a one-out field_out and the batter never reaches "
            "base. The only correct site (sim_loop.py:1992) cannot fire in production per "
            "SIM-484. Nothing reaches base on an error today: -100%."
        ),
    ),
    # --- the home-field channel, restated from scripts/sim_stats.py:102 ---
    "home_win_pct": Reference(
        RESTATED_MLB_HOME_WIN_PCT,
        f"{_SIM_STATS}:{SIM_STATS_HOME_WIN_PCT_LINE} _MLB_HOME_WIN_PCT",
        sd_ref=0.5000,
        abs_floor=0.0173,
        obs_per_sim=1,
        must_detect=0.0278,
        detect_source=(
            "CLAUDE.md:400: home_win_pct 'stuck at the structural-only ~.510-.515'. Sized "
            "on the HARDEST point of that range against the measured 2025 centre: "
            "0.5428 - 0.515 = 0.0278; floor = 0.0278/1.6 rounded down. The band also reds "
            "0.5125, which this lane's own first production run measured."
        ),
    ),
}


# ---------------------------------------------------------------------------
# The game set — an unbiased run environment at every run length
# ---------------------------------------------------------------------------

#: 2024 park run factor per acceptance game, read from ``derived.park_factors``
#: (season 2024, ``factor_type='R'``, ``regressed_factor``) in the live DuckDB on
#: 2026-08-16, after the SIM-459 recompute over the SIM-488 re-swept data. Every
#: factor moved <=0.009 from the 2026-08-10 pins and the RANK ORDER is unchanged,
#: so ``BALANCED_GAME_ORDER`` still balances. The league mean that season is
#: 1.0007; this set's mean is 0.99874.
ACCEPTANCE_PARK_FACTORS: dict[int, float] = {
    745199: 0.8726,  # venue  680
    745036: 0.9183,  # venue   12
    745280: 0.9597,  # venue 2395
    745118: 0.9765,  # venue 2889
    744795: 0.9908,  # venue 3309
    746331: 1.0038,  # venue 2392
    745441: 1.0092,  # venue   31
    745521: 1.0169,  # venue 2681
    746088: 1.0243,  # venue   22
    746560: 1.0356,  # venue 5340
    745444: 1.0432,  # venue 5150
    746494: 1.1340,  # venue   19
}

#: The order ``tests/acceptance/conftest.py`` slices, so that a shortened run
#: measures the same run environment as the full set. Built by pairing extremes:
#: the widest pair first, each pair written low-then-high. ``ACCEPTANCE_GAME_PKS``
#: in that file IS this tuple. See "THE GAME SET AND WHY ITS ORDER MATTERS".
BALANCED_GAME_ORDER: tuple[int, ...] = (
    745199,  # 0.8726  |  pair 1, the widest
    746494,  # 1.1340  |
    745036,  # 0.9183  |  pair 2
    745444,  # 1.0432  |
    745280,  # 0.9597  |  pair 3
    746560,  # 1.0356  |
    745118,  # 0.9765  |  pair 4
    746088,  # 1.0243  |
    744795,  # 0.9908  |  pair 5
    745521,  # 1.0169  |
    746331,  # 1.0038  |  pair 6, the narrowest
    745441,  # 1.0092  |
)

#: How far a prefix's mean park run factor may sit from the full set's mean.
#: 0.02 is about 0.09 runs per team-game at the R centre. That is NO LONGER small
#: against the round-3 R floor of 0.1275 — it is 72% of it. The balanced order's
#: worst real prefix bias is 0.0126, worth 0.058 runs or 46% of the floor, which
#: is still uncomfortable. Run all twelve games for any certifying run; the
#: shortened prefixes are for smoke runs only. See
#: ``test_a_shortened_run_cannot_hide_a_park_shift_in_the_R_band_sim450``.
MAX_PREFIX_PARK_BIAS: float = 0.02

#: The shortest prefix that can be balanced. One game has no partner and three
#: games split the widest pair, so both sit outside the tolerance by arithmetic.
MIN_BALANCED_PREFIX: int = 4


def mean_park_factor(game_pks: tuple[int, ...] | list[int]) -> float:
    """Mean 2024 park run factor over the given games. 0.0 for an empty set."""
    if not game_pks:
        return 0.0
    return statistics.fmean(ACCEPTANCE_PARK_FACTORS[int(pk)] for pk in game_pks)


def prefix_park_bias(order: tuple[int, ...] | list[int], k: int) -> float:
    """Signed gap between the first ``k`` games' park factor and the whole set's.

    A negative value means the prefix runs in pitchers' parks, so a run shortened
    to ``k`` games measures a lower-scoring environment than the set claims.
    """
    return mean_park_factor(list(order)[:k]) - mean_park_factor(list(order))


def worst_prefix_park_bias(
    order: tuple[int, ...] | list[int], min_k: int = MIN_BALANCED_PREFIX
) -> tuple[int, float]:
    """The prefix length with the largest park bias, and that signed bias.

    Scans every length from ``min_k`` to the full set. Returns ``(0, 0.0)`` when
    the order is shorter than ``min_k``.
    """
    lengths = range(min_k, len(order) + 1)
    biases = [(k, prefix_park_bias(order, k)) for k in lengths]
    if not biases:
        return (0, 0.0)
    return max(biases, key=lambda item: abs(item[1]))


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------

#: The four verdicts, worst-informed first. Only ``PASS`` is success and only
#: ``FAIL`` is a statement that the model is wrong.
VERDICTS: tuple[str, ...] = ("UNDERPOWERED", "UNRESOLVED", "FAIL", "PASS")


@dataclass(frozen=True, slots=True)
class BandResult:
    """The full arithmetic behind one channel's verdict.

    Every field is carried so a red lane prints the whole sum instead of a bare
    "assert failed", and a reader can tell a short run from a noisy one from a
    wrong model.
    """

    channel: str
    centre: float
    mean: float
    sd: float
    n: int
    se_term: float
    floor: float
    half_width: float
    source: str
    required_obs: int
    required_sims: int
    obs_per_sim: int
    #: How many DISTINCT game matchups produced ``values``. ``None`` means the
    #: caller did not say, which is correct for a band-rule unit test on synthetic
    #: numbers and WRONG for a certification run. See ``underpowered``.
    n_matchups: int | None = None

    @property
    def delta(self) -> float:
        """Signed gap from the MLB centre, in the channel's own units."""
        return self.mean - self.centre

    @property
    def rel_delta(self) -> float:
        """Signed gap as a fraction of the centre. Zero when the centre is zero."""
        return 0.0 if self.centre == 0.0 else self.delta / self.centre

    @property
    def lo(self) -> float:
        return self.centre - self.half_width

    @property
    def hi(self) -> float:
        return self.centre + self.half_width

    @property
    def within_band(self) -> bool:
        """Whether the measured mean sits inside the half-width."""
        return abs(self.delta) <= self.half_width

    @property
    def underpowered(self) -> bool:
        """Whether the run is shorter than this channel's floor needs.

        This is the minimum-sample gate. It asks the REFERENCE spread, which the
        run cannot influence, so a degenerate sample cannot defeat it. A channel
        below this count gets no verdict at all — not a pass, and not a failure.

        LENGTH IS NOT POWER. Iterations of ONE matchup are correlated, so 10,200
        observations drawn from two matchups carry the evidence of two games, not
        of 10,200. ``evaluate`` sees only a list, so the caller must state how many
        distinct matchups produced it. A certification run that states fewer than
        :data:`MIN_BALANCED_PREFIX` is underpowered however long it ran.
        """
        if self.n < self.required_obs:
            return True
        return self.n_matchups is not None and self.n_matchups < MIN_BALANCED_PREFIX

    @property
    def resolved(self) -> bool:
        """Whether the floor binds, so the verdict describes the model, not noise.

        This is the resolution rule. It asks the RUN's own measured spread, so it
        catches a run that is long enough on paper but noisier than MLB.
        """
        return self.se_term <= self.floor

    @property
    def verdict(self) -> str:
        """One of ``VERDICTS``. Only PASS is success; only FAIL blames the model."""
        if self.underpowered:
            return "UNDERPOWERED"
        if not self.within_band:
            return "FAIL"
        if not self.resolved:
            return "UNRESOLVED"
        return "PASS"

    @property
    def passed(self) -> bool:
        """True only for PASS."""
        return self.verdict == "PASS"

    @property
    def failed(self) -> bool:
        """True only for FAIL — the run is long enough AND the model is outside."""
        return self.verdict == "FAIL"

    @property
    def conclusive(self) -> bool:
        """Whether this run said anything about the model at all."""
        return self.verdict in ("PASS", "FAIL")

    @property
    def driver(self) -> str:
        """Which term set the half-width: the standard error or the floor."""
        return "SE" if self.se_term > self.floor else "FLOOR"

    def shortfall_sims(self) -> int:
        """Game-sims still missing before this channel resolves. 0 when it has enough."""
        have = int(math.ceil(self.n / float(self.obs_per_sim)))
        return max(0, self.required_sims - have)

    def explain(self) -> str:
        """A one-block message the assertion prints on a red channel."""
        lines = [
            f"\n  channel      {self.channel}",
            f"\n  verdict      {self.verdict}",
            f"\n  sim mean     {self.mean:.4f}   (n={self.n}, sd={self.sd:.4f})",
            f"\n  MLB centre   {self.centre:.4f}   [{self.source}]",
            f"\n  delta        {self.delta:+.4f}   ({self.rel_delta:+.1%})",
            f"\n  half-width   {self.half_width:.4f}   "
            f"= max(Z*sd/sqrt(n)={self.se_term:.4f}, floor={self.floor:.4f})"
            f" -> {self.driver}-driven",
            f"\n  band         [{self.lo:.4f}, {self.hi:.4f}]  (Z={Z})",
        ]
        if self.verdict == "UNDERPOWERED":
            lines.append(
                f"\n  why          This run is too short for this channel to say ANYTHING."
                f"\n               It has {self.n} observations and the floor needs"
                f"\n               {self.required_obs}. This is not a pass and it is not a"
                f"\n               failure — it is a missing measurement."
                f"\n  fix          Run {self.required_sims} game-sims for this channel"
                f"\n               ({self.shortfall_sims()} more than this run). Raise"
                f"\n               SIM_ACCEPTANCE_ITERS, and SIM_ACCEPTANCE_TIMEOUT with it."
            )
        elif self.verdict == "UNRESOLVED":
            lines.append(
                f"\n  why          The mean sits INSIDE the band and the run is long enough"
                f"\n               on paper, but its OWN measured spread is wider than MLB's:"
                f"\n               the standard error {self.se_term:.4f} exceeds the floor"
                f"\n               {self.floor:.4f}. This run cannot tell a correct model from"
                f"\n               a wrong one here, so it is not a pass."
                f"\n  fix          Run longer, or find out why this channel is noisier than"
                f"\n               the sd_ref={self.sd:.4f} sample implies."
            )
        elif self.verdict == "FAIL":
            lines.append("\n  If this is red and nobody changed the model, the model is wrong.")
        lines.append("\n  Read tests/acceptance/bands.py 'MOVING A BAND' before touching a floor.")
        return "".join(lines)


def sample_sd(values: list[float]) -> float:
    """Sample standard deviation, or 0.0 when fewer than two observations exist.

    A zero standard deviation is a real answer, not a failure: a channel the
    simulator never produces (stolen bases today) has no spread, so its band
    collapses to the floor and the floor decides. The minimum-sample gate, not
    this function, is what stops a degenerate SHORT sample reporting a pass.
    """
    if len(values) < 2:
        return 0.0
    return float(statistics.stdev(values))


def half_width(ref: Reference, sd: float, n: int) -> tuple[float, float, float]:
    """Return ``(half_width, se_term, floor)`` for one channel.

    ``se_term`` is ``Z * sd / sqrt(n)`` — the Monte-Carlo standard error scaled
    to the chosen false-alarm rate. ``floor`` is the model-accuracy term. The
    half-width is the larger of the two. With ``n <= 0`` the standard error is
    undefined, so the floor decides on its own.
    """
    se = 0.0 if n <= 0 else float(Z) * float(sd) / math.sqrt(float(n))
    fl = ref.floor()
    return (max(se, fl), se, fl)


def evaluate(channel: str, values: list[float], n_matchups: int | None = None) -> BandResult:
    """Score one channel's observations against its band.

    ``values`` holds one observation per team-game (per decisive game for
    ``home_win_pct``). The caller passes the raw observations, not a mean, so the
    standard error comes from the run itself.

    A run shorter than ``Reference.required_observations()`` returns
    UNDERPOWERED. That verdict is neither a pass nor a failure, and it cannot be
    talked out of by a sample with no spread.

    ``n_matchups`` states how many DISTINCT games produced ``values``. A
    certification run MUST pass it, because repeating one matchup lengthens the
    list without adding evidence. Leave it ``None`` only when scoring synthetic
    numbers in a band-rule unit test, where there is no matchup to count.
    """
    ref = REFERENCES[channel]
    n = len(values)
    mean = float(statistics.fmean(values)) if n else 0.0
    sd = sample_sd(values)
    half, se, fl = half_width(ref, sd, n)
    return BandResult(
        channel=channel,
        centre=float(ref.centre),
        mean=mean,
        sd=sd,
        n=n,
        se_term=se,
        floor=fl,
        half_width=half,
        source=ref.source,
        required_obs=ref.required_observations(),
        required_sims=ref.required_sims(),
        obs_per_sim=int(ref.obs_per_sim),
        n_matchups=None if n_matchups is None else int(n_matchups),
    )


def required_sims(channel: str) -> int:
    """Game-sims needed before ``channel`` can report anything but UNDERPOWERED."""
    return REFERENCES[channel].required_sims()


def binding_requirement(channels: tuple[str, ...] = CHANNELS) -> tuple[str, int]:
    """The channel that needs the longest run, and the game-sims it needs.

    Call this instead of copying a number out of the docstring. The lane uses it
    twice: once over ``BOX_CHANNELS`` for the twelve per-team-game channels, and
    once over ``CHANNELS`` for the whole lane.
    """
    ranked = sorted(channels, key=lambda name: (-required_sims(name), name))
    winner = ranked[0]
    return (winner, required_sims(winner))


def detection_power(margin: float) -> float:
    """Probability the band reds on a defect ``margin`` times its floor.

    Evaluated at the channel's own resolving sample size, where
    ``sigma_m = floor / Z``. See "HOW EACH FLOOR IS CHOSEN" in the module
    docstring. The value is ``Phi(Z * (margin - 1))``.
    """
    from statistics import NormalDist

    return float(NormalDist().cdf(Z * (float(margin) - 1.0)))


# ===========================================================================
# SIM-516 — the POOL-REFERENCED frequency bands (owner ruling 2026-08-20)
# ===========================================================================
#
# THE RULING. The sim is a similarity-weighted sampler of the play pool. The
# owner ruled on 2026-08-20 that its FREQUENCIES are graded against the pool's
# OWN totals — the data it draws from — not against an external season. A
# faithful sampler grades green by construction; a red is a real mechanism
# defect (a kernel tilt, a decision model off its data), never an era gap.
# The game-level channels the pool cannot state (R — the emergent integration
# of everything — and home_win_pct) stay game-graded above. The superseded
# per-team-game box channels stay MEASURED and REPORTED but no longer certify;
# their per-opportunity replacements below do.
#
# THE WINDOW. Full seasons 2023-2026 (the owner's same-day ruling: the last
# three completed seasons plus the current one; RECENCY_FLOOR_SEASONS = 4).
# Every centre below was measured on that window by
# ``scripts/pool_window_census.py`` (run 2026-08-20; the W1 block) and by the
# SIM-515 ``sim.ibb_rates`` build on the same seasons. Re-run the census and
# restate these constants whenever the window moves — the derivation is one
# command, and the source strings say exactly which number came from where.
#
# THE ARITHMETIC. Each channel is a RATIO: a numerator counted by the lane's
# probes over a denominator of real opportunities (plate appearances, balls
# in play, DP opportunities). The verdict is
#
#     |rate - centre|  <=  max(floor, Z * se)
#
# with ``se`` the binomial standard error at the lane's own denominator
# (``sqrt(centre * (1 - centre) / n)``; PITCHES_PA is a mean, not a
# proportion, so it carries a floor only plus a minimum-denominator gate).
# UNDERPOWERED when the noise term exceeds the floor — the floor then cannot
# bind and no verdict is honest. Recency-weighting drift is inside every
# floor: the census measured weighted-vs-unweighted pool rates within 0.1%.

#: The pool window the centres were measured on. Restate with every window move.
POOL_WINDOW = "full seasons 2023-2026 (SIM-516, owner ruling 2026-08-20)"

_CENSUS = "scripts/pool_window_census.py (2026-08-20, the W1 block)"


@dataclass(frozen=True, slots=True)
class PoolReference:
    """One per-opportunity channel's pool centre and the floor under its band."""

    centre: float
    source: str
    rel_floor: float
    is_proportion: bool = True
    must_detect: float | None = None
    detect_source: str = ""
    floor_rationale: str = ""

    def floor(self) -> float:
        return float(self.rel_floor) * float(self.centre)


POOL_REFERENCES: dict[str, PoolReference] = {
    # --- per plate appearance ---------------------------------------------
    "BB_PA": PoolReference(
        0.0850,
        f"{_CENSUS}: chain(pool per-count rates) BB/PA",
        rel_floor=0.02,
        floor_rationale=(
            "The diagnosis run measured the kernel tilt at +0.4%; 2% passes a "
            "faithful sampler and reds a SIM-476-scale tilt."
        ),
    ),
    "IBB_PA": PoolReference(
        0.00288,
        "sim.ibb_rates 2023-2026 build (SIM-515): 2,008 / 698,053 PAs",
        rel_floor=0.20,
        must_detect=0.0056,
        detect_source=(
            "docs/audit/2026-08-20-sim429-514-diagnosis-results.md: the retired "
            "formula issued 0.0085/PA — a +0.0056 deviation, 9.7x this floor."
        ),
    ),
    "K_PA": PoolReference(
        0.2165,
        f"{_CENSUS}: chain(pool per-count rates) K/PA",
        rel_floor=0.02,
        floor_rationale="Measured tilt +0.3%; same sizing as BB_PA.",
    ),
    "HBP_PA": PoolReference(
        0.0113,
        f"{_CENSUS}: chain(pool per-count rates) HBP/PA",
        rel_floor=0.06,
        floor_rationale=(
            "A rare channel: binomial noise ~1% of centre at lane volume; 6% "
            "absorbs count-mix drift while catching a SIM-509-scale mislabel."
        ),
    ),
    "PITCHES_PA": PoolReference(
        3.938,
        f"{_CENSUS}: chain(pool per-count rates) pitches/PA",
        rel_floor=0.015,
        is_proportion=False,
        floor_rationale="A mean, not a proportion; measured drift +0.1%.",
    ),
    # --- per ball in play --------------------------------------------------
    "SINGLES_BIP": PoolReference(
        0.2050,
        f"{_CENSUS}: singles / consistent BIP",
        rel_floor=0.025,
        floor_rationale="Rule-B sizing; base-out cell-mix drift measured <1%.",
    ),
    "DOUBLES_BIP": PoolReference(
        0.0625,
        f"{_CENSUS}: doubles / consistent BIP",
        rel_floor=0.03,
        floor_rationale="Rule-B sizing beside SINGLES_BIP.",
    ),
    "TRIPLES_BIP": PoolReference(
        0.00546,
        f"{_CENSUS}: triples / consistent BIP",
        rel_floor=0.10,
        floor_rationale=(
            "The rarest hit: the diagnosis measured the draw at 0.973x the "
            "pool with ~5% cell-mix spread; 10% absorbs that and still reds a "
            "class defect."
        ),
    ),
    "HR_BIP": PoolReference(
        0.0459,
        f"{_CENSUS}: home runs / consistent BIP",
        rel_floor=0.035,
        floor_rationale=(
            "Rule-B sizing: the tightest floor a 12x500 lane's ~300k balls in "
            "play resolves at Z=4 (3% needed ~370k and could never bind)."
        ),
    ),
    "ROE_BIP": PoolReference(
        0.00828,
        f"{_CENSUS}: field_error / consistent BIP",
        rel_floor=0.10,
        floor_rationale="Rare-channel sizing, as TRIPLES_BIP.",
    ),
    # --- per opportunity ---------------------------------------------------
    # SIM-476 (step 0): the steal bands. Centres are the artifact steal
    # pools' OWN recency-weighted rates (printed by
    # scripts/sim476_steal_probe.py from the loaded bundle — the exact object
    # the draw samples). The -15% deficit these guard was the aggression
    # multiplier's leverage factor, deleted 2026-08-30.
    "STEAL_ATT_OPP_2B": PoolReference(
        0.0214,
        "the W1 artifact steal pool [2B], recency-weighted (sim476_steal_probe)",
        rel_floor=0.06,
        must_detect=0.0033,
        detect_source=(
            "docs/audit/2026-08-28-sim476-fit-plan.md part 1: the certified "
            "deficit was -15% of the centre — 2.5x this floor."
        ),
    ),
    "STEAL_ATT_OPP_3B": PoolReference(
        0.0044,
        "the W1 artifact steal pool [3B], recency-weighted (sim476_steal_probe)",
        rel_floor=0.15,
        floor_rationale=(
            "The rarest decision channel: ~18 opportunities/team-game puts the "
            "binomial term at ~13% of the centre at the 12x500 lane; 15% is "
            "the tightest floor that binds there."
        ),
    ),
    "STEAL_SAFE_2B": PoolReference(
        0.7989,
        "the W1 artifact steal pool [2B] safe share (sim476_steal_probe)",
        rel_floor=0.03,
        floor_rationale=(
            "The split was calibrated before SIM-476 (0.804 vs 0.795 measured); "
            "3% keeps it from regressing while the attempt weighting moves."
        ),
    ),
    "DP_OPP": PoolReference(
        0.1424,
        f"{_CENSUS}: r1-and-batter-retired rows / runner-on-1B <2-out BIP",
        rel_floor=0.05,
        must_detect=0.077,
        detect_source=(
            "BACKLOG.md (SIM-494 history): the phantom-DP era ran -77% "
            "per-game; per-opportunity that class of defect dwarfs a 5% floor. "
            "The diagnosis measured the live draw at +1.9%."
        ),
    ),
    # ---- SIM-517: the catcher receiving channels (2026-09-03) -------------
    "CALLED_STRIKE_TAKEN": PoolReference(
        0.31373,
        "the W1 artifact pitch pool: called strikes / taken pitches, "
        "recency-weighted (sim517_catcher_probe)",
        rel_floor=0.02,
        floor_rationale=(
            "~93 taken pitches/team-game puts the binomial term under 1% at "
            "the 12x500 lane; 2% is the tightest floor the receiving kernel's "
            "per-bucket normalization must hold (the marginal may not move — "
            "only the per-catcher conditional may)."
        ),
    ),
    "GOT_AWAY_PITCH": PoolReference(
        0.002325,
        "the W1 artifact pitch pool: got-away rows / pitches, "
        "recency-weighted (sim517_catcher_probe)",
        rel_floor=0.10,
        floor_rationale=(
            "A rare event (~0.34 got-aways/team-game, MLB 0.33-0.35): the "
            "binomial term is ~7% of the centre at the 12x500 lane, so 10% is "
            "the tightest floor that binds. The whole channel was MISSING "
            "before SIM-517 (rate 0), so any read at all beats the old sim."
        ),
    ),
}

#: Ordered pool-band channel names. The lane asserts every one of them.
POOL_CHANNELS: tuple[str, ...] = tuple(POOL_REFERENCES)

#: Box channels SUPERSEDED as certification by the pool bands (the 2026-08-20
#: ruling). They stay measured and reported — a reader still sees them — but
#: their per-team-game asserts no longer gate the lane; the per-opportunity
#: bands above do. R and home_win_pct are NOT here: the pool cannot state
#: them, so they stay game-graded. SB and CS joined on 2026-08-30 (SIM-476
#: step 0): their per-opportunity replacements (STEAL_ATT_OPP_2B/3B +
#: STEAL_SAFE_2B) now gate the running game.
SUPERSEDED_BY_POOL: tuple[str, ...] = (
    "H",
    "HR",
    "2B",
    "3B",
    "BB",
    "K",
    "DP",
    "ROE",
    "ROE_reached",
    "SB",
    "CS",
)


@dataclass(frozen=True, slots=True)
class PoolBandResult:
    """The full arithmetic behind one pool channel's verdict."""

    channel: str
    numerator: float
    denominator: float
    rate: float
    centre: float
    floor: float
    se_term: float
    half_width: float
    delta: float
    verdict: str  # PASS | RED | UNDERPOWERED
    source: str

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"

    def explain(self) -> str:
        rel = (self.delta / self.centre * 100.0) if self.centre else 0.0
        return (
            f"{self.channel}: rate {self.rate:.5f} = {self.numerator:.0f}/"
            f"{self.denominator:.0f} vs pool centre {self.centre:.5f} "
            f"(delta {self.delta:+.5f} = {rel:+.1f}%), half-width "
            f"{self.half_width:.5f} (floor {self.floor:.5f}, Z*se "
            f"{self.se_term:.5f}) -> {self.verdict}   [{self.source}]"
        )


def evaluate_pool(channel: str, numerator: float, denominator: float) -> PoolBandResult:
    """Score one per-opportunity channel against its pool band (SIM-516)."""
    ref = POOL_REFERENCES[channel]
    if denominator <= 0:
        raise ValueError(f"{channel}: the lane produced no opportunities")
    rate = float(numerator) / float(denominator)
    floor = ref.floor()
    if ref.is_proportion:
        se = math.sqrt(max(ref.centre * (1.0 - ref.centre), 1e-12) / float(denominator))
        se_term = Z * se
    else:
        # A mean channel carries no binomial term; the floor is the band and a
        # thin denominator cannot certify it.
        se_term = 0.0 if denominator >= 10_000 else float("inf")
    half_width = max(floor, se_term)
    delta = rate - ref.centre
    if se_term > floor:
        verdict = "UNDERPOWERED"
    elif abs(delta) <= half_width:
        verdict = "PASS"
    else:
        verdict = "RED"
    return PoolBandResult(
        channel=channel,
        numerator=float(numerator),
        denominator=float(denominator),
        rate=rate,
        centre=ref.centre,
        floor=floor,
        se_term=se_term,
        half_width=half_width,
        delta=delta,
        verdict=verdict,
        source=ref.source,
    )
