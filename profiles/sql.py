"""The SQL fragments that the profile and pool builds share.

The fragments: the season list, the pitch-result classes, the swing and stance columns, the
out labels, the barrel band, the fielding-credit mask and the fatigue window expressions.
"""

from __future__ import annotations

from ingest.statcast_events import (
    BALL_TYPES,
    CALLED_STRIKE_TYPES,
    FOUL_TYPES,
    IN_PLAY_TYPES,
    STRIKE_THREE_FOUL_TYPES,
    WHIFF_TYPES,
    sql_batter_retired,
    sql_fielding_out,
    sql_in_play,
    sql_list,
    sql_outs_recorded,
    sql_swing,
)


def sql_seasons(seasons) -> str:
    """Render seasons as the inside of a SQL ``IN`` list: ``2025, 2026``."""
    return ", ".join(str(int(s)) for s in seasons)


# ---------------------------------------------------------------------------
# The Statcast pitch-result codes
#
# ingest/statcast_events.py defines one code set per pitch-pool class (the API
# reads them there too). The pool's class expression (SQL_OUTCOME_TYPE, below)
# is rendered from these sets, and the profile SQL reads WHIFF_TYPES and
# NON_SWING_TYPES, so a pitcher's whiff rate and the pool the simulator draws
# from describe the same events.
# `tests/unit/profiles/test_profile_code_sets.py` and
# `tests/unit/test_foul_tip_strike_three.py` run the expression and fail if the
# two ever diverge.
# ---------------------------------------------------------------------------


SQL_WHIFF = f"type IN {sql_list(WHIFF_TYPES)}"

#: The pitch pool's class for one ``raw.pitches`` row: the class the
#: simulator's count machine reads. The first matching branch wins. With two
#: strikes, a foul tip or a foul bunt (STRIKE_THREE_FOUL_TYPES) is strike three,
#: so the two-strike branch MUST precede the foul branch. ``strikes`` is the
#: count BEFORE the pitch. The
#: events branch comes first, so a hit-by-pitch never becomes ball four; the
#: 'H' code catches a hit-by-pitch row with no event label (one exists, in
#: 2022). There is no ELSE on purpose: an unknown code yields NULL, the NOT
#: NULL ``outcome_type`` column refuses the insert, and a person looks.
SQL_OUTCOME_TYPE = f"""CASE
                        WHEN events = 'hit_by_pitch' OR TRIM(type) = 'H' THEN 'hit_by_pitch'
                        WHEN TRIM(type) IN {sql_list(BALL_TYPES)} THEN 'ball'
                        WHEN TRIM(type) IN {sql_list(CALLED_STRIKE_TYPES)} THEN 'called_strike'
                        WHEN TRIM(type) IN {sql_list(WHIFF_TYPES)} THEN 'swinging_strike'
                        WHEN TRIM(type) IN {sql_list(STRIKE_THREE_FOUL_TYPES)} AND strikes = 2
                            THEN 'swinging_strike'
                        WHEN TRIM(type) IN {sql_list(FOUL_TYPES)} THEN 'foul'
                        WHEN TRIM(type) IN {sql_list(IN_PLAY_TYPES)} THEN 'in_play'
                    END"""


# ===========================================================================
# The physical swing and stance columns of the batter profile
# ===========================================================================
# The swing features: (profile stem, column on the leaderboard table, column on
# the per-pitch table). They come from the bat-tracking and swing-path boards,
# pulled three times: every swing, then against left-handed and against
# right-handed pitchers.
#
# The per-pitch table is the preferred source, because it aggregates to any
# cutoff date. The leaderboard column is the fallback: the per-pitch export
# carries no bat tracking before 2024, so 2023 has no other source.
#
# ``contact_depth`` measures contact relative to the BATTER, not to the plate:
# the per-pitch export publishes only that figure, and it describes the
# batter's own swing. Where he stands in the box is ``stance_depth``.
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

#: The exact order the physical columns are appended to
#: ``derived.batter_season_metrics``.
#:
#: ⚠ POSITIONAL-INSERT TRAP. ``_compute_batter_profiles`` runs
#: ``INSERT OR REPLACE`` with NO column list, so DuckDB matches the final
#: SELECT to the table by POSITION. ``ALTER TABLE ... ADD COLUMN`` appends to
#: the end of the table, so these columns come last in the SELECT, in exactly
#: this order. Migration 0025 adds them in this order, the SELECT tail is
#: generated from this tuple, and ``test_batter_physical_features.py`` asserts
#: that the live table's trailing columns match it. Do not hand-edit one side.
PHYSICAL_COLUMN_ORDER: tuple[str, ...] = tuple(
    f"{stem}{suffix}"
    for stem in (*(f[0] for f in _SWING_FEATURES), *(f[0] for f in _STANCE_FEATURES))
    for suffix in ("", "_vs_l", "_vs_r")
) + ("physical_swings", "physical_swings_vs_l", "physical_swings_vs_r")

#: The slowest bat speed (mph) that counts as a competitive swing, fitted
#: against Savant's own published numbers.
#:
#: Savant's leaderboards average "competitive" swings (no bunts, no checked
#: swings) but do not publish the cut. The per-pitch export leaves the tracking
#: columns empty on anything it did not measure, so the cut must be rebuilt.
#: Against the 2024 leaderboard, for the 372 batters with 300 or more swings,
#: 62 mph matches the mean bat speed to 0.005 mph and the swing count to under
#: two swings in about 470. A lower cut drags slow swings into the average
#: (60 mph reads 0.25 mph slow); a higher cut throws real swings away (65 mph
#: loses 55 swings per batter).
COMPETITIVE_BAT_SPEED_MIN = 62.0


def _sql_swing_pivot() -> str:
    """Return the SQL that turns the three split ROWS per batter-season into one row of columns."""
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
    """Return the SQL that turns the per-batting-side stance ROWS into one row of columns.

    A switch hitter has two rows here and keeps both.
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
    """Return the SQL of the swing columns, one line per (feature, split).

    Two sources: the per-pitch aggregate wins when it saw at least 80% of the
    leaderboard's swings; otherwise the leaderboard does.

    The per-pitch aggregate is the point-in-time source and normally wins. A
    partly backfilled per-pitch table can hold a few April games against a
    full-season leaderboard row; preferring it blindly would replace a
    600-swing average with a 50-swing one. The swing-count comparison picks
    the better source either way, with no flag for the table's state.

    On a cutoff build, the leaderboard row of the cutoff's own season is dated
    after the cutoff and never reaches this join, so the per-pitch value
    stands alone.

    ``split`` is the PITCHER's hand, so ``vs_l`` already means "against
    left-handed pitching" and needs no crossing over.
    """
    lines = []
    # The per-pitch source wins once it holds most of the season's swings. An
    # exact ">=" would split the batters roughly in half (the per-pitch count
    # runs a swing or two under the board's for about half of them), and the
    # profile would draw from two sources at once for no benefit.
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
    """Return the SQL of the per-pitch swing aggregate, one line per (feature, split).

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
    """Return the SQL of the stance columns, one line per (feature, split).

    Savant keys this board by the BATTING side, and the pitcher's hand decides
    a switch hitter's side, so the two cross over. Facing a left-handed pitcher,
    a switch hitter bats RIGHT, so ``_vs_l`` reads the ``R`` stance row. A
    batter with only one side uses it for both.

    The overall column blends a switch hitter's two stances by how often he
    faced each hand. His left-side stance is the one he used against
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


SQL_SWING = sql_swing()


# ---------------------------------------------------------------------------
# The out labels. Every out label derives from `events`, never from
# `outs_on_pitch`, which is zero on most batted-ball outs. The vocabulary and
# its measurements live in ingest/statcast_events.py. Each site states WHICH
# question it asks: "how many outs did the play record?" and "was the batter
# retired?" are different questions, and the fielder's-choice events answer
# them differently.
# ---------------------------------------------------------------------------

SQL_IN_PLAY = sql_in_play()
SQL_OUTS_RECORDED = sql_outs_recorded()
SQL_FIELDING_OUT = sql_fielding_out()
SQL_BATTER_RETIRED = sql_batter_retired()

#: The pull-side spray angle. The raw `spray_angle` sign is FIELD-side, not
#: pull-side: negative is left field. `stand` flips the sign per plate
#: appearance, so "< -15" reads PULL for both hands. `stand` is never 'S'.
SQL_PULL_SPRAY = "(CASE WHEN stand = 'L' THEN -spray_angle ELSE spray_angle END)"


# A barrel (Statcast's sliding-scale definition): exit velocity (EV) >= 98 mph
# and a launch angle (LA) inside a band that WIDENS as EV rises. At 98 mph the
# band is 26-30 degrees. Each mph above 98 drops the lower bound 1 degree and
# raises the upper bound 2 degrees, up to 8-50 degrees at about 116 mph, where
# the band stops widening. Below 98 mph a ball is never a barrel.
#   lower_la = GREATEST(BARREL_LA_FLOOR,  BARREL_MIN_LA - (EV - BARREL_MIN_VELO) * BARREL_LOWER_SLOPE)
#   upper_la = LEAST   (BARREL_LA_CEILING, BARREL_MAX_LA + (EV - BARREL_MIN_VELO) * BARREL_UPPER_SLOPE)
# A hard-hit ball: EV >= 95 mph.
BARREL_MIN_VELO = 98.0  # mph
BARREL_MIN_LA = 26.0  # degrees, the lower bound at 98 mph
BARREL_MAX_LA = 30.0  # degrees, the upper bound at 98 mph
BARREL_LOWER_SLOPE = 1.0  # degrees the lower bound drops per mph above 98
BARREL_UPPER_SLOPE = 2.0  # degrees the upper bound rises per mph above 98
BARREL_LA_FLOOR = 8.0  # degrees, the lowest lower bound
BARREL_LA_CEILING = 50.0  # degrees, the highest upper bound
HARD_HIT_MIN_VELO = 95.0


def _barrel_case_sql(prefix: str = "") -> str:
    """Return a DuckDB boolean expression, TRUE when the row is a Statcast barrel.

    A NULL launch_speed or launch_angle yields NULL, which the enclosing
    SUM(CASE ...) counts as not a barrel. ``prefix`` goes before the
    predicate, so a platoon variant can gate on e.g. ``p_throws='L' AND``.
    """
    return (
        f"{prefix}launch_speed >= {BARREL_MIN_VELO} "
        f"AND launch_angle >= GREATEST({BARREL_LA_FLOOR}, "
        f"{BARREL_MIN_LA} - (launch_speed - {BARREL_MIN_VELO}) * {BARREL_LOWER_SLOPE}) "
        f"AND launch_angle <= LEAST({BARREL_LA_CEILING}, "
        f"{BARREL_MAX_LA} + (launch_speed - {BARREL_MIN_VELO}) * {BARREL_UPPER_SLOPE})"
    )


#: The loader's fielding-credit slot columns on raw.pitches.
_PUTOUT_SLOTS: tuple[str, ...] = ("field_putout_1", "field_putout_2", "field_putout_3")
_ASSIST_SLOTS: tuple[str, ...] = (
    "field_assist_1",
    "field_assist_2",
    "field_assist_3",
    "field_assist_4",
    "field_assist_5",
)


def sql_credit_mask(prefix: str, slot_cols: tuple[str, ...], pitcher_expr: str) -> str:
    """Return the SQL of a POSITION bitmask of fielding credits.

    Bit k (1 << k, k = 1..9) is set when the player at position k on the
    play (``fielder_2`` .. ``fielder_9`` on the row; ``pitcher_expr`` for
    position 1) appears in any of ``slot_cols`` (the loader's
    ``field_putout_1..3`` / ``field_assist_1..5``). The mask is the chain the
    fielding draw reads: every fielder credited on the play. It is 0 when no
    slot is filled (every hit) or no slot matches the alignment.
    """
    terms = []
    for k in range(1, 10):
        who = pitcher_expr if k == 1 else f"{prefix}fielder_{k}"
        match = " OR ".join(f"{prefix}{c} = {who}" for c in slot_cols)
        terms.append(f"(CASE WHEN {who} IS NOT NULL AND ({match}) THEN {1 << k} ELSE 0 END)")
    return " + ".join(terms)


#: The two fatigue columns, as window expressions over raw.pitches. They live
#: at module level so a unit test can run them on a synthetic table against
#: the live helper ``simulation.loop.values.times_through_order``: ONE
#: definition on both sides of the draw.
#:   * the pitches this pitcher threw in this game BEFORE this plate
#:     appearance (an empty frame on his first one counts 0);
#:   * 1 + (batters faced before this plate appearance) // 9.
SQL_PITCHER_PITCH_COUNT = (
    "COUNT(*) OVER (PARTITION BY game_pk, pitcher ORDER BY at_bat_number "
    "RANGE BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)"
)
SQL_TIMES_THROUGH_ORDER = (
    "((DENSE_RANK() OVER (PARTITION BY game_pk, pitcher ORDER BY at_bat_number) - 1) // 9 + 1)"
)
