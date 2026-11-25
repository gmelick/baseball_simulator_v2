"""Build a bundle version and publish it: the nightly entry point.

Run ``python -m bundle.build_bundle --what all``; ``--what`` picks one part instead. With
no ``--out-dir``, the build copies the current version into a new folder, writes into it,
and points the bundle link at it only when every step succeeds. A failed build deletes the
new folder. ``--out-dir`` writes in place instead.
"""

from __future__ import annotations

import argparse
import logging

import duckdb

from bundle.catcher_receiving import build_receiving_profiles
from bundle.common import (
    DEFAULT_DUCKDB_PATH,
    last_n_seasons,
)
from bundle.export_actor_embeddings import build_actor_embeddings
from bundle.export_advancement_pool import (
    build_advancement_pool_artifact,
)
from bundle.export_batted_ball_pool import (
    build_battedball_pool_artifact,
)
from bundle.export_pitch_pool import build_pitch_pool_artifact
from bundle.export_pitcher_scores import build_pitcher_sim_matrix
from bundle.export_pitching_change_pool import build_pitching_change_pool
from bundle.export_steal_pool import build_steal_pool_artifact
from bundle.park_geometry import build_park_geometry
from bundle.reference_tables import (
    build_reference_tables,
)
from bundle.versions import (
    bundle_link,
    discard_bundle_version,
    prune_bundle_versions,
    publish_bundle_version,
    stage_bundle_version,
)
from similarity.build_score_tables import CONCENTRATION_MAX_RATIO, build_actor_sim_matrices

log = logging.getLogger("engine_artifacts")


def main(argv: list[str] | None = None) -> int:
    """Parse the arguments, build the bundle and publish it. Returns 0."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Build the SIM-422 engine-artifact bundle.")
    ap.add_argument("--duckdb-path", default=DEFAULT_DUCKDB_PATH)
    ap.add_argument(
        "--out-dir",
        default=None,
        help=(
            "Write the bundle IN PLACE here. Omit it to build a new VERSION of the "
            "published bundle ($BASEBALL_PLAY_POOL_DIR/engine_artifacts) and swap it in when "
            "the build is complete (SIM-563)."
        ),
    )
    ap.add_argument(
        "--seasons",
        type=int,
        nargs="+",
        default=None,
        help="Override the recency-floor seasons (default: the last 4 in the pitch pool).",
    )
    ap.add_argument(
        "--what",
        choices=[
            "pool",
            "pitcher_sim",
            "actors",
            "actors_sim",
            "park",
            "manager",
            "receiving",
            "reference",
            "all",
        ],
        default="pool",
    )
    ap.add_argument(
        "--strict-concentration",
        action="store_true",
        help="SIM-523: fail the actor-matrix build when a matrix's p90 own-staff ratio "
        f"exceeds {CONCENTRATION_MAX_RATIO}.",
    )
    ap.add_argument(
        "--pitcher-sim-limit",
        type=int,
        default=None,
        help="Score only N pitcher profiles (verification; omit for full nightly run).",
    )
    ap.add_argument(
        "--matrix",
        action="append",
        default=None,
        help=(
            "SIM-427: with --what actors_sim, build only this matrix (repeatable; e.g. "
            "--matrix manager_usage) and merge its size into the existing manifest."
        ),
    )
    args = ap.parse_args(argv)

    # With no --out-dir, build a new version and publish it only when the build succeeds.
    link = bundle_link()
    staged = args.out_dir is None
    if staged:
        args.out_dir = stage_bundle_version(link)
    try:
        _build(args)
    except BaseException:  # an interrupt also discards the half-built version
        if staged:
            discard_bundle_version(args.out_dir)
        raise
    if staged:
        publish_bundle_version(link, args.out_dir)
        prune_bundle_versions(link)
    log.info("engine-artifact build complete -> %s", args.out_dir)
    return 0


def _build(args: argparse.Namespace) -> None:
    """Write the parts ``args.what`` names into ``args.out_dir``."""
    con = duckdb.connect(args.duckdb_path, read_only=True)
    try:
        seasons = args.seasons or last_n_seasons(con)
        if args.what in ("pool", "all"):
            build_pitch_pool_artifact(con, args.out_dir, seasons)
            build_battedball_pool_artifact(con, args.out_dir, seasons)
            build_steal_pool_artifact(con, args.out_dir, seasons)
            build_advancement_pool_artifact(con, args.out_dir, seasons)
        if args.what in ("actors", "all"):
            build_actor_embeddings(con, args.out_dir)
        if args.what in ("park", "all"):
            # The park geometry and the carry model.
            build_park_geometry(con, args.out_dir, seasons)
        if args.what in ("manager", "all"):
            build_pitching_change_pool(con, args.out_dir, seasons)
        if args.what in ("receiving", "all"):
            build_receiving_profiles(con, args.out_dir, seasons)
        if args.what in ("reference", "all"):
            # The intentional-walk rates and the venue run factors.
            build_reference_tables(con, args.out_dir)
        if args.what in ("pitcher_sim", "all"):
            build_pitcher_sim_matrix(
                args.duckdb_path, args.out_dir, seasons, limit=args.pitcher_sim_limit
            )
        if args.what in ("actors_sim", "all"):
            # The actor score matrices and the concentration report.
            build_actor_sim_matrices(
                args.duckdb_path,
                args.out_dir,
                seasons,
                limit=args.pitcher_sim_limit,
                strict=args.strict_concentration,
                only=set(args.matrix) if args.matrix else None,
            )
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
