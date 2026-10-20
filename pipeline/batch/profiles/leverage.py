"""The leverage index and the run-expectancy matrix."""

from __future__ import annotations

import logging
from datetime import date

import duckdb

log = logging.getLogger("player_profile_computor")


# Leverage index simplified bins (used for manager profiles)
LI_LOW = 0.7


LI_HIGH = 1.5


RE24_APPROX = {
    (0, 0b001): 0.83,
    (0, 0b011): 1.45,
    (0, 0b111): 2.20,
    (1, 0b001): 0.50,
    (1, 0b011): 0.90,
    (1, 0b111): 1.50,
    (2, 0b000): 0.10,
    (0, 0b000): 0.48,
    (1, 0b000): 0.26,
    (2, 0b001): 0.22,
}


def compute_leverage_index(
    inning: int,
    outs: int,
    runners_state: int,  # bitmask 0-7
    score_diff: int,  # bat_score - fld_score, capped
) -> float:
    """
    Simplified leverage index based on the Tango/Litchman framework.

    Canonical spec: production mirrors this formula as an inline SQL expression
    when building ``derived.at_bat_situations`` (see ``_build_at_bat_situations``);
    keep the two in sync.

    Full LI requires win-probability tables; this implementation uses a
    well-calibrated approximation that captures the key drivers:
      - Late innings increase leverage
      - Close score differential increases leverage
      - Runners on base increase leverage
      - Two outs increase leverage slightly

    Phase 5 can swap in a full win-probability table lookup if desired.
    The approximation is sufficient for manager tendency profiling.
    """
    # Base inning weight: increases in later innings
    if inning <= 3:
        inning_factor = 0.6
    elif inning <= 6:
        inning_factor = 0.9
    elif inning <= 8:
        inning_factor = 1.3
    else:
        inning_factor = 1.5  # 9th+

    # Score differential factor: close games have higher leverage
    abs_diff = min(abs(score_diff), 5)
    score_factor = max(0.3, 1.5 - abs_diff * 0.24)

    # Runners on base: more runners = more scoring potential
    runners_on = bin(runners_state).count("1")
    runner_factor = 1.0 + runners_on * 0.15

    # Outs factor: two outs increases pressure slightly
    outs_factor = 1.0 if outs < 2 else 1.1

    return inning_factor * score_factor * runner_factor * outs_factor


# ---------------------------------------------------------------------------
# Run expectancy matrix builder
# ---------------------------------------------------------------------------


def build_run_expectancy_matrix(
    conn: duckdb.DuckDBPyConnection, seasons: list[int], asof: date | None = None
) -> dict:
    """
    Build the 24-state run expectancy matrix from play-by-play data.

    Returns a dict mapping (outs, runners_state) -> expected_runs
    that can also be written to derived.run_expectancy_matrix.

    SIM-537: ``asof`` restricts the underlying plays to ``game_date <=
    asof`` — this matrix feeds ``dp_run_value`` inside
    ``_compute_dp_metrics``, so an un-cut matrix would leak future
    run-scoring context into an as-of build. The persisted table is keyed
    by ``season_range`` (a string like "2023-2026"), not by an individual
    season, so a cutoff build and the live production build of the SAME
    seasons would share one key and silently overwrite each other. Rather
    than widen that key, an as-of build (``asof is not None``) skips the
    DELETE/INSERT into ``derived.run_expectancy_matrix`` entirely and
    returns the in-memory dict only — nothing else reads that table
    point-in-time-sensitively, so the live table stays untouched.
    """
    season_list = ", ".join(str(s) for s in seasons)
    log.info("Building run expectancy matrix for seasons %s …", seasons)
    date_cutoff = f"AND game_date <= DATE '{asof.isoformat()}'" if asof is not None else ""

    # Compute runs scored from each base-out state to end of inning
    df = conn.execute(f"""
        WITH plays AS (
            SELECT
                game_pk,
                inning,
                inning_topbot,
                at_bat_number,
                outs,
                -- Runners state bitmask: bit0=1B, bit1=2B, bit2=3B
                (CASE WHEN on_1b IS NOT NULL THEN 1 ELSE 0 END)
                + (CASE WHEN on_2b IS NOT NULL THEN 2 ELSE 0 END)
                + (CASE WHEN on_3b IS NOT NULL THEN 4 ELSE 0 END) AS runners_state,
                home_score,
                away_score,
                CASE WHEN inning_topbot = 'Top' THEN away_score ELSE home_score END AS bat_score
            FROM pg.raw.pitches
            WHERE season IN ({season_list})
              AND inning <= 8            -- exclude 9th+ for unbiased estimates
              AND events IS NOT NULL     -- one row per PA
              {date_cutoff}
        ),
        -- Get the final score for each half-inning.
        --
        -- SIM-458 (re-landed 2026-08-13; first landed in c11c919, reverted with
        -- it for the OTHER tickets' defect): this was `MAX(bat_score)` over the
        -- `plays` rows. `bat_score` is the score ENTERING a plate appearance, so
        -- runs scored on the LAST plate appearance of a half-inning appear on no
        -- later row and were invisible. Every run-expectancy value was biased
        -- LOW. Measured on 2024, innings 1-8: 126 of 39,543 half-innings (0.3%)
        -- and 146 runs — concentrated exactly where a run scores on a play that
        -- also ends the inning. The matrix feeds BOTH the SIM-499 run-value
        -- ledger and the SIM-473 baserunner weighting, so it is load-bearing
        -- twice.
        --
        -- BOTH available formulas are LOWER BOUNDS and they miss different
        -- runs: `MAX(bat_score)` misses the last plate appearance; the
        -- running-score sum misses any run whose `runs_on_pitch` is unset (11
        -- half-innings in 2024). Neither can invent a run, so GREATEST of the
        -- two is the tighter bound. Read from the RAW table, not from `plays`:
        -- `plays` keeps one row per plate appearance (`events IS NOT NULL`),
        -- and a run can score on a pitch that ends no plate appearance — a
        -- steal of home, a wild pitch, a balk. Summing inside `plays` would
        -- miss exactly those.
        inning_final AS (
            SELECT
                game_pk, inning, inning_topbot,
                GREATEST(
                    MAX(CASE WHEN inning_topbot = 'Top' THEN away_score ELSE home_score END),
                    MIN(CASE WHEN inning_topbot = 'Top' THEN away_score ELSE home_score END)
                        + SUM(COALESCE(runs_on_pitch, 0))
                ) AS final_bat_score
            FROM pg.raw.pitches
            WHERE season IN ({season_list})
              AND inning <= 8
              {date_cutoff}
            GROUP BY game_pk, inning, inning_topbot
        ),
        scored AS (
            SELECT
                p.outs,
                p.runners_state,
                (f.final_bat_score - p.bat_score) AS runs_to_end_of_inning
            FROM plays p
            JOIN inning_final f
              ON p.game_pk = f.game_pk
              AND p.inning = f.inning
              AND p.inning_topbot = f.inning_topbot
        )
        SELECT
            outs,
            runners_state,
            AVG(runs_to_end_of_inning) AS expected_runs,
            COUNT(*) AS sample_size
        FROM scored
        GROUP BY outs, runners_state
        ORDER BY outs, runners_state
    """).fetchdf()

    re_matrix = {}
    season_range = f"{min(seasons)}-{max(seasons)}"

    for _, row in df.iterrows():
        key = (int(row["outs"]), int(row["runners_state"]))
        re_matrix[key] = float(row["expected_runs"])

    if asof is not None:
        # SIM-537: an as-of build never persists — see the docstring.
        log.info(
            "  RE matrix built as of %s: %d states (not persisted — a cutoff "
            "build keeps its matrix in memory only).",
            asof,
            len(re_matrix),
        )
        return re_matrix

    # Write to DuckDB table
    conn.execute("DELETE FROM derived.run_expectancy_matrix WHERE season_range = ?", [season_range])
    for _, row in df.iterrows():
        conn.execute(
            """
            INSERT INTO derived.run_expectancy_matrix
            (season_range, outs, runners_state, expected_runs, sample_size)
            VALUES (?, ?, ?, ?, ?)
        """,
            [
                season_range,
                int(row["outs"]),
                int(row["runners_state"]),
                float(row["expected_runs"]),
                int(row["sample_size"]),
            ],
        )

    log.info("  RE matrix built: %d states", len(re_matrix))
    return re_matrix
