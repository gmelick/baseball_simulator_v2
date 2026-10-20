"""The pitch row: the required fields, the feed-to-column map, the row builder
and the two-tier validation.
"""

from __future__ import annotations

from dataclasses import (
    dataclass,
    field,
)
from typing import Any

from pipeline.etl.coercion import (
    to_bool,
    to_float,
    to_int,
    to_str,
)

# Columns that must be non-null on every pitch row regardless of pitch type.
ALWAYS_REQUIRED: set[str] = {
    "game_pk",
    "at_bat_number",
    "pitch_number",
    "game_date",
    "venue_id",
    "pitcher",
    "p_throws",
    "batter",
    "stand",
    "bat_hand",
    "inning",
    "inning_topbot",
    "balls",
    "strikes",
    "outs",
    "home_score",
    "away_score",
}

# SIM-441: how many fielding-credit slots raw.pitches actually has. A credit past
# the last slot sets `field_assist_6_plus` instead of being silently dropped.
_MAX_ASSIST_SLOTS = 5
_MAX_PUTOUT_SLOTS = 3
_MAX_THROWING_ERROR_SLOTS = 2

# Columns that must be present on an in-play pitch. In-play is THREE Gameday
# codes, not one: 'X' (out), 'D' (no out), 'E' (run). Gating on 'X' alone made
# the quality flag asymmetric — untracked outs were excluded downstream while
# identical untracked hits were kept.
IN_PLAY_REQUIRED: set[str] = {"launch_speed", "launch_angle", "bb_type"}


# ---------------------------------------------------------------------------
# Column rename map  (CSV / API variable name → PostgreSQL column name)
# ---------------------------------------------------------------------------

COLUMN_RENAME: dict[str, str] = {
    # pitch identity
    "pitch_code": "type",
    "pitch_code_description": "description",
    "pitch_type_description": "pitch_name",
    # strike zone
    "strike_zone_top": "sz_top",
    "strike_zone_bottom": "sz_bot",
    # physics
    "start_speed": "release_speed",
    "release_x": "release_pos_x",
    "release_y": "release_pos_y",
    "release_z": "release_pos_z",
    "velocity_x": "vx0",
    "velocity_y": "vy0",
    "velocity_z": "vz0",
    "acceleration_x": "ax",
    "acceleration_y": "ay",
    "acceleration_z": "az",
    "p_x": "plate_x",
    "p_z": "plate_z",
    "spin_rate": "release_spin_rate",
    "spin_direction": "spin_axis",
    # batted ball
    "total_distance": "hit_distance_sc",
    "coord_x": "hc_x",
    "coord_y": "hc_y",
    "batted_ball_type": "bb_type",
    "play_description": "des",
    # baserunners
    "pre_play_runner_on_first": "on_1b",
    "pre_play_runner_on_second": "on_2b",
    "pre_play_runner_on_third": "on_3b",
    "post_play_runner_on_first": "post_on_1b",
    "post_play_runner_on_second": "post_on_2b",
    "post_play_runner_on_third": "post_on_3b",
    # fielders (positional → positional number)
    "catcher": "fielder_2",
    "first_base": "fielder_3",
    "second_base": "fielder_4",
    "third_base": "fielder_5",
    "shortstop": "fielder_6",
    "left_field": "fielder_7",
    "center_field": "fielder_8",
    "right_field": "fielder_9",
    # outcome counts
    "runs": "runs_on_pitch",
    "rbis": "rbis_on_pitch",
    "earned_runs": "earned_runs_on_pitch",
    # runner scoring flags
    "runner_on_first_score": "runner_1b_scored",
    "runner_on_second_score": "runner_2b_scored",
    "runner_on_third_score": "runner_3b_scored",
    # misc
    "wild_pitch_passed_ball": "passed_ball_wild_pitch",
    "extension": "release_extension",
    "top_bot": "_inning_topbot_raw",  # transformed separately below
}

# inning_topbot value normalisation  (API → schema)
HALF_INNING_MAP = {
    "top": "Top",
    "bottom": "Bot",
}


# ---------------------------------------------------------------------------
# Row builder — rename + coerce every field
# ---------------------------------------------------------------------------
def _build_row_dict(
    raw: dict[str, Any], game_pk: int, season: int, game_date: str
) -> dict[str, Any]:
    """
    Takes the flat dict produced by write_rows() for a single pitch and
    returns a dict whose keys match raw.pitches column names exactly.

    All empty-string values are converted to None (→ SQL NULL).
    Types are coerced to match the PostgreSQL column types.
    """
    # Apply rename map first so downstream code uses schema names only.
    renamed: dict[str, Any] = {}
    for src_key, value in raw.items():
        dest_key = COLUMN_RENAME.get(src_key, src_key)
        renamed[dest_key] = value

    # inning_topbot normalisation
    raw_half = renamed.pop("_inning_topbot_raw", None)
    renamed["inning_topbot"] = HALF_INNING_MAP.get(str(raw_half).lower() if raw_half else "")

    # Ensure primary key fields are present
    renamed["game_pk"] = game_pk
    renamed["game_date"] = game_date

    row: dict[str, Any] = {
        # --- Natural key ---
        "game_pk": to_int(renamed.get("game_pk")),
        "at_bat_number": to_int(renamed.get("at_bat_number")),
        "pitch_number": to_int(renamed.get("pitch_number")),
        # --- Game context ---
        "game_date": renamed.get("game_date"),
        "season": to_int(season),
        "venue_id": to_int(renamed.get("venue_id")),
        "venue": to_str(renamed.get("venue")),
        "home_id": to_int(renamed.get("home_id")),
        "home_team": to_str(renamed.get("home_team")),
        "away_id": to_int(renamed.get("away_id")),
        "away_team": to_str(renamed.get("away_team")),
        "home_manager_id": to_int(renamed.get("home_manager_id")),
        "home_manager_name": to_str(renamed.get("home_manager_name")),
        "away_manager_id": to_int(renamed.get("away_manager_id")),
        "away_manager_name": to_str(renamed.get("away_manager_name")),
        # --- Inning state ---
        "inning": to_int(renamed.get("inning")),
        "inning_topbot": to_str(renamed.get("inning_topbot")),
        # --- Pitcher / batter ---
        "pitcher": to_int(renamed.get("pitcher")),
        "p_throws": to_str(renamed.get("p_throws")),
        "batter": to_int(renamed.get("batter")),
        "stand": to_str(renamed.get("stand")),
        "bat_hand": to_str(renamed.get("bat_hand")),
        # --- Score state ---
        "home_score": to_int(renamed.get("home_score")) or 0,
        "away_score": to_int(renamed.get("away_score")) or 0,
        "bat_score": to_int(renamed.get("bat_score")) or 0,
        "fld_score": to_int(renamed.get("fld_score")) or 0,
        # --- Count ---
        "balls": to_int(renamed.get("balls")) or 0,
        "strikes": to_int(renamed.get("strikes")) or 0,
        "outs": to_int(renamed.get("outs")) or 0,
        # --- Pitch result ---
        "type": to_str(renamed.get("type")),
        "description": to_str(renamed.get("description")),
        "pitch_type": to_str(renamed.get("pitch_type")),
        "pitch_name": to_str(renamed.get("pitch_name")),
        "events": to_str(renamed.get("events")) or to_str(renamed.get("event")),
        # --- Strike zone ---
        "sz_top": to_float(renamed.get("sz_top")),
        "sz_bot": to_float(renamed.get("sz_bot")),
        # --- Physics ---
        "release_speed": to_float(renamed.get("release_speed")),
        "end_speed": to_float(renamed.get("end_speed")),
        "release_pos_x": to_float(renamed.get("release_pos_x")),
        "release_pos_y": to_float(renamed.get("release_pos_y")),
        "release_pos_z": to_float(renamed.get("release_pos_z")),
        "vx0": to_float(renamed.get("vx0")),
        "vy0": to_float(renamed.get("vy0")),
        "vz0": to_float(renamed.get("vz0")),
        "ax": to_float(renamed.get("ax")),
        "ay": to_float(renamed.get("ay")),
        "az": to_float(renamed.get("az")),
        # --- Movement ---
        "pfx_x": to_float(renamed.get("pfx_x")),
        "pfx_z": to_float(renamed.get("pfx_z")),
        "plate_x": to_float(renamed.get("plate_x")),
        "plate_z": to_float(renamed.get("plate_z")),
        "x": to_float(renamed.get("x")),
        "y": to_float(renamed.get("y")),
        "release_spin_rate": to_int(renamed.get("release_spin_rate")),
        "spin_axis": to_int(renamed.get("spin_axis")),
        "break_angle": to_float(renamed.get("break_angle")),
        "break_length": to_float(renamed.get("break_length")),
        "break_y": to_float(renamed.get("break_y")),
        "break_vertical": to_float(renamed.get("break_vertical")),
        "break_vertical_induced": to_float(renamed.get("break_vertical_induced")),
        "break_horizontal": to_float(renamed.get("break_horizontal")),
        "zone": to_int(renamed.get("zone")),
        "release_extension": to_float(renamed.get("release_extension")),
        # --- Batted ball ---
        "launch_speed": to_float(renamed.get("launch_speed")),
        "launch_angle": to_float(renamed.get("launch_angle")),
        "hit_distance_sc": to_float(renamed.get("hit_distance_sc")),
        "hc_x": to_float(renamed.get("hc_x")),
        "hc_y": to_float(renamed.get("hc_y")),
        "spray_angle": to_float(renamed.get("spray_angle")),
        "bb_type": to_str(renamed.get("bb_type")),
        "hit_location": to_float(renamed.get("hit_location")),
        "des": to_str(renamed.get("des")),
        # --- Baserunners ---
        "on_1b": to_int(renamed.get("on_1b")) or None,
        "on_2b": to_int(renamed.get("on_2b")) or None,
        "on_3b": to_int(renamed.get("on_3b")) or None,
        "post_on_1b": to_int(renamed.get("post_on_1b")) or None,
        "post_on_2b": to_int(renamed.get("post_on_2b")) or None,
        "post_on_3b": to_int(renamed.get("post_on_3b")) or None,
        # --- Fielders ---
        "fielder_2": to_int(renamed.get("fielder_2")),
        "fielder_3": to_int(renamed.get("fielder_3")),
        "fielder_4": to_int(renamed.get("fielder_4")),
        "fielder_5": to_int(renamed.get("fielder_5")),
        "fielder_6": to_int(renamed.get("fielder_6")),
        "fielder_7": to_int(renamed.get("fielder_7")),
        "fielder_8": to_int(renamed.get("fielder_8")),
        "fielder_9": to_int(renamed.get("fielder_9")),
        # --- Outcome counts ---
        "runs_on_pitch": to_int(renamed.get("runs_on_pitch")) or 0,
        "outs_on_pitch": to_int(renamed.get("outs_on_pitch")) or 0,
        "rbis_on_pitch": to_int(renamed.get("rbis_on_pitch")) or 0,
        "earned_runs_on_pitch": to_int(renamed.get("earned_runs_on_pitch")) or 0,
        # --- Runner scoring flags ---
        "runner_1b_scored": to_bool(renamed.get("runner_1b_scored")),
        "runner_2b_scored": to_bool(renamed.get("runner_2b_scored")),
        "runner_3b_scored": to_bool(renamed.get("runner_3b_scored")),
        # --- Runner out advancing (thrown out attempting extra base) ---
        "runner_1b_out_advancing": to_bool(renamed.get("runner_1b_out_advancing")),
        "runner_2b_out_advancing": to_bool(renamed.get("runner_2b_out_advancing")),
        "runner_3b_out_advancing": to_bool(renamed.get("runner_3b_out_advancing")),
        # --- Stolen base ---
        "sb_attempt_2b": to_bool(renamed.get("sb_attempt_2b")),
        "sb_attempt_3b": to_bool(renamed.get("sb_attempt_3b")),
        "sb_attempt_home": to_bool(renamed.get("sb_attempt_home")),
        "sb_success_2b": to_bool(renamed.get("sb_success_2b")),
        "sb_success_3b": to_bool(renamed.get("sb_success_3b")),
        "sb_success_home": to_bool(renamed.get("sb_success_home")),
        # --- Substitution flags ---
        "passed_ball_wild_pitch": to_bool(renamed.get("passed_ball_wild_pitch")),
        "pinch_hitter": to_bool(renamed.get("pinch_hitter")),
        "pinch_runner": to_bool(renamed.get("pinch_runner")),
        "pitcher_sub": to_bool(renamed.get("pitcher_sub")),
        "defensive_sub": to_bool(renamed.get("defensive_sub")),
        # --- Fielding detail ---
        "fielded_by": to_float(renamed.get("fielded_by")),
        "fielding_error": to_float(renamed.get("fielding_error")),
        "dropped_ball": to_float(renamed.get("dropped_ball")),
        "of_assist": to_float(renamed.get("of_assist")),
        "field_assist_1": to_float(renamed.get("field_assist_1")),
        "field_assist_2": to_float(renamed.get("field_assist_2")),
        "field_assist_3": to_float(renamed.get("field_assist_3")),
        "field_assist_4": to_float(renamed.get("field_assist_4")),
        "field_assist_5": to_float(renamed.get("field_assist_5")),
        "field_putout_1": to_float(renamed.get("field_putout_1")),
        "field_putout_2": to_float(renamed.get("field_putout_2")),
        "field_putout_3": to_float(renamed.get("field_putout_3")),
        "throwing_error_1": to_float(renamed.get("throwing_error_1")),
        "throwing_error_2": to_float(renamed.get("throwing_error_2")),
        "field_assist_6_plus": to_bool(renamed.get("field_assist_6_plus")),
        # DB trigger sets data_quality_flag on insert; initialised to FALSE here.
        "data_quality_flag": False,
    }
    return row


# ---------------------------------------------------------------------------
# Pre-insert validation
# ---------------------------------------------------------------------------
@dataclass
class ValidationResult:
    hard_errors: list[str] = field(default_factory=list)  # skip the row
    warnings: list[str] = field(default_factory=list)  # insert flagged

    @property
    def is_valid(self) -> bool:
        return len(self.hard_errors) == 0


def _validate_row(row: dict[str, Any]) -> ValidationResult:
    """
    Two-tier validation:
      HARD ERROR — row is skipped entirely (primary key missing, inning out of range, etc.)
      WARNING    — row is inserted but data_quality_flag is forced TRUE

    The DB trigger raw.flag_pitch_quality() also fires on insert for physics
    range checks; this layer catches structural/logical issues beforehand.
    """
    result = ValidationResult()

    # ---- HARD ERRORS -------------------------------------------------------

    # Primary key must be fully populated
    for col in ("game_pk", "at_bat_number", "pitch_number"):
        if row.get(col) is None:
            result.hard_errors.append(f"NULL primary key column: {col}")

    # Required context columns
    for col in ALWAYS_REQUIRED:
        if row.get(col) is None:
            result.hard_errors.append(f"Unexpected NULL in required column: {col}")

    # inning_topbot must be one of the two valid values
    if row.get("inning_topbot") not in ("Top", "Bot"):
        result.hard_errors.append(f"Invalid inning_topbot: {row.get('inning_topbot')!r}")

    # Inning range check
    inning = row.get("inning")
    if inning is not None and not (1 <= inning <= 30):
        result.hard_errors.append(f"Inning out of range: {inning}")

    # Count / outs bounds
    if row.get("balls") is not None and row["balls"] not in range(4):
        result.hard_errors.append(f"balls out of range: {row['balls']}")
    if row.get("strikes") is not None and row["strikes"] not in range(3):
        result.hard_errors.append(f"strikes out of range: {row['strikes']}")
    if row.get("outs") is not None and row["outs"] not in range(3):
        result.hard_errors.append(f"outs out of range: {row['outs']}")

    # Stolen base logic: success requires attempt
    for base in ("2b", "3b", "home"):
        if row.get(f"sb_success_{base}") and not row.get(f"sb_attempt_{base}"):
            result.hard_errors.append(f"sb_success_{base}=TRUE but sb_attempt_{base}=FALSE")

    # SIM-441: mirror the remaining raw.pitches CHECK constraints.
    #
    # This validator was a strict SUBSET of the DB constraints, so a row that
    # violated one of the checks below sailed through here and raised inside
    # _batch_insert instead — which rolls back the ENTIRE game (one connection,
    # all chunks, one commit), reaches no raw.etl_errors row, and aborts the run.
    # Catching them here turns "lose the whole game and stop" into "skip one
    # pitch, record why, keep going".
    for col, allowed in (
        ("p_throws", ("L", "R")),
        ("stand", ("L", "R", "S")),
        ("bat_hand", ("L", "R", "S")),
    ):
        val = row.get(col)
        if val is not None and val not in allowed:
            result.hard_errors.append(f"{col}={val!r} not in {allowed}")

    for col, lo, hi in (
        ("launch_speed", 0.0, 130.0),
        ("launch_angle", -90.0, 90.0),
        ("spin_axis", 0, 360),
        ("zone", 1, 14),
    ):
        val = row.get(col)
        if val is not None and not (lo <= val <= hi):
            result.hard_errors.append(f"{col}={val} outside the DB CHECK range [{lo}, {hi}]")

    if result.hard_errors:
        return result  # don't bother with warnings if we're skipping the row

    # ---- WARNINGS (insert with data_quality_flag=TRUE) ---------------------

    # Ball in play must have batted ball stats
    if row.get("type") in ("D", "E", "X"):
        for col in IN_PLAY_REQUIRED:
            if row.get(col) is None:
                result.warnings.append(f"type={row.get('type')!r} (ball in play) but {col} is NULL")

    # Physics plausibility.  These thresholds MUST stay in lock-step with the
    # raw.flag_pitch_quality() DB trigger: the trigger only ever SETS
    # data_quality_flag TRUE and never clears it, so whichever layer is stricter
    # wins.  A Python band that is narrower than the trigger's is therefore
    # silently authoritative — that is how SIM-087's widened floor was reverted
    # in practice for five years.
    #
    # Current band: release_speed 50–110 mph, launch_speed ≤ 125 mph.
    # NOTE the launch_speed bound must stay strictly BELOW the column CHECK
    # (raw.pitches.launch_speed BETWEEN 0 AND 130): a warning at >130 can never
    # fire, because the CHECK rejects the row outright. MLB's record exit
    # velocity is ~122.4 mph, so (125, 130] is sensor artifact and must keep
    # earning data_quality_flag rather than flowing into max_exit_velo and the
    # hard-hit / barrel rates as clean data.
    # 102 was NOT a physical ceiling — Statcast records four-figure counts of
    # 102–105 mph pitches every season, and flagging them removed the entire
    # high-velocity tail from sim.pitch_pool and the arsenal GMMs.
    # If you change any bound here, ship a matching Alembic migration for
    # raw.flag_pitch_quality() AND update db/schemas/01_postgres_schema.sql.
    # (Migration 0016 aligned the trigger to this band.)
    rs = row.get("release_speed")
    if rs is not None and not (50 <= rs <= 110):
        result.warnings.append(f"release_speed={rs} outside 50–110 mph range")

    ls = row.get("launch_speed")
    if ls is not None and ls > 125:
        result.warnings.append(f"launch_speed={ls} exceeds 125 mph")

    ivb = row.get("break_vertical_induced")
    if ivb is not None and not (-25 <= ivb <= 25):
        result.warnings.append(f"break_vertical_induced={ivb} outside ±25 inch range")

    # If warnings exist, force the flag so the DB trigger agrees with ETL intent
    if result.warnings:
        row["data_quality_flag"] = True

    return result
