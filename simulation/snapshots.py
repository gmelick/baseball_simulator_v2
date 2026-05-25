"""
snapshots.py
============
SIM-331 -- field/baserunner state + pitch-level play-by-play snapshot contracts
(Phase 4, Sprint 3).

The Phase-6 frontend and the Phase-5 API need typed data shapes for the visual +
replay surface the simulator drives.  This module owns those contracts and the
pure builders that derive them from existing loop state -- a SPEC/dataclass
deliverable, NOT UI and NOT live API endpoints.

Four contracts map 1:1 onto the UX-Designer (Agent 7) deliverables and the
Backend (Agent 5) endpoint list:

  * FieldSnapshot      -- the BaseballFieldGraphic state (9 defensive positions +
                          batter + baserunners + count/outs/inning/half/score).
                          Built via FieldSnapshot.from_game_state(state).
  * PlayByPlayEntry +  -- GET /api/games/{game_pk}/plays: one entry per pitch
    PlayByPlay            (resolved PA event on the terminal pitch).  Built via
                          PlayByPlay.from_play_results(results).
  * StateAtPitch       -- GET /api/games/{game_pk}/state/{at_bat}/{pitch}: a
                          point-in-time FieldSnapshot tagged with at_bat/pitch.
  * OverrideDelta      -- POST .../simulate/with_override: a baseline-vs-override
                          comparison.  Built via OverrideDelta.from_summaries(...).

DESIGN
------
  * Additive + pure.  Every builder is a classmethod/staticmethod reading
    existing GameState / PlayResult / GameSimSummary objects.  No DB, no FAISS,
    no live API, no rng, no mutation.  sim_loop.py is NOT touched (import only).
  * Player labels optional + injectable.  The loop carries integer ids; the name
    lookup is a Phase-5 join.  Builders accept labels: Mapping[int, str]; with no
    map the label falls back to "#<id>" so contracts render standalone in tests.
  * One entry per pitch, terminal event on the last pitch.  The PA's resolved
    event lives on the terminal pitch (pa_terminal / is_pa_end); entries group
    into PAs by an at_bat index so the scroll collapses a PA / expands to pitches.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from simulation.game_state import GameState, Half, PlayResult

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: The 9 defensive positions in canonical scorebook order (1=P ... 9=RF) -- the
#: order the BaseballFieldGraphic SVG renders.
DEFENSE_POSITIONS: tuple[str, ...] = (
    "P",
    "C",
    "1B",
    "2B",
    "3B",
    "SS",
    "LF",
    "CF",
    "RF",
)
#: Scorebook number for each position (1-indexed).
POSITION_NUMBER: dict[str, int] = {p: i + 1 for i, p in enumerate(DEFENSE_POSITIONS)}

#: The three base labels the baserunner snapshot keys on.
BASE_LABELS: tuple[str, ...] = ("1B", "2B", "3B")

#: The metric attribute names OverrideDelta compares by default, in display
#: order.  Module-level (NOT a class attr) so it coexists with the slots=True
#: dataclass without becoming a slot descriptor.
OVERRIDE_METRIC_FIELDS: tuple[str, ...] = (
    "home_win_pct",
    "away_win_pct",
    "home_score_mean",
    "away_score_mean",
    "total_score_mean",
)


def _label_for(player_id: int | None, labels: Mapping[int, str] | None) -> str | None:
    """Resolve a display label for a player id (None for an empty slot)."""
    if player_id is None:
        return None
    if labels is not None and int(player_id) in labels:
        return labels[int(player_id)]
    return f"#{int(player_id)}"


# ---------------------------------------------------------------------------
# PlayerRef -- an id + optional display label
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlayerRef:
    """A player reference: the loop's integer id + an optional display label."""

    player_id: int
    label: str | None = None

    @classmethod
    def of(cls, player_id: int | None, labels: Mapping[int, str] | None = None) -> PlayerRef | None:
        """Build a PlayerRef for player_id (or None if the slot is empty)."""
        if player_id is None:
            return None
        return cls(player_id=int(player_id), label=_label_for(player_id, labels))


# ---------------------------------------------------------------------------
# FieldSnapshot -- the BaseballFieldGraphic state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldSnapshot:
    """The BaseballFieldGraphic state: defense + batter + baserunners + chrome."""

    #: Keyed by DEFENSE_POSITIONS ("P", "C", "1B", ... "RF"); None == unassigned.
    positions: dict[str, PlayerRef | None]

    batter: PlayerRef | None
    #: Baserunners keyed by base label ("1B"/"2B"/"3B"); None == empty bag.
    baserunners: dict[str, PlayerRef | None]

    balls: int
    strikes: int
    outs: int
    inning: int
    half: str  # "top" / "bottom"
    home_score: int
    away_score: int
    #: Base-occupancy bitmask (RE24 / run_resolution encoding).
    runners_state: int = 0

    @property
    def occupied_bases(self) -> tuple[str, ...]:
        """The occupied base labels, 1B->3B order."""
        return tuple(b for b in BASE_LABELS if self.baserunners.get(b) is not None)

    @property
    def runners_on(self) -> int:
        """How many runners are on base (0-3)."""
        return len(self.occupied_bases)

    @classmethod
    def from_game_state(
        cls,
        state: GameState,
        *,
        labels: Mapping[int, str] | None = None,
        defense_positions: Mapping[str, int] | None = None,
    ) -> FieldSnapshot:
        """Build a FieldSnapshot from a live GameState.

        labels resolves ids to display names.  defense_positions is an optional
        {position: player_id} map for the fielding side; when omitted the 9 slots
        are present but empty (the loop does not yet track per-position fielders).
        """
        positions: dict[str, PlayerRef | None] = {}
        for pos in DEFENSE_POSITIONS:
            pid = None
            if defense_positions is not None:
                pid = defense_positions.get(pos)
            positions[pos] = PlayerRef.of(pid, labels)

        baserunners: dict[str, PlayerRef | None] = {
            "1B": PlayerRef.of(state.bases.first, labels),
            "2B": PlayerRef.of(state.bases.second, labels),
            "3B": PlayerRef.of(state.bases.third, labels),
        }

        return cls(
            positions=positions,
            batter=PlayerRef.of(state.batter_id, labels),
            baserunners=baserunners,
            balls=int(state.balls),
            strikes=int(state.strikes),
            outs=int(state.outs),
            inning=int(state.inning),
            half="top" if state.half == Half.TOP else "bottom",
            home_score=int(state.home_score),
            away_score=int(state.away_score),
            runners_state=int(state.bases.runners_state),
        )


# ---------------------------------------------------------------------------
# PlayByPlayEntry + PlayByPlay -- the /plays scroll, pitch-level
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlayByPlayEntry:
    """One pitch in the play-by-play scroll (GET .../plays), pitch-level.

    at_bat groups pitches into a PA; pitch is the 1-based pitch number within the
    PA; sequence is the global 0-based pitch index across the game.  The PA's
    resolved event is carried on the terminal pitch (is_pa_end).
    """

    sequence: int  # global 0-based pitch index
    at_bat: int  # 0-based plate-appearance index
    pitch: int  # 1-based pitch number WITHIN the PA

    pitch_outcome: str  # ball / called_strike / swinging_strike / foul / in_play
    is_contact: bool
    is_pa_end: bool

    event: str | None = None  # "single" / "strikeout" / "home_run" / ...

    runs_scored: int = 0
    outs_recorded: int = 0

    exit_velo: float | None = None
    launch_angle: float | None = None
    spray_angle: float | None = None

    runs: float = 0.0
    canonical_event: str | None = None

    @classmethod
    def from_play_result(
        cls,
        result: PlayResult,
        *,
        sequence: int,
        at_bat: int,
        pitch: int,
    ) -> PlayByPlayEntry:
        """Build one entry from a PlayResult + its position indices."""
        return cls(
            sequence=int(sequence),
            at_bat=int(at_bat),
            pitch=int(pitch),
            pitch_outcome=result.pitch_outcome,
            is_contact=bool(result.is_contact),
            is_pa_end=bool(result.pa_terminal),
            event=result.event,
            runs_scored=int(result.runs_scored),
            outs_recorded=int(result.outs_recorded),
            exit_velo=result.exit_velo,
            launch_angle=result.launch_angle,
            spray_angle=result.spray_angle,
            runs=float(result.runs),
            canonical_event=result.canonical_event,
        )


@dataclass(frozen=True, slots=True)
class PlayByPlay:
    """The GET /api/games/{game_pk}/plays collection -- pitch-level entries."""

    entries: list[PlayByPlayEntry] = field(default_factory=list)

    @property
    def n_pitches(self) -> int:
        """Total pitches in the play-by-play."""
        return len(self.entries)

    @property
    def n_plate_appearances(self) -> int:
        """Distinct plate appearances (count of terminal pitches)."""
        return sum(1 for e in self.entries if e.is_pa_end)

    def pitches_for_at_bat(self, at_bat: int) -> list[PlayByPlayEntry]:
        """All pitch entries belonging to plate-appearance at_bat (ordered)."""
        return [e for e in self.entries if e.at_bat == int(at_bat)]

    @property
    def plate_appearances(self) -> list[list[PlayByPlayEntry]]:
        """The entries grouped into PAs (one inner list per at_bat, ordered)."""
        grouped: dict[int, list[PlayByPlayEntry]] = {}
        for e in self.entries:
            grouped.setdefault(e.at_bat, []).append(e)
        return [grouped[k] for k in sorted(grouped)]

    @classmethod
    def from_play_results(
        cls,
        results: Sequence[PlayResult],
    ) -> PlayByPlay:
        """Build a PlayByPlay from a flat, ordered sequence of pitches.

        PAs are inferred from the pa_terminal flag: a new PA begins after each
        terminal pitch, and the within-PA pitch counter resets to 1.
        """
        entries: list[PlayByPlayEntry] = []
        at_bat = 0
        pitch_in_pa = 0
        for seq, res in enumerate(results):
            pitch_in_pa += 1
            entries.append(
                PlayByPlayEntry.from_play_result(
                    res, sequence=seq, at_bat=at_bat, pitch=pitch_in_pa
                )
            )
            if res.pa_terminal:
                at_bat += 1
                pitch_in_pa = 0
        return cls(entries=entries)


# ---------------------------------------------------------------------------
# StateAtPitch -- the /state/{at_bat}/{pitch} point-in-time lookup
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StateAtPitch:
    """The GET /api/games/{game_pk}/state/{at_bat}/{pitch} contract.

    A point-in-time snapshot: the FieldSnapshot as of a given at-bat/pitch index,
    tagged with those indices and the global pitch sequence (if known).
    """

    at_bat: int  # 0-based plate-appearance index this is as-of
    pitch: int  # 1-based pitch number within the PA (0 == pre-PA)
    field: FieldSnapshot  # the BaseballFieldGraphic state at that point
    sequence: int | None = None  # global pitch index, if known

    @classmethod
    def from_game_state(
        cls,
        state: GameState,
        *,
        at_bat: int,
        pitch: int,
        sequence: int | None = None,
        labels: Mapping[int, str] | None = None,
        defense_positions: Mapping[str, int] | None = None,
    ) -> StateAtPitch:
        """Build a StateAtPitch from the GameState as of (at_bat, pitch)."""
        snap = FieldSnapshot.from_game_state(
            state, labels=labels, defense_positions=defense_positions
        )
        return cls(
            at_bat=int(at_bat),
            pitch=int(pitch),
            field=snap,
            sequence=None if sequence is None else int(sequence),
        )


# ---------------------------------------------------------------------------
# OverrideDelta -- baseline-vs-override comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetricDelta:
    """A single baseline-vs-override metric: baseline, override, and the delta."""

    metric: str
    baseline: float
    override: float

    @property
    def delta(self) -> float:
        """override - baseline (positive == the override raised the metric)."""
        return self.override - self.baseline


@dataclass(frozen=True, slots=True)
class OverrideDelta:
    """The POST .../simulate/with_override baseline-vs-override comparison.

    Build from two GameSimSummary objects (or any object exposing the same
    attribute names) via from_summaries.  Each tracked metric is a MetricDelta;
    metrics keys them by name.  description optionally records the roster change.
    """

    metrics: dict[str, MetricDelta]
    description: str | None = None

    def delta(self, metric: str) -> float:
        """The override - baseline delta for metric (KeyError if absent)."""
        return self.metrics[metric].delta

    @property
    def home_win_pct_delta(self) -> float:
        """Convenience: the change in P(home win) from the override."""
        return self.delta("home_win_pct")

    @classmethod
    def from_summaries(
        cls,
        baseline: Any,
        override: Any,
        *,
        description: str | None = None,
        metrics: Iterable[str] | None = None,
    ) -> OverrideDelta:
        """Build an OverrideDelta from two summaries.

        baseline / override are any objects exposing the metric attributes (e.g.
        two GameSimSummary objects).  metrics overrides the default tracked field
        set (OVERRIDE_METRIC_FIELDS).  Pure: reads attributes only.
        """
        fields_to_track = tuple(metrics) if metrics is not None else OVERRIDE_METRIC_FIELDS
        out: dict[str, MetricDelta] = {}
        for name in fields_to_track:
            b = float(getattr(baseline, name))
            o = float(getattr(override, name))
            out[name] = MetricDelta(metric=name, baseline=b, override=o)
        return cls(metrics=out, description=description)


__all__ = [
    "DEFENSE_POSITIONS",
    "POSITION_NUMBER",
    "BASE_LABELS",
    "OVERRIDE_METRIC_FIELDS",
    "PlayerRef",
    "FieldSnapshot",
    "PlayByPlayEntry",
    "PlayByPlay",
    "StateAtPitch",
    "OverrideDelta",
    "MetricDelta",
]
