"""The pool classes a worker draws from, and the arrays of each pool that the workers share.

An optional column is None on a bundle exported before the column existed. Its reader then
stays neutral, unless the column's comment says otherwise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np

log = logging.getLogger("engine_artifacts")


@dataclass
class HandPool:
    """The pitch pool of one batter hand: one row per real pitch, the rows the pitch draw scores."""

    geom: np.ndarray  # (N, 10) float32: the raw geometry; the sampler applies the kernel
    sit: np.ndarray  # (N, 6)  float32: balls, strikes, outs, runners, inning, score_diff
    pitcher_id: np.ndarray  # (N,) int64
    batter_id: np.ndarray  # (N,) int64
    season: np.ndarray  # (N,) int64: forms the "pitcher_id:season" pitcher similarity key
    outcome_type: np.ndarray  # (N,) object: ball/called_strike/swinging_strike/foul/in_play
    recency: np.ndarray  # (N,) float32
    # The receiving context. ``got_away`` is 1 on a passed ball or a wild pitch, an uncaught
    # third strike included.
    catcher_id: np.ndarray | None = None  # (N,) int64 (0 when unknown)
    got_away: np.ndarray | None = None  # (N,) int8 (0/1)
    # The draw-conditioning columns. An unknown row reads bat_home -1, pitch_count -1 and
    # tto 0, and every reader is neutral on it.
    bat_home: np.ndarray | None = None  # (N,) int8 (1 = the home team bats)
    pitch_count: np.ndarray | None = None  # (N,) int16 (pitches before the plate appearance)
    tto: np.ndarray | None = None  # (N,) int8 (times through the order, >= 1)
    # The row of ``bb_pools[hand]`` that holds this in-play pitch's own batted ball (-1: not in
    # play, or no batted-ball row). Without the column, no batted ball is born on the pitch.
    bb_row: np.ndarray | None = None  # (N,) int32
    # The Statcast zone (1-9 in the zone, 11-14 outside, 0 unknown): the receiving ratio's
    # zone group.
    zone: np.ndarray | None = None  # (N,) int8

    @property
    def n(self) -> int:
        return int(self.geom.shape[0])

    #: The row's game date as YYYYMMDD. Without it, the sampler cannot apply the date cutoff
    #: (no row from the simulated game's future); it says so instead of drawing silently.
    game_ymd: np.ndarray | None = None  # (N,) int32


@dataclass
class BattedBallPool:
    """The batted-ball pool of one batter hand: the rows the fielding draw picks from."""

    geom: np.ndarray  # (N, 3) float32: exit_velo, launch_angle, spray
    sit: np.ndarray  # (N, 6)  float32
    batter_id: np.ndarray  # (N,) int64
    season: np.ndarray  # (N,) int64
    event: np.ndarray  # (N,) object
    result_hits: np.ndarray  # (N,) int8 (0=out,1=1B,2=2B,3=3B,4=HR)
    result_outs: np.ndarray  # (N,) int8
    recency: np.ndarray  # (N,) float32
    # The per-row facts. The fence stage and the wall-zone park weight read ``venue_id``; the
    # fielder factor reads ``fielder_id``, the player who fielded the ball.
    p_throws: np.ndarray | None = None  # (N,) object: the pitcher's hand, 'L'/'R'
    venue_id: np.ndarray | None = None  # (N,) int64: the park
    fielder_pos: np.ndarray | None = None  # (N,) int8: the position (1–9) that fielded it
    fielder_id: np.ndarray | None = None  # (N,) int64
    # Where each runner and the batter ended up: -1 = no runner on that base before the pitch;
    # 0 = retired on the play; 1/2/3 = the base after the play; 4 = scored. ``dest_ok`` is the
    # outs-accounting guard: the fielding draw's cell index keeps only dest_ok rows.
    r1_dest: np.ndarray | None = None  # (N,) int8
    r2_dest: np.ndarray | None = None  # (N,) int8
    r3_dest: np.ndarray | None = None  # (N,) int8
    batter_dest: np.ndarray | None = None  # (N,) int8
    dest_ok: np.ndarray | None = None  # (N,) int8 (0/1)
    # The normalization facts (the loop resets each discretionary advance before the
    # advancement draws decide it again). ``r*_adv_out`` marks a runner thrown out advancing,
    # never a force or a doubled-off runner; ``is_air`` marks an air ball (the tag-up shape).
    # The advancement draw and the fence stage read the raw spray and distance.
    r1_adv_out: np.ndarray | None = None  # (N,) int8 (0/1)
    r2_adv_out: np.ndarray | None = None  # (N,) int8 (0/1)
    r3_adv_out: np.ndarray | None = None  # (N,) int8 (0/1)
    is_air: np.ndarray | None = None  # (N,) int8 (0/1)
    spray_raw: np.ndarray | None = None  # (N,) float32 (field-side spray)
    hit_dist: np.ndarray | None = None  # (N,) float32
    # The batting side; the home-field draw weight reads it.
    bat_home: np.ndarray | None = None  # (N,) int8 (1 = the home half)
    # The pitch that produced the batted ball: its ten geometry columns (the pitch pool's
    # ``_GEOM_COLS`` order, NaN where the source is missing) and its pitcher. The
    # pitch-similarity kernel reads them.
    pgeom: np.ndarray | None = None  # (N, 10) float32, NaN preserved
    pitcher_id: np.ndarray | None = None  # (N,) int64
    # The batted-ball class (``BB_CLASS``; 0 = unknown). Without it, the class filter is off.
    bb_class: np.ndarray | None = None  # (N,) int8
    # The fielding chain: the defensive alignment (the player at positions 2..9 in column
    # k-2; 0 unknown), and the positions credited with a putout and with an assist as bitmasks
    # (bit k = position k, 1..9; 0 = no credit, as on every hit).
    fielders: np.ndarray | None = None  # (N, 8) int64
    putout_mask: np.ndarray | None = None  # (N,) int16
    assist_mask: np.ndarray | None = None  # (N,) int16

    @property
    def n(self) -> int:
        return int(self.sit.shape[0])

    #: The row's game date as YYYYMMDD (see ``HandPool.game_ymd``).
    game_ymd: np.ndarray | None = None  # (N,) int32


@dataclass
class StealPool:
    """The steal opportunity pool of one target base.

    The pool holds one row per pitch where a steal of this base was possible, attempted or
    not, so it carries the attempt rate's denominator. The steal draw picks one row and reads
    both flags: ``attempted`` (does the runner go) and ``success`` (safe or caught).
    """

    sit: np.ndarray  # (N, 4) float32: count_balls, count_strikes, outs, score_diff
    runner_id: np.ndarray  # (N,) int64
    pitcher_id: np.ndarray  # (N,) int64
    catcher_id: np.ndarray  # (N,) int64 (0 when unknown)
    season: np.ndarray  # (N,) int64
    attempted: np.ndarray  # (N,) int8 (0/1)
    success: np.ndarray  # (N,) int8 (0/1)
    recency: np.ndarray  # (N,) float32
    #: The pickoff outcome on this opportunity. An advancing out is a pickoff that MLB scores
    #: as a caught stealing; a plain pickoff is an out only; an error advances the runner.
    #: None becomes all-zero labels, so the draw stages no pickoff.
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

    #: The row's game date as YYYYMMDD (see ``HandPool.game_ymd``).
    game_ymd: np.ndarray | None = None  # (N,) int32
    #: The class of the pitch each row rode, as a code of :data:`STEAL_PITCH_CLASS_CODE`
    #: (1-6). Code 0 means no class: a pickoff row, or a pitch row an old builder wrote.
    #: Without the column (a bundle exported before DuckDB migration 0031), the loop runs
    #: the single pre-pitch steal draw.
    pitch_class: np.ndarray | None = None  # (N,) int8
    #: 1 on a pickoff row, 0 on a pitch row. A pickoff row is a pickoff throw with an outcome
    #: before any pitch row of the same target base in its plate appearance. The flag tells a
    #: pickoff row from an old builder's pitch row, since both carry class code 0.
    is_pickoff_row: np.ndarray | None = None  # (N,) int8


#: The steal pool's pitch classes, in the order of ``simulation.game_state.PITCH_OUTCOMES``.
#: The copy keeps this module independent of the simulation package; a unit test pins the two
#: equal. The export writes each row's class name; the loader maps it to these codes.
STEAL_PITCH_CLASSES: tuple[str, ...] = (
    "ball",
    "called_strike",
    "swinging_strike",
    "foul",
    "in_play",
    "hit_by_pitch",
)
#: Class name -> code (1-6). Code 0 means no class (a pickoff row).
STEAL_PITCH_CLASS_CODE: dict[str, int] = {c: i + 1 for i, c in enumerate(STEAL_PITCH_CLASSES)}


def steal_pitch_class_codes(names: Any, *, where: str = "steal pool") -> np.ndarray:
    """Map the exported class names to int8 codes.

    A class of :data:`STEAL_PITCH_CLASSES` reads its code (1-6); '' or NULL (a pickoff row)
    reads 0. An unknown name also reads 0, and the function logs it: such a row joins no
    class group, so the steal draw never picks it. The map runs once per distinct name, not
    once per row, because the pool holds millions of rows.
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
    """The opportunity pool of one advancement decision.

    The key is ``"{scenario}_{from_base}_{target_base}"`` (see
    ``bundle.export_advancement_pool._ADV_KEYS``). The pool holds one row per opportunity,
    attempted or not, like the steal pool. The advancement draw picks one row and reads
    ``attempted``, ``safe`` and ``error_extra`` together.
    """

    feat: np.ndarray  # (N, 5) float32: exit_velo, launch_angle, spray, distance, outs
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

    #: The row's game date as YYYYMMDD (see ``HandPool.game_ymd``).
    game_ymd: np.ndarray | None = None  # (N,) int32


@dataclass
class ChangePool:
    """The pitching-change opportunity pool: one row per plate-appearance boundary while a
    pitcher is on the mound, changed or not.

    The ``sit`` columns are pitch_count, batters_faced, inning, outs, runners_state and
    score_diff (the fielding side minus the batting side).
    """

    sit: np.ndarray  # (N, 6) float32
    pitcher_id: np.ndarray  # (N,) int64: the pitcher on the mound at the boundary
    incoming_id: np.ndarray  # (N,) int64: the plate appearance's pitcher (the new arm on a change)
    season: np.ndarray  # (N,) int64
    is_starter: np.ndarray  # (N,) int8
    new_half: np.ndarray  # (N,) int8: the first plate appearance of a half inning
    changed: np.ndarray  # (N,) int8
    recency: np.ndarray  # (N,) float32
    # The fielding side's manager (-1 unknown) and the incoming arm's rest as of the game:
    # the days since his last outing (capped at five), whether he pitched on either of the
    # two days before, and his pitches over the three days before (-1 = unknown, neutral).
    manager_id: np.ndarray | None = None  # (N,) int64
    in_days_rest: np.ndarray | None = None  # (N,) int8
    in_pitched_2d: np.ndarray | None = None  # (N,) int8
    in_pitches_3d: np.ndarray | None = None  # (N,) int16
    in_throws: np.ndarray | None = None  # (N,) int8: 0 unknown, 1 L, 2 R
    #: The row's game date as YYYYMMDD. Without it, a date cutoff refuses the pool.
    game_ymd: np.ndarray | None = None  # (N,) int32

    @property
    def n(self) -> int:
        return int(self.sit.shape[0])


#: The numeric arrays of each pool that the parent publishes into shared memory, keyed
#: ``"<bucket>.<sub>.<attr>"`` so a worker can route each view back to its slot. Sharing them
#: saves each worker its own copy of hundreds of MB. An object-dtype array (such as
#: ``HandPool.outcome_type``, ``BattedBallPool.event`` or ``p_throws``) cannot go through
#: shared memory, so each worker loads it from disk. ``extract_shared_arrays`` skips a column
#: that is None.
_HAND_POOL_SHAREABLE_ATTRS: tuple[str, ...] = (
    "geom",
    "sit",
    "pitcher_id",
    "batter_id",
    "season",
    "recency",
    "catcher_id",
    "got_away",
    "bat_home",
    "pitch_count",
    "tto",
    "bb_row",
    "zone",
)
_BB_POOL_SHAREABLE_ATTRS: tuple[str, ...] = (
    "bb_class",
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
    "venue_id",
    "fielder_pos",
    "fielder_id",
    "r1_dest",
    "r2_dest",
    "r3_dest",
    "batter_dest",
    "dest_ok",
    "r1_adv_out",
    "r2_adv_out",
    "r3_adv_out",
    "is_air",
    "spray_raw",
    "hit_dist",
    "bat_home",
    "pgeom",
    "pitcher_id",
)
_ACTOR_EMB_SHAREABLE_ATTRS: tuple[str, ...] = ("vecs", "mean", "std")
#: Every StealPool column is numeric, so the whole pool is shareable.
_STEAL_POOL_SHAREABLE_ATTRS: tuple[str, ...] = (
    "sit",
    "runner_id",
    "pitcher_id",
    "catcher_id",
    "season",
    "attempted",
    "success",
    "recency",
    "pickoff_out",
    "pickoff_advancing",
    "pickoff_error",
    "pitch_class",
    "is_pickoff_row",
)
#: Every ChangePool column is numeric, so the whole pool is shareable.
_CHANGE_POOL_SHAREABLE_ATTRS: tuple[str, ...] = (
    "sit",
    "pitcher_id",
    "incoming_id",
    "season",
    "is_starter",
    "new_half",
    "changed",
    "recency",
    "manager_id",
    "in_days_rest",
    "in_pitched_2d",
    "in_pitches_3d",
    "in_throws",
    "game_ymd",
)
#: Every AdvancementPool column is numeric, so the whole pool is shareable.
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
