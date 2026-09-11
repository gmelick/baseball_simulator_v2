"""
SIM-535 — the play pools must not offer plays that had not happened yet.

SIM-534 made the batter PROFILES point-in-time. The pools were the larger half
of the same defect: they hold four seasons and the sampler draws from all of
them, so simulating a game played in April 2025 could return a play from that
September. Nothing errors, and the simulation is quietly better informed than
the day it is standing on.

The cutoff is applied at draw time rather than at build time. A backtest walking
a season would otherwise need one bundle rebuild per date; this needs none.
"""

from __future__ import annotations

import numpy as np
import pytest

from simulation.synthetic_bundle import synthetic_sampler


def _pool(sampler):
    return sampler.a.pools["R"]


# ---------------------------------------------------------------------------
# No cutoff means no change
# ---------------------------------------------------------------------------


def test_the_live_path_is_untouched_by_default() -> None:
    """A live simulation draws from everything, and must not pay for the
    cutoff it is not using."""
    s = synthetic_sampler()
    assert s.asof_ymd is None
    assert s._asof_mask(_pool(s)) is None
    w = np.ones(_pool(s).n, dtype=np.float32)
    assert s._apply_asof(w, _pool(s)) is w  # the same object, not a copy


def test_clearing_the_cutoff_restores_the_whole_pool() -> None:
    s = synthetic_sampler()
    s.set_asof(20240601)
    assert s._asof_mask(_pool(s)) is not None
    s.set_asof(None)
    assert s._asof_mask(_pool(s)) is None


# ---------------------------------------------------------------------------
# The mask itself
# ---------------------------------------------------------------------------


def test_the_mask_keeps_the_past_and_drops_the_future() -> None:
    s = synthetic_sampler()
    pool = _pool(s)
    cutoff = int(np.median(pool.game_ymd))
    s.set_asof(cutoff)
    mask = s._asof_mask(pool)
    assert mask is not None
    expected = (pool.game_ymd <= cutoff).astype(np.float32)
    assert np.array_equal(mask, expected)
    assert mask.sum() > 0, "the fixture must leave something on both sides"
    assert mask.sum() < pool.n


def test_a_masked_weight_is_zero_for_every_future_row() -> None:
    s = synthetic_sampler()
    pool = _pool(s)
    cutoff = int(np.median(pool.game_ymd))
    s.set_asof(cutoff)
    w = s._apply_asof(np.ones(pool.n, dtype=np.float32), pool)
    future = pool.game_ymd > cutoff
    assert future.any()
    assert not w[future].any(), "a play from after the cutoff kept weight"
    assert (w[~future] == 1.0).all(), "a play from before the cutoff lost weight"


def test_a_row_played_exactly_on_the_cutoff_is_admissible() -> None:
    """The cutoff is the date being simulated, and games earlier that day have
    happened. An off-by-one here silently discards a day of data."""
    s = synthetic_sampler()
    pool = _pool(s)
    cutoff = int(pool.game_ymd.min())
    s.set_asof(cutoff)
    mask = s._asof_mask(pool)
    assert mask[pool.game_ymd == cutoff].all()


def test_the_mask_is_computed_once_per_pool() -> None:
    s = synthetic_sampler()
    pool = _pool(s)
    s.set_asof(20250601)
    first = s._asof_mask(pool)
    assert s._asof_mask(pool) is first  # cached, not recomputed per draw


def test_moving_the_cutoff_invalidates_the_cache() -> None:
    s = synthetic_sampler()
    pool = _pool(s)
    s.set_asof(20250601)
    first = s._asof_mask(pool)
    s.set_asof(20250901)
    second = s._asof_mask(pool)
    assert second is not first
    assert second.sum() >= first.sum()


# ---------------------------------------------------------------------------
# A bundle that cannot be filtered must say so
# ---------------------------------------------------------------------------


def test_a_bundle_without_dates_refuses_a_cutoff() -> None:
    """A pre-SIM-535 bundle carries no dates. Drawing from it under a cutoff
    would be exactly the silent leak this guards against, so it raises."""
    s = synthetic_sampler()
    pool = _pool(s)
    pool.game_ymd = None
    s.set_asof(20240601)
    with pytest.raises(RuntimeError, match="no game dates"):
        s._asof_mask(pool)


def test_a_bundle_without_dates_is_fine_with_no_cutoff() -> None:
    s = synthetic_sampler()
    pool = _pool(s)
    pool.game_ymd = None
    assert s._asof_mask(pool) is None


# ---------------------------------------------------------------------------
# Every draw carries it
# ---------------------------------------------------------------------------

#: The four draws that read a dated pool. If a fifth is added without the
#: cutoff, a backtest silently sees the future through it.
_DRAW_SITES = 5  # pitch base, batted ball, manager change, steal, advancement


def test_every_draw_applies_the_cutoff() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "simulation" / "full_pool_sampler.py").read_text(
        encoding="utf-8"
    )
    assert src.count("self._apply_asof(") == _DRAW_SITES, (
        "a draw was added or removed without updating the cutoff wiring"
    )


def test_the_pitch_draw_inherits_it_from_the_half_inning_base() -> None:
    """Both pitch paths — the cell index and the plain bucket draw — build on
    the same base, so folding the cutoff there covers both."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "simulation" / "full_pool_sampler.py").read_text(
        encoding="utf-8"
    )
    assert "self._base = self._apply_asof(" in src


def test_a_drawn_pitch_never_postdates_the_cutoff() -> None:
    """The end-to-end check: draw many pitches under a cutoff and confirm every
    one came from a play that had already happened."""
    s = synthetic_sampler(seed=7)
    pool = _pool(s)
    cutoff = int(np.median(pool.game_ymd))
    s.set_asof(cutoff)
    s.new_half_inning("R", "1:2024")
    base = s._base
    assert base is not None
    drawn = np.nonzero(base > 0)[0]
    assert drawn.size, "the cutoff left no admissible rows — widen the fixture"
    assert (pool.game_ymd[drawn] <= cutoff).all()
    assert (base[pool.game_ymd > cutoff] == 0).all()


# ---------------------------------------------------------------------------
# The cutoff reaches the sampler from the game being simulated
# ---------------------------------------------------------------------------


class _FakePool:
    """A connection pool with one game on a known date."""

    def __init__(self, game_date):
        self._game_date = game_date

    async def fetchrow(self, _sql, _game_pk):
        return {"game_date": self._game_date} if self._game_date is not None else None


@pytest.mark.asyncio
async def test_the_cutoff_is_the_day_BEFORE_the_game() -> None:
    """The pools hold every play of every game in the window, this one
    included. A cutoff on the game's own date would let the simulator copy the
    very plays it is predicting."""
    import datetime as dt

    from simulation.sim_kwargs import resolve_asof_ymd

    got = await resolve_asof_ymd(_FakePool(dt.date(2025, 4, 15)), 777001)
    assert got == 20250414


@pytest.mark.asyncio
async def test_an_unknown_game_yields_no_cutoff() -> None:
    """No cutoff means "draw from everything", which is right for a live game
    and is what happened before any of this existed."""
    from simulation.sim_kwargs import resolve_asof_ymd

    assert await resolve_asof_ymd(_FakePool(None), 777001) is None
    assert await resolve_asof_ymd(None, 777001) is None


def test_the_factory_reads_the_cutoff_as_a_factory_only_key() -> None:
    """A leading underscore marks a key the machine factory consumes and
    ``simulate_game`` never sees (the SIM-377 convention)."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "simulation" / "production_factory.py").read_text(
        encoding="utf-8"
    )
    assert '"_asof_ymd"' in src
    assert "full_pool.set_asof(" in src


def test_build_sim_kwargs_passes_the_cutoff_through() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "simulation" / "sim_kwargs.py").read_text(
        encoding="utf-8"
    )
    assert 'kwargs["_asof_ymd"] = asof' in src
    assert "resolve_asof_ymd" in src
