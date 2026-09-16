"""
scripts/sim427_manager_probe.py — SIM-427: the manager probe (plan part 4f).

Runs real games on the balanced 45-game set with ONE configuration of the
pitching-change draw (an "arm") and reads, per game and per iteration:

  * THE USAGE READ — pitchers per team-game, the starters' pitches at the
    pull (the mean AND the spread across starter-games), the starters' outs,
    and the share of changes at a half-inning boundary — against the official
    box score's own numbers for the set's seasons (``raw.game_player_stats``)
    and the pool's own change rates;
  * THE MANAGER READ — the change rate per boundary by manager tier (the live
    manager's own starter pitch count: quick hook / league / long leash,
    terciles over the set's managers) against the pool's own change rate on
    those managers' rows — the per-opportunity conditional the manager power
    is fitted on;
  * THE RELIEVER READ — the entering arm's high-leverage role share by inning
    tier, its rest at entry (the share that pitched yesterday or two days
    ago, the mean pitches over the prior three days) and its hand mix, against the pool's
    own incoming arms;
  * THE COMPOSITION READ — the sim's pitch share by pitch-count band and by
    times through the order against the pool's row share (the fatigue read
    that motivated this ticket — docs/audit/2026-09-12-sim518-fit-plan.md);
  * THE PREDICTION SHIFT — per starter-game, the strikeout mean and the
    probability of clearing the closing strikeout line, arm minus arm.

One process runs ONE arm; run the arms in parallel and pair them with
``report`` (the first JSON is the baseline):

    P="MSYS_NO_PATHCONV=1 docker compose run --rm -T -v $PWD/scripts:/app/scripts app"
    $P python scripts/sim427_manager_probe.py run --arm off --iters 40 \\
        --json-out /app/scripts/sim427_probe_off.json &
    $P python scripts/sim427_manager_probe.py run --arm draw=1,pen=box,mgr=0 --iters 40 \\
        --json-out /app/scripts/sim427_probe_draw0.json &
    $P python scripts/sim427_manager_probe.py run --arm draw=1,pen=box,mgr=2 --iters 40 \\
        --json-out /app/scripts/sim427_probe_draw2.json &
    wait
    $P python scripts/sim427_manager_probe.py report scripts/sim427_probe_off.json \\
        scripts/sim427_probe_draw0.json scripts/sim427_probe_draw2.json \\
        --scan-json scripts/sim518_fatigue_scan.json

An arm is ``off`` (production today: the SIM-434 formula, the synthetic pen)
or a comma list of ``key=value``: ``draw`` (0/1), ``pen`` (synthetic/box),
``mgr`` (the manager power), ``role`` (σ), ``stuff`` (power), ``rest`` (σ,
days), ``p2d`` (mismatch weight), ``p3d`` (σ, pitches), ``hand`` (mismatch
weight). The sampler weights are set on the process-cached sampler directly
(the unit lane's convention); the pen source is the resolver's
``SIM_BULLPEN_SOURCE`` switch, set in this process's environment before the
games are resolved (the resolver has no other handle). The compose file's
production flags stay as they are.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402
from sim_stats import _FACTORY, _resolve, open_sim_duckdb, sim_kwargs_from_state  # noqa: E402

from simulation.batch_runner import GameSpec  # noqa: E402
from simulation.game_state import Team  # noqa: E402
from simulation.production_factory import production_machine_factory  # noqa: E402
from simulation.sim_loop import BoxScore, simulate_game, times_through_order  # noqa: E402


def _load_scan_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "sim518_fatigue_scan", str(_ROOT / "scripts" / "sim518_fatigue_scan.py")
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sim518_fatigue_scan"] = mod
    spec.loader.exec_module(mod)
    return mod


scan = _load_scan_module()
PC_BANDS = scan.PC_BANDS
N_PC_BAND = scan.N_PC_BAND
TTO_LABELS = scan.TTO_LABELS
N_TTO = scan.N_TTO

#: Inning tiers for the reliever's role read.
INNING_TIERS: tuple[str, ...] = ("<=6", "7-8", "9+")
#: Manager tiers (terciles of the live managers' own starter pitch count).
MANAGER_TIERS: tuple[str, ...] = ("quick hook", "league", "long leash")
#: The plan's 2024 box-score references, the fallback when the database is unreachable.
BOX_2024 = {
    "pitchers_per_team_game": 4.28,
    "starter_pitches_mean": 85.2,
    "starter_pitches_sd": 17.0,
    "starter_outs_mean": 15.6,
}


# ---------------------------------------------------------------------------
# The arm
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Arm:
    draw: bool = False
    pen: str = "synthetic"
    mgr: float = 0.0
    role: float = 0.0
    stuff: float = 0.0
    rest: float = 0.0
    p2d: float = 1.0
    p3d: float = 0.0
    hand: float = 1.0

    @classmethod
    def parse(cls, text: str) -> Arm:
        text = text.strip().lower()
        if text in ("off", "formula", "baseline"):
            return cls()
        kw: dict[str, Any] = {}
        for part in text.split(","):
            if not part.strip():
                continue
            k, v = part.split("=")
            k = k.strip()
            v = v.strip()
            if k == "draw":
                kw["draw"] = v not in ("0", "false", "off", "no")
            elif k == "pen":
                if v not in ("synthetic", "box"):
                    raise SystemExit(f"pen must be synthetic or box, not {v!r}")
                kw["pen"] = v
            elif k in ("mgr", "role", "stuff", "rest", "p2d", "p3d", "hand"):
                kw[k] = float(v)
            else:
                raise SystemExit(f"unknown arm key {k!r}")
        return cls(**kw)

    def label(self) -> str:
        if not self.draw and self.pen == "synthetic":
            return "off (formula, synthetic pen)"
        bits = [f"draw={int(self.draw)}", f"pen={self.pen}", f"mgr={self.mgr:g}"]
        for k in ("role", "stuff", "rest", "p2d", "p3d", "hand"):
            v = getattr(self, k)
            default = 1.0 if k in ("p2d", "hand") else 0.0
            if v != default:
                bits.append(f"{k}={v:g}")
        return ",".join(bits)

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    def apply(self, machine: Any) -> None:
        machine.manager_draw = bool(self.draw)
        fp = machine.full_pool_sampler
        fp.actor_power["manager_usage"] = float(self.mgr)
        fp.relief_role_sigma = float(self.role)
        fp.relief_pitcher_power = float(self.stuff)
        fp.relief_rest_sigma = float(self.rest)
        fp.relief_pitched2d_off_weight = float(self.p2d)
        fp.relief_pitches3d_sigma = float(self.p3d)
        fp.relief_hand_off_weight = float(self.hand)


# ---------------------------------------------------------------------------
# The per-arm recorder
# ---------------------------------------------------------------------------


@dataclass
class Rec:
    """Everything one arm records; serialized to JSON at the end."""

    #: one dict per pitching change: the facts the reads need
    changes: list[dict[str, Any]] = field(default_factory=list)
    #: boundaries visited per (manager_id, is_starter) -> [visits, changes]
    boundaries: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))
    #: per (game, iteration, side): pitchers used
    pitchers_per_side: list[int] = field(default_factory=list)
    #: per starter-game: the per-iteration lines
    starters: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: the composition: pitches by (band, tto)
    pitch_bt: np.ndarray = field(
        default_factory=lambda: np.zeros((N_PC_BAND, N_TTO), dtype=np.int64)
    )
    #: the pen source the resolver reported per game
    bullpen_sources: dict[str, str] = field(default_factory=dict)
    iter_seconds: list[float] = field(default_factory=list)
    n_pitches: int = 0
    # per-iteration scratch
    _pitches_this_iter: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    _pa: tuple[int, int, int] | None = None  # (pid, band, tto_idx)
    _game_pk: int = 0
    _iteration: int = 0

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "changes": self.changes,
            "boundaries": dict(self.boundaries),
            "pitchers_per_side": self.pitchers_per_side,
            "starters": self.starters,
            "pitch_bt": self.pitch_bt.tolist(),
            "bullpen_sources": self.bullpen_sources,
            "iter_seconds": self.iter_seconds,
            "n_pitches": self.n_pitches,
        }


def install(machine: Any, rec: Rec, state: Any) -> None:
    """Wrap the per-game machine's pull hook and pitch draw. Machines are per
    game, so a per-machine wrap never stacks (the cached SAMPLER is left alone)."""
    orig_pull = machine._maybe_pull_starter
    orig_outcome = machine._full_pool_outcome
    roles = getattr(machine.full_pool_sampler.a, "change_roles", None) or {}

    def pull(st: Any, li: float, _o: Any = orig_pull) -> None:
        defense = st.defense
        mid = st.home_manager_id if defense == Team.HOME else st.away_manager_id
        starter = st.home_starter_id if defense == Team.HOME else st.away_starter_id
        pid = st.pitcher_id
        is_starter = starter is None or (pid is not None and int(starter) == int(pid))
        key = f"{mid}:{int(is_starter)}"
        rec.boundaries[key][0] += 1
        n_before = len(machine.manager_decisions)
        _o(st, li)
        if len(machine.manager_decisions) > n_before:
            rec.boundaries[key][1] += 1
            d = dict(machine.manager_decisions[-1])
            in_pid = int(d["in_pitcher_id"])
            season = int(getattr(st, "season", 2024) or 2024)
            role = roles.get(f"{in_pid}:{season}") or roles.get(f"{in_pid}:{season - 1}")
            usage = (st.pitcher_recent_usage or {}).get(in_pid)
            d.update(
                {
                    "game_pk": rec._game_pk,
                    "iteration": rec._iteration,
                    "side": int(defense),
                    "manager_id": mid,
                    "out_is_starter": bool(is_starter),
                    "in_hand": (st.throw_hands or {}).get(in_pid),
                    "bat_hand": st.bat_hand_for(st.batter_id) if st.batter_id else None,
                    "in_hi_lev_share": None if role is None else float(role[1]),
                    "in_days_rest": None if usage is None else int(usage[0]),
                    "in_pitched_2d": None if usage is None else int(usage[1]),
                    "in_pitches_3d": None if usage is None else int(usage[2]),
                }
            )
            rec.changes.append(d)

    def outcome(st: Any, _o: Any = orig_outcome) -> Any:
        pid = int(st.pitcher_id)
        if (int(st.balls) == 0 and int(st.strikes) == 0) or rec._pa is None or rec._pa[0] != pid:
            pc_before = max(0, int(st.pitcher_pitch_count) - 1)
            tto = times_through_order(int(st.pitcher_bf.get(pid, 0)))
            band = int(np.digitize(pc_before, scan.PC_BAND_EDGES))
            rec._pa = (pid, band, min(max(tto, 1), N_TTO) - 1)
        out = _o(st)
        rec.n_pitches += 1
        rec._pitches_this_iter[pid] += 1
        rec.pitch_bt[rec._pa[1], rec._pa[2]] += 1
        return out

    machine._maybe_pull_starter = pull
    machine._full_pool_outcome = outcome


# ---------------------------------------------------------------------------
# The references: the box score and the pool
# ---------------------------------------------------------------------------


async def _pg_connect() -> Any:
    import asyncpg

    dsn = os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )
    return await asyncpg.connect(dsn, timeout=30)


async def _box_reference(seasons: list[int]) -> dict[str, Any]:
    """The official box score's usage numbers for the seasons: pitchers per
    team-game, the starters' pitches (mean, SD) and outs."""
    try:
        conn = await _pg_connect()
    except Exception as exc:  # noqa: BLE001 — the reference is optional
        print(
            f"  box reference unavailable ({type(exc).__name__}: {exc}); the 2024 constants stand in"
        )
        return {"source": "plan constants (2024)", **BOX_2024}
    try:
        row = await conn.fetchrow(
            """
            WITH tg AS (
                SELECT game_pk, team_id, COUNT(*) FILTER (WHERE played_pitch) AS n_p
                FROM raw.game_player_stats WHERE season = ANY($1::int[])
                GROUP BY game_pk, team_id
            ), st AS (
                SELECT p_pitches, p_outs FROM raw.game_player_stats
                WHERE season = ANY($1::int[]) AND p_started
            )
            SELECT (SELECT AVG(n_p) FROM tg) AS ppg,
                   (SELECT AVG(p_pitches) FROM st) AS pc_mean,
                   (SELECT STDDEV_SAMP(p_pitches) FROM st) AS pc_sd,
                   (SELECT AVG(p_outs) FROM st) AS outs_mean,
                   (SELECT COUNT(*) FROM st) AS n_starts
            """,
            seasons,
        )
        if row is None or row["n_starts"] in (None, 0):
            return {"source": "plan constants (2024)", **BOX_2024}
        return {
            "source": f"raw.game_player_stats {seasons}",
            "pitchers_per_team_game": float(row["ppg"]),
            "starter_pitches_mean": float(row["pc_mean"]),
            "starter_pitches_sd": float(row["pc_sd"]),
            "starter_outs_mean": float(row["outs_mean"]),
            "n_starts": int(row["n_starts"]),
        }
    finally:
        await conn.close()


async def _k_lines(game_pks: list[int]) -> dict[tuple[int, int], float]:
    """The closing strikeout line per (game, pitcher), when loaded."""
    try:
        conn = await _pg_connect()
    except Exception as exc:  # noqa: BLE001 — the line is optional
        print(f"  strikeout lines unavailable ({type(exc).__name__}: {exc})")
        return {}
    try:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (game_pk, player_id) game_pk, player_id, line
            FROM raw.prop_odds
            WHERE game_pk = ANY($1::int[]) AND prop_stat = 'strikeouts'
              AND line_type = 'closing'
            ORDER BY game_pk, player_id, fetched_at DESC
            """,
            game_pks,
        )
        return {(int(r["game_pk"]), int(r["player_id"])): float(r["line"]) for r in rows}
    finally:
        await conn.close()


def _pool_reference(fp: Any, manager_ids: list[int]) -> dict[str, Any]:
    """The change pool's own rates: overall per (role, boundary), per manager
    on starter rows, and the incoming arms' role, rest and hand mix."""
    pool = getattr(fp.a, "change_pool", None)
    if pool is None:
        return {}
    w = pool.recency.astype(np.float64)
    ch = pool.changed.astype(np.float64)
    st = pool.is_starter.astype(bool)
    nh = pool.new_half.astype(bool)

    def rate(mask: np.ndarray) -> float | None:
        d = float(w[mask].sum())
        return None if d <= 0 else float((w[mask] * ch[mask]).sum() / d)

    out: dict[str, Any] = {
        "rate_starter_half": rate(st & nh),
        "rate_starter_mid": rate(st & ~nh),
        "rate_reliever_half": rate(~st & nh),
        "rate_reliever_mid": rate(~st & ~nh),
        "rate_starter": rate(st),
        "rate_all": rate(np.ones(pool.n, dtype=bool)),
        "half_share_of_changes": None,
        "by_manager": {},
    }
    d = float((w * ch).sum())
    if d > 0:
        out["half_share_of_changes"] = float((w * ch)[nh].sum() / d)
    mid = getattr(pool, "manager_id", None)
    if mid is not None:
        mid = np.asarray(mid)
        for m in manager_ids:
            mask = (mid == int(m)) & st
            out["by_manager"][str(int(m))] = {
                "rate_starter": rate(mask),
                "rows_starter": int(mask.sum()),
            }
    # the incoming arms on changed rows
    changed = pool.changed.astype(bool)
    roles = getattr(fp.a, "change_roles", None) or {}
    inning = pool.sit[:, 2].astype(int)
    tiers = np.where(inning <= 6, 0, np.where(inning <= 8, 1, 2))
    role_by_tier: list[float | None] = []
    for t in range(3):
        vals = []
        for r in np.where(changed & (tiers == t))[0]:
            role = roles.get(f"{int(pool.incoming_id[r])}:{int(pool.season[r])}")
            if role is not None:
                vals.append((float(role[1]), float(w[r])))
        if vals:
            v = np.array(vals)
            role_by_tier.append(float((v[:, 0] * v[:, 1]).sum() / v[:, 1].sum()))
        else:
            role_by_tier.append(None)
    out["in_hi_lev_share_by_inning_tier"] = role_by_tier
    rest = getattr(pool, "in_days_rest", None)
    if rest is not None:
        rest = np.asarray(rest).astype(int)
        p3 = np.asarray(pool.in_pitches_3d).astype(int)
        p2 = np.asarray(pool.in_pitched_2d).astype(int)
        ok = changed & (rest >= 0)
        ww = w[ok]
        if ww.sum() > 0:
            # days_rest counts from the last appearance: 1 = pitched yesterday
            # (0 never occurs — a doubleheader's first game is excluded by design)
            out["in_rest1_share"] = float((ww * (rest[ok] == 1)).sum() / ww.sum())
            out["in_rest2_share"] = float((ww * (rest[ok] == 2)).sum() / ww.sum())
            out["in_pitched_2d_share"] = float((ww * (p2[ok] == 1)).sum() / ww.sum())
            out["in_pitches_3d_mean"] = float((ww * p3[ok]).sum() / ww.sum())
    throws = getattr(pool, "in_throws", None)
    if throws is not None:
        throws = np.asarray(throws).astype(int)
        ok = changed & (throws > 0)
        ww = w[ok]
        if ww.sum() > 0:
            out["in_left_share"] = float((ww * (throws[ok] == 1)).sum() / ww.sum())
    return out


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    arm = Arm.parse(args.arm)
    os.environ["SIM_BULLPEN_SOURCE"] = arm.pen  # the resolver's switch (this process only)
    set_path = _ROOT / "scripts" / "sim523_game_set.json"
    if args.game_pks:
        game_pks = [int(g) for g in args.game_pks]
        seasons = sorted({int(s) for s in (args.seasons or [2024])})
    else:
        with open(set_path, encoding="utf-8") as fh:
            order = json.load(fh)["order"]
        game_pks = [int(g["game_pk"]) for g in order]
        seasons = sorted({int(g["season"]) for g in order})
    if args.max_games:
        game_pks = game_pks[: args.max_games]
    print(
        f"sim427_manager_probe: arm {arm.label()}, {len(game_pks)} games x {args.iters} iterations",
        flush=True,
    )
    duck = open_sim_duckdb()
    try:
        states = [asyncio.run(_resolve(gp, duck)) for gp in game_pks]
    finally:
        if duck is not None:
            duck.close()
    k_lines = asyncio.run(_k_lines(game_pks))
    box_ref = asyncio.run(_box_reference(seasons))
    print(f"  closing strikeout lines found for {len(k_lines)} starter-games", flush=True)
    print(f"  box reference: {box_ref}", flush=True)

    # the live managers and their own starter pitch count (the tier key)
    managers: dict[str, dict[str, Any]] = {}
    for state in states:
        for mid, prof in (
            (state.home_manager_id, state.home_manager_profile),
            (state.away_manager_id, state.away_manager_profile),
        ):
            if mid is None:
                continue
            managers.setdefault(
                str(int(mid)),
                {"starter_avg_pitch_count": (prof or {}).get("starter_avg_pitch_count")},
            )

    rec = Rec()
    pool_ref: dict[str, Any] | None = None
    t0 = time.perf_counter()
    for gp, state in zip(game_pks, states, strict=True):
        kw = sim_kwargs_from_state(state)
        machine = production_machine_factory(
            0, GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
        )
        if machine.manager is None:
            raise SystemExit("SIM_MANAGER is off in this process; the probe needs the manager gate")
        arm.apply(machine)
        if pool_ref is None:
            pool_ref = _pool_reference(machine.full_pool_sampler, [int(m) for m in managers])
        install(machine, rec, state)
        rec.bullpen_sources[str(gp)] = str(getattr(state, "bullpen_source", None))
        machine._fp_pitcher_key = None
        machine._fp_pa_key = None
        starters = {
            int(Team.HOME): int(state.home_pitcher_id) if state.home_pitcher_id else None,
            int(Team.AWAY): int(state.away_pitcher_id) if state.away_pitcher_id else None,
        }
        for side, pid in starters.items():
            if pid is None:
                continue
            rec.starters[f"{gp}:{pid}"] = {
                "game_pk": int(gp),
                "pitcher_id": pid,
                "side": side,
                "k_line": k_lines.get((int(gp), pid)),
                "k": [],
                "outs": [],
                "bb": [],
                "pitches": [],
                "pulled": [],
            }
        for seed in range(args.iters):
            rec._pitches_this_iter = defaultdict(int)
            rec._pa = None
            rec._game_pk = int(gp)
            rec._iteration = seed
            machine.boxscore = BoxScore()
            machine.manager_decisions = []
            t1 = time.perf_counter()
            res = simulate_game(state_machine=machine, seed=seed, **kw)
            rec.iter_seconds.append(time.perf_counter() - t1)
            changes = [d for d in machine.manager_decisions if d["kind"] == "pitching_change"]
            for side, pid in starters.items():
                if pid is None:
                    continue
                line = res.boxscore.lines.get(pid)
                s = rec.starters[f"{gp}:{pid}"]
                s["k"].append(int(line.k) if line else 0)
                s["outs"].append(int(line.outs_recorded) if line else 0)
                s["bb"].append(int(line.bb) if line else 0)
                s["pitches"].append(int(rec._pitches_this_iter.get(pid, 0)))
                s["pulled"].append(any(int(d["out_pitcher_id"]) == pid for d in changes))
                # the side that FIELDS when this starter pitches is his own side
                n_side = 1 + sum(
                    1
                    for d in rec.changes
                    if d["game_pk"] == int(gp) and d["iteration"] == seed and d["side"] == side
                )
                rec.pitchers_per_side.append(n_side)
        print(f"  game {gp} done ({time.perf_counter() - t0:.0f}s)", flush=True)

    fp_last = machine.full_pool_sampler
    ess = getattr(fp_last, "change_ess_stats", None)
    ess_share = float(ess[0] / ess[1]) if ess is not None and ess[1] > 0 else None
    out = {
        "arm": arm.as_dict(),
        "arm_label": arm.label(),
        "change_ess_share": ess_share,
        "game_pks": game_pks,
        "seasons": seasons,
        "iters": args.iters,
        "elapsed_s": time.perf_counter() - t0,
        "env": {k: v for k, v in sorted(os.environ.items()) if k.startswith("SIM_")},
        "managers": managers,
        "box_reference": box_ref,
        "pool_reference": pool_ref or {},
        **rec.to_jsonable(),
    }
    Path(args.json_out).write_text(json.dumps(out))
    print(f"wrote {args.json_out}  ({out['elapsed_s']:.0f}s, {rec.n_pitches:,} pitches)")
    return 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _f(x: Any, w: int = 8, d: int = 2, pct: bool = False) -> str:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return f"{'—':>{w}}"
    return f"{x * 100:>{w}.{d}f}" if pct else f"{x:>{w}.{d}f}"


def _usage(a: dict[str, Any]) -> dict[str, float]:
    pulled_pc = []
    all_pc = []
    outs = []
    per_start_means = []
    for s in a["starters"].values():
        pcs = np.array(s["pitches"], dtype=float)
        all_pc.extend(pcs.tolist())
        pulled = np.array(s["pulled"], dtype=bool)
        pulled_pc.extend(pcs[pulled].tolist())
        outs.extend(s["outs"])
        per_start_means.append(float(pcs.mean()))
    changes = a["changes"]
    n_iter_games = len(a["game_pks"]) * a["iters"]
    return {
        "pitchers_per_team_game": float(np.mean(a["pitchers_per_side"]))
        if a["pitchers_per_side"]
        else float("nan"),
        "starter_pitches_mean": float(np.mean(all_pc)) if all_pc else float("nan"),
        "starter_pitches_sd": float(np.std(all_pc, ddof=1)) if len(all_pc) > 1 else float("nan"),
        "starter_pitches_sd_across_starts": float(np.std(per_start_means, ddof=1))
        if len(per_start_means) > 1
        else float("nan"),
        "starter_pulled_share": float(len(pulled_pc) / max(len(all_pc), 1)),
        "starter_outs_mean": float(np.mean(outs)) if outs else float("nan"),
        "changes_per_game": float(len(changes) / max(n_iter_games, 1)),
        "half_share_of_changes": float(np.mean([bool(d["new_half"]) for d in changes]))
        if changes
        else float("nan"),
        "draw_share": float(np.mean([d.get("source") == "draw" for d in changes]))
        if changes
        else float("nan"),
        "iter_seconds": float(np.mean(a["iter_seconds"])) if a["iter_seconds"] else float("nan"),
    }


def _manager_tiers(a: dict[str, Any]) -> dict[str, int]:
    """manager_id -> tier index by the manager's own starter pitch count
    (terciles over the set's managers with a profile)."""
    vals = {
        m: v["starter_avg_pitch_count"]
        for m, v in a["managers"].items()
        if v.get("starter_avg_pitch_count")
    }
    if len(vals) < 3:
        return {}
    xs = np.array(sorted(vals.values()))
    lo, hi = np.quantile(xs, [1 / 3, 2 / 3])
    return {m: (0 if v <= lo else 1 if v <= hi else 2) for m, v in vals.items()}


def _manager_read(a: dict[str, Any]) -> list[tuple[str, int, int, float | None, float | None]]:
    """Per tier: boundaries visited (starter on the mound), changes, the sim's
    rate per boundary, the pool's own rate on those managers' starter rows."""
    tiers = _manager_tiers(a)
    out = []
    by_mgr = a.get("pool_reference", {}).get("by_manager", {})
    for t, label in enumerate(MANAGER_TIERS):
        visits = changes = 0
        num = den = 0.0
        for m, tier in tiers.items():
            if tier != t:
                continue
            v, c = a["boundaries"].get(f"{m}:1", [0, 0])
            visits += v
            changes += c
            ref = by_mgr.get(m)
            if ref and ref.get("rate_starter") is not None and ref.get("rows_starter"):
                num += ref["rate_starter"] * ref["rows_starter"]
                den += ref["rows_starter"]
        sim_rate = changes / visits if visits else None
        pool_rate = num / den if den else None
        out.append((label, visits, changes, sim_rate, pool_rate))
    return out


def _pull_depth_by_tier(
    a: dict[str, Any],
) -> list[tuple[str, int, float | None, float | None, float | None]]:
    """Per manager tier: the sim's starter pitches at the pull (mean over the
    tier's starter pulls), the spread across the tier's starter-games, and the
    tier's own profile mean (the managers' measured ``starter_avg_pitch_count``,
    the box's number for those dugouts) — the read the usage weight moves."""
    tiers = _manager_tiers(a)
    out = []
    for t, label in enumerate(MANAGER_TIERS):
        pcs = [
            float(d["pitch_count"])
            for d in a["changes"]
            if d.get("out_is_starter") and tiers.get(str(d.get("manager_id"))) == t
        ]
        prof = [
            float(v["starter_avg_pitch_count"])
            for m, v in a["managers"].items()
            if tiers.get(m) == t and v.get("starter_avg_pitch_count")
        ]
        out.append(
            (
                label,
                len(pcs),
                float(np.mean(pcs)) if pcs else None,
                float(np.std(pcs, ddof=1)) if len(pcs) > 1 else None,
                float(np.mean(prof)) if prof else None,
            )
        )
    return out


def _reliever_read(a: dict[str, Any]) -> dict[str, Any]:
    ch = a["changes"]
    out: dict[str, Any] = {"by_inning_tier": [], "n": len(ch)}
    for lo, hi in ((0, 6), (7, 8), (9, 99)):
        vals = [
            d["in_hi_lev_share"]
            for d in ch
            if lo <= int(d["inning"]) <= hi and d.get("in_hi_lev_share") is not None
        ]
        out["by_inning_tier"].append(float(np.mean(vals)) if vals else None)
    rest = [d["in_days_rest"] for d in ch if d.get("in_days_rest") is not None]
    if rest:
        r = np.array(rest)
        out["rest1_share"] = float((r == 1).mean())
        out["rest2_share"] = float((r == 2).mean())
        out["pitched_2d_share"] = float(
            np.mean([d["in_pitched_2d"] == 1 for d in ch if d.get("in_pitched_2d") is not None])
        )
        out["pitches_3d_mean"] = float(
            np.mean([d["in_pitches_3d"] for d in ch if d.get("in_pitches_3d") is not None])
        )
    hands = [str(d["in_hand"])[:1].upper() for d in ch if d.get("in_hand")]
    if hands:
        out["left_share"] = float(np.mean([h == "L" for h in hands]))
        both = [
            (str(d["in_hand"])[:1].upper(), str(d["bat_hand"])[:1].upper())
            for d in ch
            if d.get("in_hand") and d.get("bat_hand") in ("L", "R")
        ]
        if both:
            out["same_hand_share"] = float(np.mean([p == b for p, b in both]))
    return out


def _prediction_shift(base: dict[str, Any], arm: dict[str, Any]) -> dict[str, float]:
    dk, dp, n = [], [], 0
    for key, s in arm["starters"].items():
        b = base["starters"].get(key)
        if b is None:
            continue
        line = s.get("k_line")
        if line is None:
            line = float(np.median(b["k"]))  # the baseline's own median stands in
        ka, kb = np.array(s["k"], dtype=float), np.array(b["k"], dtype=float)
        dk.append(ka.mean() - kb.mean())
        dp.append((ka > line).mean() - (kb > line).mean())
        n += 1
    return {
        "n_starter_games": n,
        "delta_k_mean": float(np.mean(dk)) if dk else float("nan"),
        "delta_k_abs_mean": float(np.mean(np.abs(dk))) if dk else float("nan"),
        "delta_p_over_mean": float(np.mean(dp)) if dp else float("nan"),
        "delta_p_over_abs_mean": float(np.mean(np.abs(dp))) if dp else float("nan"),
    }


def report(args: argparse.Namespace) -> int:
    arms = [json.load(open(p, encoding="utf-8")) for p in args.arm_json]
    ref = json.load(open(args.scan_json, encoding="utf-8")) if args.scan_json else None
    base = arms[0]
    box = base["box_reference"]
    print(
        f"=== SIM-427 manager probe — {len(base['game_pks'])} games x {base['iters']} iterations "
        f"per arm; arm 0 is the baseline ==="
    )
    for i, a in enumerate(arms):
        srcs = set(a["bullpen_sources"].values())
        ess = a.get("change_ess_share")
        ess_txt = "" if ess is None else f"; effective-sample share of the change cell {ess:.3f}"
        print(f"  arm {i}: {a['arm_label']}  (pen source per game: {sorted(srcs)}{ess_txt})")

    # --- usage ------------------------------------------------------------
    print("\n--- the usage read: the sim against the official box score ---")
    print(
        f"  box ({box.get('source')}): pitchers/team-game {box['pitchers_per_team_game']:.2f}, "
        f"starter pitches {box['starter_pitches_mean']:.1f} ± {box['starter_pitches_sd']:.1f}, "
        f"starter outs {box['starter_outs_mean']:.1f}"
    )
    pr = base.get("pool_reference", {})
    if pr:
        print(
            f"  pool: change rate starter@half {_f(pr.get('rate_starter_half'), 6, 1, True)}%  "
            f"starter@mid {_f(pr.get('rate_starter_mid'), 6, 1, True)}%  "
            f"reliever@half {_f(pr.get('rate_reliever_half'), 6, 1, True)}%  "
            f"reliever@mid {_f(pr.get('rate_reliever_mid'), 6, 1, True)}%  "
            f"half share of changes {_f(pr.get('half_share_of_changes'), 6, 1, True)}%"
        )
    hdr = f"{'arm':<40}{'P/tm-g':>8}{'SP pc':>8}{'±sd':>7}{'sd/st':>7}{'pulled%':>9}{'SP outs':>9}{'chg/g':>7}{'half%':>7}{'draw%':>7}{'s/it':>6}"
    print(hdr)
    for a in arms:
        u = _usage(a)
        print(
            f"{a['arm_label'][:39]:<40}{u['pitchers_per_team_game']:>8.2f}{u['starter_pitches_mean']:>8.1f}"
            f"{u['starter_pitches_sd']:>7.1f}{u['starter_pitches_sd_across_starts']:>7.1f}"
            f"{u['starter_pulled_share'] * 100:>9.1f}{u['starter_outs_mean']:>9.1f}{u['changes_per_game']:>7.2f}"
            f"{u['half_share_of_changes'] * 100:>7.1f}{u['draw_share'] * 100:>7.1f}{u['iter_seconds']:>6.2f}"
        )

    # --- the manager read -------------------------------------------------
    print(
        "\n--- the manager read: the change rate per boundary (a starter on the mound) by manager tier ---"
    )
    print(
        "  tiers = terciles of the live managers' own starter pitch count; the pool's rate is on those managers' own starter rows"
    )
    for a in arms:
        print(f"  {a['arm_label']}")
        for label, visits, changes, sim_rate, pool_rate in _manager_read(a):
            print(
                f"    {label:<11} boundaries {visits:>7,}  changes {changes:>6,}  "
                f"sim {_f(sim_rate, 6, 2, True)}%  pool {_f(pool_rate, 6, 2, True)}%"
            )

    print(
        "\n--- the pull-depth read: the starter's pitches at the pull by manager tier, "
        "against those managers' own measured starter pitch count ---"
    )
    for a in arms:
        print(f"  {a['arm_label']}")
        for label, n, mean, sd, prof in _pull_depth_by_tier(a):
            print(
                f"    {label:<11} pulls {n:>6,}  sim {_f(mean, 6, 1)} ± {_f(sd, 5, 1)}  "
                f"the managers' own {_f(prof, 6, 1)}"
            )

    # --- the reliever read ------------------------------------------------
    print("\n--- the reliever read: the entering arm against the pool's own incoming arms ---")
    if pr:
        tiers = pr.get("in_hi_lev_share_by_inning_tier") or [None] * 3
        print(
            f"  pool: hi-lev role share by inning {' / '.join(_f(x, 5, 2) for x in tiers)}; "
            f"pitched yesterday {_f(pr.get('in_rest1_share'), 5, 1, True)}% rest-2 {_f(pr.get('in_rest2_share'), 5, 1, True)}% "
            f"pitched-2d {_f(pr.get('in_pitched_2d_share'), 5, 1, True)}% pitches-3d {_f(pr.get('in_pitches_3d_mean'), 5, 1)}; "
            f"left {_f(pr.get('in_left_share'), 5, 1, True)}%"
        )
    for a in arms:
        r = _reliever_read(a)
        print(
            f"  {a['arm_label'][:39]:<40} n={r['n']:<6} hi-lev role by inning "
            f"{' / '.join(_f(x, 5, 2) for x in r['by_inning_tier'])}; "
            f"pitched yesterday {_f(r.get('rest1_share'), 5, 1, True)}% rest-2 {_f(r.get('rest2_share'), 5, 1, True)}% "
            f"pitched-2d {_f(r.get('pitched_2d_share'), 5, 1, True)}% pitches-3d {_f(r.get('pitches_3d_mean'), 5, 1)}; "
            f"left {_f(r.get('left_share'), 5, 1, True)}% same-hand {_f(r.get('same_hand_share'), 5, 1, True)}%"
        )

    # --- composition ------------------------------------------------------
    print(
        "\n--- the composition read: the sim's pitch share by band / TTO against the pool's row share ---"
    )
    pool_b = np.array(ref["reference_marginal"]["mass_share_band"]) if ref else None
    pool_t = np.array(ref["reference_marginal"]["mass_share_tto"]) if ref else None
    print(
        f"{'':<40}"
        + "".join(f"{b:>8}" for b in PC_BANDS)
        + "   "
        + "".join(f"{t:>7}" for t in TTO_LABELS)
    )
    if pool_b is not None:
        print(
            f"{'pool %':<40}"
            + "".join(f"{x * 100:>8.2f}" for x in pool_b)
            + "   "
            + "".join(f"{x * 100:>7.2f}" for x in pool_t)
        )
    for a in arms:
        pbt = np.array(a["pitch_bt"], dtype=float)
        sb = pbt.sum(axis=1) / pbt.sum()
        stt = pbt.sum(axis=0) / pbt.sum()
        print(
            f"{a['arm_label'][:39]:<40}"
            + "".join(f"{x * 100:>8.2f}" for x in sb)
            + "   "
            + "".join(f"{x * 100:>7.2f}" for x in stt)
        )

    # --- the prediction shift ---------------------------------------------
    print("\n--- the prediction shift per starter-game, arm minus the baseline (strikeouts) ---")
    for a in arms[1:]:
        ps = _prediction_shift(base, a)
        print(
            f"  {a['arm_label'][:39]:<40} n={ps['n_starter_games']:<4} ΔK mean {ps['delta_k_mean']:+.3f} "
            f"(|Δ| {ps['delta_k_abs_mean']:.3f})  ΔP(over line) {ps['delta_p_over_mean'] * 100:+.2f} pts "
            f"(|Δ| {ps['delta_p_over_abs_mean'] * 100:.2f})"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one arm")
    r.add_argument("--arm", required=True, help="off, or key=value pairs (see the module doc)")
    r.add_argument("--iters", type=int, default=40)
    r.add_argument("--max-games", type=int, default=None)
    r.add_argument("--seasons", type=int, nargs="*", default=None, help="with explicit game_pks")
    r.add_argument("--json-out", required=True)
    r.add_argument("game_pks", nargs="*")
    r.set_defaults(func=run)
    p = sub.add_parser("report", help="pair the arms (the first is the baseline)")
    p.add_argument("arm_json", nargs="+")
    p.add_argument("--scan-json", default=None)
    p.set_defaults(func=report)
    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
