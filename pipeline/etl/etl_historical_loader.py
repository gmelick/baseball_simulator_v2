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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool

from db.introspect import pg_relation_exists
from pipeline.etl.boxscore_ingest import (
    parse_boxscore,
    parse_bullpen_listing,
    persist_bullpen_sync,
    persist_sync,
)
from pipeline.etl.historical.base import LoaderBase
from pipeline.etl.historical.game_values import (
    _build_starting_lineup_rows as _build_starting_lineup_rows,  # re-export: removed in the integration pass
)
from pipeline.etl.historical.game_values import _parse_height
from pipeline.etl.historical.reference_rows import ReferenceRowsMixin
from pipeline.etl.historical.rows import (
    _MAX_ASSIST_SLOTS,
    _MAX_PUTOUT_SLOTS,
    _MAX_THROWING_ERROR_SLOTS,
)
from pipeline.etl.historical.savant import (
    SavantScrapeError,
    _parse_savant_embedded_json,
)
from pipeline.etl.historical.schedule import (
    _CONSECUTIVE_FAILURE_LIMIT,
    _date_chunks,
    _schedule_game_is_final,
)
from pipeline.etl.historical.venues import (
    _first_nonblank,
    _resolve_venue,
    _resolve_venue_id,
)
from pipeline.etl.historical.writes import WritesMixin
from pipeline.etl.play_events import extract_play_events

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("etl_historical_loader")


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


# SIM-441: 'C' ("Championship") was removed — it is an obsolete MLB game type
# predating our 2015+ window, and it is NOT in the raw.games CHECK constraint
# (('R','P','S','A','D','F','L','W')), so a single such game would have raised a
# CheckViolation and, before the per-game isolation below, killed the whole run.
GAME_TYPES = ["R", "F", "D", "L", "W", "P"]


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
    # SIM-447: neutral-site games can ship with NO gameData.venue at all.
    venue_id, venue_name = _resolve_venue(game_pk, game_dict["gameData"])

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
    # SIM-502: non-pitch events (pickoff, stepoff, balk, intentional walk) go to
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
        # SIM-502: advance the half-inning marker HERE, at the play level.
        #
        # It used to be advanced inside the pitch branch below, so a play with NO
        # pitches never reached it. The next play then re-entered this check with
        # `prev_half` still stale, the reset fired a SECOND time, and it destroyed
        # the base state carried forward from the pitch-less play. Measured over
        # 1,066 real games: 11 plays lost their base state that way, every one of
        # them following a half-inning that opened with an intentional walk.
        #
        # None of the 11 happened to be an intentional walk itself, so no bad row
        # reached the table — luck, not correctness. This is the same defect class
        # already fixed once inside the extractor, re-introduced in the caller.
        prev_half = top_bot
        # SIM-502a: an extra-innings half does NOT start with the bases empty —
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
        # SIM-501: the PRE-play out count, read from the payload rather than
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
                # SIM-502b: the PRE-play score, the same numbers the pitch rows
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

            # SIM-501: COUNT the outs this pitch caused; do not derive them.
            # This read `play_event["count"]["outs"] - outs`, which cannot work:
            # `count.outs` is the out total entering the play and is CONSTANT
            # across every pitch of it, so subtracting a running total that
            # already includes it yields zero. Measured: 92.6% of balls in play
            # that BECAME AN OUT recorded outs_on_pitch=0, and 100% of
            # strikeouts did. The runner loop below now counts instead, which
            # also picks up baserunning outs the subtraction never saw.
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
                        # The pitch that CAUSED the out owns it. This sits inside
                        # the `[i .. max_play_index]` window, so an index OTHER than
                        # `i` means a non-pitch event that followed this pitch.
                        #
                        # SIM-502c — where every displaced out actually lands,
                        # MEASURED over 357 games / 19,072 outs (131 displaced):
                        #   * 53 pickoff-family outs -> `raw.play_events`
                        #   * 63 mid-plate-appearance caught stealings -> the
                        #     `sb_attempt_*`/`sb_success_*` columns on this pitch
                        #     row (the scan above), which the IP formula counts
                        #   * 7 batter strikeouts the feed anchors to a trailing
                        #     `no_pitch` event -> the PA's `events` column, the
                        #     canonical out label since SIM-501a
                        #   * ~8 runner outs on `other_out`/`wild_pitch` actions
                        #     reach NO table when they do not end the PA —
                        #     ~0.04% of all outs, accepted and documented in
                        #     pipeline/statcast_events.py.
                        # An earlier version of this comment claimed ALL displaced
                        # outs reach `raw.play_events`. That was false for 78 of
                        # the 131.
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
                # SIM-501: this read a running variable that was updated AFTER the
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
            prev_half = top_bot
        # SIM-501: carry the POST-play out count so a pitch-less play that follows
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
    # SIM-502: ride on game_dict rather than widening the return. This function
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

        venue_id = _resolve_venue_id(game_pk, gd)  # SIM-447
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
        # SIM-545: the official per-player box score (the prop ground truth) from
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

        ``force=True`` skips the existence short-circuit and re-fetches, so an
        already-present row can be CORRECTED. The INSERT below has always carried
        ``ON CONFLICT … DO UPDATE``, so the only thing standing between a bad row
        and a good one was the early return. Used by
        ``scripts/reload_games.py --refresh-venues`` to repair the SIM-447 blank
        venue_name; a plain FK-satisfying row cannot simply be deleted and reloaded
        because raw.pitches references it.
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
                        # SIM-447: was `dimensions.get("venu_name_short", " ")` — a
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

        # --- 5. Game record -----------------------------------------------------

    #: SIM-545: tri-state cache of "does ``raw.game_player_stats`` exist" (Alembic
    #: 0023). ``None`` means not yet probed. Same reasoning as
    #: ``_play_events_table_exists``: one catalogue lookup per run, not per game.
    _game_player_stats_table_exists: bool | None = None
    #: SIM-427: the same tri-state cache for ``raw.game_bullpen`` (Alembic 0025).
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

        The official box-score ground truth (SIM-545): the live feed already
        carries the box under ``liveData.boxscore.teams``, so the loader writes
        it from the payload it holds — no extra HTTP call. The parser
        (:func:`pipeline.etl.boxscore_ingest.parse_boxscore`) emits one row per
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
                        if self._game_player_stats_table_exists is None:
                            self._game_player_stats_table_exists = pg_relation_exists(
                                cur, "raw.game_player_stats"
                            )
                            if not self._game_player_stats_table_exists:
                                log.warning(
                                    "raw.game_player_stats is absent (Alembic 0023 not "
                                    "applied) — the official box score is not stored."
                                )
                        if not self._game_player_stats_table_exists:
                            conn.rollback()
                            return
                        written = persist_sync(cur, rows)
                        # SIM-427: the arms the box lists per side (Alembic
                        # 0025) — the simulator's pen for the game. The same
                        # payload, no extra call; absent table = not stored.
                        pen_written = 0
                        if self._game_bullpen_table_exists is None:
                            self._game_bullpen_table_exists = pg_relation_exists(
                                cur, "raw.game_bullpen"
                            )
                            if not self._game_bullpen_table_exists:
                                log.warning(
                                    "raw.game_bullpen is absent (Alembic 0025 not applied) "
                                    "— the per-game bullpen listing is not stored."
                                )
                        if self._game_bullpen_table_exists:
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
# NOTE (SIM-083): FRESHNESS_TABLE_DDL was removed from this file.
# The raw.etl_data_freshness CREATE TABLE DDL now lives in:
#   db/schemas/01_postgres_schema.sql
# Applied via Alembic migration 0003 (db/migrations/versions/0003_*.py).
# SIM-090: connection pooling — see HistoricalDataLoader._ensure_pool / .close().
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # SIM-443b: no default password. The loader reads BASEBALL_DB_DSN and raises
    # a clear error when it is not set.
    loader = HistoricalDataLoader()
    try:
        loader.refresh_seasons()
    finally:
        # SIM-090: tear the pool down so all pooled sockets are released.
        loader.close()
