"""
tests/unit/test_sim478_geometry_builder.py
==========================================
The fence certification (SIM-478/479/480; the plan at
docs/audit/2026-09-20-sim478-480-fence-certification-plan.md, §4 and §5.1):
the version-2 park-geometry builder's pure pieces on toy numpy balls (no
DuckDB), and the Stats API venue-dimensions reader with ``urllib`` mocked.

What these tests pin:

  * ``sector_of`` at the edges of the +-55 grid (eleven sectors, clamped);
  * ``sector_line``'s three sources and the thin fallbacks;
  * ``published_at``'s interpolation of the five published points;
  * the prior: a park with no balls rests on the prior; a park with four
    home runs blends; a park with twelve keeps its own;
  * ``detect_moves``: a 20-ft move splits the seasons at the move; a 6-ft
    change does not; one season is one group; a skipped season still groups;
  * the group key and the latest-group rule;
  * overrides in both shapes;
  * ``build_geometry_document``'s document holds every key of the contract
    with eleven sectors everywhere;
  * ``carry_offsets`` (the plan's §11): a park whose home runs carry +20 ft
    on top of the league's law reads +20.0 and the others 0; a park under
    100 home runs gets none; the median centring; balls without the two
    factors give no offsets; the document carries the two keys;
  * ``pipeline/etl/mlb_venue_dimensions.py``: the fetch on a canned body, the
    write that keeps the last copy when the API fails, the read.
"""

from __future__ import annotations

import http.client
import io
import json
import urllib.error

import numpy as np
import pytest

from pipeline.batch.engine_artifacts import (
    CARRY_OFFSET_FEATURES,
    PARK_N_SECTORS,
    PARK_OFFSET_MIN_HR,
    PARK_PRIOR_N,
    PARK_SECTOR_DEG,
    PARK_SPRAY_MAX,
    PARK_SPRAY_MIN,
    build_geometry_document,
    carry_offsets,
    detect_moves,
    group_for_season,
    group_key,
    league_offsets,
    parse_group_key,
    prior_lines,
    published_at,
    sector_line,
    sector_of,
)
from pipeline.etl import mlb_venue_dimensions as mvd

_SEASONS = [2023, 2024, 2025, 2026]
_MID = [PARK_SPRAY_MIN + (s + 0.5) * PARK_SECTOR_DEG for s in range(PARK_N_SECTORS)]


# ===========================================================================
# The toy balls
# ===========================================================================


class Balls:
    """A builder of ``load_air_balls``-shaped dicts. A ball's exit velocity
    and launch angle (``ev`` / ``la``) are NaN unless given, so the fence
    tests read as before and the carry offset fits on nothing."""

    def __init__(self) -> None:
        self.rows: list[tuple[int, int, float, float, bool, float, float]] = []

    def add(self, venue, season, sec, dist, hr, n=1, spray=None, ev=None, la=None):
        """``n`` balls at the sector's midpoint angle (or ``spray``)."""
        angle = _MID[sec] if spray is None else spray
        ev_f = np.nan if ev is None else float(ev)
        la_f = np.nan if la is None else float(la)
        for _ in range(n):
            self.rows.append(
                (int(venue), int(season), float(angle), float(dist), bool(hr), ev_f, la_f)
            )
        return self

    def hr(self, venue, season, sec, dist, n=1, ev=None, la=None):
        return self.add(venue, season, sec, dist, True, n, ev=ev, la=la)

    def kept(self, venue, season, sec, dist, n=1, ev=None, la=None):
        return self.add(venue, season, sec, dist, False, n, ev=ev, la=la)

    def build(self, with_factors: bool = True) -> dict[str, np.ndarray]:
        """The dict; ``with_factors=False`` leaves out ``ev`` / ``la`` (the
        shape an older caller, such as the check script's fixture, builds)."""
        venue = np.asarray([r[0] for r in self.rows], dtype=np.int64)
        season = np.asarray([r[1] for r in self.rows], dtype=np.int64)
        spray = np.asarray([r[2] for r in self.rows], dtype=np.float64)
        dist = np.asarray([r[3] for r in self.rows], dtype=np.float64)
        hr = np.asarray([r[4] for r in self.rows], dtype=bool)
        out = {
            "venue": venue,
            "season": season,
            "spray": spray,
            "dist": dist,
            "hr": hr,
            "kept300": (~hr) & (dist >= 300.0),
        }
        if with_factors:
            out["ev"] = np.asarray([r[5] for r in self.rows], dtype=np.float64)
            out["la"] = np.asarray([r[6] for r in self.rows], dtype=np.float64)
        return out


def _one_venue(balls: dict[str, np.ndarray], venue: int) -> dict[str, np.ndarray]:
    m = balls["venue"] == venue
    out = {k: v[m] for k, v in balls.items()}
    out["sec"] = sector_of(out["spray"])
    return out


def _fenced_venue(b: Balls, venue: int, seasons, sec: int, hr_ft: float, kept_ft: float, n=20):
    """A sector whose line is the midpoint of ``hr_ft`` (every home run) and
    ``kept_ft`` (every kept ball), ``n`` of each per season."""
    for y in seasons:
        b.hr(venue, y, sec, hr_ft, n=n)
        b.kept(venue, y, sec, kept_ft, n=n)
    return b


# ===========================================================================
# (a) the sector index
# ===========================================================================


class TestSectorOf:
    def test_edges_of_the_grid(self):
        spray = np.array([-55.0, -45.1, -45.0, 0.0, 54.9, 60.0, -60.0])
        out = sector_of(spray, PARK_SPRAY_MIN, PARK_SECTOR_DEG, PARK_N_SECTORS)
        assert out.tolist() == [0, 0, 1, 5, 10, 10, 0]
        assert out.dtype == np.int64

    def test_reads_the_grid_it_is_given(self):
        # The old nine-sector document: -45 is sector 0, 0 is sector 4.
        assert sector_of(np.array([-45.0, 0.0, 44.9]), -45.0, 10, 9).tolist() == [0, 4, 8]

    def test_the_constants(self):
        assert (PARK_SPRAY_MIN, PARK_SPRAY_MAX, PARK_N_SECTORS) == (-55.0, 55.0, 11)


# ===========================================================================
# (b) the line rule
# ===========================================================================


class TestSectorLine:
    def test_both(self):
        hr = np.arange(340.0, 370.0)  # 30 home runs: 10th pct 342.9
        kept = np.arange(300.0, 330.0)  # 30 kept: 99th pct 328.71
        line, src, n_hr, n_kept = sector_line(hr, kept)
        assert (src, n_hr, n_kept) == ("both", 30, 30)
        assert line == pytest.approx((342.9 + 328.71) / 2, abs=0.06)

    def test_hr_alone_when_kept_is_thin(self):
        line, src, n_hr, n_kept = sector_line(np.arange(400.0, 420.0), np.array([250.0] * 30))
        # Kept balls under 300 ft count toward the quantile but not the support.
        assert (line, src, n_hr, n_kept) == (401.9, "hr", 20, 0)

    def test_kept_alone_when_home_runs_are_thin(self):
        line, src, n_hr, n_kept = sector_line(np.array([380.0] * 4), np.array([350.0] * 12))
        assert (line, src, n_hr, n_kept) == (350.0, "kept", 4, 12)

    def test_kept_quantile_runs_over_all_kept_balls(self):
        # Ten kept balls of 300+ give support; the 99th percentile reads every kept ball.
        kept = np.array([200.0] * 90 + [350.0] * 10)
        line, src, _n_hr, n_kept = sector_line(np.array([]), kept)
        assert src == "kept" and n_kept == 10
        assert line == pytest.approx(float(np.quantile(kept, 0.99)), abs=0.06)

    def test_no_estimate(self):
        assert sector_line(np.array([400.0] * 9), np.array([350.0] * 9)) == (None, None, 9, 9)
        assert sector_line(np.array([]), np.array([])) == (None, None, 0, 0)


# ===========================================================================
# (c) the published fence at a sector
# ===========================================================================

_PUB = {"leftLine": 330, "leftCenter": 375, "center": 400, "rightCenter": 375, "rightLine": 330}


class TestPublishedAt:
    def test_interpolation(self):
        assert published_at(_PUB, 5) == 400.0  # centre
        # Sector 1's midpoint is -40: between the line (-45, 330) and the alley (-22.5, 375).
        assert published_at(_PUB, 1) == pytest.approx(330 + (375 - 330) * 5 / 22.5)
        assert published_at(_PUB, 9) == pytest.approx(330 + (375 - 330) * 5 / 22.5)
        # The outer sectors (midpoints -50 / +50) clamp to the line distance.
        assert published_at(_PUB, 0) == 330.0 and published_at(_PUB, 10) == 330.0
        # Sector 3's midpoint is -20: between the alley (-22.5, 375) and centre (0, 400).
        assert published_at(_PUB, 3) == pytest.approx(375 + 25 * 2.5 / 22.5)

    def test_missing_points(self):
        assert published_at(None, 5) is None and published_at({}, 5) is None
        # Without the alleys, the line and the centre carry the interpolation.
        three = {"leftLine": 330, "center": 400, "rightLine": 330}
        assert published_at(three, 1) == pytest.approx(330 + 70 * 5 / 45)
        # A missing left line leaves the left sectors without a point on that side.
        no_left = {"leftCenter": 375, "center": 400, "rightCenter": 375, "rightLine": 330}
        assert published_at(no_left, 0) is None and published_at(no_left, 1) is None
        assert published_at(no_left, 3) == pytest.approx(375 + 25 * 2.5 / 22.5)
        assert published_at({"center": 400}, 5) is None  # one point is no line

    def test_prior_lines_add_the_offset(self):
        offset: list[float | None] = [None] + [10.0] * 9 + [None]
        line = prior_lines(_PUB, offset)
        assert len(line) == 11 and line[0] is None and line[10] is None
        assert line[5] == 410.0 and line[1] == pytest.approx(330 + 45 * 5 / 22.5 + 10, abs=0.06)
        assert prior_lines(None, offset) == [None] * 11


# ===========================================================================
# (d) the prior on thin parks
# ===========================================================================


def _league_of_six(b: Balls, published: dict[int, dict]) -> None:
    """Six parks with a full grid of both-source lines and published
    distances, so every sector's league offset exists."""
    for v in range(101, 107):
        for s in range(PARK_N_SECTORS):
            deep = 60.0 * (1 - abs(_MID[s]) / 55.0)  # a fence deepest in centre
            _fenced_venue(b, v, [2024], s, 350.0 + deep + 10, 350.0 + deep - 10)
        published[v] = dict(_PUB)


class TestThePrior:
    def test_league_offset_needs_five_parks_with_both(self):
        b, pub = Balls(), {}
        _league_of_six(b, pub)
        doc = build_geometry_document(b.build(), [2024], pub)
        assert all(x is not None for x in doc["league_offset"])
        # Every park's own line is 350 + deep; the offset is that minus the published fence.
        assert doc["league_offset"][5] == pytest.approx(410.0 - 400.0, abs=0.06)
        # Four parks: no offset anywhere.
        few = Balls()
        for v in range(101, 105):
            _fenced_venue(few, v, [2024], 5, 420.0, 400.0)
        assert build_geometry_document(few.build(), [2024], pub)["league_offset"] == [None] * 11

    def test_offset_reads_only_hr_supported_lines(self):
        own_lines = {v: [400.0] * 11 for v in range(1, 8)}
        srcs = {v: ["kept"] * 11 for v in range(1, 8)}
        pub = dict.fromkeys(range(1, 8), _PUB)
        assert league_offsets(own_lines, srcs, pub) == [None] * 11
        srcs = {v: ["hr"] * 11 for v in range(1, 8)}
        off = league_offsets(own_lines, srcs, pub)
        assert off[5] == 0.0 and off[0] == 70.0

    def test_a_park_with_no_balls_rests_on_the_prior(self):
        b, pub = Balls(), {}
        _league_of_six(b, pub)
        pub[5355] = {
            "leftLine": 340,
            "leftCenter": 380,
            "center": 415,
            "rightCenter": 380,
            "rightLine": 340,
        }
        # One ball keeps the venue in the pool; it is far from the fence.
        b.kept(5355, 2024, 5, 200.0)
        doc = build_geometry_document(b.build(), [2024], pub)
        g = doc["venues"]["5355"]
        assert list(g) == ["2024-2024"]
        assert doc["source"]["5355"]["2024-2024"] == ["prior"] * 11
        assert g["2024-2024"] == doc["prior"]["5355"]["line"]
        assert g["2024-2024"][5] == pytest.approx(415.0 + doc["league_offset"][5], abs=0.06)
        assert "league" not in doc["source"]["5355"]["2024-2024"]

    def test_a_published_park_the_pool_never_saw_gets_the_prior_alone(self):
        b, pub = Balls(), {}
        _league_of_six(b, pub)
        pub[5355] = {
            "leftLine": 340,
            "leftCenter": 380,
            "center": 415,
            "rightCenter": 380,
            "rightLine": 340,
        }
        # No ball at venue 5355: the plan's "prior alone at n_hr = 0".
        doc = build_geometry_document(b.build(), [2023, 2024], pub)
        assert "5355" in doc["prior"]
        assert list(doc["venues"]["5355"]) == ["2023-2024"]
        assert doc["source"]["5355"]["2023-2024"] == ["prior"] * 11
        assert doc["venues"]["5355"]["2023-2024"] == doc["prior"]["5355"]["line"]
        assert doc["support_hr"]["5355"]["2023-2024"] == [0] * 11
        assert doc["support_kept"]["5355"]["2023-2024"] == [0] * 11
        # A published park with no offset anywhere gets nothing: four parks
        # cannot make a league offset, so the prior line is all None.
        few = Balls()
        for v in range(101, 105):
            _fenced_venue(few, v, [2024], 5, 420.0, 400.0)
        doc_few = build_geometry_document(few.build(), [2024], pub)
        assert "5355" not in doc_few["prior"] and "5355" not in doc_few["venues"]

    def test_a_park_with_four_home_runs_blends(self):
        b, pub = Balls(), {}
        _league_of_six(b, pub)
        pub[9] = dict(_PUB)
        b.hr(9, 2024, 5, 430.0, n=4).kept(9, 2024, 5, 380.0, n=12)  # own line: kept 380
        doc = build_geometry_document(b.build(), [2024], pub)
        prior = doc["prior"]["9"]["line"][5]
        expect = (4 * 380.0 + PARK_PRIOR_N * prior) / (4 + PARK_PRIOR_N)
        assert doc["source"]["9"]["2024-2024"][5] == "blend"
        assert doc["venues"]["9"]["2024-2024"][5] == pytest.approx(expect, abs=0.06)
        assert doc["support_hr"]["9"]["2024-2024"][5] == 4

    def test_a_park_with_twelve_home_runs_keeps_its_own(self):
        b, pub = Balls(), {}
        _league_of_six(b, pub)
        pub[9] = dict(_PUB)
        b.hr(9, 2024, 5, 430.0, n=12)
        doc = build_geometry_document(b.build(), [2024], pub)
        assert doc["source"]["9"]["2024-2024"][5] == "hr"
        assert doc["venues"]["9"]["2024-2024"][5] == 430.0

    def test_no_prior_falls_to_the_league_line(self):
        b, pub = Balls(), {}
        _league_of_six(b, pub)
        b.hr(9, 2024, 5, 430.0, n=4)  # no published distances for venue 9
        doc = build_geometry_document(b.build(), [2024], pub)
        assert doc["source"]["9"]["2024-2024"][5] == "league"
        assert doc["venues"]["9"]["2024-2024"][5] == doc["league"][5]
        assert "9" not in doc["prior"]


# ===========================================================================
# (e) the change detector
# ===========================================================================


class TestDetectMoves:
    def test_a_twenty_foot_move_splits_at_the_move(self):
        b = Balls()
        _fenced_venue(b, 2, [2023, 2024], 3, 400.0, 390.0)  # line 395
        _fenced_venue(b, 2, [2025, 2026], 3, 380.0, 370.0)  # line 375
        groups, moves = detect_moves(_one_venue(b.build(), 2), _SEASONS)
        assert groups == [(2023, 2024), (2025, 2026)]
        assert len(moves) == 1
        m = moves[0]
        assert (m["venue"], m["sectors"], m["from"], m["to"]) == (2, [3], 2024, 2025)
        assert m["early"] == [395.0] and m["late"] == [375.0]

    def test_a_six_foot_change_does_not_split(self):
        b = Balls()
        _fenced_venue(b, 2, [2023, 2024], 3, 400.0, 390.0)
        _fenced_venue(b, 2, [2025, 2026], 3, 394.0, 384.0)
        groups, moves = detect_moves(_one_venue(b.build(), 2), _SEASONS)
        assert groups == [(2023, 2026)] and moves == []

    def test_a_move_on_thin_support_does_not_split(self):
        b = Balls()
        _fenced_venue(b, 2, [2023, 2024], 3, 400.0, 390.0)
        _fenced_venue(b, 2, [2025, 2026], 3, 380.0, 370.0, n=1)  # 2 home runs late: under 10
        groups, moves = detect_moves(_one_venue(b.build(), 2), _SEASONS)
        assert groups == [(2023, 2026)] and moves == []

    def test_an_hr_only_move_splits_at_the_move_not_before_it(self):
        # No kept balls: every boundary before the move reads the late block's
        # 10th percentile, a tie the later boundary wins.
        b = Balls()
        for y in (2023, 2024):
            b.hr(2, y, 3, 395.0, n=20)
        for y in (2025, 2026):
            b.hr(2, y, 3, 375.0, n=20)
        groups, moves = detect_moves(_one_venue(b.build(), 2), _SEASONS)
        assert groups == [(2023, 2024), (2025, 2026)]
        assert [(m["from"], m["to"]) for m in moves] == [(2024, 2025)]

    def test_one_season_is_one_group(self):
        b = _fenced_venue(Balls(), 5355, [2026], 5, 420.0, 400.0)
        assert detect_moves(_one_venue(b.build(), 5355), _SEASONS) == ([(2026, 2026)], [])

    def test_a_skipped_season_still_groups(self):
        b = _fenced_venue(Balls(), 7, [2023, 2025, 2026], 5, 420.0, 400.0)
        assert detect_moves(_one_venue(b.build(), 7), _SEASONS) == ([(2023, 2026)], [])

    def test_seasons_outside_the_window_are_ignored(self):
        b = _fenced_venue(Balls(), 7, [2019, 2024], 5, 420.0, 400.0)
        assert detect_moves(_one_venue(b.build(), 7), _SEASONS) == ([(2024, 2024)], [])
        assert detect_moves(_one_venue(b.build(), 7), [2027]) == ([], [])

    def test_two_moves_give_three_groups(self):
        b = Balls()
        _fenced_venue(b, 2, [2023], 3, 400.0, 390.0)
        _fenced_venue(b, 2, [2024, 2025], 3, 380.0, 370.0)
        _fenced_venue(b, 2, [2026], 3, 360.0, 350.0)
        groups, moves = detect_moves(_one_venue(b.build(), 2), _SEASONS)
        assert groups == [(2023, 2023), (2024, 2025), (2026, 2026)]
        assert [(m["from"], m["to"]) for m in moves] == [(2023, 2024), (2025, 2026)]

    def test_the_document_carries_the_groups(self):
        b = Balls()
        _fenced_venue(b, 2, [2023, 2024], 3, 400.0, 390.0)
        _fenced_venue(b, 2, [2025, 2026], 3, 380.0, 370.0)
        doc = build_geometry_document(b.build(), _SEASONS)
        assert list(doc["venues"]["2"]) == ["2023-2024", "2025-2026"]
        assert doc["venues"]["2"]["2023-2024"][3] == 395.0
        assert doc["venues"]["2"]["2025-2026"][3] == 375.0
        assert doc["support_hr"]["2"]["2023-2024"][3] == 40
        assert doc["moves"] == [
            {
                "venue": 2,
                "sectors": [3],
                "from": 2024,
                "to": 2025,
                "early": [395.0],
                "late": [375.0],
            }
        ]


# ===========================================================================
# (f) the group key and the latest-group rule
# ===========================================================================


class TestGroupKeys:
    def test_key_format(self):
        assert group_key(2023, 2024) == "2023-2024" and group_key(2026, 2026) == "2026-2026"
        assert parse_group_key("2023-2024") == (2023, 2024)

    def test_group_for_season(self):
        groups = {"2023-2024": [1.0], "2025-2026": [2.0]}
        assert group_for_season(groups, 2023) == "2023-2024"
        assert group_for_season(groups, 2024) == "2023-2024"
        assert group_for_season(groups, 2025) == "2025-2026"
        # Outside every group, or no season: the latest group.
        assert group_for_season(groups, 2019) == "2025-2026"
        assert group_for_season(groups, 2030) == "2025-2026"
        assert group_for_season(groups, None) == "2025-2026"
        # A skipped season inside a group's range belongs to it.
        assert group_for_season({"2023-2026": [1.0]}, 2024) == "2023-2026"
        assert group_for_season({}, 2024) is None


# ===========================================================================
# (g) overrides
# ===========================================================================


class TestOverrides:
    def _doc(self, overrides):
        b = Balls()
        _fenced_venue(b, 2, [2023, 2024], 3, 400.0, 390.0)
        _fenced_venue(b, 2, [2025, 2026], 3, 380.0, 370.0)
        _fenced_venue(b, 7, [2024], 5, 420.0, 400.0)
        return build_geometry_document(b.build(), _SEASONS, None, overrides)

    def test_flat_overrides_apply_to_every_group(self):
        doc = self._doc({"2": {"3": 388.0, "0": 300.0}})
        for g in ("2023-2024", "2025-2026"):
            assert doc["venues"]["2"][g][3] == 388.0 and doc["venues"]["2"][g][0] == 300.0
            assert doc["source"]["2"][g][3] == "override" and doc["source"]["2"][g][0] == "override"
        assert doc["overrides"] == {"2": {"3": 388.0, "0": 300.0}}

    def test_group_overrides_apply_to_that_group(self):
        doc = self._doc({"2": {"2025-2026": {"3": 372.0}}})
        assert doc["venues"]["2"]["2025-2026"][3] == 372.0
        assert doc["source"]["2"]["2025-2026"][3] == "override"
        assert doc["venues"]["2"]["2023-2024"][3] == 395.0
        assert doc["source"]["2"]["2023-2024"][3] == "both"

    def test_an_unseen_venue_starts_from_the_league_line(self):
        doc = self._doc({"9": {"5": 405.0}})
        assert list(doc["venues"]["9"]) == ["2023-2026"]
        assert doc["venues"]["9"]["2023-2026"][5] == 405.0
        assert doc["venues"]["9"]["2023-2026"][3] == doc["league"][3]
        assert doc["source"]["9"]["2023-2026"][5] == "override"

    def test_a_group_override_on_an_unseen_group_creates_it(self):
        doc = self._doc({"7": {"2025-2026": {"5": 410.0}}})
        assert set(doc["venues"]["7"]) == {"2024-2024", "2025-2026"}
        assert doc["venues"]["7"]["2025-2026"][5] == 410.0
        assert doc["venues"]["7"]["2024-2024"][5] == 410.0  # its own line, untouched
        assert doc["source"]["7"]["2024-2024"][5] == "both"
        # The created group carries the same keys as a built one: its
        # sources (no prior here, so the league line) and zero support.
        assert doc["source"]["7"]["2025-2026"][5] == "override"
        assert doc["source"]["7"]["2025-2026"][3] == "league"
        assert doc["support_hr"]["7"]["2025-2026"] == [0] * 11
        assert doc["support_kept"]["7"]["2025-2026"] == [0] * 11
        assert doc["support_hr"]["7"]["2024-2024"][5] > 0

    def test_a_created_group_seeds_from_the_prior_and_says_so(self):
        b, pub = Balls(), {}
        _league_of_six(b, pub)
        pub[9] = dict(_PUB)
        b.hr(9, 2024, 5, 430.0, n=12)
        doc = build_geometry_document(b.build(), [2024], pub, {"9": {"2025-2026": {"0": 320.0}}})
        created = doc["venues"]["9"]["2025-2026"]
        prior = doc["prior"]["9"]["line"]
        assert created[0] == 320.0 and doc["source"]["9"]["2025-2026"][0] == "override"
        assert created[5] == prior[5] and doc["source"]["9"]["2025-2026"][5] == "prior"
        assert "league" not in doc["source"]["9"]["2025-2026"]
        assert doc["support_hr"]["9"]["2025-2026"] == [0] * 11
        assert doc["support_kept"]["9"]["2025-2026"] == [0] * 11
        # An unseen venue with no prior seeds from the league line, labelled.
        doc = build_geometry_document(b.build(), [2024], pub, {"77": {"5": 405.0}})
        assert doc["source"]["77"]["2024-2024"][3] == "league"
        assert doc["support_hr"]["77"]["2024-2024"] == [0] * 11
        assert doc["support_kept"]["77"]["2024-2024"] == [0] * 11

    def test_a_sector_off_the_grid_is_ignored(self):
        doc = self._doc({"7": {"11": 300.0}})
        assert "override" not in doc["source"]["7"]["2024-2024"]


# ===========================================================================
# (h) the document
# ===========================================================================

_CONTRACT_KEYS = {
    "version",
    "seasons",
    "sector_deg",
    "spray_min",
    "spray_max",
    "n_sectors",
    "league",
    "venues",
    "source",
    "support_hr",
    "support_kept",
    "league_offset",
    "prior",
    "moves",
    "dropped_outside_grid",
    "overrides",
    "carry_offset_ft",
    "carry_offset_fit",
}


class TestTheDocument:
    def test_every_key_with_eleven_sectors(self):
        b, pub = Balls(), {}
        _league_of_six(b, pub)
        _fenced_venue(b, 2, [2023, 2024], 3, 400.0, 390.0)
        _fenced_venue(b, 2, [2025, 2026], 3, 380.0, 370.0)
        pub[2] = dict(_PUB)
        # Three balls outside the grid: dropped and counted.
        b.add(2, 2024, 0, 330.0, True, spray=-58.0).add(2, 2024, 0, 330.0, False, spray=55.0)
        b.add(2, 2024, 0, 330.0, False, spray=70.0)
        doc = build_geometry_document(b.build(), _SEASONS, pub, {"2": {"0": 300.0}})
        assert set(doc) == _CONTRACT_KEYS
        assert doc["version"] == 2 and doc["seasons"] == _SEASONS
        assert (doc["sector_deg"], doc["spray_min"], doc["spray_max"], doc["n_sectors"]) == (
            10,
            -55.0,
            55.0,
            11,
        )
        assert len(doc["league"]) == len(doc["league_offset"]) == 11
        assert doc["dropped_outside_grid"] == 3
        for v, groups in doc["venues"].items():
            for g, line in groups.items():
                first, last = parse_group_key(g)
                assert first <= last
                assert len(line) == 11
                assert len(doc["source"][v][g]) == 11
                assert len(doc["support_hr"][v][g]) == len(doc["support_kept"][v][g]) == 11
                assert all(isinstance(x, int) for x in doc["support_hr"][v][g])
                assert set(doc["source"][v][g]) <= {
                    "both",
                    "hr",
                    "kept",
                    "blend",
                    "prior",
                    "league",
                    "override",
                }
        for p in doc["prior"].values():
            assert set(p) == {"published", "line"} and len(p["line"]) == 11
            assert set(p["published"]) == set(_PUB)
        assert "2" in doc["prior"] and doc["moves"][0]["venue"] == 2
        assert doc["overrides"] == {"2": {"0": 300.0}}
        # The document is JSON: no numpy scalars.
        json.dumps(doc)


# ===========================================================================
# (i) the carry offset (the plan's §11)
# ===========================================================================


def _law(ev: float, la: float, spray: float) -> float:
    """One carry law for the whole league: 5 * ev - 150 at a launch angle
    of 28 degrees, two feet per degree of launch angle, a third of a foot
    per degree of spray (every term inside the offset design's span)."""
    return 5.0 * ev - 150.0 + 2.0 * (la - 28.0) - 0.3 * abs(spray)


def _carry_park(b: Balls, venue: int, extra_ft: float, n: int = 120, season: int = 2024) -> None:
    """``n`` home runs at a park whose air adds ``extra_ft`` to the law.
    The exit velocity, launch angle and spray vary ball by ball so the
    design is full rank."""
    for i in range(n):
        ev = 98.0 + (i % 13) + 0.25 * (i % 4)
        la = 22.0 + (i % 11) * 1.5
        sec = i % PARK_N_SECTORS
        spray = _MID[sec] + ((i % 5) - 2) * 1.5
        b.add(venue, season, sec, _law(ev, la, spray) + extra_ft, True, spray=spray, ev=ev, la=la)


class TestCarryOffsets:
    def test_a_park_that_carries_twenty_feet_reads_plus_twenty(self):
        b = Balls()
        for v in (1, 2, 3, 4, 5, 6):
            _carry_park(b, v, 0.0)
        _carry_park(b, 19, 20.0)
        # A few kept balls with the factors, and one home run without them:
        # neither joins the fit.
        b.kept(19, 2024, 5, 380.0, n=30, ev=104.0, la=30.0)
        b.hr(19, 2024, 5, 450.0, n=5)
        offsets, fit = carry_offsets(b.build())
        assert set(offsets) == {1, 2, 3, 4, 5, 6, 19}
        assert offsets[19] == pytest.approx(20.0, abs=0.6)
        for v in (1, 2, 3, 4, 5, 6):
            assert offsets[v] == pytest.approx(0.0, abs=0.6)
        assert fit["n_hr"] == 7 * 120 and fit["n_parks"] == 7
        assert fit["min_hr"] == PARK_OFFSET_MIN_HR == 100
        assert fit["features"] == list(CARRY_OFFSET_FEATURES)
        assert fit["reference_median_park"] in (1, 2, 3, 4, 5, 6)
        assert fit["residual_sd_ft"] == pytest.approx(0.0, abs=0.05)
        assert all(isinstance(f, float) and round(f, 1) == f for f in offsets.values())

    def test_a_park_under_the_minimum_gets_no_offset(self):
        b = Balls()
        for v in (1, 2, 3, 4, 5, 6):
            _carry_park(b, v, 0.0)
        _carry_park(b, 19, 20.0)
        _carry_park(b, 77, 50.0, n=40)  # forty home runs: dropped from the fit
        offsets, fit = carry_offsets(b.build())
        assert 77 not in offsets
        assert offsets[19] == pytest.approx(20.0, abs=0.6)
        assert fit["n_hr"] == 7 * 120 and fit["n_parks"] == 7
        # The minimum is a parameter: at 40 the park joins.
        offsets_40, fit_40 = carry_offsets(b.build(), min_hr=40)
        assert offsets_40[77] == pytest.approx(50.0, abs=0.6) and fit_40["n_parks"] == 8

    def test_centred_on_the_median_park(self):
        b = Balls()
        for v in (1, 2, 3):
            _carry_park(b, v, 20.0)
        for v in (4, 5, 6):
            _carry_park(b, v, -20.0)
        _carry_park(b, 7, 0.0)
        offsets, fit = carry_offsets(b.build())
        assert offsets[7] == pytest.approx(0.0, abs=0.6)
        for v in (1, 2, 3):
            assert offsets[v] == pytest.approx(20.0, abs=0.6)
        for v in (4, 5, 6):
            assert offsets[v] == pytest.approx(-20.0, abs=0.6)
        assert fit["reference_median_park"] == 7
        # The first park is the fit's reference, so the centre is its gap to the median.
        assert fit["centre_ft"] == pytest.approx(-20.0, abs=0.6)

    def test_one_park_alone_reads_zero(self):
        b = Balls()
        _carry_park(b, 19, 20.0)
        offsets, fit = carry_offsets(b.build())
        assert offsets == {19: 0.0} and fit["n_parks"] == 1 and fit["n_hr"] == 120

    def test_balls_without_the_factors_give_no_offsets(self):
        b = Balls()
        _carry_park(b, 19, 20.0)
        # The check script's fixture builds balls without ``ev`` / ``la``.
        offsets, fit = carry_offsets(b.build(with_factors=False))
        assert offsets == {} and fit["n_hr"] == 0 and fit["n_parks"] == 0
        assert fit["reference_median_park"] is None and fit["residual_sd_ft"] is None
        # The factors present but NaN on every ball: the same answer.
        nan_balls = Balls().hr(19, 2024, 5, 420.0, n=150).build()
        assert carry_offsets(nan_balls) == carry_offsets(b.build(with_factors=False))
        # No balls at all.
        assert carry_offsets(Balls().build())[0] == {}

    def test_the_document_carries_the_offsets(self):
        b, pub = Balls(), {}
        _league_of_six(b, pub)
        for v in (101, 102, 103, 104, 105, 106):
            _carry_park(b, v, 0.0)
        _carry_park(b, 19, 20.0)
        doc = build_geometry_document(b.build(), [2024], pub)
        assert set(doc) == _CONTRACT_KEYS
        assert doc["carry_offset_ft"]["19"] == pytest.approx(20.0, abs=0.6)
        assert set(doc["carry_offset_ft"]) == {"19", "101", "102", "103", "104", "105", "106"}
        assert all(
            isinstance(k, str) and isinstance(f, float) for k, f in doc["carry_offset_ft"].items()
        )
        assert doc["carry_offset_fit"]["n_hr"] == 7 * 120
        assert doc["carry_offset_fit"]["n_parks"] == 7
        json.dumps(doc)
        # The old shape: the keys exist and are empty.
        old = build_geometry_document(b.build(with_factors=False), [2024], pub)
        assert old["carry_offset_ft"] == {}
        assert old["carry_offset_fit"]["n_hr"] == 0
        # The offsets do not touch the lines: the same document otherwise.
        for key in _CONTRACT_KEYS - {"carry_offset_ft", "carry_offset_fit"}:
            assert doc[key] == old[key]


# ===========================================================================
# The Stats API dimensions (urllib mocked)
# ===========================================================================

_BODY = {
    "copyright": "x",
    "venues": [
        {
            "id": 1,
            "name": "Angel Stadium",
            "fieldInfo": {
                "capacity": 45517,
                "turfType": "Grass",
                "roofType": "Open",
                "leftLine": 330,
                "left": 347,
                "leftCenter": 389,
                "center": 396,
                "rightCenter": 365,
                "right": 348,
                "rightLine": 330,
            },
            "season": "2026",
        },
        {
            "id": 5355,
            "name": "Las Vegas Ballpark",
            "fieldInfo": {"leftLine": 340, "center": 415, "rightLine": 340, "roofType": "Open"},
        },
        {"id": 9999, "name": "No field info", "fieldInfo": {"roofType": "Dome"}},
        {"name": "no id"},
    ],
}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class TestVenueDimensions:
    def test_fetch_parses_the_venues_call(self, monkeypatch):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["timeout"] = timeout
            return _Resp(json.dumps(_BODY).encode("utf-8"))

        monkeypatch.setattr(mvd.urllib.request, "urlopen", fake_urlopen)
        out = mvd.fetch_venue_dimensions(2026, timeout=7)
        assert seen["url"] == mvd.VENUES_URL.format(season=2026) and seen["timeout"] == 7
        assert set(out) == {1, 5355, 9999}
        assert out[1] == {
            "name": "Angel Stadium",
            "leftLine": 330.0,
            "leftCenter": 389.0,
            "center": 396.0,
            "rightCenter": 365.0,
            "rightLine": 330.0,
            "left": 347.0,
            "right": 348.0,
            "roofType": "Open",
            "turfType": "Grass",
        }
        # A missing distance is None; no alley points when absent.
        assert out[5355]["leftCenter"] is None and "left" not in out[5355]
        assert out[9999]["center"] is None and out[9999]["roofType"] == "Dome"

    def test_fetch_rejects_a_bad_body(self, monkeypatch):
        monkeypatch.setattr(
            mvd.urllib.request, "urlopen", lambda req, timeout=None: _Resp(b'{"nope": 1}')
        )
        with pytest.raises(ValueError):
            mvd.fetch_venue_dimensions(2026)

    def test_write_merges_seasons_and_keeps_the_last_copy(self, tmp_path):
        out_dir = str(tmp_path)
        answers = {
            2025: {1: {"name": "Old", "center": 390.0}, 2: {"name": "Two", "center": 400.0}},
            2026: {1: {"name": "New", "center": 396.0}},
        }
        venues = mvd.write_venue_dimensions(out_dir, [2026, 2025], fetch=lambda s: answers[s])
        # The latest season wins per venue; venue 2 survives from 2025.
        assert venues[1]["name"] == "New" and venues[2]["center"] == 400.0
        with open(tmp_path / "venue_dimensions.json", encoding="utf-8") as fh:
            doc = json.load(fh)
        assert doc["fetched"] == {"2025": "ok", "2026": "ok"}
        assert set(doc["venues"]) == {"1", "2"}
        assert mvd.read_venue_dimensions(out_dir) == venues

        # The API fails: the last copy stands and the file records the failure.
        def down(season):
            raise urllib.error.URLError("down")

        again = mvd.write_venue_dimensions(out_dir, [2026], fetch=down)
        assert again == venues
        with open(tmp_path / "venue_dimensions.json", encoding="utf-8") as fh:
            assert json.load(fh)["fetched"] == {"2026": "failed"}
        assert mvd.read_venue_dimensions(out_dir) == venues

    def test_a_truncated_answer_keeps_the_copy(self, tmp_path, caplog):
        # ``resp.read()`` raises ``http.client.IncompleteRead`` on a body cut
        # short; it is not an ``OSError``, so the fallback names it.
        out_dir = str(tmp_path)
        venues = mvd.write_venue_dimensions(
            out_dir, [2025], fetch=lambda s: {1: {"name": "One", "center": 390.0}}
        )

        class _Cut:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                raise http.client.IncompleteRead(b'{"venues": [')

        with caplog.at_level("WARNING", logger="mlb_venue_dimensions"):
            monkey = pytest.MonkeyPatch()
            monkey.setattr(mvd.urllib.request, "urlopen", lambda req, timeout=None: _Cut())
            try:
                again = mvd.write_venue_dimensions(out_dir, [2026])
            finally:
                monkey.undo()
        assert again == venues
        assert "the venues call for 2026 failed" in caplog.text
        with open(tmp_path / "venue_dimensions.json", encoding="utf-8") as fh:
            assert json.load(fh)["fetched"] == {"2026": "failed"}

    def test_write_with_no_copy_and_no_answer_is_empty(self, tmp_path, caplog):
        def down(season):
            raise OSError("no network")

        with caplog.at_level("WARNING", logger="mlb_venue_dimensions"):
            assert mvd.write_venue_dimensions(str(tmp_path), [2026], fetch=down) == {}
        assert "no copy exists" in caplog.text
        assert mvd.read_venue_dimensions(str(tmp_path)) == {}

    def test_read_absent_or_broken(self, tmp_path):
        assert mvd.read_venue_dimensions(str(tmp_path / "nowhere")) == {}
        with open(tmp_path / "venue_dimensions.json", "w", encoding="utf-8") as fh:
            fh.write("not json")
        assert mvd.read_venue_dimensions(str(tmp_path)) == {}

    def test_default_fetch_is_looked_up_at_call_time(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            mvd, "fetch_venue_dimensions", lambda s, timeout=30: {3: {"center": 1.0}}
        )
        assert mvd.write_venue_dimensions(str(tmp_path), [2026]) == {3: {"center": 1.0}}
