"""The historical loader: MLB Stats API games into Postgres ``raw.*``.

``HistoricalDataLoader`` fetches each game's live feed, writes the reference
rows the game needs, then its pitch rows into ``raw.pitches`` and its non-pitch
events into ``raw.play_events``. The row building, the validation, the
venues, the schedule helpers, the reference rows and the writes live in
the other modules of ``ingest/games/``. This module keeps the HTTP plumbing, the feed
parser ``_fetch_game_pitches`` and the class, beside the names the tests patch
(``_connect``, ``_http_get``, ``_fetch_game_pitches``).

The flow
--------
  MLB Stats API
       │
       ▼
  _fetch_game_pitches()  ← fetches feed/live + coaches; returns (pitch_rows, game_dict)
       │
       ▼
  _ensure_prerequisites()  ← every foreign-key parent exists before raw.pitches
       │   One query per table finds the missing ids; only those are fetched.
       │   The order follows the foreign keys:
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
  _batch_insert()       ← psycopg2 execute_batch, 500-row batches
       │
       ▼
  raw.pitches           ← DB trigger raw.flag_pitch_quality() fires on insert
       │
       ▼
  _log_freshness()      ← upserts raw.etl_data_freshness after each game

Usage
-----
  # A historical backfill (the DSN defaults to BASEBALL_DB_DSN)
  loader = HistoricalDataLoader()
  loader.refresh_seasons(start_year=2022, end_year=2024)

  # Current-season incremental update
  loader.load_date_range(start_date=date(2025, 3, 18), end_date=date.today())

  # One game
  loader.load_game(game_pk=745528, season=2024)

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

import logging
import math
import threading
from datetime import date, datetime, timedelta
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool

from db.dsn import require_dsn
from ingest.games.base import LoaderBase
from ingest.games.date_windows import (
    _CONSECUTIVE_FAILURE_LIMIT,
    _date_chunks,
    _schedule_game_is_final,
)
from ingest.games.game_values import _manager_from_coaches, _parse_height
from ingest.games.pitch_rows import (
    _MAX_ASSIST_SLOTS,
    _MAX_PUTOUT_SLOTS,
    _MAX_THROWING_ERROR_SLOTS,
)
from ingest.games.play_events import extract_play_events
from ingest.games.reference_rows import ReferenceRowsMixin
from ingest.games.venues import (
    _first_nonblank,
    _resolve_venue,
    _resolve_venue_id,
)
from ingest.games.write_rows import WritesMixin
from ingest.load_boxscores import (
    parse_boxscore,
    parse_bullpen_listing,
    persist_bullpen_sync,
    persist_sync,
)
from ingest.mlb import client as mlb_client
from ingest.mlb.client import Response, RetryPolicy
from ingest.savant.page_json import (
    SavantScrapeError,
    _parse_savant_embedded_json,
)

log = logging.getLogger(__name__)


#: The loader's reads: ten attempts, the first retry after two seconds.
_POLICY = RetryPolicy(retries=9, first_wait_s=2.0)
HTTP_TIMEOUT_S = 30.0
_USER_AGENT = "baseball-sim-etl/1.0 (+historical loader)"

# The game types loaded. 'C' ("Championship") is left out: an obsolete MLB
# game type from before the 2015+ window, and not in the raw.games CHECK
# constraint (('R','P','S','A','D','F','L','W')), so such a game would raise a
# CheckViolation.
GAME_TYPES = ["R", "F", "D", "L", "W", "P"]


def _http_get(url: str, params: dict | None = None) -> Response:
    """GET ``url`` with the loader's retry policy (the seam the tests patch)."""
    return mlb_client.get(
        url,
        params,
        headers={"User-Agent": _USER_AGENT},
        timeout_s=HTTP_TIMEOUT_S,
        policy=_POLICY,
        logger=log,
        label="The game loader",
    )


def _connect(url: str, params: dict | None = None) -> dict:
    """GET ``url`` and parse its JSON body (the seam the tests patch)."""
    return _http_get(url, params).json()


def _empty_summary() -> dict[str, int]:
    """The counts a range load returns, all zero."""
    return {"attempted": 0, "loaded": 0, "failed": 0, "skipped": 0, "rows_written": 0}


def _log_summary(label: str, summary: dict[str, int]) -> None:
    """Log a range load's closing counts under the caller's label."""
    log.info(
        "%s: %d attempted, %d loaded, %d already-loaded, %d failed, %d pitch rows written",
        label,
        summary["attempted"],
        summary["loaded"],
        summary["skipped"],
        summary["failed"],
        summary["rows_written"],
    )


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
    game_dict = _connect(mlb_client.GAME_FEED_URL.format(game_pk=game_pk) + "?hydrate=alignment")

    game_date = game_dict["gameData"]["datetime"]["officialDate"]
    home_team_id = game_dict["gameData"]["teams"]["home"]["id"]
    home_team = game_dict["gameData"]["teams"]["home"]["abbreviation"]
    away_team_id = game_dict["gameData"]["teams"]["away"]["id"]
    away_team = game_dict["gameData"]["teams"]["away"]["abbreviation"]
    # Neutral-site games can ship with NO gameData.venue at all.
    venue_id, venue_name = _resolve_venue(game_pk, game_dict["gameData"])

    home_coaches = _connect(
        mlb_client.COACHES_URL.format(team_id=home_team_id) + f"?date={game_date}"
    ).get("roster", [])
    home_manager_id, home_manager_name = _manager_from_coaches(game_pk, "home", home_coaches)

    away_coaches = _connect(
        mlb_client.COACHES_URL.format(team_id=away_team_id) + f"?date={game_date}"
    ).get("roster", [])
    away_manager_id, away_manager_name = _manager_from_coaches(game_pk, "away", away_coaches)

    home_score_before, away_score_before = 0, 0
    outs = 0
    prev_half = "bottom"
    pitch_rows: list[dict[str, Any]] = []
    # Non-pitch events (pickoff, stepoff, balk, intentional walk) go to
    # raw.play_events. They cannot live on a pitch row: an intentional walk is
    # signalled rather than thrown, so its play has NO pitch events at all.
    play_event_rows: list[dict[str, Any]] = []
    # Base state ENTERING the current play, tracked at PLAY scope. The per-pitch
    # `offense` block cannot supply it for a pitch-less play, which is exactly
    # the case raw.play_events exists to capture, so it is carried forward from
    # the previous play's POST state and cleared at each half-inning.
    runners_state_before = 0

    for play in game_dict["liveData"]["plays"]["allPlays"]:
        at_bat_number = play["atBatIndex"] + 1
        inning = play["about"]["inning"]
        top_bot = play["about"]["halfInning"]
        if top_bot != prev_half:
            outs = 0
            runners_state_before = 0
        # Advance the half-inning marker HERE, at the play level, so a play
        # with NO pitches (an intentional walk) reaches it too. Advanced only
        # inside the pitch branch, the next play would reset the base state a
        # second time and erase the state carried from the pitch-less play.
        prev_half = top_bot
        # An extra-innings half does NOT start with the bases empty —
        # the automatic runner is on second, and the reset above just erased
        # him. THE FIX IS IN THE FEED: the placement is announced by an
        # `action` playEvent with eventType `runner_placed` that carries the
        # base directly (`base: 2`, verified on game 744882). The event can sit
        # at any index inside the half's first play (0, 1 and 3 all measured),
        # so every playEvent is scanned; the runner is aboard from the first
        # pitch regardless of where the feed logged the announcement.
        for _ev in play.get("playEvents") or []:
            if str(((_ev.get("details") or {}).get("eventType")) or "") == "runner_placed":
                _placed_base = _ev.get("base")
                if _placed_base in (1, 2, 3):
                    runners_state_before |= 1 << (int(_placed_base) - 1)
        # The PRE-play out count, read from the payload rather than
        # accumulated. `playEvents[0].count.outs` is the state ENTERING the play;
        # `play.count.outs` is the state after it. Verified on real games —
        # ab=13 Flyout reads playEvents[0].outs=0 and play.count.outs=1.
        # A pitch-less play (an intentional walk) has no events to read, so it
        # falls back to the value carried from the previous play.
        _first_events = play.get("playEvents") or []
        if _first_events:
            outs = int((_first_events[0].get("count") or {}).get("outs") or 0)
        play_event_rows.extend(
            extract_play_events(
                play,
                game_pk=game_pk,
                game_date=game_date,
                # `_fetch_game_pitches` never receives the season — the caller
                # (`load_game`) holds it and stamps the pitch rows later. An MLB
                # season never spans a calendar year, so the official date's year
                # is the season.
                season=int(str(game_date)[:4]),
                outs_before_play=outs,
                runners_state_before=runners_state_before,
                # The PRE-play score, the same numbers the pitch rows
                # get. The extractor must not read the play's own `result`
                # scores — those are post-play.
                home_score_before=home_score_before,
                away_score_before=away_score_before,
            )
        )
        # Carry the POST state forward for the next play. Assigned here, right
        # after the extract call above has consumed the pre-play value.
        _matchup = play.get("matchup") or {}
        runners_state_before = (
            (1 if _matchup.get("postOnFirst") else 0)
            + (2 if _matchup.get("postOnSecond") else 0)
            + (4 if _matchup.get("postOnThird") else 0)
        )
        balls, strikes = 0, 0
        # Bind the post-play score at PLAY scope. These are assigned
        # inside the pitch branch but read at play scope by the score re-sync
        # below — and Python evaluates a `.get(key, default)` default EAGERLY, so
        # the read happens even when the key is present. A pitch-less FIRST play
        # (every event taken by the non-pitch `continue`) would otherwise raise
        # NameError before the re-sync could run.
        home_score_after, away_score_after = home_score_before, away_score_before
        # The `.replace(",", "")` here and on the pitch description was a
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
                    f"{mlb_client.MLB_BASE}{play_event['offense']['batter']['link']}"
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
                    # A coord_y above 198.27 is BEHIND home plate (a foul pop
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
                        # `math.atan`, not `np.arctan`: importing numpy for one
                        # scalar would load OpenBLAS, libgfortran and libquadmath
                        # into every ETL process.
                        spray_angle = math.atan((coord_x - 125.42) / (198.27 - coord_y)) * (
                            180 / math.pi
                        )
                batted_ball_type = hd["trajectory"]
                hit_location = hd.get("location", "")

            # COUNT the outs this pitch caused; do not derive them.
            # `count.outs` is the out total entering the play and is CONSTANT
            # across every pitch of it, so no subtraction of running totals can
            # give the outs of one pitch. The runner loop below counts them,
            # baserunning outs included.
            outs_on_pitch = 0
            runner_on_first_score = runner_on_second_score = runner_on_third_score = False
            home_score_after = home_score_before
            away_score_after = away_score_before
            runs = earned_runs = rbis = 0
            fielded_by = of_assist = fielding_error = dropped_ball = ""
            assist_dict: dict[str, Any] = {}
            putout_dict: dict[str, Any] = {}
            throwing_error_dict: dict[str, Any] = {}
            assist_tracker = putout_tracker = throwing_error_tracker = 1
            # raw.pitches has field_assist_1..5, field_putout_1..3 and
            # throwing_error_1..2. Multi-runner rundowns are rare enough that a
            # 6th assist is not worth its own column, but the OVERFLOW must be
            # visible, so a reader can exclude or measure those plays instead of
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
                        # The pitch that CAUSED the out owns it. This sits inside
                        # the `[i .. max_play_index]` window, so an index OTHER than
                        # `i` means a non-pitch event that followed this pitch.
                        #
                        # Where every displaced out actually lands,
                        # MEASURED over 357 games / 19,072 outs (131 displaced):
                        #   * 53 pickoff-family outs -> `raw.play_events`
                        #   * 63 mid-plate-appearance caught stealings -> the
                        #     `sb_attempt_*`/`sb_success_*` columns on this pitch
                        #     row (the scan above), which the IP formula counts
                        #   * 7 batter strikeouts the feed anchors to a trailing
                        #     `no_pitch` event -> the PA's `events` column, the
                        #     canonical out label
                        #   * ~8 runner outs on `other_out`/`wild_pitch` actions
                        #     reach NO table when they do not end the PA —
                        #     ~0.04% of all outs, accepted and documented in
                        #     ingest/statcast_events.py.
                        if runner["details"].get("playIndex") == i:
                            outs_on_pitch += 1
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
                # This read a running variable that was updated AFTER the
                # row was built, so every row carried the PREVIOUS play's entry
                # value. Measured against the payload: raw.pitches.outs was wrong
                # on 46% of plate appearances, almost always one too low. The
                # pitch knows its own out count; ask it.
                "outs": int((play_event.get("count") or {}).get("outs") or 0),
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
            home_score_before = home_score_after
            away_score_before = away_score_after
        # Carry the POST-play out count so a pitch-less play that follows
        # still knows the state it began in. Reset to 0 on a half-inning change
        # above, because the last play of a half-inning posts 3.
        outs = int((play.get("count") or {}).get("outs") or outs)
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
    # Ride on game_dict rather than widening the return. This function
    # already hands back side-channel data that way (`_managers`), and 38 tests
    # mock it as a 2-tuple — a third element breaks every one of them for no
    # gain.
    game_dict["_play_events"] = play_event_rows
    return pitch_rows, game_dict


# ---------------------------------------------------------------------------
# Main ETL class
# ---------------------------------------------------------------------------


class HistoricalDataLoader(ReferenceRowsMixin, WritesMixin, LoaderBase):
    """
    Fetches pitch-by-pitch data from the MLB Stats API and loads it into
    raw.pitches in PostgreSQL.

    Parameters
    ----------
    dsn : str
        psycopg2 connection string, e.g.
        "postgresql://user:password@localhost:5432/baseball"
    """

    def __init__(self, dsn: str | None = None) -> None:
        # The DSN defaults to the BASEBALL_DB_DSN environment variable.
        # Pass an explicit DSN to override (test fixtures, ad-hoc backfills).
        # require_dsn raises a clear error if neither is set, rather than
        # letting psycopg2 surface a confusing "could not connect" error mid-run.
        self.dsn = dsn or require_dsn()

        # Lazily-initialised connection pool.  Created on first DB use
        # (see _ensure_pool) and shared across every game in a backfill run,
        # rather than opening/closing a fresh psycopg2 connection per round-trip.
        self._pool: psycopg2.pool.ThreadedConnectionPool | None = None
        # Guards the lazy construction above (see _ensure_pool).
        self._pool_lock = threading.Lock()
        # Circuit breaker for _dispatch_game — see its docstring.
        self._consecutive_failures = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def load_game(self, game_pk: int, season: int, batter_hand_cache: dict | None = None) -> dict:
        """
        Fetch and load a single game.  Returns a summary dict with counts
        of inserted, skipped (hard errors), and flagged (warnings) rows.
        """
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

        Also re-runs ``_ensure_prerequisites``, which re-upserts the venues,
        teams, players, managers, the ``raw.games`` row and the starting
        lineups.

        Refuses to shrink a game: if the re-parse yields fewer rows than are
        stored, the transaction is rolled back and :class:`ReloadShrinkError` is
        raised rather than committing the deletion.  Pass ``allow_shrink=True``
        only when the game genuinely has fewer pitches than the stored copy.
        """
        return self._load_game(
            game_pk, season, batter_hand_cache, replace=True, allow_shrink=allow_shrink
        )

    def _load_game(
        self,
        game_pk: int,
        season: int,
        batter_hand_cache: dict | None,
        *,
        replace: bool,
        allow_shrink: bool = False,
    ) -> dict:
        """Shared body of :meth:`load_game` / :meth:`reload_game`."""
        if batter_hand_cache is None:
            batter_hand_cache = {}
        log.info("%s game %s …", "Reloading" if replace else "Loading", game_pk)
        raw_rows, game_dict = _fetch_game_pitches(game_pk, batter_hand_cache)
        play_event_rows = game_dict.get("_play_events") or []
        self._ensure_prerequisites(game_pk, game_dict)
        result = self._process_and_insert(
            game_pk,
            season,
            raw_rows,
            replace=replace,
            allow_shrink=allow_shrink,
            play_event_rows=play_event_rows,
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

        * The MLB schedule endpoint clamps any window wider than 365 days to
          ``startDate + 365`` and still answers HTTP 200, so the window is cut
          into slices of at most 365 days, and each slice's returned coverage
          is checked against what was asked for.
        * Only Final games load (as in :meth:`refresh_seasons`): a game loaded
          in progress would read as loaded and never be completed.
        """
        if end_date < start_date:
            raise ValueError(f"end_date {end_date} precedes start_date {start_date}")

        batter_hand_cache: dict[int, str] = {}
        summary = _empty_summary()

        for chunk_start, chunk_end in _date_chunks(start_date, end_date):
            params = {
                "sportId": 1,
                "gameTypes": GAME_TYPES,
                "startDate": chunk_start.strftime("%Y-%m-%d"),
                "endDate": chunk_end.strftime("%Y-%m-%d"),
            }
            schedule = _connect(mlb_client.SCHEDULE_URL, params)["dates"]

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

            self._load_schedule_dates(
                schedule,
                batter_hand_cache,
                reload=reload,
                summary=summary,
                date_log="Processing date %s",
            )

        _log_summary(f"load_date_range {start_date}→{end_date}", summary)
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

        summary = _empty_summary()

        for season in range(start_year, end_year + 1):
            log.info("=== Season %d ===", season)
            params = {
                "sportId": 1,
                "gameTypes": GAME_TYPES,
                "season": season,
            }
            schedule = _connect(mlb_client.SCHEDULE_URL, params)["dates"]
            batter_hand_cache: dict[int, str] = {}
            self._load_schedule_dates(
                schedule,
                batter_hand_cache,
                reload=reload,
                summary=summary,
                date_log="  %s",
                season=season,
            )

        _log_summary(f"refresh_seasons {start_year}–{end_year}", summary)
        return summary

    def _load_schedule_dates(
        self,
        schedule: list[dict],
        batter_hand_cache: dict,
        *,
        reload: bool,
        summary: dict[str, int],
        date_log: str,
        season: int | None = None,
    ) -> None:
        """Send every Final game of the schedule's dates to :meth:`_dispatch_game`.

        The loop skips the entry of a rescheduled or resumed game. It loads
        Final games only: only a completed game carries full pitch-by-pitch
        data, and a game loaded in progress reads as loaded and is never
        completed. The gate also keeps the current season from pulling
        thousands of unplayed games (0 pitches, one wasted fetch and an empty
        ``raw.games`` stub each), so each nightly run picks up the games that
        have newly become Final.

        ``date_log`` is the caller's log line for each date. ``season`` is the
        season of every game; without it, each game takes its date's year.
        """
        for date_entry in schedule:
            log.info(date_log, date_entry["date"])
            for game in date_entry["games"]:
                if "rescheduleGameDate" in game or "resumeGameDate" in game:
                    continue
                if not _schedule_game_is_final(game):
                    continue
                self._dispatch_game(
                    game["gamePk"],
                    season if season is not None else int(date_entry["date"][:4]),
                    batter_hand_cache,
                    reload=reload,
                    summary=summary,
                )

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

        Both paths contain per-game failures. The parser is full of hard
        subscripts on third-party JSON (``pd_data["strikeZoneTop"]``,
        ``hd["trajectory"]``, ``defense["catcher"]["id"]``), so one malformed
        feed would otherwise unwind the whole run, and the nightly chain (which
        runs under ``set -eu``) would stop on the same game every night.

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

        venue_id = _resolve_venue_id(game_pk, gd)
        home_team_id = gd["teams"]["home"]["id"]
        away_team_id = gd["teams"]["away"]["id"]

        self._ensure_venue(venue_id, season)
        self._ensure_teams(home_team_id, away_team_id, season, gd)
        self._ensure_players(game_dict)
        self._ensure_managers(managers, home_team_id, away_team_id, season, game_date)
        # Pass the FULL game_dict so _ensure_game can read the linescore
        # + decisions (under liveData, not gameData), then ingest the lineups.
        self._ensure_game(game_pk, season, game_dict, managers)
        self._ensure_game_lineups(game_pk, season, game_dict)
        # The official per-player box score (the prop ground truth) from
        # the same in-hand payload. Non-fatal: a failure logs a warning and never
        # fails the game load.
        self._ensure_game_player_stats(
            game_pk, season, game_date, game_dict, home_team_id, away_team_id
        )

    # --- 1. Venues ----------------------------------------------------------

    def _ensure_venue(self, venue_id: int, season: int, *, force: bool = False) -> None:
        """
        Upserts raw.venues if the venue_id is not already present.
        Fetches full venue details (dimensions, surface, roof) from the
        MLB venues endpoint when the record is missing.

        ``force=True`` skips the existence check and re-fetches, so a row
        already present can be CORRECTED (the INSERT carries ``ON CONFLICT … DO
        UPDATE``). ``ingest/games/reload_games.py --refresh-venues`` uses it; a venue
        row cannot simply be deleted and reloaded, because raw.pitches
        references it.
        """
        if not force:
            with self._get_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM raw.venues WHERE venue_id = %s AND season = %s",
                    (venue_id, season),
                )
                if cur.fetchone() is not None:
                    return  # already exists

        log.info("  Fetching missing venue %s, season %s", venue_id, season)

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
            mlb_client.VENUE_URL.format(venue_id=venue_id),
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
                        # Was `dimensions.get("venu_name_short", " ")` — a
                        # typo (missing the 'e'), so the key NEVER matched and every
                        # row fell through to the " " default. All 331 rows in
                        # raw.venues had a blank name. The two payload shapes use
                        # different keys: the park-factors scrape has
                        # `venue_name_short`, the statcast-venue fallback has `name`;
                        # statsapi's own record is the last resort.
                        _first_nonblank(
                            dimensions.get("venue_name_short"),
                            dimensions.get("name"),
                            v.get("name"),
                        ),
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

    # --- 3. Players ---------------------------------------------------------

    def _ensure_players(self, game_dict: dict) -> None:
        """
        Upserts raw.players for every player appearing in this game's boxscore.

        The person record comes from the feed's own ``gameData.players.ID<pid>``
        (no extra HTTP call), then the boxscore stub, then ``/people/{pid}``. A
        player whose handedness cannot be established is skipped, never
        invented: ``simulation/lineup_resolver`` reads ``bats`` and ``throws``
        straight into the full-pool sampler's only hard pre-filter, and there is
        no other ``UPDATE raw.players`` to repair a wrong hand. Every player in
        the boxscore is upserted, so a wrong row is corrected on the next
        reload (``raw.players`` has no season column).
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
                    people = _connect(mlb_client.PEOPLE_URL.format(player_id=pid)).get("people", [])
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
            # `position` in the boxscore is the player's PRIMARY position,
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

        # --- 5. Game record -----------------------------------------------------

    #: Tri-state cache of "does ``raw.game_player_stats`` exist" (Alembic
    #: 0023). ``None`` means not yet probed. Same reasoning as
    #: ``_play_events_table_exists``: one catalogue lookup per run, not per game.
    _game_player_stats_table_exists: bool | None = None
    #: The same tri-state cache for ``raw.game_bullpen`` (Alembic 0025).
    _game_bullpen_table_exists: bool | None = None

    def _ensure_game_player_stats(
        self,
        game_pk: int,
        season: int,
        game_date: str,
        game_dict: dict,
        home_team_id: int,
        away_team_id: int,
    ) -> None:
        """Upsert the official per-player box score into ``raw.game_player_stats``.

        The official box-score ground truth: the live feed already
        carries the box under ``liveData.boxscore.teams``, so the loader writes
        it from the payload it holds — no extra HTTP call. The parser
        (:func:`ingest.load_boxscores.parse_boxscore`) emits one row per
        player who appeared; the upsert refreshes a game's rows on a reload.

        Non-fatal by design: the pitch rows are the load's product, and a
        box-score failure (the table absent before Alembic 0023, a malformed
        block, a transient DB error) logs a warning and returns. A game with no
        box block writes nothing.
        """
        try:
            teams = game_dict.get("liveData", {}).get("boxscore", {}).get("teams", {})
            rows = parse_boxscore(
                teams or {},
                game_pk=game_pk,
                season=season,
                game_date=date.fromisoformat(str(game_date)[:10]),
                home_team_id=home_team_id,
                away_team_id=away_team_id,
            )
            if not rows:
                return
            with self._get_conn() as conn:
                try:
                    with conn.cursor() as cur:
                        if not self._exists_once(
                            cur,
                            "_game_player_stats_table_exists",
                            "raw.game_player_stats",
                            absent_message=(
                                "raw.game_player_stats is absent (Alembic 0023 not "
                                "applied) — the official box score is not stored."
                            ),
                        ):
                            conn.rollback()
                            return
                        written = persist_sync(cur, rows)
                        # The arms the box lists per side (Alembic
                        # 0025) — the simulator's pen for the game. The same
                        # payload, no extra call; absent table = not stored.
                        pen_written = 0
                        if self._exists_once(
                            cur,
                            "_game_bullpen_table_exists",
                            "raw.game_bullpen",
                            absent_message=(
                                "raw.game_bullpen is absent (Alembic 0025 not applied) "
                                "— the per-game bullpen listing is not stored."
                            ),
                        ):
                            # Best-effort inside the same transaction: a bad
                            # listing must never cost the box rows their commit.
                            try:
                                cur.execute("SAVEPOINT sim427_bullpen")
                                pen_written = persist_bullpen_sync(
                                    cur,
                                    parse_bullpen_listing(
                                        teams or {},
                                        game_pk=game_pk,
                                        home_team_id=home_team_id,
                                        away_team_id=away_team_id,
                                    ),
                                )
                                cur.execute("RELEASE SAVEPOINT sim427_bullpen")
                            except Exception as pen_exc:  # noqa: BLE001
                                cur.execute("ROLLBACK TO SAVEPOINT sim427_bullpen")
                                log.warning(
                                    "SIM-427: bullpen listing not written for game %s: %s",
                                    game_pk,
                                    pen_exc,
                                )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            log.info(
                "  Upserted %d box-score rows + %d bullpen rows for game %s",
                written,
                pen_written,
                game_pk,
            )
        except Exception as exc:  # noqa: BLE001 — never fail the game load on the box
            log.warning("SIM-545: box-score rows not written for game %s: %s", game_pk, exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # No default password. The loader reads BASEBALL_DB_DSN and raises
    # a clear error when it is not set.
    loader = HistoricalDataLoader()
    try:
        loader.refresh_seasons()
    finally:
        # Tear the pool down so all pooled sockets are released.
        loader.close()
