"""
scripts/sim523_pool_tests.py — SIM-523 tests 1 + 3 on the REAL pitch pool (read-only).

How big is catcher skill on walks / strikeouts / hit-by-pitches / called strikes
once the pitcher is held fixed — and how much of a catcher-season's raw rate is
really his pitchers? Both questions are answered on the W1 pitch pool (four
seasons, one row per pitch, catcher_id on every row).

Test 1 — hold the pitcher, swap the catcher. For every pitcher-season caught
by two or more catchers (each with >= MIN_PA plate appearances), the rate
difference between the primary catcher and each other catcher. We report the
spread of those paired differences, corrected for sampling noise, relative to
the league rate: the size of "swap the catcher, same pitcher" in real data.

Test 3 — the two-way decomposition. Regress each per-PA outcome on pitcher-
season and catcher-season fixed effects (alternating demeaning). The noise-
corrected spread of the catcher effects is the true catcher skill on that
channel; the spread of RAW catcher-season rates is what a neighbourhood of
"similar catchers" inherits. The ratio says how much of the raw spread is the
pitchers.

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim523_pool_tests.py
"""

from __future__ import annotations

import os
import sys

import duckdb
import numpy as np

DUCKDB_PATH = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
SEASONS = [2023, 2024, 2025, 2026]
MIN_PA = 150  # per (pitcher-season, catcher) cell for test 1
CHANNELS = ("bb", "k", "hbp")


def _pa_table(con):
    """One row per plate appearance: pitcher-season, catcher, outcomes, taken-pitch calls."""
    seasons = ", ".join(str(s) for s in SEASONS)
    return con.execute(f"""
        WITH pitches AS (
            SELECT game_pk, at_bat_number, pitch_number, season, pitcher_id, catcher_id,
                   events, outcome_type
            FROM sim.pitch_pool
            WHERE season IN ({seasons}) AND catcher_id IS NOT NULL AND catcher_id > 0
        ),
        last AS (
            SELECT game_pk, at_bat_number, MAX(pitch_number) AS last_pn
            FROM pitches GROUP BY 1, 2
        ),
        pa AS (
            SELECT p.game_pk, p.at_bat_number, p.season, p.pitcher_id, p.catcher_id,
                   CASE WHEN p.events = 'walk' THEN 1 ELSE 0 END AS bb,
                   CASE WHEN p.events IN ('strikeout', 'strikeout_double_play') THEN 1 ELSE 0 END AS k,
                   CASE WHEN p.events = 'hit_by_pitch' THEN 1 ELSE 0 END AS hbp
            FROM pitches p JOIN last l
              ON p.game_pk = l.game_pk AND p.at_bat_number = l.at_bat_number
             AND p.pitch_number = l.last_pn
        ),
        taken AS (
            SELECT game_pk, at_bat_number,
                   COUNT(*) FILTER (WHERE outcome_type IN ('ball', 'called_strike')) AS taken,
                   COUNT(*) FILTER (WHERE outcome_type = 'called_strike') AS called
            FROM pitches GROUP BY 1, 2
        )
        SELECT pa.season, pa.pitcher_id, pa.catcher_id, pa.bb, pa.k, pa.hbp, t.taken, t.called
        FROM pa JOIN taken t USING (game_pk, at_bat_number)
    """).fetchnumpy()


def _noise_corrected_sd(est, n, p):
    """SD of a set of rate estimates with the binomial sampling variance removed."""
    est = np.asarray(est, float)
    n = np.asarray(n, float)
    w = n / n.sum()
    mean = float((w * est).sum())
    raw_var = float((w * (est - mean) ** 2).sum())
    noise = float((w * (p * (1 - p) / n)).sum())
    return mean, np.sqrt(max(raw_var - noise, 0.0)), np.sqrt(raw_var)


def test1(d):
    print("\n=== TEST 1 — hold the pitcher, swap the catcher (real pool) ===")
    ps = d["season"].astype(np.int64) * 10_000_000 + d["pitcher_id"].astype(np.int64)
    keys = ps * 10_000_000_000 + d["catcher_id"].astype(np.int64)
    uniq, inv = np.unique(keys, return_inverse=True)
    n = np.bincount(inv).astype(float)
    cell_ps = np.zeros(len(uniq), np.int64)
    cell_ps[inv] = ps
    out = {}
    for ch in CHANNELS + ("cs",):
        if ch == "cs":
            num = np.bincount(inv, weights=d["called"].astype(float))
            den = np.bincount(inv, weights=d["taken"].astype(float))
        else:
            num = np.bincount(inv, weights=d[ch].astype(float))
            den = n
        out[ch] = (num, den)
    league = {ch: out[ch][0].sum() / out[ch][1].sum() for ch in out}
    # pitcher-seasons with >= 2 catchers each holding >= MIN_PA plate appearances
    ok = n >= MIN_PA
    order = np.argsort(cell_ps)
    diffs = {ch: [] for ch in out}
    weights = {ch: [] for ch in out}
    pairs = 0
    i = 0
    cps = cell_ps[order]
    while i < len(order):
        j = i
        while j < len(order) and cps[j] == cps[i]:
            j += 1
        cells = [c for c in order[i:j] if ok[c]]
        if len(cells) >= 2:
            cells.sort(key=lambda c: -n[c])
            prim = cells[0]
            for oth in cells[1:]:
                pairs += 1
                for ch in out:
                    num, den = out[ch]
                    r1, r2 = num[prim] / den[prim], num[oth] / den[oth]
                    diffs[ch].append(r2 - r1)
                    # harmonic sample for the pair's noise
                    weights[ch].append(1.0 / (1.0 / den[prim] + 1.0 / den[oth]))
        i = j
    print(f"  pitcher-seasons with 2+ qualifying catchers (>= {MIN_PA} PA each): pairs = {pairs}")
    print(
        f"  {'channel':>8} {'league':>8} {'SD of within-pitcher swap (rel.)':>34} {'raw SD':>8} {'mean |diff|':>12}"
    )
    for ch in out:
        dd = np.asarray(diffs[ch])
        ww = np.asarray(weights[ch])
        p = league[ch]
        w = ww / ww.sum()
        raw_var = float((w * dd**2).sum())
        noise = float((w * (2 * p * (1 - p) / ww)).sum())  # two-cell difference noise
        sd = np.sqrt(max(raw_var - noise, 0.0))
        print(
            f"  {ch:>8} {p:8.4f} {sd / p:33.1%} {np.sqrt(raw_var) / p:8.1%} {np.mean(np.abs(dd)) / p:12.1%}"
        )
    return league


def test3(d, league):
    print("\n=== TEST 3 — two-way decomposition: pitcher-season + catcher-season effects ===")
    ps = d["season"].astype(np.int64) * 10_000_000 + d["pitcher_id"].astype(np.int64)
    cs = d["season"].astype(np.int64) * 10_000_000 + d["catcher_id"].astype(np.int64)
    _, pi = np.unique(ps, return_inverse=True)
    _, ci = np.unique(cs, return_inverse=True)
    np_, nc = pi.max() + 1, ci.max() + 1
    n_p = np.bincount(pi).astype(float)
    n_c = np.bincount(ci).astype(float)
    print(f"  plate appearances = {len(pi):,}  pitcher-seasons = {np_:,}  catcher-seasons = {nc:,}")
    print(
        f"  {'channel':>8} {'league':>8} {'RAW catcher-season SD (rel.)':>30} {'adjusted catcher SD (rel.)':>28} {'share of raw that is pitchers':>30}"
    )
    for ch in CHANNELS:
        y = d[ch].astype(float)
        p = league[ch]
        # raw catcher-season rates (what a neighbourhood inherits)
        raw_rate = np.bincount(ci, weights=y) / n_c
        keep = n_c >= 300
        _, raw_sd, _ = _noise_corrected_sd(raw_rate[keep], n_c[keep], p)
        # alternating demeaning: y - pitcher effect - catcher effect
        yc = y - y.mean()
        a = np.zeros(np_)
        b = np.zeros(nc)
        for _ in range(30):
            a = np.bincount(pi, weights=yc - b[ci], minlength=np_) / n_p
            b = np.bincount(ci, weights=yc - a[pi], minlength=nc) / n_c
        _, adj_sd, _ = _noise_corrected_sd(b[keep] + p, n_c[keep], p)
        share = 1 - (adj_sd / raw_sd) if raw_sd > 0 else float("nan")
        print(f"  {ch:>8} {p:8.4f} {raw_sd / p:29.1%} {adj_sd / p:27.1%} {share:29.0%}")
    print(
        "  Read: 'adjusted' is the true catcher skill on that channel after the pitcher is held fixed;"
    )
    print(
        "  'raw' is what a neighbourhood of similar catcher-seasons carries; the share column is the pitchers."
    )


def main() -> int:
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        d = _pa_table(con)
    finally:
        con.close()
    d = {k: (np.ma.filled(v, 0) if np.ma.isMaskedArray(v) else v) for k, v in d.items()}
    league = test1(d)
    test3(d, league)
    print(
        "\n  The simulator's receiving weight moved the AVERAGE game by walks -3.6%, hit-by-pitch -10.9%"
    )
    print("  (the 12x500 lane, 2026-09-04). Compare those to the adjusted catcher SDs above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
