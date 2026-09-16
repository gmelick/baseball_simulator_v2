#!/usr/bin/env python
"""
scripts/sim545_boxscore_audit.py
================================
The validation study for the official box-score ground truth (SIM-545, the
sub-ticket of the unpriced-prop-markets work, SIM-421).

WHY
---
Before the platform trusts a per-player total it DERIVES from ``raw.pitches``
(the event label for a hit, the runner flags for a run, the steal columns for a
stolen base), it has to know how far that derivation sits from the official
box score — over hundreds of games, never dozens (CLAUDE.md §2b: a 70-game
sample reported "100%" on a metric that 950 games disproved). This script
derives every prop total the platform can build today, compares it with
``raw.game_player_stats`` (Alembic 0023) for the same games, and prints
per-stat: rows compared, the exact-match rate, the mean absolute difference
and the ten worst players.

WHAT IT DERIVES (the platform's current recipe)
------------------------------------------------
Per batter, from the plate-appearance event label: H, HR, TB, 1B, 2B, 3B.
Per batter, from the runner flags: R (``runner_{1b,2b,3b}_scored`` credits the
runner on ``on_{1b,2b,3b}``; ``runs_on_pitch`` minus those flags is the
batter's own run), SB (``sb_success_2b`` → the runner on 1B, ``_3b`` → 2B,
``_home`` → 3B, folded with the PA-ending ``stolen_base_*`` event per
``pipeline.statcast_events.sql_steal_success``), RBI (``rbis_on_pitch``).
Per pitcher: K and BB from the event label, H_ALLOWED from the hit events,
ER from ``earned_runs_on_pitch``, OUTS from
``pipeline.statcast_events.sql_outs_recorded`` plus the pickoff outs in
``raw.play_events`` (they live in no pitch row), and the intentional walks in
``raw.play_events`` added to BB (the box counts them; no pitch row carries one).

USAGE (the orchestrator runs this; it is read-only against the database)
------------------------------------------------------------------------
    python scripts/sim545_boxscore_audit.py --seasons 2024 --max-games 500
    python scripts/sim545_boxscore_audit.py --game-pks 746437 746438

The SQL builders and the comparison are pure functions, unit-tested on
hand-built rows in ``tests/unit/test_sim545_boxscore_reader.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from pipeline.statcast_events import sql_outs_recorded, sql_steal_success  # noqa: E402
from simulation.prop_validation import real_props_from_boxscore_rows  # noqa: E402

log = logging.getLogger("sim545_boxscore_audit")

DEFAULT_DSN = os.environ.get(
    "BASEBALL_DB_DSN",
    "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim",
)

#: The batter stats the derivation produces, in report order.
BATTER_STATS: tuple[str, ...] = ("H", "HR", "TB", "1B", "2B", "3B", "R", "SB", "RBI")
#: The pitcher stats the derivation produces, in report order.
PITCHER_STATS: tuple[str, ...] = ("K", "BB", "H_ALLOWED", "OUTS", "ER")

#: Event label → total bases (the hit events).
_HIT_BASES: dict[str, int] = {"single": 1, "double": 2, "triple": 3, "home_run": 4}
_HIT_PROP: dict[str, str] = {"single": "1B", "double": "2B", "triple": "3B", "home_run": "HR"}
_STRIKEOUT_EVENTS = frozenset({"strikeout", "strikeout_double_play"})
#: Both walk labels count here: the box's ``baseOnBalls`` includes intentional walks.
_WALK_EVENTS = frozenset({"walk", "intent_walk"})

#: Games per SQL round-trip (bounds the pitch-row result set in memory).
_CHUNK_GAMES = 100

# ---------------------------------------------------------------------------
# SQL builders (pure)
# ---------------------------------------------------------------------------


def pitch_rows_sql() -> str:
    """The per-pitch SELECT the derivation reads. ``$1`` is an INTEGER[] of game_pks.

    ``outs_recorded`` and the three ``sb_ok_*`` columns are computed in SQL by
    the shared ``pipeline.statcast_events`` helpers, so the audit measures the
    platform's OWN out and steal labels, not a re-derivation.
    """
    return f"""
        SELECT p.game_pk, p.batter, p.pitcher, p.events,
               p.on_1b, p.on_2b, p.on_3b,
               p.runner_1b_scored, p.runner_2b_scored, p.runner_3b_scored,
               p.runs_on_pitch, p.rbis_on_pitch, p.earned_runs_on_pitch,
               {sql_steal_success("2b", "p.")} AS sb_ok_2b,
               {sql_steal_success("3b", "p.")} AS sb_ok_3b,
               {sql_steal_success("home", "p.")} AS sb_ok_home,
               {sql_outs_recorded("p.")} AS outs_recorded
        FROM raw.pitches p
        WHERE p.game_pk = ANY($1)
    """


def play_events_sql() -> str:
    """The non-pitch plays the derivation adds: pickoff outs + intentional walks."""
    return """
        SELECT game_pk, pitcher_id, event_type, is_out
        FROM raw.play_events
        WHERE game_pk = ANY($1)
          AND (is_out OR event_type = 'intent_walk')
    """


def boxscore_rows_sql() -> str:
    """The official rows for the same games (``raw.game_player_stats``)."""
    return """
        SELECT game_pk, player_id, played_bat, played_pitch,
               h, b2, b3, hr, r, rbi, sb, tb,
               p_k, p_bb, p_er, p_outs, p_h
        FROM raw.game_player_stats
        WHERE game_pk = ANY($1)
    """


def games_sql(seasons: Sequence[int], max_games: int | None) -> str:
    """Final games with box-score rows for ``seasons``, ordered by game_pk."""
    sl = ", ".join(str(int(s)) for s in seasons)
    limit = f"LIMIT {int(max_games)}" if max_games else ""
    return f"""
        SELECT g.game_pk
        FROM raw.games g
        WHERE g.status = 'Final'
          AND g.season IN ({sl})
          AND g.home_score_final IS NOT NULL
          AND EXISTS (SELECT 1 FROM raw.game_player_stats s WHERE s.game_pk = g.game_pk)
        ORDER BY g.game_pk
        {limit}
    """


# ---------------------------------------------------------------------------
# The derivation (pure, over row mappings)
# ---------------------------------------------------------------------------

Key = tuple[int, int]  # (game_pk, player_id)


def _blank(stats: Sequence[str]) -> dict[str, int]:
    return dict.fromkeys(stats, 0)


def derive_totals(
    pitch_rows: Iterable[Mapping[str, Any]],
    play_event_rows: Iterable[Mapping[str, Any]] = (),
) -> tuple[dict[Key, dict[str, int]], dict[Key, dict[str, int]]]:
    """Per-player game totals from pitch rows the way the platform derives them.

    ``pitch_rows`` carry the columns of :func:`pitch_rows_sql` (``events`` is
    NULL on every non-terminal pitch); ``play_event_rows`` carry the columns of
    :func:`play_events_sql`. Returns ``(batters, pitchers)`` keyed by
    ``(game_pk, player_id)``; every value dict holds every stat in
    :data:`BATTER_STATS` / :data:`PITCHER_STATS`.
    """
    batters: dict[Key, dict[str, int]] = {}
    pitchers: dict[Key, dict[str, int]] = {}

    def bat(game_pk: int, pid: int) -> dict[str, int]:
        return batters.setdefault((int(game_pk), int(pid)), _blank(BATTER_STATS))

    def pit(game_pk: int, pid: int) -> dict[str, int]:
        return pitchers.setdefault((int(game_pk), int(pid)), _blank(PITCHER_STATS))

    for row in pitch_rows:
        game_pk = int(row["game_pk"])
        batter_id = row["batter"]
        pitcher_id = row["pitcher"]
        b = bat(game_pk, batter_id) if batter_id is not None else None
        p = pit(game_pk, pitcher_id) if pitcher_id is not None else None

        # --- the plate-appearance result (terminal pitch only) ---
        event = str(row.get("events") or "").strip().lower()
        if event:
            bases = _HIT_BASES.get(event)
            if bases is not None:
                if b is not None:
                    b["H"] += 1
                    b["TB"] += bases
                    b[_HIT_PROP[event]] += 1
                if p is not None:
                    p["H_ALLOWED"] += 1
            if p is not None:
                if event in _STRIKEOUT_EVENTS:
                    p["K"] += 1
                elif event in _WALK_EVENTS:
                    p["BB"] += 1

        # --- every pitch: runs, steals, RBI, earned runs, outs ---
        flagged = 0
        for base in ("1b", "2b", "3b"):
            if bool(row.get(f"runner_{base}_scored")):
                flagged += 1
                runner = row.get(f"on_{base}")
                if runner is not None:
                    bat(game_pk, runner)["R"] += 1
        own = int(row.get("runs_on_pitch") or 0) - flagged
        if own > 0 and b is not None:
            b["R"] += own
        for flag, base in (("sb_ok_2b", "1b"), ("sb_ok_3b", "2b"), ("sb_ok_home", "3b")):
            if bool(row.get(flag)):
                runner = row.get(f"on_{base}")
                if runner is not None:
                    bat(game_pk, runner)["SB"] += 1
        if b is not None:
            b["RBI"] += int(row.get("rbis_on_pitch") or 0)
        if p is not None:
            p["ER"] += int(row.get("earned_runs_on_pitch") or 0)
            p["OUTS"] += int(row.get("outs_recorded") or 0)

    for row in play_event_rows:
        pid = row.get("pitcher_id")
        if pid is None:
            continue
        p = pit(int(row["game_pk"]), pid)
        if bool(row.get("is_out")):
            p["OUTS"] += 1
        if str(row.get("event_type") or "") == "intent_walk":
            p["BB"] += 1

    return batters, pitchers


def official_totals(
    box_rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[Key, dict[str, int]], dict[Key, dict[str, int]]]:
    """The official totals keyed like :func:`derive_totals`, via the shared reader.

    Groups the rows by game and runs
    :func:`simulation.prop_validation.real_props_from_boxscore_rows` per game,
    so the audit grades the SAME reader the validation lanes use.
    """
    by_game: dict[int, list[Mapping[str, Any]]] = {}
    for row in box_rows:
        by_game.setdefault(int(row["game_pk"]), []).append(row)
    batters: dict[Key, dict[str, int]] = {}
    pitchers: dict[Key, dict[str, int]] = {}
    for game_pk, rows in by_game.items():
        b, p = real_props_from_boxscore_rows(rows)
        for pid, totals in b.items():
            batters[(game_pk, pid)] = totals
        for pid, totals in p.items():
            pitchers[(game_pk, pid)] = totals
    return batters, pitchers


# ---------------------------------------------------------------------------
# The comparison (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorstRow:
    """One of the largest disagreements for a stat."""

    abs_diff: int
    game_pk: int
    player_id: int
    derived: int
    official: int


@dataclass(slots=True)
class StatComparison:
    """Derived-vs-official agreement for ONE stat over every official player-game."""

    stat: str
    n: int = 0
    n_exact: int = 0
    abs_diff_sum: int = 0
    #: Sum of (derived - official): a signed bias (negative = the derivation under-counts).
    signed_diff_sum: int = 0
    worst: list[WorstRow] = field(default_factory=list)

    @property
    def exact_rate(self) -> float:
        return self.n_exact / self.n if self.n else float("nan")

    @property
    def mean_abs_diff(self) -> float:
        return self.abs_diff_sum / self.n if self.n else float("nan")

    @property
    def mean_signed_diff(self) -> float:
        return self.signed_diff_sum / self.n if self.n else float("nan")


def compare_totals(
    derived: Mapping[Key, Mapping[str, int]],
    official: Mapping[Key, Mapping[str, int]],
    stats: Sequence[str],
    *,
    n_worst: int = 10,
) -> list[StatComparison]:
    """Compare the derived totals with the official ones, stat by stat.

    The row universe is the OFFICIAL player-games (a player the box says
    played). A player-game the derivation never saw reads 0 on every stat —
    that is exactly the "the pitch data lost him" case the audit exists to
    surface. The ``worst`` list holds the ``n_worst`` largest absolute
    differences, ties broken by (game_pk, player_id) for a stable report.
    """
    out: list[StatComparison] = []
    for stat in stats:
        cmp = StatComparison(stat=stat)
        rows: list[WorstRow] = []
        for key, off_totals in official.items():
            if stat not in off_totals:
                continue
            official_v = int(off_totals[stat])
            derived_v = int(derived.get(key, {}).get(stat, 0))
            diff = derived_v - official_v
            cmp.n += 1
            cmp.signed_diff_sum += diff
            cmp.abs_diff_sum += abs(diff)
            if diff == 0:
                cmp.n_exact += 1
            else:
                rows.append(WorstRow(abs(diff), key[0], key[1], derived_v, official_v))
        rows.sort(key=lambda w: (-w.abs_diff, w.game_pk, w.player_id))
        cmp.worst = rows[:n_worst]
        out.append(cmp)
    return out


def derived_only_keys(derived: Mapping[Key, Any], official: Mapping[Key, Any]) -> list[Key]:
    """Player-games the derivation credits but the box says did not play."""
    return sorted(k for k in derived if k not in official)


def format_report(
    batter_cmp: Sequence[StatComparison],
    pitcher_cmp: Sequence[StatComparison],
    *,
    n_games: int,
    batter_only: Sequence[Key] = (),
    pitcher_only: Sequence[Key] = (),
) -> str:
    """Render the per-stat table + the worst rows as plain text."""
    lines: list[str] = []
    lines.append(f"SIM-545 box-score audit — {n_games} games")
    lines.append("")
    for title, cmps in (("BATTERS", batter_cmp), ("PITCHERS", pitcher_cmp)):
        lines.append(title)
        lines.append(f"  {'stat':<10}{'rows':>8}{'exact':>9}{'mean|d|':>10}{'bias':>9}")
        for c in cmps:
            lines.append(
                f"  {c.stat:<10}{c.n:>8}{c.exact_rate:>9.4f}{c.mean_abs_diff:>10.4f}"
                f"{c.mean_signed_diff:>+9.4f}"
            )
        lines.append("")
        for c in cmps:
            if not c.worst:
                continue
            lines.append(
                f"  worst {c.stat} (|derived - official|, game_pk, player_id, derived, official)"
            )
            for w in c.worst:
                lines.append(
                    f"    {w.abs_diff:>3}  {w.game_pk:>8}  {w.player_id:>7}  "
                    f"{w.derived:>4}  {w.official:>4}"
                )
        lines.append("")
    if batter_only or pitcher_only:
        lines.append(
            f"derived-only player-games (the box says did not play): "
            f"{len(batter_only)} batters, {len(pitcher_only)} pitchers"
        )
        for key in list(batter_only)[:10]:
            lines.append(f"    batter  game {key[0]} player {key[1]}")
        for key in list(pitcher_only)[:10]:
            lines.append(f"    pitcher game {key[0]} player {key[1]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The database run (the orchestrator's job — read-only)
# ---------------------------------------------------------------------------


async def _fetch_game_pks(conn: Any, args: argparse.Namespace) -> list[int]:
    if args.game_pks:
        return sorted({int(g) for g in args.game_pks})
    rows = await conn.fetch(games_sql(sorted({int(s) for s in args.seasons}), args.max_games))
    return [int(r["game_pk"]) for r in rows]


async def run(args: argparse.Namespace) -> int:
    if not args.game_pks and not args.seasons:
        log.error("pass --seasons or --game-pks")
        return 2

    import asyncpg

    conn = await asyncpg.connect(args.dsn)
    try:
        game_pks = await _fetch_game_pks(conn, args)
        log.info("SIM-545 audit: %d games with box-score rows.", len(game_pks))
        if not game_pks:
            log.warning("No games to audit — run scripts/load_official_boxscores.py first.")
            return 0
        derived_b: dict[Key, dict[str, int]] = {}
        derived_p: dict[Key, dict[str, int]] = {}
        official_b: dict[Key, dict[str, int]] = {}
        official_p: dict[Key, dict[str, int]] = {}
        for start in range(0, len(game_pks), _CHUNK_GAMES):
            chunk = game_pks[start : start + _CHUNK_GAMES]
            pitch_rows = await conn.fetch(pitch_rows_sql(), chunk)
            event_rows = await conn.fetch(play_events_sql(), chunk)
            box_rows = await conn.fetch(boxscore_rows_sql(), chunk)
            b, p = derive_totals(pitch_rows, event_rows)
            derived_b.update(b)
            derived_p.update(p)
            ob, op_ = official_totals(box_rows)
            official_b.update(ob)
            official_p.update(op_)
            log.info(
                "  %d/%d games read ...", min(start + _CHUNK_GAMES, len(game_pks)), len(game_pks)
            )
    finally:
        await conn.close()

    batter_cmp = compare_totals(derived_b, official_b, BATTER_STATS)
    pitcher_cmp = compare_totals(derived_p, official_p, PITCHER_STATS)
    print(
        format_report(
            batter_cmp,
            pitcher_cmp,
            n_games=len(game_pks),
            batter_only=derived_only_keys(derived_b, official_b),
            pitcher_only=derived_only_keys(derived_p, official_p),
        )
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Compare raw.pitches-derived player totals with the official box score (SIM-545)."
    )
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (read-only).")
    p.add_argument("--seasons", type=int, nargs="*", default=[], help="Seasons to audit.")
    p.add_argument("--max-games", type=int, default=None, help="Cap games (a smoke run).")
    p.add_argument("--game-pks", type=int, nargs="*", default=[], help="Audit these games only.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
