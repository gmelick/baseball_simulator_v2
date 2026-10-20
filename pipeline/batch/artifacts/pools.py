"""The in-memory pools the sampler draws from, and the arrays of each that the
parent process shares with the workers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np

log = logging.getLogger("engine_artifacts")


# ============================================================================
# Per-worker LOADER (the read side; fork-safe — everything comes off disk)
# ============================================================================


@dataclass
class HandPool:
    """One batter-hand's resident candidate pool for full-pool scoring (SIM-423)."""

    geom: np.ndarray  # (N, 10) float32 — raw geometry (kernel applied at sample time)
    sit: np.ndarray  # (N, 6)  float32 — situation (balls,strikes,outs,runners,inning,score_diff)
    pitcher_id: np.ndarray  # (N,) int64
    batter_id: np.ndarray  # (N,) int64
    season: np.ndarray  # (N,) int64 — for the (pitcher_id:season) pitcher-sim key
    outcome_type: np.ndarray  # (N,) object — ball/called_strike/swinging_strike/foul/in_play
    recency: np.ndarray  # (N,) float32
    # SIM-517 (migration 0022): the receiving context — None on a pre-0022
    # bundle, exactly like the batted-ball realism columns.
    catcher_id: np.ndarray | None = None  # (N,) int64 (0 when unknown)
    got_away: np.ndarray | None = None  # (N,) int8 (0/1 — PB/WP incl. uncaught K3)
    # SIM-518 (migration 0023): the draw-conditioning columns — None on a
    # pre-0023 bundle. Unknown rows read bat_home -1, pitch_count -1, tto 0,
    # and every consumer is exactly neutral on them.
    bat_home: np.ndarray | None = None  # (N,) int8 (1 = the home team bats)
    pitch_count: np.ndarray | None = None  # (N,) int16 (pitches BEFORE this PA)
    tto: np.ndarray | None = None  # (N,) int8 (times through the order, >= 1)
    # SIM-523 part B: the pitch-id JOIN to the batted-ball pool — the row
    # of ``bb_pools[hand]`` that holds this in-play pitch's own batted
    # ball (-1: not in play, or no batted-ball row). None on a bundle
    # exported before part B; the born batted ball is then unavailable.
    bb_row: np.ndarray | None = None  # (N,) int32
    # SIM-523 part E: the Statcast zone (1-9 in the zone, 11-14 outside; 0
    # unknown) — the receiving ratio's zone group. None on an older bundle.
    zone: np.ndarray | None = None  # (N,) int8

    @property
    def n(self) -> int:
        return int(self.geom.shape[0])

    #: SIM-535: the row's game date as YYYYMMDD. None on a bundle exported
    #: before this existed, in which case the sampler cannot apply a cutoff and
    #: says so rather than silently drawing from the future.
    game_ymd: np.ndarray | None = None  # (N,) int32


@dataclass
class BattedBallPool:
    """One batter-hand's resident batted-ball pool for SIM-425 (step 5/6).

    SIM-411/413/425b: the four trailing fields are the per-row realism facts the
    migration-0012 outcome-pool rebuild bakes into the meta parquet. They are
    OPTIONAL (``None`` on a legacy bundle built before 0012) so the sampler runs
    unchanged on the old artifact and each consumer falls back to a neutral
    factor until the rebuild lands:

      * ``p_throws``   — SIM-413: the pitcher's throwing hand for this batted ball
        ('L'/'R', object), so the batted-ball draw can reweight toward the live
        same/opposite-hand matchup.
      * ``venue_id``   — SIM-411: the park, for the run-environment multiplier
        (joins ``derived.park_factors``).
      * ``fielder_pos``— SIM-425b: the position 1–9 that fielded the ball.
      * ``fielder_id`` — SIM-425b: the player who fielded it, so the resolver can
        read that fielder's defensive quality and nudge out/hit/error RELATIVE to
        the current defender at the same position.
    """

    geom: np.ndarray  # (N, 3) float32 — exit_velo, launch_angle, spray
    sit: np.ndarray  # (N, 6)  float32
    batter_id: np.ndarray  # (N,) int64
    season: np.ndarray  # (N,) int64
    event: np.ndarray  # (N,) object
    result_hits: np.ndarray  # (N,) int8 (0=out,1=1B,2=2B,3=3B,4=HR)
    result_outs: np.ndarray  # (N,) int8
    recency: np.ndarray  # (N,) float32
    # SIM-411/413/425b realism facts (None on a pre-0012 bundle).
    p_throws: np.ndarray | None = None  # (N,) object 'L'/'R' — SIM-413 platoon
    venue_id: np.ndarray | None = None  # (N,) int64 — SIM-411 park factor
    fielder_pos: np.ndarray | None = None  # (N,) int8 (1–9) — SIM-425b
    fielder_id: np.ndarray | None = None  # (N,) int64 — SIM-425b defensive quality
    # SIM-510 transition destinations (None on a pre-0018 bundle). Encoding:
    # -1 = no runner on that base pre-pitch; 4 = scored; 3/2/1 = the post-play
    # base; 0 = retired on the play. ``dest_ok`` is the outs-accounting guard —
    # the SIM-511 fielding draw's cell index keeps only dest_ok rows.
    r1_dest: np.ndarray | None = None  # (N,) int8
    r2_dest: np.ndarray | None = None  # (N,) int8
    r3_dest: np.ndarray | None = None  # (N,) int8
    batter_dest: np.ndarray | None = None  # (N,) int8
    dest_ok: np.ndarray | None = None  # (N,) int8 (0/1)
    # SIM-511 normalization facts: the thrown-out-advancing flags (a
    # discretionary out, never a force or a doubled-off runner), the air-ball
    # flag (the tag-up shape), and the raw spray/distance the SIM-512
    # advancement kernel conditions on.
    r1_adv_out: np.ndarray | None = None  # (N,) int8 (0/1)
    r2_adv_out: np.ndarray | None = None  # (N,) int8 (0/1)
    r3_adv_out: np.ndarray | None = None  # (N,) int8 (0/1)
    is_air: np.ndarray | None = None  # (N,) int8 (0/1)
    spray_raw: np.ndarray | None = None  # (N,) float32 (field-side spray)
    hit_dist: np.ndarray | None = None  # (N,) float32
    # SIM-491: the batting side (1 = the home half). None on a pre-0019
    # bundle — the home-field draw weight then stays neutral.
    bat_home: np.ndarray | None = None  # (N,) int8 (0/1)
    # SIM-518 (SIM-463): the pitch that produced the batted ball — its ten
    # geometry columns (the pitch pool's ``_GEOM_COLS`` order, NaN where the
    # source is missing) and its pitcher. None on a pre-sim518 export; the
    # SIM-472 pitch-similarity kernel then stays neutral.
    pgeom: np.ndarray | None = None  # (N, 10) float32, NaN preserved
    pitcher_id: np.ndarray | None = None  # (N,) int64
    # SIM-523 part C: the batted-ball class (``BB_CLASS``; 0 = unknown). None
    # on a bundle exported before part C; the class filter then stays off.
    bb_class: np.ndarray | None = None  # (N,) int8
    # SIM-523 part G (migration 0024): the fielding CHAIN — the defensive
    # alignment (the player at positions 2..9, column k-2; 0 unknown) and the
    # positions credited a putout / an assist as bitmasks (bit k = position
    # k, 1..9; 0 = no credit — every hit). None on a pre-part-G bundle.
    fielders: np.ndarray | None = None  # (N, 8) int64
    putout_mask: np.ndarray | None = None  # (N,) int16
    assist_mask: np.ndarray | None = None  # (N,) int16

    @property
    def n(self) -> int:
        return int(self.sit.shape[0])

    #: SIM-535: the row's game date as YYYYMMDD. None on a bundle exported
    #: before this existed, in which case the sampler cannot apply a cutoff and
    #: says so rather than silently drawing from the future.
    game_ymd: np.ndarray | None = None  # (N,) int32


@dataclass
class StealPool:
    """One target-base's resident steal OPPORTUNITY pool (SIM-468/474).

    One row per pitch where a steal of this base was possible, attempted or
    not. ``attempted`` is the denominator the attempts-only pool lacks: the
    SIM-474 draw picks ONE row and reads both flags — `attempted` answers
    "does the runner go", `success` answers "safe or caught".
    """

    sit: np.ndarray  # (N, 4) float32 — count_balls, count_strikes, outs, score_diff
    runner_id: np.ndarray  # (N,) int64
    pitcher_id: np.ndarray  # (N,) int64
    catcher_id: np.ndarray  # (N,) int64 (0 when unknown)
    season: np.ndarray  # (N,) int64
    attempted: np.ndarray  # (N,) int8 (0/1)
    success: np.ndarray  # (N,) int8 (0/1)
    recency: np.ndarray  # (N,) float32
    #: SIM-507: pickoff outcomes attributed to this opportunity pitch. An
    #: advancing out is a picked-off caught stealing (MLB scores a CS); a
    #: plain pickoff is an out only; an error advances the runner. None
    #: (a pre-0017 bundle or a test fixture) normalizes to all-zero labels —
    #: no pickoffs staged, the pre-SIM-507 behavior.
    pickoff_out: np.ndarray | None = None  # (N,) int8 (0/1)
    pickoff_advancing: np.ndarray | None = None  # (N,) int8 (0/1)
    pickoff_error: np.ndarray | None = None  # (N,) int8 (0/1)

    def __post_init__(self) -> None:
        n = int(self.sit.shape[0])
        if self.pickoff_out is None:
            self.pickoff_out = np.zeros(n, dtype=np.int8)
        if self.pickoff_advancing is None:
            self.pickoff_advancing = np.zeros(n, dtype=np.int8)
        if self.pickoff_error is None:
            self.pickoff_error = np.zeros(n, dtype=np.int8)

    @property
    def n(self) -> int:
        return int(self.sit.shape[0])

    #: SIM-535: the row's game date as YYYYMMDD. None on a bundle exported
    #: before this existed, in which case the sampler cannot apply a cutoff and
    #: says so rather than silently drawing from the future.
    game_ymd: np.ndarray | None = None  # (N,) int32
    #: SIM-554: the class of the pitch each row rode, as a code of
    #: :data:`STEAL_PITCH_CLASS_CODE` (1-6); 0 = no class — a PICKOFF ROW (a
    #: pickoff throw before any pitch of its pair in the plate appearance,
    #: migration 0031) or a row an old builder wrote. None on a bundle exported
    #: before migration 0031: the loop then runs the single pre-pitch draw.
    pitch_class: np.ndarray | None = None  # (N,) int8
    #: SIM-554: 1 on a pickoff row (a pickoff throw with an outcome before any
    #: pitch of its pair, migration 0031), 0 on a pitch row. It tells a pickoff
    #: row from a pitch row an old builder wrote (both have class code 0). None
    #: on a bundle exported before migration 0031.
    is_pickoff_row: np.ndarray | None = None  # (N,) int8


#: SIM-554: the steal pool's pitch classes, in the order of
#: ``simulation.game_state.PITCH_OUTCOMES``. The tuple is a copy, so this module
#: keeps its one-way dependency on the simulation package; a unit test pins the
#: two equal. The export writes the pool's ``pitch_class`` string; the loader
#: maps it to these codes.
STEAL_PITCH_CLASSES: tuple[str, ...] = (
    "ball",
    "called_strike",
    "swinging_strike",
    "foul",
    "in_play",
    "hit_by_pitch",
)


#: SIM-554: class name -> code (1-6). Code 0 means no class (a pickoff row).
STEAL_PITCH_CLASS_CODE: dict[str, int] = {c: i + 1 for i, c in enumerate(STEAL_PITCH_CLASSES)}


def steal_pitch_class_codes(names: Any, *, where: str = "steal pool") -> np.ndarray:
    """SIM-554: map the exported class strings to int8 codes.

    A class of :data:`STEAL_PITCH_CLASSES` reads its code (1-6); '' or NULL
    (a pickoff row) reads 0. An unknown string also reads 0, and the loader
    logs it by name: such a row joins no class group, so the steal draw never
    picks it. The map runs once per distinct string, not once per row (the
    pool holds about 2.4 million rows).
    """
    raw = np.ma.filled(names, "") if np.ma.isMaskedArray(names) else np.asarray(names)
    obj = np.asarray(raw, dtype=object)
    obj = np.where(np.equal(obj, np.array(None, dtype=object)), "", obj)
    uniq, inverse = np.unique(obj.astype(str), return_inverse=True)
    lut = np.zeros(len(uniq), dtype=np.int8)
    for i, name in enumerate(uniq.tolist()):
        if name == "":
            continue
        code = STEAL_PITCH_CLASS_CODE.get(name)
        if code is None:
            log.warning("%s: unknown pitch class %r read as no class (code 0)", where, name)
            continue
        lut[i] = code
    return lut[inverse.reshape(-1)].astype(np.int8)


@dataclass
class AdvancementPool:
    """One advancement decision's resident opportunity pool (SIM-510/512).

    Keyed ``"{scenario}_{from_base}_{target_base}"`` (see :data:`_ADV_KEYS`).
    One row per opportunity, attempted or not — ``attempted`` is the
    denominator, exactly the steal-pool pattern. The SIM-512 draw picks one
    row and reads ``attempted`` / ``safe`` / ``error_extra`` together.
    """

    feat: np.ndarray  # (N, 5) float32 — exit_velo, launch_angle, spray, distance, outs
    runner_id: np.ndarray  # (N,) int64 (the batter for scenario 5)
    fielder_id: np.ndarray  # (N,) int64 (0 when uncredited)
    fielder_pos: np.ndarray  # (N,) int8 (1–9; 0 when uncredited)
    season: np.ndarray  # (N,) int64
    attempted: np.ndarray  # (N,) int8 (0/1)
    safe: np.ndarray  # (N,) int8 (0/1)
    error_extra: np.ndarray  # (N,) int8 (0/1)
    recency: np.ndarray  # (N,) float32

    @property
    def n(self) -> int:
        return int(self.feat.shape[0])

    #: SIM-535: the row's game date as YYYYMMDD. None on a bundle exported
    #: before this existed, in which case the sampler cannot apply a cutoff and
    #: says so rather than silently drawing from the future.
    game_ymd: np.ndarray | None = None  # (N,) int32


@dataclass
class ChangePool:
    """SIM-523 part D: the pitching-change OPPORTUNITY pool — one row per
    plate-appearance boundary while a pitcher is on the mound, changed or not.
    ``sit`` columns: pitch_count, batters_faced, inning, outs, runners_state,
    score_diff (the fielding side minus the batting side)."""

    sit: np.ndarray  # (N, 6) float32
    pitcher_id: np.ndarray  # (N,) int64 — the pitcher on the mound at the boundary
    incoming_id: (
        np.ndarray
    )  # (N,) int64 — the plate appearance's pitcher (the new arm when changed)
    season: np.ndarray  # (N,) int64
    is_starter: np.ndarray  # (N,) int8
    new_half: np.ndarray  # (N,) int8 — the first plate appearance of a half inning
    changed: np.ndarray  # (N,) int8
    recency: np.ndarray  # (N,) float32
    # SIM-427: the FIELDING side's manager (-1 unknown) and the incoming arm's
    # rest as of the game — days since his last outing capped at five,
    # pitched on either of the two preceding days, pitches over the three
    # preceding days (-1 = unknown, neutral). None on a pre-SIM-427 bundle.
    manager_id: np.ndarray | None = None  # (N,) int64
    in_days_rest: np.ndarray | None = None  # (N,) int8
    in_pitched_2d: np.ndarray | None = None  # (N,) int8
    in_pitches_3d: np.ndarray | None = None  # (N,) int16
    in_throws: np.ndarray | None = None  # (N,) int8 — 0 unknown, 1 L, 2 R
    #: SIM-535: the row's game date as YYYYMMDD (the point-in-time cutoff's
    #: input); None on a bundle built before it — a cutoff then refuses.
    game_ymd: np.ndarray | None = None  # (N,) int32

    @property
    def n(self) -> int:
        return int(self.sit.shape[0])


#: SIM-403b — the shareable subset of numpy arrays inside an EngineArtifacts
#: bundle, by flat name. Used for zero-copy publication into
#: ``multiprocessing.shared_memory`` so worker subprocesses don't each copy the
#: ~hundreds-of-MB pool/embedding arrays from disk. The key convention is
#: ``"<bucket>.<sub>.<attr>"`` so the worker can unambiguously route each view
#: back to its slot. Object-dtype arrays (``HandPool.outcome_type`` /
#: ``BattedBallPool.event``) and the pitcher-sim dict-of-dicts are kept
#: per-worker (loaded from disk) — they are not shareable through
#: ``shared_memory`` and live as small picklable Python objects anyway.
_HAND_POOL_SHAREABLE_ATTRS: tuple[str, ...] = (
    "geom",
    "sit",
    "pitcher_id",
    "batter_id",
    "season",
    "recency",
    # SIM-517: the receiving context (None on a pre-0022 bundle — the
    # isinstance(arr, np.ndarray) guard in extract_shared_arrays skips it).
    "catcher_id",
    "got_away",
    # SIM-518: the draw-conditioning columns (None on a pre-0023 bundle).
    "bat_home",
    "pitch_count",
    "tto",
    # SIM-523 part B: the pitch-id join (None on a pre-part-B bundle).
    "bb_row",
    # SIM-523 part E: the zone (None on a pre-part-E bundle).
    "zone",
)


_BB_POOL_SHAREABLE_ATTRS: tuple[str, ...] = (
    # SIM-523 part C: the batted-ball class (None on a pre-part-C bundle).
    "bb_class",
    # SIM-523 part G: the fielding chain (None on a pre-part-G bundle).
    "fielders",
    "putout_mask",
    "assist_mask",
    "geom",
    "sit",
    "batter_id",
    "season",
    "result_hits",
    "result_outs",
    "recency",
    # SIM-411/425b: the numeric realism columns are shareable (skipped when None on
    # a legacy bundle — extract_shared_arrays' isinstance(arr, np.ndarray) guard).
    # SIM-413's p_throws is object-dtype and stays per-worker (loaded from parquet),
    # exactly like BattedBallPool.event.
    "venue_id",
    "fielder_pos",
    "fielder_id",
    # SIM-510: the transition destinations (None on a pre-0018 bundle).
    "r1_dest",
    "r2_dest",
    "r3_dest",
    "batter_dest",
    "dest_ok",
    # SIM-511: the normalization facts + the advancement-kernel geometry.
    "r1_adv_out",
    "r2_adv_out",
    "r3_adv_out",
    "is_air",
    "spray_raw",
    "hit_dist",
    # SIM-491: the batting side (None on a pre-0019 bundle).
    "bat_home",
    # SIM-518 (SIM-463): the producing pitch's geometry + pitcher (None on a
    # pre-sim518 export).
    "pgeom",
    "pitcher_id",
)


_ACTOR_EMB_SHAREABLE_ATTRS: tuple[str, ...] = ("vecs", "mean", "std")


#: SIM-474: every StealPool column is numeric, so the whole pool is shareable.
_STEAL_POOL_SHAREABLE_ATTRS: tuple[str, ...] = (
    "sit",
    "runner_id",
    "pitcher_id",
    "catcher_id",
    "season",
    "attempted",
    "success",
    "recency",
    "pickoff_out",  # SIM-507
    "pickoff_advancing",
    "pickoff_error",
    "pitch_class",  # SIM-554 (None on a pre-0031 bundle: skipped by the extract)
    "is_pickoff_row",  # SIM-554 (likewise)
)


#: SIM-523 part D: every ChangePool column is numeric, so the whole pool is shareable.
_CHANGE_POOL_SHAREABLE_ATTRS: tuple[str, ...] = (
    "sit",
    "pitcher_id",
    "incoming_id",
    "season",
    "is_starter",
    "new_half",
    "changed",
    "recency",
    # SIM-427 (None on an older bundle — the isinstance guard skips them).
    "manager_id",
    "in_days_rest",
    "in_pitched_2d",
    "in_pitches_3d",
    "in_throws",
    "game_ymd",
)


#: SIM-510: every AdvancementPool column is numeric, so the whole pool is shareable.
_ADV_POOL_SHAREABLE_ATTRS: tuple[str, ...] = (
    "feat",
    "runner_id",
    "fielder_id",
    "fielder_pos",
    "season",
    "attempted",
    "safe",
    "error_extra",
    "recency",
)
