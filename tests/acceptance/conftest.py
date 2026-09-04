"""SIM-450 — the fixtures that run the simulator in the PRODUCTION configuration.

WHY THIS FILE EXISTS
====================
``tests/conftest.py`` writes six environment flags OFF at IMPORT time (lines 33,
39, 45, 51, 52 and 53). Production sets the exact inverse. Every test in this
repo therefore drives a different simulator from the one that serves users, and
the four methods that shape every production pitch — ``_full_pool_outcome``,
``_full_pool_fielding``, ``_full_pool_out_advancement`` and the steal decision
(``_steal_opportunity_draw`` since SIM-474; previously the unreachable
``_full_pool_steal_decision``) — had zero test references anywhere. That is how
four confirmed production defects survived eight weeks.

This conftest is the first real opt-in. It turns the flags back ON for this
package only.

THE OVERRIDE SEAM, AND WHY IT IS A PACKAGE-SCOPED FIXTURE
=========================================================
Two simpler mechanisms are wrong, and both fail silently:

* Writing ``os.environ`` at the top of this file overrides the root conftest, but
  ``tests/acceptance`` sorts BEFORE ``tests/unit``, so the write poisons every
  later unit test in the same process.
* A SESSION-scoped fixture undoes its writes at the end of the session, which is
  also after ``tests/unit`` has run. Same leak.

A PACKAGE-scoped fixture is set up before any test in this package and torn down
when collection leaves the package, which is before ``tests/unit`` starts. The
scope also has to be package (not function), because the fixture that runs the
simulations is itself package-scoped, and pytest builds wider-scoped fixtures
first — a function-scoped env fixture would set the flags AFTER the simulations
had already run with them off.

The simulator reads every flag at CALL time, never at import time
(``sim_loop.py:968-978`` in ``StateMachine.__init__``, ``sim_loop.py:2115`` for
the home-field bias, ``production_factory.py:331`` and ``:473`` in the factory).
Setting the environment before the machine is built is therefore effective.

The fixture asserts the values it just wrote. If a future change to
``tests/conftest.py`` re-pins a flag in a way this seam cannot beat, the lane
fails loudly instead of quietly measuring the wrong simulator.

WHAT CHANGED ON 2026-08-10 (SIM-450 remediation round 3)
========================================================
Three things, all forced by a review of the band arithmetic:

* **The game order.** ``ACCEPTANCE_GAME_PKS`` was ASCENDING by park run factor
  and the fixture slices ``[:n_games]``, so an 8-game run took the eight most
  pitcher-friendly parks — mean 0.96844 against 0.99952 for the full twelve. It
  now IS ``bands.BALANCED_GAME_ORDER``, so every prefix of four or more games
  measures the same run environment as the whole set.
* **The run size.** The round-3 floors are 3x to 7x tighter, so the certifying
  run is 5,100 game-sims rather than 1,200. ``DEFAULT_ITERS`` moved 100 -> 425
  and ``MIN_MEANINGFUL_SIMS`` is now read from ``bands`` instead of hard-coded.
* **A second reach-on-error channel.** ``BACKLOG.md:20`` (SIM-496) records that
  the drawn-ROE probe below measures the pool BEFORE the loop discards the play,
  so no floor on it can ever red on that defect. ``ROE_reached`` counts batters
  who actually reached, at the single commit point.

Owner: QA / DevOps (SIM-450).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from tests.acceptance import bands

# ---------------------------------------------------------------------------
# The production configuration
# ---------------------------------------------------------------------------

#: The exact inverse of the flags ``tests/conftest.py`` pins off — the lane
#: states the whole production configuration so a reader of the log sees it.
PRODUCTION_FLAGS: dict[str, str] = {
    "SIM_FULL_POOL": "1",  # SIM-429 full-pool similarity sampler
    "SIM_MANAGER": "1",  # SIM-434 manager pull / reliever decisions
    "SIM_BB_PLATOON": "1",  # SIM-413 batted-ball platoon
    # SIM-476 (2026-08-30): the SIM-411/412/425b post-draw flips are DELETED;
    # production now runs the FITTED SIM-491 draw-weight kernels instead.
    "SIM_PARK_KERNEL_SIGMA": "0.02",  # SIM-476 fitted park kernel
    "SIM_FIELDER_KERNEL_SIGMA": "0.5",  # SIM-476 fitted fielder kernel
    # SIM-476 owner ruling 2026-08-30: w=0 — the batted-ball draw
    # hard-conditions on the batting side (the SIM-412 replacement).
    "SIM_HOME_OFF_WEIGHT": "0.0",
    # SIM-517 (2026-09-04): the fitted anisotropic receiving kernel (the
    # SIM-428 framing flip is DELETED) + the drawn row's got-away resolution.
    "SIM_CATCHER_FRAMING_SIGMA": "0.25",
    "SIM_CATCHER_BLOCK_SIGMA": "0.05",
    "SIM_GOT_AWAY": "1",
}

#: Env names the lane must ERASE (not set) so a default code path proves
#: itself. Empty since SIM-476 deleted the SIM-412 flip this once guarded;
#: the machinery stays for the next default-path flag.
DELETED_FLAGS: tuple[str, ...] = ()

#: Twelve 2024 regular-season Final games, one per venue, spread evenly across
#: the 2024 park-factor distribution. Verified against the live database on
#: 2026-08-10: every game is season=2024, game_type='R', status='Final' with 20
#: ingested lineup slots, and every venue has a ``derived.park_factors``
#: ``factor_type='R'`` row for 2024. The set's mean park run factor is 0.9995
#: against a league mean of 1.0014, so the set does not bias the run environment.
#:
#: THE ORDER IS LOAD-BEARING. The fixture slices ``[:n_games]``, so a shortened
#: run measures a PREFIX. ``bands.BALANCED_GAME_ORDER`` pairs extremes — widest
#: pair first, each pair low-then-high — which holds every prefix of four or more
#: games inside ``bands.MAX_PREFIX_PARK_BIAS`` of the full-set mean. Do not
#: re-sort this tuple; ``test_conftest_slices_the_balanced_game_order_sim450``
#: fails if you do.
#:
#: ⚠ SIM-497 — THIS SAMPLE IS BIASED, NOT MERELY SMALL. READ BEFORE TRUSTING A RESULT.
#: These are TWELVE MATCHUPS: twelve specific starting pitchers, twelve specific
#: lineups, twelve specific parks. Simulating them ten thousand times converges on
#: the true answer FOR THOSE TWELVE. That number is not the league mean unless the
#: twelve happen to be league-average, and nothing makes them so. **Iterations cut
#: noise. They do nothing about this.** A long run therefore buys PRECISION ON A
#: BIASED ESTIMATE — a more stable wrong number.
#:
#: The park-balance machinery above is the confession, not the cure: you only
#: hand-balance a sample you already know is too small to be representative.
#:
#: SIM-497 replaces this fixture with a DATE-RANGE backtest over every game in a
#: period (~2,430 games for 2024, ~1.5 h per pass, all 30 parks, no selection to
#: defend), scored against BOTH each game's actual outcome AND the league averages.
#: Until SIM-497a lands, treat any verdict from this lane as indicative only.
ACCEPTANCE_GAME_PKS: tuple[int, ...] = bands.BALANCED_GAME_ORDER

#: The certifying size. ``bands.binding_requirement`` says the twelve box
#: channels need 5,100 game-sims under the round-3 floors, so 12 games x 425
#: iterations = 5,100 game-sims = 10,200 team-games. Measured 2.25 s per sim on
#: 2026-08-10 (901.6 s for 400 sims), so this run takes about 3.2 hours serial.
#:
#: ``home_win_pct`` is NOT covered by it. That channel takes one observation per
#: decisive game rather than two per game-sim, and its floor is sized on the
#: ``CLAUDE.md:400`` baseline, so it needs 26,015 decisive games — about 16.3
#: hours. Read ``bands.py`` "THE COST, STATED PLAINLY" before you shorten a run.
DEFAULT_GAMES = 12
DEFAULT_ITERS = 425

#: The lowest size that still says anything, read from the band arithmetic rather
#: than hard-coded. It was a hand-written 400 until 2026-08-10, which the round-3
#: floors overtook by more than 12x. Below this every channel returns
#: UNDERPOWERED and the lane certifies nothing.
MIN_MEANINGFUL_SIMS = bands.binding_requirement(bands.BOX_CHANNELS)[1]

_FACTORY = "simulation.production_factory:production_machine_factory"

_OFF_VALUES = frozenset({"", "0", "false", "no", "off"})


def opted_in() -> bool:
    """Whether the operator asked for the heavy lane via ``SIM_ACCEPTANCE=1``.

    The lane needs the engine-artifact bundle, a 3.2 GB DuckDB file and a
    populated Postgres. ``make test`` runs ``pytest tests/``, so the heavy module
    guards itself on this and skips everywhere else.
    """
    return os.environ.get("SIM_ACCEPTANCE", "").strip().lower() not in _OFF_VALUES


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``acceptance`` marker for this package only (SIM-450).

    ``pyproject.toml`` sets ``--strict-markers``, so the marker has to be
    registered somewhere. It is registered HERE rather than in ``pyproject.toml``
    because that file is shared with every other lane and a one-line edit there
    conflicts with concurrent work. ``pytest_configure`` is a historic hook, so a
    conftest registered during collection still runs it.
    """
    config.addinivalue_line(
        "markers",
        "acceptance: SIM-450 statistical acceptance-band lane. Runs the PRODUCTION "
        "flag configuration against real MLB rates. Needs the engine-artifact bundle, "
        "DuckDB and Postgres, and opts in via SIM_ACCEPTANCE=1.",
    )


# ---------------------------------------------------------------------------
# The production-flag seam
# ---------------------------------------------------------------------------


@pytest.fixture(scope="package", autouse=True)
def production_flags() -> Any:
    """Set the production flags for this package, then put them back (SIM-450).

    A no-op when the operator did not opt in, so ``make test`` collects the cheap
    arithmetic tests in this package without ever touching the environment.

    Yields the flag map it applied, so a test can print the configuration it
    actually ran under.
    """
    if not opted_in():
        yield dict(PRODUCTION_FLAGS)
        return

    mp = pytest.MonkeyPatch()
    try:
        for name, value in PRODUCTION_FLAGS.items():
            mp.setenv(name, value)
        for name in DELETED_FLAGS:
            mp.delenv(name, raising=False)

        # Assert the seam actually beat tests/conftest.py. A silent failure here
        # would measure the per-tile simulator and label the result "production".
        for name, value in PRODUCTION_FLAGS.items():
            actual = os.environ.get(name)
            if actual != value:
                raise AssertionError(
                    f"SIM-450 override failed: {name}={actual!r}, expected {value!r}. "
                    "Something re-pinned the flag after this fixture ran. The lane "
                    "refuses to measure a simulator it cannot name."
                )
        for name in DELETED_FLAGS:
            if name in os.environ:
                raise AssertionError(
                    f"SIM-450 override failed: {name} is still set to "
                    f"{os.environ[name]!r}; production leaves it unset."
                )
        yield dict(PRODUCTION_FLAGS)
    finally:
        mp.undo()


# ---------------------------------------------------------------------------
# Preconditions — loud, never silent
# ---------------------------------------------------------------------------


def _artifact_bundle_paths() -> list[str]:
    pool_dir = os.environ.get("BASEBALL_PLAY_POOL_DIR", "/data/play_pool")
    return [
        os.path.join(pool_dir, "engine_artifacts", "pitch_pool", "manifest.json"),
        os.path.join(pool_dir, "engine_artifacts", "battedball_pool", "manifest.json"),
        os.path.join(pool_dir, "engine_artifacts", "pitcher_sim.npz"),
    ]


def _duckdb_path() -> str:
    return os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")


def _dsn() -> str:
    return os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )


@pytest.fixture(scope="package")
def preconditions(production_flags: dict[str, str]) -> None:
    """Check the data the lane needs and FAIL when any of it is missing (SIM-450).

    This fails; it does not skip. The operator already said ``SIM_ACCEPTANCE=1``,
    which means "run the lane". An explicit request that silently turns into a
    skip is the green-tick-that-means-nothing failure this ticket exists to end.
    """
    missing: list[str] = []

    duck_path = _duckdb_path()
    if not os.path.isfile(duck_path):
        missing.append(f"DuckDB file {duck_path}")

    for path in _artifact_bundle_paths():
        if not os.path.isfile(path):
            missing.append(f"engine artifact {path}")

    try:
        import asyncio

        import asyncpg

        async def _probe() -> None:
            conn = await asyncpg.connect(_dsn(), timeout=30)
            try:
                await conn.fetchval("SELECT 1")
            finally:
                await conn.close()

        asyncio.run(_probe())
    except Exception as exc:  # noqa: BLE001 -- report ANY connect failure verbatim
        missing.append(f"Postgres at {_dsn().rsplit('@', 1)[-1]} ({exc!r})")

    if missing:
        pytest.fail(
            "SIM-450 acceptance lane cannot run. SIM_ACCEPTANCE=1 asked for it, so "
            "this is a FAILURE, not a skip.\n  missing:\n    - "
            + "\n    - ".join(missing)
            + "\n  Run it on the host that owns the /data volume, or restore the data.",
            pytrace=False,
        )


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class AcceptanceRun:
    """Everything one acceptance run measured (SIM-450).

    ``observations`` holds one value per team-game for each box channel, and one
    0/1 per DECISIVE game for ``home_win_pct``. The band arithmetic takes the raw
    observations, never a pre-computed mean, so the standard error comes from the
    run itself.
    """

    n_games: int
    n_iters: int
    elapsed_s: float
    observations: dict[str, list[float]] = field(default_factory=dict)
    #: Call counts for the four production methods that had zero test references.
    calls: dict[str, int] = field(default_factory=dict)
    #: Per-game park run factor and defense-map sizes — the SIM-449 wiring guard.
    park_factors: dict[int, float] = field(default_factory=dict)
    defense_sizes: dict[int, tuple[int, int]] = field(default_factory=dict)
    #: Stolen bases and caught-stealings the boxscore reported but no lineup
    #: claimed. Non-zero means the SB/CS attribution is wrong.
    unattributed_sb: int = 0
    unattributed_cs: int = 0
    ties: int = 0
    #: SIM-516: whole-lane numerators/denominators for the POOL-REFERENCED
    #: frequency bands (bands.POOL_REFERENCES). Counted by the probes:
    #: PA / BB_pitched / IBB / HBP / K_pa (per-PA terminals), BIP / DP_ROW /
    #: DP_OPP_DEN (per fielding resolution). Pitches come from
    #: ``calls["_full_pool_outcome"]``.
    pool_counts: dict[str, int] = field(default_factory=dict)

    @property
    def total_sims(self) -> int:
        return self.n_games * self.n_iters


#: The channels ``_install_probes`` tallies per game-sim. ``R``, ``SB``, ``CS``
#: and ``home_win_pct`` come from the result object instead.
TALLY_CHANNELS: tuple[str, ...] = (
    "H",
    "HR",
    "2B",
    "3B",
    "BB",
    "K",
    "DP",
    "ROE",
    "ROE_reached",
)


def _blank_tally() -> dict[str, list[int]]:
    """Per-game-sim counters, indexed by ``Team`` (0 = AWAY, 1 = HOME)."""
    return {name: [0, 0] for name in TALLY_CHANNELS}


def _install_probes(
    machine: Any,
    tally: dict[str, list[int]],
    calls: dict[str, int],
    pool_counts: dict[str, int] | None = None,
) -> None:
    """Wrap the four production methods on a built machine (SIM-450).

    This is the instrumentation that finally gives those four methods a test
    reference. It is also the ONLY way to count double plays and reaches on
    error: ``BoxScore`` / ``PlayerStatLine`` (``sim_loop.py:3437-3510``) carry no
    DP or ROE field.

    A fifth wrapper on ``_accumulate_pa`` counts the batting side's H / HR / 2B /
    3B / BB / K. The boxscore's ``k`` and ``bb`` are PITCHING stats, so summing
    them per team gives the strikeouts a team THREW, not the ones it took. The
    wrapper reads ``state.offense`` before the plate appearance is committed, so
    every channel is attributed to the team that batted.

    A sixth wrapper on ``_commit_run_delta`` counts ``ROE_reached`` (SIM-496).
    ``BACKLOG.md:20`` requires it: the ``ROE`` counter above reads the DRAWN
    event at the ``_full_pool_fielding`` boundary, which is before the loop
    decides what the play was, so a green ROE band proves only that the pool
    supplies errors at about the right rate. ``_commit_run_delta`` is the
    single place the loop turns a play into a base-out delta, and every
    reach-on-error site passes through it. A batter reached on an error when
    that commit carries the ``field_error`` event AND ``batter_reached``.

    ⚠ PROBE HISTORY (2026-08-19): the condition used to require
    ``result_hits >= 1`` — written for the ALIAS world, where field_error
    masqueraded as a single. The SIM-511 landing made ``field_error`` its own
    canonical outcome: a correct commit carries ``result_hits = 0`` (not a
    hit) with ``batter_reached=True``, so the old condition read 0.0000 on a
    lane where batters genuinely reached. The probe now reads the
    ``batter_reached`` transition fact — the loop's own body ledger.

    A seventh wrapper counts calls to ``_resolve_in_play_transition`` — the
    SIM-511 production resolution path (the legacy
    ``_full_pool_out_advancement`` is bypassed on a transition bundle and its
    call count reads 0 by design).

    The wrapper pattern matches ``scripts/diag_dp.py:64-78``.
    """
    from simulation.constants import resolve_event_to_canonical
    from simulation.sim_loop import _DOUBLE_PLAY_EVENTS

    # SIM-516: the pool-band counters (lane-global, never reset per game).
    pc = pool_counts if pool_counts is not None else {}
    for key in (
        "PA",
        "BB_pitched",
        "IBB",
        "HBP",
        "K_pa",
        "BIP",
        "DP_ROW",
        "DP_OPP_DEN",
        # SIM-476: steal opportunities / attempts / safe, per target base.
        "STEAL_OPP_2",
        "STEAL_ATT_2",
        "STEAL_SAFE_2",
        "STEAL_OPP_3",
        "STEAL_ATT_3",
        "STEAL_SAFE_3",
        # SIM-517: the receiving channels — taken pitches, called strikes,
        # and drawn got-away rows (the pitch seam counts them below).
        "TAKEN",
        "CALLED",
        "GOT_AWAY",
    ):
        pc.setdefault(key, 0)

    # SIM-476: count steals at the SAMPLER seam, where the drawn row's own
    # `attempted`/`success` flags live — the pool's exact semantics. The
    # sampler is CACHED per process while machines are per-game, so this
    # wrapper installs ONCE (the sentinel) and closes over the lane-global
    # counters; wrapping per game would stack k-fold (the recorded SIM-514
    # instrument trap).
    fp = getattr(machine, "full_pool_sampler", None)
    if fp is not None and not getattr(fp.steal_draw, "_sim450_wrapped", False):
        orig_steal_draw = fp.steal_draw

        def steal_draw(target_base: Any, *a: Any, **kw: Any) -> Any:
            res = orig_steal_draw(target_base, *a, **kw)
            if res is not None:
                t = int(target_base)
                pc[f"STEAL_OPP_{t}"] += 1
                if bool(res[0]):
                    pc[f"STEAL_ATT_{t}"] += 1
                    if bool(res[1]):
                        pc[f"STEAL_SAFE_{t}"] += 1
            return res

        steal_draw._sim450_wrapped = True  # type: ignore[attr-defined]
        fp.steal_draw = steal_draw

    orig_outcome = machine._full_pool_outcome
    orig_fielding = machine._full_pool_fielding
    orig_advancement = machine._full_pool_out_advancement
    orig_steal = machine._steal_opportunity_draw
    orig_accumulate = machine._accumulate_pa
    orig_commit = machine._commit_run_delta
    orig_transition = machine._resolve_in_play_transition

    def outcome(state: Any, _o: Any = orig_outcome) -> Any:
        calls["_full_pool_outcome"] += 1
        out = _o(state)
        # SIM-517: the receiving channels, read at the pitch seam. The
        # got-away fact comes from the SAMPLER's accessor (the drawn row's
        # own flag) so the count works whether or not SIM_GOT_AWAY resolves
        # its consequences.
        if out in ("ball", "called_strike"):
            pc["TAKEN"] += 1
            if out == "called_strike":
                pc["CALLED"] += 1
        if fp is not None and fp.last_pitch_got_away():
            pc["GOT_AWAY"] += 1
        return out

    def fielding(state: Any, _o: Any = orig_fielding) -> Any:
        calls["_full_pool_fielding"] += 1
        side = int(state.offense)
        # SIM-516: the DP-opportunity denominator reads the PRE-resolution
        # state — a ball in play with a runner on 1B and fewer than two outs.
        dp_opportunity = state.bases.first is not None and int(state.outs) < 2
        sig = _o(state)
        if sig is not None:
            pc["BIP"] += 1
            if dp_opportunity:
                pc["DP_OPP_DEN"] += 1
                # The pool-aligned DP numerator: the drawn transition row
                # retired BOTH the runner on first and the batter — the same
                # definition the census measured the pool centre with.
                tr = getattr(sig, "transition", None)
                if tr is not None and tr.get("r1") == 0 and tr.get("batter") == 0:
                    pc["DP_ROW"] += 1
            # A DP is counted only when a second out was actually recorded. The
            # SIM-429 phantom-DP fix relabels a runner-less DP draw to a plain
            # field_out, so the drawn event alone would over-count.
            if sig.event in _DOUBLE_PLAY_EVENTS and int(sig.result_outs) >= 2:
                tally["DP"][side] += 1
            if bool(sig.is_error) or sig.event == "field_error":
                tally["ROE"][side] += 1
        return sig

    def advancement(state: Any, result: Any, sig: Any, _o: Any = orig_advancement) -> Any:
        calls["_full_pool_out_advancement"] += 1
        return _o(state, result, sig)

    def transition(
        state: Any, result: Any, sig: Any, pre_outs: Any, pre_bases: Any, _o: Any = orig_transition
    ) -> Any:
        calls["_resolve_in_play_transition"] += 1
        return _o(state, result, sig, pre_outs, pre_bases)

    def steal(state: Any, _o: Any = orig_steal) -> Any:
        calls["_steal_opportunity_draw"] += 1
        return _o(state)

    def accumulate(state: Any, result: Any, _o: Any = orig_accumulate) -> Any:
        side = int(state.offense)
        canonical = result.canonical_event or resolve_event_to_canonical(result.event)
        # SIM-516: every accumulated PA terminal feeds the pool-band
        # denominators; walks split pitched-vs-intentional because their pool
        # references differ (the count-machine chain vs sim.ibb_rates).
        pc["PA"] += 1
        if canonical in ("single", "double", "triple", "home_run"):
            tally["H"][side] += 1
            if canonical == "home_run":
                tally["HR"][side] += 1
            elif canonical == "double":
                tally["2B"][side] += 1
            elif canonical == "triple":
                tally["3B"][side] += 1
        elif canonical in ("walk", "intentional_walk"):
            tally["BB"][side] += 1
            pc["BB_pitched" if canonical == "walk" else "IBB"] += 1
        elif canonical == "strikeout":
            tally["K"][side] += 1
            pc["K_pa"] += 1
        elif canonical == "hit_by_pitch":
            pc["HBP"] += 1
        return _o(state, result)

    def commit(
        state: Any,
        result: Any,
        *,
        event: str | None,
        result_hits: int,
        result_outs: int,
        result_runs: int,
        _o: Any = orig_commit,
        **kwargs: Any,
    ) -> Any:
        # **kwargs passes through whatever the loop adds to the ledger call —
        # SIM-499 added pre_outs/pre_bases/batter_reached/runners_scored/
        # runners_retired after this probe was written, and pinning the old
        # four kwargs broke the whole lane with a TypeError at setup. The
        # probe only READS the four it tallies; it must never re-state the
        # loop's signature.
        calls["_commit_run_delta"] += 1
        # SIM-511: field_error is its own canonical outcome — a correct commit
        # carries result_hits = 0 (a reach is NOT a hit) with batter_reached
        # truth. The old `result_hits >= 1` condition was the alias world's
        # and read 0.0000 on a lane where batters genuinely reached.
        if event == "field_error" and bool(kwargs.get("batter_reached", False)):
            tally["ROE_reached"][int(state.offense)] += 1
        return _o(
            state,
            result,
            event=event,
            result_hits=result_hits,
            result_outs=result_outs,
            result_runs=result_runs,
            **kwargs,
        )

    machine._full_pool_outcome = outcome
    machine._full_pool_fielding = fielding
    machine._full_pool_out_advancement = advancement
    machine._resolve_in_play_transition = transition
    machine._steal_opportunity_draw = steal
    machine._accumulate_pa = accumulate
    machine._commit_run_delta = commit


@pytest.fixture(scope="package")
def acceptance_run(production_flags: dict[str, str], preconditions: None) -> AcceptanceRun:
    """Run the production simulator once and return every channel's observations.

    Serial on purpose. ``BatchRunner`` uses a forkserver whose workers inherit
    ``os.environ`` when the server starts, and the server is started lazily and
    cached per process. A parallel lane sharing a process with any other test
    could fork workers carrying the wrong flags — which is the exact class of
    defect this lane exists to catch. Parallelising it is a follow-up that has to
    prove the workers' environment first.

    The kwargs come from ``simulation.sim_kwargs.sim_kwargs_from_state`` (SIM-449),
    never from a hand-rolled copy. That single import is what keeps the lane from
    repeating the defect it is validating: the old harness dropped
    ``home_defense``, ``away_defense`` and ``park_run_factor``, so ``SIM_PARK_FACTOR``
    and ``SIM_FIELDER_RBF`` were structurally inert and A/B tests of them compared
    two identical no-ops.
    """
    import asyncio

    import asyncpg

    from simulation.batch_runner import GameSpec
    from simulation.lineup_resolver import resolve_game_state
    from simulation.production_factory import production_machine_factory
    from simulation.sim_kwargs import (
        SIM_KWARG_KEYS,
        open_sim_duckdb,
        resolve_park_run_factor,
        sim_kwargs_from_state,
    )
    from simulation.sim_loop import BoxScore, simulate_game

    n_games = min(_env_int("SIM_ACCEPTANCE_GAMES", DEFAULT_GAMES), len(ACCEPTANCE_GAME_PKS))
    n_iters = _env_int("SIM_ACCEPTANCE_ITERS", DEFAULT_ITERS)
    game_pks = ACCEPTANCE_GAME_PKS[:n_games]

    run = AcceptanceRun(n_games=n_games, n_iters=n_iters, elapsed_s=0.0)
    # One list per band channel, so a new channel in bands.py cannot be silently
    # dropped here: the lane asserts every name in bands.CHANNELS.
    run.observations = {name: [] for name in bands.CHANNELS}
    run.calls = {
        "_full_pool_outcome": 0,
        "_full_pool_fielding": 0,
        "_full_pool_out_advancement": 0,
        "_resolve_in_play_transition": 0,  # SIM-511: the production path
        "_steal_opportunity_draw": 0,
        "_commit_run_delta": 0,
    }

    duck = open_sim_duckdb()
    if duck is None:
        pytest.fail(
            "SIM-450: the sim DuckDB would not open, so every park_run_factor "
            "falls back to a neutral 1.0 and SIM_PARK_FACTOR is a no-op. The lane "
            "will not report a no-op as a measurement.",
            pytrace=False,
        )

    async def _resolve(game_pk: int) -> Any:
        conn = await asyncpg.connect(_dsn(), timeout=60)
        try:
            state = await resolve_game_state(conn, game_pk, seed=0)
            state.park_run_factor = await resolve_park_run_factor(
                conn, duck, int(game_pk), int(getattr(state, "season", 2024) or 2024)
            )
            return state
        finally:
            await conn.close()

    started = time.perf_counter()
    try:
        for game_pk in game_pks:
            state = asyncio.run(_resolve(game_pk))
            kwargs = sim_kwargs_from_state(state)

            # SIM-449 guard. A missing key here means the lane is measuring a
            # configuration in which two production features cannot act.
            assert set(kwargs) == set(SIM_KWARG_KEYS), (
                f"SIM-449 regression: sim kwargs are {sorted(kwargs)}, "
                f"expected {sorted(SIM_KWARG_KEYS)}"
            )
            run.park_factors[int(game_pk)] = float(kwargs["park_run_factor"])
            run.defense_sizes[int(game_pk)] = (
                len(kwargs["home_defense"]),
                len(kwargs["away_defense"]),
            )

            home_ids = {int(x) for x in (getattr(state, "home_lineup", []) or [])}
            away_ids = {int(x) for x in (getattr(state, "away_lineup", []) or [])}

            spec = GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kwargs))
            machine = production_machine_factory(0, spec)
            tally = _blank_tally()
            _install_probes(machine, tally, run.calls, run.pool_counts)

            for seed in range(n_iters):
                for values in tally.values():
                    values[0] = 0
                    values[1] = 0
                machine.boxscore = BoxScore()
                result = simulate_game(state_machine=machine, seed=seed, **kwargs)

                home_r = int(getattr(result, "home_score", 0))
                away_r = int(getattr(result, "away_score", 0))
                run.observations["R"].extend([float(away_r), float(home_r)])
                for name in TALLY_CHANNELS:
                    run.observations[name].extend([float(tally[name][0]), float(tally[name][1])])

                # SB / CS live on the RUNNER's boxscore line, so they partition by
                # lineup membership rather than by the batting side.
                sb = [0, 0]
                cs = [0, 0]
                total_sb = total_cs = 0
                for pid, line in result.boxscore.lines.items():
                    total_sb += int(line.sb)
                    total_cs += int(line.cs)
                    if int(pid) in away_ids:
                        sb[0] += int(line.sb)
                        cs[0] += int(line.cs)
                    elif int(pid) in home_ids:
                        sb[1] += int(line.sb)
                        cs[1] += int(line.cs)
                run.observations["SB"].extend([float(sb[0]), float(sb[1])])
                run.observations["CS"].extend([float(cs[0]), float(cs[1])])
                run.unattributed_sb += total_sb - (sb[0] + sb[1])
                run.unattributed_cs += total_cs - (cs[0] + cs[1])

                if home_r == away_r:
                    run.ties += 1
                else:
                    run.observations["home_win_pct"].append(1.0 if home_r > away_r else 0.0)
    finally:
        duck.close()
        run.elapsed_s = time.perf_counter() - started

    return run
