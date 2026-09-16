"""
scripts/sim548_design.py — SIM-548 §5.3: the DESIGNED EXPERIMENT on the full
simulator. The script runs a set of arms of the accuracy comparison in
sequence, pairs them against the design's baseline, and reads the result as a
response surface.

The design. ``--factors`` names up to six weights. Each factor carries two
levels (production and the offline fit's candidate) and an optional third, the
mid level. Up to four factors run the full factorial. Five factors run the
2^(5-1) half fraction with E=ABCD: resolution V, so every main effect and every
two-way interaction is clear. Six factors run the 2^(6-2) quarter fraction with
E=ABC and F=BCD: resolution IV, so every main effect is clear of the two-way
interactions, but the two-way interactions are aliased in groups (six pairs
and one triple). The read fits ONE column per alias group and names the group
(``A×B=C×E``); the offline surface breaks the alias.

Two kinds of extra arm join the factorial runs:

* ``--baseline-repeats N`` runs production (every factor at its low level)
  under N different seeds. Their seeds start at ``base_seed + 1000``, so no
  repeat shares the factorial runs' seed. The spread of the per-market Brier
  across the repeats is the noise floor.
* ``--centre-arms N`` runs N true centre points (every factor at its mid
  level) under seeds ``base_seed + 2000 + c``. They need a mid level on every
  factor. The curvature is mean(factorial corners) − mean(centre arms); without
  centre arms the curvature is None.

Each arm is one ``scripts/clv_backtest.py`` run on the same game list and the
same bundle, with the arm's environment settings exported. An arm whose report
exists is skipped (the run resumes). Then the read, per market on THAT
market's common games (the games present in every loaded arm): the main
effect of each factor, one effect per alias group of two-way interactions and
the curvature, each with a game-clustered bootstrap range; the noise floor
from the baseline repeats; the composite Z (every market's effect in units of
its own noise) and the surface's best corner.

    # generate + run inside the app container
    MSYS_NO_PATHCONV=1 docker compose run -d --rm --name sim548_design \\
        -v "$PWD/scripts:/app/scripts" app python scripts/sim548_design.py \\
        --factors '{"SIM_PITCH_PITCHER_POWER": ["16", "8"], "SIM_RESULT_BATTER_POWER": ["8", "4"]}' \\
        --game-pks-file scripts/sim548_games_2024_design.txt --out-dir /app/scripts/sim548_design

    # read existing reports only
    python scripts/sim548_design.py --analyze-only --out-dir scripts/sim548_design
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
for p in (str(_ROOT), str(_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from sim548_market_skill import load_reports  # noqa: E402

#: Generators for the 2^(k-p) fractions, k = 5 (resolution V) and 6 (resolution IV).
#: k <= 4 runs the full factorial.
GENERATORS: dict[int, dict[str, str]] = {
    5: {"E": "ABCD"},
    6: {"E": "ABC", "F": "BCD"},
}
LETTERS = "ABCDEF"

#: Seed offsets. The baseline repeats never share the factorial runs' seed.
#: The backtest seeds iteration i of every game as base_seed + i, so two arms
#: whose base seeds differ by less than the iteration count share iterations.
#: The repeats stride by the iteration count, from an offset past the factorial's.
BASELINE_SEED_OFFSET = 100_000
CENTRE_SEED_OFFSET = 200_000


# ---------------------------------------------------------------------------
# The design
# ---------------------------------------------------------------------------


def fractional_factorial(k: int) -> np.ndarray:
    """Rows of ±1 for k factors. Up to four factors give the full factorial.
    Five factors give the resolution-V half fraction. Six factors give the
    resolution-IV quarter fraction."""
    if k <= 4:
        return np.array(list(itertools.product([-1, 1], repeat=k)), dtype=int)
    base = 4
    rows = np.array(list(itertools.product([-1, 1], repeat=base)), dtype=int)
    cols = [rows[:, i] for i in range(base)]
    for gen in GENERATORS[k].values():
        prod = np.ones(rows.shape[0], dtype=int)
        for g in gen:
            prod = prod * cols[LETTERS.index(g)]
        cols.append(prod)
    return np.column_stack(cols)


def arm_kind(arm: dict[str, Any]) -> str:
    """The arm's kind: ``factorial``, ``baseline`` or ``centre``. An old
    design.json marks its production repeats ``centre: True``; those are
    baseline repeats, not centre points."""
    kind = arm.get("kind")
    if kind:
        return str(kind)
    return "baseline" if arm.get("centre") else "factorial"


def build_arms(
    factors: dict[str, list[str]],
    baseline_repeats: int,
    base_seed: int,
    centre_arms: int = 0,
    iterations: int = 100,
) -> list[dict[str, Any]]:
    """The arms of the design: the factorial runs, then the baseline repeats,
    then the centre arms. A factor's levels are ``[low, high]`` or
    ``[low, high, mid]``. Centre arms need a mid level on every factor."""
    names = list(factors)
    k = len(names)
    if k == 0 or k > 6:
        raise SystemExit("--factors needs 1 to 6 weights")
    for n in names:
        if len(factors[n]) not in (2, 3):
            raise SystemExit(f"--factors: {n} needs [low, high] or [low, high, mid]")
    if centre_arms > 0 and any(len(factors[n]) < 3 for n in names):
        missing = [n for n in names if len(factors[n]) < 3]
        raise SystemExit(f"--centre-arms needs a mid level on every factor; missing on {missing}")
    design = fractional_factorial(k)
    arms: list[dict[str, Any]] = []
    for r, row in enumerate(design):
        env = {
            names[i]: (factors[names[i]][1] if row[i] > 0 else factors[names[i]][0])
            for i in range(k)
        }
        arms.append(
            {
                "name": f"run{r + 1:02d}",
                "coded": row.tolist(),
                "env": env,
                "seed": base_seed,
                "kind": "factorial",
            }
        )
    for c in range(baseline_repeats):
        env = {n: factors[n][0] for n in names}
        arms.append(
            {
                "name": f"baseline{c + 1}",
                "coded": [-1] * k,
                "env": env,
                "seed": base_seed + BASELINE_SEED_OFFSET + c * max(1, iterations),
                "kind": "baseline",
            }
        )
    for c in range(centre_arms):
        env = {n: factors[n][2] for n in names}
        arms.append(
            {
                "name": f"centre{c + 1}",
                "coded": [0] * k,
                "env": env,
                "seed": base_seed + CENTRE_SEED_OFFSET + c * max(1, iterations),
                "kind": "centre",
            }
        )
    return arms


# ---------------------------------------------------------------------------
# Running the arms
# ---------------------------------------------------------------------------


def run_arm(
    arm: dict[str, Any],
    out_dir: Path,
    game_pks_file: str,
    iterations: int,
    workers: int,
    seasons: str,
) -> Path:
    report = out_dir / f"{arm['name']}.json"
    if report.exists():
        print(f"  {arm['name']}: report exists, skipped", flush=True)
        return report
    env = dict(os.environ)
    env.update({k: str(v) for k, v in arm["env"].items()})
    cmd = [
        sys.executable,
        str(_ROOT / "scripts" / "clv_backtest.py"),
        "--seasons",
        seasons,
        "--iterations",
        str(iterations),
        "--workers",
        str(workers),
        "--base-seed",
        str(arm["seed"]),
        "--game-pks-file",
        game_pks_file,
        "--output",
        str(report),
    ]
    log = out_dir / f"{arm['name']}.run.log"
    t0 = time.time()
    print(
        f"  {arm['name']}: start {time.strftime('%FT%TZ', time.gmtime())} env={arm['env']} seed={arm['seed']}",
        flush=True,
    )
    with open(log, "w", encoding="utf-8") as fh:
        rc = subprocess.call(cmd, env=env, stdout=fh, stderr=subprocess.STDOUT, cwd=str(_ROOT))
    print(f"  {arm['name']}: exit {rc} after {(time.time() - t0) / 3600:.2f} h", flush=True)
    return report


# ---------------------------------------------------------------------------
# The read
# ---------------------------------------------------------------------------


def _per_game_market_brier(
    records: list[dict[str, Any]],
) -> dict[str, dict[int, tuple[float, int]]]:
    """market -> game -> (sum of Brier, n records)."""
    out: dict[str, dict[int, list[float]]] = {}
    for r in records:
        m = str(r["market"])
        g = int(r["game_pk"])
        b = (float(r["sim_prob"]) - float(r["outcome"])) ** 2
        cell = out.setdefault(m, {}).setdefault(g, [0.0, 0])
        cell[0] += b
        cell[1] += 1
    return {m: {g: (v[0], v[1]) for g, v in d.items()} for m, d in out.items()}


def alias_groups(X: np.ndarray, names: list[str]) -> tuple[list[str], list[np.ndarray], list[str]]:
    """Group the two-way product columns of the design ``X`` into alias
    groups. Two product columns are aliased when they are equal, or negatives
    of each other, across the design rows. Returns the group names (for
    example ``A×B=C×E``), one column per group, and the names of the product
    columns dropped because they alias the intercept or a main effect."""
    k = X.shape[1]
    n = X.shape[0]
    mains = [X[:, i] for i in range(k)]
    group_names: list[str] = []
    group_cols: list[np.ndarray] = []
    dropped: list[str] = []
    for i, j in itertools.combinations(range(k), 2):
        col = X[:, i] * X[:, j]
        label = f"{names[i]}×{names[j]}"
        dot_one = abs(int(col.sum()))
        if dot_one == n or any(abs(int(col @ m)) == n for m in mains):
            dropped.append(label)
            continue
        placed = False
        for gi, rep in enumerate(group_cols):
            d = int(col @ rep)
            if d == n:
                group_names[gi] += f"={label}"
                placed = True
                break
            if d == -n:
                group_names[gi] += f"=−{label}"
                placed = True
                break
        if not placed:
            group_names.append(label)
            group_cols.append(col.astype(float))
    return group_names, group_cols, dropped


def analyze(
    arms: list[dict[str, Any]],
    out_dir: Path,
    min_n: int,
    n_boot: int,
    seed: int,
    weights: dict[str, float] | None,
) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    loaded = []
    for arm in arms:
        path = out_dir / f"{arm['name']}.json"
        if not path.exists():
            continue
        recs, _prov = load_reports([str(path)], force=True)
        loaded.append((arm, _per_game_market_brier(recs)))
    if not loaded:
        raise SystemExit("no reports to read")
    names = list(arms[0]["env"])
    k = len(names)
    factorial = [(a, d) for a, d in loaded if arm_kind(a) == "factorial"]
    baselines = [(a, d) for a, d in loaded if arm_kind(a) == "baseline"]
    centres = [(a, d) for a, d in loaded if arm_kind(a) == "centre"]
    # a fraction needs every factorial run: a missing row breaks the alias structure
    wanted = [a["name"] for a in arms if arm_kind(a) == "factorial"]
    have = {a["name"] for a, _d in factorial}
    missing = [n for n in wanted if n not in have]
    if missing:
        if k >= 5:
            raise SystemExit(
                f"REFUSED — the 2^({k}-{k - 4}) fraction needs every factorial report; "
                f"missing: {missing}"
            )
        print(f"WARNING — factorial reports missing: {missing}", file=sys.stderr)
    if not factorial:
        raise SystemExit("no factorial reports to read")
    all_markets = set.union(*[set(d) for _a, d in loaded])
    markets = sorted(set.intersection(*[set(d) for _a, d in loaded]))
    absent = sorted(all_markets - set(markets))
    X = np.array([a["coded"] for a, _d in factorial], dtype=float)  # [n_runs, k]
    # design matrix: intercept, main effects, one column per alias group of two-way products
    group_names, group_cols, dropped = alias_groups(X, names)
    design = np.column_stack([np.ones(len(X))] + [X[:, i] for i in range(k)] + group_cols)
    coef_names = ["mean"] + [names[i] for i in range(k)] + group_names
    if np.linalg.matrix_rank(design) < design.shape[1]:
        raise SystemExit(
            f"REFUSED — the {len(X)} factorial reports cannot separate the {design.shape[1]} terms "
            f"(missing: {missing}); run the missing arms first"
        )
    result: dict[str, Any] = {
        "games_per_market": {},
        "markets": {},
        "names": names,
        "interaction_groups": group_names,
        "dropped_interactions": dropped,
        "skipped_markets": dict.fromkeys(absent, "absent from at least one arm's report"),
    }

    def sums_on(
        rows: list[tuple[dict[str, Any], dict[str, dict[int, tuple[float, int]]]]],
        m: str,
        games: list[int],
    ) -> tuple[np.ndarray, np.ndarray]:
        """Per arm and game: the Brier sum and the record count. Shape [arms, games]."""
        num = np.array([[d[m][g][0] for g in games] for _a, d in rows], dtype=float)
        den = np.array([[d[m][g][1] for g in games] for _a, d in rows], dtype=float)
        return num.reshape(len(rows), -1), den.reshape(len(rows), -1)

    def means(num: np.ndarray, den: np.ndarray, idx: np.ndarray) -> np.ndarray:
        # mean Brier over the records of the sampled games (ratio of sums)
        return num[:, idx].sum(axis=1) / np.maximum(den[:, idx].sum(axis=1), 1e-12)

    per_market_effects: dict[str, dict[str, Any]] = {}
    z_terms_point: dict[str, np.ndarray] = {}
    z_terms_boot: dict[str, np.ndarray] = {}
    for m in markets:
        # this market's common games: present in every loaded arm
        games = sorted(set.intersection(*[set(d[m]) for _a, d in loaded]))
        G = len(games)
        if G == 0:
            result["skipped_markets"][m] = "no common games"
            continue
        n_rec = int(sum(loaded[0][1][m][g][1] for g in games))
        if n_rec < min_n:
            result["skipped_markets"][m] = f"{n_rec} records on the common games < min_n {min_n}"
            continue
        result["games_per_market"][m] = G
        f_num, f_den = sums_on(factorial, m, games)
        b_num, b_den = sums_on(baselines, m, games) if baselines else (None, None)
        c_num, c_den = sums_on(centres, m, games) if centres else (None, None)
        idx_all = np.arange(G)
        y = means(f_num, f_den, idx_all)
        base_vals = means(b_num, b_den, idx_all) if baselines else np.array([])
        centre_vals = means(c_num, c_den, idx_all) if centres else np.array([])
        coef, *_ = np.linalg.lstsq(design, y, rcond=None)
        # bootstrap over this market's games
        boots = np.zeros((n_boot, len(coef)))
        curv_boot = np.zeros(n_boot)
        for b in range(n_boot):
            idx = rng.integers(0, G, size=G)
            yb = means(f_num, f_den, idx)
            cb, *_ = np.linalg.lstsq(design, yb, rcond=None)
            boots[b] = cb
            if centres:
                curv_boot[b] = yb.mean() - means(c_num, c_den, idx).mean()
        lo = np.percentile(boots, 2.5, axis=0)
        hi = np.percentile(boots, 97.5, axis=0)
        se = boots.std(axis=0, ddof=1)
        effects = {}
        for i, cn in enumerate(coef_names):
            # a main effect = 2 × the coefficient (low→high); interactions likewise
            scale = 1.0 if cn == "mean" else 2.0
            effects[cn] = {
                "effect": float(coef[i] * scale),
                "lo": float(lo[i] * scale),
                "hi": float(hi[i] * scale),
                "se": float(se[i] * scale),
            }
        curvature = float(y.mean() - centre_vals.mean()) if centres else None
        noise_floor = float(base_vals.std(ddof=1)) if len(base_vals) > 1 else None
        per_market_effects[m] = {
            "n_records": n_rec,
            "n_games": G,
            "effects": effects,
            "curvature": curvature,
            "curvature_lo": float(np.percentile(curv_boot, 2.5)) if centres else None,
            "curvature_hi": float(np.percentile(curv_boot, 97.5)) if centres else None,
            "noise_floor_sd": noise_floor,
            "arm_means": {a["name"]: float(v) for (a, _d), v in zip(factorial, y, strict=True)},
            "baseline_means": {
                a["name"]: float(v) for (a, _d), v in zip(baselines, base_vals, strict=True)
            },
            "centre_means": {
                a["name"]: float(v) for (a, _d), v in zip(centres, centre_vals, strict=True)
            },
        }
        # the composite: each market's coefficients in units of its se
        w = (weights or {}).get(m, 1.0)
        z_terms_point[m] = w * coef / np.maximum(se, 1e-12)
        z_terms_boot[m] = w * (boots - coef) / np.maximum(se, 1e-12)
    if not z_terms_point:
        raise SystemExit("no market passed min_n on its common games")
    norm = float(np.sqrt(sum(((weights or {}).get(m, 1.0)) ** 2 for m in z_terms_point))) or 1.0
    z_point = sum(z_terms_point.values()) / norm
    z_boot = sum(z_terms_boot.values()) / norm
    z_lo = np.percentile(z_boot, 2.5, axis=0) + z_point
    z_hi = np.percentile(z_boot, 97.5, axis=0) + z_point
    composite = {
        cn: {"z": float(z_point[i]), "lo": float(z_lo[i]), "hi": float(z_hi[i])}
        for i, cn in enumerate(coef_names)
    }
    # the best corner under the composite (main effects only; negative = better)
    corner = {names[i]: ("high" if z_point[1 + i] < 0 else "low") for i in range(k)}
    result["markets"] = per_market_effects
    result["composite"] = composite
    result["best_corner_by_composite"] = corner
    return result


def print_analysis(res: dict[str, Any]) -> None:
    names = res["names"]
    print(
        "=== SIM-548 designed experiment: effects = high level − low level (negative = more accurate);"
        " each market on its own common games ==="
    )
    if res.get("dropped_interactions"):
        print(f"  dropped (aliased with a main effect): {res['dropped_interactions']}")
    print("  composite Z per term (main effects and alias groups of two-way interactions):")
    for cn, v in res["composite"].items():
        if cn == "mean":
            continue
        flag = "*" if (v["hi"] < 0 or v["lo"] > 0) else " "
        print(f"   {flag} {cn:50s} Z {v['z']:+7.2f} [{v['lo']:+7.2f}, {v['hi']:+7.2f}]")
    print(f"  best corner by the composite's main effects: {res['best_corner_by_composite']}")
    print()
    print("  per market (Brier main effects; * = clear of zero):")
    for m, d in res["markets"].items():
        parts = []
        for n in names:
            e = d["effects"][n]
            flag = "*" if (e["hi"] < 0 or e["lo"] > 0) else ""
            parts.append(f"{n.replace('SIM_', '')}={e['effect']:+.4f}{flag}")
        nf = f" noise sd {d['noise_floor_sd']:.4f}" if d.get("noise_floor_sd") is not None else ""
        cv = (
            f" curvature {d['curvature']:+.4f} [{d['curvature_lo']:+.4f}, {d['curvature_hi']:+.4f}]"
            if d.get("curvature") is not None
            else ""
        )
        print(
            f"   {m:18s} n={d['n_records']:6d} games={d['n_games']:4d} "
            + "  ".join(parts)
            + nf
            + cv
        )
    for m, why in res.get("skipped_markets", {}).items():
        print(f"   {m:18s} skipped: {why}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--factors",
        default=None,
        help="JSON {ENV_NAME: [low(production), high(candidate)] or [low, high, mid], ...} (1-6 factors)",
    )
    ap.add_argument(
        "--baseline-repeats",
        type=int,
        default=3,
        help="production repeats under fresh seeds (the noise floor)",
    )
    ap.add_argument(
        "--centre-arms",
        type=int,
        default=0,
        help="true centre points at the mid levels (needs a mid level on every factor)",
    )
    ap.add_argument("--base-seed", type=int, default=0)
    ap.add_argument("--game-pks-file", default=None)
    ap.add_argument("--seasons", default="2024")
    ap.add_argument("--iterations", type=int, default=100)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--analyze-only", action="store_true")
    ap.add_argument("--min-n", type=int, default=100)
    ap.add_argument("--bootstrap-samples", type=int, default=300)
    ap.add_argument("--bootstrap-seed", type=int, default=548)
    ap.add_argument("--weights", default=None, help="JSON market weights for the composite")
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    design_path = out_dir / "design.json"
    if args.factors:
        factors = json.loads(args.factors)
        arms = build_arms(
            factors, args.baseline_repeats, args.base_seed, args.centre_arms, args.iterations
        )
        design_path.write_text(
            json.dumps({"factors": factors, "arms": arms}, indent=1), encoding="utf-8"
        )
    elif design_path.exists():
        arms = json.loads(design_path.read_text(encoding="utf-8"))["arms"]
    else:
        raise SystemExit("give --factors (a new design) or an --out-dir with design.json")
    print(f"{len(arms)} arms: {[a['name'] for a in arms]}")
    if not args.analyze_only:
        if not args.game_pks_file:
            raise SystemExit("--game-pks-file is required to run the arms")
        for arm in arms:
            run_arm(arm, out_dir, args.game_pks_file, args.iterations, args.workers, args.seasons)
    weights = json.loads(args.weights) if args.weights else None
    res = analyze(arms, out_dir, args.min_n, args.bootstrap_samples, args.bootstrap_seed, weights)
    print_analysis(res)
    (out_dir / "analysis.json").write_text(
        json.dumps(res, indent=1, default=float), encoding="utf-8"
    )
    print(f"wrote {out_dir / 'analysis.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
