"""
api/schemas
===========
The API's response models: the wire contract of the simulation and betting
outputs.

The simulation and betting modules return dataclasses, and several of them hold
numpy arrays and numpy scalars, which ``json.dumps`` cannot write. Each model
here mirrors one of those dataclasses field for field, with JSON-native types,
and a ``from_dataclass(...)`` classmethod builds it from the dataclass. So
FastAPI documents and validates the JSON it returns (``response_model=``), and
no numpy value reaches the wire: an array becomes a ``list[int]`` or a
``list[float]``, a numpy scalar a plain ``int`` or ``float``. The converters
use :func:`api.serialization.to_jsonable` where a field needs it.

A model's docstring and fields appear in the OpenAPI schema
(``frontend/openapi.json``), so an edit to either is a contract change.

THE PER-ITERATION ARRAYS
------------------------
``GameSimSummary`` carries the per-iteration ``home_scores``,
``away_scores`` and ``total_scores`` arrays (N long, thousands for a large
run). :class:`GameSimSummaryModel` carries all three by default; a route that
does not want them passes ``include_raw_arrays=False``, or uses
:class:`GameSimSummaryLite`, which has none. An array is never cut down: it is
all there or absent.

The modules, by the source of their models:

  * ``summary``   -- ``simulation.results``: the confidence interval and the game summary
  * ``snapshots`` -- ``simulation.snapshots``: the field, the play-by-play, the override delta
  * ``game_card`` -- ``simulation.linescore`` and ``simulation.pitcher_decisions``
  * ``props``     -- the boxscore card and the prop-edge response
  * ``markets``   -- ``betting``: CLV, the edge report, the bet signal, the line movement
  * ``live``      -- the live service's stored game state

Every model derives from ``_base._ApiModel``, which forbids extra fields.
"""

from api.schemas.game_card import InningLineModel, LinescoreModel, PitcherDecisionsModel
from api.schemas.live import LiveGameStateResponse
from api.schemas.markets import (
    BetSignalModel,
    CLVModel,
    EdgeReportModel,
    LineMovementModel,
    LineQuoteModel,
)
from api.schemas.props import (
    GENERIC_PEN_IDS,
    BoxscoreCardModel,
    BoxscoreCardRowModel,
    PropEdgeResponse,
)
from api.schemas.snapshots import (
    FieldSnapshotModel,
    MetricDeltaModel,
    OverrideDeltaModel,
    PlayByPlayEntryModel,
    PlayByPlayModel,
    PlayerRefModel,
    StateAtPitchModel,
)
from api.schemas.summary import ConfidenceIntervalModel, GameSimSummaryLite, GameSimSummaryModel

__all__ = [
    # summary
    "ConfidenceIntervalModel",
    "GameSimSummaryModel",
    "GameSimSummaryLite",
    # snapshots
    "PlayerRefModel",
    "FieldSnapshotModel",
    "PlayByPlayEntryModel",
    "PlayByPlayModel",
    "StateAtPitchModel",
    "MetricDeltaModel",
    "OverrideDeltaModel",
    # game_card
    "InningLineModel",
    "LinescoreModel",
    "PitcherDecisionsModel",
    # props
    "BoxscoreCardRowModel",
    "BoxscoreCardModel",
    "GENERIC_PEN_IDS",
    "PropEdgeResponse",
    # markets
    "CLVModel",
    "EdgeReportModel",
    "BetSignalModel",
    "LineQuoteModel",
    "LineMovementModel",
    # live
    "LiveGameStateResponse",
]
