"""The loader's writes: the pitch rows, the play events, the error ledger, the
ingest outcome and the freshness report.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg2
import psycopg2.extras

from ingest.games.base import LoaderBase
from ingest.games.pitch_rows import (
    _build_row_dict,
    _validate_row,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BATCH_SIZE = 500  # rows per executemany call


class ReloadShrinkError(RuntimeError):
    """A reload would have replaced a game's pitch rows with strictly fewer rows.

    Raised inside :meth:`HistoricalDataLoader._batch_insert` BEFORE the commit, so
    the DELETE is rolled back and the existing rows survive.  The usual cause is a
    transient feed returning HTTP 200 with an empty or pitch-less ``allPlays``;
    re-running the reload once the feed is healthy is the fix.  Pass
    ``allow_shrink=True`` when a game genuinely has fewer pitches than the stored
    copy (e.g. correcting a previously double-ingested game).
    """


class WritesMixin(LoaderBase):
    """Writes a game's rows and the run's records."""

    def _game_already_loaded(self, game_pk: int) -> bool:
        """True if this game has been ingested and should be skipped.

        A pitch row OR a terminal ``raw.etl_game_ingest`` record counts as
        loaded. The ingest record covers a game that legitimately produced no
        pitch rows (every row hard-errors, or the feed has no pitches); without
        it such a game would be re-fetched every night, forever.

        Deliberately NOT consulted by the reload path: `reload=True` bypasses
        this method entirely, which is the escape hatch after a parser fix.
        """
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM raw.pitches WHERE game_pk = %s LIMIT 1", (game_pk,))
            if cur.fetchone() is not None:
                return True
            cur.execute(
                """
                SELECT 1 FROM raw.etl_game_ingest
                 WHERE game_pk = %s AND outcome IN ('loaded', 'empty')
                 LIMIT 1
                """,
                (game_pk,),
            )
            return cur.fetchone() is not None

    def _process_and_insert(
        self,
        game_pk: int,
        season: int,
        raw_rows: list[dict[str, Any]],
        *,
        replace: bool = False,
        allow_shrink: bool = False,
        play_event_rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, int]:
        """
        Validates, builds, and batch-inserts all pitch rows for a game.
        Returns count summary.

        ``replace=True`` (used by :meth:`reload_game`) deletes the game's
        existing ``raw.pitches`` rows in the same transaction as the insert, so
        corrected parser output actually overwrites the old values instead of
        being discarded by ``ON CONFLICT … DO NOTHING``.

        Hard-error rows are written to raw.etl_errors as well as logged, so
        a skipped row has an audit trail and a re-ingest knows which pitches
        to recover.
        """
        to_insert: list[dict[str, Any]] = []
        hard_errors: list[tuple[int, int, list[str]]] = []  # (at_bat, pitch, errors)
        flagged_count = 0

        for raw_row in raw_rows:
            row = _build_row_dict(raw_row, game_pk, season)
            vr = _validate_row(row)

            if not vr.is_valid:
                hard_errors.append(
                    (row.get("at_bat_number"), row.get("pitch_number"), vr.hard_errors)
                )
                continue

            if vr.warnings:
                flagged_count += 1
                for w in vr.warnings:
                    log.warning(
                        "  game=%s ab=%s pitch=%s — %s",
                        game_pk,
                        row.get("at_bat_number"),
                        row.get("pitch_number"),
                        w,
                    )

            to_insert.append(row)

        if hard_errors:
            log.error(
                "  game %s: %d rows skipped due to hard validation errors:",
                game_pk,
                len(hard_errors),
            )
            for ab, pitch_num, errs in hard_errors:
                log.error("    ab=%s pitch_number=%s: %s", ab, pitch_num, "; ".join(errs))

        # The ledger is written INSIDE the pitch transaction (see
        # _batch_insert), so a pitch insert that rolls back takes its error rows
        # with it: no ledger row describes a game that was not written.
        inserted = self._batch_insert(
            to_insert,
            replace_game_pk=game_pk if replace else None,
            allow_shrink=allow_shrink,
            error_rows=hard_errors,
            season=season,
            game_pk=game_pk,
            play_event_rows=play_event_rows,
        )
        self._log_freshness(game_pk, to_insert)

        return {
            "inserted": inserted,
            "skipped": len(hard_errors),
            "flagged": flagged_count,
        }

    @staticmethod
    def _write_error_ledger(
        cur,
        game_pk: int,
        errors: list[tuple[int | None, int | None, list[str]]],
    ) -> None:
        """One raw.etl_errors row per skipped pitch, idempotently.

        Runs on the CALLER's cursor so it shares the pitch-insert transaction.

        The insert is now ``ON CONFLICT DO UPDATE`` against the natural key
        (migration 0017). Without it, a game whose rows ALL hard-error writes no
        pitch rows, so ``_game_already_loaded`` stayed False, so the nightly job
        re-fetched and re-processed that game every night and appended a full
        duplicate error set each time — roughly 54,000 identical rows per season
        for one broken game, and any consumer sizing a recovery from
        ``COUNT(*)`` was wrong by the number of nights elapsed.
        """
        if not errors:
            return
        sql = """
            INSERT INTO raw.etl_errors
                (game_pk, at_bat_number, pitch_number, error_type, error_messages)
            VALUES (%s, %s, %s, 'HARD', %s)
            ON CONFLICT (game_pk, at_bat_number, pitch_number, error_type) DO UPDATE SET
                error_messages = EXCLUDED.error_messages,
                created_at     = NOW()
        """
        psycopg2.extras.execute_batch(
            cur,
            sql,
            [(game_pk, ab, pitch_num, list(msgs)) for (ab, pitch_num, msgs) in errors],
        )

    @staticmethod
    def _write_ingest_outcome(
        cur,
        game_pk: int,
        season: int,
        *,
        pitch_rows: int,
        skipped_rows: int,
    ) -> None:
        """Record the terminal per-game ETL outcome.

        ``_game_already_loaded`` keys on pitch rows existing, which cannot
        distinguish "never loaded" from "loaded and legitimately produced zero
        pitch rows" (every row hard-errored, or the feed genuinely has no
        pitches). Without this record such a game is re-fetched and re-processed
        every single night, forever, with nothing ever changing.

        ``outcome='empty'`` is what stops that. ``reload_game`` /
        ``reload=True`` ignore this table by design — that is the escape hatch
        once a parser fix makes the game loadable.
        """
        outcome = "loaded" if pitch_rows > 0 else "empty"
        cur.execute(
            """
            INSERT INTO raw.etl_game_ingest
                (game_pk, season, outcome, pitch_rows, skipped_rows)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (game_pk) DO UPDATE SET
                season       = EXCLUDED.season,
                outcome      = EXCLUDED.outcome,
                pitch_rows   = EXCLUDED.pitch_rows,
                skipped_rows = EXCLUDED.skipped_rows,
                attempts     = raw.etl_game_ingest.attempts + 1,
                last_error   = NULL,
                updated_at   = NOW()
            """,
            (game_pk, season, outcome, pitch_rows, skipped_rows),
        )

    #: Tri-state cache of "does `raw.play_events` exist" (``None`` means not
    #: yet probed; see ``LoaderBase._exists_once``).
    _play_events_table_exists: bool | None = None

    #: One pitch row.  ON CONFLICT DO NOTHING keeps an ordinary load
    #: idempotent; the reload path DELETEs the game first.
    INSERT_SQL = """
        INSERT INTO raw.pitches (
            game_pk, at_bat_number, pitch_number, game_date, season, venue_id, venue,
            home_id, home_team, away_id, away_team,
            home_manager_id, home_manager_name, away_manager_id, away_manager_name,
            inning, inning_topbot, pitcher, p_throws, batter, stand, bat_hand,
            home_score, away_score, bat_score, fld_score, balls, strikes, outs,
            type, description, pitch_type, pitch_name, events,
            sz_top, sz_bot, release_speed, end_speed,
            release_pos_x, release_pos_y, release_pos_z,
            vx0, vy0, vz0, ax, ay, az,
            pfx_x, pfx_z, plate_x, plate_z, x, y,
            release_spin_rate, spin_axis,
            break_angle, break_length, break_y,
            break_vertical, break_vertical_induced, break_horizontal,
            zone, release_extension,
            launch_speed, launch_angle, hit_distance_sc, hc_x, hc_y,
            spray_angle, bb_type, hit_location, des,
            on_1b, on_2b, on_3b, post_on_1b, post_on_2b, post_on_3b,
            fielder_2, fielder_3, fielder_4, fielder_5, fielder_6,
            fielder_7, fielder_8, fielder_9,
            runs_on_pitch, outs_on_pitch, rbis_on_pitch, earned_runs_on_pitch,
            runner_1b_scored, runner_2b_scored, runner_3b_scored,
            runner_1b_out_advancing, runner_2b_out_advancing, runner_3b_out_advancing,
            sb_attempt_2b, sb_attempt_3b, sb_attempt_home,
            sb_success_2b, sb_success_3b, sb_success_home,
            passed_ball_wild_pitch, pinch_hitter, pinch_runner,
            pitcher_sub, defensive_sub,
            fielded_by, fielding_error, dropped_ball, of_assist,
            field_assist_1, field_assist_2, field_assist_3, field_assist_4, field_assist_5,
            field_putout_1, field_putout_2, field_putout_3,
            throwing_error_1, throwing_error_2,
            field_assist_6_plus,
            data_quality_flag
        ) VALUES (
            %(game_pk)s, %(at_bat_number)s, %(pitch_number)s, %(game_date)s, %(season)s,
            %(venue_id)s, %(venue)s,
            %(home_id)s, %(home_team)s, %(away_id)s, %(away_team)s,
            %(home_manager_id)s, %(home_manager_name)s,
            %(away_manager_id)s, %(away_manager_name)s,
            %(inning)s, %(inning_topbot)s, %(pitcher)s, %(p_throws)s,
            %(batter)s, %(stand)s, %(bat_hand)s,
            %(home_score)s, %(away_score)s, %(bat_score)s, %(fld_score)s,
            %(balls)s, %(strikes)s, %(outs)s,
            %(type)s, %(description)s, %(pitch_type)s, %(pitch_name)s, %(events)s,
            %(sz_top)s, %(sz_bot)s, %(release_speed)s, %(end_speed)s,
            %(release_pos_x)s, %(release_pos_y)s, %(release_pos_z)s,
            %(vx0)s, %(vy0)s, %(vz0)s, %(ax)s, %(ay)s, %(az)s,
            %(pfx_x)s, %(pfx_z)s, %(plate_x)s, %(plate_z)s, %(x)s, %(y)s,
            %(release_spin_rate)s, %(spin_axis)s,
            %(break_angle)s, %(break_length)s, %(break_y)s,
            %(break_vertical)s, %(break_vertical_induced)s, %(break_horizontal)s,
            %(zone)s, %(release_extension)s,
            %(launch_speed)s, %(launch_angle)s, %(hit_distance_sc)s,
            %(hc_x)s, %(hc_y)s, %(spray_angle)s, %(bb_type)s,
            %(hit_location)s, %(des)s,
            %(on_1b)s, %(on_2b)s, %(on_3b)s,
            %(post_on_1b)s, %(post_on_2b)s, %(post_on_3b)s,
            %(fielder_2)s, %(fielder_3)s, %(fielder_4)s, %(fielder_5)s,
            %(fielder_6)s, %(fielder_7)s, %(fielder_8)s, %(fielder_9)s,
            %(runs_on_pitch)s, %(outs_on_pitch)s, %(rbis_on_pitch)s,
            %(earned_runs_on_pitch)s,
            %(runner_1b_scored)s, %(runner_2b_scored)s, %(runner_3b_scored)s,
            %(runner_1b_out_advancing)s, %(runner_2b_out_advancing)s, %(runner_3b_out_advancing)s,
            %(sb_attempt_2b)s, %(sb_attempt_3b)s, %(sb_attempt_home)s,
            %(sb_success_2b)s, %(sb_success_3b)s, %(sb_success_home)s,
            %(passed_ball_wild_pitch)s, %(pinch_hitter)s, %(pinch_runner)s,
            %(pitcher_sub)s, %(defensive_sub)s,
            %(fielded_by)s, %(fielding_error)s, %(dropped_ball)s, %(of_assist)s,
            %(field_assist_1)s, %(field_assist_2)s, %(field_assist_3)s,
            %(field_assist_4)s, %(field_assist_5)s,
            %(field_putout_1)s, %(field_putout_2)s, %(field_putout_3)s,
            %(throwing_error_1)s, %(throwing_error_2)s,
            %(field_assist_6_plus)s,
            %(data_quality_flag)s
        )
        ON CONFLICT (game_pk, at_bat_number, pitch_number) DO NOTHING;
    """

    #: Non-pitch play events.  ON CONFLICT DO NOTHING makes a partial
    #: re-run idempotent; the reload path DELETEs first so a genuine replace still
    #: replaces rather than silently keeping the old rows.
    PLAY_EVENTS_INSERT_SQL = """
        INSERT INTO raw.play_events (
            game_pk, at_bat_number, play_index, game_date, season, event_type,
            inning, inning_topbot, outs_before, runners_state, bat_score, fld_score,
            pitcher_id, batter_id, runner_id, base,
            is_out, out_number, fielder_putout, fielder_assist
        ) VALUES (
            %(game_pk)s, %(at_bat_number)s, %(play_index)s, %(game_date)s, %(season)s,
            %(event_type)s, %(inning)s, %(inning_topbot)s, %(outs_before)s,
            %(runners_state)s, %(bat_score)s, %(fld_score)s,
            %(pitcher_id)s, %(batter_id)s, %(runner_id)s, %(base)s,
            %(is_out)s, %(out_number)s, %(fielder_putout)s, %(fielder_assist)s
        )
        ON CONFLICT (game_pk, at_bat_number, play_index, event_type) DO NOTHING;
    """

    def _write_play_events(
        self,
        cur: Any,
        game_pk: int,
        rows: list[dict[str, Any]],
        *,
        replace: bool,
    ) -> int:
        """Write this game's non-pitch play events.  Returns rows written.

        Takes the CALLER's cursor so it joins the game's single write
        transaction: if the pitch insert rolls back, these roll back with it.
        Writing them separately would let a game keep play events whose pitches
        were never committed.

        DELETEs only when ``replace``, matching the pitch rows exactly. The
        incremental path must never delete — a reload REPLACES (so stale rows
        must go, in case a parser fix produces fewer of them), while an ordinary
        load relies on the natural-key ``ON CONFLICT DO NOTHING`` to stay
        idempotent.
        """
        # SKIP ENTIRELY when the table is absent.
        #
        # Alembic 0018 creates `raw.play_events`. On a database without it the
        # statements below would raise `UndefinedTable` and abort the whole game
        # transaction, so the probe skips them (it answers once per run).
        if not self._exists_once(
            cur,
            "_play_events_table_exists",
            "raw.play_events",
            absent_message=(
                "raw.play_events is absent (Alembic 0018 not applied) — "
                "SIM-502 play-event capture is INERT for this run"
            ),
            level=logging.INFO,
        ):
            return 0

        if replace:
            cur.execute("DELETE FROM raw.play_events WHERE game_pk = %s", (game_pk,))
        if not rows:
            return 0
        for offset in range(0, len(rows), BATCH_SIZE):
            psycopg2.extras.execute_batch(
                cur, self.PLAY_EVENTS_INSERT_SQL, rows[offset : offset + BATCH_SIZE]
            )
        return len(rows)

    def _batch_insert(
        self,
        rows: list[dict[str, Any]],
        *,
        replace_game_pk: int | None = None,
        allow_shrink: bool = False,
        error_rows: list[tuple[int | None, int | None, list[str]]] | None = None,
        season: int | None = None,
        game_pk: int | None = None,
        play_event_rows: list[dict[str, Any]] | None = None,
    ) -> int:
        """Insert rows in BATCH_SIZE chunks.  Returns rows ACTUALLY written.

        This is now the SINGLE transaction for one game's write — the
        optional DELETE, the pitch rows, the ``raw.etl_errors`` ledger and the
        ``raw.etl_game_ingest`` outcome all commit or roll back together."

        ``replace_game_pk``
            When supplied, every existing ``raw.pitches`` row for that game is
            DELETEd first, in the SAME transaction as the inserts.  This is the
            only supported way to correct already-ingested pitch data: the
            INSERT carries ``ON CONFLICT … DO NOTHING``, so without the delete a
            re-ingest silently writes nothing.  Delete + insert commit together
            or roll back together — a failed reload never leaves a game
            half-deleted.

        The return value is measured, not assumed.  ``execute_batch`` cannot
        report an accurate rowcount (``cur.rowcount`` reflects only the final
        page), and the previous implementation returned ``len(rows)`` — the
        number of rows *attempted*.  With ``DO NOTHING`` that number was
        indistinguishable from a completely discarded re-ingest, which is what
        made a corrective re-run look like a success while writing nothing.  We
        therefore bracket the insert with an exact COUNT on the affected game.
        """
        if not rows and replace_game_pk is None and not error_rows and season is None:
            return 0

        # Which game this batch belongs to. Prefer the pk the caller states
        # outright; inferring it from the rows is only a fallback for a caller
        # that does not pass one.
        #
        # When every row of a game hard-errors, ``rows`` is empty, so the
        # inference alone would give None and skip both the error ledger and
        # the ingest-outcome write below — for exactly the games that most need
        # an audit trail.
        game_pks = {r.get("game_pk") for r in rows}
        count_pk: int | None = replace_game_pk or game_pk
        if count_pk is None and len(game_pks) == 1:
            count_pk = next(iter(game_pks))

        count_sql = "SELECT COUNT(*) FROM raw.pitches WHERE game_pk = %s"
        deleted = 0

        with self._get_conn() as conn:
            try:
                with conn.cursor() as cur:
                    if replace_game_pk is not None:
                        cur.execute(
                            "DELETE FROM raw.pitches WHERE game_pk = %s",
                            (replace_game_pk,),
                        )
                        deleted = cur.rowcount or 0
                        log.info(
                            "  game %s: deleted %d existing pitch rows before reload",
                            replace_game_pk,
                            deleted,
                        )

                    # Non-pitch play events, INSIDE this same
                    # transaction. `_write_play_events` deletes the game's rows
                    # first, so a load and a reload behave identically and a
                    # re-run cannot double-count a pickoff. If the pitch insert
                    # below rolls back, these roll back with it — a game must
                    # never keep play events whose pitches were never committed.
                    if count_pk is not None:
                        written = self._write_play_events(
                            cur,
                            count_pk,
                            list(play_event_rows or []),
                            replace=replace_game_pk is not None,
                        )
                        if written:
                            log.info(
                                "  game %s: wrote %d non-pitch play events",
                                count_pk,
                                written,
                            )

                    def _count() -> int | None:
                        """COUNT(*) for the game, or None if it can't be read."""
                        if count_pk is None:
                            return None
                        cur.execute(count_sql, (count_pk,))
                        row = cur.fetchone()
                        return row[0] if row else None

                    before = _count()

                    for offset in range(0, len(rows), BATCH_SIZE):
                        batch = rows[offset : offset + BATCH_SIZE]
                        psycopg2.extras.execute_batch(cur, self.INSERT_SQL, batch)

                    after = _count()
                    if before is None or after is None:
                        # No single game to bracket (or the count was unreadable):
                        # fall back to the attempted count rather than reporting 0.
                        inserted = len(rows)
                    else:
                        inserted = after - before

                    # Shrink guard.  A reload that writes back FEWER rows than it
                    # deleted is data loss, not a reload — and it is reachable
                    # without any exception: _fetch_game_pitches returns
                    # ([], game_dict) for an HTTP-200 feed whose allPlays is empty
                    # or pitch-less (a postponed/suspended game, a feed blip, or a
                    # not-yet-played game reached through load_date_range, which
                    # has no Final gate).  Left unguarded, a sweep would delete a
                    # complete game and commit nothing in its place.  Raising here
                    # lands in the except below, which rolls the DELETE back.
                    if replace_game_pk is not None and not allow_shrink and inserted < deleted:
                        raise ReloadShrinkError(
                            f"reload of game {replace_game_pk} would delete {deleted} pitch "
                            f"rows and write back only {inserted}; rolled back. Re-run once "
                            f"the feed is healthy, or pass allow_shrink=True if the game "
                            f"genuinely has fewer pitches now."
                        )

                    # Ledger + outcome, in this same transaction.
                    if error_rows and count_pk is not None:
                        self._write_error_ledger(cur, count_pk, error_rows)
                    if count_pk is not None and season is not None:
                        self._write_ingest_outcome(
                            cur,
                            count_pk,
                            season,
                            pitch_rows=inserted,
                            skipped_rows=len(error_rows or []),
                        )

                conn.commit()
            except Exception:
                conn.rollback()
                raise

        if rows and inserted < len(rows):
            if replace_game_pk is not None:
                # The DELETE ran first, so "already present" is impossible here:
                # a shortfall means the batch itself carried duplicate
                # (game_pk, at_bat_number, pitch_number) keys, i.e. a parser bug.
                log.warning(
                    "  game %s: %d of %d parsed rows collided WITHIN the batch and were "
                    "dropped by ON CONFLICT — duplicate (at_bat_number, pitch_number) keys "
                    "from _fetch_game_pitches.",
                    count_pk,
                    len(rows) - inserted,
                    len(rows),
                )
            else:
                log.warning(
                    "  game %s: %d of %d rows were discarded by ON CONFLICT DO NOTHING "
                    "(rows already present). Use reload_game() to overwrite them.",
                    count_pk,
                    len(rows) - inserted,
                    len(rows),
                )

        return inserted

    def _log_freshness(self, game_pk: int, rows: list[dict[str, Any]]) -> None:
        """
        Upserts raw.etl_data_freshness so the system knows the last loaded
        game_date per pitcher and per batter.

        The table's DDL lives in db/schemas/01_postgres_schema.sql (Alembic
        migration 0003).
        """
        if not rows:
            return

        game_date = rows[0].get("game_date")
        pitcher_ids = {r["pitcher"] for r in rows if r.get("pitcher")}
        batter_ids = {r["batter"] for r in rows if r.get("batter")}

        upsert_sql = """
            INSERT INTO raw.etl_data_freshness (entity_type, entity_id, last_game_pk, last_date)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (entity_type, entity_id) DO UPDATE
                SET last_game_pk = EXCLUDED.last_game_pk,
                    last_date    = EXCLUDED.last_date,
                    updated_at   = NOW()
            WHERE EXCLUDED.last_date > raw.etl_data_freshness.last_date;
        """
        entries = [("pitcher", pid, game_pk, game_date) for pid in pitcher_ids] + [
            ("batter", bid, game_pk, game_date) for bid in batter_ids
        ]

        with self._get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, upsert_sql, entries)
            conn.commit()
