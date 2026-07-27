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

Corrective re-ingest
--------------------
``load_game`` CANNOT repair already-loaded data: the pitch INSERT carries
``ON CONFLICT (game_pk, at_bat_number, pitch_number) DO NOTHING``, so re-running
it after a parser fix discards every row and reports success.  Use the reload
path instead — it DELETEs the game's rows and re-inserts in one transaction:

  loader.reload_game(game_pk=745528, season=2024)   # one game
  loader.refresh_seasons(2017, 2025, reload=True)   # every Final game
  loader.load_date_range(d1, d2, reload=True)       # a date window

A reload sweep contains per-game failures (counted in the returned summary) so
one malformed feed cannot abort a 21,600-game re-ingest.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool

from pipeline.etl.coercion import to_bool, to_float, to_int, to_str

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

BATCH_SIZE = 500  # rows per executemany call
MAX_API_RETRIES = 10  # infinite-retry inherited from original; capped here
RETRY_BACKOFF_S = 2.0  # seconds between retries

HTTP_TIMEOUT_S = 30.0
_USER_AGENT = "baseball-sim-etl/1.0 (+historical loader)"

# SIM-446: the ETL's HTTP transport is stdlib ``urllib.request`` BY DEFAULT.
#
# The corrective reload sweep used to die with::
#
#     Fatal Python error: _PyEval_EvalFrameDefault: Executing a cache.
#
# — CPython's evaluation loop dispatching into an inline-cache entry instead of a
# real instruction, i.e. the bytecode / inline cache of ``_build_row_dict`` being
# corrupted underneath it. It presented as SIGSEGV (exit 139) in the container and
# as that fatal error natively on Windows, at a DIFFERENT game every run (observed
# at 3, 5, 66, 191, 193, 318 and 405 games). Memory exhaustion, the Docker/WSL2
# layer, psycopg2-binary's bundled OpenSSL, numpy/OpenBLAS and the host's BSOD
# driver were each ruled out by direct measurement rather than inference.
#
# Running the identical workload on ``urllib.request`` completed the FULL 2017
# season — 2,468 games, 732,475 pitch rows — with no crash. Against a fault that
# had been firing roughly every 200 games, surviving 2,468 is about a 1-in-10^5
# coincidence, so the transport is treated as the cause. What urllib takes out of
# the loop: ``requests`` itself, plus the mypyc-compiled ``charset_normalizer``
# (FOUR copies were resident — two standalone, two vendored inside requests),
# ``_brotli`` and ``simplejson._speedups``.
#
# TRADE-OFFS, stated plainly and MEASURED rather than estimated:
#
#   1. No keep-alive pool — each of the ~65,000 requests in a full backfill pays
#      its own TCP+TLS handshake.
#   2. No transparent decompression. ``requests`` sent
#      ``Accept-Encoding: gzip, deflate, br`` and decompressed for you. urllib does
#      NOT merely omit the header — ``http.client.putrequest`` injects
#      ``Accept-Encoding: identity``, which per RFC 9110 explicitly forbids the
#      server from compressing (verified against a loopback server on 3.13).
#      That is load-bearing: it is WHY :func:`_decode_body` has no gunzip branch.
#      A compressed body cannot arrive, so there is nothing to decompress — the
#      two facts are a matched pair, not an oversight. See the lock-step guard in
#      ``test_compression_is_not_requested_without_decompression_support``.
#      Cost, measured on a real feed/live payload (game 492011): 865,506 bytes
#      identity vs 116,619 gzipped — 7.4x. Across a 9-season backfill (~21,600
#      games) that is ~18.7 GB instead of ~2.5 GB.
#
# Both are accepted. The TIME cost of (2) is only ~70 ms/game (0.21 s vs 0.14 s per
# fetch, i.e. ~25 min across a whole backfill that already runs for hours), and a
# slower backfill that finishes beats a faster one that dies at game 200. Do NOT add
# gzip here casually: it would put C-extension decompression back into the very loop
# whose corruption is still unexplained, and would no longer be the configuration
# that survived 2,468 consecutive games. Only revisit it on a metered connection,
# and re-validate at season scale if you do.
#
# ``ETL_HTTP_TRANSPORT=requests`` restores the pooled Session for A/B comparison;
# ``requests`` is imported LAZILY so the default path never loads those extensions
# into the address space at all.
_HTTP_TRANSPORT = os.environ.get("ETL_HTTP_TRANSPORT", "urllib").strip().lower()

# SIM-441: statuses worth retrying. A 404/400/403 is a permanent answer — the old
# code retried it 10 times over ~90 s, turning one missing game into a 90-second
# stall. 429 and 5xx are transient and ARE retried (429 honours Retry-After).
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
_MAX_RETRY_AFTER_S = 60.0  # never sleep longer than this on a server's say-so

# SIM-090: connection pool sizing.  A historical backfill issues many short
# DB round-trips per game (one per FK-existence check, plus inserts and the
# freshness upsert).  Re-connecting for every one of those is wasteful, so we
# borrow from a pool instead.  Sized via env so ops can tune for the box.
ETL_DB_POOL_MIN = int(os.environ.get("ETL_DB_POOL_MIN", "1"))
ETL_DB_POOL_MAX = int(os.environ.get("ETL_DB_POOL_MAX", "8"))

# SIM-441: 'C' ("Championship") was removed — it is an obsolete MLB game type
# predating our 2015+ window, and it is NOT in the raw.games CHECK constraint
# (('R','P','S','A','D','F','L','W')), so a single such game would have raised a
# CheckViolation and, before the per-game isolation below, killed the whole run.
GAME_TYPES = ["R", "F", "D", "L", "W", "P"]


class ReloadShrinkError(RuntimeError):
    """A reload would have replaced a game's pitch rows with strictly fewer rows.

    Raised inside :meth:`HistoricalDataLoader._batch_insert` BEFORE the commit, so
    the DELETE is rolled back and the existing rows survive.  The usual cause is a
    transient feed returning HTTP 200 with an empty or pitch-less ``allPlays``;
    re-running the reload once the feed is healthy is the fix.  Pass
    ``allow_shrink=True`` when a game genuinely has fewer pitches than the stored
    copy (e.g. correcting a previously double-ingested game).
    """


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


# ---------------------------------------------------------------------------
# API helpers  (preserve original retry-forever behaviour, now with cap + backoff)
# ---------------------------------------------------------------------------


class SavantScrapeError(RuntimeError):
    """Baseball-Savant returned a page this loader cannot parse.

    The venue dimensions are scraped out of an inline ``var data = …`` literal in
    an HTML page — there is no API for them. SIM-441 made that parse fail LOUDLY:
    it was previously unguarded ``resp.text.find(...)`` slicing, so a page-shape
    change would have sliced a wrong substring and either raised an opaque
    JSONDecodeError deep in the call stack or, worse, parsed a *different*
    object and written plausible-but-wrong stadium dimensions.
    """


def _parse_savant_embedded_json(html: str, start_marker: str, end_marker: str, end_pad: int) -> Any:
    """Extract and parse the inline JSON literal a Savant leaderboard embeds.

    Raises :class:`SavantScrapeError` with the surrounding context when either
    marker is missing or the slice is not valid JSON, rather than letting a
    silent mis-slice through.
    """
    start = html.find(start_marker)
    if start == -1:
        raise SavantScrapeError(
            f"start marker {start_marker!r} not found in the Savant response "
            f"({len(html)} bytes) — the page shape has changed"
        )
    payload_start = start + len(start_marker) - 1  # keep the opening bracket/brace
    end = html.find(end_marker, payload_start)
    if end == -1:
        raise SavantScrapeError(
            f"end marker {end_marker!r} not found after offset {payload_start} — "
            "the page shape has changed"
        )
    try:
        return json.loads(html[payload_start : end + end_pad])
    except json.JSONDecodeError as exc:
        raise SavantScrapeError(
            f"embedded Savant payload between {start_marker!r} and {end_marker!r} "
            f"is not valid JSON: {exc}"
        ) from exc


class HttpError(OSError):
    """A non-2xx response the retry policy could not resolve.

    SIM-446: replaces ``requests.HTTPError`` so callers stay transport-agnostic.
    Subclasses ``OSError`` to match ``requests.RequestException``'s own ancestry,
    which keeps every existing ``except OSError`` handler behaving identically.
    """

    def __init__(self, message: str, *, status_code: int, url: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class _Response:
    """The slice of an HTTP response this module actually consumes.

    Deliberately tiny: ``.text``, the status, the headers, and the two methods
    the retry policy calls. Both transports build one of these, so nothing
    downstream of :func:`_fetch_once` knows which library made the request.
    """

    __slots__ = ("headers", "status_code", "text", "url")

    def __init__(self, status_code: int, headers: dict[str, str], text: str, url: str) -> None:
        self.status_code = status_code
        self.headers = headers
        self.text = text
        self.url = url

    def header(self, name: str) -> str | None:
        """Case-insensitive lookup — HTTP header names are not case-sensitive."""
        target = name.lower()
        for key, value in self.headers.items():
            if key.lower() == target:
                return value
        return None

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HttpError(
                f"HTTP {self.status_code} for {self.url}",
                status_code=self.status_code,
                url=self.url,
            )


def _decode_body(body: bytes, headers: Any) -> str:
    """Decode a response body using the declared charset, defaulting to UTF-8."""
    ctype = ""
    getter = getattr(headers, "get", None)
    if getter is not None:
        ctype = getter("Content-Type") or getter("content-type") or ""
    charset = "utf-8"
    if "charset=" in ctype.lower():
        charset = ctype.lower().split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:  # server named a charset Python doesn't know
        return body.decode("utf-8", errors="replace")


def _urllib_fetch(url: str, params: dict | None) -> _Response:
    """One round-trip over stdlib ``urllib.request`` — the default transport."""
    if params:
        # doseq=True matches requests' handling of list values (e.g. gameTypes).
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310
            body = _decode_body(resp.read(), resp.headers)
            return _Response(resp.status, dict(resp.headers), body, url)
    except urllib.error.HTTPError as exc:
        # urllib RAISES on 4xx/5xx. The retry policy has to inspect the status to
        # decide permanent-vs-transient, so hand it back as a response instead.
        try:
            body = _decode_body(exc.read(), exc.headers)
            return _Response(exc.code, dict(exc.headers), body, url)
        finally:
            exc.close()


_requests_session_lock = threading.Lock()
_requests_session_cache: Any = None


def _requests_session() -> Any:
    """Build the pooled ``requests`` Session on first use (opt-in transport).

    Lazy on purpose: importing ``requests`` pulls ``charset_normalizer``,
    ``_brotli`` and ``simplejson._speedups`` into the address space, which is
    exactly what the default transport exists to avoid. See ``_HTTP_TRANSPORT``.
    """
    global _requests_session_cache
    with _requests_session_lock:
        if _requests_session_cache is None:
            import requests  # noqa: PLC0415 — deliberately deferred; see the docstring

            session = requests.Session()
            session.headers.update({"User-Agent": _USER_AGENT})
            _requests_session_cache = session
        return _requests_session_cache


def _requests_fetch(url: str, params: dict | None) -> _Response:
    """One round-trip over a pooled ``requests`` Session — opt-in transport."""
    resp = _requests_session().get(url, params=params, timeout=HTTP_TIMEOUT_S)
    return _Response(resp.status_code, dict(resp.headers), resp.text, resp.url)


def _fetch_once(url: str, params: dict | None = None) -> _Response:
    """A single HTTP round-trip with NO retry — the transport seam."""
    if _HTTP_TRANSPORT == "requests":
        return _requests_fetch(url, params)
    return _urllib_fetch(url, params)


# Transient failures worth retrying. ``urllib.error.URLError``, ``TimeoutError``,
# ``ConnectionError`` and ``requests.RequestException`` are ALL ``OSError``
# subclasses, so one entry covers both transports; ``http.client.HTTPException``
# (malformed response, remote disconnect) is the one that isn't.
_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (OSError, http.client.HTTPException)


def _retry_after_seconds(resp: _Response, attempt: int) -> float:
    """Seconds to wait before the next attempt, honouring ``Retry-After``."""
    raw = resp.header("Retry-After") if resp is not None else None
    if raw:
        try:
            return min(float(raw), _MAX_RETRY_AFTER_S)
        except (TypeError, ValueError):
            pass  # Retry-After may be an HTTP-date; fall through to backoff
    return RETRY_BACKOFF_S * attempt


def _http_get(url: str, params: dict | None = None) -> _Response:
    """GET with bounded retry, returning the first SUCCESSFUL response.

    SIM-441 fixed three things here:

    * **Permanent errors are no longer retried.** A 404/400/403 is a final
      answer; retrying it ``MAX_API_RETRIES`` times with backoff turned one
      missing resource into a ~90-second stall. Only :data:`_RETRYABLE_STATUS`
      (429 + 5xx + timeouts) is retried, and 429 honours ``Retry-After``.
    * **A successful response is returned immediately.** The old venue loops had
      no ``break``, so a fetch that succeeded on attempt 1 was overwritten by
      attempt 10 — and if THAT one failed transiently, the caller raised despite
      having held valid data.
    * **One pooled Session** instead of a fresh TCP+TLS handshake per call.

    SIM-446 moved the round-trip itself behind :func:`_fetch_once` so the retry
    policy is shared by both transports. Note the pooling bullet above now only
    applies to ``ETL_HTTP_TRANSPORT=requests``.
    """
    last_exc: Exception | None = None
    for attempt in range(1, MAX_API_RETRIES + 1):
        try:
            resp = _fetch_once(url, params)
        except HttpError:
            raise  # a status we already classified as permanent — do not retry
        except _TRANSIENT_ERRORS as exc:  # connection/timeout — transient
            last_exc = exc
            if attempt == MAX_API_RETRIES:
                raise
            log.warning("HTTP GET failed (attempt %d/%d): %s", attempt, MAX_API_RETRIES, exc)
            time.sleep(RETRY_BACKOFF_S * attempt)
            continue

        if resp.status_code < 400:
            return resp

        if resp.status_code not in _RETRYABLE_STATUS:
            # Permanent: fail now rather than burning the retry budget.
            resp.raise_for_status()

        last_exc = HttpError(
            f"HTTP {resp.status_code} for {url}", status_code=resp.status_code, url=url
        )
        if attempt == MAX_API_RETRIES:
            resp.raise_for_status()
        delay = _retry_after_seconds(resp, attempt)
        log.warning(
            "HTTP %s (attempt %d/%d), retrying in %.1fs: %s",
            resp.status_code,
            attempt,
            MAX_API_RETRIES,
            delay,
            url,
        )
        time.sleep(delay)

    raise last_exc or RuntimeError(f"exhausted retries for {url}")


def _connect(url: str, params: dict | None = None) -> dict:
    return _http_get(url, params).json()


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

    game_date = game_dict["gameData"]["datetime"]["officialDate"]
    home_team_id = game_dict["gameData"]["teams"]["home"]["id"]
    home_team = game_dict["gameData"]["teams"]["home"]["abbreviation"]
    away_team_id = game_dict["gameData"]["teams"]["away"]["id"]
    away_team = game_dict["gameData"]["teams"]["away"]["abbreviation"]
    venue_id = game_dict["gameData"]["venue"]["id"]
    venue_name = game_dict["gameData"]["venue"]["name"]

    home_manager_id, home_manager_name = "", ""
    away_manager_id, away_manager_name = "", ""

    home_coaches = _connect(
        f"https://statsapi.mlb.com/api/v1/teams/{home_team_id}/coaches?date={game_date}"
    ).get("roster", [])
    for coach in home_coaches:
        if coach["jobId"] == "NTRM":
            home_manager_id = coach["person"]["id"]
            home_manager_name = coach["person"]["fullName"]
            break
        if coach["jobId"] == "MNGR":
            home_manager_id = coach["person"]["id"]
            home_manager_name = coach["person"]["fullName"]
        if coach["jobId"] == "COAB" and home_manager_name == "":
            home_manager_id = coach["person"]["id"]
            home_manager_name = coach["person"]["fullName"]
    if home_manager_name == "":
        # SIM-441: do NOT fall back to coaches[0].
        # The old code installed whatever coach the roster happened to list
        # first — typically a pitching coach — as the manager, with no log
        # line. That fabricated id then propagated to every pitch row, to
        # raw.games and to raw.managers; and because it was "missing",
        # _ensure_managers' closeout branch fired and set the REAL manager's
        # season_end, marking him departed (raw.managers treats a NULL
        # season_end as "currently active"). Leaving it empty is safe:
        # _ensure_managers filters falsy ids and _ensure_game binds `or None`.
        log.warning(
            "  game %s: no NTRM/MNGR/COAB coach for the home team (%s roles "
            "returned); leaving home_manager unset rather than guessing",
            game_pk,
            len(home_coaches),
        )

    away_coaches = _connect(
        f"https://statsapi.mlb.com/api/v1/teams/{away_team_id}/coaches?date={game_date}"
    ).get("roster", [])
    for coach in away_coaches:
        if coach["jobId"] == "NTRM":
            away_manager_id = coach["person"]["id"]
            away_manager_name = coach["person"]["fullName"]
            break
        if coach["jobId"] == "MNGR":
            away_manager_id = coach["person"]["id"]
            away_manager_name = coach["person"]["fullName"]
        if coach["jobId"] == "COAB" and away_manager_name == "":
            away_manager_id = coach["person"]["id"]
            away_manager_name = coach["person"]["fullName"]
    if away_manager_name == "":
        # SIM-441: do NOT fall back to coaches[0].
        # The old code installed whatever coach the roster happened to list
        # first — typically a pitching coach — as the manager, with no log
        # line. That fabricated id then propagated to every pitch row, to
        # raw.games and to raw.managers; and because it was "missing",
        # _ensure_managers' closeout branch fired and set the REAL manager's
        # season_end, marking him departed (raw.managers treats a NULL
        # season_end as "currently active"). Leaving it empty is safe:
        # _ensure_managers filters falsy ids and _ensure_game binds `or None`.
        log.warning(
            "  game %s: no NTRM/MNGR/COAB coach for the away team (%s roles "
            "returned); leaving away_manager unset rather than guessing",
            game_pk,
            len(away_coaches),
        )

    home_score_before, away_score_before = 0, 0
    outs = 0
    prev_half = "bottom"
    pitch_rows: list[dict[str, Any]] = []

    for play in game_dict["liveData"]["plays"]["allPlays"]:
        at_bat_number = play["atBatIndex"] + 1
        inning = play["about"]["inning"]
        top_bot = play["about"]["halfInning"]
        if top_bot != prev_half:
            outs = 0
        balls, strikes = 0, 0
        # SIM-441: bind the post-play score at PLAY scope. These are assigned
        # inside the pitch branch but read at play scope by the score re-sync
        # below — and Python evaluates a `.get(key, default)` default EAGERLY, so
        # the read happens even when the key is present. A pitch-less FIRST play
        # (every event taken by the non-pitch `continue`) would otherwise raise
        # NameError before the re-sync could run.
        home_score_after, away_score_after = home_score_before, away_score_before
        # SIM-441: the `.replace(",", "")` here and on the pitch description was a
        # leftover from the CSV-writing ancestor of this loader. Both values are
        # bound psycopg2 parameters now, so stripping commas was pure lossy
        # mutation — it stored "In play out(s)" for "In play, out(s)" on every
        # one of ~6.55M rows.
        play_description = play["result"].get("description", "")
        event = ""
        pinch_hitter = pinch_runner = pitcher_sub = defensive_sub = False

        for i in range(len(play["playEvents"])):
            # --- Substitution detection ---
            # Walk the play's events in order.  A substitution is recorded on the
            # non-pitch event where it actually occurs and stays latched until the
            # next pitch row is written (the flags are reset after the append
            # below), so the flag lands on the first pitch thrown AFTER the
            # substitution rather than on every pitch of the plate appearance.
            sub_event = play["playEvents"][i]
            if not sub_event["isPitch"]:
                evt_type = sub_event.get("details", {}).get("eventType", "")
                if evt_type == "defensive_substitution":
                    defensive_sub = True
                if evt_type == "pitching_substitution":
                    pitcher_sub = True
                if evt_type == "offensive_substitution":
                    pos_code = sub_event.get("position", {}).get("code", "")
                    if pos_code == "11":
                        pinch_hitter = True
                    if pos_code == "12":
                        pinch_runner = True
                continue

            # --- Pre-pitch runner state ---
            offense = play["playEvents"][i]["offense"]
            pre_runner_1b = offense.get("first", {}).get("id", "")
            pre_runner_2b = offense.get("second", {}).get("id", "")
            pre_runner_3b = offense.get("third", {}).get("id", "")

            # --- Mid-pitch events (SB, WP/PB) ---
            sb_attempt_2b = sb_attempt_3b = sb_attempt_home = False
            sb_success_2b = sb_success_3b = sb_success_home = False
            wild_pitch_passed_ball = False
            a = 1
            while True:
                if i + a < len(play["playEvents"]) and not play["playEvents"][i + a]["isPitch"]:
                    et = play["playEvents"][i + a].get("details", {}).get("eventType", "")
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

            bat_score = away_score_before if top_bot == "top" else home_score_before
            field_score = home_score_before if top_bot == "top" else away_score_before

            pitch_number = play_event["pitchNumber"]
            pitcher = play_event["defense"]["pitcher"]["id"]
            pitcher_hand = play_event["defense"]["pitcher"]["pitchHand"]["code"]
            batter = play_event["offense"]["batter"]["id"]
            bat_side = play_event["offense"]["batter"]["batSide"]["code"]

            if batter in batter_hand_cache:
                bat_hand = batter_hand_cache[batter]
            else:
                bat_hand = _connect(
                    f"https://statsapi.mlb.com{play_event['offense']['batter']['link']}"
                )["people"][0]["batSide"]["code"]
                batter_hand_cache[batter] = bat_hand

            defense = play_event["defense"]
            catcher = defense["catcher"]["id"]
            first_base = defense["first"]["id"]
            second_base = defense["second"]["id"]
            third_base = defense["third"]["id"]
            shortstop = defense["shortstop"]["id"]
            left_field = defense["left"]["id"]
            center_field = defense["center"]["id"]
            right_field = defense["right"]["id"]

            if play_event["index"] == play["pitchIndex"][-1]:
                event = play["result"]["eventType"]

            pd_data = play_event["pitchData"]
            pitch_code = play_event["details"]["code"]
            pitch_code_description = play_event["details"]["description"]
            pitch_type = play_event["details"].get("type", {"code": ""}).get("code", "")
            pitch_type_description = play_event["details"].get("type", {"description": ""})[
                "description"
            ]
            strike_zone_top = pd_data["strikeZoneTop"]
            strike_zone_bottom = pd_data["strikeZoneBottom"]
            start_speed = pd_data.get("startSpeed", "")
            end_speed = pd_data.get("endSpeed", "")
            coords = pd_data["coordinates"]
            release_x = coords.get("x0", "")
            release_y = coords.get("y0", "")
            release_z = coords.get("z0", "")
            velocity_x = coords.get("vX0", "")
            velocity_y = coords.get("vY0", "")
            velocity_z = coords.get("vZ0", "")
            acceleration_x = coords.get("aX", "")
            acceleration_y = coords.get("aY", "")
            acceleration_z = coords.get("aZ", "")
            pfx_x = coords.get("pfxX", "")
            pfx_z = coords.get("pfxZ", "")
            p_x = coords.get("pX", "")
            p_z = coords.get("pZ", "")
            x = coords.get("x", "")
            y = coords.get("y", "")
            breaks = pd_data["breaks"]
            spin_rate = breaks.get("spinRate", "")
            spin_direction = breaks.get("spinDirection", "")
            break_angle = breaks.get("breakAngle", "")
            break_length = breaks.get("breakLength", "")
            break_y = breaks.get("breakY", "")
            break_vertical = breaks.get("breakVertical", "")
            break_vertical_induced = breaks.get("breakVerticalInduced", "")
            break_horizontal = breaks.get("breakHorizontal", "")
            zone = pd_data.get("zone", "")
            extension = pd_data.get("extension", "")

            launch_speed = launch_angle = total_distance = ""
            coord_x = coord_y = spray_angle = batted_ball_type = hit_location = ""
            if "hitData" in play_event:
                hd = play_event["hitData"]
                launch_speed = hd.get("launchSpeed", "")
                launch_angle = hd.get("launchAngle", "")
                total_distance = hd.get("totalDistance", "")
                coord_x = hd["coordinates"].get("coordX", "")
                coord_y = hd["coordinates"].get("coordY", "")
                if coord_x != "" and coord_y != "":
                    # SIM-441: coord_y > 198.27 is BEHIND home plate (a foul pop
                    # into the screen/backstop). `arctan` of a negative
                    # denominator mirrors those into the fair-field quadrant, so
                    # a ball struck behind the plate reported a plausible pull or
                    # oppo angle. `arctan2` is not the fix either: the true angle
                    # lies outside +/-90 deg and the pull/oppo thresholds cannot
                    # represent it. Leave spray_angle NULL for those ~1-2% of
                    # batted balls rather than record a fabricated direction.
                    if coord_y > 198.27:
                        spray_angle = ""
                    elif coord_y == 198.27:
                        spray_angle = 90 if coord_x > 125.42 else -90
                    else:
                        # SIM-445: `math.atan`, not `np.arctan`. This was the ONLY
                        # numpy call in this 2,900-line module, and importing numpy
                        # for it drags OpenBLAS (DYNAMIC_ARCH, MAX_THREADS=64),
                        # libgfortran and libquadmath into every ETL process. It is
                        # also a scalar, so numpy bought nothing but returned a
                        # numpy.float64 that then had to be coerced back on every
                        # batted ball.
                        spray_angle = math.atan((coord_x - 125.42) / (198.27 - coord_y)) * (
                            180 / math.pi
                        )
                batted_ball_type = hd["trajectory"]
                hit_location = hd.get("location", "")

            outs_on_pitch = play_event["count"]["outs"] - outs
            outs_after_pitch = 0
            runner_on_first_score = runner_on_second_score = runner_on_third_score = False
            home_score_after = home_score_before
            away_score_after = away_score_before
            runs = earned_runs = rbis = 0
            fielded_by = of_assist = fielding_error = dropped_ball = ""
            assist_dict: dict[str, Any] = {}
            putout_dict: dict[str, Any] = {}
            throwing_error_dict: dict[str, Any] = {}
            assist_tracker = putout_tracker = throwing_error_tracker = 1
            # SIM-441: raw.pitches has field_assist_1..5, field_putout_1..3 and
            # throwing_error_1..2. Credits past those slots used to be written to
            # keys _build_row_dict never reads, so they vanished with no warning
            # and no ledger row. Multi-runner rundowns are rare enough that the
            # 6th+ assist is not worth its own column, but the OVERFLOW must be
            # visible so a consumer can exclude or measure those plays instead of
            # silently under-counting assists.
            field_assist_6_plus = False
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
                            if putout_tracker <= _MAX_PUTOUT_SLOTS:
                                putout_dict[f"field_putout_{putout_tracker}"] = pid
                            else:
                                field_assist_6_plus = True
                            putout_tracker += 1
                        elif c == "f_assist":
                            if assist_tracker <= _MAX_ASSIST_SLOTS:
                                assist_dict[f"field_assist_{assist_tracker}"] = pid
                            else:
                                field_assist_6_plus = True
                            assist_tracker += 1
                        elif c == "f_throwing_error":
                            if throwing_error_tracker <= _MAX_THROWING_ERROR_SLOTS:
                                throwing_error_dict[f"throwing_error_{throwing_error_tracker}"] = (
                                    pid
                                )
                            else:
                                field_assist_6_plus = True
                            throwing_error_tracker += 1
                        elif c == "f_assist_of":
                            of_assist = pid
                        elif c == "f_fielded_ball":
                            fielded_by = pid
                        elif c == "f_fielding_error":
                            fielding_error = pid
                        elif c == "f_error_dropped_ball":
                            dropped_ball = pid
                    # Detect runner thrown out advancing on a batted ball
                    # (not stolen base, not the batter-runner).  The API sets
                    # isOut=True and movement.end="" for these plays.
                    if runner["movement"].get("isOut", False):
                        rid = runner["details"]["runner"]["id"]
                        is_sb_play = sb_attempt_2b or sb_attempt_3b or sb_attempt_home
                        is_batter = rid == batter
                        if not is_sb_play and not is_batter:
                            if rid == pre_runner_1b:
                                runner_1b_out_advancing = True
                            elif rid == pre_runner_2b:
                                runner_2b_out_advancing = True
                            elif rid == pre_runner_3b:
                                runner_3b_out_advancing = True
                        if rid == post_runner_1b:
                            post_runner_1b = ""
                        if rid == post_runner_2b:
                            post_runner_2b = ""
                        if rid == post_runner_3b:
                            post_runner_3b = ""
                        if i != runner["details"]["playIndex"]:
                            outs_after_pitch += 1
                        # A retired runner is done: he cannot also score or reach
                        # a base. Without this the `movement.end` branches below
                        # run anyway, and a feed that populates BOTH `isOut` and
                        # `end` for the same runner (gumbo normally carries the
                        # base in `outBase` and leaves `end` null, but it is not
                        # guaranteed for every event type) would re-seat the
                        # runner on the base we just cleared him from.
                        continue
                    if runner["movement"]["end"] == "score":
                        rid = runner["details"]["runner"]["id"]
                        runs += 1
                        if runner["details"].get("earned", False):
                            earned_runs += 1
                        if runner["details"].get("rbi", False):
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

            # Assemble raw dict using YOUR variable names — COLUMN_RENAME handles
            # the mapping to schema names in _build_row_dict().
            raw_row = {
                "game_pk": game_pk,
                "game_date": game_date,
                "venue_id": venue_id,
                "venue": venue_name,
                "home_id": home_team_id,
                "home_team": home_team,
                "away_id": away_team_id,
                "away_team": away_team,
                "home_manager_id": home_manager_id,
                "home_manager_name": home_manager_name,
                "away_manager_id": away_manager_id,
                "away_manager_name": away_manager_name,
                "inning": inning,
                "top_bot": top_bot,  # → inning_topbot via rename
                "at_bat_number": at_bat_number,
                "pitch_number": pitch_number,
                "pitcher": pitcher,
                "p_throws": pitcher_hand,
                "batter": batter,
                "stand": bat_side,
                "bat_hand": bat_hand,
                "home_score": home_score_before,
                "away_score": away_score_before,
                "bat_score": bat_score,
                "fld_score": field_score,
                "balls": balls,
                "strikes": strikes,
                "outs": outs,
                "pitch_code": pitch_code,  # → type
                "pitch_code_description": pitch_code_description,  # → description
                "pitch_type": pitch_type,
                "pitch_type_description": pitch_type_description,  # → pitch_name
                "event": event,
                "strike_zone_top": strike_zone_top,  # → sz_top
                "strike_zone_bottom": strike_zone_bottom,  # → sz_bot
                "start_speed": start_speed,  # → release_speed
                "end_speed": end_speed,
                "release_x": release_x,  # → release_pos_x
                "release_y": release_y,
                "release_z": release_z,
                "velocity_x": velocity_x,  # → vx0
                "velocity_y": velocity_y,
                "velocity_z": velocity_z,
                "acceleration_x": acceleration_x,  # → ax
                "acceleration_y": acceleration_y,
                "acceleration_z": acceleration_z,
                "pfx_x": pfx_x,
                "pfx_z": pfx_z,
                "p_x": p_x,  # → plate_x
                "p_z": p_z,
                "x": x,
                "y": y,
                "spin_rate": spin_rate,  # → release_spin_rate
                "spin_direction": spin_direction,  # → spin_axis
                "break_angle": break_angle,
                "break_length": break_length,
                "break_y": break_y,
                "break_vertical": break_vertical,
                "break_vertical_induced": break_vertical_induced,
                "break_horizontal": break_horizontal,
                "zone": zone,
                "extension": extension,  # → release_extension
                "launch_speed": launch_speed,
                "launch_angle": launch_angle,
                "total_distance": total_distance,  # → hit_distance_sc
                "coord_x": coord_x,  # → hc_x
                "coord_y": coord_y,  # → hc_y
                "spray_angle": spray_angle,
                "batted_ball_type": batted_ball_type,  # → bb_type
                "hit_location": hit_location,
                "play_description": play_description,  # → des
                "pre_play_runner_on_first": pre_runner_1b,  # → on_1b
                "pre_play_runner_on_second": pre_runner_2b,
                "pre_play_runner_on_third": pre_runner_3b,
                "post_play_runner_on_first": post_runner_1b,  # → post_on_1b
                "post_play_runner_on_second": post_runner_2b,
                "post_play_runner_on_third": post_runner_3b,
                "catcher": catcher,  # → fielder_2
                "first_base": first_base,  # → fielder_3
                "second_base": second_base,  # → fielder_4
                "third_base": third_base,  # → fielder_5
                "shortstop": shortstop,  # → fielder_6
                "left_field": left_field,  # → fielder_7
                "center_field": center_field,  # → fielder_8
                "right_field": right_field,  # → fielder_9
                "runs": runs,  # → runs_on_pitch
                "outs_on_pitch": outs_on_pitch,
                "rbis": rbis,  # → rbis_on_pitch
                "earned_runs": earned_runs,  # → earned_runs_on_pitch
                "runner_on_first_score": runner_on_first_score,  # → runner_1b_scored
                "runner_on_second_score": runner_on_second_score,
                "runner_on_third_score": runner_on_third_score,
                "runner_1b_out_advancing": runner_1b_out_advancing,
                "runner_2b_out_advancing": runner_2b_out_advancing,
                "runner_3b_out_advancing": runner_3b_out_advancing,
                "sb_attempt_2b": sb_attempt_2b,
                "sb_attempt_3b": sb_attempt_3b,
                "sb_attempt_home": sb_attempt_home,
                "sb_success_2b": sb_success_2b,
                "sb_success_3b": sb_success_3b,
                "sb_success_home": sb_success_home,
                "wild_pitch_passed_ball": wild_pitch_passed_ball,  # → passed_ball_wild_pitch
                "pinch_hitter": pinch_hitter,
                "pinch_runner": pinch_runner,
                "pitcher_sub": pitcher_sub,
                "defensive_sub": defensive_sub,
                "fielded_by": fielded_by,
                "fielding_error": fielding_error,
                "dropped_ball": dropped_ball,
                "of_assist": of_assist,
                **assist_dict,
                **putout_dict,
                **throwing_error_dict,
                "field_assist_6_plus": field_assist_6_plus,
            }

            pitch_rows.append(raw_row)

            # Reset the substitution flags now that this pitch has consumed them.
            # This is load-bearing: the scan above latches a flag on the event
            # where the substitution occurs, and this reset is what stops it
            # broadcasting to the rest of the plate appearance.
            pinch_hitter = pinch_runner = pitcher_sub = defensive_sub = False
            balls = play_event["count"]["balls"]
            strikes = play_event["count"]["strikes"]
            outs = play_event["count"]["outs"] + outs_after_pitch
            home_score_before = home_score_after
            away_score_before = away_score_after
            prev_half = top_bot
        home_score_before = play["result"].get("homeScore", home_score_after)
        away_score_before = play["result"].get("awayScore", away_score_after)

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


# SIM-441: the MLB schedule endpoint silently clamps any window wider than a year
# to startDate + 365 and still answers HTTP 200. Slice below that so nothing is
# lost, and leave headroom so a leap year cannot tip a slice over the limit.
_MAX_SCHEDULE_WINDOW_DAYS = 364

# SIM-441: how many games may fail back-to-back before a run aborts. One bad feed
# must be survivable across a 21,600-game sweep; an API outage or a schema
# mismatch must not be, because it would otherwise "succeed" having loaded almost
# nothing.
_CONSECUTIVE_FAILURE_LIMIT = int(os.environ.get("ETL_CONSECUTIVE_FAILURE_LIMIT", "5"))


def _date_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split an inclusive date range into <=_MAX_SCHEDULE_WINDOW_DAYS slices."""
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=_MAX_SCHEDULE_WINDOW_DAYS), end)
        chunks.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return chunks


_WIND_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*mph\s*,\s*(.+?)\s*$", re.IGNORECASE)


def _parse_wind(raw: Any) -> tuple[int | None, str | None]:
    """Split the MLB feed's single wind string into (speed_mph, direction).

    SIM-441. The feed exposes ONE formatted string under ``gameData.weather.wind``
    — e.g. ``"8 mph, L To R"``, ``"12 mph, Out To CF"``, ``"Calm"`` — but this
    loader read ``weather["speed"]`` and ``weather["direction"]``, keys the feed
    has never provided. Both columns were therefore NULL on every game ever
    loaded. (The two variables were also bound to the same dict, which is how the
    mistake survived review.)

    Returns ``(None, None)`` for an absent value, and ``(0, "Calm")`` for the
    literal ``"Calm"`` the feed uses on a windless day — that is a real
    observation, not missing data, and collapsing it to NULL would lose it.
    """
    if not raw or not isinstance(raw, str):
        return None, None
    text = raw.strip()
    if not text:
        return None, None
    if text.lower() == "calm":
        return 0, "Calm"
    m = _WIND_RE.match(text)
    if not m:
        # Unknown shape — keep the direction text rather than dropping the row's
        # only wind information, but do not invent a speed.
        log.debug("Unrecognised wind string %r; storing direction only", raw)
        return None, text[:50] or None
    return int(round(float(m.group(1)))), m.group(2)[:50]


def _map_game_status(gd: dict) -> str:
    """
    Maps the MLB API abstractGameState to the raw.games status enum.
    """
    abstract = gd.get("status", {}).get("abstractGameState", "Preview")
    coded = gd.get("status", {}).get("codedGameState", "")
    status_map = {
        "Preview": "Preview",
        "Live": "Live",
        "Final": "Final",
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
# Schedule filtering
# ---------------------------------------------------------------------------


def _schedule_game_is_final(game: dict) -> bool:
    """True iff a schedule game entry is a completed ('Final') game.

    The historical loader pulls pitch-by-pitch Statcast data, which only exists
    for games that have actually been played.  A season schedule for the CURRENT
    year includes future / in-progress games (``abstractGameState`` 'Preview' /
    'Live'); loading those wastes one fetch each (0 pitches) and inserts an empty
    ``raw.games`` stub.  Gating on Final also makes ``refresh_seasons`` a correct
    nightly incremental updater (it picks up games as they finish).
    """
    return game.get("status", {}).get("abstractGameState") == "Final"


def _build_starting_lineup_rows(game_pk: int, season: int, game_dict: dict) -> list[tuple]:
    """Build raw.game_lineups rows for the STARTING lineups from a feed/live boxscore (SIM-409).

    Reads ``liveData.boxscore.teams.{home,away}``: a player whose ``battingOrder``
    ends in ``00`` (``int(bo) % 100 == 0``) is the original starter in slot
    ``int(bo) // 100`` (1–9); the starting pitcher (``pitchers[0]``) is added with
    ``position_code='P'`` and a NULL ``batting_order`` when not in the batting
    order (AL / DH games). Substitutions (sequence > 1, pinch roles) are out of
    scope — this ingests the opening lineup the sim resolves from.

    Returns tuples in the raw.game_lineups insert column order:
    ``(game_pk, season, team_id, player_id, batting_order, position_code,
    is_starter, sequence)``.
    """
    gd = game_dict.get("gameData", {})
    box = game_dict.get("liveData", {}).get("boxscore", {}).get("teams", {})
    rows: list[tuple] = []
    for side in ("home", "away"):
        team_id = gd.get("teams", {}).get(side, {}).get("id")
        team_box = box.get(side, {})
        if team_id is None or not team_box:
            continue
        seen: set[int] = set()
        for pdata in team_box.get("players", {}).values():
            bo = pdata.get("battingOrder")
            if not bo:
                continue
            try:
                bo_int = int(bo)
            except (TypeError, ValueError):
                continue
            if bo_int % 100 != 0:
                continue  # a later-entering occupant of the slot, not the starter
            pid = pdata.get("person", {}).get("id")
            if pid is None:
                continue
            pos = (pdata.get("position") or {}).get("abbreviation") or "UT"
            rows.append((game_pk, season, int(team_id), int(pid), bo_int // 100, pos[:5], True, 1))
            seen.add(int(pid))
        # Starting pitcher — not in the batting order for AL/DH games.
        pitchers = team_box.get("pitchers") or []
        if pitchers and int(pitchers[0]) not in seen:
            rows.append((game_pk, season, int(team_id), int(pitchers[0]), None, "P", True, 1))
    return rows


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
            field_assist_6_plus,
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
            %(field_assist_6_plus)s,
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

        # SIM-090: lazily-initialised connection pool.  Created on first DB use
        # (see _ensure_pool) and shared across every game in a backfill run,
        # rather than opening/closing a fresh psycopg2 connection per round-trip.
        self._pool: psycopg2.pool.ThreadedConnectionPool | None = None
        # SIM-441: guards the lazy construction above (see _ensure_pool).
        self._pool_lock = threading.Lock()
        # SIM-441: circuit breaker for _dispatch_game — see its docstring.
        self._consecutive_failures = 0

    def _ensure_pool(self) -> psycopg2.pool.ThreadedConnectionPool:
        """Create the pool exactly once, on first DB access.

        ThreadedConnectionPool is used (over SimpleConnectionPool) so the loader
        stays safe if a caller ever fans games out across threads; for the
        single-threaded backfill it behaves identically.  Pool bounds come from
        ETL_DB_POOL_MIN / ETL_DB_POOL_MAX (resolved at import time).
        """
        # SIM-441: double-checked locking. The bare ``if self._pool is None``
        # was a check-then-set race — two threads could each build a pool, and
        # the loser's connections would leak (never returned, never closed)
        # because ``close()`` only ever shuts down the pool it can see. Harmless
        # while the loader is driven single-threaded, but the class advertises
        # ThreadedConnectionPool precisely so a caller MAY fan out.
        if self._pool is None:
            with self._pool_lock:
                if self._pool is None:
                    self._pool = psycopg2.pool.ThreadedConnectionPool(
                        minconn=ETL_DB_POOL_MIN,
                        maxconn=ETL_DB_POOL_MAX,
                        dsn=self.dsn,
                    )
        return self._pool

    @contextmanager
    def _get_conn(self):
        """Borrow a pooled connection for the duration of the ``with`` block.

        The connection is returned to the pool (``putconn``) on exit rather than
        closed, so the underlying socket is reused by the next round-trip.
        Existing transaction semantics are unchanged: callers still own
        commit/rollback exactly as before.
        """
        pool = self._ensure_pool()
        conn = pool.getconn()
        try:
            yield conn
        finally:
            pool.putconn(conn)

    def close(self) -> None:
        """Shut the pool down, closing every pooled connection.

        Safe to call multiple times and on a loader that never touched the DB.
        Call this once a backfill / load run is complete so file descriptors
        aren't held open.
        """
        if self._pool is not None:
            self._pool.closeall()
            self._pool = None

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

        return self._load_game(game_pk, season, batter_hand_cache, replace=False)

    def reload_game(
        self,
        game_pk: int,
        season: int,
        batter_hand_cache: dict | None = None,
        *,
        allow_shrink: bool = False,
    ) -> dict:
        """Re-fetch a game and OVERWRITE its existing ``raw.pitches`` rows.

        This is the corrective-ingest entry point.  ``load_game`` cannot repair
        already-loaded data: ``INSERT … ON CONFLICT (game_pk, at_bat_number,
        pitch_number) DO NOTHING`` discards every colliding row, so re-running it
        after a parser fix writes nothing at all.  ``reload_game`` deletes the
        game's rows and re-inserts the freshly-parsed ones inside a single
        transaction, so the game is never left half-deleted if the insert fails.

        Use this after any change to ``_fetch_game_pitches`` / ``_build_row_dict``
        / ``_validate_row`` that changes a column's VALUE.  The returned
        ``inserted`` count is measured against the table, so a no-op is visible.

        Also re-runs ``_ensure_prerequisites``, which re-upserts venues, teams,
        players, managers, the ``raw.games`` row and the starting lineups —
        superseding the old ``backfill_lineups_and_scores`` helper (removed:
        ``reload_game`` does everything it did, plus the pitch rows it could not
        touch).

        Refuses to shrink a game: if the re-parse yields fewer rows than are
        stored, the transaction is rolled back and :class:`ReloadShrinkError` is
        raised rather than committing the deletion.  Pass ``allow_shrink=True``
        only when the game genuinely has fewer pitches than the stored copy.
        """
        if batter_hand_cache is None:
            batter_hand_cache = {}
        return self._load_game(
            game_pk, season, batter_hand_cache, replace=True, allow_shrink=allow_shrink
        )

    def _load_game(
        self,
        game_pk: int,
        season: int,
        batter_hand_cache: dict,
        *,
        replace: bool,
        allow_shrink: bool = False,
    ) -> dict:
        """Shared body of :meth:`load_game` / :meth:`reload_game`."""
        log.info("%s game %s …", "Reloading" if replace else "Loading", game_pk)
        raw_rows, game_dict = _fetch_game_pitches(game_pk, batter_hand_cache)
        self._ensure_prerequisites(game_pk, game_dict)
        result = self._process_and_insert(
            game_pk, season, raw_rows, replace=replace, allow_shrink=allow_shrink
        )
        log.info(
            "  game %s: %d inserted, %d skipped (hard errors), %d flagged",
            game_pk,
            result["inserted"],
            result["skipped"],
            result["flagged"],
        )
        return result

    def load_date_range(
        self, start_date: date, end_date: date, *, reload: bool = False
    ) -> dict[str, int]:
        """Incremental loader — fetches all games between two dates.

        ``reload=True`` bypasses the ``_game_already_loaded`` skip and routes
        every game through :meth:`reload_game`, overwriting existing pitch rows.
        Use it to re-ingest a window after a parser fix; the default (False)
        keeps the cheap incremental behaviour.

        SIM-441 fixed two defects here:

        * **Silent truncation.** The MLB schedule endpoint clamps any window
          wider than 365 days to ``startDate + 365`` and still returns HTTP 200,
          so a multi-season call quietly loaded about one season and exited 0.
          The module's own header recipe
          (``load_date_range(date(2025,3,18), date.today())``) was broken by this.
          The window is now chunked into <=365-day slices and each slice's
          returned coverage is checked against what was asked for.
        * **No Final gate.** Unlike :meth:`refresh_seasons`, this method ingested
          in-progress games — and ``_game_already_loaded`` (a bare
          ``SELECT 1 … LIMIT 1``) then skipped them forever, leaving a permanently
          half-played game.
        """
        if end_date < start_date:
            raise ValueError(f"end_date {end_date} precedes start_date {start_date}")

        batter_hand_cache: dict[int, str] = {}
        summary = {"attempted": 0, "loaded": 0, "failed": 0, "skipped": 0, "rows_written": 0}

        for chunk_start, chunk_end in _date_chunks(start_date, end_date):
            params = {
                "sportId": 1,
                "gameTypes": GAME_TYPES,
                "startDate": chunk_start.strftime("%Y-%m-%d"),
                "endDate": chunk_end.strftime("%Y-%m-%d"),
            }
            schedule = _connect("https://statsapi.mlb.com/api/v1/schedule", params)["dates"]

            # Coverage check: a clamped or short response is otherwise
            # indistinguishable from "there were simply no games".
            returned = [d["date"] for d in schedule if d.get("date")]
            if returned:
                last = date.fromisoformat(max(returned))
                if last < chunk_end - timedelta(days=1):
                    log.warning(
                        "schedule for %s→%s returned nothing after %s — the endpoint "
                        "may have truncated the window; %d date entries received",
                        chunk_start,
                        chunk_end,
                        last,
                        len(schedule),
                    )

            for date_entry in schedule:
                log.info("Processing date %s", date_entry["date"])
                for game in date_entry["games"]:
                    if "rescheduleGameDate" in game or "resumeGameDate" in game:
                        continue
                    # Same Final gate refresh_seasons has: only completed games
                    # carry full pitch-by-pitch Statcast data, and a half-ingested
                    # in-progress game would be skipped forever afterwards.
                    if not _schedule_game_is_final(game):
                        continue
                    self._dispatch_game(
                        game["gamePk"],
                        int(date_entry["date"][:4]),
                        batter_hand_cache,
                        reload=reload,
                        summary=summary,
                    )

        log.info(
            "load_date_range %s→%s: %d attempted, %d loaded, %d already-loaded, "
            "%d failed, %d pitch rows written",
            start_date,
            end_date,
            summary["attempted"],
            summary["loaded"],
            summary["skipped"],
            summary["failed"],
            summary["rows_written"],
        )
        return summary

    def refresh_seasons(
        self,
        start_year: int = 2017,
        end_year: int | None = None,
        *,
        reload: bool = False,
    ) -> dict[str, int]:
        """
        Full historical backfill.  Mirrors refresh_plays() from the original
        script.  Skips games that are already fully loaded.

        ``reload=True`` bypasses the ``_game_already_loaded`` skip and routes
        every Final game through :meth:`reload_game`, overwriting its existing
        pitch rows.  This is the supported way to re-ingest a season (or all
        seasons) after a parser fix — the default path cannot repair loaded data
        because the INSERT carries ``ON CONFLICT … DO NOTHING``.
        """
        if end_year is None:
            end_year = datetime.today().year - 1  # don't include current season here

        summary = {"attempted": 0, "loaded": 0, "failed": 0, "skipped": 0, "rows_written": 0}

        for season in range(start_year, end_year + 1):
            log.info("=== Season %d ===", season)
            params = {
                "sportId": 1,
                "gameTypes": GAME_TYPES,
                "season": season,
            }
            schedule = _connect("https://statsapi.mlb.com/api/v1/schedule", params)["dates"]
            batter_hand_cache: dict[int, str] = {}

            for date_entry in schedule:
                log.info("  %s", date_entry["date"])
                for game in date_entry["games"]:
                    if "rescheduleGameDate" in game or "resumeGameDate" in game:
                        continue
                    # Only completed games have full pitch-by-pitch Statcast data.
                    # Skip future / in-progress / postponed games — otherwise the
                    # current season pulls thousands of unplayed games (0 pitches,
                    # one wasted fetch + an empty raw.games stub each). This also
                    # makes refresh_seasons a correct nightly incremental updater:
                    # each run picks up games that have newly become Final.
                    if not _schedule_game_is_final(game):
                        continue
                    self._dispatch_game(
                        game["gamePk"],
                        season,
                        batter_hand_cache,
                        reload=reload,
                        summary=summary,
                    )

        log.info(
            "refresh_seasons %d–%d: %d attempted, %d loaded, %d already-loaded, "
            "%d failed, %d pitch rows written",
            start_year,
            end_year,
            summary["attempted"],
            summary["loaded"],
            summary["skipped"],
            summary["failed"],
            summary["rows_written"],
        )
        return summary

    def _dispatch_game(
        self,
        game_pk: int,
        season: int,
        batter_hand_cache: dict,
        *,
        reload: bool,
        summary: dict[str, int],
    ) -> None:
        """Route one scheduled game to load/reload and tally the outcome.

        SIM-441: BOTH paths now contain per-game failures. The incremental path
        previously called ``load_game`` bare, and the parser is full of hard
        subscripts on third-party JSON (``pd_data["strikeZoneTop"]``,
        ``hd["trajectory"]``, ``defense["catcher"]["id"]``), so one malformed feed
        raised and unwound the whole run. Because ``scripts/nightly_ingest.sh``
        runs under ``set -eu`` with this as step 1 of 3, the profile rebuild and
        the FAISS tile build then never executed — and since the poison game
        genuinely has no pitch rows, ``_game_already_loaded`` correctly reported
        it as unloaded, so the chain re-wedged on the same game every night with
        nothing alerting.

        Containment is bounded, not blanket: ``_CONSECUTIVE_FAILURE_LIMIT``
        successive failures re-raise. That is the distinction that matters — "one
        bad game" must be survivable, but "the MLB API is down" or "the DB is
        rejecting every row" must still fail loudly rather than quietly producing
        a run that loaded almost nothing.
        """
        summary["attempted"] += 1
        try:
            if reload:
                result = self.reload_game(game_pk, season, batter_hand_cache)
            else:
                if self._game_already_loaded(game_pk):
                    summary["attempted"] -= 1
                    summary["skipped"] += 1
                    self._consecutive_failures = 0
                    return
                result = self.load_game(game_pk, season, batter_hand_cache)
        except Exception as exc:  # noqa: BLE001 — one bad feed must not kill the run
            summary["failed"] += 1
            self._consecutive_failures += 1
            log.error(
                "  %s failed for game %s (%d consecutive): %s",
                "reload" if reload else "load",
                game_pk,
                self._consecutive_failures,
                exc,
            )
            if self._consecutive_failures >= _CONSECUTIVE_FAILURE_LIMIT:
                raise RuntimeError(
                    f"aborting: {self._consecutive_failures} consecutive game failures "
                    f"(last was game {game_pk}: {exc}). This is a systemic failure — an "
                    f"API outage, a credential problem or a schema mismatch — not one bad "
                    f"feed. Fix the cause and re-run; already-loaded games are skipped."
                ) from exc
            return

        self._consecutive_failures = 0
        summary["loaded"] += 1
        # Carry the measured row count up: {attempted, loaded, failed} alone
        # cannot distinguish "re-wrote 300 pitches" from "wrote nothing", and a
        # sweep that reports 21,600 loaded / 0 rows is the failure mode the whole
        # reload path exists to make visible.
        summary["rows_written"] += int(result.get("inserted", 0))

    # NOTE: ``backfill_lineups_and_scores`` (SIM-409) was removed — it re-fetched
    # each Final game and re-ran ``_ensure_prerequisites`` to fill in lineups and
    # final scores on already-loaded games.  ``refresh_seasons(reload=True)``
    # does everything it did (it calls the same ``_ensure_prerequisites``) and
    # additionally rewrites the pitch rows, which the old helper could not touch.

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
        # SIM-409: pass the FULL game_dict so _ensure_game can read the linescore
        # + decisions (under liveData, not gameData), then ingest the lineups.
        self._ensure_game(game_pk, season, game_dict, managers)
        self._ensure_game_lineups(game_pk, season, game_dict)

    # --- 1. Venues ----------------------------------------------------------

    def _ensure_venue(self, venue_id: int, season: int) -> None:
        """
        Upserts raw.venues if the venue_id is not already present.
        Fetches full venue details (dimensions, surface, roof) from the
        MLB venues endpoint when the record is missing.
        """
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM raw.venues WHERE venue_id = %s AND season = %s", (venue_id, season)
            )
            if cur.fetchone() is not None:
                return  # already exists

        log.info("  Fetching missing venue %s, season %s", venue_id, season)

        # SIM-441: both scrapes used to loop the full MAX_API_RETRIES with NO
        # `break` on success — ~10 redundant Savant requests per new
        # (venue_id, season), roughly 3,000 across a 2017-2026 backfill where 300
        # would do. Worse, `resp` was reassigned every iteration, so a fetch that
        # succeeded on attempt 1 was DISCARDED if attempt 10 failed transiently,
        # and the method then raised despite having held valid data. The trailing
        # `if resp is None: raise` was a bare re-raise outside any except block
        # (it would have thrown `RuntimeError: No active exception`) and was dead
        # code besides. `_http_get` now returns the first successful response and
        # does not retry permanent 4xx at all.
        resp = _http_get(
            "https://baseballsavant.mlb.com/leaderboard/statcast-park-factors"
            f"?type=dimensions&year={season}&parks=All&fenceStatType=distance"
        )
        venue_data = _parse_savant_embedded_json(resp.text, "var data = [", "}];", 2)

        dimensions = None
        for venue in venue_data:
            if venue.get("venue_id") != venue_id:
                continue
            dimensions = venue
            break

        if dimensions is None:
            resp = _http_get(
                f"https://baseballsavant.mlb.com/leaderboard/statcast-venue?venueId={venue_id}"
            )
            payload = _parse_savant_embedded_json(resp.text, "var data = {", "]};", 2)
            venues = payload.get("venues") if isinstance(payload, dict) else None
            if not venues:
                raise SavantScrapeError(
                    f"statcast-venue payload for venue {venue_id} has no 'venues' entries "
                    "— the page shape has changed"
                )
            dimensions = venues[0]

        data = _connect(
            f"https://statsapi.mlb.com/api/v1/venues/{venue_id}",
            params={"hydrate": "location,fieldInfo,timezone"},
        )
        v = data["venues"][0]

        field = v.get("fieldInfo", {})
        location = v.get("location", {})

        # Surface: API returns "Artificial Turf" or "Grass" variants
        surface_raw = field.get("turfType", "Grass")
        surface = (
            "Turf"
            if "turf" in surface_raw.lower() or "artificial" in surface_raw.lower()
            else "Grass"
        )

        # Roof type mapping
        roof_raw = field.get("roofType", "Open")
        roof_map = {"Open": "Open", "Retractable": "Retractable", "Indoor": "Dome", "Dome": "Dome"}
        roof_type = roof_map.get(roof_raw, "Open")

        # Foul territory: not directly in API; default to Medium
        foul_map = {"Large": "Large", "Medium": "Medium", "Small": "Small"}
        foul_map.get(field.get("leftLine", ""), "Medium")

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
                        dimensions.get("venu_name_short", " "),
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

    def _ensure_teams(self, home_team_id: int, away_team_id: int, season: int, gd: dict) -> None:
        """
        Upserts raw.teams for any team not already in the DB.
        All required data is present in the game_dict — no extra API call needed.
        """
        needed = {home_team_id, away_team_id}
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT team_id FROM raw.teams WHERE season = %s AND team_id = ANY(%s)",
                (
                    season,
                    list(needed),
                ),
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

            rows.append(
                (tid, season, t["name"], t["abbreviation"], league_code, division, venue_id)
            )

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

        SIM-441 rewrote this method. It previously had three linked defects:

        * **It fabricated handedness.** Any exception from ``/people/{pid}`` fell
          back to ``bats, throws = "R", "R"`` and stored the full name in BOTH
          ``first_name`` and ``last_name``. The realistic trigger was not a
          network blip (``_http_get`` already retries) but a 200 whose ``people``
          list is empty, which raises ``IndexError`` with no retry. A quieter
          path defaulted to ``'R'`` on a *successful* response merely missing
          ``batSide``, with no warning at all.
        * **It was write-once.** It returned early when the ``player_id`` already
          existed, and ``raw.players`` has no season column — so a value captured
          in the 2017 pass applied to all ten seasons.
        * **Its upsert was unreachable.** Every id handed to the INSERT had been
          proven absent moments earlier, so ``ON CONFLICT … DO UPDATE`` never
          fired and ``full_name`` was never refreshed.

        A wrong hand was therefore silent, permanent, and unrepairable: the repo
        has no ``UPDATE raw.players`` anywhere. It matters because
        ``simulation/lineup_resolver`` reads ``bats``/``throws`` straight into the
        full-pool sampler's only hard pre-filter, while the engines derive
        handedness from ``raw.pitches`` — so the two disagreed about the same
        player in the same simulated game.

        Now: prefer the FULL person record the feed already carries at
        ``gameData.players.ID<pid>`` (no extra HTTP call at all), fall back to the
        boxscore stub, then to ``/people/{pid}``; skip — never invent — a player
        whose handedness cannot be established; and upsert every player in the
        boxscore so a previously-wrong row is corrected on the next reload.
        """
        boxscore = game_dict.get("liveData", {}).get("boxscore", {}).get("teams", {})
        # The feed's own person dictionary: full records, already fetched.
        game_people = game_dict.get("gameData", {}).get("players", {}) or {}

        players_raw: dict[int, dict] = {}
        for side in ("home", "away"):
            for _key, pdata in boxscore.get(side, {}).get("players", {}).items():
                pid = pdata["person"]["id"]
                players_raw[pid] = pdata

        if not players_raw:
            return

        rows: list[tuple] = []
        skipped: list[int] = []
        for pid, pdata in players_raw.items():
            # Merge, most-complete first: gameData.players > boxscore stub.
            person: dict[str, Any] = {}
            person.update(game_people.get(f"ID{pid}", {}) or {})
            for k, v in (pdata.get("person") or {}).items():
                person.setdefault(k, v)

            bats = (person.get("batSide") or {}).get("code")
            throws = (person.get("pitchHand") or {}).get("code")

            if not bats or not throws:
                # Last resort: the dedicated person endpoint. A failure here is
                # NOT papered over — the player is skipped so a later reload can
                # pick him up with real data.
                try:
                    people = _connect(f"https://statsapi.mlb.com/api/v1/people/{pid}").get(
                        "people", []
                    )
                    if not people:
                        raise ValueError("people[] was empty")
                    person = {**people[0], **{k: v for k, v in person.items() if v}}
                    bats = (person.get("batSide") or {}).get("code")
                    throws = (person.get("pitchHand") or {}).get("code")
                except Exception as exc:  # noqa: BLE001 — skip, never fabricate
                    log.warning("People API failed for player %s: %s", pid, exc)

            if not bats or not throws:
                skipped.append(pid)
                continue

            name = person.get("fullName") or ""
            first = person.get("firstName") or ""
            last = person.get("lastName") or ""
            # SIM-441: `position` in the boxscore is the player's PRIMARY position,
            # not the slot he happened to fill this game, so it is safe to store.
            # The default is 'UT' (utility), not 'P' — defaulting to pitcher
            # silently asserted a fact about anyone whose position was absent, and
            # the sibling helper at _build_starting_lineup_rows already uses 'UT'.
            position = (pdata.get("position") or {}).get("abbreviation") or "UT"

            rows.append(
                (
                    pid,
                    name,
                    first or name,
                    last or name,
                    person.get("birthDate"),
                    bats,
                    throws,
                    position,
                    _parse_height(person.get("height")),
                    person.get("weight"),
                    person.get("mlbDebutDate"),
                )
            )

        if skipped:
            log.warning(
                "  %d player(s) skipped — handedness could not be established, and "
                "guessing it would silently mis-draw every PA they take in /simulate: %s",
                len(skipped),
                skipped,
            )
        if not rows:
            return

        with self._get_conn() as conn:
            try:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_batch(
                        cur,
                        """
                        INSERT INTO raw.players (
                            player_id, full_name, first_name, last_name,
                            birth_date, bats, throws, primary_position,
                            height_inches, weight_lbs, mlb_debut_date
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (player_id) DO UPDATE SET
                            full_name        = EXCLUDED.full_name,
                            first_name       = EXCLUDED.first_name,
                            last_name        = EXCLUDED.last_name,
                            bats             = EXCLUDED.bats,
                            throws           = EXCLUDED.throws,
                            primary_position = EXCLUDED.primary_position,
                            birth_date       = COALESCE(EXCLUDED.birth_date,     raw.players.birth_date),
                            height_inches    = COALESCE(EXCLUDED.height_inches,  raw.players.height_inches),
                            weight_lbs       = COALESCE(EXCLUDED.weight_lbs,     raw.players.weight_lbs),
                            mlb_debut_date   = COALESCE(EXCLUDED.mlb_debut_date, raw.players.mlb_debut_date),
                            updated_at       = NOW()
                        """,
                        rows,
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        # --- 4. Managers --------------------------------------------------------

    def _ensure_managers(
        self, managers: dict, home_team_id: int, away_team_id: int, season: int, game_date: str
    ) -> None:
        """
        Upserts raw.managers for the home and away managers of this game.

        SIM-441: the PK is ``(manager_id, season)`` — the old docstring said
        ``(manager_id, team_id, season_start)``, which is not what the DDL
        declares and not what the ``ON CONFLICT`` targets.

        ``season_end`` semantics: NULL means "currently the manager for this
        team" (that is what ``idx_managers_active`` indexes). So on conflict we
        deliberately reset ``season_end = NULL`` — seeing a manager in the dugout
        again is exactly the evidence that he is active, which is how a return
        from suspension or other missed time is recorded. The previous clause
        assigned ``season_end = EXCLUDED.season_end`` where ``season_end`` was not
        even in the INSERT column list, so it wrote NULL by accident while
        silently failing to record ``season_start``, the value actually supplied.
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
        if managers["home_manager_id"] != "":
            check_managers.append(managers["home_manager_id"])
        if managers["away_manager_id"] != "":
            check_managers.append(managers["away_manager_id"])
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    SELECT manager_id, season
                    FROM raw.managers
                    WHERE season = %s AND manager_id = ANY(%s)
                    """,
                (season, check_managers),
            )
            existing = {(r[0], r[1]) for r in cur.fetchall()}

        missing = [
            (mid, name, tid) for mid, name, tid in candidates if (mid, season) not in existing
        ]
        if not missing:
            return

        log.info("  Inserting missing managers: %s", [(m[0], m[2]) for m in missing])
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                for mid, name, tid in missing:
                    # SIM-441: find the CURRENTLY-ACTIVE incumbent to hand off from.
                    #
                    # This probe used to have no ORDER BY, no LIMIT and no
                    # `season_end IS NULL` predicate, with a bare fetchone() — so
                    # with three managers in one season it closed an arbitrary
                    # one, clobbering a correct handoff date and leaving the true
                    # predecessor's stint open forever. `season_end IS NULL` is
                    # the documented meaning of "currently active"
                    # (idx_managers_active), so only such a row may be closed,
                    # and the most recent stint is the one being replaced.
                    cur.execute(
                        """
                        SELECT manager_id
                        FROM   raw.managers
                        WHERE  manager_id <> %s
                          AND  season     = %s
                          AND  team_id    = %s
                          AND  season_end IS NULL
                        ORDER BY season_start DESC NULLS LAST
                        LIMIT 1
                        """,
                        (mid, season, tid),
                    )
                    m = cur.fetchone()
                    if m is not None:
                        cur.execute(
                            """
                            UPDATE raw.managers
                               SET season_end = %s,
                                   updated_at = NOW()
                             WHERE manager_id = %s
                               AND season     = %s
                               AND team_id    = %s
                            """,
                            (date.fromisoformat(game_date), m[0], season, tid),
                        )
                        cur.execute(
                            """
                            INSERT INTO raw.managers
                                (manager_id, season, full_name, team_id, season_start)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (manager_id, season) DO UPDATE SET
                                full_name    = EXCLUDED.full_name,
                                team_id      = EXCLUDED.team_id,
                                season_start = COALESCE(raw.managers.season_start,
                                                        EXCLUDED.season_start),
                                season_end   = NULL,
                                updated_at   = NOW()
                            """,
                            (mid, season, name, tid, date.fromisoformat(game_date)),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO raw.managers
                                (manager_id, season, full_name, team_id)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (manager_id, season) DO UPDATE SET
                                full_name  = EXCLUDED.full_name,
                                team_id    = EXCLUDED.team_id,
                                season_end = NULL,
                                updated_at = NOW()
                            """,
                            (mid, season, name, tid),
                        )
            conn.commit()

        # --- 5. Game record -----------------------------------------------------

    def _ensure_game(self, game_pk: int, season: int, game_dict: dict, managers: dict) -> None:
        """
        Upserts raw.games.  Called after venues/teams/managers are guaranteed
        to exist so all FKs resolve cleanly.

        For completed games, also populates final score, inning scores, and
        pitcher W/L/S. SIM-409: reads the linescore + decisions from
        ``liveData`` (previously read from ``gameData``, where they don't exist,
        so final scores were always NULL). The INSERT ... ON CONFLICT DO UPDATE
        always runs (no score-blind early return) so a re-fetch of an
        already-loaded game backfills its final score.
        """
        gd = game_dict["gameData"]
        live = game_dict.get("liveData", {})

        game_date = gd["datetime"]["officialDate"]
        venue_id = gd["venue"]["id"]
        home_team_id = gd["teams"]["home"]["id"]
        away_team_id = gd["teams"]["away"]["id"]
        game_type = gd.get("game", {}).get("type", "R")
        status = _map_game_status(gd)

        # Final score — present for completed games (under liveData.linescore).
        linescore = live.get("linescore", {})
        home_score = linescore.get("teams", {}).get("home", {}).get("runs")
        away_score = linescore.get("teams", {}).get("away", {}).get("runs")
        home_hits = linescore.get("teams", {}).get("home", {}).get("hits")
        away_hits = linescore.get("teams", {}).get("away", {}).get("hits")
        home_errors = linescore.get("teams", {}).get("home", {}).get("errors")
        away_errors = linescore.get("teams", {}).get("away", {}).get("errors")
        innings = linescore.get("currentInning")

        # Inning-by-inning scores as JSONB
        innings_data = linescore.get("innings", [])
        inning_scores = (
            {
                "home": [i.get("home", {}).get("runs") for i in innings_data],
                "away": [i.get("away", {}).get("runs") for i in innings_data],
            }
            if innings_data
            else None
        )

        # Winning / losing / save pitcher (under liveData.decisions, SIM-409).
        decisions = live.get("decisions", {})
        winning_pid = decisions.get("winner", {}).get("id")
        losing_pid = decisions.get("loser", {}).get("id")
        save_pid = decisions.get("save", {}).get("id")

        # Weather (this one IS under gameData).
        weather = gd.get("weather", {})
        wind_speed, wind_direction = _parse_wind(weather.get("wind"))

        log.info("  Upserting game record %s", game_pk)
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
                    -- SIM-440: refresh ALL 24 non-key columns on conflict.
                    --
                    -- This clause used to cover 6 of 25. Everything else was
                    -- computed correctly above, bound into the INSERT, and then
                    -- silently discarded on every re-fetch — so a game loaded
                    -- once could never have its innings, hits, errors, inning
                    -- scores, weather, venue or manager ids corrected, on any
                    -- code path in the repo.
                    --
                    -- Two update policies, deliberately different:
                    --
                    --   EXCLUDED.x            (unconditional overwrite) for the
                    --     identity/classification columns this loader is the
                    --     AUTHORITY for. It reads gameData.datetime.officialDate;
                    --     the live pipeline writes the UTC `gameDate` instead, so
                    --     a CT/MT/PT night game it created is stamped with the
                    --     NEXT calendar day. COALESCE would preserve that wrong
                    --     value forever, because it is wrong but not NULL.
                    --
                    --   COALESCE(EXCLUDED.x, raw.games.x)  for enrichment that
                    --     only exists once a game is Final. A scheduled or
                    --     in-progress payload carries NULLs there, and must never
                    --     blank out a good stored value.
                    ON CONFLICT (game_pk) DO UPDATE SET
                        season             = EXCLUDED.season,
                        game_date          = EXCLUDED.game_date,
                        game_type          = EXCLUDED.game_type,
                        status             = EXCLUDED.status,
                        venue_id           = COALESCE(EXCLUDED.venue_id,           raw.games.venue_id),
                        home_team_id       = COALESCE(EXCLUDED.home_team_id,       raw.games.home_team_id),
                        away_team_id       = COALESCE(EXCLUDED.away_team_id,       raw.games.away_team_id),
                        home_manager_id    = COALESCE(EXCLUDED.home_manager_id,    raw.games.home_manager_id),
                        away_manager_id    = COALESCE(EXCLUDED.away_manager_id,    raw.games.away_manager_id),
                        home_score_final   = COALESCE(EXCLUDED.home_score_final,   raw.games.home_score_final),
                        away_score_final   = COALESCE(EXCLUDED.away_score_final,   raw.games.away_score_final),
                        innings_played     = COALESCE(EXCLUDED.innings_played,     raw.games.innings_played),
                        home_hits          = COALESCE(EXCLUDED.home_hits,          raw.games.home_hits),
                        away_hits          = COALESCE(EXCLUDED.away_hits,          raw.games.away_hits),
                        home_errors        = COALESCE(EXCLUDED.home_errors,        raw.games.home_errors),
                        away_errors        = COALESCE(EXCLUDED.away_errors,        raw.games.away_errors),
                        inning_scores      = COALESCE(EXCLUDED.inning_scores,      raw.games.inning_scores),
                        winning_pitcher_id = COALESCE(EXCLUDED.winning_pitcher_id, raw.games.winning_pitcher_id),
                        losing_pitcher_id  = COALESCE(EXCLUDED.losing_pitcher_id,  raw.games.losing_pitcher_id),
                        save_pitcher_id    = COALESCE(EXCLUDED.save_pitcher_id,    raw.games.save_pitcher_id),
                        weather_temp       = COALESCE(EXCLUDED.weather_temp,       raw.games.weather_temp),
                        weather_condition  = COALESCE(EXCLUDED.weather_condition,  raw.games.weather_condition),
                        wind_speed         = COALESCE(EXCLUDED.wind_speed,         raw.games.wind_speed),
                        wind_direction     = COALESCE(EXCLUDED.wind_direction,     raw.games.wind_direction),
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
                        wind_speed,
                        wind_direction,
                    ),
                )
            conn.commit()

    def _ensure_game_lineups(self, game_pk: int, season: int, game_dict: dict) -> None:
        """Insert the starting-lineup rows into raw.game_lineups (SIM-409).

        Idempotent (``ON CONFLICT DO NOTHING`` on the natural key) so a re-fetch
        of an already-loaded game does not duplicate rows. The starting lineups
        are what :mod:`simulation.lineup_resolver` reads to build the opening
        ``GameState`` for ``/simulate``.
        """
        rows = _build_starting_lineup_rows(game_pk, season, game_dict)
        if not rows:
            return
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO raw.game_lineups
                    (game_pk, season, team_id, player_id, batting_order,
                     position_code, is_starter, sequence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (game_pk, team_id, player_id, sequence) DO NOTHING
                """,
                rows,
            )
            conn.commit()
        log.info("  Inserted %d starting-lineup rows for game %s", len(rows), game_pk)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _game_already_loaded(self, game_pk: int) -> bool:
        """True if this game has been ingested and should be skipped.

        SIM-441: this used to be `SELECT 1 FROM raw.pitches … LIMIT 1` alone,
        which cannot distinguish "never loaded" from "loaded and legitimately
        produced zero pitch rows" — a game whose every row hard-errors, or one
        the feed simply has no pitches for. Such a game was re-fetched and
        re-processed on EVERY nightly run, forever, appending a duplicate error
        set each time and never making progress. A terminal
        ``raw.etl_game_ingest`` record now also counts as loaded.

        Deliberately NOT consulted by the reload path: `reload=True` bypasses
        this method entirely, which is the escape hatch after a parser fix.
        """
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM raw.pitches WHERE game_pk = %s LIMIT 1", (game_pk,))
            if cur.fetchone() is not None:
                return True
            cur.execute(
                """
                SELECT 1 FROM raw.etl_game_ingest
                 WHERE game_pk = %s AND outcome IN ('loaded', 'empty')
                 LIMIT 1
                """,
                (game_pk,),
            )
            return cur.fetchone() is not None

    def _process_and_insert(
        self,
        game_pk: int,
        season: int,
        raw_rows: list[dict[str, Any]],
        *,
        replace: bool = False,
        allow_shrink: bool = False,
    ) -> dict[str, int]:
        """
        Validates, builds, and batch-inserts all pitch rows for a game.
        Returns count summary.

        ``replace=True`` (used by :meth:`reload_game`) deletes the game's
        existing ``raw.pitches`` rows in the same transaction as the insert, so
        corrected parser output actually overwrites the old values instead of
        being discarded by ``ON CONFLICT … DO NOTHING``.

        SIM-093: Hard-error rows are now persisted to raw.etl_errors as well
        as logged.  Without this, skipped rows had no audit trail and there
        was no way to find which pitches a re-ingest would need to recover.
        """
        to_insert: list[dict[str, Any]] = []
        hard_errors: list[tuple[int, int, list[str]]] = []  # (at_bat, pitch, errors)
        flagged_count = 0

        for raw_row in raw_rows:
            row = _build_row_dict(raw_row, game_pk, season, raw_row.get("game_date", ""))
            vr = _validate_row(row)

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
                        game_pk,
                        row.get("at_bat_number"),
                        row.get("pitch_number"),
                        w,
                    )

            to_insert.append(row)

        if hard_errors:
            log.error(
                "  game %s: %d rows skipped due to hard validation errors:",
                game_pk,
                len(hard_errors),
            )
            for ab, pitch_num, errs in hard_errors:
                log.error("    ab=%s pitch_number=%s: %s", ab, pitch_num, "; ".join(errs))

        # SIM-441: the ledger is written INSIDE the pitch transaction (see
        # _batch_insert). It used to commit first, on its own connection, which
        # made it structurally blind to insert-path losses: if the pitch insert
        # then failed and rolled back, the errors survived describing a game that
        # had not been written, and a rolled-back reload left orphan rows behind.
        inserted = self._batch_insert(
            to_insert,
            replace_game_pk=game_pk if replace else None,
            allow_shrink=allow_shrink,
            error_rows=hard_errors,
            season=season,
            game_pk=game_pk,
        )
        self._log_freshness(game_pk, to_insert)

        return {
            "inserted": inserted,
            "skipped": len(hard_errors),
            "flagged": flagged_count,
        }

    @staticmethod
    def _write_error_ledger(
        cur,
        game_pk: int,
        errors: list[tuple[int | None, int | None, list[str]]],
    ) -> None:
        """SIM-093/SIM-441: one raw.etl_errors row per skipped pitch, idempotently.

        Runs on the CALLER's cursor so it shares the pitch-insert transaction.

        The insert is now ``ON CONFLICT DO UPDATE`` against the natural key
        (migration 0017). Without it, a game whose rows ALL hard-error writes no
        pitch rows, so ``_game_already_loaded`` stayed False, so the nightly job
        re-fetched and re-processed that game every night and appended a full
        duplicate error set each time — roughly 54,000 identical rows per season
        for one broken game, and any consumer sizing a recovery from
        ``COUNT(*)`` was wrong by the number of nights elapsed.
        """
        if not errors:
            return
        sql = """
            INSERT INTO raw.etl_errors
                (game_pk, at_bat_number, pitch_number, error_type, error_messages)
            VALUES (%s, %s, %s, 'HARD', %s)
            ON CONFLICT (game_pk, at_bat_number, pitch_number, error_type) DO UPDATE SET
                error_messages = EXCLUDED.error_messages,
                created_at     = NOW()
        """
        psycopg2.extras.execute_batch(
            cur,
            sql,
            [(game_pk, ab, pitch_num, list(msgs)) for (ab, pitch_num, msgs) in errors],
        )

    @staticmethod
    def _write_ingest_outcome(
        cur,
        game_pk: int,
        season: int,
        *,
        pitch_rows: int,
        skipped_rows: int,
    ) -> None:
        """SIM-441: record the terminal per-game ETL outcome.

        ``_game_already_loaded`` keys on pitch rows existing, which cannot
        distinguish "never loaded" from "loaded and legitimately produced zero
        pitch rows" (every row hard-errored, or the feed genuinely has no
        pitches). Without this record such a game is re-fetched and re-processed
        every single night, forever, with nothing ever changing.

        ``outcome='empty'`` is what stops that. ``reload_game`` /
        ``reload=True`` ignore this table by design — that is the escape hatch
        once a parser fix makes the game loadable.
        """
        outcome = "loaded" if pitch_rows > 0 else "empty"
        cur.execute(
            """
            INSERT INTO raw.etl_game_ingest
                (game_pk, season, outcome, pitch_rows, skipped_rows)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (game_pk) DO UPDATE SET
                season       = EXCLUDED.season,
                outcome      = EXCLUDED.outcome,
                pitch_rows   = EXCLUDED.pitch_rows,
                skipped_rows = EXCLUDED.skipped_rows,
                attempts     = raw.etl_game_ingest.attempts + 1,
                last_error   = NULL,
                updated_at   = NOW()
            """,
            (game_pk, season, outcome, pitch_rows, skipped_rows),
        )

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
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (since,))
            return [r[0] for r in cur.fetchall()]

    def _batch_insert(
        self,
        rows: list[dict[str, Any]],
        *,
        replace_game_pk: int | None = None,
        allow_shrink: bool = False,
        error_rows: list[tuple[int | None, int | None, list[str]]] | None = None,
        season: int | None = None,
        game_pk: int | None = None,
    ) -> int:
        """Insert rows in BATCH_SIZE chunks.  Returns rows ACTUALLY written.

        SIM-441: this is now the SINGLE transaction for one game's write — the
        optional DELETE, the pitch rows, the ``raw.etl_errors`` ledger and the
        ``raw.etl_game_ingest`` outcome all commit or roll back together."

        ``replace_game_pk``
            When supplied, every existing ``raw.pitches`` row for that game is
            DELETEd first, in the SAME transaction as the inserts.  This is the
            only supported way to correct already-ingested pitch data: the
            INSERT carries ``ON CONFLICT … DO NOTHING``, so without the delete a
            re-ingest silently writes nothing.  Delete + insert commit together
            or roll back together — a failed reload never leaves a game
            half-deleted.

        The return value is measured, not assumed.  ``execute_batch`` cannot
        report an accurate rowcount (``cur.rowcount`` reflects only the final
        page), and the previous implementation returned ``len(rows)`` — the
        number of rows *attempted*.  With ``DO NOTHING`` that number was
        indistinguishable from a completely discarded re-ingest, which is what
        made a corrective re-run look like a success while writing nothing.  We
        therefore bracket the insert with an exact COUNT on the affected game.
        """
        if not rows and replace_game_pk is None and not error_rows and season is None:
            return 0

        # Which game this batch belongs to. Prefer the pk the caller states
        # outright; inferring it from the rows is only a fallback for a caller
        # that does not pass one.
        #
        # SIM-441 fix: this used to infer ONLY from ``rows``. When every row of a
        # game hard-errors, ``rows`` is empty, so the inference produced None and
        # BOTH the error ledger and the ingest-outcome write below were silently
        # skipped — for exactly the games that most need an audit trail. That was
        # a regression (the old ``_log_etl_errors`` call passed game_pk
        # explicitly) and it also made ``outcome='empty'`` unreachable on the
        # non-reload path, defeating the whole point of raw.etl_game_ingest.
        game_pks = {r.get("game_pk") for r in rows}
        count_pk: int | None = replace_game_pk or game_pk
        if count_pk is None and len(game_pks) == 1:
            count_pk = next(iter(game_pks))

        count_sql = "SELECT COUNT(*) FROM raw.pitches WHERE game_pk = %s"
        deleted = 0

        with self._get_conn() as conn:
            try:
                with conn.cursor() as cur:
                    if replace_game_pk is not None:
                        cur.execute(
                            "DELETE FROM raw.pitches WHERE game_pk = %s",
                            (replace_game_pk,),
                        )
                        deleted = cur.rowcount or 0
                        log.info(
                            "  game %s: deleted %d existing pitch rows before reload",
                            replace_game_pk,
                            deleted,
                        )

                    def _count() -> int | None:
                        """COUNT(*) for the game, or None if it can't be read."""
                        if count_pk is None:
                            return None
                        cur.execute(count_sql, (count_pk,))
                        row = cur.fetchone()
                        return row[0] if row else None

                    before = _count()

                    for offset in range(0, len(rows), BATCH_SIZE):
                        batch = rows[offset : offset + BATCH_SIZE]
                        psycopg2.extras.execute_batch(cur, self.INSERT_SQL, batch)

                    after = _count()
                    if before is None or after is None:
                        # No single game to bracket (or the count was unreadable):
                        # fall back to the attempted count rather than reporting 0.
                        inserted = len(rows)
                    else:
                        inserted = after - before

                    # Shrink guard.  A reload that writes back FEWER rows than it
                    # deleted is data loss, not a reload — and it is reachable
                    # without any exception: _fetch_game_pitches returns
                    # ([], game_dict) for an HTTP-200 feed whose allPlays is empty
                    # or pitch-less (a postponed/suspended game, a feed blip, or a
                    # not-yet-played game reached through load_date_range, which
                    # has no Final gate).  Left unguarded, a sweep would delete a
                    # complete game and commit nothing in its place.  Raising here
                    # lands in the except below, which rolls the DELETE back.
                    if replace_game_pk is not None and not allow_shrink and inserted < deleted:
                        raise ReloadShrinkError(
                            f"reload of game {replace_game_pk} would delete {deleted} pitch "
                            f"rows and write back only {inserted}; rolled back. Re-run once "
                            f"the feed is healthy, or pass allow_shrink=True if the game "
                            f"genuinely has fewer pitches now."
                        )

                    # Ledger + outcome, in this same transaction.
                    if error_rows and count_pk is not None:
                        self._write_error_ledger(cur, count_pk, error_rows)
                    if count_pk is not None and season is not None:
                        self._write_ingest_outcome(
                            cur,
                            count_pk,
                            season,
                            pitch_rows=inserted,
                            skipped_rows=len(error_rows or []),
                        )

                conn.commit()
            except Exception:
                conn.rollback()
                raise

        if rows and inserted < len(rows):
            if replace_game_pk is not None:
                # The DELETE ran first, so "already present" is impossible here:
                # a shortfall means the batch itself carried duplicate
                # (game_pk, at_bat_number, pitch_number) keys, i.e. a parser bug.
                log.warning(
                    "  game %s: %d of %d parsed rows collided WITHIN the batch and were "
                    "dropped by ON CONFLICT — duplicate (at_bat_number, pitch_number) keys "
                    "from _fetch_game_pitches.",
                    count_pk,
                    len(rows) - inserted,
                    len(rows),
                )
            else:
                log.warning(
                    "  game %s: %d of %d rows were discarded by ON CONFLICT DO NOTHING "
                    "(rows already present). Use reload_game() to overwrite them.",
                    count_pk,
                    len(rows) - inserted,
                    len(rows),
                )

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
        batter_ids = {r["batter"] for r in rows if r.get("batter")}

        upsert_sql = """
            INSERT INTO raw.etl_data_freshness (entity_type, entity_id, last_game_pk, last_date)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (entity_type, entity_id) DO UPDATE
                SET last_game_pk = EXCLUDED.last_game_pk,
                    last_date    = EXCLUDED.last_date,
                    updated_at   = NOW()
            WHERE EXCLUDED.last_date > raw.etl_data_freshness.last_date;
        """
        entries = [("pitcher", pid, game_pk, game_date) for pid in pitcher_ids] + [
            ("batter", bid, game_pk, game_date) for bid in batter_ids
        ]

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
        with self._get_conn() as conn, conn.cursor() as cur:
            if game_pk:
                cur.execute(
                    """
                        SELECT pitcher, COUNT(*) AS flagged_pitches
                        FROM raw.pitches
                        WHERE data_quality_flag = TRUE AND game_pk = %s
                        GROUP BY pitcher ORDER BY flagged_pitches DESC
                        """,
                    (game_pk,),
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
            log.info("  %s -> %s flagged rows", r[0], r[1])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
# NOTE (SIM-083): FRESHNESS_TABLE_DDL was removed from this file.
# The raw.etl_data_freshness CREATE TABLE DDL now lives in:
#   db/schemas/01_postgres_schema.sql
# Applied via Alembic migration 0003 (db/migrations/versions/0003_*.py).
# SIM-090: connection pooling — see HistoricalDataLoader._ensure_pool / .close().
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = HistoricalDataLoader("postgresql://baseball_user:baseball_pass@db:5432/baseball_sim")
    try:
        loader.refresh_seasons()
    finally:
        # SIM-090: tear the pool down so all pooled sockets are released.
        loader.close()
