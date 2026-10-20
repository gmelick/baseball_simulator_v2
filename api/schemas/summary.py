"""
api/schemas/summary.py
======================
The models of ``simulation.results``: the confidence interval and the game summary.
"""

from __future__ import annotations

from typing import Any

from api.schemas._base import _ApiModel


class ConfidenceIntervalModel(_ApiModel):
    """Response model for :class:`simulation.results.ConfidenceInterval`.

    A two-sided interval around ``point`` (``low <= point <= high``).  ``level``
    is the nominal confidence (e.g. 0.95); ``method`` names how it was computed.
    ``half_width`` mirrors the dataclass's derived property for consumer
    convenience (it is not a stored field on the source).
    """

    point: float
    low: float
    high: float
    level: float = 0.95
    method: str = "normal"
    half_width: float

    @classmethod
    def from_dataclass(cls, ci: Any) -> ConfidenceIntervalModel:
        """Build from a :class:`simulation.results.ConfidenceInterval`."""
        return cls(
            point=float(ci.point),
            low=float(ci.low),
            high=float(ci.high),
            level=float(ci.level),
            method=str(ci.method),
            half_width=float(ci.half_width),
        )


class GameSimSummaryModel(_ApiModel):
    """Response model for :class:`simulation.results.GameSimSummary` (SIM-327).

    The RAW per-iteration arrays (``home_scores`` / ``away_scores`` /
    ``total_scores``) are exposed as ``list[int]`` and are OPTIONAL on the wire:
    they are present by default but an endpoint may build the model with
    ``include_raw_arrays=False`` (or use :class:`GameSimSummaryLite`) to omit
    them for a list/index view -- see the module docstring's "large per-iteration
    arrays" note.  When omitted they serialise as ``None`` (never a partial /
    down-sampled array).
    """

    n_iterations: int

    home_win_pct: float
    away_win_pct: float
    tie_pct: float

    home_score_mean: float
    away_score_mean: float
    total_score_mean: float
    home_score_median: float
    away_score_median: float
    total_score_median: float

    # The per-iteration scores (N long): numpy arrays on the source, int lists
    # here; optional, so a route can leave them out.
    home_scores: list[int] | None = None
    away_scores: list[int] | None = None
    total_scores: list[int] | None = None

    home_win_ci: ConfidenceIntervalModel
    away_win_ci: ConfidenceIntervalModel
    home_score_ci: ConfidenceIntervalModel
    away_score_ci: ConfidenceIntervalModel
    total_score_ci: ConfidenceIntervalModel

    #: ISO-8601 UTC timestamp (from the source's timezone-aware datetime).
    simulated_at: str
    confidence_level: float = 0.95
    ci_method: str = "normal"

    @classmethod
    def from_dataclass(
        cls,
        summary: Any,
        *,
        include_raw_arrays: bool = True,
    ) -> GameSimSummaryModel:
        """Build from a :class:`simulation.results.GameSimSummary`.

        ``include_raw_arrays`` (default ``True``): when ``False`` the three RAW
        per-iteration score arrays are omitted (left ``None``) -- a deliberate
        opt-in for list/index endpoints that do not want to ship thousands of
        integers.  The contract is complete by default; trimming is the caller's
        explicit choice and is all-or-nothing (never a partial array).
        """
        return cls(
            n_iterations=int(summary.n_iterations),
            home_win_pct=float(summary.home_win_pct),
            away_win_pct=float(summary.away_win_pct),
            tie_pct=float(summary.tie_pct),
            home_score_mean=float(summary.home_score_mean),
            away_score_mean=float(summary.away_score_mean),
            total_score_mean=float(summary.total_score_mean),
            home_score_median=float(summary.home_score_median),
            away_score_median=float(summary.away_score_median),
            total_score_median=float(summary.total_score_median),
            home_scores=(
                [int(x) for x in summary.home_scores.tolist()] if include_raw_arrays else None
            ),
            away_scores=(
                [int(x) for x in summary.away_scores.tolist()] if include_raw_arrays else None
            ),
            total_scores=(
                [int(x) for x in summary.total_scores.tolist()] if include_raw_arrays else None
            ),
            home_win_ci=ConfidenceIntervalModel.from_dataclass(summary.home_win_ci),
            away_win_ci=ConfidenceIntervalModel.from_dataclass(summary.away_win_ci),
            home_score_ci=ConfidenceIntervalModel.from_dataclass(summary.home_score_ci),
            away_score_ci=ConfidenceIntervalModel.from_dataclass(summary.away_score_ci),
            total_score_ci=ConfidenceIntervalModel.from_dataclass(summary.total_score_ci),
            simulated_at=summary.simulated_at.isoformat(),
            confidence_level=float(summary.confidence_level),
            ci_method=str(summary.ci_method),
        )


class GameSimSummaryLite(_ApiModel):
    """A raw-array-FREE projection of :class:`simulation.results.GameSimSummary`.

    Identical to :class:`GameSimSummaryModel` minus the three large
    per-iteration score arrays -- the right shape for a list / index endpoint
    that wants the aggregates (win %, means, CIs) but not the full distribution.
    Build via :meth:`from_dataclass`.
    """

    n_iterations: int

    home_win_pct: float
    away_win_pct: float
    tie_pct: float

    home_score_mean: float
    away_score_mean: float
    total_score_mean: float
    home_score_median: float
    away_score_median: float
    total_score_median: float

    home_win_ci: ConfidenceIntervalModel
    away_win_ci: ConfidenceIntervalModel
    home_score_ci: ConfidenceIntervalModel
    away_score_ci: ConfidenceIntervalModel
    total_score_ci: ConfidenceIntervalModel

    simulated_at: str
    confidence_level: float = 0.95
    ci_method: str = "normal"

    @classmethod
    def from_dataclass(cls, summary: Any) -> GameSimSummaryLite:
        """Build the array-free projection from a :class:`GameSimSummary`."""
        full = GameSimSummaryModel.from_dataclass(summary, include_raw_arrays=False)
        data = full.model_dump()
        data.pop("home_scores", None)
        data.pop("away_scores", None)
        data.pop("total_scores", None)
        return cls(**data)
