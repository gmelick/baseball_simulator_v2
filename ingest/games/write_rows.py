"""Write a game's rows: the pitch rows, the play events, the error ledger, the ingest
outcome and the freshness record.
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


BATCH_SIZE = 500  # rows per execute_batch call


class ReloadShrinkError(RuntimeError):
    """A reload would replace a game's pitch rows with fewer rows.

    ``_batch_insert`` raises it BEFORE the commit, so the DELETE rolls back and the
    stored rows survive. The usual cause is a transient feed that answers HTTP 200
    with an empty or pitch-less ``allPlays``; rerun the reload once the feed is
    healthy. Pass ``allow_shrink=True`` when the game truly has fewer pitches than
    the stored copy (for example, a stored copy that holds the game twice).
    """


class WritesMixin(LoaderBase):
    """Writes a game's rows and the run's records."""

    def _game_already_loaded(self, game_pk: int) -> bool:
        """Return True when the game is loaded, so a range load skips it.

        A pitch row, or a terminal ``raw.etl_game_ingest`` record, counts as
        loaded. The record covers a game that produced no pitch rows (every row
        failed validation, or the feed has no pitches); without it, the nightly
        run re-fetches that game every night. The reload path never calls this
        method, so a reload still reaches a game after a parser fix.
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
        """Build, validate and insert a game's pitch rows; return the counts.

        Returns ``{"inserted", "skipped", "flagged"}``. ``replace=True`` (the reload)
        deletes the game's stored ``raw.pitches`` rows in the insert's transaction,
        so the new rows replace them; ``ON CONFLICT … DO NOTHING`` would otherwise
        discard the new rows.

        A row with a hard error goes to ``raw.etl_errors`` as well as to the log,
        so a re-ingest knows which pitches to recover.
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

        # The ledger is written INSIDE the pitch transaction (see _batch_insert):
        # a pitch insert that rolls back takes its error rows with it, so no
        # ledger row describes a game that was not written.
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
        """Write one ``raw.etl_errors`` row per skipped pitch, idempotently.

        It runs on the CALLER's cursor, inside the pitch-insert transaction. The
        upsert on the natural key keeps a second load of the same game from adding
        a duplicate error set, so a count of the ledger's rows stays true.
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
        """Record the game's terminal outcome in ``raw.etl_game_ingest``.

        ``outcome='empty'`` marks a game that loaded with zero pitch rows (every
        row failed validation, or the feed has no pitches). Pitch rows alone cannot
        tell that game from one never loaded, so without the record the nightly
        run re-fetches it forever. The reload path ignores this table, so a reload
        still reaches the game after a parser fix.
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

    #: Whether ``raw.play_events`` exists: ``None`` until the first probe (see
    #: ``LoaderBase._exists_once``).
    _play_events_table_exists: bool | None = None

    #: One pitch row. ON CONFLICT DO NOTHING keeps an ordinary load idempotent;
    #: the reload path deletes the game's rows first.
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

    #: One non-pitch play event. ON CONFLICT DO NOTHING keeps a partial rerun
    #: idempotent; the reload path deletes first, so a reload still replaces the
    #: rows.
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
        """Write the game's non-pitch play events; return the rows written.

        It takes the CALLER's cursor, so it joins the game's one write transaction:
        when the pitch insert rolls back, these rows roll back with it. A game
        never keeps play events whose pitches were not committed.

        It deletes only when ``replace`` is set, as the pitch rows do. A reload
        replaces, so stale rows must go; an ordinary load relies on the
        natural-key ``ON CONFLICT DO NOTHING`` to stay idempotent.
        """
        # Skip the write when the table is absent: the statements below would
        # raise UndefinedTable and abort the game's whole transaction.
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
        """Write one game in one transaction; return the pitch rows ACTUALLY written.

        The transaction holds the optional DELETE, the play events, the pitch rows
        (in ``BATCH_SIZE`` chunks), the ``raw.etl_errors`` ledger and the
        ``raw.etl_game_ingest`` outcome. They commit or roll back together, so a
        failed reload never leaves a game half-deleted.

        ``replace_game_pk``
            Delete every stored ``raw.pitches`` row of this game first. This is the
            only way to correct loaded pitch data: the insert carries
            ``ON CONFLICT … DO NOTHING``, so without the delete a re-ingest writes
            nothing.

        The return value is measured: an exact COUNT of the game's rows brackets
        the insert. ``execute_batch`` reports no accurate row count, and the count
        of rows attempted cannot tell a full write from a discarded one.

        Raises :class:`ReloadShrinkError` when a reload writes back fewer rows than
        it deleted, unless ``allow_shrink`` is set.
        """
        if not rows and replace_game_pk is None and not error_rows and season is None:
            return 0

        # The game this batch belongs to. The caller's pk wins; the rows are only
        # a fallback. When every row of a game fails validation, ``rows`` is
        # empty, and without the caller's pk the ledger and the outcome below are
        # not written: for exactly the games that most need a record.
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

                    # The non-pitch play events, INSIDE this transaction: if the
                    # pitch insert below rolls back, they roll back with it.
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
                        """Return the game's row count, or None when there is no single game."""
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
                        # No single game to count: report the rows attempted, not 0.
                        inserted = len(rows)
                    else:
                        inserted = after - before

                    # The shrink guard. A reload that writes back fewer rows than
                    # it deleted loses data, with no exception on the way:
                    # _fetch_game_pitches returns no rows for an HTTP-200 feed whose
                    # allPlays is empty or pitch-less (a postponed game, a feed
                    # blip). The raise lands in the except below, which rolls the
                    # DELETE back.
                    if replace_game_pk is not None and not allow_shrink and inserted < deleted:
                        raise ReloadShrinkError(
                            f"reload of game {replace_game_pk} would delete {deleted} pitch "
                            f"rows and write back only {inserted}; rolled back. Re-run once "
                            f"the feed is healthy, or pass allow_shrink=True if the game "
                            f"genuinely has fewer pitches now."
                        )

                    # The ledger and the outcome, in this same transaction.
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
                # The DELETE ran first, so no row was already present: a shortfall
                # means duplicate keys inside the batch, which is a parser bug.
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
        """Record each pitcher's and batter's last loaded game in ``raw.etl_data_freshness``.

        An older game never overwrites a newer one. The table's DDL lives in
        ``db/schemas/01_postgres_schema.sql``.
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
