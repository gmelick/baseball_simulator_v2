"""
test_backend_sim303.py
======================
Unit tests for SIM-303 -- the simulation-loop SCAFFOLD that wires the SIM-302
``PlayPoolSampler`` into a plate-appearance simulator
(``simulation/sim_loop.py``).

Strategy (mirrors ``test_backend_sim302.py``)
---------------------------------------------
Build REAL tiles by running the SIM-301 builder
(``pipeline.batch.play_pool_cache``) against a tiny synthetic DuckDB in a tmp
dir, then construct a ``PlayPoolSampler`` over those tiles and drive the
``PlateAppearanceSimulator`` scaffold for one plate appearance.

Determinism: the sampler is given a fixed-seed ``numpy.random.Generator`` and
the scaffold's own stub-fingerprint rng is fixed too.  To exercise the two
branches deterministically (contact vs no-contact) we inject an
``outcome_fetch`` into the sampler so the pitch outcome is controlled, exactly
as the SIM-302 synthetic fixtures do.

Asserts:
  (a) simulate_pitch invokes the sampler and returns a well-formed PlayResult;
  (b) a non-contact pitch path works (event is not a batted-ball event);
  (c) a contact pitch path triggers sample_batted_ball.
"""

from __future__ import annotations

import numpy as np
import pytest

faiss = pytest.importorskip("faiss")
duckdb = pytest.importorskip("duckdb")

from pipeline.batch import play_pool_cache as ppc
from simulation.play_pool_sampler import POOL_BATTEDBALL, POOL_PITCH, PlayPoolSampler
from simulation.sim_loop import (
    CONTACT_PITCH_OUTCOME,
    PitchState,
    PlateAppearanceSimulator,
    pitch_outcome_to_event,
)

SEASON = 2024
BIG_PITCHER = 477132

PITCH_POOL_DDL = """
CREATE TABLE sim.pitch_pool (
    pitch_id    BIGINT,
    season      SMALLINT,
    pitcher_id  INTEGER,
    stand       VARCHAR(1),
    velo        FLOAT, ivb FLOAT, hb FLOAT, spin_rate FLOAT, spin_axis FLOAT,
    release_x   FLOAT, release_z FLOAT, release_ext FLOAT,
    plate_x     FLOAT, plate_z FLOAT,
    outcome_type VARCHAR(20),
    game_date   DATE
)
"""

OUTCOME_POOL_DDL = """
CREATE TABLE sim.outcome_pool (
    pitch_id    BIGINT,
    season      SMALLINT,
    pitcher_id  INTEGER,
    stand       VARCHAR(1),
    exit_velo   FLOAT, launch_angle FLOAT, spray_angle FLOAT,
    events      VARCHAR(50),
    bb_type     VARCHAR(20),
    game_date   DATE
)
"""

_PITCH_OUTCOMES = ["ball", "called_strike", "swinging_strike", "foul", "in_play"]
_BB_EVENTS = ["single", "double", "triple", "home_run", "field_out"]


def _insert_pitch_rows(
    conn, pitcher_id, bat_hand, n, *, base_id, season=SEASON, game_date="2024-06-01"
):
    rng = np.random.default_rng(abs(hash((pitcher_id, bat_hand))) % (2**32))
    for i in range(n):
        pid = base_id + i
        vec = [
            float(rng.uniform(88, 100)),
            float(rng.uniform(-5, 20)),
            float(rng.uniform(-15, 15)),
            float(rng.uniform(1800, 2600)),
            float(rng.uniform(0, 360)),
            float(rng.uniform(-2.5, 2.5)),
            float(rng.uniform(5, 6.5)),
            float(rng.uniform(5.5, 7)),
            float(rng.uniform(-1.5, 1.5)),
            float(rng.uniform(1.5, 3.5)),
        ]
        conn.execute(
            "INSERT INTO sim.pitch_pool VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                pid,
                season,
                pitcher_id,
                bat_hand,
                *vec,
                _PITCH_OUTCOMES[i % len(_PITCH_OUTCOMES)],
                game_date,
            ],
        )


def _insert_outcome_rows(conn, bat_hand, n, *, base_id, season=SEASON, game_date="2024-06-01"):
    rng = np.random.default_rng(abs(hash(("bb", bat_hand))) % (2**32))
    for i in range(n):
        pid = base_id + i
        ev = float(rng.uniform(60, 110))
        la = float(rng.uniform(-25, 45))
        sa = float(rng.uniform(-45, 45))
        conn.execute(
            "INSERT INTO sim.outcome_pool VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                pid,
                season,
                600000 + (i % 20),
                bat_hand,
                ev,
                la,
                sa,
                _BB_EVENTS[i % len(_BB_EVENTS)],
                "line_drive" if la > 0 else "ground_ball",
                game_date,
            ],
        )


@pytest.fixture()
def round_trip_pool(tmp_path):
    """Build a synthetic DuckDB, run the REAL SIM-301 builder, return
    (pool_dir, duckdb_path).  ~1k pitch rows + ~240 batted-ball rows."""
    db = tmp_path / "baseball_sim.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE SCHEMA IF NOT EXISTS sim")
    conn.execute(PITCH_POOL_DDL)
    conn.execute(OUTCOME_POOL_DDL)

    base = 1_000_000
    for hand in ("L", "R"):
        _insert_pitch_rows(conn, BIG_PITCHER, hand, 500, base_id=base)
        base += 500
    _insert_outcome_rows(conn, "L", 240, base_id=5_000_000)
    _insert_outcome_rows(conn, "R", 240, base_id=6_000_000)
    conn.close()

    pool_dir = tmp_path / "play_pool"
    # recency_boost=False for deterministic neighbour ordering (spec §5).
    res = ppc.build_play_pool_cache(str(db), str(pool_dir), recency_boost=False)
    assert res.rebuilt > 0
    return str(pool_dir), str(db)


def _make_sampler(pool_dir, *, pitch_outcome, seed=0):
    """A sampler over the REAL tiles, but with an injected outcome_fetch that
    forces the PITCH pool to a chosen outcome (so the contact / no-contact
    branch is deterministic) while serving real batted-ball events."""

    def fetch(pool, rids):
        if pool == POOL_PITCH:
            return {int(r): pitch_outcome for r in rids}
        # batted-ball: cycle through a closed vocab so the event is a real BB event
        return {int(r): _BB_EVENTS[int(r) % len(_BB_EVENTS)] for r in rids}

    return PlayPoolSampler(
        pool_dir=pool_dir,
        duckdb_path=None,
        rng=np.random.default_rng(seed),
        outcome_fetch=fetch,
    )


# ===========================================================================
# (a) simulate_pitch invokes the sampler + returns a well-formed PlayResult
# ===========================================================================


def test_simulate_pitch_returns_well_formed_play_result(round_trip_pool):
    pool_dir, _ = round_trip_pool
    sampler = _make_sampler(pool_dir, pitch_outcome="ball")
    sim = PlateAppearanceSimulator(sampler, rng=np.random.default_rng(0))
    state = PitchState(pitcher_id=BIG_PITCHER, bat_hand="L", season=SEASON)

    result = sim.simulate_pitch(state)

    # Shape / key contract.
    for key in (
        "pitch_outcome",
        "is_contact",
        "event",
        "runs",
        "fellback",
        "pitch_sample",
        "battedball_sample",
    ):
        assert key in result
    assert isinstance(result["pitch_outcome"], str)
    assert isinstance(result["is_contact"], bool)
    assert isinstance(result["runs"], float)
    assert isinstance(result["fellback"], bool)
    # The pitch_sample is the genuine sampler payload (proves the sampler was
    # invoked): it must carry a row_id that is in the loaded pitch tile.
    handle = sampler.load_tile(POOL_PITCH, SEASON, "L", pitcher_id=BIG_PITCHER)
    valid_ids = {int(r) for r in handle.rowids}
    assert result["pitch_sample"]["row_id"] in valid_ids
    assert result["pitch_sample"]["tile"] == f"{SEASON}/{BIG_PITCHER}/L"
    sampler.close()


# ===========================================================================
# (b) a non-contact pitch path works
# ===========================================================================


def test_non_contact_path(round_trip_pool):
    pool_dir, _ = round_trip_pool
    sampler = _make_sampler(pool_dir, pitch_outcome="called_strike")
    sim = PlateAppearanceSimulator(sampler, rng=np.random.default_rng(1))
    state = PitchState(pitcher_id=BIG_PITCHER, bat_hand="R", season=SEASON, strikes=1)

    result = sim.simulate_pitch(state)

    assert result["pitch_outcome"] == "called_strike"
    assert result["is_contact"] is False
    # No contact -> no batted-ball sample, and the event is the scaffold's
    # mapped (non-batted-ball) marker.
    assert result["battedball_sample"] is None
    assert result["event"] == pitch_outcome_to_event("called_strike")
    assert result["event"] not in _BB_EVENTS
    sampler.close()


# ===========================================================================
# (c) a contact pitch path triggers sample_batted_ball
# ===========================================================================


def test_contact_path_triggers_batted_ball(round_trip_pool):
    pool_dir, _ = round_trip_pool
    sampler = _make_sampler(pool_dir, pitch_outcome=CONTACT_PITCH_OUTCOME)

    # Spy on sample_batted_ball to prove it is actually called on contact.
    calls = {"n": 0}
    real_sample_bb = sampler.sample_batted_ball

    def spy(*args, **kwargs):
        calls["n"] += 1
        return real_sample_bb(*args, **kwargs)

    sampler.sample_batted_ball = spy  # type: ignore[method-assign]

    sim = PlateAppearanceSimulator(sampler, rng=np.random.default_rng(2))
    state = PitchState(pitcher_id=BIG_PITCHER, bat_hand="L", season=SEASON)

    result = sim.simulate_pitch(state)

    assert result["pitch_outcome"] == CONTACT_PITCH_OUTCOME
    assert result["is_contact"] is True
    assert calls["n"] == 1, "sample_batted_ball must be called exactly once on contact"
    assert result["battedball_sample"] is not None
    # The resolved event is a real batted-ball event from the injected fetch.
    assert result["event"] in _BB_EVENTS
    # The batted-ball sample row must be in the loaded battedball tile.
    bb_handle = sampler.load_tile(POOL_BATTEDBALL, SEASON, "L")
    assert result["battedball_sample"]["row_id"] in {int(r) for r in bb_handle.rowids}
    sampler.close()


# ===========================================================================
# determinism: same seeds -> identical results
# ===========================================================================


def test_deterministic_with_fixed_rng(round_trip_pool):
    pool_dir, _ = round_trip_pool

    def run():
        sampler = _make_sampler(pool_dir, pitch_outcome=CONTACT_PITCH_OUTCOME, seed=7)
        sim = PlateAppearanceSimulator(sampler, rng=np.random.default_rng(7))
        state = PitchState(pitcher_id=BIG_PITCHER, bat_hand="R", season=SEASON)
        out = [sim.simulate_pitch(state) for _ in range(10)]
        sampler.close()
        return [(o["pitch_sample"]["row_id"], o["battedball_sample"]["row_id"]) for o in out]

    assert run() == run()
