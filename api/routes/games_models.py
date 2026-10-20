"""
api/routes/games_models.py
==========================
The request and response models of the game routes (``/api/games``).

Each model is part of the API contract: its docstring and its fields appear in
the OpenAPI schema (``frontend/openapi.json``), so an edit to either one is a
contract change. :func:`_sim_summary_lite_from_stored` turns a stored summary
into the lite model that a slate card, the status card and a run row carry.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from api.schemas import (
    GameSimSummaryLite,
    GameSimSummaryModel,
    LinescoreModel,
    OverrideDeltaModel,
    PitcherDecisionsModel,
)

# ---------------------------------------------------------------------------
# The slate and the status card
# ---------------------------------------------------------------------------


class GameCard(BaseModel):
    """One row of ``GET /api/games/{date}`` -- a scheduled game's identity.

    SIM-383 enrichment adds team names, abbreviations, venue name + city, and
    season-to-date win/loss records (computed through the day before ``game_date``
    from ``raw.games`` final results).  All enrichment fields are ``Optional``
    so existing cached payloads and unit-test stubs that only supply the raw
    ID columns continue to deserialise without error.
    """

    game_pk: int
    season: int
    game_date: str
    status: str | None = None
    home_team_id: int | None = None
    away_team_id: int | None = None
    venue_id: int | None = None
    # The names, the venue and the records
    home_team_name: str | None = None
    home_team_abbrev: str | None = None
    away_team_name: str | None = None
    away_team_abbrev: str | None = None
    venue_name: str | None = None
    venue_city: str | None = None
    # The season records entering the game (every final game before this
    # game's date); 0-0 when there is none.
    home_wins: int | None = None
    home_losses: int | None = None
    away_wins: int | None = None
    away_losses: int | None = None
    # True when raw.game_lineups holds a row for the game; False for a
    # scheduled game whose lineup is not published yet; None when the caller
    # made no lineup check.
    lineup_ready: bool | None = None
    # The schedule's view of the game. Every field is optional, so a stored
    # payload, a unit stub or an old mock still deserialises.
    game_status: Literal["scheduled", "live", "final", "postponed"] | None = None
    detailed_state: str | None = None
    reason: str | None = None
    start_utc: str | None = None
    start_time_tbd: bool | None = None
    double_header: str | None = None
    game_number: int | None = None
    game_type: str | None = None
    series_description: str | None = None
    away_score: int | None = None
    home_score: int | None = None
    inning: int | None = None
    inning_half: str | None = None
    outs: int | None = None
    n_innings: int | None = None
    away_probable_pitcher_id: int | None = None
    away_probable_pitcher_name: str | None = None
    home_probable_pitcher_id: int | None = None
    home_probable_pitcher_name: str | None = None
    rescheduled_to: str | None = None
    rescheduled_from: str | None = None
    lineup_source: str | None = None
    sim_summary: GameSimSummaryLite | None = None
    sim_run_at: str | None = None
    db_known: bool | None = None


class GamesOnDateResponse(BaseModel):
    """The ``GET /api/games/{date}`` envelope: the date echo + its game cards.

    SIM-519 Part A: ``source`` says where the list came from -- ``schedule``
    (the league's schedule, fresh or from the 20-second cache),
    ``schedule_cached`` (the last good schedule after a feed error) or ``db``
    (the stored listing). ``feed_error`` carries the error on a degraded read.
    """

    date: str
    count: int
    games: list[GameCard] = Field(default_factory=list)
    source: Literal["schedule", "schedule_cached", "db"] | None = None
    feed_error: str | None = None
    fetched_at: str | None = None


class GameCardAggregateResponse(BaseModel):
    """Response for ``GET /api/games/{game_pk}/card`` (SIM-384).

    Aggregates the identity, status, SIM-383 enrichment, and the most recent
    persisted Monte-Carlo summary into a single payload for the Day Summary
    3-state game cards.  ``sim_summary`` is ``None`` when no sim has been run
    yet (or when Postgres is unavailable).  ``odds`` is reserved for Phase 6
    Sprint 4+ (odds provider integration — SIM-405).
    """

    game_pk: int
    game_status: str  # one of GameStatus values
    game_date: str
    season: int
    # The names, the venue and the records (None when the join finds no row)
    home_team_id: int | None = None
    away_team_id: int | None = None
    venue_id: int | None = None
    home_team_name: str | None = None
    home_team_abbrev: str | None = None
    away_team_name: str | None = None
    away_team_abbrev: str | None = None
    venue_name: str | None = None
    venue_city: str | None = None
    home_wins: int | None = None
    home_losses: int | None = None
    away_wins: int | None = None
    away_losses: int | None = None
    # The final score (set when game_status is "final")
    home_score_final: int | None = None
    away_score_final: int | None = None
    # The newest stored Monte-Carlo summary (None before the first run)
    sim_summary: GameSimSummaryLite | None = None
    # Always None: the slate card's lines come from /api/betting/.../card-odds
    odds: None = None
    # The schedule's fields for the game page header (None when the league
    # feed has no entry for the game).
    detailed_state: str | None = None
    start_utc: str | None = None
    start_time_tbd: bool | None = None
    game_number: int | None = None
    double_header: str | None = None
    series_description: str | None = None
    away_probable_pitcher_name: str | None = None
    home_probable_pitcher_name: str | None = None
    away_score: int | None = None
    home_score: int | None = None


# ---------------------------------------------------------------------------
# The real game: the league feed's card, play-by-play and plate-appearance state
# ---------------------------------------------------------------------------


class FeedPerson(BaseModel):
    id: int
    name: str


class FeedPitcher(FeedPerson):
    np: int | None = None


class FeedInning(BaseModel):
    num: int | None = None
    away: int | None = None
    home: int | None = None


class FeedTotals(BaseModel):
    runs: int | None = None
    hits: int | None = None
    errors: int | None = None


class FeedLinescore(BaseModel):
    innings: list[FeedInning] = Field(default_factory=list)
    away: FeedTotals = Field(default_factory=FeedTotals)
    home: FeedTotals = Field(default_factory=FeedTotals)
    home_did_not_bat_last: bool = False
    current_inning: int | None = None
    inning_half: str | None = None


class FeedLineupSlot(BaseModel):
    order: int
    id: int | None = None
    name: str
    pos: str | None = None


class FeedLineups(BaseModel):
    away: list[FeedLineupSlot] = Field(default_factory=list)
    home: list[FeedLineupSlot] = Field(default_factory=list)
    away_probable_pitcher: FeedPerson | None = None
    home_probable_pitcher: FeedPerson | None = None


class FeedBatterLine(BaseModel):
    id: int | None = None
    name: str
    pos: str | None = None
    batting_order: int | None = None
    is_sub: bool = False
    ab: int | None = None
    r: int | None = None
    h: int | None = None
    rbi: int | None = None
    bb: int | None = None
    k: int | None = None
    hr: int | None = None
    avg: str | None = None


class FeedPitcherLine(BaseModel):
    id: int | None = None
    name: str
    outs: int | None = None
    ip: str | None = None
    h: int | None = None
    r: int | None = None
    er: int | None = None
    bb: int | None = None
    k: int | None = None
    np: int | None = None
    era: str | None = None


class FeedBoxSide(BaseModel):
    batters: list[FeedBatterLine] = Field(default_factory=list)
    pitchers: list[FeedPitcherLine] = Field(default_factory=list)


class FeedBox(BaseModel):
    away: FeedBoxSide = Field(default_factory=FeedBoxSide)
    home: FeedBoxSide = Field(default_factory=FeedBoxSide)


class FeedRunners(BaseModel):
    first: FeedPerson | None = None
    second: FeedPerson | None = None
    third: FeedPerson | None = None


class FeedLive(BaseModel):
    balls: int | None = None
    strikes: int | None = None
    outs: int | None = None
    offense: Literal["away", "home"] | None = None
    runners: FeedRunners = Field(default_factory=FeedRunners)
    batter: FeedPerson | None = None
    pitcher: FeedPitcher | None = None
    fielders: dict[str, str | None] = Field(default_factory=dict)
    last_play: str | None = None


class GameFeedCardModel(BaseModel):
    """``GET /api/games/{game_pk}/feed``: the real game behind a slate card.

    ``linescore`` and ``box`` are set on a live or final game; ``live`` on a
    live game only; ``lineups`` carries the posted batting orders (empty until
    the league posts them) and the probable pitchers. ``source`` is ``feed``
    (fresh or cached) or ``feed_cached`` (the last good copy after an error).
    """

    game_pk: int | None = None
    status: Literal["scheduled", "live", "final", "postponed"]
    detailed_state: str | None = None
    linescore: FeedLinescore | None = None
    lineups: FeedLineups = Field(default_factory=FeedLineups)
    box: FeedBox | None = None
    live: FeedLive | None = None
    source: Literal["feed", "feed_cached"] = "feed"
    feed_error: str | None = None


class RealPlayRunners(BaseModel):
    first: int | None = None
    second: int | None = None
    third: int | None = None


class RealPitch(BaseModel):
    call: str | None = None
    type: str | None = None
    speed: float | None = None


class RealPlay(BaseModel):
    at_bat: int
    inning: int
    half: Literal["top", "bottom"]
    batter_id: int | None = None
    pitcher_id: int | None = None
    event: str | None = None
    event_type: str | None = None
    description: str | None = None
    rbi: int = 0
    batter_finished: bool = True
    outs_before: int = 0
    outs_after: int = 0
    away_score_before: int = 0
    home_score_before: int = 0
    away_score_after: int = 0
    home_score_after: int = 0
    runners: RealPlayRunners = Field(default_factory=RealPlayRunners)
    pitches: list[RealPitch] = Field(default_factory=list)
    is_complete: bool = True


class RealPlaysModel(BaseModel):
    """``GET /api/games/{game_pk}/feed/plays``: the real game's plate appearances."""

    game_pk: int
    status: Literal["scheduled", "live", "final", "postponed"]
    plays: list[RealPlay] = Field(default_factory=list)
    names: dict[str, str] = Field(default_factory=dict)
    source: Literal["feed", "feed_cached"] = "feed"


class PlayerOption(BaseModel):
    id: int
    name: str
    position: str | None = None
    bats: str | None = None
    throws: str | None = None


class LineupSpot(BaseModel):
    slot: int
    id: int
    name: str
    position: str | None = None


class SideState(BaseModel):
    lineup: list[LineupSpot]
    pitcher: PlayerOption
    defense: dict[str, PlayerOption]
    bench: list[PlayerOption] = Field(default_factory=list)
    bullpen: list[PlayerOption] = Field(default_factory=list)
    used: list[PlayerOption] = Field(default_factory=list)


class PaStateModel(BaseModel):
    """``GET /api/games/{game_pk}/feed/state``: the game at the start of a plate
    appearance (``at_bat`` omitted = first pitch), for the what-if panel. ``live``
    has the shape of the slate's field, so the page draws it with ``CardField``."""

    game_pk: int
    at_bat: int | None = None
    inning: int
    half: Literal["top", "bottom"]
    outs: int
    away_score: int
    home_score: int
    batting_side: Literal["away", "home"]
    live: FeedLive
    away: SideState
    home: SideState


# ---------------------------------------------------------------------------
# The simulation routes
# ---------------------------------------------------------------------------


class SimulateResponse(BaseModel):
    """The ``GET /api/games/{game_pk}/simulate`` envelope."""

    game_pk: int
    n_iterations: int
    base_seed: int | None = None
    from_cache: bool = False
    summary: GameSimSummaryModel


class SampleGameResponse(BaseModel):
    """The ``POST /api/games/{game_pk}/sample-game`` envelope (SIM-561).

    The stored game's run id and seed.  The page then reads the game through
    /linescore, /decisions and /plays, which serve this run until a newer one.
    """

    game_pk: int
    run_id: int
    base_seed: int


class SubstitutionSlot(BaseModel):
    """A single targeted player substitution (SIM-388).

    Replaces the player at a specific batting-order slot on one side of the
    lineup, without having to know or pass the other 8 slots.  One or more of
    these can appear in :attr:`RosterOverride.substitutions` to stage a
    multi-sub override queue.

      * ``batting_order`` -- 1-indexed slot (1 = leadoff … 9 = ninth).
      * ``player_id``     -- the replacement player's MLB player ID.
      * ``side``          -- ``"home"`` or ``"away"``.

    Multiple ``SubstitutionSlot`` items for the SAME (side, batting_order) in
    a single request are applied left-to-right, so the last one wins.
    """

    batting_order: int = Field(..., ge=1, le=9, description="1-indexed lineup slot (1–9)")
    player_id: int = Field(..., gt=0, description="MLB player_id of the substitute")
    side: Literal["home", "away"] = Field(..., description="Which side's lineup to modify")


class RosterOverride(BaseModel):
    """The ``POST .../simulate/with_override`` request body (SIM-358 + SIM-388).

    Every field is OPTIONAL; an unset field keeps the resolved (baseline) value.
    At least one field SHOULD be set for the override to differ from baseline,
    but an all-empty body is accepted (it yields an all-zero delta -- a valid,
    if uninteresting, comparison).

      * ``home_lineup`` / ``away_lineup`` -- full 1..9 batting orders (player
        ids) to swap in for that side.  When set, REPLACES the entire lineup
        before any ``substitutions`` are applied.
      * ``substitutions`` -- SIM-388: a list of :class:`SubstitutionSlot`
        targeted single-player changes applied AFTER any full-lineup replacements.
        Use this to stage individual subs without knowing the full batting order.
        Left-to-right application; same-slot conflicts → last entry wins.
      * ``pitcher_id`` -- the pitcher to face the batters for the simulated half.
      * ``bat_hand`` -- the leadoff batter's hand override ('L'/'R'/'S'); the
        sampler pre-filter key.
      * ``description`` -- free text recorded on the returned OverrideDelta.
    """

    home_lineup: list[int] | None = None
    away_lineup: list[int] | None = None
    # The single-player substitutions, applied after any full lineup
    substitutions: list[SubstitutionSlot] | None = None
    pitcher_id: int | None = None
    bat_hand: str | None = Field(default=None, max_length=1)
    description: str | None = None


class WithOverrideResponse(BaseModel):
    """The ``POST .../simulate/with_override`` envelope (SIM-358).

    Carries the BASELINE summary, the OVERRIDE summary (both run at the SAME
    base seed for an apples-to-apples comparison), and the OverrideDelta diff.
    """

    game_pk: int
    n_iterations: int
    base_seed: int | None = None
    baseline: GameSimSummaryModel
    override: GameSimSummaryModel
    delta: OverrideDeltaModel


class GameCardResponse(BaseModel):
    """The combined ``GET /api/games/{game_pk}/card`` envelope (SIM-362/364).

    Carries both derived loop outputs for the game's most-recent persisted run:
    the per-inning :class:`~api.schemas.LinescoreModel` (R/H/E grid) and the
    :class:`~api.schemas.PitcherDecisionsModel` (W/L/Save).
    """

    game_pk: int
    linescore: LinescoreModel
    decisions: PitcherDecisionsModel
    #: The stored run and its seed; /plays?run_id= reads the same game.
    run_id: int | None = None
    base_seed: int | None = None


# ---------------------------------------------------------------------------
# The run jobs and the what-if
# ---------------------------------------------------------------------------


class SimRunRequest(BaseModel):
    """``POST /api/games/{game_pk}/simulate`` body."""

    n_iterations: int = Field(100, ge=1, le=10000)
    base_seed: int | None = None


class SimRunModel(BaseModel):
    """One run job (a ``sim.sim_runs`` row)."""

    run_id: int
    game_pk: int
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    n_iterations: int
    progress_done: int = 0
    base_seed: int | None = None
    position_in_queue: int | None = None
    existing: bool = False
    requested_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    lineup_source: str | None = None
    bullpen_source: str | None = None
    replay_run_id: int | None = None
    summary: GameSimSummaryLite | None = None


class WhatIfChanges(BaseModel):
    pinch_hit: list[dict[str, Any]] = Field(default_factory=list)
    pitcher: dict[str, Any] | None = None
    pinch_run: list[dict[str, Any]] = Field(default_factory=list)
    defense: list[dict[str, Any]] = Field(default_factory=list)


class WhatIfRequest(BaseModel):
    at_bat: int | None = Field(None, ge=0)
    changes: WhatIfChanges = Field(default_factory=WhatIfChanges)
    n_iterations: int = Field(100, ge=10, le=1000)


class WhatIfStarted(BaseModel):
    game_pk: int
    at_bat: int | None = None
    base_run_id: int
    change_run_id: int
    base_seed: int
    changes: list[str] = Field(default_factory=list)


class WhatIfResult(BaseModel):
    game_pk: int
    base: SimRunModel
    change: SimRunModel
    real_final: dict[str, int] | None = None
    start_score: dict[str, int] | None = None


# ---------------------------------------------------------------------------
# A stored summary as the lite model
# ---------------------------------------------------------------------------


def _with_half_width(ci: Any) -> Any:
    """A stored interval dict with ``half_width`` filled from its bounds.

    The stored JSON lacks the field (:func:`_sim_summary_lite_from_stored`). A
    value that is not such a dict passes through unchanged, and the model then
    rejects it.
    """
    if (
        isinstance(ci, dict)
        and "half_width" not in ci
        and isinstance(ci.get("low"), (int, float))
        and isinstance(ci.get("high"), (int, float))
    ):
        return {**ci, "half_width": (ci["high"] - ci["low"]) / 2.0}
    return ci


def _sim_summary_lite_from_stored(summary: dict | None) -> GameSimSummaryLite | None:
    """Build a ``GameSimSummaryLite`` from a stored (JSONB) summary dict.

    The function drops the three per-iteration score arrays (``home_scores``,
    ``away_scores``, ``total_scores``) and the per-iteration inning grids
    (``inning_grids``) before ``model_validate``: the lite model has no such
    fields and forbids extra ones. It returns ``None`` on any failure (a
    missing key, a wrong type, an unknown format), so a caller never sees an
    exception from a history read.

    The stored summary has no ``half_width`` on its intervals: ``half_width``
    is a property of the interval dataclass, and ``to_jsonable`` writes fields
    only. The lite model requires it, so the function fills it from
    ``(high - low) / 2`` on each ``*_ci`` dict that lacks it.
    """
    if not summary:
        return None
    try:
        lite_dict = {
            k: _with_half_width(v) if k.endswith("_ci") else v
            for k, v in summary.items()
            if k not in ("home_scores", "away_scores", "total_scores", "inning_grids")
        }
        return GameSimSummaryLite.model_validate(lite_dict)
    except Exception:  # noqa: BLE001 -- an unreadable summary reads as None
        return None
