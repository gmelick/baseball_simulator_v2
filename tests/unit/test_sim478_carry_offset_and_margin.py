"""
tests/unit/test_sim478_carry_offset_and_margin.py
=================================================
SIM-478 — the fence certification's three amendments on the fielding draw
(plan: docs/audit/2026-09-20-sim478-480-fence-certification-plan.md, §11,
§12 and §12.7):

  * THE CARRY OFFSET (§11): the born ball's carry re-expressed in the LIVE
    park's air — its own distance + offset[live park] - offset[the row's
    park], the offsets read from the document's ``carry_offset_ft``. One
    working copy carries that number to the fence check, the kernel and
    the wall margin; the candidate rows keep their own distance. Off, a
    document without the table, a park absent from it: no shift.
  * THE CLASS RULER (§12): the born kernel's mean and spread come from the
    pool's rows of the born ball's class; the exponent per feature
    (``bb_born_per_feature``) divides by 2 sigma^2 instead of 2 sigma^2 x 4.
    A row 60 ft short keeps 0.976 of an exact match today, 0.896 with the
    ruler alone, 0.644 with both.
  * THE WALL-MARGIN BAND (§12.7): every row's margin to its OWN wall (its
    distance minus its park's fence at its spray and season, from the
    document), and a born air ball the fence did not call over draws only
    rows within the band of its own margin to the live wall; too few rows
    fall back; over, a ground ball and a missing margin skip.
  * everything off, a document without the tables, or a pool without the
    columns is byte-identical; the factory reads the four env names.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import numpy as np
import pytest

from pipeline.batch.engine_artifacts import BB_CLASS, BattedBallPool, EngineArtifacts
from simulation.full_pool_sampler import FullPoolSampler
from simulation.production_factory import apply_fielding_env

_BATTER = "200:2024"
_FLY = BB_CLASS["fly_ball"]
_LINE = BB_CLASS["line_drive"]
_GROUND = BB_CLASS["ground_ball"]

_COORS, _PNC, _KAUFFMAN, _FENWAY, _LIVE = 19, 31, 7, 3, 39

_LEAGUE = [335.0, 345.0, 365.0, 380.0, 395.0, 400.0, 395.0, 380.0, 365.0, 345.0, 335.0]


def _line(centre: float) -> list[float | None]:
    """A league-shaped line with the centre sector (spray -5 .. 5) set."""
    out: list[float | None] = list(_LEAGUE)
    out[5] = centre
    return out


def _geometry(*, offsets: bool = True) -> dict:
    doc: dict = {
        "version": 2,
        "seasons": [2023, 2024, 2025, 2026],
        "sector_deg": 10,
        "spray_min": -55.0,
        "spray_max": 55.0,
        "n_sectors": 11,
        "league": list(_LEAGUE),
        "venues": {
            str(_COORS): _line(400.0),
            str(_PNC): _line(400.0),
            str(_LIVE): _line(390.0),
            # The margin-band parks: a 400, a 420 and a 380 centre fence.
            "40": _line(400.0),
            "42": _line(420.0),
            "38": _line(380.0),
            # Camden-like: the centre fence moved in for 2025.
            "2": {"2023-2024": _line(400.0), "2025-2026": _line(380.0)},
        },
        "carry": {"coef": [0.0, 4.0, 0.0, 0.0, 0.0, 0.0]},
    }
    if offsets:
        doc["carry_offset_ft"] = {
            str(_COORS): 20.0,
            str(_KAUFFMAN): 10.0,
            str(_FENWAY): -4.0,
            str(_PNC): 0.0,
            str(_LIVE): 0.0,
            "40": 0.0,
            "42": 0.0,
            "38": 0.0,
            "2": None,  # a park under 100 home runs: no offset, reads 0
        }
        doc["carry_offset_fit"] = {"n_hr": 21872, "reference_median_park": 31}
    return doc


def _pool(
    rows: list[dict],
    *,
    with_class: bool = True,
    with_venue: bool = True,
    with_spray: bool = True,
) -> BattedBallPool:
    """One row per dict: event, dist, and optional venue / spray / season /
    cls / ev / la (the defaults: venue 40, spray 0, season 2024, fly, 100
    mph, 28 degrees)."""
    n = len(rows)
    ev = np.asarray([r.get("ev", 100.0) for r in rows], dtype=np.float32)
    la = np.asarray([r.get("la", 28.0) for r in rows], dtype=np.float32)
    spray = np.asarray([r.get("spray", 0.0) for r in rows], dtype=np.float32)
    events = [r["event"] for r in rows]
    rh = np.asarray(
        [
            4 if e == "home_run" else (1 if e == "single" else (2 if e == "double" else 0))
            for e in events
        ],
        dtype=np.int8,
    )
    return BattedBallPool(
        geom=np.column_stack([ev, la, spray]).astype(np.float32),
        sit=np.zeros((n, 6), dtype=np.float32),
        batter_id=np.full(n, 200, dtype=np.int64),
        season=np.asarray([r.get("season", 2024) for r in rows], dtype=np.int64),
        event=np.asarray(events, dtype=object),
        result_hits=rh,
        result_outs=(rh == 0).astype(np.int8),
        recency=np.ones(n, dtype=np.float32),
        venue_id=(
            np.asarray([r.get("venue", 40) for r in rows], dtype=np.int64) if with_venue else None
        ),
        r1_dest=np.full(n, -1, dtype=np.int8),
        r2_dest=np.full(n, -1, dtype=np.int8),
        r3_dest=np.full(n, -1, dtype=np.int8),
        batter_dest=np.minimum(rh, 3).astype(np.int8),
        dest_ok=np.ones(n, dtype=np.int8),
        r1_adv_out=np.zeros(n, dtype=np.int8),
        r2_adv_out=np.zeros(n, dtype=np.int8),
        r3_adv_out=np.zeros(n, dtype=np.int8),
        is_air=np.ones(n, dtype=np.int8),
        spray_raw=(spray.copy() if with_spray else None),
        hit_dist=np.asarray([r["dist"] for r in rows], dtype=np.float32),
        bb_class=(
            np.asarray([r.get("cls", _FLY) for r in rows], dtype=np.int8) if with_class else None
        ),
    )


def _sampler(bb: BattedBallPool, geometry: dict | None, seed: int = 0) -> FullPoolSampler:
    art = EngineArtifacts(pools={}, bb_pools={"R": bb}, park_geometry=geometry)
    fp = FullPoolSampler(art, np.random.default_rng(seed))
    assert fp.carry_offset is False and fp.bb_born_per_feature is False
    assert fp.bb_margin_band == 0.0 and fp.bb_margin_min_rows == 20
    return fp


def _draws(fp: FullPoolSampler, n: int = 40, **kw) -> set[str]:
    out = set()
    for _ in range(n):
        fp.battedball_new_pa("R", _BATTER, np.zeros(6, np.float32), **kw)
        out.add(fp.battedball_draw()[0])
    return out


def _seq(fp: FullPoolSampler, n: int = 30, **kw) -> list[str]:
    out = []
    for _ in range(n):
        fp.battedball_new_pa("R", _BATTER, np.zeros(6, np.float32), **kw)
        out.append(fp.battedball_draw()[0])
    return out


def _drawn_dists(fp: FullPoolSampler, n: int, **kw) -> np.ndarray:
    pool = fp.a.bb_pools["R"]
    out = []
    for _ in range(n):
        fp.battedball_new_pa("R", _BATTER, np.zeros(6, np.float32), **kw)
        fp.battedball_draw()
        out.append(float(pool.hit_dist[fp._bb_last_i]))
    return np.asarray(out)


_MIXED = [
    {"event": "home_run", "dist": 410.0},
    {"event": "home_run", "dist": 412.0},
    {"event": "double", "dist": 380.0},
    {"event": "double", "dist": 382.0},
    {"event": "field_out", "dist": 330.0},
    {"event": "field_out", "dist": 332.0},
]


# ===========================================================================
# §11 — the carry offset
# ===========================================================================


class TestTheShift:
    def _fp(self, **kw) -> FullPoolSampler:
        fp = _sampler(_pool(_MIXED), _geometry(**kw))
        fp.carry_offset = True
        return fp

    def test_a_sea_level_row_born_at_coors_gains_the_offset(self):
        born = {"dist": 385.0, "venue": _PNC, "cls": _FLY}
        assert self._fp().carry_of(born, _COORS) == pytest.approx(405.0)
        assert self._fp().carry_shift(born, _COORS) == pytest.approx(20.0)

    def test_a_coors_row_born_at_pnc_loses_it(self):
        born = {"dist": 385.0, "venue": _COORS, "cls": _FLY}
        assert self._fp().carry_of(born, _PNC) == pytest.approx(365.0)
        # Kauffman +10 against Fenway -4: the difference of the two facts.
        assert self._fp().carry_of({"dist": 385.0, "venue": _FENWAY}, _KAUFFMAN) == pytest.approx(
            399.0
        )

    def test_the_same_park_is_zero(self):
        assert self._fp().carry_of({"dist": 385.0, "venue": _COORS}, _COORS) == 385.0

    def test_a_park_absent_from_the_table_reads_zero(self):
        fp = self._fp()
        assert fp.carry_of({"dist": 385.0, "venue": 99}, _COORS) == pytest.approx(405.0)
        assert fp.carry_of({"dist": 385.0, "venue": _COORS}, 99) == pytest.approx(365.0)
        assert fp.carry_of({"dist": 385.0, "venue": 99}, 98) == 385.0
        # A null entry (a park under 100 home runs) reads 0 as well.
        assert fp.carry_of({"dist": 385.0, "venue": 2}, _COORS) == pytest.approx(405.0)

    def test_no_live_park_and_no_row_park_read_zero(self):
        fp = self._fp()
        assert fp.carry_of({"dist": 385.0, "venue": _PNC}) == 385.0
        assert fp.carry_of({"dist": 385.0, "venue": _PNC}, None) == 385.0
        assert fp.carry_of({"dist": 385.0}, _COORS) == 385.0
        assert fp.carry_of({"dist": 385.0, "venue": None}, _COORS) == 385.0

    def test_the_flag_off_reads_zero(self):
        fp = _sampler(_pool(_MIXED), _geometry())
        assert fp.carry_of({"dist": 385.0, "venue": _PNC}, _COORS) == 385.0
        assert fp.carry_shift({"dist": 385.0, "venue": _PNC}, _COORS) == 0.0

    def test_a_document_without_the_table_reads_zero(self):
        fp = self._fp(offsets=False)
        assert fp.carry_of({"dist": 385.0, "venue": _PNC}, _COORS) == 385.0
        assert fp._carry_offsets() is None
        fp2 = _sampler(_pool(_MIXED), None)
        fp2.carry_offset = True
        assert fp2.carry_of({"dist": 385.0, "venue": _PNC}, _COORS) == 385.0

    def test_the_model_carry_shifts_too(self):
        # No own distance: the carry model (4 x exit velocity) plus the shift.
        fp = self._fp()
        assert fp.carry_of({"dist": None, "ev": 100.0, "la": 28.0, "venue": _PNC}, _COORS) == (
            pytest.approx(420.0)
        )
        assert fp.carry_of({"dist": None, "ev": None, "la": 28.0, "venue": _PNC}, _COORS) is None

    def test_the_table_is_parsed_once_per_document(self):
        fp = self._fp()
        first = fp._carry_offsets()
        assert first == {
            19: 20.0,
            7: 10.0,
            3: -4.0,
            31: 0.0,
            39: 0.0,
            40: 0.0,
            42: 0.0,
            38: 0.0,
            2: 0.0,
        }
        assert fp._carry_offsets() is first
        other = _geometry()
        other["carry_offset_ft"] = {str(_COORS): 15.0}
        fp.a.park_geometry = other
        assert fp._carry_offsets() == {19: 15.0}

    def test_the_fence_check_reads_the_shifted_carry(self):
        # A 385-ft sea-level ball at a 400-ft Coors sector: short with the
        # flag off, over with it on.
        born = {"cls": _FLY, "spray_raw": 0.0, "dist": 385.0, "venue": _PNC}
        fp = _sampler(_pool(_MIXED), _geometry())
        assert fp.fence_decision(born, _COORS) == 1
        fp.carry_offset = True
        assert fp.fence_decision(born, _COORS) == 0
        # The same ball born at PNC is still short; a Coors ball at PNC (365) too.
        assert fp.fence_decision(born, _PNC) == 1
        assert fp.fence_decision({**born, "venue": _COORS, "dist": 395.0}, _PNC) == 1

    def test_the_stage_draws_the_shifted_decision(self):
        born = {"cls": _FLY, "spray_raw": 0.0, "dist": 385.0, "venue": _PNC}
        off = _sampler(_pool(_MIXED), _geometry())
        off.fence_stage = True
        assert _draws(off, born_bb=born, venue_id=_COORS) == {"double", "field_out"}
        on = _sampler(_pool(_MIXED), _geometry())
        on.fence_stage = True
        on.carry_offset = True
        assert _draws(on, born_bb=born, venue_id=_COORS) == {"home_run"}
        assert on.fence_counts.tolist() == [40, 0, 0, 0, 0, 0]
        # The caller's dict is untouched: the shift lives on a working copy.
        assert born["dist"] == 385.0 and born["venue"] == _PNC

    def test_the_working_copy_sits_in_the_live_air(self):
        fp = self._fp()
        born = {"cls": _FLY, "spray_raw": 0.0, "dist": 385.0, "venue": _PNC}
        copy = fp._born_in_live_air(born, _COORS)
        assert copy is not born
        assert copy["dist"] == pytest.approx(405.0) and copy["venue"] == _COORS
        # A second carry_of on the copy shifts by exactly 0 — no double shift.
        assert fp.carry_of(copy, _COORS) == pytest.approx(405.0)
        assert fp.fence_decision(copy, _COORS) == 0
        # No shift possible: the ball itself comes back.
        assert fp._born_in_live_air(born, None) is born
        assert fp._born_in_live_air({"cls": _FLY, "dist": 385.0}, _COORS) is not copy
        assert fp._born_in_live_air({"cls": _FLY, "dist": 385.0}, _COORS)["dist"] == 385.0
        fp.carry_offset = False
        assert fp._born_in_live_air(born, _COORS) is born
        assert self._fp(offsets=False)._born_in_live_air(born, _COORS) is born

    def test_the_kernel_reads_the_shifted_distance(self):
        # Rows differ only in distance; the born kernel is the only weight.
        rows = [{"event": "field_out", "dist": float(d), "venue": 40} for d in range(300, 461, 4)]
        born = {
            "cls": _FLY,
            "ev": 100.0,
            "la": 28.0,
            "spray": 0.0,
            "spray_raw": 0.0,
            "dist": 380.0,
            "venue": _PNC,
        }

        def run(venue: int) -> np.ndarray:
            fp = _sampler(_pool(rows), _geometry(), seed=11)
            fp.carry_offset = True
            fp.bb_born_sigma = 0.15
            fp.bb_born_per_feature = True
            fp.bb_born_density_power = 0.0
            return _drawn_dists(fp, 600, born_bb=born, venue_id=venue)

        at_pnc, at_coors = run(_PNC), run(_COORS)
        assert at_pnc.mean() == pytest.approx(380.0, abs=4.0)
        assert at_coors.mean() - at_pnc.mean() == pytest.approx(20.0, abs=5.0)


# ===========================================================================
# §12 — the class ruler and the exponent per feature
# ===========================================================================


class TestTheClassRuler:
    def _pool(self) -> BattedBallPool:
        # Fly balls at 350 +- 20 and ground balls at 100 +- 10.
        rows = [{"event": "field_out", "dist": 330.0 + 40.0 * (i % 2)} for i in range(20)]
        rows += [
            {"event": "field_out", "dist": 90.0 + 20.0 * (i % 2), "cls": _GROUND} for i in range(20)
        ]
        return _pool(rows)

    def test_the_fly_ruler_is_the_fly_balls_own(self):
        fp = _sampler(self._pool(), _geometry())
        mean, std, valid = fp._bb_born_z_stats("R", _FLY)
        assert mean[3] == pytest.approx(350.0) and std[3] == pytest.approx(20.0)
        g_mean, g_std, _ = fp._bb_born_z_stats("R", _GROUND)
        assert g_mean[3] == pytest.approx(100.0) and g_std[3] == pytest.approx(10.0)
        p_mean, p_std, _ = fp._bb_born_z_stats("R", None)
        assert p_mean[3] == pytest.approx(225.0) and p_std[3] > 100.0
        # The mask is completeness, shared by every class: all 40 rows.
        assert valid.all() and valid.shape == (40,)
        assert set(fp._bb_born_stats) == {("R", _FLY), ("R", _GROUND), ("R", None)}

    def test_no_class_falls_to_the_pool(self):
        fp = _sampler(self._pool(), _geometry())
        pool_stats = fp._bb_born_z_stats("R", None)
        # A class with no rows (line drives), a class of 0, a pool without the column.
        assert fp._bb_born_z_stats("R", _LINE)[1][3] == pool_stats[1][3]
        assert fp._bb_born_z_stats("R", 0)[1][3] == pool_stats[1][3]
        no_col = _sampler(_pool(_MIXED, with_class=False), _geometry())
        assert no_col._bb_born_z_stats("R", _FLY)[1][3] == no_col._bb_born_z_stats("R")[1][3]

    def test_the_mask_requires_a_distance_above_zero(self):
        rows = list(_MIXED) + [{"event": "field_out", "dist": 0.0}]
        fp = _sampler(_pool(rows), _geometry())
        _, _, valid = fp._bb_born_z_stats("R")
        assert valid.tolist() == [True] * 6 + [False]

    @staticmethod
    def _sixty_foot_pool() -> tuple[BattedBallPool, int, int]:
        """Fly balls whose distance SD is exactly 64 ft, with a row AT the
        born distance (350) and one 60 ft short; ground balls added so the
        pool-wide SD is exactly 135 ft. Every other feature is constant."""
        d0 = 350.0
        k = float(np.sqrt((5 * 64.0**2 - 2 * 60.0**2) / 2.0))
        fly = [d0, d0 - 60.0, d0 + 60.0, d0 - k, d0 + k]
        m = 20
        h = float(np.sqrt((135.0**2 * (5 + m) - 5 * 64.0**2) / m))
        ground = [d0 - h if i % 2 else d0 + h for i in range(m)]
        rows = [{"event": "field_out", "dist": d} for d in fly]
        rows += [{"event": "field_out", "dist": d, "cls": _GROUND} for d in ground]
        return _pool(rows), 0, 1

    def test_the_sixty_foot_gap_weights(self):
        bb, at, short = self._sixty_foot_pool()
        fp = _sampler(bb, _geometry())
        assert fp._bb_born_z_stats("R", _FLY)[1][3] == pytest.approx(64.0, abs=1e-3)
        assert fp._bb_born_z_stats("R", None)[1][3] == pytest.approx(135.0, abs=1e-2)
        fp.bb_born_sigma = 1.0
        rows = np.asarray([at, short])
        born = {"ev": 100.0, "la": 28.0, "spray": 0.0, "dist": 350.0}

        def ratio() -> float:
            f = fp._f_born_similarity("R", rows, born)
            return float(f[1] / f[0])

        # Today: the pool ruler, the exponent over the four features.
        assert ratio() == pytest.approx(0.976, abs=0.002)
        # The class ruler alone.
        born["cls"] = _FLY
        assert ratio() == pytest.approx(0.896, abs=0.002)
        # The class ruler with the exponent per feature.
        fp.bb_born_per_feature = True
        assert ratio() == pytest.approx(0.644, abs=0.002)
        # The exponent per feature on the pool ruler: bandwidth 0.5's weight.
        born["cls"] = None
        assert ratio() == pytest.approx(np.exp(-((60.0 / 135.0) ** 2) / 2.0), abs=0.002)

    def test_the_density_correction_follows_the_ruler(self):
        bb, _, _ = self._sixty_foot_pool()
        fp = _sampler(bb, _geometry())
        fp.bb_born_sigma = 1.0
        meta = fp._transition_meta("R")
        rows = np.arange(bb.n)
        pool_ruler = fp._born_inv_density("R", meta, rows)
        fly_ruler = fp._born_inv_density("R", meta, rows, _FLY)
        fp.bb_born_per_feature = True
        fly_per_feature = fp._born_inv_density("R", meta, rows, _FLY)
        assert not np.allclose(pool_ruler, fly_ruler)
        assert not np.allclose(fly_ruler, fly_per_feature)
        keys = {(k[5], k[6]) for k in meta["born_density"]}
        assert keys == {(None, False), (_FLY, False), (_FLY, True)}
        # The same call again is the cached array.
        assert fp._born_inv_density("R", meta, rows, _FLY) is fly_per_feature

    def test_the_draw_reads_the_born_class(self):
        # Under the class ruler a fly ball born at 350 draws the 350 rows
        # over the 330 / 370 ones far more often than under the pool ruler.
        rows = [{"event": "single", "dist": 350.0}] * 4
        rows += [{"event": "double", "dist": 330.0}] * 4 + [
            {"event": "field_out", "dist": 370.0}
        ] * 4
        rows += [{"event": "home_run", "dist": 100.0, "cls": _GROUND}] * 40  # a wide pool
        born = {"cls": _FLY, "ev": 100.0, "la": 28.0, "spray": 0.0, "dist": 350.0}

        def share_at_350(per_feature: bool, cls: int | None) -> float:
            fp = _sampler(_pool(rows), _geometry(), seed=3)
            fp.bb_class_filter = True
            fp.bb_born_sigma = 0.5
            fp.bb_born_density_power = 0.0
            fp.bb_born_per_feature = per_feature
            d = _drawn_dists(fp, 400, born_bb={**born, "cls": cls})
            return float((d == 350.0).mean())

        assert share_at_350(True, _FLY) > 0.6
        assert share_at_350(False, None) < 0.45


# ===========================================================================
# §12.7 — the wall-margin band
# ===========================================================================

_ABC = [
    {"event": "double", "dist": 385.0, "venue": 40},  # margin -15 at a 400 fence
    {"event": "field_out", "dist": 385.0, "venue": 42},  # margin -35 at a 420 fence
    {"event": "single", "dist": 365.0, "venue": 38},  # margin -15 at a 380 fence
]
_BORN_SHORT = {"cls": _FLY, "spray_raw": 0.0, "spray": 0.0, "dist": 375.0, "venue": _LIVE}


class TestTheMarginBand:
    def _fp(self, rows=_ABC, band: float = 15.0, min_rows: int = 1, **kw) -> FullPoolSampler:
        fp = _sampler(_pool(rows, **kw), _geometry())
        fp.bb_margin_band = band
        fp.bb_margin_min_rows = min_rows
        return fp

    def test_the_rows_margins_are_to_their_own_walls(self):
        fp = self._fp()
        assert fp._bb_margins("R").tolist() == pytest.approx([-15.0, -35.0, -15.0])
        assert fp._bb_margins("R").dtype == np.float32

    def test_a_short_ball_draws_the_rows_within_the_band(self):
        # 15 ft short of a 390 live fence: A and C, not B.
        fp = self._fp()
        assert _draws(fp, born_bb=_BORN_SHORT, venue_id=_LIVE, live_season=2024) == {
            "double",
            "single",
        }
        assert fp.bb_margin_counts.tolist() == [40, 0, 0]
        # The band off: all three.
        off = self._fp(band=0.0)
        assert _draws(off, born_bb=_BORN_SHORT, venue_id=_LIVE) == {"double", "field_out", "single"}
        assert off.bb_margin_counts.tolist() == [0, 0, 0]

    def test_a_wider_band_takes_the_deep_row_too(self):
        fp = self._fp(band=20.0)
        assert _draws(fp, born_bb=_BORN_SHORT, venue_id=_LIVE) == {"double", "field_out", "single"}

    def test_too_few_rows_fall_back(self):
        fp = self._fp(min_rows=3)
        assert _draws(fp, born_bb=_BORN_SHORT, venue_id=_LIVE) == {"double", "field_out", "single"}
        assert fp.bb_margin_counts.tolist() == [0, 40, 0]
        # Exactly the minimum applies.
        fp = self._fp(min_rows=2)
        assert _draws(fp, born_bb=_BORN_SHORT, venue_id=_LIVE) == {"double", "single"}
        assert fp.bb_margin_counts.tolist() == [40, 0, 0]

    def test_a_ball_called_over_skips_the_band(self):
        over = {**_BORN_SHORT, "dist": 400.0}
        fp = self._fp()
        assert _draws(fp, born_bb=over, venue_id=_LIVE) == {"double", "field_out", "single"}
        assert fp.bb_margin_counts.tolist() == [0, 0, 40]
        # With the fence stage on, the stage's own decision is the one read.
        rows = list(_ABC) + [{"event": "home_run", "dist": 410.0, "venue": 40}]
        fp = self._fp(rows)
        fp.fence_stage = True
        assert _draws(fp, born_bb=over, venue_id=_LIVE) == {"home_run"}
        assert fp.bb_margin_counts.tolist() == [0, 0, 40]
        assert fp.fence_counts[0] == 40
        # A short ball through the stage: the home run leaves, the band acts.
        assert _draws(fp, born_bb=_BORN_SHORT, venue_id=_LIVE) == {"double", "single"}
        assert fp.bb_margin_counts.tolist() == [40, 0, 40]

    def test_a_ball_short_of_the_wall_zone_skips_the_band(self):
        # The band is the WALL play: a fly ball carrying under the wall-zone
        # distance keeps its whole neighbourhood (for it the band would only
        # pick parks by fence depth). 375 ft is in the zone at 300; not at 380.
        fp = self._fp()
        fp.wall_zone_distance = 380.0
        assert _draws(fp, born_bb=_BORN_SHORT, venue_id=_LIVE) == {"double", "field_out", "single"}
        assert fp.bb_margin_counts.tolist() == [0, 0, 40]
        fp.wall_zone_distance = 300.0
        assert _draws(fp, born_bb=_BORN_SHORT, venue_id=_LIVE) == {"double", "single"}
        assert fp.bb_margin_counts.tolist() == [40, 0, 40]
        # The zone is read on the ball's carry in the LIVE air: a 290-ft ball
        # born into a park whose air adds 20 ft is in the zone at 300.
        fp = self._fp(rows=[dict(r, dist=r["dist"] - 85.0) for r in _ABC])
        fp.carry_offset = True
        fp.a.park_geometry["carry_offset_ft"] = {str(_LIVE): 20.0}
        short_ball = {**_BORN_SHORT, "dist": 290.0, "venue": 40}
        _draws(fp, born_bb=short_ball, venue_id=_LIVE)
        assert fp.bb_margin_counts[2] == 0 and fp.bb_margin_counts[0] + fp.bb_margin_counts[1] == 40

    def test_a_ground_ball_skips_the_band(self):
        fp = self._fp()
        ground = {**_BORN_SHORT, "cls": _GROUND}
        assert _draws(fp, born_bb=ground, venue_id=_LIVE) == {"double", "field_out", "single"}
        assert fp.bb_margin_counts.tolist() == [0, 0, 40]
        # A line drive is an air ball: the band applies.
        assert _draws(fp, born_bb={**_BORN_SHORT, "cls": _LINE}, venue_id=_LIVE) == {
            "double",
            "single",
        }
        assert fp.bb_margin_counts.tolist() == [40, 0, 40]

    def test_no_margin_for_the_born_ball_skips(self):
        fp = self._fp()
        for born, venue in (
            (_BORN_SHORT, None),
            ({**_BORN_SHORT, "spray_raw": None}, _LIVE),
            ({**_BORN_SHORT, "dist": None, "ev": None}, _LIVE),
        ):
            assert _draws(fp, n=20, born_bb=born, venue_id=venue) == {
                "double",
                "field_out",
                "single",
            }
        assert fp.bb_margin_counts.tolist() == [0, 0, 60]

    def test_nan_margins_never_match(self):
        # A row without a venue, one without a spray, one without a distance,
        # one in a document that has no such fence: NaN, never within any band.
        rows = list(_ABC) + [
            {"event": "triple", "dist": 385.0, "venue": 0},
            {"event": "triple", "dist": 385.0, "venue": 40, "spray": float("nan")},
            {"event": "triple", "dist": 0.0, "venue": 40},
        ]
        fp = self._fp(rows, band=1000.0)
        m = fp._bb_margins("R")
        assert np.isfinite(m[:3]).all() and np.isnan(m[3:]).all()
        assert _draws(fp, born_bb=_BORN_SHORT, venue_id=_LIVE) == {"double", "field_out", "single"}
        # A pool without a venue column or a spray column: every margin NaN,
        # the band always falls back.
        for kw in ({"with_venue": False}, {"with_spray": False}):
            fp = self._fp(_ABC, **kw)
            assert np.isnan(fp._bb_margins("R")).all()
            assert _draws(fp, n=20, born_bb=_BORN_SHORT, venue_id=_LIVE) == {
                "double",
                "field_out",
                "single",
            }
            assert fp.bb_margin_counts.tolist() == [0, 20, 0]

    def test_an_unknown_park_reads_the_league_line(self):
        # Venue 77 is not in the document: the league's 400 centre fence.
        fp = self._fp([{"event": "double", "dist": 385.0, "venue": 77}])
        assert fp._bb_margins("R").tolist() == pytest.approx([-15.0])
        # A sector the venue's line leaves None falls to the league too.
        doc = _geometry()
        doc["venues"]["40"] = _line(None)
        fp = _sampler(_pool([{"event": "double", "dist": 385.0, "venue": 40}]), doc)
        assert fp._bb_margins("R").tolist() == pytest.approx([-15.0])

    def test_the_margins_follow_the_documents_season_group(self):
        # Camden's centre fence: 400 through 2024, 380 from 2025. A 385-ft
        # ball is 15 short against the old wall and 5 over the new one.
        rows = [
            {"event": "double", "dist": 385.0, "venue": 2, "season": 2024},
            {"event": "home_run", "dist": 385.0, "venue": 2, "season": 2025},
        ]
        fp = self._fp(rows, band=5.0)
        assert fp._bb_margins("R").tolist() == pytest.approx([-15.0, 5.0])
        assert _draws(fp, born_bb=_BORN_SHORT, venue_id=_LIVE) == {"double"}
        # The born ball's own margin reads the live season's group: the same
        # ball 385 ft at Camden is 15 short in 2024 and over in 2025.
        camden = {**_BORN_SHORT, "dist": 385.0, "venue": 2}
        fp = self._fp(rows, band=5.0)
        assert _draws(fp, born_bb=camden, venue_id=2, live_season=2024) == {"double"}
        assert fp.bb_margin_counts.tolist() == [40, 0, 0]
        assert _draws(fp, born_bb=camden, venue_id=2, live_season=2025) == {"double", "home_run"}
        assert fp.bb_margin_counts.tolist() == [40, 0, 40]  # over: skipped

    def test_the_margins_are_cached_against_the_document(self):
        fp = self._fp()
        first = fp._bb_margins("R")
        assert fp._bb_margins("R") is first and fp._bb_margin_doc is fp.a.park_geometry
        other = _geometry()
        other["venues"]["40"] = _line(390.0)
        fp.a.park_geometry = other
        again = fp._bb_margins("R")
        assert again is not first and again.tolist() == pytest.approx([-5.0, -35.0, -15.0])
        assert fp._bb_margin_doc is other
        # No document at all: every margin NaN.
        fp.a.park_geometry = None
        assert np.isnan(fp._bb_margins("R")).all()

    def test_a_banded_set_never_reads_another_sets_density(self):
        # Four rows at a 400 fence: margins -15 (row 0), -5 (row 1), -25
        # (row 2), -15 (row 3). Row 2 is a far outlier in the kernel's
        # features. Born 12 short (band 10) keeps rows 0, 1, 3; born 18
        # short keeps rows 0, 2, 3: the same first row, last row and size,
        # a different middle. The density cache keys a set by those three
        # numbers, so a banded set must not use the cache.
        rows = [
            {"event": "double", "dist": 385.0, "venue": 40},
            {"event": "single", "dist": 395.0, "venue": 40},
            {"event": "field_out", "dist": 375.0, "venue": 40, "ev": 60.0, "la": 5.0},
            {"event": "triple", "dist": 385.0, "venue": 40},
        ]
        fp = self._fp(rows, band=10.0)
        fp.bb_born_sigma = 1.0
        fp.bb_born_density_power = 1.0
        base = {"cls": _FLY, "spray_raw": 0.0, "spray": 0.0, "ev": 100.0, "la": 28.0}
        kw = {"venue_id": _LIVE, "live_season": 2024}
        fp.battedball_new_pa(
            "R", _BATTER, np.zeros(6, np.float32), born_bb={**base, "dist": 378.0}, **kw
        )
        rows_a, cdf_a = fp._bb_rows.copy(), fp._bb_cdf.copy()
        fp.battedball_new_pa(
            "R", _BATTER, np.zeros(6, np.float32), born_bb={**base, "dist": 372.0}, **kw
        )
        rows_b, cdf_b = fp._bb_rows.copy(), fp._bb_cdf.copy()
        assert rows_a.tolist() == [0, 1, 3] and rows_b.tolist() == [0, 2, 3]
        assert fp.bb_margin_counts.tolist() == [2, 0, 0]
        meta = fp._transition_meta("R")
        # No banded set entered the cache.
        assert meta.get("born_density", {}) == {}
        # The two sets' densities differ: set B's middle row is the outlier,
        # so its weight sits well above set A's middle row's.
        dens_a = fp._born_inv_density("R", meta, rows_a, _FLY, cache=False)
        dens_b = fp._born_inv_density("R", meta, rows_b, _FLY, cache=False)
        assert not np.allclose(dens_a, dens_b)
        assert dens_b[1] > dens_a[1] and dens_b[1] > 1.0
        # The draw's weights carry each set's own array.
        w_a = np.diff(np.concatenate([[0.0], cdf_a]))
        w_b = np.diff(np.concatenate([[0.0], cdf_b]))
        assert w_b[1] / w_b[0] != pytest.approx(w_a[1] / w_a[0])
        # The cache alone would hand set B set A's array: the defect the flag avoids.
        cached = fp._born_inv_density("R", meta, rows_a, _FLY)
        assert fp._born_inv_density("R", meta, rows_b, _FLY) is cached

    def test_the_born_margin_reads_the_shifted_carry(self):
        # A sea-level 345-ft ball born at Coors (+20): 365 in Coors air, 35
        # short of Coors' 400 fence — a 5-ft band draws the -35 row (B) only
        # when the shift is on; off, the ball is 55 short and nothing sits
        # within 5 of it (the fallback).
        born = {**_BORN_SHORT, "dist": 345.0, "venue": _PNC}
        fp = self._fp(band=5.0)
        fp.carry_offset = True
        assert _draws(fp, born_bb=born, venue_id=_COORS) == {"field_out"}
        assert fp.bb_margin_counts.tolist() == [40, 0, 0]
        off = self._fp(band=5.0)
        assert _draws(off, born_bb=born, venue_id=_COORS) == {"double", "field_out", "single"}
        assert off.bb_margin_counts.tolist() == [0, 40, 0]


# ===========================================================================
# Byte-identity and the factory
# ===========================================================================


class TestOffIsByteIdentical:
    def test_the_flags_on_without_the_tables_match_the_flags_off(self):
        born = {"cls": _FLY, "spray_raw": 0.0, "spray": 0.0, "dist": 385.0, "venue": _PNC}

        def run(configure) -> list[str]:
            fp = _sampler(_pool(_MIXED, with_venue=False), _geometry(offsets=False), seed=4)
            fp.fence_stage = True
            fp.bb_class_filter = True
            configure(fp)
            return _seq(fp, born_bb=born, venue_id=_COORS, live_season=2024)

        base = run(lambda fp: None)

        def on(fp):
            fp.carry_offset = True  # no offset table -> no shift
            fp.bb_born_per_feature = True  # the kernel is off (sigma 0)
            fp.bb_margin_band = 15.0  # no venue column -> every margin NaN

        assert run(on) == base
        # Nothing new counted on the off sampler; the band fell back on the on one.
        assert base == run(lambda fp: setattr(fp, "carry_offset", True))

    def test_the_kernel_off_ignores_the_ruler_and_the_exponent(self):
        rows = list(_MIXED)
        born = {"cls": _FLY, "ev": 100.0, "la": 28.0, "spray": 0.0, "dist": 385.0}
        a = _seq(_sampler(_pool(rows), _geometry(), seed=9), born_bb=born)
        fp = _sampler(_pool(rows), _geometry(), seed=9)
        fp.bb_born_per_feature = True
        assert _seq(fp, born_bb=born) == a
        assert _seq(_sampler(_pool(rows), _geometry(), seed=9)) == a


class TestTheFactoryEnv:
    def test_the_four_names(self):
        s = SimpleNamespace()
        apply_fielding_env(s, env={})
        assert s.carry_offset is False and s.bb_born_per_feature is False
        assert s.bb_margin_band == 0.0 and s.bb_margin_min_rows == 20
        apply_fielding_env(
            s,
            env={
                "SIM_CARRY_OFFSET": "1",
                "SIM_BB_BORN_PER_FEATURE": "on",
                "SIM_BB_MARGIN_BAND": "15",
                "SIM_BB_MARGIN_MIN_ROWS": "30",
            },
        )
        assert s.carry_offset is True and s.bb_born_per_feature is True
        assert s.bb_margin_band == 15.0 and s.bb_margin_min_rows == 30
        apply_fielding_env(s, env={"SIM_BB_MARGIN_BAND": "x", "SIM_BB_MARGIN_MIN_ROWS": "y"})
        assert s.bb_margin_band == 0.0 and s.bb_margin_min_rows == 20

    def test_the_unit_lane_pins_them_off(self):
        assert os.environ.get("SIM_CARRY_OFFSET") == "0"
        assert os.environ.get("SIM_BB_BORN_PER_FEATURE") == "0"
        assert os.environ.get("SIM_BB_MARGIN_BAND") == "0"

    def test_the_compose_file_carries_the_production_values(self):
        root = os.path.join(os.path.dirname(__file__), "..", "..")
        with open(os.path.join(root, "docker-compose.yml"), encoding="utf-8") as fh:
            text = fh.read()
        want = {
            "SIM_CARRY_OFFSET": "1",
            "SIM_BB_BORN_PER_FEATURE": "1",
            "SIM_BB_MARGIN_BAND": "15",
            "SIM_BB_MARGIN_MIN_ROWS": "20",
        }
        for name, value in want.items():
            line = next(ln for ln in text.splitlines() if ln.strip().startswith(name + ":"))
            assert line.split(":", 1)[1].strip().strip('"') == value, line


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
