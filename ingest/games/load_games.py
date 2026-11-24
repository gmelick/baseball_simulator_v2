"""The historical loader: it loads MLB Stats API games into the Postgres ``raw.*`` tables.

``HistoricalDataLoader`` fetches each game's live feed and writes the reference
rows the game needs. Then it writes the pitch rows into ``raw.pitches`` and the
non-pitch events into ``raw.play_events``. The other modules of ``ingest/games/``
build, check and write the rows. This module keeps the HTTP calls, the feed
parser ``_fetch_game_pitches`` and the class, beside the three names the tests
patch: ``_connect``, ``_http_get`` and ``_fetch_game_pitches``.

The flow
--------
  MLB Stats API
       │
       ▼
  _fetch_game_pitches()    ← fetches feed/live + coaches; returns (pitch_rows, game_dict)
       │
       ▼
  _ensure_prerequisites()  ← every foreign-key parent exists before raw.pitches
       │   One query per table finds the missing ids; only those are fetched.
       │   The order follows the foreign keys:
       │     1. raw.venues    (no parents)
       │     2. raw.teams     (→ venues)
       │     3. raw.players   (no parents; every player in the game's box score)
       │     4. raw.managers  (→ teams)
       │     5. raw.games     (→ venues, teams)
       │   Then the starting lineups and the box score.
       │
       ▼
  _build_row_dict()        ← renames the fields, converts the types, '' → None
       │
       ▼
  _validate_row()          ← checks each row before the insert
       │
       ├─ WARNING    → the row is inserted with data_quality_flag = TRUE
       └─ HARD ERROR → the row is skipped and logged to raw.etl_errors
       │
       ▼
  _batch_insert()          ← one transaction per game, 500-row batches
       │
       ▼
  raw.pitches              ← the trigger raw.flag_pitch_quality() fires on insert
       │
       ▼
  _log_freshness()         ← upserts raw.etl_data_freshness after each game

Usage
-----
  loader = HistoricalDataLoader()   # the DSN comes from BASEBALL_DB_DSN
  loader.refresh_seasons(start_year=2022, end_year=2024)                      # a backfill
  loader.load_date_range(start_date=date(2025, 3, 18), end_date=date.today())  # a window
  loader.load_game(game_pk=745528, season=2024)                               # one game

A corrective re-ingest
----------------------
``load_game`` CANNOT repair loaded data: the pitch insert carries
``ON CONFLICT (game_pk, at_bat_number, pitch_number) DO NOTHING``, so a rerun
after a parser fix discards every row and reports success. Use the reload path:
it deletes the game's rows and inserts the new ones in one transaction.

  loader.reload_game(game_pk=745528, season=2024)   # one game
  loader.refresh_seasons(2017, 2025, reload=True)   # every Final game
  loader.load_date_range(d1, d2, reload=True)       # a date window

A range load counts each game's failure in its summary and carries on, so one
malformed feed cannot stop a reload of every season.
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

# The game types loaded. 'C' (an obsolete "Championship" type) is left out: the
# raw.games CHECK constraint does not allow it, so such a game raises a
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
# The feed parser
# ---------------------------------------------------------------------------


def _fetch_game_pitches(
    game_pk: int,
    batter_hand_cache: dict[int, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch one game's live feed and parse it into pitch rows.

    Returns ``(pitch_rows, game_dict)``:
      - ``pitch_rows``: one dict per pitch, ready for ``_build_row_dict``.
      - ``game_dict``: the feed/live response. It also carries the managers under
        ``_managers`` (for ``_ensure_prerequisites``) and the non-pitch events
        under ``_play_events``.

    ``batter_hand_cache`` is shared across games, so each batter's hand is
    fetched once.
    """
    game_dict = _connect(mlb_client.GAME_FEED_URL.format(game_pk=game_pk) + "?hydrate=alignment")

    game_date = game_dict["gameData"]["datetime"]["officialDate"]
    home_team_id = game_dict["gameData"]["teams"]["home"]["id"]
    home_team = game_dict["gameData"]["teams"]["home"]["abbreviation"]
    away_team_id = game_dict["gameData"]["teams"]["away"]["id"]
    away_team = game_dict["gameData"]["teams"]["away"]["abbreviation"]
    # A neutral-site game can ship with no gameData.venue at all.
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
    # The non-pitch events (pickoff, stepoff, balk, intentional walk) go to
    # raw.play_events. A pitch row cannot hold them: an intentional walk is
    # signalled, not thrown, so its play has NO pitch events.
    play_event_rows: list[dict[str, Any]] = []
    # The base state ENTERING the current play, tracked per play. The per-pitch
    # `offense` block cannot supply it for a play with no pitch, the case
    # raw.play_events exists for. It carries the previous play's post-play state
    # and resets at each half-inning.
    runners_state_before = 0

    for play in game_dict["liveData"]["plays"]["allPlays"]:
        at_bat_number = play["atBatIndex"] + 1
        inning = play["about"]["inning"]
        top_bot = play["about"]["halfInning"]
        if top_bot != prev_half:
            outs = 0
            runners_state_before = 0
        # Move the half-inning marker per play, not per pitch. Per pitch, a play
        # with no pitch (an intentional walk) skips it, and the next play resets
        # the base state a second time.
        prev_half = top_bot
        # An extra-inning half starts with the automatic runner on second, and
        # the reset above just erased him. The feed announces him with an
        # `action` playEvent of eventType `runner_placed` that carries the base
        # (`base: 2`). The event can sit at any index of the half's first play,
        # so every playEvent is scanned; the runner is aboard from the first pitch.
        for _ev in play.get("playEvents") or []:
            if str(((_ev.get("details") or {}).get("eventType")) or "") == "runner_placed":
                _placed_base = _ev.get("base")
                if _placed_base in (1, 2, 3):
                    runners_state_before |= 1 << (int(_placed_base) - 1)
        # The out count ENTERING the play, read from the payload, not accumulated.
        # `playEvents[0].count.outs` is the state entering the play;
        # `play.count.outs` is the state after it. A play with no events (an
        # intentional walk) keeps the value carried from the previous play.
        _first_events = play.get("playEvents") or []
        if _first_events:
            outs = int((_first_events[0].get("count") or {}).get("outs") or 0)
        play_event_rows.extend(
            extract_play_events(
                play,
                game_pk=game_pk,
                game_date=game_date,
                # The caller holds the season and stamps the pitch rows later. An
                # MLB season never spans two calendar years, so the official
                # date's year is the season.
                season=int(str(game_date)[:4]),
                outs_before_play=outs,
                runners_state_before=runners_state_before,
                # The PRE-play score, as the pitch rows record it. The play's own
                # `result` scores are post-play.
                home_score_before=home_score_before,
                away_score_before=away_score_before,
            )
        )
        # Carry the post-play base state to the next play. This runs after the
        # extract call has used the pre-play value.
        _matchup = play.get("matchup") or {}
        runners_state_before = (
            (1 if _matchup.get("postOnFirst") else 0)
            + (2 if _matchup.get("postOnSecond") else 0)
            + (4 if _matchup.get("postOnThird") else 0)
        )
        balls, strikes = 0, 0
        # Bind the post-play score per play. The pitch loop assigns it, but the
        # score sync after the loop reads it: Python evaluates a `.get(key,
        # default)` default eagerly. Without this line, a first play with no
        # pitch raises NameError.
        home_score_after, away_score_after = home_score_before, away_score_before
        play_description = play["result"].get("description", "")
        event = ""
        pinch_hitter = pinch_runner = pitcher_sub = defensive_sub = False

        for i in range(len(play["playEvents"])):
            # --- Substitutions ---
            # A flag latches on the non-pitch event where the substitution
            # happens, and the reset after the append below clears it. So the
            # flag lands on the first pitch AFTER the substitution, not on every
            # pitch of the plate appearance.
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

            # --- The non-pitch events after this pitch (steals, wild pitches) ---
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
                    # A coord_y above 198.27 is BEHIND home plate (a foul pop into
                    # the backstop). `atan` mirrors it into fair ground, and its
                    # true angle lies outside +/-90 degrees, which the pull and
                    # opposite-field thresholds cannot represent. So spray_angle
                    # stays NULL rather than record a false direction.
                    if coord_y > 198.27:
                        spray_angle = ""
                    elif coord_y == 198.27:
                        spray_angle = 90 if coord_x > 125.42 else -90
                    else:
                        # `math.atan`, not `np.arctan`: numpy would load OpenBLAS
                        # and its libraries into every ETL process.
                        spray_angle = math.atan((coord_x - 125.42) / (198.27 - coord_y)) * (
                            180 / math.pi
                        )
                batted_ball_type = hd["trajectory"]
                hit_location = hd.get("location", "")

            # COUNT the outs this pitch caused; do not derive them. `count.outs`
            # is the play's entering total, the same on every pitch of the play,
            # so no subtraction gives one pitch's outs. The runner loop below
            # counts them, baserunning outs included.
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
            # throwing_error_1..2. A credit past the last slot (a rundown with
            # many runners) sets this flag, so a reader can exclude or measure
            # those plays instead of under-counting the credits.
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
                    # A runner thrown out advancing on a batted ball (not on a
                    # steal, and not the batter). The feed sets isOut=True and an
                    # empty movement.end on these plays.
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
                        # The pitch that CAUSED the out owns it. The runner sits in
                        # the `[i .. max_play_index]` window, so an index other
                        # than `i` is a non-pitch event after this pitch. Such an
                        # out lands elsewhere:
                        #   * a pickoff-family out -> `raw.play_events`
                        #   * a caught stealing during the plate appearance -> the
                        #     `sb_attempt_*` / `sb_success_*` columns of this row,
                        #     which the innings-pitched formula counts
                        #   * a strikeout the feed anchors to a trailing `no_pitch`
                        #     event -> the plate appearance's `events` column
                        #   * a runner out on an `other_out` / `wild_pitch` action
                        #     that does not end the plate appearance reaches NO
                        #     table: a small loss, accepted and documented in
                        #     ingest/statcast_events.py.
                        if runner["details"].get("playIndex") == i:
                            outs_on_pitch += 1
                        # A retired runner cannot also score or reach a base. The
                        # feed normally leaves `end` null on an out (the base is
                        # in `outBase`), but not for every event type. Without
                        # this `continue`, an `end` would re-seat the runner on the
                        # base just cleared.
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

            # The parser's names; COLUMN_RENAME maps them to the column names in
            # _build_row_dict().
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
                # The out count entering the play, read from the pitch itself.
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

            # Clear the substitution flags: this pitch used them. Without the
            # reset, a flag spreads to the rest of the plate appearance.
            pinch_hitter = pinch_runner = pitcher_sub = defensive_sub = False
            balls = play_event["count"]["balls"]
            strikes = play_event["count"]["strikes"]
            home_score_before = home_score_after
            away_score_before = away_score_after
        # Carry the post-play out count, so a following play with no pitch knows
        # its entering state. The half-inning check above resets it, because a
        # half-inning's last play posts 3.
        outs = int((play.get("count") or {}).get("outs") or outs)
        home_score_before = play["result"].get("homeScore", home_score_after)
        away_score_before = play["result"].get("awayScore", away_score_after)

    # Attach the managers, so _ensure_prerequisites() need not fetch them again.
    # The key is private, so it cannot collide with a real feed field.
    game_dict["_managers"] = {
        "home_manager_id": home_manager_id,
        "home_manager_name": home_manager_name,
        "away_manager_id": away_manager_id,
        "away_manager_name": away_manager_name,
    }
    # The play events ride on game_dict too, not in a wider return: the tests
    # mock this function as a 2-tuple.
    game_dict["_play_events"] = play_event_rows
    return pitch_rows, game_dict


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------


class HistoricalDataLoader(ReferenceRowsMixin, WritesMixin, LoaderBase):
    """Load MLB Stats API games, pitch by pitch, into the Postgres ``raw.*`` tables.

    ``dsn`` is a psycopg2 connection string; it defaults to ``BASEBALL_DB_DSN``.
    """

    def __init__(self, dsn: str | None = None) -> None:
        # An explicit DSN overrides BASEBALL_DB_DSN (test fixtures, ad-hoc
        # backfills). require_dsn raises a clear error when neither is set,
        # instead of a confusing "could not connect" in the middle of a run.
        self.dsn = dsn or require_dsn()

        # Created on the first database use (_ensure_pool) and shared by every
        # game of a run.
        self._pool: psycopg2.pool.ThreadedConnectionPool | None = None
        self._pool_lock = threading.Lock()
        # The games that failed in a row (see _dispatch_game).
        self._consecutive_failures = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def load_game(self, game_pk: int, season: int, batter_hand_cache: dict | None = None) -> dict:
        """Fetch and load one game; return the counts of rows inserted, skipped and flagged."""
        return self._load_game(game_pk, season, batter_hand_cache, replace=False)

    def reload_game(
        self,
        game_pk: int,
        season: int,
        batter_hand_cache: dict | None = None,
        *,
        allow_shrink: bool = False,
    ) -> dict:
        """Re-fetch a game and REPLACE its stored ``raw.pitches`` rows; return the counts.

        This is the corrective path. ``load_game`` cannot repair loaded data: its
        insert carries ``ON CONFLICT … DO NOTHING``, so a rerun after a parser fix
        writes nothing. ``reload_game`` deletes the game's rows and inserts the new
        ones in one transaction, so a failed insert never leaves the game
        half-deleted. Use it after any parser change that alters a column's value.
        The returned ``inserted`` count is measured from the table, so a no-op shows.

        It also re-runs ``_ensure_prerequisites``. That writes the players, the
        ``raw.games`` row, the starting lineups and the box score again; it adds a
        venue, a team or a manager only when one is missing.

        It refuses to shrink a game: when the new parse has fewer rows than are
        stored, it rolls back and raises :class:`ReloadShrinkError`. Pass
        ``allow_shrink=True`` only when the game truly has fewer pitches.
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
        """The shared body of :meth:`load_game` and :meth:`reload_game`."""
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
        """Load every Final game between two dates, inclusive; return the summary counts.

        The default skips a game already loaded. ``reload=True`` sends every game
        through :meth:`reload_game`, which replaces its stored rows: the way to
        re-ingest a window after a parser fix.

        The MLB schedule endpoint cuts a window wider than a year and still answers
        HTTP 200. So the range goes out in slices (:func:`_date_chunks`), and the
        loader warns when a slice's answer stops short of the slice's end.

        Raises ValueError when ``end_date`` precedes ``start_date``.
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

            # A cut-short answer looks the same as a stretch with no games, so
            # check how far the answer reaches.
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
        """Load every Final game of the seasons from ``start_year`` to ``end_year``.

        Returns the summary counts. ``end_year`` defaults to last season. The
        default skips a game already loaded. ``reload=True`` sends every Final
        game through :meth:`reload_game`, which replaces its stored rows: the way
        to re-ingest seasons after a parser fix.
        """
        if end_year is None:
            end_year = datetime.today().year - 1  # the current season is left out

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

        The loop skips the entry of a rescheduled or resumed game. It loads Final
        games only: only a completed game has full pitch-by-pitch data, and a game
        loaded in progress reads as loaded and is never completed. The filter also
        keeps the current season from fetching its unplayed games, so each nightly
        run picks up the games newly Final.

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
        """Load or reload one scheduled game, and add its outcome to ``summary``.

        A game's failure is counted, logged and contained. The parser subscripts
        third-party JSON directly (``pd_data["strikeZoneTop"]``,
        ``hd["trajectory"]``), so one malformed feed would otherwise stop the run,
        and the nightly chain (``set -eu``) would stop on the same game every night.

        The containment has a limit: ``_CONSECUTIVE_FAILURE_LIMIT`` failures in a
        row raise RuntimeError. One bad game must be survivable. An API outage, or
        a database that rejects every row, must fail loudly, not finish a run that
        loaded almost nothing.
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
        # Carry the measured row count up: the game counts alone cannot tell a
        # full rewrite from a write of nothing.
        summary["rows_written"] += int(result.get("inserted", 0))

    # ------------------------------------------------------------------
    # The foreign-key parents, written before every raw.pitches insert
    # ------------------------------------------------------------------

    def _ensure_prerequisites(self, game_pk: int, game_dict: dict) -> None:
        """Make sure every foreign-key parent of ``raw.pitches`` exists before the insert.

        The order follows the foreign keys:

            raw.venues  →  raw.teams  →  raw.players
                                      →  raw.managers
                                      →  raw.games

        Then it writes the starting lineups and the box score. A step checks the
        database first and calls the MLB Stats API only for a missing id, so after
        the first games of a backfill most games make no extra call.
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
        # The FULL game_dict: the linescore and the decisions are under liveData.
        self._ensure_game(game_pk, season, game_dict, managers)
        self._ensure_game_lineups(game_pk, season, game_dict)
        # The official box score, the truth the props are graded on, from the
        # same payload. A failure here logs a warning and never fails the game.
        self._ensure_game_player_stats(
            game_pk, season, game_date, game_dict, home_team_id, away_team_id
        )

    # --- Venues -------------------------------------------------------------

    def _ensure_venue(self, venue_id: int, season: int, *, force: bool = False) -> None:
        """Upsert the ``raw.venues`` row of a venue and season when it is missing.

        The dimensions come from Baseball Savant's park-factor page, with the
        venue page as the fallback. The surface, the roof and the location come
        from the MLB venues endpoint.

        ``force=True`` skips the existence check and re-fetches, so a stored row
        can be CORRECTED (the insert updates on a conflict). ``reload_games.py
        --refresh-venues`` uses it: a venue row cannot be deleted and reloaded,
        because ``raw.pitches`` references it.
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

        # The feed's turf type reads "Artificial Turf" or a kind of "Grass".
        surface_raw = field.get("turfType", "Grass")
        surface = (
            "Turf"
            if "turf" in surface_raw.lower() or "artificial" in surface_raw.lower()
            else "Grass"
        )

        # The feed's roof type → the raw.venues value.
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
                        # The two Savant payloads name the venue under different
                        # keys: `venue_name_short` (park factors) and `name` (the
                        # venue page). The MLB record is the last resort.
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

    # --- Players ------------------------------------------------------------

    def _ensure_players(self, game_dict: dict) -> None:
        """Upsert ``raw.players`` for every player in the game's box score.

        The person record comes from the feed's ``gameData.players.ID<pid>`` (no
        extra HTTP call), then the box-score entry, then ``/people/{pid}``. A
        player whose batting or throwing hand stays unknown is skipped, never
        guessed: ``simulation/lineup_resolver`` feeds ``bats`` and ``throws`` into
        the sampler's hard filter, and nothing else corrects a wrong hand.
        ``raw.players`` has no season column, so the next reload corrects a wrong
        row.
        """
        boxscore = game_dict.get("liveData", {}).get("boxscore", {}).get("teams", {})
        # The feed's own person records, already fetched.
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
            # Merge, the fullest record first: gameData.players, then the box score.
            person: dict[str, Any] = {}
            person.update(game_people.get(f"ID{pid}", {}) or {})
            for k, v in (pdata.get("person") or {}).items():
                person.setdefault(k, v)

            bats = (person.get("batSide") or {}).get("code")
            throws = (person.get("pitchHand") or {}).get("code")

            if not bats or not throws:
                # The last resort: the person endpoint. On a failure the player is
                # skipped, so a later reload can add him with real data.
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
            # The box score's `position` is the player's PRIMARY position, not his
            # slot in this game. The default is 'UT' (utility): a default of 'P'
            # asserts a fact about anyone whose position is absent.
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

    # --- The box score ------------------------------------------------------

    #: Whether ``raw.game_player_stats`` exists: ``None`` until the first probe
    #: (see ``LoaderBase._exists_once``).
    _game_player_stats_table_exists: bool | None = None
    #: The same cache for ``raw.game_bullpen``.
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

        The live feed carries the box score under ``liveData.boxscore.teams``, so
        the loader writes it from the payload it holds, with no extra HTTP call.
        :func:`ingest.load_boxscores.parse_boxscore` emits one row per player who
        appeared; the upsert refreshes the game's rows on a reload. The same
        transaction writes the bullpen listing into ``raw.game_bullpen``.

        A failure here is never fatal: the pitch rows are the load's product. A
        box-score failure (an absent table, a malformed block, a transient
        database error) logs a warning and returns. A game with no box score
        writes nothing.
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
                        # The pitchers the box score lists for each side: the
                        # simulator's bullpen for the game.
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
                            # A savepoint: a bad listing must never cost the box
                            # rows their commit.
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
    # The loader reads BASEBALL_DB_DSN and raises a clear error when it is unset.
    loader = HistoricalDataLoader()
    try:
        loader.refresh_seasons()
    finally:
        loader.close()
