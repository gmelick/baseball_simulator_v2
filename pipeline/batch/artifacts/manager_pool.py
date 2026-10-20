"""The pitching-change opportunity pool the manager draw reads, with the
relievers' role sidecar.
"""

from __future__ import annotations

import json
import logging
import os

import duckdb
import numpy as np

from db.introspect import duck_columns
from pipeline.batch.artifacts.common import take_view
from pipeline.batch.artifacts.pools import ChangePool

log = logging.getLogger("engine_artifacts")


# ---------------------------------------------------------------------------
# SIM-523 part D — the pitching-change OPPORTUNITY pool (the manager's step 1)
# ---------------------------------------------------------------------------
#
# One row per plate-appearance boundary while a pitcher is on the mound,
# changed or not — the denominator the old pull formula never had (the
# SIM-468 steal-pool pattern). The situation table gives the boundary (the
# half, the outs, the bases, the scores); the pitch pool gives the pitcher of
# every plate appearance. A boundary's row describes the pitcher who threw the
# PREVIOUS plate appearance for that side: his pitches and batters faced so
# far in the game, whether he is the side's starter, whether the boundary is
# the first plate appearance of a half inning, and whether he was replaced.

_CHANGE_SIT_COLS = ["pitch_count", "batters_faced", "inning", "outs", "runners_state", "score_diff"]


def _default_game_context(seasons: list[int]) -> tuple[dict, dict, dict] | None:
    """SIM-427: the builder's one Postgres read — the game's two managers and
    every appearance's rest as of its game (``pipeline.bullpen_usage``).
    None when no DSN is set or the read fails: the pool then carries unknowns
    (-1) in the manager and rest columns, and the log says so."""
    dsn = os.environ.get("BASEBALL_DB_DSN", "").strip()
    if not dsn:
        log.warning(
            "SIM-427: BASEBALL_DB_DSN is unset — the change pool carries no manager "
            "and no incoming-arm rest (every row unknown)."
        )
        return None
    try:
        from pipeline.bullpen_usage import fetch_game_context_sync

        return fetch_game_context_sync(dsn, seasons)
    except Exception as exc:  # noqa: BLE001 — the pool still builds, degraded
        log.warning(
            "SIM-427: the game context could not be read from Postgres (%s: %s) — the "
            "change pool carries no manager and no incoming-arm rest.",
            type(exc).__name__,
            exc,
        )
        return None


#: The four SIM-427 columns the change pool's meta parquet carries beyond the
#: part-D set, and their "unknown" value.
_CHANGE_CTX_COLS: tuple[tuple[str, int], ...] = (
    ("manager_id", -1),
    ("in_days_rest", -1),
    ("in_pitched_2d", -1),
    ("in_pitches_3d", -1),
    ("in_throws", 0),
)


def build_pitching_change_pool(
    con: duckdb.DuckDBPyConnection,
    out_dir: str,
    seasons: list[int],
    *,
    game_context: tuple[dict, dict] | None | bool = True,
) -> dict:
    """Write ``<out_dir>/manager_pool/change.{sit.npy, meta.parquet}``, the
    reliever ROLE sidecar ``roles.parquet`` and the manifest (the counts and
    the pool's own change rates). Returns the manifest.

    SIM-427: every row also carries the FIELDING side's manager (``manager_id``;
    the top of an inning is the home side fielding) and the incoming arm's rest
    as of the game (``in_days_rest`` capped at five, ``in_pitched_2d``,
    ``in_pitches_3d`` — ``pipeline.bullpen_usage``'s definitions), read from
    Postgres through ``game_context`` (``True`` = read it; a ``(managers,
    usage)`` pair = use it, the test seam; ``None`` / ``False`` = unknowns).
    """
    pool_dir = os.path.join(out_dir, "manager_pool")
    os.makedirs(pool_dir, exist_ok=True)
    season_list = ", ".join(str(int(s)) for s in seasons)
    # SIM-535: the row's game date (a YYYYMMDD integer) so the backtest's
    # point-in-time cutoff applies to this draw too; an older pitch pool
    # without the column leaves it NULL (the cutoff then refuses, loudly).
    have_date = "game_date" in duck_columns(con, "sim", "pitch_pool")
    ymd_agg = (
        "CAST(strftime(max(game_date), '%Y%m%d') AS INTEGER)" if have_date else "NULL::INTEGER"
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _change_pool AS
        WITH pp AS (
            SELECT game_pk, at_bat_number, arg_min(pitcher_id, pitch_number) AS pitcher_id,
                   count(*) AS n_pitches, max(recency_weight) AS recency,
                   {ymd_agg} AS game_ymd
            FROM sim.pitch_pool WHERE season IN ({season_list}) GROUP BY 1, 2
        ),
        pa AS (
            SELECT s.game_pk, s.season, s.at_bat_number, s.inning, s.top_or_bottom,
                   s.outs_when_up AS outs,
                   CAST(s.on_1b > 0 AS INTEGER) + 2 * CAST(s.on_2b > 0 AS INTEGER)
                       + 4 * CAST(s.on_3b > 0 AS INTEGER) AS runners_state,
                   s.home_score, s.away_score, pp.pitcher_id, pp.n_pitches, pp.recency,
                   pp.game_ymd
            FROM derived.at_bat_situations s JOIN pp USING (game_pk, at_bat_number)
            WHERE s.season IN ({season_list})
        ),
        seq AS (
            SELECT *,
                lag(pitcher_id) OVER side AS prev_pitcher,
                lag(inning) OVER side AS prev_inning,
                first_value(pitcher_id) OVER side AS side_starter,
                sum(n_pitches) OVER (PARTITION BY game_pk, pitcher_id ORDER BY at_bat_number
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS pc_after,
                row_number() OVER (PARTITION BY game_pk, pitcher_id ORDER BY at_bat_number)
                    AS bf_after
            FROM pa WINDOW side AS (PARTITION BY game_pk, top_or_bottom ORDER BY at_bat_number)
        ),
        b AS (
            SELECT game_pk, season, at_bat_number, top_or_bottom, inning, outs, runners_state,
                CASE WHEN top_or_bottom = 0 THEN home_score - away_score
                     ELSE away_score - home_score END AS score_diff,
                prev_pitcher AS pitcher_id, pitcher_id AS incoming_id,
                CAST(pitcher_id <> prev_pitcher AS TINYINT) AS changed,
                CAST(inning <> prev_inning AS TINYINT) AS new_half,
                CAST(prev_pitcher = side_starter AS TINYINT) AS is_starter,
                lag(pc_after) OVER side AS pitch_count,
                lag(bf_after) OVER side AS batters_faced,
                recency, game_ymd
            FROM seq WINDOW side AS (PARTITION BY game_pk, top_or_bottom ORDER BY at_bat_number)
        )
        SELECT * FROM b WHERE pitcher_id IS NOT NULL AND pitch_count IS NOT NULL
        ORDER BY game_pk, at_bat_number
        """
    )
    # Every read below orders by (game_pk, at_bat_number) so the sit array and
    # the meta parquet share one row order whatever the scan order.
    d = con.execute(
        f"SELECT {', '.join(_CHANGE_SIT_COLS)} FROM _change_pool ORDER BY game_pk, at_bat_number"
    ).fetchnumpy()
    n = len(d[_CHANGE_SIT_COLS[0]])
    sit = np.nan_to_num(
        np.stack([np.ma.filled(d[c], np.nan).astype(np.float32) for c in _CHANGE_SIT_COLS], axis=1)
    ).astype(np.float32)
    np.save(os.path.join(pool_dir, "change.sit.npy"), sit)
    # --- SIM-427: the fielding manager and the incoming arm's rest per row ---
    ctx = _default_game_context(seasons) if game_context is True else game_context
    keys = con.execute(
        "SELECT game_pk, at_bat_number, top_or_bottom, incoming_id FROM _change_pool "
        "ORDER BY game_pk, at_bat_number"
    ).fetchnumpy()
    gpk = np.ma.filled(keys["game_pk"], 0).astype(np.int64)
    abn = np.ma.filled(keys["at_bat_number"], 0).astype(np.int64)
    tob = np.ma.filled(keys["top_or_bottom"], 0).astype(np.int64)
    inc = np.ma.filled(keys["incoming_id"], 0).astype(np.int64)
    manager = np.full(n, -1, dtype=np.int64)
    rest = np.full((n, 3), -1, dtype=np.int64)
    throws_col = np.zeros(n, dtype=np.int64)
    if ctx:
        managers, usage = ctx[0], ctx[1]
        throws: dict = ctx[2] if len(ctx) > 2 else {}
        for i in range(n):
            pair = managers.get(int(gpk[i]))
            if pair is not None:
                m = pair[0] if int(tob[i]) == 0 else pair[1]  # top: the home side fields
                if m is not None:
                    manager[i] = int(m)
            u = usage.get((int(gpk[i]), int(inc[i])))
            if u is not None:
                rest[i] = u
            throws_col[i] = int(throws.get(int(inc[i]), 0))
    import pyarrow as pa

    ctx_tbl = pa.table(
        {
            "game_pk": pa.array(gpk, type=pa.int64()),
            "at_bat_number": pa.array(abn, type=pa.int64()),
            "manager_id": pa.array(manager, type=pa.int64()),
            "in_days_rest": pa.array(rest[:, 0], type=pa.int64()),
            "in_pitched_2d": pa.array(rest[:, 1], type=pa.int64()),
            "in_pitches_3d": pa.array(rest[:, 2], type=pa.int64()),
            "in_throws": pa.array(throws_col, type=pa.int64()),
        }
    )
    con.register("_change_ctx", ctx_tbl)
    con.execute(
        "COPY (SELECT p.game_pk, p.at_bat_number, p.season, p.pitcher_id, p.incoming_id, "
        "p.is_starter, p.new_half, p.changed, p.recency AS recency_weight, p.game_ymd, "
        "c.manager_id, c.in_days_rest, c.in_pitched_2d, c.in_pitches_3d, c.in_throws "
        "FROM _change_pool p LEFT JOIN _change_ctx c USING (game_pk, at_bat_number) "
        "ORDER BY p.game_pk, p.at_bat_number) "
        f"TO '{os.path.join(pool_dir, 'change.meta.parquet')}' (FORMAT parquet)"
    )
    con.unregister("_change_ctx")
    # --- SIM-427: the reliever ROLE sidecar — per incoming arm and season, his
    # entries, the share in a high-leverage spot (inning >= 7, margin within
    # two — the manager computor's own bucket) and his mean entry inning.
    con.execute(
        "COPY (SELECT incoming_id AS pitcher_id, season, count(*) AS n_entries, "
        "avg(CASE WHEN inning >= 7 AND abs(score_diff) <= 2 THEN 1.0 ELSE 0.0 END) "
        "AS hi_lev_share, avg(inning) AS mean_entry_inning "
        "FROM _change_pool WHERE changed = 1 AND is_starter IS NOT NULL "
        "GROUP BY incoming_id, season) "
        f"TO '{os.path.join(pool_dir, 'roles.parquet')}' (FORMAT parquet)"
    )
    n_roles = int(
        con.execute(
            f"SELECT count(*) FROM read_parquet('{os.path.join(pool_dir, 'roles.parquet')}')"
        ).fetchone()[0]
    )
    rates = con.execute(
        "SELECT avg(changed), avg(changed) FILTER (WHERE new_half = 1), "
        "avg(changed) FILTER (WHERE new_half = 0), avg(changed) FILTER (WHERE is_starter = 1), "
        "avg(changed) FILTER (WHERE is_starter = 0), count(DISTINCT game_pk) FROM _change_pool"
    ).fetchone()
    con.execute("DROP TABLE _change_pool")
    manifest = {
        "seasons": [int(s) for s in seasons],
        "count": int(n),
        "sit_cols": list(_CHANGE_SIT_COLS),
        "rates": {
            "changed": (float(rates[0]) if rates[0] is not None else None),
            "changed_new_half": (float(rates[1]) if rates[1] is not None else None),
            "changed_mid_inning": (float(rates[2]) if rates[2] is not None else None),
            "changed_starter": (float(rates[3]) if rates[3] is not None else None),
            "changed_reliever": (float(rates[4]) if rates[4] is not None else None),
        },
        "games": int(rates[5] or 0),
        # SIM-427: the context coverage and the role sidecar's size.
        "manager_coverage": float((manager >= 0).mean()) if n else 0.0,
        "rest_coverage": float((rest[:, 0] >= 0).mean()) if n else 0.0,
        "roles": n_roles,
        # SIM-535: whether every row carries its game date (the cutoff's input).
        "game_ymd": bool(have_date),
    }
    with open(os.path.join(pool_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    log.info(
        "manager_pool[change]: %d boundaries over %d games; change rate %.4f "
        "(new half %.4f, mid-inning %.4f); manager on %.1f%% of rows, incoming-arm rest "
        "on %.1f%%; %d reliever role rows",
        n,
        manifest["games"],
        manifest["rates"]["changed"] or 0.0,
        manifest["rates"]["changed_new_half"] or 0.0,
        manifest["rates"]["changed_mid_inning"] or 0.0,
        100.0 * manifest["manager_coverage"],
        100.0 * manifest["rest_coverage"],
        n_roles,
    )
    return manifest


def load_change_pool(
    con: duckdb.DuckDBPyConnection, art_dir: str, views: dict[str, np.ndarray]
) -> tuple[ChangePool | None, dict[str, tuple[int, float, float]] | None]:
    """Load the pitching-change pool and the relievers' role sidecar; (None,
    None) when the bundle has no manager pool."""
    # SIM-523 part D: the pitching-change opportunity pool (None on a
    # bundle without it).
    change_pool: ChangePool | None = None
    change_roles: dict[str, tuple[int, float, float]] | None = None
    mp_dir = os.path.join(art_dir, "manager_pool")
    if os.path.exists(os.path.join(mp_dir, "manifest.json")):
        cm = con.execute(
            f"SELECT * FROM read_parquet('{os.path.join(mp_dir, 'change.meta.parquet')}')"
        ).fetchnumpy()

        def _cp_take(attr: str, col: str, dtype, fill, *, _m=cm) -> np.ndarray:
            v = views.get(f"change_pool.{attr}")
            if isinstance(v, np.ndarray):
                return v
            return np.asarray(np.ma.filled(_m[col], fill), dtype=dtype)

        def _cp_opt(attr: str, col: str, dtype, *, _m=cm) -> np.ndarray | None:
            # SIM-427: a column an older bundle lacks stays None.
            v = views.get(f"change_pool.{attr}")
            if isinstance(v, np.ndarray):
                return v
            if col not in _m:
                return None
            return np.asarray(np.ma.filled(_m[col], -1), dtype=dtype)

        change_pool = ChangePool(
            sit=take_view(views, "change_pool.sit", os.path.join(mp_dir, "change.sit.npy")),
            pitcher_id=_cp_take("pitcher_id", "pitcher_id", np.int64, 0),
            incoming_id=_cp_take("incoming_id", "incoming_id", np.int64, 0),
            season=_cp_take("season", "season", np.int64, 0),
            is_starter=_cp_take("is_starter", "is_starter", np.int8, 0),
            new_half=_cp_take("new_half", "new_half", np.int8, 0),
            changed=_cp_take("changed", "changed", np.int8, 0),
            recency=(
                views.get("change_pool.recency")
                if isinstance(views.get("change_pool.recency"), np.ndarray)
                else np.nan_to_num(
                    np.ma.filled(cm["recency_weight"], 1.0).astype(np.float32), nan=1.0
                )
            ),
            manager_id=_cp_opt("manager_id", "manager_id", np.int64),
            in_days_rest=_cp_opt("in_days_rest", "in_days_rest", np.int8),
            in_pitched_2d=_cp_opt("in_pitched_2d", "in_pitched_2d", np.int8),
            in_pitches_3d=_cp_opt("in_pitches_3d", "in_pitches_3d", np.int16),
            in_throws=_cp_opt("in_throws", "in_throws", np.int8),
            game_ymd=_cp_opt("game_ymd", "game_ymd", np.int32),
        )
        # SIM-535: a date column that is NULL on every row (a pitch pool
        # without dates) must read as ABSENT, so a cutoff refuses instead
        # of admitting every row through the -1 fill.
        if change_pool.game_ymd is not None and not (change_pool.game_ymd > 0).any():
            change_pool.game_ymd = None
        roles_path = os.path.join(mp_dir, "roles.parquet")
        if os.path.exists(roles_path):
            rr = con.execute(
                f"SELECT pitcher_id, season, n_entries, hi_lev_share, mean_entry_inning "
                f"FROM read_parquet('{roles_path}')"
            ).fetchall()
            change_roles = {
                f"{int(pid)}:{int(se)}": (int(ne), float(hs), float(mi))
                for pid, se, ne, hs, mi in rr
            }
    return change_pool, change_roles
