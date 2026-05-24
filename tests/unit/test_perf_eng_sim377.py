"""
test_perf_eng_sim377.py
=======================
Unit tests for SIM-377 -- the ``_hit_rate`` ``TypeError`` fix in the SIM-332 batch
runner (Phase 5, Sprint 1).

THE BUG
-------
:func:`simulation.batch_runner.rng_driven_machine_factory` reads
``spec.sim_kwargs["_hit_rate"]`` to tune its no-DB resolver, but
:func:`simulation.batch_runner._run_one` then splatted the SAME ``sim_kwargs``
dict into ``simulate_game(machine, seed=seed, **spec.sim_kwargs)``.
:func:`simulation.sim_loop.simulate_game` has a FIXED signature (no ``**kwargs``),
so any :class:`GameSpec` carrying ``_hit_rate`` raised ``TypeError:
simulate_game() got an unexpected keyword argument '_hit_rate'``.

THE FIX (the underscore-prefix convention)
-----------------------------------------
``_``-prefixed ``sim_kwargs`` keys are FACTORY-ONLY: ``_run_one`` filters them out
of the ``simulate_game(**...)`` splat (the factory still sees them via the whole
``spec``).  These tests run with NO live DuckDB/FAISS and NO Redis -- the picklable
rng-driven no-DB factory + the in-memory cache fallback, exactly like SIM-332.

Coverage:
  * a spec with ``_hit_rate`` runs end-to-end through ``BatchRunner.run`` with NO
    TypeError (the regression guard);
  * ``_run_one`` directly with ``_hit_rate`` set does not raise;
  * a HIGH ``_hit_rate`` yields more hits/runs than a LOW one (the knob actually
    reaches the factory -- statistical, seeded);
  * non-underscore keys still pass through to ``simulate_game`` unchanged.
"""

from __future__ import annotations

import numpy as np

from simulation.batch_runner import (
    BatchRunner,
    GameSpec,
    InMemoryCache,
    NullCache,
    _run_one,
)

FACTORY = "simulation.batch_runner:rng_driven_machine_factory"
AWAY_LINEUP = list(range(101, 110))
HOME_LINEUP = list(range(201, 210))


def _spec(**extra_kwargs) -> GameSpec:
    kw = {
        "away_lineup": AWAY_LINEUP,
        "home_lineup": HOME_LINEUP,
        "season": 2024,
        "pitcher_id": 477132,
        "bat_hand": "R",
    }
    kw.update(extra_kwargs)
    return GameSpec(machine_factory=FACTORY, sim_kwargs=kw)


def _runner(**kw) -> BatchRunner:
    kw.setdefault("max_workers", 1)  # synchronous in-process (fast + deterministic)
    kw.setdefault("cache", InMemoryCache())
    return BatchRunner(**kw)


# ===========================================================================
# The regression: _hit_rate no longer raises TypeError through the runner
# ===========================================================================


class TestHitRateNoTypeError:
    def test_run_with_hit_rate_does_not_raise(self):
        # The exact shape the ticket calls out: a GameSpec whose sim_kwargs carries
        # the factory-only _hit_rate knob.  Before SIM-377 this raised TypeError.
        spec = _spec(_hit_rate=0.5)
        res = _runner().run(spec, n_iterations=6, base_seed=42)
        assert res.summary.n_iterations == 6

    def test_run_one_directly_with_hit_rate_does_not_raise(self):
        # _run_one is the function the pool maps; it must filter _hit_rate itself.
        spec = _spec(_hit_rate=0.7)
        result = _run_one(spec, seed=11)
        # A real GameSimResult comes back (the game ran to completion).
        assert result.home_score >= 0
        assert result.away_score >= 0
        assert result.total_pitches > 0

    def test_multiple_underscore_keys_all_filtered(self):
        # Any _-prefixed key is factory-only; none may reach simulate_game.
        spec = _spec(_hit_rate=0.4, _unused_knob=123, _another="x")
        # No TypeError despite three underscore keys not in simulate_game's sig.
        result = _run_one(spec, seed=3)
        assert result.total_pitches > 0


# ===========================================================================
# The knob still works: a higher _hit_rate -> more offense (seeded)
# ===========================================================================


class TestHitRateActuallyTunes:
    def test_high_hit_rate_tunes_the_factory_resolver(self):
        # The factory reads _hit_rate from the whole spec (even though _run_one
        # filters it out of the simulate_game splat) and wires it into the no-DB
        # _CyclingResolver.  Assert the knob bites where it is actually consumed:
        # a higher _hit_rate yields more hits from resolve_fielding.  (The no-DB
        # StateMachine's integer run production is driven by its own pitch-outcome
        # rng, so the knob's effect is verified at the resolver it tunes, not on
        # downstream runs.)
        from simulation.batch_runner import rng_driven_machine_factory

        def hits_over_n(hit_rate: float, n: int = 400) -> int:
            machine = rng_driven_machine_factory(2024, _spec(_hit_rate=hit_rate))
            return sum(
                machine.resolver.resolve_fielding(None, None).result_hits
                for _ in range(n)
            )

        low_hits = hits_over_n(0.05)
        high_hits = hits_over_n(0.95)
        assert high_hits > low_hits, (
            f"high _hit_rate produced {high_hits} hits, low produced {low_hits}; "
            "the knob is not reaching / tuning the factory's resolver"
        )

    def test_hit_rate_default_when_absent(self):
        # No _hit_rate key at all -> the factory's 0.30 default; still runs cleanly.
        res = _runner().run(_spec(), n_iterations=6, base_seed=7)
        assert res.summary.n_iterations == 6


# ===========================================================================
# Passthrough: non-underscore keys still reach simulate_game unchanged
# ===========================================================================


class TestPassthroughPreserved:
    def test_non_underscore_kwargs_reach_simulate_game(self):
        # season / pitcher_id / bat_hand / lineups are real simulate_game params and
        # must still flow through (mixed with the filtered _hit_rate).
        spec = _spec(_hit_rate=0.5, k=15, max_innings=9)
        result = _run_one(spec, seed=99)
        # The game used the wired season + ran to a legal length.
        assert result.final_state.season == 2024
        assert result.innings_played >= 1

    def test_determinism_holds_with_hit_rate(self):
        # The fix must not perturb determinism: same base seed + _hit_rate -> same
        # per-iteration scores.
        spec = _spec(_hit_rate=0.6)
        a = _runner(cache=NullCache()).run(
            spec, n_iterations=8, base_seed=321, use_cache=False
        )
        b = _runner(cache=NullCache()).run(
            spec, n_iterations=8, base_seed=321, use_cache=False
        )
        assert np.array_equal(a.summary.home_scores, b.summary.home_scores)
        assert np.array_equal(a.summary.away_scores, b.summary.away_scores)
