"""
api/schemas/props.py
====================
The per-player prop models: the boxscore card (each player's prop means over a
run) and the prop-edge response (one player's prop distribution and edge).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from api.schemas._base import _ApiModel
from api.schemas.markets import EdgeReportModel


class BoxscoreCardRowModel(_ApiModel):
    """One player's row in the SIM-366 boxscore card.

    A compact per-player payload exposing that player's prop **means** over a
    Monte-Carlo run -- for a batter the H/HR/RBI/TB/1B/2B/3B/R/SB/HRR means,
    for a pitcher the K/BB/ER/OUTS/H_ALLOWED means (SIM-421 added the market's
    other lines) -- as a ``{prop_name -> mean}`` map.  Built from a
    :class:`simulation.prop_distributions.PropDistribution` map (one player's
    ``by_player`` entry), reading each :attr:`PropDistribution.mean`.  numpy-free
    (plain floats).
    """

    player_id: int
    #: ``prop_name -> mean`` in :data:`simulation.prop_distributions.BATTER_PROPS`
    #: / :data:`PITCHER_PROPS` order (e.g. ``{"H": 1.2, "HR": 0.3, "RBI": 0.9,
    #: "TB": 2.1, "1B": 0.8, "2B": 0.3, "3B": 0.0, "R": 0.7, "SB": 0.1,
    #: "HRR": 2.8}`` for a batter, or ``{"K": 6.4, "BB": 2.1, "ER": 2.8,
    #: "OUTS": 17.0, "H_ALLOWED": 5.2}`` for a pitcher).  Whichever props the
    #: player owns in the prop set.
    means: dict[str, float] = Field(default_factory=dict)
    #: The player's full name (``raw.players``); None when unknown.
    name: str | None = None
    #: 'away' or 'home'; None for a player the game's kwargs do not name.
    side: str | None = None
    #: The 1-9 batting-order slot; None for a player who does not bat.
    lineup_slot: int | None = None
    #: True for each side's starting pitcher.
    starting_pitcher: bool = False
    #: True for the one summed row of a side's generic bullpen
    #: (the placeholder relievers a game with no listed pen runs with).
    synthetic: bool = False

    @classmethod
    def from_prop_map(
        cls,
        player_id: int,
        props: Mapping[str, Any],
        *,
        name: str | None = None,
        tag: Mapping[str, Any] | None = None,
    ) -> BoxscoreCardRowModel:
        """Build one row from a ``{prop_name -> PropDistribution}`` map.

        ``tag`` is the player's ``{side, lineup_slot, starting_pitcher}`` map;
        absent, the row carries no side.
        """
        tag = tag or {}
        return cls(
            player_id=int(player_id),
            means={str(prop): float(dist.mean) for prop, dist in props.items()},
            name=name,
            side=tag.get("side"),
            lineup_slot=tag.get("lineup_slot"),
            starting_pitcher=bool(tag.get("starting_pitcher", False)),
        )


#: The row ids of each side's summed generic bullpen.
GENERIC_PEN_IDS: dict[str, int] = {"away": -1, "home": -2}


class BoxscoreCardModel(_ApiModel):
    """Response model for the SIM-366 per-player boxscore-average card.

    The boxscore-card payload for one Monte-Carlo run: every player's prop
    **means** (the 100-iteration boxscore average) keyed
    ``str(player_id) -> BoxscoreCardRowModel`` (JSON object keys must be strings).
    Built from a :class:`simulation.prop_distributions.PropDistributionSet` via
    :meth:`from_prop_set`, iterating ``by_player`` and reading each
    :attr:`PropDistribution.mean` -- so the card is the means-only projection of
    the full PMF set.  numpy-free.
    """

    n_iterations: int
    #: The seed the run used (None = an unseeded, fresh draw). A
    #: ``/props`` call with this seed and N reads the same run.
    base_seed: int | None = None
    #: ``str(player_id) -> BoxscoreCardRowModel`` of prop means.
    players: dict[str, BoxscoreCardRowModel] = Field(default_factory=dict)

    @classmethod
    def from_prop_set(
        cls,
        pset: Any,
        *,
        base_seed: int | None = None,
        names: Mapping[int, str] | None = None,
        tags: Mapping[int, Mapping[str, Any]] | None = None,
    ) -> BoxscoreCardModel:
        """Build from a :class:`simulation.prop_distributions.PropDistributionSet`.

        ``names`` and ``tags`` are keyed by player id; a player
        missing from either gets an empty name or no side.
        """
        names = names or {}
        tags = tags or {}
        players: dict[str, BoxscoreCardRowModel] = {}
        # A placeholder reliever (a negative id) folds into its side's one
        # "Bullpen (generic)" row: the sum of their means.
        pen: dict[str, dict[str, float]] = {}
        for pid, props in pset.by_player.items():
            tag = tags.get(int(pid)) or {}
            side = tag.get("side")
            if int(pid) < 0 and side in ("away", "home") and not tag.get("starting_pitcher"):
                sums = pen.setdefault(side, {})
                for prop, dist in props.items():
                    sums[prop] = sums.get(prop, 0.0) + float(dist.mean)
                continue
            players[str(pid)] = BoxscoreCardRowModel.from_prop_map(
                pid, props, name=names.get(int(pid)), tag=tag or None
            )
        for side, means in pen.items():
            pid = GENERIC_PEN_IDS[side]
            players[str(pid)] = BoxscoreCardRowModel(
                player_id=pid,
                means=means,
                name="Bullpen (generic)",
                side=side,
                synthetic=True,
            )
        return cls(n_iterations=int(pset.n_iterations), base_seed=base_seed, players=players)


class PropEdgeResponse(_ApiModel):
    """SIM-390 response for GET /{game_pk}/props/{player_id}/{prop}.

    The full integer-support PMF for one player's one prop over a Monte-Carlo
    run, enriched with optional over/under probabilities and an edge report.

    When a ``line`` query param is supplied the ``p_over``, ``p_under``, and
    ``p_push`` fields are populated using the sportsbook convention
    (half-integer line: over + under == 1; integer line: a push mass is
    excluded from both).  When ``over_ml`` and ``under_ml`` are also supplied
    the ``edge_report`` is populated via
    :func:`betting.clv_engine.prop_edge_report`; the side evaluated is
    determined by the ``bet_side`` query param (``"over"`` or ``"under"``).

    ``pmf`` is the ``{str(value) -> probability}`` map for direct consumer use
    (JSON object keys must be strings, so integer outcomes are stringified).
    """

    player_id: int
    prop: str
    n: int
    #: The player's name, when known ("Gerrit Cole — K").
    player_name: str | None = None
    #: The compact PMF support (sorted distinct integer outcomes).
    support: list[int]
    #: Aligned probabilities (sums to 1.0).
    probabilities: list[float]
    mean: float
    median: float
    std: float
    #: ``str(value) -> probability`` view of the PMF (object keys must be str).
    pmf: dict[str, float] = Field(default_factory=dict)
    # Set when a ``line`` query param is supplied.
    line: float | None = None
    p_over: float | None = None
    p_under: float | None = None
    p_push: float | None = None
    # Set when ``over_ml`` and ``under_ml`` are also supplied.
    edge_report: EdgeReportModel | None = None
