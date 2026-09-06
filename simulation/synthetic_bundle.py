"""
synthetic_bundle.py
===================
SIM-486 -- an in-memory engine-artifact bundle for every no-DB path.

WHY THIS EXISTS
---------------
Production draws every play from the on-disk engine-artifact bundle through
:class:`simulation.full_pool_sampler.FullPoolSampler`. Until SIM-486 the tests
and the no-DB batch factory resolved contact through a second path (an injected
``PlayResolver`` and the legacy advancement code), so the suite certified an
advancement model production never ran. That is how four production defects
survived eight weeks unseen.

This module builds a SMALL :class:`~pipeline.batch.engine_artifacts.EngineArtifacts`
from a few numpy arrays, in the exact shape the production loader produces
(a sim510.1+ transition bundle). The sampler, the loop and the in-play path
(:meth:`StateMachine._resolve_in_play_transition`) are the production code.
Only the rows are synthetic.

Three uses:

  * :func:`league_artifacts` -- the league-average outcome mix over every
    count bucket and every base-out cell, plus the SIM-512 advancement pools,
    so a no-DB game has a realistic run environment (the SIM-324 idiom).
  * :func:`fixed_play_artifacts` -- ONE event in every base-out cell, so a
    unit test can drive a deterministic play through the production in-play
    path with no random draw on the batted ball.
  * :func:`synthetic_artifacts` -- the general builder the two above wrap.

THE ROW SHAPE
-------------
A batted-ball row carries the SIM-510 transition: where the batter and each
pre-pitch runner ended up (``-1`` = no runner on that base, ``0`` = retired,
``1``/``2``/``3`` = the post-play base, ``4`` = scored). The five discretionary
movements are normalized to station-to-station by the loop and re-decided by
the advancement draws, so a synthetic row only states the FORCED movement
(:func:`canonical_transition`). Without advancement pools the game is
station-to-station -- exactly deterministic for a fixed-play test.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from pipeline.batch.engine_artifacts import (
    AdvancementPool,
    BattedBallPool,
    EngineArtifacts,
    HandPool,
    StealPool,
)

__all__ = [
    "LEAGUE_PITCH_MODEL",
    "LEAGUE_INPLAY_MODEL",
    "DEFAULT_ADVANCEMENT_RATES",
    "PlayRow",
    "canonical_transition",
    "inplay_rows",
    "pitch_pool",
    "battedball_pool",
    "advancement_pools",
    "steal_pools",
    "synthetic_artifacts",
    "league_artifacts",
    "fixed_play_artifacts",
    "synthetic_sampler",
]

#: The five pitch outcomes the count machine understands.
PITCH_OUTCOMES: tuple[str, ...] = ("ball", "called_strike", "swinging_strike", "foul", "in_play")

#: A league-average per-pitch mix. Run through the count machine it lands
#: near 8-9% walks, ~22% strikeouts and ~3.85 pitches per plate appearance
#: (the SIM-324 calibration).
LEAGUE_PITCH_MODEL: dict[str, float] = {
    "ball": 0.332,
    "called_strike": 0.138,
    "swinging_strike": 0.087,
    "foul": 0.273,
    "in_play": 0.170,
}

#: The league in-play event mix (conditional on contact). The double-play
#: share moves to ``field_out`` in cells where a double play is impossible.
LEAGUE_INPLAY_MODEL: dict[str, float] = {
    "home_run": 0.049,
    "single": 0.239,
    "double": 0.071,
    "triple": 0.006,
    "field_out": 0.609,
    "grounded_into_double_play": 0.026,
}

#: The SIM-512 advancement decisions: key -> (attempt rate, safe rate).
#: Keys are ``"{scenario}_{from_base}_{target_base}"``: 1 = first-to-third on
#: a single, 2 = second-to-home on a single, 3 = first-to-home on a double,
#: 4 = a tag-up on a caught ball, 5 = the batter's stretch (from_base 0).
DEFAULT_ADVANCEMENT_RATES: dict[str, tuple[float, float]] = {
    "2_2_4": (0.60, 0.93),
    "1_1_3": (0.28, 0.95),
    "5_0_2": (0.03, 0.60),
    "3_1_4": (0.45, 0.90),
    "5_0_3": (0.03, 0.60),
    "4_3_4": (0.45, 0.95),
    "4_2_3": (0.20, 0.90),
    "4_1_2": (0.05, 0.90),
}

_HITS = {"single": 1, "double": 2, "triple": 3, "home_run": 4}
_BIT_1B, _BIT_2B, _BIT_3B = 0b001, 0b010, 0b100


@dataclass(slots=True)
class PlayRow:
    """One synthetic batted-ball row: the event, the base-out cell it is legal
    in, and its SIM-510 transition. ``weight`` is the row's draw weight inside
    its cell (the pool's ``recency`` column)."""

    event: str
    outs: int
    runners_state: int
    batter_dest: int
    r1_dest: int
    r2_dest: int
    r3_dest: int
    result_hits: int
    result_outs: int
    is_air: bool = False
    weight: float = 1.0
    adv1: bool = False
    adv2: bool = False
    adv3: bool = False
    exit_velo: float = 0.0
    launch_angle: float = 0.0
    spray: float = 0.0
    dist: float = 0.0


def canonical_transition(
    event: str, outs: int, runners_state: int, *, is_air: bool = False
) -> PlayRow | None:
    """The forced movement for ``event`` from a base-out cell, or ``None`` when
    the event is impossible there (a double play needs a runner on first and
    fewer than two outs).

    Hits push every runner the hit value; a runner on third scores on any hit.
    An out holds every runner. An error reaches the batter and pushes the
    runners one base. A ground-ball double play retires the batter and the
    runner from first; a runner on second takes third; a runner on third
    scores only when the play does not end the inning.
    """
    on1 = bool(runners_state & _BIT_1B)
    on2 = bool(runners_state & _BIT_2B)
    on3 = bool(runners_state & _BIT_3B)
    r1 = 1 if on1 else -1
    r2 = 2 if on2 else -1
    r3 = 3 if on3 else -1
    hits = _HITS.get(event, 0)
    if event in _HITS:
        if on1:
            r1 = min(1 + hits, 4)
        if on2:
            r2 = min(2 + hits, 4)
        if on3:
            r3 = 4
        return PlayRow(event, outs, runners_state, hits, r1, r2, r3, hits, 0)
    if event == "field_error":
        return PlayRow(
            event,
            outs,
            runners_state,
            1,
            2 if on1 else -1,
            3 if on2 else -1,
            4 if on3 else -1,
            0,
            0,
        )
    if event in ("grounded_into_double_play", "ground_into_double_play"):
        if not on1 or outs >= 2:
            return None
        return PlayRow(
            event,
            outs,
            runners_state,
            0,
            0,
            3 if on2 else -1,
            (4 if outs == 0 else 3) if on3 else -1,
            0,
            2,
        )
    # Every other event is a one-out play that holds the runners.
    return PlayRow(event, outs, runners_state, 0, r1, r2, r3, 0, 1, is_air=is_air)


def inplay_rows(
    model: dict[str, float] | None = None,
    *,
    air_share: float = 0.45,
) -> list[PlayRow]:
    """One weighted row per (base-out cell, event) over all 24 cells.

    ``field_out`` splits into a ground-ball row and an air-ball row (the
    air share feeds the tag-up draw). Double-play mass moves to the ground
    out in cells where a double play is impossible.
    """
    model = dict(model or LEAGUE_INPLAY_MODEL)
    rows: list[PlayRow] = []
    for outs in range(3):
        for rs in range(8):
            spill = 0.0
            cell: list[PlayRow] = []
            for event, w in model.items():
                if w <= 0.0:
                    continue
                if event == "field_out":
                    ground = canonical_transition(event, outs, rs)
                    air = canonical_transition(event, outs, rs, is_air=True)
                    assert ground is not None and air is not None
                    ground.weight = w * (1.0 - air_share)
                    air.weight = w * air_share
                    cell.extend([ground, air])
                    continue
                row = canonical_transition(event, outs, rs)
                if row is None:
                    spill += w
                    continue
                row.weight = w
                cell.append(row)
            if spill > 0.0:
                for row in cell:
                    if row.event == "field_out" and not row.is_air:
                        row.weight += spill
                        break
                else:
                    ground = canonical_transition("field_out", outs, rs)
                    assert ground is not None
                    ground.weight = spill
                    cell.append(ground)
            rows.extend(cell)
    return rows


def pitch_pool(
    model: dict[str, float] | None = None,
    *,
    pitcher_id: int = 0,
    batter_id: int = 0,
    season: int = 2024,
    got_away: bool = False,
) -> HandPool:
    """A pitch pool with one weighted row per (count bucket, outcome), so
    every live count draws the same outcome mix. ``got_away`` marks every
    row as a passed ball / wild pitch (the SIM-517 fact a dropped-third-
    strike test wants)."""
    model = dict(model or LEAGUE_PITCH_MODEL)
    rows = [(b, s, o, w) for b in range(4) for s in range(3) for o, w in model.items() if w > 0.0]
    n = len(rows)
    sit = np.zeros((n, 6), dtype=np.float32)
    sit[:, 0] = [r[0] for r in rows]
    sit[:, 1] = [r[1] for r in rows]
    return HandPool(
        geom=np.zeros((n, 10), dtype=np.float32),
        sit=sit,
        pitcher_id=np.full(n, int(pitcher_id), dtype=np.int64),
        batter_id=np.full(n, int(batter_id), dtype=np.int64),
        season=np.full(n, int(season), dtype=np.int64),
        outcome_type=np.asarray([r[2] for r in rows], dtype=object),
        recency=np.asarray([r[3] for r in rows], dtype=np.float32),
        got_away=np.full(n, 1 if got_away else 0, dtype=np.int8),
    )


def battedball_pool(
    rows: list[PlayRow],
    *,
    batter_id: int = 0,
    season: int = 2024,
) -> BattedBallPool:
    """Materialize :class:`PlayRow` rows as a transition-carrying pool."""
    n = len(rows)
    if n == 0:
        raise ValueError("a batted-ball pool needs at least one row")

    def col(attr: str, dtype) -> np.ndarray:
        return np.asarray([getattr(r, attr) for r in rows], dtype=dtype)

    geom = np.zeros((n, 3), dtype=np.float32)
    geom[:, 0] = col("exit_velo", np.float32)
    geom[:, 1] = col("launch_angle", np.float32)
    geom[:, 2] = col("spray", np.float32)
    sit = np.zeros((n, 6), dtype=np.float32)
    sit[:, 2] = col("outs", np.float32)
    sit[:, 3] = col("runners_state", np.float32)
    return BattedBallPool(
        geom=geom,
        sit=sit,
        batter_id=np.full(n, int(batter_id), dtype=np.int64),
        season=np.full(n, int(season), dtype=np.int64),
        event=np.asarray([r.event for r in rows], dtype=object),
        result_hits=col("result_hits", np.int8),
        result_outs=col("result_outs", np.int8),
        recency=col("weight", np.float32),
        r1_dest=col("r1_dest", np.int8),
        r2_dest=col("r2_dest", np.int8),
        r3_dest=col("r3_dest", np.int8),
        batter_dest=col("batter_dest", np.int8),
        dest_ok=np.ones(n, dtype=np.int8),
        r1_adv_out=col("adv1", np.int8),
        r2_adv_out=col("adv2", np.int8),
        r3_adv_out=col("adv3", np.int8),
        is_air=col("is_air", np.int8),
        spray_raw=col("spray", np.float32),
        hit_dist=col("dist", np.float32),
    )


def _rate_pool_rows(attempt: float, safe: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Three rows -- (went, safe), (went, out), (stayed) -- weighted to the rates."""
    attempt = float(min(max(attempt, 0.0), 1.0))
    safe = float(min(max(safe, 0.0), 1.0))
    attempted = np.asarray([1, 1, 0], dtype=np.int8)
    success = np.asarray([1, 0, 0], dtype=np.int8)
    weight = np.asarray([attempt * safe, attempt * (1.0 - safe), 1.0 - attempt], dtype=np.float32)
    return attempted, success, weight


def advancement_pools(
    rates: dict[str, tuple[float, float]] | None = None,
    *,
    season: int = 2024,
) -> dict[str, AdvancementPool]:
    """One three-row opportunity pool per advancement decision."""
    out: dict[str, AdvancementPool] = {}
    for key, (attempt, safe) in (rates or DEFAULT_ADVANCEMENT_RATES).items():
        attempted, success, weight = _rate_pool_rows(attempt, safe)
        n = attempted.size
        out[key] = AdvancementPool(
            feat=np.zeros((n, 5), dtype=np.float32),
            runner_id=np.zeros(n, dtype=np.int64),
            fielder_id=np.zeros(n, dtype=np.int64),
            fielder_pos=np.zeros(n, dtype=np.int8),
            season=np.full(n, int(season), dtype=np.int64),
            attempted=attempted,
            safe=success,
            error_extra=np.zeros(n, dtype=np.int8),
            recency=weight,
        )
    return out


def steal_pools(
    attempt_rate: float,
    success_rate: float = 0.78,
    *,
    targets: tuple[str, ...] = ("2", "3"),
    season: int = 2024,
) -> dict[str, StealPool]:
    """A steal-opportunity pool per target base with three rows in every
    (outs, balls, strikes) cell, weighted to the attempt and success rates."""
    cells = [(o, b, s) for o in range(3) for b in range(4) for s in range(3)]
    attempted, success, weight = _rate_pool_rows(attempt_rate, success_rate)
    n = len(cells) * attempted.size
    sit = np.zeros((n, 4), dtype=np.float32)
    for i, (o, b, s) in enumerate(cells):
        sl = slice(i * attempted.size, (i + 1) * attempted.size)
        sit[sl, 0] = b
        sit[sl, 1] = s
        sit[sl, 2] = o
    out: dict[str, StealPool] = {}
    for target in targets:
        out[str(target)] = StealPool(
            sit=sit.copy(),
            runner_id=np.zeros(n, dtype=np.int64),
            pitcher_id=np.zeros(n, dtype=np.int64),
            catcher_id=np.zeros(n, dtype=np.int64),
            season=np.full(n, int(season), dtype=np.int64),
            attempted=np.tile(attempted, len(cells)),
            success=np.tile(success, len(cells)),
            recency=np.tile(weight, len(cells)),
        )
    return out


def synthetic_artifacts(
    *,
    pitch_model: dict[str, float] | None = None,
    pitch_models: dict[str, dict[str, float]] | None = None,
    inplay_model: dict[str, float] | None = None,
    bb_rows: list[PlayRow] | None = None,
    advancement: dict[str, tuple[float, float]] | bool = False,
    steal: tuple[float, float] | None = None,
    hands: tuple[str, ...] = ("R", "L"),
    got_away: bool = False,
    air_share: float = 0.45,
    season: int = 2024,
) -> EngineArtifacts:
    """Build an in-memory bundle.

    ``pitch_model`` is the per-pitch mix for every hand; ``pitch_models`` maps
    a hand to its own mix (a platoon skew). ``inplay_model`` drives
    :func:`inplay_rows` unless explicit ``bb_rows`` are given. ``advancement``
    is ``True`` for the default rates, a mapping for custom ones, ``False``
    for station-to-station. ``steal`` is an ``(attempt, success)`` pair.
    """
    pools: dict[str, HandPool] = {}
    for hand in hands:
        model = (pitch_models or {}).get(hand, pitch_model)
        pools[hand] = pitch_pool(model, season=season, got_away=got_away)
    rows = bb_rows if bb_rows is not None else inplay_rows(inplay_model, air_share=air_share)
    bb = battedball_pool(rows, season=season)
    bb_pools = dict.fromkeys(hands, bb)
    adv: dict[str, AdvancementPool] = {}
    if advancement is True:
        adv = advancement_pools(season=season)
    elif advancement:
        adv = advancement_pools(advancement, season=season)
    stl = steal_pools(*steal, season=season) if steal is not None else {}
    return EngineArtifacts(
        pools=pools,
        bb_pools=bb_pools,
        adv_pools=adv,
        steal_pools=stl,
        seasons=[int(season)],
    )


def league_artifacts(
    *,
    pitch_model: dict[str, float] | None = None,
    pitch_models: dict[str, dict[str, float]] | None = None,
    inplay_model: dict[str, float] | None = None,
    advancement: dict[str, tuple[float, float]] | bool = True,
    steal: tuple[float, float] | None = None,
    season: int = 2024,
) -> EngineArtifacts:
    """The league-average bundle: every count, every cell, the advancement
    draws on. A no-DB game drawn from it is realistic baseball."""
    return synthetic_artifacts(
        pitch_model=pitch_model or LEAGUE_PITCH_MODEL,
        pitch_models=pitch_models,
        inplay_model=inplay_model or LEAGUE_INPLAY_MODEL,
        advancement=advancement,
        steal=steal,
        season=season,
    )


def fixed_play_artifacts(
    event: str,
    *,
    is_air: bool = False,
    pitch_model: dict[str, float] | None = None,
    got_away: bool = False,
    season: int = 2024,
    **overrides: int,
) -> EngineArtifacts:
    """ONE event in every base-out cell, with its canonical transition, and
    no advancement pools -- a deterministic play for a unit test.

    ``overrides`` (``batter_dest`` / ``r1_dest`` / ``r2_dest`` / ``r3_dest`` /
    ``result_hits`` / ``result_outs``) replace the canonical values in every
    cell. Cells where the event is impossible fall back to a ground out.
    """
    rows: list[PlayRow] = []
    for outs in range(3):
        for rs in range(8):
            row = canonical_transition(event, outs, rs, is_air=is_air)
            if row is None:
                row = canonical_transition("field_out", outs, rs)
                assert row is not None
            for name, value in overrides.items():
                setattr(row, name, int(value))
            rows.append(row)
    return synthetic_artifacts(
        pitch_model=pitch_model or LEAGUE_PITCH_MODEL,
        bb_rows=rows,
        advancement=False,
        got_away=got_away,
        season=season,
    )


def synthetic_sampler(artifacts: EngineArtifacts | None = None, seed: int | None = 0, **kw):
    """A :class:`FullPoolSampler` over ``artifacts`` (the league bundle by default)."""
    from simulation.full_pool_sampler import FullPoolSampler

    return FullPoolSampler(
        artifacts if artifacts is not None else league_artifacts(),
        np.random.default_rng(seed),
        **kw,
    )
