"""
api/schemas/snapshots.py
========================
The models of ``simulation.snapshots``: a player reference, the field at a
pitch, the play-by-play and the baseline-versus-override delta.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from api.schemas._base import _ApiModel


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

    # SIM-561: the inning, half, outs and batter BEFORE the pitch; the pitcher
    # who threw it; the score AFTER it.  None on a stream stored without them.
    inning: int | None = None
    half: str | None = None
    outs_before: int | None = None
    batter_id: int | None = None
    pitcher_id: int | None = None
    away_score: int | None = None
    home_score: int | None = None

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
            inning=_opt_int(getattr(entry, "inning", None)),
            half=_opt_str(getattr(entry, "half", None)),
            outs_before=_opt_int(getattr(entry, "outs_before", None)),
            batter_id=_opt_int(getattr(entry, "batter_id", None)),
            pitcher_id=_opt_int(getattr(entry, "pitcher_id", None)),
            away_score=_opt_int(getattr(entry, "away_score", None)),
            home_score=_opt_int(getattr(entry, "home_score", None)),
        )


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


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
    #: SIM-561: the stored run these plays belong to (the newest complete run,
    #: unless the request named one) and the seed it was played at.
    run_id: int | None = None
    base_seed: int | None = None
    #: SIM-561: ``str(player_id) -> name`` for every batter and pitcher in the
    #: returned entries; a placeholder reliever reads "Generic reliever N".
    names: dict[str, str] = Field(default_factory=dict)

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
