"""Record which pitchers each team can use in each game, in ``raw.game_bullpen_availability``.

``raw.game_lineups`` records who played, not who could play. The manager usage
profile needs both: a reliever the manager held back differs from one who
could not pitch.

The module writes one row for every pitcher on each team's roster, used or not.
Two sources feed a row:

* the rest and the recent workload (``days_rest``, ``pitches_last_3d``,
  ``back_to_back``). :func:`compute_bullpen_workload` derives them from
  ``raw.pitches``. An arm
  that did not pitch in the game takes his rest from his appearance timeline
  (:func:`_rest_as_of`), so an unused arm who threw yesterday still reads
  unavailable.
* the active roster and the injured list (IL) of each team on the game date,
  from the MLB Stats API roster endpoint.

:meth:`BullpenAvailabilityIngest.decide` takes the first rule that holds:

* off the active roster, or on the IL: unavailable, reason ``IL``;
* pitched the day before: unavailable, reason ``rest``;
* threw :data:`REST_PITCHES_3D` or more pitches in the last 3 days: unavailable,
  reason ``recent_use``;
* else: available, reason ``active``.

``_mlb_get`` is the one network seam and ``_persist`` the one database seam;
the tests stub both. Run it (``--dsn`` defaults to ``BASEBALL_DB_DSN``; a
season reads the network for every game, so it is slow)::

    python -m ingest.load_bullpen_availability --seasons 2024 --duckdb /data/baseball_sim.duckdb
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from db.dsn import require_dsn
from ingest.mlb import client as mlb_client
from ingest.mlb.schedule import played_index, schedule_entries

if TYPE_CHECKING:
    import duckdb
    import pandas as pd

log = logging.getLogger("ingest.load_bullpen_availability")

#: The pitches in the last 3 calendar days that make a reliever unavailable
#: ('recent_use'). A conservative guess, not a fit.
REST_PITCHES_3D = 45

#: The roster ``status.code`` values of an arm on the active roster. Every other
#: code (the IL codes, 'RM' removed, 'DES' designated, 'SU' suspended) reads
#: unavailable.
_ACTIVE_STATUS_CODES = frozenset({"A"})

#: The IL code prefix of the newer feeds ('IL10', 'IL60'); :meth:`_is_il_status`
#: reads the older 'D' codes apart.
_IL_STATUS_PREFIXES = ("IL",)

#: The MLB ``position.code`` of a pitcher. The table holds pitchers only.
_PITCHER_POSITION_CODES = frozenset({"1"})


@dataclass
class PitcherStatus:
    """One pitcher's MLB roster status for a team on a date."""

    pitcher_id: int
    on_active_roster: bool
    on_il: bool


@dataclass
class AvailabilityRow:
    """One pitcher's availability for one game: one ``raw.game_bullpen_availability`` row."""

    game_pk: int
    team_id: int
    pitcher_id: int
    available: bool
    reason: str  # 'active' | 'IL' | 'rest' | 'recent_use'
    days_rest: int | None
    pitches_last_3d: int
    back_to_back: bool
    # Reserved: no code sets it, so every stored row reads False. Do not read it
    # as a signal.
    is_starter: bool = False
    source: str = "mlb_api"  # 'mlb_api' | 'workload' (a pitched arm the roster read missed)

    def as_tuple(self) -> tuple[Any, ...]:
        """The row's values in the INSERT column order of ``_persist``."""
        return (
            self.game_pk,
            self.team_id,
            self.pitcher_id,
            self.available,
            self.reason,
            self.days_rest,
            self.pitches_last_3d,
            self.back_to_back,
            self.is_starter,
            self.source,
        )


@dataclass
class _GameMeta:
    """The date and the two team ids of one game."""

    date: str  # 'YYYY-MM-DD'
    home_team_id: int
    away_team_id: int


@dataclass
class BullpenAvailabilityIngest:
    """Read the rosters, decide each pitcher's availability and store the rows.

    ``pg_dsn`` is the Postgres DSN the default :meth:`_persist` writes with; a
    test that overrides ``_persist`` omits it. ``rest_pitches_3d`` is the
    ``recent_use`` threshold. ``timeout`` is the HTTP time-out in seconds per
    MLB call.
    """

    pg_dsn: str | None = None
    rest_pitches_3d: int = REST_PITCHES_3D
    timeout: float = 15.0
    _meta_cache: dict[int, _GameMeta | None] = field(default_factory=dict, repr=False)

    # ----------------------------------------------------------------- network
    def _mlb_get(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """GET an MLB Stats API endpoint (the network seam the tests stub)."""
        return mlb_client.get_json(
            f"{mlb_client.API_V1}/{path}",
            params,
            timeout_s=self.timeout,
            policy=mlb_client.DEFAULT_POLICY,
            logger=log,
            label="The bullpen load",
        )

    # ----------------------------------------------------- the game's date and teams
    def _resolve_game_meta(self, game_pk: int) -> _GameMeta | None:
        """The date and the two team ids of ``game_pk``, from the MLB schedule; None on failure.

        The date is the schedule's ``officialDate``: the local date of play.
        Never cut it from ``gameDate``, a UTC time. A western night game's UTC
        date is the next day, and the roster read would then take the wrong
        day's roster. A made-up game takes its played entry (``played_index``).
        """
        if game_pk in self._meta_cache:
            return self._meta_cache[game_pk]
        meta: _GameMeta | None = None
        try:
            data = self._mlb_get("schedule", {"sportId": 1, "gamePk": game_pk})
            entries = schedule_entries(data)
            game = entries[played_index(entries)][1]  # IndexError when none: caught below
            meta = _GameMeta(
                date=str(game["officialDate"]),
                home_team_id=int(game["teams"]["home"]["team"]["id"]),
                away_team_id=int(game["teams"]["away"]["team"]["id"]),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("SIM-433: could not resolve game_pk %s: %s", game_pk, exc)
        self._meta_cache[game_pk] = meta
        return meta

    # ----------------------------------------------------------------- parsing
    @staticmethod
    def _is_il_status(code: str) -> bool:
        """True when a roster ``status.code`` means the injured list.

        The older codes are 'D' and the days ('D10', 'D15', 'D60'); the newer
        feeds use an 'IL' prefix ('IL10'). 'DES' (designated for assignment)
        also starts with 'D' and is not the IL, so the 'D' match needs a digit
        after the 'D'.
        """
        c = (code or "").upper()
        if c.startswith("D") and c[1:2].isdigit():
            return True
        return any(c.startswith(p) for p in _IL_STATUS_PREFIXES)

    @classmethod
    def parse_roster(cls, payload: Mapping[str, Any]) -> dict[int, PitcherStatus]:
        """The pitchers of a roster payload, as ``{pitcher_id: PitcherStatus}``.

        A pitcher has ``position.code`` '1' or ``position.type`` 'Pitcher'.
        Status 'A' is the active roster; an IL code is the injured list. An
        entry with no position or no status block reads as not a pitcher or not
        active, so an odd entry never passes as an available arm.
        """
        out: dict[int, PitcherStatus] = {}
        for entry in payload.get("roster", []) or []:
            person = entry.get("person") or {}
            pid = person.get("id")
            if pid is None:
                continue
            position = entry.get("position") or {}
            pos_code = str(position.get("code", ""))
            pos_type = str(position.get("type", ""))
            is_pitcher = pos_code in _PITCHER_POSITION_CODES or pos_type == "Pitcher"
            if not is_pitcher:
                continue
            status = entry.get("status") or {}
            status_code = str(status.get("code", ""))
            on_il = cls._is_il_status(status_code)
            on_active = status_code in _ACTIVE_STATUS_CODES and not on_il
            out[int(pid)] = PitcherStatus(
                pitcher_id=int(pid),
                on_active_roster=on_active,
                on_il=on_il,
            )
        return out

    def _fetch_team_pitchers(self, team_id: int, date_str: str) -> dict[int, PitcherStatus]:
        """A team's pitching staff on a date, with each arm's roster status.

        The ``active`` roster gives the active staff. An injured arm drops off
        that roster, so the ``fullSeason`` roster adds the IL arms. A failed
        read logs and the method returns what it has.
        """
        statuses: dict[int, PitcherStatus] = {}
        try:
            active = self._mlb_get(
                f"teams/{team_id}/roster",
                {"rosterType": "active", "date": date_str},
            )
            statuses.update(self.parse_roster(active))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "SIM-433: active roster fetch failed (team %s, %s): %s",
                team_id,
                date_str,
                exc,
            )
        try:
            full = self._mlb_get(
                f"teams/{team_id}/roster",
                {"rosterType": "fullSeason", "date": date_str},
            )
            for pid, st in self.parse_roster(full).items():
                # Add only an IL arm the active read lacks. Never override an
                # arm the active read already holds.
                if st.on_il and pid not in statuses:
                    statuses[pid] = st
        except Exception as exc:  # noqa: BLE001
            log.debug("SIM-433: full-season roster fetch skipped (team %s): %s", team_id, exc)
        return statuses

    # --------------------------------------------------------- decision logic
    def decide(
        self,
        *,
        status: PitcherStatus | None,
        pitches_last_3d: int,
        back_to_back: bool,
    ) -> tuple[bool, str]:
        """Return ``(available, reason)`` for one pitcher.

        The first rule that holds wins: off the active roster or on the IL
        (``IL``), pitched the day before (``rest``), a heavy 3-day workload
        (``recent_use``), else ``active``.
        """
        if status is None or not status.on_active_roster or status.on_il:
            return False, "IL"
        if back_to_back:
            return False, "rest"
        if pitches_last_3d >= self.rest_pitches_3d:
            return False, "recent_use"
        return True, "active"

    def build_rows(
        self,
        game_pk: int,
        workload: Iterable[Mapping[str, Any]],
        roster_by_team: Mapping[int, Mapping[int, PitcherStatus]],
        timeline: Mapping[int, Iterable[tuple[date, int]]] | None = None,
        game_date: date | None = None,
    ) -> list[AvailabilityRow]:
        """One availability row per pitcher on each team's roster, used or not.

        ``workload`` holds the :func:`compute_bullpen_workload` rows of the arms that
        pitched in this game. ``roster_by_team`` maps a team id to its staff from
        :meth:`_fetch_team_pitchers`. ``timeline`` maps a pitcher id to his
        ``(date, pitches)`` appearances.

        An arm's rest comes from his workload row when he pitched in the game;
        else from ``timeline`` as of ``game_date`` (:func:`_rest_as_of`); else
        he reads fully rested. An arm who pitched but is missing from the roster
        read still gets a row (``source='workload'``): a gap in the roster read
        must not erase a real arm.
        """
        wl: dict[tuple[int, int], Mapping[str, Any]] = {
            (int(r["team_id"]), int(r["pitcher_id"])): r for r in workload
        }
        rows: list[AvailabilityRow] = []
        emitted: set[tuple[int, int]] = set()

        def _rest_for(team_id: int, pid: int) -> tuple[int | None, int, bool]:
            rec = wl.get((team_id, pid))
            if rec is not None:  # he pitched: his workload row is the truth
                return (
                    _opt_int(rec.get("days_rest")),
                    int(rec.get("pitches_last_3d") or 0),
                    bool(rec.get("back_to_back")),
                )
            if timeline is not None:
                return _rest_as_of(timeline.get(pid, ()), game_date)
            return None, 0, False

        # 1) Every arm on each team's roster.
        for team_id, roster in roster_by_team.items():
            tid = int(team_id)
            for pid, status in roster.items():
                pid = int(pid)
                emitted.add((tid, pid))
                days_rest, pitches_last_3d, back_to_back = _rest_for(tid, pid)
                available, reason = self.decide(
                    status=status,
                    pitches_last_3d=pitches_last_3d,
                    back_to_back=back_to_back,
                )
                rows.append(
                    AvailabilityRow(
                        game_pk=game_pk,
                        team_id=tid,
                        pitcher_id=pid,
                        available=available,
                        reason=reason,
                        days_rest=days_rest,
                        pitches_last_3d=pitches_last_3d,
                        back_to_back=back_to_back,
                        source="mlb_api",
                    )
                )

        # 2) The arms who pitched but are missing from the roster read. They did
        #    pitch, so they count as on the active roster; their rest still applies.
        for (tid, pid), rec in wl.items():
            if (tid, pid) in emitted:
                continue
            days_rest = _opt_int(rec.get("days_rest"))
            pitches_last_3d = int(rec.get("pitches_last_3d") or 0)
            back_to_back = bool(rec.get("back_to_back"))
            status = PitcherStatus(pitcher_id=pid, on_active_roster=True, on_il=False)
            available, reason = self.decide(
                status=status,
                pitches_last_3d=pitches_last_3d,
                back_to_back=back_to_back,
            )
            rows.append(
                AvailabilityRow(
                    game_pk=game_pk,
                    team_id=tid,
                    pitcher_id=pid,
                    available=available,
                    reason=reason,
                    days_rest=days_rest,
                    pitches_last_3d=pitches_last_3d,
                    back_to_back=back_to_back,
                    source="workload",
                )
            )
        return rows

    def ingest_game(
        self,
        game_pk: int,
        workload: Iterable[Mapping[str, Any]],
        timeline: Mapping[int, Iterable[tuple[date, int]]] | None = None,
    ) -> list[AvailabilityRow]:
        """Build and store the availability rows of one game. Returns the rows.

        The method reads the rosters of both teams of the schedule, not only of
        the teams that pitched. ``timeline`` maps a pitcher id to his
        ``(date, pitches)`` appearances. A game with no workload rows, or whose
        schedule entry does not resolve, writes nothing.
        """
        records = list(workload)
        if not records:
            return []
        meta = self._resolve_game_meta(game_pk)
        if meta is None:
            log.warning("SIM-433: skipping game_pk %s — unresolved meta.", game_pk)
            return []
        # Both teams of the schedule, plus any workload team as a guard.
        team_ids = {meta.home_team_id, meta.away_team_id} | {int(r["team_id"]) for r in records}
        roster_by_team: dict[int, dict[int, PitcherStatus]] = {}
        for team_id in team_ids:
            roster_by_team[team_id] = self._fetch_team_pitchers(team_id, meta.date)
        rows = self.build_rows(
            game_pk, records, roster_by_team, timeline=timeline, game_date=_as_date(meta.date)
        )
        self._persist(rows)
        return rows

    def ingest(self, workload_records: Iterable[Mapping[str, Any]]) -> int:
        """Build and store the availability of every game in ``workload_records``.

        ``workload_records`` is the whole :func:`compute_bullpen_workload` output,
        as records. The method builds each pitcher's appearance timeline once
        from them, so each game can derive the rest of the arms that did not
        pitch. A failed game logs a warning, and the run goes on. Returns the
        rows stored.
        """
        records = list(workload_records)
        timeline: dict[int, list[tuple[date, int]]] = {}
        by_game: dict[int, list[Mapping[str, Any]]] = {}
        for rec in records:
            pid = int(rec["pitcher_id"])
            d = _as_date(rec.get("game_date"))
            if d is not None:
                timeline.setdefault(pid, []).append((d, int(rec.get("pitches_in_game") or 0)))
            by_game.setdefault(int(rec["game_pk"]), []).append(rec)
        total = 0
        for game_pk, recs in by_game.items():
            try:
                total += len(self.ingest_game(game_pk, recs, timeline=timeline))
            except Exception as exc:  # noqa: BLE001
                log.warning("SIM-433: ingest failed for game_pk %s: %s", game_pk, exc)
        log.info("SIM-433: persisted %d availability rows across %d games.", total, len(by_game))
        return total

    # -------------------------------------------------------------- persistence
    def _persist(self, rows: list[AvailabilityRow]) -> None:
        """Upsert the rows into ``raw.game_bullpen_availability``.

        The tests override this database seam. A re-run refreshes a row.
        Raises ``RuntimeError`` when ``pg_dsn`` is unset.
        """
        if not rows:
            return
        if not self.pg_dsn:
            raise RuntimeError(
                "BullpenAvailabilityIngest._persist needs a pg_dsn — set BASEBALL_DB_DSN "
                "or override _persist (tests do)."
            )
        import psycopg2
        from psycopg2.extras import execute_values

        sql = """
            INSERT INTO raw.game_bullpen_availability (
                game_pk, team_id, pitcher_id, available, reason,
                days_rest, pitches_last_3d, back_to_back, is_starter, source
            ) VALUES %s
            ON CONFLICT (game_pk, team_id, pitcher_id) DO UPDATE SET
                available       = EXCLUDED.available,
                reason          = EXCLUDED.reason,
                days_rest       = EXCLUDED.days_rest,
                pitches_last_3d = EXCLUDED.pitches_last_3d,
                back_to_back    = EXCLUDED.back_to_back,
                is_starter      = EXCLUDED.is_starter,
                source          = EXCLUDED.source,
                updated_at      = NOW()
        """
        conn = psycopg2.connect(self.pg_dsn)
        try:
            with conn, conn.cursor() as cur:
                execute_values(cur, sql, [r.as_tuple() for r in rows])
        finally:
            conn.close()


def _opt_int(value: Any) -> int | None:
    """``value`` as an int; None when it is None, a pandas NaN or not a number."""
    if value is None:
        return None
    try:
        if isinstance(value, float) and value != value:  # noqa: PLR0124 — NaN check
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_date(value: Any) -> date | None:
    """A workload or schedule date as a plain :class:`datetime.date`, or None.

    It takes a 'YYYY-MM-DD' string, a datetime, a pandas Timestamp or a date.
    """
    if value is None:
        return None
    # Before the date check: a datetime (and a pandas Timestamp) is also a date.
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _rest_as_of(
    appearances: Iterable[tuple[date, int]], game_date: date | None
) -> tuple[int | None, int, bool]:
    """An arm's ``(days_rest, pitches_last_3d, back_to_back)`` as of ``game_date``.

    The values come from his appearance timeline and match what
    :func:`compute_bullpen_workload` gives an arm who pitched: the days since the
    last appearance, and the pitches in the 3 calendar days before the game
    (the game excluded). An arm with no earlier appearance (a call-up, say)
    returns ``(None, 0, False)``: fully rested.
    """
    if game_date is None:
        return None, 0, False
    prior = sorted((d, int(p)) for (d, p) in appearances if d is not None and d < game_date)
    if not prior:
        return None, 0, False
    last_date = prior[-1][0]
    days_rest = (game_date - last_date).days
    back_to_back = days_rest == 1
    pitches_last_3d = sum(p for (d, p) in prior if 1 <= (game_date - d).days <= 3)
    return days_rest, int(pitches_last_3d), back_to_back


def compute_bullpen_workload(conn: duckdb.DuckDBPyConnection, seasons: list[int]) -> pd.DataFrame:
    """
    Derive the per-game rest and recent workload the availability table
    (``raw.game_bullpen_availability``) needs, straight from ``raw.pitches``
    (no network).

    ``raw.game_lineups`` records who *played*; the manager profile's USAGE
    sub-score also needs the available-but-unused signal, which
    starts with workload: an arm that threw 45 pitches yesterday is
    rest-unavailable today regardless of whether the manager "chose" not to
    use it. The function builds, per (game_pk, team_id, pitcher_id) where the
    pitcher actually appeared:

      * ``last_game_date``   — this pitcher's appearance date (the game's date)
      * ``days_rest``        — days since the pitcher's PREVIOUS appearance
                               (NULL for the first appearance in the window)
      * ``pitches_last_3d``  — pitches thrown in the 3 calendar days BEFORE
                               this game's date (excludes this game)
      * ``back_to_back``     — pitched on the immediately preceding calendar
                               day

    ``team_id`` is the FIELDING team for the pitcher in each appearance: the
    home team when the half-inning is the top (``inning_topbot='Top'``), else
    the away team. (A pitcher only ever fields for one team in a game, so the
    per-(game, pitcher) MIN over the half-inning derivation is unambiguous.)

    This is the raw.pitches half of the bullpen availability. It does NOT
    persist: ``conn`` attaches PostgreSQL READ_ONLY (as ``pg``), so it returns
    the workload rows as a DataFrame for :class:`BullpenAvailabilityIngest`
    to merge with the MLB-API active-roster / IL fetch and persist to
    ``raw.game_bullpen_availability``. Returns an EMPTY DataFrame (with the
    documented columns) when there are no qualifying rows.
    """
    log.info("Deriving bullpen workload (SIM-433) …")
    season_list = ", ".join(str(s) for s in seasons)

    # One row per (game_pk, pitcher) appearance with that game's date, the
    # pitcher's fielding team, and pitches thrown; then a self-referential
    # window over the pitcher's appearance timeline derives rest/workload.
    df = conn.execute(f"""
        WITH appearances AS (
            SELECT
                game_pk,
                pitcher                                                  AS pitcher_id,
                season,
                MIN(game_date)                                           AS game_date,
                -- Fielding team: home fields the top half, away the bottom.
                MIN(CASE WHEN inning_topbot = 'Top' THEN home_id ELSE away_id END)
                                                                         AS team_id,
                COUNT(*)                                                 AS pitches_in_game
            FROM pg.raw.pitches
            WHERE data_quality_flag = FALSE
              AND season IN ({season_list})
            GROUP BY game_pk, pitcher, season
        ),
        with_prev AS (
            SELECT
                a.*,
                LAG(a.game_date) OVER (
                    PARTITION BY a.pitcher_id ORDER BY a.game_date, a.game_pk
                )                                                        AS prev_game_date
            FROM appearances a
        )
        SELECT
            w.game_pk,
            w.team_id,
            w.pitcher_id,
            w.season,
            w.game_date,
            w.pitches_in_game,
            CASE WHEN w.prev_game_date IS NOT NULL
                 THEN DATE_DIFF('day', w.prev_game_date, w.game_date)
            END                                                          AS days_rest,
            CASE WHEN w.prev_game_date IS NOT NULL
                   AND DATE_DIFF('day', w.prev_game_date, w.game_date) = 1
                 THEN TRUE ELSE FALSE END                                AS back_to_back,
            -- Pitches thrown by this arm in the 3 calendar days BEFORE this
            -- game (a correlated sum over the same appearances set).
            COALESCE((
                SELECT SUM(p.pitches_in_game)
                FROM appearances p
                WHERE p.pitcher_id = w.pitcher_id
                  AND p.game_date < w.game_date
                  AND p.game_date >= w.game_date - INTERVAL 3 DAY
            ), 0)                                                        AS pitches_last_3d
        FROM with_prev w
        ORDER BY w.pitcher_id, w.game_date, w.game_pk
    """).fetchdf()

    if df.empty:
        log.info("  bullpen workload: no qualifying appearances.")
        return df
    log.info(
        "  bullpen workload: %d appearance rows (%d pitchers).",
        len(df),
        df["pitcher_id"].nunique(),
    )
    return df


def _load_workload_records(
    duckdb_path: str, pg_dsn: str, seasons: list[int]
) -> list[dict[str, Any]]:
    """The bullpen workload rows of ``seasons``, as records.

    The function opens a ``PlayerProfileComputor`` for its DuckDB connection
    (which attaches Postgres read-only) and runs :func:`compute_bullpen_workload`
    on it.
    """
    from profiles.build_profiles import PlayerProfileComputor

    computor = PlayerProfileComputor(pg_dsn=pg_dsn, duckdb_path=duckdb_path)
    computor._connect()
    try:
        df = compute_bullpen_workload(computor._conn, seasons)
    finally:
        computor._close()
    return df.to_dict("records") if df is not None and not df.empty else []


def main(argv: list[str] | None = None) -> int:
    """The command-line entry point. Returns the exit code."""
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="SIM-433 bullpen availability ingest")
    ap.add_argument("--seasons", nargs="+", type=int, required=True)
    ap.add_argument("--dsn", default="", help="Postgres DSN (default: BASEBALL_DB_DSN).")
    ap.add_argument("--duckdb", required=True, help="Path to the DuckDB profile file")
    ap.add_argument("--rest-pitches-3d", type=int, default=REST_PITCHES_3D)
    args = ap.parse_args(argv)
    args.dsn = args.dsn or require_dsn()

    records = _load_workload_records(args.duckdb, args.dsn, args.seasons)
    ingest = BullpenAvailabilityIngest(pg_dsn=args.dsn, rest_pitches_3d=args.rest_pitches_3d)
    ingest.ingest(records)
    return 0


__all__ = [
    "AvailabilityRow",
    "BullpenAvailabilityIngest",
    "PitcherStatus",
    "REST_PITCHES_3D",
    "compute_bullpen_workload",
]


if __name__ == "__main__":  # pragma: no cover — CLI
    raise SystemExit(main())
