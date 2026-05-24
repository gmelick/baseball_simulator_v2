"""
test_qa_sim347.py
=================
SIM-347 -- the **concurrency STRESS test** for the parallel batch runner
(:mod:`simulation.batch_runner`, Phase 4, Sprint 5).  QA/DevOps (Agent 9) +
Performance Engineer (Agent 6).

WHAT THIS STRESSES
------------------
The SIM-332 :class:`~simulation.batch_runner.BatchRunner` fan-out / fan-in across
a real ``ProcessPoolExecutor`` (the SIM-281 D1 path, ``max_workers > 1``), driven
by the picklable, no-DB rng factory (the always-on injection seam) so it needs NO
live DuckDB / FAISS / Redis.  The "30 concurrent games" requirement is met by
pinning ``max_workers = 30`` (the runner caps workers at ``min(override, N)``, so a
30-game batch fans 30 tasks across 30 worker processes — sustained concurrent
load), and "100 simulations" is reached by running ``N_BATCHES`` such 30-game
batches back to back (default 4 -> 120 games >= 100; the slow variant also runs a
single 100-game pooled batch as an alternate interpretation).  Both reach the
spirit: sustained concurrent multi-process load through the runner.

WHAT IT ASSERTS (the SIM-347 acceptance criteria)
-------------------------------------------------
  * **Validity**: every game returns a real :class:`GameSimResult` with a
    ``winner`` consistent with its scores, innings in ``[1, max_innings]``,
    non-negative scores, and self-consistent walk-off / extra-innings flags (see
    :func:`_assert_valid_game` for the no-DB-driver caveat).
  * **Reproducibility under concurrency**: a fixed base seed reproduces the whole
    batch's per-game scores even across worker processes + completion-order race
    (the runner reassembles in seed order).
  * **No race / no worker exception**: every future resolves (the runner raises if
    any worker raised); a pooled run matches the in-process run exactly.
  * **No shared-memory / ``/dev/shm`` leak**: with SIM-333 ``shared_arrays`` set,
    the live-segment count returns to the pre-test baseline after
    :meth:`BatchRunner.close`, even on failure (``finally`` teardown).
  * **RAM** (optional, cheap): peak RSS stays under the SIM-280 2 GB cap when
    ``resource`` is available.

ALWAYS-ON vs SLOW
-----------------
The always-on smoke variant drives a small concurrent batch (a few worker
processes) well under the 45 s bash cap for a fast CI signal.  The full
``100 x 30`` stress run is ``@pytest.mark.slow``.  Per-game work is kept cheap
(short lineups, a capped ``max_innings``) so even the heavy run settles quickly.
"""

from __future__ import annotations

import gc
import glob

import numpy as np
import pytest

from simulation.batch_runner import (
    BatchRunner,
    GameSpec,
    NullCache,
    derive_seed,
)
from simulation.results import GameSimSummary
from simulation.sim_loop import GameSimResult, REGULATION_INNINGS

# The picklable no-DB factory (rng-driven StateMachine, no sampler) + short
# lineups so every game progresses and ENDS without a live DB/FAISS.
FACTORY = "simulation.batch_runner:rng_driven_machine_factory"
AWAY_LINEUP = list(range(101, 110))
HOME_LINEUP = list(range(201, 210))

#: Concurrency width: 30 games run concurrently across 30 worker processes
#: (the runner caps workers at min(override, n_iterations)).
CONCURRENT_GAMES = 30
#: Repeat the 30-game batch this many times to reach >= 100 simulations
#: (4 x 30 = 120 >= 100).  The spirit is sustained concurrent load.
N_BATCHES = 4
#: A hard innings cap so the heavy run cannot spin on a pathological game and
#: stays well inside the sandbox time budget (purely a guard).
MAX_INNINGS = 12

#: SIM-280 RAM cap (bytes): total resident <= 2 GB regardless of worker count.
RAM_CAP_BYTES = 2 * 1024 * 1024 * 1024


def _spec() -> GameSpec:
    """A picklable no-DB GameSpec (short lineups + a capped max_innings)."""
    return GameSpec(
        machine_factory=FACTORY,
        sim_kwargs={
            "away_lineup": AWAY_LINEUP,
            "home_lineup": HOME_LINEUP,
            "season": 2024,
            "pitcher_id": 477132,
            "bat_hand": "R",
            "max_innings": MAX_INNINGS,
        },
    )


def _shm_count() -> int:
    """Best-effort count of live POSIX shared-memory segments (``/dev/shm`` on
    Linux).  Returns 0 where ``/dev/shm`` is absent (so the leak assertion
    degrades to a no-op delta rather than failing on a non-Linux host)."""
    try:
        return len(glob.glob("/dev/shm/*"))
    except OSError:  # pragma: no cover -- defensive
        return 0


def _peak_rss_bytes() -> "int | None":
    """Peak RSS of this process in bytes, or ``None`` if unmeasurable cheaply.

    Uses ``resource.getrusage`` (``ru_maxrss`` is KiB on Linux); returns None on
    a non-POSIX host where ``resource`` is unavailable.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover -- non-POSIX
        return None
    try:
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    except (ValueError, OSError):  # pragma: no cover -- defensive
        return None


def _assert_valid_game(r: GameSimResult, *, max_innings: int = MAX_INNINGS) -> None:
    """Assert one per-game result is structurally valid + self-consistent under
    concurrency (SIM-347 AC #2).

    NOTE on the no-DB driver: the picklable ``rng_driven_machine_factory`` is a
    deterministic scaffold (the SIM-332/333 always-on seam) whose
    ``_CyclingResolver`` rarely advances runners, so a game is mostly a low-/no-run
    affair that settles at the loop's pitch-count guard (``max_innings * 200``
    pitches) rather than at a regulation finish — innings_played is well below 9
    and the score can be 0-0 or a small unequal lead at that arbitrary cutoff.
    That is the documented scaffold behaviour (the existing SIM-332 always-on
    tests likewise never assert winner/innings on this driver), so the validity
    contract here is exactly what is load-bearing for a CONCURRENCY stress test:
    a real ``GameSimResult``, non-negative integer scores, a ``winner`` consistent
    with those scores (the key race-safety check — a torn/corrupted result under
    concurrency would break it), innings in ``[1, max_innings]``, and
    self-consistent walk-off / extra-innings flags.  Regulation-length / always-a-
    winner semantics belong to the production sampler-driven path (not runnable in
    this no-DB sandbox), so they are NOT asserted here — only the flag invariants
    that hold for ANY finish are."""
    from simulation.game_state import Team

    assert isinstance(r, GameSimResult)
    # Non-negative integer scores.
    assert isinstance(r.home_score, int) and isinstance(r.away_score, int)
    assert r.home_score >= 0 and r.away_score >= 0
    # Innings are bounded: at least one inning, never past the cap.
    assert 1 <= r.innings_played <= max_innings
    # The winner property agrees with the scores (the core race-safety check —
    # a corrupted/torn result under concurrency would break this).
    if r.home_score > r.away_score:
        assert r.winner is Team.HOME
    elif r.away_score > r.home_score:
        assert r.winner is Team.AWAY
    else:
        assert r.winner is None  # tie -> no winner
    # Flag self-consistency (these hold for ANY finish path, real or scaffold).
    if r.extra_innings:
        assert r.innings_played > REGULATION_INNINGS
    if r.walk_off:
        # A walk-off is the home team winning in the bottom of regulation-or-later.
        assert r.home_score > r.away_score
        assert r.innings_played >= REGULATION_INNINGS


def _drive_concurrent(
    n_games: int, *, base_seed: int, max_workers: int, shared: bool = False
) -> "tuple[list[GameSimResult], BatchRunner]":
    """Drive ``n_games`` through the runner's REAL pooled executor and return the
    per-game results + the runner (caller closes it).

    Uses :meth:`BatchRunner._execute` so we get the raw per-game
    :class:`GameSimResult`s (the public :meth:`run` aggregates to a summary).
    ``_execute`` runs the SAME ``ProcessPoolExecutor`` fan-out / seed-ordered
    fan-in that :meth:`run` uses when ``max_workers > 1`` (SIM-281 D1).  With
    ``shared=True`` a small SIM-333 ``shared_arrays`` payload is published so the
    leak assertion has a real ``/dev/shm`` segment to track (the no-DB factory
    ignores it; only the create/unlink lifecycle is exercised).
    """
    shared_arrays = (
        {"sim347_payload": np.arange(256, dtype=np.float64)} if shared else None
    )
    runner = BatchRunner(
        max_workers=max_workers, cache=NullCache(), shared_arrays=shared_arrays
    )
    seeds = [derive_seed(base_seed, i) for i in range(n_games)]
    resolved = runner.resolve_max_workers(n_games)
    results = runner._execute(_spec(), seeds, resolved)
    return results, runner


# ===========================================================================
# Always-on smoke: a small CONCURRENT batch through the real pool (fast signal)
# ===========================================================================


class TestConcurrentSmoke:
    def test_small_concurrent_batch_all_valid(self):
        # A handful of games across >1 worker process: exercises the real fork +
        # pickling + seed-ordered reassembly path, fast (well under the 45s cap).
        results, runner = _drive_concurrent(6, base_seed=2024, max_workers=3)
        try:
            assert len(results) == 6
            for r in results:
                _assert_valid_game(r)
        finally:
            runner.close()

    def test_pooled_matches_in_process_under_concurrency(self):
        # Determinism across the process boundary: the concurrent pooled run and
        # the synchronous in-process run give identical per-game scores (no race
        # corrupted ordering or rng isolation).
        seq, seq_runner = _drive_concurrent(6, base_seed=99, max_workers=1)
        par, par_runner = _drive_concurrent(6, base_seed=99, max_workers=4)
        try:
            assert [r.home_score for r in seq] == [r.home_score for r in par]
            assert [r.away_score for r in seq] == [r.away_score for r in par]
            assert [r.innings_played for r in seq] == [r.innings_played for r in par]
        finally:
            seq_runner.close()
            par_runner.close()

    def test_no_shm_leak_after_close_smoke(self):
        # With SIM-333 shared_arrays set, the runner publishes a /dev/shm segment
        # for the pool to attach; after close() the segment count must return to
        # the pre-run baseline (no leak), even though work ran across processes.
        gc.collect()
        before = _shm_count()
        results, runner = _drive_concurrent(
            6, base_seed=7, max_workers=3, shared=True
        )
        try:
            assert len(results) == 6
            for r in results:
                _assert_valid_game(r)
        finally:
            runner.close()  # parent unlinks its owned segment(s)
        gc.collect()
        after = _shm_count()
        assert after <= before, (
            f"/dev/shm leaked: {before} segments before, {after} after close()"
        )

    def test_run_returns_valid_summary_concurrently(self):
        # The PUBLIC run() path under concurrency returns a valid SIM-327 summary
        # with the raw per-iteration arrays intact and non-negative.
        runner = BatchRunner(max_workers=3, cache=NullCache())
        try:
            res = runner.run(_spec(), n_iterations=6, base_seed=2024)
            s = res.summary
            assert isinstance(s, GameSimSummary)
            assert s.n_iterations == 6
            assert (s.home_scores >= 0).all() and (s.away_scores >= 0).all()
            assert abs(s.home_win_pct + s.away_win_pct + s.tie_pct - 1.0) < 1e-9
        finally:
            runner.close()


# ===========================================================================
# The full SIM-347 stress run: 100 simulations x 30 concurrent games  (@slow)
# ===========================================================================


@pytest.mark.slow
class TestConcurrencyStress:
    def test_100_sims_30_concurrent_games_no_leak_no_race(self):
        """100+ simulations as ``N_BATCHES`` x ``CONCURRENT_GAMES``, each batch
        fanning 30 games across 30 worker processes.  Asserts validity, no
        worker exception/race, and no /dev/shm leak across the whole run."""
        gc.collect()
        shm_before = _shm_count()
        total_games = 0
        runners: "list[BatchRunner]" = []
        try:
            for b in range(N_BATCHES):
                # A distinct base seed per batch so each 30-game wave differs, yet
                # the whole run is reproducible from these fixed bases.
                results, runner = _drive_concurrent(
                    CONCURRENT_GAMES,
                    base_seed=10_000 + b * CONCURRENT_GAMES,
                    max_workers=CONCURRENT_GAMES,
                    shared=True,
                )
                runners.append(runner)
                # Every future resolved (the runner raises if any worker raised).
                assert len(results) == CONCURRENT_GAMES
                # The runner pinned 30 workers for 30 games (true concurrency).
                assert runner.resolve_max_workers(CONCURRENT_GAMES) == CONCURRENT_GAMES
                for r in results:
                    _assert_valid_game(r)
                total_games += len(results)
        finally:
            for runner in runners:
                runner.close()  # parent unlinks every owned segment
        gc.collect()
        shm_after = _shm_count()

        assert total_games >= 100, f"only ran {total_games} games (< 100)"
        assert shm_after <= shm_before, (
            f"/dev/shm leaked across the stress run: {shm_before} -> {shm_after}"
        )
        # Optional cheap RAM assertion (SIM-280 <= 2 GB cap).
        peak = _peak_rss_bytes()
        if peak is not None:
            assert peak < RAM_CAP_BYTES, (
                f"peak RSS {peak / 1e9:.2f} GB exceeds the 2 GB SIM-280 cap"
            )

    def test_30_concurrent_batch_is_reproducible(self):
        """Reproducibility under full concurrency: a fixed base seed reproduces a
        30-game wave's per-game scores across the worker-process race."""
        a, ra = _drive_concurrent(
            CONCURRENT_GAMES, base_seed=555, max_workers=CONCURRENT_GAMES
        )
        b, rb = _drive_concurrent(
            CONCURRENT_GAMES, base_seed=555, max_workers=CONCURRENT_GAMES
        )
        try:
            assert [r.home_score for r in a] == [r.home_score for r in b]
            assert [r.away_score for r in a] == [r.away_score for r in b]
            assert [r.innings_played for r in a] == [r.innings_played for r in b]
            # The wave is not all-identical games -- distinct per-game seeds yield
            # distinct games.  (The no-DB scaffold settles low/0-0, so the per-game
            # variation surfaces in innings_played, not necessarily scores.)
            assert len({r.innings_played for r in a}) > 1
        finally:
            ra.close()
            rb.close()

    def test_single_100_game_pooled_batch(self):
        """The alternate interpretation: ONE 100-game batch through the pool (the
        SIM-281 default batch size), capped at the 10-worker ceiling."""
        results, runner = _drive_concurrent(100, base_seed=2024, max_workers=10)
        try:
            assert len(results) == 100
            for r in results:
                _assert_valid_game(r)
        finally:
            runner.close()
