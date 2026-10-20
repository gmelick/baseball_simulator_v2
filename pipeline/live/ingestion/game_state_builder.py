"""The game-state builder: one live game's feed turned into the state the
simulator starts from.
"""

from __future__ import annotations

import logging
from datetime import (
    UTC,
    date,
    datetime,
)
from typing import Any

import asyncpg

log = logging.getLogger("live_ingestion")


# ---------------------------------------------------------------------------
# Game State Builder
# ---------------------------------------------------------------------------


class GameStateBuilder:
    """
    Transforms the raw MLB Stats API `feed/live` JSON into the game_state
    JSONB structure required by sim.lineup_state.

    Required top-level keys (from schema comment):
      inning, half, outs, balls, strikes,
      home_score, away_score, batting_team_id, fielding_team_id,
      on_1b, on_2b, on_3b, current_batter_id, current_pitcher_id,
      home_lineup, away_lineup, home_bullpen, away_bullpen,
      home_bench, away_bench, play_history

    Per-game caching:
      One builder is now stored per ``game_pk`` on the pipeline
      (LiveIngestionPipeline._builders).  The builder caches:

        * ``_history``          — running list of at-bat-level summaries
        * ``_last_at_bat_index``— the highest atBatIndex parsed so far
        * ``_game_date``        — the game's officialDate (so days_rest is
          right on a replay)

      On each refresh, ``_parse_play_history()`` only walks plays whose
      ``atBatIndex > _last_at_bat_index``, then appends to ``_history``: a
      full rebuild on every socket signal would re-parse and re-serialize
      every play, 20 and more times an inning.
    """

    def __init__(self, db_pool: asyncpg.Pool) -> None:
        self._db = db_pool
        # Per-game caches.  Reset implicitly each time a fresh
        # builder is constructed (one per game_pk).
        self._history: list[dict] = []
        self._last_at_bat_index: int = -1  # -1 = nothing seen yet
        self._game_date: date | None = None

    async def build(self, feed: dict[str, Any]) -> dict[str, Any]:
        """
        Main entry point.  `feed` is the parsed JSON from
        GET /api/v1.1/game/{game_pk}/feed/live
        """
        game_data = feed["gameData"]
        live_data = feed["liveData"]

        game_pk = feed["gamePk"]
        game_status = game_data["status"]["abstractGameState"]  # Live / Final / Preview
        linescore = live_data.get("linescore", {})
        boxscore = live_data.get("boxscore", {})
        current_play = live_data["plays"].get("currentPlay", {})

        home_team_id = game_data["teams"]["home"]["id"]
        away_team_id = game_data["teams"]["away"]["id"]

        # Extract game_date for replay-safe days_rest calculation.
        # officialDate is "YYYY-MM-DD"; None-safe for edge cases.
        _game_date_str = game_data.get("datetime", {}).get("officialDate")
        try:
            _game_date: date | None = date.fromisoformat(_game_date_str) if _game_date_str else None
        except ValueError:
            _game_date = None

        # ---- Inning state --------------------------------------------------
        inning = linescore.get("currentInning", 1)
        half = linescore.get("inningHalf", "Top")  # "Top" | "Bottom"
        outs = linescore.get("outs", 0)
        balls = current_play.get("count", {}).get("balls", 0)
        strikes = current_play.get("count", {}).get("strikes", 0)
        home_score = linescore.get("teams", {}).get("home", {}).get("runs", 0)
        away_score = linescore.get("teams", {}).get("away", {}).get("runs", 0)

        batting_team_id = away_team_id if half == "Top" else home_team_id
        fielding_team_id = home_team_id if half == "Top" else away_team_id

        # ---- Baserunners ---------------------------------------------------
        offense = current_play.get("matchup", {})
        runners = linescore.get("offense", {})
        on_1b = runners.get("first", {}).get("id")
        on_2b = runners.get("second", {}).get("id")
        on_3b = runners.get("third", {}).get("id")

        # ---- Current participants ------------------------------------------
        current_batter_id = offense.get("batter", {}).get("id")

        # Removed dead first assignment from offense dict (matchup is
        # batter-centric; the "pitcher" key there was silently overwritten by
        # the lookup chain below).
        #
        # Resolution order (most authoritative → least):
        #   1. currentPlay.matchup.pitcher  — set mid-PA, most reliable
        #   2. linescore.defense.pitcher    — set between PAs
        #   3. allPlays[-1].matchup.pitcher — final fallback from last recorded play
        #   4. None → WARNING logged; simulation layer handles gracefully
        _all_plays = live_data.get("plays", {}).get("allPlays", [])
        _last_play_pitcher = (
            _all_plays[-1].get("matchup", {}).get("pitcher", {}).get("id") if _all_plays else None
        )
        current_pitcher_id = (
            current_play.get("matchup", {}).get("pitcher", {}).get("id")
            or live_data.get("linescore", {}).get("defense", {}).get("pitcher", {}).get("id")
            or _last_play_pitcher
        )
        if current_pitcher_id is None:
            log.warning(
                "game %s: could not resolve current_pitcher_id from matchup, "
                "linescore.defense, or allPlays — game state may be incomplete",
                game_pk,
            )

        # ---- Lineups -------------------------------------------------------
        # Pass game_date so days_rest is anchored to the game date,
        # not date.today() — required for correct historical replay behaviour.
        home_lineup, home_bullpen, home_bench = await self._parse_roster(
            boxscore, game_pk, side="home", team_id=home_team_id, game_date=_game_date
        )
        away_lineup, away_bullpen, away_bench = await self._parse_roster(
            boxscore, game_pk, side="away", team_id=away_team_id, game_date=_game_date
        )

        # ---- Play history (at-bat level summary) ---------------------------
        # Cache game_date on first refresh.
        if self._game_date is None:
            self._game_date = _game_date
        # Incremental parse — only walk plays beyond what we've
        # already processed.  Returns the cached running history.
        play_history = self._parse_play_history(live_data["plays"].get("allPlays", []))

        # ---- Linescore snapshot (stored for frontend convenience) ----------
        inning_scores = self._parse_linescore(linescore)

        return {
            # Core state
            "game_pk": game_pk,
            "game_status": game_status,
            "inning": inning,
            "half": half,
            "outs": outs,
            "balls": balls,
            "strikes": strikes,
            "home_score": home_score,
            "away_score": away_score,
            "batting_team_id": batting_team_id,
            "fielding_team_id": fielding_team_id,
            "on_1b": on_1b,
            "on_2b": on_2b,
            "on_3b": on_3b,
            "current_batter_id": current_batter_id,
            "current_pitcher_id": current_pitcher_id,
            # Rosters
            "home_lineup": home_lineup,
            "away_lineup": away_lineup,
            "home_bullpen": home_bullpen,
            "away_bullpen": away_bullpen,
            "home_bench": home_bench,
            "away_bench": away_bench,
            # History / display
            "play_history": play_history,
            "inning_scores": inning_scores,
            "last_updated_at": datetime.now(UTC).isoformat(),
        }

    async def _parse_roster(
        self,
        boxscore: dict,
        game_pk: int,
        side: str,
        team_id: int,
        *,
        game_date: date | None = None,
    ) -> tuple[list, list, list]:
        """
        Returns (lineup, bullpen, bench) for one team.

        lineup  — [{player_id, batting_order, position, name, stats_today}]
        bullpen — [{player_id, name, pitch_count_today, days_rest, role, available}]
        bench   — [{player_id, name, position}]

        - Every bullpen pitcher's days_rest comes from one batch query
          (``_batch_days_rest()``), not one query per pitcher.
        - A pitcher is available if he has not thrown today, OR if he has
          rested at least a day and thrown fewer than 30 pitches (a fresh arm's
          single inning).
        - ``game_date`` goes to ``_batch_days_rest()``, so a replay counts rest
          from the game's date, not date.today().
        """
        team_box = boxscore.get("teams", {}).get(side, {})
        players = team_box.get("players", {})  # keyed "ID{player_id}"
        batting_order = team_box.get("battingOrder", [])

        lineup: list[dict] = []
        bench: list[dict] = []

        # Two-pass approach — collect pitchers first, then batch-query
        # days_rest for all of them in a single DB round-trip.
        bullpen_candidates: list[tuple] = []  # (pid, name, pitching_stats, game_stats, pitch_count)

        for _key, player_data in players.items():
            pid = player_data["person"]["id"]
            name = player_data["person"]["fullName"]
            position = player_data.get("position", {}).get("abbreviation", "")
            stats = player_data.get("stats", {})
            game_stats = player_data.get("gameStats", {})
            seq = player_data.get("battingOrder")  # "100", "200" … or None

            # Batting lineup
            if seq is not None:
                batting_pos = int(seq) // 100  # "100" → 1
                batting_stats = stats.get("batting", {}).get("summary", "")
                lineup.append(
                    {
                        "player_id": pid,
                        "name": name,
                        "batting_order": batting_pos,
                        "position": position,
                        "stats_today": batting_stats,
                        "is_current": pid in batting_order,
                    }
                )
                continue

            if position == "P":
                pitching_stats = stats.get("pitching", {})
                pitch_count_today = pitching_stats.get("pitchesThrown", 0)
                # Removed used_pitcher_ids.add(pid) — set was populated
                # but never read anywhere; dead code confirmed by full-file grep.
                bullpen_candidates.append(
                    (pid, name, pitching_stats, game_stats, pitch_count_today)
                )
            else:
                bench.append(
                    {
                        "player_id": pid,
                        "name": name,
                        "position": position,
                        "stats_today": stats.get("batting", {}).get("summary", ""),
                    }
                )

        # Single batch query replaces N per-pitcher queries
        pitcher_ids = [c[0] for c in bullpen_candidates]
        days_rest_cache = await self._batch_days_rest(pitcher_ids, game_pk, as_of_date=game_date)

        bullpen: list[dict] = []
        for pid, name, pitching_stats, game_stats, pitch_count_today in bullpen_candidates:
            days_rest = days_rest_cache.get(pid)  # int | None
            role = self._infer_role(pitching_stats, game_stats)

            # Corrected availability logic.
            # Old (wrong): pitch_count_today == 0
            #   → marks every pitcher who has thrown even 1 pitch as unavailable,
            #     so most of the bullpen is unavailable mid-game.
            # New (correct): fresh arm OR rested arm with light usage
            #   pitch_count_today == 0                           → fully fresh
            #   days_rest >= 1 AND pitch_count_today < 30        → 1+ days rest,
            #     light usage (single short outing) → manager can bring back
            available = pitch_count_today == 0 or (
                days_rest is not None and days_rest >= 1 and pitch_count_today < 30
            )

            bullpen.append(
                {
                    "player_id": pid,
                    "name": name,
                    "pitch_count_today": pitch_count_today,
                    "days_rest": days_rest,
                    "role": role,
                    "era": pitching_stats.get("era", ""),
                    "available": available,
                }
            )

        lineup.sort(key=lambda x: x["batting_order"])
        return lineup, bullpen, bench

    async def _batch_days_rest(
        self,
        pitcher_ids: list[int],
        current_game_pk: int | None = None,
        *,
        game_pk: int | None = None,
        as_of_date: date | None = None,
    ) -> dict[int, int | None]:
        # ``game_pk`` is accepted as an alias of ``current_game_pk``.
        if current_game_pk is None:
            current_game_pk = game_pk
        """
        Each pitcher's days of rest, from a *single* DB query for all the
        bullpen pitchers of a team: two round trips per refresh, not 26 for two
        13-man bullpens.

        Parameters
        ----------
        pitcher_ids
            Player IDs for all pitchers on one team's roster.
        current_game_pk
            Excluded from the look-back so today's game doesn't count as a rest day.
        as_of_date
            Date to calculate rest relative to.  Pass ``game_date`` for historical
            replay; defaults to ``date.today()`` for live games.

        Returns
        -------
        dict mapping player_id → days_rest (int) or None if no prior appearance.
        """
        if not pitcher_ids:
            return {}

        anchor = as_of_date or date.today()

        try:
            rows = await self._db.fetch(
                """
                SELECT pitcher, MAX(game_date) AS last_date
                FROM   raw.pitches
                WHERE  pitcher = ANY($1)
                  AND  game_pk  != $2
                GROUP  BY pitcher
                """,
                pitcher_ids,
                current_game_pk,
            )
        except Exception as exc:
            log.warning("batch days_rest query failed: %s", exc)
            return {}

        result: dict[int, int | None] = {}
        for row in rows:
            if row["last_date"]:
                result[row["pitcher"]] = (anchor - row["last_date"]).days
        return result

    @staticmethod
    def _infer_role(pitching_stats: dict, game_stats: dict) -> str:
        """
        Best-effort role inference from in-game stats.

        An Opener bucket sits between SP and MRP: by innings alone, a pitcher
        who threw 2.0 IP and faced 9 batters would read MRP, and the manager
        engine would treat the opener as a middle reliever available for
        high-leverage re-use.

        Decision order (most specific → least):
          1. SP      — IP ≥ 4.0   (full starter outing)
          2. Opener  — IP < 4.0 AND BF ≥ 9   (deliberate first-inning role)
          3. MRP     — IP ≥ 1.0   (multi-inning relief)
          4. RP      — otherwise  (one-inning specialist)

        A heuristic; a season-level opener rate on
        derived.pitcher_season_metrics would replace it.
        """
        ip = pitching_stats.get("inningsPitched", "0")
        try:
            ip_float = float(str(ip).replace(".1", ".33").replace(".2", ".67"))
        except (ValueError, AttributeError):
            ip_float = 0.0

        # batters_faced lives on game_stats.pitching.battersFaced for live feeds;
        # default to 0 so missing data degrades to the previous IP-only behavior.
        bf = 0
        try:
            bf = int(pitching_stats.get("battersFaced", 0))
        except (TypeError, ValueError):
            bf = 0

        if ip_float >= 4.0:
            return "SP"
        if ip_float < 4.0 and bf >= 9:
            return "Opener"
        if ip_float >= 1.0:
            return "MRP"
        return "RP"

    def _parse_play_history(self, all_plays: list) -> list[dict]:
        """
        Converts allPlays into a lightweight at-bat-level log for the frontend
        play-by-play panel and for simulation replay.

        Incremental parse:
          The builder caches its history list and walks only the plays whose
          ``about.atBatIndex`` is strictly greater than the highest index
          we've already processed.  The terminal at-bat (currentPlay) is
          re-parsed each refresh as long as it hasn't completed (its
          atBatIndex equals _last_at_bat_index until isComplete flips it
          forward), so mid-PA pitch-by-pitch updates still surface in the
          history without duplication.
        """
        if not all_plays:
            return list(self._history)

        # The currentPlay (last in allPlays) may still be in-flight; replay
        # it every refresh until its atBatIndex advances past the cached
        # last-processed index.  This means: while atBatIndex == cache, we
        # update the LAST entry in self._history with the latest pitch
        # detail; once atBatIndex advances, we append the new at-bat.
        new_appended = 0
        replaced_current = False

        for play in all_plays:
            about_idx = play.get("about", {}).get("atBatIndex")
            if about_idx is None:
                continue

            entry = self._build_history_entry(play)

            if about_idx > self._last_at_bat_index:
                # Brand-new at-bat — append.
                self._history.append(entry)
                self._last_at_bat_index = about_idx
                new_appended += 1
            elif about_idx == self._last_at_bat_index and self._history:
                # Same at-bat as last refresh — refresh the in-flight entry's
                # pitch list / description / event in case the PA updated.
                self._history[-1] = entry
                replaced_current = True
            # about_idx < cache: already permanently captured — skip.

        if new_appended:
            log.debug(
                "SIM-101: parsed %d new plays (cache had %d); replaced_current=%s",
                new_appended,
                len(self._history) - new_appended,
                replaced_current,
            )

        return list(self._history)

    @staticmethod
    def _build_history_entry(play: dict) -> dict:
        """One play's history entry, shared by the incremental parse and any
        reader that reformats a single play."""
        about = play.get("about", {})
        result = play.get("result", {})
        matchup = play.get("matchup", {})
        pitches = [
            {
                "pitch_number": e.get("pitchNumber"),
                "pitch_type": e.get("details", {}).get("type", {}).get("code"),
                "pitch_name": e.get("details", {}).get("type", {}).get("description"),
                "description": e.get("details", {}).get("description"),
                "speed": e.get("pitchData", {}).get("startSpeed"),
                "balls": e.get("count", {}).get("balls"),
                "strikes": e.get("count", {}).get("strikes"),
                "outs": e.get("count", {}).get("outs"),
            }
            for e in play.get("playEvents", [])
            if e.get("isPitch")
        ]
        return {
            "at_bat_number": about.get("atBatIndex", 0) + 1,
            "inning": about.get("inning"),
            "half": about.get("halfInning", "").capitalize(),
            "batter_id": matchup.get("batter", {}).get("id"),
            "batter_name": matchup.get("batter", {}).get("fullName"),
            "pitcher_id": matchup.get("pitcher", {}).get("id"),
            "pitcher_name": matchup.get("pitcher", {}).get("fullName"),
            "event": result.get("eventType"),
            "description": result.get("description"),
            "rbi": result.get("rbi", 0),
            "pitches": pitches,
        }

    @staticmethod
    def _parse_linescore(linescore: dict) -> dict:
        """Extracts inning-by-inning scoring in the shape the frontend expects."""
        innings_raw = linescore.get("innings", [])
        home_innings = [i.get("home", {}).get("runs") for i in innings_raw]
        away_innings = [i.get("away", {}).get("runs") for i in innings_raw]
        return {
            "home": home_innings,
            "away": away_innings,
            "home_runs": linescore.get("teams", {}).get("home", {}).get("runs", 0),
            "home_hits": linescore.get("teams", {}).get("home", {}).get("hits", 0),
            "home_errors": linescore.get("teams", {}).get("home", {}).get("errors", 0),
            "away_runs": linescore.get("teams", {}).get("away", {}).get("runs", 0),
            "away_hits": linescore.get("teams", {}).get("away", {}).get("hits", 0),
            "away_errors": linescore.get("teams", {}).get("away", {}).get("errors", 0),
        }
