"""SIM-500 — real base-state invariants + the runner-conservation assertion.

WHAT THIS FILE PROVES
=====================
``Bases.assert_consistent()`` used to reject exactly one thing: a negative
runner id.  Call sites in ``simulation/sim_loop.py`` (``:1558``, ``:1786``,
``:1882``, ``:1936``, ``:2041``, ``:2251``, ``:3596``) treat it as a correctness
guard, so every one of them read as protection and gave none.

Two guards now carry the load:

  * :meth:`Bases.assert_consistent` answers **"is this base state possible?"** —
    non-negative ids, no duplicate id, at most three runners, and (when the
    caller supplies the batter) the batter is not already on a bag.
  * :meth:`Bases.assert_transition` answers **"could one play produce this
    after-state from that before-state?"** — the runner-conservation identity

        runners_before + batter_reached
            == runners_after + runners_scored + runners_retired

    which SIM-499 deletes from ``run_resolution.advance_state``.  Wrong as a
    derivation (it has no term for a runner retired on the play), right as a
    check (the caller supplies that term).

Every test below that asserts a raise is a test the OLD single-check guard
FAILS.  The two ``test_old_guard_*`` tests pin that claim in code: they
reconstruct the old body and show it accepting each illegal state.
"""

from __future__ import annotations

import pytest

from simulation.game_state import (
    MAX_RUNNERS_ON_BASE,
    Bases,
    GameState,
    Half,
)

# ---------------------------------------------------------------------------
# The old guard, reconstructed verbatim from master @ e4be4c1 game_state.py:148.
# The tests below use it to show WHAT the new invariants buy: every illegal
# state in this file walks straight past it.
# ---------------------------------------------------------------------------


def _old_assert_consistent(bases: Bases) -> None:
    """``Bases.assert_consistent`` as it stood before SIM-500 — id sign only."""
    for base, rid in (("1B", bases.first), ("2B", bases.second), ("3B", bases.third)):
        if rid is not None and int(rid) < 0:
            raise ValueError(f"Bases.{base} has a negative runner id: {rid!r}")


def _state(bases: Bases | None = None, *, batter_id: int | None = None) -> GameState:
    """A minimal GameState carrying ``bases`` (the aggregate-guard tests)."""
    gs = GameState(pitcher_id=1, bat_hand="R", season=2024, half=Half.TOP)
    if bases is not None:
        gs.bases = bases
    gs.batter_id = batter_id
    return gs


# ===========================================================================
# assert_consistent — the state guard
# ===========================================================================


def test_legal_states_pass() -> None:
    """The states a real game produces must not raise."""
    Bases().assert_consistent()  # nobody on
    Bases(first=101).assert_consistent()
    Bases(first=101, second=202, third=303).assert_consistent()  # loaded
    Bases(second=0).assert_consistent()  # id 0 is legal; only negatives are not
    # The batter is at the plate, not on a bag -> the optional check passes.
    Bases(first=101, second=202).assert_consistent(batter_id=999)


@pytest.mark.parametrize(
    ("bases", "bags"),
    [
        (Bases(first=7, second=7), "1B/2B"),
        (Bases(first=7, third=7), "1B/3B"),
        (Bases(second=7, third=7), "2B/3B"),
        (Bases(first=7, second=7, third=7), "all three"),
    ],
)
def test_duplicate_runner_id_rejected(bases: Bases, bags: str) -> None:
    """One player cannot stand on two bases.

    This is the shape the audit named: a runner doubled off is re-placed by one
    code path while another places the batter on the same bag.  The old guard
    accepts every one of these.
    """
    _old_assert_consistent(bases)  # the old guard sees nothing wrong
    with pytest.raises(ValueError, match="cannot occupy two bases"):
        bases.assert_consistent()


def test_four_runners_rejected() -> None:
    """At most three runners stand on three bases.

    ``Bases`` has exactly three slots, so it cannot physically hold a fourth
    runner.  The rule is therefore asserted where it IS reachable — on the
    shared checker, which SIM-499's transition mapping calls with a list of
    runner ids **before** it builds the ``Bases``.  ``assert_consistent`` runs
    the same checker over its three bags.
    """
    with pytest.raises(ValueError, match="holds 4 runners"):
        Bases.assert_runner_set_legal([11, 22, 33, 44])
    # Exactly three is legal.
    Bases.assert_runner_set_legal([11, 22, 33])
    assert MAX_RUNNERS_ON_BASE == 3


def test_negative_runner_id_still_rejected() -> None:
    """The one rule the old guard did enforce still holds, and still names the bag."""
    with pytest.raises(ValueError, match=r"Bases\.2B has a negative runner id"):
        Bases(second=-5).assert_consistent()
    with pytest.raises(ValueError, match="negative runner id"):
        Bases.assert_runner_set_legal([1, -2])


def test_batter_already_on_base_rejected() -> None:
    """The batter is not a runner during his own plate appearance."""
    bases = Bases(first=101, second=202)
    _old_assert_consistent(bases)  # the old guard sees nothing wrong
    bases.assert_consistent()  # legal as a STATE ...
    with pytest.raises(ValueError, match="who is also the batter"):
        bases.assert_consistent(batter_id=202)  # ... illegal with 202 at the plate
    # Opt-in: a caller that passes no batter id keeps the old behaviour.
    bases.assert_consistent(batter_id=None)


def test_runner_ids_reports_occupants_in_base_order() -> None:
    assert Bases().runner_ids() == ()
    assert Bases(first=1, third=3).runner_ids() == (1, 3)
    assert Bases(first=1, second=2, third=3).runner_ids() == (1, 2, 3)


def test_gamestate_aggregate_guard_inherits_the_new_rules() -> None:
    """``GameState.assert_invariants`` delegates to the base guard, so the new
    rules reach every one of its call sites for free."""
    gs = _state(Bases(first=7, second=7))
    with pytest.raises(ValueError, match="cannot occupy two bases"):
        gs.assert_invariants(in_play=True)
    gs.bases = Bases(first=7, second=8)
    gs.assert_invariants(in_play=True)  # must not raise


# ===========================================================================
# assert_transition — the runner-conservation guard
# ===========================================================================


def test_transition_accepts_the_real_plays() -> None:
    """Every legal shape balances, including the two the derivation got wrong."""
    # Strikeout, runner on 1B: nothing moves, the batter never reaches.
    Bases(first=11).assert_transition(
        Bases(first=11), batter_reached=0, runners_scored=0, runners_retired=0
    )
    # Walk, bases empty: the batter becomes a runner.
    Bases().assert_transition(
        Bases(first=99), batter_reached=1, runners_scored=0, runners_retired=0, batter_id=99
    )
    # Bases-loaded walk: the runner from 3B is forced home.
    Bases(first=11, second=22, third=33).assert_transition(
        Bases(first=99, second=11, third=22),
        batter_reached=1,
        runners_scored=1,
        runners_retired=0,
        batter_id=99,
    )
    # Single, runner scores from 2B.
    Bases(second=22).assert_transition(
        Bases(first=99), batter_reached=1, runners_scored=1, runners_retired=0, batter_id=99
    )
    # Home run with a runner on 1B: the batter reaches AND scores, so he counts
    # once on each side of the identity.
    Bases(first=11).assert_transition(
        Bases(), batter_reached=1, runners_scored=2, runners_retired=0, batter_id=99
    )
    # Ground-ball double play: the runner is retired, the batter never reached.
    # This is the case the deleted derivation desynced on.
    Bases(first=11).assert_transition(
        Bases(), batter_reached=0, runners_scored=0, runners_retired=1, batter_id=99
    )
    # Fielder's choice: the runner is forced at 2B, the batter is safe at 1B.
    Bases(first=11).assert_transition(
        Bases(first=99), batter_reached=1, runners_scored=0, runners_retired=1, batter_id=99
    )
    # Reach on error: the batter reaches, nobody leaves.  The deleted
    # derivation passed result_hits=0 here and recorded a run value of 0.00.
    Bases(second=22).assert_transition(
        Bases(first=99, second=22),
        batter_reached=1,
        runners_scored=0,
        runners_retired=0,
        batter_id=99,
    )
    # Caught stealing: a runner leaves the bases and no batter is involved.
    Bases(first=11).assert_transition(
        Bases(), batter_reached=0, runners_scored=0, runners_retired=1
    )
    # Steal of home: the runner scores, the plate appearance continues.
    Bases(third=33).assert_transition(
        Bases(), batter_reached=0, runners_scored=1, runners_retired=0
    )


def test_transition_rejects_a_lost_runner() -> None:
    """A runner who vanishes without scoring or being retired breaks the identity."""
    pre, post = Bases(first=11, second=22), Bases(first=11)
    _old_assert_consistent(post)  # the old guard sees nothing wrong
    post.assert_consistent()  # and the state itself IS legal
    with pytest.raises(ValueError, match="does not conserve runners"):
        pre.assert_transition(
            post, batter_reached=0, runners_scored=0, runners_retired=0, batter_id=99
        )


def test_transition_rejects_an_invented_runner() -> None:
    """A runner who appears from nowhere breaks the identity."""
    pre, post = Bases(first=11), Bases(first=11, second=22)
    _old_assert_consistent(post)  # the old guard sees nothing wrong
    post.assert_consistent()
    with pytest.raises(ValueError, match="does not conserve runners"):
        pre.assert_transition(
            post, batter_reached=0, runners_scored=0, runners_retired=0, batter_id=99
        )


def test_transition_rejects_the_phantom_runner_left_on_a_double_play() -> None:
    """The headline case: the doubled-off runner is still standing on 1B.

    The audit's C3 finding — a ground-ball double play records two outs and
    never removes the retired runner.  The after-state is a perfectly legal
    ``Bases``, so no state guard can see it.  Conservation can: the caller says
    one runner was retired, and the runner is still there.
    """
    pre = Bases(first=11)
    phantom = Bases(first=11)  # 11 was doubled off at 2B and never left
    _old_assert_consistent(phantom)  # the old guard sees nothing wrong
    phantom.assert_consistent()  # and neither can the new STATE guard
    with pytest.raises(ValueError, match="does not conserve runners"):
        pre.assert_transition(
            phantom, batter_reached=0, runners_scored=0, runners_retired=1, batter_id=99
        )
    # The correct after-state passes.
    pre.assert_transition(
        Bases(), batter_reached=0, runners_scored=0, runners_retired=1, batter_id=99
    )


def test_transition_rejects_an_identity_swap() -> None:
    """One runner out, a different runner in, counts unchanged.

    The count identity balances, so only the batter-aware membership check
    catches it.  It runs when the caller supplies ``batter_id``.
    """
    pre, post = Bases(first=11), Bases(first=22)
    with pytest.raises(ValueError, match="invents runner"):
        pre.assert_transition(
            post, batter_reached=0, runners_scored=0, runners_retired=0, batter_id=99
        )
    # Without the batter id the membership check cannot run, and the identity
    # alone accepts it.  This is the documented limit of the cheap form.
    pre.assert_transition(post, batter_reached=0, runners_scored=0, runners_retired=0)


def test_transition_rejects_the_batter_on_base_when_he_did_not_reach() -> None:
    pre, post = Bases(), Bases(first=99)
    with pytest.raises(ValueError, match="does not conserve runners"):
        pre.assert_transition(
            post, batter_reached=0, runners_scored=0, runners_retired=0, batter_id=99
        )
    # The count identity catches that one.  Now a claim where the count DOES
    # balance — two runners on, one retired, one left on base — but the runner
    # standing on 1B is the batter, who is said never to have reached:
    #   before=2 + reached=0 == after=1 + scored=0 + retired=1.
    with pytest.raises(ValueError, match=r"batter 99 stands on a base"):
        Bases(first=11, second=22).assert_transition(
            Bases(first=99), batter_reached=0, runners_scored=0, runners_retired=1, batter_id=99
        )


def test_transition_rejects_a_batter_already_on_base_in_the_pre_state() -> None:
    with pytest.raises(ValueError, match="who is also the batter"):
        Bases(first=99).assert_transition(
            Bases(first=99), batter_reached=0, runners_scored=0, runners_retired=0, batter_id=99
        )


def test_transition_rejects_an_illegal_pre_or_post_state() -> None:
    """Both endpoints must be legal states before conservation means anything."""
    with pytest.raises(ValueError, match="cannot occupy two bases"):
        Bases(first=7, second=7).assert_transition(
            Bases(first=7), batter_reached=0, runners_scored=0, runners_retired=1
        )
    with pytest.raises(ValueError, match="cannot occupy two bases"):
        Bases(first=7).assert_transition(
            Bases(first=7, second=7), batter_reached=1, runners_scored=0, runners_retired=0
        )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"batter_reached": 2, "runners_scored": 0, "runners_retired": 0}, "must be 0 or 1"),
        ({"batter_reached": 0, "runners_scored": -1, "runners_retired": 0}, "must be >= 0"),
        ({"batter_reached": 0, "runners_scored": 0, "runners_retired": -1}, "must be >= 0"),
    ],
)
def test_transition_rejects_impossible_counts(kwargs: dict[str, int], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        Bases(first=11).assert_transition(Bases(), **kwargs)


def test_transition_accepts_bool_and_int_for_batter_reached() -> None:
    """Callers hold the flag as a bool or as a 0/1 int; both must work."""
    Bases().assert_transition(
        Bases(first=99), batter_reached=True, runners_scored=0, runners_retired=0
    )
    Bases().assert_transition(
        Bases(first=99), batter_reached=1, runners_scored=0, runners_retired=0
    )
    Bases(first=11).assert_transition(
        Bases(first=11), batter_reached=False, runners_scored=0, runners_retired=0
    )


# ===========================================================================
# The old guard could not fail — pinned, so nobody re-weakens it
# ===========================================================================


def test_old_guard_accepts_every_illegal_state_the_new_one_rejects() -> None:
    """RULE 1 evidence, in code.

    Each state below is illegal.  The pre-SIM-500 guard accepts all of them.
    If a future edit weakens ``assert_consistent`` back toward the old body,
    the paired assertions in this file go red.
    """
    illegal = [
        Bases(first=7, second=7),  # one player on two bases
        Bases(first=7, second=7, third=7),
        Bases(first=101, second=202),  # 202 is also the batter (see below)
    ]
    for bases in illegal:
        _old_assert_consistent(bases)  # never raises

    # ... and the new guard rejects each of them.
    with pytest.raises(ValueError):
        illegal[0].assert_consistent()
    with pytest.raises(ValueError):
        illegal[1].assert_consistent()
    with pytest.raises(ValueError):
        illegal[2].assert_consistent(batter_id=202)


def test_old_guard_has_no_transition_check_at_all() -> None:
    """The conservation identity is new.  The old guard took one argument and
    could not see two states, so no version of it could detect a lost, invented
    or phantom runner."""
    assert not hasattr(_old_assert_consistent, "assert_transition")
    pre, phantom = Bases(first=11), Bases(first=11)
    _old_assert_consistent(pre)
    _old_assert_consistent(phantom)  # both "pass" the old guard
    with pytest.raises(ValueError):
        pre.assert_transition(
            phantom, batter_reached=0, runners_scored=0, runners_retired=1, batter_id=99
        )
