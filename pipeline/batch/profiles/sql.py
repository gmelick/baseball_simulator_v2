"""The SQL fragments the profile and pool builds share: the pitch-result
classes, the swing and stance columns, the out labels, the barrel band, the
fielding-credit mask and the fatigue window expressions.
"""

from __future__ import annotations

from pipeline.statcast_events import (
    BALL_TYPES,
    CALLED_STRIKE_TYPES,
    FOUL_TYPES,
    IN_PLAY_TYPES,
    NON_SWING_TYPES,
    STRIKE_THREE_FOUL_TYPES,
    WHIFF_TYPES,
    sql_batter_retired,
    sql_fielding_out,
    sql_in_play,
    sql_list,
    sql_outs_recorded,
)

#: SIM-532: the outfield test the aggregator's jump columns use. The jump is
#: an outfield measurement, so an infield row gets NULL, never the player's
#: figure from an outfield season.
_SQL_IS_OF = "c.position IN ('LF', 'CF', 'RF')"


# ---------------------------------------------------------------------------
# Statcast pitch-result codes (SIM-456 / SIM-501 / SIM-553)
#
# One code set per pitch-pool class. The pool build's class expression
# (SQL_OUTCOME_TYPE, below) is rendered from these sets, and the profile SQL
# reads WHIFF_TYPES / NON_SWING_TYPES, so a pitcher's whiff rate and the pool
# the simulator draws from describe the same events.
# `tests/unit/test_profile_code_sets.py` and
# `tests/unit/test_foul_tip_strike_three.py` run the expression and fail
# if the two ever diverge.
#
# The sets live in pipeline/statcast_events.py since the Data Lab vocabulary
# fix (SIM-571). The API reads them there, without this module's duckdb and
# sklearn imports. This module imports the sets and keeps their names, so
# `from pipeline.batch.player_profile_computor import WHIFF_TYPES` still works.
# ---------------------------------------------------------------------------


def sql_in(types: tuple[str, ...]) -> str:
    """Render Statcast type codes as a SQL ``IN`` list: ``('S', 'W', 'M')``.

    Delegates to the shared renderer so the two cannot drift (SIM-501a).
    """
    return sql_list(types)


SQL_WHIFF = f"type IN {sql_in(WHIFF_TYPES)}"


#: SIM-553: the pitch pool's class for one ``raw.pitches`` row — the class the
#: simulator's count machine reads. The first matching branch wins, so the
#: two-strike branch MUST precede the foul branch. ``strikes`` is the count
#: BEFORE the pitch. The events branch comes first (SIM-509: an HBP pitch must
#: never become ball four); the 'H' code covers the one 2022 hit-by-pitch row
#: with no event label. There is no ELSE on purpose: an unknown code yields
#: NULL, the NOT NULL ``outcome_type`` column refuses the insert, and a person
#: looks. The old ``ELSE 'ball'`` is how 'O', 'Q' and the hit-by-pitch rows hid.
SQL_OUTCOME_TYPE = f"""CASE
                        WHEN events = 'hit_by_pitch' OR TRIM(type) = 'H' THEN 'hit_by_pitch'
                        WHEN TRIM(type) IN {sql_in(BALL_TYPES)} THEN 'ball'
                        WHEN TRIM(type) IN {sql_in(CALLED_STRIKE_TYPES)} THEN 'called_strike'
                        WHEN TRIM(type) IN {sql_in(WHIFF_TYPES)} THEN 'swinging_strike'
                        WHEN TRIM(type) IN {sql_in(STRIKE_THREE_FOUL_TYPES)} AND strikes = 2
                            THEN 'swinging_strike'
                        WHEN TRIM(type) IN {sql_in(FOUL_TYPES)} THEN 'foul'
                        WHEN TRIM(type) IN {sql_in(IN_PLAY_TYPES)} THEN 'in_play'
                    END"""


# ===========================================================================
# SIM-529 — the physical swing and stance columns
# ===========================================================================
# (profile column stem, source column on the Savant raw table)
#
# The swing features come from the bat-tracking and swing-path boards, which are
# pulled three times: every swing, then filtered to left-handed and to
# right-handed pitchers.
# (profile stem, column on the leaderboard table, column on the per-pitch table)
#
# SIM-534: the per-pitch table is the preferred source, because it can be
# aggregated to any cutoff date. The leaderboard column is the fallback, and it
# is not dead weight — the per-pitch export carries no bat tracking before 2024,
# so 2023 has no other source.
#
# ``contact_depth`` measures contact relative to the BATTER, not to the plate.
# SIM-529 used the vs-plate figure; the per-pitch export publishes only the
# vs-batter one, and vs-batter is the better feature anyway: it describes the
# batter's own swing geometry, and where he stands in the box is already carried
# separately as ``stance_depth``.
_SWING_FEATURES: tuple[tuple[str, str, str], ...] = (
    ("bat_speed", "avg_bat_speed", "bat_speed"),
    ("swing_length", "swing_length", "swing_length"),
    ("swing_tilt", "swing_tilt", "swing_path_tilt"),
    ("attack_angle", "attack_angle", "attack_angle"),
    ("attack_direction", "attack_direction", "attack_direction"),
    ("contact_depth", "intercept_y_vs_batter", "intercept_y"),
)


# The stance features come from the stance board, which Savant publishes with
# one row per BATTING side.
_STANCE_FEATURES: tuple[tuple[str, str], ...] = (
    ("stance_foot_sep", "foot_sep"),
    ("stance_angle", "stance_angle"),
    ("stance_depth", "batter_y_position"),
    ("stance_off_plate", "batter_x_position"),
)


#: The exact order the SIM-529 columns are appended to
#: ``derived.batter_season_metrics``.
#:
#: ⚠ POSITIONAL-INSERT TRAP. ``_compute_batter_profiles`` runs
#: ``INSERT OR REPLACE`` with NO column list, so DuckDB matches the final
#: SELECT to the table by POSITION. ``ALTER TABLE ... ADD COLUMN`` appends to
#: the end of the table, so these must come last in the SELECT and in exactly
#: this order. Migration 0025 adds them in this order, the SELECT tail is
#: generated from this tuple, and ``test_batter_physical_features.py`` asserts the
#: live table's trailing columns match it. Do not hand-edit one side.
PHYSICAL_COLUMN_ORDER: tuple[str, ...] = tuple(
    f"{stem}{suffix}"
    for stem in (*(f[0] for f in _SWING_FEATURES), *(f[0] for f in _STANCE_FEATURES))
    for suffix in ("", "_vs_l", "_vs_r")
) + ("physical_swings", "physical_swings_vs_l", "physical_swings_vs_r")


#: Everything appended to ``derived.batter_season_metrics`` after ``updated_at``,
#: in order. The physical block (migration 0025) then the cutoff stamp
#: (migration 0026). Same positional-insert warning as above.
PROFILE_TAIL_COLUMNS: tuple[str, ...] = (*PHYSICAL_COLUMN_ORDER, "asof_date")


#: SIM-534: what counts as a competitive swing, FITTED against Savant's own
#: published numbers rather than guessed.
#:
#: Savant's leaderboards average "competitive" swings — excluding bunts and
#: checked swings — but do not publish the cut. The per-pitch export just leaves
#: the tracking columns empty on anything it did not measure, so the cut has to
#: be reconstructed. Comparing our per-pitch average against the 2024
#: leaderboard for the 372 batters with 300 or more swings:
#:
#:     threshold   bat-speed error   swing-count error
#:        50 mph      -0.791 mph          +47.1
#:        55 mph      -0.608 mph          +39.3
#:        58 mph      -0.428 mph          +29.2
#:        60 mph      -0.253 mph          +17.7
#:        62 mph      +0.005 mph           -1.6   <-- fitted
#:        65 mph      +0.618 mph          -55.4
#:
#: At 62 the two agree to five thousandths of a mile per hour and to under two
#: swings in roughly 470. Anything lower drags slow swings into the average;
#: anything higher throws real ones away.
COMPETITIVE_BAT_SPEED_MIN = 62.0


def _sql_swing_pivot() -> str:
    """Turn the three split ROWS per batter-season into one row of columns."""
    lines = []
    for _, src, _pitch_src in _SWING_FEATURES:
        for split in ("all", "vs_l", "vs_r"):
            lines.append(
                f"                    MAX(CASE WHEN split = '{split}' THEN {src} END)"
                f" AS {src}_{split},"
            )
    for split in ("all", "vs_l", "vs_r"):
        trail = "," if split != "vs_r" else ""
        lines.append(
            f"                    MAX(CASE WHEN split = '{split}' THEN competitive_swings END)"
            f" AS competitive_swings_{split}{trail}"
        )
    return "\n".join(lines)


def _sql_stance_pivot() -> str:
    """Turn the per-batting-side stance ROWS into one row of columns.

    A switch hitter has two rows here and keeps both (owner ruling 2026-09-10).
    """
    lines = []
    for i, (_, src) in enumerate(_STANCE_FEATURES):
        trail = "," if i < len(_STANCE_FEATURES) - 1 else ""
        lines.append(
            f"                    MAX(CASE WHEN bat_side = 'L' THEN {src} END) AS {src}_l,\n"
            f"                    MAX(CASE WHEN bat_side = 'R' THEN {src} END) AS {src}_r{trail}"
        )
    return "\n".join(lines)


def _sql_swing_select() -> str:
    """The swing columns, one line per (feature, split).

    SIM-534: two sources, and the one that saw MORE swings wins.

    The per-pitch aggregate is the point-in-time source and is normally the one
    to use. But "normally" is not "always": while the per-pitch table is still
    being backfilled it may hold a few April games against a leaderboard row
    covering the whole season, and preferring it blindly would replace a
    600-swing average with a 50-swing one. Comparing the swing counts picks the
    better source in both directions and needs no flag to say which state we are
    in.

    For a cutoff build the question does not arise: a leaderboard row for the
    cutoff's own season is dated after the cutoff and never reaches this join,
    so the per-pitch value stands alone.

    ``split`` is the PITCHER's hand, so ``vs_l`` already means "against
    left-handed pitching" and needs no crossing over.
    """
    lines = []
    # Prefer the per-pitch source once it holds most of the season's swings. An
    # exact ">=" was tried first and split the population roughly in half —
    # per-pitch runs a swing or two under the board for about half of batters —
    # which left the profile drawing from two sources at once for no benefit.
    # The point-in-time source should win whenever it is substantially complete.
    better = (
        "COALESCE(swp.competitive_swings_all, 0) >= 0.8 * COALESCE(sw.competitive_swings_all, 0)"
    )
    for stem, src, _pitch_src in _SWING_FEATURES:
        for suffix, split in (("", "all"), ("_vs_l", "vs_l"), ("_vs_r", "vs_r")):
            lines.append(
                f"                COALESCE("
                f"CASE WHEN {better} THEN swp.{stem}_{split} END,"
                f" sw.{src}_{split}, swp.{stem}_{split}) AS {stem}{suffix},"
            )
    return "\n".join(lines)


def _sql_pitch_swing_agg() -> str:
    """The per-pitch swing aggregate, one line per (feature, split).

    Each column averages the tracked swings of that split. The split is the
    PITCHER's hand, read from ``raw.pitches``, so a switch hitter's two sides
    separate exactly as they do on the leaderboard.
    """
    splits = (("all", "TRUE"), ("vs_l", "p.p_throws = 'L'"), ("vs_r", "p.p_throws = 'R'"))
    lines = []
    for _stem, _src, pitch_src in _SWING_FEATURES:
        stem = _stem
        for split, cond in splits:
            lines.append(
                f"                    AVG(CASE WHEN {cond} THEN t.{pitch_src} END)"
                f" AS {stem}_{split},"
            )
    for i, (split, cond) in enumerate(splits):
        trail = "," if i < len(splits) - 1 else ""
        lines.append(
            f"                    COUNT(CASE WHEN {cond} THEN 1 END)"
            f" AS competitive_swings_{split}{trail}"
        )
    return "\n".join(lines)


def _sql_stance_select() -> str:
    """The stance columns, one line per (feature, split).

    Savant keys this board by the BATTING side, and a batter's side is decided
    by the pitcher's hand — so the two cross over. Facing a left-handed pitcher a
    switch hitter bats RIGHT, so ``_vs_l`` reads the ``R`` stance row. A batter
    with only one side uses it for both.

    The overall column blends a switch hitter's two stances by how often he
    actually faced each hand: his left-side stance is the one he used against
    right-handed pitchers, so it carries the vs-right plate appearances.
    """
    lines = []
    for stem, src in _STANCE_FEATURES:
        left, right = f"st.{src}_l", f"st.{src}_r"
        lines.append(
            f"                CASE WHEN {left} IS NOT NULL AND {right} IS NOT NULL\n"
            f"                          AND COALESCE(bp.sample_pa_vs_l, 0)"
            f" + COALESCE(bp.sample_pa_vs_r, 0) > 0\n"
            f"                     THEN ({left} * COALESCE(bp.sample_pa_vs_r, 0)\n"
            f"                         + {right} * COALESCE(bp.sample_pa_vs_l, 0))\n"
            f"                        / (COALESCE(bp.sample_pa_vs_l, 0)"
            f" + COALESCE(bp.sample_pa_vs_r, 0))\n"
            f"                     ELSE COALESCE({left}, {right}) END AS {stem},"
        )
        lines.append(f"                COALESCE({right}, {left}) AS {stem}_vs_l,")
        lines.append(f"                COALESCE({left}, {right}) AS {stem}_vs_r,")
    return "\n".join(lines)


SQL_SWING = f"type NOT IN {sql_in(NON_SWING_TYPES)}"


# ---------------------------------------------------------------------------
# Out labels (SIM-501a/c) — every out label derives from `events`, never from
# `outs_on_pitch` (zero on 92.6% of batted-ball outs in the swept data).
# The vocabulary and its measurements live in pipeline/statcast_events.py.
# Each site below states WHICH question it asks — "how many outs did the play
# record?" and "was the batter retired?" are different questions, and the
# fielders-choice events answer them differently.
# ---------------------------------------------------------------------------

SQL_IN_PLAY = sql_in_play()


SQL_OUTS_RECORDED = sql_outs_recorded()


SQL_FIELDING_OUT = sql_fielding_out()


SQL_BATTER_RETIRED = sql_batter_retired()


#: Pull-side spray angle. The raw `spray_angle` sign is FIELD-side, not
#: pull-side: negative = left field (measured 2024: hc_x<100 rows average
#: -31.7, hc_x>150 rows average +32.9). `stand` flips the sign per plate
#: appearance, so "< -15" reads PULL for both hands (ground balls confirm:
#: R -12.7 raw, L +18.4 raw — both pull-side). `stand` is never 'S'.
SQL_PULL_SPRAY = "(CASE WHEN stand = 'L' THEN -spray_angle ELSE spray_angle END)"


# Barrel (Statcast sliding-scale definition).
# A batted ball is a *barrel* when exit velocity >= 98 mph AND launch angle falls
# inside a band that WIDENS as exit velocity increases.  At the 98 mph floor the
# band is launch_angle 26-30 deg; for every +1 mph above 98 the lower bound drops
# 1 deg and the upper bound rises 2 deg, expanding to roughly 8-50 deg at
# >= 116 mph (capped there).  Below 98 mph it is never a barrel.
#   lower_la = GREATEST(BARREL_LA_FLOOR,  BARREL_MIN_LA - (EV - BARREL_MIN_VELO) * BARREL_LOWER_SLOPE)
#   upper_la = LEAST   (BARREL_LA_CEILING, BARREL_MAX_LA + (EV - BARREL_MIN_VELO) * BARREL_UPPER_SLOPE)
# Hard hit: exit_velo >= 95 mph
BARREL_MIN_VELO = 98.0  # EV floor; below this never a barrel


BARREL_MIN_LA = 26.0  # lower LA bound at the 98 mph floor


BARREL_MAX_LA = 30.0  # upper LA bound at the 98 mph floor


BARREL_LOWER_SLOPE = 1.0  # deg the lower bound drops per +1 mph above floor


BARREL_UPPER_SLOPE = 2.0  # deg the upper bound rises per +1 mph above floor


BARREL_LA_FLOOR = 8.0  # lower LA bound never goes below this (>= ~116 mph)


BARREL_LA_CEILING = 50.0  # upper LA bound never goes above this (>= ~116 mph)


HARD_HIT_MIN_VELO = 95.0


def _barrel_case_sql(prefix: str = "") -> str:
    """Return a DuckDB SQL boolean expression that is TRUE when the row is a
    Statcast *barrel*.

    The sliding-scale band widens with exit velocity (see module constants).
    NULL launch_speed / launch_angle yield NULL (counted as not-a-barrel by the
    enclosing SUM(CASE ...)).  ``prefix`` is prepended to the predicate so the
    platoon variants can gate on e.g. ``p_throws='L' AND``.
    """
    return (
        f"{prefix}launch_speed >= {BARREL_MIN_VELO} "
        f"AND launch_angle >= GREATEST({BARREL_LA_FLOOR}, "
        f"{BARREL_MIN_LA} - (launch_speed - {BARREL_MIN_VELO}) * {BARREL_LOWER_SLOPE}) "
        f"AND launch_angle <= LEAST({BARREL_LA_CEILING}, "
        f"{BARREL_MAX_LA} + (launch_speed - {BARREL_MIN_VELO}) * {BARREL_UPPER_SLOPE})"
    )


#: SIM-523 part G: the loader's fielding-credit slot columns on raw.pitches.
_PUTOUT_SLOTS: tuple[str, ...] = ("field_putout_1", "field_putout_2", "field_putout_3")


_ASSIST_SLOTS: tuple[str, ...] = (
    "field_assist_1",
    "field_assist_2",
    "field_assist_3",
    "field_assist_4",
    "field_assist_5",
)


def sql_credit_mask(prefix: str, slot_cols: tuple[str, ...], pitcher_expr: str) -> str:
    """SIM-523 part G: the SQL for a POSITION bitmask of fielding credits.

    Bit k (1 << k, k = 1..9) is set when the player at position k on the
    play (``fielder_2`` .. ``fielder_9`` on the row; ``pitcher_expr`` for
    position 1) appears in any of ``slot_cols`` (the loader's
    ``field_putout_1..3`` / ``field_assist_1..5``). 0 when no slot is filled
    (every hit) or no slot matches the alignment — the chain the fielding
    draw reads (plan step 6: every fielder credited on the play)."""
    terms = []
    for k in range(1, 10):
        who = pitcher_expr if k == 1 else f"{prefix}fielder_{k}"
        match = " OR ".join(f"{prefix}{c} = {who}" for c in slot_cols)
        terms.append(f"(CASE WHEN {who} IS NOT NULL AND ({match}) THEN {1 << k} ELSE 0 END)")
    return " + ".join(terms)


#: SIM-518 (SIM-465): the two fatigue columns, as window expressions over
#: raw.pitches. Module-level so a unit test can run them on a synthetic table
#: against the live helper ``simulation.sim_loop.times_through_order`` — ONE
#: definition on both sides of the draw.
#:   * pitches this pitcher threw in this game BEFORE this plate appearance
#:     (an empty frame on his first PA counts 0);
#:   * 1 + (batters faced before this PA) // 9.
SQL_PITCHER_PITCH_COUNT = (
    "COUNT(*) OVER (PARTITION BY game_pk, pitcher ORDER BY at_bat_number "
    "RANGE BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)"
)


SQL_TIMES_THROUGH_ORDER = (
    "((DENSE_RANK() OVER (PARTITION BY game_pk, pitcher ORDER BY at_bat_number) - 1) // 9 + 1)"
)
