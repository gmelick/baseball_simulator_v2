"""Export the pitching-change opportunity pool for the manager draw, and load it in a worker.

A row is a plate-appearance boundary while a pitcher is on the mound, changed or not: the
rows with no change give the draw its denominator. ``derived.at_bat_situations`` gives the
boundary (the half, the outs, the bases, the score); ``sim.pitch_pool`` gives the pitcher
of each plate appearance. A row describes the pitcher of the PREVIOUS plate appearance for
that side. It holds his pitches and batters faced so far in the game, and whether he
started. It also says whether the boundary opens a half inning, and whether the manager
replaced him.

The export writes ``manager_pool/change.sit.npy``, ``change.meta.parquet``, the reliever
role table ``roles.parquet`` and ``manifest.json``. The fielding manager and the incoming
arm's rest come from one Postgres read.
"""

from __future__ import annotations

import logging
import os

import duckdb
import numpy as np

from bundle.common import (
    float_columns,
    meta_column,
    optional_meta_column,
    recency_column,
    take_view,
    write_json,
)
from bundle.worker_pools import ChangePool
from db.introspect import duck_columns
from profiles.sql import sql_seasons

log = logging.getLogger("engine_artifacts")


#: The change draw's situation columns: the columns of ``change.sit.npy``.
_CHANGE_SIT_COLS = ["pitch_count", "batters_faced", "inning", "outs", "runners_state", "score_diff"]


def _default_game_context(seasons: list[int]) -> tuple[dict, dict, dict] | None:
    """Read each game's two managers and every appearance's rest from Postgres.

    This is the builder's one Postgres read (``pipeline.bullpen_usage``). Returns the
    ``(managers, usage, throws)`` triple, or None when no DSN is set or the read fails.
    The pool then holds -1 in the manager and rest columns, and the log says so.
    """
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


def build_pitching_change_pool(
    con: duckdb.DuckDBPyConnection,
    out_dir: str,
    seasons: list[int],
    *,
    game_context: tuple[dict, dict, dict] | None | bool = True,
) -> dict:
    """Write the change pool, the reliever role table and the manifest. Returns the manifest.

    The manifest holds the counts and the pool's own change rates. Each row carries the
    FIELDING side's manager (``manager_id``; in the top of an inning the home side
    fields). It also carries the incoming arm's rest as of the game: ``in_days_rest``
    (capped at five), ``in_pitched_2d`` and ``in_pitches_3d``, as
    ``pipeline.bullpen_usage`` defines them. ``game_context`` picks their source:
    ``True`` reads Postgres, a ``(managers, usage, throws)`` triple is used as given (the
    test seam), and ``None`` or ``False`` leaves every row unknown.
    """
    pool_dir = os.path.join(out_dir, "manager_pool")
    os.makedirs(pool_dir, exist_ok=True)
    season_list = sql_seasons(seasons)
    # The row's game date (a YYYYMMDD integer) lets the backtest's cutoff apply to this
    # draw. A pitch pool without dates leaves it NULL, and the cutoff then refuses.
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
    # Every read below orders by (game_pk, at_bat_number), so the sit array and the meta
    # parquet share one row order whatever the scan order.
    d = con.execute(
        f"SELECT {', '.join(_CHANGE_SIT_COLS)} FROM _change_pool ORDER BY game_pk, at_bat_number"
    ).fetchnumpy()
    n = len(d[_CHANGE_SIT_COLS[0]])
    sit = float_columns(d, _CHANGE_SIT_COLS)
    np.save(os.path.join(pool_dir, "change.sit.npy"), sit)
    # The fielding manager and the incoming arm's rest, per row.
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
        managers, usage, throws = ctx
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
    # The reliever role table: per incoming arm and season, his entries, his share of
    # high-leverage entries and his mean entry inning. High leverage is inning 7 or later
    # within two runs, the manager computor's own bucket.
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
        # The share of rows with a manager and with a rest, and the role table's size.
        "manager_coverage": float((manager >= 0).mean()) if n else 0.0,
        "rest_coverage": float((rest[:, 0] >= 0).mean()) if n else 0.0,
        "roles": n_roles,
        # Whether the rows carry a game date (the cutoff's input).
        "game_ymd": bool(have_date),
    }
    write_json(os.path.join(pool_dir, "manifest.json"), manifest)
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
    """Load the change pool and the reliever role table; (None, None) with no manager pool.

    The role table maps ``"pitcher_id:season"`` to (entries, high-leverage share, mean
    entry inning); it is None when the bundle has no ``roles.parquet``.
    """
    change_pool: ChangePool | None = None
    change_roles: dict[str, tuple[int, float, float]] | None = None
    mp_dir = os.path.join(art_dir, "manager_pool")
    if os.path.exists(os.path.join(mp_dir, "manifest.json")):
        cm = con.execute(
            f"SELECT * FROM read_parquet('{os.path.join(mp_dir, 'change.meta.parquet')}')"
        ).fetchnumpy()

        change_pool = ChangePool(
            sit=take_view(views, "change_pool.sit", os.path.join(mp_dir, "change.sit.npy")),
            pitcher_id=meta_column(views, "change_pool.pitcher_id", cm, "pitcher_id", np.int64),
            incoming_id=meta_column(views, "change_pool.incoming_id", cm, "incoming_id", np.int64),
            season=meta_column(views, "change_pool.season", cm, "season", np.int64),
            is_starter=meta_column(views, "change_pool.is_starter", cm, "is_starter", np.int8),
            new_half=meta_column(views, "change_pool.new_half", cm, "new_half", np.int8),
            changed=meta_column(views, "change_pool.changed", cm, "changed", np.int8),
            recency=recency_column(views, "change_pool.recency", cm),
            # A column an older bundle lacks stays None.
            manager_id=optional_meta_column(
                views, "change_pool.manager_id", cm, "manager_id", np.int64, -1
            ),
            in_days_rest=optional_meta_column(
                views, "change_pool.in_days_rest", cm, "in_days_rest", np.int8, -1
            ),
            in_pitched_2d=optional_meta_column(
                views, "change_pool.in_pitched_2d", cm, "in_pitched_2d", np.int8, -1
            ),
            in_pitches_3d=optional_meta_column(
                views, "change_pool.in_pitches_3d", cm, "in_pitches_3d", np.int16, -1
            ),
            in_throws=optional_meta_column(
                views, "change_pool.in_throws", cm, "in_throws", np.int8, -1
            ),
            game_ymd=optional_meta_column(
                views, "change_pool.game_ymd", cm, "game_ymd", np.int32, -1
            ),
        )
        # A date column that is NULL on every row (a pitch pool without dates) must read
        # as ABSENT, so a cutoff refuses instead of admitting every row through the -1 fill.
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
