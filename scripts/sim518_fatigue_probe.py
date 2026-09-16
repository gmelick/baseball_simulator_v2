"""
scripts/sim518_fatigue_probe.py — SIM-518: the IN-LOOP fatigue probe (plan §5.2).

The offline scan (scripts/sim518_fatigue_scan.py) says what the fatigue kernel
does to the pool's rows. The loop dilutes any factor — the situation kernel,
recency and the batter factor act on the same rows — and the manager model
decides how often the sim visits each fatigue state. So this probe runs real
games with the kernel at a candidate bandwidth and reads, per pitch and per
plate appearance:

  * THE FIT READ — the sim's outcome mix by pitch-count band and by times
    through the order, marginal and WITHIN-PITCHER (each live pitcher's mix
    per band minus his own overall mix, pooled by his pitches), against the
    pool's own within-pitcher deltas from the scan.
  * THE COMPOSITION READ — the sim's pitch share by band and TTO against the
    pool's row share. If the sim over-visits deep counts, the manager model
    (SIM-427), not the kernel, is the cause.
  * THE SURVIVORSHIP READ, sim side — the DRAWN rows' pitcher role (starter
    share) and the starters' own whiff / ball share, by live band.
  * THE PREDICTION SHIFT — per starter-game, the sim's mean strikeouts and
    its probability of clearing the closing strikeout line (or the OFF arm's
    own median when no line is loaded), so the paired accuracy run's power
    can be judged before it is scheduled (plan §6).

One process runs ONE arm (a pitch-count / TTO bandwidth pair) on the balanced
45-game set; run the arms in parallel and pair them with ``report``:

    P="MSYS_NO_PATHCONV=1 docker compose run --rm -T -v $PWD/scripts:/app/scripts app"
    $P python scripts/sim518_fatigue_probe.py run --arm 0:0    --iters 40 --json-out /app/scripts/sim518_probe_off.json &
    $P python scripts/sim518_fatigue_probe.py run --arm 0:0.5  --iters 40 --json-out /app/scripts/sim518_probe_tto05.json &
    $P python scripts/sim518_fatigue_probe.py run --arm 0:0.7  --iters 40 --json-out /app/scripts/sim518_probe_tto07.json &
    wait
    $P python scripts/sim518_fatigue_probe.py report scripts/sim518_probe_off.json \\
        scripts/sim518_probe_tto05.json scripts/sim518_probe_tto07.json \\
        --scan-json scripts/sim518_fatigue_scan.json

The arm's bandwidths are set on the process-cached sampler directly (the unit
lane's convention), never through the environment, so the production flags
the compose file sets stay exactly as they are.
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
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
for p in (str(_ROOT), str(_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np  # noqa: E402
from sim_stats import _FACTORY, _resolve, open_sim_duckdb, sim_kwargs_from_state  # noqa: E402

from simulation.batch_runner import GameSpec  # noqa: E402
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

OUTCOMES = scan.OUTCOMES
_OUT_IDX = scan._OUT_IDX
N_OUT = scan.N_OUT
CHANNELS = scan.CHANNELS
PC_BANDS = scan.PC_BANDS
N_PC_BAND = scan.N_PC_BAND
TTO_LABELS = scan.TTO_LABELS
N_TTO = scan.N_TTO
#: Plate-appearance terminals the probe classifies from the drawn pitch.
PA_ENDS: tuple[str, ...] = ("K", "BB", "HBP", "BIP")
#: Pitch-depth strata for the prediction shift (the OFF arm's mean starter
#: pitch count per game).
DEPTH_EDGES: tuple[int, ...] = (75, 100)
DEPTH_LABELS: tuple[str, ...] = ("<75", "75-99", "100+")


# ---------------------------------------------------------------------------
# The per-arm recorder
# ---------------------------------------------------------------------------


class Rec:
    """Everything one arm records; serialized to JSON at the end."""

    def __init__(self) -> None:
        # pitch outcome counts by (band, tto, out); pitch counts by (band, tto)
        self.pitch_bt = np.zeros((N_PC_BAND, N_TTO, N_OUT), dtype=np.int64)
        # the same counts split by iteration parity (the seed-split check)
        self.pitch_bt_parity = np.zeros((2, N_PC_BAND, N_TTO, N_OUT), dtype=np.int64)
        self.parity = 0
        # plate-appearance terminals by (band, tto, end); PA counts by (band, tto)
        self.pa_bt = np.zeros((N_PC_BAND, N_TTO, len(PA_ENDS)), dtype=np.int64)
        self.pa_n_bt = np.zeros((N_PC_BAND, N_TTO), dtype=np.int64)
        # within-pitcher: per live pitcher key -> (band, out) and (tto, out) counts
        self.pitch_by_pitcher_band: dict[str, np.ndarray] = defaultdict(
            lambda: np.zeros((N_PC_BAND, N_OUT), dtype=np.int64)
        )
        self.pitch_by_pitcher_tto: dict[str, np.ndarray] = defaultdict(
            lambda: np.zeros((N_TTO, N_OUT), dtype=np.int64)
        )
        # survivorship, sim side: drawn-row quality sums by live band
        # [count, starter, whiff, ball, whiff*starter, ball*starter]
        self.drawn_quality = np.zeros((N_PC_BAND, 6), dtype=np.float64)
        # per (game_pk, pitcher_id) starter lines per iteration
        self.starters: dict[str, dict[str, list]] = {}
        # per-iteration pitch counts per pitcher (this game / this iteration)
        self._pitches_this_iter: dict[int, int] = defaultdict(int)
        # the current PA's snapshot
        self._pa: tuple[str, int, int, int] | None = None  # (key, pid, band, tto_idx)
        self.iter_seconds: list[float] = []
        self.n_pitches = 0

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "pitch_bt": self.pitch_bt.tolist(),
            "pitch_bt_parity": self.pitch_bt_parity.tolist(),
            "pa_bt": self.pa_bt.tolist(),
            "pa_n_bt": self.pa_n_bt.tolist(),
            "pitch_by_pitcher_band": {k: v.tolist() for k, v in self.pitch_by_pitcher_band.items()},
            "pitch_by_pitcher_tto": {k: v.tolist() for k, v in self.pitch_by_pitcher_tto.items()},
            "drawn_quality": self.drawn_quality.tolist(),
            "starters": self.starters,
            "iter_seconds": self.iter_seconds,
            "n_pitches": self.n_pitches,
        }


def _row_quality_by_hand(fp: Any) -> dict[str, np.ndarray]:
    """Per hand: the pool rows' pitcher quality columns (the scan's
    ``pitcher_quality``: whiff, ball, starter, whiff×starter, ball×starter)."""
    out: dict[str, np.ndarray] = {}
    for hand, pool in fp.a.pools.items():
        key = pool.pitcher_id.astype(np.int64) * 10_000 + pool.season.astype(np.int64)
        codes = scan.outcome_codes(pool.outcome_type)
        tto = getattr(pool, "tto", None)
        out[hand] = scan.pitcher_quality(
            key,
            codes,
            pool.recency.astype(np.float64),
            None if tto is None else np.asarray(tto),
        )
    return out


def install(machine: Any, rec: Rec, quality: dict[str, np.ndarray]) -> None:
    """Wrap the per-game machine's pitch draw. Machines are per game, so a
    per-machine wrap never stacks (the cached SAMPLER is left alone)."""
    fp = machine.full_pool_sampler
    orig = machine._full_pool_outcome

    def outcome(state: Any, _o: Any = orig) -> Any:
        pid = int(state.pitcher_id)
        season = int(getattr(state, "season", 2024) or 2024)
        if (
            (int(state.balls) == 0 and int(state.strikes) == 0)
            or rec._pa is None
            or rec._pa[1] != pid
        ):
            # The PA's first pitch: pitches BEFORE the PA (the counter was
            # already incremented for this pitch) and the TTO entering it —
            # the loop's own snapshot rule.
            pc_before = max(0, int(state.pitcher_pitch_count) - 1)
            tto = times_through_order(int(state.pitcher_bf.get(pid, 0)))
            band = int(np.digitize(pc_before, scan.PC_BAND_EDGES))
            rec._pa = (f"{pid}:{season}", pid, band, min(max(tto, 1), N_TTO) - 1)
            rec.pa_n_bt[band, rec._pa[3]] += 1
        key, _pid, band, t = rec._pa
        out = _o(state)
        oi = _OUT_IDX.get(str(out), -1)
        rec.n_pitches += 1
        rec._pitches_this_iter[pid] += 1
        if oi >= 0:
            rec.pitch_bt[band, t, oi] += 1
            rec.pitch_bt_parity[rec.parity, band, t, oi] += 1
            rec.pitch_by_pitcher_band[key][band, oi] += 1
            rec.pitch_by_pitcher_tto[key][t, oi] += 1
            end = -1
            if oi in (1, 2) and int(state.strikes) >= 2:
                end = 0
            elif oi == 0 and int(state.balls) >= 3:
                end = 1
            elif oi == 5:
                end = 2
            elif oi == 4:
                end = 3
            if end >= 0:
                rec.pa_bt[band, t, end] += 1
        # The drawn row's pitcher: role and quality, by the LIVE band.
        i = getattr(fp, "_pp_last_i", None)
        hand = getattr(fp, "_hand", None)
        if i is not None and hand in quality:
            q = quality[hand][int(i)]
            rec.drawn_quality[band, 0] += 1.0
            rec.drawn_quality[band, 1] += q[2]
            rec.drawn_quality[band, 2] += q[0]
            rec.drawn_quality[band, 3] += q[1]
            rec.drawn_quality[band, 4] += q[3]
            rec.drawn_quality[band, 5] += q[4]
        return out

    machine._full_pool_outcome = outcome


async def _k_lines(game_pks: list[int]) -> dict[tuple[int, int], float]:
    """The closing strikeout line per (game, pitcher) from raw.prop_odds,
    when loaded; {} when the database is unreachable."""
    try:
        import asyncpg

        dsn = os.environ.get(
            "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
        )
        conn = await asyncpg.connect(dsn, timeout=30)
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


def _parse_arm(text: str) -> tuple[float, float]:
    pc, tto = text.split(":")
    return float(pc), float(tto)


def run(args: argparse.Namespace) -> int:
    pc_sigma, tto_sigma = _parse_arm(args.arm)
    set_path = _ROOT / "scripts" / "sim523_game_set.json"
    if args.game_pks:
        game_pks = [int(g) for g in args.game_pks]
    else:
        with open(set_path, encoding="utf-8") as fh:
            game_pks = [int(g["game_pk"]) for g in json.load(fh)["order"]]
    if args.max_games:
        game_pks = game_pks[: args.max_games]
    print(
        f"sim518_fatigue_probe: arm pc={pc_sigma:g} tto={tto_sigma:g}, "
        f"{len(game_pks)} games x {args.iters} iterations",
        flush=True,
    )
    duck = open_sim_duckdb()
    try:
        states = [asyncio.run(_resolve(gp, duck)) for gp in game_pks]
    finally:
        if duck is not None:
            duck.close()
    k_lines = asyncio.run(_k_lines(game_pks))
    print(f"  closing strikeout lines found for {len(k_lines)} starter-games", flush=True)

    rec = Rec()
    quality: dict[str, np.ndarray] | None = None
    t0 = time.perf_counter()
    for gp, state in zip(game_pks, states, strict=True):
        kw = sim_kwargs_from_state(state)
        machine = production_machine_factory(
            0, GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
        )
        fp = machine.full_pool_sampler
        # The arm, set on the cached sampler directly (never the env).
        fp.fatigue_pc_sigma = float(pc_sigma)
        fp.fatigue_tto_sigma = float(tto_sigma)
        if quality is None:
            quality = _row_quality_by_hand(fp)
        install(machine, rec, quality)
        machine._fp_pitcher_key = None
        machine._fp_pa_key = None
        starters = [
            int(x)
            for x in (
                getattr(state, "home_pitcher_id", None),
                getattr(state, "away_pitcher_id", None),
            )
            if x
        ]
        for pid in starters:
            rec.starters[f"{gp}:{pid}"] = {
                "game_pk": int(gp),
                "pitcher_id": pid,
                "k_line": k_lines.get((int(gp), pid)),
                "k": [],
                "outs": [],
                "bb": [],
                "pitches": [],
            }
        for seed in range(args.iters):
            rec._pitches_this_iter = defaultdict(int)
            rec._pa = None
            rec.parity = seed % 2
            machine.boxscore = BoxScore()
            t1 = time.perf_counter()
            res = simulate_game(state_machine=machine, seed=seed, **kw)
            rec.iter_seconds.append(time.perf_counter() - t1)
            for pid in starters:
                line = res.boxscore.lines.get(pid)
                s = rec.starters[f"{gp}:{pid}"]
                s["k"].append(int(line.k) if line else 0)
                s["outs"].append(int(line.outs_recorded) if line else 0)
                s["bb"].append(int(line.bb) if line else 0)
                s["pitches"].append(int(rec._pitches_this_iter.get(pid, 0)))
        print(f"  game {gp} done ({time.perf_counter() - t0:.0f}s)", flush=True)

    out = {
        "arm": {"pc_sigma": pc_sigma, "tto_sigma": tto_sigma},
        "game_pks": game_pks,
        "iters": args.iters,
        "elapsed_s": time.perf_counter() - t0,
        "env": {k: v for k, v in sorted(os.environ.items()) if k.startswith("SIM_")},
        **rec.to_jsonable(),
    }
    Path(args.json_out).write_text(json.dumps(out))
    print(f"wrote {args.json_out}  ({out['elapsed_s']:.0f}s, {rec.n_pitches:,} pitches)")
    return 0


# ---------------------------------------------------------------------------
# The report: pair the arms
# ---------------------------------------------------------------------------


def _mix(counts: np.ndarray) -> np.ndarray:
    tot = counts.sum(axis=-1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(tot > 0, counts / np.where(tot > 0, tot, 1.0), np.nan)


def _within_pitcher(by_pitcher: dict[str, list], n_axis: int, min_rows: int = 20) -> np.ndarray:
    """The scan's B construction on the sim's own pitches: per live pitcher,
    the mix per band minus his overall mix, pooled by his pitches; NaN where
    no pitcher qualifies."""
    num = np.zeros((n_axis, N_OUT))
    den = np.zeros(n_axis)
    for arr in by_pitcher.values():
        a = np.asarray(arr, dtype=np.float64)
        n = a.sum(axis=1)
        ok = n >= min_rows
        if ok.sum() < 2:
            continue
        overall = a.sum(axis=0) / max(a.sum(), 1.0)
        mix = _mix(a)
        for x in np.where(ok)[0]:
            num[x] += (mix[x] - overall) * n[x]
            den[x] += n[x]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den[:, None] > 0, num / np.where(den > 0, den, 1.0)[:, None], np.nan)


def _fmt(x: float, w: int = 8) -> str:
    return f"{'nan':>{w}}" if x is None or not np.isfinite(x) else f"{x * 100:>+{w}.2f}"


def report(args: argparse.Namespace) -> int:
    arms = [json.load(open(p, encoding="utf-8")) for p in args.arm_json]
    ref = json.load(open(args.scan_json, encoding="utf-8")) if args.scan_json else None
    off = arms[0]
    print(
        f"=== SIM-518 in-loop fatigue probe — {len(off['game_pks'])} games x {off['iters']} "
        f"iterations per arm; arm 0 is the OFF baseline ==="
    )
    for a in arms:
        secs = np.array(a["iter_seconds"])
        print(
            f"  arm pc={a['arm']['pc_sigma']:g} tto={a['arm']['tto_sigma']:g}: "
            f"{a['n_pitches']:,} pitches, {secs.mean():.2f} s/iteration"
        )

    # --- composition ------------------------------------------------------
    print("\n--- composition: the sim's pitch share by band / TTO against the pool's row share ---")
    pbt = np.array(off["pitch_bt"]).sum(axis=2)
    sim_b = pbt.sum(axis=1) / pbt.sum()
    sim_t = pbt.sum(axis=0) / pbt.sum()
    pool_b = np.array(ref["reference_marginal"]["mass_share_band"]) if ref else None
    pool_t = np.array(ref["reference_marginal"]["mass_share_tto"]) if ref else None
    print(f"{'band':<8}" + "".join(f"{b:>9}" for b in PC_BANDS))
    print(f"{'sim %':<8}" + "".join(f"{x * 100:>9.2f}" for x in sim_b))
    if pool_b is not None:
        print(f"{'pool %':<8}" + "".join(f"{x * 100:>9.2f}" for x in pool_b))
    print(f"{'tto':<8}" + "".join(f"{t:>9}" for t in TTO_LABELS))
    print(f"{'sim %':<8}" + "".join(f"{x * 100:>9.2f}" for x in sim_t))
    if pool_t is not None:
        print(f"{'pool %':<8}" + "".join(f"{x * 100:>9.2f}" for x in pool_t))

    # --- the fit read -----------------------------------------------------
    print(
        "\n--- the fit read: the sim's WITHIN-PITCHER delta by band and TTO (points of share) "
        "against the pool's own (B) ---"
    )
    for label, axis_n, key, ref_key, labels in (
        ("by times through the order", N_TTO, "pitch_by_pitcher_tto", "delta_tto", TTO_LABELS),
        ("by pitch-count band", N_PC_BAND, "pitch_by_pitcher_band", "delta_band", PC_BANDS),
    ):
        print(f"\n  {label}")
        print(f"  {'channel':<9}{'arm':>10}" + "".join(f"{x:>9}" for x in labels))
        for ch, o in CHANNELS.items():
            if ref is not None:
                b = ref["reference_within"][ref_key][ch]
                print(f"  {ch:<9}{'pool B':>10}" + "".join(_fmt(x, 9) for x in b))
            for a in arms:
                d = _within_pitcher(a[key], axis_n)
                tag = f"{a['arm']['pc_sigma']:g}/{a['arm']['tto_sigma']:g}"
                print(f"  {'':<9}{tag:>10}" + "".join(_fmt(x, 9) for x in d[:, o]))
        # the marginal read too (composition-affected)
        print(f"  {'':<9}{'(marginal)':>10}")
        for ch, o in CHANNELS.items():
            for a in arms:
                cnt = np.array(a["pitch_bt"])
                m = cnt.sum(axis=1) if key.endswith("band") else cnt.sum(axis=0)
                mix = _mix(m)
                allm = cnt.sum(axis=(0, 1)) / cnt.sum()
                tag = f"{a['arm']['pc_sigma']:g}/{a['arm']['tto_sigma']:g}"
                print(f"  {ch:<9}{tag:>10}" + "".join(_fmt(x, 9) for x in (mix[:, o] - allm[o])))

    # --- PA terminals by TTO ---------------------------------------------
    print("\n--- plate-appearance terminals per PA by TTO (K / BB / HBP / in play), points ---")
    for a in arms:
        pa = np.array(a["pa_bt"]).sum(axis=0)
        n = np.array(a["pa_n_bt"]).sum(axis=0)
        tag = f"{a['arm']['pc_sigma']:g}/{a['arm']['tto_sigma']:g}"
        for e, name in enumerate(PA_ENDS):
            with np.errstate(invalid="ignore", divide="ignore"):
                rate = np.where(n > 0, pa[:, e] / np.where(n > 0, n, 1), np.nan)
            print(f"  {tag:>8} {name:<4}" + "".join(f"{x * 100:>9.2f}" for x in rate))

    # --- survivorship, sim side ------------------------------------------
    print(
        "\n--- survivorship (sim side): the DRAWN rows' starter share and the starters' own whiff / "
        "ball share, by live band; delta from the OFF arm in points ---"
    )
    q_off = np.array(off["drawn_quality"])
    print(f"  {'arm':>8} {'read':<14}" + "".join(f"{b:>9}" for b in PC_BANDS))
    for a in arms:
        q = np.array(a["drawn_quality"])
        with np.errstate(invalid="ignore", divide="ignore"):
            share = q[:, 1] / q[:, 0]
            share0 = q_off[:, 1] / q_off[:, 0]
            wst = q[:, 4] / q[:, 1]
            wst0 = q_off[:, 4] / q_off[:, 1]
            bst = q[:, 5] / q[:, 1]
            bst0 = q_off[:, 5] / q_off[:, 1]
        tag = f"{a['arm']['pc_sigma']:g}/{a['arm']['tto_sigma']:g}"
        print(f"  {tag:>8} {'starter %':<14}" + "".join(f"{x * 100:>9.1f}" for x in share))
        print(f"  {'':>8} {'  d role':<14}" + "".join(_fmt(x, 9) for x in share - share0))
        print(f"  {'':>8} {'  d st.whiff':<14}" + "".join(_fmt(x, 9) for x in wst - wst0))
        print(f"  {'':>8} {'  d st.ball':<14}" + "".join(_fmt(x, 9) for x in bst - bst0))

    # --- the prediction shift ---------------------------------------------
    print(
        "\n--- the prediction shift per starter-game (arm minus OFF): mean strikeouts, P(K over the "
        "line), mean outs; by the OFF arm's mean pitch depth ---"
    )
    off_st = off["starters"]
    for a in arms[1:]:
        tag = f"{a['arm']['pc_sigma']:g}/{a['arm']['tto_sigma']:g}"
        rows: dict[str, list[tuple[float, float, float, float]]] = defaultdict(list)
        n_lines = 0
        for key, s in a["starters"].items():
            s0 = off_st.get(key)
            if s0 is None or not s0["k"]:
                continue
            k1, k0 = np.array(s["k"], dtype=float), np.array(s0["k"], dtype=float)
            o1, o0 = np.array(s["outs"], dtype=float), np.array(s0["outs"], dtype=float)
            depth = float(np.mean(s0["pitches"]))
            line = s0.get("k_line")
            if line is None:
                line = float(np.median(k0)) + 0.5
            else:
                n_lines += 1
            stratum = DEPTH_LABELS[int(np.digitize(depth, DEPTH_EDGES))]
            rec_row = (
                float(k1.mean() - k0.mean()),
                float((k1 > line).mean() - (k0 > line).mean()),
                float(o1.mean() - o0.mean()),
                float(abs((k1 > line).mean() - (k0 > line).mean())),
            )
            rows[stratum].append(rec_row)
            rows["all"].append(rec_row)
        print(
            f"  arm {tag}: {n_lines} starter-games had a closing K line (else the OFF median + 0.5)"
        )
        print(
            f"  {'depth':<8}{'n':>5}{'dK mean':>10}{'dP(over)':>10}{'|dP|':>8}{'dOuts':>9}"
            f"{'se dP':>8}"
        )
        for stratum in (*DEPTH_LABELS, "all"):
            r = np.array(rows.get(stratum, []))
            if r.size == 0:
                print(f"  {stratum:<8}{0:>5}")
                continue
            se = float(r[:, 1].std(ddof=1) / np.sqrt(len(r))) if len(r) > 1 else float("nan")
            print(
                f"  {stratum:<8}{len(r):>5}{r[:, 0].mean():>+10.3f}{r[:, 1].mean():>+10.3f}"
                f"{r[:, 3].mean():>8.3f}{r[:, 2].mean():>+9.3f}{se:>8.3f}"
            )

    # --- seed split on the 3rd-TTO marginal deltas -------------------------
    print(
        "\n--- seed split: the 3rd-TTO marginal delta (points) on even vs odd iterations, "
        "per arm — the two halves should agree within their noise ---"
    )
    print(f"  {'arm':>8}{'half':>6}" + "".join(f"{ch:>9}" for ch in CHANNELS))
    for a in arms:
        tag = f"{a['arm']['pc_sigma']:g}/{a['arm']['tto_sigma']:g}"
        par = np.array(a.get("pitch_bt_parity", []))
        if par.size == 0:
            continue
        for h, name in enumerate(("even", "odd")):
            cnt = par[h].sum(axis=0)  # (tto, out)
            mix = _mix(cnt)
            allm = cnt.sum(axis=0) / max(cnt.sum(), 1)
            print(
                f"  {tag:>8}{name:>6}"
                + "".join(_fmt(mix[2, o] - allm[o], 9) for o in CHANNELS.values())
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one arm")
    r.add_argument("--arm", required=True, help="pc_sigma:tto_sigma, e.g. 0:0.5 (0:0 = OFF)")
    r.add_argument("--iters", type=int, default=40)
    r.add_argument("--max-games", type=int, default=None)
    r.add_argument("--json-out", required=True)
    r.add_argument("game_pks", nargs="*")
    r.set_defaults(func=run)
    p = sub.add_parser("report", help="pair the arms (the first is the OFF baseline)")
    p.add_argument("arm_json", nargs="+")
    p.add_argument("--scan-json", default=None)
    p.set_defaults(func=report)
    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
