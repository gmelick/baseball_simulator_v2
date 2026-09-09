"""
scripts/sim523_concentration.py — SIM-523 part F: the concentration check at the FITTED powers.

The build-time report (``--what actors_sim``) reads every actor score matrix
at power 1. Part F raises the matrices to fitted powers, and the owner's
rule joins the grade: no factor may put more than its natural share of a
draw on the live player's own team. This tool re-runs the report at the
given powers and prints, per matrix, the distribution over live profiles of
the ratio (weighted own-staff share / unweighted own-staff share) and the
effective sample share, with PASS/FAIL against the strict ratio.

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim523_concentration.py \\
        --powers fielder=2,catcher_throwing=2,runner_steal=2 --strict-ratio 3.0
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.batch.engine_artifacts import concentration_report  # noqa: E402

_DEFAULT_DUCKDB = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
_DEFAULT_SIM_DIR = "/data/play_pool/engine_artifacts/actor_sim"


def _parse_powers(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, _, value = part.partition("=")
        out[name.strip()] = float(value)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--powers", default="", help="name=power,... (fielder covers every position)")
    ap.add_argument("--strict-ratio", type=float, default=3.0)
    ap.add_argument("--duckdb-path", default=_DEFAULT_DUCKDB)
    ap.add_argument("--sim-dir", default=_DEFAULT_SIM_DIR)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    powers = _parse_powers(args.powers)
    with open(os.path.join(args.sim_dir, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    seasons = [int(s) for s in manifest.get("seasons", [])]
    report = concentration_report(args.duckdb_path, args.sim_dir, seasons, powers)
    worst = 0.0
    print(f"=== concentration at powers {powers or '{all 1.0}'} (seasons {seasons}) ===")
    print(
        f"  {'matrix':>18} {'profiles':>8} {'ratio p50':>10} {'ratio p90':>10} {'max':>7} {'ESS':>7}"
    )
    for name, rep in report.items():
        if not rep.get("profiles"):
            print(f"  {name:>18} {0:>8}")
            continue
        p90 = float(rep["ratio_p90"])
        worst = max(worst, p90)
        flag = "PASS" if p90 <= args.strict_ratio else "FAIL"
        print(
            f"  {name:>18} {rep['profiles']:>8} {rep['ratio_median']:10.3f} {p90:10.3f} "
            f"{rep['ratio_max']:7.2f} {rep['ess_share_median']:7.3f}  {flag}"
        )
    verdict = "PASS" if worst <= args.strict_ratio else "FAIL"
    print(f"  worst p90 own-staff ratio {worst:.3f} vs the strict {args.strict_ratio}: {verdict}")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"powers": powers, "strict_ratio": args.strict_ratio, "report": report}, fh)
        print(f"  wrote {args.json_out}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
