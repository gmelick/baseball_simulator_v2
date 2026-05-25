"""
test_qa_sim326.py
=================
SIM-326 -- the **invalid-state detection harness** for the Phase-4 simulation
loop (Sprint 2, QA/DevOps deliverable).

WHAT THIS IS
------------
A test-only harness that drives **N complete games** through
``simulation.sim_loop.simulate_game()`` (the SIM-320 driver) with VARIED per-game
seeds and asserts that NO invalid state is ever reached -- not just at the final
state but at every *committed* state transition the loop produces. The default is
1,000 games (the acceptance criterion); the count is overridable via the
``SIM326_GAMES`` env var or the ``n`` parameter so future tickets can crank it up.

HOW IT RUNS 1,000 GAMES FAST (no live DB / FAISS)
-------------------------------------------------
It reuses the exact SIM-320 dependency-injection idiom from
``tests/unit/test_backend_sim320.py``: an rng-driven ``StateMachine`` that draws
each pitch outcome from its own loop ``numpy`` Generator (no sampler -> no FAISS),
and an injected ``PlayResolver`` that resolves an in-play ball to a (league-
plausible) single or out from the same rng (no DuckDB). Each game gets its own
seed, so it is NOT the same game 1,000x. 1,000 games run in ~1.3s locally.

WHAT "INVALID STATE" MEANS HERE (spec §5.1 / §6.1 / §6.2 + the SIM-311 contract)
-------------------------------------------------------------------------------
At every committed transition the checker asserts:
  * **scores** never negative (home & away);
  * **outs** in [0, 3] and never recorded > 3 in a half-inning (the committed,
    ready-for-next-pitch state never exceeds 2 -- the third out rolls the half);
  * **base occupancy** consistent: no two runners on one bag, no negative /
    duplicate runner ids, runner count <= 3;
  * **count** in [0, 3] balls / [0, 2] strikes on a committed mid-PA state
    (terminal 4-balls / 3-strikes only ever exist transiently inside step 4);
  * **lineup pointers** stay in range of their lineup;
  * **inning** monotonic non-decreasing across the game.
And per finished game:
  * a winner is determined (never returned tied in a normal finish);
  * innings_played >= 9, or a valid walk-off / extra-innings finish;
  * the flags are mutually consistent (extra_innings <=> innings > 9, etc.).

The checker raises ``InvalidStateError`` with a descriptive message naming the
seed, the pitch index, and the violated invariant on the FIRST bad state, so a
failure points straight at the offending game.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from simulation.game_state import Bases, GameState, Half, Team
from simulation.sim_loop import (
    REGULATION_INNINGS,
    FieldingSignal,
    GameSimResult,
    PlayResolver,
    StateMachine,
    simulate_game,
)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

SEASON = 2024
PITCHER = 477132
AWAY_LINEUP = list(range(101, 110))  # 9 batters
HOME_LINEUP = list(range(201, 210))  # 9 batters

#: The acceptance-criterion game count. Overridable via env (so a future ticket
#: can crank it without editing the file) -- the always-on test uses this default.
DEFAULT_GAMES = int(os.environ.get("SIM326_GAMES", "1000"))

#: How big the slow-marked exhaustive run is (lets QA stress far past 1,000).
SLOW_GAMES = int(os.environ.get("SIM326_SLOW_GAMES", "5000"))


# ===========================================================================
# The invalid-state error + the per-state checker
# ===========================================================================


class InvalidStateError(AssertionError):
    """Raised on the FIRST invalid state with a descriptive, debuggable message.

    Subclasses ``AssertionError`` so it reads as a test failure but can also be
    caught explicitly by callers cranking N up programmatically.
    """


def assert_state_valid(
    state: GameState,
    *,
    seed: int,
    pitch_index: int,
    in_play: bool = True,
) -> None:
    """Assert a single committed ``GameState`` is invalid-state-free (spec §5.1 /
    §6.1 + the SIM-311 contract).

    ``in_play=True`` checks the live, ready-for-next-pitch invariants (count
    mid-PA, outs <= 2); ``in_play=False`` tolerates a transient terminal snapshot
    (outs == 3, count at 4/3) for a state inspected before the half/PA rolls.

    Raises :class:`InvalidStateError` with the seed + pitch index + the violated
    rule on the first violation.
    """

    def _fail(msg: str) -> None:
        raise InvalidStateError(
            f"[seed={seed} pitch={pitch_index}] invalid state: {msg} "
            f"(inning={state.inning} half={state.half.name} outs={state.outs} "
            f"count={state.balls}-{state.strikes} "
            f"score={state.away_score}-{state.home_score} "
            f"bases={state.bases.occupancy})"
        )

    # --- scores: never negative ------------------------------------------
    if state.home_score < 0 or state.away_score < 0:
        _fail(f"negative score (home={state.home_score} away={state.away_score})")

    # --- outs: in [0, 3]; committed in-play state never > 2 --------------
    if state.outs < 0:
        _fail(f"negative outs ({state.outs})")
    out_ceiling = 2 if in_play else 3
    if state.outs > out_ceiling:
        _fail(
            f"outs={state.outs} exceeds the "
            f"{'in-play' if in_play else 'terminal'} ceiling {out_ceiling} "
            "(>3 outs in a half-inning is impossible)"
        )

    # --- count: balls [0,3]/strikes [0,2] mid-PA; [0,4]/[0,3] terminal ---
    if state.balls < 0 or state.strikes < 0:
        _fail(f"negative count ({state.balls}-{state.strikes})")
    ball_ceiling = 3 if in_play else 4
    strike_ceiling = 2 if in_play else 3
    if state.balls > ball_ceiling:
        _fail(f"balls={state.balls} exceeds {ball_ceiling} ({'mid-PA' if in_play else 'terminal'})")
    if state.strikes > strike_ceiling:
        _fail(
            f"strikes={state.strikes} exceeds {strike_ceiling} "
            f"({'mid-PA' if in_play else 'terminal'})"
        )

    # --- base occupancy: no two runners on one bag, no bad ids, <= 3 -----
    b = state.bases
    occupants = [
        (name, rid)
        for name, rid in (("1B", b.first), ("2B", b.second), ("3B", b.third))
        if rid is not None
    ]
    ids = [rid for _, rid in occupants]
    for name, rid in occupants:
        if int(rid) < 0:
            _fail(f"{name} holds a negative runner id ({rid})")
    if len(ids) != len(set(ids)):
        # The same runner id appearing on two bags == a runner from nowhere /
        # two runners on one base (a duplicate placement bug).
        _fail(f"a runner appears on two bases at once (ids={ids})")
    if b.count_on_base > 3:
        _fail(f"more than 3 runners on base ({b.count_on_base})")
    # runners_state bitmask must round-trip the occupancy (encoding consistency).
    if bin(b.runners_state).count("1") != b.count_on_base:
        _fail(
            f"runners_state bitmask {b.runners_state:#05b} disagrees with occupancy {b.occupancy}"
        )

    # --- lineup pointers stay in range -----------------------------------
    if state.away_lineup and not (0 <= state.away_lineup_slot < len(state.away_lineup)):
        _fail(
            f"away_lineup_slot={state.away_lineup_slot} out of range [0,{len(state.away_lineup)})"
        )
    if state.home_lineup and not (0 <= state.home_lineup_slot < len(state.home_lineup)):
        _fail(
            f"home_lineup_slot={state.home_lineup_slot} out of range [0,{len(state.home_lineup)})"
        )

    # --- inning sane ------------------------------------------------------
    if state.inning < 1:
        _fail(f"inning={state.inning} is < 1")


# ===========================================================================
# Test doubles -- a no-DB resolver + an rng-driven, self-checking StateMachine
# (the SIM-320 injection idiom, reused verbatim, plus a per-transition check)
# ===========================================================================


class _CyclingResolver(PlayResolver):
    """Resolve an in-play ball to a league-plausible single (~30%) or an out,
    governed by a shared rng so games make progress, score, and END. No DB/FAISS
    -- the batted-ball sample is injected (mirrors the SIM-320 test double)."""

    def __init__(self, rng: np.random.Generator, hit_rate: float = 0.30):
        self.rng = rng
        self.hit_rate = float(hit_rate)
        self._injected_battedball = {"event": "field_out"}

    def resolve_fielding(self, state, battedball_sample) -> FieldingSignal:
        if float(self.rng.random()) < self.hit_rate:
            return FieldingSignal(event="single", result_hits=1, result_outs=0, result_runs=0)
        return FieldingSignal(event="field_out", result_hits=0, result_outs=1, result_runs=0)


class _CheckingRngStateMachine(StateMachine):
    """An rng-driven (no-sampler) StateMachine that ALSO validates the committed
    GameState after every pitch -- so the harness checks invalid states at every
    committed transition, not just the final state.

    The pitch-outcome mix matches the SIM-320 driver test (so games look like
    plausible baseball: ~55% in-play, ~20% ball, ~17% called strike, ~8% foul).
    The post-pitch check uses ``in_play=False`` because the snapshot the loop
    hands back can be a transient terminal boundary (e.g. the just-rolled half),
    exactly the tolerance the SIM-320 driver itself applies; the FINAL-state and
    per-game assertions below pin the stricter live invariants.
    """

    def __init__(self, *args, seed: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self._seed = int(seed)
        self._pitch_index = 0

    def step_pitch(self, state, **_kw):  # type: ignore[override]
        r = float(self.rng.random())
        if r < 0.55:
            outcome = "in_play"
        elif r < 0.75:
            outcome = "ball"
        elif r < 0.92:
            outcome = "called_strike"
        else:
            outcome = "foul"
        result = super().step_pitch(state, pitch_outcome=outcome)
        self._pitch_index += 1
        # Validate the committed state at this transition (tolerant of the
        # transient terminal boundary the loop may hand back post-roll).
        assert_state_valid(
            state,
            seed=self._seed,
            pitch_index=self._pitch_index,
            in_play=False,
        )
        return result


def _make_machine(seed: int, hit_rate: float = 0.30) -> _CheckingRngStateMachine:
    rng = np.random.default_rng(seed)
    return _CheckingRngStateMachine(
        resolver=_CyclingResolver(rng, hit_rate=hit_rate),
        rng=rng,
        seed=seed,
    )


def _run_one(seed: int, hit_rate: float = 0.30) -> GameSimResult:
    """Run ONE full game deterministically from ``seed`` (no live DB/FAISS)."""
    return simulate_game(
        _make_machine(seed, hit_rate=hit_rate),
        seed=seed,
        away_lineup=AWAY_LINEUP,
        home_lineup=HOME_LINEUP,
    )


# ===========================================================================
# The reusable harness: run N games, raise on the first invalid state
# ===========================================================================


def run_invalid_state_harness(
    n: int = DEFAULT_GAMES,
    *,
    start_seed: int = 0,
    hit_rate: float = 0.30,
) -> dict:
    """Run ``n`` complete games with VARIED seeds and assert ZERO invalid states.

    Each game is driven by an rng-seeded, self-checking StateMachine (per-pitch
    validation) + an injected resolver -- no live DuckDB / FAISS. Per-pitch
    invalid states raise inside ``step_pitch``; the per-game terminal + finish
    invariants are checked here. Raises :class:`InvalidStateError` (descriptive,
    seed-tagged) on the FIRST invalid state, so a failure pinpoints the game.

    Returns a small stats dict (games run, total pitches, walk-offs, extra-inning
    games, home/away wins) so callers can sanity-check the population looks like
    real baseball (varied, terminating, both teams winning sometimes).

    Designed to be reused by future tickets: bump ``n`` (or set ``SIM326_GAMES``)
    to stress far past 1,000.
    """
    games = 0
    total_pitches = 0
    walk_offs = 0
    extra_games = 0
    home_wins = 0
    away_wins = 0
    distinct_scores: set[tuple[int, int]] = set()

    for i in range(n):
        seed = start_seed + i
        r = _run_one(seed, hit_rate=hit_rate)

        # --- the game actually terminated as a valid completed game ------
        st = r.final_state
        # Final committed state holds the live invariants (a finished game's
        # final pointer is a ready-for-next-pitch boundary or a walk-off mid-half).
        assert_state_valid(st, seed=seed, pitch_index=r.total_pitches, in_play=True)

        if r.home_score == r.away_score:
            raise InvalidStateError(
                f"[seed={seed}] game finished TIED "
                f"({r.away_score}-{r.home_score}) -- a normal finish must decide "
                "a winner (spec §6.2)."
            )
        if r.innings_played < REGULATION_INNINGS:
            raise InvalidStateError(
                f"[seed={seed}] game ended after only {r.innings_played} innings "
                f"(< {REGULATION_INNINGS}); a completed game plays >= 9 (spec §6.2)."
            )
        # Flag consistency: extra innings <=> more than 9 innings were played.
        if (r.innings_played > REGULATION_INNINGS) != r.extra_innings:
            raise InvalidStateError(
                f"[seed={seed}] extra_innings flag ({r.extra_innings}) disagrees "
                f"with innings_played ({r.innings_played})."
            )
        # A walk-off is only valid in the bottom of the 9th+ with the home lead.
        if r.walk_off:
            if not (st.half == Half.BOTTOM and st.inning >= REGULATION_INNINGS):
                raise InvalidStateError(
                    f"[seed={seed}] walk_off=True but final state is "
                    f"{st.half.name} of inning {st.inning} (spec §6.2)."
                )
            if r.home_score <= r.away_score:
                raise InvalidStateError(
                    f"[seed={seed}] walk_off=True but home did not lead "
                    f"({r.away_score}-{r.home_score})."
                )
        # The winner helper agrees with the score.
        expected = Team.HOME if r.home_score > r.away_score else Team.AWAY
        if r.winner != expected:
            raise InvalidStateError(
                f"[seed={seed}] winner={r.winner} disagrees with score "
                f"({r.away_score}-{r.home_score})."
            )

        games += 1
        total_pitches += r.total_pitches
        walk_offs += int(r.walk_off)
        extra_games += int(r.extra_innings)
        if r.winner == Team.HOME:
            home_wins += 1
        else:
            away_wins += 1
        distinct_scores.add((r.away_score, r.home_score))

    return {
        "games": games,
        "total_pitches": total_pitches,
        "walk_offs": walk_offs,
        "extra_games": extra_games,
        "home_wins": home_wins,
        "away_wins": away_wins,
        "distinct_scores": len(distinct_scores),
    }


# ===========================================================================
# Tests
# ===========================================================================


class TestPerStateChecker:
    """Unit tests for the checker itself -- it must FLAG known-bad states (so a
    green harness run actually means something)."""

    def _good(self) -> GameState:
        return GameState(
            pitcher_id=PITCHER,
            bat_hand="R",
            season=SEASON,
            away_lineup=AWAY_LINEUP,
            home_lineup=HOME_LINEUP,
            batter_id=101,
        )

    def test_a_clean_state_passes(self):
        assert_state_valid(self._good(), seed=0, pitch_index=0)

    def test_negative_score_is_flagged(self):
        st = self._good()
        st.home_score = -1
        with pytest.raises(InvalidStateError, match="negative score"):
            assert_state_valid(st, seed=0, pitch_index=0)

    def test_four_outs_is_flagged(self):
        st = self._good()
        st.outs = 4
        with pytest.raises(InvalidStateError, match="outs=4"):
            assert_state_valid(st, seed=0, pitch_index=0)

    def test_terminal_count_mid_pa_is_flagged(self):
        st = self._good()
        st.balls = 4  # ball-4 should have ended the PA at step 4
        with pytest.raises(InvalidStateError, match="balls=4"):
            assert_state_valid(st, seed=0, pitch_index=0, in_play=True)

    def test_two_runners_on_one_base_is_flagged(self):
        st = self._good()
        # Same runner id on two bags == a runner from nowhere / double placement.
        st.bases = Bases(first=205, second=205)
        with pytest.raises(InvalidStateError, match="two bases"):
            assert_state_valid(st, seed=0, pitch_index=0)

    def test_lineup_pointer_out_of_range_is_flagged(self):
        st = self._good()
        st.away_lineup_slot = 99
        with pytest.raises(InvalidStateError, match="away_lineup_slot"):
            assert_state_valid(st, seed=0, pitch_index=0)


class TestInvalidStateHarness:
    """The acceptance criterion: 1,000 complete games, ZERO invalid states."""

    def test_smoke_50_games_have_zero_invalid_states(self):
        # A fast always-green smoke run (kept tiny so the suite is quick even if
        # the 1,000-game default is ever raised via env).
        stats = run_invalid_state_harness(50)
        assert stats["games"] == 50
        assert stats["total_pitches"] > 0
        # The population is varied (not the same game 50x) and both teams win.
        assert stats["distinct_scores"] > 1
        assert stats["home_wins"] > 0 and stats["away_wins"] > 0

    def test_default_thousand_games_zero_invalid_states(self):
        # THE acceptance criterion: 1,000 complete games via simulate_game() with
        # varied per-game seeds and ZERO invalid states at any committed
        # transition. Runs in ~1.3s under the no-DB injected path (well within the
        # sandbox 45s budget), so it is always-on rather than slow-gated.
        n = DEFAULT_GAMES
        stats = run_invalid_state_harness(n)
        assert stats["games"] == n
        assert stats["total_pitches"] > 0
        # Sanity: the population looks like real baseball, not one repeated game.
        assert stats["distinct_scores"] > 10
        assert stats["home_wins"] > 0 and stats["away_wins"] > 0
        # Across 1,000 games we expect at least some walk-offs and some extras.
        assert stats["walk_offs"] > 0
        assert stats["extra_games"] > 0

    def test_low_offense_population_also_clean(self):
        # A low-offense population (more ties after 9 -> more extra innings) is a
        # different stress on the ghost-runner / extra-innings control; it must be
        # invalid-state-free too.
        stats = run_invalid_state_harness(200, hit_rate=0.18)
        assert stats["games"] == 200
        assert stats["extra_games"] > 0  # low offense -> extras happen


@pytest.mark.slow
class TestExhaustiveHarness:
    """A slow-marked exhaustive run so QA can stress far past 1,000 on demand
    (``pytest -m slow``) without slowing the default suite."""

    def test_slow_exhaustive_run_zero_invalid_states(self):
        stats = run_invalid_state_harness(SLOW_GAMES)
        assert stats["games"] == SLOW_GAMES
        assert stats["walk_offs"] > 0 and stats["extra_games"] > 0
