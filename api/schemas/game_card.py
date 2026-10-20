"""
api/schemas/game_card.py
========================
The game card of a stored game: the linescore (``simulation.linescore``) and
the pitcher decisions (``simulation.pitcher_decisions``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from api.schemas._base import _ApiModel


class InningLineModel(_ApiModel):
    """Response model for :class:`simulation.linescore.InningLine` (SIM-362).

    One inning's away (top) / home (bottom) run cells.  ``away`` / ``home`` are
    ``Optional[int]``: ``None`` is the classic scoreboard "x" (a half that was
    never played -- e.g. the bottom of the final inning when the home team already
    leads, or a walk-off), distinguished from ``0`` (played and scoreless).
    ``away_played`` / ``home_played`` mirror the source's derived properties.
    """

    inning: int
    away: int | None = None
    home: int | None = None

    #: Derived from the source dataclass's properties (a half is "played" iff its
    #: cell is not None).
    away_played: bool
    home_played: bool

    @classmethod
    def from_dataclass(cls, ln: Any) -> InningLineModel:
        """Build from a :class:`simulation.linescore.InningLine`."""
        return cls(
            inning=int(ln.inning),
            away=None if ln.away is None else int(ln.away),
            home=None if ln.home is None else int(ln.home),
            away_played=bool(ln.away_played),
            home_played=bool(ln.home_played),
        )


class LinescoreModel(_ApiModel):
    """Response model for :class:`simulation.linescore.Linescore` (SIM-362).

    The classic baseball linescore: the per-inning run grid plus each team's
    Runs / Hits / Errors totals.  ``innings`` is ordered 1..N (N >= 9 for extra
    innings).  ``away_by_inning`` / ``home_by_inning`` mirror the source's derived
    per-inning cell lists (``None`` == not played); ``n_innings`` mirrors the
    derived length.  numpy-free (plain ints / Optional[int]).
    """

    innings: list[InningLineModel] = Field(default_factory=list)

    away_runs: int = 0
    home_runs: int = 0
    away_hits: int = 0
    home_hits: int = 0
    away_errors: int = 0
    home_errors: int = 0

    #: Derived from the source dataclass's properties.
    n_innings: int = 0
    away_by_inning: list[int | None] = Field(default_factory=list)
    home_by_inning: list[int | None] = Field(default_factory=list)

    @classmethod
    def from_dataclass(cls, ls: Any) -> LinescoreModel:
        """Build from a :class:`simulation.linescore.Linescore`."""
        return cls(
            innings=[InningLineModel.from_dataclass(ln) for ln in ls.innings],
            away_runs=int(ls.away_runs),
            home_runs=int(ls.home_runs),
            away_hits=int(ls.away_hits),
            home_hits=int(ls.home_hits),
            away_errors=int(ls.away_errors),
            home_errors=int(ls.home_errors),
            n_innings=int(ls.n_innings),
            away_by_inning=[None if c is None else int(c) for c in ls.away_by_inning],
            home_by_inning=[None if c is None else int(c) for c in ls.home_by_inning],
        )

    @classmethod
    def from_jsonable(cls, data: Mapping[str, Any]) -> LinescoreModel:
        """Build directly from a persisted ``to_jsonable(Linescore)`` dict.

        The DuckDB game-card store (SIM-362) persists the linescore as the
        ``to_jsonable`` dict -- which carries the dataclass FIELDS (``innings`` +
        the six R/H/E totals) but NOT the derived ``n_innings`` / ``away_by_inning``
        / ``home_by_inning`` properties (``to_jsonable`` serializes fields only).
        This recomputes those derived views from the stored ``innings`` so the wire
        shape is identical whether served fresh (``from_dataclass``) or from the
        store -- no need to reconstruct the frozen dataclasses.
        """
        innings = list(data.get("innings") or [])
        inning_models: list[InningLineModel] = []
        away_by_inning: list[int | None] = []
        home_by_inning: list[int | None] = []
        for entry in innings:
            away = entry.get("away")
            home = entry.get("home")
            away = None if away is None else int(away)
            home = None if home is None else int(home)
            inning_models.append(
                InningLineModel(
                    inning=int(entry["inning"]),
                    away=away,
                    home=home,
                    away_played=away is not None,
                    home_played=home is not None,
                )
            )
            away_by_inning.append(away)
            home_by_inning.append(home)
        return cls(
            innings=inning_models,
            away_runs=int(data.get("away_runs", 0)),
            home_runs=int(data.get("home_runs", 0)),
            away_hits=int(data.get("away_hits", 0)),
            home_hits=int(data.get("home_hits", 0)),
            away_errors=int(data.get("away_errors", 0)),
            home_errors=int(data.get("home_errors", 0)),
            n_innings=len(inning_models),
            away_by_inning=away_by_inning,
            home_by_inning=home_by_inning,
        )


class PitcherDecisionsModel(_ApiModel):
    """Response model for :class:`simulation.pitcher_decisions.PitcherDecisions` (SIM-364).

    The derived W/L/Save decision for one game.  All three pitcher ids are
    ``Optional[int]`` (``None`` when no decision can be made -- a tie, an
    empty/unfinished stream, or no save situation).  ``home_score`` / ``away_score``
    are the final committed scores for context.
    """

    winning_pitcher_id: int | None = None
    losing_pitcher_id: int | None = None
    save_pitcher_id: int | None = None
    home_score: int = 0
    away_score: int = 0

    @classmethod
    def from_dataclass(cls, dec: Any) -> PitcherDecisionsModel:
        """Build from a :class:`simulation.pitcher_decisions.PitcherDecisions`."""
        return cls(
            winning_pitcher_id=(
                None if dec.winning_pitcher_id is None else int(dec.winning_pitcher_id)
            ),
            losing_pitcher_id=(
                None if dec.losing_pitcher_id is None else int(dec.losing_pitcher_id)
            ),
            save_pitcher_id=(None if dec.save_pitcher_id is None else int(dec.save_pitcher_id)),
            home_score=int(dec.home_score),
            away_score=int(dec.away_score),
        )

    @classmethod
    def from_jsonable(cls, data: Mapping[str, Any]) -> PitcherDecisionsModel:
        """Build directly from a persisted ``to_jsonable(PitcherDecisions)`` dict.

        ``PitcherDecisions`` has only plain fields (no derived properties), so the
        persisted dict round-trips 1:1; this just re-coerces types defensively.
        """

        def _oi(v: Any) -> int | None:
            return None if v is None else int(v)

        return cls(
            winning_pitcher_id=_oi(data.get("winning_pitcher_id")),
            losing_pitcher_id=_oi(data.get("losing_pitcher_id")),
            save_pitcher_id=_oi(data.get("save_pitcher_id")),
            home_score=int(data.get("home_score", 0)),
            away_score=int(data.get("away_score", 0)),
        )
