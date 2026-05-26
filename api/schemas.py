"""
api/schemas.py
==============
SIM-350 -- the **API response-serialization contract** (Phase 5, Sprint 1).

WHAT THIS IS
------------
The Phase-4 simulation/betting output dataclasses are the simulator's internal
contract, but they are NOT a wire contract: several of them hold **numpy arrays**
(``GameSimSummary.home_scores``, ``PropDistribution.support`` /
``.probabilities``) and **numpy scalars**, which ``json.dumps`` cannot serialise.
The Phase-5 FastAPI endpoints (the P1 tickets that consume THIS module) need:

  1. typed **response models** so FastAPI can document + validate the JSON it
     returns (OpenAPI schema, ``response_model=``), and
  2. a guarantee that **no numpy ever reaches the wire** (every array becomes a
     JSON-native ``list[int]`` / ``list[float]``).

This module is that contract: one **Pydantic v2** model per Phase-4 output
dataclass, each with a ``from_dataclass(...)`` classmethod that accepts the
source dataclass instance and returns the JSON-safe model.  The converters use
:func:`api.serialization.to_jsonable` as their numpy-stripping building block.

NUMPY POLICY
------------
Every numpy array on a source dataclass is mirrored as a JSON-native list of the
appropriate element type (``list[int]`` for the int64 score / support arrays,
``list[float]`` for the float64 probability arrays).  numpy scalars become plain
``int`` / ``float``.  After conversion ``model.model_dump_json()`` succeeds and
``json.loads`` of it yields ONLY plain Python types.

LARGE PER-ITERATION ARRAYS (a documented contract decision)
-----------------------------------------------------------
:class:`GameSimSummary` carries the RAW per-iteration ``home_scores`` /
``away_scores`` / ``total_scores`` arrays (length N -- can be many thousands for
a large Monte-Carlo run).  The CONTRACT LAYER does NOT silently drop them: a
``GameSimSummaryModel`` faithfully exposes all three as ``list[int]`` so a
consumer that needs the raw distribution (e.g. to recompute a custom percentile)
gets it.  But because shipping thousands of integers on every game endpoint is
usually wasteful, this module ALSO provides:

  * :class:`GameSimSummaryModel.from_dataclass(..., include_raw_arrays=False)` --
    omit the three raw arrays (they serialise as ``None``), and
  * :class:`GameSimSummaryLite` -- a raw-array-free projection for list / index
    endpoints.

The DEFAULT of :meth:`GameSimSummaryModel.from_dataclass` INCLUDES the arrays
(the contract is complete by default; an endpoint OPTS IN to trimming).  The raw
arrays are never altered or down-sampled -- it is all-or-nothing, so a present
array is always the full, faithful signal.

CONVENTIONS
-----------
  * Pydantic v2 (``model_config = ConfigDict(...)``, ``model_dump_json``).
  * Each model mirrors its source dataclass field-for-field with precise types;
    nested dataclasses map to nested models (e.g. ``ConfidenceInterval`` ->
    :class:`ConfidenceIntervalModel`).
  * The converter is a ``@classmethod`` ``from_dataclass`` on each model so the
    call site reads ``SomeModel.from_dataclass(instance)``.
  * No source dataclass is modified; everything here reads attributes only.

Owner: Backend Developer (Agent 5).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from api.serialization import to_jsonable

# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class _ApiModel(BaseModel):
    """Shared base for every SIM-350 response model.

    ``extra="forbid"`` keeps the wire contract tight (a typo'd field is an error,
    not silently accepted); the models are response-only so we do not need
    ``populate_by_name`` / aliasing here.
    """

    model_config = ConfigDict(extra="forbid")


# ===========================================================================
# simulation/results.py  ->  ConfidenceInterval, GameSimSummary
# ===========================================================================


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

    # RAW per-iteration signal (length N) -- numpy arrays on the source, exposed
    # as JSON-native int lists; optional so an endpoint can trim them.
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


# ===========================================================================
# simulation/sim_loop.py (re-exported via results.py)
#   ->  PlayerStatLine, BoxScore
# ===========================================================================


class PlayerStatLineModel(_ApiModel):
    """Response model for :class:`simulation.sim_loop.PlayerStatLine` (SIM-328).

    Mirrors the stored batting (``ab``/``h``/``hr``/``rbi``) and pitching
    (``outs_recorded``/``k``/``bb``/``er``) fields, plus the derived innings-
    pitched views (``ip`` / ``ip_outs`` / ``ip_thirds``) the box-score UI reads.
    """

    player_id: int

    ab: int = 0
    h: int = 0
    hr: int = 0
    rbi: int = 0

    outs_recorded: int = 0
    k: int = 0
    bb: int = 0
    er: int = 0

    #: Derived (from outs_recorded): IP views the UI / rate stats consume.
    ip: float
    ip_outs: int
    ip_thirds: float

    @classmethod
    def from_dataclass(cls, line: Any) -> PlayerStatLineModel:
        """Build from a :class:`simulation.sim_loop.PlayerStatLine`."""
        return cls(
            player_id=int(line.player_id),
            ab=int(line.ab),
            h=int(line.h),
            hr=int(line.hr),
            rbi=int(line.rbi),
            outs_recorded=int(line.outs_recorded),
            k=int(line.k),
            bb=int(line.bb),
            er=int(line.er),
            ip=float(line.ip),
            ip_outs=int(line.ip_outs),
            ip_thirds=float(line.ip_thirds),
        )


class BoxScoreModel(_ApiModel):
    """Response model for :class:`simulation.sim_loop.BoxScore` (SIM-328).

    The per-game accumulator keyed by ``player_id``.  JSON object keys must be
    strings, so the ``lines`` map is keyed by the player id rendered as ``str``;
    ``batters`` / ``pitchers`` mirror the dataclass's convenience filter
    properties.
    """

    #: ``str(player_id) -> PlayerStatLineModel`` for every line in the box score.
    lines: dict[str, PlayerStatLineModel] = Field(default_factory=dict)
    #: Filtered views (str-keyed) for the lines with batting / pitching activity.
    batters: dict[str, PlayerStatLineModel] = Field(default_factory=dict)
    pitchers: dict[str, PlayerStatLineModel] = Field(default_factory=dict)

    @classmethod
    def from_dataclass(cls, box: Any) -> BoxScoreModel:
        """Build from a :class:`simulation.sim_loop.BoxScore`."""
        return cls(
            lines={
                str(pid): PlayerStatLineModel.from_dataclass(ln) for pid, ln in box.lines.items()
            },
            batters={
                str(pid): PlayerStatLineModel.from_dataclass(ln) for pid, ln in box.batters.items()
            },
            pitchers={
                str(pid): PlayerStatLineModel.from_dataclass(ln) for pid, ln in box.pitchers.items()
            },
        )


# ===========================================================================
# simulation/win_probability.py  ->  CalibrationMap, WinProbability
# ===========================================================================


class CalibrationMapModel(_ApiModel):
    """Response model for :class:`simulation.win_probability.CalibrationMap`.

    Only the ``name`` is wire-serialisable -- ``fn`` is a Python callable (the
    fitted-curve seam) and is intentionally NOT exposed on the API; the name
    (e.g. "identity" / "isotonic-2026H1") is the honest, serialisable descriptor
    of which calibration was applied.
    """

    name: str = "identity"

    @classmethod
    def from_dataclass(cls, cmap: Any) -> CalibrationMapModel:
        """Build from a :class:`simulation.win_probability.CalibrationMap`."""
        return cls(name=str(cmap.name))


class WinProbabilityModel(_ApiModel):
    """Response model for :class:`simulation.win_probability.WinProbability` (SIM-330).

    The calibrated home/away win probability (which sum to 1.0).  ``tie_handling``
    is the source enum's ``.value`` (a JSON-native string, e.g. "split").
    """

    home_win_prob: float
    away_win_prob: float

    n_iterations: int
    n_decisive: int

    home_win_ci: ConfidenceIntervalModel

    tie_pct: float

    alpha: float
    #: The TieHandling enum value as a string ("split" / "drop").
    tie_handling: str
    calibration_map: str
    confidence_level: float
    method: str = "beta-smoothed+wald"

    @classmethod
    def from_dataclass(cls, wp: Any) -> WinProbabilityModel:
        """Build from a :class:`simulation.win_probability.WinProbability`."""
        # tie_handling is a TieHandling enum; take its .value if present.
        th = wp.tie_handling
        tie_handling = th.value if hasattr(th, "value") else str(th)
        return cls(
            home_win_prob=float(wp.home_win_prob),
            away_win_prob=float(wp.away_win_prob),
            n_iterations=int(wp.n_iterations),
            n_decisive=int(wp.n_decisive),
            home_win_ci=ConfidenceIntervalModel.from_dataclass(wp.home_win_ci),
            tie_pct=float(wp.tie_pct),
            alpha=float(wp.alpha),
            tie_handling=str(tie_handling),
            calibration_map=str(wp.calibration_map),
            confidence_level=float(wp.confidence_level),
            method=str(wp.method),
        )


# ===========================================================================
# simulation/snapshots.py
#   ->  PlayerRef, FieldSnapshot, PlayByPlayEntry, PlayByPlay,
#       StateAtPitch, MetricDelta, OverrideDelta
# ===========================================================================


class PlayerRefModel(_ApiModel):
    """Response model for :class:`simulation.snapshots.PlayerRef`.

    A player reference: integer id + optional display label.
    """

    player_id: int
    label: str | None = None

    @classmethod
    def from_dataclass(cls, ref: Any) -> PlayerRefModel:
        """Build from a :class:`simulation.snapshots.PlayerRef`."""
        return cls(
            player_id=int(ref.player_id),
            label=None if ref.label is None else str(ref.label),
        )


def _optional_player_ref(ref: Any) -> PlayerRefModel | None:
    """Convert an ``Optional[PlayerRef]`` (None == empty slot) to its model."""
    return None if ref is None else PlayerRefModel.from_dataclass(ref)


class FieldSnapshotModel(_ApiModel):
    """Response model for :class:`simulation.snapshots.FieldSnapshot` (SIM-331).

    The BaseballFieldGraphic state: 9 defensive positions + batter + baserunners
    + count/outs/inning/half/score chrome.  ``positions`` and ``baserunners`` map
    a label to an OPTIONAL :class:`PlayerRefModel` (``None`` == unassigned slot /
    empty bag).  ``occupied_bases`` / ``runners_on`` mirror the source's derived
    properties for consumer convenience.
    """

    positions: dict[str, PlayerRefModel | None]
    batter: PlayerRefModel | None = None
    baserunners: dict[str, PlayerRefModel | None]

    balls: int
    strikes: int
    outs: int
    inning: int
    half: str
    home_score: int
    away_score: int
    runners_state: int = 0

    #: Derived from the source dataclass's properties.
    occupied_bases: list[str]
    runners_on: int

    @classmethod
    def from_dataclass(cls, snap: Any) -> FieldSnapshotModel:
        """Build from a :class:`simulation.snapshots.FieldSnapshot`."""
        return cls(
            positions={str(pos): _optional_player_ref(ref) for pos, ref in snap.positions.items()},
            batter=_optional_player_ref(snap.batter),
            baserunners={
                str(base): _optional_player_ref(ref) for base, ref in snap.baserunners.items()
            },
            balls=int(snap.balls),
            strikes=int(snap.strikes),
            outs=int(snap.outs),
            inning=int(snap.inning),
            half=str(snap.half),
            home_score=int(snap.home_score),
            away_score=int(snap.away_score),
            runners_state=int(snap.runners_state),
            occupied_bases=[str(b) for b in snap.occupied_bases],
            runners_on=int(snap.runners_on),
        )


class PlayByPlayEntryModel(_ApiModel):
    """Response model for :class:`simulation.snapshots.PlayByPlayEntry` (SIM-331).

    One pitch in the /plays scroll.  ``at_bat`` groups pitches into a PA, ``pitch``
    is the 1-based pitch within the PA, ``sequence`` is the global pitch index;
    the PA's resolved event is carried on the terminal pitch (``is_pa_end``).
    """

    sequence: int
    at_bat: int
    pitch: int

    pitch_outcome: str
    is_contact: bool
    is_pa_end: bool

    event: str | None = None

    runs_scored: int = 0
    outs_recorded: int = 0

    exit_velo: float | None = None
    launch_angle: float | None = None
    spray_angle: float | None = None

    runs: float = 0.0
    canonical_event: str | None = None

    @classmethod
    def from_dataclass(cls, entry: Any) -> PlayByPlayEntryModel:
        """Build from a :class:`simulation.snapshots.PlayByPlayEntry`."""
        return cls(
            sequence=int(entry.sequence),
            at_bat=int(entry.at_bat),
            pitch=int(entry.pitch),
            pitch_outcome=str(entry.pitch_outcome),
            is_contact=bool(entry.is_contact),
            is_pa_end=bool(entry.is_pa_end),
            event=None if entry.event is None else str(entry.event),
            runs_scored=int(entry.runs_scored),
            outs_recorded=int(entry.outs_recorded),
            exit_velo=None if entry.exit_velo is None else float(entry.exit_velo),
            launch_angle=(None if entry.launch_angle is None else float(entry.launch_angle)),
            spray_angle=(None if entry.spray_angle is None else float(entry.spray_angle)),
            runs=float(entry.runs),
            canonical_event=(None if entry.canonical_event is None else str(entry.canonical_event)),
        )


class PlayByPlayModel(_ApiModel):
    """Response model for :class:`simulation.snapshots.PlayByPlay` (SIM-331).

    The GET /api/games/{game_pk}/plays collection -- pitch-level entries plus the
    source's derived counts (``n_pitches`` / ``n_plate_appearances``).
    """

    entries: list[PlayByPlayEntryModel] = Field(default_factory=list)
    #: Derived from the source dataclass's properties (FULL-game totals, never
    #: trimmed by pagination — so a paged response still reports the true counts).
    n_pitches: int = 0
    n_plate_appearances: int = 0
    # SIM-415 pagination — populated only when the /plays response is paged
    # (``limit`` supplied); ``None`` on the default full-collection response so
    # the shape is backward-compatible.
    #: Total entries in the full game (== len of the un-paged stream).
    total_entries: int | None = None
    #: 0-based offset of the returned slice.
    page_offset: int | None = None
    #: The requested page size (``limit``).
    page_limit: int | None = None
    #: Number of entries actually returned in this page (len(entries)).
    returned_entries: int | None = None

    @classmethod
    def from_dataclass(cls, pbp: Any) -> PlayByPlayModel:
        """Build from a :class:`simulation.snapshots.PlayByPlay`."""
        return cls(
            entries=[PlayByPlayEntryModel.from_dataclass(e) for e in pbp.entries],
            n_pitches=int(pbp.n_pitches),
            n_plate_appearances=int(pbp.n_plate_appearances),
        )


class StateAtPitchModel(_ApiModel):
    """Response model for :class:`simulation.snapshots.StateAtPitch` (SIM-331).

    A point-in-time :class:`FieldSnapshotModel` tagged with the at-bat / pitch
    indices (and global sequence, if known).
    """

    at_bat: int
    pitch: int
    field: FieldSnapshotModel
    sequence: int | None = None

    @classmethod
    def from_dataclass(cls, sap: Any) -> StateAtPitchModel:
        """Build from a :class:`simulation.snapshots.StateAtPitch`."""
        return cls(
            at_bat=int(sap.at_bat),
            pitch=int(sap.pitch),
            field=FieldSnapshotModel.from_dataclass(sap.field),
            sequence=None if sap.sequence is None else int(sap.sequence),
        )


class MetricDeltaModel(_ApiModel):
    """Response model for :class:`simulation.snapshots.MetricDelta` (SIM-331).

    A single baseline-vs-override metric.  ``delta`` (override - baseline) mirrors
    the source dataclass's derived property.
    """

    metric: str
    baseline: float
    override: float
    delta: float

    @classmethod
    def from_dataclass(cls, md: Any) -> MetricDeltaModel:
        """Build from a :class:`simulation.snapshots.MetricDelta`."""
        return cls(
            metric=str(md.metric),
            baseline=float(md.baseline),
            override=float(md.override),
            delta=float(md.delta),
        )


class OverrideDeltaModel(_ApiModel):
    """Response model for :class:`simulation.snapshots.OverrideDelta` (SIM-331).

    The baseline-vs-override comparison: a ``{metric_name -> MetricDeltaModel}``
    map plus an optional free-text ``description`` of the roster change.
    """

    metrics: dict[str, MetricDeltaModel] = Field(default_factory=dict)
    description: str | None = None

    @classmethod
    def from_dataclass(cls, od: Any) -> OverrideDeltaModel:
        """Build from a :class:`simulation.snapshots.OverrideDelta`."""
        return cls(
            metrics={
                str(name): MetricDeltaModel.from_dataclass(md) for name, md in od.metrics.items()
            },
            description=None if od.description is None else str(od.description),
        )


# ===========================================================================
# simulation/prop_distributions.py  ->  PropDistribution, PropDistributionSet
# ===========================================================================


class PropDistributionModel(_ApiModel):
    """Response model for :class:`simulation.prop_distributions.PropDistribution` (SIM-329).

    The full integer-support PMF of one prop for one player.  ``support`` (the
    numpy int64 array of distinct values) and ``probabilities`` (the numpy
    float64 array of their relative frequencies, aligned, summing to 1.0) are
    exposed as JSON-native ``list[int]`` / ``list[float]``.  ``pmf`` is the same
    PMF as a ``{str(value) -> probability}`` map for direct consumer use (JSON
    object keys must be strings, so the integer values are stringified).
    """

    player_id: int
    prop: str
    n: int
    #: The compact PMF support (numpy int64 array -> list[int]).
    support: list[int]
    #: Aligned probabilities (numpy float64 array -> list[float]); sums to 1.0.
    probabilities: list[float]
    mean: float
    median: float
    std: float
    #: ``str(value) -> probability`` view of the PMF (object keys must be str).
    pmf: dict[str, float] = Field(default_factory=dict)

    @classmethod
    def from_dataclass(cls, dist: Any) -> PropDistributionModel:
        """Build from a :class:`simulation.prop_distributions.PropDistribution`."""
        return cls(
            player_id=int(dist.player_id),
            prop=str(dist.prop),
            n=int(dist.n),
            support=[int(v) for v in dist.support.tolist()],
            probabilities=[float(p) for p in dist.probabilities.tolist()],
            mean=float(dist.mean),
            median=float(dist.median),
            std=float(dist.std),
            pmf={str(int(v)): float(p) for v, p in dist.pmf().items()},
        )


class PropDistributionSetModel(_ApiModel):
    """Response model for :class:`simulation.prop_distributions.PropDistributionSet` (SIM-329).

    All prop PMFs for all players over one Monte-Carlo run, indexed
    ``str(player_id) -> {prop_name -> PropDistributionModel}`` (JSON object keys
    must be strings, so the integer player ids are stringified).
    """

    n_iterations: int
    by_player: dict[str, dict[str, PropDistributionModel]] = Field(default_factory=dict)

    @classmethod
    def from_dataclass(cls, dist_set: Any) -> PropDistributionSetModel:
        """Build from a :class:`simulation.prop_distributions.PropDistributionSet`."""
        return cls(
            n_iterations=int(dist_set.n_iterations),
            by_player={
                str(pid): {
                    str(prop): PropDistributionModel.from_dataclass(d) for prop, d in props.items()
                }
                for pid, props in dist_set.by_player.items()
            },
        )


# ===========================================================================
# simulation/linescore.py  ->  InningLine, Linescore  (SIM-362 API exposure)
# ===========================================================================


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


# ===========================================================================
# simulation/pitcher_decisions.py  ->  PitcherDecisions  (SIM-364 API exposure)
# ===========================================================================


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


# ===========================================================================
# SIM-366 -- per-player boxscore-card (prop MEANS over a run's PropDistributionSet)
# ===========================================================================


class BoxscoreCardRowModel(_ApiModel):
    """One player's row in the SIM-366 boxscore card.

    A compact per-player payload exposing that player's prop **means** over a
    Monte-Carlo run -- for a batter the H/HR/RBI/TB means, for a pitcher the
    K/BB/ER/OUTS means -- as a ``{prop_name -> mean}`` map.  Built from a
    :class:`simulation.prop_distributions.PropDistribution` map (one player's
    ``by_player`` entry), reading each :attr:`PropDistribution.mean`.  numpy-free
    (plain floats).
    """

    player_id: int
    #: ``prop_name -> mean`` (e.g. ``{"H": 1.2, "HR": 0.3, "RBI": 0.9, "TB": 2.1}``
    #: for a batter, or ``{"K": 6.4, "BB": 2.1, "ER": 2.8, "OUTS": 17.0}`` for a
    #: pitcher).  Whichever props the player owns in the prop set.
    means: dict[str, float] = Field(default_factory=dict)

    @classmethod
    def from_prop_map(cls, player_id: int, props: Mapping[str, Any]) -> BoxscoreCardRowModel:
        """Build one row from a ``{prop_name -> PropDistribution}`` map."""
        return cls(
            player_id=int(player_id),
            means={str(prop): float(dist.mean) for prop, dist in props.items()},
        )


class BoxscoreCardModel(_ApiModel):
    """Response model for the SIM-366 per-player boxscore-average card.

    The boxscore-card payload for one Monte-Carlo run: every player's prop
    **means** (the 100-iteration boxscore average) keyed
    ``str(player_id) -> BoxscoreCardRowModel`` (JSON object keys must be strings).
    Built from a :class:`simulation.prop_distributions.PropDistributionSet` via
    :meth:`from_prop_set`, iterating ``by_player`` and reading each
    :attr:`PropDistribution.mean` -- so the card is the means-only projection of
    the full PMF set (a consumer that needs the full PMF reads the heavier
    :class:`PropDistributionSetModel` instead).  numpy-free.
    """

    n_iterations: int
    #: ``str(player_id) -> BoxscoreCardRowModel`` of prop means.
    players: dict[str, BoxscoreCardRowModel] = Field(default_factory=dict)

    @classmethod
    def from_prop_set(cls, pset: Any) -> BoxscoreCardModel:
        """Build from a :class:`simulation.prop_distributions.PropDistributionSet`."""
        return cls(
            n_iterations=int(pset.n_iterations),
            players={
                str(pid): BoxscoreCardRowModel.from_prop_map(pid, props)
                for pid, props in pset.by_player.items()
            },
        )


# ===========================================================================
# betting/clv_engine.py  ->  CLV, EdgeReport (+ MarketSide, OddsQuote helpers)
# ===========================================================================


class CLVModel(_ApiModel):
    """Response model for :class:`betting.clv_engine.CLV` (SIM-339).

    Closing Line Value for one side of one market/prop.  ``beat_close`` mirrors
    the source dataclass's derived property (clv_prob > 0).
    """

    entry_fair_prob: float
    close_fair_prob: float
    clv_prob: float
    entry_fair_decimal: float
    close_fair_decimal: float
    clv_decimal: float
    beat_close: bool

    @classmethod
    def from_dataclass(cls, clv: Any) -> CLVModel:
        """Build from a :class:`betting.clv_engine.CLV`."""
        return cls(
            entry_fair_prob=float(clv.entry_fair_prob),
            close_fair_prob=float(clv.close_fair_prob),
            clv_prob=float(clv.clv_prob),
            entry_fair_decimal=float(clv.entry_fair_decimal),
            close_fair_decimal=float(clv.close_fair_decimal),
            clv_decimal=float(clv.clv_decimal),
            beat_close=bool(clv.beat_close),
        )


class EdgeReportModel(_ApiModel):
    """Response model for :class:`betting.clv_engine.EdgeReport` (SIM-339).

    The full edge / EV / CLV report for one side of one market or prop.  ``side``
    is the MarketSide enum's ``.value`` (a JSON-native string, e.g. "home" /
    "over"); ``clv`` is present only when a closing quote was supplied.
    ``positive_edge`` mirrors the source's derived property (edge > 0).
    """

    label: str
    #: The MarketSide enum value as a string ("home"/"away"/"over"/"under").
    side: str
    line: float | None = None

    sim_prob: float
    market_fair_prob: float

    edge: float
    ev: float

    offered_american: float
    sim_fair_american: float

    clv: CLVModel | None = None
    positive_edge: bool

    @classmethod
    def from_dataclass(cls, report: Any) -> EdgeReportModel:
        """Build from a :class:`betting.clv_engine.EdgeReport`."""
        side = report.side
        side_value = side.value if hasattr(side, "value") else str(side)
        return cls(
            label=str(report.label),
            side=str(side_value),
            line=None if report.line is None else float(report.line),
            sim_prob=float(report.sim_prob),
            market_fair_prob=float(report.market_fair_prob),
            edge=float(report.edge),
            ev=float(report.ev),
            offered_american=float(report.offered_american),
            sim_fair_american=float(report.sim_fair_american),
            clv=None if report.clv is None else CLVModel.from_dataclass(report.clv),
            positive_edge=bool(report.positive_edge),
        )


# ===========================================================================
# SIM-386 -- live in-progress game state (sim.lineup_state read path)
# ===========================================================================


class LiveGameStateResponse(_ApiModel):
    """SIM-386 response: live in-progress game state from ``sim.lineup_state``.

    Fields are extracted from the ``game_state`` JSONB blob written by the live
    ingestion pipeline (:class:`~pipeline.live.live_ingestion_pipeline.GameStateBuilder`).
    All fields default to neutral / empty values so a partially-built game_state
    (e.g. a game that just went Live with no pitches yet) never breaks
    deserialization -- the frontend handles sparse state gracefully.

    ``updated_at`` (ISO-8601 string) is the pipeline's last-write timestamp; the
    frontend can use it to detect staleness (> ~30s without an update may mean
    the pipeline lost the WS signal and is polling-fallback).
    """

    game_pk: int
    session_id: str  # UUID of the sim.lineup_state row
    # ---- Inning state -------------------------------------------------------
    inning: int = 1
    half: str = "Top"  # "Top" | "Bottom"
    outs: int = 0
    balls: int = 0
    strikes: int = 0
    # ---- Score --------------------------------------------------------------
    home_score: int = 0
    away_score: int = 0
    # ---- Team context -------------------------------------------------------
    batting_team_id: int | None = None
    fielding_team_id: int | None = None
    # ---- Baserunners --------------------------------------------------------
    on_1b: int | None = None  # player_id of runner on first, or None
    on_2b: int | None = None
    on_3b: int | None = None
    # ---- Current participants -----------------------------------------------
    current_batter_id: int | None = None
    current_pitcher_id: int | None = None
    # ---- Lineups / rosters --------------------------------------------------
    home_lineup: list[int] = Field(default_factory=list)
    away_lineup: list[int] = Field(default_factory=list)
    home_bullpen: list[int] = Field(default_factory=list)
    away_bullpen: list[int] = Field(default_factory=list)
    home_bench: list[int] = Field(default_factory=list)
    away_bench: list[int] = Field(default_factory=list)
    # ---- Staleness ----------------------------------------------------------
    updated_at: str | None = None  # ISO-8601 timestamp of last pipeline write


# ===========================================================================
# SIM-390 -- player-prop edge response (PMF + optional over/under + edge report)
# ===========================================================================


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
    #: The compact PMF support (sorted distinct integer outcomes).
    support: list[int]
    #: Aligned probabilities (sums to 1.0).
    probabilities: list[float]
    mean: float
    median: float
    std: float
    #: ``str(value) -> probability`` view of the PMF (object keys must be str).
    pmf: dict[str, float] = Field(default_factory=dict)
    # Optional: populated when a ``line`` query param is supplied.
    line: float | None = None
    p_over: float | None = None
    p_under: float | None = None
    p_push: float | None = None
    # Optional: populated when ``over_ml`` and ``under_ml`` are also supplied.
    edge_report: EdgeReportModel | None = None


# ===========================================================================
# betting/bet_signal.py  ->  BetSignal  (SIM-369 API exposure)
# ===========================================================================


class BetSignalModel(_ApiModel):
    """Response model for :class:`betting.bet_signal.BetSignal` (SIM-369).

    A fireable +EV bet recommendation derived from one :class:`EdgeReport`.
    ``side`` is the MarketSide enum's ``.value`` (a JSON-native string, e.g.
    "home" / "over"); ``report`` is the full source :class:`EdgeReportModel` (so a
    consumer gets the sim/market probabilities + CLV behind the signal). The list
    a signals endpoint returns is already ranked by EV descending (``rank`` 0 ==
    strongest). numpy-free (plain floats / ints).
    """

    #: Source report's market label ("moneyline" / "total" / "run_line" / "prop:K").
    label: str
    #: The MarketSide enum value as a string ("home"/"away"/"over"/"under").
    side: str
    line: float | None = None
    offered_american: float

    edge: float
    ev: float
    stake_fraction: float
    confidence: float
    rank: int

    #: The full source EdgeReport (sim_prob, market fair prob, sim_fair_american, CLV).
    report: EdgeReportModel

    @classmethod
    def from_dataclass(cls, signal: Any) -> BetSignalModel:
        """Build from a :class:`betting.bet_signal.BetSignal`."""
        side = signal.side
        side_value = side.value if hasattr(side, "value") else str(side)
        return cls(
            label=str(signal.label),
            side=str(side_value),
            line=None if signal.line is None else float(signal.line),
            offered_american=float(signal.offered_american),
            edge=float(signal.edge),
            ev=float(signal.ev),
            stake_fraction=float(signal.stake_fraction),
            confidence=float(signal.confidence),
            rank=int(signal.rank),
            report=EdgeReportModel.from_dataclass(signal.report),
        )


# ===========================================================================
# betting/line_movement.py  ->  LineQuote, LineMovement  (SIM-368 API exposure)
# ===========================================================================


class LineQuoteModel(_ApiModel):
    """Response model for :class:`betting.line_movement.LineQuote` (SIM-368).

    One timestamped quote for a single side of one market at one book -- a point
    on the line-movement time-series. ``fetched_at`` is rendered as a string (a
    datetime is ISO-8601'd via :func:`api.serialization.to_jsonable`, an already-
    string timestamp passes through). ``implied_prob`` is the RAW single-side
    implied probability. numpy-free.
    """

    fetched_at: str | None = None
    line_type: str
    book: str
    is_sharp_book: bool
    american: float
    other_american: float | None = None
    line: float | None = None
    implied_prob: float

    @classmethod
    def from_dataclass(cls, quote: Any) -> LineQuoteModel:
        """Build from a :class:`betting.line_movement.LineQuote`."""
        fa = quote.fetched_at
        fetched_at = None if fa is None else str(to_jsonable(fa))
        return cls(
            fetched_at=fetched_at,
            line_type=str(quote.line_type),
            book=str(quote.book),
            is_sharp_book=bool(quote.is_sharp_book),
            american=float(quote.american),
            other_american=(None if quote.other_american is None else float(quote.other_american)),
            line=None if quote.line is None else float(quote.line),
            implied_prob=float(quote.implied_prob),
        )


class LineMovementModel(_ApiModel):
    """Response model for :class:`betting.line_movement.LineMovement` (SIM-368).

    The line-movement time-series for one (game_pk, market, side, book): the
    ordered :class:`LineQuoteModel` series plus the derived opening->closing
    summary (the American / implied-prob / line deltas, the per-step delta series,
    the running ``implied_prob_series`` plottable surface, the steam ``direction``)
    and the entry-vs-close :class:`CLVModel`. ``side`` is the MarketSide enum's
    ``.value``; ``has_movement`` / ``beat_close`` mirror the source's derived
    properties. numpy-free (plain floats).
    """

    game_pk: int
    market_type: str
    #: The MarketSide enum value as a string ("home"/"away"/"over"/"under").
    side: str
    book: str | None = None

    quotes: list[LineQuoteModel] = Field(default_factory=list)

    opening_american: float | None = None
    closing_american: float | None = None
    opening_implied_prob: float | None = None
    closing_implied_prob: float | None = None

    american_delta: float | None = None
    implied_prob_delta: float | None = None
    line_delta: float | None = None

    step_implied_prob_deltas: list[float] = Field(default_factory=list)
    step_american_deltas: list[float] = Field(default_factory=list)
    implied_prob_series: list[float] = Field(default_factory=list)

    direction: str = "flat"

    clv: CLVModel | None = None
    sharp_consensus: bool | None = None

    #: Derived from the source dataclass's properties.
    has_movement: bool = False
    beat_close: bool = False

    @classmethod
    def from_dataclass(cls, mv: Any) -> LineMovementModel:
        """Build from a :class:`betting.line_movement.LineMovement`."""
        side = mv.side
        side_value = side.value if hasattr(side, "value") else str(side)

        def _of(v: Any) -> float | None:
            return None if v is None else float(v)

        return cls(
            game_pk=int(mv.game_pk),
            market_type=str(mv.market_type),
            side=str(side_value),
            book=None if mv.book is None else str(mv.book),
            quotes=[LineQuoteModel.from_dataclass(q) for q in mv.quotes],
            opening_american=_of(mv.opening_american),
            closing_american=_of(mv.closing_american),
            opening_implied_prob=_of(mv.opening_implied_prob),
            closing_implied_prob=_of(mv.closing_implied_prob),
            american_delta=_of(mv.american_delta),
            implied_prob_delta=_of(mv.implied_prob_delta),
            line_delta=_of(mv.line_delta),
            step_implied_prob_deltas=[float(x) for x in mv.step_implied_prob_deltas],
            step_american_deltas=[float(x) for x in mv.step_american_deltas],
            implied_prob_series=[float(x) for x in mv.implied_prob_series],
            direction=str(mv.direction),
            clv=None if mv.clv is None else CLVModel.from_dataclass(mv.clv),
            sharp_consensus=(None if mv.sharp_consensus is None else bool(mv.sharp_consensus)),
            has_movement=bool(mv.has_movement),
            beat_close=bool(mv.beat_close),
        )


__all__ = [
    # results.py
    "ConfidenceIntervalModel",
    "GameSimSummaryModel",
    "GameSimSummaryLite",
    # sim_loop.py boxscore (re-exported via results.py)
    "PlayerStatLineModel",
    "BoxScoreModel",
    # win_probability.py
    "CalibrationMapModel",
    "WinProbabilityModel",
    # snapshots.py
    "PlayerRefModel",
    "FieldSnapshotModel",
    "PlayByPlayEntryModel",
    "PlayByPlayModel",
    "StateAtPitchModel",
    "MetricDeltaModel",
    "OverrideDeltaModel",
    # prop_distributions.py
    "PropDistributionModel",
    "PropDistributionSetModel",
    # linescore.py (SIM-362)
    "InningLineModel",
    "LinescoreModel",
    # pitcher_decisions.py (SIM-364)
    "PitcherDecisionsModel",
    # SIM-366 boxscore card
    "BoxscoreCardRowModel",
    "BoxscoreCardModel",
    # clv_engine.py
    "CLVModel",
    "EdgeReportModel",
    # SIM-386 live game state
    "LiveGameStateResponse",
    # SIM-390 prop edge
    "PropEdgeResponse",
    # bet_signal.py (SIM-369)
    "BetSignalModel",
    # line_movement.py (SIM-368)
    "LineQuoteModel",
    "LineMovementModel",
]
