"""SIM-447: venue resolution for games whose feed omits ``gameData.venue``.

Found by reconciling ``raw.etl_game_ingest`` against ``raw.pitches`` after the
2018-2025 sweep: two games had pitch rows but no ledger row. Reloading them
surfaced a bare ``KeyError: 'venue'`` — both are Field of Dreams games (NYY@CWS
2021-08-12, CHC@CIN 2022-08-11), and MLB ships them with no venue anywhere:
``gameData.venue`` absent, schedule endpoint returning
``{"link": "/api/v1/venues/null"}``.

The dangerous non-fix is ``teams.home.venue``. It is always populated, so it makes
the crash go away — but for these games it reports the home team's REGULAR park,
which would attribute a game played in a cornfield in Iowa to Guaranteed Rate
Field. ``raw.pitches.venue_id`` is NOT NULL with an FK to ``raw.venues``, so a
wrong-but-real venue satisfies every database constraint and would only ever show
up as a quietly distorted park factor. These tests pin the loud-failure behaviour
so that shortcut cannot be reintroduced.
"""

from __future__ import annotations

import pytest

from pipeline.etl.etl_historical_loader import (
    _VENUE_OVERRIDES,
    MissingVenueError,
    _first_nonblank,
    _resolve_venue,
    _resolve_venue_id,
)

# The real shape of a Field of Dreams feed: no "venue" key at all, and the home
# team's own venue present and WRONG for this game.
FOD_GAME_DATA = {
    "teams": {
        "home": {
            "id": 145,
            "abbreviation": "CWS",
            "venue": {"id": 4, "name": "Guaranteed Rate Field"},
        },
        "away": {"id": 147, "abbreviation": "NYY"},
    },
    "datetime": {"officialDate": "2021-08-12"},
}

NORMAL_GAME_DATA = {
    "venue": {"id": 3313, "name": "Target Field"},
    "teams": {"home": {"id": 142}, "away": {"id": 116}},
}


class TestNormalGames:
    def test_venue_comes_from_the_feed_when_present(self):
        assert _resolve_venue(529440, NORMAL_GAME_DATA) == (3313, "Target Field")

    def test_id_helper_agrees_with_the_pair(self):
        assert _resolve_venue_id(529440, NORMAL_GAME_DATA) == 3313

    def test_the_override_map_is_not_consulted_when_the_feed_has_a_venue(self):
        """An override must never silently win over real feed data — otherwise a
        stale map entry would quietly relocate a normally-played game."""
        pk = next(iter(_VENUE_OVERRIDES))
        assert _resolve_venue(pk, NORMAL_GAME_DATA)[0] == 3313

    def test_a_missing_name_does_not_raise(self):
        assert _resolve_venue(1, {"venue": {"id": 17}}) == (17, "")


class TestNeutralSiteOverrides:
    @pytest.mark.parametrize("game_pk", [632924, 663023])
    def test_the_two_field_of_dreams_games_resolve(self, game_pk):
        venue_id, name = _resolve_venue(game_pk, FOD_GAME_DATA)
        assert venue_id == 5445
        assert name == "Field of Dreams"

    @pytest.mark.parametrize("game_pk", [632924, 663023])
    def test_the_home_team_park_is_NOT_used(self, game_pk):
        """THE POINT OF THIS TICKET. Guaranteed Rate Field is venue 4 and sits
        right there in the feed; picking it up would look like a working fix and
        corrupt that park's factor."""
        assert _resolve_venue_id(game_pk, FOD_GAME_DATA) != 4

    def test_override_ids_are_real_venues_not_placeholders(self):
        for game_pk, (venue_id, name) in _VENUE_OVERRIDES.items():
            assert isinstance(venue_id, int) and venue_id > 0, game_pk
            assert name.strip(), f"game {game_pk} override has no venue name"


class TestUnknownNeutralSiteFailsLoudly:
    def test_missing_venue_and_no_override_raises(self):
        with pytest.raises(MissingVenueError):
            _resolve_venue(999999999, FOD_GAME_DATA)

    def test_the_error_says_what_to_do(self):
        """This fires years from now on a future neutral-site game, for someone
        who has never read this module. The message has to carry the fix."""
        with pytest.raises(MissingVenueError) as excinfo:
            _resolve_venue(999999999, FOD_GAME_DATA)
        msg = str(excinfo.value)
        assert "999999999" in msg
        assert "_VENUE_OVERRIDES" in msg
        assert "statsapi.mlb.com/api/v1/venues" in msg
        assert "teams.home.venue" in msg, "must warn against the wrong-but-easy fix"

    def test_an_explicitly_null_venue_is_treated_as_missing(self):
        """The schedule endpoint returns {"link": ".../venues/null"} — a dict with
        no id. A truthiness check on the dict itself would sail past that."""
        data = {**FOD_GAME_DATA, "venue": {"link": "/api/v1/venues/null"}}
        with pytest.raises(MissingVenueError):
            _resolve_venue(999999999, data)

    def test_a_none_venue_is_treated_as_missing(self):
        data = {**FOD_GAME_DATA, "venue": None}
        with pytest.raises(MissingVenueError):
            _resolve_venue(999999999, data)

    def test_it_is_not_a_bare_KeyError(self):
        """A KeyError is what the sweep's per-game isolation swallowed for two
        seasons. The replacement must be a distinct, catchable type."""
        with pytest.raises(MissingVenueError) as excinfo:
            _resolve_venue(999999999, FOD_GAME_DATA)
        assert not isinstance(excinfo.value, KeyError)


class TestVenueNameKeyTypo:
    """SIM-447: ``dimensions.get("venu_name_short", " ")`` — missing 'e' — meant the
    key never matched and ALL 331 rows in raw.venues stored a single space as the
    venue name. The bug survived because " " is truthy, so every `or`-style guard
    downstream accepted it as a real name."""

    def test_whitespace_is_treated_as_blank(self):
        """The exact failure mode: a single space must NOT win.

        Note the blank-handling is enforced one layer down, in ``to_str`` — this
        test binds the CONTRACT, not a particular implementation of it.
        """
        assert _first_nonblank(" ", "Chase Field") == "Chase Field"
        assert _first_nonblank("   ", "\t", "Wrigley Field") == "Wrigley Field"

    def test_first_real_value_wins_in_order(self):
        assert _first_nonblank("Chase Field", "other") == "Chase Field"

    def test_falls_through_none_and_empty(self):
        assert _first_nonblank(None, "", "Field of Dreams") == "Field of Dreams"

    def test_all_blank_yields_empty_string_not_a_space(self):
        """Returning " " again would silently recreate the original bug."""
        assert _first_nonblank(None, "", "  ") == ""

    def test_values_are_stripped(self):
        assert _first_nonblank("  Target Field  ") == "Target Field"

    def test_the_park_factors_key_is_spelled_correctly_at_the_call_site(self):
        """Guards the actual typo. The park-factors payload key is
        ``venue_name_short``; the statcast-venue fallback uses ``name``."""
        import inspect

        from pipeline.etl.etl_historical_loader import HistoricalDataLoader

        src = inspect.getsource(HistoricalDataLoader._ensure_venue)
        code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
        assert 'dimensions.get("venue_name_short")' in code
        assert "venu_name_short" not in code, "the typo is back"
