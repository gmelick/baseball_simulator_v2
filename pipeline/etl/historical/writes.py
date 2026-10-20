"""The loader's writes: the pitch rows, the play events, the error ledger, the
ingest outcome and the freshness and quality reports.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool

from db.introspect import pg_relation_exists
from pipeline.etl.historical.base import LoaderBase
from pipeline.etl.historical.rows import (
    _build_row_dict,
    _validate_row,
)

log = logging.getLogger("etl_historical_loader")


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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _game_already_loaded(self, game_pk: int) -> bool:
        """True if this game has been ingested and should be skipped.

        SIM-441: this used to be `SELECT 1 FROM raw.pitches … LIMIT 1` alone,
        which cannot distinguish "never loaded" from "loaded and legitimately
        produced zero pitch rows" — a game whose every row hard-errors, or one
        the feed simply has no pitches for. Such a game was re-fetched and
        re-processed on EVERY nightly run, forever, appending a duplicate error
        set each time and never making progress. A terminal
        ``raw.etl_game_ingest`` record now also counts as loaded.

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

        SIM-093: Hard-error rows are now persisted to raw.etl_errors as well
        as logged.  Without this, skipped rows had no audit trail and there
        was no way to find which pitches a re-ingest would need to recover.
        """
        to_insert: list[dict[str, Any]] = []
        hard_errors: list[tuple[int, int, list[str]]] = []  # (at_bat, pitch, errors)
        flagged_count = 0

        for raw_row in raw_rows:
            row = _build_row_dict(raw_row, game_pk, season, raw_row.get("game_date", ""))
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

        # SIM-441: the ledger is written INSIDE the pitch transaction (see
        # _batch_insert). It used to commit first, on its own connection, which
        # made it structurally blind to insert-path losses: if the pitch insert
        # then failed and rolled back, the errors survived describing a game that
        # had not been written, and a rolled-back reload left orphan rows behind.
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
        """SIM-093/SIM-441: one raw.etl_errors row per skipped pitch, idempotently.

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
        """SIM-441: record the terminal per-game ETL outcome.

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

    def reprocess_errored_games(self, since: date) -> list[int]:
        """
        SIM-093: Return distinct game_pks that have raw.etl_errors rows
        on or after ``since``.  Caller iterates the list and reruns
        load_game(game_pk, season) for each — typical use is "after a
        validator bug-fix, find all games that were affected and re-ingest".

        Returns an empty list if no errors are recorded in the window.
        """
        sql = """
            SELECT DISTINCT game_pk
            FROM   raw.etl_errors
            WHERE  created_at >= %s
              AND  game_pk IS NOT NULL
            ORDER BY game_pk
        """
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (since,))
            return [r[0] for r in cur.fetchall()]

    #: SIM-502: non-pitch play events.  ON CONFLICT DO NOTHING makes a partial
    #: re-run idempotent; the reload path DELETEs first so a genuine replace still
    #: replaces rather than silently keeping the old rows.
    #: SIM-502: tri-state cache of "does `raw.play_events` exist".  ``None`` means
    #: not yet probed.  Cached because a 2,430-game sweep must not run 2,430
    #: identical catalogue lookups, and because the answer cannot change mid-run:
    #: applying a migration while a sweep is in flight is not a supported move.
    _play_events_table_exists: bool | None = None

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
        # SIM-502: SKIP ENTIRELY when the table is absent.
        #
        # Alembic 0018 creates `raw.play_events`, and this code lands BEFORE that
        # migration is applied anywhere. Without this guard the statements below
        # raise `UndefinedTable`, which aborts the whole game transaction — so
        # merely landing the code would break every nightly ETL run until someone
        # ran the migration. All four SIM-502 defects (a..d) closed 2026-08-13;
        # the guard stays until 0018 is APPLIED, and is harmless after (the
        # probe then answers true once per run).
        if self._play_events_table_exists is None:
            self._play_events_table_exists = pg_relation_exists(cur, "raw.play_events")
            if not self._play_events_table_exists:
                log.info(
                    "raw.play_events is absent (Alembic 0018 not applied) — "
                    "SIM-502 play-event capture is INERT for this run"
                )
        if not self._play_events_table_exists:
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

        SIM-441: this is now the SINGLE transaction for one game's write — the
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
        # SIM-441 fix: this used to infer ONLY from ``rows``. When every row of a
        # game hard-errors, ``rows`` is empty, so the inference produced None and
        # BOTH the error ledger and the ingest-outcome write below were silently
        # skipped — for exactly the games that most need an audit trail. That was
        # a regression (the old ``_log_etl_errors`` call passed game_pk
        # explicitly) and it also made ``outcome='empty'`` unreachable on the
        # non-reload path, defeating the whole point of raw.etl_game_ingest.
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

                    # SIM-502: non-pitch play events, INSIDE this same
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

        Table DDL lives in db/schemas/01_postgres_schema.sql and is applied via
        Alembic migration 0003.  SIM-083 removed the dead FRESHNESS_TABLE_DDL
        string constant that previously appeared at module level — it was never
        executed and caused UndefinedTable errors in _process_and_insert().
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

    # ------------------------------------------------------------------
    # Post-load quality report
    # ------------------------------------------------------------------

    def quality_report(self, game_pk: int | None = None) -> None:
        """
        Prints a summary of data_quality_flag=TRUE rows.
        Scoped to a specific game if game_pk is provided.
        """
        with self._get_conn() as conn, conn.cursor() as cur:
            if game_pk:
                cur.execute(
                    """
                        SELECT pitcher, COUNT(*) AS flagged_pitches
                        FROM raw.pitches
                        WHERE data_quality_flag = TRUE AND game_pk = %s
                        GROUP BY pitcher ORDER BY flagged_pitches DESC
                        """,
                    (game_pk,),
                )
            else:
                cur.execute(
                    """
                        SELECT game_date, COUNT(*) AS flagged_pitches
                        FROM raw.pitches
                        WHERE data_quality_flag = TRUE
                        GROUP BY game_date ORDER BY game_date DESC
                        LIMIT 30
                        """
                )
            rows = cur.fetchall()

        if not rows:
            log.info("No flagged rows found.")
            return

        header = "pitcher / flagged" if game_pk else "date / flagged"
        log.info("Quality report (%s):", header)
        for r in rows:
            log.info("  %s -> %s flagged rows", r[0], r[1])
