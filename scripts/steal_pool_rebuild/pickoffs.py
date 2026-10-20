"""Step 1: the snapshot, and the pickoff rows counted from the play records (never
from the builder's SQL)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .common import (
    EXPECT,
    LABELS,
    MEASURED_FIT,
    MEASURED_PICKOFF_ROWS,
    POOL,
    SNAP,
    _log,
    _season_list,
    _table_columns,
)

# ---------------------------------------------------------------------------
# The snapshot and the counted pickoff rows
# ---------------------------------------------------------------------------


def take_snapshot(con: Any, seasons: list[int]) -> None:
    """Every pool row's six values (and its pair) into a TEMP table. NULL
    labels read FALSE, the export's own reading."""
    has_mark = "is_pickoff_row" in _table_columns(con, "sim", "steal_opportunity_pool")
    mark = "COALESCE(is_pickoff_row, FALSE)" if has_mark else "FALSE"
    labels = ", ".join(f"COALESCE({c}, FALSE) AS {c}" for c in LABELS)
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE {SNAP} AS "
        f"SELECT pitch_id, season, game_pk, at_bat_number, target_base, {labels}, "
        f"{mark} AS is_pickoff_row "
        f"FROM {POOL} WHERE season IN ({_season_list(seasons)})"
    )


def count_pickoff_rows(con: Any, seasons: list[int]) -> None:
    """Count, into a TEMP table, every pickoff outcome of the play records
    that fits a pair, and whether the snapshot holds a pitch of its pair.

    This restates the design's rule (§1.3, §4.2) in its own SQL; it does NOT
    run the builder's. An outcome is a pickoff out or an errant pickoff throw.
    It fits the target-2 pair when first base is occupied, second is open and
    the throw went to first or second; the target-3 pair when second is
    occupied, third is open and the throw went to second or third. An out at
    the next base (second for the target-2 pair, third for the target-3 pair)
    is a picked-off caught stealing. A pitch of the pair is a snapshot row
    with ``pitch_id > 0`` in the same plate appearance and pair.
    """
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE {EXPECT} AS
        WITH ev AS (
            SELECT id, game_pk, at_bat_number, game_date, season, base, outs_before,
                   bat_score, fld_score, runner_id, pitcher_id, event_type,
                   (event_type = 'pickoff' AND COALESCE(is_out, FALSE)) AS is_out,
                   (event_type = 'pickoff_error') AS is_err,
                   CASE
                       WHEN (runners_state & 1) = 1 AND (runners_state & 2) = 0
                            AND base IN (1, 2) THEN 2
                       WHEN (runners_state & 2) = 2 AND (runners_state & 4) = 0
                            AND base IN (2, 3) THEN 3
                   END AS pair
            FROM pg.raw.play_events
            WHERE event_type IN ('pickoff', 'pickoff_error')
              AND season IN ({_season_list(seasons)})
        ),
        pitched AS (
            SELECT DISTINCT game_pk, at_bat_number, target_base
            FROM {SNAP} WHERE pitch_id > 0
        )
        SELECT ev.*,
               (ev.is_out AND ev.base = ev.pair) AS is_adv,
               (p.game_pk IS NOT NULL) AS has_pitch,
               (ev.runner_id IS NOT NULL AND ev.pitcher_id IS NOT NULL) AS has_ids
        FROM ev
        LEFT JOIN pitched p
          ON p.game_pk = ev.game_pk AND p.at_bat_number = ev.at_bat_number
         AND p.target_base = ev.pair
        WHERE ev.pair IS NOT NULL AND (ev.is_out OR ev.is_err)
        """
    )


@dataclass
class PickoffCount:
    """The counted pickoff outcomes of one season (:func:`pickoff_summary`)."""

    fit: int = 0  # outcomes that fit a pair
    with_pitch: int = 0  # ... whose pair has a pitch in the plate appearance
    tag_groups: int = 0  # ... distinct (plate appearance, pair): one tag each
    rows: int = 0  # outcomes with no pitch of the pair, a runner and a thrower
    no_id: list[tuple[Any, ...]] = field(default_factory=list)  # the listed ones

    @property
    def folded(self) -> int:
        """Outcomes that share a tag with another in the same plate appearance."""
        return self.with_pitch - self.tag_groups


def pickoff_summary(con: Any, seasons: list[int]) -> dict[int, PickoffCount]:
    """Per season, from the counted table (:func:`count_pickoff_rows`)."""
    out = {int(s): PickoffCount() for s in seasons}
    for s, fit, wp, groups, rows in con.execute(
        f"""
        SELECT season, COUNT(*),
               COUNT(*) FILTER (WHERE has_pitch),
               COUNT(DISTINCT CASE WHEN has_pitch
                     THEN CAST(game_pk AS VARCHAR) || ':' || CAST(at_bat_number AS VARCHAR)
                          || ':' || CAST(pair AS VARCHAR) END),
               COUNT(*) FILTER (WHERE NOT has_pitch AND has_ids)
        FROM {EXPECT} GROUP BY 1
        """
    ).fetchall():
        c = out.setdefault(int(s), PickoffCount())
        c.fit, c.with_pitch, c.tag_groups, c.rows = int(fit), int(wp), int(groups), int(rows)
    for row in con.execute(
        f"SELECT season, id, game_pk, at_bat_number, event_type, runner_id, pitcher_id "
        f"FROM {EXPECT} WHERE NOT has_pitch AND NOT has_ids ORDER BY season, id"
    ).fetchall():
        out.setdefault(int(row[0]), PickoffCount()).no_id.append(tuple(row[1:]))
    return out


def log_pickoff_summary(counts: dict[int, PickoffCount]) -> None:
    for s in sorted(counts):
        c = counts[s]
        measured = MEASURED_PICKOFF_ROWS.get(s)
        _log(
            f"  {s}: outcomes that fit a pair {c.fit:,}; with a pitch of the pair {c.with_pitch:,} "
            f"({c.tag_groups:,} tags, {c.folded:,} folded into another's tag); pickoff rows to "
            f"write {c.rows:,}" + (f" (measured 2026-09-30: {measured})" if measured else "")
        )
        for item in c.no_id:
            _log(f"    listed, no runner or pitcher id (no row possible): {item}")
    total = sum(c.fit for c in counts.values())
    _log(
        f"  outcomes that fit a pair, all seasons: {total:,} (measured 2026-09-30: {MEASURED_FIT:,})"
    )
