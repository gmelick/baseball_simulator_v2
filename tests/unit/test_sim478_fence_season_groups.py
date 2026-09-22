"""
tests/unit/test_sim478_fence_season_groups.py
=============================================
SIM-478/480 — the fence stage reads a version-2 park geometry: an eleven-
sector grid (spray -55 to +55 degrees) and, per venue, SEASON GROUPS keyed
``"FIRST-LAST"`` (a fence that moved gets a new group at the season of the
move). What these tests pin on the sampler:

  * ``fence_at`` picks the group whose range holds the live season, and the
    LATEST group when no range holds it or the season is unknown;
  * a plain-list venue (the old shape) still reads inside a new document;
  * a None sector falls to the league line; the sector index follows the
    document's own grid (``spray_min`` / ``sector_deg`` / ``n_sectors``);
  * ``fence_decision`` with a season: the same ball is short before a move
    and over after it;
  * the sixth fence counter (not an air ball) rides with the short counter;
  * the parsed-group cache is keyed to the document object;
  * SIM-478 §11 / §12.7 on the groups: the carry offset's shift reads the
    live season's fence, and every row's wall margin follows its own
    season's group.
"""

from __future__ import annotations

import numpy as np
import pytest

from pipeline.batch.engine_artifacts import BB_CLASS, BattedBallPool, EngineArtifacts
from simulation.full_pool_sampler import FullPoolSampler

_SEASON = 2024
_BATTER = "200:2024"
_FLY = BB_CLASS["fly_ball"]
_GROUND = BB_CLASS["ground_ball"]

_LEAGUE = [335.0, 345.0, 365.0, 380.0, 395.0, 400.0, 395.0, 380.0, 365.0, 345.0, 335.0]


def _line(**at: float | None) -> list[float | None]:
    """A league-shaped line with the given sectors replaced."""
    out: list[float | None] = list(_LEAGUE)
    for k, v in at.items():
        out[int(k[1:])] = v
    return out


def _geometry() -> dict:
    return {
        "version": 2,
        "seasons": [2023, 2024, 2025, 2026],
        "sector_deg": 10,
        "spray_min": -55.0,
        "spray_max": 55.0,
        "n_sectors": 11,
        "league": list(_LEAGUE),
        "venues": {
            # Camden-like: the left-field line moved in for 2025.
            "2": {
                "2023-2024": _line(s0=330.0, s1=392.0, s5=None),
                "2025-2026": _line(s0=330.0, s1=372.0, s5=None),
            },
            "7": {"2026-2026": _line(s1=350.0, s10=None)},
            # The old shape inside a new document: one plain list.
            "9": _line(s1=360.0, s5=410.0),
        },
        "carry": {"coef": [0.0, 4.0, 0.0, 0.0, 0.0, 0.0]},
    }


def _bb_pool(events: list[str]) -> BattedBallPool:
    n = len(events)
    rh = np.asarray(
        [4 if e == "home_run" else (1 if e == "single" else 0) for e in events], dtype=np.int8
    )
    return BattedBallPool(
        geom=np.zeros((n, 3), dtype=np.float32) + np.array([95.0, 30.0, 0.0], dtype=np.float32),
        sit=np.zeros((n, 6), dtype=np.float32),
        batter_id=np.full(n, 200, dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        event=np.asarray(events, dtype=object),
        result_hits=rh,
        result_outs=(rh == 0).astype(np.int8),
        recency=np.ones(n, dtype=np.float32),
        r1_dest=np.full(n, -1, dtype=np.int8),
        r2_dest=np.full(n, -1, dtype=np.int8),
        r3_dest=np.full(n, -1, dtype=np.int8),
        batter_dest=rh.copy(),
        dest_ok=np.ones(n, dtype=np.int8),
        r1_adv_out=np.zeros(n, dtype=np.int8),
        r2_adv_out=np.zeros(n, dtype=np.int8),
        r3_adv_out=np.zeros(n, dtype=np.int8),
        is_air=np.ones(n, dtype=np.int8),
        spray_raw=np.zeros(n, dtype=np.float32),
        hit_dist=np.where(rh == 4, 410.0, 330.0).astype(np.float32),
        bb_class=None,
    )


_MIXED = ["home_run"] * 3 + ["double"] * 3 + ["field_out"] * 3


def _sampler(geometry: dict | None = None, seed: int = 0) -> FullPoolSampler:
    art = EngineArtifacts(
        pools={}, bb_pools={"R": _bb_pool(_MIXED)}, park_geometry=geometry or _geometry()
    )
    return FullPoolSampler(art, np.random.default_rng(seed))


def _draws(fp: FullPoolSampler, n: int = 40, **kw) -> set[str]:
    out = set()
    for _ in range(n):
        fp.battedball_new_pa("R", _BATTER, np.zeros(6, np.float32), **kw)
        out.add(fp.battedball_draw()[0])
    return out


class TestTheGroupLookup:
    def test_the_season_picks_the_group(self):
        fp = _sampler()
        assert fp.fence_at(2, -40.0, 2024) == 392.0
        assert fp.fence_at(2, -40.0, 2023) == 392.0
        assert fp.fence_at(2, -40.0, 2025) == 372.0
        assert fp.fence_at(2, -40.0, 2026) == 372.0

    def test_no_containing_group_reads_the_latest(self):
        fp = _sampler()
        assert fp.fence_at(2, -40.0, 2027) == 372.0  # past every range
        assert fp.fence_at(2, -40.0, 2022) == 372.0  # before every range
        assert fp.fence_at(2, -40.0, None) == 372.0  # no season
        assert fp.fence_at(2, -40.0) == 372.0
        assert fp.fence_at(7, -40.0, 2024) == 350.0  # the one group of venue 7

    def test_the_latest_group_is_by_last_season_not_key_order(self):
        g = _geometry()
        g["venues"]["2"] = {
            "2025-2026": _line(s1=372.0),
            "2023-2024": _line(s1=392.0),
        }
        assert _sampler(g).fence_at(2, -40.0, 2030) == 372.0

    def test_a_plain_list_venue_reads_inside_a_new_document(self):
        fp = _sampler()
        assert fp.fence_at(9, -40.0, 2024) == 360.0
        assert fp.fence_at(9, 0.0, 2019) == 410.0
        assert fp.fence_at(9, 0.0, None) == 410.0

    def test_a_none_sector_and_an_unknown_venue_fall_to_the_league_line(self):
        fp = _sampler()
        assert fp.fence_at(2, 0.0, 2024) == 400.0  # sector 5 is None in both groups
        assert fp.fence_at(7, 50.0, 2026) == 335.0  # sector 10 is None
        assert fp.fence_at(99, 0.0, 2024) == 400.0
        assert fp.fence_at(None, 0.0, 2024) == 400.0
        assert fp.fence_at(2, None, 2024) is None
        assert fp.fence_at(2, float("nan"), 2024) is None

    def test_the_sector_index_follows_the_documents_grid(self):
        fp = _sampler()
        # spray_min -55: -50 -> sector 0, -40 -> 1, 0 -> 5, 50 -> 10, 60 clamps to 10.
        assert fp.fence_at(2, -50.0, 2024) == 330.0
        assert fp.fence_at(2, -40.0, 2024) == 392.0
        assert fp.fence_at(9, 0.0, 2024) == 410.0
        assert fp.fence_at(2, 50.0, 2024) == _LEAGUE[10]
        assert fp.fence_at(2, 60.0, 2024) == _LEAGUE[10]
        assert fp.fence_at(2, -60.0, 2024) == 330.0  # clamps to sector 0
        # The grid is the document's, not a module constant: a 9-sector
        # document puts spray -40 in its sector 0.
        g = _geometry()
        g.update(n_sectors=9, spray_min=-45.0, spray_max=45.0, league=_LEAGUE[1:10])
        g["venues"] = {"9": [300.0] + _LEAGUE[2:10]}
        assert _sampler(g).fence_at(9, -40.0, 2024) == 300.0

    def test_an_empty_group_dict_reads_the_league_line(self):
        g = _geometry()
        g["venues"]["2"] = {}
        assert _sampler(g).fence_at(2, -40.0, 2024) == _LEAGUE[1]


class TestTheDecisionAndTheCounters:
    def test_the_same_ball_is_short_before_the_move_and_over_after(self):
        fp = _sampler()
        ball = {"cls": _FLY, "spray_raw": -40.0, "dist": 380.0}
        assert fp.fence_decision(ball, 2, 2024) == 1
        assert fp.fence_decision(ball, 2, 2025) == 0
        assert fp.fence_decision(ball, 2, None) == 0  # the latest group
        assert fp.fence_decision(ball, 2) == 0

    def test_the_stage_reads_the_live_season(self):
        fp = _sampler()
        fp.fence_stage = True
        ball = {"cls": _FLY, "spray_raw": -40.0, "dist": 380.0}
        assert _draws(fp, born_bb=ball, venue_id=2, live_season=2024) == {"double", "field_out"}
        assert fp.fence_counts.tolist() == [0, 40, 0, 0, 0, 0]
        assert _draws(fp, born_bb=ball, venue_id=2, live_season=2025) == {"home_run"}
        assert fp.fence_counts.tolist() == [40, 40, 0, 0, 0, 0]

    def test_a_ground_ball_counts_in_short_and_in_not_an_air_ball(self):
        fp = _sampler()
        fp.fence_stage = True
        ground = {"cls": _GROUND, "spray_raw": -40.0, "dist": 380.0}
        assert _draws(fp, n=10, born_bb=ground, venue_id=2, live_season=2025) == {
            "double",
            "field_out",
        }
        assert fp.fence_counts.tolist() == [0, 10, 0, 0, 0, 10]
        # An air ball called short counts in [1] alone.
        short = {"cls": _FLY, "spray_raw": -40.0, "dist": 380.0}
        _draws(fp, n=10, born_bb=short, venue_id=2, live_season=2024)
        assert fp.fence_counts.tolist() == [0, 20, 0, 0, 0, 10]
        # A passed ball (no venue) touches neither.
        _draws(fp, n=10, born_bb=short, venue_id=None, live_season=2024)
        assert fp.fence_counts.tolist() == [0, 20, 0, 10, 0, 10]


class TestTheCache:
    def test_the_cache_is_keyed_to_the_document(self):
        fp = _sampler()
        assert fp.fence_at(2, -40.0, 2024) == 392.0
        assert "2" in fp._fence_groups and fp._fence_groups_doc is fp.a.park_geometry
        other = _geometry()
        other["venues"]["2"] = {"2020-2030": _line(s1=300.0)}
        fp.a.park_geometry = other
        assert fp.fence_at(2, -40.0, 2024) == 300.0
        assert fp._fence_groups_doc is other
        assert fp._fence_groups["2"] == [(2020, 2030, _line(s1=300.0))]

    def test_the_cache_holds_one_parse_per_venue(self):
        fp = _sampler()
        for season in (2023, 2024, 2025, None):
            fp.fence_at(2, -40.0, season)
        fp.fence_at(7, -40.0, 2026)
        fp.fence_at(9, -40.0, 2026)  # a plain list is never cached
        assert set(fp._fence_groups) == {"2", "7"}
        assert [g[:2] for g in fp._fence_groups["2"]] == [(2023, 2024), (2025, 2026)]


class TestTheAmendmentsOnTheGroups:
    def test_the_shift_meets_the_live_seasons_fence(self):
        # A sea-level 380-ft ball born at Camden, whose air carries +15: 395
        # in Camden's air — over the 2024 wall (392) and the 2025 wall (372);
        # without the shift it is short in 2024 and over in 2025.
        doc = _geometry()
        doc["carry_offset_ft"] = {"2": 15.0, "31": 0.0}
        fp = _sampler(doc)
        ball = {"cls": _FLY, "spray_raw": -40.0, "dist": 380.0, "venue": 31}
        assert fp.fence_decision(ball, 2, 2024) == 1
        assert fp.fence_decision(ball, 2, 2025) == 0
        fp.carry_offset = True
        assert fp.fence_decision(ball, 2, 2024) == 0
        assert fp.fence_decision(ball, 2, 2025) == 0
        # A 362-ft Camden ball born at venue 7 (its 2026 wall 350 in that
        # sector; no offset of its own) loses the 15: 347, short.
        camden = {"cls": _FLY, "spray_raw": -40.0, "dist": 362.0, "venue": 2}
        assert fp.fence_decision(camden, 7, 2026) == 1
        fp.carry_offset = False
        assert fp.fence_decision(camden, 7, 2026) == 0

    def test_the_rows_margins_follow_their_own_seasons_group(self):
        # Two Camden rows at 385 ft, spray -40: 7 short of the 2024 wall
        # (392), 13 over the 2025 wall (372); a 2030 row reads the latest
        # group; a plain-list venue (9) reads its one line (360).
        bb = _bb_pool(["double", "home_run", "home_run", "double"])
        bb.venue_id = np.asarray([2, 2, 2, 9], dtype=np.int64)
        bb.season = np.asarray([2024, 2025, 2030, 2024], dtype=np.int64)
        bb.spray_raw = np.full(4, -40.0, dtype=np.float32)
        bb.hit_dist = np.full(4, 385.0, dtype=np.float32)
        art = EngineArtifacts(pools={}, bb_pools={"R": bb}, park_geometry=_geometry())
        fp = FullPoolSampler(art, np.random.default_rng(0))
        assert fp._bb_margins("R").tolist() == pytest.approx([-7.0, 13.0, 13.0, 25.0])
        # The band draws by the margin: a ball 7 short of the live wall
        # (Camden 2024, 385 ft) draws the 2024 row alone at a 5-ft band.
        fp.bb_margin_band = 5.0
        fp.bb_margin_min_rows = 1
        ball = {"cls": _FLY, "spray_raw": -40.0, "dist": 385.0}
        assert _draws(fp, born_bb=ball, venue_id=2, live_season=2024) == {"double"}
        assert fp.bb_margin_counts.tolist() == [40, 0, 0]
        # The same ball in 2025 is over the wall: the band is skipped.
        assert _draws(fp, born_bb=ball, venue_id=2, live_season=2025) == {"double", "home_run"}
        assert fp.bb_margin_counts.tolist() == [40, 0, 40]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
