"""SIM-468 / SIM-474 — the steal opportunity pool and the steal draw.

Production attempted ZERO steals from 2026-06-04 to 2026-08-16 (SIM-495
measured SB 0.0000 against 0.59): the manager green-light gated the decision
and then handed it to a resolver stub. SIM-474 deletes the gate and the tuned
fallback; the decision is a similarity-weighted draw from the SIM-468
opportunity pool — one row per pitch where a steal was POSSIBLE, attempted or
not. The drawn row's `attempted` flag says whether the runner goes; its
`success` flag says safe or caught; manager aggression is a WEIGHT on
attempted rows, never a gate.

Three layers, mirroring the seams:

  * the computor build (`_build_steal_opportunity_pool`) over an in-memory
    DuckDB emulating pg.raw.pitches — the opportunity filter, the lead-runner
    mapping, and the flag selection per target base;
  * the artifact round-trip (`build_steal_pool_artifact` ->
    `EngineArtifacts.load`), including the legacy-bundle fallback;
  * the sampler draw (`FullPoolSampler.steal_draw`) and the loop wiring
    (`StateMachine._steal_opportunity_draw`) with a duck-typed sampler, the
    pattern of `test_realism_consumers_sim411_413_425b._FakeFramingFP`.
"""

from __future__ import annotations

import json
import os

import duckdb
import numpy as np
import pytest

from pipeline.batch.engine_artifacts import (
    EngineArtifacts,
    StealPool,
    build_steal_pool_artifact,
)
from pipeline.batch.player_profile_computor import PlayerProfileComputor
from simulation.full_pool_sampler import FullPoolSampler
from simulation.game_state import GameState, Half
from simulation.sim_loop import StateMachine, Team

# ---------------------------------------------------------------------------
# The computor build
# ---------------------------------------------------------------------------

_POOL_DDL = """
CREATE TABLE sim.steal_opportunity_pool (
    pitch_id BIGINT NOT NULL, game_pk INTEGER NOT NULL,
    at_bat_number INTEGER NOT NULL, pitch_number INTEGER NOT NULL,
    game_date DATE NOT NULL, season SMALLINT NOT NULL,
    runner_id INTEGER NOT NULL, pitcher_id INTEGER NOT NULL, catcher_id INTEGER,
    target_base SMALLINT NOT NULL CHECK (target_base IN (2, 3)),
    inning SMALLINT NOT NULL, outs SMALLINT NOT NULL CHECK (outs BETWEEN 0 AND 2),
    count_balls SMALLINT NOT NULL, count_strikes SMALLINT NOT NULL,
    score_diff SMALLINT NOT NULL,
    attempted BOOLEAN NOT NULL, success BOOLEAN NOT NULL DEFAULT FALSE,
    recency_weight FLOAT NOT NULL DEFAULT 1.0,
    PRIMARY KEY (pitch_id)
)
"""


def _conn() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    c.execute("ATTACH ':memory:' AS pg")
    c.execute("CREATE SCHEMA pg.raw")
    c.execute(
        "CREATE TABLE pg.raw.pitches ("
        "game_pk INTEGER, at_bat_number INTEGER, pitch_number INTEGER, "
        "game_date DATE, season SMALLINT, pitcher INTEGER, fielder_2 INTEGER, "
        "inning SMALLINT, outs SMALLINT, balls SMALLINT, strikes SMALLINT, "
        "bat_score SMALLINT, fld_score SMALLINT, "
        "on_1b INTEGER, on_2b INTEGER, on_3b INTEGER, "
        "sb_attempt_2b BOOLEAN, sb_attempt_3b BOOLEAN, "
        "sb_success_2b BOOLEAN, sb_success_3b BOOLEAN, "
        "data_quality_flag BOOLEAN)"
    )
    c.execute("CREATE SCHEMA sim")
    c.execute(
        "CREATE TABLE sim.pitch_pool ("
        "pitch_id BIGINT, game_pk INTEGER, at_bat_number INTEGER, pitch_number INTEGER)"
    )
    c.execute(_POOL_DDL)
    c.execute(
        "CREATE TABLE sim.pool_build_metadata ("
        "pool_name VARCHAR, season SMALLINT, row_count BIGINT, "
        "source_max_game_date DATE, source_row_count BIGINT, "
        "recency_ref_season SMALLINT, builder_version VARCHAR, "
        "built_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY (pool_name, season))"
    )
    return c


def _pitch(
    c,
    pk,
    ab,
    pn,
    *,
    on_1b=None,
    on_2b=None,
    on_3b=None,
    att2=False,
    att3=False,
    suc2=False,
    suc3=False,
    outs=0,
    balls=0,
    strikes=0,
    flag=False,
):
    pid = pk * 10000 + ab * 100 + pn
    c.execute(
        "INSERT INTO pg.raw.pitches VALUES (?,?,?,DATE '2024-06-01',2024,901,902,1,?,?,?,3,1,"
        "?,?,?,?,?,?,?,?)",
        [pk, ab, pn, outs, balls, strikes, on_1b, on_2b, on_3b, att2, att3, suc2, suc3, flag],
    )
    c.execute("INSERT INTO sim.pitch_pool VALUES (?,?,?,?)", [pid, pk, ab, pn])
    return pid


def _build(c) -> None:
    comp = PlayerProfileComputor.__new__(PlayerProfileComputor)
    comp._conn = c
    comp._build_steal_opportunity_pool([2024], incremental=False)


class TestTheComputorBuild:
    def test_an_open_base_is_an_opportunity_attempted_or_not(self):
        c = _conn()
        _pitch(c, 1, 1, 1, on_1b=11)  # 1B occupied, 2B open, no attempt
        _pitch(c, 1, 1, 2, on_1b=11, att2=True, suc2=True)  # the attempt
        _build(c)
        rows = c.execute(
            "SELECT target_base, runner_id, attempted, success "
            "FROM sim.steal_opportunity_pool ORDER BY pitch_id"
        ).fetchall()
        assert rows == [(2, 11, False, False), (2, 11, True, True)]

    def test_the_lead_runner_drives_the_pair(self):
        c = _conn()
        # Runner on 2B only -> target 3. Runners on 1B+2B -> STILL target 3
        # (the lead runner drives a double steal; 2B is not open for the
        # trailing runner's own decision).
        _pitch(c, 1, 2, 1, on_2b=22, att3=True, suc3=False)
        _pitch(c, 1, 3, 1, on_1b=11, on_2b=22)
        _build(c)
        rows = c.execute(
            "SELECT target_base, runner_id, attempted, success "
            "FROM sim.steal_opportunity_pool ORDER BY pitch_id"
        ).fetchall()
        assert rows == [(3, 22, True, False), (3, 22, False, False)]

    def test_no_opportunity_rows(self):
        c = _conn()
        _pitch(c, 1, 4, 1)  # bases empty
        _pitch(c, 1, 5, 1, on_1b=11, on_2b=22, on_3b=33)  # bases loaded: nothing open
        _pitch(c, 1, 6, 1, on_1b=11, flag=True)  # data-quality flagged
        _build(c)
        assert c.execute("SELECT COUNT(*) FROM sim.steal_opportunity_pool").fetchone()[0] == 0

    def test_the_positional_insert_matches_the_ddl(self):
        """The INSERT has no column list, so the SELECT order must match the
        DDL exactly — the trap the 0012 note in 02_duckdb_schema.sql records."""
        c = _conn()
        _pitch(c, 1, 1, 1, on_1b=11, att2=True, suc2=True, outs=1, balls=2, strikes=1)
        _build(c)
        row = c.execute(
            "SELECT game_pk, at_bat_number, pitch_number, season, runner_id, "
            "pitcher_id, catcher_id, target_base, inning, outs, count_balls, "
            "count_strikes, score_diff, attempted, success "
            "FROM sim.steal_opportunity_pool"
        ).fetchone()
        assert row == (1, 1, 1, 2024, 11, 901, 902, 2, 1, 1, 2, 1, 2, True, True)

    def test_metadata_recorded_under_the_pool_name(self):
        c = _conn()
        _pitch(c, 1, 1, 1, on_1b=11)
        _build(c)
        got = c.execute("SELECT pool_name, row_count FROM sim.pool_build_metadata").fetchone()
        assert got == ("steal_opportunity_pool", 1)


# ---------------------------------------------------------------------------
# The artifact round-trip
# ---------------------------------------------------------------------------


def _minimal_pitch_pool_manifest(tmp_path) -> None:
    """load() requires the PITCH pool's files for both hands; zero rows do."""
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


class TestTheArtifactRoundTrip:
    def test_export_then_load(self, tmp_path):
        c = _conn()
        _pitch(c, 1, 1, 1, on_1b=11)
        _pitch(c, 1, 1, 2, on_1b=11, att2=True, suc2=True)
        _pitch(c, 1, 2, 1, on_2b=22, att3=True)
        _build(c)
        counts = build_steal_pool_artifact(c, str(tmp_path), [2024])
        assert counts == {"2": 2, "3": 1}
        _minimal_pitch_pool_manifest(tmp_path)
        art = EngineArtifacts.load(str(tmp_path))
        assert set(art.steal_pools) == {"2", "3"}
        p2 = art.steal_pools["2"]
        assert p2.n == 2
        assert p2.attempted.tolist() == [0, 1]
        assert p2.success.tolist() == [0, 1]
        assert p2.runner_id.tolist() == [11, 11]
        assert p2.sit.shape == (2, 4)

    def test_a_legacy_bundle_has_no_steal_pools(self, tmp_path):
        _minimal_pitch_pool_manifest(tmp_path)
        art = EngineArtifacts.load(str(tmp_path))
        assert art.steal_pools == {}

    def test_the_pool_arrays_are_shareable(self, tmp_path):
        c = _conn()
        _pitch(c, 1, 1, 1, on_1b=11, att2=True)
        _build(c)
        build_steal_pool_artifact(c, str(tmp_path), [2024])
        _minimal_pitch_pool_manifest(tmp_path)
        art = EngineArtifacts.load(str(tmp_path))
        shared = art.extract_shared_arrays()
        assert "steal_pool.2.sit" in shared
        assert "steal_pool.2.attempted" in shared
        # Attach round-trip: a replacement view lands on the pool.
        new_att = np.zeros_like(art.steal_pools["2"].attempted)
        art.attach_shared_views({"steal_pool.2.attempted": new_att})
        assert art.steal_pools["2"].attempted is new_att


# ---------------------------------------------------------------------------
# The sampler draw
# ---------------------------------------------------------------------------


def _steal_pool(n, attempted, success=None, *, runner_ids=None, sit=None) -> StealPool:
    att = np.asarray(attempted, dtype=np.int8)
    return StealPool(
        sit=(
            np.asarray(sit, dtype=np.float32)
            if sit is not None
            else np.zeros((n, 4), dtype=np.float32)
        ),
        runner_id=(
            np.asarray(runner_ids, dtype=np.int64)
            if runner_ids is not None
            else np.full(n, 11, dtype=np.int64)
        ),
        pitcher_id=np.full(n, 901, dtype=np.int64),
        catcher_id=np.full(n, 902, dtype=np.int64),
        season=np.full(n, 2024, dtype=np.int64),
        attempted=att,
        success=(np.asarray(success, dtype=np.int8) if success is not None else att.copy()),
        recency=np.ones(n, dtype=np.float32),
    )


def _sampler(steal_pools, actor_emb=None, seed=7) -> FullPoolSampler:
    art = EngineArtifacts({}, steal_pools=steal_pools, actor_emb=actor_emb or {})
    return FullPoolSampler(art, np.random.default_rng(seed))


def _attempt_rate(fp, n_draws=2000, **kw) -> float:
    kw.setdefault("outs", 0)
    kw.setdefault("balls", 0)
    kw.setdefault("strikes", 0)
    kw.setdefault("score_diff", 0)
    hits = 0
    for _ in range(n_draws):
        drawn = fp.steal_draw(2, "11:2024", "901:2024", "902:2024", **kw)
        assert drawn is not None
        hits += int(drawn[0])
    return hits / n_draws


class TestTheSamplerDraw:
    def test_the_attempt_rate_is_the_pools_own(self):
        # 10% of opportunity rows are attempts; with neutral weights the drawn
        # attempt rate must track it. This is the denominator working: the old
        # attempts-only pool would attempt 100% of the time.
        fp = _sampler({"2": _steal_pool(1000, [1] * 100 + [0] * 900)})
        assert _attempt_rate(fp) == pytest.approx(0.10, abs=0.03)

    def test_the_cell_filter_is_exact(self):
        # All attempts sit in the (0,0,0) cell; the (1,0,0) cell holds none.
        sit = np.zeros((200, 4), dtype=np.float32)
        sit[100:, 2] = 1.0  # outs=1 rows
        fp = _sampler({"2": _steal_pool(200, [1] * 100 + [0] * 100, sit=sit)})
        assert _attempt_rate(fp, n_draws=300) == 1.0
        assert _attempt_rate(fp, n_draws=300, outs=1) == 0.0

    def test_manager_aggression_weights_the_attempt(self):
        fp = _sampler({"2": _steal_pool(1000, [1] * 100 + [0] * 900)})
        low = _attempt_rate(fp, aggression=0.05)
        base = _attempt_rate(fp)
        high = _attempt_rate(fp, aggression=4.0)
        assert low < base < high
        assert low > 0.0  # a weight, never a gate: even 0.05 still steals

    def test_runner_similarity_conditions_the_draw(self):
        # Rows split between a burner (id 11) whose rows are all attempts and a
        # plodder (id 12) whose rows never are. A live runner identical to the
        # burner must draw attempts far more often than one identical to the
        # plodder.
        n = 400
        runner_ids = [11] * 200 + [12] * 200
        attempted = [1] * 200 + [0] * 200
        feats = ["sprint_speed", "sb_attempt_rate", "sb_success_rate", "cs_rate"]
        vecs = np.array([[30.0, 0.4, 0.85, 0.1], [24.0, 0.01, 0.4, 0.5]], dtype=np.float32)
        emb = {
            "key_index": {"11:2024": 0, "12:2024": 1},
            "vecs": vecs,
            "mean": vecs.mean(axis=0),
            "std": vecs.std(axis=0) + 1e-6,
            "features": feats,
        }
        fp = _sampler(
            {"2": _steal_pool(n, attempted, runner_ids=runner_ids)},
            actor_emb={"baserunner": emb},
        )
        burner = 0
        plodder = 0
        for _ in range(800):
            d = fp.steal_draw(2, "11:2024", "", None, outs=0, balls=0, strikes=0, score_diff=0)
            burner += int(d[0])
            d = fp.steal_draw(2, "12:2024", "", None, outs=0, balls=0, strikes=0, score_diff=0)
            plodder += int(d[0])
        assert burner > plodder * 2

    def test_absent_pool_and_empty_cell_return_none(self):
        fp = _sampler({})
        assert (
            fp.steal_draw(2, "11:2024", "", None, outs=0, balls=0, strikes=0, score_diff=0) is None
        )
        fp2 = _sampler({"2": _steal_pool(10, [1] * 10)})
        assert (
            fp2.steal_draw(3, "11:2024", "", None, outs=0, balls=0, strikes=0, score_diff=0) is None
        )

    def test_the_drawn_row_answers_both_questions(self):
        # Every row: attempted, caught. The draw must return (True, False).
        fp = _sampler({"2": _steal_pool(50, [1] * 50, success=[0] * 50)})
        assert fp.steal_draw(2, "11:2024", "", None, outs=0, balls=0, strikes=0, score_diff=0) == (
            True,
            False,
        )


# ---------------------------------------------------------------------------
# The loop wiring
# ---------------------------------------------------------------------------


class _FakeStealFP:
    """Duck-typed sampler exposing only the steal seam (the _FakeFramingFP
    pattern). Records the call so the wiring's arguments are assertable."""

    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    def has_steal_pool(self) -> bool:
        return True

    def steal_draw(self, target, runner_key, pitcher_key, catcher_key, **kw):
        self.calls.append({"target": target, "runner": runner_key, "catcher": catcher_key, **kw})
        return self.result


def _machine_with_fp(fp, manager=None):
    m = StateMachine(rng=np.random.default_rng(5), manager=manager)
    m.full_pool_sampler = fp
    return m


def _state(first=None, second=None, third=None):
    s = GameState(pitcher_id=901, bat_hand="R", season=2024, batter_id=900)
    s.half = Half.TOP
    s.home_catcher_id = 902
    s.away_catcher_id = 903
    if first is not None:
        s.bases.first = first
    if second is not None:
        s.bases.second = second
    if third is not None:
        s.bases.third = third
    return s


class TestTheLoopWiring:
    def test_an_attempted_draw_stages_the_steal(self):
        fp = _FakeStealFP((True, True))
        m = _machine_with_fp(fp)
        s = _state(first=11)
        m._steal_opportunity_draw(s)
        assert m._pending_steal is not None
        assert m._pending_steal.attempted and m._pending_steal.safe
        assert m._pending_steal.runner_id == 11 and m._pending_steal.from_base == 1
        call = fp.calls[0]
        assert call["target"] == 2 and call["runner"] == "11:2024"
        # TOP half: the HOME team fields, so the home catcher deters the steal.
        assert call["catcher"] == "902:2024"

    def test_a_caught_draw_stages_a_caught_steal(self):
        m = _machine_with_fp(_FakeStealFP((True, False)))
        s = _state(second=22)
        m._steal_opportunity_draw(s)
        assert m._pending_steal is not None and not m._pending_steal.safe
        assert m._pending_steal.from_base == 2

    def test_a_no_go_draw_stages_nothing(self):
        m = _machine_with_fp(_FakeStealFP((False, False)))
        m._steal_opportunity_draw(_state(first=11))
        assert m._pending_steal is None

    def test_no_sampler_is_a_no_op(self):
        m = StateMachine(rng=np.random.default_rng(5))
        assert m.full_pool_sampler is None
        m._steal_opportunity_draw(_state(first=11))
        assert m._pending_steal is None

    def test_no_stealable_runner_never_calls_the_sampler(self):
        fp = _FakeStealFP((True, True))
        m = _machine_with_fp(fp)
        m._steal_opportunity_draw(_state())  # bases empty
        m._steal_opportunity_draw(_state(first=11, second=22, third=33))  # loaded
        assert fp.calls == [] and m._pending_steal is None

    def test_the_lead_runner_drives_a_double_steal_spot(self):
        fp = _FakeStealFP((False, False))
        m = _machine_with_fp(fp)
        m._steal_opportunity_draw(_state(first=11, second=22))
        assert fp.calls[0]["target"] == 3 and fp.calls[0]["runner"] == "22:2024"

    def test_manager_aggression_is_a_weight_not_a_gate(self):
        # An aggressive manager raises the weight; a never-runs manager damps
        # it but the draw still happens — the sampler is ALWAYS consulted.
        fp = _FakeStealFP((False, False))
        m = _machine_with_fp(fp, manager={"steal_order_rate_per_1b_opp": 0.32})
        m._steal_opportunity_draw(_state(first=11))
        assert fp.calls[0]["aggression"] > 1.0
        fp2 = _FakeStealFP((False, False))
        m2 = _machine_with_fp(fp2, manager={"steal_order_rate_per_1b_opp": 0.0})
        m2._steal_opportunity_draw(_state(first=11))
        assert 0.0 < fp2.calls[0]["aggression"] <= 0.05

    def test_no_manager_is_neutral(self):
        fp = _FakeStealFP((False, False))
        m = _machine_with_fp(fp)
        m._steal_opportunity_draw(_state(first=11))
        assert fp.calls[0]["aggression"] == 1.0

    def test_the_offense_side_picks_the_defending_catcher(self):
        fp = _FakeStealFP((False, False))
        m = _machine_with_fp(fp)
        s = _state(first=11)
        s.half = Half.BOTTOM  # HOME bats; the AWAY catcher defends
        assert s.offense == Team.HOME
        m._steal_opportunity_draw(s)
        assert fp.calls[0]["catcher"] == "903:2024"
