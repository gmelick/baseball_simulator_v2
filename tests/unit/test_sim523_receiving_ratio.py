"""
tests/unit/test_sim523_receiving_ratio.py
=========================================
SIM-523 part E — the catcher RECEIVING ratio on taken pitches, built OFF
(plan: docs/audit/2026-09-08-sim523-play-picker-redesign-plan.md, part E; the
loop's step 4, last factor; fitting and enabling is SIM-526).

On taken pitches (called strikes and balls) a called-strike row is weighted by
the live catcher's framing multiplier at its zone group, a ball row by the
mirror, a got-away row by his blocking ratio (got-aways above expectation) and
the other taken rows by its mirror; then the taken group is rescaled so its
total weight is unchanged. What these tests pin:

  * the builder derives the league rates and every catcher's multipliers
    from the pool (with the priors and the clamps) and the loader reads them;
  * the zone groups and the blocking cells;
  * the factor's values: multiplier, mirror, blocking ratio, neutral rows;
  * the invariant the plan asks for: the taken group's total weight and every
    swung-at weight are unchanged for any catcher — on the single draw's
    whole-pool path, its cell path, and the result draw of the split;
  * a good framer draws more called strikes among taken pitches, never more
    taken pitches; the pitch draw of the split never sees the factor;
  * OFF is byte-identical; an unknown catcher, a bundle without the document
    or without zones is neutral; the old kernel's names are gone;
  * the factory env reads.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import duckdb
import numpy as np
import pytest

from pipeline.batch.engine_artifacts import (
    RECV_BLOCK_PRIOR,
    RECV_FRAME_PRIOR,
    EngineArtifacts,
    HandPool,
    build_receiving_profiles,
    recv_block_cell,
    recv_zone_group,
)
from simulation.full_pool_sampler import FullPoolSampler
from simulation.production_factory import apply_result_split_env

_SEASON = 2024
_PITCHER = "100:2024"
_BATTER = "200:2024"
_BASE_OUT = np.array([0, 0, 5, 0], dtype=np.float32)
_FASTBALL = np.array([95.0, 15.0, 5.0, 2300.0, 200.0, -1.0, 6.0, 6.5, 0.0, 2.5], dtype=np.float32)

# ===========================================================================
# The builder
# ===========================================================================

_DDL = """
CREATE SCHEMA IF NOT EXISTS sim;
CREATE TABLE sim.pitch_pool (
    pitch_id BIGINT, season SMALLINT, catcher_id INTEGER, zone SMALLINT, plate_z FLOAT,
    velo FLOAT, outcome_type VARCHAR, got_away BOOLEAN
);
"""


def _seed(con: duckdb.DuckDBPyConnection) -> None:
    """Outside the zone (zone 11, high pitches): the league calls 20% of taken
    pitches strikes. Catcher 700 gets 60% (a great framer, 1,000 taken);
    catcher 701 gets 0% (200 taken); catcher 702 has 10 taken pitches, all
    strikes (a tiny sample, shrunk hard). Low pitches (zone 13, plate_z 0.3):
    the league got-away rate is 10%; 700 lets 30% get away, 701 none."""
    con.execute(_DDL)
    pid = 0

    def add(catcher, zone, pz, outcome, ga, n):
        nonlocal pid
        for _ in range(n):
            pid += 1
            con.execute(
                "INSERT INTO sim.pitch_pool VALUES (?, ?, ?, ?, ?, 92.0, ?, ?)",
                [pid, _SEASON, catcher, zone, pz, outcome, ga],
            )

    # 700: 600 called strikes, 400 balls outside (high); 701: 200 balls; 702: 10 strikes;
    # a fourth catcher 703 supplies league mass: 190 strikes, 1,800 balls.
    add(700, 11, 3.0, "called_strike", False, 600)
    add(700, 11, 3.0, "ball", False, 400)
    add(701, 11, 3.0, "ball", False, 200)
    add(702, 11, 3.0, "called_strike", False, 10)
    add(703, 11, 3.0, "called_strike", False, 190)
    add(703, 11, 3.0, "ball", False, 1800)
    # low pitches, all balls: 700 lets 30 of 100 get away; 701 0 of 100; 703 70 of 800.
    add(700, 13, 0.3, "ball", True, 30)
    add(700, 13, 0.3, "ball", False, 70)
    add(701, 13, 0.3, "ball", False, 100)
    add(703, 13, 0.3, "ball", True, 70)
    add(703, 13, 0.3, "ball", False, 730)
    # a swung-at pitch never counts
    add(700, 5, 2.5, "swinging_strike", False, 50)


class TestTheBuilder:
    def test_rates_priors_and_clamps(self, tmp_path):
        con = duckdb.connect(":memory:")
        try:
            _seed(con)
            doc = build_receiving_profiles(con, str(tmp_path), [_SEASON])
        finally:
            con.close()
        # The league: outside (zones 11-14, the low pitches included) 800
        # strikes of 4,200 taken; no heart or edge taken pitches -> absent; the
        # low-pitch got-away cell 100 of 1,000.
        assert doc["league_frame"]["2024"]["outside"] == pytest.approx(800 / 4200)
        assert doc["league_frame_all"]["outside"] == pytest.approx(800 / 4200)
        assert "heart" not in doc["league_frame"]["2024"]
        low_out = 0 * 2 + 0  # bucket 0 (below 0.5 ft), outside the zone
        assert doc["league_block"]["2024"][low_out] == pytest.approx(100 / 1000)
        assert doc["league_block_all"][low_out] == pytest.approx(100 / 1000)
        c700, c701, c702 = (
            doc["catchers"]["700:2024"],
            doc["catchers"]["701:2024"],
            doc["catchers"]["702:2024"],
        )
        L = 800 / 4200
        # 700: 600 strikes of 1,100 taken outside (the 100 low balls count), shrunk
        # toward the league with 200 pitches of prior weight.
        assert c700["frame"]["outside"] == pytest.approx(
            (600 + L * RECV_FRAME_PRIOR) / (1100 + RECV_FRAME_PRIOR) / L, abs=1e-4
        )
        # 701: no strikes in 300 taken -> (0 + 200 L) / 500 / L = 0.4.
        assert c701["frame"]["outside"] == pytest.approx(
            (L * RECV_FRAME_PRIOR) / (300 + RECV_FRAME_PRIOR) / L, abs=1e-4
        )
        # 702: ten strikes in ten, shrunk hard: (10 + 200 L) / 210 / L ~ x 1.25, not x 5.
        assert c702["frame"]["outside"] == pytest.approx(
            (10 + L * RECV_FRAME_PRIOR) / (10 + RECV_FRAME_PRIOR) / L, abs=1e-4
        )
        assert c702["frame"]["outside"] < 1.3
        # Groups a catcher never saw read 1.0.
        assert c700["frame"]["heart"] == 1.0 and c700["frame"]["edge"] == 1.0
        # Blocking: 700 let 30 get away against 10 expected -> (30 + 5) / (10 + 5) = 2.33;
        # 701 none against 10 expected -> 5 / 15 = 0.33.
        assert c700["block"] == pytest.approx(
            (30 + RECV_BLOCK_PRIOR) / (10 + RECV_BLOCK_PRIOR), abs=1e-4
        )
        assert c701["block"] == pytest.approx(RECV_BLOCK_PRIOR / (10 + RECV_BLOCK_PRIOR), abs=1e-4)
        assert c700["taken"] == 1100  # the swung-at pitches never count
        with open(tmp_path / "receiving.json", encoding="utf-8") as fh:
            assert json.load(fh)["catchers"]["700:2024"] == c700

    def test_zone_groups_and_block_cells(self):
        z = np.array([0, 1, 4, 5, 6, 9, 11, 14, 10, 15])
        assert recv_zone_group(z).tolist() == [0, 2, 2, 1, 2, 2, 3, 3, 0, 0]
        pz = np.array([0.2, 0.7, 1.2, 2.5, 0.2, 2.5])
        zz = np.array([11, 11, 11, 11, 5, 5])
        valid = np.array([True, True, True, True, True, False])
        assert recv_block_cell(zz, pz, valid).tolist() == [0, 2, 4, 6, 1, -1]
        assert recv_block_cell(np.array([0]), np.array([2.0]), np.array([True])).tolist() == [-1]


def _minimal_pitch_pool(tmp_path) -> None:
    d = tmp_path / "pitch_pool"
    os.makedirs(d, exist_ok=True)
    with open(d / "manifest.json", "w", encoding="utf-8") as fh:
        json.dump({"seasons": [2024], "counts": {"L": 0, "R": 0}}, fh)
    con = duckdb.connect(":memory:")
    for hand in ("L", "R"):
        np.save(d / f"{hand}.geom.npy", np.zeros((0, 3), dtype=np.float32))
        np.save(d / f"{hand}.sit.npy", np.zeros((0, 6), dtype=np.float32))
        con.execute(
            "COPY (SELECT 0::BIGINT AS pitch_id, 0::BIGINT AS pitcher_id, "
            "0::BIGINT AS batter_id, 0::BIGINT AS season, ''::VARCHAR AS outcome_type, "
            "1.0::FLOAT AS recency_weight WHERE 1=0) "
            f"TO '{(d / f'{hand}.meta.parquet').as_posix()}' (FORMAT parquet)"
        )
    con.close()


class TestTheLoader:
    def test_load_reads_the_document_or_none(self, tmp_path):
        _minimal_pitch_pool(tmp_path)
        assert EngineArtifacts.load(str(tmp_path)).receiving is None
        con = duckdb.connect(":memory:")
        try:
            _seed(con)
            build_receiving_profiles(con, str(tmp_path), [_SEASON])
        finally:
            con.close()
        rv = EngineArtifacts.load(str(tmp_path)).receiving
        assert rv is not None and "700:2024" in rv["catchers"]


# ===========================================================================
# The factor and the invariant
# ===========================================================================

#: The league: outside pitches are called strikes 20% of the time; low outside
#: pitches get away 10% of the time.
_TABLE = {
    "groups": ["heart", "edge", "outside"],
    # Per season (the rows' own 2024), with the pooled rates as the fallback.
    "league_frame": {"2024": {"heart": 0.99, "edge": 0.85, "outside": 0.2}},
    "league_frame_all": {"heart": 0.99, "edge": 0.85, "outside": 0.2},
    # (the high-pitch cells 6 and 7 read 0 so the framing tests see framing alone)
    "league_block": {"2024": [0.10, 0.02, 0.01, 0.005, 0.002, 0.001, 0.0, 0.0]},
    "league_block_all": [0.10, 0.02, 0.01, 0.005, 0.002, 0.001, 0.0, 0.0],
    "catchers": {
        "700:2024": {"frame": {"heart": 1.0, "edge": 1.05, "outside": 2.0}, "block": 0.5},
        "701:2024": {"frame": {"heart": 1.0, "edge": 0.95, "outside": 0.5}, "block": 2.0},
    },
}


def _pool(
    outcomes: list[str],
    zones: list[int],
    *,
    got_away: list[int] | None = None,
    plate_z: float = 3.0,
) -> HandPool:
    n = len(outcomes)
    sit = np.zeros((n, 6), dtype=np.float32)
    sit[:, 4] = 5.0
    geom = np.stack([_FASTBALL] * n).astype(np.float32)
    geom[:, 9] = plate_z
    return HandPool(
        geom=geom,
        sit=sit,
        pitcher_id=np.full(n, 100, dtype=np.int64),
        batter_id=np.full(n, 200, dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        outcome_type=np.asarray(outcomes, dtype=object),
        recency=np.ones(n, dtype=np.float32),
        catcher_id=np.full(n, 703, dtype=np.int64),
        got_away=(
            np.asarray(got_away, dtype=np.int8)
            if got_away is not None
            else np.zeros(n, dtype=np.int8)
        ),
        zone=np.asarray(zones, dtype=np.int8),
    )


#: Eight outside pitches: two called strikes, four balls, two swung at.
_OUTCOMES = ["called_strike"] * 2 + ["ball"] * 4 + ["swinging_strike", "foul"]
_ZONES = [11] * 8


def _sampler(
    pool: HandPool, *, table: dict | None = _TABLE, seed: int = 0, cell: bool = False
) -> FullPoolSampler:
    art = EngineArtifacts(
        pools={"R": pool},
        pitcher_sim={_PITCHER: {_PITCHER: 1.0}},
        pitcher_sim_index={_PITCHER: 0},
        receiving=table,
    )
    fp = FullPoolSampler(art, np.random.default_rng(seed))
    fp.catcher_receiving = True
    if cell:
        fp.pitch_cell_index = True
        fp.pitch_min_cell = 0
    return fp


def _weights(fp: FullPoolSampler, catcher: str | None) -> np.ndarray:
    """The single draw's 0-0 bucket weights (from its cumulative sum)."""
    fp.new_half_inning("R", _PITCHER, catcher_key=catcher)
    fp.new_plate_appearance(_BATTER, _BASE_OUT)
    cdf = fp._bucket_cdf[0]
    assert cdf is not None
    return np.diff(np.concatenate([[0.0], cdf]))


class TestTheFactor:
    def test_multiplier_mirror_and_blocking(self):
        fp = _sampler(_pool(_OUTCOMES, _ZONES))
        f = fp._recv_factor("R", "700:2024")
        assert f is not None
        # Called strikes x 2.0; balls x (1 - 0.2 x 2) / (1 - 0.2) = 0.75; swung x 1.
        assert f[:2].tolist() == [2.0, 2.0]
        np.testing.assert_allclose(f[2:6], 0.75)
        assert f[6:].tolist() == [1.0, 1.0]
        # The worse framer: x 0.5 and the mirror (1 - 0.1) / 0.8 = 1.125.
        g = fp._recv_factor("R", "701:2024")
        assert g is not None and g[0] == pytest.approx(0.5) and g[2] == pytest.approx(1.125)
        # Blocking on low pitches (cell 0, league 10%): a got-away ball x 0.5
        # for 700, the other taken rows x (1 - 0.1 x 0.5) / 0.9.
        low = _sampler(_pool(["ball"] * 4, [13] * 4, got_away=[1, 0, 0, 0], plate_z=0.3))
        h = low._recv_factor("R", "700:2024")
        assert h is not None
        assert h[0] == pytest.approx(0.75 * 0.5)  # the ball mirror x the got-away ratio
        assert h[1] == pytest.approx(0.75 * (1 - 0.1 * 0.5) / 0.9)

    def test_a_row_season_the_document_lacks_uses_the_pooled_rates(self):
        # No 2024 entry; the pooled outside rate 0.5: a x2 framer's ball mirror
        # (1 - 0.5 x 2) / 0.5 reads 0 and is clipped there; the strikes x2.
        table = dict(_TABLE)
        table["league_frame"] = {"2019": {"heart": 0.99, "edge": 0.85, "outside": 0.1}}
        table["league_frame_all"] = {"heart": 0.99, "edge": 0.85, "outside": 0.5}
        fp = _sampler(_pool(_OUTCOMES, _ZONES), table=table)
        f = fp._recv_factor("R", "700:2024")
        assert f is not None and f[0] == pytest.approx(2.0) and f[2] == pytest.approx(0.0)

    def test_neutral_rows_and_missing_data(self):
        # An unknown zone group and a swung-at row read 1.0.
        fp = _sampler(_pool(["called_strike", "ball", "foul"], [0, 0, 11]))
        f = fp._recv_factor("R", "700:2024")
        assert f is not None and f.tolist() == [1.0, 1.0, 1.0]
        # An unknown catcher, no document, no zones: None.
        assert fp._recv_factor("R", "999:2024") is None
        assert _sampler(_pool(_OUTCOMES, _ZONES), table=None)._recv_factor("R", "700:2024") is None
        pool = _pool(_OUTCOMES, _ZONES)
        pool.zone = None
        assert _sampler(pool)._recv_factor("R", "700:2024") is None


class TestTheInvariant:
    @pytest.mark.parametrize("cell", [False, True])
    def test_the_taken_mass_and_every_swung_weight_are_unchanged(self, cell):
        for catcher in ("700:2024", "701:2024"):
            off = _weights(_sampler(_pool(_OUTCOMES, _ZONES), cell=cell), None)
            on = _weights(_sampler(_pool(_OUTCOMES, _ZONES), cell=cell), catcher)
            taken = np.array([True] * 6 + [False] * 2)
            assert on[taken].sum() == pytest.approx(off[taken].sum(), rel=1e-6)
            np.testing.assert_array_equal(on[~taken], off[~taken])
            # And the split moved: the great framer's strikes carry more.
            if catcher == "700:2024":
                assert on[0] > off[0] and on[2] < off[2]
            else:
                assert on[0] < off[0] and on[2] > off[2]

    def test_a_good_framer_draws_more_called_strikes_among_taken_and_no_more_taken(self):
        def shares(catcher):
            fp = _sampler(_pool(_OUTCOMES, _ZONES), seed=5)
            fp.new_half_inning("R", _PITCHER, catcher_key=catcher)
            fp.new_plate_appearance(_BATTER, _BASE_OUT)
            draws = [fp.draw(0, 0) for _ in range(3000)]
            taken = [d for d in draws if d in ("called_strike", "ball")]
            return len(taken) / len(draws), draws.count("called_strike") / max(1, len(taken))

        taken_off, cs_off = shares(None)
        taken_700, cs_700 = shares("700:2024")
        taken_701, cs_701 = shares("701:2024")
        # The pool: 6 of 8 taken (0.75); 2 of 6 taken called strikes (0.333).
        assert (
            abs(taken_off - 0.75) < 0.03
            and abs(taken_700 - 0.75) < 0.03
            and abs(taken_701 - 0.75) < 0.03
        )
        assert cs_700 > cs_off + 0.15  # 2.0 x 2 vs 0.75 x 4: 4 / 7 = 0.57
        assert cs_701 < cs_off - 0.10  # 0.5 x 2 vs 1.125 x 4: 1 / 5.5 = 0.18

    def test_the_split_weights_the_result_draw_not_the_pitch_draw(self):
        fp = _sampler(_pool(_OUTCOMES, _ZONES), cell=True)
        fp.pitch_result_split = True
        fp.result_pitch_sigma = 1e6
        fp.result_density_power = 0.0
        fp.new_half_inning("R", _PITCHER, catcher_key="700:2024")
        fp.new_plate_appearance(_BATTER, _BASE_OUT)
        # The pitch draw's weights carry no receiving factor: uniform here.
        cdf = fp._bucket_cdf[0]
        np.testing.assert_allclose(np.diff(np.concatenate([[0.0], cdf])), 1.0)
        assert fp._bucket_recv is not None and fp._bucket_recv[0] is not None
        # The result draw does: called strikes come back more often than balls
        # among taken results, and the taken share stays at 6 of 8.
        draws = [fp.draw(0, 0) for _ in range(3000)]
        taken = [d for d in draws if d in ("called_strike", "ball")]
        assert abs(len(taken) / len(draws) - 0.75) < 0.03
        assert draws.count("called_strike") / max(1, len(taken)) > 0.5

    def test_off_is_byte_identical(self):
        def seq(on: bool, catcher):
            fp = _sampler(_pool(_OUTCOMES, _ZONES), seed=9)
            fp.catcher_receiving = on
            fp.new_half_inning("R", _PITCHER, catcher_key=catcher)
            fp.new_plate_appearance(_BATTER, _BASE_OUT)
            return [fp.draw(0, 0) for _ in range(40)]

        assert seq(False, "700:2024") == seq(False, None) == seq(True, None)

    def test_the_old_kernel_is_gone(self):
        fp = _sampler(_pool(_OUTCOMES, _ZONES))
        for name in ("catcher_framing_sigma", "catcher_block_sigma", "_f_catcher_receiving"):
            assert not hasattr(fp, name)


class TestTheFactoryEnv:
    def test_switch(self):
        s = SimpleNamespace()
        apply_result_split_env(s, env={})
        assert s.catcher_receiving is False
        apply_result_split_env(s, env={"SIM_CATCHER_RECEIVING": "1"})
        assert s.catcher_receiving is True
        assert os.environ.get("SIM_CATCHER_RECEIVING") == "0"
        assert "SIM_CATCHER_FRAMING_SIGMA" not in os.environ


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
