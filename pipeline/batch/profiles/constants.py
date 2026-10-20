"""The sample floors, shrinkage priors and positional-insert column orders of the profile build."""

from __future__ import annotations

# SIM-531: derived.baserunner_season_metrics carries a POSITIONAL insert (no
# column list), so the Savant baserunning columns go LAST, in this order, after
# ``asof_date`` — the SELECT below appends them in ``XB_COLUMN_ORDER`` and a
# unit test holds the live table's tail to ``BASERUNNER_TAIL_COLUMNS``.
XB_COLUMN_ORDER: tuple[str, ...] = (
    "xb_opportunities",
    "xb_attempt_rate",
    "xb_expected_attempt_rate",
    "xb_attempt_rate_above_expected",
)


BASERUNNER_TAIL_COLUMNS: tuple[str, ...] = ("asof_date", *XB_COLUMN_ORDER)


# Minimum sample thresholds (from schema comments)
MIN_PITCHER_PITCHES = 200


MIN_BATTER_PA = 100


MIN_FIELDER_BIP = 50


MIN_RUNNER_ADV_OPPS = 20


MIN_RUNNER_SB_ATTEMPTS = 5


MIN_FIELDER_PLAYS = 50


MIN_CATCHER_SB_ATTEMPTS = 10


MIN_MANAGER_GAMES = 50


#: SIM-550: the chance count at which an outfielder's arm block repeats
#: year to year at about 0.5 (measured on the advancement pool, plan §2.2).
#: The fielder model shares this value: it is the prior the arm group's own
#: shrinkage confidence uses, chances / (chances + 50).
ARM_ALPHA_PRIOR_CHANCES = 50


# SIM-532: derived.fielder_season_metrics carries a POSITIONAL insert (no
# column list), so Savant's outs above average and the outfield jump go LAST,
# in this order, after ``asof_date`` — the aggregator's SELECT appends them in
# ``OAA_JUMP_COLUMN_ORDER`` and a unit test holds the live table's tail to
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


#: SIM-532: the play count at which the outfield jump's year-to-year repeat
#: passes 0.8 (plan §2.2). The fielder model shares this value: it is the
#: prior the jump features' own shrinkage confidence uses,
#: plays / (plays + 25).
JUMP_ALPHA_PRIOR_PLAYS = 25


# FIP constant — approximation; update per-season if desired
FIP_CONSTANT = 3.17


# Park factor Bayesian shrinkage — weight of prior (1.0 = league average)
# At sample_pa = PARK_PRIOR_PA, posterior is 50/50 between prior and observed.
PARK_PRIOR_PA = 2000
