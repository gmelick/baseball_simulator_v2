"""The game selection: which games a run scores, and the frozen inputs it scores
them with.

A run scores the Final games of its seasons, optionally cut to the game_pks of a
file (the paired-arm read runs both arms on one file). The provenance block names
the bundle, the calibration and the flags behind the run's probabilities, so the
paired read and the market-skill merge refuse two reports built from different
inputs.
"""

from __future__ import annotations

import json
import os
from typing import Any

from simulation import flags


def _read_game_pks_file(path: str) -> set[int]:
    """The game_pks in ``path``: a JSON list, or one integer per line (blank lines
    and ``#`` comments ignored)."""
    text = open(path, encoding="utf-8").read()
    stripped = text.strip()
    if stripped.startswith("["):
        return {int(x) for x in json.loads(stripped)}
    out: set[int] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(int(line))
    return out


def _flag_as_set(name: str) -> str | None:
    """The ``SIM_*`` flag ``name`` as the environment sets it, or ``None`` when
    unset.

    The provenance records what was set, so an unset flag stays ``None`` instead
    of reading as its default. The name must be a registered flag
    (``simulation/flags.py``).
    """
    if not flags.is_registered(name):
        raise KeyError(f"{name} is not a registered SIM_* flag (simulation/flags.py)")
    return os.environ.get(name)


def bundle_provenance(calibration_path: str) -> dict[str, Any]:
    """What the run's probabilities were built from: the bundle manifests'
    timestamps, the calibration file's hash and the flags that pick each arm.

    ``scripts/pair_accuracy.py`` pairs two reports only when these match. A
    missing file reads as ``None`` instead of failing the run.
    """
    import hashlib

    art_dir = os.path.join(
        os.environ.get("BASEBALL_PLAY_POOL_DIR", "/data/play_pool"), "engine_artifacts"
    )
    stamps: dict[str, float | None] = {}
    # The change pool and the steal pool are part of the frozen bundle too. A
    # steal-pool rebuild exports steal_pool/ alone and leaves every other file
    # as it was, so without its stamp a report from before that export pairs
    # with one from after it.
    for sub in ("pitch_pool", "battedball_pool", "actor_sim", "manager_pool", "steal_pool"):
        mp = os.path.join(art_dir, sub, "manifest.json")
        stamps[sub] = os.path.getmtime(mp) if os.path.exists(mp) else None
    cal_hash: str | None = None
    if os.path.exists(calibration_path):
        with open(calibration_path, "rb") as fh:
            cal_hash = hashlib.sha256(fh.read()).hexdigest()

    def _steal_pool_classed() -> bool | None:
        """True when every pitch row of the exported steal pool carries its pitch
        class, the condition of the sampler's ``has_steal_classes``.

        None when the bundle has no steal pool or a file cannot be read; False on
        a pool exported before migration 0031 or with an unclassed season. A
        pickoff row is told apart by ``is_pickoff_row`` (by an empty class when
        the column is missing), as the sampler does. An empty target is skipped,
        as the sampler skips it.
        """
        sp_dir = os.path.join(art_dir, "steal_pool")
        if not os.path.exists(os.path.join(sp_dir, "manifest.json")):
            return None
        import duckdb

        con = duckdb.connect()
        try:
            live = 0
            for target in ("2", "3"):
                path = os.path.join(sp_dir, f"{target}.meta.parquet").replace("'", "''")
                src = f"read_parquet('{path}')"
                cols = {row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
                if "pitch_class" not in cols:
                    if con.execute(f"SELECT COUNT(*) FROM {src}").fetchone()[0] == 0:
                        continue
                    return False
                pickoff = (
                    "COALESCE(is_pickoff_row, FALSE)"
                    if "is_pickoff_row" in cols
                    else "COALESCE(pitch_class, '') = ''"
                )
                n_rows, n_pitch, n_unclassed = con.execute(
                    f"SELECT COUNT(*), COUNT(*) FILTER (WHERE NOT {pickoff}), "
                    f"COUNT(*) FILTER (WHERE NOT {pickoff} AND COALESCE(pitch_class, '') = '') "
                    f"FROM {src}"
                ).fetchone()
                if n_rows == 0:
                    continue
                if n_pitch == 0 or n_unclassed > 0:
                    return False
                live += 1
            return live > 0
        except duckdb.Error:
            return None
        finally:
            con.close()

    from types import SimpleNamespace

    from simulation.production_factory import apply_running_game_env

    flag = SimpleNamespace()
    apply_running_game_env(flag)
    return {
        "artifact_dir": art_dir,
        "manifest_mtimes": stamps,
        "calibration_sha256": cal_hash,
        "fatigue_pc_sigma": _flag_as_set("SIM_FATIGUE_PC_SIGMA"),
        "fatigue_tto_sigma": _flag_as_set("SIM_FATIGUE_TTO_SIGMA"),
        "sit_sigma": _flag_as_set("SIM_SIT_SIGMA"),
        # Which arm of the manager switch the report measured: the draw, the
        # pen source, the manager power and the reliever weights.
        "manager": {
            key: _flag_as_set(key)
            for key in (
                "SIM_MANAGER",
                "SIM_MANAGER_DRAW",
                "SIM_BULLPEN_SOURCE",
                "SIM_ACTOR_POWER_MANAGER_USAGE",
                "SIM_RELIEF_ROLE_SIGMA",
                "SIM_RELIEF_PITCHER_POWER",
                "SIM_RELIEF_REST_SIGMA",
                "SIM_RELIEF_PITCHED2D_OFF_WEIGHT",
                "SIM_RELIEF_PITCHES3D_SIGMA",
                "SIM_RELIEF_HAND_OFF_WEIGHT",
            )
        },
        # Which order of the running game the report measured. The pickoff
        # draw before the pitch and the steal draw after it run only when
        # both entries are True: the flag SIM_STEAL_PITCH_CLASS as the factory
        # reads it (unset = on), and a steal pool whose every pitch row carries
        # its class. Otherwise the single pre-pitch draw runs. The market-skill
        # merge refuses chunks that differ here; a pair that flips this on
        # purpose is allowed.
        "running_game": {
            "flag": bool(flag.steal_pitch_class),
            "classed_bundle": _steal_pool_classed(),
        },
        # Which arm of the pitch / pitch-result split the report measured: the
        # switch, the pitch draw's pitcher power, and the result draw's pitcher
        # and batter powers, bandwidth and density correction.
        "split": {
            key: _flag_as_set(key)
            for key in (
                "SIM_PITCH_RESULT_SPLIT",
                "SIM_PITCH_PITCHER_POWER",
                "SIM_RESULT_PITCHER_POWER",
                "SIM_RESULT_BATTER_POWER",
                "SIM_RESULT_PITCH_SIGMA",
                "SIM_RESULT_DENSITY_POWER",
            )
        },
    }


async def _fetch_final_games(dsn: str, seasons: list[int], max_games: int | None) -> list[int]:
    """The game_pks of the Final games of ``seasons``, in game_pk order, at most
    ``max_games``. A Final game here has both final scores
    (``home_score_final``, ``away_score_final``)."""
    import asyncpg

    sl = ", ".join(str(int(s)) for s in seasons)
    limit = f"LIMIT {int(max_games)}" if max_games else ""
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            f"""
            SELECT game_pk
            FROM raw.games
            WHERE status = 'Final'
              AND season IN ({sl})
              AND home_score_final IS NOT NULL AND away_score_final IS NOT NULL
            ORDER BY game_pk
            {limit}
            """
        )
    finally:
        await conn.close()
    return [int(r["game_pk"]) for r in rows]
