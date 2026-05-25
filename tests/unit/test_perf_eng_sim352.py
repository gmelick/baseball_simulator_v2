"""
test_perf_eng_sim352.py
=======================
Unit tests for SIM-352 -- the **production, DB-backed machine factory**
:func:`simulation.production_factory.production_machine_factory` (Phase 5,
Sprint 1).

These run with NO live DuckDB / FAISS: the factory's sampler construction is
behind the injectable :data:`simulation.production_factory._SAMPLER_BUILDER` hook,
so a test installs an in-memory / mock sampler (built via the ``__new__``-bypass
pattern used across tests/unit to dodge the live-DB constructor) and asserts the
factory wires THAT sampler into the returned :class:`StateMachine`.

Coverage (the SIM-352 acceptance criteria):
  * the factory returns a :class:`StateMachine` wired to the injected sampler (and
    the PlateAppearanceSimulator inside it sees the same sampler);
  * the ``factory(seed, spec) -> StateMachine`` signature mirrors the rng factory
    and is dotted-ref-able / picklable by reference;
  * the per-game ``seed`` threads into BOTH the machine loop rng and the sampler
    k-NN rng reproducibly;
  * the factory-only ``_k`` knob sets the machine's k (SIM-377 convention);
  * when ``spec.shared_segments`` is set, the SIM-333 shared tiles are attached
    zero-copy via :meth:`PlayPoolSampler.attach_shared_tile` over the published
    views; with no shared_segments no attach happens (disk-path fall-back);
  * the production default builder constructs a real PlayPoolSampler (no DB touched
    at construction -- the DuckDB connection is lazy).
"""

from __future__ import annotations

import numpy as np
import pytest

import simulation.batch_runner as br
import simulation.production_factory as pf
from simulation.batch_runner import GameSpec
from simulation.play_pool_sampler import POOL_PITCH, PlayPoolSampler
from simulation.production_factory import (
    production_machine_factory,
    set_sampler_builder,
    use_sampler_builder,
)
from simulation.sim_loop import StateMachine

# ---------------------------------------------------------------------------
# A dependency-light mock sampler (no DuckDB / FAISS) -- the __new__ bypass
# ---------------------------------------------------------------------------


class _MockSampler:
    """A stand-in for :class:`PlayPoolSampler` that records attach calls.

    Holds an ``rng`` attribute (so the ``simulate_game`` re-seed seam finds it)
    and an :meth:`attach_shared_tile` that logs its args instead of touching FAISS,
    so we can assert the SIM-333 wiring with no live tiles.
    """

    def __init__(self, rng=None):
        self.rng = rng if rng is not None else np.random.default_rng()
        self.attached: list[dict] = []

    def attach_shared_tile(
        self,
        pool,
        season,
        bat_hand,
        *,
        vectors,
        rowids,
        pitcher_id=None,
        meta=None,
        is_fallback=False,
        label=None,
    ):
        self.attached.append(
            {
                "pool": pool,
                "season": season,
                "bat_hand": bat_hand,
                "pitcher_id": pitcher_id,
                "n_rowids": None if rowids is None else int(np.asarray(rowids).size),
                "has_vectors": vectors is not None,
            }
        )
        return None


def _spec(**extra) -> GameSpec:
    kw = {
        "pitcher_id": 477132,
        "bat_hand": "R",
        "season": 2024,
        "away_lineup": list(range(101, 110)),
        "home_lineup": list(range(201, 210)),
    }
    kw.update(extra)
    return GameSpec(
        machine_factory="simulation.production_factory:production_machine_factory",
        sim_kwargs=kw,
        shared_segments=extra.get("_shared_segments"),
    )


def _spec_with_segments(segments, **extra) -> GameSpec:
    kw = {
        "pitcher_id": 477132,
        "bat_hand": "R",
        "season": 2024,
        "away_lineup": list(range(101, 110)),
        "home_lineup": list(range(201, 210)),
    }
    kw.update(extra)
    return GameSpec(
        machine_factory="simulation.production_factory:production_machine_factory",
        sim_kwargs=kw,
        shared_segments=segments,
    )


@pytest.fixture(autouse=True)
def _restore_builders():
    """Every test gets a clean builder global (restore the production defaults)."""
    yield
    set_sampler_builder(None)
    pf.set_deriver_builder(None)


# ===========================================================================
# The factory returns a StateMachine wired to the injected sampler
# ===========================================================================


class TestWiring:
    def test_returns_statemachine_wired_to_injected_sampler(self):
        mock = _MockSampler()
        with use_sampler_builder(lambda spec, seed: mock):
            machine = production_machine_factory(7, _spec())
        assert isinstance(machine, StateMachine)
        # The machine holds the injected sampler...
        assert machine.sampler is mock
        # ...and the inner PlateAppearanceSimulator sees the SAME sampler.
        assert machine._pa is not None
        assert machine._pa.sampler is mock

    def test_k_knob_sets_machine_k(self):
        mock = _MockSampler()
        with use_sampler_builder(lambda spec, seed: mock):
            machine = production_machine_factory(1, _spec(_k=15))
        assert machine.k == 15

    def test_default_k_is_25(self):
        mock = _MockSampler()
        with use_sampler_builder(lambda spec, seed: mock):
            machine = production_machine_factory(1, _spec())
        assert machine.k == 25

    def test_builder_receives_spec_and_seed(self):
        seen = {}

        def _builder(spec, seed):
            seen["spec"] = spec
            seen["seed"] = seed
            return _MockSampler(rng=np.random.default_rng(seed))

        spec = _spec()
        with use_sampler_builder(_builder):
            production_machine_factory(123, spec)
        assert seen["seed"] == 123
        assert seen["spec"] is spec


# ===========================================================================
# Seed threading -- reproducible loop rng + sampler k-NN rng (§6.3)
# ===========================================================================


class TestSeedThreading:
    def test_machine_loop_rng_seeded_from_seed(self):
        # Two machines built from the SAME seed must draw the SAME loop-rng stream.
        with use_sampler_builder(lambda spec, seed: _MockSampler(rng=np.random.default_rng(seed))):
            m1 = production_machine_factory(99, _spec())
            m2 = production_machine_factory(99, _spec())
        a = m1.rng.random(5)
        b = m2.rng.random(5)
        assert np.array_equal(a, b)

    def test_different_seed_different_stream(self):
        with use_sampler_builder(lambda spec, seed: _MockSampler(rng=np.random.default_rng(seed))):
            m1 = production_machine_factory(1, _spec())
            m2 = production_machine_factory(2, _spec())
        assert not np.array_equal(m1.rng.random(5), m2.rng.random(5))

    def test_default_sampler_builder_seeds_sampler_rng(self):
        # The production default builder seeds the sampler's own k-NN Generator from
        # the per-game seed so FAISS draws are reproducible (no DB touched: the
        # DuckDB connection is lazy, only opened on the first outcome fetch).
        spec = _spec(_pool_dir="/nonexistent", _duckdb_path="/nonexistent.duckdb")
        sampler = pf._default_sampler_builder(spec, seed=55)
        assert isinstance(sampler, PlayPoolSampler)
        ref = np.random.default_rng(55).random(4)
        assert np.array_equal(sampler.rng.random(4), ref)


# ===========================================================================
# SIM-333 shared-tile attach over the published views
# ===========================================================================


class TestSharedTileAttach:
    def test_attaches_shared_tiles_when_segments_set(self):
        # Publish a pitch tile (vectors + rowids) into the worker-global views the
        # factory reads via get_shared_view, then attach through the factory.
        vecs = np.random.default_rng(0).standard_normal((20, 10)).astype(np.float32)
        rowids = (np.arange(20) + 500).astype(np.int64)
        registry, owned = br.publish_shared_arrays(
            {
                "pitch_vectors": vecs,
                "pitch_rowids": rowids,
            }
        )
        try:
            br._worker_init(registry)  # attach the views in-process
            mock = _MockSampler()
            spec = _spec_with_segments(
                {
                    "pitch_vectors": (vecs.shape, "float32"),
                    "pitch_rowids": (rowids.shape, "int64"),
                }
            )
            with use_sampler_builder(lambda s, seed: mock):
                machine = production_machine_factory(7, spec)
            assert machine.sampler is mock
            # The factory attached exactly the pitch tile, over the shared rowids.
            assert len(mock.attached) == 1
            rec = mock.attached[0]
            assert rec["pool"] == POOL_PITCH
            assert rec["season"] == 2024
            assert rec["bat_hand"] == "R"
            assert rec["pitcher_id"] == 477132
            assert rec["n_rowids"] == 20
            assert rec["has_vectors"] is True
        finally:
            for shm in br._WORKER_SHARED["_handles"]:
                shm.close()
            br._worker_init(None)
            br.unlink_shared_segments(owned)

    def test_no_attach_when_no_segments(self):
        mock = _MockSampler()
        with use_sampler_builder(lambda spec, seed: mock):
            production_machine_factory(7, _spec())  # shared_segments is None
        assert mock.attached == []

    def test_attach_real_sampler_zero_copy_rowids(self):
        # End-to-end with a REAL PlayPoolSampler (no DB / no disk tiles): the
        # attached handle's rowids must be the SAME shared buffer (zero-copy).
        pytest.importorskip("faiss")
        vecs = np.random.default_rng(3).standard_normal((30, 10)).astype(np.float32)
        rowids = (np.arange(30) + 900).astype(np.int64)
        registry, owned = br.publish_shared_arrays(
            {
                "pitch_vectors": vecs,
                "pitch_rowids": rowids,
            }
        )
        try:
            br._worker_init(registry)
            shared_rowids_view = br.get_shared_view("pitch_rowids")

            def _real_builder(spec, seed):
                return PlayPoolSampler(
                    pool_dir="/nonexistent",
                    outcome_fetch=lambda pool, ids: dict.fromkeys(ids, "single"),
                    rng=np.random.default_rng(seed),
                )

            spec = _spec_with_segments(
                {
                    "pitch_vectors": (vecs.shape, "float32"),
                    "pitch_rowids": (rowids.shape, "int64"),
                }
            )
            with use_sampler_builder(_real_builder):
                machine = production_machine_factory(7, spec)
            handle = machine.sampler.load_tile(POOL_PITCH, 2024, "R", pitcher_id=477132)
            assert handle.n_vectors == 30
            assert np.shares_memory(handle.rowids, shared_rowids_view)
            machine.sampler.close()
        finally:
            for shm in br._WORKER_SHARED["_handles"]:
                shm.close()
            br._worker_init(None)
            br.unlink_shared_segments(owned)


# ===========================================================================
# Dotted-ref / production-default construction
# ===========================================================================


class TestDottedRefAndDefault:
    def test_dotted_ref_resolves_to_factory(self):
        ref = "simulation.production_factory:production_machine_factory"
        resolved = br._resolve_dotted(ref)
        assert resolved is production_machine_factory

    def test_default_builder_constructs_real_sampler_no_db(self):
        # The production default builder returns a real PlayPoolSampler and touches
        # NO DB at construction (lazy connection) -- safe to build in the sandbox.
        spec = _spec(
            _pool_dir="/data/play_pool",
            _duckdb_path="/data/baseball_sim.duckdb",
            _max_resident_tiles=64,
        )
        sampler = pf._default_sampler_builder(spec, seed=1)
        assert isinstance(sampler, PlayPoolSampler)
        assert sampler.max_resident_tiles == 64

    def test_default_deriver_builder_returns_none(self):
        # Default deriver is None -> the StateMachine's stub fingerprints (no engine
        # stack needed for the factory to build).
        assert pf._default_deriver_builder(_spec()) is None
