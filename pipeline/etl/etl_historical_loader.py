"""
etl_historical_loader.py
========================
MLB Baseball Simulation Platform

Wraps the existing create_season_play_file.py fetch logic (write_rows /
refresh_plays / get_plays) and routes every pitch row directly into
PostgreSQL raw.pitches — no intermediate CSV required, though CSV output
is preserved as an optional side-effect for audit purposes.

Architecture
------------
  MLB Stats API
       │
       ▼
  _fetch_game_pitches()  ← fetches feed/live + coaches; returns (pitch_rows, game_dict)
       │
       ▼
  _ensure_prerequisites()  ← guarantees all FK parents exist before touching raw.pitches
       │   Checks DB for missing IDs in one query per table, fetches only what's absent.
       │   Load order respects FK chain:
       │     1. raw.venues    (no parents)
       │     2. raw.teams     (→ venues)
       │     3. raw.players   (no parents; all pitcher/batter/fielder IDs in this game)
       │     4. raw.managers  (→ teams)
       │     5. raw.games     (→ venues, teams)
       │
       ▼
  _build_row_dict()     ← renames fields, coerces types, converts '' → None
       │
       ▼
  _validate_row()       ← pre-insert validation (unexpected nulls, range checks)
       │
       ├─ WARN only → row inserted with data_quality_flag=TRUE
       └─ HARD ERROR  → row skipped, logged to etl_errors table
       │
       ▼
  _batch_insert()       ← psycopg2 executemany, 500-row batches
       │
       ▼
  raw.pitches           ← DB trigger raw.flag_pitch_quality() fires on insert
       │
       ▼
  _log_freshness()      ← upserts raw.etl_data_freshness after each game

Usage
-----
  # Full historical backfill (2022–2024)
  loader = HistoricalDataLoader(dsn="postgresql://user:pass@localhost/baseball")
  loader.refresh_seasons(start_year=2022, end_year=2024)

  # Current-season incremental update
  loader.load_date_range(start_date=date(2025, 3, 18), end_date=date.today())

  # Single game (for testing)
  loader.load_game(game_pk=745528)
"""

from __future__ import annotations

import logging
import math
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import numpy as np
import psycopg2
import psycopg2.extras
import requests
import json
import pandas as pd
import io

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("etl_historical_loader")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BATCH_SIZE = 500          # rows per executemany call
MAX_API_RETRIES = 10      # infinite-retry inherited from original; capped here
RETRY_BACKOFF_S = 2.0     # seconds between retries

GAME_TYPES = ["R", "F", "D", "L", "W", "C", "P"]

# Columns that must be non-null on every pitch row regardless of pitch type.
ALWAYS_REQUIRED: set[str] = {
    "game_pk", "at_bat_number", "pitch_number", "game_date",
    "venue_id", "pitcher", "p_throws", "batter", "stand", "bat_hand",
    "inning", "inning_topbot", "balls", "strikes", "outs",
    "home_score", "away_score",
}

# Columns that must be present when type == 'X' (ball in play).
IN_PLAY_REQUIRED: set[str] = {"launch_speed", "launch_angle", "bb_type"}


# ---------------------------------------------------------------------------
# Column rename map  (CSV / API variable name → PostgreSQL column name)
# ---------------------------------------------------------------------------

COLUMN_RENAME: dict[str, str] = {
    # pitch identity
    "pitch_code":               "type",
    "pitch_code_description":   "description",
    "pitch_type_description":   "pitch_name",
    # strike zone
    "strike_zone_top":          "sz_top",
    "strike_zone_bottom":       "sz_bot",
    # physics
    "start_speed":              "release_speed",
    "release_x":                "release_pos_x",
    "release_y":                "release_pos_y",
    "release_z":                "release_pos_z",
    "velocity_x":               "vx0",
    "velocity_y":               "vy0",
    "velocity_z":               "vz0",
    "acceleration_x":           "ax",
    "acceleration_y":           "ay",
    "acceleration_z":           "az",
    "p_x":                      "plate_x",
    "p_z":                      "plate_z",
    "spin_rate":                "release_spin_rate",
    "spin_direction":           "spin_axis",
    # batted ball
    "total_distance":           "hit_distance_sc",
    "coord_x":                  "hc_x",
    "coord_y":                  "hc_y",
    "batted_ball_type":         "bb_type",
    "play_description":         "des",
    # baserunners
    "pre_play_runner_on_first":  "on_1b",
    "pre_play_runner_on_second": "on_2b",
    "pre_play_runner_on_third":  "on_3b",
    "post_play_runner_on_first": "post_on_1b",
    "post_play_runner_on_second":"post_on_2b",
    "post_play_runner_on_third": "post_on_3b",
    # fielders (positional → positional number)
    "catcher":                  "fielder_2",
    "first_base":               "fielder_3",
    "second_base":              "fielder_4",
    "third_base":               "fielder_5",
    "shortstop":                "fielder_6",
    "left_field":               "fielder_7",
    "center_field":             "fielder_8",
    "right_field":              "fielder_9",
    # outcome counts
    "runs":                     "runs_on_pitch",
    "rbis":                     "rbis_on_pitch",
    "earned_runs":              "earned_runs_on_pitch",
    # runner scoring flags
    "runner_on_first_score":    "runner_1b_scored",
    "runner_on_second_score":   "runner_2b_scored",
    "runner_on_third_score":    "runner_3b_scored",
    # misc
    "wild_pitch_passed_ball":   "passed_ball_wild_pitch",
    "extension":                "release_extension",
    "top_bot":                  "_inning_topbot_raw",   # transformed separately below
}

# inning_topbot value normalisation  (API → schema)
HALF_INNING_MAP = {
    "top":    "Top",
    "bottom": "Bot",
}

# ---------------------------------------------------------------------------
# Float / int coercion helpers
# ---------------------------------------------------------------------------

def _to_float(v: Any) -> float | None:
    """Convert API value to float; return None for empty/missing."""
    if v is None or v == "" or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> int | None:
    f = _to_float(v)
    return None if f is None else int(f)


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v) if v is not None else False


def _to_str(v: Any) -> str | None:
    if v is None or v == "":
        return None
    s = str(v).strip()
    return s if s else None


# ---------------------------------------------------------------------------
# Row builder — rename + coerce every field
# ---------------------------------------------------------------------------

def _build_row_dict(raw: dict[str, Any], game_pk: int, season: int, game_date: str) -> dict[str, Any]:
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
    renamed["inning_topbot"] = HALF_INNING_MAP.get(
        str(raw_half).lower() if raw_half else "", None
    )

    # Ensure primary key fields are present
    renamed["game_pk"] = game_pk
    renamed["game_date"] = game_date

    row: dict[str, Any] = {
        # --- Natural key ---
        "game_pk":                  _to_int(renamed.get("game_pk")),
        "at_bat_number":            _to_int(renamed.get("at_bat_number")),
        "pitch_number":             _to_int(renamed.get("pitch_number")),

        # --- Game context ---
        "game_date":                renamed.get("game_date"),
        "season":                   _to_int(season),
        "venue_id":                 _to_int(renamed.get("venue_id")),
        "venue":                    _to_str(renamed.get("venue")),
        "home_id":                  _to_int(renamed.get("home_id")),
        "home_team":                _to_str(renamed.get("home_team")),
        "away_id":                  _to_int(renamed.get("away_id")),
        "away_team":                _to_str(renamed.get("away_team")),
        "home_manager_id":          _to_int(renamed.get("home_manager_id")),
        "home_manager_name":        _to_str(renamed.get("home_manager_name")),
        "away_manager_id":          _to_int(renamed.get("away_manager_id")),
        "away_manager_name":        _to_str(renamed.get("away_manager_name")),

        # --- Inning state ---
        "inning":                   _to_int(renamed.get("inning")),
        "inning_topbot":            _to_str(renamed.get("inning_topbot")),

        # --- Pitcher / batter ---
        "pitcher":                  _to_int(renamed.get("pitcher")),
        "p_throws":                 _to_str(renamed.get("p_throws")),
        "batter":                   _to_int(renamed.get("batter")),
        "stand":                    _to_str(renamed.get("stand")),
        "bat_hand":                 _to_str(renamed.get("bat_hand")),

        # --- Score state ---
        "home_score":               _to_int(renamed.get("home_score")) or 0,
        "away_score":               _to_int(renamed.get("away_score")) or 0,
        "bat_score":                _to_int(renamed.get("bat_score")) or 0,
        "fld_score":                _to_int(renamed.get("fld_score")) or 0,

        # --- Count ---
        "balls":                    _to_int(renamed.get("balls")) or 0,
        "strikes":                  _to_int(renamed.get("strikes")) or 0,
        "outs":                     _to_int(renamed.get("outs")) or 0,

        # --- Pitch result ---
        "type":                     _to_str(renamed.get("type")),
        "description":              _to_str(renamed.get("description")),
        "pitch_type":               _to_str(renamed.get("pitch_type")),
        "pitch_name":               _to_str(renamed.get("pitch_name")),
        "events":                   _to_str(renamed.get("events")) or _to_str(renamed.get("event")),

        # --- Strike zone ---
        "sz_top":                   _to_float(renamed.get("sz_top")),
        "sz_bot":                   _to_float(renamed.get("sz_bot")),

        # --- Physics ---
        "release_speed":            _to_float(renamed.get("release_speed")),
        "end_speed":                _to_float(renamed.get("end_speed")),
        "release_pos_x":            _to_float(renamed.get("release_pos_x")),
        "release_pos_y":            _to_float(renamed.get("release_pos_y")),
        "release_pos_z":            _to_float(renamed.get("release_pos_z")),
        "vx0":                      _to_float(renamed.get("vx0")),
        "vy0":                      _to_float(renamed.get("vy0")),
        "vz0":                      _to_float(renamed.get("vz0")),
        "ax":                       _to_float(renamed.get("ax")),
        "ay":                       _to_float(renamed.get("ay")),
        "az":                       _to_float(renamed.get("az")),

        # --- Movement ---
        "pfx_x":                    _to_float(renamed.get("pfx_x")),
        "pfx_z":                    _to_float(renamed.get("pfx_z")),
        "plate_x":                  _to_float(renamed.get("plate_x")),
        "plate_z":                  _to_float(renamed.get("plate_z")),
        "x":                        _to_float(renamed.get("x")),
        "y":                        _to_float(renamed.get("y")),
        "release_spin_rate":        _to_int(renamed.get("release_spin_rate")),
        "spin_axis":                _to_int(renamed.get("spin_axis")),
        "break_angle":              _to_float(renamed.get("break_angle")),
        "break_length":             _to_float(renamed.get("break_length")),
        "break_y":                  _to_float(renamed.get("break_y")),
        "break_vertical":           _to_float(renamed.get("break_vertical")),
        "break_vertical_induced":   _to_float(renamed.get("break_vertical_induced")),
        "break_horizontal":         _to_float(renamed.get("break_horizontal")),
        "zone":                     _to_int(renamed.get("zone")),
        "release_extension":        _to_float(renamed.get("release_extension")),

        # --- Batted ball ---
        "launch_speed":             _to_float(renamed.get("launch_speed")),
        "launch_angle":             _to_float(renamed.get("launch_angle")),
        "hit_distance_sc":          _to_float(renamed.get("hit_distance_sc")),
        "hc_x":                     _to_float(renamed.get("hc_x")),
        "hc_y":                     _to_float(renamed.get("hc_y")),
        "spray_angle":              _to_float(renamed.get("spray_angle")),
        "bb_type":                  _to_str(renamed.get("bb_type")),
        "hit_location":             _to_float(renamed.get("hit_location")),
        "des":                      _to_str(renamed.get("des")),

        # --- Baserunners ---
        "on_1b":                    _to_int(renamed.get("on_1b")) or None,
        "on_2b":                    _to_int(renamed.get("on_2b")) or None,
        "on_3b":                    _to_int(renamed.get("on_3b")) or None,
        "post_on_1b":               _to_int(renamed.get("post_on_1b")) or None,
        "post_on_2b":               _to_int(renamed.get("post_on_2b")) or None,
        "post_on_3b":               _to_int(renamed.get("post_on_3b")) or None,

        # --- Fielders ---
        "fielder_2":                _to_int(renamed.get("fielder_2")),
        "fielder_3":                _to_int(renamed.get("fielder_3")),
        "fielder_4":                _to_int(renamed.get("fielder_4")),
        "fielder_5":                _to_int(renamed.get("fielder_5")),
        "fielder_6":                _to_int(renamed.get("fielder_6")),
        "fielder_7":                _to_int(renamed.get("fielder_7")),
        "fielder_8":                _to_int(renamed.get("fielder_8")),
        "fielder_9":                _to_int(renamed.get("fielder_9")),

        # --- Outcome counts ---
        "runs_on_pitch":            _to_int(renamed.get("runs_on_pitch")) or 0,
        "outs_on_pitch":            _to_int(renamed.get("outs_on_pitch")) or 0,
        "rbis_on_pitch":            _to_int(renamed.get("rbis_on_pitch")) or 0,
        "earned_runs_on_pitch":     _to_int(renamed.get("earned_runs_on_pitch")) or 0,

        # --- Runner scoring flags ---
        "runner_1b_scored":         _to_bool(renamed.get("runner_1b_scored")),
        "runner_2b_scored":         _to_bool(renamed.get("runner_2b_scored")),
        "runner_3b_scored":         _to_bool(renamed.get("runner_3b_scored")),

        # --- Runner out advancing (thrown out attempting extra base) ---
        "runner_1b_out_advancing": _to_bool(renamed.get("runner_1b_out_advancing")),
        "runner_2b_out_advancing": _to_bool(renamed.get("runner_2b_out_advancing")),
        "runner_3b_out_advancing": _to_bool(renamed.get("runner_3b_out_advancing")),

        # --- Stolen base ---
        "sb_attempt_2b":            _to_bool(renamed.get("sb_attempt_2b")),
        "sb_attempt_3b":            _to_bool(renamed.get("sb_attempt_3b")),
        "sb_attempt_home":          _to_bool(renamed.get("sb_attempt_home")),
        "sb_success_2b":            _to_bool(renamed.get("sb_success_2b")),
        "sb_success_3b":            _to_bool(renamed.get("sb_success_3b")),
        "sb_success_home":          _to_bool(renamed.get("sb_success_home")),

        # --- Substitution flags ---
        "passed_ball_wild_pitch":   _to_bool(renamed.get("passed_ball_wild_pitch")),
        "pinch_hitter":             _to_bool(renamed.get("pinch_hitter")),
        "pinch_runner":             _to_bool(renamed.get("pinch_runner")),
        "pitcher_sub":              _to_bool(renamed.get("pitcher_sub")),
        "defensive_sub":            _to_bool(renamed.get("defensive_sub")),

        # --- Fielding detail ---
        "fielded_by":               _to_float(renamed.get("fielded_by")),
        "fielding_error":           _to_float(renamed.get("fielding_error")),
        "dropped_ball":             _to_float(renamed.get("dropped_ball")),
        "of_assist":                _to_float(renamed.get("of_assist")),
        "field_assist_1":           _to_float(renamed.get("field_assist_1")),
        "field_assist_2":           _to_float(renamed.get("field_assist_2")),
        "field_assist_3":           _to_float(renamed.get("field_assist_3")),
        "field_assist_4":           _to_float(renamed.get("field_assist_4")),
        "field_assist_5":           _to_float(renamed.get("field_assist_5")),
        "field_putout_1":           _to_float(renamed.get("field_putout_1")),
        "field_putout_2":           _to_float(renamed.get("field_putout_2")),
        "field_putout_3":           _to_float(renamed.get("field_putout_3")),
        "throwing_error_1":         _to_float(renamed.get("throwing_error_1")),
        "throwing_error_2":         _to_float(renamed.get("throwing_error_2")),

        # DB trigger sets data_quality_flag on insert; initialised to FALSE here.
        "data_quality_flag":        False,
    }

    return row


# ---------------------------------------------------------------------------
# Pre-insert validation
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    hard_errors: list[str] = field(default_factory=list)   # skip the row
    warnings: list[str]    = field(default_factory=list)   # insert flagged

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
        result.hard_errors.append(
            f"Invalid inning_topbot: {row.get('inning_topbot')!r}"
        )

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
            result.hard_errors.append(
                f"sb_success_{base}=TRUE but sb_attempt_{base}=FALSE"
            )

    if result.hard_errors:
        return result   # don't bother with warnings if we're skipping the row

    # ---- WARNINGS (insert with data_quality_flag=TRUE) ---------------------

    # Ball in play must have batted ball stats
    if row.get("type") == "X":
        for col in IN_PLAY_REQUIRED:
            if row.get(col) is None:
                result.warnings.append(
                    f"type='X' (ball in play) but {col} is NULL"
                )

    # Physics plausibility (mirrors DB trigger thresholds — caught pre-insert
    # so the ETL log shows them explicitly rather than relying on trigger only).
    # SIM-087: Validator floor lowered from 70 → 60 mph so legitimate slow
    # curveballs (60–65 mph) are not flagged as bad data.  The DB trigger uses
    # 50 mph as its impossible-floor — if you change the validator floor here,
    # also update raw.flag_pitch_quality() in 01_postgres_schema.sql.
    rs = row.get("release_speed")
    if rs is not None and not (60 <= rs <= 102):
        result.warnings.append(f"release_speed={rs} outside 60–102 mph range")

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


# ---------------------------------------------------------------------------
# API helpers  (preserve original retry-forever behaviour, now with cap + backoff)
# ---------------------------------------------------------------------------

def _connect(url: str, params: dict | None = None) -> dict:
    for attempt in range(1, MAX_API_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            if attempt == MAX_API_RETRIES:
                raise
            log.warning("API call failed (attempt %d/%d): %s", attempt, MAX_API_RETRIES, exc)
            time.sleep(RETRY_BACKOFF_S * attempt)
    raise RuntimeError("Unreachable")


# ---------------------------------------------------------------------------
# Core per-game fetch — adapted from your write_rows()
# Returns a list of raw dicts (one per pitch) instead of writing to a file.
# ---------------------------------------------------------------------------

def _fetch_game_pitches(
    game_pk: int,
    batter_hand_cache: dict[int, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Replaces write_rows().  All the same parsing logic; returns a tuple of:
      - pitch_rows: one dict per pitch, ready for _build_row_dict()
      - game_dict:  the raw feed/live response, with manager info attached
                    under game_dict['_managers'] for use by _ensure_prerequisites()

    batter_hand_cache is shared across games to avoid redundant API calls.
    """
    game_dict = _connect(
        f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live?hydrate=alignment"
    )

    game_date   = game_dict["gameData"]["datetime"]["officialDate"]
    home_team_id = game_dict["gameData"]["teams"]["home"]["id"]
    home_team    = game_dict["gameData"]["teams"]["home"]["abbreviation"]
    away_team_id = game_dict["gameData"]["teams"]["away"]["id"]
    away_team    = game_dict["gameData"]["teams"]["away"]["abbreviation"]
    venue_id     = game_dict["gameData"]["venue"]["id"]
    venue_name   = game_dict["gameData"]["venue"]["name"]

    home_manager_id, home_manager_name = "", ""
    away_manager_id, away_manager_name = "", ""

    home_coaches = _connect(
        f"https://statsapi.mlb.com/api/v1/teams/{home_team_id}/coaches?date={game_date}"
    )["roster"]
    temp_manager_id = home_coaches[0]["person"]["id"]
    temp_manager_name = home_coaches[0]["person"]["fullName"]
    for coach in home_coaches:
        if coach["jobId"] == "NTRM":
            home_manager_id   = coach["person"]["id"]
            home_manager_name = coach["person"]["fullName"]
            break
        if coach["jobId"] == "MNGR":
            home_manager_id   = coach["person"]["id"]
            home_manager_name = coach["person"]["fullName"]
        if coach["jobId"] == "COAB" and home_manager_name == '':
            home_manager_id = coach["person"]["id"]
            home_manager_name = coach["person"]["fullName"]
    if home_manager_name == '':
        home_manager_id = temp_manager_id
        home_manager_name = temp_manager_name

    away_coaches = _connect(
        f"https://statsapi.mlb.com/api/v1/teams/{away_team_id}/coaches?date={game_date}"
    )["roster"]
    temp_manager_id = away_coaches[0]["person"]["id"]
    temp_manager_name = away_coaches[0]["person"]["fullName"]
    for coach in away_coaches:
        if coach["jobId"] == "NTRM":
            away_manager_id   = coach["person"]["id"]
            away_manager_name = coach["person"]["fullName"]
            break
        if coach["jobId"] == "MNGR":
            away_manager_id   = coach["person"]["id"]
            away_manager_name = coach["person"]["fullName"]
        if coach["jobId"] == "COAB" and away_manager_name == '':
            away_manager_id = coach["person"]["id"]
            away_manager_name = coach["person"]["fullName"]
    if away_manager_name == '':
        away_manager_id = temp_manager_id
        away_manager_name = temp_manager_name

    home_score_before, away_score_before = 0, 0
    outs      = 0
    prev_half = "bottom"
    pitch_rows: list[dict[str, Any]] = []

    for play in game_dict["liveData"]["plays"]["allPlays"]:
        at_bat_number = play["atBatIndex"] + 1
        inning        = play["about"]["inning"]
        top_bot       = play["about"]["halfInning"]
        if top_bot != prev_half:
            outs = 0
        balls, strikes = 0, 0
        play_description = play["result"].get("description", "").replace(",", "")
        event = ""
        pinch_hitter = pinch_runner = pitcher_sub = defensive_sub = False

        for i in range(len(play["playEvents"])):
            # --- Substitution detection (unchanged from your original) ---
            a = 0
            while True:
                if a < len(play["playEvents"]) and not play["playEvents"][a]["isPitch"]:
                    evt_type = play["playEvents"][a]["details"].get("eventType", "")
                    if evt_type == "defensive_substitution":
                        defensive_sub = True
                    if evt_type == "pitching_substitution":
                        pitcher_sub = True
                    if evt_type == "offensive_substitution":
                        pos_code = play["playEvents"][a]["position"]["code"]
                        if pos_code == "11":
                            pinch_hitter = True
                        if pos_code == "12":
                            pinch_runner = True
                else:
                    break
                a += 1

            if len(play["pitchIndex"]) == 0:
                continue
            if not play["playEvents"][i]["isPitch"]:
                continue

            # --- Pre-pitch runner state ---
            offense = play["playEvents"][i]["offense"]
            pre_runner_1b = offense.get("first",  {}).get("id", "")
            pre_runner_2b = offense.get("second", {}).get("id", "")
            pre_runner_3b = offense.get("third",  {}).get("id", "")

            # --- Mid-pitch events (SB, WP/PB) ---
            sb_attempt_2b = sb_attempt_3b = sb_attempt_home = False
            sb_success_2b = sb_success_3b = sb_success_home = False
            wild_pitch_passed_ball = False
            a = 1
            while True:
                if i + a < len(play["playEvents"]) and not play["playEvents"][i + a]["isPitch"]:
                    et = play["playEvents"][i + a]["details"].get("eventType", "")
                    if et in ("stolen_base_2b", "caught_stealing_2b"):
                        sb_attempt_2b = True
                        if et == "stolen_base_2b":
                            sb_success_2b = True
                    if et in ("stolen_base_3b", "caught_stealing_3b"):
                        sb_attempt_3b = True
                        if et == "stolen_base_3b":
                            sb_success_3b = True
                    if et in ("stolen_base_home", "caught_stealing_home"):
                        sb_attempt_home = True
                        if et == "stolen_base_home":
                            sb_success_home = True
                    if et in ("wild_pitch", "passed_ball"):
                        wild_pitch_passed_ball = True
                else:
                    max_play_index = i + a - 1
                    break
                a += 1

            play_event = play["playEvents"][i]

            bat_score   = away_score_before if top_bot == "top" else home_score_before
            field_score = home_score_before if top_bot == "top" else away_score_before

            pitch_number  = play_event["pitchNumber"]
            pitcher       = play_event["defense"]["pitcher"]["id"]
            pitcher_hand  = play_event["defense"]["pitcher"]["pitchHand"]["code"]
            batter        = play_event["offense"]["batter"]["id"]
            bat_side      = play_event["offense"]["batter"]["batSide"]["code"]

            if batter in batter_hand_cache:
                bat_hand = batter_hand_cache[batter]
            else:
                bat_hand = _connect(
                    f"https://statsapi.mlb.com{play_event['offense']['batter']['link']}"
                )["people"][0]["batSide"]["code"]
                batter_hand_cache[batter] = bat_hand

            defense      = play_event["defense"]
            catcher      = defense["catcher"]["id"]
            first_base   = defense["first"]["id"]
            second_base  = defense["second"]["id"]
            third_base   = defense["third"]["id"]
            shortstop    = defense["shortstop"]["id"]
            left_field   = defense["left"]["id"]
            center_field = defense["center"]["id"]
            right_field  = defense["right"]["id"]

            if play_event["index"] == play["pitchIndex"][-1]:
                event = play["result"]["eventType"]

            pd_data = play_event["pitchData"]
            pitch_code             = play_event["details"]["code"]
            pitch_code_description = play_event["details"]["description"].replace(",", "")
            pitch_type             = play_event["details"].get("type", {"code": ""}).get("code", "")
            pitch_type_description = play_event["details"].get("type", {"description": ""})["description"]
            strike_zone_top        = pd_data["strikeZoneTop"]
            strike_zone_bottom     = pd_data["strikeZoneBottom"]
            start_speed    = pd_data.get("startSpeed", "")
            end_speed      = pd_data.get("endSpeed", "")
            coords = pd_data["coordinates"]
            release_x = coords.get("x0", "");  release_y = coords.get("y0", "")
            release_z = coords.get("z0", "")
            velocity_x = coords.get("vX0", ""); velocity_y = coords.get("vY0", "")
            velocity_z = coords.get("vZ0", "")
            acceleration_x = coords.get("aX", ""); acceleration_y = coords.get("aY", "")
            acceleration_z = coords.get("aZ", "")
            pfx_x = coords.get("pfxX", "");    pfx_z = coords.get("pfxZ", "")
            p_x   = coords.get("pX", "");       p_z   = coords.get("pZ", "")
            x = coords.get("x", "");             y = coords.get("y", "")
            breaks = pd_data["breaks"]
            spin_rate    = breaks.get("spinRate", "")
            spin_direction = breaks.get("spinDirection", "")
            break_angle  = breaks.get("breakAngle", "")
            break_length = breaks.get("breakLength", "")
            break_y      = breaks.get("breakY", "")
            break_vertical          = breaks.get("breakVertical", "")
            break_vertical_induced  = breaks.get("breakVerticalInduced", "")
            break_horizontal        = breaks.get("breakHorizontal", "")
            zone      = pd_data.get("zone", "")
            extension = pd_data.get("extension", "")

            launch_speed = launch_angle = total_distance = ""
            coord_x = coord_y = spray_angle = batted_ball_type = hit_location = ""
            if "hitData" in play_event:
                hd = play_event["hitData"]
                launch_speed   = hd.get("launchSpeed", "")
                launch_angle   = hd.get("launchAngle", "")
                total_distance = hd.get("totalDistance", "")
                coord_x = hd["coordinates"].get("coordX", "")
                coord_y = hd["coordinates"].get("coordY", "")
                if coord_x != "" and coord_y != "":
                    if coord_y == 198.27:
                        spray_angle = 90 if coord_x > 125.42 else -90
                    else:
                        spray_angle = np.arctan(
                            (coord_x - 125.42) / (198.27 - coord_y)
                        ) * (180 / np.pi)
                batted_ball_type = hd["trajectory"]
                hit_location     = hd.get("location", "")

            outs_on_pitch = play_event["count"]["outs"] - outs
            runner_on_first_score = runner_on_second_score = runner_on_third_score = False
            home_score_after = home_score_before
            away_score_after = away_score_before
            runs = earned_runs = rbis = 0
            fielded_by = of_assist = fielding_error = dropped_ball = ""
            assist_dict: dict[str, Any] = {}
            putout_dict: dict[str, Any] = {}
            throwing_error_dict: dict[str, Any] = {}
            assist_tracker = putout_tracker = throwing_error_tracker = 1
            post_runner_1b = pre_runner_1b
            post_runner_2b = pre_runner_2b
            post_runner_3b = pre_runner_3b
            runner_1b_out_advancing = runner_2b_out_advancing = runner_3b_out_advancing = False

            for runner in play["runners"]:
                if i <= runner["details"]["playIndex"] <= max_play_index:
                    for credit in runner.get("credits", []):
                        c = credit["credit"]
                        pid = credit["player"]["id"]
                        if fielded_by == "":
                            fielded_by = pid
                        if c == "f_putout":
                            putout_dict[f"field_putout_{putout_tracker}"] = pid
                            putout_tracker += 1
                        elif c == "f_assist":
                            assist_dict[f"field_assist_{assist_tracker}"] = pid
                            assist_tracker += 1
                        elif c == "f_throwing_error":
                            throwing_error_dict[f"throwing_error_{throwing_error_tracker}"] = pid
                            throwing_error_tracker += 1
                        elif c == "f_assist_of":
                            of_assist = pid
                        elif c == "f_fielded_ball":
                            fielded_by = pid
                        elif c == "f_fielding_error":
                            fielding_error = pid
                        elif c == "f_error_dropped_ball":
                            dropped_ball = pid
                    if runner["movement"]["end"] == "score":
                        rid  = runner["details"]["runner"]["id"]
                        runs += 1
                        if runner["details"].get("earned", False):
                            earned_runs += 1
                        if runner.get("rbi", False):
                            rbis += 1
                        if top_bot == "top":
                            away_score_after += 1
                        else:
                            home_score_after += 1
                        if rid == pre_runner_1b:
                            runner_on_first_score = True
                            if rid == post_runner_1b:
                                post_runner_1b = ""
                        if rid == pre_runner_2b:
                            runner_on_second_score = True
                            if rid == post_runner_2b:
                                post_runner_2b = ""
                        if rid == pre_runner_3b:
                            runner_on_third_score = True
                            if rid == post_runner_3b:
                                post_runner_3b = ""
                    if runner["movement"]["end"] == "3B":
                        post_runner_3b = runner["details"]["runner"]["id"]
                        if post_runner_3b == post_runner_2b:
                            post_runner_2b = ""
                        if post_runner_3b == post_runner_1b:
                            post_runner_1b = ""
                    if runner["movement"]["end"] == "2B":
                        post_runner_2b = runner["details"]["runner"]["id"]
                        if post_runner_2b == post_runner_1b:
                            post_runner_1b = ""
                    if runner["movement"]["end"] == "1B":
                        post_runner_1b = runner["details"]["runner"]["id"]
                    # Detect runner thrown out advancing on a batted ball
                    # (not stolen base, not the batter-runner).  The API sets
                    # isOut=True and movement.end="" for these plays.
                    if runner["details"].get("isOut", False):
                        rid = runner["details"]["runner"]["id"]
                        is_sb_play = (sb_attempt_2b or sb_attempt_3b or sb_attempt_home)
                        is_batter = (rid == batter)
                        if not is_sb_play and not is_batter:
                            if rid == pre_runner_1b:
                                runner_1b_out_advancing = True
                            elif rid == pre_runner_2b:
                                runner_2b_out_advancing = True
                            elif rid == pre_runner_3b:
                                runner_3b_out_advancing = True
            # Assemble raw dict using YOUR variable names — COLUMN_RENAME handles
            # the mapping to schema names in _build_row_dict().
            raw_row = {
                "game_pk":              game_pk,
                "game_date":            game_date,
                "venue_id":             venue_id,
                "venue":                venue_name,
                "home_id":              home_team_id,
                "home_team":            home_team,
                "away_id":              away_team_id,
                "away_team":            away_team,
                "home_manager_id":      home_manager_id,
                "home_manager_name":    home_manager_name,
                "away_manager_id":      away_manager_id,
                "away_manager_name":    away_manager_name,
                "inning":               inning,
                "top_bot":              top_bot,            # → inning_topbot via rename
                "at_bat_number":        at_bat_number,
                "pitch_number":         pitch_number,
                "pitcher":              pitcher,
                "p_throws":             pitcher_hand,
                "batter":               batter,
                "stand":                bat_side,
                "bat_hand":             bat_hand,
                "home_score":           home_score_before,
                "away_score":           away_score_before,
                "bat_score":            bat_score,
                "fld_score":            field_score,
                "balls":                balls,
                "strikes":              strikes,
                "outs":                 outs,
                "pitch_code":           pitch_code,         # → type
                "pitch_code_description": pitch_code_description,  # → description
                "pitch_type":           pitch_type,
                "pitch_type_description": pitch_type_description,  # → pitch_name
                "event":                event,
                "strike_zone_top":      strike_zone_top,    # → sz_top
                "strike_zone_bottom":   strike_zone_bottom, # → sz_bot
                "start_speed":          start_speed,        # → release_speed
                "end_speed":            end_speed,
                "release_x":            release_x,          # → release_pos_x
                "release_y":            release_y,
                "release_z":            release_z,
                "velocity_x":           velocity_x,         # → vx0
                "velocity_y":           velocity_y,
                "velocity_z":           velocity_z,
                "acceleration_x":       acceleration_x,     # → ax
                "acceleration_y":       acceleration_y,
                "acceleration_z":       acceleration_z,
                "pfx_x":                pfx_x,
                "pfx_z":                pfx_z,
                "p_x":                  p_x,                # → plate_x
                "p_z":                  p_z,
                "x":                    x,
                "y":                    y,
                "spin_rate":            spin_rate,          # → release_spin_rate
                "spin_direction":       spin_direction,     # → spin_axis
                "break_angle":          break_angle,
                "break_length":         break_length,
                "break_y":              break_y,
                "break_vertical":       break_vertical,
                "break_vertical_induced": break_vertical_induced,
                "break_horizontal":     break_horizontal,
                "zone":                 zone,
                "extension":            extension,          # → release_extension
                "launch_speed":         launch_speed,
                "launch_angle":         launch_angle,
                "total_distance":       total_distance,     # → hit_distance_sc
                "coord_x":              coord_x,            # → hc_x
                "coord_y":              coord_y,            # → hc_y
                "spray_angle":          spray_angle,
                "batted_ball_type":     batted_ball_type,   # → bb_type
                "hit_location":         hit_location,
                "play_description":     play_description,   # → des
                "pre_play_runner_on_first":  pre_runner_1b,     # → on_1b
                "pre_play_runner_on_second": pre_runner_2b,
                "pre_play_runner_on_third":  pre_runner_3b,
                "post_play_runner_on_first":  post_runner_1b,   # → post_on_1b
                "post_play_runner_on_second": post_runner_2b,
                "post_play_runner_on_third":  post_runner_3b,
                "catcher":              catcher,            # → fielder_2
                "first_base":           first_base,         # → fielder_3
                "second_base":          second_base,        # → fielder_4
                "third_base":           third_base,         # → fielder_5
                "shortstop":            shortstop,          # → fielder_6
                "left_field":           left_field,         # → fielder_7
                "center_field":         center_field,       # → fielder_8
                "right_field":          right_field,        # → fielder_9
                "runs":                 runs,               # → runs_on_pitch
                "outs_on_pitch":        outs_on_pitch,
                "rbis":                 rbis,               # → rbis_on_pitch
                "earned_runs":          earned_runs,        # → earned_runs_on_pitch
                "runner_on_first_score":  runner_on_first_score,   # → runner_1b_scored
                "runner_on_second_score": runner_on_second_score,
                "runner_on_third_score":  runner_on_third_score,
                "runner_1b_out_advancing": runner_1b_out_advancing,
                "runner_2b_out_advancing": runner_2b_out_advancing,
                "runner_3b_out_advancing": runner_3b_out_advancing,
                "sb_attempt_2b":        sb_attempt_2b,
                "sb_attempt_3b":        sb_attempt_3b,
                "sb_attempt_home":      sb_attempt_home,
                "sb_success_2b":        sb_success_2b,
                "sb_success_3b":        sb_success_3b,
                "sb_success_home":      sb_success_home,
                "wild_pitch_passed_ball": wild_pitch_passed_ball,  # → passed_ball_wild_pitch
                "pinch_hitter":         pinch_hitter,
                "pinch_runner":         pinch_runner,
                "pitcher_sub":          pitcher_sub,
                "defensive_sub":        defensive_sub,
                "fielded_by":           fielded_by,
                "fielding_error":       fielding_error,
                "dropped_ball":         dropped_ball,
                "of_assist":            of_assist,
                **assist_dict,
                **putout_dict,
                **throwing_error_dict,
            }

            pitch_rows.append(raw_row)

            # Reset per-pitch substitution flags (matches original behaviour)
            pinch_hitter = pinch_runner = pitcher_sub = defensive_sub = False
            balls    = play_event["count"]["balls"]
            strikes  = play_event["count"]["strikes"]
            outs     = play_event["count"]["outs"]
            home_score_before = home_score_after
            away_score_before = away_score_after
            prev_half = top_bot

    # Attach manager info so _ensure_prerequisites() can use it without
    # re-fetching.  Stored under a private key to avoid colliding with any
    # real MLB API fields.
    game_dict["_managers"] = {
        "home_manager_id": home_manager_id,
        "home_manager_name": home_manager_name,
        "away_manager_id": away_manager_id,
        "away_manager_name": away_manager_name,
    }
    return pitch_rows, game_dict


# ---------------------------------------------------------------------------
# Module-level helpers used by _ensure_prerequisites
# ---------------------------------------------------------------------------

def _parse_height(height_str: str | None) -> int | None:
    """
    Converts MLB API height string (e.g. "6' 2\"") to total inches.
    Returns None if the string is absent or unparseable.
    """
    if not height_str:
        return None
    try:
        # Format: "6' 2\"" or "6-2"
        height_str = height_str.replace('"', "").replace("'", "-").replace(" ", "")
        parts = height_str.split("-")
        return int(parts[0]) * 12 + int(parts[1])
    except Exception:
        return None


def _map_game_status(gd: dict) -> str:
    """
    Maps the MLB API abstractGameState to the raw.games status enum.
    """
    abstract = gd.get("status", {}).get("abstractGameState", "Preview")
    coded    = gd.get("status", {}).get("codedGameState", "")
    status_map = {
        "Preview": "Preview",
        "Live":    "Live",
        "Final":   "Final",
    }
    base = status_map.get(abstract, "Preview")
    # Handle edge cases that share abstractGameState="Final"
    if coded in ("D",):
        return "Postponed"
    if coded in ("T",):
        return "Suspended"
    if coded in ("C",):
        return "Cancelled"
    return base


# ---------------------------------------------------------------------------
# Main ETL class
# ---------------------------------------------------------------------------

class HistoricalDataLoader:
    """
    Fetches pitch-by-pitch data from the MLB Stats API and loads it into
    raw.pitches in PostgreSQL.

    Parameters
    ----------
    dsn : str
        psycopg2 connection string, e.g.
        "postgresql://user:password@localhost:5432/baseball"
    write_csv : bool
        If True, also write per-season CSVs to csv_output_dir (for audit).
    csv_output_dir : str
        Directory for optional CSV output. Defaults to current directory.
    """

    INSERT_SQL = """
        INSERT INTO raw.pitches (
            game_pk, at_bat_number, pitch_number, game_date, season, venue_id, venue,
            home_id, home_team, away_id, away_team,
            home_manager_id, home_manager_name, away_manager_id, away_manager_name,
            inning, inning_topbot, pitcher, p_throws, batter, stand, bat_hand,
            home_score, away_score, bat_score, fld_score, balls, strikes, outs,
            type, description, pitch_type, pitch_name, events,
            sz_top, sz_bot, release_speed, end_speed,
            release_pos_x, release_pos_y, release_pos_z,
            vx0, vy0, vz0, ax, ay, az,
            pfx_x, pfx_z, plate_x, plate_z, x, y,
            release_spin_rate, spin_axis,
            break_angle, break_length, break_y,
            break_vertical, break_vertical_induced, break_horizontal,
            zone, release_extension,
            launch_speed, launch_angle, hit_distance_sc, hc_x, hc_y,
            spray_angle, bb_type, hit_location, des,
            on_1b, on_2b, on_3b, post_on_1b, post_on_2b, post_on_3b,
            fielder_2, fielder_3, fielder_4, fielder_5, fielder_6,
            fielder_7, fielder_8, fielder_9,
            runs_on_pitch, outs_on_pitch, rbis_on_pitch, earned_runs_on_pitch,
            runner_1b_scored, runner_2b_scored, runner_3b_scored,
            runner_1b_out_advancing, runner_2b_out_advancing, runner_3b_out_advancing,
            sb_attempt_2b, sb_attempt_3b, sb_attempt_home,
            sb_success_2b, sb_success_3b, sb_success_home,
            passed_ball_wild_pitch, pinch_hitter, pinch_runner,
            pitcher_sub, defensive_sub,
            fielded_by, fielding_error, dropped_ball, of_assist,
            field_assist_1, field_assist_2, field_assist_3, field_assist_4, field_assist_5,
            field_putout_1, field_putout_2, field_putout_3,
            throwing_error_1, throwing_error_2,
            data_quality_flag
        ) VALUES (
            %(game_pk)s, %(at_bat_number)s, %(pitch_number)s, %(game_date)s, %(season)s,
            %(venue_id)s, %(venue)s,
            %(home_id)s, %(home_team)s, %(away_id)s, %(away_team)s,
            %(home_manager_id)s, %(home_manager_name)s,
            %(away_manager_id)s, %(away_manager_name)s,
            %(inning)s, %(inning_topbot)s, %(pitcher)s, %(p_throws)s,
            %(batter)s, %(stand)s, %(bat_hand)s,
            %(home_score)s, %(away_score)s, %(bat_score)s, %(fld_score)s,
            %(balls)s, %(strikes)s, %(outs)s,
            %(type)s, %(description)s, %(pitch_type)s, %(pitch_name)s, %(events)s,
            %(sz_top)s, %(sz_bot)s, %(release_speed)s, %(end_speed)s,
            %(release_pos_x)s, %(release_pos_y)s, %(release_pos_z)s,
            %(vx0)s, %(vy0)s, %(vz0)s, %(ax)s, %(ay)s, %(az)s,
            %(pfx_x)s, %(pfx_z)s, %(plate_x)s, %(plate_z)s, %(x)s, %(y)s,
            %(release_spin_rate)s, %(spin_axis)s,
            %(break_angle)s, %(break_length)s, %(break_y)s,
            %(break_vertical)s, %(break_vertical_induced)s, %(break_horizontal)s,
            %(zone)s, %(release_extension)s,
            %(launch_speed)s, %(launch_angle)s, %(hit_distance_sc)s,
            %(hc_x)s, %(hc_y)s, %(spray_angle)s, %(bb_type)s,
            %(hit_location)s, %(des)s,
            %(on_1b)s, %(on_2b)s, %(on_3b)s,
            %(post_on_1b)s, %(post_on_2b)s, %(post_on_3b)s,
            %(fielder_2)s, %(fielder_3)s, %(fielder_4)s, %(fielder_5)s,
            %(fielder_6)s, %(fielder_7)s, %(fielder_8)s, %(fielder_9)s,
            %(runs_on_pitch)s, %(outs_on_pitch)s, %(rbis_on_pitch)s,
            %(earned_runs_on_pitch)s,
            %(runner_1b_scored)s, %(runner_2b_scored)s, %(runner_3b_scored)s,
            %(runner_1b_out_advancing)s, %(runner_2b_out_advancing)s, %(runner_3b_out_advancing)s,
            %(sb_attempt_2b)s, %(sb_attempt_3b)s, %(sb_attempt_home)s,
            %(sb_success_2b)s, %(sb_success_3b)s, %(sb_success_home)s,
            %(passed_ball_wild_pitch)s, %(pinch_hitter)s, %(pinch_runner)s,
            %(pitcher_sub)s, %(defensive_sub)s,
            %(fielded_by)s, %(fielding_error)s, %(dropped_ball)s, %(of_assist)s,
            %(field_assist_1)s, %(field_assist_2)s, %(field_assist_3)s,
            %(field_assist_4)s, %(field_assist_5)s,
            %(field_putout_1)s, %(field_putout_2)s, %(field_putout_3)s,
            %(throwing_error_1)s, %(throwing_error_2)s,
            %(data_quality_flag)s
        )
        ON CONFLICT (game_pk, at_bat_number, pitch_number) DO NOTHING;
    """

    def __init__(
        self,
        dsn: str | None = None,
        write_csv: bool = False,
        csv_output_dir: str = ".",
    ) -> None:
        # SIM-153: dsn is now optional — falls back to BASEBALL_DB_DSN env var.
        # Pass an explicit DSN to override (test fixtures, ad-hoc backfills).
        # Raises a clear error if neither is set, rather than letting psycopg2
        # surface a confusing "could not connect" error mid-run.
        resolved = dsn or os.environ.get("BASEBALL_DB_DSN")
        if not resolved:
            raise RuntimeError(
                "HistoricalDataLoader: no DSN provided and BASEBALL_DB_DSN "
                "environment variable is not set.  Pass dsn=… or set "
                "BASEBALL_DB_DSN per .env.example."
            )
        self.dsn = resolved
        self.write_csv = write_csv
        self.csv_output_dir = csv_output_dir

    @contextmanager
    def _get_conn(self):
        conn = psycopg2.connect(self.dsn)
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def load_game(self, game_pk: int, season: int, batter_hand_cache: dict | None = None) -> dict:
        """
        Fetch and load a single game.  Returns a summary dict with counts
        of inserted, skipped (hard errors), and flagged (warnings) rows.
        """
        if batter_hand_cache is None:
            batter_hand_cache = {}

        log.info("Loading game %s …", game_pk)
        raw_rows, game_dict = _fetch_game_pitches(game_pk, batter_hand_cache)
        self._ensure_prerequisites(game_pk, game_dict)
        result = self._process_and_insert(game_pk, season, raw_rows)
        log.info(
            "  game %s: %d inserted, %d skipped (hard errors), %d flagged",
            game_pk, result["inserted"], result["skipped"], result["flagged"],
        )
        return result

    def load_date_range(self, start_date: date, end_date: date) -> None:
        """Incremental loader — fetches all games between two dates."""
        params = {
            "sportId":    1,
            "gameTypes":  GAME_TYPES,
            "startDate":  start_date.strftime("%Y-%m-%d"),
            "endDate":    end_date.strftime("%Y-%m-%d"),
        }
        schedule = _connect("https://statsapi.mlb.com/api/v1/schedule", params)["dates"]
        batter_hand_cache: dict[int, str] = {}

        for date_entry in schedule:
            log.info("Processing date %s", date_entry["date"])
            for game in date_entry["games"]:
                if "rescheduleGameDate" in game or "resumeGameDate" in game:
                    continue
                if not self._game_already_loaded(game["gamePk"]):
                    self.load_game(game["gamePk"], date_entry["date"][:4], batter_hand_cache)

    def refresh_seasons(self, start_year: int = 2017, end_year: int | None = None) -> None:
        """
        Full historical backfill.  Mirrors refresh_plays() from the original
        script.  Skips games that are already fully loaded.
        """
        if end_year is None:
            end_year = datetime.today().year - 1   # don't include current season here

        for season in range(start_year, end_year + 1):
            log.info("=== Season %d ===", season)
            params = {
                "sportId":    1,
                "gameTypes":  GAME_TYPES,
                "season":     season,
            }
            schedule = _connect("https://statsapi.mlb.com/api/v1/schedule", params)["dates"]
            batter_hand_cache: dict[int, str] = {}

            for date_entry in schedule:
                log.info("  %s", date_entry["date"])
                for game in date_entry["games"]:
                    if "rescheduleGameDate" in game or "resumeGameDate" in game:
                        continue
                    if not self._game_already_loaded(game["gamePk"]):
                        self.load_game(game["gamePk"], season, batter_hand_cache)

    # ------------------------------------------------------------------
    # FK prerequisite checks — run before every raw.pitches insert
    # ------------------------------------------------------------------

    def _ensure_prerequisites(self, game_pk: int, game_dict: dict) -> None:
        """
        Guarantees that every FK parent of raw.pitches exists in the database
        before any pitch rows are inserted.  Runs in FK dependency order:

            raw.venues  →  raw.teams  →  raw.players
                                      →  raw.managers
                                      →  raw.games

        Each sub-method does a single batch existence check against the DB
        and only hits the MLB Stats API for IDs that are actually missing.
        A historical backfill across 3 seasons will find most records already
        present after the first few games; subsequent games trigger zero API
        calls for the entities they share.
        """
        gd = game_dict["gameData"]
        managers = game_dict["_managers"]
        game_date = gd["datetime"]["officialDate"]
        season = int(game_date[:4])

        venue_id = gd["venue"]["id"]
        home_team_id = gd["teams"]["home"]["id"]
        away_team_id = gd["teams"]["away"]["id"]

        self._ensure_venue(venue_id, season)
        self._ensure_teams(home_team_id, away_team_id, season, gd)
        self._ensure_players(game_dict)
        self._ensure_managers(managers, home_team_id, away_team_id, season, game_date)
        self._ensure_game(game_pk, season, gd, managers)

    # --- 1. Venues ----------------------------------------------------------

    def _ensure_venue(self, venue_id: int, season: int) -> None:
        """
        Upserts raw.venues if the venue_id is not already present.
        Fetches full venue details (dimensions, surface, roof) from the
        MLB venues endpoint when the record is missing.
        """
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM raw.venues WHERE venue_id = %s AND season = %s", (venue_id, season)
                )
                if cur.fetchone() is not None:
                    return  # already exists

        log.info("  Fetching missing venue %s, season %s", venue_id, season)
        resp = None
        for attempt in range(1, MAX_API_RETRIES + 1):
            try:
                resp = requests.get(f'https://baseballsavant.mlb.com/leaderboard/statcast-park-factors?type=dimensions&year={season}&parks=All&fenceStatType=distance', timeout=30)
                resp.raise_for_status()
            except Exception as exc:
                if attempt == MAX_API_RETRIES:
                    raise
                log.warning("API call failed (attempt %d/%d): %s", attempt, MAX_API_RETRIES, exc)
                time.sleep(RETRY_BACKOFF_S * attempt)
        if resp is None:
            raise
        venue_data = json.loads(resp.text[resp.text.find('var data = [')+11:resp.text.find('}];')+2])
        dimensions = None
        for venue in venue_data:
            if venue["venue_id"] != venue_id:
                continue
            dimensions = venue
            break

        if dimensions is None:
            for attempt in range(1, MAX_API_RETRIES + 1):
                try:
                    resp = requests.get(
                        f'https://baseballsavant.mlb.com/leaderboard/statcast-venue?venueId={venue_id}', timeout=30)
                    resp.raise_for_status()
                except Exception as exc:
                    if attempt == MAX_API_RETRIES:
                        raise
                    log.warning("API call failed (attempt %d/%d): %s", attempt, MAX_API_RETRIES, exc)
                    time.sleep(RETRY_BACKOFF_S * attempt)
            if resp is None:
                raise
            venue_data = json.loads(resp.text[resp.text.find('var data = {') + 11:resp.text.find(']};') + 2])
            dimensions = venue_data['venues'][0]

        data = _connect(
            f'https://statsapi.mlb.com/api/v1/venues/{venue_id}',
            params={'hydrate': 'location,fieldInfo,timezone'}
        )
        v = data["venues"][0]

        field = v.get("fieldInfo", {})
        location = v.get("location", {})

        # Surface: API returns "Artificial Turf" or "Grass" variants
        surface_raw = field.get("turfType", "Grass")
        surface = "Turf" if "turf" in surface_raw.lower() or "artificial" in surface_raw.lower() else "Grass"

        # Roof type mapping
        roof_raw = field.get("roofType", "Open")
        roof_map = {"Open": "Open", "Retractable": "Retractable", "Indoor": "Dome", "Dome": "Dome"}
        roof_type = roof_map.get(roof_raw, "Open")

        # Foul territory: not directly in API; default to Medium
        foul_map = {"Large": "Large", "Medium": "Medium", "Small": "Small"}
        foul_terr = foul_map.get(field.get("leftLine", ""), "Medium")

        coords = location.get("defaultCoordinates", {})
        elevation = location.get("elevation", 0) or 0

        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO raw.venues (
                        venue_id, season, venue_name, city, state,
                        surface, roof_type, elevation_ft,
                        lf_dist, lf_gap_dist, cf_dist, rf_gap_dist, rf_dist,
                        lf_wall_ht, lf_gap_wall_ht, cf_wall_ht, rf_gap_wall_ht, rf_wall_ht,
                        capacity,
                        coordinates_lat, coordinates_lon
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (venue_id, season) DO UPDATE SET
                        venue_name      = EXCLUDED.venue_name,
                        surface         = EXCLUDED.surface,
                        roof_type       = EXCLUDED.roof_type,
                        elevation_ft    = EXCLUDED.elevation_ft,
                        lf_dist         = EXCLUDED.lf_dist,
                        lf_gap_dist     = EXCLUDED.lf_gap_dist,
                        cf_dist         = EXCLUDED.cf_dist,
                        rf_gap_dist     = EXCLUDED.rf_gap_dist,
                        rf_dist         = EXCLUDED.rf_dist,
                        updated_at      = NOW()
                    """,
                    (
                        venue_id,
                        season,
                        dimensions.get('venu_name_short', ' '),
                        location.get("city", ""),
                        location.get("stateAbbrev", ""),
                        surface,
                        roof_type,
                        int(elevation),
                        dimensions.get("distance_lf_line", dimensions.get("left_line", None)),
                        dimensions.get("distance_lf_gap", dimensions.get("left_center", None)),
                        dimensions.get("distance_cf", dimensions.get("center", None)),
                        dimensions.get("distance_rf_gap", dimensions.get("right_center", None)),
                        dimensions.get("distance_rf_line", dimensions.get("right_line", None)),
                        dimensions.get("height_lf_line", None),
                        dimensions.get("height_lf_gap", None),
                        dimensions.get("height_cf", None),
                        dimensions.get("height_rf_gap", None),
                        dimensions.get("height_rf_line", None),
                        v.get("capacity"),
                        coords.get("latitude"),
                        coords.get("longitude"),
                    ),
                )
            conn.commit()

    # --- 2. Teams -----------------------------------------------------------

    def _ensure_teams(
            self, home_team_id: int, away_team_id: int, season: int, gd: dict
    ) -> None:
        """
        Upserts raw.teams for any team not already in the DB.
        All required data is present in the game_dict — no extra API call needed.
        """
        needed = {home_team_id, away_team_id}
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT team_id FROM raw.teams WHERE season = %s AND team_id = ANY(%s)",
                    (season, list(needed),),
                )
                existing = {r[0] for r in cur.fetchall()}

        missing = needed - existing
        if not missing:
            return

        # Division name → schema enum mapping
        div_map = {
            "American League East": "AL East",
            "American League Central": "AL Central",
            "American League West": "AL West",
            "National League East": "NL East",
            "National League Central": "NL Central",
            "National League West": "NL West",
        }

        rows = []
        for side in ("home", "away"):
            t = gd["teams"][side]
            tid = t["id"]
            if tid not in missing:
                continue

            league_name = t.get("league", {}).get("name", "")
            league_code = "AL" if "American" in league_name else "NL"
            division = div_map.get(t.get("division", {}).get("name", ""), "AL East")
            venue_id = t.get("venue", {}).get("id") or gd["venue"]["id"]
            self._ensure_venue(venue_id, season)

            rows.append((tid, season, t["name"], t["abbreviation"], league_code, division, venue_id))

        if not rows:
            return

        log.info("  Inserting missing teams: %s", [r[0] for r in rows])
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                for row in rows:
                    cur.execute(
                        """
                        INSERT INTO raw.teams
                            (team_id, season, team_name, team_abbrev, league, division, venue_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (team_id, season) DO UPDATE SET
                            team_name   = EXCLUDED.team_name,
                            team_abbrev = EXCLUDED.team_abbrev,
                            updated_at  = NOW()
                        """,
                        row,
                    )
            conn.commit()

    # --- 3. Players ---------------------------------------------------------

    def _ensure_players(self, game_dict: dict) -> None:
        """
        Upserts raw.players for every player appearing in this game's boxscore.
        The boxscore contains all batters, pitchers, and fielders with enough
        detail to populate raw.players without an extra API call in most cases.
        For any player whose detail fields are incomplete, falls back to
        GET /api/v1/people/{player_id}.
        """
        boxscore = game_dict.get("liveData", {}).get("boxscore", {}).get("teams", {})
        players_raw: dict[int, dict] = {}

        for side in ("home", "away"):
            for key, pd in boxscore.get(side, {}).get("players", {}).items():
                pid = pd["person"]["id"]
                players_raw[pid] = pd

        if not players_raw:
            return

        # Batch existence check
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT player_id FROM raw.players WHERE player_id = ANY(%s)",
                    (list(players_raw.keys()),),
                )
                existing = {r[0] for r in cur.fetchall()}

        missing_ids = set(players_raw.keys()) - existing
        if not missing_ids:
            return

        log.info("  Fetching %d missing player(s)", len(missing_ids))

        rows = []
        for pid in missing_ids:
            pd = players_raw[pid]
            person = pd.get("person", {})
            name = person.get("fullName", "")
            position = pd.get("position", {}).get("abbreviation", "P")

            # Try boxscore detail first; fall back to people API if incomplete
            bats = person.get("batSide", {}).get("code")
            throws = person.get("pitchHand", {}).get("code")

            if not bats or not throws:
                try:
                    detail = _connect(
                        f"https://statsapi.mlb.com/api/v1/people/{pid}"
                    )["people"][0]
                    bats = detail.get("batSide", {}).get("code", "R")
                    throws = detail.get("pitchHand", {}).get("code", "R")
                    first = detail.get("firstName", "")
                    last = detail.get("lastName", "")
                    birth = detail.get("birthDate")
                    height = detail.get("height")  # "6' 2\""
                    weight = detail.get("weight")
                    debut = detail.get("mlbDebutDate")
                    height_in = _parse_height(height)
                except Exception as exc:
                    log.warning("People API failed for player %s: %s", pid, exc)
                    bats, throws = "R", "R"
                    first = last = ""
                    birth = debut = None
                    height_in = weight = None
            else:
                first = person.get("firstName", "")
                last = person.get("lastName", "")
                birth = person.get("birthDate")
                debut = person.get("mlbDebutDate")
                height_in = _parse_height(person.get("height"))
                weight = person.get("weight")

            rows.append((
                pid, name, first or name, last or name,
                birth, bats or "R", throws or "R",
                position, height_in, weight, debut,
            ))

        with self._get_conn() as conn:
            with conn.cursor() as cur:
                for row in rows:
                    cur.execute(
                        """
                        INSERT INTO raw.players (
                            player_id, full_name, first_name, last_name,
                            birth_date, bats, throws, primary_position,
                            height_inches, weight_lbs, mlb_debut_date
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (player_id) DO UPDATE SET
                            full_name  = EXCLUDED.full_name,
                            updated_at = NOW()
                        """,
                        row,
                    )
            conn.commit()

        # --- 4. Managers --------------------------------------------------------

    def _ensure_managers(
            self,
            managers: dict,
            home_team_id: int,
            away_team_id: int,
            season: int,
            game_date: str
    ) -> None:
        """
        Upserts raw.managers for the home and away managers of this game.
        raw.managers PK is (manager_id, team_id, season_start), so a manager
        who changes teams mid-career will have multiple rows — one per stint.
        """
        candidates = [
            (managers["home_manager_id"], managers["home_manager_name"], home_team_id),
            (managers["away_manager_id"], managers["away_manager_name"], away_team_id),
        ]
        # Filter out games where manager data was unavailable (empty string)
        candidates = [(mid, name, tid) for mid, name, tid in candidates if mid]

        if not candidates:
            return
        check_managers = []
        if managers["home_manager_id"] != '':
            check_managers.append(managers["home_manager_id"])
        if managers["away_manager_id"] != '':
            check_managers.append(managers["away_manager_id"])
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT manager_id, season
                    FROM raw.managers
                    WHERE season = %s AND manager_id = ANY(%s)
                    """,
                    (season, check_managers),
                )
                existing = {(r[0], r[1]) for r in cur.fetchall()}

        missing = [(mid, name, tid) for mid, name, tid in candidates if (mid, season) not in existing]
        if not missing:
            return

        log.info("  Inserting missing managers: %s", [(m[0], m[2]) for m in missing])
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                for mid, name, tid in missing:
                    cur.execute(
                        """
                        SELECT manager_id
                        FROM raw.managers
                        WHERE manager_id <> %s
                        AND season = %s
                        AND team_id = %s
                        """,
                        (mid, season, tid)
                    )
                    m = cur.fetchone()
                    if m is not None:
                        cur.execute(
                            """
                            UPDATE raw.managers
                            SET season_end = %s
                            WHERE manager_id = %s
                            AND season = %s
                            AND team_id = %s
                            """,
                            (date.fromisoformat(game_date), m[0], season, tid)
                        )
                        cur.execute(
                            """
                            INSERT INTO raw.managers 
                                (manager_id, season, full_name, team_id, season_start)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (manager_id, season) DO UPDATE SET
                                full_name  = EXCLUDED.full_name,
                                season_end = EXCLUDED.season_end,
                                updated_at = NOW()
                            """,
                            (mid, season, name, tid, date.fromisoformat(game_date))
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO raw.managers
                                (manager_id, season, full_name, team_id)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (manager_id, season) DO UPDATE SET
                                full_name  = EXCLUDED.full_name,
                                updated_at = NOW()
                            """,
                            (mid, season, name, tid),
                        )
            conn.commit()

        # --- 5. Game record -----------------------------------------------------

    def _ensure_game(
            self, game_pk: int, season: int, gd: dict, managers: dict
    ) -> None:
        """
        Upserts raw.games.  Called after venues/teams/managers are guaranteed
        to exist so all FKs resolve cleanly.

        For completed games, also populates final score and pitcher W/L/S
        fields if available in the game_dict.
        """
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM raw.games WHERE game_pk = %s", (game_pk,)
                )
                if cur.fetchone() is not None:
                    # Game already exists — only update status in case it changed
                    # (Preview → Live → Final progression)
                    status = _map_game_status(gd)
                    cur.execute(
                        "UPDATE raw.games SET status = %s, updated_at = NOW() WHERE game_pk = %s",
                        (status, game_pk),
                    )
                    conn.commit()
                    return

        game_date = gd["datetime"]["officialDate"]
        venue_id = gd["venue"]["id"]
        home_team_id = gd["teams"]["home"]["id"]
        away_team_id = gd["teams"]["away"]["id"]
        game_type = gd.get("game", {}).get("type", "R")
        status = _map_game_status(gd)

        # Final score — present for completed games
        linescore = gd.get("linescore", {})
        home_score = linescore.get("teams", {}).get("home", {}).get("runs")
        away_score = linescore.get("teams", {}).get("away", {}).get("runs")
        home_hits = linescore.get("teams", {}).get("home", {}).get("hits")
        away_hits = linescore.get("teams", {}).get("away", {}).get("hits")
        home_errors = linescore.get("teams", {}).get("home", {}).get("errors")
        away_errors = linescore.get("teams", {}).get("away", {}).get("errors")
        innings = linescore.get("currentInning")

        # Inning-by-inning scores as JSONB
        innings_data = linescore.get("innings", [])
        inning_scores = {
            "home": [i.get("home", {}).get("runs") for i in innings_data],
            "away": [i.get("away", {}).get("runs") for i in innings_data],
        } if innings_data else None

        # Winning / losing / save pitcher
        decisions = gd.get("decisions", {})
        winning_pid = decisions.get("winner", {}).get("id")
        losing_pid = decisions.get("loser", {}).get("id")
        save_pid = decisions.get("save", {}).get("id")

        # Weather
        weather = gd.get("weather", {})
        wind = gd.get("weather", {})

        log.info("  Inserting missing game record %s", game_pk)
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                import json as _json
                cur.execute(
                    """
                    INSERT INTO raw.games (
                        game_pk, season, game_date, game_type, status,
                        venue_id, home_team_id, away_team_id,
                        home_manager_id, away_manager_id,
                        home_score_final, away_score_final,
                        innings_played,
                        home_hits, away_hits, home_errors, away_errors,
                        inning_scores,
                        winning_pitcher_id, losing_pitcher_id, save_pitcher_id,
                        weather_temp, weather_condition,
                        wind_speed, wind_direction
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s
                    )
                    ON CONFLICT (game_pk) DO UPDATE SET
                        status             = EXCLUDED.status,
                        home_score_final   = COALESCE(EXCLUDED.home_score_final, raw.games.home_score_final),
                        away_score_final   = COALESCE(EXCLUDED.away_score_final, raw.games.away_score_final),
                        winning_pitcher_id = COALESCE(EXCLUDED.winning_pitcher_id, raw.games.winning_pitcher_id),
                        losing_pitcher_id  = COALESCE(EXCLUDED.losing_pitcher_id,  raw.games.losing_pitcher_id),
                        save_pitcher_id    = COALESCE(EXCLUDED.save_pitcher_id,    raw.games.save_pitcher_id),
                        updated_at         = NOW()
                    """,
                    (
                        game_pk,
                        season,
                        date.fromisoformat(game_date),
                        game_type,
                        status,
                        venue_id,
                        home_team_id,
                        away_team_id,
                        managers["home_manager_id"] or None,
                        managers["away_manager_id"] or None,
                        home_score,
                        away_score,
                        innings,
                        home_hits,
                        away_hits,
                        home_errors,
                        away_errors,
                        _json.dumps(inning_scores) if inning_scores else None,
                        winning_pid,
                        losing_pid,
                        save_pid,
                        weather.get("temp"),
                        weather.get("condition"),
                        wind.get("speed"),
                        wind.get("direction"),
                    ),
                )
            conn.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _game_already_loaded(self, game_pk: int) -> bool:
        """Returns True if any pitch rows exist for this game_pk."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM raw.pitches WHERE game_pk = %s LIMIT 1",
                    (game_pk,)
                )
                return cur.fetchone() is not None

    def _process_and_insert(
        self,
        game_pk: int,
        season: int,
        raw_rows: list[dict[str, Any]],
    ) -> dict[str, int]:
        """
        Validates, builds, and batch-inserts all pitch rows for a game.
        Returns count summary.

        SIM-093: Hard-error rows are now persisted to raw.etl_errors as well
        as logged.  Without this, skipped rows had no audit trail and there
        was no way to find which pitches a re-ingest would need to recover.
        """
        to_insert:    list[dict[str, Any]] = []
        hard_errors:  list[tuple[int, int, list[str]]] = []   # (at_bat, pitch, errors)
        flagged_count = 0

        for raw_row in raw_rows:
            row = _build_row_dict(raw_row, game_pk, season, raw_row.get("game_date", ""))
            vr  = _validate_row(row)

            if not vr.is_valid:
                hard_errors.append(
                    (row.get("at_bat_number"), row.get("pitch_number"), vr.hard_errors)
                )
                continue

            if vr.warnings:
                flagged_count += 1
                for w in vr.warnings:
                    log.warning(
                        "  game=%s ab=%s pitch=%s — %s",
                        game_pk, row.get("at_bat_number"), row.get("pitch_number"), w
                    )

            to_insert.append(row)

        if hard_errors:
            log.error(
                "  game %s: %d rows skipped due to hard validation errors:",
                game_pk, len(hard_errors)
            )
            for ab, pitch_num, errs in hard_errors:
                log.error("    ab=%s pitch_number=%s: %s",
                          ab, pitch_num, "; ".join(errs))
            # SIM-093: Persist the audit trail.  Best-effort — never let a
            # logging failure prevent the rest of the game from loading.
            try:
                self._log_etl_errors(game_pk, hard_errors)
            except Exception as exc:                       # noqa: BLE001
                log.error(
                    "  game %s: failed to write etl_errors audit rows: %s",
                    game_pk, exc,
                )

        inserted = self._batch_insert(to_insert)
        self._log_freshness(game_pk, to_insert)

        return {
            "inserted": inserted,
            "skipped":  len(hard_errors),
            "flagged":  flagged_count,
        }

    def _log_etl_errors(
        self,
        game_pk: int,
        errors: list[tuple[int | None, int | None, list[str]]],
    ) -> None:
        """
        SIM-093: Bulk-insert one raw.etl_errors row per skipped pitch.

        Schema lives in db/schemas/01_postgres_schema.sql and is created via
        Alembic migration 0011.  This is a fail-soft path — caller wraps in
        try/except so an etl_errors write failure never aborts the ingest.
        """
        if not errors:
            return

        sql = """
            INSERT INTO raw.etl_errors
                (game_pk, at_bat_number, pitch_number, error_type, error_messages)
            VALUES (%s, %s, %s, 'HARD', %s)
        """
        rows = [
            (game_pk, ab, pitch_num, list(msgs))
            for (ab, pitch_num, msgs) in errors
        ]
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, sql, rows)
            conn.commit()

    def reprocess_errored_games(self, since: date) -> list[int]:
        """
        SIM-093: Return distinct game_pks that have raw.etl_errors rows
        on or after ``since``.  Caller iterates the list and reruns
        load_game(game_pk, season) for each — typical use is "after a
        validator bug-fix, find all games that were affected and re-ingest".

        Returns an empty list if no errors are recorded in the window.
        """
        sql = """
            SELECT DISTINCT game_pk
            FROM   raw.etl_errors
            WHERE  created_at >= %s
              AND  game_pk IS NOT NULL
            ORDER BY game_pk
        """
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (since,))
                return [r[0] for r in cur.fetchall()]

    def _batch_insert(self, rows: list[dict[str, Any]]) -> int:
        """Inserts rows in BATCH_SIZE chunks.  Returns total rows inserted."""
        if not rows:
            return 0

        inserted = 0
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                for offset in range(0, len(rows), BATCH_SIZE):
                    batch = rows[offset : offset + BATCH_SIZE]
                    psycopg2.extras.execute_batch(cur, self.INSERT_SQL, batch)
                    inserted += len(batch)
            conn.commit()

        return inserted

    def _log_freshness(self, game_pk: int, rows: list[dict[str, Any]]) -> None:
        """
        Upserts raw.etl_data_freshness so the system knows the last loaded
        game_date per pitcher and per batter.

        Table DDL lives in db/schemas/01_postgres_schema.sql and is applied via
        Alembic migration 0003.  SIM-083 removed the dead FRESHNESS_TABLE_DDL
        string constant that previously appeared at module level — it was never
        executed and caused UndefinedTable errors in _process_and_insert().
        """
        if not rows:
            return

        game_date = rows[0].get("game_date")
        pitcher_ids = {r["pitcher"] for r in rows if r.get("pitcher")}
        batter_ids  = {r["batter"]  for r in rows if r.get("batter")}

        upsert_sql = """
            INSERT INTO raw.etl_data_freshness (entity_type, entity_id, last_game_pk, last_date)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (entity_type, entity_id) DO UPDATE
                SET last_game_pk = EXCLUDED.last_game_pk,
                    last_date    = EXCLUDED.last_date,
                    updated_at   = NOW()
            WHERE EXCLUDED.last_date > raw.etl_data_freshness.last_date;
        """
        entries = (
            [("pitcher", pid, game_pk, game_date) for pid in pitcher_ids] +
            [("batter",  bid, game_pk, game_date) for bid in batter_ids]
        )

        with self._get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, upsert_sql, entries)
            conn.commit()

    # ------------------------------------------------------------------
    # Post-load quality report
    # ------------------------------------------------------------------

    def quality_report(self, game_pk: int | None = None) -> None:
        """
        Prints a summary of data_quality_flag=TRUE rows.
        Scoped to a specific game if game_pk is provided.
        """
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                if game_pk:
                    cur.execute(
                        """
                        SELECT pitcher, COUNT(*) AS flagged_pitches
                        FROM raw.pitches
                        WHERE data_quality_flag = TRUE AND game_pk = %s
                        GROUP BY pitcher ORDER BY flagged_pitches DESC
                        """,
                        (game_pk,)
                    )
                else:
                    cur.execute(
                        """
                        SELECT game_date, COUNT(*) AS flagged_pitches
                        FROM raw.pitches
                        WHERE data_quality_flag = TRUE
                        GROUP BY game_date ORDER BY game_date DESC
                        LIMIT 30
                        """
                    )
                rows = cur.fetchall()

        if not rows:
            log.info("No flagged rows found.")
            return

        header = "pitcher / flagged" if game_pk else "date / flagged"
        log.info("Quality report (%s):", header)
        for r in rows:
            log.info("  %s → %s flagged rows", r[0], r[1])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
# NOTE (SIM-083): FRESHNESS_TABLE_DDL was removed from this file.
# The raw.etl_data_freshness CREATE TABLE DDL now lives in:
#   db/schemas/01_postgres_schema.sql
# Applied via Alembic migration 0003 (db/migrations/versions/0003_*.py).
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = HistoricalDataLoader('postgresql://localhost/baseball_simulator?user=postgres&password=baseball')
    loader.refresh_seasons()
