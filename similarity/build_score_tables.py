"""Build the actor score matrices that the draws look up, and check their concentration.

An actor score matrix holds one engine's 0-to-1 similarity between every two player-seasons
of one role, for example two catcher-seasons. The nightly bundle build writes each matrix to
``<out_dir>/actor_sim/<name>.npz``, with a ``manifest.json`` and a ``concentration.json``.

The concentration check measures how much of a draw's weight a matrix puts on rows of the
live player's own staff (the pitchers he caught or fielded behind that season). No factor
may put more than its natural share of a draw on the live player's own team.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import duckdb
import numpy as np

from bundle.common import (
    _savez_plain,
)
from profiles.sql import sql_seasons
from simulation.constants import FIELD_POSITION_NAMES

log = logging.getLogger("engine_artifacts")


#: The engine class of each role, as (module, class). The table lives here, not in
#: api.state's registry, so this build never imports the API layer.
_ACTOR_SIM_ENGINES: dict[str, tuple[str, str]] = {
    "batter": ("similarity.engines.batter_similarity", "BatterSimilarityEngine"),
    "catcher": ("similarity.engines.catcher_similarity", "CatcherSimilarityEngine"),
    "fielder": ("similarity.engines.fielder_similarity", "FielderSimilarityEngine"),
    "baserunner_steal": (
        "similarity.engines.baserunner_steal_similarity",
        "BaserunnerStealSimilarityEngine",
    ),
    "baserunner": ("similarity.engines.baserunner_similarity", "BaserunnerSimilarityEngine"),
    "pitcher_steal": (
        "similarity.engines.pitcher_steal_similarity",
        "PitcherStealSimilarityEngine",
    ),
    # The manager (the usage sub-score weights the pitching-change draw).
    "manager": ("similarity.engines.manager_similarity", "ManagerSimilarityEngine"),
}


#: The matrices by name. Each spec names the engine, the score attribute of the
#: engine's SimilarityResult, and the result's key attributes. The keys join into
#: the matrix index's "a:b[:c]" string.
_ACTOR_SIM_SPECS: dict[str, dict] = {
    # The manager's USAGE similarity weights the pitching-change draw. It reads
    # the starters' pitch counts, the pulls before 100 pitches, the closer's
    # entry leverage, the high-leverage reliever share, the opener and bulk
    # rates, and the use of the available relievers. The concentration check
    # skips it by design: the live manager's own rows should carry more weight.
    # The fit's floor on the effective sample size guards it instead.
    "manager_usage": {
        "engine": "manager",
        "score": "usage_score",
        "keys": ("manager_id", "season"),
    },
    "batter": {"engine": "batter", "score": "score", "keys": ("batter_id", "season")},
    "catcher": {"engine": "catcher", "score": "score", "keys": ("catcher_id", "season")},
    "catcher_throwing": {
        "engine": "catcher",
        "score": "throwing_score",
        "keys": ("catcher_id", "season"),
    },
    "runner_steal": {
        "engine": "baserunner_steal",
        "score": "score",
        "keys": ("player_id", "season"),
    },
    "runner_adv": {"engine": "baserunner", "score": "score", "keys": ("player_id", "season")},
    "pitcher_steal": {
        "engine": "pitcher_steal",
        "score": "score",
        "keys": ("pitcher_id", "season"),
    },
}


#: The fielder matrices' positions: scorekeeping numbers 3 to 9 and their names.
_FIELDER_POSITION_NAMES: dict[int, str] = {
    num: name for num, name in FIELD_POSITION_NAMES.items() if num >= 3
}


#: The engines that also score their THIN profiles (the ones below the engine's
#: sample minimum), so the matrices cover every runner-season the pools hold.
#: Without them, about two thirds of the steal rows and half of the advancement
#: rows have no score.
_THIN_PROFILE_ENGINES = frozenset({"baserunner", "baserunner_steal"})


#: The largest p90 own-staff ratio a matrix may reach. The ratio is the weighted
#: own-staff share over the unweighted share; p90 is its 90th percentile over the
#: live profiles. Above it, a strict build raises and any other build logs a warning.
CONCENTRATION_MAX_RATIO = 3.0


def _default_actor_engine_loader(module_path: str, class_name: str):
    """Import ``module_path`` and return its class ``class_name``."""
    import importlib

    return getattr(importlib.import_module(module_path), class_name)


# A test swaps this loader for one that returns a fake engine.
_ENGINE_LOADER = _default_actor_engine_loader


def _key_of(result, key_attrs: tuple[str, ...]) -> str:
    """The matrix index key of one query result: its key attributes joined by ``:``."""
    # A list comprehension, not ``str.join(<generator>)``: the generator form
    # segfaulted in this hot loop, and this form ran clean. The crash is
    # stochastic (CLAUDE.md §2a), so this form is a precaution, not a proven fix.
    return ":".join([str(getattr(result, a)) for a in key_attrs])


def _matrix_from_queries(
    profile_keys: list[tuple], query_fn, key_attrs: tuple[str, ...], score_attr: str
) -> tuple[dict[str, int], np.ndarray]:
    """Score each profile against the others with the engine's own query.

    Returns ``(index, matrix)``: the key-to-row index and a dense float32
    matrix, with 1.0 on the diagonal and NaN for a pair the query does not score.
    """
    index = {":".join(str(x) for x in k): i for i, k in enumerate(profile_keys)}
    n = len(index)
    mat = np.full((n, n), np.nan, dtype=np.float32)
    for i, k in enumerate(profile_keys):
        mat[i, i] = 1.0
        for r in query_fn(*k):
            j = index.get(_key_of(r, key_attrs))
            if j is not None:
                mat[i, j] = float(getattr(r, score_attr))
    return index, mat


def _write_actor_sim(out_dir: str, name: str, index: dict[str, int], mat: np.ndarray) -> None:
    """Save one matrix and its key index as ``<out_dir>/<name>.npz``."""
    _savez_plain(os.path.join(out_dir, f"{name}.npz"), index=json.dumps(index), matrix=mat)


def build_actor_sim_matrices(
    duckdb_path: str,
    out_dir: str,
    seasons: list[int],
    *,
    limit: int | None = None,
    strict: bool = False,
    only: set[str] | None = None,
) -> dict[str, int]:
    """Write the actor score matrices of ``seasons`` to ``<out_dir>/actor_sim/``.

    The build writes one ``.npz`` file per matrix, the ``manifest.json`` and
    the concentration report (``concentration.json``). Returns the matrix sizes
    by name. ``limit`` scores only the first N profiles of each matrix, for a
    quick check. When the worst p90 own-staff ratio exceeds
    :data:`CONCENTRATION_MAX_RATIO`, ``strict`` raises ``RuntimeError``;
    otherwise the build logs a warning.

    ``only`` (matrix names, for example ``{"manager_usage"}``) rebuilds those
    matrices alone and MERGES their sizes into the existing manifest. It
    rebuilds one matrix without paying for the others, which helps when a
    full build segfaults (CLAUDE.md §2a). That mode skips the concentration
    report.
    """
    sim_dir = os.path.join(out_dir, "actor_sim")
    os.makedirs(sim_dir, exist_ok=True)
    season_set = {int(s) for s in seasons}
    sizes: dict[str, int] = {}
    if only:
        manifest_path = os.path.join(sim_dir, "manifest.json")
        if os.path.exists(manifest_path):
            with open(manifest_path, encoding="utf-8") as fh:
                sizes = dict(json.load(fh).get("sizes", {}))
    engines: dict[str, Any] = {}

    def _engine(name: str):
        eng = engines.get(name)
        if eng is None:
            module, cls = _ACTOR_SIM_ENGINES[name]
            eng = _ENGINE_LOADER(module, cls)(duckdb_path=duckdb_path)
            if name in _THIN_PROFILE_ENGINES:
                eng.build(seasons=sorted(season_set), include_below_minimum=True)
            else:
                eng.build(seasons=sorted(season_set))
            engines[name] = eng
        return eng

    for name, spec in _ACTOR_SIM_SPECS.items():
        if only and name not in only:
            continue
        eng = _engine(spec["engine"])
        keys = sorted(k for k in eng._profiles if int(k[-1]) in season_set)
        if limit is not None:
            keys = keys[:limit]
        index, mat = _matrix_from_queries(keys, eng.query, spec["keys"], spec["score"])
        _write_actor_sim(sim_dir, name, index, mat)
        sizes[name] = len(index)
        log.info("actor_sim[%s]: %d x %d", name, len(index), len(index))
    if not only or any(n.startswith("fielder_") for n in only):
        feng = _engine("fielder")
        for pos in _FIELDER_POSITION_NAMES.values():
            if only and f"fielder_{pos}" not in only:
                continue
            keys = sorted(k for k in feng._profiles if k[1] == pos and int(k[2]) in season_set)
            if limit is not None:
                keys = keys[:limit]
            index, mat = _matrix_from_queries(
                keys, feng.query, ("player_id", "position", "season"), "score"
            )
            _write_actor_sim(sim_dir, f"fielder_{pos}", index, mat)
            sizes[f"fielder_{pos}"] = len(index)
            log.info("actor_sim[fielder_%s]: %d x %d", pos, len(index), len(index))
    with open(os.path.join(sim_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"seasons": sorted(season_set), "sizes": sizes}, fh, indent=2)
    if only:
        return sizes

    report = concentration_report(duckdb_path, sim_dir, sorted(season_set))
    with open(os.path.join(sim_dir, "concentration.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    worst = max((v.get("ratio_p90", 0.0) for v in report.values()), default=0.0)
    if worst > CONCENTRATION_MAX_RATIO:
        msg = (
            f"actor_sim concentration: a matrix's p90 own-staff ratio is {worst:.2f} "
            f"(> {CONCENTRATION_MAX_RATIO}); the draw would lean on the live player's team"
        )
        if strict:
            raise RuntimeError(msg)
        log.warning(msg)
    return sizes


def concentration_share(
    matrix: np.ndarray,
    live_row: int,
    row_actor_col: np.ndarray,
    row_pitcher: np.ndarray,
    staff: set[int],
    power: float = 1.0,
) -> tuple[float, float, float]:
    """Return the three concentration shares of one live profile's matrix row.

    The shares are ``(weighted own-staff share, unweighted own-staff share,
    effective-sample share)``. The effective-sample share is the effective
    sample size, (sum of w)^2 / (sum of w^2), over the row count. The shares
    cover the pool rows whose actor has a matrix column (``row_actor_col >=
    0``). An unscored pair weighs 1.0. ``power`` is the fitted power the row
    is raised to; 1.0 keeps the raw score. All three shares are NaN when no
    row has a matrix column. The weighted and effective-sample shares are NaN
    when the weights sum to 0 or less.
    """
    valid = row_actor_col >= 0
    if not valid.any():
        return float("nan"), float("nan"), float("nan")
    w = matrix[live_row, row_actor_col[valid]].astype(np.float64)
    w = np.where(np.isfinite(w), w, 1.0)
    if power != 1.0:
        w = np.power(np.clip(w, 0.0, None), float(power))
    own = np.isin(row_pitcher[valid], list(staff))
    total = float(w.sum())
    if total <= 0.0:
        return float("nan"), float(own.mean()), float("nan")
    return float(w[own].sum() / total), float(own.mean()), float(total * total / (w @ w) / w.size)


def concentration_report(
    duckdb_path: str,
    sim_dir: str,
    seasons: list[int],
    powers: dict[str, float] | None = None,
) -> dict[str, dict]:
    """Run the concentration check on the matrices in ``sim_dir``.

    The check covers the two actors whose rows carry a pitching staff: the
    catchers over the pitch pool and the fielders over the batted-ball pool.
    Each matrix's entry summarizes, over its live profiles, the own-staff
    ratio (the weighted own-staff share over the unweighted share) and the
    effective-sample share (:func:`concentration_share`). ``powers`` maps a
    matrix name to its fitted power; the default 1.0 keeps the raw score.
    Returns the entries by matrix name; a matrix with no file has no entry.
    """
    pw = powers or {}
    season_list = sql_seasons(seasons)
    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        pp = con.execute(
            "SELECT season, catcher_id, pitcher_id FROM sim.pitch_pool "
            f"WHERE season IN ({season_list}) AND catcher_id IS NOT NULL AND catcher_id > 0"
        ).fetchnumpy()
        bb = con.execute(
            "SELECT season, fielder_player_id, fielded_by_position, pitcher_id "
            f"FROM sim.outcome_pool WHERE season IN ({season_list}) "
            "AND fielder_player_id IS NOT NULL AND fielder_player_id > 0"
        ).fetchnumpy()
    finally:
        con.close()

    def _load(name: str):
        p = os.path.join(sim_dir, f"{name}.npz")
        if not os.path.exists(p):
            return None, None
        z = np.load(p, allow_pickle=False)
        return json.loads(str(z["index"])), z["matrix"]

    def _summarize(entries: list[tuple[float, float, float]]) -> dict:
        arr = np.asarray([e for e in entries if all(np.isfinite(e))], dtype=np.float64)
        if arr.size == 0:
            return {"profiles": 0}
        ratio = arr[:, 0] / np.maximum(arr[:, 1], 1e-9)
        return {
            "profiles": int(arr.shape[0]),
            "own_share_weighted_median": float(np.median(arr[:, 0])),
            "own_share_unweighted_median": float(np.median(arr[:, 1])),
            "ratio_median": float(np.median(ratio)),
            "ratio_p90": float(np.percentile(ratio, 90)),
            "ratio_max": float(ratio.max()),
            "ess_share_median": float(np.median(arr[:, 2])),
        }

    report: dict[str, dict] = {}
    # --- catchers over the pitch pool
    for name in ("catcher", "catcher_throwing"):
        index, mat = _load(name)
        if index is None:
            continue
        seasons_arr = np.ma.filled(pp["season"], 0).astype(np.int64)
        cid = np.ma.filled(pp["catcher_id"], 0).astype(np.int64)
        pid = np.ma.filled(pp["pitcher_id"], 0).astype(np.int64)
        col = np.fromiter(
            (index.get(f"{c}:{s}", -1) for c, s in zip(cid, seasons_arr, strict=False)),
            dtype=np.int64,
            count=cid.size,
        )
        entries = []
        for key, row in index.items():
            c, s = (int(x) for x in key.split(":"))
            in_season = seasons_arr == s
            staff = set(pid[in_season & (cid == c)].tolist())
            if not staff:
                continue
            entries.append(
                concentration_share(
                    mat, row, col[in_season], pid[in_season], staff, pw.get(name, 1.0)
                )
            )
        report[name] = _summarize(entries)
    # --- fielders over the batted-ball pool, per position
    seasons_arr = np.ma.filled(bb["season"], 0).astype(np.int64)
    fid = np.ma.filled(bb["fielder_player_id"], 0).astype(np.int64)
    fpos = np.ma.filled(bb["fielded_by_position"], 0).astype(np.int64)
    pid = np.ma.filled(bb["pitcher_id"], 0).astype(np.int64)
    for pnum, pos in _FIELDER_POSITION_NAMES.items():
        index, mat = _load(f"fielder_{pos}")
        if index is None:
            continue
        at_pos = fpos == pnum
        col = np.fromiter(
            (
                index.get(f"{f}:{pos}:{s}", -1) if ok else -1
                for f, s, ok in zip(fid, seasons_arr, at_pos, strict=False)
            ),
            dtype=np.int64,
            count=fid.size,
        )
        entries = []
        for key, row in index.items():
            f, _p, s = key.split(":")
            f, s = int(f), int(s)
            in_season = seasons_arr == s
            staff = set(pid[in_season & at_pos & (fid == f)].tolist())
            if not staff:
                continue
            entries.append(
                concentration_share(
                    mat,
                    row,
                    col[in_season],
                    pid[in_season],
                    staff,
                    pw.get(f"fielder_{pos}", pw.get("fielder", 1.0)),
                )
            )
        report[f"fielder_{pos}"] = _summarize(entries)
    return report
