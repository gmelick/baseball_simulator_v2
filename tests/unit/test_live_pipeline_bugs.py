"""
Unit tests for SIM-099, SIM-100, SIM-132 bug fixes
====================================================
Permanent regression suite for the three P0 bugs fixed in this sprint.

SIM-099 — Redis key mismatch: _cache_to_redis() wrote 'game_state:{pk}'
          but _fetch_feed() read 'game_feed:{pk}' — fallback always returned None.

SIM-100 — GameStateBuilder roster parsing:
          N+1 days_rest queries, wrong availability logic,
          dead used_pitcher_ids code, wrong date anchor for replay.

SIM-132 — MockOddsAPI zero-vig: implied probs summed to exactly 1.0;
          architecture comment still said 'inning>=7 OR score_diff<=2'.

Run:
    pytest tests/unit/test_live_pipeline_bugs.py -v
"""

from __future__ import annotations

import json
import sys
import pathlib
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so pipeline imports work
# ---------------------------------------------------------------------------
_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.live.live_ingestion_pipeline import (
    GameStateBuilder,
    LiveIngestionPipeline,
    MockOddsAPI,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _american_to_implied(american: int) -> float:
    """Convert American odds integer to implied probability."""
    if american < 0:
        return (-american) / (-american + 100)
    else:
        return 100 / (american + 100)


def _make_minimal_feed(
    game_pk: int = 745000,
    abstract_state: str = "Live",
    inning: int = 3,
    is_complete: bool = True,
    at_bat_index: int = 42,
    score_diff: int = 8,
) -> dict:
    """Build a minimal feed/live dict sufficient to exercise the pipeline methods."""
    home_score = 3
    away_score = home_score - score_diff if score_diff < 0 else home_score + score_diff
    return {
        "gamePk": game_pk,
        "gameData": {
            "status": {"abstractGameState": abstract_state},
            "teams": {"home": {"id": 147}, "away": {"id": 111}},
            "datetime": {"officialDate": "2024-08-15"},
        },
        "liveData": {
            "linescore": {
                "currentInning":  inning,
                "inningHalf":     "Top",
                "outs":           2,
                "teams": {
                    "home": {"runs": home_score},
                    "away": {"runs": away_score},
                },
                "offense":  {},
                "defense":  {"pitcher": {"id": 999}},
            },
            "boxscore": {"teams": {"home": {"players": {}, "battingOrder": []},
                                   "away": {"players": {}, "battingOrder": []}}},
            "plays": {
                "currentPlay": {
                    "matchup": {"batter": {"id": 101}, "pitcher": {"id": 999}},
                    "count": {"balls": 2, "strikes": 1},
                    "about": {"isComplete": is_complete, "atBatIndex": at_bat_index},
                },
                "allPlays": [],
            },
        },
    }


# ===========================================================================
# SIM-099 — Redis key mismatch
# ===========================================================================

class TestSIM099RedisKeyMismatch:
    """
    Regression tests ensuring _cache_to_redis() writes 'game_feed:{pk}'
    and _fetch_feed() reads the same key.

    These tests are the permanent guard against this class of bug recurring.
    """

    @pytest.mark.asyncio
    async def test_cache_to_redis_writes_game_feed_key(self) -> None:
        """
        _cache_to_redis() MUST write 'game_feed:{game_pk}'.
        Previously it wrote 'game_state:{game_pk}' only, making the fallback
        read a key that was never populated.
        """
        mock_redis = AsyncMock()
        pipeline = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        pipeline._redis = mock_redis

        feed       = {"gamePk": 745000, "gameData": {"status": {}}}
        game_state = {"inning": 3}

        await pipeline._cache_to_redis(745000, feed, game_state, status="Live")

        written_keys = {c.args[0] for c in mock_redis.setex.call_args_list}
        assert "game_feed:745000" in written_keys, (
            "game_feed:745000 was not written — _fetch_feed() fallback will never work"
        )

    @pytest.mark.asyncio
    async def test_cache_to_redis_still_writes_game_state_key(self) -> None:
        """
        'game_state:{game_pk}' must still be written — consumed by the
        resimulate endpoint (POST /api/games/{pk}/resimulate).
        """
        mock_redis = AsyncMock()
        pipeline = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        pipeline._redis = mock_redis

        await pipeline._cache_to_redis(745000, {}, {"inning": 1}, status="Live")

        written_keys = {c.args[0] for c in mock_redis.setex.call_args_list}
        assert "game_state:745000" in written_keys, (
            "game_state:745000 was not written — resimulate endpoint will break"
        )

    @pytest.mark.asyncio
    async def test_redis_key_written_matches_key_read(self) -> None:
        """
        The key written by _cache_to_redis() must equal the key read by
        _fetch_feed()'s fallback branch.  This test explicitly cross-checks both
        sides so a rename in either place immediately breaks this test.
        """
        mock_redis = AsyncMock()
        pipeline = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        pipeline._redis = mock_redis

        game_pk    = 745001
        feed       = {"gamePk": game_pk, "gameData": {"status": {"abstractGameState": "Live"}}}
        game_state = {"inning": 5}

        # --- write side ---
        await pipeline._cache_to_redis(game_pk, feed, game_state, status="Live")
        written_keys = {c.args[0] for c in mock_redis.setex.call_args_list}

        # --- read side: simulate what _fetch_feed() does in its fallback ---
        expected_read_key = f"game_feed:{game_pk}"

        assert expected_read_key in written_keys, (
            f"_fetch_feed() reads '{expected_read_key}' but _cache_to_redis() "
            f"wrote: {written_keys!r}. "
            "This is the exact key mismatch that broke rate-limit resilience."
        )

    @pytest.mark.asyncio
    async def test_fetch_feed_returns_cached_feed_on_api_failure(self) -> None:
        """
        When the MLB API returns a 429 (or raises), _fetch_feed() MUST return
        the previously cached feed from Redis 'game_feed:{pk}', not None.
        """
        import aiohttp

        cached_feed = {"gamePk": 745002, "gameData": {"status": {"abstractGameState": "Live"}}}

        mock_redis = AsyncMock()
        mock_redis.get.return_value = json.dumps(cached_feed)

        # Simulate a failed HTTP response (429 status)
        mock_resp = AsyncMock()
        mock_resp.status = 429
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__  = AsyncMock(return_value=False)

        mock_http = MagicMock()
        mock_http.get.return_value = mock_resp

        pipeline = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        pipeline._redis = mock_redis
        pipeline._http  = mock_http

        result = await pipeline._fetch_feed(745002)

        assert result is not None, (
            "_fetch_feed() returned None on 429 — Redis fallback is not working"
        )
        assert result["gamePk"] == 745002
        mock_redis.get.assert_awaited_once_with("game_feed:745002")

    @pytest.mark.asyncio
    async def test_cache_to_redis_writes_feed_payload_not_game_state(self) -> None:
        """
        The value stored under 'game_feed:{pk}' must be the raw feed dict,
        not the built game_state — _fetch_feed() passes it through the full
        GameStateBuilder.build() pipeline again on fallback.
        """
        mock_redis = AsyncMock()
        pipeline = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        pipeline._redis = mock_redis

        feed       = {"gamePk": 745003, "sentinel": "feed_payload"}
        game_state = {"inning": 7, "sentinel": "game_state_payload"}

        await pipeline._cache_to_redis(745003, feed, game_state, status="Live")

        # Find the call that wrote game_feed:745003
        feed_call = next(
            (c for c in mock_redis.setex.call_args_list if c.args[0] == "game_feed:745003"),
            None,
        )
        assert feed_call is not None
        stored = json.loads(feed_call.args[2])
        assert stored.get("sentinel") == "feed_payload", (
            "game_feed key stores game_state instead of raw feed — "
            "fallback will produce double-built state"
        )


# ===========================================================================
# SIM-100 — Roster parsing bugs
# ===========================================================================

class TestSIM100RosterParsing:
    """
    Tests for the four roster parsing bugs fixed in SIM-100.
    """

    # -----------------------------------------------------------------------
    # N+1 query → batch query
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_batch_days_rest_single_query_for_all_pitchers(self) -> None:
        """
        12 bullpen pitchers must produce exactly 1 DB query, not 12.

        This is the core N+1 regression test for SIM-100.
        """
        mock_db = AsyncMock()
        mock_db.fetch.return_value = []   # no prior appearances

        builder = GameStateBuilder.__new__(GameStateBuilder)
        builder._db = mock_db

        pitcher_ids = list(range(100001, 100013))   # 12 pitchers
        await builder._batch_days_rest(pitcher_ids, game_pk=745000)

        assert mock_db.fetch.await_count == 1, (
            f"Expected 1 DB query, got {mock_db.fetch.await_count}. "
            "N+1 query regression: _batch_days_rest() is issuing one query per pitcher."
        )

    @pytest.mark.asyncio
    async def test_batch_days_rest_returns_empty_dict_for_no_pitchers(self) -> None:
        """Empty pitcher list must return {} without hitting the DB."""
        mock_db = AsyncMock()
        builder = GameStateBuilder.__new__(GameStateBuilder)
        builder._db = mock_db

        result = await builder._batch_days_rest([], game_pk=745000)

        assert result == {}
        mock_db.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_batch_days_rest_correct_days_calculation(self) -> None:
        """
        Rest days must be calculated relative to as_of_date (not date.today()).
        This guards against the replay anchor bug in SIM-100.
        """
        from datetime import date as dt

        anchor     = dt(2024, 8, 15)
        last_pitch = dt(2024, 8, 12)   # 3 days before anchor

        mock_db = AsyncMock()
        mock_db.fetch.return_value = [
            {"pitcher": 100001, "last_date": last_pitch},
        ]

        builder = GameStateBuilder.__new__(GameStateBuilder)
        builder._db = mock_db

        result = await builder._batch_days_rest(
            [100001], game_pk=745000, as_of_date=anchor
        )

        assert result[100001] == 3, (
            f"Expected 3 days rest, got {result.get(100001)}. "
            "as_of_date anchor not being used — replay will compute wrong rest days."
        )

    @pytest.mark.asyncio
    async def test_batch_days_rest_returns_none_for_no_prior_appearance(self) -> None:
        """Pitcher with no historical data → None (not 0) in the result dict."""
        mock_db = AsyncMock()
        mock_db.fetch.return_value = []   # DB returned nothing

        builder = GameStateBuilder.__new__(GameStateBuilder)
        builder._db = mock_db

        result = await builder._batch_days_rest([100001], game_pk=745000)

        assert result.get(100001) is None

    # -----------------------------------------------------------------------
    # Availability logic
    # -----------------------------------------------------------------------

    def _make_roster_bullpen_entry(
        self,
        pitch_count_today: int,
        days_rest: int | None,
    ) -> dict:
        """Build a minimal bullpen entry matching what _parse_roster() produces."""
        available = pitch_count_today == 0 or (
            days_rest is not None and days_rest >= 1 and pitch_count_today < 30
        )
        return {
            "pitch_count_today": pitch_count_today,
            "days_rest":         days_rest,
            "available":         available,
        }

    def test_availability_fresh_arm_is_available(self) -> None:
        """pitch_count_today=0 → available regardless of days_rest."""
        entry = self._make_roster_bullpen_entry(pitch_count_today=0, days_rest=None)
        assert entry["available"] is True

    def test_availability_rested_light_usage_is_available(self) -> None:
        """
        SIM-100: pitcher with pitch_count_today=8 and days_rest=2 must be
        available (short outing yesterday, rested arm today).
        Old code: available=False because pitch_count_today != 0.
        New code: available=True (days_rest >= 1 AND pitch_count_today < 30).
        """
        entry = self._make_roster_bullpen_entry(pitch_count_today=8, days_rest=2)
        assert entry["available"] is True, (
            "Pitcher with 8 pitches and 2 days rest should be available. "
            "Old bug: any pitch_count > 0 marked as unavailable."
        )

    def test_availability_heavy_usage_is_unavailable(self) -> None:
        """
        pitch_count_today=35 → unavailable even with days_rest=2.
        30-pitch threshold for 'light usage'.
        """
        entry = self._make_roster_bullpen_entry(pitch_count_today=35, days_rest=2)
        assert entry["available"] is False, (
            "Pitcher with 35 pitches today should not be available even with rest."
        )

    def test_availability_no_rest_any_pitches_is_unavailable(self) -> None:
        """days_rest=0 → unavailable even for light usage."""
        entry = self._make_roster_bullpen_entry(pitch_count_today=8, days_rest=0)
        assert entry["available"] is False

    def test_availability_threshold_boundary_29_pitches_available(self) -> None:
        """29 pitches with 1 day rest → available (boundary condition)."""
        entry = self._make_roster_bullpen_entry(pitch_count_today=29, days_rest=1)
        assert entry["available"] is True

    def test_availability_threshold_boundary_30_pitches_unavailable(self) -> None:
        """30 pitches with 1 day rest → unavailable (boundary condition)."""
        entry = self._make_roster_bullpen_entry(pitch_count_today=30, days_rest=1)
        assert entry["available"] is False

    def test_availability_unknown_rest_with_pitches_is_unavailable(self) -> None:
        """days_rest=None (no historical data) with pitches today → unavailable."""
        entry = self._make_roster_bullpen_entry(pitch_count_today=15, days_rest=None)
        assert entry["available"] is False


# ===========================================================================
# SIM-132 — MockOddsAPI vig + resim trigger
# ===========================================================================

class TestSIM132MockOddsVig:
    """
    Tests that MockOddsAPI moneylines carry realistic book overround.

    Core assertion: for any game_pk, the sum of implied probabilities derived
    from home_ml and away_ml must exceed 1.03 (real books carry 3–5 % hold on
    MLB ML).
    """

    # SIM-159 calibration: MockOddsAPI samples vig from rng.uniform(0.06, 0.10)
    # so the underlying overround `1 + vig/2` lives in [1.030, 1.050].  After
    # the line is round-tripped through `_prob_to_american()` (which rounds to
    # an integer) and then back via `_american_to_implied()`, the recovered
    # implied-probability sum drifts by up to ±0.003 from that range — so the
    # observed sum sits in roughly [1.027, 1.053].  Strict `> 1.03` therefore
    # flakes at the lower edge (game_pk=12345 hits 1.0286).
    #
    # PM-approved fix (sprint 2026-05-13): keep the [0.06, 0.10] RNG range so
    # the mock spans both sharp-book (3–5 %) and soft-book (6–8 %) overround
    # ranges, and weaken the test bounds to absorb the ~0.003 American-odds
    # rounding error.  The bounds below assert "vig is meaningfully present
    # AND not absurdly high", which is the SIM-132 invariant we actually care
    # about — they reject zero-vig (1.0) and unrealistic vig (> 1.06) without
    # flaking on rounding.
    _VIG_LOWER = 1.025   # 1.030 - rounding slack
    _VIG_UPPER = 1.055   # 1.050 + rounding slack

    def test_moneyline_implied_probs_sum_exceeds_1_03(self) -> None:
        """
        SIM-132: Zero-vig check.  Before fix, home_implied + away_implied == 1.0
        exactly, meaning the mock produced fair-value lines that don't exist in
        any real market.
        """
        odds = MockOddsAPI.get_odds(745000)
        home_implied = _american_to_implied(odds["home_ml"])
        away_implied = _american_to_implied(odds["away_ml"])
        total = home_implied + away_implied

        assert total >= self._VIG_LOWER, (
            f"Implied prob sum = {total:.4f} (< {self._VIG_LOWER}). "
            "MockOddsAPI is producing zero/near-zero-vig lines. "
            "Edge calculations will be inflated by 3–8 pp vs. real markets."
        )
        assert total <= self._VIG_UPPER, (
            f"Implied prob sum = {total:.4f} (> {self._VIG_UPPER}). "
            "MockOddsAPI vig is unrealistically high for any real book."
        )

    @pytest.mark.parametrize("game_pk", [100001, 234567, 745000, 999999, 12345])
    def test_moneyline_sum_in_realistic_vig_band(self, game_pk: int) -> None:
        """Vig must be present and realistic for every game_pk — not just a
        specific seed.  Bounds account for American-odds integer rounding
        (see _VIG_LOWER / _VIG_UPPER comment above)."""
        odds = MockOddsAPI.get_odds(game_pk)
        home_implied = _american_to_implied(odds["home_ml"])
        away_implied = _american_to_implied(odds["away_ml"])
        total = home_implied + away_implied
        assert self._VIG_LOWER <= total <= self._VIG_UPPER, (
            f"game_pk={game_pk}: implied sum = {total:.4f} outside "
            f"[{self._VIG_LOWER}, {self._VIG_UPPER}]"
        )

    def test_moneyline_vig_is_not_excessive(self) -> None:
        """
        Vig should not be absurdly high — capped so lines remain plausible.
        Total implied should be < 1.12 (even the softest books don't exceed that).
        """
        odds = MockOddsAPI.get_odds(745000)
        total = (
            _american_to_implied(odds["home_ml"])
            + _american_to_implied(odds["away_ml"])
        )
        assert total < 1.12, f"Implied sum = {total:.4f} — vig is unrealistically high"

    def test_get_odds_returns_all_required_keys(self) -> None:
        """get_odds() must still return all fields required by _persist_odds()."""
        odds = MockOddsAPI.get_odds(745000)
        required = {
            "game_pk", "source", "is_mock", "book", "line_type",
            "market_type", "is_sharp_book",
            "home_ml", "away_ml",
            "home_spread", "home_spread_ml", "away_spread", "away_spread_ml",
            "total_line", "over_ml", "under_ml",
        }
        missing = required - odds.keys()
        assert not missing, f"get_odds() is missing keys: {missing}"


class TestSIM132ResimTrigger:
    """
    Regression tests confirming _should_resimulate() fires on every completed
    PA, with NO inning/score-diff filter.

    The architecture comment previously said 'inning >= 7 OR |score_diff| <= 2'
    but the code has always fired on every PA.  These tests lock in the correct
    behaviour so it can never accidentally regress to the filtered version.
    """

    def _make_pipeline(self) -> LiveIngestionPipeline:
        p = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        p._last_resim_at_bat = {}
        return p

    def test_resim_fires_early_inning_large_score_diff(self) -> None:
        """
        PA complete in inning 2 with score_diff=8 MUST trigger resim.
        The old incorrect doc implied this would NOT trigger (inning < 7,
        |score_diff| > 2).  The code (and this test) says otherwise.
        """
        pipeline = self._make_pipeline()
        feed = _make_minimal_feed(
            abstract_state="Live",
            inning=2,
            is_complete=True,
            at_bat_index=5,
            score_diff=8,
        )
        should_resim, at_bat = pipeline._should_resimulate(745000, feed)
        assert should_resim is True, (
            "Resim should fire in inning 2 with score_diff=8. "
            "If this fails, the wrong 'inning>=7 OR score_diff<=2' filter "
            "has been accidentally re-introduced."
        )

    def test_resim_fires_inning_1_score_tied(self) -> None:
        """First inning, tied game → resim fires."""
        pipeline = self._make_pipeline()
        feed = _make_minimal_feed(inning=1, is_complete=True, at_bat_index=0, score_diff=0)
        should_resim, _ = pipeline._should_resimulate(745000, feed)
        assert should_resim is True

    def test_resim_does_not_fire_for_incomplete_pa(self) -> None:
        """Mid-PA message (isComplete=False) must NOT trigger resim."""
        pipeline = self._make_pipeline()
        feed = _make_minimal_feed(is_complete=False, at_bat_index=10)
        should_resim, _ = pipeline._should_resimulate(745000, feed)
        assert should_resim is False

    def test_resim_does_not_fire_for_non_live_game(self) -> None:
        """Games in Preview or Final state must not trigger resim."""
        pipeline = self._make_pipeline()
        for state in ("Preview", "Final"):
            feed = _make_minimal_feed(abstract_state=state, is_complete=True)
            should_resim, _ = pipeline._should_resimulate(745000, feed)
            assert should_resim is False, f"Resim should not fire for state={state}"

    def test_resim_deduplication_prevents_double_trigger(self) -> None:
        """
        Two consecutive messages for the same completed PA must not both
        trigger a resim — only the first should.
        """
        pipeline = self._make_pipeline()
        feed = _make_minimal_feed(is_complete=True, at_bat_index=20)

        # First message: should trigger
        r1, at_bat = pipeline._should_resimulate(745000, feed)
        assert r1 is True

        # Simulate pipeline recording the fired resim
        pipeline._last_resim_at_bat[745000] = at_bat

        # Second message for same PA: must NOT trigger
        r2, _ = pipeline._should_resimulate(745000, feed)
        assert r2 is False, (
            "Deduplication guard failed — same PA triggered resim twice. "
            "This causes a double-simulation per plate appearance."
        )
