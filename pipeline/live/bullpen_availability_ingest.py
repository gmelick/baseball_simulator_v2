"""
pipeline/live/bullpen_availability_ingest.py
============================================
SIM-433 — per-game bullpen availability ingestion.

``raw.game_lineups`` records who *played*, not who was *available*. The manager
USAGE profile (SIM-427 / SIM-434) needs the available-but-unused signal: was a
reliever NOT used because the manager chose to hold him, or because he *couldn't*
pitch (on the IL, or rest-unavailable after a heavy / back-to-back outing)?

This module produces ``raw.game_bullpen_availability`` rows (migration 0015) by
combining two sources:

  * **rest / recent workload** (days_rest, pitches_last_3d, back_to_back) —
    derived from ``raw.pitches`` by
    :meth:`pipeline.batch.player_profile_computor.PlayerProfileComputor._compute_bullpen_workload`
    and passed in here as a DataFrame (no network);
  * **active roster + IL** — fetched per (team, game-date) from the public MLB
    Stats API (statsapi.mlb.com) active-roster endpoint, whose entries carry a
    ``status.code`` (``'A'`` = active; ``'D10'``/``'D15'``/``'D60'``/``'IL*'`` =
    injured list; ``'RM'``/``'DES'`` etc. = otherwise off the active roster).

The availability decision per pitcher:

  * not on the active roster (or status indicates IL)  → available=False, IL
  * threw on the immediately preceding calendar day    → available=False, rest
    (a closer/setup arm that pitched back-to-back is conventionally down)
  * threw a heavy workload in the last 3 days
    (``pitches_last_3d`` ≥ :data:`REST_PITCHES_3D`)     → available=False,
    recent_use
  * otherwise                                           → available=True, active

Network surface is a SINGLE stubbable seam, ``_mlb_get`` (mirrors
:mod:`pipeline.bettingpros_odds_provider`); ``game_pk`` → date + (home,away)
team ids is resolved via the MLB schedule endpoint, also through ``_mlb_get``.
Persistence is a single stubbable seam, :meth:`_persist`, so the parse + decision
logic is unit-tested against a captured roster JSON fixture with NO live call and
NO live DB.

Run (data-gated — network + ~21k games, slow):

    python -m pipeline.live.bullpen_availability_ingest --seasons 2024 \\
        --dsn "$BASEBALL_DB_DSN" --duckdb /data/baseball_sim.duckdb

HTTP is stdlib ``urllib`` (sync); persistence is ``psycopg2`` (sync) — both are
existing dependencies and keep the module importable without a running app.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("pipeline.live.bullpen_availability_ingest")

_MLB_BASE = "https://statsapi.mlb.com/api/v1"

#: pitches in the last 3 calendar days at/above which a reliever is treated as
#: rest-unavailable ('recent_use'). Conservative; tune against the manager-profile
#: validation once SIM-427/434 reads this signal.
REST_PITCHES_3D = 45

#: MLB roster ``status.code`` values that mean "on the active roster / available
#: to play". Everything else (IL codes 'D10'/'D15'/'D60'/'IL*', 'RM' removed,
#: 'DES' designated, 'SU' suspended, ...) is treated as not-available.
_ACTIVE_STATUS_CODES = frozenset({"A"})

#: roster ``status.code`` literal prefix for IL codes that are NOT digit-suffixed.
_IL_STATUS_PREFIXES = ("IL",)  # e.g. 'IL10' / 'IL60'

#: pitcher position codes (MLB ``position.code``: '1' = pitcher). Only pitchers
#: are written to the bullpen-availability table.
_PITCHER_POSITION_CODES = frozenset({"1"})


@dataclass
class PitcherStatus:
    """One pitcher's MLB-roster status for a (team, date)."""

    pitcher_id: int
    on_active_roster: bool
    on_il: bool
    is_pitcher: bool = True


@dataclass
class AvailabilityRow:
    """A resolved per-game availability decision for one pitcher."""

    game_pk: int
    team_id: int
    pitcher_id: int
    available: bool
    reason: str  # 'active' | 'IL' | 'rest' | 'recent_use'
    days_rest: int | None
    pitches_last_3d: int
    back_to_back: bool
    is_starter: bool = False
    source: str = "mlb_api"

    def as_tuple(self) -> tuple[Any, ...]:
        """Positional tuple matching the INSERT column order in :meth:`_persist`."""
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
    date: str  # 'YYYY-MM-DD'
    home_team_id: int
    away_team_id: int


# Columns the workload DataFrame (from _compute_bullpen_workload) must carry.
_WORKLOAD_COLS = (
    "game_pk",
    "team_id",
    "pitcher_id",
    "days_rest",
    "pitches_last_3d",
    "back_to_back",
)


@dataclass
class BullpenAvailabilityIngest:
    """Fetch + parse + persist per-game bullpen availability (SIM-433).

    Parameters
    ----------
    pg_dsn:
        PostgreSQL DSN (``BASEBALL_DB_DSN``) used by the default :meth:`_persist`.
        May be omitted in tests that override ``_persist``.
    rest_pitches_3d:
        ``pitches_last_3d`` threshold for the 'recent_use' rest block.
    timeout:
        HTTP timeout (seconds) for the MLB Stats API calls.
    """

    pg_dsn: str | None = None
    rest_pitches_3d: int = REST_PITCHES_3D
    timeout: float = 15.0
    _meta_cache: dict[int, _GameMeta | None] = field(default_factory=dict, repr=False)

    # ----------------------------------------------------------------- network
    def _http_get_json(self, url: str) -> dict[str, Any]:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 — fixed host
            return json.loads(resp.read().decode("utf-8"))

    def _mlb_get(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """GET an MLB Stats API endpoint (the ONLY network seam — stubbed in tests)."""
        qs = urllib.parse.urlencode(dict(params))
        url = f"{_MLB_BASE}/{path}?{qs}" if qs else f"{_MLB_BASE}/{path}"
        return self._http_get_json(url)

    # ----------------------------------------------------- identifier bridges
    def _resolve_game_meta(self, game_pk: int) -> _GameMeta | None:
        """``game_pk`` → (date, home_team_id, away_team_id) via the MLB schedule."""
        if game_pk in self._meta_cache:
            return self._meta_cache[game_pk]
        meta: _GameMeta | None = None
        try:
            data = self._mlb_get("schedule", {"sportId": 1, "gamePk": game_pk})
            game = data["dates"][0]["games"][0]
            meta = _GameMeta(
                date=str(game["gameDate"])[:10],
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
        """True if a roster ``status.code`` denotes the injured list.

        The legacy disabled-list / injured-list codes are a 'D' followed by the
        number of days ('D10' / 'D15' / 'D60'); newer feeds also use an 'IL'
        prefix ('IL10' / 'IL60'). Crucially, 'DES' (designated for assignment),
        'DEC', etc. are NOT the IL despite the leading 'D', so the 'D' match
        requires a trailing digit.
        """
        c = (code or "").upper()
        if c.startswith("D") and c[1:2].isdigit():
            return True
        return any(c.startswith(p) for p in _IL_STATUS_PREFIXES)

    @classmethod
    def parse_roster(cls, payload: Mapping[str, Any]) -> dict[int, PitcherStatus]:
        """Parse an MLB active-roster payload → {pitcher_id: PitcherStatus}.

        Reads the ``roster`` list; keeps only pitchers (``position.code`` '1').
        ``status.code`` == 'A' → on the active roster; an IL-prefixed code → on
        the IL. Robust to missing ``position`` / ``status`` blocks (defaults to
        not-a-pitcher / not-active so an oddly-shaped entry can't masquerade as
        an available arm).
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
        """Fetch + parse a team's pitching staff status for a date (network seam).

        Uses ``rosterType=active`` for the active 26-man and merges
        ``rosterType=fullSeason`` IL entries so injured arms (which drop off the
        ACTIVE roster) are still recorded as ``on_il=True`` rather than vanishing.
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
                # Only ADD info the active fetch lacked: an IL arm not on the
                # active roster. Never downgrade an arm the active fetch marked up.
                if st.on_il and pid not in statuses:
                    statuses[pid] = st
        except Exception as exc:  # noqa: BLE001
            log.debug("SIM-433: full-season roster fetch skipped (team %s): %s", team_id, exc)
        return statuses

    # --------------------------------------------------------- decision logic
    def decide(
        self,
        *,
        pitcher_id: int,
        status: PitcherStatus | None,
        days_rest: int | None,
        pitches_last_3d: int,
        back_to_back: bool,
    ) -> tuple[bool, str]:
        """Resolve (available, reason) from roster status + workload.

        Precedence: IL/off-roster → rest (back-to-back) → recent_use (heavy 3-day
        workload) → active.
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
    ) -> list[AvailabilityRow]:
        """Merge per-(team,pitcher) workload with roster status → availability rows.

        ``workload`` is an iterable of mappings carrying at least
        ``team_id``/``pitcher_id``/``days_rest``/``pitches_last_3d``/``back_to_back``
        (the per-game slice of the ``_compute_bullpen_workload`` DataFrame, e.g.
        ``df.to_dict('records')``). ``roster_by_team`` maps team_id → the
        {pitcher_id: PitcherStatus} from :meth:`_fetch_team_pitchers`.

        An arm that pitched in this game (present in ``workload``) but is missing
        from its team's roster fetch is still emitted as available='active'
        (it demonstrably pitched), so a roster-API gap can't erase a real arm.
        """
        rows: list[AvailabilityRow] = []
        for rec in workload:
            team_id = int(rec["team_id"])
            pitcher_id = int(rec["pitcher_id"])
            status = roster_by_team.get(team_id, {}).get(pitcher_id)
            days_rest = _opt_int(rec.get("days_rest"))
            pitches_last_3d = int(rec.get("pitches_last_3d") or 0)
            back_to_back = bool(rec.get("back_to_back"))
            # If the arm pitched in THIS game we know it was available; trust that
            # over an absent/active-roster-lagging fetch, but still honour an
            # explicit IL flag (a same-day activation edge case is rare).
            if status is None:
                status = PitcherStatus(pitcher_id=pitcher_id, on_active_roster=True, on_il=False)
            available, reason = self.decide(
                pitcher_id=pitcher_id,
                status=status,
                days_rest=days_rest,
                pitches_last_3d=pitches_last_3d,
                back_to_back=back_to_back,
            )
            rows.append(
                AvailabilityRow(
                    game_pk=game_pk,
                    team_id=team_id,
                    pitcher_id=pitcher_id,
                    available=available,
                    reason=reason,
                    days_rest=days_rest,
                    pitches_last_3d=pitches_last_3d,
                    back_to_back=back_to_back,
                )
            )
        return rows

    def ingest_game(
        self, game_pk: int, workload: Iterable[Mapping[str, Any]]
    ) -> list[AvailabilityRow]:
        """Resolve + persist availability for one game; returns the rows written.

        Fetches the active-roster/IL status for each team that has workload rows
        in this game, builds the availability rows, and persists them. The two
        teams are taken from the workload's ``team_id`` values (falling back to
        the schedule's home/away if needed).
        """
        records = list(workload)
        if not records:
            return []
        meta = self._resolve_game_meta(game_pk)
        if meta is None:
            log.warning("SIM-433: skipping game_pk %s — unresolved meta.", game_pk)
            return []
        team_ids = {int(r["team_id"]) for r in records}
        roster_by_team: dict[int, dict[int, PitcherStatus]] = {}
        for team_id in team_ids:
            roster_by_team[team_id] = self._fetch_team_pitchers(team_id, meta.date)
        rows = self.build_rows(game_pk, records, roster_by_team)
        self._persist(rows)
        return rows

    def ingest(self, workload_records: Iterable[Mapping[str, Any]]) -> int:
        """Ingest availability for every game present in ``workload_records``.

        ``workload_records`` is the full ``_compute_bullpen_workload`` output
        (``df.to_dict('records')``). Returns the total rows persisted. Per-game
        failures are logged and skipped (never abort the whole run).
        """
        by_game: dict[int, list[Mapping[str, Any]]] = {}
        for rec in workload_records:
            by_game.setdefault(int(rec["game_pk"]), []).append(rec)
        total = 0
        for game_pk, recs in by_game.items():
            try:
                total += len(self.ingest_game(game_pk, recs))
            except Exception as exc:  # noqa: BLE001
                log.warning("SIM-433: ingest failed for game_pk %s: %s", game_pk, exc)
        log.info("SIM-433: persisted %d availability rows across %d games.", total, len(by_game))
        return total

    # -------------------------------------------------------------- persistence
    def _persist(self, rows: list[AvailabilityRow]) -> None:
        """UPSERT availability rows into raw.game_bullpen_availability (DB seam).

        Overridden in unit tests to capture rows without a live DB. The live
        implementation uses psycopg2 + ``execute_values`` with an ON CONFLICT
        (game_pk, team_id, pitcher_id) DO UPDATE so re-runs are idempotent.
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
    if value is None:
        return None
    try:
        # NaN (pandas) → None; everything else → int.
        if isinstance(value, float) and value != value:  # noqa: PLR0124 — NaN check
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_workload_records(
    duckdb_path: str, pg_dsn: str, seasons: list[int]
) -> list[dict[str, Any]]:
    """Run the raw.pitches-derived workload query and return its records.

    Constructs a :class:`PlayerProfileComputor`, opens its DuckDB connection
    (which attaches Postgres READ_ONLY), runs ``_compute_bullpen_workload`` only,
    and returns ``df.to_dict('records')``. Kept here (not in the computor) so the
    CLI run path is self-contained.
    """
    from pipeline.batch.player_profile_computor import PlayerProfileComputor

    computor = PlayerProfileComputor(pg_dsn=pg_dsn, duckdb_path=duckdb_path)
    computor._connect()
    try:
        df = computor._compute_bullpen_workload(seasons)
    finally:
        computor._close()
    return df.to_dict("records") if df is not None and not df.empty else []


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint (data-gated — network + a full season of games)."""
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="SIM-433 bullpen availability ingest")
    ap.add_argument("--seasons", nargs="+", type=int, required=True)
    ap.add_argument("--dsn", default=os.environ.get("BASEBALL_DB_DSN"))
    ap.add_argument("--duckdb", required=True, help="Path to the DuckDB profile file")
    ap.add_argument("--rest-pitches-3d", type=int, default=REST_PITCHES_3D)
    args = ap.parse_args(argv)
    if not args.dsn:
        ap.error("--dsn or BASEBALL_DB_DSN is required")

    records = _load_workload_records(args.duckdb, args.dsn, args.seasons)
    ingest = BullpenAvailabilityIngest(pg_dsn=args.dsn, rest_pitches_3d=args.rest_pitches_3d)
    ingest.ingest(records)
    return 0


__all__ = [
    "AvailabilityRow",
    "BullpenAvailabilityIngest",
    "PitcherStatus",
    "REST_PITCHES_3D",
]


if __name__ == "__main__":  # pragma: no cover — CLI
    raise SystemExit(main())
