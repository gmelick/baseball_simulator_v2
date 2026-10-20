"""The profile build's sample floors, shrinkage priors and positional-insert
column orders."""

from __future__ import annotations

# derived.baserunner_season_metrics takes a POSITIONAL insert (no column
# list), so Savant's baserunning columns go LAST, in this order, after
# ``asof_date``. The builder's SELECT appends them in ``XB_COLUMN_ORDER``, and a
# unit test holds the live table's tail to ``BASERUNNER_TAIL_COLUMNS``.
XB_COLUMN_ORDER: tuple[str, ...] = (
    "xb_opportunities",
    "xb_attempt_rate",
    "xb_expected_attempt_rate",
    "xb_attempt_rate_above_expected",
)
BASERUNNER_TAIL_COLUMNS: tuple[str, ...] = ("asof_date", *XB_COLUMN_ORDER)

# The sample floors (each matches its column's comment in the DuckDB schema).
MIN_PITCHER_PITCHES = 200
MIN_BATTER_PA = 100
MIN_FIELDER_BIP = 50
MIN_RUNNER_ADV_OPPS = 20
MIN_RUNNER_SB_ATTEMPTS = 5
MIN_FIELDER_PLAYS = 50
MIN_CATCHER_SB_ATTEMPTS = 10
MIN_MANAGER_GAMES = 50

#: The chance count at which an outfielder's arm block repeats year to year
#: at about 0.5 (measured on the advancement pool; the outfield arm block,
#: SIM-550). The fielder model shares this value: it is the prior of the arm
#: group's shrinkage confidence, chances / (chances + 50).
ARM_ALPHA_PRIOR_CHANCES = 50

# derived.fielder_season_metrics takes a POSITIONAL insert (no column list),
# so Savant's outs above average and the outfield jump go LAST, in this order,
# after ``asof_date``. The aggregator's SELECT appends them in
# ``OAA_JUMP_COLUMN_ORDER``, and a unit test holds the live table's tail to
# ``FIELDER_TAIL_COLUMNS``.
OAA_JUMP_COLUMN_ORDER: tuple[str, ...] = (
    "savant_oaa",
    "savant_oaa_per_100",
    "jump_reaction_ft",
    "jump_burst_ft",
    "jump_route_ft",
    "jump_plays",
)
FIELDER_TAIL_COLUMNS: tuple[str, ...] = ("asof_date", *OAA_JUMP_COLUMN_ORDER)

#: The play count at which the outfield jump's year-to-year repeat passes
#: 0.8 (the Savant outs-above-average and jump fit, SIM-532). The fielder
#: model shares this value: it is the prior of the jump features' shrinkage
#: confidence, plays / (plays + 25).
JUMP_ALPHA_PRIOR_PLAYS = 25


# The FIP constant: one league-wide approximation for every season.
FIP_CONSTANT = 3.17

# The park factor's shrinkage toward league average (1.0): at
# ``sample_pa == PARK_PRIOR_PA`` the estimate is half the prior, half the park's own.
PARK_PRIOR_PA = 2000
