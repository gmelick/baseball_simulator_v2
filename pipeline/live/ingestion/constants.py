"""The live pipeline's settings: the MLB endpoints, the poll and reconnect
intervals, the cache lifetimes, the re-simulation cooldown and the odds cadences.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MLB_BASE = "https://statsapi.mlb.com"
MLB_WS_TEMPLATE = "wss://ws.statsapi.mlb.com/api/v1/game/push/subscribe/gameday/{game_pk}"
SCHEDULE_URL = f"{MLB_BASE}/api/v1/schedule"
#: The schedule poll asks for each game's probable pitchers and posted
#: lineups, so the pre-game odds cycle knows whose props to store before a game
#: has a live feed (``teams.<side>.probablePitcher``, ``lineups.<side>Players``).
SCHEDULE_HYDRATE = "probablePitcher,lineups"

SCHEDULE_POLL_S = 30  # how often to check for newly-live games
#: The league's day turns over in Eastern time.
_EASTERN = ZoneInfo("America/New_York")
WS_RECONNECT_BASE = 2.0  # base seconds for WS reconnect backoff
WS_RECONNECT_MAX = 60.0  # cap on WS reconnect backoff
HTTP_TIMEOUT_S = 10  # aiohttp request timeout
REDIS_TTL_LIVE_S = 60  # cache TTL for live game states
REDIS_TTL_DONE_S = 3600  # cache TTL for completed game states

# The per-game cooldown of the manual /resimulate endpoint: generous enough
# for a user who re-simulates, reads the result and re-simulates again, tight
# enough that spam cannot queue dozens of 100-iteration runs.
RESIM_COOLDOWN_S = 10


GAME_TYPES = ["R", "F", "D", "L", "W", "C", "P"]


# The role a prop-eligible player plays, as _collect_prop_player_roles()
# reports it. A pitcher is asked only for the pitcher markets and a hitter only
# for the batter markets. "both" is the safe default: a player whose role is
# unknown, or a two-way player who bats AND pitches, is asked for every market.
PROP_ROLE_PITCHER = "pitcher"
PROP_ROLE_BATTER = "batter"
PROP_ROLE_BOTH = "both"

#: Boxscore position codes that mark a pitcher (P; some feeds use
#: SP / RP; TWP is a two-way player).
_PITCHER_POSITION_CODES = frozenset({"P", "SP", "RP", "TWP"})

# Prop-odds fetch cadence.  Prop lines move far more slowly than the
# WS refresh rate (a WS message fires on every pitch).  Polling every book ×
# player × prop on every WS signal would issue thousands of redundant writes
# per game, so the prop fetches are gated to at most once per
# PROP_FETCH_CADENCE_S seconds per game; the dedup hash (migration 0013)
# collapses any identical snapshot that still slips through. The game markets
# (all fifteen, every book) ride the same cadence.
PROP_FETCH_CADENCE_S = 60

#: The cadence of the pre-game odds cycle, per game. The schedule poll
#: runs every 30 seconds for every game of the day. Pre-game lines move slowly,
#: so a game in the ``Preview`` state reads the vendor at most once every ten
#: minutes. The in-play cycle keeps PROP_FETCH_CADENCE_S.
PREGAME_ODDS_CADENCE_S = 600

#: The near-start window. Closing lines move most in the last minutes,
#: so once the scheduled start is this close (or past, while the game waits in
#: ``Preview``), the pre-game cycle reads the vendor every
#: PREGAME_NEAR_START_CADENCE_S instead. The live marker promotes the last row
#: fetched before first pitch, so that row is then at most a minute old.
PREGAME_NEAR_START_WINDOW_S = 900

#: The pre-game cadence inside the near-start window: the live cadence.
PREGAME_NEAR_START_CADENCE_S = PROP_FETCH_CADENCE_S
